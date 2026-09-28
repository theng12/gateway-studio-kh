"""Background worker — executes queued jobs off the request thread.

Why this exists rather than generating inline: a local FLUX image on a busy Mac
can take minutes, and Studio Hub jobs are themselves asynchronous. If the
gateway generated inside the HTTP handler, every Story Studio poll would block
for the duration and its own timeouts would fire. So the request path only ever
*accepts* and *reads*; the worker does the waiting.

**It used to be one thread, deliberately.** That was right for one owner and one
Mac, and the reason is recorded verbatim: *"a second thread would add concurrency
bugs (double-claiming, artifact races) for no benefit."* Both named risks were
re-checked before this changed, and both are smaller than the note claims:

  * **double-claiming** — `mark_running` is a conditional `UPDATE … WHERE state =
    'accepted'` returning `rowcount == 1`, so two threads racing cannot both win;
  * **artifact races** — paths are per job (`{job_id}.{ext}`,
    `{job_id}-{index}.{ext}`), retention has a one-hour floor, and
    `record_artifacts` merges into the job row, which is safe with one worker per
    job.

What is *not* smaller is the cost of the old shape: with a fleet, one slot means
nineteen idle Macs. Concurrency is now bounded rather than absent — see
`gateway/concurrency.py` for the three ceilings and where each number comes from.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import concurrency
from .adapters import BackendError, BackendRequest, adapter_for
from .config import ARTIFACT_DIR
from .providers import registry
from .store import RECOVERY_UNKNOWN, JobStore
from . import fleet

#: Modality → the Hub's own operation name, for reading a site's per-operation
#: capacity. The Hub spells these differently from the catalogue (`chat.completion`
#: where the catalogue says `chat.response`), so the translation is explicit rather
#: than assumed — a wrong key reads as "the site said nothing" and silently falls
#: back to one at a time.
#: **Imported, not restated.** This table and `fleet`'s copy had drifted — the fleet one
#: was missing `chat` — and admission compares its operation's numbers with routing's.
#: `fleet.HUB_OPERATION_BY_MODALITY` is the single definition.
_HUB_OPERATION_BY_MODALITY = fleet.HUB_OPERATION_BY_MODALITY

#: "Not now" rather than "not ever" — the site could not take this job *this time*.
#: **Exactly the set the worker already re-routes on**, which is the point: if a
#: condition is worth trying another location for, it is worth waiting for this one.
#: Both prove the upstream accepted nothing, which is what makes a retry safe;
#: `SITE_CAPACITY_UNAVAILABLE` is deliberately absent because a submit that may have
#: left the building is an unknown outcome, and those get a recovery hold instead.
_TRANSIENT_SITE_CODES = {"LOCATION_NOT_READY", "LOCATION_UNREACHABLE"}

#: How long a job will wait for a busy fleet before it is reported as failed.
#: ponytail: a flat eight deferrals with a backoff capped at a minute — about four
#: minutes in total, and nine dispatch attempts. Ceiling — a fleet busy for longer
#: than that still fails work it would eventually have run. Upgrade path — make both
#: numbers settings beside the pool size, which is where an owner would look.
#: **Editable now** — `concurrency.queue_wait_budget()` reads `queue.max_deferrals` and
#: `queue.defer_cap_seconds` from the same settings file as the pool size, live, without a restart.
#: These two names remain as the defaults a fresh profile starts from, because tests and callers
#: refer to them.
_MAX_DEFERRALS = concurrency.DEFAULT_MAX_DEFERRALS
_DEFER_CAP_S = concurrency.DEFAULT_DEFER_CAP_S


def _defer_delay(attempts: int, cap_s: float | None = None) -> float:
    """2s, 4s, 8s … capped. Long enough not to hammer a busy machine."""
    return min(2.0 ** max(int(attempts), 0), _DEFER_CAP_S if cap_s is None else float(cap_s))


def _next_machine(store: JobStore, job_id: str, model_id: str, modality: str,
                  provider_id: str, *, code: str, detail: str) -> str | None:
    """Record this attempt and name the next machine to try, or `None`.

    A7's whole decision, in one place. Returns the provider id of a machine that has **not**
    been tried, or `None` when there is nothing left to try — either because the attempts are
    used up (so the caller must fail the job properly) or because no other machine offers the
    model (so the caller must wait rather than burn an attempt).

    **The attempt is recorded before the choice, not after.** The log is what the ceiling is
    measured against and what the terminal failure is built from, and a crash between "we tried
    it" and "we remembered" would make the same machine eligible again.

    **The exclusion is the job's own record, not a local set.** The old `tried` set lived inside
    one dispatch call: a job that deferred and came back — or that was redelivered after a
    restart — began with an empty exclusion and could be sent to the machine that had just
    failed it. That is the difference this fixes, and it is why the list is a column.
    """
    policy = concurrency.retry_policy()
    # **The two id spellings, normalised in one place.** An attempt log stores the *provider* id
    # that routed the work (`studio-hub-9701220bf30f`) because that is what names a machine to an
    # operator; `fleet.route` compares `exclude_locations` against the bare **controller** id
    # (`9701220bf30f`). Passing one where the other is expected makes the exclusion **inert**, and
    # the defect this whole item exists to close is exactly that mismatch (audit H5) — so it is
    # normalised here rather than at either end, and the same function is applied to the route's
    # answer so the two can never drift.
    #
    # It came back the first time in this shape and the §9 rehearsal caught it: with three
    # locations enabled every job still stopped at `attempts_used: 2` while publishing
    # `attempts_allowed: 3`, because the lottery could re-pick a site that was already tried,
    # `chosen.id in tried` then rejected it, and the worker read that as "nothing left to try".
    def _location(value: Any) -> str:
        return str(fleet.controller_id_from(str(value or "")) or value or "")

    record = getattr(store, "record_attempt", None)
    if record is None:
        # A store without the record still gets the old one-step behaviour rather than none.
        tried = {_location(provider_id)}
        chosen, _ = fleet.route(model_id, modality, exclude_locations=tried)
        return chosen.id if chosen is not None and _location(chosen.id) not in tried else None
    attempts = record(job_id, location_id=provider_id, code=code, detail=detail) or []
    tried = {_location(entry.get("location")) for entry in attempts}
    tried.add(_location(provider_id))
    tried.discard("")
    if len(attempts) >= int(policy["max_attempts"]):
        # The ceiling the owner named. The failure path builds the sentence from this log.
        return None
    chosen, _note = fleet.route(model_id, modality, exclude_locations=tried)
    if chosen is None or _location(chosen.id) in tried:
        return None
    record_route = getattr(store, "record_route", None)
    if record_route is not None:
        record_route(job_id, chosen.id)
    return chosen.id


def _tried_everything_detail(attempts: list[dict[str, Any]], *, limit: int) -> str:
    """The terminal reason: every machine that was tried, and why each one failed.

    The owner asked for a *proper* failure rather than an endless defer, and a proper failure
    names what happened. "All machines failed" is not actionable; naming the site, its code and
    its own sentence is.
    """
    parts = []
    for entry in attempts[:limit]:
        where = str(entry.get("location") or "an unnamed machine")
        why = str(entry.get("code") or "failed")
        said = str(entry.get("detail") or "").strip()
        parts.append(f"{where} ({why}{': ' + said if said else ''})")
    joined = "; ".join(parts) if parts else "no attempt was recorded"
    return (f"tried {len(parts) or limit} machines and every one failed - "
            f"{joined}. This is the end of the retry policy, not a queue wait.")


def _defer_if_transient(
    store: JobStore, job_id: str, exc: BackendError, current: dict[str, Any], *,
    unknown: bool,
) -> bool:
    """Put the job back in our own queue when the fleet is merely busy.

    Returns `True` when it deferred, so the caller returns instead of failing.

    **A busy fleet is not a broken job.** Every location that offers this model just
    refused it, or could not be reached, and both of those are facts about *now*.
    The caller asked for work, not for a verdict on the fleet's occupancy. The old
    GenStudio held work this way; this gateway failed it — measured 2026-09-25:
    ninety-three of a hundred and thirty-five chat jobs failed inside half an hour,
    most of them `MACHINE_BUSY` against the single location that had the model.

    Four conditions, each load-bearing:

      * the code is transient (`_TRANSIENT_SITE_CODES`);
      * **`upstream_accepted is False`** — nothing was dispatched, so a retry cannot
        duplicate work. A request that may have reached a worker is a durable
        recovery hold and must never come through here;
      * `unknown` is false, which is the same rule stated for a job that already
        carries an upstream id;
      * we have not already waited `_MAX_DEFERRALS` times, because a job that is
        never reported is worse than one reported honestly.
    """
    defer = getattr(store, "defer", None)
    if defer is None or exc.code not in _TRANSIENT_SITE_CODES:
        return False
    if exc.upstream_accepted is not False or unknown:
        return False
    wait = concurrency.queue_wait_budget()
    attempts = int(current.get("attempt_count") or 0)
    if attempts > int(wait["max_deferrals"]):
        return False
    return bool(defer(
        job_id,
        delay_s=_defer_delay(attempts, wait["defer_cap_seconds"]),
        reason=str(exc)[:300],
    ))

log = logging.getLogger("gateway.worker")

_EXT_BY_MEDIA = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "video/mp4": "mp4",
    "application/x-subrip": "srt",
    "text/plain": "txt",
}


def _ext_for(media_type: str | None) -> str:
    return _EXT_BY_MEDIA.get((media_type or "").split(";")[0].strip(), "bin")


def _unknown_detail(detail: str) -> str:
    suffix = (
        " Upstream outcome is unknown; reconcile the provider request before retrying; "
        "the gateway will not resubmit it automatically."
    )
    return f"{detail[: max(0, 500 - len(suffix))]}{suffix}"


def _fail(
    store: JobStore,
    job_id: str,
    *,
    code: str,
    detail: str,
    retryable: bool = False,
    recovery_state: str | None = None,
    recovery_detail: str | None = None,
    recovery_context: dict[str, Any] | None = None,
) -> None:
    """Settle a real store, while keeping the small legacy test double usable."""
    try:
        store.fail(
            job_id,
            code=code,
            detail=detail,
            retryable=retryable,
            recovery_state=recovery_state,
            recovery_detail=recovery_detail,
            recovery_context=recovery_context,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        store.fail(job_id, code=code, detail=detail)


def execute_job(job: dict[str, Any], store: JobStore) -> None:
    """Run one job to a terminal state. Never raises."""
    job_id = job["id"]
    modality = job["modality"]
    provider_id = job.get("provider_id") or ""
    model_id = job.get("model_id") or ""

    current = store.get(job_id) if hasattr(store, "get") else job
    if current is None:
        return
    state = current.get("state")
    if state == "accepted":
        marker = getattr(store, "mark_running", None)
        if marker is not None and marker(job_id) is False:
            return
    elif state is not None and state != "running":
        return

    definition = registry.definition(provider_id)
    if definition is None:
        _fail(
            store,
            job_id,
            code="PROVIDER_NOT_FOUND",
            detail=f"unknown provider '{provider_id}'",
        )
        return

    # Re-check enablement at execution time. A provider can be switched off
    # between submit and run, and the catalogue and the generation path must
    # never disagree about what is available.
    if not registry.credentials.is_enabled(provider_id):
        _fail(
            store,
            job_id,
            code="PROVIDER_DISABLED",
            detail=f"provider '{provider_id}' is disabled",
        )
        return

    try:
        params = json.loads(job.get("params") or "{}")
    except json.JSONDecodeError:
        _fail(store, job_id, code="PROVIDER_FAILED", detail="unreadable job params")
        return

    # **The whole rule, not just the provider.** Provider enablement was re-checked
    # here and nothing else, so three states could change between submit and run and
    # the job would still be dispatched:
    #   * the *model* switched off — the toggle is a control, and it did nothing here;
    #   * a Hub refresh flipping `catalogued` or `memory_eligible`, which is exactly
    #     the "the model cannot serve" state `can_publish` exists to withhold;
    #   * a modality the adapter does not serve.
    # The catalogue's rule is one function — `enabled_models` — so this asks it
    # instead of re-implementing its gates. (The provider check above stays because
    # `PROVIDER_DISABLED` names the cause more precisely than "not offerable".)
    if not any(
        d.id == provider_id and m["model_id"] == model_id
        for d, m in registry.models.enabled_models(modality)
    ):
        _fail(
            store,
            job_id,
            code="MODEL_NOT_OFFERABLE",
            detail=(
                f"'{model_id}' is no longer offered by '{provider_id}' for {modality} "
                "— disabled, unconfigured, or withheld since this job was queued"
            ),
        )
        return

    # The final check sits immediately before the adapter call. A cancellation
    # that wins here is confirmed locally; once this succeeds, the upstream
    # outcome is no longer ours to assume.
    begin_dispatch = getattr(store, "begin_dispatch", None)
    if begin_dispatch is not None and not begin_dispatch(job_id):
        confirm_cancel = getattr(store, "confirm_cancel", None)
        if confirm_cancel is not None:
            confirm_cancel(job_id)
        return

    params["_job_id"] = job_id
    record_acceptance = getattr(store, "record_upstream_acceptance", None)
    # A voice is pinned to the controller that holds its reference clip, so a
    # refusal there is final. Any other job may be tried at the next location that
    # offers the same model: `LOCATION_NOT_READY` is a property of the site, not of
    # the job, and the adapter proved the Hub accepted nothing before it refused.
    pinned = fleet.pinned_location(params)
    tried: set[str] = set()
    result = None
    while True:
        definition = registry.definition(provider_id)
        if definition is None or not registry.credentials.is_enabled(provider_id):
            _fail(
                store,
                job_id,
                code="PROVIDER_DISABLED" if definition is not None else "PROVIDER_NOT_FOUND",
                detail=f"provider '{provider_id}' is disabled or unknown",
            )
            return
        request = BackendRequest(
            provider_id=provider_id,
            model_id=model_id,
            modality=modality,
            params=params,
            values=registry.credentials.resolved(provider_id),
            base_url=definition.base_url,
            display_name=definition.display_name,
            checkpoint=(
                (lambda upstream_job_id: record_acceptance(job_id, upstream_job_id))
                if record_acceptance is not None
                else None
            ),
        )
        adapter = adapter_for(definition)
        try:
            result = adapter.generate(request)
            break
        except BackendError as exc:
            log.warning("job %s failed via %s: %s", job_id, provider_id, exc)
            current = store.get(job_id) if hasattr(store, "get") else job
            current = current or {}
            # **A definite 4xx is the provider's own refusal — it accepted
            # nothing.** That is a settled failure, never a recovery hold, no
            # matter which adapter raised it. Without this a `400` such as
            # "'temperature' does not support 0.7 with this model" became a
            # permanent `unknown_outcome` hold the owner could not clear.
            # A 5xx, or a failure carrying no status at all after dispatch, stays
            # ambiguous because the request may have arrived.
            definite_refusal = isinstance(exc.status, int) and 400 <= exc.status < 500
            # **An adapter that followed its own upstream id to a terminal answer has settled the
            # question every heuristic below is guessing at, so it outranks all of them.** The id
            # itself proves only that a container was created: Studio Hub creates a batch and
            # validates the item afterwards, so a refused or failed item leaves an id behind with
            # nothing running. Without this, every ordinary Hub item failure — a validation refusal,
            # a model error, an engine out of memory — became a durable `unknown_outcome` hold
            # asking the operator to reconcile work that never ran. Measured live 2026-09-26: a 422
            # geometry refusal and a 409 "machine busy" refusal both read "reconcile the provider
            # request before retrying; the gateway will not resubmit it automatically".
            #
            # Only an adapter that polled its id to a terminal state sets this. Every path that may
            # still be running leaves it false — including a failure fetching an artifact for an
            # item that succeeded — and those keep the hold.
            if exc.upstream_settled:
                unknown = False
            else:
                unknown = bool(current.get("upstream_job_id")) or exc.upstream_accepted is True
                if not definite_refusal:
                    # Once dispatch began, an adapter that cannot prove pre-acceptance
                    # must be treated as ambiguous even when it calls the failure
                    # retryable.
                    unknown = unknown or (
                        current.get("dispatch_started_at") is not None
                        and exc.upstream_accepted is None
                    )
            if exc.code == "PROVIDER_SUBMISSION_UNCERTAIN":
                unknown = True
            detail = _unknown_detail(str(exc)) if unknown else str(exc)[:500]
            # ── A7: a **known** failure is retried on a different machine ──────────
            #
            # The owner's decision E, and the whole point of it: *"i want a 'job' that's failed
            # to retry on a different machine, be it in the same cluster or a different site.
            # just make sure no duplicates but also make sure the job is retried 3 times."*
            #
            # **Only a known failure**, and that is the load-bearing condition. An upstream that
            # accepted the work and then went silent is an unknown outcome: re-running it might
            # render and bill twice, so it becomes a reconciliation hold and is **never** retried
            # automatically. `unknown` above is exactly that question, already answered by the
            # adapter's own evidence.
            #
            # A pinned voice is excluded: its reference clip lives on one controller, so there is
            # no other machine that could serve it and a "retry" would only be a slower failure.
            if not unknown and pinned is None:
                moved = _next_machine(
                    store, job_id, model_id, modality, provider_id,
                    code=exc.code, detail=detail,
                )
                if moved is not None:
                    log.warning(
                        "job %s: %s could not run it (%s); trying %s next",
                        job_id, provider_id, exc.code, moved,
                    )
                    provider_id = moved
                    continue
            # **A busy fleet is not a broken job** — see `_defer_if_transient` for
            # the whole rule and why each of its conditions is load-bearing. With the re-route
            # above in front of it, this is now the case where there is **nowhere else to go**:
            # a site busy with its own work waits for its own machine rather than failing, which
            # is the owner's "hold, don't fail, for a temporarily absent cluster".
            if _defer_if_transient(store, job_id, exc, current, unknown=unknown):
                log.warning(
                    "job %s: %s cannot take it yet (%s); waiting, attempt %s of %s",
                    job_id, provider_id, exc.code,
                    current.get("attempt_count"), _MAX_DEFERRALS,
                )
                return
            # A durable recovery hold keeps the upstream's own identifiers, so the
            # operator has the batch, item, worker and the routes that resolve it
            # rather than a failure they have to reconstruct.
            recovery_detail = detail
            if unknown and exc.recovery:
                recovery_detail = f"{detail} {json.dumps(exc.recovery, sort_keys=True)}"
            # **A known failure that ran out of machines is declared properly.** The owner's
            # words: *"if all 3 fails. that it's declared failed properly to story studio."*
            # The last error alone knows only about itself, so the sentence is built from the
            # attempt log — every machine that was tried, and why each one failed.
            terminal_detail = detail
            if not unknown:
                attempts_of = getattr(store, "attempts_of", None)
                logged = attempts_of(job_id) if attempts_of is not None else []
                limit = int(concurrency.retry_policy()["max_attempts"])
                if logged and len(logged) >= limit:
                    terminal_detail = _tried_everything_detail(logged, limit=limit)
            _fail(
                store,
                job_id,
                code=exc.code,
                detail=terminal_detail,
                retryable=exc.retryable,
                recovery_state=RECOVERY_UNKNOWN if unknown else None,
                recovery_detail=recovery_detail if unknown else None,
                recovery_context=exc.recovery if unknown else None,
            )
            return
        except Exception as exc:  # noqa: BLE001 - a backend bug must not kill the worker
            log.exception("job %s raised unexpectedly", job_id)
            getter = getattr(store, "get", None)
            current = getter(job_id) if getter is not None else job
            current = current or {}
            detail = _unknown_detail(f"{type(exc).__name__}: {exc}")
            _fail(
                store,
                job_id,
                code="PROVIDER_FAILED",
                detail=detail,
                recovery_state=RECOVERY_UNKNOWN if current.get("dispatch_started_at") else None,
                recovery_detail=detail if current.get("dispatch_started_at") else None,
            )
            return

    # **Every artifact the provider returned is written.** A job holds a list now,
    # because a provider can answer several variations for one request (Suno sends
    # one entry per track, an image call with `n` sends one per image) and keeping
    # only the first made the rest unreachable. Artifact 0 keeps the original
    # `{job_id}.{ext}` name, so every single-artifact route and every file already
    # on disk is unchanged; the rest add an index.
    stored: list[dict[str, Any]] = []
    for index, artifact in enumerate(result.artifacts):
        stem = job_id if index == 0 else f"{job_id}-{index}"
        path = Path(ARTIFACT_DIR) / f"{stem}.{_ext_for(artifact.media_type)}"
        path.write_bytes(artifact.content)
        stored.append(
            {
                "asset_id": str(index),
                "path": str(path),
                "media_type": artifact.media_type,
                "bytes": len(artifact.content),
            }
        )
        record_artifacts = getattr(store, "record_artifacts", None)
        if record_artifacts is not None:
            record_artifacts(job_id, [stored[-1]])

    # **The delivery receipt goes out only now.** The Hub protects its output
    # receipt and index on a 2xx from its ack route, and that route is the caller
    # asserting it has verified delivery — downloading an artifact never reaches
    # it. So it is sent once every artifact is on disk, never from inside the
    # download. A refusal is recorded, not raised: the artifact is saved and
    # serveable, and discarding real work over a bookkeeping step would be worse.
    if result.deliver is not None:
        receipt = result.deliver()
        if not receipt.get("ok"):
            log.warning(
                "job %s: upstream did not acknowledge delivery: %s", job_id, receipt
            )

    store.succeed(
        job_id,
        asset_path=stored[0]["path"] if stored else None,
        media_type=stored[0]["media_type"] if stored else None,
        artifacts=stored,
        result={
            "text": result.text,
            "payload": result.payload,
        },
    )


class Worker:
    """Dispatches due jobs to bounded concurrency, respecting three ceilings.

    **One thread dispatched; several ran.** The previous shape executed each job
    on the poller itself, so exactly one job was ever in flight — correct while
    this gateway served one Mac, and the thing that makes a fleet useless when it
    serves twenty.

    The dispatcher stays single-threaded and that is deliberate: it is the only
    thing that admits work, so the budget arithmetic needs no lock of its own
    beyond the counter it already takes. Each admitted job gets its own daemon
    thread. **Daemon, not a `ThreadPoolExecutor`** — an executor's threads join at
    interpreter exit, so a quit during a two-minute render would hang the process
    instead of stopping it, and this gateway is killed and restarted often.

    Admission checks, in order, all of which must pass:

      * the pool is not full (`concurrency.global_limit`);
      * the lane is not at its cap (`concurrency.lane_budget`);
      * the provider is not at *its* limit (`concurrency.cloud_budget` for a cloud
        lane, or the site's own `available_worker_slots` for a Studio Hub one).

    The claim is still `store.mark_running`, which is a conditional UPDATE and so
    is safe against a second dispatcher. Nothing here relies on the single
    dispatcher for correctness of the claim — only for tidiness of the counters.
    """

    def __init__(self, store: JobStore, interval_s: float = 0.4,
                 pool_size: int | None = None) -> None:
        self.store = store
        self.interval_s = interval_s
        # An explicit size PINS the pool; `None` means "ask the live limit", resolved per
        # admission by `_global_limit`. This used to be captured here, which broke the
        # promise `concurrency` makes about a settings edit — see that method.
        self.pool_size = pool_size
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._budget_lock = threading.Lock()
        self._in_flight: dict[str, int] = {}
        self._lane_in_flight: dict[str, int] = {}
        #: `(provider_id, modality) -> running count`. The provider book above is per
        #: *site*; this one is per site-and-modality, which is the shape the Hub's own
        #: free-slot count is published in and therefore the only one that can be
        #: compared with it.
        self._per_lane: dict[tuple[str, str], int] = {}
        #: The last budget resolved per provider, so the console can say what is
        #: bounding a lane and where the number came from rather than leaving a
        #: reader to guess why their fleet is idle.
        self.budgets: dict[str, dict[str, Any]] = {}
        self.processed = 0
        #: Liveness of the dispatch loop itself (M6). `last_pass_at` is stamped at the end of
        #: every pass, clean or not, and `pass_failures` counts consecutive passes that raised.
        #: A dead dispatcher used to be indistinguishable from an idle one: `/health` said ok
        #: and every later job simply sat in `accepted`.
        self.last_pass_at: float | None = None
        self.pass_failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gateway-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    # -- admission ---------------------------------------------------------
    def _site_lane_in_flight(self, provider_id: str, modality: str) -> int:
        """This gateway's own running jobs at **one site in one modality**.

        The provider counter was the only one admission had, and it is per *site*: a site
        running one image job refused a chat job while a second, free Mac sat there. A
        Mac runs one job at a time and the Hub's free count is per operation, so the two
        numbers that are compared have to be about the same thing.
        """
        with self._budget_lock:
            return self._per_lane.get((provider_id, modality), 0)

    def _provider_budget(self, provider_id: str, modality: str) -> concurrency.Budget:
        """What bounds one provider, from the best source that has an answer.

        A Studio Hub location is bounded by **its own published capacity** — the free
        physical machines it reports for this operation, plus this gateway's own in-flight
        jobs at that operation, clamped to the site's eligible total — and by the
        operator's number, which `hub_budget` takes the smaller of. See there for the
        arithmetic and why adding the in-flight count back is the fix rather than a
        looseness: the Hub's free count is *already* net of the Macs this gateway is using.

        The reading is `hub_slot_reading`, which prefers `available_physical_machine_slots`
        over `available_worker_slots`. Routing already ranks by the physical count; reading
        the service count here is how a one-Mac site was told it had two transcription
        slots and then answered `MACHINE_BUSY`.
        """
        operation = _HUB_OPERATION_BY_MODALITY.get(modality, "")
        ours = self._site_lane_in_flight(provider_id, modality)
        try:
            from . import fleet

            for row in fleet.snapshot(allow_stale=True).get("locations") or []:
                if str(row.get("provider_id") or "") != provider_id:
                    continue
                reading = concurrency.hub_slot_reading(row, operation)
                free = reading.get("free")
                if free is None and not reading.get("unstated"):
                    free = reading.get("ceiling")
                return concurrency.hub_budget(
                    free,
                    operation=operation or modality,
                    stated=concurrency.stated_provider_limit(provider_id),
                    in_flight=ours,
                    ceiling=reading.get("ceiling"),
                    reachable=row.get("reachable") is not False,
                )
        except Exception:  # noqa: BLE001 - an unreadable fleet is not a reason to stall
            pass
        return concurrency.cloud_budget(provider_id)

    def active(self) -> int:
        with self._budget_lock:
            return sum(self._in_flight.values())

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is in flight; `False` if it did not settle in time.

        Needed because `_loop` now returns as soon as it has *dispatched*. A caller
        that requires the work to be finished has to say so — a test asserting "the
        loop returned, therefore the job ran" silently became a race when the pool
        landed, and that is the kind of gate people learn to re-run.

        Deliberately **not** folded into `stop()`: stopping the poller must stay
        fast, because the app quits by stopping this process and a two-minute
        render must not hold the quit open.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.active() == 0:
                return True
            time.sleep(0.02)
        return self.active() == 0

    def _global_limit(self) -> int:
        """The live global admission cap.

        An explicit ``pool_size`` pins it — the tests use that, and so would any caller
        that means to hold a fixed size. With no pin, this is read AT ADMISSION rather
        than captured in ``__init__``, because ``global_budget()`` re-reads the settings
        file whenever it changes and ``concurrency`` states the consequence in as many
        words: *"a fix to a rate limit should take effect without a restart."*

        That promise held for lanes and providers, which were always read per admission,
        and **not for this one**: the value was frozen when the worker started, so raising
        `global` on the Providers page changed what the API reported and nothing about how
        many jobs actually ran. The class docstring has named `concurrency.global_limit`
        as this check since it was written; now the code does too.
        """
        return self.pool_size if self.pool_size else concurrency.global_limit()

    def _admit(self, job: dict[str, Any]) -> bool:
        """Reserve room for one job, or refuse it this pass.

        Refusing is not failing: the job stays due and the next pass tries again,
        which is what keeps a busy provider from turning into a crash.

        The provider check reads `self._per_lane` — this site's jobs **in this
        modality** — because that is what the Hub's free-slot count describes. The
        site-wide book is still kept, and still bounds the pool as a whole.
        """
        provider_id = str(job.get("provider_id") or "")
        modality = str(job.get("modality") or "")
        provider_budget = self._provider_budget(provider_id, modality)
        lane_budget = concurrency.lane_budget(modality)
        with self._budget_lock:
            if sum(self._in_flight.values()) >= self._global_limit():
                return False
            if self._lane_in_flight.get(modality, 0) >= lane_budget.limit:
                return False
            if self._per_lane.get((provider_id, modality), 0) >= provider_budget.limit:
                return False
            self._in_flight[provider_id] = self._in_flight.get(provider_id, 0) + 1
            self._lane_in_flight[modality] = self._lane_in_flight.get(modality, 0) + 1
            key = (provider_id, modality)
            self._per_lane[key] = self._per_lane.get(key, 0) + 1
            self.budgets[provider_id] = provider_budget.as_dict()
        return True

    def _release(self, job: dict[str, Any]) -> None:
        provider_id = str(job.get("provider_id") or "")
        modality = str(job.get("modality") or "")
        with self._budget_lock:
            for book, key in ((self._in_flight, provider_id),
                              (self._lane_in_flight, modality),
                              (self._per_lane, (provider_id, modality))):
                if book.get(key):
                    book[key] -= 1
                if book.get(key, 0) <= 0:
                    book.pop(key, None)

    # -- running -----------------------------------------------------------
    def _run(self, job: dict[str, Any]) -> None:
        """One job on its own thread. Never raises."""
        try:
            execute_job(job, self.store)
            self.processed += 1
        except Exception:  # noqa: BLE001
            log.exception("worker failed to execute %s", job.get("id"))
            getter = getattr(self.store, "get", None)
            current = getter(job["id"]) if getter is not None else job
            current = current or {}
            unknown = bool(
                current.get("dispatch_started_at") or current.get("upstream_job_id")
            )
            detail = (
                _unknown_detail("internal worker error")
                if unknown
                else "internal worker error"
            )
            _fail(
                self.store,
                job["id"],
                code="PROVIDER_FAILED",
                detail=detail,
                recovery_state=RECOVERY_UNKNOWN if unknown else None,
                recovery_detail=detail if unknown else None,
            )
        finally:
            self._release(job)

    def _loop(self) -> None:
        while not self._stop.is_set():
            # **One pass, and one raise is not allowed to end dispatch for ever.** The loop
            # guarded `due()` and nothing else: a raise from `_admit`, `mark_running` or the
            # thread spawn escaped the `while`, the thread died, and every later job sat in
            # `accepted` for ever while `/health` still said ok — there was no liveness field to
            # say otherwise (audit M6). The pass is wrapped whole and the count of consecutive
            # failures is published, so an operator can tell "idle" from "wedged".
            try:
                due = self.store.due()
            except Exception:  # noqa: BLE001
                log.exception("worker could not read due jobs")
                due = []

            try:
                for job in due:
                    if self._stop.is_set():
                        return
                    # Reserve first, then claim: a claim that fails must not leave the
                    # budget holding a slot nobody will ever release.
                    if not self._admit(job):
                        continue
                    if not self.store.mark_running(job["id"]):
                        self._release(job)
                        continue
                    job["state"] = "running"
                    threading.Thread(
                        target=self._run, args=(job,),
                        name=f"gateway-job-{job['id'][:8]}", daemon=True,
                    ).start()
                self.pass_failures = 0
            except Exception:  # noqa: BLE001 - the next pass must still happen
                self.pass_failures += 1
                log.exception("worker dispatch pass failed (consecutive: %s)",
                              self.pass_failures)
            finally:
                # **When the last pass finished, successful or not.** `worker_alive` alone is a
                # boolean an operator cannot date; this is the number that says whether dispatch
                # is moving. Written every pass, so a stalled loop shows a stale timestamp rather
                # than a green light.
                self.last_pass_at = time.time()

            self._stop.wait(self.interval_s)

    def liveness(self) -> dict[str, Any]:
        """Whether dispatch is actually moving, in the three fields `/health` publishes.

        `alive` is the thread's own state, `last_pass_at` dates the last attempt, and
        `consecutive_failures` counts passes that raised since the last clean one. A boolean
        alone is what let a dead dispatcher read as healthy.
        """
        thread = self._thread
        return {
            "worker_alive": bool(thread is not None and thread.is_alive()),
            "last_pass_at": self.last_pass_at,
            "consecutive_failures": self.pass_failures,
            "in_flight": self.active(),
        }
