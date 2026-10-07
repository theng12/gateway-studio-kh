"""Exercise the real Swift supervisor against disposable, TERM-resistant backends.

The fixture releases its ephemeral listener on TERM but stays alive, reproducing
shutdown threads that retain the database after uvicorn has released the port.
No installed app, owner defaults, gateway database, or fixed port is accessed.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = '''import json, os, signal, socket, time
from pathlib import Path
root = Path(os.environ["GATEWAY_APP_SUPPORT"])
port = int(os.environ["FIXTURE_PORT"])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
server.settimeout(.1)
with (root / "starts").open("a") as stream:
    stream.write(str(os.getpid()) + "\\n")
closing = False
def shutdown(*_):
    global closing
    closing = True
    server.close()
    (root / (str(os.getpid()) + ".term")).touch()
signal.signal(signal.SIGTERM, shutdown)
while True:
    if closing:
        time.sleep(.05)
        continue
    try:
        connection, _ = server.accept()
        connection.recv(4096)
        body = json.dumps({"pid": os.getpid(), "providers": {"enabled": 1},
                           "models": {"publishable_models": 1}}).encode()
        status = b"404 Not Found" if os.environ.get("FIXTURE_UNRELATED") else b"200 OK"
        connection.sendall(b"HTTP/1.1 " + status + b"\\r\\nContent-Type: application/json\\r\\nContent-Length: "
                           + str(len(body)).encode() + b"\\r\\nConnection: close\\r\\n\\r\\n" + body)
        connection.close()
    except (TimeoutError, OSError):
        pass
'''

STUBS = r'''
import Cocoa
import WebKit
struct Configuration {
    let port = Int(ProcessInfo.processInfo.environment["FIXTURE_PORT"]!)!
    let gatewayDir = ProcessInfo.processInfo.environment["GATEWAY_APP_SUPPORT"]!
    let autoStartGateway = false
}
final class ConfigStore {
    static let shared = ConfigStore()
    let config = Configuration()
}
enum AppPaths {
    static let appSupport = URL(fileURLWithPath: ConfigStore.shared.config.gatewayDir)
    static let bundledPython: URL? = URL(fileURLWithPath: ProcessInfo.processInfo.environment["FIXTURE_PYTHON"]!)
    static let bundledSource: URL? = appSupport
    static let env = appSupport.appendingPathComponent(".env")
    static let data = appSupport.appendingPathComponent("data")
    static let logFile = appSupport.appendingPathComponent("fixture.log")
    static let stateFile = appSupport.appendingPathComponent("fixture-state.json")
}
enum EnvFile { static func host() -> String { "127.0.0.1" } }
enum LocalAddresses { static func displayHost() -> String { "127.0.0.1" } }
enum LogRotation { static func rotateIfNeeded(path: String, handle: FileHandle?) {} }
enum GatewayAPI {
    static func get(_ path: String, timeout: Double,
                    completion: @escaping (Result<[String: Any], Error>) -> Void) {
        completion(.success([:]))
    }
}
enum PageKit { static func confirm(_ title: String, _ message: String, ok: String) -> Bool { true } }
'''

HARNESS = r'''
func waitFor(_ condition: () -> Bool, seconds: Double = 8) -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while !condition(), Date() < deadline {
        _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
    }
    return condition()
}
func alive(_ pid: pid_t) -> Bool {
    var info = proc_bsdinfo()
    let count = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
    return count == MemoryLayout<proc_bsdinfo>.size && info.pbi_status != SZOMB
}
func starts() -> [pid_t] {
    let text = (try? String(contentsOf: AppPaths.appSupport.appendingPathComponent("starts"))) ?? ""
    return text.split(separator: "\n").compactMap { pid_t($0) }
}
let controller = GatewayController.shared
let mode = CommandLine.arguments[1]
if mode == "unrelated" {
    controller.start()
    guard waitFor({ if case .failed = controller.state { return true }; return false })
    else { fatalError("Start did not report the unrelated listener") }
    controller.stop()
    controller.killPortHolders()
    let pid = starts()[0]
    guard alive(pid), !FileManager.default.fileExists(atPath: AppPaths.appSupport.appendingPathComponent("\(pid).term").path)
    else { fatalError("supervisor signalled an unrelated service on its port") }
    guard starts().count == 1 else { fatalError("Start overlapped the unrelated service") }
    print("PASS: unrelated listener survives Start, Stop, and port repair")
    exit(0)
}
if mode == "stop-pending-start" {
    controller.start()
    controller.stop()
    let deadline = Date().addingTimeInterval(3)
    _ = waitFor({ Date() >= deadline })
    guard starts().isEmpty else { fatalError("pending Start callback spawned a backend after Stop") }
    print("PASS: Stop cancels a pending Start")
    exit(0)
}
controller.start()
guard waitFor({ controller.isRunning && starts().count == 1 }) else {
    fatalError("fixture never became healthy")
}
let oldPID = starts()[0]
if mode == "restart" || mode == "restartCleanly" {
    if mode == "restart" { controller.restart() } else { controller.restartCleanly() }
    guard waitFor({ starts().count == 2 }) else { fatalError("replacement never started") }
    guard !alive(oldPID) else { fatalError("replacement overlaps the old backend after its listener closed") }
    controller.stop()
} else if mode == "adopted-no-listener" {
    kill(oldPID, SIGTERM)
    guard waitFor({ FileManager.default.fileExists(atPath: AppPaths.appSupport.appendingPathComponent("\(oldPID).term").path) })
    else { fatalError("fixture did not release its listener") }
    var finished = false
    controller.stopAnything { _ in finished = true }
    guard waitFor({ finished }) else { fatalError("stop completion never arrived") }
    guard !alive(oldPID) else { fatalError("adopted backend survived Stop after releasing its listener") }
} else {
    controller.stop()
    guard !alive(oldPID) else { fatalError("backend survived Stop after releasing its listener") }
}
print("PASS: \(mode) terminates the backend before returning or replacing it")
'''


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "macOS Swift supervisor check")
class GatewayLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Preserve generated sources and binaries in project _trash on cleanup.
        cls.workspace = Path(tempfile.mkdtemp(prefix="gateway-lifecycle-"))
        source = (ROOT / "menubar/main.swift").read_text()
        state = source[source.index("enum GatewayState {"):source.index("/// Size-based rotation")]
        controller = source[source.index("final class GatewayController {"):source.index("// MARK: - Launch at login")]
        harness = cls.workspace / "main.swift"
        harness.write_text(STUBS + state + controller + HARNESS)
        cls.binary = cls.workspace / "check"
        result = subprocess.run(["swiftc", "-swift-version", "5", "-framework", "Cocoa",
                                 "-framework", "WebKit", str(harness), "-o", str(cls.binary)],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            cls.preserve_workspace()
            raise RuntimeError(result.stderr)

    @classmethod
    def preserve_workspace(cls):
        destination = ROOT / "_trash" / "lifecycle-checks" / cls.workspace.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cls.workspace), destination)

    @classmethod
    def tearDownClass(cls):
        cls.preserve_workspace()

    def check_mode(self, mode):
        support = self.workspace / mode
        support.mkdir()
        (support / "uvicorn.py").write_text(FIXTURE)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        environment = {**os.environ, "GATEWAY_APP_SUPPORT": str(support),
                       "FIXTURE_PORT": str(port), "FIXTURE_PYTHON": sys.executable,
                       "PYTHONPATH": str(support)}
        if mode == "unrelated":
            environment["FIXTURE_UNRELATED"] = "1"
        child = None
        try:
            if mode.startswith("adopted") or mode == "unrelated":
                child = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn"], env=environment)
                deadline = time.monotonic() + 5
                while not (support / "starts").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
            result = subprocess.run([str(self.binary), mode], env=environment,
                                    capture_output=True, text=True, timeout=25)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS:", result.stdout)
        finally:
            # These exact child PIDs were recorded by this isolated fixture only.
            if (support / "starts").exists():
                for value in (support / "starts").read_text().splitlines():
                    try:
                        os.kill(int(value), 9)
                    except ProcessLookupError:
                        pass
            if child:
                child.wait(timeout=3)

    def test_unrelated_listener_is_never_signalled(self):
        self.check_mode("unrelated")

    def test_stop_cancels_pending_start(self):
        self.check_mode("stop-pending-start")

    def test_stop_spawned_backend_after_listener_closes(self):
        self.check_mode("stop")

    def test_stop_adopted_backend_after_listener_closes(self):
        self.check_mode("adopted")

    def test_stop_known_adopted_backend_without_listener(self):
        self.check_mode("adopted-no-listener")

    def test_restart_waits_for_old_backend_exit(self):
        self.check_mode("restart")

    def test_clean_restart_waits_for_old_backend_exit(self):
        self.check_mode("restartCleanly")


if __name__ == "__main__":
    unittest.main()
