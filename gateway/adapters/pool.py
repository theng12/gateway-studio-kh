"""The pool adapter — one name, several models, tried in order.

This is where "try the next one" lives, and it is here rather than in the chat
route on purpose. The route resolves exactly one model and calls `generate`, the
same as it does for every vendor, so the synchronous and durable paths behave
identically and neither had to learn about pools. A pool is a provider whose
`generate` happens to make more than one call.

The rule for advancing
----------------------
**Advance on anything except a request the gateway itself got wrong.** The
asymmetry is the whole design:

* Advancing when we should have stopped costs a few provider round-trips. The
  request was never going to work, so the caller still gets the same failure —
  later, and with the attempts named.
* Stopping when we should have advanced costs the job. A spent free lane, a
  retired model, a provider refusing one account — none of those are the
  request's fault, and all of them are exactly what a pool exists to walk past.

So the only condition that stops the walk is `status == 400` **and** not
`retryable`: the upstream saying the body itself is wrong, which the next model
would reject for the same reason. Everything else — 401, 402, 403, 404, 429,
5xx, a timeout, a connection error, a retryable 400 — advances.

**Quota memory.** A member that fails with a quota or rate-limit signal is remembered
(`pool_cooldowns.py`) and skipped by later jobs until its reset, so a spent free lane
costs one round-trip, not one per job. If every member is cooling, the one recovering
soonest is tried rather than failing outright; a success clears the memory.

The members are resolved through `catalog.resolve`, never from the raw strings, so
a member the owner has since disabled is skipped for the same reason it would be
refused on its own: a toggle has to still be a toggle when the job runs.
"""

from __future__ import annotations

from typing import Any

from ..providers.definitions import KIND_POOL
from .base import Backend, BackendError, BackendRequest, BackendResult


def _is_request_shaped_failure(error: BackendError) -> bool:
    """Whether trying another model is pointless because the request is wrong.

    Deliberately narrow. A 400 that the adapter marked retryable is *not* the
    request's fault, and a 400 from one vendor about one model is often that
    model's own parameter surface rather than the caller's mistake — the
    catalogue publishes per-model ranges precisely because they differ. Only an
    un-retryable 400 is read as "the body is wrong", and even then the attempts
    are reported so the answer is actionable rather than bare.
    """
    return error.status == 400 and not error.retryable


class PoolBackend(Backend):
    def __init__(self) -> None:
        # Injected by `adapters/__init__.py` after construction. A module-level
        # import of the catalogue here would be a cycle: the catalogue imports
        # the adapter table to decide what can be published.
        self._resolve: Any = None
        self._adapter_for: Any = None

    def attach(self, resolve: Any, adapter_for: Any) -> None:
        self._resolve = resolve
        self._adapter_for = adapter_for

    def generate(self, request: BackendRequest) -> BackendResult:
        from .. import pool_cooldowns, pools

        members = pools.members(request.model_id)
        if not members:
            # The catalogue publishes a pool only while it has members, so this is
            # a pool deleted between publish and use. Named rather than silently
            # answered with something else.
            raise BackendError(
                f"Pool '{request.model_id}' has no members.",
                code="POOL_EMPTY",
                status=404,
            )

        attempts: list[dict[str, Any]] = []
        last: BackendError | None = None
        tried = 0

        # Resolve first, so "every member is cooling down" is judged over the members that
        # could answer at all (a disabled one is not a candidate and must not hide the rule).
        candidates: list[tuple[str, Any, dict[str, Any]]] = []
        for member in members:
            definition, model = self._resolve("chat", member)
            if definition is None or model is None:
                # Disabled, or the provider was switched off, since the pool was
                # written. Recorded, not retried: nothing to call.
                attempts.append({"member": member, "outcome": "unavailable"})
            elif definition.kind == KIND_POOL:
                # Refused at the door when the pool is written; refused again here
                # because a hand-edited pools.json is not the admin route.
                attempts.append({"member": member, "outcome": "nested-pool-refused"})
            else:
                candidates.append((member, definition, model))

        # **Skip what is out of quota, until its reset.** A member that already said
        # "quota" is not asked again every job. But never fail outright because of a
        # memory: if every candidate is cooling, the one that recovers soonest is tried,
        # since the provider's window may have lifted early.
        cooling = {c[0]: pool_cooldowns.until(c[0]) for c in candidates}
        ready = [c for c in candidates if cooling[c[0]] is None]
        if not ready and candidates:
            ready = [min(candidates, key=lambda c: cooling[c[0]] or 0.0)]
        for member, _definition, _model in candidates:
            if cooling[member] is not None and all(member != r[0] for r in ready):
                attempts.append(
                    {"member": member, "outcome": "cooling-down", "until": cooling[member]}
                )

        for member, definition, model in ready:
            tried += 1
            member_request = BackendRequest(
                provider_id=definition.id,
                model_id=model["model_id"],
                modality="chat",
                params=request.params,
                values=self._credentials_for(definition.id),
                base_url=definition.base_url,
                display_name=definition.display_name,
                checkpoint=request.checkpoint,
            )
            try:
                result = self._adapter_for(definition).generate(member_request)
            except BackendError as exc:
                attempts.append(
                    {
                        "member": member,
                        "outcome": "failed",
                        "code": exc.code,
                        "status": exc.status,
                        "retryable": bool(exc.retryable),
                        "detail": str(exc)[:400],
                    }
                )
                last = exc
                pool_cooldowns.note_failure(member, exc)
                if _is_request_shaped_failure(exc):
                    # The body is wrong. Every remaining member would say so too.
                    break
                continue
            except Exception as exc:  # noqa: BLE001
                # An adapter bug on one member must not take the pool down with it.
                # This is the same reasoning as the chat route's broad catch, one
                # level in: the next member may well be a different code path.
                attempts.append(
                    {"member": member, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"[:400]}
                )
                last = BackendError(str(exc), code="BACKEND_ERROR", status=502)
                continue

            pool_cooldowns.clear(member)
            payload = dict(result.payload or {})
            payload["pool"] = {
                "name": request.model_id,
                "attempts": attempts + [{"member": member, "outcome": "answered"}],
                "used": member,
                "tried": tried,
            }
            result.payload = payload
            return result

        # Nothing answered. The failure is the last real one, so the job's code and
        # retryability still describe a provider rather than a pool.
        detail = "; ".join(
            f"{a['member']}: {a.get('detail') or a['outcome']}" for a in attempts
        )[:600]
        raise BackendError(
            f"Pool '{request.model_id}' had no member that could answer — {detail}",
            code=(last.code if last else "POOL_EXHAUSTED"),
            status=(last.status if last else 502),
            retryable=bool(last.retryable) if last else False,
        )

    def _credentials_for(self, provider_id: str) -> dict[str, str]:
        from ..providers import registry

        return registry.credentials.resolved(provider_id)


__all__ = ["PoolBackend"]
