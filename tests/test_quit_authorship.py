"""Who asked the app to stop — authorship, not attendance.

The app decided whether to challenge a stop with a dialog by asking `NSApp.isActive &&
MainWindow.shared.isShown`: a guess about the audience. The Updater quits by sending a Quit
AppleEvent, so whenever the owner happened to have the window in front the Updater's own quit was
met by a modal nobody answers, in front of an install, until the 40 s deadline replied.

`QuitSource` already parsed the AppleEvent sender (`main.swift`, and it is written to the log on
every quit). `QuitAuthorship` is the decision built on it, with the Gateway's own `dispatch_paused`
as corroboration: only `POST /api/maintenance/drain` — guarded by the Launcher's hold-token —
pauses dispatch, so a stop that follows one is the Updater asking.

What this pins:
  1. the decision itself, compiled and run natively (`tests/test_quit_authorship.swift`);
  2. that `confirmIfBusy` consults it rather than the attendance heuristic;
  3. that `applicationShouldTerminate` no longer skips the confirm on `attended`.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


def _block() -> str:
    """The pure authorship decision, lifted out of `main.swift` so it can be compiled on its own."""
    main = (MENUBAR / "main.swift").read_text()
    start = main.index("// BEGIN quit-authorship")
    end = main.index("// END quit-authorship")
    return main[start:end]


def _function_body(name: str) -> str:
    """The source of one `func name(` in `main.swift`, to its closing brace at the same indent."""
    main = (MENUBAR / "main.swift").read_text()
    start = main.index(f"func {name}(")
    indent = len(main[:start].rsplit("\n", 1)[-1]) - len(main[:start].rsplit("\n", 1)[-1].lstrip())
    close = "\n" + " " * indent + "}\n"
    end = main.index(close, start)
    return main[start:end + len(close)]


class QuitAuthorshipNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="quit-authorship-")
        temp = Path(cls._temp.name)
        binary = temp / "quit-authorship-check"
        block = temp / "quit-authorship.swift"
        block.write_text(_block())
        build = subprocess.run(
            ["swiftc", "-swift-version", "5", "-framework", "Foundation",
             str(block), "tests/test_quit_authorship.swift", "-o", str(binary)],
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
        self.assertIn("PASS: quit authorship", self._run())

    def test_an_updater_stop_is_never_challenged(self) -> None:
        # The defect: this was decided by whether a person was watching.
        out = self._run()
        self.assertNotIn("FAIL: the Launcher's Quit is not challenged", out)
        self.assertNotIn("FAIL: the Launcher's helper's Quit is not challenged", out)

    def test_a_persons_quit_is_still_challenged(self) -> None:
        out = self._run()
        self.assertNotIn("FAIL: a person's Quit may still be challenged", out)


class TheDecisionIsActuallyUsed(unittest.TestCase):
    """Source-level, so a later edit cannot quietly put the attendance heuristic back."""

    def test_confirm_if_busy_asks_who_is_stopping_it(self) -> None:
        body = _function_body("confirmIfBusy")
        self.assertIn("QuitAuthorship.whyNotAsked", body)
        self.assertIn("dispatch_paused", body, "the hold is the corroborating signal")
        self.assertIn("noteInLog", body, "a stop that does not ask must say why")

    def test_the_authorship_check_runs_while_the_gateway_is_busy(self) -> None:
        """The whole defect: work in flight is what puts a dialog in front of an Updater's stop."""
        body = _function_body("confirmIfBusy")
        busy = body.index("guard running + waiting > 0 else { return proceed(true) }")
        asked = body.index("QuitAuthorship.whyNotAsked")
        self.assertLess(busy, asked,
                        "the authorship check must be the thing that suppresses the dialog when busy")

    def test_the_quit_handler_does_not_gate_on_attendance(self) -> None:
        body = _function_body("applicationShouldTerminate")
        self.assertNotIn("guard attended else", body,
                         "attendance decided the dialog; authorship must decide it now")
        self.assertIn("senderBundleID: source.bundleID", body)
        self.assertIn("corroborateWithHold: true", body)

    def test_the_menu_still_asks(self) -> None:
        # Stop and Restart from the menu are a person's, so they pass no sender and no hold.
        main = (MENUBAR / "main.swift").read_text()
        for verb in ("Stop", "Restart"):
            self.assertIn(f'confirmIfBusy("{verb}")', main,
                          f"the menu's {verb} must keep asking")

    def test_the_status_read_carries_the_hold(self) -> None:
        admin = (ROOT / "gateway" / "admin.py").read_text()
        self.assertIn('"dispatch_paused": worker.dispatch_paused()', admin)


if __name__ == "__main__":
    unittest.main()
