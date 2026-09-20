"""How long the gateway keeps what it produced, and how much of it.

The gateway is a **receiver**, not the owner of an artifact. A node renders it, the
gateway downloads it and serves it to Story Studio, and Story Studio keeps the copy
that matters in its project. The gateway's copy exists so a download can happen and
to make a retry possible — it does not need to be permanent, and nothing pruned it
at all before this, so `data/artifacts/` grew without bound.

Two limits, whichever is reached first, both editable:

    max_age_days  (default 7)
    max_bytes     (default 10 GB)

The age limit is the normal one — an artifact Story Studio has not fetched in a week
is not going to be. The size limit is the backstop for a burst: a few long videos
can exceed ten gigabytes inside a day, and when that happens the oldest go first so
the newest stay fetchable.

Settings live in their own file rather than `.env`, which the menu bar app owns
and rewrites when saving connection settings. Retention settings are read fresh
at every prune, so an edit takes effect on the next pass. Credential/configuration
reads are live too; key rotation does not require a service restart.

**A pin outranks both limits.** An asset the owner creates here *directly* has no
Story Studio project holding the copy that matters, so the gateway's file is the
only copy there is — and under the ordinary policy it would be deleted after a
week. `keep()` pins one artifact against both limits until `unkeep()` releases it.
A pin is answered separately from the age protection, never folded into it: "too
new to touch" and "the owner told me to keep it" are different questions and a
single count would let one masquerade as the other.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import ARTIFACT_DIR, DATA_DIR

SETTINGS_PATH = Path(str(DATA_DIR)) / "retention.json"

#: Where the pins live. Its own file rather than a key inside `retention.json`,
#: because `save()` rewrites that file from a merge, so a pin list stored there
#: would be one clamp bug away from being silently lost — and losing a pin loses
#: the asset.
KEPT_PATH = Path(str(DATA_DIR)) / "kept.json"

DEFAULT_MAX_AGE_DAYS = 7.0
DEFAULT_MAX_BYTES = 10 * 1024**3  # 10 GB
GB = 1024**3

#: Nothing newer than this is ever removed, whatever the limits say. An artifact is
#: written when a job finishes and Story Studio fetches it seconds later; a staged
#: upload is read by the worker while the job runs. Deleting inside that window
#: would turn a retention policy into flaky failures, so the newest files are safe
#: by construction rather than by luck.
PROTECTED_AGE_S = 3600.0

#: The pruner's own interval. An hour is well under the smallest useful age limit and
#: costs one directory walk.
PRUNE_INTERVAL_S = 3600.0

_state_lock = threading.Lock()


def _clamp_age(value: object) -> float:
    try:
        days = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_DAYS
    # A fraction of a day is legitimate (triaging), zero is not: it would mean
    # "delete everything the moment it is old enough to touch".
    return min(max(days, 0.05), 3650.0)


def _clamp_bytes(value: object) -> int:
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    # Floor of 1 MB: a smaller cap cannot hold a single video and would just churn.
    return min(max(size, 1024 * 1024), 10 * 1024**4)


def settings() -> dict:
    """The current limits, read fresh so an edit applies on the next pass."""
    raw: dict = {}
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "max_age_days": _clamp_age(raw.get("max_age_days", DEFAULT_MAX_AGE_DAYS)),
        "max_bytes": _clamp_bytes(raw.get("max_bytes", DEFAULT_MAX_BYTES)),
    }


def save(*, max_age_days: object = None, max_bytes: object = None) -> dict:
    """Store new limits. Omitted values keep what is there."""
    current = settings()
    updated = {
        "max_age_days": _clamp_age(
            max_age_days if max_age_days is not None else current["max_age_days"]
        ),
        "max_bytes": _clamp_bytes(
            max_bytes if max_bytes is not None else current["max_bytes"]
        ),
    }
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    return updated


def _kept_entries() -> list[dict]:
    """Every stored pin, read fresh. Missing or unreadable means none, not an error."""
    try:
        raw: object = json.loads(KEPT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("kept") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("path")]


def _write_kept(entries: list[dict]) -> None:
    KEPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEPT_PATH.write_text(json.dumps({"kept": entries}, indent=2) + "\n", encoding="utf-8")


def kept() -> list[dict]:
    """The pins, each saying whether the file it names is still there.

    A pin whose file is gone is **reported** rather than quietly dropped: the owner
    asked for that artifact, so its absence is a fact about their asset, not an
    untidy list entry to tidy away. `present: false` is also the only way to tell a
    missing file from a pin that was never written.
    """
    out: list[dict] = []
    for entry in _kept_entries():
        path = Path(str(entry["path"]))
        try:
            size: int | None = path.stat().st_size
        except OSError:
            size = None
        out.append(
            {
                "path": str(path),
                "job_id": str(entry.get("job_id") or ""),
                "asset_id": str(entry.get("asset_id") or ""),
                "kept_at": entry.get("kept_at"),
                "present": size is not None,
                "bytes": size,
            }
        )
    return out


def keep(path: str | Path, *, job_id: str = "", asset_id: str = "") -> dict:
    """Pin one artifact so neither limit removes it. Keeping twice is not an error."""
    resolved = str(Path(path))
    with _state_lock:
        entries = [e for e in _kept_entries() if str(e.get("path")) != resolved]
        entries.append(
            {
                "path": resolved,
                "job_id": str(job_id),
                "asset_id": str(asset_id),
                "kept_at": time.time(),
            }
        )
        _write_kept(entries)
    return {"path": resolved, "pinned": True}


def unkeep(path: str | Path) -> dict:
    """Release a pin. The file is left alone — the next prune may take it, or not."""
    resolved = str(Path(path))
    with _state_lock:
        entries = _kept_entries()
        remaining = [e for e in entries if str(e.get("path")) != resolved]
        removed = len(entries) - len(remaining)
        if removed:
            _write_kept(remaining)
    return {"path": resolved, "pinned": False, "removed": removed}


def _files() -> list[tuple[Path, float, int]]:
    """Every file we manage: (path, mtime, size). Missing directory is not an error."""
    out: list[tuple[Path, float, int]] = []
    if not ARTIFACT_DIR.exists():
        return out
    for path in ARTIFACT_DIR.rglob("*"):
        try:
            if path.is_file():
                info = path.stat()
                out.append((path, info.st_mtime, info.st_size))
        except OSError:
            continue
    return out


def _protected_input_paths(store: object | None = None) -> set[Path] | None:
    """Read staged inputs from live and recovery-held job rows."""
    if store is None:
        try:
            from .runtime import store as runtime_store

            store = runtime_store
        except Exception:  # noqa: BLE001 - unknown protection must fail closed
            return None
    reader = getattr(store, "protected_input_paths", None)
    if reader is None:
        return None
    try:
        return {Path(str(path)).resolve() for path in reader() if path}
    except Exception:  # noqa: BLE001 - unreadable is not the same as no inputs
        return None


def usage() -> dict:
    entries = _files()
    pins = kept()
    present = [p for p in pins if p["present"]]
    return {
        "files": len(entries),
        "bytes": sum(size for _, _, size in entries),
        "path": str(ARTIFACT_DIR),
        # **Two questions, two numbers.** `protected_files` is "too new to touch";
        # `pinned_files` is "the owner asked me to keep this". One count answering
        # both would let a pin look like the hour-long grace window, and would hide
        # a pin that had stopped protecting anything because its file was gone.
        "pinned_files": len(present),
        "pinned_bytes": sum(int(p["bytes"] or 0) for p in present),
        "pinned_missing": len(pins) - len(present),
    }


def prune(now: float | None = None, store: object | None = None) -> dict:
    """Delete by age, then by size, oldest first. Never raises.

    Returns what happened, so a person can see the policy working rather than
    trusting that it does.
    """
    moment = time.time() if now is None else now
    # **The transcription quote table is swept here too.** It holds raw audio in
    # memory between a quote and its confirm, and it was pruned only when a *new*
    # quote arrived — so a session that uploaded and never confirmed kept its
    # bytes for the life of the process, up to 25 MB each. Lazy import: `api`
    # imports this module, so the edge has to point the other way only at call
    # time, and a failure to sweep must never stop the files being pruned.
    try:
        from .api import _prune_uploads
        _prune_uploads(moment)
    except Exception:  # noqa: BLE001 - a sweeper must not raise
        pass
    limits = settings()
    entries = _files()
    pinned = {p["path"] for p in kept() if p["present"]}
    protected_inputs = _protected_input_paths(store)
    if protected_inputs is None:
        return {"deleted": 0, "freed_bytes": 0, "limits": limits,
                "blocked_reason": "Cannot read job input protection; no artifacts were pruned. Retry after the job database is readable."}
    # A pinned artifact is not a candidate at all, whatever its age or the size
    # pressure. The young-file protection is counted separately from the pins so
    # neither can be read as the other.
    young = [(p, m, s) for p, m, s in entries if (moment - m) <= PROTECTED_AGE_S]
    removable = [
        (p, m, s)
        for p, m, s in entries
        if (moment - m) > PROTECTED_AGE_S
        and str(p) not in pinned
        and p.resolve() not in protected_inputs
    ]

    doomed: dict[str, Path] = {}
    by_age = 0
    cutoff = moment - limits["max_age_days"] * 86400.0
    for path, mtime, _ in removable:
        if mtime < cutoff:
            doomed[str(path)] = path
            by_age += 1

    # Size second, and only over what is left: a file already deleted for age must
    # not be counted twice against the cap.
    survivors = sorted(
        (e for e in removable if str(e[0]) not in doomed),
        key=lambda e: e[1],  # oldest first
    )
    kept_bytes = sum(size for path, _, size in entries if str(path) not in doomed)
    by_size = 0
    for path, _, size in survivors:
        if kept_bytes <= limits["max_bytes"]:
            break
        doomed[str(path)] = path
        kept_bytes -= size
        by_size += 1

    freed = 0
    deleted = 0
    for path in doomed.values():
        try:
            freed += path.stat().st_size
            path.unlink()
            deleted += 1
        except OSError:
            continue

    # A staged upload lives in a subdirectory; remove the directories it leaves
    # behind, so the tree does not fill with empty folders.
    if ARTIFACT_DIR.exists():
        for child in sorted(ARTIFACT_DIR.rglob("*"), reverse=True):
            try:
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
            except OSError:
                continue

    return {
        "deleted": deleted,
        "freed_bytes": freed,
        "deleted_by_age": by_age,
        "deleted_by_size": by_size,
        "kept_files": len(entries) - deleted,
        "kept_bytes": max(kept_bytes, 0),
        "limits": limits,
        "protected_files": len(young),
        "pinned_files": len(pinned),
        "protected_inputs": len(protected_inputs),
    }


_pruner: threading.Thread | None = None


def start_pruner(interval: float = PRUNE_INTERVAL_S) -> None:
    """Prune once at startup and then on a timer.

    Started from the app's lifespan, so a short-lived process (a test, a CLI call)
    never pays for it — the same rule the fleet refresher follows.
    """
    global _pruner
    with _state_lock:
        if _pruner is not None and _pruner.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    result = prune()
                    if result["deleted"]:
                        print(
                            f"[retention] removed {result['deleted']} file(s), "
                            f"{result['freed_bytes'] // 1024} KiB "
                            f"({result['deleted_by_age']} by age, {result['deleted_by_size']} by size)",
                            flush=True,
                        )
                except Exception:  # noqa: BLE001 - a sweeper must not kill the gateway
                    pass
                time.sleep(interval)

        _pruner = threading.Thread(target=loop, name="artifact-retention", daemon=True)
        _pruner.start()
