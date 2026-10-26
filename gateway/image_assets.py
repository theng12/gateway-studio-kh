"""Reference images for the image lane, held between the upload and the submit.

"Reference to Image" is two calls, the same shape as image-to-video: Story Studio uploads each
character/reference image to ``POST /v1/images/source-images`` and then submits a generation whose
``reference_asset_ids`` are the ids it was given. The gateway is the only address Story knows, so
the bytes have to be held here between the two calls.

**Bounded and temporary, like the video source images it is modelled on.** An image is held for
``TTL_S`` and then dropped. **The job keeps its own copy**: ``create_image`` stages every reference
beside the transcription uploads at submit (``_reference_paths``), so a job that queues past the
window, or outlives a restart, still has its images; the adapter base64s those copies into the job
body. The cap is the same 25 MB the transcription door enforces, and it is a refusal rather than a
truncation.

**The id is the sha256 of the bytes, not a uuid.** Two consequences, both wanted: a re-upload of the
same file returns the same id, so Story's retry after ``REFERENCE_ASSET_EXPIRED`` needs no new
bookkeeping; and a replay of an ``Idempotency-Key`` with a different reference set is detectable by
comparing digests, which is the difference between "the same request again" and "a different render
under the same key".

Held in memory on purpose: a character PNG is a few megabytes and the window is seconds. This is
not an artifact store — generation artifacts live in ``data/artifacts`` and are served by the routes
that own them.
"""
from __future__ import annotations

import hashlib
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any


#: How long an uploaded reference image is held. Story submits within seconds of the upload; a
#: generous quarter hour covers a retry after a slow admission without keeping bytes for ever.
TTL_S = 15 * 60
#: One upload ceiling, the same number the transcription and video routes refuse past.
MAX_SOURCE_IMAGE_BYTES = 25 * 1024 * 1024
#: Media types a reference image may be. The Hub's broker reads the bytes into its multipart door;
#: refusing an unknown type here names the accepted set instead of failing later.
ACCEPTED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")

_LOCK = threading.Lock()
#: digest -> (content, filename, media_type, stored_at)
_IMAGES: dict[str, tuple[bytes, str, str, float]] = {}


def _prune(now: float) -> None:
    for key in [key for key, value in _IMAGES.items() if now - value[3] > TTL_S]:
        _IMAGES.pop(key, None)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat().replace("+00:00", "Z")


def store(content: bytes, filename: str, media_type: str) -> dict[str, Any]:
    """Hold one uploaded reference image and describe it the way the upload route answers.

    The answer carries what Story's ``GenStudioImageSourceAsset`` reads — and deliberately not the
    video route's ``bytes``/``expires_in_seconds``, which its image parser does not know.
    """
    content = bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    media_type = media_type or "application/octet-stream"
    now = time.time()
    with _LOCK:
        _prune(now)
        _IMAGES[digest] = (content, filename or "reference.png", media_type, now)
    return {"id": digest, "media_type": media_type, "byte_size": len(content),
            "stored_at": now, "expires_at": _iso(now + TTL_S)}


def get(asset_id: str) -> dict[str, Any] | None:
    """The held image, or None once it expired or was never uploaded. `digest` is the id."""
    with _LOCK:
        _prune(time.time())
        value = _IMAGES.get(str(asset_id or ""))
    if value is None:
        return None
    content, filename, media_type, stored_at = value
    return {"id": str(asset_id), "digest": str(asset_id), "content": content,
            "filename": filename, "media_type": media_type, "stored_at": stored_at,
            "bytes": len(content), "expires_at": _iso(stored_at + TTL_S)}


# ---------------------------------------------------------------------------
# Header reads. **No image library**, and the reason is the same one `worker.artifact_problem`
# records: the gateway runtime is a short dependency list (requirements.lock) and these checks need
# two integers and "are these bytes this kind of file at all", not a decode.
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def dimensions(content: bytes, media_type: str) -> tuple[int, int] | None:
    """`(width, height)` from the declared image's own header, or `None` when it is not one.

    `None` is the refusal: a body that is not a readable image of the type it claims cannot be
    handed to a worker, so the upload door says so instead of holding bytes that fail mid-render.
    """
    kind = (media_type or "").split(";")[0].strip().lower()
    if kind == "image/png":
        return _png_size(content)
    if kind == "image/jpeg":
        return _jpeg_size(content)
    if kind == "image/webp":
        return _webp_size(content)
    return None


def _png_size(content: bytes) -> tuple[int, int] | None:
    """A PNG opens with IHDR, whose first two fields are the pixel size."""
    if not content.startswith(_PNG_SIGNATURE) or len(content) < 24 or content[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", content[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _jpeg_size(content: bytes) -> tuple[int, int] | None:
    """Walk the JPEG's segments to its start-of-frame, whose payload carries height then width."""
    if content[:3] != b"\xff\xd8\xff":
        return None
    position, end = 2, len(content)
    while position + 4 <= end:
        if content[position] != 0xFF:
            return None
        marker = content[position + 1]
        if marker == 0xFF:            # fill byte before a marker
            position += 1
            continue
        if marker == 0x01 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:  # markers with no length
            position += 2
            continue
        if marker == 0xD9:            # end of image before any frame header
            return None
        length = struct.unpack(">H", content[position + 2:position + 4])[0]
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if position + 9 > end:
                return None
            height, width = struct.unpack(">HH", content[position + 5:position + 9])
            return (width, height) if width > 0 and height > 0 else None
        position += 2 + length
    return None


def _webp_size(content: bytes) -> tuple[int, int] | None:
    """A WebP is a RIFF container; all three chunk kinds state the canvas."""
    if len(content) < 30 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        return None
    chunk, data = content[12:16], content[20:]
    if chunk == b"VP8 ":              # lossy: a start code, then 14 bits of width and of height
        if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack("<HH", data[6:10])
        width, height = width & 0x3FFF, height & 0x3FFF
        return (width, height) if width > 0 and height > 0 else None
    if chunk == b"VP8L":              # lossless: 14 bits each, stored as value - 1
        if len(data) < 5 or data[0] != 0x2F:
            return None
        bits = int.from_bytes(data[1:5], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":              # extended: the canvas, again stored as value - 1
        if len(data) < 10:
            return None
        width = int.from_bytes(data[4:7], "little") + 1
        height = int.from_bytes(data[7:10], "little") + 1
        return (width, height) if width > 0 and height > 0 else None
    return None
