#!/usr/bin/env python3
"""A placement bound measured from submit, that cannot be skipped (0.2.291).

**The defect (measured live 2026-11-19).** Every placement check lived under `for job in due:`,
so a job deferred with a long backoff was never examined. `_expired_in_queue` bounds by
`created_at`, but it is reached from that same loop, so it never ran either. Across 580 jobs the
PLACEMENT_TIMEOUT waits were 5.0–214.2 minutes against a 45-minute ceiling — a bound that only
applies on the next look is not a bound.

Two things are pinned here:

1. A job that is **not due** — `next_action_at` hours in the future — is still ended at its bound,
   with the same decision `_note_waiting` makes and the reasons 0.2.290 records.
2. A job **with an upstream id** is left alone by this sweep. That half needs the batch withdrawn
   before the job can be failed (a Hub job that is merely slow is not an abandoned one), so it is
   deliberately not done here and the test says so rather than leaving it to look like an oversight.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-placement-wallclock-")

from gateway import concurrency, fleet  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

SITE = "2c7a2403564b"
MODEL_ID = "mlx-community/qwen-image-2-1-turbo-mflux-q4"


class TheBoundIsMeasuredFromSubmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="wallclock-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store)

    def _job(self, *, age_s: float, upstream: str | None = None, not_due_s: float = 7200.0) -> dict:
        job, _ = self.store.submit(
            modality="image", model_id=MODEL_ID, provider_id=f"studio-hub-{SITE}",
            params={"prompt": "hi"}, idempotency_key=f"wallclock-{age_s}-{upstream or 'none'}",
        )
        now = time.time()
        with self.store._lock:  # noqa: SLF001 - the clock is not settable through the public API
            self.store._conn.execute(
                "UPDATE jobs SET created_at = ?, next_action_at = ?, upstream_job_id = ? WHERE id = ?",
                (now - age_s, now + not_due_s, upstream, job["id"]),
            )
            self.store._conn.commit()
        return self.store.get(job["id"])

    def _empty_fleet(self):
        row = {
            "id": SITE, "provider_id": f"studio-hub-{SITE}", "label": f"Site {SITE}",
            "reachable": True, "slow": False, "answered_at": time.time(), "role": "controller",
            "drained": False, "capacity": {"accepting_customer_jobs": True, "queue_depth": 0},
            "paused_at": None, "machines": [], "machine_summary": {"reachable": 0, "total": 0},
            "running_jobs": [], "model_supply": {},
        }
        return patch.object(fleet, "snapshot", lambda **_kw: {"locations": [row]})

    def test_a_job_that_is_not_due_is_still_ended_at_its_bound(self) -> None:
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        job = self._job(age_s=ceiling + 60)
        self.assertNotIn(job["id"], [row["id"] for row in self.store.due()],
                         "the job must be genuinely not due, or the test proves nothing")
        # **Why the old code could never reach it**: the only two placement endings are called
        # from the due loop, and this job is not in it. Nothing here is a timing accident.
        self.assertFalse(self.worker._expired_in_queue(job))
        with self._empty_fleet():
            self.worker._sweep_placement_bound()
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        self.assertTrue(row.get("retryable"), "nothing was sent; retrying is safe")

    def test_a_young_job_is_left_alone(self) -> None:
        job = self._job(age_s=60.0)
        with self._empty_fleet():
            self.worker._sweep_placement_bound()
        self.assertEqual(self.store.get(job["id"])["state"], "accepted")

    def test_a_job_with_an_upstream_id_is_not_ended_here(self) -> None:
        """Not done by design: that half must withdraw the batch first (see the backlog row)."""
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        job = self._job(age_s=ceiling + 60, upstream="batch-123")
        with self._empty_fleet():
            self.worker._sweep_placement_bound()
        row = self.store.get(job["id"])
        self.assertNotEqual(row["state"], "failed",
                            "an upstream-id job must not be failed without withdrawing its batch")


if __name__ == "__main__":
    unittest.main()
