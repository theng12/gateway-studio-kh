"""Switching pages must never resize the Gateway main window.

Overview once pinned its stat-tile row to the pane at priority 750. AppKit's own window-size priority is 500, so
a constraint stronger than that is satisfied by resizing the *window*: showing Overview snapped a 1920pt window to
~1177pt, and every page after it inherited that. This builds the real app (minus its entry point, as the render
harness does) against the render fixture gateway, sets a large, a mid and the minimum window size, visits every
page in sequence and asserts the frame never moves.
"""
from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "needs macOS and the Swift toolchain")
class WindowSize(unittest.TestCase):
    def test_every_page_switch_keeps_the_window_size(self):
        with tempfile.TemporaryDirectory(prefix="gateway-window-size-") as directory:
            work = Path(directory)
            support = work / "support"
            support.mkdir()
            sources = work / "src"
            sources.mkdir()
            for swift in (ROOT / "menubar").glob("*.swift"):
                shutil.copy(swift, sources / swift.name)
            subprocess.run([sys.executable, str(ROOT / "menubar/render/patch_sources.py"), str(sources)], check=True)
            shutil.copy(ROOT / "tests/window_size_harness.swift", sources / "main.swift")
            binary = work / "window-size"
            sdk = subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            compiled = subprocess.run(
                ["xcrun", "swiftc", "-swift-version", "5", "-sdk", sdk,
                 "-target", f"{os.uname().machine}-apple-macos13.0",
                 "-framework", "Cocoa", "-framework", "WebKit", "-framework", "AVFoundation", "-framework", "AVKit",
                 *map(str, sorted(sources.glob("*.swift"))), "-o", str(binary)],
                capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-3000:])

            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            (support / "menubar.json").write_text(
                '{"gatewayDir":"%s","port":%d,"autoStartGateway":false,"hasLaunchedBefore":true,'
                '"keepGatewayRunning":false}' % (ROOT, port))
            log = work / "server.log"
            with log.open("w") as out:
                server = subprocess.Popen(
                    [sys.executable, str(ROOT / "menubar/render/fixture_server.py"), "--port", str(port),
                     "--support", str(support)], cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
            try:
                import time
                for _ in range(150):
                    if "READY" in log.read_text():
                        break
                    time.sleep(0.2)
                self.assertIn("READY", log.read_text(), "fixture gateway did not start")
                checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=240,
                                         env={**os.environ, "GATEWAY_APP_SUPPORT": str(support)})
            finally:
                server.send_signal(signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr[-2000:])
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
