"""Private Content Pack archive shelf, not a store or an ownership authority.

Checks ZIP safety, manifest identity and every declared file hash. Story Studio
still owns document/schema compatibility and receipt/signature verification.
Archives are never extracted, executed, signed, purchased or installed here.
"""
from __future__ import annotations

import hashlib
import errno
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
import zlib

from .config import DATA_DIR

LIBRARY = DATA_DIR / "content-library"
MAX_ARCHIVE = 50 * 1024**2
MAX_EXPANDED = 100 * 1024**2
MAX_JSON = 4 * 1024**2
KINDS = {"series-formula", "topic-pack", "visual-style", "thumbnail-recipe",
         "subtitle-style", "scene-effect", "overlay", "lettering-style", "metadata-formula"}
IDENTITY = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+")
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
DIGEST = re.compile(r"[a-f0-9]{64}")


class PackError(ValueError):
    pass


def _json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PackError("Duplicate JSON key.")
            result[key] = value
        return result
    def invalid(_):
        raise PackError("Non-finite JSON number.")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PackError("Invalid or ambiguous JSON.") from exc


def _safe_path(path: str) -> str:
    if (not path or len(path) > 240 or "\\" in path or "\0" in path
            or ":" in path or any(part in ("", ".", "..") for part in path.split("/"))):
        raise PackError("Unsafe archive path.")
    return unicodedata.normalize("NFC", path).casefold()


def inspect_archive(data: bytes) -> dict:
    if not data or len(data) > MAX_ARCHIVE:
        raise PackError("Pack archive must be nonempty and at most 50 MiB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if archive.comment or not entries or len(entries) > 256:
                raise PackError("Invalid archive entry count or comment.")
            seen = set()
            total = 0
            for entry in entries:
                name = _safe_path(entry.filename)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (name in seen or mode not in (0, stat.S_IFREG) or entry.is_dir()
                        or entry.extra or entry.comment or entry.flag_bits & ~(0x800 | 0x6)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise PackError("Duplicate, special, encrypted or unsupported ZIP entry.")
                seen.add(name)
                total += entry.file_size
                limit = MAX_JSON if entry.filename.endswith(".json") else 15 * 1024**2
                if entry.file_size > limit or total > MAX_EXPANDED:
                    raise PackError("Expanded archive size exceeds the safe limit.")
            manifest = _json(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or type(manifest.get("pack_format")) is not int or manifest["pack_format"] != 1:
                raise PackError("Only Content Pack format 1 is supported.")
            for key in ("pack_id", "publisher_id"):
                if not isinstance(manifest.get(key), str) or len(manifest[key]) > 160 or not IDENTITY.fullmatch(manifest[key]):
                    raise PackError(f"Invalid {key}.")
            for key in ("version", "min_app_version"):
                if not isinstance(manifest.get(key), str) or not VERSION.fullmatch(manifest[key]):
                    raise PackError(f"Invalid {key}.")
            profile = manifest.get("delivery_profile")
            signature = "signature.ed25519" in archive.namelist()
            if profile == "genstudio-store-receipt-v1":
                if signature:
                    raise PackError("Store archives cannot contain a publisher signature.")
                trust = "Requires original account-bound Store receipts and trusted signing-key history."
            elif profile is None and signature:
                if len(archive.read("signature.ed25519")) != 64:
                    raise PackError("Publisher signature must contain 64 bytes.")
                trust = "Publisher signature is present but has not been verified against Story Studio trust."
            else:
                raise PackError("Missing signature or unsupported delivery profile.")
            files = manifest.get("files")
            items = manifest.get("items")
            if not isinstance(files, list) or not files or not isinstance(items, list) or not items:
                raise PackError("Manifest needs declared files and items.")
            declared = set()
            for row in files:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise PackError("Invalid declared file.")
                path = row["path"]
                _safe_path(path)
                if not path.startswith(("content/", "assets/")) or path in declared:
                    raise PackError("Invalid or duplicate declared file.")
                declared.add(path)
                content = archive.read(path)  # zipfile also checks local headers and CRC
                if (type(row.get("bytes")) is not int or row["bytes"] != len(content)
                        or row.get("sha256") != hashlib.sha256(content).hexdigest()):
                    raise PackError("Manifest file hash or length does not match.")
                if path.endswith(".json"):
                    _json(content)
            allowed = declared | {"manifest.json"} | ({"signature.ed25519"} if signature else set())
            if set(archive.namelist()) != allowed:
                raise PackError("Archive contains undeclared files.")
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                        or item["kind"] not in KINDS or not isinstance(item.get("path"), str)
                        or item["path"] not in declared or type(item.get("schema_version")) is not int
                        or item["schema_version"] != 1):
                    raise PackError("Unsupported or undeclared content item.")
            # The pack's own name, so the preserved-archive list can show what a pack
            # *is* rather than its 60-character identity. The document was already
            # read and verified above; this only parses it again to take `name`.
            title = ""
            if items:
                document = _json(archive.read(items[0]["path"]))
                if isinstance(document, dict) and isinstance(document.get("name"), str):
                    title = document["name"].strip()[:200]
            return {
                "archive_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                **{key: manifest[key] for key in ("pack_id", "publisher_id", "version", "min_app_version")},
                "title": title,
                "delivery_profile": profile, "item_kinds": sorted({item["kind"] for item in items}),
                "file_count": len(files), "integrity": "verified", "ownership": "unverified",
                "installable": False, "trust_requirement": trust,
                "compatibility": "Not verified; Story Studio must validate document schemas and app version.",
            }
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, NotImplementedError, zlib.error, UnicodeError) as exc:
        raise PackError("Unreadable or invalid Content Pack archive.") from exc


def import_archive(data: bytes, *, root: Path = LIBRARY) -> dict:
    metadata = inspect_archive(data)
    if root.is_symlink():
        raise PackError("Library root cannot be a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / metadata["archive_sha256"]
    if target.exists():
        read_archive(metadata["archive_sha256"], root=root)
        return metadata
    staging = Path(tempfile.mkdtemp(prefix=".import-", dir=root))
    try:
        for name, content in (("archive.sspack", data), ("metadata.json", json.dumps(metadata).encode())):
            with (staging / name).open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        try:
            staging.rename(target)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            read_archive(metadata["archive_sha256"], root=root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)  # only this operation's freshly-created temp directory
    return metadata


def inventory(*, root: Path = LIBRARY) -> list[dict]:
    rows = []
    for folder in sorted(root.glob("*")):
        if not DIGEST.fullmatch(folder.name) or folder.is_symlink() or (folder / "metadata.json").is_symlink():
            continue
        try:
            row = _json((folder / "metadata.json").read_bytes())
            if isinstance(row, dict) and row.get("archive_sha256") == folder.name:
                try:
                    read_archive(folder.name, root=root)
                    integrity, detail = "verified", "Current archive SHA-256 matches the preserved original."
                except PackError as exc:
                    integrity, detail = "unavailable", str(exc)
                rows.append({**row, "integrity": integrity, "integrity_detail": detail,
                             "ownership": "unverified", "installable": False})
        except (OSError, PackError):
            rows.append({"archive_sha256": folder.name, "integrity": "unreadable", "installable": False})
    return rows


def read_archive(digest: str, *, root: Path = LIBRARY) -> bytes:
    if not DIGEST.fullmatch(digest):
        raise PackError("Invalid archive identity.")
    path = root / digest / "archive.sspack"
    if path.is_symlink() or path.parent.is_symlink():
        raise PackError("Library archives cannot be symlinks.")
    try:
        with path.open("rb") as source:
            data = source.read(MAX_ARCHIVE + 1)
    except OSError as exc:
        raise PackError("Archived file is missing or unreadable.") from exc
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != digest:
        raise PackError("Archived file no longer matches its saved SHA-256.")
    return data
