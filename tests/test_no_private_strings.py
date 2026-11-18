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
        # Pinned so a later sweep does not "clean" the range or the placeholder out of the UI
        # or out of the connector README's copy-paste block.
        for allowed in (
            "100.64.0.0/10",
            "100.64.0.9",
            "/Users/…/.env",
            "/Users/YOUR-NAME/Developer/gateway-kh/repository/connectors/mcp/server.py",
            "the tailnet address",
            "a Mac's tailnet hostname",
        ):
            with self.subTest(allowed=allowed):
                self.assertEqual(strings.scan_text(allowed), [])

    def test_the_scan_covers_the_changelog(self):
        # The 0.2.282 miss was in the changelog specifically; keep it in the set explicitly.
        self.assertIn(ROOT / "CHANGELOG.md", shipped_files())

    def test_the_build_scans_everything_it_copies(self):
        # Not a list of paths: a copy added later must be covered without anyone remembering it.
        build = (ROOT / "menubar" / "build.sh").read_text(encoding="utf-8")
        self.assertIn("no_private_strings.py", build)
        self.assertIn('"$APP/Contents/Resources"', build)
        # Last, so the tree scanned is the tree that is signed.
        self.assertLess(build.index("checking the bundle for owner data"), build.index("==> signing"))

    def test_a_binary_is_skipped_and_a_new_copied_file_is_not(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "binary.dylib").write_bytes(b"\xcf\xfa\xed\xfe\0\0" + b"terranash" * 4)
            self.assertEqual(strings.scan_tree(root), [])
            (root / "newdir").mkdir()
            (root / "newdir" / "NOTES.md").write_text("ran on Terranash 1600\n", encoding="utf-8")
            hits = strings.scan_tree(root)
            self.assertEqual(len(hits), 1)
            self.assertIn("NOTES.md", hits[0])


if __name__ == "__main__":
    unittest.main()
