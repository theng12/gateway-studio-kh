"""MiniMax adapter — image, video, music and speech.

MiniMax is four different APIs wearing one vendor name, and they do not share a
shape:

    image   POST /v1/image_generation                    -> base64 in the response
    music   POST /v1/music_generation                    -> a temporary audio URL
    speech  POST /v1/t2a_v2                              -> hex-encoded audio
    video   POST /v2/video_generation, then poll
            GET  /v2/query/video_generation/{task_id}    -> a temporary video URL

So this adapter is a dispatcher over four handlers rather than one flow, which
is the honest shape of the provider.

Note the two encoding quirks: image comes back as base64 *inside* the JSON, and
speech as **hex**, not base64. Decoding the audio as base64 produces bytes that
look plausible and play as noise.
"""

from __future__ import annotations

import base64
import binascii
import time
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

SUBMIT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

VIDEO_POLL_INTERVAL_S = 4.0
VIDEO_DEADLINE_S = 900.0

_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"}


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
    }


def _base(request: BackendRequest) -> str:
    return (request.values.get("base_url") or request.base_url).rstrip("/")


def _params(request: BackendRequest) -> dict[str, str]:
    """MiniMax wants a GroupId on some endpoints when the key is not scoped."""
    group = str(request.values.get("group_id") or "").strip()
    return {"GroupId": group} if group else {}


def _unwrap(response: httpx.Response, provider: str = "minimax") -> dict[str, Any]:
    """Raise on transport/base errors, returning `base_resp`-checked JSON."""
    status = response.status_code
    if status >= 400:
        code = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED"
        if status == 429:
            code = "PROVIDER_QUOTA_EXHAUSTED"
        raise BackendError(
            f"{provider}: HTTP {status}: {response.text[:250]}",
            code=code,
            status=status,
            retryable=status in {408, 425, 429} or status >= 500,
        )

    payload = response.json() or {}
    base = payload.get("base_resp") if isinstance(payload, dict) else None
    if isinstance(base, dict):
        status_code = base.get("status_code")
        if status_code not in (0, None):
            message = str(base.get("status_msg") or status_code)
            # 1002/1004/1008 are auth/balance problems, not transient faults.
            code = "PROVIDER_AUTH_FAILED" if status_code in {1002, 1004} else "PROVIDER_FAILED"
            raise BackendError(f"{provider}: {message}", code=code)
    return payload


def _download(url: str, *, accept: str, label: str) -> httpx.Response:
    try:
        with httpx.Client(timeout=ARTIFACT_TIMEOUT) as client:
            artifact = client.get(url, headers={"Accept": accept})
    except httpx.HTTPError as exc:
        raise BackendError(
            f"{label} download failed: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
        ) from exc
    if artifact.status_code >= 400:
        raise BackendError(f"{label} HTTP {artifact.status_code}", code="PROVIDER_FAILED")
    return artifact


def _media_type(response: httpx.Response, fallback: str) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip() or fallback


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
def _image(request: BackendRequest) -> BackendResult:
    ratio = str(request.params.get("aspect_ratio") or "1:1")
    if ratio not in _RATIOS:
        ratio = "1:1"

    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
        "aspect_ratio": ratio,
        # Base64 keeps the bytes in-response; MiniMax's own URLs expire in 24h.
        "response_format": "base64",
        "n": 1,
        "prompt_optimizer": False,
    }
    if request.params.get("seed") is not None:
        body["seed"] = request.params["seed"]

    try:
        response = httpx.post(
            f"{_base(request)}/v1/image_generation",
            headers=_headers(request),
            params=_params(request),
            json=body,
            timeout=SUBMIT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    payload = _unwrap(response)
    rows = (payload.get("data") or {}).get("image_base64")
    if not isinstance(rows, list) or not rows:
        raise BackendError("minimax returned no image", code="IMAGE_FAILED")
    try:
        content = base64.b64decode(rows[0], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BackendError("minimax image is not valid base64", code="IMAGE_FAILED") from exc

    return BackendResult(
        content=content, media_type="image/png", payload={"model": request.model_id}
    )


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------
def _music(request: BackendRequest) -> BackendResult:
    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
    }
    for key in ("lyrics", "title", "instrumental"):
        if request.params.get(key) is not None:
            body[key] = request.params[key]

    try:
        response = httpx.post(
            f"{_base(request)}/v1/music_generation",
            headers=_headers(request),
            params=_params(request),
            json=body,
            timeout=SUBMIT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    payload = _unwrap(response)
    data = payload.get("data") or {}
    # status 2 means finished; anything else has no audio yet.
    if data.get("status") not in (2, None):
        raise BackendError(
            f"minimax music did not complete (status {data.get('status')})",
            code="PROVIDER_FAILED",
        )
    url = str(data.get("audio") or "")
    if not url:
        raise BackendError("minimax returned no audio url", code="PROVIDER_FAILED")

    artifact = _download(url, accept="audio/*", label="minimax music")
    return BackendResult(
        content=artifact.content,
        media_type=_media_type(artifact, "audio/mpeg"),
        payload={"model": request.model_id, "provider_url": url},
    )


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------
def _speech(request: BackendRequest) -> BackendResult:
    # **A missing voice used to become `English_expressive_narrator`** — a
    # specific named English speaker, chosen silently, so the narration came back
    # in a voice the caller never asked for and nothing said so. Refusing names the
    # missing field instead. (fal's `Rachel` and ElevenLabs' account-first-voice are
    # documented deliberate exceptions: those providers genuinely default, and the
    # gateway's own catalogue offers that default as a voice.)
    voice = str(request.params.get("voice_id") or request.params.get("reference_id") or "").strip()
    if not voice:
        raise BackendError(
            "this model needs a voice: send 'voice_id' (or 'reference_id') naming one "
            "of the voices the catalogue lists for it",
            code="INVALID_REQUEST",
            status=422,
        )
    body: dict[str, Any] = {
        "model": request.model_id,
        "text": str(request.params.get("text") or ""),
        "voice_setting": {"voice_id": voice},
        "audio_setting": {"format": "mp3"},
    }
    if request.params.get("speed") is not None:
        body["voice_setting"]["speed"] = request.params["speed"]

    try:
        response = httpx.post(
            f"{_base(request)}/v1/t2a_v2",
            headers=_headers(request),
            params=_params(request),
            json=body,
            timeout=SUBMIT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    payload = _unwrap(response)
    encoded = str((payload.get("data") or {}).get("audio") or "")
    if not encoded:
        raise BackendError("minimax returned no audio", code="VOICE_PROVIDER_RESULT_INVALID")

    # HEX, not base64. Decoding as base64 yields plausible-looking noise.
    try:
        content = bytes.fromhex(encoded)
    except ValueError as exc:
        raise BackendError(
            "minimax speech audio is not valid hex", code="VOICE_PROVIDER_RESULT_INVALID"
        ) from exc

    return BackendResult(
        content=content, media_type="audio/mpeg", payload={"model": request.model_id}
    )


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
def _video(request: BackendRequest) -> BackendResult:
    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
    }
    for key in ("duration", "resolution", "first_frame_image", "prompt_optimizer"):
        if request.params.get(key) is not None:
            body[key] = request.params[key]

    base = _base(request)
    headers = _headers(request)

    try:
        submitted = httpx.post(
            f"{base}/v2/video_generation",
            headers=headers,
            params=_params(request),
            json=body,
            timeout=SUBMIT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    task_id = str(_unwrap(submitted).get("task_id") or "")
    if not task_id:
        raise BackendError("minimax returned no task_id", code="PROVIDER_FAILED")

    deadline = time.monotonic() + VIDEO_DEADLINE_S
    url = ""
    while time.monotonic() < deadline:
        time.sleep(VIDEO_POLL_INTERVAL_S)
        try:
            polled = httpx.get(
                f"{base}/v2/query/video_generation/{task_id}",
                headers=headers,
                params=_params(request),
                timeout=POLL_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise BackendError(
                f"poll network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
            ) from exc

        task = (_unwrap(polled) or {}).get("task") or {}
        status = str(task.get("status") or "")
        if status == "succeeded":
            content = task.get("content")
            url = str(content.get("url") if isinstance(content, dict) else "") or ""
            if not url:
                raise BackendError("minimax video succeeded without a url", code="PROVIDER_FAILED")
            break
        if status in {"failed", "expired"}:
            raise BackendError(f"minimax video {status}", code="PROVIDER_FAILED")
        if status not in {"queued", "running", ""}:
            raise BackendError(f"minimax video status '{status}'", code="PROVIDER_FAILED")
    else:
        raise BackendError(
            "minimax video did not finish in time",
            code="PROVIDER_SUBMISSION_UNCERTAIN",
            retryable=True,
        )

    artifact = _download(url, accept="video/mp4,video/webm", label="minimax video")
    return BackendResult(
        content=artifact.content,
        media_type=_media_type(artifact, "video/mp4"),
        payload={"model": request.model_id, "provider_url": url, "task_id": task_id},
    )


_HANDLERS = {
    "image": _image,
    "video": _video,
    "music": _music,
    "tts": _speech,
}


class MiniMaxBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        handler = _HANDLERS.get(request.modality)
        if handler is None:
            raise BackendError(
                f"MiniMax does not serve '{request.modality}'",
                code="MODEL_MODALITY_MISMATCH",
            )
        return handler(request)
