#!/usr/bin/env python3
"""A Lite Mode Mac that is switched off is expected, not a fault (owner, 2026-10-08).

"it's the shop mac so it's supposed to be offline. it shouldn't trigger anything." The two shop iMacs are on by
day and off at night and on holiday. Off, they report nothing, so their Lite Mode is the last one reported or a
change waiting for them. Such a Mac raises no warning, counts in no "offline" number and no group's attention;
other offline Macs warn exactly as before. Temp data dir and mocked Hub transport only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-lite-offline-")

import httpx  # noqa: E402

from gateway import config, fleet, notifier, startup_control  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHOP = ("kt-pdt-imac-m1-2021-hub", "kt-pps-m1-imac-256-red-hub")
NODE_UNREACHABLE = httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "reason": "unreachable",
                                             "detail": "that Mac did not answer, so its settings did not change"})


def _row(seen_at: float | None = None) -> dict:
    """The Shops controller as its Hub reports it: its own Mac ready, both shop iMacs switched off."""
    seen_at = time.time() - 6 * 3600 if seen_at is None else seen_at
    own = {"machine": "local", "reachable": True, "health": "up",
           "workstation": {"enabled": False, "state": "off"}}
    shops = [{"machine": mid, "reachable": False, "health": "unreachable"} for mid in SHOP]
    return {
        "id": "shops", "label": "Shops", "local": False, "reachable": True, "url": "http://100.64.0.9:47873",
        "machines": [own, *shops],
        "nodes": [{"id": mid, "name": mid, "state": "unreachable", "reason": "unreachable",
                   "detail": "could not reach the node (unreachable)", "last_seen_at": seen_at} for mid in SHOP],
        "activity": {"machines": [{"machine": "local", "state": "ready"},
                                  *({"machine": mid, "state": "offline"} for mid in SHOP)], "pulse": {}},
        "workers": [{"id": f"{mid}-{studio}", "machine": mid, "modality": studio, "online": False}
                    for mid in SHOP for studio in ("image", "tts")],
        "running_jobs": [], "local_models": [], "probe_machines_hidden": 0,
    }


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patcher in (patch.object(config, "DATA_DIR", self.dir),
                        patch.object(fleet, "LITE_MODE_KNOWN_PATH", self.dir / "lite_mode_known.json"),
                        patch.object(startup_control, "_target", lambda c: ("http://100.64.0.9:47873", "t")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def known(self, **values: bool) -> None:
        fleet.LITE_MODE_KNOWN_PATH.write_text(json.dumps({f"shops/{k}": v for k, v in values.items()}))

    def both_known_on(self) -> None:
        fleet.LITE_MODE_KNOWN_PATH.write_text(json.dumps({f"shops/{mid}": True for mid in SHOP}))

    def save_offline(self, machine: str, settings: dict) -> dict:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: NODE_UNREACHABLE))
        return startup_control.set_node_workstation("shops", machine, settings, client=client)

    @staticmethod
    def codes(row: dict) -> list[tuple[str, str, str]]:
        return [(i["severity"], i["code"], i["subject"]) for i in fleet._issues([row])]

    @staticmethod
    def flat(row: dict) -> dict[str, dict]:
        return {m["machine"]: m for m in fleet.machines_flat([row])}


class GatewayRuleTests(_Case):
    def test_a_switched_off_lite_mode_mac_raises_nothing_and_counts_in_no_offline_number(self) -> None:
        self.both_known_on()
        row = _row()
        self.assertEqual(self.codes(row), [], "no machine_offline, and no worker line for its studios either")
        flat = self.flat(row)
        self.assertTrue(all(flat[mid]["lite_off"] for mid in SHOP))
        self.assertFalse(flat["local"]["lite_off"])
        self.assertEqual(flat[SHOP[0]]["state"], "offline", "the Hub's word is kept: it is not reachable")
        summary = fleet._summarise([row])
        self.assertEqual((summary["machines_online"], summary["machines_offline"], summary["machines_off_lite"]),
                         (1, 0, 2))

    def test_a_waiting_enable_counts_at_once_and_a_waiting_disable_wins_over_the_last_report(self) -> None:
        answer = self.save_offline(SHOP[0], {"enabled": True, "idle_only": True})
        self.assertTrue(answer["pending"], "the offline path keeps the change")
        flat = self.flat(_row())
        self.assertTrue(flat[SHOP[0]]["lite_off"], "pending counts before the Mac is ever back")
        self.assertFalse(flat[SHOP[1]]["lite_off"])
        self.assertEqual(self.codes(_row()), [("warning", "machine_offline", SHOP[1]),
                                              ("info", "worker_offline", "2 workers")])
        self.known(**{SHOP[1]: True})
        self.save_offline(SHOP[1], {"enabled": False})
        self.assertFalse(self.flat(_row())[SHOP[1]]["lite_off"], "the owner's latest word is the waiting one")

    def test_a_mac_not_in_lite_mode_still_warns_exactly_as_before(self) -> None:
        self.known(**{SHOP[0]: False})
        row = _row()
        issues = fleet._issues([row])
        self.assertEqual([(i["severity"], i["code"]) for i in issues],
                         [("warning", "machine_offline"), ("warning", "machine_offline"), ("info", "worker_offline")])
        self.assertEqual(issues[0]["action"], "Check that Mac is switched on and awake")
        self.assertEqual(fleet._summarise([row])["machines_offline"], 2)
        self.assertFalse(any(m["lite_off"] for m in self.flat(row).values()))

    def test_off_for_more_than_fourteen_days_is_one_quiet_info_line(self) -> None:
        self.both_known_on()
        self.assertEqual(self.codes(_row(seen_at=time.time() - 13 * 86400)), [])
        issues = fleet._issues([_row(seen_at=time.time() - 16 * 86400)])
        self.assertEqual([(i["severity"], i["code"]) for i in issues], [("info", "lite_mode_long_off")] * 2)
        self.assertIn("has been switched off for 16 days", issues[0]["message"])

    def test_the_last_report_is_kept_while_the_mac_is_off(self) -> None:
        on = {"location_id": "shops", "machine": SHOP[0], "workstation": {"enabled": True, "state": "working"}}
        fleet.remember_lite_mode([on, {"location_id": "shops", "machine": SHOP[1], "workstation": None}])
        self.assertEqual(fleet._lite_mode_known(), {f"shops/{SHOP[0]}": True})
        fleet.remember_lite_mode([{"location_id": "shops", "machine": SHOP[0], "workstation": None}])
        self.assertEqual(fleet._lite_mode_known(), {f"shops/{SHOP[0]}": True}, "silence never forgets it")
        self.assertTrue(self.flat(_row())[SHOP[0]]["lite_off"])
        fleet.remember_lite_mode([{**on, "workstation": {"enabled": False}}])
        self.assertFalse(self.flat(_row())[SHOP[0]]["lite_off"], "turned off on the Mac itself: it warns again")

    def test_telegram_raises_no_offline_alarm_for_it(self) -> None:
        self.both_known_on()
        snap = {"locations": [_row()], "machines": fleet.machines_flat([_row()])}
        self.assertEqual(notifier.Notifier.offline_conditions(object.__new__(notifier.Notifier), snap), [])
        self.known(**{SHOP[0]: False, SHOP[1]: False})
        snap["machines"] = fleet.machines_flat([_row()])
        self.assertEqual(len(notifier.Notifier.offline_conditions(object.__new__(notifier.Notifier), snap)), 2)


SWIFT = r'''
import AppKit

enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct LiteOffCheck {
    static func main() {
        _ = NSApplication.shared
        func expect(_ ok: Bool, _ what: String) { if !ok { print("FAIL \(what)"); exit(1) } }
        for name in ["quiet", "loud"] {
            let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1] + "/\(name).json"))
            let snapshot = FleetSnapshot.from(try! JSONSerialization.jsonObject(with: data) as! [String: Any])!
            let group = snapshot.groups.first { $0.name == "Shops" }!
            let shop = group.machines.first { $0.machine == "kt-pdt-imac-m1-2021-hub" }!
            if name == "quiet" {
                expect(group.health == .ok && !group.needsLook, "a group whose only absent Macs are Lite Mode is healthy")
                expect(group.stateWord == "Healthy", "chip: \(group.stateWord)")
                expect(group.problems.isEmpty, "problems: \(group.problems)")
                expect(group.online == 1 && group.offLite == 2, "online \(group.online), off \(group.offLite)")
                expect(shop.stateLabel == "Off" && shop.badgeHealth == .off, "badge: \(shop.stateLabel)")
                expect(FleetOffline.why(shop) == FleetOffline.liteOffNote, "note: \(FleetOffline.why(shop) ?? "nil")")
                expect(FleetWarnings.group(issues: group.issues).isEmpty, "no banner line")
            } else {
                expect(group.needsLook && group.stateWord == "Attention", "a Mac not in Lite Mode still needs a look")
                expect(group.problems == ["2 nodes offline"], "problems: \(group.problems)")
                expect(shop.stateLabel == "Offline" && shop.badgeHealth == .bad, "badge: \(shop.stateLabel)")
            }
        }
        print("PASS: switched-off Lite Mode Macs read Off, grey, and never as attention")
    }
}
'''


class SwiftGroupTests(_Case):
    def snapshot(self) -> dict:
        row = _row()
        return {"generated_at": 1, "locations": [row], "machines": fleet.machines_flat([row]),
                "issues": fleet._issues([row]), "summary": fleet._summarise([row]), "running": []}

    def test_the_groups_page_reads_the_gateways_rows_with_the_same_rule(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-lite-off-swift-") as directory:
            work = Path(directory)
            (work / "loud.json").write_text(json.dumps(self.snapshot()))
            self.both_known_on()
            (work / "quiet.json").write_text(json.dumps(self.snapshot()))
            (work / "check.swift").write_text(SWIFT)
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift",
                 "menubar/FleetLastStop.swift", "menubar/FleetWorkstation.swift",
                 "menubar/FleetMacTrashModel.swift", "menubar/FleetLiteMode.swift", "menubar/FleetTabs.swift",
                 "menubar/FleetGroups.swift", "menubar/SecretField.swift", "menubar/FleetBanner.swift",
                 str(work / "check.swift"), "-o", str(work / "check")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(work / "check"), directory], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
