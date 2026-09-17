"""ElevenLabs adapter — text-to-speech.

Synchronous: one POST returns the audio bytes. Unlike fal, Kie, Fish and Studio
Hub there is no task to poll, so this adapter does not model one.

Two things worth knowing:

  * The voice is part of the *path* (`/v1/text-to-speech/{voice_id}`), not the
    body, and ElevenLabs requires one. When the caller does not name a voice,
    the account's first voice is resolved via `GET /v1/voices` and cached rather
    than failing — an owner who has never copied a voice id should still get
    audio.
  * `model_id` *is* in the body, the opposite of Fish, whose model travels in a
    header. Two providers, opposite conventions; both are followed exactly.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

TTS_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
LOOKUP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# ElevenLabs accepts mp3_44100_128 etc. Story Studio downloads whatever it is
# given and transcodes locally, so 128 kbps mp3 is a sensible default.
OUTPUT_FORMAT = "mp3_44100_128"
MEDIA_TYPE = "audio/mpeg"

# Voice ids are 20-ish alphanumeric characters. Our own voice records use
# `voice-<hash>`, which would 404 upstream, so they are detected and ignored.
_VOICE_CACHE: dict[str, tuple[str, float]] = {}
_VOICE_CACHE_TTL_S = 1800.0


def _headers(request: BackendRequest) -> dict[str, str]:
    return {"xi-api-key": request.values.get("api_key", "")}


def _looks_like_elevenlabs_voice(value: str) -> bool:
    return bool(value) and len(value) >= 16 and value.isalnum()


def _default_voice(request: BackendRequest, base: str) -> str:
    """The account's first voice, so an unnamed request still produces audio."""
    cached = _VOICE_CACHE.get(request.provider_id)
    if cached and (time.time() - cached[1]) < _VOICE_CACHE_TTL_S:
        return cached[0]

    try:
        response = httpx.get(
            f"{base}/v1/voices", headers=_headers(request), timeout=LOOKUP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise BackendError(
            f"could not list ElevenLabs voices: {exc}",
            code="VOICE_PROVIDER_RESULT_INVALID",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        raise BackendError(
            f"ElevenLabs voice list HTTP {response.status_code}: {response.text[:200]}",
            code="VOICE_PROVIDER_RESULT_INVALID",
            status=response.status_code,
        )

    voices = (response.json() or {}).get("voices") or []
    if not voices:
        raise BackendError(
            "the ElevenLabs account has no voices to speak with",
            code="VOICE_PROVIDER_RESULT_INVALID",
        )

    voice_id = str(voices[0].get("voice_id") or "")
    if not voice_id:
        raise BackendError(
            "ElevenLabs returned a voice without an id", code="VOICE_PROVIDER_RESULT_INVALID"
        )
    _VOICE_CACHE[request.provider_id] = (voice_id, time.time())
    return voice_id


def _resolve_voice(request: BackendRequest, base: str) -> str:
    params = request.params
    for key in ("voice_id", "reference_id", "elevenlabs_voice_id"):
        candidate = str(params.get(key) or "").strip()
        if candidate and _looks_like_elevenlabs_voice(candidate):
            return candidate
    # A configured voice record may carry the provider's own id.
    voice_id = str(params.get("voice_id") or "")
    if voice_id:
        from ..catalog import catalog  # local import: avoids a cycle

        reference = catalog.voice_reference(voice_id)
        if reference:
            configured = str(reference.get("elevenlabs_voice_id") or "").strip()
            if configured:
                return configured
    return _default_voice(request, base)


def generate(request: BackendRequest) -> BackendResult:
    if request.modality != "tts":
        raise BackendError(
            f"ElevenLabs does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )

    base = (request.values.get("base_url") or request.base_url).rstrip("/")
    text = str(request.params.get("text") or "").strip()
    if not text:
        raise BackendError("no text to synthesize", code="PROVIDER_FAILED")

    voice = _resolve_voice(request, base)

    body: dict[str, Any] = {"text": text, "model_id": request.model_id}

    settings: dict[str, Any] = {}
    controls = request.params.get("controls")
    if isinstance(controls, dict):
        settings.update({k: v for k, v in controls.items() if v is not None})
    if request.params.get("speed") is not None:
        settings["speed"] = request.params["speed"]
    if settings:
        body["voice_settings"] = settings

    url = f"{base}/v1/text-to-speech/{voice}"
    if request.params.get("output_format"):
        url += f"?output_format={request.params['output_format']}"
    else:
        url += f"?output_format={OUTPUT_FORMAT}"

    try:
        response = httpx.post(
            url,
            headers={**_headers(request), "Accept": MEDIA_TYPE, "Content-Type": "application/json"},
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
        if status == 422 and "voice" in response.text.lower():
            code = "VOICE_PROVIDER_RESULT_INVALID"
        detail = response.text[:250]
        try:
            payload = response.json()
            if isinstance(payload, dict):
                nested = payload.get("detail")
                if isinstance(nested, dict):
                    detail = str(nested.get("message") or detail)[:250]
                elif isinstance(nested, str):
                    detail = nested[:250]
        except ValueError:
            pass
        raise BackendError(
            f"elevenlabs: HTTP {status}: {detail}",
            code=code,
            status=status,
            retryable=status in {408, 425, 429} or status >= 500,
        )

    if not response.content:
        raise BackendError("elevenlabs returned empty audio", code="VOICE_PROVIDER_RESULT_INVALID")

    return BackendResult(
        content=response.content,
        media_type=response.headers.get("content-type", MEDIA_TYPE).split(";")[0].strip(),
        payload={
            "model": request.model_id,
            "voice_id": voice,
            "request_id": response.headers.get("request-id"),
        },
    )


class ElevenLabsBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        return generate(request)
