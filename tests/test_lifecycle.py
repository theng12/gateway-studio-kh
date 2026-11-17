"""Source contracts for the lifecycle rule: quit, second launch, and uninstall.

Owner, 2026-11-12: every app releases everything on quit and on delete — helper stopped, launchd
job unloaded, port freed, no orphan; a second instance reports plainly instead of panicking or
taking the screen; uninstall removes app, helper, launchd jobs and the login item, keeping data.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "menubar"
MAIN = (ROOT / "main.swift").read_text()


class LifecycleTests(unittest.TestCase):
    def test_quit_stops_the_helper_and_covers_an_adopted_gateway(self):
        quit_path = MAIN[MAIN.index("func applicationShouldTerminate("):]
        quit_path = quit_path[:quit_path.index("func applicationShouldTerminateAfterLastWindowClosed")]
        self.assertIn("GatewayController.shared.stopAnything", quit_path)
        self.assertIn("NSApp.reply(toApplicationShouldTerminate", quit_path)
        # A wedged backend must not hold the app open forever.
        self.assertIn("asyncAfter(deadline: .now() + 25)", quit_path)

    def test_a_quit_writes_down_who_asked(self):
        """1301, 2026-11-16: Gateway stopped before 21:38 and no log said why."""
        quit_path = MAIN[MAIN.index("func applicationShouldTerminate("):]
        quit_path = quit_path[:quit_path.index("func applicationShouldTerminateAfterLastWindowClosed")]
        self.assertIn('noteInLog("quit requested by \\(QuitSource.describe())"', quit_path)
        self.assertIn("keySenderPIDAttr", MAIN[MAIN.index("enum QuitSource"):])
        api = (ROOT.parent / "gateway/api.py").read_text()
        term = api[api.index("def on_term("):api.index("signal.signal(signal.SIGTERM, on_term)")]
        self.assertIn("TERM received", term)

    def test_a_second_instance_exits_quietly_without_taking_the_screen(self):
        guard = MAIN[MAIN.index("let duplicates = NSRunningApplication"):]
        guard = guard[:guard.index("signal(SIGPIPE")]
        self.assertIn('"gateway-kh is already running; exiting this copy.\\n"', guard)
        self.assertIn("exit(0)", guard)          # 0: launchd's KeepAlive must not read it as a crash
        for steal in ("activate(", "NSApp.activate", "makeKeyAndOrderFront"):
            self.assertNotIn(steal, guard)

    def test_a_gateway_from_another_build_is_never_adopted(self):
        """Measured on 1301: a 0.2.241 helper out of an updater trash folder served :8787 after
        0.2.246 was installed, and the new app adopted it. A foreign build must be replaced."""
        start = MAIN[MAIN.index("func start()"):]
        start = start[:start.index("/// Probe first, then stop whatever is serving the port")]
        self.assertIn("self.probedBuild", start)
        self.assertIn("served != GatewayController.shared.appBuild", start)
        self.assertIn("and did not stop", start)      # it says so instead of pretending
        probe = MAIN[MAIN.index("func probeExisting("):]
        probe = probe[:probe.index("private func spawn(")]
        self.assertIn('json["build"] as? String', probe)
        self.assertIn("self?.probedBuild = build", probe)

    def test_uninstall_removes_the_login_agent_and_leaves_one_record(self):
        """The login item is a plist; uninstall boots the job out, deletes the plist, and drops any
        SMAppService record an older build registered, so no row and no job outlives the app."""
        uninstall = MAIN[MAIN.index("static func uninstall()"):]
        uninstall = uninstall[:uninstall.index("private static func writePlist(")]
        self.assertIn('"bootout", "gui/', uninstall)
        self.assertIn("removeItem(at: plistURL)", uninstall)
        self.assertIn("clearLegacyRegistration()", uninstall)
        build = (ROOT.parent / "menubar" / "build.sh").read_text()
        self.assertNotIn("Contents/Library/LaunchAgents", build,
                         "the bundled plist was the SMAppService mechanism and is not read now")

    def test_the_bundled_python_cannot_write_bytecode_into_its_own_bundle(self):
        """The bundle is signed: a `.pyc` written inside it breaks the seal and macOS kills the
        app at its next launch (measured on 1301, 2026-11-17: 9 `__pycache__` dirs, codesign -v
        failed, the app killed at launch). `-B` covers the process the app spawns; `sitecustomize`
        covers every other caller of the bundled interpreter."""
        build = (ROOT.parent / "menubar" / "build.sh").read_text()
        self.assertIn("sitecustomize.py", build,
                      "the bundled interpreter needs sitecustomize, or a bare run writes .pyc")
        self.assertIn("sys.dont_write_bytecode = True", build,
                      "sitecustomize must switch bytecode writing off, not merely warn")

    def test_uninstall_keeps_the_users_data(self):
        """Nothing in the app may delete the Application Support folder or its config/logins."""
        for forbidden in ("Application Support/gateway-kh\", \"",
                          "removeItem(at: FileManager.default.homeDirectoryForCurrentUser",
                          "admin-sessions.json\"", "menubar.json\""):
            segment = MAIN[MAIN.index("static func uninstall()"):]
            self.assertNotIn(forbidden, segment[:segment.index("private static func writePlist(")])



class BundleRunnerTests(unittest.TestCase):
    """Owner, 2026-11-12: background runners live inside the app bundle — deleting the app deletes
    the runner, and nothing an app starts may outlive or point outside its bundle."""

    def test_every_runner_path_is_derived_from_the_bundle(self):
        paths = MAIN[MAIN.index("enum AppPaths {"):]
        paths = paths[:paths.index("// MARK: - Configuration")]
        self.assertIn("Bundle.main.bundleURL", paths)
        self.assertIn("Contents/Helpers/Gateway Studio KH Helper.app/Contents/MacOS/Gateway Studio KH Helper", paths)
        self.assertIn('appendingPathComponent("python/bin/python3")', paths)
        self.assertIn('appendingPathComponent("gateway", isDirectory: true)', paths)
        for absolute in ('"/Applications/', '"/Users/', '"/opt/', '"/usr/local/'):
            self.assertNotIn(absolute, paths)

    def test_the_bundle_runtime_wins_over_a_checkout(self):
        runtime = MAIN[MAIN.index("var runtime: Runtime {"):]
        runtime = runtime[:runtime.index("var isReady")]
        self.assertLess(runtime.index("AppPaths.bundledSource"), runtime.index("gatewayDir"))
        # The checkout is the documented developer fallback, and only when the bundle has no runtime.
        self.assertIn("The app has no bundled runtime and there is no .venv", runtime)

    def test_an_orphaned_helper_is_stopped_at_launch(self):
        """A helper with no owning app — a leftover from a replaced bundle — must not survive."""
        sweep = MAIN[MAIN.index("enum OrphanSweep {"):]
        sweep = sweep[:sweep.index("// MARK: - Launch at login")]
        self.assertIn('helperBundleID = "kh.gateway.helper"', sweep)
        self.assertIn("Bundle.main.bundleURL", sweep)
        self.assertIn("app.terminate()", sweep)
        # It runs before this app starts anything of its own.
        launch = MAIN[MAIN.index("OrphanSweep.stopForeignHelpers()"):]
        self.assertLess(launch.index("OrphanSweep.stopForeignHelpers()"),
                        launch.index("GatewayController.shared.start()"))

    def test_a_dead_checkout_pointer_is_not_written(self):
        """gatewayDir is optional now: a shipped install stores nothing rather than a dead path."""
        config = MAIN[MAIN.index("struct AppConfig: Codable"):]
        config = config[:config.index("final class ConfigStore")]
        self.assertIn("var gatewayDir: String?", config)
        self.assertIn("fileExists(atPath: repository) ? gatewayDir : nil", config)


if __name__ == "__main__":
    unittest.main()
