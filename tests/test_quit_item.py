"""The menu bar Quit item must take the gateway down with the app.

A quit that only closes the app leaves the helper serving the port with no icon, and
the next launch silently adopts it. These are source contracts: the label the owner
sees, and the stop-then-terminate order behind it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "menubar" / "main.swift").read_text()
SIGNATURE = "func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply"


def handler() -> str:
    text = MAIN[MAIN.index(SIGNATURE):]
    return text[: text.index("\n    }")]


class QuitItemTests(unittest.TestCase):
    def test_dropdown_has_a_quit_item_with_the_app_name(self):
        self.assertIn('menu.addItem(item("Quit Gateway Studio KH", #selector(quit), key: "q"))', MAIN)

    def test_the_app_menu_quit_reaches_the_same_path(self):
        # ⌘Q in the App menu terminates the app, which the delegate intercepts below.
        self.assertIn("action: #selector(NSApplication.terminate(_:))", MAIN)

    def test_quit_stops_the_gateway_including_an_adopted_one(self):
        body = handler()
        self.assertIn("return .terminateLater", body)
        self.assertIn("stopAnything", body)
        # The old shortcut let "Keep running" skip the stop and leave the helper serving.
        self.assertNotIn("keepGatewayRunning", body)

    def test_a_slow_stop_cannot_hang_the_quit(self):
        self.assertRegex(handler(), r"asyncAfter\(deadline: \.now\(\) \+ \d+")

    def test_an_unattended_quit_never_asks_a_modal(self):
        body = handler()
        # The Updater quits by AppleEvent with nobody there; a modal runModal() never returns and
        # termination was never answered (1301, 2026-11-15: every install hung).
        self.assertIn("let attended = NSApp.isActive && MainWindow.shared.isShown", body)
        self.assertIn("guard attended else", body)
        unattended = body[body.index("guard attended else"):]
        self.assertIn("stopAnything", unattended[: unattended.index("confirmIfBusy")])

    def test_a_deadline_is_armed_before_any_dialog(self):
        body = handler()
        deadline = body.index("DispatchQueue.main.asyncAfter(deadline: .now() + 40)")
        self.assertLess(deadline, body.index("confirmIfBusy"),
                        "the 40 s net must be armed before the modal, not after its answer")

    def test_a_cancelled_confirmation_keeps_the_app_open(self):
        self.assertIn("guard ok else { return finish(false) }", handler())


if __name__ == "__main__":
    unittest.main()
