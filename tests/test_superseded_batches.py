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
from gateway import orphans as orphans_module  # noqa: E402
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


    def test_a_hold_that_appears_mid_read_is_left_alone_so_its_batch_name_survives(self) -> None:
        """The re-read must not settle a hold with this attempt's failure.

        `store.fail` settles an `unknown_outcome` hold with the arriving failure and with it
        `recovery_context` — the only place the batch id lives until `record_superseded_batch`
        runs. Measured on review 2026-10-30: the re-read reported the attempt's answer for a row
        the hold sweep had taken, so the cleanup was handed a job that no longer named a batch.
        """
        from gateway.store import RECOVERY_UNKNOWN

        row = self._running_job("hold-reread-1")
        self.next_site = [B]
        adapter, _calls = self._flaky()
        real_answer = worker_module._hub_batch_answer
        turned: list[int] = []

        def answer_then_hold(*a: Any, **k: Any):
            # The hold lands while the job's batch is being asked about: the other copy gave up,
            # or a restart wrote one. This is the window the re-read was added for.
            if not turned:
                turned.append(1)
                self.store.fail(row["id"], code="UNKNOWN_OUTCOME", detail="outcome unknown",
                                recovery_state=RECOVERY_UNKNOWN,
                                recovery_detail="outcome unknown",
                                recovery_context={"batch_id": "batch-a", "item_index": 0,
                                                  "provider_id": A, "location": BASE[A],
                                                  "modality": "image",
                                                  "jobs_path": "/api/hub/jobs"})
            return real_answer(*a, **k)

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(worker_module, "adapter_for", lambda _d: adapter), \
                patch.object(worker_module, "FOLLOW_LIMIT", 0), \
                patch.object(worker_module, "_hub_batch_answer", answer_then_hold), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "running"}):
            worker_module.execute_job(row, self.store, resume=True)

        job = self.store.get(row["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["recovery_state"], RECOVERY_UNKNOWN,
                         "the hold was not settled by this attempt's own failure")
        self.assertEqual((job.get("recovery_context") or {}).get("batch_id"), "batch-a",
                         "the only record of the batch the hold is waiting on is still there")
        self.assertEqual(self.store.superseded_batches(row["id"]), [],
                         "the hold's own reconciliation owns it; nothing is recorded twice")
        self.assertEqual(self.routed, [], "the job was not moved off its hold")


class SweepStartTests(_Case):
    """The cleanup's thread, and the states it must treat as finished (review 2026-10-30)."""

    def test_a_thread_that_cannot_start_leaves_the_cleanup_able_to_run_again(self) -> None:
        worker = worker_module.Worker(self.store)
        started: list[int] = []

        class _Refuses:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        with patch.object(worker_module.threading, "Thread", _Refuses):
            self.assertFalse(worker._start_superseded_sweep(),
                             "a start() that raised is reported, never swallowed as success")
        self.assertFalse(worker._superseded_sweeping.is_set(),
                         "the event is cleared, or every later pass would skip the cleanup for ever")

        with patch.object(worker_module, "reconcile_superseded",
                          lambda _s: started.append(1) or {"checked": 0}):
            self.assertTrue(worker._start_superseded_sweep(), "the next pass tried again")
            self.wait_for(lambda: not worker._superseded_sweeping.is_set())
        self.assertEqual(started, [1])


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

    def test_a_partial_rollup_is_a_finished_batch_not_one_asked_about_for_ever(self) -> None:
        """`partial` is a rollup word; `cancelled_items` is a count and could never match."""
        self.assertIn("partial", worker_module._TERMINAL_UPSTREAM_STATES)
        self.assertNotIn("cancelled_items", worker_module._TERMINAL_UPSTREAM_STATES,
                         "a number in the Hub's summary is not a state word")

        row = self.job("partial-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image", "jobs_path": "/api/hub/jobs"})
        fleet = _Fleet(a_answers_gets=True, a_states=["partial"])

        out = self.sweep(fleet)
        self.assertEqual(out["closed"], 1, out)
        entry = self.store.superseded_batches(row["id"])[0]
        self.assertEqual(entry["status"], "finished_partial", entry)

        again = self.sweep(fleet)
        self.assertEqual(again["checked"], 0, "a closed batch is never asked about again")

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
        before = self.store.get(row["id"])["updated_at"]
        self.assertFalse(self.store.record_upstream_acceptance(row["id"], "batch-late"))
        after = self.store.get(row["id"])
        self.assertIsNone(after["upstream_job_id"], "a settled row is never re-pointed")
        self.assertEqual(after["updated_at"], before,
                         "a late accept is not a change to the settled job's own history")
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


class FlappingSiteTests(_Case):
    """Following a still-running batch is bounded (2 follows), not open-ended."""

    def test_a_flapping_hub_cannot_hold_a_job_running_for_ever(self) -> None:
        row = self.job("flap-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-a")
        calls = {"resume": 0, "generate": 0}

        class _Adapter:
            def resume(self, request):
                calls["resume"] += 1
                if calls["resume"] > 5:
                    # Old code would keep looping; this ends the test instead of hanging it.
                    raise BackendError("too many follows", code="PROVIDER_FAILED",
                                       upstream_settled=True)
                raise BackendError(
                    "lost contact with https://hub-a.test while polling batch batch-a",
                    code="PROVIDER_SUBMISSION_UNCERTAIN", upstream_accepted=True,
                    recovery={"location": BASE[A], "batch_id": "batch-a", "item_index": 0,
                              "jobs_path": "/api/hub/jobs", "lost_contact": True})

            def generate(self, request):
                calls["generate"] += 1
                return BackendResult(content=PNG + b"-from-b", media_type="image/png",
                                     deliver=lambda: {"ok": True})

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(worker_module, "adapter_for", lambda _d: _Adapter()), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "running"}):
            worker_module.execute_job(self.store.get(row["id"]), self.store, resume=True)

        job = self.store.get(row["id"])
        self.assertEqual(calls["resume"], worker_module.FOLLOW_LIMIT + 1,
                         "the initial lost contact plus the bounded follows, then a move")
        self.assertEqual(calls["generate"], 1)
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B))
        entry = self.store.superseded_batches(row["id"])[0]
        self.assertEqual((entry["batch_id"], entry["status"]), ("batch-a", "open"))


class OpenEntryScanTests(_Case):
    def test_an_open_entry_older_than_fifty_resolved_movers_is_still_found(self) -> None:
        old = self.job("old-open")
        self.store.record_superseded_batch(old["id"], {
            "batch_id": "batch-old", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})
        for index in range(55):
            row = self.job(f"resolved-{index}")
            self.store.record_superseded_batch(row["id"], {
                "batch_id": f"batch-{index}", "item_index": 0, "provider_id": A,
                "location": BASE[A], "modality": "image"})
            self.store.resolve_superseded_batch(row["id"], f"batch-{index}", status="stopped")
        # Every resolved mover is newer than the one open entry: a "50 newest jobs" scan misses it.
        self.store._conn.execute("UPDATE jobs SET updated_at = 1.0 WHERE id = ?", (old["id"],))
        self.store._conn.commit()

        found = [entry["batch_id"] for _job, entry in self.store.open_superseded_batches(limit=50)]
        self.assertEqual(found, ["batch-old"])

    def test_an_open_entry_written_without_json_spaces_is_still_found(self) -> None:
        """The SQL pre-filter accepts both spellings; the exact `status` test stays the authority."""
        row = self.job("compact-open")
        compact = json.dumps([{
            "batch_id": "batch-compact", "item_index": 0, "provider_id": "studio-hub-a",
            "location": "https://hub-a.test", "modality": "image",
            "jobs_path": "/api/hub/jobs", "status": "open"}], separators=(",", ":"))
        self.store._conn.execute(
            "UPDATE jobs SET superseded_batches = ? WHERE id = ?", (compact, row["id"]))
        self.store._conn.commit()
        raw = self.store._conn.execute(
            "SELECT superseded_batches FROM jobs WHERE id = ?", (row["id"],)).fetchone()[0]
        self.assertNotIn('": "', raw, "the row really is the compact spelling")

        found = [entry["batch_id"] for _job, entry in self.store.open_superseded_batches(limit=50)]
        self.assertEqual(found, ["batch-compact"])


class RejectedResultTests(_Case):
    def _running_with_superseded(self, key: str) -> dict[str, Any]:
        row = self.job(key)
        self.store.record_route(row["id"], B)
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-b")
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image", "jobs_path": "/api/hub/jobs"})
        return self.store.get(row["id"])

    def test_a_finished_result_the_done_path_could_not_keep_yet_stays_open(self) -> None:
        """Only `INVALID_ARTIFACT` is a verdict; a bad moment is retried on the next sweep."""
        row = self._running_with_superseded("transient-1")
        fleet = _Fleet(a_answers_gets=True, a_states=["done"])

        def adopt(*_a, **_k):
            return BackendResult(content=PNG, media_type="image/png")

        with patch.object(worker_module, "registry", _Registry()), \
                self.client_patch(fleet), \
                patch.object(hub_module, "adopt_batch", adopt), \
                patch.object(worker_module, "settle_result",
                             side_effect=OSError("the artifact directory went away")):
            out = worker_module.reconcile_superseded(self.store)
        entry = self.store.superseded_batches(row["id"])[0]
        self.assertEqual(out["closed"], 0, out)
        self.assertEqual(entry["status"], "open", "a disk error closed the only copy's name")
        self.assertEqual(out["left"], 1, out)

        # The next sweep asks the site again and settles it normally.
        with patch.object(worker_module, "registry", _Registry()), \
                self.client_patch(fleet), \
                patch.object(hub_module, "adopt_batch", adopt):
            again = worker_module.reconcile_superseded(self.store)
        self.assertEqual(again["adopted"], 1, again)
        self.assertEqual(self.store.superseded_batches(row["id"])[0]["status"], "adopted")

    def test_a_rejected_result_is_named_once_and_never_retried(self) -> None:
        row = self._running_with_superseded("reject-1")
        fleet = _Fleet(a_answers_gets=True, a_states=["done"])

        def adopt(*_a, **_k):
            return BackendResult(content=b"", media_type="image/png")  # INVALID_ARTIFACT

        with patch.object(worker_module, "registry", _Registry()), \
                self.client_patch(fleet), \
                patch.object(hub_module, "adopt_batch", adopt):
            out = worker_module.reconcile_superseded(self.store)
        with patch.object(worker_module, "registry", _Registry()), self.client_patch(fleet):
            again = worker_module.reconcile_superseded(self.store)

        self.assertEqual(out["closed"], 1, out)
        entry = self.store.superseded_batches(row["id"])[0]
        self.assertEqual(entry["status"], "result_rejected")
        self.assertIn("INVALID_ARTIFACT", entry["detail"])
        self.assertEqual(again["checked"], 0, "a refused result is not downloaded again")
        self.assertEqual(self.store.get(row["id"])["state"], "running")

    def test_a_rejected_adoption_moves_the_job_on_instead_of_an_internal_error(self) -> None:
        fleet = _Fleet(a_answers_gets=False)
        row = self.job("reject-2")

        def adopt(*_a, **_k):
            return BackendResult(content=b"", media_type="image/png")

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                self.client_patch(fleet), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "POLL_DROP_GRACE_S", 0.05), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "done"}), \
                patch.object(hub_module, "adopt_batch", adopt):
            worker_module.execute_job(row, self.store)
            self.wait_for(lambda: not any(t.name.startswith("gateway-cancel") and t.is_alive()
                                          for t in __import__("threading").enumerate()))

        job = self.store.get(row["id"])
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B),
                         job.get("error_detail"))
        self.assertIsNone(job["error_code"])
        entry = self.store.superseded_batches(job["id"])[0]
        self.assertEqual((entry["batch_id"], entry["status"]), ("batch-a", "open"))


class HoldTests(_Case):
    def test_an_unresolved_hold_is_not_settled_so_its_batch_is_not_stopped(self) -> None:
        from gateway.store import RECOVERY_UNKNOWN

        row = self.job("hold-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.fail(row["id"], code="UNKNOWN_OUTCOME", detail="outcome unknown",
                        recovery_state=RECOVERY_UNKNOWN, recovery_detail="outcome unknown")
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})
        fleet = _Fleet(a_answers_gets=True, a_states=["running"])

        out = self.sweep(fleet)

        self.assertEqual(out["stopped"], 0, out)
        self.assertEqual(fleet.deletes, [],
                         "the hold is still waiting for this batch's answer; it is not stoppable")
        self.assertEqual(self.store.superseded_batches(row["id"])[0]["status"], "open")


class SweepBudgetTests(_Case):
    def test_the_cleanup_has_its_own_thread_and_never_queues_behind_holds(self) -> None:
        worker = worker_module.Worker(self.store)
        cleaned: list[int] = []
        with patch.object(worker_module, "reconcile_holds", lambda _s: {"checked": 0}), \
                patch.object(worker_module, "reconcile_superseded",
                             lambda _s: cleaned.append(1) or {}):
            worker._sweep_holds()
            self.assertEqual(cleaned, [], "the hold sweep must not carry the cleanup")
            worker._sweep_superseded()
            self.assertEqual(cleaned, [1])

    def test_one_sweep_stops_at_its_wall_clock_budget(self) -> None:
        row = self.job("budget-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})
        with patch.object(worker_module, "SUPERSEDED_SWEEP_BUDGET_S", 0.0):
            with patch.object(worker_module, "registry", _Registry()), \
                    self.client_patch(_Fleet(a_answers_gets=True, a_states=["running"])):
                out = worker_module.reconcile_superseded(self.store)
        self.assertEqual(out["checked"], 0, out)


class NoMoveTests(_Case):
    def test_a_batch_is_recorded_even_when_no_other_site_can_take_the_job(self) -> None:
        self.next_site = []
        fleet = _Fleet(a_answers_gets=False)
        job = self.run_job(fleet, self.job("nomove-1"))
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["recovery_required"])
        entry = self.store.superseded_batches(job["id"])[0]
        self.assertEqual((entry["batch_id"], entry["status"]), ("batch-a", "not_moved"),
                         "recorded before the move, then closed because the job stayed")


class DiskLowTests(_Case):
    def test_a_finished_batch_the_disk_refused_is_recorded_for_the_sweep(self) -> None:
        from gateway import diskguard

        row = self.job("disk-1")
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-a")
        fleet = _Fleet(a_answers_gets=False)

        def adopt(*_a, **_k):
            return BackendResult(content=PNG, media_type="image/png")

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                self.client_patch(fleet), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "POLL_DROP_GRACE_S", 0.05), \
                patch.object(hub_module, "batch_snapshot",
                             lambda *_a, **_k: {"known": True, "state": "done"}), \
                patch.object(hub_module, "adopt_batch", adopt), \
                patch.object(diskguard, "require_room",
                             side_effect=diskguard.DiskLow("not enough free space")):
            worker_module.execute_job(self.store.get(row["id"]), self.store, resume=True)

        job = self.store.get(row["id"])
        self.assertEqual((job["state"], job["error_code"]), ("failed", "DISK_LOW"))
        entry = self.store.superseded_batches(row["id"])[0]
        self.assertEqual((entry["batch_id"], entry["status"]), ("batch-a", "open"),
                         "the finished work is not forgotten when the disk said no")


class PaidLaneTests(_Case):
    def test_a_paid_adapter_cancel_is_never_called(self) -> None:
        called: list[str] = []

        class _Paid:
            def cancel(self, request):
                called.append(request.provider_id)
                return {"ok": True}

        registry = _Registry()
        registry.kind_of[A] = "openai_compat"
        with patch.object(worker_module, "registry", registry), \
                patch.object(worker_module, "adapter_for", lambda _d: _Paid()):
            answer = worker_module._cancel_hub_batch(A, "image", "batch-a", 0, BASE[A])
        self.assertFalse(answer["ok"])
        self.assertEqual(called, [], "a paid lane's cancel has billing consequences and is not ours")


class GuestRowTests(unittest.TestCase):
    def test_the_guest_reachable_job_row_never_carries_a_site_address(self) -> None:
        from gateway import admin

        view = admin.superseded_view({"superseded_batches": [
            {"batch_id": "batch-a", "provider_id": A, "location": BASE[A], "status": "open"}]})
        self.assertEqual(view[0]["batch_id"], "batch-a")
        self.assertEqual(view[0]["provider_id"], A)
        self.assertNotIn("location", view[0], "the address is fleet topology, not a guest's history")


class SwitchTests(_Case):
    def _settings(self) -> Path:
        # One file per test: the switches are global state in the process, and a shared path
        # would let one test read another's choice.
        path = Path(_SANDBOX.name) / f"fleet-orphans-{self.id().split('.')[-1]}.json"
        patcher = patch.object(orphans_module, "SETTINGS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_the_replaced_copy_switch_is_on_by_default_and_independent(self) -> None:
        self._settings()
        self.assertTrue(orphans_module.settings()["superseded_stop"])
        self.assertFalse(orphans_module.settings()["auto_stop"])
        orphans_module.set_settings({"auto_stop": True})
        self.assertTrue(orphans_module.settings()["superseded_stop"],
                        "the general switch never changes this one")
        orphans_module.set_settings({"superseded_stop": False})
        self.assertTrue(orphans_module.settings()["auto_stop"],
                        "this switch never changes the general one")
        self.assertFalse(orphans_module.settings()["superseded_stop"])

    def test_off_stops_nothing_but_a_finished_copy_is_still_collected(self) -> None:
        self._settings()
        orphans_module.set_settings({"superseded_stop": False})
        fleet = _Fleet(a_answers_gets=False)
        job = self.run_job(fleet, self.job("switch-1"))
        self.assertEqual(job["state"], "succeeded")
        fleet.answer["a"] = {"gets": True, "states": ["running"]}
        out = self.sweep(fleet)
        self.assertEqual(out["stopped"], 0, out)
        self.assertEqual(fleet.deletes, [], "the owner turned the automatic stop off")

        # Collection is not a stop: a finished copy is still adopted with the switch off.
        row = self.job("switch-2")
        self.store.record_route(row["id"], B)
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-b")
        self.store.record_superseded_batch(row["id"], {
            "batch_id": "batch-a", "item_index": 0, "provider_id": A,
            "location": BASE[A], "modality": "image"})
        fleet.answer["a"] = {"gets": True, "states": ["done"]}
        fleet.answer["b"] = {"gets": True, "states": ["running"]}
        carried = self.sweep(fleet)
        self.assertEqual(carried["adopted"], 1, carried)
        self.assertEqual(fleet.deletes, [], "the losing copy is not stopped while the switch is off")
        statuses = {entry["batch_id"]: entry["status"]
                    for entry in self.store.superseded_batches(row["id"])}
        self.assertEqual(statuses["batch-b"], "open",
                         "the loser is recorded for the page and a later switch-on")

    def test_both_switches_are_in_the_backup(self) -> None:
        from gateway import backup, config

        declared = dict(backup.sources())["gateway/data/fleet_orphans.json"]
        self.assertEqual(declared, Path(config.DATA_DIR) / "fleet_orphans.json")
        self.assertEqual(backup.target_for("gateway/data/fleet_orphans.json"),
                         Path(config.DATA_DIR) / "fleet_orphans.json")


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
