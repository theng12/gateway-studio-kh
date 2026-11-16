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


class LoginItemIsAPlistAgent(unittest.TestCase):
    """1301, 2026-11-16: the app is ad-hoc signed, so an SMAppService agent cannot spawn it.

    `job state = spawn failed`, `last exit reason = OS_REASON_CODESIGNING`, even after 0.2.263 made
    the app re-register once per build: an ad-hoc signature's designated requirement is its cdhash,
    and that changes with every build. A plain LaunchAgent runs a path and is not held to a code
    requirement, so it starts an ad-hoc build at login with no certificate and no Keychain item.
    """

    def setUp(self) -> None:
        main = (MENUBAR / "main.swift").read_text()
        start = main.index("enum LaunchAgent {")
        self.agent = main[start:main.index("// MARK: - Settings window", start)]
        self.plist = (MENUBAR / "build.sh").read_text()

    def _function(self, name: str, end: str) -> str:
        return self.agent[self.agent.index(f"static func {name}("):self.agent.index(end, self.agent.index(f"static func {name}("))]

    def test_the_login_item_is_a_plist_in_the_users_launch_agents(self) -> None:
        self.assertIn("Library/LaunchAgents", self.agent)
        write = self._function("writePlist", "private static func bootstrap(")
        self.assertIn('"Label": label', write)
        self.assertIn('"ProgramArguments": [executable, "--background"]', write)
        self.assertIn('"RunAtLoad": true', write)
        self.assertIn('"KeepAlive": ["SuccessfulExit": false]', write,
                      "a crash or a failed spawn restarts; a deliberate Quit (exit 0) stays quit")
        self.assertIn('"ThrottleInterval": 60', write,
                      "one attempt a minute: a refused spawn self-heals, never spins (1301 ran 10 060 times)")

    def test_it_loads_the_job_without_needing_a_certificate(self) -> None:
        install = self._function("install", "static func uninstall(")
        self.assertIn("writePlist(executable: executable)", install)
        self.assertIn("bootstrap(executable: executable)", install)
        boot = self._function("bootstrap", "@discardableResult")
        self.assertIn('"bootstrap", "gui/', boot)
        self.assertIn("status != 37", boot, "already-loaded is not a failure")

    def test_one_record_only_and_nothing_left_behind(self) -> None:
        install = self._function("install", "static func uninstall(")
        self.assertIn("record.unregister()", install, "an older SMAppService record is dropped")
        uninstall = self._function("uninstall", "private static func writePlist(")
        self.assertIn('"bootout", "gui/', uninstall)
        self.assertIn("removeItem(at: plistURL)", uninstall)
        self.assertIn("record.unregister()", uninstall)

    def test_the_copy_launchd_started_never_rewrites_its_own_job(self) -> None:
        refresh = self._function("refresh", "static func install(")
        self.assertIn("if runningAsAgent { return }", refresh)
        self.assertIn('"bootout", "gui/', refresh, "a moved app is re-pointed once")
        self.assertIn("writePlist(executable: executable)", refresh)

    def test_a_missing_stale_or_misdirected_plist_is_put_right(self) -> None:
        # Missing: install writes it.
        install = self._function("install", "static func uninstall(")
        self.assertIn("writePlist(executable: executable)", install)
        # Stale (a plist exists but the app moved) and misdirected (it names another path):
        # `refresh` compares the program the plist names and rewrites it once.
        refresh = self._function("refresh", "static func install(")
        self.assertIn("guard let executable, plistExecutable != executable else {", refresh)
        self.assertIn("writePlist(executable: executable)", refresh)
        # A stale plist whose job is no longer loaded is loaded again without being rewritten.
        self.assertIn("if !isLoaded { _ = bootstrap(executable: executable) }", refresh)
        # And a plist at the right path that the app cannot read is not counted as installed.
        self.assertIn("plistExecutable != nil", self.agent.split("static var isInstalled")[1].split("}")[0])

    def test_the_old_bundled_smappservice_plist_is_gone_from_the_build(self) -> None:
        self.assertNotIn("SMAppService.agent(plistName:", self._function("writePlist", "private static func bootstrap("))
        self.assertNotIn("Contents/Library/LaunchAgents", self.plist,
                         "the bundled agent could not start an ad-hoc build; it is not shipped")
        self.assertIn("Library/LaunchAgents", self.plist, "and the build says where the item lives now")


if __name__ == "__main__":
    unittest.main()
