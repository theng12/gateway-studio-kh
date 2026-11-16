#!/usr/bin/env python3
"""Interruptions invisible to Story (0.2.265; owner, 2026-11-16).

*"story studio doesn't need to know the failure behind. it's only consuming assets and sending work."*

Story sees one job id and only `queued → running → done`, or a failure no retry can fix. A Mac, a
site or the Gateway being interrupted is the Gateway's own problem: it re-places the work under the
same id, adopts the first result that lands, and keeps everything else to itself.

These pin the two halves of that:
1. a live unknown-outcome hold reads `running` to Story, with none of the hold's fields, while the
   operator's own reading still shows the hold it must reconcile;
2. a hold that has been settled reads as the real verdict it became.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-seamless-")

from gateway import api, concurrency  # noqa: E402

HOLD = {
    "id": "3f2b1c00-0000-4000-8000-000000000001", "modality": "tts", "model_id": "voice-model",
    "provider_id": "studio-hub-a", "params": "{}", "result_json": None, "priority": "normal",
    "created_at": 1.0, "state": "failed",
    "recovery_state": "unknown_outcome", "recovery_required": 1, "recovery_action": "reconcile",
    "recovery_detail": "lost contact with the location while polling batch ab0a235660 (item 0) for 60s",
    "failure_code": "PROVIDER_SUBMISSION_UNCERTAIN", "failure_class": "provider_error",
    "failure_reason": "lost contact with the location while polling batch ab0a235660",
    "upstream_job_id": "ab0a235660", "dispatch_state": "dispatched",
}


class AHoldLooksRunningToStory(unittest.TestCase):
    def test_story_sees_running_and_nothing_about_the_interruption(self) -> None:
        story = api._job_envelope(dict(HOLD))
        self.assertEqual(story["state"], "running", "an unsettled hold is not a failure Story may see")
        for key in ("recovery_required", "recovery_state", "recovery_detail", "recovery_action",
                    "upstream_job_id", "late_result_state", "recovery_context"):
            self.assertNotIn(key, story, f"{key} is the Gateway's business, not Story's")
        self.assertIsNone(story["failure_code"], "no interruption code reaches Story")
        self.assertIsNone(story["failure_reason"])
        self.assertGreater(story["poll_after_ms"], 0, "and it still polls like a running job")

    def test_the_operator_still_sees_the_hold_it_must_reconcile(self) -> None:
        operator = api._job_envelope(dict(HOLD), native=True)
        self.assertEqual(operator["state"], "failed", "the row is what the database says")
        self.assertEqual(operator["recovery_state"], "unknown_outcome")
        self.assertTrue(operator["recovery_required"])
        self.assertIn("lost contact", operator["recovery_detail"])

    def test_a_settled_hold_reads_as_the_verdict_it_became(self) -> None:
        closed = dict(HOLD, recovery_state=None, failure_code="PROVIDER_FAILED",
                      failure_reason="the Voice worker rejected the reference")
        story = api._job_envelope(closed)
        self.assertEqual(story["state"], "failed")
        self.assertEqual(story["failure_code"], "PROVIDER_FAILED")

    def test_the_one_time_limit_is_on_the_row_with_what_is_left(self) -> None:
        """Owner-approved ("only one time limit, long for free local work, shown on the row")."""
        import time as _time
        queued = dict(HOLD, state="accepted", recovery_state=None, created_at=_time.time() - 600)
        story = api._job_envelope(queued)
        self.assertEqual(story["time_limit_s"], int(concurrency.queue_wait_budget()["wait_ceiling_seconds"]))
        self.assertLess(story["time_left_s"], story["time_limit_s"], "time passes")
        self.assertGreater(story["time_left_s"], 0)
        # A finished job has no clock left to show.
        done = dict(queued, state="succeeded")
        self.assertIsNone(api._job_envelope(done)["time_limit_s"])

    def test_a_running_job_is_untouched(self) -> None:
        running = dict(HOLD, state="running", recovery_state=None, failure_code=None, failure_reason=None)
        self.assertEqual(api._job_envelope(running)["state"], "running")
        self.assertEqual(api._job_envelope(running, native=True)["state"], "running")


if __name__ == "__main__":
    unittest.main()
