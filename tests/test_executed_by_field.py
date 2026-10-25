#!/usr/bin/env python3
"""Test that the admin jobs API exposes `executed_by` from result_json.payload.hub_machine.

The Hub reports which Mac ran each job as `machine` on each item; the Gateway stores it at
`result_json.payload.hub_machine` on success and on failure evidence. The admin API must
extract it and publish it as `executed_by` so the Jobs window can show which node did the work,
not just the site controller.

0.2.157+: a job that succeeded on a node shows the node name; a job with no hub_machine
(older Hub, not dispatched, or non-Hub lane) shows None and does not break.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-executed-by-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, api  # noqa: E402
from gateway.store import JobStore  # noqa: E402

store = api.store


def _submit_and_succeed(modality: str, result_payload: dict) -> str:
    """Submit a job and mark it succeeded with the given result payload."""
    row, _ = store.submit(
        modality=modality,
        model_id="test-model",
        params={"test": True},
        idempotency_key=f"exec-by-{time.time_ns()}",
        provider_id="test-provider",
    )
    store.mark_running(row["id"])
    store.succeed(row["id"], result=result_payload)
    return row["id"]


def _submit_and_fail(modality: str, evidence_payload: dict) -> str:
    """Submit a job and mark it failed with the given evidence payload."""
    row, _ = store.submit(
        modality=modality,
        model_id="test-model",
        params={"test": True},
        idempotency_key=f"exec-by-fail-{time.time_ns()}",
        provider_id="test-provider",
    )
    store.mark_running(row["id"])
    store.fail(row["id"], code="PROVIDER_FAILED", detail="test failure", evidence=evidence_payload)
    return row["id"]


class ExecutedByFieldTests(unittest.TestCase):
    def test_executed_by_is_extracted_from_hub_machine_on_success(self) -> None:
        """A succeeded job with hub_machine in result_json.payload shows executed_by."""
        job_id = _submit_and_succeed("image", {
            "payload": {
                "hub_machine": "terranash-1305",
                "hub_batch_id": "batch-123",
            },
            "text": "generated image data",
        })

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        response = client.get("/admin/api/jobs?limit=10")
        self.assertEqual(response.status_code, 200)

        jobs = response.json()["jobs"]
        target = next((j for j in jobs if j["id"] == job_id), None)
        self.assertIsNotNone(target, "job should be in the list")
        self.assertEqual(target["executed_by"], "terranash-1305")

    def test_executed_by_is_none_when_hub_machine_absent(self) -> None:
        """A succeeded job without hub_machine shows executed_by as None."""
        job_id = _submit_and_succeed("tts", {
            "payload": {
                "audio_duration_s": 5.2,
            },
            "text": "audio data",
        })

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        response = client.get("/admin/api/jobs?limit=10")
        self.assertEqual(response.status_code, 200)

        jobs = response.json()["jobs"]
        target = next((j for j in jobs if j["id"] == job_id), None)
        self.assertIsNotNone(target, "job should be in the list")
        self.assertIsNone(target["executed_by"])

    def test_executed_by_is_none_for_non_hub_provider(self) -> None:
        """A job from a non-Hub provider (no payload.hub_machine) shows executed_by as None."""
        job_id = _submit_and_succeed("chat", {
            "text": "chat response",
        })

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        response = client.get("/admin/api/jobs?limit=10")
        self.assertEqual(response.status_code, 200)

        jobs = response.json()["jobs"]
        target = next((j for j in jobs if j["id"] == job_id), None)
        self.assertIsNotNone(target, "job should be in the list")
        self.assertIsNone(target["executed_by"])

    def test_executed_by_is_extracted_from_failed_job_evidence(self) -> None:
        """A failed job with hub_machine in result_json.payload shows executed_by."""
        job_id = _submit_and_fail("video", {
            "hub_machine": "terranash-1310",
            "hub_batch_id": "batch-456",
            "run_seconds": 12.5,
        })

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        response = client.get("/admin/api/jobs?limit=10")
        self.assertEqual(response.status_code, 200)

        jobs = response.json()["jobs"]
        target = next((j for j in jobs if j["id"] == job_id), None)
        self.assertIsNotNone(target, "job should be in the list")
        self.assertEqual(target["executed_by"], "terranash-1310")

    def test_executed_by_handles_malformed_result_json(self) -> None:
        """A job with malformed result_json does not break the list endpoint."""
        # Manually insert a row with invalid JSON
        row, _ = store.submit(
            modality="image",
            model_id="test-model",
            params={"test": True},
            idempotency_key=f"exec-by-malformed-{time.time_ns()}",
            provider_id="test-provider",
        )
        store.mark_running(row["id"])
        # Corrupt the result_json directly
        store._conn.execute(
            "UPDATE jobs SET state='succeeded', result_json=?, updated_at=? WHERE id=?",
            ("{invalid json", time.time(), row["id"]),
        )
        store._conn.commit()

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        response = client.get("/admin/api/jobs?limit=10")
        self.assertEqual(response.status_code, 200, "malformed JSON should not break the endpoint")

        jobs = response.json()["jobs"]
        target = next((j for j in jobs if j["id"] == row["id"]), None)
        self.assertIsNotNone(target, "job should be in the list")
        self.assertIsNone(target["executed_by"])


if __name__ == "__main__":
    unittest.main()