// Per-Mac function priority from the Gateway Fleet page.
//
// The Hub owns the function vocabulary. This editor renders the ids and titles returned by that
// Mac, keeps unchecked functions tied below the checked list, and sends only `priority_order`.

import AppKit

/// The one place a rankable function gets a name when its Studio Hub has not supplied one.
///
/// `render` is final video assembly and has no worker row, so there is no title to read from a
/// Studio. Every other id is the Hub's own title, and a Hub that predates a function simply never
/// lists it. Kept here rather than inlined into "the".capitalized: "render" capitalised is not
/// the words the owner asked for (owner, 2026-10-28: plain words, "Final render").
enum FleetPriorityNames {
    static func title(for id: String, from hub: String?) -> String {
        let given = hub?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let given, !given.isEmpty { return given }
        return id == "render" ? "Final render" : id.capitalized
    }
}

struct FleetPriorityFunction: Equatable {
    let id: String
    let title: String
    /// nil when an older Hub does not state studio enablement for this function.
    let enabled: Bool?
}

struct FleetWorkPriorityPolicy {
    let functions: [FleetPriorityFunction]
    let order: [String]
    let machineEnabled: Bool?

    init?(_ payload: [String: Any]) {
        guard let rawFunctions = payload["priority_functions"] as? [[String: Any]],
              let rawOrder = payload["priority_order"] as? [String] else { return nil }
        let studios = payload["studios"] as? [[String: Any]] ?? []
        var seen = Set<String>()
        functions = rawFunctions.compactMap { row in
            guard let id = row["id"] as? String, !id.isEmpty, seen.insert(id).inserted else { return nil }
            let title = (row["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
            let matching = studios.filter { ($0["modality"] as? String) == id }
            let enabled = matching.isEmpty ? nil : matching.contains { ($0["enabled"] as? Bool) == true }
            return FleetPriorityFunction(
                id: id,
                title: FleetPriorityNames.title(for: id, from: title),
                enabled: enabled
            )
        }
        guard functions.count == rawFunctions.count,
              Set(rawOrder).count == rawOrder.count,
              Set(rawOrder).isSubset(of: Set(functions.map(\.id))) else { return nil }
        order = rawOrder
        machineEnabled = payload["machine_enabled"] as? Bool
    }
}

/// Accessible, button-based ordering. Dragging is deliberately unnecessary: every row has named
/// Up and Down buttons, and the checkbox says whether the function is ranked or tied below it.
final class FleetWorkPriorityForm: NSObject {
    private(set) var functions: [FleetPriorityFunction]
    private var included: Set<String>
    private var hasCustomSelection: Bool
    private let noPreference = NSButton(radioButtonWithTitle: "No preference — all enabled functions are equal",
                                        target: nil, action: nil)
    private let custom = NSButton(radioButtonWithTitle: "Custom order", target: nil, action: nil)
    private let rows = NSStackView()
    let view: NSView

    init(_ policy: FleetWorkPriorityPolicy) {
        included = Set(policy.order)
        hasCustomSelection = !policy.order.isEmpty
        let byID = Dictionary(uniqueKeysWithValues: policy.functions.map { ($0.id, $0) })
        functions = policy.order.compactMap { byID[$0] }
        functions.append(contentsOf: policy.functions.filter { !policy.order.contains($0.id) })

        let explanation = PageKit.label(
            "For a custom order, checked functions run first from top to bottom. Unchecked functions "
                + "remain tied below them.",
            .body, colour: .secondaryLabelColor, wrapping: true
        )
        explanation.preferredMaxLayoutWidth = 460
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = DS.Space.s
        let mode = NSStackView(views: [noPreference, custom])
        mode.orientation = .vertical
        mode.alignment = .leading
        mode.spacing = DS.Space.s
        let stack = NSStackView(views: [mode, explanation, rows])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.m
        stack.widthAnchor.constraint(equalToConstant: 480).isActive = true
        view = stack
        super.init()
        noPreference.target = self
        noPreference.action = #selector(modeChanged(_:))
        custom.target = self
        custom.action = #selector(modeChanged(_:))
        noPreference.state = hasCustomSelection ? .off : .on
        custom.state = hasCustomSelection ? .on : .off
        rebuild()
        stack.layoutSubtreeIfNeeded()
        stack.frame = NSRect(origin: .zero, size: stack.fittingSize)
    }

    var priorityOrder: [String] {
        custom.state == .on ? functions.filter { included.contains($0.id) }.map(\.id) : []
    }

    private func rebuild() {
        rows.arrangedSubviews.forEach { view in
            rows.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        for (index, function) in functions.enumerated() {
            let suffix = function.enabled == false ? " — Disabled" : ""
            let check = NSButton(checkboxWithTitle: function.title + suffix,
                                 target: self, action: #selector(toggle(_:)))
            check.identifier = NSUserInterfaceItemIdentifier(function.id)
            check.state = included.contains(function.id) ? .on : .off
            check.toolTip = (function.enabled == false ? "Disabled now. Its saved rank is kept for when it is enabled. " : "")
                + "Rank \(function.title); unchecked functions stay tied below ranked work."
            check.setAccessibilityLabel("Prioritize \(function.title)")
            check.lineBreakMode = .byTruncatingTail
            check.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            check.widthAnchor.constraint(equalToConstant: 300).isActive = true

            let previous = functions.indices[..<index].last { functions[$0].enabled != false }
            let following = functions.indices[(index + 1)...].first { functions[$0].enabled != false }
            let up = NSButton(title: "Up", target: self, action: #selector(moveUp(_:)))
            up.tag = index
            up.controlSize = .small
            up.bezelStyle = .rounded
            up.setAccessibilityLabel("Move \(function.title) up")
            let down = NSButton(title: "Down", target: self, action: #selector(moveDown(_:)))
            down.tag = index
            down.controlSize = .small
            down.bezelStyle = .rounded
            down.setAccessibilityLabel("Move \(function.title) down")

            let editable = custom.state == .on && function.enabled != false
            check.isEnabled = editable
            up.isEnabled = editable && previous != nil
            down.isEnabled = editable && following != nil

            let row = NSStackView(views: [check, NSView(), up, down])
            row.orientation = .horizontal
            row.alignment = .centerY
            row.spacing = DS.Space.s
            row.widthAnchor.constraint(equalToConstant: 480).isActive = true
            rows.addArrangedSubview(row)
        }
    }

    @objc private func toggle(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue else { return }
        if sender.state == .on { included.insert(id) } else { included.remove(id) }
    }

    @objc private func modeChanged(_ sender: NSButton) {
        let choseCustom = sender === custom
        noPreference.state = choseCustom ? .off : .on
        custom.state = choseCustom ? .on : .off
        if choseCustom && !hasCustomSelection {
            included = Set(functions.filter { $0.enabled != false }.map(\.id))
            hasCustomSelection = true
        }
        rebuild()
    }

    @objc private func moveUp(_ sender: NSButton) {
        guard functions.indices.contains(sender.tag), functions[sender.tag].enabled != false,
              let previous = functions.indices[..<sender.tag].last(where: {
                  functions[$0].enabled != false
              }) else { return }
        functions.swapAt(sender.tag, previous)
        rebuild()
    }

    @objc private func moveDown(_ sender: NSButton) {
        guard functions.indices.contains(sender.tag), functions[sender.tag].enabled != false,
              let following = functions.indices[(sender.tag + 1)...].first(where: {
                  functions[$0].enabled != false
              }) else { return }
        functions.swapAt(sender.tag, following)
        rebuild()
    }
}

enum FleetWorkPriority {
    private static func path(_ machine: FleetMachine) -> String? {
        guard let location = machine.locationId, let id = machine.machine,
              let place = location.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)/work-policy"
    }

    static func edit(_ machine: FleetMachine, done: @escaping () -> Void) {
        guard let path = path(machine) else { return }
        GatewayAPI.get(path, timeout: 30) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    FleetStartup.show("Work Priorities unavailable", error.localizedDescription)
                case .success(let payload):
                    guard let policy = FleetWorkPriorityPolicy(payload) else {
                        let saved = (machine.priorityOrder ?? [])
                            .map { FleetPriorityNames.title(for: $0, from: nil) }
                            .joined(separator: ", ")
                        FleetStartup.show(
                            "Work Priorities are read-only",
                            "This Mac needs a newer Studio Hub before the Gateway can edit its function order."
                                + (saved.isEmpty ? "" : " Saved order: \(saved).")
                        )
                        return
                    }
                    guard !policy.functions.isEmpty else {
                        FleetStartup.show(
                            "No local functions to rank",
                            "Enable at least one Studio on this Mac, then open Work Priorities again."
                        )
                        return
                    }
                    present(machine, path: path, policy: policy, done: done)
                }
            }
        }
    }

    private static func present(_ machine: FleetMachine, path: String,
                                policy: FleetWorkPriorityPolicy, done: @escaping () -> Void) {
        let form = FleetWorkPriorityForm(policy)
        let alert = NSAlert()
        alert.messageText = "Work Priorities — \(machine.label ?? machine.machine ?? "this Mac")"
        alert.informativeText = (policy.machineEnabled == false
            ? "This Mac is disabled now; its saved order is kept for when it is enabled. " : "")
            + "The current job always finishes. This order applies when the Mac is free."
        alert.accessoryView = form.view
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        GatewayAPI.post(path, body: ["priority_order": form.priorityOrder], timeout: 30) { result in
            DispatchQueue.main.async {
                if case .failure(let error) = result {
                    FleetStartup.show("Could not save Work Priorities", error.localizedDescription)
                }
                done()
            }
        }
    }
}
