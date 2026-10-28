#!/usr/bin/env python3
"""The body guard, third pass: **every door's real ceiling, credentials before the body, a budget
that fits a real upload.**

Three reviews found three different ways for the same middleware to be wrong. The tests here are
the three shapes that must never come back:

* **A legitimate large body reaches its route.** A 4.26 MiB `.sspack`, a 5 MB image in a chat turn
  and a 10 MB vision image — each the size real traffic is, each asserted to get the *route's*
  answer rather than a 413 from a ceiling nobody had thought about (2026-10-29c findings 1, 2).
* **An unauthenticated caller never has a body read.** The door answers 401 from the headers,
  before FastAPI's multipart parse would have spooled it (finding 3).
* **The budget always fits one real upload, and a burst cannot all be in flight** (findings 3, 4,
  5): declared lengths count against the same budget as chunked ones, one host cannot take it
  all, and a sender that goes quiet loses its slot.

Every transport-level test goes through HTTP; the idle timer, which needs a `receive` that never
answers, is driven at the middleware's own ASGI boundary.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-ceiling-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"), "GATEWAY_VOICES_OUT": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "ceiling-key",
})

from fastapi import UploadFile  # noqa: E402
from fastapi.params import File as FileParam, Form as FormParam  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api  # noqa: E402

FRIEND = ("100.100.1.1", 5555)
ORIGIN = {"Origin": "http://testserver"}
KEY = {"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}
BOUNDARY = "----ceiling"
MB = 1024 * 1024


def _iter_routes():
    for route in api.app.routes:
        inner = getattr(route, "original_router", None) or route
        for sub in getattr(inner, "routes", [inner]):
            path = getattr(sub, "path", None)
            endpoint = getattr(sub, "endpoint", None)
            if path and endpoint:
                yield sub, path, endpoint


def file_and_form_routes():
    """`(path, takes_file, takes_form)` for every route that declares either."""
    rows = []
    for _sub, path, endpoint in _iter_routes():
        try:
            signature = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue
        takes_file = takes_form = False
        for parameter in signature.parameters.values():
            info = parameter.default
            annotation = str(parameter.annotation)
            # `file: UploadFile = File(...)` arrives as an `UploadFile | None` annotation with a
            # `File` default; a plain form field is a `Form` default. `File` subclasses `Form`,
            # so it has to be checked first.
            if isinstance(info, FileParam) or "UploadFile" in annotation:
                takes_file = True
            elif isinstance(info, FormParam):
                takes_form = True
        if takes_file or takes_form:
            rows.append((path, takes_file, takes_form))
    return rows


def stream_reading_routes():
    """Routes whose handler reads the request body itself — the shape finding 1 missed.

    A Content Pack import does `async for chunk in request.stream()`, so no `File`/`Form`
    parameter exists to find it by, and a 2 MiB default refused a real 4.26 MiB `.sspack`.
    """
    rows = []
    for _sub, path, endpoint in _iter_routes():
        try:
            source = inspect.getsource(endpoint)
        except (OSError, TypeError):
            continue
        if "request.stream()" in source or "request.body()" in source:
            rows.append(path)
    return rows


#: Routes that read their own body **and bound it themselves**. The walk allows these because
#: the reader's own limit is at or below the default ceiling — asserted, so lowering the default
#: below one of them fails here rather than in production.
_SELF_BOUNDED = {
    # A URL-encoded password form; it stops reading at a few hundred bytes by its own shape.
    "/admin/login": 4 * MB,
    # `admin._json_body` reads the stream with its own 4 MiB bound.
    "/admin/api/content-shelf/remove": 4 * MB,
    "/admin/api/content-shelf/retire": 4 * MB,
    "/admin/api/content-shelf/unretire": 4 * MB,
}


def concrete(path: str) -> str:
    """A route template with its parameters filled, as a request path would look."""
    return path.replace("{controller_id}", "c1").replace("{machine_id}", "m1") \
               .replace("{quote_id}", "q1").replace("{sha256}", "a" * 64)


class EveryDoorIsNamed(unittest.TestCase):
    def test_the_walk_finds_the_routes_that_take_a_file(self) -> None:
        found = file_and_form_routes()
        self.assertGreaterEqual(len(found), 6, found)
        self.assertTrue(any(takes_file for _p, takes_file, _f in found))

    def test_the_walk_finds_the_routes_that_read_their_own_stream(self) -> None:
        found = stream_reading_routes()
        self.assertIn("/admin/api/content-library/import", found)
        self.assertIn("/admin/api/content-library/import-and-publish", found)

    def test_every_route_that_takes_a_file_names_its_own_ceiling(self) -> None:
        # A route that accepts an upload must be in the table. "Covered by the default" is not
        # good enough for a file door: it would mean nobody decided what it takes.
        for path, takes_file, _takes_form in file_and_form_routes():
            with self.subTest(path=path):
                resolved = api.body_ceiling_for(concrete(path))
                self.assertIsNotNone(resolved, f"{path} is exempt from any bound")
                _ceiling, source = resolved
                if takes_file:
                    self.assertEqual(source, "route",
                                     f"{path} takes a file but has no ceiling of its own")
                else:
                    self.assertNotEqual(source, "exempt")

    def test_every_route_that_reads_its_own_stream_names_its_ceiling(self) -> None:
        # **The gap finding 1 fell through**: a Content Pack import reads `request.stream()`
        # itself, so there is no `File` parameter to find it by, and nobody had given it a
        # ceiling. A route that reads its own body must be named — unless its own reader bounds
        # it at or below the default, which is asserted, not assumed.
        for path in stream_reading_routes():
            with self.subTest(path=path):
                resolved = api.body_ceiling_for(concrete(path))
                if resolved is None:
                    self.assertTrue(path.startswith("/v1/final-renders/blobs/"),
                                    f"{path} reads a body but is exempt")
                    continue
                _ceiling, source = resolved
                if source == "route":
                    continue
                self.assertIn(path, _SELF_BOUNDED,
                              f"{path} reads request.stream()/body() but relies on the default")
                self.assertLessEqual(_SELF_BOUNDED[path], api._BODY_CEILING_DEFAULT,
                                     f"{path} bounds itself above the door's default")

    def test_the_pack_door_takes_a_real_archive(self) -> None:
        from gateway import content_packs

        resolved = api.body_ceiling_for("/admin/api/content-library/import")
        self.assertEqual(resolved[0], api._MAX_PACK_BODY)
        self.assertGreaterEqual(resolved[0], content_packs.MAX_ARCHIVE,
                                "the door must admit the largest archive the route accepts")

    def test_a_chat_turn_fits_the_largest_image_the_gateway_publishes(self) -> None:
        ceiling, source = api.body_ceiling_for("/v1/chat/completions")
        self.assertEqual(source, "route")
        # One 12 MiB image, base64 (×4/3), must fit with room to spare.
        self.assertGreater(ceiling, api.VISION_MAX_IMAGE_BYTES * 4 / 3)

    def test_the_vision_door_fits_the_size_it_advertises(self) -> None:
        ceiling, source = api.body_ceiling_for("/v1/vision/analyze")
        self.assertEqual(source, "route")
        self.assertGreater(ceiling, api.VISION_MAX_IMAGE_BYTES * 4 / 3,
                           "it publishes a 12 MB image limit and must admit one")

    def test_the_default_is_at_least_the_largest_json_reader(self) -> None:
        # `admin._json_body` accepts 4 MiB on its own; a lower default would refuse a body that
        # route was written to take, before that route ever saw it.
        self.assertGreaterEqual(api._BODY_CEILING_DEFAULT, 4 * MB)

    def test_an_ordinary_json_route_is_covered_by_the_default(self) -> None:
        self.assertEqual(api.body_ceiling_for("/v1/images/generations"),
                         (api._BODY_CEILING_DEFAULT, "default"))

    def test_the_render_blob_door_is_exempt_on_purpose(self) -> None:
        self.assertIsNone(api.body_ceiling_for("/v1/final-renders/blobs/" + "a" * 64))


class TheBudgetFitsRealWork(unittest.TestCase):
    def test_one_upload_of_the_largest_ceiling_always_fits(self) -> None:
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})
        self.assertGreaterEqual(api._SPOOL_BUDGET_BYTES, api._LARGEST_CEILING)
        self.assertGreaterEqual(api._SPOOL_PER_HOST_BYTES, api._LARGEST_CEILING,
                                "one host must be able to send one maximum upload")
        self.assertTrue(api._spool_slot("host-a"))
        self.assertTrue(api._spool_take("host-a", api._LARGEST_CEILING))
        api._spool_release("host-a", api._LARGEST_CEILING)

    def test_one_host_cannot_take_more_than_its_share(self) -> None:
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})
        api._spool_slot("host-a")
        api._spool_slot("host-a")
        self.assertTrue(api._spool_take("host-a", api._SPOOL_PER_HOST_BYTES))
        self.assertFalse(api._spool_take("host-a", 1), "the host's share is spent")
        self.assertTrue(api._spool_take("host-b", 1), "another host is unaffected")
        api._spool_release("host-a", api._SPOOL_PER_HOST_BYTES)
        api._spool_release("host-b", 1)


class RealTraffic(unittest.TestCase):
    """**The regressions of finding 1 and 2: a real large body must reach its route.**"""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)

    def test_a_real_content_pack_reaches_the_import_route(self) -> None:
        # 4.26 MiB, the size of the `.sspack` the owner tried: the archive is not a valid pack,
        # so the *route* answers 422 — which is the point. Under the old 2 MiB default this was
        # a 413 at the door and the route never ran.
        body = b"PK\x03\x04" + os.urandom(int(4.26 * MB))
        response = self.client.post("/admin/api/content-library/import",
                                    headers={**KEY, "Content-Type": "application/octet-stream"},
                                    content=body)
        self.assertEqual(response.status_code, 422, response.text[:300])

    def test_a_five_megabyte_image_reaches_a_chat_turn(self) -> None:
        image = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + os.urandom(5 * MB)).decode()
        response = self.client.post("/v1/chat/completions", headers=KEY, json={
            "model": "no-such-chat-model",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": image}}]}],
        })
        self.assertNotIn(response.status_code, (413, 503), response.text[:300])
        self.assertEqual(response.status_code, 404, response.text[:300])

    def test_a_ten_megabyte_vision_image_reaches_the_route(self) -> None:
        image = base64.b64encode(b"\xff\xd8\xff\xe0" + os.urandom(10 * MB)).decode()
        response = self.client.post("/v1/vision/analyze", headers=KEY, json={
            "tasks": ["depth"], "image": {"b64": image, "mime": "image/jpeg"},
        })
        # The route runs (and says it has no vision Mac in this sandbox); what matters is that
        # a 10 MB image is not refused by the door.
        self.assertNotIn(response.status_code, (413, 401), response.text[:300])
        self.assertIn("vision", response.text.lower())


class CredentialsBeforeTheBody(unittest.TestCase):
    """Finding 3: no credential, no body read — answered from the headers."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)

    def test_an_unauthenticated_declared_upload_is_refused_without_a_read(self) -> None:
        # A 5 MB body is declared and only one byte is sent. If the door tried to read it the
        # request would stall; it answers from the headers instead.
        started = time.monotonic()
        response = self.client.post(
            "/admin/api/create/transcription",
            headers={"Content-Length": str(5 * MB)}, content=b"x", timeout=10)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "UNAUTHENTICATED_UPLOAD")
        self.assertLess(elapsed, 3.0, "the answer must not wait on the body")

    def test_an_unauthenticated_chunked_upload_is_refused_too(self) -> None:
        def produce():
            yield b"x" * 8192
        response = self.client.post(
            "/v1/images/source-images",
            headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
            content=produce(), timeout=10)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "UNAUTHENTICATED_UPLOAD")

    def test_the_key_still_opens_the_door(self) -> None:
        response = self.client.post(
            "/v1/images/source-images", headers=KEY,
            files={"upload": ("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")})
        self.assertNotIn(response.status_code, (401, 413), response.text[:200])

    def test_an_oversize_body_is_refused_even_without_credentials(self) -> None:
        # Ceiling first: the answer is "too big" without reading, and it does not depend on
        # having authenticated — no information is leaked by saying so.
        response = self.client.post("/v1/images/source-images",
                                    headers={"Content-Length": str(120 * MB)}, content=b"x",
                                    timeout=10)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    def test_a_declared_upload_counts_against_the_budget(self) -> None:
        # Finding 3, second half: a declared length is reserved like a chunked body, so a burst
        # of large uploads cannot all be in flight.
        with patch.object(api, "_SPOOL_PER_HOST_BYTES", 1 * MB), \
                patch.object(api, "_SPOOL_BUDGET_BYTES", 2 * MB), \
                patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 1024):
            response = self.client.post(
                "/v1/images/source-images", headers={**KEY, "Content-Length": str(5 * MB)},
                content=b"x", timeout=10)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_BUDGET_BUSY")


class TheSpoolBudget(unittest.TestCase):
    """Findings B/4/5: parallel and dribbling uploads cannot hold the process."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})

    def chunked(self, chunks: int) -> object:
        def produce():
            for _ in range(chunks):
                yield b"x" * 8192
        return produce()

    def post_chunked(self, chunks: int):
        return self.client.post(
            "/v1/images/source-images",
            headers={**KEY, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
            content=self.chunked(chunks))

    def test_more_than_the_budget_held_is_refused_and_released(self) -> None:
        # The 1 MiB "is this an upload" threshold is lowered too: the budget is what is under
        # test, and a test body should cross it after a few kilobytes, not after a megabyte.
        with patch.object(api, "_SPOOL_BUDGET_BYTES", 32 * 1024), \
                patch.object(api, "_SPOOL_PER_HOST_BYTES", 32 * 1024), \
                patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 1024):
            response = self.post_chunked(32)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_BUDGET_BUSY")
        self.assertEqual(response.headers.get("retry-after"), "1")
        self.assertEqual(api._spool_state["bytes"], 0)
        self.assertEqual(api._spool_state["in_flight"], 0)
        self.assertEqual(api._spool_state["hosts"], {})

    def test_concurrent_uploads_are_capped_before_any_body_is_buffered(self) -> None:
        with patch.object(api, "_SPOOL_MAX_IN_FLIGHT", 1), \
                patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 1024):
            with api._SPOOL_LOCK:
                api._spool_state["in_flight"] = 1
            try:
                response = self.post_chunked(1)
            finally:
                with api._SPOOL_LOCK:
                    api._spool_state["in_flight"] = 0
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_BUDGET_BUSY")

    def test_a_sequential_upload_releases_its_reservation(self) -> None:
        for _ in range(3):
            with patch.object(api, "_SPOOL_BUDGET_BYTES", 64 * 1024), \
                    patch.object(api, "_SPOOL_PER_HOST_BYTES", 64 * 1024), \
                    patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 1024):
                response = self.post_chunked(1)
            self.assertNotEqual(response.status_code, 503, response.text)
            self.assertEqual(api._spool_state["bytes"], 0)
            self.assertEqual(api._spool_state["in_flight"], 0)

    def test_a_sender_that_goes_quiet_loses_its_slot(self) -> None:
        # The idle timer is the middleware's own boundary: a `receive` that never answers is
        # not something an HTTP client can be asked to produce on demand.
        middleware = api.BoundedUploadBody(lambda scope, receive, send: None)
        sent: list[dict] = []

        async def never() -> dict:
            await asyncio.sleep(1.0)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/images/source-images",
                 "headers": [(b"authorization",
                              b"Bearer " + os.environ["GATEWAY_API_KEY"].encode()),
                             (b"transfer-encoding", b"chunked")],
                 "client": ("host-a", 1234)}
        with patch.object(api, "_SPOOL_IDLE_TIMEOUT_S", 0.05), \
                patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 1):
            asyncio.run(middleware(scope, never, send))
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        self.assertEqual(status, 408)
        self.assertEqual(api._spool_state["in_flight"], 0, "the slot is released")
        self.assertEqual(api._spool_state["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
