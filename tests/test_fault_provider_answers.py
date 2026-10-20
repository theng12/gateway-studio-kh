#!/usr/bin/env python3
"""Fault injection: a provider's own unusable answer is a *settled* failure, never a hold.

Phase 3, 2026-10-20. The provider **answered** — its bytes are on the wire — so nothing is
outstanding upstream and the job must settle as a *known* failure carrying the reason. It must
never become an `unknown_outcome` hold: that verdict means "we cannot tell whether work is still
running", and a hold with no attempt to aim at has no settle path at all.

Measured on this gateway before the fix, through the real adapters and the real worker:

  * a chat lane answered `200` with a CDN's HTML error page → `failed / unknown_outcome`,
    `retryable: False`, "Upstream outcome is unknown; reconcile the provider request before
    retrying" — an unresolvable hold for a request that provably finished;
  * the Hub's own empty chat answer → the same hold;
  * an OpenAI-compatible empty answer → **succeeded with blank text**, a success with no output.

After the fix each is a settled, retryable failure whose sentence names what was wrong with the
answer, so the caller's Retry can get a fresh answer and a second bad answer is bounded by the
ordinary retry ceiling instead of looping.

Offline: one loopback HTTP server stands in for the provider. No live provider, no generation.
"""
from __future__ import annotations

import atexit
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-fault-answers-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters.anthropic import AnthropicBackend  # noqa: E402
from gateway.adapters.base import answer_json, unusable_answer  # noqa: E402
from gateway.adapters.openai_compat import OpenAICompatibleBackend  # noqa: E402
from gateway.adapters.pool import PoolBackend  # noqa: E402
from gateway.adapters.studio_hub import StudioHubBackend  # noqa: E402
from gateway.store import JobStore  # noqa: E402

HTML_ERROR = b"<html><body><h1>502 Bad Gateway</h1></body></html>"
EMPTY_ANSWER = json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}).encode()
GOOD_ANSWER = json.dumps(
    {"choices": [{"message": {"content": "a real answer"}, "finish_reason": "stop"}]}
).encode()


class _Provider:
    """One loopback provider whose 200 answer this test chooses, so a real request is sent."""

    def __init__(self, body: bytes = HTML_ERROR, content_type: str = "text/html") -> None:
        self.body, self.content_type, self.calls = body, content_type, 0
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.calls += 1
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("content-type", outer.content_type)
                self.send_header("content-length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            do_POST = _answer
            do_GET = _answer

            def log_message(self, *args: Any) -> None:  # keep the test output clean
                pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve(self, body: bytes, content_type: str = "application/json") -> None:
        """What the next call answers — the same provider, a different answer."""
        self.body, self.content_type = body, content_type

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _Registry:
    """The registry the worker reads: these providers, at their loopback bases."""

    def __init__(self, bases: dict[str, str], kind: str) -> None:
        self.bases, self.kind = bases, kind
        first = next(iter(bases.values()))
        self.credentials = SimpleNamespace(
            is_enabled=lambda _provider_id: True,
            resolved=lambda provider_id: {"api_key": "fixture", "hub_token": "fixture",
                                          "base_url": bases.get(provider_id, first)},
        )
        self.models = SimpleNamespace(
            enabled_models=lambda _modality: [
                (SimpleNamespace(id=pid, base_url=base, display_name=pid), {"model_id": "mock-model"})
                for pid, base in bases.items()
            ]
        )

    def definition(self, provider_id: str) -> Any:
        return SimpleNamespace(id=provider_id, kind=self.kind,
                               base_url=self.bases.get(provider_id, next(iter(self.bases.values()))),
                               display_name=provider_id)


class _AnswerCase(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def provider(self, body: bytes = HTML_ERROR, content_type: str = "text/html") -> _Provider:
        provider = _Provider(body, content_type)
        self.addCleanup(provider.close)
        return provider

    def run_job(self, provider: _Provider, adapter: Any, *, kind: str,
                modality: str = "chat", name: str | None = None,
                params: dict[str, Any] | None = None) -> dict[str, Any]:
        store = JobStore(_TEMP_ROOT / f"{name or self.id().split('.')[-1]}.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(
            modality=modality, model_id="mock-model",
            params=params if params is not None else {"input": [{"role": "user", "content": "hi"}]},
            idempotency_key=f"{name or self.id()}-key", provider_id="mock-provider",
        )
        self.assertTrue(store.mark_running(row["id"]))
        with patch.object(worker_module, "registry", _Registry({"mock-provider": provider.base}, kind)), \
                patch.object(worker_module, "adapter_for", return_value=adapter):
            worker_module.execute_job(row, store)
        return store.get(row["id"])

    def assert_settled_refusal(self, job: dict[str, Any], *words: str) -> None:
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["recovery_required"],
                         "the provider answered: nothing is outstanding to reconcile")
        self.assertNotEqual(job["recovery_state"], "unknown_outcome")
        self.assertTrue(job["retryable"], "Retry must be able to ask for a fresh answer")
        for word in words:
            self.assertIn(word, job["error_detail"], job["error_detail"])


class ChatAnswerTests(_AnswerCase):
    def test_an_openai_lane_body_that_is_not_json_is_a_known_failure(self) -> None:
        # Malformed, and the same body cut short in flight: both are a 200 the lane cannot read.
        for name, body, ctype in (
            ("oai-not-json", HTML_ERROR, "text/html"),
            ("oai-truncated", b'{"choices": [{"message": {"content": "half an ans', "application/json"),
        ):
            with self.subTest(name):
                job = self.run_job(self.provider(body, ctype), OpenAICompatibleBackend(),
                                   kind="openai_compatible", name=name)
                self.assert_settled_refusal(job, "not JSON")

    def test_an_empty_openai_answer_is_not_a_blank_success(self) -> None:
        # Measured before the fix: this job was `succeeded` with `text: ""`.
        job = self.run_job(self.provider(EMPTY_ANSWER, "application/json"),
                           OpenAICompatibleBackend(), kind="openai_compatible", name="oai-empty")
        self.assert_settled_refusal(job, "empty")
        self.assertIsNone(job.get("asset_path"))

    def test_an_anthropic_lane_refuses_both_shapes(self) -> None:
        for name, body, ctype, word in (
            ("anthropic-not-json", HTML_ERROR, "text/html", "not JSON"),
            ("anthropic-empty", json.dumps({"content": [], "stop_reason": "end_turn"}).encode(),
             "application/json", "empty"),
        ):
            with self.subTest(name):
                job = self.run_job(self.provider(body, ctype), AnthropicBackend(),
                                   kind="anthropic", name=name)
                self.assert_settled_refusal(job, word)

    def test_the_hub_chat_lane_refuses_both_shapes(self) -> None:
        for name, body, ctype, word in (
            ("hub-not-json", HTML_ERROR, "text/html", "not JSON"),
            ("hub-empty", EMPTY_ANSWER, "application/json", "empty"),
        ):
            with self.subTest(name):
                job = self.run_job(self.provider(body, ctype), StudioHubBackend(),
                                   kind="studio_hub", name=name)
                self.assert_settled_refusal(job, word)


class ImageAnswerTests(_AnswerCase):
    def test_an_image_lane_body_that_is_not_json_is_a_known_failure(self) -> None:
        job = self.run_job(self.provider(), OpenAICompatibleBackend(),
                           kind="openai_compatible", modality="image",
                           name="oai-image-not-json", params={"prompt": "a cat"})
        self.assert_settled_refusal(job, "not JSON")

    def test_an_image_answer_with_no_image_is_a_known_failure(self) -> None:
        job = self.run_job(self.provider(b'{"data": []}', "application/json"),
                           OpenAICompatibleBackend(), kind="openai_compatible", modality="image",
                           name="oai-image-none", params={"prompt": "a cat"})
        self.assert_settled_refusal(job, "no image")


class NeverALoopTests(_AnswerCase):
    """A second bad answer is bounded by the ordinary retry ceiling, not an endless re-ask."""

    def test_every_machine_that_answers_garbage_ends_the_job_terminal(self) -> None:
        sites = [self.provider() for _ in range(3)]
        bases = {f"site-{index}": site.base for index, site in enumerate(sites)}
        remaining = list(bases)
        store = JobStore(_TEMP_ROOT / "three-garbage-sites.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="chat", model_id="mock-model",
                              params={"input": [{"role": "user", "content": "hi"}]},
                              idempotency_key="three-garbage-sites", provider_id="site-0")
        self.assertTrue(store.mark_running(row["id"]))

        def route(_model_id: str, _modality: str, *, exclude_locations: Any = None, **_k: Any):
            tried = {str(item) for item in (exclude_locations or ())}
            left = [pid for pid in remaining if pid not in tried and pid.lstrip("site-") not in tried]
            return (SimpleNamespace(id=left[0]), "") if left else (None, "")

        with patch.object(worker_module, "registry", _Registry(bases, "openai_compatible")), \
                patch.object(worker_module, "adapter_for", return_value=OpenAICompatibleBackend()), \
                patch.object(worker_module.fleet, "route", side_effect=route):
            worker_module.execute_job(dict(row, provider_id="site-0"), store)

        job = store.get(row["id"])
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["recovery_required"], "the providers answered; nothing is outstanding")
        self.assertEqual([site.calls for site in sites], [1, 1, 1],
                         "each machine was asked exactly once, then the policy ended")
        self.assertIn("tried 3 machines and every one failed", job["error_detail"])
        self.assertIn("not JSON", job["error_detail"])

    def test_a_retry_after_a_settled_refusal_gets_a_fresh_answer(self) -> None:
        """The reason the verdict is `retryable`: the same provider, answering properly, works."""
        provider = self.provider()
        job = self.run_job(provider, OpenAICompatibleBackend(), kind="openai_compatible",
                           name="retry-fresh")
        self.assert_settled_refusal(job, "not JSON")

        provider.serve(GOOD_ANSWER)
        store = JobStore(_TEMP_ROOT / "retry-fresh-followup.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="chat", model_id="mock-model",
                              params={"input": [{"role": "user", "content": "hi"}]},
                              idempotency_key="retry-fresh-followup", provider_id="mock-provider")
        self.assertTrue(store.mark_running(row["id"]))
        with patch.object(worker_module, "registry",
                          _Registry({"mock-provider": provider.base}, "openai_compatible")), \
                patch.object(worker_module, "adapter_for", return_value=OpenAICompatibleBackend()):
            worker_module.execute_job(row, store)
        again = store.get(row["id"])
        self.assertEqual(again["state"], "succeeded", again.get("error_detail"))
        self.assertEqual(json.loads(again["result_json"])["text"], "a real answer")


class PoolOfBadAnswersTests(_AnswerCase):
    """The same rule through a pool: a member's unusable answer must not escape as a crash.

    `PoolBackend` catches `BackendError` and only `BackendError`. Before the fix the member's
    `ValueError` escaped the whole walk and reached the worker's generic handler — an
    unknown-outcome hold for a pool that had answered on every member.
    """

    def test_a_pool_whose_members_all_answer_garbage_ends_the_job_known(self) -> None:
        members = {"member-a": self.provider(), "member-b": self.provider()}
        bases = {name: provider.base for name, provider in members.items()}
        bases["pool"] = members["member-a"].base
        registry = _Registry(bases, "openai_compatible")

        def resolve(_modality: str, member: str):
            return (registry.definition(member), {"model_id": "mock-model"})

        backend = PoolBackend()
        backend.attach(resolve, lambda _definition: OpenAICompatibleBackend())
        backend._credentials_for = lambda _provider_id: {"api_key": "fixture"}

        store = JobStore(_TEMP_ROOT / "pool-of-garbage.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="chat", model_id="mock-model",
                              params={"input": [{"role": "user", "content": "hi"}]},
                              idempotency_key="pool-of-garbage", provider_id="pool")
        self.assertTrue(store.mark_running(row["id"]))
        with patch.object(worker_module, "registry", registry), \
                patch.object(worker_module, "adapter_for", return_value=backend), \
                patch("gateway.pools.members", return_value=list(members)), \
                patch("gateway.pools.routing_policy", return_value=None), \
                patch("gateway.pool_cooldowns.until", return_value=None), \
                patch("gateway.pool_cooldowns.note_failure"):
            worker_module.execute_job(row, store)

        job = store.get(row["id"])
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["recovery_required"], "every member answered; nothing is outstanding")
        self.assertNotEqual(job["recovery_state"], "unknown_outcome")
        self.assertEqual([provider.calls for provider in members.values()], [1, 1],
                         "each member was asked once, then the pool ended")


class SharedRuleTests(unittest.TestCase):
    """The one rule both helpers state, asserted where it lives rather than at a call site."""

    def test_a_settled_refusal_says_so(self) -> None:
        error = unusable_answer("p", "it is empty")
        self.assertTrue(error.retryable)
        self.assertIs(error.upstream_accepted, True)
        self.assertIs(error.upstream_settled, True, "the worker's unknown-outcome gate reads this")
        self.assertIn("it is empty", str(error))

    def test_answer_json_refuses_a_body_it_cannot_read(self) -> None:
        import httpx

        response = httpx.Response(200, content=HTML_ERROR, headers={"content-type": "text/html"})
        with self.assertRaises(Exception) as caught:
            answer_json(response, "p")
        self.assertIn("not JSON", str(caught.exception))

    def test_answer_json_returns_a_mapping_for_a_non_object_body(self) -> None:
        import httpx

        for body in (b"null", b"[1, 2]", b'{"choices": []}'):
            with self.subTest(body):
                response = httpx.Response(200, content=body,
                                          headers={"content-type": "application/json"})
                self.assertIsInstance(answer_json(response, "p"), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
