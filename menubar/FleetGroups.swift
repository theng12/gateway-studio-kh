// Fleet > Groups: one card per controller group, so the question "is every group and every Mac in it
// alright?" is answered on one screen instead of at the physical machine.
//
// Everything here is derived from the snapshot the page already reads (`/admin/api/fleet`); the one field
// the Hub may not state yet is which job a node runs (`machines[].current`), and without it the "Doing now"
// column says only what the Hub does say: working, and which studios are busy. Approved design:
// `_orchestra/audit-20261001/design/README.md` section 4.
import AppKit

// MARK: - Derived group data

/// What a group card needs, every field a function of the snapshot.
struct GroupSummary {
    let location: FleetLocation
    let machines: [FleetMachine]
    let batches: [FleetJob]
    let issues: [FleetIssue]

    var name: String { location.label ?? location.id ?? "Location" }
    var id: String { location.id ?? name }
    /// Down = the controller does not answer, or the Gateway paused it after it stopped answering.
    /// A group the owner paused by hand is not down: it answers, it simply gets no new work.
    var isDown: Bool { location.reachable != true || (location.pausedAt != nil && !location.isPausedByOwner) }
    var isPausedByOwner: Bool { location.isPausedByOwner }

    /// What the table lists. A group that is down lists the roster the gateway remembers (last known
    /// state), never an empty box; a live one lists the machines it reports.
    var showsRoster: Bool { isDown && !location.nodes.isEmpty }

    var online: Int { machines.filter { $0.reachable != false && !isOffline($0) }.count }
    var running: Int { batches.reduce(0) { $0 + ($1.running ?? 0) } }
    var queued: Int { batches.reduce(0) { $0 + ($1.queued ?? 0) } }
    var nodeCount: Int { showsRoster ? location.nodes.count : machines.count }

    /// Offline as a fault. A switched-off Lite Mode Mac is expected and counted apart (`offLite`).
    private func isOffline(_ machine: FleetMachine) -> Bool {
        (machine.state ?? "").lowercased() == "offline" && machine.liteOff != true
    }
    /// Shop Macs in Lite Mode that are switched off: "2 off (Lite Mode)", never "offline".
    var offLite: Int { machines.filter { $0.liteOff == true }.count }

    /// The machine with the least disk, ranked by tier first and free space second. Offline nodes report none.
    var worstDisk: FleetMachine? {
        machines.filter { $0.diskFree != nil && $0.reachable != false }.min {
            if $0.diskHealth != $1.diskHealth { return $0.diskHealth > $1.diskHealth }
            return ($0.diskFree ?? 0) < ($1.diskFree ?? 0)
        }
    }

    /// The machine with the least free memory by share of its RAM, ranked by tier first.
    var worstMemory: FleetMachine? {
        machines.filter { $0.availableGb != nil && $0.reachable != false }.min {
            if $0.memoryHealth != $1.memoryHealth { return $0.memoryHealth > $1.memoryHealth }
            return ($0.availableGb ?? 0) / max($0.memoryGb ?? 1, 1) < ($1.availableGb ?? 0) / max($1.memoryGb ?? 1, 1)
        }
    }

    func nodeHealth(_ machine: FleetMachine) -> Health { machine.activityHealth }

    /// Macs that said their services will not start after a restart (any reachability: the fix is the same).
    var startupOff: Int { machines.filter { $0.startup?.startServicesOnLaunch == false }.count }

    /// The controller's own Mac (the one row that is not a node).
    var controller: FleetMachine? { machines.first { !$0.isNode } }

    private func versions(_ word: String) -> Int { machines.filter { ($0.versionStatus ?? "") == word }.count }
    var onLatest: Int { versions("latest") }
    var updating: Int { versions("updating") }
    var behind: Int { versions("behind") }
    /// Macs that answered with a version (offline ones are unknown, and not counted either way).
    var versionKnown: Int { onLatest + updating + behind }
    var needsAttention: Int { machines.filter { ($0.state ?? "").lowercased() == "needs_attention" }.count }

    /// "Hub 0.6.132 · Latest · 10 of 11 on latest" for the card header; the bare version when the Gateway
    /// has published nothing to compare with.
    var versionLine: (text: String, health: Health)? {
        guard let version = location.version ?? controller?.hubVersion else { return nil }
        var parts = ["Hub \(version)"]
        var health = Health.off
        if let badge = controller?.versionBadge(latest: nil) {
            parts.append(badge.text.hasPrefix("Behind") ? "Behind" : badge.text)
            health = badge.health == .ok ? .off : badge.health
        }
        if versionKnown > 0, controller?.versionStatus != nil {
            parts.append("\(onLatest) of \(versionKnown) on latest")
            if updating > 0 { parts.append("\(updating) updating") }
            if behind > 0 { health = max(health, .warn) }
        }
        if let failed = FleetUpdateFailure.countText(machines) {
            parts.append(failed)
            health = max(health, .warn)
        }
        return (parts.joined(separator: " · "), health)
    }

    /// Down (red) = the group is unreachable or paused. Attention (amber) = any node offline or refused, any
    /// disk or memory not green, a stalled job, a drained group, a Mac that will not auto-start, or any
    /// warning or error the Gateway raised. Red as well when the controller's own Mac has no disk left: the
    /// group cannot be trusted. Everything else is healthy.
    var health: Health {
        if isDown { return .bad }
        if isPausedByOwner { return .off }   // paused by you: quiet grey, never "needs attention"
        var worst = Health.ok
        for machine in machines where (machine.reachable != false || machine.isNode) && machine.liteOff != true {
            for state in [nodeHealth(machine), machine.diskHealth, machine.faultMemoryHealth] where state == .warn || state == .bad {
                worst = max(worst, .warn)
            }
        }
        if startupOff > 0 || location.drained == true { worst = max(worst, .warn) }
        if issues.contains(where: { $0.severity == "error" || $0.severity == "warning" }) { worst = max(worst, .warn) }
        if batches.contains(where: { $0.stalled == true }) { worst = max(worst, .warn) }
        if let controller = machines.first(where: { !$0.isNode }), controller.diskHealth == .bad { worst = .bad }
        return worst
    }

    /// Whether the banner and the "Needs attention" filter count this group. A group paused by hand is grey, not a fault.
    var needsLook: Bool { health != .ok && !isPausedByOwner }

    /// The badge word for the group.
    var stateWord: String { isDown ? "Down" : isPausedByOwner ? "Paused" : health == .ok ? "Healthy" : "Attention" }

    /// The Gateway's own sentences for what is wrong (never paraphrased), for the expanded body.
    var notes: [(health: Health, text: String)] {
        if isDown {
            return [(.bad, location.pauseReason ?? location.error ?? "This group is not answering.")]
        }
        return issues.filter { $0.severity == "error" || $0.severity == "warning" }
            .map { (Health(severity: $0.severity), $0.message ?? "") }
    }

    /// The same, grouped by kind for the card body: "5 machines are offline — check them" once, with
    /// [Show Macs], instead of one sentence per Mac (FleetBanner.swift).
    var warnings: [FleetWarning] {
        if isDown {
            return [FleetWarning(id: "down", kind: "down", health: .bad,
                                 text: location.pauseReason ?? location.error ?? "This group is not answering.", fix: nil)]
        }
        return FleetWarnings.group(issues: issues)
    }

    /// Counted reasons, so a collapsed card still says what is wrong.
    var problems: [String] {
        if isDown {
            let since = location.unreachableSince.map { " · last answered \(FleetFormat.relative($0))" } ?? ""
            return [(location.pausedAt != nil ? "Paused, not answering" : "Not answering") + since]
        }
        if isPausedByOwner { return ["Paused by you · gets no new work"] }
        func plural(_ n: Int, _ one: String, _ many: String) -> String { "\(n) \(n == 1 ? one : many)" }
        var out: [String] = []
        let live = machines.filter { $0.reachable != false }
        let refused = machines.filter { $0.nodeStateIsUnauthorized }.count
        let offline = machines.filter { isOffline($0) && !$0.nodeStateIsUnauthorized }.count
        let disks = live.filter { $0.diskHealth == .warn || $0.diskHealth == .bad }.count
        let memory = live.filter { $0.faultMemoryHealth == .warn || $0.faultMemoryHealth == .bad }.count
        let stalled = batches.filter { $0.stalled == true }.count
        if refused > 0 { out.append(plural(refused, "credential refused", "credentials refused")) }
        if offline > 0 { out.append(plural(offline, "node offline", "nodes offline")) }
        if needsAttention > 0 { out.append(plural(needsAttention, "node needs attention", "nodes need attention")) }
        if behind > 0 { out.append(plural(behind, "Mac behind on Studio Hub", "Macs behind on Studio Hub")) }
        if disks > 0 { out.append(plural(disks, "node low on disk", "nodes low on disk")) }
        if memory > 0 { out.append(plural(memory, "node low on memory", "nodes low on memory")) }
        if startupOff > 0 { out.append(plural(startupOff, "node will not auto-start", "nodes will not auto-start")) }
        if stalled > 0 { out.append(plural(stalled, "job stalled", "jobs stalled")) }
        if location.drained == true { out.append("drained") }
        // Never an empty reason on an amber group: the Gateway's own first warning, in its words.
        if out.isEmpty, health != .ok, let first = FleetWarnings.group(issues: issues).first {
            out.append(first.text)
        }
        return out
    }

    /// "answered in 38 ms", or how long ago a down group last answered.
    var lastAnswer: String {
        if isDown { return location.unreachableSince.map { "down since " + FleetFormat.relative($0).replacingOccurrences(of: " ago", with: "") } ?? "not answering" }
        return location.latencyMs.map { "answered in \($0) ms" } ?? "answered just now"
    }

    /// What a node is doing. The Hub states `working` and which studios are busy; which job is stated only
    /// when it reports `current` (Gateway `machines[].current`). A group with exactly one working node and
    /// one running batch is unambiguous, so that batch is named too.
    func doing(_ machine: FleetMachine) -> (headline: String, detail: String?) {
        let state = (machine.state ?? "").lowercased()
        // The State badge already says ready, idle, offline or refused; this column is only for work.
        if machine.nodeStateIsUnauthorized { return ("—", nil) }
        // A workstation the shop has for now: why, in the Hub's words ("Someone is using this Mac").
        if machine.liteModeResting { return (machine.workstationReasonText ?? "—", nil) }
        switch state {
        case "working":
            let busy = location.workers.filter { $0.machine == machine.machine && $0.busy == true }
                .compactMap { $0.modality }
            let studios = machine.current?.studio.map { [$0] } ?? busy
            let headline = "Working" + (studios.isEmpty ? "" : " · " + studios.joined(separator: ", "))
            func short(_ model: String?) -> String? { model.map { $0.split(separator: "/").last.map(String.init) ?? $0 } }
            var name: String?
            var since: Double?
            if let current = machine.current {
                name = current.label ?? short(current.model)
                since = current.startedAt
            } else {
                let working = machines.filter { ($0.state ?? "").lowercased() == "working" }
                if working.count == 1, let batch = batches.first(where: { ($0.running ?? 0) > 0 }) {
                    name = batch.label ?? short(batch.model)
                    since = batch.startedAt
                }
            }
            guard let name else { return (headline, nil) }
            let elapsed = FleetFormat.elapsed(since.map { Date().timeIntervalSince1970 - $0 })
            return (headline, elapsed.map { "\(name) · \($0)" } ?? name)
        case "ready", "long_idle", "offline", "just_finished": return ("—", nil)
        // The Gateway's few words for *why*: "Voice studio not running", "Last job failed: …".
        case "needs_attention": return (machine.attention ?? "Needs attention", nil)
        default: return (FleetFormat.words(machine.state), nil)
        }
    }

    func stateWord(_ machine: FleetMachine) -> String { machine.stateLabel }

    /// Machines worst first, the controller's own Mac before them: what a person scans for.
    var rankedMachines: [FleetMachine] {
        func rank(_ machine: FleetMachine) -> Int {
            let states = [nodeHealth(machine), machine.diskHealth, machine.faultMemoryHealth].filter { $0 == .warn || $0 == .bad }
            return nodeHealth(machine) == .bad ? 3 : (states.isEmpty ? 0 : 1)
        }
        return machines.sorted {
            if !$0.isNode != !$1.isNode { return !$0.isNode }
            if rank($0) != rank($1) { return rank($0) > rank($1) }
            return ($0.label ?? $0.machine ?? "") < ($1.label ?? $1.machine ?? "")
        }
    }

    /// Does this group (or one of its nodes) match a search?
    func matches(_ query: String) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return true }
        return nameMatches(needle) || machines.contains { machineMatches($0, needle) }
            || location.nodes.contains { ($0.name ?? $0.id ?? "").lowercased().contains(needle) }
    }

    func nameMatches(_ needle: String) -> Bool { name.lowercased().contains(needle) }

    func machineMatches(_ machine: FleetMachine, _ needle: String) -> Bool {
        [machine.label, machine.machine, machine.chip].contains { ($0 ?? "").lowercased().contains(needle) }
    }
}

extension FleetSnapshot {
    /// One summary per location, problems first (worst first), then A to Z. The local Mac is just a group.
    var groups: [GroupSummary] {
        locations.map { location in
            GroupSummary(
                location: location,
                machines: machines.filter { $0.locationId == location.id },
                batches: running.filter { $0.locationId == location.id },
                issues: issues.filter { $0.locationId == location.id })
        }.sorted {
            if $0.health != $1.health { return $0.health > $1.health }
            return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }
}

// MARK: - Menu helper

/// A menu item whose action is a closure (it is its own target, so it lives as long as the menu).
final class ClosureMenuItem: NSMenuItem {
    private let handler: () -> Void
    init(_ title: String, enabled: Bool = true, _ handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: "")
        target = self
        isEnabled = enabled
    }
    required init(coder: NSCoder) { fatalError("not used") }
    @objc private func fire() { handler() }
}

// MARK: - Page

/// The first Fleet tab. The shell hands it the snapshot; it owns its own toolbar (filter, search, refresh)
/// because the shell's location/search row means nothing here.
final class FleetGroupsTab: NSView, NSSearchFieldDelegate {
    // Actions the shell wires to its existing requests (the Locations tab's, moved into the card menu).
    var onRefresh: (() -> Void)?
    var onRefreshGroup: ((FleetLocation) -> Void)?
    var onTest: ((FleetLocation) -> Void)?
    var onResume: ((FleetLocation) -> Void)?
    var onPause: ((FleetLocation) -> Void)?
    var onChangeAddress: ((FleetLocation) -> Void)?
    var onRename: ((FleetLocation) -> Void)?
    var onCredentials: ((FleetLocation) -> Void)?
    var onRemove: ((FleetLocation) -> Void)?
    /// The bulk "Enable Auto-Start" (FleetStartup.swift): given every machine the snapshot lists.
    var onEnableAutostart: (([FleetMachine]) -> Void)?
    /// The per-node "…" startup menu, injected by the shell (FleetStartup.swift): nil when nothing can be sent.
    var startupMenu: ((FleetMachine) -> NSView?)?

    private var snapshot: FleetSnapshot?
    private var groups: [GroupSummary] = []
    private var problemsOnly = false
    private var query = ""
    private var expanded: Set<String> = []
    private var seeded = false
    private var showingAll: Set<String> = []
    private var error: String?
    private var compact = false
    /// The Version column needs a wide pane (it costs ~130 pt); below that the version rides under the node name.
    private var showVersion = false

    private let scroll = NSScrollView()
    private let body = NSStackView()
    private let strip = NSStackView()      // an error / stale-data line
    private let bannerHolder = NSStackView()
    private let summaryHolder = NSStackView()
    private let cardsHolder = NSStackView()
    private let filterControl = NSSegmentedControl()
    private let search = NSSearchField()
    private let updated: NSTextField = {
        let label = PageKit.label("", .caption, colour: .secondaryLabelColor)
        label.lineBreakMode = .byTruncatingTail
        return label
    }()

    private static let compactBelow: CGFloat = 760
    private static let versionColumnFrom: CGFloat = 1000
    private static let rowLimit = 40

    override init(frame: NSRect) {
        super.init(frame: frame)
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false

        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = PageKit.blockSpacing
        body.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: PageKit.pagePadding,
                                       bottom: PageKit.pagePadding, right: PageKit.pagePadding)
        body.translatesAutoresizingMaskIntoConstraints = false
        body.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        document.addSubview(body)
        addSubview(scroll)

        filterControl.segmentCount = 2
        filterControl.trackingMode = .selectOne
        filterControl.controlSize = .small
        filterControl.target = self
        filterControl.action = #selector(filterChanged)
        search.placeholderString = "Find a group or node"
        search.controlSize = .small
        search.delegate = self
        // 220 when there is room, never more than the pane can spare: a window at its minimum width must not grow.
        PageKit.softWidth(search, 220, min: 120)
        let refresh = PageKit.button("Refresh", target: self, action: #selector(refreshPressed))
        let toolbar = PageKit.toolbar([filterControl, search, NSView(), updated, refresh])

        for stack in [strip, bannerHolder, summaryHolder, cardsHolder] {
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.m
        }
        cardsHolder.spacing = DS.Space.m
        for view in [strip, bannerHolder, toolbar, summaryHolder, cardsHolder] {
            body.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: body.widthAnchor, constant: -2 * PageKit.pagePadding).isActive = true
        }
        body.setCustomSpacing(PageKit.blockSpacing, after: cardsHolder)

        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor), scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor), scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            body.topAnchor.constraint(equalTo: document.topAnchor), body.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            body.leadingAnchor.constraint(equalTo: document.leadingAnchor), body.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
        ])
        cardsHolder.addArrangedSubview(PageKit.loading("Reading the fleet…"))
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// A narrow pane drops the column that matters least ("Doing now") and the Jobs and Memory header figures.
    override func layout() {
        super.layout()
        let narrow = bounds.width - 2 * PageKit.pagePadding < Self.compactBelow
        let roomy = bounds.width - 2 * PageKit.pagePadding >= Self.versionColumnFrom
        if roomy != showVersion, snapshot != nil {
            showVersion = roomy
            rebuild()
        }
        if narrow != compact, snapshot != nil {
            compact = narrow
            rebuild()
        }
    }

    // MARK: Data

    func update(snapshot: FleetSnapshot) {
        self.snapshot = snapshot
        error = nil
        groups = snapshot.groups
        // Problem groups open, healthy ones collapsed, the first time only: after that the person's own choice stands.
        if !seeded {
            expanded = Set(groups.filter { $0.needsLook }.map { $0.id })
            if expanded.isEmpty, groups.count == 1 { expanded = Set(groups.map { $0.id }) }
            seeded = true
        }
        setStatus(snapshot.generatedAt.map { "Updated \(FleetFormat.relative($0))" } ?? "")
        // `layout()` only reacts to a change of width, so the first data has to read the width itself.
        if bounds.width > 0 { showVersion = bounds.width - 2 * PageKit.pagePadding >= Self.versionColumnFrom }
        rebuild()
    }

    /// The read failed. Old data stays (with an amber strip saying how old); with none, a red card says why.
    func showError(_ message: String) {
        error = message
        rebuild()
    }

    /// The shell's one-line status ("Refreshing 3 locations…", "Refreshed 3 locations.").
    func setStatus(_ text: String, colour: NSColor = .secondaryLabelColor) {
        updated.stringValue = text
        updated.textColor = colour
    }

    func setProblemsOnly(_ value: Bool) {
        problemsOnly = value
        rebuild()
    }

    @objc private func filterChanged() {
        problemsOnly = filterControl.selectedSegment == 1
        rebuild()
    }

    @objc private func refreshPressed() { onRefresh?() }

    func controlTextDidChange(_ notification: Notification) {
        query = search.stringValue
        rebuild()
    }

    private func clear(_ stack: NSStackView) { stack.arrangedSubviews.forEach { $0.removeFromSuperview() } }

    private func full(_ view: NSView, in stack: NSStackView) {
        stack.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func rebuild() {
        clear(strip); clear(bannerHolder); clear(summaryHolder); clear(cardsHolder)
        let attention = groups.filter { $0.needsLook }
        filterControl.setLabel("All groups (\(groups.count))", forSegment: 0)
        filterControl.setLabel("Needs attention (\(attention.count))", forSegment: 1)
        filterControl.selectedSegment = problemsOnly ? 1 : 0

        if let error {
            if snapshot == nil || groups.isEmpty {
                full(errorCard(error), in: cardsHolder)
                return
            }
            full(staleStrip(error), in: strip)
        }
        guard snapshot != nil else {
            cardsHolder.addArrangedSubview(PageKit.loading("Reading the fleet…"))
            return
        }
        guard !groups.isEmpty else {
            cardsHolder.addArrangedSubview(PageKit.empty(
                "No groups yet",
                "Add a location on the Locations tab and its controller group, with every Mac joined to it, appears here.",
                symbol: "network"))
            return
        }
        if !attention.isEmpty { full(banner(attention), in: bannerHolder) }
        full(summaryStrip(), in: summaryHolder)

        var shown = problemsOnly ? attention : groups
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        if !needle.isEmpty { shown = shown.filter { $0.matches(needle) } }
        for group in shown { full(card(group, needle: needle), in: cardsHolder) }
        if shown.isEmpty {
            cardsHolder.addArrangedSubview(!needle.isEmpty
                ? PageKit.empty("Nothing matches “\(query)”", "Search finds a group by name, or a node by name, chip or id.", symbol: "magnifyingglass")
                : PageKit.empty("Every group is healthy", "Nothing needs attention right now. Switch to All Groups to see every node.", symbol: "checkmark.seal"))
        }
    }

    // MARK: Pieces

    private func errorCard(_ message: String) -> NSView {
        let card = CardView()
        card.tint = Health.bad.tint
        let icon = NSImageView(image: NSImage(systemSymbolName: Health.bad.symbol, accessibilityDescription: nil)!)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 16, weight: .semibold)
        icon.contentTintColor = Health.bad.text
        let text = NSStackView(views: [PageKit.label("Could not read the fleet", .rowTitle),
                                       PageKit.label(message, .secondary, colour: Health.bad.text, wrapping: true)])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = DS.Space.xs
        let retry = PageKit.button("Retry", target: self, action: #selector(refreshPressed))
        let row = NSStackView(views: [icon, text, NSView(), retry])
        row.alignment = .top
        row.spacing = DS.Space.m
        card.hold(row)
        return card
    }

    private func staleStrip(_ message: String) -> NSView {
        let card = CardView()
        card.tint = Health.warn.tint
        let age = snapshot?.generatedAt.map { "Showing data from \(FleetFormat.relative($0)). " } ?? ""
        let text = PageKit.label(age + message, .secondary, colour: Health.warn.text, wrapping: true)
        let retry = PageKit.button("Retry", target: self, action: #selector(refreshPressed))
        let row = NSStackView(views: [text, NSView(), retry])
        row.alignment = .centerY
        row.spacing = DS.Space.m
        card.hold(row, inset: NSEdgeInsets(top: DS.Space.s, left: DS.Space.l, bottom: DS.Space.s, right: DS.Space.l))
        return card
    }

    private func banner(_ groups: [GroupSummary]) -> NSView {
        let panel = CardView()
        let worst = groups.map { $0.health }.max() ?? .warn
        panel.tint = worst.tint
        let icon = NSImageView(image: NSImage(systemSymbolName: worst.symbol, accessibilityDescription: nil)!)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 16, weight: .semibold)
        icon.contentTintColor = worst.text

        let title = PageKit.label("\(groups.count) of \(self.groups.count) group\(self.groups.count == 1 ? "" : "s") need\(groups.count == 1 ? "s" : "") attention", .rowTitle)
        var lines: [NSView] = []
        for group in groups {
            let reason = group.problems.joined(separator: " · ")
            let badge = Badge(group.name, group.health)
            let cell = NSView()
            badge.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(badge)
            NSLayoutConstraint.activate([
                badge.leadingAnchor.constraint(equalTo: cell.leadingAnchor), badge.topAnchor.constraint(equalTo: cell.topAnchor),
                badge.bottomAnchor.constraint(equalTo: cell.bottomAnchor), badge.trailingAnchor.constraint(lessThanOrEqualTo: cell.trailingAnchor),
                cell.widthAnchor.constraint(equalToConstant: 112),
            ])
            let text = PageKit.label(reason.isEmpty ? "Needs a look" : reason, .secondary, wrapping: true)
            text.maximumNumberOfLines = 3
            text.preferredMaxLayoutWidth = 0   // wrap at the width the row actually has
            let row = NSStackView(views: [cell, text])
            row.alignment = .centerY
            row.spacing = DS.Space.s
            lines.append(row)
        }

        // The bulk fix the owner asked for: Macs that will not bring their services up after a restart.
        let everyMachine = snapshot?.machines ?? []
        let off = FleetStartup.offCount(everyMachine)
        let offAway = FleetStartup.offUnreachableCount(everyMachine)
        var actions: [NSView] = [PageKit.button(problemsOnly ? "Show All Groups" : "Show Problems Only", target: self, action: #selector(toggleProblems), regular: true)]
        if off + offAway > 0 {
            let sentence = "\(off + offAway) Mac\(off + offAway == 1 ? "" : "s") will not start their services after a restart"
                + (offAway > 0 ? " (\(offAway) not answering now)" : "")
            let line = PageKit.label(sentence, .secondary, colour: Health.warn.text, wrapping: true)
            line.maximumNumberOfLines = 2
            line.preferredMaxLayoutWidth = 0
            lines.append(line)
            let enable = PageKit.primaryButton("Enable Auto-Start", target: self, action: #selector(enableAutostart), regular: true)
            enable.isEnabled = off > 0 && onEnableAutostart != nil
            enable.toolTip = off > 0 ? "Turns on \"Start services on startup\" for the \(off) Mac\(off == 1 ? "" : "s") that answer now. Nothing is restarted."
                : "Those Macs are not answering; a Mac with no Studio Hub running needs its services started by hand once."
            actions.insert(enable, at: 0)
        }
        // However many groups need attention, the banner takes at most ~5 lines and scrolls past that, so the
        // cards below keep the page.
        let list = NSStackView(views: lines)
        list.orientation = .vertical
        list.alignment = .leading
        list.spacing = DS.Space.tight
        for line in lines { line.widthAnchor.constraint(equalTo: list.widthAnchor).isActive = true }
        let clamp = FleetClampedScroll(maxHeight: FleetClampedScroll.groupsHeight)
        clamp.setContent(list)
        let text = NSStackView(views: [title, clamp])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = DS.Space.tight
        clamp.widthAnchor.constraint(equalTo: text.widthAnchor).isActive = true
        let buttons = NSStackView(views: actions)
        buttons.orientation = .vertical
        buttons.alignment = .trailing
        buttons.spacing = DS.Space.s
        // No spacer: the text column takes every point the icon and buttons leave, so its rows can wrap.
        text.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let row = NSStackView(views: [icon, text, buttons])
        row.alignment = .top
        row.spacing = DS.Space.m
        panel.hold(row)
        return panel
    }

    @objc private func toggleProblems() { setProblemsOnly(!problemsOnly) }

    @objc private func enableAutostart() { onEnableAutostart?(snapshot?.machines ?? []) }

    /// Four inline figures instead of six coloured tiles: colour is reserved for state, never decoration.
    private func summaryStrip() -> NSView {
        let nodes = groups.reduce(0) { $0 + $1.machines.count }
        let online = groups.reduce(0) { $0 + $1.online }
        let up = groups.filter { !$0.isDown }.count
        let running = groups.reduce(0) { $0 + $1.running }
        let queued = groups.reduce(0) { $0 + $1.queued }
        let worst = groups.compactMap { $0.worstDisk }.min { ($0.diskFree ?? 0) < ($1.diskFree ?? 0) }
        let summary = snapshot?.summary

        func stat(_ value: String, _ caption: String, _ health: Health = .ok) -> NSView {
            let figure = PageKit.label(value, .figure, colour: health == .ok || health == .off ? .labelColor : health.text)
            let text = PageKit.label(caption, .caption, colour: .secondaryLabelColor)
            text.lineBreakMode = .byTruncatingTail
            figure.lineBreakMode = .byTruncatingTail
            let stack = NSStackView(views: [figure, text])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 0
            return stack
        }
        let panel = CardView()
        let row = NSStackView(views: [
            stat("\(up)/\(groups.count)", "groups reachable", up == groups.count ? .ok : .bad),
            stat("\(online)/\(nodes)", "nodes online", online == nodes ? .ok : .warn),
            stat("\(running)", "jobs running · \(queued) queued"),
            Self.onLatestStat(summary, stat),
            stat(worst.map { FleetFormat.gb($0.diskFree) } ?? "—",
                 "least free disk" + (worst.map { " · " + ($0.label ?? $0.machine ?? "") } ?? ""),
                 worst.map { $0.diskHealth } ?? .off),
        ])
        row.distribution = .fillEqually
        row.spacing = DS.Space.xl
        panel.hold(row)
        return panel
    }

    /// "On latest 39/41 · Studio Hub 0.6.132": the fleet's version tile, amber while any Mac is behind.
    static func onLatestStat(_ summary: FleetSummary?, _ stat: (String, String, Health) -> NSView) -> NSView {
        guard let summary, let latest = summary.hubLatest else {
            return stat("—", "on latest · nothing published", .off)
        }
        let known = summary.versionKnown ?? 0
        let on = summary.onLatest ?? 0
        var caption = "on Hub \(latest)"
        if let updating = summary.hubUpdating, updating > 0 { caption += " · \(updating) updating" }
        let health: Health = (summary.hubBehind ?? 0) > 0 ? .warn : .ok
        return stat("\(on)/\(known)", caption, health)
    }

    // MARK: Group card

    /// Column widths shared by the header row and every node row, so figures line up under their titles.
    private struct Column {
        let title: String
        let width: CGFloat      // 0 = the flexible first column
        let wide: Bool          // dropped when the pane is narrow
        var grows = false       // takes an equal share of the spare width, with the first column
    }
    private var columns: [Column] {
        [Column(title: "Node", width: 0, wide: false),
         Column(title: "State", width: 124, wide: false),
         Column(title: "Version", width: 124, wide: true),
         Column(title: "Doing now", width: 132, wide: true, grows: true),
         Column(title: "Disk", width: 96, wide: false),
         Column(title: "Memory", width: 92, wide: false),
         Column(title: "Startup", width: 108, wide: false)]
    }
    private var activeColumns: [Column] {
        guard compact else { return columns.filter { $0.title != "Version" || showVersion } }
        // Narrow pane: drop "Doing now" and "Version" (the version then rides under the node name) and tighten
        // the rest so a window at its 860 pt minimum does not grow.
        return columns.filter { !$0.wide }.map { column in
            var tight = column
            switch column.title {
            case "State": tight = Column(title: column.title, width: 112, wide: false)
            case "Disk": tight = Column(title: column.title, width: 88, wide: false)
            case "Memory": tight = Column(title: column.title, width: 84, wide: false)
            case "Startup": tight = Column(title: column.title, width: 104, wide: false)
            default: break
            }
            return tight
        }
    }
    private static let actionsWidth: CGFloat = 28

    private func card(_ group: GroupSummary, needle: String) -> NSView {
        let panel = CardView()
        // A search that names a node (not the group) opens the group so the match is visible.
        let searching = !needle.isEmpty && !group.nameMatches(needle)
        let isOpen = expanded.contains(group.id) || searching

        let content = NSStackView(views: [header(group, open: isOpen)])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 0
        content.views.first!.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true

        if isOpen {
            func add(_ view: NSView, inset: CGFloat = 0) {
                content.addArrangedSubview(view)
                view.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -2 * inset).isActive = true
            }
            func rule() { let line = NSBox(); line.boxType = .separator; add(line, inset: 0) }
            rule()
            let warnings = group.warnings.filter { !$0.text.isEmpty }
            if !warnings.isEmpty {
                let list = FleetWarningList(plain: true)
                list.set(warnings: warnings)
                add(list)
            }
            add(columnHeader())
            if group.showsRoster {
                for node in group.location.nodes { rule(); add(rosterRow(node, group)) }
            } else {
                var machines = group.rankedMachines
                if searching { machines = machines.filter { group.machineMatches($0, needle) } }
                let limit = showingAll.contains(group.id) ? machines.count : Self.rowLimit
                for machine in machines.prefix(limit) { rule(); add(nodeRow(machine, group)) }
                if machines.count > limit { rule(); add(moreRow(group, hidden: machines.count - limit)) }
                if machines.isEmpty {
                    rule()
                    add(textRow(group.location.nodes.isEmpty
                        ? "No nodes have joined this group yet."
                        : "The roster lists \(group.location.nodes.count) node\(group.location.nodes.count == 1 ? "" : "s"), but none reported yet.",
                                colour: .secondaryLabelColor))
                }
            }
            if group.showsRoster, let from = group.location.membersStaleSeconds, from > 0 {
                rule()
                add(textRow("This is the roster as it was when the group last answered. Nothing here is live.", colour: .secondaryLabelColor))
            }
        }
        panel.hold(content, inset: NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0))
        return panel
    }

    private func header(_ group: GroupSummary, open: Bool) -> NSView {
        let chevron = NSImageView(image: NSImage(systemSymbolName: open ? "chevron.down" : "chevron.right", accessibilityDescription: open ? "Collapse" : "Expand")!)
        chevron.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 11, weight: .semibold)
        chevron.contentTintColor = .secondaryLabelColor
        chevron.widthAnchor.constraint(equalToConstant: DS.Space.l).isActive = true

        let badge = Badge(group.stateWord, group.health, symbol: true)
        let badgeCell = NSView()
        badge.translatesAutoresizingMaskIntoConstraints = false
        badgeCell.addSubview(badge)
        NSLayoutConstraint.activate([
            badge.leadingAnchor.constraint(equalTo: badgeCell.leadingAnchor), badge.topAnchor.constraint(equalTo: badgeCell.topAnchor),
            badge.bottomAnchor.constraint(equalTo: badgeCell.bottomAnchor),
            badgeCell.widthAnchor.constraint(equalToConstant: 92),
        ])

        var titleViews: [NSView] = [PageKit.label(group.name, .rowTitle)]
        if group.location.local == true, group.name.lowercased() != "this mac" {
            titleViews.append(PageKit.label("this Mac", .caption, colour: .secondaryLabelColor))
        }
        let title = NSStackView(views: titleViews)
        title.spacing = DS.Space.s
        title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let problems = group.problems
        // "Hub 0.6.132 · Latest · 10 of 11 on latest · " (against the newest release this Gateway published;
        // amber while a Mac is behind), then the counted reasons.
        let sub = PageKit.label("", .secondary, wrapping: true)
        let statusColour = problems.isEmpty || group.isPausedByOwner ? NSColor.secondaryLabelColor
            : (group.health == .bad ? Health.bad.text : Health.warn.text)
        let line = NSMutableAttributedString()
        if let version = group.versionLine {
            let colour = version.health == .warn || version.health == .bad ? version.health.text
                : version.health == .info ? Health.info.text : NSColor.secondaryLabelColor
            line.append(NSAttributedString(string: version.text + " · ",
                                           attributes: [.font: TextRole.secondary.font, .foregroundColor: colour]))
        }
        var words = problems.isEmpty ? "\(group.nodeCount) node\(group.nodeCount == 1 ? "" : "s") · \(group.lastAnswer)" : problems.joined(separator: " · ")
        // The group's own network, last, only when one is set (Macs that differ say so on their rows).
        if let network = group.location.network, !network.isEmpty { words += " · Network \(network)" }
        line.append(NSAttributedString(string: words, attributes: [.font: TextRole.secondary.font, .foregroundColor: statusColour]))
        sub.attributedStringValue = line
        sub.maximumNumberOfLines = 2
        // Pinned to the card's width below, so it wraps there rather than at the label default.
        sub.preferredMaxLayoutWidth = 0

        func metric(_ caption: String, _ value: NSView, width: CGFloat) -> NSView {
            let stack = NSStackView(views: [PageKit.label(caption, .caption, colour: .secondaryLabelColor), value])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = DS.Space.xs
            stack.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            self.pinned(stack, width)
            return stack
        }
        func plain(_ text: String, _ colour: NSColor = .labelColor) -> NSView { PageKit.label(text, .secondary, colour: colour) }
        func tiered(_ machine: FleetMachine?, _ health: Health?, _ text: String?) -> NSView {
            guard let text, let health else { return plain("—", .secondaryLabelColor) }
            return health == .ok || health == .off ? plain(text) : Badge(text, health)
        }

        let offLite = group.offLite > 0 ? " · \(group.offLite) off (Lite Mode)" : ""
        let nodesValue = plain(group.isDown ? "\(group.location.nodes.count) known" : "\(group.online) of \(group.machines.count) online" + offLite,
                               group.isDown || group.online + group.offLite < group.machines.count ? Health.warn.text : .labelColor)
        let jobsValue = plain(group.isDown ? "—" : "\(group.running) running · \(group.queued) queued")
        let worstDisk = group.worstDisk
        let diskValue = group.isDown ? plain("—", .secondaryLabelColor)
            : tiered(worstDisk, worstDisk?.diskHealth, worstDisk.map { "\(FleetFormat.gb($0.diskFree)) free" })
        let worstMemory = group.worstMemory
        let memoryValue = group.isDown ? plain("—", .secondaryLabelColor)
            : tiered(worstMemory, worstMemory?.memoryHealth, worstMemory.map { "\(FleetFormat.gb($0.availableGb)) free" })

        var views: [NSView] = [chevron, badgeCell, title, NSView(), metric("Nodes", nodesValue, width: 100)]
        if !compact { views.append(metric("Jobs", jobsValue, width: 128)) }
        views.append(metric("Disk (worst)", diskValue, width: 100))
        if !compact { views.append(metric("Memory (worst)", memoryValue, width: 108)) }
        views.append(actionsButton(group))

        let row = NSStackView(views: views)
        row.spacing = DS.Space.m
        row.alignment = .centerY
        // The sentence about what is wrong sits under the name, across the full card width.
        let nameInset = DS.Space.l + DS.Space.l + DS.Space.m + 92 + DS.Space.m
        let subRow = NSView()
        sub.translatesAutoresizingMaskIntoConstraints = false
        subRow.addSubview(sub)
        NSLayoutConstraint.activate([
            sub.trailingAnchor.constraint(equalTo: subRow.trailingAnchor, constant: -DS.Space.l),
            sub.topAnchor.constraint(equalTo: subRow.topAnchor),
            sub.bottomAnchor.constraint(equalTo: subRow.bottomAnchor),
        ])
        let stacked = NSStackView(views: [row, subRow])
        stacked.orientation = .vertical
        stacked.alignment = .leading
        stacked.spacing = DS.Space.xs
        stacked.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l)
        row.widthAnchor.constraint(equalTo: stacked.widthAnchor, constant: -2 * DS.Space.l).isActive = true
        subRow.widthAnchor.constraint(equalTo: stacked.widthAnchor, constant: -2 * DS.Space.l).isActive = true
        // `nameInset` was measured from the card edge, and the stack already insets by `l`.
        sub.leadingAnchor.constraint(equalTo: subRow.leadingAnchor, constant: nameInset - DS.Space.l).isActive = true
        let click = NSClickGestureRecognizer(target: self, action: #selector(toggleCard(_:)))
        let holder = HeaderRow(id: group.id)
        holder.addGestureRecognizer(click)
        holder.hold(stacked)
        return holder
    }

    /// The header strip carries the id of its group, so a click anywhere on it (but the menu) toggles it.
    private final class HeaderRow: NSView {
        let id: String
        init(id: String) { self.id = id; super.init(frame: .zero) }
        required init?(coder: NSCoder) { fatalError("not used") }
        func hold(_ content: NSView) {
            content.translatesAutoresizingMaskIntoConstraints = false
            addSubview(content)
            NSLayoutConstraint.activate([
                content.topAnchor.constraint(equalTo: topAnchor), content.bottomAnchor.constraint(equalTo: bottomAnchor),
                content.leadingAnchor.constraint(equalTo: leadingAnchor), content.trailingAnchor.constraint(equalTo: trailingAnchor),
            ])
        }
    }

    @objc private func toggleCard(_ recogniser: NSClickGestureRecognizer) {
        guard let id = (recogniser.view as? HeaderRow)?.id else { return }
        if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) }
        rebuild()
    }

    /// The group's actions: the Locations tab's, moved into the card.
    private func actionsButton(_ group: GroupSummary) -> NSView {
        let button = FleetMenuButton { [weak self] in
            guard let self else { return NSMenu() }
            let location = group.location
            let menu = NSMenu()
            menu.addItem(ClosureMenuItem("Test") { self.onTest?(location) })
            menu.addItem(ClosureMenuItem("Refresh Group") { self.onRefreshGroup?(location) })
            if location.isPausedByOwner {
                menu.addItem(ClosureMenuItem("Resume Group") { self.onResume?(location) })
            } else {
                if location.pausedAt != nil { menu.addItem(ClosureMenuItem("Resume") { self.onResume?(location) }) }
                menu.addItem(ClosureMenuItem("Pause Group") { self.onPause?(location) })
            }
            if location.local != true {
                menu.addItem(.separator())
                menu.addItem(ClosureMenuItem("Rename…") { self.onRename?(location) })
                menu.addItem(ClosureMenuItem("Credentials…") { self.onCredentials?(location) })
                menu.addItem(ClosureMenuItem("Change Address…") { self.onChangeAddress?(location) })
                menu.addItem(.separator())
                menu.addItem(ClosureMenuItem("Remove…") { self.onRemove?(location) })
            }
            return menu
        }
        button.toolTip = "Actions for \(group.name)"
        button.widthAnchor.constraint(equalToConstant: Self.actionsWidth).isActive = true
        return button
    }

    // MARK: Node rows

    /// One row of cells: a holder per column with a fixed width (the first one flexes), content pinned to the
    /// leading edge at its natural size, so a badge is never stretched to its column.
    private func rowStack(_ cells: [String: NSView], trailing: NSView? = nil) -> NSStackView {
        let row = NSStackView()
        row.spacing = DS.Space.s
        row.alignment = .centerY
        row.edgeInsets = NSEdgeInsets(top: DS.Space.s, left: DS.Space.l, bottom: DS.Space.s, right: DS.Space.l)
        var sharing: [NSView] = []
        for column in activeColumns {
            let holder = NSView()
            if let cell = cells[column.title] {
                cell.translatesAutoresizingMaskIntoConstraints = false
                holder.addSubview(cell)
                NSLayoutConstraint.activate([
                    cell.leadingAnchor.constraint(equalTo: holder.leadingAnchor),
                    cell.trailingAnchor.constraint(lessThanOrEqualTo: holder.trailingAnchor),
                    cell.topAnchor.constraint(equalTo: holder.topAnchor),
                    cell.bottomAnchor.constraint(equalTo: holder.bottomAnchor),
                ])
            }
            if column.grows {
                let atLeast = holder.widthAnchor.constraint(greaterThanOrEqualToConstant: column.width)
                atLeast.priority = Self.widthPriority(compact)
                atLeast.isActive = true
                holder.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                sharing.append(holder)
            } else if column.width > 0 {
                pinned(holder, column.width)
            } else {
                let atLeast = holder.widthAnchor.constraint(greaterThanOrEqualToConstant: compact ? 72 : 96)
                atLeast.priority = Self.widthPriority(compact)
                atLeast.isActive = true
                holder.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                sharing.append(holder)
            }
            row.addArrangedSubview(holder)
        }
        // The node and "Doing now" columns split the spare width, so a wide window does not leave the
        // figures stranded at the right edge.
        if sharing.count == 2 {
            let equal = sharing[0].widthAnchor.constraint(equalTo: sharing[1].widthAnchor)
            equal.priority = NSLayoutConstraint.Priority(700)
            equal.isActive = true
        }
        let end = NSView()
        end.widthAnchor.constraint(equalToConstant: Self.actionsWidth).isActive = true
        if let trailing {
            trailing.translatesAutoresizingMaskIntoConstraints = false
            end.addSubview(trailing)
            NSLayoutConstraint.activate([
                trailing.trailingAnchor.constraint(equalTo: end.trailingAnchor),
                trailing.centerYAnchor.constraint(equalTo: end.centerYAnchor),
            ])
        }
        row.addArrangedSubview(end)
        return row
    }

    private func columnHeader() -> NSView {
        var titles: [String: NSView] = [:]
        for column in activeColumns { titles[column.title] = PageKit.label(column.title, .captionStrong, colour: .secondaryLabelColor) }
        let row = rowStack(titles)
        row.edgeInsets.top = DS.Space.s
        row.edgeInsets.bottom = DS.Space.xs
        return row
    }

    /// **Why the wide layout's widths are soft.** A window grows to satisfy any required width, and `layout()`
    /// only drops to the compact layout once it sees a narrow pane, so required widths in the wide layout would
    /// stop the window ever getting narrow. Below `NSLayoutPriorityDragThatCanResizeWindow` (510) they are a
    /// preference only; the compact layout, which fits the 860 pt minimum, uses them as hard widths.
    private static func widthPriority(_ compact: Bool) -> NSLayoutConstraint.Priority {
        compact ? .required : NSLayoutConstraint.Priority(400)
    }

    private func pinned(_ view: NSView, _ width: CGFloat) {
        let constraint = view.widthAnchor.constraint(equalToConstant: width)
        constraint.priority = Self.widthPriority(compact)
        constraint.isActive = true
    }

    private func dash() -> NSView { PageKit.label("—", .secondary, colour: .secondaryLabelColor) }

    private func meter(_ text: String, _ fraction: Double?, _ health: Health) -> NSView {
        guard let fraction else { return dash() }
        let value = PageKit.label(text, .secondary, colour: health == .ok || health == .off ? .labelColor : health.text)
        let bar = Meter(fraction: fraction, health: health)
        bar.widthAnchor.constraint(equalToConstant: 72).isActive = true
        let stack = NSStackView(views: [value, bar])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs
        return stack
    }

    private func nodeRow(_ machine: FleetMachine, _ group: GroupSummary) -> NSView {
        let node = group.location.nodes.first { $0.id == machine.machine }
        let health = group.nodeHealth(machine)
        let offline = machine.reachable == false

        // "Network A · Apple M4 · 16 GB" on a Mac whose network is not its group's (a shop iMac, 2026-10-05):
        // first, so a narrow Node column still shows it. The group's own network is on Locations.
        let ownNetwork = machine.network == group.location.network ? nil : machine.network
        var facts = ([ownNetwork.map { "Network \($0)" }] + [machine.hardwareText])
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        var factsColour = NSColor.secondaryLabelColor
        let latest = snapshot?.summary?.hubLatest
        let versionBadge = machine.versionBadge(latest: latest)
        // No Version column (a pane under ~1000 pt): the version rides under the name, amber when behind.
        if !showVersion || compact, let version = machine.hubVersion, machine.versionStatus != "unknown" {
            let word = versionBadge.map { $0.text.hasPrefix("Behind") ? "behind" : $0.text.lowercased() }
            facts += (facts.isEmpty ? "" : " · ") + "Hub \(version)" + (word.map { " \($0)" } ?? "")
            if let badge = versionBadge, badge.health == .warn || badge.health == .info { factsColour = badge.health.text }
        }
        if !machine.isNode { facts += (facts.isEmpty ? "" : " · ") + "controller" }
        var nameViews: [NSView] = [PageKit.label(machine.label ?? machine.machine ?? "node", .rowTitle),
                                   PageKit.label(facts, .caption, colour: factsColour)]
        // Workstation mode: "[Working] Today: 3 jobs · 1 step-aside" (a resting Mac's chip is its State badge).
        if let workstation = machine.workstation, let today = workstation.today {
            var parts: [NSView] = []
            // Offline: its last Lite Mode state is not now (the waiting change, if any, is the note row below).
            if machine.liteModeOffline {
                parts.append(Badge("Offline", .off))
            } else {
                if !workstation.isResting, let chip = workstation.chip { parts.append(Badge(chip, workstation.chipHealth)) }
                parts.append(PageKit.label(today, .caption, colour: .secondaryLabelColor))
            }
            let line = NSStackView(views: parts)
            line.spacing = DS.Space.xs
            line.alignment = .centerY
            line.toolTip = machine.workstationLine
            nameViews.append(line)
        }
        let name = stack(nameViews)
        name.toolTip = machine.stateReason ?? machine.limitation
        // Why it is offline, in plain English; the controller's raw sentence still shows for any other fault.
        let offlineWhy = FleetOffline.why(machine)

        // The state badge, and under it when the controller last heard from this Mac (a node only).
        let stateBadge = Badge(group.stateWord(machine), machine.badgeHealth)
        var stateViews: [NSView] = [stateBadge]
        if machine.isNode { stateViews.append(PageKit.label("seen \(machine.nodeLastSeenText)", .caption, colour: .secondaryLabelColor)) }
        let state = stack(stateViews)
        let stateTip = [machine.workstationLine, machine.stateReason, FleetOffline.tooltip(machine)].compactMap { $0 }
            .joined(separator: "\n\n")
        state.toolTip = stateTip.isEmpty ? nil : stateTip

        let (headline, detail) = group.doing(machine)
        var doingViews = [PageKit.label(headline, .secondary,
                                        colour: machine.badgeHealth == .bad || machine.badgeHealth == .warn ? machine.badgeHealth.text : .labelColor)]
        if let detail { doingViews.append(PageKit.label(detail, .caption, colour: .secondaryLabelColor)) }
        let doing = stack(doingViews)
        doing.toolTip = detail

        let diskCell = offline ? dash() : meter("\(FleetFormat.gb(machine.diskFree)) free", machine.diskUsedFraction, machine.diskHealth)
        diskCell.toolTip = Self.diskTooltip(machine)
        let memoryCell = offline ? dash() : meter("\(FleetFormat.gb(machine.availableGb)) free", machine.memoryUsedFraction, machine.memoryHealth)
        memoryCell.toolTip = machine.memoryGb.map { "Total \(FleetFormat.gb($0)); free is what the Hub's own admission counts." }

        let (startupHealth, startupText) = FleetStartup.health(machine.startup)
        let startup = Badge(startupText, startupHealth)
        startup.toolTip = FleetStartup.tooltip(machine.startup, name: machine.label ?? machine.machine ?? "This Mac")

        let version = Self.versionCell(machine, versionBadge)

        let row = rowStack(["Node": name, "State": state, "Version": version, "Doing now": doing, "Disk": diskCell,
                            "Memory": memoryCell, "Startup": startup],
                          trailing: startupMenu?(machine))
        let sentence = ([offlineWhy] + [node?.detail ?? machine.nodeDetail]).compactMap { $0 }.first(where: { !$0.isEmpty && health == .bad })
        // A Lite Mode change saved while this Mac was offline: "Lite Mode: Waiting to apply (offline since 13:52)".
        // A switched-off Lite Mode Mac: one grey line, plus the Gateway's quiet note once it has been off a long time.
        let liteOff = machine.liteOff == true
        let longOff = group.issues.first { $0.code == "lite_mode_long_off" && $0.subject == machine.machine }?.message
        let notes = [sentence.map { noteRow(.bad, $0, caption: true) },
                     // **A thin Ethernet cable is a fault this row would otherwise look healthy under.**
                     // Amber, naming the speed and the fix; a Mac on Wi-Fi is not a fault and gets no line.
                     machine.connection?.slow == true
                        ? noteRow(.warn, "Slow network link — \(machine.connection?.display ?? "—") · check the cable or switch port",
                                  caption: true) : nil,
                     liteOff ? noteRow(.off, longOff ?? FleetOffline.liteOffNote, caption: true) : nil,
                     FleetUpdateFailure.rowLine(machine).map { noteRow(.warn, $0, caption: true) },
                     machine.liteModePending.map { noteRow(liteOff ? .off : .warn, "Lite Mode: \($0.line)", caption: true) }].compactMap { $0 }
        guard !notes.isEmpty else { return row }
        let wrapper = stack([row] + notes)
        wrapper.spacing = 0
        row.widthAnchor.constraint(equalTo: wrapper.widthAnchor).isActive = true
        return wrapper
    }

    /// The Version cell: the badge, and under it the version itself (a Behind badge already names it).
    static func versionCell(_ machine: FleetMachine, _ badge: (text: String, health: Health, tooltip: String)?) -> NSView {
        guard let badge else {
            return PageKit.label(machine.hubVersion ?? "—", .secondary,
                                 colour: machine.hubVersion == nil ? .secondaryLabelColor : .labelColor)
        }
        var views: [NSView] = [Badge(badge.text, badge.health)]
        if let version = machine.hubVersion, !badge.text.contains(version) {
            views.append(PageKit.label(version, .caption, colour: .secondaryLabelColor))
        }
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs / 2
        stack.toolTip = badge.tooltip
        return stack
    }

    /// What the Disk cell's tooltip adds: the volume, the Hub's own state and its largest consumers.
    private static func diskTooltip(_ machine: FleetMachine) -> String? {
        guard let disk = machine.disk else { return machine.diskTotal.map { "Volume: \(FleetFormat.gb($0))" } }
        var lines: [String] = []
        if let total = disk.totalGb { lines.append("Volume: \(FleetFormat.gb(total))") }
        if disk.stateSource == "derived" { lines.append("State worked out from free space; this Hub does not report one.") }
        if let top = disk.topText { lines.append("Largest: " + top) }
        if let trash = disk.trashGb { lines.append("Trash: " + FleetFormat.gb(trash)) }
        if let swap = disk.swapUsedGb { lines.append("Swap in use: " + FleetFormat.gb(swap)) }
        lines.append(contentsOf: disk.warnings)
        return lines.isEmpty ? nil : lines.joined(separator: "\n")
    }

    /// A node the controller still lists but this snapshot has no machine row for (a group that is down).
    private func rosterRow(_ node: FleetNode, _ group: GroupSummary) -> NSView {
        let name = stack([PageKit.label(node.name ?? node.id ?? "node", .rowTitle),
                          PageKit.label(node.address ?? "", .caption, colour: .secondaryLabelColor)])
        let last = PageKit.label("Last known: " + node.stateText, .caption, colour: .secondaryLabelColor, wrapping: true)
        last.maximumNumberOfLines = 2
        last.preferredMaxLayoutWidth = 124
        let seen = PageKit.label(node.lastSeenAt?.doubleValue == nil ? "never seen" : "seen \(node.lastSeenText)", .caption, colour: .secondaryLabelColor)
        return rowStack(["Node": name, "State": stack([Badge("Unknown", .off), seen]),
                         "Version": PageKit.label(node.version ?? "—", .secondary, colour: .secondaryLabelColor),
                         "Doing now": last, "Disk": dash(), "Memory": dash(), "Startup": dash()])
    }

    private func noteRow(_ health: Health, _ text: String, caption: Bool = false) -> NSView {
        let label = PageKit.label(text, caption ? .caption : .secondary, colour: health.text, wrapping: true)
        label.maximumNumberOfLines = 3
        label.preferredMaxLayoutWidth = 0
        return padded(label, top: caption ? 0 : DS.Space.s, bottom: caption ? DS.Space.s : 0)
    }

    private func textRow(_ text: String, colour: NSColor) -> NSView {
        let label = PageKit.label(text, .secondary, colour: colour, wrapping: true)
        label.preferredMaxLayoutWidth = 0
        return padded(label, top: DS.Space.m, bottom: DS.Space.m)
    }

    private func moreRow(_ group: GroupSummary, hidden: Int) -> NSView {
        let button = FleetClosureButton(title: "Show All \(hidden + Self.rowLimit) Nodes") { [weak self] in
            self?.showingAll.insert(group.id)
            self?.rebuild()
        }
        return padded(button, top: DS.Space.s, bottom: DS.Space.s)
    }

    private func padded(_ view: NSView, top: CGFloat, bottom: CGFloat) -> NSView {
        let holder = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        holder.addSubview(view)
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: holder.leadingAnchor, constant: DS.Space.l),
            view.trailingAnchor.constraint(lessThanOrEqualTo: holder.trailingAnchor, constant: -DS.Space.l),
            view.topAnchor.constraint(equalTo: holder.topAnchor, constant: top),
            view.bottomAnchor.constraint(equalTo: holder.bottomAnchor, constant: -bottom),
        ])
        return holder
    }

    private func stack(_ views: [NSView]) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.xs / 2
        return stack
    }
}

/// A small "…" button that pops a menu built on demand.
final class FleetMenuButton: NSButton {
    private let makeMenu: () -> NSMenu

    init(_ makeMenu: @escaping () -> NSMenu) {
        self.makeMenu = makeMenu
        super.init(frame: .zero)
        title = ""
        image = NSImage(systemSymbolName: "ellipsis", accessibilityDescription: "Actions")
        imagePosition = .imageOnly
        bezelStyle = .rounded
        controlSize = .small
        isBordered = false
        contentTintColor = .secondaryLabelColor
        target = self
        action = #selector(open)
        setContentHuggingPriority(.required, for: .horizontal)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    /// The titles the menu would show (for a layout test, which cannot click).
    var itemTitles: [String] { makeMenu().items.map { $0.title } }

    @objc private func open() {
        makeMenu().popUp(positioning: nil, at: NSPoint(x: 0, y: bounds.height + 2), in: self)
    }
}
