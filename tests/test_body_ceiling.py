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
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class _PoolCase(unittest.TestCase):
    """Resets the upload pool around every test: a leaked reservation would refuse the next."""

    def setUp(self) -> None:
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})

    def tearDown(self) -> None:
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})


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

    def test_an_upload_is_budgeted_and_a_picture_in_a_chat_turn_is_not(self) -> None:
        # A chat turn with a picture is parsed into memory and dropped; an upload holds bytes for
        # as long as it runs. Budgeting both would make a batch of image chats compete with
        # eight parallel uploads for the same slots.
        self.assertFalse(api.upload_door("/v1/chat/completions")[2])
        self.assertFalse(api.upload_door("/v1/vision/analyze")[2])
        for door in ("/admin/api/create/transcription", "/v1/images/source-images",
                     "/admin/api/content-library/import"):
            with self.subTest(door=door):
                self.assertTrue(api.upload_door(door)[2])

    def test_the_default_is_at_least_the_largest_json_reader(self) -> None:
        # `admin._json_body` accepts 4 MiB on its own; a lower default would refuse a body that
        # route was written to take, before that route ever saw it.
        self.assertGreaterEqual(api._BODY_CEILING_DEFAULT, 4 * MB)

    def test_an_ordinary_json_route_is_covered_by_the_default(self) -> None:
        self.assertEqual(api.body_ceiling_for("/v1/images/generations"),
                         (api._BODY_CEILING_DEFAULT, "default"))

    def test_the_render_blob_door_is_exempt_on_purpose(self) -> None:
        self.assertIsNone(api.body_ceiling_for("/v1/final-renders/blobs/" + "a" * 64))


class TheBudgetFitsRealWork(_PoolCase):
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


class TheWatchdog(_PoolCase):
    """Finding 1: a declared-length upload is watched too, and dribbling is not progress."""

    def middleware(self, declared: str, receive):
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/v1/images/source-images",
                 "headers": [(b"authorization", b"Bearer " + os.environ["GATEWAY_API_KEY"].encode()),
                             (b"content-length", declared.encode())],
                 "client": ("127.0.0.1", 1234)}

        async def drain(_scope: dict, read: Any, _send: Any) -> None:
            # The route reads its body — that is what the watchdog guards. An app that never
            # read would never stall, and the test would pass for the wrong reason.
            while True:
                message = await read()
                if not message.get("more_body"):
                    return

        asyncio.run(api.BoundedUploadBody(drain)(scope, receive, send))
        return [m for m in sent if m["type"] == "http.response.start"]

    def test_a_declared_upload_that_goes_silent_is_dropped(self) -> None:
        # Content-Length 5 MB, one byte sent, then nothing. Before the fix this held its slot
        # and its reservation for ever.
        calls = {"n": 0}

        async def receive() -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "http.request", "body": b"x", "more_body": True}
            await asyncio.sleep(1.0)
            return {"type": "http.request", "body": b"", "more_body": False}

        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})
        with patch.object(api, "_SPOOL_IDLE_TIMEOUT_S", 0.05), \
                patch.object(api, "is_loopback", lambda: True, create=True):
            started = self.middleware(str(5 * MB), receive)
        self.assertEqual(started[0]["status"], 408, started)
        self.assertEqual(api._spool_state["in_flight"], 0)
        self.assertEqual(api._spool_state["bytes"], 0)

    def test_a_dribbling_upload_is_dropped_even_though_it_never_goes_silent(self) -> None:
        # One byte every 30 ms: never a timeout on a single read, but far below the window's
        # minimum progress. Silence is not the only shape of a stalled upload.
        seen = {"n": 0}

        async def receive() -> dict:
            # **Bounded**, so a build without the watchdog ends instead of looping: 60 bytes
            # at 30 ms is well under a second, and the fix must stop it long before that.
            seen["n"] += 1
            await asyncio.sleep(0.03)
            return {"type": "http.request", "body": b"x", "more_body": seen["n"] < 60}

        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})
        with patch.object(api, "_SPOOL_IDLE_TIMEOUT_S", 0.05), \
                patch.object(api, "is_loopback", lambda: True, create=True):
            started = self.middleware(str(5 * MB), receive)
        self.assertEqual(started[0]["status"], 408, started)
        self.assertEqual(api._spool_state["in_flight"], 0)


class EightAtOnce(_PoolCase):
    """Finding 2: the eighth concurrent upload was refused by an off-by-one."""

    def setUp(self) -> None:
        with api._SPOOL_LOCK:
            api._spool_state.update({"bytes": 0, "in_flight": 0, "hosts": {}})

    def test_eight_slots_are_eight_and_the_ninth_is_refused(self) -> None:
        for index in range(api._SPOOL_MAX_IN_FLIGHT):
            self.assertTrue(api._spool_slot(f"host-{index}"), f"slot {index + 1} of 8")
        # The bug: `_spool_take` re-tested the count the slot had just reached.
        self.assertTrue(api._spool_take("host-0", 1 << 20), "the eighth upload must reserve")
        self.assertFalse(api._spool_slot("host-8"), "the ninth is refused")
        api._spool_release("host-0", 1 << 20)
        for index in range(api._SPOOL_MAX_IN_FLIGHT):
            api._spool_release(f"host-{index}", 0)

    def test_eight_parallel_one_megabyte_uploads_all_reach_the_route(self) -> None:
        # Story sends reference images with `Promise.all` and does not retry: a spurious 503
        # fails the scene. Each upload is a real 1 MiB body from the same host.
        barrier = threading.Barrier(8)
        results: list[int] = []
        lock = threading.Lock()

        def one() -> None:
            client = TestClient(api.app, client=FRIEND)
            body = b"\x89PNG\r\n\x1a\n" + b"x" * ((1 * MB) - 8)
            barrier.wait(timeout=10)
            response = client.post("/v1/images/source-images", headers=KEY,
                                   files={"upload": ("scene.png", body, "image/png")})
            with lock:
                results.append(response.status_code)
            client.close()

        threads = [threading.Thread(target=one) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 8, results)
        self.assertNotIn(503, results, f"a spurious 503 fails Story's scene: {results}")


class TheFileSizeIsWhatCounts(unittest.TestCase):
    """Finding 4: a file of exactly the published size was refused for its envelope."""

    def test_a_file_of_exactly_the_limit_reaches_the_route(self) -> None:
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        # A small limit, so a real file can be exactly at it. The multipart envelope pushes
        # the body past the file's size by a few hundred bytes — that was the refusal.
        with patch.object(api, "upload_limit_bytes", lambda model_id: 4096):
            response = client.post(
                "/admin/api/create/transcription", headers=KEY,
                files={"file": ("exact.wav", b"x" * 4096, "audio/wav")},
                data={"model_id": "no-such-model"})
        self.assertNotEqual(response.status_code, 413, response.text[:300])
        self.assertIn(response.status_code, (404, 422), response.text[:300])

    def test_a_file_over_the_limit_is_still_refused(self) -> None:
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        with patch.object(api, "upload_limit_bytes", lambda model_id: 4096):
            response = client.post(
                "/admin/api/create/transcription", headers=KEY,
                files={"file": ("big.wav", b"x" * 8192, "audio/wav")},
                data={"model_id": "no-such-model"})
        self.assertEqual(response.status_code, 413, response.text[:300])


class TheLoopbackRule(unittest.TestCase):
    """Finding 3: the pre-body check uses `require_admin`'s rule, exactly, plus Origin."""

    def setUp(self) -> None:
        # A loopback peer, no credentials at all.
        self.client = TestClient(api.app, client=("127.0.0.1", 5555))
        self.addCleanup(self.client.close)

    def upload(self, headers: dict | None = None):
        body = b"\x89PNG\r\n\x1a\n" + b"x" * 64
        return self.client.post("/v1/images/source-images", headers=headers or {},
                                files={"upload": ("a.png", body, "image/png")})

    def test_a_loopback_peer_is_trusted_only_on_a_loopback_bind(self) -> None:
        # `require_admin` trusts loopback only while the gateway is *bound* to loopback. On a
        # fleet bind (0.0.0.0) this same request is an ordinary unauthenticated caller, and
        # trusting it here read the body before the route answered 401.
        with patch("gateway.config.is_loopback", lambda: False):
            response = self.upload()
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "UNAUTHENTICATED_UPLOAD")

    def test_on_a_loopback_bind_the_owners_own_window_still_works(self) -> None:
        with patch("gateway.config.is_loopback", lambda: True):
            response = self.upload()
        # The route still wants its own credential (`/v1/...` needs the key) — what must not
        # happen is the *door* refusing a loopback peer it is supposed to trust.
        self.assertNotEqual(response.json().get("code"), "UNAUTHENTICATED_UPLOAD", response.text)

    def test_a_cross_site_origin_is_refused_even_with_the_key(self) -> None:
        # A page on another site must not upload with the operator's key or cookie.
        response = self.upload({**KEY, "Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["code"], "CROSS_SITE_UPLOAD")

    def test_this_gateways_own_origin_is_accepted(self) -> None:
        response = self.upload({**KEY, "Origin": "http://testserver"})
        self.assertNotIn(response.status_code, (401, 403), response.text)


class ThePublishedUploadLimit(unittest.TestCase):
    """Finding 4: each transcription model publishes what it accepts, and the page shows it."""

    def test_a_transcription_row_publishes_the_limit_its_door_enforces(self) -> None:
        from gateway import admin
        from gateway.providers import definitions as defs

        definition = SimpleNamespace(id="studio-hub-x", display_name="Studio Hub",
                                     kind=defs.KIND_STUDIO_HUB, data_use="", free_tier=None)
        model = {"model_id": "whisper-x", "display_name": "Whisper X"}
        row = {"id": "studio-hub::whisper-x", "model_id": "whisper-x",
               "display_name": "Whisper X", "chargeable": False}
        with patch.object(admin.registry.models, "enabled_models",
                          lambda modality: [(definition, model)] if modality == "transcription" else []), \
                patch.object(admin.catalog, "rows",
                             lambda modality: [dict(row)] if modality == "transcription" else []), \
                patch.object(api.catalog, "resolve", lambda *a, **k: (definition, model)):
            payload = admin.create_models(SimpleNamespace(cookies={}))
            # Read inside the patch: it is the same function the door refuses with, which is
            # the point — the published number and the enforced number are one call.
            expected = api.upload_limit_bytes("studio-hub::whisper-x")
        published = payload["by_modality"]["transcription"][0]["upload_limit_bytes"]
        self.assertEqual(published, expected)
        self.assertGreaterEqual(published, api._MAX_UPLOAD_BYTES_HUB,
                                "a Hub row must publish the Hub's ceiling, not a cloud 25 MB")

    def test_other_lanes_publish_nothing_they_do_not_have(self) -> None:
        from gateway import admin
        from gateway.providers import definitions as defs

        definition = SimpleNamespace(id="openai-direct", display_name="OpenAI",
                                     kind="openai_compatible", data_use="", free_tier=None)
        model = {"model_id": "mock-chat", "display_name": "Mock Chat"}
        row = {"id": "openai-direct::mock-chat", "model_id": "mock-chat",
               "display_name": "Mock Chat", "chargeable": True}
        with patch.object(admin.registry.models, "enabled_models",
                          lambda modality: [(definition, model)] if modality == "chat" else []), \
                patch.object(admin.catalog, "rows",
                             lambda modality: [dict(row)] if modality == "chat" else []):
            payload = admin.create_models(SimpleNamespace(cookies={}))
        self.assertNotIn("upload_limit_bytes", payload["by_modality"]["chat"][0])


class ReleasedWhenTheBodyIsIn(_PoolCase):
    """Finding 3: the pool is freed when the body has been received, not when the reply is sent."""

    def test_the_pool_is_free_while_the_route_is_still_running(self) -> None:
        # The route has read every byte and is busy answering. Before the fix its slot and its
        # reservation were still held, so the next upload in a back-to-back batch met 503.
        seen: dict = {}

        async def slow_route(_scope: dict, read: Any, _send: Any) -> None:
            await read()                      # the whole body, one message
            with api._SPOOL_LOCK:
                seen["in_flight"] = api._spool_state["in_flight"]
                seen["bytes"] = api._spool_state["bytes"]
            await asyncio.sleep(0.2)          # "answering" — the pool must already be free

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        scope = {"type": "http", "method": "POST", "path": "/alright/anywhere",
                 "headers": [(b"content-length", b"100")], "client": ("host-a", 1)}
        with patch.object(api, "body_ceiling_for", lambda path: (4096, "route")), \
                patch.object(api, "upload_door", lambda path: (4096, "an upload", True)), \
                patch.object(api, "_upload_credentials_ok", lambda scope: True), \
                patch.object(api, "_SPOOL_BUDGET_MIN_BYTES", 64):
            asyncio.run(api.BoundedUploadBody(slow_route)(scope, receive, send))
        self.assertEqual(seen, {"in_flight": 0, "bytes": 0},
                         "the slot must be released when the body is in, not when the reply is out")

    def test_ten_rounds_of_eight_parallel_uploads_never_meet_503(self) -> None:
        # Story sends references with `Promise.all` and does not retry, so a spurious 503 fails
        # a scene. No pause between rounds: the previous round's routes are still answering.
        statuses: list[int] = []
        lock = threading.Lock()
        for _round in range(10):
            barrier = threading.Barrier(8)
            threads = []
            for _index in range(8):
                def one() -> None:
                    client = TestClient(api.app, client=FRIEND)
                    body = b"\x89PNG\r\n\x1a\n" + b"x" * ((1 * MB) - 8)
                    barrier.wait(timeout=10)
                    response = client.post("/v1/images/source-images", headers=KEY,
                                           files={"upload": ("scene.png", body, "image/png")})
                    with lock:
                        statuses.append(response.status_code)
                    client.close()
                thread = threading.Thread(target=one)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(len(statuses), 80, len(statuses))
        self.assertNotIn(503, statuses, f"a spurious 503 fails Story's scene: {statuses}")

    def test_two_large_declared_uploads_back_to_back(self) -> None:
        client = TestClient(api.app, client=FRIEND)
        self.addCleanup(client.close)
        body = b"x" * (90 * MB)
        with patch.object(api, "upload_limit_bytes", lambda model_id: 100 * MB):
            first = client.post("/admin/api/create/transcription", headers=KEY,
                                files={"file": ("a.wav", body, "audio/wav")},
                                data={"model_id": "no-such-model"})
            second = client.post("/admin/api/create/transcription", headers=KEY,
                                 files={"file": ("b.wav", body, "audio/wav")},
                                 data={"model_id": "no-such-model"})
        for response in (first, second):
            # The body was read and the route answered (the model does not exist): what is
            # under test is that the second upload was admitted, not answered 503.
            self.assertNotEqual(response.status_code, 503, response.text[:200])
            self.assertIn(response.status_code, (404, 422), response.text[:200])


class TheSpoolBudget(_PoolCase):
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
