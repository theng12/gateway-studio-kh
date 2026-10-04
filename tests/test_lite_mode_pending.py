#!/usr/bin/env python3
"""A Lite Mode change for an offline Mac waits on the Gateway and is applied when the Mac is back (owner, 2026-10-04).

"Can a Lite Mode setting be applied while the Mac is offline, and when it's back online it accepts the new
setting?" The relay that cannot reach the Mac keeps the change (one per Mac, merged field by field, on disk);
the fleet refresher sends it once the Mac answers again. Mocked Hub transport and a temp data dir only.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()  # never bind to the owner's real Application Support

import httpx  # noqa: E402

from gateway import config, fleet, startup_control  # noqa: E402

OFFLINE = httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "reason": "unreachable",
                                    "detail": "that Mac did not answer, so its settings did not change"})


def _machine(state: str = "ready", reachable: bool = True) -> dict:
    return {"location_id": "ctl1", "location": "Shops", "location_reachable": True, "machine": "shop-2",
            "label": "Shop 2", "reachable": reachable, "state": state, "node_last_seen_at": 1000.0}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(tmp.name)),
                        patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "t")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.seen: list[dict] = []
        self.answer = OFFLINE

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(json.loads(request.content))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        return httpx.Client(transport=httpx.MockTransport(handler))

    def set(self, settings: dict):
        try:
            return startup_control.set_node_workstation("ctl1", "shop-2", settings, client=self.client())
        except startup_control.StartupError as error:
            return error

    def pending(self) -> dict:
        return startup_control.pending_lite_mode().get("ctl1/shop-2") or {}

    def events(self) -> list[str]:
        return [row["event"] for row in startup_control.recent_audit(50)]


class SaveTests(_Case):
    def test_an_offline_mac_keeps_the_change_and_the_answer_is_ok_and_pending(self) -> None:
        answer = self.set({"enabled": True, "idle_minutes": 5})
        self.assertEqual((answer["ok"], answer["pending"]), (True, True))
        self.assertEqual(answer["detail"], startup_control.PENDING_SENTENCE)
        self.assertEqual(self.pending()["settings"], {"enabled": True, "idle_minutes": 5})
        self.assertIn("workstation_pending_saved", self.events())

    def test_an_unreachable_controller_or_a_timeout_is_pending_too(self) -> None:
        for failure in (httpx.ConnectError("down"), httpx.ReadTimeout("slow")):
            self.answer = failure
            self.assertTrue(self.set({"enabled": False})["pending"])

    def test_a_newer_change_merges_over_the_older_one_field_by_field(self) -> None:
        self.set({"enabled": True, "idle_minutes": 5})
        since = self.pending()["since"]
        self.set({"idle_minutes": 9, "quiet_hours": ["22:00-07:00"]})
        self.assertEqual(self.pending()["settings"], {"enabled": True, "idle_minutes": 9, "quiet_hours": ["22:00-07:00"]})
        self.assertEqual(self.pending()["since"], since, "offline since the first change, not the last")
        self.assertEqual(len(startup_control.pending_lite_mode()), 1, "one pending change per Mac")

    def test_it_survives_a_gateway_restart(self) -> None:
        self.set({"enabled": True})
        on_disk = json.loads((config.DATA_DIR / "lite_mode_pending.json").read_text())
        self.assertEqual(on_disk["ctl1/shop-2"]["settings"], {"enabled": True})
        self.assertEqual((config.DATA_DIR / "lite_mode_pending.json").stat().st_mode & 0o777, 0o600)

    def test_the_online_path_is_unchanged(self) -> None:
        self.answer = httpx.Response(200, json={"ok": True, "node": "shop-2", "workstation": {"enabled": True}})
        answer = self.set({"enabled": True})
        self.assertEqual(answer, {"ok": True, "node": "shop-2", "workstation": {"enabled": True}, "machine": "shop-2"})
        self.assertEqual(self.seen, [{"enabled": True}])
        self.assertEqual(startup_control.pending_lite_mode(), {})
        self.answer = httpx.Response(400, json={"ok": False, "code": "BAD_SETTING", "detail": "too long"})
        self.assertEqual(self.set({"idle_minutes": 99}).body["code"], "BAD_SETTING", "a refusal is still a refusal")

    def test_a_direct_change_once_back_carries_and_clears_what_waited(self) -> None:
        self.set({"enabled": True, "idle_minutes": 5})
        self.answer = httpx.Response(200, json={"ok": True})
        self.set({"idle_minutes": 7})
        self.assertEqual(self.seen[-1], {"enabled": True, "idle_minutes": 7})
        self.assertEqual(startup_control.pending_lite_mode(), {})


class ApplyTests(_Case):
    def apply(self, row: dict | None = None) -> None:
        startup_control.apply_pending_lite_mode([row or _machine()], client=self.client())

    def test_the_refresher_sends_it_once_the_mac_is_back_and_clears_it(self) -> None:
        self.set({"enabled": True, "idle_minutes": 5})
        self.seen.clear()
        self.apply(_machine(state="offline", reachable=False))
        self.assertEqual(self.seen, [], "not tried while the Fleet sees it offline")
        self.answer = httpx.Response(200, json={"ok": True})
        self.apply()
        self.assertEqual(self.seen, [{"enabled": True, "idle_minutes": 5}])
        self.assertEqual(startup_control.pending_lite_mode(), {})
        self.assertIn("workstation_pending_applied", self.events())
        self.apply()
        self.assertEqual(len(self.seen), 1, "sent once")

    def test_another_reach_failure_keeps_it(self) -> None:
        self.set({"enabled": True})
        self.apply()
        self.answer = httpx.ConnectError("down")
        self.apply()
        self.assertEqual(self.pending()["settings"], {"enabled": True})

    def test_a_refused_value_is_cleared_and_shown_as_a_fleet_issue(self) -> None:
        self.set({"idle_minutes": 99})
        self.answer = httpx.Response(400, json={"ok": False, "code": "BAD_SETTING",
                                                "detail": "idle_minutes must be 1 to 60"})
        self.apply()
        self.assertNotIn("settings", self.pending())
        self.assertIn("workstation_pending_refused", self.events())
        row = _machine()
        issues = startup_control.stamp_lite_mode_pending([row])
        self.assertIsNone(row["lite_mode_pending"])
        self.assertEqual([(i["code"], i["severity"], i["subject"]) for i in issues],
                         [("lite_mode_refused", "warning", "Shop 2")])
        self.assertIn("idle_minutes must be 1 to 60", issues[0]["message"])
        self.seen.clear()
        self.apply()
        self.assertEqual(self.seen, [], "a refused change is never retried")
        self.answer = httpx.Response(200, json={"ok": True})
        self.set({"idle_minutes": 5})
        self.assertEqual(startup_control.stamp_lite_mode_pending([_machine()]), [], "the next change clears the issue")

    def test_the_row_carries_the_pending_change_and_since_when(self) -> None:
        self.set({"enabled": True})
        row = _machine(state="offline", reachable=False)
        self.assertEqual(startup_control.stamp_lite_mode_pending([row]), [])
        self.assertEqual(row["lite_mode_pending"]["settings"], {"enabled": True})
        self.assertEqual(row["lite_mode_pending"]["offline_since"], 1000.0)


if __name__ == "__main__":
    unittest.main()
