#!/usr/bin/env python3
"""Fleet warnings: machine labels and the hide-a-kind setting (owner 2026-10-02).

A controller's own Mac comes through the Hub labelled "This Mac"; only the Gateway's own Mac may be
called that. The Hub's disk warnings are classified by kind so the app can group them, and the two
non-space kinds (macOS automatic updates, Time Machine) can be hidden while disk space never can.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-fleet-banner-")

from gateway import fleet  # noqa: E402

UPDATE = ("macOS is set to download updates automatically (8-15 GB each). Turn it off in System Settings > "
          "General > Software Update > Automatic Updates > Download new updates.")


def _row(machines, nodes=(), *, local=False, label="Blue Room"):
    return {"id": "local" if local else "ctl", "label": label, "local": local, "reachable": True,
            "machines": list(machines), "nodes": list(nodes),
            "activity": {"machines": [{"machine": m["machine"], "state": "ready"} for m in machines], "pulse": {}},
            "running_jobs": [], "local_models": [], "workers": [], "probe_machines_hidden": 0}


def _by_id(flat):
    return {m["machine"]: m["label"] for m in flat}


class MachineLabelTests(unittest.TestCase):
    def test_a_remote_controllers_own_mac_is_never_this_mac(self) -> None:
        row = _row([{"machine": "terranash-1100-m4-16-256", "label": "This Mac", "reachable": True},
                    {"machine": "n1", "label": "This Mac", "reachable": True}],
                   nodes=[{"id": "n1", "name": "Studio B"}])
        labels = _by_id(fleet.machines_flat([row]))
        self.assertEqual(labels["terranash-1100-m4-16-256"], "terranash-1100-m4-16-256")
        self.assertEqual(labels["n1"], "Studio B")   # roster name outranks the Hub's placeholder

    def test_a_controller_whose_own_id_is_local_uses_its_roster_label(self) -> None:
        flat = fleet.machines_flat([_row([{"machine": "local", "label": "This Mac"}], label="Blue Room")])
        self.assertEqual(flat[0]["label"], "Blue Room")

    def test_a_real_hub_label_is_kept_and_roster_name_wins_over_it(self) -> None:
        row = _row([{"machine": "m1", "label": "Mini 1"}, {"machine": "m2", "label": "hubname"}],
                   nodes=[{"id": "m2", "name": "Rack 2"}])
        self.assertEqual(_by_id(fleet.machines_flat([row])), {"m1": "Mini 1", "m2": "Rack 2"})

    def test_only_the_gateways_own_mac_is_this_mac(self) -> None:
        row = _row([{"machine": "local", "label": "This Mac"}, {"machine": "n1", "label": "This Mac"}],
                   nodes=[{"id": "n1"}], local=True, label="This Mac")
        labels = _by_id(fleet.machines_flat([row]))
        self.assertEqual(labels["local"], "This Mac")
        self.assertEqual(labels["n1"], "n1")   # a node of the local controller is another Mac
        with patch.object(fleet, "local_label", return_value="Studio Mac"):
            row = _row([{"machine": "local", "label": "This Mac"}], local=True, label="Studio Mac")
            self.assertEqual(fleet.machines_flat([row])[0]["label"], "Studio Mac")

    def test_location_version_is_not_assigned_to_an_unidentified_machine(self) -> None:
        row = _row([{"machine": "local"}, {"machine": "other"}], local=True)
        row["version"] = "0.6.211"
        flat = {m["machine"]: m for m in fleet.machines_flat([row])}
        self.assertEqual(flat["local"]["hub_version"], "0.6.211")
        self.assertIsNone(flat["other"]["hub_version"])

    def test_disk_issues_name_the_real_label(self) -> None:
        mac = {"machine": "terranash-1106", "label": "This Mac", "reachable": True, "disk_free_gb": 3.0,
               "disk_total_gb": 460.0, "disk": {"state": "red", "free_gb": 3.0, "total_gb": 460.0}}
        issues = [i for i in fleet._issues_for(_row([mac])) if i["code"] == "disk_red"]
        self.assertIn("terranash-1106 3 GB free", issues[0]["message"])
        self.assertNotIn("This Mac", issues[0]["message"])

    def test_identical_warnings_are_stated_once(self) -> None:
        disk = fleet.machine_disk({"machine": "m", "disk": {"free_gb": 80, "total_gb": 500, "state": "green",
                                                            "warnings": [UPDATE, UPDATE, {"message": UPDATE}]}})
        self.assertEqual(disk["warnings"], [UPDATE])
        self.assertEqual([i["kind"] for i in disk["warning_items"]], ["macos_updates"])
        row = _row([{"machine": "a", "label": "a"}])
        issue = {"severity": "warning", "code": "x", "location_id": "ctl", "subject": "s", "message": "m",
                 "action": "", "location": "Blue Room"}
        with patch.object(fleet, "_issues_for", return_value=[issue, dict(issue)]):
            self.assertEqual(len(fleet._issues([row])), 1)


class WarningKindTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="fleet-warnings-")
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(fleet, "FLEET_WARNINGS_PATH", Path(tmp.name) / "fleet_warnings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        fleet._warnings_cache.update(stamp=None, hidden=[])

    def test_kinds(self) -> None:
        self.assertEqual(fleet.warning_kind(UPDATE), "macos_updates")
        self.assertEqual(fleet.warning_kind("Time Machine is backing up to this disk."), "time_machine")
        self.assertEqual(fleet.warning_kind("Only 14 GB free."), "disk_space")
        self.assertEqual(fleet.warning_kind("Something else."), "other")

    def test_hide_persist_and_show_again(self) -> None:
        self.assertEqual(fleet.hidden_warning_kinds(), [])
        self.assertEqual(fleet.set_hidden_warning_kinds(["macos_updates"]), ["macos_updates"])
        fleet._warnings_cache.update(stamp=None, hidden=[])   # as a fresh process would
        self.assertEqual(fleet.hidden_warning_kinds(), ["macos_updates"])
        self.assertEqual(fleet.warnings_setting()["hidden"], ["macos_updates"])
        fleet.set_hidden_warning_kinds([])
        self.assertEqual(fleet.hidden_warning_kinds(), [])

    def test_disk_space_and_unknown_kinds_can_never_be_hidden(self) -> None:
        for bad in (["disk_space"], ["macos_updates", "disk_space"], ["other"], ["nonsense"]):
            with self.assertRaises(ValueError):
                fleet.set_hidden_warning_kinds(bad)
        self.assertEqual(fleet.hidden_warning_kinds(), [])

    def test_the_endpoint_refuses_disk_space_and_the_snapshot_carries_the_setting(self) -> None:
        from gateway import admin
        ok = admin.set_fleet_warnings({"hidden": ["time_machine"]})
        self.assertEqual(ok["hidden"], ["time_machine"])
        refused = admin.set_fleet_warnings({"hidden": ["disk_space"]})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(admin.get_fleet_warnings()["hidden"], ["time_machine"])
        with patch.object(fleet, "snapshot", return_value={"locations": [], "machines": []}):
            self.assertEqual(admin.fleet_snapshot()["fleet_warnings"]["hidden"], ["time_machine"])



SWIFT_HARNESS = r'''
import AppKit

enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct BannerCheck {
    static func descendants(_ view: NSView) -> [NSView] { [view] + view.subviews.flatMap(descendants) }

    static func main() {
        _ = NSApplication.shared
        let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let payload = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        let snapshot = FleetSnapshot.from(payload)!
        let machines = snapshot.machines
        assert(machines.count == 45, "decoded \(machines.count) machines")

        // 17 Macs say the same thing: ONE line, the count, the fix once, the labels behind [Show Macs].
        let built = FleetWarnings.build(machines: machines)
        let updates = built.warnings.filter { $0.kind == "macos_updates" }
        assert(updates.count == 1, "update warnings must be one line, got \(updates.count)")
        let line = updates[0]
        assert(line.text.hasPrefix("17 Macs download macOS updates automatically (8-15 GB each)"), line.text)
        assert((line.fix ?? "").contains("System Settings › General › Software Update › Automatic Updates › Download new updates"),
               "fix: \(String(describing: line.fix))")
        assert(line.items.count == 17 && line.items.contains("mac-03"), "the 17 labels: \(line.items)")
        assert(line.hideKind == "macos_updates", "the update kind is hideable")
        // The same sentence twice from one Mac still counts it once (mac-03 sends it twice).
        assert(Set(line.items).count == 17)
        // Time Machine is its own line; an unknown warning is a generic one-Mac line.
        assert(built.warnings.filter { $0.kind == "time_machine" }.count == 1)

        // One Mac low on disk: the Hub's own "Only 29.8 GB free" is not repeated beside it.
        let disks = built.warnings.filter { $0.kind == "disk_space" }
        assert(disks.count == 1, "one disk line: \(disks.map { $0.text })")
        assert(disks[0].text == "terranash-1100-m4-16-256: disk amber, 29.8 GB free (largest: other 373 GB)", disks[0].text)
        assert(disks[0].hideKind == nil, "disk space must never be hideable")
        assert(built.warnings.first?.kind == "disk_space", "disk lines come first")
        assert(!built.warnings.contains { $0.text.contains("This Mac") }, "a controller's own Mac is never 'This Mac'")

        // Hiding: updates go, and the banner says how many Macs it is hiding. Disk space cannot be hidden,
        // even when the stored list names it.
        let hidden = FleetWarnings.build(machines: machines, hidden: ["macos_updates", "disk_space"])
        assert(!hidden.warnings.contains { $0.kind == "macos_updates" })
        assert(hidden.warnings.contains { $0.kind == "disk_space" }, "disk space survived a hide request")
        assert(hidden.hiddenKinds == ["macos_updates"] && hidden.hiddenMacs == 17, "\(hidden.hiddenKinds) \(hidden.hiddenMacs)")

        // Gateway issues: eight offline Macs are one line with the fix once.
        let grouped = FleetWarnings.group(issues: snapshot.issues)
        let offline = grouped.filter { $0.kind == "machine_offline" }
        assert(offline.count == 1 && offline[0].text == "8 machines are offline", offline.map { $0.text }.joined())
        assert(offline[0].fix == "Check that Mac is switched on and awake" && offline[0].items.count == 8)
        assert(grouped.contains { $0.text == "Job j-1 is stalled." }, "a single issue keeps its own sentence")

        // The view: 20 kinds of warning are capped at ~5 lines and scroll; the table below keeps the rest.
        let size = NSSize(width: 1100, height: 800)
        let tab = FleetMachinesTab(frame: NSRect(origin: .zero, size: size))
        var wide = machines
        for index in 0..<20 {   // twenty distinct warning sentences on the first Mac
            var disk = wide[0].disk!
            disk.warningItems.append(FleetWarningItem(kind: "other", text: "Distinct warning number \(index) about something."))
            wide[0].disk = disk
        }
        tab.update(machines: wide, studios: [:], filter: FleetFilter())
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = tab
        tab.frame = NSRect(origin: .zero, size: size)
        for _ in 0..<3 { tab.layoutSubtreeIfNeeded() }
        let scrolls = descendants(tab.warningList).compactMap { $0 as? NSScrollView }
        assert(!scrolls.isEmpty && scrolls[0].frame.height <= FleetClampedScroll.bannerHeight + 1,
               "banner viewport \(scrolls.first?.frame.height ?? -1) exceeds the cap")
        assert(scrolls[0].documentView!.frame.height > FleetClampedScroll.bannerHeight, "the content must be taller than the cap here")
        let tables = descendants(tab).compactMap { $0 as? NSTableView }
        let tableHeight = tables.first.map { ($0.enclosingScrollView ?? NSScrollView()).frame.height } ?? 0
        assert(tableHeight >= 200, "the table got only \(tableHeight) pt of 800")
        assert(tab.warningList.frame.height <= FleetClampedScroll.bannerHeight + 2 * DS.Space.s + 1)

        // [Show Macs] expands the labels inline; [Hide Warning] reports the kind.
        var hiddenKind: String?
        tab.warningList.onHide = { hiddenKind = $0 }
        let buttons = descendants(tab.warningList).compactMap { $0 as? NSButton }
        buttons.first { $0.title == "Hide Warning" }?.performClick(nil)
        assert(hiddenKind != nil, "Hide Warning did not report its kind")
        let before = descendants(tab.warningList).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(!before.contains { $0.contains("mac-16") }, "labels must be collapsed until asked for")
        descendants(tab.warningList).compactMap { $0 as? NSButton }.first { $0.title == "Show Macs" }?.performClick(nil)
        for _ in 0..<3 { tab.layoutSubtreeIfNeeded() }
        let after = descendants(tab.warningList).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(after.contains { $0.contains("mac-16") }, "Show Macs did not list the labels")
        assert(descendants(tab.warningList).compactMap { $0 as? NSButton }.contains { $0.title == "Hide Macs" })

        // No warnings: the banner takes no room.
        let calm = FleetMachinesTab(frame: NSRect(origin: .zero, size: size))
        calm.update(machines: [], studios: [:], filter: FleetFilter())
        assert(calm.warningList.isHidden, "an empty banner must be hidden")
        print("PASS: banner grouping, hide, cap and expand")
    }
}
'''


def _machines():
    out = []
    for i in range(45):
        name = f"mac-{i:02d}"
        warnings = []
        if i < 17:
            warnings.append(UPDATE)
        if i == 2:
            warnings.append(UPDATE)            # the same sentence twice from one Mac
        if i == 20:
            warnings.append("Time Machine is backing up to this disk.")
        free, state = 200.0, "green"
        if i == 5:
            name, free, state = "terranash-1100-m4-16-256", 29.8, "amber"
            warnings.append("Only 29.8 GB free.")   # restates the disk line: must not be repeated
        disk = {"state": state, "free_gb": free, "total_gb": 460.0, "warnings": warnings,
                "warning_items": [{"kind": fleet.warning_kind(w), "text": w} for w in warnings],
                "top": [{"name": "other", "gb": 373.0}] if i == 5 else []}
        out.append({"location_id": "ctl", "location": "Blue Room", "machine": name, "label": name,
                    "state": "offline" if 30 <= i < 38 else "ready", "reachable": not (30 <= i < 38),
                    "chip": "Apple M4", "memory_gb": 16.0, "disk_free_gb": free, "disk_total_gb": 460.0,
                    "disk": disk, "node_state": "reachable", "node_role": "agent"})
    return out


class BannerSwiftTests(unittest.TestCase):
    def test_grouping_hide_cap_and_expand(self) -> None:
        import json
        import subprocess
        root = Path(__file__).resolve().parents[1]
        issues = [{"severity": "warning", "code": "machine_offline", "location_id": "ctl", "location": "Blue Room",
                   "subject": f"mac-{30 + n}", "message": f"Machine mac-{30 + n} is offline.",
                   "action": "Check that Mac is switched on and awake"} for n in range(8)]
        issues.append({"severity": "error", "code": "running_stalled", "location_id": "ctl", "location": "Blue Room",
                       "subject": "j-1", "message": "Job j-1 is stalled.", "action": "Fleet → Jobs"})
        fixture = {"generated_at": 1, "machines": _machines(), "issues": issues, "locations": [], "running": []}
        with tempfile.TemporaryDirectory(prefix="gateway-banner-") as directory:
            source = Path(directory) / "banner.swift"
            binary = Path(directory) / "banner"
            data = Path(directory) / "fleet.json"
            source.write_text(SWIFT_HARNESS)
            data.write_text(json.dumps(fixture))
            compiled = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift", "menubar/FleetWorkstation.swift",
                 "menubar/FleetMacTrashModel.swift", "menubar/FleetLiteMode.swift",
                 "menubar/FleetHealth.swift", "menubar/FleetTabs.swift", "menubar/FleetGroups.swift", "menubar/SecretField.swift",
                 "menubar/FleetBanner.swift",
                 str(source), "-o", str(binary)],
                cwd=root, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary), str(data)], capture_output=True, text=True, timeout=60)
            self.assertEqual(checked.returncode, 0, checked.stderr + checked.stdout)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
