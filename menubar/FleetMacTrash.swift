// macOS's own Trash setting on the Fleet page (owner request 2026-10-19): the per-Mac "Turn on" and the
// bulk "Turn on for all Macs that are Off". The pure half - the badge, the tooltip and which Macs a bulk
// run would ask - is in FleetMacTrashModel.swift, so the layout test can use it without this file.
//
// **One way only**: the Hub can turn this on and has no route that turns it off, so nothing here turns it
// off. The Machines tab only gets a cell closure and a bar injected by `FleetSection`, the way startup
// control does.

import AppKit

extension FleetMacTrash {
    // MARK: Requests

    private static func path(_ machine: FleetMachine) -> String? {
        guard let location = machine.locationId, let id = machine.machine,
              let place = location.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)/macos-trash-auto-empty/enable"
    }

    /// Turn it on for one Mac, through its controller. No body: there is nothing to choose.
    static func enable(_ machine: FleetMachine, done: @escaping () -> Void) {
        let name = machine.label ?? machine.machine ?? "this Mac"
        guard let path = path(machine) else { return done() }
        GatewayAPI.post(path, body: [:], timeout: 120) { result in
            DispatchQueue.main.async {
                switch result {
                case .success(let payload):
                    if payload["macos_trash_auto_empty"] as? Bool == true {
                        show("macOS Trash on \(name)",
                             "Finder will now remove items from the Trash after 30 days.")
                    } else {
                        show("macOS Trash was not turned on", payload["detail"] as? String
                             ?? "The Mac wrote the setting but did not read it back as on.")
                    }
                case .failure(let error):
                    show("Could not turn on macOS's Trash setting for \(name)", error.localizedDescription)
                }
                done()
            }
        }
    }

    /// "Turn on for all Macs that are Off", with a confirm that names every Mac it will touch and every
    /// one it will not. The gateway asks each Mac's controller in turn; a Mac that is not answering is
    /// listed with why and skipped, so it never holds up the rest.
    static func enableEverywhere(machines: [FleetMachine], done: @escaping () -> Void) {
        let asked = targets(machines)
        let away = unreachable(machines)
        guard !asked.isEmpty else {
            show("Nothing to turn on",
                 away.isEmpty
                    ? "Every Mac that reported this setting already empties its Trash after 30 days."
                    : "\(away.count) Mac\(away.count == 1 ? "" : "s") report this setting off but are not "
                        + "answering now. Nothing was sent to them - start their services once, then ask again.")
            return
        }
        let sure = NSAlert()
        sure.messageText = "Turn on macOS's 30-day Trash emptying for \(asked.count) Mac\(asked.count == 1 ? "" : "s")?"
        sure.informativeText = "Finder on those Macs will remove items from the Trash after 30 days. "
            + (away.isEmpty ? "" : "\(away.count) more are not answering and will be skipped, not saved for. ")
            + "This is macOS's own setting. Nothing is emptied now, and nothing is turned off."
        sure.accessoryView = FleetTrash.listView(
            asked.map { "\(name($0)): will be turned on" }
            + away.map { "\(name($0)): skipped - not answering" })
        sure.addButton(withTitle: "Turn On")
        sure.addButton(withTitle: "Cancel")
        guard sure.runModal() == .alertFirstButtonReturn else { return }
        GatewayAPI.post("/admin/api/fleet/trash/enable-macos-auto-empty", body: [:], timeout: 300) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    show("Could not turn it on", error.localizedDescription)
                case .success(let payload):
                    let applied = (payload["applied"] as? [Any])?.count ?? 0
                    let failed = (payload["failed"] as? [[String: Any]]) ?? []
                    let missed = (payload["skipped"] as? [Any])?.count ?? 0
                    var text = "\(applied) Mac\(applied == 1 ? "" : "s") now empty their Trash after 30 days."
                    if missed > 0 { text += "\n\(missed) not answering, skipped." }
                    if !failed.isEmpty {
                        text += "\n\n\(failed.count) could not be changed:\n" + failed.prefix(8).map {
                            "• \($0["label"] as? String ?? "?"): "
                                + "\($0["detail"] as? String ?? $0["code"] as? String ?? "refused")"
                        }.joined(separator: "\n")
                    }
                    show("macOS Trash", text)
                }
                done()
            }
        }
    }

    private static func name(_ machine: FleetMachine) -> String {
        machine.label ?? machine.machine ?? "this Mac"
    }

    private static func show(_ title: String, _ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = detail
        alert.closeOnEscape()
        alert.runModal()
    }

    // MARK: Table cell

    /// The "Turn on" button, or nil when there is nothing to do: a Mac that is already on has nothing to
    /// change, and one that is not answering cannot be asked (its tooltip says so).
    static func button(for machine: FleetMachine, done: @escaping () -> Void) -> NSButton? {
        guard targets([machine]).count == 1 else { return nil }
        return FleetClosureButton(title: "Turn on") { enable(machine, done: done) }
    }
}

/// Badge + "Turn on" in one table cell, laid out like the Startup cell.
final class FleetMacTrashCell: NSView {
    init(machine: FleetMachine, done: @escaping () -> Void) {
        super.init(frame: .zero)
        let (health, text) = FleetMacTrash.health(machine.startup)
        let tip = FleetMacTrash.tooltip(machine.startup, name: machine.label ?? machine.machine ?? "This Mac")
        let badge = Badge(text, health)
        badge.toolTip = tip
        toolTip = tip
        var views: [NSView] = [badge]
        if let button = FleetMacTrash.button(for: machine, done: done) { views.append(button) }
        let line = NSStackView(views: views)
        line.spacing = DS.Space.xs
        line.alignment = .centerY
        line.translatesAutoresizingMaskIntoConstraints = false
        addSubview(line)
        NSLayoutConstraint.activate([
            line.leadingAnchor.constraint(equalTo: leadingAnchor, constant: DS.Space.xs),
            line.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -DS.Space.xs),
            line.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }
}

/// The slim bar above the Machines table: the count and the one bulk button.
final class FleetMacTrashBar: NSStackView {
    private let label = NSTextField(wrappingLabelWithString: "")
    private lazy var button = FleetClosureButton(title: "Turn On for All Macs That Are Off") { [weak self] in
        guard let self else { return }
        FleetMacTrash.enableEverywhere(machines: self.machines) { self.onChange?() }
    }
    private var machines: [FleetMachine] = []
    /// Called after a bulk change so the page can reload.
    var onChange: (() -> Void)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        orientation = .horizontal
        alignment = .centerY
        spacing = DS.Space.m
        label.font = TextRole.secondary.font
        label.textColor = .secondaryLabelColor
        label.preferredMaxLayoutWidth = PageKit.textWidth
        addArrangedSubview(label)
        addArrangedSubview(button)
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func update(machines: [FleetMachine]) {
        self.machines = machines
        let off = FleetMacTrash.offCount(machines)
        let away = FleetMacTrash.unreachable(machines).count
        let unknown = machines.filter { $0.startup?.macosTrashAutoEmpty == nil }.count
        var parts: [String] = []
        if off + away > 0 {
            parts.append("\(off + away) Mac\(off + away == 1 ? "" : "s") never empty their Trash by themselves")
        }
        if unknown > 0 { parts.append("\(unknown) not reporting (older Studio Hub or unreachable)") }
        label.stringValue = parts.joined(separator: " · ")
        label.textColor = off + away > 0 ? Health.warn.text : .secondaryLabelColor
        button.isEnabled = off > 0
        isHidden = parts.isEmpty
    }
}
