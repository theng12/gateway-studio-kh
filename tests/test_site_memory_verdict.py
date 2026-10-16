#!/usr/bin/env python3
"""A site's memory verdict is any Mac that fits, not whichever Mac the catalogue listed last.

Measured 2026-10-04: the Hub's aggregate catalogue holds one row per worker, and discovery kept
the last. An 8 GB node listed last at 1100/1300/1400 published `memory_eligible: false` for
VoxCPM2/Breeze/Qwen3 1.7B, so those 16 GB+ sites were withheld from routing and every job of
those models waited on 1500/1600.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-site-memory-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
        "GATEWAY_API_KEY": "",
    }
)

from gateway import fleet  # noqa: E402
from gateway.providers import discovery  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)
from gateway.providers.models import ModelInventory  # noqa: E402

REPO = "mlx-community/VoxCPM2-8bit"


def _row(machine: str, fits: bool | None) -> dict:
    return {"repo": REPO, "hub_machine": machine, "memory_eligible": fits, "min_unified_memory_gb": 16}


def _catalog(*rows: dict) -> dict:
    response = SimpleNamespace(status_code=200, json=lambda: {"models": list(rows)})
    with patch.object(discovery.httpx, "get", lambda *a, **k: response):
        return discovery._hub_catalog("http://site.test", "token")


class SiteMemoryVerdictTests(unittest.TestCase):
    def test_a_small_node_listed_last_does_not_withhold_the_site(self) -> None:
        facts = _catalog(_row("local", True), _row("node-1104", True), _row("node-1109-8gb", False))
        self.assertIs(facts[REPO]["memory_eligible"], True)

    def test_a_site_where_no_mac_fits_still_says_so(self) -> None:
        facts = _catalog(_row("local", False), _row("node-8gb", False))
        self.assertIs(facts[REPO]["memory_eligible"], False)


FISH = "mlx-community/fish-s2-pro-4bit"
HUB = ProviderDefinition(
    id="studio-hub-1500", display_name="1500", category="multi", kind=KIND_STUDIO_HUB,
    base_url="http://site.test", modalities=("tts",),
    discovery=DISCOVERY_STUDIO_HUB, auto_enable_discovered=True,
)


def _package(installed: bool) -> dict:
    """The controller's own `/api/hub/model-packages` row: its disk only."""
    return {"package_id": "fish-s2-pro-4bit", "model_id": FISH, "engine": "voice-mlx",
            "modality": "tts", "type": "model",
            "files_state": "files_verified" if installed else "not_installed",
            "dispatch_state": "eligible" if installed else "ineligible"}


def _mac(machine: str, *, cached: bool, fits: bool = True) -> dict:
    """One Mac's row in the Hub's aggregate `/api/hub/catalog`."""
    return {"repo": FISH, "hub_machine": machine, "hub_modality": "tts", "type": "model",
            "hub_cached": cached, "runtime_ready": True, "available": cached,
            "memory_eligible": fits}


class _Credentials:
    def resolved(self, _provider_id):
        return {}

    def is_enabled(self, _provider_id):
        return True

    def is_configured(self, _provider_id):
        return True


def _withheld(package: dict, *macs: dict) -> str | None:
    """Discover a site from its controller's packages + per-Mac catalogue; why Fish is withheld."""
    def fake_get(url, **_kwargs):
        body = {"packages": [package]} if url.endswith("/model-packages") else {"models": list(macs)}
        return SimpleNamespace(status_code=200, text="", json=lambda: body)

    with patch.object(discovery.httpx, "get", fake_get):
        result = discovery.discover(HUB, {"base_url": "http://site.test"})
    model = next(m for m in result.models if m.model_id == FISH)
    inventory = ModelInventory(_Credentials(), path=_TEMP_ROOT / "models.json",
                               definition_lookup=lambda pid: HUB, definitions_lookup=lambda: [HUB])
    return inventory.withhold_reason(HUB, {"model_id": FISH, "modality": "tts", "raw": model.raw})


class SiteReadinessAnyMacTests(unittest.TestCase):
    """A site offers a model when any of its Macs can run it, not only the controller."""

    def test_a_group_where_only_a_node_has_the_model_is_offered(self) -> None:
        # 1500 on 2026-10-04: Fish installed on node 1504, not on the controller.
        reason = _withheld(_package(installed=False), _mac("local", cached=False),
                           _mac("1504", cached=True), _mac("1502", cached=False))
        self.assertIsNone(reason)

    def test_a_group_where_no_mac_has_it_is_not_offered(self) -> None:
        reason = _withheld(_package(installed=False), _mac("local", cached=False),
                           _mac("1504", cached=False))
        self.assertIn("no Mac", reason or "")

    def test_an_installed_mac_that_cannot_fit_it_does_not_offer_it(self) -> None:
        reason = _withheld(_package(installed=True), _mac("local", cached=True, fits=False),
                           _mac("1504", cached=False))
        self.assertIsNotNone(reason)

    def test_controller_only_still_works(self) -> None:
        self.assertIsNone(_withheld(_package(installed=True), _mac("local", cached=True)))

    def test_an_older_hub_without_install_facts_falls_back_to_the_package_row(self) -> None:
        old = {"repo": FISH, "hub_modality": "tts", "type": "model", "memory_eligible": True}
        self.assertIsNone(_withheld(_package(installed=True), old))
        self.assertIn("not installed", _withheld(_package(installed=False), old) or "")


class CatalogueReadFailureTests(unittest.TestCase):
    """A catalogue read that fails must not erase the last good answer (1607 Gemma, 2026-10-16).

    The 1600 controller's aggregate catalogue is slow while its busy node works. One read that timed
    out produced rows with no per-Mac facts, so the controller's own disk (`not_installed`) withheld
    a model only a node has, and a queued job failed MODEL_NOT_OFFERABLE until the next refresh.
    """

    def _inventory(self) -> ModelInventory:
        return ModelInventory(_Credentials(), path=Path(tempfile.mkdtemp(dir=_TEMP_ROOT)) / "models.json",
                              definition_lookup=lambda pid: HUB, definitions_lookup=lambda: [HUB])

    def _refresh(self, inventory: ModelInventory, catalogue) -> object:
        def fake_get(url, **_kwargs):
            if url.endswith("/model-packages"):
                body = {"packages": [_package(installed=False)]}
            elif catalogue is None:
                raise discovery.httpx.ReadTimeout("slow catalogue")
            else:
                body = {"models": catalogue}
            return SimpleNamespace(status_code=200, text="", json=lambda: body)

        with patch.object(discovery.httpx, "get", fake_get):
            return inventory.refresh(HUB.id, hub_version="0.6.180")

    def _reason(self, inventory: ModelInventory) -> str | None:
        model = next(m for m in inventory.models_for(HUB.id) if m["model_id"] == FISH)
        return inventory.withhold_reason(HUB, model)

    def test_a_failed_catalogue_read_keeps_the_last_answer(self) -> None:
        inventory = self._inventory()
        self.assertIsNone(self._refresh(inventory, [_mac("local", cached=False), _mac("1504", cached=True)]).error)
        self.assertIsNone(self._reason(inventory))
        self.assertIsNotNone(self._refresh(inventory, None).error)
        self.assertIsNone(self._reason(inventory))

    def test_a_first_discovery_without_a_catalogue_still_lists_the_packages(self) -> None:
        inventory = self._inventory()
        self.assertIsNone(self._refresh(inventory, None).error)
        self.assertIn("not installed", self._reason(inventory) or "")


class FleetSiteVerdictTests(unittest.TestCase):
    def test_supply_rows_of_one_model_on_different_revisions_add_up(self) -> None:
        index = fleet._model_supply_index([
            {"internal_model_id": FISH, "runtime_revision": "a",
             "eligible_physical_slots_total": 2, "available_physical_slots": 1},
            {"internal_model_id": FISH, "runtime_revision": "b",
             "eligible_physical_slots_total": 1, "available_physical_slots": 0},
        ])
        self.assertEqual(index[FISH], {"eligible": 3, "available": 1})

    def test_the_controllers_package_verdict_is_not_stamped_on_a_node(self) -> None:
        dispatch = fleet._dispatch_index({"models": [_package(installed=False)]})
        rows = fleet._local_models({"models": [
            {"repo": FISH, "hub_machine": "local", "cache": {"package_id": "fish-s2-pro-4bit"}},
            {"repo": FISH, "hub_machine": "1504", "cache": {"package_id": "fish-s2-pro-4bit"}},
        ]}, dispatch)
        self.assertEqual([r["dispatch_state"] for r in rows], ["ineligible", None])


if __name__ == "__main__":
    unittest.main()
