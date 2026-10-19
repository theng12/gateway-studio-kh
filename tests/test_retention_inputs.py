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


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._isolation import sandbox_env  # noqa: E402
_SANDBOX = sandbox_env("gateway-retention-inputs-")
_TEMP_ROOT = Path(_SANDBOX.name)

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


class TrashFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(_TEMP_ROOT / f"{self._testMethodName}.sqlite3")
        shutil.rmtree(retention.TRASH_DIR, ignore_errors=True)
        shutil.rmtree(ARTIFACT_DIR, ignore_errors=True)
        ARTIFACT_DIR.mkdir(parents=True)
        recycled = _TEMP_ROOT / self._testMethodName / "macos-trash"
        recycled.mkdir(parents=True, exist_ok=True)
        def recycle(path):
            path.rename(recycled / path.name)
            return True
        p = patch.object(retention, "_recycle", side_effect=recycle)
        p.start(); self.addCleanup(p.stop)
        for pin in retention.kept():
            retention.unkeep(pin["path"])

    def tearDown(self) -> None:
        self.store.close()

    def test_defaults_are_three_days_and_ten_gb(self) -> None:
        self.assertEqual(retention.DEFAULT_MAX_AGE_DAYS, 3.0)
        self.assertEqual(retention.DEFAULT_MAX_BYTES, 10 * 1024**3)

    def test_prune_moves_into_dated_trash_and_never_unlinks(self) -> None:
        now = time.time()
        old = RetentionInputTests.write_file("sub/old.bin", age_s=5 * 86400)
        young = RetentionInputTests.write_file("young.bin", age_s=30)
        retention.save(max_age_days=3, max_bytes=8 * 1024**3)
        result = retention.prune(now=now, store=self.store)
        moved = next(retention.TRASH_DIR.rglob("old.bin"))
        self.assertFalse(old.exists(), result)
        self.assertTrue(moved.is_file(), "pruned file must be in the Trash Folder, not gone")
        self.assertEqual(moved.stat().st_size, 2_000_000)
        self.assertTrue(young.exists())
        self.assertEqual(result["deleted"], 1)
        # a second file at the same path the same day gets a stamped name, not an overwrite
        again = RetentionInputTests.write_file("sub/old.bin", age_s=5 * 86400)
        retention.prune(now=now, store=self.store)
        self.assertFalse(again.exists())
        self.assertEqual(len(list(retention.TRASH_DIR.rglob("old.bin*"))), 2)

    def test_failed_move_skips_and_keeps_the_file(self) -> None:
        old = RetentionInputTests.write_file("stuck.bin", age_s=5 * 86400)
        with patch.object(Path, "rename", side_effect=OSError("cross-device")):
            result = retention.prune(now=time.time(), store=self.store)
        self.assertTrue(old.exists())
        self.assertEqual(result["deleted"], 0)

    def test_transfer_only_folders_older_than_seven_days(self) -> None:
        now = time.time()
        stale = retention.TRASH_DIR / retention._day(now - 8 * 86400)
        fresh = retention.TRASH_DIR / retention._day(now - 6 * 86400)
        stranger = retention.TRASH_DIR / "not-a-date"
        for folder in (stale, fresh, stranger):
            folder.mkdir(parents=True)
            (folder / "f.bin").write_bytes(b"x" * 10)
        result = retention.prune(now=now, store=self.store)
        self.assertFalse(stale.exists())
        self.assertTrue((fresh / "f.bin").exists())
        self.assertTrue((stranger / "f.bin").exists())
        self.assertEqual(result["purged_files"], 1)

    def test_max_bytes_ignores_the_trash_folder_and_usage_reports_it(self) -> None:
        today = retention.TRASH_DIR / retention._day(time.time())
        today.mkdir(parents=True)
        for name in ("a", "b"):
            (today / name).write_bytes(b"x" * 3_000_000)
        live = RetentionInputTests.write_file("live.bin", age_s=2 * 3600, size=2_000_000)
        retention.save(max_age_days=3, max_bytes=2_500_000)  # live alone fits; live+trash would not
        result = retention.prune(now=time.time(), store=self.store)
        self.assertTrue(live.exists(), result)
        use = retention.usage()
        self.assertEqual(use["trash_bytes"], 6_000_000)
        self.assertEqual(use["trash_days"], 7)
        self.assertEqual(use["bytes"], 2_000_000)


if __name__ == "__main__":
    unittest.main()
