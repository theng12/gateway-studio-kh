"""Offscreen layout checks for the tabbed Fleet page.

The five tabs are built from a fixture — never a live gateway — laid out in a
borderless window, and asserted: each fits the pane it is given (a page must never
be able to resize the window), the machines/models/jobs tables receive rows, and a
tinted card resolves its colour per appearance. That last one is not academic: the
tint was set once from `NSColor.cgColor`, which snapshots one appearance, and the
first offscreen draw showed the washed-out result.
"""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

#: One local Mac and one controller, each reporting the same two models on two
#: machines, so the Models tab has something to pool and the detail table has more
#: than one row. Deliberately small and offline.
FIXTURE = {
    "generated_at": 1,
    "summary": {
        "locations": 2, "locations_up": 2, "machines": 4, "machines_online": 3,
        "machines_offline": 1, "machines_unknown": 0, "jobs_completed": 11,
        "jobs_failed": 1, "running_jobs": 1, "local_models_total": 2,
        "models_available": 3, "workers_total": 4, "workers_ready": 3, "issues": 2,
    },
    "locations": [
        {
            "id": "local", "label": "This Mac", "local": True, "reachable": True,
            "role": "agent", "version": "0.6.47", "provider_id": "studio-hub-local",
            "models_discovered": 5, "models_enabled": 4, "models_publishable": 2,
            "machines": [{"machine": "local", "label": "This Mac", "reachable": True}],
            "workers": [
                {"id": "voice", "studio": "voice", "modality": "voice", "machine": "local",
                 "online": True, "ready": True, "busy": False, "drained": False,
                 "maintenance": False, "quarantined": False, "slots": 1, "slots_total": 1,
                 "models": ["mock-hub/clone-model"],
                 "supported_operations": ["voice.tts"]},
            ],
            "local_models": [
                {"model": "mock-hub/clone-model", "label": "Clone Model", "studio": "voice",
                 "modality": "voice", "machine": "local", "available": True,
                 "runtime_ready": True, "memory_eligible": True, "cache_state": "cached",
                 "dispatch_state": "eligible",
                 "fit": {"state": "ok", "label": "fits comfortably", "actual_gb": 32,
                         "required_gb": 8}},
                {"model": "mock-hub/image-model", "label": "Image Model", "studio": "image",
                 "modality": "image", "machine": "local", "available": False,
                 "runtime_ready": False, "memory_eligible": True, "cache_state": "absent",
                 "catalog_error": "the Hub could not read this model's catalogue"},
            ],
        },
        {
            "id": "ctrl-1", "label": "Blue Room", "local": False, "reachable": True,
            "role": "controller", "version": "0.6.47", "provider_id": "studio-hub-ctrl-1",
            "models_discovered": 5, "models_enabled": 0, "models_publishable": 2,
            "machines": [
                {"machine": "mac-a", "label": "mac-a", "state": "long_idle"},
                {"machine": "mac-b", "label": "mac-b", "state": "offline"},
            ],
            "workers": [
                {"id": "mac-a:voice", "studio": "voice", "modality": "voice", "machine": "mac-a",
                 "online": True, "ready": True, "busy": False, "drained": False,
                 "maintenance": False, "quarantined": False, "slots": 0, "slots_total": 1},
            ],
            "local_models": [
                {"model": "mock-hub/clone-model", "label": "Clone Model", "studio": "voice",
                 "modality": "voice", "machine": "mac-a", "available": True,
                 "runtime_ready": True, "memory_eligible": True, "cache_state": "cached",
                 "fit": {"state": "tight", "label": "tight — close other apps",
                         "actual_gb": 16, "required_gb": 12}},
                {"model": "mock-hub/clone-model", "label": "Clone Model", "studio": "voice",
                 "modality": "voice", "machine": "mac-b", "available": False,
                 "runtime_ready": False, "memory_eligible": False, "cache_state": "absent"},
                {"model": "mock-hub/image-model", "label": "Image Model", "studio": "image",
                 "modality": "image", "machine": "mac-a", "available": False,
                 "runtime_ready": True, "memory_eligible": True, "cache_state": "cached"},
            ],
        },
    ],
    "machines": [
        {"location_id": "local", "location": "This Mac", "machine": "local", "label": "This Mac",
         "state": "long_idle", "completed": 11, "failed": 1, "median_runtime_s": 68.0,
         "chip": "Apple M2 Max", "memory_gb": 32.0, "disk_free_gb": 250.0,
         "modalities": {"image": 3, "voice": 7}},
        {"location_id": "ctrl-1", "location": "Blue Room", "machine": "mac-a", "label": "mac-a",
         "state": "ready", "completed": 4, "failed": 0, "median_runtime_s": 41.0,
         "chip": "Apple M4", "memory_gb": 16.0, "disk_free_gb": 120.0,
         "modalities": {"voice": 4}, "limitation": "Direct activity partially unavailable"},
        {"location_id": "ctrl-1", "location": "Blue Room", "machine": "mac-b", "label": "mac-b",
         "state": "offline", "completed": 0, "failed": 0, "chip": "Apple M4",
         "memory_gb": 16.0, "disk_free_gb": 380.0, "modalities": {}},
    ],
    "running": [
        {"location_id": "ctrl-1", "provider_id": "studio-hub-ctrl-1",
         "location": "Blue Room", "location_local": False, "modality": "voice",
         "model": "mock-hub/clone-model", "batch_id": "batch-9",
         "running": 1, "queued": 2, "total": 3, "stalled": True,
         "governor_note": "waiting on a free slot", "uncertain": 1,
         "no_progress_s": 1200, "routing": "pool", "started_at": 1},
    ],
    "issues": [
        {"severity": "error", "code": "running_stalled", "location_id": "ctrl-1",
         "location": "Blue Room", "subject": "job-1",
         "message": "Job job-1 is stalled.", "action": "Fleet → Jobs"},
        {"severity": "warning", "code": "model_catalog_error", "location_id": "local",
         "location": "This Mac", "subject": "mock-hub/image-model",
         "message": "Model mock-hub/image-model failed its catalogue check.",
         "action": "Fleet → Models"},
    ],
}


class FleetLayout(unittest.TestCase):
    def test_tabs_lay_out_and_a_card_tint_follows_the_appearance(self):
        harness = r'''
import AppKit

// Stubbed so the harness compiles the Fleet sources without the whole app module
// (GatewayController lives in main.swift).
enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct FleetLayoutCheck {
    static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    static func rgb(_ color: CGColor?) -> [CGFloat] {
        guard let color, let space = CGColorSpace(name: CGColorSpace.sRGB),
              let converted = color.converted(to: space, intent: .defaultIntent, options: nil),
              let components = converted.components, components.count >= 3 else { return [] }
        return Array(components.prefix(3))
    }

    static func layout(_ name: String, _ view: NSView, rows: Int? = nil) -> NSWindow {
        let size = NSSize(width: 900, height: 620)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = view
        view.frame = NSRect(origin: .zero, size: size)
        view.layoutSubtreeIfNeeded()
        let fitting = view.fittingSize
        assert(fitting.width <= size.width + 1,
               "\(name) asks for \(fitting.width)pt width in a \(size.width)pt pane")
        if let rows {
            let drawn = descendants(view).compactMap { $0 as? NSTableView }
                .map { $0.numberOfRows }.reduce(0, +)
            assert(drawn >= rows, "\(name) drew \(drawn) rows, expected at least \(rows)")
        }
        return window
    }

    static func main() {
        let app = NSApplication.shared
        app.appearance = NSAppearance(named: .darkAqua)
        let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let payload = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
        guard let snapshot = FleetSnapshot.from(payload) else {
            print("FAIL: the fixture did not decode"); exit(1)
        }

        let overview = FleetOverviewTab(frame: .zero)
        overview.update(snapshot: snapshot)
        let overviewWindow = layout("Overview", overview)
        // Two issues, ordered error first: the list is what tells the owner what
        // is wrong, so an empty one would pass trivially and prove nothing.
        let issueCards = descendants(overview).compactMap { $0 as? FleetTintStack }
            .filter { $0.borderAlpha > 0 }
        assert(issueCards.count == 2, "expected 2 issue cards, got \(issueCards.count)")

        let card = issueCards[0]
        overviewWindow.appearance = NSAppearance(named: .darkAqua)
        card.display()
        let dark = rgb(card.layer?.borderColor)
        overviewWindow.appearance = NSAppearance(named: .aqua)
        card.display()
        let light = rgb(card.layer?.borderColor)
        assert(!dark.isEmpty && !light.isEmpty && dark != light,
               "card tint did not follow the appearance: dark \(dark), light \(light)")

        let locations = FleetLocationsTab(frame: .zero)
        locations.update(locations: snapshot.locations)
        _ = layout("Locations", locations, rows: 2)

        let machines = FleetMachinesTab(frame: .zero)
        machines.update(machines: snapshot.machines, studios: snapshot.studiosByMachine,
                        filter: FleetFilter())
        _ = layout("Machines", machines, rows: 3)

        let models = FleetModelsTab(frame: .zero)
        models.update(models: snapshot.allLocalModels, workers: snapshot.locations.flatMap { $0.workers }, filter: FleetFilter())
        _ = layout("Models", models, rows: 4)

        let jobs = FleetJobsTab(frame: .zero)
        jobs.update(snapshot: snapshot, filter: FleetFilter())
        _ = layout("Jobs", jobs, rows: 1)

        // The cancel is the one Fleet action that reaches upstream. It must be
        // offered for a batch the Hub still holds, and the Hub's own reason must
        // be what the page says rather than a field read as the wrong type.
        guard let live = snapshot.running.first else {
            print("FAIL: the running batch did not decode"); exit(1)
        }
        assert(live.canCancel, "a running batch with an id must be cancellable")
        assert(FleetJobsTab.stateText(live).contains("uncertain"),
               "state text ignored the Hub's uncertainty: \(FleetJobsTab.stateText(live))")
        assert(live.errorCount == nil, "a missing error count must stay unknown, not 0")
        let cancelButtons = descendants(jobs).compactMap { $0 as? FleetClosureButton }
            .filter { $0.title == "Cancel" }
        assert(cancelButtons.count == 1 && cancelButtons[0].isEnabled,
               "expected one enabled Cancel button, got \(cancelButtons.map { $0.isEnabled })")

        // A batch already asked to stop must not offer it again.
        var asked = live
        asked.cancelRequested = 1
        assert(!asked.canCancel, "a batch already being stopped must not offer Cancel again")

        // The location filter must actually narrow: the controller's machines only.
        let filteredMachines = FleetMachinesTab(frame: .zero)
        var filter = FleetFilter()
        filter.locationID = "ctrl-1"
        filteredMachines.update(machines: snapshot.machines,
                                studios: snapshot.studiosByMachine, filter: filter)
        let rows = descendants(filteredMachines).compactMap { $0 as? NSTableView }
            .map { $0.numberOfRows }.reduce(0, +)
        assert(rows == 2, "location filter drew \(rows) machine rows, expected 2")

        print("PASS: five Fleet tabs fit the pane, filter works, card tint follows appearance")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="gateway-fleet-layout-") as directory:
            source = Path(directory) / "layout.swift"
            binary = Path(directory) / "layout"
            fixture = Path(directory) / "fleet.json"
            source.write_text(harness)
            fixture.write_text(json.dumps(FIXTURE))
            compiled = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "menubar/FleetModels.swift", "menubar/FleetTabs.swift",
                 str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary), str(fixture)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
