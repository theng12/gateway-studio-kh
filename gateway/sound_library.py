"""Shared sound library — download, hash, probe, persist, and list clips.

One library for every channel. Each entry records the bytes on disk plus
licence metadata so Story can resolve cues without re-downloading and so
the credits step knows exactly which CC BY clips ended up in a render.

Storage layout under ``DATA_DIR/sound-library/``::

    <sha256>.<ext>          # the audio bytes
    <sha256>.json           # metadata sidecar (licence, author, title, …)
    index.json              # ordered list of shas, newest first

**ponytail:** flat file store with a JSON index rather than a SQLite table.
The library will hold hundreds of clips, not millions; a single JSON array
is simpler to inspect, back up, and prune. Upgrade path: move to the
gateway's SQLite store if the index grows past ~10k entries or concurrent
writes start colliding.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import config
from .sound_sources import (SoundSearchResult, SoundSourceError, hash_bytes, name_list,
                            probe_audio, source_for)
from .providers.credentials import CredentialStore

log = logging.getLogger("gateway.sound_library")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _library_dir() -> Path:
    d = config.DATA_DIR / "sound-library"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _index_path() -> Path:
    return _library_dir() / "index.json"

# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

_lock = threading.Lock()

def _read_index() -> list[str]:
    p = _index_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(s) for s in data]
    except (json.JSONDecodeError, OSError):
        pass
    return []

def _write_index(entries: list[str]) -> None:
    from .config import write_secret  # reuse atomic writer for consistency
    write_secret(_index_path(), json.dumps(entries, indent=2) + "\n")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_clips() -> list[dict[str, Any]]:
    """Return every clip in the library, newest first."""
    with _lock:
        shas = _read_index()
    out: list[dict[str, Any]] = []
    lib = _library_dir()
    for sha in shas:
        meta_path = lib / f"{sha}.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta["sha256"] = sha
        out.append(meta)
    return out


def get_clip(sha256: str) -> dict[str, Any] | None:
    """Return one clip's metadata, or None."""
    lib = _library_dir()
    meta_path = lib / f"{sha256}.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    meta["sha256"] = sha256
    return meta


def clip_audio_path(sha256: str) -> Path | None:
    """Return the path to the audio bytes, or None if missing."""
    lib = _library_dir()
    for ext in ("mp3", "wav", "ogg", "flac", "m4a"):
        p = lib / f"{sha256}.{ext}"
        if p.is_file():
            return p
    # Fallback: scan for any file starting with the hash
    for p in lib.iterdir():
        if p.name.startswith(sha256) and p.is_file() and p.suffix != ".json":
            return p
    return None


#: Media type per extension the library stores. Values are stated, not guessed:
#: a clip's bytes are served to another Mac, so the type travels with them.
_CLIP_MEDIA_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
}


def clip_media_type(path: Path) -> str:
    """The media type for a stored clip file, from its stored extension."""
    return _CLIP_MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


def search_library(query: str, kind: str | None = None) -> list[dict[str, Any]]:
    """Full-text-ish search over the local library titles and tags.

    **ponytail:** substring match on title + author. No inverted index.
    Upgrade path: add trigram matching or an FTS table if the library
    passes ~1k clips and Story needs ranked results.
    """
    q = query.strip().casefold()
    if not q:
        return list_clips()
    out: list[dict[str, Any]] = []
    for clip in list_clips():
        haystack = " ".join(filter(None, [
            str(clip.get("title", "")),
            str(clip.get("author", "")),
            str(clip.get("description", "")),
            " ".join(str(t) for t in (clip.get("tags") or [])),
        ])).casefold()
        if q in haystack:
            if kind and clip.get("kind") and clip["kind"] != kind:
                continue
            out.append(clip)
    return out


def download_into_library(
    source_id: str,
    source_item_id: str,
    credential_store: CredentialStore,
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """Fetch a clip from an upstream source, validate it, and store it.

    Returns the metadata dict on success. Raises ``SoundSourceError`` or
    ``ValueError`` on refusal.
    """
    src = source_for(source_id, credential_store)
    if src is None:
        raise ValueError(f"Unknown sound source: {source_id}")

    # Fetch the detail FIRST so we carry the real licence — no placeholder
    # may ever be stored. The adapter's download_bytes also re-checks, but
    # we need the metadata here regardless.
    detail = src.detail_for(source_item_id) if hasattr(src, "detail_for") else None
    if detail is None:
        raise SoundSourceError("The clip is no longer available upstream.", status=502)

    from .sound_sources import normalise_licence
    licence = normalise_licence(detail.get("license"))
    if licence is None:
        raise SoundSourceError(
            "The clip's licence is not commercial-safe.",
            status=409, code="LICENCE_REFUSED",
        )

    # Build the stub with the REAL licence from the detail response.
    stub = SoundSearchResult(
        source=source_id,
        source_id=source_item_id,
        title=str(detail.get("title") or source_item_id),
        author=(detail.get("creator") if isinstance(detail.get("creator"), str) else None),
        source_url=str(detail.get("foreign_landing_url") or ""),
        licence=licence,
        licence_version=(detail.get("license_version")
                         if isinstance(detail.get("license_version"), str) else None),
        attribution=(detail.get("attribution")
                     if isinstance(detail.get("attribution"), str) else None),
        duration_ms=(int(detail["duration"])
                     if isinstance(detail.get("duration"), (int, float)) else None),
        filesize=(detail.get("filesize")
                  if isinstance(detail.get("filesize"), int) else None),
        filetype=(detail.get("filetype")
                  if isinstance(detail.get("filetype"), str) else None),
        download_url=str(detail.get("url") or ""),
        tags=name_list(detail.get("tags")),
        category=(detail.get("category")
                  if isinstance(detail.get("category"), str) else None),
        genres=name_list(detail.get("genres")),
    )

    raw_bytes = src.download_bytes(stub, timeout=45.0)
    sha = hash_bytes(raw_bytes)

    # Already in the library?
    existing = get_clip(sha)
    if existing is not None:
        return existing

    filetype = str(detail.get("filetype") or "mp3").lstrip(".")
    if filetype not in ("mp3", "wav", "ogg", "flac", "m4a"):
        filetype = "mp3"

    lib = _library_dir()
    audio_path = lib / f"{sha}.{filetype}"
    meta_path = lib / f"{sha}.json"

    # Write bytes
    audio_path.write_bytes(raw_bytes)

    # Probe
    probe = probe_audio(audio_path)

    duration_s: float | None = None
    if probe and probe.get("duration_seconds") is not None:
        duration_s = round(probe["duration_seconds"], 3)
    elif detail.get("duration"):
        try:
            duration_s = round(int(detail["duration"]) / 1000.0, 3)
        except (TypeError, ValueError):
            pass

    meta: dict[str, Any] = {
        "title": str(detail.get("title") or source_item_id),
        "author": detail.get("creator"),
        "licence": licence,
        "licence_version": detail.get("license_version"),
        "source": source_id,
        "source_id": source_item_id,
        "source_url": str(detail.get("foreign_landing_url") or ""),
        "attribution": detail.get("attribution"),
        # The upstream's own description of the clip. Kept so `search_library` can match a
        # tag and Story can show what the clip is. Old sidecars have none; readers treat
        # the absence as "no tags", never as a reason to drop the clip.
        "tags": list(name_list(detail.get("tags"))),
        "category": (detail.get("category")
                     if isinstance(detail.get("category"), str) else None),
        "genres": list(name_list(detail.get("genres"))),
        "kind": kind,
        "duration_seconds": duration_s,
        "format": (probe or {}).get("format"),
        "sample_rate": (probe or {}).get("sample_rate"),
        "channels": (probe or {}).get("channels"),
        "bit_rate": (probe or {}).get("bit_rate"),
        "filesize_bytes": len(raw_bytes),
        "added_at": time.time(),
    }

    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # Prepend to index
    with _lock:
        idx = _read_index()
        if sha not in idx:
            idx.insert(0, sha)
            _write_index(idx)

    meta["sha256"] = sha
    log.info("sound library: added %s (%s, %s)", sha[:12], meta["title"], licence)
    return meta


#: Licence value of a clip a model made for us. Deliberately its own word: it is never
#: CC0 and never passes through `normalise_licence`, whose job is downloaded clips.
GENERATED_LICENCE = "generated"
_IMPORT_FORMATS = ("wav", "mp3", "ogg", "flac", "m4a")


def _generated_text(payload: dict[str, Any], field: str, limit: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{field}' is required.")
    if len(value) > limit:
        raise ValueError(f"'{field}' is limited to {limit} characters.")
    return value.strip()


def import_generated_clip(audio: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Store one clip a model generated, in the same `<sha256>.<ext>` + sidecar shape as a download.

    The licence note and model id are required, because a credit line and a later licence
    question both read them. Raises ``ValueError`` on refusal; nothing is left on disk.
    """
    title = _generated_text(payload, "title", 200)
    description = _generated_text(payload, "description", 500)
    model_id = _generated_text(payload, "model_id", 200)
    licence_note = _generated_text(payload, "licence_note", 500)
    filetype = str(payload.get("format") or "").lstrip(".").lower()
    if filetype not in _IMPORT_FORMATS:
        raise ValueError(f"'format' must be one of {', '.join(_IMPORT_FORMATS)}.")
    tags = payload.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) and tag.strip() for tag in tags):
        raise ValueError("'tags' must be a list of words.")
    if not audio:
        raise ValueError("No audio was sent.")

    sha = hash_bytes(audio)
    existing = get_clip(sha)
    if existing is not None:
        return existing

    lib = _library_dir()
    audio_path = lib / f"{sha}.{filetype}"
    audio_path.write_bytes(audio)
    probe = probe_audio(audio_path)
    duration_s = (probe or {}).get("duration_seconds")
    if not isinstance(duration_s, (int, float)) or duration_s <= 0:
        audio_path.unlink(missing_ok=True)
        raise ValueError("That file could not be read as audio, so it was not added.")

    meta: dict[str, Any] = {
        "title": title,
        "author": None,
        "licence": GENERATED_LICENCE,
        "licence_version": None,
        "licence_note": licence_note,
        "model_id": model_id,
        "source": "generated",
        "source_id": sha,
        "source_url": "",
        "attribution": None,
        "description": description,
        "tags": [tag.strip() for tag in tags],
        "category": (payload.get("category") if isinstance(payload.get("category"), str) else None),
        "genres": [],
        "kind": (payload.get("kind") if payload.get("kind") in ("bed", "spot") else None),
        "duration_seconds": round(float(duration_s), 3),
        "format": probe.get("format"),
        "sample_rate": probe.get("sample_rate"),
        "channels": probe.get("channels"),
        "bit_rate": probe.get("bit_rate"),
        "filesize_bytes": len(audio),
        "added_at": time.time(),
    }
    (lib / f"{sha}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    with _lock:
        idx = _read_index()
        if sha not in idx:
            idx.insert(0, sha)
            _write_index(idx)
    meta["sha256"] = sha
    log.info("sound library: imported generated %s (%s)", sha[:12], title)
    return meta


def quota_status(credential_store: CredentialStore) -> dict[str, Any]:
    """Aggregate quota state across all sources."""
    from .sound_sources import all_sources
    sources = all_sources(credential_store)
    result: dict[str, Any] = {"sources": {}, "exhausted": False}
    for src in sources:
        info = src.status()
        result["sources"][src.id] = info
        if info.get("quota", {}).get("exhausted"):
            result["exhausted"] = True
    return result