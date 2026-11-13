import AppKit

/// Settings, as a page in the main window.
///
/// This was its own window, on the reasoning that macOS puts Settings in the
/// app menu. That reasoning failed for *this* app while it was an accessory
/// (`LSUIElement`) process: its app menu was never on screen, and the only
/// routes in were `⌘,` and the bolt menu — the owner went looking and could not
/// find Settings at all. The app is now a regular app with a Dock icon and an
/// app menu, but Settings stays a sidebar page, because Studio Hub KH — the
/// sibling app this one is modelled on — lists Settings in the sidebar under
/// "Library & app", and that is where people actually look. The app menu's `⌘,`
/// still reaches it.
///
/// The controls are the same ones the window had; only the home changed.
final class SettingsSection: NSObject, AppSection {
    static let shared = SettingsSection()

    let title = "Settings"
    let symbol = "gearshape"
    /// Nothing here is live: re-reading it would fight a half-typed port.
    let refreshesAutomatically = false

    private var runtimeLabel: NSTextField!
    private var appearanceControl: NSSegmentedControl!
    private var appearanceDetail: NSTextField!
    private var dataLabel: NSTextField!
    private var portField: NSTextField!
    private var keyField: SecretField!
    private var storyBaseField: NSTextField!
    private var retentionDaysField: NSTextField!
    private var retentionGBField: NSTextField!
    private var trashDaysField: NSTextField!
    private var trashGBField: NSTextField!
    private var retentionUsageLabel: NSTextField!
    private var loginCheck: NSButton!
    private var autoStartCheck: NSButton!
    private var keepRunningCheck: NSButton!
    private var pairingCheck: NSButton!
    private var pairingStatus: NSTextField!
    private var statusLabel: NSTextField!
    private let telegram = TelegramSettings()
    private let alerts = AlertsSettings()
    private let browserPassword = BrowserPasswordSettings()
    private let backups = BackupPanel()

    /// What the retention fields last showed from the gateway, to tell a change from a re-read.
    private var loadedRetention: [String: Double] = [:]
    /// What `fill` last wrote into a field. A field that no longer says that has been typed
    /// into, and a re-read on activation must not put the old value back over it.
    private var written: [ObjectIdentifier: String] = [:]

    lazy var view: NSView = buildContent()

    func activated() {
        refresh()
    }

    // MARK: Layout

    /// Every text field on the page is one of two widths, so the controls line up down the page.
    private static let fieldWidth: CGFloat = 420
    private static let numberWidth: CGFloat = 100

    /// A titled card holding a section's rows. Sections are full width, 24 apart, like every other page.
    private func section(_ title: String, _ rows: [NSView], in root: NSStackView) {
        let inner = NSStackView(views: rows)
        inner.orientation = .vertical
        inner.alignment = .leading
        inner.spacing = DS.Space.l
        for row in rows where row is NSBox {
            row.widthAnchor.constraint(equalTo: inner.widthAnchor).isActive = true
        }
        let card = CardView()
        card.hold(inner, inset: NSEdgeInsets(top: DS.Space.l, left: DS.Space.l, bottom: DS.Space.l, right: DS.Space.l))
        let group = NSStackView(views: [PageKit.sectionTitle(title), card])
        group.orientation = .vertical
        group.alignment = .leading
        group.spacing = DS.Space.m
        card.widthAnchor.constraint(equalTo: group.widthAnchor).isActive = true
        PageKit.addFull(group, to: root)
    }

    private func buildContent() -> NSView {
        let root = PageKit.page()

        root.addArrangedSubview(PageKit.heading(
            "Settings",
            "The gateway's port and Gateway API Key, and whether it starts with this Mac."
        ))

        // ---- gateway
        runtimeLabel = PageKit.label(
            GatewayController.shared.runtimeDescription, .secondary,
            colour: .secondaryLabelColor, wrapping: true
        )
        dataLabel = PageKit.label(AppPaths.data.path, .mono, colour: .secondaryLabelColor)
        dataLabel.lineBreakMode = .byTruncatingMiddle
        dataLabel.widthAnchor.constraint(equalToConstant: Self.fieldWidth).isActive = true

        portField = NSTextField(string: String(ConfigStore.shared.config.port))
        portField.widthAnchor.constraint(equalToConstant: Self.numberWidth).isActive = true
        section("Gateway", [
            PageKit.formRow("Runtime", runtimeLabel),
            PageKit.formRow("Data folder", dataLabel),
            PageKit.formRow("Port", [
                portField,
                PageKit.help("Changing this restarts the gateway. Story Studio must match."),
            ]),
        ], in: root)

        // ---- appearance
        appearanceControl = NSSegmentedControl(labels: AppearanceMode.allCases.map(\.title),
                                               trackingMode: .selectOne,
                                               target: self, action: #selector(changeAppearance))
        appearanceControl.setAccessibilityLabel("Appearance")
        appearanceDetail = PageKit.label(AppearanceMode.current.detail, .secondary,
                                         colour: .secondaryLabelColor, wrapping: true)
        appearanceControl.selectedSegment = AppearanceMode.allCases.firstIndex(of: .current) ?? 0
        section("Appearance", [appearanceControl, appearanceDetail], in: root)

        // ---- API key
        // Masked like every secret in the app: the eye shows it, Copy works without showing it.
        keyField = SecretField(EnvFile.apiKey(), width: Self.fieldWidth)
        let keyButtons = PageKit.row([
            PageKit.button("Copy", target: self, action: #selector(copyKey)),
            PageKit.button("Regenerate", target: self, action: #selector(regenerateKey)),
        ])
        section("Gateway API Key", [
            PageKit.help(
                "The password Story Studio uses to reach this Gateway. Keep it private; Regenerate if it leaks, then paste the new one into each Story Mac."
            ),
            PageKit.formRow("Key", [keyField, keyButtons] as [NSView]),
        ], in: root)

        // ---- where Story Studio should point
        //
        // This used to be nowhere: the status line, the console link and the About
        // panel all hardcoded 127.0.0.1, which is right only while Story Studio is
        // on this Mac. Bound beyond loopback the useful address is the Tailscale
        // one, and the owner should not have to read it off a terminal to paste it
        // into the other machine.
        storyBaseField = NSTextField(labelWithString:
            LocalAddresses.storyBaseURL(port: ConfigStore.shared.config.port))
        storyBaseField.font = TextRole.mono.font
        storyBaseField.lineBreakMode = .byTruncatingMiddle
        storyBaseField.widthAnchor.constraint(equalToConstant: Self.fieldWidth).isActive = true
        let baseButtons = PageKit.row([
            PageKit.button("Copy", target: self, action: #selector(copyBaseURL)),
        ])
        section("Story Studio", [
            PageKit.formRow("Base URL", [storyBaseField, baseButtons] as [NSView]),
            PageKit.help(
                "Paste this address and the Gateway API Key into Story Studio → Settings → This Mac."
            ),
        ], in: root)

        // ---- updates
        // Owner rule 2026-11-13: only Studio Launcher KH installs updates, for every app. Gateway
        // signs and serves no app release, so there is no key, fingerprint or trust control here.
        let updatesHelp = PageKit.help("Updates are installed by Studio Launcher KH.")
        updatesHelp.preferredMaxLayoutWidth = 0
        section("Updates", [updatesHelp], in: root)

        // ---- received artifacts
        //
        // The gateway only *receives* these: a node renders them, this Mac hands
        // them to Story Studio, and Story Studio keeps the copy that matters. So
        // they are temporary here — and nothing removed them at all before this.
        retentionDaysField = NSTextField(string: "")
        retentionDaysField.widthAnchor.constraint(equalToConstant: Self.numberWidth).isActive = true
        retentionGBField = NSTextField(string: "")
        retentionGBField.widthAnchor.constraint(equalToConstant: Self.numberWidth).isActive = true
        trashDaysField = NSTextField(string: "")
        trashDaysField.widthAnchor.constraint(equalToConstant: Self.numberWidth).isActive = true
        trashGBField = NSTextField(string: "")
        trashGBField.widthAnchor.constraint(equalToConstant: Self.numberWidth).isActive = true
        retentionUsageLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
        section("Received artifacts", [
            PageKit.help(
                "Images, audio and subtitles pass through this Mac on their way to Story Studio, "
                + "which keeps its own copy. They are moved to the Trash Folder when either limit is "
                + "reached — whichever comes first, oldest first. The Trash Folder itself moves the "
                + "oldest files to macOS Trash at its own limits (default 7 days or 20 GB). "
                + "Gateway never empties macOS Trash. Pinned files, work in flight and files newer than an hour are kept."
            ),
            PageKit.formRow("Keep for", [PageKit.row([
                retentionDaysField,
                PageKit.label("days, or", .secondary, colour: .secondaryLabelColor),
                retentionGBField,
                PageKit.label("GB", .secondary, colour: .secondaryLabelColor),
            ])]),
            PageKit.formRow("Trash Folder", [PageKit.row([
                trashDaysField,
                PageKit.label("days, or", .secondary, colour: .secondaryLabelColor),
                trashGBField,
                PageKit.label("GB before macOS Trash", .secondary, colour: .secondaryLabelColor),
            ])]),
            PageKit.formRow("In use", [
                retentionUsageLabel,
                PageKit.button("Prune Now", target: self, action: #selector(pruneNow)),
            ] as [NSView]),
        ], in: root)

        // ---- Telegram: messages to the owner's phone about the fleet
        section("Telegram", telegram.rows, in: root)
        // ---- what the notifier has open and recently cleared (gateway/notifier.py)
        section("Alerts", alerts.rows, in: root)
        // ---- the password a phone browser signs in with at /admin (gateway/admin_session.py)
        section("Browser password", browserPassword.rows, in: root)
        // ---- automatic pairing for apps on this Mac (gateway/pairing.py)
        pairingCheck = NSButton(
            checkboxWithTitle: "Allow local apps to pair automatically",
            target: self, action: #selector(togglePairing)
        )
        pairingCheck.setAccessibilityLabel("Allow local apps to pair automatically")
        pairingStatus = PageKit.label("Reading…", .secondary, colour: .secondaryLabelColor, wrapping: true)
        section("Local apps", [
            PageKit.formRow("Pairing", [pairingCheck]),
            PageKit.help(
                "The pairing token can only mint a worker token for a worker id on this Mac; "
                + "it cannot read providers, change settings, publish, or act as admin."
            ),
            pairingStatus,
        ], in: root)
        // ---- backups (keys, credentials, roster, voices): gateway/backup.py
        section("Backups", backups.rows(), in: root)

        // ---- startup
        loginCheck = NSButton(
            checkboxWithTitle: "Launch at login",
            target: self, action: #selector(toggleLogin)
        )
        autoStartCheck = NSButton(
            checkboxWithTitle: "Start the gateway automatically when this app opens",
            target: self, action: #selector(toggleAutoStart)
        )
        // Off by default. Leaving the gateway running means the next launch adopts
        // it, so a rebuild silently keeps serving the old code — the bug this
        // setting exists to expose rather than to repeat.
        keepRunningCheck = NSButton(
            checkboxWithTitle: "Keep the gateway running after I quit",
            target: self, action: #selector(toggleKeepRunning)
        )
        section("Startup", [
            PageKit.formRow("Login", [loginCheck, autoStartCheck]),
            PageKit.formRow("On quit", [keepRunningCheck]),
            PageKit.help(
                "Off: quitting stops the gateway and frees the port, so reopening always "
                + "runs the build you just installed. On: the gateway keeps serving Story "
                + "Studio while this app is closed — but a rebuild will not take effect "
                + "until you press Restart Gateway."
            ),
        ], in: root)

        let actions = PageKit.row([
            PageKit.primaryButton("Save", target: self, action: #selector(save), regular: true),
            PageKit.button("Open Provider Console", target: self, action: #selector(openConsole), regular: true),
            PageKit.button("Reveal Data Folder", target: self, action: #selector(revealData), regular: true),
        ])
        statusLabel = PageKit.label("", .secondary, colour: .secondaryLabelColor, wrapping: true)
        let footer = NSStackView(views: [actions, statusLabel])
        footer.orientation = .vertical
        footer.alignment = .leading
        footer.spacing = DS.Space.s
        root.addArrangedSubview(footer)

        // A form has a natural height, and it *must not* be able to grow the
        // window — but `lessThanOrEqualTo` only stops the growth. It left the
        // surplus with nowhere to go, so on a shorter window the bottom of the
        // page (the Startup checkboxes, Save, Reveal data and the status line)
        // was compressed and clipped with no scrollbar: the controls the owner
        // needed were the ones that disappeared. Wrapping the body in a scroller
        // satisfies both halves of the rule — the page fits the window, and
        // everything on it stays reachable.
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

    private func refresh() {
        loginCheck?.state = LaunchAgent.isInstalled ? .on : .off
        autoStartCheck?.state = ConfigStore.shared.config.autoStartGateway ? .on : .off
        keepRunningCheck?.state = ConfigStore.shared.config.keepGatewayRunning == true ? .on : .off
        fill(keyField, EnvFile.apiKey())
        storyBaseField?.stringValue = LocalAddresses.storyBaseURL(port: ConfigStore.shared.config.port)
        runtimeLabel?.stringValue = GatewayController.shared.runtimeDescription
        dataLabel?.stringValue = AppPaths.data.path
        statusLabel?.stringValue = "Config: \(ConfigStore.shared.configPath)"
        loadRetention()
        telegram.load()
        alerts.load()
        browserPassword.load()
        loadPairing()
        backups.reload()
    }

    /// The automatic-pairing switch and where its file is. The token itself is never shown here.
    private func loadPairing() {
        GatewayAPI.get("/admin/api/settings/pairing") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.pairingCheck?.state = .off
                self.pairingCheck?.isEnabled = false
                self.pairingStatus?.stringValue =
                    "Pairing settings unavailable — \(error.localizedDescription)"
            case .success(let payload):
                let on = (payload["enabled"] as? Bool) ?? true
                self.pairingCheck?.isEnabled = true
                self.pairingCheck?.state = on ? .on : .off
                if on, let file = payload["endpoint_file"] as? String {
                    self.pairingStatus?.stringValue = "On — a local app reads \(file)."
                } else if on {
                    self.pairingStatus?.stringValue =
                        "On, but no file is written while the gateway does not listen on loopback."
                } else {
                    self.pairingStatus?.stringValue =
                        "Off — the file is removed and the pairing token is revoked."
                }
            }
        }
    }

    @objc private func togglePairing() {
        let wanted = pairingCheck.state == .on
        pairingCheck.isEnabled = false
        GatewayAPI.put("/admin/api/settings/pairing", body: ["enabled": wanted]) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success:
                self.loadPairing()
            case .failure(let error):
                // The box goes back to what the gateway still has; the reason stays on the page.
                self.pairingCheck?.isEnabled = true
                self.pairingCheck?.state = wanted ? .off : .on
                self.pairingStatus?.stringValue =
                    "Could not change pairing — \(error.localizedDescription)"
            }
        }
    }

    private func fill(_ field: NSTextField?, _ value: String) {
        guard let field else { return }
        fill(ObjectIdentifier(field), get: { field.stringValue }, set: { field.stringValue = $0 }, value)
    }

    private func fill(_ field: SecretField?, _ value: String) {
        guard let field else { return }
        fill(ObjectIdentifier(field), get: { field.stringValue }, set: { field.stringValue = $0 }, value)
    }

    private func fill(_ id: ObjectIdentifier, get: () -> String, set: (String) -> Void, _ value: String) {
        if let last = written[id], get() != last { return }
        set(value)
        written[id] = value
    }

    /// The limits live in the gateway, not in this app, so they are read from it.
    ///
    /// **Two reads, deliberately.** The four numbers are one small JSON file; the "in use" report
    /// walks every artifact and trash file and costs seconds. Asking for both in one response made
    /// the form wait on the walk — and a read abandoned at the 15 s a plain read is given blanked
    /// all four fields (`GET /admin/api/settings/retention` measured 3.1 s warm and past 15 s cold
    /// on this Mac's ~10k artifacts, and it grew once walks queued behind each other). The numbers
    /// now arrive immediately; only the report can be late, and it can never blank them.
    private func loadRetention() {
        GatewayAPI.get("/admin/api/settings/retention") { [weak self] result in
            guard let self else { return }
            guard case .success(let payload) = result else {
                // Never invent the numbers. This used to fall back to 7 days and
                // 10 GB on *any* failure — and those invented values were what
                // "Prune now" then wrote back to the gateway, so a read that
                // failed could silently overwrite the owner's real limits with
                // defaults. An empty field is honest; a wrong number is not.
                self.fill(self.retentionDaysField, "")
                self.fill(self.retentionGBField, "")
                self.fill(self.trashDaysField, "")
                self.fill(self.trashGBField, "")
                self.loadedRetention = [:]
                if case .failure(let error) = result {
                    self.retentionUsageLabel?.stringValue =
                        "Could not read the limits — \(error.localizedDescription)"
                }
                return
            }
            self.loadedRetention = [:]
            if let days = payload["max_age_days"] as? Double {
                self.loadedRetention["max_age_days"] = days
                self.fill(self.retentionDaysField, String(format: "%g", days))
            }
            if let bytes = payload["max_bytes"] as? Double {
                self.loadedRetention["max_bytes"] = bytes
                self.fill(self.retentionGBField, String(format: "%g", bytes / (1024 * 1024 * 1024)))
            }
            if let days = payload["trash_days"] as? Double {
                self.loadedRetention["trash_days"] = days
                self.fill(self.trashDaysField, String(format: "%g", days))
            }
            if let bytes = payload["trash_cap_bytes"] as? Double {
                self.loadedRetention["trash_cap_bytes"] = bytes
                self.fill(self.trashGBField, String(format: "%g", bytes / (1024 * 1024 * 1024)))
            }
            self.loadRetentionUsage()
        }
    }

    /// The "in use" line, on its own read so the store walk behind it cannot touch the four
    /// numbers above it. Its own ceiling is generous because the walk is slow by nature.
    private func loadRetentionUsage() {
        GatewayAPI.get("/admin/api/settings/retention/usage", timeout: 60) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.retentionUsageLabel?.stringValue =
                    "Could not read what is stored — \(error.localizedDescription)"
            case .success(let payload):
                self.describeRetention(payload["usage"] as? [String: Any])
            }
        }
    }

    private func describeRetention(_ usage: [String: Any]?) {
        guard let usage else { return }
        let files = usage["files"] as? Int ?? 0
        let bytes = (usage["bytes"] as? Double) ?? 0
        let formatted = ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
        let trashBytes = Int64((usage["trash_bytes"] as? Double) ?? 0)
        let trashDays = (usage["trash_days"] as? Double) ?? 7
        let trashCap = Int64((usage["trash_cap_bytes"] as? Double) ?? 20 * 1024 * 1024 * 1024)
        let trash = ByteCountFormatter.string(fromByteCount: trashBytes, countStyle: .file)
        let cap = ByteCountFormatter.string(fromByteCount: trashCap, countStyle: .file)
        retentionUsageLabel?.stringValue =
            "\(files) file\(files == 1 ? "" : "s"), \(formatted) — Story Studio keeps its own copy\n"
            + "Trash Folder: \(trash), moved to macOS Trash after \(String(format: "%g", trashDays)) days or beyond \(cap)"
        if let warning = usage["warning"] as? String, !warning.isEmpty {
            retentionUsageLabel?.stringValue += "\n" + warning
        }
    }

    /// The typed limits as a request body, or nil after saying what is wrong. A blank field
    /// (the read failed) is left alone rather than sent as a guess.
    private func retentionBody() -> [String: Double]? {
        var body: [String: Double] = [:]
        let fields = [
            (retentionDaysField!, "max_age_days", 1.0, "days"),
            (retentionGBField!, "max_bytes", 1024.0 * 1024 * 1024, "GB"),
            (trashDaysField!, "trash_days", 1.0, "days"),
            (trashGBField!, "trash_cap_bytes", 1024.0 * 1024 * 1024, "GB"),
        ]
        for (field, key, scale, unit) in fields {
            let text = field.stringValue.trimmingCharacters(in: .whitespaces)
            if text.isEmpty { continue }
            guard let value = Double(text), value > 0 else {
                statusLabel.stringValue = "The \(unit) limit must be a number above zero — nothing was saved."
                return nil
            }
            body[key] = value * scale
        }
        return body
    }

    private func retentionChanged(_ body: [String: Double]) -> Bool {
        body.contains { key, new in
            guard let old = loadedRetention[key] else { return true }
            return abs(new - old) > max(new, old) * 1e-5
        }
    }

    /// Save the limits and run the policy, so the effect of a change is visible now
    /// rather than at the next hourly pass. Retires files, so it asks first.
    @objc private func pruneNow() {
        guard let body = retentionBody() else { return }
        guard !body.isEmpty else {
            statusLabel.stringValue = "The limits could not be read from the gateway — nothing was pruned."
            return
        }
        guard PageKit.confirm(
            "Move old received artifacts to the Trash Folder now?",
            "Files older than \(retentionDaysField.stringValue) days, or beyond \(retentionGBField.stringValue) GB "
                + "(oldest first), move to the Trash Folder. After \(trashDaysField.stringValue) days there, or "
                + "beyond \(trashGBField.stringValue) GB, "
                + "the oldest files move to macOS Trash. Gateway never empties macOS Trash. Now: \(retentionUsageLabel.stringValue). "
                + "Pinned files and anything newer than an hour are kept.",
            ok: "Prune"
        ) else { return }

        GatewayAPI.put("/admin/api/settings/retention", body: body as [String: Any]) { [weak self] result in
            guard let self else { return }
            if case .failure(let error) = result {
                self.statusLabel.stringValue = "Could not save the limits — \(error.localizedDescription)"
                return
            }
            GatewayAPI.post("/admin/api/artifacts/prune", body: [:]) { [weak self] result in
                guard let self else { return }
                switch result {
                case .failure(let error):
                    self.statusLabel.stringValue = "Could not prune — \(error.localizedDescription)"
                case .success(let payload):
                    let deleted = payload["deleted"] as? Int ?? 0
                    let moved = (payload["moved_bytes"] as? Double) ?? 0
                    let recycled = payload["recycled_files"] as? Int ?? 0
                    self.statusLabel.stringValue = deleted == 0 && recycled == 0
                        ? "Nothing to move — everything is inside the limits."
                        : "Moved \(deleted) file\(deleted == 1 ? "" : "s") to the Trash Folder ("
                          + ByteCountFormatter.string(fromByteCount: Int64(moved), countStyle: .file)
                          + "), and \(recycled) to macOS Trash."
                    if let warning = payload["warning"] as? String {
                        self.statusLabel.stringValue += " " + warning
                    }
                    self.describeRetention(payload["usage"] as? [String: Any])
                    self.loadedRetention.merge(body) { _, new in new }
                }
            }
        }
    }

    // MARK: Actions

    @objc private func changeAppearance() {
        let index = appearanceControl.selectedSegment
        guard AppearanceMode.allCases.indices.contains(index) else { return }
        let mode = AppearanceMode.allCases[index]
        AppearanceMode.set(mode)
        appearanceDetail.stringValue = mode.detail
    }

    /// Copies the whole hex key, exactly as shown — the one value a Mac takes.

    @objc private func copyKey() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(keyField.stringValue, forType: .string)
        statusLabel.stringValue = "Gateway API Key copied to the clipboard."
    }

    @objc private func copyBaseURL() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(storyBaseField.stringValue, forType: .string)
        statusLabel.stringValue = "Story Studio Base URL copied to the clipboard."
    }

    @objc private func regenerateKey() {
        guard PageKit.confirm(
            "Generate a new Gateway API Key?",
            "Story Studio presents this key. It stops working the moment the new one is written, "
                + "and the old key cannot be recovered. Paste the new key into Story Studio → Settings afterwards.",
            ok: "Generate"
        ) else { return }
        let key = EnvFile.generateKey()
        EnvFile.writeApiKey(key)
        ConsoleSection.refreshCredentials()
        keyField.stringValue = key
        written = [:]
        statusLabel.stringValue =
            "New key written. New requests use it immediately; update Story Studio if it has the old key."
    }

    @objc private func toggleLogin() {
        // The plist is the only record of this: no config mirror to drift.
        if loginCheck.state == .on {
            ConfigStore.shared.update { $0.launchAtLoginOptedOut = false }
            LaunchAgent.install()
        } else {
            ConfigStore.shared.update { $0.launchAtLoginOptedOut = true }
            LaunchAgent.uninstall()
        }
        refresh()
    }

    @objc private func toggleAutoStart() {
        ConfigStore.shared.update { $0.autoStartGateway = autoStartCheck.state == .on }
    }

    @objc private func toggleKeepRunning() {
        ConfigStore.shared.update { $0.keepGatewayRunning = keepRunningCheck.state == .on }
    }

    @objc private func save() {
        let typed = portField.stringValue.trimmingCharacters(in: .whitespaces)
        // **A mistyped port is refused, not replaced with 8787.** `?? 8787` meant an
        // empty or unparseable field silently rebound the gateway to 8787 while the
        // page still said "Saved. Restarting…" — so Story Studio, pointed at the
        // port the owner had actually chosen, went dark with nothing on screen to
        // explain why. Put the real value back and say what was wrong.
        guard let port = Int(typed), (1...65535).contains(port) else {
            portField.stringValue = String(ConfigStore.shared.config.port)
            statusLabel.stringValue = "Port must be a whole number from 1 to 65535 — nothing was saved."
            return
        }
        let key = keyField.stringValue.trimmingCharacters(in: .whitespaces)
        guard let retention = retentionBody() else { return }

        // The gateway reads its key from .env on every request, so only a new port needs a restart.
        let portChanged = port != ConfigStore.shared.config.port
        let keyChanged = !key.isEmpty && key != EnvFile.apiKey()
        let limitsChanged = retentionChanged(retention)
        guard portChanged || keyChanged || limitsChanged else {
            statusLabel.stringValue = "Nothing to save — no setting has changed."
            return
        }
        let apply = { [weak self] in
            guard let self else { return }
            if keyChanged {
                EnvFile.writeApiKey(key)
                ConsoleSection.refreshCredentials()
            }
            if limitsChanged {
                GatewayAPI.put("/admin/api/settings/retention", body: retention as [String: Any]) { [weak self] result in
                    if case .failure(let error) = result {
                        self?.statusLabel.stringValue = "Could not save the limits — \(error.localizedDescription)"
                    } else {
                        self?.loadedRetention.merge(retention) { _, new in new }
                    }
                }
            }
            self.written = [:]
            guard portChanged else {
                self.statusLabel.stringValue = "Saved."
                return
            }
            ConfigStore.shared.update { $0.port = port }
            self.statusLabel.stringValue = "Saved. Restarting the gateway…"
            GatewayController.shared.restart()
        }
        guard portChanged else { return apply() }
        GatewayController.shared.confirmIfBusy("Restart") { [weak self] ok in
            if ok { apply() } else { self?.statusLabel.stringValue = "Not saved." }
        }
    }

    @objc private func openConsole() { MainWindow.shared.show(section: "providers") }

    @objc private func revealData() {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: AppPaths.data.path)
    }
}
