#!/usr/bin/env python3
"""The Gateway forwards the caller's expected answer length, and never times out first.

Two halves, both of them wire facts rather than adapter return values.

**One: the expectation reaches the Hub.** Studio Hub sizes each Chat job's deadline from the job
(`components/hub/backend/chat_deadline.py`), and it cannot tell a 100-prompt scene pack from a
one-line question by reading the body — so Story Studio's own number has to travel with the
request. It travels as `X-StudioHub-Expected-Output-Tokens`, a header rather than a body field,
because the Hub forwards the body verbatim to the chat worker and the worker refuses a field it
does not know.

**Two: nothing here cuts the job short.** A chat call is one HTTP request that lasts as long as
the model runs. The Hub may hold it for four hours (its own ceiling), so a 300 s read timeout —
which is what this gateway had on its OpenAI-compatible chat lane — would have cut a 100-prompt
scene pack fifteen minutes in. The Hub's published `deadline_seconds` is a *floor* (1,800 s for
the lane), not the number, so bounding this call by it would still have cut the same job.

Run with::

    .venv/bin/python tests/test_chat_deadline_forwarding.py
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

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-chat-deadline-"))
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

from gateway.adapters import openai_compat, studio_hub  # noqa: E402
from gateway.adapters.base import (  # noqa: E402
    CHAT_DEADLINE_CEILING_S,
    BackendRequest,
    caller_expected_output_tokens,
    expected_output_header,
)
from gateway.adapters.studio_hub import StudioHubBackend  # noqa: E402

#: The Hub's own computed limit for the job this change exists for: a 100-prompt scene pack
#: (100 x 300 expected output tokens, a ~12,000-token prompt) on the fleet's slowest measured
#: Mac, an M1 at 12.0 tok/s — 15 s load + 200 s prefill + 2,500 s decode, x 1.5 safety.
HUNDRED_PROMPT_PACK_S = 4_072.0

BASE = ""


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


class ExpectationReachesTheHubTests(unittest.TestCase):
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

    def _header(self, params: dict | None = None) -> str | None:
        before = len(CHAT_CALLS)
        StudioHubBackend().generate(_request(params))
        self.assertEqual(len(CHAT_CALLS), before + 1, "the lane must send exactly one POST")
        return CHAT_CALLS[-1]["headers"].get("X-StudioHub-Expected-Output-Tokens")

    def test_the_callers_expectation_is_forwarded_to_the_hub(self) -> None:
        params = {"input": [{"role": "user", "content": "scene pack"}],
                  "expected_output_tokens": 30_000}
        self.assertEqual(self._header(params), "30000")

    def test_no_expectation_is_not_an_empty_header(self) -> None:
        """An absent expectation is absent. Sending an empty one would be a number the Hub has
        to refuse, where saying nothing lets it use the fleet's own measured answer size."""
        self.assertIsNone(self._header())

    def test_a_malformed_expectation_is_not_forwarded(self) -> None:
        for junk in ("many", 0, -1, True, 1.5, None):
            self.assertIsNone(
                self._header({"input": [{"role": "user", "content": "hi"}],
                              "expected_output_tokens": junk}),
                f"{junk!r} is not a token count")

    def test_a_numeric_string_is_accepted(self) -> None:
        """Story Studio's own settings travel as strings in places; a numeric one is a number."""
        self.assertEqual(
            self._header({"input": [{"role": "user", "content": "hi"}],
                          "expected_output_tokens": "30000"}), "30000")

    def test_the_expectation_is_never_put_in_the_body(self) -> None:
        """The Hub forwards this body verbatim to the chat worker, which refuses a field it does
        not know — so the hint must not be a body field."""
        self._header({"input": [{"role": "user", "content": "hi"}],
                      "expected_output_tokens": 30_000})
        self.assertNotIn("expected_output_tokens", CHAT_CALLS[-1]["body"])

    def test_the_openai_compatible_lane_forwards_it_too(self) -> None:
        """A Hub reached through the OpenAI-compatible lane needs the same header: that lane is
        the other way this gateway reaches a Hub, and it carried the flat 300 s timeout."""
        self.assertEqual(
            expected_output_header({"expected_output_tokens": 12_345}),
            {"X-StudioHub-Expected-Output-Tokens": "12345"})
        self.assertEqual(expected_output_header({}), {})


class TheGatewayDoesNotTimeOutFirstTests(unittest.TestCase):
    """Every bound this gateway puts on a Hub chat call must outlast the Hub's own ceiling."""

    def _chat_request(self, **params) -> BackendRequest:
        return _request({"input": [{"role": "user", "content": "hi"}], **params})

    def test_the_hub_chat_lane_outwaits_the_hub_ceiling_not_its_published_floor(self) -> None:
        """The Hub's contract states the lane's floor (1,800 s) and computes the real deadline per
        job. Bounding this call by the contract would cut the 100-prompt pack at 1,800 s of a
        4,072 s job — so the chat lane is bounded by the ceiling instead."""
        with patch.object(studio_hub, "lane_deadline_seconds", wraps=studio_hub.lane_deadline_seconds):
            lane = studio_hub.lane_deadline_seconds(self._chat_request())
        self.assertGreaterEqual(lane, HUNDRED_PROMPT_PACK_S)
        self.assertEqual(lane, CHAT_DEADLINE_CEILING_S)

    def test_the_openai_compatible_chat_timeout_outlasts_the_hub(self) -> None:
        """This was `httpx.Timeout(300.0, ...)` — fifteen minutes into a job that needs an hour."""
        self.assertGreaterEqual(openai_compat.CHAT_TIMEOUT.read, HUNDRED_PROMPT_PACK_S)
        self.assertGreaterEqual(openai_compat.CHAT_TIMEOUT.read, CHAT_DEADLINE_CEILING_S)
        self.assertGreaterEqual(openai_compat.CHAT_TIMEOUT.connect, 10.0)

    def test_a_lane_that_states_nothing_still_gets_the_flat_fallback(self) -> None:
        """`JOB_DEADLINE_S` is the right answer for a Hub from before the per-lane table, and this
        change must not have raised it for every lane on the way past."""
        self.assertEqual(studio_hub.lane_deadline_seconds(None), studio_hub.JOB_DEADLINE_S)

    def test_a_non_chat_lane_keeps_its_own_number(self) -> None:
        image = BackendRequest(
            provider_id="studio-hub-mock", model_id="mock-hub/image-model", modality="image",
            params={}, values={"hub_token": "t"}, base_url="http://127.0.0.1:1",
            display_name="Mock Hub")
        self.assertLess(studio_hub.lane_deadline_seconds(image), CHAT_DEADLINE_CEILING_S)

    def test_the_chooser_refuses_what_the_hub_would_refuse(self) -> None:
        self.assertEqual(caller_expected_output_tokens({"expected_output_tokens": 10}), 10)
        for junk in ("many", 0, -3, True, 2.5, None, float("inf")):
            self.assertIsNone(caller_expected_output_tokens({"expected_output_tokens": junk}),
                              repr(junk))


if __name__ == "__main__":
    unittest.main()
