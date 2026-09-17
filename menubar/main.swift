// gateway-kh menu bar app
//
// A native macOS status-bar application that supervises the gateway process, so
// the owner never opens a terminal to run it.
//
// Design notes:
//
//  * `LSUIElement` is set in Info.plist, so this has no Dock icon and no
//    windows until asked — it is a background app that happens to have UI.
//  * The gateway itself is a Python service. Rather than pretend otherwise,
//    this supervises the venv that already exists in the gateway directory and
//    surfaces its state. Bundling a Python runtime would make the app
//    self-contained but would also mean shipping a second copy of the
//    interpreter; the directory path is therefore a setting.
//  * Launch-at-login uses a LaunchAgent rather than `SMAppService.mainApp`,
//    because `SMAppService` requires a signed, registered bundle and this app
//    is unsigned. A LaunchAgent works regardless of signing.
//  * Status is polled from the gateway's own `/health`, so what the menu shows
//    is what the service actually reports, not what the app hopes is true.

import Cocoa
import WebKit

// MARK: - Configuration

struct AppConfig: Codable {
    var gatewayDir: String
    var port: Int
    var launchAtLogin: Bool
    var autoStartGateway: Bool

    static func makeDefault() -> AppConfig {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        return AppConfig(
            gatewayDir: "\(home)/Developer/gateway-kh",
            port: 8010,
            launchAtLogin: false,
            autoStartGateway: true
        )
    }
}

final class ConfigStore {
    static let shared = ConfigStore()
    private let fileURL: URL
    private(set) var config: AppConfig

    private init() {
        let support = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("gateway-kh", isDirectory: true)
        try? FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        fileURL = support.appendingPathComponent("menubar.json")

        if let data = try? Data(contentsOf: fileURL),
           let decoded = try? JSONDecoder().decode(AppConfig.self, from: data) {
            config = decoded
        } else {
            config = .makeDefault()
            persist()
        }
    }

    func update(_ mutate: (inout AppConfig) -> Void) {
        mutate(&config)
        persist()
    }

    private func persist() {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? encoder.encode(config) {
            try? data.write(to: fileURL)
        }
    }

    var configPath: String { fileURL.path }
}

// MARK: - .env handling

enum EnvFile {
    static func url(for directory: String) -> URL {
        URL(fileURLWithPath: directory, isDirectory: true).appendingPathComponent(".env")
    }

    static func read(_ directory: String) -> [String: String] {
        guard let text = try? String(contentsOf: url(for: directory), encoding: .utf8) else {
            return [:]
        }
        var values: [String: String] = [:]
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else {
                continue
            }
            let key = String(line[line.startIndex..<separator]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: separator)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2, let first = value.first, let last = value.last,
               (first == "\"" || first == "'"), first == last {
                value = String(value.dropFirst().dropLast())
            }
            values[key] = value
        }
        return values
    }

    static func apiKey(_ directory: String) -> String {
        read(directory)["GATEWAY_API_KEY"] ?? ""
    }

    static func writeApiKey(_ directory: String, _ key: String) {
        var values = read(directory)
        values["GATEWAY_API_KEY"] = key
        let port = String(ConfigStore.shared.config.port)
        values["GATEWAY_PORT"] = port
        values["GATEWAY_HOST"] = "127.0.0.1"

        var lines = ["# Written by the gateway-kh menu bar app.", ""]
        for name in ["GATEWAY_API_KEY", "GATEWAY_HOST", "GATEWAY_PORT"] {
            if let value = values[name] { lines.append("\(name)=\(value)") }
        }
        let text = lines.joined(separator: "\n") + "\n"
        let target = url(for: directory)
        try? text.write(to: target, atomically: true, encoding: .utf8)
        // Owner-only: this file holds the key Story Studio presents and, in a
        // .env-sourced setup, provider credentials too.
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: target.path)
    }

    static func generateKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 20)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return "gw-" + bytes.map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - Gateway supervision

enum GatewayState {
    case stopped
    case starting
    case running(providers: Int, models: Int)
    case failed(String)
}

final class GatewayController {
    static let shared = GatewayController()

    private(set) var process: Process?
    private(set) var state: GatewayState = .stopped
    private var logHandle: FileHandle?
    private var healthTimer: Timer?
    private var onStateChange: (() -> Void)?
    private var stopping = false
    private var restartAttempts = 0
    private var isStarting = false
    /// Set by the app once the status item exists, so the state file can prove
    /// the UI actually initialised.
    var statusItemReady = false

    var port: Int { ConfigStore.shared.config.port }
    var gatewayDir: String { ConfigStore.shared.config.gatewayDir }
    var consoleURL: URL? { URL(string: "http://127.0.0.1:\(port)/admin") }
    var storyURL: URL? { URL(string: "http://127.0.0.1:\(port)") }

    var pythonPath: String {
        URL(fileURLWithPath: gatewayDir, isDirectory: true)
            .appendingPathComponent(".venv/bin/python").path
    }

    var logPath: String {
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        return logs.appendingPathComponent("gateway-kh.log").path
    }

    func observe(_ handler: @escaping () -> Void) {
        onStateChange = handler
    }

    private func publish(_ newState: GatewayState) {
        state = newState
        writeState()
        DispatchQueue.main.async { [weak self] in self?.onStateChange?() }
    }

    /// A small status file.
    ///
    /// This exists so the app's own view of the world is inspectable without a
    /// GUI: useful for debugging a background service, and it is how a headless
    /// check can confirm the status item was created and the gateway is up.
    var stateFileURL: URL {
        FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("gateway-kh", isDirectory: true)
            .appendingPathComponent("menubar-state.json")
    }

    func writeState() {
        let stateName: String
        switch state {
        case .running: stateName = "running"
        case .starting: stateName = "starting"
        case .stopped: stateName = "stopped"
        case .failed: stateName = "failed"
        }
        var payload: [String: Any] = [
            "appPid": ProcessInfo.processInfo.processIdentifier,
            "statusItemReady": statusItemReady,
            "gatewayState": stateName,
            "port": port,
            "gatewayDir": gatewayDir,
            "restartAttempts": restartAttempts,
            "updatedAt": ISO8601DateFormatter().string(from: Date()),
        ]
        if let child = process { payload["gatewayPid"] = child.processIdentifier }
        if case .running(let providers, let models) = state {
            payload["providers"] = providers
            payload["models"] = models
        }
        if case .failed(let reason) = state { payload["failure"] = reason }
        if let data = try? JSONSerialization.data(
            withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]
        ) {
            try? data.write(to: stateFileURL)
        }
    }

    var isRunning: Bool {
        if case .running = state { return true }
        return false
    }

    // MARK: Lifecycle

    func start() {
        guard process == nil, !isStarting else { return }

        guard FileManager.default.fileExists(atPath: pythonPath) else {
            publish(.failed("No .venv in \(gatewayDir)"))
            return
        }

        // Adopt a gateway that is already serving our port rather than fighting
        // it for the bind. This is the normal state after quitting the app while
        // leaving the gateway running -- which is deliberate -- and without this
        // check the new app would report a failure it had caused itself.
        isStarting = true
        probeExisting { [weak self] alive in
            guard let self else { return }
            if alive {
                self.isStarting = false
                self.publish(.starting)
                self.startHealthPolling()
            } else {
                self.spawn()
            }
        }
    }

    private func probeExisting(_ completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { _, response, _ in
            let alive = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(alive) }
        }.resume()
    }

    private func spawn() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: pythonPath)
        task.arguments = [
            "-m", "uvicorn", "gateway.api:app",
            "--host", "127.0.0.1", "--port", String(port),
            "--log-level", "warning",
        ]
        task.currentDirectoryURL = URL(fileURLWithPath: gatewayDir, isDirectory: true)
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        task.environment = environment

        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        logHandle = try? FileHandle(forWritingTo: URL(fileURLWithPath: logPath))
        logHandle?.seekToEndOfFile()
        if let handle = logHandle {
            task.standardOutput = handle
            task.standardError = handle
        }

        task.terminationHandler = { [weak self] finished in
            guard let self else { return }
            self.process = nil
            self.logHandle?.closeFile()
            self.logHandle = nil

            // A stop we asked for is not a failure.
            if self.stopping { return }

            // Unexpected exit. This is a background service: bring it back with
            // exponential backoff rather than leaving Story Studio with no
            // endpoint and no explanation.
            self.restartAttempts += 1
            let delay = min(pow(2.0, Double(self.restartAttempts)), 30.0)
            self.publish(.failed(
                "Gateway exited (code \(finished.terminationStatus)); restarting in \(Int(delay))s"
            ))
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                guard let self, !self.stopping else { return }
                self.start()
            }
        }

        do {
            try task.run()
            process = task
            stopping = false
            isStarting = false
            publish(.starting)
            startHealthPolling()
        } catch {
            isStarting = false
            publish(.failed(error.localizedDescription))
        }
    }

    func stop() {
        stopping = true
        restartAttempts = 0
        healthTimer?.invalidate()
        healthTimer = nil
        guard let task = process else {
            publish(.stopped)
            return
        }
        publish(.stopped)
        task.terminate()
        process = nil
        logHandle?.closeFile()
        logHandle = nil
    }

    func restart() {
        stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in self?.start() }
    }

    // MARK: Health

    func startHealthPolling() {
        healthTimer?.invalidate()
        let timer = Timer(timeInterval: 3.0, repeats: true) { [weak self] _ in self?.probe() }
        RunLoop.main.add(timer, forMode: .common)
        healthTimer = timer
        probe()
    }

    func probe() {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.5

        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let self else { return }
            guard let data, error == nil,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                // Only downgrade a running service; never overwrite a failure
                // message with a vaguer one.
                if self.process == nil, case .failed = self.state { return }
                if self.process != nil { self.publish(.starting); return }

                // We own no process and nothing is answering. That happens when
                // the app *adopted* an externally-started gateway and that one
                // later died: there is no child to restart, so sitting idle
                // leaves the gateway down for good with the menu reporting
                // "stopped" as if that were normal. Take ownership instead.
                if ConfigStore.shared.config.autoStartGateway,
                   !self.stopping,
                   self.restartAttempts < 8 {
                    self.restartAttempts += 1
                    self.publish(.failed("Gateway is not responding — starting it"))
                    self.start()
                } else {
                    self.publish(.stopped)
                }
                return
            }
            let providers = (payload["providers"] as? [String: Any])?["enabled"] as? Int ?? 0
            let models = (payload["models"] as? [String: Any])?["enabled_models"] as? Int ?? 0
            // Healthy again: clear the backoff so a later crash restarts fast.
            self.restartAttempts = 0
            self.publish(.running(providers: providers, models: models))
        }.resume()
    }
}

// MARK: - Launch at login

enum LaunchAgent {
    static let label = "kh.gateway.menubar"

    static var plistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            .appendingPathComponent("\(label).plist")
    }

    static var isInstalled: Bool {
        FileManager.default.fileExists(atPath: plistURL.path)
    }

    static func install() {
        guard let executable = Bundle.main.executableURL?.path else { return }
        let directory = plistURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": [executable],
            "RunAtLoad": true,
            // Only auto-launch if it died; a user quit stays quit.
            "KeepAlive": ["SuccessfulExit": false],
            "ProcessType": "Interactive",
        ]
        if let data = try? PropertyListSerialization.data(
            fromPropertyList: plist, format: .xml, options: 0
        ) {
            try? data.write(to: plistURL)
        }
        reload()
    }

    static func uninstall() {
        let domain = "gui/\(getuid())"
        _ = runLaunchctl(["bootout", "\(domain)/\(label)"])
        try? FileManager.default.removeItem(at: plistURL)
    }

    private static func reload() {
        let domain = "gui/\(getuid())"
        _ = runLaunchctl(["bootout", "\(domain)/\(label)"])
        _ = runLaunchctl(["bootstrap", domain, plistURL.path])
    }

    @discardableResult
    private static func runLaunchctl(_ arguments: [String]) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = arguments
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
            task.waitUntilExit()
            return task.terminationStatus
        } catch {
            return -1
        }
    }
}

// MARK: - Settings window

final class SettingsWindow: NSObject {
    static let shared = SettingsWindow()
    private var window: NSWindow?
    private var dirField: NSTextField!
    private var portField: NSTextField!
    private var keyField: NSTextField!
    private var loginCheck: NSButton!
    private var autoStartCheck: NSButton!
    private var statusLabel: NSTextField!

    func show() {
        if let existing = window {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            refresh()
            return
        }

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 380),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "gateway-kh Settings"
        window.center()
        window.isReleasedWhenClosed = false

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false

        func label(_ text: String, bold: Bool = false) -> NSTextField {
            let field = NSTextField(labelWithString: text)
            field.font = bold ? .boldSystemFont(ofSize: 13) : .systemFont(ofSize: 12)
            field.textColor = bold ? .labelColor : .secondaryLabelColor
            return field
        }

        stack.addArrangedSubview(label("Gateway", bold: true))

        stack.addArrangedSubview(label("Project directory"))
        let dirRow = NSStackView()
        dirRow.orientation = .horizontal
        dirRow.spacing = 8
        dirField = NSTextField(string: ConfigStore.shared.config.gatewayDir)
        dirField.widthAnchor.constraint(equalToConstant: 380).isActive = true
        let chooseButton = NSButton(title: "Choose…", target: self, action: #selector(chooseDirectory))
        dirRow.addArrangedSubview(dirField)
        dirRow.addArrangedSubview(chooseButton)
        stack.addArrangedSubview(dirRow)

        stack.addArrangedSubview(label("Port"))
        portField = NSTextField(string: String(ConfigStore.shared.config.port))
        portField.widthAnchor.constraint(equalToConstant: 100).isActive = true
        stack.addArrangedSubview(portField)

        stack.addArrangedSubview(label("API key (paste this into Story Studio)"))
        let keyRow = NSStackView()
        keyRow.orientation = .horizontal
        keyRow.spacing = 8
        keyField = NSTextField(string: EnvFile.apiKey(ConfigStore.shared.config.gatewayDir))
        keyField.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        keyField.widthAnchor.constraint(equalToConstant: 320).isActive = true
        let copyButton = NSButton(title: "Copy", target: self, action: #selector(copyKey))
        let regenButton = NSButton(title: "Regenerate", target: self, action: #selector(regenerateKey))
        keyRow.addArrangedSubview(keyField)
        keyRow.addArrangedSubview(copyButton)
        keyRow.addArrangedSubview(regenButton)
        stack.addArrangedSubview(keyRow)

        let divider = NSBox()
        divider.boxType = .separator
        divider.widthAnchor.constraint(equalToConstant: 500).isActive = true
        stack.addArrangedSubview(divider)

        loginCheck = NSButton(
            checkboxWithTitle: "Launch at login",
            target: self, action: #selector(toggleLogin)
        )
        stack.addArrangedSubview(loginCheck)

        autoStartCheck = NSButton(
            checkboxWithTitle: "Start the gateway automatically when this app opens",
            target: self, action: #selector(toggleAutoStart)
        )
        stack.addArrangedSubview(autoStartCheck)

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 8
        buttonRow.addArrangedSubview(
            NSButton(title: "Save", target: self, action: #selector(save))
        )
        buttonRow.addArrangedSubview(
            NSButton(title: "Open provider console", target: self, action: #selector(openConsole))
        )
        buttonRow.addArrangedSubview(
            NSButton(title: "Reveal data folder", target: self, action: #selector(revealData))
        )
        stack.addArrangedSubview(buttonRow)

        statusLabel = label("")
        stack.addArrangedSubview(statusLabel)

        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
        ])
        window.contentView = content
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh()
    }

    private func refresh() {
        loginCheck?.state = LaunchAgent.isInstalled ? .on : .off
        autoStartCheck?.state = ConfigStore.shared.config.autoStartGateway ? .on : .off
        keyField?.stringValue = EnvFile.apiKey(ConfigStore.shared.config.gatewayDir)
        statusLabel?.stringValue = "Config: \(ConfigStore.shared.configPath)"
    }

    @objc private func chooseDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.directoryURL = URL(fileURLWithPath: ConfigStore.shared.config.gatewayDir)
        if panel.runModal() == .OK, let url = panel.url {
            dirField.stringValue = url.path
        }
    }

    @objc private func copyKey() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(keyField.stringValue, forType: .string)
        statusLabel.stringValue = "API key copied to the clipboard."
    }

    @objc private func regenerateKey() {
        let key = EnvFile.generateKey()
        EnvFile.writeApiKey(ConfigStore.shared.config.gatewayDir, key)
        keyField.stringValue = key
        statusLabel.stringValue = "New key written. Restart the gateway to apply it."
    }

    @objc private func toggleLogin() {
        if loginCheck.state == .on {
            LaunchAgent.install()
            ConfigStore.shared.update { $0.launchAtLogin = true }
        } else {
            LaunchAgent.uninstall()
            ConfigStore.shared.update { $0.launchAtLogin = false }
        }
        refresh()
    }

    @objc private func toggleAutoStart() {
        ConfigStore.shared.update { $0.autoStartGateway = autoStartCheck.state == .on }
    }

    @objc private func save() {
        let directory = dirField.stringValue.trimmingCharacters(in: .whitespaces)
        let port = Int(portField.stringValue.trimmingCharacters(in: .whitespaces)) ?? 8010
        let key = keyField.stringValue.trimmingCharacters(in: .whitespaces)

        ConfigStore.shared.update {
            $0.gatewayDir = directory
            $0.port = port
        }
        if !key.isEmpty {
            EnvFile.writeApiKey(directory, key)
        }
        statusLabel.stringValue = "Saved. Restarting the gateway…"
        GatewayController.shared.restart()
    }

    @objc private func openConsole() { ConsoleWindow.shared.show() }

    @objc private func revealData() {
        let path = URL(fileURLWithPath: ConfigStore.shared.config.gatewayDir)
            .appendingPathComponent("data").path
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }
}

// MARK: - Provider console window

/// The provider console is a local web UI served by the gateway, so it is shown
/// in a real app window via `WKWebView` rather than being handed to a browser.
///
/// The console stays a plain web page underneath -- that is why it works from
/// any browser on the LAN and why it needs no separate build -- but the owner
/// should not have to leave the app to use it. An explicit "open in browser"
/// item is kept for debugging and for reaching it from another machine.
final class ConsoleWindow: NSObject, WKNavigationDelegate {
    static let shared = ConsoleWindow()
    private var window: NSWindow?
    private var webView: WKWebView?

    func show() {
        guard let url = GatewayController.shared.consoleURL else { return }

        if let existing = window, let web = webView {
            existing.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            web.load(URLRequest(url: url))
            return
        }

        let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "gateway-kh — Provider Console"
        window.contentView = web
        window.isReleasedWhenClosed = false
        // Remembers size and position between launches.
        window.setFrameAutosaveName("GatewayConsoleWindow")
        window.center()

        self.window = window
        self.webView = web

        load(url, into: web)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// If the gateway is not listening yet, say so plainly instead of showing a
    /// raw connection error, and load for real once it should be up.
    private func load(_ url: URL, into web: WKWebView, attempt: Int = 0) {
        if GatewayController.shared.isRunning || attempt > 0 {
            web.load(URLRequest(url: url))
            return
        }
        web.loadHTMLString(Self.startingPage, baseURL: nil)
        GatewayController.shared.start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self, weak web] in
            guard let self, let web else { return }
            self.load(url, into: web, attempt: attempt + 1)
        }
    }

    private static let startingPage = """
    <!doctype html><meta charset="utf-8">
    <style>
      body { background:#0e1117; color:#e6eaf2;
             font:14px -apple-system, BlinkMacSystemFont, sans-serif;
             display:flex; align-items:center; justify-content:center;
             height:100vh; margin:0 }
      div { text-align:center } h2 { font-weight:600 } p { color:#8b95a8 }
    </style>
    <div>
      <h2>Starting the gateway…</h2>
      <p>This window will load the provider console automatically.</p>
    </div>
    """

    func reload() {
        guard let web = webView, let url = GatewayController.shared.consoleURL else { return }
        web.load(URLRequest(url: url))
    }
}

// MARK: - Main menu

/// A menu-bar-only app still needs a main menu.
///
/// Standard editing shortcuts are **not** handled by the text system directly:
/// AppKit dispatches `⌘A`, `⌘C`, `⌘V`, `⌘X` and `⌘Z` through the application's
/// main menu, matching them against menu items whose action is the matching
/// selector (`selectAll:`, `copy:`, …). An `LSUIElement` app with no main menu
/// therefore has a console where typing works but select-all, copy and paste do
/// nothing at all — which is exactly what happened here.
///
/// The menu bar itself is never shown for an accessory app, but a menu
/// installed as `NSApp.mainMenu` still supplies the key equivalents, which is
/// all that is wanted.
enum MainMenuBuilder {
    static func make(target: AnyObject) -> NSMenu {
        let main = NSMenu()

        // ---- Application -------------------------------------------------
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu(title: "gateway-kh")
        appItem.submenu = appMenu
        appMenu.addItem(
            withTitle: "About gateway-kh",
            action: #selector(AppDelegate.showAbout),
            keyEquivalent: ""
        ).target = target
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Settings…",
            action: #selector(AppDelegate.openSettings),
            keyEquivalent: ","
        ).target = target
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Hide gateway-kh",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit gateway-kh",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )

        // ---- Edit --------------------------------------------------------
        // The whole point of this menu. Targets are left nil so each action
        // travels the responder chain to whatever is focused — the web view's
        // text fields, the search box, or a Cocoa text field in Settings.
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editItem.submenu = editMenu
        editMenu.addItem(
            withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z"
        )
        let redo = editMenu.addItem(
            withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z"
        )
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(
            withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"
        )
        editMenu.addItem(
            withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"
        )
        editMenu.addItem(
            withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"
        )
        editMenu.addItem(
            withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: ""
        )
        editMenu.addItem(
            withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"
        )

        // ---- View --------------------------------------------------------
        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        viewItem.submenu = viewMenu
        viewMenu.addItem(
            withTitle: "Overview",
            action: #selector(AppDelegate.openOverview),
            keyEquivalent: "1"
        ).target = target
        viewMenu.addItem(
            withTitle: "Jobs",
            action: #selector(AppDelegate.openJobs),
            keyEquivalent: "2"
        ).target = target
        viewMenu.addItem(.separator())
        viewMenu.addItem(
            withTitle: "Open Provider Console",
            action: #selector(AppDelegate.openConsole),
            keyEquivalent: "p"
        ).target = target
        viewMenu.addItem(
            withTitle: "Reload Console",
            action: #selector(AppDelegate.reloadConsole),
            keyEquivalent: "r"
        ).target = target
        viewMenu.addItem(
            withTitle: "Open Console in Browser",
            action: #selector(AppDelegate.openConsoleInBrowser),
            keyEquivalent: "b"
        ).target = target

        // ---- Window ------------------------------------------------------
        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let windowMenu = NSMenu(title: "Window")
        windowItem.submenu = windowMenu
        windowMenu.addItem(
            withTitle: "Minimize",
            action: #selector(NSWindow.performMiniaturize(_:)),
            keyEquivalent: "m"
        )
        windowMenu.addItem(
            withTitle: "Close",
            action: #selector(NSWindow.performClose(_:)),
            keyEquivalent: "w"
        )
        NSApp.windowsMenu = windowMenu

        return main
    }

    /// Flattened "Menu › Item — ⌘X" listing, for `--dump-menu`.
    static func describe(_ menu: NSMenu, depth: Int = 0) -> [String] {
        var lines: [String] = []
        for item in menu.items {
            if item.isSeparatorItem {
                lines.append(String(repeating: "  ", count: depth) + "---")
                continue
            }
            var suffix = ""
            if !item.keyEquivalent.isEmpty {
                var modifiers = ""
                let mask = item.keyEquivalentModifierMask
                if mask.contains(.control) { modifiers += "⌃" }
                if mask.contains(.option) { modifiers += "⌥" }
                if mask.contains(.shift) { modifiers += "⇧" }
                if mask.contains(.command) { modifiers += "⌘" }
                let key = item.keyEquivalent == "\u{8}" ? "⌫" : item.keyEquivalent.uppercased()
                suffix = "  \(modifiers)\(key)"
            }
            let action = item.action.map { NSStringFromSelector($0) } ?? "(none)"
            // For a top-level item the displayed title comes from its submenu,
            // so report that rather than the empty placeholder title.
            let title = item.submenu?.title ?? item.title
            lines.append(
                String(repeating: "  ", count: depth) + title + suffix + "   [\(action)]"
            )
            if let submenu = item.submenu {
                lines.append(contentsOf: describe(submenu, depth: depth + 1))
            }
        }
        return lines
    }
}

// MARK: - App delegate

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var menu: NSMenu!
    private var studioHubItem: NSMenuItem!
    private var studioHubStartItem: NSMenuItem!
    private var studioHubRunning = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "bolt.horizontal.circle",
                                accessibilityDescription: "gateway-kh")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "gateway-kh"
        }

        // Record that the UI is genuinely up. The state file is how a headless
        // check can distinguish an initialised menu bar from a process that
        // merely started.
        GatewayController.shared.statusItemReady = statusItem.button?.image != nil
        GatewayController.shared.writeState()

        // Editing shortcuts (⌘A/⌘C/⌘V/⌘X/⌘Z) are dispatched through the main
        // menu, so it has to exist even though an accessory app never shows a
        // menu bar. See MainMenuBuilder.
        NSApp.mainMenu = MainMenuBuilder.make(target: self)

        if let requested = launchWindow?.lowercased() {
            switch requested {
            case "overview": OverviewWindow.shared.show()
            case "jobs": JobsWindow.shared.show()
            case "console": ConsoleWindow.shared.show()
            case "settings": SettingsWindow.shared.show()
            default: break
            }
        }

        menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        GatewayController.shared.observe { [weak self] in self?.refreshTitle() }

        startStudioHubProbe()
        if ConfigStore.shared.config.autoStartGateway {
            GatewayController.shared.start()
        }
        refreshTitle()
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Leave the gateway running: it is a service, and quitting a menu bar
        // helper should not silently take the API away from Story Studio.
    }

    // MARK: Menu

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let header = NSMenuItem(title: statusText(), action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        let detail = NSMenuItem(title: detailText(), action: nil, keyEquivalent: "")
        detail.isEnabled = false
        menu.addItem(detail)

        menu.addItem(.separator())

        // Offered whether or not the gateway is up: opening a window starts it
        // if needed, which is what an owner expects from a menu bar app.
        menu.addItem(item("Overview", #selector(openOverview), key: "o"))
        menu.addItem(item("Jobs", #selector(openJobs), key: "j"))
        menu.addItem(.separator())
        menu.addItem(item("Open Provider Console", #selector(openConsole), key: "p"))
        menu.addItem(item("Reload Console", #selector(reloadConsole)))
        menu.addItem(item("Open Console in Browser", #selector(openConsoleInBrowser)))

        menu.addItem(.separator())

        if GatewayController.shared.isRunning {
            menu.addItem(item("Restart Gateway", #selector(restartGateway)))
            menu.addItem(item("Stop Gateway", #selector(stopGateway)))
        } else {
            menu.addItem(item("Start Gateway", #selector(startGateway)))
        }

        menu.addItem(.separator())

        studioHubItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        studioHubItem.isEnabled = false
        menu.addItem(studioHubItem)
        studioHubStartItem = item("Start Studio Hub", #selector(startStudioHub))
        menu.addItem(studioHubStartItem)
        updateStudioHubItems()

        menu.addItem(.separator())

        menu.addItem(item("Settings…", #selector(openSettings), key: ","))
        menu.addItem(item("Open data folder", #selector(openData)))
        menu.addItem(item("Open log", #selector(openLog)))
        menu.addItem(.separator())
        menu.addItem(item("Quit gateway-kh", #selector(quit), key: "q"))
    }

    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.target = self
        return entry
    }

    private func statusText() -> String {
        switch GatewayController.shared.state {
        case .running(let providers, let models):
            return "Gateway running · \(providers) providers · \(models) models"
        case .starting:
            return "Gateway starting…"
        case .stopped:
            return "Gateway stopped"
        case .failed(let reason):
            return "Gateway problem: \(reason)"
        }
    }

    private func detailText() -> String {
        "http://127.0.0.1:\(GatewayController.shared.port)"
    }

    private func refreshTitle() {
        guard let button = statusItem.button else { return }
        let symbol: String
        switch GatewayController.shared.state {
        case .running: symbol = "bolt.horizontal.circle.fill"
        case .starting: symbol = "bolt.horizontal.circle"
        case .stopped: symbol = "bolt.horizontal.circle"
        case .failed: symbol = "exclamationmark.triangle"
        }
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "gateway-kh")
        image?.isTemplate = true
        button.image = image
    }

    // MARK: Actions

    @objc private func startGateway() { GatewayController.shared.start() }
    @objc private func stopGateway() { GatewayController.shared.stop() }
    @objc private func restartGateway() { GatewayController.shared.restart() }

    // Not `private`: MainMenuBuilder references these by selector from another
    // type in this file.
    @objc func openOverview() { OverviewWindow.shared.show() }

    @objc func openJobs() { JobsWindow.shared.show() }

    @objc func openConsole() { ConsoleWindow.shared.show() }

    @objc func reloadConsole() { ConsoleWindow.shared.reload() }

    @objc func openConsoleInBrowser() {
        if let url = GatewayController.shared.consoleURL { NSWorkspace.shared.open(url) }
    }

    @objc func openSettings() { SettingsWindow.shared.show() }

    @objc func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "gateway-kh",
            .applicationVersion: "0.1.0",
            .credits: NSAttributedString(
                string: "A personal model gateway.\n\nConsole: http://127.0.0.1:\(GatewayController.shared.port)/admin",
                attributes: [
                    .font: NSFont.systemFont(ofSize: 11),
                    .foregroundColor: NSColor.secondaryLabelColor,
                ]
            ),
        ])
    }

    @objc private func openData() {
        let path = URL(fileURLWithPath: GatewayController.shared.gatewayDir)
            .appendingPathComponent("data").path
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
    }

    @objc private func openLog() {
        NSWorkspace.shared.selectFile(
            GatewayController.shared.logPath, inFileViewerRootedAtPath: ""
        )
    }

    @objc private func quit() { NSApp.terminate(nil) }

    // MARK: Studio Hub

    private func startStudioHubProbe() {
        probeStudioHub()
        let timer = Timer(timeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.probeStudioHub()
        }
        RunLoop.main.add(timer, forMode: .common)
    }

    private func probeStudioHub() {
        let ready = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/StudioHub Standalone/hub-ready.json")
        guard let data = try? Data(contentsOf: ready),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let urlString = payload["url"] as? String
        else {
            studioHubRunning = false
            updateStudioHubItems()
            return
        }
        guard let url = URL(string: urlString + "/api/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.5
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                self?.studioHubRunning = ok
                self?.updateStudioHubItems()
            }
        }.resume()
    }

    private func updateStudioHubItems() {
        studioHubItem?.title = studioHubRunning
            ? "Studio Hub running (local models available)"
            : "Studio Hub not running"
        studioHubStartItem?.isHidden = studioHubRunning
    }

    @objc private func startStudioHub() {
        let candidates = [
            "\(FileManager.default.homeDirectoryForCurrentUser.path)/Applications/Studio Hub.app",
            "/Applications/Studio Hub.app",
        ]
        for path in candidates where FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.openApplication(
                at: URL(fileURLWithPath: path), configuration: NSWorkspace.OpenConfiguration()
            ) { _, _ in }
            return
        }
    }
}

// MARK: - Entry point

// Headless helpers.
//
// These exist so the LaunchAgent path can be set up and verified from a script
// (and by tests) without clicking through the UI -- the same reason the state
// file exists. They run before AppKit is initialised and exit immediately.
let arguments = CommandLine.arguments

/// `--open overview|jobs|console|settings` opens that window at launch.
///
/// Used to verify a window builds and lays out without a human clicking a menu.
/// A crash inside a window controller would otherwise only surface when someone
/// happened to open it.
let launchWindow: String? = arguments.firstIndex(of: "--open").flatMap { index in
    index + 1 < arguments.count ? arguments[index + 1] : nil
}

if arguments.contains("--install-login-item") {
    LaunchAgent.install()
    print("login item installed: \(LaunchAgent.plistURL.path)")
    exit(0)
}
if arguments.contains("--uninstall-login-item") {
    LaunchAgent.uninstall()
    print("login item removed")
    exit(0)
}
if arguments.contains("--status") {
    let path = GatewayController.shared.stateFileURL
    if let data = try? Data(contentsOf: path), let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        print("no state file at \(path.path) — the app has not run yet")
    }
    exit(0)
}

if arguments.contains("--dump-menu") {
    // Proves the Edit menu — and therefore ⌘A / ⌘C / ⌘V / ⌘X / ⌘Z — is really
    // installed, without needing a GUI to press keys in. A shortcut that is
    // absent from this listing cannot work, because AppKit resolves editing
    // shortcuts by matching menu key equivalents.
    _ = NSApplication.shared  // NSApp must exist before make() touches windowsMenu
    // Hold the target: menu items reference it weakly, so a temporary would be
    // deallocated before the description is built.
    let dumpTarget = NSObject()
    let menu = MainMenuBuilder.make(target: dumpTarget)
    print(MainMenuBuilder.describe(menu).joined(separator: "\n"))
    exit(0)
}

// Single-instance guard.
//
// `open` on an already-running app just activates it, but a LaunchAgent
// bootstrap starts a *new* process unconditionally -- so installing
// launch-at-login while the app is already running leaves two copies, both
// trying to own the menu bar item and both trying to bind the same port. The
// second one then reports a failure it caused itself, overwriting the first
// one's healthy state. Exiting quietly is the correct behaviour: an existing
// instance is already doing the job.
//
// Exit code 0 is deliberate: the LaunchAgent's KeepAlive is keyed on
// unsuccessful exit, so a duplicate must NOT look like a crash to launchd.
let ownPID = ProcessInfo.processInfo.processIdentifier
let bundleIdentifier = Bundle.main.bundleIdentifier ?? "kh.gateway.menubar"
let duplicates = NSRunningApplication
    .runningApplications(withBundleIdentifier: bundleIdentifier)
    .filter { $0.processIdentifier != ownPID }

if !duplicates.isEmpty {
    FileHandle.standardError.write(
        Data("gateway-kh is already running; exiting this copy.\n".utf8)
    )
    exit(0)
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
