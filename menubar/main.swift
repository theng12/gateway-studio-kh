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

// MARK: - App paths

/// Every writable location the app uses, in one place.
///
/// These all live under `~/Library/Application Support/gateway-kh/` rather than
/// next to the code. That is what a macOS app is expected to do, and it is what
/// makes the bundle movable: the `.app` can be copied anywhere while the data —
/// credentials, database, voices — stays put.
enum AppPaths {
    private static func explicitAppSupport() -> URL? {
        guard let value = ProcessInfo.processInfo.environment["GATEWAY_APP_SUPPORT"]?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else { return nil }
        return URL(fileURLWithPath: value, isDirectory: true)
    }

    static let appSupport: URL = {
        let base = explicitAppSupport() ?? FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("gateway-kh", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base
    }()

    /// The API key Story Studio presents, and the gateway's own settings.
    static var env: URL { appSupport.appendingPathComponent(".env") }

    /// Runtime state: credentials, model cache, database, artifacts.
    static var data: URL { appSupport.appendingPathComponent("data", isDirectory: true) }

    static var stateFile: URL { appSupport.appendingPathComponent("menubar-state.json") }
    static var configFile: URL { appSupport.appendingPathComponent("menubar.json") }
    static var logFile: URL {
        if let override = explicitAppSupport() {
            try? FileManager.default.createDirectory(
                at: override,
                withIntermediateDirectories: true
            )
            return override.appendingPathComponent("gateway-kh.log")
        }
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        return logs.appendingPathComponent("gateway-kh.log")
    }

    /// What the bundle ships: a relocatable Python and a copy of the gateway.
    static var bundledRoot: URL? { Bundle.main.resourceURL }
    static var bundledPython: URL? {
        bundledRoot?.appendingPathComponent("python/bin/python3")
    }
    static var bundledSource: URL? {
        bundledRoot?.appendingPathComponent("gateway", isDirectory: true)
    }
}

// MARK: - Configuration

struct AppConfig: Codable {
    var gatewayDir: String
    var port: Int
    var autoStartGateway: Bool
    // NOTE: there is deliberately no `launchAtLogin` here. It existed, was
    // written by the Settings checkbox, and was **never read** — the checkbox
    // reads `LaunchAgent.isInstalled`, i.e. the plist itself, which is the real
    // source of truth. So the field could only ever drift, and did: the config
    // said `false` while the agent was installed and loaded, which reads as
    // "launch at login is off" to anyone who checks the JSON instead of the
    // plist. One fact, one place.
    /// Optional so an existing config file written before this field existed
    /// still decodes; `nil` is treated as "has not launched before".
    var hasLaunchedBefore: Bool?

    /// Whether quitting the app leaves the gateway running.
    ///
    /// **Defaults to `false`, and that default was changed deliberately.** The
    /// gateway outliving the app looks convenient — Story Studio keeps working
    /// when the menu bar app is closed — but it makes a rebuild impossible to
    /// verify by hand: on the next launch the app *adopts* the surviving process,
    /// so "quit and reopen" leaves you on the old code, with no symptom other
    /// than a feature quietly not working. Measured once: a gateway served for
    /// two hours across four rebuilds. An orphaned process is not a feature you
    /// can see; a checkbox you can read is.
    ///
    /// Optional so a config written before it existed still decodes; `nil` reads
    /// as `false`, which is the new default.
    var keepGatewayRunning: Bool?

    static func makeDefault() -> AppConfig {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let container = "\(home)/Developer/gateway-kh"
        let repository = "\(container)/repository"
        let gatewayDir = FileManager.default.fileExists(atPath: repository)
            ? repository : container
        return AppConfig(
            gatewayDir: gatewayDir,
            port: 8000,
            autoStartGateway: true,
            keepGatewayRunning: false
        )
    }
}

final class ConfigStore {
    static let shared = ConfigStore()
    private let fileURL: URL
    private(set) var config: AppConfig

    private init() {
        fileURL = AppPaths.configFile
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

/// Reads and writes the gateway's `.env`.
///
/// This lives in Application Support, not beside the code: the bundle is
/// read-only in normal use and is replaced wholesale on update, so a key stored
/// there would be lost. The gateway reads the same file via `GATEWAY_ENV_FILE`.
enum EnvFile {
    static var url: URL { AppPaths.env }

    static func read() -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
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

    static func apiKey() -> String { read()["GATEWAY_API_KEY"] ?? "" }

    /// The address the gateway binds, from `.env`.
    ///
    /// Loopback unless the owner says otherwise, and the default is what keeps the
    /// endpoint private. Setting `GATEWAY_HOST=0.0.0.0` (or a Tailscale address) is
    /// what makes it reachable from another device — and the app must honour it,
    /// because it used to pass `--host 127.0.0.1` on the command line, which
    /// overrode the file and made the setting a lie.
    static func host() -> String {
        let value = (read()["GATEWAY_HOST"] ?? "").trimmingCharacters(in: .whitespaces)
        return value.isEmpty ? "127.0.0.1" : value
    }

    /// Whether the gateway is bound beyond loopback, which is what makes the admin
    /// API ask for the key.
    static func bindsBeyondLoopback() -> Bool {
        !["127.0.0.1", "localhost", "::1"].contains(host())
    }

    /// Update the keys this app owns, and **leave every other line untouched**.
    ///
    /// This used to write five hardcoded lines over the whole file. That deleted
    /// every provider API key the owner had put there — `.env` is the documented
    /// no-UI way to add one — and silently reset a deliberate `GATEWAY_HOST` LAN
    /// bind back to loopback. Nothing said so: the only symptom was a provider
    /// that quietly stopped working, or a key that "vanished".
    ///
    /// A `GATEWAY_HOST` the owner set by hand is left as it is; the app writes
    /// loopback only when nothing states a host.
    static func writeApiKey(_ key: String) {
        var owned: [String: String] = [
            "GATEWAY_API_KEY": key,
            "GATEWAY_PORT": "\(ConfigStore.shared.config.port)",
            // Point the gateway at the same data directory the app uses, so both
            // halves agree without either having to guess.
            "GATEWAY_DATA": AppPaths.data.path,
            "GATEWAY_ENV_FILE": AppPaths.env.path,
        ]

        let existing = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        let lines = existing.isEmpty
            ? []
            : existing.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)

        /// The name on the left of a `KEY=value` line, ignoring comments.
        func name(of line: String) -> String? {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.hasPrefix("#"), let eq = trimmed.firstIndex(of: "=") else { return nil }
            return String(trimmed[..<eq])
        }

        if !lines.contains(where: { name(of: $0) == "GATEWAY_HOST" }) {
            owned["GATEWAY_HOST"] = "127.0.0.1"
        }

        var written: Set<String> = []
        var output: [String] = []
        for line in lines {
            if let name = name(of: line), let value = owned[name], !written.contains(name) {
                output.append("\(name)=\(value)")
                written.insert(name)
            } else {
                output.append(line)
            }
        }
        for (name, value) in owned.sorted(by: { $0.key < $1.key }) where !written.contains(name) {
            output.append("\(name)=\(value)")
        }
        if existing.isEmpty {
            output.insert(contentsOf: ["# Written by the gateway-kh menu bar app.", ""], at: 0)
        }

        let text = output.joined(separator: "\n").trimmingCharacters(in: .newlines) + "\n"
        try? text.write(to: url, atomically: true, encoding: .utf8)
        // Owner-only: this file holds the key Story Studio presents.
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
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
    /// Pid of a gateway this app adopted rather than spawned.
    private var adoptedPid: pid_t?
    /// Set by the app once the status item exists, so the state file can prove
    /// the UI actually initialised.
    var statusItemReady = false

    var port: Int { ConfigStore.shared.config.port }
    var gatewayDir: String { ConfigStore.shared.config.gatewayDir }
    var consoleURL: URL? { URL(string: "http://127.0.0.1:\(port)/admin") }
    var storyURL: URL? { URL(string: "http://127.0.0.1:\(port)") }

    /// Where the gateway runs from.
    ///
    /// A shipped bundle carries its own Python and a copy of the source, so it
    /// depends on nothing outside itself — that is what makes it movable. A
    /// developer checkout is the fallback, which keeps the usual edit-and-run
    /// loop working without rebuilding the bundle each time.
    enum Runtime {
        case bundled(python: URL, source: URL)
        case checkout(root: URL)
        case missing(String)
    }

    var runtime: Runtime {
        if let python = AppPaths.bundledPython,
           let source = AppPaths.bundledSource,
           FileManager.default.fileExists(atPath: python.path),
           FileManager.default.fileExists(atPath: source.path) {
            return .bundled(python: python, source: source)
        }
        let root = URL(fileURLWithPath: gatewayDir, isDirectory: true)
        if FileManager.default.fileExists(
            atPath: root.appendingPathComponent(".venv/bin/python").path
        ) {
            return .checkout(root: root)
        }
        return .missing("The app has no bundled runtime and there is no .venv in \(gatewayDir).")
    }

    var isReady: Bool {
        if case .missing = runtime { return false }
        return true
    }

    var runtimeDescription: String {
        switch runtime {
        case .bundled: return "Bundled runtime"
        case .checkout: return "Developer checkout (\(gatewayDir))"
        case .missing(let reason): return reason
        }
    }

    var logPath: String { AppPaths.logFile.path }

    /// The one line pinned to the bottom of the sidebar, mirroring Studio Hub's
    /// connection line. It answers "is it up, and what is on offer" without
    /// making the owner change pages to find out.
    var sidebarSummary: String {
        switch state {
        case .running(let providers, let models):
            return "Running on 127.0.0.1:\(port)\n\(providers) providers · \(models) models live"
        case .starting:
            return "Starting on 127.0.0.1:\(port)…"
        case .stopped:
            return "Stopped\n127.0.0.1:\(port)"
        case .failed(let reason):
            return "Failed: \(reason)"
        }
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
    var stateFileURL: URL { AppPaths.stateFile }

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
        if let adopted = adoptedPid { payload["adoptedPid"] = Int(adopted) }
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

        let executable: URL
        let workingDirectory: URL
        switch runtime {
        case .bundled(let python, let source):
            executable = python
            workingDirectory = source
        case .checkout(let root):
            executable = root.appendingPathComponent(".venv/bin/python")
            workingDirectory = root
        case .missing(let reason):
            publish(.failed(reason))
            return
        }

        // Adopt a gateway that is already serving our port rather than fighting
        // it for the bind. This is the normal state when Settings allows the
        // gateway to outlive the app, and without this
        // check the new app would report a failure it had caused itself.
        isStarting = true
        probeExisting { [weak self] alive in
            guard let self else { return }
            if alive {
                self.isStarting = false
                // Adopting a gateway that is still shutting down used to leave
                // `stopping == true`, which is only ever cleared inside `spawn`.
                // When that process then died, the health poll refused to restart
                // it (`guard !stopping`) and the gateway stayed down with the menu
                // reading "Stopped" — after Settings → Save had said it was
                // restarting. Adopting means we are not stopping anything.
                self.stopping = false
                self.publish(.starting)
                self.startHealthPolling()
            } else {
                self.spawn(executable: executable, workingDirectory: workingDirectory)
            }
        }
    }

    /// Probe first, then stop whatever is serving the port — spawned or adopted.
    ///
    /// `stop()` alone cannot do this from a cold start, because the adopted pid
    /// is only learned by probing.
    func stopAnything(_ completion: @escaping (Bool) -> Void) {
        probeExisting { [weak self] alive in
            guard let self, alive else {
                completion(false)
                return
            }
            self.stop()
            completion(true)
        }
    }

    func probeExisting(_ completion: @escaping (Bool) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            var found: pid_t?
            if code == 200, let data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let raw = json["pid"] as? Int, raw > 0 {
                found = pid_t(raw)
            }
            DispatchQueue.main.async {
                // Remember whose service this is. The app may adopt a gateway
                // it did not spawn (when Settings leaves it running), and without
                // the pid "Stop Gateway" could only
                // pretend to stop it.
                self?.adoptedPid = code == 200 ? found : nil
                completion(code == 200)
            }
        }.resume()
    }

    private func spawn(executable: URL, workingDirectory: URL) {
        let task = Process()
        task.executableURL = executable
        task.arguments = [
            // Python caches inside the signed bundle would invalidate its seal.
            "-B", "-m", "uvicorn", "gateway.api:app",
            // Read, not hard-coded. This used to pass `127.0.0.1` unconditionally,
            // so a `GATEWAY_HOST` in `.env` was written by this app, shown to the
            // owner as a setting, and then quietly overridden here — the endpoint
            // could never be reached from another device, which is the whole point
            // of setting it.
            "--host", EnvFile.host(), "--port", String(port),
            "--no-proxy-headers",
            "--log-level", "warning",
        ]
        task.currentDirectoryURL = workingDirectory

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        // The app and the gateway must agree on where state lives, so the paths
        // are passed explicitly rather than left for the gateway to infer.
        environment["GATEWAY_APP_SUPPORT"] = AppPaths.appSupport.path
        environment["GATEWAY_DATA"] = AppPaths.data.path
        environment["GATEWAY_ENV_FILE"] = AppPaths.env.path
        // Run the source in place; no install step required.
        environment["PYTHONPATH"] = workingDirectory.path
        // The gateway reports this at /health, so the app can tell whether the
        // process it is talking to is the one it just installed.
        environment["GATEWAY_BUILD"] = appBuild
        // **The same host on both sides of the decision.** The gateway's admin auth
        // asks itself "am I bound to loopback?" by reading `GATEWAY_HOST`, while
        // uvicorn is bound by the `--host` argument above. Passed explicitly so the
        // two cannot disagree: a process bound to 0.0.0.0 that still believed it was
        // on loopback would leave the admin API — which can change providers and
        // spend money — open to the whole network.
        environment["GATEWAY_HOST"] = EnvFile.host()
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
            //
            // Bounded, like the probe path (`restartAttempts < 8`). This path had
            // no cap at all, so a gateway that could not start — most often
            // because something else already held the port — was respawned every
            // 30 seconds forever, writing a failure into the log each time.
            self.restartAttempts += 1
            if self.restartAttempts > 8 {
                self.publish(.failed(
                    "Gateway exited (code \(finished.terminationStatus)) and has not come back "
                    + "after \(self.restartAttempts - 1) attempts. Check the log, then Start Gateway."
                ))
                return
            }
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
            // We own this one now, so there is nothing adopted to stop.
            adoptedPid = nil
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
        publish(.stopped)

        if let task = process {
            task.terminate()
            process = nil
            logHandle?.closeFile()
            logHandle = nil
        } else if let adopted = adoptedPid {
            // Adopted, not spawned. Stopping it is still what was asked for.
            kill(adopted, SIGTERM)
            adoptedPid = nil
        }
        // Whatever else is holding the port, even if we never learned its pid —
        // which is exactly the case that makes a restart silently ineffective.
        killPortHolders()
    }

    /// Kill anything listening on our port, by asking the OS who that is.
    ///
    /// `stop()` alone could only kill a process it already knew about, so a
    /// gateway whose `/health` had been unreachable — or one started before this
    /// app ran — survived a restart and was then adopted. That is the difference
    /// between "restart" and "restart, probably". `lsof -ti` names the holders
    /// regardless of how they started.
    /// Is anything still listening on our port?
    func portIsHeld() -> Bool {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-ti", "tcp:\(port)", "-sTCP:LISTEN"]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        do { try task.run() } catch { return false }
        task.waitUntilExit()
        return task.terminationStatus == 0
    }

    @discardableResult
    func killPortHolders() -> [pid_t] {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        task.arguments = ["-ti", "tcp:\(port)", "-sTCP:LISTEN"]
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        do {
            try task.run()
        } catch {
            return []
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        let pids = (String(data: data, encoding: .utf8) ?? "")
            .split(whereSeparator: { $0 == "\n" || $0 == " " })
            .compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
            .filter { $0 > 0 && $0 != getpid() }
        for pid in pids { kill(pid, SIGTERM) }
        return pids
    }

    // MARK: Build identity

    /// The build stamp of the app that is running.
    lazy var appBuild: String = {
        Bundle.main.object(forInfoDictionaryKey: "GatewayBuild") as? String ?? "unknown"
    }()

    /// What the running gateway says it is, or `nil` when nothing answers.
    func runningBuild(_ completion: @escaping (String?) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else {
            completion(nil); return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 4.0
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            var build: String?
            if code == 200, let data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                build = json["build"] as? String
            }
            DispatchQueue.main.async { completion(build) }
        }.resume()
    }

    /// A sentence a person can act on, comparing the two builds.
    ///
    /// This is the check that was missing: an adopted gateway running code from
    /// before a rebuild has no symptom except a feature that quietly does not
    /// work, and the app had no way to say so.
    func buildReport(_ completion: @escaping (String, Bool) -> Void) {
        runningBuild { [weak self] running in
            guard let self else { return }
            guard let running else {
                completion("Nothing is listening on port \(self.port), so nothing is running.", false)
                return
            }
            if running == "unknown" {
                completion("A gateway is running but does not say which build it is. "
                           + "Restart it to be sure.", false)
            } else if running == self.appBuild {
                completion("Gateway is up to date — build \(running).", true)
            } else {
                completion("The gateway is running build \(running), but this app is "
                           + "\(self.appBuild). It is serving older code — restart it.",
                           false)
            }
        }
    }

    /// Stop whatever is running and start this build, for the menu and the button.
    func restartCleanly() {
        killPortHolders()
        process = nil
        adoptedPid = nil
        stopping = false
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            self?.start()
        }
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
            let models = (payload["models"] as? [String: Any])?["publishable_models"] as? Int ?? 0
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
            // `--background` keeps a login launch quiet: starting at login should
            // put the icon in the menu bar, not open a window in your face.
            "ProgramArguments": [executable, "--background"],
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
        // **Built from the section list, not by hand.** Adding the six Create lanes
        // to the sidebar without adding them here left the View menu offering a
        // destination nothing could reach — the UI suite caught it by trying to
        // click "Audio" and finding no such item. One action for every row means the
        // menu cannot fall behind the sidebar again.
        //
        // The key equivalents are deliberately unchanged for the pages that already
        // had them (⌘1 Overview, ⌘2 Jobs, ⌘3 Voices, ⌘4 Models, ⌘5 Providers,
        // ⌘6 Fleet): renumbering to match the new order would silently retrain the
        // owner's fingers. The Create lanes are reachable by name and by sidebar.
        for (title, key) in MainWindow.viewMenuSections {
            viewMenu.addItem(
                withTitle: title,
                action: #selector(AppDelegate.openSectionRow(_:)),
                keyEquivalent: key
            ).target = target
        }
        viewMenu.addItem(NSMenuItem.separator())
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

    /// Quitting the app stops the gateway it started, unless asked otherwise.
    ///
    /// **This is a behaviour change, and the old one was a mistake.** Leaving the
    /// gateway running means the next launch *adopts* it, so "quit and reopen"
    /// silently keeps serving pre-rebuild code — measured once at two hours across
    /// four rebuilds, with no symptom but a feature quietly not working. The
    /// convenience it bought (Story Studio keeps working when the app is closed)
    /// is real, so it is one checkbox away in Settings — but the safe thing is now
    /// the default, and the app says which build is running either way.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if ConfigStore.shared.config.keepGatewayRunning == true {
            return .terminateNow
        }
        GatewayController.shared.stop()
        // Terminating a process is asynchronous, so give the port a moment to be
        // released — otherwise the next launch races the old listener and adopts
        // it, which is the whole problem this is here to fix.
        GatewayController.shared.killPortHolders()
        let deadline = Date().addingTimeInterval(2.0)
        while Date() < deadline, GatewayController.shared.portIsHeld() {
            usleep(100_000)
        }
        return .terminateNow
    }

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

        // First run: guarantee a key exists. The gateway treats an empty key as
        // "accept any bearer token", which is fine on loopback but should never
        // be the state an owner is left in without being told.
        if EnvFile.apiKey().isEmpty {
            EnvFile.writeApiKey(EnvFile.generateKey())
        }

        // Show the window on a normal launch. An app you double-click in
        // Applications that opens nothing looks broken; the menu bar icon is a
        // convenience, not the whole interface. A login launch passes
        // `--background` and stays quiet.
        let backgroundLaunch = arguments.contains("--background")
        if !backgroundLaunch {
            MainWindow.shared.show(section: "overview")
        }
        ConfigStore.shared.update { $0.hasLaunchedBefore = true }

        // Editing shortcuts (⌘A/⌘C/⌘V/⌘X/⌘Z) are dispatched through the main
        // menu, so it has to exist even though an accessory app never shows a
        // menu bar. See MainMenuBuilder.
        NSApp.mainMenu = MainMenuBuilder.make(target: self)

        if let requested = launchWindow?.lowercased() {
            switch requested {
            case "settings": MainWindow.shared.show(section: "settings")
            case "console": MainWindow.shared.show(section: "providers")
            default: MainWindow.shared.show(section: requested)
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
        // applicationShouldTerminate already applied the Keep running setting and
        // stopped the child when needed. There is no second lifecycle action here.
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
        menu.addItem(item("Voices", #selector(openVoices), key: "v"))
        menu.addItem(item("Models", #selector(openModels), key: "m"))
        menu.addItem(item("Fleet", #selector(openFleet), key: "f"))
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

        menu.addItem(item("Check Gateway…", #selector(checkGateway)))
        menu.addItem(item("What's New", #selector(showWhatsNew)))
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
    @objc func openOverview() { MainWindow.shared.show(section: "overview") }

    /// One action for every View-menu row, so the menu is data rather than a
    /// hand-kept list that can fall behind the sidebar.
    @objc func openSectionRow(_ sender: NSMenuItem) {
        MainWindow.shared.show(section: sender.title.lowercased())
    }

    @objc func openJobs() { MainWindow.shared.show(section: "jobs") }

    @objc func openVoices() { MainWindow.shared.show(section: "voices") }

    @objc func openModels() { MainWindow.shared.show(section: "models") }

    @objc func openFleet() { MainWindow.shared.show(section: "fleet") }

    @objc func openConsole() { MainWindow.shared.show(section: "providers") }

    @objc func reloadConsole() { MainWindow.shared.reloadCurrentSection() }

    @objc func openConsoleInBrowser() {
        if let url = GatewayController.shared.consoleURL { NSWorkspace.shared.open(url) }
    }

    @objc func openSettings() { MainWindow.shared.show(section: "settings") }

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

    @objc func openData() {
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: AppPaths.data.path)
    }

    @objc private func openLog() {
        NSWorkspace.shared.selectFile(
            GatewayController.shared.logPath, inFileViewerRootedAtPath: ""
        )
    }

    @objc private func quit() { NSApp.terminate(nil) }

    /// The same two answers the Overview page gives, from the menu bar, because
    /// "is this the latest build?" is asked when something looks wrong — and the
    /// window may not be open.
    @objc private func checkGateway() {
        GatewayController.shared.buildReport { message, _ in
            let alert = NSAlert()
            alert.messageText = "Gateway status"
            alert.informativeText = message
            alert.alertStyle = .informational
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Restart Gateway")
            if alert.runModal() == .alertSecondButtonReturn {
                GatewayController.shared.restartCleanly()
            }
        }
    }

    @objc private func showWhatsNew() { WhatsNewWindow.shared.show() }

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
        // **Not "local models available".** That claim came from a bare
        // `/api/health` 200, and this session measured a Hub answering health in
        // 1.7 ms with no model loaded at all. A health check says the service is up;
        // it says nothing about what it can run, so the menu bar now claims only
        // what it measured. The stronger sentence needs `/api/hub/model-packages`,
        // which takes the operator token — the gateway's own status route already
        // answers that question with real numbers.
        studioHubItem?.title = studioHubRunning
            ? "Studio Hub running"
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

if arguments.contains("--stop-gateway") {
    // Verifies that the app can stop a gateway it *adopted*, not just one it
    // spawned — the case that used to leave the service running while the menu
    // claimed it had stopped.
    _ = NSApplication.shared
    var finished = false
    var stopped = false
    GatewayController.shared.stopAnything { result in
        stopped = result
        finished = true
    }
    // Pump the run loop rather than blocking on a semaphore: the completion is
    // delivered on the main queue, so blocking main would deadlock until the
    // timeout and report a false negative.
    let deadline = Date().addingTimeInterval(10)
    while !finished, Date() < deadline {
        RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }
    Thread.sleep(forTimeInterval: 1.5)
    print(stopped ? "gateway stopped" : "no gateway was running")
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
