// A mock of the proposed Fleet > Groups tab, built from the real `FleetSnapshot` the app already decodes.
//
// Not shipped: it exists so the design in design/README.md can be looked at before anyone builds it, and so the
// implementation starts from working layout code rather than from a picture. The tokens in `DS` are the ones the
// spec proposes for both apps; the group/node derivations (`GroupSummary`) are the rules the spec states.
//
// Everything is derived from fields `/admin/api/fleet` serves today, except two that are marked `GAP`:
// the per-node `disk` object (feat/disk-guard + Hub feat/disk-governor) and which job a node is running.
import AppKit

// MARK: - Tokens

enum DS {
    enum Space {
        static let xs: CGFloat = 4, s: CGFloat = 8, m: CGFloat = 12, l: CGFloat = 16, xl: CGFloat = 24, page: CGFloat = 28
    }
    enum Radius { static let card: CGFloat = 8, control: CGFloat = 6 }
    enum Font {
        static let pageTitle = NSFont.systemFont(ofSize: 26, weight: .semibold)
        static let section = NSFont.systemFont(ofSize: 15, weight: .semibold)
        static let rowTitle = NSFont.systemFont(ofSize: 13, weight: .semibold)
        static let body = NSFont.systemFont(ofSize: 13)
        static let secondary = NSFont.systemFont(ofSize: 12)
        static let caption = NSFont.systemFont(ofSize: 11)
        static let captionStrong = NSFont.systemFont(ofSize: 11, weight: .semibold)
        static let mono = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        static let figure = NSFont.monospacedDigitSystemFont(ofSize: 22, weight: .semibold)
    }
}

/// The one status vocabulary. Green = fine, amber = look soon, red = act now, grey = idle or unknown.
enum Health: Int, Comparable {
    case ok, off, warn, bad
    static func < (a: Health, b: Health) -> Bool { a.rank < b.rank }
    /// Worst-of ordering: grey (idle/unknown) never outranks a real problem, but a problem outranks green.
    var rank: Int { switch self { case .ok: return 0; case .off: return 1; case .warn: return 2; case .bad: return 3 } }

    var tint: NSColor {
        switch self {
        case .ok: return .systemGreen
        case .warn: return .systemOrange
        case .bad: return .systemRed
        case .off: return .systemGray
        }
    }
    /// Text in a status colour. Plain `systemOrange` on a light card is ~2.3:1; blending toward the label colour
    /// darkens it in light mode and lightens it in dark mode, so one token reads in both.
    var text: NSColor {
        let base = tint
        return NSColor(name: nil) { appearance in
            let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return base.blended(withFraction: self == .off ? 0.2 : 0.4, of: dark ? .white : .black) ?? base
        }
    }
    var symbol: String {
        switch self {
        case .ok: return "checkmark.circle.fill"
        case .warn: return "exclamationmark.triangle.fill"
        case .bad: return "xmark.octagon.fill"
        case .off: return "moon.zzz.fill"
        }
    }
}

/// Disk and memory states share the Hub's disk governor tiers: amber < 12 % free, red < 6 %, stop < 5 GB.
enum Tier {
    static func disk(free: Double?, total: Double?) -> (Health, String) {
        guard let free, let total, total > 0 else { return (.off, "—") }
        let share = free / total
        if free < 5 { return (.bad, "stop") }
        if share < 0.06 { return (.bad, "red") }
        if share < 0.12 { return (.warn, "amber") }
        return (.ok, "ok")
    }
    /// ponytail: proposed thresholds, an owner decision. `available_gb` moves a lot on macOS, so amber is
    /// deliberately generous and red means "swapping is likely".
    static func memory(available: Double?, total: Double?) -> Health {
        guard let available, let total, total > 0 else { return .off }
        let share = available / total
        if available < 1.5 || share < 0.10 { return .bad }
        if share < 0.20 { return .warn }
        return .ok
    }
}

// MARK: - Small views

/// A rounded panel that re-resolves its colours with the appearance (see PageKit's CardView for why).
class Panel: NSView {
    var tint: NSColor?
    override init(frame: NSRect) { super.init(frame: frame); wantsLayer = true }
    required init?(coder: NSCoder) { fatalError() }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        if let tint {
            layer?.backgroundColor = tint.withAlphaComponent(0.10).cgColor
            layer?.borderColor = tint.withAlphaComponent(0.35).cgColor
        } else {
            layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
            layer?.borderColor = NSColor.separatorColor.cgColor
        }
        layer?.borderWidth = 1
        layer?.cornerRadius = DS.Radius.card
    }
}

/// A status badge: tinted capsule, 11 pt semibold, optional symbol. Same anatomy in both apps.
final class Badge: NSView {
    private let health: Health
    init(_ text: String, _ health: Health, symbol: Bool = false) {
        self.health = health
        super.init(frame: .zero)
        wantsLayer = true
        let label = NSTextField(labelWithString: text)
        label.font = DS.Font.captionStrong
        label.textColor = health.text
        label.lineBreakMode = .byTruncatingTail
        var views: [NSView] = []
        if symbol, let image = NSImage(systemSymbolName: health.symbol, accessibilityDescription: nil) {
            let icon = NSImageView(image: image)
            icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 10, weight: .semibold)
            icon.contentTintColor = health.text
            views.append(icon)
        }
        views.append(label)
        let stack = NSStackView(views: views)
        stack.spacing = 4
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
        natural = NSSize(width: stack.fittingSize.width + 16, height: 20)
        setContentHuggingPriority(.required, for: .horizontal)
        setContentHuggingPriority(.required, for: .vertical)
        setContentCompressionResistancePriority(.required, for: .horizontal)
    }
    private var natural = NSSize(width: 60, height: 20)
    override var intrinsicContentSize: NSSize { natural }
    required init?(coder: NSCoder) { fatalError() }
    override var wantsUpdateLayer: Bool { true }
    override func updateLayer() {
        layer?.backgroundColor = health.tint.withAlphaComponent(0.14).cgColor
        layer?.borderColor = health.tint.withAlphaComponent(0.35).cgColor
        layer?.borderWidth = 1
        layer?.cornerRadius = 10
    }
}

/// A thin usage bar. Drawn in `draw`, which always runs in the right appearance.
final class Meter: NSView {
    var fraction: CGFloat
    var health: Health
    init(fraction: Double, health: Health) {
        self.fraction = CGFloat(max(0, min(1, fraction)))
        self.health = health
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        heightAnchor.constraint(equalToConstant: 4).isActive = true
    }
    required init?(coder: NSCoder) { fatalError() }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 2, yRadius: 2).fill()
        health.tint.setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: bounds.width * fraction, height: bounds.height),
                     xRadius: 2, yRadius: 2).fill()
    }
}

private func label(_ text: String, _ font: NSFont, _ colour: NSColor = .labelColor, lines: Int = 1) -> NSTextField {
    let field = NSTextField(labelWithString: text)
    field.font = font
    field.textColor = colour
    field.lineBreakMode = .byTruncatingTail
    field.maximumNumberOfLines = lines
    field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return field
}

private func column(_ views: [NSView], spacing: CGFloat = 2) -> NSStackView {
    let stack = NSStackView(views: views)
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = spacing
    return stack
}

// MARK: - Derived group data

/// What the spec says a group card needs. Every field is a function of the snapshot.
struct GroupSummary {
    let location: FleetLocation
    let machines: [FleetMachine]
    let batches: [FleetJob]
    let issues: [FleetIssue]

    var name: String { location.label ?? location.id ?? "Location" }
    var isDown: Bool { location.reachable != true }
    var nodes: [FleetMachine] { machines }

    var online: Int { machines.filter { ($0.state ?? "").lowercased() != "offline" && $0.reachable != false }.count }
    var running: Int { batches.reduce(0) { $0 + ($1.running ?? 0) } }
    var queued: Int { batches.reduce(0) { $0 + ($1.queued ?? 0) } }

    func disk(_ machine: FleetMachine) -> (Health, String) { Tier.disk(free: machine.diskFreeGb, total: machine.diskTotalGb) }
    func memory(_ machine: FleetMachine) -> Health { Tier.memory(available: machine.availableGb, total: machine.memoryGb) }

    /// The node with the least disk, ranked by tier first and free space second. Offline nodes report none.
    var worstDisk: (machine: FleetMachine, health: Health)? {
        var best: (machine: FleetMachine, health: Health)?
        for machine in machines where machine.diskFreeGb != nil && machine.reachable != false {
            let health = disk(machine).0
            guard let current = best else { best = (machine, health); continue }
            if health > current.health
                || (health == current.health && (machine.diskFreeGb ?? 0) < (current.machine.diskFreeGb ?? 0)) {
                best = (machine, health)
            }
        }
        return best
    }
    var worstMemory: (machine: FleetMachine, health: Health)? {
        var best: (machine: FleetMachine, health: Health)?
        for machine in machines where machine.availableGb != nil && machine.reachable != false {
            let health = memory(machine)
            guard let current = best else { best = (machine, health); continue }
            if health > current.health { best = (machine, health) }
        }
        return best
    }

    func nodeHealth(_ machine: FleetMachine) -> Health {
        let state = (machine.state ?? "").lowercased()
        if machine.nodeStateIsUnauthorized || state == "offline" { return .bad }
        if state == "long_idle" || state.isEmpty { return .off }
        return .ok
    }

    /// Group health: down if unreachable; amber for anything a person should look at on a node or in a job; red only
    /// when the controller's own Mac is in trouble, because then the whole group cannot be trusted.
    var health: Health {
        if isDown { return .bad }
        var worst = Health.ok
        for machine in machines {
            for state in [nodeHealth(machine), disk(machine).0, memory(machine)] where state == .warn || state == .bad {
                worst = max(worst, .warn)
            }
        }
        if issues.contains(where: { $0.severity == "error" || $0.severity == "warning" }) { worst = max(worst, .warn) }
        if let controller = machines.first(where: { !$0.isNode }), disk(controller).0 == .bad { worst = .bad }
        return worst
    }

    /// The gateway's own sentences for what is wrong (never paraphrased), for the expanded body.
    var notes: [(Health, String)] {
        if isDown {
            return [(.bad, location.pauseReason ?? location.error ?? "This group is not answering.")]
        }
        return issues.filter { $0.severity == "error" || $0.severity == "warning" }
            .map { ($0.severity == "error" ? Health.bad : Health.warn, $0.message ?? "") }
    }

    /// Short phrases for the sub-line under the group name: counts, so a collapsed card still says what is wrong.
    var problems: [String] {
        if isDown {
            let since = location.unreachableSince.map { " · last answered \(FleetFormat.relative($0))" } ?? ""
            return [(location.pausedAt != nil ? "Paused, not answering" : "Not answering") + since]
        }
        var out: [String] = []
        let refused = machines.filter { $0.nodeStateIsUnauthorized }.count
        let offline = machines.filter { ($0.state ?? "").lowercased() == "offline" && !$0.nodeStateIsUnauthorized }.count
        let disks = machines.filter { $0.reachable != false && disk($0).0 != .ok }.count
        let memory = machines.filter { $0.reachable != false && self.memory($0) != .ok && self.memory($0) != .off }.count
        let stalled = batches.filter { $0.stalled == true }.count
        func plural(_ n: Int, _ one: String, _ many: String) -> String { "\(n) \(n == 1 ? one : many)" }
        if refused > 0 { out.append(plural(refused, "credential refused", "credentials refused")) }
        if offline > 0 { out.append(plural(offline, "node offline", "nodes offline")) }
        if disks > 0 { out.append(plural(disks, "node low on disk", "nodes low on disk")) }
        if memory > 0 { out.append(plural(memory, "node low on memory", "nodes low on memory")) }
        if stalled > 0 { out.append(plural(stalled, "job stalled", "jobs stalled")) }
        if location.hubVersionRelation == "older" { out.append("Hub older than the gateway targets") }
        return out
    }

    var lastSeen: String {
        if isDown { return location.unreachableSince.map { "down " + FleetFormat.relative($0) } ?? "unreachable" }
        return location.latencyMs.map { "answered in \($0) ms" } ?? "answered just now"
    }

    /// What a node is doing. GAP: the Hub reports a node as `working` but not which job; the studios come from
    /// its busy workers, and a model/label is shown only when the group has exactly one working node.
    func doing(_ machine: FleetMachine) -> (String, String?) {
        let state = (machine.state ?? "").lowercased()
        switch state {
        case "working":
            let busy = location.workers.filter { $0.machine == machine.machine && $0.busy == true }.compactMap { $0.modality }
            let headline = "Working" + (busy.isEmpty ? "" : " · " + busy.joined(separator: ", "))
            let working = machines.filter { ($0.state ?? "").lowercased() == "working" }
            if working.count == 1, let batch = batches.first(where: { ($0.running ?? 0) > 0 }) {
                return (headline, [batch.label, batch.model.map { $0.split(separator: "/").last.map(String.init) ?? $0 }]
                    .compactMap { $0 }.first)
            }
            return (headline, nil)
        case "ready": return ("Ready", nil)
        case "long_idle": return ("Idle", nil)
        case "offline": return ("—", nil)
        default: return (FleetFormat.words(machine.state), nil)
        }
    }
}

// MARK: - Page

final class FleetGroupsPage: NSView {
    private var summaries: [GroupSummary] = []
    private var problemsOnly = false
    private let body = NSStackView()
    private var expanded: Set<String> = []
    private var snapshot: FleetSnapshot?
    private var filterControl = NSSegmentedControl()

    init(frame: NSRect, problemsOnly: Bool) {
        self.problemsOnly = problemsOnly
        super.init(frame: frame)
        let scroll = NSScrollView()
        let document = FlippedView()
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        document.translatesAutoresizingMaskIntoConstraints = false
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = DS.Space.xl
        body.edgeInsets = NSEdgeInsets(top: DS.Space.page, left: DS.Space.page, bottom: DS.Space.page, right: DS.Space.page)
        body.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(body)
        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor), scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor), scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            body.topAnchor.constraint(equalTo: document.topAnchor), body.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            body.leadingAnchor.constraint(equalTo: document.leadingAnchor), body.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func update(_ snapshot: FleetSnapshot) {
        self.snapshot = snapshot
        summaries = snapshot.locations.map { location in
            GroupSummary(
                location: location,
                machines: snapshot.machines.filter { $0.locationId == location.id },
                batches: snapshot.running.filter { $0.locationId == location.id },
                issues: snapshot.issues.filter { $0.locationId == location.id })
        }
        // Problems first (worst first), then A-Z; the local Mac is just another group.
        summaries.sort {
            $0.health.rank != $1.health.rank ? $0.health.rank > $1.health.rank : $0.name < $1.name
        }
        if expanded.isEmpty { expanded = Set(summaries.filter { $0.health != .ok }.map { $0.name }) }
        rebuild()
    }

    private func rebuild() {
        body.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let width = widthAnchor
        func full(_ view: NSView) {
            body.addArrangedSubview(view)
            view.widthAnchor.constraint(equalTo: body.widthAnchor, constant: -2 * DS.Space.page).isActive = true
        }
        _ = width

        // Scaffold: title + subtitle, then the tab strip the Fleet page already has.
        body.addArrangedSubview(PageKit.heading("Fleet", "Every controller group this gateway sends work to, and how each node in it is doing."))
        let tabs = NSSegmentedControl(labels: ["Groups", "Locations", "Machines", "Models", "Jobs"], trackingMode: .selectOne, target: nil, action: nil)
        tabs.selectedSegment = 0
        body.addArrangedSubview(tabs)
        body.setCustomSpacing(DS.Space.m, after: body.arrangedSubviews[0])

        let attention = summaries.filter { $0.health != .ok }
        // Needs-attention banner.
        if !attention.isEmpty { full(banner(attention)) }
        full(toolbar(attentionCount: attention.count))
        full(summaryStrip())

        let shown = problemsOnly ? attention : summaries
        for group in shown { full(card(group)) }
        if shown.isEmpty { full(emptyState()) }
    }

    // MARK: Pieces

    private func banner(_ groups: [GroupSummary]) -> NSView {
        let panel = Panel()
        let worst = groups.map { $0.health }.max() ?? .warn
        panel.tint = worst.tint
        let icon = NSImageView(image: NSImage(systemSymbolName: worst.symbol, accessibilityDescription: nil)!)
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 16, weight: .semibold)
        icon.contentTintColor = worst.text
        let title = label("\(groups.count) of \(summaries.count) groups need attention", DS.Font.rowTitle)
        var lines: [NSView] = [title]
        for group in groups.prefix(4) {
            let reason = group.problems.joined(separator: " · ")
            let badge = Badge(group.name, group.health)
            let cell = NSView()
            badge.translatesAutoresizingMaskIntoConstraints = false
            cell.addSubview(badge)
            NSLayoutConstraint.activate([
                badge.leadingAnchor.constraint(equalTo: cell.leadingAnchor), badge.topAnchor.constraint(equalTo: cell.topAnchor),
                badge.bottomAnchor.constraint(equalTo: cell.bottomAnchor), cell.widthAnchor.constraint(equalToConstant: 96),
            ])
            let row = NSStackView(views: [cell, label(reason.isEmpty ? "Needs a look" : reason, DS.Font.secondary, .labelColor)])
            row.spacing = DS.Space.s
            lines.append(row)
        }
        if groups.count > 4 { lines.append(label("and \(groups.count - 4) more below", DS.Font.caption, .secondaryLabelColor)) }
        let text = column(lines, spacing: 6)
        let button = NSButton(title: problemsOnly ? "Show all groups" : "Show problems only", target: nil, action: nil)
        button.bezelStyle = .rounded
        button.controlSize = .regular
        let row = NSStackView(views: [icon, text, NSView(), button])
        row.alignment = .top
        row.spacing = DS.Space.m
        row.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l)
        row.translatesAutoresizingMaskIntoConstraints = false
        panel.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: panel.topAnchor), row.bottomAnchor.constraint(equalTo: panel.bottomAnchor),
            row.leadingAnchor.constraint(equalTo: panel.leadingAnchor), row.trailingAnchor.constraint(equalTo: panel.trailingAnchor),
        ])
        return panel
    }

    private func toolbar(attentionCount: Int) -> NSView {
        filterControl = NSSegmentedControl(labels: ["All groups (\(summaries.count))", "Needs attention (\(attentionCount))"],
                                           trackingMode: .selectOne, target: nil, action: nil)
        filterControl.selectedSegment = problemsOnly ? 1 : 0
        let search = NSSearchField()
        search.placeholderString = "Find a group or node"
        search.widthAnchor.constraint(equalToConstant: 220).isActive = true
        let updated = label("Updated 3 s ago", DS.Font.caption, .secondaryLabelColor)
        let refresh = NSButton(title: "Refresh", target: nil, action: nil)
        refresh.bezelStyle = .rounded
        let row = NSStackView(views: [filterControl, search, NSView(), updated, refresh])
        row.spacing = DS.Space.m
        return row
    }

    /// Four inline figures instead of six coloured tiles: colour is reserved for state, never decoration.
    private func summaryStrip() -> NSView {
        let nodes = summaries.reduce(0) { $0 + $1.machines.count }
        let online = summaries.reduce(0) { $0 + $1.online }
        let up = summaries.filter { !$0.isDown }.count
        let running = summaries.reduce(0) { $0 + $1.running }
        let queued = summaries.reduce(0) { $0 + $1.queued }
        let worst = summaries.compactMap { $0.worstDisk }.min { ($0.machine.diskFreeGb ?? 0) < ($1.machine.diskFreeGb ?? 0) }
        func stat(_ value: String, _ caption: String, _ health: Health = .ok) -> NSView {
            let figure = label(value, DS.Font.figure, health == .ok ? .labelColor : health.text)
            return column([figure, label(caption, DS.Font.caption, .secondaryLabelColor)], spacing: 0)
        }
        let panel = Panel()
        let row = NSStackView(views: [
            stat("\(up)/\(summaries.count)", "groups reachable", up == summaries.count ? .ok : .bad),
            stat("\(online)/\(nodes)", "nodes online", online == nodes ? .ok : .warn),
            stat("\(running)", "jobs running · \(queued) queued"),
            stat(worst.map { FleetFormat.gb($0.machine.diskFreeGb) } ?? "—",
                 "least free disk" + (worst.map { " · " + ($0.machine.label ?? "") } ?? ""), worst?.health ?? .off),
        ])
        row.distribution = .fillEqually
        row.spacing = DS.Space.xl
        row.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l)
        row.translatesAutoresizingMaskIntoConstraints = false
        panel.addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: panel.topAnchor), row.bottomAnchor.constraint(equalTo: panel.bottomAnchor),
            row.leadingAnchor.constraint(equalTo: panel.leadingAnchor), row.trailingAnchor.constraint(equalTo: panel.trailingAnchor),
        ])
        return panel
    }

    private func emptyState() -> NSView {
        PageKit.empty("Every group is healthy", "Nothing needs attention right now. Switch to All groups to see every node.", symbol: "checkmark.seal")
    }

    // Column widths shared by the header row and every node row, so the numbers line up under their titles.
    private static let widths: [CGFloat] = [0, 112, 150, 118, 108, 64]   // node (flex), state, doing now, disk, memory, seen

    private func card(_ group: GroupSummary) -> NSView {
        let panel = Panel()
        let isOpen = expanded.contains(group.name)

        // Header: health dot + name + role + version | nodes | jobs | disk | memory.
        let dot = Badge(group.isDown ? "Down" : group.health == .ok ? "Healthy" : "Attention", group.health, symbol: true)
        let chevron = NSImageView(image: NSImage(systemSymbolName: isOpen ? "chevron.down" : "chevron.right", accessibilityDescription: nil)!)
        chevron.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 11, weight: .semibold)
        chevron.contentTintColor = .secondaryLabelColor
        chevron.widthAnchor.constraint(equalToConstant: 14).isActive = true

        var titleViews: [NSView] = [label(group.name, DS.Font.rowTitle)]
        if group.location.local == true { titleViews.append(label("this Mac", DS.Font.caption, .secondaryLabelColor)) }
        if let version = group.location.version {
            let relation = group.location.hubVersionRelation
            let colour: NSColor = (relation == "older" || relation == "newer") ? Health.warn.text : .secondaryLabelColor
            titleViews.append(label("v\(version)" + (relation == "older" ? " · older" : relation == "newer" ? " · newer" : ""), DS.Font.mono, colour))
        }
        let title = NSStackView(views: titleViews)
        title.spacing = DS.Space.s
        let problems = group.problems
        let sub = label(problems.isEmpty ? "\(group.nodes.count) node\(group.nodes.count == 1 ? "" : "s") · \(group.lastSeen)" : problems.joined(separator: " · "),
                        DS.Font.secondary, problems.isEmpty ? .secondaryLabelColor : (group.health == .bad ? Health.bad.text : Health.warn.text))
        let left = column([title, sub], spacing: 3)
        left.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        func metric(_ caption: String, _ value: NSView) -> NSView {
            let stack = column([label(caption, DS.Font.caption, .secondaryLabelColor), value], spacing: 3)
            stack.widthAnchor.constraint(equalToConstant: caption == "Jobs" ? 140 : 108).isActive = true
            return stack
        }
        let nodesValue = label(group.isDown ? "\(group.location.nodes.count) known" : "\(group.online) of \(group.nodes.count) online",
                               DS.Font.secondary, group.online < group.nodes.count || group.isDown ? Health.warn.text : .labelColor)
        let jobsValue = label(group.isDown ? "—" : "\(group.running) running · \(group.queued) queued", DS.Font.secondary)
        let diskValue: NSView = group.worstDisk.map { worst -> NSView in
            let (health, _) = group.disk(worst.machine)
            let badge = Badge("\(FleetFormat.gb(worst.machine.diskFreeGb)) free", health)
            return health == .ok ? label("\(FleetFormat.gb(worst.machine.diskFreeGb)) free", DS.Font.secondary) : badge
        } ?? label("—", DS.Font.secondary, .secondaryLabelColor)
        let memValue: NSView = group.worstMemory.map { worst -> NSView in
            let text = "\(FleetFormat.gb(worst.machine.availableGb)) free"
            return worst.health == .ok ? label(text, DS.Font.secondary) : Badge(text, worst.health)
        } ?? label("—", DS.Font.secondary, .secondaryLabelColor)

        let header = NSStackView(views: [chevron, dot, left, NSView(),
                                         metric("Nodes", nodesValue), metric("Jobs", jobsValue),
                                         metric("Disk (worst)", diskValue), metric("Memory (worst)", memValue)])
        header.spacing = DS.Space.m
        header.alignment = .centerY
        header.edgeInsets = NSEdgeInsets(top: DS.Space.m, left: DS.Space.l, bottom: DS.Space.m, right: DS.Space.l)
        dot.widthAnchor.constraint(greaterThanOrEqualToConstant: 88).isActive = true

        let content = NSStackView(views: [header])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 0
        header.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true

        if isOpen {
            let divider = NSBox(); divider.boxType = .separator
            content.addArrangedSubview(divider)
            divider.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
            for (health, note) in group.notes {
                let line = label("• " + note, DS.Font.secondary, health.text, lines: 2)
                line.lineBreakMode = .byWordWrapping
                line.preferredMaxLayoutWidth = 760
                content.addArrangedSubview(noteRow(line, top: DS.Space.s))
            }
            content.addArrangedSubview(columnHeader())
            for node in (group.nodes.isEmpty ? group.location.nodes : []) {
                content.addArrangedSubview(rosterRow(node))
                let line = NSBox(); line.boxType = .separator
                content.addArrangedSubview(line)
                line.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -2 * DS.Space.l).isActive = true
            }
            for machine in group.nodes {
                content.addArrangedSubview(nodeRow(machine, group))
                let line = NSBox(); line.boxType = .separator
                content.addArrangedSubview(line)
                line.widthAnchor.constraint(equalTo: content.widthAnchor, constant: -2 * DS.Space.l).isActive = true
            }
            if group.nodes.isEmpty && group.location.nodes.isEmpty {
                let note = label(group.isDown ? "No roster was ever read from this group." : "No nodes have joined this group yet.",
                                 DS.Font.secondary, .secondaryLabelColor)
                note.translatesAutoresizingMaskIntoConstraints = false
                let holder = NSView(); holder.addSubview(note)
                NSLayoutConstraint.activate([note.leadingAnchor.constraint(equalTo: holder.leadingAnchor, constant: DS.Space.l + 22),
                                             note.topAnchor.constraint(equalTo: holder.topAnchor, constant: DS.Space.m),
                                             note.bottomAnchor.constraint(equalTo: holder.bottomAnchor, constant: -DS.Space.m)])
                content.addArrangedSubview(holder)
            }
        }
        content.translatesAutoresizingMaskIntoConstraints = false
        panel.addSubview(content)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: panel.topAnchor), content.bottomAnchor.constraint(equalTo: panel.bottomAnchor),
            content.leadingAnchor.constraint(equalTo: panel.leadingAnchor), content.trailingAnchor.constraint(equalTo: panel.trailingAnchor),
        ])
        return panel
    }

    private func rowStack(_ cells: [NSView]) -> NSStackView {
        let row = NSStackView()
        row.spacing = DS.Space.s
        row.alignment = .centerY
        row.edgeInsets = NSEdgeInsets(top: DS.Space.s, left: DS.Space.l + 22, bottom: DS.Space.s, right: DS.Space.l)
        for (index, cell) in cells.enumerated() {
            // A holder per column: fixed width (the first one is the flexible one), content pinned to the leading
            // edge at its natural size, so a badge is never stretched to its column.
            let holder = NSView()
            cell.translatesAutoresizingMaskIntoConstraints = false
            holder.addSubview(cell)
            NSLayoutConstraint.activate([
                cell.leadingAnchor.constraint(equalTo: holder.leadingAnchor),
                cell.trailingAnchor.constraint(lessThanOrEqualTo: holder.trailingAnchor),
                cell.topAnchor.constraint(equalTo: holder.topAnchor),
                cell.bottomAnchor.constraint(equalTo: holder.bottomAnchor),
            ])
            let width = Self.widths[index]
            if width > 0 {
                holder.widthAnchor.constraint(equalToConstant: width).isActive = true
            } else {
                holder.widthAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
                holder.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            }
            row.addArrangedSubview(holder)
        }
        return row
    }

    private func columnHeader() -> NSView {
        let titles = ["Node", "State", "Doing now", "Disk", "Memory", "Last seen"]
        return rowStack(titles.map { label($0, DS.Font.captionStrong, .secondaryLabelColor) })
    }

    private func nodeRow(_ machine: FleetMachine, _ group: GroupSummary) -> NSView {
        let node = group.location.nodes.first { $0.id == machine.machine }
        let nodeHealth = group.nodeHealth(machine)
        let stateWord = machine.nodeStateIsUnauthorized ? "Credential refused"
            : (machine.state ?? "").lowercased() == "long_idle" ? "Idle" : FleetFormat.words(machine.state).capitalized
        let hardware = [machine.chip, machine.memoryGb.map { FleetFormat.gb($0).replacingOccurrences(of: ".0", with: "") }].compactMap { $0 }.joined(separator: " · ")
        var subtitle = hardware
        var subtitleColour = NSColor.secondaryLabelColor
        if let version = node?.version, let controller = group.location.version, version != controller {
            subtitle += " · v\(version)"; subtitleColour = Health.warn.text
        }
        let name = column([label(machine.label ?? machine.machine ?? "node", DS.Font.rowTitle),
                           label(subtitle, DS.Font.caption, subtitleColour)], spacing: 1)

        let (doing, detail) = group.doing(machine)
        let doingView = column([label(doing, DS.Font.secondary, nodeHealth == .bad ? Health.bad.text : .labelColor)]
                               + (detail.map { [label($0, DS.Font.caption, .secondaryLabelColor)] } ?? []), spacing: 1)

        func meter(_ text: String, _ fraction: Double?, _ health: Health) -> NSView {
            guard let fraction else { return label("—", DS.Font.secondary, .secondaryLabelColor) }
            return column([label(text, DS.Font.secondary, health == .ok ? .labelColor : health.text),
                           Meter(fraction: fraction, health: health)], spacing: 4)
        }
        let offline = machine.reachable == false
        let (diskHealth, _) = group.disk(machine)
        let diskCell = offline ? label("—", DS.Font.secondary, .secondaryLabelColor)
            : meter("\(FleetFormat.gb(machine.diskFreeGb)) free",
                    machine.diskTotalGb.flatMap { $0 > 0 ? 1 - (machine.diskFreeGb ?? 0) / $0 : nil }, diskHealth)
        let memHealth = group.memory(machine)
        let memCell = offline ? label("—", DS.Font.secondary, .secondaryLabelColor)
            : meter("\(FleetFormat.gb(machine.availableGb)) free",
                    machine.memoryGb.flatMap { $0 > 0 ? 1 - (machine.availableGb ?? 0) / $0 : nil }, memHealth)
        let seen = label(machine.isNode ? machine.nodeLastSeenText : "live", DS.Font.secondary, .secondaryLabelColor)

        let row = rowStack([name, Badge(stateWord, nodeHealth), doingView, diskCell, memCell, seen])
        if let detail = node?.detail ?? machine.nodeDetail, !detail.isEmpty, nodeHealth == .bad {
            let note = label(detail, DS.Font.caption, Health.bad.text)
            let wrapper = column([row, noteRow(note)], spacing: 0)
            row.widthAnchor.constraint(equalTo: wrapper.widthAnchor).isActive = true
            return wrapper
        }
        return row
    }

    /// A node the controller still lists but this snapshot has no machine row for (a group that is down).
    private func rosterRow(_ node: FleetNode) -> NSView {
        let name = column([label(node.name ?? node.id ?? "node", DS.Font.rowTitle),
                           label(node.address ?? "", DS.Font.caption, .secondaryLabelColor)], spacing: 1)
        let seen = label(node.lastSeenAt?.doubleValue == nil ? "never" : node.lastSeenText, DS.Font.secondary, .secondaryLabelColor)
        let dash = { label("—", DS.Font.secondary, .secondaryLabelColor) }
        return rowStack([name, Badge("Unknown", .off), label("Last known state: " + node.stateText, DS.Font.caption, .secondaryLabelColor, lines: 2), dash(), dash(), seen])
    }

    private func noteRow(_ note: NSTextField, top: CGFloat = 0) -> NSView {
        let holder = NSView()
        note.translatesAutoresizingMaskIntoConstraints = false
        holder.addSubview(note)
        NSLayoutConstraint.activate([
            note.leadingAnchor.constraint(equalTo: holder.leadingAnchor, constant: DS.Space.l + 22),
            note.trailingAnchor.constraint(lessThanOrEqualTo: holder.trailingAnchor, constant: -DS.Space.l),
            note.topAnchor.constraint(equalTo: holder.topAnchor, constant: top),
            note.bottomAnchor.constraint(equalTo: holder.bottomAnchor, constant: top == 0 ? -DS.Space.s : 0),
        ])
        return holder
    }
}
