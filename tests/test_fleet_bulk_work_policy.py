"""Bulk work orders use the existing per-Mac relay and preserve each outcome."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-bulk-work-")

from gateway import admin, startup_control


class BulkWorkPolicyTest(unittest.TestCase):
    def setUp(self):
        self.machines = [{"controller_id": "site", "machine_id": name} for name in ("one", "two", "three", "four")]
        self.body = {"machines": self.machines, "priority_order": ["voice", "image"]}

    def test_mixed_results_continue_after_refusals_and_exceptions(self):
        refusal = startup_control.StartupError(409, {"code": "BAD_KIND", "detail": "image is not published"})
        with patch.object(startup_control, "set_node_work_policy", side_effect=[
            {"priority_order": ["voice", "image"]}, {"queued": True}, refusal, RuntimeError("unavailable")
        ]) as relay:
            result = admin.fleet_set_work_policy(self.body)["results"]
        self.assertEqual([r["status"] for r in result], ["applied", "queued", "refused", "refused"])
        self.assertEqual(result[2]["detail"], "image is not published")
        self.assertEqual(relay.call_count, 4)

    def test_no_confirmation_is_not_applied(self):
        with patch.object(startup_control, "set_node_work_policy", return_value={"ok": True}):
            result = admin.fleet_set_work_policy({**self.body, "machines": self.machines[:1]})
        self.assertEqual(result["results"][0]["status"], "refused")

    def test_bad_bodies_never_relay(self):
        invalid = [{**self.body, "machines": []}, {**self.body, "priority_order": ["voice", "voice"]},
                   {**self.body, "priority_order": "voice"}, {**self.body, "machines": [{}]},
                   {**self.body, "machines": [self.machines[0], self.machines[0]]}, {**self.body, "extra": True}]
        with patch.object(startup_control, "set_node_work_policy") as relay:
            for body in invalid:
                with self.subTest(body=body):
                    response = admin.fleet_set_work_policy(body)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(json.loads(response.body)["code"], "BAD_WORK_POLICY_REQUEST")
            relay.assert_not_called()

    def test_route_requires_admin(self):
        route = next(r for r in admin.router.routes if getattr(r, "path", "") == "/admin/api/fleet/work-policy")
        self.assertIn(admin.require_admin, [d.call for d in route.dependant.dependencies])

    def test_queued_envelope_is_not_unwrapped_as_confirmed(self):
        with patch.object(startup_control, "_node_post", return_value={"queued": True, "work_policy": {"priority_order": ["voice"]}}):
            self.assertTrue(startup_control.set_node_work_policy("site", "one", ["voice"])["queued"])
