#!/usr/bin/env python3
"""A batch this Gateway was moved off is adopted, or stopped once the guard holds (0.2.211).

Three for three on 2026-10-29: a Studio Hub site dropped off the network mid-job, the worker
re-dispatched the job under a **new** batch id elsewhere, and the old batch kept running on the
site that came back. The job succeeded, so no gate saw anything — only the 0.2.210 orphan finder
reported it, and a person had to press Stop.

The fix is at the source, and this file pins it:

* before moving off a silent site, the batch is read once more — still running is followed, done is
  collected, and only a site that does not answer is re-dispatched;
* a batch the Gateway itself moved off is recorded in a **structured field** (`superseded_batches`),
  not only in attempt-log prose;
* when that site answers again, whichever copy finished first wins: a finished superseded batch is
  adopted when the job has no result, recorded as a late duplicate when it already delivered, and
  a still-running one is stopped exactly once, under `orphans.stop_allowed` — the guard the Fleet
  Stop button uses. Nothing else is ever touched.

Mocked Hub transport and fake adapters only: no live Hub, no generation, no installed app.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-superseded-")

import httpx  # noqa: E402

from gateway import concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendResult  # noqa: E402
from gateway.store import JobStore  # noqa: E402

A, B = "studio-hub-a", "studio-hub-b"
#: A real PNG header: the Gateway refuses an artifact that is not the file its type says.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
BASE = {A: "https://hub-a.test", B: "https://hub-b.test"}


class _Fleet:
    """Two Hubs. `a_answers_gets=False` is a site that accepted its batch and then went silent."""

    def __init__(self, *, a_answers_gets: bool = False, a_states: list[str] | None = None,
                 b_states: list[str] | None = None) -> None:
        self.answer = {
            "a": {"gets": a_answers_gets, "states": list(a_states or ["running"])},
            "b": {"gets": True, "states": list(b_states or ["done"])},
        }
        self.calls: list[tuple[str, str, str]] = []
        self.created = {"a": 0, "b": 0}
        #: Deletes one Hub actually **answered** — a request to a host that is down is in `calls`,
        #: not here, which is the difference the stop assertions need.
        self.deletes: list[str] = []
        self.seen: set[tuple[str, str]] = set()

    def host_of(self, request: httpx.Request) -> str:
        return "a" if "hub-a" in request.url.host else "b"

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = self.host_of(request)
        state = self.answer[host]
        method, path = request.method, request.url.path
        self.calls.append((host, method, path))
        if method == "POST" and path == "/api/hub/jobs":
            body = json.loads(request.content)
            key = str(body.get("clientRequestId") or "")
            if (host, key) in self.seen:
                return httpx.Response(200, json={"batch_id": f"batch-{host}", "replayed": True})
            self.seen.add((host, key))
            self.created[host] += 1
            return httpx.Response(200, json={"batch_id": f"batch-{host}", "replayed": False})
        if not state["gets"]:
            raise httpx.ConnectError("no route to host")
        if method == "POST" and path.endswith("/ack"):
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and path.endswith("/artifact"):
            return httpx.Response(200, content=PNG + f"-from-{host}".encode(),
                                  headers={"content-type": "image/png"})
        if method == "GET" and path.startswith("/api/hub/jobs/"):
            states = state["states"]
            value = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json={"items": [
                {"index": 0, "state": value, "terminal_result": {"machine_id": host}}]})
        if method == "DELETE":
            self.deletes.append(f"{host}:{path}")
            return httpx.Response(200, json={"ok": True, "queued_cancelled": 0,
                                             "running_signalled": 1, "running_pending": 0})
        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})

    def saw(self, host: str, method: str) -> list[str]:
        return [path for h, m, path in self.calls if (h, m) == (host, method)]


class _Registry:
    def __init__(self) -> None:
        self.kind_of = {A: "studio_hub", B: "studio_hub"}
        self.credentials = SimpleNamespace(is_enabled=lambda _p: True, resolved=lambda _p: {})
        self.models = SimpleNamespace(enabled_models=lambda _m: [
            (SimpleNamespace(id=pid), {"model_id": "model/test"}) for pid in self.kind_of])

    def definition(self, provider_id: str) -> Any:
        return SimpleNamespace(id=provider_id, kind=self.kind_of[provider_id],
                               base_url=BASE.get(provider_id, ""), display_name=provider_id)


class _Case(unittest.TestCase):
    modality = "image"

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(_SANDBOX.name) / f"{self.id().split('.')[-1]}.sqlite3")
        self.addCleanup(self.store.close)
        self.routed: list[set[str]] = []
        self.next_site: list[str] = [B]

    def route(self, model_id, modality, *, exclude_locations=None, **_k):
        self.routed.append(set(exclude_locations or ()))
        return (SimpleNamespace(id=self.next_site.pop(0)), "") if self.next_site else (None, "")

    def job(self, key: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        row, _ = self.store.submit(modality=self.modality, model_id="model/test",
                                   params=params or {"prompt": "x"}, idempotency_key=key,
                                   provider_id=A)
        return row

    def client_patch(self, fleet: _Fleet):
        real = httpx.Client

        def factory(*a: Any, **k: Any) -> httpx.Client:
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fleet.handler), **k)

        return patch.object(hub_module.httpx, "Client", side_effect=factory)

    def run_job(self, fleet: _Fleet, row: dict[str, Any]) -> dict[str, Any]:
        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                self.client_patch(fleet), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "POLL_DROP_GRACE_S", 0.05):
            worker_module.execute_job(row, self.store)
            self.wait_for(lambda: not any(t.name.startswith("gateway-cancel") and t.is_alive()
                                          for t in __import__("threading").enumerate()))
        return self.store.get(row["id"])

    def sweep(self, fleet: _Fleet) -> dict[str, int]:
        with patch.object(worker_module, "registry", _Registry()), \
                self.client_patch(fleet):
            return worker_module.reconcile_superseded(self.store)

    @staticmethod
    def wait_for(predicate, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            time.sleep(0.01)


class AdoptBeforeRedispatchTests(_Case):
    """The read that happens before a job is moved off a silent site."""

    def _running_job(self, key: str) -> dict[str, Any]:
        row = self.job(key)
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-a")
        return self.store.get(row["id"])

    def _flaky(self, *, lost: bool = True):
        calls: list[str] = []

        class _Adapter:
            def generate(self, request):
                calls.append(f"generate:{request.provider_id}")
                raise AssertionError("a lost-contact job must not submit another batch")

            def resume(self, request):
                calls.append(f"resume:{request.upstream_job_id}")
                if len(calls) == 1:
                    raise BackendError(
                        "lost contact with https://hub-a.test while polling batch batch-a",
                        code="PROVIDER_SUBMISSION_UNCERTAIN", upstream_accepted=True,
                        recovery={"location": BASE[A], "batch_id": "batch-a", "item_index": 0,
                                  "jobs_path": "/api/hub/jobs", "lost_contact": lost},
                    )
                return BackendResult(content=PNG + b"-from-a", media_type="image/png",
                                     deliver=lambda: {"ok": True})

        return _Adapter(), calls

    def test_lost_contact_with_the_batch_still_running_follows_it_not_a_second_submit(self) -> None:
        fleet = _Fleet()
        row = self._running_job("stay-1")
        adapter, calls = self._flaky()
        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(worker_module, "adapter_for", lambda _d: adapter), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "running"}):
            worker_module.execute_job(row, self.store, resume=True)
        job = self.store.get(row["id"])
        self.assertEqual(job["state"], "succeeded", job.get("error_detail"))
        self.assertEqual(calls, ["resume:batch-a", "resume:batch-a"],
                         "the same batch was followed; nothing was submitted")
        self.assertEqual(job["upstream_job_id"], "batch-a")
        self.assertEqual(self.routed, [], "no other site was tried")

    def test_lost_contact_with_the_batch_finished_collects_it_instead(self) -> None:
        row = self._running_job("done-1")
        adapter, calls = self._flaky()
        adopted: list[tuple[str, str]] = []

        def adopt(request, batch_id, **_k):
            adopted.append((request.provider_id, batch_id))
            return BackendResult(content=PNG + b"-from-a", media_type="image/png",
                                 deliver=lambda: {"ok": True})

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(worker_module, "adapter_for", lambda _d: adapter), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "done"}), \
                patch.object(hub_module, "adopt_batch", adopt):
            worker_module.execute_job(row, self.store, resume=True)
        job = self.store.get(row["id"])
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A), job.get("error_detail"))
        self.assertEqual(adopted, [(A, "batch-a")])
        self.assertEqual(calls, ["resume:batch-a"], "no second submit, no second poll loop")
        self.assertEqual(self.routed, [])


class SupersededCleanupTests(_Case):
    """After a re-dispatch: record it, then reconcile when the old site answers."""

    def test_the_batch_is_recorded_in_a_structured_field_when_the_job_is_moved(self) -> None:
        fleet = _Fleet(a_answers_gets=False)
        job = self.run_job(fleet, self.job("record-1"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B),
                         job.get("error_detail"))
        entries = self.store.superseded_batches(job["id"])
        self.assertEqual([entry["batch_id"] for entry in entries], ["batch-a"])
        self.assertEqual(entries[0]["provider_id"], A)
        self.assertEqual(entries[0]["location"], BASE[A])
        self.assertEqual(entries[0]["status"], "open")

    def test_the_new_copy_finishes_first_and_the_old_site_is_stopped_once(self) -> None:
        fleet = _Fleet(a_answers_gets=False)
        job = self.run_job(fleet, self.job("first-1"))
        self.assertEqual(job["state"], "succeeded", job.get("error_detail"))
        self.assertEqual(fleet.deletes, [], "the site was down: the best-effort cancel failed")

        fleet.answer["a"] = {"gets": True, "states": ["running"]}
        first = self.sweep(fleet)
        self.assertEqual(first["stopped"], 1, first)
        self.assertEqual(fleet.deletes, ["a:/api/hub/jobs/batch-a"])

        # It resolved: a second sweep asks nothing and stops nothing again.
        second = self.sweep(fleet)
        self.assertEqual(second["checked"], 0, second)
        self.assertEqual(fleet.deletes, ["a:/api/hub/jobs/batch-a"], "stopped exactly once")
        entries = self.store.superseded_batches(job["id"])
        self.assertEqual(entries[0]["status"], "stopped")

        # A stop nobody clicked is explained in the startup audit.
        from gateway import startup_control
        events = [row.get("event") for row in startup_control.recent_audit(limit=20)]
        self.assertIn("superseded_stop", events)

    def test_the_old_copy_finishes_first_and_is_adopted_then_the_loser_stopped(self) -> None:
        row = self.job("reverse-1")
        self.store.record_route(row["id"], B)
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-b")
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image", "jobs_path": "/api/hub/jobs"})

        fleet = _Fleet(a_answers_gets=True, a_states=["done"], b_states=["running"])
        out = self.sweep(fleet)

        job = self.store.get(row["id"])
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B))
        self.assertEqual(Path(job["asset_path"]).read_bytes(), PNG + b"-from-a",
                         "the copy that finished first is the one collected")
        self.assertEqual(out["adopted"], 1, out)
        self.assertEqual(out["stopped"], 1, out)
        self.assertEqual(fleet.deletes, ["b:/api/hub/jobs/batch-b"],
                         "the losing copy was stopped once")
        statuses = {entry["batch_id"]: entry["status"]
                    for entry in self.store.superseded_batches(job["id"])}
        self.assertEqual(statuses["batch-a"], "adopted")
        self.assertEqual(statuses["batch-b"], "stopped")

    def test_a_finished_late_duplicate_is_recorded_and_never_delivered(self) -> None:
        fleet = _Fleet(a_answers_gets=False)
        job = self.run_job(fleet, self.job("dupe-1"))
        self.assertEqual(job["state"], "succeeded")
        before = Path(job["asset_path"]).read_bytes()

        fleet.answer["a"] = {"gets": True, "states": ["done"]}
        out = self.sweep(fleet)

        self.assertEqual(out["duplicates"], 1, out)
        self.assertEqual([path for path in fleet.saw("a", "GET") if path.endswith("/artifact")],
                         [], "a duplicate's bytes are never fetched")
        after = self.store.get(job["id"])
        self.assertEqual(after["state"], "succeeded")
        self.assertEqual(Path(after["asset_path"]).read_bytes(), before)
        entries = self.store.superseded_batches(job["id"])
        self.assertEqual(entries[0]["status"], "finished_after_delivery")

    def test_a_finished_copy_beside_a_failed_job_is_kept_not_lost(self) -> None:
        row = self.job("kept-late-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.fail(row["id"], code="PROVIDER_FAILED", detail="the new copy failed")
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})

        fleet = _Fleet(a_answers_gets=True, a_states=["done"])
        out = self.sweep(fleet)

        job = self.store.get(row["id"])
        self.assertEqual(job["state"], "failed", "a late success never rewrites the verdict")
        self.assertEqual(Path(job["asset_path"]).read_bytes(), PNG + b"-from-a",
                         "the finished copy is kept on disk beside the failure")
        self.assertEqual(job["late_result_state"], "succeeded")
        self.assertEqual(out["duplicates"], 1, out)
        self.assertEqual(fleet.deletes, [], "a failed job with a kept artifact is never stopped")

    def test_a_batch_the_hub_never_heard_of_is_closed_not_stopped(self) -> None:
        fleet = _Fleet(a_answers_gets=True, a_states=["done"])
        row = self.job("gone-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})

        def snapshot(*_a, **_k):
            return {"known": False}

        with patch.object(worker_module, "registry", _Registry()), \
                self.client_patch(fleet), \
                patch.object(hub_module, "batch_snapshot", snapshot):
            out = worker_module.reconcile_superseded(self.store)
        self.assertEqual(out["closed"], 1, out)
        self.assertEqual(fleet.deletes, [])
        self.assertEqual(self.store.superseded_batches(row["id"])[0]["status"], "not_found")

    def test_a_running_job_keeps_both_copies_and_a_batch_nothing_names_is_never_touched(self) -> None:
        row = self.job("live-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})
        fleet = _Fleet(a_answers_gets=True, a_states=["running"])
        out = self.sweep(fleet)
        self.assertEqual(out["stopped"], 0, "the job is still running: its other copy may win")
        self.assertEqual(fleet.deletes, [],
                         "and a batch no structured record names is never reached at all")
        self.assertEqual(self.store.superseded_batches(row["id"])[0]["status"], "open")


class GuardTests(_Case):
    def test_the_cleanup_uses_the_orphans_guard(self) -> None:
        from gateway import orphans

        tmp = Path(_SANDBOX.name) / "kept.png"
        tmp.write_bytes(PNG)
        kept = {"state": "succeeded", "artifacts_json": json.dumps([
            {"asset_id": "0", "path": str(tmp), "media_type": "image/png", "bytes": len(PNG)}])}
        self.assertTrue(orphans.stop_allowed(kept)["allowed"])

        empty = {**kept, "artifacts_json": "[]"}
        self.assertFalse(orphans.stop_allowed(empty)["allowed"])
        self.assertFalse(orphans.stop_allowed({"state": "running"})["allowed"])
        self.assertFalse(orphans.stop_allowed(None)["allowed"])

        failed_with_artifact = {**kept, "state": "failed"}
        self.assertFalse(orphans.stop_allowed(failed_with_artifact)["allowed"],
                         "collection comes first")
        self.assertTrue(orphans.stop_allowed({"state": "failed", "artifacts_json": "[]"})["allowed"])


class SettledJobNeverDispatchesAgainTests(_Case):
    def test_a_batch_accepted_after_the_job_settled_is_recorded_not_dropped(self) -> None:
        row = self.job("late-accept-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.succeed(row["id"], asset_path="/already.png", media_type="image/png",
                           artifacts=[{"asset_id": "0", "path": "/already.png",
                                       "media_type": "image/png", "bytes": 1}],
                           result={"text": None, "payload": {}})
        self.assertFalse(self.store.record_upstream_acceptance(row["id"], "batch-late"))
        after = self.store.get(row["id"])
        self.assertIsNone(after["upstream_job_id"], "a settled row is never re-pointed")
        entries = self.store.superseded_batches(row["id"])
        self.assertEqual([entry["batch_id"] for entry in entries], ["batch-late"],
                         "the accepted batch is owned by the cleanup, not orphaned")
        self.assertEqual(entries[0]["status"], "open")

    def test_a_losing_copy_that_answers_never_submits_a_third_batch(self) -> None:
        row = self.job("settled-1")
        job_id = row["id"]
        self.assertTrue(self.store.mark_running(job_id))
        self.store.succeed(job_id, asset_path="/already.png", media_type="image/png",
                           artifacts=[{"asset_id": "0", "path": "/already.png",
                                       "media_type": "image/png", "bytes": 1}],
                           result={"text": None, "payload": {}})
        submitted: list[str] = []

        class _Adapter:
            def resume(self, request):
                raise BackendError("hub item cancelled: stopped by the Gateway",
                                   code="PROVIDER_FAILED", upstream_settled=True)

            def generate(self, request):
                submitted.append(request.provider_id)
                raise BackendError("must not be reached", code="PROVIDER_FAILED")

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(worker_module, "adapter_for", lambda _d: _Adapter()):
            worker_module._dispatch(self.store.get(job_id), self.store, A, "model/test",
                                    "image", {}, "batch-a")
        self.assertEqual(submitted, [], "a settled job is never re-dispatched")
        self.assertEqual(self.routed, [])
        after = self.store.get(job_id)
        self.assertEqual(after["state"], "succeeded")
        self.assertEqual(after["late_result_state"], "failed")


class StoreMigrationTests(unittest.TestCase):
    """A 0.2.210 store opens with the new column, and its rows survive."""

    def test_a_0_2_210_database_gains_the_field_and_keeps_its_rows(self) -> None:
        path = Path(_SANDBOX.name) / "old-0210.sqlite3"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, modality TEXT NOT NULL,
                model_id TEXT, provider_id TEXT, state TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}', asset_path TEXT, media_type TEXT,
                result_json TEXT, error_code TEXT, error_detail TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, next_action_at REAL,
                retryable INTEGER NOT NULL DEFAULT 0, recovery_state TEXT,
                recovery_detail TEXT, cancel_requested_at REAL, cancel_state TEXT,
                dispatch_started_at REAL, attempt_count INTEGER NOT NULL DEFAULT 0,
                upstream_job_id TEXT, late_result_state TEXT, late_error_code TEXT,
                late_error_detail TEXT, caller TEXT, requested_by TEXT, defer_reason TEXT
            );
            """
        )
        now = time.time()
        old.execute(
            "INSERT INTO jobs (id, idempotency_key, modality, model_id, provider_id, state,"
            " params, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-job", "old-key", "image", "model/test", A, "succeeded", "{}", now, now),
        )
        old.commit()
        old.close()

        store = JobStore(path)
        self.addCleanup(store.close)
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(jobs)")}
        self.assertIn("superseded_batches", columns)
        indexes = {row["name"] for row in store._conn.execute("PRAGMA index_list(jobs)")}
        self.assertIn("jobs_superseded", indexes)

        kept = store.get("old-job")
        self.assertEqual(kept["state"], "succeeded", "the 0.2.210 row is untouched")
        self.assertIsNone(kept["superseded_batches"])

        self.assertTrue(store.record_superseded_batch("old-job", {
            "batch_id": "batch-old", "provider_id": A, "modality": "image"}))
        self.assertEqual(store.superseded_batches("old-job")[0]["status"], "open")
        self.assertTrue(store.resolve_superseded_batch("old-job", "batch-old",
                                                       status="stopped"))
        self.assertEqual(store.open_superseded_batches(), [],
                         "a resolved entry leaves nothing for the sweep")


if __name__ == "__main__":
    unittest.main()
