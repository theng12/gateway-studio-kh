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
    case groups, locations, machines, models, jobs, story

    var title: String {
        switch self {
        case .groups: return "Groups"
        case .locations: return "Settings / Locations"
        case .machines: return "Machines"
        case .models: return "Local Models"
        case .jobs: return "Jobs / Generations"
        case .story: return "Story Studio"
        }
    }

    /// The short label the segmented control can fit.
    var shortTitle: String {
        switch self {
        case .groups: return "Groups"
        case .locations: return "Locations"
        case .machines: return "Machines"
        case .models: return "Models"
        case .jobs: return "Jobs"
        case .story: return "Story Studio"
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
    /// A weight: the spare width of the pane is shared out by it, and a narrow pane shrinks the column toward
    /// `minWidth`, so the table fits instead of scrolling sideways (`PageKit.fitColumns`). 0 is a fixed column.
    var flex: CGFloat = 1
    /// The pane width below which this (secondary) column is dropped, so the primary ones stay readable.
    var hideBelow: CGFloat = 0
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
    var tint: NSColor = Health.warn.tint
    var tintAlpha: CGFloat = 0.10
    var borderAlpha: CGFloat = 0.35

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.backgroundColor = tint.withAlphaComponent(tintAlpha).cgColor
        layer?.borderColor = tint.withAlphaComponent(borderAlpha).cgColor
        layer?.borderWidth = borderAlpha > 0 ? 1 : 0
        layer?.cornerRadius = DS.Radius.card
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
    let container = TableCard()

    private let columns: [FleetColumn]
    private let emptyLabel = NSTextField(wrappingLabelWithString: "")
    private var entries: [(order: Int, row: Any)] = []
    private(set) var rows: [Any] = []
    private var sortID: String?
    private var sortAscending = true

    /// Called when the selected row changes; `nil` when nothing is selected.
    var onSelection: ((Any?) -> Void)?
    var emptyText = "Nothing to show."

    init(columns: [FleetColumn], rowHeight: CGFloat = DS.Size.row) {
        self.columns = columns
        super.init()
        build(rowHeight: rowHeight)
    }

    private func build(rowHeight: CGFloat) {
        table.dataSource = self
        table.delegate = self
        table.rowHeight = rowHeight
        // Separators only (drawn by `SeparatorRowView`), no zebra: the card says where the table ends.
        table.usesAlternatingRowBackgroundColors = false
        table.backgroundColor = .clear
        // The system's 17 pt between columns would eat a 1100 pt window's spare width; cells inset themselves.
        table.intercellSpacing = NSSize(width: DS.Space.xs, height: 0)
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
        scrollView.translatesAutoresizingMaskIntoConstraints = false

        container.translatesAutoresizingMaskIntoConstraints = false
        container.hold(table: scrollView)

        emptyLabel.font = TextRole.secondary.font
        emptyLabel.textColor = .secondaryLabelColor
        emptyLabel.alignment = .center
        emptyLabel.maximumNumberOfLines = 3
        emptyLabel.lineBreakMode = .byWordWrapping
        emptyLabel.preferredMaxLayoutWidth = 380
        emptyLabel.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(emptyLabel)

        NSLayoutConstraint.activate([
            emptyLabel.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            emptyLabel.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            emptyLabel.leadingAnchor.constraint(
                greaterThanOrEqualTo: container.leadingAnchor, constant: 16),
            emptyLabel.trailingAnchor.constraint(
                lessThanOrEqualTo: container.trailingAnchor, constant: -16),
        ])
        container.onLayout = { [weak self] in self?.fitColumns() }
        updateEmptyState()
    }

    private func fitColumns() {
        var flex: [String: (weight: CGFloat, base: CGFloat)] = [:]
        var hiding: [String: CGFloat] = [:]
        for spec in columns {
            if spec.flex > 0 { flex[spec.id] = (spec.flex, spec.width) }
            if spec.hideBelow > 0 { hiding[spec.id] = spec.hideBelow }
        }
        PageKit.fitColumns(of: table, in: scrollView, flex: flex, hiding: hiding)
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
        if let cell = column.cell {
            let view = cell(item)
            if view.toolTip == nil, let tooltip = column.tooltip?(item), !tooltip.isEmpty { view.toolTip = tooltip }
            return view
        }

        let cell = NSTableCellView()
        let label = NSTextField(labelWithString: column.text(item))
        label.font = TextRole.secondary.font
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
            label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
            label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -DS.Space.xs),
            label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        ])
        return cell
    }

    func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? {
        SeparatorRowView()
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
    stack.spacing = DS.Space.xs
    stack.translatesAutoresizingMaskIntoConstraints = false
    cell.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
        stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
        stack.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -DS.Space.xs),
    ])
    return cell
}

/// A cell whose text wraps to a couple of lines — for a fit hint or an error.
/// The table's row height is raised to match, so the text is readable rather than
/// elided at one line.
func fleetWrappingCell(_ text: String, colour: NSColor = .labelColor, lines: Int = 2) -> NSView {
    let cell = NSTableCellView()
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = TextRole.caption.font
    label.textColor = colour
    label.isSelectable = true
    label.maximumNumberOfLines = lines
    label.lineBreakMode = .byWordWrapping
    label.preferredMaxLayoutWidth = 0
    // The full sentence stays one hover away when the cell has to cut it.
    cell.toolTip = text
    label.translatesAutoresizingMaskIntoConstraints = false
    cell.addSubview(label)
    cell.textField = label
    NSLayoutConstraint.activate([
        label.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
        label.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -DS.Space.xs),
        label.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
    ])
    return cell
}

/// Pin `content` inside a table cell: leading edge, vertically centred, never wider than the cell.
private func fleetPinned(_ content: NSView) -> NSTableCellView {
    let cell = NSTableCellView()
    content.translatesAutoresizingMaskIntoConstraints = false
    cell.addSubview(content)
    NSLayoutConstraint.activate([
        content.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
        content.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -DS.Space.xs),
        content.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
    ])
    return cell
}

private func fleetColumnStack(_ views: [NSView]) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = 1
    return stack
}

/// A cell with a title over a smaller grey caption: the way a table keeps a second fact without a second
/// column (a machine's location and chip, a model's studio).
func fleetTwoLineCell(_ title: String, _ caption: String?, titleColour: NSColor = .labelColor,
                      tooltip: String? = nil) -> NSView {
    let first = PageKit.label(title, .secondary, colour: titleColour)
    first.lineBreakMode = .byTruncatingTail
    var views: [NSView] = [first]
    if let caption, !caption.isEmpty {
        let second = PageKit.label(caption, .caption, colour: .secondaryLabelColor)
        second.lineBreakMode = .byTruncatingTail
        views.append(second)
    }
    let cell = fleetPinned(fleetColumnStack(views))
    cell.toolTip = tooltip
    return cell
}

/// A status badge, with an optional caption under it.
func fleetBadgeCell(_ text: String, _ health: Health, caption: String? = nil, tooltip: String? = nil) -> NSView {
    var views: [NSView] = [Badge(text, health)]
    if let caption, !caption.isEmpty { views.append(PageKit.label(caption, .caption, colour: .secondaryLabelColor)) }
    let cell = fleetPinned(fleetColumnStack(views))
    cell.toolTip = tooltip
    return cell
}

/// "21.0 GB free" over a usage bar in the tier colour: how much is left, and how worried to be.
func fleetMeterCell(_ text: String, fraction: Double?, health: Health, tooltip: String? = nil) -> NSView {
    guard let fraction else { return fleetTwoLineCell(text, nil, tooltip: tooltip) }
    let value = PageKit.label(text, .secondary, colour: health == .ok || health == .off ? .labelColor : health.text)
    let bar = Meter(fraction: fraction, health: health)
    bar.widthAnchor.constraint(equalToConstant: 72).isActive = true
    let stack = fleetColumnStack([value, bar])
    stack.spacing = DS.Space.xs
    let cell = fleetPinned(stack)
    cell.toolTip = tooltip
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

    /// A section title with the page's 16 pt to its content instead of the 24 pt between sections.
    func addSection(_ title: String) {
        let label = PageKit.sectionTitle(title)
        page.addArrangedSubview(label)
        page.setCustomSpacing(DS.Space.m, after: label)
    }

    /// Pin a row to the usable width inside the page's padding.
    func fullWidth(_ view: NSView) {
        view.widthAnchor.constraint(
            equalTo: page.widthAnchor, constant: -PageKit.pagePadding * 2
        ).isActive = true
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
    var onPause: ((FleetLocation) -> Void)?
    /// "Network…": name the network this location's Macs are on (LAN model sharing).
    var onNetwork: ((FleetLocation) -> Void)?
    var onRefresh: (() -> Void)?
    var onRetryFailed: (() -> Void)?
    var onAddLocation: ((String, String, String) -> Void)?
    /// Asks the shell to run the publish flow — the open panel, the version/build
    /// alert and the network all belong there, not in a tab that a layout test
    /// builds without a gateway.
    var onPublishRelease: (() -> Void)?
    /// Stop or resume the paced rollout (`/admin/api/release/rollout/stop|resume`).
    var onStopRollout: (() -> Void)?
    var onResumeRollout: (() -> Void)?

    private lazy var controller = FleetTableController(columns: columns(), rowHeight: 44)
    /// Location id -> "1 Mac on A" under its Network (`update(locations:machines:)`).
    private var networkNotes: [String: String] = [:]
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private lazy var retryButton: FleetClosureButton = {
        let button = FleetClosureButton(title: "Retry Failed") { [weak self] in
            self?.onRetryFailed?()
        }
        button.isHidden = true
        return button
    }()
    /// The per-controller release report, rebuilt on every fleet read.
    private let releaseStack = NSStackView()
    private let releaseStatus = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    /// This gateway's own publisher fingerprint, so a controller that trusts it
    /// reads "this gateway" rather than an opaque hex string.
    private var identityFingerprint: String?
    private lazy var publishButton: PrimaryButton = {
        PrimaryButton(title: "Publish a Studio Hub Release…", regular: true) { [weak self] in
            self?.onPublishRelease?()
        }
    }()
    private let labelField = NSTextField(string: "")
    private let urlField = NSTextField(string: "")
    private let tokenField = SecretField(placeholder: "the controller's Fleet token", width: 260)
    private let addStatus = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private var addButton: PrimaryButton!
    private var tableHeight: NSLayoutConstraint!
    private let addSpinner: NSProgressIndicator = {
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        return spinner
    }()

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
        tableHeight = controller.container.heightAnchor.constraint(equalToConstant: 240)
        tableHeight.isActive = true
        page.addArrangedSubview(controller.container)
        fullWidth(controller.container)

        // ---- Release
        //
        // What the controllers *hold*, beside what the last push did with it. The
        // two are different facts: a controller accepting a push is not the same as
        // a controller that stored it, and only the controller's own read-back
        // answers the second. A refusal is quoted word for word.
        addSection("Release")
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
        releaseStack.spacing = DS.Space.m
        let releaseCard = CardView()
        releaseCard.hold(releaseStack, inset: NSEdgeInsets(top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l))
        page.addArrangedSubview(releaseCard)
        fullWidth(releaseCard)

        addSection("Add a location")
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
        addButton = PrimaryButton(title: "Add Location", regular: true) { [weak self] in
            guard let self else { return }
            self.onAddLocation?(
                self.labelField.stringValue.trimmingCharacters(in: .whitespaces),
                self.urlField.stringValue.trimmingCharacters(in: .whitespaces),
                self.tokenField.stringValue.trimmingCharacters(in: .whitespaces)
            )
        }

        let form = NSStackView(views: [
            PageKit.formRow("Name", labelField),
            PageKit.formRow("Address", urlField),
            PageKit.formRow(
                "Fleet token",
                [tokenField, PageKit.help(
                    "The token this gateway needs to send work to that Mac and collect the results."
                )]
            ),
            PageKit.row([addButton, addSpinner, addStatus]),
        ])
        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = DS.Space.l
        let formCard = CardView()
        formCard.hold(form, inset: NSEdgeInsets(top: DS.Space.l, left: DS.Space.l, bottom: DS.Space.l, right: DS.Space.l))
        page.addArrangedSubview(formCard)
        fullWidth(formCard)
        // The status line wraps rather than pinning to the page width: it lives in
        // a form row beside a caption and its own width belongs to that column.
        addStatus.preferredMaxLayoutWidth = 520
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// Seven columns that fit a 1100 pt window: the role and machine count are the location's caption, the
    /// address and the controller's own sentence are the status, and the row actions are a "Test" button and
    /// a "…" menu.
    private func columns() -> [FleetColumn] {
        func location(_ any: Any) -> FleetLocation? { any as? FleetLocation }
        func models(_ row: FleetLocation) -> (offered: Int?, discovered: Int?) {
            // The explicit counts when the gateway states them; otherwise derived from the (model, machine)
            // rows it does send, so a location with models never reads as "—".
            let offered = row.modelsPublishable ?? row.modelsEnabled
                ?? (row.localModels.isEmpty ? nil : row.derivedModelsOffered)
            let discovered = row.modelsDiscovered
                ?? (row.localModels.isEmpty ? nil : row.derivedModelsDiscovered)
            return (offered, discovered)
        }
        return [
            FleetColumn(
                id: "llocation", title: "Location", width: 150, minWidth: 120, flex: 3,
                text: { location($0)?.label ?? "—" },
                sortValue: { .text(location($0)?.label ?? "") },
                cell: { any in
                    guard let row = location(any) else { return NSView() }
                    let count = row.machineSummary?.total ?? row.machines.count
                    let machines = row.reachable == true ? "\(count) machine\(count == 1 ? "" : "s")" : nil
                    let role = row.role.map { FleetFormat.words($0) }
                    let caption = row.pending == true ? "Just added"
                        : row.slow == true ? "Slow to answer"
                        : [role, machines].compactMap { $0 }.joined(separator: " · ")
                    return fleetTwoLineCell(row.label ?? "—", caption)
                }
            ),
            // The network its Macs are on (LAN model sharing, 2026-10-05); a Mac can override it on Machines.
            // A Mac on another network than its location's says so under it: "C" over "1 Mac on A".
            FleetColumn(
                id: "lnetwork", title: "Network", width: 84, minWidth: 84, flex: 0,
                text: { location($0)?.network ?? "—" },
                sortValue: { .text(location($0)?.network ?? "") },
                tooltip: { _ in "Macs on the same network copy models from each other. Set it with … → Network…" },
                cell: { [weak self] any in
                    guard let row = location(any) else { return NSView() }
                    let note = self?.networkNotes[row.id ?? ""]
                    return fleetTwoLineCell(row.network ?? "—", note,
                                            titleColour: row.network == nil ? .secondaryLabelColor : .labelColor,
                                            tooltip: (note.map { "\($0). " } ?? "")
                                                + "Macs on the same network copy models from each other. "
                                                + "Set it with … → Network…")
                }
            ),
            FleetColumn(
                id: "lhub", title: "Hub", width: 120, minWidth: 100, sortable: false,
                // **The version, against the newest release this Gateway published** — not against the
                // release this build was compiled for, which called every controller "newer" the day after
                // a release. `versionStatus` is the controller's own Mac (fleet.stamp_controller_versions).
                text: { row in
                    guard let row = location(row) else { return "—" }
                    let stated = FleetFormat.text(row.version)
                    switch row.versionStatus {
                    case "latest": return "\(stated) · Latest"
                    case "updating": return "\(stated) · Updating…"
                    case "behind": return "\(stated) · Behind"
                    default: return stated
                    }
                },
                colour: { row in
                    switch location(row)?.versionStatus {
                    case "behind": return Health.warn.text
                    case "updating": return Health(state: "updating").text
                    default: return .labelColor
                    }
                },
                tooltip: { row in
                    switch location(row)?.versionStatus {
                    case "latest": return "The newest Studio Hub this Gateway has published."
                    case "updating": return "The controller holds the newest release and installs it as soon as it is idle."
                    case "behind": return "Behind the newest release, and not updating by itself. Open Studio Hub on "
                        + "that Mac, or publish the release to this group."
                    default: return "Nothing has been published from this Gateway to compare with."
                    }
                }
            ),
            FleetColumn(
                id: "lmembers", title: "Members", width: 124, minWidth: 120,
                // **Who has joined this controller** (audit H10). The fraction is the controller's own count
                // of nodes that answered out of the roster it holds; a refused credential is spelled out
                // rather than folded into "offline", because it is the one state with a fix a person has to
                // perform.
                text: { row in location(row)?.membersText ?? "—" },
                sortValue: { row in
                    guard let count = location(row)?.memberCount else { return .none }
                    return .number(Double(count.reachable))
                },
                colour: { row in
                    guard let row = location(row) else { return .labelColor }
                    if (row.nodeSummary?.unauthorized ?? 0) > 0 { return Health.bad.text }
                    if let count = row.memberCount, count.total > 0, count.reachable == 0 {
                        return Health.warn.text
                    }
                    return .labelColor
                },
                tooltip: { row in location(row)?.membersTooltip }
            ),
            FleetColumn(
                id: "lmodels", title: "Models", width: 70, minWidth: 56, hideBelow: 720,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    switch models(row) {
                    case let (offered?, discovered?): return "\(offered) / \(discovered)"
                    case let (offered?, nil): return "\(offered) / —"
                    case let (nil, discovered?): return "— / \(discovered)"
                    default: return "—"
                    }
                },
                sortValue: { row in .number(Double(location(row).flatMap { models($0).offered } ?? 0)) },
                tooltip: { _ in "Models offered, of the models discovered." }
            ),
            FleetColumn(
                id: "lrelease", title: "Release", width: 112, minWidth: 90, hideBelow: 720,
                text: { row in location(row)?.release?.versionBuild ?? "—" },
                sortValue: { row in
                    guard let release = location(row)?.release else { return .none }
                    return .text(release.versionBuild)
                },
                tooltip: { row in
                    guard let release = location(row)?.release else { return nil }
                    var parts: [String] = []
                    if let filename = release.filename, !filename.isEmpty { parts.append(filename) }
                    if let sha = release.sha256, !sha.isEmpty { parts.append("sha256 \(sha)") }
                    if let key = release.publisherKeyId, !key.isEmpty { parts.append("signed by \(key)") }
                    if let key = release.controllerKeyId, !key.isEmpty { parts.append("countersigned by \(key)") }
                    return parts.isEmpty ? nil : parts.joined(separator: "\n")
                }
            ),
            FleetColumn(
                id: "lstatus", title: "Status", width: 220, minWidth: 120, flex: 4, sortable: false,
                text: { row in
                    guard let row = location(row) else { return "—" }
                    let address = FleetFormat.text(row.url)
                    // A pause outranks the raw probe error: it says what the gateway did about it (routing
                    // skips this location; nothing was removed) and how to undo it.
                    if row.pausedAt != nil {
                        let reason = row.pauseReason ?? "Paused: unreachable."
                        return address.isEmpty ? reason : "\(address)\n\(reason)"
                    }
                    if row.reachable == true { return address }
                    if row.pending == true { return "\(address)\n\(Self.pendingNote)" }
                    // **The address is what you need most when it is not answering**, so it never replaces
                    // the reason: "Nothing is listening on that address" must say which address.
                    let reason = row.error ?? "Not reachable"
                    return address.isEmpty ? reason : "\(address)\n\(reason)"
                },
                colour: { row in
                    location(row)?.reachable == true || location(row)?.pending == true
                        ? .secondaryLabelColor : Health.warn.text
                },
                // The whole sentence, for a cell that wraps it at three lines.
                tooltip: { row in location(row)?.error },
                cell: { any in
                    guard let row = location(any) else { return NSView() }
                    let text: String
                    let address = FleetFormat.text(row.url)
                    if row.pausedAt != nil {
                        let reason = row.pauseReason ?? "Paused: unreachable."
                        text = address.isEmpty ? reason : "\(address)\n\(reason)"
                    } else if row.reachable == true {
                        text = address
                    } else if row.pending == true {
                        return fleetWrappingCell("\(address)\n\(Self.pendingNote)", colour: .secondaryLabelColor, lines: 3)
                    } else {
                        let reason = row.error ?? "Not reachable"
                        text = address.isEmpty ? reason : "\(address)\n\(reason)"
                    }
                    return fleetWrappingCell(text, colour: row.reachable == true ? .secondaryLabelColor : Health.warn.text, lines: 3)
                }
            ),
            FleetColumn(
                id: "lactions", title: "", width: 112, flex: 0, sortable: false,
                text: { _ in "" },
                cell: { [weak self] any in
                    guard let self, let row = location(any) else { return NSView() }
                    return self.actions(for: row)
                }
            ),
        ]
    }

    /// A just-added location, before the gateway's first read of it lands.
    static let pendingNote = "Just added — reading its Macs…"

    /// "Test" and, for a paused controller, "Resume" are buttons; Rename, Credentials, Change Address and
    /// Remove are in the "…" menu beside them. This Mac gets Rename only: it has no address to test and
    /// nothing to remove.
    private func actions(for row: FleetLocation) -> NSView {
        var views: [NSView] = []
        if row.local != true {
            views.append(FleetClosureButton(title: "Test") { [weak self] in self?.onTest?(row) })
        }
        if row.pausedAt != nil {
            views.append(FleetClosureButton(title: "Resume") { [weak self] in self?.onResume?(row) })
        }
        let more = FleetMenuButton { [weak self] in
            let menu = NSMenu()
            menu.addItem(ClosureMenuItem("Rename…") { self?.onRename?(row) })
            menu.addItem(ClosureMenuItem("Network…") { self?.onNetwork?(row) })
            if !row.isPausedByOwner { menu.addItem(ClosureMenuItem("Pause") { self?.onPause?(row) }) }
            if row.local != true {
                // One credentials action, not two: the overflow of five buttons was once left out of the
                // hierarchy entirely, so Remove was unreachable while its endpoint worked.
                menu.addItem(ClosureMenuItem("Credentials…") { self?.onCredentials?(row) })
                // Handover, not Remove + Add: a second row orphans the first one's credential and every
                // per-model enable decision (see the gateway's `replace_address`).
                menu.addItem(ClosureMenuItem("Change Address…") { self?.onChangeAddress?(row) })
                menu.addItem(.separator())
                menu.addItem(ClosureMenuItem("Remove…") { self?.onRemove?(row) })
            }
            return menu
        }
        more.toolTip = "More actions for this location"
        views.append(more)
        let cell = NSTableCellView()
        let stack = NSStackView(views: views)
        stack.spacing = DS.Space.xs
        stack.alignment = .centerY
        stack.translatesAutoresizingMaskIntoConstraints = false
        cell.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
            stack.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor, constant: -DS.Space.xs),
        ])
        return cell
    }

    func update(locations: [FleetLocation], machines: [FleetMachine] = []) {
        // "1 Mac on A" / "2 Macs on A, 1 on D": the Macs whose own network differs from their location's.
        var others: [String: [String: Int]] = [:]
        for mac in machines {
            guard let id = mac.locationId, let own = mac.networkOverride,
                  own != locations.first(where: { $0.id == id })?.network else { continue }
            others[id, default: [:]][own, default: 0] += 1
        }
        networkNotes = others.mapValues { counts in
            counts.sorted { $0.key < $1.key }.enumerated().map { index, entry in
                index == 0 ? "\(entry.value) Mac\(entry.value == 1 ? "" : "s") on \(entry.key)" : "\(entry.value) on \(entry.key)"
            }.joined(separator: ", ")
        }
        controller.setRows(locations)
        // **Every location in view, up to twelve.** A fixed 240 pt showed five rows, so with the owner's
        // eight the location just added — appended last — was below the fold and read as missing.
        tableHeight.constant = 40 + 44 * CGFloat(min(max(locations.count, 4), 12))
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
            addReleaseLine(unavailable, colour: Health.warn.text)
            return
        }
        let report = state?.lastPush
        let reportKnown = state != nil
        identityFingerprint = state?.identity?.fingerprint
        if let error = state?.error, !error.isEmpty {
            addReleaseLine(error, colour: Health.warn.text)
        }
        if let rollout = state?.rollout {
            addRollout(rollout)
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
                "This gateway did not report a release status.", colour: Health.warn.text
            )
        }
        // The local Mac is a location but not a controller: releases are pushed to
        // configured controllers only, so it has no row here.
        let controllers = locations.filter { $0.local != true }
        guard !controllers.isEmpty else {
            addReleaseLine("No controllers are configured yet.", colour: .secondaryLabelColor)
            return
        }
        // **One line when every controller says the same thing about its trusted publisher** ("the Hub did not
        // report one", three times over, said nothing the first did not).
        let trusts = Set(controllers.map { trustedPublisherText(for: $0) })
        let sharedTrust = trusts.count == 1 && controllers.count > 1 ? controllers.first : nil
        if let sharedTrust {
            addReleaseLine(trustedPublisherText(for: sharedTrust), colour: trustedPublisherColour(for: sharedTrust))
        }
        for controller in controllers {
            let outcome = report?.outcomes[controller.id ?? ""]
            let row = releaseRow(for: controller, outcome: outcome, reportKnown: reportKnown,
                                 showTrust: sharedTrust == nil)
            releaseStack.addArrangedSubview(row)
            row.widthAnchor.constraint(equalTo: releaseStack.widthAnchor).isActive = true
        }
    }

    /// Disable the publish button while a push is in flight and say what it is
    /// doing, because a 1.1 GB artifact in one base64 body is a long wait.
    /// One rollout at a time: the gateway refuses a second, so the button says so before it is pressed.
    func setRolloutRunning(_ running: Bool) {
        publishButton.isEnabled = !running
        publishButton.toolTip = running ? "A rollout is running. Stop it first, or wait for it to finish." : nil
    }

    func setPublishing(_ publishing: Bool, message: String) {
        publishButton.isEnabled = !publishing
        releaseStatus.isHidden = !publishing
        releaseStatus.stringValue = publishing ? message : ""
    }

    /// The paced rollout: its one line with Stop or Resume beside it, why it stopped, then each location
    /// in order — its state and, once the release reached it, how many of its Macs run the new version
    /// (they update themselves, one at a time).
    private func addRollout(_ rollout: FleetRollout) {
        let version = rollout.version ?? "the release"
        let headline = PageKit.label(
            "Rollout of \(version): \(rollout.summary ?? rollout.state ?? "")", .rowTitle, wrapping: true
        )
        headline.preferredMaxLayoutWidth = 520
        var views: [NSView] = [headline, NSView()]
        if rollout.isRunning {
            views.append(FleetClosureButton(title: "Stop Rollout") { [weak self] in self?.onStopRollout?() })
        } else if rollout.canResume {
            views.append(FleetClosureButton(title: "Resume Rollout") { [weak self] in self?.onResumeRollout?() })
        }
        let head = PageKit.row(views)
        releaseStack.addArrangedSubview(head)
        head.widthAnchor.constraint(equalTo: releaseStack.widthAnchor).isActive = true
        if let reason = rollout.reason, !reason.isEmpty, !rollout.isRunning {
            addReleaseLine("Stopped: \(reason).", colour: (rollout.state == "failed" ? Health.bad : Health.warn).text)
        }
        for (_, step) in rollout.orderedSteps {
            let (text, health) = step.panelText(version: version)
            addReleaseLine("\(step.label ?? "—") — \(text)", colour: health.map(\.text) ?? .secondaryLabelColor)
        }
    }

    private func addReleaseLine(_ text: String, colour: NSColor) {
        let label = PageKit.label(text, .secondary, colour: colour, wrapping: true)
        label.preferredMaxLayoutWidth = 620
        releaseStack.addArrangedSubview(label)
        label.widthAnchor.constraint(equalTo: releaseStack.widthAnchor).isActive = true
    }

    private func releaseRow(
        for location: FleetLocation, outcome: FleetPushOutcome?, reportKnown: Bool, showTrust: Bool = true
    ) -> NSView {
        let name = PageKit.label(
            location.label ?? location.id ?? "Controller", .rowTitle
        )
        name.lineBreakMode = .byTruncatingTail
        name.setContentHuggingPriority(.required, for: .horizontal)
        name.widthAnchor.constraint(equalToConstant: 170).isActive = true

        let heldText = location.release.map { "held \($0.versionBuild)" } ?? "held none"
        let held = PageKit.label(
            heldText, .secondary,
            colour: location.release == nil ? .secondaryLabelColor : .labelColor
        )
        held.lineBreakMode = .byTruncatingTail
        let top = PageKit.row([name, held, NSView()])

        let outcomeLabel = PageKit.label(
            releaseOutcomeText(outcome, for: location, reportKnown: reportKnown),
            .secondary, colour: releaseOutcomeColour(outcome, reportKnown: reportKnown),
            wrapping: true
        )
        outcomeLabel.preferredMaxLayoutWidth = 620

        // What the controller trusts decides whether a push will be accepted at
        // all, so it is shown beside what it holds — before the push, not only
        // after a refusal.
        let trustLabel = PageKit.label(
            trustedPublisherText(for: location),
            .secondary, colour: trustedPublisherColour(for: location), wrapping: true
        )
        trustLabel.preferredMaxLayoutWidth = 620

        let stack = NSStackView(views: showTrust ? [top, trustLabel, outcomeLabel] : [top, outcomeLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs
        stack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return stack
    }

    /// What the controller says it trusts. `reported == false` is "this Hub does
    /// not carry the field" (older than 0.6.51), which is **not** the same answer
    /// as an installed `null` — that one means it trusts nobody and a push will
    /// be refused until the gateway's raw public key is installed.
    private func trustedPublisherText(for location: FleetLocation) -> String {
        guard let trust = location.trustedPublisher, trust.reported == true else {
            return "App Update Key: the Hub did not report one."
        }
        let fingerprint = (trust.fingerprint ?? "").trimmingCharacters(in: .whitespaces)
        guard !fingerprint.isEmpty else {
            return "App Update Key: none installed yet — copy the public key from Gateway → Settings → App Update Key → Technical details "
                + "into Hub → Connections → App updates, then compare fingerprints."
        }
        let who = (trust.publisherId ?? "").trimmingCharacters(in: .whitespaces)
        let short = String(fingerprint.prefix(12))
        let named = fingerprint == identityFingerprint ? "this gateway" : (who.isEmpty ? "unnamed" : who)
        return "App Update Key (fingerprint): \(named) \(short)…"
    }

    private func trustedPublisherColour(for location: FleetLocation) -> NSColor {
        guard let trust = location.trustedPublisher, trust.reported == true else {
            return .secondaryLabelColor
        }
        let fingerprint = (trust.fingerprint ?? "").trimmingCharacters(in: .whitespaces)
        guard !fingerprint.isEmpty else { return Health.warn.text }
        return fingerprint == identityFingerprint ? Health.ok.text : Health.warn.text
    }

    /// The last push's outcome for one controller. A refusal is the gateway's or
    /// the controller's own sentence, kept exactly — a 409 already names the App
    /// Update Key public-key path and comparison fingerprint.
    private func releaseOutcomeText(
        _ outcome: FleetPushOutcome?, for location: FleetLocation, reportKnown: Bool
    ) -> String {
        guard let outcome else {
            return reportKnown ? "Not pushed from here yet." : "No push outcome was reported."
        }
        if outcome.auto == true {
            let at = outcome.at.map { " at \(Self.shortTime($0))" } ?? ""
            if outcome.pending == true { return "Catching up… sending the newest release automatically." }
            if outcome.ok == true {
                let readBack = outcome.release?.versionBuild ?? location.release?.versionBuild ?? "—"
                return "Sent automatically\(at) — it read back \(readBack)."
            }
            let why = (outcome.detail ?? outcome.code ?? "no reason given")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if outcome.skipped == true { return "Not sent automatically — \(why)" }
            let stop = why.hasSuffix(".") ? "" : "."
            let retry = outcome.retryAt.map { "\(stop) Next try at \(Self.shortTime($0))." } ?? ""
            return "Automatic send failed\(at) — \(why)\(retry)"
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

    private static func shortTime(_ epoch: Double) -> String {
        DateFormatter.localizedString(
            from: Date(timeIntervalSince1970: epoch), dateStyle: .none, timeStyle: .short
        )
    }

    /// 409 is the actionable "no trusted publisher key yet" case, so it stays
    /// orange; anything else is a harder failure and reads red.
    private func releaseOutcomeColour(
        _ outcome: FleetPushOutcome?, reportKnown: Bool
    ) -> NSColor {
        guard let outcome else {
            return reportKnown ? .secondaryLabelColor : Health.warn.text
        }
        if outcome.pending == true { return .secondaryLabelColor }
        if outcome.ok == true { return Health.ok.text }
        if outcome.skipped == true { return Health.warn.text }
        // A refused downgrade is a decision for the owner, not a failure: orange,
        // and the detail says what it holds and how to publish anyway.
        if outcome.downgrade == true { return Health.warn.text }
        return outcome.status == 409 ? Health.warn.text : Health.bad.text
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

    /// While an add is in flight: the button says so and cannot be pressed again, the fields are
    /// locked, and the spinner turns beside "Connecting to 100.120.246.9…".
    func setAdding(_ adding: Bool, message: String) {
        addButton.isEnabled = !adding
        addButton.title = adding ? "Adding…" : "Add Location"
        for field in [labelField, urlField, tokenField.secureField, tokenField.plainField] { field.isEnabled = !adding }
        if adding { addSpinner.startAnimation(nil) } else { addSpinner.stopAnimation(nil) }
        if adding { setAddStatus(message) }
    }

    /// Render harness: fill the form and press Add Location, as the owner would.
    func pressAdd(label: String, url: String, token: String) {
        labelField.stringValue = label
        urlField.stringValue = url
        tokenField.stringValue = token
        addButton.performClick(nil)
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
/// **Six columns that fit a 1100 pt window, and a detail strip for the rest.** The table used to have
/// thirteen (about 2,200 pt), so disk and memory, the figures this page exists to show, were clipped off
/// the right edge. State, Disk, Memory and Startup come first, as two-line cells (a machine's location and
/// hardware under its name, when the controller last heard from it under its state); the rarely used facts
/// (workers, work done by kind, the Hub's limitation sentence, the node's address) are in the strip under
/// the table for the selected row. **Studios** is which worker modalities the machine serves, while **work
/// done by kind** is the count of jobs it has completed per modality, and the two are named apart.
final class FleetMachinesTab: NSView {
    private lazy var controller = FleetTableController(columns: columns(), rowHeight: 44)
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private let detail = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    private let detailCard = CardView()
    /// Grouped fleet warnings above the table: one line per kind, capped in height (FleetBanner.swift).
    let warningList = FleetWarningList()
    private var warningTop: NSLayoutConstraint!
    private var belowWarnings: NSLayoutConstraint!
    private var belowStatus: NSLayoutConstraint!
    /// The warning kinds the owner hid (`GET /admin/api/settings/fleet-warnings`); set by the Fleet page.
    var hiddenWarningKinds: Set<String> = []
    /// The fleet summary, for the newest published Studio Hub (Version column, status line); set by the page.
    var summary: FleetSummary?
    private var studios: [String: [String]] = [:]
    /// Injected by the Fleet page (FleetStartup.swift): the per-machine startup badge and "…" menu, and the
    /// strip above the table that holds the bulk "Enable Auto-Start" bar. Left empty, the tab only shows the
    /// badge, which is all the layout test needs.
    var startupCell: ((FleetMachine) -> NSView)?
    /// Injected by the Fleet page (FleetMacTrash.swift): the macOS Trash cell with its "Turn on" button.
    /// Left unset, the tab draws the plain badge.
    var macTrashCell: ((FleetMachine) -> NSView)?
    let startupAccessory = NSStackView()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        startupAccessory.translatesAutoresizingMaskIntoConstraints = false
        startupAccessory.orientation = .vertical
        startupAccessory.alignment = .leading
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        detail.preferredMaxLayoutWidth = 0
        detail.isHidden = false
        detailCard.hold(detail, inset: NSEdgeInsets(top: DS.Space.s, left: DS.Space.m, bottom: DS.Space.s, right: DS.Space.m))
        detailCard.translatesAutoresizingMaskIntoConstraints = false
        warningList.translatesAutoresizingMaskIntoConstraints = false
        addSubview(statusLabel)
        addSubview(warningList)
        addSubview(startupAccessory)
        addSubview(controller.container)
        addSubview(detailCard)
        let pad = PageKit.pagePadding
        warningTop = warningList.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: DS.Space.s)
        belowWarnings = startupAccessory.topAnchor.constraint(equalTo: warningList.bottomAnchor, constant: DS.Space.s)
        belowStatus = startupAccessory.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: DS.Space.s)
        belowStatus.isActive = true
        NSLayoutConstraint.activate([
            warningTop,
            warningList.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            warningList.trailingAnchor.constraint(equalTo: statusLabel.trailingAnchor),
            statusLabel.topAnchor.constraint(equalTo: topAnchor, constant: DS.Space.l),
            statusLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: pad),
            statusLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -pad),
                        startupAccessory.leadingAnchor.constraint(equalTo: statusLabel.leadingAnchor),
            startupAccessory.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -pad),
            controller.container.topAnchor.constraint(equalTo: startupAccessory.bottomAnchor, constant: DS.Space.m),
            controller.container.leadingAnchor.constraint(equalTo: leadingAnchor, constant: pad),
            controller.container.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -pad),
            detailCard.topAnchor.constraint(equalTo: controller.container.bottomAnchor, constant: DS.Space.m),
            detailCard.leadingAnchor.constraint(equalTo: leadingAnchor, constant: pad),
            detailCard.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -pad),
            detailCard.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -pad),
            // A low floor keeps the table from collapsing without ever being the
            // reason the window wants to grow.
            controller.container.heightAnchor.constraint(greaterThanOrEqualToConstant: 120),
        ])
        controller.emptyText = "No machines are reporting. Add a location on the Locations tab."
        controller.onSelection = { [weak self] any in self?.showDetail(any as? FleetMachine) }
        showDetail(nil)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    private func columns() -> [FleetColumn] {
        func machine(_ any: Any) -> FleetMachine? { any as? FleetMachine }
        return [
            FleetColumn(
                id: "machine", title: "Machine", width: 170, minWidth: 110, flex: 3,
                text: { machine($0)?.label ?? machine($0)?.machine ?? "—" },
                sortValue: { .text(machine($0)?.label ?? machine($0)?.machine ?? "") },
                cell: { any in
                    guard let row = machine(any) else { return NSView() }
                    var facts = [row.location == row.label ? nil : row.location, row.network.map { "Network \($0)" },
                                 row.hardwareText].compactMap { $0 }.filter { !$0.isEmpty }
                    if !row.isNode { facts.append("controller") }
                    return fleetTwoLineCell(row.label ?? row.machine ?? "—", facts.joined(separator: " · "),
                                            tooltip: row.stateReason)
                }
            ),
            FleetColumn(
                id: "state", title: "State", width: 132, minWidth: 120,
                text: { machine($0)?.stateLabel ?? "—" },
                // **A refused credential is not an offline machine.** The Hub puts the node in its own
                // `unauthorized` state precisely so a token problem does not read as a network one; its
                // activity word is `offline` because nothing can be dispatched, and drawing that sent the
                // owner to the network for a credential.
                sortValue: { .text(machine($0)?.stateLabel ?? "") },
                cell: { any in
                    guard let row = machine(any) else { return NSView() }
                    // **The reason, not just the verdict**, and "is it switched off, or gone?": the Hub's own
                    // sentence for why it cannot reach the node, and when the controller last heard from it.
                    var tips = [row.workstationLine, FleetOffline.why(row), row.attention, row.stateReason, row.limitation]
                        .compactMap { $0 }
                    if let stop = FleetOffline.lastStopLine(row) { tips.append(stop) }
                    if row.isNode { tips.append("The controller last heard from this Mac \(row.nodeLastSeenText).") }
                    // A Lite Mode change waiting for this Mac is said here; the tooltip has since when.
                    return fleetBadgeCell(row.stateLabel, row.badgeHealth,
                                          caption: row.liteModePending != nil ? "Lite Mode waiting"
                                              : row.isNode ? "seen \(row.nodeLastSeenText)" : nil,
                                          tooltip: tips.isEmpty ? nil : tips.joined(separator: "\n\n"))
                }
            ),
            FleetColumn(
                id: "version", title: "Version", width: 116, minWidth: 100, hideBelow: 640,
                // Studio Hub on this Mac against the newest release this Gateway published.
                text: { [weak self] any in
                    guard let row = machine(any) else { return "—" }
                    return row.versionBadge(latest: self?.summary?.hubLatest)?.text ?? row.hubVersion ?? "—"
                },
                sortValue: { any in
                    let rank = ["behind": 0, "updating": 1, "unknown": 2, "latest": 3]
                    return .number(Double(rank[machine(any)?.versionStatus ?? ""] ?? 4))
                },
                cell: { [weak self] any in
                    guard let row = machine(any) else { return NSView() }
                    guard let badge = row.versionBadge(latest: self?.summary?.hubLatest) else {
                        return fleetTwoLineCell(row.hubVersion ?? "—", nil)
                    }
                    let caption = row.hubVersion.flatMap { badge.text.contains($0) ? nil : $0 }
                    return fleetBadgeCell(badge.text, badge.health, caption: caption, tooltip: badge.tooltip)
                }
            ),
            FleetColumn(
                id: "disk", title: "Disk", width: 116, minWidth: 96,
                text: { row in machine(row).map { "\(FleetFormat.gb($0.diskFree)) free" } ?? "—" },
                sortValue: { .number(machine($0)?.diskFree ?? -1) },
                cell: { any in
                    guard let row = machine(any) else { return NSView() }
                    if row.reachable == false { return fleetTwoLineCell("—", nil) }
                    return fleetMeterCell("\(FleetFormat.gb(row.diskFree)) free", fraction: row.diskUsedFraction,
                                          health: row.diskHealth, tooltip: FleetMachinesTab.diskTooltip(row))
                }
            ),
            FleetColumn(
                id: "memory", title: "Memory", width: 108, minWidth: 92,
                // **Free is what the Hub's own admission counts**, beside the total: "Apple M4 · 17.18 GB"
                // next to "118 GB free" used to read as though the memory were free too.
                text: { row in machine(row).map { "\(FleetFormat.gb($0.availableGb)) free" } ?? "—" },
                sortValue: { .number(machine($0)?.availableGb ?? -1) },
                cell: { any in
                    guard let row = machine(any) else { return NSView() }
                    if row.reachable == false { return fleetTwoLineCell("—", nil) }
                    return fleetMeterCell("\(FleetFormat.gb(row.availableGb)) free", fraction: row.memoryUsedFraction,
                                          health: row.memoryHealth,
                                          tooltip: row.memoryGb.map { "Total \(FleetFormat.gb($0)). Free is what the Hub's own admission counts." })
                }
            ),
            // Startup control: what this Mac starts when it starts, with a "…" menu to change it, in one
            // compact column. Self-contained in FleetStartup.swift; the Groups view reuses it.
            FleetColumn(
                id: "startup", title: "Startup", width: 150, minWidth: 132,
                text: { FleetStartup.health(machine($0)?.startup).text },
                sortValue: { row in
                    // Off first when sorted ascending: those are the Macs that need attention.
                    switch machine(row)?.startup?.startServicesOnLaunch {
                    case false?: return .number(0)
                    case true?: return .number(2)
                    default: return .number(1)
                    }
                },
                tooltip: { FleetStartup.tooltip(machine($0)?.startup, name: machine($0)?.label ?? machine($0)?.machine ?? "This Mac") },
                // Without the injected cell (the layout test) the plain badge is drawn.
                cell: { [weak self] any in
                    guard let row = machine(any) else { return NSView() }
                    if let make = self?.startupCell { return make(row) }
                    let (health, text) = FleetStartup.health(row.startup)
                    return fleetBadgeCell(text, health, tooltip: FleetStartup.tooltip(row.startup, name: row.label ?? row.machine ?? "This Mac"))
                }
            ),
            // macOS's own 30-day Trash emptying (FleetMacTrash.swift): every Hub retirement ends in macOS
            // Trash, so a Mac without it fills its disk. Self-contained, like the Startup column.
            FleetColumn(
                id: "mactrash", title: "Trash auto-empty", width: 128, minWidth: 108,
                text: { FleetMacTrash.health(machine($0)?.startup).text },
                sortValue: { row in
                    // Off first when sorted ascending: those are the Macs that need the button.
                    switch machine(row)?.startup?.macosTrashAutoEmpty {
                    case false?: return .number(0)
                    case true?: return .number(2)
                    default: return .number(1)
                    }
                },
                tooltip: { FleetMacTrash.tooltip(machine($0)?.startup,
                                                 name: machine($0)?.label ?? machine($0)?.machine ?? "This Mac") },
                // Without the injected cell (the layout test) the plain badge is drawn.
                cell: { [weak self] any in
                    guard let row = machine(any) else { return NSView() }
                    if let make = self?.macTrashCell { return make(row) }
                    let (health, text) = FleetMacTrash.health(row.startup)
                    return fleetBadgeCell(text, health,
                                          tooltip: FleetMacTrash.tooltip(row.startup,
                                                                         name: row.label ?? row.machine ?? "This Mac"))
                }
            ),
            FleetColumn(
                id: "jobs", title: "Jobs", width: 128, minWidth: 112, hideBelow: 800,
                text: { row in
                    guard let row = machine(row) else { return "—" }
                    if row.completed == nil && row.failed == nil { return "—" }
                    return "\(row.completed ?? 0) done, \(row.failed ?? 0) failed"
                },
                sortValue: { .number(Double(machine($0)?.completed ?? 0)) },
                cell: { [weak self] any in
                    guard let row = machine(any) else { return NSView() }
                    if row.completed == nil && row.failed == nil { return fleetTwoLineCell("—", nil) }
                    var median: String?
                    if let value = row.medianRuntimeS { median = FleetFormat.seconds(value) + " median" }
                    else if let value = row.averageSeconds { median = FleetFormat.seconds(value) + " average" }
                    // Since the owner's "Reset counters", or the Hub's whole history when it could not honour one.
                    let since = row.countedSince.map { "since " + FleetFormat.stamp($0) }
                        ?? (self?.summary?.countersSince != nil ? "all history (older Hub)" : nil)
                    return fleetTwoLineCell("\(row.completed ?? 0) done · \(row.failed ?? 0) failed",
                                            [median, since].compactMap { $0 }.joined(separator: " · "))
                }
            ),
        ]
    }

    /// What the Disk cell's tooltip adds: the volume, the Hub's own state and its largest consumers.
    static func diskTooltip(_ machine: FleetMachine) -> String? {
        guard let disk = machine.disk else { return machine.diskTotal.map { "Volume: \(FleetFormat.gb($0))" } }
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

    private func studios(for row: FleetMachine) -> [String] {
        if let key = row.machine, let found = studios[key], !found.isEmpty { return found }
        if let key = row.label, let found = studios[key], !found.isEmpty { return found }
        return []
    }

    /// The facts that did not fit a column, for the selected machine.
    private func showDetail(_ row: FleetMachine?) {
        guard let row else {
            detail.stringValue = "Select a machine to see its studios, the work it has done by kind, and what the Hub says it cannot read."
            return
        }
        var lines: [String] = []
        let served = studios(for: row)
        lines.append("Studios (workers): " + (served.isEmpty ? "—" : served.joined(separator: ", ")))
        if let kinds = row.modalities, !kinds.isEmpty {
            lines.append("Work done by kind: " + kinds.sorted { $0.key < $1.key }
                .map { "\($0.value) \($0.key == "tts" ? "TTS" : $0.key)" }.joined(separator: ", "))
        }
        if row.isNode {
            lines.append("Node: \(row.nodeRoleText)" + (row.nodeAddress.map { " at \($0)" } ?? "")
                         + " · last seen \(row.nodeLastSeenText)")
        }
        if let why = row.attention { lines.append("Needs attention: " + why) }
        if let version = row.hubVersion {
            let badge = row.versionBadge(latest: summary?.hubLatest)
            lines.append("Studio Hub \(version)" + (badge.map { " · \($0.text)" } ?? ""))
        }
        if let ignored = row.notInstalledStudios, !ignored.isEmpty {
            lines.append("Not run on this Mac (ignored): " + ignored.joined(separator: ", "))
        }
        if row.limitationText != "—" { lines.append("Limitation: " + row.limitationText) }
        detail.stringValue = lines.joined(separator: "\n")
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
        showDetail(controller.table.selectedRow >= 0 ? controller.rows[controller.table.selectedRow] as? FleetMachine : nil)
        let count = rows.count == machines.count
            ? "\(machines.count) machine\(machines.count == 1 ? "" : "s")."
            : "\(rows.count) of \(machines.count) machines match the filter."
        let text = NSMutableAttributedString(
            string: count,
            attributes: [.font: TextRole.secondary.font, .foregroundColor: NSColor.secondaryLabelColor])
        // "On latest 39/41 (Studio Hub 0.6.132) · 2 behind · 6 not answering": the fleet's version line.
        if let summary, let latest = summary.hubLatest {
            var parts = ["On latest \(summary.onLatest ?? 0)/\(summary.versionKnown ?? 0) (Studio Hub \(latest))"]
            if let n = summary.hubUpdating, n > 0 { parts.append("\(n) updating") }
            if let n = summary.hubBehind, n > 0 { parts.append("\(n) behind") }
            if let n = summary.hubUnknown, n > 0 { parts.append("\(n) not answering") }
            let behind = (summary.hubBehind ?? 0) > 0
            text.append(NSAttributedString(
                string: " " + parts.joined(separator: " · ") + ".",
                attributes: [.font: TextRole.secondary.font,
                             .foregroundColor: behind ? Health.warn.text : NSColor.secondaryLabelColor]))
        }
        statusLabel.attributedStringValue = text
        // The warnings are about the whole fleet, so they ignore the filter: a Mac that is filling up must
        // not disappear because a search hid it. One line per kind; the area scrolls past ~5 lines.
        warningList.set(FleetWarnings.build(machines: machines, hidden: hiddenWarningKinds))
        // An empty banner takes no room: the accessory strip sits straight under the summary line.
        belowWarnings.isActive = !warningList.isEmpty
        belowStatus.isActive = warningList.isEmpty
        needsLayout = true
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
    private let master = FleetTableController(columns: FleetModelsTab.masterColumns())
    // Built in `init`: the Worker column reads the per-worker catalogue, which is
    // instance state (set by `update`), so the closure captures the tab.
    private var detail: FleetTableController!
    private let masterTitle = PageKit.sectionTitle("Local models")
    private let detailTitle = PageKit.sectionTitle("Machines serving this model")
    private let statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
    /// Every "Install on every Mac that can run it" in progress, per network (FleetModelSpread.swift).
    let spreadView = FleetModelSpreadView()
    /// "Install on every Mac that can run it" for the selected model (package id, name); set by the Fleet page.
    var onInstallEverywhere: ((String, String) -> Void)?
    private lazy var everywhereButton = FleetClosureButton(title: FleetModelSpread.actionTitle) { [weak self] in
        guard let self, let picked = self.selectedPackage() else { return }
        self.onInstallEverywhere?(picked.0, picked.1)
    }

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
            }), rowHeight: 44)
        // Master and detail sit inside this tab's own scroll view, with fixed
        // heights in the document. That keeps the tab's required height out of the
        // window: a plain stack of two fixed tables grew the window at the app's
        // 860x560 minimum, which is the trap `AGENTS.md` records.
        statusLabel.preferredMaxLayoutWidth = 520
        master.container.heightAnchor.constraint(equalToConstant: 220).isActive = true
        detail.container.heightAnchor.constraint(equalToConstant: 220).isActive = true
        page.addArrangedSubview(spreadView)
        fullWidth(spreadView)
        page.addArrangedSubview(masterTitle)
        page.setCustomSpacing(DS.Space.m, after: masterTitle)
        page.addArrangedSubview(statusLabel)
        page.addArrangedSubview(master.container)
        let detailHeader = PageKit.row([detailTitle, NSView(), everywhereButton])
        page.addArrangedSubview(detailHeader)
        page.setCustomSpacing(DS.Space.m, after: detailHeader)
        page.addArrangedSubview(detail.container)
        fullWidth(detailHeader)
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
            self.everywhereButton.isEnabled = self.selectedPackage() != nil
        }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The selected model's package and name, when some Mac that reports it does not have it yet.
    private func selectedPackage() -> (String, String)? {
        guard let aggregate = aggregates.first(where: { $0.model == selectedModel }),
              let package = aggregate.rows.compactMap(\.packageId).first,
              aggregate.rows.contains(where: { !$0.cacheIsCached }) else { return nil }
        return (package, aggregate.label)
    }

    func update(models: [FleetLocalModel], workers: [FleetWorker], filter: FleetFilter,
                installs: [FleetModelSpread] = []) {
        spreadView.update(installs)
        defer {
            everywhereButton.isEnabled = selectedPackage() != nil
            everywhereButton.toolTip = everywhereButton.isEnabled
                ? "Each network downloads it once; its Macs copy it from each other."
                : "Every Mac that reports this model has it already."
        }
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

    /// Eight columns that fit a 1100 pt window. "Modality" repeated "Studio", "Available" repeated the
    /// numerator of "Machines serving", and "Cached" moved to the per-machine table below.
    private static func masterColumns() -> [FleetColumn] {
        func aggregate(_ any: Any) -> FleetModelAggregate? { any as? FleetModelAggregate }
        return [
            FleetColumn(
                id: "model", title: "Model", width: 200, minWidth: 140, flex: 3,
                text: { aggregate($0)?.label ?? "—" },
                sortValue: { .text(aggregate($0)?.label ?? "") },
                tooltip: { aggregate($0)?.model }
            ),
            FleetColumn(
                id: "studio", title: "Studio", width: 90, minWidth: 70,
                text: { aggregate($0)?.studio ?? "—" },
                sortValue: { .text(aggregate($0)?.studio ?? "") }
            ),
            FleetColumn(
                id: "locations", title: "Locations", width: 76, minWidth: 60, hideBelow: 700,
                text: { "\(aggregate($0)?.locations.count ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.locations.count ?? 0)) },
                tooltip: { aggregate($0)?.locations.joined(separator: ", ") }
            ),
            FleetColumn(
                id: "serving", title: "Serving", width: 84, minWidth: 70,
                text: { row in
                    guard let row = aggregate(row) else { return "—" }
                    return "\(row.machinesServing) of \(row.machinesReporting)"
                },
                sortValue: { .number(Double(aggregate($0)?.machinesServing ?? 0)) },
                colour: { (aggregate($0)?.machinesServing ?? 0) > 0 ? .labelColor : Health.warn.text },
                tooltip: { _ in "Machines that can serve this model right now, of the machines that report it." }
            ),
            FleetColumn(
                id: "bestfit", title: "Best fit", width: 120, minWidth: 90,
                text: { aggregate($0)?.bestFit ?? "—" },
                sortValue: { .text(aggregate($0)?.bestFit ?? "") }
            ),
            FleetColumn(
                id: "memory", title: "Memory floor", width: 100, minWidth: 92,
                text: { row in
                    guard let value = aggregate(row)?.memoryFloor else { return "—" }
                    return FleetFormat.gb(value)
                },
                sortValue: { .number(aggregate($0)?.memoryFloor ?? 0) }
            ),
            FleetColumn(
                id: "dispatch", title: "Dispatch", width: 96, minWidth: 80, hideBelow: 700,
                text: { aggregate($0)?.dispatch ?? "—" },
                sortValue: { .text(aggregate($0)?.dispatch ?? "") }
            ),
            FleetColumn(
                id: "issues", title: "Issues", width: 60, minWidth: 50,
                text: { "\(aggregate($0)?.issues ?? 0)" },
                sortValue: { .number(Double(aggregate($0)?.issues ?? 0)) },
                colour: { (aggregate($0)?.issues ?? 0) > 0 ? Health.bad.text : .labelColor }
            ),
        ]
    }

    /// One row per machine serving the model: the machine with its location and worker under it, the three
    /// yes/no gates, the fit, and the Hub's own note wrapped across two lines.
    private static func detailColumns(
        worker: @escaping (FleetLocalModel) -> String
    ) -> [FleetColumn] {
        func row(_ any: Any) -> FleetLocalModel? { any as? FleetLocalModel }
        return [
            // **Which worker actually serves it**, from the worker's own catalogue. The machine is where the
            // hardware is; the worker is what would run the job, and on a Mac with four studios those are
            // not the same answer.
            FleetColumn(
                id: "dmachine", title: "Machine", width: 170, minWidth: 130, flex: 3,
                text: { row($0)?.machine ?? "—" },
                sortValue: { .text(row($0)?.machine ?? "") },
                cell: { any in
                    guard let row = row(any) else { return NSView() }
                    let place = FleetFormat.text(row.location ?? row.locationId)
                    let served = worker(row)
                    return fleetTwoLineCell(row.machine ?? "—", served == "—" ? place : "\(place) · \(served)")
                }
            ),
            FleetColumn(
                id: "davailable", title: "Available", width: 74, minWidth: 66,
                text: { fleetFlag(row($0)?.available) },
                sortValue: { .number((row($0)?.available == true) ? 1 : 0) },
                colour: { row($0)?.available == true ? .labelColor : Health.warn.text }
            ),
            FleetColumn(
                id: "druntime", title: "Runtime", width: 70, minWidth: 62,
                text: { fleetFlag(row($0)?.runtimeReady) },
                sortValue: { .number((row($0)?.runtimeReady == true) ? 1 : 0) },
                tooltip: { _ in "Runtime ready" }
            ),
            FleetColumn(
                id: "dmemory", title: "Memory", width: 70, minWidth: 62,
                text: { fleetFlag(row($0)?.memoryEligible) },
                sortValue: { .number((row($0)?.memoryEligible == true) ? 1 : 0) },
                tooltip: { _ in "Memory eligible" }
            ),
            FleetColumn(
                id: "dfit", title: "Fit", width: 150, minWidth: 120,
                text: { row($0)?.fitLabel ?? "—" },
                sortValue: { .text(row($0)?.fitLabel ?? "") },
                cell: { any in
                    guard let row = row(any) else { return NSView() }
                    var caption = [FleetFormat.words(row.dispatchState), row.cacheState].compactMap { $0 }
                        .filter { $0 != "—" && !$0.isEmpty }.joined(separator: " · ")
                    if caption.isEmpty { caption = "" }
                    return fleetTwoLineCell(row.fitLabel, caption, tooltip: "Dispatch state and cache state")
                }
            ),
            FleetColumn(
                id: "dnote", title: "Note", width: 200, minWidth: 120, flex: 4, sortable: false,
                text: { FleetFormat.text(row($0)?.issueText ?? row($0)?.fit?.hint) },
                cell: { any in
                    guard let row = row(any) else { return NSView() }
                    if let error = row.issueText {
                        return fleetWrappingCell(error, colour: Health.bad.text)
                    }
                    return fleetWrappingCell(
                        FleetFormat.text(row.fit?.hint), colour: .secondaryLabelColor)
                }
            ),
        ]
    }
}

// MARK: - Install on every Mac

/// Fleet → Models: what each "Install on every Mac" is doing, per network and per Mac (`model_installs`). It shows
/// every running one and the newest finished one; the Fleet page re-reads every few seconds while one runs.
final class FleetModelSpreadView: NSStackView {
    var onStop: ((FleetModelSpread) -> Void)?
    private var shown: [FleetModelSpread] = []
    private var expanded = Set<String>()
    private var runningIDs = Set<String>()
    private var detailScrolls: [String: NSScrollView] = [:]
    private var detailOffsets: [String: CGFloat] = [:]

    init() {
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = DS.Space.m
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func update(_ plans: [FleetModelSpread]) {
        shown = plans.filter(\.running) + plans.filter { !$0.running }.prefix(1)
        let nowRunning = Set(shown.filter(\.running).map(key))
        expanded.subtract(runningIDs.subtracting(nowRunning))
        expanded.formIntersection(Set(shown.map(key)))
        runningIDs = nowRunning
        rebuild()
    }

    private func key(_ plan: FleetModelSpread) -> String {
        plan.id ?? plan.packageId ?? plan.label ?? "install"
    }

    private func rebuild() {
        for (id, scroll) in detailScrolls { detailOffsets[id] = scroll.contentView.bounds.origin.y }
        detailOffsets = detailOffsets.filter { Set(shown.map(key)).contains($0.key) }
        detailScrolls.removeAll()
        for view in arrangedSubviews {
            removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        for plan in shown {
            let card = card(for: plan)
            addArrangedSubview(card)
            card.widthAnchor.constraint(equalTo: widthAnchor).isActive = true
        }
        isHidden = shown.isEmpty
        layoutSubtreeIfNeeded()
        for (id, scroll) in detailScrolls {
            guard let offset = detailOffsets[id] else { continue }
            scroll.contentView.scroll(to: NSPoint(x: 0, y: offset))
            scroll.reflectScrolledClipView(scroll.contentView)
        }
    }
    static func health(_ state: String?) -> Health {
        switch state {
        case "done", "have": return .ok
        case "failed": return .bad
        case "copying", "downloading": return .info
        default: return .off
        }
    }

    private func card(for plan: FleetModelSpread) -> NSView {
        let name = plan.label ?? plan.packageId ?? "?"
        let count = plan.done.flatMap { done in plan.total.map { "\(done) of \($0) Macs have it" } }
            ?? "Progress not reported"
        let macs = (plan.networks ?? []).flatMap { $0.macs ?? [] }
        let failed = macs.filter { $0.state == "failed" }
        let active = macs.filter { ["copying", "downloading", "waiting"].contains($0.state ?? "") }.count
        let skipped = macs.filter { $0.state == "skipped" }.count
        let title: String
        switch plan.state {
        case "running": title = "Installing \(name)"
        case "stopped": title = "Stopped installing \(name)"
        default: title = "Install finished: \(name)"
        }
        let titleLabel = PageKit.label(title, .rowTitle, wrapping: true)
        titleLabel.maximumNumberOfLines = 2
        titleLabel.preferredMaxLayoutWidth = 520
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        titleLabel.toolTip = title
        var header: [NSView] = [titleLabel, NSView()]
        if plan.running {
            header.append(FleetClosureButton(title: "Stop") { [weak self] in self?.onStop?(plan) })
        }
        let headerRow = PageKit.row(header)
        let stack = NSStackView(views: [headerRow])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs
        headerRow.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        let reportID = key(plan)
        let detailsVisible = expanded.contains(reportID)
        let details = FleetClosureButton(title: detailsVisible ? "Hide details" : "Show details") { [weak self] in
            guard let self else { return }
            if self.expanded.contains(reportID) { self.expanded.remove(reportID) }
            else { self.expanded.insert(reportID) }
            self.rebuild()
        }
        details.setAccessibilityRole(.disclosureTriangle)
        details.setAccessibilityLabel("Installation details for \(name)")
        details.setAccessibilityValue(detailsVisible ? 1 : 0)
        var parts = [count]
        if active > 0 { parts.append("\(active) active") }
        if !failed.isEmpty { parts.append("\(failed.count) failed") }
        if skipped > 0 { parts.append("\(skipped) skipped") }
        let progress = PageKit.label(parts.joined(separator: " · "), .secondary,
                                     colour: failed.isEmpty ? .secondaryLabelColor : Health.bad.text,
                                     wrapping: true)
        progress.maximumNumberOfLines = 2
        progress.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let summary = PageKit.row([progress, NSView(), details])
        stack.addArrangedSubview(summary)
        summary.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        if let first = failed.first {
            let text = "\(first.label ?? first.machine ?? "Mac") — \(first.text ?? "Installation failed")"
            let error = PageKit.label(text, .secondary, colour: Health.bad.text, wrapping: true)
            error.maximumNumberOfLines = 2
            error.toolTip = text
            error.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            stack.addArrangedSubview(error)
            error.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        }
        if detailsVisible {
            let scroll = NSScrollView()
            scroll.hasVerticalScroller = true
            scroll.hasHorizontalScroller = false
            scroll.drawsBackground = false
            scroll.translatesAutoresizingMaskIntoConstraints = false
            // ponytail: every report's details use 220 points; longer reports scroll within it.
            scroll.heightAnchor.constraint(equalToConstant: 220).isActive = true
            let document = FlippedView()
            document.translatesAutoresizingMaskIntoConstraints = false
            let rows = detailRows(for: plan)
            rows.translatesAutoresizingMaskIntoConstraints = false
            document.addSubview(rows)
            scroll.documentView = document
            NSLayoutConstraint.activate([
                document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
                rows.topAnchor.constraint(equalTo: document.topAnchor),
                rows.leadingAnchor.constraint(equalTo: document.leadingAnchor),
                rows.trailingAnchor.constraint(equalTo: document.trailingAnchor),
                rows.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            ])
            stack.addArrangedSubview(scroll)
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            detailScrolls[reportID] = scroll
        }
        let card = CardView()
        card.hold(stack)
        return card
    }

    private func detailRows(for plan: FleetModelSpread) -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs
        for network in plan.networks ?? [] {
            let heading = PageKit.label("\(network.title ?? "Network") · \(network.done ?? 0) of \(network.total ?? 0)",
                                        .captionStrong, colour: .secondaryLabelColor)
            stack.addArrangedSubview(heading)
            stack.setCustomSpacing(DS.Space.xs, after: heading)
            for mac in network.macs ?? [] {
                var line = "\(mac.label ?? mac.machine ?? "?") — \(mac.text ?? "")"
                if let note = mac.note, !note.isEmpty { line += " (\(note))" }
                let health = Self.health(mac.state)
                let label = PageKit.label(line, .secondary,
                                          colour: health == .off ? .secondaryLabelColor : health == .info ? .labelColor : health.text,
                                          wrapping: true)
                label.preferredMaxLayoutWidth = 640
                label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                stack.addArrangedSubview(label)
                label.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            }
        }
        return stack
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
    private let activity = FleetTableController(columns: FleetJobsTab.activityColumns(), rowHeight: 44)
    private let runningTitle = PageKit.sectionTitle("Running now")
    private let queueLine = PageKit.help("")
    private let activityTitle = PageKit.sectionTitle("Work by machine")
    private let problemsTitle = PageKit.sectionTitle("Needs attention")
    private let problems = NSStackView()

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        running = FleetTableController(columns: FleetJobsTab.runningColumns(
            cancel: { [weak self] job in self?.onCancel?(job) }), rowHeight: 44)
        running.emptyText = "Nothing is running right now."
        activity.emptyText = "No machine has reported any work."
        running.container.heightAnchor.constraint(equalToConstant: 240).isActive = true
        activity.container.heightAnchor.constraint(equalToConstant: 280).isActive = true

        let refresh = FleetClosureButton(title: "Refresh") { [weak self] in self?.onRefresh?() }
        let header = PageKit.row([runningTitle, NSView(), refresh])
        page.addArrangedSubview(header)
        fullWidth(header)
        page.addArrangedSubview(queueLine)
        fullWidth(queueLine)
        page.addArrangedSubview(running.container)
        fullWidth(running.container)
        page.addArrangedSubview(activityTitle)
        page.setCustomSpacing(DS.Space.m, after: activityTitle)
        page.addArrangedSubview(activity.container)
        fullWidth(activity.container)
        page.addArrangedSubview(problemsTitle)
        page.setCustomSpacing(DS.Space.m, after: problemsTitle)
        problems.orientation = .vertical
        problems.alignment = .leading
        problems.spacing = DS.Space.s
        // Said once for every card below: what Retry, Cancel and Refresh each do.
        let advice = PageKit.help(
            "Retry re-queues a failed item — the Hub accepts only an item in an error state, and refuses a "
            + "cancelled batch or an uncertain item (discard that first). Cancel asks the Hub to stop the "
            + "batch; Refresh re-reads its list.")
        page.addArrangedSubview(advice)
        fullWidth(advice)
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
        queueLine.stringValue = snapshot.gatewayQueue?.text ?? ""
        queueLine.isHidden = snapshot.gatewayQueue == nil
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
                .secondary, colour: .secondaryLabelColor, wrapping: true))
            return
        }
        for job in broken {
            let card = problemCard(job)
            problems.addArrangedSubview(card)
            fullWidth(card)
        }
    }

    private func problemCard(_ job: FleetJob) -> NSView {
        let health: Health = job.stalled == true ? .warn : .bad
        let colour = health.text
        let heading = PageKit.label(
            "\(FleetFormat.text(job.location)) · \(FleetFormat.words(job.modality)) · \(FleetFormat.text(job.model))",
            .rowTitle
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
        let message = PageKit.label(reason, .secondary, colour: colour, wrapping: true)
        message.maximumNumberOfLines = 4
        let progress = PageKit.label(
            Self.progressText(job) + Self.troubleText(job) + " · started "
            + FleetFormat.relative(job.startedAt),
            .caption, colour: .tertiaryLabelColor
        )
        let stack = FleetTintStack(views: [heading, message, progress])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs
        stack.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.m, bottom: DS.Space.m, right: DS.Space.m)
        stack.wantsLayer = true
        stack.tint = health.tint
        stack.translatesAutoresizingMaskIntoConstraints = false

        // **The failed items, named and retryable.** From Hub 0.6.48 the batch row
        // carries `errors[]` — the item index, a code and the Hub's own message —
        // so the card can say which item failed instead of only how many, and can
        // offer the one action that applies to it.
        for item in job.failedItems {
            let index = item.itemIndex.map { "Item \($0)" } ?? "Item"
            let label = PageKit.label("\(index) · \(item.text)", .caption,
                                      colour: .secondaryLabelColor, wrapping: true)
            label.maximumNumberOfLines = 3
            let retry = FleetClosureButton(title: "Retry") { [weak self] in
                self?.onRetry?(job, item)
            }
            let row = PageKit.row([label, NSView(), retry])
            stack.addArrangedSubview(row)
            row.widthAnchor.constraint(
                equalTo: stack.widthAnchor, constant: -2 * DS.Space.m
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

    /// Six columns that fit a 1100 pt window: the model carries its modality and label as a caption, the
    /// state is a badge, and the progress is two short lines.
    private static func runningColumns(cancel: @escaping (FleetJob) -> Void) -> [FleetColumn] {
        func job(_ any: Any) -> FleetJob? { any as? FleetJob }
        func health(_ job: FleetJob) -> Health {
            // A cancelled batch that also errored is red; cancelled by the owner is grey; in flight is amber.
            if (job.errorCount ?? 0) > 0 || (job.uncertain ?? 0) > 0 { return .bad }
            if job.cancelled == true { return Health(state: "cancelled") }
            return Health(state: "running")
        }
        return [
            FleetColumn(
                id: "jmodel", title: "Model", width: 200, minWidth: 130, flex: 3,
                text: { FleetFormat.text(job($0)?.model ?? job($0)?.label) },
                sortValue: { .text(job($0)?.model ?? "") },
                tooltip: { job($0)?.label },
                cell: { any in
                    guard let job = job(any) else { return NSView() }
                    let model = FleetFormat.text(job.model ?? job.label)
                    let short = model.split(separator: "/").last.map(String.init) ?? model
                    let kind = FleetJobsTab.typeName(job.modality)
                    return fleetTwoLineCell(short, [kind, job.label].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "),
                                            tooltip: model)
                }
            ),
            FleetColumn(
                id: "jlocation", title: "Location", width: 130, minWidth: 100,
                text: { FleetFormat.text(job($0)?.location) },
                sortValue: { .text(job($0)?.location ?? "") }
            ),
            FleetColumn(
                id: "jprogress", title: "Progress", width: 150, minWidth: 130,
                text: { row in
                    guard let job = job(row) else { return "—" }
                    return FleetJobsTab.progressText(job)
                },
                sortValue: { .number(Double(job($0)?.running ?? 0)) },
                tooltip: { job($0).map(FleetJobsTab.progressText) },
                cell: { any in
                    guard let job = job(any) else { return NSView() }
                    return fleetTwoLineCell("\(job.done ?? 0) of \(job.total ?? 0) done",
                                            "\(job.running ?? 0) running · \(job.queued ?? 0) queued"
                                            + ((job.retrying ?? 0) > 0 ? " · \(job.retrying ?? 0) retrying" : ""),
                                            tooltip: FleetJobsTab.progressText(job))
                }
            ),
            FleetColumn(
                id: "jstate", title: "State", width: 128, minWidth: 110,
                text: { row in
                    guard let job = job(row) else { return "—" }
                    return FleetJobsTab.stateText(job)
                },
                sortValue: { .text(job($0).map(FleetJobsTab.stateText) ?? "") },
                tooltip: { job($0)?.governorNote },
                cell: { any in
                    guard let job = job(any) else { return NSView() }
                    let word = FleetJobsTab.stateText(job)
                    return fleetBadgeCell(word.prefix(1).uppercased() + word.dropFirst(), health(job),
                                          tooltip: job.governorNote)
                }
            ),
            FleetColumn(
                id: "jstarted", title: "Started", width: 80, minWidth: 70, hideBelow: 700,
                text: { FleetFormat.relative(job($0)?.startedAt) },
                sortValue: { .number(job($0)?.startedAt ?? 0) }
            ),
            // **The cancel is the one Fleet action that reaches upstream.** It is
            // offered only while the Hub still holds the batch and has not already
            // been asked to stop it; the answer comes back as a request, not a
            // promise, and the banner says which.
            FleetColumn(
                id: "jcancel", title: "", width: 76, flex: 0, sortable: false,
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
                        button.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: DS.Space.xs),
                        button.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
                    ])
                    return cell
                }
            ),
        ]
    }

    /// "TTS", "Image", "Transcription": the acronym keeps its capitals (`capitalized` made it "Tts").
    static func typeName(_ modality: String?) -> String {
        let word = FleetFormat.words(modality)
        switch word.lowercased() {
        case "tts", "voice.tts": return "TTS"
        case "—", "": return ""
        default: return word.prefix(1).uppercased() + word.dropFirst()
        }
    }

    private static func activityColumns() -> [FleetColumn] {
        func machine(_ any: Any) -> FleetMachine? { any as? FleetMachine }
        return [
            FleetColumn(
                id: "amachine", title: "Machine", width: 170, minWidth: 130, flex: 3,
                text: { machine($0)?.label ?? machine($0)?.machine ?? "—" },
                sortValue: { .text(machine($0)?.label ?? "") },
                cell: { any in
                    guard let row = machine(any) else { return NSView() }
                    return fleetTwoLineCell(row.label ?? row.machine ?? "—", row.location == row.label ? nil : row.location)
                }
            ),
            FleetColumn(
                id: "adone", title: "Done", width: 70, minWidth: 56,
                text: { FleetFormat.count(machine($0)?.completed) },
                sortValue: { .number(Double(machine($0)?.completed ?? 0)) }
            ),
            FleetColumn(
                id: "afailed", title: "Failed", width: 70, minWidth: 56,
                text: { FleetFormat.count(machine($0)?.failed) },
                sortValue: { .number(Double(machine($0)?.failed ?? 0)) }
            ),
            FleetColumn(
                id: "amedian", title: "Median", width: 100, minWidth: 90,
                text: { row in
                    guard let machine = machine(row) else { return "—" }
                    if let median = machine.medianRuntimeS { return FleetFormat.seconds(median) + " med" }
                    if let average = machine.averageSeconds { return FleetFormat.seconds(average) + " avg" }
                    return "—"
                },
                sortValue: { .number(machine($0)?.medianRuntimeS ?? machine($0)?.averageSeconds ?? 0) }
            ),
            FleetColumn(
                id: "alimitation", title: "Limitation", width: 220, minWidth: 120, flex: 3,
                text: { FleetFormat.text(machine($0)?.limitation) },
                sortValue: { .text(machine($0)?.limitation ?? "") },
                tooltip: { machine($0)?.limitation }
            ),
        ]
    }

}
