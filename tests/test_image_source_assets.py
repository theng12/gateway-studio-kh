#!/usr/bin/env python3
"""`POST /v1/images/source-images` — the upload half of "Reference to Image".

Story Studio already speaks this: it POSTs multipart `upload` to that path, reads `id`,
`media_type`, `byte_size`, `width`, `height` and `expires_at`, and then submits a generation whose
`reference_asset_ids` are the ids it was given. Until this route existed the request was a hard 404
at the first step — the reason the catalogue suppressed reference capability entirely.

The id is the **sha256 of the bytes**, so a re-upload after the 15-minute window is the same id and
a replay with different references is detectable. The body is read as an image here rather than at
the worker: a file a Mac cannot open must fail at the door that can still name the caller's upload.
"""
from __future__ import annotations

import hashlib
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-image-assets-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, image_assets  # noqa: E402

READ = {"Authorization": "Bearer fixture-key"}
STORY_KEYS = {"id", "media_type", "byte_size", "width", "height", "expires_at"}


def png(width: int = 1024, height: int = 1024, body: bytes = b"pixels") -> bytes:
    """A PNG whose header states the size — which is all the upload door reads."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + body)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self.addCleanup(image_assets._IMAGES.clear)

    def upload(self, content: bytes, media_type: str = "image/png", field: str = "upload"):
        return self.client.post(
            "/v1/images/source-images", headers=READ,
            files={field: ("reference.png", content, media_type)},
        )


class UploadContractTests(_Case):
    def test_the_answer_is_exactly_what_story_reads(self) -> None:
        response = self.upload(png(1024, 768))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), STORY_KEYS,
                         "the video route's bytes/expires_in_seconds would throw in Story's parser")
        self.assertEqual((body["media_type"], body["byte_size"]), ("image/png", len(png(1024, 768))))
        self.assertEqual((body["width"], body["height"]), (1024, 768))
        self.assertTrue(body["expires_at"].endswith("Z"), body["expires_at"])

    def test_the_id_is_the_sha256_of_the_bytes(self) -> None:
        content = png(512, 512)
        body = self.upload(content).json()
        self.assertEqual(body["id"], hashlib.sha256(content).hexdigest())
        self.assertEqual(image_assets.get(body["id"])["digest"], body["id"],
                         "the store must answer the digest as well as the id")

    def test_the_same_bytes_uploaded_twice_keep_one_id(self) -> None:
        content = png(640, 480)
        first, second = self.upload(content).json(), self.upload(content).json()
        self.assertEqual(first["id"], second["id"],
                         "Story's retry after REFERENCE_ASSET_EXPIRED re-uploads the same file")
        self.assertEqual(len(image_assets._IMAGES), 1)

    def test_the_file_field_is_accepted_as_the_alias(self) -> None:
        response = self.upload(png(), field="file")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["width"], 1024)

    def test_jpeg_and_webp_are_read_from_their_own_headers(self) -> None:
        jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
                + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 480, 640)
                + b"\x03" + bytes(9))
        webp = (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X" + struct.pack("<I", 10)
                + b"\x00" + (0).to_bytes(3, "little") + (1023).to_bytes(3, "little")
                + (767).to_bytes(3, "little"))
        for content, media_type, size in ((jpeg, "image/jpeg", (640, 480)),
                                          (webp, "image/webp", (1024, 768))):
            with self.subTest(media_type=media_type):
                body = self.upload(content, media_type).json()
                self.assertEqual((body["width"], body["height"]), size)


class RefusalTests(_Case):
    def test_a_body_over_the_cap_is_413(self) -> None:
        too_big = png(body=b"\0" * (image_assets.MAX_SOURCE_IMAGE_BYTES + 1))
        response = self.upload(too_big)
        self.assertEqual(response.status_code, 413, response.text[:200])
        # **Answered one layer earlier since 2026-10-29.** The gateway bounds every body at
        # the door (`BoundedUploadBody`), so an oversize upload is refused before the route
        # runs; the route keeps its own 413 for the boundary case and for a caller that
        # reaches it another way.
        self.assertEqual(response.json()["code"], "UPLOAD_TOO_LARGE")
        self.assertIn("at most", response.json()["detail"])

    def test_a_media_type_outside_the_set_is_named(self) -> None:
        response = self.upload(b"just text", "text/plain")
        self.assertEqual(response.status_code, 400, response.text[:200])
        self.assertEqual(response.json()["code"], "INVALID_PARAMS")
        self.assertIn("text/plain", response.json()["detail"])

    def test_an_undecodable_image_is_refused_rather_than_held(self) -> None:
        response = self.upload(b"not a png at all", "image/png")
        self.assertEqual(response.status_code, 400, response.text[:200])
        self.assertEqual(response.json()["code"], "INVALID_PARAMS")
        self.assertEqual(image_assets._IMAGES, {}, "nothing unreadable may be held")

    def test_an_empty_body_is_refused(self) -> None:
        response = self.upload(b"")
        self.assertEqual(response.status_code, 400, response.text[:200])

    def test_a_png_truncated_before_its_header_is_refused(self) -> None:
        response = self.upload(png()[:20], "image/png")
        self.assertEqual(response.status_code, 400, response.text[:200])

    def test_the_route_needs_the_key(self) -> None:
        response = self.client.post("/v1/images/source-images",
                                    files={"upload": ("a.png", png(), "image/png")})
        self.assertEqual(response.status_code, 401)


class RetentionTests(_Case):
    def test_the_window_is_the_video_routes_fifteen_minutes(self) -> None:
        self.assertEqual(image_assets.TTL_S, 900)

    def test_an_entry_past_the_window_is_dropped(self) -> None:
        body = self.upload(png()).json()
        with image_assets._LOCK:
            content, filename, media_type, stored_at = image_assets._IMAGES[body["id"]]
            image_assets._IMAGES[body["id"]] = (
                content, filename, media_type, time.time() - image_assets.TTL_S - 1)
        self.assertIsNone(image_assets.get(body["id"]),
                          "an expired upload is gone, not answered from stale bytes")


class StagingTests(_Case):
    def test_a_generation_with_no_references_is_staged_unchanged(self) -> None:
        payload = {"prompt": "a cat", "model_id": "m"}
        self.assertEqual(api._stage_image_references(payload), payload)

    def test_an_expired_reference_is_a_typed_refusal_naming_the_id(self) -> None:
        asset = self.upload(png()).json()
        image_assets._IMAGES.clear()
        with self.assertRaises(api.GatewayError) as caught:
            api._stage_image_references({"reference_asset_ids": [asset["id"]]})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (400, "REFERENCE_ASSET_EXPIRED"))
        self.assertIn(asset["id"], caught.exception.detail)

    def test_a_malformed_reference_list_is_refused(self) -> None:
        for bad in ("one-id", [{"id": "x"}], [""]):
            with self.subTest(value=bad), self.assertRaises(api.GatewayError) as caught:
                api._image_reference_ids({"reference_asset_ids": bad})
            self.assertEqual(caught.exception.code, "INVALID_PARAMS")

    def test_the_staged_bytes_are_the_uploaded_bytes(self) -> None:
        content = png(320, 240, b"the actual picture")
        asset = self.upload(content).json()
        staged = api._stage_image_references({"reference_asset_ids": [asset["id"]]})
        path = Path(staged["_reference_paths"][0])
        self.assertEqual(path.read_bytes(), content)
        self.assertIn(f"image-ref-{asset['id']}", path.name)


if __name__ == "__main__":
    unittest.main()
