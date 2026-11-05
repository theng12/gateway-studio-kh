"""Exact-node image measurements use the owner door and keep their original lineage."""
from __future__ import annotations

import copy
import json
import sys
import struct
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _isolation import sandbox_env
_SANDBOX = sandbox_env("gateway-image-target-")

from fastapi.testclient import TestClient
from gateway import api, image_assets, image_target


def png(width):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, width) + b"\x08\x02\x00\x00\x00pixels")

TARGET = {"location_id": "fixture-site", "machine": "fixture-node"}
MODEL = "studio-hub::fixture-image"


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self.addCleanup(image_assets._IMAGES.clear)
        self.key = uuid.uuid4().hex
        self.body = {"model_id": MODEL, "prompt": "offline fixture", "measurement_target": dict(TARGET)}
        self.resolved = (SimpleNamespace(id="studio-hub-fixture-site", kind="studio_hub"),
                         {"model_id": "fixture-image", "raw": {"capabilities": ["txt2img", "edit"]}})
        for target, attr, value in ((api, "gateway_api_key", "fixture-owner-key"),
                                     (image_target, "unavailable", None)):
            p = patch.object(target, attr, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(api.catalog, "resolve", side_effect=lambda *a, **k: self.resolved)
        self.resolve = p.start()
        self.addCleanup(p.stop)
        self.headers = {"Authorization": "Bearer fixture-owner-key", "Idempotency-Key": self.key}

    def submit(self, body=None, headers=None):
        return self.client.post("/v1/images/generations", headers=headers or self.headers,
                                json=self.body if body is None else body)

    def test_exact_location_and_target_are_durable_and_background(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.text)
        job = api.store.get(response.json()["id"])
        self.assertEqual(json.loads(job["params"])["measurement_target"], TARGET)
        self.assertEqual(job["provider_id"], "studio-hub-fixture-site")
        self.assertEqual(job["priority"], "background")
        self.assertEqual(self.resolve.call_args.kwargs["only_location"], "fixture-site")

    def test_identical_replay_returns_original_without_fresh_admission(self):
        first = self.submit()
        with patch.object(image_target, "unavailable", side_effect=AssertionError("replay must not admit")):
            replay = self.submit()
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(self.resolve.call_count, 1)

    def test_changing_or_dropping_target_conflicts_before_selection(self):
        self.assertEqual(self.submit().status_code, 202)
        for target in ({**TARGET, "machine": "other-node"}, {**TARGET, "location_id": "other-site"}, None):
            body = copy.deepcopy(self.body)
            if target is None:
                del body["measurement_target"]
            else:
                body["measurement_target"] = target
            response = self.submit(body)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(self.resolve.call_count, 1)

    def test_target_cannot_be_added_to_an_existing_ordinary_job(self):
        body = {k: v for k, v in self.body.items() if k != "measurement_target"}
        self.assertEqual(self.submit(body).status_code, 202)
        self.assertEqual(self.submit().status_code, 409)

    def test_reference_replay_keeps_target_and_expired_reference_bytes(self):
        uploaded = self.client.post("/v1/images/source-images", headers=self.headers,
                                    files={"upload": ("fixture.png", png(512), "image/png")})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        self.body["reference_asset_ids"] = [uploaded.json()["id"]]
        first = self.submit()
        self.assertEqual(first.status_code, 202, first.text)
        image_assets._IMAGES.clear()
        self.assertEqual(self.submit().status_code, 200)
        self.body["measurement_target"]["machine"] = "other-node"
        self.assertEqual(self.submit().status_code, 409)

    def test_unknown_busy_or_paused_target_is_not_queued(self):
        with patch.object(image_target, "unavailable", return_value="The measurement Mac is not idle."):
            response = self.submit()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "MEASUREMENT_TARGET_UNAVAILABLE")
        self.assertIsNone(api.store.find_by_idempotency_key(self.key))
        self.resolve.assert_not_called()

    def test_image_pool_cannot_replace_the_exact_measurement_model(self):
        with patch("gateway.pools.image_pool_name", return_value="fixture-pool"):
            response = self.submit()
        self.assertEqual(response.status_code, 422, response.text)
        self.resolve.assert_not_called()

    def test_different_site_or_cloud_resolution_never_falls_back(self):
        for definition in (SimpleNamespace(id="studio-hub-other-site", kind="studio_hub"),
                           SimpleNamespace(id="fixture-cloud", kind="openai_compat")):
            self.resolved = (definition, self.resolved[1])
            response = self.submit()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIsNone(api.store.find_by_idempotency_key(self.key))

    def test_closed_target_and_auth_are_checked_before_selection(self):
        for target in (None, {}, {**TARGET, "routing": "pool"}, {**TARGET, "machine": "machine:other"},
                       {**TARGET, "location_id": "../site"}, {**TARGET, "machine": ""}):
            body = {**self.body, "measurement_target": target}
            response = self.submit(body)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.submit(headers={"Idempotency-Key": self.key}).status_code, 401)
        self.resolve.assert_not_called()

    def test_guest_submit_helper_cannot_target_a_mac(self):
        with self.assertRaises(api.GatewayError) as raised:
            api._submit("image", MODEL, dict(self.body), self.key,
                        requested_by={"role": "guest", "id": "fixture-guest", "label": "fixture"})
        self.assertEqual(raised.exception.code, "MEASUREMENT_OWNER_REQUIRED")
        self.resolve.assert_not_called()

    def test_nonimage_submit_cannot_carry_the_image_target(self):
        with self.assertRaises(api.GatewayError) as raised:
            api._submit("tts", MODEL, dict(self.body), self.key)
        self.assertEqual(raised.exception.code, "INVALID_MEASUREMENT_TARGET")
        self.resolve.assert_not_called()

    def test_store_race_does_not_replay_a_different_target(self):
        first = self.submit()
        self.assertEqual(first.status_code, 202)
        original = api.store.get(first.json()["id"])
        other_key = uuid.uuid4().hex
        with patch.object(api.store, "find_by_idempotency_key", return_value=None), \
             patch.object(api.store, "submit", return_value=(original, False)):
            body = copy.deepcopy(self.body)
            body["measurement_target"]["machine"] = "other-node"
            response = self.submit(body, headers={**self.headers, "Idempotency-Key": other_key})
        self.assertEqual(response.status_code, 409, response.text)


class FreshInventoryTests(unittest.TestCase):
    def setUp(self):
        import time
        from gateway import fleet
        now = time.time()
        self.snapshot = {"generated_at": now,
                         "locations": [{"id": "fixture-site", "enabled": True, "reachable": True,
                                        "role": "controller", "activity": {"observed_at": now}}],
                         "machines": [{**TARGET, "reachable": True, "node_paused": False,
                                       "state": "long_idle", "current": None}]}
        for attr, value in (("snapshot", self.snapshot), ("owner_paused_at", None)):
            p = patch.object(fleet, attr, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_exact_idle_target_is_admitted_without_forced_network_fanout(self):
        from gateway import fleet
        self.assertIsNone(image_target.unavailable(TARGET))
        fleet.snapshot.assert_called_once_with(allow_stale=True)

    def test_stale_unknown_busy_paused_and_offline_facts_fail_closed(self):
        original = copy.deepcopy(self.snapshot)
        mutations = [
            lambda s: s.update(generated_at=0),
            lambda s: s.update(generated_at=float("nan")),
            lambda s: s["locations"][0]["activity"].update(observed_at=None),
            lambda s: s["machines"][0].update(node_paused=None),
            lambda s: s["machines"][0].update(node_paused=True),
            lambda s: s["machines"][0].update(reachable=False),
            lambda s: s["machines"][0].update(state="working"),
            lambda s: s["machines"][0].update(current={"id": "normal-work"}),
            lambda s: s["machines"][0].update(machine="other-node"),
            lambda s: s["machines"].append(copy.deepcopy(s["machines"][0])),
            lambda s: s["locations"][0].update(paused_at=1),
            lambda s: s["locations"][0].update(drained=True),
            lambda s: s["locations"][0].update(enabled=False),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.snapshot.clear()
                self.snapshot.update(copy.deepcopy(original))
                mutate(self.snapshot)
                self.assertIsNotNone(image_target.unavailable(TARGET))

    def test_targeted_jobs_cannot_late_bind_or_choose_a_next_site(self):
        from gateway import worker
        row = {"id": "fixture-job", "provider_id": "studio-hub-fixture-site", "modality": "image",
               "params": json.dumps({"measurement_target": TARGET})}
        store = SimpleNamespace(get=lambda _id: row)
        instance = worker.Worker(store)
        with patch.object(worker.fleet, "route", side_effect=AssertionError("no routing")):
            self.assertFalse(instance._rebind(row))
            self.assertIsNone(worker._next_machine(store, row["id"], "fixture-model", "image",
                                                   row["provider_id"], code="LOCATION_NOT_READY", detail="fixture"))

    def test_busy_target_is_refused_before_worker_capacity_booking(self):
        from gateway import worker
        row = {"id": "fixture-job", "provider_id": "studio-hub-fixture-site", "modality": "image",
               "params": json.dumps({"measurement_target": TARGET})}
        instance = worker.Worker(SimpleNamespace())
        with patch.object(worker, "_takes_no_new_work", return_value=False), \
             patch.object(image_target, "unavailable", return_value="busy"), \
             patch.object(instance, "_admission_reads", side_effect=AssertionError("no booking")):
            self.assertFalse(instance._admit(row))

    def test_malformed_stored_target_never_becomes_an_ordinary_replay(self):
        for stored in ('{"measurement_target":"broken"}', '{broken', '[]',
                       '{"measurement_target":{"location_id":"fixture-site"}}'):
            with self.subTest(stored=stored):
                self.assertFalse(image_target.same_request({'params': stored}, {}))
                self.assertFalse(image_target.same_request({'params': stored}, {'measurement_target': TARGET}))

    def test_location_pin_survives_a_paused_or_missing_location(self):
        from gateway import fleet
        with patch.object(fleet, "_location_takes_customer_jobs", return_value=False):
            self.assertEqual(fleet.pinned_location({"measurement_target": TARGET}), "fixture-site")


if __name__ == "__main__":
    unittest.main()
