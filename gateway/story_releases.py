"""Story Studio releases and the Story Macs that fetch them.

The owner's pain (2026-10-07): every Story update was a walk to the Mac mini, a DMG and an
`xattr -d com.apple.quarantine` by hand. The Gateway already signs Studio Hub releases with its
publisher key (`release_publisher`); this module reuses that key and that signer for Story:

* **Publish** (`publish`): the admin names a Story DMG or a .zip of the .app on this Mac. The
  version and build are read **from the bundle's own Info.plist**, never typed (a wrong version is
  signed exactly like a right one). The artifact is copied into `DATA_DIR/story-release/` and a
  descriptor is signed with the same publisher key as Hub releases.
* **Serve** (`/v1/apps/story/...`, customer bearer auth): the descriptor plus the publisher's
  public key, and the file. Story verifies the signature against the key it pinned, and the
  artifact's sha256 against the signed one, before it stages anything.
* **Check-in** (`checkin`): each Story Mac reports its version, channels, queue and update phase
  every 30 minutes. The answer carries the current release and whether the admin pressed
  "Update now" for that Mac.

Canonical bytes are `release_publisher.canonical_bytes` (sorted keys, no whitespace, UTF-8) and
the signature is over the descriptor without `signature`, exactly as for Hub releases.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import config, release_publisher

SCHEMA = "storystudio.app-release.v1"
APP_ID = "story"
#: A Story Mac checks in every 30 minutes; it is shown as offline after missing two and a half.
CHECKIN_EVERY_S = 30 * 60
OFFLINE_AFTER_S = 75 * 60
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ '()-]{0,99}$")
_PHASES = {"idle", "downloading", "ready", "draining", "installing", "installed",
           "rolled_back", "failed"}
_lock = threading.Lock()  # one publish / check-in writer at a time; readers need no lock


class StoryReleaseError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "STORY_RELEASE_ERROR") -> None:
        self.message, self.status, self.code = message, status, code
        super().__init__(message)


def release_dir() -> Path:
    return Path(config.DATA_DIR) / "story-release"


def _release_file() -> Path:
    return release_dir() / "release.json"


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
# Reading the bundle's own version
# ---------------------------------------------------------------------------

def _plist_versions(data: bytes) -> tuple[str, str]:
    try:
        info = plistlib.loads(data)
    except Exception as exc:  # plistlib raises several types for a bad file
        raise StoryReleaseError("The app's Info.plist could not be read.", 400,
                                "STORY_RELEASE_BAD_BUNDLE") from exc
    version = str(info.get("CFBundleShortVersionString") or "").strip()
    build = str(info.get("CFBundleVersion") or version).strip()
    if release_publisher.version_key(version) is None:
        raise StoryReleaseError(f"The app reports no usable version ({version!r}).", 400,
                                "STORY_RELEASE_BAD_BUNDLE")
    return version, build


def read_versions(artifact: Path) -> tuple[str, str]:
    """(version, build) from the Story .app inside a .zip or a .dmg."""
    if artifact.suffix.lower() == ".zip":
        with zipfile.ZipFile(artifact) as archive:
            names = [n for n in archive.namelist()
                     if re.fullmatch(r"[^/]+\.app/Contents/Info\.plist", n)]
            if len(names) != 1:
                raise StoryReleaseError("The .zip must hold exactly one .app at its top level.", 400,
                                        "STORY_RELEASE_BAD_BUNDLE")
            return _plist_versions(archive.read(names[0]))
    if artifact.suffix.lower() == ".dmg":
        with release_publisher._mounted(artifact) as root:  # noqa: SLF001 - same module family
            apps = sorted(root.glob("*.app"))
            if len(apps) != 1:
                raise StoryReleaseError("The disk image must hold exactly one .app.", 400,
                                        "STORY_RELEASE_BAD_BUNDLE")
            return _plist_versions((apps[0] / "Contents/Info.plist").read_bytes())
    raise StoryReleaseError("A Story release is a .dmg or a .zip of the .app.", 400,
                            "STORY_RELEASE_BAD_TYPE")


# ---------------------------------------------------------------------------
# Publish and serve
# ---------------------------------------------------------------------------

def _artifact(descriptor: dict[str, Any]) -> Path:
    """Where a descriptor's file lives: `<sha256>/<filename>`, so a descriptor and its bytes cannot
    drift apart. A re-publish writes a new path; it never overwrites the file a descriptor offers."""
    return release_dir() / str(descriptor.get("sha256") or "") / str(descriptor.get("filename") or "")


def current() -> dict[str, Any] | None:
    """The signed descriptor being offered, or None. Never a descriptor whose file is missing or
    the wrong size, so no Mac is offered a release it cannot finish downloading."""
    descriptor = _read_json(_release_file()).get("descriptor")
    if not isinstance(descriptor, dict):
        return None
    try:
        return descriptor if _artifact(descriptor).stat().st_size == descriptor.get("bytes") else None
    except OSError:
        return None


def open_artifact() -> tuple[Any, dict[str, Any]] | None:
    """The offered artifact, already open, with its descriptor. Opened here (not by path later) so a
    re-publish that moves the old file to the Trash cannot pull it out from under a download that has
    started: an open file survives the rename."""
    for _ in range(3):  # a publish can swap the release between reading the descriptor and opening
        descriptor = current()
        if not descriptor:
            return None
        try:
            return open(_artifact(descriptor), "rb"), descriptor
        except OSError:
            continue
    return None


def offer() -> dict[str, Any]:
    """What a Story Mac is told: the descriptor plus the key it was signed with."""
    descriptor = current()
    if not descriptor:
        return {"release": None, "publisher": None}
    ident = release_publisher.identity()
    return {"release": descriptor, "publisher": {
        "algorithm": "ed25519", "public_key_base64": ident["public_key_base64"],
        "fingerprint": ident["fingerprint"]}}


def publish(artifact: str | Path) -> dict[str, Any]:
    source = Path(artifact).expanduser()
    if not source.is_file():
        raise StoryReleaseError(f"No file at {source}.", 400, "STORY_RELEASE_MISSING")
    version, build = read_versions(source)
    filename = re.sub(r"[^A-Za-z0-9._ -]+", "-", source.name)
    target_dir = release_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = target_dir / f".incoming-{uuid.uuid4().hex}"  # dot-name: never served, never a release path
    try:
        shutil.copyfile(source, staged)
        _fsync(staged)
        sha256, size = release_publisher._hash_file(staged)  # noqa: SLF001 - streamed hash
        descriptor: dict[str, Any] = {
            "schema": SCHEMA,
            "schema_version": 1,
            "app": APP_ID,
            "version": version,
            "build": build,
            "filename": filename,
            "bytes": size,
            "sha256": sha256,
            "published_at": int(time.time()),
            "publisher_id": release_publisher.PUBLISHER_ID,
            "publisher_key_id": release_publisher.identity()["fingerprint"],
        }
        descriptor["signature"] = release_publisher.sign_descriptor(descriptor)
        final = _artifact(descriptor)
        with _lock:
            old = _artifact(prev) if (prev := _read_json(_release_file()).get("descriptor")) else None
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)  # complete and durable under its final name first...
            _fsync(final.parent)
            _write_json(_release_file(), {"descriptor": descriptor, "source": str(source)})  # ...then offered
            if old and old != final and old.is_file():
                _to_trash(old)
                try:
                    old.parent.rmdir()  # its now-empty <sha256> folder
                except OSError:
                    pass
    finally:
        if staged.exists():  # a failed publish leaves nothing half-written lying around
            _to_trash(staged)
    return {"descriptor": descriptor}


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _to_trash(path: Path) -> None:
    """Retire a superseded or failed staged release using the shared runtime policy."""
    from . import retention
    if not retention.retire(path, root=release_dir(), namespace="story-release"):
        print("[story-release] Could not retire a superseded/staged release; the file remains in the release store.", flush=True)


# ---------------------------------------------------------------------------
# Story Macs
# ---------------------------------------------------------------------------

def _text(value: Any, limit: int = 200) -> str | None:
    return str(value).strip()[:limit] or None if isinstance(value, (str, int, float)) else None


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def checkin(payload: dict[str, Any]) -> dict[str, Any]:
    """Record one Story Mac's report; answer with the release and any "Update now"."""
    instance = _text(payload.get("instance_id"), 100)
    if not instance or not _INSTANCE_ID.match(instance):
        raise StoryReleaseError("instance_id is the machine name.", 422, "STORY_BAD_CHECKIN")
    queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    update = payload.get("update") if isinstance(payload.get("update"), dict) else {}
    phase = _text(update.get("phase"), 20)
    record = {
        "instance_id": instance,
        "machine": _text(payload.get("machine")) or instance,
        "version": _text(payload.get("version"), 40),
        "channels": _count(payload.get("channels")),
        "queue": {key: _count(queue.get(key)) for key in ("running", "queued", "paused")}
        | {"held": queue.get("held") is True},
        "update": {"phase": phase if phase in _PHASES else None,
                   "version": _text(update.get("version"), 40),
                   "error": _text(update.get("error"), 400)},
        "server_mode": payload.get("server_mode") is True,
        "last_seen": time.time(),
    }
    if "request_id" in update:
        record["update"]["request_id"] = _text(update.get("request_id"), 100) or None
    release = current()
    with _lock:
        rows = _read_json(_instances_file())
        previous = rows.get(instance) if isinstance(rows.get(instance), dict) else {}
        requested = previous.get("update_requested_at")
        request_id = previous.get("update_request_id")
        delivered = previous.get("update_request_delivered") is True
        failed = record["update"]["phase"] in ("rolled_back", "failed")
        if "request_id" in update:
            # A stale report or lost response cannot acknowledge a new attempt.
            failed_attempt = bool(request_id) and record["update"]["request_id"] == request_id
        else:
            # Legacy reports cannot name attempts. Preserve the first old failure until delivery;
            # subsequent failures stop the command. Updated Macs use the exact identity above.
            old_failure = previous.get("update_request_previous_failure")
            failed_attempt = delivered or not old_failure or record["update"] != old_failure
        if requested and (not release or not _behind(record["version"], release)
                          or (failed and failed_attempt)):
            requested = None
        record["update_requested_at"] = requested
        record["update_request_id"] = request_id if requested else None
        record["update_request_delivered"] = bool(requested)
        record["update_request_previous_failure"] = previous.get("update_request_previous_failure") if requested else None
        rows[instance] = record
        _write_json(_instances_file(), rows)
    return {**offer(), "update_now": bool(requested),
            "update_request_id": request_id if requested else None}


def _behind(version: str | None, release: dict[str, Any]) -> bool:
    mine = release_publisher.version_key(version or "")
    theirs = release_publisher.version_key(str(release.get("version") or ""))
    return mine is not None and theirs is not None and mine < theirs


def request_update(instance: str) -> dict[str, Any]:
    with _lock:
        rows = _read_json(_instances_file())
        row = rows.get(instance)
        if not isinstance(row, dict):
            raise StoryReleaseError("No Story Mac by that name has checked in.", 404, "STORY_UNKNOWN")
        row["update_requested_at"] = time.time()
        row["update_request_id"] = uuid.uuid4().hex
        row["update_request_delivered"] = False
        update = row.get("update") or {}
        row["update_request_previous_failure"] = update if update.get("phase") in ("failed", "rolled_back") else None
        _write_json(_instances_file(), rows)
    return status_row(row, current(), time.time())


def status_row(row: dict[str, Any], release: dict[str, Any] | None, now: float) -> dict[str, Any]:
    phase = (row.get("update") or {}).get("phase")
    last_seen = row.get("last_seen") or 0
    if now - last_seen > OFFLINE_AFTER_S:
        status = "offline"
    elif phase in ("downloading", "ready", "draining", "installing") or row.get("update_requested_at"):
        status = "updating"
    elif phase in ("rolled_back", "failed"):
        status = "failed"
    elif release and _behind(row.get("version"), release):
        status = "update_available"
    else:
        status = "up_to_date"
    return {**row, "status": status, "age_seconds": round(now - last_seen, 1) if last_seen else None,
            "next_check_by": last_seen + CHECKIN_EVERY_S if last_seen else None}


def instances() -> dict[str, Any]:
    """Every Story Mac that has checked in, with its standing against the release."""
    release, now = current(), time.time()
    rows = [status_row(row, release, now) for row in _read_json(_instances_file()).values()
            if isinstance(row, dict)]
    rows.sort(key=lambda r: str(r.get("machine") or "").lower())
    return {"release": release, "instances": rows}
