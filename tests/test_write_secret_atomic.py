"""`write_secret` replaces the file whole or not at all (a kill mid-write used to leave it empty)."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.TemporaryDirectory(prefix="gateway-write-secret-")
os.environ.setdefault("GATEWAY_APP_SUPPORT", _TMP.name)
os.environ.setdefault("GATEWAY_NO_MIGRATE", "1")

from gateway import config  # noqa: E402


class WriteSecretAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_a_failed_write_leaves_the_old_file_intact(self) -> None:
        target = self.dir / "models_cache.json"
        config.write_secret(target, "old")
        with patch.object(config.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.write_secret(target, "new " * 1000)
        self.assertEqual(target.read_text(), "old")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["models_cache.json"], "no temp litter")

    def test_owner_only_and_replaced(self) -> None:
        target = self.dir / "credentials.json"
        config.write_secret(target, "one")
        config.write_secret(target, "two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_a_symlinked_file_is_written_through(self) -> None:
        real = self.dir / "real.json"
        real.write_text("x")
        link = self.dir / "link.json"
        link.symlink_to(real)
        config.write_secret(link, "y")
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), "y")


if __name__ == "__main__":
    unittest.main()
