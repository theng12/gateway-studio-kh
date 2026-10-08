"""Anthropic adapter — the Messages API.

Anthropic is the one configured chat provider that is not OpenAI-compatible, so
it earns its own adapter. Two differences matter:

  * `max_tokens` is **required**, not optional. A request without it is a 400,
    so a default is applied rather than forwarded as absent.
  * system prompts are a top-level `system` parameter, not a message with
    `role: "system"`. Forwarding a system message in the array is an error.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..providers.definitions import default_temperature_only, learn_default_temperature_only
from .base import BackendError, BackendRequest, BackendResult, quota_hints
from .openai_compat import ANTHROPIC_STOP_MAP, CHAT_TIMEOUT, _temperature_refused

#: `max_tokens` is **required** by the Messages API, so a number has to be sent
#: whether or not the caller supplied one. The value is **published as ours**:
#: `catalog._chat_row` reads this constant and puts it on the Anthropic rows with
#: `max_output_tokens_source: "gateway"`, so the row and the request cannot
#: describe different allowances — which they did, as `null` beside a silent 4096.
#:
#: ponytail: one number for every Anthropic model, ours. Ceiling — a model whose
#: real ceiling is lower is over-described and a longer answer is truncated at
#: 4096 with no signal. Upgrade path — the owner's `model-limits.local.json`
#: already flows through `_limit_with_source` ahead of this fallback, per model.
DEFAULT_MAX_TOKENS = 4096
ANTHROPIC_VERSION = "2023-06-01"


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Pull system turns out of the message array into the `system` parameter."""
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.extend(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            continue
        rest.append(
            {
                "role": "assistant" if message.get("role") == "assistant" else "user",
                "content": content if content is not None else "",
            }
        )
    return "\n\n".join(p for p in system_parts if p), rest


class AnthropicBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality != "chat":
            raise BackendError(
                f"{request.provider_id} does not serve '{request.modality}'",
                code="MODEL_MODALITY_MISMATCH",
            )

        from .openai_compat import _messages  # shared normalisation

        system, messages = _split_system(_messages(request.params))

        body: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.params.get("max_output_tokens")
            or request.params.get("max_tokens")
            or DEFAULT_MAX_TOKENS,
            "messages": messages or [{"role": "user", "content": ""}],
        }
        if system:
            body["system"] = system
        dropped: list[str] = []
        if request.params.get("temperature") is not None:
            # A default-only family answers any temperature with a definite `400`
            # ("`temperature` is deprecated for this model."), and Story Studio sends
            # 0.7 on every chat call. Same table the catalogue withholds the control by.
            if default_temperature_only(request.provider_id, request.model_id):
                dropped.append("temperature")
            else:
                body["temperature"] = request.params["temperature"]

        base = (request.values.get("base_url") or request.base_url).rstrip("/")

        def send() -> httpx.Response:
            try:
                return httpx.post(
                    f"{base}/messages",
                    headers={
                        "x-api-key": request.values.get("api_key", ""),
                        "anthropic-version": ANTHROPIC_VERSION,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=CHAT_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                ) from exc

        response = send()
        if "temperature" in body and _temperature_refused(response):
            # A family the table above does not know yet. The 400 is definite (nothing
            # ran, nothing billed), so ask once more without it, as `openai_compat._chat`
            # does, and remember the model so the catalogue stops offering the control.
            body.pop("temperature")
            dropped.append("temperature")
            learn_default_temperature_only(request.provider_id, request.model_id)
            response = send()

        if response.status_code >= 400:
            status = response.status_code
            code = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED"
            if status == 429:
                code = "PROVIDER_QUOTA_EXHAUSTED"
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = response.text[:200]
            raise BackendError(
                f"{request.provider_id}: HTTP {status}: {detail}"[:300],
                code=code,
                status=status,
                retryable=status in {408, 425, 429} or status >= 500,
                **(quota_hints(response) if status == 429 else {}),
            )

        payload = response.json()
        blocks = payload.get("content") or []
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

        result_payload = {
            "finish_reason": ANTHROPIC_STOP_MAP.get(str(payload.get("stop_reason")), payload.get("stop_reason")),
            "usage": payload.get("usage"),
            "model": payload.get("model") or request.model_id,
        }
        if dropped:
            # Recorded, as `openai_compat._chat` does, so nobody reads the output as if
            # the caller's temperature had been honoured.
            result_payload["params_dropped"] = dropped
        return BackendResult(text=text, payload=result_payload)
