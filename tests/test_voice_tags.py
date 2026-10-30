#!/usr/bin/env python3
"""Voice tags: one free-form label list per voice, editable and published.

The owner, 2026-10-30: *"we should be able to tag voices and show the tag in both
story and gateway and hub too... we are gonna have hundreds of voices."*

What this file pins:

* tags are trimmed, de-duplicated case-insensitively, bounded by the published
  count and length limits, and a refusal names the limit;
* a local voice stores them in its catalogue **and** its folder metadata, and an
  old voice with none reads as an empty list;
* a provider voice stores them in the registry's own state, so a discovery refresh
  cannot erase them;
* `/v1/voices` and `/admin/api/voices` publish them, and the share payload carries
  them as a JSON array in one multipart field;
* removing a tag is a replacement, not a merge.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_TMP = sandbox_env("gateway-voice-tags-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, voice_import  # noqa: E402
from gateway import voices as voice_mod  # noqa: E402
from gateway.providers import registry as provider_registry  # noqa: E402

CATALOG = Path(os.environ["GATEWAY_VOICES_OUT"])
LOCAL_ID = "studio-hub-local::voice-test"


class VoiceTagTests(unittest.TestCase):
    def setUp(self) -> None:
        CATALOG.parent.mkdir(parents=True, exist_ok=True)
        clip = CATALOG.parent / "clip.wav"
        clip.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32)
        CATALOG.write_text(json.dumps({"voices": [{
            "id": "voice-test",
            "display_name": "Test Voice",
            "language": "en",
            "reference_path": str(clip),
            "duration_seconds": 2.0,
            "audio_sha256": "a" * 64,
            "permission_acknowledged": True,
            "license": "cc0",
        }]}), encoding="utf-8")
        # The registry is a singleton over a process-wide path; each test starts
        # with no provider state of its own.
        state = voice_mod.voice_registry()
        state._data["tags"] = {}
        state._data["enabled"] = {}
        state._data["discovered"] = {}
        self.env = patch.dict(os.environ, {"GATEWAY_API_KEY": "voice-tags-test-key", "GATEWAY_HOST": "127.0.0.1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(api.app, client=("127.0.0.1", 41001),
                                 headers={"Authorization": "Bearer voice-tags-test-key"})

    def tearDown(self) -> None:
        self.client.close()

    def stored(self) -> dict:
        return voice_import.voices()[0]

    def settings(self, **body):
        return self.client.post("/admin/api/voices/settings",
                                json={"voice_id": LOCAL_ID, **body})

    def published(self) -> dict:
        response = self.client.get("/v1/voices")
        self.assertEqual(response.status_code, 200, response.text)
        return next(row for row in response.json()["voices"] if row["id"] == LOCAL_ID)

    # -- cleaning ------------------------------------------------------------
    def test_tags_are_trimmed_deduplicated_and_case_insensitive(self) -> None:
        self.assertEqual(
            voice_mod.clean_tags([" Khmer ", "khmer", "Narration", "", "  "]),
            ("Khmer", "Narration"),
        )

    def test_the_limits_are_refused_by_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 12 tags"):
            voice_mod.clean_tags([f"tag-{i}" for i in range(13)])
        with self.assertRaisesRegex(ValueError, "at most 32 characters"):
            voice_mod.clean_tags(["x" * 33])
        with self.assertRaisesRegex(ValueError, "list of short strings"):
            voice_mod.clean_tags("not-a-list")
        with self.assertRaisesRegex(ValueError, "must be text"):
            voice_mod.clean_tags([1])

    # -- a local voice -------------------------------------------------------
    def test_add_stores_in_the_catalogue_folder_and_both_surfaces(self) -> None:
        response = self.settings(tags=["Khmer", " narration ", "Khmer"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tags"], ["Khmer", "narration"])

        entry = self.stored()
        self.assertEqual(entry["tags"], ["Khmer", "narration"])
        metadata = json.loads(
            (voice_import.voice_dir("voice-test") / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["tags"], ["Khmer", "narration"])

        self.assertEqual(self.published()["tags"], ["Khmer", "narration"])
        admin = self.client.get("/admin/api/voices").json()
        row = next(r for r in admin["voices"] if r["id"] == LOCAL_ID)
        self.assertEqual(row["tags"], ["Khmer", "narration"])
        self.assertIn("Khmer", admin["tag_suggestions"])
        self.assertEqual(admin["tag_limits"], {"max_tags": 12, "max_length": 32})

    def test_removing_a_tag_replaces_the_list(self) -> None:
        self.assertEqual(self.settings(tags=["Khmer", "narration"]).status_code, 200)
        response = self.settings(tags=["narration"])
        self.assertEqual(response.json()["tags"], ["narration"])
        self.assertEqual(self.stored()["tags"], ["narration"])
        cleared = self.settings(tags=[])
        self.assertEqual(cleared.json()["tags"], [])
        self.assertEqual(self.published()["tags"], [])

    def test_the_route_refuses_a_limit_and_stores_nothing(self) -> None:
        response = self.settings(tags=[f"tag-{i}" for i in range(13)])
        self.assertEqual(response.status_code, 422)
        self.assertIn("at most 12 tags", response.json()["detail"])
        self.assertNotIn("tags", self.stored())

    def test_an_old_voice_without_tags_reads_as_an_empty_list(self) -> None:
        self.assertEqual(self.published()["tags"], [])
        admin = self.client.get("/admin/api/voices").json()
        row = next(r for r in admin["voices"] if r["id"] == LOCAL_ID)
        self.assertEqual(row["tags"], [])
        # An edit adds to the absent field without needing a migration.
        self.assertEqual(self.settings(tags=["Khmer"]).status_code, 200)
        self.assertEqual(self.published()["tags"], ["Khmer"])

    # -- a provider voice ----------------------------------------------------
    def test_provider_tags_round_trip_through_the_route_and_v1_voices(self) -> None:
        voice_id = "elevenlabs-direct::voice-provider"
        state = voice_mod.voice_registry()
        state._data["discovered"]["elevenlabs-direct"] = {"voices": [{
            "id": voice_id,
            "provider_id": "elevenlabs-direct",
            "provider_voice_id": "voice-provider",
            "display_name": "Provider Voice",
            "source": "account",
        }]}
        state._data["enabled"]["elevenlabs-direct"] = {"voice-provider": True}
        with patch.object(provider_registry.credentials, "is_configured", lambda _pid: True):
            response = self.client.post("/admin/api/voices/settings",
                                        json={"voice_id": voice_id,
                                              "tags": ["Khmer", "khmer", "narration"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["tags"], ["Khmer", "narration"])
            # Stored in the registry's state, keyed by the provider's own id.
            self.assertEqual(state._data["tags"]["elevenlabs-direct"]["voice-provider"],
                             ["Khmer", "narration"])
            rows = self.client.get("/v1/voices").json()["voices"]
            row = next(r for r in rows if r["id"] == voice_id)
            self.assertEqual(row["tags"], ["Khmer", "narration"])

            # The state is a file, so a reload sees the same tags and a removal.
            reloaded = voice_mod.VoiceRegistry(state.path)
            self.assertEqual(reloaded._tags_for("elevenlabs-direct", "voice-provider"),
                             ("Khmer", "narration"))
            cleared = self.client.post("/admin/api/voices/settings",
                                       json={"voice_id": voice_id, "tags": []})
            self.assertEqual(cleared.json()["tags"], [])

    # -- the Hub share payload ----------------------------------------------
    def test_the_share_payload_carries_tags_as_a_json_array(self) -> None:
        self.assertEqual(self.settings(tags=["Khmer", "narration"]).status_code, 200)
        fields = voice_import.push_fields(self.stored())
        self.assertEqual(fields["tags"], json.dumps(["Khmer", "narration"]))
        self.assertEqual(json.loads(fields["tags"]), ["Khmer", "narration"])


if __name__ == "__main__":
    unittest.main()
