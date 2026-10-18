"""Adding voices one file at a time, without duplicates nobody asked for.

2026-10-11: the picker took folders only, so adding Christopher meant adding the whole
"Voices for Gateway KH" folder, and every different clip under a used name (Chris, Christian,
Leo) became a second voice that looked exactly like the first. Now: files can be picked, the
same recording is skipped and said so, a taken name is a question (keep both / replace / skip),
Replace keeps the voice id, and Remove/Restore move folders instead of deleting them.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-voice-duplicates-")

from gateway import voice_import, voices  # noqa: E402
from gateway.store import JobStore  # noqa: E402


def _clip(path: Path, seconds: float, tone: int = 1) -> Path:
    """A silent-ish WAV whose bytes differ by `seconds` and `tone`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(bytes([tone % 256, 0]) * int(8000 * seconds))
    return path


class DuplicateHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="voice-dupes-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.library = self.root / "data" / "voice-library"
        self.catalog = self.root / "data" / "voices.json"
        for target, values in (
            (voice_import, {"LIBRARY_DIR": self.library, "WRITABLE_VOICES_PATH": self.catalog,
                            "VOICES_PATH": self.catalog}),
            (voices, {"WRITABLE_VOICES_PATH": self.catalog, "VOICES_PATH": self.catalog}),
        ):
            for name, value in values.items():
                patcher = patch.object(target, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.picked = self.root / "picked"

    def _by_name(self, name: str) -> list[dict]:
        return [v for v in voice_import.voices() if v["display_name"] == name]

    def test_individual_files_can_be_picked(self) -> None:
        a = _clip(self.picked / "Christopher - Gentle.wav", 2.0)
        b = _clip(self.picked / "Leo.wav", 1.0, tone=2)
        (self.picked / "notes.txt").write_text("x")
        result = voice_import.import_paths([a, b, self.picked / "notes.txt"])
        self.assertEqual(sorted(v["display_name"] for v in result["imported"]), ["Christopher", "Leo"])
        self.assertEqual([s["name"] for s in result["skipped"]], ["notes.txt"])
        chris = self._by_name("Christopher")[0]
        self.assertAlmostEqual(chris["duration_seconds"], 2.0, places=2)
        self.assertLess(abs(chris["imported_at"] - time.time()), 60)
        # and a folder is still read the way it always was
        _clip(self.picked / "more" / "Vincent.wav", 0.5, tone=3)
        self.assertEqual([v["display_name"] for v in voice_import.import_paths([self.picked / "more"])["imported"]],
                         ["Vincent"])

    def test_the_same_recording_is_skipped_and_left_alone(self) -> None:
        clip = _clip(self.picked / "Chris.wav", 1.0)
        voice_import.import_paths([clip])
        original = self._by_name("Chris")[0]
        payload = json.loads(self.catalog.read_text())
        payload["voices"][0].update({"language": "en", "gender": "m"})
        self.catalog.write_text(json.dumps(payload))

        renamed = shutil.copy(clip, self.picked / "Chris - Soft - 12s.wav")
        result = voice_import.import_paths([Path(renamed)])
        self.assertEqual(result["imported"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertTrue(result["skipped"][0]["duplicate"])
        self.assertIn("same recording", result["skipped"][0]["reason"])
        self.assertEqual(result["skipped"][0]["id"], original["id"])
        # the voice it already was is untouched: no reset of language or gender
        after = voice_import.voices()
        self.assertEqual(len(after), 1)
        self.assertEqual((after[0]["language"], after[0]["gender"]), ("en", "m"))

    def test_a_taken_name_is_asked_about_not_added(self) -> None:
        voice_import.import_paths([_clip(self.picked / "old" / "Chris.wav", 1.0)])
        newer = _clip(self.picked / "new" / "Chris.wav", 3.0, tone=9)
        result = voice_import.import_paths([newer])
        self.assertEqual(result["imported"], [])
        self.assertEqual(len(self._by_name("Chris")), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["message"], "A voice named “Chris” already exists.")
        self.assertEqual(conflict["source"], str(newer))
        self.assertEqual(conflict["suggested_name"], "Chris 2")
        self.assertAlmostEqual(conflict["duration_seconds"], 3.0, places=2)
        self.assertAlmostEqual(conflict["existing"][0]["duration_seconds"], 1.0, places=2)
        self.assertIsNotNone(conflict["existing"][0]["imported_at"])

        skipped = voice_import.import_paths([newer], {str(newer): "skip"})
        self.assertEqual((skipped["imported"], skipped["conflicts"]), ([], []))
        self.assertIn("you chose Skip", skipped["skipped"][0]["reason"])

        both = voice_import.import_paths([newer], {str(newer): "keep_both"})
        self.assertEqual([v["display_name"] for v in both["imported"]], ["Chris 2"])
        self.assertEqual(len(voice_import.voices()), 2)

    def test_replace_keeps_the_id_and_backs_up_the_old_clip(self) -> None:
        old_clip = _clip(self.picked / "old" / "Chris.wav", 1.0)
        voice_import.import_paths([old_clip])
        payload = json.loads(self.catalog.read_text())
        payload["voices"][0].update({
            "language": "en", "gender": "m", "permission_acknowledged": True, "license": "self-owned",
            "reference_segments": [{"start": 0.0, "end": 1.0, "text": "old"}],
        })
        self.catalog.write_text(json.dumps(payload))
        voice_id = payload["voices"][0]["id"]

        newer = _clip(self.picked / "new" / "Chris.wav", 3.0, tone=9)
        result = voice_import.import_paths([newer], {str(newer): f"replace:{voice_id}"})
        self.assertEqual([(v["id"], v["replaced"]) for v in result["imported"]], [(voice_id, True)])
        [entry] = voice_import.voices()
        self.assertEqual(entry["id"], voice_id)                        # channels keep working
        self.assertEqual(entry["audio_sha256"], voice_import.clip_digest(newer))
        self.assertIsNone(entry["reference_segments"])                 # re-transcribed
        self.assertEqual((entry["language"], entry["gender"]), ("en", "m"))
        self.assertFalse(entry["permission_acknowledged"])             # granted for the old recording
        self.assertNotIn("license", entry)
        self.assertTrue(any("private again" in n for n in result["notes"]), result["notes"])
        self.assertAlmostEqual(entry["duration_seconds"], 3.0, places=2)
        # the old clip is kept, not overwritten
        [backup] = list(voice_import.backup_dir().iterdir())
        self.assertTrue(backup.name.startswith(voice_id) and backup.name.endswith("--replaced"))
        self.assertEqual((backup / "reference.wav").read_bytes(), old_clip.read_bytes())
        self.assertEqual(json.loads((backup / "entry.json").read_text())["reference_segments"][0]["text"], "old")
        # the old recording, added again, cannot land on the id the replacement now holds
        again = voice_import.import_paths([old_clip], {str(old_clip): "keep_both"})
        self.assertNotEqual(again["imported"][0]["id"], voice_id)
        self.assertEqual(len(voice_import.voices()), 2)

    def test_old_voice_backups_retire_into_the_gateway_trash_but_the_library_stays(self) -> None:
        from gateway import retention
        held = self.root / "data" / "voice-library" / "kept-voice"
        held.mkdir(parents=True)
        (held / "reference.wav").write_bytes(b"live")
        root = voice_import.backup_dir()
        old = root / "voice-old--20260101T000000--removed"
        fresh = root / "voice-new--20260102T000000--removed"
        old.mkdir(parents=True)
        (old / "reference.wav").write_bytes(b"old")
        fresh.mkdir(parents=True)
        moment = time.time()
        for path, when in ((old, moment - 10 * 86400), (fresh, moment)):
            os.utime(path, (when, when))
        result = voice_import.sweep_backups(moment, days=3)
        self.assertEqual((result["retired"], result["failed"]), (1, 0))
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        [retired] = list(retention.TRASH_DIR.rglob(old.name))
        self.assertEqual((retired / "reference.wav").read_bytes(), b"old")
        self.assertEqual((held / "reference.wav").read_bytes(), b"live")

    def test_replace_names_only_a_voice_of_that_name(self) -> None:
        voice_import.import_paths([_clip(self.picked / "Chris.wav", 1.0)])
        voice_import.import_paths([_clip(self.picked / "Leo.wav", 1.0, tone=4)])
        leo = self._by_name("Leo")[0]["id"]
        newer = _clip(self.picked / "new" / "Chris.wav", 2.0, tone=7)
        result = voice_import.import_paths([newer], {str(newer): f"replace:{leo}"})
        self.assertEqual(result["imported"], [])
        self.assertIn("no voice", result["skipped"][0]["reason"])

    def test_remove_moves_the_folder_and_restore_brings_it_back(self) -> None:
        voice_import.import_paths([_clip(self.picked / "Chris.wav", 1.0)])
        [entry] = voice_import.voices()
        clip_bytes = Path(entry["reference_path"]).read_bytes()
        result = voice_import.remove(f"studio-hub-local::{entry['id']}")
        self.assertEqual(result["removed"], entry["id"])
        self.assertEqual(voice_import.voices(), [])
        self.assertFalse(voice_import.voice_dir(entry["id"]).exists())
        [listed] = voice_import.removed()
        self.assertEqual((listed["id"], listed["display_name"]), (entry["id"], "Chris"))
        self.assertAlmostEqual(listed["duration_seconds"], 1.0, places=2)

        voice_import.restore(listed["backup"])
        [back] = voice_import.voices()
        self.assertEqual(back["id"], entry["id"])
        self.assertEqual(Path(back["reference_path"]).read_bytes(), clip_bytes)
        self.assertEqual(voice_import.removed(), [])
        with self.assertRaises(ValueError):
            voice_import.restore(listed["backup"])
        self.assertIsNone(voice_import.remove("voice-000000000000"))

    def test_the_list_shows_length_and_import_date_for_older_voices_too(self) -> None:
        # A voice from before either was stamped: both are read from the clip and its folder.
        folder = self.library / "voice-111111111111"
        clip = _clip(folder / "reference.wav", 1.5)
        self.catalog.parent.mkdir(parents=True, exist_ok=True)
        self.catalog.write_text(json.dumps({"voices": [
            {"id": "voice-111111111111", "display_name": "Chris", "reference_path": str(clip)}]}))
        row = voices._local_voices()[0].as_dict()
        self.assertAlmostEqual(row["duration_seconds"], 1.5, places=2)
        self.assertAlmostEqual(row["imported_at"], folder.stat().st_birthtime, places=0)

    def test_recent_narration_with_a_voice_is_found_in_the_job_history(self) -> None:
        store = JobStore(self.root / "jobs.sqlite3")
        self.addCleanup(store.close)
        for voice in ("studio-hub-local::voice-aaaaaaaaaaaa", "studio-hub-local::voice-bbbbbbbbbbbb"):
            store.submit(modality="tts", model_id="m", provider_id="p",
                         params={"voice_id": voice, "text": "hi"}, caller="story-studio@mini",
                         idempotency_key=None)
        used = store.voice_use("voice-aaaaaaaaaaaa", since=time.time() - 60)
        self.assertEqual([(u["caller"], u["jobs"]) for u in used], [("story-studio@mini", 1)])
        self.assertEqual(store.voice_use("voice-cccccccccccc", since=0), [])
        self.assertEqual(store.voice_use("voice-aaaaaaaaaaaa", since=time.time() + 60), [])

    def test_removing_a_voice_used_lately_needs_a_second_yes(self) -> None:
        from gateway import admin

        voice_import.import_paths([_clip(self.picked / "Chris.wav", 1.0)])
        [entry] = voice_import.voices()
        store = JobStore(self.root / "jobs.sqlite3")
        self.addCleanup(store.close)
        store.submit(modality="tts", model_id="m", params={"voice_id": f"studio-hub-local::{entry['id']}"},
                     caller="story-studio@mini", idempotency_key=None)
        with patch.object(admin, "store", store):
            refused = admin.forget_voice(f"studio-hub-local::{entry['id']}")
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(json.loads(refused.body)["code"], "VOICE_IN_USE")
            self.assertEqual(len(voice_import.voices()), 1)
            usage = admin.voice_usage(f"studio-hub-local::{entry['id']}")
            self.assertEqual([u["caller"] for u in usage["used_by"]], ["story-studio@mini"])
            done = admin.forget_voice(f"studio-hub-local::{entry['id']}", confirm_in_use=True)
        self.assertEqual(done["removed"], entry["id"])
        self.assertEqual(voice_import.voices(), [])


if __name__ == "__main__":
    unittest.main()
