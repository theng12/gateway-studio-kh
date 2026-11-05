"""H9 — the console's queue view: what is waiting, where it stands, what bounds it.

Audit H9, in one sentence: *"The console has no queue view at all. Four views exist (models,
providers, create, packs); 'queue' appears twice in 104 KB (a per-row badge). `worker.budgets` —
written 'so the console can say what is bounding a lane' — has no reader outside tests."*

The route this file holds to its contract is `GET /admin/api/queue`, and the two halves of it are
deliberately in one answer. A position with no ceiling beside it is a number without a reason:
"position 3" cannot be told from "position 3 and this lane may run one at a time", which is the
difference between a queue doing its job and a fleet that is simply not being used.

It is also where the owner's decision E is stated **while the work waits** — *"the same policy
stated on the Gateway's Jobs view so an operator can see attempt n of 3"* — from the same function
the job envelope uses, so the console and Story Studio cannot read two different policies.

Every test runs against a temp profile (`GATEWAY_*` pointed at a TemporaryDirectory before the
gateway package is imported), so nothing here touches the owner's job database, roster or settings.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-admin-queue-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "queue-fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, concurrency  # noqa: E402
from gateway import api  # noqa: E402
from gateway.runtime import store, worker  # noqa: E402


def _routes(routes):
    """Every route, including a mounted router's own table (FastAPI 0.141 keeps it in one wrapper)."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _routes(route.original_router.routes)
        else:
            yield route


class QueueView(unittest.TestCase):
    def setUp(self) -> None:
        # The worker is never started here, so a submitted job stays `accepted` and
        # the queue under test is the real one rather than a fixture.
        self.assertTrue(
            worker._thread is None,
            "this suite asserts on waiting jobs; a running dispatcher would consume them",
        )
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.headers = {"Authorization": "Bearer queue-fixture-key"}
        self.jobs: list[dict] = []

    def tearDown(self) -> None:
        self.client.close()
        with store._lock:
            store._conn.execute("DELETE FROM jobs")
            store._conn.commit()
        worker._in_flight.clear()
        worker._lane_in_flight.clear()
        worker._per_lane.clear()
        worker.budgets.clear()

    def submit(self, *, modality: str = "image", model: str = "m", key: str | None = None,
               **extra) -> dict:
        job, _ = store.submit(
            modality=modality, model_id=model, params={},
            idempotency_key=key or f"k{len(self.jobs)}",
            provider_id="studio-hub-local", **extra,
        )
        self.jobs.append(job)
        return job

    def queue(self, **params) -> dict:
        response = self.client.get("/admin/api/queue", headers=self.headers, params=params)
        self.assertEqual(response.status_code, 200, response.text[:400])
        return response.json()

    def test_a_waiting_job_is_listed_with_its_place_and_its_lane(self) -> None:
        first = self.submit(modality="image", caller="story-studio")
        second = self.submit(modality="image")
        chat = self.submit(modality="chat")
        body = self.queue()
        self.assertEqual(body["waiting"], 3)
        self.assertFalse(body["truncated"])
        self.assertEqual(
            [row["id"] for row in body["jobs"]], [first["id"], second["id"], chat["id"]],
            "the listing is the queue: oldest first",
        )
        by_id = {row["id"]: row for row in body["jobs"]}
        self.assertEqual(by_id[first["id"]]["queue_position"], 1)
        self.assertEqual(by_id[second["id"]]["queue_position"], 2)
        self.assertEqual(by_id[first["id"]]["queue_size"], 2)
        self.assertEqual(by_id[chat["id"]]["queue_position"], 1,
                         "a chat job is not behind image work: the queue is per modality")
        row = by_id[first["id"]]
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["modality"], "image")
        self.assertEqual(row["provider_id"], "studio-hub-local")
        self.assertTrue(row["provider_name"], row)
        self.assertEqual(row["model_id"], "m")
        self.assertTrue(row["model_name"], row)
        self.assertEqual(row["caller"], "story-studio")
        self.assertLess(row["age_seconds"], 60)
        self.assertIsNone(row["defer_reason"])
        self.assertEqual(row["deferrals"], 0)

    def test_a_running_job_is_not_in_the_queue_at_all(self) -> None:
        first = self.submit()
        second = self.submit()
        self.assertTrue(store.mark_running(first["id"]))
        body = self.queue()
        self.assertEqual([row["id"] for row in body["jobs"]], [second["id"]])
        self.assertEqual(body["jobs"][0]["queue_position"], 1)
        self.assertEqual(body["jobs"][0]["queue_size"], 1,
                         "the dispatched job is not counted as waiting")

    def test_a_deferred_job_keeps_its_place_and_names_its_reason(self) -> None:
        first = self.submit()
        second = self.submit()
        self.assertTrue(store.defer(first["id"], delay_s=30,
                                    reason="LOCATION_NOT_READY: MACHINE_BUSY"))
        body = self.queue()
        by_id = {row["id"]: row for row in body["jobs"]}
        self.assertEqual(by_id[first["id"]]["defer_reason"],
                         "LOCATION_NOT_READY: MACHINE_BUSY")
        self.assertEqual(by_id[first["id"]]["queue_position"], 1,
                         "a deferral does not push the job behind newer work")
        self.assertEqual(by_id[second["id"]]["queue_position"], 2)
        self.assertEqual(by_id[first["id"]]["deferrals"], 0)
        self.assertGreaterEqual(by_id[first["id"]]["retry_in_s"], 0)
        self.assertLessEqual(by_id[first["id"]]["retry_in_s"], 30)
        self.assertIsNone(by_id[second["id"]]["retry_in_s"],
                          "a fresh job is not retrying; it is being dispatched for the first time")

    def test_the_retry_policy_is_stated_before_the_first_failure(self) -> None:
        """The owner's decision E, read where the job waits rather than after it dies."""
        job = self.submit()
        row = self.queue()["jobs"][0]
        self.assertEqual(row["attempts_used"], 0)
        self.assertEqual(row["attempts_allowed"], int(concurrency.retry_policy()["max_attempts"]))
        self.assertEqual(row["attempts"], [])
        self.assertIn("different machine", row["attempt_policy"])

    def test_a_job_that_has_been_tried_names_the_machine_and_the_reason(self) -> None:
        job = self.submit()
        store.record_attempt(job["id"], location_id="studio-hub-blue-room",
                             code="MACHINE_BUSY", detail="no free machine for image")
        row = self.queue()["jobs"][0]
        self.assertEqual(row["attempts_used"], 1)
        self.assertEqual(row["attempts_allowed"], 3)
        self.assertEqual(row["attempts"][0]["number"], 1)
        self.assertEqual(row["attempts"][0]["machine"], "studio-hub-blue-room")
        self.assertEqual(row["attempts"][0]["failure_code"], "MACHINE_BUSY")
        self.assertEqual(row["attempts"][0]["failure_reason"], "no free machine for image")

    def test_what_is_bounding_each_lane_is_published_live(self) -> None:
        """`worker.budgets` was written for this view and had no reader."""
        self.submit(modality="image")
        worker._in_flight["studio-hub-local"] = 1
        worker._lane_in_flight["image"] = 1
        worker._per_lane[("studio-hub-local", "image")] = 1
        # `active()` reads the total `_book`/`_release` publish beside the books, so booking the
        # books by hand means publishing that too.
        worker._active_total = 1
        worker.budgets["studio-hub-local"] = {
            "limit": 1, "source": "hub",
            "detail": "the Hub reports 1 free Mac for this operation",
        }
        body = self.queue()
        self.assertEqual(body["in_flight"], {"studio-hub-local": 1})
        self.assertEqual(body["budgets"]["studio-hub-local"]["limit"], 1)
        self.assertEqual(body["budgets"]["studio-hub-local"]["source"], "hub")
        site = next(row for row in body["sites"] if row["provider_id"] == "studio-hub-local")
        self.assertEqual(site["in_flight"], 1)
        self.assertEqual(site["budget"]["limit"], 1)
        self.assertEqual(body["pool"]["in_flight"], 1)
        lane = next(row for row in body["lanes"] if row["modality"] == "image")
        self.assertEqual(lane["waiting"], 1)
        self.assertEqual(lane["in_flight"], 1)
        self.assertIn("limit", lane)
        self.assertIn("source", lane)
        self.assertIn("detail", lane)

    def test_an_empty_lane_with_no_cap_is_not_a_row(self) -> None:
        self.submit(modality="chat")
        lanes = self.queue()["lanes"]
        self.assertEqual([row["modality"] for row in lanes], ["chat"])

    def test_the_listing_is_bounded_and_says_so(self) -> None:
        for _ in range(3):
            self.submit()
        body = self.queue(limit=2)
        self.assertEqual(len(body["jobs"]), 2)
        self.assertTrue(body["truncated"],
                        "a truncated queue must say so rather than looking complete")
        self.assertEqual(body["jobs"][0]["queue_size"], 3,
                         "the bounding number is the whole lane, not the page")

    def test_a_nonsense_limit_is_refused_rather_than_reaching_sql(self) -> None:
        response = self.client.get("/admin/api/queue", headers=self.headers,
                                   params={"limit": 0})
        self.assertEqual(response.status_code, 422)
        self.assertGreater(len(self.queue(limit=1000)["jobs"]), -1)


class TheRouteIsRegistered(unittest.TestCase):
    def test_the_admin_router_serves_the_queue(self) -> None:
        paths = {route.path for route in _routes(admin.router.routes)}
        self.assertIn("/admin/api/queue", paths)


if __name__ == "__main__":
    unittest.main()
