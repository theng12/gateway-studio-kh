"""A Mac's preset and its models, from the Gateway's Fleet page (owner decision 2026-10-28).

Three operations against one Mac, all through its controller on the fleet credential:

* **read** that Mac's install screen (`GET .../machines/{id}/model-presets`) - relayed unchanged, so
  the numbers are the Hub's;
* **set** its preset (`POST .../model-preset`) - one of the four, with `requested_by` required and
  recorded;
* **remove** one model (`DELETE .../model-packages/{package}`) - the Hub moves it into that
  library's trash, never deletes.

What these pin:

* the request goes to the controller on the fleet credential; reads are not audited, writes are;
* a controller without the route (404, or the Fleet allowlist refusing it) is `HUB_TOO_OLD` naming
  the Hub version, never a bare failure;
* an unknown preset id, and a write with no approver, are refused **before** anything is sent;
* an offline Mac is `NODE_UNREACHABLE` in its own words - reported, not failed - so one sleeping
  Mac never blanks the Fleet page.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-model-presets-")

import httpx  # noqa: E402

from gateway import config, fleet, startup_control  # noqa: E402


def _routes(routes):
    """Every route, including a mounted router's own table (FastAPI 0.141 keeps it in one wrapper)."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _routes(route.original_router.routes)
        else:
            yield route


MACHINE = "node-1-hub"
PACKAGE = "qwen36-35b-4bit"


class RelayCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gateway-model-presets-case-")
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(tmp.name)),
                        patch.object(startup_control, "_target",
                                     lambda c: ("http://100.64.0.2:47873", "fleet-token")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, answer, fn, *args, **kwargs):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return answer
        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            return fn("ctl1", MACHINE, *args, client=client, **kwargs), seen
        except startup_control.StartupError as error:
            return error, seen

    def events(self) -> list[str]:
        return [row["event"] for row in startup_control.recent_audit(50)]


class ReadTests(RelayCase):
    def test_the_read_is_a_get_through_the_fleet_path_and_is_not_audited(self) -> None:
        payload = {"memoryGb": 16, "presets": [{"presetId": "creator-16", "installedCount": 9,
                                                "packageCount": 10, "complete": False}],
                   "groups": [], "problems": []}
        answer, seen = self.call(httpx.Response(200, json=payload), startup_control.model_presets)
        self.assertEqual(answer["presets"][0]["installedCount"], 9)
        self.assertEqual((seen[0].method, seen[0].url.path),
                         ("GET", f"/api/hub/fleet/nodes/{MACHINE}/model-presets"))
        self.assertEqual(seen[0].headers["x-hub-token"], "fleet-token")
        self.assertNotIn("model_presets_requested", self.events(),
                         "a read is not an action and must not fill the audit log")

    def test_a_controller_without_the_route_names_the_hub_version(self) -> None:
        answer, _ = self.call(httpx.Response(404, json={"detail": "not found"}),
                              startup_control.model_presets)
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "HUB_TOO_OLD")
        self.assertIn("0.6.205", answer.body["detail"])

    def test_an_offline_mac_is_reported_in_its_own_words(self) -> None:
        # The controller answers 409 NODE_UNREACHABLE for a Mac it cannot dial; the Gateway keeps
        # that as the Mac's state rather than turning the whole read into a failure.
        body = {"ok": False, "code": "NODE_UNREACHABLE",
                "detail": "this controller could not reach that Mac"}
        answer, _ = self.call(httpx.Response(409, json=body), startup_control.model_presets)
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "NODE_UNREACHABLE")
        self.assertIn("could not reach", answer.body["detail"])


class SetPresetTests(RelayCase):
    def test_a_preset_is_set_with_who_asked_and_is_audited(self) -> None:
        answer, seen = self.call(
            httpx.Response(200, json={"ok": True, "preset_id": "creator-16"}),
            startup_control.set_model_preset, "creator-16", requested_by="vengtheng")
        self.assertEqual(answer["preset_id"], "creator-16")
        self.assertEqual((seen[0].method, seen[0].url.path),
                         ("POST", f"/api/hub/fleet/nodes/{MACHINE}/model-preset"))
        self.assertEqual(json.loads(seen[0].content),
                         {"preset_id": "creator-16", "requested_by": "vengtheng"})
        self.assertIn("model_preset_requested", self.events())
        self.assertIn("model_preset_result", self.events())

    def test_an_unrecorded_approver_is_refused_before_anything_is_sent(self) -> None:
        for who in ("", "   "):
            with self.subTest(requested_by=who):
                answer, seen = self.call(httpx.Response(200, json={"ok": True}),
                                         startup_control.set_model_preset, "creator-16",
                                         requested_by=who)
                self.assertIsInstance(answer, startup_control.StartupError)
                self.assertEqual(answer.body["code"], "APPROVER_REQUIRED")
                self.assertEqual(seen, [], "nothing was sent for a request with no approver")

    def test_a_missing_preset_id_is_refused_before_anything_is_sent(self) -> None:
        answer, seen = self.call(httpx.Response(200, json={"ok": True}),
                                 startup_control.set_model_preset, "",
                                 requested_by="vengtheng")
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "BAD_PRESET")
        self.assertEqual(seen, [])

    def test_an_unknown_preset_is_the_hubs_refusal_passed_through(self) -> None:
        detail = "no preset named 'starter-8'; the presets are ['essentials-8', ...]"
        answer, _ = self.call(httpx.Response(409, json={"ok": False, "code": "UNKNOWN_PRESET",
                                                       "detail": detail}),
                              startup_control.set_model_preset, "starter-8",
                              requested_by="vengtheng")
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "UNKNOWN_PRESET")
        self.assertIn("starter-8", answer.body["detail"])

    def test_a_controller_without_the_preset_route_names_the_hub_version(self) -> None:
        answer, _ = self.call(httpx.Response(404, json={"detail": "not found"}),
                              startup_control.set_model_preset, "creator-16",
                              requested_by="vengtheng")
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "HUB_TOO_OLD")
        self.assertIn("0.6.205", answer.body["detail"])


class RemoveTests(RelayCase):
    def test_a_removal_is_a_delete_through_the_fleet_path_and_is_audited(self) -> None:
        answer, seen = self.call(
            httpx.Response(200, json={"ok": True, "package_id": PACKAGE, "bytes": 20_000_000_000}),
            startup_control.remove_model, PACKAGE)
        self.assertEqual(answer["package_id"], PACKAGE)
        self.assertEqual((seen[0].method, seen[0].url.path),
                         ("DELETE", f"/api/hub/fleet/nodes/{MACHINE}/model-packages/{PACKAGE}"))
        self.assertIn("model_remove_requested", self.events())
        self.assertIn("model_remove_result", self.events())

    def test_a_model_in_use_is_the_hubs_sentence_passed_through(self) -> None:
        detail = ("qwen36-35b-4bit is in use, so nothing was moved: a chat@local job is running on "
                  "mlx-community/Qwen3.6-35B-A3B-4bit. Stop the job first, then try again.")
        answer, _ = self.call(httpx.Response(409, json={"ok": False, "code": "IN_USE",
                                                       "detail": detail}),
                              startup_control.remove_model, PACKAGE)
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "IN_USE")
        self.assertIn("nothing was moved", answer.body["detail"])

    def test_an_offline_mac_is_reported_not_failed(self) -> None:
        answer, _ = self.call(httpx.Response(409, json={"ok": False, "code": "NODE_UNREACHABLE",
                                                       "detail": "that Mac is asleep"}),
                              startup_control.remove_model, PACKAGE)
        self.assertIsInstance(answer, startup_control.StartupError)
        self.assertEqual(answer.body["code"], "NODE_UNREACHABLE")


class AdminRouteTests(unittest.TestCase):
    """The routes the app calls: their dependencies and their body rules, without a live Hub."""

    def test_the_write_routes_are_owner_only_and_the_read_is_admin(self) -> None:
        from gateway import admin as admin_module
        # These routes carry their own `/admin` prefix, so the key is the full path. Selected by
        # suffix rather than rebuilt from a prefix, so a future mount change fails loudly here
        # instead of quietly matching nothing.
        routes = {route.path: route for route in _routes(admin_module.router.routes)}
        prefix = "/admin/api/fleet/controllers/{controller_id}/machines/{machine_id}"
        read = routes[f"{prefix}/model-presets"]
        set_preset = routes[f"{prefix}/model-preset"]
        remove = routes[f"{prefix}/model-packages/{{package_id}}"]
        # A read may be served to any admin caller; the two writes may not, because they change
        # what a Mac installs and can run.
        names = {
            "read": {getattr(dep.dependency, "__name__", "") for dep in read.dependencies},
            "set": {getattr(dep.dependency, "__name__", "") for dep in set_preset.dependencies},
            "remove": {getattr(dep.dependency, "__name__", "") for dep in remove.dependencies},
        }
        self.assertIn("require_admin", names["read"])
        self.assertIn("require_owner", names["set"])
        self.assertIn("require_owner", names["remove"])

    def test_an_extra_field_is_refused_rather_than_ignored(self) -> None:
        from gateway import admin as admin_module
        superuser = {"preset_id": "creator-16", "requested_by": "vengtheng", "packages": ["x"]}
        answer = admin_module.fleet_machine_set_model_preset("ctl1", MACHINE, superuser)
        self.assertEqual(answer.status_code, 400)
        self.assertIn("packages", answer.body.decode())


if __name__ == "__main__":
    unittest.main()
