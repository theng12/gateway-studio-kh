"""Controller-local pause comes from its own authenticated work policy, never its siblings."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-controller-pause-")

import httpx
from gateway import fleet

POLICY_ROUTE = "/api/hub/fleet/nodes/local/work-policy"


class ControllerPauseTests(unittest.TestCase):
    def snapshot(self, policy, *, status=200, fail=False):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path, request.headers.get("X-Hub-Token")))
            if request.url.path == "/api/hub/fleet/overview":
                return httpx.Response(200, json={"machines": [
                    {"machine": "local", "reachable": True},
                    {"machine": "member", "reachable": True},
                    {"machine": "unlisted", "reachable": True},
                ]})
            if request.url.path == "/api/hub/fleet/nodes":
                return httpx.Response(200, json={"nodes": [{"id": "member", "paused": True}]})
            if request.url.path == POLICY_ROUTE:
                if fail:
                    raise httpx.ReadTimeout("unavailable", request=request)
                return httpx.Response(status, json=policy)
            return httpx.Response(404, json={})

        real = httpx.Client
        with patch.object(fleet.httpx, "Client", side_effect=lambda **kw: real(
                transport=httpx.MockTransport(handler), **kw)):
            location = fleet._probe("http://controller.test", "scoped-fleet-token")
        location.update(id="site", label="Site", local=False)
        rows = {r["machine"]: r for r in fleet.machines_flat([location])}
        self.assertTrue(location["reachable"])
        self.assertTrue(rows["member"]["node_paused"], "local policy cannot unpause a joined member")
        self.assertIsNone(rows["unlisted"]["node_paused"], "local policy cannot fill an unknown member")
        self.assertTrue(all(method == "GET" and token == "scoped-fleet-token"
                            for method, _, token in calls))
        self.assertEqual(1, sum(path == POLICY_ROUTE for _, path, _ in calls))
        return rows["local"]["node_paused"]

    def test_enabled_controller_is_unpaused_without_a_joined_node_row(self):
        self.assertIs(self.snapshot({"work_policy": {"machine_enabled": True}}), False)

    def test_disabled_controller_stays_paused(self):
        self.assertIs(self.snapshot({"work_policy": {"machine_enabled": False}}), True)

    def test_absent_or_malformed_evidence_stays_unknown(self):
        bodies = [{}, [], None, {"work_policy": []}, {"work_policy": {}}]
        bodies.extend({"work_policy": {"machine_enabled": value}}
                      for value in (None, 0, 1, "true", "false", [], {}))
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(self.snapshot(body))

    def test_failed_policy_read_does_not_hide_the_location_or_invent_unpaused(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                self.assertIsNone(self.snapshot({"detail": "not available"}, status=status))
        self.assertIsNone(self.snapshot({}, fail=True))


if __name__ == "__main__":
    unittest.main()
