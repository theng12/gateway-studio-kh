"""The files that ship must carry no owner data — in any capitalisation.

0.2.282 passed a case-sensitive scan and still shipped `Terranash-1600` in the bundled
changelog; the check, not the file, was wrong. The rules now live in
`menubar/tools/no_private_strings.py`, which `menubar/build.sh` also runs against the files it has
just copied into the app — so the release test and the build cannot drift, and a later changelog
entry cannot quietly put a name back after a release scrubbed it.

Generic vocabulary stays legal: `100.64.0.0/10` (the range), `100.64.0.9` (the UI placeholder), the
word "tailnet", and `/Users/…` with no user name after it.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "no_private_strings", ROOT / "menubar" / "tools" / "no_private_strings.py"
)
assert _spec and _spec.loader
strings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(strings)


def shipped_files() -> list[Path]:
    """What build.sh puts inside the app: the changelog, the gateway source, the Swift sources.

    `menubar/tools/` is build tooling, not shipped — and its rules file has to spell out the names
    it forbids, so scanning it would flag the rule itself.
    """
    files = [ROOT / "CHANGELOG.md", ROOT / "VERSION"]
    for pattern in ("gateway/**/*.py", "gateway/**/*.html", "menubar/*.swift"):
        files.extend(sorted(ROOT.glob(pattern)))
    return [path for path in files if path.is_file()]


class TheShippedFilesCarryNoOwnerDataTest(unittest.TestCase):
    def test_no_machine_name_hostname_address_or_build_path(self):
        failures = strings.scan_paths(shipped_files())
        self.assertEqual(failures, [], "\n".join(failures))

    def test_every_capitalisation_and_separator_hits(self):
        for variant in (
            "Terranash-1600",
            "TERRANASH-1600",
            "terranash_1600",
            "Terranash 1600",
            "MacMini-Angkor",
            "STORY-MINI",
            "a-mac.{tailnet}.ts.net",
            "100.120.246.9",
            "/Users/someone/.env",
        ):
            text = variant.replace("{tailnet}", "example")
            with self.subTest(variant=variant):
                self.assertTrue(strings.scan_text(text), f"{variant!r} was not caught")

    def test_the_generic_placeholders_are_still_allowed(self):
        # Pinned so a later sweep does not "clean" the range or the placeholder out of the UI.
        for allowed in (
            "100.64.0.0/10",
            "100.64.0.9",
            "/Users/…/.env",
            "the tailnet address",
            "a Mac's tailnet hostname",
        ):
            with self.subTest(allowed=allowed):
                self.assertEqual(strings.scan_text(allowed), [])

    def test_the_scan_covers_the_changelog(self):
        # The 0.2.282 miss was in the changelog specifically; keep it in the set explicitly.
        self.assertIn(ROOT / "CHANGELOG.md", shipped_files())
        # And the build runs the same rules on the copies it puts in the app.
        build = (ROOT / "menubar" / "build.sh").read_text(encoding="utf-8")
        self.assertIn("no_private_strings.py", build)
        self.assertIn("Contents/Resources/CHANGELOG.md", build)


if __name__ == "__main__":
    unittest.main()
