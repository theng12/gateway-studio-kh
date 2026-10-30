#!/usr/bin/env python3
"""Isolated tests for the secret-free nightly settings archive."""
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-nightly-settings-")
from gateway import admin, backup, config, fleet, retention


class NightlySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nightly-settings-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, name, value in (
            (config, "DATA_DIR", self.root),
            (config, "ENV_PATH", self.root / ".env"),
            (config, "CREDENTIALS_PATH", self.root / "credentials.json"),
            (fleet, "FLEET_PATH", self.root / "fleet.json"),
            (retention, "TRASH_DIR", self.root / "_trash"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        (self.root / "fleet.json").write_text(json.dumps({"controllers": [{"id": "site", "url": "http://site:8787"}]}))
        (self.root / "retention.json").write_text('{"days": 3}')
        for name in ("credentials.json", "telegram.json", "release-publisher.pem"):
            (self.root / name).write_text('KNOWN_SECRET_VALUE_987654321')
        (self.root / ".env").write_text('KNOWN_SECRET_VALUE_987654321')

    def test_default_off_and_explicit_switch(self):
        chosen = backup.settings()
        self.assertFalse(chosen["settings_nightly"])
        self.assertFalse(backup.settings_due(datetime.now(), chosen, {}))
        self.assertIsNone(admin.get_nightly_backup()["last_success"])
        self.assertIsNone(admin.get_nightly_backup()["last_failure"])
        self.assertIsNone(admin.get_nightly_backup()["latest"])
        self.assertTrue(admin.set_backup({"settings_nightly": True})["settings"]["settings_nightly"])
        self.assertEqual(admin.set_backup({"settings_nightly": "false"}).status_code, 422)

    def test_archive_is_secret_free_restorable_and_fleet_opt_in(self):
        backup.save_settings({"include_fleet": True})
        result = backup.run_nightly_settings()
        self.assertTrue(result["ok"], result)
        folder = self.root / "backups/nightly"
        archive = folder / result["name"]
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            self.assertEqual(set(names), {"gateway/data/fleet.json", "gateway/data/retention.json", "manifest.json", "SHA256SUMS"})
            for member in tar:
                self.assertNotIn(b"KNOWN_SECRET_VALUE_987654321", tar.extractfile(member).read())
        status = admin.get_nightly_backup()
        self.assertEqual(status["latest"]["name"], archive.name)
        self.assertEqual(status["latest"]["bytes"], archive.stat().st_size)
        self.assertEqual(status["latest"]["sha256"], backup._sha(archive))
        self.assertEqual(admin.download_nightly_backup().path, archive)
        self.assertTrue(backup.inspect(archive)["include_fleet"])
        (self.root / "retention.json").write_text('{"days": 9}')
        with patch.object(backup, "TRASH_DIR", self.root / "restore-trash"):
            backup.restore(archive)
        self.assertEqual(json.loads((self.root / "retention.json").read_text()), {"days": 3})

    def test_secret_in_allowed_file_fails_without_leaking(self):
        (self.root / "fleet.json").write_text('{"controllers": [{"token": "KNOWN_SECRET_VALUE_987654321"}]}')
        backup.save_settings({"include_fleet": True})
        result = backup.run_nightly_settings()
        self.assertFalse(result["ok"])
        status = admin.get_nightly_backup()
        self.assertIsNone(status["last_success"])
        self.assertIsNone(status["latest"])
        self.assertIn("credential-shaped", status["last_failure"]["reason"])
        self.assertNotIn("KNOWN_SECRET_VALUE_987654321", json.dumps(status))

    def test_retention_moves_old_archives_to_trash(self):
        folder = self.root / "backups/nightly"
        folder.mkdir(parents=True)
        for day in range(1, 16):
            (folder / f"kh-backup-202601{day:02d}-030000.tar.gz").write_bytes(b"old")
        result = backup.run_nightly_settings()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(backup._ours(folder)), 14)
        self.assertEqual(len(list((self.root / "_trash").rglob("*.tar.gz"))), 2)

    def test_due_once_per_day_and_fleet_is_omitted_by_default(self):
        now = datetime(2026, 10, 31, 4, 0)
        chosen = backup.save_settings({"settings_nightly": True})
        self.assertTrue(backup.settings_due(now, chosen, {}))
        self.assertFalse(backup.settings_due(now, chosen, {"last_failure": {"at": now.timestamp()}}))
        result = backup.run_nightly_settings()
        with tarfile.open(self.root / "backups/nightly" / result["name"]) as tar:
            self.assertNotIn("gateway/data/fleet.json", tar.getnames())

    def test_credential_in_url_is_refused(self):
        (self.root / "fleet.json").write_text('{"controllers": [{"url": "https://user:KNOWN_SECRET_VALUE_987654321@example.test"}]}')
        backup.save_settings({"include_fleet": True})
        result = backup.run_nightly_settings()
        self.assertFalse(result["ok"])
        self.assertNotIn("KNOWN_SECRET_VALUE_987654321", json.dumps(result))

    def test_failure_after_success_keeps_both_records(self):
        self.assertTrue(backup.run_nightly_settings()["ok"])
        with patch.object(backup, "pack", side_effect=OSError("disk unavailable")), patch.object(backup.time, "time", return_value=backup.time.time() + 1):
            result = backup.run_nightly_settings()
        self.assertFalse(result["ok"])
        status = admin.get_nightly_backup()
        self.assertIsNotNone(status["last_success"])
        self.assertIn("disk unavailable", status["last_failure"]["reason"])
        self.assertFalse(list((self.root / "backups/nightly").glob("*.partial")))


class RealShapedSettings(unittest.TestCase):
    """Shapes taken from the real Gateway's files: both once made every night's backup fail."""

    def test_a_provider_declaring_its_credential_fields_is_not_a_secret(self) -> None:
        data = json.dumps({"providers": [{"id": "x", "credential_fields": [{"key": "api_key", "label": "Key", "env_var": "X_KEY"}]}]})
        backup._check_settings_data("providers.local.json", data.encode())

    def test_a_real_credential_inside_a_provider_is_still_refused(self) -> None:
        data = json.dumps({"providers": [{"id": "x", "api_key": "abc"}]})
        with self.assertRaises(backup.BackupError):
            backup._check_settings_data("providers.local.json", data.encode())

    def test_the_rebuilt_model_cache_is_not_backed_up(self) -> None:
        self.assertNotIn("gateway/data/models_cache.json", backup.SETTINGS_ROLES)


if __name__ == "__main__":
    unittest.main()
