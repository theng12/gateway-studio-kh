#!/usr/bin/env python3
"""A model installed later on a location switches on by itself.

The owner's rule (2026-10-31): *"models installed should be on."* The seeding that
gave a **new** location its installed models (`seed_enabled_installed`, 2026-10-09) ran
exactly once — when the roster row was created. A model installed on a Mac that was
already on the roster kept no decision at all, and "no decision" on a location reads as
off, so it stayed off for ever with nobody to notice.

The fix is to re-run the *same* seeding after every read of a location's inventory that
answered (`ModelInventory.refresh` → `fleet.seed_installed_after_refresh`). This module
holds the edges that must not blur:

  * an owner's **off is a decision** — `false` in `models_cache.json`, not the absence of
    a key — and a decision is never rewritten;
  * a location the owner **paused by hand stays paused** and takes no work; models being
    on must not resume it;
  * `NEVER_AUTO_ENABLED`, retired models and dependencies stay off, and a model that is
    no longer installed or ready gets **no** decision written for it;
  * a **failed or partial read writes nothing** — a Hub that did not answer is not a Hub
    saying "nothing is installed";
  * the rule is **idempotent**: a second refresh writes nothing the first had not.

Run with::

    .venv/bin/python tests/test_installed_models_switch_on.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real data folder.
_SANDBOX = sandbox_env()

from gateway import admin, fleet  # noqa: E402
from gateway.providers import models as models_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.discovery import DiscoveryResult, DiscoveredModel  # noqa: E402

#: The controller this module adds to the roster, and the provider id derived from it.
CONTROLLER = "b"
LOCATION = fleet.provider_id_for(CONTROLLER)
MODEL_CACHE = Path(os.environ["GATEWAY_MODEL_CACHE"])


def _model(model_id: str, *, files: str = "files_verified", dispatch: str = "eligible",
           catalogued: bool | None = True, package_type: str = "model") -> DiscoveredModel:
    raw: dict = {"files_state": files, "dispatch_state": dispatch, "package_type": package_type}
    if catalogued is not None:
        raw["catalogued"] = catalogued
    return DiscoveredModel(model_id=model_id, display_name=model_id, modality="tts", raw=raw)


class InstallsLaterSwitchOn(unittest.TestCase):
    """The owner's rule: a model installed on a Mac that is already a location turns on by itself."""

    def setUp(self) -> None:
        MODEL_CACHE.unlink(missing_ok=True)
        fleet.invalidate()
        fleet._write([{"id": CONTROLLER, "label": "Blue Room", "url": "http://127.0.0.1:9"}])
        fleet.invalidate()
        self._rows: list[DiscoveredModel] = []
        self.addCleanup(self._forget)

    def _forget(self) -> None:
        fleet._forget_models(CONTROLLER)
        fleet._write([])
        fleet.invalidate()
        MODEL_CACHE.unlink(missing_ok=True)

    def _refresh(self, *rows: DiscoveredModel, error: str | None = None,
                 catalogue_missing: bool = False) -> DiscoveryResult:
        from unittest.mock import patch

        self._rows = list(rows)

        def discover(definition, _values):
            return DiscoveryResult(models=list(self._rows), error=error,
                                   catalogue_missing=catalogue_missing)

        with patch.object(models_module, "discover", discover):
            return registry.refresh(LOCATION)

    def _on(self) -> set[str]:
        return {m["model_id"] for m in registry.models.models_for(LOCATION, include_disabled=True)
                if m["enabled"]}

    def _stored(self) -> dict:
        return json.loads(MODEL_CACHE.read_text(encoding="utf-8")).get("enabled", {}).get(LOCATION, {})

    def test_a_model_installed_later_switches_on_at_the_first_refresh_after_it(self) -> None:
        self._refresh(_model("org/first"))
        self.assertEqual(self._on(), {"org/first"})

        # The owner installs a second model on a Mac in that same location.
        result = self._refresh(_model("org/first"), _model("org/second"))
        self.assertIsNone(result.error)
        self.assertEqual(self._on(), {"org/first", "org/second"},
                         "a model installed later on an existing location switches on by itself")

    def test_an_owners_off_is_a_decision_and_is_never_rewritten(self) -> None:
        self._refresh(_model("org/first"))
        registry.models.set_model_enabled(LOCATION, "org/first", False)

        stored = self._stored()
        self.assertIn("org/first", stored, "an off is a decision, not the absence of a key")
        self.assertIs(stored["org/first"], False)

        result = self._refresh(_model("org/first"), _model("org/second"))
        self.assertIsNone(result.error)
        self.assertEqual(self._on(), {"org/second"},
                         "a stored explicit off is never overwritten")
        self.assertIs(self._stored()["org/first"], False, "the decision is still recorded, not erased")

    def test_a_location_paused_by_hand_stays_paused_and_takes_no_work(self) -> None:
        fleet.set_owner_pause(CONTROLLER, True)
        self.addCleanup(fleet.set_owner_pause, CONTROLLER, False)

        result = self._refresh(_model("org/first"))
        self.assertIsNone(result.error)
        self.assertEqual(self._on(), {"org/first"}, "the rule still applies to a paused location")

        self.assertIsNotNone(fleet.owner_paused_at(CONTROLLER), "models being on must not resume it")
        row = {"id": CONTROLLER}
        self.assertFalse(fleet._takes_customer_jobs(row),
                         "a paused location takes no work whatever its models say")

    def test_ruled_out_retired_and_uninstalled_models_get_no_decision(self) -> None:
        retired = "baa-ai/ltx-2.3-22b-ram-12gb-mlx"  # gateway/providers/models.RETIRED_MODEL_IDS
        result = self._refresh(
            _model("org/ready"),
            _model("org/not-installed", files="not_installed", dispatch="ineligible"),
            _model("org/dependency", catalogued=False, package_type="dependency"),
            _model(f"selcukkubur/Phonon-2-mlx", package_type="phonon-2-mlx"),
            _model(retired),
        )
        self.assertIsNone(result.error)
        self.assertEqual(self._on(), {"org/ready"},
                         "NEVER_AUTO_ENABLED, retired, uninstalled and dependency models stay off")

        stored = self._stored()
        for model_id in ("org/not-installed", "org/dependency", "selcukkubur/Phonon-2-mlx", retired):
            self.assertNotIn(model_id, stored, f"{model_id} must get no decision written at all")

    def test_a_model_that_stops_being_installed_gets_no_new_decision(self) -> None:
        self._refresh(_model("org/first"), _model("org/second"))
        registry.models.set_model_enabled(LOCATION, "org/second", False)
        before = dict(self._stored())

        # The Mac frees the disk: the Hub no longer reports the model as installed.
        self._refresh(_model("org/first"))
        self.assertEqual(self._stored(), before,
                         "a model that is no longer installed gets nothing written for it")

    def test_a_failed_read_writes_nothing(self) -> None:
        self._refresh(_model("org/first"))
        before = dict(self._stored())

        result = self._refresh(error="network: no route")
        self.assertIsNotNone(result.error)
        self.assertEqual(self._stored(), before, "a read failure is not 'nothing installed'")

    def test_a_partial_read_writes_nothing(self) -> None:
        result = self._refresh(_model("org/first"), catalogue_missing=True)
        self.assertIsNone(result.error)
        self.assertEqual(self._stored(), {}, "a partial read is not an inventory that says what is ready")

    def test_repeated_refreshes_write_nothing_the_second_time(self) -> None:
        self._refresh(_model("org/first"))
        first = dict(self._stored())

        for _ in range(3):
            result = self._refresh(_model("org/first"))
            self.assertIsNone(result.error)
        self.assertEqual(self._stored(), first, "no repeated writes when nothing changed")
        self.assertEqual(self._on(), {"org/first"})

    def test_the_models_page_shows_the_new_on_state_without_a_restart(self) -> None:
        """The console's Models page reads the same inventory, live."""
        self._refresh(_model("org/first"))
        # No restart of anything: the page's own route is asked again.
        rows = admin.search_models(limit=400).get("models", [])
        merged = [r for r in rows if r["model_id"] == "org/first"]
        self.assertTrue(merged, "a switched-on location model is listed by the Models page")
        self.assertTrue(merged[0]["enabled"], "and the page shows it on")


class OffIsADecision(unittest.TestCase):
    """The one place an owner's off was not a decision: the remove route."""

    def setUp(self) -> None:
        MODEL_CACHE.unlink(missing_ok=True)
        fleet.invalidate()
        fleet._write([{"id": CONTROLLER, "label": "Blue Room", "url": "http://127.0.0.1:9"}])
        fleet.invalidate()
        self.addCleanup(self._forget)

    def _forget(self) -> None:
        fleet._forget_models(CONTROLLER)
        fleet._write([])
        fleet.invalidate()
        MODEL_CACHE.unlink(missing_ok=True)

    def test_removing_a_discovered_model_keeps_the_owners_off(self) -> None:
        """A model the owner switched off must not be turned back into 'never decided'.

        `remove_user_model` used to pop the decision for *any* model id. For a discovered
        model that is only the owner's off disappearing, and the next seeding pass would
        switch it on again — an off that flip back on.
        """
        registry.models.set_model_enabled(LOCATION, "org/first", False)
        registry.models.remove_user_model(LOCATION, "org/first")

        stored = json.loads(MODEL_CACHE.read_text(encoding="utf-8"))["enabled"][LOCATION]
        self.assertIn("org/first", stored, "removing a model this route never added keeps its decision")
        self.assertIs(stored["org/first"], False)

    def test_removing_a_model_the_route_added_clears_its_decision(self) -> None:
        registry.models.add_user_model(LOCATION, "org/manual", "Manual", "tts")
        self.addCleanup(registry.models.remove_user_model, LOCATION, "org/manual")
        registry.models.remove_user_model(LOCATION, "org/manual")

        stored = json.loads(MODEL_CACHE.read_text(encoding="utf-8"))["enabled"].get(LOCATION, {})
        self.assertNotIn("org/manual", stored, "a model removed is no longer a model with a decision")


if __name__ == "__main__":
    unittest.main(verbosity=2)
