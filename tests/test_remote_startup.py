"""Seeing and changing, from the Gateway, what each Mac starts when it starts.

Owner, 2026-10-02: "I don't know if each Mac has 'start services on startup' enabled ... Can we see,
enable, and restart the services remotely from the Gateway?" What these tests pin:

* every machine row carries a `startup` object, and *unknown* is never shown as *off*;
* a request goes to the Mac's controller on the fleet credential, with a validated body, and the Hub's
  refusal (work in progress, too old, not running) reaches the caller as the Hub's own sentence;
* the admin routes are admin-only, and the bulk "enable on all Macs where it's off" touches exactly
  the Macs that said off and answer - and lists, rather than tries, the ones that do not;
* every request is audited.
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

_SANDBOX = sandbox_env("gateway-startup-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")
os.environ["GATEWAY_API_KEY"] = "startup-fixture-key"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, config, fleet, startup_control  # noqa: E402

OFF = {"open_at_login": True, "open_at_login_saved": True, "start_services_on_launch": False,
       "app_reporting": True, "app_version": "0.6.127",
       "services_running": {"up": 4, "total": 4, "studios": {"voice": "up"}}}
ON = {**OFF, "start_services_on_launch": True}


def location(machines: list[dict], nodes: list[dict] | None = None) -> dict:
    return {"id": "ctl1", "label": "Site A", "local": False, "reachable": True,
            "machines": machines, "nodes": nodes or [],
            "activity": {"machines": [{"machine": m["machine"], "state": "ready"} for m in machines],
                         "pulse": {}},
            "running_jobs": [], "local_models": [], "workers": [], "probe_machines_hidden": 0}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-startup-case-")
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        # `unittest discover` imports the gateway once for the whole run, so another file's key may be
        # the one it froze: the admin key is pinned here, per test, where it is read.
        keyed = patch.dict(os.environ, {"GATEWAY_API_KEY": "startup-fixture-key"})
        keyed.start()
        self.addCleanup(keyed.stop)

    def events(self) -> list[str]:
        return [row["event"] for row in reversed(startup_control.recent_audit(100))]


class SnapshotTests(unittest.TestCase):
    def test_a_machine_row_carries_the_startup_report_the_hub_sent(self) -> None:
        rows = fleet.machines_flat([location([{"machine": "m1", "reachable": True, "startup": OFF}])])
        fact = rows[0]["startup"]
        self.assertIs(fact["start_services_on_launch"], False)
        self.assertIs(fact["open_at_login"], True)
        self.assertEqual(fact["services_running"]["up"], 4)
        self.assertEqual(fact["app_version"], "0.6.127")

    def test_a_node_row_is_used_when_the_overview_says_nothing(self) -> None:
        rows = fleet.machines_flat([location([{"machine": "n1", "reachable": False}],
                                             [{"id": "n1", "state": "unreachable", "reason": "unreachable",
                                               "startup": {**OFF, "stale": True}}])])
        self.assertTrue(rows[0]["startup"]["stale"], "a Mac that went quiet is described by its last word")
        self.assertIs(rows[0]["startup"]["start_services_on_launch"], False)

    def test_a_hub_that_says_nothing_is_unknown_not_off(self) -> None:
        rows = fleet.machines_flat([location([{"machine": "old", "reachable": True}])])
        self.assertIsNone(rows[0]["startup"])
        summary = fleet._summarise([location([{"machine": "old", "reachable": True}])])
        self.assertEqual(summary["startup_off"], 0)
        self.assertEqual(summary["startup_unknown"], 1)

    def test_only_the_known_keys_and_real_booleans_are_republished(self) -> None:
        rows = fleet.machines_flat([location([{"machine": "m1", "reachable": True, "startup": {
            "start_services_on_launch": "yes", "open_at_login": 1, "evil": "<script>", "app_version": "1"}}])])
        fact = rows[0]["startup"]
        self.assertIsNone(fact["start_services_on_launch"])
        self.assertIsNone(fact["open_at_login"])
        self.assertNotIn("evil", fact)

    def test_the_summary_counts_the_macs_that_will_not_serve_after_a_restart(self) -> None:
        row = location([{"machine": "a", "reachable": True, "startup": OFF},
                        {"machine": "b", "reachable": True, "startup": ON},
                        {"machine": "c", "reachable": True}])
        summary = fleet._summarise([row])
        self.assertEqual((summary["startup_off"], summary["startup_unknown"]), (1, 1))

    def test_one_issue_per_location_says_how_many_and_what_to_press(self) -> None:
        row = {**location([{"machine": "a", "label": "terranash-1201", "reachable": True, "startup": OFF},
                           {"machine": "b", "reachable": True, "startup": OFF}]), "role": "controller"}
        issues = [issue for issue in fleet._issues_for(row) if issue["code"] == "services_not_auto_start"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertIn("2 machines", issues[0]["subject"])
        self.assertIn("Enable auto-start", issues[0]["action"])
        self.assertEqual(
            [i for i in fleet._issues_for(location([{"machine": "a", "reachable": True, "startup": ON}]))
             if i["code"] == "services_not_auto_start"], [])


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class RequestTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(startup_control, "_target",
                               lambda controller_id: ("http://100.64.0.2:47873", "fleet-token-value"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen: list[httpx.Request] = []

    def ask(self, answer, *args, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return answer(request) if callable(answer) else answer
        return startup_control.request("ctl1", "node-1", *args, client=_client(handler), **kwargs)

    def test_it_goes_to_the_controllers_relay_route_on_the_fleet_credential(self) -> None:
        answer = self.ask(httpx.Response(200, json={"ok": True, "state": "applied"}),
                          "set_start_services_on_launch", True)
        request = self.seen[0]
        self.assertEqual(str(request.url), "http://100.64.0.2:47873/api/hub/fleet/nodes/node-1/startup")
        self.assertEqual(request.headers["X-Hub-Token"], "fleet-token-value")
        self.assertEqual(json.loads(request.content), {"action": "set_start_services_on_launch", "force": False,
                                                       "requested_by": "gateway-admin", "value": True})
        self.assertEqual(answer["state"], "applied")
        self.assertEqual(answer["machine"], "node-1")
        self.assertEqual(self.events(), ["requested", "result"])

    def test_a_bad_request_never_leaves_the_gateway(self) -> None:
        for args in (("reboot", True), ("set_open_at_login", None), ("set_open_at_login", "yes")):
            with self.assertRaises(startup_control.StartupError) as error:
                self.ask(httpx.Response(200, json={}), *args)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(startup_control.StartupError):
            self.ask(httpx.Response(200, json={}), "restart_services", None, "yes")
        self.assertEqual(self.seen, [])
        with self.assertRaises(startup_control.StartupError) as bad_machine:
            startup_control.request("ctl1", "../../etc", "restart_services", client=_client(lambda r: None))
        self.assertEqual(bad_machine.exception.body["code"], "BAD_MACHINE")

    def test_a_restart_refused_for_running_work_keeps_the_hubs_status_and_counts(self) -> None:
        refusal = {"ok": False, "code": "WORK_IN_PROGRESS", "detail": "1 job(s) running",
                   "work": {"running": 1, "queued": 0}}
        with self.assertRaises(startup_control.StartupError) as error:
            self.ask(httpx.Response(409, json=refusal), "restart_services")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(error.exception.body["work"], {"running": 1, "queued": 0})
        self.assertIs(json.loads(self.seen[0].content)["force"], False, "no force unless asked")
        forced = self.ask(httpx.Response(200, json={"ok": True, "state": "handed_to_app"}),
                          "restart_services", None, True)
        self.assertIs(json.loads(self.seen[1].content)["force"], True)
        self.assertEqual(forced["state"], "handed_to_app")

    def test_a_controller_without_the_route_is_named_too_old_not_a_bare_404(self) -> None:
        with self.assertRaises(startup_control.StartupError) as error:
            self.ask(httpx.Response(404, json={"detail": "Not Found"}), "restart_services")
        self.assertEqual(error.exception.body["code"], "HUB_TOO_OLD")
        self.assertIn("0.6.127", error.exception.body["detail"])

    def test_a_node_with_no_hub_running_reaches_the_caller_as_the_hubs_own_answer(self) -> None:
        body = {"ok": False, "code": "NODE_UNREACHABLE", "reason": "no_hub", "detail": "services are off"}
        with self.assertRaises(startup_control.StartupError) as error:
            self.ask(httpx.Response(502, json=body), "set_start_services_on_launch", True)
        self.assertEqual(error.exception.body["reason"], "no_hub")
        self.assertEqual(error.exception.status, 502)

    def test_a_controller_that_does_not_answer_is_a_502_with_a_sentence(self) -> None:
        def refuse(request: httpx.Request):
            raise httpx.ConnectError("refused", request=request)
        with self.assertRaises(startup_control.StartupError) as error:
            self.ask(refuse, "restart_services")
        self.assertEqual(error.exception.status, 502)
        self.assertEqual(error.exception.body["code"], "CONTROLLER_UNREACHABLE")
        self.assertEqual(self.events(), ["requested", "result"])

    def test_a_rejected_credential_is_not_reported_as_a_job_problem(self) -> None:
        with self.assertRaises(startup_control.StartupError) as error:
            self.ask(httpx.Response(403, json={"detail": "refused"}), "restart_services")
        self.assertEqual(error.exception.body["code"], "CONTROLLER_REFUSED")

    def test_the_audit_log_is_private_and_bounded_and_newest_first(self) -> None:
        for number in range(startup_control._AUDIT_KEEP + 5):
            startup_control.audit("noop", n=number)
        path = startup_control.audit_path()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(path.read_text().splitlines()), startup_control._AUDIT_KEEP)
        self.assertEqual(startup_control.recent_audit(1)[0]["n"], startup_control._AUDIT_KEEP + 4)


class TargetTests(unittest.TestCase):
    def test_a_controller_is_reached_with_its_own_fleet_token_and_this_mac_with_its_hub_token(self) -> None:
        with patch.object(fleet, "roster", lambda: [{"id": "ctl1", "url": "http://100.64.0.2:47873/"}]), \
                patch.object(fleet, "_token_for", lambda cid: f"token-for-{cid}"):
            self.assertEqual(startup_control._target("ctl1"), ("http://100.64.0.2:47873", "token-for-ctl1"))
            with self.assertRaises(startup_control.StartupError) as unknown:
                startup_control._target("nope")
            self.assertEqual(unknown.exception.status, 404)
        with patch("gateway.hub.hub_base_url", lambda: "http://127.0.0.1:47873"), \
                patch("gateway.hub.hub_token", lambda: "local-hub-token"):
            self.assertEqual(startup_control._target(fleet.LOCAL_ID),
                             ("http://127.0.0.1:47873", "local-hub-token"))


class BulkTests(_Isolated):
    def machines(self) -> list[dict]:
        def row(machine, startup, reachable=True):
            return {"location_id": "ctl1", "machine": machine, "label": machine, "reachable": reachable,
                    "startup": startup, "node_reason": None if reachable else "unreachable"}
        return [row("off-up", OFF), row("on-up", ON), row("unknown", None),
                row("off-asleep", OFF, reachable=False),
                row("off-stale", {**OFF, "stale": True}),
                {**row("off-no-hub", OFF, reachable=False), "node_reason": "no_hub"}]

    def test_only_macs_that_said_off_and_answer_are_asked(self) -> None:
        asked: list[tuple] = []

        def ask(controller, machine, action, value):
            asked.append((controller, machine, action, value))
            return {"ok": True, "state": "applied"}

        result = startup_control.enable_autostart_where_off(machines=self.machines(), ask=ask)
        self.assertEqual(asked, [("ctl1", "off-up", "set_start_services_on_launch", True)])
        self.assertEqual([row["machine"] for row in result["applied"]], ["off-up"])
        skipped = {row["machine"]: row["reason"] for row in result["skipped"]}
        self.assertEqual(set(skipped), {"off-asleep", "off-stale", "off-no-hub"})
        self.assertIn("services are off", skipped["off-no-hub"])
        self.assertIn("asleep", skipped["off-asleep"])
        self.assertEqual(self.events(), ["bulk_enable_autostart", "bulk_enable_autostart_done"])

    def test_a_dry_run_lists_and_asks_nobody(self) -> None:
        def ask(*args):
            raise AssertionError("a dry run must not send anything")
        result = startup_control.enable_autostart_where_off(dry_run=True, machines=self.machines(), ask=ask)
        self.assertTrue(result["dry_run"])
        self.assertEqual([row["machine"] for row in result["would_ask"]], ["off-up"])
        self.assertEqual(result["asked"], 0)

    def test_a_mac_that_refuses_is_listed_with_the_reason_and_does_not_stop_the_rest(self) -> None:
        machines = [{"location_id": "ctl1", "machine": name, "label": name, "reachable": True, "startup": OFF}
                    for name in ("a", "b", "c")]

        def ask(controller, machine, action, value):
            if machine == "b":
                raise startup_control.StartupError(409, {"ok": False, "code": "APP_NOT_REPORTING",
                                                         "detail": "open Studio Hub once"})
            return {"ok": True, "state": "queued"}

        result = startup_control.enable_autostart_where_off(machines=machines, ask=ask)
        self.assertEqual(sorted(row["machine"] for row in result["applied"]), ["a", "c"])
        self.assertEqual(result["failed"][0]["machine"], "b")
        self.assertEqual(result["failed"][0]["code"], "APP_NOT_REPORTING")


class AdminRouteTests(_Isolated):
    KEY = {"Authorization": "Bearer startup-fixture-key"}

    def remote(self) -> TestClient:
        client = TestClient(api.app, client=("10.9.8.7", 50000))
        self.addCleanup(client.close)
        return client

    def local(self) -> TestClient:
        client = TestClient(api.app, client=("127.0.0.1", 50001))
        self.addCleanup(client.close)
        return client

    PATHS = ("/admin/api/fleet/controllers/ctl1/machines/node-1/startup", "/admin/api/fleet/startup/enable-autostart")

    def test_nothing_is_open_to_a_caller_that_is_not_the_admin(self) -> None:
        client = self.remote()
        for path in self.PATHS:
            self.assertEqual(client.post(path, json={}).status_code, 401, path)
            self.assertEqual(client.post(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code,
                             403, path)
        self.assertEqual(client.get("/admin/api/fleet/startup/audit").status_code, 401)

    def test_a_valid_request_is_passed_through_with_the_hubs_answer(self) -> None:
        with patch.object(startup_control, "request",
                          lambda *a, **k: {"ok": True, "state": "applied", "machine": a[1]}) as asked:
            answer = self.remote().post(self.PATHS[0], headers=self.KEY,
                                        json={"action": "set_start_services_on_launch", "value": True})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["state"], "applied")

    def test_a_refusal_keeps_its_status_and_code(self) -> None:
        def refuse(*args, **kwargs):
            raise startup_control.StartupError(409, {"ok": False, "code": "WORK_IN_PROGRESS",
                                                     "work": {"running": 2, "queued": 0}})
        with patch.object(startup_control, "request", refuse):
            answer = self.remote().post(self.PATHS[0], headers=self.KEY, json={"action": "restart_services"})
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json()["code"], "WORK_IN_PROGRESS")

    def test_a_malformed_body_is_a_400_before_anything_is_sent(self) -> None:
        answer = self.remote().post(self.PATHS[0], headers=self.KEY, json={"action": "wipe"})
        self.assertEqual(answer.status_code, 400)
        self.assertEqual(answer.json()["code"], "UNKNOWN_ACTION")
        self.assertEqual(self.remote().post(self.PATHS[1], headers=self.KEY,
                                            json={"dry_run": "yes"}).status_code, 400)

    def test_the_bulk_route_defaults_to_doing_it_and_accepts_a_dry_run(self) -> None:
        calls: list[bool] = []

        def bulk(*, dry_run=False, **kwargs):
            calls.append(dry_run)
            return {"dry_run": dry_run, "asked": 0, "applied": [], "failed": [], "skipped": []}
        with patch.object(startup_control, "enable_autostart_where_off", bulk):
            self.remote().post(self.PATHS[1], headers=self.KEY, json={})
            self.remote().post(self.PATHS[1], headers=self.KEY, json={"dry_run": True})
        self.assertEqual(calls, [False, True])

    def test_the_audit_route_lists_newest_first(self) -> None:
        startup_control.audit("requested", machine="a")
        startup_control.audit("result", machine="a")
        rows = self.remote().get("/admin/api/fleet/startup/audit", headers=self.KEY).json()["audit"]
        self.assertEqual([row["event"] for row in rows][:2], ["result", "requested"])


if __name__ == "__main__":
    unittest.main()
