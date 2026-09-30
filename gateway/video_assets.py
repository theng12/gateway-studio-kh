"""Source images for video generation, held between the upload and the submit.

Story Studio's image-to-video flow is two calls: it uploads the exact scene image to
``POST /v1/videos/source-images`` and then submits a generation naming the returned id as
``reference_asset_id``. The gateway is the only address Story knows, so the bytes have to be
held here between the two calls.

**Bounded and temporary, like the transcription uploads it is modelled on.** An image is held
for ``TTL_S`` and then dropped. **The job keeps its own copy**: ``create_video`` stages the bytes
beside the transcription uploads at submit (``_reference_path``), so a job that queues past the
window, or outlives a restart, still has its image; the adapter base64s that copy into the job body. The cap is the same
25 MB the transcription door enforces, and it is a refusal rather than a truncation.

Held in memory on purpose: a scene PNG is a few megabytes and the window is seconds. This is
not an artifact store — generation artifacts live in ``data/artifacts`` and are served by the
routes that own them.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any


#: How long an uploaded source image is held. Story submits within seconds of the upload; a
#: generous quarter hour covers a retry after a slow admission without keeping bytes for ever.
TTL_S = 15 * 60
#: One upload ceiling, the same number the transcription route refuses past. No video provider
#: here publishes a source-image limit, so this is a gateway guard, not a model's figure.
MAX_SOURCE_IMAGE_BYTES = 25 * 1024 * 1024
#: Media types a scene image may be. The Hub's broker reads the first bytes only for its
#: multipart door; refusing an unknown type here names the accepted set instead of failing later.
ACCEPTED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")

_LOCK = threading.Lock()
_IMAGES: dict[str, tuple[bytes, str, str, float]] = {}


def _prune(now: float) -> None:
    for key in [key for key, value in _IMAGES.items() if now - value[3] > TTL_S]:
        _IMAGES.pop(key, None)


def store(content: bytes, filename: str, media_type: str) -> str:
    """Hold one uploaded image and return the id a submit may reference."""
    asset_id = str(uuid.uuid4())
    with _LOCK:
        _prune(time.time())
        _IMAGES[asset_id] = (bytes(content), filename or "scene.png",
                             media_type or "application/octet-stream", time.time())
    return asset_id


def get(asset_id: str) -> dict[str, Any] | None:
    """The held image, or None once it expired or was never uploaded."""
    with _LOCK:
        _prune(time.time())
        value = _IMAGES.get(str(asset_id or ""))
    if value is None:
        return None
    content, filename, media_type, stored_at = value
    return {"content": content, "filename": filename, "media_type": media_type,
            "stored_at": stored_at, "bytes": len(content)}
