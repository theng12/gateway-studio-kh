#!/usr/bin/env python3
"""0.2.153 "Remove Failed From List": hides settled failures only; totals and analytics stay."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-jobs-bulk-clear-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api  # noqa: E402

store = api.store


def _job(name: str, state: str, **fail) -> str:
    row, _ = store.submit(modality="chat", model_id="m", params={"prompt": name},
                          idempotency_key=f"bulk-{name}-{time.time_ns()}", provider_id="pool")
    if state in ("running", "failed", "succeeded"):
        store.mark_running(row["id"])
    if state == "failed":
        store.fail(row["id"], **fail)
    if state == "succeeded":
        store.succeed(row["id"], result={"text": "ok"})
    return row["id"]


class ClearFailedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.ok = _job("ok", "succeeded")
        self.plain = _job("plain", "failed", code="PROVIDER_FAILED", detail="x")
        self.retryable = _job("retry", "failed", code="PROVIDER_FAILED", detail="x", retryable=True)
        self.hold = _job("hold", "failed", code="UNKNOWN_OUTCOME", detail="x",
                         recovery_state="unknown_outcome", recovery_detail="held")
        self.running = _job("run", "running")
        self.queued = _job("queue", "accepted")

    def tearDown(self) -> None:
        self.client.close()

    def listed(self, query: str = "") -> set[str]:
        body = self.client.get(f"/admin/api/jobs?limit=500{query}").json()
        return {row["id"] for row in body["jobs"]}

    def test_hides_only_settled_failures_and_keeps_every_record(self) -> None:
        mine = {self.ok, self.plain, self.retryable, self.hold, self.running, self.queued}
        before_rows = {j: store.get(j) for j in mine}
        stats, analytics = store.stats(), store.generation_analytics()
        bulk = self.client.get("/admin/api/jobs?limit=500").json()["bulk"]
        self.assertGreaterEqual(bulk["removable_failed"], 2)

        removed = self.client.post(f"/admin/api/jobs/clear-failed?before={bulk['as_of']}").json()
        self.assertGreaterEqual(removed["removed"], 2)
        shown = self.listed() & mine
        self.assertEqual(shown, {self.ok, self.hold, self.running, self.queued})
        self.assertEqual(self.listed("&state=failed") & mine, {self.hold}, "a needs-recovery job stays")
        self.assertEqual(self.listed("&include_removed=1") & mine, mine, "Show removed brings them back")

        # Nothing but the view flag moved: totals, analytics, every column of every row.
        self.assertEqual(store.stats(), stats)
        self.assertEqual(store.generation_analytics(), analytics)
        for job_id, row in before_rows.items():
            after = store.get(job_id)
            after.pop("dismissed_at")
            row.pop("dismissed_at")
            self.assertEqual(after, row, job_id)

        again = self.client.post("/admin/api/jobs/clear-failed").json()
        self.assertEqual(again["removed"], 0, "idempotent")
        self.assertEqual(self.client.get("/admin/api/jobs?state=failed").json()["bulk"]["removable_failed"], 0)

    def test_a_failure_newer_than_the_confirmation_is_left_alone(self) -> None:
        cutoff = time.time() - 60
        self.client.post(f"/admin/api/jobs/clear-failed?before={cutoff}")
        self.assertIn(self.plain, self.listed())

    def test_a_job_that_changes_after_removal_shows_again(self) -> None:
        self.client.post("/admin/api/jobs/clear-failed")
        self.assertNotIn(self.plain, self.listed())
        time.sleep(0.01)
        store._update(self.plain, error_detail="late news")
        self.assertIn(self.plain, self.listed())


if __name__ == "__main__":
    unittest.main()
