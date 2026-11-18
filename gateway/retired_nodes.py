"""Machines removed here that their own site could not unregister.

Force removal purges the Gateway's own records, but the Fleet read is rebuilt from the
controller's last report, which still lists a node its Hub could not unregister — so the row
came back and "removed" was only true from this Gateway. A tombstone keyed by
(controller, machine) keeps the row gone. A fresh report from that machine after the removal
clears it (a repaired Mac returns; a sold one never does), and so does Undo.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .config import DATA_DIR

PATH = Path(os.environ.get("GATEWAY_RETIRED_NODES") or (DATA_DIR / "retired-nodes.json"))
_LOCK = threading.RLock()


def _key(controller_id: object, machine_id: object) -> str:
    return f"{controller_id}|{machine_id}"


def registry() -> dict[str, dict]:
    """Every tombstone, keyed `controller|machine`. An unreadable file reads as none."""
    try:
        data = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def entries() -> list[dict]:
    return sorted(registry().values(), key=lambda entry: entry.get("at") or 0)


def mark(controller_id: str, machine_id: str, *, label: str | None = None, at: float | None = None) -> dict:
    with _LOCK:
        marks = registry()
        entry = {"controller_id": str(controller_id), "machine_id": str(machine_id),
                 "label": label, "at": float(at if at is not None else time.time())}
        marks[_key(controller_id, machine_id)] = entry
        _write(marks)
        return entry


def clear(controller_id: str, machine_id: str) -> bool:
    with _LOCK:
        marks = registry()
        removed = marks.pop(_key(controller_id, machine_id), None)
        if removed is not None:
            _write(marks)
        return removed is not None


def _write(marks: dict[str, dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".retired-nodes-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(marks, handle, indent=1, sort_keys=True)
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def reset_for_tests() -> None:
    with _LOCK:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass
