#!/usr/bin/env python3
"""The native Remote final rendering sheet: parsing, wording, and which switches count as changes.

Compiles `menubar/FleetRemoteRender.swift` against small stand-ins for the app's shared helpers and runs it
headless (no window, no network). Skipped where there is no `swiftc`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import AppKit

enum TextRole { case body, secondary, caption, rowTitle }
enum DS { enum Space { static let xs: CGFloat = 4; static let s: CGFloat = 8; static let m: CGFloat = 12 } }
enum PageKit {
    static func label(_ text: String, _ role: TextRole, colour: NSColor = .labelColor,
                      wrapping: Bool = false) -> NSTextField {
        wrapping ? NSTextField(wrappingLabelWithString: text) : NSTextField(labelWithString: text)
    }
}
enum GatewayAPI {
    static func get(_ path: String, timeout: TimeInterval = 15,
                    completion: @escaping (Result<[String: Any], Error>) -> Void) {}
    static func post(_ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 15,
                     completion: ((Result<[String: Any], Error>) -> Void)? = nil) {}
}
enum FleetStartup { static func show(_ title: String, _ detail: String) {} }

func buttons(_ view: NSView) -> [NSButton] {
    (view as? NSButton).map { [$0] } ?? view.subviews.flatMap(buttons)
}

@main struct Check {
    static func main() {
        let rows: [[String: Any]] = [
            ["controller_id": "shop", "machine_id": "near", "machine": "Near Mini", "chip": "Apple M4",
             "memory_gb": 16.0, "idle_state": "idle", "network": "same_network",
             "address": "http://192.168.1.20:47873", "authority": "none", "opted_in": false,
             "hub_accepting": false, "suggested": true],
            ["controller_id": "shop", "machine_id": "tail", "machine": "Tail Mac", "chip": "Apple M2",
             "memory_gb": 8.0, "idle_state": "busy", "network": "tailscale",
             "address": "http://100.101.1.1:47873", "authority": "none", "opted_in": true,
             "hub_accepting": false, "suggested": false,
             "note": "Switched on here, but this Mac's Studio Hub has not confirmed it yet."],
            ["controller_id": "shop", "machine_id": "good", "machine": "Qualified", "chip": "Apple M4",
             "memory_gb": 32.0, "idle_state": "idle", "network": "other_lan", "authority": "production",
             "opted_in": false, "hub_accepting": false, "suggested": false],
            ["controller_id": "shop", "machine": "No id"],   // not a usable row
        ]
        let workers = rows.compactMap(FleetRenderWorker.init)
        assert(workers.map(\.machineId) == ["near", "tail", "good"], "bad rows must be dropped")
        assert(workers[0].statusText == "Off")
        assert(workers[1].statusText == "On here, waiting for the Mac's Studio Hub")
        assert(workers[2].statusText == "Already qualified" && workers[2].alreadyQualified)
        assert(FleetRenderWorker(["controller_id": "c", "machine_id": "m", "opted_in": true,
                                  "hub_accepting": true])!.statusText == "On, confirmed by the Mac")
        assert(workers[0].detailLine
               == "Apple M4 · 16 GB · Idle · Same network (http://192.168.1.20:47873) · Off",
               workers[0].detailLine)
        assert(workers[1].networkText == "Tailscale" && workers[2].networkText == "Another local network")
        assert(FleetRenderWorker(["controller_id": "c", "machine_id": "m"])!.networkText == "Network unknown")

        let jobs = [
            FleetRenderJob(["id": "1", "machine": "Near Mini", "state": "output_ready",
                            "unqualified": true, "created_at": 1_700_000_000])!,
            FleetRenderJob(["id": "2", "machine": "Qualified", "state": "accepted", "unqualified": false])!,
        ]
        assert(FleetRenderJob(["state": "x"]) == nil)
        assert(jobs[0].line.hasSuffix("Near Mini · output ready · unqualified"), jobs[0].line)
        assert(jobs[1].line == "Qualified · accepted", jobs[1].line)

        let form = FleetRemoteRenderForm(workers: workers, jobs: jobs)
        let boxes = buttons(form.view).filter { $0.title.hasPrefix("Allow remote rendering (untested)") }
        assert(boxes.count == 3, "one switch per Mac")
        assert(boxes.map { $0.state == .on } == [false, true, false], "default is the Gateway's saved choice")
        assert(boxes[0].isEnabled && boxes[1].isEnabled && !boxes[2].isEnabled, "a qualified Mac has no switch")
        assert(form.changes.isEmpty, "nothing is sent until a switch moves")
        boxes[0].performClick(nil)
        assert(form.changes.map { $0.worker.machineId } == ["near"] && form.changes[0].enabled)
        boxes[1].performClick(nil)
        assert(form.changes.map { $0.worker.machineId } == ["near", "tail"]
               && form.changes.map { $0.enabled } == [true, false])
        boxes[0].performClick(nil)
        assert(form.changes.map { $0.worker.machineId } == ["tail"], "moving it back is no change")

        let empty = FleetRemoteRenderForm(workers: [], jobs: [])
        assert(empty.changes.isEmpty)
        print("PASS: native remote rendering sheet")
    }
}
'''


@unittest.skipUnless(shutil.which("swiftc") and sys.platform == "darwin", "needs swiftc on macOS")
class NativeSheetTests(unittest.TestCase):
    def test_sheet_model_wording_and_switch_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-remote-render-") as directory:
            source = Path(directory) / "check.swift"
            binary = Path(directory) / "check"
            source.write_text(HARNESS)
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/FleetRemoteRender.swift", str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
