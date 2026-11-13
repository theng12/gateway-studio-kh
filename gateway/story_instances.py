"""Story Studio Macs that check in, and what they said about Content Pack removals.

**This is not an app updater.** Owner rule 2026-11-13: only Studio Launcher KH installs updates, for
every app. Gateway no longer signs or serves Story releases; the publisher key, descriptors and
"Update now" are gone. A Story Mac still checks in every 30 minutes to report its version, channels,
queue and the packs it was asked to remove; the answer carries no release and no update request.

The check-in answer keeps the old keys (`release: null`, `publisher: null`, `update_now: false`) so a
Story build that has not yet dropped its updater reads "nothing offered" instead of erroring.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import config

#: A Story Mac checks in every 30 minutes; it is shown as offline after missing two and a half.
CHECKIN_EVERY_S = 30 * 60
OFFLINE_AFTER_S = 75 * 60
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ '()-]{0,99}$")
_lock = threading.Lock()  # one check-in writer at a time; readers need no lock


class StoryCheckinError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "STORY_CHECKIN_ERROR") -> None:
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def _instances_file() -> Path:
    return Path(config.DATA_DIR) / "story-instances.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    config.write_secret(path, json.dumps(value, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Story Macs
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int = 200) -> str | None:
    return str(value).strip()[:limit] or None if isinstance(value, (str, int, float)) else None


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


#: What a Story app says it did with a pack the owner asked it to remove. **Only these three
#: words**, and nothing is inferred: an unknown outcome is dropped rather than stored, because
#: "removed" is a claim about a customer's machine and a guess there is the worst kind.
_REMOVAL_OUTCOMES = ("removed", "kept_in_use", "failed")
_MAX_REMOVALS = 200
_MAX_CHANNELS = 50


def _removals(value: Any) -> list[dict[str, Any]]:
    """One Mac's report on the packs it was asked to remove, exactly as the Mac stated it.

    A pack is reported once (the last entry wins), so a Mac cannot count twice for itself. A field
    the Mac left out stays `None`/empty — never a word we supply — and `channels` is only what the
    Mac named.
    """
    if not isinstance(value, list):
        return []
    out: dict[str, dict[str, Any]] = {}
    for item in value[:_MAX_REMOVALS]:
        if not isinstance(item, dict):
            continue
        pack_id = _text(item.get("pack_id"), 200)
        outcome = _text(item.get("outcome"), 20)
        if not pack_id or outcome not in _REMOVAL_OUTCOMES:
            continue
        channels = item.get("channels")
        out[pack_id] = {
            "pack_id": pack_id,
            "outcome": outcome,
            "channels": [name for name in (_text(entry, 120) for entry in channels[:_MAX_CHANNELS])
                         if name] if isinstance(channels, list) else [],
            "detail": _text(item.get("detail"), 400),
        }
    return list(out.values())


def checkin(payload: dict[str, Any]) -> dict[str, Any]:
    """Record one Story Mac's report. The answer offers no release: updates are the Launcher's."""
    instance = _text(payload.get("instance_id"), 100)
    if not instance or not _INSTANCE_ID.match(instance):
        raise StoryCheckinError("instance_id is the machine name.", 422, "STORY_BAD_CHECKIN")
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    now = time.time()
    record = {
        "instance_id": instance,
        "machine": _text(payload.get("machine")) or instance,
        "version": _text(payload.get("version"), 40),
        "channels": _count(payload.get("channels")),
        "queue": {key: _count(queue.get(key)) for key in ("running", "queued", "paused")}
        | {"held": queue.get("held") is True},
        "server_mode": payload.get("server_mode") is True,
        "last_seen": now,
    }
    if "pack_removals" in payload:
        record["pack_removals"] = _removals(payload.get("pack_removals"))
        record["pack_removals_at"] = now
    with _lock:
        rows = _read_json(_instances_file())
        previous = rows.get(instance) if isinstance(rows.get(instance), dict) else {}
        if "pack_removals" not in payload:
            # An older Story states nothing. Keeping the last thing it said is honest — removals
            # are one-shot acts and the report carries when it was made — while reading absence
            # as "removed nothing" would not be.
            record["pack_removals"] = previous.get("pack_removals") if isinstance(
                previous.get("pack_removals"), list) else []
            record["pack_removals_at"] = previous.get("pack_removals_at")
        rows[instance] = record
        _write_json(_instances_file(), rows)
    # The old answer's keys stay, empty: a Story build still running its own updater reads
    # "nothing offered" rather than failing to parse. No release is ever signed or served here.
    return {"release": None, "publisher": None, "update_now": False, "update_request_id": None}


def status_row(row: dict[str, Any], now: float) -> dict[str, Any]:
    last_seen = row.get("last_seen") or 0
    status = "offline" if now - last_seen > OFFLINE_AFTER_S else "online"
    return {**row, "status": status, "age_seconds": round(now - last_seen, 1) if last_seen else None,
            "next_check_by": last_seen + CHECKIN_EVERY_S if last_seen else None}


def instances() -> dict[str, Any]:
    """Every Story Mac that has checked in, as it last reported itself."""
    now = time.time()
    rows = [status_row(row, now) for row in _read_json(_instances_file()).values()
            if isinstance(row, dict)]
    rows.sort(key=lambda r: str(r.get("machine") or "").lower())
    return {"instances": rows}


def pack_removal_report(asked_at: dict[str, float]) -> dict[str, dict[str, Any]]:
    """What each Story Mac last said it did with each pack the owner asked it to remove.

    `asked_at` maps a pack id to the moment the ask was made. **A Mac counts only when it checked
    in after that moment**: a report from before the ask answers a question nobody had asked yet,
    and the whole point of this view is that "not reported yet" never reads as "removed". Macs that
    have never checked in at all are not counted at all — they are unknown, not zero.

    Every outcome is the Mac's own word. Nothing here derives one, and a Mac that named no channel
    is simply not credited with one.
    """
    rows = [row for row in _read_json(_instances_file()).values() if isinstance(row, dict)]
    rows.sort(key=lambda row: str(row.get("machine") or row.get("instance_id") or "").lower())
    out: dict[str, dict[str, Any]] = {}
    for pack_id, asked in asked_at.items():
        report: dict[str, Any] = {
            "pack_id": pack_id,
            "asked_at": asked,
            "total_instances": len(rows),
            "reported_count": 0,
            "removed_count": 0,
            "removed": [], "kept_in_use": [], "failed": [], "not_reported": [],
        }
        for row in rows:
            name = str(row.get("machine") or row.get("instance_id") or "")
            entries = row.get("pack_removals")
            entry = next((item for item in entries if isinstance(item, dict)
                          and item.get("pack_id") == pack_id), None) if isinstance(entries, list) else None
            reported_at = row.get("pack_removals_at")
            if entry is None or not isinstance(reported_at, (int, float)) or reported_at < asked:
                report["not_reported"].append(name)
                continue
            item = {
                "machine": name,
                "instance_id": row.get("instance_id"),
                "outcome": entry.get("outcome"),
                "channels": entry.get("channels") or [],
                "detail": entry.get("detail"),
                "reported_at": reported_at,
            }
            report["reported_count"] += 1
            if entry.get("outcome") == "removed":
                report["removed_count"] += 1
            bucket = report.get(str(entry.get("outcome")))
            if isinstance(bucket, list):
                bucket.append(item)
        out[pack_id] = report
    return out
