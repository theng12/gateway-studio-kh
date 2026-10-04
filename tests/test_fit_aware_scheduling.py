#!/usr/bin/env python3
"""Fleet scheduling, owner-approved 2026-10-04 (`_orchestra/audit-20261001/fleet-scheduling/`).

  * D1 fit-aware group budget: a group takes a job only while
    `min(operation room, slots(model) - our jobs there whose model fits no more Macs)` > 0;
  * D3 fleet lanes are automatic: their cap follows the fleet's published capacity, a typed
    number is a ceiling, the old shipped 50 reads as "not set", and fleet-lane work is not
    counted against the owner's `global` (cloud lanes keep their limits);
  * D6 fair turns: `job_group` is stored (never forwarded) and orders a scarcity class by
    (pool, group turn, arrival);
  * D8 analytics by model x Mac class.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_studio_hub_contract import _TEMP_ROOT  # noqa: E402,F401  (sets the isolated test profile)

from gateway import admin, api, concurrency, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker, scarcest_first  # noqa: E402

OPERATION = "voice.tts"
HEAVY, LIGHT = "voxcpm2", "qwen06"


def _pid(location_id: str) -> str:
    return f"{fleet.PROVIDER_PREFIX}{location_id}"


def _site(location_id: str, *, op_total: int, supply: dict[str, int] | None = None,
          reachable: bool = True, operation: str = OPERATION) -> dict:
    """A snapshot row: the operation's eligible total and, optionally, the per-model supply."""
    row = {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": reachable, "machines": [], "machine_summary": {"reachable": op_total},
        "running_jobs": [],
        "capacity": {"queue_depth": 0, "by_operation": {operation: {
            "eligible_physical_machine_slots_total": op_total,
            "available_physical_machine_slots": op_total}}},
    }
    if supply is not None:
        row["model_supply"] = {m: {"eligible": n, "available": n} for m, n in supply.items()}
    return row


class _Case(unittest.TestCase):
    settings: dict = {"global": 50}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "provider-concurrency.local.json"
        path.write_text(json.dumps(self.settings), encoding="utf-8")
        for patcher in (patch.object(concurrency, "SETTINGS_PATH", path),
                        patch.object(fleet, "is_paused_provider", lambda pid: pid in self.paused)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.paused: set[str] = set()
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store)
        self.sites: list[dict] = []
        snap = patch.object(fleet, "snapshot", lambda *a, **k: {"locations": self.sites})
        snap.start()
        self.addCleanup(snap.stop)
        self._n = 0

    def job(self, model: str, site: str = "g", modality: str = "tts", provider: str | None = None,
            group: str | None = None) -> dict:
        self._n += 1
        row, _ = self.store.submit(modality=modality, model_id=model, params={},
                                   idempotency_key=f"k{self._n}", provider_id=provider or _pid(site),
                                   job_group=group)
        return row

    def admitted(self, models: list[str], site: str = "g") -> list[bool]:
        return [self.worker._admit(self.job(m, site)) for m in models]


class FitAwareGroupBudgetTest(_Case):
    def test_the_two_mac_nested_tier_case_from_the_simulator(self) -> None:
        # sim.check(): one 16 GB Mac + one 8 GB Mac. Two VoxCPM2 (16 GB only) and one Qwen 0.6B.
        # Before: the operation budget (2) went to both VoxCPM2 and Qwen waited while the 8 GB idled.
        self.sites = [_site("g", op_total=2, supply={HEAVY: 1, LIGHT: 2})]
        self.assertEqual(self.admitted([HEAVY, HEAVY, LIGHT]), [True, False, True])

    def test_heavy_jobs_no_longer_fill_the_whole_voice_budget(self) -> None:
        # One big Mac and three small ones: three heavy jobs get the one Mac that fits them,
        # and the light work goes to the small Macs instead of waiting behind them.
        self.sites = [_site("g", op_total=4, supply={HEAVY: 1, LIGHT: 4})]
        self.assertEqual(self.admitted([HEAVY, HEAVY, HEAVY, LIGHT, LIGHT, LIGHT, LIGHT]),
                         [True, False, False, True, True, True, False])

    def test_light_work_does_not_count_against_a_heavier_model(self) -> None:
        # Light jobs can wait for any Mac, so they never use up the big Mac's slot.
        self.sites = [_site("g", op_total=3, supply={HEAVY: 1, LIGHT: 3})]
        self.assertEqual(self.admitted([LIGHT, LIGHT, HEAVY, LIGHT]), [True, True, True, False])

    def test_without_model_supply_the_operation_total_is_the_bound_as_before(self) -> None:
        self.sites = [_site("g", op_total=2)]
        self.assertEqual(self.admitted([HEAVY, HEAVY, LIGHT]), [True, True, False])

    def test_a_release_gives_the_room_back(self) -> None:
        self.sites = [_site("g", op_total=2, supply={HEAVY: 1, LIGHT: 2})]
        first = self.job(HEAVY)
        self.assertTrue(self.worker._admit(first))
        self.assertEqual(self.worker._room(_pid("g"), "tts", HEAVY), 0)
        self.worker._release(first)
        self.assertEqual(self.worker._room(_pid("g"), "tts", HEAVY), 1)

    def test_rebind_uses_the_same_room_rule(self) -> None:
        # Site a has room in its operation total but its only VoxCPM2 Mac is taken; b has one free.
        self.sites = [_site("a", op_total=2, supply={HEAVY: 1, LIGHT: 2}),
                      _site("b", op_total=2, supply={HEAVY: 1, LIGHT: 2})]
        rows = [(types.SimpleNamespace(id=_pid(s)), {"model_id": HEAVY, "modality": "tts"}) for s in "ab"]
        with patch.object(registry.models, "enabled_models", lambda modality: rows):
            self.assertTrue(self.worker._admit(self.job(HEAVY, "a")))
            waiting = self.job(HEAVY, "a")
            self.assertFalse(self.worker._admit(waiting))
            self.assertTrue(self.worker._rebind(waiting))
        self.assertEqual(waiting["provider_id"], _pid("b"))
        self.assertTrue(self.worker._admit(waiting))


class AutomaticFleetLanesTest(_Case):
    def test_the_lane_follows_a_growing_and_shrinking_fleet(self) -> None:
        self.sites = [_site("a", op_total=3), _site("b", op_total=5)]
        budget = concurrency.lane_budget("tts")
        self.assertEqual((budget.limit, budget.source), (8, concurrency.SOURCE_FLEET))
        self.assertIn("Automatic — follows the fleet (now 8 Macs)", budget.detail)
        self.sites.append(_site("c", op_total=40))
        self.assertEqual(concurrency.lane_budget("tts").limit, 48)
        self.sites[1]["reachable"] = False
        self.paused.add(_pid("c"))
        self.assertEqual(concurrency.lane_budget("tts").limit, 3)

    def test_the_settings_view_says_automatic_with_the_effective_number(self) -> None:
        self.sites = [_site("a", op_total=7)]
        rows = {r["lane"]: r for r in concurrency.settings_view([], {}, lane_names=["tts", "chat"])["lanes"]["rows"]}
        self.assertTrue(rows["tts"]["automatic"])
        self.assertEqual((rows["tts"]["limit"], rows["tts"]["stated"]), (7, None))
        self.assertFalse(rows["chat"]["automatic"])


class LegacyAndOverrideTest(_Case):
    settings = {"global": 2, "lanes": {"tts": 50, "image": 3, "chat": 50},
                "providers": {"fal-ai": {"limit": 10}}}

    def test_the_old_shipped_50_reads_as_not_set_only_on_fleet_lanes(self) -> None:
        self.sites = [_site("a", op_total=56)]
        self.assertEqual(concurrency.lane_budget("tts").source, concurrency.SOURCE_FLEET)
        self.assertEqual(concurrency.lane_budget("tts").limit, 56)
        self.assertEqual((concurrency.lane_budget("chat").limit, concurrency.lane_budget("chat").source),
                         (50, concurrency.SOURCE_CURATED))

    def test_an_owner_number_caps_the_fleet_lane(self) -> None:
        self.sites = [_site("g", op_total=8, operation="image.text_to_image")]
        results = [self.worker._admit(self.job("flux", modality="image")) for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_cloud_lanes_keep_global_and_their_own_limit(self) -> None:
        self.sites = [_site("g", op_total=5)]
        self.assertEqual(self.admitted([LIGHT] * 5), [True] * 5)  # fleet: not counted in global
        cloud = [self.worker._admit(self.job("eleven", provider="fal-ai")) for _ in range(3)]
        self.assertEqual(cloud, [True, True, False])  # global 2 still bounds the cloud job


class FairTurnsTest(_Case):
    def _wave(self, group: str | None, n: int) -> list[dict]:
        return [self.job("flux", modality="image", provider="fal-ai", group=group) for _ in range(n)]

    def test_a_20_job_redo_interleaves_with_a_500_job_wave(self) -> None:
        wave, redo = self._wave("p1/e1", 500), self._wave("p2/e9", 20)
        order = [job["job_group"] for job in scarcest_first(self.store.due(now=1e12))]
        self.assertEqual(order[:40], ["p1/e1", "p2/e9"] * 20)
        self.assertTrue(all(g == "p1/e1" for g in order[40:]))
        self.assertEqual(len(order), len(wave) + len(redo))

    def test_a_group_already_running_waits_its_turn(self) -> None:
        self._wave("p1/e1", 500)
        self._wave("p2/e9", 20)
        order = [job["job_group"] for job in scarcest_first(self.store.due(now=1e12), {"p1/e1": 50})]
        self.assertEqual(order[:20], ["p2/e9"] * 20)

    def test_no_group_is_todays_order(self) -> None:
        jobs = self._wave(None, 30)
        self.assertEqual([j["id"] for j in scarcest_first(self.store.due(now=1e12))], [j["id"] for j in jobs])

    def test_the_worker_counts_running_jobs_per_group(self) -> None:
        self.sites = [_site("g", op_total=5)]
        job = self.job(LIGHT, group="p1/e1")
        self.worker._admit(job)
        self.assertEqual(self.worker._per_group, {"p1/e1": 1})
        self.worker._release(job)
        self.assertEqual(self.worker._per_group, {})


class JobGroupIsStoredNeverForwardedTest(_Case):
    def _submit(self, params: dict) -> dict:
        resolved = (types.SimpleNamespace(id=_pid("g")), {"model_id": LIGHT})
        with patch.object(api, "store", self.store), patch.object(api.catalog, "resolve", return_value=resolved):
            return api._submit("tts", LIGHT, params, f"key-{len(params)}-{self._n}")[0]

    def test_stored_on_the_job_and_out_of_the_params(self) -> None:
        job = self._submit({"text": "hi", "job_group": "my-project/ep-12"})
        self.assertEqual(job["job_group"], "my-project/ep-12")
        self.assertNotIn("job_group", json.loads(job["params"]))

    def test_a_malformed_group_is_ignored_not_refused(self) -> None:
        self._n += 1
        job = self._submit({"text": "hi", "job_group": "has spaces; drop table"})
        self.assertIsNone(job["job_group"])
        self.assertNotIn("job_group", json.loads(job["params"]))

    def test_the_transcription_confirm_carries_it(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        api._UPLOADS["q-group"] = (b"RIFF", "a.wav", "audio/wav", 1e12, "whisper")
        self.addCleanup(api._UPLOADS.pop, "q-group", None)
        resolved = (types.SimpleNamespace(id=_pid("g")), {"model_id": "whisper"})
        with patch.object(api, "store", self.store), patch.object(api.catalog, "resolve", return_value=resolved), \
                patch.object(api, "_quote_model", return_value="whisper"):
            answer = client.post("/v1/audio/transcriptions/quotes/q-group/confirm",
                                 headers={"Authorization": "Bearer fixture-key", "Idempotency-Key": "confirm-g"},
                                 json={"job_group": "my-project/ep-12"})
        self.assertIn(answer.status_code, (200, 202), answer.text)
        job = self.store.find_by_idempotency_key("confirm-g")
        self.assertEqual(job["job_group"], "my-project/ep-12")
        self.assertNotIn("job_group", json.loads(job["params"]))


class SpeedByMacClassTest(_Case):
    def _done(self, model: str, machine: str, seconds: float, audio: float | None = None) -> None:
        row = self.job(model)
        payload = {"hub_machine": machine, "generation_seconds": seconds}
        if audio is not None:
            payload["audio_duration_s"] = audio
        self.store._conn.execute("UPDATE jobs SET state='succeeded', result_json=? WHERE id=?",
                                 (json.dumps({"payload": payload}), row["id"]))
        self.store._conn.commit()

    def test_grouped_by_model_and_mac_class_with_rtf(self) -> None:
        self.sites = [_site("g", op_total=3)]
        self.sites[0]["machines"] = [{"machine": "local", "chip": "Apple M4", "memory_gb": 17.18},
                                     {"machine": "n1", "chip": "Apple M4", "memory_gb": 16.0},
                                     {"machine": "n2", "chip": "Apple M1", "memory_gb": 8.59}]
        self._done(HEAVY, "local", 240.0, audio=100.0)
        self._done(HEAVY, "n1", 260.0, audio=100.0)
        self._done(HEAVY, "n2", 390.0, audio=100.0)
        self._done(HEAVY, "gone", 10.0)
        with patch.object(admin, "store", self.store):
            rows = {r["key"]: r for r in admin.generation_analytics(limit=100)["by_model_class"]}
        self.assertEqual(rows[f"{HEAVY} @ M4 · 16 GB"]["seconds_median"], 250.0)
        self.assertEqual(rows[f"{HEAVY} @ M4 · 16 GB"]["rtf_median"], 2.5)
        self.assertEqual(rows[f"{HEAVY} @ M1 · 8 GB"]["jobs"], 1)
        self.assertIn(f"{HEAVY} @ unknown Mac", rows)

    def test_mac_class(self) -> None:
        self.assertEqual(admin.mac_class("Apple M2 Max", 34.36), "M2 Max · 32 GB")
        self.assertEqual(admin.mac_class("Apple M3", 25.77), "M3 · 24 GB")
        self.assertEqual(admin.mac_class(None, None), "unknown chip · ? GB")


if __name__ == "__main__":
    unittest.main()
