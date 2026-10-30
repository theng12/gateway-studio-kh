#!/usr/bin/env python3
"""A published output allowance is a number the model can actually answer within.

**Measured live 2026-10-30.** `studio-hub::mlx-community/gemma-4-26B-A4B-it-qat-4bit`
published `context_window_tokens: 32768` beside `max_output_tokens: 128000`. The 128,000
was the Hub's own worker fallback (`llm_engine.FALLBACK_MAX_OUTPUT_TOKENS`), which its
lane contract says in as many words "is not a number the model states". A model cannot
produce more tokens than fit in its window, so the published allowance is the smaller of
the two — the model's own published context, not an invented number — and
`max_output_tokens_capped_from` names the figure that was withheld.

The same chooser is what the generation path sends (`catalog.published_output_cap`), so
the row and the request cannot describe two different allowances. On the Hub lane a
caller's own number above that allowance is lowered before it is sent and reported on the
job (`max_tokens_lowered_to`) instead of earning the Hub's refusal after it measured the
prompt.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402

from gateway import catalog  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.adapters.base import BackendRequest  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402

#: The live row's own two statements, verbatim in shape: the manifest's window and the worker's
#: fallback output budget, each with the sentence the Hub publishes beside it.
LIVE_LIMITS = {
    "context_window_tokens": {
        "value": 32768,
        "source": "the model manifest's qualification.context_window_tokens",
        "source_kind": "curated",
    },
    "max_output_tokens": {
        "value": 128000,
        "source": "worker default: model/generation_config.json states no max_new_tokens",
        "source_kind": "worker_default",
    },
}

LIVE_MODEL = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"


def _definition() -> ProviderDefinition:
    return ProviderDefinition(
        id="studio-hub-2c7a2403564b", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=("chat",),
        discovery="static",
    )


def _row(limits: dict | None) -> dict:
    raw = {"model_limits": limits} if limits is not None else {}
    return catalog._chat_row(LIVE_MODEL, _definition(), {"raw": raw})


class ThePublishedAllowanceCannotExceedTheWindowTest(unittest.TestCase):
    def test_the_live_row_publishes_the_window_as_its_output_allowance(self) -> None:
        row = _row(LIVE_LIMITS)
        self.assertEqual(row["context_window_tokens"], 32768)
        self.assertEqual(row["max_output_tokens"], 32768)
        self.assertEqual(row["max_output_tokens_capped_from"], 128000,
                         "the figure that was withheld is named, not silently dropped")
        self.assertEqual(row["max_output_tokens_source"], "worker_default",
                         "whose number the stated one was is still published")

    def test_the_generation_path_reads_the_same_answer(self) -> None:
        model = {"model_id": LIVE_MODEL, "raw": {"model_limits": LIVE_LIMITS}}
        self.assertEqual(
            32768, catalog.published_output_cap(_definition(), LIVE_MODEL, model),
            "the request must never carry an allowance the row does not publish",
        )

    def test_an_allowance_inside_the_window_is_left_exactly_as_it_was(self) -> None:
        row = _row({
            "context_window_tokens": {"value": 262144, "source": "model config",
                                      "source_kind": "model"},
            "max_output_tokens": {"value": 32768, "source": "model config",
                                  "source_kind": "model"},
        })
        self.assertEqual(row["max_output_tokens"], 32768)
        self.assertNotIn("max_output_tokens_capped_from", row,
                         "an ordinary row is byte-identical to before")

    def test_a_window_nobody_stated_caps_nothing(self) -> None:
        row = _row({"max_output_tokens": {"value": 128000, "source": "worker default",
                                          "source_kind": "worker_default"}})
        self.assertEqual(row["max_output_tokens"], 128000)
        self.assertIsNone(row["context_window_tokens"])
        self.assertNotIn("max_output_tokens_capped_from", row)


def _request(published_cap: int | None, **params: object) -> BackendRequest:
    return BackendRequest(
        provider_id="studio-hub-2c7a2403564b", model_id=LIVE_MODEL, modality="chat",
        params={"messages": [{"role": "user", "content": "research"}], **params},
        values={"hub_token": "t", "base_url": "http://mock"},
        base_url="http://mock", max_output_tokens=published_cap,
    )


class TheHubLaneSendsOnlyWhatItCanAcceptTest(unittest.TestCase):
    def test_a_caller_above_the_published_allowance_is_lowered_and_told(self) -> None:
        notes: dict = {}
        body = studio_hub._chat_body(_request(32768, max_tokens=128000), notes)
        self.assertEqual(body["max_tokens"], 32768)
        self.assertEqual(notes["max_tokens_lowered_to"], 32768)
        self.assertEqual(notes["max_tokens_asked"], 128000)

    def test_a_caller_inside_the_allowance_is_sent_verbatim(self) -> None:
        notes: dict = {}
        body = studio_hub._chat_body(_request(32768, max_completion_tokens=2000), notes)
        self.assertEqual(body["max_tokens"], 2000)
        self.assertEqual(notes, {})

    def test_a_caller_that_names_nothing_still_sends_nothing(self) -> None:
        notes: dict = {}
        body = studio_hub._chat_body(_request(32768), notes)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(notes, {})

    def test_a_lane_with_no_published_allowance_never_invents_a_cap(self) -> None:
        body = studio_hub._chat_body(_request(None, max_tokens=128000))
        self.assertEqual(body["max_tokens"], 128000,
                         "with no published number there is nothing to lower to")

    def test_the_lowered_value_is_reported_on_the_jobs_result(self) -> None:
        """What was sent travels back on the job, not only into the request body."""
        answer = httpx.Response(200, json={
            "model": LIVE_MODEL,
            "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2},
        })
        sent: list[dict] = []

        def post(url, *, headers=None, json=None, timeout=None):
            sent.append(json)
            return answer

        request = _request(32768, max_tokens=128000)
        with patch.object(studio_hub.httpx, "post", post), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            result = studio_hub._generate_chat(request)
        self.assertEqual(sent[0]["max_tokens"], 32768)
        self.assertEqual(result.payload["max_tokens_lowered_to"], 32768)
        self.assertEqual(result.payload["max_tokens_asked"], 128000)


if __name__ == "__main__":
    unittest.main()
