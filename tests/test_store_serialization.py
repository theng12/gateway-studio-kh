#!/usr/bin/env python3
"""One SQLite connection is used by many Gateway threads, so every access is serialized."""
from __future__ import annotations

import sys
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
