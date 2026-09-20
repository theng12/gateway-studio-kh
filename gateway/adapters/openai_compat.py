"""OpenAI-compatible adapter.

Covers the majority of configured providers, because OpenAI's API shape is the
de-facto standard: OpenRouter, DeepSeek, OpenAI, Groq, NVIDIA NIM, Token Harbor,
Experiential Labs, xAI, and Kie's language route all speak it. One adapter, nine
providers -- which is the whole argument for the declarative provider registry.

Handles chat, image, TTS and transcription. The async `create/poll/download`
port in `gateway.ports` is not used here on purpose: these are single
request/response calls, and wrapping them in a fake task lifecycle would add
state without adding capability.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0, read=300.0)
MEDIA_TIMEOUT = httpx.Timeout(600.0, connect=10.0, read=600.0)

# Anthropic's own names for a finish reason, translated into the vocabulary the
# contract uses — which is OpenAI's, because every other chat lane here speaks it.
#
# **The map existed but two of its three entries were identity mappings**, so a
# *successful* Claude completion published `finish_reason: "end_turn"`. Story
# Studio accepts only a clean stop and refuses anything else as a truncated
# response, so every working Claude job would have read as truncated — a
# translation half-finished is worse than none, because it looks deliberate.
# `max_tokens` is the one it got right, and it stays: that genuinely *is* a
# truncated generation and must be passed through as `length` so the consumer
# refuses it.
#
# Anything not named here is passed through unmodified rather than laundered into
# a success. An unrecognised reason is a reason to be unsure, and the safe
# direction is the consumer refusing the job rather than a partial answer being
# presented as a whole one.
ANTHROPIC_STOP_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}


def _auth(values: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {values.get('api_key', '')}"}


def _base(request: BackendRequest) -> str:
    return (request.values.get("base_url") or request.base_url).rstrip("/")


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if isinstance(error, str):
            return error[:300]
        if payload.get("detail"):
            return str(payload["detail"])[:300]
        if payload.get("message"):
            return str(payload["message"])[:300]
    return json.dumps(payload)[:300]


def _raise_for(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    retryable = status in {408, 425, 429} or status >= 500
    code = "PROVIDER_QUOTA_EXHAUSTED" if status == 429 else "PROVIDER_FAILED"
    if status in {401, 403}:
        code = "PROVIDER_AUTH_FAILED"
    raise BackendError(
        f"{provider}: HTTP {status}: {_detail(response)}",
        code=code,
        status=status,
        retryable=retryable,
    )


def _messages(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Story Studio sends `input`; older callers send `messages`."""
    raw = params.get("input") or params.get("messages") or []
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if not isinstance(raw, list):
        return [{"role": "user", "content": str(raw)}]

    messages: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or "user"
        content = item.get("content")
        if isinstance(content, list):
            # Normalise the OpenAI content-part shape, dropping unknown parts
            # rather than forwarding something the provider will reject.
            parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "text", "text": str(part["text"])})
                elif part.get("type") == "image_url" and part.get("image_url"):
                    parts.append({"type": "image_url", "image_url": part["image_url"]})
            messages.append({"role": role, "content": parts or [{"type": "text", "text": ""}]})
        else:
            messages.append({"role": role, "content": "" if content is None else str(content)})
    return messages


def _size_for(params: dict[str, Any]) -> str:
    """Map requested geometry onto a size the image APIs actually accept.

    Arbitrary width/height is rejected by OpenAI-family endpoints, so this
    snaps to the nearest supported size rather than forwarding the raw numbers.
    """
    width = params.get("width")
    height = params.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        ratio = str(params.get("aspect_ratio") or "16:9")
        if ratio in {"9:16", "3:4", "2:3"}:
            width, height = 1024, 1536
        elif ratio in {"1:1"}:
            width, height = 1024, 1024
        else:
            width, height = 1536, 1024
    if width > height:
        return "1536x1024" if min(width, height) >= 1000 else "1792x1024"
    if width < height:
        return "1024x1536" if min(width, height) >= 1000 else "1024x1792"
    return "1024x1024"


def _chat(request: BackendRequest) -> BackendResult:
    body: dict[str, Any] = {
        "model": request.model_id,
        "messages": _messages(request.params),
    }

    if request.params.get("temperature") is not None:
        body["temperature"] = request.params["temperature"]
    max_tokens = request.params.get("max_output_tokens") or request.params.get("max_tokens")
    if max_tokens:
        body["max_tokens"] = max_tokens

    response_format = request.params.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {
        "json_object",
        "json_schema",
        "text",
    }:
        body["response_format"] = response_format

    try:
        response = httpx.post(
            f"{_base(request)}/chat/completions",
            headers={**_auth(request.values), "Content-Type": "application/json"},
            json=body,
            timeout=CHAT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    _raise_for(response, request.provider_id)
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise BackendError("provider returned no choices", code="PROVIDER_FAILED")

    choice = choices[0] or {}
    message = choice.get("message") or {}
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))

    return BackendResult(
        text=text or "",
        payload={
            "finish_reason": choice.get("finish_reason"),
            "usage": payload.get("usage"),
            "model": payload.get("model") or request.model_id,
        },
    )


def _image(request: BackendRequest) -> BackendResult:
    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
        "n": 1,
        "size": _size_for(request.params),
    }
    # `b64_json` avoids a second fetch and sidesteps the same-origin rule.
    body["response_format"] = "b64_json"

    try:
        response = httpx.post(
            f"{_base(request)}/images/generations",
            headers={**_auth(request.values), "Content-Type": "application/json"},
            json=body,
            timeout=MEDIA_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    _raise_for(response, request.provider_id)
    payload = response.json()
    entries = payload.get("data") or []
    if not entries:
        raise BackendError("provider returned no image", code="IMAGE_FAILED")

    entry = entries[0] or {}
    if entry.get("b64_json"):
        content = base64.b64decode(entry["b64_json"])
    elif entry.get("url"):
        try:
            fetched = httpx.get(entry["url"], timeout=MEDIA_TIMEOUT)
        except httpx.HTTPError as exc:
            raise BackendError(f"artifact fetch failed: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc
        if fetched.status_code >= 400:
            raise BackendError(f"artifact fetch HTTP {fetched.status_code}", code="IMAGE_FAILED")
        content = fetched.content
    else:
        raise BackendError("provider returned neither b64_json nor url", code="IMAGE_FAILED")

    return BackendResult(content=content, media_type="image/png", payload={"model": request.model_id})


def _tts(request: BackendRequest) -> BackendResult:
    # `/v1/audio/speech` requires a voice, so something has to be sent — but
    # `alloy` was *our* choice presented as the caller's, and a narration in an
    # unchosen voice is silent in both directions: the caller sees the audio it did
    # not ask for, and nothing tells it why. Refuse and name the field.
    voice = str(request.params.get("voice_id") or request.params.get("voice") or "").strip()
    if not voice:
        raise BackendError(
            "this model needs a voice: send 'voice_id' (or 'voice') naming one of the "
            "voices the catalogue lists for it",
            code="INVALID_REQUEST",
            status=422,
        )
    body: dict[str, Any] = {
        "model": request.model_id,
        "input": str(request.params.get("text") or ""),
        "voice": voice,
        "response_format": "wav",
    }
    if request.params.get("speed") is not None:
        body["speed"] = request.params["speed"]

    try:
        response = httpx.post(
            f"{_base(request)}/audio/speech",
            headers={**_auth(request.values), "Content-Type": "application/json"},
            json=body,
            timeout=MEDIA_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    _raise_for(response, request.provider_id)
    return BackendResult(
        content=response.content,
        media_type=response.headers.get("content-type", "audio/wav"),
        payload={"model": request.model_id},
    )


def _transcription(request: BackendRequest) -> BackendResult:
    upload_path = request.params.get("_upload_path")
    if not upload_path or not Path(upload_path).exists():
        raise BackendError(
            "transcription needs an uploaded audio file", code="PROVIDER_FAILED"
        )
    audio = Path(upload_path).read_bytes()
    files = {
        "file": (
            request.params.get("_upload_name") or "audio.mp3",
            audio,
            request.params.get("_upload_type") or "application/octet-stream",
        )
    }
    data: dict[str, str] = {"model": request.model_id, "response_format": "srt"}
    if request.params.get("language"):
        data["language"] = str(request.params["language"])

    try:
        response = httpx.post(
            f"{_base(request)}/audio/transcriptions",
            headers=_auth(request.values),
            files=files,
            data=data,
            timeout=MEDIA_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    _raise_for(response, request.provider_id)
    return BackendResult(
        content=response.content,
        media_type="application/x-subrip",
        payload={"model": request.model_id},
    )


_HANDLERS = {
    "chat": _chat,
    "image": _image,
    "tts": _tts,
    "transcription": _transcription,
}


class OpenAICompatibleBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        handler = _HANDLERS.get(request.modality)
        if handler is None:
            raise BackendError(
                f"{request.provider_id} does not serve '{request.modality}'",
                code="MODEL_MODALITY_MISMATCH",
            )
        return handler(request)
