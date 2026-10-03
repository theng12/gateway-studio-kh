"""What a voice's reference clip really is, and a WAV the app's player can open.

Some references in the library are QuickTime/ISO-media (`.mov`) containers holding MP3
audio under a `.mp3` name. The Hub's ffmpeg-based decoder reads them, but the menu bar
player (`NSSound`) goes by the bytes and refuses them, so the Voices page could not
preview those voices while the WAV ones played. A name is not a format: this module goes
by the first bytes.

* `playable()` -- preview: the clip itself when it is a plain WAV/MP3 matching its name,
  otherwise a WAV transcoded beside it in a hidden cache (the reference is never replaced).
* `normalise_to_wav()` -- import and migration: write a proper `reference.wav`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: The hidden cache a transcoded preview lives in, beside the reference it came from.
PREVIEW_CACHE = ".preview.wav"

_CONVERT_TIMEOUT_S = 120


def sniff(path: Path) -> str:
    """`wav`, `mp3` or `other`, from the file's first bytes (never its name)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(12)
    except OSError:
        return "other"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return "other"


def plain_media_type(path: Path) -> str | None:
    """The media type to serve `path` as, when its bytes and its name agree; else None."""
    kind, suffix = sniff(path), path.suffix.lower()
    if kind == "wav" and suffix == ".wav":
        return "audio/wav"
    if kind == "mp3" and suffix == ".mp3":
        return "audio/mpeg"
    return None


def _converters(src: Path, dst: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    # afconvert ships with macOS, so it exists even when the gateway's PATH has no Homebrew.
    afconvert = shutil.which("afconvert") or ("/usr/bin/afconvert" if Path("/usr/bin/afconvert").exists() else "")
    if afconvert:
        commands.append([afconvert, "-f", "WAVE", "-d", "LEI16", str(src), str(dst)])
    ffmpeg = shutil.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if Path(p).exists()), ""
    )
    if ffmpeg:
        commands.append([ffmpeg, "-v", "error", "-y", "-i", str(src), "-vn",
                         "-c:a", "pcm_s16le", "-f", "wav", str(dst)])
    return commands


def to_wav(src: Path, dst: Path) -> None:
    """Decode `src` to a 16-bit PCM WAV at `dst`, atomically. Raises `RuntimeError` on failure."""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    errors: list[str] = []
    try:
        for command in _converters(src, tmp):
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=_CONVERT_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{Path(command[0]).name}: {exc}"[:200])
                continue
            if sniff(tmp) == "wav" and tmp.stat().st_size > 44:
                os.replace(tmp, dst)
                return
            errors.append(f"{Path(command[0]).name}: produced no WAV")
        raise RuntimeError("could not convert to WAV (" + ("; ".join(errors) or "no converter found") + ")")
    finally:
        tmp.unlink(missing_ok=True)


def playable(path: Path) -> tuple[Path, str]:
    """A file and media type the app's player can open for this reference clip.

    A plain WAV/MP3 is served as it is. Anything else (a `.mov` named `.mp3`, M4A, FLAC...)
    is transcoded once to a hidden `.preview.wav` beside it and re-made when the reference
    is newer. Raises `RuntimeError` when it cannot be converted.
    """
    media = plain_media_type(path)
    if media:
        return path, media
    cache = path.with_name(PREVIEW_CACHE)
    try:
        fresh = cache.stat().st_mtime >= path.stat().st_mtime and sniff(cache) == "wav"
    except OSError:
        fresh = False
    if not fresh:
        to_wav(path, cache)
    return cache, "audio/wav"
