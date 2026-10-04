// "Install a model…" on Fleet → Machines (owner decision 2026-10-04): make one Mac download a model it can run.
//
// The list is the gateway's (`installable_models` on each machine row: the Mac's own catalog says it has the
// memory, the model is not on it and not downloading). The install goes to the Mac's controller, which checks it
// again (shelved, disk, memory) and answers in its own sentence. Progress is polled and shown on the Mac's row,
// "Downloading Fish S2 Pro 4-bit — 42%", then the result, for the rest of this session.

import AppKit

enum FleetModelInstall {
    /// What each Mac's row says about its install, keyed `location/machine`. Kept for the session so a page reload
    /// does not lose a download in progress or its result.
    static var status: [String: (text: String, health: Health)] = [:]
    static let changed = Notification.Name("FleetModelInstallChanged")
    static let tooOld = "Needs a newer Studio Hub on this Mac"

    static func key(_ machine: FleetMachine) -> String { "\(machine.locationId ?? "")/\(machine.machine ?? "")" }

    private static func set(_ machine: FleetMachine, _ text: String, _ health: Health) {
        status[key(machine)] = (text, health)
        NotificationCenter.default.post(name: changed, object: key(machine))
    }

    /// The "…" menu's entry: enabled with the list, or greyed with why (nothing to install, or an older gateway).
    static func menuItems(for machine: FleetMachine, done: @escaping () -> Void) -> [NSMenuItem] {
        guard machine.reachable == true, machine.locationId != nil, machine.machine != nil else { return [] }
        let models = (machine.installableModels ?? []).filter { $0.packageId != nil }
        if !models.isEmpty {
            return [ClosureMenuItem("Install a model…") { choose(machine, models, done: done) }]
        }
        let item = NSMenuItem(title: "Install a model…", action: nil, keyEquivalent: "")
        let why = NSMenuItem(title: machine.installableModels == nil ? tooOld
                                 : "Every model this Mac can run is already on it", action: nil, keyEquivalent: "")
        why.indentationLevel = 1
        return [item, why]
    }

    /// The picker sheet: the models this Mac can run but does not have.
    static func pickerAlert(for machine: FleetMachine, _ models: [FleetInstallableModel]) -> (NSAlert, NSPopUpButton) {
        let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 320, height: 26), pullsDown: false)
        picker.addItems(withTitles: models.map(\.title))
        let alert = NSAlert()
        let name = machine.label ?? machine.machine ?? "this Mac"
        alert.messageText = "Install a model on \(name)"
        alert.informativeText = "These are the models \(name) has the memory to run and does not have yet. "
            + "It downloads the one you choose by itself; its controller checks the disk space first."
        alert.accessoryView = picker
        alert.addButton(withTitle: "Install")
        alert.addButton(withTitle: "Cancel")
        return (alert, picker)
    }

    static func choose(_ machine: FleetMachine, _ models: [FleetInstallableModel], done: @escaping () -> Void) {
        let (alert, picker) = pickerAlert(for: machine, models)
        guard alert.runModal() == .alertFirstButtonReturn, models.indices.contains(picker.indexOfSelectedItem) else { return }
        install(machine, models[picker.indexOfSelectedItem], done: done)
    }

    private static func path(_ machine: FleetMachine) -> String? {
        guard let place = machine.locationId?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = machine.machine?.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)"
    }

    static func install(_ machine: FleetMachine, _ model: FleetInstallableModel, done: @escaping () -> Void) {
        guard let base = path(machine), let package = model.packageId else { return }
        let name = model.label ?? package
        set(machine, "Starting the download of \(name)…", .info)
        GatewayAPI.post("\(base)/model-packages/\(package)/download", timeout: 45) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    set(machine, "Not installed: \(error.localizedDescription)", .bad)
                    FleetStartup.show("Could not install \(name) on \(machine.label ?? machine.machine ?? "this Mac")",
                                      error.localizedDescription)
                case .success(let answer):
                    let run = answer["run"] as? [String: Any] ?? [:]
                    guard let id = run["id"] as? String else {
                        set(machine, "Not installed: the Mac did not say it started.", .bad)
                        return
                    }
                    follow(machine, name, base: base, run: id, last: run, done: done)
                }
            }
        }
    }

    /// The row's sentence for one status read.
    static func sentence(_ name: String, _ run: [String: Any]) -> (String, Health, finished: Bool) {
        switch run["state"] as? String ?? "" {
        case "done": return ("Installed \(name)", .ok, true)
        case "error", "interrupted":
            let tail = (run["error_tail"] as? String).map { ": \($0.split(separator: "\n").last.map(String.init) ?? $0)" } ?? ""
            return ("Download of \(name) failed\(tail)", .bad, true)
        case "cancelled": return ("Download of \(name) was cancelled", .warn, true)
        default:
            let total = (run["total_bytes"] as? NSNumber)?.doubleValue ?? 0
            let done = (run["bytes_done"] as? NSNumber)?.doubleValue ?? 0
            let percent = total > 0 ? " — \(min(99, Int(done * 100 / total)))%" : ""
            return ("Downloading \(name)\(percent)", .info, false)
        }
    }

    /// Poll the download every few seconds until it ends. A status read that fails keeps the last sentence and
    /// tries again (the Mac may be busy); the page reloads once at the end so the model shows as installed.
    private static func follow(_ machine: FleetMachine, _ name: String, base: String, run: String,
                               last: [String: Any], misses: Int = 0, done: @escaping () -> Void) {
        let (text, health, finished) = sentence(name, last)
        set(machine, text, health)
        if finished { done(); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            GatewayAPI.get("\(base)/model-downloads/\(run)", timeout: 30) { result in
                DispatchQueue.main.async {
                    switch result {
                    case .success(let answer):
                        follow(machine, name, base: base, run: run, last: answer["run"] as? [String: Any] ?? last, done: done)
                    case .failure(let error) where misses >= 20:
                        set(machine, "Lost track of \(name): \(error.localizedDescription)", .warn)
                    case .failure:
                        follow(machine, name, base: base, run: run, last: last, misses: misses + 1, done: done)
                    }
                }
            }
        }
    }
}

/// The row's install line, kept current from `FleetModelInstall.status`; hidden when there is nothing to say.
final class FleetModelInstallLabel: NSTextField {
    private let key: String

    init(machine: FleetMachine) {
        key = FleetModelInstall.key(machine)
        super.init(frame: .zero)
        isEditable = false
        isBordered = false
        drawsBackground = false
        font = TextRole.caption.font
        lineBreakMode = .byTruncatingMiddle   // a narrow column keeps the percent and the result's end
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        refresh()
        NotificationCenter.default.addObserver(self, selector: #selector(changed(_:)), name: FleetModelInstall.changed,
                                               object: nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    deinit { NotificationCenter.default.removeObserver(self) }

    @objc private func changed(_ note: Notification) {
        if note.object as? String == key { refresh() }
    }

    private func refresh() {
        let line = FleetModelInstall.status[key]
        stringValue = line?.text ?? ""
        toolTip = line?.text
        textColor = line.map { $0.health == .info ? .secondaryLabelColor : $0.health.text } ?? .secondaryLabelColor
        isHidden = line == nil
    }
}
