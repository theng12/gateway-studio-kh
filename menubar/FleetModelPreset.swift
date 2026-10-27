// A Mac's model preset, and its models, from the Gateway's Fleet page (owner decision 2026-10-28).
//
// Three things the owner asked for, each one request against the Mac's own controller:
//
//   * **set a Mac's preset** to one of the four the Hub ships - and it is the *Mac's* list that is
//     offered, read from that Mac, never a list in this app;
//   * **add one model** (already `FleetModelInstall`'s "Install a model…");
//   * **remove one model** - the Hub moves it into that library's trash, so it is recoverable.
//
// The read is that Mac's own install projection, relayed unchanged. Nothing here computes a preset,
// a count or a cost: a Mac that is asleep answers in its own words and is reported, not failed.

import AppKit

enum FleetModelPreset {
    static let changed = Notification.Name("FleetModelPresetChanged")
    /// One line per Mac, kept for the session so a page reload does not lose the last answer.
    static var status: [String: (text: String, health: Health)] = [:]

    static func key(_ machine: FleetMachine) -> String { "\(machine.locationId ?? "")/\(machine.machine ?? "")" }

    static func set(_ machine: FleetMachine, _ text: String, _ health: Health) {
        status[key(machine)] = (text, health)
        NotificationCenter.default.post(name: changed, object: key(machine))
    }

    private static func path(_ machine: FleetMachine) -> String? {
        guard let place = machine.locationId?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = machine.machine?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
        else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)"
    }

    static func name(_ machine: FleetMachine) -> String { machine.label ?? machine.machine ?? "this Mac" }

    /// The "…" menu entries: the preset sheet, and Remove a model…. Both need a reachable Mac whose
    /// Hub is new enough - the route is what says so, so the entries are offered and the refusal is
    /// shown in the Mac's own words rather than hidden behind a guess about its version.
    static func menuItems(for machine: FleetMachine, done: @escaping () -> Void) -> [NSMenuItem] {
        guard machine.reachable == true, machine.locationId != nil, machine.machine != nil else { return [] }
        return [ClosureMenuItem("Model Preset…") { choose(machine, done: done) }]
    }

    /// "Remove a model…" - the Hub moves it into that library's trash, so it is recoverable for 30
    /// days. Offered with the models **that Mac reports as installed**, so the list is the Mac's own
    /// and a model it does not have is not offered.
    static func removeItem(for machine: FleetMachine, done: @escaping () -> Void) -> NSMenuItem? {
        guard machine.reachable == true, machine.locationId != nil, machine.machine != nil else { return nil }
        return ClosureMenuItem("Remove a Model…") { chooseRemoval(machine, done: done) }
    }

    static func chooseRemoval(_ machine: FleetMachine, done: @escaping () -> Void) {
        guard let base = path(machine) else { return }
        set(machine, "Reading \(name(machine))'s models…", .info)
        GatewayAPI.get("\(base)/model-presets", timeout: 45) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    set(machine, "Could not read \(name(machine))'s models", .warn)
                    show(machine, error.localizedDescription)
                case .success(let answer):
                    let installed = (answer["models"] as? [[String: Any]] ?? []).filter {
                        $0["installed"] as? Bool == true
                    }
                    guard !installed.isEmpty else {
                        set(machine, "That Mac reported no installed models", .warn)
                        show(machine, "This Mac's Studio Hub reported no installed models, so there is nothing to remove.")
                        return
                    }
                    presentRemoval(machine, installed: installed, base: base, done: done)
                }
            }
        }
    }

    static func presentRemoval(_ machine: FleetMachine, installed: [[String: Any]], base: String,
                               done: @escaping () -> Void) {
        let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 380, height: 26), pullsDown: false)
        picker.addItems(withTitles: installed.map { row in
            let label = row["label"] as? String ?? row["packageId"] as? String ?? "A model"
            let gb = (row["bytes"] as? NSNumber).map { String(format: " — %.1f GB", $0.doubleValue / 1e9) } ?? ""
            return "\(label)\(gb)"
        })
        let alert = NSAlert()
        alert.messageText = "Remove a model from \(name(machine))"
        alert.informativeText = "It is moved into that Mac's model-library trash, not deleted, and can be "
            + "restored there for 30 days. A model a job is using, or one another installed model needs, "
            + "is refused and says which."
        alert.accessoryView = picker
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn,
              installed.indices.contains(picker.indexOfSelectedItem),
              let package = installed[picker.indexOfSelectedItem]["packageId"] as? String else { return }
        remove(machine, package: package, base: base, done: done)
    }

    static func remove(_ machine: FleetMachine, package: String, base: String,
                       done: @escaping () -> Void) {
        set(machine, "Removing \(package) from \(name(machine))…", .info)
        GatewayAPI.delete("\(base)/model-packages/\(package)") { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    set(machine, "Not removed: \(error.localizedDescription)", .bad)
                    show(machine, error.localizedDescription)
                case .success(let answer):
                    // The Hub's own sentence names the trash folder and the bytes; showing it is
                    // better than a status this app made up from the same fields.
                    set(machine, answer["detail"] as? String ?? "Removed \(package); it is in that Mac's trash",
                        .ok)
                    done()
                }
            }
        }
    }

    /// Read this Mac's install screen and show the preset sheet on it.
    ///
    /// The read comes first because the sheet has to offer **that Mac's** presets with **that Mac's**
    /// counts: a preset list held here would be a second copy of a data file on the far side of the
    /// network, and it would be the wrong one the day a preset changes.
    static func choose(_ machine: FleetMachine, done: @escaping () -> Void) {
        guard let base = path(machine) else { return }
        set(machine, "Reading \(name(machine))'s models…", .info)
        GatewayAPI.get("\(base)/model-presets", timeout: 45) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    set(machine, "Could not read \(name(machine))'s models", .warn)
                    show(machine, error.localizedDescription)
                case .success(let answer):
                    guard let presets = answer["presets"] as? [[String: Any]], !presets.isEmpty else {
                        set(machine, "That Mac reported no presets", .warn)
                        show(machine, "This Mac's Studio Hub reported no presets. Update Studio Hub on it, then try again.")
                        return
                    }
                    present(machine, presets: presets, answer: answer, base: base, done: done)
                }
            }
        }
    }

    /// One line per preset, in the owner's own words and with the Mac's own counts.
    ///
    /// `installedCount` of `packageCount` is what decides "already done", and it is the Hub's count of
    /// its own verified receipts - never a byte comparison made here.
    static func titles(_ presets: [[String: Any]]) -> [String] {
        presets.map { preset in
            let name = preset["name"] as? String ?? preset["presetId"] as? String ?? "A preset"
            let installed = (preset["installedCount"] as? NSNumber)?.intValue ?? 0
            let total = (preset["packageCount"] as? NSNumber)?.intValue ?? 0
            if preset["complete"] as? Bool == true { return "\(name) — installed" }
            if installed > 0 { return "\(name) — \(installed) of \(total)" }
            let toCopy = (preset["projection"] as? [String: Any])?["toCopyBytes"] as? NSNumber
            let gb = toCopy.map { String(format: "%.1f GB", $0.doubleValue / 1e9) } ?? "an unknown size"
            return "\(name) — copies \(gb)"
        }
    }

    static func present(_ machine: FleetMachine, presets: [[String: Any]], answer: [String: Any],
                        base: String, done: @escaping () -> Void) {
        let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 380, height: 26), pullsDown: false)
        picker.addItems(withTitles: titles(presets))
        if let starting = answer["startingPreset"] as? String,
           let index = presets.firstIndex(where: { $0["presetId"] as? String == starting }) {
            picker.selectItem(at: index)
        }
        let memory = (answer["memoryGb"] as? NSNumber)?.intValue
        let free = ((answer["disk"] as? [String: Any])?["freeBytes"] as? NSNumber).map {
            String(format: "%.1f GB", $0.doubleValue / 1e9)
        }
        var facts: [String] = []
        if let memory { facts.append("\(memory) GB of memory") }
        if let free { facts.append("\(free) free on its models' drive") }
        let alert = NSAlert()
        alert.messageText = "Model preset for \(name(machine))"
        alert.informativeText = (facts.isEmpty ? "" : facts.joined(separator: ", ") + ". ")
            + "A preset decides what this Mac installs and what its one-time cleanup moves. "
            + "Every model outside it is chosen by hand on the Mac itself, and a model too big for it "
            + "is greyed out with the reason rather than hidden."
        alert.accessoryView = picker
        alert.addButton(withTitle: "Set")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn,
              presets.indices.contains(picker.indexOfSelectedItem),
              let chosen = presets[picker.indexOfSelectedItem]["presetId"] as? String else { return }
        apply(machine, preset: chosen, base: base, done: done)
    }

    static func apply(_ machine: FleetMachine, preset: String, base: String, done: @escaping () -> Void) {
        set(machine, "Setting \(name(machine))'s preset…", .info)
        // `requested_by` is the Gateway's own name for the actor: this route is owner-only, so the
        // owner pressing the button IS the request, and the Mac records that it came from here.
        GatewayAPI.post("\(base)/model-preset",
                        body: ["preset_id": preset, "requested_by": "gateway-fleet"],
                        timeout: 45) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    set(machine, "Preset not set: \(error.localizedDescription)", .bad)
                    show(machine, error.localizedDescription)
                case .success(let answer):
                    let saved = answer["preset_id"] as? String ?? preset
                    set(machine, "Preset set to \(saved)", .ok)
                    done()
                }
            }
        }
    }

    private static func show(_ machine: FleetMachine, _ message: String) {
        FleetStartup.show("Model preset on \(name(machine))", message)
    }
}
