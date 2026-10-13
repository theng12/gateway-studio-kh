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

from .base import Artifact, BackendError, BackendRequest, BackendResult, quota_hints

CHAT_TIMEOUT = httpx.Timeout(300.0, connect=10.0, read=300.0)
MEDIA_TIMEOUT = httpx.Timeout(600.0, connect=10.0, read=600.0)

#: How many images one request may ask for. No provider here *publishes* a maximum
#: (`n` is documented as an integer and the real cap differs per model), so this is
#: a gateway ceiling rather than a provider fact — which is why crossing it refuses
#: with the number instead of clamping and quietly returning fewer than were asked
#: for. The count also bounds how many downloads one job can perform.
MAX_IMAGE_COUNT = 10

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
        **(quota_hints(response) if status == 429 else {}),
    )


def _temperature_refused(response: httpx.Response) -> bool:
    """A 400 saying this model will not take the temperature we sent.

    Newer reasoning models accept only their own default. A 400 is a definite
    refusal — nothing ran, nothing was charged — so re-sending once without the
    knob is safe, and it is the only way a caller that always sets a temperature
    (Story Studio sends 0.7) can use such a model at all. Without this the 400
    became a permanent unknown-outcome hold instead of a working job.
    """
    if response.status_code != 400:
        return False
    text = response.text.lower()
    # Anthropic says it as "`temperature` is deprecated for this model." (2026-10-09).
    return "temperature" in text and any(
        word in text for word in ("does not support", "unsupported", "not supported", "deprecated")
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

    The mapping lives in `providers.definitions.IMAGE_SIZE_BY_RATIO`, which the
    catalogue also reads to publish each model's `output_options` — so the size
    Story Studio is shown is the size the provider is sent. It used to be a local
    DALL·E-3-era table here (`1792x1024`, `1024x1792`) that disagreed with the
    catalogue *and* is refused by `gpt-image-1`.
    """
    from ..providers.definitions import image_size_for

    return image_size_for(
        str(params.get("aspect_ratio") or "16:9"),
        params.get("width"),
        params.get("height"),
    )


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

    def send() -> httpx.Response:
        try:
            return httpx.post(
                f"{_base(request)}/chat/completions",
                headers={**_auth(request.values), "Content-Type": "application/json"},
                json=body,
                timeout=CHAT_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise BackendError(
                f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
            ) from exc

    response = send()
    dropped: list[str] = []
    if "temperature" in body and _temperature_refused(response):
        # The model supports only its default temperature. Drop the knob and ask
        # once more; the result records that it was dropped, so nobody reads the
        # output as if 0.7 had been honoured.
        body.pop("temperature")
        dropped.append("temperature")
        response = send()

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

    finish_reason = choice.get("finish_reason")
    refusal = message.get("refusal")
    if finish_reason in {"content_filter", "length"} or refusal:
        policy_refusal = finish_reason == "content_filter" or bool(refusal)
        error = BackendError(
            f"{request.provider_id}: answer stopped early "
            f"({finish_reason or 'refusal'})",
            code="POLICY_REFUSAL" if policy_refusal else "INCOMPLETE_OUTPUT",
            retryable=not policy_refusal,
            upstream_accepted=True,
            upstream_settled=True,
        )
        error.evidence = {"finish_reason": finish_reason or "refusal"}
        if not policy_refusal:
            error.evidence["partial_response"] = {
                "text": text or "",
                "finish_reason": finish_reason,
                "usage": payload.get("usage"),
                "model": payload.get("model") or request.model_id,
            }
        raise error

    result_payload = {
        "finish_reason": finish_reason,
        "usage": payload.get("usage"),
        "model": payload.get("model") or request.model_id,
    }
    if dropped:
        result_payload["params_dropped"] = dropped
    return BackendResult(text=text or "", payload=result_payload)


def _image_count(params: dict[str, Any]) -> int:
    """How many images the caller asked for, refusing anything that is not a count.

    A wrong `n` used to be impossible to send because `n` was hardcoded to 1. Now
    that it is reachable, a value the provider would reject — or that we would have
    to round — is refused here, where the message can name the field, rather than at
    the far end where it arrives as an opaque 400.
    """
    raw = params.get("n")
    if raw is None:
        return 1
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise BackendError(f"n must be a whole number, got {raw!r}", code="INVALID_REQUEST")
    try:
        count = int(str(raw).strip())
    except ValueError:
        raise BackendError(
            f"n must be a whole number, got {raw!r}", code="INVALID_REQUEST"
        ) from None
    if not 1 <= count <= MAX_IMAGE_COUNT:
        raise BackendError(
            f"n must be between 1 and {MAX_IMAGE_COUNT}, got {count}", code="INVALID_REQUEST"
        )
    return count


#: Image models that always answer base64 and refuse `response_format`.
#:
#: A prefix, because the family is what changed -- `gpt-image-1`,
#: `gpt-image-2.5-sunburst-2026-09-08` and whatever ships next all behave the same
#: way, and listing every dated snapshot would rot.
IMAGE_MODELS_WITHOUT_RESPONSE_FORMAT = ("gpt-image",)


def _takes_response_format(model_id: str) -> bool:
    """Whether this image model accepts `response_format` at all."""
    name = str(model_id or "").strip().lower()
    return not any(name.startswith(prefix) for prefix in IMAGE_MODELS_WITHOUT_RESPONSE_FORMAT)


def _image(request: BackendRequest) -> BackendResult:
    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
        "n": _image_count(request.params),
        "size": _size_for(request.params),
    }
    # `b64_json` avoids a second fetch and sidesteps the same-origin rule -- but
    # only ask for it where the model takes the parameter.
    #
    # **The `gpt-image-*` family rejects it outright.** Measured 2026-09-30 on the
    # owner's gateway: `gpt-image-2.5-sunburst-2026-09-08` answered
    # `HTTP 400: Unknown parameter: 'response_format'` on every attempt, while the
    # older `dall-e-*` models still accept it and still need it to avoid a URL. The
    # newer models return `b64_json` unconditionally, so omitting it is not a
    # trade -- it is the only spelling they accept, and the reader below already
    # handles either shape.
    #
    # Curated rather than discovered, for the same reason
    # `TRANSCRIPTION_SRT_UNSUPPORTED` is a list: no provider *publishes* which
    # parameters it rejects, so the alternative is discovering it one failed
    # generation at a time.
    if _takes_response_format(request.model_id):
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

    # **Every entry is kept, not just the first.** `n: 2` asks for two images, and a
    # provider may answer several even when asked for one; taking `entries[0]`
    # discarded the rest silently. The first entry must still be fetchable, because
    # it is the artifact the single-artifact image routes serve — a later one that
    # will not fetch is counted in `variations_failed` rather than failing a call
    # that did produce a usable image.
    artifacts: list[Artifact] = [Artifact(content, "image/png")]
    failed = 0
    for raw_entry in entries[1:]:
        extra = raw_entry or {}
        try:
            if extra.get("b64_json"):
                extra_content = base64.b64decode(extra["b64_json"])
            elif extra.get("url"):
                fetched = httpx.get(extra["url"], timeout=MEDIA_TIMEOUT)
                if fetched.status_code >= 400:
                    failed += 1
                    continue
                extra_content = fetched.content
            else:
                failed += 1
                continue
        except (httpx.HTTPError, ValueError):
            failed += 1
            continue
        artifacts.append(Artifact(extra_content, "image/png"))

    result_payload: dict[str, Any] = {"model": request.model_id, "variation_count": len(entries)}
    if failed:
        result_payload["variations_failed"] = failed

    return BackendResult(
        content=artifacts[0].content,
        media_type=artifacts[0].media_type,
        payload=result_payload,
        additional_artifacts=artifacts[1:],
    )


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
