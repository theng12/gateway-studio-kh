"""Launch at login is on by default, switchable off (menubar/LoginDefault.swift).

On 2026-11-08 this Mac restarted and the gateway did not come back: the LaunchAgent was only ever
written by the Settings checkbox. Pinned here: the decision itself (native), that the app runs it
at every launch, and that unticking the box records an explicit "off" the next launch respects.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


class LoginDefaultNative(unittest.TestCase):
    def test_the_decision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="login-default-") as temp:
            binary = Path(temp) / "login-default-check"
            build = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "Foundation",
                 "menubar/LoginDefault.swift", "tests/test_login_default.swift", "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS: login default", run.stdout)


class TheAppAppliesIt(unittest.TestCase):
    def test_launch_installs_by_default(self) -> None:
        main = (MENUBAR / "main.swift").read_text()
        self.assertIn("LoginDefault.shouldInstall(", main)
        self.assertIn("var launchAtLoginOptedOut: Bool?", main)

    def test_the_switch_records_the_owners_choice(self) -> None:
        settings = (MENUBAR / "SettingsSection.swift").read_text()
        handler = settings[settings.index("func toggleLogin()"):settings.index("func toggleAutoStart()")]
        self.assertIn("launchAtLoginOptedOut = false", handler)
        self.assertIn("launchAtLoginOptedOut = true", handler)


if __name__ == "__main__":
    unittest.main()
