#!/usr/bin/env python3
"""The placement-wait ceiling bites (0.2.266).

A fleet job pinned to a Mac that never frees either moves to another eligible
Mac or fails PLACEMENT_TIMEOUT retryable — never waits forever on retry_in 0.
No attempt is consumed on this path.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-placement-bound-")

from gateway import concurrency, fleet  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402


class PlacementBoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="placement-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.job, _ = self.store.submit(
            modality="tts", model_id="voice-model",
            provider_id="studio-hub-2c7a2403564b", params={"text": "hi"},
            idempotency_key="placement-1",
        )
        self.worker = Worker(self.store)

    def _row(self) -> dict:
        return self.store.get(self.job["id"])

    def test_past_ceiling_with_no_move_fails_retryable_placement_timeout(self) -> None:
        """(a) A pinned job that cannot move fails PLACEMENT_TIMEOUT retryable."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "all Macs busy"), \
             patch("gateway.worker._next_machine", return_value=None):
            self.worker._note_waiting(self._row())
        row = self._row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        self.assertTrue(row.get("retryable"), "nothing was sent; retrying is safe")
        self.assertIn("all Macs busy", row.get("error_detail") or "")
        self.assertNotIn(self.job["id"], self.worker._placement_wait_started)

    def test_past_ceiling_moves_when_another_mac_is_eligible(self) -> None:
        """(a)+(b) A pinned job moves instead of failing; no attempt consumed."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        before_attempts = self._row().get("attempt_count") or 0
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "all Macs busy"), \
             patch("gateway.worker._next_machine", return_value="studio-hub-other"):
            self.worker._note_waiting(self._row())
        row = self._row()
        # The job stays accepted when it moves; _next_machine records the attempt log
        # but does not consume an attempt_count on the row itself.
        self.assertNotEqual(row["state"], "failed", "the job moved, not failed")
        after_attempts = row.get("attempt_count") or 0
        self.assertEqual(before_attempts, after_attempts,
                         "placement move consumes no attempt")
        self.assertNotIn(self.job["id"], self.worker._placement_wait_started)

    def test_inside_ceiling_waits_with_remaining_minutes_sentence(self) -> None:
        """(c) A job inside the ceiling still waits, with remaining-minutes suffix."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        half = ceiling / 2
        self.worker._placement_wait_started[self.job["id"]] = time.time() - half
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "all Macs busy"):
            self.worker._note_waiting(self._row())
        row = self._row()
        self.assertEqual(row["state"], "accepted", "still waiting, not failed")
        self.assertIsNotNone(row["defer_reason"])
        self.assertIn("min of", row["defer_reason"],
                       "the sentence names remaining and allowed minutes")
        self.assertIn(self.job["id"], self.worker._placement_wait_started,
                       "the clock stays while the job waits")

    def test_a_site_that_cannot_take_the_job_is_not_waited_on_for_the_ceiling(self) -> None:
        """A site that answers nothing is not a queue: 45 minutes at terranash-1500 (2026-11-17)."""
        from gateway.worker import _UNUSABLE_SITE_WAIT_S
        self.worker._placement_wait_started[self.job["id"]] = time.time() - _UNUSABLE_SITE_WAIT_S - 1
        with patch.object(fleet, "site_cannot_take", return_value=True), \
             patch.object(fleet, "site_wait_reason", lambda pid, mid: "waiting for A to answer"), \
             patch("gateway.worker._next_machine", return_value=None):
            self.worker._note_waiting(self._row())
        row = self._row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        self.assertTrue(row.get("retryable"), "nothing was sent; retrying is safe")
        self.assertIn("waiting for A to answer", row.get("error_detail") or "")

    def test_inside_the_short_grace_an_unusable_site_still_waits(self) -> None:
        from gateway.worker import _UNUSABLE_SITE_WAIT_S
        self.worker._placement_wait_started[self.job["id"]] = time.time() - _UNUSABLE_SITE_WAIT_S / 2
        with patch.object(fleet, "site_cannot_take", return_value=True), \
             patch.object(fleet, "site_wait_reason", lambda pid, mid: "waiting for A to answer"):
            self.worker._note_waiting(self._row())
        self.assertEqual(self._row()["state"], "accepted",
                         "a site that missed one probe is not gone")

    def test_a_busy_site_keeps_the_full_ceiling(self) -> None:
        from gateway.worker import _UNUSABLE_SITE_WAIT_S
        self.worker._placement_wait_started[self.job["id"]] = time.time() - _UNUSABLE_SITE_WAIT_S - 1
        with patch.object(fleet, "site_cannot_take", return_value=False), \
             patch.object(fleet, "site_wait_reason", lambda pid, mid: "waiting for a free Mac at A"):
            self.worker._note_waiting(self._row())
        self.assertEqual(self._row()["state"], "accepted",
                         "busy Macs do free up; the placement ceiling is for that wait")

    def test_admission_closed_ends_in_retryable_refusal_not_permanent_gate(self) -> None:
        """(d) A pinned job with admission closed gets a retryable refusal, not a dead end."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 10
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "admission closed"), \
             patch("gateway.worker._next_machine", return_value=None):
            self.worker._note_waiting(self._row())
        row = self._row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        self.assertTrue(row.get("retryable"),
                        "a permanently closed gate is the defect this closes")

    def test_running_job_is_never_touched_by_the_ceiling(self) -> None:
        """Gate: state != accepted or upstream_job_id set skips the ceiling entirely."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 100
        # Simulate a running job with an upstream id.
        self.store.defer(self.job["id"], delay_s=0, reason="running")
        row = self._row()
        row["state"] = "running"
        row["upstream_job_id"] = "upstream-abc"
        # _note_waiting reads from store.get, so we need the row in the store to reflect this.
        # Use fail+resurrect is too heavy; just call with a synthetic dict.
        synthetic = dict(row, state="running", upstream_job_id="upstream-abc")
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "should not fire"):
            self.worker._note_waiting(synthetic)
        # The real row must be untouched by any failure.
        real = self._row()
        self.assertNotEqual(real.get("error_code"), "PLACEMENT_TIMEOUT")

    def test_no_attempt_count_touched_on_any_placement_path(self) -> None:
        """(b) Neither move nor fail touches attempt_count."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        # Fail path
        job2, _ = self.store.submit(
            modality="tts", model_id="voice-model",
            provider_id="studio-hub-2c7a2403564b", params={"text": "hi"},
            idempotency_key="placement-no-attempt",
        )
        self.worker._placement_wait_started[job2["id"]] = time.time() - ceiling - 1
        before = self.store.get(job2["id"]).get("attempt_count") or 0
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "busy"), \
             patch("gateway.worker._next_machine", return_value=None):
            self.worker._note_waiting(self.store.get(job2["id"]))
        after = self.store.get(job2["id"]).get("attempt_count") or 0
        self.assertEqual(before, after, "fail path must not touch attempt_count")


    def test_a_real_placement_ceiling_hit_costs_no_attempt(self) -> None:
        """The real `_next_machine`, not a stub: the site joins the tried set, no try is spent.

        Every other test here stubs `_next_machine`, so the one thing that could still spend the
        job's three tries — `record_attempt(code="PLACEMENT_TIMEOUT")` inside it — was never run.
        A ceiling is a refusal: nothing was sent, so nothing is charged (0.2.263's rule)."""
        from gateway.worker import _UNCOUNTED_ATTEMPT_CODES

        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "all Macs busy"):
            self.worker._note_waiting(self._row())
        attempts = self.store.attempts_of(self.job["id"])
        counted = [e for e in attempts if e.get("code") not in _UNCOUNTED_ATTEMPT_CODES]
        self.assertEqual(counted, [], "a placement wait must not spend one of the three tries")
        self.assertTrue(any(e.get("code") == "PLACEMENT_TIMEOUT" for e in attempts),
                        "the ceiling really did record the wait — otherwise this proves nothing")
        self.assertTrue(attempts, "but the site still joins the tried set, so it cannot bounce")
        self.assertIn(self._row()["state"], ("failed", "accepted"))


if __name__ == "__main__":
    unittest.main()