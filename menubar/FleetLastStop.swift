// Why a Mac is offline, in plain English - the pure half (no networking, no windows), so the Fleet pages and
// their layout tests can share it.
//
// Studio Hub records, on the Mac itself, why it last stopped (`last_stop` on each machine row: quit, crashed,
// updated, force-quit, low memory, Mac restarted) and reports it when it comes back. This turns that and the
// controller's own "could not reach the node" evidence into one sentence. **It never guesses**: an unknown
// reason says nothing, a stop older than the last time the Mac was seen online does not explain the outage
// that is happening now, and a Mac that did not answer at all is "reason unknown".

import Foundation

/// The last time a Mac's Studio Hub stopped, as that Mac recorded it. Every field is optional: absent is unknown.
struct FleetLastStop: Decodable {
    /// quit, crashed, updated, force_quit, low_memory, mac_restarted - or nil when the Mac said something else.
    var reason: String?
    /// Unix epoch seconds.
    var at: Double?
    /// The Hub's own plain-English phrase, without a time ("Force-quit", "Crashed (restarted)").
    var label: String?
    var relaunched: Bool?
    /// True when the controller is repeating what an unreachable Mac last said.
    var stale: Bool?
}

enum FleetOffline {
    /// A Lite Mode Mac that is switched off (a shop Mac at night or on holiday): expected, said plainly.
    static let liteOffNote = "Switched off — Lite Mode. Expected; it gets no work until it is back."

    /// The Hub's phrase when it sent one, else the same words for the reasons it defines, else nil.
    static func phrase(_ stop: FleetLastStop) -> String? {
        if let label = stop.label?.trimmingCharacters(in: .whitespacesAndNewlines), !label.isEmpty { return label }
        switch stop.reason {
        case "quit": return "Quit"
        case "crashed": return stop.relaunched == true ? "Crashed (restarted)" : "Crashed"
        case "updated": return "Restarted for an update"
        case "force_quit": return "Force-quit"
        case "low_memory": return "Stopped for low memory"
        case "mac_restarted": return "Mac restarted, shut down or lost power"
        default: return nil
        }
    }

    /// Local time as HH:mm, with the date when it was not today ("Oct 2, 09:36").
    static func clock(_ epoch: Double, now: Date = Date(), calendar: Calendar = .current) -> String {
        let date = Date(timeIntervalSince1970: epoch)
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = calendar.isDate(date, inSameDayAs: now) ? "HH:mm" : "MMM d, HH:mm"
        return formatter.string(from: date)
    }

    /// "Force-quit at 09:36", or nil when the stop has no usable phrase or time.
    static func described(_ stop: FleetLastStop, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let phrase = phrase(stop), let at = stop.at else { return nil }
        return "\(phrase) at \(clock(at, now: now, calendar: calendar))"
    }

    /// The tooltip line, always shown when a stop is known: "Last stopped: Quit at 09:36".
    static func lastStopLine(_ machine: FleetMachine, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let stop = machine.lastStop, let text = described(stop, now: now, calendar: calendar) else { return nil }
        return "Last stopped: \(text)" + (stop.stale == true ? " (as last reported by the Mac)" : "")
    }

    /// Why this Mac is offline, for the row: nil unless it is offline for a reason the controller can state.
    ///
    /// * the Mac answers but nothing listens (`no_hub`): "Studio Hub isn't running on this Mac", plus how it
    ///   stopped when the Mac recorded a stop **after** the last time it was seen online - an older stop
    ///   belongs to an earlier outage and would explain nothing about this one;
    /// * it did not answer at all (`unreachable`): it may have restarted, be asleep or have lost its network,
    ///   and nothing here can tell which.
    static func why(_ machine: FleetMachine, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard !machine.nodeStateIsUnauthorized,
              machine.reachable == false || (machine.state ?? "").lowercased() == "offline" else { return nil }
        if machine.liteOff == true { return liteOffNote }
        switch machine.reachReason {
        case "no_hub":
            var text = "Studio Hub isn't running on this Mac"
            if let stop = machine.lastStop, let at = stop.at, let seen = machine.nodeLastSeenAt?.doubleValue, at > seen,
               let how = described(stop, now: now, calendar: calendar) {
                text += " · \(how)"
            }
            return text
        case "unreachable":
            return "Mac restarted, asleep or lost network (reason unknown)"
        default:
            return nil
        }
    }

    /// Everything the row's tooltip adds: why it is offline, then the last known stop.
    static func tooltip(_ machine: FleetMachine, now: Date = Date(), calendar: Calendar = .current) -> String? {
        let lines = [why(machine, now: now, calendar: calendar), lastStopLine(machine, now: now, calendar: calendar)]
            .compactMap { $0 }
        return lines.isEmpty ? nil : lines.joined(separator: "\n")
    }
}
