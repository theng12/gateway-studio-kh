"""Fleet reachability as a history, and what that history can honestly say.

The owner, 2026-10-29: *"there's no enough detail for me to sort through, i want to be able to sort
for the offline site. and also 'problematic' machine, that goes in and out over a period of time or
sth. like a bad pattern. and also a way for me to see which ones could be the likely cause or sth
too."*

Three facts, one file:

1. **When each Mac (and each site's controller) went up and down**, recorded from the reachability
   the Gateway already measures on every fan-out. **No new polling**: `observe()` is called once per
   refresh, and it writes only when a state actually changed.
2. **What that pattern means** — drops in the last 24 h and 7 days, offline time, a 7-day strip, and
   a *problematic* verdict against a threshold the owner can change (`flapping_drops_day`, default 4).
3. **A likely cause, with the evidence beside it** — built only from fields the Gateway already
   fetched (the controller's `no_hub`/`unreachable`/`unauthorized`, the probe sentence, the Mac's own
   link reading, startup flags, Hub version, running services). Nothing is inferred from a name, a
   model or a hunch, and when nothing supports a cause the answer is **"No evidence yet"** — never a
   plausible-sounding guess (`Publish what is known, and nothing else`).

**A site outage is not its members flapping.** When a controller cannot be reached its machines stop
being observed at all, which is why they drop out of the snapshot rather than being recorded as
offline. A machine transition observed *while its site was down* (a Hub that still reports rows for a
dead site) is recorded with the `site_down` reason and **excluded from that machine's drop count**, so
one dead controller is never five problematic Macs — the defect shape `machines_flat`'s own docstring
records for probe machines.

**Cost is bounded by construction.** Events older than `WINDOW_DAYS` are pruned on every write, each
subject keeps at most `MAX_EVENTS_PER_SUBJECT` events, and the store keeps at most `MAX_SUBJECTS`
subjects (least recently observed first to go). A Mac polling every 15 s that never changes state
writes nothing at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .config import DATA_DIR

#: ponytail: the rolling window is a constant, not a setting. Seven days is what the owner asked to
#: be able to sort by, and it is one number in one place; if a longer memory is ever wanted, this
#: constant and the strip's day count are the two places to change.
WINDOW_DAYS = 7
DAY = 86400.0

#: ponytail: flat caps, so the file cannot grow without bound on a large fleet. 400 events is ~57
#: state changes a day per subject for a week, far past a Mac that sleeps nightly (14); 2,000
#: subjects is a fleet an order of magnitude larger than the owner's.
MAX_EVENTS_PER_SUBJECT = 400
MAX_SUBJECTS = 2000

#: The flapping threshold: drops in 24 h at which a Mac is called *problematic*. **The owner's
#: setting**, defaulted here and never baked into a reader (see `settings()`).
FLAPPING_DROPS_DEFAULT = 4
FLAPPING_DROPS_MIN = 1
FLAPPING_DROPS_MAX = 100

HISTORY_PATH = Path(os.environ.get("GATEWAY_FLEET_HISTORY") or (DATA_DIR / "fleet_history.json"))

#: The owner's knobs, beside the history rather than in `.env` (the menu bar app owns that file).
SETTINGS_PATH = Path(os.environ.get("GATEWAY_FLEET_HEALTH_SETTINGS") or (DATA_DIR / "fleet_health.json"))

#: How long before a recovery a startup action still counts as the thing that brought it back.
#: A restart request is queued and collected at the Mac's next check-in, so the two are minutes
#: apart rather than seconds; ten minutes is far wider than the relay needs and far narrower than
#: an unrelated request in the same hour.
RECOVERY_ACTION_WINDOW_S = 600.0

#: The startup actions that can bring a Mac back. Setting a preference does not; starting or
#: restarting the services does. Read from the audit log the Gateway already writes
#: (`startup_control.audit`), never inferred from the fact that the Mac came back.
RECOVERY_ACTIONS = frozenset({"start_services", "restart_services"})


def _startup_events() -> list[dict]:
    """The startup audit's recent rows, newest first. A seam for tests, and a soft failure.

    Imported lazily: `startup_control` imports `fleet`, which imports this module.
    """
    try:
        from . import startup_control

        return startup_control.recent_audit(limit=500)
    except Exception:  # noqa: BLE001 - no audit is "no evidence", never a failed page
        return []


#: The clock, as a seam: a test drives a week of transitions without sleeping, and nothing else
#: about the module changes (the alternative — patching `time.time` — is process-wide in a suite
#: that runs other work in threads).
_clock = time.time

_lock = threading.Lock()
_state: dict | None = None
_settings_cache: dict = {"stamp": None, "value": None}


# ---------------------------------------------------------------------------
# Settings — read fresh, so a change shows on the next read
# ---------------------------------------------------------------------------


def settings() -> dict:
    """`{"flapping_drops_day": int, ...}` plus the bounds, so the page can show and clamp the knob."""
    value = _read_settings()
    return {
        "flapping_drops_day": value,
        "default": FLAPPING_DROPS_DEFAULT,
        "min": FLAPPING_DROPS_MIN,
        "max": FLAPPING_DROPS_MAX,
        "window_days": WINDOW_DAYS,
    }


def _read_settings() -> int:
    try:
        info = SETTINGS_PATH.stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        return FLAPPING_DROPS_DEFAULT
    with _lock:
        if _settings_cache["stamp"] == stamp and _settings_cache["value"] is not None:
            return int(_settings_cache["value"])
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return FLAPPING_DROPS_DEFAULT
    stated = raw.get("flapping_drops_day") if isinstance(raw, dict) else None
    value = _clamp_threshold(stated)
    with _lock:
        _settings_cache.update(stamp=stamp, value=value)
    return value


def _clamp_threshold(value: object) -> int:
    """The stated threshold when it is a whole number in range; the default otherwise.

    A value outside the range is **refused on the way in** (`set_settings`) and, if it somehow
    reaches the file, is not turned into a number nobody chose: the default stands.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return FLAPPING_DROPS_DEFAULT
    if value < FLAPPING_DROPS_MIN or value > FLAPPING_DROPS_MAX:
        return FLAPPING_DROPS_DEFAULT
    return value


def set_settings(payload: object) -> dict:
    """Replace the threshold. Raises `ValueError` with a sentence rather than clamping silently."""
    if not isinstance(payload, dict):
        raise ValueError("Send an object with 'flapping_drops_day'.")
    value = payload.get("flapping_drops_day")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("'flapping_drops_day' must be a whole number.")
    if value < FLAPPING_DROPS_MIN or value > FLAPPING_DROPS_MAX:
        raise ValueError(
            f"'flapping_drops_day' must be between {FLAPPING_DROPS_MIN} and {FLAPPING_DROPS_MAX}.")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    scratch.write_text(json.dumps({"flapping_drops_day": value}, indent=2), encoding="utf-8")
    os.replace(scratch, SETTINGS_PATH)
    with _lock:
        _settings_cache["stamp"] = None
        _settings_cache["value"] = None
    return settings()


# ---------------------------------------------------------------------------
# The store: transitions per subject, persisted, bounded
# ---------------------------------------------------------------------------


def _load() -> dict:
    """The store, read from disk once per process (then kept in memory under `_lock`)."""
    global _state
    if _state is not None:
        return _state
    subjects: dict[str, dict] = {}
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        raw = {}
    stored = raw.get("subjects") if isinstance(raw, dict) else None
    for key, row in (stored or {}).items():
        if not isinstance(row, dict):
            continue
        events = [e for e in (row.get("events") or []) if _valid_event(e)]
        subjects[str(key)] = {
            "kind": row.get("kind") if row.get("kind") in {"machine", "site"} else "machine",
            "location": row.get("location"),
            "machine": row.get("machine"),
            "label": row.get("label"),
            "up": row.get("up") if isinstance(row.get("up"), bool) else None,
            "since": _num(row.get("since")),
            "observed_since": _num(row.get("observed_since")),
            "observed_at": _num(row.get("observed_at")),
            "events": events,
        }
    _state = {"subjects": subjects}
    return _state


def _valid_event(event: object) -> bool:
    return (
        isinstance(event, list) and len(event) >= 3
        and _num(event[0]) is not None and isinstance(event[1], bool)
    )


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


def _write(subjects: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = HISTORY_PATH.with_name(HISTORY_PATH.name + ".tmp")
    scratch.write_text(json.dumps({"version": 1, "subjects": subjects}, indent=0), encoding="utf-8")
    os.replace(scratch, HISTORY_PATH)


def reset_for_tests() -> None:
    """Forget the process's copy (a test that points GATEWAY_FLEET_HISTORY somewhere else)."""
    global _state
    with _lock:
        _state = None
        _settings_cache.update(stamp=None, value=None)


def _note(subjects: dict, key: str, identity: dict, up: bool, reason: str | None,
          now: float) -> bool:
    """Record one observation. Returns True when the stored state changed."""
    subject = subjects.get(key)
    if subject is None:
        subject = {
            "kind": identity["kind"], "location": identity.get("location"),
            "machine": identity.get("machine"), "label": identity.get("label"),
            "up": up, "since": now, "observed_since": now, "observed_at": now, "events": [],
        }
        subjects[key] = subject
        return True
    changed = False
    if subject.get("observed_since") is None:
        subject["observed_since"] = now
        changed = True
    subject["observed_at"] = now
    for field in ("location", "machine", "label"):
        if identity.get(field) and subject.get(field) != identity[field]:
            subject[field] = identity[field]
            changed = True
    if subject.get("up") is not up:
        previous = subject.get("up")
        subject["up"] = up
        subject["since"] = now
        # The first observation is not a transition: nothing changed, we had simply never looked.
        if previous is not None:
            subject.setdefault("events", []).append(
                [round(now, 3), up, reason, reason != "site_down"])
        changed = True
    return changed


def _prune(subjects: dict, now: float) -> None:
    cutoff = now - WINDOW_DAYS * DAY
    for subject in subjects.values():
        events = subject.get("events") or []
        if not events:
            continue
        kept = [e for e in events if float(e[0]) >= cutoff]
        # Keep the last event before the cutoff: it is the state the window opened in, and the
        # derivation clips to the window itself.
        before = [e for e in events if float(e[0]) < cutoff]
        if before:
            kept.insert(0, before[-1])
        subject["events"] = kept[-MAX_EVENTS_PER_SUBJECT:]
    if len(subjects) <= MAX_SUBJECTS:
        return
    ordered = sorted(subjects.items(), key=lambda item: _num(item[1].get("observed_at")) or 0.0)
    for key, _ in ordered[: len(subjects) - MAX_SUBJECTS]:
        subjects.pop(key, None)


def observe(rows: object, machines: object = None) -> None:
    """Record one fan-out's reachability. Called once per refresh; writes only on a change.

    `rows` are the locations and `machines` the flat machine table the same fan-out built
    (`fleet.machines_flat`) — the one place the controller's own reach reason has already been read
    off the node (`_node_reach_reason`), so the history stores that word rather than a second
    opinion on the same sentence. Without `machines`, only the sites are recorded.
    """
    if not isinstance(rows, list):
        return
    now = _clock()
    with _lock:
        state = _load()
        subjects = state["subjects"]
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            location_id = str(row.get("id") or "")
            if not location_id:
                continue
            site_up = row.get("reachable")
            if isinstance(site_up, bool):
                changed |= _note(
                    subjects, "l:" + location_id,
                    {"kind": "site", "location": location_id, "label": row.get("label")},
                    site_up, _site_reason(row), now)
        for machine in (machines if isinstance(machines, list) else []):
            if not isinstance(machine, dict):
                continue
            location_id = str(machine.get("location_id") or "")
            machine_id = str(machine.get("machine") or "")
            up = machine.get("reachable")
            # `None` is unknown, not a state: a Mac the Hub did not mention is not a Mac that went
            # down, and recording it as one would invent a drop.
            if not location_id or not machine_id or not isinstance(up, bool):
                continue
            reason = None if up else _machine_reason(machine)
            changed |= _note(
                subjects, f"m:{location_id}/{machine_id}",
                {"kind": "machine", "location": location_id, "machine": machine_id,
                 "label": machine.get("label") or machine_id},
                up, reason, now)
        if not changed:
            return
        _prune(subjects, now)
        try:
            _write(subjects)
        except OSError:
            # A history that cannot be written is worth less than the fleet read it rode in on.
            pass


def _site_reason(row: dict) -> str | None:
    """Why a controller was unreachable, in the vocabulary the cause hints use."""
    kind = row.get("error_kind")
    if kind in {"refused", "timeout", "http", "server_disconnected", "error", "local_hub_down"}:
        return kind
    return None


def _machine_reason(machine: dict) -> str | None:
    """Why a machine was unreachable — and `site_down` when its own site was the one not answering.

    **A member is never blamed for its controller being down.** Every Mac behind an unreachable
    controller reads unreachable, and counting those as the Mac's own drops is how one dead site
    becomes five "problematic" Macs.
    """
    if machine.get("location_reachable") is False:
        return "site_down"
    reason = str(machine.get("reach_reason") or "").strip().lower()
    if reason in {"no_hub", "unreachable", "unauthorized"}:
        return reason
    # Nobody stated one. That is `None`, not "unreachable": a reason is evidence, and the history
    # does not keep a word the controller never said.
    return None


# ---------------------------------------------------------------------------
# Derivation — what the history says about one subject, right now
# ---------------------------------------------------------------------------

_REASON_WORDS = {
    "refused": "nothing is listening on its address",
    "timeout": "the connection timed out",
    "http": "it answered with an error",
    "server_disconnected": "the connection dropped mid-answer",
    "no_hub": "the Mac answered but its Studio Hub is not listening",
    "unreachable": "the Mac did not answer at all",
    "site_down": "its whole site was not answering",
    "error": "the connection failed",
}


def _spans(subject: dict, since: float, now: float) -> list[tuple[float, float, bool]]:
    """`[(start, end, up)]` covering `[since, now]`, from the stored transitions.

    **The state the window opened in comes from the events, not from the current state.** The
    subject's own `up` is where the story *ends*; using it as the opening state paints every span
    backwards, which is a history that reads exactly like a real one and is wrong.
    """
    events = sorted(subject.get("events") or [], key=lambda e: float(e[0]))
    spans: list[tuple[float, float, bool]] = []
    cursor = since
    state: bool | None = None
    for event in events:
        if float(event[0]) <= since:
            state = bool(event[1])
    if state is None:
        later = [e for e in events if float(e[0]) > since]
        if later:
            # Nothing recorded before the window: it opened in the opposite state to the first
            # transition inside it.
            state = not bool(later[0][1])
        elif isinstance(subject.get("up"), bool):
            # No transitions recorded at all: it has been in one state the whole time.
            state = bool(subject["up"])
    for event in events:
        at = float(event[0])
        if at <= since or at >= now:
            continue
        if isinstance(state, bool):
            spans.append((cursor, at, state))
        cursor = at
        state = bool(event[1])
    if isinstance(state, bool):
        spans.append((cursor, now, state))
    return spans


def summary(key: str, now: float | None = None) -> dict:
    """Everything the page needs about one subject, derived fresh from the store."""
    now = _clock() if now is None else now
    with _lock:
        subject = (_load()["subjects"] or {}).get(key)
    if subject is None:
        return _unobserved(now)
    observed_since = _num(subject.get("observed_since")) or _num(subject.get("since"))
    if observed_since is None:
        return _unobserved(now)
    events = subject.get("events") or []
    day_start = now - DAY
    week_start = now - WINDOW_DAYS * DAY
    own = [e for e in events if len(e) < 4 or e[3]]
    drops_24 = sum(1 for e in own if not e[1] and float(e[0]) >= day_start)
    drops_7 = sum(1 for e in own if not e[1] and float(e[0]) >= week_start)
    flips_24 = sum(1 for e in events if float(e[0]) >= day_start)
    flips_7 = sum(1 for e in events if float(e[0]) >= week_start)
    spans = _spans(subject, max(week_start, observed_since), now)
    offline_7 = sum(end - start for start, end, up in spans if not up)
    offline_24 = sum(max(0.0, min(end, now) - max(start, day_start))
                     for start, end, up in spans if not up)
    up_now = subject.get("up")
    since = _num(subject.get("since"))
    drops = [e for e in own if not e[1]]
    last = drops[-1] if drops else None
    facts = _drop_facts(drops, events, subject, now)
    return {
        "observed": True,
        "online": up_now if isinstance(up_now, bool) else None,
        "observed_since": observed_since,
        "observed_at": _num(subject.get("observed_at")),
        "since": since,
        "online_since": since if up_now else None,
        "offline_since": since if up_now is False else None,
        "drops_day": drops_24,
        "drops_week": drops_7,
        "flips_day": flips_24,
        "flips_week": flips_7,
        "offline_seconds_day": round(offline_24, 1),
        "offline_seconds_week": round(offline_7, 1),
        # **How long each drop lasted, and whether it came back by itself.** A Mac that drops every
        # eight minutes reads differently from one that dropped once for two minutes, and the
        # count alone cannot tell them apart. Every figure is derived from recorded transitions;
        # a length nobody saw end (or a recovery no startup action explains) is `None`/"unknown",
        # never a silent zero.
        "drop_median_seconds": facts["median_seconds"],
        "drop_longest_seconds": facts["longest_seconds"],
        "drop_gap_seconds": facts["gap_seconds"],
        "drops_completed": facts["completed"],
        "days": _strip(spans, observed_since, now),
        "pattern": _pattern(own, now),
        # What the Gateway itself saw when it last lost this subject, in its own words.
        "last_drop": ({"at": float(last[0]), "reason": last[2] if len(last) > 2 else None,
                       "text": _REASON_WORDS.get(str(last[2])) if len(last) > 2 else None,
                       **(facts["last"] or {})}
                      if last else None),
        # The verdict itself is added by `annotate`: it depends on the owner's threshold, which is
        # read live there.
        "problematic": None,
    }


def _drop_facts(drops: list, events: list, subject: dict, now: float) -> dict:
    """Median/longest drop and the typical gap between drops, from recorded transitions.

    `None` everywhere nothing was measured: a drop still in progress has no length, one drop has
    no gap, and a Mac with no history has neither. A median of one value is that value, which is
    honest — it is not a claim about a distribution that does not exist, and the page also says
    how many drops it is based on.
    """
    ordered = sorted(events, key=lambda event: float(event[0]))
    facts: list[dict] = []
    ended_at: list[float | None] = []
    for drop in sorted(drops, key=lambda event: float(event[0])):
        at = float(drop[0])
        recovery = next((event for event in ordered if float(event[0]) > at and event[1]), None)
        ended = float(recovery[0]) if recovery is not None else None
        duration = round(ended - at, 1) if ended is not None else None
        facts.append({"at": at, "duration_seconds": duration, "ongoing": ended is None})
        ended_at.append(ended)
    completed = [fact for fact in facts if fact["duration_seconds"] is not None]
    durations = [fact["duration_seconds"] for fact in completed]
    gaps = [round(facts[index + 1]["at"] - facts[index]["at"], 1)
            for index in range(len(facts) - 1)
            if facts[index + 1]["at"] > facts[index]["at"]]
    # **Only the last drop publishes its recovery cause**, so ask the startup audit once for it
    # rather than once per drop: that audit is a 500-row read on every Fleet read.
    last = None
    if facts:
        ended = ended_at[-1]
        last = {
            "duration_seconds": facts[-1]["duration_seconds"],
            "ongoing": facts[-1]["ongoing"],
            "recovered_by": _recovered_by(subject, facts[-1]["at"], ended) if ended is not None
                            else "unknown",
        }
    return {
        "median_seconds": _median(durations),
        "longest_seconds": max(durations) if durations else None,
        "gap_seconds": _median(gaps),
        "completed": len(completed),
        "last": last,
    }


def _recovered_by(subject: dict, dropped_at: float, recovered_at: float | None) -> str:
    """`after_startup_action` when the audit proves one, `by_itself` when it just came back.

    A recovery with no audit entry is not proof that nobody restarted anything: the log keeps the
    last 500 events. The word is therefore `by_itself` only for a **machine** the Gateway watched
    return (the transition is evidence) with no matching action; a site's controller is not in the
    startup audit at all, so its recovery is `unknown` rather than given a cause it never had.
    """
    if recovered_at is None:
        return "unknown"
    machine = str(subject.get("machine") or "").strip()
    location = str(subject.get("location") or "").strip()
    if not machine:
        return "unknown"
    for event in _startup_events():
        if event.get("event") != "requested":
            continue
        if str(event.get("action") or "") not in RECOVERY_ACTIONS:
            continue
        if str(event.get("machine") or "") != machine:
            continue
        controller = str(event.get("controller") or "")
        if location and controller and controller != location:
            continue
        at = _num(event.get("at"))
        if at is None:
            continue
        if 0 <= recovered_at - at <= RECOVERY_ACTION_WINDOW_S:
            return "after_startup_action"
    return "by_itself"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)


def _length_text(seconds: float | None) -> str:
    """How a drop's length reads in a sentence — never a guess, never a zero for unknown."""
    if seconds is None:
        return "length not measured"
    return f"{int(seconds)} s" if seconds < 90 else (
        f"{int(seconds // 60)} min" if seconds < 5400 else f"{int(seconds // 3600)} h")


def _unobserved(now: float) -> dict:
    """No history at all: every figure is `None` (not zero) and the strip is uncovered."""
    days = [{"date": _day_label(now - offset * DAY), "offline_fraction": None}
            for offset in range(WINDOW_DAYS - 1, -1, -1)]
    return {
        "observed": False, "online": None, "observed_since": None, "observed_at": None,
        "since": None, "online_since": None, "offline_since": None,
        "drops_day": None, "drops_week": None, "flips_day": None, "flips_week": None,
        "offline_seconds_day": None, "offline_seconds_week": None,
        "drop_median_seconds": None, "drop_longest_seconds": None, "drop_gap_seconds": None,
        "drops_completed": None,
        "days": days, "pattern": None, "problematic": None, "last_drop": None,
        "problematic_basis": None,
    }


def purge_location(location_id: str) -> int:
    """Remove a controller and every machine behind it from reachability history."""
    if not location_id:
        return 0
    prefix = f"m:{location_id}/"
    site_key = f"l:{location_id}"
    with _lock:
        state = _load()
        subjects = state["subjects"]
        gone = [
            key for key, row in subjects.items()
            if key == site_key or key.startswith(prefix) or row.get("location") == location_id
        ]
        if not gone:
            return 0
        for key in gone:
            subjects.pop(key, None)
        try:
            _write(subjects)
        except OSError:
            pass
        return len(gone)


def purge_machine(location_id: str, machine_id: str) -> int:
    """Remove one machine's reachability history."""
    if not location_id or not machine_id:
        return 0
    target = f"m:{location_id}/{machine_id}"
    with _lock:
        state = _load()
        subjects = state["subjects"]
        gone = [
            key for key, row in subjects.items()
            if key == target or (row.get("location") == location_id and row.get("machine") == machine_id)
        ]
        if not gone:
            return 0
        for key in gone:
            subjects.pop(key, None)
        try:
            _write(subjects)
        except OSError:
            pass
        return len(gone)


def _day_label(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def _strip(spans: list[tuple[float, float, bool]], observed_since: float, now: float) -> list[dict]:
    """The last seven calendar days, each with the fraction of the day it was unreachable.

    A day with no observation at all is `None`, never `0.0`: "nobody looked" and "it was up all
    day" must not draw the same bar.
    """
    out: list[dict] = []
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    for offset in range(WINDOW_DAYS - 1, -1, -1):
        start = midnight - offset * DAY
        end = min(start + DAY, now)
        if end <= start:
            out.append({"date": _day_label(start), "offline_fraction": None})
            continue
        if end <= observed_since:
            out.append({"date": _day_label(start), "offline_fraction": None})
            continue
        covered_start = max(start, observed_since)
        covered = end - covered_start
        if covered <= 0:
            out.append({"date": _day_label(start), "offline_fraction": None})
            continue
        offline = 0.0
        for span_start, span_end, up in spans:
            if up:
                continue
            overlap = min(span_end, end) - max(span_start, covered_start)
            if overlap > 0:
                offline += overlap
        fraction = max(0.0, min(1.0, offline / covered))
        out.append({"date": _day_label(start), "offline_fraction": round(fraction, 3)})
    return out


def _pattern(own_events: list, now: float) -> dict | None:
    """A recurring clock time in the drops, when there is one.

    **Three drops at least, and most of them in one hour of the day.** A Mac that sleeps at the same
    time every night is a schedule, not a fault, and the owner named that shape himself ("a bad
    pattern"). The evidence names the window and the count, so the reader can check it.
    """
    drops = [float(e[0]) for e in own_events
             if not e[1] and float(e[0]) >= now - WINDOW_DAYS * DAY]
    if len(drops) < 3:
        return None
    minutes = sorted(int(time.localtime(at).tm_hour * 60 + time.localtime(at).tm_min) for at in drops)
    best_start, best = 0, 0
    for start in range(0, 24 * 60, 30):
        count = sum(1 for minute in minutes if start <= minute < start + 60
                    or (start + 60 > 24 * 60 and minute < start + 60 - 24 * 60))
        if count > best:
            best_start, best = start, count
    if best < 3 or best * 10 < len(drops) * 6:
        return None
    window = f"{_hhmm(best_start)}–{_hhmm((best_start + 60) % (24 * 60))}"
    return {
        "code": "nightly_drops",
        "severity": "warn",
        "text": f"Drops at about the same time every day ({window}) — likely a sleep, power or "
                f"schedule on that Mac",
        "evidence": f"{best} of {len(drops)} drops in the last {WINDOW_DAYS} days fell between "
                    f"{window}.",
    }


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# Likely cause — evidence-cited, from fields the Gateway already has
# ---------------------------------------------------------------------------


#: How a cause may be drawn, decided here once so the page keeps no second list of codes.
#:
#: * ``bad`` — broken or down: a site, controller, Hub or Mac that is not answering, or a refused
#:   credential. The only red.
#: * ``warn`` — degraded but running: a slow link, drops, a partial studio set, a config that will
#:   bite later.
#: * ``info`` — informational and healthy: on Wi-Fi, which drops more than a cable but is not a fault.
#: * ``none`` — no claim either way ("Nothing wrong reported", "No evidence yet", or a reader that
#:   does not know the code). Never red.
#:
#: The owner, 2026-10-30: *"slow link should be yellow and not red. red means it's either broken or
#: down. slow is yellow."* and *"on wifi … should be blue … as long as it's still running fine."*
CAUSE_SEVERITIES = ("bad", "warn", "info", "none")


def _cause(code: str, text: str, evidence: str | None, *, severity: str) -> dict:
    """One evidence-cited cause. `severity` is required: a new code cannot arrive unclassified."""
    if severity not in CAUSE_SEVERITIES:
        raise ValueError(f"unknown cause severity {severity!r} for {code!r}")
    return {"code": code, "text": text, "evidence": evidence, "severity": severity}


NO_EVIDENCE = _cause(
    "no_evidence", "No evidence yet",
    "Nothing this Mac or its controller reports points at a cause.", severity="none")
NOTHING_WRONG = _cause("fine", "Nothing wrong reported", None, severity="none")


def _age(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "at an unknown time"
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 2 * DAY:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // DAY)} days ago"


def _clock_at(epoch: float | None) -> str:
    if not epoch:
        return "an unknown time"
    now = _clock()
    if time.strftime("%Y-%m-%d", time.localtime(epoch)) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", time.localtime(epoch))
    return time.strftime("%b %d, %H:%M", time.localtime(epoch))


#: What each `attention_code` from `fleet._attention` means, in a sentence a person can act on.
#:
#: Every code here is a fact **this Gateway read itself** — a studio it watched stop, a job it
#: watched stop moving, a job it watched fail — and each keeps its own `attention` as the evidence.
#: (The seventh reason, `hub_flagged`, is the Hub's word alone and is drawn further down.) Severity
#: is `warn` for all of them: the Mac is answering (the only red is a site, controller, Hub or Mac
#: that is not, or a refused credential), so an amber row is honest.
ATTENTION_CAUSES = {
    "studio_down": "One of its studios is not running, so this Mac cannot take that kind of work.",
    "studio_not_ready": "One of its studios is up but not ready, so it cannot take work yet.",
    "job_stalled": "Its current job has stopped making progress.",
    "health_word": "Its Studio Hub reports its own health as something other than up.",
    "recent_job_failed": "Its most recent job failed.",
    "no_model_ready": "No model is ready on this Mac, so routing never gives it work.",
}


def machine_causes(machine: dict, history: dict, site: dict | None, threshold: int,
                   latest: dict | None = None) -> list[dict]:
    """Every hint the evidence supports for one Mac, strongest first. Never a guess.

    The order is the order a person would work through them: the site first (nothing on that Mac can
    be the cause if its controller is the thing that is down), then what the controller's own probe
    said, then the Mac's own reported fault, then a pattern in the history.
    """
    causes: list[dict] = []
    offline = machine.get("reachable") is False or str(machine.get("state") or "").lower() == "offline"
    site_down = machine.get("location_reachable") is False
    site_label = machine.get("location") or "its site"

    if site_down and (offline or machine.get("reachable") is None):
        error = (site or {}).get("error")
        seen = _age(_clock() - float((site or {}).get("members_from") or 0)) \
            if (site or {}).get("members_from") else None
        evidence = f"{site_label}'s controller was not answering" + (f": {error}" if error else ".")
        if seen:
            evidence += f" Its member list was last read {seen}."
        causes.append(_cause(
            "site_down",
            f"Its whole site is down — {site_label} is not answering, so this Mac cannot be reached "
            f"either. Fix the controller, not the Mac.",
            evidence, severity="bad"))

    state = str(machine.get("node_state") or "").lower()
    reason = str(machine.get("reach_reason") or "").lower()
    detail = str(machine.get("node_detail") or "").strip()
    last_seen = _num(machine.get("node_last_seen_at"))
    seen_text = _age(_clock() - last_seen) if last_seen else None
    address = str(machine.get("node_address") or "").strip() or None

    if state == "unauthorized":
        causes.append(_cause(
            "unauthorized",
            "Its controller refused that Mac's token — a credential problem, not the network.",
            detail or "The node is listed as unauthorized by its controller.", severity="bad"))
    elif offline and not site_down:
        if machine.get("node_never_connected") is True:
            causes.append(_cause(
                "never_connected",
                "It has never answered since it joined the fleet, so it may be on a network its "
                "controller cannot reach.",
                f"It has not answered once"
                + (f" since it joined {seen_text}" if seen_text else "")
                + (f", at {address}" if address else "") + ".", severity="bad"))
        elif reason == "no_hub":
            causes.append(_cause(
                "hub_not_running",
                "Studio Hub is not running on this Mac — the Mac is awake and answered.",
                f"Its controller reached the Mac but found nothing listening on the Hub port"
                + (" (the probe said no_hub)" if detail else "")
                + (f", last heard {seen_text}" if seen_text else "") + ".", severity="bad"))
        elif reason == "unreachable":
            causes.append(_cause(
                "asleep_or_off",
                "It is not answering its controller — it may be asleep, off the network, or its "
                "Studio Hub may be frozen or waiting on a permission prompt.",
                f"Its controller reported it unreachable" + (f" and it was last heard {seen_text}" if seen_text else "")
                + (" — the controller's own sentence: " + detail if detail else "") + ".",
                severity="bad"))
        elif detail:
            causes.append(_cause("probe_failed", "Its controller could not reach it.",
                                 detail, severity="bad"))

    # What the Mac itself reports about being reachable-but-wrong.
    startup = machine.get("startup") if isinstance(machine.get("startup"), dict) else {}
    running = startup.get("services_running") if isinstance(startup.get("services_running"), dict) else {}
    up_services, total_services = running.get("up"), running.get("total")
    if (offline or reason == "no_hub") and startup.get("start_services_on_launch") is False:
        causes.append(_cause(
            "services_not_starting",
            "This Mac does not start its services when it starts, so they stay down until someone "
            "opens Studio Hub.",
            "The Mac's own startup report says \"start services on launch\" is off.", severity="warn"))
    if (offline or reason == "no_hub") and startup.get("open_at_login") is False:
        causes.append(_cause(
            "not_open_at_login",
            "Studio Hub is not set to open when this Mac starts.",
            "The Mac's own startup report says \"open at login\" is off.", severity="warn"))
    if isinstance(up_services, int) and isinstance(total_services, int) and total_services > 0 \
            and up_services < total_services and not offline:
        causes.append(_cause(
            "studios_down",
            "Studio Hub is up but some of its studios are not running, so it has less to offer than "
            "it should.",
            f"{up_services} of {total_services} services running, as this Mac reports.",
            severity="warn"))
    missing = machine.get("not_installed_studios")
    if isinstance(missing, list) and missing:
        causes.append(_cause(
            "studios_missing",
            "Some studios the controller lists for this Mac are not installed on it.",
            "Not installed: " + ", ".join(str(name) for name in missing[:6]) + ".",
            severity="warn"))

    connection = machine.get("connection") if isinstance(machine.get("connection"), dict) else None
    if connection and connection.get("status") == "measured":
        kind = connection.get("kind")
        speed = connection.get("speed_mbps")
        if connection.get("slow") is True:
            causes.append(_cause(
                "slow_link",
                f"Its Ethernet link runs at {speed} Mb/s — a cable or switch port to replace, and "
                f"the likely reason work there crawls or drops.",
                f"Its own Hub measured {connection.get('text') or 'a link below 1 Gb/s'}.",
                severity="warn"))
        elif kind == "wifi" and (offline or (history.get("drops_day") or 0) > 0):
            drops = history.get("drops_day") or 0
            causes.append(_cause(
                "on_wifi",
                "It is on Wi-Fi, which drops in and out more than a cable.",
                f"Its own Hub measured {connection.get('text') or 'Wi-Fi'}"
                + (f", and it dropped {drops} time{'s' if drops != 1 else ''} in the last 24 h."
                   if drops else "."), severity="info"))

    status = str(machine.get("version_status") or "")
    if status == "behind" and offline:
        newest = (latest or {}).get("version") if isinstance(latest, dict) else None
        causes.append(_cause(
            "hub_behind",
            "Its Studio Hub is behind the newest release, which can leave its services in a state "
            "nobody tested.",
            f"This Mac reports Studio Hub {machine.get('hub_version') or '(no version)'}"
            + (f"; the newest release this Gateway published is {newest}." if newest else
               "; it is behind the newest release this Gateway published."), severity="warn"))

    pattern = history.get("pattern")
    if isinstance(pattern, dict) and pattern.get("text"):
        # A pattern's own severity, when it states a known one; anything else is no claim.
        severity = pattern.get("severity")
        causes.append(_cause(str(pattern.get("code") or "pattern"), str(pattern["text"]),
                             pattern.get("evidence"),
                             severity=severity if severity in CAUSE_SEVERITIES else "none"))

    if causes:
        return causes[:4]
    if offline:
        return [NO_EVIDENCE]
    if history.get("problematic") is True:
        return [_cause(
            "flapping", f"It keeps dropping out — {history.get('drops_day')} drops in the last 24 h.",
            history.get("problematic_basis")
            or f"The threshold is {threshold} drops in 24 h; this Mac has "
               f"{history.get('drops_week')} in the last {WINDOW_DAYS} days.", severity="warn")]
    # **A Mac its own Hub flagged is not "nothing wrong".** `needs_attention` carries a reason
    # (`fleet._attention`) that the page draws in amber, and the Likely-cause cell used to answer
    # "Nothing wrong reported" beside it — the two sentences contradicting each other on the same
    # row (seen live on a fleet Mac, 2026-10-30).
    #
    # The reason now carries the code that says what it is, and most of those codes are this
    # Gateway's own read: quoting a studio it watched go down as "the Hub reports a problem this
    # Gateway cannot see" sent the owner somewhere else to look. Those get a sentence of their own
    # with the reason as evidence; only a reason this Gateway has no code for — genuinely the Hub's
    # word alone, an older Gateway's cached row, or a code this build does not know — keeps the old
    # wording, which stays true for all three.
    attention = str(machine.get("attention") or "").strip()
    code = str(machine.get("attention_code") or "").strip()
    if attention or str(machine.get("state") or "").lower() == "needs_attention":
        if code in ATTENTION_CAUSES:
            return [_cause(code, ATTENTION_CAUSES[code], attention, severity="warn")]
        return [_cause(
            "hub_flagged",
            "Its Hub reports a problem this Gateway cannot see from outside.",
            f"Its Hub says: {attention}" if attention
            else "Its Hub marked this Mac as needing attention without saying why.",
            severity="warn")]
    return [NOTHING_WRONG]


def site_causes(location: dict, history: dict, threshold: int) -> list[dict]:
    """Why one site's controller is not answering, from its own probe's classification."""
    if location.get("reachable") is True:
        if history.get("problematic") is True:
            return [_cause(
                "flapping",
                f"This site keeps dropping out — {history.get('drops_day')} drops in the last 24 h.",
                history.get("problematic_basis")
                or f"The threshold is {threshold} drops in 24 h; it has "
                   f"{history.get('drops_week')} in the last {WINDOW_DAYS} days.", severity="warn")]
        return [NOTHING_WRONG]
    kind = location.get("error_kind")
    error = location.get("error")
    label = location.get("label") or location.get("id") or "This site"
    table = {
        "refused": ("controller_refused",
                    "Nothing is listening at this site's address — Studio Hub is probably bound to "
                    "that Mac only.",
                    error),
        "timeout": ("controller_silent",
                    "The site's address answered nothing in time — the controller is asleep, off, "
                    "or its network is down.",
                    error),
        "server_disconnected": ("controller_disconnected",
                                "The site dropped the connection mid-answer — a network fault at that "
                                "end.",
                                error),
        "http": ("controller_error",
                 "The controller answered with an error rather than a fleet.",
                 error),
        "local_hub_down": ("local_hub_down",
                           "Studio Hub is not running on this Mac, so this site has nothing to "
                           "report.",
                           error),
    }
    entry = table.get(str(kind or ""))
    if entry:
        return [_cause(entry[0], entry[1], entry[2] or f"{label} did not answer.", severity="bad")]
    if error:
        return [_cause("controller_failed", "The controller did not answer.", str(error),
                       severity="bad")]
    return [NO_EVIDENCE]


# ---------------------------------------------------------------------------
# The read-time annotation both surfaces use
# ---------------------------------------------------------------------------


def annotate(value: dict) -> dict:
    """Add `history` and `causes` to every location and machine row, in place.

    **Read-time, never cached.** The flapping verdict depends on the owner's threshold, and a number
    the owner just changed must show on the next read rather than on the next fan-out. The caller
    passes copies of the snapshot's rows (see `admin.fleet_snapshot`), so nothing here reaches the
    cache the routing layer reads.
    """
    threshold = _read_settings()
    now = _clock()
    latest = (value.get("summary") or {}).get("hub_latest")
    latest = latest if isinstance(latest, dict) else None
    locations = [row for row in (value.get("locations") or []) if isinstance(row, dict)]
    by_id = {str(row.get("id")): row for row in locations}
    for row in locations:
        location_id = str(row.get("id") or "")
        history = summary("l:" + location_id, now) if location_id else _unobserved(now)
        history["problematic"] = _verdict(history, threshold)
        history["problematic_basis"] = _basis(history, threshold)
        row["history"] = history
        row["causes"] = site_causes(row, history, threshold)
    for machine in value.get("machines") or []:
        if not isinstance(machine, dict):
            continue
        location_id = str(machine.get("location_id") or "")
        machine_id = str(machine.get("machine") or "")
        history = summary(f"m:{location_id}/{machine_id}", now)
        history["problematic"] = _verdict(history, threshold)
        history["problematic_basis"] = _basis(history, threshold)
        machine["history"] = history
        machine["causes"] = machine_causes(machine, history, by_id.get(location_id), threshold,
                                           latest)
    value["fleet_health"] = settings()
    # **Self-describing windows, one per read.** The two offline figures differ only by their
    # window, and "5 in 24 h · 42 h off" mixed them on both surfaces (Production, 2026-10-30).
    # A reader that consults this map cannot pair a number with the wrong span, and the names
    # themselves say which is which without opening this file.
    value["history_windows"] = {
        "drops_day": "last 24 h",
        "flips_day": "last 24 h",
        "offline_seconds_day": "last 24 h",
        "drops_week": f"last {WINDOW_DAYS} days",
        "flips_week": f"last {WINDOW_DAYS} days",
        "offline_seconds_week": f"last {WINDOW_DAYS} days",
        "drop_median_seconds": f"drops that ended in the last {WINDOW_DAYS} days",
        "drop_longest_seconds": f"drops that ended in the last {WINDOW_DAYS} days",
        "drop_gap_seconds": f"between drops in the last {WINDOW_DAYS} days",
    }
    return value


def _verdict(history: dict, threshold: int) -> bool | None:
    """`True`/`False` for a subject that has been observed, `None` for one nobody has looked at."""
    if not history.get("observed"):
        return None
    return int(history.get("drops_day") or 0) >= threshold


def _basis(history: dict, threshold: int) -> str | None:
    """The evidence behind *Problematic*, in one line beside it.

    The rule is the owner's drop count and stays exactly that — this says **what was counted**
    (24 h, not the 7-day offline figure the same row shows) and how long those drops lasted. A
    drop whose length nobody measured says so, rather than reading as a short one.
    """
    if not history.get("observed"):
        return None
    drops = history.get("drops_day")
    if drops is None:
        return None
    longest = _length_text(history.get("drop_longest_seconds"))
    median = _length_text(history.get("drop_median_seconds"))
    # The drop count is a 24 h figure; the lengths are the recorded drops of the last 7 days. Say
    # which window each number belongs to rather than letting one clock stand for both.
    return (f"{drops} drop{'s' if drops != 1 else ''} in 24 h (threshold {threshold}) · "
            f"longest {longest} · median {median} in {WINDOW_DAYS} days")
