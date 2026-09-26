#!/usr/bin/env python3
"""The Hub's stall signal, recorded on the job it happened to.

Studio Hub added a progress signal to their job rows and told this gateway, 2026-09-26:
*"`stall.py` marks a job whose worker has gone quiet… If you want to surface it, it is on
the row now — but note nothing in our native app consumes it yet, so you would be
first."*

This is that first consumer, and it exists because of a number this gateway asked for:
the chat and voice deadlines are **1,800 s**. The Hub's argument for why a thirty-minute
ceiling is *safe* rather than merely long is that a stall is visible — which is only true
if something shows it. On this path, that something is us; Story Studio cannot reach a
Hub.

**It is a record, not a live signal.** A caller watching an unfinished job still sees
`running` until it settles. The live half needs a write back to our own job row while
the poll is in flight and is deliberately not built (GW-13).

Most of what is asserted here is about **what silence must not be read as**, because
that is where this kind of field goes wrong:

  * `no_progress_s: None` is the Hub saying *not applicable* — a queued or finished job
    has no progress to be missing — and folding it in as `0.0` would make every
    completed job look perfect and drag the worst case down with it;
  * a Hub that never states a figure yields **`None`**, not a reassuring
    `stalled: false` this gateway invented out of its own silence;
  * `stalled` **latches** — one poll past the threshold is a fact about the run, and a
    later poll that happens to be fresh must not erase it.

Run with::

    .venv/bin/python tests/test_hub_progress.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.adapters.studio_hub import HubProgress, _poll_batch  # noqa: E402


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    """Serves one canned summary per poll, then a terminal one for ever."""

    def __init__(self, summaries: list[dict]) -> None:
        self._summaries = list(summaries)
        self.calls = 0

    def get(self, _url: str, headers=None) -> _Response:  # noqa: ANN001, ARG002
        self.calls += 1
        if self._summaries:
            return _Response(self._summaries.pop(0))
        return _Response({"items": [{"state": "done"}]})


def _running(no_progress_s=None, stalled_after_s=None, stalled=None) -> dict:
    summary: dict = {"items": [{"state": "running"}]}
    if no_progress_s is not None:
        summary["no_progress_s"] = no_progress_s
    if stalled_after_s is not None:
        summary["stalled_after_s"] = stalled_after_s
    if stalled is not None:
        summary["stalled"] = stalled
    return summary


class SilenceIsNotZeroTest(unittest.TestCase):
    def test_a_hub_that_states_nothing_publishes_nothing(self) -> None:
        # An older Hub, or a lane that reports no progress. `None` means "we were not
        # told"; `stalled: false` would mean "we were told it did not stall". Those are
        # different answers and must not share a value.
        progress = HubProgress()
        progress.note({"items": [{"state": "running"}]})
        self.assertIsNone(progress.as_payload())
        self.assertEqual(progress.samples, 0)

    def test_a_null_figure_is_not_applicable_and_not_zero(self) -> None:
        # The Hub sets `no_progress_s` only while a batch is active. Counting the null
        # as 0.0 would make every completed job read as perfect progress.
        progress = HubProgress()
        progress.note(_running())
        progress.note({"no_progress_s": None, "stalled": False})
        self.assertIsNone(progress.as_payload())

    def test_a_boolean_is_not_a_measurement(self) -> None:
        # `True` is an `int` in Python; read as a number it becomes one second of
        # progress, which is a fabricated sample.
        progress = HubProgress()
        progress.note({"no_progress_s": True, "stalled": True})
        self.assertIsNone(progress.as_payload())


class TheWorstIsWhatMattersTest(unittest.TestCase):
    def test_the_worst_figure_is_kept_not_the_last(self) -> None:
        # A run that stalled and then recovered must still read as having stalled.
        # Keeping the last poll would report the recovery and lose the event.
        progress = HubProgress()
        for seconds in (5.0, 1_400.0, 12.0):
            progress.note(_running(no_progress_s=seconds, stalled_after_s=900))
        payload = progress.as_payload()
        self.assertEqual(payload["worst_no_progress_s"], 1_400.0)
        self.assertEqual(payload["samples"], 3)

    def test_a_stall_latches_across_a_later_healthy_poll(self) -> None:
        progress = HubProgress()
        progress.note(_running(no_progress_s=1_000.0, stalled_after_s=900, stalled=True))
        progress.note(_running(no_progress_s=3.0, stalled_after_s=900, stalled=False))
        payload = progress.as_payload()
        self.assertTrue(payload["stalled"])
        self.assertEqual(payload["stalled_after_s"], 900)

    def test_the_threshold_travels_with_the_figure(self) -> None:
        # "1,400 seconds" means nothing without the number it was judged against, and
        # the Hub derives that threshold per batch from its own average.
        progress = HubProgress()
        progress.note(_running(no_progress_s=1_400.0, stalled_after_s=1_800))
        self.assertEqual(progress.as_payload()["stalled_after_s"], 1_800)


class TheErrorSaysWhyTest(unittest.TestCase):
    def test_a_healthy_run_adds_nothing_to_a_message(self) -> None:
        progress = HubProgress()
        progress.note(_running(no_progress_s=5.0, stalled_after_s=900, stalled=False))
        self.assertEqual(progress.why(), "")

    def test_a_stalled_run_names_both_numbers(self) -> None:
        progress = HubProgress()
        progress.note(_running(no_progress_s=1_700.0, stalled_after_s=900, stalled=True))
        why = progress.why()
        self.assertIn("1700", why)
        self.assertIn("900", why)
        self.assertIn("stalled", why)

    def test_a_run_with_no_threshold_still_explains_itself(self) -> None:
        # Partial data is still worth reporting; it must not be silently dropped
        # because one of the two numbers is missing.
        progress = HubProgress()
        progress.note({"no_progress_s": 1_700.0, "stalled": True})
        self.assertIn("1700", progress.why())
        self.assertNotIn("None", progress.why())


class ThePollRecordsItTest(unittest.TestCase):
    """The wiring, driven through the real loop rather than inspected."""

    def _poll(self, summaries: list[dict]) -> tuple[dict, HubProgress]:
        progress = HubProgress()
        client = _Client(summaries)
        outcome = _poll_batch(  # type: ignore[arg-type]
            client, "http://127.0.0.1:1", {}, "batch-1", 0,
            deadline_seconds=5.0, progress=progress,
        )
        return outcome, progress

    def test_the_loop_notes_every_poll_before_it_finishes(self) -> None:
        outcome, progress = self._poll([
            _running(no_progress_s=10.0, stalled_after_s=900, stalled=False),
            _running(no_progress_s=1_000.0, stalled_after_s=900, stalled=True),
            {"items": [{"state": "done"}]},
        ])
        self.assertEqual(outcome["item"]["state"], "done")
        payload = progress.as_payload()
        self.assertEqual(payload["samples"], 2, "the terminal poll states no progress")
        self.assertTrue(payload["stalled"])
        self.assertEqual(payload["worst_no_progress_s"], 1_000.0)

    def test_a_poll_that_forgets_its_tracker_still_works(self) -> None:
        # `progress` is optional so no existing caller breaks, and the lane must not
        # depend on somebody having passed one.
        client = _Client([{"items": [{"state": "done"}]}])
        outcome = _poll_batch(  # type: ignore[arg-type]
            client, "http://127.0.0.1:1", {}, "batch-1", 0, deadline_seconds=5.0,
        )
        self.assertEqual(outcome["item"]["state"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
