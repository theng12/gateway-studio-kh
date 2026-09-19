"""Google Lyria music generation.

Lyria is served through Gemini's ordinary `:generateContent`, not a `:predict`
route and not an OpenAI-compatible one, so it needs its own path even though the
Google provider is otherwise delegated to the OpenAI shim.

Verified against Google's own docs:

  * `POST {base}/models/{model}:generateContent` with `x-goog-api-key`.
  * The prompt is an ordinary text part.
  * **The audio comes back inline as base64**, at
    `candidates[0].content.parts[N].inlineData.data`, and the docs warn
    explicitly that the text parts (lyrics, structure) are interleaved in the
    *same* array — *"you should not assume the lyrics are always the first part.
    Always iterate through all parts."* So this scans every part rather than
    indexing `[0]`, which is what would silently produce no audio.

`lyria-realtime-exp` is deliberately not reachable: it is a WebSocket
bidirectional session with no unary call, so it could never answer a one-shot
generation. It is withheld in `providers/definitions.py`.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

MUSIC_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def _prompt_text(params: dict[str, Any]) -> str:
    """One text part, because Lyria takes one and controls the rest by prose.

    Lyria has no field for lyrics, title or instrumental-vs-vocal — Google states
    those are controlled by the prompt. So they are composed into it rather than
    sent as fields the schema does not have.
    """
    parts = [str(params.get("prompt") or "").strip()]
    title = str(params.get("title") or "").strip()
    if title:
        parts.append(f"Title: {title}")
    lyrics = str(params.get("lyrics") or "").strip()
    if lyrics:
        parts.append(f"Lyrics:\n{lyrics}")
    return "\n\n".join(p for p in parts if p)


def generate_music(request: BackendRequest) -> BackendResult:
    base = (request.values.get("base_url") or request.base_url).rstrip("/")
    text = _prompt_text(request.params)
    if not text:
        raise BackendError("music generation needs a prompt", code="INVALID_PARAMS")

    body = {"contents": [{"parts": [{"text": text}]}]}
    try:
        response = httpx.post(
            f"{base}/models/{request.model_id}:generateContent",
            headers={
                "x-goog-api-key": request.values.get("api_key", ""),
                "Content-Type": "application/json",
            },
            json=body,
            timeout=MUSIC_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    if response.status_code >= 400:
        status = response.status_code
        raise BackendError(
            f"Lyria HTTP {status}: {response.text[:200]}",
            code="PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED",
            status=status,
            retryable=status in {408, 425, 429} or status >= 500,
        )

    payload = response.json() or {}
    candidates = payload.get("candidates") or []
    if not candidates:
        # A safety refusal arrives here rather than as an HTTP error.
        feedback = payload.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or "no candidates"
        raise BackendError(f"Lyria returned no audio ({reason})", code="PROVIDER_FAILED")

    # Every part, in order, because the audio is not guaranteed to be first.
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data") or {}
        data = inline.get("data")
        if not data:
            continue
        try:
            content = base64.b64decode(data)
        except (ValueError, TypeError) as exc:
            raise BackendError(
                f"Lyria returned audio that is not valid base64: {exc}", code="PROVIDER_FAILED"
            ) from exc
        return BackendResult(
            content=content,
            media_type=str(inline.get("mimeType") or inline.get("mime_type") or "audio/mpeg"),
            payload={"model": request.model_id},
        )

    # A 200 with candidates but **no audio part** is retryable, because it is a
    # transient of a preview model rather than a refusal: measured live, one call
    # came back with only the lyrics part and the next identical call returned the
    # audio. A refusal looks different — it carries `promptFeedback.blockReason`
    # and is handled above as a terminal failure. Retrying a refusal would burn
    # credit for nothing; not retrying this one loses a generation that works on
    # the second attempt.
    raise BackendError(
        "Lyria returned no audio (the model answered with text only); retrying",
        code="PROVIDER_FAILED",
        retryable=True,
    )
