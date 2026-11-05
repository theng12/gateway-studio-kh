"""No-network capture of the actual Studio Hub image submission envelope."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _isolation import sandbox_env
_SANDBOX = sandbox_env("gateway-image-target-envelope-")
from gateway.adapters import studio_hub
from gateway.adapters.base import BackendRequest, BackendError

TARGET = {"location_id": "fixture-site", "machine": "fixture-node"}


class Captured(Exception):
    pass


class EnvelopeTests(unittest.TestCase):
    def capture(self, extra, provider="studio-hub-fixture-site"):
        bodies = []
        class Client:
            def __init__(self, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def post(self, _url, *, headers, json):
                bodies.append(json)
                raise Captured()
        request = BackendRequest(provider_id=provider, model_id="fixture-model", modality="image",
                                 params={"prompt": "offline fixture", "_job_id": "original-job", **extra},
                                 values={"hub_token": "fixture-token"}, base_url="http://fixture.invalid")
        with patch.object(studio_hub.httpx, "Client", Client):
            with self.assertRaises(Captured):
                studio_hub._generate_image_or_voice(request)
        return bodies[0]

    def test_exact_machine_is_the_hub_envelope_routing(self):
        body = self.capture({"measurement_target": TARGET})
        self.assertEqual(body["routing"], "machine:fixture-node")
        self.assertEqual(body["clientRequestId"], "original-job")
        self.assertNotIn("measurement_target", body["items"][0]["params"])

    def test_measurement_pin_is_not_replaced_by_pool_assigned(self):
        body = self.capture({"measurement_target": TARGET, "_pool_id": "fixture-pool"})
        self.assertEqual(body["routing"], "machine:fixture-node")

    def test_private_client_request_id_cannot_change_targeted_job_identity(self):
        body = self.capture({"measurement_target": TARGET, "_client_request_id": "injected-id"})
        self.assertEqual(body["clientRequestId"], "original-job")

    def test_wrong_or_missing_execution_identity_is_refused_before_download(self):
        request = BackendRequest(provider_id="studio-hub-fixture-site", model_id="fixture-model", modality="image",
                                 params={"_job_id": "job", "measurement_target": TARGET}, values={},
                                 base_url="http://fixture.invalid")
        for machine in (None, "other-node"):
            with self.subTest(machine=machine), \
                 patch.object(studio_hub, "_fetch_artifact", side_effect=AssertionError("no download")):
                with self.assertRaises(BackendError) as raised:
                    studio_hub._collect_batch(None, "http://fixture.invalid", {}, request, "batch",
                        {"item": {"terminal_result": {"machine_id": machine}}}, studio_hub.HubProgress())
                self.assertEqual(raised.exception.code, "MEASUREMENT_RESULT_IDENTITY_UNVERIFIED")
                self.assertTrue(raised.exception.upstream_settled)

    def test_known_batch_collection_does_not_require_a_new_submission_id(self):
        request = BackendRequest(provider_id="studio-hub-fixture-site", model_id="fixture-model", modality="image",
                                 params={"measurement_target": TARGET}, values={}, base_url="http://fixture.invalid")
        artifact = SimpleNamespace(status_code=200, headers={"content-type": "image/png"}, content=b"fixture-png")
        with patch.object(studio_hub, "_fetch_artifact", return_value=artifact):
            result = studio_hub._collect_batch(None, "http://fixture.invalid", {}, request, "original-batch",
                {"item": {"terminal_result": {"machine_id": "fixture-node"}}}, studio_hub.HubProgress())
        self.assertEqual(result.payload["hub_machine"], "fixture-node")
        self.assertEqual(result.payload["hub_batch_id"], "original-batch")

    def test_new_submit_without_a_durable_job_id_is_refused_before_http(self):
        request = BackendRequest(provider_id="studio-hub-fixture-site", model_id="fixture-model", modality="image",
                                 params={"measurement_target": TARGET, "_client_request_id": "injected"},
                                 values={}, base_url="http://fixture.invalid")
        with patch.object(studio_hub.httpx, "Client", side_effect=AssertionError("no HTTP")):
            with self.assertRaises(BackendError):
                studio_hub._generate_image_or_voice(request)

    def test_ordinary_pool_behavior_stays_assigned(self):
        body = self.capture({"_pool_id": "fixture-pool"})
        self.assertEqual(body["routing"], "assigned")

    def test_different_registered_site_is_refused_before_http(self):
        with patch.object(studio_hub.httpx, "Client", side_effect=AssertionError("no HTTP")):
            request = BackendRequest(provider_id="studio-hub-other", model_id="fixture-model", modality="image",
                                     params={"prompt": "fixture", "_job_id": "job", "measurement_target": TARGET},
                                     values={"hub_token": "fixture-token"}, base_url="http://fixture.invalid")
            with self.assertRaises(BackendError):
                studio_hub._generate_image_or_voice(request)


if __name__ == "__main__":
    unittest.main()
