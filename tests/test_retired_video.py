#!/usr/bin/env python3
"""The local video models (LTX-2.3, Lance) are retired: a Hub that publishes none is a clean result.

Retirement reaches this gateway through the Hub's own package list -- there is no gateway-side
retired list, and none was added (ACE-Step 1.5 and Lance went the same way). What this pins is the
other half: once a Hub stops publishing a video model, nothing here keeps offering it, and the
Video lane with no model at all answers with an empty list rather than an error.

Run with::

    .venv/bin/python tests/test_retired_video.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-retired-video-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_FLEET": str(_TEMP_ROOT / "data" / "fleet.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import admin  # noqa: E402
from gateway.catalog import Catalog  # noqa: E402
from gateway.providers import discovery, registry  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)
from gateway.providers.models import ModelInventory  # noqa: E402

LTX = "baa-ai/LTX-2.3-22B-RAM-12GB-MLX"
GEMMA = "mlx-community/gemma-3-12b-it-4bit"

HUB = ProviderDefinition(
    id="studio-hub-test", display_name="Test Hub", category="multi", kind=KIND_STUDIO_HUB,
    base_url="http://hub.invalid",
    modalities=("chat", "image", "tts", "music", "transcription", "video"),
    discovery=DISCOVERY_STUDIO_HUB, auto_enable_discovered=True,
)

#: What a Hub on a release that has dropped the Video worker publishes: no video package, and the
#: text encoder LTX used is still there as an ordinary chat model.
_RETIRED_PACKAGES = [
    {"package_id": "flux", "model_id": "mock-hub/image-model", "engine": "image-mflux",
     "modality": "image", "files_state": "files_verified", "dispatch_state": "eligible"},
    {"package_id": "gemma-3-12b-it-4bit", "model_id": GEMMA, "engine": "chat-mlx",
     "modality": "chat", "files_state": "files_verified", "dispatch_state": "eligible"},
]
#: A Hub not yet updated: it still publishes the retired model, saying nothing about its lane.
_STALE_PACKAGES = _RETIRED_PACKAGES + [
    {"package_id": "ltx-2-3-22b-ram-12gb-mlx", "model_id": LTX, "engine": "video-ltx",
     "files_state": "files_verified", "dispatch_state": "eligible"},
]


class _Response:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _hub_answering(packages):
    def fake_get(url, **_kwargs):
        if url.endswith("/api/hub/model-packages"):
            return _Response({"packages": packages})
        return _Response({"models": []})  # /api/hub/catalog
    return patch.object(discovery.httpx, "get", fake_get)


class _Credentials:
    def resolved(self, _provider_id):
        return {}

    def is_enabled(self, _provider_id):
        return True

    def is_configured(self, _provider_id):
        return True


def _inventory(name: str) -> ModelInventory:
    return ModelInventory(_Credentials(), path=_TEMP_ROOT / name,
                          definition_lookup=lambda pid: HUB if pid == HUB.id else None,
                          definitions_lookup=lambda: [HUB])


class HubWithoutVideoTests(unittest.TestCase):
    def test_a_hub_that_publishes_no_video_model_discovers_cleanly(self) -> None:
        with _hub_answering(_RETIRED_PACKAGES):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        self.assertIsNone(result.error)
        self.assertEqual(result.skipped, [])
        self.assertEqual({m.model_id for m in result.models}, {"mock-hub/image-model", GEMMA})
        self.assertEqual([m for m in result.models if m.modality == "video"], [])

    def test_gemma_is_not_touched_by_the_retirement(self) -> None:
        with _hub_answering(_RETIRED_PACKAGES):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        gemma = next(m for m in result.models if m.model_id == GEMMA)
        self.assertEqual(gemma.modality, "chat")

    def test_a_retired_video_engine_with_no_stated_lane_is_reported_not_offered(self) -> None:
        with _hub_answering(_STALE_PACKAGES):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        self.assertNotIn(LTX, {m.model_id for m in result.models})
        self.assertEqual([row["engine"] for row in result.skipped], ["video-ltx"])
        self.assertEqual(result.skipped[0]["model_ids"], [LTX])


class VideoLaneWithNoModelTests(unittest.TestCase):
    def test_ltx_leaves_the_inventory_once_the_hub_stops_publishing_it(self) -> None:
        inventory = _inventory("stale-then-updated.json")
        # A Hub 0.6.126 still publishes it, with its lane stated on the package row.
        still_published = _RETIRED_PACKAGES + [dict(_STALE_PACKAGES[-1], modality="video")]
        with _hub_answering(still_published):
            inventory.refresh(HUB.id)
        self.assertIn(LTX, {m["model_id"] for m in inventory.models_for(HUB.id)})
        # The owner had switched it on; that decision is still on file after the Hub drops it.
        inventory.set_model_enabled(HUB.id, LTX, True)
        with _hub_answering(_RETIRED_PACKAGES):
            inventory.refresh(HUB.id)
        listed = {m["model_id"] for m in inventory.models_for(HUB.id)}
        self.assertNotIn(LTX, listed)
        self.assertIn(GEMMA, listed)
        self.assertEqual(inventory.enabled_models("video"), [])

    def test_the_create_picker_answers_an_empty_video_lane_not_an_error(self) -> None:
        inventory = _inventory("no-video.json")
        with _hub_answering(_RETIRED_PACKAGES):
            inventory.refresh(HUB.id)
        # A catalogue of its own: the shared one holds a job store another test may have closed.
        with patch.object(registry, "models", inventory), \
                patch.object(admin, "catalog", Catalog()):
            payload = admin.create_models()
        self.assertEqual(payload["by_modality"]["video"], [])
        self.assertEqual(payload["counts"]["video"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
