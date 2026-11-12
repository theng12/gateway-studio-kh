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

    def test_uninstall_removes_the_launch_agent(self):
        uninstall = MAIN[MAIN.index("static func uninstall()"):]
        uninstall = uninstall[:uninstall.index("private static func reload()")]
        self.assertIn('"bootout"', uninstall)
        self.assertIn("removeItem(at: plistURL)", uninstall)

    def test_uninstall_keeps_the_users_data(self):
        """Nothing in the app may delete the Application Support folder or its config/logins."""
        for forbidden in ("Application Support/gateway-kh\", \"",
                          "removeItem(at: FileManager.default.homeDirectoryForCurrentUser",
                          "admin-sessions.json\"", "menubar.json\""):
            segment = MAIN[MAIN.index("static func uninstall()"):]
            self.assertNotIn(forbidden, segment[:segment.index("private static func reload()")])


if __name__ == "__main__":
    unittest.main()
