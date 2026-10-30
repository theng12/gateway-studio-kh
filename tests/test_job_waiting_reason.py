#!/usr/bin/env python3
"""A queued job says why it is waiting — on the row and on the wire.

**Measured 2026-10-30.** A research chat call was accepted, the Hub answered
`MACHINE_BUSY` ("every Mac with this Chat model is busy") for twenty minutes, and the job
was cancelled with nothing visible: `waiting` was published for `running` jobs only, so
`accepted` read as "no worker, no reason". The deferral sentence had been on the row
(`defer_reason`) the whole time.

Three facts are pinned here:

1. an `accepted` job with a `defer_reason` publishes it as `waiting`, with the
   scheduler's own `next_action_at`;
2. a job that was never deferred publishes no invented reason;
3. a fleet job the dispatcher could not admit gets that sentence written once —
   `worker._note_waiting` — and is not rewritten every 0.4 s pass while it stays the same.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-job-waiting-")

from gateway import api, fleet  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402


def _job(**overrides) -> dict:
    row = {
        "id": "job-1", "state": "accepted", "modality": "chat",
        "provider_id": "studio-hub-2c7a2403564b",
        "model_id": "mlx-community/gemma-4-26B-A4B-it-qat-4bit",
        "dispatch_started_at": None, "upstream_job_id": None,
        "waiting": None, "defer_reason": None, "next_action_at": None,
        "cancel_requested": False,
    }
    row.update(overrides)
    return row


class TheEnvelopePublishesTheReasonTest(unittest.TestCase):
    def test_a_deferred_job_says_why_and_when_it_is_tried_again(self) -> None:
        fields = api._contract_fields(_job(
            defer_reason="the location cannot serve this model right now (MACHINE_BUSY): "
                         "every Mac with this Chat model is busy",
            next_action_at=1_793_345_000.0))
        self.assertEqual(fields["dispatch_state"], "pre_dispatch")
        self.assertIsNotNone(fields["waiting"])
        self.assertIn("MACHINE_BUSY", fields["waiting"]["reason"])
        self.assertTrue(fields["waiting"]["next_try_at"].endswith("Z"))
        self.assertTrue(fields["cancellable"], "a queued job can still be cancelled")

    def test_a_job_that_was_never_deferred_publishes_no_reason(self) -> None:
        self.assertIsNone(api._contract_fields(_job())["waiting"])

    def test_a_running_jobs_own_waiting_record_still_wins(self) -> None:
        fields = api._contract_fields(_job(
            state="running", dispatch_started_at=1_793_345_000.0,
            waiting={"reason": "waiting for a free Mac", "since": 1_793_345_000.0}))
        self.assertEqual(fields["waiting"]["reason"], "waiting for a free Mac")


class TheDispatcherWritesItOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="waiting-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.job, _ = self.store.submit(
            modality="chat", model_id="mlx-community/gemma-4-26B-A4B-it-qat-4bit",
            provider_id="studio-hub-2c7a2403564b", params={"messages": []},
            idempotency_key="waiting-1",
        )
        self.worker = Worker(self.store)

    def _row(self) -> dict:
        return self.store.get(self.job["id"])

    def test_a_fleet_job_with_no_room_gets_the_fleet_sentence(self) -> None:
        with patch.object(fleet, "site_wait_reason",
                          lambda pid, mid: "waiting for Terranash-1600 to answer — slow"):
            self.worker._note_waiting(self._row())
        row = self._row()
        self.assertEqual(row["defer_reason"], "waiting for Terranash-1600 to answer — slow")
        self.assertIsNotNone(row["next_action_at"], "the job stays due while it waits")

    def test_the_same_sentence_is_not_rewritten_on_the_next_pass(self) -> None:
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "waiting for a free Mac"):
            self.worker._note_waiting(self._row())
            first = self._row()["updated_at"]
            self.worker._note_waiting(self._row())
        self.assertEqual(first, self._row()["updated_at"],
                         "a stable wait costs one UPDATE, not one per dispatcher pass")

    def test_a_changed_site_state_updates_the_sentence(self) -> None:
        with patch.object(fleet, "site_wait_reason", lambda pid, mid: "waiting for a free Mac"):
            self.worker._note_waiting(self._row())
        with patch.object(fleet, "site_wait_reason",
                          lambda pid, mid: "waiting for Terranash-1600 to answer — slow"):
            self.worker._note_waiting(self._row())
        self.assertIn("Terranash-1600", self._row()["defer_reason"])

    def test_a_non_fleet_job_is_left_alone(self) -> None:
        other, _ = self.store.submit(
            modality="chat", model_id="model", provider_id="openrouter-main",
            params={}, idempotency_key="waiting-2",
        )
        self.worker._note_waiting(self.store.get(other["id"]))
        self.assertIsNone(self.store.get(other["id"])["defer_reason"])


if __name__ == "__main__":
    unittest.main()
