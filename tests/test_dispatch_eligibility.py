#!/usr/bin/env python3
"""Dispatch eligibility — fail fast, per-candidate reasons, cancellation and retry.

The live defect (2026-11-14): job `646570e3…` sat pending about four hours with
`selected_machine: null`. Its only trace candidate was a location offline for seven
days, and nothing on the job said so; Story's retry then answered "image job was
cancelled at the Gateway" because it reused the cancelled job. These tests pin the
accepted contract:

  * a job no reachable location can run fails with `DISPATCH_NO_ELIGIBLE_MACHINE`,
    the reason per location, and the time — `/upstream` exposes them;
  * every rejection is recorded in the fixed vocabulary, never silently dropped;
  * an offline location is not a candidate; a live location beside it is chosen;
  * a cancelled job carries who/why/where, and a retry is a **fresh** job that
    keeps the original cause; the cancelled row is immutable.
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
sys.path.insert(0, str(ROOT / "tests"))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env(prefix="gateway-dispatch-eligibility-")

from gateway import admin, concurrency, fleet  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

MODEL = "qwen-image-2-1-turbo"
MODALITY = "image"


def _pid(location_id: str) -> str:
    return fleet.provider_id_for(location_id)


def _site(location_id: str, *, reachable: bool = True, paused: bool = False,
          drained: bool = False, supply: dict | None = None,
          last_seen_at: float | None = None, models: list[str] | None = None) -> dict:
    row = {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": reachable, "machines": [], "machine_summary": {"reachable": 1},
        "running_jobs": [],
        "capacity": {"queue_depth": 0, "by_operation": {
            "image.text_to_image": {"eligible_physical_machine_slots_total": 1,
                                    "available_physical_machine_slots": 1}}},
    }
    if supply is not None:
        row["model_supply"] = supply
    if paused:
        row["paused_at"] = time.time()
    if drained:
        row["drained"] = True
    if last_seen_at is not None:
        row["machines"] = [{"id": f"{location_id}-mac", "reachable": True,
                            "node_last_seen_at": last_seen_at}]
    row["_models"] = models if models is not None else [MODEL]
    return row


def _definition(provider_id: str):
    return types.SimpleNamespace(id=provider_id, kind="studio_hub",
                                 display_name=provider_id)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sites: list[dict] = []

        def enabled(modality):
            return [(_definition(row["provider_id"]),
                     {"model_id": model, "modality": modality, "raw": {}})
                    for row in self.sites for model in row.get("_models") or []
                    if modality == MODALITY]

        for patcher in (
            patch.object(fleet, "snapshot", lambda *a, **k: {"locations": self.sites}),
            patch.object(registry.models, "enabled_models", enabled),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store, interval_s=10.0, pool_size=0)
        self._n = 0

    def job(self, *, created_ago: float = 120.0, site: str = "a") -> dict:
        self._n += 1
        row, _ = self.store.submit(
            modality=MODALITY, model_id=MODEL, params={},
            idempotency_key=f"job-{self._n}", provider_id=_pid(site))
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET next_action_at = 0, created_at = ? WHERE id = ?",
                (time.time() - created_ago, row["id"]))
            self.store._conn.commit()
        return self.store.get(row["id"])


class EligibilityTests(_Base):
    def test_no_candidate_fails_fast_with_reason(self) -> None:
        self.sites.append(_site("a", reachable=False))
        job = self.job()
        self.assertTrue(self.worker._no_eligible_machine(job))
        settled = self.store.get(job["id"])
        self.assertEqual(settled["state"], "failed")
        self.assertEqual(settled["error_code"], "DISPATCH_NO_ELIGIBLE_MACHINE")
        self.assertIsNotNone(settled["finished_at"])
        self.assertIn("offline", settled["error_detail"])
        dispatch = json.loads(settled["result_json"])["payload"]["dispatch"]
        self.assertEqual(dispatch["code"], "DISPATCH_NO_ELIGIBLE_MACHINE")
        self.assertEqual(dispatch["origin"], "gateway")
        self.assertTrue(dispatch["checked_at"])
        self.assertIsNone(dispatch["selected"])
        self.assertEqual(dispatch["candidates"][0]["reason"], "offline")
        self.assertEqual(dispatch["candidates"][0]["location_id"], "a")

    def test_a_young_job_gets_the_grace(self) -> None:
        self.sites.append(_site("a", reachable=False))
        job = self.job(created_ago=1.0)
        self.assertFalse(self.worker._no_eligible_machine(job))
        self.assertEqual(self.store.get(job["id"])["state"], "accepted")

    def test_a_full_fleet_is_a_wait_not_a_failure(self) -> None:
        # The model is published and its one Mac is eligible but taken; routing keeps the
        # location as a candidate, so the ordinary queue wait and ceiling apply.
        self.sites.append(_site("a", supply={MODEL: {"eligible": 1, "available": 0}},
                               last_seen_at=time.time()))
        job = self.job()
        self.assertFalse(self.worker._no_eligible_machine(job))
        self.assertEqual(self.store.get(job["id"])["state"], "accepted")

    def test_every_candidate_rejection_is_recorded(self) -> None:
        self.sites.extend([
            _site("offline", reachable=False),
            _site("paused", paused=True),
            _site("draining", drained=True),
            _site("noinstall", supply={MODEL: {"eligible": 0}},
                 models=[MODEL, "another-model"]),
            _site("published"),  # this one is a candidate
            _site("other", models=["different-model"]),  # does not publish the model at all
        ])
        eligibility = fleet.dispatch_eligibility(MODEL, MODALITY)
        reasons = {row["location_id"]: row["reason"] for row in eligibility["rejections"]}
        self.assertEqual(reasons.get("offline"), "offline")
        self.assertEqual(reasons.get("paused"), "paused")
        self.assertEqual(reasons.get("draining"), "draining")
        self.assertIn(reasons.get("noinstall"), {"model_not_installed", "model_not_published"})
        self.assertEqual(reasons.get("other"), "model_not_published")
        self.assertEqual([c["location_id"] for c in eligibility["candidates"]], ["published"])
        self.assertTrue(eligibility["available"])
        for reason in reasons.values():
            self.assertIn(reason, fleet.REJECTION_REASONS)

    def test_offline_location_is_not_a_candidate(self) -> None:
        seven_days = time.time() - 7 * 86400
        self.sites.append(_site("gone", last_seen_at=seven_days,
                                supply={MODEL: {"eligible": 1, "available": 1}}))
        eligibility = fleet.dispatch_eligibility(MODEL, MODALITY)
        self.assertFalse(eligibility["available"])
        rejection = eligibility["rejections"][0]
        self.assertEqual(rejection["reason"], "offline")
        self.assertAlmostEqual(rejection["last_seen_at"], seven_days, delta=1)
        summary = fleet.dispatch_reason_summary(eligibility)
        self.assertIn("offline, last seen 7d", summary)

    def test_live_qualified_hub_is_selected(self) -> None:
        self.sites.extend([
            _site("gone", reachable=False),
            _site("live", supply={MODEL: {"eligible": 1, "available": 1}},
                 last_seen_at=time.time()),
        ])
        eligibility = fleet.dispatch_eligibility(MODEL, MODALITY)
        self.assertTrue(eligibility["available"])
        self.assertEqual([c["location_id"] for c in eligibility["candidates"]], ["live"])
        self.assertEqual([r["location_id"] for r in eligibility["rejections"]], ["gone"])

    def test_a_capability_mismatch_is_recorded(self) -> None:
        self.sites.append(_site("no-edit", supply={MODEL: {"eligible": 1, "available": 1}}))
        eligibility = fleet.dispatch_eligibility(MODEL, MODALITY,
                                                require_capability=fleet.EDIT_CAPABILITY)
        self.assertFalse(eligibility["available"])
        self.assertEqual(eligibility["rejections"][0]["reason"], "location_mismatch")


class CancellationTests(_Base):
    def test_cancelled_job_retry_creates_a_fresh_job(self) -> None:
        self.sites.append(_site("a"))
        job = self.job()
        cancelled = self.store.cancel(job["id"], by="Story Studio",
                                      reason="wrong scene", origin="api")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["cancelled_by"], "Story Studio")
        self.assertEqual(cancelled["cancel_reason"], "wrong scene")
        self.assertEqual(cancelled["cancel_origin"], "api")
        self.assertIsNotNone(cancelled["cancelled_at"])
        # The retry frees the key and mints a fresh row; the cancelled row is untouched.
        self.store.release_idempotency_key(
            job["id"], key=f"job-{self._n}:cancelled:{job['id']}")
        fresh, created = self.store.submit(
            modality=MODALITY, model_id=MODEL, params={},
            idempotency_key=f"job-{self._n}", provider_id=_pid("a"),
            previous_job_id=job["id"],
            previous_cancel={"cancelled_by": cancelled["cancelled_by"],
                             "cancel_reason": cancelled["cancel_reason"],
                             "cancel_origin": cancelled["cancel_origin"],
                             "cancelled_at": cancelled["cancelled_at"]})
        self.assertTrue(created)
        self.assertNotEqual(fresh["id"], job["id"])
        self.assertEqual(fresh["previous_job_id"], job["id"])
        old = self.store.get(job["id"])
        self.assertEqual(old["state"], "cancelled")
        self.assertEqual(self.store.find_by_idempotency_key(f"job-{self._n}")["id"],
                         fresh["id"])

    def test_cancelled_job_retry_reports_the_original_cause(self) -> None:
        self.sites.append(_site("a"))
        job = self.job()
        self.store.cancel(job["id"], by="owner", reason="stopped by hand", origin="console")
        old = self.store.get(job["id"])
        self.assertEqual(admin._previous_cancellation({
            "previous_cancel_json": json.dumps({
                "cancelled_by": old["cancelled_by"],
                "cancel_reason": old["cancel_reason"],
                "cancel_origin": old["cancel_origin"],
                "cancelled_at": old["cancelled_at"]})}),
            {"cancelled_by": "owner", "cancel_reason": "stopped by hand",
             "cancel_origin": "console", "cancelled_at": old["cancelled_at"]})

    def test_upstream_endpoint_reports_cancellation(self) -> None:
        self.sites.append(_site("a"))
        job = self.job()
        self.store.cancel(job["id"], by="Story Studio", reason="gave up", origin="api")
        with patch.object(admin, "store", self.store), \
                patch.object(admin.registry, "definition", lambda pid: _definition(pid)):
            answer = admin.upstream_job_trace(job["id"])
        self.assertEqual(answer["cancellation"]["cancelled_by"], "Story Studio")
        self.assertEqual(answer["cancellation"]["cancel_reason"], "gave up")
        self.assertEqual(answer["cancellation"]["cancel_origin"], "api")
        self.assertEqual(answer["state"], "cancelled")
        self.assertIsNone(answer["selected_machine"])
        self.assertIsNone(answer["dispatch_trace"])

    def test_upstream_endpoint_reports_the_dispatch_refusal(self) -> None:
        self.sites.append(_site("a", reachable=False))
        job = self.job()
        self.worker._no_eligible_machine(job)
        with patch.object(admin, "store", self.store), \
                patch.object(admin.registry, "definition", lambda pid: _definition(pid)):
            answer = admin.upstream_job_trace(job["id"])
        self.assertEqual(answer["error_code"], "DISPATCH_NO_ELIGIBLE_MACHINE")
        self.assertEqual(answer["candidates"][0]["reason"], "offline")
        self.assertIsNone(answer["cancellation"])
        self.assertIsNone(answer["selected_machine"])


class SubmitRetryTests(_Base):
    def test_a_cancelled_key_mints_a_fresh_job_and_keeps_the_cause(self) -> None:
        from gateway import api

        definition = types.SimpleNamespace(id="openrouter", kind="openai_compat",
                                           display_name="OpenRouter", base_url="")
        model = {"model_id": "some/chat-model", "raw": {}}
        with patch.object(api, "store", self.store), \
                patch.object(api.catalog, "resolve", lambda *a, **k: (definition, model)), \
                patch.object(api, "_refuse_when_the_queue_is_full", lambda *a, **k: None), \
                patch.object(api, "_refuse_when_the_disk_is_low", lambda *a, **k: None), \
                patch.object(api, "_refuse_a_request_that_cannot_fit", lambda *a, **k: None):
            first, created = api._submit("chat", "openrouter::some/chat-model",
                                         {"messages": []}, "retry-1")
            self.assertTrue(created)
            self.store.cancel(first["id"], by="Story Studio", reason="wrong tone",
                              origin="api")
            second, created_again = api._submit("chat", "openrouter::some/chat-model",
                                                {"messages": []}, "retry-1")
            self.assertTrue(created_again, "a cancelled key must mint a fresh job")
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(second["previous_job_id"], first["id"])
            self.assertEqual(json.loads(second["previous_cancel_json"])["cancel_reason"],
                             "wrong tone")
            self.assertEqual(self.store.get(first["id"])["state"], "cancelled",
                             "the cancelled row is immutable")
            # A second replay now finds the live job, not a third one.
            third, created_third = api._submit("chat", "openrouter::some/chat-model",
                                               {"messages": []}, "retry-1")
            self.assertFalse(created_third)
            self.assertEqual(third["id"], second["id"])
            self.assertEqual(self.store.find_by_idempotency_key("retry-1")["id"],
                             second["id"])


if __name__ == "__main__":
    unittest.main()
