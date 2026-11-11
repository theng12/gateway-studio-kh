#!/usr/bin/env python3
"""The SSD installer index must describe the folder it sits in.

Owner rule (through Developer, 2026-11-11): the list is generated from the DMGs actually
present at refresh time, and a check fails when a listed file is missing or an unlisted
DMG is present. `scripts/verify_ssd_index.py` is that check and that writer.

Run with::

    .venv/bin/python -m unittest tests.test_ssd_index
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_ssd_index.py"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SsdIndex(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "installers"
        self.folder.mkdir()

    def dmg(self, name: str, data: bytes, mtime: float | None = None) -> Path:
        path = self.folder / name
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def index(self, *lines: str) -> None:
        (self.folder / "SHA256.txt").write_text("\n".join(lines) + "\n")

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              text=True, capture_output=True, check=False)

    def test_a_folder_that_matches_its_list_passes(self) -> None:
        self.dmg("Gateway Studio KH-0.2.241.dmg", b"prior")
        self.dmg("Gateway Studio KH-0.2.242.dmg", b"current")
        self.index(f"{sha(b'current')}  Gateway Studio KH-0.2.242.dmg",
                   f"{sha(b'prior')}  Gateway Studio KH-0.2.241.dmg")
        result = self.run_script(str(self.folder))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 DMGs, hashes match", result.stdout)

    def test_a_listed_file_that_is_missing_fails(self) -> None:
        self.dmg("Gateway Studio KH-0.2.242.dmg", b"current")
        self.index(f"{sha(b'current')}  Gateway Studio KH-0.2.242.dmg",
                   f"{sha(b'gone')}  Gateway Studio KH-0.2.240.dmg")
        result = self.run_script(str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("listed but missing: Gateway Studio KH-0.2.240.dmg", result.stdout)

    def test_a_dmg_that_is_not_listed_fails(self) -> None:
        self.dmg("Gateway Studio KH-0.2.241.dmg", b"prior")
        self.dmg("Gateway Studio KH-0.2.242.dmg", b"current")
        self.index(f"{sha(b'prior')}  Gateway Studio KH-0.2.241.dmg")
        result = self.run_script(str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("present but not listed: Gateway Studio KH-0.2.242.dmg", result.stdout)

    def test_a_hash_that_does_not_match_fails(self) -> None:
        self.dmg("Gateway Studio KH-0.2.242.dmg", b"current")
        self.index(f"{sha(b'something else')}  Gateway Studio KH-0.2.242.dmg")
        result = self.run_script(str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("hash does not match: Gateway Studio KH-0.2.242.dmg", result.stdout)

    def test_write_builds_the_list_from_the_folder_newest_first(self) -> None:
        self.dmg("Gateway Studio KH-0.2.241.dmg", b"prior", mtime=1_700_000_000)
        self.dmg("Gateway Studio KH-0.2.242.dmg", b"current", mtime=1_700_000_100)
        # A stale list naming a file that is gone: the writer must not carry it over.
        self.index(f"{sha(b'gone')}  Gateway Studio KH-0.2.240.dmg")
        written = self.run_script("--write", str(self.folder))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        lines = (self.folder / "SHA256.txt").read_text().splitlines()
        self.assertEqual(lines, [
            f"{sha(b'current')}  Gateway Studio KH-0.2.242.dmg",
            f"{sha(b'prior')}  Gateway Studio KH-0.2.241.dmg",
        ])
        checked = self.run_script(str(self.folder))
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_write_refuses_a_folder_with_no_installers(self) -> None:
        (self.folder / "WHATS-NEW.txt").write_text("nothing here\n")
        result = self.run_script("--write", str(self.folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to write an empty list", result.stdout)


if __name__ == "__main__":
    unittest.main()
