#!/usr/bin/env python3
"""Third review, finding A: **every door that takes a body has a ceiling**, not just the named ones.

The second review's guard matched two paths by hand. Three more multipart doors existed — an
image door, a video door and a fleet transcription door — and an unauthenticated 120 MB POST to
any of them was spooled to disk before the route answered 401. This suite:

* walks the app's **own route table**, finds every route that declares a `File`/`UploadFile` or a
  `Form`, and fails if a file route is not named in the ceiling table (a route added tomorrow
  cannot slip through by being forgotten);
* proves, over HTTP, that each of the three doors refuses an oversize body **before**
  authenticating it;
* proves the render-blob door is exempt and the small default does not wall off ordinary calls;
* proves the spool budget stops parallel uploads from becoming one file each (finding B).
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import threading
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


def concrete(path: str) -> str:
    """A route template with its parameters filled, as a request path would look."""
    return path.replace("{controller_id}", "c1").replace("{machine_id}", "m1") \
               .replace("{quote_id}", "q1").replace("{sha256}", "a" * 64)


class EveryFileDoorIsNamed(unittest.TestCase):
    def test_the_walk_finds_the_routes_that_take_a_file(self) -> None:
        found = file_and_form_routes()
        self.assertGreaterEqual(len(found), 6, found)
        self.assertTrue(any(takes_file for _p, takes_file, _f in found))

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

    def test_an_ordinary_json_route_is_covered_by_the_default(self) -> None:
        self.assertEqual(api.body_ceiling_for("/v1/images/generations"),
                         (api._BODY_CEILING_DEFAULT, "default"))

    def test_the_render_blob_door_is_exempt_on_purpose(self) -> None:
        # A private render is a whole video, streamed against its own declared size and checked
        # by `render_service`. Capping it at the JSON default would refuse real renders.
        self.assertIsNone(api.body_ceiling_for("/v1/final-renders/blobs/" + "a" * 64))


class OverHttp(unittest.TestCase):
    """Each of the three doors the review named, refused **before** authentication."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)

    def oversize(self, path: str, *, headers: dict | None = None):
        # 120 MB declared, one byte sent: the review's own shape. No Authorization header —
        # the ceiling must answer before the route's 401 would.
        return self.client.post(path, headers={**(headers or {}), "Content-Length": str(120 * MB)},
                                content=b"x")

    def by_path(self, path: str):
        return self.client.post(path, headers={"Content-Length": str(120 * MB)}, content=b"x")

    def test_the_image_door(self) -> None:
        response = self.by_path("/v1/images/source-images")
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    def test_the_video_door(self) -> None:
        response = self.by_path("/v1/videos/source-images")
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    def test_the_fleet_transcription_door(self) -> None:
        response = self.by_path("/admin/api/fleet/controllers/c1/machines/m1/transcribe")
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")

    def test_a_small_upload_still_gets_the_route_answer(self) -> None:
        # The bound must not become the answer for a legitimate call: with a sane body the door
        # answers as it always did — here, 401 for a caller with no credential.
        response = self.client.post(
            "/v1/videos/source-images",
            files={"upload": ("scene.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")})
        self.assertEqual(response.status_code, 401, response.text)

    def test_a_chunked_upload_to_a_named_door_is_cut_at_its_ceiling(self) -> None:
        def produce():
            yield (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"upload\"; "
                   f"filename=\"a.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
            for _ in range(64):
                yield b"x" * 8192
            yield f"\r\n--{BOUNDARY}--\r\n".encode()

        with patch.object(api, "_UPLOAD_CEILINGS",
                          tuple((pattern, 16 * 1024, what)
                                for pattern, _c, what in api._UPLOAD_CEILINGS)):
            response = self.client.post(
                "/v1/images/source-images",
                headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
                content=produce())
        self.assertEqual(response.status_code, 413, response.text)


class TheSpoolBudget(unittest.TestCase):
    """Finding B: parallel uploads must not become one spool file each."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=FRIEND)
        self.addCleanup(self.client.close)
        with api._SPOOL_LOCK:
            api._spool_state["bytes"] = 0
            api._spool_state["in_flight"] = 0

    def chunked(self, chunks: int) -> object:
        def produce():
            for _ in range(chunks):
                yield b"x" * 8192
        return produce()

    def test_more_than_the_budget_held_is_refused_and_released(self) -> None:
        with patch.object(api, "_SPOOL_BUDGET_BYTES", 32 * 1024):
            response = self.client.post("/v1/images/source-images",
                                        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
                                        content=self.chunked(32))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_BUDGET_BUSY")
        self.assertEqual(response.headers.get("retry-after"), "1")
        # Nothing is still held: the refusal released what it had reserved.
        self.assertEqual(api._spool_state["bytes"], 0)
        self.assertEqual(api._spool_state["in_flight"], 0)

    def test_concurrent_uploads_are_capped_before_any_body_is_buffered(self) -> None:
        with patch.object(api, "_SPOOL_MAX_IN_FLIGHT", 1):
            # One upload is already being received (held by this thread's own request), so a
            # second in flight is refused rather than spooled.
            with api._SPOOL_LOCK:
                api._spool_state["in_flight"] = 1
            try:
                response = self.client.post(
                    "/v1/images/source-images",
                    headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
                    content=self.chunked(1))
            finally:
                with api._SPOOL_LOCK:
                    api._spool_state["in_flight"] = 0
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["code"], "UPLOAD_BUDGET_BUSY")

    def test_a_sequential_upload_releases_its_reservation(self) -> None:
        for _ in range(3):
            with patch.object(api, "_SPOOL_BUDGET_BYTES", 64 * 1024):
                response = self.client.post(
                    "/v1/images/source-images",
                    headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
                    content=self.chunked(1))
            self.assertNotEqual(response.status_code, 503, response.text)
            self.assertEqual(api._spool_state["bytes"], 0)
            self.assertEqual(api._spool_state["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
