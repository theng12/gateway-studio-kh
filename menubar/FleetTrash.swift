// Each Mac's Studio Hub Trash Folder, seen and emptied from here (owner decision 4g, 2026-10-16).
//
// Studio Hub never hard-deletes what it retires: it moves it to its Trash Folder, so a Mac's disk fills until
// someone empties it. This is where the owner does that without visiting the Mac - one Mac from its "…" menu,
// or every Mac at once. "Move to macOS Trash" uses the normal admin auth (recoverable on that Mac).
// "Delete Permanently" names the Mac and the exact count and bytes, and is sent with the owner-console secret
// only this app holds (`OwnerConsole`), so no API-key holder can trigger it. A Mac on an older Studio Hub
// shows "needs Hub 0.6.182".

import AppKit
import CoreFoundation

enum FleetTrash {
    /// Every Mac the Machines tab last showed, for "Trash on All Macs…" (set in `FleetMachinesTab.update`).
    static var everyMac: [FleetMachine] = []

    struct Report {
        let machine: FleetMachine
        let payload: [String: Any]?
        let error: String?
        var name: String { machine.label ?? machine.machine ?? "this Mac" }
        var supported: Bool { payload?["supported"] as? Bool ?? false }
        var count: Int { (payload?["count"] as? NSNumber)?.intValue ?? 0 }
        var bytes: Int64 { (payload?["bytes"] as? NSNumber)?.int64Value ?? 0 }
        var deletable: Bool { supported && count > 0 }
        /// The gateway labels every reading: `fresh`, `stale`, `unreachable` or `unknown`. A payload from an
        /// older gateway states none, which is `unknown` — never claimed as fresh.
        var status: String { payload?["status"] as? String ?? "unknown" }
        /// Only a fresh reading is counted in a fleet total: an offline Mac states no size at all, and an old
        /// reading is not a measurement now.
        var isFresh: Bool { status == "fresh" && supported }
        /// When the Mac's own Hub walked the folder (`measured_at`), passed through by the gateway.
        var measuredAt: Date? {
            (payload?["measured_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        }
        var checked: String {
            guard let measuredAt else { return "checked time unknown" }
            if Date().timeIntervalSince(measuredAt) < 5 { return "checked just now" }
            return "checked " + FleetTrash.relative.localizedString(for: measuredAt, relativeTo: Date())
        }
        var free: String? {
            ((payload?["disk"] as? [String: Any])?["free_bytes"] as? NSNumber).map { FleetTrash.size($0.int64Value) + " free" }
        }
        /// A number the answer stated, or `nil`. `true`/`false` bridge to `NSNumber` and would read as
        /// 1/0 — a size nobody measured — so they are refused here rather than drawn.
        static func number(_ value: Any?) -> NSNumber? {
            guard let number = value as? NSNumber,
                  CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
            return number
        }
        /// What the folder holds, as the Mac stated it. `nil` when it stated no size (unsupported or
        /// unreachable) — 0 would claim a measurement nobody made.
        var trashBytes: Int64? { Report.number(payload?["bytes"])?.int64Value }
        /// The Mac's free disk from this reading; `nil` when the answer carries none.
        var freeDiskBytes: Int64? {
            Report.number((payload?["disk"] as? [String: Any])?["free_bytes"])?.int64Value
        }
        /// The Mac's free **memory** from this reading (Studio Hub 0.6.243 states it beside the disk).
        /// `nil` from a Hub that does not report it — and memory is never shown in the disk's column,
        /// so a shortfall in one never reads as a shortfall in the other.
        var freeMemoryBytes: Int64? {
            Report.number((payload?["memory"] as? [String: Any])?["free_bytes"])?.int64Value
        }
        var line: String {
            if let error { return "\(name): could not read (\(error))" }
            if !supported {
                if status == "unreachable" {
                    return "\(name): unreachable — \((payload?["detail"] as? String) ?? "it did not answer")"
                }
                return "\(name): needs Hub \(payload?["needs_hub"] as? String ?? "0.6.182")"
            }
            let held = "\(count) item\(count == 1 ? "" : "s"), \(FleetTrash.size(bytes))" + (free.map { " · \($0)" } ?? "")
            switch status {
            case "fresh": return "\(name): \(held) · \(checked)"
            case "stale": return "\(name): \(held) · STALE, \(checked)"
            default: return "\(name): \(held) · \(status), \(checked)"
            }
        }
    }

    private static let relative: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return formatter
    }()

    static func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    static func exact(_ bytes: Int64) -> String {
        NumberFormatter.localizedString(from: NSNumber(value: bytes), number: .decimal) + " bytes"
    }

    private static func path(_ machine: FleetMachine, _ tail: String = "") -> String? {
        guard let location = machine.locationId, let id = machine.machine,
              let place = location.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let node = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) else { return nil }
        return "/admin/api/fleet/controllers/\(place)/machines/\(node)/trash\(tail)"
    }

    /// Read every Mac's Trash Folder at once. Completions arrive on the main queue (`GatewayAPI`).
    static func load(_ machines: [FleetMachine], done: @escaping ([Report]) -> Void) {
        var reports = [Report?](repeating: nil, count: machines.count)
        let group = DispatchGroup()
        for (index, machine) in machines.enumerated() {
            guard let path = path(machine) else {
                reports[index] = Report(machine: machine, payload: nil, error: "this row has no machine id")
                continue
            }
            group.enter()
            GatewayAPI.get(path, timeout: 180) { result in
                switch result {
                case .success(let payload): reports[index] = Report(machine: machine, payload: payload, error: nil)
                case .failure(let error):
                    reports[index] = Report(machine: machine, payload: nil, error: error.localizedDescription)
                }
                group.leave()
            }
        }
        group.notify(queue: .main) { done(reports.compactMap { $0 }) }
    }

    /// A scrolling monospaced list for an alert's accessory view. Shared with the other Fleet confirms
    /// that have to name one line per Mac (FleetMacTrash.swift, FleetLiteMode.swift).
    static func listView(_ lines: [String]) -> NSView {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: min(260, 22 + 17 * lines.count)))
        let text = NSTextView(frame: scroll.bounds)
        text.string = lines.joined(separator: "\n")
        text.isEditable = false
        text.font = .monospacedDigitSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        text.autoresizingMask = [.width]
        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        return scroll
    }

    private static func summary(_ report: Report) -> String {
        guard report.error == nil, report.supported else { return report.line }
        var lines = ["\(report.count) item\(report.count == 1 ? "" : "s"), \(size(report.bytes))"
                     + (report.free.map { " · this Mac has \($0)" } ?? "")]
        for (title, key) in [("By kind", "by_kind"), ("By age", "by_age")] {
            guard let table = report.payload?[key] as? [String: [String: Any]], !table.isEmpty else { continue }
            lines.append("\n\(title):")
            for (name, row) in table.sorted(by: {
                (($0.value["bytes"] as? NSNumber)?.int64Value ?? 0) > (($1.value["bytes"] as? NSNumber)?.int64Value ?? 0)
            }) {
                lines.append("  \(name): \((row["count"] as? NSNumber)?.intValue ?? 0), "
                             + size((row["bytes"] as? NSNumber)?.int64Value ?? 0))
            }
        }
        lines.append("\n\(report.checked)" + (report.isFresh ? "" : " — \(report.status); not counted as fresh"))
        return lines.joined(separator: "\n")
    }

    // MARK: One Mac

    static func show(_ machine: FleetMachine, done: @escaping () -> Void) {
        load([machine]) { reports in
            guard let report = reports.first else { return }
            let alert = NSAlert()
            alert.messageText = "Trash Folder on \(report.name)"
            alert.informativeText = summary(report) + (report.deletable ? "\n\nStudio Hub never empties it by itself." : "")
            if report.deletable {
                alert.addButton(withTitle: "Close")
                alert.addButton(withTitle: "Move to macOS Trash")
                alert.addButton(withTitle: "Delete Permanently…")
            } else {
                alert.addButton(withTitle: "OK")
            }
            alert.closeOnEscape()
            switch alert.runModal() {
            case .alertSecondButtonReturn: move([report], done: done)
            case .alertThirdButtonReturn: confirmDelete([report], done: done)
            default: break
            }
        }
    }

    // MARK: Every Mac

    static func showAll(done: @escaping () -> Void) {
        let machines = everyMac.filter { $0.locationId != nil && $0.machine != nil }
        load(machines) { reports in
            let sorted = reports.sorted { $0.bytes > $1.bytes }
            let deletable = sorted.filter(\.deletable)
            // The claimed fleet total counts fresh readings only; offline and stale Macs are named, not zeroed.
            let fresh = sorted.filter(\.isFresh)
            let notCounted = sorted.filter { !$0.isFresh }
            let total = fresh.reduce(Int64(0)) { $0 + $1.bytes }
            let alert = NSAlert()
            alert.messageText = "Trash Folders on \(machines.count) Mac\(machines.count == 1 ? "" : "s")"
            var text = "\(size(total)) in \(fresh.reduce(0) { $0 + $1.count }) items on "
                + "\(fresh.count) Mac\(fresh.count == 1 ? "" : "s"), counted from fresh readings. "
                + "Studio Hub never empties them by itself."
            if !notCounted.isEmpty {
                text += "\n\(notCounted.count) Mac\(notCounted.count == 1 ? "" : "s") not counted (offline, stale or unknown)."
            }
            alert.informativeText = text
            alert.accessoryView = listView(sorted.map(\.line))
            alert.addButton(withTitle: "Close")
            if !deletable.isEmpty {
                alert.addButton(withTitle: "Move All to macOS Trash")
                alert.addButton(withTitle: "Delete Permanently on All…")
            }
            alert.closeOnEscape()
            switch alert.runModal() {
            case .alertSecondButtonReturn: move(deletable, done: done)
            case .alertThirdButtonReturn: confirmDelete(deletable, done: done)
            default: break
            }
        }
    }

    // MARK: Actions

    /// One request per Mac, one after another; then one sentence per Mac.
    private static func each(_ reports: [Report], _ send: @escaping (Report, @escaping (String) -> Void) -> Void,
                             title: String, done: @escaping () -> Void) {
        var outcomes: [String] = []
        func next(_ index: Int) {
            guard index < reports.count else {
                FleetStartup.show(title, outcomes.joined(separator: "\n"))
                done()
                return
            }
            send(reports[index]) { outcomes.append("\(reports[index].name): \($0)"); next(index + 1) }
        }
        next(0)
    }

    private static func failures(_ payload: [String: Any]) -> String {
        let failed = payload["failed"] as? [[String: Any]] ?? []
        guard !failed.isEmpty else { return "" }
        return "; \(failed.count) could not be: " + failed.prefix(3).map { "\($0["id"] as? String ?? "?") "
            + "(\($0["error"] as? String ?? "refused"))" }.joined(separator: ", ")
    }

    private static func move(_ reports: [Report], done: @escaping () -> Void) {
        each(reports, { report, finished in
            guard let path = path(report.machine, "/move-to-macos-trash") else { return finished("no machine id") }
            GatewayAPI.post(path, body: ["items": "all"], timeout: 900) { result in
                switch result {
                case .success(let payload):
                    finished("moved \((payload["moved_count"] as? NSNumber)?.intValue ?? 0) to macOS Trash" + failures(payload))
                case .failure(let error): finished(error.localizedDescription)
                }
            }
        }, title: "Move to macOS Trash", done: done)
    }

    /// The confirmation names every Mac with its exact count and bytes; those are what the Mac re-measures,
    /// and it deletes nothing if they differ (the answer then says what it holds now).
    private static func confirmDelete(_ reports: [Report], done: @escaping () -> Void) {
        let sure = NSAlert()
        sure.alertStyle = .critical
        let count = reports.reduce(0) { $0 + $1.count }, bytes = reports.reduce(Int64(0)) { $0 + $1.bytes }
        if reports.count == 1, let report = reports.first {
            sure.messageText = "Permanently delete \(report.count) item\(report.count == 1 ? "" : "s") "
                + "(\(size(report.bytes))) on \(report.name)?"
            sure.informativeText = "Exactly \(report.count) items, \(exact(report.bytes)), from the Studio Hub Trash "
                + "Folder on \(report.name). They do not go to macOS Trash and cannot be recovered. If the Trash "
                + "Folder changed since it was measured, nothing is deleted."
        } else {
            sure.messageText = "Permanently delete \(count) items (\(size(bytes))) on \(reports.count) Macs?"
            sure.informativeText = "Exactly these, from each Mac's Studio Hub Trash Folder. They do not go to macOS "
                + "Trash and cannot be recovered. A Mac whose Trash Folder changed since it was measured deletes nothing."
            sure.accessoryView = listView(reports.map {
                "\($0.name): \($0.count) item\($0.count == 1 ? "" : "s"), \(exact($0.bytes)) (\(size($0.bytes)))"
            })
        }
        sure.addButton(withTitle: "Cancel")
        sure.closeOnEscape()
        let delete = sure.addButton(withTitle: reports.count == 1 ? "Delete Permanently" : "Delete Permanently on \(reports.count) Macs")
        if #available(macOS 11.0, *) { delete.hasDestructiveAction = true }
        guard sure.runModal() == .alertSecondButtonReturn else { return }
        each(reports, { report, finished in
            guard let path = path(report.machine, "/delete") else { return finished("no machine id") }
            // The ids that were listed and confirmed, not "all": the Mac then deletes exactly these.
            let ids = (report.payload?["items"] as? [[String: Any]] ?? []).compactMap { $0["id"] as? String }
            guard !ids.isEmpty else { return finished("no item list; look again") }
            GatewayAPI.post(path, body: ["items": ids, "confirm": true, "expected_count": report.count,
                                         "expected_bytes": report.bytes],
                            timeout: 900, ownerConsole: true) { result in
                switch result {
                case .success(let payload):
                    let freed = (payload["freed_bytes"] as? NSNumber)?.int64Value ?? 0
                    finished("deleted \((payload["deleted_count"] as? NSNumber)?.intValue ?? 0), freed \(size(freed))"
                             + failures(payload))
                case .failure(let error): finished(error.localizedDescription)
                }
            }
        }, title: "Delete permanently", done: done)
    }

    // MARK: Empty trash — one Mac, or every Mac at every linked site

    /// One Mac's line in the confirmation: what its Hub folder and its macOS Trash hold, as stated.
    /// `nil` is "not reported" — named, never counted as 0.
    struct EmptyLine {
        let name: String
        let folderCount: Int?
        let folderBytes: Int64?
        let macosCount: Int?
        let macosBytes: Int64?

        var text: String {
            func part(_ count: Int?, _ bytes: Int64?) -> String {
                guard let bytes else { return "not reported" }
                if bytes == 0 { return "empty" }
                let items = count ?? 0
                return "\(items) item\(items == 1 ? "" : "s"), \(FleetTrash.size(bytes))"
            }
            return "\(name) — Hub folder \(part(folderCount, folderBytes)) · macOS Trash \(part(macosCount, macosBytes))"
        }
        var stated: Bool { folderBytes != nil || macosBytes != nil }
        var bytes: Int64 { (folderBytes ?? 0) + (macosBytes ?? 0) }
        var count: Int { (folderBytes == nil ? 0 : folderCount ?? 0) + (macosBytes == nil ? 0 : macosCount ?? 0) }
    }

    /// The confirmation's total: only what was stated, and how many Macs stated nothing.
    static func total(_ lines: [EmptyLine]) -> String {
        let stated = lines.filter(\.stated)
        let bytes = stated.reduce(Int64(0)) { $0 + $1.bytes }
        let count = stated.reduce(0) { $0 + $1.count }
        var text = "Total: \(count) item\(count == 1 ? "" : "s"), \(size(bytes)) (\(exact(bytes))) "
            + "on \(stated.count) Mac\(stated.count == 1 ? "" : "s")"
        let unstated = lines.count - stated.count
        if unstated > 0 { text += " · \(unstated) Mac\(unstated == 1 ? "" : "s") not reported, not counted" }
        return text
    }

    /// What one run said: the totals, then every Mac that was not emptied, with its reason and what is left.
    static func outcome(_ payload: [String: Any]) -> String {
        let totals = payload["totals"] as? [String: Any] ?? [:]
        func n(_ key: String) -> Int64 { Report.number(totals[key])?.int64Value ?? 0 }
        var text = "emptied \(n("emptied")) · skipped \(n("skipped")) · failed \(n("failed"))"
            + " — freed \(size(n("freed_bytes")))"
        if n("left_bytes") > 0 { text += ", \(size(n("left_bytes"))) left" }
        let named = (payload["machines"] as? [[String: Any]] ?? []).compactMap { row -> String? in
            guard let state = row["state"] as? String, state != "emptied" else { return nil }
            let who = row["label"] as? String ?? row["machine"] as? String
                ?? (row["location_id"] as? String).map { "site \($0)" } ?? "a Mac"
            var said = "\(who) \(state): \(row["reason"] as? String ?? "no reason given")"
            if let left = Report.number(row["left_bytes"])?.int64Value { said += " (\(size(left)) left)" }
            return said
        }
        return ([text] + named).joined(separator: "; ")
    }

    /// **Empty trash** (owner, 2026-11-16: "one button to delete across all the linking sites and nodes
    /// and controllers ... what's in the trash will go"). `machine: nil` is every Mac at every linked
    /// site; otherwise that one Mac. Both go to the one route an agent can call too
    /// (`POST /admin/api/fleet/trash/empty`): Studio Hub 0.6.243 removes the Hub Trash Folder and the
    /// whole macOS Trash, and answers each Mac emptied, skipped or failed with its reason.
    ///
    /// One in-app confirmation names each Mac's count and bytes and the total; every run ends in a
    /// sentence saying what was freed and what was not, and why.
    static func emptyTrash(_ lines: [EmptyLine], machine: FleetMachine?, done: @escaping (String) -> Void) {
        var body: [String: Any] = ["scope": "all"]
        if let machine {
            guard let location = machine.locationId, let id = machine.machine else { return done("no machine id") }
            body = ["location_id": location, "machine": id]
        }
        let sure = NSAlert()
        sure.alertStyle = .critical
        sure.messageText = machine == nil ? "Empty trash on every Mac?"
            : "Empty \(lines.first?.name ?? "this Mac")'s trash?"
        sure.informativeText = "Permanently deletes the Studio Hub Trash Folder and everything in macOS Trash on "
            + (machine == nil ? "every Mac at every linked site" : "this Mac")
            + ". It cannot be recovered. A Mac whose own switch is off, whose Hub is older than 0.6.243, "
            + "or that is offline is skipped and says why.\n\n" + total(lines)
        if !lines.isEmpty { sure.accessoryView = listView(lines.map(\.text)) }
        sure.addButton(withTitle: "Cancel")
        sure.closeOnEscape()
        let empty = sure.addButton(withTitle: machine == nil ? "Empty All Trash" : "Empty Trash")
        if #available(macOS 11.0, *) { empty.hasDestructiveAction = true }
        guard sure.runModal() == .alertSecondButtonReturn else { return done("Nothing was deleted.") }
        // shortcut: one request for the whole fleet, bounded at an hour; past it the tab's re-read shows the truth.
        GatewayAPI.post("/admin/api/fleet/trash/empty", body: body, timeout: machine == nil ? 3600 : 960,
                        ownerConsole: true) { result in
            switch result {
            case .success(let payload): done(outcome(payload))
            case .failure(let error): done("could not empty trash: \(error.localizedDescription)")
            }
        }
    }
}
