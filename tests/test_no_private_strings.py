"""The files that ship must carry no owner data — in any capitalisation.

The 0.2.282 DMG passed a case-sensitive scan and still shipped `Terranash-1600` in the bundled
changelog; the check, not the file, was wrong. This test is case-insensitive and reads exactly the
files `menubar/build.sh` copies into the app, so a name, a tailnet address or a build path fails a
gate instead of reaching the feed.

The generic vocabulary stays: `100.64.0.0/10` (the range), `100.64.0.9` (the UI placeholder), the
word "tailnet", and the `/Users/…` placeholder with no user name.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = [
    ("machine name", re.compile(r"terranash|macmini|angkor|story-mini", re.I)),
    ("tailnet hostname", re.compile(r"[a-z0-9-]+\.ts\.net", re.I)),
    ("build path", re.compile(r"/Users/[A-Za-z0-9_.-]")),
    (
        "tailnet address",
        re.compile(r"100\.(?:6[5-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+"),
    ),
]


def shipped_files() -> list[Path]:
    """What build.sh puts inside the app: the changelog, the gateway source, the Swift sources."""
    files = [ROOT / "CHANGELOG.md", ROOT / "VERSION"]
    for pattern in ("gateway/**/*.py", "gateway/**/*.html", "menubar/*.swift", "menubar/tools/*.py"):
        files.extend(sorted(ROOT.glob(pattern)))
    return [path for path in files if path.is_file()]


class TheShippedFilesCarryNoOwnerDataTest(unittest.TestCase):
    def test_no_machine_name_hostname_address_or_build_path(self):
        failures: list[str] = []
        for path in shipped_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for label, pattern in FORBIDDEN:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    failures.append(f"{path.relative_to(ROOT)}:{line}: {label}: {match.group(0)}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_the_generic_placeholders_are_still_allowed(self):
        # Pinned so a later sweep does not "clean" the range or the placeholder out of the UI.
        self.assertIn(
            "100.64.0.0/10", (ROOT / "gateway" / "fleet.py").read_text(encoding="utf-8")
        )
        self.assertIn(
            "100.64.0.9", (ROOT / "gateway" / "admin_ui.html").read_text(encoding="utf-8")
        )

    def test_the_scan_covers_the_changelog(self):
        # The 0.2.282 miss was in the changelog specifically; keep it in the set explicitly.
        self.assertIn(ROOT / "CHANGELOG.md", shipped_files())


if __name__ == "__main__":
    unittest.main()
