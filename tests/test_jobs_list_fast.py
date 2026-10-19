#!/usr/bin/env python3
"""0.2.157: the Jobs list reads through indexes, and `/health` answers while store reads are slow.

Live 2026-10-09 (51k jobs, 300 MB): `GET /admin/api/jobs?limit=200` took 6-21 s, because every list,
count and dispatcher pass scanned the whole table behind the store's one lock. Requests waiting on
that lock filled anyio's shared thread pool, so `/health` (1 ms of work) waited seconds for a thread,
the app's 2.5 s probe timed out and the Jobs window said "The gateway is not running".
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-jobs-list-fast-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, api  # noqa: E402
from gateway.store import JobStore  # noqa: E402

store = api.store
NEW_INDEXES = {"jobs_created", "jobs_state_created", "jobs_modality_state", "jobs_open"}


def _plans(sql_seen: list[str]) -> list[str]:
    """EXPLAIN QUERY PLAN for each statement the store ran (placeholders bound to 0)."""
    out = []
    for sql in sql_seen:
        if not sql.lstrip().upper().startswith("SELECT"):
            continue
        rows = store._conn.execute("EXPLAIN QUERY PLAN " + sql, [0] * sql.count("?")).fetchall()
        out.append(" | ".join(str(r["detail"]) for r in rows))
    return out


def _traced(fn) -> list[str]:
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        store._conn.set_trace_callback(None)
    return seen


class QueryPlanTests(unittest.TestCase):
    def test_hot_reads_walk_an_index_not_the_table(self) -> None:
        reads = {
            "list": lambda: store.list_jobs(limit=200, hide_dismissed=True),
            "list page": lambda: store.list_jobs(limit=200, before=time.time(), hide_dismissed=True),
            "list removed": lambda: store.list_jobs(limit=200),
            "list failed": lambda: store.list_jobs(limit=200, state="failed", hide_dismissed=True),
            "due": store.due,
            "stats": store.stats,
            "removable": lambda: store.dismissable_failed_count(time.time()),
            "needs recovery": lambda: store.needs_recovery_count(time.time()),
        }
        for name, read in reads.items():
            plans = _plans(_traced(read))
            self.assertTrue(plans, name)
            for plan in plans:
                if "job_summaries" in plan:
                    continue  # stats' archived count; a separate, small table
                with self.subTest(read=name, plan=plan):
                    self.assertNotRegex(plan, r"SCAN jobs(?! USING)", "a full table scan")
                    self.assertNotIn("TEMP B-TREE", plan, "a sort of every matching row")

    def test_an_existing_database_gets_the_indexes_on_open(self) -> None:
        path = Path(_SANDBOX.name) / "old.sqlite3"
        old = JobStore(path)
        for name in NEW_INDEXES:
            old._conn.execute(f"DROP INDEX {name}")
        old._conn.commit()
        old.close()
        reopened = JobStore(path)
        names = {r[0] for r in reopened._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        reopened.close()
        self.assertLessEqual(NEW_INDEXES, names)


def _job(i: int, state: str, created: float, **fail) -> str:
    row, _ = store.submit(modality="tts" if i % 3 else "image", model_id="m", params={"n": i},
                          idempotency_key=f"fast-{i}-{time.time_ns()}", provider_id="pool")
    if state != "accepted":
        store.mark_running(row["id"])
    if state == "failed":
        store.fail(row["id"], code="PROVIDER_FAILED", detail="x", **fail)
    if state == "succeeded":
        store.succeed(row["id"], result={"text": "ok"})
    store._update(row["id"], created_at=created)
    return row["id"]


class ListCorrectnessTests(unittest.TestCase):
    """Paging, Show removed and the bulk counts are the 0.2.156 rules, checked against every row."""

    @classmethod
    def setUpClass(cls) -> None:
        store._conn.execute("DELETE FROM jobs")
        store._conn.commit()
        base = 1_000_000.0
        cls.ids = []
        for i in range(40):
            state = ("succeeded", "failed", "running", "accepted")[i % 4]
            extra = {}
            if state == "failed" and i % 8 == 1:
                extra = {"recovery_state": "unknown_outcome", "recovery_detail": "held"}
            cls.ids.append(_job(i, state, base + i // 2, **extra))  # pairs share created_at
        store.dismiss_failed()  # removes the plain failures, not the holds
        cls.client = TestClient(api.app, client=("127.0.0.1", 54321))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def expected(self, *, include_removed: bool, state: str | None = None) -> list[str]:
        rows = [dict(r) for r in store._conn.execute("SELECT * FROM jobs")]
        keep = [r for r in rows
                if (include_removed or r["dismissed_at"] is None or r["dismissed_at"] < r["updated_at"])
                and (state is None or r["state"] == state)]
        return [r["id"] for r in sorted(keep, key=lambda r: -r["created_at"])]

    def walk(self, query: str, limit: int) -> list[str]:
        seen, before = [], None
        for _ in range(50):
            body = self.client.get(f"/admin/api/jobs?limit={limit}{query}"
                                   + (f"&before={before}" if before else "")).json()
            seen += [r["id"] for r in body["jobs"]]
            before = body["next_before"]
            if before is None:
                return seen
        self.fail("paging did not end")

    def same_order(self, got: list[str], want: list[str]) -> None:
        """Same rows, newest first; rows sharing a `created_at` may come in either order."""
        created = {r["id"]: r["created_at"] for r in store._conn.execute("SELECT id, created_at FROM jobs")}
        self.assertEqual([created[i] for i in got], [created[i] for i in want])

    def test_one_page_matches_every_row(self) -> None:
        for removed in (False, True):
            for state in (None, "failed", "running"):
                q = ("&include_removed=1" if removed else "") + (f"&state={state}" if state else "")
                with self.subTest(q=q):
                    got = [r["id"] for r in self.client.get(f"/admin/api/jobs?limit=500{q}").json()["jobs"]]
                    want = self.expected(include_removed=removed, state=state)
                    self.assertEqual(set(got), set(want))
                    self.same_order(got, want)

    def test_paging_reaches_every_distinct_created_at(self) -> None:
        # The documented ceiling (admin.py): a page edge inside a shared created_at can skip its twin.
        got = self.walk("&include_removed=1", 7)
        want = self.expected(include_removed=True)
        created = {r["id"]: r["created_at"] for r in store._conn.execute("SELECT id, created_at FROM jobs")}
        self.assertEqual(len(got), len(set(got)), "no row twice")
        self.assertEqual({created[i] for i in got}, {created[i] for i in want})

    def test_bulk_counts(self) -> None:
        bulk = self.client.get("/admin/api/jobs?limit=1").json()["bulk"]
        rows = [dict(r) for r in store._conn.execute("SELECT * FROM jobs")]
        holds = sum(1 for r in rows if r["state"] == "failed" and r["recovery_state"] == "unknown_outcome")
        self.assertGreater(holds, 0)
        self.assertEqual(bulk["needs_recovery"], holds)
        self.assertEqual(bulk["removable_failed"], 0, "every plain failure was removed")
        # A hold newer than the confirmation instant is not counted.
        later = self.client.get("/admin/api/jobs?limit=1").json()["bulk"]
        self.assertEqual(store.needs_recovery_count(later["as_of"] - 10 ** 7), 0)


class HealthUnderLoadTests(unittest.TestCase):
    def test_health_answers_while_slow_jobs_lists_fill_the_thread_pool(self) -> None:
        release, inside = threading.Event(), []
        real = store.list_jobs

        def slow(**kw):
            inside.append(1)
            release.wait(30)
            return real(**kw)

        # anyio's default pool is 40 threads; 45 slow lists hold every one of them. They come from
        # **two** client addresses, because `api._CLIENT_MAX_IN_FLIGHT` (32) now stops one client
        # filling the shared pool on its own — that cap working, and this test is about the pool.
        with patch.object(admin.store, "list_jobs", side_effect=slow), \
                TestClient(api.app, client=("127.0.0.1", 54321)) as first, \
                TestClient(api.app, client=("127.0.0.2", 54322)) as second:
            clients = [first] * 23 + [second] * 22
            lists = [threading.Thread(target=client.get, args=("/admin/api/jobs?limit=200",))
                     for client in clients]
            try:
                for t in lists:
                    t.start()
                deadline = time.monotonic() + 10
                while len(inside) < 40 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(len(inside), 40, "the pool is full of slow lists")
                answer: list = []
                probe = threading.Thread(target=lambda: answer.append(first.get("/health")))
                started = time.monotonic()
                probe.start()
                probe.join(3)
                self.assertTrue(answer, "/health waited behind the slow lists")
                self.assertEqual(answer[0].status_code, 200)
                self.assertLess(time.monotonic() - started, 2.5, "inside the app's probe timeout")
            finally:
                release.set()
                for t in lists:
                    t.join(30)


if __name__ == "__main__":
    unittest.main()
