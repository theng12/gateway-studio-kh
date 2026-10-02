"""Which pool members are out of quota, and until when.

Without this, a pool walks every member on every job, so a member whose free quota
is spent is called again each time and costs a round-trip (and its latency) before the
pool moves on. A member that fails with a *quota or rate-limit* signal is remembered
here and skipped until the provider's reset; one success clears it.

What counts as a quota signal
-----------------------------
HTTP 429, the adapter's own `PROVIDER_QUOTA_EXHAUSTED`, or a provider message saying
`RESOURCE_EXHAUSTED` / quota / rate limit. Anything else (a timeout, a 5xx, a 401, a
bad request) is **not** remembered: those say nothing about the member's quota, and
parking a healthy member for a flaky minute would cost more answers than it saves.

How long
--------
1. What the provider said: `Retry-After` or Google's `retryDelay` (`BackendError.retry_after`),
   or "retry in 34s" in the message.
2. A *daily* limit (Google's `...PerDay...` quota id, or "per day" in the message) lasts
   until the next midnight Pacific, where Google resets its daily free-tier quotas. If the
   provider also gave a shorter delay, the longer of the two wins: Google attaches a
   per-minute `retryDelay` to daily-quota errors too, and trusting it is the loop this
   file exists to stop.
3. Otherwise 60 s: the usual per-minute window.

Persisted as a small JSON file beside `pools.json`, written atomically, so a restart
(frequent) does not forget. A cooling member is never *forbidden*: when every member
that could answer is cooling, the pool tries the one that recovers soonest.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import write_secret

DEFAULT_MINUTE_S = 60.0
#: A provider asking for longer than this is read as a mistake, not a schedule.
MAX_COOLDOWN_S = 36 * 3600.0

#: Replaced in tests with a fake clock.
_clock = time.time

_lock = threading.Lock()
_state: dict[str, Any] = {"key": None, "members": {}}


def _path() -> Path:
    from . import pools

    return pools.POOLS_PATH.with_name("pool_cooldowns.json")


def reset() -> None:
    """Forget the in-process copy (the file is untouched). For tests."""
    with _lock:
        _state["key"] = None
        _state["members"] = {}


def _load() -> dict[str, dict[str, Any]]:
    path = _path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        key = (str(path), None)
    if _state["key"] == key:
        return _state["members"]
    members: dict[str, dict[str, Any]] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    raw = parsed.get("members") if isinstance(parsed, dict) else None
    for member, entry in (raw if isinstance(raw, dict) else {}).items():
        try:
            members[str(member)] = {
                "until": float(entry["until"]),
                "since": float(entry.get("since") or 0),
                "kind": str(entry.get("kind") or "quota"),
                "reason": str(entry.get("reason") or "")[:300],
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    _state["key"], _state["members"] = key, members
    return members


def _save(members: dict[str, dict[str, Any]]) -> None:
    path = _path()
    write_secret(path, json.dumps({"version": 1, "members": members}, indent=2) + "\n")
    try:
        _state["key"] = (str(path), path.stat().st_mtime_ns)
    except OSError:
        _state["key"] = None
    _state["members"] = members


def until(member: str) -> float | None:
    """When this member's cooldown ends, or `None` if it is not cooling down."""
    with _lock:
        entry = _load().get(member)
    if entry and entry["until"] > _clock():
        return entry["until"]
    return None


def active() -> dict[str, dict[str, Any]]:
    """Every member still cooling down (expired entries are not reported)."""
    now = _clock()
    with _lock:
        return {m: dict(e) for m, e in _load().items() if e["until"] > now}


def mark(member: str, until_ts: float, *, kind: str, reason: str) -> None:
    now = _clock()
    with _lock:
        members = {m: e for m, e in _load().items() if e["until"] > now}
        members[member] = {
            "until": until_ts,
            "since": now,
            "kind": kind,
            "reason": reason[:300],
        }
        _save(members)


def clear(member: str) -> None:
    """A success (or an explicit reset) ends the cooldown. No write when there is none."""
    with _lock:
        members = _load()
        if member not in members:
            return
        _save({m: e for m, e in members.items() if m != member})


# -- reading a failure --------------------------------------------------------
_MARKERS = re.compile(
    r"RESOURCE_EXHAUSTED|QUOTA|RATE[ _-]?LIMIT|TOO MANY REQUESTS|REQUESTS PER (DAY|MINUTE)", re.I
)
_DAILY = re.compile(r"per[ _-]?day|daily|PerDay", re.I)
_RETRY_IN = re.compile(
    r"(?:retry|try again|reset)s?\s*(?:in|after)\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|seconds|m|min|minutes|h|hours?)\b",
    re.I,
)
#: Statuses that mean "the request or credential is wrong", never "quota".
_NOT_QUOTA_STATUS = {400, 401, 403, 404, 422}


def is_quota_signal(exc: Any) -> bool:
    status = getattr(exc, "status", None)
    if status == 429 or getattr(exc, "code", "") == "PROVIDER_QUOTA_EXHAUSTED":
        return True
    if status in _NOT_QUOTA_STATUS:
        return False
    return bool(_MARKERS.search(f"{getattr(exc, 'code', '')} {exc}"))


def next_midnight_pacific(now: float) -> float:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/Los_Angeles")
    local = datetime.fromtimestamp(now, zone)
    tomorrow = (local + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone).timestamp()


def _seconds_in_text(text: str) -> float | None:
    found = _RETRY_IN.search(text)
    if not found:
        return None
    amount, unit = float(found.group(1)), found.group(2).lower()
    if unit == "ms":
        return amount / 1000
    if unit.startswith("h"):
        return amount * 3600
    if unit.startswith("m"):
        return amount * 60
    return amount


def cooldown_until(exc: Any, now: float) -> tuple[float, str]:
    """(when the member may be tried again, "daily" | "hint" | "minute") for a quota failure."""
    text = f"{exc}"
    wait = getattr(exc, "retry_after", None)
    if wait is None:
        wait = _seconds_in_text(text)
    daily = getattr(exc, "quota_scope", None) == "day" or bool(_DAILY.search(text))
    ends = now + (min(float(wait), MAX_COOLDOWN_S) if wait is not None else DEFAULT_MINUTE_S)
    kind = "hint" if wait is not None else "minute"
    if daily:
        ends, kind = max(ends, next_midnight_pacific(now)), "daily"
    return max(ends, now + 1.0), kind


def note_failure(member: str, exc: Any) -> float | None:
    """Remember `member` if `exc` is a quota signal. Returns the cooldown end, else `None`."""
    if not is_quota_signal(exc):
        return None
    ends, kind = cooldown_until(exc, _clock())
    mark(member, ends, kind=kind, reason=str(exc))
    return ends
