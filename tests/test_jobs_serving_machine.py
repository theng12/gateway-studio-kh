#!/usr/bin/env python3
"""The Jobs list names the serving Mac only when the Hub reported it (owner, 2026-11-15).

Owner, looking at Jobs: *"we don't get to see which actual machine is serving request?"* The
list already carried `executed_by` (the Hub's `machine` for the item, stored on the result or
failure evidence). These pin the honest semantics behind the new Serving machine column:

  * an accepted job has no serving Mac — `executed_by` stays `None` (never the target location);
  * a settled fleet job reports the Hub's machine, and the row says it is a fleet job;
  * a cloud job says it is not a fleet job, so an empty serving machine reads "no Mac".
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-jobs-serving-")

from starlette.requests import Request  # noqa: E402

from gateway import admin  # noqa: E402
from gateway.store import JobStore  # noqa: E402


def _request() -> Request:
    request = MagicMock(spec=Request)
    request.headers = {}
    request.cookies = {}
    request.client = SimpleNamespace(host="127.0.0.1")
    return request


class ServingMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._patch = unittest.mock.patch.object(admin, "store", self.store)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _job(self, provider: str, key: str) -> dict:
        row, _ = self.store.submit(modality="image", model_id="qwen-image",
                                   params={}, idempotency_key=key, provider_id=provider)
        return row

    def test_an_accepted_job_has_no_serving_machine(self) -> None:
        job = self._job("studio-hub-abc", "k1")
        rows = admin.list_jobs(_request(), limit=10, since=None, before=None)["jobs"]
        row = next(r for r in rows if r["id"] == job["id"])
        self.assertIsNone(row["executed_by"], "the target location is not the serving Mac")
        self.assertTrue(row["fleet"])
        self.assertEqual(row["state"], "accepted")

    def test_a_settled_fleet_job_reports_the_hubs_machine(self) -> None:
        job = self._job("studio-hub-abc", "k2")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET state='succeeded', result_json=? WHERE id=?",
                (json.dumps({"payload": {"hub_machine": "mac-7"}}), job["id"]))
            self.store._conn.commit()
        rows = admin.list_jobs(_request(), limit=10, since=None, before=None)["jobs"]
        row = next(r for r in rows if r["id"] == job["id"])
        self.assertEqual(row["executed_by"], "mac-7")
        self.assertEqual(row["execution"]["machine_id"], "mac-7")
        self.assertTrue(row["fleet"])

    def test_a_cloud_job_says_it_has_no_mac(self) -> None:
        job = self._job("openai-direct", "k3")
        rows = admin.list_jobs(_request(), limit=10, since=None, before=None)["jobs"]
        row = next(r for r in rows if r["id"] == job["id"])
        self.assertIsNone(row["executed_by"])
        self.assertFalse(row["fleet"])


if __name__ == "__main__":
    unittest.main()
