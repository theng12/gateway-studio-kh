"""A claim or thread-start failure must return its booking and unsent job."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolation import sandbox_env

_SANDBOX = sandbox_env(prefix="gateway-launch-guard-")

from gateway import worker as worker_module
from gateway.store import JobStore
from gateway.worker import Worker


class WorkerLaunchGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = JobStore(Path(directory.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store, pool_size=1)
        self.job, _ = self.store.submit(
            modality="chat", model_id="fixture", params={},
            idempotency_key="launch", provider_id="fixture")
        self.store._conn.execute("UPDATE jobs SET next_action_at = 0 WHERE id = ?", (self.job["id"],))
        self.store._conn.commit()

    def one_pass(self, *, start_hold_sweep=False):
        worker = self.worker
        worker._stop.clear()
        worker._last_hold_sweep = 0 if start_hold_sweep else time.time()
        worker._last_superseded_sweep = time.time()
        worker._last_liveness = time.time()

        def admit(job):
            worker._book(job)
            return True

        with patch.object(worker, "_admit", side_effect=admit), \
                patch.object(worker, "_expired_in_queue", return_value=False), \
                patch.object(worker, "_stranded_on_paused", return_value=False), \
                patch.object(worker, "_image_limits_refused", return_value=False), \
                patch.object(worker._stop, "wait", side_effect=lambda _: worker._stop.set()):
            worker._loop()
        self.assertIsNotNone(worker.last_pass_at)

    def assert_released(self):
        self.assertEqual(self.worker.active(), 0, "failed launch leaked a phantom in-flight slot")
        for book in (self.worker._in_flight, self.worker._lane_in_flight,
                     self.worker._per_lane, self.worker._per_model, self.worker._per_group):
            self.assertEqual(book, {}, "failed launch left an admission booking behind")

    def test_malformed_stored_target_refuses_one_job_and_keeps_dispatching(self):
        self.store._conn.execute("UPDATE jobs SET params = ? WHERE id = ?",
                                 ('{"measurement_target":"broken"}', self.job["id"]))
        next_job, _ = self.store.submit(modality="chat", model_id="fixture", params={},
                                        idempotency_key="next", provider_id="fixture")
        self.store._conn.execute("UPDATE jobs SET next_action_at = 0")
        self.store._conn.commit()
        with patch.object(worker_module.threading, "Thread") as thread:
            self.one_pass()
            thread.return_value.start.assert_called_once()
        broken = self.store.get(self.job["id"])
        self.assertEqual(broken["state"], "failed")
        self.assertEqual(broken["error_code"], "INVALID_MEASUREMENT_TARGET")
        self.assertEqual(self.store.get(next_job["id"])["state"], "running")
        self.assertEqual(self.worker.pass_failures, 0)

    def test_a_failed_due_read_leaves_health_red_until_a_clean_pass(self):
        """A due() that raises must count as a failed pass — for the whole pass.

        The pass read `due` in one try and dispatched in another, and the second one ended with
        `self.pass_failures = 0`. So a persistent read failure left every job parked in `accepted`,
        dispatched nothing, and `/health` published `consecutive_failures: 0` with a fresh
        `last_pass_at` — a lying page about the whole app's ability to work.
        """
        worker = self.worker
        worker._stop.clear()
        worker._last_hold_sweep = time.time()
        worker._last_superseded_sweep = time.time()
        worker._last_liveness = time.time()
        with patch.object(self.store, "due",
                          side_effect=sqlite3.OperationalError("database is locked")), \
                patch.object(worker._stop, "wait", side_effect=lambda _: worker._stop.set()), \
                self.assertLogs("gateway.worker", level="ERROR"):
            worker._loop()
        self.assertEqual(worker.pass_failures, 1, "the failed pass must stay counted")
        self.assertEqual(worker.liveness()["consecutive_failures"], 1,
                         "/health must not read green when nothing was dispatched for a reason")

        worker._stop.clear()   # the first loop stopped itself at the end of its pass
        with patch.object(self.store, "due", return_value=[]), \
                patch.object(worker._stop, "wait", side_effect=lambda _: worker._stop.set()):
            worker._loop()
        self.assertEqual(worker.pass_failures, 0, "a clean pass clears the count")

    def test_locked_claim_releases_booking_and_next_pass_can_start(self):
        with patch.object(self.store, "mark_running", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs("gateway.worker", level="ERROR"):
            self.one_pass()
        self.assert_released()
        self.assertEqual(self.store.get(self.job["id"])["state"], "accepted")
        with patch.object(worker_module.threading, "Thread") as thread:
            self.one_pass()
            thread.return_value.start.assert_called_once()
        self.assertEqual(self.worker.active(), 1, "second pass must own exactly one slot")

    def test_refused_thread_start_returns_unsent_claim_without_spending_an_attempt(self):
        with patch.object(worker_module.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("gateway.worker", level="ERROR"):
            self.one_pass()
        self.assert_released()
        row = self.store.get(self.job["id"])
        self.assertEqual(row["state"], "accepted", "unsent claim was stranded as running")
        self.assertEqual(row["attempt_count"], 0)
        self.assertIsNone(row["dispatch_started_at"])
        with patch.object(worker_module.threading, "Thread") as thread:
            self.one_pass()
            thread.return_value.start.assert_called_once()
        self.assertEqual(self.worker.active(), 1)

    def test_claim_that_committed_before_raising_is_also_returned_to_queue(self):
        claim = self.store.mark_running

        def committed_then_raised(job_id):
            claim(job_id)
            raise sqlite3.OperationalError("commit acknowledgement failed")

        with patch.object(self.store, "mark_running", side_effect=committed_then_raised), \
                self.assertLogs("gateway.worker", level="ERROR"):
            self.one_pass()
        self.assert_released()
        self.assertEqual(self.store.get(self.job["id"])["state"], "accepted")

    def test_refused_hold_sweep_start_keeps_dispatch_alive_and_clears_the_flag(self):
        with patch.object(self.store, "due", return_value=[]), \
                patch.object(worker_module.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("gateway.worker", level="ERROR"):
            self.one_pass(start_hold_sweep=True)
        self.assertFalse(self.worker._sweeping.is_set(), "refused sweep must be eligible for another launch")
        self.assert_released()

    def test_resume_spawn_failure_releases_slot_and_preserves_unknown_outcome_hold(self):
        self.store.mark_running(self.job["id"])
        self.store.interrupted = [self.job["id"]]
        with patch.object(worker_module, "restart_can_follow", return_value=True), \
                patch.object(worker_module.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("gateway.worker", level="ERROR"):
            self.worker._resume_interrupted()
        self.assert_released()
        row = self.store.get(self.job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["recovery_state"], "unknown_outcome")


if __name__ == "__main__":
    unittest.main()
