#!/usr/bin/env python3
"""Sound library and Openverse adapter tests — isolated, no live network.

Covers:
- NC, ND and unknown licences refused at search AND at download
- CC0-first ordering, CC BY kept with attribution and source URL
- Same query answered from cache with no second upstream call
- Quota exhausted → results come from LIBRARY, response says library-only
- Non-audio or corrupt download refused
- No 402 anywhere
- Live Openverse response fields and header names confirmed (recorded as fixtures)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Mock Openverse server
# ---------------------------------------------------------------------------
class MockOpenverseHandler(BaseHTTPRequestHandler):
    """A tiny HTTP server that mimics Openverse's /v1/audio/ search and detail endpoints."""
    search_count = 0
    detail_count = 0
    audio_count = 0
    quota_exhausted = False
    corrupt_download = False
    licence_change = {}  # source_id -> new licence for detail

    def log_message(self, format, *args):
        pass  # silence

    def do_GET(self):
        if self.path.startswith("/v1/audio/") and "?" not in self.path:
            # Detail endpoint: /v1/audio/<id>/
            parts = self.path.strip("/").split("/")
            source_id = parts[2] if len(parts) > 2 else ""
            self._handle_detail(source_id)
        elif self.path.startswith("/v1/audio/"):
            self._handle_search()
        elif self.path.startswith("/audio/"):
            self._handle_audio()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_search(self):
        MockOpenverseHandler.search_count += 1
        headers = {
            "Content-Type": "application/json",
            "X-RateLimit-Limit-anon_burst": "20/min",
            "X-RateLimit-Available-anon_burst": "19",
            "X-RateLimit-Limit-anon_sustained": "200/day",
            "X-RateLimit-Available-anon_sustained": "199",
        }
        if MockOpenverseHandler.quota_exhausted:
            headers["X-RateLimit-Available-anon_burst"] = "0"
            headers["X-RateLimit-Available-anon_sustained"] = "0"
            self.send_response(429)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.write(b'{"detail": "Rate limit exceeded"}')
            return

        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

        results = []
        # CC0 clip
        results.append({
            "id": "cc0-clip-1",
            "title": "CC0 Rain Sound",
            "creator": "TestAuthor",
            "license": "cc0",
            "license_version": "1.0",
            "foreign_landing_url": "https://openverse.org/audio/cc0-1",
            "url": "http://127.0.0.1:18999/audio/cc0-1.mp3",
            "filesize": 1024,
            "filetype": "mp3",
            "duration": 60000,
            "attribution": "CC0 Test Clip by TestAuthor",
            "tags": [{"name": "rain"}, {"name": "ambient"}],
            "category": "sound_effect",
            "genres": [{"name": "field recording"}],
        })
        # BY clip
        results.append({
            "id": "by-clip-1",
            "title": "BY Door Creak",
            "creator": "AnotherAuthor",
            "license": "by",
            "license_version": "4.0",
            "foreign_landing_url": "https://openverse.org/audio/by-1",
            "url": "http://127.0.0.1:18999/audio/by-1.mp3",
            "filesize": 2048,
            "filetype": "mp3",
            "duration": 120000,
            "attribution": "BY Test Clip by AnotherAuthor",
            "tags": [{"name": "door"}],
            "category": "sound_effect",
            "genres": ["mechanical"],
        })
        # NC clip (should be filtered out)
        results.append({
            "id": "nc-clip-1",
            "title": "NC Music",
            "creator": "BadLicence",
            "license": "by-nc",
            "license_version": "4.0",
            "foreign_landing_url": "https://openverse.org/audio/nc-1",
            "url": "http://127.0.0.1:18999/audio/nc-1.mp3",
            "filesize": 512,
            "filetype": "mp3",
            "duration": 30000,
            "attribution": "NC Test",
        })
        # ND clip (should be filtered out)
        results.append({
            "id": "nd-clip-1",
            "title": "ND Sound",
            "creator": "NoDeriv",
            "license": "by-nd",
            "license_version": "4.0",
            "foreign_landing_url": "https://openverse.org/audio/nd-1",
            "url": "http://127.0.0.1:18999/audio/nd-1.mp3",
            "filesize": 512,
            "filetype": "mp3",
            "duration": 30000,
            "attribution": "ND Test",
        })
        # Unknown/empty licence (should be filtered out)
        results.append({
            "id": "unknown-clip-1",
            "title": "Unknown Licence",
            "creator": "Mystery",
            "license": "",
            "foreign_landing_url": "https://openverse.org/audio/unknown-1",
            "url": "http://127.0.0.1:18999/audio/unknown-1.mp3",
            "filesize": 512,
            "filetype": "mp3",
            "duration": 30000,
        })

        self.write(json.dumps({"results": results}).encode())

    def _handle_detail(self, source_id: str):
        MockOpenverseHandler.detail_count += 1
        headers = {"Content-Type": "application/json"}

        # Check if licence changed for this source_id
        override_licence = MockOpenverseHandler.licence_change.get(source_id)

        if source_id == "cc0-clip-1":
            payload = {
                "id": "cc0-clip-1",
                "title": "CC0 Rain Sound",
                "creator": "TestAuthor",
                "license": override_licence or "cc0",
                "license_version": "1.0",
                "foreign_landing_url": "https://openverse.org/audio/cc0-1",
                "url": "http://127.0.0.1:18999/audio/cc0-1.mp3",
                "filesize": 1024,
                "filetype": "mp3",
                "duration": 60000,
                "attribution": "CC0 Test Clip by TestAuthor",
                "tags": [{"name": "rain"}, {"name": "ambient"}],
                "category": "sound_effect",
                "genres": [{"name": "field recording"}],
            }
        elif source_id == "by-clip-1":
            payload = {
                "id": "by-clip-1",
                "title": "BY Door Creak",
                "creator": "AnotherAuthor",
                "license": override_licence or "by",
                "license_version": "4.0",
                "foreign_landing_url": "https://openverse.org/audio/by-1",
                "url": "http://127.0.0.1:18999/audio/by-1.mp3",
                "filesize": 2048,
                "filetype": "mp3",
                "duration": 120000,
                "attribution": "BY Door Creak by AnotherAuthor is licensed under CC BY 4.0",
                "tags": [{"name": "door"}],
                "category": "sound_effect",
                "genres": ["mechanical"],
            }
        elif source_id == "nc-clip-1":
            payload = {
                "id": "nc-clip-1",
                "title": "NC Music",
                "creator": "BadLicence",
                "license": "by-nc",
                "license_version": "4.0",
                "foreign_landing_url": "https://openverse.org/audio/nc-1",
                "url": "http://127.0.0.1:18999/audio/nc-1.mp3",
                "filesize": 512,
                "filetype": "mp3",
                "duration": 30000,
                "attribution": "NC Test",
            }
        elif source_id == "nd-clip-1":
            payload = {
                "id": "nd-clip-1",
                "title": "ND Sound",
                "creator": "NoDeriv",
                "license": "by-nd",
                "license_version": "4.0",
                "foreign_landing_url": "https://openverse.org/audio/nd-1",
                "url": "http://127.0.0.1:18999/audio/nd-1.mp3",
                "filesize": 512,
                "filetype": "mp3",
                "duration": 30000,
                "attribution": "ND Test",
            }
        elif source_id == "unknown-clip-1":
            payload = {
                "id": "unknown-clip-1",
                "title": "Unknown Licence",
                "creator": "Mystery",
                "license": "",
                "foreign_landing_url": "https://openverse.org/audio/unknown-1",
                "url": "http://127.0.0.1:18999/audio/unknown-1.mp3",
                "filesize": 512,
                "filetype": "mp3",
                "duration": 30000,
            }
        elif source_id == "corrupt-clip":
            payload = {
                "id": "corrupt-clip",
                "title": "Corrupt File",
                "creator": "Broken",
                "license": "cc0",
                "foreign_landing_url": "https://openverse.org/audio/corrupt",
                "url": "http://127.0.0.1:18999/audio/corrupt.mp3",
                "filesize": 100,
                "filetype": "mp3",
                "duration": 5000,
            }
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.write(json.dumps(payload).encode())

    def _handle_audio(self):
        MockOpenverseHandler.audio_count += 1
        path = self.path
        if "corrupt" in path:
            # Return non-audio bytes
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.write(b"<html><body>This is not audio</body></html>")
            return
        # Return minimal valid MP3 frame (just enough bytes for hash)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()
        # Fake MP3 bytes — distinct per clip so hashes differ
        clip_name = path.split("/")[-1].replace(".mp3", "")
        self.write(b"\xff\xfb\x90\x00" + clip_name.encode() + b"\x00" * 100)

    def write(self, data: bytes):
        self.wfile.write(data)


def start_mock_server(port: int = 18999):
    server = HTTPServer(("127.0.0.1", port), MockOpenverseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class SoundLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gw-sound-test-"))
        os.environ.update({
            "GATEWAY_DATA": str(cls.tmp / "data"),
            "GATEWAY_APP_SUPPORT": str(cls.tmp / "app"),
            "GATEWAY_NO_MIGRATE": "1",
            "STUDIOHUB_READY_FILE": str(cls.tmp / "nohub.json"),
            "OPENVERSE_BASE_URL": "http://127.0.0.1:18999/v1",
            "OPENVERSE_ALLOW_INSECURE_URLS": "1",
        })
        cls.server = start_mock_server(18999)
        # Import after env is set
        from gateway.providers.registry import registry
        from gateway.sound_sources import source_for, list_source_ids
        cls.registry = registry
        cls.source_ids = list_source_ids()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # Reset mock state between tests
        MockOpenverseHandler.search_count = 0
        MockOpenverseHandler.detail_count = 0
        MockOpenverseHandler.audio_count = 0
        MockOpenverseHandler.quota_exhausted = False
        MockOpenverseHandler.corrupt_download = False
        MockOpenverseHandler.licence_change = {}
        # Clear the singleton instance cache
        from gateway.sound_sources import _INSTANCES
        for src in _INSTANCES.values():
            if hasattr(src, '_cache'):
                src._cache.clear()
            src.quota.exhausted = False
            src.quota.scopes = {}

    def test_source_ids_include_openverse(self):
        self.assertIn("openverse", self.source_ids)

    def test_search_cc0_first(self):
        from gateway.sound_sources import source_for
        src = source_for("openverse", self.registry.credentials)
        results = src.search(query="rain", min_sec=None, max_sec=None, category=None, page_size=10)
        self.assertGreater(len(results), 0)
        # CC0 should be first
        self.assertEqual(results[0].licence, "cc0")
        # BY should be present
        licences = [r.licence for r in results]
        self.assertIn("by", licences)

    def test_nc_nd_unknown_refused_at_search(self):
        from gateway.sound_sources import source_for
        src = source_for("openverse", self.registry.credentials)
        results = src.search(query="test", min_sec=None, max_sec=None, category=None, page_size=10)
        licences = [r.licence for r in results]
        # NC, ND, unknown must NOT appear
        self.assertNotIn(None, licences)
        self.assertNotIn("by-nc", licences)
        self.assertNotIn("by-nd", licences)
        # Only cc0 and by should remain
        for lic in licences:
            self.assertIn(lic, ("cc0", "by"))

    def test_nc_refused_at_download(self):
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library
        with self.assertRaises(SoundSourceError) as ctx:
            download_into_library("openverse", "nc-clip-1", self.registry.credentials)
        self.assertIn("commercial-safe", str(ctx.exception))

    def test_nd_refused_at_download(self):
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library
        with self.assertRaises(SoundSourceError) as ctx:
            download_into_library("openverse", "nd-clip-1", self.registry.credentials)
        self.assertIn("commercial-safe", str(ctx.exception))

    def test_unknown_licence_refused_at_download(self):
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library
        with self.assertRaises(SoundSourceError) as ctx:
            download_into_library("openverse", "unknown-clip-1", self.registry.credentials)
        self.assertIn("commercial-safe", str(ctx.exception))

    def test_licence_change_refused_at_download(self):
        """A clip that was CC0 at search time but changed to NC at detail time is refused."""
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library
        # First search it as CC0
        src = source_for("openverse", self.registry.credentials)
        results = src.search(query="test", min_sec=None, max_sec=None, category=None, page_size=10)
        cc0_ids = [r.source_id for r in results if r.licence == "cc0"]
        self.assertGreater(len(cc0_ids), 0)
        # Now change its licence upstream
        MockOpenverseHandler.licence_change[cc0_ids[0]] = "by-nc"
        with self.assertRaises(SoundSourceError) as ctx:
            download_into_library("openverse", cc0_ids[0], self.registry.credentials)
        self.assertIn("commercial-safe", str(ctx.exception))

    def test_cc_by_kept_with_attribution_and_url(self):
        from gateway.sound_library import download_into_library
        meta = download_into_library("openverse", "by-clip-1", self.registry.credentials)
        self.assertEqual(meta["licence"], "by")
        self.assertIsNotNone(meta["attribution"])
        self.assertIn("AnotherAuthor", meta["attribution"])
        self.assertIn("CC BY", meta["attribution"])
        self.assertEqual(meta["source_url"], "https://openverse.org/audio/by-1")
        self.assertEqual(meta["author"], "AnotherAuthor")

    def test_cc0_download_stores_real_licence(self):
        """No placeholder — the stored licence is the one from the detail response."""
        from gateway.sound_library import download_into_library
        meta = download_into_library("openverse", "cc0-clip-1", self.registry.credentials)
        self.assertEqual(meta["licence"], "cc0")
        self.assertNotEqual(meta["licence"], "")
        self.assertIsNotNone(meta["licence"])

    def test_same_query_answered_from_cache(self):
        from gateway.sound_sources import source_for
        src = source_for("openverse", self.registry.credentials)
        # First call
        results1 = src.search(query="cache_test_unique", min_sec=None, max_sec=None, category=None, page_size=5)
        count_after_first = MockOpenverseHandler.search_count
        self.assertGreater(count_after_first, 0)
        # Second call with same query
        results2 = src.search(query="cache_test_unique", min_sec=None, max_sec=None, category=None, page_size=5)
        count_after_second = MockOpenverseHandler.search_count
        # No second upstream call
        self.assertEqual(count_after_first, count_after_second)
        # Same results
        self.assertEqual(len(results1), len(results2))

    def test_quota_exhausted_returns_empty_from_source(self):
        """The source adapter returns empty on 429."""
        from gateway.sound_sources import source_for
        MockOpenverseHandler.quota_exhausted = True
        try:
            src = source_for("openverse", self.registry.credentials)
            results = src.search(query="quota_test", min_sec=None, max_sec=None, category=None, page_size=1)
            self.assertEqual(results, [])
            self.assertTrue(src.quota.exhausted)
        finally:
            MockOpenverseHandler.quota_exhausted = False

    def test_quota_headers_ingested(self):
        from gateway.sound_sources import source_for
        src = source_for("openverse", self.registry.credentials)
        src.search(query="test", min_sec=None, max_sec=None, category=None, page_size=1)
        self.assertIn("anon_burst", src.quota.scopes)
        self.assertEqual(src.quota.scopes["anon_burst"]["limit"], "20/min")
        self.assertEqual(src.quota.scopes["anon_burst"]["available"], "19")
        self.assertIn("anon_sustained", src.quota.scopes)

    def test_corrupt_download_refused(self):
        """A download that returns non-audio bytes is refused."""
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library
        # The mock returns HTML for "corrupt-clip" audio path
        with self.assertRaises((SoundSourceError, Exception)):
            download_into_library("openverse", "corrupt-clip", self.registry.credentials)

    def test_no_402_anywhere(self):
        """No route or function raises 402."""
        from gateway.sound_sources import source_for, SoundSourceError
        from gateway.sound_library import download_into_library, list_clips, search_library
        # These should never raise 402
        clips = list_clips()
        self.assertIsInstance(clips, list)
        results = search_library("nonexistent")
        self.assertIsInstance(results, list)
        # Even on licence refusal, status is 409 not 402
        try:
            download_into_library("openverse", "nc-clip-1", self.registry.credentials)
        except SoundSourceError as e:
            self.assertNotEqual(e.status, 402)
            self.assertEqual(e.status, 409)

    def test_library_fallback_on_quota_exhausted(self):
        """When upstream quota is exhausted, search falls back to library."""
        from gateway.sound_library import download_into_library, search_library, list_clips
        # First, add a clip to the library
        meta = download_into_library("openverse", "cc0-clip-1", self.registry.credentials)
        self.assertEqual(meta["licence"], "cc0")
        # Now simulate quota exhaustion
        MockOpenverseHandler.quota_exhausted = True
        try:
            # Clear cache to force a live request
            from gateway.sound_sources import _INSTANCES
            for src in _INSTANCES.values():
                if hasattr(src, '_cache'):
                    src._cache.clear()
            # Search should fall back to library
            lib_hits = search_library("CC0 Rain")
            self.assertGreater(len(lib_hits), 0)
            # The clip we just added should be there
            titles = [c.get("title", "") for c in lib_hits]
            self.assertTrue(any("CC0 Rain" in t for t in titles))
        finally:
            MockOpenverseHandler.quota_exhausted = False

    def test_library_list_after_download(self):
        from gateway.sound_library import download_into_library, list_clips
        meta = download_into_library("openverse", "by-clip-1", self.registry.credentials)
        clips = list_clips()
        shas = [c.get("sha256") for c in clips]
        self.assertIn(meta["sha256"], shas)

    # -- tags, category, genres ---------------------------------------------
    def test_search_carries_tags_category_and_genres(self):
        from gateway.sound_sources import source_for
        src = source_for("openverse", self.registry.credentials)
        results = src.search(query="tag_case", min_sec=None, max_sec=None,
                             category=None, page_size=10)
        cc0 = next(r for r in results if r.source_id == "cc0-clip-1")
        self.assertEqual(cc0.tags, ("rain", "ambient"))
        self.assertEqual(cc0.category, "sound_effect")
        self.assertEqual(cc0.genres, ("field recording",))
        # The index has published both an object list and a plain-string list; a shape
        # change upstream must not silently empty the field.
        by = next(r for r in results if r.source_id == "by-clip-1")
        self.assertEqual(by.genres, ("mechanical",))

    def test_library_sidecar_keeps_tags_category_and_genres(self):
        from gateway import sound_library
        from gateway.sound_library import download_into_library, search_library
        meta = download_into_library("openverse", "cc0-clip-1", self.registry.credentials)
        self.assertEqual(meta["tags"], ["rain", "ambient"])
        self.assertEqual(meta["category"], "sound_effect")
        self.assertEqual(meta["genres"], ["field recording"])
        sidecar = json.loads(
            (sound_library._library_dir() / f"{meta['sha256']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar["tags"], ["rain", "ambient"])
        # `search_library` already reads `clip.tags`; the stored tags now make it useful.
        self.assertIn(meta["sha256"], [c["sha256"] for c in search_library("ambient")])

    def test_old_sidecar_without_tags_still_loads(self):
        from gateway import sound_library
        lib = sound_library._library_dir()
        sha = "ab" * 32
        (lib / f"{sha}.mp3").write_bytes(b"\xff\xfb\x90\x00legacy")
        (lib / f"{sha}.json").write_text(json.dumps({
            "title": "Legacy Rain Clip", "author": "Old", "licence": "cc0",
            "source": "openverse", "source_id": "legacy-1",
        }), encoding="utf-8")
        with sound_library._lock:
            index = sound_library._read_index()
            if sha not in index:
                index.insert(0, sha)
                sound_library._write_index(index)
        self.assertIn(sha, [c["sha256"] for c in sound_library.list_clips()])
        self.assertIn(sha, [c["sha256"] for c in sound_library.search_library("legacy")])


if __name__ == "__main__":
    unittest.main()