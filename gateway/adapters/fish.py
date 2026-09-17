"""Fish Audio adapter — text-to-speech with optional voice cloning.

The API is unusual in one important way: **the model is selected by an HTTP
header**, not a body field. Fish's own quickstart is explicit about it —

    POST https://api.fish.audio/v1/tts
    Authorization: Bearer <key>
    Content-Type: application/json
    model: s2.1-pro-free
    {"text": "...", "reference_id": "...", "format": "mp3"}

So the model id travels in `model:` while everything else is JSON. Putting it in
the body instead produces a request that looks right and silently uses the
account's default model.

`reference_id` names the voice to speak with: a public Fish model id, or a
private clone's id. It is optional here -- when absent, Fish uses the account's
default voice rather than failing -- but narration almost always wants one.
"""

from __future__ import annotations

import struct
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

TTS_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# Fish accepts mp3, wav, opus, pcm. WAV is requested because Story Studio
# downloads TTS artifacts as WAV and transcodes locally.
DEFAULT_FORMAT = "wav"

_MEDIA_BY_FORMAT = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "pcm": "audio/L16",
    "pcm16": "audio/L16",
}


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
        # The model id goes in the header. This is the whole trick.
        "model": request.model_id,
    }


def _repair_streaming_wav(data: bytes) -> bytes:
    """Rewrite placeholder RIFF/data sizes on a streamed WAV.

    Fish streams WAV responses, so the RIFF and `data` chunk sizes are emitted
    as sentinels (`0xFFFFFFxx`) and never finalised. In a real response a 327 KB
    file declared a 4 GB RIFF size and a 4 GB data chunk.

    ffmpeg reads to EOF and copes, so this is not an immediate failure -- but
    the container is malformed: Python's `wave` reports a 13-hour duration, and
    any strict parser is entitled to reject it outright. Story Studio transcodes
    this file locally, so "ffmpeg happens to be lenient" is not a guarantee
    worth depending on.

    Rewriting two 4-byte fields costs nothing and yields a normal WAV.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data

    out = bytearray(data)
    struct.pack_into("<I", out, 4, len(data) - 8)

    offset = 12
    while offset + 8 <= len(out):
        chunk_id = bytes(out[offset : offset + 4])
        size = struct.unpack_from("<I", out, offset + 4)[0]
        if chunk_id == b"data":
            struct.pack_into("<I", out, offset + 4, len(data) - (offset + 8))
            break
        # A bogus size mid-stream means we can no longer walk the chunk list;
        # stop rather than spin or overrun.
        if size > len(data):
            break
        offset += 8 + size + (size & 1)

    return bytes(out)


def generate(request: BackendRequest) -> BackendResult:
    if request.modality != "tts":
        raise BackendError(
            f"Fish Audio does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )

    base = (request.values.get("base_url") or request.base_url).rstrip("/")
    params = request.params

    text = str(params.get("text") or "").strip()
    if not text:
        raise BackendError("no text to synthesize", code="PROVIDER_FAILED")

    audio_format = str(params.get("format") or DEFAULT_FORMAT).lower()
    body: dict[str, Any] = {"text": text, "format": audio_format}

    # A voice may arrive as Fish's own reference id, or as a catalogue voice id
    # that the registry resolves to one.
    reference_id = str(
        params.get("reference_id")
        or params.get("fish_reference_id")
        or ""
    ).strip()
    if not reference_id:
        reference_id = _resolve_reference(str(params.get("voice_id") or ""))
    if reference_id:
        body["reference_id"] = reference_id

    # Passthrough for the documented tuning knobs, when the caller sets them.
    for key in (
        "chunk_length",
        "normalize",
        "latency",
        "top_p",
        "temperature",
        "repetition_penalty",
        "condition_on_previous_chunks",
        "prosody",
    ):
        if params.get(key) is not None:
            body[key] = params[key]

    try:
        response = httpx.post(
            f"{base}/v1/tts",
            headers=_headers(request),
            json=body,
            timeout=TTS_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(
            f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
        ) from exc

    if response.status_code >= 400:
        status = response.status_code
        code = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED"
        if status == 429:
            code = "PROVIDER_QUOTA_EXHAUSTED"
        detail = response.text[:250]
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("message") or payload.get("detail") or detail)[:250]
        except ValueError:
            pass
        raise BackendError(
            f"fish: HTTP {status}: {detail}",
            code=code,
            status=status,
            retryable=status in {408, 425, 429} or status >= 500,
        )

    if not response.content:
        raise BackendError("fish returned empty audio", code="VOICE_PROVIDER_RESULT_INVALID")

    content = response.content
    if audio_format == "wav":
        content = _repair_streaming_wav(content)

    media_type = (
        response.headers.get("content-type", "").split(";")[0].strip()
        or _MEDIA_BY_FORMAT.get(audio_format, "audio/wav")
    )

    return BackendResult(
        content=content,
        media_type=media_type,
        payload={
            "model": request.model_id,
            "format": audio_format,
            "request_id": response.headers.get("request-id"),
            "header_repaired": audio_format == "wav",
        },
    )


def _resolve_reference(voice_id: str) -> str:
    """Turn a catalogue voice id into the Fish model id to speak with.

    Fish addresses both your clones and the public library by the same `_id`, so
    the resolved voice's `provider_voice_id` is exactly what `reference_id`
    wants.
    """
    if not voice_id:
        return ""
    from ..voices import resolve  # local import: avoids a cycle

    voice = resolve(voice_id)
    if voice is None:
        return ""
    return voice.provider_voice_id


class FishBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        return generate(request)
