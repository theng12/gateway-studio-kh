// Startup control on the Fleet page: the buttons, requests and bulk bar that act on what a Mac reports.
//
// **Self-contained on purpose.** The decoded fact, badge text and "which buttons" logic are in
// FleetStartupModel.swift; this file is everything that talks to the gateway or shows a window: the
// per-machine action cell, the confirm-then-force flow for a restart that meets running work, and the
// bulk "Enable auto-start" bar. The Machines tab only gets a cell closure and an accessory view injected
// by `FleetSection`, so merging with the Fleet-tab work on feat/disk-guard is a column list and two lines,
// and the Groups view reuses `FleetStartup.cell` and `FleetStartupBar` as they are.

import AppKit

extension FleetStartup {
    // MARK: Requests

    /// What a request answered, in words the owner can act on.
    struct Outcome {
        let ok: Bool
        let sentence: String
        /// A restart refused because work is running: the caller may ask whether to force it.
        let workInProgress: Bool
    }

    static func request(_ machine: FleetMachine, _ action: FleetStartupAction, force: Bool = false,
                        completion: @escaping (Outcome) -> Void) {
        guard let location = machine.locationId, let id = machine.machine,
              let place = location.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else {
            completion(Outcome(ok: false, sentence: "This row has no machine id.", workInProgress: false))
            return
        }
        // A restart waits for the app on that Mac to collect it: give it the time.
        GatewayAPI.post("/admin/api/fleet/controllers/\(place)/machines/\(node)/startup",
                        body: action.body(force: force), timeout: 45) { result in
            DispatchQueue.main.async {
                switch result {
                case .success(let payload): completion(outcome(from: payload, action: action))
                case .failure(let error):
                    let refused = (error as? GatewayAPIError).flatMap { error -> Bool? in
                        if case .refused(let code, let detail) = error {
                            // The Hub's refusal for running work is the one 409 that offers `force: true`.
                            return code == 409 && detail.contains("force: true")
                        }
                        return nil
                    } ?? false
                    completion(Outcome(ok: false, sentence: error.localizedDescription, workInProgress: refused))
                }
            }
        }
    }

    private static func outcome(from payload: [String: Any], action: FleetStartupAction) -> Outcome {
        let state = payload["state"] as? String ?? ""
        switch state {
        case "applied": return Outcome(ok: true, sentence: "\(action.title): done.", workInProgress: false)
        case "queued":
            return Outcome(ok: true, sentence: "\(action.title): sent. The Mac applies it on its next check.",
                           workInProgress: false)
        case "handed_to_app":
            return Outcome(ok: true, sentence: "\(action.title): the Mac is restarting its services.",
                           workInProgress: false)
        case "already_running":
            return Outcome(ok: true, sentence: "Its services are already running.", workInProgress: false)
        default:
            let detail = payload["detail"] as? String ?? "The Mac did not accept the request."
            return Outcome(ok: false, sentence: detail, workInProgress: false)
        }
    }

    /// Run one action, asking before a restart that would interrupt work. `done` fires on the main
    /// thread after any outcome, so the page can reload.
    static func run(_ machine: FleetMachine, _ action: FleetStartupAction, in window: NSWindow?,
                    done: @escaping () -> Void) {
        let name = machine.label ?? machine.machine ?? "this Mac"
        if action == .restartServices || action == .startServices {
            let sure = NSAlert()
            sure.messageText = "\(action.title) on \(name)?"
            sure.informativeText = "Its Studio Hub restarts its Studios. It refuses if a job is running, "
                + "and asks you again before interrupting one."
            sure.addButton(withTitle: action.title)
            sure.addButton(withTitle: "Cancel")
            guard sure.runModal() == .alertFirstButtonReturn else { return }
        }
        request(machine, action) { first in
            if first.ok || !first.workInProgress {
                if !first.ok { show("\(action.title) failed on \(name)", first.sentence) }
                done()
                return
            }
            let force = NSAlert()
            force.alertStyle = .warning
            force.messageText = "\(name) is busy"
            force.informativeText = first.sentence + "\n\nRestart anyway and interrupt that work?"
            force.addButton(withTitle: "Wait")
            force.addButton(withTitle: "Restart anyway")
            guard force.runModal() == .alertSecondButtonReturn else { done(); return }
            request(machine, action, force: true) { forced in
                if !forced.ok { show("\(action.title) failed on \(name)", forced.sentence) }
                done()
            }
        }
    }

    static func show(_ title: String, _ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = title
        alert.informativeText = detail
        alert.runModal()
    }

    // MARK: Table cell

    /// The row's buttons, for a column with `cell:` set. Empty (with the reason in the badge tooltip)
    /// for a Mac nothing can be sent to.
    static func cell(for machine: FleetMachine, window: @escaping () -> NSWindow?,
                     done: @escaping () -> Void) -> NSView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 4
        for action in actions(for: machine) {
            stack.addArrangedSubview(FleetClosureButton(title: action.title) {
                run(machine, action, in: window(), done: done)
            })
        }
        return stack
    }

    // MARK: Bulk

    /// "Enable auto-start on all Macs where it's off", with a count and a confirm that lists what it will
    /// and will not do. The answer names every Mac, so nothing is fixed silently.
    static func enableEverywhere(machines: [FleetMachine], done: @escaping () -> Void) {
        let count = offCount(machines)
        let unreachable = offUnreachableCount(machines)
        guard count > 0 else {
            show("Nothing to enable",
                 unreachable > 0
                    ? "\(unreachable) Mac\(unreachable == 1 ? "" : "s") report auto-start off but are not answering now. "
                        + "A Mac with no Studio Hub running needs its services started by hand once."
                    : "Every Mac that reported its setting already starts its services after a restart.")
            return
        }
        let sure = NSAlert()
        sure.messageText = "Turn on \"Start services on startup\" for \(count) Mac\(count == 1 ? "" : "s")?"
        sure.informativeText = "After a restart those Macs will bring Studio Hub's services up by themselves. "
            + (unreachable > 0 ? "\(unreachable) more are not answering and will be skipped. " : "")
            + "Nothing is restarted now."
        sure.addButton(withTitle: "Enable auto-start")
        sure.addButton(withTitle: "Cancel")
        guard sure.runModal() == .alertFirstButtonReturn else { return }
        GatewayAPI.post("/admin/api/fleet/startup/enable-autostart", body: [:], timeout: 120) { result in
            DispatchQueue.main.async {
                switch result {
                case .failure(let error):
                    show("Enable auto-start failed", error.localizedDescription)
                case .success(let payload):
                    let applied = (payload["applied"] as? [Any])?.count ?? 0
                    let failed = (payload["failed"] as? [[String: Any]]) ?? []
                    let skipped = (payload["skipped"] as? [Any])?.count ?? 0
                    var text = "\(applied) Mac\(applied == 1 ? "" : "s") will now start their services after a restart."
                    if skipped > 0 { text += "\n\(skipped) not answering, skipped." }
                    if !failed.isEmpty {
                        text += "\n\n\(failed.count) could not be changed:\n" + failed.prefix(8).map {
                            "• \($0["label"] as? String ?? "?"): \($0["detail"] as? String ?? $0["code"] as? String ?? "refused")"
                        }.joined(separator: "\n")
                    }
                    show("Enable auto-start", text)
                }
                done()
            }
        }
    }
}

/// A slim bar for the top of the Machines tab (and the Groups view): the count and the one button.
final class FleetStartupBar: NSStackView {
    private let label = NSTextField(wrappingLabelWithString: "")
    private lazy var button = FleetClosureButton(title: "Enable auto-start on all Macs where it's off") { [weak self] in
        guard let self else { return }
        FleetStartup.enableEverywhere(machines: self.machines) { self.onChange?() }
    }
    private var machines: [FleetMachine] = []
    /// Called after a bulk change so the page can reload.
    var onChange: (() -> Void)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        orientation = .horizontal
        alignment = .centerY
        spacing = 10
        label.font = .systemFont(ofSize: 11.5)
        label.textColor = .secondaryLabelColor
        addArrangedSubview(label)
        addArrangedSubview(button)
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func update(machines: [FleetMachine]) {
        self.machines = machines
        let off = FleetStartup.offCount(machines)
        let offAway = FleetStartup.offUnreachableCount(machines)
        let unknown = machines.filter { $0.startup?.startServicesOnLaunch == nil }.count
        var parts: [String] = []
        if off + offAway > 0 {
            parts.append("\(off + offAway) Mac\(off + offAway == 1 ? "" : "s") will not start their services after a restart")
        }
        if unknown > 0 { parts.append("\(unknown) not reporting (older Studio Hub or unreachable)") }
        label.stringValue = parts.joined(separator: " · ")
        label.textColor = off + offAway > 0 ? .systemOrange : .secondaryLabelColor
        button.isEnabled = off > 0
        isHidden = parts.isEmpty
    }
}
