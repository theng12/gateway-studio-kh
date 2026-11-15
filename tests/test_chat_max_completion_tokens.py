"""The OpenAI-compatible chat lane sends `max_completion_tokens` to models that refuse `max_tokens`.

Live 2026-11-15, gpt-6-luna through `openai-direct`: a capped request answered

    HTTP 400: Unsupported parameter: 'max_tokens' is not supported with this model.
              Use 'max_completion_tokens' instead.

while an uncapped request to the same model succeeded — so the key and the model were fine and
only the field name was wrong. Story always sends an output cap, so every gpt-6-luna job failed.

A 400 is a definite refusal: nothing ran, nothing was charged. One re-send with the spelling the
model itself named is safe, and the model is remembered so the discovery is paid once per process
rather than on every request.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()  # never bind to the owner's real Application Support

import httpx  # noqa: E402

from gateway.adapters import openai_compat  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402

_REFUSAL = (
    "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead."
)


def _request(model_id: str = "gpt-6-luna", **params) -> BackendRequest:
    return BackendRequest(
        provider_id="openai-direct",
        model_id=model_id,
        modality="chat",
        params={"input": [{"role": "user", "content": "hi"}], **params},
        values={"base_url": "http://mock", "api_key": "k"},
        base_url="http://mock",
    )


def _answer(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]},
    )


def _refusal(message: str = _REFUSAL) -> httpx.Response:
    return httpx.Response(400, json={"error": {"message": message}})


class MaxCompletionTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        # The memo is process-wide; each test starts from a model nobody has learned about.
        openai_compat._WANTS_COMPLETION_TOKENS.clear()

    def test_a_model_naming_max_completion_tokens_is_retried_with_that_field(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))  # copy: the retry mutates the same dict
            return _refusal() if len(calls) == 1 else _answer("done")

        with patch.object(openai_compat.httpx, "post", fake_post):
            result = openai_compat._chat(_request(max_tokens=4096))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", calls[0])
        self.assertEqual(calls[1]["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", calls[1])
        self.assertEqual(result.text, "done")
        self.assertEqual(result.payload["params_dropped"], ["max_tokens→max_completion_tokens"])

    def test_the_model_is_remembered_so_the_next_request_does_not_pay_for_it(self) -> None:
        first: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            first.append(dict(json))
            return _refusal() if len(first) == 1 else _answer()

        with patch.object(openai_compat.httpx, "post", fake_post):
            openai_compat._chat(_request(max_tokens=4096))

        second: list[dict] = []

        def fake_post_again(url, *, headers, json, timeout):
            second.append(dict(json))
            return _answer()

        with patch.object(openai_compat.httpx, "post", fake_post_again):
            openai_compat._chat(_request(max_tokens=4096))

        # Straight to the spelling it wants: one call, no refused discovery request.
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", second[0])

    def test_the_same_model_id_on_another_provider_keeps_max_tokens(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))
            return _answer()

        # Learn the new spelling for openai-direct's gpt-6-luna...
        def learn(url, *, headers, json, timeout):
            calls.append(dict(json))
            return _refusal() if len(calls) == 1 else _answer()
        with patch.object(openai_compat.httpx, "post", learn):
            openai_compat._chat(_request(max_tokens=4096))
        calls.clear()
        # ...another provider serving the same id is not taught by it.
        other = _request(max_tokens=4096)
        other.provider_id = "another-openai-compatible"
        with patch.object(openai_compat.httpx, "post", fake_post):
            openai_compat._chat(other)
        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", calls[0])

    def test_another_model_keeps_max_tokens(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))
            return _answer()

        with patch.object(openai_compat.httpx, "post", fake_post):
            openai_compat._chat(_request(model_id="gpt-5", max_tokens=4096))

        self.assertEqual(calls[0]["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", calls[0])

    def test_an_unrelated_400_is_still_a_refusal_with_no_retry(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))
            return _refusal("invalid response_format for this model")

        with patch.object(openai_compat.httpx, "post", fake_post):
            with self.assertRaises(BackendError) as raised:
                openai_compat._chat(_request(max_tokens=4096))

        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.status, 400)

    def test_a_request_with_no_cap_is_untouched(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))
            return _answer()

        with patch.object(openai_compat.httpx, "post", fake_post):
            openai_compat._chat(_request())

        self.assertEqual(len(calls), 1)
        self.assertNotIn("max_tokens", calls[0])
        self.assertNotIn("max_completion_tokens", calls[0])


if __name__ == "__main__":
    unittest.main()
