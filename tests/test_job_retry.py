#!/usr/bin/env python3
"""Per-job retry (0.2.272): one terminal job id in, one new request out, siblings untouched.

Production context (2026-11-18): a Mac held eleven jobs for 137 minutes with no way to redo a
single one, so an operator had to resubmit a whole batch or leave the row stuck. `POST
/admin/api/jobs/{id}/retry` queues one finished job again as a fresh row carrying `retry_of`,
refuses a non-terminal or still-held row with a plain sentence, never mutates the old row, and
counts the retry as a new job.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-job-retry-"))
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
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_API_KEY": "test-retry-secret-key",
        "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
        "GATEWAY_VOICES": str(_TEMP_ROOT / "voices.json"),
        "GATEWAY_VOICES_OUT": str(_TEMP_ROOT / "voices.json"),
    }
)

HUB = SimpleNamespace(
    id="studio-hub-studio-1200",
    kind="studio_hub",
    display_name="studio-1200",
    base_url="http://127.0.0.1:47873",
)
MODEL = {"model_id": "qwen-image", "raw": {"capabilities": ["txt2img"]}}


class JobRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        self.api = api
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.headers = {"Authorization": "Bearer test-retry-secret-key"}

    def tearDown(self) -> None:
        self.client.close()

    def _patches(self):
        return (
            patch.object(self.api.catalog, "resolve", return_value=(HUB, MODEL)),
            patch.object(self.api.fleet, "advertises", return_value=True),
            patch.object(self.api, "_refuse_when_the_queue_is_full", return_value=None),
            patch.object(self.api, "_refuse_when_the_disk_is_low", return_value=None),
        )

    def _submit_one(
        self,
        key: str,
        *,
        prompt: str = "temple at dawn",
        group: str = "ep-04/scene-2",
        priority: str = "background",
        guest_id: str | None = None,
    ) -> dict:
        p1, p2, p3, p4 = self._patches()
        requested_by = (
            {"role": "guest", "guest_id": guest_id, "label": "Sophea"}
            if guest_id
            else {"role": "key", "label": "Story Studio"}
        )
        with p1, p2, p3, p4:
            row, created = self.api._submit(
                "image",
                "studio-hub::qwen-image",
                {"prompt": prompt, "aspect_ratio": "16:9", "job_group": group, "priority": priority},
                key,
                caller="story-studio",
                requested_by=requested_by,
            )
        self.assertTrue(created)
        return row

    def _retry(self, job_id: str):
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            return self.client.post(f"/admin/api/jobs/{job_id}/retry", headers=self.headers)

    def test_cancelling_one_job_of_a_batch_and_retrying_it_runs_one_new_job_with_siblings_untouched(
        self,
    ) -> None:
        first = self._submit_one(f"{self.id()}-1", prompt="shot 1")
        second = self._submit_one(f"{self.id()}-2", prompt="shot 2")
        third = self._submit_one(f"{self.id()}-3", prompt="shot 3")

        # Sibling 1 succeeded; sibling 3 is still running; only job 2 is cancelled and retried.
        self.assertTrue(self.api.store.mark_running(first["id"]))
        self.api.store.succeed(first["id"], result={"payload": {"ok": True}}, media_type="image/png")
        self.assertTrue(self.api.store.mark_running(third["id"]))

        cancel_res = self.client.post(
            f"/v1/images/{second['id']}/cancel",
            headers=self.headers,
            json={"reason": "stuck on studio-1200"},
        )
        self.assertEqual(cancel_res.status_code, 200, cancel_res.text)
        self.assertEqual(cancel_res.json()["state"], "cancelled")

        first_before = dict(self.api.store.get(first["id"]))
        second_before = dict(self.api.store.get(second["id"]))
        third_before = dict(self.api.store.get(third["id"]))

        res = self._retry(second["id"])
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertIs(body["ok"], True)
        self.assertEqual(body["retry_of"], second["id"])
        retried = body["job"]
        self.assertNotEqual(retried["id"], second["id"])
        self.assertEqual(retried["retry_of"], second["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(retried["model_id"], "studio-hub::qwen-image")
        self.assertEqual(retried["priority"], "background")

        # The new row carries the same request, params, routing, group, priority and requester.
        fresh_row = self.api.store.get(retried["id"])
        self.assertIsNotNone(fresh_row)
        self.assertEqual(fresh_row["modality"], "image")
        self.assertEqual(fresh_row["model_id"], "qwen-image")
        self.assertEqual(fresh_row["provider_id"], "studio-hub-studio-1200")
        self.assertEqual(fresh_row["job_group"], "ep-04/scene-2")
        self.assertEqual(fresh_row["priority"], "background")
        self.assertEqual(fresh_row["caller"], "story-studio")
        self.assertEqual(fresh_row["previous_job_id"], second["id"])
        self.assertIn("shot 2", fresh_row["params"])

        # Drive the retried job to completion and verify its siblings and the old row never moved.
        self.assertTrue(self.api.store.mark_running(fresh_row["id"]))
        self.api.store.succeed(fresh_row["id"], result={"payload": {"ok": True}}, media_type="image/png")
        self.assertEqual(self.api.store.get(fresh_row["id"])["state"], "succeeded")
        self.assertEqual(dict(self.api.store.get(first["id"])), first_before)
        self.assertEqual(dict(self.api.store.get(second["id"])), second_before)
        self.assertEqual(dict(self.api.store.get(third["id"])), third_before)

        # The admin Jobs list publishes `retry_of` on the new row and null on the original rows.
        listed = self.client.get("/admin/api/jobs?limit=20", headers=self.headers).json()["jobs"]
        by_id = {row["id"]: row for row in listed}
        self.assertEqual(by_id[fresh_row["id"]]["retry_of"], second["id"])
        self.assertIsNone(by_id[first["id"]]["retry_of"])
        self.assertIsNone(by_id[second["id"]]["retry_of"])
        self.assertIsNone(by_id[third["id"]]["retry_of"])

    def test_non_terminal_and_held_jobs_are_refused_with_a_sentence_saying_why(self) -> None:
        queued = self._submit_one(f"{self.id()}-queued")
        res_queued = self._retry(queued["id"])
        self.assertEqual(res_queued.status_code, 409)
        self.assertEqual(res_queued.json()["code"], "JOB_NOT_TERMINAL")
        self.assertEqual(
            res_queued.json()["detail"],
            "That job is still queued, so there is no finished request to repeat. "
            "Wait for it to settle, or cancel it first.",
        )

        running = self._submit_one(f"{self.id()}-running")
        self.assertTrue(self.api.store.mark_running(running["id"]))
        res_running = self._retry(running["id"])
        self.assertEqual(res_running.status_code, 409)
        self.assertEqual(res_running.json()["code"], "JOB_NOT_TERMINAL")
        self.assertEqual(
            res_running.json()["detail"],
            "That job is still running, so there is no finished request to repeat. "
            "Wait for it to settle, or cancel it first.",
        )

        held = self._submit_one(f"{self.id()}-held")
        self.assertTrue(self.api.store.mark_running(held["id"]))
        self.api.store.fail(
            held["id"],
            code="UNKNOWN_OUTCOME",
            detail="poll lost contact",
            recovery_state="unknown_outcome",
            recovery_detail="reconcile or discard",
        )
        res_held = self._retry(held["id"])
        self.assertEqual(res_held.status_code, 409)
        self.assertEqual(res_held.json()["code"], "JOB_STILL_HELD")
        self.assertIn("Needs recovery", res_held.json()["detail"])

        missing = self._retry("no-such-job-id")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "JOB_NOT_FOUND")

    def test_the_old_row_stays_terminal_and_is_never_reopened_or_mutated(self) -> None:
        failed = self._submit_one(f"{self.id()}-failed", guest_id="guest-7")
        self.assertTrue(self.api.store.mark_running(failed["id"]))
        self.api.store.fail(failed["id"], code="STUCK_ON_NODE", detail="poll ceiling exceeded")
        snapshot = dict(self.api.store.get(failed["id"]))
        stats_before = self.api.store.stats()
        guest_before = self.api.store.requested_by_counts().get("guest-7", {}).get("jobs", 0)

        res = self._retry(failed["id"])
        self.assertEqual(res.status_code, 200, res.text)
        fresh_id = res.json()["job"]["id"]

        # The failed row did not change a single column.
        self.assertEqual(dict(self.api.store.get(failed["id"])), snapshot)
        self.assertEqual(self.api.store.get(failed["id"])["state"], "failed")

        # The retry is a separate job in the books — but on the operator's account, not the
        # guest's: re-running somebody else's job must not grow their usage.
        stats_after = self.api.store.stats()
        self.assertEqual(
            stats_after["by_modality"].get("image", 0),
            stats_before["by_modality"].get("image", 0) + 1,
        )
        fresh = self.api.store.get(fresh_id)
        self.assertEqual(
            self.api.store.requested_by_counts().get("guest-7", {}).get("jobs", 0), guest_before,
        )
        self.assertEqual(json.loads(fresh["requested_by"])["role"], "owner")
        self.assertEqual(fresh["caller"], "story-studio", "the original caller label rides along")
        self.assertIsNone(fresh["previous_cancel_json"])


if __name__ == "__main__":
    unittest.main()
