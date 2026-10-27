#!/usr/bin/env python3
"""A Hub that publishes no video model is a clean result, and a retired model is never offered.

Retirement reaches this gateway through the Hub's own package list; `RETIRED_MODEL_IDS` is this
gateway's own backstop for a Hub that has not yet dropped one. What these tests pin is behaviour,
never a model's name: once a Hub stops publishing a video model nothing here keeps offering it, an
unrecognised video engine is reported rather than offered, every id on the retired list is withheld
whatever modality or casing a stale Hub gives it, and the Video lane with no model at all answers
with an empty list rather than an error.

The ids come from `RETIRED_MODEL_IDS` itself. A test that wrote them out would keep passing after
the list changed, and would be the one place a retired product was still named.

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
from gateway.providers.models import RETIRED_MODEL_IDS, ModelInventory  # noqa: E402

#: Read from the retirement list, never written here (see the module docstring).
RETIRED_PACKAGE_ID = RETIRED_MODEL_IDS[0]
RETIRED_MODEL_ID = RETIRED_MODEL_IDS[1]
#: A video engine no Hub release ships: `_HUB_ENGINE_MODALITY` has no video entry at all, so a
#: package naming one and stating no modality of its own cannot be placed.
RETIRED_ENGINE = "video-retired-engine"

HUB = ProviderDefinition(
    id="studio-hub-test", display_name="Test Hub", category="multi", kind=KIND_STUDIO_HUB,
    base_url="http://hub.invalid",
    modalities=("chat", "image", "tts", "music", "transcription", "video"),
    discovery=DISCOVERY_STUDIO_HUB, auto_enable_discovered=True,
)

_IMAGE_PACKAGE = {"package_id": "flux", "model_id": "mock-hub/image-model", "engine": "image-mflux",
                  "modality": "image", "type": "model", "files_state": "files_verified",
                  "dispatch_state": "eligible"}
_LANE_CONTRACT = {"schema": "studiohub.lane-contract.v1", "modality": "video",
                  "modes": ["image-to-video", "text-to-video"]}
#: A Hub that has not yet dropped a retired model: it still publishes it as an eligible video row.
_RETIRED_PACKAGE = {"package_id": RETIRED_PACKAGE_ID, "model_id": RETIRED_MODEL_ID,
                    "engine": RETIRED_ENGINE, "modality": "video", "type": "model",
                    "files_state": "files_verified", "dispatch_state": "eligible",
                    "lane_contract": _LANE_CONTRACT}
_RETIRED_CATALOGUE = {"repo": RETIRED_MODEL_ID, "hub_modality": "video", "type": "model",
                      "memory_eligible": True, "lane_contract": _LANE_CONTRACT}
_IMAGE_CATALOGUE = {"repo": "mock-hub/image-model", "hub_modality": "image", "type": "model"}

#: What a Hub on a release that has dropped the Video worker publishes: no video package at all.
_NO_VIDEO_PACKAGES = [_IMAGE_PACKAGE]
#: The same Hub as it still runs on the fleet, with the retired model still published.
_STALE_PACKAGES = [_IMAGE_PACKAGE, _RETIRED_PACKAGE]


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
        with _hub_answering(_NO_VIDEO_PACKAGES):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        self.assertIsNone(result.error)
        self.assertEqual(result.skipped, [])
        self.assertEqual({m.model_id for m in result.models}, {"mock-hub/image-model"})

    def test_an_unrecognised_video_engine_with_no_stated_lane_is_reported_not_offered(self) -> None:
        bare = {k: v for k, v in _RETIRED_PACKAGE.items() if k not in ("modality", "lane_contract")}
        with _hub_answering([_IMAGE_PACKAGE, bare]):
            result = discovery.discover(HUB, {"base_url": "http://hub.invalid"})
        self.assertNotIn(RETIRED_MODEL_ID, {m.model_id for m in result.models})
        self.assertEqual([row["engine"] for row in result.skipped], [RETIRED_ENGINE])


class RetiredModelTests(unittest.TestCase):
    def _inventory(self, name: str) -> ModelInventory:
        inventory = _inventory(name)
        with _hub_answering(_STALE_PACKAGES, (_IMAGE_CATALOGUE, _RETIRED_CATALOGUE)):
            result = inventory.refresh(HUB.id)
        self.assertIsNone(result.error)
        return inventory

    def test_a_hub_still_publishing_a_retired_model_is_not_published_and_shows_withheld(self) -> None:
        inventory = self._inventory("stale-hub.json")
        row = next(m for m in inventory.models_for(HUB.id) if m["model_id"] == RETIRED_MODEL_ID)
        self.assertTrue(row["enabled"], "setup: the Hub's own auto-enable is on")
        reason = inventory.withhold_reason(HUB, row)
        self.assertIn("retired", reason or "")
        self.assertFalse(inventory.can_publish(HUB, row))
        self.assertEqual(inventory.enabled_models("video"), [])

    def test_every_retired_id_is_withheld_in_any_case_and_under_any_modality(self) -> None:
        inventory = _inventory("retired-ids.json")
        for retired_id in RETIRED_MODEL_IDS:
            for modality in ("chat", "video"):
                for model in (
                    {"model_id": retired_id, "modality": modality, "raw": {}},
                    {"model_id": retired_id.upper(), "modality": modality, "raw": {}},
                    {"model_id": "something-else", "modality": modality,
                     "raw": {"package_id": retired_id}},
                ):
                    self.assertIn("retired", inventory.withhold_reason(HUB, model) or "",
                                  (retired_id, modality, model))

    def test_a_lookalike_id_is_not_caught_by_the_retirement(self) -> None:
        inventory = _inventory("lookalike.json")
        other = {"model_id": f"{RETIRED_MODEL_ID}-successor", "modality": "chat", "raw": {}}
        self.assertNotIn("retired", inventory.withhold_reason(HUB, other) or "")

    def test_a_retired_model_on_the_real_hub_row_is_never_published_as_a_video_model(self) -> None:
        inventory = self._inventory("stale-row.json")
        row = next(m for m in inventory.models_for(HUB.id) if m["model_id"] == RETIRED_MODEL_ID)
        # Discovery placed it where the Hub says it is; the retirement list now names it.
        self.assertEqual(row["modality"], "video")
        self.assertIn("retired", inventory.withhold_reason(HUB, row) or "")
        self.assertIn(RETIRED_MODEL_ID, {m["model_id"] for m in inventory.models_for(HUB.id)})
        self.assertNotIn(RETIRED_MODEL_ID,
                         [m["model_id"] for _d, m in inventory.enabled_models("video")])
        # Installed, with the worker still absent: still withheld (no lane contract, not catalogued).
        installed = dict(row, raw=dict(row["raw"], files_state="files_verified"))
        self.assertIsNotNone(inventory.withhold_reason(HUB, installed))
        # Not on any Create page either.
        with patch.object(registry, "models", inventory), patch.object(admin, "catalog", Catalog()):
            payload = admin.create_models()
        for rows in payload["by_modality"].values():
            self.assertNotIn(RETIRED_MODEL_ID, [row["model_id"] for row in rows])
        self.assertEqual(payload["counts"]["video"], 0)


class VideoLaneWithNoModelTests(unittest.TestCase):
    def test_a_retired_model_leaves_the_inventory_once_the_hub_stops_publishing_it(self) -> None:
        inventory = _inventory("stale-then-updated.json")
        with _hub_answering(_STALE_PACKAGES, (_IMAGE_CATALOGUE, _RETIRED_CATALOGUE)):
            inventory.refresh(HUB.id)
        self.assertIn(RETIRED_MODEL_ID, {m["model_id"] for m in inventory.models_for(HUB.id)})
        # The owner had switched it on; that decision is still on file after the Hub drops it.
        inventory.set_model_enabled(HUB.id, RETIRED_MODEL_ID, True)
        with _hub_answering(_NO_VIDEO_PACKAGES):
            inventory.refresh(HUB.id)
        self.assertNotIn(RETIRED_MODEL_ID, {m["model_id"] for m in inventory.models_for(HUB.id)})
        self.assertEqual(inventory.enabled_models("video"), [])

    def test_the_create_picker_answers_an_empty_video_lane_not_an_error(self) -> None:
        inventory = _inventory("no-video.json")
        with _hub_answering(_NO_VIDEO_PACKAGES):
            inventory.refresh(HUB.id)
        # A catalogue of its own: the shared one holds a job store another test may have closed.
        with patch.object(registry, "models", inventory), \
                patch.object(admin, "catalog", Catalog()):
            payload = admin.create_models()
        self.assertEqual(payload["by_modality"]["video"], [])
        self.assertEqual(payload["counts"]["video"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
