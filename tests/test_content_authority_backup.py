"""The private shelf identity backup must be complete, private and non-destructive.

The signing identity is never regenerated, so a partial or unsafe backup is
worse than none: it would look like a recovery point and not be one. These
checks use disposable files, never a real authority.
"""
from __future__ import annotations

import hashlib
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

from tests._isolation import sandbox_env  # noqa: E402

# scripts.backup_content_authority imports gateway.config, freezing DATA_DIR;
# isolate before that import binds the owner's real data folder.
_SANDBOX = sandbox_env()

from scripts.backup_content_authority import BackupError, backup  # noqa: E402


NAMES = ("authority.json", "signing-key.pem", "initialized")


def make_authority(root: Path, *, mode: int = 0o600, release: str | None = None,
                   write_archive: bool = True) -> Path:
    source = root / "data" / "content-delivery"
    source.mkdir(parents=True, exist_ok=True)
    os.chmod(source, 0o700)
    releases = []
    if release:
        releases = [{"release_id": "gateway.private.release.fixture", "pack_id": "com.storystudio.pack.topic.fixture",
                     "version": "1.0.0", "archive_sha256": release, "bytes": 4}]
        if write_archive:
            archive = root / "data" / "content-library" / release / "archive.sspack"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"PK\x03\x04")
    (source / "authority.json").write_text(
        json.dumps({"schema_version": 1, "authority": {}, "releases": releases}))
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

    def test_release_archives_are_included_and_verified(self) -> None:
        # The shelf cannot serve a release without its archive, so an
        # identity-only backup is not a recovery point.
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            value = hashlib.sha256(b"PK\x03\x04").hexdigest()
            source = make_authority(root, release=value)
            destination = root / "backup"
            copied = backup(source, destination)
            self.assertIn(f"archives/{value}.sspack", copied)
            archived = destination / "archives" / f"{value}.sspack"
            self.assertEqual(archived.read_bytes(), b"PK\x03\x04")
            self.assertEqual(stat.S_IMODE(archived.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((destination / "archives").stat().st_mode), 0o700)

    def test_a_release_whose_archive_is_missing_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            value = hashlib.sha256(b"PK\x03\x04").hexdigest()
            source = make_authority(root, release=value, write_archive=False)
            with self.assertRaises(BackupError):
                backup(source, root / "backup")
            self.assertFalse((root / "backup").exists())

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

    def test_backup_passes_with_retired_and_removed_packs_present(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-authority-backup-") as temporary:
            root = Path(temporary)
            source = root / "data" / "content-delivery"
            source.mkdir(parents=True, exist_ok=True)
            os.chmod(source, 0o700)

            active_digest = hashlib.sha256(b"active-content").hexdigest()
            retired_digest = hashlib.sha256(b"retired-content").hexdigest()
            removed_digest = hashlib.sha256(b"removed-content").hexdigest()

            # Active archive on disk
            active_file = root / "data" / "content-library" / active_digest / "archive.sspack"
            active_file.parent.mkdir(parents=True, exist_ok=True)
            active_file.write_bytes(b"active-content")

            # Retired archive on disk (retired packs keep their files on disk)
            retired_file = root / "data" / "content-library" / retired_digest / "archive.sspack"
            retired_file.parent.mkdir(parents=True, exist_ok=True)
            retired_file.write_bytes(b"retired-content")

            # Removed archive is NOT in content-library; it is recorded in trash
            trash_archive = root / "data" / "_trash" / "content-library" / removed_digest / "archive.sspack"
            trash_archive.parent.mkdir(parents=True, exist_ok=True)
            trash_archive.write_bytes(b"removed-content")

            state = {
                "schema_version": 1,
                "authority": {},
                "releases": [
                    {
                        "release_id": "gateway.private.release.active",
                        "pack_id": "com.storystudio.pack.topic.active",
                        "version": "1.0.0",
                        "archive_sha256": active_digest,
                        "bytes": len(b"active-content"),
                        "retired": False,
                    },
                    {
                        "release_id": "gateway.private.release.retired",
                        "pack_id": "com.storystudio.pack.topic.retired",
                        "version": "1.0.0",
                        "archive_sha256": retired_digest,
                        "bytes": len(b"retired-content"),
                        "retired": True,
                        "retired_at": "2026-10-27T00:00:00Z",
                    },
                ],
                "removed": [
                    {
                        "what": "com.storystudio.pack.topic.removed@1.0.0",
                        "when": "2026-09-22T00:00:00Z",
                        "from_where": f"content-library/{removed_digest}",
                        "sha256": removed_digest,
                        "archive_sha256": removed_digest,
                        "pack_id": "com.storystudio.pack.topic.removed",
                        "version": "1.0.0",
                        "trash_path": str(trash_archive),
                    }
                ]
            }
            (source / "authority.json").write_text(json.dumps(state))
            (source / "signing-key.pem").write_text("{{PRIVATE_KEY_fixture}}\n")
            (source / "initialized").write_text("gateway-private-content-authority-v1\n")
            for name in NAMES:
                os.chmod(source / name, 0o600)

            destination = root / "backup"
            copied = backup(source, destination, root / "data")

            # Both active and retired archives are copied and verified
            self.assertIn(f"archives/{active_digest}.sspack", copied)
            self.assertIn(f"archives/{retired_digest}.sspack", copied)
            # Removed archive was not in content-library and did NOT fail the backup
            self.assertNotIn(f"archives/{removed_digest}.sspack", copied)
            self.assertEqual((destination / "archives" / f"{active_digest}.sspack").read_bytes(), b"active-content")
            self.assertEqual((destination / "archives" / f"{retired_digest}.sspack").read_bytes(), b"retired-content")


if __name__ == "__main__":
    unittest.main()
