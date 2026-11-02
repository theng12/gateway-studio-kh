"""Backups of what cannot be recreated (owner, 2026-10-02).

**What** - only the files whose loss costs more than a reinstall:

* the two signing identities: `release-publisher.pem` (every Hub pinned its fingerprint) and
  `content-delivery/` (the Content Pack shelf identity Story Studio trusts);
* the Content Packs themselves: `content-library/` holds the released `.sspack` bytes (and each
  one's `metadata.json`). The shelf cannot serve a release without them, so a backup of the
  identity alone would look like a recovery point and not be one (~160 MB today);
* the API keys: `.env` (the key Story Studio presents) and `credentials.json` (provider keys and
  the controllers' fleet tokens);
* the fleet roster `fleet.json` (controller ids, labels, addresses) when that option is on;
* the voices library: the Gateway's catalogue `voices.json` + `voice-library/` (reference clips,
  transcripts) and this Mac's Studio Hub voices (`StudioHub Standalone/voice/voices/`);
* the local settings files: `providers.local.json`, `pools.json`, `provider-concurrency.local.json`,
  `model-limits.local.json`, `telegram.json`, `render_optin.json`, `fleet_warnings.json`,
  `voices_state.json`, `models_cache.json`, `retention.json`, `fleet_health.json` (the owner's
  *problematic* threshold; the 7-day history beside it is observed and comes back on its own).

Nothing else - job history, analytics, artifacts and transient caches are recreatable or
disposable (owner's call). Not encrypted, by owner decision: the archive is mode 0600, like the
originals.

**Format** - one `kh-backup-YYYYMMDD-HHMMSS.tar.gz` holding the files under role names
(`gateway/data/fleet.json`, `hub/voice/voices/...`), then `manifest.json` (sizes, sha256,
versions) and `SHA256SUMS` (`shasum -a 256 -c` reads it). Opening one needs nothing but Finder
or `tar`; see docs/backup-restore.md.

**Where** - iCloud Drive `KH Backups/`, `2 Backups/` on every attached `KH-STUDIO-*` drive, and
`~/Developer/_backups/`. Each keeps the newest `keep` archives; an older one *this module named*
is moved to that volume's `_trash`, or deleted when the volume has none. Nothing else in those
folders is ever touched.

**When** - Back Up Now, plus an optional nightly run (off by default).

**Restore** - `restore()` checks every hash first, moves each current file it would replace into
`~/Developer/_trash/kh-backup-restore-<time>/`, then writes the backed-up copy (0600). Content
Pack archives that are already on the shelf, byte for byte, are left alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__, config
from .os_metadata import is_os_metadata

def notify(event: str, text: str) -> None:
    """Tell the owner on Telegram (`gateway.notifier`), when that module is there; never raises."""
    try:
        from . import notifier  # feat/telegram-alerts; absent on a build without it
    except ImportError:
        return
    try:
        notifier.notify(event, text)
    except Exception:  # noqa: BLE001 - a backup must not fail because a message did
        pass


HOME = Path.home()
ICLOUD_DIR = Path(os.environ.get("GATEWAY_BACKUP_ICLOUD")
                  or HOME / "Library/Mobile Documents/com~apple~CloudDocs/KH Backups")
LOCAL_DIR = Path(os.environ.get("GATEWAY_BACKUP_LOCAL") or HOME / "Developer/_backups")
VOLUMES = Path(os.environ.get("GATEWAY_BACKUP_VOLUMES") or "/Volumes")
TRASH_DIR = Path(os.environ.get("GATEWAY_BACKUP_TRASH") or HOME / "Developer/_trash")
HUB_DIR = Path(os.environ.get("GATEWAY_BACKUP_HUB_DIR")
               or HOME / "Library/Application Support/StudioHub Standalone")
DRIVE_PREFIX = "KH-STUDIO-"
FOLDER = "2 Backups"
NAME_RE = re.compile(r"^kh-backup-\d{8}-\d{6}\.tar\.gz$")
FORMAT = 1
META = ("manifest.json", "SHA256SUMS")
DEFAULTS: dict[str, Any] = {
    "nightly": False,
    "settings_nightly": False,
    "include_fleet": False,
    "time": "03:00",
    "keep": 5,
    "destinations": {"icloud": True, "ssd": True, "local": True},
}
CHECK_INTERVAL_S = 60.0

# Only reviewed settings files; review a new file's contents before adding it. models_cache.json is
# rebuilt from the providers, and its URLs carry query strings, so it stays out.
SETTINGS_ROLES = frozenset("gateway/data/" + name for name in (
    "fleet.json", "providers.local.json", "pools.json", "provider-concurrency.local.json",
    "model-limits.local.json", "render_optin.json", "fleet_warnings.json", "voices_state.json",
    "retention.json", "fleet_health.json", "fleet_orphans.json",
))

_lock = threading.Lock()


class BackupError(RuntimeError):
    """A sentence the owner reads."""


# --------------------------------------------------------------------------- what


def sources() -> list[tuple[str, Path]]:
    """`(role name in the archive, where it lives on this Mac)`, read at call time."""
    from . import fleet

    data = Path(config.DATA_DIR)
    return [
        ("gateway/.env", Path(config.ENV_PATH)),
        ("gateway/data/release-publisher.pem", data / "release-publisher.pem"),
        ("gateway/data/content-delivery", data / "content-delivery"),
        ("gateway/data/content-library", data / "content-library"),
        ("gateway/data/credentials.json", Path(config.CREDENTIALS_PATH)),
        ("gateway/data/fleet.json", Path(fleet.FLEET_PATH)),
        ("gateway/data/voices.json", Path(config.WRITABLE_VOICES_PATH)),
        ("gateway/data/voice-library", data / "voice-library"),
        ("hub/voice/voices", HUB_DIR / "voice" / "voices"),
        ("gateway/data/providers.local.json", data / "providers.local.json"),
        ("gateway/data/pools.json", data / "pools.json"),
        ("gateway/data/provider-concurrency.local.json", data / "provider-concurrency.local.json"),
        ("gateway/data/model-limits.local.json", data / "model-limits.local.json"),
        ("gateway/data/telegram.json", data / "telegram.json"),
        ("gateway/data/render_optin.json", data / "render_optin.json"),
        ("gateway/data/fleet_warnings.json", data / "fleet_warnings.json"),
        ("gateway/data/voices_state.json", data / "voices_state.json"),
        ("gateway/data/models_cache.json", data / "models_cache.json"),
        ("gateway/data/retention.json", data / "retention.json"),
        ("gateway/data/fleet_health.json", data / "fleet_health.json"),
        # Both orphan switches (0.2.211): the general auto-stop, and the superseded-copy stop.
        # One file, so a restore brings back the owner's two deliberate choices — which is also
        # how a site that turned one on (0.2.213 made both start off) keeps it.
        ("gateway/data/fleet_orphans.json", data / "fleet_orphans.json"),
    ]


def _files(chosen: dict[str, Any] | None = None, *, settings_only: bool = False) -> tuple[list[tuple[str, Path]], list[str]]:
    """Every file to back up, and the sources that are absent on this Mac."""
    chosen = settings() if chosen is None else chosen
    found: list[tuple[str, Path]] = []
    missing: list[str] = []
    for role, path in sources():
        if settings_only and role not in SETTINGS_ROLES:
            continue
        if role == "gateway/data/fleet.json" and not chosen["include_fleet"]:
            continue
        if path.is_file() and not path.is_symlink():
            found.append((role, path))
        elif path.is_dir() and not path.is_symlink():
            for item in sorted(path.rglob("*")):
                rel = item.relative_to(path)
                if rel.parts[0].startswith(".import-"):  # a Content Pack import still being staged
                    continue
                if any(is_os_metadata(part) for part in rel.parts):  # Finder's files are not data
                    continue
                if item.is_file() and not item.is_symlink():
                    found.append((f"{role}/{rel.as_posix()}", item))
        else:
            missing.append(role)
    return found, missing


def target_for(name: str) -> Path:
    """Where a file named in an archive goes on this Mac. Anything else is refused."""
    parts = name.split("/")
    if name.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise BackupError(f"The backup names a path that is not allowed: {name}")
    for role, path in sources():
        if name == role:
            return path
        if name.startswith(role + "/"):
            return path / name[len(role) + 1:]
    raise BackupError(f"The backup holds a file this Gateway does not restore: {name}")


# --------------------------------------------------------------------------- settings / state


def _settings_path() -> Path:
    return Path(config.DATA_DIR) / "backup.json"


def _state_path() -> Path:
    return Path(config.DATA_DIR) / "backup-state.json"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _clean_time(value: object) -> str:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise BackupError("The nightly time must look like 03:00.")
    return f"{int(match[1]):02d}:{match[2]}"


def settings() -> dict[str, Any]:
    raw = _read_json(_settings_path())
    out = json.loads(json.dumps(DEFAULTS))
    out["nightly"] = bool(raw.get("nightly", out["nightly"]))
    out["settings_nightly"] = raw.get("settings_nightly") is True
    out["include_fleet"] = raw.get("include_fleet") is True
    try:
        out["time"] = _clean_time(raw.get("time", out["time"]))
    except BackupError:
        pass
    try:
        out["keep"] = min(max(int(raw.get("keep", out["keep"])), 1), 100)
    except (TypeError, ValueError):
        pass
    for key, value in (raw.get("destinations") or {}).items():
        if key in out["destinations"]:
            out["destinations"][key] = bool(value)
    return out


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Store what was sent; omitted values keep what is there."""
    current = settings()
    if "include_fleet" in payload:
        if not isinstance(payload["include_fleet"], bool):
            raise BackupError("Include fleet roster must be true or false.")
        current["include_fleet"] = payload["include_fleet"]
    if "settings_nightly" in payload:
        if not isinstance(payload["settings_nightly"], bool):
            raise BackupError("Settings nightly must be true or false.")
        current["settings_nightly"] = payload["settings_nightly"]
    if "nightly" in payload:
        current["nightly"] = bool(payload["nightly"])
    if "time" in payload:
        current["time"] = _clean_time(payload["time"])
    if "keep" in payload:
        try:
            current["keep"] = min(max(int(payload["keep"]), 1), 100)
        except (TypeError, ValueError):
            raise BackupError("Keep must be a number of backups, 1 to 100.") from None
    for key, value in (payload.get("destinations") or {}).items():
        if key in current["destinations"]:
            current["destinations"][key] = bool(value)
    config.write_secret(_settings_path(), json.dumps(current, indent=2) + "\n")
    return current


def _state() -> dict[str, Any]:
    state = _read_json(_state_path())
    state.setdefault("destinations", {})
    return state


# --------------------------------------------------------------------------- where


def destinations(chosen: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every place a backup can go now: iCloud, this Mac, each attached KH-STUDIO drive."""
    enabled = (chosen or settings())["destinations"]
    shared_trash = TRASH_DIR / "kh-backups" if TRASH_DIR.is_dir() else None
    out = [
        {"id": "icloud", "kind": "icloud", "label": "iCloud Drive", "dir": ICLOUD_DIR,
         "available": ICLOUD_DIR.parent.is_dir(), "trash": shared_trash and shared_trash / "icloud"},
        {"id": "local", "kind": "local", "label": "This Mac", "dir": LOCAL_DIR,
         "available": True, "trash": shared_trash and shared_trash / "local"},
    ]
    try:
        drives = sorted(p for p in VOLUMES.glob(DRIVE_PREFIX + "*") if p.is_dir())
    except OSError:
        drives = []
    for drive in drives:
        trash = drive / "_trash"
        out.append({"id": f"ssd:{drive.name}", "kind": "ssd", "label": drive.name,
                    "dir": drive / FOLDER, "available": True,
                    "trash": trash / "kh-backups" if trash.is_dir() else None})
    for row in out:
        row["enabled"] = bool(enabled.get(row["kind"], True))
    return out


# --------------------------------------------------------------------------- make / check


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = int(mtime)
    tar.addfile(info, io.BytesIO(data))


def write_archive(target: Path, chosen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write one archive at `target` (created 0600, never over an existing file)."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as raw:
        return pack(raw, chosen)


def _check_settings_data(name: str, data: bytes) -> None:
    """Fail closed on credential-shaped fields or URLs; never put their values in errors."""
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise BackupError(f"{name} is not valid JSON; settings backup refused.") from None

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                key = str(key).lower().replace("-", "_")
                # credential_fields only describes which fields a provider asks for; it holds no value.
                if key != "credential_fields" and re.search(r"(^|_)(api_?key|token|password|secret|credential|authorization|private_key)(_|$)", key):
                    if child not in (None, "", False):
                        raise BackupError(f"{name} contains a credential-shaped field; settings backup refused.")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, str) and "://" in item:
            try:
                url = urlsplit(item)
                if url.username or url.password or url.query or url.fragment:
                    raise BackupError(f"{name} contains a URL with credentials or parameters; settings backup refused.")
            except ValueError:
                raise BackupError(f"{name} contains an invalid URL; settings backup refused.") from None

    check(value)


def pack(raw: Any, chosen: dict[str, Any] | None = None, *, settings_only: bool = False) -> dict[str, Any]:
    """Write the archive into an open binary file; returns its manifest."""
    files, missing = _files(chosen, settings_only=settings_only)
    entries = []
    with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=3) as tar:
        for name, path in files:
            data = path.read_bytes()
            if settings_only:
                _check_settings_data(name, data)
            _add(tar, name, data, path.stat().st_mtime)
            entries.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "gateway_version": __version__,
            "files": entries,
            "missing": missing,
        }
        now = time.time()
        _add(tar, "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode(), now)
        _add(tar, "SHA256SUMS", "".join(f"{e['sha256']}  {e['path']}\n" for e in entries).encode(), now)
    return manifest


def inspect(path: Path) -> dict[str, Any]:
    """Read an archive end to end and prove it: every file it lists is there with its hash.

    Raises `BackupError` with a sentence; returns the manifest's summary.
    """
    path = Path(path)
    hashes: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    raise BackupError(f"The backup holds something that is not a file: {member.name}")
                data = tar.extractfile(member).read()  # type: ignore[union-attr]
                if member.name == "manifest.json":
                    manifest = json.loads(data)
                elif member.name != "SHA256SUMS":
                    target_for(member.name)
                    hashes[member.name] = hashlib.sha256(data).hexdigest()
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise BackupError(f"{path.name} is not a readable backup ({exc}).") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError(f"{path.name} has no manifest this Gateway understands.")
    listed = {e["path"]: e["sha256"] for e in manifest.get("files") or []}
    if listed != hashes:
        bad = sorted(set(listed.items()) ^ set(hashes.items()))
        raise BackupError(f"{path.name} is damaged: {len(bad)} file(s) do not match the manifest.")
    return {
        "name": path.name,
        "created_at": manifest.get("created_at"),
        "host": manifest.get("host"),
        "gateway_version": manifest.get("gateway_version"),
        "files": len(listed),
        "bytes": sum(int(e.get("bytes") or 0) for e in manifest["files"]),
        "missing": manifest.get("missing") or [],
        "include_fleet": "gateway/data/fleet.json" in listed,
    }


# --------------------------------------------------------------------------- deliver / prune


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _ours(folder: Path) -> list[Path]:
    """Archives this module named, oldest first (the name carries the time)."""
    try:
        return sorted(p for p in folder.iterdir()
                      if NAME_RE.match(p.name) and p.is_file() and not p.is_symlink())
    except OSError:
        return []


def prune(folder: Path, keep: int, trash: Path | None) -> int:
    """Keep the newest `keep` of our archives; move older ones to `trash` (same volume).

    A backup is never unlinked. If there is no trash place, or the move fails, the archive stays where
    it is and is reported by the returned count of archives actually moved.
    """
    old = _ours(folder)[:-keep] if keep > 0 else []
    moved = 0
    for path in old:
        if trash is None:
            continue
        try:
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / path.name
            if target.exists():
                continue
            os.rename(path, target)
            moved += 1
        except OSError:
            continue   # keep it in place; never replace the copy the owner might need
    return moved


def _deliver(archive: Path, dest: dict[str, Any], keep: int) -> int:
    folder: Path = dest["dir"]
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / (archive.name + ".partial")
    shutil.copyfile(archive, partial)
    try:
        os.chmod(partial, 0o600)
    except OSError:
        pass  # a drive without permissions (exFAT) still takes the copy
    if _sha(partial) != _sha(archive):
        partial.unlink(missing_ok=True)
        raise OSError("the copy did not read back identical")
    os.replace(partial, folder / archive.name)
    return prune(folder, keep, dest["trash"])


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB"


def run(reason: str = "manual") -> dict[str, Any]:
    """Make one archive, check it, copy it to every enabled place that is there, prune."""
    if not _lock.acquire(blocking=False):
        raise BackupError("A backup is already running.")
    try:
        chosen = settings()
        started = time.time()
        name = datetime.fromtimestamp(started).strftime("kh-backup-%Y%m%d-%H%M%S.tar.gz")
        state = _state()
        results: dict[str, dict[str, Any]] = {}
        work = Path(tempfile.mkdtemp(prefix="kh-backup-"))  # 0700, per-user
        archive = work / name
        try:
            manifest = write_archive(archive, chosen)
            inspect(archive)
            size = archive.stat().st_size
            for dest in destinations(chosen):
                if not (dest["enabled"] and dest["available"]):
                    continue
                record = state["destinations"].setdefault(dest["id"], {})
                record.update(label=dest["label"], path=str(dest["dir"]), last_attempt_at=started)
                try:
                    pruned = _deliver(archive, dest, chosen["keep"])
                except OSError as exc:
                    record["last_error"] = f"{type(exc).__name__}: {exc}"
                    results[dest["id"]] = {"ok": False, "label": dest["label"], "error": record["last_error"]}
                else:
                    record.update(last_success_at=started, last_name=name, last_bytes=size, last_error=None)
                    results[dest["id"]] = {"ok": True, "label": dest["label"], "pruned": pruned}
        except (OSError, BackupError) as exc:
            summary = {"ok": False, "reason": reason, "started_at": started, "name": name,
                       "error": str(exc), "destinations": results}
            state["last_run"] = summary
            config.write_secret(_state_path(), json.dumps(state, indent=2) + "\n")
            notify("backup", f"Backup failed: {exc}")
            return summary
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
        ok = [r["label"] for r in results.values() if r["ok"]]
        failed = [f"{r['label']} ({r['error']})" for r in results.values() if not r["ok"]]
        summary = {
            "ok": bool(ok) and not failed, "reason": reason, "started_at": started, "name": name,
            "bytes": size, "files": len(manifest["files"]), "missing": manifest["missing"],
            "seconds": round(time.time() - started, 2), "destinations": results,
            "error": None if ok else "No backup place was available.",
        }
        state["last_run"] = summary
        config.write_secret(_state_path(), json.dumps(state, indent=2) + "\n")
        text = f"Backup {name}: {_mb(size)}, {len(manifest['files'])} files, saved to {', '.join(ok) or 'nowhere'}."
        if failed:
            text += f" Failed: {'; '.join(failed)}."
        notify("backup", text)
        return summary
    finally:
        _lock.release()


# --------------------------------------------------------------------------- restore


def _write_private(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, scratch = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, target)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def restore(path: Path) -> dict[str, Any]:
    """Put a backup's files back. Checks every hash first; replaced files go to _trash first."""
    path = Path(path)
    with _lock:
        summary = inspect(path)
        safety = TRASH_DIR / f"kh-backup-restore-{datetime.now():%Y%m%d-%H%M%S}"
        written = unchanged = moved = 0
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                if member.name in META:
                    continue
                target = target_for(member.name)
                data = tar.extractfile(member).read()  # type: ignore[union-attr]
                if target.is_file() and target.read_bytes() == data:
                    unchanged += 1
                    continue
                if target.exists() or target.is_symlink():
                    keep = safety / member.name
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(keep))
                    moved += 1
                _write_private(target, data)
                written += 1
        return {**summary, "written": written, "unchanged": unchanged, "moved_to_trash": moved,
                "safety_copy": str(safety) if moved else None}


# --------------------------------------------------------------------------- status / schedule


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def status() -> dict[str, Any]:
    chosen, state = settings(), _state()
    files, missing = _files(chosen)
    rows = []
    for dest in destinations(chosen):
        record = state["destinations"].get(dest["id"], {})
        rows.append({**record, "id": dest["id"], "kind": dest["kind"], "label": dest["label"],
                     "path": str(dest["dir"]), "enabled": dest["enabled"],
                     "available": dest["available"], "count": len(_ours(dest["dir"]))})
    attached = {row["id"] for row in rows}
    for key, record in sorted(state["destinations"].items()):
        if key.startswith("ssd:") and key not in attached:  # a drive that is not plugged in now
            rows.append({**record, "id": key, "kind": "ssd", "label": key[4:],
                         "enabled": chosen["destinations"]["ssd"], "available": False, "count": None})
    return {
        "settings": chosen,
        "running": _lock.locked(),
        "last_run": state.get("last_run"),
        "included": {"files": len(files), "bytes": sum(_size(p) for _, p in files),
                     "missing": missing},
        "destinations": rows,
    }


def due(now: datetime, chosen: dict[str, Any], state: dict[str, Any]) -> bool:
    """Nightly is on, today's time has passed and nothing has run since it."""
    if not chosen["nightly"]:
        return False
    hour, minute = map(int, chosen["time"].split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last = float((state.get("last_run") or {}).get("started_at") or 0)
    return now >= slot and last < slot.timestamp()


_nightly_state_error: dict[str, Any] | None = None


def nightly_settings_status() -> dict[str, Any]:
    state = _read_json(Path(config.DATA_DIR) / "nightly-settings-state.json")
    if _nightly_state_error is not None:
        state["last_failure"] = _nightly_state_error
    folder = Path(config.DATA_DIR) / "backups/nightly"
    archives = _ours(folder)
    latest = None
    if archives:
        path = archives[-1]
        try:
            latest = {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha(path)}
        except OSError as exc:
            state["archive_error"] = f"{type(exc).__name__}: {exc}"
    return {"enabled": settings()["settings_nightly"], "last_success": state.get("last_success"),
            "last_failure": state.get("last_failure"), "latest": latest,
            "archive_error": state.get("archive_error")}


def _save_nightly_state(path: Path, state: dict[str, Any]) -> None:
    global _nightly_state_error
    try:
        config.write_secret(path, json.dumps(state, indent=2) + "\n")
    except OSError as exc:
        _nightly_state_error = {"at": time.time(), "reason": f"Could not save backup status: {type(exc).__name__}: {exc}"}
        print(f"[backup] {_nightly_state_error['reason']}", flush=True)
        raise
    _nightly_state_error = None


def run_nightly_settings() -> dict[str, Any]:
    """Build beside the final archive, verify it, then atomically publish it."""
    from . import retention

    with _lock:
        folder = Path(config.DATA_DIR) / "backups/nightly"
        state_path = Path(config.DATA_DIR) / "nightly-settings-state.json"
        state = _read_json(state_path)
        started = time.time()
        name = datetime.fromtimestamp(started).strftime("kh-backup-%Y%m%d-%H%M%S.tar.gz")
        partial = folder / (name + ".partial")
        previous_failure = state.get("last_failure")
        state["last_failure"] = {"at": started, "reason": "The backup did not finish."}
        _save_nightly_state(state_path, state)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            final = folder / name
            if final.exists():
                raise BackupError("A nightly archive already exists for this second.")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as raw:
                pack(raw, settings(), settings_only=True)
                raw.flush()
                os.fsync(raw.fileno())
            inspect(partial)
            os.rename(partial, final)
            for old in _ours(folder)[:-14]:
                if not retention.retire(old, root=folder, namespace="nightly-backups"):
                    raise BackupError(f"Could not move {old.name} to Gateway trash.")
        except Exception as exc:
            if partial.exists():
                retention.retire(partial, root=folder, namespace="nightly-backups-incomplete")
            state["last_failure"] = {"at": started, "reason": f"{type(exc).__name__}: {exc}"}
            _save_nightly_state(state_path, state)
            print(f"[backup] nightly settings failed: {state['last_failure']['reason']}", flush=True)
            return {"ok": False, **state["last_failure"]}
        state["last_success"] = {"at": started, "name": name}
        state["last_failure"] = previous_failure
        _save_nightly_state(state_path, state)
        return {"ok": True, **state["last_success"]}


def settings_due(now: datetime, chosen: dict[str, Any], state: dict[str, Any]) -> bool:
    if not chosen["settings_nightly"]:
        return False
    hour, minute = map(int, chosen["time"].split(":"))
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last = max(float((state.get(key) or {}).get("at") or 0) for key in ("last_success", "last_failure"))
    return now >= slot and last < slot.timestamp()


_scheduler: threading.Thread | None = None


def start_scheduler(interval: float = CHECK_INTERVAL_S) -> None:
    """The nightly run. Started from the app's lifespan, like the retention pruner."""
    global _scheduler
    if _scheduler is not None and _scheduler.is_alive():
        return

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                chosen = settings()
                if settings_due(datetime.now(), chosen, _read_json(Path(config.DATA_DIR) / "nightly-settings-state.json")):
                    run_nightly_settings()
                if due(datetime.now(), chosen, _state()):
                    result = run("nightly")
                    print(f"[backup] nightly {result['name']}: ok={result['ok']}", flush=True)
            except Exception as exc:  # noqa: BLE001 - never take the gateway down
                print(f"[backup] scheduler failed: {type(exc).__name__}: {exc}", flush=True)

    _scheduler = threading.Thread(target=loop, name="backup-nightly", daemon=True)
    _scheduler.start()
