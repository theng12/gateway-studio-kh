"""Footage library — the authoritative stock store, mirroring `sound_library`.

One library for every channel, at a **configurable root** (default
``DATA_DIR/footage-library/``) so the NAS mounted on 1301 is a setting rather than
a path written into the code::

    <sha256>.<ext>      # the clip bytes (mp4/webm/mov/mkv/m4v/avi)
    <sha256>.json       # metadata sidecar: probe, licence, provenance, source
    index.json          # ordered list of shas, newest first

The configured root is persisted in ``DATA_DIR/footage-library.json`` and
`set_root` refuses a directory that is missing or not writable instead of quietly
accepting a typo. A configured root that has gone away (an unmounted disk) is
reported and the default is used, so the library keeps answering.

**Unknown is null, and readers say so.** A clip whose duration, resolution or
licence was never recorded stores `null` and `describe()` names it under
`not_recorded` — never `0`, never a guessed value. **A clip with no licence is not
silently usable**: a connector may only be imported with a commercial-safe
licence, while an explicit owner/generated import may state its own.

**Nothing is deleted for good.** `retire()` moves the bytes and sidecar to
``DATA_DIR/_trash/footage-library/`` so a mistaken retirement is a move back.

ponytail: the same flat JSON index `sound_library` uses. A footage library is
hundreds to thousands of clips, not millions; upgrade to the SQLite store if the
index passes ~10k entries or concurrent writers start colliding.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .sound_sources import _find_ffprobe, hash_bytes, normalise_licence

log = logging.getLogger("gateway.footage_library")

#: Containers this library stores, and the media type each is served with.
VIDEO_MEDIA_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
}
VIDEO_FORMATS = tuple(VIDEO_MEDIA_TYPES)

#: Licence words a monetised video may carry, beyond the CC pair `normalise_licence`
#: already accepts. Each is the provider's own free-use licence.
COMMERCIAL_SAFE_LICENCES = frozenset({"pexels", "pixabay", "public domain", "pdm"})

#: The sidecar fields a reader may ask about; `describe` names the unrecorded ones.
RECORDED_FIELDS = (
    "title", "description", "duration_seconds", "width", "height", "resolution",
    "fps", "codec", "tags", "licence", "licence_note", "provenance", "ai_generated",
    "source", "source_id", "source_url", "author", "attribution",
)

NOT_RECORDED = "not recorded"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Root: configurable, persisted, never a hard-coded NAS path
# ---------------------------------------------------------------------------
def _config_path() -> Path:
    return config.DATA_DIR / "footage-library.json"


def default_root() -> Path:
    return config.DATA_DIR / "footage-library"


def _configured_root() -> str | None:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    root = data.get("root") if isinstance(data, dict) else None
    return root if isinstance(root, str) and root.strip() else None


def get_root() -> Path:
    """The library's root: the configured one when it is there, else the default.

    A configured root that has gone away (an unmounted NAS) is **not** silently
    replaced without a word: it is logged, and the default is used so the library
    still answers instead of failing every route.
    """
    configured = _configured_root()
    if configured:
        path = Path(configured).expanduser()
        if path.is_dir():
            return path
        log.warning("footage library: configured root %s is not available; using %s",
                    path, default_root())
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def set_root(path: str | Path) -> Path:
    """Persist a new root. Refuses a directory that is missing or not writable."""
    target = Path(str(path)).expanduser()
    if not target.exists():
        raise ValueError(f"The footage library root does not exist: {target}")
    if not target.is_dir():
        raise ValueError(f"The footage library root is not a directory: {target}")
    probe = target / f".footage-write-test-{time.time_ns()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise ValueError(
            f"The footage library root is not writable: {target} ({exc.strerror or exc})"
        ) from exc
    from .config import write_secret
    write_secret(_config_path(), json.dumps({"root": str(target)}, indent=2) + "\n")
    return target


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def _index_path() -> Path:
    return get_root() / "index.json"


def _read_index() -> list[str]:
    path = _index_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(sha) for sha in data] if isinstance(data, list) else []


def _write_index(entries: list[str]) -> None:
    from .config import write_secret
    write_secret(_index_path(), json.dumps(entries, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------
def _rate(value: Any) -> float | None:
    """`30000/1001` (ffprobe's frame rate) as a number, or None."""
    try:
        text = str(value)
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            return round(float(numerator) / float(denominator), 3) if float(denominator) else None
        return round(float(text), 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_video(path: Path) -> dict[str, Any] | None:
    """`{duration_seconds, width, height, fps, codec, resolution}` via ffprobe.

    `None` when there is no ffprobe to read the file, or it could not. Individual
    fields stay `None` when the file does not state them — never a guessed value.
    """
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "video"]
    stream = streams[0] if streams else {}
    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    width = stream.get("width") if isinstance(stream.get("width"), int) else None
    height = stream.get("height") if isinstance(stream.get("height"), int) else None
    return {
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "width": width,
        "height": height,
        "fps": _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate")),
        "codec": stream.get("codec_name") if isinstance(stream.get("codec_name"), str) else None,
        "resolution": f"{width}x{height}" if width and height else None,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_clip(sha256: str) -> dict[str, Any] | None:
    meta_path = get_root() / f"{sha256}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    meta["sha256"] = sha256
    return meta


def list_clips() -> list[dict[str, Any]]:
    """Every clip in the library, newest first."""
    with _lock:
        shas = _read_index()
    return [clip for sha in shas if (clip := get_clip(sha)) is not None]


def clip_path(sha256: str) -> Path | None:
    root = get_root()
    for ext in VIDEO_FORMATS:
        path = root / f"{sha256}.{ext}"
        if path.is_file():
            return path
    return None


def clip_media_type(path: Path) -> str:
    return VIDEO_MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


def describe(clip: dict[str, Any]) -> dict[str, Any]:
    """One clip as a reader sees it: unknown fields stay null, and are named."""
    out = dict(clip)
    out["not_recorded"] = [name for name in RECORDED_FIELDS if clip.get(name) is None]
    return out


def search_library(query: str, tag: str | None = None) -> list[dict[str, Any]]:
    """Substring search over titles, descriptions, tags and authors; or by one tag.

    **ponytail:** no inverted index. Upgrade to trigram/FTS if the library passes
    ~1k clips and Story needs ranked results.
    """
    wanted = (tag or "").strip().casefold()
    text = query.strip().casefold()
    out: list[dict[str, Any]] = []
    for clip in list_clips():
        tags = [str(item).casefold() for item in clip.get("tags") or []]
        if wanted and wanted not in tags:
            continue
        if text:
            haystack = " ".join(filter(None, [
                str(clip.get("title") or ""), str(clip.get("description") or ""),
                str(clip.get("author") or ""), " ".join(tags),
            ])).casefold()
            if text not in haystack:
                continue
        out.append(clip)
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _licence(value: Any, *, explicit: bool) -> str | None:
    """`cc0`/`by`/a provider's free-use word, or — only for an explicit import — the owner's word.

    A connector's clip must pass the commercial-safe gate; a clip a model generated
    or the owner supplied states its own provenance and may carry its own licence.
    """
    normalised = normalise_licence(value)
    if normalised is not None:
        return normalised
    word = str(value or "").strip()
    if not word:
        return None
    if word.casefold() in COMMERCIAL_SAFE_LICENCES:
        return word.casefold()
    return word if explicit else None


def _legal_format(value: Any) -> str | None:
    ext = str(value or "").lstrip(".").strip().lower()
    return ext if ext in VIDEO_FORMATS else None


def _decode_base64(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("No file bytes were sent. Send them as base64 in 'format'.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The file bytes are not valid base64.") from exc


def sniff_format(data: bytes) -> str | None:
    """The container an explicit import carries, read from its first bytes."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mov" if data[8:12] == b"qt  " else "mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "avi"
    return None


def _stored_fields(payload: dict[str, Any], probe: dict[str, Any] | None) -> dict[str, Any]:
    """The sidecar's fields, every one present and `None` where nothing was recorded."""
    tags = payload.get("tags") or []
    return {
        "title": str(payload.get("title") or "").strip() or None,
        "description": (str(payload["description"]).strip()
                        if isinstance(payload.get("description"), str) and payload["description"].strip()
                        else None),
        "duration_seconds": (probe or {}).get("duration_seconds"),
        "width": (probe or {}).get("width"),
        "height": (probe or {}).get("height"),
        "resolution": (probe or {}).get("resolution"),
        "fps": (probe or {}).get("fps"),
        "codec": (probe or {}).get("codec"),
        "tags": [str(tag) for tag in tags if str(tag).strip()],
        "licence": payload.get("licence"),
        "licence_note": payload.get("licence_note"),
        "provenance": payload.get("provenance"),
        "ai_generated": (payload["ai_generated"]
                         if isinstance(payload.get("ai_generated"), bool) else None),
        "source": payload.get("source"),
        "source_id": payload.get("source_id"),
        "source_url": payload.get("source_url"),
        "author": payload.get("author"),
        "attribution": payload.get("attribution"),
    }


def add_clip(data: bytes, payload: dict[str, Any], *, explicit: bool) -> dict[str, Any]:
    """Store one clip: hash, probe, sidecar, index. An existing sha is returned, not re-added.

    `explicit` is the licence door: `True` for bytes the owner supplied or a model
    generated (they state their own licence), `False` for a connector's clip (only a
    commercial-safe licence may land).
    """
    if not data:
        raise ValueError("No file bytes were sent.")
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("'title' is required.")
    if len(title) > 200:
        raise ValueError("'title' is limited to 200 characters.")
    licence = _licence(payload.get("licence"), explicit=explicit)
    if licence is None:
        raise ValueError(
            "The clip has no commercial-safe licence, so it was not added."
            if not explicit else "The clip's licence is missing, so it was not added."
        )
    filetype = _legal_format(payload.get("format"))
    if filetype is None:
        raise ValueError(f"'format' must be one of {', '.join(VIDEO_FORMATS)}.")
    payload = {**payload, "title": title, "licence": licence}

    sha = hash_bytes(data)
    existing = get_clip(sha)
    if existing is not None:
        return existing

    root = get_root()
    media_path = root / f"{sha}.{filetype}"
    media_path.write_bytes(data)
    probe = probe_video(media_path)
    if probe is None and _find_ffprobe():
        # ffprobe is here and could not read it: this is not a video we can store.
        media_path.unlink(missing_ok=True)
        raise ValueError("That file could not be read as a video, so it was not added.")

    meta = {**_stored_fields(payload, probe), "filesize_bytes": len(data), "added_at": time.time()}
    (root / f"{sha}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    with _lock:
        index = _read_index()
        if sha not in index:
            index.insert(0, sha)
            _write_index(index)
    meta["sha256"] = sha
    log.info("footage library: added %s (%s, %s)", sha[:12], title, licence)
    return meta


def import_bytes(payload: dict[str, Any]) -> dict[str, Any]:
    """An explicit owner import.

    `format` carries the file as base64 (the brief's shape); `data` is accepted as
    the same field under a plainer name. The container is read from the file's own
    first bytes when `format` is not a known extension, and a file we cannot
    recognise is refused rather than stored with a guessed extension.
    """
    encoded = payload.get("data")
    if isinstance(encoded, str) and encoded.strip():
        data = _decode_base64(encoded)
        filetype = _legal_format(payload.get("format")) or sniff_format(data)
    else:
        data = _decode_base64(payload.get("format"))
        filetype = sniff_format(data)
    if filetype is None:
        raise ValueError(
            f"The file's format could not be recognised; it must be one of "
            f"{', '.join(VIDEO_FORMATS)}.")
    return add_clip(data, {**payload, "format": filetype}, explicit=True)


def download_into_library(source_id: str, source_item_id: str,
                          credential_store: Any) -> dict[str, Any]:
    """Fetch one clip from a stock connector and store it.

    The licence is re-read from the connector's own detail first: a listing can be
    minutes old, and an upstream takedown must not land as a library entry.
    """
    from .stock_sources import StockSourceError, source_for

    source = source_for(source_id, credential_store)
    if source is None:
        raise ValueError(f"Unknown stock source: {source_id}")
    if not source.is_available():
        raise StockSourceError(f"{source.label} is switched off; enable it first.",
                               status=409, code="SOURCE_OFF")
    item = source.detail_for(source_item_id)
    if item is None:
        raise StockSourceError("The clip is no longer available upstream.", status=502)
    licence = _licence(item.licence, explicit=False)
    if licence is None:
        raise StockSourceError("The clip's licence is not commercial-safe.",
                               status=409, code="LICENCE_REFUSED")
    data = source.fetch(item)
    payload = {
        "title": item.title or source_item_id,
        "description": item.description,
        "licence": licence,
        "licence_note": item.licence_note,
        "provenance": f"{source.label} stock",
        "ai_generated": False,
        "source": source.id,
        "source_id": item.source_id,
        "source_url": item.source_url,
        "author": item.author,
        "attribution": item.attribution,
        "tags": list(item.tags),
        "format": _legal_format(item.filetype) or _legal_format(
            source.default_format) or "mp4",
    }
    return add_clip(data, payload, explicit=False)


def add_generated(data: bytes, *, model_id: str, title: str,
                  description: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    """Store a clip a model generated on the fleet, with its provenance recorded."""
    return add_clip(data, {
        "title": title,
        "description": description,
        "licence": "generated",
        "licence_note": f"Generated by {model_id} on the fleet.",
        "provenance": f"generated by {model_id} on the fleet",
        "ai_generated": True,
        "source": "ai-stock",
        "source_id": None,
        "source_url": "",
        "author": None,
        "attribution": None,
        "tags": list(tags or []),
        "format": "mp4",
    }, explicit=True)


# ---------------------------------------------------------------------------
# Quota and retirement
# ---------------------------------------------------------------------------
def quota_status() -> dict[str, Any]:
    """The library's footprint, **measured from the disk** — never estimated."""
    root = get_root()
    clips = list_clips()
    total = 0
    for clip in clips:
        path = clip_path(clip["sha256"])
        if path is not None:
            try:
                total += path.stat().st_size
            except OSError:
                continue
    usage = shutil.disk_usage(root)
    return {"files": len(clips), "bytes": total, "root": str(root), "free_bytes": usage.free}


def retire(sha256: str) -> dict[str, Any]:
    """Move a clip's bytes and sidecar to the trash. Restorable, never `rm`."""
    path = clip_path(sha256)
    meta_path = get_root() / f"{sha256}.json"
    if path is None and not meta_path.exists():
        raise ValueError(f"No such clip in the library: {sha256}")
    trash = config.DATA_DIR / "_trash" / "footage-library"
    trash.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for source in (path, meta_path):
        if source is None or not source.exists():
            continue
        target = trash / source.name
        if target.exists():
            target = trash / f"{source.stem}-{time.time_ns()}{source.suffix}"
        shutil.move(str(source), target)
        moved.append(str(target))
    with _lock:
        index = _read_index()
        if sha256 in index:
            index.remove(sha256)
            _write_index(index)
    log.info("footage library: retired %s to %s", sha256[:12], trash)
    return {
        "sha256": sha256,
        "retired": moved,
        "restorable": True,
        "restore": f"move the files back into {get_root()} and add the sha to index.json",
    }
