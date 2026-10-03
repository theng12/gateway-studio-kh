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

**Paid members are a last resort** (owner decision 2026-10-03). Members are split free / paid by
the catalogue's own rule (`pricing.model_chargeable`). In a pool that has both, the free members
are walked first; when none is ready and one is only on a short limit (back within
`FREE_WAIT_HORIZON_S`), the job **waits** for it rather than paying. A paid member is tried only
when every free member is out for the day, unavailable, or failed this job for a non-quota reason.

The members are resolved through `catalog.resolve`, never from the raw strings, so
a member the owner has since disabled is skipped for the same reason it would be
refused on its own: a toggle has to still be a toggle when the job runs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..providers.definitions import KIND_POOL
from .base import Backend, BackendError, BackendRequest, BackendResult, requests_started

log = logging.getLogger(__name__)

#: A free member back within this long is on a per-minute class of limit: wait for it, never pay.
FREE_WAIT_HORIZON_S = 5 * 60.0
#: The most one job waits for free members in total. Past it the pool fails (retryable) — still
#: without paying, since every free member was only on a short limit.
FREE_WAIT_CEILING_S = 15 * 60.0
#: How often a wait looks for the job's cancellation.
WAIT_STEP_S = 1.0

#: Replaced in tests.
_sleep = time.sleep


def _cancel_requested(request: BackendRequest) -> bool:
    """Whether the owner cancelled the durable job this request runs for (never, for the
    synchronous route, which has no job)."""
    job_id = (request.params or {}).get("_job_id")
    if not job_id:
        return False
    from ..runtime import store

    return (store.get(str(job_id)) or {}).get("cancel_requested_at") is not None


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


def _outcome_known(error: BackendError, sent: bool) -> bool:
    """Whether this member's failure leaves nothing outstanding upstream.

    The worker's own rules, applied per member: nothing was sent, the provider refused it (a 4xx),
    the adapter proved it accepted nothing, or it followed its own id to a terminal answer.
    """
    if error.code == "PROVIDER_SUBMISSION_UNCERTAIN":
        return False
    return (not sent or error.upstream_accepted is False or error.upstream_settled
            or (isinstance(error.status, int) and 400 <= error.status < 500))


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
        from .. import pool_cooldowns, pools, pricing

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
        #: `last` is the last real failure, `tried` the members called, `stop` set by a request-shaped
        #: failure. `ambiguous`: some member may have left work running upstream. Until one does, the
        #: pool's failure is a known one — a member that failed before sending anything must not make
        #: it "unknown".
        walk: dict[str, Any] = {"last": None, "tried": 0, "ambiguous": False, "stop": False}

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

        def call(member: str, definition: Any, model: dict[str, Any], paid: bool) -> BackendResult | None:
            walk["tried"] += 1
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
            sends_before = requests_started()
            try:
                result = self._adapter_for(definition).generate(member_request)
            except BackendError as exc:
                walk["ambiguous"] = walk["ambiguous"] or not _outcome_known(
                    exc, requests_started() != sends_before)
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
                walk["last"] = exc
                pool_cooldowns.note_failure(member, exc)
                # The body is wrong. Every remaining member would say so too.
                walk["stop"] = _is_request_shaped_failure(exc)
                return None
            except Exception as exc:  # noqa: BLE001
                # An adapter bug on one member must not take the pool down with it.
                # This is the same reasoning as the chat route's broad catch, one
                # level in: the next member may well be a different code path.
                walk["ambiguous"] = walk["ambiguous"] or requests_started() != sends_before
                attempts.append(
                    {"member": member, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"[:400]}
                )
                walk["last"] = BackendError(str(exc), code="BACKEND_ERROR", status=502)
                return None

            pool_cooldowns.clear(member)
            payload = dict(result.payload or {})
            payload["pool"] = {
                "name": request.model_id,
                "attempts": attempts + [{"member": member, "outcome": "answered", "paid": paid}],
                "used": member,
                "paid": paid,
                "tried": walk["tried"],
            }
            result.payload = payload
            return result

        def in_order(group: list[tuple[str, Any, dict[str, Any]]], paid: bool) -> BackendResult | None:
            """**Skip what is out of quota, until its reset.** A member that already said "quota" is
            not asked again every job. But never fail outright because of a memory: if every member of
            `group` is cooling, the one that recovers soonest is tried, since the provider's window
            may have lifted early."""
            cooling = {c[0]: pool_cooldowns.until(c[0]) for c in group}
            ready = [c for c in group if cooling[c[0]] is None]
            if not ready and group:
                ready = [min(group, key=lambda c: cooling[c[0]] or 0.0)]
            for member, _definition, _model in group:
                if cooling[member] is not None and all(member != r[0] for r in ready):
                    attempts.append(
                        {"member": member, "outcome": "cooling-down", "until": cooling[member]}
                    )
            for member, definition, model in ready:
                answer = call(member, definition, model, paid)
                if answer is not None or walk["stop"]:
                    return answer
            return None

        # **Free first; a paid member only when the free ones are out for the day.** Classified by
        # the catalogue's own rule, so "paid" here is the Models page's Paid. A pool that is all one
        # kind walks as it always has.
        paid = [c for c in candidates if pricing.model_chargeable(c[1], c[2])]
        free = [c for c in candidates if c not in paid]
        if free and paid:
            result = self._free_first(request, free, call, walk, attempts)
            if result is None and not walk["stop"]:
                result = in_order(paid, True)
        else:
            result = in_order(candidates, bool(paid))
        if result is not None:
            if free and result.payload["pool"]["paid"]:
                # Money was spent while the pool has free members: say so where the owner looks.
                log.warning("pool %s: free members out for the day → used paid member %s",
                            request.model_id, result.payload["pool"]["used"])
            return result

        # Nothing answered. The failure is the last real one, so the job's code and
        # retryability still describe a provider rather than a pool.
        last: BackendError | None = walk["last"]
        detail = "; ".join(
            f"{a['member']}: {a.get('detail') or a['outcome']}" for a in attempts
        )[:600]
        raise BackendError(
            f"Pool '{request.model_id}' had no member that could answer — {detail}",
            code=(last.code if last else "POOL_EXHAUSTED"),
            status=(last.status if last else 502),
            retryable=bool(last.retryable) if last else False,
            upstream_accepted=None if walk["ambiguous"] else False,
        )

    def _free_first(self, request: BackendRequest, free: list[tuple[str, Any, dict[str, Any]]],
                    call: Any, walk: dict[str, Any], attempts: list[dict[str, Any]]) -> BackendResult | None:
        """Walk the free members, **waiting** for one on a short limit rather than paying.

        Returns the answer, or `None` when every free member is out for the day, unavailable or
        failed this job for a reason that is not quota — the only case a paid member may be used.
        Waiting is bounded by `FREE_WAIT_CEILING_S`; past it the pool gives up *without* paying (the
        owner's rule: a per-minute limit is never a reason to spend money).
        """
        from .. import pool_cooldowns

        started = pool_cooldowns._clock()
        spent: set[str] = set()  # failed this job for something other than quota
        while True:
            ready = [c for c in free if c[0] not in spent and pool_cooldowns.until(c[0]) is None]
            for member, definition, model in ready:
                answer = call(member, definition, model, False)
                if answer is not None or walk["stop"]:
                    return answer
                if pool_cooldowns.until(member) is None:
                    spent.add(member)
            if ready:
                continue  # judge again: what just failed may only be out for a minute
            back = {c[0]: pool_cooldowns.until(c[0]) for c in free if c[0] not in spent}
            back = {m: u for m, u in back.items() if u is not None}
            now = pool_cooldowns._clock()
            soonest = min(back, key=back.get) if back else None
            if soonest is None or back[soonest] - now > FREE_WAIT_HORIZON_S:
                for member, until in back.items():
                    attempts.append({"member": member, "outcome": "cooling-down", "until": until})
                return None
            if back[soonest] - started > FREE_WAIT_CEILING_S:
                walk["stop"] = True  # waited long enough; fail rather than pay for a minute's limit
                attempts.append({"member": soonest, "outcome": "wait-ceiling", "until": back[soonest]})
                return None
            attempts.append({"member": soonest, "outcome": "waited", "seconds": round(back[soonest] - now, 1)})
            self._wait_until(request, back[soonest], soonest, walk)

    def _wait_until(self, request: BackendRequest, until: float, member: str, walk: dict[str, Any]) -> None:
        """Sleep in small steps until `until`, telling the job why, and stop on a cancellation."""
        from .. import pool_cooldowns

        if request.on_wait is not None:
            request.on_wait({"reason": f"free models are on a per-minute limit; waiting for {member}",
                             "next_try_at": until, "since": pool_cooldowns._clock()})
        try:
            while (left := until - pool_cooldowns._clock()) > 0:
                if _cancel_requested(request):
                    raise BackendError(
                        f"Pool '{request.model_id}': cancelled while waiting for a free member.",
                        code="POOL_WAIT_CANCELLED",
                        upstream_accepted=None if walk["ambiguous"] else False,
                        upstream_settled=not walk["ambiguous"],
                    )
                _sleep(min(WAIT_STEP_S, left))
        finally:
            if request.on_wait is not None:
                request.on_wait(None)

    def _credentials_for(self, provider_id: str) -> dict[str, str]:
        from ..providers import registry

        return registry.credentials.resolved(provider_id)


__all__ = ["PoolBackend"]
