#!/usr/bin/env python3
"""Idempotency across references — a retry may not change which characters are in the picture.

Story builds its idempotency key from prompt/model/ratio/seed and, since 2026-10-26, from the
reference files' own digests. The gateway has to hold the other end of that: the same key with the
same references is a replay, and the same key with a **different** reference set is a conflict — the
earlier job's image is a picture of other people, and handing it back under a 200 would look exactly
like a success.

The uploads are content-addressed, so a re-upload after ``REFERENCE_ASSET_EXPIRED`` returns the same
ids; the retry and the replay then agree at both ends.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-image-refs-replay-")
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
MODEL = "studio-hub::AITRADER/FLUX2-klein-4B-mlx-4bit"


def png(width: int = 1024, body: bytes = b"pixels") -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, width) + b"\x08\x02\x00\x00\x00" + body)


def resolved(references: dict | None) -> tuple[SimpleNamespace, dict]:
    """What a location that advertises `edit` resolves to, with the lane's own reference contract."""
    raw: dict = {"capabilities": ["txt2img", "edit"]}
    if references is not None:
        raw["lane_contract"] = {"references": references}
    return (SimpleNamespace(id="studio-hub-local", kind="studio_hub"),
            {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit", "raw": raw})


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self.addCleanup(image_assets._IMAGES.clear)
        self._resolved = resolved({"min": 1, "max": 2, "max_dimension": 1024})
        patcher = patch.object(api.catalog, "resolve", side_effect=lambda *a, **k: self._resolved)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ids: list[str] = []

    def upload(self, width: int) -> str:
        content = png(width)
        response = self.client.post("/v1/images/source-images", headers=READ,
                                    files={"upload": (f"{width}.png", content, "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    @staticmethod
    def stored_digests(job_id: str) -> list[str]:
        import json
        params = json.loads(api.store.get(job_id)["params"])
        return params.get("_reference_digests") or []

    def submit(self, key: str, references: list[str] | None, prompt: str = "two characters"):
        body: dict = {"prompt": prompt, "model_id": MODEL, "aspect_ratio": "1:1"}
        if references is not None:
            body["reference_asset_ids"] = references
        return self.client.post("/v1/images/generations", headers={**READ, "Idempotency-Key": key},
                                json=body)


class ReplayTests(_Case):
    def test_the_same_key_with_the_same_references_replays_the_job(self) -> None:
        key = f"refs-{time.time()}"
        refs = [self.upload(640), self.upload(800)]
        first = self.submit(key, refs)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.submit(key, refs)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIs(second.json()["idempotent_replay"], True)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.stored_digests(first.json()["id"]), refs)

    def test_a_replay_survives_the_upload_window(self) -> None:
        """The job keeps its own staged copies, so a lost 202 answered late still works."""
        key = f"refs-late-{time.time()}"
        refs = [self.upload(700)]
        self.assertEqual(self.submit(key, refs).status_code, 202)
        image_assets._IMAGES.clear()          # the fifteen minutes have passed
        again = self.submit(key, refs)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIs(again.json()["idempotent_replay"], True)

    def test_the_same_key_with_different_references_is_a_conflict(self) -> None:
        key = f"refs-change-{time.time()}"
        one, other = self.upload(512), self.upload(768)
        self.assertEqual(self.submit(key, [one]).status_code, 202)
        changed = self.submit(key, [other])
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertIn("reference", changed.json()["detail"])
        # And the earlier job is untouched: this is a refusal, not a re-render.
        self.assertEqual(self.stored_digests(api.store.find_by_idempotency_key(key)["id"]), [one])

    def test_dropping_the_references_is_also_a_different_request(self) -> None:
        key = f"refs-drop-{time.time()}"
        self.assertEqual(self.submit(key, [self.upload(600)]).status_code, 202)
        changed = self.submit(key, None)
        self.assertEqual(changed.status_code, 409, changed.text)

    def test_an_expired_reference_is_a_typed_refusal_a_client_can_retry(self) -> None:
        asset = self.upload(900)
        image_assets._IMAGES.clear()
        response = self.submit(f"refs-expired-{time.time()}", [asset])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "REFERENCE_ASSET_EXPIRED",
                         "Story retries exactly this code, once, after re-uploading")


class PublishedWindowTests(_Case):
    def test_over_the_published_maximum_is_refused_with_the_number(self) -> None:
        refs = [self.upload(width) for width in (400, 500, 600)]
        response = self.submit(f"refs-many-{time.time()}", refs)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "INVALID_PARAMS")
        self.assertIn("at most 2", response.json()["detail"])

    def test_an_empty_reference_list_is_not_a_text_to_image_request(self) -> None:
        response = self.submit(f"refs-empty-{time.time()}", [])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("at least 1", response.json()["detail"])

    def test_a_lane_that_states_no_ceiling_is_not_refused_by_a_gateway_number(self) -> None:
        self._resolved = resolved(None)       # an older Hub: advertises `edit`, states no limits
        refs = [self.upload(width) for width in (400, 500, 600, 700)]
        response = self.submit(f"refs-unstated-{time.time()}", refs)
        self.assertEqual(response.status_code, 202, response.text)

    def test_a_plain_text_to_image_request_is_unchanged(self) -> None:
        response = self.submit(f"plain-{time.time()}", None)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.stored_digests(response.json()["id"]), [])


class ReferencesOnAModelThatCannotTakeThemTests(_Case):
    """A pinned lane is resolved without routing, so the lane is asked directly.

    A legacy `studio-hub-<site>::…` id and a cloud id both resolve to the location the caller named.
    Without asking, `_reference_paths` would reach an adapter that never reads them and the caller
    would get a text-to-image render whose envelope is identical to a success.
    """

    def test_a_lane_that_advertises_no_edit_refuses_the_references(self) -> None:
        self._resolved = (SimpleNamespace(id="studio-hub-local", kind="studio_hub"),
                          {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit",
                           "raw": {"capabilities": ["txt2img"]}})
        response = self.submit(f"no-edit-{time.time()}", [self.upload(640)])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["code"], "INVALID_PARAMS")
        self.assertIn("does not take reference images", response.json()["detail"])

    def test_a_cloud_lane_is_refused_the_same_way(self) -> None:
        self._resolved = (SimpleNamespace(id="fal-ai", kind="fal"),
                          {"model_id": "fal-ai/flux/dev", "raw": {"category": "text-to-image"}})
        response = self.submit(f"cloud-refs-{time.time()}", [self.upload(640)])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("does not take reference images", response.json()["detail"])

    def test_a_cloud_lane_that_takes_references_says_whose_limit_it_is(self) -> None:
        self._resolved = (SimpleNamespace(id="fal-ai", kind="fal"),
                          {"model_id": "fal-ai/flux/dev/image-to-image",
                           "raw": {"category": "image-to-image"}})
        response = self.submit(f"cloud-carrier-{time.time()}", [self.upload(640)])
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("cannot send them to that provider's lane", response.json()["detail"],
                      "the model's statement is not contradicted; the gateway's limit is named")


if __name__ == "__main__":
    unittest.main()
