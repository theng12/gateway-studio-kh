// What each Mac says about macOS's own 30-day Trash emptying, as the Fleet page reads it - the pure half
// (no networking, no windows), so the table code and its layout test can use it without the rest.
// The button and the requests that act on it are in FleetMacTrash.swift.
//
// Why it exists (owner request, 2026-10-19): every retirement a Studio Hub makes ends in macOS Trash, so
// a Mac whose Finder never empties that Trash fills its disk until someone empties it by hand - and
// across ~53 Macs nobody could tell which ones were set. The value rides the `startup` object every
// machine row already carries (`macos_trash_auto_empty`, Hub 0.6.187), so nothing new is fetched.

import AppKit

enum FleetMacTrash {
    /// "On" / "Off" / "Unknown". Unknown is grey and never drawn as off: a Hub older than 0.6.187 says
    /// nothing about it, and neither does a Mac nobody has heard from since.
    static func health(_ fact: FleetStartupFact?) -> (health: Health, text: String) {
        switch fact?.macosTrashAutoEmpty {
        case true?: return (.ok, "On")
        case false?: return (.warn, "Off")
        case nil: return (.off, "Unknown")
        }
    }

    /// What the setting is, in the Mac's own terms, for a tooltip or a sentence.
    static let setting = "macOS's own setting (Finder > Settings > Advanced > \"Remove items from the "
        + "Trash after 30 days\"), not Studio Hub's."

    static func tooltip(_ fact: FleetStartupFact?, name: String = "This Mac") -> String {
        switch fact?.macosTrashAutoEmpty {
        case true?:
            return "\(name) empties its Trash after 30 days. \(setting)"
        case false?:
            return "\(name) never empties its Trash by itself, so everything Studio Hub moves there fills "
                + "its disk until someone empties it by hand. \(setting) Turn it on here."
        case nil:
            return "\(name) did not report this: its Studio Hub is older than 0.6.187, or its controller "
                + "has not heard from it since. \(setting)"
        }
    }

    /// The Macs a bulk "turn it on" would ask: reachable, and their Hub said **off**. Unknown is never
    /// guessed at, and neither is a stale reading - an older Hub cannot take the request at all.
    static func targets(_ machines: [FleetMachine]) -> [FleetMachine] {
        machines.filter { $0.reachable == true && $0.startup?.stale != true
            && $0.startup?.macosTrashAutoEmpty == false }
    }

    /// Macs that said it is off but cannot be reached now: listed, so nobody thinks they were fixed.
    static func unreachable(_ machines: [FleetMachine]) -> [FleetMachine] {
        machines.filter { ($0.reachable != true || $0.startup?.stale == true)
            && $0.startup?.macosTrashAutoEmpty == false }
    }

    /// How many reachable Macs said it is off: what the bulk button offers to fix.
    static func offCount(_ machines: [FleetMachine]) -> Int { targets(machines).count }
}
