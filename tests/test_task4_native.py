"""Focused native transport and failure-state checks for handoff Task 4."""
from __future__ import annotations

import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
KEY = "task4-fixture-key"


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/fixture/redirect-same":
            self.send_response(302)
            self.send_header("Location", "/fixture/auth")
            self.end_headers()
            return
        if self.path in {"/fixture/redirect-cross", "/fixture/redirect-json"}:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/not-this-origin")
            self.end_headers()
            return

        expected = "Bearer " + KEY
        if self.path in {"/fixture/auth", "/fixture/slow"} and self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/fixture/unauthorized":
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/fixture/gone":
            self.send_response(410)
            self.end_headers()
            return
        if self.path == "/fixture/empty":
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/fixture/server-error":
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/fixture/slow":
            time.sleep(0.25)
            body = b"native-fixture"
        elif self.path == "/fixture/auth":
            body = b"native-fixture"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path in {"/fixture/redirect-json", "/fixture/redirect-multipart"}:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/not-this-origin")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


class Task4Native(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.temp = tempfile.TemporaryDirectory(prefix="gateway-task4-native-")
        cls.binary = Path(cls.temp.name) / "task4-native-check"
        compile_result = subprocess.run(
            [
                "swiftc", "-swift-version", "5", "menubar/GatewayAPI.swift",
                "tests/test_task4_native.swift", "-o", str(cls.binary),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if compile_result.returncode:
            raise RuntimeError(compile_result.stderr or compile_result.stdout)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def test_authenticated_download_contract(self) -> None:
        result = subprocess.run(
            [str(self.binary)],
            cwd=ROOT,
            env={
                **os.environ,
                "NATIVE_TEST_PORT": str(self.server.server_address[1]),
                "NATIVE_TEST_KEY": KEY,
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("PASS:", result.stdout)

    def test_native_failure_and_fixture_contracts(self) -> None:
        source = (ROOT / "menubar").joinpath("GatewayAPI.swift").read_text()
        main = (ROOT / "menubar").joinpath("MainWindow.swift").read_text()
        overview = (ROOT / "menubar").joinpath("OverviewWindow.swift").read_text()
        voices = (ROOT / "menubar").joinpath("VoicesWindow.swift").read_text()
        fleet = (ROOT / "menubar").joinpath("FleetSection.swift").read_text()
        # The Fleet page became a shell around five tabs (`FleetTabs.swift`), and
        # "Retry failed" moved into the Locations tab with the rest of the controls.
        # The contract is about the *page*, so both files are read: asserting the
        # old location turned this check red for three days while the button was
        # present and working.
        fleet_tabs = (ROOT / "menubar").joinpath("FleetTabs.swift").read_text()
        settings = (ROOT / "menubar").joinpath("SettingsSection.swift").read_text()
        launcher = (ROOT / "menubar").joinpath("main.swift").read_text()

        self.assertIn("static func download(", source)
        self.assertNotIn("URLSession.shared.dataTask", source)
        self.assertGreaterEqual(source.count("authenticatedSession(for: target, key: key)"), 3)
        self.assertIn("removeAllUserScripts", main)
        self.assertIn("static func refreshCredentials()", main)
        self.assertIn("ConsoleSection.refreshCredentials()", main)
        self.assertIn("timeoutInterval = 60", main)
        self.assertIn("Retry", main)
        self.assertIn("case .failure(let error):", overview)
        self.assertIn("No jobs yet.", overview)
        self.assertIn("Could not load recent activity", overview)
        self.assertIn("Could not discover", fleet)
        self.assertIn("Retry Failed", fleet_tabs)
        self.assertIn("Could not change those voices", voices)
        self.assertIn("Retrying", voices)
        self.assertIn("download(", voices)
        self.assertIn("New requests use it immediately", settings)
        self.assertIn("ConsoleSection.refreshCredentials()", settings)
        self.assertIn("--no-proxy-headers", launcher)
        self.assertIn("GATEWAY_APP_SUPPORT", launcher)
        self.assertIn("private static func explicitAppSupport()", launcher)
        self.assertIn('override.appendingPathComponent("gateway-kh.log")', launcher)
        self.assertIn('environment["GATEWAY_DATA"]', launcher)
        self.assertIn(r'let repository = "\(container)/repository"', launcher)


if __name__ == "__main__":
    unittest.main()
