import AppKit

/// Settings, as a page in the main window.
///
/// This was its own window, on the reasoning that macOS puts Settings in the
/// app menu. That reasoning fails for *this* app: it is an accessory
/// (`LSUIElement`) process, so its app menu is never on screen, and the only
/// routes in were `⌘,` and the bolt menu — the owner went looking and could not
/// find Settings at all. Studio Hub KH, the sibling app this one is modelled
/// on, lists Settings in the sidebar under "Library & app", and that is where
/// people actually look.
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
    private var loginCheck: NSButton!
    private var autoStartCheck: NSButton!
    private var statusLabel: NSTextField!

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
        root.addArrangedSubview(PageKit.formRow("Login", [loginCheck, autoStartCheck]))

        root.addArrangedSubview(divider())

        let actions = PageKit.row([
            NSButton(title: "Save", target: self, action: #selector(save)),
            NSButton(title: "Open provider console", target: self, action: #selector(openConsole)),
            NSButton(title: "Reveal data folder", target: self, action: #selector(revealData)),
        ], spacing: 8)
        statusLabel = PageKit.label("", size: 11.5, colour: .secondaryLabelColor, wrapping: true)
        root.addArrangedSubview(PageKit.formRow("", [actions, statusLabel] as [NSView]))

        // A form has a natural height, so its bottom is `lessThanOrEqualTo` the
        // host rather than pinned to it. Pinning it made the stack view hand the
        // leftover height to the heading, which opened a ~360pt void between the
        // title and "Gateway". The table pages do the opposite on purpose: their
        // tables are meant to absorb that space.
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

    private func refresh() {
        loginCheck?.state = LaunchAgent.isInstalled ? .on : .off
        autoStartCheck?.state = ConfigStore.shared.config.autoStartGateway ? .on : .off
        keyField?.stringValue = EnvFile.apiKey()
        runtimeLabel?.stringValue = GatewayController.shared.runtimeDescription
        dataLabel?.stringValue = AppPaths.data.path
        statusLabel?.stringValue = "Config: \(ConfigStore.shared.configPath)"
    }

    // MARK: Actions

    @objc private func copyKey() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(keyField.stringValue, forType: .string)
        statusLabel.stringValue = "API key copied to the clipboard."
    }

    @objc private func regenerateKey() {
        let key = EnvFile.generateKey()
        EnvFile.writeApiKey(key)
        keyField.stringValue = key
        statusLabel.stringValue = "New key written. Restart the gateway to apply it."
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

    @objc private func save() {
        let port = Int(portField.stringValue.trimmingCharacters(in: .whitespaces)) ?? 8000
        let key = keyField.stringValue.trimmingCharacters(in: .whitespaces)

        ConfigStore.shared.update { $0.port = port }
        if !key.isEmpty {
            EnvFile.writeApiKey(key)
        }
        statusLabel.stringValue = "Saved. Restarting the gateway…"
        GatewayController.shared.restart()
    }

    @objc private func openConsole() { MainWindow.shared.show(section: "providers") }

    @objc private func revealData() {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: AppPaths.data.path)
    }
}
