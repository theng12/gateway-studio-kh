// The Fleet page's reachability history: offline now, how often it drops, and the likely cause.
//
// The owner, 2026-10-29: *"i want to be able to sort for the offline site. and also 'problematic'
// machine, that goes in and out over a period of time or sth. like a bad pattern. and also a way for
// me to see which ones could be the likely cause"*.
//
// **The Gateway decides, this draws.** `problematic` is its verdict against the owner's own threshold
// (Settings on the Fleet page, `/admin/api/settings/fleet-health`), and every cause arrives with the
// evidence it came from. Nothing here re-derives a verdict or a reason from a number, so this page
// and the browser console cannot say different things about the same Mac.
//
// A figure the Gateway did not measure stays `nil` and draws "—": "nobody looked" and "it is fine"
// must never be the same row (`Publish what is known, and nothing else`).
//
// Pure apart from the one small strip view, so the layout tests and the harness can drive it.

import AppKit

enum FleetHealth {
    /// The words the Machines tab's filter offers.
    enum Filter: String, CaseIterable {
        case all, offline, problematic

        var title: String {
            switch self {
            case .all: return "All machines"
            case .offline: return "Offline now"
            case .problematic: return "Problematic"
            }
        }
    }

    // MARK: A machine

    /// Is it offline **now**? `reachable` is the controller's answer; `state` is the Hub's word for the
    /// same Mac, and either saying so is enough. `nil` is unknown and never reads as offline.
    static func offlineNow(_ machine: FleetMachine) -> Bool {
        if machine.reachable == false { return true }
        return (machine.state ?? "").lowercased() == "offline"
    }

    static func matches(_ filter: Filter, machine: FleetMachine) -> Bool {
        switch filter {
        case .all: return true
        case .offline: return offlineNow(machine)
        case .problematic: return machine.history?.problematic == true
        }
    }

    static func matches(_ filter: Filter, location: FleetLocation) -> Bool {
        switch filter {
        case .all: return true
        case .offline: return location.reachable == false
        case .problematic: return location.history?.problematic == true
        }
    }

    /// What the Health cell says: the history's verdict, **not** a restatement of State.
    ///
    /// "Problematic" is the Gateway's threshold verdict. A Mac dropping less often says how often,
    /// because that is the number the owner will sort by. A Mac that is **down right now** says how
    /// long for — "Stable" on a Mac that has been offline for two days is exactly the kind of
    /// misleading green the owner asked this page for. A Mac nobody has observed says "—", which is
    /// not "stable".
    static func verdict(_ history: FleetHistory?, offline: Bool = false)
        -> (text: String, health: Health, tooltip: String) {
        guard let history, history.measured else {
            return ("—", .off, "This Gateway has no history for it yet — it cannot say whether it "
                + "drops, so it does not claim it is fine.")
        }
        var lines = [summary(history)]
        if let flips = history.flipsDay {
            lines.append("\(flips) up/down change\(flips == 1 ? "" : "s") in 24 h"
                + (history.flipsWeek.map { ", \($0) in 7 days" } ?? "") + ".")
        }
        if let drop = history.lastDrop, let text = drop.text, let at = drop.at {
            lines.append("Last drop \(FleetOffline.clock(at)): \(text).")
        }
        if let pattern = history.pattern, let text = pattern.text {
            lines.append(text + (pattern.evidence.map { " (\($0))" } ?? ""))
        }
        let tooltip = lines.joined(separator: "\n")
        if history.problematic == true {
            return ("Problematic", .warn, tooltip)
        }
        if offline {
            guard let since = history.offlineSince else {
                // Offline according to the row, and the history has not recorded the drop yet: say
                // what is known and nothing more. "Offline 0 s" is a number nobody measured.
                return ("Offline now", .bad,
                        tooltip + "\nIt is offline now; this Gateway has not recorded how long yet.")
            }
            let away = Date().timeIntervalSince1970 - since
            // The badge is a column wide; the tooltip carries the same fact in full words.
            return ("Offline \(compact(away))", .bad,
                    tooltip + "\nOffline for \(duration(away)), since \(FleetOffline.clock(since)).")
        }
        let drops = history.dropsDay ?? 0
        if drops > 0 {
            return ("\(drops) drop\(drops == 1 ? "" : "s")", .off, tooltip)
        }
        return ("Stable", .ok, tooltip)
    }

    /// The one-line caption under the strip: the 24 h figure and the week's offline time, because a
    /// column is about as wide as it can be and the strip says the rest.
    static func shortSummary(_ history: FleetHistory?) -> String {
        guard let history, history.measured else { return "Not measured yet" }
        guard let drops = history.dropsDay else { return "Not measured yet" }
        var parts = ["\(drops) in 24 h"]
        if let offline = history.offlineSecondsWeek, offline > 0 {
            parts.append("\(duration(offline)) off")
        }
        return parts.joined(separator: " · ")
    }

    /// Sort rank for the Health column, worst first when ascending: problematic, then anything that
    /// dropped, then stable, and the unmeasured last — never sorted as if it were healthy.
    static func rank(_ history: FleetHistory?) -> Int {
        guard let history, history.measured else { return 3 }
        if history.problematic == true { return 0 }
        return (history.dropsDay ?? 0) > 0 ? 1 : 2
    }

    /// When this Mac was last seen up, in epoch seconds — the history's own observation when it has
    /// one, else the controller's word about the node. `nil` when nobody said, which sorts last.
    static func lastSeen(_ machine: FleetMachine) -> Double? {
        if offlineNow(machine) {
            if let since = machine.history?.offlineSince { return since }
            return machine.nodeLastSeenAt?.doubleValue
        }
        return machine.history?.observedAt ?? machine.nodeLastSeenAt?.doubleValue
    }

    /// "3 h ago", "2 days ago", or "—" when nobody said.
    static func seenText(_ epoch: Double?, now: Date = Date()) -> String {
        guard let epoch, epoch > 0 else { return "—" }
        let seconds = now.timeIntervalSince1970 - epoch
        if seconds < 0 { return "just now" }
        if seconds < 90 { return "just now" }
        if seconds < 5400 { return "\(Int((seconds / 60).rounded())) min ago" }
        if seconds < 172_800 { return "\(Int((seconds / 3600).rounded())) h ago" }
        return "\(Int((seconds / 86_400).rounded())) days ago"
    }

    // MARK: The seven days

    /// One line of figures: "3 drops in 24 h · 9 in 7 days · 2 h offline in 7 days".
    static func summary(_ history: FleetHistory?) -> String {
        guard let history, history.measured else {
            return "Not measured yet — this Gateway has no history for it."
        }
        var parts: [String] = []
        if let drops = history.dropsDay, let week = history.dropsWeek {
            parts.append("\(drops) drop\(drops == 1 ? "" : "s") in 24 h")
            parts.append("\(week) in 7 days")
        } else {
            parts.append("Drops not recorded")
        }
        if let offline = history.offlineSecondsWeek {
            parts.append("\(duration(offline)) offline in 7 days")
        }
        if history.problematic == true { parts.insert("Problematic", at: 0) }
        if history.online == false, let since = history.offlineSince {
            parts.insert("offline since \(FleetOffline.clock(since))", at: 0)
        }
        return parts.joined(separator: " · ")
    }

    /// The tooltip of the whole strip: a day is either a figure or "not recorded", never a blank.
    static func stripTooltip(_ history: FleetHistory?) -> String {
        let days = history?.days ?? []
        guard !days.isEmpty else { return "The last 7 days. This Gateway has no history for it yet." }
        let lines = days.map { day -> String in
            guard let fraction = day.offlineFraction else {
                return "\(day.date ?? "—"): not recorded"
            }
            return "\(day.date ?? "—"): \(Int((fraction * 100).rounded()))% of the day offline"
        }
        return (["The last 7 days, oldest on the left. A filled bar is time this was unreachable."] + lines)
            .joined(separator: "\n")
    }

    /// "45s", "12m", "3h", "7d" — for a badge, where the full word does not fit.
    static func compact(_ seconds: Double) -> String {
        if seconds < 90 { return "\(Int(seconds.rounded()))s" }
        if seconds < 5400 { return "\(Int((seconds / 60).rounded()))m" }
        if seconds < 172_800 { return "\(Int((seconds / 3600).rounded()))h" }
        return "\(Int((seconds / 86_400).rounded()))d"
    }

    /// "45 s", "12 min", "3 h", "2 days" — never "172800".
    static func duration(_ seconds: Double?) -> String {
        guard let seconds else { return "—" }
        if seconds < 90 { return "\(Int(seconds.rounded())) s" }
        if seconds < 5400 { return "\(Int((seconds / 60).rounded())) min" }
        if seconds < 172_800 { return "\(Int((seconds / 3600).rounded())) h" }
        return "\(Int((seconds / 86_400).rounded())) days"
    }

    // MARK: The likely cause

    /// The strongest cause in plain words, or `nil` when there is nothing to show.
    ///
    /// A cause of `fine` is a sentence, not an alarm: a Mac that is up and never dropped has no fault,
    /// and colouring that red would teach the owner to ignore the column.
    static func causeLine(_ causes: [FleetCause]?) -> String? {
        guard let first = causes?.first, let text = first.text, !text.isEmpty else { return nil }
        return text
    }

    static func causeHealth(_ causes: [FleetCause]?) -> Health {
        guard let code = causes?.first?.code else { return .off }
        switch code {
        case "fine": return .ok
        case "no_evidence": return .off
        default: return .warn
        }
    }

    /// Everything the row cannot fit: each hint and **the evidence it came from**, one per line.
    static func causeTooltip(_ causes: [FleetCause]?) -> String? {
        guard let causes, !causes.isEmpty else { return nil }
        let lines = causes.compactMap { cause -> String? in
            guard let text = cause.text, !text.isEmpty else { return nil }
            if let evidence = cause.evidence, !evidence.isEmpty {
                return "\(text)\n   Evidence: \(evidence)"
            }
            return text
        }
        return lines.isEmpty ? nil : lines.joined(separator: "\n\n")
    }
}

/// The seven-day strip: one bar a day, oldest on the left, filled from the bottom by how much of that
/// day the Gateway saw this Mac or site unreachable.
///
/// A day with no observation is drawn as a dashed empty box. That is deliberate: a solid empty bar is
/// the drawing of "up all day", and nobody looked.
final class FleetHistoryStripView: NSView {
    static let dayWidth: CGFloat = 9
    static let gap: CGFloat = 2
    static let height: CGFloat = 16

    var history: FleetHistory? { didSet { needsDisplay = true } }

    override var isFlipped: Bool { true }

    override var intrinsicContentSize: NSSize {
        let count = max(history?.days?.count ?? 7, 1)
        return NSSize(width: CGFloat(count) * Self.dayWidth + CGFloat(count - 1) * Self.gap,
                      height: Self.height)
    }

    override func draw(_ dirtyRect: NSRect) {
        let days = history?.days ?? []
        guard !days.isEmpty else {
            drawUnmeasured(in: NSRect(x: 0, y: 2, width: intrinsicContentSize.width, height: 12))
            return
        }
        var x: CGFloat = 0
        for day in days {
            let box = NSRect(x: x, y: 2, width: Self.dayWidth, height: 12)
            let path = NSBezierPath(roundedRect: box, xRadius: 2, yRadius: 2)
            NSColor.quaternaryLabelColor.withAlphaComponent(0.22).setFill()
            path.fill()
            if let fraction = day.offlineFraction {
                if fraction > 0 {
                    let filled = max(box.height * CGFloat(min(max(fraction, 0), 1)), 3)
                    let bar = NSRect(x: box.minX, y: box.maxY - filled, width: box.width, height: filled)
                    let colour: Health = fraction >= 0.5 ? .bad : .warn
                    colour.tint.setFill()
                    NSBezierPath(roundedRect: bar, xRadius: 2, yRadius: 2).fill()
                }
            } else {
                // Nobody observed this day: an outline, dashed, instead of an empty bar.
                let outline = NSBezierPath(roundedRect: box.insetBy(dx: 0.5, dy: 0.5), xRadius: 2, yRadius: 2)
                outline.lineWidth = 1
                outline.setLineDash([2, 1.5], count: 2, phase: 0)
                NSColor.tertiaryLabelColor.setStroke()
                outline.stroke()
            }
            x += Self.dayWidth + Self.gap
        }
    }

    private func drawUnmeasured(in box: NSRect) {
        let outline = NSBezierPath(roundedRect: box.insetBy(dx: 0.5, dy: 0.5), xRadius: 2, yRadius: 2)
        outline.lineWidth = 1
        outline.setLineDash([2, 1.5], count: 2, phase: 0)
        NSColor.tertiaryLabelColor.setStroke()
        outline.stroke()
    }
}

/// The strip over its figures — the one cell both the Machines tab and the Groups view can use.
func fleetHistoryCell(_ history: FleetHistory?) -> NSView {
    let strip = FleetHistoryStripView()
    strip.history = history
    strip.translatesAutoresizingMaskIntoConstraints = false
    NSLayoutConstraint.activate([
        strip.widthAnchor.constraint(equalToConstant: strip.intrinsicContentSize.width),
        strip.heightAnchor.constraint(equalToConstant: strip.intrinsicContentSize.height),
    ])
    let line = PageKit.label(FleetHealth.shortSummary(history), .caption, colour: .secondaryLabelColor)
    line.lineBreakMode = .byTruncatingTail
    let stack = fleetColumnStack([strip, line])
    stack.spacing = DS.Space.xs
    let cell = fleetPinned(stack)
    cell.toolTip = FleetHealth.stripTooltip(history)
    return cell
}
