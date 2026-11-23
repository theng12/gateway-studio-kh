"""The hold token moves into the app's own file, once (0.2.306).

The settled rule: own file first; if it is absent and the legacy estate file exists, copy it once,
say so, and never read the legacy path again; if neither exists, refuse in words a person can read.
A loose or short legacy file is a failed check, not something to copy.

    python -B tests/test_maintenance_token.py -v
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import maintenance

TOKEN = "a" * 64
OTHER = "b" * 64


class TokenMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-token-")
        self.home = Path(self.tmp.name)
        self.home_patch = patch("pathlib.Path.home", return_value=self.home)
        self.home_patch.start()
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("KHU_HOLD_TOKEN_FILE", None)
        (self.home / "Library/KHUpdater/state").mkdir(parents=True)
        (self.home / "Library/Application Support/gateway-kh").mkdir(parents=True)

    def tearDown(self):
        self.env_patch.stop()
        self.home_patch.stop()
        self.tmp.cleanup()

    def write(self, path: Path, text: str, mode: int = 0o600) -> Path:
        path.write_text(text)
        path.chmod(mode)
        return path

    def own(self) -> Path:
        return self.home / "Library/Application Support/gateway-kh/hold-token"

    def legacy(self) -> Path:
        return self.home / "Library/KHUpdater/state/hold-token"

    def test_legacy_token_is_copied_once_and_then_read_from_own(self):
        self.write(self.legacy(), TOKEN + "\n")
        self.assertEqual(maintenance._read_token_file(), TOKEN)
        self.assertTrue(self.own().is_file())
        self.assertEqual(stat.S_IMODE(self.own().stat().st_mode), 0o600)
        # The copy is one-time: with the legacy file gone the own file still answers.
        self.legacy().unlink()
        self.assertEqual(maintenance._read_token_file(), TOKEN)

    def test_own_file_wins_over_the_legacy_one(self):
        self.write(self.own(), OTHER + "\n")
        self.write(self.legacy(), TOKEN + "\n")
        self.assertEqual(maintenance._read_token_file(), OTHER)
        self.assertIsNone(maintenance.migrate_token_file())

    def test_a_loose_legacy_file_is_not_copied(self):
        self.write(self.legacy(), TOKEN + "\n", mode=0o644)
        self.assertIsNone(maintenance._read_token_file())
        self.assertFalse(self.own().exists())
        self.assertIn("NOT migrated", maintenance.migrate_token_file() or "")

    def test_a_short_legacy_file_is_not_copied(self):
        self.write(self.legacy(), "a" * 32 + "\n")
        self.assertIsNone(maintenance._read_token_file())
        self.assertFalse(self.own().exists())

    def test_no_token_anywhere_says_which_path_is_missing(self):
        self.assertIsNone(maintenance._read_token_file())
        problem = maintenance.token_problem()
        self.assertTrue(problem.startswith("update blocked: this app's hold token is missing at "))
        self.assertIn(str(self.own()), problem)

    def test_a_loose_own_file_is_named_as_a_failed_check(self):
        self.write(self.own(), TOKEN + "\n", mode=0o644)
        self.assertIsNone(maintenance._read_token_file())
        self.assertIn("is not a 0600 file of 64 hex", maintenance.token_problem() or "")

    def test_the_app_support_override_moves_the_own_file(self):
        support = self.home / "elsewhere"
        support.mkdir()
        with patch.dict(os.environ, {"GATEWAY_APP_SUPPORT": str(support)}):
            self.assertEqual(maintenance.token_path(), support / "hold-token")

    def test_a_named_file_is_used_as_is_and_never_migrated(self):
        named = self.home / "named-token"
        self.write(named, OTHER + "\n")
        self.write(self.legacy(), TOKEN + "\n")
        with patch.dict(os.environ, {"KHU_HOLD_TOKEN_FILE": str(named)}):
            self.assertEqual(maintenance._read_token_file(), OTHER)
            self.assertIsNone(maintenance.migrate_token_file())
        self.assertFalse(self.own().exists())


if __name__ == "__main__":
    unittest.main()
