#!/usr/bin/env python3
"""Focused lifecycle checks for interrupted jobs and late provider outcomes."""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-job-recovery-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os

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

from gateway.adapters.base import BackendError, BackendResult, note_request_started  # noqa: E402
from gateway.store import JobStore, artifacts_of  # noqa: E402
import gateway.worker as worker_module  # noqa: E402


class _RetryingAdapter:
    def __init__(
        self,
        *,
        checkpoint: bool = False,
        upstream_accepted: bool | None = None,
        status: int | None = None,
        retryable: bool = True,
        code: str = "PROVIDER_UNAVAILABLE",
        upstream_settled: bool = False,
    ) -> None:
        self.calls = 0
        self.checkpoint = checkpoint
        self.upstream_accepted = upstream_accepted
        self.status = status
        self.retryable = retryable
        self.code = code
        self.upstream_settled = upstream_settled

    def generate(self, request):
        self.calls += 1
        # A dropped connection means a request went out; the worker only treats a failure as
        # possibly-accepted when something was actually sent.
        note_request_started()
        if self.checkpoint:
            request.checkpoint("upstream-123")
        raise BackendError(
            "provider connection dropped",
            code=self.code,
            status=self.status,
            retryable=self.retryable,
            upstream_accepted=self.upstream_accepted,
            upstream_settled=self.upstream_settled,
        )


class _Registry:
    credentials = SimpleNamespace(
        is_enabled=lambda _provider_id: True,
        resolved=lambda _provider_id: {},
    )
    models = SimpleNamespace(
        enabled_models=lambda _modality: [
            (
                SimpleNamespace(id="mock-provider", base_url="http://mock", display_name="Mock"),
                {"model_id": "mock-model"},
            )
        ]
    )

    @staticmethod
    def definition(_provider_id):
        return SimpleNamespace(
            id="mock-provider", base_url="http://mock", display_name="Mock"
        )


class JobRecoveryTests(unittest.TestCase):
    def make_store(self, name: str) -> JobStore:
        return JobStore(_TEMP_ROOT / f"{name}.sqlite3")

    @staticmethod
    def submit(store: JobStore, key: str, *, params: dict | None = None) -> dict:
        row, created = store.submit(
            modality="chat",
            model_id="mock-model",
            params=params or {},
            idempotency_key=key,
            provider_id="mock-provider",
        )
        assert created
        return row

    def test_reopen_requeues_work_that_never_left_this_process(self) -> None:
        """**This test used to assert the opposite, and the change is deliberate** (C2/M5).

        A restart failed *everything* `running` as an unknown-outcome hold, including work the
        adapter had never been called for — `dispatch_started_at IS NULL`. Six of the live holds
        were exactly that: `upstream_job_id: NULL`, nothing running anywhere, and an operator
        asked to reconcile work that had never been sent. A job that provably never left this
        process goes back in the queue.

        The next test is the other half, and it is the one that must never change: a job that
        *had* been dispatched is a hold, because re-running it could render and bill twice.
        """
        store = self.make_store("reopen")
        row = self.submit(store, "reopen-unknown")
        self.assertTrue(store.mark_running(row["id"]))
        store.close()

        reopened = JobStore(store.path)
        try:
            recovered = reopened.get(row["id"])
            self.assertEqual(recovered["state"], "accepted",
                             "a job that was never dispatched is not a recovery hold")
            self.assertIsNone(recovered["recovery_state"])
            self.assertIsNone(recovered["error_code"])
            self.assertEqual([job["id"] for job in reopened.due(time.time() + 60)], [row["id"]],
                             "and it is due again, so the queue picks it up")
        finally:
            reopened.close()

    def test_reopen_still_holds_work_that_may_have_reached_a_worker(self) -> None:
        # **The line, and why it is drawn at `dispatch_started_at`.** An upstream id proves a
        # container was created; no id is *not* proof that none was, because the one window with
        # no id and real work in it is a POST that succeeded whose answer was lost. Before
        # dispatch began there is no such window. Never replay what might be running.
        store = self.make_store("reopen-dispatched")
        row = self.submit(store, "reopen-dispatched")
        self.assertTrue(store.mark_running(row["id"]))
        self.assertTrue(store.begin_dispatch(row["id"]))
        store.close()

        reopened = JobStore(store.path)
        try:
            recovered = reopened.get(row["id"])
            self.assertEqual(recovered["state"], "failed")
            self.assertEqual(recovered["error_code"], "UNKNOWN_OUTCOME")
            self.assertEqual(recovered["recovery_state"], "unknown_outcome")
            self.assertIn("will not dispatch", recovered["error_detail"].lower())
            self.assertEqual(reopened.due(time.time() + 60), [],
                             "a dispatched job is never re-queued automatically")
        finally:
            reopened.close()

    def test_reopen_records_the_upstream_attempt_the_hold_is_waiting_on(self) -> None:
        """**The hold a restart leaves must name what it is waiting on.**

        The sweep described the batch in prose and left `recovery_context` null,
        so the operator's discard had nothing to aim at: `discard-uncertain`
        answered `RECOVERY_CONTEXT_MISSING` forever and the Jobs window offered no
        button, which left the job unresolvable short of editing the database. The
        context is recorded in the same shape the Hub adapter uses — the batch the
        provider accepted, and the item index the discard path already defaults to.
        """
        store = self.make_store("restart-context")
        row = self.submit(store, "restart-context")
        self.assertTrue(store.mark_running(row["id"]))
        store.record_upstream_acceptance(row["id"], "hub-batch-11")
        store.close()

        reopened = JobStore(store.path)
        try:
            # An upstream id means the work is still running there: the store leaves it `running`
            # for the worker to resume, and only a lane that cannot be polled is held.
            self.assertEqual(reopened.get(row["id"])["state"], "running")
            self.assertEqual(reopened.interrupted, [row["id"]])
            reopened.hold_interrupted(row["id"])
            recovered = reopened.get(row["id"])
            self.assertTrue(recovered["recovery_required"])
            self.assertEqual(
                recovered["recovery_context"],
                {
                    "upstream_job_id": "hub-batch-11",
                    "upstream_item_index": 0,
                    "recorded_by": "gateway_restart",
                },
            )
        finally:
            reopened.close()

    def test_the_hold_the_hub_adapter_writes_can_be_discarded(self) -> None:
        """**The reader has to know every spelling the writers use.**

        A restart writes `upstream_job_id` / `upstream_item_index`; the Hub
        adapter writes `batch_id` / `item_index`. `_named_attempt` read only the
        first pair, so the one route that clears a hold refused a hold whose
        attempt was named perfectly well — measured on a real uncertain
        submission whose context read
        `{"batch_id": "9a9e74a3a5", "item_index": 0}` while the reader looked for
        keys nothing had written, and answered `RECOVERY_CONTEXT_MISSING` with the
        batch sitting right there. `worker.py` reads `batch_id` too, so the gap
        was in this reader alone.
        """
        from gateway.api import _named_attempt

        self.assertEqual(
            ("9a9e74a3a5", 0),
            _named_attempt({"recovery_context": {"batch_id": "9a9e74a3a5", "item_index": 0}}),
        )
        self.assertEqual(
            ("9a9e74a3a5", 2),
            _named_attempt({"recovery_context": {"batch_id": "9a9e74a3a5", "item_index": 2}}),
        )
        # The restart's spelling keeps working, and neither is preferred by luck.
        self.assertEqual(
            ("hub-batch-11", 0),
            _named_attempt({"recovery_context": {"upstream_job_id": "hub-batch-11"}}),
        )
        # A hold that names nothing is still refused, rather than aimed at batch "".
        self.assertIsNone(_named_attempt({"recovery_context": {"reason": "no batch here"}}))
        self.assertIsNone(_named_attempt({"recovery_context": None}))

    def test_a_restart_before_dispatch_records_no_attempt(self) -> None:
        # Nothing was accepted upstream, so there is nothing to name — and nothing to reconcile
        # either. **Absence stays absent rather than becoming a batch**: the job is not a hold at
        # all, so it has no context to resolve and must not be given an empty one that reads as
        # "a batch we could not name".
        store = self.make_store("restart-no-upstream")
        row = self.submit(store, "restart-no-upstream")
        self.assertTrue(store.mark_running(row["id"]))
        store.close()

        reopened = JobStore(store.path)
        try:
            recovered = reopened.get(row["id"])
            self.assertIsNone(recovered["recovery_context"])
            self.assertEqual(recovered["state"], "accepted")
        finally:
            reopened.close()

    def test_checkpoint_and_artifact_survive_reopen_as_recovery_data(self) -> None:
        upload = _TEMP_ROOT / "data" / "artifacts" / "uploads" / "old.wav"
        output = _TEMP_ROOT / "data" / "artifacts" / "late.wav"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"input")
        output.write_bytes(b"late output")
        store = self.make_store("checkpoint")
        row = self.submit(store, "checkpoint-unknown", params={"_upload_path": str(upload)})
        self.assertTrue(store.mark_running(row["id"]))
        store.record_upstream_acceptance(row["id"], "hub-batch-7")
        store.record_artifacts(
            row["id"],
            [{"asset_id": "0", "path": str(output), "media_type": "audio/wav"}],
        )
        store.close()

        reopened = JobStore(store.path)
        try:
            self.assertEqual(reopened.get(row["id"])["state"], "running", "resumed, not failed")
            reopened.hold_interrupted(row["id"])
            recovered = reopened.get(row["id"])
            self.assertEqual(recovered["state"], "failed")
            self.assertEqual(recovered["upstream_job_id"], "hub-batch-7")
            self.assertEqual(artifacts_of(recovered)[0]["path"], str(output))
            self.assertIn(str(upload), reopened.protected_input_paths())
        finally:
            reopened.close()

    def test_running_cancel_is_a_request_and_completion_wins_with_output(self) -> None:
        output = _TEMP_ROOT / "data" / "artifacts" / "completed-after-cancel.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio")
        store = self.make_store("cancel-request")
        try:
            row = self.submit(store, "cancel-request")
            self.assertTrue(store.mark_running(row["id"]))
            requested = store.cancel(row["id"])
            self.assertEqual(requested["state"], "running")
            self.assertEqual(requested["cancel_state"], "requested")
            self.assertIsNotNone(requested["cancel_requested_at"])

            store.succeed(
                row["id"],
                asset_path=str(output),
                media_type="audio/wav",
                artifacts=[
                    {"asset_id": "0", "path": str(output), "media_type": "audio/wav"}
                ],
            )
            completed = store.get(row["id"])
            self.assertEqual(completed["state"], "succeeded")
            self.assertEqual(artifacts_of(completed)[0]["path"], str(output))
        finally:
            store.close()

    def test_terminal_cancel_stays_cancelled_but_late_output_is_discoverable(self) -> None:
        output = _TEMP_ROOT / "data" / "artifacts" / "late-after-queued-cancel.wav"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"audio")
        store = self.make_store("cancel-late")
        try:
            row = self.submit(store, "cancel-late")
            self.assertEqual(store.cancel(row["id"])["state"], "cancelled")
            store.succeed(
                row["id"],
                asset_path=str(output),
                media_type="audio/wav",
                artifacts=[
                    {"asset_id": "0", "path": str(output), "media_type": "audio/wav"}
                ],
            )
            late = store.get(row["id"])
            self.assertEqual(late["state"], "cancelled")
            self.assertEqual(late["late_result_state"], "succeeded")
            self.assertEqual(artifacts_of(late)[0]["path"], str(output))
        finally:
            store.close()

    def test_retryable_adapter_failure_is_persisted_without_a_second_dispatch(self) -> None:
        store = self.make_store("retryable")
        adapter = _RetryingAdapter(upstream_accepted=False)
        row = self.submit(store, "retryable-failure")
        self.assertTrue(store.mark_running(row["id"]))
        try:
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)
            failed = store.get(row["id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["retryable"])
            self.assertEqual(failed["recovery_state"], "retryable")
            self.assertEqual(store.due(time.time() + 60), [])
        finally:
            store.close()

    def test_default_retryable_failure_after_dispatch_is_unknown_without_replay(self) -> None:
        store = self.make_store("retryable-ambiguous")
        adapter = _RetryingAdapter()
        row = self.submit(store, "retryable-ambiguous")
        self.assertTrue(store.mark_running(row["id"]))
        try:
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)
            failed = store.get(row["id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["retryable"])
            self.assertEqual(failed["recovery_state"], "unknown_outcome")
            self.assertEqual(failed["error_code"], "PROVIDER_UNAVAILABLE")
            self.assertEqual(store.due(time.time() + 60), [])
        finally:
            store.close()

    def test_a_definite_4xx_refusal_is_never_an_unknown_hold(self) -> None:
        # A 400 is the provider's own refusal: nothing ran, nothing was charged.
        # It must settle as an ordinary failure, not a recovery hold the owner
        # cannot clear. This is the live `gpt-6-luna` case — OpenAI answered
        # "Unsupported value: 'temperature' does not support 0.7 with this model"
        # and the job became a permanent unknown_outcome.
        store = self.make_store("definite-4xx")
        adapter = _RetryingAdapter(status=400, retryable=False, code="PROVIDER_FAILED")
        row = self.submit(store, "definite-4xx")
        self.assertTrue(store.mark_running(row["id"]))
        try:
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)
            failed = store.get(row["id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["error_code"], "PROVIDER_FAILED")
            self.assertNotEqual(failed["recovery_state"], "unknown_outcome")
            self.assertIsNone(failed["recovery_state"])
        finally:
            store.close()

    def test_a_settled_upstream_failure_is_not_an_unknown_hold(self) -> None:
        """A batch id is a container, not a promise that work is pending.

        Studio Hub creates a batch and only then validates the item, so every refused or failed
        item leaves an upstream id on the job. Treating that id as ambiguity turned ordinary
        failures — a validation refusal, a model error, an engine out of memory — into durable
        `unknown_outcome` holds asking the operator to reconcile work that never ran. Measured
        live 2026-09-26 on a 422 geometry refusal and a 409 "machine busy" refusal.
        """
        store = self.make_store("settled")
        adapter = _RetryingAdapter(
            checkpoint=True, upstream_settled=True, retryable=False, code="PROVIDER_FAILED"
        )
        row = self.submit(store, "settled-item-failure")
        self.assertTrue(store.mark_running(row["id"]))
        try:
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)
            failed = store.get(row["id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["error_code"], "PROVIDER_FAILED")
            # The id is still recorded — it is how the operator finds the batch — but it no longer
            # implies that something may be running.
            self.assertEqual(failed["upstream_job_id"], "upstream-123")
            self.assertIsNone(failed["recovery_state"])
        finally:
            store.close()

    def test_accepted_then_lost_adapter_response_is_unknown_and_not_replayed(self) -> None:
        store = self.make_store("accepted")
        adapter = _RetryingAdapter(checkpoint=True)
        row = self.submit(store, "accepted-unknown")
        self.assertTrue(store.mark_running(row["id"]))
        path = store.path
        closed = False
        try:
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)
            failed = store.get(row["id"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["upstream_job_id"], "upstream-123")
            self.assertEqual(failed["recovery_state"], "unknown_outcome")
            self.assertEqual(store.due(time.time() + 60), [])
            store.close()
            closed = True
            reopened = JobStore(path)
            try:
                self.assertEqual(reopened.due(time.time() + 60), [])
                self.assertEqual(reopened.get(row["id"])["state"], "failed")
            finally:
                reopened.close()
        finally:
            if not closed:
                store.close()

    def test_completion_then_cancel_does_not_change_success(self) -> None:
        store = self.make_store("completion-first")
        try:
            row = self.submit(store, "completion-first")
            self.assertTrue(store.mark_running(row["id"]))
            store.succeed(row["id"], result={"text": "done"})
            self.assertEqual(store.cancel(row["id"])["state"], "succeeded")
        finally:
            store.close()

    def test_late_failure_cannot_replace_a_confirmed_cancel(self) -> None:
        store = self.make_store("late-failure")
        try:
            row = self.submit(store, "late-failure")
            self.assertEqual(store.cancel(row["id"])["state"], "cancelled")
            store.fail(row["id"], code="LATE_FAILURE", detail="provider failed later")
            failed_late = store.get(row["id"])
            self.assertEqual(failed_late["state"], "cancelled")
            self.assertEqual(failed_late["late_result_state"], "failed")
            self.assertEqual(failed_late["late_error_code"], "LATE_FAILURE")
        finally:
            store.close()


    def test_a_second_opener_is_not_a_restart(self) -> None:
        """**Opening the database must not orphan the work of the process serving it.**

        The sweep in `JobStore.__init__` is for one thing: a job was in flight when the process
        that owned it died. Running it for *every* opener made the sentence false and the damage
        real — measured on the owner's gateway 2026-10-19. `tests/test_fleet_cancel.py`,
        `tests/test_job_caller.py` and `tests/test_queue_visibility.py` each import `gateway.api`
        (hence `gateway.runtime`, hence this store) before redirecting `GATEWAY_*`, so they bound
        `$HOME`'s Application Support — the live data directory — and held every `running` chat job
        as `failed / UNKNOWN_OUTCOME`, repeatedly, while pid 12123 had been up since 08:15 and its
        job threads were still running and still answered. A gateway spawn that cannot bind 8787
        does the same: uvicorn imports the app before it tries the port.
        """
        store = self.make_store("second-opener")
        row = self.submit(store, "second-opener")
        self.assertTrue(store.mark_running(row["id"]))
        self.assertTrue(store.begin_dispatch(row["id"]))

        other = JobStore(store.path)  # a second gateway, a CLI, a fixture, a test module
        try:
            live = other.get(row["id"])
            self.assertEqual(live["state"], "running", "the serving process's job is still running")
            self.assertIsNone(live["recovery_state"], "nothing says its outcome is unknown")
            self.assertIsNone(live["error_code"])
            self.assertEqual(store.get(row["id"])["state"], "running",
                             "and the serving process reads the same row")
        finally:
            other.close()

        # A real restart still holds it: the owner is gone, so the next starter owns the queue.
        store.close()
        reopened = JobStore(store.path)
        try:
            recovered = reopened.get(row["id"])
            self.assertEqual(recovered["state"], "failed")
            self.assertEqual(recovered["error_code"], "UNKNOWN_OUTCOME")
            self.assertEqual(recovered["recovery_state"], "unknown_outcome")
            self.assertIn("restarted", recovered["error_detail"],
                          "and the sentence is true here, because the owner really did go")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
