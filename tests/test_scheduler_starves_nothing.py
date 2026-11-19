#!/usr/bin/env python3
"""The scheduler's own queue cannot starve its bounds, and settled jobs leave no state (0.2.293).

Two idle-capacity shapes, both small and both measured against the live store on 1301:

1. **A bound read that the fleet's own work crowds out.** `waiting_past` filtered
   "non-terminal" and ordered by `created_at`, so a `running` job older than the cutoff sorted
   ahead of a waiting one and spent the row budget. 1301 held 65 running against 199 accepted, so
   a fleet busy enough would have had the placement sweep read only rows it then skips — the bound
   would still exist in the code and stop being enforced in reality.
2. **Sticky state** — the placement clock and rebind backoff were popped only on the paths that
   keep a job going. The four paths that *end* a row `continue` past both, so every settled job
   left an entry in two dicts nothing prunes.
"""
from __future__ import annotations

import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-scheduler-starvation-")

from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

MODEL_ID = "mlx-community/qwen-image-2-1-turbo-mflux-q4"


class AWaitingJobIsNeverCrowdedOutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="starvation-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def _job(self, *, key: str, age_s: float, state: str = "accepted",
             upstream: str | None = None) -> dict:
        job, _ = self.store.submit(
            modality="image", model_id=MODEL_ID, provider_id="studio-hub-2c7a2403564b",
            params={"prompt": "hi"}, idempotency_key=key,
        )
        now = time.time()
        with self.store._lock:  # noqa: SLF001 - the clock is not settable through the public API
            self.store._conn.execute(
                "UPDATE jobs SET created_at = ?, state = ?, upstream_job_id = ?, "
                "dispatch_started_at = ? WHERE id = ?",
                (now - age_s, state, upstream, now - age_s if upstream else None, job["id"]),
            )
            self.store._conn.commit()
        return self.store.get(job["id"])

    def test_an_old_running_job_does_not_spend_the_waiting_budget(self) -> None:
        """The failing path: older running rows used to sort ahead and fill the limit."""
        cutoff = time.time() - 2700
        waiting = self._job(key="waiting", age_s=3000)
        for index in range(5):
            self._job(key=f"running-{index}", age_s=9000 + index, state="running",
                      upstream=f"batch-{index}")
        rows = self.store.waiting_past(cutoff, limit=2)
        ids = [row["id"] for row in rows]
        self.assertIn(waiting["id"], ids,
                      f"the waiting job was crowded out by running rows: {ids}")

    def test_a_running_job_is_not_returned_at_all(self) -> None:
        self._job(key="running", age_s=9000, state="running", upstream="batch-1")
        self.assertEqual(self.store.waiting_past(time.time() - 2700), [])


class SettledJobsLeaveNoStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="sticky-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        # A short interval so one pass happens promptly; the stop event ends the loop.
        self.worker = Worker(self.store, interval_s=0.01)

    def test_a_job_ended_by_a_bound_forgets_its_clocks(self) -> None:
        """The failing path: the row ends and both dicts kept an entry for it."""
        job, _ = self.store.submit(
            modality="image", model_id=MODEL_ID, provider_id="studio-hub-2c7a2403564b",
            params={"prompt": "hi"}, idempotency_key="sticky-1",
        )
        with self.store._lock:  # noqa: SLF001 - make the job due this pass
            self.store._conn.execute("UPDATE jobs SET next_action_at = 0 WHERE id = ?", (job["id"],))
            self.store._conn.commit()
        self.worker._placement_wait_started[job["id"]] = time.time() - 99999
        self.worker._rebind_backoff[job["id"]] = (time.monotonic() + 30, 30.0)
        with patch.object(self.worker, "_expired_in_queue", lambda _job: True), \
             patch.object(self.worker, "dispatch_paused", lambda: False), \
             patch.object(self.worker, "_sweep_placement_bound", lambda: None):
            threading.Timer(0.15, self.worker.stop).start()
            self.worker._loop()
        self.assertNotIn(job["id"], self.worker._placement_wait_started,
                         "a settled job left its placement clock behind")
        self.assertNotIn(job["id"], self.worker._rebind_backoff,
                         "a settled job left its rebind backoff behind")


if __name__ == "__main__":
    unittest.main()
