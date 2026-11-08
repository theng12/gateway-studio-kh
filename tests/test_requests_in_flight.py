"""`/health` publishes HTTP requests in flight by kind (`api.RequestsInFlight`).

An upgrade guard reads it to judge real work rather than open sockets (Developer 2026-11-08):
status polls never block, a write, an upload or an artifact transfer does, and a reading that is
absent must not look like zero — so every kind is always published.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-requests-in-flight-")

from gateway import api  # noqa: E402

K = api.RequestsInFlight.kind


class Kinds(unittest.TestCase):
    def test_polls_are_status(self) -> None:
        for path in ("/health", "/api/version", "/admin/api/fleet", "/v1/models"):
            self.assertEqual(K("GET", path, ""), "status", path)

    def test_file_reads_are_transfers(self) -> None:
        for path in ("/v1/jobs/j1/outputs/a1", "/v1/final-renders/r1/output",
                     "/admin/api/backup/nightly/download", "/admin/api/content-library/d1/archive",
                     "/v1/content/archives/rel1"):
            self.assertEqual(K("GET", path, ""), "artifact_transfer", path)

    def test_writes_and_uploads(self) -> None:
        self.assertEqual(K("POST", "/v1/videos/source-images", "multipart/form-data; boundary=x"), "upload")
        self.assertEqual(K("POST", "/v1/jobs", "application/json"), "write")
        self.assertEqual(K("PUT", "/admin/api/settings/retention", ""), "write")
        self.assertEqual(K("DELETE", "/admin/api/x", ""), "write")


class Counting(unittest.TestCase):
    def _run(self, method: str, path: str, *, fail: bool = False) -> dict:
        seen: dict = {}

        async def inner(scope, receive, send):
            seen.update(api.requests_in_flight())  # read while the request is still being answered
            if fail:
                raise RuntimeError("handler failed")

        wrapper = api.RequestsInFlight(inner)
        scope = {"type": "http", "method": method, "path": path, "headers": []}
        try:
            asyncio.run(wrapper(scope, None, None))
        except RuntimeError:
            pass
        return seen

    def test_counted_while_answered_and_released_after(self) -> None:
        before = api.requests_in_flight()
        during = self._run("POST", "/v1/jobs")
        self.assertEqual(during["write"], before["write"] + 1)
        self.assertEqual(api.requests_in_flight(), before)

    def test_released_when_the_handler_raises(self) -> None:
        before = api.requests_in_flight()
        during = self._run("GET", "/v1/jobs/j/outputs/a", fail=True)
        self.assertEqual(during["artifact_transfer"], before["artifact_transfer"] + 1)
        self.assertEqual(api.requests_in_flight(), before)

    def test_the_app_is_wrapped_outermost(self) -> None:
        self.assertIs(api.app.user_middleware[0].cls, api.RequestsInFlight)


class Published(unittest.TestCase):
    def test_health_carries_every_kind(self) -> None:
        published = api.health()["requests_in_flight"]
        self.assertEqual(set(published), set(api.RequestsInFlight.KINDS))
        self.assertTrue(all(isinstance(v, int) for v in published.values()))


if __name__ == "__main__":
    unittest.main()
