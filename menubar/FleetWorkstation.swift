// Lite Mode (internally "workstation"): a shop's own Mac lends the fleet its idle time (owner request 2026-10-03).
//
// Studio Hub reports each Mac's settings and state as `workstation` on its machine row (the Gateway republishes
// a checked copy); `nil` means that Mac's Hub is too old for it. A Mac the shop is using, one that just stepped
// aside, or one in its quiet hours is busy *for now*: grey like a paused Mac, never offline, never "needs
// attention". The settings sheet below is built here (AppKit only, no networking) so the render harness can draw
// it; `FleetStartup.editWorkstation` runs it and sends the change through the Mac's controller.

import AppKit

struct FleetWorkstation: Decodable {
    var enabled: Bool?
    var idleOnly: Bool?
    var idleMinutes: Int?
    var stepAside: Bool?
    var resumeAfterMinutes: Int?
    /// Free memory needed to start a job on this Mac; `nil` is each model's own floor.
    var startFreeGb: Double?
    var quietHours: [String]?
    var hideMenuBarIcon: Bool?
    /// off, working, standing_by, yielded, quiet_hours - or nil when the Hub said something else.
    var state: String?
    /// The Hub's few words for why ("someone is using this Mac").
    var reason: String?
    var yieldsToday: Int?
    var jobsToday: Int?
    /// True when the controller is repeating what an unreachable Mac last said.
    var stale: Bool?

    /// The shop has this Mac for now. A stale report says nothing about now.
    var isResting: Bool { stale != true && ["standing_by", "yielded", "quiet_hours"].contains(state ?? "") }

    /// The state chip, short enough for the Machines table's State column at 1100 pt; nil when the mode is off or
    /// the state is unknown.
    var chip: String? {
        switch state {
        case "working": return "Working"
        case "standing_by": return "Standing by"
        case "yielded": return "Stepped aside"
        case "quiet_hours": return "Quiet hours"
        default: return nil
        }
    }

    /// The chip's full wording for a tooltip or a sentence.
    var chipLong: String? { state == "standing_by" ? "In use — standing by" : chip }

    /// What this Mac does with the shop's own use, said from its settings - so the sheet never claims "only when the
    /// shop is not using it" for a Mac set to work through it.
    var policySentence: String {
        guard enabled == true else { return "Lite Mode is off, so this Mac takes fleet work whenever it is free." }
        let minutes = idleMinutes ?? 3
        let unit = minutes == 1 ? "minute" : "minutes"
        switch (idleOnly == true, stepAside == true) {
        case (true, true):
            return "This Mac takes fleet work only after \(minutes) \(unit) without input, and if someone starts using it "
                + "it finishes its current job, then pauses new work."
        case (true, false):
            return "This Mac takes fleet work only after \(minutes) \(unit) without input."
        case (false, true):
            return "This Mac takes fleet work at background priority; while someone is using it, it finishes its current "
                + "job, then pauses new work."
        case (false, false):
            return "This Mac takes fleet work at background priority, even while someone is using it."
        }
    }

    /// Grey for the shop's time (as a paused Mac is), green while it takes work.
    var chipHealth: Health { isResting ? .off : .ok }

    /// "Today: 3 jobs · 1 step-aside", once the mode is on.
    var today: String? {
        guard enabled == true else { return nil }
        let jobs = jobsToday ?? 0, yields = yieldsToday ?? 0
        return "Today: \(jobs) job\(jobs == 1 ? "" : "s") · \(yields) step-aside\(yields == 1 ? "" : "s")"
    }
}

/// A Lite Mode change saved on the Gateway while the Mac was offline (owner request 2026-10-04); the Gateway sends
/// it when the Mac is back. `settings` carries only the keys that were changed.
struct FleetLiteModePending: Decodable {
    var settings: FleetWorkstation?
    var at: Double?
    var offlineSince: Double?

    /// "Waiting to apply (offline since Oct 2, 09:36)".
    var line: String { "Waiting to apply" + (offlineSince.map { " (offline since \(FleetOffline.clock($0)))" } ?? "") }
}

extension FleetWorkstation {
    /// These settings with a waiting change laid over them, so the sheet opens on what the Mac will get.
    func overlaid(by pending: FleetWorkstation?) -> FleetWorkstation {
        guard let pending else { return self }
        var out = self
        out.enabled = pending.enabled ?? enabled
        out.idleOnly = pending.idleOnly ?? idleOnly
        out.idleMinutes = pending.idleMinutes ?? idleMinutes
        out.stepAside = pending.stepAside ?? stepAside
        out.resumeAfterMinutes = pending.resumeAfterMinutes ?? resumeAfterMinutes
        out.startFreeGb = pending.startFreeGb   // the sheet sends every key, so nil here is "each model's own"
        out.quietHours = pending.quietHours ?? quietHours
        out.hideMenuBarIcon = pending.hideMenuBarIcon ?? hideMenuBarIcon
        return out
    }
}

/// The Lite Mode sheet's controls, filled from what the Mac reported, and the change they describe.
final class FleetWorkstationForm: NSObject {
    static let idleHint = "Takes work only after this many minutes with no keyboard or mouse use, or while the screen is locked."
    static let stepAsideHint = "When someone uses the keyboard or mouse, this Mac finishes its current job, then takes no "
        + "new work until it has had this many minutes without input."
    static let startFreeHint = "A job starts only when this much memory is free. A running job always finishes; it is "
        + "stopped only if macOS reports critical memory pressure for 20 seconds (very rare), and then moves to another Mac."
    static let quietHint = "No work during these hours. Separate several with commas, like 22:00-07:00, 12:00-13:00."

    let enabled = NSButton(checkboxWithTitle: "Lite Mode", target: nil, action: nil)
    let idleOnly = NSButton(checkboxWithTitle: "Only when idle", target: nil, action: nil)
    let idleMinutes = NSTextField(string: "")
    let stepAside = NSButton(checkboxWithTitle: "Step aside when someone uses it", target: nil, action: nil)
    let resumeMinutes = NSTextField(string: "")
    let startFree = NSPopUpButton(frame: .zero, pullsDown: false)
    let startFreeGb = NSTextField(string: "")
    let quietHours = NSTextField(string: "")
    let hideIcon = NSButton(checkboxWithTitle: "Hide the Studio Hub menu-bar icon", target: nil, action: nil)
    let view: NSView

    private static let width: CGFloat = 460

    init(_ current: FleetWorkstation) {
        enabled.state = current.enabled == true ? .on : .off
        idleOnly.state = current.idleOnly == true ? .on : .off
        idleMinutes.stringValue = String(current.idleMinutes ?? 3)
        stepAside.state = current.stepAside == true ? .on : .off
        resumeMinutes.stringValue = String(current.resumeAfterMinutes ?? 2)
        startFree.addItems(withTitles: ["Each model's own (about 2.8 GB)", "Custom"])
        startFree.selectItem(at: current.startFreeGb == nil ? 0 : 1)
        startFreeGb.stringValue = current.startFreeGb.map { String(format: "%g", $0) } ?? ""
        startFreeGb.placeholderString = "2.8"
        quietHours.stringValue = (current.quietHours ?? []).joined(separator: ", ")
        quietHours.placeholderString = "22:00-07:00"
        hideIcon.state = current.hideMenuBarIcon == true ? .on : .off
        enabled.font = TextRole.rowTitle.font

        func field(_ field: NSTextField, _ width: CGFloat, numeric: Bool = true) -> NSTextField {
            field.widthAnchor.constraint(equalToConstant: width).isActive = true
            if numeric { field.alignment = .right }
            return field
        }
        func text(_ text: String) -> NSTextField {
            let label = PageKit.label(text, .body)
            label.setContentCompressionResistancePriority(.required, for: .horizontal)
            return label
        }
        /// A control line under its heading, indented to the checkbox's text.
        func indented(_ views: [NSView]) -> NSView {
            let row = NSStackView(views: views)
            row.spacing = DS.Space.s
            row.alignment = .firstBaseline
            row.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 0, right: 0)
            return row
        }
        func hint(_ text: String) -> NSView {
            let label = PageKit.label(text, .caption, colour: .secondaryLabelColor, wrapping: true)
            label.preferredMaxLayoutWidth = Self.width - 20
            return indented([label])
        }
        /// One setting: its heading, its control line, its hint, kept together.
        func group(_ views: [NSView]) -> NSView {
            let stack = NSStackView(views: views)
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.xs
            return stack
        }

        let stack = NSStackView(views: [
            enabled,
            group([idleOnly, indented([text("Idle for"), field(idleMinutes, 44), text("minutes")]), hint(Self.idleHint)]),
            group([stepAside, indented([text("Resume after"), field(resumeMinutes, 44), text("minutes without input")]),
                   hint(Self.stepAsideHint)]),
            group([text("Free memory needed to start a job"),
                   indented([startFree, field(startFreeGb, 52), text("GB")]), hint(Self.startFreeHint)]),
            group([text("Quiet hours"), indented([field(quietHours, 240, numeric: false)]), hint(Self.quietHint)]),
            hideIcon,
        ])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.m
        stack.widthAnchor.constraint(equalToConstant: Self.width).isActive = true
        view = stack
        super.init()
        startFree.target = self
        startFree.action = #selector(startFreeChanged)
        startFreeChanged()
        stack.layoutSubtreeIfNeeded()
        stack.frame = NSRect(origin: .zero, size: stack.fittingSize)
    }

    /// The custom figure only means something when "Custom" is chosen.
    @objc private func startFreeChanged() {
        startFreeGb.isEnabled = startFree.indexOfSelectedItem == 1
    }

    /// A value that does not parse, in a sentence to show.
    struct Invalid: Error { let sentence: String }

    /// The change to send: every setting, checked here so a typo never travels.
    func body() throws -> [String: Any] {
        func minutes(_ field: NSTextField, _ name: String) throws -> Int {
            guard let value = Int(field.stringValue.trimmingCharacters(in: .whitespaces)), (1...60).contains(value) else {
                throw Invalid(sentence: "\(name) must be a whole number of minutes from 1 to 60.")
            }
            return value
        }
        let idle = try minutes(idleMinutes, "Idle time"), resume = try minutes(resumeMinutes, "Resume time")
        var start: Any = NSNull()
        if startFree.indexOfSelectedItem == 1 {
            let text = startFreeGb.stringValue.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
            guard let gb = Double(text), gb >= 0.5, gb <= 8 else {
                throw Invalid(sentence: "Free memory needed to start a job must be from 0.5 to 8 GB.")
            }
            start = gb
        }
        let hours = quietHours.stringValue.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        let shape = #"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$"#
        if let bad = hours.first(where: { $0.range(of: shape, options: .regularExpression) == nil }) {
            throw Invalid(sentence: "“\(bad)” is not a time range. Write quiet hours like 22:00-07:00.")
        }
        guard hours.count <= 6 else { throw Invalid(sentence: "Quiet hours can have at most 6 ranges.") }
        return ["enabled": enabled.state == .on, "idle_only": idleOnly.state == .on, "idle_minutes": idle,
                "step_aside": stepAside.state == .on, "resume_after_minutes": resume, "start_free_gb": start,
                "quiet_hours": hours, "hide_menu_bar_icon": hideIcon.state == .on]
    }

    /// The sheet: what the Mac is doing now, then its settings. Save is the blue default button.
    static func alert(for machine: FleetMachine, _ current: FleetWorkstation) -> (NSAlert, FleetWorkstationForm) {
        let pending = machine.liteModePending
        let form = FleetWorkstationForm(current.overlaid(by: pending?.settings))
        let alert = NSAlert()
        alert.messageText = "Lite Mode — \(machine.label ?? machine.machine ?? "this Mac")"
        var now = current.enabled == true ? (current.chipLong ?? "On") : "Off"
        if let reason = current.reason, current.enabled == true { now += " (\(reason))" }
        if current.stale == true { now += ", as last reported by the Mac" }
        alert.informativeText = (pending.map { "\($0.line). The settings below are saved and will be applied when "
            + "this Mac comes back.\n\n" } ?? "")
            + (machine.workstation == nil
               ? "This Mac is offline and has not reported its Lite Mode. A change is saved and applied when it comes back."
               : "\(current.policySentence) Now: \(now)." + (current.today.map { " \($0)." } ?? ""))
        alert.accessoryView = form.view
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        return (alert, form)
    }
}

extension FleetMachine {
    /// What the Lite Mode sheet opens on: the Mac's report, else (an offline Mac whose controller repeats nothing)
    /// the defaults, so a change can still be saved for it. `nil` only for a reachable Mac whose Hub is too old.
    var liteModeSheetStart: FleetWorkstation? {
        workstation ?? (reachable == false || (state ?? "").lowercased() == "offline" || liteModePending != nil
                        ? FleetWorkstation() : nil)
    }

    /// The State column's word when the shop has this Mac for now; nil otherwise.
    var workstationRestingLabel: String? { workstation?.isResting == true ? workstation?.chip : nil }

    /// The whole story in one sentence, for a tooltip: "Lite Mode: In use — standing by (someone is using
    /// this Mac). Today: 2 jobs · 0 step-asides."
    var workstationLine: String? {
        let waiting = liteModePending.map { "Lite Mode change: \($0.line)." }
        guard let workstation, workstation.enabled == true || workstation.isResting else { return waiting }
        var text = "Lite Mode: " + (workstation.chipLong ?? "on")
        if let reason = workstation.reason, workstation.isResting { text += " (\(reason))" }
        if let today = workstation.today { text += ". \(today)" }
        text += workstation.stale == true ? " (as last reported by the Mac)." : "."
        return waiting.map { "\(text)\n\($0)" } ?? text
    }

    /// Why the shop has this Mac now, for the "Doing now" column: the Hub's words, sentence-cased unless the first
    /// word carries its own capitals ("macOS ...").
    var workstationReasonText: String? {
        guard workstation?.isResting == true, let reason = workstation?.reason, let first = reason.first else { return nil }
        let word = reason.prefix { $0 != " " }
        return word.dropFirst().contains(where: \.isUppercase) ? reason : first.uppercased() + reason.dropFirst()
    }

    /// Memory as a fault for a group to count: the shop's own use of a Mac it is using is not one.
    var faultMemoryHealth: Health { workstation?.isResting == true ? .off : memoryHealth }
}
