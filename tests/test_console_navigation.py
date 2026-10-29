"""What the console's web view may load: only its own origin, because the key is inside it.

**The Gateway key is injected into the console's web view**, so the main frame is a security
boundary: a link to another origin followed there would hand that page the key. The rule lives in
`menubar/ConsoleNavigation.swift`, and `MainWindow.swift`'s `decidePolicyFor` uses it.

This suite pins three things:

* the rule itself, driven directly (`tests/test_console_navigation.swift`) — the console and its
  own `about:`/`data:` documents load; another host, another port and a lookalike host do not;
* that the web view actually asks the rule, so the rule cannot be written and then not used;
* the native entrance for the one action a web page cannot take — the permanent Trash Folder
  delete, which needs the app's owner-console secret (`View → Fleet Trash…`).

Run with::

    .venv/bin/python -m unittest tests.test_console_navigation
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


class ConsoleNavigationNative(unittest.TestCase):
    def test_the_rule(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-console-nav-") as temp:
            binary = Path(temp) / "console-navigation-check"
            build = subprocess.run(
                ["swiftc", "-swift-version", "5",
                 "menubar/ConsoleNavigation.swift", "tests/test_console_navigation.swift",
                 "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS: console navigation", run.stdout)


class ConsoleNavigationWired(unittest.TestCase):
    def test_the_web_view_asks_the_rule(self) -> None:
        window = (MENUBAR / "MainWindow.swift").read_text()
        self.assertIn("decidePolicyFor navigationAction: WKNavigationAction", window)
        self.assertIn("ConsoleNavigation.allowsMainFrame(url, console:",
                      window, "the policy is written but the web view never asks it")
        self.assertIn("ConsoleNavigation.opensInBrowser(url)", window)

    def test_the_permanent_trash_delete_has_a_native_entrance(self) -> None:
        # The web Fleet page offers the recoverable move and says the permanent delete is in the
        # app; that has to be true.
        app = (MENUBAR / "main.swift").read_text()
        self.assertIn('withTitle: "Fleet Trash…"', app)
        self.assertIn("#selector(AppDelegate.openFleetTrash)", app)
        self.assertIn("func openFleetTrash()", app)
        self.assertIn("FleetTrash.show(machines[index], done:", app)
        # ...and it reads a real fleet rather than the retired native page's last snapshot.
        self.assertIn('GatewayAPI.get("/admin/api/fleet"', app)
