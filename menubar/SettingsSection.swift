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
    private var dataLabel: NSTextField!
    private var portField: NSTextField!
    private var keyField: NSTextField!
    private var storyBaseField: NSTextField!
    private var retentionDaysField: NSTextField!
    private var retentionGBField: NSTextField!
    private var retentionUsageLabel: NSTextField!
    private var loginCheck: NSButton!
    private var autoStartCheck: NSButton!
    private var keepRunningCheck: NSButton!
    private var statusLabel: NSTextField!
    private var publisherFingerprintLabel: NSTextField!
    private var publisherKeyBase64Label: NSTextField!
    private var publisherKeyHexLabel: NSTextField!
    private var publisherIDLabel: NSTextField!
    private var releaseIdentityStatus: NSTextField!

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

    private func buildContent() -> NSView {
        let root = PageKit.page()

        root.addArrangedSubview(PageKit.heading(
            "Settings",
            "The gateway's port and API key, and whether it starts with this Mac."
        ))

        // ---- gateway
        root.addArrangedSubview(PageKit.sectionTitle("Gateway"))

        runtimeLabel = PageKit.label(
            GatewayController.shared.runtimeDescription, size: 12,
            colour: .secondaryLabelColor, wrapping: true
        )
        root.addArrangedSubview(PageKit.formRow("Runtime", runtimeLabel))

        dataLabel = PageKit.label(AppPaths.data.path, size: 11, colour: .secondaryLabelColor)
        dataLabel.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        dataLabel.lineBreakMode = .byTruncatingMiddle
        dataLabel.widthAnchor.constraint(equalToConstant: 420).isActive = true
        root.addArrangedSubview(PageKit.formRow("Data folder", dataLabel))

        portField = NSTextField(string: String(ConfigStore.shared.config.port))
        portField.widthAnchor.constraint(equalToConstant: 100).isActive = true
        root.addArrangedSubview(PageKit.formRow("Port", [
            portField,
            PageKit.help("Changing this restarts the gateway. Story Studio must match."),
        ]))

        root.addArrangedSubview(divider())

        // ---- API key
        root.addArrangedSubview(PageKit.sectionTitle("API key"))
        root.addArrangedSubview(PageKit.help(
            "Story Studio presents this key. Paste it there once, and it never needs "
            + "to appear in a terminal."
        ))

        keyField = NSTextField(string: EnvFile.apiKey())
        keyField.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        keyField.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let keyButtons = PageKit.row([
            NSButton(title: "Copy", target: self, action: #selector(copyKey)),
            NSButton(title: "Regenerate", target: self, action: #selector(regenerateKey)),
        ], spacing: 8)
        root.addArrangedSubview(PageKit.formRow("Key", [keyField, keyButtons] as [NSView]))

        // ---- where Story Studio should point
        //
        // This used to be nowhere: the status line, the console link and the About
        // panel all hardcoded 127.0.0.1, which is right only while Story Studio is
        // on this Mac. Bound beyond loopback the useful address is the Tailscale
        // one, and the owner should not have to read it off a terminal to paste it
        // into the other machine.
        root.addArrangedSubview(PageKit.sectionTitle("Story Studio"))
        storyBaseField = NSTextField(labelWithString:
            LocalAddresses.storyBaseURL(port: ConfigStore.shared.config.port))
        storyBaseField.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        storyBaseField.lineBreakMode = .byTruncatingMiddle
        storyBaseField.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let baseButtons = PageKit.row([
            NSButton(title: "Copy", target: self, action: #selector(copyBaseURL)),
        ], spacing: 8)
        root.addArrangedSubview(PageKit.formRow("Base URL", [storyBaseField, baseButtons] as [NSView]))
        root.addArrangedSubview(PageKit.help(
            EnvFile.bindsBeyondLoopback()
                ? "Paste this and the key into Story Studio → Settings → This Mac on the other machine."
                : "This Mac only. Set GATEWAY_HOST=0.0.0.0 in .env to reach it from another machine."
        ))

        root.addArrangedSubview(divider())

        // ---- Studio Hub app releases
        //
        // The one string an owner has to carry to another Mac is the fingerprint:
        // a controller accepts a release only from a gateway whose publisher key
        // its owner installed with POST /api/hub/release/trust. Both values are
        // public; the private half never leaves the gateway, and this page never
        // asks for it.
        root.addArrangedSubview(PageKit.sectionTitle("Studio Hub app releases"))
        root.addArrangedSubview(PageKit.help(
            "This is this gateway's signing identity. Install its fingerprint on a "
            + "controller so that controller accepts releases pushed from here. The "
            + "fingerprint and the public key are safe to share; the private key is "
            + "never shown."
        ))
        publisherFingerprintLabel = PageKit.label(
            "Reading the publisher identity…", size: 13, wrapping: true
        )
        publisherFingerprintLabel.font = .monospacedSystemFont(ofSize: 13, weight: .semibold)
        publisherFingerprintLabel.lineBreakMode = .byCharWrapping
        publisherFingerprintLabel.maximumNumberOfLines = 2
        publisherFingerprintLabel.preferredMaxLayoutWidth = 420
        publisherFingerprintLabel.widthAnchor.constraint(equalToConstant: 420).isActive = true
        root.addArrangedSubview(PageKit.formRow("Fingerprint", publisherFingerprintLabel))

        publisherKeyBase64Label = identityField()
        root.addArrangedSubview(PageKit.formRow("Public key (base64)", publisherKeyBase64Label))
        publisherKeyHexLabel = identityField()
        root.addArrangedSubview(PageKit.formRow("Public key (hex)", publisherKeyHexLabel))
        publisherIDLabel = PageKit.label("—", size: 11.5, colour: .secondaryLabelColor)
        publisherIDLabel.isSelectable = true
        root.addArrangedSubview(PageKit.formRow("Publisher", publisherIDLabel))
        releaseIdentityStatus = PageKit.label(
            "", size: 11.5, colour: .secondaryLabelColor, wrapping: true
        )
        releaseIdentityStatus.preferredMaxLayoutWidth = 520
        root.addArrangedSubview(PageKit.formRow("", releaseIdentityStatus))

        root.addArrangedSubview(divider())

        // ---- received artifacts
        //
        // The gateway only *receives* these: a node renders them, this Mac hands
        // them to Story Studio, and Story Studio keeps the copy that matters. So
        // they are temporary here — and nothing removed them at all before this.
        root.addArrangedSubview(PageKit.sectionTitle("Received artifacts"))
        root.addArrangedSubview(PageKit.help(
            "Images, audio and subtitles pass through this Mac on their way to Story Studio, "
            + "which keeps its own copy. They are deleted when either limit is reached — "
            + "whichever comes first, oldest first. A file newer than an hour is never removed, "
            + "so work in flight is safe."
        ))
        retentionDaysField = NSTextField(string: "")
        retentionDaysField.widthAnchor.constraint(equalToConstant: 70).isActive = true
        retentionGBField = NSTextField(string: "")
        retentionGBField.widthAnchor.constraint(equalToConstant: 70).isActive = true
        root.addArrangedSubview(PageKit.formRow("Keep for", [
            retentionDaysField,
            PageKit.label("days, or", size: 12, colour: .secondaryLabelColor),
            retentionGBField,
            PageKit.label("GB", size: 12, colour: .secondaryLabelColor),
        ]))
        retentionUsageLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
        root.addArrangedSubview(PageKit.formRow("In use", [
            retentionUsageLabel,
            NSButton(title: "Prune now", target: self, action: #selector(pruneNow)),
        ] as [NSView]))

        root.addArrangedSubview(divider())

        // ---- startup
        root.addArrangedSubview(PageKit.sectionTitle("Startup"))
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
        root.addArrangedSubview(PageKit.formRow("Login", [loginCheck, autoStartCheck]))
        root.addArrangedSubview(PageKit.formRow("On quit", [keepRunningCheck]))
        root.addArrangedSubview(PageKit.help(
            "Off: quitting stops the gateway and frees the port, so reopening always "
            + "runs the build you just installed. On: the gateway keeps serving Story "
            + "Studio while this app is closed — but a rebuild will not take effect "
            + "until you press Restart Gateway."
        ))

        root.addArrangedSubview(divider())

        let actions = PageKit.row([
            NSButton(title: "Save", target: self, action: #selector(save)),
            NSButton(title: "Open provider console", target: self, action: #selector(openConsole)),
            NSButton(title: "Reveal data folder", target: self, action: #selector(revealData)),
        ], spacing: 8)
        statusLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
        root.addArrangedSubview(PageKit.formRow("", [actions, statusLabel] as [NSView]))

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

    /// A small caption above its control, matching the "label then field" rhythm
    /// the Settings window already used.
    private func divider() -> NSBox {
        let box = NSBox()
        box.boxType = .separator
        box.translatesAutoresizingMaskIntoConstraints = false
        box.widthAnchor.constraint(equalToConstant: 500).isActive = true
        // A separator has no intrinsic height, so in a stack view it is the first
        // thing asked to absorb spare vertical space and renders as a huge blank
        // band. Pin it to its real thickness and let it hug.
        box.heightAnchor.constraint(equalToConstant: 1).isActive = true
        box.setContentHuggingPriority(.required, for: .vertical)
        return box
    }

    /// A read-only, selectable line for the public half of the identity.
    ///
    /// Monospaced and middle-truncated: the string is long, but its start and end
    /// are what identify it, and it must stay selectable so it can be copied whole.
    private func identityField() -> NSTextField {
        let field = PageKit.label("—", size: 11)
        field.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        field.isSelectable = true
        field.lineBreakMode = .byTruncatingMiddle
        field.widthAnchor.constraint(equalToConstant: 420).isActive = true
        return field
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
        loadReleaseIdentity()
    }

    private func fill(_ field: NSTextField?, _ value: String) {
        guard let field else { return }
        let id = ObjectIdentifier(field)
        if let last = written[id], field.stringValue != last { return }
        field.stringValue = value
        written[id] = value
    }

    /// Read the publisher identity from the gateway. It is public material and
    /// read-only here; nothing private is ever requested or drawn.
    private func loadReleaseIdentity() {
        GatewayAPI.get("/admin/api/release/identity") { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.showReleaseIdentity(nil, error: error.localizedDescription)
            case .success(let payload):
                let state = FleetReleaseState.from(payload)
                self.showReleaseIdentity(state?.identity, error: state?.error)
            }
        }
    }

    private func showReleaseIdentity(_ identity: FleetReleaseIdentity?, error: String?) {
        guard let identity else {
            publisherFingerprintLabel?.stringValue = "Unavailable"
            publisherKeyBase64Label?.stringValue = "—"
            publisherKeyHexLabel?.stringValue = "—"
            publisherIDLabel?.stringValue = "—"
            if let error, !error.isEmpty {
                releaseIdentityStatus?.stringValue =
                    "Could not read the publisher identity — \(error)"
            } else {
                releaseIdentityStatus?.stringValue =
                    "The gateway has not published a signing identity yet."
            }
            return
        }
        publisherFingerprintLabel?.stringValue = identity.fingerprint ?? "not stated"
        publisherKeyBase64Label?.stringValue = identity.publicKeyBase64 ?? "not stated"
        publisherKeyHexLabel?.stringValue = identity.publicKeyHex ?? "not stated"
        publisherIDLabel?.stringValue = identity.publisherId ?? "—"
        var parts: [String] = []
        if let algorithm = identity.algorithm, !algorithm.isEmpty {
            parts.append("\(algorithm) signing key")
        }
        parts.append(
            "Install the fingerprint on a controller to trust releases from this gateway."
        )
        releaseIdentityStatus?.stringValue = parts.joined(separator: " · ")
    }

    /// The limits live in the gateway, not in this app, so they are read from it.
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
            self.describeRetention(payload["usage"] as? [String: Any])
        }
    }

    private func describeRetention(_ usage: [String: Any]?) {
        guard let usage else { return }
        let files = usage["files"] as? Int ?? 0
        let bytes = (usage["bytes"] as? Double) ?? 0
        let formatted = ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
        retentionUsageLabel?.stringValue =
            "\(files) file\(files == 1 ? "" : "s"), \(formatted) — Story Studio keeps its own copy"
    }

    /// The typed limits as a request body, or nil after saying what is wrong. A blank field
    /// (the read failed) is left alone rather than sent as a guess.
    private func retentionBody() -> [String: Double]? {
        var body: [String: Double] = [:]
        let fields = [
            (retentionDaysField!, "max_age_days", 1.0, "days"),
            (retentionGBField!, "max_bytes", 1024.0 * 1024 * 1024, "GB"),
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
    /// rather than at the next hourly pass. Deletes files, so it asks first.
    @objc private func pruneNow() {
        guard let body = retentionBody() else { return }
        guard !body.isEmpty else {
            statusLabel.stringValue = "The limits could not be read from the gateway — nothing was pruned."
            return
        }
        guard PageKit.confirm(
            "Delete old received artifacts now?",
            "Files older than \(retentionDaysField.stringValue) days, or beyond \(retentionGBField.stringValue) GB "
                + "(oldest first), are deleted from this Mac. Now: \(retentionUsageLabel.stringValue). "
                + "Pinned files and anything newer than an hour are kept. This cannot be undone.",
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
                    let freed = (payload["freed_bytes"] as? Double) ?? 0
                    self.statusLabel.stringValue = deleted == 0
                        ? "Nothing to remove — everything is inside the limits."
                        : "Removed \(deleted) file\(deleted == 1 ? "" : "s"), "
                          + ByteCountFormatter.string(fromByteCount: Int64(freed), countStyle: .file)
                          + " freed."
                    self.describeRetention(payload["usage"] as? [String: Any])
                    self.loadedRetention.merge(body) { _, new in new }
                }
            }
        }
    }

    // MARK: Actions

    @objc private func copyKey() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(keyField.stringValue, forType: .string)
        statusLabel.stringValue = "API key copied to the clipboard."
    }

    @objc private func copyBaseURL() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(storyBaseField.stringValue, forType: .string)
        statusLabel.stringValue = "Story Studio Base URL copied to the clipboard."
    }

    @objc private func regenerateKey() {
        guard PageKit.confirm(
            "Generate a new API key?",
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
            LaunchAgent.install()
        } else {
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
