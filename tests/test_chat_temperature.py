"""The OpenAI-compatible chat lane drops a temperature the model refuses.

Newer reasoning models accept only their own default. Story Studio sends 0.7 for
every chat call, so the gateway got a `400 "Unsupported value: 'temperature' does
not support 0.7 with this model"` — and, before the worker fix, turned that
definite refusal into a permanent recovery hold. A 400 means nothing ran and
nothing was charged, so one retry without the knob is safe and makes the model
usable. The drop is recorded on the result so nobody reads the output as if 0.7
had been honoured.
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


def _request(**params) -> BackendRequest:
    return BackendRequest(
        provider_id="openai-direct",
        model_id="gpt-6-luna",
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


def _refusal(message: str) -> httpx.Response:
    return httpx.Response(400, json={"error": {"message": message}})


class ChatTemperatureTests(unittest.TestCase):
    def test_a_temperature_the_model_refuses_is_dropped_and_the_call_retried(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            # Copy: `_chat` pops `temperature` off the same dict before the retry.
            calls.append(dict(json))
            if len(calls) == 1:
                return _refusal(
                    "Unsupported value: 'temperature' does not support 0.7 with this "
                    "model. Only the default (1) value is supported."
                )
            return _answer("done")

        with patch.object(openai_compat.httpx, "post", fake_post):
            result = openai_compat._chat(_request(temperature=0.7))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["temperature"], 0.7)
        self.assertNotIn("temperature", calls[1])
        self.assertEqual(result.text, "done")
        self.assertEqual(result.payload["params_dropped"], ["temperature"])

    def test_an_unrelated_400_is_still_a_refusal(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(json)
            return _refusal("invalid response_format for this model")

        with patch.object(openai_compat.httpx, "post", fake_post):
            with self.assertRaises(BackendError) as raised:
                openai_compat._chat(_request(temperature=0.7))

        # No retry, and the refusal keeps its status so the worker can call it a
        # settled failure rather than a hold.
        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.status, 400)

    def test_a_call_with_no_temperature_is_untouched(self) -> None:
        calls: list[dict] = []

        def fake_post(url, *, headers, json, timeout):
            calls.append(json)
            return _answer()

        with patch.object(openai_compat.httpx, "post", fake_post):
            openai_compat._chat(_request())

        self.assertEqual(len(calls), 1)
        self.assertNotIn("temperature", calls[0])


if __name__ == "__main__":
    unittest.main()
