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
from gateway.adapters import anthropic, openai_compat  # noqa: E402
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

    def test_prose_that_misses_the_schema_fails(self) -> None:
        self._assert_failed_attempt(_claude("Here are your prompts: ..."), "not valid JSON")

    def test_a_cut_answer_and_a_refusal_keep_their_typed_outcomes(self) -> None:
        _, cut = _run(_claude('{"results": [', stop="max_tokens"), response_format=RESPONSE_FORMAT)
        self.assertEqual("INCOMPLETE_OUTPUT", cut.code)
        self.assertEqual('{"results": [', cut.evidence["partial_response"]["text"])
        _, refusal = _run(_claude(json.dumps(GOOD), stop="refusal"), response_format=RESPONSE_FORMAT)
        self.assertEqual("POLICY_REFUSAL", refusal.code)
        self.assertFalse(refusal.retryable)

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
            said = '{"words": "plain"}' if params.get("response_format") == {"type": "json_object"} else "plain words, not JSON"
            calls, result = _run(_claude(said), **params)
            self.assertEqual(calls[0], {
                "model": "claude-haiku-5-5", "max_tokens": anthropic.DEFAULT_MAX_TOKENS,
                "messages": [{"role": "user", "content": "hi"}], "system": "be brief"})
            self.assertEqual(result.text, said)


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


class PublishedOutputLimitTests(unittest.TestCase):
    """Anthropic's own `max_tokens` is the published `max_output_tokens` (0.2.164)."""

    def _discovered_row(self, entry: dict) -> dict:
        answer = httpx.Response(200, json={"data": [entry]})
        definition = SimpleNamespace(base_url="http://mock")
        with patch.object(discovery.httpx, "get", lambda *a, **k: answer):
            raw = discovery._anthropic_models(definition, {"api_key": "k"}).models[0].raw
        provider = SimpleNamespace(id="anthropic-direct", kind=KIND_ANTHROPIC)
        return catalog._chat_row(entry["id"], provider, {"raw": raw})

    def test_the_models_list_figure_is_published_as_the_providers(self) -> None:
        row = self._discovered_row({"id": "claude-haiku-5-5", "max_tokens": 128000,
                                    "max_input_tokens": 1000000})
        self.assertEqual(row["max_output_tokens"], 128000)
        self.assertEqual(row["max_output_tokens_source"], "provider")

    def test_a_list_without_it_keeps_the_adapter_default_as_ours(self) -> None:
        row = self._discovered_row({"id": "claude-haiku-5-5"})
        self.assertEqual(row["max_output_tokens"], anthropic.DEFAULT_MAX_TOKENS)
        self.assertEqual(row["max_output_tokens_source"], "gateway")

    def test_the_context_window_is_not_touched(self) -> None:
        row = self._discovered_row({"id": "claude-haiku-5-5", "max_tokens": 128000,
                                    "max_input_tokens": 1000000})
        self.assertIsNone(row["context_window_tokens"])

    def test_an_omitted_max_tokens_sends_the_published_cap_not_the_private_default(self) -> None:
        """**The request carries the number the catalogue promised the caller.**

        Measured live 2026-10-19: `story_idea_campaign` — 257 kB of prompt asking for 75 ideas —
        through `pool::writing-haiku-gemini-luna-ordered` answered `stop_reason: max_tokens` on
        every Anthropic attempt, because the caller sent no max and the member got the adapter's
        private 4,096, while the row above it published Haiku 5.5's own 128,000. A pool member's
        request now carries its own published cap; the caller's number still wins, and 4,096 is
        only the last resort when nothing at all is published.
        """
        from gateway.adapters.pool import PoolBackend
        from gateway import pools
        from gateway.providers.definitions import ProviderDefinition

        definition = ProviderDefinition(
            id="anthropic-direct", display_name="Anthropic", category="language",
            kind=KIND_ANTHROPIC, base_url="http://mock", modalities=("chat",),
            discovery="static",
        )
        model = {"model_id": "claude-haiku-5-5", "raw": {"max_completion_tokens": 128000}}
        self.assertEqual(
            128000, catalog.published_output_cap(definition, "claude-haiku-5-5", model),
            "the chooser reads the provider's own figure, as the published row does",
        )
        self.assertEqual(
            anthropic.DEFAULT_MAX_TOKENS,
            catalog.published_output_cap(definition, "nothing-stated", {"raw": {}}),
            "and falls back to the number it publishes as ours",
        )

        pools.create("writing", ["anthropic-direct::claude-haiku-5-5"])
        backend = PoolBackend()
        backend.attach(lambda *_: (definition, model),
                       lambda _definition: anthropic.AnthropicBackend())
        bodies: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            bodies.append(json)
            return _claude("ok")

        params = {"messages": [{"role": "user", "content": "75 ideas, please"}]}
        request = BackendRequest(
            provider_id="pool", model_id="writing", modality="chat",
            params=params, values={}, base_url="",
        )
        with patch.object(anthropic.httpx, "post", fake_post):
            backend.generate(request)
        self.assertEqual(128000, bodies[0]["max_tokens"],
                         "the member's published cap, not the adapter's private default")

        params["max_output_tokens"] = 9000  # the caller's own number still wins
        with patch.object(anthropic.httpx, "post", fake_post):
            backend.generate(request)
        self.assertEqual(9000, bodies[1]["max_tokens"])

    def test_magpies_stated_output_cap_reaches_the_member_request(self) -> None:
        """Top-level `max_output_tokens` is a stated cap, not the Hub's nested contract."""
        from gateway.adapters.pool import PoolBackend
        from gateway import pools
        from gateway.providers.definitions import KIND_OPENAI_COMPATIBLE, ProviderDefinition

        definition = ProviderDefinition(
            id="magpie", display_name="Magpie", category="language",
            kind=KIND_OPENAI_COMPATIBLE, base_url="http://mock", modalities=("chat",),
            discovery="openai_models",
        )
        model = {"model_id": "group/story-llm-free",
                 "raw": {"context_window": 1056784, "max_output_tokens": 512000}}
        self.assertEqual(512000, catalog.published_output_cap(definition, model["model_id"], model))
        row = catalog._chat_row(model["model_id"], definition, model)
        self.assertEqual(512000, row["max_output_tokens"])
        self.assertEqual("provider", row["max_output_tokens_source"])

        pools.create("magpie-cap", ["magpie::group/story-llm-free"])
        backend = PoolBackend()
        member_requests: list[BackendRequest] = []

        def adapter_for(_definition):
            member = openai_compat.OpenAICompatibleBackend()
            generate = member.generate

            def observed(member_request):
                member_requests.append(member_request)
                return generate(member_request)

            member.generate = observed
            return member

        backend.attach(lambda *_: (definition, model), adapter_for)
        bodies = []

        def fake_post(url, *, headers, json, timeout):
            bodies.append(dict(json))
            return httpx.Response(200, json={
                "model": "group/story-llm-free",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 1},
            })

        request = BackendRequest(provider_id="pool", model_id="magpie-cap", modality="chat",
                                 params={"messages": [{"role": "user", "content": "write"}]},
                                 values={"base_url": "http://mock", "api_key": "key"}, base_url="http://mock")
        with patch.object(openai_compat.httpx, "post", fake_post):
            backend.generate(request)
        self.assertEqual(512000, member_requests[0].max_output_tokens)
        self.assertEqual(512000, bodies[0]["max_tokens"])
        request.params["max_output_tokens"] = 128000
        with patch.object(openai_compat.httpx, "post", fake_post):
            backend.generate(request)
        self.assertEqual(128000, bodies[1]["max_tokens"])

    def test_the_last_resort_allowance_is_the_owners_128k(self) -> None:
        """The owner's decision, 2026-10-19: *"4096 is too small. all models write more than
        that. the minimum is at least 128K tokens."*

        Pinned as a number rather than read from the constant, deliberately: the constant is
        what this test is about, so reading it back would assert only that nobody edited the
        file. The published row is checked beside it, because the two must agree — the row
        says what the request will carry.
        """
        self.assertEqual(128_000, anthropic.DEFAULT_MAX_TOKENS)
        row = self._discovered_row({"id": "claude-haiku-5-5"})  # the models list states nothing
        self.assertEqual(128_000, row["max_output_tokens"])
        self.assertEqual("gateway", row["max_output_tokens_source"])

    def _sequence(self, *answers: httpx.Response):
        """A `fake_post` that answers each call in turn, and the bodies it saw.

        A **copy** per call: the adapter reuses one `body` dict and edits it between sends
        (`max_tokens` on the correction path, `temperature` on the refusal path), so holding
        the reference would rewrite history and make the first attempt unobservable.
        """
        bodies: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            bodies.append(dict(json))
            return answers[min(len(bodies) - 1, len(answers) - 1)]

        return bodies, fake_post

    def test_a_number_of_ours_the_model_refuses_is_corrected_from_its_own_sentence(self) -> None:
        """A definite `400` naming the model's real maximum is corrected, once.

        The last-resort allowance is now the largest figure any model in the fleet publishes,
        so a model whose own ceiling is lower answers `max_tokens: 128000 > 8192, which is the
        maximum allowed number of output tokens`. Nothing ran and nothing was billed, so the
        call is re-sent with the number the provider stated — the alternative is a model that
        is unusable for every caller that asks for nothing, which is the state this whole
        section exists to remove. Nothing is invented: the number comes from the refusal.
        """
        refusal = httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "max_tokens: 128000 > 8192, which is the maximum allowed number of "
                       "output tokens for claude-3-5-haiku-20241022"}})
        bodies, fake_post = self._sequence(refusal, httpx.Response(200, json={
            "model": "claude-3-5-haiku-20241022", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}))
        with patch.object(anthropic.httpx, "post", fake_post):
            result = anthropic.AnthropicBackend().generate(_request())
        self.assertEqual([128_000, 8192], [body["max_tokens"] for body in bodies],
                         "asked at ours, then re-sent at the model's own maximum")
        self.assertEqual(8192, result.payload["max_tokens_lowered_to"],
                         "a reduction is never quiet")

    def test_a_callers_own_number_is_never_rewritten_by_the_refusal(self) -> None:
        """The caller's figure is its own to be refused.

        Quietly answering with 8,192 when the caller asked for 128,000 is the substitution
        this repo refuses everywhere else: the caller asked for a number, and the `400` says
        why it cannot have it. Only a figure this gateway chose may be corrected.
        """
        refusal = httpx.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "max_tokens: 128000 > 8192, which is the maximum allowed number of "
                       "output tokens for claude-3-5-haiku-20241022"}})
        bodies, fake_post = self._sequence(refusal, httpx.Response(200, json={}))
        with patch.object(anthropic.httpx, "post", fake_post):
            with self.assertRaises(BackendError) as caught:
                anthropic.AnthropicBackend().generate(_request(max_output_tokens=128_000))
        self.assertEqual(1, len(bodies), "no second request")
        self.assertEqual(400, caught.exception.status)

    def test_an_unreadable_400_is_not_a_licence_to_guess_a_number(self) -> None:
        """A refusal this gateway cannot read stays the failure it was."""
        for message in ("max_tokens: something went wrong",
                        "Invalid 'max_tokens': must be greater than 0"):
            bodies, fake_post = self._sequence(
                httpx.Response(400, json={"error": {"message": message}}), httpx.Response(200, json={}))
            with patch.object(anthropic.httpx, "post", fake_post):
                with self.assertRaises(BackendError):
                    anthropic.AnthropicBackend().generate(_request())
            self.assertEqual(1, len(bodies), message)

    def test_the_openai_compatible_spelling_of_the_same_refusal_is_read(self) -> None:
        """A proxied Anthropic lane answers with the other sentence for the same fact."""
        refusal = httpx.Response(400, json={"error": {"message": (
            "Invalid 'max_tokens': integer above maximum value. Expected a value <= 16384, "
            "but got 128000 instead.")}})
        bodies, fake_post = self._sequence(refusal, httpx.Response(200, json={
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}))
        with patch.object(anthropic.httpx, "post", fake_post):
            anthropic.AnthropicBackend().generate(_request())
        self.assertEqual([128_000, 16_384], [body["max_tokens"] for body in bodies])


if __name__ == "__main__":
    unittest.main()
