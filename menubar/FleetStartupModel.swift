// What a Mac says it starts when it starts, as the Fleet page reads it - the pure half of startup control
// (no networking, no windows), so the table code and its layout test can use it without the rest.
// The buttons and requests that act on it are in FleetStartup.swift.
//
// Why it exists (owner, 2026-10-02): about thirty Macs were set up without ticking "Start services on
// startup", and a Mac whose services are off is silent to its controller after every reboot.

import AppKit

/// What a Mac says about what starts when it starts (`startup` on each `/admin/api/fleet` machine row).
/// Every field is optional: absent means the Hub did not say, which is **unknown**, never "off".
struct FleetStartupFact: Decodable {
    var openAtLogin: Bool?
    var openAtLoginSaved: Bool?
    var startServicesOnLaunch: Bool?
    var appVersion: String?
    var appReporting: Bool?
    /// True when the controller is repeating what the Mac said before it went quiet.
    var stale: Bool?
    var servicesRunning: Running?

    struct Running: Decodable {
        var up: Int?
        var total: Int?
    }
}

enum FleetStartupAction {
    case enableAutostart
    case startServices
    case restartServices

    var title: String {
        switch self {
        case .enableAutostart: return "Enable Auto-Start"
        case .startServices: return "Start Services"
        case .restartServices: return "Restart Services"
        }
    }

    /// The body the gateway expects (`POST .../machines/{id}/startup`).
    func body(force: Bool) -> [String: Any] {
        switch self {
        case .enableAutostart: return ["action": "set_start_services_on_launch", "value": true, "force": force]
        case .startServices: return ["action": "start_services", "force": force]
        case .restartServices: return ["action": "restart_services", "force": force]
        }
    }
}

enum FleetStartup {
    // MARK: Badge

    /// "Starts at login ✓ · Services auto-start ✗". A mark is `?` when the Mac did not say, and a Mac
    /// the controller can no longer reach is described by its last word and marked as such.
    static func badge(_ fact: FleetStartupFact?) -> String {
        guard let fact else { return "Not reported" }
        func mark(_ value: Bool?) -> String { value == nil ? "?" : (value == true ? "✓" : "✗") }
        let text = "Starts at login \(mark(fact.openAtLogin)) · Services auto-start \(mark(fact.startServicesOnLaunch))"
        return fact.stale == true ? text + " (last known)" : text
    }

    /// The compact badge the Machines table draws: "Login ✓ · Auto-start ✗". Same marks and rules as `badge`.
    static func shortBadge(_ fact: FleetStartupFact?) -> String {
        guard let fact else { return "Not reported" }
        func mark(_ value: Bool?) -> String { value == nil ? "?" : (value == true ? "✓" : "✗") }
        let text = "Login \(mark(fact.openAtLogin)) · Auto-start \(mark(fact.startServicesOnLaunch))"
        return fact.stale == true ? text + " (last known)" : text
    }

    /// Amber text when this Mac will not serve after a restart; plain otherwise.
    static func colour(_ fact: FleetStartupFact?) -> NSColor? {
        fact?.startServicesOnLaunch == false ? Health.warn.text : nil
    }

    /// The Groups view's per-node startup badge: whether this Mac brings its services up by itself after a
    /// restart. Unknown is grey, never green; the tooltip carries the full report.
    static func health(_ fact: FleetStartupFact?) -> (health: Health, text: String) {
        switch fact?.startServicesOnLaunch {
        case true?: return (.ok, "Auto-start on")
        case false?: return (.warn, "Auto-start off")
        case nil: return (.off, "Not reported")
        }
    }

    static func tooltip(_ fact: FleetStartupFact?) -> String {
        guard let fact else {
            return "This Mac did not report its startup settings: its Studio Hub is older than 0.6.127, "
                + "or the controller has not heard from it since."
        }
        var lines: [String] = []
        switch fact.openAtLogin {
        case true?: lines.append("Studio Hub opens by itself at login.")
        case false?: lines.append("Studio Hub does not open by itself at login.")
        case nil: lines.append("Whether Studio Hub opens at login was not reported.")
        }
        switch fact.startServicesOnLaunch {
        case true?: lines.append("Its services start on their own after a restart.")
        case false?:
            lines.append("Its services do NOT start after a restart: the Mac opens Studio Hub but "
                + "serves nothing until someone starts them by hand.")
        case nil: lines.append("Whether its services start after a restart was not reported.")
        }
        if let running = fact.servicesRunning, let up = running.up, let total = running.total {
            lines.append("Services running now: \(up) of \(total).")
        }
        if let version = fact.appVersion, !version.isEmpty { lines.append("Studio Hub \(version).") }
        if fact.stale == true { lines.append("This Mac is not answering; this is what it last reported.") }
        return lines.joined(separator: "\n")
    }

    // MARK: Which buttons

    /// The actions that make sense for this Mac right now. A Mac that is not answering gets none: its
    /// controller cannot pass anything on (the tooltip says so), and a `no_hub` Mac needs its services
    /// started by hand once.
    static func actions(for machine: FleetMachine) -> [FleetStartupAction] {
        guard machine.reachable == true, let fact = machine.startup, fact.stale != true else { return [] }
        var out: [FleetStartupAction] = []
        if fact.startServicesOnLaunch == false { out.append(.enableAutostart) }
        if let running = fact.servicesRunning, let up = running.up, let total = running.total, up < total {
            out.append(.startServices)
        }
        out.append(.restartServices)
        return out
    }

    /// How many reachable Macs said auto-start is off: what the bulk button offers to fix.
    static func offCount(_ machines: [FleetMachine]) -> Int {
        machines.filter { $0.reachable == true && $0.startup?.stale != true
            && $0.startup?.startServicesOnLaunch == false }.count
    }

    /// Macs that said it is off but cannot be reached now (listed so nobody thinks they were fixed).
    static func offUnreachableCount(_ machines: [FleetMachine]) -> Int {
        machines.filter { ($0.reachable != true || $0.startup?.stale == true)
            && $0.startup?.startServicesOnLaunch == false }.count
    }
}
