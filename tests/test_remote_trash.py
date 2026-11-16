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


class MacosHubTrashTests(_Isolated):
    """The items the Hub moved into macOS Trash (Studio Hub 0.6.243): owner-only, same re-check.

    Owner, 2026-11-16: the cleanup must actually free space, including items already moved to macOS
    Trash. The Hub states the count and bytes per Mac and offers a route that deletes **only** what it
    put there; the Gateway relays it behind the same owner app-window secret as the Trash Folder delete.
    """
    #: The real relay, kept before `setUp` replaces the module attribute for the route tests.
    REAL = staticmethod(startup_control.macos_trash_delete)
    KEY = {"Authorization": "Bearer trash-fixture-key"}
    DELETE = "/admin/api/fleet/controllers/ctl1/machines/node-1/macos-trash/delete"
    BODY = {"confirm": True, "expected_count": 4, "expected_bytes": 1_500_000_000}

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple] = []
        patcher = patch.object(startup_control, "macos_trash_delete",
                               lambda *a, **k: self.calls.append(a) or {"ok": True, "deleted_count": 4,
                                                                        "freed_bytes": 1_500_000_000})
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, host: str):
        client = TestClient(api.app, client=(host, 50000))
        self.addCleanup(client.close)
        return client

    def test_only_the_owner_app_window_can_use_it(self) -> None:
        admin_session._owner_secret = SECRET
        for host in ("10.9.8.7", "127.0.0.1"):
            answer = self.client(host).post(self.DELETE, headers=self.KEY, json=self.BODY)
            self.assertEqual(answer.status_code, 403, host)
        local = self.client("127.0.0.1")
        self.assertEqual(local.post(self.DELETE, json=self.BODY).status_code, 403)
        token = admin_session.create_session()
        self.assertEqual(local.post(self.DELETE, json=self.BODY, cookies={admin_session.COOKIE: token},
                                    headers={"Origin": "http://testserver"}).status_code, 403)
        self.assertEqual(self.calls, [], "a gateway key or a browser session cannot reach this delete")
        owner = {**self.KEY, admin_session.OWNER_HEADER: SECRET}
        self.assertEqual(local.post(self.DELETE, headers=owner, json=self.BODY).status_code, 200)
        self.assertEqual(self.calls, [("ctl1", "node-1", True, 4, 1_500_000_000)])

    def test_the_relay_sends_only_the_confirmed_count_and_bytes(self) -> None:
        seen: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200, json={"ok": True, "deleted_count": 4})))
        with patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token")):
            got = self.REAL("ctl1", "node-1", True, 4, 1_500_000_000, client=client)
        self.assertEqual(got["deleted_count"], 4)
        self.assertTrue(str(seen[0].url).endswith("/api/hub/fleet/nodes/node-1/macos-trash/delete"))
        self.assertEqual(json.loads(seen[0].content),
                         {"confirm": True, "expected_count": 4, "expected_bytes": 1_500_000_000},
                         "no item ids: the Hub deletes only what it moved there")

    def test_a_changed_folder_is_a_409_that_deletes_nothing(self) -> None:
        answer = httpx.Response(409, json={"ok": False, "code": "TRASH_CHANGED", "count": 5, "bytes": 2})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: answer))
        with patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "t")):
            with self.assertRaises(startup_control.StartupError) as caught:
                self.REAL("ctl1", "node-1", True, 4, 1_500_000_000, client=client)
        self.assertEqual((caught.exception.status, caught.exception.body["code"]), (409, "TRASH_CHANGED"))

    def test_a_malformed_request_is_refused_before_anything_is_sent(self) -> None:
        seen: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: seen.append(request) or httpx.Response(200)))
        for args in ((False, 4, 1), (True, None, 1), (True, 4, "1"), (True, True, 1)):
            with self.assertRaises(startup_control.StartupError) as caught:
                self.REAL("ctl1", "node-1", *args, client=client)
            self.assertEqual(caught.exception.status, 400, args)
        self.assertEqual(seen, [])

    def test_a_mac_row_states_the_hub_items_or_none_never_zero(self) -> None:
        self.assertEqual(fleet._macos_trash_fact(
            {"macos_trash": {"hub_items_count": 4, "hub_items_bytes": 1500}}),
            {"hub_items_count": 4, "hub_items_bytes": 1500})
        self.assertEqual(fleet._macos_trash_fact({"macos_trash_hub_items_bytes": 7}),
                         {"hub_items_count": None, "hub_items_bytes": 7})
        self.assertIsNone(fleet._macos_trash_fact({}), "an older Hub states nothing: None, never 0")
        self.assertIsNone(fleet._macos_trash_fact(
            {"macos_trash": {"hub_items_count": True, "hub_items_bytes": True}}),
            "a boolean is not a size")


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
