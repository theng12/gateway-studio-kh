"""Hub batches still running at a site for Gateway work that has already finished.

**The incident, 2026-10-29.** A restart on site 1200 made the Gateway lose contact with eight Hub
batches. The Gateway marked those attempts `PROVIDER_SUBMISSION_UNCERTAIN`, re-dispatched each one
elsewhere under a **new upstream batch id**, and all eight Gateway jobs succeeded — but three of the
**old** batches kept running on 1200 doing duplicate work, and only a person noticed. `upstream_job_id`
holds only the newest id; the old ones survive in the job's `attempt_log` prose.

This module answers one question, from data the Gateway already has, with **no new polling**:
*for each batch a site reports running that this Gateway labelled, does a Gateway job still own it?*
It runs beside `fleet_history` on the Fleet read, over the same fan-out.

Three answers, and they must never collapse into two:

* `live` — its Gateway job is non-terminal and this **is** the current attempt. Not an orphan.
* `orphan_superseded` — its Gateway job moved on to another batch, or finished, or failed, or was
  cancelled. The work it is doing is duplicate work.
* `orphan_unknown` — **no** Gateway job references this batch at all. A person looks at this one:
  the Gateway cannot prove the work was kept, so it is never stoppable from here and never stopped
  automatically (`orphan_unknown` is the one class auto-stop must not touch).

**A failed read is not an empty fleet.** A controller that timed out has `running_jobs: []`, which
reads exactly like a controller with nothing running. Its list is `None` ("not checked"), never
`[]`.

**Only this Gateway's batches.** A batch is ours when its Hub `label` is `gateway-kh` — the label
`adapters/studio_hub._submit` sets on every batch it creates. Anything else a Hub runs (a local
Studio Hub user's own job, another caller's work) is invisible here and is never stopped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .store import TERMINAL_STATES, artifacts_of

log = logging.getLogger("gateway.orphans")

#: The label every batch this Gateway submits carries (`adapters/studio_hub._submit`).
GATEWAY_LABEL = "gateway-kh"

#: The owner's switch, beside the history rather than in `.env` (the menu bar app owns that file).
SETTINGS_PATH = Path(os.environ.get("GATEWAY_ORPHANS_SETTINGS") or (DATA_DIR / "fleet_orphans.json"))

#: Auto-stop is a deliberate switch and it is **off** unless the owner turned it on.
AUTO_STOP_DEFAULT = False

#: How long one batch may go between automatic stop requests. The Hub answers "signalled, not yet
#: stopped", so the batch keeps appearing on the next read; without this the Gateway would ask
#: again on every fan-out.
AUTO_STOP_RETRY_S = 600.0

#: Extended identifiers may be surrounded by punctuation in an attempt log's sentence; a bare
#: substring match would let id `abc` claim `xabc` (the "match what it ENDS with" lesson).
_WORD = re.compile(r"[A-Za-z0-9]")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings() -> dict:
    """`{"auto_stop": bool, "default": False}` — read fresh, so a change shows on the next read."""
    return {"auto_stop": _read_auto_stop(), "default": AUTO_STOP_DEFAULT}


def _read_auto_stop() -> bool:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return AUTO_STOP_DEFAULT
    stated = raw.get("auto_stop") if isinstance(raw, dict) else None
    # Only a real boolean turns it on. A string `"false"` is not a deliberate yes.
    return stated is True


def set_settings(payload: object) -> dict:
    """Replace the switch. Raises `ValueError` with a sentence rather than coercing a value."""
    if not isinstance(payload, dict):
        raise ValueError("Send an object with 'auto_stop'.")
    value = payload.get("auto_stop")
    if not isinstance(value, bool):
        raise ValueError("'auto_stop' must be true or false.")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    scratch.write_text(json.dumps({"auto_stop": value}, indent=2), encoding="utf-8")
    os.replace(scratch, SETTINGS_PATH)
    return settings()


# ---------------------------------------------------------------------------
# Detection — the whole of it, pure, so the read and the auto-stop agree
# ---------------------------------------------------------------------------


def analyse(locations: list[dict], *, store: Any = None, now: float | None = None) -> dict:
    """Classify every Gateway-labelled batch these location rows report running.

    Pure: the caller's rows are not modified. `admin.fleet_snapshot` copies them before writing the
    answer, because this read rides the same cached snapshot routing and admission read.

    Returns `{"by_location": {id: {"checked", "note", "orphans"}}, "count", "checked_sites",
    "unchecked_sites"}`.
    """
    now = time.time() if now is None else now
    locations = [row for row in locations if isinstance(row, dict)]
    checked = [row for row in locations if _checked(row)]
    wanted: list[str] = []
    for row in checked:
        for batch in _our_batches(row):
            wanted.append(_batch_id(batch))
    jobs = _jobs_for(store, wanted) if wanted else []

    by_location: dict[str, dict] = {}
    total = 0
    for row in locations:
        location_id = str(row.get("id") or "")
        if not _checked(row):
            by_location[location_id] = {
                "checked": False, "note": _not_checked_note(row), "orphans": [],
            }
            continue
        orphans = []
        for batch in _our_batches(row):
            entry = classify(batch, jobs, now=now)
            if entry is not None:
                orphans.append(entry)
        orphans.sort(key=lambda item: item.get("started_at") or 0)
        by_location[location_id] = {"checked": True, "note": None, "orphans": orphans}
        total += len(orphans)
    return {
        "by_location": by_location,
        "count": total,
        "checked_sites": sum(1 for value in by_location.values() if value["checked"]),
        "unchecked_sites": sum(1 for value in by_location.values() if not value["checked"]),
    }


def annotate(snapshot: dict, *, store: Any = None, now: float | None = None) -> dict:
    """Write `orphan_batches` / `orphans_checked` / `orphans_note` onto the Fleet read.

    Called on **copies** of the location rows (`admin.fleet_snapshot`), after `fleet_history.annotate`,
    so nothing settings-dependent reaches the cached snapshot routing reads.
    """
    locations = [row for row in (snapshot.get("locations") or []) if isinstance(row, dict)]
    result = analyse(locations, store=store, now=now)
    for row in locations:
        state = result["by_location"].get(str(row.get("id") or ""))
        if state is None:
            continue
        row["orphans_checked"] = state["checked"]
        # `None`, never `[]`, when the site was not read: an empty list is a claim this read
        # cannot make.
        row["orphan_batches"] = state["orphans"] if state["checked"] else None
        row["orphans_note"] = state["note"]
    snapshot["orphan_batches"] = result["count"]
    snapshot["orphans_checked_sites"] = result["checked_sites"]
    snapshot["orphans_unchecked_sites"] = result["unchecked_sites"]
    snapshot["orphans_auto_stop"] = settings()["auto_stop"]
    return result


def _checked(row: dict) -> bool:
    """Whether the in-flight list was actually read from this location."""
    if row.get("reachable") is not True:
        return False
    # Absent means "this row predates the flag" (a fixture, an older cached row); reachable rows
    # from `_probe` state it. `False` is a failed jobs read, which is not an empty list.
    return row.get("jobs_read") is not False


def _not_checked_note(row: dict) -> str:
    error = str(row.get("error") or "").strip()
    if row.get("reachable") is not True:
        return "not checked: the site did not answer" + (f" — {error}" if error else ".")
    return "not checked: the site answered, but its running-jobs read failed."


def _our_batches(row: dict) -> list[dict]:
    """The rows of this location's in-flight list that this Gateway submitted."""
    out = []
    for batch in row.get("running_jobs") or []:
        if not isinstance(batch, dict):
            continue
        if str(batch.get("label") or "").strip() != GATEWAY_LABEL:
            continue
        if not _batch_id(batch):
            continue
        out.append(batch)
    return out


def _batch_id(batch: dict) -> str:
    return str(batch.get("id") or "").strip()


def _jobs_for(store: Any, batch_ids: list[str]) -> list[dict]:
    if not batch_ids:
        return []
    if store is None:
        from .runtime import store as runtime_store

        store = runtime_store
    reader = getattr(store, "jobs_for_upstream_batches", None)
    if reader is None:  # a small test double that predates this read
        return []
    return [job for job in reader(batch_ids) if isinstance(job, dict)]


def classify(batch: dict, jobs: list[dict], *, now: float) -> dict | None:
    """One batch's answer, or `None` when it is `live` and therefore not an orphan."""
    batch_id = _batch_id(batch)
    matches = [job for job in jobs if _job_references(job, batch_id)]
    if not matches:
        return _entry(batch, now, "orphan_unknown", None)
    job = _best_match(matches, batch_id)
    current = str(job.get("upstream_job_id") or "").strip() == batch_id
    if current and str(job.get("state") or "") not in TERMINAL_STATES:
        return None
    return _entry(batch, now, "orphan_superseded", job)


def _job_references(job: dict, batch_id: str) -> bool:
    """Whether this job's record names the batch: as its current attempt, or in its attempt log.

    The attempt log stores a **superseded** id inside a sentence (`"lost contact with … while
    polling batch <id> …"`). Matching is on a whole token, so an id that merely sits inside a
    longer one does not claim it.
    """
    if str(job.get("upstream_job_id") or "").strip() == batch_id:
        return True
    return any(_token_in(str(entry.get("detail") or ""), batch_id) for entry in _attempts(job))


def _attempts(job: dict) -> list[dict]:
    raw = job.get("attempt_log")
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)] if isinstance(parsed, list) else []


def _token_in(text: str, token: str) -> bool:
    start = 0
    while True:
        at = text.find(token, start)
        if at < 0:
            return False
        before = text[at - 1] if at else ""
        after = text[at + len(token)] if at + len(token) < len(text) else ""
        if not (before and _WORD.match(before)) and not (after and _WORD.match(after)):
            return True
        start = at + 1


def _best_match(jobs: list[dict], batch_id: str) -> dict:
    """The job that owns this batch now, else the newest one that ever named it."""
    def rank(job: dict) -> tuple:
        current = str(job.get("upstream_job_id") or "").strip() == batch_id
        terminal = str(job.get("state") or "") in TERMINAL_STATES
        return (0 if current else 1, 0 if not terminal else 1, -float(job.get("updated_at") or 0))

    return sorted(jobs, key=rank)[0]


# ---------------------------------------------------------------------------
# What the page shows, and the guard in front of Stop
# ---------------------------------------------------------------------------


def _entry(batch: dict, now: float, classification: str, job: dict | None) -> dict:
    started = _number(batch.get("started_at"))
    state = _artifact_state(job) if job is not None else None
    return {
        "batch_id": _batch_id(batch),
        "label": batch.get("label"),
        "modality": batch.get("modality"),
        "model": batch.get("model"),
        "model_id": batch.get("model_id"),
        "started_at": started,
        "running_seconds": round(max(0.0, now - started), 1) if started is not None else None,
        # Every figure the Hub did not send stays `None`, so the page can print "not reported"
        # rather than a zero nobody measured.
        "progress": {
            "done": _count(batch.get("done")),
            "total": _count(batch.get("total")),
            "queued": _count(batch.get("queued")),
            "running": _count(batch.get("running")),
            "retrying": _count(batch.get("retrying")),
            "artifact_count": _count(batch.get("artifact_count")),
            "output_path": _text(batch.get("output_path")),
        },
        "classification": classification,
        "job": _job_view(job, state, _batch_id(batch)),
        "stop": _stop_guard(classification, job, state),
    }


def _job_view(job: dict | None, state: dict | None, batch_id: str) -> dict | None:
    if job is None:
        return None
    job_state = str(job.get("state") or "")
    current = _text(job.get("upstream_job_id"))
    return {
        "id": job.get("id"),
        "state": job_state,
        "outcome": job_state if job_state in TERMINAL_STATES else "still running",
        # The attempt that replaced this batch. `None` when the job never moved on (it finished
        # or failed on this very batch), so the page does not print "replaced by" itself.
        "superseded_by": current if current and current != batch_id else None,
        "provider_id": job.get("provider_id"),
        "modality": job.get("modality"),
        "model_id": job.get("model_id"),
        "error_code": job.get("error_code"),
        "error_detail": _text(job.get("error_detail")),
        "artifacts": state["count"] if state else None,
        "artifacts_on_disk": state["present"] if state else None,
        "artifacts_complete": state["complete"] if state else None,
    }


def _artifact_state(job: dict) -> dict:
    """What a settled job actually kept, checked on disk rather than taken on trust."""
    rows = artifacts_of(job)
    present = 0
    for row in rows:
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        try:
            if Path(path).is_file() and Path(path).stat().st_size > 0:
                present += 1
        except OSError:
            continue
    return {"count": len(rows), "present": present,
            "complete": bool(rows) and present == len(rows)}


def _stop_guard(classification: str, job: dict | None, state: dict | None) -> dict:
    """Whether Stop may be offered — and why not, in words the person reading it can act on.

    The guard is the *job's own evidence*, never a hunch: either the Gateway job succeeded and its
    artifacts are on disk and non-empty, or it is failed/cancelled with nothing left to collect.
    `orphan_unknown` has no Gateway job to ask, so it is never offered a Stop at all.
    """
    if classification == "orphan_unknown":
        return {
            "allowed": False,
            "reason": "No Gateway job names this batch, so the Gateway cannot prove its work was "
                      "kept. Check it on that site's Hub.",
        }
    if job is None or state is None:
        return {"allowed": False, "reason": "No Gateway job to check."}
    job_state = str(job.get("state") or "")
    if job_state == "succeeded":
        if state["count"] == 0:
            return {"allowed": False,
                    "reason": "Its Gateway job succeeded but kept no artifact, so stopping the "
                              "batch could throw away the only copy."}
        if not state["complete"]:
            missing = state["count"] - state["present"]
            return {"allowed": False,
                    "reason": f"{missing} of its {state['count']} artifact(s) are missing or empty "
                              "on disk, so stopping the batch could throw away the only copy."}
        return {"allowed": True,
                "reason": f"Its Gateway job succeeded and all {state['count']} artifact(s) are on "
                          "disk."}
    if job_state in ("failed", "cancelled"):
        if state["present"] > 0:
            return {"allowed": False,
                    "reason": f"Its Gateway job {job_state}, but {state['present']} artifact(s) are "
                              "still on disk — collect them first."}
        return {"allowed": True,
                "reason": f"Its Gateway job {job_state} and there is nothing left to collect."}
    return {"allowed": False,
            "reason": f"Its Gateway job is still {job_state or 'running'} — the work is not "
                      "finished, so the old batch must not be stopped."}


def stop_allowed(job: dict | None) -> dict:
    """**The orphan Stop guard, for a job this Gateway already settled.**

    The same rule the Fleet Stop button and the automatic stop use, exposed for the one other
    caller that stops a batch without a person: cleaning up an attempt this Gateway superseded
    itself (0.2.211, `worker.reconcile_superseded`). It is public and shared rather than
    re-implemented there, so the automatic cleanup can never hold a weaker rule than a click.
    """
    if job is None:
        return {"allowed": False, "reason": "No Gateway job to check."}
    return _stop_guard("orphan_superseded", job, _artifact_state(job))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


# ---------------------------------------------------------------------------
# Automatic stop — the same guard, one deliberate switch, never `orphan_unknown`
# ---------------------------------------------------------------------------

_last_auto_stop: dict[str, float] = {}


def maybe_auto_stop(snapshot: dict, *, store: Any = None, now: float | None = None) -> list[dict]:
    """Stop the orphans the guard allows, when the owner switched auto-stop on.

    Off by default. Every stop is written to the startup audit log with what was proven first.
    `orphan_unknown` is filtered out here as well as in the guard: a second door, deliberately, so
    a future caller cannot re-open it by changing one branch.
    """
    if not settings()["auto_stop"]:
        return []
    now = time.time() if now is None else now
    result = analyse([row for row in (snapshot.get("locations") or []) if isinstance(row, dict)],
                     store=store, now=now)
    stopped: list[dict] = []
    for row in snapshot.get("locations") or []:
        if not isinstance(row, dict) or not result["by_location"].get(str(row.get("id")), {}).get("checked"):
            continue
        for entry in result["by_location"][str(row.get("id"))]["orphans"]:
            if entry["classification"] != "orphan_superseded":
                continue  # never `orphan_unknown`: a person looks at that one
            if not entry["stop"]["allowed"]:
                continue
            key = f"{row.get('id')}/{entry['batch_id']}"
            if now - _last_auto_stop.get(key, 0.0) < AUTO_STOP_RETRY_S:
                continue
            _last_auto_stop[key] = now
            try:
                answer = _cancel(row, entry["batch_id"], entry["modality"])
            except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest
                log.warning("orphan auto-stop for batch %s failed: %s", entry["batch_id"], exc)
                _audit("orphan_stop_refused", controller=row.get("id"), batch=entry["batch_id"],
                       modality=entry["modality"], detail=str(exc)[:300])
                continue
            _audit("orphan_stop", controller=row.get("id"), batch=entry["batch_id"],
                   modality=entry["modality"], result=answer, requested_by="gateway-auto",
                   reason=entry["stop"]["reason"], job=entry["job"]["id"] if entry["job"] else None)
            stopped.append({"location": row.get("id"), "batch_id": entry["batch_id"],
                            "result": answer})
    _prune_auto_stop(now)
    return stopped


def _cancel(location: dict, batch_id: str, modality: str | None) -> dict:
    """One Hub cancel, through the same path the Fleet Stop button uses."""
    from . import admin  # late: admin imports this module

    return admin.cancel_fleet_batch(location, batch_id, str(modality or "").strip())


def _audit(event: str, **fields: Any) -> None:
    try:
        from . import startup_control

        startup_control.audit(event, **fields)
    except Exception:  # noqa: BLE001 - an audit that cannot be written is logged there
        log.warning("orphan audit could not be written", exc_info=True)


def _prune_auto_stop(now: float) -> None:
    cutoff = now - AUTO_STOP_RETRY_S
    for key in [key for key, at in _last_auto_stop.items() if at < cutoff]:
        _last_auto_stop.pop(key, None)


def reset_for_tests() -> None:
    """Forget the in-process throttle and release any patched settings path."""
    _last_auto_stop.clear()
