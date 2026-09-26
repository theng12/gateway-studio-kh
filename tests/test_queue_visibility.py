"""The gateway's own queue, made visible — and the two numbers that bound it.

The owner asked for this in as many words: *"when the machines get busy, i want it queue… i need the
UI for that."* The queue was built on 2026-09-25 (`store.defer()` puts a job back with a
`next_action_at`), it is unit-tested, and **it had never once fired in live traffic** — zero jobs
carried a `defer_reason`, and no job had ever been dispatched twice. What it did do was stay
invisible: the job envelope hardcoded `queue_position`, `queue_eta_seconds` and `queue_reason` to
`null`, and `defer_reason` was written to the database and never read out.

Story's side was already waiting for it. Its contract types `queue_reason` as exactly
`'provider_pace' | null`, and its Jobs page renders *"Waiting in the GenStudio provider queue…
provider queue position 2… Estimated provider wait about 2 minutes… GenStudio is pacing new
requests."* So these tests hold the vocabulary to Story's, and hold the numbers to something the
gateway can actually know:

  * `queue_position` counts **waiting** jobs of the same modality — 1 is next, and a dispatched job
    is not in this queue at all;
  * `queue_eta_seconds` is **when this gateway will try again**, never a claim about when the
    provider will finish;
  * a job that is not waiting publishes nothing, rather than a zero that reads like a fact.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import api  # noqa: E402
from gateway import concurrency  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import _defer_delay  # noqa: E402


class TheQueueHasAPositionTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.first, _ = self.store.submit(
            modality="image", model_id="m", params={}, idempotency_key="a",
            provider_id="studio-hub-local",
        )
        self.second, _ = self.store.submit(
            modality="image", model_id="m", params={}, idempotency_key="b",
            provider_id="studio-hub-local",
        )
        self.other_lane, _ = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="c",
            provider_id="studio-hub-local",
        )

    def test_a_dispatched_job_is_not_in_the_queue(self) -> None:
        self.assertIsNone(
            self.store.queue_position(self.first["id"]),
            "a job nobody is waiting on must not be given a queue position",
        )

    def test_waiting_jobs_are_ranked_first_come_first_served(self) -> None:
        for row in (self.first, self.second, self.other_lane):
            self.store.defer(row["id"], delay_s=30, reason="LOCATION_NOT_READY: MACHINE_BUSY")
        self.assertEqual(self.store.queue_position(self.first["id"]), 1)
        self.assertEqual(
            self.store.queue_position(self.second["id"]), 2,
            "the second image job is behind the first",
        )
        self.assertEqual(
            self.store.queue_position(self.other_lane["id"]), 1,
            "a chat job is not behind image work: the queue is per modality",
        )


class TheEnvelopeSaysSoTest(unittest.TestCase):
    def _body(self, job: dict) -> dict:
        body: dict = {}
        with patch.object(api, "store", _Queue(3)):
            api._publish_queue_evidence(body, job)
        return body

    def test_a_waiting_job_publishes_the_vocabulary_story_reads(self) -> None:
        job = {
            "id": "job-1",
            "defer_reason": "LOCATION_NOT_READY: MACHINE_BUSY",
            "next_action_at": time.time() + 42,
        }
        body = self._body(job)
        self.assertEqual(body["queue_reason"], "provider_pace")
        self.assertEqual(body["queue_position"], 3)
        self.assertTrue(0 <= body["queue_eta_seconds"] <= 42)

    def test_a_job_that_is_not_waiting_publishes_nothing(self) -> None:
        body = self._body({"id": "job-2", "next_action_at": None})
        self.assertEqual(body, {}, "an absent queue is not a queue at position zero")

    def test_a_passed_retry_time_does_not_publish_a_negative_wait(self) -> None:
        job = {"id": "job-3", "defer_reason": "x", "next_action_at": time.time() - 5}
        body = self._body(job)
        self.assertEqual(body["queue_eta_seconds"], 0)


class _Queue:
    """Stands in for the store's queue reader."""

    def __init__(self, position: int) -> None:
        self.position = position

    def queue_position(self, job_id: str) -> int:
        return self.position


class TheWaitIsSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "provider-concurrency.local.json"
        patcher = patch.object(concurrency, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_documented_defaults_stand_when_nothing_is_set(self) -> None:
        budget = concurrency.queue_wait_budget()
        self.assertEqual(budget["max_deferrals"], 8)
        self.assertEqual(budget["defer_cap_seconds"], 60.0)
        self.assertEqual(budget["source"], concurrency.SOURCE_GATEWAY)

    def test_an_operator_can_wait_longer_than_four_minutes(self) -> None:
        """The point of the change: ~4 minutes was failing work a busy fleet would have run."""
        self.path.write_text(
            json.dumps({"global": 60, "queue": {"max_deferrals": 24, "defer_cap_seconds": 300}}),
            encoding="utf-8",
        )
        budget = concurrency.queue_wait_budget()
        self.assertEqual(budget["max_deferrals"], 24)
        self.assertEqual(budget["defer_cap_seconds"], 300.0)
        self.assertEqual(budget["source"], concurrency.SOURCE_CURATED)
        self.assertEqual(_defer_delay(20, budget["defer_cap_seconds"]), 300.0,
                         "the backoff must respect the configured ceiling")

    def test_a_nonsense_value_falls_back_rather_than_removing_the_ceiling(self) -> None:
        self.path.write_text(json.dumps({"queue": {"max_deferrals": 0, "defer_cap_seconds": -1}}),
                             encoding="utf-8")
        budget = concurrency.queue_wait_budget()
        self.assertEqual(budget["max_deferrals"], 8)
        self.assertEqual(budget["defer_cap_seconds"], 60.0)


if __name__ == "__main__":
    unittest.main()
