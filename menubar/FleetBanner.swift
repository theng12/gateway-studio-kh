import AppKit

// The Fleet warnings banner: one line per KIND of warning, with a count and the fix stated once, instead
// of one line per Mac. 45 Macs that each said "macOS is set to download updates automatically" used to
// push the machines table off the page in a block that could not be resized.
//
// Two halves. `FleetWarnings` is pure (machines in, grouped lines out) so the grouping is tested without
// a window. `FleetWarningList` draws the lines: each has [Show Macs] to expand the Mac labels inline, and
// the whole banner is capped at `FleetClampedScroll.bannerHeight` and scrolls past that, so the table
// below always gets the remaining space. The same two pieces serve the Machines tab, the Groups tab's
// attention banner and the issue notes inside a group card.

// MARK: - Model

/// One line of the banner.
struct FleetWarning {
    var id: String
    /// What the warning is about (`disk_space`, `macos_updates`, `time_machine`, `other`, or an issue code).
    var kind: String
    var health: Health
    /// The counted sentence: "17 Macs download macOS updates automatically".
    var text: String
    /// The fix, once for everybody the line covers.
    var fix: String?
    /// The Mac labels (or subjects) the line covers, shown by [Show …].
    var items: [String] = []
    /// What the items are, for the button: "Macs", "Workers" …
    var itemsNoun = "Macs"
    /// Set when the owner may hide this kind of warning. Never set for disk space.
    var hideKind: String?

    var line: String { fix.map { text + " — " + $0 } ?? text }
}

/// What `FleetWarnings.build` found: the lines to show and what the owner's hidden list swallowed.
struct FleetWarningSet {
    var warnings: [FleetWarning] = []
    /// Kinds that are hiding at least one Mac's warning right now, and how many Macs that is.
    var hiddenKinds: [String] = []
    var hiddenMacs = 0
}

enum FleetWarnings {
    /// The only kinds that may be hidden. Disk space is deliberately absent and is never hidden, whatever the
    /// stored list says (the gateway refuses it too).
    static let hideable: Set<String> = ["macos_updates", "time_machine"]

    static let titles = ["macos_updates": "macOS automatic updates", "time_machine": "Time Machine"]

    static func macs(_ count: Int) -> String { count == 1 ? "1 Mac" : "\(count) Macs" }

    private static func name(_ machine: FleetMachine) -> String { machine.label ?? machine.machine ?? "a Mac" }

    /// Group every Mac's disk state and Hub warnings into one line per kind.
    static func build(machines: [FleetMachine], hidden: Set<String> = []) -> FleetWarningSet {
        let hidden = hidden.intersection(hideable)
        var out = FleetWarningSet()
        out.warnings = diskLines(machines)

        struct Key: Hashable { let kind: String; let text: String }
        var order: [Key] = []
        var members: [Key: [String]] = [:]
        var hiddenMacs: [String: Set<String>] = [:]
        for machine in machines {
            guard let disk = machine.disk else { continue }
            let who = name(machine)
            for item in disk.warningItems {
                // A Hub's own low-disk sentence restates the disk line already drawn for this Mac.
                if disk.isLow && item.kind == "disk_space" { continue }
                if hidden.contains(item.kind) { hiddenMacs[item.kind, default: []].insert(who); continue }
                let key = Key(kind: item.kind, text: item.text)
                if members[key] == nil { order.append(key); members[key] = [] }
                // The same sentence twice for one Mac is one warning.
                if members[key]?.contains(who) == false { members[key]?.append(who) }
            }
        }
        let other = order.map { key -> FleetWarning in
            let who = members[key] ?? []
            return compose(kind: key.kind, text: key.text, who: who)
        }.sorted { ($0.items.count, $1.text) > ($1.items.count, $0.text) }
        out.warnings.append(contentsOf: other)
        out.hiddenKinds = hiddenMacs.keys.sorted()
        out.hiddenMacs = Set(hiddenMacs.values.flatMap { $0 }).count
        return out
    }

    // MARK: Disk lines

    private static func diskLines(_ machines: [FleetMachine]) -> [FleetWarning] {
        // A Mac that is not answering has no disk worth reporting (its last reading is stale or a zero).
        let low = machines.filter { $0.disk?.isLow == true && $0.reachable != false }
            .sorted { ($0.disk?.freeGb ?? 0) < ($1.disk?.freeGb ?? 0) }
        var out: [FleetWarning] = []
        for state in ["stop", "red", "amber"] {
            let group = low.filter { ($0.disk?.state ?? "").lowercased() == state }
            guard let first = group.first?.disk else { continue }
            func detail(_ machine: FleetMachine) -> String {
                guard let disk = machine.disk else { return name(machine) }
                var line = "\(FleetFormat.gb(disk.freeGb)) free"
                if let top = disk.top.first(where: { $0.name?.isEmpty == false }), let what = top.name {
                    line += " (largest: \(what)\(top.gb.map { " " + FleetFormat.gb($0) } ?? ""))"
                }
                return line
            }
            let consequence = state == "stop" ? ". Not being sent work" : ""
            let text: String
            if group.count == 1 {
                text = "\(name(group[0])): disk \(state), \(detail(group[0]))\(consequence)"
            } else {
                text = "\(macs(group.count)): disk \(state), \(FleetFormat.gb(group.first?.disk?.freeGb))"
                    + " to \(FleetFormat.gb(group.last?.disk?.freeGb)) free\(consequence)"
            }
            out.append(FleetWarning(
                id: "disk:\(state)", kind: "disk_space", health: first.health ?? .warn, text: text, fix: nil,
                items: group.count == 1 ? [] : group.map { "\(name($0)) — \(detail($0))" }))
        }
        return out
    }

    // MARK: A Hub's own warnings

    /// "macOS is set to download updates automatically (8-15 GB each). Turn it off in System Settings > …".
    private static let updateSentence = try? NSRegularExpression(
        pattern: #"^macOS is set to download updates automatically\s*(\([^)]*\))?\s*\.?\s*Turn it off in (.+?)\.?$"#,
        options: [.caseInsensitive])

    private static func compose(kind: String, text: String, who: [String]) -> FleetWarning {
        let count = who.count
        let hide = hideable.contains(kind) ? kind : nil
        if kind == "macos_updates", let regex = updateSentence,
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let path = Range(match.range(at: 2), in: text) {
            let size = Range(match.range(at: 1), in: text).map { " " + text[$0] } ?? ""
            let subject = count == 1 ? who[0] : macs(count)
            let verb = count == 1 ? "downloads" : "download"
            let steps = text[path].replacingOccurrences(of: " > ", with: " › ")
            return FleetWarning(
                id: "warn:\(kind):\(text)", kind: kind, health: .warn,
                text: "\(subject) \(verb) macOS updates automatically\(size)",
                fix: "turn off in \(steps)", items: count == 1 ? [] : who, hideKind: hide)
        }
        let sentence = text.hasSuffix(".") ? String(text.dropLast()) : text
        return FleetWarning(
            id: "warn:\(kind):\(text)", kind: kind, health: .warn,
            text: count == 1 ? "\(who[0]): \(sentence)" : "\(macs(count)): \(sentence)", fix: nil,
            items: count == 1 ? [] : who, hideKind: hide)
    }

    // MARK: Gateway issues

    /// What a repeated issue code reads as, with the count in front: "5 machines are offline".
    private static let issueNouns: [String: (many: String, noun: String)] = [
        "machine_offline": ("machines are offline", "Macs"),
        "machine_not_healthy": ("machines report a health problem", "Macs"),
        "machine_quarantined": ("machines are quarantined", "Macs"),
        "node_credential_rejected": ("nodes refused this controller's credential", "Macs"),
        "worker_offline": ("workers are offline", "Workers"),
        "worker_maintenance": ("workers are in maintenance", "Workers"),
        "model_catalog_error": ("models failed their catalogue check", "Models"),
        "running_stalled": ("jobs are stalled", "Jobs"),
        "machine_limited": ("machines have a limitation", "Macs"),
    ]

    /// The Gateway's error and warning issues, one line per (code, action): a single issue keeps the Gateway's
    /// own sentence verbatim; several of the same kind become "N machines are offline — <fix once>".
    static func group(issues: [FleetIssue]) -> [FleetWarning] {
        struct Key: Hashable { let code: String; let action: String }
        var order: [Key] = []
        var rows: [Key: [FleetIssue]] = [:]
        var seen = Set<String>()
        for issue in issues where issue.severity == "error" || issue.severity == "warning" {
            let message = issue.message ?? ""
            guard !message.isEmpty, seen.insert("\(issue.code ?? "")|\(issue.subject ?? "")|\(message)").inserted else { continue }
            let key = Key(code: issue.code ?? message, action: issue.action ?? "")
            if rows[key] == nil { order.append(key); rows[key] = [] }
            rows[key]?.append(issue)
        }
        return order.compactMap { key in
            guard let group = rows[key], let first = group.first else { return nil }
            let health = group.contains { $0.severity == "error" } ? Health.bad : Health.warn
            if group.count == 1 {
                return FleetWarning(id: "issue:\(key.code):\(first.subject ?? "")", kind: key.code, health: health,
                                    text: first.message ?? "", fix: nil)
            }
            let known = issueNouns[key.code]
            let text = known.map { "\(group.count) \($0.many)" } ?? "\(first.message ?? "") (and \(group.count - 1) more like it)"
            let fix = key.action.isEmpty || key.action.hasPrefix("Fleet →") ? nil : key.action
            return FleetWarning(id: "issue:\(key.code):\(key.action)", kind: key.code, health: health, text: text, fix: fix,
                                items: group.compactMap { $0.subject }.filter { !$0.isEmpty },
                                itemsNoun: known?.noun ?? "Details")
        }
    }
}

// MARK: - Clamped scroller

/// Any content, capped at `maxHeight` and scrolling beyond it. The cap is what keeps a long banner from
/// pushing the table off the page: the page gives the banner at most this much, the table gets the rest.
final class FleetClampedScroll: NSView {
    /// About five lines of 12 pt text with their gaps.
    static let bannerHeight: CGFloat = 128
    /// Inside a group card, where the rows are the card's own text.
    static let cardHeight: CGFloat = 140
    /// The Groups tab's attention banner, whose rows carry a badge.
    static let groupsHeight: CGFloat = 150

    let maxHeight: CGFloat
    private let scroll = NSScrollView()
    private let document = FlippedView()
    private var content: NSView?
    private var height: NSLayoutConstraint!

    init(maxHeight: CGFloat = FleetClampedScroll.bannerHeight) {
        self.maxHeight = maxHeight
        super.init(frame: .zero)
        height = heightAnchor.constraint(equalToConstant: 0)
        scroll.documentView = document
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        document.translatesAutoresizingMaskIntoConstraints = false
        addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: topAnchor), scroll.bottomAnchor.constraint(equalTo: bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: leadingAnchor), scroll.trailingAnchor.constraint(equalTo: trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            height,
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func setContent(_ view: NSView) {
        content?.removeFromSuperview()
        view.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: document.topAnchor), view.bottomAnchor.constraint(equalTo: document.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: document.leadingAnchor), view.trailingAnchor.constraint(equalTo: document.trailingAnchor),
        ])
        content = view
        needsLayout = true
    }

    /// The height the content wants at the width it has, capped.
    override func layout() {
        super.layout()
        let wanted = min(max(document.frame.height, 0), maxHeight)
        if abs(height.constant - wanted) > 0.5 {
            height.constant = wanted
        }
    }
}

// MARK: - Banner view

/// The banner: grouped warning lines in a tinted card (or flat, inside a group card), capped in height.
final class FleetWarningList: CardView {
    /// The owner hid a kind (`macos_updates` …).
    var onHide: ((String) -> Void)?
    /// The owner asked for hidden kinds to show again.
    var onShowHidden: (() -> Void)?

    private let clamp: FleetClampedScroll
    private var expanded = Set<String>()
    private var current = FleetWarningSet()
    private let plain: Bool

    /// `plain` is the flat form inside a group card, which is allowed a little more height than the page banner.
    init(plain: Bool = false) {
        self.plain = plain
        clamp = FleetClampedScroll(maxHeight: plain ? FleetClampedScroll.cardHeight : FleetClampedScroll.bannerHeight)
        super.init(frame: .zero)
        clamp.translatesAutoresizingMaskIntoConstraints = false
        hold(clamp, inset: plain
            ? NSEdgeInsets(top: DS.Space.s, left: DS.Space.l, bottom: DS.Space.s, right: DS.Space.l)
            : NSEdgeInsets(top: DS.Space.s, left: DS.Space.m, bottom: DS.Space.s, right: DS.Space.m))
        isHidden = true
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func updateLayer() {
        super.updateLayer()
        if plain { layer?.backgroundColor = NSColor.clear.cgColor; layer?.borderWidth = 0 }
    }

    /// Whether anything is drawn at all.
    var isEmpty: Bool { current.warnings.isEmpty && current.hiddenKinds.isEmpty }

    func set(_ set: FleetWarningSet) {
        current = set
        rebuild()
    }

    func set(warnings: [FleetWarning]) { set(FleetWarningSet(warnings: warnings)) }

    private func rebuild() {
        let set = current
        isHidden = isEmpty
        let worst = set.warnings.map { $0.health }.max() ?? .off
        if !plain { tint = set.warnings.isEmpty ? nil : worst.tint }
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = DS.Space.tight
        for warning in set.warnings { add(row(warning), to: stack) }
        if !set.hiddenKinds.isEmpty { add(hiddenRow(set), to: stack) }
        clamp.setContent(stack)
    }

    private func add(_ view: NSView, to stack: NSStackView) {
        stack.addArrangedSubview(view)
        view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func row(_ warning: FleetWarning) -> NSView {
        let icon = NSImageView(image: NSImage(systemSymbolName: warning.health.symbol, accessibilityDescription: "Warning")
            ?? NSImage())
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 11, weight: .semibold)
        icon.contentTintColor = warning.health.text
        icon.setContentHuggingPriority(.required, for: .horizontal)
        icon.widthAnchor.constraint(equalToConstant: 14).isActive = true

        let label = PageKit.label(warning.line, .secondary, colour: warning.health.text, wrapping: true)
        label.preferredMaxLayoutWidth = 0
        label.maximumNumberOfLines = 0
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        var views: [NSView] = [icon, label, NSView()]
        let open = expanded.contains(warning.id)
        if !warning.items.isEmpty {
            views.append(link("\(open ? "Hide" : "Show") \(warning.itemsNoun)") { [weak self] in
                guard let self else { return }
                if open { self.expanded.remove(warning.id) } else { self.expanded.insert(warning.id) }
                self.rebuild()
            })
        }
        if let kind = warning.hideKind {
            let button = link("Hide Warning") { [weak self] in self?.onHide?(kind) }
            button.toolTip = "Stop showing \(FleetWarnings.titles[kind] ?? "this kind of") warnings. "
                + "You can show them again from this banner."
            views.append(button)
        }
        let top = NSStackView(views: views)
        top.orientation = .horizontal
        top.alignment = .firstBaseline
        top.spacing = DS.Space.s
        top.setHuggingPriority(.defaultLow, for: .horizontal)

        guard open, !warning.items.isEmpty else { return top }
        let list = PageKit.label(warning.items.joined(separator: "  ·  "), .caption, colour: .secondaryLabelColor, wrapping: true)
        list.preferredMaxLayoutWidth = 0
        list.maximumNumberOfLines = 0
        let indented = NSView()
        list.translatesAutoresizingMaskIntoConstraints = false
        indented.addSubview(list)
        NSLayoutConstraint.activate([
            list.leadingAnchor.constraint(equalTo: indented.leadingAnchor, constant: 14 + DS.Space.s),
            list.trailingAnchor.constraint(equalTo: indented.trailingAnchor),
            list.topAnchor.constraint(equalTo: indented.topAnchor), list.bottomAnchor.constraint(equalTo: indented.bottomAnchor),
        ])
        let wrapper = NSStackView(views: [top, indented])
        wrapper.orientation = .vertical
        wrapper.alignment = .leading
        wrapper.spacing = DS.Space.xs
        top.widthAnchor.constraint(equalTo: wrapper.widthAnchor).isActive = true
        indented.widthAnchor.constraint(equalTo: wrapper.widthAnchor).isActive = true
        return wrapper
    }

    private func hiddenRow(_ set: FleetWarningSet) -> NSView {
        let names = set.hiddenKinds.map { FleetWarnings.titles[$0] ?? $0 }.joined(separator: ", ")
        let label = PageKit.label("Hidden by you: \(names) (\(FleetWarnings.macs(set.hiddenMacs)))",
                                  .caption, colour: .secondaryLabelColor, wrapping: true)
        label.preferredMaxLayoutWidth = 0
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let show = link("Show Again") { [weak self] in self?.onShowHidden?() }
        let row = NSStackView(views: [label, NSView(), show])
        row.orientation = .horizontal
        row.alignment = .firstBaseline
        row.spacing = DS.Space.s
        return row
    }

    private func link(_ title: String, _ action: @escaping () -> Void) -> NSButton {
        let button = FleetClosureButton(title: title, handler: action)
        button.isBordered = false
        button.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: TextRole.captionStrong.font, .foregroundColor: NSColor.linkColor])
        button.setContentHuggingPriority(.required, for: .horizontal)
        button.setContentCompressionResistancePriority(.required, for: .horizontal)
        return button
    }
}
