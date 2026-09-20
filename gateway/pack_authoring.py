"""Private Topic Pack drafts backed by Story Studio's authoring tools."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator

from . import config, content_packs
from .config import DATA_DIR


MAX_DOCUMENT_BYTES = 4 * 1024**2
MAX_RECORD_BYTES = MAX_DOCUMENT_BYTES * 6 + 64 * 1024
MAX_TOOL_OUTPUT = 8 * 1024**2
MAX_CANDIDATE_METADATA_BYTES = 4096
AUTHORING_TIMEOUT = 15.0
MAX_ARCHIVE = content_packs.MAX_ARCHIVE
_ID = re.compile(r"[a-f0-9]{64}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_BRIDGE = Path(__file__).with_name("story_authoring.mjs")
_DEFAULT_STORY_SOURCE = Path.home() / "Developer" / "story-studio-kh"
_HOMEBREW_NODE = Path("/opt/homebrew/bin/node")
_SAFE_ENV = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE")


class AuthoringError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def _root() -> Path:
    return Path(DATA_DIR) / "content-authoring"


def _unsafe(path: Path) -> None:
    if path.is_symlink():
        raise AuthoringError("Authoring storage cannot contain symlinks.", 422)


def _ensure_root() -> Path:
    data = Path(DATA_DIR)
    _unsafe(data)
    try:
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data, 0o700)
    except OSError as exc:
        raise AuthoringError("Authoring storage is unavailable.", 503) from exc
    root = _root()
    _unsafe(root)
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        if not root.is_dir():
            raise AuthoringError("Authoring storage is not a directory.", 503)
        os.chmod(root, 0o700)
    except AuthoringError:
        raise
    except OSError as exc:
        raise AuthoringError("Authoring storage is unavailable.", 503) from exc
    return root


def _existing_root() -> Path | None:
    data = Path(DATA_DIR)
    _unsafe(data)
    if not data.exists():
        return None
    root = _root()
    _unsafe(root)
    if not root.exists():
        return None
    if not root.is_dir():
        raise AuthoringError("Authoring storage is not a directory.", 503)
    return root


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    lock_path = root / ".lock"
    _unsafe(lock_path)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except AuthoringError:
        raise
    except OSError as exc:
        raise AuthoringError("Authoring storage is unavailable.", 503) from exc


def _sync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AuthoringError("Authoring storage could not be made durable.", 503) from exc


def _atomic_write(path: Path, data: bytes, *, replace: bool = True) -> None:
    _unsafe(path.parent)
    _unsafe(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb", closefd=True) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path, follow_symlinks=False)
            temporary_path.unlink()
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        _sync_directory(path.parent)
    except FileExistsError as exc:
        raise AuthoringError("Draft already exists.", 409) from exc
    except OSError as exc:
        raise AuthoringError("Authoring storage is unavailable.", 503) from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _require_key(key: str) -> str:
    if type(key) is not str or not key or len(key.encode("utf-8")) > 240:
        raise AuthoringError("Reservation key is invalid.", 422)
    return key


def _require_document_text(document_text: str) -> str:
    if type(document_text) is not str:
        raise AuthoringError("Document text must be a string.", 422)
    try:
        size = len(document_text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AuthoringError("Document text must be valid UTF-8.", 422) from exc
    if size > MAX_DOCUMENT_BYTES:
        raise AuthoringError("Document text is limited to 4 MiB.", 413)
    return document_text


def _require_revision(revision: int) -> int:
    if type(revision) is not int or revision < 1:
        raise AuthoringError("Revision must be a positive integer.", 422)
    return revision


def _draft_id(key: str, receipt_ref: str) -> str:
    value = json.dumps([key, receipt_ref], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _draft_dir(root: Path, draft_id: str, *, create: bool = False) -> Path:
    _require_draft_id(draft_id)
    directory = root / draft_id
    _unsafe(directory)
    if directory.exists() and not directory.is_dir():
        raise AuthoringError("Draft storage is not a directory.", 503)
    if create and not directory.exists():
        try:
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise AuthoringError("Authoring storage is unavailable.", 503) from exc
    if directory.exists():
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise AuthoringError("Authoring storage is unavailable.", 503) from exc
    return directory


def _require_draft_id(draft_id: str) -> str:
    if type(draft_id) is not str or not _ID.fullmatch(draft_id):
        raise AuthoringError("Draft identity is invalid.", 422)
    return draft_id


def _draft_path(directory: Path) -> Path:
    path = directory / "draft.json"
    _unsafe(path)
    return path


def _candidate_path(directory: Path) -> Path:
    path = directory / "candidate.sspack"
    _unsafe(path)
    return path


def _candidate_metadata_path(directory: Path) -> Path:
    path = directory / "candidate.json"
    _unsafe(path)
    return path


def _decode_json(data: bytes, detail: str, status: int = 503) -> Any:
    try:
        return content_packs._json(data)
    except content_packs.PackError as exc:
        raise AuthoringError(detail, status) from exc


def _read_bounded_file(path: Path, maximum: int, *, missing: str, unreadable: str,
                       unsafe: str, too_large: str, status: int,
                       missing_status: int | None = None) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or nonblock is None:
        raise AuthoringError(unsafe, status)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
    except FileNotFoundError as exc:
        raise AuthoringError(missing, status if missing_status is None else missing_status) from exc
    except OSError as exc:
        raise AuthoringError(unreadable, status) from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AuthoringError(unsafe, status)
            if before.st_size > maximum:
                raise AuthoringError(too_large, status)
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except AuthoringError:
        raise
    except OSError as exc:
        raise AuthoringError(unreadable, status) from exc
    if (len(data) > maximum or len(data) != after.st_size
            or before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns):
        raise AuthoringError(too_large if len(data) > maximum else unsafe, status)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise AuthoringError(unsafe, status) from exc
    if (not stat.S_ISREG(current.st_mode) or current.st_dev != after.st_dev
            or current.st_ino != after.st_ino):
        raise AuthoringError(unsafe, status)
    return data


def _sync_file(path: Path) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or nonblock is None:
        raise AuthoringError("Candidate archive cannot be made durable safely.", 503)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | nonblock)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AuthoringError("Candidate archive is not a regular file.", 503)
            os.fsync(fd)
        finally:
            os.close(fd)
    except AuthoringError:
        raise
    except OSError as exc:
        raise AuthoringError("Candidate archive could not be made durable.", 503) from exc


def _read_record(path: Path) -> dict[str, Any]:
    data = _read_bounded_file(
        path, MAX_RECORD_BYTES,
        missing="Draft was not found.", unreadable="Draft is unreadable.",
        unsafe="Draft record is not a safe regular file.",
        too_large="Draft record exceeds its storage limit.", status=503, missing_status=404,
    )
    value = _decode_json(data, "Draft record is invalid.")
    if not isinstance(value, dict):
        raise AuthoringError("Draft record is invalid.", 503)
    required = {"id", "key", "receipt_ref", "pack_id", "version", "item_id",
                "document_text", "revision", "sealed"}
    if set(value) != required:
        raise AuthoringError("Draft record is invalid.", 503)
    if (type(value["id"]) is not str or not _ID.fullmatch(value["id"])
            or any(type(value[key]) is not str for key in ("key", "receipt_ref", "pack_id", "version", "item_id"))
            or type(value["document_text"]) is not str
            or type(value["revision"]) is not int or value["revision"] < 1
            or type(value["sealed"]) is not bool):
        raise AuthoringError("Draft record is invalid.", 503)
    _require_document_text(value["document_text"])
    return value


def _load_draft(root: Path, draft_id: str, *, derive_sealed: bool = True) -> dict[str, Any]:
    directory = _draft_dir(root, draft_id)
    draft = _read_record(_draft_path(directory))
    if draft["id"] != draft_id:
        raise AuthoringError("Draft identity does not match its path.", 503)
    candidate = _candidate_path(directory)
    metadata = _candidate_metadata_path(directory)
    if derive_sealed and (candidate.exists() or metadata.exists()):
        draft = {**draft, "sealed": True}
    return draft


def _public_draft(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": draft["id"],
        "key": draft["key"],
        "receipt_ref": draft["receipt_ref"],
        "pack_id": draft["pack_id"],
        "version": draft["version"],
        "item_id": draft["item_id"],
        "document_text": draft["document_text"],
        "revision": draft["revision"],
        "sealed": draft["sealed"],
    }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _strict_document(document_text: str) -> dict[str, Any]:
    _require_document_text(document_text)
    try:
        document = content_packs._json(document_text.encode("utf-8"))
    except content_packs.PackError as exc:
        raise AuthoringError(str(exc), 422) from exc
    if not isinstance(document, dict):
        raise AuthoringError("Topic Pack document must be a JSON object.", 422)
    return document


def _node_executable() -> str:
    configured = config.get("GATEWAY_AUTHORING_NODE", "").strip()
    if configured:
        return configured
    found = shutil.which("node")
    if found:
        return found
    if _HOMEBREW_NODE.is_file():
        return str(_HOMEBREW_NODE)
    return "node"


def _story_source() -> str:
    configured = config.get("GATEWAY_STORY_SOURCE", "").strip()
    return str(Path(configured or _DEFAULT_STORY_SOURCE).expanduser())


def _tool_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
    environment.setdefault("PATH", os.defpath)
    return environment


def _safe_tool_error(value: Any) -> str:
    if not isinstance(value, str):
        return "Story authoring tool rejected the request."
    message = " ".join(value.split())
    if not message or len(message) > 512:
        return "Story authoring tool rejected the request."
    return message


def _tool_result(stdout: bytes, *, failed: bool = False) -> Any:
    if len(stdout) > MAX_TOOL_OUTPUT:
        raise AuthoringError("Story authoring tool returned too much output.", 503)
    try:
        result = content_packs._json(stdout)
    except content_packs.PackError as exc:
        raise AuthoringError("Story authoring tool returned invalid output.", 503) from exc
    if isinstance(result, dict) and "error" in result:
        raise AuthoringError(_safe_tool_error(result["error"]), 422 if failed else 503)
    return result


def _run_process(argv: list[str], input_bytes: bytes, environment: dict[str, str], *,
                 timeout: float, output_limit: int) -> Any:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            close_fds=True,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AuthoringError("Story authoring tools are unavailable.", 503) from exc

    stdout = bytearray()
    stderr = bytearray()
    total = 0
    guard = threading.Lock()
    too_large = threading.Event()

    def collect(pipe, target: bytearray) -> None:
        nonlocal total
        try:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    return
                with guard:
                    if total + len(chunk) > output_limit:
                        too_large.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                        return
                    total += len(chunk)
                    target.extend(chunk)
        except (OSError, ValueError):
            return

    def send() -> None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    readers = [
        threading.Thread(target=collect, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=collect, args=(process.stderr, stderr), daemon=True),
    ]
    writer = threading.Thread(target=send, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if too_large.is_set():
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise AuthoringError("Story authoring tool returned too much output.", 503)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise AuthoringError("Story authoring tool timed out.", 503)
            try:
                process.wait(timeout=min(remaining, 0.05))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        for thread in (*readers, writer):
            thread.join(timeout=1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass
    if too_large.is_set():
        raise AuthoringError("Story authoring tool returned too much output.", 503)
    return type("Completed", (), {
        "returncode": process.returncode,
        "stdout": bytes(stdout),
        "stderr": bytes(stderr),
    })()


def _run_authoring(action: str, payload: dict[str, Any] | None = None,
                   *, output_path: Path | None = None) -> Any:
    request = {"action": action, **(payload or {})}
    try:
        encoded = _json_bytes(request)
    except (TypeError, ValueError) as exc:
        raise AuthoringError("Authoring request could not be encoded.", 422) from exc
    if len(encoded) > MAX_TOOL_OUTPUT:
        raise AuthoringError("Authoring request is too large.", 413)
    argv = [_node_executable(), str(_BRIDGE), _story_source()]
    if output_path is not None:
        argv.append(str(output_path))
    try:
        completed = _run_process(
            argv,
            encoded,
            _tool_environment(),
            timeout=AUTHORING_TIMEOUT,
            output_limit=MAX_TOOL_OUTPUT,
        )
    except FileNotFoundError as exc:
        raise AuthoringError("Story authoring tools are unavailable: Node was not found.", 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthoringError("Story authoring tool timed out.", 503) from exc
    except OSError as exc:
        raise AuthoringError("Story authoring tools are unavailable.", 503) from exc
    return _tool_result(completed.stdout, failed=completed.returncode != 0)


def _reservation(result: Any, key: str) -> dict[str, str]:
    required = ("key", "item_id", "pack_id", "version", "receipt_ref")
    if not isinstance(result, dict) or any(type(result.get(name)) is not str for name in required):
        raise AuthoringError("Story returned an invalid reservation.", 503)
    if result["key"] != key:
        raise AuthoringError("Story reservation did not match the requested key.", 409)
    return {name: result[name] for name in required}


def catalogue() -> dict[str, Any]:
    try:
        result = _run_authoring("catalogue")
        if (not isinstance(result, dict) or not isinstance(result.get("reservations"), list)
                or not isinstance(result.get("templates"), list)):
            raise AuthoringError("Story catalogue returned an invalid result.", 503)
        return {**result, "available": True}
    except AuthoringError as exc:
        return {
            "available": False,
            "detail": f"Authoring prerequisites are unavailable: {str(exc)}",
            "reservations": [],
            "templates": [],
        }


def template(key: str) -> dict[str, Any]:
    key = _require_key(key)
    result = _run_authoring("template", {"key": key})
    if not isinstance(result, dict) or "document" not in result:
        raise AuthoringError("Story returned an invalid template.", 503)
    return result


def create_draft(key: str, document_text: str) -> dict[str, Any]:
    key = _require_key(key)
    document_text = _require_document_text(document_text)
    root = _ensure_root()
    with _lock(root):
        reservation = _reservation(_run_authoring("reservation", {"key": key}), key)
        draft_id = _draft_id(key, reservation["receipt_ref"])
        directory = _draft_dir(root, draft_id, create=True)
        path = _draft_path(directory)
        if path.exists():
            return _public_draft(_load_draft(root, draft_id))
        draft = {
            "id": draft_id,
            "key": key,
            "receipt_ref": reservation["receipt_ref"],
            "pack_id": reservation["pack_id"],
            "version": reservation["version"],
            "item_id": reservation["item_id"],
            "document_text": document_text,
            "revision": 1,
            "sealed": False,
        }
        _atomic_write(path, _json_bytes(draft), replace=False)
        return _public_draft(draft)


def list_drafts() -> list[dict[str, Any]]:
    root = _existing_root()
    if root is None:
        return []
    with _lock(root):
        drafts = []
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise AuthoringError("Authoring storage is unavailable.", 503) from exc
        for child in children:
            if child.name == ".lock" or child.name.startswith(".") or child.is_symlink():
                continue
            if not child.is_dir() or not _ID.fullmatch(child.name):
                continue
            try:
                drafts.append(_public_draft(_load_draft(root, child.name)))
            except AuthoringError:
                continue
        return drafts


def read_draft(draft_id: str) -> dict[str, Any]:
    _require_draft_id(draft_id)
    root = _existing_root()
    if root is None:
        raise AuthoringError("Draft was not found.", 404)
    with _lock(root):
        return _public_draft(_load_draft(root, draft_id))


def save_draft(draft_id: str, document_text: str, revision: int) -> dict[str, Any]:
    _require_draft_id(draft_id)
    document_text = _require_document_text(document_text)
    revision = _require_revision(revision)
    root = _existing_root()
    if root is None:
        raise AuthoringError("Draft was not found.", 404)
    with _lock(root):
        draft = _load_draft(root, draft_id)
        if draft["sealed"]:
            raise AuthoringError("Exported drafts are immutable; reserve a new version to edit.", 409)
        if revision != draft["revision"]:
            raise AuthoringError("Draft changed; reload before saving.", 409)
        updated = {**draft, "document_text": document_text, "revision": revision + 1}
        _atomic_write(_draft_path(_draft_dir(root, draft_id)), _json_bytes(updated))
        return _public_draft(updated)


def _check_revision(draft: dict[str, Any], revision: int) -> None:
    if revision != draft["revision"]:
        raise AuthoringError("Draft changed; reload before continuing.", 409)


def _expected_reservation(draft: dict[str, Any]) -> dict[str, str]:
    return {name: draft[name] for name in ("key", "item_id", "pack_id", "version", "receipt_ref")}


def preview_draft(draft_id: str, revision: int) -> dict[str, Any]:
    _require_draft_id(draft_id)
    revision = _require_revision(revision)
    root = _existing_root()
    if root is None:
        raise AuthoringError("Draft was not found.", 404)
    with _lock(root):
        draft = _load_draft(root, draft_id)
        _check_revision(draft, revision)
        document = _strict_document(draft["document_text"])
        result = _run_authoring("validate", {
            "key": draft["key"],
            "expected_reservation": _expected_reservation(draft),
            "document": document,
        })
        if not isinstance(result, dict) or "document" not in result or "manifest" not in result:
            raise AuthoringError("Story returned an invalid preview.", 503)
        return result


def _read_candidate(directory: Path, draft: dict[str, Any]) -> tuple[bytes, dict[str, Any]] | None:
    candidate = _candidate_path(directory)
    metadata_path = _candidate_metadata_path(directory)
    if metadata_path.exists() and not candidate.exists():
        raise AuthoringError(
            "Saved candidate metadata exists, but the exact candidate bytes are unavailable; reserve a new version.",
            410,
        )
    if not candidate.exists():
        if draft["sealed"]:
            raise AuthoringError("Saved candidate is missing.", 410)
        return None
    if not metadata_path.exists():
        raise AuthoringError(
            "Saved candidate has no binding metadata, so its bytes cannot be authorized; reserve a new version.",
            410,
        )
    metadata_bytes = _read_bounded_file(
        metadata_path, MAX_CANDIDATE_METADATA_BYTES,
        missing="Saved candidate has no binding metadata.",
        unreadable="Saved candidate metadata is unreadable.",
        unsafe="Saved candidate metadata is not a safe regular file.",
        too_large="Saved candidate metadata is too large.", status=410,
    )
    metadata = _decode_json(metadata_bytes, "Saved candidate metadata is invalid.", 410)
    if (not isinstance(metadata, dict)
            or set(metadata) != {"sha256", "bytes", "pack_id", "version"}
            or type(metadata.get("sha256")) is not str or not _DIGEST.fullmatch(metadata["sha256"])
            or type(metadata.get("bytes")) is not int or metadata["bytes"] < 1
            or metadata["bytes"] > MAX_ARCHIVE
            or type(metadata.get("pack_id")) is not str
            or type(metadata.get("version")) is not str
            or metadata["pack_id"] != draft["pack_id"]
            or metadata["version"] != draft["version"]):
        raise AuthoringError("Saved candidate metadata is invalid.", 410)
    data = _read_bounded_file(
        candidate, MAX_ARCHIVE,
        missing="Saved candidate bytes are missing.", unreadable="Saved candidate is unreadable.",
        unsafe="Saved candidate is not a safe regular file.",
        too_large="Saved candidate is outside the archive size limit.", status=410,
    )
    digest = hashlib.sha256(data).hexdigest()
    if (not data or metadata["sha256"] != digest or metadata["bytes"] != len(data)):
        raise AuthoringError("Saved candidate no longer matches its saved SHA-256.", 410)
    return data, metadata


def _read_build_output(path: Path) -> bytes:
    _unsafe(path)
    data = _read_bounded_file(
        path, MAX_ARCHIVE,
        missing="Story did not produce a candidate archive.",
        unreadable="Candidate archive is unreadable.",
        unsafe="Candidate archive is not a safe regular file.",
        too_large="Candidate archive is outside the archive size limit.", status=503,
    )
    if not data or len(data) > MAX_ARCHIVE:
        raise AuthoringError("Candidate archive is outside the archive size limit.", 422)
    _sync_file(path)
    return data


def _build_metadata(result: Any, data: bytes, draft: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AuthoringError("Story returned an invalid build result.", 503)
    expected_digest = hashlib.sha256(data).hexdigest()
    if (type(result.get("sha256")) is not str or not _DIGEST.fullmatch(result["sha256"])
            or result["sha256"] != expected_digest
            or type(result.get("bytes")) is not int or result["bytes"] != len(data)
            or type(result.get("pack_id")) is not str or result["pack_id"] != draft["pack_id"]
            or type(result.get("version")) is not str or result["version"] != draft["version"]):
        raise AuthoringError("Story returned candidate bytes with mismatched digest, length, or identity.", 422)
    return {
        "sha256": expected_digest,
        "bytes": len(data),
        "pack_id": draft["pack_id"],
        "version": draft["version"],
    }


def _install_candidate(temporary: Path, candidate: Path) -> None:
    _unsafe(temporary)
    _unsafe(candidate)
    if candidate.exists():
        raise AuthoringError("A candidate already exists for this reservation.", 409)
    try:
        os.link(temporary, candidate, follow_symlinks=False)
        os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
        temporary.unlink()
        _sync_directory(candidate.parent)
    except FileExistsError as exc:
        raise AuthoringError("A candidate already exists for this reservation.", 409) from exc
    except OSError as exc:
        raise AuthoringError("Authoring storage is unavailable.", 503) from exc


def _seal(directory: Path, draft: dict[str, Any]) -> None:
    _atomic_write(_draft_path(directory), _json_bytes({**draft, "sealed": True}))


def export_draft(draft_id: str, revision: int) -> tuple[bytes, dict[str, Any]]:
    _require_draft_id(draft_id)
    revision = _require_revision(revision)
    root = _existing_root()
    if root is None:
        raise AuthoringError("Draft was not found.", 404)
    with _lock(root):
        directory = _draft_dir(root, draft_id)
        draft = _load_draft(root, draft_id, derive_sealed=False)
        _check_revision(draft, revision)
        existing = _read_candidate(directory, draft)
        if existing is not None:
            data, metadata = existing
            if not draft["sealed"]:
                _seal(directory, draft)
            return data, metadata
        document = _strict_document(draft["document_text"])
        try:
            staging = Path(tempfile.mkdtemp(prefix=".build-", dir=str(directory)))
            os.chmod(staging, 0o700)
        except OSError as exc:
            raise AuthoringError("Authoring storage is unavailable.", 503) from exc
        temporary = staging / "candidate.sspack"
        try:
            result = _run_authoring(
                "build",
                {
                    "key": draft["key"],
                    "expected_reservation": _expected_reservation(draft),
                    "document": document,
                },
                output_path=temporary,
            )
            data = _read_build_output(temporary)
            metadata = _build_metadata(result, data, draft)
            _atomic_write(_candidate_metadata_path(directory), _json_bytes(metadata), replace=False)
            _install_candidate(temporary, _candidate_path(directory))
            installed = _read_candidate(directory, draft)
            if installed is None:
                raise AuthoringError("Candidate installation did not persist exact bytes.", 503)
            data, metadata = installed
            _seal(directory, draft)
            return data, metadata
        finally:
            try:
                if staging.is_symlink():
                    staging.unlink()
                elif staging.exists():
                    shutil.rmtree(staging)
            except FileNotFoundError:
                pass
