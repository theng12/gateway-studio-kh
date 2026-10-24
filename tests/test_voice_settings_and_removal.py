"""A voice's reference transcript is optional: audio-only voices send no words.

Owner, 2026-10-24: "our transcriber can't understand khmer. so we need to make it optional."
A Khmer voice's Whisper text is wrong, and VoxCPM2 conditions on whatever words it gets
(prompt_text). Off = no transcript, no segments, never transcribed; on = the owner's own words.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-voice-transcript-")

from gateway import admin, voice_import, voices  # noqa: E402
from gateway.adapters import BackendRequest  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402

SEGMENTS = [{"start": 0.0, "end": 1.5, "text": "garbled words"}]


def _clip(path: Path, seconds: float = 1.0, tone: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(bytes([tone % 256, 0]) * int(8000 * seconds))
    return path


class OptionalTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="voice-transcript-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.catalog = self.root / "data" / "voices.json"
        for target, values in (
            (voice_import, {"LIBRARY_DIR": self.root / "data" / "voice-library",
                            "WRITABLE_VOICES_PATH": self.catalog, "VOICES_PATH": self.catalog}),
            (voices, {"WRITABLE_VOICES_PATH": self.catalog, "VOICES_PATH": self.catalog}),
        ):
            for name, value in values.items():
                patcher = patch.object(target, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def _add(self, name: str, *, transcript: bool, tone: int = 1) -> dict:
        result = voice_import.import_paths([_clip(self.root / "picked" / f"{name}.wav", tone=tone)],
                                           transcript=transcript)
        voice_id = result["imported"][0]["id"]
        return next(v for v in voice_import.voices() if v["id"] == voice_id)

    def _set_segments(self, voice_id: str, segments: list) -> None:
        payload = json.loads(self.catalog.read_text())
        for entry in payload["voices"]:
            if entry["id"] == voice_id:
                entry["reference_segments"] = segments
        self.catalog.write_text(json.dumps(payload))

    def _reference(self, voice_id: str) -> dict:
        voice = next(v for v in voices.voice_registry().voices() if v.provider_voice_id == voice_id)
        request = BackendRequest(provider_id="studio-hub-local", model_id="m", modality="tts",
                                 params={"voice_id": voice.id}, values={}, base_url="")
        reference = studio_hub._reference_for(request)
        assert reference is not None
        return reference

    def test_older_voices_keep_their_words(self) -> None:
        entry = self._add("Mark", transcript=True)
        self.assertTrue(entry["include_reference_transcript"])
        self.assertTrue(voice_import.transcript_included({}), "a voice saved before the switch is on")
        self._set_segments(entry["id"], SEGMENTS)
        reference = self._reference(entry["id"])
        self.assertEqual(reference["transcript"], "garbled words")
        self.assertEqual(reference["segments"], SEGMENTS)

    def test_audio_only_voice_sends_no_words_even_with_segments_saved(self) -> None:
        entry = self._add("Yim Mengly", transcript=False)
        self.assertFalse(entry["include_reference_transcript"])
        # A transcription that landed anyway (or one from before the switch) is not sent.
        self._set_segments(entry["id"], SEGMENTS)
        reference = self._reference(entry["id"])
        self.assertEqual((reference["transcript"], reference["segments"]), ("", []))
        self.assertTrue(reference["path"].endswith(".wav"))

    def test_audio_only_voice_is_never_transcribed(self) -> None:
        on = self._add("Mark", transcript=True)
        self._add("Yim Mengly", transcript=False, tone=2)
        started: list = []
        with patch.object(voice_import, "hub_probe", return_value={"running": True, "detail": ""}), \
             patch.object(voice_import.threading, "Thread") as thread:
            thread.side_effect = lambda target, args, daemon: started.append(args[0]) or unittest.mock.Mock()
            voice_import.start_annotation("whisper")
        self.assertEqual([v["id"] for v in started[0]], [on["id"]])

    def test_switch_needs_words_to_turn_on_and_keeps_them_when_off(self) -> None:
        entry = self._add("Yim Mengly", transcript=False)
        with self.assertRaises(ValueError):
            voice_import.update_voice(entry["id"], include=True)
        with self.assertRaises(ValueError):
            voice_import.update_voice(entry["id"], include=True, transcript="   ")
        saved = voice_import.update_voice(entry["id"], include=True, transcript="សួស្តី")
        self.assertEqual(saved, {"id": entry["id"], "language": "", "include_reference_transcript": True,
                                 "reference_transcript": "សួស្តី"})
        self.assertEqual(self._reference(entry["id"])["transcript"], "សួស្តី")
        voice_import.update_voice(entry["id"], include=False)
        self.assertEqual(self._reference(entry["id"])["transcript"], "")
        # Back on without new text: the words saved earlier come back.
        voice_import.update_voice(entry["id"], include=True)
        self.assertEqual(self._reference(entry["id"])["transcript"], "សួស្តី")
        meta = json.loads((voice_import.voice_dir(entry["id"]) / "metadata.json").read_text())
        self.assertTrue(meta["include_reference_transcript"])
        with self.assertRaises(KeyError):
            voice_import.update_voice("voice-000000000000", include=False)

    def test_fleet_share_sends_no_words_and_does_not_demand_segments(self) -> None:
        entry = self._add("Yim Mengly", transcript=False)
        self._set_segments(entry["id"], SEGMENTS)
        entry = next(v for v in voice_import.voices() if v["id"] == entry["id"])
        fields = voice_import.push_fields(entry)
        self.assertEqual((fields["transcript"], fields["transcript_segments"]), ("", "[]"))
        self._set_segments(entry["id"], [])
        entry = next(v for v in voice_import.voices() if v["id"] == entry["id"])
        problems = voice_import.shared_readiness(entry)["problems"]
        self.assertFalse(any("segments" in p for p in problems), problems)

    def test_admin_route_validates_and_reports(self) -> None:
        entry = self._add("Yim Mengly", transcript=False)
        bad = admin.set_voice_settings({"voice_id": entry["id"], "include": "yes"})
        self.assertEqual(bad.status_code, 422)
        missing = admin.set_voice_settings({"voice_id": "voice-000000000000", "include": False})
        self.assertEqual(missing.status_code, 404)
        no_words = admin.set_voice_settings({"voice_id": entry["id"], "include": True})
        self.assertEqual(no_words.status_code, 422)
        ok = admin.set_voice_settings({"voice_id": entry["id"], "include": True, "transcript": "ok"})
        self.assertTrue(ok["include_reference_transcript"])
        self.assertEqual(admin.set_voice_settings({"voice_id": entry["id"], "language": 7}).status_code, 422)

    def test_language_label_is_set_validated_and_carried(self) -> None:
        entry = self._add("Yim Mengly", transcript=False)
        saved = voice_import.update_voice(entry["id"], language="km")
        self.assertEqual(saved["language"], "km")
        self.assertFalse(saved["include_reference_transcript"], "a language change leaves the transcript alone")
        voice = next(v for v in voices.voice_registry().voices() if v.provider_voice_id == entry["id"])
        self.assertEqual(voices.story_row(voice)["language"], "km")
        self.assertEqual(self._reference(entry["id"])["language"], "km")
        for bad in ("k/m", "x" * 41, "<b>"):
            with self.assertRaises(ValueError):
                voice_import.update_voice(entry["id"], language=bad)
        self.assertEqual(voice_import.update_voice(entry["id"], language="")["language"], "")

    def test_add_route_passes_the_choice_and_skips_transcription(self) -> None:
        clip = _clip(self.root / "picked" / "Khmer.wav")
        with patch.object(voice_import, "start_annotation") as annotate:
            result = admin.add_voices({"files": [str(clip)], "include_reference_transcript": False})
        added = next(v for v in voice_import.voices() if v["id"] == result["imported"][0]["id"])
        self.assertFalse(added["include_reference_transcript"])
        # The pass still runs for other voices that need it; it skips this one itself.
        annotate.assert_called_once()


class RemoveProviderVoicesTests(unittest.TestCase):
    """Owner, 2026-10-24: the old ElevenLabs voices gone, keeping only the private ones.

    Removed = hidden in this Gateway (gone from every list and from use); nothing is deleted
    at ElevenLabs, and Restore brings a voice back.
    """

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="voice-hide-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.registry = voices.VoiceRegistry(path=root / "voices_state.json")
        self.registry._data["discovered"]["elevenlabs-direct"] = {"fetched_at": 1, "error": None, "voices": [
            {"id": f"elevenlabs-direct::{vid}", "provider_id": "elevenlabs-direct", "provider_voice_id": vid,
             "display_name": name, "source": "account", "raw": {"category": category}}
            for vid, name, category in (("adam1", "Adam", "premade"), ("jade1", "Jade", "generated"))]}
        for target, name, value in ((voices, "REGISTRY", self.registry),
                                    (voices.registry.credentials, "is_configured", lambda _p: True)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _names(self) -> list[str]:
        return [v.display_name for v in self.registry.voices(provider_id="elevenlabs-direct")]

    def test_remove_hides_and_restore_brings_back(self) -> None:
        self.assertEqual(self._names(), ["Adam", "Jade"])
        with patch.object(admin, "_voice_use", return_value=[]):
            done = admin.remove_provider_voices({"items": ["elevenlabs-direct::adam1"]})
        self.assertEqual(done["removed"], ["elevenlabs-direct::adam1"])
        self.assertEqual(self._names(), ["Jade"])
        self.assertIsNone(voices.resolve("elevenlabs-direct::adam1"), "a removed voice cannot be used")
        # Survives a reload and a fresh discovery of the same voice.
        reloaded = voices.VoiceRegistry(path=self.registry.path)
        reloaded._data["discovered"] = self.registry._data["discovered"]
        self.assertEqual([v.display_name for v in reloaded._merged("elevenlabs-direct")], ["Jade"])
        listed = admin.removed_voices()["provider_voices"]
        self.assertEqual([(r["id"], r["display_name"]) for r in listed], [("elevenlabs-direct::adam1", "Adam")])
        restored = admin.restore_voice({"voice_id": "elevenlabs-direct::adam1"})
        self.assertEqual(restored["display_name"], "Adam")
        self.assertEqual(self._names(), ["Adam", "Jade"])
        self.assertEqual(admin.restore_voice({"voice_id": "elevenlabs-direct::adam1"}).status_code, 404)

    def test_recently_used_voice_needs_confirmation_and_nothing_is_half_done(self) -> None:
        used = [{"caller": "story", "jobs": 3, "last_used_at": 1.0}]
        with patch.object(admin, "_voice_use", side_effect=lambda raw: used if raw == "jade1" else []):
            refused = admin.remove_provider_voices({"items": ["elevenlabs-direct::adam1", "elevenlabs-direct::jade1"]})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("Jade", json.loads(refused.body)["detail"])
            self.assertEqual(self._names(), ["Adam", "Jade"], "a refusal removes nothing")
            done = admin.remove_provider_voices({"items": ["elevenlabs-direct::adam1", "elevenlabs-direct::jade1"],
                                                 "confirm_in_use": True})
        self.assertEqual(self._names(), [])
        self.assertEqual([b["display_name"] for b in done["in_use"]], ["Jade"])

    def test_bad_requests(self) -> None:
        self.assertEqual(admin.remove_provider_voices({"items": []}).status_code, 422)
        self.assertEqual(admin.remove_provider_voices({"items": ["elevenlabs-direct::nope"]}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
