"""The private shelf identity backup must be complete, private and non-destructive.

The signing identity is never regenerated, so a partial or unsafe backup is
worse than none: it would look like a recovery point and not be one. These
checks use disposable files, never a real authority.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.backup_content_authority import BackupError, backup  # noqa: E402


NAMES = ("authority.json", "signing-key.pem", "initialized")


def make_authority(root: Path, *, mode: int = 0o600) -> Path:
    source = root / "data" / "content-delivery"
    source.mkdir(parents=True, exist_ok=True)
    os.chmod(source, 0o700)
    (source / "authority.json").write_text(
        json.dumps({"schema_version": 1, "authority": {}, "releases": []}))
    (source / "signing-key.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nfixture-only\n-----END PRIVATE KEY-----\n")
    (source / "initialized").write_text("gateway-private-content-authority-v1\n")
    for name in NAMES:
        os.chmod(source / name, mode)
    return source


class AuthorityBackupTest(unittest.TestCase):
    def test_backup_copies_every_file_private_and_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            source = make_authority(root)
            destination = root / "backup"
            copied = backup(source, destination)
            self.assertEqual(sorted(copied), sorted(NAMES))
            for name in NAMES:
                self.assertEqual((destination / name).read_bytes(), (source / name).read_bytes())
                self.assertEqual(stat.S_IMODE((destination / name).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)

    def test_an_existing_backup_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            source = make_authority(root)
            destination = root / "backup"
            backup(source, destination)
            (destination / "signing-key.pem").write_text("do not lose me")
            with self.assertRaises(BackupError):
                backup(source, destination)
            self.assertEqual((destination / "signing-key.pem").read_text(), "do not lose me")

    def test_missing_and_world_readable_authorities_are_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            with self.assertRaises(BackupError):
                backup(root / "absent", root / "backup")
            source = make_authority(root, mode=0o644)
            with self.assertRaises(BackupError):
                backup(source, root / "backup2")
            self.assertFalse((root / "backup2").exists())

    def test_cli_writes_a_stamped_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            make_authority(root)
            out = root / "out"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/backup_content_authority.py"),
                 str(out), "--data-dir", str(root / "data")],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            stamped = list(out.glob("content-authority-*"))
            self.assertEqual(len(stamped), 1, completed.stdout)
            for name in NAMES:
                self.assertTrue((stamped[0] / name).is_file())
            self.assertNotIn("fixture-only", completed.stdout)


if __name__ == "__main__":
    unittest.main()
