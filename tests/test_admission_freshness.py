"""Admission must not sum a fleet view routing would refuse (G12).

Routing rejects a snapshot older than `fleet._ROUTING_VIEW_MAX_AGE_S`; admission and
the displayed capacity used to read the same `allow_stale` snapshot with no age check.
These tests pin the degraded admission answer, the unchanged fresh answer, and the
freshness the display publishes beside its sum.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-admission-freshness-")

from gateway import concurrency, fleet  # noqa: E402
from gateway.worker import Worker  # noqa: E402

PROVIDER = "studio-hub-test"
OPERATION = "image.text_to_image"
BOUND = fleet._ROUTING_VIEW_MAX_AGE_S


def _row(slots: int = 7) -> dict:
    return {
        "provider_id": PROVIDER,
        "reachable": True,
        "capacity": {"by_operation": {OPERATION: {"available_physical_machine_slots": slots}}},
    }


class _FreshnessCase(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "provider-concurrency.local.json"
        settings = patch.object(concurrency, "SETTINGS_PATH", self.path)
        settings.start()
        self.addCleanup(settings.stop)

    def _view(self, age_s: float, slots: int = 7) -> dict:
        return {"locations": [_row(slots)], "generated_at": time.time() - age_s}


class AdmissionRefusesAStaleViewTest(_FreshnessCase):
    def _budget(self, *, age_s: float, slots: int = 7, stated: int | None = None):
        self.path.write_text(json.dumps(
            {"providers": {PROVIDER: {"limit": stated, "note": "paid plan"}}}
            if stated is not None else {}
        ), encoding="utf-8")
        concurrency.reset_for_tests()
        with patch.object(fleet, "snapshot", lambda **_: self._view(age_s, slots)):
            return Worker(None, interval_s=10.0)._provider_budget(PROVIDER, "image")

    def test_stale_capacity_degrades_to_the_stated_budget_with_its_reason(self) -> None:
        budget = self._budget(age_s=BOUND + 180, slots=7, stated=2)
        self.assertEqual((budget.limit, budget.source), (2, "curated"))
        self.assertNotEqual(budget.limit, 7)
        self.assertEqual(
            budget.detail,
            f"fleet read is {int(BOUND + 180)} s old, past the {int(BOUND)} s routing bound; "
            "no live budget, so using the stated/cloud one: paid plan",
        )

    def test_a_fresh_snapshot_still_returns_todays_budget(self) -> None:
        budget = self._budget(age_s=5, slots=7)
        expected = concurrency.hub_budget(7, operation=OPERATION, ceiling=None, reachable=True)
        self.assertEqual(budget, expected)
        self.assertEqual((budget.limit, budget.source), (7, "hub"))


class DisplayedCapacityStatesItsAgeTest(_FreshnessCase):
    def _capacity(self, age_s: float) -> dict:
        with patch.object(fleet, "snapshot", lambda **_: self._view(age_s, 3)):
            return concurrency.live_capacity()

    def test_stale_figure_says_when_it_was_made_and_is_not_current(self) -> None:
        stale = self._capacity(BOUND + 300)
        self.assertTrue(stale["stale"])
        self.assertGreaterEqual(stale["age_seconds"], BOUND)
        self.assertIsInstance(stale["generated_at"], float)
        self.assertIn("not current", stale["source"])

        fresh = self._capacity(5)
        self.assertFalse(fresh["stale"])
        self.assertLess(fresh["age_seconds"], BOUND)
        self.assertEqual(fresh["source"], "the live fleet, summed over reachable locations")
        self.assertEqual(fresh["by_modality"]["image"], 3)

    def test_rows_from_the_fanout_are_stamped_without_rereading_the_fleet(self) -> None:
        with patch.object(fleet, "snapshot", side_effect=AssertionError("re-entered the fan-out")):
            capacity = concurrency.live_capacity(rows=[_row(3)])
        self.assertIsInstance(capacity["generated_at"], float)
        self.assertFalse(capacity["stale"])
        self.assertEqual(capacity["by_modality"]["image"], 3)


if __name__ == "__main__":
    unittest.main()
