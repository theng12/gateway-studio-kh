// Every Mac's Trash Folder on one Fleet page (owner, 2026-11-16: "i want to add a feature so that we
// can delete it through the gateway or the hub").
//
// One row per Mac, with the trash it holds, its free disk and its free **memory** in separate columns,
// so a memory shortfall never reads as a disk one, and one step to empty a Mac's Trash Folder.
//
// **Nothing here invents a number.** A Mac that does not report a field reads "not reported" — never 0,
// which would claim a measurement nobody made. A field the Mac *does* report as 0 is shown as 0.
//
// The numbers come from the Mac's own trash answer when Studio Hub 0.6.243 states them there, and from
// the fleet row the Hub publishes for free disk/memory otherwise. The three are independent on purpose:
// trash size, free disk and free memory are different facts and each has its own column.

import AppKit

/// One Mac's row: what the fleet read said about it, and what its Trash Folder answered.
struct FleetTrashRow {
    let machine: FleetMachine
    let report: FleetTrash.Report?

    var name: String { machine.label ?? machine.machine ?? "This Mac" }
    var key: String { "\(machine.locationId ?? "")|\(machine.machine ?? "")" }

    /// The folder's size, as stated. `nil` is "not reported": never counted, never drawn as 0.
    var trashBytes: Int64? { report?.trashBytes }
    /// Free disk: the Mac's trash reading first (it is stamped with when it was measured), else the
    /// fleet row the Hub publishes. Still `nil` when neither states one.
    var freeDiskBytes: Int64? {
        if let stated = report?.freeDiskBytes { return stated }
        if let gb = machine.disk?.freeGb { return Int64(gb * 1_000_000_000) }
        return machine.diskFreeGb.map { Int64($0 * 1_000_000_000) }
    }
    /// Free memory, from the same reading or the fleet row. A Mac's memory is never folded into its disk.
    var freeMemoryBytes: Int64? {
        if let stated = report?.freeMemoryBytes { return stated }
        return machine.availableGb.map { Int64($0 * 1_000_000_000) }
    }
    var deletable: Bool { report?.deletable ?? false }

    /// The items this Hub moved into macOS Trash, when it states both a count and a size and holds
    /// something. `nil` is "nothing this app can delete" — an older Hub, an unstated figure, or none.
    var macosTrashPart: FleetTrash.MacosTrashPart? {
        guard let stated = machine.macosTrash, let count = stated.hubItemsCount,
              let bytes = stated.hubItemsBytes, count > 0 else { return nil }
        return FleetTrash.MacosTrashPart(count: count, bytes: bytes)
    }
    /// The macOS Trash column. `not reported` from a Hub older than 0.6.243 — never 0.
    var macosTrashText: String {
        guard let stated = machine.macosTrash, let bytes = stated.hubItemsBytes else { return "not reported" }
        if bytes == 0 { return "empty" }
        let count = stated.hubItemsCount ?? 0
        return "\(count) item\(count == 1 ? "" : "s") · \(FleetTrash.size(bytes))"
    }
    /// One step can empty either part, so the row's button is live when either holds something.
    var canEmpty: Bool { deletable || macosTrashPart != nil }

    /// What the Trash column says. A Mac that cannot be read says why instead of a size.
    var trashText: String {
        guard let report else { return "not read" }
        if report.error != nil { return "could not read" }
        if !report.supported {
            return report.status == "unreachable"
                ? "unreachable"
                : "needs Hub \(report.payload?["needs_hub"] as? String ?? "0.6.182")"
        }
        guard let bytes = report.trashBytes else { return "not reported" }
        if bytes == 0 { return "empty" }
        let items = report.count
        return "\(items) item\(items == 1 ? "" : "s") · \(FleetTrash.size(bytes))"
            + (report.isFresh ? "" : " · \(report.status)")
    }
    var diskText: String { freeDiskBytes.map { FleetTrash.size($0) + " free" } ?? "not reported" }
    var memoryText: String { freeMemoryBytes.map { FleetTrash.size($0) + " free" } ?? "not reported" }
}

final class FleetTrashTab: FleetTabView {
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private lazy var refresh = FleetClosureButton(title: "Read Every Mac") { [weak self] in
        self?.load(force: true)
    }
    private lazy var table = FleetTableController(columns: columns(), rowHeight: 36)

    private var machines: [FleetMachine] = []
    private var reports: [String: FleetTrash.Report] = [:]
    private var loading = false
    /// The roster the last read covered, so a Mac joining or leaving triggers one.
    private var readFor: Set<String> = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        addSection("Trash folders")
        let header = PageKit.row([statusLabel, NSView(), refresh], spacing: DS.Space.m)
        page.addArrangedSubview(header)
        page.setCustomSpacing(DS.Space.m, after: header)
        table.container.heightAnchor.constraint(equalToConstant: 340).isActive = true
        page.addArrangedSubview(table.container)
        fullWidth(header)
        fullWidth(table.container)
        table.emptyText = "No Macs are being reported yet."
        statusLabel.stringValue = "Reading every Mac's Trash Folder…"
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The shell's fleet read, every refresh. The roster decides when to read the Macs again; the free
    /// disk and memory columns move with the fleet row even between trash readings.
    func update(machines: [FleetMachine]) {
        self.machines = machines.filter { $0.locationId != nil && $0.machine != nil }
        rebuild()
        let roster = Set(self.machines.map(key))
        if !self.machines.isEmpty && roster != readFor { load(force: true) }
        if self.machines.isEmpty { statusLabel.stringValue = "No Macs are being reported yet." }
    }

    private func key(_ machine: FleetMachine) -> String {
        "\(machine.locationId ?? "")|\(machine.machine ?? "")"
    }

    private func rebuild() {
        table.setRows(machines.map { FleetTrashRow(machine: $0, report: reports[key($0)]) })
    }

    /// Read every Mac's Trash Folder. One request per Mac, in parallel (`FleetTrash.load`); a Mac that
    /// does not answer becomes a row that says so, never a zero.
    func load(force: Bool = false) {
        guard !loading, !machines.isEmpty else { return }
        if !force && !reports.isEmpty { return }
        loading = true
        statusLabel.stringValue = "Reading \(machines.count) Mac\(machines.count == 1 ? "" : "s")…"
        FleetTrash.load(machines) { [weak self] loaded in
            guard let self else { return }
            self.loading = false
            self.readFor = Set(self.machines.map(self.key))
            self.reports = Dictionary(loaded.map { (self.key($0.machine), $0) },
                                      uniquingKeysWith: { first, _ in first })
            self.rebuild()
            self.statusLabel.stringValue = self.summary()
        }
    }

    /// The fleet total counts **fresh** readings only; every other Mac is named and left unsized.
    private func summary() -> String {
        let all = Array(reports.values)
        let fresh = all.filter(\.isFresh)
        let bytes = fresh.reduce(Int64(0)) { $0 + ($1.trashBytes ?? 0) }
        let items = fresh.reduce(0) { $0 + $1.count }
        let notCounted = all.count - fresh.count
        var parts = ["\(FleetTrash.size(bytes)) in \(items) item\(items == 1 ? "" : "s") on "
                     + "\(fresh.count) Mac\(fresh.count == 1 ? "" : "s"), counted from fresh readings"]
        if notCounted > 0 {
            parts.append("\(notCounted) Mac\(notCounted == 1 ? "" : "s") not counted (offline, stale or unknown)")
        }
        return parts.joined(separator: " · ")
    }

    /// One step: confirm, send, and say what happened. The read afterwards is what proves the freed
    /// bytes — the number is re-measured by the Mac, not taken from the delete's own receipt.
    private func empty(_ row: FleetTrashRow) {
        guard let report = row.report else { return }
        FleetTrash.empty(report, macos: row.macosTrashPart) { [weak self] outcome in
            guard let self else { return }
            self.statusLabel.stringValue = "\(row.name): \(outcome)"
            self.reports[row.key] = nil
            self.load(force: true)
        }
    }

    // MARK: Columns

    private func columns() -> [FleetColumn] {
        [
            FleetColumn(id: "mac", title: "Mac", width: 210, minWidth: 120, flex: 3,
                        text: { ($0 as? FleetTrashRow)?.name ?? "" },
                        sortValue: { .text(($0 as? FleetTrashRow)?.name ?? "") },
                        tooltip: { ($0 as? FleetTrashRow).map {
                            "\($0.machine.location ?? "") · \($0.machine.chip ?? "")"
                        } }),
            FleetColumn(id: "trash", title: "Trash", width: 190, minWidth: 120, flex: 3,
                        text: { ($0 as? FleetTrashRow)?.trashText ?? "" },
                        sortValue: { .number(Double(($0 as? FleetTrashRow)?.trashBytes ?? -1)) }),
            FleetColumn(id: "macos", title: "macOS Trash", width: 160, minWidth: 110, flex: 2,
                        text: { ($0 as? FleetTrashRow)?.macosTrashText ?? "" },
                        sortValue: { .number(Double(($0 as? FleetTrashRow)?.macosTrashPart?.bytes ?? -1)) },
                        tooltip: { _ in "Only the items this Hub moved there — never anything else in Finder's Trash." }),
            FleetColumn(id: "disk", title: "Free disk", width: 130, minWidth: 96,
                        text: { ($0 as? FleetTrashRow)?.diskText ?? "" },
                        sortValue: { .number(($0 as? FleetTrashRow)?.freeDiskBytes.map(Double.init) ?? -1) }),
            FleetColumn(id: "memory", title: "Free memory", width: 130, minWidth: 96,
                        text: { ($0 as? FleetTrashRow)?.memoryText ?? "" },
                        sortValue: { .number(($0 as? FleetTrashRow)?.freeMemoryBytes.map(Double.init) ?? -1) }),
            FleetColumn(id: "empty", title: "", width: 118, minWidth: 104, flex: 0, sortable: false,
                        text: { _ in "" },
                        cell: { [weak self] any in
                            guard let row = any as? FleetTrashRow, row.canEmpty else {
                                let none = NSTextField(labelWithString: "—")
                                none.textColor = .tertiaryLabelColor
                                none.toolTip = (any as? FleetTrashRow)?.trashText
                                return none
                            }
                            let button = FleetClosureButton(title: "Empty…") { [weak self] in
                                guard let self else { return }
                                self.empty(row)
                            }
                            button.toolTip = "Permanently delete what this Mac's Trash holds, and what the Hub "
                                + "moved to macOS Trash."
                            return button
                        })
        ]
    }
}
