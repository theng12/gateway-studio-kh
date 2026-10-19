"""The Gateway's own liveness check (menubar/GatewayLiveness.swift) and the file it reads.

The check runs in the app because the Gateway cannot report its own freeze, and it posts to Telegram
itself because the Gateway's own sender is what is down. What is pinned here is when it speaks, that
it stays quiet about an outage the app's own restart cleared, and that it reads the notifier's own
`telegram.json` — including that event's switch.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"
#: Read as text, never imported: importing `gateway.config` creates the data folder, and a test
#: must not touch the owner's own.
EVENT_KEYS = ('"fleet_watchdog"', '"story_update"', '"gateway_health"')


class GatewayLivenessNative(unittest.TestCase):
    def test_the_state_machine_and_the_credential_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-liveness-") as temp:
            binary = Path(temp) / "gateway-liveness-check"
            build = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit", "-framework", "Foundation",
                 "menubar/GatewayLiveness.swift", "tests/test_gateway_liveness.swift",
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS:", run.stdout)


class TheAppWatchesTheGatewayItself(unittest.TestCase):
    """The wiring: the probe reports to the check, and the check reads the notifier's own file."""

    def test_the_health_probe_is_what_tells_it(self) -> None:
        main = (MENUBAR / "main.swift").read_text()
        self.assertIn("GatewayLiveness.observe(healthy: false)", main,
                      "the failing /health probe is the external measurement")
        self.assertIn("GatewayLiveness.observe(healthy: true)", main,
                      "and the answering one is what clears it")

    def test_it_reads_the_notifiers_own_telegram_file_and_its_own_switch(self) -> None:
        liveness = (MENUBAR / "GatewayLiveness.swift").read_text()
        self.assertIn('appendingPathComponent("telegram.json")', liveness)
        self.assertIn('"gateway_health"', liveness, "this event's own switch silences it")
        hosts = set(re.findall(r"https?://([A-Za-z0-9.:_-]+)", liveness))
        self.assertEqual(hosts, {"api.telegram.org"}, "the one host it ever talks to")

    def test_the_two_sides_agree_on_where_that_file_lives(self) -> None:
        """The app builds the path by hand; the notifier computes it from `config.DATA_DIR`."""
        config = (ROOT / "gateway" / "config.py").read_text()
        self.assertIn('DATA_DIR = Path(os.environ.get("GATEWAY_DATA") or (APP_SUPPORT / "data"))', config)
        main = (MENUBAR / "main.swift").read_text()
        self.assertIn('appendingPathComponent("gateway-kh"', main)
        self.assertIn('appendingPathComponent("data", isDirectory: true)', main)

    def test_every_event_the_watchdog_reports_is_offered_as_a_switch(self) -> None:
        """A message the owner cannot switch off is the one thing the settings list must not do."""
        notifier = (ROOT / "gateway" / "notifier.py").read_text()
        for key in EVENT_KEYS:
            self.assertIn(key, notifier, f"{key} must be a switch the owner can turn off")


if __name__ == "__main__":
    unittest.main()
