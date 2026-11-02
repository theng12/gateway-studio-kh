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
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402

from gateway import catalog  # noqa: E402
from gateway import limits as curated_limits  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
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


def _row(limits: dict | None, **raw_extra: object) -> dict:
    raw: dict = {"model_limits": limits} if limits is not None else {}
    raw.update(raw_extra)
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


#: The Hub's own 422 of 2026-11-02, verbatim: job 6e184889 asked for 40,000 output tokens on
#: gemma-4, the Gateway had already bounded it to 32,768, and the Hub measured a 13,604-token
#: prompt and refused the pair before producing a single token.
HUB_ROOM_LEFT_422 = (
    '{"detail":"messages: the prompt is 13604 tokens and max_tokens asks for 32768 more, '
    '46372 together, above the 32768 tokens this model states (the model manifest\'s '
    'qualification.context_window_tokens). Send a shorter prompt or a smaller max_tokens; '
    'this worker refuses rather than truncating."}'
)

#: The *other* sentence the same function raises, when the prompt alone will not fit. It is a
#: refusal about the prompt, not about the pair, so nothing may be resized.
HUB_PROMPT_ONLY_422 = (
    '{"detail":"messages: the prompt is 40000 tokens, above the 32768 tokens this model states '
    '(the model manifest\'s qualification.context_window_tokens). Send a shorter prompt; this '
    'worker refuses rather than truncating."}'
)


def _answer(content: str = "done", finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "model": LIVE_MODEL,
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 13604, "completion_tokens": 100},
    })


def _refusal(text: str) -> httpx.Response:
    return httpx.Response(422, text=text)


class TheRoomLeftIsResentOnceTest(unittest.TestCase):
    """The Hub's own two counts resize the ask; the prompt-only refusal is never re-sent."""

    def test_a_room_left_refusal_is_resent_with_the_hub_s_own_figure(self) -> None:
        sent: list[dict] = []
        replies = [_refusal(HUB_ROOM_LEFT_422), _answer()]

        def post(url, *, headers=None, json=None, timeout=None):
            sent.append(json)
            return replies.pop(0)

        with patch.object(studio_hub.httpx, "post", post), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            result = studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertEqual(len(sent), 2, "the door refused, so one re-send is the whole story")
        # min(asked, window - prompt - margin) = min(32768, 32768 - 13604 - 1000) = 18164.
        self.assertEqual(sent[1]["max_tokens"], 18164)
        self.assertEqual(result.payload["max_tokens_lowered_to"], 18164)
        self.assertEqual(result.payload["max_tokens_asked"], 32768)
        self.assertEqual(result.payload["prompt_tokens_measured"], 13604)
        self.assertEqual(result.payload["max_tokens_lowered_reason"],
                         "above the room left after the prompt")
        self.assertEqual(result.text, "done")

    def test_the_caller_s_figure_is_kept_when_the_lane_had_already_lowered_it(self) -> None:
        """`max_tokens_asked` is what the *caller* asked, not what the Gateway last sent."""
        sent: list[dict] = []
        replies = [_refusal(HUB_ROOM_LEFT_422), _answer()]
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), replies.pop(0))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            # 128,000 asked, already lowered to the published 32,768 before it was sent.
            result = studio_hub._generate_chat(_request(32768, max_tokens=128000))
        self.assertEqual(sent[0]["max_tokens"], 32768)
        self.assertEqual(result.payload["max_tokens_asked"], 128000)
        self.assertEqual(result.payload["max_tokens_lowered_to"], 18164)

    def test_the_prompt_only_refusal_is_never_resent(self) -> None:
        """A prompt past the window is the caller's to shorten; a smaller ask cannot help."""
        sent: list[dict] = []
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), _refusal(HUB_PROMPT_ONLY_422))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            with self.assertRaises(BackendError) as caught:
                studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertEqual(len(sent), 1, "the prompt, not the pair, is what did not fit")
        self.assertEqual(caught.exception.code, "PROVIDER_FAILED")
        self.assertFalse(caught.exception.retryable)

    def test_a_room_too_thin_to_answer_in_is_a_plain_refusal_not_a_smaller_ask(self) -> None:
        """Window 32768, prompt 32000: 32768-32000-1000 = -232. No answer fits."""
        thin = HUB_ROOM_LEFT_422.replace("13604", "32000")
        sent: list[dict] = []
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), _refusal(thin))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            with self.assertRaises(BackendError) as caught:
                studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertEqual(len(sent), 1)
        self.assertIn("too little room for an answer", str(caught.exception))

    def test_a_sentence_no_figure_can_be_read_from_omits_max_tokens_rather_than_guessing(self) -> None:
        """The Hub measured the prompt but named no window, so no number may be invented."""
        vague = ('{"detail":"messages: the prompt is 13604 tokens and max_tokens asks for '
                 '32768 more, 46372 together, above the limit. Send a shorter prompt or a '
                 'smaller max_tokens."}')
        sent: list[dict] = []
        replies = [_refusal(vague), _answer()]
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), replies.pop(0))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            result = studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertNotIn("max_tokens", sent[1], "no figure, so the Hub picks the room left")
        self.assertNotIn("max_tokens_lowered_to", result.payload,
                         "a guessed number is never published")
        self.assertIn("omitted", result.payload["max_tokens_lowered_reason"])

    def test_a_re_sent_queued_job_uses_a_fresh_request_id(self) -> None:
        """The Hub queues an id at the door and marks it used even when it then refuses.

        `chat_priority.register` pops only `refused`/`expired`; a door refusal leaves the row
        `done`, so a re-send under the same id is answered `409 CHAT_REQUEST_DUPLICATE`.
        """
        ids: list[str] = []
        replies = [_refusal(HUB_ROOM_LEFT_422), _answer()]

        def post(url, *, headers=None, json=None, timeout=None):
            ids.append(headers.get("X-StudioHub-Request-ID"))
            return replies.pop(0)

        with patch.object(studio_hub.httpx, "post", post), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 120.0):
            studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2, "one id per attempt, or the re-send is a duplicate")

    def test_an_ordinary_422_is_still_quoted_and_never_resent(self) -> None:
        sent: list[dict] = []
        other = '{"detail":"thinking must be a boolean"}'
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), _refusal(other))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            with self.assertRaises(BackendError) as caught:
                studio_hub._generate_chat(_request(32768, max_tokens=32768))
        self.assertEqual(len(sent), 1)
        self.assertIn("thinking must be a boolean", str(caught.exception))


class ACutOffAnswerIsLoudTest(unittest.TestCase):
    """Measured 2026-11-02: job 04a617c2 settled `succeeded` with `finish_reason: length`."""

    def test_a_length_finish_is_a_failure_not_a_silent_success(self) -> None:
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: _answer("a truncated chapter", finish="length")), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            with self.assertRaises(BackendError) as caught:
                studio_hub._generate_chat(_request(32768, max_tokens=64))
        error = caught.exception
        self.assertEqual(error.code, "INCOMPLETE_OUTPUT")
        self.assertTrue(error.upstream_settled, "the Hub answered; nothing is left running")
        self.assertTrue(error.retryable, "a longer allowance would produce the whole answer")
        self.assertEqual(error.evidence["finish_reason"], "length")
        # The text that was produced travels, so a caller can salvage it.
        self.assertEqual(error.evidence["partial_response"]["text"], "a truncated chapter")
        self.assertEqual(error.evidence["partial_response"]["usage"]["completion_tokens"], 100)

    def test_a_clean_stop_is_untouched(self) -> None:
        with patch.object(studio_hub.httpx, "post", lambda url, **kw: _answer()), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            result = studio_hub._generate_chat(_request(32768, max_tokens=64))
        self.assertEqual(result.payload["finish_reason"], "stop")


class TheNotesReachTheJobTest(unittest.TestCase):
    """The adapter wrote them all along; `_job_envelope` never published them."""

    def test_a_lowered_allowance_is_on_the_envelope_the_caller_polls(self) -> None:
        from gateway.api import _job_envelope
        payload = {"text": "done",
                   "payload": {"finish_reason": "stop",
                               "max_tokens_asked": 128000,
                               "max_tokens_lowered_to": 8192,
                               "max_tokens_lowered_reason": "above the output allowance "
                                                            "this model publishes"}}
        job = {"id": "j1", "modality": "chat", "state": "succeeded", "model_id": "m",
               "params": "{}", "priority": "normal"}
        with patch("gateway.api._result_of", return_value=payload):
            body = _job_envelope(job)
        self.assertEqual(body["max_tokens_asked"], 128000)
        self.assertEqual(body["max_tokens_lowered_to"], 8192)
        self.assertIn("above the output allowance", body["max_tokens_lowered_reason"])

    def test_a_failed_job_carries_the_same_notes(self) -> None:
        from gateway.api import _job_envelope
        payload = {"partial_response": None,
                   "payload": {"finish_reason": "length",
                               "prompt_tokens_measured": 13604}}
        job = {"id": "j2", "modality": "chat", "state": "failed", "model_id": "m",
               "params": "{}", "priority": "normal", "error_code": "INCOMPLETE_OUTPUT",
               "error_detail": "stopped early"}
        with patch("gateway.api._result_of", return_value=payload):
            body = _job_envelope(job)
        self.assertEqual(body["prompt_tokens_measured"], 13604)

    def test_a_reshaped_job_says_what_the_hub_measured(self) -> None:
        """The notes are the same keys after a re-send, so one reader serves both."""
        from gateway.api import _job_envelope
        payload = {"text": "done",
                   "payload": {"finish_reason": "stop",
                               "max_tokens_asked": 32768,
                               "max_tokens_lowered_to": 18164,
                               "prompt_tokens_measured": 13604}}
        job = {"id": "j3", "modality": "chat", "state": "succeeded", "model_id": "m",
               "params": "{}", "priority": "normal"}
        with patch("gateway.api._result_of", return_value=payload):
            body = _job_envelope(job)
        self.assertEqual(body["max_tokens_lowered_to"], 18164)
        self.assertEqual(body["prompt_tokens_measured"], 13604)


#: The owner's curated figures for gemma-4, measured 2026-11-02: window 49152, publish a maximum
#: answer of 8192 and a maximum request of 39960. They live in the curated file, not in code —
#: `limits.py` reads `max_output_tokens` / `max_request_tokens` per model.
CURATED = {"max_output_tokens": 8192, "max_request_tokens": 39960}


class TheOwnerSCeilingBeatsTheWorkersFallbackTest(unittest.TestCase):
    """The Hub relays its own fallback as `max_output_tokens`; the owner's file answers over it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-curated-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "model-limits.local.json"
        self.path.write_text(json.dumps({"models": {f"studio-hub::{LIVE_MODEL}": CURATED}}),
                             encoding="utf-8")
        self._real_limits_path = curated_limits.LIMITS_PATH
        curated_limits.LIMITS_PATH = self.path
        self.addCleanup(setattr, curated_limits, "LIMITS_PATH", self._real_limits_path)
        curated_limits._cache.update(key=None, limits={})

    def tearDown(self) -> None:
        curated_limits._cache.update(key=None, limits={})

    def test_the_row_publishes_the_owner_s_ceiling_not_the_fallback(self) -> None:
        row = _row(LIVE_LIMITS)
        self.assertEqual(row["max_output_tokens"], 8192)
        self.assertEqual(row["max_output_tokens_source"], "curated")
        # The withheld figure is still named: the reader can see both numbers.
        self.assertNotIn("max_output_tokens_capped_from", row,
                         "8192 is inside the window, so nothing was capped away")

    def test_the_generation_path_reads_the_same_number(self) -> None:
        model = {"model_id": LIVE_MODEL, "raw": {"model_limits": LIVE_LIMITS}}
        self.assertEqual(catalog.published_output_cap(_definition(), LIVE_MODEL, model), 8192)

    def test_the_row_publishes_a_request_ceiling_with_its_source(self) -> None:
        row = _row(LIVE_LIMITS)
        self.assertEqual(row["max_request_tokens"], 39960)
        self.assertEqual(row["max_request_tokens_source"], "curated")

    def test_the_qualified_48k_window_and_request_are_published_together(self) -> None:
        qualified = {**LIVE_LIMITS, "context_window_tokens": {
            **LIVE_LIMITS["context_window_tokens"], "value": 49152}}
        row = _row(qualified)
        self.assertEqual(row["context_window_tokens"], 49152)
        self.assertEqual(row["max_output_tokens"], 8192)
        self.assertEqual(row["max_request_tokens"], 39960)
        self.assertEqual(49152 - 8192 - 1000, row["max_request_tokens"])
        model = {"model_id": LIVE_MODEL, "raw": {"model_limits": qualified}}
        from gateway.api import _refuse_a_request_that_cannot_fit
        # The old 32K admission would refuse this request; 48K leaves an answer.
        _refuse_a_request_that_cannot_fit(
            _definition(), model, {"content": "a" * 147852})
        with self.assertRaises(Exception) as caught:
            _refuse_a_request_that_cannot_fit(
                _definition(), model, {"content": "a" * 174400})
        self.assertEqual(caught.exception.code, "REQUEST_TOO_LARGE_FOR_MODEL")
        self.assertIn("47128", str(caught.exception))

    def test_48k_room_left_still_lowers_the_answer_and_reports_it(self) -> None:
        refusal = json.dumps({"detail":
            "messages: the prompt is 41000 tokens and max_tokens asks for 8192 more, "
            "49192 together, above the 49152 tokens this model states (the model manifest's "
            "qualification.context_window_tokens). Send a shorter prompt or a smaller max_tokens; "
            "this model cannot fit both."})
        sent = []
        replies = [_refusal(refusal), _answer()]
        with patch.object(studio_hub.httpx, "post",
                          lambda url, **kw: (sent.append(kw["json"]), replies.pop(0))[1]), \
                patch.object(studio_hub, "chat_queue_wait_seconds", lambda *_a, **_k: 0.0):
            result = studio_hub._generate_chat(_request(8192, max_tokens=8192))
        self.assertEqual([body["max_tokens"] for body in sent], [8192, 7152])
        self.assertEqual(result.payload["max_tokens_lowered_to"], 7152)
        self.assertEqual(result.payload["prompt_tokens_measured"], 41000)

    def test_a_row_with_no_ceiling_publishes_none_of_it(self) -> None:
        self.path.write_text(json.dumps({"models": {}}), encoding="utf-8")
        curated_limits._cache.update(key=None, limits={})
        row = _row(LIVE_LIMITS)
        self.assertNotIn("max_request_tokens", row,
                         "a model nobody sized publishes no ceiling, not a guessed one")

    def test_a_number_the_model_itself_states_still_wins(self) -> None:
        """The curated file never overrides a live fact — only a worker's own fallback."""
        stated = {"context_window_tokens": {"value": 32768, "source": "model config",
                                            "source_kind": "model"},
                  "max_output_tokens": {"value": 4096, "source": "model config",
                                        "source_kind": "model"}}
        row = _row(stated)
        self.assertEqual(row["max_output_tokens"], 4096)
        self.assertEqual(row["max_output_tokens_source"], "model")

    def test_a_caller_above_the_owner_s_ceiling_is_lowered_to_it(self) -> None:
        notes: dict = {}
        body = studio_hub._chat_body(_request(8192, max_tokens=40000), notes)
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(notes["max_tokens_asked"], 40000)
        self.assertEqual(notes["max_tokens_lowered_to"], 8192)


class ThePromptCeilingIsTightenedNotLoosenedTest(unittest.TestCase):
    """`max_prompt_characters: 85000` is applied only where the row has none or a larger one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-curated-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "model-limits.local.json"
        self.path.write_text(json.dumps({"models": {f"studio-hub::{LIVE_MODEL}": {
            "max_prompt_characters": 85000}}}), encoding="utf-8")
        self._real_limits_path = curated_limits.LIMITS_PATH
        curated_limits.LIMITS_PATH = self.path
        self.addCleanup(setattr, curated_limits, "LIMITS_PATH", self._real_limits_path)
        curated_limits._cache.update(key=None, limits={})

    def tearDown(self) -> None:
        curated_limits._cache.update(key=None, limits={})

    def test_an_empty_row_takes_the_curated_ceiling(self) -> None:
        row = _row(None)
        self.assertEqual(row["max_prompt_characters"], 85000)
        self.assertEqual(row["max_prompt_characters_source"], "curated")

    def test_a_larger_row_is_tightened(self) -> None:
        row = _row(None, max_prompt_characters=200000)
        self.assertEqual(row["max_prompt_characters"], 85000)

    def test_a_smaller_row_is_left_alone(self) -> None:
        """A real ceiling the producer stated is never widened by a curated figure."""
        row = _row(None, max_prompt_characters=40000)
        self.assertEqual(row["max_prompt_characters"], 40000)
        self.assertEqual(row["max_prompt_characters_source"], "provider")


class TheRequestIsRefusedAtTheDoorTest(unittest.TestCase):
    """Measured ratios: ASCII/3.7 + non-ASCII/2.0. The Hub's own count stays the authority."""

    def _submit(self, params: dict, definition=None, model=None):
        from gateway.api import _refuse_a_request_that_cannot_fit
        definition = definition or _definition()
        model = model or {"model_id": LIVE_MODEL, "display_name": "gemma-4",
                          "raw": {"model_limits": LIVE_LIMITS}}
        _refuse_a_request_that_cannot_fit(definition, model, params)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-curated-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "model-limits.local.json"
        self.path.write_text(json.dumps({"models": {f"studio-hub::{LIVE_MODEL}": CURATED}}),
                             encoding="utf-8")
        self._real_limits_path = curated_limits.LIMITS_PATH
        curated_limits.LIMITS_PATH = self.path
        self.addCleanup(setattr, curated_limits, "LIMITS_PATH", self._real_limits_path)
        curated_limits._cache.update(key=None, limits={})

    def tearDown(self) -> None:
        curated_limits._cache.update(key=None, limits={})

    def test_a_request_over_the_line_is_refused_by_name(self) -> None:
        # 32768 - 1000 - 1024 = 30744; 40,000 ASCII characters is ~10,811 tokens... so build a
        # request whose estimate clears 30,744: 114,000 ASCII characters / 3.7 ≈ 30,810.
        params = {"messages": [{"role": "user", "content": "a" * 114000}]}
        with self.assertRaises(Exception) as caught:
            self._submit(params)
        error = caught.exception
        self.assertEqual(getattr(error, "code", None), "REQUEST_TOO_LARGE_FOR_MODEL")
        self.assertEqual(error.status, 422)
        self.assertIn("Shorten the request", str(error))
        self.assertIn("30744", str(error))

    def test_a_request_just_under_the_line_still_runs(self) -> None:
        # 30,000 ASCII characters / 3.7 ≈ 8,108 tokens: well inside the line, and above the
        # published 23000-character-scale ceiling is the *answer* that gets shrunk, not this.
        params = {"messages": [{"role": "user", "content": "a" * 110000}]}
        self._submit(params)  # no raise

    def test_a_model_with_no_ceiling_is_never_estimated(self) -> None:
        self.path.write_text(json.dumps({"models": {}}), encoding="utf-8")
        curated_limits._cache.update(key=None, limits={})
        params = {"messages": [{"role": "user", "content": "a" * 500000}]}
        self._submit(params)  # no raise: nothing was ever sized for this model

    def test_non_ascii_is_priced_so_a_khmer_prompt_is_not_called_huge(self) -> None:
        from gateway.api import _estimate_request_tokens
        # Khmer measured 2.14 chars/token: 6,320 Khmer characters ≈ 2,953 tokens, not 1,708.
        khmer = "\u1780" * 6320
        estimated = _estimate_request_tokens({"content": khmer})
        # 6320 / 2.0 = 3160. Priced by characters, an ASCII rate would have called it 1708.
        self.assertEqual(estimated, 3160)
        ascii_same = _estimate_request_tokens({"content": "a" * 6320})
        self.assertEqual(ascii_same, 1708)

    def test_an_empty_request_is_not_refused(self) -> None:
        from gateway.api import _estimate_request_tokens
        self.assertIsNone(_estimate_request_tokens({"messages": []}))


if __name__ == "__main__":
    unittest.main()
