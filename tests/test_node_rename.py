"""Renaming a Mac from the Fleet page, and a renamed Mac showing its new name.

Live 2026-10-02: studio-0108-m1-8-256 was renamed studio-1409-m1-8-256 on the Mac; its controller's
roster, and so this Fleet page, kept the old name. The row's name is the controller's roster name
(`machine_label`), and nothing the gateway receives carries the Mac's own name, so the Hub fixes the
roster (the release after 0.6.132). The owner also asked to rename a Mac from here. What these tests pin:

* the row shows the roster name, so it follows the controller's roster once that changes;
* a rename goes to the Mac's controller on the fleet credential with a validated name, and the
  controller's refusal (Mac off, Hub too old) reaches the caller as a sentence;
* the admin route is admin-only and passes the answer through.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-rename-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")
os.environ["GATEWAY_API_KEY"] = "rename-fixture-key"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, config, fleet, startup_control  # noqa: E402

NODE = "studio-0108-m1-8-256-hub"


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-rename-case-")
        self.addCleanup(self.tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(self.tmp.name)),
                        patch.dict(os.environ, {"GATEWAY_API_KEY": "rename-fixture-key"})):
            patcher.start()
            self.addCleanup(patcher.stop)


class LabelTests(unittest.TestCase):
    def test_the_row_is_named_by_the_roster_so_a_renamed_roster_renames_the_row(self) -> None:
        row = {"id": "ctl1", "label": "Site A", "local": False}
        machine = {"machine": NODE, "label": "studio-0108-m1-8-256"}
        self.assertEqual(fleet.machine_label(row, machine, {"id": NODE, "name": "studio-1409-m1-8-256"}),
                         "studio-1409-m1-8-256")


class RenameTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(startup_control, "_target",
                               lambda controller_id: ("http://100.64.0.2:47873", "fleet-token-value"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen: list[httpx.Request] = []

    def rename(self, answer, name="studio-1409-m1-8-256", machine=NODE):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return startup_control.rename("ctl1", machine, name,
                                      client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_it_goes_to_the_controller_on_the_fleet_credential_and_drops_the_cached_fleet(self) -> None:
        with patch.object(fleet, "invalidate") as invalidated:
            answer = self.rename(httpx.Response(200, json={"ok": True, "node": NODE, "name": "studio-1409"}),
                                 "  studio-1409  ")
        request = self.seen[0]
        self.assertEqual(str(request.url), f"http://100.64.0.2:47873/api/hub/fleet/nodes/{NODE}/name")
        self.assertEqual(request.headers["X-Hub-Token"], "fleet-token-value")
        self.assertEqual(json.loads(request.content), {"name": "studio-1409"})
        self.assertTrue(answer["ok"])
        invalidated.assert_called_once()

    def test_a_bad_name_or_machine_never_leaves_the_gateway(self) -> None:
        for name in ("", "   ", "x" * 121, "two\nlines", None):
            with self.assertRaises(startup_control.StartupError) as error:
                self.rename(httpx.Response(200, json={}), name)
            self.assertEqual(error.exception.body["code"], "BAD_NAME")
        with self.assertRaises(startup_control.StartupError) as error:
            self.rename(httpx.Response(200, json={}), machine="../../etc")
        self.assertEqual(error.exception.body["code"], "BAD_MACHINE")
        self.assertEqual(self.seen, [])

    def test_the_controllers_refusal_is_passed_through_and_nothing_is_cached_away(self) -> None:
        cases = ((httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "off"}),
                  502, "NODE_UNREACHABLE"),
                 (httpx.Response(404, json={"detail": "Not Found"}), 404, "HUB_TOO_OLD"),
                 (httpx.Response(403, json={"detail": "refused"}), 403, "CONTROLLER_REFUSED"),
                 (httpx.ConnectError("refused"), 502, "CONTROLLER_UNREACHABLE"))
        for answer, status, code in cases:
            with patch.object(fleet, "invalidate") as invalidated:
                with self.assertRaises(startup_control.StartupError) as error:
                    self.rename(answer)
            self.assertEqual((error.exception.status, error.exception.body["code"]), (status, code))
            invalidated.assert_not_called()


class AdminRouteTests(_Isolated):
    PATH = f"/admin/api/fleet/controllers/ctl1/machines/{NODE}/name"
    KEY = {"Authorization": "Bearer rename-fixture-key"}

    def client(self) -> TestClient:
        client = TestClient(api.app, client=("10.9.8.7", 50000))
        self.addCleanup(client.close)
        return client

    def test_only_the_admin_may_rename(self) -> None:
        self.assertEqual(self.client().post(self.PATH, json={"name": "x"}).status_code, 401)
        self.assertEqual(self.client().post(self.PATH, json={"name": "x"},
                                            headers={"Authorization": "Bearer wrong"}).status_code, 403)

    def test_the_answer_and_a_refusal_pass_through(self) -> None:
        with patch.object(startup_control, "rename",
                          lambda c, m, n: {"ok": True, "node": m, "name": n, "machine": m}):
            answer = self.client().post(self.PATH, headers=self.KEY, json={"name": "New Name"})
        self.assertEqual(answer.json()["name"], "New Name")
        bad = self.client().post(self.PATH, headers=self.KEY, json={"name": ""})
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "BAD_NAME"))


if __name__ == "__main__":
    unittest.main()
