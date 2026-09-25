#!/usr/bin/env python3
"""The Hub's chat lane: what the gateway sends, and what it does with the answer.

Studio Hub 0.6.63 opened `POST /studio/chat/v1/chat/completions` to a **Fleet**
credential, which is what makes a Hub LLM dispatchable by this gateway at all —
before it, every chat route on a Hub answered 403 to a fleet token, so the lane
could not exist here. This file proves the half the gateway owns, and it asserts
**on the wire** (what the mock's chat route actually received) rather than on the
adapter's return value, because the contract is about the request:

  * the route is the Hub's **peer** surface, through the Hub, so admission, the
    machine lease and worker signing apply — not a worker's own route, and not the
    Hub's `/api/hub/chat/completions`, which is Hub-token only;
  * `stream` is false: this adapter's contract is one request for one answer;
  * only the parameters that lane honours are sent, because a parameter outside
    its allowlist is dropped at the far end — a promise the answer would not keep;
  * `finish_reason` and `usage` come back as the worker stated them, and the
    worker's own generation time is a different fact from the round trip.

Run with::

    .venv/bin/python tests/test_hub_chat_lane.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-hub-chat-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

import mock_upstream  # noqa: E402
from mock_upstream import CHAT_CALLS, Handler, Server  # noqa: E402

from gateway.adapters import serves  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.adapters.studio_hub import StudioHubBackend  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)


def _request(params: dict | None = None) -> BackendRequest:
    return BackendRequest(
        provider_id="studio-hub-mock",
        model_id="mock-hub/chat-model",
        modality="chat",
        params=params if params is not None else {"input": [{"role": "user", "content": "hi"}]},
        values={"hub_token": "fleet-token-abc"},
        base_url=BASE,
        display_name="Mock Hub",
    )


class ChatLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        global BASE
        BASE = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        CHAT_CALLS.clear()

    def test_the_lane_is_served_now(self) -> None:
        # `serves` is the one gate the catalogue reads; chat missing from it was
        # why a Hub LLM could be discovered and never offered.
        definition = ProviderDefinition(
            id="studio-hub-mock", display_name="Mock Hub", category="multi",
            kind=KIND_STUDIO_HUB, base_url=BASE,
            modalities=("chat", "image", "tts", "music", "transcription"),
            discovery=DISCOVERY_STUDIO_HUB,
        )
        self.assertTrue(serves(definition, "chat"))

    def test_the_call_goes_through_the_hub_with_only_honoured_parameters(self) -> None:
        result = StudioHubBackend().generate(_request({
            "input": [{"role": "user", "content": "hello"}],
            "temperature": 0.3,
            "top_p": 0.9,
            "max_output_tokens": 128,
            "seed": 7,
            "thinking": False,
            "response_format": {"type": "json_object"},
            # Not on the lane's allowlist: the worker would drop it, so sending it
            # would tell the caller something the answer cannot honour.
            "frequency_penalty": 0.5,
        }))
        self.assertEqual(len(CHAT_CALLS), 1, CHAT_CALLS)
        call = CHAT_CALLS[0]
        self.assertEqual(call["path"], "/studio/chat/v1/chat/completions")
        # The credential presented is the provider's own — a Fleet token, which is
        # the whole point of 0.6.63 for this lane.
        self.assertEqual(call["token"], "fleet-token-abc")
        body = call["body"]
        self.assertFalse(body["stream"], "this adapter is one request for one answer")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hello"}])
        for key, value in (
            ("temperature", 0.3), ("top_p", 0.9), ("max_tokens", 128), ("seed", 7),
            ("thinking", False),
        ):
            self.assertEqual(body.get(key), value, key)
        self.assertEqual(body.get("response_format"), {"type": "json_object"})
        self.assertNotIn("frequency_penalty", body, "a parameter the lane drops is not sent")
        self.assertIn("mock hub reply", result.text)

    def test_the_answer_is_passed_through_as_the_worker_stated_it(self) -> None:
        result = StudioHubBackend().generate(_request())
        # `finish_reason` travels: rewriting `length` into a clean stop would hide
        # a truncated answer from the one reader that refuses those.
        self.assertEqual(result.payload["finish_reason"], "stop")
        self.assertEqual(
            result.payload["usage"],
            {"prompt_tokens": 11, "completion_tokens": 13, "total_tokens": 24},
        )
        # The worker's own generation time, not this gateway's round trip.
        self.assertEqual(result.payload["generation_seconds"], 2.5)

    def test_a_hub_that_cannot_serve_the_model_is_a_location_refusal(self) -> None:
        # `MODEL_NOT_PACKAGED` / `MODEL_NOT_READY` are *site* properties: the Hub
        # named the reason, accepted nothing, and the worker may safely try the next
        # location that offers the same model — so this is `LOCATION_NOT_READY`, not
        # a failed job and not an unknown-outcome hold.
        with patch.object(mock_upstream, "CHAT_STATUS", 409), \
                patch.object(mock_upstream, "CHAT_REFUSAL_CODE", "MODEL_NOT_PACKAGED"), \
                patch.object(mock_upstream, "CHAT_DETAIL", "the model is not packaged here"):
            with self.assertRaises(BackendError) as caught:
                StudioHubBackend().generate(_request())
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertTrue(caught.exception.retryable)
        self.assertIs(caught.exception.upstream_accepted, False)

    def test_a_definite_refusal_is_never_an_unknown_hold(self) -> None:
        # Their 422 names the field it refused (a prompt past the model's stated
        # context, or a non-boolean `thinking`). It is the Hub's own decision, so
        # the job settles as failed rather than becoming durable recovery work.
        with patch.object(mock_upstream, "CHAT_STATUS", 422), \
                patch.object(mock_upstream, "CHAT_DETAIL",
                             "prompt is 9000 tokens, the model's stated context is 8192"):
            with self.assertRaises(BackendError) as caught:
                StudioHubBackend().generate(_request())
        self.assertEqual(caught.exception.status, 422)
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertIn("8192", str(caught.exception))

    def test_a_5xx_may_have_run_and_stays_ambiguous(self) -> None:
        with patch.object(mock_upstream, "CHAT_STATUS", 503):
            with self.assertRaises(BackendError) as caught:
                StudioHubBackend().generate(_request())
        self.assertTrue(caught.exception.retryable)
        self.assertIsNone(caught.exception.upstream_accepted)

    def test_an_empty_answer_is_a_failure_not_a_blank_success(self) -> None:
        with patch.object(mock_upstream, "CHAT_EMPTY", True):
            with self.assertRaises(BackendError) as caught:
                StudioHubBackend().generate(_request())
        self.assertIn("empty", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
