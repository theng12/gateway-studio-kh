"""macOS's own "Remove items from the Trash after 30 days", seen and turned on from the Gateway.

Owner request, 2026-10-19: every Studio Hub retirement ends in macOS Trash, so a Mac whose Finder never
empties that Trash fills its disk - and nobody could tell which of the fleet's Macs had the setting.
What these tests pin:

* the per-Mac value rides the `startup` object the Fleet page already reads, and a Hub that says
  nothing about it (older than 0.6.187) reads as **unknown**, never as off;
* one Mac's "Turn on" goes to its controller's own copy of the route on the fleet credential, and the
  Hub's refusal (`HUB_TOO_OLD`, `NODE_UNREACHABLE`, `NOT_APPLIED`) reaches the caller unchanged;
* "turn on for all Macs that are off" touches exactly the Macs whose Hub said `false` and that answer,
  and lists the offline and stale ones with why instead of blocking on them;
* the admin routes are admin-only.
"""
from __future__ import annotations

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

_SANDBOX = sandbox_env("gateway-macos-trash-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")
os.environ["GATEWAY_API_KEY"] = "macos-trash-fixture-key"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, config, fleet, startup_control  # noqa: E402

OFF = {"open_at_login": True, "start_services_on_launch": True, "app_reporting": True,
       "app_version": "0.6.187", "macos_trash_auto_empty": False}
ON = {**OFF, "macos_trash_auto_empty": True}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-macos-trash-case-")
        self.addCleanup(self.tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(self.tmp.name)),
                        # Pinned per test: another file in a `discover` run may have frozen a different key.
                        patch.dict(os.environ, {"GATEWAY_API_KEY": "macos-trash-fixture-key",
                                                "GATEWAY_HOST": "127.0.0.1"})):
            patcher.start()
            self.addCleanup(patcher.stop)


class PassThroughTests(_Isolated):
    """The Gateway republishes a checked copy of what the Mac said - never a guess."""

    def fact(self, startup):
        return fleet._startup_fact({"startup": startup}, {})

    def test_the_value_the_hub_sent_is_the_value_the_fleet_page_reads(self) -> None:
        self.assertIs(self.fact(ON)["macos_trash_auto_empty"], True)
        self.assertIs(self.fact(OFF)["macos_trash_auto_empty"], False)

    def test_a_hub_that_says_nothing_reads_as_unknown_not_as_off(self) -> None:
        """A Hub older than 0.6.187 has no such field, and an unreachable Mac may never have answered."""
        self.assertIsNone(self.fact({"open_at_login": True})["macos_trash_auto_empty"])
        self.assertIsNone(self.fact({**ON, "macos_trash_auto_empty": None})["macos_trash_auto_empty"])

    def test_anything_that_is_not_a_boolean_is_unknown(self) -> None:
        for value in ("true", "false", 1, 0, [], {}, "yes"):
            with self.subTest(value=value):
                self.assertIsNone(self.fact({**ON, "macos_trash_auto_empty": value})["macos_trash_auto_empty"])

    def test_it_also_rides_a_node_row_a_controller_is_repeating(self) -> None:
        self.assertIs(fleet._startup_fact({}, {"startup": ON})["macos_trash_auto_empty"], True)


class RelayTests(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen: list[httpx.Request] = []

    def client(self, answer) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return answer
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_the_request_goes_to_the_controllers_node_route_on_the_fleet_credential(self) -> None:
        answer = httpx.Response(200, json={"ok": True, "macos_trash_auto_empty": True,
                                           "finder_restart_required": False})
        got = startup_control.enable_macos_trash_auto_empty("ctl1", "node-1", client=self.client(answer))
        self.assertIs(got["macos_trash_auto_empty"], True)
        self.assertIs(got["finder_restart_required"], False)
        self.assertEqual(str(self.seen[0].url),
                         "http://100.64.0.2:47873/api/hub/fleet/nodes/node-1/macos-trash-auto-empty/enable")
        self.assertEqual(self.seen[0].method, "POST")
        self.assertEqual(self.seen[0].headers["x-hub-token"], "fleet-token")

    def test_a_hub_older_than_0_6_187_reads_as_needing_it_not_as_an_error(self) -> None:
        for answer in (httpx.Response(404, json={"detail": "Not Found"}),        # controller predates the route
                       httpx.Response(403, json={"detail": "the fleet credential may send work only to ..."})):
            with self.subTest(answer=answer.status_code):
                with self.assertRaises(startup_control.StartupError) as caught:
                    startup_control.enable_macos_trash_auto_empty("ctl1", "node-1", client=self.client(answer))
                self.assertEqual(caught.exception.body["code"], "HUB_TOO_OLD")
                self.assertIn("0.6.187", caught.exception.body["detail"])
        # The node's own refusal carries the version it needs, straight from the Hub.
        old = httpx.Response(409, json={"ok": False, "code": "HUB_TOO_OLD", "needs_hub": "0.6.187",
                                        "detail": "That Mac needs Studio Hub 0.6.187"})
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.enable_macos_trash_auto_empty("ctl1", "node-1", client=self.client(old))
        self.assertEqual((caught.exception.body["code"], caught.exception.body["needs_hub"]),
                         ("HUB_TOO_OLD", "0.6.187"))

    def test_a_mac_that_is_not_answering_keeps_its_own_code_and_sentence(self) -> None:
        answer = httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "asleep"})
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.enable_macos_trash_auto_empty("ctl1", "node-1", client=self.client(answer))
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (502, "NODE_UNREACHABLE"))

    def test_a_mac_that_wrote_it_but_could_not_read_it_back_is_a_refusal(self) -> None:
        """A 5xx from the Hub is reported as 502 here (the controller did not carry it out), code kept."""
        answer = httpx.Response(500, json={"ok": False, "code": "NOT_APPLIED",
                                           "detail": "The preference was written but does not read back as on."})
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.enable_macos_trash_auto_empty("ctl1", "node-1", client=self.client(answer))
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (502, "NOT_APPLIED"))
        self.assertIn("does not read back as on", caught.exception.body["detail"])

    def test_a_bad_machine_id_never_reaches_the_wire(self) -> None:
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.enable_macos_trash_auto_empty("ctl1", "../etc", client=self.client(httpx.Response(200)))
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (400, "BAD_MACHINE"))
        self.assertEqual(self.seen, [])

    def test_it_is_audited_on_both_ends(self) -> None:
        startup_control.enable_macos_trash_auto_empty(
            "ctl1", "node-1", client=self.client(httpx.Response(200, json={"ok": True})))
        events = [row["event"] for row in startup_control.recent_audit(10)]
        self.assertEqual(events[:2], ["macos_trash_enable_result", "macos_trash_enable_requested"])


class BulkTests(_Isolated):
    def machines(self) -> list[dict]:
        def row(machine, startup, reachable=True):
            return {"location_id": "ctl1", "machine": machine, "label": machine, "reachable": reachable,
                    "startup": startup, "node_reason": None if reachable else "unreachable"}
        return [row("off-up", OFF), row("on-up", ON), row("unknown", {"open_at_login": True}),
                row("off-asleep", OFF, reachable=False),
                row("off-stale", {**OFF, "stale": True}),
                {**row("off-no-hub", OFF, reachable=False), "node_reason": "no_hub"}]

    def events(self) -> list[str]:
        return [row["event"] for row in reversed(startup_control.recent_audit(20))]

    def test_only_macs_that_said_off_and_answer_are_asked(self) -> None:
        asked: list[tuple] = []

        def ask(controller, machine):
            asked.append((controller, machine))
            return {"ok": True, "state": "applied", "macos_trash_auto_empty": True}

        result = startup_control.enable_macos_trash_auto_empty_where_off(machines=self.machines(), ask=ask)
        self.assertEqual(asked, [("ctl1", "off-up")])
        self.assertEqual([row["machine"] for row in result["applied"]], ["off-up"])
        self.assertEqual(result["asked"], 1)

    def test_the_offline_ones_are_listed_with_why_and_never_block_the_rest(self) -> None:
        result = startup_control.enable_macos_trash_auto_empty_where_off(
            machines=self.machines(), ask=lambda c, m: {"ok": True, "state": "applied"})
        skipped = {row["machine"]: row["reason"] for row in result["skipped"]}
        self.assertEqual(set(skipped), {"off-asleep", "off-stale", "off-no-hub"})
        self.assertIn("services are off", skipped["off-no-hub"])
        self.assertIn("asleep", skipped["off-asleep"])
        # Unknown is never guessed at: an older Hub cannot take the request at all.
        self.assertNotIn("unknown", skipped)
        self.assertNotIn("unknown", [row["machine"] for row in result["applied"]])

    def test_a_dry_run_lists_and_asks_nobody(self) -> None:
        def ask(*args):
            raise AssertionError("a dry run must not send anything")
        result = startup_control.enable_macos_trash_auto_empty_where_off(
            dry_run=True, machines=self.machines(), ask=ask)
        self.assertTrue(result["dry_run"])
        self.assertEqual([row["machine"] for row in result["would_ask"]], ["off-up"])
        self.assertEqual(result["asked"], 0)

    def test_a_mac_that_refuses_is_listed_with_the_reason_and_does_not_stop_the_rest(self) -> None:
        machines = [{"location_id": "ctl1", "machine": name, "label": name, "reachable": True, "startup": OFF}
                    for name in ("a", "b", "c")]

        def ask(controller, machine):
            if machine == "b":
                raise startup_control.StartupError(409, {"ok": False, "code": "HUB_TOO_OLD",
                                                         "detail": "needs Studio Hub 0.6.187"})
            return {"ok": True, "state": "applied", "macos_trash_auto_empty": True}

        result = startup_control.enable_macos_trash_auto_empty_where_off(machines=machines, ask=ask)
        self.assertEqual(sorted(row["machine"] for row in result["applied"]), ["a", "c"])
        self.assertEqual(result["failed"][0]["machine"], "b")
        self.assertEqual(result["failed"][0]["code"], "HUB_TOO_OLD")

    def test_the_bulk_run_is_audited_under_its_own_event_names(self) -> None:
        startup_control.enable_macos_trash_auto_empty_where_off(machines=self.machines(), ask=lambda c, m: {"ok": True})
        self.assertEqual(self.events(), ["bulk_enable_macos_trash", "bulk_enable_macos_trash_done"])

    def test_nothing_here_can_turn_it_off(self) -> None:
        """The owner authorized turning this on. There is no bulk off, and no per-Mac off route."""
        asked: list[tuple] = []
        startup_control.enable_macos_trash_auto_empty_where_off(
            machines=[{"location_id": "ctl1", "machine": "on-up", "reachable": True, "startup": ON}],
            ask=lambda c, m: asked.append((c, m)) or {"ok": True})
        self.assertEqual(asked, [])


class AdminRouteTests(_Isolated):
    KEY = {"Authorization": "Bearer macos-trash-fixture-key"}
    ONE = "/admin/api/fleet/controllers/ctl1/machines/node-1/macos-trash-auto-empty/enable"
    ALL = "/admin/api/fleet/trash/enable-macos-auto-empty"

    def remote(self) -> TestClient:
        client = TestClient(api.app, client=("10.9.8.7", 50000))
        self.addCleanup(client.close)
        return client

    def test_nothing_is_open_to_a_caller_that_is_not_the_admin(self) -> None:
        client = self.remote()
        for path in (self.ONE, self.ALL):
            self.assertEqual(client.post(path, json={}).status_code, 401, path)
            self.assertEqual(client.post(path, json={}, headers={"Authorization": "Bearer wrong"}).status_code,
                             403, path)

    def test_one_macs_turn_on_is_passed_through_with_the_hubs_answer(self) -> None:
        answer = {"ok": True, "state": "applied", "macos_trash_auto_empty": True, "machine": "node-1"}
        with patch.object(startup_control, "enable_macos_trash_auto_empty", return_value=answer) as asked:
            reply = self.remote().post(self.ONE, headers=self.KEY, json={})
        self.assertEqual(reply.status_code, 200)
        self.assertIs(reply.json()["macos_trash_auto_empty"], True)
        self.assertEqual(asked.call_args.args, ("ctl1", "node-1"))

    def test_a_refusal_keeps_its_status_and_code(self) -> None:
        def refuse(*args, **kwargs):
            raise startup_control.StartupError(409, {"ok": False, "code": "HUB_TOO_OLD", "needs_hub": "0.6.187",
                                                     "detail": "needs Studio Hub 0.6.187"})
        with patch.object(startup_control, "enable_macos_trash_auto_empty", refuse):
            answer = self.remote().post(self.ONE, headers=self.KEY, json={})
        self.assertEqual(answer.status_code, 409)
        self.assertEqual((answer.json()["code"], answer.json()["needs_hub"]), ("HUB_TOO_OLD", "0.6.187"))

    def test_the_bulk_route_defaults_to_doing_it_and_accepts_a_dry_run(self) -> None:
        calls: list[bool] = []

        def bulk(*, dry_run=False, **kwargs):
            calls.append(dry_run)
            return {"dry_run": dry_run, "asked": 0, "applied": [], "failed": [], "skipped": []}
        with patch.object(startup_control, "enable_macos_trash_auto_empty_where_off", bulk):
            self.remote().post(self.ALL, headers=self.KEY, json={})
            self.remote().post(self.ALL, headers=self.KEY, json={"dry_run": True})
            self.assertEqual(self.remote().post(self.ALL, headers=self.KEY, json={"dry_run": "yes"}).status_code, 400)
        self.assertEqual(calls, [False, True])


class SwiftLabelTests(_Isolated):
    """The Fleet page's own wording, compiled and run against rows the gateway's assembly produced.

    On / Off / Unknown, and **Unknown is never drawn as Off** - the whole point of the column is that the
    owner can tell "this Mac has it off" from "nobody knows", so the two must not read alike.
    """

    SWIFT = r'''
import AppKit

// FleetModels.swift reaches the network for one relative-time string; the check is offline.
enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct MacTrashCheck {
    static func main() {
        let dir = CommandLine.arguments[1]
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let rows = try! decoder.decode([String: FleetMachine].self,
                                       from: try! Data(contentsOf: URL(fileURLWithPath: dir + "/rows.json")))
        func expect(_ actual: String, _ wanted: String, _ what: String) {
            if actual != wanted { print("FAIL \(what): got \(actual), wanted \(wanted)"); exit(1) }
        }
        func expectHealth(_ actual: Health, _ wanted: Health, _ what: String) {
            if actual != wanted { print("FAIL \(what): got \(actual), wanted \(wanted)"); exit(1) }
        }
        func text(_ id: String) -> String { FleetMacTrash.health(rows[id]!.startup).text }

        expect(text("on"), "On", "a Mac whose Hub said it is on")
        expect(text("off"), "Off", "a Mac whose Hub said it is off")
        expect(text("silent"), "Unknown", "a Hub older than 0.6.187 says nothing - unknown, not off")
        expect(text("junk"), "Unknown", "a value that is not a boolean is not a claim about the setting")
        expect(text("nohub"), "Unknown", "a Mac whose controller never heard from it")

        expectHealth(FleetMacTrash.health(rows["off"]!.startup).health, .warn, "off is the one worth an amber badge")
        expectHealth(FleetMacTrash.health(rows["on"]!.startup).health, .ok, "on is green")
        expectHealth(FleetMacTrash.health(rows["silent"]!.startup).health, .off, "unknown is grey, never green")

        // A tooltip says what the setting is and whose it is, so nobody hunts for it in Studio Hub.
        expect(String(FleetMacTrash.tooltip(rows["off"]!.startup, name: "Shop 3").contains("Finder > Settings")), "true",
               "the tooltip names where the setting lives")
        expect(String(FleetMacTrash.tooltip(rows["silent"]!.startup, name: "Shop 3").contains("0.6.187")), "true",
               "and says which Hub would have reported it")

        // The bulk run: only the Macs that said off AND answer. Never unknown, never stale, never silent.
        let all = ["on", "off", "silent", "junk", "nohub"].map { rows[$0]! }
        expect(FleetMacTrash.targets(all).map { $0.machine! }.joined(separator: ","), "off",
               "exactly the one Mac that said off and is answering")
        expect(String(FleetMacTrash.offCount(all)), "1", "the count the bulk bar shows")
        expect(String(FleetMacTrash.unreachable(all).count), "0", "nothing to skip here")
        print("PASS: the macOS Trash column reads on, off and unknown apart")
    }
}
'''

    def test_the_column_tells_on_from_off_from_unknown(self) -> None:
        import json
        import subprocess

        rows = {
            "on": fleet._startup_fact({"startup": {**ON}}, {}),
            "off": fleet._startup_fact({"startup": {**OFF}}, {}),
            "silent": fleet._startup_fact({"startup": {"open_at_login": True}}, {}),
            "junk": fleet._startup_fact({"startup": {**ON, "macos_trash_auto_empty": "yes"}}, {}),
            "nohub": None,
        }
        machines = {name: {"machine": name, "reachable": True, "startup": fact} for name, fact in rows.items()}
        # `nohub` never reported at all: the row carries no `startup` object.
        machines["nohub"].pop("startup")
        with tempfile.TemporaryDirectory(prefix="gateway-macos-trash-swift-") as directory:
            work = Path(directory)
            (work / "check.swift").write_text(self.SWIFT)
            (work / "rows.json").write_text(json.dumps(machines))
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit", "menubar/PageKit.swift",
                 "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift",
                 "menubar/FleetWorkstation.swift", "menubar/FleetMacTrashModel.swift",
                 str(work / "check.swift"), "-o", str(work / "check")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(work / "check"), directory], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
