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

_IMAGE_PACKAGE = {"package_id": "flux", "model_id": "mock-hub/image-model", "engine": "image-mflux",
                  "modality": "image", "type": "model", "files_state": "files_verified",
                  "dispatch_state": "eligible"}
#: The Gemma text encoder as the real Hub publishes it once LTX no longer lists it: a `model` on
#: the retired video engine whose worker is absent. The Hub's catalogue does not describe it.
_GEMMA_PACKAGE = {"package_id": "gemma-3-12b-it-4bit", "model_id": GEMMA, "engine": "video-ltx",
                  "modality": "video", "type": "model", "worker_id": "video",
                  "worker_state": "unavailable", "dispatch_state": "ineligible",
                  "files_state": "not_installed"}
_LTX_CONTRACT = {"schema": "studiohub.lane-contract.v1", "modality": "video",
                 "modes": ["image-to-video", "text-to-video"]}
#: A Hub 0.6.126 that still publishes LTX as an eligible, catalogued video model.
_LTX_PACKAGE = {"package_id": "ltx-2-3-22b-ram-12gb-mlx", "model_id": LTX, "engine": "video-ltx",
                "modality": "video", "type": "model", "files_state": "files_verified",
                "dispatch_state": "eligible", "lane_contract": _LTX_CONTRACT}
_LTX_CATALOGUE = {"repo": LTX, "hub_modality": "video", "type": "model",
                  "memory_eligible": True, "lane_contract": _LTX_CONTRACT}
_IMAGE_CATALOGUE = {"repo": "mock-hub/image-model", "hub_modality": "image", "type": "model"}

#: What a Hub on a release that has dropped the Video worker publishes: no video package at all.
_RETIRED_PACKAGES = [_IMAGE_PACKAGE]
#: The same Hub as it still runs on the fleet: LTX published, Gemma beside it.
_STALE_PACKAGES = [_IMAGE_PACKAGE, _GEMMA_PACKAGE, _LTX_PACKAGE]


class _Response:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _hub_answering(packages, catalogue=(_IMAGE_CATALOGUE,)):
    def fake_get(url, **_kwargs):
        if url.endswith("/api/hub/model-packages"):
            return _Response({"packages": packages})
        return _Response({"models": list(catalogue)})  # /api/hub/catalog
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
        self.assertEqual({m.model_id for m in result.models}, {"mock-hub/image-model"})

    def test_a_retired_video_engine_with_no_stated_lane_is_reported_not_offered(self) -> None:
        bare = {k: v for k, v in _LTX_PACKAGE.items() if k not in ("modality", "lane_contract")}
        with _hub_answering([_IMAGE_PACKAGE, bare]):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        self.assertNotIn(LTX, {m.model_id for m in result.models})
        self.assertEqual([row["engine"] for row in result.skipped], ["video-ltx"])


class RetiredModelTests(unittest.TestCase):
    def _inventory(self, name: str) -> ModelInventory:
        inventory = _inventory(name)
        with _hub_answering(_STALE_PACKAGES, (_IMAGE_CATALOGUE, _LTX_CATALOGUE)):
            result = inventory.refresh(HUB.id)
        self.assertIsNone(result.error)
        return inventory

    def test_a_hub_still_publishing_ltx_is_not_published_and_shows_withheld(self) -> None:
        inventory = self._inventory("stale-hub.json")
        ltx = next(m for m in inventory.models_for(HUB.id) if m["model_id"] == LTX)
        self.assertTrue(ltx["enabled"], "setup: the Hub's own auto-enable is on")
        reason = inventory.withhold_reason(HUB, ltx)
        self.assertIn("retired", reason or "")
        self.assertFalse(inventory.can_publish(HUB, ltx))
        self.assertEqual(inventory.enabled_models("video"), [])

    def test_the_retirement_matches_either_id_in_any_case(self) -> None:
        inventory = _inventory("case.json")
        for model in (
            {"model_id": LTX.upper(), "modality": "video", "raw": {}},
            {"model_id": "something-else", "modality": "video",
             "raw": {"package_id": "LTX-2-3-22B-RAM-12GB-MLX"}},
        ):
            self.assertIn("retired", inventory.withhold_reason(HUB, model) or "", model)

    def test_gemma_on_the_real_hub_row_is_never_published_as_a_video_model(self) -> None:
        inventory = self._inventory("gemma.json")
        gemma = next(m for m in inventory.models_for(HUB.id) if m["model_id"] == GEMMA)
        # Discovery placed it where the Hub says it is, and the retirement list does not name it.
        self.assertEqual(gemma["modality"], "video")
        reason = inventory.withhold_reason(HUB, gemma)
        self.assertIsNotNone(reason)
        self.assertNotIn("retired", reason)
        self.assertIn(GEMMA, {m["model_id"] for m in inventory.models_for(HUB.id)})
        self.assertNotIn(GEMMA, [m["model_id"] for _d, m in inventory.enabled_models("video")])
        # Installed, with the worker still absent: still withheld (no lane contract, not catalogued).
        installed = dict(gemma, raw=dict(gemma["raw"], files_state="files_verified"))
        self.assertIsNotNone(inventory.withhold_reason(HUB, installed))
        # Not on any Create page either.
        with patch.object(registry, "models", inventory), patch.object(admin, "catalog", Catalog()):
            payload = admin.create_models()
        for rows in payload["by_modality"].values():
            self.assertNotIn(GEMMA, [row["model_id"] for row in rows])
            self.assertNotIn(LTX, [row["model_id"] for row in rows])
        self.assertEqual(payload["counts"]["video"], 0)


class VideoLaneWithNoModelTests(unittest.TestCase):
    def test_ltx_leaves_the_inventory_once_the_hub_stops_publishing_it(self) -> None:
        inventory = _inventory("stale-then-updated.json")
        with _hub_answering(_STALE_PACKAGES, (_IMAGE_CATALOGUE, _LTX_CATALOGUE)):
            inventory.refresh(HUB.id)
        self.assertIn(LTX, {m["model_id"] for m in inventory.models_for(HUB.id)})
        # The owner had switched it on; that decision is still on file after the Hub drops it.
        inventory.set_model_enabled(HUB.id, LTX, True)
        with _hub_answering(_RETIRED_PACKAGES):
            inventory.refresh(HUB.id)
        self.assertNotIn(LTX, {m["model_id"] for m in inventory.models_for(HUB.id)})
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
