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
}

/// A labelled proportional bar, for the per-modality breakdown.
final class ModalityBar: NSView {
    private let name = NSTextField(labelWithString: "")
    private let count = NSTextField(labelWithString: "")
    private let bar = NSView()
    private var barWidth: NSLayoutConstraint!

    init() {
        super.init(frame: .zero)
        name.font = .systemFont(ofSize: 12)
        count.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        count.textColor = .secondaryLabelColor
        count.alignment = .right

        let track = NSView()
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
            name.leadingAnchor.constraint(equalTo: leadingAnchor),
            name.widthAnchor.constraint(equalToConstant: 96),
            name.centerYAnchor.constraint(equalTo: centerYAnchor),

            track.leadingAnchor.constraint(equalTo: name.trailingAnchor, constant: 8),
            track.centerYAnchor.constraint(equalTo: centerYAnchor),
            track.heightAnchor.constraint(equalToConstant: 6),

            count.leadingAnchor.constraint(equalTo: track.trailingAnchor, constant: 8),
            count.trailingAnchor.constraint(equalTo: trailingAnchor),
            count.widthAnchor.constraint(equalToConstant: 44),
            count.centerYAnchor.constraint(equalTo: centerYAnchor),

            bar.leadingAnchor.constraint(equalTo: track.leadingAnchor),
            bar.topAnchor.constraint(equalTo: track.topAnchor),
            bar.bottomAnchor.constraint(equalTo: track.bottomAnchor),
            barWidth,
        ])
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    func set(label: String, value: Int, maxValue: Int) {
        name.stringValue = label.capitalized
        count.stringValue = "\(value)"
        let fraction = maxValue > 0 ? CGFloat(value) / CGFloat(maxValue) : 0
        barWidth.constant = max(2, 150 * fraction)
    }
}

final class OverviewWindow: NSObject {
    static let shared = OverviewWindow()

    private var window: NSWindow?
    private var statusDot = NSView()
    private var statusLabel = NSTextField(labelWithString: "…")
    private var subtitleLabel = NSTextField(labelWithString: "")
    private var tiles: [String: StatTile] = [:]
    private var modalityStack = NSStackView()
    private var hubLabel = NSTextField(labelWithString: "—")
    private var activityStack = NSStackView()
    private var timer: Timer?

    func show() {
        if let existing = window {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            refresh()
            return
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 660, height: 640),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "gateway-kh — Overview"
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("GatewayOverviewWindow")
        window.center()
        window.contentView = buildContent()

        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self, self.window?.isVisible == true else { return }
            self.refresh()
        }
        RunLoop.main.add(timer!, forMode: .common)
    }

    // MARK: Layout

    private func buildContent() -> NSView {
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 14
        root.edgeInsets = NSEdgeInsets(top: 18, left: 20, bottom: 18, right: 20)
        root.translatesAutoresizingMaskIntoConstraints = false

        // ---- status header
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

        // ---- stat tiles
        let tileRow = NSStackView()
        tileRow.orientation = .horizontal
        tileRow.distribution = .fillEqually
        tileRow.spacing = 10
        for (key, title, colour) in [
            ("providers", "Providers on", NSColor.controlAccentColor),
            ("models", "Models live", NSColor.systemGreen),
            ("succeeded", "Jobs succeeded", NSColor.systemTeal),
            ("failed", "Jobs failed", NSColor.systemOrange),
        ] {
            let tile = StatTile(title: title, accent: colour)
            tiles[key] = tile
            tileRow.addArrangedSubview(tile)
        }
        root.addArrangedSubview(tileRow)
        tileRow.widthAnchor.constraint(equalTo: root.widthAnchor, constant: -40).isActive = true

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
        actions.addArrangedSubview(button("Open Log", #selector(openLog)))
        actions.addArrangedSubview(button("Open Data Folder", #selector(openData)))
        root.addArrangedSubview(actions)

        let container = NSView()
        container.addSubview(root)
        NSLayoutConstraint.activate([
            root.topAnchor.constraint(equalTo: container.topAnchor),
            root.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            root.bottomAnchor.constraint(lessThanOrEqualTo: container.bottomAnchor),
        ])
        return container
    }

    private func sectionTitle(_ text: String) -> NSTextField {
        let label = NSTextField(labelWithString: text.uppercased())
        label.font = .systemFont(ofSize: 10.5, weight: .semibold)
        label.textColor = .tertiaryLabelColor
        return label
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
        guard let window, window.isVisible else { return }

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
            guard let self, let window = self.window, window.isVisible else { return }
            guard case .success(let payload) = result else {
                self.subtitleLabel.stringValue = "Waiting for the gateway…"
                return
            }

            let providers = payload["providers"] as? [String: Any] ?? [:]
            let models = payload["models"] as? [String: Any] ?? [:]
            let jobs = payload["jobs"] as? [String: Any] ?? [:]
            let byState = jobs["by_state"] as? [String: Any] ?? [:]
            let uptime = payload["uptime_seconds"] as? Int ?? 0

            self.subtitleLabel.stringValue =
                "127.0.0.1:\(GatewayController.shared.port) · up \(GatewayAPI.uptime(uptime))"

            self.tiles["providers"]?.set(
                value: "\(providers["enabled"] as? Int ?? 0)/\(providers["total"] as? Int ?? 0)",
                title: "Providers on"
            )
            self.tiles["models"]?.set(
                value: "\(models["enabled_models"] as? Int ?? 0)",
                title: "Live of \(models["total_models"] as? Int ?? 0) discovered"
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
                    bar.set(label: modality, value: value, maxValue: maximum)
                    bar.translatesAutoresizingMaskIntoConstraints = false
                    bar.widthAnchor.constraint(equalToConstant: 300).isActive = true
                    self.modalityStack.addArrangedSubview(bar)
                }
            }

            // hub
            let hub = payload["hub"] as? [String: Any] ?? [:]
            if hub["running"] as? Bool == true {
                self.hubLabel.stringValue =
                    "Studio Hub is running — \(hub["base_url"] as? String ?? "")"
            } else {
                self.hubLabel.stringValue = "Studio Hub is not running — start it to use local models."
            }

            self.loadActivity()
        }
    }

    private func loadActivity() {
        GatewayAPI.get("/admin/api/jobs?limit=5") { [weak self] result in
            guard let self, let window = self.window, window.isVisible else { return }
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

    @objc private func openConsole() { ConsoleWindow.shared.show() }

    @objc private func openLog() {
        NSWorkspace.shared.selectFile(GatewayController.shared.logPath, inFileViewerRootedAtPath: "")
    }

    @objc private func openData() {
        let path = URL(fileURLWithPath: GatewayController.shared.gatewayDir)
            .appendingPathComponent("data").path
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }
}
