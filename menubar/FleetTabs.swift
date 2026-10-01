import AppKit

// The five inner tabs of the Fleet page, and the one sortable, filterable table
// they are all built on.
//
// The old page was three tables stacked on one long scroll. The owner's words
// were "this Fleet Tab is a bit flat … we can have different tabs inside the
// fleet tab", so the page is now a shell that swaps one of these views at a time.
// Everything a table shares — click-to-sort headers, filtering, the empty state,
// row height, alternating rows — lives in `FleetTableController`, so five tables
// do not drift into five slightly different behaviours. Comments here explain
// *why* a column exists, because the numbers are easy to misread: `modalities` on
// a machine is job counts, never "models offered".

// MARK: - Tabs

enum FleetTab: Int, CaseIterable {
    case overview, locations, machines, models, jobs

    var title: String {
        switch self {
        case .overview: return "Overview"
        case .locations: return "Settings / Locations"
        case .machines: return "Machines"
        case .models: return "Local Models"
        case .jobs: return "Jobs / Generations"
        }
    }

    /// The short label the segmented control can fit.
    var shortTitle: String {
        switch self {
        case .overview: return "Overview"
        case .locations: return "Locations"
        case .machines: return "Machines"
        case .models: return "Models"
        case .jobs: return "Jobs"
        }
    }
}

// MARK: - Sorting

enum FleetSortValue {
    case text(String)
    case number(Double)
    case none

    var textValue: String {
        switch self {
        case .text(let value): return value
        case .number(let value):
            return value == value.rounded()
                ? String(format: "%.0f", value)
                : String(format: "%.1f", value)
        case .none: return ""
        }
    }
}

/// Numbers compare numerically, so "10" sorts after "2"; an unknown value always
/// sorts last, whichever direction the header was clicked.
private func fleetCompare(_ a: FleetSortValue, _ b: FleetSortValue) -> ComparisonResult {
    switch (a, b) {
    case (.number(let x), .number(let y)):
        if x == y { return .orderedSame }
        return x < y ? .orderedAscending : .orderedDescending
    case (.none, .none):
        return .orderedSame
    case (.none, _):
        return .orderedDescending
    case (_, .none):
        return .orderedAscending
    default:
        return a.textValue.localizedCaseInsensitiveCompare(b.textValue)
    }
}

// MARK: - Table controller

/// A declarative column. The closures take `Any` because one controller serves
/// several row types; each tab casts to its own model in the closure body, which
/// keeps the controller free of generics (an Objective-C protocol cannot be
/// satisfied from a generic class) without giving up a single table behaviour.
struct FleetColumn {
    let id: String
    let title: String
    let width: CGFloat
    var minWidth: CGFloat = 60
    var sortable: Bool = true
    var alignment: NSTextAlignment = .left
    /// The text drawn in a plain cell.
    let text: (Any) -> String
    /// What a header click sorts on. `.none` means "not meaningful" and sorts last.
    var sortValue: (Any) -> FleetSortValue = { _ in .none }
    /// A colour override for a plain text cell.
    var colour: ((Any) -> NSColor?)?
    /// A tooltip, so a value truncated by the column width is still readable.
    var tooltip: ((Any) -> String?)?
    /// When set, the cell draws this instead of a plain label (row actions).
    var cell: ((Any) -> NSView)?
}

/// A button whose action is a closure, so a cell can call back into a tab without
/// a selector for every column.
final class FleetClosureButton: NSButton {
    private let handler: () -> Void

    init(title: String, handler: @escaping () -> Void) {
        self.handler = handler
        super.init(frame: .zero)
        self.title = title
        self.target = self
        self.action = #selector(fire)
        bezelStyle = .rounded
        controlSize = .small
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    @objc private func fire() { handler() }
}

/// A stack that paints a tinted card, re-resolving the tint whenever the Mac
/// switches between light and dark.
///
/// **`NSColor.cgColor` snapshots one appearance.** A card tinted once at build
/// time therefore kept its light-appearance colour in dark mode — measured
/// offscreen as white label text on a pale card, which is unreadable. `updateLayer`
/// runs inside the effective appearance, so the tint follows the Mac and the text
/// on top of it stays legible in both.
final class FleetTintStack: NSStackView {
    var tint: NSColor = .systemOrange
    var tintAlpha: CGFloat = 0.08
    var borderAlpha: CGFloat = 0.35

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = tint.withAlphaComponent(tintAlpha).cgColor
        layer?.borderColor = tint.withAlphaComponent(borderAlpha).cgColor
        layer?.borderWidth = borderAlpha > 0 ? 1 : 0
        layer?.cornerRadius = 6
    }
}

/// One sortable, filterable table, plus the scroll view and empty state around it.
///
/// Sorting is stable: equal rows keep the order the data arrived in, because the
/// alternative is rows swapping places between two refreshes for no reason. A
/// numeric column is compared as a number, not as a string.
final class FleetTableController: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    let table = NSTableView()
    let scrollView = NSScrollView()
    let container = NSView()

    private let columns: [FleetColumn]
    private let emptyLabel = NSTextField(wrappingLabelWithString: "")
    private var entries: [(order: Int, row: Any)] = []
    private(set) var rows: [Any] = []
    private var sortID: String?
    private var sortAscending = true

    /// Called when the selected row changes; `nil` when nothing is selected.
    var onSelection: ((Any?) -> Void)?
    var emptyText = "Nothing to show."

    init(columns: [FleetColumn], rowHeight: CGFloat = 22) {
        self.columns = columns
        super.init()
        build(rowHeight: rowHeight)
    }

    private func build(rowHeight: CGFloat) {
        table.dataSource = self
        table.delegate = self
        table.rowHeight = rowHeight
        table.usesAlternatingRowBackgroundColors = true
        table.allowsMultipleSelection = false
        table.allowsEmptySelection = true
        table.columnAutoresizingStyle = .noColumnAutoresizing
        for spec in columns {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(spec.id))
            column.title = spec.title
            column.width = spec.width
            column.minWidth = spec.minWidth
            if spec.sortable {
                // The prototype is what turns a header into a click target; the
                // delegate callback below reads the descriptor it produces.
                column.sortDescriptorPrototype = NSSortDescriptor(key: spec.id, ascending: true)
            }
            table.addTableColumn(column)
        }

        scrollView.documentView = table
        scrollView.hasVerticalScroller = true
        // A machine table is wider than the pane at a small window; a horizontal
        // scroller keeps every column reachable rather than clipping the last one.
        scrollView.hasHorizontalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        container.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(scrollView)

        emptyLabel.font = .systemFont(ofSize: 12)
        emptyLabel.textColor = .secondaryLabelColor
        emptyLabel.alignment = .center
        emptyLabel.maximumNumberOfLines = 3
        emptyLabel.lineBreakMode = .byWordWrapping
        emptyLabel.preferredMaxLayoutWidth = 380
        emptyLabel.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(emptyLabel)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: container.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            emptyLabel.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            emptyLabel.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            emptyLabel.leadingAnchor.constraint(
                greaterThanOrEqualTo: container.leadingAnchor, constant: 16),
            emptyLabel.trailingAnchor.constraint(
                lessThanOrEqualTo: container.trailingAnchor, constant: -16),
        ])
        updateEmptyState()
    }

    // MARK: Data

    func setRows(_ newRows: [Any]) {
        entries = newRows.enumerated().map { (order: $0.offset, row: $0.element) }
        apply()
    }

    /// Restore a selection after a reload, by matching a key value. Used by the
    /// Models master table so refreshing does not lose the drilled-in model.
    func selectRow(matching key: (Any) -> Bool) {
        guard let index = rows.firstIndex(where: key) else { return }
        table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
    }

    private func apply() {
        var visible = entries
        if let sortID, let column = columns.first(where: { $0.id == sortID }) {
            visible.sort { a, b in
                let result = fleetCompare(column.sortValue(a.row), column.sortValue(b.row))
                if result == .orderedSame { return a.order < b.order }
                return sortAscending
                    ? result == .orderedAscending
                    : result == .orderedDescending
            }
        }
        rows = visible.map { $0.row }
        table.reloadData()
        updateEmptyState()
    }

    private func updateEmptyState() {
        emptyLabel.isHidden = !rows.isEmpty
        emptyLabel.stringValue = emptyText
    }

    // MARK: NSTableViewDataSource / Delegate

    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }

    func tableView(
        _ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int
    ) -> NSView? {
        guard let tableColumn, row >= 0, row < rows.count else { return nil }
        let item = rows[row]
        guard let column = columns.first(where: { $0.id == tableColumn.identifier.rawValue }) else {
            return nil
        }
        if let cell = column.cell { return cell(item) }

        let cell = NSTableCellView()
        let label = NSTextField(labelWithString: column.text(item))
        label.font = .systemFont(ofSize: 11.5)
        label.lineBreakMode = .byTruncatingTail
        label.alignment = column.alignment
        if let colour = column.colour?(item) { label.textColor = colour }
        if let tooltip = column.tooltip?(item), !tooltip.isEmpty {
            label.toolTip = tooltip
            cell.toolTip = tooltip
        }
        label.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(label)
        cell.textField = label
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        let index = table.selectedRow
        onSelection?(index >= 0 && index < rows.count ? rows[index] : nil)
    }

    func tableView(
        _ tableView: NSTableView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]
    ) {
        guard let descriptor = tableView.sortDescriptors.first,
              let key = descriptor.key,
              columns.contains(where: { $0.id == key }) else { return }
        sortID = key
        sortAscending = descriptor.ascending
        apply()
    }
}

/// Shared filter state, built once by the shell and handed to each table so the
/// location popup and the search field mean the same thing on every tab.
struct FleetFilter {
    var locationID: String?
    var query: String = ""

    func matchesLocation(_ id: String?) -> Bool {
        guard let locationID else { return true }
        return id == locationID
    }

    func matchesSearch(_ fields: [String?]) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return true }
        return fields.contains { ($0 ?? "").lowercased().contains(needle) }
    }
}

// MARK: - Shared view helpers

func fleetFlag(_ value: Bool?) -> String {
    guard let value else { return "—" }
    return value ? "yes" : "no"
}

func fleetButtonCell(_ buttons: [NSButton]) -> NSView {
    let cell = NSTableCellView()
    let stack = NSStackView(views: buttons)
    stack.orientation = .horizontal
    stack.spacing = 4
    stack.translatesAutoresizingMaskIntoConstraints = false
    cell.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
        stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        stack.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -4),
    ])
    return cell
}

/// A cell whose text wraps to a couple of lines — for a fit hint or an error.
/// The table's row height is raised to match, so the text is readable rather than
/// elided at one line.
func fleetWrappingCell(_ text: String, colour: NSColor = .labelColor) -> NSView {
    let cell = NSTableCellView()
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .systemFont(ofSize: 11)
    label.textColor = colour
    label.isSelectable = true
    label.maximumNumberOfLines = 2
    label.lineBreakMode = .byWordWrapping
    label.preferredMaxLayoutWidth = 220
    label.translatesAutoresizingMaskIntoConstraints = false
    cell.addSubview(label)
    cell.textField = label
    NSLayoutConstraint.activate([
        label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
        label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -4),
        label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
    ])
    return cell
}

/// The base of a tab whose body scrolls.
///
/// A tab with a form or a long list has a *required* height, and a required height
/// propagates outwards until AppKit satisfies it by growing the window — the Fleet
/// page once did that to 1162pt on a 1080pt screen. The scroll view gives the
/// content somewhere to go instead. The document view must be flipped, or AppKit
/// scrolls to the bottom of a page whose origin is bottom-left.
class FleetTabView: NSView {
    let page = PageKit.page()
    private let scroll = NSScrollView()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(page)

        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document
        addSubview(scroll)

        NSLayoutConstraint.activate([
            page.topAnchor.constraint(equalTo: document.topAnchor),
            page.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            page.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            page.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            scroll.topAnchor.constraint(equalTo: topAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Pin a row to the usable width inside the page's padding.
    func fullWidth(_ view: NSView) {
        view.widthAnchor.constraint(
            equalTo: page.widthAnchor, constant: -PageKit.pagePadding * 2
        ).isActive = true
    }
}

// MARK: - Overview

/// The page at a glance: the headline numbers, the Hub's pulse, and the list of
/// things that need fixing — errors before warnings before notes, each with the
/// tab that can act on it.
final class FleetOverviewTab: FleetTabView {
    var onOpenTab: ((FleetTab) -> Void)?
    /// Re-runs the model refresh for one location, by its provider id.
    var onRefreshLocation: ((String) -> Void)?

    private let tiles = NSStackView()
    private let pulseLabel = PageKit.label("", size: 11.5, colour: .tertiaryLabelColor, wrapping: true)
    private let statusLabel = PageKit.label("Loading…", size: 12, colour: .secondaryLabelColor, wrapping: true)
    private let issuesStack = NSStackView()
    /// Issue location id → provider id, so an issue can offer the right refresh.
    private var providerIDs: [String: String] = [:]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        page.addArrangedSubview(PageKit.sectionTitle("At a glance"))
        tiles.orientation = .vertical
        tiles.alignment = .leading
        tiles.spacing = 10
        page.addArrangedSubview(tiles)
        page.addArrangedSubview(pulseLabel)
        page.addArrangedSubview(statusLabel)
        page.addArrangedSubview(PageKit.sectionTitle("Issues"))
        issuesStack.orientation = .vertical
        issuesStack.alignment = .leading
        issuesStack.spacing = 8
        page.addArrangedSubview(issuesStack)
        fullWidth(pulseLabel)
        fullWidth(statusLabel)
        fullWidth(tiles)
        fullWidth(issuesStack)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func setStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        statusLabel.stringValue = text
        statusLabel.textColor = colour
    }

    func setProviderIDs(_ map: [String: String]) {
        providerIDs = map
    }

    func update(snapshot: FleetSnapshot, filter: FleetFilter = FleetFilter()) {
        rebuildTiles(summary: snapshot.summary, snapshot: snapshot)
        pulseLabel.stringValue = pulseText(summary: snapshot.summary, snapshot: snapshot)
        // The location filter is drawn above every tab, so it has to mean the same
        // thing on this one: narrowing to one Mac must not leave another Mac's
        // problems on screen.
        rebuildIssues(snapshot.issues.filter { filter.matchesLocation($0.locationId) })
    }

    private func rebuildTiles(summary: FleetSummary?, snapshot: FleetSnapshot) {
        clear(tiles)
        let locationsUp = summary?.locationsUp ?? snapshot.locations.filter { $0.reachable == true }.count
        let locationsTotal = summary?.locations ?? snapshot.locations.count
        let online = summary?.machinesOnline ?? 0
        let offline = summary?.machinesOffline ?? 0
        let unknown = summary?.machinesUnknown ?? 0
        let workersReady = summary?.workersReady
        let workersTotal = summary?.workersTotal
        let modelsAvailable = summary?.modelsAvailable
        let modelsTotal = summary?.localModelsTotal
        let done = summary?.jobsCompleted ?? 0
        let failed = summary?.jobsFailed ?? 0
        let running = summary?.runningJobs ?? snapshot.running.count

        let data: [(String, String, NSColor?)] = [
            ("\(locationsUp)/\(locationsTotal)", "locations reachable", nil),
            ("\(online)",
             "machines online · \(offline) offline" + (unknown > 0 ? " · \(unknown) no state" : ""),
             offline > 0 ? .systemOrange : nil),
            ("\(workersReady.map(String.init) ?? "—")/\(workersTotal.map(String.init) ?? "—")",
             "workers ready", nil),
            ("\(modelsAvailable.map(String.init) ?? "—")/\(modelsTotal.map(String.init) ?? "—")",
             "local models available", nil),
            ("\(done)", "jobs done" + (failed > 0 ? " · \(failed) failed" : ""),
             failed > 0 ? .systemOrange : nil),
            ("\(running)", "running now", nil),
        ]
        // Three per row: six across would need ~900pt and the page has about 700.
        var rowViews: [NSView] = []
        for (index, tile) in data.enumerated() {
            rowViews.append(tileView(tile.0, tile.1, colour: tile.2))
            if rowViews.count == 3 || index == data.count - 1 {
                tiles.addArrangedSubview(PageKit.row(rowViews, spacing: 12))
                rowViews = []
            }
        }
        if let hidden = summary?.probeMachinesHidden, hidden > 0 {
            tiles.addArrangedSubview(PageKit.help(
                "\(hidden) Hub connectivity probe\(hidden == 1 ? "" : "s") hidden — those are not your machines."
            ))
        }
    }

    private func tileView(_ value: String, _ caption: String, colour: NSColor?) -> NSView {
        let valueLabel = PageKit.label(value, size: 20, weight: .semibold, colour: colour ?? .labelColor)
        let captionLabel = PageKit.label(caption, size: 11, colour: .secondaryLabelColor, wrapping: true)
        captionLabel.preferredMaxLayoutWidth = 142
        let stack = FleetTintStack(views: [valueLabel, captionLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        stack.wantsLayer = true
        // A tile is a surface, not a status: no tint, no border, just the control
        // background so it reads as a card in both appearances.
        stack.tint = .controlBackgroundColor
        stack.tintAlpha = 1
        stack.borderAlpha = 0
        stack.widthAnchor.constraint(equalToConstant: 166).isActive = true
        return stack
    }

    /// The Hub's own view of what every machine is doing, shown verbatim rather
    /// than re-derived. Falls back to the machine list when the pulse is absent.
    private func pulseText(summary: FleetSummary?, snapshot: FleetSnapshot) -> String {
        let pulse = summary?.pulse ?? [:]
        let active = pulse.filter { $0.value > 0 && $0.key != "offline" }
        if !active.isEmpty {
            return active.sorted { $0.key < $1.key }
                .map { "\($0.value) \($0.key.replacingOccurrences(of: "_", with: " "))" }
                .joined(separator: " · ")
        }
        let offline = summary?.machinesOffline ?? 0
        if offline > 0 {
            return "\(offline) machine\(offline == 1 ? "" : "s") not reporting"
        }
        return snapshot.machines.isEmpty ? "No machines are reporting." : ""
    }

    private func rebuildIssues(_ issues: [FleetIssue]) {
        clear(issuesStack)
        guard !issues.isEmpty else {
            issuesStack.addArrangedSubview(
                PageKit.label("Nothing needs attention.", size: 12.5, colour: .secondaryLabelColor))
            return
        }
        let ordered = issues.sorted {
            let a = FleetFormat.severityRank($0.severity)
            let b = FleetFormat.severityRank($1.severity)
            if a != b { return a < b }
            return ($0.location ?? "").localizedCaseInsensitiveCompare($1.location ?? "") == .orderedAscending
        }
        // **"Wants a person" and "informational" are drawn apart.** Under one
        // heading they read the same, which is how a Hub measurement gap ("direct
        // activity partially unavailable") was taken for something broken. The
        // count leads each group, so the top of the list is only what is worth
        // acting on.
        let problems = ordered.filter { ($0.severity ?? "") != "info" }
        let notes = ordered.filter { ($0.severity ?? "") == "info" }

        func add(_ issue: FleetIssue) {
            let row = issueRow(issue)
            issuesStack.addArrangedSubview(row)
            fullWidth(row)
        }

        if !problems.isEmpty {
            issuesStack.addArrangedSubview(PageKit.label(
                "\(problems.count) to look at", size: 11, weight: .semibold,
                colour: .secondaryLabelColor))
            problems.forEach(add)
        }
        if !notes.isEmpty {
            issuesStack.addArrangedSubview(PageKit.label(
                problems.isEmpty
                    ? "\(notes.count) informational"
                    : "\(notes.count) informational — nothing to fix",
                size: 11, weight: .semibold, colour: .tertiaryLabelColor))
            notes.forEach(add)
        }
    }

    /// One issue, as a card: what is wrong, where, and the button that goes there.
    ///
    /// The whole point of this list is the owner's "some status update so i can go
    /// and fix it as it tells me exactly what's wrong" — so the message and the
    /// action phrase are always drawn, never summarised into a colour.
    private func issueRow(_ issue: FleetIssue) -> NSView {
        let colour = FleetFormat.severityColour(issue.severity)
        let icon = NSImageView()
        icon.image = NSImage(
            systemSymbolName: FleetFormat.severitySymbol(issue.severity),
            accessibilityDescription: issue.severity
        )
        icon.contentTintColor = colour
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 14, weight: .semibold)

        let subject = PageKit.label(FleetFormat.text(issue.subject), size: 12.5, weight: .semibold)
        let whereLabel = PageKit.label(
            "· \(FleetFormat.text(issue.location))", size: 11.5, colour: .secondaryLabelColor)
        let heading = PageKit.row([subject, whereLabel, NSView()])
        let message = PageKit.label(
            FleetFormat.text(issue.message), size: 11.5, colour: .labelColor, wrapping: true)
        message.maximumNumberOfLines = 4
        var detailViews: [NSView] = [heading, message]
        if let action = issue.action, !action.trimmingCharacters(in: .whitespaces).isEmpty {
            detailViews.append(PageKit.label(
                "Fix: \(action)", size: 11.5, colour: .secondaryLabelColor, wrapping: true))
        }
        let detail = NSStackView(views: detailViews)
        detail.orientation = .vertical
        detail.alignment = .leading
        detail.spacing = 4
        detail.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let target = issueTargetTab(issue)
        let open = FleetClosureButton(title: "Open \(target.shortTitle)") { [weak self] in
            self?.onOpenTab?(target)
        }
        var trailing: [NSView] = [open]
        if let providerID = providerID(for: issue) {
            // The refresh re-reads the location's models *and* reloads the fleet
            // snapshot, so it is useful for any issue that names a location: a
            // stalled batch clears if the Hub has since moved it on.
            let title = target == .models ? "Refresh models" : "Refresh location"
            trailing.append(FleetClosureButton(title: title) { [weak self] in
                self?.onRefreshLocation?(providerID)
            })
        }

        let row = FleetTintStack(views: [icon, detail, NSView()] + trailing)
        row.orientation = .horizontal
        row.alignment = .top
        row.spacing = 10
        row.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        row.wantsLayer = true
        row.tint = colour
        row.translatesAutoresizingMaskIntoConstraints = false
        return row
    }

    /// Where an issue's fix lives.
    ///
    /// The gateway already routes each issue, in its `action` phrase — "Fleet →
    /// Machines", "Studio Hub on that Mac" — so that is read first and the words
    /// are only a fallback. Using the hub's own routing means a new issue code
    /// lands on the right tab without this list being edited.
    private func issueTargetTab(_ issue: FleetIssue) -> FleetTab {
        let action = (issue.action ?? "").lowercased()
        if action.contains("jobs") { return .jobs }
        if action.contains("machines") { return .machines }
        if action.contains("models") { return .models }
        if action.contains("locations") { return .locations }
        let haystack = "\(issue.code ?? "") \(issue.subject ?? "") \(issue.message ?? "")".lowercased()
        if haystack.contains("machine") || haystack.contains("node") { return .machines }
        if haystack.contains("model") || haystack.contains("package") || haystack.contains("catalog") {
            return .models
        }
        return .locations
    }

    private func providerID(for issue: FleetIssue) -> String? {
        guard let id = issue.locationId, !id.isEmpty else { return nil }
        return providerIDs[id]
    }

    private func clear(_ stack: NSStackView) {
        for view in stack.arrangedSubviews { view.removeFromSuperview() }
    }
}

// MARK: - Locations

/// The roster: the table with the row actions, then the form that adds one and
/// the two-token explanation that makes pasting the right token possible.
final class FleetLocationsTab: FleetTabView {
    var onTest: ((FleetLocation) -> Void)?
    var onRename: ((FleetLocation) -> Void)?
    var onCredentials: ((FleetLocation) -> Void)?
    var onRemove: ((FleetLocation) -> Void)?
    /// Handover: re-point this location at a new address, keeping its identity and decisions.
    var onChangeAddress: ((FleetLocation) -> Void)?
    /// Clear a pause. Only offered while the gateway reports the location paused.
    var onResume: ((FleetLocation) -> Void)?
    var onRefresh: (() -> Void)?
    var onRetryFailed: (() -> Void)?
    var onAddLocation: ((String, String, String) -> Void)?
    /// Asks the shell to run the publish flow — the open panel, the version/build
    /// alert and the network all belong there, not in a tab that a layout test
    /// builds without a gateway.
    var onPublishRelease: (() -> Void)?

    private lazy var controller = FleetTableController(columns: columns())
    private let statusLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
    private lazy var retryButton: FleetClosureButton = {
        let button = FleetClosureButton(title: "Retry failed") { [weak self] in
            self?.onRetryFailed?()
        }
        button.isHidden = true
        return button
    }()
    /// The per-controller release report, rebuilt on every fleet read.
    private let releaseStack = NSStackView()
    private let releaseStatus = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
    /// This gateway's own publisher fingerprint, so a controller that trusts it
    /// reads "this gateway" rather than an opaque hex string.
    private var identityFingerprint: String?
    private lazy var publishButton: FleetClosureButton = {
        FleetClosureButton(title: "Publish a Studio Hub release…") { [weak self] in
            self?.onPublishRelease?()
        }
    }()
    private let labelField = NSTextField(string: "")
    private let urlField = NSTextField(string: "")
    private let tokenField = NSSecureTextField(string: "")
    private let addStatus = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)

        let refresh = FleetClosureButton(title: "Refresh") { [weak self] in self?.onRefresh?() }
        // A wrapping label inside a horizontal row must be told where to break and
        // must not also claim the row's full width, or it fights the buttons beside it.
        statusLabel.preferredMaxLayoutWidth = 320
        let header = PageKit.row([statusLabel, NSView(), retryButton, refresh])
        page.addArrangedSubview(header)
        fullWidth(header)

        controller.emptyText = "No locations have been read yet."
        controller.container.heightAnchor.constraint(equalToConstant: 240).isActive = true
        page.addArrangedSubview(controller.container)
        fullWidth(controller.container)

        // ---- Release
        //
        // What the controllers *hold*, beside what the last push did with it. The
        // two are different facts: a controller accepting a push is not the same as
        // a controller that stored it, and only the controller's own read-back
        // answers the second. A refusal is quoted word for word.
        page.addArrangedSubview(PageKit.sectionTitle("Release"))
        page.addArrangedSubview(PageKit.help(
            "The Studio Hub app release this gateway signs and pushes to your controllers. "
            + "“Held” is what each controller reports it has now — not what was pushed. "
            + "A refusal below is the controller's own sentence, quoted exactly."
        ))
        let publishRow = PageKit.row([publishButton, NSView()])
        page.addArrangedSubview(publishRow)
        fullWidth(publishRow)
        releaseStatus.preferredMaxLayoutWidth = 520
        releaseStatus.isHidden = true
        page.addArrangedSubview(releaseStatus)
        releaseStack.orientation = .vertical
        releaseStack.alignment = .leading
        releaseStack.spacing = 8
        page.addArrangedSubview(releaseStack)
        fullWidth(releaseStack)

        page.addArrangedSubview(PageKit.sectionTitle("Add a location"))
        page.addArrangedSubview(PageKit.help(
            "Another Mac running Studio Hub. Standalone or controller, both work. "
            + "A new machine can be added while it is still standalone and keep working "
            + "after it is promoted to a controller; nothing here depends on the role. "
            + "Enter its address and its Fleet token — the Fleet token is the one this "
            + "gateway needs to send work there and collect the results. A controller "
            + "reports its own nodes — this gateway never contacts them directly. Every "
            + "location below can be renamed at any time: a name is a label, and no "
            + "address, credential or model depends on it. On the other Mac, Studio Hub "
            + "must be told to answer on a network this one can reach: in its Settings, "
            + "stop the services, tick its Tailscale network under "
            + "\u{201C}Listening on\u{201D}, and start them again. It answers only that Mac "
            + "itself by default, and nothing here can change that from a distance."
        ))
        // The Hub's own Connections page labels these two and says what each is for.
        // A person pasting a token here has to be able to tell which one this page
        // wants before they paste it: both reach every route the gateway calls, so
        // the wrong one works silently until the day it matters.
        page.addArrangedSubview(PageKit.help(
            "Two tokens, two jobs.\n\n"
            + "Hub token — the Mac's own key.\n"
            + "Who it is for: the person who owns that Mac, using that Mac.\n"
            + "What it does: change settings, install or remove models, delete output, "
            + "manage shared voices.\n"
            + "Never send it to GatewayKH unless GatewayKH will manage shared voices on "
            + "that Mac. Anyone who holds it can do everything on that Mac.\n\n"
            + "Fleet token — the work badge for the group.\n"
            + "Who it is for: GatewayKH, and every Mac that joins a controller.\n"
            + "What it allows: send work, follow the job, download the result, "
            + "acknowledge it, cancel it or discard an uncertain one, and read the model "
            + "and fleet lists.\n"
            + "What it cannot do: change settings, install or remove models, delete "
            + "output, manage shared voices, or make a Mac join or leave.\n"
            + "This is the token to enter here."
        ))
        // Joining is the Mac's own act, on the Mac itself. Nothing on this side can
        // enlist or dismiss a machine, and saying so here stops "why can't GatewayKH
        // just add it?" from becoming a support question.
        page.addArrangedSubview(PageKit.help(
            "Joining is that Mac's own decision, made from its own app with the "
            + "controller's Fleet token. GatewayKH cannot join or unjoin a Mac."
        ))

        labelField.placeholderString = "Name (e.g. Blue Room)"
        labelField.widthAnchor.constraint(equalToConstant: 220).isActive = true
        urlField.placeholderString = "http://10.0.0.5:47873"
        urlField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        tokenField.placeholderString = "the controller's Fleet token"
        tokenField.widthAnchor.constraint(equalToConstant: 260).isActive = true
        let addButton = FleetClosureButton(title: "Add location") { [weak self] in
            guard let self else { return }
            self.onAddLocation?(
                self.labelField.stringValue.trimmingCharacters(in: .whitespaces),
                self.urlField.stringValue.trimmingCharacters(in: .whitespaces),
                self.tokenField.stringValue.trimmingCharacters(in: .whitespaces)
            )
        }

        page.addArrangedSubview(PageKit.formRow("Name", labelField))
        page.addArrangedSubview(PageKit.formRow("Address", urlField))
        page.addArrangedSubview(PageKit.formRow(
            "Fleet token",
            [tokenField, PageKit.help(
                "The token this gateway needs to send work to that Mac and collect the results."
            )]
        ))
        page.addArrangedSubview(PageKit.formRow("", addButton))
        page.addArrangedSubview(PageKit.formRow("", addStatus))
        // The status line wraps rather than pinning to the page width: it lives in
        // a form row beside a caption and its own width belongs to that column.
        addStatus.preferredMaxLayoutWidth = 520
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func columns() -> [FleetColumn] {
        func location(_ any: Any) -> FleetLocation? { any as? FleetLocation }
        return [
            FleetColumn(
                id: "llocation", title: "Location", width: 160, minWidth: 90,
                text: { location($0)?.label ?? "—" },
                sortValue: { .text(location($0)?.label ?? "") }
            ),
            FleetColumn(
                id: "lrole", title: "Role", width: 90,
                text: { FleetFormat.text(location($0)?.role) },
                sortValue: { .text(location($0)?.role ?? "") }
            ),
            FleetColumn(
                id: "lhub", title: "Hub", width: 110, sortable: false,
                // **The version, and whether this gateway was built against it.**
                // Studio Hub auto-updates itself, so a controller can move ahead
                // of this app without anyone touching it; the relation is drawn
                // rather than left in a tooltip, because "which of my Macs is on
                // something this build has never seen?" is the question a drift
                // raises. Nothing is refused on a mismatch — see
                // `SUPPORTED_STUDIO_HUB_VERSION`.
                text: { row in
                    guard let row = location(row) else { return "—" }
                    let stated = FleetFormat.text(row.version)
                    switch row.hubVersionRelation {
                    case "older": return "\(stated) · older"
                    case "newer": return "\(stated) · newer"
                    default: return stated
                    }
                },
                colour: { row in
                    let relation = location(row)?.hubVersionRelation
                    return relation == "older" || relation == "newer"
                        ? .systemOrange : .labelColor
                },
                tooltip: { row in
                    guard let row = location(row) else { return nil }
                    switch row.hubVersionRelation {
                    case "older":
                        return "Older than the Studio Hub this gateway build targets. "
                            + "It keeps working; a route or field added since then is not read."
                    case "newer":
                        return "Newer than the Studio Hub this gateway build targets. It keeps "
                            + "working; anything it serves in a lane this build does not know "
                            + "is listed as unsupported rather than offered."
                    case "match":
                        return "The Studio Hub release this gateway build targets."
                    default:
                        return "The controller stated no Hub version."
                    }
                }
            ),
            FleetColumn(
                id: "lmachines", title: "Machines", width: 90,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    guard row.reachable == true else { return "—" }
                    return "\(row.machineSummary?.total ?? row.machines.count)"
                },
                sortValue: { row in
                    guard let row = location(row) else { return .none }
                    return .number(Double(row.machineSummary?.total ?? row.machines.count))
                }
            ),
            FleetColumn(
                id: "lmembers", title: "Members", width: 130,
                // **Who has joined this controller** (audit H10). The Hub publishes
                // `node_summary` per location and this page never decoded it, so
                // "able to see each member connected" meant opening Studio Hub on the
                // controller. The fraction is the controller's own count of nodes that
                // answered out of the roster it holds; a refused credential is spelled
                // out rather than folded into "offline", because it is the one state
                // with a fix a person has to perform.
                text: { row in
                    guard let row = location(row) else { return "—" }
                    return row.membersText
                },
                sortValue: { row in
                    guard let count = location(row)?.memberCount else { return .none }
                    return .number(Double(count.reachable))
                },
                colour: { row in
                    guard let row = location(row) else { return .labelColor }
                    if (row.nodeSummary?.unauthorized ?? 0) > 0 { return .systemRed }
                    if let count = row.memberCount, count.total > 0, count.reachable == 0 {
                        return .systemOrange
                    }
                    return .labelColor
                },
                tooltip: { row in location(row)?.membersTooltip }
            ),
            FleetColumn(
                id: "lmodels", title: "Models (offered/discovered)", width: 170,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    // The explicit counts when the gateway states them; otherwise
                    // derived from the (model, machine) rows it does send, so a
                    // location with models never reads as "—".
                    let offered = row.modelsPublishable ?? row.modelsEnabled
                        ?? (row.localModels.isEmpty ? nil : row.derivedModelsOffered)
                    let discovered = row.modelsDiscovered
                        ?? (row.localModels.isEmpty ? nil : row.derivedModelsDiscovered)
                    switch (offered, discovered) {
                    case let (offered?, discovered?): return "\(offered) / \(discovered)"
                    case let (offered?, nil): return "\(offered) / —"
                    case let (nil, discovered?): return "— / \(discovered)"
                    default: return "—"
                    }
                },
                sortValue: { row in
                    guard let row = location(row) else { return .none }
                    let offered = row.modelsPublishable ?? row.modelsEnabled
                        ?? (row.localModels.isEmpty ? nil : row.derivedModelsOffered)
                    return .number(Double(offered ?? 0))
                }
            ),
            FleetColumn(
                id: "lrelease", title: "Release", width: 150,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    return row.release?.versionBuild ?? "—"
                },
                sortValue: { row in
                    guard let release = location(row)?.release else { return .none }
                    return .text(release.versionBuild)
                },
                tooltip: { row in
                    guard let release = location(row)?.release else { return nil }
                    var parts: [String] = []
                    if let filename = release.filename, !filename.isEmpty {
                        parts.append(filename)
                    }
                    if let sha = release.sha256, !sha.isEmpty { parts.append("sha256 \(sha)") }
                    if let key = release.publisherKeyId, !key.isEmpty {
                        parts.append("signed by \(key)")
                    }
                    if let key = release.controllerKeyId, !key.isEmpty {
                        parts.append("countersigned by \(key)")
                    }
                    return parts.isEmpty ? nil : parts.joined(separator: "\n")
                }
            ),
            FleetColumn(
                id: "lstatus", title: "Status", width: 240, sortable: false,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    let address = FleetFormat.text(row.url)
                    // A pause outranks the raw probe error: it says what the gateway did about
                    // it (routing skips this location; nothing was removed) and how to undo it.
                    if row.pausedAt != nil {
                        let reason = row.pauseReason ?? "Paused: unreachable."
                        return address.isEmpty ? reason : "\(address)\n\(reason)"
                    }
                    if row.reachable == true { return address }
                    // **The address is what you need most when it is not answering.** This cell
                    // used to *replace* the address with the reason, so "Nothing is listening on
                    // that address" never said which address — and the only place the address was
                    // ever printed was the branch you could not reach. The owner read three rows
                    // saying exactly that and could not tell what the gateway had dialled.
                    // `row.url` is present for an unreachable location too; it was simply dropped.
                    let reason = row.error ?? "Not reachable"
                    return address.isEmpty ? reason : "\(address)\n\(reason)"
                },
                colour: { row in
                    location(row)?.reachable == true ? .secondaryLabelColor : .systemOrange
                }
            ),
            FleetColumn(
                id: "lactions", title: "Actions", width: 420, minWidth: 400, sortable: false,
                text: { _ in "" },
                cell: { [weak self] any in
                    guard let self, let row = location(any) else { return NSView() }
                    return self.actions(for: row)
                }
            ),
        ]
    }

    /// Test / Rename / Credentials / Change address / Resume (paused only) / Remove, per location.
    ///
    /// This Mac gets Rename only: it has no address to test and nothing to remove.
    private func actions(for row: FleetLocation) -> NSView {
        var buttons: [NSButton] = []
        if row.local != true {
            buttons.append(FleetClosureButton(title: "Test") { [weak self] in self?.onTest?(row) })
        }
        buttons.append(FleetClosureButton(title: "Rename") { [weak self] in self?.onRename?(row) })
        if row.local != true {
            // One credentials action, not two: five buttons did not fit the cell,
            // and the overflow was left out of the hierarchy entirely — so Remove
            // was unreachable while its endpoint worked.
            buttons.append(FleetClosureButton(title: "Credentials…") { [weak self] in
                self?.onCredentials?(row)
            })
            // Handover, not Remove + Add: a second row orphans the first one's credential
            // and every per-model enable decision (see the gateway's `replace_address`).
            buttons.append(FleetClosureButton(title: "Change address…") { [weak self] in
                self?.onChangeAddress?(row)
            })
            if row.pausedAt != nil {
                buttons.append(FleetClosureButton(title: "Resume") { [weak self] in self?.onResume?(row) })
            }
            buttons.append(FleetClosureButton(title: "Remove") { [weak self] in self?.onRemove?(row) })
        }
        return fleetButtonCell(buttons)
    }

    func update(locations: [FleetLocation]) {
        controller.setRows(locations)
    }

    /// The Release section: what each configured controller holds now, and what
    /// the last push did with it.
    ///
    /// `state` is the top-level `release` object from the fleet read. A gateway
    /// that sends none leaves the rows saying so; a controller the last push
    /// never reached says "not pushed from here yet" rather than borrowing
    /// another controller's outcome. `unavailable` is for the case the *read*
    /// failed: "I could not read the answer" must not look like "there is none".
    func updateRelease(
        locations: [FleetLocation], state: FleetReleaseState?, unavailable: String? = nil
    ) {
        for view in releaseStack.arrangedSubviews {
            releaseStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        if let unavailable, !unavailable.isEmpty {
            addReleaseLine(unavailable, colour: .systemOrange)
            return
        }
        let report = state?.lastPush
        let reportKnown = state != nil
        identityFingerprint = state?.identity?.fingerprint
        if let error = state?.error, !error.isEmpty {
            addReleaseLine(error, colour: .systemOrange)
        }
        if let report, let updated = report.updatedAt {
            addReleaseLine(
                "Last push from this gateway: \(report.release?.versionBuild ?? "—"), "
                + "\(FleetFormat.relative(updated)).",
                colour: .secondaryLabelColor
            )
        } else if reportKnown {
            addReleaseLine(
                "This gateway has not pushed a release yet.", colour: .secondaryLabelColor
            )
        } else {
            addReleaseLine(
                "This gateway did not report a release status.", colour: .systemOrange
            )
        }
        // The local Mac is a location but not a controller: releases are pushed to
        // configured controllers only, so it has no row here.
        let controllers = locations.filter { $0.local != true }
        guard !controllers.isEmpty else {
            addReleaseLine("No controllers are configured yet.", colour: .secondaryLabelColor)
            return
        }
        for controller in controllers {
            let outcome = report?.outcomes[controller.id ?? ""]
            let row = releaseRow(for: controller, outcome: outcome, reportKnown: reportKnown)
            releaseStack.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: releaseStack.widthAnchor).isActive = true
        }
    }

    /// Disable the publish button while a push is in flight and say what it is
    /// doing, because a 1.1 GB artifact in one base64 body is a long wait.
    func setPublishing(_ publishing: Bool, message: String) {
        publishButton.isEnabled = !publishing
        releaseStatus.isHidden = !publishing
        releaseStatus.stringValue = publishing ? message : ""
    }

    private func addReleaseLine(_ text: String, colour: NSColor) {
        let label = PageKit.label(text, size: 11.5, colour: colour, wrapping: true)
        label.preferredMaxLayoutWidth = 620
        releaseStack.addArrangedSubview(label)
        label.widthAnchor.constraint(equalTo: releaseStack.widthAnchor).isActive = true
    }

    private func releaseRow(
        for location: FleetLocation, outcome: FleetPushOutcome?, reportKnown: Bool
    ) -> NSView {
        let name = PageKit.label(
            location.label ?? location.id ?? "Controller", size: 12, weight: .semibold
        )
        name.lineBreakMode = .byTruncatingTail
        name.setContentHuggingPriority(.required, for: .horizontal)
        name.widthAnchor.constraint(equalToConstant: 170).isActive = true

        let heldText = location.release.map { "held \($0.versionBuild)" } ?? "held none"
        let held = PageKit.label(
            heldText, size: 11.5,
            colour: location.release == nil ? .secondaryLabelColor : .labelColor
        )
        held.lineBreakMode = .byTruncatingTail
        let top = PageKit.row([name, held, NSView()], spacing: 8)

        let outcomeLabel = PageKit.label(
            releaseOutcomeText(outcome, for: location, reportKnown: reportKnown),
            size: 11.5, colour: releaseOutcomeColour(outcome, reportKnown: reportKnown),
            wrapping: true
        )
        outcomeLabel.preferredMaxLayoutWidth = 620

        // What the controller trusts decides whether a push will be accepted at
        // all, so it is shown beside what it holds — before the push, not only
        // after a refusal.
        let trustLabel = PageKit.label(
            trustedPublisherText(for: location),
            size: 11.5, colour: trustedPublisherColour(for: location), wrapping: true
        )
        trustLabel.preferredMaxLayoutWidth = 620

        let stack = NSStackView(views: [top, trustLabel, outcomeLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 2
        stack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return stack
    }

    /// What the controller says it trusts. `reported == false` is "this Hub does
    /// not carry the field" (older than 0.6.51), which is **not** the same answer
    /// as an installed `null` — that one means it trusts nobody and a push will
    /// be refused until the gateway's fingerprint is installed.
    private func trustedPublisherText(for location: FleetLocation) -> String {
        guard let trust = location.trustedPublisher, trust.reported == true else {
            return "Trusted publisher: the Hub did not report one."
        }
        let fingerprint = (trust.fingerprint ?? "").trimmingCharacters(in: .whitespaces)
        guard !fingerprint.isEmpty else {
            return "Trusted publisher: none installed yet — a push is refused until you install "
                + "this gateway's fingerprint there (Settings → Studio Hub app releases)."
        }
        let who = (trust.publisherId ?? "").trimmingCharacters(in: .whitespaces)
        let short = String(fingerprint.prefix(12))
        let named = fingerprint == identityFingerprint ? "this gateway" : (who.isEmpty ? "unnamed" : who)
        return "Trusted publisher: \(named) \(short)…"
    }

    private func trustedPublisherColour(for location: FleetLocation) -> NSColor {
        guard let trust = location.trustedPublisher, trust.reported == true else {
            return .secondaryLabelColor
        }
        let fingerprint = (trust.fingerprint ?? "").trimmingCharacters(in: .whitespaces)
        guard !fingerprint.isEmpty else { return .systemOrange }
        return fingerprint == identityFingerprint ? .systemGreen : .systemOrange
    }

    /// The last push's outcome for one controller. A refusal is the gateway's or
    /// the controller's own sentence, kept exactly — a 409 already names the
    /// fingerprint to install, and nothing here rewrites it.
    private func releaseOutcomeText(
        _ outcome: FleetPushOutcome?, for location: FleetLocation, reportKnown: Bool
    ) -> String {
        guard let outcome else {
            return reportKnown ? "Not pushed from here yet." : "No push outcome was reported."
        }
        if outcome.ok == true {
            let readBack = outcome.release?.versionBuild ?? location.release?.versionBuild
            if let readBack, readBack != "—" {
                return "Last push accepted — it read back \(readBack)."
            }
            return "Last push accepted — it read back no release."
        }
        let detail = (outcome.detail ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !detail.isEmpty { return "Last push refused — \(detail)" }
        if let code = outcome.code, !code.isEmpty {
            return "Last push refused — \(code)"
        }
        return "Last push refused — the controller gave no reason."
    }

    /// 409 is the actionable "no trusted publisher key yet" case, so it stays
    /// orange; anything else is a harder failure and reads red.
    private func releaseOutcomeColour(
        _ outcome: FleetPushOutcome?, reportKnown: Bool
    ) -> NSColor {
        guard let outcome else {
            return reportKnown ? .secondaryLabelColor : .systemOrange
        }
        if outcome.ok == true { return .systemGreen }
        // A refused downgrade is a decision for the owner, not a failure: orange,
        // and the detail says what it holds and how to publish anyway.
        if outcome.downgrade == true { return .systemOrange }
        return outcome.status == 409 ? .systemOrange : .systemRed
    }

    func setStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        statusLabel.stringValue = text
        statusLabel.textColor = colour
    }

    func setRetryFailedVisible(_ visible: Bool) {
        retryButton.isHidden = !visible
    }

    func setAddStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        addStatus.stringValue = text
        addStatus.textColor = colour
    }

    func clearAddForm() {
        labelField.stringValue = ""
        urlField.stringValue = ""
        tokenField.stringValue = ""
    }
}

// MARK: - Machines

/// Every machine in the fleet, with what it has done and what it can serve.
///
/// Two columns are easy to confuse and are deliberately named apart: **Studios**
/// is which worker modalities the machine serves (from the workers list), while
/// **Work done by kind** is the count of jobs it has completed per modality. The
/// second is not "models offered" and says so in its header.
final class FleetMachinesTab: NSView {
    private lazy var controller = FleetTableController(columns: columns())
    private let statusLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
    private var studios: [String: [String]] = [:]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        let title = PageKit.sectionTitle("Machines")
        title.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        addSubview(title)
        addSubview(statusLabel)
        addSubview(controller.container)
        NSLayoutConstraint.activate([
            title.topAnchor.constraint(equalTo: topAnchor, constant: 24),
            title.leadingAnchor.constraint(equalTo: leadingAnchor, constant: PageKit.pagePadding),
            statusLabel.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 6),
            statusLabel.leadingAnchor.constraint(equalTo: title.leadingAnchor),
            statusLabel.trailingAnchor.constraint(
                lessThanOrEqualTo: trailingAnchor, constant: -PageKit.pagePadding),
            controller.container.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: 10),
            controller.container.leadingAnchor.constraint(
                equalTo: leadingAnchor, constant: PageKit.pagePadding),
            controller.container.trailingAnchor.constraint(
                equalTo: trailingAnchor, constant: -PageKit.pagePadding),
            controller.container.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -24),
            // A low floor keeps the table from collapsing without ever being the
            // reason the window wants to grow.
            controller.container.heightAnchor.constraint(greaterThanOrEqualToConstant: 120),
        ])
        controller.emptyText = "No machines are reporting. Add a location on the Locations tab."
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func columns() -> [FleetColumn] {
        func machine(_ any: Any) -> FleetMachine? { any as? FleetMachine }
        return [
            FleetColumn(
                id: "state", title: "State", width: 110,
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    // **A refused credential is not an offline machine.** The Hub puts
                    // the node in its own `unauthorized` state precisely so a token
                    // problem does not read as a network one; its activity word is
                    // `offline` because nothing can be dispatched, and drawing that
                    // sent the owner to the network for a credential.
                    if row.nodeStateIsUnauthorized { return "credential refused" }
                    return row.state.map { FleetFormat.words($0) }
                        ?? (row.reachable == true ? "up" : "offline")
                },
                sortValue: { .text(machine($0)?.state ?? "") },
                colour: { row in
                    guard let row = machine(row) else { return .labelColor }
                    if row.nodeStateIsUnauthorized { return .systemRed }
                    return row.reachable == false ? .systemOrange : .labelColor
                },
                // **The reason, not just the verdict.** The Hub distinguishes a
                // node it cannot reach from one whose Hub is not running, and the
                // controller writes its own sentence about it; both sit here so
                // "offline" is actionable. The Limitation column keeps its own
                // tooltip, so this one only speaks for state and health.
                tooltip: { row in
                    let limitation = machine(row)?.limitation
                    let reason = machine(row)?.stateReason
                    switch (reason, limitation) {
                    case let (reason?, limitation?): return reason + "\n\n" + limitation
                    case let (reason?, nil): return reason
                    case let (nil, limitation?): return limitation
                    default: return nil
                    }
                }
            ),
            FleetColumn(
                id: "machine", title: "Machine", width: 170,
                text: { machine($0)?.label ?? machine($0)?.machine ?? "—" },
                sortValue: { .text(machine($0)?.label ?? machine($0)?.machine ?? "") }
            ),
            FleetColumn(
                id: "location", title: "Location", width: 150,
                text: { FleetFormat.text(machine($0)?.location) },
                sortValue: { .text(machine($0)?.location ?? "") }
            ),
            FleetColumn(
                id: "node", title: "Node", width: 110,
                // **Which Macs are members, and which one this is.** The node roster
                // was fetched per location and never joined onto its machine (H10/M21);
                // `node_role` is the role the controller recorded when the Mac joined
                // (`agent` today), and `member` marks a Mac whose controller stated no
                // role rather than claiming it is not one. The controller's own Mac has
                // no roster row and reads `—`.
                text: { machine($0)?.nodeRoleText ?? "—" },
                sortValue: { .text(machine($0)?.nodeRole ?? "") },
                colour: { row in
                    guard let row = machine(row) else { return .labelColor }
                    if row.nodeStateIsUnauthorized { return .systemRed }
                    if row.nodeState?.lowercased() == "unreachable" { return .systemOrange }
                    return .labelColor
                },
                tooltip: { row in
                    guard let row = machine(row) else { return nil }
                    guard row.isNode else {
                        return "This is the controller's own Mac: it is not a node of "
                            + "another controller, so it has no roster row."
                    }
                    guard let role = row.nodeRole, !role.isEmpty else {
                        return "Joined this controller; the controller stated no role for it."
                    }
                    return "Joined this controller as ‘\(role)’."
                }
            ),
            FleetColumn(
                id: "nodeSeen", title: "Node last seen", width: 130,
                // **"Is that Mac switched off, or gone?"** (M20.) The Hub stamps
                // `last_seen_at` on every probe that answers; nothing drew it, so a Mac
                // that was off and one that was scrapped looked the same. `—` means the
                // controller has never heard from it, which is not "a long time ago".
                text: { row in
                    guard let row = machine(row), row.isNode else { return "—" }
                    return row.nodeLastSeenText
                },
                sortValue: { .number(machine($0)?.nodeLastSeenAt?.doubleValue ?? 0) },
                tooltip: { row in
                    guard let row = machine(row), row.isNode else { return nil }
                    let when = row.nodeLastSeenText
                    if row.nodeLastSeenAt?.doubleValue == nil {
                        return "The controller has not recorded a probe from this Mac."
                    }
                    return "The controller last heard from this Mac \(when)."
                }
            ),
            FleetColumn(
                id: "jobs", title: "Jobs (done/failed)", width: 140,
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    if row.completed == nil && row.failed == nil { return "—" }
                    return "\(row.completed ?? 0) done, \(row.failed ?? 0) failed"
                },
                sortValue: { .number(Double(machine($0)?.completed ?? 0)) },
                colour: { (machine($0)?.failed ?? 0) > 0 ? .systemOrange : .labelColor }
            ),
            FleetColumn(
                id: "median", title: "Median", width: 100,
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    if let median = row.medianRuntimeS { return FleetFormat.seconds(median) + " med" }
                    if let average = row.averageSeconds { return FleetFormat.seconds(average) + " avg" }
                    return "—"
                },
                sortValue: { .number(machine($0)?.medianRuntimeS ?? machine($0)?.averageSeconds ?? 0) }
            ),
            FleetColumn(
                id: "disk", title: "Disk", width: 130,
                // **Free space and the state beside it, coloured.** The Hub states
                // green/amber/red/stop; a Hub that predates the field draws just the free GB.
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    if let disk = row.disk { return disk.columnText }
                    return FleetFormat.gb(row.diskFreeGb)
                },
                sortValue: { .number(machine($0)?.disk?.freeGb ?? machine($0)?.diskFreeGb ?? -1) },
                colour: { machine($0)?.disk?.colour ?? .labelColor },
                tooltip: { row in
                    guard let disk = machine(row)?.disk else { return nil }
                    var lines: [String] = []
                    if let total = disk.totalGb { lines.append("Volume: \(FleetFormat.gb(total))") }
                    if disk.stateSource == "derived" {
                        lines.append("State worked out here from free space; this Hub does not report one.")
                    }
                    if let top = disk.topText { lines.append("Largest: " + top) }
                    if let trash = disk.trashGb { lines.append("Trash: " + FleetFormat.gb(trash)) }
                    if let swap = disk.swapUsedGb { lines.append("Swap in use: " + FleetFormat.gb(swap)) }
                    if let sweep = disk.lastSweep?.display { lines.append("Last clean-up: " + sweep) }
                    lines.append(contentsOf: disk.warnings)
                    return lines.isEmpty ? nil : lines.joined(separator: "\n")
                }
            ),
            FleetColumn(
                id: "hardware", title: "Hardware", width: 240,
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    var parts: [String] = []
                    if let chip = row.chip, !chip.isEmpty { parts.append(chip) }
                    // **Total and free are two numbers, and the column says which
                    // is which.** It read "Apple M4 · 17.18 GB · 118.11 GB free" —
                    // the disk labelled and the memory not — so a machine with
                    // 7.14 GB actually available looked as though all 17.18 GB
                    // were free. The Hub states both; this draws both.
                    if let memory = row.memoryGb {
                        parts.append(row.availableGb.map {
                            "\(FleetFormat.gb(memory)) (\(FleetFormat.gb($0)) free)"
                        } ?? FleetFormat.gb(memory))
                    }
                    if let free = row.diskFreeGb { parts.append(FleetFormat.gb(free) + " free") }
                    return parts.isEmpty ? "—" : parts.joined(separator: " · ")
                },
                sortValue: { .text(machine($0)?.chip ?? "") }
            ),
            FleetColumn(
                id: "studios", title: "Studios (workers)", width: 220,
                text: { [weak self] row in
                    guard let row = machine(row) else { return "—" }
                    let studios = self?.studios(for: row) ?? []
                    return studios.isEmpty ? "—" : studios.joined(separator: ", ")
                },
                sortValue: { .text(machine($0)?.label ?? "") }
            ),
            FleetColumn(
                id: "workDone", title: "Work done by kind", width: 230,
                text: { row in
                    guard let row = machine(row), let kinds = row.modalities, !kinds.isEmpty else {
                        return "—"
                    }
                    return kinds.sorted { $0.key < $1.key }
                        .map { "\($0.value) \($0.key)" }
                        .joined(separator: ", ")
                },
                sortValue: { row in
                    .number(Double((machine(row)?.modalities ?? [:]).values.reduce(0, +)))
                },
                tooltip: { row in
                    guard let row = machine(row), let kinds = row.modalities, !kinds.isEmpty else {
                        return nil
                    }
                    return "Jobs completed by kind: "
                        + kinds.sorted { $0.key < $1.key }
                            .map { "\($0.value) \($0.key)" }.joined(separator: ", ")
                }
            ),
            FleetColumn(
                id: "limitation", title: "Limitation", width: 260,
                text: { machine($0)?.limitationText ?? "—" },
                sortValue: { .text(machine($0)?.limitationReason ?? machine($0)?.limitation ?? "") },
                tooltip: { row in
                    // The Hub's own sentence stays reachable even when the column
                    // shows its shorter phrase.
                    let machine = machine(row)
                    return machine?.limitation ?? machine?.limitationText
                }
            ),
        ]
    }

    private func studios(for row: FleetMachine) -> [String] {
        if let key = row.machine, let found = studios[key], !found.isEmpty { return found }
        if let key = row.label, let found = studios[key], !found.isEmpty { return found }
        return []
    }

    func update(machines: [FleetMachine], studios: [String: [String]], filter: FleetFilter) {
        self.studios = studios
        let rows = machines.filter { row in
            filter.matchesLocation(row.locationId)
                && filter.matchesSearch([
                    row.label, row.machine, row.location, row.state, row.chip, row.limitation,
                ])
        }
        controller.setRows(rows)
        let summary = rows.count == machines.count
            ? "\(machines.count) machine\(machines.count == 1 ? "" : "s")."
            : "\(rows.count) of \(machines.count) machines match the filter."
        let text = NSMutableAttributedString(
            string: summary,
            attributes: [.font: NSFont.systemFont(ofSize: 11.5), .foregroundColor: NSColor.secondaryLabelColor])
        // The disk banner is about the whole fleet, so it ignores the filter: a Mac that is
        // filling up must not disappear because a search hid it.
        for line in Self.diskBanner(for: machines) {
            text.append(NSAttributedString(
                string: "\n" + line.text,
                attributes: [.font: NSFont.systemFont(ofSize: 11.5, weight: .semibold),
                             .foregroundColor: line.colour]))
        }
        statusLabel.attributedStringValue = text
    }

    /// One line per concern: Macs low on disk (state, free space, biggest consumer), then any
    /// warning a Hub reported (macOS updates / Time Machine). Empty when every Mac is fine.
    static func diskBanner(for machines: [FleetMachine]) -> [(text: String, colour: NSColor)] {
        var lines: [(text: String, colour: NSColor)] = []
        let low = machines.filter { $0.disk?.isLow == true }
            .sorted { ($0.disk?.freeGb ?? 0) < ($1.disk?.freeGb ?? 0) }
        for row in low {
            guard let disk = row.disk else { continue }
            let name = row.label ?? row.machine ?? "a Mac"
            var line = "\(name): disk \((disk.state ?? "").uppercased()), \(FleetFormat.gb(disk.freeGb)) free"
            if let top = disk.top.first(where: { $0.name?.isEmpty == false }), let what = top.name {
                line += " (largest: \(what)\(top.gb.map { " " + FleetFormat.gb($0) } ?? ""))"
            }
            if (disk.state ?? "").lowercased() == "stop" { line += ". It is not being sent work." }
            lines.append((line, disk.colour))
        }
        for row in machines {
            for warning in row.disk?.warnings ?? [] {
                lines.append(("\(row.label ?? row.machine ?? "a Mac"): \(warning)", .systemOrange))
            }
        }
        return lines
    }
}

// MARK: - Models

/// One model, pooled across every location and machine that reports it.
///
/// This is the answer to both of the owner's questions on one row: how many
/// providers (locations) offer the model, and how many workers actually serve it.
struct FleetModelAggregate {
    let model: String
    let label: String
    let studio: String
    let modality: String
    let locations: [String]
    let machinesServing: Int
    let machinesReporting: Int
    let available: Int
    let bestFit: String
    let memoryFloor: Double?
    let cached: Int
    let issues: Int
    let dispatch: String
    let rows: [FleetLocalModel]
}

/// Local models as a master-detail: one row per model above, and on selection one
/// row per (location, machine) below.
///
/// The detail table is the drill-in the owner asked for — "see which workers has
/// said model and is serving" — so it shows the machine's own answer, why it
/// cannot serve when it cannot (`fit.hint`, `catalogError`), and the Hub's exact
/// wording rather than a dot.
final class FleetModelsTab: FleetTabView {
    private let master = FleetTableController(columns: FleetModelsTab.masterColumns(), rowHeight: 22)
    // Built in `init`: the Worker column reads the per-worker catalogue, which is
    // instance state (set by `update`), so the closure captures the tab.
    private var detail: FleetTableController!
    private let masterTitle = PageKit.sectionTitle("Local models")
    private let detailTitle = PageKit.sectionTitle("Machines serving this model")
    private let statusLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)

    private var aggregates: [FleetModelAggregate] = []
    private var selectedModel: String?
    /// "machine|model id" -> the worker that serves it, from `workers[].models`
    /// (Hub 0.6.48). This is a fact the Hub states, not an inference from a studio.
    private var workerLookup: [String: String] = [:]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        detail = FleetTableController(columns: FleetModelsTab.detailColumns(
            worker: { [weak self] row in
                guard let self else { return "—" }
                let key = "\(row.machine ?? "")|\(row.modelId ?? row.model ?? "")"
                return self.workerLookup[key] ?? "—"
            }), rowHeight: 36)
        // Master and detail sit inside this tab's own scroll view, with fixed
        // heights in the document. That keeps the tab's required height out of the
        // window: a plain stack of two fixed tables grew the window at the app's
        // 860x560 minimum, which is the trap `AGENTS.md` records.
        statusLabel.preferredMaxLayoutWidth = 520
        master.container.heightAnchor.constraint(equalToConstant: 220).isActive = true
        detail.container.heightAnchor.constraint(equalToConstant: 220).isActive = true
        page.addArrangedSubview(masterTitle)
        page.addArrangedSubview(statusLabel)
        page.addArrangedSubview(master.container)
        page.addArrangedSubview(detailTitle)
        page.addArrangedSubview(detail.container)
        fullWidth(statusLabel)
        fullWidth(master.container)
        fullWidth(detail.container)
        master.emptyText =
            "No local models are being reported. Enable the ones you want in Models; "
            + "they arrive disabled and become available once switched on."
        detail.emptyText = "Select a model above to see which machines serve it."
        master.onSelection = { [weak self] any in
            guard let self else { return }
            self.selectedModel = (any as? FleetModelAggregate)?.model
            self.showDetail(for: any as? FleetModelAggregate)
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func update(models: [FleetLocalModel], workers: [FleetWorker], filter: FleetFilter) {
        // Which worker serves which model, straight from the worker's own catalogue
        // (0.6.48). A worker that states no models is simply absent, so the column
        // reads "—" rather than a guess.
        workerLookup = [:]
        for worker in workers {
            guard let machine = worker.machine, !machine.isEmpty else { continue }
            let name = worker.id ?? worker.studio ?? "worker"
            for modelId in worker.models where !modelId.isEmpty {
                workerLookup["\(machine)|\(modelId)"] = name
            }
        }
        let filtered = models.filter { row in
            filter.matchesLocation(row.locationId)
                && filter.matchesSearch([
                    row.model, row.label, row.studio, row.modality, row.machine, row.family,
                ])
        }
        aggregates = Self.aggregate(filtered)
        master.setRows(aggregates)
        if let selectedModel, aggregates.contains(where: { $0.model == selectedModel }) {
            master.selectRow(matching: { ($0 as? FleetModelAggregate)?.model == selectedModel })
            showDetail(for: aggregates.first { $0.model == selectedModel })
        } else if let first = aggregates.first {
            // Selecting the first row gives the detail table something to show
            // without a click, which is the whole point of the lower half.
            selectedModel = first.model
            master.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
            showDetail(for: first)
        } else {
            selectedModel = nil
            showDetail(for: nil)
        }
        let locationCount = Set(aggregates.flatMap { $0.locations }).count
        statusLabel.stringValue = models.isEmpty
            ? "No local models are being reported."
            : "\(aggregates.count) model\(aggregates.count == 1 ? "" : "s") across "
              + "\(locationCount) location\(locationCount == 1 ? "" : "s") and "
              + "\(filtered.count) machine row\(filtered.count == 1 ? "" : "s")."
    }

    private func showDetail(for aggregate: FleetModelAggregate?) {
        guard let aggregate else {
            detail.setRows([])
            detail.emptyText = "Select a model above to see which machines serve it."
            detailTitle.stringValue = "Machines serving this model"
            return
        }
        detail.emptyText = "No machine rows were reported for this model."
        detail.setRows(aggregate.rows)
        detailTitle.stringValue = "Machines serving “\(aggregate.label)”"
    }

    private static func aggregate(_ rows: [FleetLocalModel]) -> [FleetModelAggregate] {
        var byModel: [String: [FleetLocalModel]] = [:]
        var order: [String] = []
        for row in rows {
            let key = row.model ?? row.label ?? "—"
            if byModel[key] == nil { order.append(key) }
            byModel[key, default: []].append(row)
        }
        return order.compactMap { key in
            guard let group = byModel[key], let first = group.first else { return nil }
            let locationLabels = Array(Set(group.compactMap { $0.location ?? $0.locationId })).sorted()
            let machineKeys = Set(group.map { "\($0.locationId ?? "")|\($0.machine ?? "")" })
            let available = group.filter { $0.available == true }.count
            let best = group.max { $0.fitScore < $1.fitScore }
            let cache = group.filter { $0.cacheIsCached }.count
            let issues = group.filter { $0.issueText != nil }.count
            let dispatchValues = Array(Set(group.compactMap { value -> String? in
                guard let state = value.dispatchState, !state.isEmpty else { return nil }
                return state
            }))
            let dispatch = dispatchValues.count > 1
                ? "mixed"
                : (dispatchValues.first.map(FleetFormat.words) ?? "—")
            return FleetModelAggregate(
                model: key,
                label: first.label ?? first.model ?? key,
                studio: first.studio ?? "—",
                modality: FleetFormat.words(first.modality),
                locations: locationLabels,
                machinesServing: available,
                machinesReporting: machineKeys.count,
                available: available,
                bestFit: best?.fitLabel ?? "—",
                memoryFloor: group.compactMap { $0.memoryFloor }.min(),
                cached: cache,
                issues: issues,
                dispatch: dispatch,
                rows: group.sorted {
                    ($0.location ?? "").localizedCaseInsensitiveCompare($1.location ?? "")
                        == .orderedAscending
                }
            )
        }
    }

    private static func masterColumns() -> [FleetColumn] {
        func aggregate(_ any: Any) -> FleetModelAggregate? { any as? FleetModelAggregate }
        return [
            FleetColumn(
                id: "model", title: "Model", width: 260,
                text: { aggregate($0)?.label ?? "—" },
                sortValue: { .text(aggregate($0)?.label ?? "") },
                tooltip: { aggregate($0)?.model }
            ),
            FleetColumn(
                id: "studio", title: "Studio", width: 110,
                text: { aggregate($0)?.studio ?? "—" },
                sortValue: { .text(aggregate($0)?.studio ?? "") }
            ),
            FleetColumn(
                id: "modality", title: "Modality", width: 110,
                text: { aggregate($0)?.modality ?? "—" },
                sortValue: { .text(aggregate($0)?.modality ?? "") }
            ),
            FleetColumn(
                id: "locations", title: "Locations", width: 90,
                text: { "\(aggregate($0)?.locations.count ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.locations.count ?? 0)) },
                tooltip: { aggregate($0)?.locations.joined(separator: ", ") }
            ),
            FleetColumn(
                id: "serving", title: "Machines serving", width: 140,
                text: { row in
                    guard let row = aggregate(row) else { return "—" }
                    return "\(row.machinesServing) / \(row.machinesReporting)"
                },
                sortValue: { .number(Double(aggregate($0)?.machinesServing ?? 0)) },
                colour: { (aggregate($0)?.machinesServing ?? 0) > 0 ? .labelColor : .systemOrange }
            ),
            FleetColumn(
                id: "available", title: "Available", width: 90,
                text: { "\(aggregate($0)?.available ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.available ?? 0)) },
                colour: { (aggregate($0)?.available ?? 0) > 0 ? .labelColor : .systemOrange }
            ),
            FleetColumn(
                id: "bestfit", title: "Best fit", width: 120,
                text: { aggregate($0)?.bestFit ?? "—" },
                sortValue: { .text(aggregate($0)?.bestFit ?? "") }
            ),
            FleetColumn(
                id: "memory", title: "Memory floor", width: 110,
                text: { row in
                    guard let value = aggregate(row)?.memoryFloor else { return "—" }
                    return FleetFormat.gb(value)
                },
                sortValue: { .number(aggregate($0)?.memoryFloor ?? 0) }
            ),
            FleetColumn(
                id: "cached", title: "Cached", width: 80,
                text: { "\(aggregate($0)?.cached ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.cached ?? 0)) }
            ),
            FleetColumn(
                id: "dispatch", title: "Dispatch", width: 110,
                text: { aggregate($0)?.dispatch ?? "—" },
                sortValue: { .text(aggregate($0)?.dispatch ?? "") }
            ),
            FleetColumn(
                id: "issues", title: "Issues", width: 80,
                text: { "\(aggregate($0)?.issues ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.issues ?? 0)) },
                colour: { (aggregate($0)?.issues ?? 0) > 0 ? .systemRed : .labelColor }
            ),
        ]
    }

    private static func detailColumns(
        worker: @escaping (FleetLocalModel) -> String
    ) -> [FleetColumn] {
        func row(_ any: Any) -> FleetLocalModel? { any as? FleetLocalModel }
        return [
            FleetColumn(
                id: "dlocation", title: "Location", width: 140,
                text: { FleetFormat.text(row($0)?.location ?? row($0)?.locationId) },
                sortValue: { .text(row($0)?.location ?? "") }
            ),
            FleetColumn(
                id: "dmachine", title: "Machine", width: 170,
                text: { row($0)?.machine ?? "—" },
                sortValue: { .text(row($0)?.machine ?? "") }
            ),
            // **Which worker actually serves it**, from the worker's own catalogue.
            // The machine is where the hardware is; the worker is what would run the
            // job, and on a Mac with four studios those are not the same answer.
            FleetColumn(
                id: "dworker", title: "Worker", width: 170,
                text: { any in row(any).map(worker) ?? "—" },
                sortValue: { any in .text(row(any).map(worker) ?? "") }
            ),
            FleetColumn(
                id: "davailable", title: "Available", width: 80,
                text: { fleetFlag(row($0)?.available) },
                sortValue: { .number((row($0)?.available == true) ? 1 : 0) },
                colour: { row($0)?.available == true ? .labelColor : .systemOrange }
            ),
            FleetColumn(
                id: "druntime", title: "Runtime ready", width: 100,
                text: { fleetFlag(row($0)?.runtimeReady) },
                sortValue: { .number((row($0)?.runtimeReady == true) ? 1 : 0) }
            ),
            FleetColumn(
                id: "dmemory", title: "Memory eligible", width: 110,
                text: { fleetFlag(row($0)?.memoryEligible) },
                sortValue: { .number((row($0)?.memoryEligible == true) ? 1 : 0) }
            ),
            FleetColumn(
                id: "dfit", title: "Fit label", width: 130,
                text: { row($0)?.fitLabel ?? "—" },
                sortValue: { .text(row($0)?.fitLabel ?? "") }
            ),
            FleetColumn(
                id: "dhint", title: "Fit hint", width: 240, sortable: false,
                text: { FleetFormat.text(row($0)?.fit?.hint) },
                cell: { any in
                    fleetWrappingCell(
                        FleetFormat.text(row(any)?.fit?.hint), colour: .secondaryLabelColor)
                }
            ),
            FleetColumn(
                id: "dcached", title: "Cached", width: 90,
                text: { row($0)?.cacheState ?? "—" },
                sortValue: { .text(row($0)?.cacheState ?? "") }
            ),
            FleetColumn(
                id: "dpackage", title: "Package / dispatch", width: 140,
                text: { FleetFormat.words(row($0)?.dispatchState) },
                sortValue: { .text(row($0)?.dispatchState ?? "") }
            ),
            FleetColumn(
                id: "dnote", title: "Note", width: 260, sortable: false,
                text: { FleetFormat.text(row($0)?.issueText ?? row($0)?.fit?.hint) },
                cell: { any in
                    guard let row = row(any) else { return NSView() }
                    if let error = row.issueText {
                        return fleetWrappingCell(error, colour: .systemRed)
                    }
                    return fleetWrappingCell(
                        FleetFormat.text(row.fit?.hint), colour: .secondaryLabelColor)
                }
            ),
        ]
    }
}

// MARK: - Jobs

/// Running batches, the per-machine work history, and the batches that are
/// stalled or failed with the Hub's exact words.
///
/// **This comment said there was no cancel button, and the button and its route
/// both exist below** (C7, the audit's LOW list): `onCancel` is wired, the Cancel
/// column is built, and the shell sends the request. A docstring that contradicts
/// the control beside it is worse than none, because the next reader believes it
/// and removes the button as dead code.
///
/// The distinction it was reaching for is real and is kept here: the *gateway's*
/// per-job cancel is `POST /admin/api/create/status/{id}/cancel`, and a Hub **batch**
/// id is not a gateway job id, so that call would 404. What this page cancels is
/// the Hub's own batch, through the Hub's own route — the Hub owns its queue. What
/// must never happen is a button wired to the wrong one of those, which is the
/// defect `tests/test_ui.py` was written about.
final class FleetJobsTab: FleetTabView {
    var onRefresh: (() -> Void)?
    /// Ask the Hub to stop this batch. The shell owns the request, so the tab
    /// stays a view — and the button is the only place a generation is touched.
    var onCancel: ((FleetJob) -> Void)?
    /// Ask the Hub to re-queue one failed item. Only items in state `error` are
    /// accepted; the Hub refuses the rest with its own sentence.
    var onRetry: ((FleetJob, FleetJobError) -> Void)?

    // Built in `init` rather than inline: the Cancel column's button captures the
    // tab, and `self` is not available until after `super.init`.
    private var running: FleetTableController!
    private let activity = FleetTableController(columns: FleetJobsTab.activityColumns())
    private let runningTitle = PageKit.sectionTitle("Running now")
    private let activityTitle = PageKit.sectionTitle("Work by machine")
    private let problemsTitle = PageKit.sectionTitle("Needs attention")
    private let problems = NSStackView()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        running = FleetTableController(columns: FleetJobsTab.runningColumns(
            cancel: { [weak self] job in self?.onCancel?(job) }))
        running.emptyText = "Nothing is running right now."
        activity.emptyText = "No machine has reported any work."
        running.container.heightAnchor.constraint(equalToConstant: 220).isActive = true
        activity.container.heightAnchor.constraint(equalToConstant: 170).isActive = true

        let refresh = FleetClosureButton(title: "Refresh") { [weak self] in self?.onRefresh?() }
        let header = PageKit.row([runningTitle, NSView(), refresh])
        page.addArrangedSubview(header)
        fullWidth(header)
        page.addArrangedSubview(running.container)
        fullWidth(running.container)
        page.addArrangedSubview(activityTitle)
        page.addArrangedSubview(activity.container)
        fullWidth(activity.container)
        page.addArrangedSubview(problemsTitle)
        problems.orientation = .vertical
        problems.alignment = .leading
        problems.spacing = 8
        page.addArrangedSubview(problems)
        fullWidth(problems)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func update(snapshot: FleetSnapshot, filter: FleetFilter) {
        // A running batch now carries the location's id, so it filters the same way
        // every other tab does — a label is not an identity to match on.
        let runningRows = snapshot.running.filter { job in
            filter.matchesLocation(job.locationId)
                && filter.matchesSearch([job.location, job.modality, job.model, job.label,
                                         job.errorText, job.governorNote, job.batchId])
        }
        let activityRows = snapshot.machines.filter { row in
            filter.matchesLocation(row.locationId)
                && filter.matchesSearch([row.label, row.machine, row.location])
        }
        running.setRows(runningRows)
        activity.setRows(activityRows)
        rebuildProblems(runningRows)
    }

    private func rebuildProblems(_ jobs: [FleetJob]) {
        for view in problems.arrangedSubviews { view.removeFromSuperview() }
        let broken = jobs.filter { job in
            job.stalled == true || (job.errorCount ?? 0) > 0 || (job.uncertain ?? 0) > 0
        }
        guard !broken.isEmpty else {
            problems.addArrangedSubview(PageKit.label(
                "No stalled or failed batches. A batch that stops making progress appears here "
                + "with the Hub's own reason.",
                size: 11.5, colour: .secondaryLabelColor, wrapping: true))
            return
        }
        for job in broken {
            let card = problemCard(job)
            problems.addArrangedSubview(card)
            fullWidth(card)
        }
    }

    private func problemCard(_ job: FleetJob) -> NSView {
        let stalled = job.stalled == true
        let colour: NSColor = stalled ? .systemOrange : .systemRed
        let heading = PageKit.label(
            "\(FleetFormat.text(job.location)) · \(FleetFormat.words(job.modality)) · \(FleetFormat.text(job.model))",
            size: 12.5, weight: .semibold
        )
        let reason: String
        if let note = job.governorNote, !note.trimmingCharacters(in: .whitespaces).isEmpty {
            // The Hub's own note about why it is holding the batch.
            reason = note
        } else if let text = job.errorText {
            reason = text
        } else if let count = job.errorCount, count > 0 {
            reason = "\(count) item\(count == 1 ? "" : "s") failed. Each is listed below with the "
                + "Hub's own reason."
        } else if let uncertain = job.uncertain, uncertain > 0 {
            reason = "\(uncertain) item\(uncertain == 1 ? "" : "s") uncertain — the Hub could not "
                + "confirm the outcome, so it will not dispatch them again on its own."
        } else {
            reason = "The Hub lists this batch as stalled and has not marked it finished."
        }
        let message = PageKit.label(reason, size: 11.5, colour: colour, wrapping: true)
        message.maximumNumberOfLines = 4
        let hint = PageKit.label(
            "Fix: Retry re-queues a failed item — the Hub accepts only an item in an error state, and "
            + "refuses a cancelled batch or an uncertain item (discard that first). Cancel asks the Hub "
            + "to stop the batch; Refresh re-reads its list.",
            size: 11, colour: .secondaryLabelColor, wrapping: true
        )
        hint.maximumNumberOfLines = 3
        let progress = PageKit.label(
            Self.progressText(job) + Self.troubleText(job) + " · started "
            + FleetFormat.relative(job.startedAt),
            size: 11, colour: .tertiaryLabelColor
        )
        let stack = FleetTintStack(views: [heading, message, hint, progress])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        stack.wantsLayer = true
        stack.tint = colour
        stack.translatesAutoresizingMaskIntoConstraints = false

        // **The failed items, named and retryable.** From Hub 0.6.48 the batch row
        // carries `errors[]` — the item index, a code and the Hub's own message —
        // so the card can say which item failed instead of only how many, and can
        // offer the one action that applies to it.
        for item in job.failedItems {
            let index = item.itemIndex.map { "Item \($0)" } ?? "Item"
            let label = PageKit.label("\(index) · \(item.text)", size: 11,
                                      colour: .secondaryLabelColor, wrapping: true)
            label.maximumNumberOfLines = 3
            let retry = FleetClosureButton(title: "Retry") { [weak self] in
                self?.onRetry?(job, item)
            }
            let row = PageKit.row([label, NSView(), retry], spacing: 8)
            stack.addArrangedSubview(row)
            row.widthAnchor.constraint(
                equalTo: stack.widthAnchor, constant: -24
            ).isActive = true
        }
        return stack
    }

    private static func progressText(_ job: FleetJob) -> String {
        "\(job.running ?? 0) running, \(job.queued ?? 0) queued, "
            + "\(job.retrying ?? 0) retrying, \(job.done ?? 0) of \(job.total ?? 0) done"
    }

    /// The Hub's own timings for a batch, appended to the progress line.
    ///
    /// A countdown rather than `relativeTime`: a retry time is in the future, and
    /// "in 0s"/"0s ago" would be a wrong answer to a question nobody asked.
    private static func troubleText(_ job: FleetJob) -> String {
        var parts: [String] = []
        if let silent = job.noProgressS, silent > 0 {
            parts.append("no progress for \(Int(silent))s")
        }
        if let routing = job.routing, !routing.isEmpty {
            parts.append("routing \(routing)")
        }
        if let retry = job.nextRetryAt, retry > 0 {
            let seconds = retry - Date().timeIntervalSince1970
            if seconds > 0 { parts.append("retry in \(Int(seconds))s") }
        }
        return parts.isEmpty ? "" : " · " + parts.joined(separator: " · ")
    }

    /// What is happening to a batch, from the Hub's own facts.
    ///
    /// `error` is a count of failed items rather than a sentence, so a failed
    /// batch is drawn as "2 failed" and the Hub's reason (`governor_note`) or the
    /// per-item text is drawn separately. Reporting the count as if it were the
    /// word was how a failed batch read as "0".
    static func stateText(_ job: FleetJob) -> String {
        if job.cancelled == true { return "cancelled" }
        if (job.cancelRequested ?? 0) > 0 { return "stop requested" }
        if let text = job.errorText { return text }
        if let count = job.errorCount, count > 0 { return "\(count) failed" }
        if let uncertain = job.uncertain, uncertain > 0 { return "\(uncertain) uncertain" }
        return job.stalled == true ? "stalled" : "in flight"
    }

    private static func runningColumns(cancel: @escaping (FleetJob) -> Void) -> [FleetColumn] {
        func job(_ any: Any) -> FleetJob? { any as? FleetJob }
        return [
            FleetColumn(
                id: "jlocation", title: "Location", width: 160,
                text: { FleetFormat.text(job($0)?.location) },
                sortValue: { .text(job($0)?.location ?? "") }
            ),
            FleetColumn(
                id: "jmodality", title: "Modality", width: 110,
                text: { FleetFormat.words(job($0)?.modality) },
                sortValue: { .text(job($0)?.modality ?? "") }
            ),
            FleetColumn(
                id: "jmodel", title: "Model", width: 240,
                text: { FleetFormat.text(job($0)?.model ?? job($0)?.label) },
                sortValue: { .text(job($0)?.model ?? "") },
                tooltip: { job($0)?.label }
            ),
            FleetColumn(
                id: "jprogress", title: "Progress", width: 260,
                text: { row in
                    guard let job = job(row) else { return "—" }
                    return FleetJobsTab.progressText(job)
                },
                sortValue: { .number(Double(job($0)?.running ?? 0)) }
            ),
            FleetColumn(
                id: "jstate", title: "State", width: 220,
                text: { row in
                    guard let job = job(row) else { return "—" }
                    return FleetJobsTab.stateText(job)
                },
                sortValue: { .text(job($0).map(FleetJobsTab.stateText) ?? "") },
                colour: { row in
                    guard let job = job(row) else { return .labelColor }
                    if job.cancelled == true { return .secondaryLabelColor }
                    if (job.errorCount ?? 0) > 0 || (job.uncertain ?? 0) > 0 {
                        return .systemRed
                    }
                    if job.stalled == true || (job.cancelRequested ?? 0) > 0 {
                        return .systemOrange
                    }
                    return .labelColor
                },
                tooltip: { job($0)?.governorNote }
            ),
            FleetColumn(
                id: "jstarted", title: "Started", width: 100,
                text: { FleetFormat.relative(job($0)?.startedAt) },
                sortValue: { .number(job($0)?.startedAt ?? 0) }
            ),
            // **The cancel is the one Fleet action that reaches upstream.** It is
            // offered only while the Hub still holds the batch and has not already
            // been asked to stop it; the answer comes back as a request, not a
            // promise, and the banner says which.
            FleetColumn(
                id: "jcancel", title: "", width: 96, sortable: false,
                text: { _ in "" },
                cell: { any in
                    let cell = NSTableCellView()
                    guard let row = job(any) else { return cell }
                    let button = FleetClosureButton(title: "Cancel") { cancel(row) }
                    button.isEnabled = row.canCancel
                    button.toolTip = row.canCancel
                        ? "Ask the Hub at this location to stop the batch."
                        : "This batch has already finished, or a stop was already asked for."
                    button.translatesAutoresizingMaskIntoConstraints = false
                    cell.addSubview(button)
                    NSLayoutConstraint.activate([
                        button.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 4),
                        button.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                    ])
                    return cell
                }
            ),
        ]
    }

    private static func activityColumns() -> [FleetColumn] {
        func machine(_ any: Any) -> FleetMachine? { any as? FleetMachine }
        return [
            FleetColumn(
                id: "amachine", title: "Machine", width: 180,
                text: { machine($0)?.label ?? machine($0)?.machine ?? "—" },
                sortValue: { .text(machine($0)?.label ?? "") }
            ),
            FleetColumn(
                id: "alocation", title: "Location", width: 160,
                text: { FleetFormat.text(machine($0)?.location) },
                sortValue: { .text(machine($0)?.location ?? "") }
            ),
            FleetColumn(
                id: "adone", title: "Done", width: 80,
                text: { FleetFormat.count(machine($0)?.completed) },
                sortValue: { .number(Double(machine($0)?.completed ?? 0)) }
            ),
            FleetColumn(
                id: "afailed", title: "Failed", width: 80,
                text: { FleetFormat.count(machine($0)?.failed) },
                sortValue: { .number(Double(machine($0)?.failed ?? 0)) },
                colour: { (machine($0)?.failed ?? 0) > 0 ? .systemOrange : .labelColor }
            ),
            FleetColumn(
                id: "amedian", title: "Median", width: 100,
                text: { row in
                    guard let machine = machine(row) else { return "—" }
                    if let median = machine.medianRuntimeS { return FleetFormat.seconds(median) + " med" }
                    if let average = machine.averageSeconds { return FleetFormat.seconds(average) + " avg" }
                    return "—"
                },
                sortValue: { .number(machine($0)?.medianRuntimeS ?? machine($0)?.averageSeconds ?? 0) }
            ),
            FleetColumn(
                id: "alimitation", title: "Limitation", width: 240,
                text: { FleetFormat.text(machine($0)?.limitation) },
                sortValue: { .text(machine($0)?.limitation ?? "") },
                tooltip: { machine($0)?.limitation }
            ),
        ]
    }
}
