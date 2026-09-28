#!/usr/bin/env python3
"""A newly added location inherits this Mac's own model decisions.

The defect this holds shut: adding a controller seeded **nothing**, because a
remote location is a provider with `auto_enable_discovered=False` (right for a
cloud vendor's 300-row catalogue, wrong for a Mac running the same Hub packages
as this one). So the owner ticked a model here, believed the fleet offered it,
and every other Mac went on not serving it — measured live, five controllers
advertising 19 models each with 5/5/2/2/2 enabled.

The rule, and the edges it must not blur:

  * a model the **local Hub has enabled** is enabled on the new location;
  * a model the **local Hub has disabled** is recorded as disabled there too —
    the decision travels in both directions, not only the convenient one;
  * a model the **local Hub has never seen** is left alone. An absent decision is
    not a decision, and a remote provider's default is off;
  * **re-adding** a location rewrites nothing. Only a new one is seeded, or
    pasting a token again would silently undo choices made on that Mac.

The first class drives the real add route against the suite's mock Studio Hub
(`tests/mock_upstream.py`) — never the owner's Macs. The second drives the rule
itself with a controlled inventory, because the mock serves one catalogue to
every location and the "never seen here" edge needs one location to know a model
the other does not.

Run with::

    .venv/bin/python tests/test_fleet_inherit_models.py
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

    def test_a_new_location_gets_exactly_the_decisions_the_local_hub_has(self) -> None:
        discovered = registry.refresh(LOCAL)
        self.assertIsNone(discovered.error, discovered.error)
        local_ids = [
            model["model_id"]
            for model in registry.models.models_for(LOCAL, include_disabled=True)
        ]
        self.assertGreaterEqual(len(local_ids), 3, local_ids)

        # Two kinds of "on" and one "off", so the rule is tested where it is
        # easiest to get quietly wrong: a model left at the local Hub's own
        # default is *on* (its `auto_enable_discovered`), and inheriting only the
        # explicit map would leave it off on every other Mac.
        switched_off = local_ids[0]
        registry.models.set_model_enabled(LOCAL, switched_off, False)

        added = admin.fleet_add_controller(
            {"label": "Second Mac", "url": self.base, "token": "fleet-token"}
        )
        self.assertTrue(added["controller"]["created"], added["controller"])
        self.assertIsNone(added["discovery_error"], added["discovery_error"])
        self.assertEqual(added["discovered"], len(local_ids))

        seed = added["enable_seed"]
        self.assertIsNotNone(seed, added)
        self.assertEqual(seed["source_provider_id"], LOCAL)
        self.assertEqual(seed["models_discovered"], len(local_ids))
        self.assertEqual(seed["enabled"], len(local_ids) - 1, seed)
        self.assertEqual(seed["disabled"], 1, seed)
        self.assertEqual(seed["no_decision"], 0, seed)
        self.assertIn(f"{len(local_ids) - 1} of {len(local_ids)}", seed["sentence"])

        provider_id = fleet.provider_id_for(str(added["controller"]["id"]))
        remote = {
            model["model_id"]: model["enabled"]
            for model in registry.models.models_for(provider_id, include_disabled=True)
        }
        local = {
            model["model_id"]: model["enabled"]
            for model in registry.models.models_for(LOCAL, include_disabled=True)
        }
        self.assertEqual(
            {model_id for model_id, on in remote.items() if on},
            {model_id for model_id, on in local.items() if on},
            f"the new location must serve exactly what this Mac's Hub serves: {remote}",
        )
        self.assertFalse(remote[switched_off], "a decision off here is a decision off there")

        # The decisions are in the one store, both directions recorded explicitly.
        stored = json.loads(Path(os.environ["GATEWAY_MODEL_CACHE"]).read_text())["enabled"]
        self.assertEqual(stored[provider_id][switched_off], False)
        self.assertTrue(all(stored[provider_id][m] is True for m in local_ids[1:]))

        # **Re-adding is not re-seeding.** An owner who switched a model off on
        # that Mac, then had to re-enter its token or let it move to a new
        # address, must not find the decision silently reversed.
        registry.models.set_model_enabled(provider_id, local_ids[1], False)
        again = admin.fleet_add_controller(
            {"label": "Second Mac", "url": self.base, "token": "fleet-token"}
        )
        self.assertFalse(again["controller"]["created"], again["controller"])
        self.assertIsNone(again.get("enable_seed"), again)
        after = {
            model["model_id"]: model["enabled"]
            for model in registry.models.models_for(provider_id, include_disabled=True)
        }
        self.assertFalse(
            after[local_ids[1]],
            "an existing location's own decisions must survive a re-add",
        )


class TheRuleItself(unittest.TestCase):
    """One inventory, two providers, controlled catalogues — the edges the mock cannot serve."""

    def _inventory(self, tmp: str, catalogues: dict[str, list[str]]) -> models_module.ModelInventory:
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
            return DiscoveryResult(
                models=[
                    DiscoveredModel(model_id=model_id, display_name=model_id, modality="tts")
                    for model_id in catalogues.get(definition.id, [])
                ]
            )

        with patch.object(models_module, "discover", side_effect=discover):
            for provider_id in catalogues:
                inventory.refresh(provider_id)
        return inventory

    def _seed(self, tmp: str, catalogues: dict[str, list[str]]):
        inventory = self._inventory(tmp, catalogues)
        with patch.object(registry, "models", inventory):
            report = fleet.seed_enabled_from_local("studio-hub-b")
        return inventory, report

    def test_an_enabled_decision_is_inherited_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalogues = {LOCAL: ["local-on", "local-off"], "studio-hub-b": ["local-on", "local-off"]}
            inventory = self._inventory(tmp, catalogues)
            inventory.set_model_enabled(LOCAL, "local-off", False)
            with patch.object(registry, "models", inventory):
                report = fleet.seed_enabled_from_local("studio-hub-b")

            decisions = {
                model["model_id"]: model["enabled"]
                for model in inventory.models_for("studio-hub-b", include_disabled=True)
            }
            self.assertEqual(decisions, {"local-on": True, "local-off": False})
            self.assertEqual((report["enabled"], report["disabled"], report["no_decision"]),
                             (1, 1, 0))
            stored = json.loads((Path(tmp) / "models-cache.json").read_text())["enabled"]
            self.assertEqual(stored["studio-hub-b"], {"local-on": True, "local-off": False})

    def test_a_model_the_local_hub_has_never_seen_stays_at_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory, report = self._seed(
                tmp,
                {LOCAL: ["shared"], "studio-hub-b": ["shared", "remote-only"]},
            )
            decisions = {
                model["model_id"]: model["enabled"]
                for model in inventory.models_for("studio-hub-b", include_disabled=True)
            }
            self.assertEqual(decisions, {"shared": True, "remote-only": False})
            # Nothing invented for the stranger: no entry at all, so a later
            # decision on that model reads as the owner's rather than as ours.
            stored = json.loads((Path(tmp) / "models-cache.json").read_text())["enabled"]
            self.assertEqual(stored["studio-hub-b"], {"shared": True})
            self.assertEqual(report["no_decision"], 1)
            self.assertIn("never seen", report["sentence"])

    def test_a_mac_with_no_local_hub_inherits_nothing_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _inventory, report = self._seed(tmp, {"studio-hub-b": ["a", "b"]})
            self.assertEqual((report["enabled"], report["disabled"], report["no_decision"]),
                             (0, 0, 2))
            self.assertIn("0 of 2", report["sentence"])

    def test_a_location_that_reported_nothing_is_not_given_a_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _inventory, report = self._seed(tmp, {LOCAL: ["a"]})
            self.assertEqual(report["models_discovered"], 0)
            self.assertEqual(
                report["sentence"],
                "No models were discovered here, so there was nothing to inherit.",
            )

    def test_the_local_hubs_own_decisions_are_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = self._inventory(
                tmp, {LOCAL: ["on", "off"], "studio-hub-b": ["on", "off"]}
            )
            inventory.set_model_enabled(LOCAL, "off", False)
            before = inventory.models_for(LOCAL, include_disabled=True)
            with patch.object(registry, "models", inventory):
                fleet.seed_enabled_from_local("studio-hub-b")
            self.assertEqual(inventory.models_for(LOCAL, include_disabled=True), before)


class TheSentenceReachesThePageThatAddsALocation(unittest.TestCase):
    """The add result's sentence is drawn, not only computed.

    `fleet.seed_enabled_from_local` returns the counts *and* the sentence built from
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
