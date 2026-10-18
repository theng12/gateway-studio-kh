#!/usr/bin/env python3
"""Owner backups (gateway/backup.py): what goes in, where it goes, pruning, and a real restore.

Everything runs in temp dirs: the sources, iCloud, the local folder, fake KH-STUDIO drives and
the trash are all repointed, so nothing here reads or writes the owner's files.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-backup-")

from gateway import admin, backup, config  # noqa: E402


_REAL_NOTIFY = backup.notify


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="kh-backup-test-")
        self.addCleanup(tmp.cleanup)
        root = self.root = Path(tmp.name)
        self.live = root / "live"
        data = self.live / "data"
        self.files = {
            "gateway/.env": self.live / ".env",
            "gateway/data/release-publisher.pem": data / "release-publisher.pem",
            "gateway/data/content-delivery/signing-key.pem": data / "content-delivery/signing-key.pem",
            "gateway/data/content-delivery/authority.json": data / "content-delivery/authority.json",
            "gateway/data/content-library/" + "a" * 64 + "/archive.sspack": data / "content-library" / ("a" * 64) / "archive.sspack",
            "gateway/data/content-library/" + "a" * 64 + "/metadata.json": data / "content-library" / ("a" * 64) / "metadata.json",
            "gateway/data/credentials.json": data / "credentials.json",
            "gateway/data/fleet.json": data / "fleet.json",
            "gateway/data/voice-library/voice-a/reference.mp3": data / "voice-library/voice-a/reference.mp3",
            "gateway/data/voice-library/voice-a/transcript.txt": data / "voice-library/voice-a/transcript.txt",
            "hub/voice/voices/abc/reference.wav": root / "hub/voice/voices/abc/reference.wav",
            "hub/voice/voices/abc/transcript.txt": root / "hub/voice/voices/abc/transcript.txt",
        }
        for name, path in self.files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"original {name}\n".encode() + os.urandom(64))
        (data / "voice-library/.DS_Store").write_bytes(b"junk")
        staging = data / "content-library/.import-half/archive.sspack"  # an import in progress
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b"half written")
        # voices.json is absent on purpose: it must be reported missing, not fail the backup.
        roles = [
            ("gateway/.env", self.live / ".env"),
            ("gateway/data/release-publisher.pem", data / "release-publisher.pem"),
            ("gateway/data/content-delivery", data / "content-delivery"),
            ("gateway/data/content-library", data / "content-library"),
            ("gateway/data/credentials.json", data / "credentials.json"),
            ("gateway/data/fleet.json", data / "fleet.json"),
            ("gateway/data/voices.json", data / "voices.json"),
            ("gateway/data/voice-library", data / "voice-library"),
            ("hub/voice/voices", root / "hub/voice/voices"),
        ]
        cloud_docs = root / "CloudDocs"
        cloud_docs.mkdir()
        volumes = root / "Volumes"
        (volumes / "KH-STUDIO-NOVA/_trash").mkdir(parents=True)
        (volumes / "KH-STUDIO-ATLAS").mkdir(parents=True)
        (volumes / "Other-Drive").mkdir(parents=True)
        (root / "trash").mkdir()
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.notes: list[tuple[str, str]] = []
        for p in (
            patch.object(backup, "sources", lambda: roles),
            patch.object(backup, "ICLOUD_DIR", cloud_docs / "KH Backups"),
            patch.object(backup, "LOCAL_DIR", root / "_backups"),
            patch.object(backup, "VOLUMES", volumes),
            patch.object(backup, "TRASH_DIR", root / "trash"),
            patch.object(backup, "notify", lambda event, text: self.notes.append((event, text))),
            patch.object(config, "DATA_DIR", self.state_dir),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _archives(self) -> list[Path]:
        return sorted(self.root.rglob("kh-backup-*.tar.gz"))

    # ------------------------------------------------------------------ make

    def test_backup_lands_in_every_place_owner_only_with_manifest(self) -> None:
        result = backup.run()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files"], len(self.files))
        self.assertEqual(result["missing"], ["gateway/data/voices.json"])
        places = {p.parent for p in self._archives()}
        self.assertEqual(places, {
            self.root / "CloudDocs/KH Backups", self.root / "_backups",
            self.root / "Volumes/KH-STUDIO-NOVA/KH Backups", self.root / "Volumes/KH-STUDIO-ATLAS/KH Backups",
        })
        self.assertFalse((self.root / "Volumes/Other-Drive/KH Backups").exists())
        for archive in self._archives():
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600, archive)
        with tarfile.open(self._archives()[0]) as tar:
            names = tar.getnames()
            manifest = json.loads(tar.extractfile("manifest.json").read())  # type: ignore[union-attr]
        self.assertNotIn("gateway/data/voice-library/.DS_Store", names)
        self.assertFalse([n for n in names if ".import-" in n])
        self.assertIn("gateway/data/content-library/" + "a" * 64 + "/archive.sspack", names)
        self.assertEqual({e["path"] for e in manifest["files"]}, set(self.files))
        self.assertIn("gateway_version", manifest)
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(len(result["destinations"]), 4)
        status = backup.status()
        self.assertEqual({row["id"] for row in status["destinations"] if row.get("last_success_at")},
                         {"icloud", "local", "ssd:KH-STUDIO-NOVA", "ssd:KH-STUDIO-ATLAS"})

    def test_disabled_and_absent_places_are_skipped(self) -> None:
        backup.save_settings({"destinations": {"ssd": False}})
        with patch.object(backup, "ICLOUD_DIR", self.root / "no-icloud/KH Backups"):
            result = backup.run()
        self.assertEqual(set(result["destinations"]), {"local"})
        self.assertEqual([p.parent for p in self._archives()], [self.root / "_backups"])

    def test_a_second_run_while_one_is_going_is_refused(self) -> None:
        with backup._lock:
            with self.assertRaises(backup.BackupError):
                backup.run()

    # ------------------------------------------------------------------ restore

    def test_restore_puts_files_back_and_moves_replaced_ones_to_trash(self) -> None:
        backup.run()
        archive = next(p for p in self._archives() if p.parent.name == "_backups")
        originals = {name: path.read_bytes() for name, path in self.files.items()}
        key = self.files["gateway/data/release-publisher.pem"]
        key.write_bytes(b"a key someone regenerated")
        self.files["hub/voice/voices/abc/reference.wav"].unlink()
        pack = self.files["gateway/data/content-library/" + "a" * 64 + "/archive.sspack"]
        pack.unlink()  # a released Content Pack lost from the shelf
        newer = self.files["gateway/data/voice-library/voice-a/reference.mp3"].parent.parent / "voice-new/x.mp3"
        newer.parent.mkdir()
        newer.write_bytes(b"a voice added after the backup")

        result = backup.restore(archive)

        for name, path in self.files.items():
            self.assertEqual(path.read_bytes(), originals[name], name)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(newer.read_bytes(), b"a voice added after the backup")  # never touched
        self.assertEqual((result["written"], result["moved_to_trash"]), (3, 1))
        safety = Path(result["safety_copy"])
        self.assertEqual(safety.parent, self.root / "trash")
        self.assertEqual((safety / "gateway/data/release-publisher.pem").read_bytes(), b"a key someone regenerated")
        # A second restore of the same backup changes nothing.
        again = backup.restore(archive)
        self.assertEqual((again["written"], again["moved_to_trash"]), (0, 0))

    def test_the_telegram_hook_is_gateway_notifier_and_optional(self) -> None:
        import sys
        from types import ModuleType
        sent: list[tuple[str, str]] = []
        fake = ModuleType("gateway.notifier")
        fake.notify = lambda event, text: sent.append((event, text))  # type: ignore[attr-defined]
        gateway_pkg = __import__("gateway")
        patch.object(backup, "notify", _REAL_NOTIFY).start()  # setUp swapped in a recorder
        self.addCleanup(patch.stopall)
        with patch.dict(sys.modules, {"gateway.notifier": fake}), patch.object(gateway_pkg, "notifier", fake, create=True):
            backup.notify("backup", "hello")
        self.assertEqual(sent, [("backup", "hello")])
        broken = ModuleType("gateway.notifier")
        broken.notify = lambda event, text: 1 / 0  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"gateway.notifier": broken}), patch.object(gateway_pkg, "notifier", broken, create=True):
            backup.notify("backup", "never raises")
        with patch.dict(sys.modules, {"gateway.notifier": None}), patch.object(gateway_pkg, "notifier", None, create=True):
            backup.notify("backup", "silently skipped")

    def test_restore_by_hand_with_tar_and_shasum(self) -> None:
        """The documented no-app path: Finder/tar extracts it and `shasum -c` checks it."""
        backup.run()
        archive = self._archives()[0]
        out = self.root / "by-hand"
        out.mkdir()
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(out)], check=True)
        check = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=out,
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertEqual((out / "gateway/.env").read_bytes(), self.files["gateway/.env"].read_bytes())

    def test_a_damaged_backup_is_refused_and_nothing_is_written(self) -> None:
        backup.run()
        good = self._archives()[0]
        bad = self.root / "bad.tar.gz"
        with tarfile.open(good) as src, tarfile.open(bad, "w:gz") as dst:
            for member in src:
                data = src.extractfile(member).read()  # type: ignore[union-attr]
                if member.name == "gateway/data/credentials.json":
                    data = b"tampered"
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        before = self.files["gateway/data/credentials.json"].read_bytes()
        self.files["gateway/data/credentials.json"].write_bytes(b"current")
        with self.assertRaises(backup.BackupError):
            backup.restore(bad)
        self.assertEqual(self.files["gateway/data/credentials.json"].read_bytes(), b"current")
        self.assertNotEqual(before, b"current")
        self.assertIsInstance(admin.inspect_backup({"path": str(bad)}), admin.JSONResponse)

    def test_paths_outside_the_known_places_are_refused(self) -> None:
        for name in ("../etc/passwd", "/etc/passwd", "gateway/data/../../x", "gateway/data/gateway.sqlite3"):
            with self.assertRaises(backup.BackupError, msg=name):
                backup.target_for(name)
        self.assertEqual(backup.target_for("hub/voice/voices/abc/transcript.txt"),
                         self.files["hub/voice/voices/abc/transcript.txt"])

    # ------------------------------------------------------------------ prune / schedule

    def test_prune_keeps_the_newest_and_touches_only_our_archives(self) -> None:
        folder = self.root / "_backups"
        folder.mkdir()
        names = [f"kh-backup-2026100{d}-030000.tar.gz" for d in range(1, 6)]
        for name in names:
            (folder / name).write_bytes(b"x")
        (folder / "notes.txt").write_text("the owner's")
        (folder / "kh-backup-latest.tar.gz").write_bytes(b"not a name we write")
        trash = self.root / "trash/kh-backups/local"
        self.assertEqual(backup.prune(folder, 3, trash), 2)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         sorted(names[2:] + ["notes.txt", "kh-backup-latest.tar.gz"]))
        self.assertEqual(sorted(p.name for p in trash.iterdir()), names[:2])
        # No trash on that volume: the old archives stay in place, never unlinked.
        self.assertEqual(backup.prune(folder, 1, None), 0)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         sorted(names[2:] + ["notes.txt", "kh-backup-latest.tar.gz"]))
        # A trash place that cannot be written to also keeps the source.
        blocked = self.root / "not-a-folder"
        blocked.write_text("x")
        self.assertEqual(backup.prune(folder, 1, blocked), 0)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         sorted(names[2:] + ["notes.txt", "kh-backup-latest.tar.gz"]))

    def test_nightly_is_off_by_default_and_runs_once_after_its_time(self) -> None:
        chosen = backup.settings()
        self.assertFalse(chosen["nightly"])
        now = datetime(2026, 10, 2, 3, 5)
        self.assertFalse(backup.due(now, chosen, {}))
        chosen["nightly"] = True
        self.assertTrue(backup.due(now, chosen, {}))
        self.assertFalse(backup.due(datetime(2026, 10, 2, 2, 59), chosen, {}))
        ran = {"last_run": {"started_at": datetime(2026, 10, 2, 3, 1).timestamp()}}
        self.assertFalse(backup.due(now, chosen, ran))
        self.assertTrue(backup.due(datetime(2026, 10, 3, 3, 0), chosen, ran))

    def test_settings_are_checked(self) -> None:
        refused = admin.set_backup({"time": "25:00"})
        self.assertEqual(refused.status_code, 422)
        saved = admin.set_backup({"nightly": True, "time": "4:30", "keep": 500})
        self.assertEqual((saved["settings"]["time"], saved["settings"]["keep"]), ("04:30", 100))
        self.assertEqual(os.stat(self.state_dir / "backup.json").st_mode & 0o777, 0o600)


class HubVoicesSkippedNote(unittest.TestCase):
    def test_the_backups_card_explains_skipped_hub_voices(self) -> None:
        # The role the manifest reports as `missing` when the Hub's voices are on another Mac, and the
        # card that must turn it into a plain sentence (not just a path in a "skipped" list).
        root = Path(__file__).resolve().parents[1]
        self.assertIn('("hub/voice/voices", HUB_DIR', (root / "gateway/backup.py").read_text().replace("\n", " "))
        panel = (root / "menubar/BackupPanel.swift").read_text()
        self.assertIn('hubVoices = "hub/voice/voices"', panel)
        self.assertIn("Studio Hub voices are not in this backup", panel)
        self.assertIn("Hub voices", (root / "docs/backup-restore.md").read_text())


if __name__ == "__main__":
    unittest.main()
