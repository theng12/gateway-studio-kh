#!/usr/bin/env python3
"""Every automatic stop, and every orphan Stop a person pressed, must be visible afterwards.

**Why (0.2.212).** An automatic stop nobody can see afterwards is worse than none: a batch stops
on a Mac nobody is looking at, and no one can tell whether the right copy was stopped, why the
Gateway thought it was safe, or what the Hub said it did. The stops were already written to
`startup_audit.jsonl`; what was missing was a read the Fleet page could draw, and the manual
orphan Stop — the one path that wrote nothing at all.

This suite pins, in the order it matters:

1. **Each stop kind writes a row** — `superseded_stop` (a copy this Gateway replaced, both worker
   paths), `auto_stop` (an orphan the owner's switch covered), and `manual` (the Stop the owner
   pressed) — each with its site, Hub batch, Gateway job, the guard's reason, the switch that did
   it, and the Hub's own answer.
2. **The read lists them newest first**, bounded by days and rows, and resolves the site's name.
3. **An unreadable log is `available: false` with `stops: null`**, never an empty list; a log with
   nothing in range is an honest empty list.
4. **Guests never see it**: the route is behind `require_admin`, which refuses a guest session.

Mocked Hub transport and a fake adapter only: no live Hub, no generation, no installed app.

Run with::

    .venv/bin/python -m unittest tests.test_auto_stops
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

# Before the first `gateway` import: the store and the data dir must never be the owner's live ones.
_SANDBOX = sandbox_env("gateway-auto-stops-")

from gateway import admin, admin_session, orphans, startup_control, worker  # noqa: E402


def _routes(routes):
    """Every route, including a mounted router's own table (FastAPI 0.141 keeps it in one wrapper)."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _routes(route.original_router.routes)
        else:
            yield route


NOW = time.time()
HUB_DEFINITION = types.SimpleNamespace(id="studio-hub-L1", kind="studio_hub",
                                       display_name="Blue Room", base_url="")
HUB_ANSWER = {"ok": True, "status": 200, "route": "/api/hub/jobs/b-old",
              "answer": {"ok": True, "queued_cancelled": 0, "running_signalled": 1,
                         "running_pending": 0}}


class AuditFile(unittest.TestCase):
    """Point the one audit writer at a file this test owns."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="auto-stops-", dir=Path(_SANDBOX.name)))
        self.path = self.root / "startup_audit.jsonl"
        patched = patch.object(startup_control, "audit_path", lambda: self.path)
        patched.start()
        self.addCleanup(patched.stop)

    def test_deadline_stop_is_visible_with_its_requester(self) -> None:
        startup_control.audit("deadline_stop", controller="studio-hub-L1", batch="batch-a",
                              requested_by="gateway", switch="deadline", result=HUB_ANSWER)
        row = startup_control.recent_stops()["stops"][0]
        self.assertEqual(row["batch"], "batch-a")
        self.assertEqual(row["requested_by"], "gateway")
        self.assertEqual(row["hub"]["running_signalled"], 1)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class FakeStore:
    """Just the three calls `worker._stop_losing_copy` makes."""

    def __init__(self, job: dict) -> None:
        self.job = job
        self.recorded: list[tuple[str, dict]] = []
        self.resolved: list[tuple[str, str, str]] = []

    def get(self, job_id: str) -> dict | None:
        return dict(self.job) if job_id == self.job.get("id") else None

    def jobs_for_upstream_batches(self, batch_ids: list[str]) -> list[dict]:
        return [dict(self.job)]

    def record_superseded_batch(self, job_id: str, entry: dict) -> None:
        self.recorded.append((job_id, entry))

    def resolve_superseded_batch(self, job_id: str, batch_id: str, *, status: str,
                                 detail: str | None = None) -> None:
        self.resolved.append((job_id, batch_id, status))


def settled_job(root: Path, *, batch: str = "b-loser") -> dict:
    """A succeeded job whose one artifact is really on disk — the guard's strongest case."""
    kept = root / "kept.png"
    kept.write_bytes(b"png")
    return {
        "id": "job-1", "state": "succeeded", "upstream_job_id": batch,
        "provider_id": "studio-hub-L1", "modality": "image", "model_id": "qwen-image",
        "artifacts_json": json.dumps([{"path": str(kept), "media_type": "image/png"}]),
        "asset_path": str(kept), "updated_at": NOW,
    }


class EveryStopKindIsVisible(AuditFile):
    def test_a_copy_this_gateway_replaced_is_listed(self) -> None:
        store = FakeStore(settled_job(self.root))
        # An actual stop sets its own switch on explicitly: both switches start off (0.2.213).
        with patch.object(orphans, "settings",
                          return_value={"auto_stop": False, "superseded_stop": True}), \
                patch.object(worker.registry, "definition", return_value=HUB_DEFINITION), \
                patch.object(worker, "_cancel_hub_batch", return_value=HUB_ANSWER) as cancel:
            worker._stop_losing_copy(store, "job-1", "b-winner", {"stopped": 0})
        self.assertEqual(cancel.call_count, 1)
        listed = startup_control.recent_stops(now=NOW + 1)
        self.assertTrue(listed["available"])
        self.assertEqual(len(listed["stops"]), 1, listed)
        row = listed["stops"][0]
        self.assertEqual(row["switch"], "superseded_stop")
        self.assertEqual(row["batch"], "b-loser")
        self.assertEqual(row["job"], "job-1")
        self.assertEqual(row["site"], "studio-hub-L1")
        self.assertIn("Gateway job succeeded", row["reason"])
        self.assertEqual(row["hub"], {"queued_cancelled": 0, "running_signalled": 1,
                                      "running_pending": 0})
        self.assertIs(row["ok"], True)

    def test_an_orphan_the_auto_stop_covered_is_listed(self) -> None:
        kept = settled_job(self.root, batch="b-old")
        row = {"id": "L1", "label": "Blue Room", "reachable": True, "jobs_read": True,
               "running_jobs": [{"id": "b-old", "label": "gateway-kh", "modality": "image",
                                 "started_at": NOW - 60}]}
        with patch.object(orphans, "settings",
                          return_value={"auto_stop": True, "superseded_stop": True}), \
                patch.object(orphans, "_cancel", return_value=HUB_ANSWER):
            stopped = orphans.maybe_auto_stop(
                {"locations": [row]}, store=FakeStore(kept), now=NOW)
        self.assertEqual([entry["batch_id"] for entry in stopped], ["b-old"])
        listed = startup_control.recent_stops(now=NOW + 1)
        self.assertEqual([item["switch"] for item in listed["stops"]], ["auto_stop"])
        item = listed["stops"][0]
        self.assertEqual((item["site"], item["site_label"], item["batch"], item["job"]),
                         ("L1", "L1", "b-old", "job-1"))
        self.assertEqual(item["hub"]["running_signalled"], 1)

    def test_a_stop_the_owner_pressed_is_listed_and_marked_manual(self) -> None:
        job = settled_job(self.root, batch="b-old")
        adapter = types.SimpleNamespace(cancel=lambda request: HUB_ANSWER)
        definition = types.SimpleNamespace(id="studio-hub-L1", display_name="Blue Room")
        location = {"id": "L1", "label": "Blue Room", "url": "http://10.0.0.2:47873",
                    "provider_id": "studio-hub-L1", "reachable": True, "jobs_read": True,
                    "running_jobs": [{"id": "b-old", "label": "gateway-kh", "modality": "image",
                                      "started_at": NOW - 60}]}
        with patch.object(admin.fleet, "snapshot", return_value={"locations": [location]}), \
                patch.object(admin, "store", FakeStore(job)), \
                patch.object(admin.registry, "definition", return_value=definition), \
                patch.object(admin.registry.credentials, "resolved", return_value={"hub_token": "t"}), \
                patch.object(admin, "adapter_for", return_value=adapter):
            answer = admin.fleet_cancel_job("L1", "b-old",
                                            {"modality": "image", "orphan_stop": True})
        self.assertEqual(answer["result"], HUB_ANSWER)

        listed = startup_control.recent_stops(now=NOW + 1)
        self.assertEqual(len(listed["stops"]), 1, listed)
        item = listed["stops"][0]
        self.assertEqual(item["switch"], "manual")
        self.assertEqual(item["requested_by"], "admin")
        self.assertEqual((item["batch"], item["job"]), ("b-old", "job-1"))
        self.assertIn("Gateway job succeeded", item["reason"])
        self.assertEqual(item["hub"]["running_signalled"], 1)

    def test_an_ordinary_cancel_is_not_a_stop(self) -> None:
        # The Jobs tab's own Stop keeps its previous meaning; it is not an orphan Stop and does not
        # pretend to be one in the automatic-stop list.
        adapter = types.SimpleNamespace(cancel=lambda request: HUB_ANSWER)
        definition = types.SimpleNamespace(id="studio-hub-L1", display_name="Blue Room")
        location = {"id": "L1", "label": "Blue Room", "url": "http://10.0.0.2:47873",
                    "provider_id": "studio-hub-L1"}
        with patch.object(admin.fleet, "snapshot", return_value={"locations": [location]}), \
                patch.object(admin.registry, "definition", return_value=definition), \
                patch.object(admin.registry.credentials, "resolved", return_value={"hub_token": "t"}), \
                patch.object(admin, "adapter_for", return_value=adapter):
            admin.fleet_cancel_job("L1", "b-old", {"modality": "image"})
        self.assertEqual(startup_control.recent_stops(now=NOW + 1)["stops"], [])
        self.assertEqual(self.rows(), [])


class TheReadIsBounded(AuditFile):
    def write(self, rows: list[dict]) -> None:
        self.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def stop(self, at: float, batch: str, *, switch: str = "auto_stop") -> dict:
        return {"at": at, "event": "orphan_stop", "controller": "L1", "batch": batch,
                "job": "job-1", "modality": "image", "reason": "kept", "switch": switch,
                "result": HUB_ANSWER}

    def test_newest_first_and_bounded_by_days_and_rows(self) -> None:
        self.write([self.stop(NOW - 9 * 86400, "too-old"),
                    self.stop(NOW - 300, "newer"),
                    self.stop(NOW - 60, "newest"),
                    {"at": NOW, "event": "requested", "controller": "L1"}])
        listed = startup_control.recent_stops(days=7, limit=10, now=NOW)
        self.assertEqual([item["batch"] for item in listed["stops"]], ["newest", "newer"])
        self.assertEqual(len(startup_control.recent_stops(days=7, limit=1, now=NOW)["stops"]), 1)
        # A row that is not a stop is never listed, however recent.
        self.assertNotIn("requested", [item["switch"] for item in listed["stops"]])

    def test_an_unreadable_log_is_not_available_never_empty(self) -> None:
        self.path.mkdir(parents=True)  # a directory where the log should be: read_text fails
        listed = startup_control.recent_stops(now=NOW)
        self.assertIs(listed["available"], False)
        self.assertIsNone(listed["stops"])
        self.assertIsNone(listed["count"])
        self.assertIn("not available", listed["note"])

    def test_a_log_with_nothing_in_range_is_an_honest_empty_list(self) -> None:
        self.write([self.stop(NOW - 30 * 86400, "ancient")])
        listed = startup_control.recent_stops(days=7, now=NOW)
        self.assertTrue(listed["available"])
        self.assertEqual((listed["stops"], listed["count"]), ([], 0))

    def test_a_row_written_before_the_switch_field_still_names_its_switch(self) -> None:
        # 0.2.211 rows carry no `switch`, and each event has always had exactly one writer.
        old = self.stop(NOW - 60, "b-old", switch="superseded_stop")
        old.pop("switch")
        old["event"] = "superseded_stop"
        self.write([old])
        self.assertEqual(startup_control.recent_stops(now=NOW)["stops"][0]["switch"],
                         "superseded_stop")


class OwnerOnly(unittest.TestCase):
    def request(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(cookies={admin_session.COOKIE: "token"}, method="GET",
                                    headers={}, client=None)

    def test_a_guest_is_refused_the_stops_read(self) -> None:
        with patch.object(admin_session, "session", return_value={
                "role": "guest", "guest_id": "g1", "name": "Friend"}), \
                patch.object(admin, "is_loopback", return_value=False), \
                patch.object(admin, "request_is_loopback", return_value=False):
            with self.assertRaises(admin.AdminError) as caught:
                admin.require_admin(self.request(), authorization=None)
        self.assertEqual(caught.exception.status, 403)

    def test_the_route_declares_require_admin(self) -> None:
        path = "/admin/api/fleet/startup/auto-stops"
        served = {route.path: route for route in _routes(admin.router.routes)}
        self.assertIn(path, served, "the Fleet page's stops read is not a route this Gateway serves")
        wired = any(getattr(dep, "dependency", None) is admin.require_admin
                    for dep in (served[path].dependencies or []))
        self.assertTrue(wired, f"{path} does not declare require_admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
