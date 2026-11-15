"""A chat job on a `pool` provider, in flight at a restart, must be held — never left `running`.

Found on 1301 during the 0.2.259 install window (2026-11-15): eight `story-studio-llm` jobs on
provider `pool`, `dispatch_started_at` set, no upstream id, came back `running` with
`recovery_state NULL` after the restart and nothing ever wrote the rows again. The sweep
(`JobStore._recover_interrupted`) should send that shape to `_write_restart_hold`: it matches
neither the upstream-id branch nor the `studio-hub-` + resendable-modality branch.

This reproduces it on a throwaway database: dispatch a chat job to a `pool` provider, close the
store as a restart does, reopen, and read the row.

    .venv/bin/python tests/test_restart_pool_chat_hold.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-restart-pool-chat-")
os.environ["GATEWAY_HOST"] = "127.0.0.1"

from gateway import worker as worker_module  # noqa: E402
from gateway.store import JobStore  # noqa: E402

_ROOT = Path(_SANDBOX.name)


class _PoolRegistry:
    """The provider is a model pool, whose chat lane cannot be followed or re-sent by id."""

    @staticmethod
    def definitions():
        return []

    @staticmethod
    def definition(provider_id: str):
        return SimpleNamespace(id=provider_id, kind="pool", base_url="http://pool.invalid",
                               display_name="Pool")


class _Sends:
    def __init__(self) -> None:
        self.sending = threading.Event()
        self.release = threading.Event()

    def __call__(self, job, store, resume=False):
        store.begin_dispatch(job["id"])
        self.sending.set()
        self.release.wait(30)  # the send is unanswered: no upstream id, nothing to follow


class PoolChatRestartHold(unittest.TestCase):
    def setUp(self) -> None:
        self.path = _ROOT / f"{self._testMethodName}.sqlite3"
        self.store = JobStore(self.path)
        self.addCleanup(self.store.close)
        for item in (patch.object(worker_module, "registry", _PoolRegistry()),
                     patch("gateway.fleet.snapshot", lambda **_: {"locations": []})):
            item.start()
            self.addCleanup(item.stop)

    def test_a_pool_chat_send_in_flight_at_restart_is_held(self) -> None:
        row, _ = self.store.submit(modality="chat", model_id="story-studio-llm", params={},
                                   idempotency_key="pool-chat-1", provider_id="pool")
        job_id = row["id"]
        sends = _Sends()
        worker = worker_module.Worker(self.store, interval_s=0.02, pool_size=2)
        executing = patch.object(worker_module, "execute_job", sends)
        executing.start()
        self.addCleanup(executing.stop)
        self.addCleanup(sends.release.set)
        self.addCleanup(worker.stop)
        worker.start()
        self.assertTrue(sends.sending.wait(5), "the job reached dispatch")
        # The restart: the owning process is gone, so the next opener sweeps.
        self.store.close()
        reopened = JobStore(self.path)
        self.addCleanup(reopened.close)
        job = reopened.get(job_id)
        self.assertEqual(
            job["state"], "failed",
            f"a pool/chat job interrupted mid-send must be held, not left {job['state']!r}",
        )
        self.assertTrue(job["recovery_required"], "the restart hold must be recorded")
        self.assertIsNotNone(job.get("recovery_state"))


class TheQueueClaimWaitsForADrainingOwner(unittest.TestCase):
    """The 2026-11-15 1301 restart raced the old process's drain, lost the claim once, and skipped
    the sweep. A starting process must wait for the owner to go, then sweep."""

    def setUp(self) -> None:
        self.path = _ROOT / f"{self._testMethodName}.sqlite3"

    def test_a_start_that_races_the_drain_waits_then_owns_the_queue(self) -> None:
        owner = JobStore(self.path)
        self.assertTrue(owner.queue_owned)
        def release() -> None:
            time.sleep(1.0)
            owner.close()
        threading.Thread(target=release, daemon=True).start()
        with patch.dict(os.environ, {"GATEWAY_QUEUE_CLAIM_WAIT_S": "5"}):
            late = JobStore(self.path)
        self.addCleanup(late.close)
        self.assertTrue(late.queue_owned, "the late starter waited out the drain and claimed")

    def test_a_claim_that_never_succeeds_is_reported_not_silently_skipped(self) -> None:
        owner = JobStore(self.path)
        self.addCleanup(owner.close)
        with patch.dict(os.environ, {"GATEWAY_QUEUE_CLAIM_WAIT_S": "0"}):
            second = JobStore(self.path)
        self.addCleanup(second.close)
        self.assertFalse(second.queue_owned, "a process that cannot claim must know it")
        worker = worker_module.Worker(second, interval_s=0.02, pool_size=1)
        self.addCleanup(worker.stop)
        worker.start()  # must refuse, not dispatch
        self.assertIsNone(worker._thread, "a process without the queue must not dispatch")


class AnOrphanedRunningRowIsHeldNotLeft(unittest.TestCase):
    """The backstop: a `running` row with no thread behind it is held when a worker starts."""

    def setUp(self) -> None:
        self.path = _ROOT / f"{self._testMethodName}.sqlite3"
        self.store = JobStore(self.path)
        self.addCleanup(self.store.close)
        for item in (patch.object(worker_module, "registry", _PoolRegistry()),
                     patch("gateway.fleet.snapshot", lambda **_: {"locations": []})):
            item.start()
            self.addCleanup(item.stop)

    def test_a_running_row_with_no_worker_is_held(self) -> None:
        row, _ = self.store.submit(modality="chat", model_id="story-studio-llm", params={},
                                   idempotency_key="orphan-1", provider_id="pool")
        job_id = row["id"]
        # The shape 1301 was left in: running, dispatch opened, no upstream, no thread anywhere.
        self.store.mark_running(job_id)
        self.store.begin_dispatch(job_id)
        self.assertEqual(self.store.get(job_id)["state"], "running")
        worker = worker_module.Worker(self.store, interval_s=0.02, pool_size=1)
        self.addCleanup(worker.stop)
        worker.start()
        job = self.store.get(job_id)
        self.assertEqual(job["state"], "failed", "an orphan must be held, not left running")
        self.assertTrue(job["recovery_required"])


class APendingCancelSettlesWithTheRow(unittest.TestCase):
    """A cancel the dead worker never finalised must not survive the sweep as a stuck request."""

    def setUp(self) -> None:
        self.path = _ROOT / f"{self._testMethodName}.sqlite3"
        self.store = JobStore(self.path)
        self.addCleanup(self.store.close)
        for item in (patch.object(worker_module, "registry", _PoolRegistry()),
                     patch("gateway.fleet.snapshot", lambda **_: {"locations": []})):
            item.start()
            self.addCleanup(item.stop)

    def test_a_running_row_with_a_pending_cancel_is_cancelled_by_the_sweep(self) -> None:
        row, _ = self.store.submit(modality="chat", model_id="story-studio-llm", params={},
                                   idempotency_key="cancel-1", provider_id="pool")
        job_id = row["id"]
        self.store.mark_running(job_id)
        self.store.begin_dispatch(job_id)
        self.store.cancel(job_id, by="operator", reason="stall", origin="console")
        self.assertIsNotNone(self.store.get(job_id)["cancel_requested_at"])
        # The restart: the worker that would have answered is gone.
        self.store.close()
        reopened = JobStore(self.path)
        self.addCleanup(reopened.close)
        worker = worker_module.Worker(reopened, interval_s=0.02, pool_size=1)
        self.addCleanup(worker.stop)
        worker.start()
        job = reopened.get(job_id)
        self.assertEqual(job["state"], "cancelled",
                         "the pending cancel must settle, not be carried into a hold")
        self.assertFalse(job["recovery_required"])


if __name__ == "__main__":
    unittest.main()
