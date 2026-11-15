#!/usr/bin/env python3
"""Terminal discard for a job that is not a recovery hold.

Story's escape for a stuck row: `store.cancel` only *requests* for a running
job, and `_discard_recovery_hold` refused anything without `recovery_state`, so
a `running` row whose upstream was gone could never end (measured 2026-11-15:
seven Story jobs at top-level `running` with terminal linked Gateway jobs).
`POST /admin/api/jobs/{id}/discard` ends any live row terminally, delegates a
hold to the existing Hub-discard path, and changes nothing twice.

Everything here runs against the app's own store with a patched lane
definition: no live Hub, no real generation, no credential, no installed app.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-job-discard-"))
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
        "GATEWAY_API_KEY": "fixture-key",
        "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
        "GATEWAY_VOICES": str(_TEMP_ROOT / "voices.json"),
        "GATEWAY_VOICES_OUT": str(_TEMP_ROOT / "voices.json"),
    }
)

#: A cloud lane: no discard route at all, which is the common stuck-row case.
CLOUD = SimpleNamespace(
    id="openrouter-test",
    kind="openai_compatible",
    display_name="OpenRouter",
    base_url="https://openrouter.example/api/v1",
)


class JobDiscardTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        self.api = api
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))

    def tearDown(self) -> None:
        self.client.close()

    def _submit(self) -> dict:
        row, created = self.api.store.submit(
            modality="music",
            model_id="model/test",
            params={"prompt": "x"},
            idempotency_key=f"discard-{self.id()}",
            provider_id="openrouter-test",
        )
        self.assertTrue(created)
        return row

    def _discard(self, job_id: str, body: dict | None = None):
        with patch.object(self.api.registry, "definition", return_value=CLOUD):
            return self.client.post(
                f"/admin/api/jobs/{job_id}/discard", json=body if body is not None else {}
            )

    def _hold(self) -> str:
        """A job settled as an uncertain outcome, the way the worker leaves a hold."""
        row = self._submit()
        self.assertTrue(self.api.store.mark_running(row["id"]))
        self.api.store.fail(
            row["id"],
            code="UNKNOWN_OUTCOME",
            detail="the Hub settled the item without knowing the worker's outcome",
            recovery_state="unknown_outcome",
            recovery_detail="batch hub-batch-1 item 0",
            recovery_context=None,
        )
        return row["id"]

    def test_a_running_row_with_no_route_ends_cancelled_and_keeps_a_late_result(self) -> None:
        """The defect itself: a stuck `running` row ends here, terminal and honest."""
        row = self._submit()
        self.assertTrue(self.api.store.mark_running(row["id"]))
        response = self._discard(
            row["id"], {"by": "Story Studio", "reason": "upstream is gone"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["discarded"])
        self.assertFalse(body["already_terminal"])
        self.assertEqual(body["upstream"], "none")
        # The same honest words the hold path uses: the provider was not told,
        # and a late answer is still this job's to keep.
        self.assertIn("was not told", body["detail"])
        self.assertIn("adopted if it arrives", body["detail"])
        job = self.api.store.get(row["id"])
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["cancel_state"], "confirmed")
        self.assertEqual(job["cancelled_by"], "Story Studio")
        self.assertEqual(job["cancel_reason"], "upstream is gone")
        self.assertEqual(job["cancel_origin"], "discard")
        # Terminal wins: a settlement that arrives late is adopted as evidence
        # beside the verdict, never a flip back to running or succeeded.
        self.api.store.succeed(row["id"], result={"payload": {"text": "late"}})
        late = self.api.store.get(row["id"])
        self.assertEqual(late["state"], "cancelled")
        self.assertEqual(late["late_result_state"], "succeeded")

    def test_an_accepted_row_ends_terminal(self) -> None:
        row = self._submit()
        response = self._discard(
            row["id"], {"by": "Story Studio", "reason": "changed my mind"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["discarded"])
        self.assertFalse(body["already_terminal"])
        self.assertEqual(body["upstream"], "none")
        self.assertIn("Nothing was ever dispatched", body["detail"])
        job = self.api.store.get(row["id"])
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["cancel_state"], "confirmed")
        self.assertEqual(job["cancelled_by"], "Story Studio")
        self.assertEqual(job["cancel_reason"], "changed my mind")
        self.assertEqual(job["cancel_origin"], "discard")

    def test_a_hold_is_delegated_to_the_hold_path(self) -> None:
        """A hold keeps today's Hub-discard semantics whole — one implementation."""
        job_id = self._hold()
        with patch.object(
            self.api.registry, "definition", return_value=CLOUD
        ), patch.object(
            self.api, "_discard_recovery_hold", wraps=self.api._discard_recovery_hold
        ) as spy:
            response = self.client.post(
                f"/admin/api/jobs/{job_id}/discard", json={"by": "Story Studio"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        spy.assert_called_once_with(job_id)
        body = response.json()
        self.assertTrue(body["discarded"])
        self.assertFalse(body["already_terminal"])
        self.assertEqual(body["upstream"], "none")
        self.assertIn("not told", body["detail"])
        job = self.api.store.get(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "HOLD_CLOSED")
        self.assertFalse(job["recovery_required"])

    def test_an_already_terminal_row_is_untouched_and_idempotent(self) -> None:
        row = self._submit()
        self.api.store.cancel(row["id"])
        before = self.api.store.get(row["id"])
        first = self._discard(row["id"], {"by": "Story Studio", "reason": "again"})
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body["already_terminal"])
        self.assertFalse(body["discarded"])
        self.assertEqual(body["upstream"], "none")
        self.assertEqual(self.api.store.get(row["id"]), before, "a terminal row changes nothing")
        second = self._discard(row["id"], {"by": "Someone Else", "reason": "changed"})
        self.assertTrue(second.json()["already_terminal"])
        self.assertFalse(second.json()["discarded"])
        self.assertEqual(self.api.store.get(row["id"]), before, "a second call changes nothing")

    def test_a_second_call_after_a_discard_changes_nothing(self) -> None:
        row = self._submit()
        self.assertTrue(self.api.store.mark_running(row["id"]))
        first = self._discard(row["id"], {"by": "Story Studio", "reason": "stuck"})
        self.assertTrue(first.json()["discarded"])
        snapshot = self.api.store.get(row["id"])
        second = self._discard(row["id"], {"by": "Other", "reason": "again"})
        body = second.json()
        self.assertTrue(body["already_terminal"])
        self.assertFalse(body["discarded"])
        self.assertEqual(self.api.store.get(row["id"]), snapshot)


if __name__ == "__main__":
    unittest.main()
