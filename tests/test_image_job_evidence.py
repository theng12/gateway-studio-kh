#!/usr/bin/env python3
"""Image intent survives replays without claiming the Hub applied submitted references."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-image-job-evidence-")

from gateway.store import JobStore


class ImageJobEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(_SANDBOX.name) / f"{self.id()}.sqlite3")
        self.addCleanup(self.store.close)

    def submit(self, params=None, key=None, modality="image"):
        return self.store.submit(
            modality=modality, model_id="fixture", params=params or {}, idempotency_key=key,
        )

    def test_text_job_records_zero_submitted_references(self) -> None:
        job, created = self.submit({"prompt": "a tree"})
        self.assertTrue(created)
        self.assertEqual(job["image_mode"], "text-to-image")
        self.assertEqual(job["submitted_reference_count"], 0)
        self.assertIsNone(job["result_json"])

    def test_reference_job_records_staged_count(self) -> None:
        job, _ = self.submit({"_reference_digests": ["a", "b"]})
        self.assertEqual(job["image_mode"], "reference-to-image")
        self.assertEqual(job["submitted_reference_count"], 2)
        self.assertIsNone(job["result_json"], "submission is no proof of applied references")

    def test_empty_reference_request_does_not_become_text_mode(self) -> None:
        job, _ = self.submit({"_reference_digests": []})
        self.assertEqual(job["image_mode"], "reference-to-image")
        self.assertEqual(job["submitted_reference_count"], 0)

    def test_caller_cannot_spoof_recorded_intent(self) -> None:
        job, _ = self.submit({"_reference_digests": ["a"], "image_mode": "text-to-image",
                              "submitted_reference_count": 99})
        self.assertEqual(job["image_mode"], "reference-to-image")
        self.assertEqual(job["submitted_reference_count"], 1)

    def test_unstaged_reference_input_stays_unknown(self) -> None:
        job, _ = self.submit({"reference_asset_ids": ["asset"]})
        self.assertIsNone(job["image_mode"])
        self.assertIsNone(job["submitted_reference_count"])

    def test_replay_retains_original_intent(self) -> None:
        original, _ = self.submit({"_reference_digests": ["a"]}, key="replay")
        replay, created = self.submit({}, key="replay")
        self.assertFalse(created)
        self.assertEqual(replay["id"], original["id"])
        self.assertEqual(replay["image_mode"], "reference-to-image")
        self.assertEqual(replay["submitted_reference_count"], 1)

    def test_other_modalities_have_no_image_evidence(self) -> None:
        job, _ = self.submit(modality="chat")
        self.assertIsNone(job["image_mode"])
        self.assertIsNone(job["submitted_reference_count"])

    def test_admin_publishes_intent_and_filters_without_inventing_applied_count(self) -> None:
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from gateway import admin, api
        reference, _ = self.submit({"_reference_digests": ["a"]})
        self.submit({"prompt": "a tree"})
        with patch.object(admin, "store", self.store):
            client = TestClient(api.app, client=("127.0.0.1", 12345))
            self.addCleanup(client.close)
            response = client.get('/admin/api/jobs?image_mode=reference-to-image&limit=1')
            self.assertEqual(response.status_code, 200, response.text)
            jobs = response.json()["jobs"]
            self.assertEqual([job["id"] for job in jobs], [reference["id"]])
            self.assertEqual(jobs[0]["image_mode"], "reference-to-image")
            self.assertEqual(jobs[0]["submitted_reference_count"], 1)
            self.assertIsNone(jobs[0]["reference_count"])
            self.assertEqual(client.get('/admin/api/jobs?image_mode=edit').status_code, 422)

    def test_mode_filter_applies_before_page_limit(self) -> None:
        reference, _ = self.submit({"_reference_digests": ["a"]})
        self.submit({"prompt": "a tree"})
        rows = self.store.list_jobs(image_mode="reference-to-image", limit=1)
        self.assertEqual([row["id"] for row in rows], [reference["id"]])
        self.assertEqual(self.store.list_jobs(image_mode="text-to-image")[0]["submitted_reference_count"], 0)

    def test_reference_intent_with_missing_staging_never_becomes_text(self) -> None:
        from gateway.adapters.studio_hub import BackendError, _image_reference
        for params in ({"_reference_digests": []}, {"reference_asset_ids": ["asset"]},
                       {"_reference_digests": ["digest"], "_reference_paths": []}):
            with self.subTest(params=params), self.assertRaises(BackendError) as caught:
                _image_reference(SimpleNamespace(params=params))
            self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.assertIsNone(_image_reference(SimpleNamespace(params={})))

    def test_legacy_row_is_not_backfilled_as_text(self) -> None:
        self.store._conn.execute(
            "INSERT INTO jobs (id, modality, model_id, state, params, created_at, updated_at) "
            "VALUES ('legacy', 'image', 'fixture', 'succeeded', '{}', 1, 1)"
        )
        self.store._ensure_columns()
        job = self.store.get("legacy")
        self.assertIsNone(job["image_mode"])
        self.assertIsNone(job["submitted_reference_count"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
