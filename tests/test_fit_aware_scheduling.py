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

Fleet audit 2026-10-07 (`_orchestra/audit-20261001/BACKLOG-20261007.md`):

  * F1 a Mac that handed a job back for memory is not counted for that model for a cool-down;
  * F2 a re-routed job's booking moves with it, and a fleet job's wait is capped at seconds;
  * F3 offline Macs are not a site's budget.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

#: The route reads the caller's session to scope a guest's list. These are the owner's own calls
#: from inside the process, so the stand-in carries no cookie.
_ANON_REQUEST = types.SimpleNamespace(cookies={})

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_studio_hub_contract import _TEMP_ROOT  # noqa: E402,F401  (sets the isolated test profile)

from gateway import admin, api, concurrency, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.worker import Worker, _MAX_DEFERRALS, _defer_if_transient, scarcest_first  # noqa: E402

OPERATION = "voice.tts"
HEAVY, LIGHT = "voxcpm2", "qwen06"
#: The Hub's own hand-back sentence (Hub 0.6.127+, `broker._insufficient_memory_text`).
REFUSAL = ("INSUFFICIENT_MEMORY: {mac} needs about 3.2 GB free and has about 2.4 GB free for qwen06 "
           "(it refused 3 times). Another Mac with more free memory can run this job.")


def _mac(name: str, *, reachable: bool = True, free: float | None = None) -> dict:
    return {"machine": name, "label": name, "reachable": reachable, "available_gb": free}


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
        self.addCleanup(fleet._MEMORY_COOLDOWN.clear)
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


class MemoryRefusalCooldownTest(_Case):
    """F1: on 10-04, 993 memory hand-backs on 110 jobs; 898 from 8 GB Macs still counted as eligible."""

    def test_a_mac_that_refused_a_model_for_memory_is_not_counted_for_it(self) -> None:
        self.sites = [_site("g", op_total=2, supply={LIGHT: 2})]
        fleet.note_memory_refusal(_pid("g"), LIGHT, REFUSAL.format(mac="mac-8"))
        fleet.note_memory_refusal(_pid("g"), LIGHT, REFUSAL.format(mac="mac-8"))  # one Mac, twice
        self.assertEqual(self.admitted([LIGHT, LIGHT]), [True, False])

    def test_an_older_sentence_naming_no_mac_changes_nothing(self) -> None:
        self.sites = [_site("g", op_total=2, supply={LIGHT: 2})]
        fleet.note_memory_refusal(_pid("g"), LIGHT, "INSUFFICIENT_MEMORY: the Mac does not have enough free memory")
        self.assertEqual(self.admitted([LIGHT, LIGHT]), [True, True])

    def test_the_cool_down_ends_on_time_or_when_the_mac_has_the_memory(self) -> None:
        site = _site("g", op_total=2, supply={LIGHT: 2})
        site["machines"] = [_mac("mac-8", free=2.4), _mac("mac-16", free=9.0)]
        self.sites = [site]
        fleet.note_memory_refusal(_pid("g"), LIGHT, REFUSAL.format(mac="mac-8"))
        self.assertEqual(self.worker._room(_pid("g"), "tts", LIGHT), 1)
        site["machines"][0]["available_gb"] = 3.24  # what an idle 8 GB Mac reads: not enough to trust
        self.assertEqual(self.worker._room(_pid("g"), "tts", LIGHT), 1)
        site["machines"][0]["available_gb"] = 4.5  # clearly more than the 3.2 GB it needed
        self.assertEqual(self.worker._room(_pid("g"), "tts", LIGHT), 2)
        fleet.note_memory_refusal(_pid("g"), LIGHT, REFUSAL.format(mac="mac-x"),
                                  now=time.time() - fleet.MEMORY_COOLDOWN_S - 1)
        self.assertEqual(self.worker._room(_pid("g"), "tts", LIGHT), 2, "a cool-down that ran out")

    def test_routing_prefers_a_site_whose_macs_did_not_refuse_but_never_routes_nowhere(self) -> None:
        self.sites = [_site("a", op_total=1, supply={LIGHT: 1}), _site("b", op_total=1, supply={LIGHT: 1})]
        rows = [(types.SimpleNamespace(id=_pid(s)), {"model_id": LIGHT, "modality": "tts"}) for s in "ab"]
        with patch.object(registry.models, "enabled_models", lambda modality: rows), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True):
            fleet.note_memory_refusal(_pid("a"), LIGHT, REFUSAL.format(mac="mac-8"))
            self.assertEqual([c["location_id"] for c in fleet.routing_candidates(LIGHT, "tts")], ["b"])
            fleet.note_memory_refusal(_pid("b"), LIGHT, REFUSAL.format(mac="mac-9"))
            self.assertEqual(sorted(c["location_id"] for c in fleet.routing_candidates(LIGHT, "tts")), ["a", "b"],
                             "a submit with no route is refused, so a spent fleet still routes")


class OfflineMacsAreNotBudgetTest(_Case):
    """F3: Shops counted 3 slots with 2 iMacs offline; 26 % of its images waited 30 s+ beyond generation."""

    def _shops(self, **kwargs) -> dict:
        site = _site("shops", op_total=3, **kwargs)
        site["machines"] = [_mac("controller"), _mac("imac-1", reachable=False), _mac("imac-2", reachable=False)]
        return site

    def test_a_site_with_two_macs_offline_takes_one_job(self) -> None:
        self.sites = [self._shops(supply={LIGHT: 3})]
        self.assertEqual(self.admitted([LIGHT, LIGHT, LIGHT], "shops"), [True, False, False])

    def test_the_operation_total_is_capped_too(self) -> None:
        self.sites = [self._shops()]
        self.assertEqual(self.admitted([LIGHT, LIGHT, LIGHT], "shops"), [True, False, False])

    def test_routing_weighs_the_reachable_macs(self) -> None:
        self.sites = [self._shops(supply={LIGHT: 3})]
        rows = [(types.SimpleNamespace(id=_pid("shops")), {"model_id": LIGHT, "modality": "tts"})]
        with patch.object(registry.models, "enabled_models", lambda modality: rows), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True):
            self.assertEqual([c["slots"] for c in fleet.routing_candidates(LIGHT, "tts")], [1])

    def test_a_mac_whose_reachability_is_unstated_still_counts(self) -> None:
        site = _site("g", op_total=2, supply={LIGHT: 2})
        site["machines"] = [{"machine": "a"}, {"machine": "b"}]
        self.sites = [site]
        self.assertEqual(self.admitted([LIGHT, LIGHT]), [True, True])


class FleetDispatchGapTest(_Case):
    """F2: on 10-05 Breeze jobs waited 2-15 min in the Gateway while Macs idled."""

    def _registry(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            definition=lambda pid: types.SimpleNamespace(id=pid, kind="studio_hub", base_url="", display_name=pid),
            credentials=types.SimpleNamespace(is_enabled=lambda _p: True, resolved=lambda _p: {}),
            models=types.SimpleNamespace(enabled_models=lambda _m: [
                (types.SimpleNamespace(id=_pid(s)), {"model_id": LIGHT}) for s in "ab"]))

    def _run_moved(self, refusal: BackendError) -> tuple[dict, threading.Event, threading.Thread, list]:
        """Admit a job at a; a refuses it with `refusal`, the re-route sends it to b, where it holds."""
        at_b, finish, rooms = threading.Event(), threading.Event(), []

        class _Hub:
            def generate(_self, request):
                if request.provider_id == _pid("a"):
                    raise refusal
                rooms.append((self.worker._room(_pid("a"), "tts", LIGHT), self.worker._room(_pid("b"), "tts", LIGHT)))
                at_b.set()
                finish.wait(5)
                raise BackendError("stopped by the test", code="PROVIDER_FAILED", upstream_accepted=False,
                                   upstream_settled=True)

        job = self.job(LIGHT, "a")
        self.assertTrue(self.worker._admit(job))
        patches = (patch.object(worker_module, "registry", self._registry()),
                   patch.object(worker_module, "adapter_for", lambda _d: _Hub()),
                   patch.object(fleet, "route", lambda *a, **k: (types.SimpleNamespace(id=_pid("b")), "")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        thread = threading.Thread(target=self.worker._run, args=(job,), daemon=True)
        thread.start()
        self.assertTrue(at_b.wait(5), "the job never reached b")
        finish.set()
        thread.join(5)
        return job, at_b, thread, rooms

    def test_a_rerouted_job_takes_its_booking_with_it(self) -> None:
        # Before: a still counted the job (room 0) while b, which was running it, read as empty
        # (room 1) - a idled its Mac and b was offered a second job for the same one.
        self.sites = [_site("a", op_total=1, supply={LIGHT: 1}), _site("b", op_total=1, supply={LIGHT: 1})]
        failed = BackendError("hub item error: engine crashed", code="PROVIDER_FAILED", upstream_accepted=False,
                              upstream_settled=True)
        _job, _at_b, _thread, rooms = self._run_moved(failed)
        self.assertEqual(rooms, [(1, 0)])
        self.assertEqual(self.worker._in_flight, {}, "released from where it ended, nothing left booked")
        self.assertEqual(self.worker._per_model, {})

    def test_a_memory_hand_back_rests_that_mac_for_the_model(self) -> None:
        self.sites = [_site("a", op_total=2, supply={LIGHT: 2}), _site("b", op_total=1, supply={LIGHT: 1})]
        refusal = BackendError(REFUSAL.format(mac="mac-8"), code="INSUFFICIENT_MEMORY", retryable=True,
                               upstream_accepted=False, upstream_settled=True)
        _job, _at_b, _thread, rooms = self._run_moved(refusal)
        self.assertEqual(rooms, [(1, 0)], "a: 2 Macs less the one resting; the job's booking is at b")
        self.assertEqual(fleet.memory_cooled("a", LIGHT), 1)

    def _deferred_wait(self, job: dict, attempts: int, **row) -> float | None:
        current = {**self.store.get(job["id"]), "attempt_count": attempts, **row}
        before = time.time()
        busy = BackendError("the location cannot serve this model right now", code="LOCATION_NOT_READY",
                            upstream_accepted=False)
        if not _defer_if_transient(self.store, job["id"], busy, current, unknown=False):
            return None
        return self.store.get(job["id"])["next_action_at"] - before

    def test_a_fleet_job_waits_seconds_not_minutes(self) -> None:
        # Before: 2, 4 ... 256, then 300 s - a job deferred eight times slept through every Mac
        # that freed in the next four minutes (10-04: 34 jobs reached 256-300 s waits).
        self.assertLessEqual(self._deferred_wait(self.job(LIGHT), 12), worker_module._FLEET_DEFER_CAP_S + 0.5)
        self.assertGreaterEqual(self._deferred_wait(self.job("eleven", provider="fal-ai"), 12), 299.0,
                                "a cloud lane keeps its own ladder")

    def test_a_fleet_job_still_stops_waiting_once_the_deferral_budget_is_spent(self) -> None:
        self.assertIsNotNone(self._deferred_wait(self.job(LIGHT), _MAX_DEFERRALS))
        self.assertIsNone(self._deferred_wait(self.job(LIGHT), _MAX_DEFERRALS + 1))
        self.assertEqual(worker_module._defer_delay(10_000, 15.0), 15.0, "no OverflowError on a huge count")


class SchedulingObservabilityTest(_Case):
    """Fleet audit 2026-10-07: page past 500 jobs, per-day model analytics, eligible Macs in the queue."""

    def _at(self, job: dict, created: float, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in ("created_at", *fields))
        self.store._conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (created, *fields.values(), job["id"]))
        self.store._conn.commit()

    def test_jobs_page_past_one_answer_with_before(self) -> None:
        made = [self.job(LIGHT) for _ in range(5)]
        for n, job in enumerate(made):
            self._at(job, 1000.0 + n)
        seen, before = [], None
        with patch.object(admin, "store", self.store):
            while True:
                page = admin.list_jobs(_ANON_REQUEST, limit=2, state=None, modality=None, since=None, before=before)
                seen += [row["id"] for row in page["jobs"]]
                before = page["next_before"]
                if before is None:
                    break
            recent = admin.list_jobs(_ANON_REQUEST, limit=50, state=None, modality=None, since=1003.0, before=None)
        self.assertEqual(seen, [job["id"] for job in reversed(made)], "every job once, newest first")
        self.assertEqual([row["id"] for row in recent["jobs"]], [made[4]["id"], made[3]["id"]])

    def test_daily_by_model_has_queue_wait_percentiles_and_failure_codes(self) -> None:
        day = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))
        for n, wait in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 100]):
            job = self.job(LIGHT)
            self._at(job, day + n, dispatch_started_at=day + n + wait, state="succeeded")
        broken = self.job(LIGHT)
        self._at(broken, day, state="failed", error_code="INSUFFICIENT_MEMORY")
        self._at(self.job(HEAVY), day + 86400, state="accepted")
        rows = {(r["day"], r["model_id"]): r for r in self.store.daily_by_model(day - 3600)}
        light = rows[("2026-10-05", LIGHT)]
        self.assertEqual((light["jobs"], light["succeeded"], light["failed"]), (11, 10, 1))
        self.assertEqual(light["failures"], {"INSUFFICIENT_MEMORY": 1})
        self.assertEqual((light["queue_wait_p50_s"], light["queue_wait_p90_s"]), (5.0, 9.0))
        self.assertIsNone(rows[("2026-10-06", HEAVY)]["queue_wait_p50_s"], "never dispatched is not zero")
        with patch.object(admin, "store", self.store):
            self.assertEqual(admin.daily_analytics(days=7)["days"], 7)

    def test_the_queue_says_how_many_macs_could_take_a_waiting_job(self) -> None:
        self.sites = [_site("a", op_total=4, supply={LIGHT: 3}), _site("b", op_total=2, supply={LIGHT: 2})]
        rows = [(types.SimpleNamespace(id=_pid(s)), {"model_id": LIGHT, "modality": "tts"}) for s in "ab"]
        fleet.note_memory_refusal(_pid("b"), LIGHT, REFUSAL.format(mac="mac-8"))
        self.job(LIGHT, "a")
        self.job("eleven", provider="fal-ai")
        # **The catalogue is not what this test is about.** `queue_view` reads `all_rows()`
        # for each lane's own deadline, and that read rebuilds the catalogue — under the two
        # fake definitions above (plain `SimpleNamespace`s with an `id` and nothing else), the
        # rebuild fails on `.description`. It passed while a neighbouring test's catalogue
        # happened to be warm; the isolation it relied on was never written down (2026-10-29).
        with patch.object(admin, "store", self.store), \
                patch.object(registry.models, "enabled_models", lambda modality: rows), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True), \
                patch.object(admin.catalog, "all_rows", lambda: []):
            jobs = {row["provider_id"]: row for row in admin.queue_view(limit=10)["jobs"]}
        self.assertEqual(jobs[_pid("a")]["eligible_macs"], 4, "3 at a, plus b's 2 less the one resting")
        self.assertIsNone(jobs["fal-ai"]["eligible_macs"], "a cloud lane has no Macs to count")


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


class OutputSpaceTest(_Case):
    """A Mac that cannot write the result is not a Mac that can run the job.

    Measured 2026-11-16: a voice job was sent to a Mac whose managed output storage was full and
    was refused `VOICE_REFERENCE_REJECTED` — the Hub's own disk figure already said so, and the
    gateway asked nobody. Both halves are held here: the rest after a refusal, and the disk
    reading before one.
    """

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(fleet._STORAGE_COOLDOWN.clear)

    def _rows(self, sites: str) -> list[tuple[types.SimpleNamespace, dict]]:
        return [(types.SimpleNamespace(id=_pid(s)), {"model_id": HEAVY, "modality": "tts"})
                for s in sites]

    def test_a_location_that_refused_for_output_space_is_offered_only_as_a_last_resort(self) -> None:
        self.sites = [_site("a", op_total=1, supply={HEAVY: 1}), _site("b", op_total=1, supply={HEAVY: 1})]
        with patch.object(registry.models, "enabled_models", lambda modality: self._rows("ab")), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True):
            fleet.note_storage_refusal(_pid("a"), HEAVY)
            self.assertEqual([c["location_id"] for c in fleet.routing_candidates(HEAVY, "tts")], ["b"],
                             "the location that just ran out of output space is not offered the next asset")
            fleet._STORAGE_COOLDOWN[("a", HEAVY)] = time.time() - 1
            self.assertEqual(sorted(c["location_id"] for c in fleet.routing_candidates(HEAVY, "tts")),
                             ["a", "b"], "and it is back once its rest has run out")

    def test_a_mac_the_hub_states_is_out_of_disk_is_not_offered_voice_output(self) -> None:
        site = _site("a", op_total=1, supply={HEAVY: 1})
        site["machines"] = [{"machine": "mac-1", "label": "mac-1", "reachable": True, "health": "up",
                             "available_gb": 8.0,
                             "disk": {"state": "red", "free_gb": 3.0, "total_gb": 500.0}}]
        site["local_models"] = [{"machine": "mac-1", "model_id": HEAVY, "modality": "tts",
                                 "memory_eligible": True, "runtime_ready": True}]
        self.sites = [site]
        with patch.object(registry.models, "enabled_models", lambda modality: self._rows("a")), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True):
            self.assertTrue(fleet.routing_candidates(HEAVY, "tts")[0]["spent"],
                            "offered only when nothing else can take the model, never skipped outright")
            site["machines"][0]["disk"]["state"] = "green"
            self.assertFalse(fleet.routing_candidates(HEAVY, "tts")[0]["spent"])

    def test_a_disk_state_this_gateway_derived_is_shown_but_not_acted_on(self) -> None:
        site = _site("a", op_total=1, supply={HEAVY: 1})
        site["machines"] = [{"machine": "mac-1", "label": "mac-1", "reachable": True,
                             "disk_free_gb": 1.0, "disk_total_gb": 500.0}]
        site["local_models"] = [{"machine": "mac-1", "model_id": HEAVY, "modality": "tts",
                                 "memory_eligible": True, "runtime_ready": True}]
        self.sites = [site]
        with patch.object(registry.models, "enabled_models", lambda modality: self._rows("a")), \
                patch.object(fleet, "_takes_customer_jobs", lambda row: True):
            self.assertFalse(fleet.routing_candidates(HEAVY, "tts")[0]["spent"],
                             "only the Hub's own reading gates work; a derived one is for showing")


if __name__ == "__main__":
    unittest.main()
