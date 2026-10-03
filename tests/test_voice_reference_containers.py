"""A reference clip is judged by its bytes: QuickTime containers named `.mp3` get a real WAV.

Aiden, Mark, Vincent (and Samuel) were `.mov` files holding MP3 audio under a `.mp3` name. The
Voices page served them as `audio/mpeg` and the app's player (`NSSound`) refused them. Preview now
transcodes on the fly, import and the one-off migration write a proper `reference.wav`, and the
staging path (which hashes whatever `reference_path` names) uploads and checks the new file.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-voice-containers-")

from gateway import voice_audio, voice_import, voices  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402

_FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_clip(path: Path, kind: str) -> Path:
    """A half-second tone as `kind`: `wav`, `mp3`, or `mov` (MP3 audio in a QuickTime container)."""
    args = {
        "wav": ["-c:a", "pcm_s16le", "-f", "wav"],
        "mp3": ["-c:a", "libmp3lame", "-f", "mp3"],
        "mov": ["-c:a", "libmp3lame", "-f", "mov"],
    }[kind]
    subprocess.run(
        [_FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", *args, str(path)],
        check=True,
    )
    return path


@unittest.skipUnless(_FFMPEG, "ffmpeg is needed to build the fixture clips")
class ReferenceContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="voice-containers-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.library = self.root / "voice-library"
        self.catalog = self.root / "voices.json"
        for target, value in (
            (voice_import, {"LIBRARY_DIR": self.library, "WRITABLE_VOICES_PATH": self.catalog,
                            "VOICES_PATH": self.catalog}),
            (voices, {"WRITABLE_VOICES_PATH": self.catalog, "VOICES_PATH": self.catalog}),
        ):
            for name, replacement in value.items():
                patcher = patch.object(target, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)

    def _mismatched_voice(self, voice_id: str = "voice-aaaaaaaaaaaa", name: str = "Aiden") -> dict:
        folder = self.library / voice_id
        folder.mkdir(parents=True)
        clip = _make_clip(folder / "reference.mp3", "mov")
        entry = {
            "id": voice_id, "display_name": name, "language": "en", "gender": "m",
            "reference_path": str(clip), "audio_sha256": _sha(clip),
            "reference_transcript": "A half second tone.",
            "reference_segments": [{"start": 0.0, "end": 0.5, "text": "A half second tone."}],
        }
        return entry

    def _write_catalog(self, *entries: dict) -> None:
        self.catalog.write_text(json.dumps({"voices": list(entries)}), encoding="utf-8")

    # -- detection and preview -------------------------------------------------
    def test_the_container_is_judged_by_its_bytes_not_its_name(self) -> None:
        wav = _make_clip(self.root / "a.wav", "wav")
        mp3 = _make_clip(self.root / "b.mp3", "mp3")
        mov = _make_clip(self.root / "c.mp3", "mov")
        self.assertEqual((voice_audio.sniff(wav), voice_audio.sniff(mp3), voice_audio.sniff(mov)),
                         ("wav", "mp3", "other"))
        self.assertEqual(voice_audio.plain_media_type(wav), "audio/wav")
        self.assertEqual(voice_audio.plain_media_type(mp3), "audio/mpeg")
        self.assertIsNone(voice_audio.plain_media_type(mov))
        # a real MP3 under the wrong name is not served as the type its name claims
        self.assertIsNone(voice_audio.plain_media_type(mp3.rename(self.root / "d.wav")))

    def test_preview_of_a_mov_named_mp3_is_a_wav_and_the_reference_is_untouched(self) -> None:
        entry = self._mismatched_voice()
        self._write_catalog(entry)
        clip = Path(entry["reference_path"])
        before = clip.read_bytes()
        registry = voices.VoiceRegistry(path=self.root / "state.json")
        voice = voices._local_voices()[0]
        with patch.object(registry, "find", return_value=voice):
            body, media = registry.preview(voice.id)
        self.assertEqual(media, "audio/wav")
        self.assertEqual(body[:4] + body[8:12], b"RIFFWAVE")
        self.assertEqual(clip.read_bytes(), before)
        self.assertTrue((clip.parent / voice_audio.PREVIEW_CACHE).is_file())

    def test_preview_of_a_plain_clip_is_served_as_it_is(self) -> None:
        folder = self.library / "voice-bbbbbbbbbbbb"
        folder.mkdir(parents=True)
        clip = _make_clip(folder / "reference.mp3", "mp3")
        path, media = voice_audio.playable(clip)
        self.assertEqual((path, media), (clip, "audio/mpeg"))
        self.assertFalse((folder / voice_audio.PREVIEW_CACHE).exists())

    def test_a_stale_preview_cache_is_remade(self) -> None:
        entry = self._mismatched_voice()
        clip = Path(entry["reference_path"])
        cache, _ = voice_audio.playable(clip)
        cache.write_bytes(b"not a wav")
        cache2, media = voice_audio.playable(clip)  # unreadable cache: remade
        self.assertEqual(voice_audio.sniff(cache2), "wav")
        self.assertEqual(media, "audio/wav")

    # -- import ----------------------------------------------------------------
    def test_import_normalises_a_container_to_wav_and_keeps_the_original(self) -> None:
        source = self.root / "incoming"
        source.mkdir()
        mov = _make_clip(source / "Mark.mp3", "mov")
        result = voice_import.import_folder(source)
        self.assertEqual(len(result["imported"]), 1, result)
        entry = voice_import.voices()[0]
        folder = self.library / entry["id"]
        wav = folder / "reference.wav"
        self.assertEqual(entry["reference_path"], str(wav))
        self.assertEqual(voice_audio.sniff(wav), "wav")
        self.assertEqual((folder / "original.mp3").read_bytes(), mov.read_bytes())
        # identity stays the recording; the reference has its own digest
        self.assertEqual(entry["audio_sha256"], _sha(mov))
        self.assertEqual(entry["reference_sha256"], _sha(wav))
        meta = json.loads((folder / "metadata.json").read_text())
        self.assertEqual((meta["reference_file"], meta["reference_sha256"]), ("reference.wav", _sha(wav)))
        # what a shared-voice push uploads is checked by the Hub against the bytes it gets
        self.assertEqual(voice_import.push_fields(entry)["audio_sha256"], _sha(wav))
        # re-importing the same recording is the same voice, not a second one
        voice_import.import_folder(source)
        self.assertEqual(len(voice_import.voices()), 1)

    def test_import_copies_a_real_wav_as_it_is(self) -> None:
        source = self.root / "incoming"
        source.mkdir()
        wav = _make_clip(source / "Chris.wav", "wav")
        voice_import.import_folder(source)
        entry = voice_import.voices()[0]
        folder = self.library / entry["id"]
        self.assertEqual(_sha(folder / "reference.wav"), _sha(wav))
        self.assertFalse(list(folder.glob("original.*")))
        self.assertEqual(entry["reference_sha256"], entry["audio_sha256"])

    def test_import_that_cannot_convert_still_imports_the_clip(self) -> None:
        source = self.root / "incoming"
        source.mkdir()
        (source / "Odd.mp3").write_bytes(b"not audio at all")
        with patch.object(voice_audio, "_converters", return_value=[]):
            result = voice_import.import_folder(source)
        self.assertEqual(len(result["imported"]), 1)
        self.assertTrue(any("could not be converted" in n for n in result["notes"]), result["notes"])
        self.assertTrue(voice_import.voices()[0]["reference_path"].endswith("reference.mp3"))

    # -- migration -------------------------------------------------------------
    def test_migration_writes_a_wav_keeps_the_original_and_is_idempotent(self) -> None:
        mismatched = self._mismatched_voice()
        plain_folder = self.library / "voice-cccccccccccc"
        plain_folder.mkdir(parents=True)
        plain = _make_clip(plain_folder / "reference.mp3", "mp3")
        plain_entry = {"id": "voice-cccccccccccc", "display_name": "Todd", "reference_path": str(plain),
                       "audio_sha256": _sha(plain)}
        self._write_catalog(mismatched, plain_entry)
        original_bytes = Path(mismatched["reference_path"]).read_bytes()
        plain_bytes = plain.read_bytes()

        with self.assertLogs("gateway.voice_import", level="INFO") as logged:
            first = voice_import.migrate_references()
        self.assertEqual(first, {"migrated": ["voice-aaaaaaaaaaaa"], "failed": []})
        self.assertTrue(any("voice-aaaaaaaaaaaa" in line for line in logged.output))

        folder = self.library / "voice-aaaaaaaaaaaa"
        wav = folder / "reference.wav"
        by_id = {v["id"]: v for v in voice_import.voices()}
        migrated = by_id["voice-aaaaaaaaaaaa"]
        self.assertEqual(migrated["reference_path"], str(wav))
        self.assertEqual(migrated["reference_sha256"], _sha(wav))
        self.assertEqual(migrated["audio_sha256"], mismatched["audio_sha256"])  # the recording's identity
        self.assertEqual(migrated["reference_segments"], mismatched["reference_segments"])
        self.assertEqual((folder / "original.mp3").read_bytes(), original_bytes)
        self.assertFalse((folder / "reference.mp3").exists())
        self.assertEqual(voice_audio.sniff(wav), "wav")
        meta = json.loads((folder / "metadata.json").read_text())
        self.assertEqual((meta["reference_file"], meta["reference_sha256"]), ("reference.wav", _sha(wav)))
        # a plain MP3 reference is left exactly as it was
        self.assertEqual(by_id["voice-cccccccccccc"]["reference_path"], str(plain))
        self.assertEqual(plain.read_bytes(), plain_bytes)

        catalog_after = self.catalog.read_text()
        wav_after = wav.read_bytes()
        self.assertEqual(voice_import.migrate_references(), {"migrated": [], "failed": []})
        self.assertEqual(self.catalog.read_text(), catalog_after)
        self.assertEqual(wav.read_bytes(), wav_after)

    def test_migration_resumes_after_the_old_file_was_already_moved_aside(self) -> None:
        entry = self._mismatched_voice()
        clip = Path(entry["reference_path"])
        folder = clip.parent
        voice_audio.to_wav(clip, folder / "reference.wav")
        clip.rename(folder / "original.mp3")  # interrupted before the catalogue was written
        self._write_catalog(entry)
        self.assertEqual(voice_import.migrate_references()["migrated"], [entry["id"]])
        self.assertEqual(voice_import.voices()[0]["reference_path"], str(folder / "reference.wav"))

    def test_migration_that_cannot_convert_leaves_the_voice_untouched(self) -> None:
        entry = self._mismatched_voice()
        self._write_catalog(entry)
        clip = Path(entry["reference_path"])
        before = clip.read_bytes()
        with patch.object(voice_audio, "_converters", return_value=[]):
            result = voice_import.migrate_references()
        self.assertEqual(result["migrated"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(clip.read_bytes(), before)
        self.assertEqual(voice_import.voices()[0]["reference_path"], str(clip))

    # -- staging and TTS (fakes, no fleet) -------------------------------------
    def test_staging_hashes_the_new_file_and_tts_uses_it(self) -> None:
        entry = self._mismatched_voice()
        self._write_catalog(entry)
        voice_import.migrate_references()

        voice = voices._local_voices()[0]
        with patch.object(voices, "resolve", return_value=voice):
            request = type("R", (), {"params": {"voice_id": voice.id}})()
            reference = studio_hub._reference_for(request)
        wav = self.library / entry["id"] / "reference.wav"
        self.assertEqual(reference["path"], str(wav))

        posted: dict = {}

        class FakeResponse:
            status_code = 200

            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def json(self) -> dict:
                return self._payload

        class FakeHub:
            """Acts like the Hub: verifies the declared sha against the bytes, echoes what it kept."""

            def post(self, url: str, headers=None, files=None, data=None):
                audio = files["audio"][1]
                kept = hashlib.sha256(audio).hexdigest()
                assert kept == data["sha256"], "declared sha does not match the uploaded bytes"
                posted.update(url=url, audio=audio, sha=kept, transcript=data["transcript_segments"])
                return FakeResponse({"asset_id": "asset-1", "sha256": kept, "bytes": len(audio)})

        studio_hub._REFERENCE_CACHE.clear()
        asset_id = studio_hub._voice_placement(FakeHub(), "http://hub.test", {}, voice.id, reference)
        self.assertEqual(asset_id, "asset-1")
        self.assertEqual(posted["audio"], wav.read_bytes())
        self.assertEqual(posted["sha"], _sha(wav))
        self.assertNotEqual(posted["sha"], entry["audio_sha256"])  # the old container's digest
        self.assertIn("A half second tone.", posted["transcript"])
        self.assertTrue(posted["url"].endswith("/api/hub/voice-references"))


if __name__ == "__main__":
    unittest.main()
