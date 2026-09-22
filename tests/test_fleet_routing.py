#!/usr/bin/env python3
"""Fleet routing: weight a model by the machines that can actually run it.

The router used to weight a controller by **every** machine at the site, so a
controller with ten 8 GB image Macs and one 24 GB Mac out-weighed a controller
with two 24 GB Macs for a model only the big ones can run. These checks pin the
per-model weight the Hub's `model_supply` now feeds, its fallbacks for an older
Hub, and the `GATEWAY_PREFER_FASTEST_SITE` toggle.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-fleet-routing-"))
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
    }
)

from gateway import fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402


MODEL = "mlx-community/heavy-image-model"
MODALITY = "image"


def _definition(location_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{location_id}")


def _model_row() -> dict:
    return {"model_id": MODEL, "modality": MODALITY, "enabled": True, "raw": {}}


def _location(location_id: str, **overrides) -> dict:
    row = {
        "id": location_id,
        "label": location_id.title(),
        "reachable": True,
        "capacity": {},
        "model_supply": {},
        "machines": [],
        "machine_summary": {"reachable": 0},
        "running_jobs": [],
    }
    row.update(overrides)
    return row


def _route_with(locations: list[dict], model_id: str = MODEL, modality: str = MODALITY):
    def snapshot(allow_stale: bool = False) -> dict:
        return {"locations": locations}

    definitions = [_definition(row["id"]) for row in locations]

    def enabled(modality_name: str):
        assert modality_name == modality, modality_name
        return [(definition, _model_row()) for definition in definitions]

    with patch.object(fleet, "snapshot", snapshot), \
            patch.object(registry.models, "enabled_models", enabled):
        return fleet.routing_candidates(model_id, modality)


class PerModelWeightTests(unittest.TestCase):
    def test_weight_counts_only_the_machines_that_fit_the_model(self) -> None:
        # A: ten 8 GB image Macs + one 24 GB. Site-wide capacity is 11, but only
        # one machine can run this model. B: two 24 GB Macs, both fitting.
        a = _location(
            "a",
            capacity={"eligible_physical_machine_slots_total": 11, "queue_depth": 0},
            model_supply={MODEL: {"eligible": 1, "available": 1}},
        )
        b = _location(
            "b",
            capacity={"eligible_physical_machine_slots_total": 2, "queue_depth": 0},
            model_supply={MODEL: {"eligible": 2, "available": 2}},
        )
        slots = {c["location_id"]: c["slots"] for c in _route_with([a, b])}
        self.assertEqual(slots, {"a": 1, "b": 2}, "the site-wide total must not dilute the weight")

    def test_pressure_uses_the_fitting_machine_count(self) -> None:
        # Equal queues: A's 11 machines would read as low pressure; its one fitting
        # machine is the truth, so B (two fitting machines) is the less pressured.
        a = _location(
            "a",
            capacity={"eligible_physical_machine_slots_total": 11, "queue_depth": 2},
            model_supply={MODEL: {"eligible": 1, "available": 1}},
        )
        b = _location(
            "b",
            capacity={"eligible_physical_machine_slots_total": 2, "queue_depth": 2},
            model_supply={MODEL: {"eligible": 2, "available": 2}},
        )
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_falls_back_to_operation_then_site_total(self) -> None:
        # No per-model row: the per-operation capacity is next best.
        a = _location(
            "a",
            capacity={
                "eligible_physical_machine_slots_total": 9,
                "queue_depth": 0,
                "by_operation": {"image.text_to_image": {"eligible_physical_machine_slots_total": 3}},
            },
        )
        # Then the old site-wide total, for a Hub that publishes neither.
        b = _location("b", capacity={"eligible_physical_machine_slots_total": 5, "queue_depth": 0})
        slots = {c["location_id"]: c["slots"] for c in _route_with([a, b])}
        self.assertEqual(slots, {"a": 3, "b": 5})

    def test_available_and_speed_are_read_from_the_hub(self) -> None:
        a = _location(
            "a",
            model_supply={MODEL: {"eligible": 2, "available": 1}},
            machines=[{"chip": "Apple M4 Pro", "memory_gb": 24.0}],
        )
        candidate, = _route_with([a])
        self.assertEqual(candidate["available"], 1)
        self.assertEqual(candidate["speed"], 424.0)  # 4 * 100 + 24

    def test_missing_supply_is_not_zero(self) -> None:
        # A Hub that says nothing about free capacity must not read as "none free".
        a = _location("a", capacity={"eligible_physical_machine_slots_total": 2})
        candidate, = _route_with([a])
        self.assertIsNone(candidate["available"])
        self.assertEqual(candidate["slots"], 2)


class RankTests(unittest.TestCase):
    def test_free_capacity_breaks_a_pressure_tie(self) -> None:
        # Same queue and same fitting count; only one site has a Mac free now.
        a = _location("a", model_supply={MODEL: {"eligible": 2, "available": 0}})
        b = _location("b", model_supply={MODEL: {"eligible": 2, "available": 3}})
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_prefer_fastest_site_ranks_the_faster_controller_first(self) -> None:
        # Equal pressure and equal capacity; only the chip differs (the Hub's proxy).
        a = _location(
            "a",
            model_supply={MODEL: {"eligible": 2, "available": 2}},
            machines=[{"chip": "Apple M2", "memory_gb": 8.0}],
        )
        b = _location(
            "b",
            model_supply={MODEL: {"eligible": 2, "available": 2}},
            machines=[{"chip": "Apple M4 Pro", "memory_gb": 16.0}],
        )
        with patch.dict(os.environ, {"GATEWAY_PREFER_FASTEST_SITE": "1"}), \
                patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_speed_score_mirrors_the_hub_proxy(self) -> None:
        self.assertEqual(fleet._site_speed_score([{"chip": "Apple M2", "memory_gb": 8.0}]), 208.0)
        self.assertEqual(
            fleet._site_speed_score(
                [{"chip": "Apple M2", "memory_gb": 8.0}, {"chip": "Apple M4 Pro", "memory_gb": 24.0}]
            ),
            424.0,
        )
        self.assertEqual(fleet._site_speed_score([]), 0.0)
        self.assertEqual(fleet._site_speed_score(None), 0.0)


if __name__ == "__main__":
    unittest.main()
