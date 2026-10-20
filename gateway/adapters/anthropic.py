"""Anthropic adapter — the Messages API.

Anthropic is the one configured chat provider that is not OpenAI-compatible, so
it earns its own adapter. Two differences matter:

  * `max_tokens` is **required**, not optional. A request without it is a 400,
    so a default is applied rather than forwarded as absent.
  * system prompts are a top-level `system` parameter, not a message with
    `role: "system"`. Forwarding a system message in the array is an error.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..providers.definitions import default_temperature_only, learn_default_temperature_only
from .base import (
    BackendError,
    BackendRequest,
    BackendResult,
    answer_json,
    caller_max_output_tokens,
    quota_hints,
    unusable_answer,
)
from .openai_compat import ANTHROPIC_STOP_MAP, CHAT_TIMEOUT, _temperature_refused

#: `max_tokens` is **required** by the Messages API, so a number has to be sent
#: whether or not the caller supplied one. The caller's number wins; otherwise the
#: request carries the allowance this gateway **publishes** for the model
#: (`catalog.published_output_cap`, filled in where the request is built), so the
#: row and the request cannot describe different allowances — which they did, as a
#: published 128,000 beside a silent 4,096.
#:
#: This constant is therefore the **last resort only**: no caller value and no
#: published cap. `catalog._output_default` still publishes it as ours on an
#: Anthropic row nothing else states a figure for (`max_output_tokens_source:
#: "gateway"`), so even then the fallback and the row agree.
#:
#: **128,000 since 2026-10-19 (the owner): "4096 is too small. all models write more
#: than that. the minimum is at least 128K tokens."** The number is deliberately the
#: largest published output allowance in the fleet (Anthropic's own Haiku 5.5), so a
#: row nothing states a figure for is not described more narrowly than any model the
#: catalogue knows. Two consequences, both handled rather than assumed:
#:
#:   * A model whose real ceiling is lower answers `400 "max_tokens: 128000 > N,
#:     which is the maximum allowed number of output tokens"`. That refusal is
#:     definite — nothing ran, nothing was billed — so when the number was **ours**
#:     (not the caller's) the call is re-sent once with the `N` the provider named
#:     (`_max_tokens_too_large`). A caller's own number is never rewritten: it asked
#:     for that figure, and the 400 says why it cannot have it.
#:   * Nothing is invented from the refusal. The retry uses the provider's own
#:     sentence, and the row keeps publishing the number this gateway chose.
DEFAULT_MAX_TOKENS = 128_000
ANTHROPIC_VERSION = "2023-06-01"


#: JSON Schema keywords the Messages API answers with a `400` inside `output_config.format`
#: ("If you use an unsupported feature, you'll receive a 400 error"), where every object also
#: needs `additionalProperties: false`. Story Studio's contracts use most of these (`minItems`
#: = scene count, `maxLength`, `minimum`), so `_wire_schema` removes them before sending, as
#: Anthropic's own SDKs do, and `_schema_problem` checks them on the answer instead.
#: `minItems` 0 or 1 is accepted by the API and kept.
_UNSUPPORTED_KEYWORDS = frozenset({
    "minLength", "maxLength", "pattern", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "patternProperties",
})


def _max_tokens_too_large(response: httpx.Response) -> int | None:
    """The output ceiling the provider itself named in a `400`, or `None`.

    A definite refusal — nothing ran, nothing was billed — so re-sending once with the number
    the provider stated is safe, exactly as `_temperature_refused` re-sends without a knob the
    model refuses. It exists because the gateway has to state a `max_tokens` on this lane, and
    a model whose real ceiling is lower than the figure it chose (the published cap, or
    `DEFAULT_MAX_TOKENS`) would otherwise be unusable for a caller that asked for nothing.

    Two sentences are read, because two shapes are measured. Anthropic's own:

        max_tokens: 128000 > 8192, which is the maximum allowed number of output tokens
        for claude-3-5-haiku-20241022

    and the OpenAI-compatible spelling a proxied lane answers with:

        Invalid 'max_tokens': integer above maximum value. Expected a value <= 16384,
        but got 128000 instead.

    Anything else is `None` and the 400 stays the caller-visible failure it was: a message
    this function cannot read is not a licence to guess a number.
    """
    if response.status_code != 400:
        return None
    text = response.text
    if "max_tokens" not in text:
        return None
    patterns = (
        r"max_tokens:\s*\d+\s*>\s*(\d+)",          # Anthropic: "128000 > 8192, which is the maximum"
        r"value\s*<=\s*(\d+)",                      # OpenAI-compatible: "Expected a value <= 16384"
    )
    for pattern in patterns:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            stated = int(found.group(1))
            # A ceiling of zero, or one that is not smaller than what was sent, is not a
            # usable correction: it would loop or raise. The 400 stands instead.
            return stated if stated > 0 else None
    return None


def _requested_schema(params: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON schema the caller asked for, in the shape every other chat lane receives it.

    Story Studio sends OpenAI's `response_format: {type: "json_schema", json_schema: {name,
    strict, schema}}` (`productResponseFormat`); `openai_compat` and `studio_hub` forward it
    as is. `json_object` without a schema has no Anthropic equivalent and stays prompt-only.
    """
    response_format = params.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
        return None
    spec = response_format.get("json_schema")
    schema = spec.get("schema") if isinstance(spec, dict) else None
    return schema if isinstance(schema, dict) else None


def _is_object(node: dict[str, Any]) -> bool:
    kind = node.get("type")
    return "properties" in node or kind == "object" or (isinstance(kind, list) and "object" in kind)


def _wire_schema(node: Any) -> Any:
    """`node` as the Messages API accepts it: unsupported keywords out, objects closed."""
    if isinstance(node, list):
        return [_wire_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_KEYWORDS and not (key == "minItems" and value in (0, 1)):
            continue
        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            # Keys here are property names, not keywords: a field called `pattern` stays.
            out[key] = {name: _wire_schema(sub) for name, sub in value.items()}
        elif key in {"enum", "const", "default", "examples"}:
            out[key] = value
        else:
            out[key] = _wire_schema(value)
    if _is_object(out):
        out["additionalProperties"] = False
    return out


_TYPES: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool))
    or (isinstance(v, float) and v.is_integer()),
}


def _schema_problem(value: Any, schema: Any, at: str = "$") -> str | None:
    """The first way `value` breaks `schema`, or `None`.

    The caller's **original** schema, so the keywords `_wire_schema` removed are checked here.
    ponytail: the JSON Schema subset Story Studio's contracts use (type, enum, const, anyOf,
    properties/required/additionalProperties, items, the length/count/range keywords and
    `pattern`); `$ref` and the rest pass unchecked. Upgrade path: the `jsonschema` package if
    a caller ever sends more.
    """
    if not isinstance(schema, dict):
        return None
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_TYPES.get(str(k), lambda v: True)(value) for k in kinds):
            return f"{at} is not {'/'.join(map(str, kinds))}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{at} is not one of the allowed values"
    if "const" in schema and value != schema["const"]:
        return f"{at} is not the required value"
    if isinstance(schema.get("anyOf"), list) and all(
        _schema_problem(value, option, at) for option in schema["anyOf"]
    ):
        return f"{at} matches none of its options"
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return f"{at} is shorter than {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{at} is longer than {schema['maxLength']}"
        if "pattern" in schema and not re.search(str(schema["pattern"]), value):
            return f"{at} does not match its pattern"
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{at} is below {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{at} is above {schema['maximum']}"
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return f"{at} is not above {schema['exclusiveMinimum']}"
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return f"{at} is not below {schema['exclusiveMaximum']}"
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return f"{at} has {len(value)} items, fewer than {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{at} has {len(value)} items, more than {schema['maxItems']}"
        for index, item in enumerate(value):
            problem = _schema_problem(item, schema.get("items"), f"{at}[{index}]")
            if problem:
                return problem
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{at}.{name} is missing"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                return f"{at}.{extra[0]} is not allowed"
        for name, sub in properties.items():
            if name in value:
                problem = _schema_problem(value[name], sub, f"{at}.{name}")
                if problem:
                    return problem
    return None


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

        # Whether the number on the wire is the CALLER's. It decides one thing only: a 400
        # saying the figure is above the model's own maximum may be corrected from the
        # provider's sentence when the number was ours, and never when it was theirs.
        asked = caller_max_output_tokens(request.params)
        body: dict[str, Any] = {
            "model": request.model_id,
            # **The caller's number, then the model's published one, then ours.** The caller
            # always wins, under every spelling it may use (`base.caller_max_output_tokens`:
            # `max_completion_tokens` is OpenAI's current name for the field and was being
            # dropped here, which sent this adapter's private 4,096 in the caller's place).
            # Absent a caller value the allowance is the one this gateway publishes for the
            # model (`catalog.published_output_cap` — Anthropic states 128,000 for Haiku
            # 5.5), so a long answer is not cut at 4,096 while the catalogue promises 128,000.
            # The constant is only the last resort: no published cap, no caller value.
            "max_tokens": asked or request.max_output_tokens or DEFAULT_MAX_TOKENS,
            "messages": messages or [{"role": "user", "content": ""}],
        }
        if system:
            body["system"] = system
        # **Guaranteed structured output** (`output_config.format`, GA): the API constrains
        # decoding to the schema and returns the JSON in the ordinary text block, so the
        # result below has the same shape as every other lane's. Chosen over a forced tool
        # call because `tool_choice` `any`/`tool` is a `400` on the newest Claude models.
        schema = _requested_schema(request.params)
        if schema is not None:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": _wire_schema(schema)}
            }
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

        # **A number of ours the model refuses is corrected from the model's own sentence.**
        # `asked is None` is the whole guard: the figure on the wire came from the published
        # cap or from `DEFAULT_MAX_TOKENS`, so lowering it to what the provider states gives
        # the caller everything the model can actually produce. A caller's own number is never
        # rewritten — it asked for that figure, and the 400 below says why it cannot have it.
        lowered_to: int | None = None
        if asked is None:
            stated = _max_tokens_too_large(response)
            if stated is not None and stated < int(body["max_tokens"]):
                body["max_tokens"] = stated
                lowered_to = stated
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

        payload = answer_json(response, request.provider_id)
        blocks = payload.get("content") or []
        text = "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        stop = payload.get("stop_reason")
        if stop in {"refusal", "max_tokens"}:
            code = "POLICY_REFUSAL" if stop == "refusal" else "INCOMPLETE_OUTPUT"
            error = BackendError(
                f"{request.provider_id}: answer stopped early ({stop})",
                code=code,
                retryable=stop == "max_tokens",
                upstream_accepted=True,
                upstream_settled=True,
            )
            error.evidence = {"finish_reason": stop}
            if stop == "max_tokens":
                error.evidence["partial_response"] = {
                    "text": text,
                    "finish_reason": ANTHROPIC_STOP_MAP.get(str(stop), stop),
                    "usage": payload.get("usage"),
                    "model": payload.get("model") or request.model_id,
                }
            raise error

        if schema is not None:
            # The guarantee has two documented exceptions, a refusal and a `max_tokens` cut,
            # and the keywords `_wire_schema` removed are not enforced upstream. An answer that
            # misses the schema is a failed attempt: settled (the call finished, nothing is
            # pending upstream) and retryable, so a pool moves on to its next member.
            try:
                problem = _schema_problem(json.loads(text), schema)
            except ValueError:
                problem = "the answer is not JSON"
            if problem:
                raise BackendError(
                    f"{request.provider_id}: answer does not match the requested JSON schema: "
                    f"{problem}"[:300],
                    code="PROVIDER_FAILED",
                    retryable=True,
                    upstream_accepted=True,
                    upstream_settled=True,
                )

        if not text.strip():
            # Same rule as every other chat lane: an empty answer is a failure, not a blank success.
            raise unusable_answer(request.provider_id, "it is empty")

        result_payload = {
            "finish_reason": ANTHROPIC_STOP_MAP.get(str(payload.get("stop_reason")), payload.get("stop_reason")),
            "usage": payload.get("usage"),
            "model": payload.get("model") or request.model_id,
        }
        if dropped:
            # Recorded, as `openai_compat._chat` does, so nobody reads the output as if
            # the caller's temperature had been honoured.
            result_payload["params_dropped"] = dropped
        if lowered_to is not None:
            # A reduction is never quiet: the job records that the answer was asked for at
            # the model's own maximum rather than the figure this gateway chose, and which
            # number that turned out to be.
            result_payload["max_tokens_lowered_to"] = lowered_to
        return BackendResult(text=text, payload=result_payload)
