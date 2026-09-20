from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.relocate_profile import RelocationError, apply_plan, main, plan_relocation


class ProfileRelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-relocation-")
        root = Path(self.temp.name)
        self.old_root = root / "old-home"
        self.new_root = root / "new-home"
        self.profile = root / "copied-profile"
        (self.old_root / "Library/Application Support/StudioHub/voices").mkdir(
            parents=True
        )
        (self.new_root / "Library/Application Support/StudioHub/voices").mkdir(
            parents=True
        )
        (self.old_root / "gateway-artifacts").mkdir(parents=True)
        (self.new_root / "gateway-artifacts").mkdir(parents=True)
        (self.old_root / "uploads").mkdir(parents=True)
        (self.new_root / "uploads").mkdir(parents=True)
        (self.profile / "data").mkdir(parents=True)

        self.voice_old = (
            self.old_root
            / "Library/Application Support/StudioHub/voices/voice-a/reference.wav"
        )
        self.voice_new = self.new_root / self.voice_old.relative_to(self.old_root)
        self.output_old = self.old_root / "gateway-artifacts/output.png"
        self.output_new = self.new_root / self.output_old.relative_to(self.old_root)
        self.upload_old = self.old_root / "uploads/input.wav"
        self.upload_new = self.new_root / self.upload_old.relative_to(self.old_root)
        for source, target, content in (
            (self.voice_old, self.voice_new, b"voice-bytes"),
            (self.output_old, self.output_new, b"output-bytes"),
            (self.upload_old, self.upload_new, b"upload-bytes"),
        ):
            source.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
            target.write_bytes(content)

        self.voices_path = self.profile / "data/voices.json"
        self.db_path = self.profile / "data/gateway.sqlite3"
        self.kept_path = self.profile / "data/kept.json"
        self.voices_path.write_text(
            json.dumps(
                {
                    "_comment": "keep /old-home in this prose",
                    "voices": [
                        {
                            "id": "voice-a",
                            "display_name": "Fixture Voice",
                            "reference_path": str(self.voice_old),
                            "reference_transcript": "A transcript mentioning /old-home stays text.",
                            "permission_acknowledged": True,
                            "license": {"kind": "private", "granted": True},
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.voices_path.chmod(0o640)

        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                modality TEXT NOT NULL,
                model_id TEXT,
                provider_id TEXT,
                state TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                asset_path TEXT,
                media_type TEXT,
                result_json TEXT,
                error_code TEXT,
                error_detail TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                next_action_at REAL,
                artifacts_json TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO jobs
                (id, modality, state, params, asset_path, media_type,
                 result_json, created_at, updated_at, artifacts_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-a",
                "image",
                "succeeded",
                json.dumps(
                    {
                        "_upload_path": str(self.upload_old),
                        "prompt": "literal /old-home text is not a path field",
                    }
                ),
                str(self.output_old),
                "image/png",
                json.dumps({"note": "content remains"}),
                1.0,
                1.0,
                json.dumps(
                    [
                        {
                            "asset_id": "0",
                            "path": str(self.output_old),
                            "media_type": "image/png",
                            "label": "literal /old-home label",
                        }
                    ]
                ),
            ),
        )
        connection.commit()
        connection.close()
        self.db_path.chmod(0o600)

        self.kept_path.write_text(
            json.dumps(
                {
                    "kept": [
                        {
                            "path": str(self.output_old),
                            "job_id": "job-a",
                            "asset_id": "0",
                            "kept_at": 1.0,
                            "note": "literal /old-home text stays",
                        }
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.kept_path.chmod(0o640)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plan(self):
        return plan_relocation(self.profile, self.old_root, self.new_root)

    def test_cli_defaults_to_read_only_dry_run(self) -> None:
        before = {
            path: path.read_bytes()
            for path in (self.voices_path, self.db_path, self.kept_path)
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "--profile-root",
                    str(self.profile),
                    "--old-root",
                    str(self.old_root),
                    "--new-root",
                    str(self.new_root),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("DRY-RUN", output.getvalue())
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in before},
        )
        self.assertFalse((self.profile / ".relocation-backups").exists())

    def test_source_home_need_not_exist_on_destination_mac(self) -> None:
        self.old_root.rename(self.old_root.with_name("source-not-mounted"))
        plan = self._plan()
        self.assertGreater(len(plan.references), 0)
        apply_plan(plan)
        voice = json.loads(self.voices_path.read_text())["voices"][0]
        self.assertEqual(voice["reference_path"], str(self.voice_new))

    def test_cli_apply_reports_success_and_keeps_backup(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "--profile-root",
                    str(self.profile),
                    "--old-root",
                    str(self.old_root),
                    "--new-root",
                    str(self.new_root),
                    "--apply",
                    "--backup-dir",
                    str(self.profile / "cli-backup"),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("APPLIED", output.getvalue())
        self.assertTrue((self.profile / "cli-backup/voices.json").exists())
        self.assertEqual(
            json.loads(self.voices_path.read_text(encoding="utf-8"))["voices"][0]["reference_path"],
            str(self.voice_new),
        )

    def test_apply_relocates_voice_outputs_upload_and_pin_without_changing_content(self) -> None:
        modes = {
            path: os.stat(path).st_mode & 0o777
            for path in (self.voices_path, self.db_path, self.kept_path)
        }
        result = apply_plan(self._plan(), backup_dir=self.profile / "backup")
        self.assertEqual(result["changed_files"], 3)
        self.assertTrue((self.profile / "backup/voices.json").exists())

        voices = json.loads(self.voices_path.read_text(encoding="utf-8"))
        voice = voices["voices"][0]
        self.assertEqual(voice["id"], "voice-a")
        self.assertEqual(voice["reference_path"], str(self.voice_new))
        self.assertEqual(voice["reference_transcript"], "A transcript mentioning /old-home stays text.")
        self.assertTrue(voice["permission_acknowledged"])
        self.assertEqual(voice["license"], {"kind": "private", "granted": True})

        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT params, asset_path, artifacts_json FROM jobs WHERE id = ?",
            ("job-a",),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        params, asset_path, artifacts_json = row
        self.assertEqual(json.loads(params)["_upload_path"], str(self.upload_new))
        self.assertEqual(json.loads(params)["prompt"], "literal /old-home text is not a path field")
        self.assertEqual(asset_path, str(self.output_new))
        artifact = json.loads(artifacts_json)[0]
        self.assertEqual(artifact["path"], str(self.output_new))
        self.assertEqual(artifact["label"], "literal /old-home label")

        kept = json.loads(self.kept_path.read_text(encoding="utf-8"))["kept"][0]
        self.assertEqual(kept["path"], str(self.output_new))
        self.assertEqual(kept["job_id"], "job-a")
        self.assertEqual(self.output_new.read_bytes(), b"output-bytes")
        self.assertEqual(
            modes,
            {path: os.stat(path).st_mode & 0o777 for path in modes},
        )

    def test_failed_replacement_rolls_back_all_profile_files(self) -> None:
        plan = self._plan()
        before = {
            path: path.read_bytes()
            for path in (self.voices_path, self.db_path, self.kept_path)
        }

        def fail_on_database(source: Path, target: Path) -> None:
            if target == self.db_path:
                raise OSError("fixture replacement failure")
            os.replace(source, target)

        with self.assertRaises(RelocationError):
            apply_plan(
                plan,
                backup_dir=self.profile / "rollback-backup",
                replace=fail_on_database,
            )
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in before},
        )

    def test_plan_does_not_read_old_asset_bytes(self) -> None:
        original_read_bytes = Path.read_bytes
        source_paths = {self.voice_old, self.output_old, self.upload_old}

        def reject_source_reads(path: Path) -> bytes:
            if path in source_paths:
                raise AssertionError(f"source asset was read: {path}")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", reject_source_reads):
            plan = self._plan()
        self.assertEqual(len(plan.references), 5)

    def test_rejects_missing_escape_symlink_and_destination_conflicts(self) -> None:
        cases = ("missing", "escape", "symlink", "conflict")
        for index, case in enumerate(cases):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(case=case):
                if case == "missing":
                    self.upload_new.unlink()
                elif case == "escape":
                    outside = Path(self.temp.name) / "outside.wav"
                    outside.write_bytes(b"outside")
                    payload = json.loads(self.voices_path.read_text(encoding="utf-8"))
                    payload["voices"][0]["reference_path"] = str(
                        self.old_root / ".." / "outside.wav"
                    )
                    self.voices_path.write_text(json.dumps(payload), encoding="utf-8")
                elif case == "symlink":
                    self.output_new.unlink()
                    self.output_new.symlink_to(self.new_root / "uploads/input.wav")
                else:
                    other_old = self.old_root / "gateway-artifacts/other.png"
                    other_new = self.new_root / "gateway-artifacts/other.png"
                    os.link(self.output_new, other_new)
                    payload = json.loads(self.kept_path.read_text(encoding="utf-8"))
                    payload["kept"].append({"path": str(other_old), "job_id": "job-b"})
                    self.kept_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(RelocationError):
                    self._plan()


if __name__ == "__main__":
    unittest.main()
