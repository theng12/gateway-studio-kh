#!/usr/bin/env python3
"""One SQLite connection is used by many Gateway threads, so every access is serialized."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-store-serialization-")

from gateway.store import JobStore  # noqa: E402


class _PausingConnection:
    """Pause a real terminal write before SQLite sees it, exposing an overlapping reader."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.writer_entered = threading.Event()
        self.release_writer = threading.Event()
        self.reader_touched_connection = threading.Event()

    def execute(self, sql, *args, **kwargs):
        name = threading.current_thread().name
        if name == "terminal-writer" and not self.writer_entered.is_set():
            self.writer_entered.set()
            if not self.release_writer.wait(2):
                raise AssertionError("test did not release the terminal writer")
        elif name == "concurrent-reader":
            self.reader_touched_connection.set()
        return self.connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class SharedConnectionSerializationTest(unittest.TestCase):
    def test_a_read_waits_while_terminal_settlement_owns_the_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.sqlite3")
            job, _ = store.submit(
                modality="chat", model_id="model", provider_id="provider",
                params={}, idempotency_key="one",
            )
            self.assertTrue(store.mark_running(job["id"]))
            connection = _PausingConnection(store._conn)
            store._conn = connection
            read_done = threading.Event()
            errors: list[BaseException] = []
            result = {}

            def settle() -> None:
                try:
                    store.succeed(job["id"])
                except BaseException as exc:  # surfaced on the main test thread below
                    errors.append(exc)

            def read() -> None:
                try:
                    result["job"] = store.get(job["id"])
                except BaseException as exc:  # surfaced on the main test thread below
                    errors.append(exc)
                finally:
                    read_done.set()

            writer = threading.Thread(
                target=settle, name="terminal-writer"
            )
            reader = threading.Thread(
                target=read, name="concurrent-reader",
            )
            writer.start()
            self.assertTrue(connection.writer_entered.wait(1), "terminal write did not start")
            reader.start()
            try:
                self.assertFalse(
                    read_done.wait(0.1),
                    "a reader reached the shared connection during terminal settlement",
                )
                self.assertFalse(connection.reader_touched_connection.is_set())
            finally:
                connection.release_writer.set()
                writer.join(2)
                reader.join(2)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(read_done.is_set())
            self.assertEqual(result["job"]["state"], "succeeded")
            self.assertEqual(store.get(job["id"])["state"], "succeeded")
            store.close()


class ReviewCandidateLocatorTest(unittest.TestCase):
    """A failed narration's review locator commits with its failure and outlives the live row."""

    LOCATOR = {"provider_id": "studio-hub-site-a", "batch_id": "hub-batch-1", "item_index": 0,
               "receipt_sha256": "a" * 64, "pcm_bytes": 4844, "pcm_sha256": "b" * 64}

    def running(self, store: JobStore, key: str) -> str:
        job, _ = store.submit(modality="tts", model_id="model", provider_id="studio-hub-site-a",
                              params={"text": "x"}, idempotency_key=key)
        self.assertTrue(store.mark_running(job["id"]))
        return job["id"]

    def test_the_locator_commits_with_the_failure_and_survives_dismiss_archive_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.sqlite3"
            store = JobStore(path)
            job_id = self.running(store, "bound")
            store.fail(job_id, code="PROVIDER_FAILED", detail="refused section",
                       review_candidate=dict(self.LOCATOR))
            self.assertEqual(store.get(job_id)["state"], "failed")
            other = sqlite3.connect(path)   # what another connection sees: both, or neither
            row = other.execute("SELECT provider_id, batch_id FROM review_candidate_locators "
                                "WHERE gateway_job_id = ?", (job_id,)).fetchone()
            other.close()
            self.assertEqual(row, ("studio-hub-site-a", "hub-batch-1"))

            store.fail(job_id, code="PROVIDER_FAILED", detail="late",
                       review_candidate=dict(self.LOCATOR, receipt_sha256="c" * 64))
            self.assertEqual(store.review_locator(job_id)["receipt_sha256"], "a" * 64)
            self.assertEqual(store.dismiss_failed(), 1)                 # the owner's "Clear failed"
            self.assertIsNotNone(store.review_locator(job_id))
            summary_columns = [row[1] for row in store._conn.execute("PRAGMA table_info(job_summaries)")]
            archived = store.archive_old(days=0, now=time.time() + 1, archive_dir=Path(tmp) / "archive")
            self.assertEqual(archived["archived"], 1)
            self.assertIsNone(store.get(job_id))
            store.close()

            reopened = JobStore(path)                                    # a restarted Gateway
            self.assertIsNone(reopened.get(job_id))
            kept = reopened.review_locator(job_id)
            self.assertEqual({key: kept[key] for key in self.LOCATOR}, self.LOCATOR)
            self.assertEqual(kept["schema_version"], 1)
            self.assertEqual([row[1] for row in reopened._conn.execute("PRAGMA table_info(job_summaries)")],
                             summary_columns)
            reopened.close()

    def test_a_bad_or_surplus_locator_never_changes_the_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.sqlite3")
            malformed = self.running(store, "malformed")
            store.fail(malformed, code="PROVIDER_FAILED", detail="refused",
                       review_candidate={"batch_id": "only"})
            self.assertEqual(store.get(malformed)["state"], "failed")
            self.assertIsNone(store.review_locator(malformed))
            for n in range(32):
                store.fail(self.running(store, f"fill-{n}"), code="PROVIDER_FAILED", detail="refused",
                           review_candidate=dict(self.LOCATOR, batch_id=f"batch-{n}"))
            surplus = self.running(store, "surplus")
            store.fail(surplus, code="PROVIDER_FAILED", detail="refused",
                       review_candidate=dict(self.LOCATOR))
            self.assertEqual(store.get(surplus)["state"], "failed")
            self.assertIsNone(store.review_locator(surplus))
            elsewhere = self.running(store, "elsewhere")
            store.fail(elsewhere, code="PROVIDER_FAILED", detail="refused",
                       review_candidate=dict(self.LOCATOR, provider_id="studio-hub-site-b"))
            self.assertIsNotNone(store.review_locator(elsewhere))
            store.close()


if __name__ == "__main__":
    unittest.main()
