"""Each Mac's Trash Folder, managed from the Gateway (owner decision 4g, 2026-10-16; Studio Hub 0.6.182).

* listing and "Move to macOS Trash" go through the Mac's controller on the fleet credential, behind the
  normal admin auth; a Mac (or controller) on an older Hub reads as `needs_hub: 0.6.182`, not an error;
  an unreachable Mac is labelled `unreachable` with no size, and only `fresh` readings count in a total;
* a permanent delete needs the owner's own app window: the per-launch secret only the app holds, on a
  loopback request. The gateway key, a browser session or loopback alone are refused, so an agent that
  holds the key cannot delete anything;
* the Mac's count/bytes refusal (409 `TRASH_CHANGED`) reaches the caller unchanged.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-trash-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")
os.environ["GATEWAY_API_KEY"] = "trash-fixture-key"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin_session, api, config, fleet, startup_control  # noqa: E402

SECRET = "s" * 64
LISTING = {"ok": True, "count": 2, "bytes": 8192, "items": [], "by_kind": {}, "by_age": {},
           "disk": {"free_bytes": 1, "total_bytes": 2}}


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gateway-trash-case-")
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(tmp.name)),
                        # Pinned per test: another file in a `discover` run may have frozen a different bind or key.
                        patch.dict(os.environ, {"GATEWAY_API_KEY": "trash-fixture-key", "GATEWAY_HOST": "127.0.0.1"}),
                        patch.object(admin_session, "_owner_secret", "")):
            patcher.start()
            self.addCleanup(patcher.stop)


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

    def test_the_listing_goes_to_the_controllers_node_route_on_the_fleet_credential(self) -> None:
        got = startup_control.trash_list("ctl1", "node-1", client=self.client(httpx.Response(200, json=LISTING)))
        self.assertEqual((got["supported"], got["status"], got["bytes"]), (True, "fresh", 8192))
        self.assertAlmostEqual(got["measured_at"], time.time(), delta=5)   # stamped when no Hub time is stated
        self.assertEqual(str(self.seen[0].url), "http://100.64.0.2:47873/api/hub/fleet/nodes/node-1/trash")
        self.assertEqual(self.seen[0].headers["x-hub-token"], "fleet-token")

    def test_an_older_hub_reads_as_needing_0_6_182_not_as_an_error(self) -> None:
        for answer in (httpx.Response(404, json={"detail": "Not Found"}),           # controller predates the route
                       httpx.Response(403, json={"detail": "the fleet credential may send work only to ..."}),
                       httpx.Response(409, json={"ok": False, "code": "HUB_TOO_OLD", "needs_hub": "0.6.182",
                                                 "detail": "That Mac needs Studio Hub 0.6.182"})):  # old node
            got = startup_control.trash_list("ctl1", "node-1", client=self.client(answer))
            self.assertEqual((got["ok"], got["supported"], got["needs_hub"]), (True, False, "0.6.182"), answer)
            self.assertEqual(got["status"], "unknown", answer)

    def test_an_unreachable_mac_is_labelled_with_no_size(self) -> None:
        got = startup_control.trash_list("ctl1", "node-1", client=self.client(httpx.Response(
            502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "asleep"})))
        self.assertEqual((got["ok"], got["supported"], got["status"], got["machine"]),
                         (True, False, "unreachable", "node-1"))
        self.assertEqual(got["detail"], "asleep")
        self.assertNotIn("bytes", got)

    def test_only_a_fresh_reading_is_counted_in_a_fleet_total(self) -> None:
        now = time.time()
        fresh = startup_control.trash_list("ctl1", "node-1", client=self.client(httpx.Response(
            200, json={**LISTING, "measured_at": now})))
        stale = startup_control.trash_list("ctl1", "node-2", client=self.client(httpx.Response(
            200, json={**LISTING, "measured_at": now - startup_control.TRASH_STALE_S - 1, "bytes": 4096})))
        unreachable = startup_control.trash_list("ctl1", "node-3", client=self.client(httpx.Response(
            502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "asleep"})))
        unknown = startup_control.trash_list("ctl1", "node-4", client=self.client(httpx.Response(
            409, json={"ok": False, "code": "HUB_TOO_OLD", "needs_hub": "0.6.182"})))
        rows = [fresh, stale, unreachable, unknown]
        self.assertEqual([row["status"] for row in rows], ["fresh", "stale", "unreachable", "unknown"])
        self.assertEqual(fresh["measured_at"], now)                        # the Hub's own measurement time passes through
        self.assertAlmostEqual(stale["age_seconds"], startup_control.TRASH_STALE_S + 1, delta=1)
        # The fleet total sums fresh rows only; every other row is named and left unsized, never zeroed.
        self.assertEqual(sum(int(row.get("bytes") or 0) for row in rows if row["status"] == "fresh"), 8192)
        self.assertEqual(sum(1 for row in rows if row["status"] != "fresh"), 3)
        for row in rows[2:]:
            self.assertFalse(row["supported"])
            self.assertNotIn("bytes", row)

    def test_delete_sends_the_confirmed_count_and_bytes_and_passes_a_mismatch_through(self) -> None:
        changed = httpx.Response(409, json={"ok": False, "code": "TRASH_CHANGED", "count": 3, "bytes": 9})
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.trash_delete("ctl1", "node-1", "all", True, 2, 8192, client=self.client(changed))
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (409, "TRASH_CHANGED"))
        self.assertEqual(json.loads(self.seen[0].content),
                         {"items": "all", "confirm": True, "expected_count": 2, "expected_bytes": 8192})
        self.assertTrue(str(self.seen[0].url).endswith("/api/hub/fleet/nodes/node-1/trash/delete"))

    def test_a_malformed_request_is_refused_before_anything_is_sent(self) -> None:
        for args in (("all", False, 2, 1), ("all", True, None, 1), ("all", True, 2, "1"), ("all", True, True, 1),
                     ("everything", True, 1, 1), ([], True, 1, 1)):
            with self.assertRaises(startup_control.StartupError) as caught:
                startup_control.trash_delete("ctl1", "node-1", *args, client=self.client(httpx.Response(200)))
            self.assertEqual(caught.exception.status, 400, args)
        with self.assertRaises(startup_control.StartupError):
            startup_control.trash_list("ctl1", "../../etc", client=self.client(httpx.Response(200)))
        self.assertEqual(self.seen, [])


class EmptyTrashTests(_Isolated):
    """Empty trash on one Mac or every Mac at every linked site (owner, 2026-11-16; Studio Hub 0.6.243).

    The owner's one exception to "only the owner deletes": "one button to delete across all the linking
    sites and nodes and controllers ... make sure even agents control and make that happen too". The
    gateway key works here; every other delete stays owner-only (`OwnerConsoleTests`).
    """
    KEY = {"Authorization": "Bearer trash-fixture-key"}
    EMPTY = "/admin/api/fleet/trash/empty"
    EMPTIED = {"machine": "node-1", "label": "terranash-1500", "state": "emptied",
               "freed_bytes": 2_000, "deleted_count": 3}

    def setUp(self) -> None:
        super().setUp()
        self.audits: list[tuple[str, dict]] = []
        for target, name, fake in (
                (startup_control, "audit", lambda event, **fields: self.audits.append((event, fields))),
                (startup_control, "_target", lambda c: (f"http://{c}.fixture:47873", "fleet-token")),
                (fleet, "roster", lambda: [{"id": "ctl2"}, {"id": "ctl3"}]),
                (fleet, "invalidate", lambda: None)):
            patcher = patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def site(self, answers: dict[str, httpx.Response | Exception]) -> httpx.Client:
        """One fixture transport for every controller, answering by host."""
        self.seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            answer = answers[request.url.host.split(".")[0]]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Client(transport=httpx.MockTransport(handler))

    def run_route(self, host: str, headers: dict, body: dict, answers: dict) -> httpx.Response:
        client = self.site(answers)
        real_one, real_all = startup_control.empty_trash_one, startup_control.empty_trash_everywhere
        with patch.object(startup_control, "empty_trash_one",
                          lambda *a, **k: real_one(*a, client=client, **k)), \
             patch.object(startup_control, "empty_trash_everywhere",
                          lambda **k: real_all(client=client, **k)):
            api_client = TestClient(api.app, client=(host, 50000))
            self.addCleanup(api_client.close)
            return api_client.post(self.EMPTY, headers=headers, json=body)

    def run_lines(self) -> list[dict]:
        return [fields for event, fields in self.audits if event == "trash_empty"]

    def test_an_agent_holding_the_gateway_key_empties_every_mac_at_every_site(self) -> None:
        admin_session._owner_secret = SECRET            # the app is running; the agent does not hold it
        ok = httpx.Response(200, json={"ok": True, "machines": [self.EMPTIED]})
        answer = self.run_route("10.9.8.7", self.KEY, {}, {"local": ok, "ctl2": ok, "ctl3": ok})
        self.assertEqual(answer.status_code, 200, answer.text)
        got = answer.json()
        self.assertEqual((got["ok"], got["scope"], got["caller"]), (True, "all", "agent"))
        self.assertEqual([request.url.host for request in self.seen],
                         ["local.fixture", "ctl2.fixture", "ctl3.fixture"], "every linked site, 1301 first")
        self.assertTrue(all(request.url.path == "/api/hub/fleet/empty-trash" for request in self.seen))
        self.assertEqual(self.seen[0].headers["x-hub-token"], "fleet-token")
        self.assertEqual(got["totals"], {"freed_bytes": 6_000, "deleted_count": 9, "left_bytes": 0,
                                         "left_count": 0, "emptied": 3, "skipped": 0, "failed": 0})
        self.assertEqual(self.run_lines(), [{"caller": "agent", "scope": "all", "machines": 3,
                                             **got["totals"]}], "one audit line per run, naming the caller")

    def test_the_owners_app_window_is_named_as_the_owner(self) -> None:
        admin_session._owner_secret = SECRET
        ok = httpx.Response(200, json={"ok": True, "machines": [self.EMPTIED]})
        answer = self.run_route("127.0.0.1", {**self.KEY, admin_session.OWNER_HEADER: SECRET}, {"scope": "all"},
                                {"local": ok, "ctl2": ok, "ctl3": ok})
        self.assertEqual(answer.json()["caller"], "owner")
        self.assertEqual(self.run_lines()[0]["caller"], "owner")

    def test_no_credential_is_refused_and_nothing_is_sent(self) -> None:
        answer = self.run_route("10.9.8.7", {}, {}, {})
        self.assertEqual(answer.status_code, 401)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.run_lines(), [])

    def test_a_switch_off_mac_an_old_hub_and_an_offline_site_are_skipped_with_a_reason(self) -> None:
        local = httpx.Response(200, json={"ok": True, "machines": [
            self.EMPTIED,
            {"machine": "node-2", "state": "skipped", "reason": "its own switch is off"}]})
        answers = {"local": local,
                   "ctl2": httpx.Response(404, json={"detail": "Not Found"}),      # Hub older than 0.6.243
                   "ctl3": httpx.ConnectError("no route to host")}                 # offline
        got = self.run_route("10.9.8.7", self.KEY, {}, answers).json()
        rows = {(row["location_id"], row["machine"]): row for row in got["machines"]}
        self.assertEqual(rows[("local", "node-2")]["state"], "skipped")
        self.assertEqual(rows[("local", "node-2")]["reason"], "its own switch is off")
        self.assertEqual((rows[("ctl2", None)]["state"], rows[("ctl2", None)]["code"]), ("skipped", "HUB_TOO_OLD"))
        self.assertIn("0.6.243", rows[("ctl2", None)]["reason"])
        self.assertEqual((rows[("ctl3", None)]["state"], rows[("ctl3", None)]["code"]),
                         ("skipped", "CONTROLLER_UNREACHABLE"), "offline is skipped, never failed")
        self.assertTrue(rows[("ctl3", None)]["reason"])
        self.assertEqual((got["ok"], got["totals"]["skipped"], got["totals"]["failed"]), (True, 3, 0))
        self.assertIsNone(rows[("ctl3", None)]["freed_bytes"], "nothing attempted: not reported, never 0")

    def test_a_partial_failure_reports_exactly_what_is_left(self) -> None:
        partial = httpx.Response(500, json={"ok": False, "machines": [
            self.EMPTIED,
            {"machine": "node-3", "state": "failed", "reason": "Operation not permitted",
             "freed_bytes": 500, "deleted_count": 1, "left_bytes": 300, "left_count": 2}]})
        ok = httpx.Response(200, json={"ok": True, "machines": []})
        got = self.run_route("10.9.8.7", self.KEY, {}, {"local": partial, "ctl2": ok, "ctl3": ok}).json()
        failed = [row for row in got["machines"] if row["state"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["machine"], failed[0]["reason"], failed[0]["left_bytes"],
                          failed[0]["left_count"]), ("node-3", "Operation not permitted", 300, 2))
        self.assertFalse(got["ok"])
        self.assertEqual((got["totals"]["freed_bytes"], got["totals"]["left_bytes"], got["totals"]["failed"]),
                         (2_500, 300, 1))
        # A Hub that answered without naming a Mac is said, not a silent empty run.
        self.assertEqual([row["reason"] for row in got["machines"] if row["location_id"] == "ctl2"],
                         ["the Hub answered without naming a Mac"])

    def test_a_refused_credential_or_an_unknown_state_is_a_failure_not_a_skip(self) -> None:
        answers = {"local": httpx.Response(200, json={"ok": True, "machines": [
                       {"machine": "node-1", "state": "maybe", "freed_bytes": True}]}),
                   "ctl2": httpx.Response(401, json={"detail": "bad token"}),
                   "ctl3": httpx.Response(200, json={"ok": True, "machines": [self.EMPTIED]})}
        got = self.run_route("10.9.8.7", self.KEY, {}, answers).json()
        states = {(row["location_id"], row["state"]) for row in got["machines"]}
        self.assertEqual(states, {("local", "failed"), ("ctl2", "failed"), ("ctl3", "emptied")})
        unknown = next(row for row in got["machines"] if row["location_id"] == "local")
        self.assertIn("unknown state", unknown["reason"])
        self.assertIsNone(unknown["freed_bytes"], "a boolean is not a size")

    def test_one_mac_goes_to_its_node_route(self) -> None:
        ok = httpx.Response(200, json={"ok": True, "state": "emptied", "freed_bytes": 10, "deleted_count": 1})
        got = self.run_route("10.9.8.7", self.KEY, {"location_id": "ctl2", "machine": "node-7"},
                             {"ctl2": ok}).json()
        self.assertEqual([request.url.path for request in self.seen], ["/api/hub/fleet/nodes/node-7/empty-trash"])
        self.assertEqual((got["scope"], got["machines"][0]["machine"], got["machines"][0]["state"]),
                         ("machine", "node-7", "emptied"))
        self.assertEqual(self.run_lines()[0]["scope"], "machine")

    def test_a_malformed_request_is_refused_before_anything_is_sent(self) -> None:
        for body, code in (({"scope": "everything"}, "BAD_SCOPE"), ({"location_id": "ctl2"}, "MACHINE_REQUIRED"),
                           ({"location_id": "ctl2", "machine": "../../etc"}, "BAD_MACHINE")):
            answer = self.run_route("10.9.8.7", self.KEY, body, {})
            self.assertEqual((answer.status_code, answer.json()["code"]), (400, code), body)
        self.assertEqual(self.seen, [])

    def test_a_mac_row_states_its_macos_trash_or_none_never_zero(self) -> None:
        self.assertEqual(fleet._macos_trash_fact({"macos_trash": {"count": 4, "bytes": 1500}}),
                         {"count": 4, "bytes": 1500})
        self.assertEqual(fleet._macos_trash_fact({"macos_trash": {"hub_items_bytes": 7}}),
                         {"count": None, "bytes": 7}, "a first Hub cut's spelling is still read")
        self.assertIsNone(fleet._macos_trash_fact({}), "an older Hub states nothing: None, never 0")
        self.assertIsNone(fleet._macos_trash_fact({"macos_trash": {"count": True, "bytes": -1}}),
                          "a boolean or a negative is not a size")


class OwnerConsoleTests(_Isolated):
    KEY = {"Authorization": "Bearer trash-fixture-key"}
    LIST = "/admin/api/fleet/controllers/ctl1/machines/node-1/trash"
    MOVE = LIST + "/move-to-macos-trash"
    DELETE = LIST + "/delete"
    BODY = {"items": "all", "confirm": True, "expected_count": 2, "expected_bytes": 8192}

    def setUp(self) -> None:
        super().setUp()
        self.deleted: list[tuple] = []
        for name, fake in (("trash_list", lambda *a, **k: {**LISTING, "supported": True}),
                           ("trash_move", lambda *a, **k: {"ok": True, "moved_count": 2, "failed": []}),
                           ("trash_delete", lambda *a, **k: self.deleted.append(a) or {"ok": True, "failed": []})):
            patcher = patch.object(startup_control, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, host: str) -> TestClient:
        client = TestClient(api.app, client=(host, 50000))
        self.addCleanup(client.close)
        return client

    def test_listing_and_move_use_the_normal_admin_auth(self) -> None:
        remote = self.client("10.9.8.7")
        self.assertEqual(remote.get(self.LIST).status_code, 401)
        self.assertEqual(remote.get(self.LIST, headers=self.KEY).json()["bytes"], 8192)
        self.assertEqual(remote.post(self.MOVE, headers=self.KEY, json={"items": "all"}).status_code, 200)

    def test_the_api_key_alone_cannot_delete_permanently(self) -> None:
        admin_session._owner_secret = SECRET                       # the app is running and holds it
        for host in ("10.9.8.7", "127.0.0.1"):
            answer = self.client(host).post(self.DELETE, headers=self.KEY, json=self.BODY)
            self.assertEqual(answer.status_code, 403, host)
            self.assertIn("Gateway app", answer.json()["detail"])
        self.assertEqual(self.deleted, [])

    def test_loopback_trust_or_a_browser_session_alone_cannot_delete(self) -> None:
        admin_session._owner_secret = SECRET
        local = self.client("127.0.0.1")
        self.assertEqual(local.post(self.DELETE, json=self.BODY).status_code, 403)
        token = admin_session.create_session()
        session = local.post(self.DELETE, json=self.BODY, cookies={admin_session.COOKIE: token},
                             headers={"Origin": "http://testserver"})
        self.assertEqual(session.status_code, 403)
        wrong = local.post(self.DELETE, json=self.BODY, headers={**self.KEY, admin_session.OWNER_HEADER: "x" * 64})
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.deleted, [])

    def test_the_owners_app_window_deletes(self) -> None:
        admin_session._owner_secret = SECRET
        owner = {**self.KEY, admin_session.OWNER_HEADER: SECRET}
        answer = self.client("127.0.0.1").post(self.DELETE, headers=owner, json=self.BODY)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(self.deleted, [("ctl1", "node-1", "all", True, 2, 8192)])

    def test_the_secret_is_refused_off_this_mac_and_when_the_gateway_never_got_one(self) -> None:
        admin_session._owner_secret = SECRET
        owner = {**self.KEY, admin_session.OWNER_HEADER: SECRET}
        self.assertEqual(self.client("10.9.8.7").post(self.DELETE, headers=owner, json=self.BODY).status_code, 403)
        admin_session._owner_secret = ""                           # a gateway the app adopted, not spawned
        self.assertEqual(self.client("127.0.0.1").post(
            self.DELETE, headers={**self.KEY, admin_session.OWNER_HEADER: ""}, json=self.BODY).status_code, 403)
        self.assertEqual(self.deleted, [])

    def test_the_secret_arrives_on_stdin_and_a_short_one_is_ignored(self) -> None:
        with patch.object(sys, "stdin", io.StringIO("short\n")):
            admin_session._read_owner_secret()
        self.assertFalse(admin_session.owner_console("short"))
        with patch.object(sys, "stdin", io.StringIO(SECRET + "\n")):
            admin_session._read_owner_secret()
        self.assertTrue(admin_session.owner_console(SECRET))
        self.assertFalse(admin_session.owner_console(None))

    def test_a_non_ascii_header_is_a_403_not_a_500(self) -> None:
        admin_session._owner_secret = SECRET
        self.assertFalse(admin_session.owner_console("caf\u00e9" * 10))
        for value in ("caf\u00e9".encode("utf-8"), "caf\u00e9".encode("latin-1")):
            headers = {**self.KEY, admin_session.OWNER_HEADER: value}
            self.assertEqual(self.client("127.0.0.1").post(self.DELETE, headers=headers, json=self.BODY).status_code, 403)
        self.assertEqual(self.deleted, [])


if __name__ == "__main__":
    unittest.main()
