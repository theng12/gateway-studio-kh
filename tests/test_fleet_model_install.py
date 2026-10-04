"""Install a model on a Mac from the Fleet page (owner decision 2026-10-04).

The gateway asks the Mac's controller (`POST .../fleet/nodes/{id}/model-packages/{package}/download`, Studio
Hub 0.6.143) and follows it (`GET .../fleet/nodes/{id}/model-downloads/{run}`). What these pin:

* the request goes to the controller on the fleet credential, audited; the status read is not audited;
* a controller without the route (404, or its Fleet allowlist refusing an unknown path) is `HUB_TOO_OLD`
  with a plain sentence naming 0.6.143; the Mac's own refusal (memory, shelved, disk) passes through;
* the Fleet read offers a Mac only the models its catalog says it can run and does not hold, by package id.
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

_SANDBOX = sandbox_env("gateway-model-install-")

import httpx  # noqa: E402

from gateway import admin, config, fleet, startup_control  # noqa: E402

FISH = "fish-audio-s2-pro-4bit"
FISH_REPO = "majentik/fishaudio-s2-pro-MLX-4bit"


class RelayTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gateway-model-install-case-")
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(tmp.name)),
                        patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, answer, fn=startup_control.install_model, *args):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return answer
        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            return fn("ctl1", "node-1-hub", *(args or (FISH,)), client=client), seen
        except startup_control.StartupError as error:
            return error, seen

    def events(self) -> list[str]:
        return [row["event"] for row in startup_control.recent_audit(50)]

    def test_it_goes_to_the_controller_on_the_fleet_credential_and_is_audited(self) -> None:
        run = {"id": "run-1", "package_id": FISH, "state": "running", "total_bytes": 100}
        answer, seen = self.call(httpx.Response(200, json={"ok": True, "node": "node-1-hub", "run": run}))
        self.assertEqual(answer["run"]["id"], "run-1")
        self.assertEqual((seen[0].method, seen[0].url.path),
                         ("POST", f"/api/hub/fleet/nodes/node-1-hub/model-packages/{FISH}/download"))
        self.assertEqual(seen[0].headers["x-hub-token"], "fleet-token")
        self.assertIn("model_install_requested", self.events())
        self.assertIn("model_install_result", self.events())

    def test_the_status_read_goes_the_same_way_and_is_not_audited(self) -> None:
        run = {"id": "run-1", "state": "running", "total_bytes": 100, "bytes_done": 42}
        answer, seen = self.call(httpx.Response(200, json={"ok": True, "run": run}),
                                 startup_control.model_install_status, "run-1")
        self.assertEqual(answer["run"]["bytes_done"], 42)
        self.assertEqual((seen[0].method, seen[0].url.path), ("GET", "/api/hub/fleet/nodes/node-1-hub/model-downloads/run-1"))
        self.assertEqual(self.events(), [])

    def test_a_controller_without_the_route_is_too_old_in_a_plain_sentence(self) -> None:
        older = {"detail": "POST /api/hub/fleet/nodes/node-1-hub/model-packages/x/download was refused by mini: the "
                           "fleet credential may send work to this Mac and read its results, but it cannot change "
                           "this Mac's settings or delete its output."}
        for response in (httpx.Response(404, json={"detail": "Not Found"}), httpx.Response(403, json=older)):
            error, _ = self.call(response)
            self.assertEqual(error.body["code"], "HUB_TOO_OLD")
            self.assertIn("needs Studio Hub 0.6.143", error.body["detail"])
        error, _ = self.call(httpx.Response(403, json={"detail": "refused"}))
        self.assertEqual(error.body["code"], "CONTROLLER_REFUSED", "a plain 403 is still a credential refusal")

    def test_the_macs_own_refusal_reaches_the_caller(self) -> None:
        sentence = (f"Not on this Mac: {FISH} needs a Mac with at least 16 GB of memory and this one has 8 GB, "
                    "so it could never run here. Nothing was downloaded.")
        error, _ = self.call(httpx.Response(409, json={"ok": False, "code": "REFUSED", "detail": sentence}))
        self.assertEqual((error.status, error.body["detail"]), (409, sentence))
        error, _ = self.call(httpx.Response(502, json={"ok": False, "code": "NODE_REFUSED", "detail": "older"}))
        self.assertEqual((error.status, error.body["code"]), (502, "NODE_REFUSED"))

    def test_junk_ids_never_leave_the_gateway(self) -> None:
        error, seen = self.call(httpx.Response(200, json={}), startup_control.install_model, "../../etc")
        self.assertEqual((error.status, seen), (400, []))

    def test_the_admin_routes_pass_the_answer_and_the_refusal_through(self) -> None:
        refusal = startup_control.StartupError(404, {"ok": False, "code": "HUB_TOO_OLD", "detail": "x"})
        with patch.object(startup_control, "install_model", side_effect=refusal):
            response = admin.fleet_machine_install_model("ctl1", "n1", FISH)
        self.assertEqual((response.status_code, json.loads(response.body)["code"]), (404, "HUB_TOO_OLD"))
        with patch.object(startup_control, "model_install_status", return_value={"ok": True}) as called:
            self.assertEqual(admin.fleet_machine_model_download("ctl1", "n1", "run-1"), {"ok": True})
        called.assert_called_once_with("ctl1", "n1", "run-1")


class OfferTest(unittest.TestCase):
    def test_the_hub_rows_gain_their_package_id_from_the_catalog(self) -> None:
        packages = {"models": [{"package_id": FISH, "model_id": FISH_REPO, "type": "model"},
                               {"package_id": "codec", "model_id": "x/codec", "type": "dependency"}]}
        rows = fleet._local_models({"models": [{"repo": FISH_REPO, "model_id": FISH_REPO, "hub_machine": "n1",
                                                "memory_eligible": True, "cache": {"state": "absent"}}]},
                                   {}, fleet._package_ids(packages))
        self.assertEqual(rows[0]["package_id"], FISH)
        self.assertEqual(fleet._package_ids(packages), {FISH_REPO: FISH}, "a dependency is not offered")

    def test_a_mac_is_offered_only_what_it_can_run_and_does_not_hold(self) -> None:
        def row(machine, package, *, fits=True, cache="absent", downloading=False):
            return {"machine": machine, "package_id": package, "model": package, "label": package.title(),
                    "memory_eligible": fits, "cache_state": cache, "active_download": downloading, "size_gb": 3.5}
        location = {"id": "ctl", "label": "C", "local": False, "reachable": True, "nodes": [],
                    "activity": {"machines": []}, "workers": [],
                    "machines": [{"machine": "n1", "reachable": True}, {"machine": "n8", "reachable": True}],
                    "local_models": [row("n1", FISH), row("n1", "kokoro", cache="cached"), row("n1", "qwen", downloading=True),
                                     row("n1", "flux", fits=False), row("n8", FISH, fits=False),
                                     {**row("n1", FISH), "package_id": None}]}
        n1, n8 = fleet.machines_flat([location])
        self.assertEqual([m["package_id"] for m in n1["installable_models"]], [FISH])
        self.assertEqual(n8["installable_models"], [], "an 8 GB Mac is never offered Fish")


if __name__ == "__main__":
    unittest.main()
