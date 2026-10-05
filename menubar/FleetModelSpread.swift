// LAN model sharing (owner decision 2026-10-05): name each Mac's network, and install a model on every Mac that can
// run it — each network downloads it from the internet once, then its Macs copy it from each other.
//
// The gateway plans and runs the spread (`gateway/model_share.py`); this file is the "Network…" sheet (Fleet →
// Locations "…" and Fleet → Machines "…"), the "Install on every Mac that can run it" action (the Install sheet and
// Fleet → Models). The progress card on Fleet → Models (per network, per Mac: "Copying 42% from 1104 (LAN)" /
// "Installed from the internet") is `FleetModelSpreadView` in FleetTabs.swift; its rows are in FleetModels.swift.

import AppKit

enum FleetNetwork {
    /// Every network name in use (locations and Macs), sorted; `FleetSection` refreshes it on each Fleet read.
    static var names: [String] = []

    /// The gateway's spelling of a name (`model_share._norm`): trimmed, inner spaces collapsed, upper case.
    static func normalised(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ").uppercased()
    }

    static func locationAlert(_ location: FleetLocation) -> FleetNetworkPicker {
        FleetNetworkPicker(title: "Network for \(location.label ?? "this location")",
                           info: "Macs on the same network copy models from each other instead of each downloading "
                            + "them. A Mac can have its own network on Fleet → Machines.",
                           first: "Not Set", current: location.network)
    }

    static func machineAlert(_ machine: FleetMachine) -> FleetNetworkPicker {
        let inherited = machine.networkOverride == nil ? machine.network : nil
        return FleetNetworkPicker(title: "Network for \(machine.label ?? machine.machine ?? "this Mac")",
                                  info: "Macs copy models only from Macs on the same network. A shop iMac can be on any.",
                                  first: "Same as Its Group" + (inherited.map { " (\($0))" } ?? ""),
                                  current: machine.networkOverride)
    }

    private static func encoded(_ value: String?) -> String? {
        value?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
    }

    static func editLocation(_ location: FleetLocation, done: @escaping () -> Void) {
        guard let place = encoded(location.id) else { return }
        let picker = locationAlert(location)
        guard picker.alert.runModal() == .alertFirstButtonReturn, let name = picker.value else { return }
        save("/admin/api/fleet/controllers/\(place)/network", name, what: location.label, done: done)
    }

    static func editMachine(_ machine: FleetMachine, done: @escaping () -> Void) {
        guard let place = encoded(machine.locationId), let node = encoded(machine.machine) else { return }
        let picker = machineAlert(machine)
        guard picker.alert.runModal() == .alertFirstButtonReturn, let name = picker.value else { return }
        save("/admin/api/fleet/controllers/\(place)/machines/\(node)/network", name,
             what: machine.label ?? machine.machine, done: done)
    }

    private static func save(_ path: String, _ name: String, what: String?, done: @escaping () -> Void) {
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

/// The "Network…" sheet: a popup of the networks in use (a typo can no longer start a separate network), with
/// "New Network…" last, which shows a field for a new name. Save stays off while that name is empty.
final class FleetNetworkPicker: NSObject, NSTextFieldDelegate {
    let alert = NSAlert()
    let popup = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 240, height: 26), pullsDown: false)
    let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
    private let stack = NSStackView()
    private let newItem = NSMenuItem(title: "New Network…", action: nil, keyEquivalent: "")

    /// `first` saves empty; `current` is the name set now (`nil`: `first`).
    init(title: String, info: String, first: String, current: String?) {
        super.init()
        popup.addItem(withTitle: first)
        popup.lastItem?.representedObject = ""
        for name in Set(FleetNetwork.names + [current].compactMap { $0 }).sorted() {
            popup.addItem(withTitle: "Network \(name)")
            popup.lastItem?.representedObject = name
        }
        popup.menu?.addItem(.separator())
        popup.menu?.addItem(newItem)
        popup.selectItem(at: max(0, popup.indexOfItem(withRepresentedObject: current ?? "")))
        popup.target = self
        popup.action = #selector(changed)
        field.placeholderString = "Name, e.g. D"
        field.delegate = self
        field.isHidden = true
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.addArrangedSubview(popup)
        stack.addArrangedSubview(field)
        stack.setFrameSize(stack.fittingSize)
        alert.messageText = title
        alert.informativeText = info
        alert.accessoryView = stack
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
    }

    /// The name to save ("" clears it), or `nil` while a new name is still empty.
    var value: String? {
        guard popup.selectedItem === newItem else { return popup.selectedItem?.representedObject as? String ?? "" }
        let name = FleetNetwork.normalised(field.stringValue)
        return name.isEmpty ? nil : name
    }

    @objc func changed() {
        field.isHidden = popup.selectedItem !== newItem
        stack.setFrameSize(stack.fittingSize)
        alert.layout()
        if !field.isHidden { alert.window.makeFirstResponder(field) }
        controlTextDidChange(Notification(name: NSControl.textDidChangeNotification))
    }

    func controlTextDidChange(_ notification: Notification) {
        alert.buttons.first?.isEnabled = value != nil
    }
}
