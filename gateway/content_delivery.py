"""Durable private Content Pack authority and Story delivery."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
from typing import Any
from uuid import uuid4

from . import config, content_packs, node_tool


SUPPORTED_KINDS = [
    "series-formula",
    "topic-pack",
    "visual-style",
    "thumbnail-recipe",
    # Story 1.4.216 accepted these two, so the shelf may publish them. Safe to offer
    # before every Mac updates because `products()` withholds a product from a client
    # older than its `min_story_version`: an older app never receives a row whose kind
    # its parser does not know.
    "lettering-style",
    "metadata-formula",
]
#: The historical Store identities this build ships, so an install with no Story
#: Studio source can still refuse to reissue them. Exported from the owner's
#: authority by store-analysis/export_historical_identities.py: no keys inside.
_HISTORICAL = Path(__file__).with_name("data") / "historical-identities.json"
_SIGNER = Path(__file__).with_name("content_signing.mjs")
_STATE_MAX = 4 * 1024 * 1024
_PUBLIC_MAX = 1024 * 1024
_MAX_RELEASES = 1000
_DIGEST = re.compile(r"[a-f0-9]{64}")
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class DeliveryError(ValueError):
    def __init__(self, detail: str, status: int = 422, code: str = "CONTENT_DELIVERY_REFUSED") -> None:
        super().__init__(detail)
        self.status = status
        self.code = code


def _root() -> Path:
    return Path(config.DATA_DIR) / "content-delivery"


def _state_path() -> Path:
    return _root() / "authority.json"


def _key_path() -> Path:
    return _root() / "signing-key.pem"


def _marker_path() -> Path:
    return _root() / "initialized"


def _now(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _node_request(script: Path, request: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    try:
        completed = node_tool.run_process(
            [node_tool.node_executable(), str(script)],
            encoded,
            node_tool.tool_environment(),
            timeout=30,
            output_limit=_PUBLIC_MAX,
        )
    except (node_tool.NodeToolError, OSError) as exc:
        raise DeliveryError("Content delivery prerequisites are unavailable.", 503,
                            "CONTENT_DELIVERY_UNAVAILABLE") from exc
    try:
        result = content_packs._json(completed.stdout)
    except content_packs.PackError as exc:
        raise DeliveryError("Content delivery helper returned invalid output.", 503, "CONTENT_DELIVERY_UNAVAILABLE") from exc
    if not isinstance(result, dict) or completed.returncode != 0 or "error" in result:
        detail = result.get("error") if isinstance(result, dict) else None
        if not isinstance(detail, str) or not detail or len(detail) > 400:
            detail = "Content delivery prerequisites are unavailable."
        raise DeliveryError(detail, 503, "CONTENT_DELIVERY_UNAVAILABLE")
    return result


def _shipped_baseline() -> dict[str, Any] | None:
    """The historical identities this build carries, or None when it carries none.

    None is not "nothing to refuse": it means this build cannot state the guard, so
    setup must fail closed rather than initialize an unguarded authority.
    """
    try:
        if _HISTORICAL.stat().st_size > 1024 * 1024:
            return None
        payload = json.loads(_HISTORICAL.read_text())
    except (OSError, ValueError):
        return None
    revision = payload.get("baseline_revision")
    pack_ids = payload.get("pack_ids")
    family_ids = payload.get("family_ids")
    if (type(revision) is not int or revision < 1
            or not isinstance(pack_ids, list) or not isinstance(family_ids, list)
            or any(not isinstance(value, str) for value in pack_ids + family_ids)):
        return None
    return {"registry_revision": revision, "pack_ids": pack_ids, "family_ids": family_ids}





_inspection_lock = threading.Lock()
_inspection: dict[str, Any] = {"stamp": None, "result": None}


def _key_stamp() -> tuple[int, ...] | None:
    """The signing key file's identity, or None when it cannot be stated safely.

    None means "do not trust a cache": a deleted, replaced or symlinked key goes
    back through a real inspection, so the failure stays fail-closed.
    """
    try:
        info = _key_path().lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _signing(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sign, or inspect the signing identity once per unchanged key file.

    The inspection is a Node spawn measured at ~116 ms and it runs on every
    configured content request, because `_read_state` re-verifies that the key still
    matches the stored authority. The answer can only change when the key file
    changes, so it is cached against the file's own identity — and the identity is
    re-stated before the result is stored, so a key swapped mid-inspection is never
    cached under the new key's stamp. The authority comparison in `_read_state`
    stays, so a stale entry could not pass as a match anyway.

    Signing is never cached: it is a per-payload operation.
    """
    if action != "inspect":
        return _signing_uncached(action, payload)
    stamp = _key_stamp()
    if stamp is not None:
        with _inspection_lock:
            if _inspection["stamp"] == stamp:
                return _inspection["result"]
    result = _signing_uncached("inspect")
    if stamp is not None and _key_stamp() == stamp:
        with _inspection_lock:
            _inspection["stamp"] = stamp
            _inspection["result"] = result
    return result


def _signing_uncached(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"action": action, "private_key_path": str(_key_path())}
    if payload is not None:
        request["payload"] = payload
    result = _node_request(_SIGNER, request)
    if action == "sign":
        if not isinstance(result.get("signature"), str) or not result["signature"]:
            raise DeliveryError("Signing operation returned invalid output.", 503, "CONTENT_DELIVERY_UNAVAILABLE")
    else:
        required = ("key_id", "public_key_base64", "public_key_fingerprint")
        if any(not isinstance(result.get(key), str) or not result[key] for key in required):
            raise DeliveryError("Signing identity returned invalid public metadata.", 503, "CONTENT_DELIVERY_UNAVAILABLE")
    return result


def _read_file(path: Path, maximum: int) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise DeliveryError("Safe private storage reads are unavailable.", 503, "CONTENT_DELIVERY_UNAVAILABLE")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size < 1
                or before.st_size > maximum or stat.S_IMODE(before.st_mode) != 0o600):
            raise DeliveryError("Private Content Pack authority storage is damaged.", 503, "CONTENT_AUTHORITY_DAMAGED")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (len(data) > maximum or any(getattr(before, name) != getattr(after, name) for name in stable)
                or stat.S_ISLNK(current.st_mode) or current.st_dev != after.st_dev or current.st_ino != after.st_ino):
            raise DeliveryError("Private Content Pack authority storage changed while being read.", 503, "CONTENT_AUTHORITY_DAMAGED")
        return bytes(data)
    except DeliveryError:
        raise
    except OSError as exc:
        raise DeliveryError("Private Content Pack authority storage is missing or unreadable.", 503, "CONTENT_AUTHORITY_DAMAGED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_state() -> dict[str, Any]:
    try:
        root = _root().lstat()
    except OSError as exc:
        raise DeliveryError("Private Content Pack authority storage is missing.", 503, "CONTENT_AUTHORITY_DAMAGED") from exc
    if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode) or stat.S_IMODE(root.st_mode) != 0o700:
        raise DeliveryError("Private Content Pack authority storage is unsafe.", 503, "CONTENT_AUTHORITY_DAMAGED")
    if _read_file(_marker_path(), 64) != b"gateway-private-content-authority-v1\n":
        raise DeliveryError("Private Content Pack authority initialization marker is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
    try:
        state = content_packs._json(_read_file(_state_path(), _STATE_MAX))
    except content_packs.PackError as exc:
        raise DeliveryError("Private Content Pack authority state is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED") from exc
    allowed_keys = {"schema_version", "authority", "releases", "removed"}
    if (not isinstance(state, dict) or set(state) - allowed_keys or {"schema_version", "authority", "releases"} - set(state)
            or state.get("schema_version") != 1 or not isinstance(state.get("authority"), dict)
            or not isinstance(state.get("releases"), list) or len(state["releases"]) > _MAX_RELEASES
            or not isinstance(state.get("removed", []), list)):
        raise DeliveryError("Private Content Pack authority state is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
    authority = state["authority"]
    text_fields = (
        "account_id", "key_id", "public_key_base64", "public_key_fingerprint", "created_at",
    )
    if (any(not isinstance(authority.get(key), str) or not authority[key] for key in text_fields)
            or type(authority.get("baseline_revision")) is not int
            or not isinstance(authority.get("baseline_pack_ids"), list)
            or not isinstance(authority.get("baseline_family_ids"), list)):
        raise DeliveryError("Private Content Pack authority identity is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
    signing = _signing("inspect")
    if any(signing[key] != authority[key] for key in ("key_id", "public_key_base64", "public_key_fingerprint")):
        raise DeliveryError("Private Content Pack signing identity no longer matches setup.", 503, "CONTENT_AUTHORITY_DAMAGED")
    return state


def _atomic_state(state: dict[str, Any]) -> None:
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > _STATE_MAX:
        raise DeliveryError("Private Content Pack authority state exceeds its safe limit.", 507, "CONTENT_DELIVERY_CAPACITY")
    temporary = _root() / f".authority-{uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short authority write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, _state_path())
        directory = os.open(_root(), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise DeliveryError("Private Content Pack authority state could not be stored.", 503, "CONTENT_DELIVERY_UNAVAILABLE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mark_initialized() -> None:
    descriptor = None
    try:
        descriptor = os.open(_marker_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        marker = b"gateway-private-content-authority-v1\n"
        if os.write(descriptor, marker) != len(marker):
            raise OSError("short initialization marker write")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        directory = os.open(_root(), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise DeliveryError("Private Content Pack authority could not be initialized.", 503,
                            "CONTENT_DELIVERY_UNAVAILABLE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def _lock():
    root = _root()
    descriptor = None
    try:
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise DeliveryError("Private Content Pack authority storage is unsafe.", 503, "CONTENT_AUTHORITY_DAMAGED")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        lock_info = os.fstat(descriptor)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or stat.S_IMODE(lock_info.st_mode) != 0o600):
            raise DeliveryError("Private Content Pack authority lock is unsafe.", 503,
                                "CONTENT_AUTHORITY_DAMAGED")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except DeliveryError:
        raise
    except OSError as exc:
        raise DeliveryError("Private Content Pack authority storage is unavailable.", 503, "CONTENT_DELIVERY_UNAVAILABLE") from exc
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _public_authority(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state["authority"][key] for key in (
        "account_id", "key_id", "public_key_fingerprint", "created_at", "baseline_revision",
    )}


def _version(value: str) -> tuple[int, int, int]:
    matched = _VERSION.fullmatch(value)
    if not matched:
        raise DeliveryError("Release version is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
    return tuple(int(part) for part in matched.groups())


_availability_lock = threading.Lock()
_availability: dict[str, tuple[tuple[int, ...], bool]] = {}


def _available(release: dict[str, Any]) -> bool:
    """Whether the archive is still present and still matches its identity.

    The check is `read_archive`'s — a regular, non-symlinked file whose bytes hash to
    the recorded digest, at the recorded byte count — but its answer can only change
    when the file does, so it is cached against the file's own stat. Without that, a
    single catalogue request read **and hashed every archive in the library** (140
    packs, the thumbnail recipes ~7 MB each) to decide which were available, which
    measured ~155 ms per request. An entry whose stat changes is re-hashed, so a
    corrupted or replaced archive still reads as unavailable.
    """
    try:
        digest = release["archive_sha256"]
        expected = release["bytes"]
    except (KeyError, TypeError):
        return False
    path = content_packs.LIBRARY / digest / "archive.sspack"
    if path.is_symlink() or path.parent.is_symlink():
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected:
        return False
    stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with _availability_lock:
        cached = _availability.get(digest)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        data = content_packs.read_archive(digest)
    except (content_packs.PackError, KeyError, TypeError):
        return False
    result = len(data) == expected
    with _availability_lock:
        _availability[digest] = (stamp, result)
    return result


def _validate_dependency_closure(
    releases: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
    *,
    root_pack_id: str | None = None,
    root_version: str | None = None,
) -> None:
    """Require a complete acyclic closure with one exact version per logical pack."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for release in releases:
        key = (release.get("pack_id"), release.get("version"))
        if not all(isinstance(value, str) and value for value in key) or key in index:
            raise DeliveryError("Published dependency state is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
        index[key] = release
    pinned: dict[str, str] = {}
    if root_pack_id is not None and root_version is not None:
        pinned[root_pack_id] = root_version
    visited: set[tuple[str, str]] = set()

    def visit(dependency: dict[str, Any], stack: tuple[str, ...]) -> None:
        if (not isinstance(dependency, dict) or set(dependency) != {"pack_id", "version"}
                or not all(isinstance(dependency.get(key), str) and dependency[key]
                           for key in ("pack_id", "version"))):
            raise DeliveryError("Content Pack dependency identity is invalid.", 422, "CONTENT_DEPENDENCY_INVALID")
        pack_id, version = dependency["pack_id"], dependency["version"]
        if pack_id in stack:
            raise DeliveryError("Content Pack dependency closure contains a logical pack cycle.",
                                409, "CONTENT_DEPENDENCY_CYCLE")
        if pack_id in pinned and pinned[pack_id] != version:
            raise DeliveryError("Content Pack dependency closure pins conflicting versions of one pack.",
                                409, "CONTENT_DEPENDENCY_CONFLICT")
        pinned[pack_id] = version
        key = (pack_id, version)
        release = index.get(key)
        if release is None:
            raise DeliveryError("Every exact dependency must already be available on this private shelf.",
                                409, "CONTENT_DEPENDENCY_MISSING")
        if key in visited:
            return
        visited.add(key)
        children = release.get("dependencies")
        if not isinstance(children, list):
            raise DeliveryError("Published dependency state is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
        for child in children:
            visit(child, (*stack, pack_id))

    initial_stack = (root_pack_id,) if root_pack_id else ()
    for dependency in dependencies:
        visit(dependency, initial_stack)


def _release_item_ids(row: dict[str, Any]) -> list[str]:
    """A release's item ids. Rows written before multi-item packs carry `item_id`."""
    listed = row.get("item_ids")
    if isinstance(listed, list) and listed:
        return [entry for entry in listed if isinstance(entry, str)]
    single = row.get("item_id")
    return [single] if isinstance(single, str) else []


def _release_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    available = {row.get("release_id"): _available(row) for row in state["releases"] if isinstance(row, dict)}
    current: dict[str, dict[str, Any]] = {}
    for row in state["releases"]:
        if not isinstance(row, dict) or not available.get(row.get("release_id")):
            continue
        if row.get("retired"):
            continue
        family = row.get("entitlement_family")
        if not isinstance(family, str):
            continue
        if family not in current or _version(row["version"]) > _version(current[family]["version"]):
            current[family] = row
    result = []
    fields = ("release_id", "pack_id", "version", "archive_sha256", "bytes", "title", "kind", "published_at")
    for row in state["releases"]:
        if not isinstance(row, dict) or any(key not in row for key in fields):
            raise DeliveryError("Private Content Pack release state is invalid.", 503, "CONTENT_AUTHORITY_DAMAGED")
        is_available = bool(available.get(row["release_id"]))
        is_retired = bool(row.get("retired", False))
        entry = {
            **{key: row[key] for key in fields},
            "current": is_available and not is_retired and current.get(row.get("entitlement_family")) is row,
            "available": is_available,
        }
        if is_retired:
            entry["retired"] = True
            if row.get("retired_at"):
                entry["retired_at"] = row["retired_at"]
            # **Retire keeps the pack; this asks Story to remove it.** The flag is always a bool so
            # a client never has to guess, and its stamp is unix seconds because that is the wire
            # contract Story's client is built against (`retired_at` above stays the shelf's own
            # ISO stamp; the two units differ on purpose and neither is derived from the other).
            entry["remove_from_installs"] = bool(row.get("remove_from_installs", False))
            if row.get("remove_from_installs_at"):
                entry["remove_from_installs_at"] = row["remove_from_installs_at"]
        result.append(entry)
    return result


def _status(state: dict[str, Any], available: bool, detail: str) -> dict[str, Any]:
    return {
        "configured": True,
        "available": available,
        "detail": detail,
        "authority": _public_authority(state),
        "supported_kinds": SUPPORTED_KINDS,
        "releases": _release_rows(state),
        **({"removed": state["removed"]} if state.get("removed") else {}),
    }


def status() -> dict[str, Any]:
    """Read-only shelf state. Explicit setup is the only creation path."""
    if (any(path.exists() or path.is_symlink()
            for path in (_state_path(), _key_path(), _marker_path()))):
        try:
            state = _read_state()
        except DeliveryError as exc:
            return {"configured": False, "available": False, "detail": str(exc), "authority": None,
                    "supported_kinds": SUPPORTED_KINDS, "releases": []}
        ready = bool(config.gateway_api_key())
        detail = ("Private Content Pack delivery is ready." if ready
                  else "Set a nonempty Gateway API key to use private Content Pack delivery.")
        return _status(state, ready, detail)
    if not config.gateway_api_key():
        ready, detail = False, "Set a nonempty Gateway API key before setup."
    elif _shipped_baseline() is None:
        ready, detail = False, "Private Content Pack setup needs the historical identity list this build ships."
    else:
        ready, detail = True, "Ready for explicit private Content Pack authority setup."
    return {"configured": False, "available": ready, "detail": detail, "authority": None,
            "supported_kinds": SUPPORTED_KINDS, "releases": []}


def setup() -> dict[str, Any]:
    if not config.gateway_api_key():
        raise DeliveryError("Set a nonempty Gateway API key before setup.", 503, "API_KEY_NOT_CONFIGURED")
    snapshot = _shipped_baseline()
    if snapshot is None:
        raise DeliveryError(
            "Private Content Pack setup needs the historical identity list this build ships.",
            503,
            "CONTENT_DELIVERY_UNAVAILABLE",
        )
    with _lock():
        if _state_path().exists() or _state_path().is_symlink():
            return _status(_read_state(), True, "Private Content Pack delivery is ready.")
        if (_key_path().exists() or _key_path().is_symlink()
                or _marker_path().exists() or _marker_path().is_symlink()):
            raise DeliveryError("Private Content Pack authority setup is incomplete; the signing identity will not be replaced.", 409, "CONTENT_AUTHORITY_DAMAGED")
        # This durable tombstone is written before the key. Any interrupted or
        # later-deleted identity remains initialized-and-damaged, never new.
        _mark_initialized()
        signing = _signing("generate")
        state = {
            "schema_version": 1,
            "authority": {
                "account_id": f"gateway.private.account.{uuid4().hex}",
                **signing,
                "created_at": _now(),
                "baseline_revision": snapshot["registry_revision"],
                "baseline_pack_ids": snapshot["pack_ids"],
                "baseline_family_ids": snapshot["family_ids"],
            },
            "releases": [],
        }
        _atomic_state(state)
    return _status(state, True, "Private Content Pack delivery is ready.")


def _archive_metadata(digest: str, authority: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Publish metadata read from the archive's own manifest.

    Every identity fact comes from the archive itself, and the product and family
    are read from the manifest's `entitlement` — falling back to the pack-id rule
    only for an archive that declares none — so a pack keeps the identity it was
    built under instead of acquiring a new one per install.

    The receipt records *this* shelf's import. Its revision must sit above the
    baseline or the historical-identity guard would refuse the pack it was written
    for.
    """
    inspection = content_packs.inspect_archive(content_packs.read_archive(digest))
    pack_id = str(inspection["pack_id"])
    item_ids = inspection.get("item_ids") or []
    kinds = inspection.get("item_kinds") or []
    if not item_ids or len(set(item_ids)) != len(item_ids):
        raise DeliveryError("This archive declares no usable content item identities.", 422, "CONTENT_ARCHIVE_INVALID")
    if len(kinds) != 1:
        raise DeliveryError("A private release holds one kind of content.", 422, "CONTENT_ARCHIVE_INVALID")
    if kinds[0] not in SUPPORTED_KINDS:
        raise DeliveryError(f"Story does not support the kind '{kinds[0]}'.", 422, "CONTENT_ARCHIVE_INVALID")
    # Story's catalogue parser caps tags at twelve and throws on the whole catalogue
    # at thirteen — the outage that made this a release gate. No parser runs on this
    # path, so the cap is applied here, and the archive is trimmed rather than refused.
    tags = [tag for tag in (inspection.get("tags") or []) if isinstance(tag, str)][:12]
    # The pack states the product and family it was built for. Read them; fall back
    # to the pack-id rule only for an archive that declares none.
    entitlement = inspection.get("entitlement")
    entitlement = entitlement if isinstance(entitlement, dict) else {}
    declared_sku = str(entitlement.get("sku") or "").strip()
    declared_family = str(entitlement.get("family") or "").strip()
    return {
        "archive_sha256": digest,
        "archive_bytes": len(content_packs.read_archive(digest)),
        "pack_id": pack_id,
        "version": inspection["version"],
        "title": inspection.get("title") or pack_id,
        "kind": kinds[0],
        "publisher_id": inspection["publisher_id"],
        "product_id": declared_sku or pack_id.replace(".pack.", ".sku."),
        "entitlement_family": declared_family or pack_id.replace(".pack.", ".family."),
        "item_ids": item_ids,
        "min_story_version": inspection["min_app_version"],
        "description": str(inspection.get("description") or "")[:2000],
        "tags": tags,
        "changelog": "",
        "dependencies": [],
        "receipt_ref": f"import:{digest}",
        "receipt_revision": int(authority.get("baseline_revision") or 0) + 1 + len(state.get("releases") or []) + len(state.get("removed") or []),
    }


def publish_archive(digest: str, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _DIGEST.fullmatch(digest):
        raise DeliveryError("archive_sha256 must be a lowercase SHA-256 digest.")
    if not config.gateway_api_key():
        raise DeliveryError("Set a nonempty Gateway API key before publishing.", 503, "API_KEY_NOT_CONFIGURED")
    with _lock():
        state = _read_state()
        try:
            data = content_packs.read_archive(digest)
        except content_packs.PackError as exc:
            raise DeliveryError(str(exc), 410, "CONTENT_ARCHIVE_UNAVAILABLE") from exc
        if metadata is None:
            metadata = _archive_metadata(digest, state["authority"], state)
        if metadata["archive_bytes"] != len(data):
            raise DeliveryError("Validated archive byte count changed.", 409, "CONTENT_ARCHIVE_CHANGED")
        authority = state["authority"]
        existing_identity = [row for row in state["releases"] if row["pack_id"] == metadata["pack_id"]]
        same_version = next((row for row in existing_identity if row["version"] == metadata["version"]), None)
        if same_version:
            if same_version["archive_sha256"] != digest:
                raise DeliveryError("That pack/version is already published with different bytes.", 409, "CONTENT_RELEASE_CONFLICT")
            public = next(row for row in _release_rows(state) if row["release_id"] == same_version["release_id"])
            return {"release": public, "replayed": True}
        if metadata["receipt_revision"] <= authority["baseline_revision"]:
            raise DeliveryError("This release identity predates private authority setup.", 409, "CONTENT_IDENTITY_HISTORICAL")
        if not existing_identity:
            if (metadata["pack_id"] in authority["baseline_pack_ids"]
                    or metadata["entitlement_family"] in authority["baseline_family_ids"]):
                raise DeliveryError("Historical pack or family identities cannot enter the private shelf.", 409, "CONTENT_IDENTITY_HISTORICAL")
        else:
            first = existing_identity[0]
            identity_fields = ("publisher_id", "product_id", "entitlement_family", "kind")
            if (any(first[field] != metadata[field] for field in identity_fields)
                    or set(_release_item_ids(first)) != set(metadata["item_ids"])):
                raise DeliveryError("A later release changed the private product identity.", 409, "CONTENT_IDENTITY_CONFLICT")
        wanted_items = set(metadata["item_ids"])
        for other in state["releases"]:
            if other["pack_id"] != metadata["pack_id"] and (
                other["product_id"] == metadata["product_id"]
                or other["entitlement_family"] == metadata["entitlement_family"]
                or bool(wanted_items & set(_release_item_ids(other)))
            ):
                raise DeliveryError("Product, family, and item identities must remain unique.", 409, "CONTENT_IDENTITY_CONFLICT")
        available_releases = [row for row in state["releases"] if _available(row)]
        _validate_dependency_closure(
            available_releases,
            metadata["dependencies"],
            root_pack_id=metadata["pack_id"],
            root_version=metadata["version"],
        )
        if len(state["releases"]) >= _MAX_RELEASES:
            raise DeliveryError("Private shelf release capacity has been reached.", 507, "CONTENT_DELIVERY_CAPACITY")
        release_identity = f"{metadata['pack_id']}@{metadata['version']}"
        release = {
            "release_id": f"gateway.private.release.{hashlib.sha256(release_identity.encode()).hexdigest()[:24]}",
            **{key: metadata[key] for key in (
                "pack_id", "version", "title", "kind", "publisher_id", "product_id",
                "entitlement_family", "item_ids", "min_story_version", "description",
                "tags", "changelog", "dependencies", "receipt_revision", "receipt_ref",
            )},
            "archive_sha256": digest,
            "bytes": len(data),
            "published_at": _now(),
        }
        state["releases"].append(release)
        _atomic_state(state)
        public = next(row for row in _release_rows(state) if row["release_id"] == release["release_id"])
        return {"release": public, "replayed": False}


def import_and_publish(data: bytes) -> dict[str, Any]:
    """Preserve a pack archive and put it on this shelf, in one action.

    What someone does with a pack they were sent: one `.sspack` in, one product
    available. The two steps stay separate functions because the owner's flow still
    reviews an archive before publishing it.
    """
    entry = content_packs.import_archive(data)
    result = publish_archive(entry["archive_sha256"])
    return {"archive": entry, **result}


# Reading the authority is cheap now that the two things that made it expensive are
# cached against the files they depend on: the signing-identity inspection (a Node
# spawn, ~116 ms) and each archive's verification (reading and hashing every pack,
# ~155 ms for the whole catalogue). Both invalidate on the file's own stat, so a
# replaced or deleted key or archive is re-checked rather than trusted. Measured:
# `products()` 391 ms -> 8 ms, `account()` 406 ms -> 6 ms.
def _configured_state() -> dict[str, Any]:
    if not config.gateway_api_key():
        raise DeliveryError("Content Store is not connected.", 503, "CONTENT_STORE_NOT_CONNECTED")
    try:
        return _read_state()
    except DeliveryError as exc:
        if not (_state_path().exists() or _key_path().exists() or _marker_path().exists()):
            raise DeliveryError("Content Store is not connected.", 503, "CONTENT_STORE_NOT_CONNECTED") from exc
        raise


def ensure_configured() -> dict[str, Any]:
    """Read the authority once, for the caller to reuse.

    `_read_state()` is not cheap — it re-reads the authority files *and* spawns
    Node to re-inspect the signing identity (~100 ms measured). The request
    dependency and the handler both need the state, so the dependency returns it
    and the handler passes it in; reading twice per request was ~204 ms for a
    configured shelf.
    """
    return _configured_state()


def _current_releases(state: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    public = {row["release_id"]: row for row in _release_rows(state) if row["current"] and row["available"] and not row.get("retired")}
    return [(row, public[row["release_id"]]) for row in state["releases"] if row["release_id"] in public]


def _owned_releases(state: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """What a Mac that already installed a pack is still told it owns: every family with an
    available release, retired or not. Retiring hides a pack from the catalogue and blocks new
    installs (`_current_releases`), but must not make an installed pack look lost, so the
    account and restore receipt keep it, flagged `retired`. A live release of a family wins over
    a retired one; otherwise the highest version does."""
    public = {row["release_id"]: row for row in _release_rows(state) if row["available"]}
    latest: dict[str, dict[str, Any]] = {}
    for row in state["releases"]:
        if not isinstance(row, dict) or row.get("release_id") not in public:
            continue
        family = row.get("entitlement_family")
        if not isinstance(family, str):
            continue
        held = latest.get(family)
        if held is None or ((not row.get("retired")), _version(row["version"])) > (
                (not held.get("retired")), _version(held["version"])):
            latest[family] = row
    return [(latest[f], public[latest[f]["release_id"]]) for f in sorted(latest)]


# The pack kind -> wire kind spelling. Story's spelling table is mechanical except
# for one entry: the product kind for lettering has been `font_pack` since 1.4.62, so
# `lettering-style.replace("-", "_")` would be wrong.
_WIRE_KIND_EXCEPTIONS = {"lettering-style": "font_pack"}


def _wire_kind(kind: str) -> str:
    return _WIRE_KIND_EXCEPTIONS.get(kind, kind.replace("-", "_"))


def _wire_release(row: dict[str, Any], public: dict[str, Any]) -> dict[str, Any]:
    """One release as Story reads it.

    **`archive_available` is "this client can download it now", not "the file is on disk".** A
    retired release stays on the shelf, and `issue_download` refuses it (`410
    CONTENT_PRODUCT_RETIRED`, 6729f29), so its archive is not available on the wire. That is also
    what makes it agree with an entitlement's `download_available`, which Story requires
    (`download_available must match current release archive availability`): a retired family whose
    archive is still on disk used to read `download_available: false` beside
    `archive_available: true`, and Story rejected the whole receipts payload. A non-retired release
    reports exactly what it did before.
    """
    return {
        "release_id": row["release_id"], "pack_id": row["pack_id"], "version": row["version"],
        "archive_sha256": row["archive_sha256"], "archive_bytes": row["bytes"],
        "min_story_version": row["min_story_version"], "changelog": row["changelog"],
        "component_summary": {row["kind"]: 1},
        "archive_available": public["available"] and not bool(row.get("retired", False)),
    }


def _entitlement(row: dict[str, Any], public: dict[str, Any]) -> dict[str, Any]:
    is_retired = bool(row.get("retired", False))
    return {
        "entitlement_id": f"gateway.private.entitlement.{hashlib.sha256(row['entitlement_family'].encode()).hexdigest()[:24]}",
        "entitlement_family": row["entitlement_family"], "product_id": row["product_id"],
        "title": row["title"], "kind": _wire_kind(row["kind"]), "publisher_id": row["publisher_id"],
        "granted_at": row["published_at"], "purchase_id": None, "retired": is_retired,
        "current_release": _wire_release(row, public), "download_available": public["available"] and not is_retired,
        "recalled_release_versions": [], "showcase_videos": [],
    }


def account(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _configured_state()
    return _bounded({
        "account_id": state["authority"]["account_id"],
        "content_account_id": state["authority"]["account_id"],
        "available_credits": 0, "content_spendable_credits": 0,
        "owned": [{
            "entitlement_family": row["entitlement_family"], "product_id": row["product_id"],
            "title": row["title"], "kind": _wire_kind(row["kind"]), "granted_at": row["published_at"],
            "purchase_id": None, "current_release": _wire_release(row, public), "showcase_videos": [],
        } for row, public in _owned_releases(state)],
    })


def _retired_pack_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every retired release, so a Story Mac can act on the owner's ask.

    A retired pack is deliberately **absent from `products`**: it must not appear in Studio Library
    as something to install. But Story still has to learn the pack id before it can remove a copy it
    already has, and the catalogue read is the one call every Story Mac already makes. So the same
    response carries this minimal list beside the catalogue rather than a second endpoint nothing
    would call.

    The two timestamps carry different units on purpose: `retired_at` is the shelf's own ISO stamp
    (as `releases` publishes it), and `remove_from_installs_at` is unix seconds, which is the wire
    contract Story's client is built against.
    """
    rows = [row for row in _release_rows(state) if row.get("retired")]
    rows.sort(key=lambda row: (str(row.get("pack_id") or ""), _version(row["version"])))
    return [{
        "pack_id": row["pack_id"],
        "release_id": row["release_id"],
        "retired_at": row.get("retired_at"),
        "remove_from_installs": bool(row.get("remove_from_installs", False)),
        "remove_from_installs_at": row.get("remove_from_installs_at"),
    } for row in rows]


def products(story_version: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _configured_state()
    try:
        client_version = _version(story_version)
        client_version_known = True
    except DeliveryError:
        client_version = (0, 0, 0)
        client_version_known = False
    rows = []
    for row, public in _current_releases(state):
        compatible = client_version >= _version(row["min_story_version"])
        # Never hand a client a product its own parser may not understand. A kind the
        # client does not know throws inside `parseContentStoreCatalog` and Story then
        # answers 500 for the **whole** Content Store — one product empties every
        # other — so a pack the client is too old to install is withheld rather than
        # listed as incompatible. This is what makes it safe to publish a new kind
        # before every Mac has updated: an old client simply does not see it.
        #
        # Withheld only when the client stated a version we could parse: a malformed
        # `story_version` must not silently empty the catalogue, so it keeps the old
        # behaviour and sees everything.
        if client_version_known and not compatible:
            continue
        rows.append({
            "product_id": row["product_id"], "entitlement_family": row["entitlement_family"],
            "kind": _wire_kind(row["kind"]), "title": row["title"], "summary": row["description"],
            "description": row["description"], "publisher_id": row["publisher_id"], "credit_price": 0,
            "free": True, "owned": True, "retired": False, "successor_product_id": None,
            "current_release": _wire_release(row, public), "compatible": compatible,
            "compatibility_reason": None if compatible else f"Requires Story Studio {row['min_story_version']} or newer.",
            "bundle_components": [], "owned_component_credit_value": 0, "showcase_videos": [],
            "cover": None, "hero": None, "tags": row["tags"], "license": None, "discount": None,
        })
    return _bounded({"products": rows, "retired_packs": _retired_pack_rows(state)})


def shelves(story_version: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    visible = [row["product_id"] for row in products(story_version, state)["products"]]
    return _bounded({"shelves": ([{"shelf_id": "gateway.private", "title": "Private Content Packs",
                                   "position": 0, "product_ids": visible}] if visible else []),
                     "fallback_product_ids": []})


def public_key(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _configured_state()
    authority = state["authority"]
    key = {"key_id": authority["key_id"], "algorithm": "ed25519",
           "public_key_base64": authority["public_key_base64"], "current": True}
    return {
        "key_id": authority["key_id"], "algorithm": "ed25519",
        "public_key_base64": authority["public_key_base64"], "keys": [key],
        "payload_types": [
            "genstudio.content.download.v1", "genstudio.content.quote.v1",
            "genstudio.content.receipt.v1", "genstudio.content.restore.v1",
        ],
        "canonicalization": "json:sorted-keys,no-whitespace,utf-8",
    }


def receipts(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _configured_state()
    authority = state["authority"]
    current = _owned_releases(state)
    entitlements = [_entitlement(row, public) for row, public in current]
    now = datetime.now(timezone.utc)
    payload = {
        "typ": "genstudio.content.restore.v1", "key_id": authority["key_id"],
        "receipt_id": f"gateway.private.restore.{uuid4().hex}",
        "account_id": authority["account_id"], "content_account_id": authority["account_id"],
        "issued_at": _now(now),
        "expiry_policy": {"ownership": "permanent", "receipt_expires_at": None,
                          "refresh_recommended_after": _now(now + timedelta(days=1))},
        "entitlement_families": [row["entitlement_family"] for row, _ in current],
        "entitlements": [{
            "entitlement_family": row["entitlement_family"], "product_id": row["product_id"],
            "title": row["title"], "kind": _wire_kind(row["kind"]), "granted_at": row["published_at"],
            "purchase_id": None,
        } for row, _ in current],
        "releases": [{
            "entitlement_family": row["entitlement_family"], "release_id": row["release_id"],
            "pack_id": row["pack_id"], "version": row["version"],
            "archive_sha256": row["archive_sha256"], "archive_bytes": row["bytes"],
            "min_story_version": row["min_story_version"],
        } for row, _ in current],
    }
    return _bounded({
        "account_id": authority["account_id"], "content_account_id": authority["account_id"],
        "restore_receipt": {"receipt": payload, "receipt_signature": _signing("sign", payload)["signature"],
                            "key_id": authority["key_id"]},
        "purchase_receipts": [], "entitlements": entitlements,
    })


def release_history(family: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """One family's releases, **describing the same ownership the receipt does**.

    Story checks this against the receipt entitlement, field for field, before it will use a
    release: `history.retired === entitlement.retired` and `history.currentReleaseId ===
    entitlement.currentRelease.releaseId` (`ownedReleaseHistory`,
    `electron/server/services/content-store-delivery.ts`). Both rows must therefore come from the
    **same release row**, which is the one `_owned_releases` picked for the receipt and account —
    not `matches[0]`, which is whatever order the shelf happens to store.

    That matters for a fully retired family, whose receipt still names its last release (an
    installed pack must not look lost) while `_release_rows` marks no row `current` for the
    catalogue. Publishing `current_release_id: null` against a receipt that names a release made
    Story answer `CONTENT_PACK_OWNED_RELEASE_MISMATCH` — the next failure after the archive
    mismatch, on the same family.
    """
    state = state if state is not None else _configured_state()
    matches = [row for row in state["releases"] if row["entitlement_family"] == family]
    if not matches:
        raise DeliveryError("No such private entitlement.", 404, "CONTENT_ENTITLEMENT_NOT_FOUND")
    public = {row["release_id"]: row for row in _release_rows(state)}
    owned = next(((row, availability) for row, availability in _owned_releases(state)
                  if row["entitlement_family"] == family), None)
    described = owned[0] if owned else matches[0]
    current_release_id = described["release_id"] if owned else None
    return _bounded({
        **{key: value for key, value in _entitlement(described, public[described["release_id"]]).items()
           if key in ("entitlement_family", "entitlement_id", "product_id", "title", "kind",
                      "publisher_id", "granted_at", "retired")},
        "current_release_id": current_release_id,
        "releases": [{
            **_wire_release(row, public[row["release_id"]]), "state": "published",
            "published_at": row["published_at"],
            "current": row["release_id"] == current_release_id,
            "recalled_at": None, "recall_reason": None,
        } for row in sorted(matches, key=lambda item: _version(item["version"]))],
    })


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _grant_token(payload: dict[str, Any], api_key: str) -> str:
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(api_key.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def issue_download(release_id: str, origin: str, api_key: str,
                   state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else _configured_state()
    public = {row["release_id"]: row for row in _release_rows(state)}
    row = next((item for item in state["releases"] if item["release_id"] == release_id), None)
    if not row or not public.get(release_id, {}).get("available"):
        raise DeliveryError("No downloadable private release.", 404, "CONTENT_RELEASE_NOT_FOUND")
    if row.get("retired"):
        raise DeliveryError("This product is retired and cannot be newly installed.", 410, "CONTENT_PRODUCT_RETIRED")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=900)
    grant_id = f"gateway.private.grant.{uuid4().hex}"
    grant = {
        "typ": "genstudio.content.download.v1", "key_id": state["authority"]["key_id"],
        "grant_id": grant_id, "account_id": state["authority"]["account_id"],
        "product_id": row["product_id"], "entitlement_family": row["entitlement_family"],
        "release_id": row["release_id"], "pack_id": row["pack_id"], "version": row["version"],
        "archive_sha256": row["archive_sha256"], "archive_bytes": row["bytes"],
        "issued_at": _now(now), "expires_at": _now(expires),
    }
    token = _grant_token({"grant_id": grant_id, "release_id": release_id,
                          "account_id": state["authority"]["account_id"],
                          "key_id": state["authority"]["key_id"],
                          "archive_sha256": row["archive_sha256"], "bytes": row["bytes"],
                          "exp": int(expires.timestamp())}, api_key)
    return {
        **{key: grant[key] for key in (
            "grant_id", "account_id", "product_id", "entitlement_family", "release_id",
            "pack_id", "version", "archive_sha256", "archive_bytes", "expires_at",
        )},
        "download_url": f"{origin}/v1/content/archives/{release_id}?grant={token}",
        "ttl_seconds": 900, "grant": grant,
        "grant_signature": _signing("sign", grant)["signature"], "key_id": state["authority"]["key_id"],
    }


def resolve_release_id(entitlement_family: str, version: str,
                       state: dict[str, Any] | None = None) -> str:
    state = state if state is not None else _configured_state()
    matches = [row for row in state["releases"]
               if row["entitlement_family"] == entitlement_family and row["version"] == version]
    if len(matches) != 1 or not _available(matches[0]):
        raise DeliveryError("No downloadable private release matches that family and version.",
                            404, "CONTENT_RELEASE_NOT_FOUND")
    if matches[0].get("retired"):
        raise DeliveryError("This product is retired and cannot be newly installed.",
                            410, "CONTENT_PRODUCT_RETIRED")
    return matches[0]["release_id"]


def download_archive(release_id: str, token: str) -> bytes:
    api_key = config.gateway_api_key()
    if not api_key or len(token) > 4096 or token.count(".") != 1:
        raise DeliveryError("Download grant is invalid.", 403, "CONTENT_GRANT_INVALID")
    encoded, signature = token.split(".", 1)
    expected = _b64(hmac.new(api_key.encode(), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise DeliveryError("Download grant is invalid.", 403, "CONTENT_GRANT_INVALID")
    try:
        payload = content_packs._json(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, content_packs.PackError):
        raise DeliveryError("Download grant is invalid.", 403, "CONTENT_GRANT_INVALID") from None
    if (not isinstance(payload, dict) or set(payload) != {
            "grant_id", "release_id", "account_id", "key_id", "archive_sha256", "bytes", "exp"}
            or payload.get("release_id") != release_id or type(payload.get("exp")) is not int
            or payload["exp"] <= int(datetime.now(timezone.utc).timestamp())):
        raise DeliveryError("Download grant is invalid or expired.", 403, "CONTENT_GRANT_INVALID")
    state = _configured_state()
    row = next((item for item in state["releases"] if item["release_id"] == release_id), None)
    if (not row or payload["account_id"] != state["authority"]["account_id"]
            or payload["key_id"] != state["authority"]["key_id"]
            or payload["archive_sha256"] != row["archive_sha256"] or payload["bytes"] != row["bytes"]):
        raise DeliveryError("Download grant does not match the current private authority.", 403, "CONTENT_GRANT_INVALID")
    if not _available(row):
        raise DeliveryError("Published archive is unavailable.", 410, "CONTENT_ARCHIVE_UNAVAILABLE")
    try:
        return content_packs.read_archive(row["archive_sha256"])
    except content_packs.PackError as exc:
        raise DeliveryError(str(exc), 410, "CONTENT_ARCHIVE_UNAVAILABLE") from exc


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) > _PUBLIC_MAX:
        raise DeliveryError("Private Content Store response exceeds its safe limit.", 507, "CONTENT_DELIVERY_CAPACITY")
    return value


def retire_pack(pack_id: str, version: str | None = None, *, confirm_latest: bool = False,
                remove_from_installs: bool = False) -> dict[str, Any]:
    """Mark a published pack (pack id + version, or whole pack) as retired.

    Its archive and records stay on disk. Retired packs are hidden from the
    customer-facing catalogue / Story's Studio Library listing and cannot be newly
    installed, but existing installs keep working.

    Retiring the only or newest version hides the pack from the customer catalogue
    and requires explicit confirmation (confirm_latest=True).

    **`remove_from_installs` is a second, deliberate decision** (owner, 2026-10-29): retire alone
    hides the pack and leaves every Mac's copy alone, and only this flag asks each Story app to
    drop it. It is stored on the retired row as a bool plus unix seconds, published to Story on the
    catalogue read, and **only ever written when true** — a plain retire must not clear an ask the
    owner already made, because the ask is withdrawn by `set_remove_from_installs(..., enabled=False)`
    or by un-retiring, never by a default. A later one-off ask on an already-retired pack is the
    same field through that function, so both doors cannot drift apart.
    """
    if not pack_id:
        raise DeliveryError("pack_id is required.", 422, "PACK_ID_REQUIRED")
    with _lock():
        state = _read_state()
        all_for_pack = [row for row in state["releases"] if row.get("pack_id") == pack_id]
        if not all_for_pack:
            raise DeliveryError(f"No published release found for pack_id '{pack_id}'.", 404, "CONTENT_RELEASE_NOT_FOUND")

        sorted_releases = sorted(all_for_pack, key=lambda r: _version(r["version"]))
        newest_version = sorted_releases[-1]["version"]

        target_releases = all_for_pack if version is None else [r for r in all_for_pack if r.get("version") == version]
        if not target_releases:
            raise DeliveryError(f"No release found for pack_id '{pack_id}' version '{version}'.", 404, "CONTENT_RELEASE_NOT_FOUND")

        is_only = len(all_for_pack) == 1
        is_newest = any(r["version"] == newest_version for r in target_releases)

        survivors = [r for r in all_for_pack if r not in target_releases and not r.get("retired")]
        if (is_only or is_newest) and not confirm_latest:
            title = target_releases[0].get("title", pack_id)
            if survivors:
                shown = max(survivors, key=lambda r: _version(r["version"]))["version"]
                impact = f"Customers will be offered v{shown} instead."
            else:
                impact = "Customers will no longer see this pack in Studio Library at all."
            raise DeliveryError(
                f"'{title}' is its newest version. {impact} Macs that already installed it keep working; "
                "new installs of the retired version are blocked. Send confirm_latest: true to go ahead.",
                409,
                "CONFIRM_LATEST_REQUIRED",
            )

        now = _now()
        asked_at = _unix_now()
        for r in target_releases:
            r["retired"] = True
            r["retired_at"] = now
            if remove_from_installs:
                r["remove_from_installs"] = True
                r["remove_from_installs_at"] = asked_at

        _atomic_state(state)
        asked = any(r.get("remove_from_installs") for r in target_releases)
        return {
            "ok": True,
            "retired": True,
            "pack_id": pack_id,
            "version": version,
            "affected_versions": [r["version"] for r in target_releases],
            "remove_from_installs": asked,
            "remove_from_installs_at": _asked_at(target_releases),
            "customer_impact": ("Customers are offered the newest version that is not retired."
                                if survivors else
                                "Hidden from the customer catalogue. Existing installs keep working; new installs are blocked."),
            "story_apps": ("Every Story app is told to remove this pack the next time it checks in, "
                           "and reports what it did." if asked else
                           "Story apps keep the copy they already have."),
        }


def set_remove_from_installs(pack_id: str, version: str | None = None, *, enabled: bool = True) -> dict[str, Any]:
    """Ask every Story app to remove a retired pack — or withdraw the ask.

    The later half of the same decision `retire_pack` can make in one call. **Only a retired pack
    may carry it**: a live pack is one customers can still install, so "remove it from every Mac"
    is not a thing to set quietly on the shelf.
    """
    if not pack_id:
        raise DeliveryError("pack_id is required.", 422, "PACK_ID_REQUIRED")
    with _lock():
        state = _read_state()
        all_for_pack = [row for row in state["releases"] if row.get("pack_id") == pack_id]
        if not all_for_pack:
            raise DeliveryError(f"No published release found for pack_id '{pack_id}'.", 404, "CONTENT_RELEASE_NOT_FOUND")
        target_releases = all_for_pack if version is None else [r for r in all_for_pack if r.get("version") == version]
        if not target_releases:
            raise DeliveryError(f"No release found for pack_id '{pack_id}' version '{version}'.", 404, "CONTENT_RELEASE_NOT_FOUND")
        live = [r for r in target_releases if not r.get("retired")]
        if live:
            raise DeliveryError(
                f"'{live[0].get('title', pack_id)}' v{live[0]['version']} is not retired. "
                "Retire a pack before asking Story apps to remove it.", 409, "CONTENT_PACK_NOT_RETIRED")

        asked_at = _unix_now()
        for r in target_releases:
            if enabled:
                r["remove_from_installs"] = True
                r["remove_from_installs_at"] = asked_at
            else:
                r.pop("remove_from_installs", None)
                r.pop("remove_from_installs_at", None)

        _atomic_state(state)
        return {
            "ok": True,
            "pack_id": pack_id,
            "version": version,
            "affected_versions": [r["version"] for r in target_releases],
            "remove_from_installs": bool(enabled),
            "remove_from_installs_at": _asked_at(target_releases),
        }


def _unix_now() -> int:
    """Unix seconds, for the one timestamp whose contract is Story's (`remove_from_installs_at`)."""
    return int(datetime.now(timezone.utc).timestamp())


def _asked_at(rows: list[dict[str, Any]]) -> int | None:
    """The latest ask across the targeted rows, or None when no row carries one."""
    stamps = [row.get("remove_from_installs_at") for row in rows if row.get("remove_from_installs")]
    return max(stamps) if stamps else None


def unretire_pack(pack_id: str, version: str | None = None) -> dict[str, Any]:
    """Reactivate a retired pack or version.

    **Un-retiring withdraws the removal ask too.** A pack that is back on the shelf is one a Mac may
    install again, so leaving "remove it from every Story app" standing would delete the very copy
    the owner just made available. Whatever is already gone does not come back, and the shelf says
    so rather than implying a restore.
    """
    if not pack_id:
        raise DeliveryError("pack_id is required.", 422, "PACK_ID_REQUIRED")
    with _lock():
        state = _read_state()
        all_for_pack = [row for row in state["releases"] if row.get("pack_id") == pack_id]
        if not all_for_pack:
            raise DeliveryError(f"No published release found for pack_id '{pack_id}'.", 404, "CONTENT_RELEASE_NOT_FOUND")

        target_releases = all_for_pack if version is None else [r for r in all_for_pack if r.get("version") == version]
        if not target_releases:
            raise DeliveryError(f"No release found for pack_id '{pack_id}' version '{version}'.", 404, "CONTENT_RELEASE_NOT_FOUND")

        for r in target_releases:
            r["retired"] = False
            r.pop("retired_at", None)
            r.pop("remove_from_installs", None)
            r.pop("remove_from_installs_at", None)

        _atomic_state(state)
        return {
            "ok": True,
            "retired": False,
            "pack_id": pack_id,
            "version": version,
            "affected_versions": [r["version"] for r in target_releases],
            "remove_from_installs": False,
            "remove_from_installs_at": None,
            "story_apps": ("The removal request is withdrawn. Story apps keep the pack from now on; "
                           "a copy already removed does not come back."),
        }


#: Where a removed pack's folder goes. Deliberately not `DATA_DIR/_trash`: retention moves that
#: folder to macOS Trash after seven days, and a removal record must keep pointing at a file.
def trash_root() -> Path:
    return Path(config.DATA_DIR) / "content-trash"


def _digest_files(folder: Path) -> list[dict[str, Any]]:
    rows = []
    for item in sorted(folder.iterdir()):
        if item.is_file() and not item.is_symlink():
            rows.append({"name": item.name, "bytes": item.stat().st_size,
                         "sha256": hashlib.sha256(item.read_bytes()).hexdigest()})
    return rows


def remove_pack(pack_id: str, version: str | None = None, *, confirm: bool = False) -> dict[str, Any]:
    """Remove a retired pack. Never deletes: the archive's whole library folder (the archive
    and every sidecar beside it) is moved under `content-trash/`, a manifest entry records what,
    when, from where and its sha256, and the release leaves the active records.

    Refused unless every targeted release is retired. If the authority cannot be written after
    the move, the folder is moved back.
    """
    if not pack_id:
        raise DeliveryError("pack_id is required.", 422, "PACK_ID_REQUIRED")
    if confirm is not True:
        raise DeliveryError("Removing a pack needs explicit confirmation.", 422, "CONFIRMATION_REQUIRED")
    with _lock():
        state = _read_state()
        all_for_pack = [row for row in state["releases"] if row.get("pack_id") == pack_id]
        targets = all_for_pack if version is None else [r for r in all_for_pack if r.get("version") == version]
        if not targets:
            raise DeliveryError("No published release matches that pack and version.", 404, "CONTENT_RELEASE_NOT_FOUND")
        live = [r for r in targets if not r.get("retired")]
        if live:
            raise DeliveryError(
                f"'{live[0].get('title', pack_id)}' v{live[0]['version']} is not retired. "
                "Retire a pack before removing it.", 409, "CONTENT_PACK_NOT_RETIRED")
        target_ids = {r["release_id"] for r in targets}
        remaining = [r for r in state["releases"] if r["release_id"] not in target_ids]
        shared = {r["archive_sha256"] for r in remaining}
        root = trash_root()
        now = _now()
        moved: list[tuple[Path, Path]] = []
        entries: list[dict[str, Any]] = []
        try:
            for r in targets:
                digest = r["archive_sha256"]
                source = content_packs.LIBRARY / digest
                destination = None
                files: list[dict[str, Any]] = []
                if digest not in shared and source.is_dir() and not source.is_symlink():
                    root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    destination = root / digest
                    if destination.exists():
                        destination = root / f"{digest}.{uuid4().hex[:8]}"
                    files = _digest_files(source)
                    source.rename(destination)
                    moved.append((destination, source))
                entry = {
                    "what": f"{r['pack_id']}@{r['version']}", "when": now,
                    "from_where": str(source), "sha256": digest, "archive_sha256": digest,
                    "pack_id": r["pack_id"], "version": r["version"], "title": r.get("title", ""),
                    "release_id": r["release_id"], "retired": True,
                    "trash_path": str(destination / "archive.sspack") if destination else None,
                    "files": files,
                }
                entries.append(entry)
                if destination is not None:
                    (destination / "manifest.json").write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
            state["releases"] = remaining
            state.setdefault("removed", []).extend(entries)
            _atomic_state(state)
        except BaseException:
            for destination, source in reversed(moved):
                try:
                    (destination / "manifest.json").unlink(missing_ok=True)
                    destination.rename(source)
                except OSError:
                    pass
            raise
        return {"ok": True, "removed": True, "pack_id": pack_id, "version": version,
                "removed_releases": entries}


def record_removed(pack_id: str, version: str, archive_sha256: str, *, trash_path: str,
                   when: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Record an archive that was already moved out of the library. Touches no archive file:
    the release (if still listed) leaves the active records and a `removed` entry points at
    where the file now is. Used once, to migrate the Sep-21 Wars archive."""
    if not _DIGEST.fullmatch(archive_sha256):
        raise DeliveryError("archive_sha256 must be a lowercase SHA-256 digest.")
    with _lock():
        state = _read_state()
        removed = state.setdefault("removed", [])
        if any(e.get("archive_sha256") == archive_sha256 for e in removed):
            return {"ok": True, "recorded": False, "detail": "Already recorded."}
        listed = next((r for r in state["releases"] if r.get("archive_sha256") == archive_sha256), None)
        if listed and (listed.get("pack_id") != pack_id or listed.get("version") != version):
            raise DeliveryError("The listed release does not match that pack and version.", 409, "CONTENT_RELEASE_CONFLICT")
        entry = {
            "what": f"{pack_id}@{version}", "when": when or _now(),
            "from_where": str(content_packs.LIBRARY / archive_sha256),
            "sha256": archive_sha256, "archive_sha256": archive_sha256,
            "pack_id": pack_id, "version": version,
            "title": title or (listed or {}).get("title", ""),
            "release_id": (listed or {}).get("release_id"), "retired": True,
            "trash_path": trash_path, "files": [],
        }
        state["releases"] = [r for r in state["releases"] if r is not listed]
        removed.append(entry)
        _atomic_state(state)
        return {"ok": True, "recorded": True, "entry": entry}
