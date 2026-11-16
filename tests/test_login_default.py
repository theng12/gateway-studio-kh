"""Launch at login is on by default, switchable off (menubar/LoginDefault.swift).

On 2026-11-08 this Mac restarted and the gateway did not come back: the LaunchAgent was only ever
written by the Settings checkbox. Pinned here: the decision itself (native), that the app runs it
at every launch, and that unticking the box records an explicit "off" the next launch respects.

1301, 2026-11-17: the app also owns the label `kh.gateway.menubar`. An older build registered it
through `SMAppService`, and that registration can only be cleared by a bundle that carries the
agent plist — so the bundle carries it again, and `isLoaded` means *the plist's own job*.
"""
from __future__ import annotations

import plistlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


def _job_check_source() -> str:
    """The pure job check, lifted out of `main.swift` so it can be compiled on its own."""
    main = (MENUBAR / "main.swift").read_text()
    start = main.index("// BEGIN launch-agent-job-check")
    end = main.index("// END launch-agent-job-check")
    return main[start:end]


def _swift_value(text: str):
    """A value from the `writePlist` dictionary, read as Python."""
    text = text.strip()
    if text == "true":
        return True
    if text == "false":
        return False
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if ":" in inner:
            out = {}
            for part in inner.split(","):
                key, _, value = part.partition(":")
                out[key.strip().strip('"')] = _swift_value(value)
            return out
        return [_swift_value(part) for part in inner.split(",")]
    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return text


class LoginDefaultNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="login-default-")
        temp = Path(cls._temp.name)
        binary = temp / "login-default-check"
        job_check = temp / "launch-agent-job-check.swift"
        job_check.write_text(_job_check_source())
        build = subprocess.run(
            ["swiftc", "-swift-version", "5", "-framework", "Foundation",
             "menubar/LoginDefault.swift", str(job_check), "tests/test_login_default.swift",
             "-o", str(binary)],
            cwd=ROOT, capture_output=True, text=True,
        )
        if build.returncode != 0:
            raise AssertionError(build.stderr or build.stdout)
        cls.binary = binary

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def _run(self) -> str:
        run = subprocess.run([str(self.binary)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run.stdout

    def test_the_decision(self) -> None:
        out = self._run()
        self.assertIn("PASS: login default", out)

    def test_a_submitted_row_is_not_the_plists_job(self) -> None:
        """`isLoaded` must mean the plist's own job: `launchctl print` exits 0 for any job wearing
        the label, and the old SMAppService row prints `path = (submitted by smd.93)`."""
        out = self._run()
        self.assertIn("PASS: login default", out)
        self.assertNotIn("FAIL", out)


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
        self.assertIn('"ThrottleInterval": 15', write,
                      "seconds, not launchd's 10 s default that ran 10 060 times on 1301")

    def test_it_loads_the_job_without_needing_a_certificate(self) -> None:
        install = self._function("install", "static func uninstall(")
        self.assertIn("writePlist(executable: executable)", install)
        self.assertIn("loadPlist(executable: executable)", install)
        load = self._function("loadPlist", "/// One plist, one program.")
        self.assertIn("bootstrap(executable: executable)", load)
        boot = self._function("bootstrap", "@discardableResult")
        self.assertIn('"bootstrap", "gui/', boot)
        self.assertIn("status != 37", boot, "already-loaded is not a failure")

    def test_the_app_never_registers_through_smappservice_and_clears_an_old_row(self) -> None:
        """Owner ruling 2026-11-16: stay certificate-free; one thing owns the label."""
        self.assertNotIn(".register()", self.agent, "never registers through SMAppService again")
        clear = self._function("clearLegacyRegistration", "static func repairIfNeeded(")
        self.assertIn("record.unregister()", clear)
        self.assertIn(".notRegistered", clear, "only when a row exists")
        repair = self._function("repairIfNeeded", "static var currentBuild")
        self.assertIn("clearLegacyRegistration()", repair, "cleared on launch")

    def test_a_failed_unregister_is_not_silent(self) -> None:
        """A swallowed failure left the old row holding the label, and every later import was
        refused with "same label as an existing service" (1301, 2026-11-17)."""
        clear = self._function("clearLegacyRegistration", "static func repairIfNeeded(")
        self.assertNotIn("try? record.unregister()", clear)
        self.assertIn("catch", clear)
        self.assertIn("NSLog", clear, "the refusal is said out loud")

    def test_the_clear_runs_before_the_bootstrap(self) -> None:
        install = self._function("install", "static func uninstall(")
        self.assertLess(install.index("clearLegacyRegistration()"), install.index("loadPlist("),
                        "the old row is cleared before the plist is imported")
        repair = self._function("repairIfNeeded", "static var currentBuild")
        self.assertLess(repair.index("clearLegacyRegistration()"), repair.index("refresh(executable: current)"))

    def test_a_leftover_row_is_booted_out_and_the_import_retried(self) -> None:
        load = self._function("loadPlist", "/// One plist, one program.")
        self.assertIn("bootstrap(executable: executable)", load)
        self.assertIn("labelIsTaken", load, "a row wearing the label is found")
        self.assertIn('"bootout", "gui/', load, "and booted out once")
        self.assertIn("isLoaded", load, "and the plist has to end up as the loaded job")

    def test_is_loaded_means_the_plists_own_job(self) -> None:
        loaded = self.agent[self.agent.index("static var isLoaded:"):self.agent.index("static var labelIsTaken")]
        self.assertIn("LaunchAgentJobCheck.isPlistJob", loaded)
        self.assertIn("plistURL.path", loaded, "the job has to name our own plist")

    def test_one_record_only_and_nothing_left_behind(self) -> None:
        install = self._function("install", "static func uninstall(")
        self.assertIn("clearLegacyRegistration()", install, "an older SMAppService record is dropped")
        uninstall = self._function("uninstall", "private static func writePlist(")
        self.assertIn('"bootout", "gui/', uninstall)
        self.assertIn("removeItem(at: plistURL)", uninstall)
        self.assertIn("clearLegacyRegistration()", uninstall)

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
        self.assertIn("if !isLoaded { loadPlist(executable: executable) }", refresh)
        # And a plist at the right path that the app cannot read is not counted as installed.
        self.assertIn("plistExecutable != nil", self.agent.split("static var isInstalled")[1].split("}")[0])

    def test_the_bundle_carries_the_plist_so_the_old_row_can_be_cleared(self) -> None:
        """`SMAppService.agent(plistName:)` finds the agent in the app bundle. With the plist gone
        the status was `.notFound`, the old row was never cleared and the label stayed taken
        (1301, 2026-11-17), so the bundle carries it again — to be cleared, never to be used."""
        self.assertIn("Contents/Library/LaunchAgents", self.plist)
        self.assertIn("kh.gateway.menubar.plist", self.plist)
        self.assertNotIn("SMAppService.agent(plistName:", self._function("writePlist", "private static func bootstrap("))
        self.assertIn("Library/LaunchAgents", self.plist, "and the build says where the item lives now")

    def test_the_bundled_plist_and_write_plist_never_disagree(self) -> None:
        """Every key `writePlist` writes is in the bundle's plist with the same value; the program
        is spelled bundle-relative there, because that is the only spelling that survives the app
        being moved."""
        bundled = self._bundled_plist()
        written = self._written_plist()
        label = re.search(r'static let label = "([^"]+)"', self.agent).group(1)
        written["Label"] = label
        self.assertEqual(bundled["Label"], label)
        for key, value in written.items():
            if key == "ProgramArguments":
                continue
            self.assertEqual(bundled.get(key), value, f"{key} must be the same in both plists")
        self.assertEqual(written["ProgramArguments"][-1], "--background")
        self.assertEqual(bundled["ProgramArguments"][-1], "--background")
        app_name = re.search(r'APP_NAME="([^"]+)"', self.plist).group(1)
        self.assertEqual(bundled["BundleProgram"], f"Contents/MacOS/{app_name}")
        self.assertEqual(bundled["ProgramArguments"][0], f"Contents/MacOS/{app_name}")
        install = self._function("install", "static func uninstall(")
        self.assertIn("Bundle.main.executableURL", install,
                      "and the plist the app writes names that same executable")

    def _written_plist(self) -> dict:
        body = self._function("writePlist", "private static func bootstrap(")
        out = {}
        for line in body.splitlines():
            match = re.match(r'\s*"(\w+)":\s*(.+),\s*$', line)
            if match:
                out[match.group(1)] = _swift_value(match.group(2))
        return out

    def _bundled_plist(self) -> dict:
        app_name = re.search(r'APP_NAME="([^"]+)"', self.plist).group(1)
        start = self.plist.index("kh.gateway.menubar.plist")
        body = self.plist[start:]
        body = body[body.index("<plist"):body.index("</plist>") + len("</plist>")]
        return plistlib.loads(body.replace("${APP_NAME}", app_name).encode())


if __name__ == "__main__":
    unittest.main()
