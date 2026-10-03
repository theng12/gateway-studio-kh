"""A paced Studio Hub rollout: this Mac's Hub, then one controller at a time.

The owner's rollout model (2026-10-03): every controller is updated, but one at a time — send the
release to 1100, wait until 1100 itself reports the new version, then 1200, and so on. Inside each
group the Hub does the rest by itself (one update slot per group: the controller first, then its Macs
one by one, each draining before it installs), so the Gateway only paces the controllers.

* **Order**: this Mac's Hub first (when it is a location), then the controllers by label.
* **Wait**: a controller is done when its own row on the fleet read (`version`) says the new version.
  One that does not get there within `STEP_TIMEOUT` stops the whole rollout (`failed`) — something is
  wrong with the release or that Mac, and the rest must not follow it.
* **Skip**: a controller that cannot be reached, or refuses the push, is tried every `RETRY_EVERY`;
  after `SKIP_AFTER` it is skipped and left to the release catch-up once the rollout has finished.
* **Persisted**: `release-rollout.json` in the data directory, written on every change, so a Gateway
  restart carries on where it was (`resume_on_startup`). A push the restart cut short is sent again.

The same file records an "all at once" publish as a finished rollout, so the catch-up's gate is one
question: "is nothing rolling out, and did the newest release finish its rollout?" (`finished`).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import fleet

#: From the push to the controller reporting the new version: its drain grace (10 min), its
#: own running work, the install and the relaunch. The owner's script used the same 30 minutes.
STEP_TIMEOUT = 30 * 60
#: An unreachable or refusing controller is tried again this often...
RETRY_EVERY = 5 * 60
#: ...and skipped (left to the catch-up) after this long.
SKIP_AFTER = 60 * 60
#: How often the rollout looks at the fleet read (the refresher re-reads it every 15 s).
TICK = 20.0

DONE = ("updated", "skipped")

_lock = threading.RLock()
_thread: threading.Thread | None = None
_wake = threading.Event()


def _path() -> Path:
    """Beside the publisher's own record (`release-state.json`), wherever that is."""
    from . import release_publisher

    return release_publisher._state_path().with_name("release-rollout.json")  # noqa: SLF001


def read() -> dict[str, Any]:
    try:
        state = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write(state: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def active() -> bool:
    return read().get("state") == "running"


def finished(version: str) -> bool:
    """The newest release `version` finished its rollout (paced or all at once)."""
    state = read()
    return state.get("state") == "finished" and str(state.get("version") or "").strip() == version.strip()


def plan() -> list[tuple[str, str]]:
    """`(id, label)` in rollout order: this Mac's Hub when it is a location, then controllers by label."""
    order = [(fleet.LOCAL_ID, fleet.local_label())] if fleet.hub_base_url() else []
    controllers = [(str(row.get("id") or ""), str(row.get("label") or row.get("id") or ""))
                   for row in fleet._read()]  # noqa: SLF001 - the roster
    return order + sorted((c for c in controllers if c[0]), key=lambda c: (c[1].lower(), c[0]))


def start(*, artifact_path: str | Path, version: str, build: str, filename: str | None = None,
          allow_downgrade: bool = False) -> dict[str, Any]:
    """Begin a paced rollout in the background and answer at once with its state."""
    from .release_publisher import ReleaseError

    path = Path(artifact_path).expanduser()
    if not path.is_file():
        raise ReleaseError(f"No artifact at {path}.", 400, "RELEASE_ARTIFACT_MISSING")
    if not version.strip() or not build.strip():
        raise ReleaseError("A release needs a version, a build and a filename.", 400, "RELEASE_FIELDS_REQUIRED")
    with _lock:
        current = read()
        if current.get("state") == "running":
            raise ReleaseError(
                f"A rollout of {current.get('version')} is already running; stop it first.",
                409, "RELEASE_ROLLOUT_RUNNING")
        order = plan()
        if not order:
            raise ReleaseError("There is no location to roll out to.", 400, "RELEASE_NO_LOCATIONS")
        state = {
            "mode": "paced", "state": "running", "version": version.strip(), "build": build.strip(),
            "artifact_path": str(path.resolve()), "filename": filename or path.name,
            "allow_downgrade": bool(allow_downgrade), "started_at": time.time(), "finished_at": None,
            "reason": None, "order": [cid for cid, _ in order],
            "steps": {cid: {"label": label, "state": "waiting"} for cid, label in order},
        }
        _write(state)
    print(f"[rollout] {state['version']}: {', '.join(label for _, label in order)}", flush=True)
    _ensure_thread()
    return state


def record_all_at_once(version: str, build: str) -> None:
    """An emergency "all at once" publish counts as a finished rollout (the catch-up may follow it)."""
    now = time.time()
    with _lock:
        _write({"mode": "all_at_once", "state": "finished", "version": version, "build": build,
                "started_at": now, "finished_at": now, "reason": None, "order": [], "steps": {}})


def stop(reason: str = "stopped by the owner") -> dict[str, Any]:
    with _lock:
        state = read()
        if state.get("state") != "running":
            return state
        state.update(state="stopped", reason=reason, finished_at=time.time())
        _write(state)
    _wake.set()
    _notify(state)
    return state


def resume() -> dict[str, Any]:
    """Carry on a stopped or failed rollout; a controller that timed out is sent the release again."""
    with _lock:
        state = read()
        if state.get("state") not in ("stopped", "failed"):
            return state
        for step in state.get("steps", {}).values():
            if step.get("state") not in DONE:
                for key in ("first_try_at", "retry_at", "sent_at", "detail"):
                    step.pop(key, None)
                step["state"] = "waiting"
        state.update(state="running", reason=None, finished_at=None)
        _write(state)
    _ensure_thread()
    return state


def resume_on_startup() -> None:
    """A Gateway restart in the middle of a rollout carries on with it."""
    if active():
        _ensure_thread()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        _wake.set()
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="release-rollout", daemon=True)
        _thread.start()


def _run() -> None:
    while active():
        _wake.clear()
        try:
            tick(fleet.snapshot().get("locations") or [])
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the rollout
            print(f"[rollout] pass failed: {type(exc).__name__}: {exc}", flush=True)
        _wake.wait(TICK)


def _push(state: dict[str, Any], cid: str) -> dict[str, Any]:
    from . import release_publisher

    try:
        result = release_publisher.publish(
            artifact_path=state["artifact_path"], version=state["version"], build=state["build"],
            filename=state.get("filename"), allow_downgrade=bool(state.get("allow_downgrade")),
            controller_ids=[cid], rollout=True)
        return (result.get("locations") or {}).get(cid) or {"ok": False, "detail": "no answer for it"}
    except Exception as exc:  # noqa: BLE001 - recorded on the step, retried like a refusal
        return {"ok": False, "code": getattr(exc, "code", "RELEASE_PUSH_FAILED"),
                "detail": getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"}


def tick(rows: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """One look at the fleet: settle the controller being updated, or send the release to the next.

    At most one push per call. `rows` are the fleet read's location rows (`version`, `reachable`).
    """
    clock = now is None
    now = time.time() if now is None else now
    with _lock:
        state = read()
        if state.get("state") != "running":
            return state
    version = state["version"]
    by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict)}
    for cid in state["order"]:
        step = state["steps"][cid]
        if step["state"] in DONE:
            continue
        row = by_id.get(cid) or {}
        reachable = bool(row.get("reachable"))
        if reachable and str(row.get("version") or "").strip() == version:
            step.update(state="updated", updated_at=now, detail=None)
            print(f"[rollout] {step['label']} is on {version}", flush=True)
            continue
        if step["state"] in ("sent", "installing"):
            if now - float(step.get("sent_at", now)) >= STEP_TIMEOUT:
                step.update(state="failed", detail=f"did not come back on {version} within "
                                                   f"{STEP_TIMEOUT // 60} min")
                state.update(state="failed", finished_at=now,
                             reason=f"{step['label']} did not come back on {version} within "
                                    f"{STEP_TIMEOUT // 60} min, so the rollout stopped there")
                break
            # Not answering after the push: it is swapping in the new app and relaunching.
            step["state"] = "sent" if reachable else "installing"
            break
        # waiting (or a push a restart cut short): send it, unless it is waiting out a retry
        first = step.setdefault("first_try_at", now)
        if float(step.get("retry_at") or 0) > now:
            break
        outcome: dict[str, Any] | None = None
        if reachable:
            step["state"] = "sending"
            _save(state)
            print(f"[rollout] sending {version} to {step['label']}", flush=True)
            outcome = _push(state, cid)
            now = time.time() if clock else now  # a full push takes minutes
        if outcome and outcome.get("ok"):
            step.update(state="sent", sent_at=now, detail=None)
            step.pop("retry_at", None)
            break
        if outcome and outcome.get("downgrade"):
            step.update(state="skipped", detail=str(outcome.get("detail") or "it holds a newer release"))
            continue
        why = (str(outcome.get("detail") or outcome.get("code") or "it refused the release")
               if outcome else "it is not answering")
        if now - float(first) >= SKIP_AFTER:
            step.update(state="skipped", detail=f"{why}; skipped after {SKIP_AFTER // 60} min — the "
                                                 "catch-up sends it once it is back")
            continue
        step.update(state="waiting", retry_at=now + RETRY_EVERY,
                    detail=f"{why}; trying again in {RETRY_EVERY // 60} min")
        break
    else:
        state.update(state="finished", finished_at=now)
    with _lock:
        latest = read()
        if latest.get("state") != "running":
            # Stopped while this pass was pushing: the owner's stop wins, the push's outcome is kept.
            state.update({key: latest.get(key) for key in ("state", "reason", "finished_at")})
            _write(state)
            return state
        _write(state)
    if state["state"] != "running":
        _notify(state)
    return state


def _save(state: dict[str, Any]) -> None:
    with _lock:
        if read().get("state") == "running":
            _write(state)


def _labels(state: dict[str, Any], *states: str) -> list[str]:
    return [state["steps"][cid]["label"] for cid in state.get("order", [])
            if state["steps"][cid]["state"] in states]


def _span(labels: list[str]) -> str:
    """`1300–1600` for three or more in a row, otherwise `1300, 1400`."""
    if len(labels) > 2:
        return f"{labels[0]}–{labels[-1]}"
    return ", ".join(labels)


def summary(state: dict[str, Any]) -> str:
    """"Updating 1200 (3 of 6) · 1100 done · 1300–1600 waiting"."""
    if not state.get("order"):
        return ""
    order = state["order"]
    current = next((cid for cid in order if state["steps"][cid]["state"] not in DONE), None)
    parts = []
    if state.get("state") == "running" and current:
        verb = {"sending": "Sending to", "waiting": "Waiting for"}.get(state["steps"][current]["state"], "Updating")
        parts.append(f"{verb} {state['steps'][current]['label']} ({order.index(current) + 1} of {len(order)})")
    elif state.get("state") == "finished":
        parts.append(f"Finished {state.get('version')}")
    elif current:
        parts.append(f"Stopped at {state['steps'][current]['label']} ({order.index(current) + 1} of {len(order)})")
    done = _labels(state, "updated")
    if done:
        parts.append(f"{_span(done)} done")
    skipped = _labels(state, "skipped")
    if skipped:
        parts.append(f"{', '.join(skipped)} skipped")
    later = [state["steps"][cid]["label"] for cid in order[order.index(current) + 1:]] if current else []
    if later:
        parts.append(f"{_span(later)} waiting")
    return " · ".join(parts)


def status(snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The rollout for the Fleet page: its state, one line, and each group's Macs on the new version."""
    state = read()
    if not state or state.get("mode") != "paced":
        return None
    machines = (snapshot or {}).get("machines") or []
    for cid, step in state.get("steps", {}).items():
        group = [m for m in machines if m.get("location_id") == cid]
        step["nodes_total"] = len(group) if group else None
        step["nodes_updated"] = (sum(1 for m in group if str(m.get("hub_version") or "").strip() == state["version"])
                                 if group else None)
    state["summary"] = summary(state)
    return state


def _notify(state: dict[str, Any]) -> None:
    from . import notifier

    version = state.get("version")
    done, skipped = _labels(state, "updated"), _labels(state, "skipped")
    waiting = _labels(state, "waiting", "sending", "sent", "installing", "failed")
    if state.get("state") == "finished":
        text = f"🚀 Studio Hub {version} rolled out, one controller at a time: {len(done)} updated"
        if skipped:
            text += f", {', '.join(skipped)} skipped (the catch-up sends it once they are back)"
        text += ". Their Macs update themselves one at a time."
    else:
        text = (f"⚠️ Studio Hub {version} rollout stopped: {state.get('reason')}. "
                f"Updated: {', '.join(done) or 'none'}; not yet: {', '.join(waiting) or 'none'}. "
                "Resume it from Fleet → Locations.")
    try:
        notifier.notify("release", text)
    except Exception:  # noqa: BLE001 - telling the owner must never break the rollout
        pass
