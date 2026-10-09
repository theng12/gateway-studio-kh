"""Claude models get guaranteed structured output (`output_config.format`).

Story Studio refused Claude Haiku 5.5 for scene plans and image prompts because the catalogue
did not publish `supports_structured_output: true`. Anthropic states the capability per model
(`/v1/models` `capabilities.structured_outputs.supported`), and the Messages API constrains
decoding to a JSON schema sent as `output_config.format`. The schema arrives the way every
other lane receives it: OpenAI's `response_format: {type: "json_schema", json_schema: {...}}`.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()  # never bind to the owner's real Application Support

import httpx  # noqa: E402

from gateway import catalog  # noqa: E402
from gateway.adapters import anthropic  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.adapters.pool import _outcome_known  # noqa: E402
from gateway.providers import discovery  # noqa: E402
from gateway.providers.definitions import KIND_ANTHROPIC  # noqa: E402

#: The shape of Story Studio's image-prompt contract (`story-output-contracts.ts`): a scene
#: count pinned by `minItems`/`maxItems`, bounded strings, an id enum, closed objects.
SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "string", "enum": ["s1", "s2"]},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "number": {"type": "integer", "minimum": 1},
                },
                "required": ["scene_id", "prompt"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}
RESPONSE_FORMAT = {"type": "json_schema",
                   "json_schema": {"name": "image_prompts", "strict": True, "schema": SCHEMA}}
GOOD = {"results": [{"scene_id": "s1", "prompt": "a lantern"},
                    {"scene_id": "s2", "prompt": "a river", "number": 2}]}


def _request(model: str = "claude-haiku-5-5", **params) -> BackendRequest:
    return BackendRequest(
        provider_id="anthropic-direct",
        model_id=model,
        modality="chat",
        params={"messages": [{"role": "system", "content": "be brief"},
                             {"role": "user", "content": "hi"}], **params},
        values={"base_url": "http://mock", "api_key": "k"},
        base_url="http://mock",
    )


def _claude(text: str, stop: str = "end_turn") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "claude-haiku-5-5", "stop_reason": stop,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "output_tokens": 5}})


def _run(answer: httpx.Response, **params):
    calls: list[dict] = []

    def fake_post(url, *, headers, json, timeout):
        calls.append(json)
        return answer

    with patch.object(anthropic.httpx, "post", fake_post):
        try:
            return calls, anthropic.AnthropicBackend().generate(_request(**params))
        except BackendError as exc:
            return calls, exc


class SchemaRequestPayloadTests(unittest.TestCase):
    def test_a_json_schema_request_becomes_output_config_format(self) -> None:
        calls, _ = _run(_claude(json.dumps(GOOD)), response_format=RESPONSE_FORMAT)
        body = calls[0]
        self.assertNotIn("response_format", body)
        fmt = body["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        wire = fmt["schema"]
        items = wire["properties"]["results"]["items"]
        # The keywords the API answers with a 400 are gone; the structure is kept.
        self.assertNotIn("minItems", wire["properties"]["results"])
        self.assertNotIn("maxItems", wire["properties"]["results"])
        self.assertEqual(items["properties"]["prompt"], {"type": "string"})
        self.assertEqual(items["properties"]["number"], {"type": "integer"})
        self.assertEqual(items["properties"]["scene_id"]["enum"], ["s1", "s2"])
        self.assertEqual(items["required"], ["scene_id", "prompt"])
        self.assertIs(wire["additionalProperties"], False)
        self.assertIs(items["additionalProperties"], False)
        # The caller's schema is not modified (it is checked against afterwards).
        self.assertEqual(SCHEMA["properties"]["results"]["minItems"], 2)
        self.assertEqual(body["system"], "be brief")

    def test_every_object_is_closed_and_property_names_are_not_keywords(self) -> None:
        wire = anthropic._wire_schema({
            "type": "object",
            "properties": {"pattern": {"type": "string", "pattern": "^a"},
                           "inner": {"type": ["object", "null"], "properties": {}},
                           "tags": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
        })
        self.assertIn("pattern", wire["properties"])            # a field called `pattern`
        self.assertNotIn("pattern", wire["properties"]["pattern"])  # the keyword
        self.assertIs(wire["additionalProperties"], False)
        self.assertIs(wire["properties"]["inner"]["additionalProperties"], False)
        self.assertEqual(wire["properties"]["tags"]["minItems"], 1)  # 0/1 is accepted


class SchemaAnswerTests(unittest.TestCase):
    def test_a_schema_valid_answer_comes_back_as_json_text_like_every_lane(self) -> None:
        _, result = _run(_claude(json.dumps(GOOD)), response_format=RESPONSE_FORMAT)
        self.assertEqual(json.loads(result.text), GOOD)
        self.assertEqual(result.payload["finish_reason"], "stop")
        self.assertEqual(result.payload["usage"]["output_tokens"], 5)

    def _assert_failed_attempt(self, answer: httpx.Response, fragment: str) -> None:
        _, exc = _run(answer, response_format=RESPONSE_FORMAT)
        self.assertIsInstance(exc, BackendError)
        self.assertEqual(exc.code, "PROVIDER_FAILED")
        self.assertTrue(exc.retryable)
        self.assertTrue(exc.upstream_settled)
        self.assertIn(fragment, str(exc))
        # A settled failure leaves nothing upstream, so a pool moves on (Haiku → Gemini).
        self.assertTrue(_outcome_known(exc, sent=True))

    def test_a_stripped_constraint_is_still_enforced(self) -> None:
        short = {"results": [{"scene_id": "s1", "prompt": "only one"}]}
        self._assert_failed_attempt(_claude(json.dumps(short)), "fewer than 2")

    def test_a_missing_field_an_empty_string_and_a_wrong_enum_fail(self) -> None:
        missing = {"results": [{"scene_id": "s1"}, {"scene_id": "s2", "prompt": "x"}]}
        self._assert_failed_attempt(_claude(json.dumps(missing)), "prompt is missing")
        empty = {"results": [{"scene_id": "s1", "prompt": ""}, {"scene_id": "s2", "prompt": "x"}]}
        self._assert_failed_attempt(_claude(json.dumps(empty)), "shorter than 1")
        enum = {"results": [{"scene_id": "s9", "prompt": "a"}, {"scene_id": "s2", "prompt": "x"}]}
        self._assert_failed_attempt(_claude(json.dumps(enum)), "allowed values")
        extra = {"results": GOOD["results"], "note": "hi"}
        self._assert_failed_attempt(_claude(json.dumps(extra)), "note is not allowed")

    def test_prose_a_cut_answer_and_a_refusal_fail(self) -> None:
        self._assert_failed_attempt(_claude("Here are your prompts: ..."), "not JSON")
        self._assert_failed_attempt(_claude('{"results": [', stop="max_tokens"), "max_tokens")
        self._assert_failed_attempt(_claude(json.dumps(GOOD), stop="refusal"), "refusal")

    def test_validator_types(self) -> None:
        problem = anthropic._schema_problem
        self.assertIsNone(problem(3, {"type": "integer", "maximum": 3}))
        self.assertIsNotNone(problem(True, {"type": "integer"}))
        self.assertIsNotNone(problem(4, {"type": "number", "maximum": 3}))
        self.assertIsNone(problem(None, {"type": ["string", "null"]}))
        self.assertIsNone(problem("b", {"anyOf": [{"const": "a"}, {"const": "b"}]}))
        self.assertIsNotNone(problem("c", {"anyOf": [{"const": "a"}, {"const": "b"}]}))
        self.assertIsNotNone(problem("xyz", {"type": "string", "pattern": "^a"}))


class NonSchemaRequestsAreUnchangedTests(unittest.TestCase):
    def test_plain_and_json_object_requests_send_the_same_body_as_before(self) -> None:
        for params in ({}, {"response_format": {"type": "json_object"}},
                       {"response_format": {"type": "text"}}):
            calls, result = _run(_claude("plain words, not JSON"), **params)
            self.assertEqual(calls[0], {
                "model": "claude-haiku-5-5", "max_tokens": anthropic.DEFAULT_MAX_TOKENS,
                "messages": [{"role": "user", "content": "hi"}], "system": "be brief"})
            self.assertEqual(result.text, "plain words, not JSON")


def _row(model_id: str, capabilities) -> dict:
    provider = SimpleNamespace(id="anthropic-direct", kind=KIND_ANTHROPIC,
                               base_url="https://api.anthropic.com/v1",
                               display_name="Anthropic")
    raw = {"id": model_id, "capabilities": capabilities}
    return catalog._chat_row(model_id, provider, {"raw": raw})


class CapabilityFlagTests(unittest.TestCase):
    def test_a_stated_capability_publishes_the_strong_mode(self) -> None:
        for model in ("claude-haiku-5-5", "claude-haiku-4-5"):
            row = _row(model, {"structured_outputs": {"supported": True}})
            self.assertIs(row["supports_structured_output"], True)
            self.assertEqual(row["structured_output_mode"], "request_json_schema")

    def test_a_stated_no_is_false_and_silence_stays_unknown(self) -> None:
        row = _row("claude-old", {"structured_outputs": {"supported": False}})
        self.assertIs(row["supports_structured_output"], False)
        self.assertIsNone(row["structured_output_mode"])
        row = _row("claude-old", None)
        self.assertIsNone(row["supports_structured_output"])
        self.assertIsNone(row["structured_output_mode"])

    def test_discovery_keeps_anthropics_capability_tree(self) -> None:
        caps = {"structured_outputs": {"supported": True}, "image_input": {"supported": True}}
        answer = httpx.Response(200, json={"data": [
            {"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5",
             "created_at": "2026-09-01T00:00:00Z", "capabilities": caps}]})
        definition = SimpleNamespace(base_url="http://mock")
        with patch.object(discovery.httpx, "get", lambda *a, **k: answer):
            result = discovery._anthropic_models(definition, {"api_key": "k"})
        self.assertEqual(result.models[0].raw["capabilities"], caps)


if __name__ == "__main__":
    unittest.main()
