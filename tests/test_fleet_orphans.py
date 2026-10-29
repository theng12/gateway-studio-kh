#!/usr/bin/env python3
"""Hub batches still running for work that already finished — the orphan check.

The incident, 2026-10-29: a restart on site 1200 made the Gateway lose contact with eight Hub
batches, re-dispatch every one under a **new** upstream id, and all eight Gateway jobs succeeded.
Three of the old batches kept running on 1200 doing duplicate work, and only a person noticed.

This suite pins the whole answer, in the order it matters:

1. **Three classes, never two.** `live` is not listed; `orphan_superseded` has a Gateway job that
   moved on, finished, failed or was cancelled; `orphan_unknown` has no Gateway job at all.
2. **An old id survives only in the attempt log**, and a longer id that merely contains it does not
   claim it.
3. **Only this Gateway's batches.** Anything labelled otherwise is invisible and never stopped.
4. **The Stop guard is the job's own evidence**: succeeded with its artifacts on disk and non-empty,
   or failed/cancelled with nothing left to collect. Nothing else.
5. **Auto-stop is off by default**, applies the same guard, logs every stop, and **never touches
   `orphan_unknown`**.
6. **A failed read is "not checked", never "none"** — a timed-out site and a failed jobs read.
7. **Owner-only**: the Fleet read and the settings door are behind `require_admin`, which refuses a
   guest session.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_orphans
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

# Before the first `gateway` import: the store must never be the owner's live database.
_SANDBOX = sandbox_env("gateway-fleet-orphans-")

from gateway import admin, admin_session, orphans  # noqa: E402
from gateway.runtime import store as runtime_store  # noqa: E402

NOW = 1_760_000_000.0


def batch(batch_id: str = "b-old", label: str = "gateway-kh", **extra) -> dict:
    row = {
        "id": batch_id, "label": label, "modality": "image", "model": "qwen-image",
        "model_id": "qwen-image", "total": 4, "done": 1, "queued": 2, "running": 1,
        "retrying": 0, "started_at": NOW - 600, "artifact_count": None, "output_path": None,
    }
    row.update(extra)
    return row


def location(batches: list[dict] | None = None, **extra) -> dict:
    row = {"id": "L1", "label": "Blue Room", "reachable": True, "jobs_read": True,
           "error": None, "running_jobs": list(batches or [])}
    row.update(extra)
    return row


def job(job_id: str = "job-1", *, state: str = "running", upstream: str | None = None,
        attempts: list[dict] | None = None, artifacts: list[dict] | None = None,
        updated_at: float = NOW, **extra) -> dict:
    row = {
        "id": job_id, "state": state, "provider_id": "studio-hub-L1", "modality": "image",
        "model_id": "qwen-image", "upstream_job_id": upstream, "updated_at": updated_at,
        "attempt_log": json.dumps(attempts or []),
        "artifacts_json": json.dumps(artifacts) if artifacts is not None else None,
        "asset_path": (artifacts or [{}])[0].get("path") if artifacts else None,
        "error_code": None, "error_detail": None,
    }
    row.update(extra)
    return row


def attempt(detail: str) -> dict:
    return {"location": "studio-hub-L1", "code": "PROVIDER_SUBMISSION_UNCERTAIN",
            "detail": detail, "at": NOW - 300}


class FakeStore:
    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs

    def jobs_for_upstream_batches(self, batch_ids: list[str]) -> list[dict]:
        return list(self.jobs)


def analyse(batches: list[dict], jobs: list[dict], *, row: dict | None = None) -> dict:
    return orphans.analyse([row or location(batches)], store=FakeStore(jobs), now=NOW)


class Classification(unittest.TestCase):
    def test_a_batch_the_job_still_owns_is_live_and_not_listed(self) -> None:
        result = analyse([batch()], [job(upstream="b-old")])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["by_location"]["L1"]["orphans"], [])

    def test_a_batch_the_job_moved_off_is_superseded(self) -> None:
        result = analyse([batch()], [job(state="running", upstream="b-new",
                                         attempts=[attempt("lost contact with http://x while "
                                                           "polling batch b-old (item 0) for 8s")])])
        orphan = result["by_location"]["L1"]["orphans"][0]
        self.assertEqual(orphan["classification"], "orphan_superseded")
        self.assertEqual(orphan["job"]["state"], "running")
        self.assertEqual(orphan["job"]["superseded_by"], "b-new")
        # The job is still running elsewhere: the old attempt must not be stopped.
        self.assertFalse(orphan["stop"]["allowed"])

    def test_the_old_id_may_live_only_in_the_attempt_log(self) -> None:
        # This is exactly the incident's shape: upstream_job_id holds the new id, and the old one
        # survives only inside the attempt log's sentence.
        result = analyse([batch("12b2212bca")], [
            job(state="succeeded", upstream="new-id",
                attempts=[attempt("lost contact with http://1200 while polling batch "
                                  "12b2212bca (item 0) for 9s")])])
        orphan = result["by_location"]["L1"]["orphans"][0]
        self.assertEqual(orphan["classification"], "orphan_superseded")
        self.assertEqual(orphan["job"]["outcome"], "succeeded")

    def test_a_longer_id_containing_it_does_not_claim_it(self) -> None:
        result = analyse([batch("b-old")], [
            job(state="succeeded", upstream="x-b-old",
                attempts=[attempt("while polling batch xb-oldy")])])
        orphan = result["by_location"]["L1"]["orphans"][0]
        self.assertEqual(orphan["classification"], "orphan_unknown")
        self.assertIsNone(orphan["job"])

    def test_no_job_at_all_is_unknown_and_has_no_stop(self) -> None:
        result = analyse([batch()], [])
        orphan = result["by_location"]["L1"]["orphans"][0]
        self.assertEqual(orphan["classification"], "orphan_unknown")
        self.assertFalse(orphan["stop"]["allowed"])
        self.assertIn("No Gateway job names this batch", orphan["stop"]["reason"])

    def test_another_callers_batch_is_invisible(self) -> None:
        # The owner's own Studio Hub use, or another caller's work, must never be touched.
        result = analyse([batch(label="local-hub-use"), batch("b-theirs", label="someone-else")],
                         [job(state="succeeded", upstream="other")])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["by_location"]["L1"]["orphans"], [])

    def test_running_time_and_progress_come_from_the_hubs_own_row(self) -> None:
        result = analyse([batch()], [])
        orphan = result["by_location"]["L1"]["orphans"][0]
        self.assertEqual(orphan["running_seconds"], 600.0)
        self.assertEqual(orphan["progress"]["done"], 1)
        self.assertEqual(orphan["progress"]["total"], 4)
        self.assertEqual(orphan["progress"]["running"], 1)
        # The Hub's list route publishes no artifact count: "not reported", never 0.
        self.assertIsNone(orphan["progress"]["artifact_count"])
        self.assertIsNone(orphan["progress"]["output_path"])

    def test_a_reported_artifact_count_and_output_path_are_passed_through(self) -> None:
        result = analyse([batch(artifact_count=2, output_path="/tmp/out.png")], [])
        progress = result["by_location"]["L1"]["orphans"][0]["progress"]
        self.assertEqual(progress["artifact_count"], 2)
        self.assertEqual(progress["output_path"], "/tmp/out.png")


class Guard(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="orphan-guard-", dir=Path(_SANDBOX.name)))

    def artifact(self, name: str = "out.png", content: bytes = b"png") -> dict:
        path = self.work / name
        path.write_bytes(content)
        return {"asset_id": "0", "path": str(path), "media_type": "image/png", "bytes": len(content)}

    def verdict(self, jobs: list[dict]) -> dict:
        return analyse([batch()], jobs)["by_location"]["L1"]["orphans"][0]

    def test_succeeded_with_artifacts_on_disk_may_be_stopped(self) -> None:
        orphan = self.verdict([job(state="succeeded", upstream="b-old",
                                   artifacts=[self.artifact()])])
        self.assertTrue(orphan["stop"]["allowed"], orphan["stop"]["reason"])
        self.assertTrue(orphan["job"]["artifacts_complete"])

    def test_succeeded_with_no_artifacts_is_refused(self) -> None:
        orphan = self.verdict([job(state="succeeded", upstream="b-old")])
        self.assertFalse(orphan["stop"]["allowed"])
        self.assertIn("kept no artifact", orphan["stop"]["reason"])

    def test_succeeded_with_an_empty_file_is_refused(self) -> None:
        orphan = self.verdict([job(state="succeeded", upstream="b-old",
                                   artifacts=[self.artifact(content=b"")])])
        self.assertFalse(orphan["stop"]["allowed"])
        self.assertIn("missing or empty", orphan["stop"]["reason"])

    def test_succeeded_with_a_file_gone_is_refused(self) -> None:
        artifact = self.artifact()
        Path(artifact["path"]).unlink()
        orphan = self.verdict([job(state="succeeded", upstream="b-old", artifacts=[artifact])])
        self.assertFalse(orphan["stop"]["allowed"])
        self.assertIn("missing or empty", orphan["stop"]["reason"])

    def test_failed_with_nothing_to_collect_may_be_stopped(self) -> None:
        orphan = self.verdict([job(state="failed", upstream="b-old")])
        self.assertTrue(orphan["stop"]["allowed"], orphan["stop"]["reason"])
        self.assertIn("nothing left to collect", orphan["stop"]["reason"])

    def test_failed_with_an_artifact_still_to_collect_is_refused(self) -> None:
        orphan = self.verdict([job(state="failed", upstream="b-old",
                                   artifacts=[self.artifact()])])
        self.assertFalse(orphan["stop"]["allowed"])
        self.assertIn("still on disk", orphan["stop"]["reason"])

    def test_cancelled_with_nothing_to_collect_may_be_stopped(self) -> None:
        orphan = self.verdict([job(state="cancelled", upstream="b-old")])
        self.assertTrue(orphan["stop"]["allowed"])

    def test_unknown_is_never_offered_a_stop(self) -> None:
        self.assertFalse(self.verdict([])["stop"]["allowed"])


class FailedReadIsNotAnEmptyFleet(unittest.TestCase):
    def test_a_timed_out_site_is_not_checked_not_empty(self) -> None:
        row = location([], reachable=False, jobs_read=None, error="No answer within 6s.",
                       error_kind="timeout")
        result = orphans.analyse([row], store=FakeStore([]), now=NOW)
        self.assertFalse(result["by_location"]["L1"]["checked"])
        self.assertEqual(result["unchecked_sites"], 1)

    def test_a_failed_jobs_read_is_not_checked(self) -> None:
        row = location([], reachable=True, jobs_read=False)
        result = orphans.analyse([row], store=FakeStore([]), now=NOW)
        self.assertFalse(result["by_location"]["L1"]["checked"])

    def test_annotate_publishes_none_not_an_empty_list(self) -> None:
        snapshot = {"locations": [
            location([batch()], reachable=False, error="No answer within 6s."),
            location([batch("b-2")], id="L2"),
        ]}
        orphans.annotate(snapshot, store=FakeStore([]), now=NOW)
        first = snapshot["locations"][0]
        self.assertIsNone(first["orphan_batches"])
        self.assertFalse(first["orphans_checked"])
        self.assertIn("not checked", first["orphans_note"])
        self.assertEqual(snapshot["locations"][1]["orphan_batches"][0]["batch_id"], "b-2")
        self.assertEqual(snapshot["orphan_batches"], 1)
        self.assertEqual(snapshot["orphans_unchecked_sites"], 1)

    def test_a_reachable_row_without_the_flag_is_still_checked(self) -> None:
        # Older cached rows and fixtures predate `jobs_read`; absence is not a failed read.
        row = location([batch()])
        row.pop("jobs_read")
        self.assertTrue(orphans.analyse([row], store=FakeStore([]), now=NOW)
                        ["by_location"]["L1"]["checked"])


class Settings(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="orphan-settings-", dir=Path(_SANDBOX.name))) \
            / "fleet_orphans.json"
        self.started = patch.object(orphans, "SETTINGS_PATH", self.path)
        self.started.start()
        self.addCleanup(self.started.stop)
        orphans.reset_for_tests()

    def test_auto_stop_is_off_by_default(self) -> None:
        self.assertFalse(orphans.settings()["auto_stop"])
        self.assertFalse(orphans.settings()["default"])

    def test_a_string_false_is_refused_not_coerced(self) -> None:
        with self.assertRaises(ValueError):
            orphans.set_settings({"auto_stop": "false"})
        self.assertFalse(orphans.settings()["auto_stop"])

    def test_the_switch_round_trips(self) -> None:
        self.assertTrue(orphans.set_settings({"auto_stop": True})["auto_stop"])
        self.assertTrue(orphans.settings()["auto_stop"])

    def test_auto_stop_off_stops_nothing(self) -> None:
        row = location([batch()])
        jobs = [job(state="succeeded", upstream="b-old")]
        with patch.object(orphans, "_cancel") as cancel:
            self.assertEqual(orphans.maybe_auto_stop({"locations": [row]},
                                                     store=FakeStore(jobs), now=NOW), [])
        cancel.assert_not_called()

    def test_auto_stop_on_applies_the_guard_and_logs(self) -> None:
        orphans.set_settings({"auto_stop": True})
        work = Path(tempfile.mkdtemp(prefix="orphan-auto-", dir=Path(_SANDBOX.name)))
        kept = work / "kept.png"
        kept.write_bytes(b"png")
        allowed = job("job-kept", state="succeeded", upstream="b-kept",
                      artifacts=[{"path": str(kept)}])
        refused = job("job-running", state="running", upstream="b-new",
                      attempts=[attempt("polling batch b-refused")])
        row = location([batch("b-kept"), batch("b-refused")])
        with patch.object(orphans, "_cancel", return_value={"running_signalled": 1}) as cancel, \
                patch.object(orphans, "_audit") as audit:
            stopped = orphans.maybe_auto_stop({"locations": [row]},
                                              store=FakeStore([allowed, refused]), now=NOW)
        self.assertEqual([entry["batch_id"] for entry in stopped], ["b-kept"])
        self.assertEqual(cancel.call_count, 1)
        events = [call.args[0] for call in audit.call_args_list]
        self.assertIn("orphan_stop", events)

    def test_auto_stop_never_touches_an_unknown_orphan(self) -> None:
        orphans.set_settings({"auto_stop": True})
        row = location([batch("b-nobody")])
        with patch.object(orphans, "_cancel") as cancel, patch.object(orphans, "_audit") as audit:
            stopped = orphans.maybe_auto_stop({"locations": [row]}, store=FakeStore([]), now=NOW)
        self.assertEqual(stopped, [])
        cancel.assert_not_called()
        audit.assert_not_called()


class TheStoreFindsAttemptLogIds(unittest.TestCase):
    """The real SQL, not a fake: an old id survives only inside `attempt_log` prose."""

    def setUp(self) -> None:
        self.made: list[str] = []
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        with runtime_store._lock:
            runtime_store._conn.execute(
                f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(self.made))})", self.made)
            runtime_store._conn.commit()

    def submit(self, key: str) -> dict:
        row, _ = runtime_store.submit(modality="image", model_id="qwen-image",
                                      params={"prompt": "x"}, idempotency_key=key,
                                      provider_id="studio-hub-L1")
        self.made.append(row["id"])
        return row

    def test_an_id_only_in_the_attempt_log_is_found(self) -> None:
        row = self.submit("orphan-attempt-log")
        runtime_store.mark_running(row["id"])
        runtime_store.record_upstream_acceptance(row["id"], "new-batch-id")
        runtime_store.record_attempt(
            row["id"], location_id="studio-hub-L1", code="PROVIDER_SUBMISSION_UNCERTAIN",
            detail="lost contact with http://1200 while polling batch old-batch-id (item 0)")
        found = runtime_store.jobs_for_upstream_batches(["old-batch-id"])
        self.assertEqual([item["id"] for item in found], [row["id"]])

    def test_the_current_id_is_found(self) -> None:
        row = self.submit("orphan-current")
        runtime_store.mark_running(row["id"])
        runtime_store.record_upstream_acceptance(row["id"], "current-batch-id")
        found = runtime_store.jobs_for_upstream_batches(["current-batch-id"])
        self.assertEqual([item["id"] for item in found], [row["id"]])


class OwnerOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.started = patch.object(admin_session, "session", return_value={
            "role": "guest", "guest_id": "g1", "name": "Friend"})
        self.started.start()
        self.addCleanup(self.started.stop)

    def request(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            cookies={admin_session.COOKIE: "token"}, method="GET", headers={}, client=None)

    def test_a_guest_is_refused_the_fleet_read(self) -> None:
        with patch.object(admin, "is_loopback", return_value=False), \
                patch.object(admin, "request_is_loopback", return_value=False):
            with self.assertRaises(admin.AdminError) as caught:
                admin.require_admin(self.request(), authorization=None)
        self.assertEqual(caught.exception.status, 403)

    def test_the_new_doors_declare_require_admin(self) -> None:
        # **The routes the Fleet page actually calls**, not a path somebody imagined: the Fleet
        # read that carries the orphan rows, the settings door its auto-stop switch writes, and
        # the cancel route Stop (and auto-stop) post to. An unknown path is a failure, so a
        # renamed route cannot quietly leave this check covering nothing.
        paths = {
            "/admin/api/fleet",
            "/admin/api/settings/fleet-orphans",
            "/admin/api/fleet/controllers/{controller_id}/jobs/{batch_id}/cancel",
        }
        served = {route.path for route in admin.router.routes}
        wired = {route.path for route in admin.router.routes
                 if any(getattr(dep, "dependency", None) is admin.require_admin
                        for dep in (route.dependencies or []))}
        for path in paths:
            self.assertIn(path, served, f"{path} is not a route this Gateway serves")
            self.assertIn(path, wired, f"{path} does not declare require_admin")


class RecordingAdapter:
    def __init__(self) -> None:
        self.requests = []

    def cancel(self, request):
        self.requests.append(request)
        return {"ok": True, "queued_cancelled": 1, "running_signalled": 0, "running_pending": 0}


class RouteGuard(unittest.TestCase):
    """The page draws the button; the server refuses without the guard."""

    def call(self, adapter, payload, *, jobs, batch_row=None):
        definition = types.SimpleNamespace(id="studio-hub-L1", display_name="Blue Room")
        loc = location([batch_row or batch()], id="L1", url="http://10.0.0.2:47873",
                       provider_id="studio-hub-L1")
        with patch.object(admin.fleet, "snapshot", return_value={"locations": [loc]}), \
                patch.object(admin, "store", FakeStore(jobs)), \
                patch.object(admin.registry, "definition", return_value=definition), \
                patch.object(admin.registry.credentials, "resolved", return_value={"hub_token": "t"}), \
                patch.object(admin, "adapter_for", return_value=adapter):
            return admin.fleet_cancel_job("L1", "b-old", payload)

    def test_orphan_stop_refuses_when_the_guard_does_not_hold(self) -> None:
        adapter = RecordingAdapter()
        response = self.call(adapter, {"modality": "image", "orphan_stop": True},
                             jobs=[job(state="succeeded", upstream="b-old")])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(adapter.requests, [], "a refused orphan stop must reach nothing")
        self.assertIn("kept no artifact", json.loads(bytes(response.body))["detail"])

    def test_orphan_stop_reaches_the_hub_when_the_guard_holds(self) -> None:
        adapter = RecordingAdapter()
        work = Path(tempfile.mkdtemp(prefix="orphan-route-", dir=Path(_SANDBOX.name)))
        artifact = work / "out.png"
        artifact.write_bytes(b"png")
        response = self.call(adapter, {"modality": "image", "orphan_stop": True},
                             jobs=[job(state="succeeded", upstream="b-old",
                                       artifacts=[{"path": str(artifact)}])])
        self.assertEqual(response["result"]["queued_cancelled"], 1)
        self.assertEqual(adapter.requests[0].upstream_job_id, "b-old")

    def test_orphan_stop_refuses_an_unknown_batch(self) -> None:
        adapter = RecordingAdapter()
        response = self.call(adapter, {"modality": "image", "orphan_stop": True}, jobs=[])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(adapter.requests, [])

    def test_an_ordinary_cancel_is_unchanged(self) -> None:
        adapter = RecordingAdapter()
        response = self.call(adapter, {"modality": "image"}, jobs=[])
        self.assertEqual(response["result"]["queued_cancelled"], 1)
        self.assertEqual(adapter.requests[0].upstream_job_id, "b-old")


if __name__ == "__main__":
    unittest.main()
