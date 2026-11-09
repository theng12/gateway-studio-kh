"""The Gateway relays a Mac's RAM admission read/write to its controller, unchanged (Hub 0.6.226)."""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from tests._isolation import sandbox_env
sandbox_env("gateway-memory-admission-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, startup_control

REV = "a" * 64
STATE = {"ok": True, "model": "qwen-image", "revision": REV, "min_free_memory_gb": 12}


class MemoryAdmissionRelayTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.client = TestClient(api.app, client=("127.0.0.1", 41001))
        self.path = "/admin/api/fleet/controllers/site/machines/mac/memory/admission"
        for name, kwargs in (("_target", {"return_value": ("http://controller", "token")}), ("audit", {})):
            p = patch.object(startup_control, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def serve(self, status, body):
        def respond(request):
            self.requests.append(request)
            return httpx.Response(status, json=body)
        return patch.object(startup_control.httpx, "Client",
                            return_value=httpx.Client(transport=httpx.MockTransport(respond)))

    def test_read_sends_model_mode_and_token(self):
        with self.serve(200, STATE):
            response = self.client.get(self.path, params={"model": "qwen-image", "mode": "edit"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], REV)
        sent = self.requests[0]
        self.assertEqual((sent.method, sent.url.path), ("GET", "/api/hub/fleet/nodes/mac/memory/admission"))
        self.assertEqual(dict(sent.url.params), {"model": "qwen-image", "mode": "edit"})
        self.assertEqual(sent.headers["x-hub-token"], "token")

    def test_write_relays_exact_body_once(self):
        body = {"expected_revision": REV, "model": "qwen-image", "min_free_memory_gb": None,
                "min_total_memory_gb": None}
        with self.serve(200, STATE):
            response = self.client.put(self.path, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].method, "PUT")
        self.assertEqual(json.loads(self.requests[0].content), body)

    def test_conflict_passes_through_with_current_revision(self):
        conflict = {"ok": False, "code": "REVISION_CONFLICT", "current_revision": "b" * 64, "admission": STATE}
        with self.serve(409, conflict):
            response = self.client.put(self.path, json={"expected_revision": REV, "model": "qwen-image"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["current_revision"], "b" * 64)
        self.assertEqual(len(self.requests), 1)   # never retried

    def test_pending_relay_stays_202(self):
        with self.serve(202, {"ok": True, "outcome": "pending"}):
            response = self.client.put(self.path, json={"expected_revision": REV, "model": "m"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["outcome"], "pending")

    def test_reference_refusal_reaches_caller(self):
        refusal = {"ok": False, "code": "TIER_UNKNOWN", "field": "reference", "detail": "D2-D4 not wired"}
        with self.serve(400, refusal):
            response = self.client.put(self.path, json={"expected_revision": REV, "model": "m",
                                                        "reference": "x", "reference_count": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "TIER_UNKNOWN")

    def test_old_hub_and_unknown_model_are_told_apart(self):
        with self.serve(404, {"detail": "Not Found"}):
            old = self.client.get(self.path, params={"model": "m"})
        with self.serve(404, {"detail": "unknown model"}):
            unknown = self.client.get(self.path, params={"model": "m"})
        self.assertEqual(old.json()["code"], "HUB_TOO_OLD")
        self.assertEqual(unknown.json()["code"], "UNKNOWN_MODEL")

    def test_hub_228_coded_404_and_node_refused_pass_through(self):
        coded = {"ok": False, "code": "UNKNOWN_MODEL", "detail": "this Mac does not know that model"}
        with self.serve(404, coded):
            answer = self.client.get(self.path, params={"model": "m"})
        self.assertEqual((answer.status_code, answer.json()["code"]), (404, "UNKNOWN_MODEL"))
        with self.serve(502, {"ok": False, "code": "NODE_REFUSED", "detail": "uncoded 404"}):
            answer = self.client.get(self.path, params={"model": "m"})
        self.assertEqual((answer.status_code, answer.json()["code"]), (502, "NODE_REFUSED"))

    def test_bad_machine_refused_before_any_call(self):
        response = self.client.get("/admin/api/fleet/controllers/site/machines/..%2Fx/memory/admission",
                                   params={"model": "m"})
        self.assertIn(response.status_code, (400, 404))
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
