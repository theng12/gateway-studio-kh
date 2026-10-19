#!/usr/bin/env python3
"""0.2.162: the catalogue answers while the thread pool is full, and the Jobs poll no longer fills it.

Live 2026-10-09 16:15 (0.2.161): `GET /v1/models` timed out at 150-180 s while `/health` took 0.03 s.
`sample` showed the event loop idle and ~40 worker threads (anyio's whole pool) in rglob/lstat: the
Jobs window's 5 s poll of `/admin/api/artifacts/kept` ran `retention.usage()`, a walk of ~50,000
artifact and trash files, once per poll, and the walks stacked. Every sync route waited for a thread.
"""
from __future__ import annotations

import inspect
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-catalog-fast-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, retention  # noqa: E402
from gateway.catalog import REBUILD_TTL_S, Catalog  # noqa: E402

CATALOG_PATHS = ("/v1/models", "/v1/chat/models", "/v1/image/models", "/v1/videos/models",
                 "/v1/tts/models", "/v1/music/models", "/v1/transcription/models")
LOCAL = ("127.0.0.1", 54321)


class CatalogRoutesTests(unittest.TestCase):
    def test_catalogue_routes_and_their_auth_need_no_worker_thread(self) -> None:
        routes = {r.path: r for r in api.app.routes if getattr(r, "path", None) in CATALOG_PATHS}
        self.assertEqual(set(routes), set(CATALOG_PATHS))
        for path, route in routes.items():
            with self.subTest(path=path):
                self.assertTrue(inspect.iscoroutinefunction(route.endpoint), "a sync route waits for a shared thread")
                deps = [d.call for d in route.dependant.dependencies]
                self.assertTrue(deps and all(inspect.iscoroutinefunction(c) for c in deps))

    def test_catalogue_answers_while_slow_usage_walks_fill_the_thread_pool(self) -> None:
        release, inside = threading.Event(), []

        def slow() -> dict:
            inside.append(1)
            release.wait(30)
            return {}

        # 45 slow requests to a sync route hold all 40 of anyio's worker threads. They come from
        # **two** client addresses, because `api._CLIENT_MAX_IN_FLIGHT` (32) now stops one client
        # holding the shared pool on its own — that cap working, and this test is about the pool.
        # The hog is the usage walk itself — the slow route that remains after the limits moved off
        # `/admin/api/settings/retention` — so this keeps measuring the thing that actually blocks
        # workers rather than a limits read that no longer walks anything.
        with patch.object(retention, "shared_usage", side_effect=lambda: (slow(), {})[1]), \
                TestClient(api.app, client=LOCAL) as client, \
                TestClient(api.app, client=("127.0.0.2", 54322)) as other:
            clients = [client] * 23 + [other] * 22
            hogs = [threading.Thread(target=c.get, args=("/admin/api/settings/retention/usage",))
                    for c in clients]
            try:
                for t in hogs:
                    t.start()
                deadline = time.monotonic() + 10
                while len(inside) < 40 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(len(inside), 40, "the pool is full")
                for path in ("/v1/models", "/v1/tts/models"):
                    answer: list = []
                    probe = threading.Thread(target=lambda p=path: answer.append(
                        client.get(p, headers={"Authorization": "Bearer any"})))
                    started = time.monotonic()
                    probe.start()
                    probe.join(3)
                    with self.subTest(path=path):
                        self.assertTrue(answer, f"{path} waited behind the full pool")
                        self.assertEqual(answer[0].status_code, 200, answer[0].text[:200])
                        self.assertIn("freshness", answer[0].json())
                        self.assertLess(time.monotonic() - started, 1.0)
            finally:
                release.set()
                for t in hogs:
                    t.join(30)


class RefresherTests(unittest.TestCase):
    def _counted(self, interval: float):
        fresh = Catalog()
        signature = [fresh._signature]
        builds: list[float] = []
        real = fresh.rebuild

        def counted(*, force: bool = False) -> None:
            before = fresh._built_at
            real(force=force)
            if fresh._built_at != before:
                builds.append(time.time())

        self.enterContext(patch.object(fresh, "_current_signature", side_effect=lambda: signature[0]))
        self.enterContext(patch.object(fresh, "rebuild", side_effect=counted))
        fresh.start_refresher(interval=interval)
        time.sleep(0.2)
        return fresh, signature, builds

    def test_reads_skip_the_ttl_rebuild_once_the_refresher_runs(self) -> None:
        fresh, _, builds = self._counted(interval=60)
        settled = len(builds)
        fresh._built_at -= 10 * REBUILD_TTL_S  # long past the TTL: 0.2.161 rebuilt on this read
        for _ in range(20):
            fresh.all_rows(), fresh.freshness(), fresh.rows("tts")
        self.assertEqual(len(builds), settled, "reads stopped doing the TTL rebuild")

    def test_a_change_is_in_the_very_next_read(self) -> None:
        fresh, signature, builds = self._counted(interval=60)
        settled = len(builds)
        signature[0] = (("p", "m", "tts"),)  # a model switched on
        fresh.all_rows()
        self.assertEqual(len(builds), settled + 1)

    def test_the_refresher_rebuilds_a_change_by_itself(self) -> None:
        fresh, signature, builds = self._counted(interval=0.05)
        settled = len(builds)
        signature[0] = (("p", "m", "tts"),)
        deadline = time.monotonic() + 2
        while len(builds) == settled and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(len(builds), settled)

    def test_without_a_refresher_reads_still_rebuild(self) -> None:
        fresh = Catalog()
        with patch.object(fresh, "rebuild") as rebuild:
            fresh.all_rows()
        rebuild.assert_called()


class UsageTests(unittest.TestCase):
    def test_concurrent_callers_share_one_walk(self) -> None:
        walks, gate = [], threading.Event()

        def slow_usage() -> dict:
            walks.append(1)
            gate.wait(5)
            return {"files": len(walks)}

        with patch.object(retention, "usage", side_effect=slow_usage):
            out: list = []
            callers = [threading.Thread(target=lambda: out.append(retention.shared_usage())) for _ in range(10)]
            for t in callers:
                t.start()
            time.sleep(0.3)
            gate.set()
            for t in callers:
                t.join(5)
            self.assertEqual(len(out), 10)
            self.assertLessEqual(len(walks), 2, "ten callers, at most two walks")
            # A caller that arrives after a walk finished gets a fresh one.
            before = len(walks)
            retention.shared_usage()
            self.assertEqual(len(walks), before + 1)

    def test_the_jobs_poll_route_does_not_walk(self) -> None:
        with patch.object(retention, "usage", side_effect=AssertionError("walked")), \
                patch.object(retention, "shared_usage", side_effect=AssertionError("walked")):
            body = TestClient(api.app, client=LOCAL).get("/admin/api/artifacts/kept").json()
        self.assertIn("artifacts", body)
        self.assertNotIn("usage", body)

    def test_usage_walks_the_trash_once(self) -> None:
        with patch.object(retention, "_trash_files", wraps=retention._trash_files) as trash:
            retention.usage()
        self.assertEqual(trash.call_count, 1)


class JobsWindowPollTests(unittest.TestCase):
    def test_the_jobs_tick_skips_while_a_read_is_out_and_backs_off(self) -> None:
        source = (Path(__file__).resolve().parent.parent / "menubar" / "JobsWindow.swift").read_text()
        self.assertIn("if quiet && (readsInFlight > 0 || Date() < nextQuietLoad) { return }", source)
        self.assertEqual(source.count("self.readFinished(startedAt: startedAt)"), 2)


if __name__ == "__main__":
    unittest.main()
