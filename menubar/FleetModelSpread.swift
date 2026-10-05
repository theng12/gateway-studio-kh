// LAN model sharing (owner decision 2026-10-05): name each Mac's network, and install a model on every Mac that can
// run it — each network downloads it from the internet once, then its Macs copy it from each other.
//
// The gateway plans and runs the spread (`gateway/model_share.py`); this file is the "Network…" sheet (Fleet →
// Locations "…" and Fleet → Machines "…"), the "Install on every Mac that can run it" action (the Install sheet and
// Fleet → Models). The progress card on Fleet → Models (per network, per Mac: "Copying 42% from 1104 (LAN)" /
// "Installed from the internet") is `FleetModelSpreadView` in FleetTabs.swift; its rows are in FleetModels.swift.

import AppKit

enum FleetNetwork {
    /// The "Network…" sheet: one short name; empty clears it.
    static func alert(title: String, info: String, current: String?, placeholder: String) -> (NSAlert, NSTextField) {
        let field = NSTextField(string: current ?? "")
        field.frame = NSRect(x: 0, y: 0, width: 220, height: 24)
        field.placeholderString = placeholder
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = info
        alert.accessoryView = field
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        return (alert, field)
    }

    static func locationAlert(_ location: FleetLocation) -> (NSAlert, NSTextField) {
        alert(title: "Network for \(location.label ?? "this location")",
              info: "The network this location's Macs are on, e.g. A. Macs on the same network copy models from "
                + "each other instead of each downloading them from the internet. A Mac can be given its own "
                + "network on Fleet → Machines. Leave it empty if you do not know.",
              current: location.network, placeholder: "e.g. A")
    }

    static func machineAlert(_ machine: FleetMachine) -> (NSAlert, NSTextField) {
        let name = machine.label ?? machine.machine ?? "this Mac"
        let inherited = machine.networkOverride == nil ? machine.network : nil
        return alert(title: "Network for \(name)",
                     info: "The network this Mac is on, when it is not its group's (a shop iMac can be on any). "
                        + "Leave it empty to use its group's" + (inherited.map { " (now \($0))." } ?? ".")
                        + " Macs copy models only from Macs on the same network.",
                     current: machine.networkOverride, placeholder: inherited.map { "\($0) (its group's)" } ?? "e.g. C")
    }

    private static func encoded(_ value: String?) -> String? {
        value?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
    }

    static func editLocation(_ location: FleetLocation, done: @escaping () -> Void) {
        guard let place = encoded(location.id) else { return }
        let (alert, field) = locationAlert(location)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        save("/admin/api/fleet/controllers/\(place)/network", field.stringValue, what: location.label, done: done)
    }

    static func editMachine(_ machine: FleetMachine, done: @escaping () -> Void) {
        guard let place = encoded(machine.locationId), let node = encoded(machine.machine) else { return }
        let (alert, field) = machineAlert(machine)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        save("/admin/api/fleet/controllers/\(place)/machines/\(node)/network", field.stringValue,
             what: machine.label ?? machine.machine, done: done)
    }

    private static func save(_ path: String, _ value: String, what: String?, done: @escaping () -> Void) {
        let name = value.trimmingCharacters(in: .whitespacesAndNewlines)
        GatewayAPI.put(path, body: ["network": name]) { result in
            DispatchQueue.main.async {
                if case .failure(let error) = result {
                    FleetStartup.show("Could not set the network of \(what ?? "it")", error.localizedDescription)
                }
                done()
            }
        }
    }

    /// Plan and start the spread. `confirm` asks first (Fleet → Models); the Install sheet's button already did.
    static func installEverywhere(packageId: String, label: String, confirm: Bool, done: @escaping () -> Void) {
        if confirm {
            let alert = NSAlert()
            alert.messageText = "Install \(label) on every Mac that can run it?"
            alert.informativeText = "Each network downloads it from the internet once; the other Macs on that network "
                + "copy it from each other. Macs that cannot run it (memory, Lite Mode), This Mac while it is paused "
                + "and Macs that are offline are skipped. Progress shows here, per network."
            alert.addButton(withTitle: "Install Everywhere")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        GatewayAPI.post("/admin/api/fleet/model-installs", body: ["package_id": packageId], timeout: 30) { result in
            DispatchQueue.main.async {
                if case .failure(let error) = result {
                    FleetStartup.show("Could not install \(label) everywhere", error.localizedDescription)
                }
                done()
            }
        }
    }

    static func stop(_ plan: FleetModelSpread, done: @escaping () -> Void) {
        guard let id = encoded(plan.id) else { return }
        GatewayAPI.post("/admin/api/fleet/model-installs/\(id)/stop", body: [:], timeout: 30) { _ in
            DispatchQueue.main.async { done() }
        }
    }
}
