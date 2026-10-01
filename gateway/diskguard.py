"""Free-space guard for the Mac the gateway runs on (owner decisions, 2026-10-01).

The gateway had no free-space check at all: this Mac once reached 462 MB free, and a video
burst can write tens of GB a day. Three states by the **volume's free fraction**:

    green   free >= 12 %
    amber   free <  12 %    shown in the menu bar; nothing is deleted yet
    red     free <   6 %    the gateway sweeps its own caches: Trash oldest-first, then
                            artifacts already past their age limit (`retention.reclaim`)

and one absolute floor, **below 5 GB free the gateway refuses new work** with `DISK_LOW`
and a sentence a person can act on, instead of failing in the middle of a write. The
check runs in the retention loop, before a job is accepted, before a large upload is
staged and before a finished artifact is written.

Free space is `shutil.disk_usage(DATA_DIR).free` (what a write can actually use).
"""

from __future__ import annotations

import shutil
import threading
import time
from typing import Any

from .config import DATA_DIR

GB = 1024**3
AMBER_PCT = 12.0
RED_PCT = 6.0
#: A sweep that starts in red keeps going until the volume is back above this, so one pass
#: does not leave the Mac one write away from red again.
RECOVER_PCT = 8.0
#: Below this the gateway refuses new work.
REFUSE_FREE_BYTES = 5 * GB

#: A refusal that triggers a sweep may not trigger another within this window: the sweep
#: walks the artifact tree, and a client retrying every second must not make it a busy loop.
_SWEEP_COOLDOWN_S = 60.0

_lock = threading.Lock()
_last_sweep: dict[str, Any] = {"at": 0.0, "result": None}


class DiskLow(Exception):
    """Not enough free space to take this on. `sentence` is what the owner reads."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def disk_usage() -> tuple[int, int]:
    """`(total_bytes, free_bytes)` of the volume holding the gateway's data."""
    usage = shutil.disk_usage(str(DATA_DIR))
    return int(usage.total), int(usage.free)


def state_of(total: int, free: int) -> str:
    """`green` / `amber` / `red` from the free fraction. Unknown size reads as green."""
    if total <= 0:
        return "green"
    pct = free * 100.0 / total
    if pct < RED_PCT:
        return "red"
    if pct < AMBER_PCT:
        return "amber"
    return "green"


def recover_target(total: int) -> int:
    """Free bytes a red-state sweep aims for."""
    return int(total * RECOVER_PCT / 100.0)


def status() -> dict[str, Any]:
    """Free space, the state and whether new work is being refused. Never raises."""
    try:
        total, free = disk_usage()
    except OSError:
        return {"state": "green", "free_gb": None, "total_gb": None, "refusing": False,
                "last_sweep": _last_sweep["result"]}
    return {
        "state": state_of(total, free),
        "free_gb": round(free / GB, 2),
        "total_gb": round(total / GB, 2),
        "free_pct": round(free * 100.0 / total, 1) if total else None,
        "refusing": free < REFUSE_FREE_BYTES,
        "last_sweep": _last_sweep["result"],
    }


def low_sentence(free: int) -> str:
    return (
        f"This Mac is almost out of disk space ({free / GB:.1f} GB free), so the gateway is not "
        "taking new work until some is freed. Empty the Trash or remove files you no longer "
        "need, then try again."
    )


def sweep_if_red(*, force: bool = False) -> dict[str, Any] | None:
    """In red, free what the gateway itself owns. Returns what was reclaimed, else `None`.

    Rate-limited unless `force` (the retention loop is already on a timer).
    """
    try:
        total, free = disk_usage()
    except OSError:
        return None
    if state_of(total, free) != "red" and free >= REFUSE_FREE_BYTES:
        return None
    with _lock:
        now = time.time()
        if not force and now - _last_sweep["at"] < _SWEEP_COOLDOWN_S:
            return _last_sweep["result"]
        _last_sweep["at"] = now
    from . import retention

    # Aim above the refusal floor even on a volume small enough that 8 % is under it.
    target = max(recover_target(total), REFUSE_FREE_BYTES + GB)
    result = retention.reclaim(max(target - free, 0))
    result["at"] = now
    result["free_gb_before"] = round(free / GB, 2)
    _last_sweep["result"] = result
    return result


def require_room(extra_bytes: int = 0) -> None:
    """Raise `DiskLow` when taking `extra_bytes` more would leave under 5 GB free.

    A red volume is swept once first, so a Mac whose Trash is the problem recovers by
    itself instead of refusing until the next hourly pass.
    """
    try:
        _, free = disk_usage()
    except OSError:
        return  # cannot measure: do not turn a read failure into a refusal
    if free - max(int(extra_bytes), 0) >= REFUSE_FREE_BYTES:
        return
    sweep_if_red()
    _, free = disk_usage()
    if free - max(int(extra_bytes), 0) < REFUSE_FREE_BYTES:
        raise DiskLow(low_sentence(free))
