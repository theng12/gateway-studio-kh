#!/usr/bin/env python3
"""Stock connectors — free source on by default, keyed sources gated by key + switch.

Pinned here:

* Wikimedia Commons is free, keyless and on by default, and its search only returns
  clips carrying a licence a monetised video may use;
* Pexels/Pixabay are off with no key, still off with a key but their switch off, and
  on only with both — and turning one on without its key is refused, naming the key;
* fetching through a connector into the footage library dedupes by sha256;
* free AI stock refuses a paid model **by name**, before anything is queued;
* the generation collector imports a settled job's artifact with AI provenance.

No network: every adapter's httpx client is replaced with a fake one.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-stock-sources-"))
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
for _key in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "WIKIMEDIA_COMMONS_API"):
    os.environ.pop(_key, None)

from gateway import footage_library, stock_sources  # noqa: E402
from gateway.catalog import catalog  # noqa: E402
from gateway.providers.credentials import CredentialStore  # noqa: E402

_VIDEO = b"\x00\x00\x00\x18ftypisom" + b"\x11" * 128 + b"a-stock-clip"

_COMMONS = {"query": {"pages": {
    "1": {"title": "File:Rain over the harbour.mp4", "imageinfo": [{
        "url": "https://upload.wikimedia.org/rain.mp4", "size": 4096, "mime": "video/mp4",
        "duration": 12.5,
        "extmetadata": {
            "LicenseShortName": {"value": "CC0"},
            "Artist": {"value": "<a href='#'>Ada</a>"},
            "ImageDescription": {"value": "Rain on the quay"},
        },
    }]},
    "2": {"title": "File:NC clip.mp4", "imageinfo": [{
        "url": "https://upload.wikimedia.org/nc.mp4", "mime": "video/mp4",
        "extmetadata": {"LicenseShortName": {"value": "CC BY-NC 4.0"}},
    }]},
}}}


class _Response:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.headers: dict = {}

    def json(self) -> dict:
        return self._payload


class _Stream:
    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data, self.status_code = data, status

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 65536):
        for start in range(0, len(self._data), chunk_size):
            yield self._data[start:start + chunk_size]


class _Client:
    """Enough of httpx.Client for an adapter: one JSON answer, one byte stream."""

    def __init__(self, payload: dict, data: bytes = b"", status: int = 200) -> None:
        self.payload, self.data, self.status = payload, data, status
        self.calls: list[dict] = []

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            **kwargs: object) -> _Response:
        self.calls.append({"url": url, "params": params, "headers": headers})
        return _Response(self.payload, self.status)

    def stream(self, method: str, url: str, timeout: float | None = None):
        self.calls.append({"stream": url})
        return _Stream(self.data, self.status)


class StockSourceTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_TEMP_ROOT, ignore_errors=True)

    def setUp(self) -> None:
        root = footage_library.default_root()
        if root.exists():
            shutil.rmtree(root)
        footage_library._config_path().unlink(missing_ok=True)
        stock_sources._state_path().unlink(missing_ok=True)
        stock_sources._INSTANCES.clear()
        self.store = CredentialStore(path=_TEMP_ROOT / "credentials.json")
        self._no_ffprobe = patch.object(footage_library, "_find_ffprobe", return_value=None)
        self._no_ffprobe.start()

    def tearDown(self) -> None:
        self._no_ffprobe.stop()

    # -- the free source --------------------------------------------------
    def test_the_free_keyless_source_is_on_by_default_and_searchable(self) -> None:
        source = stock_sources.source_for("wikimedia", self.store)
        self.assertTrue(source.is_configured())
        self.assertTrue(source.is_enabled())
        self.assertTrue(source.is_available())
        source._client = _Client(_COMMONS)
        items = source.search("rain", limit=5)
        self.assertEqual([item.source_id for item in items],
                         ["File:Rain over the harbour.mp4"],
                         "the CC BY-NC clip is refused, not returned")
        item = items[0]
        self.assertEqual(item.licence, "cc0")
        self.assertEqual(item.filetype, "mp4")
        self.assertEqual(item.duration_seconds, 12.5)
        self.assertEqual(item.author, "Ada", "the credit is text, not the markup")
        self.assertEqual(source.status()["cost"], source.definition.cost)
        self.assertIs(source.status()["free"], True)
        self.assertIs(source.status()["needs_key"], False)

    # -- the keyed sources ------------------------------------------------
    def test_a_keyed_source_needs_a_key_and_its_own_switch(self) -> None:
        source = stock_sources.source_for("pexels", self.store)
        self.assertFalse(source.is_configured(), "no key yet")
        self.assertFalse(source.is_available())

        key = "pk" + "-test-" + "1234"
        self.store.set_values_for("pexels", {"api_key": key}, {"api_key"})
        self.assertTrue(source.is_configured(), "now it has a key")
        self.assertFalse(source.is_enabled(), "a key alone does not turn it on")
        self.assertFalse(source.is_available())

        status = stock_sources.set_enabled("pexels", True, self.store)
        self.assertTrue(status["enabled"])
        self.assertTrue(source.is_available(), "key + switch = on")
        self.assertEqual(status["key_masked"], "pk-t••••••1234")

    def test_enabling_a_keyed_source_without_its_key_is_refused_with_the_reason(self) -> None:
        with self.assertRaises(stock_sources.StockSourceError) as refused:
            stock_sources.set_enabled("pixabay", True, self.store)
        self.assertEqual(refused.exception.code, "KEY_REQUIRED")
        self.assertIn("Pixabay API key", str(refused.exception))
        self.assertIn("PIXABAY_API_KEY", str(refused.exception))
        self.assertFalse(stock_sources.source_for("pixabay", self.store).is_enabled())

    def test_unknown_sources_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            stock_sources.set_enabled("nope", True, self.store)
        self.assertIsNone(stock_sources.source_for("nope", self.store))

    # -- fetch into the library -------------------------------------------
    def test_fetching_a_clip_into_the_library_dedupes_by_sha256(self) -> None:
        source = stock_sources.source_for("wikimedia", self.store)
        source._client = _Client(_COMMONS, data=_VIDEO)
        first = footage_library.download_into_library(
            "wikimedia", "File:Rain over the harbour.mp4", self.store)
        second = footage_library.download_into_library(
            "wikimedia", "File:Rain over the harbour.mp4", self.store)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(footage_library.list_clips()), 1)
        self.assertEqual(first["licence"], "cc0")
        self.assertEqual(first["provenance"], "Wikimedia Commons stock")
        self.assertEqual(first["source"], "wikimedia")
        self.assertIs(first["ai_generated"], False)
        self.assertIsNotNone(footage_library.clip_path(first["sha256"]))

    def test_a_switched_off_source_is_refused_at_fetch(self) -> None:
        stock_sources.set_enabled("wikimedia", False, self.store)
        with self.assertRaises(stock_sources.StockSourceError) as refused:
            footage_library.download_into_library(
                "wikimedia", "File:Rain over the harbour.mp4", self.store)
        self.assertEqual(refused.exception.code, "SOURCE_OFF")

    # -- free AI stock ----------------------------------------------------
    def test_free_ai_stock_refuses_a_paid_model_by_name(self) -> None:
        definition = types.SimpleNamespace(id="fal-ai", kind="fal", display_name="fal.ai",
                                           free_tier=False)
        model = {"model_id": "alibaba/wan-3.0/text-to-video", "display_name": "Wan 3.0",
                 "raw": {"pricing": {"unit": "second", "unit_price": 0.04, "currency": "USD"}}}
        with patch.object(catalog, "resolve", return_value=(definition, model)):
            with self.assertRaises(stock_sources.StockSourceError) as refused:
                stock_sources.assert_free_model("fal-ai::alibaba/wan-3.0/text-to-video")
        self.assertEqual(refused.exception.code, "PAID_MODEL_REFUSED")
        self.assertIn("Wan 3.0", str(refused.exception))
        self.assertIn("paid model", str(refused.exception))

    def test_free_ai_stock_accepts_a_model_on_the_owners_macs(self) -> None:
        definition = types.SimpleNamespace(id="studio-hub-local", kind="studio_hub",
                                           display_name="Studio Hub", free_tier=True)
        model = {"model_id": "mlx-community/local-video", "display_name": "Local video",
                 "raw": {}}
        with patch.object(catalog, "resolve", return_value=(definition, model)):
            self.assertIs(stock_sources.assert_free_model("studio-hub::mlx-community/local-video"),
                          model)

    def test_an_unknown_model_is_named_not_priced(self) -> None:
        with patch.object(catalog, "resolve", return_value=(None, None)):
            with self.assertRaises(stock_sources.StockSourceError) as refused:
                stock_sources.assert_free_model("no-such-model")
        self.assertEqual(refused.exception.code, "MODEL_NOT_FOUND")

    # -- the collector ----------------------------------------------------
    def test_a_settled_generation_is_imported_with_ai_provenance(self) -> None:
        artifact = _TEMP_ROOT / "generated-job.mp4"
        artifact.write_bytes(_VIDEO)
        job = {"id": "job-1", "state": "succeeded",
               "artifacts_json": json.dumps([{"path": str(artifact), "media_type": "video/mp4"}])}
        state = stock_sources.finish_job("job-1", "studio-hub::mlx-community/local-video", job=job)
        self.assertEqual(state, "imported")
        clips = footage_library.list_clips()
        self.assertEqual(len(clips), 1)
        self.assertIs(clips[0]["ai_generated"], True)
        self.assertEqual(clips[0]["licence"], "generated")
        self.assertIn("generated by studio-hub::mlx-community/local-video on the fleet",
                      clips[0]["provenance"])

    def test_a_pending_or_failed_generation_imports_nothing(self) -> None:
        self.assertEqual(stock_sources.finish_job("x", "m", job={"state": "running"}), "pending")
        self.assertEqual(stock_sources.finish_job("x", "m", job={"state": "failed"}), "failed")
        self.assertEqual(stock_sources.finish_job("x", "m", job={"state": "succeeded"}),
                         "failed", "a succeeded job with no artifact is not a clip")
        self.assertEqual(footage_library.list_clips(), [])

    def test_the_watcher_is_bounded(self) -> None:
        self.assertLessEqual(stock_sources.MAX_WATCH_SECONDS, 3600.0)
        self.assertGreater(stock_sources.MAX_WATCH_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
