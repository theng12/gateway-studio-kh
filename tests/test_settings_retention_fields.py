"""The Settings page's Received-artifacts limits and its one App Update Key, rendered offscreen.

Two defects, one page, both from an owner's screenshot (2026-10-19):

* **The four limits rendered empty.** `GET /admin/api/settings/retention` answered the limits *and*
  a `usage` report in one response, and the report walks every artifact and trash file — measured
  on this Mac's 10,350 artifacts and 41,668 trash entries: 3.1 s warm, past 15 s cold, and minutes
  once walks queued behind each other. The app gives a plain read 15 s, and on failure it clears
  every field rather than inventing a number, so a slow walk emptied all four. The limits are now
  their own instant read and the report is `GET /admin/api/settings/retention/usage`.
* **The App Update Key showed three numbers** — a fingerprint first, then the base64 key, then the
  hex key with its middle ellipsised — and a Mac stayed broken for days because the fingerprint went
  into Studio Hub's App Update Key field, which takes the hex public key. The page now shows one
  value: the whole hex key, with the eight-character check beside it.

The stub gateway below is the point of the first check. It answers the limits at once and **never**
answers the usage walk, so the numbers can only be right if they no longer ride on that walk.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent

LIMITS = {"max_age_days": 3.0, "max_bytes": 8589934592,
          "trash_days": 7.0, "trash_cap_bytes": 21474836480}


class _Stub(BaseHTTPRequestHandler):
    """The gateway's own shape, with the walk taken out and the walk left in."""

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/admin/api/settings/retention":
            # The limits alone, and instantly. `usage` is deliberately absent: the app reads it
            # from `/usage`, which is the route this stub never answers.
            self._send(200, LIMITS)
        elif path == "/admin/api/settings/retention/usage":
            # The walk. Slow enough that nothing on the page can be waiting for it.
            time.sleep(60)
            self._send(200, {"usage": {"files": 3, "bytes": 1024.0}})
        else:
            self._send(404, {"detail": "no such route"})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"),
                     "needs macOS and the Swift toolchain")
class SettingsPage(unittest.TestCase):
    def test_limits_load_without_the_usage_walk_and_updates_are_one_sentence(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory(prefix="gateway-settings-page-") as directory:
            work = Path(directory)
            support = work / "support"
            support.mkdir()
            (support / ".env").write_text("GATEWAY_API_KEY=settings-page-fixture-key\n")
            (support / "menubar.json").write_text(json.dumps({
                "gatewayDir": str(ROOT), "port": port, "autoStartGateway": False,
                "hasLaunchedBefore": True, "keepGatewayRunning": False,
            }))
            sources = work / "src"
            sources.mkdir()
            for swift in (ROOT / "menubar").glob("*.swift"):
                shutil.copy(swift, sources / swift.name)
            subprocess.run([sys.executable, str(ROOT / "menubar/render/patch_sources.py"), str(sources)],
                           check=True)
            shutil.copy(HERE / "settings_page_harness.swift", sources / "main.swift")
            binary = work / "settings-page"
            sdk = subprocess.run(["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            compiled = subprocess.run(
                ["xcrun", "swiftc", "-swift-version", "5", "-sdk", sdk,
                 "-target", f"{os.uname().machine}-apple-macos13.0",
                 "-framework", "Cocoa", "-framework", "WebKit",
                 "-framework", "AVFoundation", "-framework", "AVKit",
                 *map(str, sorted(sources.glob("*.swift"))), "-o", str(binary)],
                capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr[-3000:])

            checked = subprocess.run([str(binary)], capture_output=True, text=True, timeout=240,
                                     env={**os.environ, "GATEWAY_APP_SUPPORT": str(support)})
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr[-2000:])
            self.assertIn("PASS:", checked.stdout)


class RoutesSayWhatTheyCost(unittest.TestCase):
    """The wire contract the page depends on: the limits read does not walk the store."""

    def test_the_limits_route_carries_no_usage_and_usage_has_its_own(self) -> None:
        source = (ROOT / "gateway" / "admin.py").read_text()
        limits = source.split('@router.get("/api/settings/retention",')[1].split("@router.")[0]
        self.assertIn("return retention.settings()", limits,
                      "the limits read must be the small settings read alone")
        self.assertNotIn("shared_usage", limits,
                         "the limits read must not carry the usage walk")
        usage = source.split('@router.get("/api/settings/retention/usage",')[1].split("@router.")[0]
        self.assertIn("shared_usage", usage)
        # The write answers the same way: a save that walks is a save the app reports as failed.
        write = source.split('@router.put("/api/settings/retention",')[1].split("@router.")[0]
        self.assertNotIn("shared_usage", write,
                         "saving must not wait on the usage walk either")

    def test_the_app_reads_the_two_routes_separately(self) -> None:
        settings = (ROOT / "menubar" / "SettingsSection.swift").read_text()
        self.assertIn('GatewayAPI.get("/admin/api/settings/retention")', settings)
        self.assertIn('GatewayAPI.get("/admin/api/settings/retention/usage"', settings)
        # Updates are one sentence; the app no longer reads or draws an update key (owner rule
        # 2026-11-13: only Studio Launcher KH installs updates).
        self.assertIn('Updates are installed by Studio Launcher KH.', settings)
        self.assertNotIn("/admin/api/release/identity", settings)
        self.assertNotIn("App Update Key", settings)


if __name__ == "__main__":
    unittest.main()
