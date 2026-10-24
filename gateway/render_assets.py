"""Descriptor-relative durable inputs for private fleet rendering."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import queue
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .os_metadata import is_os_metadata

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_OFFSET_NAME = re.compile(r"[0-9a-f]{16}\.json\Z")
_BUFFER_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 8192
_MAX_I63 = (1 << 63) - 1
_RECORD_BUDGET = _MAX_RECORD_BYTES
_ROOT_NAMES = {"format.json", "store.lock", "index", "uploads", "assets", "published", "_trash"}
_UPLOAD_NAMES = {"head.json", "ranges", "chunks", "assembly", "protections", "changes"}
_RECOVERY_REASONS = {"unknown_write", "unsafe_entry", "missing_record", "integrity", "record_change"}
#: Same one-hour floor the artifact retention policy uses (retention.PROTECTED_AGE_S):
#: a just-published asset is still in a job's window and is never touched by the sweep.
_STAGING_GRACE_S = 3600.0
#: A complete upload that never published is recoverable by `finish_upload`; only
#: after this long is it treated as abandoned rather than in flight.
_ABANDONED_UPLOAD_GRACE_S = 3 * 24 * 3600.0
#: Published assets (durable input/output blobs) are reclaimed only after their render has
#: been terminal for this long. Three days matches retention.DEFAULT_MAX_AGE_DAYS, so a
#: re-used content digest is still served from the store well after the first render ends.
_PUBLISHED_GRACE_S = 3 * 24 * 3600.0


class RenderAssetError(Exception):
    pass


class AssetValidationError(RenderAssetError, ValueError):
    pass


class AssetNotFoundError(RenderAssetError, LookupError):
    pass


class AssetConflictError(RenderAssetError):
    pass


class AssetCapacityError(RenderAssetError):
    pass


class AssetRecoveryRequiredError(RenderAssetError):
    pass


class _AsyncQueueReader:
    """Blocking reader presented to the existing descriptor-safe chunk writer."""

    def __init__(self, pieces: "queue.Queue[bytes | BaseException | None]") -> None:
        self._pieces = pieces
        self._pending = b""
        self._done = False

    def read(self, size: int = -1) -> bytes:
        if self._done:
            return b""
        if size < 0 or size > _BUFFER_BYTES:
            size = _BUFFER_BYTES
        while not self._pending:
            item = self._pieces.get()
            if item is None:
                self._done = True
                return b""
            if isinstance(item, BaseException):
                raise item
            self._pending = item
        piece, self._pending = self._pending[:size], self._pending[size:]
        return piece


def _exact_int(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _MAX_I63:
        qualifier = "positive" if positive else "non-negative"
        raise AssetValidationError(f"{name} must be a {qualifier} signed-63-bit integer")
    return value


def _owner_token(owner_scope: Any) -> str:
    if not isinstance(owner_scope, str) or not owner_scope:
        raise AssetValidationError("owner_scope must be a non-empty string")
    try:
        encoded = owner_scope.encode("utf-8")
    except UnicodeError as exc:
        raise AssetValidationError("owner_scope must be valid Unicode") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validated_sha(value: Any, name: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AssetValidationError(f"{name} must be exactly 64 lower-case hexadecimal characters")
    return value


def _validated_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise AssetValidationError(f"{name} is invalid")
    return value


def _validated_reference(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise AssetValidationError("reference is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AssetValidationError("reference is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise AssetValidationError("reference is invalid") from exc
    if len(encoded) > 2048:
        raise AssetValidationError("reference is invalid")
    return value


def _byte_view(piece: Any) -> memoryview:
    if not isinstance(piece, (bytes, bytearray, memoryview)):
        raise AssetValidationError("stream must yield bytes")
    view = memoryview(piece)
    if not view.contiguous:
        raise AssetValidationError("stream must yield contiguous bytes")
    try:
        return view.cast("B")
    except (TypeError, ValueError) as exc:
        raise AssetValidationError("stream must yield contiguous bytes") from exc


def _write_all(descriptor: int, data: bytes | memoryview) -> None:
    view = _byte_view(data)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError("short render asset write")
        view = view[written:]


def _iter_stream(stream: Any, length: int) -> Iterator[memoryview]:
    remaining = length
    read = getattr(stream, "read", None)
    if callable(read):
        while remaining:
            view = _byte_view(read(min(_BUFFER_BYTES, remaining)))
            if not view:
                raise AssetValidationError("stream ended before declared length")
            if view.nbytes > remaining:
                raise AssetValidationError("stream exceeded declared length")
            remaining -= view.nbytes
            yield view
        if _byte_view(read(1)):
            raise AssetValidationError("stream exceeded declared length")
        return
    try:
        iterator = iter(stream)
    except TypeError as exc:
        raise AssetValidationError("stream must be file-like or iterable") from exc
    for piece in iterator:
        view = _byte_view(piece)
        if not view:
            continue
        if view.nbytes > remaining:
            raise AssetValidationError("stream exceeded declared length")
        remaining -= view.nbytes
        for start in range(0, view.nbytes, _BUFFER_BYTES):
            yield view[start : start + _BUFFER_BYTES]
        if remaining == 0:
            try:
                extra = next(iterator)
            except StopIteration:
                return
            if _byte_view(extra):
                raise AssetValidationError("stream exceeded declared length")
    if remaining:
        raise AssetValidationError("stream ended before declared length")


def _no_float(value: str):
    raise ValueError("floating point JSON is forbidden")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _valid_unicode(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
    if isinstance(value, list):
        return all(_valid_unicode(item) for item in value)
    if isinstance(value, dict):
        return all(_valid_unicode(key) and _valid_unicode(item) for key, item in value.items())
    return True


def _stored_int(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and (value >= (1 if positive else 0)) and value <= _MAX_I63


def _stored_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _stored_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _exact_keys(record: dict, keys: set[str]) -> None:
    if set(record) != keys:
        raise ValueError("record keys are not closed")


def _validate_pending(pending: Any, total: int) -> None:
    if not isinstance(pending, dict) or not isinstance(pending.get("kind"), str):
        raise ValueError("invalid pending operation")
    if pending["kind"] == "range":
        _exact_keys(pending, {"kind", "operation_id", "candidate_id", "offset", "length", "phase", "sha256"})
        if not _stored_id(pending["operation_id"]) or not _stored_id(pending["candidate_id"]):
            raise ValueError("invalid pending identity")
        if not _stored_int(pending["offset"]) or not _stored_int(pending["length"], positive=True):
            raise ValueError("invalid pending range")
        if pending["offset"] > total or pending["length"] > total - pending["offset"]:
            raise ValueError("pending range overflow")
        if pending["phase"] not in {"prepared", "verified", "rejected"}:
            raise ValueError("invalid pending phase")
        if pending["phase"] == "verified":
            if not _stored_sha(pending["sha256"]):
                raise ValueError("verified range lacks digest")
        elif pending["sha256"] is not None:
            raise ValueError("unverified range has digest")
        return
    if pending["kind"] == "publication":
        _exact_keys(pending, {"kind", "operation_id", "candidate_id", "phase"})
        if not _stored_id(pending["operation_id"]) or not _stored_id(pending["candidate_id"]):
            raise ValueError("invalid publication identity")
        if pending["phase"] not in {"prepared", "verified"}:
            raise ValueError("invalid publication phase")
        return
    raise ValueError("invalid pending kind")


def _validate_record(record: Any, expected_kind: str) -> None:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if type(record.get("schema")) is not int or record["schema"] != 1 or record.get("kind") != expected_kind:
        raise ValueError("record schema or kind is invalid")
    common_init = {"schema", "kind", "owner_token", "sha256", "size_bytes", "upload_id", "asset_handle"}
    if expected_kind == "format":
        _exact_keys(record, {"schema", "kind", "format", "store_id"})
        if record["format"] != "render-asset-fd-v1" or not _stored_id(record["store_id"]):
            raise ValueError("invalid format record")
    elif expected_kind == "init":
        _exact_keys(record, common_init)
        if not (_stored_sha(record["owner_token"]) and _stored_sha(record["sha256"]) and _stored_int(record["size_bytes"], positive=True) and _stored_id(record["upload_id"]) and _stored_id(record["asset_handle"])):
            raise ValueError("invalid init record")
    elif expected_kind == "upload_head":
        _exact_keys(record, common_init | {"generation", "committed_offset", "state", "pending", "recovery_reason"})
        init_copy = {key: record[key] for key in common_init}
        init_copy["kind"] = "init"
        _validate_record(init_copy, "init")
        if not _stored_int(record["generation"]) or not _stored_int(record["committed_offset"]):
            raise ValueError("invalid head counters")
        if record["committed_offset"] > record["size_bytes"] or record["state"] not in {"incomplete", "published", "recovery_required"}:
            raise ValueError("invalid head state")
        if record["pending"] is not None:
            _validate_pending(record["pending"], record["size_bytes"])
        if record["recovery_reason"] is not None and record["recovery_reason"] not in _RECOVERY_REASONS:
            raise ValueError("invalid recovery reason")
        if record["state"] == "published" and (record["committed_offset"] != record["size_bytes"] or record["pending"] is not None):
            raise ValueError("invalid published head")
    elif expected_kind == "range":
        _exact_keys(record, {"schema", "kind", "owner_token", "upload_id", "operation_id", "candidate_id", "offset", "length", "sha256"})
        if not (_stored_sha(record["owner_token"]) and _stored_id(record["upload_id"]) and _stored_id(record["operation_id"]) and _stored_id(record["candidate_id"]) and _stored_int(record["offset"]) and _stored_int(record["length"], positive=True) and _stored_sha(record["sha256"])):
            raise ValueError("invalid range record")
        if record["offset"] > _MAX_I63 - record["length"]:
            raise ValueError("range overflow")
    elif expected_kind == "asset":
        _exact_keys(record, {"schema", "kind", "owner_token", "upload_id", "asset_handle", "sha256", "size_bytes", "operation_id"})
        if not (_stored_sha(record["owner_token"]) and _stored_id(record["upload_id"]) and _stored_id(record["asset_handle"]) and _stored_sha(record["sha256"]) and _stored_int(record["size_bytes"], positive=True) and _stored_id(record["operation_id"])):
            raise ValueError("invalid asset record")
    elif expected_kind == "protection":
        _exact_keys(record, {"schema", "kind", "owner_token", "upload_id", "asset_handle", "protection_kind", "reference", "generation", "state"})
        if not (_stored_sha(record["owner_token"]) and _stored_id(record["upload_id"]) and _stored_id(record["asset_handle"]) and record["protection_kind"] in {"reference", "reader"} and _stored_int(record["generation"]) and record["state"] in {"active", "released", "unknown"}):
            raise ValueError("invalid protection record")
        _validated_reference(record["reference"])
    elif expected_kind == "record_change":
        keys = {"schema", "kind", "operation_id", "record_kind", "target_name", "parent_device", "parent_inode", "old_generation", "old_sha256", "old_device", "old_inode", "new_generation", "new_sha256", "candidate_id", "candidate_device", "candidate_inode", "archive_id", "archive_device", "archive_inode"}
        _exact_keys(record, keys)
        if not (_stored_id(record["operation_id"]) and record["record_kind"] in {"upload_head", "protection"} and _stored_int(record["parent_device"]) and _stored_int(record["parent_inode"]) and _stored_int(record["new_generation"]) and _stored_sha(record["new_sha256"]) and _stored_id(record["candidate_id"]) and _stored_int(record["candidate_device"]) and _stored_int(record["candidate_inode"]) and _stored_id(record["archive_id"]) and _stored_int(record["archive_device"]) and _stored_int(record["archive_inode"])):
            raise ValueError("invalid record change")
        if record["record_kind"] == "upload_head":
            if record["target_name"] != "head.json":
                raise ValueError("invalid head target")
        elif not isinstance(record["target_name"], str) or re.fullmatch(r"[0-9a-f]{64}\.json", record["target_name"]) is None:
            raise ValueError("invalid protection target")
        old = (record["old_generation"], record["old_sha256"], record["old_device"], record["old_inode"])
        if all(value is None for value in old):
            if record["new_generation"] != 0:
                raise ValueError("invalid creation generation")
        elif any(value is None for value in old) or not (_stored_int(old[0]) and _stored_sha(old[1]) and _stored_int(old[2]) and _stored_int(old[3]) and record["new_generation"] == old[0] + 1):
            raise ValueError("invalid replacement identity")
    else:
        raise ValueError("unknown record kind")


def _decode_record(data: bytes, expected_kind: str) -> dict[str, Any]:
    try:
        if len(data) > _MAX_RECORD_BYTES or not data.endswith(b"\n"):
            raise ValueError("record size or terminator is invalid")
        record = json.loads(data[:-1].decode("utf-8"), object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_float)
        if not _valid_unicode(record):
            raise ValueError("invalid Unicode")
        _validate_record(record, expected_kind)
        if canonical := (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"):
            if canonical != data:
                raise ValueError("record is not canonical")
        return record
    except (AssetValidationError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AssetRecoveryRequiredError("render asset metadata record is invalid") from exc


def _encode_record(record: dict[str, Any]) -> bytes:
    try:
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (UnicodeError, TypeError, ValueError) as exc:
        raise AssetValidationError("record cannot be encoded") from exc
    _decode_record(encoded, record.get("kind", ""))
    return encoded


def _protection_key(owner_token: str, asset_handle: str, protection_kind: str, reference: str) -> str:
    raw = (json.dumps([owner_token, asset_handle, protection_kind, reference], ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


@contextmanager
def _scan_entries(descriptor: int) -> Iterator[Iterator[os.DirEntry]]:
    # Filter before validation, emptiness checks, recovery and retirement accounting.
    with os.scandir(descriptor) as iterator:
        yield (entry for entry in iterator if not is_os_metadata(entry.name))


def _known_entries(parent_fd: int, allowed: set[str], *, initializing: bool = False) -> set[str]:
    entries = set()
    with _scan_entries(parent_fd) as iterator:
        for entry in iterator:
            if entry.name not in allowed:
                if initializing:
                    raise AssetValidationError("existing render asset root has another format")
                raise AssetRecoveryRequiredError("render asset directory has an unexpected entry")
            entries.add(entry.name)
    return entries


@dataclass
class _UploadDirs:
    upload_id: str
    path: Path
    upload: int
    ranges: int
    chunks: int
    assembly: int
    protections: int
    changes: int

    def close(self) -> None:
        for descriptor in (self.changes, self.protections, self.assembly, self.chunks, self.ranges, self.upload):
            os.close(descriptor)


class _ReaderLease:
    def __init__(self, store: "RenderAssetStore", owner_token: str, asset_handle: str) -> None:
        self._store = store
        self._owner_token = owner_token
        self._asset_handle = asset_handle
        self._lease_id: str | None = None
        self._file = None
        self.sha256 = ""
        self.size_bytes = 0

    def __enter__(self) -> "_ReaderLease":
        self._store._enter_reader(self)
        return self

    def read(self, size: int = -1) -> bytes:
        if self._file is None:
            raise ValueError("reader lease is not active")
        return self._file.read(size)

    def seek(self, offset: int) -> int:
        if self._file is None:
            raise ValueError("reader lease is not active")
        return self._file.seek(offset)

    def __exit__(self, exc_type, exc, traceback) -> None:
        close_error = None
        try:
            if self._file is not None:
                self._file.close()
        except BaseException as caught:
            close_error = caught
        finally:
            self._file = None
        try:
            if self._lease_id is not None and not self._store._closed:
                if close_error is None:
                    self._store._release_reader(self._owner_token, self._asset_handle, self._lease_id)
                else:
                    self._store._unknown_reader(self._owner_token, self._asset_handle, self._lease_id)
        finally:
            self._lease_id = None
            self._store._forget_reader(self)
        if close_error is not None:
            raise close_error

    def _close_for_store_shutdown(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        finally:
            self._file = None


class RenderAssetStore:
    _root_locks_guard = threading.Lock()
    _root_locks: dict[tuple[int, int], threading.RLock] = {}

    def __init__(self, data_root: str | Path, *, max_chunk_bytes: int, max_asset_bytes: int, reserve_bytes: int, measure_free_bytes: Callable[[], int]) -> None:
        self.max_chunk_bytes = _exact_int(max_chunk_bytes, "max_chunk_bytes", positive=True)
        self.max_asset_bytes = _exact_int(max_asset_bytes, "max_asset_bytes", positive=True)
        self.reserve_bytes = _exact_int(reserve_bytes, "reserve_bytes")
        if not callable(measure_free_bytes):
            raise AssetValidationError("measure_free_bytes must be callable")
        self._measure_free_bytes = measure_free_bytes
        self.root = Path(data_root).absolute()
        # macOS keeps ``/var`` and ``/tmp`` as root-owned symlinks into
        # ``/private``; an isolated boot under $TMPDIR would otherwise refuse
        # its own data path. Only those platform links are followed -- any
        # link this process's user could have planted still fails closed.
        self._parent_path = self._canonical_directory(self.root.parent)
        self.root = self._parent_path / self.root.name
        self._lock = threading.RLock()
        self._open_readers: set[_ReaderLease] = set()
        self._closed = False
        self._held: list[int] = []
        self._initializing = True
        try:
            self._parent_fd = self._hold(self._ensure_directory_chain(self._parent_path))
            self._reject_symlink_chain(self._parent_path)
            self._parent_identity = _identity(os.fstat(self._parent_fd))
            parent_visible = os.stat(self._parent_path, follow_symlinks=False)
            if not stat.S_ISDIR(parent_visible.st_mode) or _identity(parent_visible) != self._parent_identity:
                raise AssetRecoveryRequiredError("render asset parent identity changed")
            try:
                self._root_fd = self._hold(os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._parent_fd))
                fresh_root = False
            except FileNotFoundError:
                os.mkdir(self.root.name, 0o700, dir_fd=self._parent_fd)
                os.fsync(self._parent_fd)
                self._root_fd = self._hold(os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._parent_fd))
                fresh_root = True
            self._root_identity = _identity(os.fstat(self._root_fd))
            entries = _known_entries(self._root_fd, _ROOT_NAMES, initializing=True)
            if entries and "format.json" not in entries:
                raise AssetValidationError("existing render asset root has another format")
            if entries and entries != _ROOT_NAMES:
                raise AssetValidationError("render asset root contains unexpected entries")
            if not entries:
                fresh_root = True
                self._initialize_root()
            self._lock_fd = self._hold(os.open("store.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=self._root_fd))
            lock_info = os.fstat(self._lock_fd)
            if not stat.S_ISREG(lock_info.st_mode):
                raise AssetValidationError("render asset lock is unsafe")
            self._lock_identity = _identity(lock_info)
            with self._root_locks_guard:
                self._root_thread_lock = self._root_locks.setdefault(self._root_identity, threading.RLock())
            with self._root_guard():
                self._index_fd = self._open_held_dir(self._root_fd, "index")
                self._uploads_fd = self._open_held_dir(self._root_fd, "uploads")
                self._assets_fd = self._open_held_dir(self._root_fd, "assets")
                self._published_fd = self._open_held_dir(self._root_fd, "published")
                self._trash_fd = self._open_held_dir(self._root_fd, "_trash")
                self._history_fd = self._open_held_dir(self._trash_fd, "metadata-history")
                self._fixed_identities = {
                    self._index_fd: _identity(os.fstat(self._index_fd)),
                    self._uploads_fd: _identity(os.fstat(self._uploads_fd)),
                    self._assets_fd: _identity(os.fstat(self._assets_fd)),
                    self._published_fd: _identity(os.fstat(self._published_fd)),
                    self._trash_fd: _identity(os.fstat(self._trash_fd)),
                    self._history_fd: _identity(os.fstat(self._history_fd)),
                }
                format_record, _, _ = self._read_record(self._root_fd, "format.json", "format")
                self.store_id = format_record["store_id"]
                self._assert_layout()
            self._initializing = False
        except BaseException:
            self._close_held()
            self._closed = True
            raise

    def _hold(self, descriptor: int) -> int:
        self._held.append(descriptor)
        return descriptor

    def _close_held(self) -> list[BaseException]:
        errors = []
        while self._held:
            descriptor = self._held.pop()
            try:
                os.close(descriptor)
            except BaseException as exc:
                errors.append(exc)
        return errors

    @staticmethod
    def _canonical_directory(path: Path) -> Path:
        """Resolve platform symlinks only: links owned by root, in root-owned dirs.

        macOS ships ``/var -> private/var`` and ``/tmp -> private/tmp``. A data
        directory under ``$TMPDIR`` therefore has a symlink in its ancestry that
        no user created, and refusing it made an isolated Gateway unable to boot
        at all. Every other component must still be a real directory, so a link
        the local user could plant is refused exactly as before.
        """
        parts = list(path.parts)
        hops = 0
        index = 1
        if not path.is_absolute() or not parts or parts[0] != "/":
            raise AssetValidationError("render asset parent path is unsafe")
        while index < len(parts):
            part = parts[index]
            candidate = Path(parts[0], *parts[1:index], part)
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                return candidate.joinpath(*parts[index + 1:])
            if stat.S_ISLNK(info.st_mode):
                hops += 1
                if hops > 40:
                    raise AssetValidationError("render asset parent path is unsafe")
                parent_info = candidate.parent.lstat()
                if (info.st_uid != 0 or parent_info.st_uid != 0
                        or info.st_mode & 0o022 or parent_info.st_mode & 0o022):
                    raise AssetValidationError("render asset parent path is unsafe")
                target = Path(os.readlink(candidate))
                if not target.is_absolute():
                    target = candidate.parent / target
                if any(item in (".", "..") for item in target.parts):
                    raise AssetValidationError("render asset parent path is unsafe")
                parts = list(target.parts) + parts[index + 1:]
                index = 1
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise AssetValidationError("render asset parent path is unsafe")
            index += 1
        return Path(parts[0], *parts[1:])

    @staticmethod
    def _ensure_directory_chain(path: Path) -> int:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        child = None
        keep_descriptor = False
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                child_info = os.fstat(child)
                visible = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(child_info.st_mode) or _identity(child_info) != _identity(visible):
                    raise AssetValidationError("render asset path is unsafe")
                os.close(descriptor)
                descriptor = child
                child = None
            keep_descriptor = True
            return descriptor
        except OSError as exc:
            raise AssetValidationError("render asset path is unsafe") from exc
        except BaseException:
            raise
        finally:
            if child is not None:
                os.close(child)
            if not keep_descriptor:
                os.close(descriptor)

    @staticmethod
    def _reject_symlink_chain(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise AssetValidationError("render asset parent path is unsafe")

    def _initialize_root(self) -> None:
        lock_fd = os.open("store.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        try:
            os.fsync(lock_fd)
        finally:
            os.close(lock_fd)
        os.fsync(self._root_fd)
        for name in ("index", "uploads", "assets", "published", "_trash"):
            os.mkdir(name, 0o700, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        trash_fd = os.open("_trash", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._root_fd)
        try:
            os.mkdir("metadata-history", 0o700, dir_fd=trash_fd)
            os.fsync(trash_fd)
        finally:
            os.close(trash_fd)
        record = {"schema": 1, "kind": "format", "format": "render-asset-fd-v1", "store_id": uuid.uuid4().hex}
        self._create_file(self._root_fd, "format.json", _encode_record(record))
        os.fsync(self._root_fd)

    def _open_held_dir(self, parent_fd: int, name: str) -> int:
        return self._hold(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd))

    @contextmanager
    def _root_guard(self):
        lock = getattr(self, "_root_thread_lock", threading.RLock())
        with lock:
            if hasattr(self, "_lock_fd"):
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if hasattr(self, "_lock_fd"):
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    @contextmanager
    def _exclusive(self):
        with self._root_thread_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("render asset store is closed")
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                try:
                    self._assert_layout()
                    yield
                finally:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _assert_layout(self) -> None:
        if _identity(self._parent_path.lstat()) != self._parent_identity or _identity(self.root.lstat()) != self._root_identity:
            raise AssetRecoveryRequiredError("render asset root identity changed")
        lock_info = os.stat("store.lock", dir_fd=self._root_fd, follow_symlinks=False)
        if not stat.S_ISREG(lock_info.st_mode) or _identity(lock_info) != self._lock_identity:
            raise AssetRecoveryRequiredError("render asset lock identity changed")
        for descriptor, expected in getattr(self, "_fixed_identities", {}).items():
            if _identity(os.fstat(descriptor)) != expected:
                raise AssetRecoveryRequiredError("render asset directory identity changed")
        for name, descriptor in (("index", getattr(self, "_index_fd", -1)), ("uploads", getattr(self, "_uploads_fd", -1)), ("assets", getattr(self, "_assets_fd", -1)), ("published", getattr(self, "_published_fd", -1)), ("_trash", getattr(self, "_trash_fd", -1))):
            if descriptor >= 0 and _identity(os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)) != _identity(os.fstat(descriptor)):
                raise AssetRecoveryRequiredError("render asset directory entry changed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            errors = []
            for reader in tuple(self._open_readers):
                try:
                    reader._close_for_store_shutdown()
                except BaseException as exc:
                    errors.append(exc)
            self._open_readers.clear()
            errors.extend(self._close_held())
            if errors:
                raise errors[0]

    def _measure(self, needed: int) -> None:
        try:
            free = self._measure_free_bytes()
        except Exception as exc:
            raise AssetCapacityError("free disk space could not be measured") from exc
        if type(free) is not int or free < 0 or free > _MAX_I63:
            raise AssetCapacityError("free disk space measurement is invalid")
        if free < self.reserve_bytes + needed:
            raise AssetCapacityError("render asset storage reserve would be crossed")

    @staticmethod
    def _create_file(parent_fd: int, name: str, data: bytes) -> tuple[int, int]:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise AssetRecoveryRequiredError("metadata candidate is not regular")
            return _identity(info)
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_bytes(parent_fd: int, name: str) -> tuple[bytes, tuple[int, int]]:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise AssetRecoveryRequiredError("metadata leaf is not regular")
            pieces = []
            total = 0
            while total <= _MAX_RECORD_BYTES:
                piece = os.read(descriptor, min(4096, _MAX_RECORD_BYTES + 1 - total))
                if not piece:
                    break
                pieces.append(piece)
                total += len(piece)
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(visible) != _identity(info):
                raise AssetRecoveryRequiredError("opened metadata leaf changed")
            return b"".join(pieces), _identity(info)
        except BaseException:
            raise
        finally:
            os.close(descriptor)

    def _read_record(self, parent_fd: int, name: str, kind: str) -> tuple[dict[str, Any], bytes, tuple[int, int]]:
        try:
            raw, identity = self._read_bytes(parent_fd, name)
        except (OSError, AssetRecoveryRequiredError) as exc:
            raise AssetRecoveryRequiredError("render asset metadata is unavailable") from exc
        return _decode_record(raw, kind), raw, identity

    @staticmethod
    def _entry_exists(parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _publish_immutable(self, parent_fd: int, target: str, record: dict[str, Any], candidate_id: str | None = None) -> None:
        data = _encode_record(record)
        candidate_id = candidate_id or uuid.uuid4().hex
        candidate = f"candidate-{candidate_id}.json"
        candidate_missing = not self._entry_exists(parent_fd, candidate)
        target_missing = not self._entry_exists(parent_fd, target)
        if candidate_missing or target_missing:
            self._measure(2 * _RECORD_BUDGET)
        if candidate_missing:
            self._create_file(parent_fd, candidate, data)
        candidate_raw, candidate_identity = self._read_bytes(parent_fd, candidate)
        if candidate_raw != data:
            raise AssetRecoveryRequiredError("immutable candidate conflicts")
        try:
            os.link(candidate, target, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            pass
        target_raw, target_identity = self._read_bytes(parent_fd, target)
        if target_raw != data or target_identity != candidate_identity:
            raise AssetRecoveryRequiredError("immutable target has an unexpected winner")
        os.fsync(parent_fd)

    def _open_archive(self, archive_id: str, *, create: bool) -> int:
        if create:
            os.mkdir(archive_id, 0o700, dir_fd=self._history_fd)
            os.fsync(self._history_fd)
        return os.open(archive_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._history_fd)

    def _change_record(self, dirs: _UploadDirs, parent_fd: int, target: str, kind: str, new_record: dict[str, Any]) -> None:
        self._recover_changes(dirs)
        self._measure(4 * _RECORD_BUDGET)
        parent_info = os.fstat(parent_fd)
        old_record = old_raw = old_identity = None
        if self._entry_exists(parent_fd, target):
            old_record, old_raw, old_identity = self._read_record(parent_fd, target, kind)
            if new_record["generation"] != old_record["generation"] + 1:
                raise AssetRecoveryRequiredError("record generation is not contiguous")
        elif new_record["generation"] != 0:
            raise AssetRecoveryRequiredError("replacement record is missing")
        operation_id, candidate_id, archive_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
        archive_fd = self._open_archive(archive_id, create=True)
        try:
            archive_info = os.fstat(archive_fd)
            candidate_name = f"candidate-{candidate_id}.json"
            new_raw = _encode_record(new_record)
            candidate_identity = self._create_file(dirs.changes, candidate_name, new_raw)
            marker = {
                "schema": 1,
                "kind": "record_change",
                "operation_id": operation_id,
                "record_kind": kind,
                "target_name": target,
                "parent_device": parent_info.st_dev,
                "parent_inode": parent_info.st_ino,
                "old_generation": None if old_record is None else old_record["generation"],
                "old_sha256": None if old_raw is None else hashlib.sha256(old_raw).hexdigest(),
                "old_device": None if old_identity is None else old_identity[0],
                "old_inode": None if old_identity is None else old_identity[1],
                "new_generation": new_record["generation"],
                "new_sha256": hashlib.sha256(new_raw).hexdigest(),
                "candidate_id": candidate_id,
                "candidate_device": candidate_identity[0],
                "candidate_inode": candidate_identity[1],
                "archive_id": archive_id,
                "archive_device": archive_info.st_dev,
                "archive_inode": archive_info.st_ino,
            }
            marker_name = f"change-{operation_id}.json"
            self._create_file(dirs.changes, marker_name, _encode_record(marker))
            os.fsync(dirs.changes)
            if old_record is not None:
                os.rename(target, "prior.json", src_dir_fd=parent_fd, dst_dir_fd=archive_fd)
                prior, prior_raw, prior_identity = self._read_record(archive_fd, "prior.json", kind)
                if prior_identity != old_identity or prior_raw != old_raw or prior["generation"] != old_record["generation"]:
                    raise AssetRecoveryRequiredError("moved prior record changed")
                os.fsync(parent_fd)
                os.fsync(archive_fd)
            self._complete_change(dirs, marker_name, marker, archive_fd)
        finally:
            os.close(archive_fd)

    def _complete_change(self, dirs: _UploadDirs, marker_name: str, marker: dict[str, Any], archive_fd: int) -> None:
        parent_fd = dirs.upload if marker["record_kind"] == "upload_head" else dirs.protections
        if marker_name != f"change-{marker['operation_id']}.json":
            raise AssetRecoveryRequiredError("record marker name does not match its operation")
        parent_info = os.fstat(parent_fd)
        if _identity(parent_info) != (marker["parent_device"], marker["parent_inode"]):
            raise AssetRecoveryRequiredError("record destination identity changed")
        candidate_name = f"candidate-{marker['candidate_id']}.json"
        candidate_parent = dirs.changes
        if not self._entry_exists(candidate_parent, candidate_name):
            candidate_name = "candidate.json"
            candidate_parent = archive_fd
        candidate_raw, candidate_identity = self._read_bytes(candidate_parent, candidate_name)
        if hashlib.sha256(candidate_raw).hexdigest() != marker["new_sha256"] or candidate_identity != (marker["candidate_device"], marker["candidate_inode"]):
            raise AssetRecoveryRequiredError("record candidate identity changed")
        new_record = _decode_record(candidate_raw, marker["record_kind"])
        if new_record["generation"] != marker["new_generation"]:
            raise AssetRecoveryRequiredError("record candidate generation changed")
        if marker["record_kind"] == "upload_head":
            if new_record["upload_id"] != dirs.upload_id:
                raise AssetRecoveryRequiredError("head candidate upload identity changed")
            self._validate_head_init(new_record)
        else:
            expected_target = f"{_protection_key(new_record['owner_token'], new_record['asset_handle'], new_record['protection_kind'], new_record['reference'])}.json"
            head = self._validated_head(dirs)
            fields = ("owner_token", "upload_id", "asset_handle")
            if any(new_record[field] != head[field] for field in fields) or marker["target_name"] != expected_target:
                raise AssetRecoveryRequiredError("protection candidate identity changed")
        archive_info = os.fstat(archive_fd)
        if _identity(archive_info) != (marker["archive_device"], marker["archive_inode"]):
            raise AssetRecoveryRequiredError("record archive identity changed")
        if marker["old_generation"] is None:
            if self._entry_exists(archive_fd, "prior.json"):
                raise AssetRecoveryRequiredError("creation archive has unexpected prior")
        else:
            prior, prior_raw, prior_identity = self._read_record(archive_fd, "prior.json", marker["record_kind"])
            if prior["generation"] != marker["old_generation"] or hashlib.sha256(prior_raw).hexdigest() != marker["old_sha256"] or prior_identity != (marker["old_device"], marker["old_inode"]):
                raise AssetRecoveryRequiredError("prior record identity changed")
            os.fsync(parent_fd)
            os.fsync(archive_fd)
        if self._entry_exists(parent_fd, marker["target_name"]):
            target_raw, target_identity = self._read_bytes(parent_fd, marker["target_name"])
            if target_identity != candidate_identity or hashlib.sha256(target_raw).hexdigest() != marker["new_sha256"]:
                raise AssetRecoveryRequiredError("record target has an unexpected winner")
        else:
            self._measure(_RECORD_BUDGET)
            os.link(candidate_name, marker["target_name"], src_dir_fd=candidate_parent, dst_dir_fd=parent_fd, follow_symlinks=False)
        target_record, target_raw, target_identity = self._read_record(parent_fd, marker["target_name"], marker["record_kind"])
        if target_identity != candidate_identity or target_record["generation"] != marker["new_generation"] or hashlib.sha256(target_raw).hexdigest() != marker["new_sha256"]:
            raise AssetRecoveryRequiredError("published record identity changed")
        os.fsync(parent_fd)
        if candidate_parent == dirs.changes:
            os.rename(candidate_name, "candidate.json", src_dir_fd=dirs.changes, dst_dir_fd=archive_fd)
            os.fsync(dirs.changes)
            os.fsync(archive_fd)
        if self._entry_exists(dirs.changes, marker_name):
            os.rename(marker_name, "marker.json", src_dir_fd=dirs.changes, dst_dir_fd=archive_fd)
            os.fsync(dirs.changes)
            os.fsync(archive_fd)

    def _recover_changes(self, dirs: _UploadDirs) -> None:
        with _scan_entries(dirs.changes) as iterator:
            for entry in iterator:
                if re.fullmatch(r"candidate-[0-9a-f]{32}\.json", entry.name):
                    continue
                if re.fullmatch(r"change-[0-9a-f]{32}\.json", entry.name) is None:
                    raise AssetRecoveryRequiredError("upload has unknown change evidence")
                marker, _, _ = self._read_record(dirs.changes, entry.name, "record_change")
                archive_fd = self._open_archive(marker["archive_id"], create=False)
                try:
                    self._complete_change(dirs, entry.name, marker, archive_fd)
                finally:
                    os.close(archive_fd)
        with _scan_entries(dirs.changes) as iterator:
            if next(iterator, None) is not None:
                raise AssetRecoveryRequiredError("upload has unresolved change evidence")

    @staticmethod
    def _upload_is_pristine(dirs: _UploadDirs) -> bool:
        for descriptor in (dirs.ranges, dirs.chunks, dirs.assembly, dirs.protections, dirs.changes):
            with _scan_entries(descriptor) as iterator:
                if next(iterator, None) is not None:
                    return False
        return True

    def _ensure_upload(self, init: dict[str, Any]) -> _UploadDirs:
        upload_id = init["upload_id"]
        created = False
        try:
            upload_fd = os.open(upload_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._uploads_fd)
        except FileNotFoundError:
            os.mkdir(upload_id, 0o700, dir_fd=self._uploads_fd)
            os.fsync(self._uploads_fd)
            upload_fd = os.open(upload_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._uploads_fd)
            created = True
        descriptors = {}
        try:
            entries = _known_entries(upload_fd, _UPLOAD_NAMES)
            for name in ("ranges", "chunks", "assembly", "protections", "changes"):
                if name not in entries:
                    os.mkdir(name, 0o700, dir_fd=upload_fd)
                    os.fsync(upload_fd)
                descriptors[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=upload_fd)
            dirs = _UploadDirs(upload_id, self.root / "uploads" / upload_id, upload_fd, descriptors["ranges"], descriptors["chunks"], descriptors["assembly"], descriptors["protections"], descriptors["changes"])
            self._recover_changes(dirs)
            if not self._entry_exists(upload_fd, "head.json"):
                if not created and not self._upload_is_pristine(dirs):
                    raise AssetRecoveryRequiredError("authoritative upload head is missing")
                head = {**init, "kind": "upload_head", "generation": 0, "committed_offset": 0, "state": "incomplete", "pending": None, "recovery_reason": None}
                self._change_record(dirs, upload_fd, "head.json", "upload_head", head)
            return dirs
        except BaseException:
            for descriptor in descriptors.values():
                os.close(descriptor)
            os.close(upload_fd)
            raise

    def _find_init_candidate(self, owner: str, sha256: str, size: int) -> dict[str, Any] | None:
        found = None
        with _scan_entries(self._index_fd) as iterator:
            for entry in iterator:
                if not entry.name.startswith("candidate-") or not entry.name.endswith(".json"):
                    continue
                try:
                    record, _, _ = self._read_record(self._index_fd, entry.name, "init")
                except AssetRecoveryRequiredError:
                    continue
                if (record["owner_token"], record["sha256"], record["size_bytes"]) == (owner, sha256, size):
                    if found is not None and (record["upload_id"], record["asset_handle"]) != (found["upload_id"], found["asset_handle"]):
                        raise AssetRecoveryRequiredError("multiple init candidates conflict")
                    found = record
        return found

    @staticmethod
    def _upload_descriptor(head: dict[str, Any]) -> dict[str, Any]:
        state = head["state"]
        result = {"upload_id": head["upload_id"], "sha256": head["sha256"], "size_bytes": head["size_bytes"], "committed_offset": head["committed_offset"], "state": state, "incomplete": state == "incomplete", "published": state == "published", "recovery_required": state == "recovery_required"}
        if state == "published":
            result["asset_handle"] = head["asset_handle"]
        return result

    @staticmethod
    def _asset_descriptor(asset: dict[str, Any]) -> dict[str, Any]:
        return {"asset_handle": asset["asset_handle"], "sha256": asset["sha256"], "size_bytes": asset["size_bytes"]}

    def _validate_head_init(self, head: dict[str, Any]) -> None:
        target = f"{head['owner_token']}-{head['sha256']}.json"
        init, _, _ = self._read_record(self._index_fd, target, "init")
        fields = ("owner_token", "sha256", "size_bytes", "upload_id", "asset_handle")
        if any(head[field] != init[field] for field in fields):
            raise AssetRecoveryRequiredError("upload head does not match its init reservation")

    def _validated_head(self, dirs: _UploadDirs) -> dict[str, Any]:
        head, _, _ = self._read_record(dirs.upload, "head.json", "upload_head")
        if head["upload_id"] != dirs.upload_id:
            raise AssetRecoveryRequiredError("upload head identity changed")
        self._validate_head_init(head)
        return head

    def _head(self, dirs: _UploadDirs) -> dict[str, Any]:
        self._recover_changes(dirs)
        return self._validated_head(dirs)

    def _set_head(self, dirs: _UploadDirs, old: dict[str, Any], **changes) -> dict[str, Any]:
        new = {**old, **changes, "generation": old["generation"] + 1}
        self._change_record(dirs, dirs.upload, "head.json", "upload_head", new)
        return new

    def _mark_recovery(self, dirs: _UploadDirs, head: dict[str, Any], reason: str) -> dict[str, Any]:
        return self._set_head(dirs, head, state="recovery_required", pending=None, recovery_reason=reason)

    def begin_upload(self, owner_scope: str, sha256: str, size_bytes: int) -> dict[str, Any]:
        owner = _owner_token(owner_scope)
        sha256 = _validated_sha(sha256)
        size_bytes = _exact_int(size_bytes, "size_bytes", positive=True)
        if size_bytes > self.max_asset_bytes:
            raise AssetValidationError("asset exceeds max_asset_bytes")
        with self._exclusive():
            self._measure(4 * _RECORD_BUDGET)
            target = f"{owner}-{sha256}.json"
            if self._entry_exists(self._index_fd, target):
                init, _, _ = self._read_record(self._index_fd, target, "init")
                if init["owner_token"] != owner or init["sha256"] != sha256:
                    raise AssetRecoveryRequiredError("init lookup identity changed")
                if init["size_bytes"] != size_bytes:
                    raise AssetConflictError("content digest was declared with a different total")
            else:
                init = self._find_init_candidate(owner, sha256, size_bytes)
                if init is None:
                    init = {"schema": 1, "kind": "init", "owner_token": owner, "sha256": sha256, "size_bytes": size_bytes, "upload_id": uuid.uuid4().hex, "asset_handle": uuid.uuid4().hex}
                self._publish_immutable(self._index_fd, target, init)
            dirs = self._ensure_upload(init)
            try:
                head = self._resume_pending(dirs, self._head(dirs))
                return self._upload_descriptor(head)
            finally:
                dirs.close()

    def blob_status(self, owner_scope: str, sha256: str, size_bytes: int) -> dict[str, Any]:
        """Read digest/size upload state without creating an upload or revealing another scope."""
        owner = _owner_token(owner_scope)
        sha256 = _validated_sha(sha256)
        size_bytes = _exact_int(size_bytes, "size_bytes", positive=True)
        target = f"{owner}-{sha256}.json"
        with self._exclusive():
            if not self._entry_exists(self._index_fd, target):
                return {"state": "missing", "offset": 0}
            init, _, _ = self._read_record(self._index_fd, target, "init")
            if init["owner_token"] != owner or init["sha256"] != sha256:
                raise AssetRecoveryRequiredError("render blob identity changed")
            if init["size_bytes"] != size_bytes:
                raise AssetConflictError("content digest was declared with a different total")
            dirs = self._open_owned_upload(owner, init["upload_id"])
            try:
                with _scan_entries(dirs.changes) as changes:
                    if next(changes, None) is not None:
                        raise AssetRecoveryRequiredError(
                            "render blob has unresolved durable evidence"
                        )
                head = self._validated_head(dirs)
                if head["pending"] is not None or head["state"] == "recovery_required":
                    raise AssetRecoveryRequiredError("render blob has unresolved durable evidence")
                state = "complete" if head["state"] == "published" else head["state"]
                if state == "complete":
                    asset, _, _ = self._read_record(
                        self._assets_fd, f"{head['asset_handle']}.json", "asset"
                    )
                    fields = ("owner_token", "upload_id", "asset_handle", "sha256", "size_bytes")
                    if any(asset[field] != head[field] for field in fields):
                        raise AssetRecoveryRequiredError("render blob publication identity changed")
                    self._verify_blob(
                        self._published_fd, f"{head['asset_handle']}.blob",
                        head["sha256"], head["size_bytes"],
                    ).close()
                return {
                    "state": state,
                    "offset": head["committed_offset"],
                    "upload_id": head["upload_id"],
                    "asset_handle": head["asset_handle"] if state == "complete" else None,
                }
            finally:
                dirs.close()

    async def append_async_chunk(
        self,
        owner_scope: str,
        upload_id: str,
        offset: int,
        body: Any,
        length: int,
        *,
        piece_bytes: int = _BUFFER_BYTES,
        queued_pieces: int = 4,
    ) -> int:
        """Bridge an async request body into ``append_chunk`` with constant memory."""
        length = _exact_int(length, "length", positive=True)
        if piece_bytes != _BUFFER_BYTES or queued_pieces != 4:
            raise AssetValidationError("private render stream bridge has fixed bounds")
        pieces: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=queued_pieces)
        reader = _AsyncQueueReader(pieces)
        result: list[int] = []
        failure: list[BaseException] = []

        def consume() -> None:
            try:
                result.append(self.append_chunk(owner_scope, upload_id, offset, reader, length))
            except BaseException as exc:
                failure.append(exc)

        thread = threading.Thread(target=consume, name="render-asset-stream", daemon=True)
        thread.start()
        sent = 0

        async def put(item: bytes | BaseException | None) -> None:
            while True:
                if failure:
                    raise failure[0]
                try:
                    pieces.put_nowait(item)
                    return
                except queue.Full:
                    await asyncio.sleep(0)

        try:
            async for incoming in body:
                if not isinstance(incoming, bytes):
                    raise AssetValidationError("request body chunks must be bytes")
                for start in range(0, len(incoming), piece_bytes):
                    piece = incoming[start:start + piece_bytes]
                    if sent + len(piece) > length:
                        raise AssetValidationError("request body exceeds declared length")
                    sent += len(piece)
                    await put(piece)
            if sent != length:
                raise AssetValidationError("request body ended before declared length")
            await put(None)
        except BaseException as exc:
            try:
                await put(exc)
            except BaseException:
                pass
            raise
        finally:
            await asyncio.to_thread(thread.join)
        if failure:
            raise failure[0]
        return result[0]

    def _open_owned_upload(self, owner: str, upload_id: str) -> _UploadDirs:
        upload_id = _validated_id(upload_id, "upload_id")
        try:
            upload_fd = os.open(upload_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._uploads_fd)
        except OSError as exc:
            raise AssetNotFoundError("render upload was not found") from exc
        descriptors = {}
        try:
            for name in ("ranges", "chunks", "assembly", "protections", "changes"):
                descriptors[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=upload_fd)
            dirs = _UploadDirs(upload_id, self.root / "uploads" / upload_id, upload_fd, descriptors["ranges"], descriptors["chunks"], descriptors["assembly"], descriptors["protections"], descriptors["changes"])
            head = self._head(dirs)
            if head["owner_token"] != owner:
                raise AssetNotFoundError("render upload was not found")
            return dirs
        except BaseException:
            for descriptor in descriptors.values():
                os.close(descriptor)
            os.close(upload_fd)
            raise

    def upload_status(self, owner_scope: str, upload_id: str) -> dict[str, Any]:
        owner = _owner_token(owner_scope)
        upload_id = _validated_id(upload_id, "upload_id")
        with self._exclusive():
            dirs = self._open_owned_upload(owner, upload_id)
            try:
                head = self._resume_pending(dirs, self._head(dirs))
                self._validate_range_records(dirs, head)
                return self._upload_descriptor(head)
            finally:
                dirs.close()

    def _range_target(self, offset: int) -> str:
        return f"{offset:016x}.json"

    def _range_record(self, dirs: _UploadDirs, offset: int) -> dict[str, Any]:
        record, _, _ = self._read_record(dirs.ranges, self._range_target(offset), "range")
        if record["upload_id"] != dirs.upload_id or record["offset"] != offset:
            raise AssetRecoveryRequiredError("range record identity changed")
        return record

    def _validate_range_records(self, dirs: _UploadDirs, head: dict[str, Any]) -> None:
        offset = 0
        while offset < head["committed_offset"]:
            record = self._range_record(dirs, offset)
            if record["owner_token"] != head["owner_token"] or record["length"] > head["committed_offset"] - offset:
                raise AssetRecoveryRequiredError("range record is inconsistent")
            offset += record["length"]
        if offset != head["committed_offset"]:
            raise AssetRecoveryRequiredError("range records are not contiguous")

    def _create_payload(self, parent_fd: int, name: str, stream: Any, length: int) -> str:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        hasher = hashlib.sha256()
        try:
            for piece in _iter_stream(stream, length):
                hasher.update(piece)
                _write_all(descriptor, piece)
            os.fsync(descriptor)
        except BaseException:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raise
        os.close(descriptor)
        os.fsync(parent_fd)
        return hasher.hexdigest()

    def _verify_blob(self, parent_fd: int, name: str, sha256: str, size: int):
        descriptor = None
        handle = None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise AssetRecoveryRequiredError("payload is not regular")
            visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(info) != _identity(visible):
                raise AssetRecoveryRequiredError("opened payload changed")
            handle = os.fdopen(descriptor, "rb")
            descriptor = None
            actual = hashlib.sha256()
            total = 0
            while piece := handle.read(_BUFFER_BYTES):
                actual.update(piece)
                total += len(piece)
            if total != size or actual.hexdigest() != sha256:
                raise AssetRecoveryRequiredError("payload failed verification")
            handle.seek(0)
            return handle
        except (OSError, AssetRecoveryRequiredError) as exc:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)
            raise AssetRecoveryRequiredError("payload is unavailable") from exc
        except BaseException:
            if handle is not None:
                handle.close()
            if descriptor is not None:
                os.close(descriptor)
            raise

    def append_chunk(self, owner_scope: str, upload_id: str, offset: int, stream: Any, length: int) -> int:
        owner = _owner_token(owner_scope)
        upload_id = _validated_id(upload_id, "upload_id")
        offset = _exact_int(offset, "offset")
        length = _exact_int(length, "length", positive=True)
        if length > self.max_chunk_bytes:
            raise AssetValidationError("chunk exceeds max_chunk_bytes")
        with self._exclusive():
            dirs = self._open_owned_upload(owner, upload_id)
            try:
                head = self._resume_pending(dirs, self._head(dirs))
                if head["state"] == "recovery_required":
                    raise AssetRecoveryRequiredError("render upload requires recovery")
                if head["state"] != "incomplete":
                    raise AssetConflictError("published upload cannot accept chunks")
                if offset < head["committed_offset"]:
                    record = self._range_record(dirs, offset)
                    if record["length"] != length:
                        raise AssetConflictError("chunk overlaps committed bytes")
                    replay = hashlib.sha256()
                    for piece in _iter_stream(stream, length):
                        replay.update(piece)
                    if replay.hexdigest() != record["sha256"]:
                        raise AssetConflictError("replayed chunk bytes differ")
                    return offset + length
                if offset != head["committed_offset"]:
                    raise AssetConflictError("chunk is not contiguous")
                if offset > head["size_bytes"] or length > head["size_bytes"] - offset:
                    raise AssetConflictError("chunk exceeds declared asset size")
                target = self._range_target(offset)
                if self._entry_exists(dirs.ranges, target):
                    self._mark_recovery(dirs, head, "unsafe_entry")
                    raise AssetRecoveryRequiredError("unexpected range winner")
                self._measure((head["size_bytes"] - offset) + head["size_bytes"] + 10 * _RECORD_BUDGET)
                operation_id, candidate_id = uuid.uuid4().hex, uuid.uuid4().hex
                pending = {"kind": "range", "operation_id": operation_id, "candidate_id": candidate_id, "offset": offset, "length": length, "phase": "prepared", "sha256": None}
                head = self._set_head(dirs, head, pending=pending)
                payload_name = f"{candidate_id}.bin"
                try:
                    chunk_sha = self._create_payload(dirs.chunks, payload_name, stream, length)
                except AssetValidationError:
                    rejected = {**pending, "phase": "rejected"}
                    head = self._set_head(dirs, head, pending=rejected)
                    self._set_head(dirs, head, pending=None)
                    raise
                except FileExistsError as exc:
                    self._mark_recovery(dirs, head, "unsafe_entry")
                    raise AssetRecoveryRequiredError("chunk candidate has an unexpected winner") from exc
                verified = {**pending, "phase": "verified", "sha256": chunk_sha}
                head = self._set_head(dirs, head, pending=verified)
                record = {"schema": 1, "kind": "range", "owner_token": owner, "upload_id": upload_id, "operation_id": operation_id, "candidate_id": candidate_id, "offset": offset, "length": length, "sha256": chunk_sha}
                self._publish_immutable(dirs.ranges, target, record, candidate_id=operation_id)
                self._set_head(dirs, head, committed_offset=offset + length, pending=None)
                return offset + length
            finally:
                dirs.close()

    def _resume_pending(self, dirs: _UploadDirs, head: dict[str, Any]) -> dict[str, Any]:
        pending = head["pending"]
        if pending is None:
            return head
        if pending["kind"] == "range":
            if pending["offset"] != head["committed_offset"]:
                raise AssetRecoveryRequiredError("pending range does not match the committed frontier")
            if pending["phase"] == "rejected":
                return self._set_head(dirs, head, pending=None)
            if pending["phase"] == "prepared":
                raise AssetRecoveryRequiredError("range write outcome is unknown")
            payload = f"{pending['candidate_id']}.bin"
            self._verify_blob(dirs.chunks, payload, pending["sha256"], pending["length"]).close()
            target = self._range_target(pending["offset"])
            record = {"schema": 1, "kind": "range", "owner_token": head["owner_token"], "upload_id": head["upload_id"], "operation_id": pending["operation_id"], "candidate_id": pending["candidate_id"], "offset": pending["offset"], "length": pending["length"], "sha256": pending["sha256"]}
            self._publish_immutable(dirs.ranges, target, record, candidate_id=pending["operation_id"])
            return self._set_head(dirs, head, committed_offset=pending["offset"] + pending["length"], pending=None)
        if pending["phase"] == "prepared":
            raise AssetRecoveryRequiredError("publication outcome is unknown")
        return self._complete_publication(dirs, head)

    def _publish_blob(self, source_fd: int, source: str, target_fd: int, target: str, sha256: str, size: int) -> None:
        source_info = os.stat(source, dir_fd=source_fd, follow_symlinks=False)
        if self._entry_exists(target_fd, target):
            target_info = os.stat(target, dir_fd=target_fd, follow_symlinks=False)
            if _identity(target_info) != _identity(source_info):
                raise AssetRecoveryRequiredError("published payload has an unexpected winner")
            self._verify_blob(target_fd, target, sha256, size).close()
            return
        self._measure(_RECORD_BUDGET)
        os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
        self._verify_blob(target_fd, target, sha256, size).close()
        os.fsync(target_fd)

    def _complete_publication(self, dirs: _UploadDirs, head: dict[str, Any]) -> dict[str, Any]:
        pending = head["pending"]
        assembly_name = f"{pending['candidate_id']}.bin"
        output_name = f"{head['asset_handle']}.blob"
        try:
            self._verify_blob(dirs.assembly, assembly_name, head["sha256"], head["size_bytes"]).close()
            self._publish_blob(dirs.assembly, assembly_name, self._published_fd, output_name, head["sha256"], head["size_bytes"])
            asset_record = {"schema": 1, "kind": "asset", "owner_token": head["owner_token"], "upload_id": head["upload_id"], "asset_handle": head["asset_handle"], "sha256": head["sha256"], "size_bytes": head["size_bytes"], "operation_id": pending["operation_id"]}
            self._publish_immutable(self._assets_fd, f"{head['asset_handle']}.json", asset_record, candidate_id=pending["operation_id"])
        except (OSError, AssetRecoveryRequiredError) as exc:
            self._mark_recovery(dirs, head, "unsafe_entry")
            raise AssetRecoveryRequiredError("publication evidence conflicts") from exc
        return self._set_head(dirs, head, state="published", pending=None, recovery_reason=None)

    def finish_upload(self, owner_scope: str, upload_id: str) -> dict[str, Any]:
        owner = _owner_token(owner_scope)
        upload_id = _validated_id(upload_id, "upload_id")
        with self._exclusive():
            dirs = self._open_owned_upload(owner, upload_id)
            try:
                head = self._resume_pending(dirs, self._head(dirs))
                if head["state"] == "recovery_required":
                    raise AssetRecoveryRequiredError("render upload requires recovery")
                if head["state"] == "published":
                    try:
                        asset = self._owned_asset(owner, head["asset_handle"])
                        self._verify_blob(self._published_fd, f"{head['asset_handle']}.blob", head["sha256"], head["size_bytes"]).close()
                    except (AssetNotFoundError, AssetRecoveryRequiredError) as exc:
                        self._mark_recovery(dirs, head, "integrity")
                        raise AssetRecoveryRequiredError("published asset evidence is inconsistent") from exc
                    return self._asset_descriptor(asset)
                if head["committed_offset"] != head["size_bytes"]:
                    raise AssetConflictError("upload is incomplete")
                self._validate_range_records(dirs, head)
                self._measure(head["size_bytes"] + 10 * _RECORD_BUDGET)
                operation_id, candidate_id = uuid.uuid4().hex, uuid.uuid4().hex
                pending = {"kind": "publication", "operation_id": operation_id, "candidate_id": candidate_id, "phase": "prepared"}
                head = self._set_head(dirs, head, pending=pending)
                assembly_name = f"{candidate_id}.bin"
                descriptor = os.open(assembly_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirs.assembly)
                total_hash = hashlib.sha256()
                total_size = 0
                try:
                    offset = 0
                    while offset < head["size_bytes"]:
                        record = self._range_record(dirs, offset)
                        with self._verify_blob(dirs.chunks, f"{record['candidate_id']}.bin", record["sha256"], record["length"]) as source:
                            while piece := source.read(_BUFFER_BYTES):
                                total_hash.update(piece)
                                total_size += len(piece)
                                _write_all(descriptor, piece)
                        offset += record["length"]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(dirs.assembly)
                if total_size != head["size_bytes"] or total_hash.hexdigest() != head["sha256"]:
                    self._mark_recovery(dirs, head, "integrity")
                    raise AssetRecoveryRequiredError("assembled asset does not match declared identity")
                head = self._set_head(dirs, head, pending={**pending, "phase": "verified"})
                head = self._complete_publication(dirs, head)
                asset = self._owned_asset(owner, head["asset_handle"])
                return self._asset_descriptor(asset)
            finally:
                dirs.close()

    def _owned_asset(self, owner: str, handle: str) -> dict[str, Any]:
        try:
            asset, _, _ = self._read_record(self._assets_fd, f"{handle}.json", "asset")
        except AssetRecoveryRequiredError as exc:
            raise AssetNotFoundError("render asset was not found") from exc
        if asset["owner_token"] != owner:
            raise AssetNotFoundError("render asset was not found")
        dirs = self._open_owned_upload(owner, asset["upload_id"])
        try:
            head = self._resume_pending(dirs, self._head(dirs))
            if head["state"] == "recovery_required":
                raise AssetRecoveryRequiredError("render asset requires recovery")
            if head["state"] != "published" or head["asset_handle"] != handle:
                raise AssetNotFoundError("render asset was not found")
            fields = ("owner_token", "upload_id", "asset_handle", "sha256", "size_bytes")
            if any(asset[field] != head[field] for field in fields):
                raise AssetRecoveryRequiredError("asset lookup does not match its published head")
        finally:
            dirs.close()
        return asset

    def describe(self, owner_scope: str, asset_handle: str) -> dict[str, Any]:
        owner = _owner_token(owner_scope)
        asset_handle = _validated_id(asset_handle, "asset_handle")
        with self._exclusive():
            asset = self._owned_asset(owner, asset_handle)
            try:
                self._verify_blob(self._published_fd, f"{asset_handle}.blob", asset["sha256"], asset["size_bytes"]).close()
            except AssetRecoveryRequiredError:
                dirs = self._open_owned_upload(owner, asset["upload_id"])
                try:
                    self._mark_recovery(dirs, self._head(dirs), "integrity")
                finally:
                    dirs.close()
                raise
            return self._asset_descriptor(asset)

    def open_reader(self, owner_scope: str, asset_handle: str) -> _ReaderLease:
        return _ReaderLease(self, _owner_token(owner_scope), _validated_id(asset_handle, "asset_handle"))

    def _protection(self, dirs: _UploadDirs, owner: str, handle: str, kind: str, reference: str, state: str) -> dict[str, Any]:
        key = _protection_key(owner, handle, kind, reference)
        target = f"{key}.json"
        if self._entry_exists(dirs.protections, target):
            old, _, _ = self._read_record(dirs.protections, target, "protection")
            if (old["owner_token"], old["asset_handle"], old["protection_kind"], old["reference"]) != (owner, handle, kind, reference):
                raise AssetRecoveryRequiredError("protection key identity changed")
            if old["state"] == state:
                return old
            generation = old["generation"] + 1
        else:
            generation = 0
        record = {"schema": 1, "kind": "protection", "owner_token": owner, "upload_id": dirs.upload_id, "asset_handle": handle, "protection_kind": kind, "reference": reference, "generation": generation, "state": state}
        self._measure(4 * _RECORD_BUDGET)
        self._change_record(dirs, dirs.protections, target, "protection", record)
        return record

    def _enter_reader(self, reader: _ReaderLease) -> None:
        with self._exclusive():
            asset = self._owned_asset(reader._owner_token, reader._asset_handle)
            try:
                handle = self._verify_blob(self._published_fd, f"{reader._asset_handle}.blob", asset["sha256"], asset["size_bytes"])
            except AssetRecoveryRequiredError:
                dirs = self._open_owned_upload(reader._owner_token, asset["upload_id"])
                try:
                    self._mark_recovery(dirs, self._head(dirs), "integrity")
                finally:
                    dirs.close()
                raise
            lease_id = uuid.uuid4().hex
            dirs = None
            try:
                dirs = self._open_owned_upload(reader._owner_token, asset["upload_id"])
                self._protection(dirs, reader._owner_token, reader._asset_handle, "reader", lease_id, "active")
            except BaseException:
                handle.close()
                raise
            finally:
                if dirs is not None:
                    dirs.close()
            reader._file, reader.sha256, reader.size_bytes, reader._lease_id = handle, asset["sha256"], asset["size_bytes"], lease_id
            self._remember_reader(reader)

    def _remember_reader(self, reader: _ReaderLease) -> None:
        with self._lock:
            self._open_readers.add(reader)

    def _forget_reader(self, reader: _ReaderLease) -> None:
        with self._lock:
            self._open_readers.discard(reader)

    def _set_reader_state(self, owner: str, handle: str, lease_id: str, state: str) -> None:
        asset = self._owned_asset(owner, handle)
        dirs = self._open_owned_upload(owner, asset["upload_id"])
        try:
            self._protection(dirs, owner, handle, "reader", lease_id, state)
        finally:
            dirs.close()

    def _release_reader(self, owner: str, handle: str, lease_id: str) -> None:
        with self._exclusive():
            self._set_reader_state(owner, handle, lease_id, "released")

    def _unknown_reader(self, owner: str, handle: str, lease_id: str) -> None:
        with self._exclusive():
            self._set_reader_state(owner, handle, lease_id, "unknown")

    def acquire_reference(self, owner_scope: str, asset_handle: str, reference: str) -> None:
        owner = _owner_token(owner_scope)
        asset_handle = _validated_id(asset_handle, "asset_handle")
        reference = _validated_reference(reference)
        with self._exclusive():
            asset = self._owned_asset(owner, asset_handle)
            dirs = self._open_owned_upload(owner, asset["upload_id"])
            try:
                self._protection(dirs, owner, asset_handle, "reference", reference, "active")
            finally:
                dirs.close()

    def release_reference(self, owner_scope: str, asset_handle: str, reference: str) -> None:
        owner = _owner_token(owner_scope)
        asset_handle = _validated_id(asset_handle, "asset_handle")
        reference = _validated_reference(reference)
        with self._exclusive():
            asset = self._owned_asset(owner, asset_handle)
            dirs = self._open_owned_upload(owner, asset["upload_id"])
            try:
                self._protection(dirs, owner, asset_handle, "reference", reference, "released")
            finally:
                dirs.close()

    def active_protection(self, owner_scope: str) -> list[dict[str, str]]:
        owner = _owner_token(owner_scope)
        result = []
        with self._exclusive():
            with _scan_entries(self._uploads_fd) as uploads:
                for entry in uploads:
                    if not _ID.fullmatch(entry.name):
                        raise AssetRecoveryRequiredError("unexpected upload directory")
                    try:
                        dirs = self._open_owned_upload(owner, entry.name)
                    except AssetNotFoundError:
                        continue
                    try:
                        self._recover_changes(dirs)
                        head = self._validated_head(dirs)
                        with _scan_entries(dirs.protections) as protections:
                            for leaf in protections:
                                if re.fullmatch(r"[0-9a-f]{64}\.json", leaf.name) is None:
                                    raise AssetRecoveryRequiredError("upload has unknown protection evidence")
                                record, _, _ = self._read_record(dirs.protections, leaf.name, "protection")
                                expected = _protection_key(record["owner_token"], record["asset_handle"], record["protection_kind"], record["reference"])
                                if leaf.name != f"{expected}.json":
                                    raise AssetRecoveryRequiredError("protection lookup identity changed")
                                fields = ("owner_token", "upload_id", "asset_handle")
                                if any(record[field] != head[field] for field in fields):
                                    raise AssetRecoveryRequiredError("protection record does not match its upload")
                                if record["owner_token"] == owner and record["state"] in {"active", "unknown"}:
                                    result.append({"kind": record["protection_kind"], "asset_handle": record["asset_handle"], "reference": record["reference"]})
                    finally:
                        dirs.close()
        return result

    def sweep_staging(self, *, grace_seconds: float = _STAGING_GRACE_S,
                      abandoned_grace_seconds: float = _ABANDONED_UPLOAD_GRACE_S,
                      ) -> dict[str, int]:
        """Move dead chunk/assembly copies of published uploads into `_trash`.

        Once an upload is published the content-addressed `.blob` is the durable copy;
        `chunks/` and `assembly/` hold pure duplication — nothing reads them again
        (append refuses a published upload, and every reader verifies the published
        blob). Moving them keeps the store bounded without touching any upload
        identity, index record, range record, protection or published blob.

        A second class is swept whole: an upload whose bytes are complete and verified
        but which never published and has no asset record (a crash between the last
        chunk and `finish_upload`, or a rejected accept). Only a sealed head counts —
        `state='incomplete'`, no pending publication, `committed_offset == size_bytes`,
        every range record verified — and only after `abandoned_grace_seconds`; an
        in-progress upload is never guessed dead from age.

        Failures fail closed per upload: an unresolved `changes/` journal, a live
        reader/reference, a non-published head (for the copies pass), an unreadable
        record, or anything newer than the grace period leaves that upload untouched.
        This only bounds redundant copies; freeing the volume bytes is the owner's
        `_trash` step. Durable published assets (the `.blob` itself) are reclaimed
        separately by `sweep_published` once their render is terminal.
        """
        if (type(grace_seconds) not in (int, float) or isinstance(grace_seconds, bool)
                or not math.isfinite(grace_seconds) or grace_seconds < 0):
            raise ValueError("grace_seconds is invalid")
        if (type(abandoned_grace_seconds) not in (int, float)
                or isinstance(abandoned_grace_seconds, bool)
                or not math.isfinite(abandoned_grace_seconds)
                or abandoned_grace_seconds < 0):
            raise ValueError("abandoned_grace_seconds is invalid")
        now = time.time()
        uploads = files = moved_bytes = 0
        with self._exclusive():
            with _scan_entries(self._uploads_fd) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                if not _ID.fullmatch(name):
                    raise AssetRecoveryRequiredError("unexpected upload directory")
                try:
                    dirs = self._open_owned_upload_any(name)
                except (AssetNotFoundError, AssetRecoveryRequiredError, OSError):
                    continue
                try:
                    swept_files, swept_bytes = self._sweep_one_upload(
                        dirs, now, grace_seconds, abandoned_grace_seconds
                    )
                    if swept_files:
                        uploads += 1
                        files += swept_files
                        moved_bytes += swept_bytes
                except (AssetRecoveryRequiredError, AssetNotFoundError, OSError):
                    continue
                finally:
                    dirs.close()
        return {"uploads": uploads, "files": files, "bytes": moved_bytes}

    def _open_owned_upload_any(self, upload_id: str) -> _UploadDirs:
        upload_id = _validated_id(upload_id, "upload_id")
        try:
            upload_fd = os.open(upload_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._uploads_fd)
        except OSError as exc:
            raise AssetNotFoundError("render upload was not found") from exc
        descriptors = {}
        try:
            for name in ("ranges", "chunks", "assembly", "protections", "changes"):
                descriptors[name] = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=upload_fd)
            return _UploadDirs(upload_id, self.root / "uploads" / upload_id, upload_fd, descriptors["ranges"], descriptors["chunks"], descriptors["assembly"], descriptors["protections"], descriptors["changes"])
        except BaseException:
            for descriptor in descriptors.values():
                os.close(descriptor)
            os.close(upload_fd)
            raise

    def _sweep_one_upload(
        self, dirs: _UploadDirs, now: float, grace_seconds: float,
        abandoned_grace_seconds: float,
    ) -> tuple[int, int]:
        self._recover_changes(dirs)
        head = self._validated_head(dirs)
        if head["state"] == "incomplete":
            return self._sweep_abandoned_upload(dirs, head, now, abandoned_grace_seconds)
        if head["state"] != "published":
            return (0, 0)
        if not self._entry_is_empty(dirs.changes):
            return (0, 0)
        if (now - os.stat("head.json", dir_fd=dirs.upload, follow_symlinks=False).st_mtime) < grace_seconds:
            return (0, 0)
        if self._has_live_protection(dirs, head):
            return (0, 0)
        names = {}
        for source in (dirs.chunks, dirs.assembly):
            with _scan_entries(source) as entries:
                names[source] = [entry.name for entry in entries]
        if not any(names.values()):
            return (0, 0)
        trash_id = f"{dirs.upload_id}-staging-{uuid.uuid4().hex[:12]}"
        os.mkdir(trash_id, 0o700, dir_fd=self._trash_fd)
        os.fsync(self._trash_fd)
        trash_fd = os.open(trash_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._trash_fd)
        files = moved_bytes = 0
        try:
            os.mkdir("chunks", 0o700, dir_fd=trash_fd)
            os.mkdir("assembly", 0o700, dir_fd=trash_fd)
            os.fsync(trash_fd)
            chunk_dst = os.open("chunks", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            assembly_dst = os.open("assembly", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            try:
                for source, target in ((dirs.chunks, chunk_dst), (dirs.assembly, assembly_dst)):
                    for name in names[source]:
                        info = os.stat(name, dir_fd=source, follow_symlinks=False)
                        os.rename(name, name, src_dir_fd=source, dst_dir_fd=target)
                        files += 1
                        moved_bytes += info.st_size
                    os.fsync(source)
                    os.fsync(target)
            finally:
                os.close(chunk_dst)
                os.close(assembly_dst)
        finally:
            os.close(trash_fd)
        return (files, moved_bytes)

    def _sweep_abandoned_upload(
        self, dirs: _UploadDirs, head: dict[str, Any], now: float,
        abandoned_grace_seconds: float,
    ) -> tuple[int, int]:
        """Move a complete, verified upload that never published into `_trash` whole."""
        if head["pending"] is not None or head["committed_offset"] != head["size_bytes"]:
            return (0, 0)
        if not self._entry_is_empty(dirs.changes):
            return (0, 0)
        try:
            if (now - os.stat("head.json", dir_fd=dirs.upload, follow_symlinks=False).st_mtime
                    < abandoned_grace_seconds):
                return (0, 0)
            if self._has_live_protection(dirs, head):
                return (0, 0)
            self._validate_range_records(dirs, head)
            files, moved_bytes = self._tree_size(dirs.upload)
        except (AssetConflictError, AssetRecoveryRequiredError, AssetNotFoundError, OSError):
            return (0, 0)
        trash_id = f"{dirs.upload_id}-abandoned-{uuid.uuid4().hex[:12]}"
        os.mkdir(trash_id, 0o700, dir_fd=self._trash_fd)
        os.fsync(self._trash_fd)
        trash_fd = os.open(trash_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=self._trash_fd)
        try:
            os.rename(dirs.upload_id, dirs.upload_id, src_dir_fd=self._uploads_fd,
                      dst_dir_fd=trash_fd)
            os.fsync(self._uploads_fd)
            os.fsync(trash_fd)
        finally:
            os.close(trash_fd)
        return (files, moved_bytes)

    @staticmethod
    def _entry_is_empty(descriptor: int) -> bool:
        with _scan_entries(descriptor) as entries:
            return next(entries, None) is None

    def _has_live_protection(self, dirs: _UploadDirs, head: dict[str, Any]) -> bool:
        with _scan_entries(dirs.protections) as protections:
            for leaf in protections:
                if re.fullmatch(r"[0-9a-f]{64}\.json", leaf.name) is None:
                    raise AssetRecoveryRequiredError("upload has unknown protection evidence")
                record, _, _ = self._read_record(dirs.protections, leaf.name, "protection")
                if record["upload_id"] != head["upload_id"] or record["asset_handle"] != head["asset_handle"]:
                    raise AssetRecoveryRequiredError("protection record does not match its upload")
                if record["state"] in {"active", "unknown"}:
                    return True
        return False

    def sweep_published(
        self, terminal_at: dict[str, Any], *, grace_seconds: float = _PUBLISHED_GRACE_S,
        protected: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, int]:
        """Retire durable published assets whose renders settled past the grace.

        `terminal_at` comes from `JobStore.terminal_render_asset_handles()`: handle -> the
        settle time, or the identity dict that method returns (its `settled_at` is used).
        `protected` is `JobStore.referenced_render_asset_handles()`: every handle a durable
        row still points at, terminal or not. Only terminal handles, and published assets no
        row references at all and whose asset record is older than the grace, are
        considered; everything else is untouched. Each candidate is re-validated against the
        store before anything moves, including the identity the row recorded, so a handle
        re-created by a later upload is never mistaken for the settled one. Any
        inconsistency leaves it in place. Everything moves into the store's own `_trash` by
        rename (never unlink): the index record, the asset record, the published blob and
        the whole upload directory. The index moves first, so a crash leaks orphan files but
        never leaves a live index pointing at moved data. Transferring `_trash` to macOS
        Trash stays the owner's retention step; this store's `_trash` is not under
        `retention.TRASH_DIR`.
        """
        if (type(grace_seconds) not in (int, float) or isinstance(grace_seconds, bool)
                or not math.isfinite(grace_seconds) or grace_seconds < 0):
            raise ValueError("grace_seconds is invalid")
        if type(terminal_at) is not dict:
            raise ValueError("terminal_at is invalid")
        if type(protected) not in (set, frozenset):
            raise ValueError("protected is invalid")
        now = time.time()
        retired = files = moved_bytes = 0
        with self._exclusive():
            for handle, value in terminal_at.items():
                if type(handle) is not str or _ID.fullmatch(handle) is None:
                    raise ValueError("terminal_at handle is invalid")
                identity = value if type(value) is dict else None
                settled = value.get("settled_at") if identity is not None else value
                if (type(settled) not in (int, float) or isinstance(settled, bool)
                        or not math.isfinite(settled) or settled < 0):
                    raise ValueError("terminal_at timestamp is invalid")
                if now - settled < grace_seconds:
                    continue
                try:
                    count, size = self._retire_one_upload(handle, identity)
                except (AssetRecoveryRequiredError, AssetNotFoundError, OSError):
                    continue
                if count:
                    retired += 1
                    files += count
                    moved_bytes += size
            # A published asset no row references any more (a crash between publication
            # and the delivery record) would otherwise leak forever. Age it from its own
            # record, and never touch a handle any row still points at.
            with _scan_entries(self._assets_fd) as entries:
                names = [entry.name for entry in entries]
            for name in names:
                if re.fullmatch(r"candidate-[0-9a-f]{32}\.json", name) is not None:
                    continue  # publication evidence, hard-linked to its asset record
                if re.fullmatch(r"[0-9a-f]{32}\.json", name) is None:
                    raise AssetRecoveryRequiredError("store has unknown asset evidence")
                handle = name[:-5]
                if handle in terminal_at or handle in protected:
                    continue
                info = os.stat(name, dir_fd=self._assets_fd, follow_symlinks=False)
                if now - info.st_mtime < grace_seconds:
                    continue
                try:
                    count, size = self._retire_one_upload(handle, None)
                except (AssetRecoveryRequiredError, AssetNotFoundError, OSError):
                    continue
                if count:
                    retired += 1
                    files += count
                    moved_bytes += size
        return {"assets": retired, "files": files, "bytes": moved_bytes}

    def _retire_one_upload(self, handle: str, identity: dict[str, Any] | None) -> tuple[int, int]:
        try:
            asset, _, _ = self._read_record(self._assets_fd, f"{handle}.json", "asset")
        except AssetRecoveryRequiredError as exc:
            raise AssetNotFoundError("render asset was not found") from exc
        if asset["asset_handle"] != handle:
            raise AssetRecoveryRequiredError("asset record identity changed")
        if identity is not None:
            owner_scope = identity.get("owner_scope")
            if owner_scope is not None and _owner_token(owner_scope) != asset["owner_token"]:
                raise AssetRecoveryRequiredError("published asset identity changed")
            for field in ("sha256", "size_bytes", "upload_id"):
                value = identity.get(field)
                if value is not None and value != asset[field]:
                    raise AssetRecoveryRequiredError("published asset identity changed")
        dirs = self._open_owned_upload_any(asset["upload_id"])
        try:
            self._recover_changes(dirs)
            head = self._validated_head(dirs)
            if (head["state"] != "published" or head["asset_handle"] != handle
                    or head["owner_token"] != asset["owner_token"]
                    or head["sha256"] != asset["sha256"]
                    or head["size_bytes"] != asset["size_bytes"]):
                raise AssetRecoveryRequiredError("published asset identity changed")
            if not self._entry_is_empty(dirs.changes):
                return (0, 0)
            if self._has_live_protection(dirs, head):
                return (0, 0)
            return self._relocate_asset(dirs, asset)
        finally:
            dirs.close()

    def _relocate_asset(self, dirs: _UploadDirs, asset: dict[str, Any]) -> tuple[int, int]:
        handle = asset["asset_handle"]
        trash_id = f"retired-{handle}-{uuid.uuid4().hex[:12]}"
        os.mkdir(trash_id, 0o700, dir_fd=self._trash_fd)
        os.fsync(self._trash_fd)
        trash_fd = os.open(trash_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._trash_fd)
        files = moved_bytes = 0
        try:
            for name in ("index", "assets", "published", "uploads"):
                os.mkdir(name, 0o700, dir_fd=trash_fd)
            os.fsync(trash_fd)
            index_dst = os.open("index", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            assets_dst = os.open("assets", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            published_dst = os.open("published", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            uploads_dst = os.open("uploads", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=trash_fd)
            try:
                # 1. Index record first: a crash after this leaves recoverable orphans,
                #    never an index that still resolves to moved data.
                index_name = f"{asset['owner_token']}-{asset['sha256']}.json"
                if self._entry_exists(self._index_fd, index_name):
                    init, _, _ = self._read_record(self._index_fd, index_name, "init")
                    if (init["upload_id"] == asset["upload_id"]
                            and init["asset_handle"] == handle):
                        info = os.stat(index_name, dir_fd=self._index_fd, follow_symlinks=False)
                        os.rename(index_name, index_name, src_dir_fd=self._index_fd, dst_dir_fd=index_dst)
                        files += 1
                        moved_bytes += info.st_size
                os.fsync(self._index_fd)
                os.fsync(index_dst)
                # 2. The asset record and its publication candidate (one inode, two names).
                for asset_name in (f"{handle}.json", f"candidate-{asset['operation_id']}.json"):
                    if not self._entry_exists(self._assets_fd, asset_name):
                        continue
                    info = os.stat(asset_name, dir_fd=self._assets_fd, follow_symlinks=False)
                    os.rename(asset_name, asset_name, src_dir_fd=self._assets_fd, dst_dir_fd=assets_dst)
                    files += 1
                    moved_bytes += info.st_size
                os.fsync(self._assets_fd)
                os.fsync(assets_dst)
                # 3. The published blob (the durable copy this whole sweep is reclaiming).
                blob_name = f"{handle}.blob"
                if self._entry_exists(self._published_fd, blob_name):
                    info = os.stat(blob_name, dir_fd=self._published_fd, follow_symlinks=False)
                    os.rename(blob_name, blob_name, src_dir_fd=self._published_fd, dst_dir_fd=published_dst)
                    files += 1
                    moved_bytes += info.st_size
                os.fsync(self._published_fd)
                os.fsync(published_dst)
                # 4. The whole upload directory, counted as one move.
                tree_files, tree_bytes = self._tree_size(dirs.upload)
                os.rename(dirs.upload_id, dirs.upload_id, src_dir_fd=self._uploads_fd, dst_dir_fd=uploads_dst)
                files += tree_files
                moved_bytes += tree_bytes
                os.fsync(self._uploads_fd)
                os.fsync(uploads_dst)
            finally:
                for descriptor in (index_dst, assets_dst, published_dst, uploads_dst):
                    os.close(descriptor)
        finally:
            os.close(trash_fd)
        return (files, moved_bytes)

    @staticmethod
    def _tree_size(descriptor: int) -> tuple[int, int]:
        files = size = 0
        with _scan_entries(descriptor) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        sub_files, sub_size = RenderAssetStore._tree_size(child)
                    finally:
                        os.close(child)
                    files += sub_files
                    size += sub_size
                else:
                    files += 1
                    size += info.st_size
        return (files, size)
