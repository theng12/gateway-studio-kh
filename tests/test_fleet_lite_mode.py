"""Each Mac's Lite Mode, as a plain column on the Fleet page (owner request 2026-10-19).

The Machines view never showed it: the owner could see whether a Mac starts its services and whether it
answers, but not whether it lends the fleet its idle time. Every field this column reads already exists
on the machine row - `workstation` (the Hub's own block), `lite_off` (the Gateway's verdict for a Mac
switched off *in* Lite Mode) and `lite_mode_pending` (a change held for a Mac that was offline). No Hub
change, no new field, and **nothing is toggled**: the Lite Mode sheet is already on the Mac's "…" menu.

What these tests pin: the four states read apart (On / Off / a change on its way / Unknown), `lite_off`
counts as On rather than Unknown, and a waiting change that leaves the mode alone is not called a change
of mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-lite-mode-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import fleet  # noqa: E402


def _row(name: str, **fields) -> dict:
    """One machine row as the Hub's overview carries it."""
    return {"machine": name, "label": name, **fields}


def _rows(machines: list[dict], states: dict[str, str] | None = None) -> dict[str, dict]:
    """Rows straight out of `fleet.machines_flat`, so these are the fields the page really gets."""
    states = states or {}
    location = {"id": "ctl1", "label": "Site A", "local": False, "reachable": True,
                "machines": machines, "nodes": [],
                "activity": {"machines": [{"machine": row["machine"],
                                           "state": states.get(row["machine"], "ready")} for row in machines],
                             "pulse": {}},
                "running_jobs": [], "local_models": [], "workers": [], "probe_machines_hidden": 0}
    return {row["machine"]: row for row in fleet.machines_flat([location])}


class FieldTests(unittest.TestCase):
    """The three fields the column reads are all already on the row - nothing new is asked of the Hub."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-lite-mode-case-")
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(fleet, "LITE_MODE_KNOWN_PATH", Path(self.tmp.name) / "lite_mode_known.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_row_carries_the_lite_mode_block_and_the_gateways_own_verdict(self) -> None:
        rows = _rows([
            _row("on", workstation={"enabled": True, "state": "standing_by"}),
            _row("off", workstation={"enabled": False}),
            _row("quiet", workstation={"enabled": True, "state": "quiet_hours"}),
        ])
        self.assertIs(rows["on"]["workstation"]["enabled"], True)
        self.assertIs(rows["off"]["workstation"]["enabled"], False)
        # `lite_off` is only ever true for an offline Mac, so these reachable ones are all False.
        self.assertEqual([rows[name]["lite_off"] for name in ("on", "off", "quiet")], [False, False, False])

    def test_an_offline_mac_last_known_to_be_in_lite_mode_reads_lite_off(self) -> None:
        """The Hub is not there to repeat it, so the Gateway's own verdict is the only word - and it is On."""
        rows = _rows([_row("asleep", workstation={"enabled": True, "stale": True})],
                     states={"asleep": "offline"})
        self.assertIs(rows["asleep"]["lite_off"], True)
        # And one that was last known to be out of Lite Mode is not.
        rows = _rows([_row("awake", workstation={"enabled": False, "stale": True})],
                     states={"awake": "offline"})
        self.assertIs(rows["awake"]["lite_off"], False)

    def test_a_change_held_for_an_offline_mac_lands_on_its_row(self) -> None:
        from gateway import startup_control
        with patch.object(startup_control, "pending_lite_mode",
                          lambda: {"ctl1/mac-3": {"settings": {"enabled": True, "idle_minutes": 10},
                                                  "at": 1.0, "since": 2.0}}):
            machines = [{"location_id": "ctl1", "machine": "mac-3", "label": "Mac 3"}]
            startup_control.stamp_lite_mode_pending(machines)
        self.assertEqual(machines[0]["lite_mode_pending"]["settings"]["enabled"], True)
        self.assertEqual(machines[0]["lite_mode_pending"]["offline_since"], 2.0)

    def test_a_mac_with_no_waiting_change_gets_none(self) -> None:
        from gateway import startup_control
        with patch.object(startup_control, "pending_lite_mode", lambda: {}):
            machines = [{"location_id": "ctl1", "machine": "mac-4"}]
            startup_control.stamp_lite_mode_pending(machines)
        self.assertIsNone(machines[0]["lite_mode_pending"])


class LabelTests(unittest.TestCase):
    """The label mapping itself, compiled and run the way the page runs it."""

    SWIFT = r'''
import AppKit

// FleetModels.swift reaches the network for one relative-time string; the check is offline.
enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct LiteModeCheck {
    static func main() {
        let dir = CommandLine.arguments[1]
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let rows = try! decoder.decode([String: FleetMachine].self,
                                       from: try! Data(contentsOf: URL(fileURLWithPath: dir + "/rows.json")))
        func expect(_ actual: String, _ wanted: String, _ what: String) {
            if actual != wanted { print("FAIL \(what): got \(actual), wanted \(wanted)"); exit(1) }
        }
        func label(_ id: String) -> String { FleetLiteMode.state(rows[id]!).text }

        // The four states the owner asked for.
        expect(label("on"), "On", "Lite Mode on")
        expect(label("off"), "Off", "Lite Mode off")
        expect(label("unknown"), "Unknown", "a Hub too old for Lite Mode says nothing - unknown, not off")

        // A Mac switched off in Lite Mode: its Hub is not there to repeat it, so the Gateway's own
        // verdict is the only word - and it means On, not Unknown.
        expect(label("liteoff"), "On", "switched off in Lite Mode still reads On")

        // A change on its way. It wins over the last report, because it is the owner's latest word.
        expect(label("turningon"), "Turning on…", "off now, a change to turn it on is waiting")
        expect(label("turningoff"), "Turning off…", "on now, a change to turn it off is waiting")
        expect(label("turningon-unknown"), "Turning on…", "never reported, a change to turn it on is waiting")

        // A waiting change that leaves the mode alone is NOT a change of mode.
        expect(label("waiting-on"), "Change waiting", "already on, the waiting change keeps it on")
        expect(label("waiting-off"), "Change waiting", "already off, the waiting change keeps it off")
        expect(label("waiting-silent"), "Change waiting", "a waiting change that does not state the mode")

        // Colours: on is green, a waiting change is worth a look, and nothing here is an alarm.
        if FleetLiteMode.state(rows["on"]!).health != .ok { print("FAIL: On is not green"); exit(1) }
        if FleetLiteMode.state(rows["off"]!).health != .off { print("FAIL: Off is not grey"); exit(1) }
        if FleetLiteMode.state(rows["unknown"]!).health != .off { print("FAIL: Unknown is not grey"); exit(1) }
        if FleetLiteMode.state(rows["turningon"]!).health != .warn { print("FAIL: a waiting change is not amber"); exit(1) }

        // The tooltip says why, in words: which of the two ways it is unknown, and what is waiting.
        expect(String(FleetLiteMode.tooltip(rows["unknown"]!, name: "Shop 3").contains("too old")), "true",
               "unknown says why")
        expect(String(FleetLiteMode.tooltip(rows["liteoff"]!, name: "Shop 3").contains("switched off")), "true",
               "a switched-off Mac says so rather than looking like a fault")
        expect(String(FleetLiteMode.tooltip(rows["turningon"]!, name: "Shop 3").contains("Waiting to apply")), "true",
               "a waiting change quotes when it was saved")
        expect(FleetLiteMode.tooltip(nil, name: "Shop 3"), "Shop 3 has no row to read.",
               "no row, no claim")
        print("PASS: Lite Mode reads on, off, on its way and unknown apart")
    }
}
'''

    ROWS = {
        "on": {"machine": "on", "reachable": True, "workstation": {"enabled": True, "state": "standing_by"}},
        "off": {"machine": "off", "reachable": True, "workstation": {"enabled": False}},
        "unknown": {"machine": "unknown", "reachable": True},
        "liteoff": {"machine": "liteoff", "reachable": False, "state": "offline", "lite_off": True},
        "turningon": {"machine": "turningon", "reachable": False, "state": "offline",
                      "workstation": {"enabled": False, "stale": True},
                      "lite_mode_pending": {"settings": {"enabled": True}, "at": 1.0, "offline_since": 2.0}},
        "turningoff": {"machine": "turningoff", "reachable": True, "workstation": {"enabled": True},
                       "lite_mode_pending": {"settings": {"enabled": False}, "at": 1.0}},
        "turningon-unknown": {"machine": "turningon-unknown", "reachable": False, "state": "offline",
                              "lite_mode_pending": {"settings": {"enabled": True}, "at": 1.0, "offline_since": 2.0}},
        "waiting-on": {"machine": "waiting-on", "reachable": True, "workstation": {"enabled": True},
                       "lite_mode_pending": {"settings": {"enabled": True, "idle_minutes": 10}, "at": 1.0}},
        "waiting-off": {"machine": "waiting-off", "reachable": True, "workstation": {"enabled": False},
                        "lite_mode_pending": {"settings": {"enabled": False, "idle_minutes": 10}, "at": 1.0}},
        "waiting-silent": {"machine": "waiting-silent", "reachable": True, "workstation": {"enabled": False},
                           "lite_mode_pending": {"settings": {"idle_minutes": 10}, "at": 1.0}},
    }

    def test_the_column_tells_the_four_states_apart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-lite-mode-swift-") as directory:
            work = Path(directory)
            (work / "check.swift").write_text(self.SWIFT)
            (work / "rows.json").write_text(json.dumps(self.ROWS))
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit", "menubar/PageKit.swift",
                 "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift",
                 "menubar/FleetWorkstation.swift", "menubar/FleetMacTrashModel.swift", "menubar/FleetLiteMode.swift",
                 str(work / "check.swift"), "-o", str(work / "check")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(work / "check"), directory], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
