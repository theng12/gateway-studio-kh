#!/usr/bin/env python3
"""Regression checks for atomic job claiming.

Run directly with the standard library::

    .venv/bin/python tests/test_job_claim.py

The worker test feeds it one real due() snapshot, then cancels the second job
while the first is executing. A cancelled row from that old snapshot must not
be claimed or dispatched.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path


# Set all writable paths before importing gateway modules. This test only uses
# JobStore and Worker, but their imports initialize the process-wide config and
# provider registry.
_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-job-claim-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402


def _submit(store: JobStore, label: str) -> dict:
    row, created = store.submit(
        modality="chat",
        model_id=label,
        params={},
        idempotency_key=f"claim-{label}",
        provider_id="missing-provider",
    )
    assert created
    return row


class JobClaimTests(unittest.TestCase):
    def make_store(self) -> JobStore:
        path = _TEMP_ROOT / f"{self._testMethodName}.sqlite3"
        return JobStore(path)

    def test_claim_is_atomic_and_only_queued_rows_can_be_claimed(self) -> None:
        store = self.make_store()
        try:
            first = _submit(store, "first")
            cancelled = _submit(store, "cancelled")
            due_snapshot = store.due(time.time() + 2.0)
            self.assertEqual(
                {row["id"] for row in due_snapshot}, {first["id"], cancelled["id"]}
            )

            store.cancel(cancelled["id"])
            self.assertFalse(store.mark_running(cancelled["id"]))
            self.assertEqual(store.get(cancelled["id"])["state"], "cancelled")

            self.assertTrue(store.mark_running(first["id"]))
            self.assertEqual(store.get(first["id"])["state"], "running")
            self.assertFalse(store.mark_running(first["id"]))
        finally:
            store.close()

    def test_worker_skips_cancelled_row_from_old_due_snapshot(self) -> None:
        store = self.make_store()
        worker = Worker(store, interval_s=0.001)
        try:
            first = _submit(store, "first-worker")
            cancelled = _submit(store, "cancelled-worker")
            snapshot = store.due(time.time() + 2.0)
            self.assertEqual(len(snapshot), 2)

            due_calls = 0

            def old_snapshot(_now: float | None = None) -> list[dict]:
                nonlocal due_calls
                due_calls += 1
                if due_calls == 1:
                    return snapshot
                if due_calls >= 100:
                    worker._stop.set()  # bounded guard for an unexpected stall
                return []

            store.due = old_snapshot  # type: ignore[method-assign]
            original_mark = store.mark_running

            def mark_and_stop(job_id: str) -> bool:
                result = original_mark(job_id)
                if job_id == cancelled["id"]:
                    worker._stop.set()  # stop after the second claim attempt
                return result

            store.mark_running = mark_and_stop  # type: ignore[method-assign]
            executed: list[str] = []

            def fake_execute(job: dict, _store: JobStore) -> None:
                executed.append(job["id"])
                if job["id"] == first["id"]:
                    _store.cancel(cancelled["id"])

            import gateway.worker as worker_module

            old_execute = worker_module.execute_job
            worker_module.execute_job = fake_execute
            try:
                worker._loop()  # deterministic: execute the real worker loop inline
            finally:
                worker_module.execute_job = old_execute

            # **`_loop` dispatches; it no longer executes.** It returns as soon as
            # the job is on its own thread, so this has to wait for the work rather
            # than read the loop's return as "it ran" — that assertion became a race
            # the moment the pool landed, and a gate that fails occasionally is one
            # people learn to re-run.
            self.assertTrue(worker.wait_idle(timeout=5.0), "the dispatched job never finished")
            self.assertEqual(executed, [first["id"]])
            self.assertEqual(store.get(cancelled["id"])["state"], "cancelled")
        finally:
            worker.stop()
            store.close()


if __name__ == "__main__":
    unittest.main()
