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


class ReregisterOncePerBuild(unittest.TestCase):
    """1301, 2026-11-16: `kh.gateway.menubar` at "spawn failed", last exit 78, runs 10 060, with
    "needs LWCR update" and "inferred program". The app never redid the registration after an
    update, the plist named an absolute program, and KeepAlive retried a refused spawn for ever."""

    def setUp(self) -> None:
        main = (MENUBAR / "main.swift").read_text()
        start = main.index("enum LaunchAgent {")
        self.agent = main[start:main.index("// MARK: - Settings window", start)]
        self.refresh = self.agent[self.agent.index("static func refresh("):self.agent.index("static func install()")]
        self.plist = (MENUBAR / "build.sh").read_text()
        self.plist = self.plist[self.plist.index("kh.gateway.menubar.plist\" <<'PLIST'"):]
        self.plist = self.plist[:self.plist.index("\nPLIST\n")]

    def test_an_installed_agent_is_refreshed_at_launch(self) -> None:
        repair = self.agent[self.agent.index("static func repairIfNeeded("):self.agent.index("static var currentBuild")]
        self.assertIn("return refresh(executable: current)", repair)
        self.assertIn("LoginDefault.shouldReregister(", self.refresh)

    def test_it_goes_through_smappservice_and_never_launchctl(self) -> None:
        self.assertIn("try service.unregister()", self.refresh)
        self.assertIn("try service.register()", self.refresh)
        for raw in ("launchctl", "runLaunchctl", "bootout", "bootstrap", "kickstart"):
            self.assertNotIn(raw, self.refresh)

    def test_it_records_the_build_and_says_what_happened(self) -> None:
        self.assertIn("$0.launchAgentBuild = build", self.refresh)
        self.assertIn('NSLog("launch-at-login re-registered', self.refresh)
        self.assertIn('NSLog("launch-at-login could not be re-registered', self.refresh)
        install = self.agent[self.agent.index("static func install()"):]
        self.assertIn("$0.launchAgentBuild = currentBuild", install[:install.index("installLegacyPlist()")])

    def test_the_plist_names_the_program_inside_the_bundle(self) -> None:
        self.assertIn("<key>BundleProgram</key>\n\t<string>Contents/MacOS/Gateway Studio KH</string>", self.plist)
        self.assertNotIn("/Applications/", self.plist, "an absolute path is what launchd had to infer")

    def test_a_spawn_launchd_refused_is_not_retried_for_ever(self) -> None:
        self.assertIn("<key>Crashed</key>\n\t\t<true/>", self.plist)
        self.assertNotIn("SuccessfulExit", self.plist)


if __name__ == "__main__":
    unittest.main()
