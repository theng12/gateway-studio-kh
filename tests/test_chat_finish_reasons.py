#!/usr/bin/env python3
"""Provider-declared chat refusal and truncation remain distinct typed outcomes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api  # noqa: E402
from gateway.adapters import anthropic, openai_compat  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.adapters.pool import PoolBackend  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    KIND_OPENAI_COMPATIBLE,
    KIND_POOL,
    ProviderDefinition,
)


def _request(provider: str = "provider", model: str = "model") -> BackendRequest:
    return BackendRequest(
        provider_id=provider,
        model_id=model,
        modality="chat",
        params={"messages": [{"role": "user", "content": "write"}]},
        values={"base_url": "http://mock", "api_key": "key"},
        base_url="http://mock",
    )


class OpenAIFinishReasonTests(unittest.TestCase):
    def _run(self, finish_reason: str, text: str = "kept words") -> BackendError:
        answer = httpx.Response(200, json={
            "model": "model",
            "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
            "usage": {"completion_tokens": 4},
        })
        with patch.object(openai_compat.httpx, "post", return_value=answer):
            with self.assertRaises(BackendError) as caught:
                openai_compat.OpenAICompatibleBackend().generate(_request())
        return caught.exception

    def test_content_filter_is_a_terminal_policy_refusal(self) -> None:
        error = self._run("content_filter", "")
        self.assertEqual("POLICY_REFUSAL", error.code)
        self.assertTrue(error.upstream_settled)
        self.assertFalse(error.retryable)

    def test_length_is_incomplete_and_preserves_partial_text(self) -> None:
        error = self._run("length")
        self.assertEqual("INCOMPLETE_OUTPUT", error.code)
        self.assertTrue(error.upstream_settled)
        self.assertTrue(error.retryable)
        self.assertEqual("kept words", error.evidence["partial_response"]["text"])
        self.assertEqual(error.evidence["partial_response"]["finish_reason"], "length")
        self.assertEqual(error.evidence["partial_response"]["usage"], {"completion_tokens": 4})

    def test_a_truncated_answer_says_what_was_asked_and_sent(self) -> None:
        answer = httpx.Response(200, json={
            "model": "model",
            "choices": [{"message": {"content": "kept"}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 10623, "completion_tokens_details": {"reasoning_tokens": 8602}},
        })
        request = _request()
        request.params["max_tokens"] = 10624
        with patch.object(openai_compat.httpx, "post", return_value=answer):
            with self.assertRaises(BackendError) as caught:
                openai_compat.OpenAICompatibleBackend().generate(request)
        self.assertEqual(caught.exception.evidence["max_tokens_asked"], 10624)
        self.assertEqual(caught.exception.evidence["max_tokens_sent"], 10624)
        self.assertEqual(caught.exception.evidence["partial_response"]["usage"]["completion_tokens"], 10623)

    def test_sync_route_preserves_incomplete_output_and_pool_attempts(self) -> None:
        partial_text = "See https://example.com/chapter and keep studio-hub-reader exactly."
        diagnostic_url = "http://10.9.8.7:8080/api/hub/private?token=secret-token"

        class _DiagnosticAdapter:
            def generate(self, request):
                try:
                    return openai_compat.OpenAICompatibleBackend().generate(request)
                except BackendError as exc:
                    exc.args = (f"{exc}; endpoint {diagnostic_url}",)
                    raise

        member = ProviderDefinition(
            id="member",
            display_name="Member",
            category="language",
            kind=KIND_OPENAI_COMPATIBLE,
            base_url="http://mock",
            modalities=("chat",),
            discovery="static",
        )
        pool = ProviderDefinition(
            id="pool",
            display_name="Pool",
            category="language",
            kind=KIND_POOL,
            base_url="",
            modalities=("chat",),
            discovery="static",
        )
        backend = PoolBackend()
        backend.attach(
            lambda _modality, _member: (member, {"model_id": "member-model"}),
            lambda _definition: _DiagnosticAdapter(),
        )
        backend._credentials_for = lambda _provider_id: {"api_key": "key"}
        answer = httpx.Response(200, json={
            "model": "member-model",
            "choices": [{
                "message": {"content": partial_text},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 4},
        })
        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        with patch.object(api.catalog, "resolve", return_value=(pool, {"model_id": "writing"})), \
                patch.object(api, "adapter_for", return_value=backend), \
                patch.object(openai_compat.httpx, "post", return_value=answer), \
                patch("gateway.pools.members", return_value=["member::member-model"]), \
                patch("gateway.pools.routing_policy", return_value="strict_order"), \
                patch("gateway.pool_cooldowns.until", return_value=None), \
                patch("gateway.pool_cooldowns.note_failure"):
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer fixture-key"},
                json={"model": "pool::writing", "messages": [{"role": "user", "content": "write"}]},
            )

        self.assertEqual(502, response.status_code)
        body = response.json()
        self.assertEqual("INCOMPLETE_OUTPUT", body["code"])
        self.assertEqual("incomplete_output", body["failure_class"])
        self.assertEqual({
            "text": partial_text,
            "finish_reason": "length",
            "usage": {"completion_tokens": 4},
            "model": "member-model",
        }, body["partial_response"])
        self.assertEqual("member::member-model", body["pool_attempts"][0]["member"])
        self.assertEqual(
            partial_text,
            body["pool_attempts"][0]["evidence"]["partial_response"]["text"],
        )
        self.assertNotIn("10.9.8.7", body["detail"])
        self.assertNotIn("10.9.8.7", body["pool_attempts"][0]["detail"])
        self.assertNotIn("secret-token", body["detail"])
        self.assertIn("the Hub", body["pool_attempts"][0]["detail"])


class AnthropicStopReasonTests(unittest.TestCase):
    def _run(self, stop_reason: str, text: str = "kept words") -> BackendError:
        answer = httpx.Response(200, json={
            "model": "claude-haiku-5-5",
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": text}],
            "usage": {"output_tokens": 4},
        })
        with patch.object(anthropic.httpx, "post", return_value=answer):
            with self.assertRaises(BackendError) as caught:
                anthropic.AnthropicBackend().generate(
                    _request("anthropic-direct", "claude-haiku-5-5")
                )
        return caught.exception

    def test_refusal_is_a_terminal_policy_refusal(self) -> None:
        error = self._run("refusal", "")
        self.assertEqual("POLICY_REFUSAL", error.code)
        self.assertTrue(error.upstream_settled)
        self.assertFalse(error.retryable)

    def test_max_tokens_is_incomplete_and_preserves_partial_text(self) -> None:
        error = self._run("max_tokens")
        self.assertEqual("INCOMPLETE_OUTPUT", error.code)
        self.assertTrue(error.upstream_settled)
        self.assertTrue(error.retryable)
        self.assertEqual("kept words", error.evidence["partial_response"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
