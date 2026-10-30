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
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import concurrency, pools
from . import catalog
from .adapters import BackendError, BackendRequest, adapter_for
from .adapters import studio_hub
from .adapters.base import requests_started
from .config import ARTIFACT_DIR
from .providers import registry
from .providers.definitions import KIND_STUDIO_HUB
from .store import (BACKGROUND, HUB_RESENDABLE_MODALITIES, NEVER_SENT_SUFFIX, RECOVERY_UNKNOWN,
                    TERMINAL_STATES, JobStore)
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
_TRANSIENT_SITE_CODES = {"LOCATION_NOT_READY", "LOCATION_UNREACHABLE", "SITE_CAPACITY_UNAVAILABLE",
                         # A pool member that had no free slot inside `pool.member_wait_seconds`
                         # (`adapters.pool._acquire_member`): a fact about this minute, nothing was
                         # sent, so the job waits in our queue with its reason published rather
                         # than failing or occupying a thread for ever.
                         "POOL_MEMBER_BUSY"}

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
#: The longest a job waiting for a fleet location sleeps between tries (F2, fleet audit 2026-10-07).
_FLEET_DEFER_CAP_S = 15.0


#: A job that ran past its own time limit is not retried on another Mac: the limit is the lane's
#: ceiling for this work, so another machine would only spend the same time again. It is stopped on
#: the Hub and reported. **Except once, for free Studio Hub work** (owner decision 2026-10-05): the
#: Breeze night-atlas deadline failures were slow Macs (M1 16 GB), not the job, so it moves to
#: another location one time and that move does not count against the retry ceiling
#: (`_deadline_reroute_left`). Paid lanes never move on it.
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
_HANDBACK_CODES = frozenset({WORKSTATION_YIELDED, "INSUFFICIENT_MEMORY", "NO_AVAILABLE_MACHINE"})

#: Hand-backs that are no fault of the job, so they do not count against the retry ceiling: a
#: workstation yield, and a group whose every Mac able to run the model is paused
#: (`NO_AVAILABLE_MACHINE`, from the Hub or the Gateway's own stranded-queue check, 2026-10-04).
_UNCOUNTED_CODES = frozenset({WORKSTATION_YIELDED, "NO_AVAILABLE_MACHINE"})
#: What the retry ceiling does not count: the hand-backs above, and the one free deadline move.
_UNCOUNTED_ATTEMPT_CODES = _UNCOUNTED_CODES | _NO_REROUTE_CODES


def _takes_no_new_work(provider_id: str) -> bool:
    """Whether this location must not be sent work *now*.

    Two facts, one predicate, used by every place that decides candidacy: the owner paused the
    location by hand, or it told us it is installing an update (Kind I, `fleet.DRAIN_COOLDOWN_S`).
    Both are deliberate stops, so a job bound to one waits or moves rather than being sent to a
    machine that cannot answer.
    """
    return fleet.is_paused_provider(provider_id) or fleet.is_draining_provider(provider_id)


def _deadline_reroute_left(store: Any, job_id: str) -> bool:
    """True while this job has not yet been moved after a `DEADLINE_EXCEEDED` (one move only)."""
    attempts_of = getattr(store, "attempts_of", None)
    attempts = attempts_of(job_id) if attempts_of is not None else []
    return not any(entry.get("code") in _NO_REROUTE_CODES for entry in attempts or [])


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
    return f"{job_id}.{handed_back}" if handed_back else job_id


def _defer_delay(attempts: int, cap_s: float | None = None) -> float:
    """2s, 4s, 8s … capped. Long enough not to hammer a busy machine."""
    # The exponent is clamped: 2.0 ** 10_000 raises OverflowError long before any cap applies.
    return min(2.0 ** min(max(int(attempts), 0), 30), _DEFER_CAP_S if cap_s is None else float(cap_s))


#: How often the hold reconciliation asks the Hub about its outstanding holds.
_HOLD_SWEEP_S = 60.0

#: How often the superseded-batch cleanup runs. **Its own timer and thread**, so a slow cleanup
#: can never be the reason a hold is not reconciled (both are network-bound, and sharing one
#: thread would queue one behind the other). One pass is additionally bounded by
#: `SUPERSEDED_SWEEP_BUDGET_S`.
_SUPERSEDED_SWEEP_S = 60.0

#: How often the running jobs are asked whether they have said anything (`_check_running_liveness`).
#: Cheap: one row read per job in flight, and nothing when nothing is running.
_LIVENESS_SWEEP_S = 30.0
#: What is added to a lane's own call ceiling before a silent job is called dead: a grace for the
#: adapter to notice its own timeout and settle the row itself.
_LANE_DEADLINE_GRACE_S = 120.0


def _where(row: dict[str, Any], job: dict[str, Any]) -> str:
    """The owner's own name for the place a job is running, or an honest fallback.

    Never the controller id: Story's rule is that it never shows one, and `api.scrub` would replace
    it anyway -- saying the label here keeps the sentence readable before it is scrubbed. The roster
    name wins; a location the roster has not named uses the label its own row publishes.
    """
    provider_id = str(row.get("provider_id") or job.get("provider_id") or "")
    try:
        label = fleet.label_for_provider(provider_id)
        if label:
            return label
    except Exception:  # noqa: BLE001 - a sentence must never fail a sweep
        pass
    try:
        for site in fleet.snapshot(allow_stale=True).get("locations") or []:
            if str(site.get("provider_id") or "") == provider_id and site.get("label"):
                return str(site["label"])
    except Exception:  # noqa: BLE001 - an unreadable fleet is not a missing name
        pass
    return "the provider"


def _silent_since(row: dict[str, Any]) -> float | None:
    """When this job's current attempt began waiting — **the restart's own re-send included**.

    `_check_running_liveness` measures silence from here, and the stamp it reads is the one the
    attempt that is *live now* wrote. **Three ways an attempt can begin without touching
    `dispatch_started_at`**, which is therefore only the floor:

    * **a restart re-send** — `_resume_interrupted` calls `store.note_restart_resend`, which stamps
      `restart_resent_at` and nothing else. Measuring from the pre-restart stamp failed a job the
      restart had just re-sent (probe `probe_resend_race.py`: a job stamped two hours before the
      shutdown failed `PROVIDER_TIMEOUT` 20 s into its re-send);
    * **a re-route** — `store.record_route` drops the old site's `upstream_job_id` and stamps
      `attempt_started_at`, because `begin_dispatch` only stamps while `dispatch_started_at` is
      NULL and the queue-wait statistic reads that column. Without it a job 30 min old failed
      `PROVIDER_TIMEOUT` while its second site was still submitting (`probe_reroute_sweep.py`);
    * **any new send** — `begin_dispatch` stamps `attempt_started_at` as it opens the window.

    A *retryable* failure on work that may be live upstream is what makes Story submit it again,
    which can be charged twice, so the latest of the three stamps is when the wait really began;
    `updated_at` covers a resumed row that carries none of them.
    """
    stamps = [row.get("dispatch_started_at"), row.get("restart_resent_at"),
              row.get("attempt_started_at")]
    values = [float(value) for value in stamps if isinstance(value, (int, float))]
    if values:
        return max(values)
    fallen = row.get("updated_at")
    return float(fallen) if isinstance(fallen, (int, float)) else None


def lane_deadline_s(modality: str) -> float:
    """The longest one call on this lane can legitimately take, from **the lane's own ceiling**.

    A job with no upstream id is not waiting for a *batch*: it is one call that has not answered
    yet, so the honest bound is the ceiling that call was given, not a number of ours.
    `adapters.base.CHAT_DEADLINE_CEILING_S` is the Hub's own four-hour chat ceiling (restated there
    because this gateway must outwait it), and every media lane is one HTTP request bounded by
    `adapters.openai_compat.MEDIA_TIMEOUT`. A Hub media submit returns its batch id long before
    that, so a job still without an id past it is wedged rather than busy.
    """
    from .adapters import base, openai_compat

    if str(modality or "") == "chat":
        ceiling = float(base.CHAT_DEADLINE_CEILING_S)
    else:
        ceiling = float(getattr(openai_compat.MEDIA_TIMEOUT, "read", None) or 600.0)
    return ceiling + _LANE_DEADLINE_GRACE_S


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

    Runs on a separate recovery thread, on a timer, and never raises: a hold that cannot be
    reconciled this minute is reconciled the next.
    """
    out = {"checked": 0, "closed_finished": 0, "requeued": 0, "adopted": 0, "still_open": 0}
    holds_of = getattr(store, "holds", None)
    close = getattr(store, "close_hold", None)
    if holds_of is None or close is None:
        return out
    try:
        hub_ids = [d.id for d in registry.definitions() if d.kind == "studio_hub"]
        rows = holds_of(limit=50, sweepable=True, provider_ids=hub_ids)
    except Exception:  # noqa: BLE001 - an unreadable table is not a reason to stop dispatching
        log.exception("could not read recovery holds")
        return out
    for job in rows:
        try:
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
            if job.get("modality") == "chat" or batch_id.startswith("chat:"):
                # A synchronous request handle is not a durable batch. A 404 on the batch
                # route proves nothing about it, and repeating a lost completion is unsafe.
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
        except Exception as exc:  # noqa: BLE001 - one unreadable hold must not starve the rest
            log.warning("hold %s: could not reconcile: %s", job.get("id"), exc)
            out["still_open"] += 1
    return out


#: The Hub finished it and there is an artifact: not a failure, so not closed as one.
_DONE_UPSTREAM_STATES = frozenset({"succeeded", "success", "done", "complete", "completed"})
#: The Hub's terminal words, as `studio_hub` already names them. `cancelled_items` is the Hub's
#: batch-level rollup spelling when its items were cancelled; without it a superseded batch that
#: ended that way looked "still running" to the cleanup and was asked about for ever.
_TERMINAL_UPSTREAM_STATES = frozenset(
    {"failed", "error", "cancelled", "canceled", "cancelled_items", "refused", "expired"}
)


# ---------------------------------------------------------------------------
# A batch this Gateway superseded itself (0.2.211)
# ---------------------------------------------------------------------------
#
# **The incident (three in a row, 2026-10-29).** A site's controller dropped off the network while
# a Hub batch was running. The adapter raised `lost_contact` past its grace, the worker moved the
# job to another site and submitted a *new* batch there, and the old batch kept rendering on the
# site that came back. The job succeeded, so nothing failed and no gate saw it; only `orphans.py`
# reported the duplicate, and a person had to stop it.
#
# The rule now: an attempt this Gateway moved off is **recorded in a structured field**, and when
# that site answers again the batch is reconciled. Whichever copy finishes first wins: a finished
# superseded batch is adopted when the job has no result yet, recorded as a late duplicate when it
# has one, and stopped under the same guard `orphans.py` uses for the Fleet Stop button. A batch
# no Gateway job names (`orphan_unknown`) is never touched — this cleanup only ever aims at
# batches this Gateway wrote down itself, and the general auto-stop switch stays off.

#: How many consecutive times a still-running batch may be re-followed before the job moves on.
#: **A Hub flapping on about a minute's cycle otherwise keeps a job `running` for ever**: each
#: follow restarts `_poll_batch` (so the run budget and the queue ceiling begin again) and the
#: liveness sweep never looks at a row that has an `upstream_job_id`. Two follows ride out a
#: reboot or a blip; the third lost contact falls through to the ordinary `_next_machine` path,
#: which records the batch and moves the job.
FOLLOW_LIMIT = 2

#: One cleanup pass stops after this long. It also has **its own timer and thread** (see
#: `_SUPERSEDED_SWEEP_S`), so the hold reconciliation is never queued behind it; this budget
#: bounds one pass so a dead or slow site cannot pin its thread indefinitely. Leftovers stay open
#: for the next minute, which is why the budget is a per-pass number rather than a longer one.
SUPERSEDED_SWEEP_BUDGET_S = 90.0

#: One settlement at a time per job. A superseded batch can finish while the job's *other* copy is
#: still being collected on its own thread; before this, both could pass the "already succeeded?"
#: check and write the same artifact path. The lock makes the second caller re-read the row and
#: return, which is what "first result wins" has always meant.
_SETTLE_LOCKS: dict[str, threading.Lock] = {}
_SETTLE_LOCKS_GUARD = threading.Lock()


def _settle_lock(job_id: str) -> threading.Lock:
    """The lock for one job's terminal write. Never pruned: one small lock per settled job."""
    with _SETTLE_LOCKS_GUARD:
        lock = _SETTLE_LOCKS.get(job_id)
        if lock is None:
            lock = _SETTLE_LOCKS[job_id] = threading.Lock()
        return lock


def _is_local_provider(provider_id: str) -> bool:
    try:
        return provider_id == fleet.provider_id_for(fleet.LOCAL_ID)
    except Exception:  # noqa: BLE001 - a lookup that cannot answer is not this Mac's Hub
        return False


def _superseded_entry(recovery: dict[str, Any], *, provider_id: str,
                      modality: str) -> dict[str, Any] | None:
    """The structured record of a batch the job was moved off, or `None` without a batch id."""
    batch_id = str((recovery or {}).get("batch_id") or "").strip()
    if not batch_id:
        return None
    try:
        item_index = int((recovery or {}).get("item_index") or 0)
    except (TypeError, ValueError):
        item_index = 0
    return {
        "batch_id": batch_id,
        "item_index": item_index,
        "provider_id": provider_id,
        "location": str((recovery or {}).get("location") or ""),
        "modality": str(modality or ""),
        "jobs_path": str((recovery or {}).get("jobs_path") or ""),
    }


def _hub_batch_answer(provider_id: str, modality: str, batch_id: str,
                      location: str | None = None) -> dict[str, Any] | None:
    """Ask one Hub about one batch **once**, or `None` when it did not answer.

    `studio_hub.batch_snapshot` already draws the distinction this needs: `{"known": False}` is
    the Hub saying it never heard of the batch (nothing to collect, nothing to stop), while `None`
    is "could not ask" and must leave the question open. Never a second implementation of that.
    """
    if not str(batch_id or "").strip():
        return None
    try:
        definition = registry.definition(provider_id)
        if definition is None or getattr(definition, "kind", "") != KIND_STUDIO_HUB:
            return None
        values = registry.credentials.resolved(provider_id)
        base = str(location or values.get("base_url") or definition.base_url or "")
        return studio_hub.batch_snapshot(values, base, str(batch_id).strip(),
                                         kind=str(modality or "image"),
                                         local=_is_local_provider(provider_id))
    except Exception:  # noqa: BLE001 - a read that cannot be made is "could not ask", not a verdict
        log.warning("could not read batch %s at %s", batch_id, provider_id, exc_info=True)
        return None


def _adopt_hub_batch(job: dict[str, Any], provider_id: str, modality: str, batch_id: str,
                     item_index: int, location: str | None,
                     store: JobStore) -> tuple[bool, bool, str | None]:
    """Fetch, verify and settle a finished superseded batch, through the normal done path.

    Returns `(attempted, settled, rejected)`. `attempted` says the bytes were fetched and handed
    to the done path. `settled` says **this** result settled the job; a job whose other copy won
    the race is `attempted=True, settled=False`. `rejected` is the sentence from a result the
    done path refused (`INVALID_ARTIFACT`, a disk refusal): the caller must close the entry with a
    named status rather than re-download it every sweep. Nothing escapes to the sweep's caller —
    a failed adoption is a report, like `reconcile_holds` treats it.
    """
    definition = registry.definition(provider_id)
    if definition is None or getattr(definition, "kind", "") != KIND_STUDIO_HUB:
        return False, False, None
    try:
        params = json.loads(job.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    request = BackendRequest(
        provider_id=provider_id,
        model_id=str(job.get("model_id") or ""),
        modality=str(modality or job.get("modality") or ""),
        # The job's own params travel: `_collect_batch` reads `_reference_digests` from them to
        # refuse a reference render that applied fewer references than were sent. An empty
        # params here would waive that proof.
        params=params if isinstance(params, dict) else {},
        values=registry.credentials.resolved(provider_id),
        base_url=str(location or definition.base_url or ""),
        display_name=getattr(definition, "display_name", ""),
        upstream_job_id=batch_id,
        upstream_item_index=int(item_index or 0),
    )
    try:
        result = studio_hub.adopt_batch(request, batch_id,
                                       local=_is_local_provider(provider_id))
    except Exception as exc:  # noqa: BLE001 - an adoption that cannot finish stays for next sweep
        log.warning("job %s: could not adopt superseded batch %s at %s: %s",
                    job.get("id"), batch_id, provider_id, exc)
        return False, False, None
    try:
        settled = settle_result(job["id"], result, store)
    except Exception as exc:  # noqa: BLE001 - **the same rule as `reconcile_holds`**: a refused
        # result (a corrupt or empty artifact, a disk refusal) is a report about this batch, not
        # an exception to escape the sweep and re-download the same bytes every minute.
        log.warning("job %s: superseded batch %s was refused by the done path: %s",
                    job.get("id"), batch_id, exc)
        return True, False, f"{type(exc).__name__}({getattr(exc, 'code', None) or 'no code'}): {exc}"
    return True, settled, None


def _cancel_hub_batch(provider_id: str, modality: str, batch_id: str,
                      item_index: int = 0, location: str | None = None) -> dict[str, Any]:
    """Ask one Hub to stop one batch, through the adapter the Fleet Stop button uses."""
    definition = registry.definition(provider_id)
    if definition is None or getattr(definition, "kind", "") != KIND_STUDIO_HUB:
        # **Never a paid lane's cancel.** This cleanup exists for Studio Hub batches this Gateway
        # replaced; a paid provider's cancel is a different action with billing consequences, and
        # it is not this sweep's decision to make.
        return {"ok": False, "status": None,
                "reason": f"{provider_id or 'the location'} is not a Studio Hub location"}
    cancel = getattr(adapter_for(definition), "cancel", None)
    if cancel is None:
        return {"ok": False, "status": None,
                "reason": f"the {getattr(definition, 'kind', '')} lane cannot cancel dispatched work"}
    request = BackendRequest(
        provider_id=provider_id,
        model_id="",
        modality=str(modality or "image"),
        params={},
        values=registry.credentials.resolved(provider_id),
        base_url=str(location or definition.base_url or ""),
        display_name=getattr(definition, "display_name", ""),
        upstream_job_id=batch_id,
        upstream_item_index=int(item_index or 0),
    )
    try:
        answer = cancel(request)
    except Exception as exc:  # noqa: BLE001 - a cancel reports, it never raises into the sweep
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
    return answer if isinstance(answer, dict) else {
        "ok": False, "status": None, "error": f"unreadable cancel answer: {answer!r}"
    }


def _audit_superseded(event: str, **fields: Any) -> None:
    """The startup audit is where a stop nobody clicked is explained. Never raises."""
    try:
        from . import startup_control

        startup_control.audit(event, **fields)
    except Exception:  # noqa: BLE001 - an audit that cannot be written is logged there
        log.warning("superseded-batch audit could not be written", exc_info=True)


def reconcile_superseded(store: JobStore) -> dict[str, int]:
    """Settle the batches this Gateway moved off when a site went silent (0.2.211).

    One Hub read per open entry, asked only of the site that owned the batch. The answers:

      * **finished** — the first copy to finish wins. If the job has no result yet its artifact is
        fetched and settled through the normal done path; if the job already delivered, the batch
        is recorded as a late duplicate and its bytes are never fetched (nothing is delivered
        twice); if the owner cancelled the job, it is recorded and not adopted.
      * **still running, and the job already settled** — stopped only when the orphan guard holds
        (`orphans.stop_allowed`: the job succeeded with every artifact on disk, or failed/was
        cancelled with nothing left to collect) **and** the owner's `superseded_stop` switch is on
        (default on; it is its own key, separate from the general orphan auto-stop). A job still
        running, and a job on an unresolved `unknown_outcome` hold, are left alone: their other
        copy may still finish and be adopted, and the guard never stops unkept work.
      * **no such batch** — closed as `not_found`; nothing to collect or stop.
      * **the site did not answer** — left open for the next sweep. "Could not ask" is never
        "finished".

    **Bounded per sweep, on its own clock.** It has its own timer and thread
    (`_SUPERSEDED_SWEEP_S`), so the hold reconciliation that runs on the other sweep thread is
    never queued behind it; and one pass stops after `SUPERSEDED_SWEEP_BUDGET_S`, so a dead or
    slow site cannot pin its own thread either. Anything not reached stays open for the next
    minute.

    Never raises: one unreadable entry must not starve the rest, and reconciliation must never
    stop dispatch. Only jobs whose own record names a batch reach this function, so an
    `orphan_unknown` cannot be reached through it.
    """
    out = {"checked": 0, "adopted": 0, "duplicates": 0, "stopped": 0, "closed": 0, "left": 0}
    reader = getattr(store, "open_superseded_batches", None)
    resolve = getattr(store, "resolve_superseded_batch", None)
    if reader is None or resolve is None:
        return out
    try:
        entries = reader(limit=50)
    except Exception:  # noqa: BLE001 - an unreadable table is not a reason to stop
        log.exception("could not read superseded batches")
        return out
    # Read once per sweep: the owner's switch is live, and a whole pass should agree with itself.
    stops_allowed = _superseded_stops_allowed()
    deadline = time.monotonic() + float(SUPERSEDED_SWEEP_BUDGET_S)
    for job, entry in entries:
        if time.monotonic() >= deadline:
            log.info("superseded-batch sweep used its %ss budget; the rest wait for the next pass",
                     SUPERSEDED_SWEEP_BUDGET_S)
            break
        try:
            out["checked"] += 1
            job_id = str(job.get("id") or "")
            batch_id = str(entry.get("batch_id") or "")
            provider_id = str(entry.get("provider_id") or "")
            modality = str(entry.get("modality") or job.get("modality") or "image")
            definition = registry.definition(provider_id) if provider_id else None
            if definition is None or getattr(definition, "kind", "") != KIND_STUDIO_HUB:
                resolve(job_id, batch_id, status="location_gone",
                        detail=f"{provider_id or 'the location'} is no longer a Studio Hub location")
                out["closed"] += 1
                continue
            snapshot = _hub_batch_answer(provider_id, modality, batch_id, entry.get("location"))
            if snapshot is None:
                out["left"] += 1
                continue
            if not snapshot.get("known"):
                resolve(job_id, batch_id, status="not_found",
                        detail="the Hub has no record of this batch")
                out["closed"] += 1
                continue
            # The scan's row is a snapshot; the job may have settled while the Hub was asked.
            fresh = store.get(job_id) if hasattr(store, "get") else None
            job = fresh or job
            state = str(snapshot.get("state") or "").lower()
            job_state = str(job.get("state") or "")
            if state in _DONE_UPSTREAM_STATES:
                if job_state == "succeeded":
                    resolve(job_id, batch_id, status="finished_after_delivery",
                            detail="its result arrived after the job's own copy was delivered")
                    _audit_superseded("superseded_duplicate", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality,
                                      job_state=job_state)
                    out["duplicates"] += 1
                    continue
                if job_state == "cancelled":
                    resolve(job_id, batch_id, status="finished_after_cancel",
                            detail="the owner cancelled this job; the duplicate was not collected")
                    _audit_superseded("superseded_duplicate", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality,
                                      job_state=job_state)
                    out["duplicates"] += 1
                    continue
                # **First copy to finish wins.** The job has no result yet, so this one is it.
                attempted, settled, rejected = _adopt_hub_batch(
                    job, provider_id, modality, batch_id, int(entry.get("item_index") or 0),
                    entry.get("location"), store)
                if rejected:
                    # A named answer, closed once: the bytes were refused (corrupt, empty, or the
                    # disk said no) and asking the Hub for the same file every minute would never
                    # change it. The owner sees the reason instead of a silent retry loop.
                    resolve(job_id, batch_id, status="result_rejected", detail=rejected[:300])
                    _audit_superseded("superseded_result_rejected", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality,
                                      detail=rejected[:300])
                    out["closed"] += 1
                    continue
                if not attempted:
                    out["left"] += 1  # the Hub or the network refused; try again next sweep
                    continue
                after = store.get(job_id) if hasattr(store, "get") else None
                after_state = str((after or job).get("state") or "")
                if settled and after_state == "succeeded":
                    resolve(job_id, batch_id, status="adopted",
                            detail="it finished first; its result was collected")
                    _audit_superseded("superseded_adopted", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality)
                    out["adopted"] += 1
                    _stop_losing_copy(store, job_id, batch_id, out)
                elif settled:
                    # A failed job keeps a late result beside its verdict; the bytes are on disk,
                    # nothing was delivered, and there is nothing left to collect.
                    resolve(job_id, batch_id, status="collected_late",
                            detail="its result was kept beside the job's own verdict")
                    _audit_superseded("superseded_collected_late", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality,
                                      job_state=after_state)
                    out["duplicates"] += 1
                elif after_state == "succeeded":
                    # The job's other copy settled while this one was being fetched: the bytes
                    # were not written and this is a late duplicate, not an adoption.
                    resolve(job_id, batch_id, status="finished_after_delivery",
                            detail="the job's own copy settled first")
                    _audit_superseded("superseded_duplicate", job=job_id, batch=batch_id,
                                      controller=provider_id, modality=modality,
                                      job_state=after_state)
                    out["duplicates"] += 1
                else:
                    # The disk refused the write (the job is now failed retryable): try next sweep.
                    out["left"] += 1
                continue
            if state in _TERMINAL_UPSTREAM_STATES:
                resolve(job_id, batch_id, status=f"finished_{state}",
                        detail=f"the Hub reports '{state}'; there is nothing to collect")
                out["closed"] += 1
                continue
            # **Still running or queued.** Only a job that already settled may have its duplicate
            # stopped, and only under the same guard the Fleet Stop button uses — and only while
            # the owner's own switch is on.
            #
            # **An unresolved hold is not settled.** A job `failed` with `recovery_state =
            # unknown_outcome` is waiting for this very batch's answer; its artifacts are not on
            # disk *yet*, so the guard would call "nothing to collect" and stopping it would throw
            # away the only copy. It is treated exactly like a running job: left alone.
            settled_job = (job_state in ("succeeded", "cancelled")
                           or (job_state == "failed"
                               and job.get("recovery_state") != RECOVERY_UNKNOWN))
            if not settled_job:
                out["left"] += 1
                continue
            if not stops_allowed:
                out["left"] += 1
                continue
            verdict = _orphan_stop_verdict(job)
            if not verdict.get("allowed"):
                out["left"] += 1
                continue
            answer = _cancel_hub_batch(provider_id, modality, batch_id,
                                       int(entry.get("item_index") or 0), entry.get("location"))
            if answer.get("ok"):
                resolve(job_id, batch_id, status="stopped", detail=json.dumps(answer)[:300])
                _audit_superseded("superseded_stop", job=job_id, batch=batch_id,
                                  controller=provider_id, modality=modality,
                                  requested_by="gateway-auto",
                                  reason=verdict.get("reason"), result=answer)
                out["stopped"] += 1
            else:
                _audit_superseded("superseded_stop_refused", job=job_id, batch=batch_id,
                                  controller=provider_id, modality=modality,
                                  result=answer)
                out["left"] += 1
        except Exception as exc:  # noqa: BLE001 - one entry must not starve the rest
            log.warning("superseded batch %s: could not reconcile: %s",
                        entry.get("batch_id"), exc)
            out["left"] += 1
    return out


def _orphan_stop_verdict(job: dict[str, Any]) -> dict[str, Any]:
    """The shared orphan Stop guard (`orphans.stop_allowed`), imported late to avoid a cycle."""
    from . import orphans

    return orphans.stop_allowed(job)


def _superseded_stops_allowed() -> bool:
    """The owner's switch for stopping copies **this Gateway replaced** (default on).

    Not the general orphan auto-stop: that one still starts off. A settings file that cannot be
    read is the default, never a reason to stop something the owner did not allow.
    """
    from . import orphans

    return orphans.settings()["superseded_stop"]


def _stop_losing_copy(store: JobStore, job_id: str, adopted_batch_id: str,
                      out: dict[str, int]) -> None:
    """The copy that lost the race: record it, then stop it if the guard allows.

    Runs after this Gateway adopted the *other* copy, so the job has settled succeeded and the
    guard's strongest condition — every artifact present and non-empty — has just been proven.
    The losing batch is recorded either way, so the Fleet page can draw it and the owner's switch
    can stop it later; a stop is only attempted while `superseded_stop` is on.
    """
    row = store.get(job_id) if hasattr(store, "get") else None
    if not row or str(row.get("state") or "") != "succeeded":
        return
    current = str(row.get("upstream_job_id") or "").strip()
    if not current or current == adopted_batch_id:
        return
    provider_id = str(row.get("provider_id") or "")
    modality = str(row.get("modality") or "")
    definition = registry.definition(provider_id)
    if definition is None or getattr(definition, "kind", "") != KIND_STUDIO_HUB:
        # **Nothing outside a Studio Hub is this cleanup's to stop.** A paid lane's batch is not a
        # duplicate this Gateway chose to leave behind; cancelling it is a different decision with
        # billing consequences, and it is never made here.
        return
    record = getattr(store, "record_superseded_batch", None)
    if record is not None:
        record(job_id, {"batch_id": current, "item_index": 0, "provider_id": provider_id,
                        "location": "", "modality": modality, "jobs_path": ""})
    if not _superseded_stops_allowed():
        return  # recorded for the page and a later switch-on; the owner turned stopping off
    verdict = _orphan_stop_verdict(row)
    if not verdict.get("allowed"):
        return
    answer = _cancel_hub_batch(provider_id, modality, current, 0)
    resolve = getattr(store, "resolve_superseded_batch", None)
    if answer.get("ok"):
        if resolve is not None:
            resolve(job_id, current, status="stopped", detail=json.dumps(answer)[:300])
        _audit_superseded("superseded_stop", job=job_id, batch=current, controller=provider_id,
                          modality=modality, requested_by="gateway-auto",
                          reason=verdict.get("reason"), result=answer)
        out["stopped"] += 1
    else:
        _audit_superseded("superseded_stop_refused", job=job_id, batch=current,
                          controller=provider_id, modality=modality, result=answer)


def is_background(job: dict[str, Any]) -> bool:
    return job.get("priority") == BACKGROUND


def fair_key(job: dict[str, Any]) -> str:
    """Whose turn a background job is: its project run (`job_group`), else its caller."""
    return str(job.get("job_group") or job.get("caller") or "")


def scarcest_first(jobs: list[dict[str, Any]],
                   group_in_flight: dict[str, int] | None = None,
                   background_in_flight: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Order due work without bypassing the worker's existing admission authority.

    A fleet job reads the preference of its already-bound location once per distinct
    (provider, model, function) in this pass. Eligible jobs sort by that ordinal preference,
    then by `due()`'s arrival order; a job whose bound location is no longer eligible follows
    them and can still late-bind in the normal loop. Thus one capable Mac sees its preferred
    queued function first, while equal and no-preference work stays FIFO. `_admit`, `_room`,
    the conditional claim and `_rebind` remain the final capacity and no-duplicate checks.

    Cloud jobs keep the prior scarcity/group-turn ordering and stay ahead because they take
    no Mac. One candidate read per distinct bound local shape per pass.

    **Turns (D6).** Inside one scarcity class, a job's turn is how many jobs of its
    `job_group` are already running (`group_in_flight`) plus its place among that group's
    waiting jobs. So a 20-image redo submitted behind a 500-image wave interleaves with it
    instead of waiting for all 500 (sim: done after 152 s, not 903 s). A job with no group is
    its own group — turn 1 — so a queue with no groups is ordered exactly as before.

    **Background jobs (0.2.165) come after every normal job**, however long they have waited, and
    take turns among themselves by `fair_key` (project run, else caller) counting that key's running
    background jobs (`background_in_flight`): two projects' pools share the idle Macs round-robin.
    """
    pools: dict[tuple[str, str], float] = {}
    local_preferences: dict[tuple[str, str, str], float | None] = {}
    group_in_flight = group_in_flight or {}
    background_in_flight = background_in_flight or {}
    turns: dict[tuple[float, str], int] = {}
    background_turns: dict[str, int] = {}

    def pool(job: dict[str, Any]) -> float:
        if not str(job.get("provider_id") or "").startswith(fleet.PROVIDER_PREFIX):
            return 0
        key = (str(job.get("model_id") or ""), str(job.get("modality") or ""))
        if key not in pools:
            pools[key] = fleet.pool_size(*key) or float("inf")
        return pools[key]

    def turn(job: dict[str, Any]) -> tuple[float, int]:
        group = job.get("job_group")
        if not group:
            return pool(job), 1
        key = (pool(job), str(group))
        turns[key] = turns.get(key, group_in_flight.get(str(group), 0)) + 1
        return pool(job), turns[key]

    def local_preference(job: dict[str, Any]) -> float | None:
        provider_id = str(job.get("provider_id") or "")
        model_id = str(job.get("model_id") or "")
        modality = str(job.get("modality") or "")
        key = (provider_id, model_id, modality)
        if key not in local_preferences:
            location_id = fleet.controller_id_from(provider_id)
            choices = (fleet.routing_candidates(model_id, modality, only_location=location_id)
                       if location_id is not None else [])
            chosen = next((candidate for candidate in choices
                           if candidate.get("location_id") == location_id), None)
            local_preferences[key] = (float(chosen.get("preference"))
                                      if chosen is not None and chosen.get("preference") is not None
                                      else None)
        return local_preferences[key]

    def rank(job: dict[str, Any], index: int) -> tuple[float, float, int, int]:
        if is_background(job):
            key = fair_key(job)
            background_turns[key] = background_turns.get(key, background_in_flight.get(key, 0)) + 1
            return 3.0, float(background_turns[key]), 0, index
        provider_id = str(job.get("provider_id") or "")
        if not provider_id.startswith(fleet.PROVIDER_PREFIX):
            scarcity, group_turn = turn(job)
            return 0.0, scarcity, group_turn, index
        preference = local_preference(job)
        if preference is None:
            return 2.0, 0.0, 0, index
        return 1.0, preference, 0, index

    try:
        # Read in arrival order so cloud group turns and every equal local rank remain FIFO.
        ranked = sorted((rank(job, index), index) for index, job in enumerate(jobs))
        return [jobs[index] for _, index in ranked]
    except Exception:  # noqa: BLE001 - ordering is a preference; never the reason nothing runs
        log.exception("could not order due jobs by scarcity and work preference")
        return jobs


def _job_params(job: dict | None) -> dict:
    """A job row's `params` as an object — they are stored as JSON."""
    raw = (job or {}).get("params")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pool_members_for(job: dict | None, modality: str) -> list[str] | None:
    """The members an image-pool job may move between, or None when it must not move across members.

    None for a non-pool job, and for one already accepted upstream, held or past queueing: its result
    belongs to the member that ran it. An image pool whose models are gone sits out (`[]`).
    """
    if not job or modality != "image":
        return None
    pool_id = _job_params(job).get("_pool_id")
    if not pool_id or str(job.get("upstream_job_id") or "").strip() or job.get("state") not in ("accepted", "queued"):
        return None
    from . import image_assignment, pools
    pool = pools.get(pool_id)
    return image_assignment.pool_models(pool) if pool else []


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

    # An image-pool job may move to any location with a free Mac assigned one of its members, but only
    # while nothing was accepted upstream (held, batch and unknown-outcome jobs stay where they are).
    job = store.get(job_id) if hasattr(store, "get") else None
    pool_members = _pool_members_for(job, modality)
    # **A re-route obeys the same capability rule as the submit** — a reference job is not sent to a
    # location whose worker never advertised `edit`, however many attempts it has left.
    require_capability = fleet.required_capability(modality, _job_params(job))
    # Looked up before either branch: the pool branch reads it too, and reading it after the assignment
    # below made `room` a local the pool branch used unbound (UnboundLocalError on every pool re-route).
    room = _ROOM_HOOKS.get(job_id)
    if pool_members is not None:
        chosen, member, _note = fleet.route_pool(pool_members, modality, exclude_locations=tried,
                                                 load=room, require_capability=require_capability)
        if chosen is None or not store.record_route(job_id, chosen.id, model_id=member):
            return None
        if room is not None and room(chosen.id, claim=True)[0] <= 0:
            store.defer(job_id, delay_s=0, unsent=True,
                        reason="waiting for room: every location with a Mac assigned a pool model is full")
            return QUEUED_IN_GATEWAY
        return chosen.id

    if sum(entry.get("code") not in _UNCOUNTED_ATTEMPT_CODES for entry in attempts) >= int(policy["max_attempts"]):
        # The ceiling the owner named. The failure path builds the sentence from this log.
        return None
    if room is None:
        chosen, _note = fleet.route(model_id, modality, exclude_locations=tried,
                                    require_capability=require_capability)
    else:
        chosen, _note = fleet.route(model_id, modality, exclude_locations=tried, load=room,
                                    require_capability=require_capability)
    if chosen is None or _location(chosen.id) in tried:
        return None
    record_route = getattr(store, "record_route", None)
    # **A re-route takes a lane with room, or waits here (10-08 pile-up).** The claim books the job at
    # the new site only while its lane has room, under the worker's lock, so a burst of moves cannot
    # all read the same free slot. Every lane full: the job goes back to our queue bound to the best
    # site, nothing sent, and normal dispatch (`_admit`, then `_rebind`) sends it when a lane frees.
    if room is not None and room(chosen.id, claim=True)[0] <= 0:
        defer = getattr(store, "defer", None)
        if record_route is None or defer is None:
            return None
        record_route(job_id, chosen.id)
        defer(job_id, delay_s=0, unsent=True,
              reason=f"waiting for room: every location offering {model_id} is full")
        return QUEUED_IN_GATEWAY
    if record_route is not None:
        record_route(job_id, chosen.id)
    return chosen.id


#: `_next_machine`'s answer when every lane that could take the job is full: it is queued here again.
QUEUED_IN_GATEWAY = "queued-in-gateway"
#: How far back a send counts as "just sent", i.e. one the fleet snapshot may not show yet.
#: ponytail: about one refresh (15 s) plus a slow fan-out; read the snapshot's own age if it drifts.
_JUST_SENT_S = 30.0
#: A late rebind that raised waits this long before the next try, doubling up to the cap.
_REBIND_BACKOFF_S = 1.0
_REBIND_BACKOFF_CAP_S = 60.0


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
    cap = float(wait["defer_cap_seconds"])
    # **A fleet job comes back within seconds, not minutes (F2).** A Mac frees every few minutes
    # somewhere in the fleet, and a job asleep on the 300 s ladder missed it (10-04: 34 jobs reached
    # 256-300 s waits). The deferral count still bounds it: only a Hub refusal counts (waiting for
    # room here does not), so 240 at 15 s is an hour of back-to-back refusals (10-04's worst was 13).
    if fleet.is_location_provider(str(current.get("provider_id") or "")):
        cap = min(cap, _FLEET_DEFER_CAP_S)
    if handback:
        return bool(defer(
            job_id,
            delay_s=_defer_delay(attempts, cap),
            reason=f"waiting for a free Mac - {exc}"[:300],
            new_round=True,
        ))
    return bool(defer(
        job_id,
        delay_s=_defer_delay(attempts, cap),
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


def _mp4_problem(content: bytes) -> str | None:
    """Walk the top-level boxes: an m4a/mp4 needs `ftyp` and `moov`, and no box may run past the end."""
    pos, end, seen = 0, len(content), set()
    while pos + 8 <= end:
        size, name = struct.unpack(">I4s", content[pos:pos + 8])
        if size == 1:
            if pos + 16 > end:
                return "is cut off inside a box header"
            size = struct.unpack(">Q", content[pos + 8:pos + 16])[0]
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return f"is cut off: its '{name.decode('latin-1')}' box runs past the end"
        seen.add(name)
        pos += size
    if b"ftyp" not in seen:
        return "has no 'ftyp' box"
    if b"moov" not in seen:
        return "has no 'moov' box (moov atom not found)"
    return None


_AUDIO_SIGNATURES = (b"RIFF", b"fLaC", b"OggS")
_CHECKED_AUDIO = {"audio/wav", "audio/flac", "audio/x-flac", "audio/ogg"}
_CHECKED_IMAGES = {"image/png", "image/jpeg", "image/webp"}


def _is_image(head: bytes) -> bool:
    if head[:8] == b"\x89PNG\r\n\x1a\n":  # a PNG must open with an IHDR of non-zero size
        return head[12:16] == b"IHDR" and len(head) >= 24 and min(struct.unpack(">II", head[16:24])) > 0
    return (head[:3] == b"\xff\xd8\xff" or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[:4] == b"GIF8" or head[4:8] == b"ftyp")  # ftyp: HEIC/AVIF


def artifact_problem(content: bytes | None, media_type: str | None) -> str | None:
    """Why these bytes are not a usable artifact, or `None`. Header bytes only, never a decode.

    Measured 2026-10-09: a Hub voice item finished `done` with a 0-byte m4a, the Gateway stored it
    and marked the job succeeded, and Story failed its narration three times collecting it. Run
    over all 20,761 stored artifacts this flags that one file and nothing else.

    Lenient about a *label*, strict about a *broken file*: a paid lane whose CDN calls a JPEG
    `image/png` still delivered a good picture, and refusing it would pay twice. So an image type
    needs any image header, an audio type any audio signature, and anything that opens with `ftyp`
    must have its `moov` and no box cut short (unless it is labelled an image). MP3 has no container and an unknown type nothing to
    read: size only.
    """
    if not content:
        return "is empty (0 bytes)"
    kind = (media_type or "").split(";")[0].strip().lower()
    head = content[:24]
    if head[4:8] == b"ftyp" and not kind.startswith("image/"):  # HEIC/AVIF carry `meta`, not `moov`
        return _mp4_problem(content)
    if kind in ("audio/mp4", "audio/x-m4a", "video/mp4"):
        return "has no 'ftyp' box"
    if kind in _CHECKED_IMAGES and not _is_image(head):
        return f"is not a valid {kind} file"
    if kind in _CHECKED_AUDIO and not (head[:4] in _AUDIO_SIGNATURES or head[:3] == b"ID3"
                                       or (head[0] == 0xFF and len(head) > 1 and head[1] & 0xE0 == 0xE0)):
        return f"is not a valid {kind} file"
    return None


def _check_artifacts(result: Any) -> None:
    """Refuse a result whose artifact is empty or broken, as a **settled, known** failure.

    Raised from inside the dispatch, so the ordinary A7 rule applies: a fleet job is retried on
    another Mac, a cloud lane follows its own rules. Nothing is written and no delivery receipt is
    sent, so the Hub keeps its copy. The adopt path gets the same refusal and keeps its hold.
    """
    for index, artifact in enumerate(getattr(result, "artifacts", None) or []):
        problem = artifact_problem(artifact.content, artifact.media_type)
        if problem:
            raise BackendError(
                f"the provider answered done, but artifact {index} {problem}; not kept",
                code="INVALID_ARTIFACT", upstream_accepted=True, upstream_settled=True,
                retryable=True,
            )


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
    evidence: dict[str, Any] | None = None,
    review_candidate: dict[str, Any] | None = None,
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
            **({"evidence": evidence} if evidence else {}),
            **({"review_candidate": review_candidate} if review_candidate else {}),
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


def settle_result(job_id: str, result: Any, store: JobStore) -> bool:
    """Save what an adapter returned, send the delivery receipt, and settle the job `succeeded`.

    The one done path: a job that ran to the end and a **held job the Hub has since finished**
    (`adopt_hold`) both come through here, so an adopted result is a normal one -- same artifact
    names, same receipt-after-save ordering, same published URLs.

    **One settlement at a time per job.** A superseded batch (0.2.211) can finish on the recovery
    thread while the job's other copy is being collected on its own thread; without this, both
    could pass the "already succeeded?" check below and write the same artifact path. The lock
    makes the second caller re-read the row and return, which is what "first result wins" means.

    Returns whether **this** result settled the job. `False` is a result somebody else's copy
    already delivered (or one the disk had no room for); the caller must not treat it as adopted.
    """
    with _settle_lock(job_id):
        return _settle_result_locked(job_id, result, store)


def _settle_result_locked(job_id: str, result: Any, store: JobStore) -> bool:
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
        return False
    _check_artifacts(result)  # the adopt path's guard; a live result was checked in `_dispatch`
    try:
        diskguard.require_room(sum(len(a.content or b"") for a in result.artifacts))
    except diskguard.DiskLow as low:
        _fail(store, job_id, code="DISK_LOW", detail=low.sentence, retryable=True)
        return False
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
    return True


#: `job id -> callback(provider_id)` for jobs the `Worker` is running: a re-route calls it so the
#: worker's booking follows the job to its new location (F2). Keyed by id so `execute_job` keeps
#: the `(job, store)` shape every caller and test uses.
_MOVE_HOOKS: dict[str, Callable[[str], None]] = {}
#: `job id -> room(provider_id, claim=False) -> (room, ours, just_sent)`: the worker's live books for
#: this job's lane at a site, so a re-route ranks and claims by them (`fleet.apply_load`).
_ROOM_HOOKS: dict[str, Callable[..., tuple[int, int, int]]] = {}


def _moved(job_id: str, provider_id: str) -> None:
    hook = _MOVE_HOOKS.get(job_id)
    if hook is not None:
        hook(provider_id)


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
        upstream = str(current.get("upstream_job_id") or "")
        _dispatch(job, store, provider_id, model_id, modality, params, upstream,
                  resend=not upstream)
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


def _published_cap(definition: Any, model_id: str, modality: str) -> int | None:
    """The model's published output allowance, for a lane that must state a `max_tokens`.

    **Enrichment, never a gate.** No inventory or no model row means "this gateway has no
    published cap", and such an adapter falls back to its own constant exactly as before.
    The caller's own value still wins: the adapter reads `params` first.
    """
    inventory = getattr(registry, "models", None)
    if inventory is None:
        return None
    model = next(
        (m for d, m in inventory.enabled_models(modality)
         if d.id == getattr(definition, "id", "") and m.get("model_id") == model_id),
        None,
    )
    return catalog.published_output_cap(definition, model_id, model)


def _dispatch(job: dict[str, Any], store: JobStore, provider_id: str, model_id: str,
              modality: str, params: dict[str, Any], resume_id: str, *, resend: bool = False) -> None:
    """Call the adapter and settle what it answers. `resume_id` polls that upstream id first.

    `resend`: a restart re-sends a fleet job whose first send was unanswered (`restart_can_resend`).
    Anything short of the Hub's own settled answer is held, never moved: the first send may run."""
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
    #: Consecutive "the batch is still running, follow it again" answers at this site. Bounded by
    #: `FOLLOW_LIMIT` so a flapping Hub cannot hold a job `running` for ever (see there).
    follows = 0
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
            max_output_tokens=_published_cap(definition, model_id, modality),
        )
        adapter = adapter_for(definition)
        if job.get("waiting_json") or current.get("waiting_json"):
            on_wait(None)  # a fresh attempt is not waiting for anything yet
        sends_before = requests_started()
        try:
            result = adapter.resume(request) if resume_id else adapter.generate(request)
            _check_artifacts(result)
            break
        except BackendError as exc:
            log.warning("job %s failed via %s: %s", job_id, provider_id, exc)
            if exc.code == "INSUFFICIENT_MEMORY":
                fleet.note_memory_refusal(provider_id, model_id, str(exc))  # F1: rest that Mac
            elif exc.code == "LOCATION_NOT_READY":
                # **A location that is installing an update has stopped taking work, and saying
                # so is not a reason to ask it again** (Kind I). `_next_machine`/`_rebind` already
                # skip what they have tried, but the job stays *bound* to this location, so the
                # deferral below sent it straight back: 124 refusals from one installing Mac in
                # 80 minutes. The rest is recorded only for the Hub's own install sentence — a
                # Mac that is merely busy is a moment and stays a candidate.
                fleet.note_drain_refusal(provider_id, str(exc))
            current = store.get(job_id) if hasattr(store, "get") else job
            current = current or {}
            # **A job that has already settled never dispatches again.** This thread can still be
            # polling the losing copy of a job whose other copy won — the superseded-batch cleanup
            # (0.2.211) settles the job and then asks this copy to stop, so the stop arrives here
            # as an ordinary failure. Without this the re-route below would treat it as a machine
            # that could not run the job and submit a *third* batch for work already delivered.
            # The late answer is kept as a footnote on the settled row, never as a reason to send.
            if str(current.get("state") or "") in TERMINAL_STATES:
                log.warning("job %s: the attempt at %s answered after the job settled (%s); "
                            "recording it, not dispatching again", job_id, provider_id, exc.code)
                _fail(store, job_id, code=exc.code, detail=str(exc)[:500])
                return
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
                # Explicit ambiguity outranks even a wrapped 400 from a pool member.
                unknown = True
            if resend and not exc.upstream_settled:
                unknown = True  # the first send may be running at this site: hold, never move
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
            if (kind_is_hub and not resume_id and not resend and (exc.recovery or {}).get("replayable")
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
            if (unknown and pinned is None and kind_is_hub and not resend
                    and (exc.recovery or {}).get("lost_contact")
                    and current.get("cancel_requested_at") is None):
                recovery = dict(exc.recovery or {})
                batch_id = str(recovery.get("batch_id") or "").strip()
                # **Ask the batch before moving the job** (0.2.211). The grace is over, but the
                # site may be answering again by now — a controller rebooting, or this Gateway
                # restarting with it — and in that case there is nothing to re-dispatch: a batch
                # that is still running is followed, and one that finished is collected. One GET,
                # no wait, so a site that is genuinely gone costs nothing but that request.
                snapshot = (_hub_batch_answer(provider_id, modality, batch_id,
                                              recovery.get("location"))
                            if batch_id else None)
                state = (str(snapshot.get("state") or "").lower()
                         if snapshot is not None and snapshot.get("known") else "")
                if state in _DONE_UPSTREAM_STATES:
                    attempted, settled, rejected = _adopt_hub_batch(
                        current or job, provider_id, modality, batch_id,
                        int(recovery.get("item_index") or 0),
                        recovery.get("location"), store)
                    if settled:
                        log.warning("job %s: %s finished batch %s while contact was lost; "
                                    "collected it instead of dispatching a second batch",
                                    job_id, provider_id, batch_id)
                        return
                    if rejected:
                        # The done path refused the bytes (corrupt or empty). There is nothing
                        # more to get from this batch; record it and move the job on below.
                        log.warning("job %s: %s finished batch %s, but the done path refused it "
                                    "(%s); moving the job on", job_id, provider_id, batch_id,
                                    rejected)
                    elif attempted:
                        # The disk refused the write, so the job is now failed retryable and
                        # nothing was kept. **Record the batch anyway**: the sweep retries the
                        # collection once the disk frees, so the finished work is not lost.
                        entry = _superseded_entry(recovery, provider_id=provider_id,
                                                  modality=modality)
                        record = getattr(store, "record_superseded_batch", None)
                        if entry is not None and record is not None:
                            record(job_id, entry)
                        log.warning("job %s: %s finished batch %s but it could not be kept; the "
                                    "batch is recorded for the cleanup to retry",
                                    job_id, provider_id, batch_id)
                        return
                elif state and state not in _TERMINAL_UPSTREAM_STATES:
                    # Still running (or queued) there: keep following it. Re-entering the loop
                    # with `resume_id` polls the same batch and never submits a new one.
                    #
                    # **Bounded**: a Hub flapping on a minute's cycle would otherwise keep this
                    # job `running` for ever, restarting the poll's own budget each time and
                    # never reaching the liveness sweep (it skips rows with an upstream id).
                    follows += 1
                    if follows <= FOLLOW_LIMIT:
                        log.warning("job %s: %s still has batch %s; following it again "
                                    "(%s of %s) instead of dispatching a second batch",
                                    job_id, provider_id, batch_id, follows, FOLLOW_LIMIT)
                        resume_id = batch_id
                        continue
                    log.warning("job %s: %s still has batch %s after %s follows; moving the job "
                                "on and recording the batch", job_id, provider_id, batch_id,
                                FOLLOW_LIMIT)
                # **Re-read immediately before the move.** The read above can take fifteen
                # seconds; a cancel, or the other copy settling the job, may have happened since.
                current = store.get(job_id) if hasattr(store, "get") else current
                current = current or {}
                if str(current.get("state") or "") in TERMINAL_STATES:
                    log.warning("job %s: settled while its batch was being re-read; recording "
                                "this attempt's answer instead of moving it", job_id)
                    _fail(store, job_id, code=exc.code, detail=str(exc)[:500])
                    return
                abandoned = dict(current, provider_id=provider_id)
                # **Written before `_next_machine`**, so a crash between recording the move and
                # writing the batch cannot leave a duplicate nobody can aim at. If nothing moves,
                # the entry is closed as `not_moved` below and the hold sweep owns the batch.
                entry = _superseded_entry(recovery, provider_id=provider_id, modality=modality)
                record = getattr(store, "record_superseded_batch", None)
                resolve_entry = getattr(store, "resolve_superseded_batch", None)
                if entry is not None and record is not None:
                    record(job_id, entry)
                moved = _next_machine(
                    store, job_id, model_id, modality, provider_id,
                    code=exc.code, detail=str(exc)[:500],
                )
                if moved is None:
                    if entry is not None and resolve_entry is not None:
                        resolve_entry(job_id, entry["batch_id"], status="not_moved",
                                      detail="the job stayed on this batch; its hold owns it")
                else:
                    log.warning("job %s: %s went silent mid-job; moving it to %s and cancelling "
                                "the abandoned attempt", job_id, provider_id,
                                "the Gateway queue (every lane full)" if moved == QUEUED_IN_GATEWAY else moved)
                    # The immediate best-effort stop of the abandoned attempt is the same kind of
                    # automatic stop as the cleanup's: the owner's switch covers both. (On by
                    # default; off leaves the duplicate to the Fleet page's Stop.)
                    if _superseded_stops_allowed():
                        threading.Thread(target=cancel_upstream, args=(abandoned, store),
                                         kwargs={"record": False}, daemon=True,
                                         name=f"gateway-cancel-{job_id[:8]}").start()
                    if moved == QUEUED_IN_GATEWAY:
                        return
                    provider_id = moved
                    resume_id = ""
                    follows = 0  # a different batch at a different site gets its own follows
                    _moved(job_id, moved)
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
            if (not unknown and pinned is None
                    and (exc.code not in _NO_REROUTE_CODES
                         or (kind_is_hub and _deadline_reroute_left(store, job_id)))
                    and (exc.code != WORKSTATION_YIELDED or kind_is_hub)):
                moved = _next_machine(
                    store, job_id, model_id, modality, provider_id,
                    code=exc.code, detail=detail,
                )
                if moved == QUEUED_IN_GATEWAY:
                    log.warning("job %s: %s could not run it (%s); every other lane is full, so it "
                                "waits in the Gateway queue", job_id, provider_id, exc.code)
                    return
                if moved is not None:
                    log.warning(
                        "job %s: %s could not run it (%s); trying %s next",
                        job_id, provider_id, exc.code, moved,
                    )
                    provider_id = moved
                    resume_id = ""  # a fresh submission at the next location
                    _moved(job_id, moved)
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
                elif exc.code in _UNCOUNTED_CODES:
                    terminal_detail = detail.rstrip(". ") + ". " + (
                        "This voice exists only on that location, so it was not moved to another Mac."
                        if pinned is not None else "No other location offering this model could take it."
                    ) + " Nothing was charged; run it again."
            # Only here, where no re-route is left, does a refused narration section's review
            # candidate bind to the job: an attempt that moved on to another location binds none.
            candidate = getattr(exc, "review_candidate", None)
            _fail(
                store,
                job_id,
                code=exc.code,
                detail=terminal_detail,
                retryable=exc.retryable,
                recovery_state=RECOVERY_UNKNOWN if unknown else None,
                recovery_detail=recovery_detail if unknown else None,
                recovery_context=exc.recovery if unknown else None,
                evidence=getattr(exc, "evidence", None),
                review_candidate=(dict(candidate, provider_id=provider_id)
                                  if candidate and not unknown else None),
            )
            return
        except Exception as exc:  # noqa: BLE001 - a backend bug must not kill the worker
            log.exception("job %s raised unexpectedly", job_id)
            if requests_started() == sends_before and not resume_id and not resend:
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


def restart_can_follow(job: dict[str, Any]) -> bool:
    """Whether a restart now would resume this running job by its upstream id instead of holding it.

    True only for an upstream id on a lane whose adapter can poll it (`resume`). A Hub chat's
    `chat:` id cannot be polled, and a cloud lane without `resume` is held even with its id."""
    upstream = str(job.get("upstream_job_id") or "")
    if not upstream or (job.get("modality") == "chat" and upstream.startswith("chat:")):
        return False
    definition = registry.definition(str(job.get("provider_id") or ""))
    return definition is not None and getattr(adapter_for(definition), "resume", None) is not None


def restart_can_resend(job: dict[str, Any]) -> bool:
    """Whether a restart re-sends this running fleet job instead of holding it (owner 2026-10-09).

    Its send was unanswered at shutdown, so there is no batch id to follow. The Hub answers a repeated
    client request id with the batch it already made (durable, across a Hub restart), so the same key
    to the same site attaches to that batch, or runs the job once if the Hub never got it. Only a
    Studio Hub site, on a lane that dedupes that way, with no cancel asked for."""
    if (job.get("upstream_job_id") or not job.get("dispatch_started_at")
            or job.get("cancel_requested_at") is not None
            or job.get("modality") not in HUB_RESENDABLE_MODALITIES):
        return False
    definition = registry.definition(str(job.get("provider_id") or ""))
    return definition is not None and getattr(definition, "kind", "") == KIND_STUDIO_HUB


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
        #: **`_budget_lock` is a leaf lock. Nothing under it may reach `fleet.snapshot()` or
        #: the store.** It exists to move the five books below together (`_book`/`_release`);
        #: one counter read needs no lock at all, because a dict read of an int is atomic
        #: under the GIL.
        #:
        #: The rule is not style, it is a lock order. The fleet fan-out holds
        #: `fleet._refresh_lock` and calls back into this worker (`fleet._attach_admission` →
        #: `_site_lane_in_flight`), so a fleet read taken while this is held is the reverse
        #: order. On 2026-10-19 the two met: a fan-out whose slowest controller took 435 s held
        #: `_refresh_lock` while `_admit` held `_budget_lock` and waited for the fleet cache,
        #: 63 of 73 threads parked in `RLock.acquire`, and — because `/health` took both —
        #: every later request, health included, waited for ever. **Fleet and store first,
        #: then `_budget_lock`.**
        #:
        #: Everything admission needs from the fleet or the store is read by
        #: `_admission_reads` **before** this lock is taken; `/health` and the fleet fan-out
        #: read only the published counters below, never this lock.
        self._budget_lock = threading.RLock()  # re-entered by `_book` from `_admit`
        #: Jobs in flight, republished by `_book`/`_release` and read by `/health`,
        #: `liveness()` and the console **without** `_budget_lock`, so a health probe can
        #: never park behind admission, or admission behind a probe.
        self._active_total = 0
        self._in_flight: dict[str, int] = {}
        self._lane_in_flight: dict[str, int] = {}
        #: `(provider_id, modality) -> running count`. The provider book above is per
        #: *site*; this one is per site-and-modality, which is the shape the Hub's own
        #: free-slot count is published in and therefore the only one that can be
        #: compared with it.
        self._per_lane: dict[tuple[str, str], int] = {}
        #: `(provider_id, modality, model_id) -> running count`: what `_room` needs to know
        #: whether the Macs that fit *this* model are already spoken for (D1, fit-aware).
        self._per_model: dict[tuple[str, str, str], int] = {}
        #: `job_group -> running count`, the fair-turns key `scarcest_first` orders by (D6).
        self._per_group: dict[str, int] = {}
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
        #: The superseded-batch cleanup's own clock and thread. Separate from the hold sweep so
        #: one can never queue behind the other; `superseded_sweep` is its last result.
        self._last_superseded_sweep = 0.0
        self._superseded_sweeping = threading.Event()
        #: When the running jobs were last asked whether they have said anything (see
        #: `_check_running_liveness`), and the last sweep's count, kept for an operator.
        self._last_liveness = 0.0
        self.liveness_sweep: dict[str, int] = {"checked": 0, "said_something": 0, "failed": 0}
        #: `(model, modality)` shapes for which a late rebind found no site with room this
        #: pass, so the rest of the queue behind them does not repeat the search. Cleared each pass.
        self._nowhere: set[tuple[str, str]] = set()
        #: `(model, modality)` of normal fleet jobs still waiting, read once per pass at the first
        #: background job (`_background_room`); `None` until read. Cleared each pass.
        self._normal_waiting: set[tuple[str, str]] | None = None
        self.hold_sweep: dict[str, int] = {}
        #: What the superseded-batch cleanup last found (0.2.211): adopted, stopped, duplicates,
        #: left open. It rides the hold sweep's timer and its thread, so it is never a second
        #: schedule to reason about.
        self.superseded_sweep: dict[str, int] = {}
        #: `(provider_id, model_id) -> jobs this pass left waiting for that site`: the line a speed-
        #: ranked job would join by staying (`_rebind`). Cleared each pass.
        self._ahead: dict[tuple[str, str], int] = {}
        #: `(provider_id, modality) -> monotonic times` of our sends in the last `_JUST_SENT_S`:
        #: what the fleet snapshot cannot have seen yet (`fleet.apply_load`).
        self._sent: dict[tuple[str, str], list[float]] = {}
        #: `job id -> (retry at, delay)` after a late rebind raised (10-07: "database is locked"
        #: 2,758 times per job, every 0.4 s pass). Dropped once the job moves or is admitted.
        #: ponytail: a job that ends while backed off leaves its entry (bytes); prune if that matters.
        self._rebind_backoff: dict[str, tuple[float, float]] = {}
        #: `job id -> job` for every job thread alive in this process: what a drain waits on.
        self._running: dict[str, dict[str, Any]] = {}
        #: Dispatch takes no new work before this `time.time()` (a drain; `inf` on shutdown).
        self._paused_until = 0.0
        self._render_service: Any | None = None

    def attach_render_service(self, service: Any) -> None:
        """Attach the one private render coordinator to this existing dispatch clock."""
        if self._thread is not None or self._render_service not in (None, service):
            raise RuntimeError("render service must be attached once before worker start")
        self._render_service = service

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._render_service is not None:
            self._render_service.recover()
        self._thread = threading.Thread(target=self._loop, name="gateway-worker", daemon=True)
        self._thread.start()
        self._resume_interrupted()

    def _resume_interrupted(self) -> None:
        """Follow every job a restart interrupted to its end, by the upstream id it was given.

        The store left them `running` (`JobStore.interrupted`). A lane whose adapter can poll by
        id (`resume`) is polled on its own thread exactly as before the restart, counted against
        the budgets like any running job but never refused by them — the work is already running
        upstream. A fleet send unanswered at shutdown is re-sent (`restart_can_resend`).
        Any other lane gets the restart hold, as every lane used to.
        """
        ids = list(getattr(self.store, "interrupted", None) or [])
        if ids:
            self.store.interrupted = []
        for job_id in ids:
            try:
                job = self.store.get(job_id)
                if not job or job.get("state") != "running":
                    continue
                if restart_can_follow(job):
                    log.warning("job %s: resuming upstream %s on %s after a restart",
                                job_id, job.get("upstream_job_id"), job.get("provider_id"))
                elif restart_can_resend(job):
                    self.store.note_restart_resend(job_id)
                    log.warning("job %s: its send to %s was unanswered at shutdown; re-sending it "
                                "there with the same key", job_id, job.get("provider_id"))
                else:
                    self.store.hold_interrupted(job_id)
                    continue
                self._book(job)
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
        if self._render_service is not None:
            self._render_service.stop()

    # -- drain (a restart never strands a job) -------------------------------
    def dispatch_paused(self) -> bool:
        return time.time() < self._paused_until

    def pause_dispatch(self, seconds: float = float("inf")) -> None:
        self._paused_until = time.time() + seconds

    def resume_dispatch(self) -> None:
        self._paused_until = 0.0

    def unsettled_sends(self) -> list[str]:
        """Jobs running here that a restart now would hold: no id a restart can follow yet."""
        left = []
        for job_id in list(self._running):
            row = self.store.get(job_id)
            if row and row.get("state") == "running" and not restart_can_follow(row):
                left.append(job_id)
        if self._render_service is not None:
            left.extend("render:" + job_id for job_id in self._render_service.unsettled())
        return left

    def drain(self, timeout: float, pause_s: float = float("inf")) -> list[str]:
        """Take no new work for `pause_s`, then wait up to `timeout` for every in-flight send to get
        an id a restart can follow (or to settle). Returns the job ids still unsettled.

        Queued jobs are not touched; a job claimed but not yet sent goes back to the queue (`_run`).
        A render already followable by its id is not waited for: the next start resumes it."""
        self.pause_dispatch(pause_s)
        deadline = time.time() + timeout
        while (left := self.unsettled_sends()) and time.time() < deadline:
            time.sleep(0.25)  # one PK read per running job per pass; light on the store lock
        return left

    # -- admission ---------------------------------------------------------
    def _site_lane_in_flight(self, provider_id: str, modality: str) -> int:
        """This gateway's own running jobs at **one site in one modality**.

        The provider counter was the only one admission had, and it is per *site*: a site
        running one image job refused a chat job while a second, free Mac sat there. A
        Mac runs one job at a time and the Hub's free count is per operation, so the two
        numbers that are compared have to be about the same thing.

        **Read without `_budget_lock`.** This is called from `fleet._attach_admission`,
        which runs inside the fan-out holding `fleet._refresh_lock`: taking the worker lock
        there is exactly the inversion the lock order note on `_budget_lock` forbids. One
        read of one int needs no lock — the lock is for moving the five books together.
        """
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
                ceiling = reading.get("ceiling")
                reach = fleet.reachable_machines(row)
                if ceiling is not None and reach is not None:
                    ceiling = min(ceiling, reach)  # offline Macs are not capacity (F3)
                free = reading.get("free")
                if free is None and not reading.get("unstated"):
                    free = ceiling
                return concurrency.hub_budget(
                    free,
                    operation=operation or modality,
                    stated=concurrency.stated_provider_limit(provider_id),
                    ceiling=ceiling,
                    reachable=row.get("reachable") is not False,
                )
        except Exception:  # noqa: BLE001 - an unreadable fleet is not a reason to stall
            pass
        return concurrency.cloud_budget(provider_id)

    def active(self) -> int:
        """Jobs in flight, from the published counter.

        **Never takes `_budget_lock`** — `/health` and `/v1/account/overview` read this on
        every poll, and a probe that waits on admission is a probe that reports an outage
        while the gateway is working (see the lock order note on `_budget_lock`).
        """
        total = self._active_total
        if self._render_service is not None:
            total += self._render_service.active()
        return total

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

    def _admission_reads(self, job: dict[str, Any], provider_id: str) -> dict[str, Any]:
        """Everything `_job_room` needs from the **fleet and the store**, read without `_budget_lock`.

        One place, so the rule has one place to be broken. `_room` reads the Hub's chat queue
        from a fleet snapshot, `_background_room` reads a location's idle Macs and the store's
        waiting shapes — all of it was previously done *inside* `_budget_lock` (from `_admit`
        and `_site_room`), and the fleet fan-out takes `_budget_lock` back through
        `_attach_admission` → `_site_lane_in_flight`. That is the 2026-10-19 hang; see the lock
        order note on `_budget_lock`.

        The two conditional reads are the ones a plain admission does not need: the chat queue
        allowance is only for a chat job on a location with no operator limit, and the
        background reads only for background work. `_background_reads` keeps the per-pass cache
        of `waiting_normal_shapes`, so the store is still asked once a pass rather than once a
        background job.

        `site` is the location's own row reduced to what admission needs: its per-model supply,
        whether it is reachable, and the row itself, so `fleet.publishes_model` can be asked about
        this job's model without a second fleet read, which the lock forbids.
        """
        modality = str(job.get("modality") or "")
        model_id = str(job.get("model_id") or "")
        site = self._site_facts(provider_id)
        reads: dict[str, Any] = {
            "budget": self._provider_budget(provider_id, modality, model_id),
            "supply": site["supply"],
            "pending": 0,
            "background": None,
            "site": site,
        }
        if modality == "chat" and concurrency.stated_provider_limit(provider_id) is None:
            reads["pending"] = int(studio_hub.chat_queue_wait_seconds(provider_id, model_id) > 0)
        if is_background(job) and provider_id.startswith(fleet.PROVIDER_PREFIX):
            reads["background"] = self._background_reads(provider_id)
        return reads

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
        # **No new work: the owner paused this location, or it is installing an update.** Refusing
        # is not failing — the job stays due and `_rebind` moves it to a location that is back.
        if _takes_no_new_work(provider_id):
            return False
        # Every fleet and store read this admission needs, taken **before** `_budget_lock`:
        # `_job_room` reaches `fleet.snapshot()` (and, for a background job, the store), and
        # the lock order note on `_budget_lock` makes that a deadlock, not a slow read.
        reads = self._admission_reads(job, provider_id)
        lane_budget = concurrency.lane_budget(modality)
        fleet_job = provider_id.startswith(fleet.PROVIDER_PREFIX) and modality in concurrency.FLEET_LANES
        with self._budget_lock:
            # **A fleet job's share of the pool is the fleet itself (D3).** The owner's `global`
            # bounds everything else; fleet work is bounded per group and model by `_room`, which
            # adds up to the fleet's own published capacity however many Macs join.
            if not fleet_job and self._not_fleet_in_flight() >= self._global_limit():
                return False
            # An automatic lane is the sum of the groups' own totals, so `_room` already holds it;
            # only an owner's number is a second, separate ceiling.
            if lane_budget.source != concurrency.SOURCE_FLEET \
                    and self._lane_in_flight.get(modality, 0) >= lane_budget.limit:
                return False
            if self._job_room(job, provider_id, reads) <= 0:
                return False
            self._book(job)
            self.budgets[provider_id] = reads["budget"].as_dict()
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
            model = (provider_id, modality, str(job.get("model_id") or ""))
            self._per_model[model] = self._per_model.get(model, 0) + 1
            if job.get("job_group"):
                self._per_group[job["job_group"]] = self._per_group.get(job["job_group"], 0) + 1
            now = time.monotonic()
            self._sent[key] = [t for t in self._sent.get(key, ()) if now - t < _JUST_SENT_S] + [now]
            # Republished under the lock and read without it (`active`). Derived from the book
            # itself rather than incremented, so it cannot drift away from it.
            self._active_total = sum(self._in_flight.values())

    def _site_room(self, job: dict[str, Any], provider_id: str,
                   claim: bool = False) -> tuple[int, int, int]:
        """`(room, ours, just_sent)` for this job's model and lane at one site (`fleet.apply_load`).

        `claim`: when there is room, move this running job's booking there in the same lock, so two
        re-routes in one burst cannot both take the last slot.
        """
        modality = str(job.get("modality") or "")
        # Fleet first, then the lock — see the lock order note on `_budget_lock`.
        reads = self._admission_reads(job, provider_id)
        with self._budget_lock:
            room = self._job_room(job, provider_id, reads)
            if claim and room > 0:
                self._move(job, provider_id)
            now = time.monotonic()
            sent = sum(now - t < _JUST_SENT_S for t in self._sent.get((provider_id, modality), ()))
            return room, self._per_lane.get((provider_id, modality), 0), sent

    def _not_fleet_in_flight(self) -> int:
        """Running jobs the owner's `global` bounds: everything but fleet-lane work at a location."""
        fleet_jobs = sum(n for (provider_id, modality), n in self._per_lane.items()
                         if provider_id.startswith(fleet.PROVIDER_PREFIX)
                         and modality in concurrency.FLEET_LANES)
        return sum(self._in_flight.values()) - fleet_jobs

    @staticmethod
    def _site_facts(provider_id: str) -> dict[str, Any]:
        """One location's row, reduced: `model -> usable Macs`, reachability, and the row itself.

        **One fleet read, three answers.** `_room` needs the per-model supply, admission now also
        needs to know whether the location is reachable and whether it states the model at all,
        and asking the snapshot three times per job is what the lock-order rule on `_budget_lock`
        exists to prevent. `supply` is `fleet.usable_slots` per model (models with no eligible Mac
        stay out, as before: they are bounded by the operation total, and the same reduction is
        what `routing_candidates` ranks by); `row` is the raw row so `fleet.publishes_model` can be
        asked about one particular model without another read.
        """
        facts: dict[str, Any] = {"supply": {}, "reachable": None, "row": None}
        if not provider_id.startswith(fleet.PROVIDER_PREFIX):
            return facts
        try:
            for row in fleet.snapshot(allow_stale=True).get("locations") or []:
                if str(row.get("provider_id") or "") == provider_id:
                    facts["row"] = row
                    facts["reachable"] = row.get("reachable") is not False
                    facts["supply"] = {
                        str(model): fleet.usable_slots(row, str(model), int(entry.get("eligible") or 0))
                        for model, entry in (row.get("model_supply") or {}).items()
                        if isinstance(entry, dict) and int(entry.get("eligible") or 0) > 0}
                    return facts
        except Exception:  # noqa: BLE001 - no supply reading falls back to the operation total
            pass
        return facts

    @staticmethod
    def _model_supply(provider_id: str) -> dict[str, int]:
        """`model -> eligible Macs` at one location (see `_site_facts`, which reads it once)."""
        return Worker._site_facts(provider_id)["supply"]

    def _room(self, provider_id: str, modality: str, model_id: str = "",
              budget: concurrency.Budget | None = None, supply: dict[str, int] | None = None,
              pending: int | None = None) -> int:
        """How many more jobs of this model this site would take now.

        The site's operation budget less our own jobs there — and, **fit-aware (D1)**, no more
        than the Macs that can run *this* model less our jobs there whose model fits no more
        Macs than this one does. Nested RAM tiers make that exact: a heavy job only a 16 GB Mac
        can run holds one of those Macs, and every lighter model can run on it too, so it counts
        against them; a light job does not count against the heavy one, because the heavy one
        cannot use the Mac it waits for. So heavy jobs stop filling a group's whole voice budget
        while its 8 GB Macs idle (sim: two Macs, two VoxCPM2 + one Qwen 0.6B — the Qwen job ran
        after the heavy ones; now beside them). A model the Hub publishes no supply for is
        bounded by the operation total alone, as before (`routing_candidates` falls back the same way).

        `budget`, `supply` and `pending` are the fleet readings this needs. A caller that
        already holds `_budget_lock` **must** pass all three: each `None` fallback below reads
        the fleet, and a fleet read under that lock is the inversion the lock order note on
        `_budget_lock` forbids. `_admission_reads` builds them; a caller with the lock released
        (a test, `_rebind`) may leave them out and they are read here.
        """
        if budget is None:
            budget = self._provider_budget(provider_id, modality, model_id)
        if supply is None:
            supply = self._model_supply(provider_id)
        if pending is None:
            pending = (int(studio_hub.chat_queue_wait_seconds(provider_id, model_id) > 0)
                       if modality == "chat" and concurrency.stated_provider_limit(provider_id) is None
                       else 0)
        with self._budget_lock:
            # The chat peer route executes on the controller's own Mac: one execution plus
            # one waiting request, across every model sharing that physical machine.
            ceiling = min(budget.limit, 1) + pending if pending else budget.limit
            room = ceiling - self._per_lane.get((provider_id, modality), 0)
            if model_id in supply:
                slots = supply[model_id]
                held = sum(n for (p, lane, model), n in self._per_model.items()
                           if p == provider_id and lane == modality and model in supply and supply[model] <= slots)
                room = min(room, slots + pending - held)
        return room

    def _job_room(self, job: dict[str, Any], provider_id: str,
                  reads: dict[str, Any] | None = None) -> int:
        """`_room` for this job at one site, and for a background job no more than `_background_room`.

        A model with a fleet-wide cap (`concurrency.model_fleet_cap`, YuE2) gets no more than the room
        left under it, this job's own booking excluded when it is already running and re-routing. Its
        priority is the caller's (0.2.171, owner 2026-10-12: images are not the priority, so a sung song
        need not wait behind them; the cap alone bounds how many Macs sing).

        `reads` is `_admission_reads`'s bundle. A caller holding `_budget_lock` must pass one; a
        caller that is not (`_rebind`) gets it built here, where the fleet may be read.

        **A site that cannot run this model gets no room for it, whatever the budget says.** The
        operation budget is a count of slots, not of Macs that have the model: at a location whose
        `model_supply` leaves the model out (or gives it no eligible Mac) the budget still answered
        "yes", so a job stayed bound there and replayed "hub submit failed: timed out" until its
        queue ceiling. An unreachable location is the same answer for the same reason (`fleet.
        routing_candidates` never offers one), and both refusals let `_rebind` look elsewhere.
        Unstated supply stays unknown and changes nothing (§6: a zero that means "not stated" must
        not read as "none").

        **A pool job is exempt, and that is not a hole.** Its `model_id` is the member it is bound
        to, while `fleet.pool_candidates` picks the next site through **any** member of the pool, so
        asking this site about that one member would refuse a site that is a correct answer through
        another.
        """
        if reads is None:
            reads = self._admission_reads(job, provider_id)
        modality = str(job.get("modality") or "")
        model_id = str(job.get("model_id") or "")
        room = self._room(provider_id, modality, model_id,
                          reads["budget"], reads["supply"], reads["pending"])
        fleet_site = provider_id.startswith(fleet.PROVIDER_PREFIX)
        if room > 0 and fleet_site and not _job_params(job).get("_pool_id"):
            site = reads.get("site") or {}
            if site.get("reachable") is False:
                return 0
            if site.get("row") is not None and fleet.publishes_model(site["row"], model_id) is False:
                return 0
        cap = concurrency.model_fleet_cap(model_id) if fleet_site else None
        if room > 0 and cap is not None:
            with self._budget_lock:
                running = sum(n for (p, _lane, m), n in self._per_model.items()
                              if m == model_id and p.startswith(fleet.PROVIDER_PREFIX))
            room = min(room, cap - running + int(job.get("id") in self._running))
        if room > 0 and fleet_site and is_background(job):
            room = min(room, self._background_room(provider_id, model_id, modality,
                                                   reads["background"]))
        return room

    def _background_reads(self, provider_id: str) -> dict[str, Any]:
        """What `_background_room` needs from the fleet and the store, read without `_budget_lock`.

        `waiting_normal_shapes` is the one store query admission makes, and it stays cached for
        the pass (`_normal_waiting`, cleared in `_loop`), so it is still asked once a pass.
        """
        row = next((r for r in fleet.snapshot(allow_stale=True).get("locations") or []
                    if str(r.get("provider_id") or "") == provider_id), None) or {}
        supply = row.get("model_supply") or {}
        if self._normal_waiting is None:
            self._normal_waiting = getattr(self.store, "waiting_normal_shapes", lambda: set())()
        return {
            "idle": {str(model): int(entry.get("available") or 0)
                     for model, entry in supply.items() if isinstance(entry, dict)},
            "normal_waiting": self._normal_waiting,
        }

    def _background_room(self, provider_id: str, model_id: str, modality: str = "",
                         reads: dict[str, Any] | None = None) -> int:
        """Macs at this site a background job may take now: idle ones no normal job is waiting for.

        **Only an idle Mac, never the Hub's queue.** A background job sent to a site with no free Mac
        would wait in the Hub's FIFO queue and be taken ahead of normal work sent after it, so the
        bound is the Hub's own count of idle Macs for this model (`model_supply[model].available`),
        less our sends there the snapshot may not show yet. A site that does not state it gets none.

        **Production first, in the lane it competes in.** While a normal job waits (due or deferred)
        in **this job's own lane** for a model this site has an idle Mac for, that Mac is the normal
        job's: no background job of that lane is sent here. Background work already running is never
        stopped; it finishes and the Mac takes normal work next.

        **The lane is the bound, and it was the whole site** (measured live, 2026-10-29). One
        wait shape of any lane used to hold every background job at every site: seven normal
        Breeze-TTS jobs stranded on an unreachable site that did not publish Breeze made all six
        image sites refuse **132 background image jobs for 3.5 hours** while their image Macs sat
        idle, because each site also lists Breeze. Capacity is published per model and per lane
        (`model_supply[model]`, `capacity.by_operation`), so a Mac that is idle for a model in this
        lane is what a job in this lane competes for; a waiting narration job does not reserve an
        image Mac, because it cannot run there.

        ponytail: idle Macs from a snapshot up to one refresh old, less every send in `_JUST_SENT_S`
        (any lane) — it under-fills for a few seconds after a burst rather than over-fill.

        `reads` is `_background_reads`'s bundle, and a caller holding `_budget_lock` must pass it:
        the fleet row and the store query are the reads this used to do *under* that lock, which
        is the inversion the lock order note on `_budget_lock` forbids.
        """
        if reads is None:
            reads = self._background_reads(provider_id)
        idle: dict[str, int] = reads["idle"]
        held = {str(model) for model, lane in reads["normal_waiting"]
                if not modality or str(lane) == modality}
        if any(idle.get(model, 0) > 0 for model in held):
            return 0
        now = time.monotonic()
        with self._budget_lock:
            sent = sum(now - t < _JUST_SENT_S for (pid, _lane), times in self._sent.items()
                       if pid == provider_id for t in times)
        return idle.get(model_id, 0) - sent

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
        backoff = self._rebind_backoff.get(job["id"])
        if backoff is not None and time.monotonic() < backoff[0]:
            return False
        try:
            # A site with room keeps its job, unless the owner paused it or it is installing.
            model_id = str(job.get("model_id") or "")
            if self._job_room(job, provider_id) > 0 and not _takes_no_new_work(provider_id):
                return False
            if fleet.pinned_location(json.loads(job.get("params") or "{}")):
                return False
            tried = {str(fleet.controller_id_from(provider_id))}
            for value in getattr(self.store, "attempted_locations", lambda _id: [])(job["id"]):
                tried.add(str(fleet.controller_id_from(str(value)) or value))
            pool_members = _pool_members_for(job, modality)
            # A rebind is routing too: a reference job stays off a location whose worker never
            # advertised `edit`, however much room that location has.
            capability = fleet.required_capability(modality, _job_params(job))
            if pool_members is not None:
                candidates = fleet.apply_load(
                    fleet.pool_candidates(pool_members, modality, exclude_locations=tried,
                                          require_capability=capability),
                    lambda pid: self._site_room(job, pid))
                ready = [c for c in candidates if c["room"] > 0]
                if not ready:
                    self._nowhere.add((model_id, modality))
                    return False
                best = min(ready, key=lambda c: fleet.rank_candidate(c, str(job["id"])))
                if best["provider_id"] == provider_id or not rebind(
                        job["id"], from_provider=provider_id, to_provider=best["provider_id"],
                        model_id=best["_member"]):
                    return False
                self._rebind_backoff.pop(job["id"], None)
                job["provider_id"] = best["provider_id"]
                job["model_id"] = best["_member"]
                return True
            if fleet.speed_ranked(model_id):
                # Qwen (owner 2026-10-09): stay in line for a fast Mac unless a site with room would
                # finish it sooner. This site is ranked too, with our jobs already waiting for it.
                # A paused site is not a line to stay in, and neither is one installing.
                here = "" if _takes_no_new_work(provider_id) else str(fleet.controller_id_from(provider_id))
                ahead = self._ahead.get((provider_id, model_id), 0)
                pool = fleet.apply_load(
                    fleet.routing_candidates(model_id, modality, exclude_locations=tried - {here},
                                             require_capability=capability),
                    lambda pid: self._site_room(job, pid))
                best = min(pool, default=None, key=lambda c: fleet.rank_candidate(
                    c, str(job["id"]), ahead if c["provider_id"] == provider_id else 0))
                if (best is None or best["provider_id"] == provider_id or best["room"] <= 0
                        or not rebind(job["id"], from_provider=provider_id,
                                      to_provider=best["provider_id"])):
                    return False
                self._rebind_backoff.pop(job["id"], None)
                job["provider_id"] = best["provider_id"]
                return True
            candidates = [
                c for c in fleet.apply_load(
                    fleet.routing_candidates(model_id, modality, exclude_locations=tried,
                                             require_capability=capability),
                    lambda pid: self._site_room(job, pid))
                if c["room"] > 0
            ]
            if not candidates:
                self._nowhere.add(shape)
                return False
            best = min(candidates, key=lambda c: fleet.rank_candidate(c, str(job["id"])))
            if not rebind(job["id"], from_provider=provider_id, to_provider=best["provider_id"]):
                return False
            self._rebind_backoff.pop(job["id"], None)
            job["provider_id"] = best["provider_id"]
            return True
        except Exception as exc:  # noqa: BLE001 - a failed rebind leaves the job where it is
            # Backed off per job, doubling to a cap, and logged once per job rather than every pass.
            delay = min(backoff[1] * 2, _REBIND_BACKOFF_CAP_S) if backoff else _REBIND_BACKOFF_S
            self._rebind_backoff[job["id"]] = (time.monotonic() + delay, delay)
            if backoff is None:
                log.warning("late rebind of %s failed: %s; retrying with backoff up to %.0fs "
                            "(logged once)", job.get("id"), exc, _REBIND_BACKOFF_CAP_S, exc_info=True)
            return False

    def _release(self, job: dict[str, Any]) -> None:
        provider_id = str(job.get("provider_id") or "")
        modality = str(job.get("modality") or "")
        with self._budget_lock:
            for book, key in ((self._in_flight, provider_id),
                              (self._lane_in_flight, modality),
                              (self._per_lane, (provider_id, modality)),
                              (self._per_model, (provider_id, modality, str(job.get("model_id") or ""))),
                              (self._per_group, job.get("job_group"))):
                if book.get(key):
                    book[key] -= 1
                if book.get(key, 0) <= 0:
                    book.pop(key, None)
            # Republished under the lock and read without it (`active`); see `_book`.
            self._active_total = sum(self._in_flight.values())

    def _move(self, job: dict[str, Any], provider_id: str) -> None:
        """A re-route sent this running job to another location: its booking goes with it (F2).

        It stayed on the site that refused the job for the rest of the job's life, so that site read
        one Mac fuller than it was and the one now running it one emptier (10-05: 102 Breeze re-routes,
        ~500 s each)."""
        with self._budget_lock:
            self._release(job)
            job["provider_id"] = provider_id
            self._book(job)

    # -- running -----------------------------------------------------------
    def _run(self, job: dict[str, Any], resume: bool = False) -> None:
        """One job on its own thread. Never raises."""
        self._running[job["id"]] = job
        try:
            if not resume and self.dispatch_paused() and self.store.unclaim(job["id"]):
                return  # a drain began after the claim and before any send: back to the queue
            _MOVE_HOOKS[job["id"]] = lambda moved: self._move(job, moved)
            _ROOM_HOOKS[job["id"]] = lambda pid, claim=False: self._site_room(job, pid, claim)
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
            _MOVE_HOOKS.pop(job["id"], None)
            _ROOM_HOOKS.pop(job["id"], None)
            self._release(job)
            self._running.pop(job["id"], None)

    def _loop(self) -> None:
        while not self._stop.is_set():
            # **One pass, and one raise is not allowed to end dispatch for ever.** The loop
            # guarded `due()` and nothing else: a raise from `_admit`, `mark_running` or the
            # thread spawn escaped the `while`, the thread died, and every later job sat in
            # `accepted` for ever while `/health` still said ok — there was no liveness field to
            # say otherwise (audit M6). The pass is wrapped whole and the count of consecutive
            # failures is published, so an operator can tell "idle" from "wedged".
            self._nowhere.clear()
            self._ahead.clear()
            self._normal_waiting = None
            try:
                with self._budget_lock:
                    groups = dict(self._per_group)
                background: dict[str, int] = {}
                for running in list(self._running.values()):
                    if is_background(running):
                        background[fair_key(running)] = background.get(fair_key(running), 0) + 1
                # A drain takes no new work; queued jobs stay queued, untouched.
                due = [] if self.dispatch_paused() else scarcest_first(self.store.due(), groups, background)
            except Exception:  # noqa: BLE001
                log.exception("worker could not read due jobs")
                due = []

            try:
                if self._render_service is not None and not self.dispatch_paused():
                    self._render_service.dispatch_pass()
                for job in due:
                    if self._stop.is_set():
                        return
                    if self._expired_in_queue(job) or self._stranded_on_paused(job):
                        continue
                    # Reserve first, then claim: a claim that fails must not leave the
                    # budget holding a slot nobody will ever release.
                    if not self._admit(job) and not (self._rebind(job) and self._admit(job)):
                        key = (str(job.get("provider_id") or ""), str(job.get("model_id") or ""))
                        self._ahead[key] = self._ahead.get(key, 0) + 1
                        continue
                    self._rebind_backoff.pop(job["id"], None)
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

            # **The superseded-batch cleanup has its own clock and thread** (0.2.211). Both
            # sweeps are network-bound; sharing one thread queued one behind the other, and a
            # slow site could make a hold wait behind a duplicate's read. Separate events mean a
            # slow cleanup does not delay a hold, or the next dispatch pass.
            if (time.time() - self._last_superseded_sweep >= _SUPERSEDED_SWEEP_S
                    and not self._superseded_sweeping.is_set()):
                self._last_superseded_sweep = time.time()
                self._superseded_sweeping.set()
                threading.Thread(target=self._sweep_superseded,
                                 name="gateway-superseded-sweep", daemon=True).start()

            # **A running job that has said nothing gets asked** (`_check_running_liveness`). It is
            # row reads only, on this thread, so it cannot park dispatch behind a network call --
            # and it is what makes a pool wait or a wedged call visible instead of silent.
            if time.time() - self._last_liveness >= _LIVENESS_SWEEP_S:
                self._last_liveness = time.time()
                try:
                    self._check_running_liveness()
                except Exception:  # noqa: BLE001 - the watchdog never ends dispatch
                    log.exception("running-job liveness sweep failed")

            # **When the last pass finished, successful or not.** `worker_alive` alone is a
            # boolean an operator cannot date; this is the number that says whether dispatch is
            # moving. Written every pass, so a stalled loop shows a stale timestamp rather than a
            # green light.
            self.last_pass_at = time.time()

            self._stop.wait(self.interval_s)

    def _check_running_liveness(self, now: float | None = None) -> dict[str, int]:
        """**A running job that says nothing is indistinguishable from a healthy one.**

        `liveness()` says whether the *dispatcher* is alive; nothing said whether the *work* was.
        Measured live, 2026-10-29: jobs sat `running` with no `upstream_job_id` and no progress for
        hours — a pool job waiting for a member slot with no deadline, a call that never answered —
        and every operator-visible field read like a job that was simply working.

        Two thresholds, both honest:

          * past `liveness.waiting_after_seconds` (two minutes by default) a job with **no upstream
            id** and no published reason publishes one (`waiting.reason`), so Story and the console
            can say "waiting for <location> to accept it" instead of showing a silent row. The reason
            clears itself when the adapter reports real waiting, or `_dispatch` starts a new attempt.
          * past the **lane's own call ceiling** (`lane_deadline_s`: the number that call was given,
            plus a grace) the call is wedged rather than busy: the job is failed **retryable**,
            naming how long it waited and what was not seen, rather than left silent for ever. A
            late answer is still kept — `store.succeed` records it as `late_result_state` beside
            the verdict rather than rewriting it.

        Never raises: a job that cannot be read is looked at again next sweep.
        """
        moment = time.time() if now is None else float(now)
        waiting_after = float(concurrency.liveness_budget()["waiting_after_seconds"])
        out = {"checked": 0, "said_something": 0, "failed": 0}
        for job_id, job in list(self._running.items()):
            try:
                row = self.store.get(job_id) if hasattr(self.store, "get") else None
                if not row or row.get("state") != "running" or row.get("upstream_job_id"):
                    continue
                out["checked"] += 1
                started = _silent_since(row)
                if started is None:
                    continue
                age = moment - started
                modality = str(row.get("modality") or job.get("modality") or "")
                where = _where(row, job)
                if age > lane_deadline_s(modality):
                    out["failed"] += 1
                    _fail(
                        self.store, job_id, code="PROVIDER_TIMEOUT", retryable=True,
                        detail=(f"{where} did not answer within {int(age // 60)} min and never "
                                f"reported a job id, so the wait was ended rather than left "
                                f"silent. The Gateway could not tell whether the provider took "
                                f"it, so any late answer is kept beside this failure rather than "
                                f"overwriting it; run it again if nothing arrives."),
                    )
                    continue
                if age <= waiting_after or row.get("waiting"):
                    continue
                record_waiting = getattr(self.store, "record_waiting", None)
                if record_waiting is not None:
                    record_waiting(job_id, {
                        "reason": f"waiting for {where} to accept the job - no job id yet "
                                  f"({int(age // 60)} min)"
                                  if age >= 60 else
                                  f"waiting for {where} to accept the job - no job id yet",
                        "since": started,
                    })
                    out["said_something"] += 1
            except Exception:  # noqa: BLE001 - a watchdog must never end the dispatch loop
                log.debug("liveness: could not read %s", job_id, exc_info=True)
        self.liveness_sweep = out
        return out

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
        # **The editable ceiling, not the adapter's private constant.** This read
        # `studio_hub.QUEUE_WAIT_CEILING_S` — a module number in the adapter — while the budget
        # beside it was a setting the owner edits, so the owner's number could be overruled in
        # silence. 146 jobs failed `QUEUE_TIMEOUT` in the seven days to 2026-10-19 through it.
        ceiling = float(concurrency.queue_wait_budget()["wait_ceiling_seconds"])
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

    def _sweep_superseded(self) -> None:
        """The 0.2.211 cleanup's own thread; never raises, never blocks the hold sweep."""
        try:
            self.superseded_sweep = reconcile_superseded(self.store)
        except Exception:  # noqa: BLE001 - a cleanup must never stop dispatch or holds
            log.exception("superseded-batch reconciliation failed")
        finally:
            self._superseded_sweeping.clear()

    def liveness(self) -> dict[str, Any]:
        """Whether dispatch is actually moving, in the three fields `/health` publishes.

        `alive` is the thread's own state, `last_pass_at` dates the last attempt, and
        `consecutive_failures` counts passes that raised since the last clean one. A boolean
        alone is what let a dead dispatcher read as healthy.
        """
        thread = self._thread
        render = self._render_service
        return {
            "worker_alive": bool(thread is not None and thread.is_alive()),
            "last_pass_at": self.last_pass_at,
            "consecutive_failures": self.pass_failures,
            "in_flight": self.active(),
            "dispatch_paused": self.dispatch_paused(),
            "hold_sweep": dict(self.hold_sweep),
            "superseded_sweep": dict(self.superseded_sweep),
            # What the running-job watchdog last saw: how many silent jobs it asked, how many it
            # told, and how many it had to end. Zeroes mean "nothing running", not "not checked".
            "running_sweep": dict(self.liveness_sweep),
            "render_in_flight": render.active() if render is not None else 0,
            "render_unsettled": render.unsettled() if render is not None else [],
        }
