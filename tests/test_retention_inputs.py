#!/usr/bin/env python3
"""Retention checks for staged inputs and protected outputs."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-retention-inputs-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import retention  # noqa: E402
from gateway.config import ARTIFACT_DIR  # noqa: E402
from gateway.store import JobStore  # noqa: E402


class RetentionInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(_TEMP_ROOT / f"{self._testMethodName}.sqlite3")

    def tearDown(self) -> None:
        self.store.close()

    @staticmethod
    def write_file(relative: str, *, age_s: float, size: int = 2_000_000) -> Path:
        path = ARTIFACT_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        old = time.time() - age_s
        os.utime(path, (old, old))
        return path

    def test_old_referenced_input_survives_while_old_unreferenced_and_protected_outputs_behave(self) -> None:
        now = time.time()
        referenced = self.write_file("uploads/referenced.wav", age_s=2 * 3600)
        unreferenced = self.write_file("unreferenced.bin", age_s=2 * 3600)
        recent = self.write_file("recent.bin", age_s=30)
        pinned = self.write_file("pinned.bin", age_s=2 * 3600)
        retention.save(max_age_days=0.05, max_bytes=1_000_000)
        retention.keep(pinned)
        row, created = self.store.submit(
            modality="transcription",
            model_id="mock-model",
            params={"_upload_path": str(referenced)},
            idempotency_key="retention-referenced",
            provider_id="mock-provider",
        )
        self.assertTrue(created)

        result = retention.prune(now=now, store=self.store)

        self.assertTrue(referenced.exists(), result)
        self.assertFalse(unreferenced.exists(), result)
        self.assertTrue(recent.exists(), result)
        self.assertTrue(pinned.exists(), result)
        self.assertGreaterEqual(result["deleted"], 1)
        self.assertEqual(self.store.get(row["id"])["state"], "accepted")

    def test_unknown_outcome_input_remains_protected_after_reopen(self) -> None:
        referenced = self.write_file("uploads/recovery.wav", age_s=2 * 3600)
        retention.save(max_age_days=0.05, max_bytes=1_000_000)
        row, _ = self.store.submit(
            modality="transcription",
            model_id="mock-model",
            params={"_upload_path": str(referenced)},
            idempotency_key="retention-recovery",
            provider_id="mock-provider",
        )
        self.assertTrue(self.store.mark_running(row["id"]))
        # **`begin_dispatch` is now what makes this a hold.** A job claimed but never dispatched
        # goes back to `accepted` on reopen (C2/M5) — it provably never left this process — so the
        # unknown-outcome case this test is about needs the dispatch marker. Without it the test
        # would still pass on the input assertion while no longer exercising a recovery hold at
        # all, which is the kind of green that hides a regression.
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        path = self.store.path
        self.store.close()
        self.store = JobStore(path)

        retention.prune(now=time.time(), store=self.store)

        recovered = self.store.get(row["id"])
        self.assertEqual(recovered["recovery_state"], "unknown_outcome")
        self.assertTrue(referenced.exists())

    def test_unreadable_protection_skips_deletion(self) -> None:
        referenced = self.write_file("uploads/database-unreadable.wav", age_s=2 * 3600)
        retention.save(max_age_days=0.05, max_bytes=1_000_000)
        with patch.object(self.store, "protected_input_paths", side_effect=OSError("database unavailable")):
            result = retention.prune(now=time.time(), store=self.store)
        self.assertTrue(referenced.exists())
        self.assertEqual(result["deleted"], 0)
        self.assertIn("blocked_reason", result)


if __name__ == "__main__":
    unittest.main()
