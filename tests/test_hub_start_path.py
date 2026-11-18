"""The menu's Start Hub Studio KH item opens the app that exists, and is never left dead.

The Hub was renamed from `Studio Hub` to `Hub Studio KH` (2026-11-18). The menu item searched only
the old name, so on every Mac with the renamed Hub it existed and did nothing — exactly the false
affordance the owner ruled out. Pinned here: the lookup order, the legacy fallback, and the third
case (neither app installed) resolving to nothing so the item can say so.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENUBAR = ROOT / "menubar"


def _resolver_source() -> str:
    """The pure resolver, lifted out of `main.swift` so it can be compiled on its own."""
    main = (MENUBAR / "main.swift").read_text()
    start = main.index("// BEGIN hub-app-start-path")
    end = main.index("// END hub-app-start-path")
    return main[start:end]


class HubStartPathNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory(prefix="hub-start-path-")
        temp = Path(cls._temp.name)
        binary = temp / "hub-start-path-check"
        resolver = temp / "hub-app-start-path.swift"
        resolver.write_text(_resolver_source())
        build = subprocess.run(
            ["swiftc", "-swift-version", "5", "-framework", "Foundation",
             str(resolver), "tests/test_hub_start_path.swift", "-o", str(binary)],
            cwd=ROOT, capture_output=True, text=True,
        )
        if build.returncode != 0:
            raise AssertionError(build.stderr or build.stdout)
        cls.binary = binary

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_new_legacy_and_missing(self) -> None:
        run = subprocess.run([str(self.binary)], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("PASS: hub app start path", run.stdout)

    def test_the_menu_item_says_why_when_no_app_is_installed(self) -> None:
        # The resolver returning nil is only half of it: the item must be disabled and say so in
        # plain words, never a visible item that does nothing.
        src = (MENUBAR / "main.swift").read_text()
        self.assertIn("studioHubStartItem?.isEnabled = installed != nil", src)
        self.assertIn('"Hub Studio KH is not installed on this Mac"', src)
        self.assertIn('"Start Hub Studio KH"', src)


if __name__ == "__main__":
    unittest.main()
