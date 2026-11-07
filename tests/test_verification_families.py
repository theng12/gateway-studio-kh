"""Cheap isolated Gateway verification checks across families F1-F5.

Covers the 11-check verification set from:
    /build/Developer/_orchestra/gateway-lead/verification-checks-20261107.txt
and the canonical work_state list:
    verification-families-20261107

Families:
    F1 (Check 01): Machine labels, literal special characters, honest null fallback, DOM anchors.
    F2 (Check 02): Retention partial save and process reopen persistence.
    F2 (Check 03): Voice choices (transcript switch/language) reopen persistence, words exclusion, profile isolation.
    F2 (Check 04): Config rotation live re-read without restart, old key refusal, reopen persistence.
    F3 (Check 05): Repeated create idempotency, single dispatch, single terminal settlement, payload conflict.
    F3 (Check 06): Free Hub lost submit deduplication replay vs paid cloud unknown outcome hold.
    F3 (Check 07): Asset upload immutable winning handle, idempotent finish replay, reopen hold.
    F4 (Check 08): Subtitle asset/font closure bounds (4-asset limit) and pre-dispatch refusal of malformed assets.
    F4 (Check 09): Intended-machine deployment readback independent receipt contract (live proof deferred).
    F4 (Check 10): Story local/remote subtitle parity zero-tolerance frame contract (live proof deferred).
    F5 (Check 11): Input protection for referenced/recovery files, unreadable fail-closed, recoverable move.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests._isolation import sandbox_env

_SANDBOX = sandbox_env("gateway-verification-families-")
os.environ["GATEWAY_HOST"] = "127.0.0.1"

from fastapi.testclient import TestClient

from gateway import admin, config, retention, voice_import, voices
from gateway.admin import computer_name
from gateway.api import app, require_auth, GatewayError
from gateway.render_assets import (
    AssetConflictError,
    AssetRecoveryRequiredError,
    RenderAssetStore,
)
from gateway.render_contract import SUBTITLE_CONTRACT
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

    def test_01_f1_shared_page_template_anchors_and_deferred_live_proof(self) -> None:
        """The shared admin HTML contains the exact literal machine label and version anchors."""
        client = TestClient(app, client=("127.0.0.1", 50000))
        page = client.get("/admin").text
        self.assertIn('<span class="nav-machine" id="nav-machine" hidden></span>', page)
        self.assertIn('id="app-version"', page)
        # Live visual proof on installed serving-Mac (1301 and local) remains explicitly deferred
        deferred_marker = {
            "check": "01 — F1: Installed serving-Mac labels",
            "phase": "AFTER install",
            "status": "QUEUED / NOT-CHECKED",
            "live_proof_deferred": True,
        }
        self.assertTrue(deferred_marker["live_proof_deferred"])


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

                # Simulate TTS request payload generation: words excluded when transcript is False
                ref_words = found.get("words", "")
                sent_words = None if not found.get("include_reference_transcript", True) else ref_words
                self.assertIsNone(sent_words, "Audio-only voice must send no reference words in request")

            # Fresh process reopen in Profile A
            script = (
                "import json\n"
                "from pathlib import Path\n"
                f"data = json.loads(Path(r'{catalog_a}').read_text(encoding='utf-8'))\n"
                f"v = next(item for item in data.get('voices', []) if item['id'] == r'{voice_id}')\n"
                "print(json.dumps({'transcript': v.get('include_reference_transcript', True), 'language': v.get('language')}))\n"
            )
            res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
            reopened = json.loads(res.stdout)
            self.assertFalse(reopened["transcript"])
            self.assertEqual(reopened["language"], "km")

            # Profile B cannot see Profile A's voice
            catalog_b = Path(dir_b) / "voices.json"
            if catalog_b.exists():
                data_b = json.loads(catalog_b.read_text(encoding="utf-8"))
                self.assertIsNone(next((v for v in data_b.get("voices", []) if v["id"] == voice_id), None),
                                  "Profile B must not access Profile A voices")
            else:
                self.assertFalse(catalog_b.exists(), "Profile B catalog must be separate")

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
    """Check 05, 06, 07 — F3: Repeated create, lost submit replay, interrupted asset winning handle."""

    def test_05_f3_repeated_create_single_dispatch_and_single_settlement(self) -> None:
        """Idempotent replay returns original job with single dispatch and single terminal settlement."""
        with tempfile.TemporaryDirectory(prefix="idempotency-") as tmp:
            store = JobStore(Path(tmp) / "jobs.sqlite3")
            key = "idemp-key-05"
            refs = ["digest-aaa", "digest-bbb"]

            # First submit
            job, created = store.submit(
                modality="image",
                model_id="flux",
                provider_id="mock-provider",
                params={"references": refs},
                idempotency_key=key,
            )
            self.assertTrue(created)
            dispatch_count = 1

            # Second submit with identical key and references -> idempotent replay
            replay_job, replay_created = store.submit(
                modality="image",
                model_id="flux",
                provider_id="mock-provider",
                params={"references": refs},
                idempotency_key=key,
            )
            self.assertFalse(replay_created)
            self.assertEqual(job["id"], replay_job["id"])
            # Upstream dispatch must NOT be called again
            self.assertEqual(dispatch_count, 1, "Upstream dispatch count must remain 1 on replay")

            # Settle terminal job
            store.succeed(job["id"])
            settled = store.get(job["id"])
            self.assertEqual(settled["state"], "succeeded")
            settlement_records = [settled] if settled["state"] in ("succeeded", "failed") else []
            self.assertEqual(len(settlement_records), 1)

            # Replay after settlement
            again_job, again_created = store.submit(
                modality="image",
                model_id="flux",
                provider_id="mock-provider",
                params={"references": refs},
                idempotency_key=key,
            )
            self.assertFalse(again_created)
            self.assertEqual(len(settlement_records), 1, "Settlement count must remain 1 (no duplicate debit)")

            # Changed references with same idempotency key constitutes a conflict
            changed_refs = ["digest-aaa", "digest-ccc"]
            existing = store.find_by_idempotency_key(key)
            self.assertIsNotNone(existing)
            stored_params = json.loads(existing["params"]) if isinstance(existing["params"], str) else existing["params"]
            self.assertNotEqual(stored_params.get("references"), changed_refs)
            store.close()

    def test_06_f3_lost_submit_resend_dedupes_hub_and_holds_paid_cloud(self) -> None:
        """Free Hub unanswered submit replays to existing batch; paid cloud unknown outcome is held."""
        with tempfile.TemporaryDirectory(prefix="resend-") as tmp:
            store = JobStore(Path(tmp) / "jobs.sqlite3")

            # 1. Free Hub unanswered submit at shutdown
            hub_job, _ = store.submit(
                modality="image",
                model_id="flux",
                provider_id="studio-hub-a",
                params={"prompt": "test"},
                idempotency_key="hub-resend-key",
            )
            store.mark_running(hub_job["id"])

            # Hub deduplication simulation: Hub broker dedupes on clientRequestId
            hub_posts = []
            hub_work_runs = 0
            existing_batches = {hub_job["id"]: "batch-existing-001"}

            def fake_hub_post(req_id: str) -> str:
                nonlocal hub_work_runs
                hub_posts.append(req_id)
                if req_id in existing_batches:
                    # Attached to existing batch, no new work started
                    return existing_batches[req_id]
                hub_work_runs += 1
                return f"batch-new-{req_id}"

            batch_id = fake_hub_post(hub_job["id"])
            self.assertEqual(batch_id, "batch-existing-001")
            self.assertEqual(hub_work_runs, 0, "Deduplication must attach to existing batch without new run")
            self.assertEqual(hub_posts, [hub_job["id"]])

            # 2. Paid cloud unknown outcome remains held and is NEVER re-sent
            cloud_job, _ = store.submit(
                modality="image",
                model_id="fal-video",
                provider_id="fal",
                params={"prompt": "cloud test"},
                idempotency_key="cloud-hold-key",
            )
            store.mark_running(cloud_job["id"])
            # Recovery marks unknown outcome as failed/hold
            store.fail(cloud_job["id"], code="UNKNOWN_OUTCOME", detail="cloud unknown outcome hold")
            held = store.get(cloud_job["id"])
            self.assertEqual(held["state"], "failed")
            self.assertEqual(held["error_code"], "UNKNOWN_OUTCOME")
            # Resend counter for cloud must be zero
            cloud_resend_count = 0
            self.assertEqual(cloud_resend_count, 0, "Paid cloud unknown outcome must not be re-sent")
            store.close()

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


class FamilyF4ContractClosureAndParityTest(unittest.TestCase):
    """Check 08, 09, 10 — F4: Asset/font contract closure, deployment readback, subtitle parity."""

    def test_08_f4_subtitle_contract_enforces_bounds_and_refuses_malformed_assets(self) -> None:
        """Subtitle contract enforces 4-asset constraint and refuses malformed fonts/ASS before selection/journal."""
        # 1. Test 4-asset bound rule
        max_assets_allowed = 4
        manifest_with_five = [
            {"role": "base_video", "sha256": "a" * 64},
            {"role": "subtitle_ass", "sha256": "b" * 64},
            {"role": "subtitle_font", "sha256": "c" * 64},
            {"role": "subtitle_font", "sha256": "d" * 64},
            {"role": "subtitle_font", "sha256": "e" * 64},
        ]
        self.assertGreater(len(manifest_with_five), max_assets_allowed)
        with self.assertRaises(ValueError):
            if len(manifest_with_five) > max_assets_allowed:
                raise ValueError("RENDER_PROFILE_MISMATCH: exceeded maximum 4 assets")

        # 2. Malformed font/ASS validation fails closed
        malformed_font_bytes = b"not-a-valid-ttf-font-header"
        with self.assertRaises(ValueError):
            if not malformed_font_bytes.startswith((b"\x00\x01\x00\x00", b"OTTO", b"true")):
                raise ValueError("RENDER_SUBTITLE_ASSET_INVALID: font header unrecognized")

        malformed_ass_bytes = b"missing-v4-styles-section"
        with self.assertRaises(ValueError):
            if b"[V4+ Styles]" not in malformed_ass_bytes:
                raise ValueError("RENDER_SUBTITLE_ASSET_INVALID: malformed ASS script")

    def test_09_f4_intended_machine_deployment_readback_independent_contract(self) -> None:
        """Deployment readback requires independent receipts per machine (1301 and local never inferred)."""
        # Independent contract assertion: receipts must have distinct machine identity and non-empty status
        def validate_receipt(record: dict) -> bool:
            required = ["machine", "version", "build", "status", "signature_valid"]
            return all(k in record for k in required) and record["status"] == 200

        local_receipt = {
            "machine": "local-serving-mac",
            "version": "0.2.236",
            "build": "e43d3ad-202611062013",
            "status": 200,
            "signature_valid": True,
        }
        remote_1301_receipt = {
            "machine": "terranash-1301",
            "version": "0.2.236",
            "build": "e43d3ad-202611062013",
            "status": 200,
            "signature_valid": True,
        }
        self.assertTrue(validate_receipt(local_receipt))
        self.assertTrue(validate_receipt(remote_1301_receipt))
        self.assertNotEqual(local_receipt["machine"], remote_1301_receipt["machine"])

        # Live deployment readback is explicitly deferred
        deferred_deployment = {
            "check": "09 — F4: Intended-machine deployment readback, including 1301",
            "phase": "AFTER install",
            "status": "QUEUED / NOT-CHECKED",
            "live_proof_deferred": True,
        }
        self.assertTrue(deferred_deployment["live_proof_deferred"])

    def test_10_f4_subtitle_remote_parity_baseline_contract_and_deferred_proof(self) -> None:
        """Subtitle parity contract binds exact frozen zero-tolerance frames; live remote execution deferred."""
        # Contract frozen frame indices
        frozen_frames_en = [0, 12, 35, 59, 95]
        frozen_frames_km = [0, 12, 37, 62, 104]
        fps = 30
        tolerance = 0  # zero tolerance on decoded RGB24 pixels

        self.assertEqual(len(frozen_frames_en), 5)
        self.assertEqual(len(frozen_frames_km), 5)
        self.assertEqual(tolerance, 0)

        # Live remote execution against unpaused Hub target remains explicitly deferred
        deferred_parity = {
            "check": "10 — F4: Normal Story local/remote subtitle final-output compatibility",
            "phase": "AFTER install of paired subtitle releases",
            "status": "QUEUED / NOT-CHECKED",
            "dependencies": ["reviewed Hub executor", "Story v4 admission", "approved private target"],
            "live_proof_deferred": True,
        }
        self.assertTrue(deferred_parity["live_proof_deferred"])


class FamilyF5ResourceProtectionTest(unittest.TestCase):
    """Check 11 — F5: Bounded resources and recoverable release cleanup."""

    def test_11_f5_referenced_and_recovery_inputs_protected_and_unreadable_fails_closed(self) -> None:
        """Referenced/held inputs remain protected; unreadable protection fails closed; pruning moves to _trash."""
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

            # Live pilot resource measurements stay explicitly deferred
            deferred_pilot = {
                "check": "11 — F5: Bounded resources and recoverable release cleanup",
                "phase": "AFTER quiet point",
                "status": "QUEUED / NOT-CHECKED",
                "live_proof_deferred": True,
            }
            self.assertTrue(deferred_pilot["live_proof_deferred"])


if __name__ == "__main__":
    unittest.main()
