#!/usr/bin/env python3
"""A newly added location starts with the models it has installed switched on.

The owner's rule (2026-10-09): *"a new group starts with every model it actually
has installed switched on; you switch off what you don't want in the Gateway.
Nothing copied from This Mac."* It replaced `seed_enabled_from_local`, which
copied This Mac's switches to every new location.

The edges it must not blur:

  * installed and ready means the location's own Hub says `files_verified` and
    `eligible`, for a catalogued model (not a dependency) that is not retired;
  * the owner's ruled-out models (`fleet.NEVER_AUTO_ENABLED`: Phonon-2 parked,
    Pocket-TTS disqualified, the Hub's shelved list) stay off;
  * nothing is read from This Mac's switches;
  * **re-adding** a location rewrites nothing, and a decision already recorded
    for a location stands.

Run with::

    .venv/bin/python tests/test_fleet_new_location_models.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-fleet-inherit-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))
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
        # The local Hub is this test's own mock, not whatever is running on the
        # owner's Mac: `find_ready_file()` falls back to his real hub-ready.json,
        # and a test that seeded from *his* decisions would be measuring his
        # machine and mutating nothing of ours.
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "hub-ready.json"),
    }
)

from mock_upstream import Handler, Server  # noqa: E402

from gateway import admin, fleet  # noqa: E402
from gateway.providers import models as models_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.credentials import CredentialStore  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)
from gateway.providers.discovery import DiscoveredModel, DiscoveryResult  # noqa: E402

LOCAL = fleet.provider_id_for(fleet.LOCAL_ID)  # studio-hub-local


def _definition(provider_id: str, *, auto_enable: bool) -> ProviderDefinition:
    return ProviderDefinition(
        id=provider_id,
        display_name=provider_id,
        category="multi",
        kind=KIND_STUDIO_HUB,
        base_url=f"http://{provider_id}.invalid",
        modalities=("tts", "chat"),
        discovery=DISCOVERY_STUDIO_HUB,
        auto_enable_discovered=auto_enable,
    )


class AddingALocationAgainstTheMockHub(unittest.TestCase):
    """The real add route, the real registry, the suite's mock Hub."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # What a running Hub publishes so this gateway can find it. Pointed at the
        # mock, this Mac *is* a location with models, and one whose decisions can
        # be set deliberately.
        (Path(os.environ["STUDIOHUB_READY_FILE"])).write_text(
            json.dumps({"url": cls.base, "token": "local-hub-token"}), encoding="utf-8"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_new_location_switches_on_what_it_has_installed_not_what_this_mac_has(self) -> None:
        discovered = registry.refresh(LOCAL)
        self.assertIsNone(discovered.error, discovered.error)
        # This Mac switches everything off: nothing of that may travel.
        local_ids = [m["model_id"] for m in registry.models.models_for(LOCAL, include_disabled=True)]
        registry.models.set_many(LOCAL, local_ids, False)

        added = admin.fleet_add_controller(
            {"label": "Second Mac", "url": self.base, "token": "fleet-token"}
        )
        self.assertTrue(added["controller"]["created"], added["controller"])
        self.assertIsNone(added["discovery_error"], added["discovery_error"])
        provider_id = fleet.provider_id_for(str(added["controller"]["id"]))
        definition = registry.definition(provider_id)
        rows = registry.models.models_for(provider_id, include_disabled=True)
        ready = {m["model_id"] for m in rows
                 if m["raw"].get("files_state") == "files_verified"
                 and m["raw"].get("dispatch_state") == "eligible"
                 and m["raw"].get("catalogued") is not False
                 and registry.models.can_publish(definition, m)}
        self.assertTrue(ready, "the mock Hub serves installed, ready models")
        self.assertLess(len(ready), len(rows), "and some that are not, so the filter is exercised")
        on = {m["model_id"] for m in rows if m["enabled"]}
        self.assertEqual(on, ready)
        seed = added["enable_seed"]
        self.assertEqual(seed["enabled"], len(ready), seed)
        self.assertIn(f"{len(ready)} installed", seed["sentence"])

        # **Re-adding is not re-seeding.**
        switched_off = sorted(ready)[0]
        registry.models.set_model_enabled(provider_id, switched_off, False)
        again = admin.fleet_add_controller(
            {"label": "Second Mac", "url": self.base, "token": "fleet-token"}
        )
        self.assertFalse(again["controller"]["created"], again["controller"])
        self.assertIsNone(again.get("enable_seed"), again)
        after = {m["model_id"]: m["enabled"] for m in registry.models.models_for(provider_id, include_disabled=True)}
        self.assertFalse(after[switched_off], "an existing location's own decisions must survive a re-add")


def _row(model_id: str, *, files: str = "files_verified", dispatch: str = "eligible",
         catalogued: bool = True, package_id: str | None = None) -> DiscoveredModel:
    return DiscoveredModel(model_id=model_id, display_name=model_id, modality="tts", raw={
        "files_state": files, "dispatch_state": dispatch, "catalogued": catalogued,
        "package_id": package_id or model_id.split("/")[-1].lower()})


class TheRuleItself(unittest.TestCase):
    """One inventory, controlled catalogues: the edges the mock cannot serve."""

    def _inventory(self, tmp: str, catalogues: dict[str, list[DiscoveredModel]]) -> models_module.ModelInventory:
        definitions = {
            LOCAL: _definition(LOCAL, auto_enable=True),
            "studio-hub-b": _definition("studio-hub-b", auto_enable=False),
        }
        inventory = models_module.ModelInventory(
            CredentialStore(path=Path(tmp) / "credentials.json"),
            path=Path(tmp) / "models-cache.json",
            definition_lookup=lambda provider_id: definitions.get(provider_id),
            definitions_lookup=lambda: list(definitions.values()),
        )

        def discover(definition: ProviderDefinition, _values: dict) -> DiscoveryResult:
            return DiscoveryResult(models=list(catalogues.get(definition.id, [])))

        with patch.object(models_module, "discover", side_effect=discover):
            for provider_id in catalogues:
                inventory.refresh(provider_id)
        return inventory

    def _seed(self, inventory: models_module.ModelInventory) -> dict:
        with patch.object(registry, "models", inventory), \
                patch.object(registry, "definition", inventory._definition):  # noqa: SLF001
            return fleet.seed_enabled_installed("studio-hub-b")

    def _stored(self, tmp: str) -> dict:
        return json.loads((Path(tmp) / "models-cache.json").read_text())["enabled"].get("studio-hub-b", {})

    def test_installed_on_the_rest_off_and_ruled_out_models_stay_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(tmp, {LOCAL: [_row("local/only")], "studio-hub-b": [
                _row("org/installed"),
                _row("org/not-installed", files="not_installed", dispatch="ineligible"),
                _row("org/a-dependency", catalogued=False),
                _row("selcukkubur/Phonon-2-mlx", package_id="phonon-2-mlx"),
                _row("mlx-community/pocket-tts-4bit", package_id="pocket-tts-4bit"),
                _row("mlx-community/chatterbox-8bit"),
            ]})
            report = self._seed(inventory)
            decisions = {m["model_id"]: m["enabled"] for m in inventory.models_for("studio-hub-b")}
            self.assertEqual({k for k, v in decisions.items() if v}, {"org/installed"})
            self.assertEqual(self._stored(tmp), {"org/installed": True}, "only the switch-on is written")
            self.assertEqual((report["enabled"], report["ruled_out"]), (1, 3), report)
            self.assertIn("ruled out by the owner left off", report["sentence"])

    def test_nothing_is_copied_from_this_mac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(tmp, {
                LOCAL: [_row("shared"), _row("local-on")],
                "studio-hub-b": [_row("shared"), _row("absent-here", files="not_installed",
                                                     dispatch="ineligible")]})
            inventory.set_model_enabled(LOCAL, "shared", False)          # off here: still on there
            inventory.set_model_enabled(LOCAL, "absent-here", True)      # on here: not installed there
            self._seed(inventory)
            self.assertEqual(self._stored(tmp), {"shared": True})
            local = {m["model_id"]: m["enabled"] for m in inventory.models_for(LOCAL)}
            self.assertEqual(local, {"shared": False, "local-on": True}, "This Mac's switches are untouched")

    def test_an_existing_decision_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(tmp, {"studio-hub-b": [_row("kept-off"), _row("new")]})
            inventory.set_model_enabled("studio-hub-b", "kept-off", False)
            self._seed(inventory)
            self.assertEqual(self._stored(tmp), {"kept-off": False, "new": True})

    def test_a_location_that_reported_nothing_is_not_given_a_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self._seed(self._inventory(tmp, {LOCAL: [_row("a")]}))
            self.assertEqual(report["models_discovered"], 0)
            self.assertEqual(report["sentence"], "No models were discovered here, so nothing was switched on.")


class TheSentenceReachesThePageThatAddsALocation(unittest.TestCase):
    """The add result's sentence is drawn, not only computed.

    `fleet.seed_enabled_installed` returns the counts *and* the sentence built from
    them, `/admin/api/fleet/controllers` carries it as `enable_seed`, and the menubar
    app drew only `discovered`/`discovery_error` — so the one moment the owner is told
    what a new Mac inherited passed without saying it. `enable_seed` is `null` for a
    re-add (whose own decisions are deliberately kept), which is why the app has to
    guard it rather than assume it is there.
    """

    def test_the_app_reads_enable_seed_and_draws_its_sentence(self) -> None:
        source = (_ROOT / "menubar" / "FleetSection.swift").read_text(encoding="utf-8")
        self.assertIn('payload["enable_seed"] as? [String: Any]', source)
        self.assertIn('enableSeed["sentence"] as? String', source)
        # A failed seeding has no sentence: it must say so rather than reading as
        # "nothing to inherit".
        self.assertIn('enableSeed["error"] as? String', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
