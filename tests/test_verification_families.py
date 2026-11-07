"""Isolated checks supplementing the verification-families-20261107 request.

Exercises machine-label responses and shared-page anchors (F1), settings and
voice persistence plus key rotation (F2), asset-upload replay (F3), and active
input protection with recoverable pruning (F5). Existing idempotency, restart,
subtitle and retention-input suites cover the other offline boundaries.
Installed-machine readback, final-video parity and resource measurements need
separate live evidence; this file does not prove them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-verification-families-")
os.environ["GATEWAY_HOST"] = "127.0.0.1"

from fastapi.testclient import TestClient

from gateway import config, retention, voice_import, voices
from gateway.admin import computer_name
from gateway.api import app, require_auth, GatewayError
from gateway.render_assets import (
    AssetConflictError,
    RenderAssetStore,
)
from gateway.store import JobStore


def _wave_clip(path: Path, seconds: float = 0.5, tone: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(bytes([tone % 256, 0]) * int(8000 * seconds))
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FamilyF1MachineLabelsTest(unittest.TestCase):
    """Check 01 — F1: Installed serving-Mac labels."""

    def tearDown(self) -> None:
        computer_name.cache_clear()

    def test_01_f1_machine_label_literal_special_chars_and_markup(self) -> None:
        """Markup-like and quoted names are served literally in the JSON payload without alteration."""
        markup_name = '<script>alert("xss")</script> & \'Studio\' "Mini"'
        computer_name.cache_clear()
        done = subprocess.CompletedProcess([], 0, stdout=f"{markup_name}\n", stderr="")
        with patch("gateway.admin.subprocess.run", return_value=done):
            client = TestClient(app, client=("127.0.0.1", 50000))
            data = client.get("/admin/api/status").json()
        self.assertEqual(data["computer_name"], markup_name)

    def test_01_f1_machine_label_honest_null_fallback_on_unavailable_name(self) -> None:
        """Unavailable machine names return honest null, never an invented or blank string."""
        failures = {
            "scutil_error": OSError("scutil unavailable"),
            "scutil_nonzero": subprocess.CompletedProcess([], 1, stdout="", stderr="refused"),
            "scutil_whitespace": subprocess.CompletedProcess([], 0, stdout="   \n", stderr=""),
        }
        client = TestClient(app, client=("127.0.0.1", 50000))
        for reason, outcome in failures.items():
            with self.subTest(reason=reason):
                computer_name.cache_clear()
                with patch("gateway.admin.subprocess.run", side_effect=outcome if isinstance(outcome, Exception) else None,
                           return_value=outcome if not isinstance(outcome, Exception) else None):
                    data = client.get("/admin/api/status").json()
                self.assertIsNone(data["computer_name"], f"Failed honest null for {reason}")

    def test_01_f1_shared_page_template_anchors(self) -> None:
        """The shared admin HTML contains the exact literal machine label and version anchors.

        Live visual proof on an installed serving Mac (1301 and local) is recorded NOT-CHECKED in
        the lane report; this test only proves the served page carries the anchors the client needs.
        """
        client = TestClient(app, client=("127.0.0.1", 50000))
        page = client.get("/admin").text
        self.assertIn('<span class="nav-machine" id="nav-machine" hidden></span>', page)
        self.assertIn('id="app-version"', page)


class FamilyF2SettingsPersistenceTest(unittest.TestCase):
    """Check 02, 03, 04 — F2: Retention, voice choices, config rotation persistence."""

    def test_02_f2_retention_partial_save_and_reopen_persists_all_fields(self) -> None:
        """Partial save updates one field; fresh process reopen retains all non-default values and units."""
        with tempfile.TemporaryDirectory(prefix="retention-reopen-") as tmp:
            tmp_data = Path(tmp)
            settings_path = tmp_data / "retention.json"
            with patch.object(retention, "SETTINGS_PATH", settings_path):
                # 1. Save non-default tuple
                retention.save(
                    max_age_days=1.5,
                    max_bytes=4 * 1024**3,
                    trash_days=4.0,
                    trash_cap_bytes=16 * 1024**3,
                )
                # 2. Partial save of trash_days only
                partial = retention.save(trash_days=2.5)
                self.assertEqual(partial["trash_days"], 2.5)
                self.assertEqual(partial["max_age_days"], 1.5)
                self.assertEqual(partial["max_bytes"], 4 * 1024**3)
                self.assertEqual(partial["trash_cap_bytes"], 16 * 1024**3)

            # 3. Reopen isolated fresh subprocess reading the persisted file
            script = (
                "import json\n"
                "from pathlib import Path\n"
                "from gateway import retention\n"
                f"retention.SETTINGS_PATH = Path(r'{settings_path}')\n"
                "print(json.dumps(retention.settings()))\n"
            )
            res = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
            )
            reopened = json.loads(res.stdout)
            self.assertEqual(reopened["trash_days"], 2.5)
            self.assertEqual(reopened["max_age_days"], 1.5)
            self.assertEqual(reopened["max_bytes"], 4 * 1024**3)
            self.assertEqual(reopened["trash_cap_bytes"], 16 * 1024**3)

    def test_03_f2_voice_choices_persist_across_reopen_and_profile_isolation(self) -> None:
        """Voice switches survive reopen, transcript off sends no words, and profiles remain strictly isolated."""
        with tempfile.TemporaryDirectory(prefix="profile-a-") as dir_a, tempfile.TemporaryDirectory(prefix="profile-b-") as dir_b:
            path_a = Path(dir_a)
            catalog_a = path_a / "voices.json"
            lib_a = path_a / "voice-library"
            clip = _wave_clip(path_a / "raw.wav")

            with patch.object(voices, "VOICES_PATH", catalog_a), \
                 patch.object(voices, "WRITABLE_VOICES_PATH", catalog_a), \
                 patch.object(voice_import, "VOICES_PATH", catalog_a), \
                 patch.object(voice_import, "WRITABLE_VOICES_PATH", catalog_a), \
                 patch.object(voice_import, "LIBRARY_DIR", lib_a):
                # Import voice with transcript=False and explicit language
                added = voice_import.import_paths([clip], transcript=False)
                voice_id = added["imported"][0]["id"]
                voice_import.update_voice(voice_id, language="km")

                # Verify choices stored in catalog file
                catalog_data = json.loads(catalog_a.read_text(encoding="utf-8"))
                found = next((v for v in catalog_data.get("voices", []) if v["id"] == voice_id), None)
                self.assertIsNotNone(found)
                self.assertFalse(found.get("include_reference_transcript", True))
                self.assertEqual(found.get("language"), "km")

                # Nonempty stored words must still be excluded by the actual Hub payload builder.
                payload = voice_import.push_fields({
                    **found,
                    "reference_transcript": "These words must not travel.",
                    "reference_segments": [{"text": "These words must not travel."}],
                })
                self.assertEqual(payload["transcript"], "")
                self.assertEqual(json.loads(payload["transcript_segments"]), [])

            # Fresh process reopen in Profile A
            script = (
                "import json\n"
                "from pathlib import Path\n"
                "from gateway import voice_import\n"
                f"voice_import.VOICES_PATH = Path({str(catalog_a)!r})\n"
                "voice_import.WRITABLE_VOICES_PATH = voice_import.VOICES_PATH\n"
                "data = voice_import._read_catalog()\n"
                f"v = next(item for item in data.get('voices', []) if item['id'] == r'{voice_id}')\n"
                "print(json.dumps({'transcript': v.get('include_reference_transcript', True), 'language': v.get('language')}))\n"
            )
            res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
            reopened = json.loads(res.stdout)
            self.assertFalse(reopened["transcript"])
            self.assertEqual(reopened["language"], "km")

            # Profile B cannot see Profile A's voice
            catalog_b = Path(dir_b) / "voices.json"
            with patch.object(voice_import, "VOICES_PATH", catalog_b), \
                 patch.object(voice_import, "WRITABLE_VOICES_PATH", catalog_b):
                data_b = voice_import._read_catalog()
            self.assertFalse(any(v["id"] == voice_id for v in data_b.get("voices", [])),
                             "Profile B must not access Profile A voices")

    def test_04_f2_config_rotation_live_read_and_reopen_persists_ownership(self) -> None:
        """Wrong key refused first; .env rotation accepted without restart; reopen retains file choice."""
        token_a = "secret-token-alpha"
        token_b = "secret-token-beta"
        wrong_token = "secret-token-wrong"

        config.ENV_PATH.write_text(f"GATEWAY_API_KEY={token_a}\n", encoding="utf-8")
        config._ENV_STAMP = None
        config._ENV_FILE = None

        # 1. Wrong key is refused 403
        dummy_req = SimpleNamespace(headers={"host": "127.0.0.1"}, client=("127.0.0.1", 50000))
        with patch("gateway.api.is_loopback", return_value=False), \
             patch("gateway.api.request_is_loopback", return_value=False):
            with self.assertRaises(GatewayError) as caught:
                require_auth(dummy_req, authorization=f"Bearer {wrong_token}")
            self.assertEqual(caught.exception.status, 403)

            # 2. Key A is accepted
            require_auth(dummy_req, authorization=f"Bearer {token_a}")

            # 3. Live rotation to Key B in .env without process restart
            time.sleep(0.01)
            config.ENV_PATH.write_text(f"GATEWAY_API_KEY={token_b}\n", encoding="utf-8")
            now = time.time() + 2.0
            os.utime(config.ENV_PATH, (now, now))

            # Key A now immediately refused
            with self.assertRaises(GatewayError) as caught:
                require_auth(dummy_req, authorization=f"Bearer {token_a}")
            self.assertEqual(caught.exception.status, 403)

            # Key B accepted
            require_auth(dummy_req, authorization=f"Bearer {token_b}")

        # 4. Process reopen retains the file-backed choice
        sub_env = os.environ.copy()
        sub_env["GATEWAY_ENV_FILE"] = str(config.ENV_PATH)
        res = subprocess.run([sys.executable, "-c", "from gateway import config; print(config.gateway_api_key())"],
                             env=sub_env, capture_output=True, text=True, check=True)
        self.assertEqual(res.stdout.strip(), token_b)


class FamilyF3IdempotencyRecoveryTest(unittest.TestCase):
    """Check 07 — F3: Interrupted asset upload retains its winning handle."""

    def test_07_f3_interrupted_asset_upload_has_one_immutable_winning_handle(self) -> None:
        """Chunk replay/finish replay preserves exact hash and single handle; interrupted range holds across reopen."""
        with tempfile.TemporaryDirectory(prefix="asset-store-") as tmp:
            store_root = Path(tmp) / "store"
            store = RenderAssetStore(
                str(store_root),
                max_chunk_bytes=64 * 1024,
                max_asset_bytes=16 * 1024 * 1024,
                reserve_bytes=2 * 1024 * 1024,
                measure_free_bytes=lambda: 512 * 1024 * 1024,
            )
            data = b"chunk1-bytes" + b"chunk2-bytes"
            expected_sha = _sha256(data)
            owner = "test-principal"

            begun = store.begin_upload(owner, expected_sha, len(data))
            upload_id = begun["upload_id"]

            # Append chunk 1
            part1 = b"chunk1-bytes"
            store.append_chunk(owner, upload_id, 0, io.BytesIO(part1), len(part1))

            # Idempotent replay of chunk 1
            store.append_chunk(owner, upload_id, 0, io.BytesIO(part1), len(part1))

            # Conflicting range is refused
            with self.assertRaises(AssetConflictError):
                store.append_chunk(owner, upload_id, 0, io.BytesIO(b"X" * len(part1)), len(part1))

            # Append chunk 2
            part2 = b"chunk2-bytes"
            store.append_chunk(owner, upload_id, len(part1), io.BytesIO(part2), len(part2))

            # Finish upload produces winning handle
            first_finished = store.finish_upload(owner, upload_id)
            winning_handle = first_finished["asset_handle"]
            self.assertEqual(first_finished["sha256"], expected_sha)

            # Finish replay returns exact same winning handle idempotently
            replay_finished = store.finish_upload(owner, upload_id)
            self.assertEqual(replay_finished["asset_handle"], winning_handle)
            self.assertEqual(replay_finished["sha256"], expected_sha)

            # Interrupted range holds across reopen
            begun2 = store.begin_upload(owner, _sha256(b"incomplete"), 100)
            store.close()

            reopened_store = RenderAssetStore(
                str(store_root),
                max_chunk_bytes=64 * 1024,
                max_asset_bytes=16 * 1024 * 1024,
                reserve_bytes=2 * 1024 * 1024,
                measure_free_bytes=lambda: 512 * 1024 * 1024,
            )
            # Querying incomplete upload status after reopen
            status = reopened_store.upload_status(owner, begun2["upload_id"])
            self.assertIn(status["state"], ("incomplete", "active", "held"))
            reopened_store.close()


class FamilyF5ResourceProtectionTest(unittest.TestCase):
    """Check 11 — F5: Bounded resources and recoverable release cleanup."""

    def test_11_f5_active_inputs_protected_and_unreadable_fails_closed(self) -> None:
        """Active inputs remain protected; unreadable protection fails closed; pruning moves to _trash."""
        with tempfile.TemporaryDirectory(prefix="retention-protect-") as tmp:
            root = Path(tmp)
            art_dir = root / "artifacts"
            trash_dir = root / "_trash"
            db_path = root / "jobs.sqlite3"
            settings_path = root / "retention.json"
            art_dir.mkdir(parents=True)
            trash_dir.mkdir(parents=True)

            store = JobStore(db_path)
            old_time = time.time() - (4 * 86400)  # 4 days old

            # 1. Active referenced input
            ref_file = art_dir / "referenced.wav"
            ref_file.write_bytes(b"referenced-audio-bytes")
            os.utime(ref_file, (old_time, old_time))

            job, _ = store.submit(
                modality="transcription",
                model_id="whisper",
                params={"_upload_path": str(ref_file)},
                idempotency_key="ref-input-job",
            )
            store.mark_running(job["id"])

            # 2. Unreferenced file that should be pruned
            unref_file = art_dir / "unreferenced.bin"
            unref_file.write_bytes(b"unreferenced-bytes")
            os.utime(unref_file, (old_time, old_time))

            with patch.object(retention, "ARTIFACT_DIR", art_dir), \
                 patch.object(retention, "TRASH_DIR", trash_dir), \
                 patch.object(retention, "SETTINGS_PATH", settings_path):
                retention.save(max_age_days=1.0, max_bytes=100 * 1024**2)

                # Prune pass: referenced input survives, unreferenced moves to trash
                result = retention.prune(now=time.time(), store=store)
                self.assertTrue(ref_file.exists(), "Referenced input must survive retention prune")
                self.assertFalse(unref_file.exists(), "Unreferenced old file must be moved")
                # Prune moves recoverably to _trash rather than unlinking
                trash_files = list(trash_dir.rglob("unreferenced.bin"))
                self.assertTrue(len(trash_files) >= 1, "File must be moved to _trash, never unlinked")

                # 3. Unreadable protection fails closed
                another_file = art_dir / "protected_fail_closed.bin"
                another_file.write_bytes(b"fail-closed-test")
                os.utime(another_file, (old_time, old_time))

                with patch.object(store, "protected_input_paths", side_effect=OSError("DB lock failure")):
                    fc_result = retention.prune(now=time.time(), store=store)
                    self.assertTrue(another_file.exists(), "Unreadable protection must fail closed and preserve files")
                    self.assertEqual(fc_result.get("deleted"), 0)
                    self.assertIn("blocked_reason", fc_result)

            store.close()


if __name__ == "__main__":
    unittest.main()
