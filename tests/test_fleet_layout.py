"""Offscreen layout checks for the tabbed Fleet page.

The tabs (Groups, Locations, Machines, Models, Jobs) are built from a fixture — never a live gateway — laid out in a
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
    # The App Update Key identity and the last push. The refusal carries the raw
    # public-key path and comparison fingerprint, which the page quotes verbatim.
    "release": {
        "identity": {
            "algorithm": "ed25519",
            "public_key_base64": "A" * 44,
            "public_key_hex": "ab" * 32,
            "fingerprint": "cd" * 32,
            "publisher_id": "gatewaykh",
        },
        "last_push": {
            "updated_at": 1790000000,
            "fingerprint": "cd" * 32,
            "release": {
                "version": "0.6.50", "build": "abc1234",
                "filename": "StudioHub-0.6.50-abc1234.dmg",
                "bytes": 1234, "sha256": "ef" * 32, "signature": "sig",
            },
            "locations": {
                "ctrl-1": {
                    "ok": False, "status": 409, "code": "PUBLISHER_NOT_TRUSTED",
                    "detail": "no trusted gateway key is installed. Copy this Gateway's App Update Key "
                              "public key from Gateway → Settings → App Update Key "
                              "into Hub → Connections → App updates, then compare fingerprints "
                              "(this Gateway: " + "cd" * 32 + ").",
                    "label": "Blue Room", "release": None,
                },
            },
        },
    },
    "locations": [
        {
            "id": "local", "label": "This Mac", "local": True, "reachable": True,
            "role": "agent", "version": "0.6.47", "provider_id": "studio-hub-local",
            "models_discovered": 5, "models_enabled": 4, "models_publishable": 2,
            "release": None,
            # An older Hub that does not carry the field: "did not report".
            "trusted_publisher": {"reported": False, "publisher_id": None, "fingerprint": None},
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
            "network": "C",
            "role": "controller", "version": "0.6.47", "provider_id": "studio-hub-ctrl-1",
            "models_discovered": 5, "models_enabled": 0, "models_publishable": 2,
            # The gateway's pause clock (Q7): epoch seconds plus its own sentence.
            "paused_at": 1790000500, "unreachable_since": 1789000000,
            "pause_reason": "unreachable since 2026-09-01 — paused after 7 days. Routing skips "
                            "it; nothing was removed. Bring the Mac back, or resume it from the Fleet page.",
            # What the controller holds now: older than the pushed 0.6.50.
            "release": {
                "version": "0.6.49", "build": "deadbee",
                "filename": "StudioHub-0.6.49-deadbee.dmg", "sha256": "12" * 32,
                "publisher_key_id": "cd" * 32, "controller_key_id": "34" * 32,
                "controller_signature": "countersig",
            },
            # Hub 0.6.51 says it trusts nobody — the 409 state, not an unknown.
            "trusted_publisher": {"reported": True, "publisher_id": None, "fingerprint": None},
            # **The joined roster, and the controller's own count of it.** This is the
            # data the page never decoded (audit H10/M20/M21): a Mac that is reachable
            # with a last-seen stamp, and one that answered and refused this
            # controller's credential — which must not draw as "offline".
            "nodes": [
                {"id": "mac-a", "name": "mac-a", "state": "reachable", "role": "agent",
                 "address": "http://10.0.0.11:47873", "joined_at": 1790000000,
                 "last_seen_at": 1790000000, "credential_issued_at": 1790000000,
                 "has_credential": True, "version": "0.6.91",
                 "studios": [{"id": "voice", "modality": "voice"}]},
                {"id": "mac-b", "name": "mac-b", "state": "unauthorized", "role": "agent",
                 "address": "http://10.0.0.12:47873", "joined_at": 1790000000,
                 "last_seen_at": 1789999000, "credential_issued_at": 1790000000,
                 "has_credential": True,
                 "detail": "the node answered but rejected this controller's credential "
                           "(HTTP 401) - re-join it with the current Fleet Token",
                 "studios": [{"id": "image", "modality": "image"}]},
            ],
            "node_summary": {"total": 2, "reachable": 1, "unreachable": 0,
                             "unauthorized": 1, "error": 0, "removed": 0},
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
        {"location_id": "local", "location": "This Mac", "machine": "local", "label": "This Mac", "network": "B",
         "state": "long_idle", "completed": 11, "failed": 1, "median_runtime_s": 68.0,
         "chip": "Apple M2 Max", "memory_gb": 32.0, "disk_free_gb": 250.0,
         "modalities": {"image": 3, "voice": 7}},
        {"location_id": "ctrl-1", "location": "Blue Room", "machine": "mac-a", "label": "mac-a",
         # On network A by hand while its group is on C (`model_share.stamp_networks`); mac-b set C, the same.
         "network": "A", "network_override": "A",
         "state": "ready", "completed": 4, "failed": 0, "median_runtime_s": 41.0,
         "chip": "Apple M4", "memory_gb": 16.0, "disk_free_gb": 120.0,
         "modalities": {"voice": 4}, "limitation": "Direct activity partially unavailable",
         # What `machines_flat` joins from the roster: the role it joined with, its own
         # state, and when the controller last heard from it.
         "node_state": "reachable", "node_role": "agent",
         "node_address": "http://10.0.0.11:47873", "node_last_seen_at": 1790000000,
         # What the Hub says this node is running (Gateway `machines[].current`).
         "current": {"batch_id": "batch-9", "model": "mock-hub/clone-model", "label": "Chapter 3",
                     "studio": "voice", "started_at": 1790000000}},
        {"location_id": "ctrl-1", "location": "Blue Room", "machine": "mac-b", "label": "mac-b",
         "network": "C", "network_override": "C",
         "state": "offline", "completed": 0, "failed": 0, "chip": "Apple M4",
         "memory_gb": 16.0, "disk_free_gb": 380.0, "modalities": {},
         # The refused credential: the activity word is still `offline` (nothing can be
         # dispatched to it), and the node's own state and sentence are what make the
         # difference readable.
         "node_state": "unauthorized", "node_role": "agent",
         "node_last_seen_at": 1789999000,
         "node_detail": "the node answered but rejected this controller's credential "
                        "(HTTP 401) - re-join it with the current Fleet Token"},
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

        // **Groups: one card per controller group, the worst first.** Two groups, both with something
        // wrong (a paused controller; a stalled job on the local Mac and a refused credential), so the
        // needs-attention banner has to be drawn and its tint has to follow the appearance.
        let groups = FleetGroupsTab(frame: .zero)
        groups.update(snapshot: snapshot)
        let groupsWindow = layout("Groups", groups)
        let summaries = snapshot.groups
        assert(summaries.count == 2, "expected 2 groups, got \(summaries.count)")
        assert(summaries[0].isDown && summaries[0].name == "Blue Room",
               "the paused controller must sort first and read as down: \(summaries.map { $0.name })")
        assert(summaries[0].health == .bad, "an unreachable or paused group is red")
        assert(summaries[1].health == .warn, "a group with a stalled job and a disk or memory warning is amber, not red")
        let groupTexts = descendants(groups).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(groupTexts.contains(where: { $0.contains("2 of 2 groups need attention") }),
               "the needs-attention banner is missing: \(groupTexts.prefix(12))")
        assert(groupTexts.contains("Down") && groupTexts.contains("Attention"),
               "each group card must carry its state word: \(groupTexts.prefix(30))")
        let tinted = descendants(groups).compactMap { $0 as? CardView }.filter { $0.tint != nil }
        assert(!tinted.isEmpty, "the banner must be a tinted card")
        let card = tinted[0]
        groupsWindow.appearance = NSAppearance(named: .darkAqua)
        card.display()
        let dark = rgb(card.layer?.borderColor)
        groupsWindow.appearance = NSAppearance(named: .aqua)
        card.display()
        let light = rgb(card.layer?.borderColor)
        assert(!dark.isEmpty && !light.isEmpty && dark != light,
               "card tint did not follow the appearance: dark \(dark), light \(light)")

        // The rules that make a group red or amber, and the disk and memory tiers behind them.
        let studio = summaries[1]
        var mini = snapshot.machines.first { $0.machine == "local" }!
        mini.diskTotalGb = 460.0
        mini.availableGb = 18.0
        assert(mini.diskHealth == .ok && mini.memoryHealth == .ok, "a Mac with plenty free reads green")
        var tight = mini
        tight.availableGb = 4.0   // 12.5 % of 32 GB: amber under 20 %
        assert(tight.memoryHealth == .warn, "memory under 20 % free is amber")
        tight.availableGb = 3.0   // 9.4 %: red under 10 %
        assert(tight.memoryHealth == .bad, "memory under 10 % free is red")
        tight.memoryGb = 8.0
        tight.availableGb = 1.2   // 15 %, but under 1.5 GB: red
        assert(tight.memoryHealth == .bad, "memory under 1.5 GB free is red")
        tight.diskFreeGb = 4.0
        tight.disk = nil
        tight.diskTotalGb = 250.0
        assert(tight.diskHealth == .bad, "less than 5 GB free is red whatever the share")
        assert(!studio.doing(mini).headline.isEmpty, "doing-now must never be empty")
        // "Doing now": the node's stated current job decodes, and names the job and how long it has run.
        let stated = snapshot.machines.first { $0.machine == "mac-a" }!
        assert(stated.current?.label == "Chapter 3" && stated.current?.studio == "voice",
               "the node's current job did not decode: \(String(describing: stated.current))")
        var busy = mini
        busy.state = "working"
        busy.current = FleetCurrentJob(batchId: "b-1", model: "mlx/qwen", label: "Chapter 3", studio: "voice",
                                       startedAt: Date().timeIntervalSince1970 - 600)
        let doing = studio.doing(busy)
        assert(doing.headline == "Working · voice", "doing-now headline: \(doing.headline)")
        assert((doing.detail ?? "").contains("Chapter 3") && (doing.detail ?? "").contains("10 min"),
               "doing-now must name the stated job and its age: \(String(describing: doing.detail))")
        busy.current = nil
        assert(studio.doing(busy).detail == nil || studio.batches.isEmpty == false,
               "without a stated job the label-only fallback must not invent one")
        // A down group still lists the roster it remembers, with how long ago each node was seen.
        assert(summaries[0].showsRoster && summaries[0].nodeCount == 2,
               "a down group must list its last known roster, not an empty box")
        assert(groupTexts.contains(where: { $0.hasPrefix("Last known:") }),
               "the roster rows must say they are the last known state: \(groupTexts)")

        let locations = FleetLocationsTab(frame: .zero)
        locations.update(locations: snapshot.locations, machines: snapshot.machines)
        locations.updateRelease(locations: snapshot.locations, state: snapshot.release)
        _ = layout("Locations", locations, rows: 2)

        // **The members of a controller, decoded and drawn** (audit H10/M20/M21). The
        // Hub publishes `nodes` and `node_summary` per location, the gateway has always
        // forwarded both, and nothing here read them — so "see each member connected"
        // meant opening Studio Hub on the controller.
        let controller = snapshot.locations.first(where: { $0.id == "ctrl-1" })
        assert(controller?.nodes.count == 2, "the node roster did not decode")
        assert(controller?.nodeSummary?.total == 2 && controller?.nodeSummary?.unauthorized == 1,
               "the controller's own node summary did not decode")
        assert(controller?.memberCount?.reachable == 1 && controller?.memberCount?.total == 2,
               "the Members fraction did not decode")
        guard let refused = controller?.nodes.first(where: { $0.id == "mac-b" }) else {
            print("FAIL: the roster row for mac-b did not decode"); exit(1)
        }
        assert(refused.isUnauthorized, "the unauthorized node state did not decode")
        assert(!refused.isUnreachable, "unauthorized must not be read as unreachable")
        assert(refused.stateText == "credential refused", "the state word was collapsed: \(refused.stateText)")
        assert(refused.lastSeenAt?.doubleValue == 1789999000, "the node's last_seen_at did not decode")
        assert((refused.detail ?? "").contains("re-join it with the current Fleet Token"),
               "the controller's own sentence did not decode verbatim")
        assert(controller?.nodes.first(where: { $0.id == "mac-a" })?.role == "agent",
               "the node's join role did not decode")
        // **A paused controller decodes, says so, and offers Resume** (owner decision: the gateway's
        // own pause message tells the operator to resume from this page). Handover is offered for
        // every controller, never for this Mac.
        assert(controller?.pausedAt == 1790000500 && controller?.unreachableSince == 1789000000,
               "the pause clock did not decode")
        assert((controller?.pauseReason ?? "").contains("resume it from the Fleet page"),
               "the gateway's pause sentence did not decode")
        assert(snapshot.locations.first(where: { $0.id == "local" })?.pausedAt == nil,
               "a location that stated no pause must not read as paused")
        let buttonTitles = descendants(locations).compactMap { ($0 as? NSButton)?.title }
        assert(buttonTitles.filter { $0 == "Resume" }.count == 1,
               "Resume must appear exactly once: on the paused controller only, got \(buttonTitles)")
        // Rename, Credentials, Change Address and Remove sit in each row's "…" menu: Change Address is offered
        // once, for the controller, and never for this Mac.
        let menuTitles = descendants(locations).compactMap { $0 as? FleetMenuButton }.flatMap { $0.itemTitles }
        assert(menuTitles.filter { $0 == "Change Address…" }.count == 1,
               "Change Address… must be offered for the controller and not for this Mac: \(menuTitles)")
        let pausedTexts = descendants(locations).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(pausedTexts.contains(where: { $0.contains("paused after 7 days") }),
               "the Status column did not show the pause reason")
        // A location with no roster summary is unknown, never `0 / 0`.
        assert(snapshot.locations.first(where: { $0.id == "local" })?.memberCount == nil,
               "a location that stated no roster must not read as an empty one")
        let locationTexts = descendants(locations).compactMap { ($0 as? NSTextField)?.stringValue }
        // A Mac on another network than its location's is visible on Locations and on its Groups row.
        assert(locationTexts.contains("1 Mac on A"), "the Network column must say a Mac differs: \(locationTexts)")
        assert(groupTexts.contains(where: { $0.contains("Network B") }), "a node row must name its network")
        assert(groupTexts.contains(where: { $0.contains("Hub ") && $0.hasSuffix("Network C") }), "a group header must name its network: \(groupTexts)")
        assert(locationTexts.contains(where: { $0.contains("1 / 2") && $0.contains("1 refused") }),
               "the Members column did not draw the reachable count and the refusal")
        assert((controller?.membersTooltip ?? "").contains("re-join it with the current Fleet Token"),
               "the Members tooltip did not quote the controller's sentence")

        // The publisher identity and the controller-held descriptor decode, and
        // the refusal is quoted verbatim — raw-public-key path and comparison
        // fingerprint. A page that paraphrased it could lose the required action.
        assert(snapshot.release?.identity?.fingerprint == String(repeating: "cd", count: 32),
               "the publisher fingerprint did not decode")
        assert(snapshot.release?.identity?.publicKeyHex == String(repeating: "ab", count: 32),
               "the publisher public key did not decode")
        assert(snapshot.locations.first(where: { $0.id == "ctrl-1" })?.release?.versionBuild
               == "0.6.49 (deadbee)",
               "the controller-held release did not decode")
        let outcome = snapshot.release?.lastPush?.outcomes["ctrl-1"]
        assert(outcome?.status == 409, "the push status did not decode")
        assert(outcome?.ok == false, "the push outcome did not decode as a refusal")
        assert((outcome?.detail ?? "").contains("App Update Key public key"),
               "the refusal detail did not decode verbatim")
        let texts = descendants(locations).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(texts.contains(where: { $0.contains("held 0.6.49 (deadbee)") }),
               "the Release section did not show what the controller holds")
        // The trusted publisher decides whether a push will be accepted at all, so
        // "trusts nobody" and "did not say" must not draw the same sentence.
        assert(snapshot.locations.first(where: { $0.id == "ctrl-1" })?.trustedPublisher?.reported == true,
               "the controller's trusted-publisher field did not decode")
        assert(snapshot.locations.first(where: { $0.id == "local" })?.trustedPublisher?.reported == false,
               "the local Hub's missing trusted-publisher field did not decode as unreported")
        assert(texts.contains(where: { $0.contains("App Update Key: none installed yet") }),
               "the Release section did not say no trusted publisher is installed")
        assert(texts.contains(where: {
                   $0.contains("Last push refused")
                       && $0.contains("App Update Key public key")
                       && $0.contains("then compare fingerprints")
               }),
               "the Release section did not quote the refusal verbatim")

        let machines = FleetMachinesTab(frame: .zero)
        machines.update(machines: snapshot.machines, studios: snapshot.studiosByMachine,
                        filter: FleetFilter())
        _ = layout("Machines", machines, rows: 3)

        // **The Node marker and last-seen, on the machine itself.** `node_role` is what
        // says which Macs are members (never the label, which the owner can change),
        // `last_seen_at` is what separates "switched off" from "gone", and a refused
        // credential has to be readable as that rather than as one more offline Mac.
        let machineTexts = descendants(machines).compactMap { ($0 as? NSTextField)?.stringValue }
        assert(machineTexts.contains("Credential refused"),
               "the Machines tab drew a refused credential as a plain state: \(machineTexts)")
        assert(!machineTexts.contains(where: { $0.lowercased() == "offline" }),
               "the refused node was also drawn as an offline machine")
        assert(machineTexts.contains(where: { $0.hasPrefix("seen ") }),
               "a node's last-seen time must sit under its state: \(machineTexts)")
        guard let joined = snapshot.machines.first(where: { $0.machine == "mac-a" }) else {
            print("FAIL: the machine rows did not decode"); exit(1)
        }
        assert(joined.isNode && joined.nodeRoleText == "agent",
               "the node join did not reach the machine row")
        assert(joined.nodeLastSeenText == "just now",
               "the machine's node last-seen did not render: \(joined.nodeLastSeenText)")
        assert(!(snapshot.machines.first(where: { $0.machine == "local" })?.isNode ?? true),
               "the controller's own Mac must not be marked as a node")

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

        print("PASS: the Fleet tabs fit the pane, filter works, card tint follows appearance")
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
                 "menubar/PageKit.swift", "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift", "menubar/FleetWorkstation.swift",
                 "menubar/FleetMacTrashModel.swift", "menubar/FleetLiteMode.swift",
                 "menubar/FleetTabs.swift", "menubar/FleetGroups.swift", "menubar/SecretField.swift",
                 "menubar/FleetBanner.swift",
                 str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            checked = subprocess.run([str(binary), str(fixture)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
