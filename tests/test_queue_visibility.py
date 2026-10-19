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

  * `queue_position` counts **waiting** jobs of the same modality — 1 is next, a dispatched job is
    not in this queue at all, and **a fresh `accepted` job is waiting too**: publishing only for a
    job that had already been deferred made a queued job look exactly like a running one, and made
    the position count only the deferred jobs while the fresh submissions the dispatcher takes
    first stood in front of it;
  * `queue_size` is the number bounding that position, so "3" can be read as "3 of 7";
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

# **Before the first `gateway` import.** Importing the package runs `gateway/runtime.py`, which
# opens the job store at `GATEWAY_DB`; with nothing set that is the owner's live database, and
# `JobStore.__init__` used to hold every job his serving gateway had in flight (measured
# 2026-10-19, from this file among others). The store now refuses to sweep while another process
# owns the queue, but a test must not open his database at all.
_SANDBOX = sandbox_env("gateway-queue-visibility-")

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
        # A job nobody is waiting on must not be given a queue position. It has to
        # be *dispatched* to prove that: every accepted job is waiting, whether or
        # not it has been deferred yet, so merely submitting one is not the case
        # this guards.
        self.assertTrue(self.store.mark_running(self.first["id"]))
        self.assertIsNone(
            self.store.queue_position(self.first["id"]),
            "a job that is running must not be told it is next in a queue",
        )
        self.assertIsNone(self.store.queue_rank(self.first["id"]))

    def test_a_fresh_submission_is_waiting_and_knows_its_place(self) -> None:
        """The H8 defect: a newly queued job published nothing and read as running."""
        self.assertEqual(self.store.queue_position(self.first["id"]), 1)
        self.assertEqual(
            self.store.queue_position(self.second["id"]), 2,
            "a fresh job stands behind the fresh job created before it",
        )
        self.assertEqual(
            self.store.queue_position(self.other_lane["id"]), 1,
            "a chat job is not behind image work: the queue is per modality",
        )
        rank = self.store.queue_rank(self.second["id"])
        self.assertEqual(rank, {"position": 2, "size": 2})

    def test_an_earlier_fresh_job_is_counted_ahead_of_a_deferred_one(self) -> None:
        """What `store.queue_position` got wrong: it counted only deferred jobs.

        The fresh job was created first and the dispatcher takes it first, so the
        deferred job is second — not "next" because it is the only one carrying a
        `defer_reason`.
        """
        self.store.defer(self.first["id"], delay_s=30, reason="LOCATION_NOT_READY: MACHINE_BUSY")
        self.assertEqual(self.store.queue_position(self.first["id"]), 1)
        self.assertEqual(
            self.store.queue_position(self.second["id"]), 2,
            "the fresh job created before it is ahead of it, not invisible",
        )
        rank = self.store.queue_rank(self.first["id"])
        self.assertEqual(rank, {"position": 1, "size": 2})

    def test_waiting_jobs_are_listed_in_the_order_they_will_be_dispatched(self) -> None:
        self.store.defer(self.first["id"], delay_s=30, reason="LOCATION_NOT_READY: MACHINE_BUSY")
        rows = self.store.waiting_jobs()
        self.assertEqual(
            [row["id"] for row in rows], [self.first["id"], self.second["id"], self.other_lane["id"]],
            "oldest first, and only the jobs that are still waiting",
        )


class TheEnvelopeSaysSoTest(unittest.TestCase):
    def _body(self, job: dict) -> dict:
        body: dict = {}
        with patch.object(api, "store", _Queue(3, 7)):
            api._publish_queue_evidence(body, job)
        return body

    def test_a_waiting_job_publishes_the_vocabulary_story_reads(self) -> None:
        job = {
            "id": "job-1",
            "state": "accepted",
            "defer_reason": "LOCATION_NOT_READY: MACHINE_BUSY",
            "next_action_at": time.time() + 42,
        }
        body = self._body(job)
        self.assertEqual(body["queue_reason"], "provider_pace")
        self.assertEqual(body["queue_position"], 3)
        self.assertEqual(body["queue_size"], 7)
        self.assertTrue(0 <= body["queue_eta_seconds"] <= 42)

    def test_a_fresh_waiting_job_publishes_its_place_without_claiming_pacing(self) -> None:
        """The position is published for every `accepted` job; pacing is not claimed.

        A job that has never been refused is queued, not paced by a provider, and
        `queue_reason` is Story's one word for pacing — so it stays absent while the
        two numbers that are true are published.
        """
        body = self._body({
            "id": "job-fresh", "state": "accepted", "defer_reason": None,
            "next_action_at": time.time() + 0.8,
        })
        self.assertEqual(body["queue_position"], 3)
        self.assertEqual(body["queue_size"], 7)
        self.assertNotIn("queue_reason", body)

    def test_a_job_that_is_not_waiting_publishes_nothing(self) -> None:
        for state in ("running", "succeeded", "failed", "cancelled", None):
            body = self._body({"id": "job-2", "state": state, "next_action_at": None})
            self.assertEqual(body, {}, f"{state!r} is not a queue at position zero")

    def test_a_passed_retry_time_does_not_publish_a_negative_wait(self) -> None:
        job = {"id": "job-3", "state": "accepted", "defer_reason": "x",
               "next_action_at": time.time() - 5}
        body = self._body(job)
        self.assertEqual(body["queue_eta_seconds"], 0)

    def test_a_store_with_only_the_old_reader_still_answers(self) -> None:
        """A store without `queue_rank` must not leave a waiting job unpublished."""
        job = {"id": "job-4", "state": "accepted", "defer_reason": "x", "next_action_at": None}
        body: dict = {}
        with patch.object(api, "store", _OldQueue(4)):
            api._publish_queue_evidence(body, job)
        self.assertEqual(body["queue_position"], 4)
        self.assertNotIn("queue_size", body)


class _Queue:
    """Stands in for the store's queue reader."""

    def __init__(self, position: int, size: int = 1) -> None:
        self.position = position
        self.size = size

    def queue_rank(self, job_id: str) -> dict:
        return {"position": self.position, "size": self.size}


class _OldQueue:
    """A store from before `queue_rank`: one number and no bounding count."""

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
        # Read from the constants, never restated: the shipped wait was 8 deferrals at a
        # minute, and the owner's decision C moved it (a fleet running two-hour video jobs
        # needs a longer hold). A literal here would turn that decision into a red test —
        # the trap `tests/mock_upstream.py` names for the Hub's own numbers.
        budget = concurrency.queue_wait_budget()
        self.assertEqual(budget["max_deferrals"], concurrency.DEFAULT_MAX_DEFERRALS)
        self.assertEqual(budget["defer_cap_seconds"], concurrency.DEFAULT_DEFER_CAP_S)
        self.assertEqual(budget["source"], concurrency.SOURCE_GATEWAY)

    def test_an_operator_can_wait_longer_than_the_shipped_default(self) -> None:
        """The point of the settings: a busy fleet must be waited out, not failed."""
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
        self.assertEqual(budget["max_deferrals"], concurrency.DEFAULT_MAX_DEFERRALS)
        self.assertEqual(budget["defer_cap_seconds"], concurrency.DEFAULT_DEFER_CAP_S)


if __name__ == "__main__":
    unittest.main()
