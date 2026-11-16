// gateway-kh menu bar app
//
// A native macOS status-bar application that supervises the gateway process, so
// the owner never opens a terminal to run it.
//
// Design notes:
//
//  * This is a regular app, not an `LSUIElement`: it has a Dock icon and appears
//    in Cmd-Tab, and its window is the whole interface. The menu bar bolt stays
//    as a convenience, and closing the window keeps the app supervising the
//    gateway rather than quitting.
//  * The gateway itself is a Python service. Rather than pretend otherwise,
//    this supervises the venv that already exists in the gateway directory and
//    surfaces its state. Bundling a Python runtime would make the app
//    self-contained but would also mean shipping a second copy of the
//    interpreter; the directory path is therefore a setting.
//  * Launch-at-login is an `SMAppService.agent` whose plist ships inside the
//    bundle (`Contents/Library/LaunchAgents/`), so it moves with the app and
//    unregistering removes its background-item record. It is on by default and
//    switchable off (`LoginDefault`).
//  * Status is polled from the gateway's own `/health`, so what the menu shows
//    is what the service actually reports, not what the app hopes is true.

import Cocoa
import ServiceManagement
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
    /// The shipped service runs from a real helper bundle, so macOS names it by
    /// "Gateway Studio KH Helper" rather than showing a bare interpreter.
    static var bundledHelper: URL? {
        Bundle.main.bundleURL.appendingPathComponent(
            "Contents/Helpers/Gateway Studio KH Helper.app/Contents/MacOS/Gateway Studio KH Helper"
        )
    }
    /// The helper's executable is the interpreter; this is the runtime it reads.
    static var bundledPythonHome: URL? {
        bundledRoot?.appendingPathComponent("python", isDirectory: true)
    }
    static var bundledSource: URL? {
        bundledRoot?.appendingPathComponent("gateway", isDirectory: true)
    }
}

// MARK: - Configuration

/// The addresses this Mac can be reached on, so the app can tell Story Studio
/// where the gateway is when Story runs on another machine.
///
/// The app used to hardcode `127.0.0.1` in its status line, console link and
/// About panel, which is correct only while Story Studio is on this Mac. Bound
/// beyond loopback, the useful address is the Tailscale one (100.64.0.0/10);
/// the private LAN address is the fallback.
enum LocalAddresses {
    static func ipv4() -> [String] {
        var addresses: [String] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return addresses }
        defer { freeifaddrs(head) }
        var pointer: UnsafeMutablePointer<ifaddrs>? = first
        while let interface = pointer {
            pointer = interface.pointee.ifa_next
            guard let socketAddress = interface.pointee.ifa_addr,
                  socketAddress.pointee.sa_family == UInt8(AF_INET) else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(socketAddress, socklen_t(socketAddress.pointee.sa_len),
                              &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
            let ip = String(cString: host)
            if !ip.hasPrefix("127."), !ip.hasPrefix("169.254."), !addresses.contains(ip) {
                addresses.append(ip)
            }
        }
        return addresses
    }

    /// Tailscale hands out 100.64.0.0/10: first octet 100, second 64...127.
    static func isTailscale(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".")
        guard parts.count == 4, parts[0] == "100", let second = Int(parts[1]) else { return false }
        return (64...127).contains(second)
    }

    static func tailscale() -> String? { ipv4().first(where: isTailscale) }

    static func privateLAN() -> String? {
        for ip in ipv4() {
            if ip.hasPrefix("192.168.") || ip.hasPrefix("10.") { return ip }
            let parts = ip.split(separator: ".")
            if parts.count == 4, parts[0] == "172", let second = Int(parts[1]), (16...31).contains(second) {
                return ip
            }
        }
        return nil
    }

    /// What to paste into Story Studio's Base URL for the current bind. Loopback
    /// is the honest answer only while the gateway is bound to loopback.
    static func storyBaseURL(port: Int) -> String {
        if EnvFile.bindsBeyondLoopback() {
            if let address = tailscale() { return "http://\(address):\(port)" }
            if let address = privateLAN() { return "http://\(address):\(port)" }
        }
        return "http://127.0.0.1:\(port)"
    }

    /// The host shown in the sidebar's connection line.
    static func displayHost() -> String {
        if EnvFile.bindsBeyondLoopback() {
            return tailscale() ?? privateLAN() ?? EnvFile.host()
        }
        return "127.0.0.1"
    }
}

struct AppConfig: Codable {
    /// Optional: a shipped install has no developer checkout, and a dead pointer is worse than
    /// none — it is only ever a fallback for running from a checkout, never the bundled path.
    var gatewayDir: String?
    var port: Int
    var autoStartGateway: Bool
    // Existing configs never stored appearance, so no migration is needed.
    var appearance: String?
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
    /// The owner's explicit "off" for launch at login — not a mirror of the plist, which stays
    /// the one record of whether it is on. `nil` (never chosen) means on (`LoginDefault`).
    var launchAtLoginOptedOut: Bool?
    /// The build whose bundle the login item was last registered from (`LaunchAgent.refresh`).
    var launchAgentBuild: String?

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
            gatewayDir: FileManager.default.fileExists(atPath: repository) ? gatewayDir : nil,
            port: 8787,
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

/// Size-based rotation for the gateway's log (owner decision 2026-10-01: nothing bounded it).
///
/// Copy-truncate, because the Python child holds the file open: the live file is copied to
/// `.1` (older ones shift to `.2`) and then truncated in place. That is only safe because the
/// handle is opened `O_APPEND` (see `GatewayController.launch`), so the child's next write
/// lands at the new end instead of at its old offset, which would leave a hole of NULs.
enum LogRotation {
    static let maxBytes: UInt64 = 8 * 1024 * 1024
    static let keep = 2   // gateway-kh.log + .1 + .2 = 24 MB at most

    /// Retire a rotation nobody needs any more into `data/_trash/logs`, where the gateway's own
    /// retention policy moves it to macOS Trash. Never unlinked; a failed move keeps it in place.
    private static func retire(_ path: String, data: URL) -> Bool {
        let fm = FileManager.default
        let folder = data.appendingPathComponent("_trash/logs", isDirectory: true)
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
            let stamp = Int(Date().timeIntervalSince1970)
            var target = folder.appendingPathComponent("\(URL(fileURLWithPath: path).lastPathComponent)-\(stamp)")
            var n = 2
            while fm.fileExists(atPath: target.path) {
                target = folder.appendingPathComponent(
                    "\(URL(fileURLWithPath: path).lastPathComponent)-\(stamp)-\(n)")
                n += 1
            }
            try fm.moveItem(atPath: path, toPath: target.path)
            return true
        } catch {
            return false
        }
    }

    @discardableResult
    static func rotateIfNeeded(path: String, handle: FileHandle?, maxBytes: UInt64 = LogRotation.maxBytes,
                               data: URL = AppPaths.data) -> Bool {
        let fm = FileManager.default
        guard let size = (try? fm.attributesOfItem(atPath: path))?[.size] as? UInt64, size > maxBytes else {
            return false
        }
        // Oldest first: the last rotation retires to the Trash Folder, then .N-1 -> .N, ..., .1 -> .2,
        // and the live file -> .1. Anything that cannot be moved aborts instead of being unlinked.
        let oldest = "\(path).\(keep)"
        if fm.fileExists(atPath: oldest), !retire(oldest, data: data) { return false }
        for index in stride(from: keep - 1, through: 1, by: -1) {
            let newer = "\(path).\(index + 1)"
            let older = "\(path).\(index)"
            guard fm.fileExists(atPath: older) else { continue }
            if fm.fileExists(atPath: newer) { return false }
            do { try fm.moveItem(atPath: older, toPath: newer) } catch { return false }
        }
        let first = "\(path).1"
        guard !fm.fileExists(atPath: first) else { return false }
        guard (try? fm.copyItem(atPath: path, toPath: first)) != nil else { return false }
        if let handle {
            return ftruncate(handle.fileDescriptor, 0) == 0
        }
        if let writer = FileHandle(forWritingAtPath: path) {
            defer { writer.closeFile() }
            return ftruncate(writer.fileDescriptor, 0) == 0
        }
        return false
    }
}

final class GatewayController {
    static let shared = GatewayController()

    private(set) var process: Process?
    private(set) var state: GatewayState = .stopped
    private var logHandle: FileHandle?
    private var logRotationTimer: Timer?
    private var healthTimer: Timer?
    private var onStateChange: (() -> Void)?
    private var stopping = false
    private var restartAttempts = 0
    private var isStarting = false
    private var lifecycleGeneration = 0
    /// Pid of a gateway this app adopted rather than spawned.
    private var adoptedProcess: BackendIdentity?
    /// The build string the last probe found listening, so `start()` can refuse a foreign build.
    private var probedBuild: String?
    private var adoptedPid: pid_t? { adoptedProcess?.pid }

    /// Start time distinguishes a stopped backend from an unrelated reused PID.
    private struct BackendIdentity {
        let pid: pid_t
        let startedSeconds: UInt64
        let startedMicroseconds: UInt64

        init?(_ pid: pid_t) {
            guard pid > 0, pid != getpid(), let info = Self.info(pid) else { return nil }
            self.pid = pid
            startedSeconds = info.pbi_start_tvsec
            startedMicroseconds = info.pbi_start_tvusec
        }

        private static func info(_ pid: pid_t) -> proc_bsdinfo? {
            var info = proc_bsdinfo()
            let size = MemoryLayout<proc_bsdinfo>.size
            guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(size)) == size else { return nil }
            return info
        }

        var isRunning: Bool {
            guard let info = Self.info(pid) else { return kill(pid, 0) == 0 || errno != ESRCH }
            return info.pbi_status != SZOMB
                && info.pbi_start_tvsec == startedSeconds
                && info.pbi_start_tvusec == startedMicroseconds
        }

        func signal(_ value: Int32) {
            // Fail closed if identity cannot be re-read before sending a signal.
            guard let info = Self.info(pid), info.pbi_status != SZOMB,
                  info.pbi_start_tvsec == startedSeconds,
                  info.pbi_start_tvusec == startedMicroseconds else { return }
            kill(pid, value)
        }
    }
    /// Set by the app once the status item exists, so the state file can prove
    /// the UI actually initialised.
    var statusItemReady = false

    var port: Int { ConfigStore.shared.config.port }
    var gatewayDir: String { ConfigStore.shared.config.gatewayDir ?? "" }
    var consoleURL: URL? { URL(string: "http://127.0.0.1:\(port)/admin") }
    var storyURL: URL? { URL(string: "http://127.0.0.1:\(port)") }

    /// Where the gateway runs from.
    ///
    /// A shipped bundle carries its own Python and a copy of the source, so it
    /// depends on nothing outside itself — that is what makes it movable. A
    /// developer checkout is the fallback, which keeps the usual edit-and-run
    /// loop working without rebuilding the bundle each time.
    enum Runtime {
        case bundled(python: URL, pythonHome: URL?, source: URL)
        case checkout(root: URL)
        case missing(String)
    }

    var runtime: Runtime {
        if let source = AppPaths.bundledSource,
           FileManager.default.fileExists(atPath: source.path) {
            // A bundle from before 0.2.243 has no helper; it still runs.
            if let helper = AppPaths.bundledHelper,
               FileManager.default.fileExists(atPath: helper.path) {
                return .bundled(python: helper, pythonHome: AppPaths.bundledPythonHome, source: source)
            }
            if let python = AppPaths.bundledPython,
               FileManager.default.fileExists(atPath: python.path) {
                return .bundled(python: python, pythonHome: nil, source: source)
            }
        }
        // A developer checkout is the only fallback, and only when one is actually configured.
        if let configured = ConfigStore.shared.config.gatewayDir, !configured.isEmpty {
            let root = URL(fileURLWithPath: configured, isDirectory: true)
            if FileManager.default.fileExists(
                atPath: root.appendingPathComponent(".venv/bin/python").path
            ) {
                return .checkout(root: root)
            }
            return .missing("The app has no bundled runtime and there is no .venv in \(configured).")
        }
        return .missing("The app has no bundled runtime and no developer checkout is configured.")
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
        stopping = false

        let executable: URL
        let pythonHome: URL?
        let workingDirectory: URL
        switch runtime {
        case .bundled(let python, let home, let source):
            executable = python
            pythonHome = home
            workingDirectory = source
        case .checkout(let root):
            executable = root.appendingPathComponent(".venv/bin/python")
            pythonHome = nil
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
        let generation = lifecycleGeneration
        probeExisting { [weak self] alive in
            guard let self, self.isStarting, !self.stopping,
                  self.lifecycleGeneration == generation else { return }
            if alive {
                if let served = self.probedBuild, served != GatewayController.shared.appBuild {
                    // **Never adopt another build's gateway.** A leftover helper from an older
                    // bundle answers /health perfectly well, so adopting it would run old code
                    // behind a new app — measured on 1301: a 0.2.241 helper out of an updater
                    // trash folder served :8787 after 0.2.246 was installed. Stop it, then start
                    // ours; if it will not stop, say so instead of pretending.
                    self.isStarting = false
                    guard self.stop() else {
                        self.publish(.failed("A gateway from build \(served) is serving \(self.port) and did not stop. Quit Gateway Studio KH, then start it again."))
                        return
                    }
                    self.isStarting = true
                    self.start()
                    return
                }
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
                // A known backend can still hold the database after its listener closes.
                if self.process != nil || self.adoptedProcess != nil {
                    let attempts = self.restartAttempts
                    guard self.stop() else { return }
                    self.restartAttempts = attempts
                    self.isStarting = true
                }
                guard !self.portIsHeld() else {
                    self.isStarting = false
                    self.publish(.failed("Port \(self.port) is in use. Stop its service before starting Gateway."))
                    return
                }
                self.spawn(executable: executable, workingDirectory: workingDirectory, pythonHome: pythonHome)
            }
        }
    }

    /// Probe first, then stop whatever is serving the port — spawned or adopted.
    ///
    /// `stop()` alone cannot do this from a cold start, because the adopted pid
    /// is only learned by probing.
    func stopAnything(_ completion: @escaping (Bool) -> Void) {
        let generation = lifecycleGeneration
        probeExisting { [weak self] alive in
            guard let self, self.lifecycleGeneration == generation else { completion(false); return }
            guard alive || self.process != nil || self.adoptedProcess != nil else {
                completion(false)
                return
            }
            completion(self.stop())
        }
    }

    func probeExisting(_ completion: @escaping (Bool) -> Void) {
        let generation = lifecycleGeneration
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.0
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            var found: pid_t?
            var build: String?
            if code == 200, let data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let raw = json["pid"] as? Int, raw > 0, raw <= Int(Int32.max) {
                found = pid_t(raw)
                build = json["build"] as? String
            }
            DispatchQueue.main.async {
                guard self?.lifecycleGeneration == generation else { completion(false); return }
                // Remember whose service this is. The app may adopt a gateway
                // it did not spawn (when Settings leaves it running), and without
                // the pid "Stop Gateway" could only
                // pretend to stop it.
                if let found, let identity = BackendIdentity(found) {
                    self?.adoptedProcess = identity
                }
                // What the listener says it is running; a mismatch with our own build is why
                // `start()` refuses to adopt it.
                self?.probedBuild = build
                completion(code == 200)
            }
        }.resume()
    }

    private func spawn(executable: URL, workingDirectory: URL, pythonHome: URL?) {
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
            // **A bound on connections, so a pile-up cannot become the load.** On 2026-10-19 the
            // gateway accepted connections and answered none of them — every thread was parked on
            // a lock a slow fleet fan-out held — and the clients that kept arriving were the load
            // that kept it stuck. uvicorn refuses past this with a `503` instead of accepting
            // work nobody will answer. 256 is well above the fleet's own live capacity and the
            // 40-thread pool the request work runs on, so it can only ever be a runaway client.
            // `api._CLIENT_MAX_IN_FLIGHT` bounds one client below this; this is the process.
            "--limit-concurrency", "256",
            // Without a bound, TERM waits for every open client request first; on 2026-10-16
            // the mini's slow open requests held an install's old backend for 11.6 min with
            // nothing serving. 3 s + the 15 s lifespan drain (`_SHUTDOWN_DRAIN_S`) stays inside
            // `backendDrainWait` (20 s): keep the three in step.
            "--timeout-graceful-shutdown", "3",
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
        // The helper's executable is the interpreter, so it must be told where
        // the bundled runtime's stdlib and packages are.
        if let pythonHome { environment["PYTHONHOME"] = pythonHome.path }
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
        // The owner-console secret (`OwnerConsole`) goes over this pipe, written once after launch: never
        // argv, the environment or a file, all of which other processes of this user can read.
        environment["GATEWAY_OWNER_CONSOLE_STDIN"] = "1"
        let ownerPipe = Pipe()
        task.standardInput = ownerPipe
        task.environment = environment

        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        // O_APPEND so a truncation by `LogRotation` cannot leave the child writing at a stale
        // offset past the new end of file.
        LogRotation.rotateIfNeeded(path: logPath, handle: nil)
        let logDescriptor = open(logPath, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        logHandle = logDescriptor >= 0 ? FileHandle(fileDescriptor: logDescriptor, closeOnDealloc: true) : nil
        logRotationTimer?.invalidate()
        let rotation = Timer(timeInterval: 600, repeats: true) { [weak self] _ in
            guard let self else { return }
            LogRotation.rotateIfNeeded(path: self.logPath, handle: self.logHandle)
        }
        RunLoop.main.add(rotation, forMode: .common)
        logRotationTimer = rotation
        if let handle = logHandle {
            task.standardOutput = handle
            task.standardError = handle
        }

        task.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async { [weak self] in
                guard let self, self.process === finished else { return }
                self.process = nil
                self.logRotationTimer?.invalidate()
                self.logRotationTimer = nil
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
        }

        do {
            try task.run()
            ownerPipe.fileHandleForWriting.write(Data((OwnerConsole.secret + "\n").utf8))
            ownerPipe.fileHandleForWriting.closeFile()
            process = task
            // We own this one now, so there is nothing adopted to stop.
            adoptedProcess = nil
            stopping = false
            isStarting = false
            publish(.starting)
            startHealthPolling()
        } catch {
            isStarting = false
            publish(.failed(error.localizedDescription))
        }
    }

    @discardableResult
    func stop() -> Bool {
        stopping = true
        lifecycleGeneration += 1
        isStarting = false
        restartAttempts = 0
        healthTimer?.invalidate()
        healthTimer = nil
        logRotationTimer?.invalidate()
        logRotationTimer = nil

        let targets = knownBackends()
        terminateBackends(targets)
        guard process?.isRunning != true, !targets.contains(where: { $0.isRunning }) else {
            publish(.failed("Gateway could not stop. Check its service before starting another backend."))
            return false
        }
        process = nil
        adoptedProcess = nil
        logHandle?.closeFile()
        logHandle = nil
        guard !portIsHeld() else {
            publish(.failed("Port \(port) is in use. Stop its service before starting Gateway."))
            return false
        }
        publish(.stopped)
        return true
    }

    private func knownBackends() -> [BackendIdentity] {
        var targets: [BackendIdentity] = []
        if let task = process, task.isRunning,
           let identity = BackendIdentity(task.processIdentifier) { targets.append(identity) }
        if let adoptedProcess { targets.append(adoptedProcess) }
        return targets
    }

    /// After TERM the backend drains: it takes no new work and waits up to 15 s
    /// (`api._SHUTDOWN_DRAIN_S`) for in-flight sends to get an upstream id. A SIGKILL
    /// before that turns a send into a "needs recovery" hold, so wait for it, plus a margin.
    static let backendDrainWait: Double = 20.0

    /// TERM may close the listener while shutdown threads keep the process alive.
    /// Wait for the exact processes, then escalate; never infer exit from a free port.
    private func terminateBackends(_ targets: [BackendIdentity]) {
        for target in targets { target.signal(SIGTERM) }
        func wait(_ seconds: Double) {
            let deadline = ProcessInfo.processInfo.systemUptime + seconds
            while targets.contains(where: { $0.isRunning }),
                  ProcessInfo.processInfo.systemUptime < deadline { usleep(50_000) }
        }
        wait(GatewayController.backendDrainWait)
        for target in targets { target.signal(SIGKILL) }
        wait(1.0)
    }

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

    private func portHolderPIDs() -> [pid_t] {
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
        return pids
    }

    @discardableResult
    func killPortHolders() -> [pid_t] {
        // A listener is not proof of ownership: never signal an unrelated service.
        let pids = Set(portHolderPIDs())
        let targets = knownBackends().filter { pids.contains($0.pid) }
        terminateBackends(targets)
        return targets.map { $0.pid }
    }

    // MARK: Build identity

    /// The user-facing version from the app bundle, or an explicit unknown.
    lazy var appVersion: String = {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "unknown"
    }()

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

    /// Warn before anything that interrupts the gateway while it has work in flight.
    ///
    /// The backend drains on stop (`terminateBackends`): sends in progress get their upstream id
    /// and running work resumes after a restart. Only a send still unanswered after the drain
    /// becomes a manual recovery hold. Fails open: a gateway that does not answer has nothing
    /// to lose, and the owner must always be able to stop it.
    func confirmIfBusy(_ verb: String, then proceed: @escaping (Bool) -> Void) {
        GatewayAPI.get("/admin/api/status", timeout: 3) { result in
            guard case .success(let payload) = result,
                  let byState = (payload["jobs"] as? [String: Any])?["by_state"] as? [String: Any]
            else { return proceed(true) }
            let running = byState["running"] as? Int ?? 0
            let waiting = byState["accepted"] as? Int ?? 0
            guard running + waiting > 0 else { return proceed(true) }
            proceed(PageKit.confirm(
                "\(verb) the gateway?",
                "\(running) job\(running == 1 ? " is" : "s are") running and \(waiting) waiting. "
                    + "Waiting jobs stay queued, and jobs being sent get up to 15 seconds to finish "
                    + "sending. Work a provider cannot resume may need manual recovery on the Jobs page.",
                ok: verb
            ))
        }
    }

    /// Stop whatever is running and start this build, for the menu and the button.
    func restartCleanly() { restart() }

    func restart() {
        guard stop() else { return }
        start()
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
        let generation = lifecycleGeneration
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2.5

        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.stopping, self.lifecycleGeneration == generation else { return }
                guard let data, error == nil,
                      let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else {
                    // The one fault the Gateway cannot report about itself, watched from here
                    // (`GatewayLiveness`): two minutes without an answer and the owner is told.
                    GatewayLiveness.observe(healthy: false)
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
                // Healthy again: clear the backoff so a later crash restarts fast, and say so on
                // Telegram if the owner was told about the outage.
                self.restartAttempts = 0
                GatewayLiveness.observe(healthy: true)
                self.publish(.running(providers: providers, models: models))
            }
        }.resume()
    }
}

// MARK: - Orphan sweep

/// **Nothing this app started may outlive its bundle.** A helper is a child of this app, so a
/// helper running with no parent is either a leftover from a replaced bundle (measured on 1301:
/// a 0.2.241 helper kept serving :8787 out of an updater's trash folder) or one whose app was
/// killed rather than quit. Either way it is stopped here, at launch, before we start our own.
enum OrphanSweep {
    /// The helper's own bundle id; `NSRunningApplication` is the only way to find a process by
    /// the bundle it came from without shelling out.
    static let helperBundleID = "kh.gateway.helper"

    static func stopForeignHelpers() {
        let ours = Bundle.main.bundleURL.standardizedFileURL.path
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: helperBundleID) {
            let pid = app.processIdentifier
            guard pid != getpid() else { continue }
            guard let url = app.bundleURL?.standardizedFileURL.path else { continue }
            // Inside our own bundle, or a copy of our app elsewhere (an updater's archive, an old
            // install): both are ours to clean up. A helper from a *different* app is not.
            let insideOurs = url == ours || url.hasPrefix(ours + "/")
            let copyOfOurs = url.contains("gateway-kh.app")
            guard insideOurs || copyOfOurs else { continue }
            app.terminate()
        }
    }
}

// MARK: - Launch at login

enum LaunchAgent {
    static let label = "kh.gateway.menubar"
    /// The agent registered through `SMAppService`: its plist ships inside this bundle at
    /// `Contents/Library/LaunchAgents/`, so it travels with the app and **unregistering removes the
    /// background-item record**. The old raw `~/Library/LaunchAgents` plist did not — measured
    /// 2026-11-12, three stale `kh.gateway.menubar` rows survived an uninstall.
    /// The registered agent, on macOS 13 and later; older systems use the raw plist below.
    @available(macOS 13.0, *)
    static var service: SMAppService { SMAppService.agent(plistName: "\(label).plist") }

    /// Where the plist lives now, in the bundle (for messages and tests).
    static var plistURL: URL {
        Bundle.main.bundleURL.appendingPathComponent("Contents/Library/LaunchAgents/\(label).plist")
    }

    /// The pre-SMAppService location. Nothing writes it any more; it is removed so a machine that
    /// had one keeps a single launch-at-login record instead of two.
    static var legacyPlistURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            .appendingPathComponent("\(label).plist")
    }

    static var isInstalled: Bool {
        if #available(macOS 13.0, *), service.status == .enabled { return true }
        return FileManager.default.fileExists(atPath: legacyPlistURL.path)
    }

    /// The legacy plist's program, read only to decide whether it still points at this app.
    static var legacyExecutable: String? {
        guard let data = try? Data(contentsOf: legacyPlistURL),
              let plist = try? PropertyListSerialization.propertyList(
                  from: data, options: [], format: nil
              ) as? [String: Any],
              let arguments = plist["ProgramArguments"] as? [String]
        else { return nil }
        return arguments.first
    }

    /// Registering is idempotent, so "repair" is: drop the legacy plist if one is left, and make
    /// sure the service is registered. The executable path is no longer stored anywhere by hand —
    /// that is what let an old path strand the agent after a rename.
    static func repairIfNeeded(current: String?) {
        removeLegacyPlist()
        guard !isInstalled else { return refresh(executable: current) }
        if ConfigStore.shared.config.launchAtLoginOptedOut != true {
            install()
        }
    }

    /// This bundle's build stamp (`Contents/Resources/gateway/BUILD`, commit + minute): it changes
    /// with every build, so it changes whenever the code identity does.
    static var currentBuild: String? {
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/gateway/BUILD")
        let stamp = (try? String(contentsOf: url, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return stamp?.isEmpty == false ? stamp : nil
    }

    /// launchd names the job it spawned in the child's environment.
    static var runningAsAgent: Bool {
        ProcessInfo.processInfo.environment["XPC_SERVICE_NAME"] == label
    }

    /// Redo the registration once per new build, through `SMAppService` only, so the record
    /// macOS keeps always describes the app on disk. One log line says what happened.
    static func refresh(executable: String?) {
        guard #available(macOS 13.0, *) else { return }
        let build = currentBuild
        let recorded = ConfigStore.shared.config.launchAgentBuild
        if runningAsAgent || service.status != .enabled {
            if runningAsAgent, let build, recorded != build {
                ConfigStore.shared.update { $0.launchAgentBuild = build }   // it spawned: the record matches
            }
            return
        }
        guard LoginDefault.shouldReregister(enabled: true, registeredBuild: recorded, currentBuild: build,
                                            executable: executable, runningAsAgent: false) else { return }
        do {
            try service.unregister()
            try service.register()
            ConfigStore.shared.update { $0.launchAgentBuild = build }
            NSLog("launch-at-login re-registered for build %@ (was %@)", build ?? "?", recorded ?? "none")
        } catch {
            NSLog("launch-at-login could not be re-registered for build %@: %@",
                  build ?? "?", error.localizedDescription)
        }
    }

    static func install() {
        removeLegacyPlist()
        if #available(macOS 13.0, *) {
            do {
                try service.register()
                ConfigStore.shared.update { $0.launchAgentBuild = currentBuild }
            } catch {
                NSLog("launch-at-login could not be registered: %@", error.localizedDescription)
            }
            return
        }
        installLegacyPlist()
    }

    /// macOS 12 and older: the only option there, and the path that leaves a row behind on delete.
    private static func installLegacyPlist() {
        guard let executable = Bundle.main.executableURL?.resolvingSymlinksInPath().path else { return }
        try? FileManager.default.createDirectory(at: legacyPlistURL.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": [executable, "--background"],
            "RunAtLoad": true,
            "KeepAlive": ["SuccessfulExit": false],
            "ProcessType": "Interactive",
        ]
        if let data = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0) {
            try? data.write(to: legacyPlistURL)
        }
    }

    static func uninstall() {
        // Unregistering is what clears the background-item row; removing the old plist alone did not.
        if #available(macOS 13.0, *) { try? service.unregister() }
        removeLegacyPlist()
    }

    private static func removeLegacyPlist() {
        guard FileManager.default.fileExists(atPath: legacyPlistURL.path) else { return }
        _ = runLaunchctl(["bootout", "gui/\(getuid())/\(label)"])
        try? FileManager.default.removeItem(at: legacyPlistURL)
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
        let appMenu = NSMenu(title: "Gateway Studio KH")
        appItem.submenu = appMenu
        appMenu.addItem(
            withTitle: "About Gateway Studio KH",
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
            withTitle: "Hide Gateway Studio KH",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"
        )
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit Gateway Studio KH",
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
        // **The one Fleet action a web page cannot take.** A permanent Trash Folder delete needs
        // this app's own per-launch owner-console secret, so the web Fleet page offers the
        // recoverable move and says the permanent delete is in the app. This is its entrance.
        viewMenu.addItem(
            withTitle: "Fleet Trash…",
            action: #selector(AppDelegate.openFleetTrash),
            keyEquivalent: ""
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
    /// Retained while `--test-mode` is running, so SIGTERM keeps reaching the handler.
    private var testSignalSource: DispatchSourceSignal?
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
        // Quit takes the service down with it, Keep running setting or not: leaving the helper
        // behind strips the only icon while something still serves the port, and the next
        // launch merely adopts it. stopAnything also covers a gateway this launch adopted.
        var replied = false
        func finish(_ ok: Bool) {
            guard !replied else { return }
            replied = true
            NSApp.reply(toApplicationShouldTerminate: ok)
        }
        // **An unattended quit has nobody to answer a dialog.** The Updater quits by sending a
        // Quit AppleEvent and waits 60 seconds; `PageKit.confirm` is a modal `NSAlert.runModal()`,
        // which never returns when no one is there — so `reply(toApplicationShouldTerminate:)`
        // never fired and the app never quit. Measured on 1301, 2026-11-15: every install failed
        // because the host always has jobs running or queued. Ask only when the question can
        // actually be seen (the app is active and its window is up); otherwise go straight to
        // stopping the gateway.
        let attended = NSApp.isActive && MainWindow.shared.isShown
        // Armed **before** any dialog, and shorter than the installer's wait: whatever happens
        // below, termination is answered within 40 seconds. A modal can never hang it again.
        DispatchQueue.main.asyncAfter(deadline: .now() + 40) { finish(true) }
        guard attended else {
            GatewayController.shared.stopAnything { stopped in finish(stopped) }
            return .terminateLater
        }
        GatewayController.shared.confirmIfBusy("Quit") { ok in
            guard ok else { return finish(false) }
            // A wedged backend must not keep the app alive forever.
            DispatchQueue.main.asyncAfter(deadline: .now() + 25) { finish(true) }
            GatewayController.shared.stopAnything { stopped in finish(stopped) }
        }
        return .terminateLater
    }

    /// Clicking the Dock icon, or activating the app when its window is closed or
    /// hidden, brings the window back. Without this a regular app's Dock icon
    /// looks broken once the window has been closed.
    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {
        if !flag {
            MainWindow.shared.show()
        }
        return true
    }

    /// Closing the window must not quit the app: the gateway keeps running and the
    /// menu bar bolt still controls it. Quit is ⌘Q or the bolt menu.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if CommandLine.arguments.contains("--test-mode") {
            // **The headless entry a test can walk** — no window, no Dock icon, no owner data.
            // GATEWAY_APP_SUPPORT already points AppPaths at a throwaway dir; KH_TEST_PORT keeps the
            // test gateway away from the owner's live port. The console is the same one the app
            // window loads, so a headless browser exercises the real UI and API.
            guard let support = ProcessInfo.processInfo.environment["GATEWAY_APP_SUPPORT"],
                  !support.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                FileHandle.standardError.write(Data(
                    "--test-mode needs GATEWAY_APP_SUPPORT pointing at a throwaway data dir.\n".utf8))
                exit(2)
            }
            guard let rawPort = ProcessInfo.processInfo.environment["KH_TEST_PORT"],
                  let testPort = Int(rawPort), (1...65535).contains(testPort) else {
                FileHandle.standardError.write(Data(
                    "--test-mode needs KH_TEST_PORT for the console.\n".utf8))
                exit(2)
            }
            ConfigStore.shared.update { $0.port = testPort }
            signal(SIGTERM, SIG_IGN)
            let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
            term.setEventHandler {
                GatewayController.shared.stopAnything { _ in exit(0) }
                // A wedged child must not hold the test run open forever.
                DispatchQueue.main.asyncAfter(deadline: .now() + 12) { exit(0) }
            }
            term.resume()
            testSignalSource = term
            var announced = false
            GatewayController.shared.observe {
                let controller = GatewayController.shared
                if case .failed(let reason) = controller.state {
                    FileHandle.standardError.write(Data("gateway could not start: \(reason)\n".utf8))
                    exit(1)
                }
                guard controller.isRunning, !announced else { return }
                announced = true
                print("TEST_URL=http://127.0.0.1:\(controller.port)/admin")
                fflush(stdout)
            }
            GatewayController.shared.statusItemReady = false
            GatewayController.shared.writeState()
            GatewayController.shared.start()
            return
        }
        AppearanceMode.current.apply()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            let image = NSImage(systemSymbolName: "bolt.horizontal.circle",
                                accessibilityDescription: "Gateway Studio KH")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Gateway Studio KH"
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
        let ownExecutable = Bundle.main.executableURL?.resolvingSymlinksInPath().path
        if LoginDefault.shouldInstall(installed: LaunchAgent.isInstalled,
                                      optedOut: ConfigStore.shared.config.launchAtLoginOptedOut,
                                      executable: ownExecutable) {
            LaunchAgent.install()
        } else if ConfigStore.shared.config.launchAtLoginOptedOut != true {
            LaunchAgent.repairIfNeeded(current: ownExecutable)
        }

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
        // Before anything of ours starts: stop a helper from a replaced bundle that nobody owns.
        // At this point in launch we have spawned nothing, so any such process is an orphan.
        OrphanSweep.stopForeignHelpers()

        if ConfigStore.shared.config.autoStartGateway {
            GatewayController.shared.start()
        }
        refreshTitle()
    }

    func applicationWillTerminate(_ notification: Notification) {
        // applicationShouldTerminate stopped the gateway before allowing the exit.
        // There is no second lifecycle action here.
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
        let version = NSMenuItem(title: "Gateway Studio KH \(GatewayController.shared.appVersion)", action: nil, keyEquivalent: "")
        version.isEnabled = false
        menu.addItem(version)
        menu.addItem(item("Quit Gateway Studio KH", #selector(quit), key: "q"))
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
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Gateway Studio KH")
        image?.isTemplate = true
        button.image = image
    }

    // MARK: Actions

    @objc private func startGateway() { GatewayController.shared.start() }
    @objc private func stopGateway() {
        GatewayController.shared.confirmIfBusy("Stop") { if $0 { GatewayController.shared.stop() } }
    }
    @objc private func restartGateway() {
        GatewayController.shared.confirmIfBusy("Restart") { if $0 { GatewayController.shared.restart() } }
    }

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

    /// **Where the permanent delete lives now that the Fleet row is a web page.** The page can move
    /// a Mac's Studio Hub Trash Folder to the macOS Trash (recoverable); the permanent delete takes
    /// the owner-console secret only this app holds, and a web page cannot send it. The page says
    /// so and points here — so this has to exist.
    @objc func openFleetTrash() {
        GatewayAPI.get("/admin/api/fleet", timeout: 45) { result in
            DispatchQueue.main.async {
                guard case .success(let payload) = result,
                      let snapshot = FleetSnapshot.from(payload) else {
                    let alert = NSAlert()
                    alert.messageText = "Could not read the fleet"
                    alert.informativeText = "The Gateway did not answer with a fleet this app can "
                        + "read, so no Mac was chosen and nothing was changed. Start the Gateway, "
                        + "then try again."
                    alert.addButton(withTitle: "OK")
                    alert.closeOnEscape()
                    alert.runModal()
                    return
                }
                let machines = snapshot.machines.filter { $0.locationId != nil && $0.machine != nil }
                guard !machines.isEmpty else {
                    let alert = NSAlert()
                    alert.messageText = "No Mac reported a Trash Folder"
                    alert.informativeText = "No Mac on the fleet answered with a machine id, so "
                        + "there is nothing to open. Refresh the fleet, then try again."
                    alert.addButton(withTitle: "OK")
                    alert.closeOnEscape()
                    alert.runModal()
                    return
                }
                let picker = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 340, height: 26),
                                           pullsDown: false)
                picker.addItems(withTitles: machines.map { machine in
                    let name = machine.label ?? machine.machine ?? "this Mac"
                    return machine.location.map { "\(name) — \($0)" } ?? name
                })
                let alert = NSAlert()
                alert.messageText = "Trash Folder…"
                alert.informativeText = "Choose a Mac. Its Studio Hub Trash Folder opens here, "
                    + "where the permanent delete lives: only this app holds the secret it needs, "
                    + "so the web page can only move items to the macOS Trash."
                alert.accessoryView = picker
                alert.addButton(withTitle: "Open")
                alert.addButton(withTitle: "Cancel")
                guard alert.runModal() == .alertFirstButtonReturn else { return }
                let index = picker.indexOfSelectedItem
                guard index >= 0 && index < machines.count else { return }
                // The "Trash on All Macs…" entry inside the sheet reads this list.
                FleetTrash.everyMac = machines
                FleetTrash.show(machines[index], done: {})
            }
        }
    }

    @objc func openConsole() { MainWindow.shared.show(section: "providers") }

    @objc func reloadConsole() { MainWindow.shared.reloadCurrentSection() }

    @objc func openConsoleInBrowser() {
        if let url = GatewayController.shared.consoleURL { NSWorkspace.shared.open(url) }
    }

    @objc func openSettings() { MainWindow.shared.show(section: "settings") }

    @objc func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let credits = NSMutableAttributedString(
            string: "A personal model gateway.\n\nConsole: http://127.0.0.1:\(GatewayController.shared.port)/admin\n\n",
            attributes: [.font: TextRole.caption.font, .foregroundColor: NSColor.secondaryLabelColor]
        )
        if let notesURL = URL(string: "http://127.0.0.1:\(GatewayController.shared.port)/admin#whatsnew") {
            credits.append(NSAttributedString(
                string: "What's New",
                attributes: [.font: TextRole.caption.font, .link: notesURL]
            ))
        }
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Gateway Studio KH",
            .applicationVersion: GatewayController.shared.appVersion,
            .credits: credits,
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
            alert.closeOnEscape()
            if alert.runModal() == .alertSecondButtonReturn {
                GatewayController.shared.confirmIfBusy("Restart") { if $0 { GatewayController.shared.restartCleanly() } }
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

// **A headless entry for tests** (`--test-mode`): the app runs with no window and no Dock icon,
// so an acceptance or smoke run never takes the owner's desktop. It starts the real gateway on
// the port `KH_TEST_PORT` names, with the throwaway data dir `GATEWAY_APP_SUPPORT` names, prints
// `TEST_URL` once the console answers, and exits on SIGTERM. A test instance is deliberately not
// treated as a duplicate of the owner's running app: it is a separate process with its own port
// and data dir.
let testMode = CommandLine.arguments.contains("--test-mode")

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

if !testMode && !duplicates.isEmpty {
    FileHandle.standardError.write(
        Data("gateway-kh is already running; exiting this copy.\n".utf8)
    )
    exit(0)
}

// A gateway that exits before the owner-console secret is written must not kill this app with SIGPIPE.
signal(SIGPIPE, SIG_IGN)
let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(testMode ? .accessory : .regular)
application.run()
