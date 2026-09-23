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

from gateway.adapters.base import BackendError, BackendResult  # noqa: E402
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
    ) -> None:
        self.calls = 0
        self.checkpoint = checkpoint
        self.upstream_accepted = upstream_accepted
        self.status = status
        self.retryable = retryable
        self.code = code

    def generate(self, request):
        self.calls += 1
        if self.checkpoint:
            request.checkpoint("upstream-123")
        raise BackendError(
            "provider connection dropped",
            code=self.code,
            status=self.status,
            retryable=self.retryable,
            upstream_accepted=self.upstream_accepted,
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

    def test_reopen_fails_claimed_work_as_unknown_without_dispatching_again(self) -> None:
        store = self.make_store("reopen")
        row = self.submit(store, "reopen-unknown")
        self.assertTrue(store.mark_running(row["id"]))
        store.close()

        reopened = JobStore(store.path)
        try:
            recovered = reopened.get(row["id"])
            self.assertEqual(recovered["state"], "failed")
            self.assertEqual(recovered["error_code"], "UNKNOWN_OUTCOME")
            self.assertEqual(recovered["recovery_state"], "unknown_outcome")
            self.assertIn("will not dispatch", recovered["error_detail"].lower())
            self.assertEqual(reopened.due(time.time() + 60), [])
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


if __name__ == "__main__":
    unittest.main()
