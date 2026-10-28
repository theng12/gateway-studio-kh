#!/usr/bin/env python3
"""The Create page's two published answers: which voices a model may use, and how a result is saved.

**Both were wrong on the page the owner complained about (2026-10-28).**

* The Voice field showed `studio-hub-local::voice-ba75c0a0aa4f` instead of the name the
  catalogue publishes. `/admin/api/voices` has always answered `display_name`; the page read
  `x.name`, which is not a field, and fell back to the id. A list is also not a voice picker:
  every model was offered every voice in the library, including voices it would refuse.
  **`model_id` is answered by `voices_for_model`**, the same function `/v1/voices` uses, so the
  console and Story Studio cannot answer "which voices match this model" differently.
* "Save…" built its own filename with no extension and downloaded the inline bytes. The route
  already answers `?download=1` with `Content-Disposition: attachment` and the file's own
  suffix; the page simply never asked for it.

Every test runs against a temp profile, so nothing here touches the owner's jobs or settings.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-create-voices-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "create-voices-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import admin, api  # noqa: E402
from gateway.runtime import store  # noqa: E402

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28

SCOPED_ROWS = [
    {"id": "studio-hub-local::voice-0616aec33b41", "display_name": "Christian",
     "provider": "Gateway voice library", "language": "multilingual", "description": ""},
    {"id": "studio-hub-local::voice-ba75c0a0aa4f",
     # The catalogue's own fallback when nothing was published: the raw id in the name field.
     # The row is honest about it — the page is what must not print it as a person's name.
     "display_name": "voice-ba75c0a0aa4f", "provider": "Gateway voice library",
     "language": "multilingual", "description": ""},
]


class _FakeVoiceRegistry:
    def voices(self, provider_id=None):
        return []

    def is_enabled(self, voice) -> bool:
        return True

    def summary(self) -> dict:
        return {"enabled": 0, "total": 0}


class VoiceScope(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        patcher = patch.object(admin, "voice_registry", _FakeVoiceRegistry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_model_id_is_answered_by_the_same_rule_story_studio_gets(self) -> None:
        with patch("gateway.voices.voices_for_model", return_value=list(SCOPED_ROWS)) as scoped:
            body = self.client.get(
                "/admin/api/voices", params={"model_id": "studio-hub::mock-hub/clone-model"}
            ).json()
        scoped.assert_called_once_with("studio-hub::mock-hub/clone-model")
        self.assertEqual([v["id"] for v in body["voices"]],
                         [v["id"] for v in SCOPED_ROWS])
        self.assertEqual(body["matched"], 2)
        self.assertEqual(body["model_id"], "studio-hub::mock-hub/clone-model")
        # The published name travels with the row; it is what the page prints.
        self.assertEqual(body["voices"][0]["display_name"], "Christian")
        self.assertEqual(body["voices"][0]["provider"], "Gateway voice library")
        # **Not the whole library.** The scoped answer is what the picker draws, and the
        # library-wide totals stay labelled as such rather than read as this model's count.
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["enabled"], 0)

    def test_without_a_model_the_library_answer_is_unchanged(self) -> None:
        body = self.client.get("/admin/api/voices").json()
        self.assertNotIn("model_id", body)
        self.assertEqual(body["voices"], [])

    def test_a_query_still_filters_a_scoped_answer(self) -> None:
        with patch("gateway.voices.voices_for_model", return_value=list(SCOPED_ROWS)):
            body = self.client.get("/admin/api/voices",
                                   params={"model_id": "studio-hub::m", "q": "chris"}).json()
        self.assertEqual([v["display_name"] for v in body["voices"]], ["Christian"])
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["total"], 0, "the library total is not the filtered count")


class DownloadRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self.dir = base / "downloads"
        self.dir.mkdir(exist_ok=True)
        row, _ = store.submit(modality="tts", model_id="mock-tts", params={"text": "hi"},
                              idempotency_key=f"dl-{self.id().split('.')[-1]}", provider_id="ui-fixture")
        self.job_id = row["id"]
        path = self.dir / f"{self.job_id}.wav"
        path.write_bytes(WAV)
        self.path = path
        store.succeed(self.job_id, asset_path=str(path), media_type="audio/wav",
                      artifacts=[{"asset_id": "0", "path": str(path),
                                  "media_type": "audio/wav", "bytes": len(WAV)}],
                      result={"text": None, "payload": None})

    def tearDown(self) -> None:
        with store._lock:
            store._conn.execute("DELETE FROM jobs")
            store._conn.commit()

    def test_download_is_an_attachment_with_a_real_name_and_extension(self) -> None:
        response = self.client.get(f"/admin/api/artifacts/{self.job_id}/outputs/0?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, WAV)
        disposition = response.headers["content-disposition"]
        self.assertTrue(disposition.startswith("attachment;"), disposition)
        # The suffix is the **file's own**, not a guess from the URL or the media type.
        self.assertIn(f'filename="tts-{self.job_id[:8]}-0.wav"', disposition)
        self.assertEqual(response.headers["content-type"], "audio/wav")

    def test_without_download_the_bytes_stay_inline_for_a_preview(self) -> None:
        response = self.client.get(f"/admin/api/artifacts/{self.job_id}/outputs/0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, WAV)
        self.assertNotIn("content-disposition", response.headers)

    def test_a_missing_asset_is_a_typed_404(self) -> None:
        body = self.client.get(f"/admin/api/artifacts/{self.job_id}/outputs/9?download=1").json()
        self.assertIn("has no artifact '9'", body["detail"])

    def test_a_pruned_file_is_a_typed_410_and_not_the_old_bytes(self) -> None:
        self.path.unlink()
        response = self.client.get(f"/admin/api/artifacts/{self.job_id}/outputs/0?download=1")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["code"], "ARTIFACT_GONE")


if __name__ == "__main__":
    unittest.main()
