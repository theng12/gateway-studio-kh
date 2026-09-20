// The Overview window: what the app is doing, at a glance.
//
// This is the piece that turns a menu bar toggle into an application. It reads
// only from the gateway's own APIs, so what it shows is what the service
// actually reports rather than what the app hopes is true.

import Cocoa

/// A boxed number with a caption. Used for the headline stats.
final class StatTile: NSView {
    private let valueLabel = NSTextField(labelWithString: "—")
    private let titleLabel = NSTextField(labelWithString: "")
    private let accent: NSColor

    init(title: String, accent: NSColor) {
        self.accent = accent
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        layer?.cornerRadius = 10
        layer?.borderWidth = 1
        layer?.borderColor = NSColor.separatorColor.cgColor

        valueLabel.font = .monospacedDigitSystemFont(ofSize: 26, weight: .semibold)
        valueLabel.textColor = accent
        titleLabel.font = .systemFont(ofSize: 11, weight: .medium)
        titleLabel.textColor = .secondaryLabelColor
        titleLabel.lineBreakMode = .byTruncatingTail

        let stack = NSStackView(views: [valueLabel, titleLabel])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 1
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 12),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -12),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            heightAnchor.constraint(equalToConstant: 66),
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
    static let rowWidth = nameWidth + 8 + trackWidth + 8 + countWidth

    private let name = NSTextField(labelWithString: "")
    private let count = NSTextField(labelWithString: "")
    private let track = NSView()
    private let bar = NSView()
    private var barWidth: NSLayoutConstraint!

    init() {
        super.init(frame: .zero)
        name.font = .systemFont(ofSize: 12)
        name.lineBreakMode = .byTruncatingTail
        name.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        count.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        count.textColor = .secondaryLabelColor
        count.alignment = .right

        track.wantsLayer = true
        track.layer?.backgroundColor = NSColor.quaternaryLabelColor.cgColor
        track.layer?.cornerRadius = 3
        bar.wantsLayer = true
        bar.layer?.backgroundColor = NSColor.controlAccentColor.cgColor
        bar.layer?.cornerRadius = 3
        track.addSubview(bar)

        for view in [name, count, track] {
            view.translatesAutoresizingMaskIntoConstraints = false
            addSubview(view)
        }
        bar.translatesAutoresizingMaskIntoConstraints = false
        barWidth = bar.widthAnchor.constraint(equalToConstant: 0)

        NSLayoutConstraint.activate([
            // Without this the row has no height and the stack view crushes it.
            heightAnchor.constraint(equalToConstant: Self.rowHeight),

            name.leadingAnchor.constraint(equalTo: leadingAnchor),
            name.widthAnchor.constraint(equalToConstant: Self.nameWidth),
            name.centerYAnchor.constraint(equalTo: centerYAnchor),

            track.leadingAnchor.constraint(equalTo: name.trailingAnchor, constant: 8),
            track.widthAnchor.constraint(equalToConstant: Self.trackWidth),
            track.heightAnchor.constraint(equalToConstant: 7),
            track.centerYAnchor.constraint(equalTo: centerYAnchor),

            count.leadingAnchor.constraint(equalTo: track.trailingAnchor, constant: 8),
            count.widthAnchor.constraint(equalToConstant: Self.countWidth),
            count.centerYAnchor.constraint(equalTo: centerYAnchor),

            bar.leadingAnchor.constraint(equalTo: track.leadingAnchor),
            bar.topAnchor.constraint(equalTo: track.topAnchor),
            bar.bottomAnchor.constraint(equalTo: track.bottomAnchor),
            barWidth,
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func set(label: String, value: Int, maxValue: Int) {
        name.stringValue = label
        count.stringValue = "\(value)"
        let fraction = maxValue > 0 ? CGFloat(value) / CGFloat(maxValue) : 0
        // Proportional to the real track, so a full bar exactly fills it.
        barWidth.constant = max(3, Self.trackWidth * fraction)
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

    private var statusDot = NSView()
    private var statusLabel = NSTextField(labelWithString: "…")
    private var subtitleLabel = NSTextField(labelWithString: "")
    private var tiles: [String: StatTile] = [:]
    private var modalityStack = NSStackView()
    private var hubLabel = NSTextField(labelWithString: "—")
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

        root.addArrangedSubview(PageKit.heading(
            "Overview",
            "One endpoint in front of every local runner and cloud provider."
        ))

        // ---- live status line
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 5
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusDot.widthAnchor.constraint(equalToConstant: 10).isActive = true
        statusDot.heightAnchor.constraint(equalToConstant: 10).isActive = true

        statusLabel.font = .systemFont(ofSize: 17, weight: .semibold)
        subtitleLabel.font = .systemFont(ofSize: 12)
        subtitleLabel.textColor = .secondaryLabelColor

        let statusText = NSStackView(views: [statusLabel, subtitleLabel])
        statusText.orientation = .vertical
        statusText.alignment = .leading
        statusText.spacing = 0

        let header = NSStackView(views: [statusDot, statusText])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 10
        root.addArrangedSubview(header)

        // ---- onboarding hint, shown only while there is something to do
        guidanceLabel = NSTextField(wrappingLabelWithString: "")
        guidanceLabel.font = .systemFont(ofSize: 12)
        guidanceLabel.textColor = .labelColor
        guidanceLabel.isHidden = true
        guidanceLabel.translatesAutoresizingMaskIntoConstraints = false
        guidanceLabel.widthAnchor.constraint(equalToConstant: 580).isActive = true
        root.addArrangedSubview(guidanceLabel)

        // ---- stat tiles
        let tileRow = NSStackView()
        tileRow.orientation = .horizontal
        tileRow.distribution = .fillEqually
        tileRow.spacing = 10
        // One palette, three meanings: accent for "on offer", green for "good",
        // orange for "needs attention". "Jobs succeeded" used to be teal next to
        // a green "Models live", which were two different colours for the same
        // idea and read as an accident. The dot colours in the status line use
        // the same meanings (green / yellow starting / red failed).
        for (key, title, colour) in [
            ("providers", "Providers on", NSColor.controlAccentColor),
            ("models", "Models live", NSColor.systemGreen),
            ("succeeded", "Jobs succeeded", NSColor.systemGreen),
            ("failed", "Jobs failed", NSColor.systemOrange),
        ] {
            let tile = StatTile(title: title, accent: colour)
            tiles[key] = tile
            tileRow.addArrangedSubview(tile)
        }
        root.addArrangedSubview(tileRow)
        // The tiles are cards, so they stretch with the window — but on a wide
        // window (this Mac runs the app at 1920pt) four cards would each be over
        // 400pt of mostly empty card, which reads as broken alignment rather than
        // as a dashboard. Cap the row and let the page grow instead.
        let tileWidth = tileRow.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -56)
        tileWidth.priority = .defaultHigh
        tileWidth.isActive = true
        tileRow.widthAnchor.constraint(lessThanOrEqualToConstant: 900).isActive = true

        // ---- models by modality
        root.addArrangedSubview(sectionTitle("Offered to Story Studio"))
        modalityStack.orientation = .vertical
        modalityStack.alignment = .leading
        modalityStack.spacing = 6
        root.addArrangedSubview(modalityStack)

        // ---- local runner
        root.addArrangedSubview(sectionTitle("Local runner"))
        hubLabel.font = .systemFont(ofSize: 12)
        root.addArrangedSubview(hubLabel)

        // ---- recent activity
        root.addArrangedSubview(sectionTitle("Recent activity"))
        activityStack.orientation = .vertical
        activityStack.alignment = .leading
        activityStack.spacing = 3
        root.addArrangedSubview(activityStack)

        // ---- actions
        let actions = NSStackView()
        actions.orientation = .horizontal
        actions.spacing = 8
        actions.addArrangedSubview(button("Open Provider Console", #selector(openConsole)))
        actions.addArrangedSubview(button("Refresh", #selector(refreshNow)))
        // "Am I running the latest?" used to be unanswerable from the app, and a
        // stale gateway adopted on launch had no symptom but a feature quietly not
        // working. These two are that answer.
        actions.addArrangedSubview(button("Check Gateway", #selector(checkGateway)))
        actions.addArrangedSubview(button("Restart Gateway", #selector(restartGateway)))
        actions.addArrangedSubview(button("What's New", #selector(showWhatsNew)))
        actions.addArrangedSubview(button("Open Log", #selector(openLog)))
        actions.addArrangedSubview(button("Open Data Folder", #selector(openData)))
        root.addArrangedSubview(actions)

        // The build check reports here, next to the other status text rather than
        // in a dialog: an answer you have to dismiss is one you stop asking for.
        buildLabel = NSTextField(labelWithString: "")
        buildLabel.font = .systemFont(ofSize: 11.5)
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
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        b.controlSize = .small
        return b
    }

    // MARK: Data

    @objc func refreshNow() { refresh() }

    func refresh() {
        switch GatewayController.shared.state {
        case .running:
            statusDot.layer?.backgroundColor = NSColor.systemGreen.cgColor
            statusLabel.stringValue = "Gateway running"
        case .starting:
            statusDot.layer?.backgroundColor = NSColor.systemYellow.cgColor
            statusLabel.stringValue = "Gateway starting…"
        case .stopped:
            statusDot.layer?.backgroundColor = NSColor.tertiaryLabelColor.cgColor
            statusLabel.stringValue = "Gateway stopped"
        case .failed(let reason):
            statusDot.layer?.backgroundColor = NSColor.systemRed.cgColor
            statusLabel.stringValue = reason
        }

        GatewayAPI.get("/admin/api/status") { [weak self] result in
            guard let self else { return }
            guard case .success(let payload) = result else {
                self.subtitleLabel.stringValue = "Waiting for the gateway…"
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
                self.hubLabel.stringValue =
                    "Studio Hub is running — \(hub["base_url"] as? String ?? "")"
            } else if hub["advertised"] as? Bool == true {
                self.hubLabel.stringValue = "Studio Hub is not answering — "
                    + (hub["probe"] as? String ?? "it published a ready-file but did not answer.")
            } else {
                self.hubLabel.stringValue = "Studio Hub is not running — start it to use local models."
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
        hubLabel.stringValue = "—"
        guidanceLabel.isHidden = true
        activityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
    }

    private func loadActivity() {
        GatewayAPI.get("/admin/api/jobs?limit=5") { [weak self] result in
            guard let self else { return }
            self.activityStack.arrangedSubviews.forEach { $0.removeFromSuperview() }
            guard case .success(let payload) = result,
                  let rows = payload["jobs"] as? [[String: Any]], !rows.isEmpty else {
                self.activityStack.addArrangedSubview(self.dim("No jobs yet."))
                return
            }
            for job in rows {
                let state = job["state"] as? String ?? "?"
                let symbol: String
                switch state {
                case "succeeded": symbol = "✓"
                case "failed": symbol = "✗"
                case "running", "starting", "accepted": symbol = "•"
                default: symbol = "–"
                }
                let colour: NSColor
                switch state {
                case "succeeded": colour = .systemGreen
                case "failed": colour = .systemRed
                default: colour = .secondaryLabelColor
                }

                let line = NSMutableAttributedString()
                line.append(NSAttributedString(string: "\(symbol) ", attributes: [
                    .foregroundColor: colour, .font: NSFont.systemFont(ofSize: 12, weight: .bold),
                ]))
                line.append(NSAttributedString(
                    string: "\(job["model_name"] as? String ?? "?")  ",
                    attributes: [.font: NSFont.systemFont(ofSize: 12)]
                ))
                let detail = "\(job["modality"] as? String ?? "") · \(state) · "
                    + "\(GatewayAPI.duration(job["duration_s"] as? Double))"
                line.append(NSAttributedString(string: detail, attributes: [
                    .font: NSFont.systemFont(ofSize: 11), .foregroundColor: NSColor.tertiaryLabelColor,
                ]))

                let label = NSTextField(labelWithAttributedString: line)
                label.lineBreakMode = .byTruncatingTail
                label.translatesAutoresizingMaskIntoConstraints = false
                label.widthAnchor.constraint(lessThanOrEqualToConstant: 580).isActive = true
                self.activityStack.addArrangedSubview(label)
            }
        }
    }

    private func dim(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text)
        label.font = .systemFont(ofSize: 12)
        label.textColor = .tertiaryLabelColor
        return label
    }

    // MARK: Actions

    @objc private func checkGateway() {
        buildLabel.textColor = .secondaryLabelColor
        buildLabel.stringValue = "Checking the gateway…"
        GatewayController.shared.buildReport { [weak self] message, ok in
            guard let self else { return }
            self.buildLabel.textColor = ok ? .secondaryLabelColor : .systemOrange
            self.buildLabel.stringValue = message
        }
    }

    /// Stop whatever holds the port and start this build.
    ///
    /// Deliberately not `restart()`: that could only stop a process it already knew
    /// about, so a gateway started before this app ran survived a "restart" and was
    /// adopted again. This kills the port holder by pid and then says what happened.
    @objc private func restartGateway() {
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
