"""Recoverable retention: artifacts -> runtime _trash -> macOS Trash.

First-stage configurable limits default to three days/eight GB, with pins,
one-hour grace and live/held job inputs protected. Retired files keep their
source mtime; timestamp buckets record the actual retirement time instead.
Both current and legacy runtime trash are transferred to macOS Trash after a
configurable seven days (default), or oldest first when their combined physical
size exceeds a configurable twenty GB. Native transfer failures preserve the
source. Gateway never empties macOS Trash.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ARTIFACT_DIR, DATA_DIR, write_secret

SETTINGS_PATH = Path(str(DATA_DIR)) / "retention.json"

#: Where the pins live. Its own file rather than a key inside `retention.json`,
#: because `save()` rewrites that file from a merge, so a pin list stored there
#: would be one clamp bug away from being silently lost — and losing a pin loses
#: the asset.
KEPT_PATH = Path(str(DATA_DIR)) / "kept.json"

DEFAULT_MAX_AGE_DAYS = 3.0
DEFAULT_MAX_BYTES = 10 * 1024**3  # 10 GB
GB = 1024**3

#: Runtime trash is separate from the developer project's owner-managed _trash.
TRASH_DIR = Path(str(DATA_DIR)) / "_trash"
LEGACY_TRASH_DIR = Path(str(DATA_DIR)) / "trash"
#: Defaults for the runtime-trash policy; the owner's `retention.json` may change both.
TRASH_DAYS = 7.0
TRASH_CAP_BYTES = 20 * GB
_BUCKET_FORMAT = "%Y-%m-%dT%H-%M-%S.%fZ"
_trash_lock = threading.RLock()
_last_transfer: dict = {"recycle_failures": 0}

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


def _clamp_age(value: object, default: float = DEFAULT_MAX_AGE_DAYS) -> float:
    try:
        days = float(value)
    except (TypeError, ValueError):
        return default
    # A fraction of a day is legitimate (triaging), zero is not: it would mean
    # "delete everything the moment it is old enough to touch".
    return min(max(days, 0.05), 3650.0)


def _clamp_bytes(value: object, default: int = DEFAULT_MAX_BYTES) -> int:
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return default
    # Floor of 1 MB: a smaller cap cannot hold a single video and would just churn.
    return min(max(size, 1024 * 1024), 10 * 1024**4)


def settings() -> dict:
    """The current limits, read fresh so an edit applies on the next pass.

    The first pair is the live artifact store; the second is the runtime Trash Folder,
    whose defaults are seven days and twenty GB.
    """
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
        "trash_days": _clamp_age(raw.get("trash_days", TRASH_DAYS), TRASH_DAYS),
        "trash_cap_bytes": _clamp_bytes(raw.get("trash_cap_bytes", TRASH_CAP_BYTES), TRASH_CAP_BYTES),
    }


def save(*, max_age_days: object = None, max_bytes: object = None,
         trash_days: object = None, trash_cap_bytes: object = None) -> dict:
    """Store new limits. Omitted values keep what is there."""
    current = settings()
    updated = {
        "max_age_days": _clamp_age(
            max_age_days if max_age_days is not None else current["max_age_days"]
        ),
        "max_bytes": _clamp_bytes(
            max_bytes if max_bytes is not None else current["max_bytes"]
        ),
        "trash_days": _clamp_age(
            trash_days if trash_days is not None else current["trash_days"], TRASH_DAYS
        ),
        "trash_cap_bytes": _clamp_bytes(
            trash_cap_bytes if trash_cap_bytes is not None else current["trash_cap_bytes"], TRASH_CAP_BYTES
        ),
    }
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_secret(SETTINGS_PATH, json.dumps(updated, indent=2) + "\n")
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
    write_secret(KEPT_PATH, json.dumps({"kept": entries}, indent=2) + "\n")


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


def _safe_path(path: Path, root: Path) -> bool:
    """Only lexical descendants with no symlink component or resolved escape."""
    try:
        relative = path.relative_to(root)
        if root.is_symlink() or ".." in relative.parts:
            return False
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _files() -> list[tuple[Path, float, int]]:
    """Managed regular files; symlinks and paths escaping the store are excluded."""
    out: list[tuple[Path, float, int]] = []
    for path in ARTIFACT_DIR.rglob("*"):
        try:
            if _safe_path(path, ARTIFACT_DIR) and path.is_file():
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


def _physical(info: os.stat_result) -> int:
    """Bytes a file really occupies (allocated blocks), falling back to its length."""
    blocks = getattr(info, "st_blocks", None)
    return int(blocks) * 512 if blocks is not None else int(info.st_size)


def _trash_stats(entries: list | None = None) -> tuple[int, int]:
    files = size = 0
    for _, _, _, path in _trash_files() if entries is None else entries:
        try:
            size += path.stat().st_size
            files += 1
        except OSError:
            continue
    return files, size


def trash_cap_bytes(total: int | None = None) -> int:
    """Owner-adjustable combined runtime-trash cap; `total` retained for caller compatibility."""
    return settings()["trash_cap_bytes"]


def _trash_files() -> list[tuple[str, float, int, Path]]:
    """Recognized runtime files, ordered by retirement time, never source mtime.

    Legacy dates conservatively mean the end of that UTC day. Unknown buckets
    are left alone; there is no inferred retirement time or manual migration.
    The one named exception is `_trash/logs`, where the app retires its own log
    rotations: a file there carries its own mtime, because it is written by us,
    not a date bucket.
    """
    out: list[tuple[str, float, int, Path]] = []
    for root in (TRASH_DIR, LEGACY_TRASH_DIR):
        if not root.is_dir() or root.is_symlink():
            continue
        for folder in root.iterdir():
            try:
                if not _safe_path(folder, root) or not folder.is_dir():
                    continue
                try:
                    retired = datetime.strptime(folder.name, _BUCKET_FORMAT).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    retired = datetime.strptime(folder.name, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86400
                for path in folder.rglob("*"):
                    if _safe_path(path, root) and path.is_file():
                        out.append((folder.name, retired, _physical(path.stat()), path))
            except (OSError, ValueError):
                continue
    logs = TRASH_DIR / "logs"
    if logs.is_dir() and not logs.is_symlink():
        for path in logs.rglob("*"):
            try:
                if _safe_path(path, TRASH_DIR) and path.is_file():
                    info = path.stat()
                    out.append(("logs", info.st_mtime, _physical(info), path))
            except OSError:
                continue
    out.sort(key=lambda entry: (entry[1], str(entry[3])))
    return out


def _drop_empty_dirs(root: Path) -> None:
    for child in sorted(root.rglob("*"), reverse=True):
        try:
            if _safe_path(child, root) and child.is_dir() and not any(child.iterdir()):
                child.rmdir()
        except OSError:
            continue


# Constant JXA source: paths are argv, never shell/source interpolation. Foundation
# provides per-volume native Trash and collision handling without Finder AppleEvents.
_RECYCLE_SCRIPT = """ObjC.import('Foundation');
function run(argv) {
    var error = Ref();
    var result = Ref();
    var url = $.NSURL.fileURLWithPath($(argv[0]));
    if (!$.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError(url, result, error)) {
        throw new Error('Native Trash transfer failed');
    }
    return 'ok';
}
"""


def _recycle(path: Path) -> bool:
    """Injectable native boundary. Failure leaves the source; no delete fallback."""
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _RECYCLE_SCRIPT, str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.returncode == 0 and not path.exists()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _transfer_trash(moment: float, *, cap: int | None = None) -> dict:
    """Apply age OR combined physical-size cap, oldest retired files first."""
    global _last_transfer
    with _trash_lock:
        entries = _trash_files()
        limits = settings()
        total = sum(entry[2] for entry in entries)
        limit = trash_cap_bytes() if cap is None else cap
        files = moved = failures = capped = 0
        for _, retired, size, path in entries:
            aged = moment - retired > limits["trash_days"] * 86400
            if not aged and total <= limit:
                continue
            root = TRASH_DIR if path.is_relative_to(TRASH_DIR) else LEGACY_TRASH_DIR
            if not _safe_path(path, root):
                continue
            try:
                recycled = _recycle(path)
            except Exception:  # noqa: BLE001 - preserve source on native/helper failure
                recycled = False
            if not recycled:
                failures += 1
                continue
            files += 1
            moved += size
            capped += int(not aged)
            total -= size
        _drop_empty_dirs(TRASH_DIR)
        _drop_empty_dirs(LEGACY_TRASH_DIR)
        # Legacy purge fields remain as compatibility aliases; no file is purged,
        # and neither native Trash transfer nor retirement claims freed bytes.
        result = {"recycled_files": files, "recycled_bytes": moved,
                  "recycle_failures": failures, "trash_capped_files": capped,
                  "purged_files": files, "purged_bytes": 0}
        if failures:
            result["warning"] = f"Could not move {failures} file(s) to macOS Trash; they remain in runtime trash. Retry Prune Now."
        _last_transfer = result.copy()
        return result


def _day(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")


def retire(path: Path, *, root: Path, namespace: str = "", now: float | None = None,
           tree: bool = False) -> bool:
    """Shared retirement for artifacts, superseded/failed Story releases, and whole backup folders.

    `tree=True` moves a directory as one unit (a voice backup), so its files stay together in the Trash.
    """
    moment = time.time() if now is None else now
    with _trash_lock:
        try:
            if not _safe_path(path, root) or not (path.is_dir() if tree else path.is_file()):
                return False
            bucket = datetime.fromtimestamp(moment, timezone.utc).strftime(_BUCKET_FORMAT)
            target = TRASH_DIR / bucket / namespace / path.relative_to(root)
            if not _safe_path(target, TRASH_DIR):
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            while target.exists():
                target = target.with_name(f"{path.name}.{uuid.uuid4().hex}")
            path.rename(target)
            return True
        except (OSError, ValueError):
            return False


def _move_to_trash(path: Path, moment: float) -> bool:
    return retire(path, root=ARTIFACT_DIR, now=moment)


def reclaim(need_bytes: int, now: float | None = None, store: object | None = None) -> dict:
    """Run ordinary recoverable policy under pressure; renames free no volume bytes.

    `diskguard` measures actual available space after this pass. Pressure never
    authorizes early Trash transfer or permanent deletion.
    """
    result = prune(now=now, store=store)
    return {**result, "trash_files": result["recycled_files"],
            "trash_bytes": result["recycled_bytes"], "artifact_files": result["deleted"],
            "artifact_bytes": result["moved_bytes"], "freed_bytes": 0,
            "enough": int(need_bytes) <= 0}


def _disk_status() -> dict:
    from . import diskguard

    return diskguard.status()


#: One usage walk at a time (0.2.162). The walk visits every artifact and trash file
#: (~50,000 on 2026-10-09) with a symlink check per path component, and one call per
#: 5 s Jobs poll stacked up to 40 concurrent walks: every server worker thread, so every
#: sync route, `/v1/models` included, waited minutes.
_usage_lock = threading.Lock()
_usage_last: dict = {"done": 0, "value": None}


def shared_usage() -> dict:
    """`usage()`, but callers that arrive while a walk runs take that walk's answer.

    ponytail: the shared answer may have started just before the caller asked (at most
    one walk old); a per-caller fresh walk is what stacked up.
    """
    seen = _usage_last["done"]
    with _usage_lock:
        if _usage_last["done"] != seen and _usage_last["value"] is not None:
            return _usage_last["value"]
        value = usage()
        _usage_last["value"] = value
        _usage_last["done"] = seen + 1
        return value


def usage() -> dict:
    entries = _files()
    pins = kept()
    trash = _trash_files()
    trash_files, trash_bytes = _trash_stats(trash)
    present = [p for p in pins if p["present"]]
    limits = settings()
    return {
        "files": len(entries),
        "bytes": sum(size for _, _, size in entries),
        "path": str(ARTIFACT_DIR),
        # The Trash Folder is reported apart from `bytes`, which is the live store
        # the size cap is measured against.
        "trash_files": trash_files,
        "trash_bytes": trash_bytes,
        "trash_days": limits["trash_days"],
        "trash_physical_bytes": sum(e[2] for e in trash),
        "trash_cap_bytes": limits["trash_cap_bytes"],
        "disk": _disk_status(),
        "trash_path": str(TRASH_DIR),
        "legacy_trash_path": str(LEGACY_TRASH_DIR),
        "recycle_failures": _last_transfer.get("recycle_failures", 0),
        "warning": _last_transfer.get("warning"),
        # **Two questions, two numbers.** `protected_files` is "too new to touch";
        # `pinned_files` is "the owner asked me to keep this". One count answering
        # both would let a pin look like the hour-long grace window, and would hide
        # a pin that had stopped protecting anything because its file was gone.
        "pinned_files": len(present),
        "pinned_bytes": sum(int(p["bytes"] or 0) for p in present),
        "pinned_missing": len(pins) - len(present),
    }


def prune(now: float | None = None, store: object | None = None) -> dict:
    """Retire by age/size, then transfer runtime trash by its seven-day/20 GB policy.

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
        return {"deleted": 0, "moved_files": 0, "moved_bytes": 0, "freed_bytes": 0,
                "limits": limits, **_transfer_trash(moment),
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

    # Size second, and only over what is left: a file already retired for age must
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

    moved_bytes = 0
    deleted = 0
    move_failures = 0
    for path in doomed.values():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if _move_to_trash(path, moment):
            moved_bytes += size
            deleted += 1
        else:
            move_failures += 1

    # A staged upload lives in a subdirectory; remove the directories it leaves
    # behind, so the tree does not fill with empty folders.
    _drop_empty_dirs(ARTIFACT_DIR)

    transferred = _transfer_trash(moment)
    if move_failures:
        message = f"Could not retire {move_failures} file(s); they remain in the artifact store. Retry Prune Now."
        transferred["warning"] = " ".join(filter(None, (message, transferred.get("warning"))))
        _last_transfer["warning"] = transferred["warning"]

    return {
        "deleted": deleted,  # moved to the Trash Folder, not unlinked
        "moved_files": deleted,
        "moved_bytes": moved_bytes,
        "move_failures": move_failures,
        "freed_bytes": 0,
        **transferred,
        "deleted_by_age": by_age,
        "deleted_by_size": by_size,
        "kept_files": len(entries) - deleted,
        "kept_bytes": sum(size for _, _, size in entries) - moved_bytes,
        "limits": limits,
        "protected_files": len(young),
        "pinned_files": len(pinned),
        "protected_inputs": len(protected_inputs),
    }


def _guard_and_compact() -> None:
    """The rest of the hourly pass: the red-volume sweep and the job-database upkeep.

    Each step is independent and none may stop the loop.
    """
    try:
        from . import diskguard

        swept = diskguard.sweep_if_red(force=True)
        if swept and swept["recycled_files"]:
            print(f"[retention] disk is low: moved {swept['recycled_bytes'] // (1024 * 1024)} MiB to macOS Trash "
                  f"(Trash {swept['trash_files']} file(s), expired artifacts "
                  f"{swept['artifact_files']})", flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from .runtime import store as runtime_store

        done = runtime_store.archive_old()
        if done["archived"]:
            print(f"[retention] archived {done['archived']} job(s) older than "
                  f"{done['days']:g} days to {done['file_count']} file(s)", flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import voice_import

        swept = voice_import.sweep_backups()
        if swept["retired"]:
            print(f"[retention] retired {swept['retired']} voice backup(s) older than "
                  f"{swept['days']:g} days to the Trash Folder", flush=True)
    except Exception:  # noqa: BLE001
        pass


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
                result = {}
                try:
                    result = prune()
                    if result["deleted"]:
                        print(
                            f"[retention] moved {result['deleted']} file(s) to the Trash Folder, "
                            f"{result['moved_bytes'] // 1024} KiB "
                            f"({result['deleted_by_age']} by age, {result['deleted_by_size']} by size)",
                            flush=True,
                        )
                except Exception:  # noqa: BLE001 - a sweeper must not kill the gateway
                    pass
                if result.get("warning"):
                    print(f"[retention] {result['warning']}", flush=True)
                _guard_and_compact()
                time.sleep(interval)

        _pruner = threading.Thread(target=loop, name="artifact-retention", daemon=True)
        _pruner.start()
