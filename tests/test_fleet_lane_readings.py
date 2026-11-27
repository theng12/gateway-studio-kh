#!/usr/bin/env python3
"""A fleet lane routes only to a site with a live, complete per-model reading.

The defect this pins is the one on `OPEN-WORK.md`: a site that has not answered for a model, or
whose `model_supply` is absent, stale or incomplete, was still counted as able. Routing offered
it, admission gave it room, and the job waited there with nothing saying the site had never
answered the question.

Four shapes, and they are not one:

  * missing supply — a live site with per-Mac rows but no `model_supply` for the model;
  * stale supply — a row whose last reading is past the routing bound;
  * incomplete supply — an entry without both `eligible` and `available`;
  * the Macs at the site do not hold the model, whatever the supply says.

"We cannot tell" stays `unknown`; "this site cannot run it" stays `model_not_installed`. A Hub
that publishes no per-Mac facts at all keeps the older-Hub fallback: its lack of detail is not a
reading, and it is not a refusal either.
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env(prefix="gateway-fleet-lane-readings-")

from gateway import concurrency, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

SITE = "alpha"
MODEL = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"
OTHER = "mlx-community/other-model"
MODALITY = "chat"
PROVIDER = fleet.provider_id_for(SITE)


def _local(*model_ids: str, **extra) -> list[dict]:
    return [{"model_id": model_id, "model": model_id, "machine": "mac-1",
             "modality": MODALITY, "memory_eligible": True, "available": True, **extra}
            for model_id in model_ids]


def _row(*, supply: dict | None, local_models: list[dict] | None = None,
         answered_at: float | None = None) -> dict:
    now = time.time()
    return {
        "id": SITE,
        "provider_id": PROVIDER,
        "label": "Site Alpha",
        "reachable": True,
        "answered_at": now if answered_at is None else answered_at,
        "machines": [{"machine": "mac-1", "reachable": True, "enabled": True,
                      "node_last_seen_at": now}],
        "nodes": [],
        "local_models": local_models or [],
        "workers": [],
        "running_jobs": [],
        "capacity": {"queue_depth": 0, "eligible_physical_machine_slots_total": 4},
        "model_supply": supply if supply is not None else {},
    }


def _enabled(modality: str):
    if modality != MODALITY:
        return []
    definition = types.SimpleNamespace(id=PROVIDER, kind="studio_hub", display_name="Studio Hub")
    return [(definition, {"model_id": MODEL, "modality": MODALITY, "enabled": True, "raw": {}})]


class _Base(unittest.TestCase):
    def candidates(self, row: dict):
        rejections: list[dict] = []
        with patch.object(fleet, "snapshot", lambda **_kw: {"locations": [row]}), \
                patch.object(registry.models, "enabled_models", _enabled):
            found = fleet.routing_candidates(MODEL, MODALITY, rejections=rejections)
        return found, rejections

    def reason(self, row: dict) -> tuple[bool, str]:
        with patch.object(fleet, "snapshot", lambda **_kw: {"locations": [row]}):
            return fleet.site_cannot_take(PROVIDER, MODEL), fleet.site_wait_reason(PROVIDER, MODEL)


class MissingSupplyTest(_Base):
    def test_a_live_site_without_a_model_supply_is_unknown_not_able(self) -> None:
        row = _row(supply={}, local_models=_local(MODEL))
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])
        self.assertIn("model_supply", rejections[0]["detail"])
        cannot, reason = self.reason(row)
        self.assertFalse(cannot, "unknown is not the same as this site cannot run it")
        self.assertIn("fresh reading", reason)
        self.assertNotIn("can run", reason)


class StaleSupplyTest(_Base):
    def test_a_row_past_the_routing_bound_is_unknown_not_able(self) -> None:
        row = _row(supply={MODEL: {"eligible": 2, "available": 2}},
                   local_models=_local(MODEL),
                   answered_at=time.time() - fleet._ROUTING_VIEW_MAX_AGE_S - 30)
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])
        self.assertIn("old", rejections[0]["detail"])
        cannot, reason = self.reason(row)
        self.assertFalse(cannot)
        self.assertIn("fresh reading", reason)


class IncompleteSupplyTest(_Base):
    def test_an_entry_without_available_is_unknown_not_able(self) -> None:
        row = _row(supply={MODEL: {"eligible": 2}}, local_models=_local(MODEL))
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])
        self.assertIn("incomplete", rejections[0]["detail"])
        cannot, reason = self.reason(row)
        self.assertFalse(cannot)
        self.assertIn("fresh reading", reason)

    def test_a_non_object_entry_is_incomplete_not_a_crash(self) -> None:
        row = _row(supply={MODEL: []}, local_models=_local(MODEL))
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])
        self.assertIn("incomplete", rejections[0]["detail"])

    def test_a_negative_count_is_incomplete_not_a_live_reading(self) -> None:
        row = _row(supply={MODEL: {"eligible": -1, "available": 1}},
                   local_models=_local(MODEL))
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])


class ZeroReadGraceTest(_Base):
    def test_a_positive_reading_still_arms_the_transient_zero_grace(self) -> None:
        fleet._ELIGIBLE_AT.clear()
        now = time.time()
        healthy = _row(supply={MODEL: {"eligible": 2, "available": 2}},
                       local_models=_local(MODEL), answered_at=now)
        self.assertEqual(fleet.model_supply_state(healthy, MODEL, now)[0], "live")
        blip = _row(supply={MODEL: {"eligible": 0, "available": 0}},
                    local_models=_local(MODEL), answered_at=now + 10)
        self.assertEqual(fleet.model_supply_state(blip, MODEL, now + 10)[0], "live")
        later = _row(supply={MODEL: {"eligible": 0, "available": 0}},
                     local_models=_local(MODEL),
                     answered_at=now + fleet.LOCK_ANSWER_GRACE_S + 1)
        self.assertEqual(fleet.model_supply_state(
            later, MODEL, now + fleet.LOCK_ANSWER_GRACE_S + 1)[0], "cannot")

    def test_the_zero_grace_does_not_rescue_a_site_whose_macs_do_not_hold_the_model(self) -> None:
        fleet._ELIGIBLE_AT.clear()
        now = time.time()
        healthy = _row(supply={MODEL: {"eligible": 1, "available": 1}},
                       local_models=_local(MODEL), answered_at=now)
        fleet.model_supply_state(healthy, MODEL, now)
        wrong_mac = _row(supply={MODEL: {"eligible": 0, "available": 0}},
                         local_models=_local(OTHER), answered_at=now + 10)
        self.assertEqual(fleet.model_supply_state(wrong_mac, MODEL, now + 10)[0], "cannot")


class MacDoesNotHoldModelTest(_Base):
    def test_a_supply_entry_without_a_mac_holding_the_model_cannot_take(self) -> None:
        row = _row(supply={MODEL: {"eligible": 1, "available": 1}},
                   local_models=_local(OTHER))
        found, rejections = self.candidates(row)
        self.assertEqual(found, [])
        self.assertEqual([r["reason"] for r in rejections], ["model_not_installed"])
        self.assertIn("do not hold", rejections[0]["detail"])
        cannot, reason = self.reason(row)
        self.assertTrue(cannot)
        self.assertIn("can run", reason)


class LiveReadingTest(_Base):
    def test_a_complete_live_reading_is_still_a_candidate(self) -> None:
        row = _row(supply={MODEL: {"eligible": 2, "available": 2}},
                   local_models=_local(MODEL))
        found, rejections = self.candidates(row)
        self.assertEqual([c["location_id"] for c in found], [SITE], rejections)
        cannot, _ = self.reason(row)
        self.assertFalse(cannot)


class EligibleMacsNeedALiveReadingTest(_Base):
    """The `OPEN-WORK.md` shape: a Mac with no live reading was counted in a capacity figure."""

    def test_a_machine_the_site_did_not_list_is_not_eligible(self) -> None:
        row = _row(supply={MODEL: {"eligible": 1, "available": 1}},
                   local_models=_local(MODEL))
        row["machines"] = [{"machine": "mac-2", "reachable": True,
                            "node_last_seen_at": time.time()}]
        self.assertEqual(fleet._eligible_macs(row, MODEL, MODALITY), {})

    def test_a_stale_heartbeat_is_not_eligible(self) -> None:
        row = _row(supply={MODEL: {"eligible": 1, "available": 1}},
                   local_models=_local(MODEL))
        row["machines"][0]["node_last_seen_at"] = time.time() - fleet.ONLINE_WINDOW_S - 1
        self.assertEqual(fleet._eligible_macs(row, MODEL, MODALITY), {})

    def test_a_live_reading_is_still_eligible(self) -> None:
        row = _row(supply={MODEL: {"eligible": 1, "available": 1}},
                   local_models=_local(MODEL))
        self.assertEqual(set(fleet._eligible_macs(row, MODEL, MODALITY)), {"mac-1"})


class AdmissionUsesTheSameReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Path(tmp.name) / "provider-concurrency.local.json"
        settings.write_text('{"global": 50}', encoding="utf-8")
        self.sites: list[dict] = []
        for patcher in (
            patch.object(concurrency, "SETTINGS_PATH", settings),
            patch.object(fleet, "snapshot", lambda **_kw: {"locations": self.sites}),
            patch.object(registry.models, "enabled_models", _enabled),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store, interval_s=10.0, pool_size=0)

    def job(self) -> dict:
        row, _ = self.store.submit(modality=MODALITY, model_id=MODEL, params={},
                                   idempotency_key="lane-reading", provider_id=PROVIDER)
        return self.store.get(row["id"])

    def test_a_site_with_no_model_supply_gets_no_room(self) -> None:
        self.sites[:] = [_row(supply={}, local_models=_local(MODEL))]
        self.assertEqual(self.worker._job_room(self.job(), PROVIDER), 0)

    def test_a_complete_live_reading_keeps_its_room(self) -> None:
        self.sites[:] = [_row(supply={MODEL: {"eligible": 2, "available": 2}},
                             local_models=_local(MODEL))]
        self.assertGreater(self.worker._job_room(self.job(), PROVIDER), 0)


if __name__ == "__main__":
    unittest.main()
