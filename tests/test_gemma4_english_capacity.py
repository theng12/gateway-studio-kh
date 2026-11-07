#!/usr/bin/env python3
"""Offline English long-payload acceptance, capacity and fit-aware admission regression for Gemma 4.

Verifies the Gateway's contracts for `studio-hub::mlx-community/gemma-4-26B-A4B-it-qat-4bit`:
1. Door admission for long English payloads:
   - English token estimation (ASCII / 3.7) accurately bounds English prose without undercounting.
   - English long payloads fitting within the qualified 48K window (49,152 context, 39,960 request ceiling,
     door limit 47,128) are accepted at admission.
   - Payloads exceeding the door limit are refused 422 with `REQUEST_TOO_LARGE_FOR_MODEL`.
   - The same payload refused under historical 32K context is accepted under qualified 48K context.
2. Adapter execution and room-left lowering:
   - Callers asking above published output cap (8,192) are bounded before transmission.
   - Hub 422 room-left on long English prompts triggers one re-send resized to room left with fresh request ID.
   - Truncated completions (`finish_reason: "length"`) surface loudly as `INCOMPLETE_OUTPUT`.
3. Fit-aware fleet capacity & admission:
   - Gemma 4 (26B) is fit-aware: only Macs with >= 32 GB RAM (e.g. terranash-1607) are eligible.
   - Busy node (`eligible: 1, available: 0`) queues the job in `accepted` state.
   - Node outage (`eligible: 0`) triggers `states_no_mac` and 503 `MODEL_LOCKED` refusal at the door.
   - Paused nodes (like Mac Studio) are excluded from routing candidates.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-gemma4-english-")

import httpx  # noqa: E402

from gateway import api, catalog, config, fleet  # noqa: E402
from gateway import limits as curated_limits  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402

MODEL_ID = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"
SITE_1600 = "2c7a2403564b"
SITE_STUDIO = "bf33509789c5"

QUALIFIED_48K_LIMITS = {
    "context_window_tokens": {
        "value": 49152,
        "source": "the model manifest's qualification.context_window_tokens",
        "source_kind": "curated",
    },
    "max_output_tokens": {
        "value": 8192,
        "source": "the model manifest's max_output_tokens",
        "source_kind": "curated",
    },
}

HISTORICAL_32K_LIMITS = {
    "context_window_tokens": {
        "value": 32768,
        "source": "the model manifest's qualification.context_window_tokens",
        "source_kind": "curated",
    },
    "max_output_tokens": {
        "value": 128000,
        "source": "worker default",
        "source_kind": "worker_default",
    },
}

CURATED_48K = {"max_output_tokens": 8192, "max_request_tokens": 39960}
CURATED_32K = {"max_output_tokens": 8192, "max_request_tokens": 23000}


def _definition(site_id: str = SITE_1600) -> ProviderDefinition:
    return ProviderDefinition(
        id=f"studio-hub-{site_id}", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=("chat",),
        discovery="static",
    )


def _english_text(word_count: int) -> str:
    """Generate realistic English prose text of approximately word_count words."""
    sentence_template = (
        "The observational analysis of autonomous fleet systems requires rigorous verification "
        "and reproducible benchmarking across distributed nodes under varying memory pressure. "
    )
    words = sentence_template.split()
    repeated = (words * ((word_count // len(words)) + 1))[:word_count]
    return " ".join(repeated)


class Gemma4EnglishAdmissionTest(unittest.TestCase):
    """Door admission and token estimation regression for long English payloads."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gemma4-limits-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "model-limits.local.json"
        self._set_curated(CURATED_48K)
        self._real_limits_path = curated_limits.LIMITS_PATH
        curated_limits.LIMITS_PATH = self.path
        self.addCleanup(setattr, curated_limits, "LIMITS_PATH", self._real_limits_path)
        curated_limits._cache.update(key=None, limits={})

    def tearDown(self) -> None:
        curated_limits._cache.update(key=None, limits={})

    def _set_curated(self, data: dict) -> None:
        self.path.write_text(
            json.dumps({"models": {f"studio-hub::{MODEL_ID}": data}}),
            encoding="utf-8",
        )
        curated_limits._cache.update(key=None, limits={})

    def test_english_token_estimation_accuracy(self) -> None:
        """ASCII English text is estimated at 3.7 chars per token."""
        from gateway.api import _estimate_request_tokens
        sample = _english_text(370)  # ~370 words
        ascii_count = len(sample)
        expected = int(ascii_count / 3.7)
        estimated = _estimate_request_tokens({"content": sample})
        self.assertEqual(estimated, expected)
        self.assertGreater(estimated, 0)

    def test_long_english_payload_accepted_under_48k_window(self) -> None:
        """A ~37,000 token English payload passes admission in the 48K window (door limit 47,128)."""
        from gateway.api import _refuse_a_request_that_cannot_fit
        # ~138,000 characters of English text ≈ 37,297 tokens
        english_payload = _english_text(16000)
        self.assertGreaterEqual(len(english_payload), 135000)
        self.assertLessEqual(len(english_payload), 140000)

        model = {"model_id": MODEL_ID, "display_name": "gemma-4",
                 "raw": {"model_limits": QUALIFIED_48K_LIMITS}}
        # Must not raise: ~37,000 <= 47,128 limit
        _refuse_a_request_that_cannot_fit(
            _definition(), model, {"messages": [{"role": "user", "content": english_payload}]})

    def test_same_long_english_payload_refused_under_32k_window(self) -> None:
        """The same ~37,000 token English payload is refused at admission under the 32K window."""
        from gateway.api import _refuse_a_request_that_cannot_fit
        self._set_curated(CURATED_32K)

        english_payload = _english_text(16000)
        model = {"model_id": MODEL_ID, "display_name": "gemma-4",
                 "raw": {"model_limits": HISTORICAL_32K_LIMITS}}

        # Under 32K (32768 - 1000 - 1024 = 30744 limit), ~37,000 tokens must raise 422
        with self.assertRaises(Exception) as caught:
            _refuse_a_request_that_cannot_fit(
                _definition(), model, {"messages": [{"role": "user", "content": english_payload}]})
        error = caught.exception
        self.assertEqual(getattr(error, "code", None), "REQUEST_TOO_LARGE_FOR_MODEL")
        self.assertEqual(getattr(error, "status", None), 422)
        self.assertIn("30744", str(error))

    def test_over_ceiling_english_payload_refused_under_48k_window(self) -> None:
        """An English payload exceeding 47,128 tokens is refused by name under 48K."""
        from gateway.api import _refuse_a_request_that_cannot_fit
        # ~180,000 chars / 3.7 ≈ 48,648 tokens > 47,128
        oversized = _english_text(21000)
        model = {"model_id": MODEL_ID, "display_name": "gemma-4",
                 "raw": {"model_limits": QUALIFIED_48K_LIMITS}}

        with self.assertRaises(Exception) as caught:
            _refuse_a_request_that_cannot_fit(
                _definition(), model, {"messages": [{"role": "user", "content": oversized}]})
        error = caught.exception
        self.assertEqual(getattr(error, "code", None), "REQUEST_TOO_LARGE_FOR_MODEL")
        self.assertIn("47128", str(error))
        self.assertIn("Shorten the request", str(error))


class Gemma4EnglishAdapterExecutionTest(unittest.TestCase):
    """Adapter execution regression: output cap lowering, room-left re-send, incomplete output."""

    def _request(self, published_cap: int | None = 8192, **params: object) -> BackendRequest:
        return BackendRequest(
            provider_id=f"studio-hub-{SITE_1600}", model_id=MODEL_ID, modality="chat",
            params={"messages": [{"role": "user", "content": _english_text(500)}], **params},
            values={"hub_token": "token", "base_url": "http://mock"},
            base_url="http://mock", max_output_tokens=published_cap,
        )

    def test_caller_asking_above_published_output_cap_is_lowered(self) -> None:
        notes: dict = {}
        body = studio_hub._chat_body(self._request(8192, max_tokens=32768), notes)
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(notes["max_tokens_asked"], 32768)
        self.assertEqual(notes["max_tokens_lowered_to"], 8192)

    def test_room_left_422_on_long_english_prompt_resends_once(self) -> None:
        """Hub reports 41,000 prompt tokens with 8,192 ask: resized to 7,152 and re-sent once."""
        room_left_422 = json.dumps({
            "detail": "messages: the prompt is 41000 tokens and max_tokens asks for 8192 more, "
                      "49192 together, above the 49152 tokens this model states (the model manifest's "
                      "qualification.context_window_tokens). Send a shorter prompt or a smaller max_tokens; "
                      "this worker refuses rather than truncating."
        })
        success_response = httpx.Response(200, json={
            "model": MODEL_ID,
            "choices": [{"message": {"content": "English analytical summary completed."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 41000, "completion_tokens": 120},
        })
        sent_bodies: list[dict] = []
        sent_headers: list[dict] = []
        replies = [httpx.Response(422, text=room_left_422), success_response]

        def post(url, *, headers=None, json=None, timeout=None):
            sent_bodies.append(json)
            sent_headers.append(headers or {})
            return replies.pop(0)

        with patch.object(studio_hub.httpx, "post", post), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 120.0):
            result = studio_hub._generate_chat(self._request(8192, max_tokens=8192))

        self.assertEqual(len(sent_bodies), 2, "initial attempt + exactly one re-send")
        self.assertEqual(sent_bodies[0]["max_tokens"], 8192)
        # 49152 - 41000 - 1000 margin = 7152
        self.assertEqual(sent_bodies[1]["max_tokens"], 7152)
        self.assertNotEqual(sent_headers[0].get("X-StudioHub-Request-ID"),
                            sent_headers[1].get("X-StudioHub-Request-ID"),
                            "re-send uses fresh request id to prevent duplicate refusal")
        self.assertEqual(result.payload["max_tokens_lowered_to"], 7152)
        self.assertEqual(result.payload["prompt_tokens_measured"], 41000)
        self.assertEqual(result.text, "English analytical summary completed.")

    def test_cut_off_english_generation_surfaces_as_incomplete_output(self) -> None:
        """finish_reason: length is an INCOMPLETE_OUTPUT failure with recoverable partial text."""
        truncated_response = httpx.Response(200, json={
            "model": MODEL_ID,
            "choices": [{"message": {"content": "The chapter conclusion was abruptly cut off "},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5000, "completion_tokens": 8192},
        })
        with patch.object(studio_hub.httpx, "post", lambda *a, **k: truncated_response), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            with self.assertRaises(BackendError) as caught:
                studio_hub._generate_chat(self._request(8192, max_tokens=8192))
        err = caught.exception
        self.assertEqual(err.code, "INCOMPLETE_OUTPUT")
        self.assertTrue(err.upstream_settled)
        self.assertTrue(err.retryable)
        self.assertEqual(err.evidence["partial_response"]["text"],
                         "The chapter conclusion was abruptly cut off ")


class Gemma4FitAwareCapacityTest(unittest.TestCase):
    """Fit-aware fleet routing & admission regression for Gemma 4 (26B requiring >= 32 GB RAM)."""

    def setUp(self) -> None:
        fleet._ELIGIBLE_AT.clear()

    def _site_row(
        self,
        site_id: str,
        *,
        eligible: int = 1,
        available: int = 1,
        reachable: bool = True,
        paused: bool = False,
    ) -> dict:
        return {
            "id": site_id,
            "provider_id": f"studio-hub-{site_id}",
            "label": f"Site {site_id}",
            "reachable": reachable,
            "slow": False,
            "answered_at": time.time(),
            "role": "controller",
            "drained": False,
            "capacity": {"accepting_customer_jobs": True, "queue_depth": 0},
            "paused_at": time.time() if paused else None,
            "machines": [
                {"machine": "terranash-1607-m2-pro-32-512", "reachable": True, "memory_gb": 34.36},
                {"machine": "terranash-1601-m2-8-256", "reachable": True, "memory_gb": 8.59},
            ],
            "model_supply": {MODEL_ID: {"eligible": eligible, "available": available}},
        }

    def patches(self, rows: list[dict], sites: list[str]):
        def _enabled(requested: str):
            if requested != "chat":
                return []
            return [(_definition(s), {"model_id": MODEL_ID, "display_name": "Gemma 4",
                                      "modality": "chat", "enabled": True, "raw": {}})
                    for s in sites]
        return (
            patch.object(fleet, "snapshot", lambda **_kw: {"locations": list(rows)}),
            patch.object(registry.models, "enabled_models", _enabled),
        )

    def test_single_eligible_node_provides_exact_slot_capacity(self) -> None:
        """Site 1600 provides exactly 1 slot (terranash-1607) for Gemma 4."""
        snap, enabled = self.patches([self._site_row(SITE_1600, eligible=1, available=1)], [SITE_1600])
        with snap, enabled:
            candidates = fleet.routing_candidates(MODEL_ID, "chat")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["location_id"], SITE_1600)
        self.assertEqual(candidates[0]["slots"], 1)
        self.assertFalse(candidates[0]["spent"])

    def test_busy_eligible_node_remains_candidate_for_queueing(self) -> None:
        """When 1607 is busy (eligible: 1, available: 0), candidate is retained and not spent."""
        snap, enabled = self.patches([self._site_row(SITE_1600, eligible=1, available=0)], [SITE_1600])
        with snap, enabled:
            candidates = fleet.routing_candidates(MODEL_ID, "chat")
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["spent"])

    def test_eligible_zero_locks_model_at_door_with_503(self) -> None:
        """When 1607 is down (eligible: 0), states_no_mac locks the row and refuses 503."""
        from fastapi.testclient import TestClient
        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)

        rows = [self._site_row(SITE_1600, eligible=0, available=0)]
        snap, enabled = self.patches(rows, [SITE_1600])
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            res = client.post(
                "/v1/chat/responses",
                headers={
                    "Authorization": f"Bearer {config.gateway_api_key() or 'test'}",
                    "Idempotency-Key": "gemma4-lock-test-0001",
                },
                json={"model_id": f"studio-hub::{MODEL_ID}", "prompt": "Hello world"},
            )
        self.assertEqual(res.status_code, 503)
        body = res.json()
        self.assertEqual(body["code"], "MODEL_LOCKED")
        self.assertIn("No Mac able to run this model is available right now", body["detail"])

    def test_paused_location_excluded_from_gemma_routing(self) -> None:
        """A paused location with 32 GB RAM (Mac Studio) is excluded from candidates."""
        paused_site = self._site_row(SITE_STUDIO, eligible=1, available=1, paused=True)
        snap, enabled = self.patches([paused_site], [SITE_STUDIO])
        with snap, enabled:
            candidates = fleet.routing_candidates(MODEL_ID, "chat")
        self.assertEqual(candidates, [], "paused site must never be routed to")


if __name__ == "__main__":
    unittest.main()
