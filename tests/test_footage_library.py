#!/usr/bin/env python3
"""Footage library — the authoritative stock store.

Pinned here: bytes and sidecar land together, a duplicate sha256 never becomes a
second row, search and the byte round-trip work, quota is measured from the disk,
unknown fields stay null and are named "not recorded", generated clips record
their provenance, the root setter refuses a missing/unwritable directory, and a
retired clip is *moved* to the trash, never deleted.

No network and no ffprobe: probing is patched out so the tests exercise the store,
not the host's tools.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-footage-library-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})

from gateway import footage_library as library  # noqa: E402
from gateway.sound_sources import hash_bytes  # noqa: E402


def _clip(seed: int) -> bytes:
    """Synthetic bytes in an mp4's shape, distinct per seed."""
    return b"\x00\x00\x00\x18ftypisom" + bytes([seed]) * 64 + f"clip-{seed}".encode()


class FootageLibraryTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_TEMP_ROOT, ignore_errors=True)

    def setUp(self) -> None:
        root = library.default_root()
        if root.exists():
            shutil.rmtree(root)
        library._config_path().unlink(missing_ok=True)
        self._no_ffprobe = patch.object(library, "_find_ffprobe", return_value=None)
        self._no_ffprobe.start()

    def tearDown(self) -> None:
        self._no_ffprobe.stop()

    # -- adding -----------------------------------------------------------
    def test_a_clip_is_stored_with_its_sidecar_and_index_entry(self) -> None:
        data = _clip(1)
        meta = library.add_clip(data, {
            "title": "Rain on a street", "description": "Night city",
            "licence": "cc0", "licence_note": "Public domain",
            "provenance": "owner import", "ai_generated": False,
            "source": "owner", "source_id": "clip-1", "tags": ["rain", "city"],
            "format": "mp4",
        }, explicit=True)
        sha = hash_bytes(data)
        root = library.get_root()
        self.assertEqual(meta["sha256"], sha)
        self.assertEqual((root / f"{sha}.mp4").read_bytes(), data)
        sidecar = json.loads((root / f"{sha}.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["title"], "Rain on a street")
        self.assertEqual(sidecar["licence"], "cc0")
        self.assertEqual(sidecar["tags"], ["rain", "city"])
        self.assertEqual(sidecar["filesize_bytes"], len(data))
        self.assertIsNone(sidecar["duration_seconds"], "no probe, so no invented duration")
        self.assertEqual([clip["sha256"] for clip in library.list_clips()], [sha])

    def test_an_explicit_import_takes_base64_in_format_and_sniffs_the_container(self) -> None:
        data = _clip(2)
        meta = library.import_bytes({
            "title": "Imported clip", "licence": "owner-supplied",
            "format": base64.b64encode(data).decode(),
        })
        sha = hash_bytes(data)
        self.assertEqual(meta["sha256"], sha)
        self.assertEqual(meta["licence"], "owner-supplied")
        self.assertIsNotNone(library.clip_path(sha), "the container came from the file's own bytes")
        self.assertEqual(library.clip_path(sha).suffix, ".mp4")

    def test_a_duplicate_sha256_is_the_same_clip_not_a_second_row(self) -> None:
        data = _clip(3)
        payload = {"title": "Same clip", "licence": "cc0", "format": "mp4"}
        first = library.add_clip(data, dict(payload), explicit=True)
        second = library.add_clip(data, dict(payload), explicit=True)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(library.list_clips()), 1)
        self.assertEqual(len(library._read_index()), 1)

    def test_a_connector_clip_must_carry_a_commercial_safe_licence(self) -> None:
        with self.assertRaises(ValueError) as missing:
            library.add_clip(_clip(4), {"title": "No licence", "format": "mp4"}, explicit=False)
        self.assertIn("commercial-safe", str(missing.exception))
        with self.assertRaises(ValueError):
            library.add_clip(_clip(5), {"title": "NC", "licence": "by-nc", "format": "mp4"},
                             explicit=False)
        # The provider's own free-use word is accepted, not forced into cc0/by.
        meta = library.add_clip(_clip(6), {"title": "Pexels", "licence": "Pexels",
                                           "format": "mp4"}, explicit=False)
        self.assertEqual(meta["licence"], "pexels")

    # -- reading ----------------------------------------------------------
    def test_search_matches_titles_and_tags(self) -> None:
        library.add_clip(_clip(7), {"title": "Harbour at dawn", "licence": "cc0",
                                    "format": "mp4", "tags": ["harbour"]}, explicit=True)
        library.add_clip(_clip(8), {"title": "Desert road", "licence": "cc0",
                                    "format": "mp4", "tags": ["desert"]}, explicit=True)
        self.assertEqual([c["title"] for c in library.search_library("harbour")],
                         ["Harbour at dawn"])
        self.assertEqual([c["title"] for c in library.search_library("", tag="desert")],
                         ["Desert road"])
        self.assertEqual(len(library.search_library("")), 2)

    def test_the_bytes_round_trip_with_their_media_type(self) -> None:
        data = _clip(9)
        meta = library.add_clip(data, {"title": "Round trip", "licence": "cc0",
                                       "format": "mp4"}, explicit=True)
        path = library.clip_path(meta["sha256"])
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(library.clip_media_type(path), "video/mp4")

    def test_unknown_fields_read_null_and_are_named_not_recorded(self) -> None:
        meta = library.add_clip(_clip(10), {"title": "Unknowns", "licence": "cc0",
                                            "format": "mp4"}, explicit=True)
        self.assertEqual(library.NOT_RECORDED, "not recorded")
        clip = library.describe(library.get_clip(meta["sha256"]))
        self.assertIsNone(clip["duration_seconds"])
        self.assertIsNone(clip["resolution"])
        self.assertIn("duration_seconds", clip["not_recorded"])
        self.assertIn("resolution", clip["not_recorded"])
        self.assertNotIn("licence", clip["not_recorded"])

    def test_a_probe_fills_the_sidecar_when_the_file_can_be_read(self) -> None:
        probe = {"duration_seconds": 4.5, "width": 1920, "height": 1080,
                 "resolution": "1920x1080", "fps": 25.0, "codec": "h264"}
        with patch.object(library, "probe_video", return_value=probe):
            meta = library.add_clip(_clip(11), {"title": "Probed", "licence": "cc0",
                                                "format": "mp4"}, explicit=True)
        self.assertEqual(meta["duration_seconds"], 4.5)
        self.assertEqual(meta["resolution"], "1920x1080")
        self.assertEqual(meta["fps"], 25.0)
        self.assertEqual(meta["codec"], "h264")

    def test_a_generated_clip_records_its_provenance(self) -> None:
        meta = library.add_generated(_clip(12), model_id="studio-hub::wan-3.0",
                                     title="A generated street", tags=["ai"])
        self.assertIs(meta["ai_generated"], True)
        self.assertEqual(meta["licence"], "generated")
        self.assertIn("generated by studio-hub::wan-3.0 on the fleet", meta["provenance"])
        stored = library.describe(library.get_clip(meta["sha256"]))
        self.assertIs(stored["ai_generated"], True)

    # -- quota ------------------------------------------------------------
    def test_quota_is_measured_from_the_disk(self) -> None:
        data = _clip(13)
        library.add_clip(data, {"title": "Measured", "licence": "cc0", "format": "mp4"},
                         explicit=True)
        quota = library.quota_status()
        self.assertEqual(quota["files"], 1)
        self.assertEqual(quota["bytes"], len(data))
        self.assertEqual(quota["root"], str(library.get_root()))
        self.assertGreater(quota["free_bytes"], 0)

    # -- root -------------------------------------------------------------
    def test_the_root_setter_refuses_a_missing_or_unwritable_directory(self) -> None:
        with self.assertRaises(ValueError) as missing:
            library.set_root(_TEMP_ROOT / "nowhere")
        self.assertIn("does not exist", str(missing.exception))

        a_file = _TEMP_ROOT / "not-a-dir"
        a_file.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError) as not_dir:
            library.set_root(a_file)
        self.assertIn("not a directory", str(not_dir.exception))

        read_only = _TEMP_ROOT / "read-only"
        read_only.mkdir()
        read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(ValueError) as unwritable:
                library.set_root(read_only)
            self.assertIn("not writable", str(unwritable.exception))
        finally:
            read_only.chmod(stat.S_IRWXU)

        chosen = _TEMP_ROOT / "chosen-root"
        chosen.mkdir()
        self.assertEqual(library.set_root(chosen), chosen)
        self.assertEqual(library.get_root(), chosen)
        self.assertTrue((chosen / "index.json").exists() or library.list_clips() == [])

    def test_a_configured_root_that_has_gone_away_falls_back_to_the_default(self) -> None:
        gone = _TEMP_ROOT / "unmounted" / "footage"
        library.set_root(gone.mkdir(parents=True) or gone)
        shutil.rmtree(gone.parent)
        self.assertEqual(library.get_root(), library.default_root())

    # -- retirement -------------------------------------------------------
    def test_retire_moves_the_clip_to_the_trash_so_it_can_come_back(self) -> None:
        data = _clip(14)
        meta = library.add_clip(data, {"title": "Retire me", "licence": "cc0",
                                       "format": "mp4"}, explicit=True)
        sha = meta["sha256"]
        result = library.retire(sha)
        self.assertTrue(result["restorable"])
        self.assertIsNone(library.clip_path(sha))
        self.assertIsNone(library.get_clip(sha))
        self.assertEqual(library.list_clips(), [])
        trashed = _TEMP_ROOT / "data" / "_trash" / "footage-library" / f"{sha}.mp4"
        self.assertTrue(trashed.exists(), "the bytes moved to the trash, they were not deleted")
        self.assertEqual(trashed.read_bytes(), data)
        with self.assertRaises(ValueError):
            library.retire(sha)


if __name__ == "__main__":
    unittest.main()
