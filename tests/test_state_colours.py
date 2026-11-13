"""One state -> colour mapping: running amber, failed red, succeeded green, cancelled/paused grey.

The Swift mapping is `Health(state:)` in menubar/PageKit.swift; the web console's twin is `stateBadgeClass`.
Every state pill in the app is routed through it (guarded below), so a word never has two colours.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


class StateColoursNative(unittest.TestCase):
    def test_swift_mapping(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-state-colours-") as temp:
            binary = Path(temp) / "state-colours-check"
            build = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/PageKit.swift", "tests/test_state_colours.swift", "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            run = subprocess.run([str(binary)], cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS:", run.stdout)


class StateColoursRouted(unittest.TestCase):
    def test_pills_use_the_mapping(self) -> None:
        jobs = (MENUBAR / "JobsWindow.swift").read_text()
        self.assertIn("Health(state: displayState(job))", jobs)
        tabs = (MENUBAR / "FleetTabs.swift").read_text()
        self.assertIn('Health(state: "running")', tabs)
        self.assertIn("row.badgeHealth", tabs)
        self.assertIn("Health(state: state).text", (MENUBAR / "OverviewWindow.swift").read_text())
        models = (MENUBAR / "FleetModels.swift").read_text()
        # Nothing hand-picks green for "running" any more.
        self.assertNotIn('case "succeeded", "running": return .ok', jobs)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_console_mapping(self) -> None:
        html = (ROOT / "gateway" / "admin_ui.html").read_text()
        match = re.search(r"function stateBadgeClass\(state\) \{.*?\n\}\n", html, re.S)
        self.assertIsNotNone(match, "stateBadgeClass moved; update this test")
        cases = {"running": "warn", "queued": "warn", "accepted": "warn", "dispatching": "warn", "waiting": "warn",
                 "updating": "warn", "failed": "bad", "error": "bad", "succeeded": "ok", "done": "ok",
                 "updated": "ok", "latest": "ok", "cancelled": "", "paused": "", "mystery": ""}
        script = match.group(0) + f"\nconst c = {json.dumps(cases)};" + \
            "\nconst bad = Object.entries(c).filter(([k, v]) => stateBadgeClass(k) !== v);" + \
            "\nconsole.log(JSON.stringify(bad));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])
        self.assertIn("stateBadgeClass(j.state)", html)


if __name__ == "__main__":
    unittest.main()
