// The Overview window: what the app is doing, at a glance.
//
// This is the piece that turns a menu bar toggle into an application. It reads
// only from the gateway's own APIs, so what it shows is what the service
// actually reports rather than what the app hopes is true.

import Cocoa

/// A boxed number with a caption. Used for the headline stats.
///
/// A card (`CardView`), so the fill and border are re-resolved whenever the appearance changes; this view
/// used to assign `controlBackgroundColor.cgColor` once at build time and stayed light on a dark page. The
/// figure is the label colour unless `health` says the number itself is a problem: colour is state, never
/// decoration.
final class StatTile: CardView {
    private let valueLabel = NSTextField(labelWithString: "—")
    private let titleLabel = NSTextField(labelWithString: "")
    private let health: Health

    init(title: String, health: Health = .ok) {
        self.health = health
        super.init(frame: .zero)

        valueLabel.font = TextRole.figure.font
        valueLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        titleLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        valueLabel.textColor = health == .ok ? .labelColor : health.text
        titleLabel.font = TextRole.caption.font
        titleLabel.textColor = .secondaryLabelColor
        titleLabel.lineBreakMode = .byTruncatingTail

        let stack = NSStackView(views: [valueLabel, titleLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 0
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: DS.Space.l),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -DS.Space.l),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            heightAnchor.constraint(equalToConstant: 72),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func set(value: String, title: String) {
        valueLabel.stringValue = value
        titleLabel.stringValue = title
    }

    /// Show "unknown" without losing the caption.
    func blank() {
        valueLabel.stringValue = "—"
    }
}

/// A labelled proportional bar, for the per-modality breakdown.
///
/// Every dimension here is explicit, and that is the whole point. The first
/// version positioned its children relative to `centerY` but never constrained
/// its own height, so inside a stack view the row collapsed to nothing and the
/// labels piled on top of each other — a window that "built fine" and was
/// unreadable. The track width is explicit for the same reason: the fill is
/// computed from it, rather than from a magic number that could overflow.
final class ModalityBar: NSView {
    private static let nameWidth: CGFloat = 108
    private static let trackWidth: CGFloat = 132
    private static let countWidth: CGFloat = 44
    static let rowHeight: CGFloat = 18
    /// The row's natural width, so callers do not have to guess one.
    static let rowWidth = nameWidth + DS.Space.s + trackWidth + DS.Space.s + countWidth

    private let name = NSTextField(labelWithString: "")
    private let count = NSTextField(labelWithString: "")
    private let track = Meter(fraction: 0, health: .info)

    init() {
        super.init(frame: .zero)
        name.font = TextRole.secondary.font
        name.lineBreakMode = .byTruncatingTail
        name.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        count.font = TextRole.secondary.font
        count.textColor = .secondaryLabelColor
        count.alignment = .right

        track.fill = .controlAccentColor

        for view in [name, count, track] {
            view.translatesAutoresizingMaskIntoConstraints = false
            addSubview(view)
        }

        NSLayoutConstraint.activate([
            // Without this the row has no height and the stack view crushes it.
            heightAnchor.constraint(equalToConstant: Self.rowHeight),

            name.leadingAnchor.constraint(equalTo: leadingAnchor),
            name.widthAnchor.constraint(equalToConstant: Self.nameWidth),
            name.centerYAnchor.constraint(equalTo: centerYAnchor),

            track.leadingAnchor.constraint(equalTo: name.trailingAnchor, constant: DS.Space.s),
            track.widthAnchor.constraint(equalToConstant: Self.trackWidth),
            track.centerYAnchor.constraint(equalTo: centerYAnchor),

            count.leadingAnchor.constraint(equalTo: track.trailingAnchor, constant: DS.Space.s),
            count.widthAnchor.constraint(equalToConstant: Self.countWidth),
            count.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func set(label: String, value: Int, maxValue: Int) {
        name.stringValue = label
        count.stringValue = "\(value)"
        let fraction = maxValue > 0 ? CGFloat(value) / CGFloat(maxValue) : 0
        // Proportional to the real track, so a full bar exactly fills it; a tiny count still shows a sliver.
        track.fraction = max(3 / Self.trackWidth, fraction)
    }
}

final class OverviewSection: NSObject, AppSection {
    static let shared = OverviewSection()

    let title = "Overview"
    let symbol = "gauge"
    let refreshesAutomatically = true

    /// Display names for the modality keys the API returns. `capitalized` would
    /// render TTS as "Tts", which reads like a typo.
    private static let modalityLabels: [String: String] = [
        "chat": "Chat",
        "image": "Image",
        "video": "Video",
        "tts": "TTS",
        "transcription": "Transcription",
        "music": "Music",
    ]

    private let statusDot = StatusDot(.tertiaryLabelColor, diameter: 10)
    private var statusLabel = NSTextField(labelWithString: "…")
    private var subtitleLabel = NSTextField(labelWithString: "")
    private var tiles: [String: StatTile] = [:]
    private var modalityStack = NSStackView()
    private var hubBadge = NSStackView()
    private var buildLabel: NSTextField!
    private var guidanceLabel: NSTextField!
    private var activityStack = NSStackView()

    /// The section's content, built once and hosted by the main window.
    /// Lazy so it can be built by a method on self.
    lazy var view: NSView = buildContent()

    /// Called when this section becomes visible.
    ///
    /// Refreshing on activation rather than on a private timer keeps hidden
    /// sections from polling: with everything in one window, exactly one section
    /// is on screen at a time.
    func activated() {
        refresh()
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = PageKit.page()

        // The page's own verbs sit beside its title: the console is the one primary action.
        root.addArrangedSubview(PageKit.heading(
            "Overview",
            "One endpoint in front of every local runner and cloud provider.",
            actions: [
                PageKit.primaryButton("Open Provider Console", target: self, action: #selector(openConsole)),
                PageKit.button("Refresh", target: self, action: #selector(refreshNow)),
            ]
        ))
        root.arrangedSubviews[0].widthAnchor.constraint(equalTo: root.widthAnchor, constant: -2 * PageKit.pagePadding).isActive = true

        // ---- live status line
        statusLabel.font = TextRole.section.font
        subtitleLabel.font = TextRole.secondary.font
        subtitleLabel.textColor = .secondaryLabelColor

        let statusText = NSStackView(views: [statusLabel, subtitleLabel])
        statusText.orientation = .vertical
        statusText.alignment = .leading
        statusText.spacing = 0

        let header = NSStackView(views: [statusDot, statusText])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = DS.Space.m
        root.addArrangedSubview(header)

        // ---- onboarding hint, shown only while there is something to do
        guidanceLabel = NSTextField(wrappingLabelWithString: "")
        guidanceLabel.font = TextRole.secondary.font
        guidanceLabel.textColor = .labelColor
        guidanceLabel.isHidden = true
        guidanceLabel.translatesAutoresizingMaskIntoConstraints = false
        root.addArrangedSubview(guidanceLabel)
        // As wide as the text column when there is room, and as wide as the pane when there is not: a required
        // 620 here would make a window at its minimum width grow.
        guidanceLabel.preferredMaxLayoutWidth = 0
        guidanceLabel.widthAnchor.constraint(lessThanOrEqualToConstant: PageKit.textWidth).isActive = true
        let fit = guidanceLabel.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -2 * PageKit.pagePadding)
        fit.priority = NSLayoutConstraint.Priority(400)
        fit.isActive = true

        // ---- stat tiles
        let tileRow = NSStackView()
        tileRow.orientation = .horizontal
        tileRow.distribution = .fillEqually
        tileRow.spacing = DS.Space.m
        // Neutral figures: a count is not a status, so none of them is coloured. (They used to be accent,
        // green, green and orange, which made "Jobs failed" look like a warning on every Mac with history.)
        for (key, title) in [
            ("providers", "Providers on"),
            ("models", "Models live"),
            ("succeeded", "Jobs succeeded"),
            ("failed", "Jobs failed"),
        ] {
            let tile = StatTile(title: title)
            tiles[key] = tile
            tileRow.addArrangedSubview(tile)
        }
        root.addArrangedSubview(tileRow)
        // The tiles are cards, so they stretch with the window — but on a wide
        // window (this Mac runs the app at 1920pt) four cards would each be over
        // 400pt of mostly empty card, which reads as broken alignment rather than
        // as a dashboard. Cap the row and let the page grow instead.
        //
        // **Below 500, never above.** AppKit gives the window's own size a priority of 500 (the
        // "stay put" priority): a constraint stronger than that is satisfied by *resizing the
        // window*. This one used to be `.defaultHigh` (750) against the 900 cap, so the moment
        // Overview was shown the window snapped to ~1177pt wide whatever size the owner had set,
        // and stayed there on every other page. 400 still wins over the row's own hugging.
        let tileWidth = tileRow.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -2 * PageKit.pagePadding)
        tileWidth.priority = NSLayoutConstraint.Priority(400)
        tileWidth.isActive = true
        tileRow.widthAnchor.constraint(lessThanOrEqualToConstant: 900).isActive = true

        // ---- models by modality
        root.addArrangedSubview(sectionTitle("Offered to Story Studio"))
        modalityStack.orientation = .vertical
        modalityStack.alignment = .leading
        modalityStack.spacing = DS.Space.tight
        root.addArrangedSubview(modalityStack)

        // ---- local runner
        root.addArrangedSubview(sectionTitle("Local runner"))
        hubBadge.orientation = .horizontal
        hubBadge.spacing = DS.Space.s
        hubBadge.alignment = .centerY
        root.addArrangedSubview(hubBadge)

        // ---- recent activity
        root.addArrangedSubview(sectionTitle("Recent activity"))
        activityStack.orientation = .vertical
        activityStack.alignment = .leading
        activityStack.spacing = DS.Space.xs
        root.addArrangedSubview(activityStack)

        // ---- gateway maintenance
        root.addArrangedSubview(sectionTitle("Gateway"))
        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = DS.Space.s
        // "Am I running the latest?" used to be unanswerable from the app, and a
        // stale gateway adopted on launch had no symptom but a feature quietly not
        // working. The first two are that answer.
        actions.addArrangedSubview(button("Check Gateway", #selector(checkGateway)))
        actions.addArrangedSubview(button("Restart Gateway", #selector(restartGateway)))
        actions.addArrangedSubview(button("What's New", #selector(showWhatsNew)))
        actions.addArrangedSubview(button("Open Log", #selector(openLog)))
        actions.addArrangedSubview(button("Open Data Folder", #selector(openData)))
        root.addArrangedSubview(actions)

        // The build check reports here, next to the other status text rather than
        // in a dialog: an answer you have to dismiss is one you stop asking for.
        buildLabel = NSTextField(labelWithString: "")
        buildLabel.font = TextRole.secondary.font
        buildLabel.textColor = .secondaryLabelColor
        buildLabel.lineBreakMode = .byTruncatingTail
        root.addArrangedSubview(buildLabel)

        // Scrolls, for the same reason the Fleet page does: this content has a
        // required height (stat tiles, modality rows, activity) which, pinned
        // straight to the host, propagates outwards and grows the window.
        // Measured at 756pt inside a 700pt window — small here, but the same
        // defect, and it is the page's job to fit the window rather than the
        // window's to fit the page.
        let document = FlippedView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(root)

        let pageScroll = NSScrollView()
        pageScroll.hasVerticalScroller = true
        pageScroll.drawsBackground = false
        pageScroll.translatesAutoresizingMaskIntoConstraints = false
        pageScroll.documentView = document

        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: document.topAnchor),
            root.leadingAnchor.constraint(equalTo: document.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: document.trailingAnchor),
            root.bottomAnchor.constraint(equalTo: document.bottomAnchor),

            document.leadingAnchor.constraint(equalTo: pageScroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: pageScroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: pageScroll.contentView.topAnchor),
        ])

        let container = NSView()
        container.addSubview(pageScroll)
        NSLayoutConstraint.activate([
            pageScroll.topAnchor.constraint(equalTo: container.topAnchor),
            pageScroll.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            pageScroll.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            pageScroll.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        return container
    }

    /// Sentence case at `title3` weight, matching Studio Hub's `sectionTitle`.
    /// It used to be 10.5pt uppercase tertiary, which read as a field label
    /// rather than a heading and did not match the sibling app.
    private func sectionTitle(_ text: String) -> NSTextField {
        PageKit.sectionTitle(text)
    }

    private func button(_ title: String, _ action: Selector) -> NSButton {
        PageKit.button(title, target: self, action: action)
    }

    // MARK: Data

    @objc func refreshNow() { refresh() }

    func refresh() {
        switch GatewayController.shared.state {
        case .running:
            statusDot.colour = Health.ok.tint
            statusLabel.stringValue = "Gateway running"
        case .starting:
            statusDot.colour = Health.warn.tint
            statusLabel.stringValue = "Gateway starting…"
        case .stopped:
            statusDot.colour = Health.off.tint
            statusLabel.stringValue = "Gateway stopped"
        case .failed(let reason):
            statusDot.colour = Health.bad.tint
            statusLabel.stringValue = reason
        }

        GatewayAPI.get("/admin/api/status") { [weak self] result in
            guard let self else { return }
            guard case .success(let payload) = result else {
                // Say why: a 401 after a key change and a gateway that is not there are
                // different problems. Only a gateway that is starting is "waiting".
                if case .failure(let error) = result, GatewayController.shared.isRunning {
                    self.subtitleLabel.stringValue = error.localizedDescription
                } else {
                    self.subtitleLabel.stringValue = "Waiting for the gateway…"
                }
                // The tiles, the modality bars and the hub lines all kept their
                // previous values, so a failed read showed stale numbers beside a
                // message saying the gateway was not answering. Blank them: an
                // em-dash is honest, a number from an earlier read is not.
                self.clearStats()
                return
            }

            let providers = payload["providers"] as? [String: Any] ?? [:]
            let models = payload["models"] as? [String: Any] ?? [:]
            let jobs = payload["jobs"] as? [String: Any] ?? [:]
            let byState = jobs["by_state"] as? [String: Any] ?? [:]
            let uptime = payload["uptime_seconds"] as? Int ?? 0

            self.subtitleLabel.stringValue =
                "127.0.0.1:\(GatewayController.shared.port) · up \(GatewayAPI.uptime(uptime))"

            // Onboarding: say what to do next, rather than showing zeros and
            // leaving a new owner to work it out.
            let configured = providers["configured"] as? Int ?? 0
            // `publishable`, not `enabled`: a model the gateway withholds — a
            // package the Hub's catalogue does not describe as a model, or one it
            // says does not fit this machine — is switched on but cannot be
            // picked, so counting it here read "121 live" with nine unofferable.
            let liveModels = models["publishable_models"] as? Int ?? 0
            if configured == 0 {
                self.guidanceLabel.stringValue =
                    "Getting started — no provider keys yet. Open the Provider Console and paste "
                    + "a key; that provider's models then become available to enable and to Story Studio."
                self.guidanceLabel.isHidden = false
            } else if liveModels == 0 {
                self.guidanceLabel.stringValue =
                    "\(configured) provider\(configured == 1 ? "" : "s") configured, but no models "
                    + "enabled. Open the Provider Console → Models tab and tick what Story Studio should offer."
                self.guidanceLabel.isHidden = false
            } else {
                self.guidanceLabel.isHidden = true
            }

            self.tiles["providers"]?.set(
                value: "\(providers["enabled"] as? Int ?? 0)/\(providers["total"] as? Int ?? 0)",
                title: "Providers on"
            )
            // The withheld count is shown rather than dropped: a model that is
            // switched on and silently absent from the picker is the one thing an
            // operator cannot diagnose from this page.
            //
            // **"of N enabled", not "of N discovered".** The denominator is the
            // models that are switched on, because that is the set `withheld` is
            // taken from — offered + withheld = enabled, so the sentence adds up.
            // It used to read `total_models` under the word "discovered", and that
            // number is a different question: discovered is *every* model on every
            // provider, so switching a provider off made the "discovered" count
            // fall, which is the opposite of what the word means.
            let withheld = models["withheld_models"] as? Int ?? 0
            let switchedOn = models["enabled_models"] as? Int ?? 0
            self.tiles["models"]?.set(
                value: "\(liveModels)",
                title: withheld > 0
                    ? "Offered of \(switchedOn) enabled · \(withheld) withheld"
                    : "Offered of \(switchedOn) enabled"
            )
            self.tiles["succeeded"]?.set(
                value: "\(byState["succeeded"] as? Int ?? 0)", title: "Jobs succeeded"
            )
            self.tiles["failed"]?.set(
                value: "\(byState["failed"] as? Int ?? 0)", title: "Jobs failed"
            )

            // modality breakdown
            let catalogCounts = payload["catalog"] as? [String: Any] ?? [:]
            // Filtering a Dictionary yields (key, value) pairs, not values.
            let counts = catalogCounts.compactMapValues { $0 as? Int }.filter { $0.value > 0 }
            let maximum = counts.values.max() ?? 1
            self.modalityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            if counts.isEmpty {
                self.modalityStack.addArrangedSubview(
                    self.dim("No models enabled yet. Use the console to switch some on.")
                )
            } else {
                for modality in ["chat", "image", "video", "tts", "transcription", "music"] {
                    guard let value = counts[modality], value > 0 else { continue }
                    let bar = ModalityBar()
                    bar.set(
                        label: Self.modalityLabels[modality] ?? modality,
                        value: value,
                        maxValue: maximum
                    )
                    bar.translatesAutoresizingMaskIntoConstraints = false
                    bar.widthAnchor.constraint(equalToConstant: ModalityBar.rowWidth).isActive = true
                    self.modalityStack.addArrangedSubview(bar)
                }
            }

            // hub
            //
            // `running` means the Hub **answered** `/api/health`, not that a
            // `hub-ready.json` exists. The two ways it can be false are different
            // problems with different fixes, and the gateway says which one it is:
            // nothing published a ready-file at all, versus a ready-file pointing
            // at something that did not answer (a crash, a restart, a stale file).
            // Collapsing them into "not running" sends the owner to start a Hub
            // that is already starting.
            let hub = payload["hub"] as? [String: Any] ?? [:]
            if hub["running"] as? Bool == true {
                self.setHub(.ok, "Running", hub["base_url"] as? String ?? "")
            } else if hub["advertised"] as? Bool == true {
                self.setHub(.warn, "Not answering",
                            hub["probe"] as? String ?? "It published a ready-file but did not answer.")
            } else {
                self.setHub(.off, "Not running", "Start Studio Hub to use local models.")
            }

            self.loadActivity()
        }
    }

    /// Blank everything the status read fills, so a failed read cannot leave a
    /// previous read's numbers on screen looking current.
    private func clearStats() {
        for tile in tiles.values {
            tile.blank()
        }
        modalityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
        hubBadge.arrangedSubviews.forEach { $0.removeFromSuperview() }
        guidanceLabel.isHidden = true
        activityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
    }

    /// The local runner as a state badge and the Hub's own sentence beside it.
    private func setHub(_ health: Health, _ state: String, _ detail: String) {
        hubBadge.arrangedSubviews.forEach { $0.removeFromSuperview() }
        let text = PageKit.label(detail, .secondary, colour: .secondaryLabelColor)
        text.lineBreakMode = .byTruncatingTail
        hubBadge.addArrangedSubview(Badge(state, health, symbol: true))
        hubBadge.addArrangedSubview(text)
    }

    private func loadActivity() {
        GatewayAPI.get("/admin/api/jobs?limit=5") { [weak self] result in
            guard let self else { return }
            self.activityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            switch result {
            case .failure(let error):
                let label = self.dim(
                    "Could not load recent activity — \(error.localizedDescription). Use Refresh to try again."
                )
                label.textColor = Health.warn.text
                self.activityStack.addArrangedSubview(label)
                return
            case .success(let payload):
                guard let rows = payload["jobs"] as? [[String: Any]] else {
                    let label = self.dim(
                        "Could not read recent activity — the gateway returned an unexpected response."
                    )
                    label.textColor = Health.warn.text
                    self.activityStack.addArrangedSubview(label)
                    return
                }
                guard !rows.isEmpty else {
                    self.activityStack.addArrangedSubview(self.dim("No jobs yet."))
                    return
                }
                self.addActivityRows(rows)
            }
        }
    }

    private func addActivityRows(_ rows: [[String: Any]]) {
        for job in rows {
            let state = job["state"] as? String ?? "?"
            let symbol: String
            switch state {
            case "succeeded": symbol = "✓"
            case "failed": symbol = "✗"
            case "running", "starting", "accepted": symbol = "•"
            default: symbol = "–"
            }
            let colour = Health(state: state).text

            let line = NSMutableAttributedString()
            line.append(NSAttributedString(string: "\(symbol) ", attributes: [
                .foregroundColor: colour, .font: TextRole.rowTitle.font,
            ]))
            line.append(NSAttributedString(
                string: "\(job["model_name"] as? String ?? "?")  ",
                attributes: [.font: TextRole.secondary.font]
            ))
            let kind = job["modality"] as? String ?? ""
            let detail = "\(Self.modalityLabels[kind.lowercased()] ?? kind) · \(state) · "
                + "\(GatewayAPI.duration(job["duration_s"] as? Double))"
            line.append(NSAttributedString(string: detail, attributes: [
                .font: TextRole.caption.font, .foregroundColor: NSColor.tertiaryLabelColor,
            ]))

            let label = NSTextField(labelWithAttributedString: line)
            label.lineBreakMode = .byTruncatingTail
            label.translatesAutoresizingMaskIntoConstraints = false
            label.widthAnchor.constraint(lessThanOrEqualToConstant: PageKit.textWidth).isActive = true
            activityStack.addArrangedSubview(label)
        }
    }

    private func dim(_ text: String) -> NSTextField {
        PageKit.label(text, .secondary, colour: .secondaryLabelColor)
    }

    // MARK: Actions

    @objc private func checkGateway() {
        buildLabel.textColor = .secondaryLabelColor
        buildLabel.stringValue = "Checking the gateway…"
        GatewayController.shared.buildReport { [weak self] message, ok in
            guard let self else { return }
            self.buildLabel.textColor = ok ? .secondaryLabelColor : Health.warn.text
            self.buildLabel.stringValue = message
        }
    }

    /// Stop whatever holds the port and start this build.
    ///
    /// Deliberately not `restart()`: that could only stop a process it already knew
    /// about, so a gateway started before this app ran survived a "restart" and was
    /// adopted again. This kills the port holder by pid and then says what happened.
    @objc private func restartGateway() {
        GatewayController.shared.confirmIfBusy("Restart") { [weak self] ok in
            if ok { self?.restartConfirmed() }
        }
    }

    private func restartConfirmed() {
        buildLabel.textColor = .secondaryLabelColor
        let killed = GatewayController.shared.killPortHolders()
        buildLabel.stringValue = killed.isEmpty
            ? "Restarting the gateway…"
            : "Stopped \(killed.count) process\(killed.count == 1 ? "" : "es") on port "
              + "\(GatewayController.shared.port); restarting…"
        GatewayController.shared.restartCleanly()
        // Long enough for the new process to answer, short enough to feel like a
        // button and not a spinner.
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.0) { [weak self] in
            self?.checkGateway()
        }
    }

    @objc private func showWhatsNew() { WhatsNewWindow.shared.show() }

    @objc private func openConsole() { MainWindow.shared.show(section: "providers") }

    @objc private func openLog() {
        NSWorkspace.shared.selectFile(GatewayController.shared.logPath, inFileViewerRootedAtPath: "")
    }

    @objc private func openData() {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: AppPaths.data.path)
    }
}
