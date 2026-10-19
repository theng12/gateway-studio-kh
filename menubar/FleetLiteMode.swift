// Each Mac's Lite Mode status on the Fleet page (owner request 2026-10-19): a plain column, so the owner
// can see at a glance which Macs lend the fleet their idle time and which have a change waiting.
//
// **Show only.** Everything it reads is already on the machine row: `workstation` (the Hub's own block),
// `lite_off` (the Gateway's verdict for a Mac that is switched off *in* Lite Mode) and `lite_mode_pending`
// (a change the Gateway is holding for a Mac that was offline). Nothing new is fetched and nothing is
// changed here - the Lite Mode sheet is already on this Mac's "..." menu (`FleetStartup.editWorkstation`).

import AppKit

enum FleetLiteMode {
    /// What this Mac's Lite Mode is, in one word the column can draw.
    ///
    /// A change the Gateway is holding wins over the last report, because it is the owner's latest word:
    /// "Turning on…" is a Mac that will be in Lite Mode once it answers. A waiting change that leaves
    /// `enabled` alone is "Change waiting" - the mode is not changing, its settings are.
    ///
    /// `lite_off` counts as **on**: it is the Gateway's own statement that this Mac was last known to be
    /// in Lite Mode and is switched off, so its Hub is not there to repeat it.
    static func state(_ machine: FleetMachine) -> (health: Health, text: String) {
        let current = machine.liteOff == true ? true : machine.workstation?.enabled
        if let pending = machine.liteModePending {
            switch (pending.settings?.enabled, current) {
            case (true?, true?), (false?, false?): return (.warn, "Change waiting")
            case (true?, _): return (.warn, "Turning on…")
            case (false?, _): return (.warn, "Turning off…")
            default: return (.warn, "Change waiting")
            }
        }
        switch current {
        case true?: return (.ok, "On")
        case false?: return (.off, "Off")
        case nil: return (.off, "Unknown")
        }
    }

    static func tooltip(_ machine: FleetMachine?, name: String = "This Mac") -> String {
        guard let machine else { return "\(name) has no row to read." }
        var lines: [String] = []
        if let pending = machine.liteModePending {
            lines.append("A Lite Mode change is saved here for \(name) and is sent when it answers: "
                         + pending.line + ".")
        }
        if machine.liteOff == true {
            lines.append("\(name) is switched off and its Lite Mode is on - expected, not a fault: it gets "
                         + "no work until it is back.")
        } else {
            switch machine.workstation?.enabled {
            case true?:
                lines.append("Lite Mode is on: \(name) lends the fleet its idle time.")
            case false?:
                lines.append("Lite Mode is off, so \(name) takes fleet work whenever it is free.")
            case nil:
                lines.append("\(name) did not report Lite Mode: its Studio Hub is too old for it, or its "
                             + "controller has not heard from it since.")
            }
        }
        if let resting = machine.workstation?.chipLong, machine.workstation?.stale != true {
            lines.append("Right now: \(resting.lowercased()).")
        }
        if machine.workstation?.stale == true {
            lines.append("\(name) is not answering; this is what it last reported.")
        }
        if let location = machine.locationId, let id = machine.machine, !location.isEmpty, !id.isEmpty {
            lines.append("Change it from this Mac's \"…\" menu, on the Startup column.")
        }
        return lines.joined(separator: "\n")
    }
}
