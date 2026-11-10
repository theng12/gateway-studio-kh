"""Authenticated audio discovery: mock HTTP only, no provider or native app.

Synthetic broker offers test assignment fencing, not successful audio admission.
"""
import copy
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-extension-audio-inventory-")
from gateway import extension_workers as ew


class AudioInventoryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {ew.SWITCH: "1",
            "GATEWAY_EXTENSION_WORKER_ONLINE_SECONDS": "90",
            "GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "1048576"})
        env.start()
        self.addCleanup(env.stop)
        ew.reset_for_tests()
        self.addCleanup(ew.reset_for_tests)
        from tests.extension_account_fixture import fixture_offers
        fixture_offers(self)
        app = FastAPI()
        app.include_router(ew.router)
        self.client = TestClient(app)
        self.token = ew.mint("audio-a", "audio")
        self.payload = {"provider": "fish", "model": "s2.1-pro", "voice": "voice-a", "text": "x"}

    def auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def inventory(self, **changes):
        data = {"source": "live", "complete": True, "fetchedAt": int(time.time() * 1000),
                "models": [{"id": "s2.1-pro"}], "voices": [{"id": "voice-a"}]}
        data.update(changes)
        return {"schema": 1, "providers": {"fish": data}}

    def claim(self, inventory=None, token=None, worker="audio-a", kind="audio", **extra):
        body = {"workerId": worker, "kind": kind, **extra}
        if inventory is not None:
            body["audioInventory"] = inventory
        from tests.extension_account_fixture import claim_with_observed_account
        return claim_with_observed_account(self.client, token or self.token, worker, kind,
                                           {key: value for key, value in body.items() if key not in ("workerId", "kind")})

    def test_health_advertises_live_finite_limits(self):
        with patch.dict(os.environ, {"GATEWAY_EXTENSION_WORKER_ONLINE_SECONDS": "12",
                                     "GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "2048"}):
            body = self.client.get("/api/workers/health", headers=self.auth()).json()
        self.assertEqual(body["onlineSeconds"], 12)
        self.assertEqual(body["audioInventoryMaxAgeSeconds"], 12)
        self.assertEqual(body["audioInventoryMaxBytes"], 2048)
        self.assertNotIn(self.token, json.dumps(body))
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value), patch.dict(os.environ,
                    {"GATEWAY_EXTENSION_WORKER_ONLINE_SECONDS": value}):
                self.assertEqual(ew.online_seconds(), 90)

    def test_switch_off_hides_discovery(self):
        with patch.dict(os.environ, {ew.SWITCH: "0"}):
            self.assertEqual(self.claim(self.inventory()).status_code, 404)
        self.assertFalse(ew.broker.audio_member(self.payload))

    def test_inventory_replaces_and_absent_snapshot_clears(self):
        first = self.inventory()
        first["providers"]["elevenlabs_web"] = copy.deepcopy(first["providers"]["fish"])
        self.assertEqual(self.claim(first).status_code, 204)
        self.assertTrue(ew.broker.audio_member(self.payload))
        self.claim(self.inventory())
        self.assertEqual(set(ew.broker.inventory_status("audio-a")), {"fish"})
        self.claim()
        self.assertEqual(ew.broker.inventory_status("audio-a"), {})
        self.assertFalse(ew.broker.audio_member(self.payload))

    def test_invalid_inventory_is_rejected_before_assignment_and_clears_authority(self):
        bad = [self.inventory(source="fallback"), self.inventory(complete=False),
               self.inventory(fetchedAt=True), self.inventory(fetchedAt=2**53),
               self.inventory(fetchedAt=int((time.time() + 60) * 1000)),
               self.inventory(fetchedAt=int((time.time() - 91) * 1000)),
               self.inventory(models=[]), self.inventory(voices=[{"id": " "}]),
               {"schema": True, "providers": {}}, {"schema": 1, "providers": {"unknown": {}}}]
        for value in bad:
            with self.subTest(value=value):
                self.claim(self.inventory())
                with patch.object(ew.broker, "claim") as assign:
                    response = self.claim(value)
                    assign.assert_not_called()
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "WORKER_INVENTORY_INVALID")
                self.assertFalse(ew.broker.audio_member(self.payload))

    def test_entire_chunked_claim_is_bounded_before_json_or_assignment(self):
        self.claim(self.inventory())
        with patch.dict(os.environ, {"GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "64"}), \
                patch.object(ew.broker, "claim") as assign:
            response = self.client.post("/api/workers/claim", headers=self.auth(),
                                        content=iter([b" " * 40, b" " * 40]))
            assign.assert_not_called()
        self.assertEqual(response.status_code, 413)
        self.assertIn("64 byte", response.json()["detail"])
        self.assertFalse(ew.broker.audio_member(self.payload))

    def test_malformed_json_is_definite_preclaim_rejection(self):
        self.claim(self.inventory())
        for raw in (b"[1]", b"{broken", b"\xff"):
            with self.subTest(raw=raw), patch.object(ew.broker, "claim") as assign:
                response = self.client.post("/api/workers/claim", headers=self.auth(), content=raw)
                assign.assert_not_called()
            self.assertEqual(response.status_code, 422)
        self.assertFalse(ew.broker.audio_member(self.payload))

    def test_wrong_token_or_identity_never_sets_inventory(self):
        self.assertEqual(self.claim(self.inventory(), token="ekw_invalid").status_code, 401)
        self.assertEqual(self.claim(self.inventory(), worker="another").status_code, 403)
        self.assertFalse(ew.broker.audio_member(self.payload))
        video = ew.mint("video-a", "video")
        response = self.claim(self.inventory(), token=video, worker="video-a", kind="video")
        self.assertEqual(response.status_code, 422)

    def test_actual_claiming_worker_must_match_both_exact_ids(self):
        self.claim(self.inventory())
        job = ew.broker.offer("audio", dict(self.payload))
        other = ew.mint("audio-b", "audio")
        self.assertEqual(self.claim(self.inventory(voices=[{"id": "voice-b"}]), token=other,
                                    worker="audio-b").status_code, 204)
        self.assertEqual(self.claim(self.inventory(models=[{"id": "different"}]), token=other,
                                    worker="audio-b").status_code, 204)
        self.assertEqual(job.state, "pending")
        response = self.claim(self.inventory())
        self.assertEqual(response.json()["job"]["jobId"], job.job_id)
        self.assertEqual(job.owner, "audio-a")

    def test_inventory_expiry_does_not_abandon_claimed_or_held_work(self):
        snapshot = self.inventory()
        job = ew.broker.offer("audio", dict(self.payload))
        self.assertEqual(self.claim(snapshot).status_code, 200)
        repeated = self.claim()
        self.assertEqual(repeated.status_code, 409)
        self.assertNotIn("job", repeated.json())
        self.assertEqual(job.state, "claimed")
        self.assertFalse(ew.broker.audio_member(self.payload))
        ew.broker.hold(job)
        self.assertEqual(self.claim().status_code, 204)
        self.assertEqual(ew.broker.owned(ew.worker_for(self.token), job.job_id), job)
        self.assertEqual(ew.broker.answer(ew.worker_for(self.token), job.job_id,
                                         state="succeeded", content=b"audio"), "late")

    def test_health_and_progress_do_not_renew_observation(self):
        snapshot = self.inventory()
        self.claim(snapshot)
        observed = ew.broker.inventory_status("audio-a")["fish"]["observed_at"]
        with patch.object(ew.time, "time", return_value=observed + 91):
            self.client.get("/api/workers/health", headers=self.auth())
            self.client.post("/api/workers/jobs/missing/progress", headers=self.auth(),
                             json={"phase": "x"})
            self.assertFalse(ew.broker.audio_member(self.payload))
            self.assertFalse(ew.broker.inventory_status("audio-a")["fish"]["fresh"])
        self.assertEqual(ew.broker.inventory_status("audio-a")["fish"]["observed_at"], observed)

    def test_remint_revoke_and_reset_invalidate_discovery(self):
        self.claim(self.inventory())
        previous_worker = ew.worker_for(self.token)
        self.token = ew.mint("audio-a", "audio")
        self.assertFalse(ew.broker.audio_member(self.payload))
        with self.assertRaises(PermissionError):
            ew.broker.claim(previous_worker, ew.audio_inventory(self.inventory(), time.time()))
        self.client.get("/api/workers/health", headers=self.auth())
        self.assertFalse(ew.broker.audio_member(self.payload))
        self.claim(self.inventory())
        ew.revoke("audio-a")
        self.assertFalse(ew.broker.audio_member(self.payload))
        self.assertFalse(ew.broker.online("audio"))
        self.token = ew.mint("audio-a", "audio")
        self.claim(self.inventory())
        ew.reset_for_tests()
        self.assertFalse(ew.broker.audio_member(self.payload))

    def test_reminting_another_kind_cannot_take_or_answer_the_existing_kind_job(self):
        job = ew.broker.offer("audio", dict(self.payload))
        self.assertEqual(self.claim(self.inventory()).status_code, 200)
        video = ew.mint("audio-a", "video")
        other = ew.broker.offer("video", {"prompt": "other"})
        response = self.client.post("/api/workers/claim", headers=self.auth(video),
                                    json={"workerId": "audio-a", "kind": "video"})
        self.assertEqual(response.status_code, 204)
        worker = ew.worker_for(video)
        self.assertIsNone(ew.broker.owned(worker, job.job_id))
        self.assertFalse(ew.broker.touch(worker, job.job_id, "wrong", ""))
        self.assertEqual(ew.broker.answer(worker, job.job_id, state="failed"), "missing")
        self.assertEqual(job.state, "claimed")
        self.assertEqual(other.state, "pending")

    def test_status_reports_the_new_token_kind_after_remint(self):
        from gateway import api
        ew.mint("audio-a", "video")
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        row = next(row for row in local.get("/admin/api/extension-workers").json()["workers"]
                   if row["worker_id"] == "audio-a")
        self.assertEqual(row["kind"], "video")

    def test_durable_late_answers_are_also_bound_to_token_kind(self):
        from gateway import runtime
        upstream = ew.upstream_id("audio-a", "job_durable")
        row = {"id": "durable", "upstream_job_id": upstream, "modality": "tts"}
        audio = ew.worker_for(self.token)
        with patch.object(runtime.store, "jobs_for_upstream_batches", return_value=[row]):
            self.assertEqual(ew._held_row(audio, "job_durable"), row)
            video = ew.worker_for(ew.mint("audio-a", "video"))
            self.assertIsNone(ew._held_row(video, "job_durable"))

    def test_outer_body_middleware_honors_advertised_limit_and_clears_rejected_inventory(self):
        from gateway import api
        app = FastAPI()
        app.include_router(ew.router)
        client = TestClient(api.BoundedUploadBody(app))
        envelope = {"workerId": "audio-a", "kind": "audio", "audioInventory": self.inventory()}
        raw = json.dumps(envelope).encode()
        with patch.object(api, "_BODY_CEILING_DEFAULT", 16), patch.dict(os.environ,
                {"GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "1024"}):
            self.assertGreater(len(raw), api._BODY_CEILING_DEFAULT)
            self.assertEqual(client.post("/api/workers/claim", headers=self.auth(), content=raw).status_code, 204)
            self.assertTrue(ew.broker.audio_member(self.payload))
            with patch.object(ew.broker, "claim") as assign:
                response = client.post("/api/workers/claim", headers=self.auth(),
                                       content=iter([b" " * 600, b" " * 600]))
                assign.assert_not_called()
            self.assertEqual(response.status_code, 413)
            self.assertIn("1024 bytes", response.json()["detail"])
            self.assertFalse(ew.broker.audio_member(self.payload))

    def test_membership_is_not_compatibility_and_caller_cannot_authorize_it(self):
        from gateway.adapters.base import BackendError, BackendRequest
        from gateway.adapters.chrome_extension import ChromeExtensionBackend
        self.claim(self.inventory())
        request = BackendRequest(provider_id="story-studio-audio", model_id="s2.1-pro", modality="tts",
                                 params={"provider": "fish", "voice_id": "voice-a", "text": "hello",
                                         "_worker_inventory": {"compatible": True}}, values={}, base_url="")
        with patch.object(ew.broker, "offer") as offer, self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(request)
        self.assertEqual(caught.exception.code, "WORKER_INVENTORY_UNAVAILABLE")
        self.assertIn("compatible pairs", str(caught.exception))
        offer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
