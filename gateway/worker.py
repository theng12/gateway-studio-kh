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

from . import concurrency, pools
from .adapters import BackendError, BackendRequest, adapter_for
from .adapters import studio_hub
from .adapters.base import requests_started
from .config import ARTIFACT_DIR
from .providers import registry
from .providers.definitions import KIND_STUDIO_HUB
from .store import NEVER_SENT_SUFFIX, RECOVERY_UNKNOWN, JobStore
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
#: All prove the upstream accepted nothing, which is what makes a retry safe;
#: `_defer_if_transient` still requires `upstream_accepted is False`, so a
#: `SITE_CAPACITY_UNAVAILABLE` that may have left the building (a submit that timed out) is
#: an unknown outcome and gets a recovery hold - or, for a free Studio Hub job, a replay.
_TRANSIENT_SITE_CODES = {"LOCATION_NOT_READY", "LOCATION_UNREACHABLE", "SITE_CAPACITY_UNAVAILABLE"}

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


#: A job that ran past its own time limit is not retried on another Mac: the limit is the lane's
#: ceiling for this work, so another machine would only spend the same time again. It is stopped on
#: the Hub and reported.
_NO_REROUTE_CODES = {"DEADLINE_EXCEEDED"}

#: A shop's workstation Mac stopped the job (macOS critical memory pressure for 20 s, rare) and its
#: controller had nowhere else for it (Hub workstation mode, owner request 2026-10-03). Free work that
#: never finished: it moves at once to the next location, and **does not count against the retry
#: ceiling** - the Mac was taken back by its owner's apps, the job did not fail. The exclusion list still grows with every yield, so it cannot bounce for ever.
WORKSTATION_YIELDED = "WORKSTATION_YIELDED"

#: A free Studio Hub job its Mac handed back: a workstation yield, or three memory refusals. Nothing
#: ran to completion. When no other location can take it *right now* the job is not failed with "run
#: it again": it waits for a free Mac on the same queue-wait ladder as any busy fleet, and goes to
#: whichever location can take it first - the one that handed it back included (owner request 2026-10-03).
_HANDBACK_CODES = frozenset({WORKSTATION_YIELDED, "INSUFFICIENT_MEMORY"})


def _client_request_id(store: Any, job_id: str, provider_id: str) -> str:
    """What the Hub dedupes a submit on. The job id - until that location has handed the job back.

    The Hub keeps a client request id for good and answers a repeat with the batch it already made,
    so after a hand-back the same id would replay the finished, errored batch even though the Mac is
    free again. Each hand-back at a location therefore gets the next suffix; a submit that merely
    timed out keeps its id, which is exactly what its replay relies on.
    """
    attempts_of = getattr(store, "attempts_of", None)
    if attempts_of is None:
        return job_id
    here = fleet.controller_id_from(provider_id) or provider_id
    handed_back = sum(
        1 for entry in attempts_of(job_id)
        if entry.get("code") in _HANDBACK_CODES
        and (fleet.controller_id_from(str(entry.get("location") or "")) or entry.get("location")) == here
    )
    return f"{job_id}~{handed_back}" if handed_back else job_id


def _defer_delay(attempts: int, cap_s: float | None = None) -> float:
    """2s, 4s, 8s … capped. Long enough not to hammer a busy machine."""
    return min(2.0 ** max(int(attempts), 0), _DEFER_CAP_S if cap_s is None else float(cap_s))


#: How often the hold reconciliation asks the Hub about its outstanding holds.
_HOLD_SWEEP_S = 60.0


def reconcile_holds(store: JobStore, *, now: float | None = None) -> dict[str, int]:
    """Close the holds the **Hub's own record** has already settled (Q8).

    The owner's answer: *"Yes — auto-close when the Hub's own batch record says finished or never
    started."* Both are one request away, and neither is a guess:

      * **finished** — the Hub reports the item `error`/`cancelled`, so nothing is running and the
        hold has nothing left to protect. The job is closed as failed with the Hub's own state
        recorded. An item the Hub reports `done` is **adopted**: its artifact is fetched, verified,
        saved and the job settled `succeeded` through the normal done path.
      * **never started** — the Hub has never heard of the batch id (`404`). The work it was
        supposed to be doing does not exist, which is the same fact the restart sweep acts on when
        `dispatch_started_at` is null: provably nothing to duplicate, so the job goes back in the
        queue if it still has attempts left, and is failed cleanly if it does not.
      * **still running, or the Hub did not answer** — left exactly where it is. "I could not ask"
        is not "it is finished", and this function never treats the two the same.

    Runs on the dispatch loop's own thread, on a timer, and never raises: a hold that cannot be
    reconciled this minute is reconciled the next.
    """
    out = {"checked": 0, "closed_finished": 0, "requeued": 0, "adopted": 0, "still_open": 0}
    holds_of = getattr(store, "holds", None)
    close = getattr(store, "close_hold", None)
    if holds_of is None or close is None:
        return out
    try:
        rows = holds_of(limit=50, sweepable=True)
    except Exception:  # noqa: BLE001 - an unreadable table is not a reason to stop dispatching
        log.exception("could not read recovery holds")
        return out
    for job in rows:
        provider_id = str(job.get("provider_id") or "")
        definition = registry.definition(provider_id)
        context = job.get("recovery_context")
        context = context if isinstance(context, dict) else {}
        batch_id = str(context.get("batch_id") or job.get("upstream_job_id") or "")
        if definition is None or not batch_id:
            out["still_open"] += 1
            continue
        # Only Studio Hub can be asked: it is the lane that keeps a batch record and publishes a
        # route for it. Every other lane stays on the operator's discard action.
        if str(getattr(definition, "kind", "")) != "studio_hub":
            out["still_open"] += 1
            continue
        out["checked"] += 1
        values = registry.credentials.resolved(provider_id)
        snapshot = studio_hub.batch_snapshot(
            values, str(values.get("base_url") or definition.base_url or ""), batch_id,
            kind=str(job.get("modality") or "image"),
            local=provider_id == fleet.provider_id_for(fleet.LOCAL_ID),
        )
        if snapshot is None:
            out["still_open"] += 1
            continue
        if not snapshot.get("known"):
            limit = int(concurrency.retry_policy()["max_attempts"])
            attempted = len(store.attempts_of(job["id"])) if hasattr(store, "attempts_of") else 0
            if attempted < limit:
                close(job["id"], resolution="requeued",
                      detail=(f"the Hub has never heard of batch {batch_id} ({provider_id}), so "
                              f"nothing was ever run for it. Returned to the queue — this is "
                              f"proof of no work, not a guess."))
                out["requeued"] += 1
            else:
                close(job["id"], resolution="failed",
                      detail=(f"the Hub has never heard of batch {batch_id} ({provider_id}) and "
                              f"the job has used all {limit} attempts, so it is failed rather "
                              f"than queued again."))
                out["closed_finished"] += 1
            continue
        state = str(snapshot.get("state") or "")
        if state in _DONE_UPSTREAM_STATES:
            # **The Hub finished it and still holds the artifact: adopt it** (owner decision,
            # 2026-09-30) -- fetch, verify the sha256, save it and settle the job `succeeded`
            # through the normal done path. Anything that stops that (the bytes will not verify,
            # the Hub stops answering) leaves the hold exactly as it was, to be tried next minute.
            request = BackendRequest(
                provider_id=provider_id, model_id=str(job.get("model_id") or ""),
                modality=str(job.get("modality") or "image"), params={}, values=values,
                base_url=str(values.get("base_url") or definition.base_url or ""),
                display_name=getattr(definition, "display_name", ""),
            )
            try:
                settle_result(job["id"], studio_hub.adopt_batch(
                    request, batch_id,
                    local=provider_id == fleet.provider_id_for(fleet.LOCAL_ID)), store)
            except Exception as exc:  # noqa: BLE001 - a hold that cannot be adopted stays a hold
                log.warning("hold %s: could not adopt finished batch %s (%s): %s",
                            job["id"], batch_id, provider_id, exc)
            else:
                out["adopted"] += 1
                continue
        elif state in _TERMINAL_UPSTREAM_STATES:
            item = snapshot.get("item") or {}
            detail = (f"the Hub reports batch {batch_id} ({provider_id}) finished with state "
                      f"'{state}'"
                      + (f": {item.get('error')}" if item.get("error") else "")
                      + ". Closed automatically (Q8): the upstream's own record settled it.")
            close(job["id"], resolution="failed", detail=detail)
            out["closed_finished"] += 1
            continue
        out["still_open"] += 1
    return out


#: The Hub finished it and there is an artifact: not a failure, so not closed as one.
_DONE_UPSTREAM_STATES = frozenset({"succeeded", "success", "done", "complete", "completed"})
#: The Hub's terminal words, as `studio_hub` already names them.
_TERMINAL_UPSTREAM_STATES = frozenset(
    {"failed", "error", "cancelled", "canceled", "refused", "expired"}
)

def scarcest_first(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`store.due()` re-ordered so the model with the fewest machines goes first.

    Key: the model's fleet-wide pool (`fleet.pool_size`: eligible slots summed over every
    reachable location, from one snapshot), then arrival order. A pass hands out slots in
    this order, so work only a few Macs can run is not queued behind work any Mac can.
    `sorted` is stable and `due()` is already oldest-first, so ties stay FIFO. Cloud jobs
    take no Mac and sort first; a model nothing can run right now sorts last. One
    `pool_size` per distinct model per pass.
    """
    pools: dict[tuple[str, str], float] = {}

    def pool(job: dict[str, Any]) -> float:
        if not str(job.get("provider_id") or "").startswith(fleet.PROVIDER_PREFIX):
            return 0
        key = (str(job.get("model_id") or ""), str(job.get("modality") or ""))
        if key not in pools:
            pools[key] = fleet.pool_size(*key) or float("inf")
        return pools[key]

    try:
        return sorted(jobs, key=pool)
    except Exception:  # noqa: BLE001 - ordering is a preference; never the reason nothing runs
        log.exception("could not order due jobs by scarcity")
        return jobs


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
    # Only this round: a job that waited for a free Mac (`defer(new_round=True)`) starts over.
    attempts = [entry for entry in attempts if not entry.get("waited")]
    tried = {_location(entry.get("location")) for entry in attempts}
    tried.add(_location(provider_id))
    tried.discard("")
    if sum(entry.get("code") != WORKSTATION_YIELDED for entry in attempts) >= int(policy["max_attempts"]):
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
    unknown: bool, replay: bool = False, handback: bool = False,
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
    if defer is None:
        return False
    # `replay`: a free Studio Hub submit that timed out. It may have been accepted, but the Hub
    # answers the same client request id with the batch it already made, so the retry at this same
    # location cannot run twice - which is the whole reason the two conditions below exist.
    # `handback`: a free Studio Hub job its Mac handed back with no other location free (`_HANDBACK_CODES`);
    # the Hub settled the item itself, so it is as safe to queue as a busy site.
    if not (replay or handback) and (exc.code not in _TRANSIENT_SITE_CODES
                                     or exc.upstream_accepted is not False or unknown):
        return False
    wait = concurrency.queue_wait_budget()
    attempts = int(current.get("attempt_count") or 0)
    if attempts > int(wait["max_deferrals"]):
        return False
    if handback:
        return bool(defer(
            job_id,
            delay_s=_defer_delay(attempts, wait["defer_cap_seconds"]),
            reason=f"waiting for a free Mac - {exc}"[:300],
            new_round=True,
        ))
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
    # What a Hub lane returns for `output_format: m4a` (AAC in MP4) and for FLAC. Without these an
    # AAC artifact is stored as `{job}.bin`: it still serves with the right type, but every tool that
    # trusts the extension (Finder, an ffmpeg muxer guess, the admin download) would mislabel it.
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "video/mp4": "mp4",
    "application/x-subrip": "srt",
    "text/plain": "txt",
}


def _ext_for(media_type: str | None) -> str:
    return _EXT_BY_MEDIA.get((media_type or "").split(";")[0].strip(), "bin")


def _params_for(definition: Any, params: dict[str, Any]) -> dict[str, Any]:
    """The job's params as one provider should see them.

    `output_format` is a request to a Studio Hub lane (`wav`/`m4a`, `png`/`jpeg`) and means nothing to
    a cloud provider: ElevenLabs would put `m4a` into its own `?output_format=` query and refuse a
    paid job, and a fal model that does not declare the field may reject it. Cloud lanes return their
    native format, so they never see it; the stored job still records what was asked.
    """
    if "output_format" not in params or getattr(definition, "kind", None) == KIND_STUDIO_HUB:
        return params
    return {key: value for key, value in params.items() if key != "output_format"}


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


def cancel_upstream(job: dict[str, Any], store: JobStore, *, record: bool = True) -> dict[str, Any] | None:
    """Ask whoever owns dispatched work to stop it, and record the answer.

    A cancellation after dispatch is a *request*: a worker that has accepted the
    job is not ours to stop. So this asks the lane that took the work — for a
    Studio Hub location, its own DELETE route — and stores exactly what came back
    on the job's `cancel_remote`. Nothing may report a remote stop the owner of
    the work did not confirm, and an unconfirmed answer stays visible as
    `ok: false` beside the reason.

    A job with no upstream id never left the gateway; `store.cancel` already
    settled that case locally, and there is nothing to ask.

    `record=False` asks without writing the answer onto the job: the abandoned attempt of a job
    that was moved to another location is not the job's own cancel.
    """
    batch_id = str(job.get("upstream_job_id") or "").strip()
    if not batch_id:
        return None
    provider_id = str(job.get("provider_id") or "")
    definition = registry.definition(provider_id)
    if definition is None:
        return None
    adapter = adapter_for(definition)
    cancel = getattr(adapter, "cancel", None)
    if cancel is None:
        answer: dict[str, Any] = {
            "ok": False,
            "status": None,
            "reason": f"the {definition.kind} lane cannot cancel dispatched work",
        }
    else:
        request = BackendRequest(
            provider_id=provider_id,
            model_id=str(job.get("model_id") or ""),
            modality=str(job.get("modality") or ""),
            params={},
            values=registry.credentials.resolved(provider_id),
            base_url=definition.base_url,
            display_name=definition.display_name,
            upstream_job_id=batch_id,
        )
        try:
            answer = cancel(request)
        except Exception as exc:  # noqa: BLE001 - a cancel must never 500 the caller
            answer = {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(answer, dict):
            answer = {
                "ok": False,
                "status": None,
                "error": f"unreadable cancel answer: {answer!r}",
            }
    if record:
        store.record_cancel_remote(job["id"], answer)
    return answer


def settle_result(job_id: str, result: Any, store: JobStore) -> None:
    """Save what an adapter returned, send the delivery receipt, and settle the job `succeeded`.

    The one done path: a job that ran to the end and a **held job the Hub has since finished**
    (`adopt_hold`) both come through here, so an adopted result is a normal one -- same artifact
    names, same receipt-after-save ordering, same published URLs.
    """
    # **Every artifact the provider returned is written.** A job holds a list now,
    # because a provider can answer several variations for one request (Suno sends
    # one entry per track, an image call with `n` sends one per image) and keeping
    # only the first made the rest unreachable. Artifact 0 keeps the original
    # `{job_id}.{ext}` name, so every single-artifact route and every file already
    # on disk is unchanged; the rest add an index.
    # **Room first, so a full disk is a sentence and not a half-written file.** Nothing has been
    # acknowledged to the Hub yet (the receipt goes out after the writes), so the Hub still
    # holds the output while the job is failed retryable.
    from . import diskguard

    # **First result wins.** A job moved off a silent location can hear from the abandoned attempt
    # later; once one result has settled the job, a second never rewrites its files or its row.
    row = store.get(job_id) if hasattr(store, "get") else None
    if row and row.get("state") == "succeeded":
        log.warning("job %s: ignoring a late result, the job already succeeded", job_id)
        return
    try:
        diskguard.require_room(sum(len(a.content or b"") for a in result.artifacts))
    except diskguard.DiskLow as low:
        _fail(store, job_id, code="DISK_LOW", detail=low.sentence, retryable=True)
        return
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


def execute_job(job: dict[str, Any], store: JobStore, *, resume: bool = False) -> None:
    """Run one job to a terminal state. Never raises.

    `resume` follows a job the upstream already accepted before a gateway restart: the adapter's
    `resume` polls its `upstream_job_id` instead of submitting it again, and everything after the
    adapter call (holds, re-routes, the done path) is the same as for a live job.
    """
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

    try:
        params = json.loads(job.get("params") or "{}")
    except json.JSONDecodeError:
        _fail(store, job_id, code="PROVIDER_FAILED", detail="unreadable job params")
        return
    if resume:
        # Already dispatched: the enablement gates and the dispatch window below are for work not
        # yet sent. What was sent is followed to its end whatever was toggled since.
        _dispatch(job, store, provider_id, model_id, modality, params,
                  str(current.get("upstream_job_id") or ""))
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
    _dispatch(job, store, provider_id, model_id, modality, params, "")


def _dispatch(job: dict[str, Any], store: JobStore, provider_id: str, model_id: str,
              modality: str, params: dict[str, Any], resume_id: str) -> None:
    """Call the adapter and settle what it answers. `resume_id` polls that upstream id first."""
    job_id = job["id"]
    current = (store.get(job_id) if hasattr(store, "get") else job) or job
    params["_job_id"] = job_id
    record_acceptance = getattr(store, "record_upstream_acceptance", None)

    record_waiting = getattr(store, "record_waiting", None)

    def on_wait(waiting: dict[str, Any] | None) -> None:
        if record_waiting is not None:
            record_waiting(job_id, waiting)

    def on_accepted(upstream_job_id: str) -> None:
        record_acceptance(job_id, upstream_job_id)
        # A cancel that arrived before the Hub's batch id existed had nothing to ask (the API read
        # the row before there was an id), so the batch would run on to `succeeded`. Ask now.
        row = store.get(job_id) or {}
        if row.get("cancel_requested_at") is not None:
            cancel_upstream(row, store)
    # A voice whose clip only one Hub holds is pinned there, so a refusal there is
    # final (a clone the gateway holds is staged wherever the job runs and pins nothing). Any other job may be tried at the next location that
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
        params["_client_request_id"] = _client_request_id(store, job_id, provider_id)
        request = BackendRequest(
            provider_id=provider_id,
            model_id=model_id,
            modality=modality,
            params=_params_for(definition, params),
            values=registry.credentials.resolved(provider_id),
            base_url=definition.base_url,
            display_name=definition.display_name,
            checkpoint=on_accepted if record_acceptance is not None else None,
            on_wait=on_wait,
            upstream_job_id=resume_id,
            dispatch_started_at=(current.get("dispatch_started_at") if resume_id else None),
        )
        adapter = adapter_for(definition)
        if job.get("waiting_json") or current.get("waiting_json"):
            on_wait(None)  # a fresh attempt is not waiting for anything yet
        sends_before = requests_started()
        try:
            result = adapter.resume(request) if resume_id else adapter.generate(request)
            break
        except BackendError as exc:
            log.warning("job %s failed via %s: %s", job_id, provider_id, exc)
            current = store.get(job_id) if hasattr(store, "get") else job
            current = current or {}
            # **Nothing was sent, so nothing can be outstanding.** `dispatch_started_at` is stamped
            # before the adapter runs, so on its own it cannot tell "refused before any request"
            # (a validation error, a pool with nothing to try) from "sent and went silent".
            # (A resumed job was sent before the restart, whatever this call managed to send.)
            never_sent = requests_started() == sends_before and not resume_id
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
            if exc.upstream_settled or never_sent:
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
            # **The owner cancelled this job, so a failure now is the cancel arriving, not a
            # reason to try another machine.** The Hub answers a cancelled item with an error, the
            # loop below would treat that as a known failure and re-submit the job elsewhere, and
            # nothing ever turned the Hub's confirmed cancel into `cancelled`. An unknown outcome
            # keeps its hold: that is unresolved work, whatever was asked.
            if (not unknown and current.get("cancel_requested_at") is not None
                    and store.settle_cancelled(job_id)):
                return
            detail = _unknown_detail(str(exc)) if unknown else str(exc)[:500]
            # ── A free Studio Hub submit that timed out is replayed, not failed ─────────────────
            #
            # *"hub submit failed: timed out"* ended Dreamlands narration chapters `failed /
            # SITE_CAPACITY_UNAVAILABLE`: the answer never came, so the outcome was unknown and the
            # job was held. For the owner's own Mac that is the wrong trade: the Hub answers a
            # repeated submit carrying the same client request id (the job id) with the batch it
            # already made, so the job goes back in the queue - Story Studio reads it as waiting -
            # and is sent again to **the same location**, finding its accepted batch rather than
            # starting a second one. Not to another location: that one has no way to know. Once the
            # queue-wait budget is spent the location counts as silent (below).
            kind_is_hub = getattr(definition, "kind", "") == "studio_hub"
            if (kind_is_hub and not resume_id and (exc.recovery or {}).get("replayable")
                    and current.get("cancel_requested_at") is None
                    and _defer_if_transient(store, job_id, exc, current, unknown=False, replay=True)):
                log.warning("job %s: %s did not answer its submit (%s); replaying it, attempt %s of %s",
                            job_id, provider_id, exc, current.get("attempt_count"), _MAX_DEFERRALS)
                return
            # ── A free location that went silent mid-job is moved, not held ─────────────────────
            #
            # Owner decision 2026-10-03. The unknown-outcome hold exists so a *paid* provider is
            # never billed twice. A Studio Hub location is the owner's own Mac: re-running costs
            # nothing, while holding a render for a Mac that has died strands it for good. So when
            # a Studio Hub location stayed silent past the adapter's grace (`lost_contact`), the
            # job goes to the next location offering the model (`_next_machine`: owner pauses,
            # tried locations and the retry ceiling all apply) and the abandoned attempt is
            # cancelled best effort. Where that cannot happen - a pinned voice, no other location,
            # attempts used up, or an owner cancel in flight - it stays the hold it always was.
            # A resumed job whose Hub answers never gets here; a `resume` 404 and a Hub-reported
            # `uncertain` item carry no `lost_contact`, so they stay holds too.
            if (unknown and pinned is None and kind_is_hub
                    and (exc.recovery or {}).get("lost_contact")
                    and current.get("cancel_requested_at") is None):
                abandoned = dict(current, provider_id=provider_id)
                moved = _next_machine(
                    store, job_id, model_id, modality, provider_id,
                    code=exc.code, detail=str(exc)[:500],
                )
                if moved is not None:
                    log.warning("job %s: %s went silent mid-job; moving it to %s and cancelling "
                                "the abandoned attempt", job_id, provider_id, moved)
                    threading.Thread(target=cancel_upstream, args=(abandoned, store),
                                     kwargs={"record": False}, daemon=True,
                                     name=f"gateway-cancel-{job_id[:8]}").start()
                    provider_id = moved
                    resume_id = ""
                    continue
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
            # A workstation yield is free Studio Hub work only: no paid lane is ever moved on it.
            if (not unknown and pinned is None and exc.code not in _NO_REROUTE_CODES
                    and (exc.code != WORKSTATION_YIELDED or kind_is_hub)):
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
                    resume_id = ""  # a fresh submission at the next location
                    continue
            # ── A free Mac handed the job back and no other location can take it now: wait ───────
            #
            # A workstation yield or a low-memory hand-back is a fact about *now*, so the job is queued
            # in a plain "waiting for a free Mac" state on the queue-wait ladder (the same budget and
            # backoff a busy fleet gets), not failed with "run it again". A late rebind sends it to any
            # location with room; the Mac that handed it back is eligible again once it has a slot. A job
            # pinned to this Mac (owner, 2026-10-04) does not wait: it fails at once with the plain
            # "Nothing was charged" sentence below. Paid lanes never come through here.
            if (kind_is_hub and pinned is None and not unknown and exc.code in _HANDBACK_CODES
                    and current.get("cancel_requested_at") is None):
                if _defer_if_transient(store, job_id, exc, current, unknown=False, handback=True):
                    log.warning("job %s: %s handed it back (%s) and no other location is free; waiting "
                                "for a free Mac, attempt %s of %s", job_id, provider_id, exc.code,
                                current.get("attempt_count"), _MAX_DEFERRALS)
                    return
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
                elif exc.code == WORKSTATION_YIELDED:
                    terminal_detail = detail.rstrip(". ") + ". " + (
                        "This voice exists only on that location, so it was not moved to another Mac."
                        if pinned is not None else "No other location offering this model could take it."
                    ) + " Nothing was charged; run it again."
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
            if requests_started() == sends_before and not resume_id:
                # **A gateway bug before any request is a plain failure, not an unknown outcome.**
                # Nothing reached a provider, so there is nothing to reconcile and a retry cannot
                # duplicate work (the 0.2.97 pool `AttributeError` is the case that taught this).
                _fail(
                    store,
                    job_id,
                    code="PROVIDER_FAILED",
                    detail=(f"{type(exc).__name__}: {exc}"[:380] + NEVER_SENT_SUFFIX),
                    retryable=True,
                )
                return
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

    settle_result(job_id, result, store)


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
        self._budget_lock = threading.RLock()  # re-entered by `_book` from `_admit`
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
        #: When the hold reconciliation last ran, and what it found (Q8).
        self._last_hold_sweep = 0.0
        self._sweeping = threading.Event()
        #: `(model, modality)` shapes for which a late rebind found no site with room this
        #: pass, so the rest of the queue behind them does not repeat the search. Cleared each pass.
        self._nowhere: set[tuple[str, str]] = set()
        self.hold_sweep: dict[str, int] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gateway-worker", daemon=True)
        self._thread.start()
        self._resume_interrupted()

    def _resume_interrupted(self) -> None:
        """Follow every job a restart interrupted to its end, by the upstream id it was given.

        The store left them `running` (`JobStore.interrupted`). A lane whose adapter can poll by
        id (`resume`) is polled on its own thread exactly as before the restart, counted against
        the budgets like any running job but never refused by them — the work is already running
        upstream. Any other lane gets the restart hold, as every lane used to.
        """
        ids = list(getattr(self.store, "interrupted", None) or [])
        if ids:
            self.store.interrupted = []
        for job_id in ids:
            try:
                job = self.store.get(job_id)
                if not job or job.get("state") != "running":
                    continue
                definition = registry.definition(str(job.get("provider_id") or ""))
                if definition is None or getattr(adapter_for(definition), "resume", None) is None:
                    self.store.hold_interrupted(job_id)
                    continue
                self._book(job)
                log.warning("job %s: resuming upstream %s on %s after a restart",
                            job_id, job.get("upstream_job_id"), job.get("provider_id"))
                threading.Thread(
                    target=self._run, args=(job,), kwargs={"resume": True},
                    name=f"gateway-job-{job_id[:8]}", daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 - one unreadable row must not strand the others
                log.exception("could not resume %s; holding it", job_id)
                try:
                    self.store.hold_interrupted(job_id)
                except Exception:  # noqa: BLE001
                    log.exception("could not hold %s either", job_id)

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

    def _provider_budget(self, provider_id: str, modality: str, model_id: str = "") -> concurrency.Budget:
        """What bounds one provider, from the best source that has an answer.

        A Studio Hub location is bounded by **its own published eligible total** for this
        operation (queue ahead: the Hub's durable queue holds what waits for a Mac) and by
        the operator's number, which `hub_budget` takes the smaller of.

        The reading is `hub_slot_reading`, which prefers `available_physical_machine_slots`
        over `available_worker_slots`. Routing already ranks by the physical count; reading
        the service count here is how a one-Mac site was told it had two transcription
        slots and then answered `MACHINE_BUSY`.
        """
        if provider_id == pools.POOL_PROVIDER_ID:
            return concurrency.pool_budget(pools.members(model_id))
        operation = _HUB_OPERATION_BY_MODALITY.get(modality, "")
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
        if fleet.is_paused_provider(provider_id):
            return False  # the owner paused this location: no new work; `_rebind` moves the job on
        provider_budget = self._provider_budget(provider_id, modality, str(job.get("model_id") or ""))
        lane_budget = concurrency.lane_budget(modality)
        with self._budget_lock:
            if sum(self._in_flight.values()) >= self._global_limit():
                return False
            if self._lane_in_flight.get(modality, 0) >= lane_budget.limit:
                return False
            if self._per_lane.get((provider_id, modality), 0) >= provider_budget.limit:
                return False
            self._book(job)
            self.budgets[provider_id] = provider_budget.as_dict()
        return True

    def _book(self, job: dict[str, Any]) -> None:
        """Count one running job in all three books (`_release` is the inverse)."""
        provider_id = str(job.get("provider_id") or "")
        modality = str(job.get("modality") or "")
        with self._budget_lock:
            self._in_flight[provider_id] = self._in_flight.get(provider_id, 0) + 1
            self._lane_in_flight[modality] = self._lane_in_flight.get(modality, 0) + 1
            key = (provider_id, modality)
            self._per_lane[key] = self._per_lane.get(key, 0) + 1

    def _room(self, provider_id: str, modality: str) -> int:
        """How many more jobs this site would take now: its budget less our own there."""
        return (self._provider_budget(provider_id, modality).limit
                - self._site_lane_in_flight(provider_id, modality))

    def _rebind(self, job: dict[str, Any]) -> bool:
        """Late bind: move a job that was never sent off a full site onto one with room.

        Called when `_admit` refused. The site chosen at submit is a guess from a snapshot up
        to 15 s old and one lottery draw, so a burst can stack on one site while another
        (this Mac included) has room. Only when the *site* is the reason (not the pool or the
        lane), and never a job with `dispatch_started_at` or an `upstream_job_id`: the store's
        conditional update is the authority (the `job` here may be a pass old), so a job that
        may be running is not re-sent.
        A voice-pinned job and a site this job already failed at are not candidates.
        Candidates are ranked exactly as `fleet.route` ranks them, seeded by the job id.
        """
        provider_id = str(job.get("provider_id") or "")
        modality = str(job.get("modality") or "")
        rebind = getattr(self.store, "rebind_unsent", None)
        if rebind is None or not provider_id.startswith(fleet.PROVIDER_PREFIX):
            return False
        shape = (str(job.get("model_id") or ""), modality)
        if shape in self._nowhere:
            return False
        try:
            # A site with room keeps its job, unless the owner paused it.
            if self._room(provider_id, modality) > 0 and not fleet.is_paused_provider(provider_id):
                return False
            if fleet.pinned_location(json.loads(job.get("params") or "{}")):
                return False
            tried = {str(fleet.controller_id_from(provider_id))}
            for value in getattr(self.store, "attempted_locations", lambda _id: [])(job["id"]):
                tried.add(str(fleet.controller_id_from(str(value)) or value))
            candidates = [
                c for c in fleet.routing_candidates(
                    str(job.get("model_id") or ""), modality, exclude_locations=tried)
                if self._room(c["provider_id"], modality) > 0
            ]
            if not candidates:
                self._nowhere.add(shape)
                return False
            best = min(candidates, key=lambda c: fleet.rank_candidate(c, str(job["id"])))
            if not rebind(job["id"], from_provider=provider_id, to_provider=best["provider_id"]):
                return False
            job["provider_id"] = best["provider_id"]
            return True
        except Exception:  # noqa: BLE001 - a failed rebind leaves the job where it is
            log.exception("late rebind of %s failed", job.get("id"))
            return False

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
    def _run(self, job: dict[str, Any], resume: bool = False) -> None:
        """One job on its own thread. Never raises."""
        try:
            if resume:
                execute_job(job, self.store, resume=True)
            else:
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
            self._nowhere.clear()
            try:
                due = scarcest_first(self.store.due())
            except Exception:  # noqa: BLE001
                log.exception("worker could not read due jobs")
                due = []

            try:
                for job in due:
                    if self._stop.is_set():
                        return
                    if self._expired_in_queue(job) or self._stranded_on_paused(job):
                        continue
                    # Reserve first, then claim: a claim that fails must not leave the
                    # budget holding a slot nobody will ever release.
                    if not self._admit(job) and not (self._rebind(job) and self._admit(job)):
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
            # **The hold reconciliation rides the dispatch loop** (Q8). It is a network call per
            # hold, so it runs on its own timer rather than every 0.4 s pass — and it is wrapped
            # separately, because a hold that cannot be reconciled must not stop dispatch.
            #
            # **On its own thread.** It used to run inline: each hold is up to 21 s of HTTP (6 s
            # connect, 15 s read) and up to 50 holds are asked serially, so one unreachable
            # Hub parked the dispatcher for minutes and every other job waited in `accepted`.
            if (time.time() - self._last_hold_sweep >= _HOLD_SWEEP_S
                    and not self._sweeping.is_set()):
                self._last_hold_sweep = time.time()
                self._sweeping.set()
                threading.Thread(target=self._sweep_holds, name="gateway-hold-sweep",
                                 daemon=True).start()

            # **When the last pass finished, successful or not.** `worker_alive` alone is a
            # boolean an operator cannot date; this is the number that says whether dispatch is
            # moving. Written every pass, so a stalled loop shows a stale timestamp rather than a
            # green light.
            self.last_pass_at = time.time()

            self._stop.wait(self.interval_s)

    def _stranded_on_paused(self, job: dict[str, Any]) -> bool:
        """End a queued job whose voice exists only on a location the owner has since paused."""
        if not fleet.is_paused_provider(str(job.get("provider_id") or "")):
            return False
        try:
            why = fleet.pause_message_for_voice(json.loads(job.get("params") or "{}"))
        except ValueError:
            return False
        if why:
            _fail(self.store, job["id"], code="VOICE_ON_PAUSED_LOCATION", detail=why)
        return bool(why)

    def _expired_in_queue(self, job: dict[str, Any]) -> bool:
        """End a job that has waited out the queue ceiling, with a typed failure -- no orphans.

        **The Gateway decides when a job is over; Story Studio follows it.** `accepted` had no
        maximum age: a job could be deferred for hours (240 deferrals) or wait for a lane that never
        freed, long after Story had stopped looking. The owner's three-hour queue ceiling is now a
        job-lifetime one for work waiting on a Hub location: past it the job fails `QUEUE_TIMEOUT`
        with a sentence, nothing has been dispatched (so nothing to cancel upstream), and Story
        shows it. Returns True when the job was ended here.
        """
        if job.get("state") != "accepted" or job.get("upstream_job_id"):
            return False
        if not fleet.is_location_provider(str(job.get("provider_id") or "")):
            return False
        ceiling = float(studio_hub.QUEUE_WAIT_CEILING_S)
        waited = time.time() - float(job.get("created_at") or time.time())
        if waited < ceiling:
            return False
        _fail(
            self.store, job["id"], code="QUEUE_TIMEOUT", retryable=True,
            detail=(f"No Mac was free for {int(waited // 3600)} hours, so the job was withdrawn from the "
                    "queue before anything ran. Nothing was started or charged; run it again when the "
                    "Macs are free."),
        )
        return True

    def _sweep_holds(self) -> None:
        try:
            self.hold_sweep = reconcile_holds(self.store)
        except Exception:  # noqa: BLE001 - reconciliation is never allowed to stop work
            log.exception("recovery-hold reconciliation failed")
        finally:
            self._sweeping.clear()

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
            "hold_sweep": dict(self.hold_sweep),
        }
