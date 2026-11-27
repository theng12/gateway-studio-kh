"""Concurrency is bounded, and the bound is the one we publish.

The gateway ran exactly one job at a time. That was right for one owner and one
Mac — and with a fleet it means nineteen idle machines, which is the whole reason
this changed.

Three ceilings, and these tests hold each of them to a number a reader could check:

  * **global** — the pool size, and the `paid_concurrency_limit` Story Studio reads;
  * **lane** — a modality cap, off by default;
  * **provider** — a cloud lane's curated limit, or a Studio Hub site's own
    **capacity**: its free physical machines for that operation plus this gateway's own
    in-flight jobs there, clamped to the site's eligible total, and then `min`'d with the
    operator's number for that location.

Two properties matter more than the numbers and are tested the same way each time:
**a limit is never exceeded**, and **a limit of zero never stalls the queue** — a
site reporting no free slots must still get work, or a busy fleet would wedge
behind a number that only meant "busy right now".

A third arrived with the fleet work: **M Macs must accept M concurrent jobs**. The
measured defect was the opposite — a site's free count was used as a ceiling and this
gateway's own in-flight jobs were subtracted from it a second time, so a site could only
ever run half its Macs. `AdmissionIsACapacityNotASecondSubtractionTest` holds M = 1, 3 and
6 against a live fake site.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import concurrency  # noqa: E402
from gateway import constants  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.worker import Worker  # noqa: E402


class _FakeStore:
    """A store whose claim can be made to fail, to prove no slot leaks."""

    def __init__(self, jobs, *, claim_always_fails: bool = False) -> None:
        self._jobs = {job["id"]: dict(job, state="accepted") for job in jobs}
        self._lock = threading.Lock()
        self.claim_always_fails = claim_always_fails
        self.failed: list[tuple[str, str]] = []

    def due(self):
        with self._lock:
            return [dict(job) for job in self._jobs.values() if job["state"] == "accepted"]

    def mark_running(self, job_id: str) -> bool:
        if self.claim_always_fails:
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["state"] != "accepted":
                return False
            job["state"] = "running"
            return True

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def finish(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["state"] = "succeeded"

    def fail(self, job_id: str, **kwargs) -> None:
        self.failed.append((job_id, kwargs.get("detail", "")))

    def settled(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["state"] == "succeeded")


class _Concurrency:
    """Peak simultaneous execution, overall and per provider."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.by_provider: dict[str, int] = {}
        self.provider_peak: dict[str, int] = {}

    def enter(self, job) -> None:
        key = str(job.get("provider_id") or "")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.by_provider[key] = self.by_provider.get(key, 0) + 1
            self.provider_peak[key] = max(self.provider_peak.get(key, 0), self.by_provider[key])

    def leave(self, job) -> None:
        key = str(job.get("provider_id") or "")
        with self.lock:
            self.active -= 1
            self.by_provider[key] -= 1

    def provider(self, key: str) -> int:
        with self.lock:
            return self.provider_peak.get(key, 0)


def _job(job_id: str, *, provider: str = "openai-direct", modality: str = "image") -> dict:
    return {"id": job_id, "provider_id": provider, "modality": modality, "state": "accepted"}


class _Harness:
    """Run a real Worker against a fake store, with execution under our control."""

    def __init__(self, jobs, *, pool_size=4, interval=0.01, hold=0.25,
                 claim_always_fails=False, locations=None, execute=None):
        self.store = _FakeStore(jobs, claim_always_fails=claim_always_fails)
        self.tracker = _Concurrency()
        self.hold = hold
        self.locations = locations if locations is not None else []
        self.worker = Worker(self.store, interval_s=interval, pool_size=pool_size)
        self._execute_fn = execute

    def _execute(self, job, store):
        if self._execute_fn is not None:
            self._execute_fn(job, store)
            return
        self.tracker.enter(job)
        try:
            time.sleep(self.hold)
        finally:
            self.tracker.leave(job)
        store.finish(job["id"])

    def __enter__(self):
        # `locations` may be a **callable**, which is how a test models a site whose own
        # free count moves as jobs start and finish. A static list says the same thing on
        # every pass, which cannot distinguish a capacity from a remainder.
        snapshot = (self.locations if callable(self.locations)
                    else (lambda **_: {"locations": self.locations}))
        self._patches = [
            patch.object(worker_module, "execute_job", self._execute),
            # No live fleet: an empty location list makes every provider fall through
            # to its cloud budget, which is what these tests are about.
            patch("gateway.fleet.snapshot", snapshot),
        ]
        for item in self._patches:
            item.start()
        self.worker.start()
        return self

    def __exit__(self, *exc):
        self.worker.stop()
        for item in self._patches:
            item.stop()
        return False

    def wait_for(self, count: int, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.store.settled() >= count:
                return True
            time.sleep(0.02)
        return False


class AStuckHoldSweepDoesNotStallDispatchTest(unittest.TestCase):
    """Hold reconciliation asks Hubs over HTTP (up to 21 s per hold, up to 50 holds, serially). It used
    to run on the dispatch thread, so one unreachable Hub parked every other job in `accepted`."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_a_blocked_sweep_leaves_dispatch_running(self) -> None:
        release = threading.Event()
        started = threading.Event()

        def stuck(_store):
            started.set()
            release.wait(10)
            return {}

        self.addCleanup(release.set)
        with patch.object(worker_module, "reconcile_holds", stuck), \
                _Harness([], pool_size=2, hold=0.01) as h:
            self.assertTrue(started.wait(3), "the sweep should have started on the first pass")
            # Arrives after the sweep began: only a free dispatcher can pick it up.
            with h.store._lock:
                h.store._jobs["late"] = dict(_job("late"), state="accepted")
            self.assertTrue(h.wait_for(1, timeout=3), "dispatch was blocked behind the hold sweep")


class ThePoolRunsMoreThanOneTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_several_jobs_run_at_the_same_time(self) -> None:
        # A barrier only opens when every party arrives, so this cannot pass on a
        # serial worker: it would block until the timeout and raise. The third party
        # is this thread, which only arrives if the worker really started two jobs.
        gate = threading.Barrier(3, timeout=8)

        def execute(job, store):
            gate.wait()
            store.finish(job["id"])

        jobs = [_job(f"job-{n}", provider=f"provider-{n}") for n in range(2)]
        with _Harness(jobs, pool_size=2, execute=execute) as h:
            gate.wait()  # raises BrokenBarrierError if the worker ran them in turn
            self.assertTrue(h.wait_for(2))

    def test_the_pool_size_is_a_real_ceiling(self) -> None:
        jobs = [_job(f"job-{n}", provider=f"provider-{n}") for n in range(6)]
        with _Harness(jobs, pool_size=2, hold=0.3) as h:
            self.assertTrue(h.wait_for(6))
            self.assertLessEqual(h.tracker.peak, 2, "more jobs ran at once than the pool allows")
            self.assertGreater(h.tracker.peak, 1, "nothing ran concurrently at all")


class ProviderBudgetsTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_a_cloud_lane_never_runs_more_than_its_own_limit(self) -> None:
        # A provider's limit is an "up to" ceiling that differs per provider and rises with its
        # tier, so no number is asserted: whatever the settings say (or the default when nothing
        # does) is what bounds the lane -- over-scheduling is what produces 429s, and a 429 on a
        # submitted job is a durable hold rather than a clean retry.
        limit = concurrency.cloud_budget("openai-direct").limit
        jobs = [_job(f"job-{n}") for n in range(6)]
        with _Harness(jobs, pool_size=4, hold=0.2) as h:
            self.assertTrue(h.wait_for(6))
            self.assertEqual(h.tracker.provider("openai-direct"), min(limit, 4),
                             "the lane ran at its configured limit -- not over it, not under it")

    def test_a_curated_limit_is_what_the_owner_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider-concurrency.local.json"
            path.write_text(json.dumps({
                "providers": {"openai-direct": {"limit": 3, "note": "tier 4"}}
            }), encoding="utf-8")
            with patch.object(concurrency, "SETTINGS_PATH", path):
                concurrency.reset_for_tests()
                jobs = [_job(f"job-{n}") for n in range(4)]
                with _Harness(jobs, pool_size=4, hold=0.25) as h:
                    self.assertTrue(h.wait_for(4))
                    self.assertEqual(h.tracker.provider("openai-direct"), 3)
                self.assertEqual(concurrency.cloud_budget("openai-direct").source, "curated")

    def test_a_hub_site_runs_as_many_as_its_own_free_slots(self) -> None:
        locations = [{
            "provider_id": "studio-hub-abc",
            "reachable": True,
            "capacity": {"by_operation": {"image.text_to_image": {"available_worker_slots": 3}}},
        }]
        jobs = [_job(f"job-{n}", provider="studio-hub-abc") for n in range(5)]
        with _Harness(jobs, pool_size=5, hold=0.25, locations=locations) as h:
            self.assertTrue(h.wait_for(5))
            self.assertEqual(h.tracker.provider("studio-hub-abc"), 3)

    def test_a_hub_with_no_free_slots_still_runs_one(self) -> None:
        # **A limit of zero must never wedge the queue.** A site reporting no free
        # slots is busy right now, not closed; treating 0 as a real cap would leave
        # jobs due for ever with nothing wrong and nothing to see.
        locations = [{
            "provider_id": "studio-hub-abc",
            "reachable": True,
            "capacity": {"by_operation": {"image.text_to_image": {"available_worker_slots": 0}}},
        }]
        jobs = [_job(f"job-{n}", provider="studio-hub-abc") for n in range(2)]
        with _Harness(jobs, pool_size=2, hold=0.1, locations=locations) as h:
            self.assertTrue(h.wait_for(2), "a site with no free slots stalled the queue")
            self.assertEqual(h.tracker.provider("studio-hub-abc"), 1)


class AdmissionIsACapacityNotASecondSubtractionTest(unittest.TestCase):
    """**M Macs accept M concurrent jobs** — admission must not halve a site.

    The Hub's `available_worker_slots` is already net of the Macs that are busy, and this
    gateway's own dispatched jobs are exactly what makes them busy. Reading that free count
    as the ceiling and then refusing at `in_flight >= free` needed `T < M − T`: at most
    **half** of every site could ever run, and the arithmetic got worse the busier the fleet
    got. The fix is to add this gateway's own in-flight count back before comparing:

        budget = min(free + in_flight, eligible_total)

    These run against a **live** fake site — its free count falls as jobs start and rises as
    they finish, exactly as the Hub's does — so the numbers are the ones a real rollout
    would produce, at M = 1, 3 and 6.
    """

    def setUp(self) -> None:
        # These measure the arithmetic of one site, so the pool above it must not be the thing that
        # bounds them: a lane with no cap of its own is bounded by the settings' `global`, which is
        # 100 on the owner's machine and 4 on a clean one. The test states its own, above every M.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "provider-concurrency.local.json"
        path.write_text(json.dumps({"global": 50}), encoding="utf-8")
        patcher = patch.object(concurrency, "SETTINGS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    class _LiveSite:
        """A fake controller whose free count follows the jobs actually running."""

        def __init__(self, total: int, *, modality: str = "image") -> None:
            self.total = total
            self.modality = modality
            self.busy = 0
            self.operation = "image.text_to_image" if modality == "image" else "chat.completion"

        def snapshot(self, **_) -> dict:
            free = max(0, self.total - self.busy)
            return {"locations": [{
                "provider_id": "studio-hub-live",
                "reachable": True,
                "capacity": {
                    "eligible_physical_machine_slots_total": self.total,
                    "available_physical_machine_slots": free,
                    "by_operation": {self.operation: {
                        "eligible_physical_machine_slots_total": self.total,
                        "available_physical_machine_slots": free,
                    }},
                },
            }]}

    def _run(self, total: int, jobs: int):
        site = self._LiveSite(total)
        peak = {"value": 0}
        lock = threading.Lock()

        def execute(job, store):
            with lock:
                site.busy += 1
                peak["value"] = max(peak["value"], site.busy)
            try:
                time.sleep(0.25)
            finally:
                with lock:
                    site.busy -= 1
            store.finish(job["id"])

        rows = [_job(f"job-{n}", provider="studio-hub-live") for n in range(jobs)]
        with _Harness(rows, pool_size=jobs, hold=0.25, execute=execute,
                      locations=site.snapshot) as h:
            self.assertTrue(h.wait_for(jobs, timeout=20.0),
                            f"a site of {total} Macs did not finish {jobs} jobs")
        return peak["value"]

    def test_one_mac_runs_one_job(self) -> None:
        self.assertEqual(self._run(total=1, jobs=1), 1)

    def test_three_macs_run_three_at_once(self) -> None:
        # The old arithmetic allowed floor(3/2) = 1 here.
        self.assertEqual(self._run(total=3, jobs=3), 3)

    def test_six_macs_run_six_at_once(self) -> None:
        self.assertEqual(self._run(total=6, jobs=6), 6)

    def test_an_unreachable_location_is_given_no_budget(self) -> None:
        # M2: an offline controller used to compute 1, so it burned a global slot and a
        # timeout on every pass. Routing already skips it; admission now agrees.
        budget = concurrency.hub_budget(4, operation="image.text_to_image", ceiling=4,
                                        reachable=False)
        self.assertEqual(budget.limit, 0)
        self.assertIn("not reachable", budget.detail)

    def test_a_free_count_with_no_total_is_left_alone(self) -> None:
        # A Hub that publishes free slots and no eligible total cannot tell "3 of 3" from
        # "3 of 30", so the free count stands and nothing is invented.
        budget = concurrency.hub_budget(3, operation="image.text_to_image")
        self.assertEqual(budget.limit, 3)

    def test_a_site_is_admitted_up_to_its_own_total_whatever_is_free(self) -> None:
        # Queue ahead (0.2.82): the ceiling is the site's eligible total. 2 free of 6 is 6,
        # not 2, because the Hub's durable queue holds the rest for whichever Mac frees.
        budget = concurrency.hub_budget(2, operation="image.text_to_image", ceiling=6)
        self.assertEqual(budget.limit, 6)
        self.assertIn("eligible total", budget.detail)

    def test_busy_right_now_is_not_closed_for_ever(self) -> None:
        # The property the module docstring demands: a zero reading must not wedge a queue.
        # With a stated total it is the total; without one, the default.
        budget = concurrency.hub_budget(0, operation="image.text_to_image", ceiling=4)
        self.assertEqual(budget.limit, 4)
        self.assertEqual(concurrency.hub_budget(0, operation="image.text_to_image").limit,
                         concurrency.DEFAULT_PROVIDER)

    def test_the_physical_count_wins_over_the_worker_service_count(self) -> None:
        # M1, measured live: one Mac published `available_worker_slots: 2` for
        # audio.transcription, so admission scheduled two jobs onto one Mac and the second
        # came back MACHINE_BUSY. Routing already used the physical count; now so does this.
        row = {
            "reachable": True,
            "capacity": {"by_operation": {"audio.transcription": {
                "available_worker_slots": 2,
                "eligible_worker_service_slots_total": 2,
                "available_physical_machine_slots": 1,
                "eligible_physical_machine_slots_total": 1,
            }}},
        }
        reading = concurrency.hub_slot_reading(row, "audio.transcription")
        self.assertEqual(reading["unit"], "physical_machine")
        self.assertEqual(reading["free"], 1)
        self.assertEqual(reading["ceiling"], 1)


class LaneBudgetsTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def _settings(self, body: dict):
        """Point the settings at a file this test wrote: the owner's own must not decide it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "provider-concurrency.local.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        patcher = patch.object(concurrency, "SETTINGS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()

    def test_a_lane_cap_bounds_that_lane(self) -> None:
        self._settings({"lanes": {"image": 2}})
        cap = concurrency.lane_budget("image").limit
        self.assertEqual(cap, 2, "the cap that was configured is the cap in force")
        jobs = [_job(f"job-{n}", provider=f"provider-{n}") for n in range(6)]
        with _Harness(jobs, pool_size=4, hold=0.25) as h:
            self.assertTrue(h.wait_for(6))
            self.assertEqual(h.tracker.peak, cap, "the lane ran at its cap: not over it, not under")

    def test_a_lane_with_no_cap_may_use_the_whole_pool(self) -> None:
        # A non-fleet lane. A fleet lane with no cap follows the fleet instead (D3,
        # tests/test_fit_aware_scheduling.py).
        self._settings({})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GATEWAY_LANE_CONCURRENCY", None)
            budget = concurrency.lane_budget("chat")
        self.assertEqual(budget.limit, concurrency.global_limit())
        self.assertEqual(budget.source, "gateway")


class SlotsAreNeverLeakedTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_a_failed_claim_releases_the_slot_it_reserved(self) -> None:
        # Reserve-then-claim: if a failing claim kept its reservation, the pool
        # would bleed slots until nothing could be admitted, and the symptom would
        # be "the gateway stopped taking work" with no error anywhere.
        job = _job("job-0")
        with patch("gateway.fleet.snapshot", lambda **_: {"locations": []}):
            h = _Harness([job], pool_size=1, claim_always_fails=True)
            with patch.object(worker_module, "execute_job", h._execute):
                self.assertTrue(h.worker._admit(job))
                self.assertEqual(h.worker.active(), 1)
                # The claim fails, which is what the loop does next.
                self.assertFalse(h.store.mark_running(job["id"]))
                h.worker._release(job)
                self.assertEqual(h.worker.active(), 0)

    def test_the_budget_reports_where_its_number_came_from(self) -> None:
        with patch("gateway.fleet.snapshot", lambda **_: {"locations": []}), \
                patch.object(worker_module, "execute_job", lambda job, store: None):
            h = _Harness([_job("job-0", provider="openai-direct")], pool_size=1)
            h.worker._admit(_job("job-0", provider="openai-direct"))
            # Whatever the settings say -- or the default, when nothing does -- and *which* of the
            # two it was, exactly as `cloud_budget` states it.
            stated = concurrency.cloud_budget("openai-direct")
            self.assertEqual(h.worker.budgets["openai-direct"]["source"], stated.source)
            self.assertEqual(h.worker.budgets["openai-direct"]["limit"], stated.limit)


class ThePublishedLimitIsTheRealOneTest(unittest.TestCase):
    """The env var and the default, **not** whatever this Mac's settings file happens to say.

    These two tests asserted the environment and the fallback while reading the LIVE
    `provider-concurrency.local.json`, so they passed only while that file had no `global` key.
    The owner set one (`global: 12`, 2026-09-26) and both began failing on his machine and
    nowhere else: `global_budget()` consults the file **before** the environment, so 12 beat the
    `GATEWAY_JOB_CONCURRENCY` the test had just set. A test that depends on the operator's own
    configuration is not measuring the code.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        # An empty settings file, so the environment and the default are what is under test.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "provider-concurrency.local.json"
        patcher = patch.object(concurrency, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_pool_bounds_admission_and_is_published_beside_the_fleet(self) -> None:
        """**This test used to assert that the pool was what Story was told. It is not any more.**

        It was the literal 4 while the worker ran one job at a time — a published number that did
        not describe the system, in a field Story Studio reads — so it was moved onto the pool.
        That was right as far as it went and wrong about *which* number belongs in that field: the
        pool is this gateway's own concurrency, while `paid_concurrency_limit` is what Story may
        run, and the owner's decision D is that the second is the fleet's live capacity
        (*"setting a number isn't correct … it must be dynamic of reality"*). Measured live before
        the change: Story was told **60** while the fleet could do about **4** (audit H7).

        So the assertion is split, deliberately. The pool still bounds dispatch — that is what
        `global_limit()` is for and the line below still holds it. The published capacity is the
        fleet's, derived in `tests/test_live_capacity.py`, and the pool is published **beside** it
        under its own name so the two cannot be confused.
        """
        with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": "7"}):
            concurrency.reset_for_tests()
            self.assertEqual(concurrency.global_limit(), 7)
            self.assertEqual(constants.admission()["concurrency_pool"], 7)

    def test_a_bad_value_falls_back_rather_than_stopping_the_gateway(self) -> None:
        # "0" or "many" must not mean "take no work at all".
        for bad in ("0", "-2", "many", ""):
            with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": bad}):
                self.assertEqual(concurrency.global_limit(), concurrency.DEFAULT_GLOBAL)

    def test_a_stated_setting_beats_the_environment(self) -> None:
        # The precedence the two tests above depend on, stated rather than assumed: the file is
        # read first, so an operator's own number wins over an exported one. This is the rule
        # that made them fail on a configured Mac.
        self.path.write_text(json.dumps({"global": 5}), encoding="utf-8")
        with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": "7"}):
            self.assertEqual(concurrency.global_limit(), 5)

    def test_the_admission_record_still_carries_the_other_limits(self) -> None:
        published = constants.admission()
        self.assertIn("max_queued_jobs", published)
        self.assertIn("generation_submissions_per_minute", published)


class TheConsoleReportsWhatWillActuallyRunTest(unittest.TestCase):
    """A Studio Hub location's concurrency is the site's own number, and the page shows it.

    `settings_view` drew EVERY provider from `cloud_budget`, so a location whose Hub
    published four free image slots was rendered as *"no limit set, so one at a time; set
    it on the Providers page"*. Two defects in one row: the number did not describe the
    system — the same shape as the `paid_concurrency_limit` literal that once read 4 while
    the worker ran one job — and the advice did nothing, because `Worker._provider_budget`
    read the Hub's `available_worker_slots` and never consulted the settings file for a Hub
    location.

    Measured live before the first fix: dispatch allowed **4** for `studio-hub-9701220bf30f`
    while the console said **1** for it.

    **The second half is now fixed too, and this class was written against the first.** The
    page was corrected to *report* the site's slots and to say the control was not read; the
    better answer was to make the control real. An operator's number for a location is now a
    **cap** — `min(site, stated)` — so it can throttle a busy site but can never push more
    concurrent work at it than the site says it has free workers. These tests cover both.
    """

    def _view_with(self, by_operation, provider_ids=("studio-hub-test",), stated=None):
        snap = {"locations": [{
            "provider_id": "studio-hub-test",
            "capacity": {"by_operation": by_operation},
        }]}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "provider-concurrency.local.json"
        path.write_text(json.dumps(
            {"providers": {"studio-hub-test": {"limit": stated}}} if stated is not None else {}
        ), encoding="utf-8")
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        with patch.object(concurrency, "SETTINGS_PATH", path), \
                patch("gateway.fleet.snapshot", lambda **_: snap):
            return concurrency.settings_view(list(provider_ids))

    def _row(self, view, provider_id):
        return next(p for p in view["providers"] if p["provider_id"] == provider_id)

    def test_a_hub_row_reports_the_sites_own_slots(self) -> None:
        view = self._view_with({"image.text_to_image": {"available_worker_slots": 4}})
        row = self._row(view, "studio-hub-test")
        self.assertEqual(row["limit"], 4)
        self.assertEqual(row["source"], concurrency.SOURCE_HUB)

    def test_the_operators_number_caps_the_site(self) -> None:
        # The control is real now: setting 2 against a site offering 4 means 2 run.
        view = self._view_with({"image.text_to_image": {"available_worker_slots": 4}}, stated=2)
        row = self._row(view, "studio-hub-test")
        self.assertEqual(row["limit"], 2)
        self.assertIn("You set 2", row["detail"])
        self.assertIn("Raise it to use the site's full 4", row["detail"])

    def test_the_operators_number_can_never_raise_a_site_above_what_it_offers(self) -> None:
        # The rule the `min` exists for: a number typed into a form is not licence to send a
        # site more concurrent work than it says it has free workers.
        view = self._view_with({"image.text_to_image": {"available_worker_slots": 4}}, stated=99)
        row = self._row(view, "studio-hub-test")
        self.assertEqual(row["limit"], 4, "the site's own count is the ceiling")
        self.assertIn("never over-schedules", row["detail"])

    def test_a_looser_cap_does_not_tell_you_to_raise_it(self) -> None:
        # The first version of this sentence said "raise it to use the rest" whichever way the
        # comparison fell, which is advice that does nothing when the site is already smaller —
        # the same defect as the sentence this row was fixed for in the first place.
        view = self._view_with({"image.text_to_image": {"available_worker_slots": 4}}, stated=99)
        self.assertNotIn("Raise it", self._row(view, "studio-hub-test")["detail"])

    def test_a_lane_spread_is_named_rather_than_flattened(self) -> None:
        # One row per provider, one budget per operation: the honest answer names both.
        view = self._view_with({
            "image.text_to_image": {"available_worker_slots": 4},
            "voice.tts": {"available_worker_slots": 1},
        })
        row = self._row(view, "studio-hub-test")
        self.assertEqual(row["limit"], 4, "the widest lane is the headline number")
        self.assertIn("voice.tts 1", row["detail"])
        self.assertIn("image.text_to_image 4", row["detail"])

    def test_a_uniform_site_says_so_instead_of_listing_itself(self) -> None:
        view = self._view_with({
            "image.text_to_image": {"available_worker_slots": 3},
            "voice.tts": {"available_worker_slots": 3},
        })
        self.assertIn("the same for every lane", self._row(view, "studio-hub-test")["detail"])

    def test_a_cloud_provider_keeps_the_settings_file_answer(self) -> None:
        # The Hub path must not leak onto providers it does not govern.
        with patch("gateway.fleet.snapshot", lambda **_: {"locations": []}):
            view = concurrency.settings_view(["openai-direct"])
        row = self._row(view, "openai-direct")
        # The page reports the very budget admission enforces: the owner's number when he wrote
        # one, the default when he did not.
        stated = concurrency.cloud_budget("openai-direct")
        self.assertEqual(row["source"], stated.source)
        self.assertEqual(row["limit"], stated.limit)

    def test_an_unreadable_fleet_does_not_stop_the_page_drawing(self) -> None:
        def boom(**_):
            raise RuntimeError("no fleet")
        with patch("gateway.fleet.snapshot", boom):
            view = concurrency.settings_view(["studio-hub-test", "openai-direct"])
        # Both asked-for providers are drawn (the owner's file may add more rows of its own), and
        # a location whose fleet cannot be read falls back to the ordinary budget.
        self.assertEqual({"studio-hub-test", "openai-direct"} - {
            row["provider_id"] for row in view["providers"]}, set())
        self.assertEqual(self._row(view, "studio-hub-test")["limit"],
                         concurrency.cloud_budget("studio-hub-test").limit)


class AHubLocationsNumberIsACapNotALicenceTest(unittest.TestCase):
    """`hub_budget` takes the smaller of the site's slots and the operator's number.

    The site's count is a live measurement of what it can physically take; the operator's
    number is policy. Policy may throttle; it may not over-schedule, because that is how a
    queue becomes a crash.
    """

    def test_no_cap_uses_the_site(self) -> None:
        budget = concurrency.hub_budget(4, operation="image.text_to_image")
        self.assertEqual(budget.limit, 4)
        self.assertEqual(budget.source, concurrency.SOURCE_HUB)

    def test_a_tighter_cap_wins(self) -> None:
        budget = concurrency.hub_budget(4, operation="image.text_to_image", stated=2)
        self.assertEqual(budget.limit, 2)
        self.assertEqual(budget.source, concurrency.SOURCE_CURATED)

    def test_a_looser_cap_does_not_raise_the_site(self) -> None:
        budget = concurrency.hub_budget(4, operation="image.text_to_image", stated=99)
        self.assertEqual(budget.limit, 4)
        self.assertEqual(budget.source, concurrency.SOURCE_HUB)

    def test_a_cap_with_no_answer_from_the_site_is_still_the_cap(self) -> None:
        budget = concurrency.hub_budget(None, operation="image.text_to_image", stated=3)
        self.assertEqual(budget.limit, 3)
        self.assertEqual(budget.source, concurrency.SOURCE_CURATED,
                         "a number the operator set must not be published as the site's answer")

    def test_neither_stated_stays_one_and_says_why(self) -> None:
        budget = concurrency.hub_budget(None, operation="image.text_to_image")
        self.assertEqual(budget.limit, concurrency.DEFAULT_PROVIDER)
        self.assertIn("did not state free slots", budget.detail)

    def test_an_equally_sized_cap_reports_your_limit_as_the_live_one(self) -> None:
        # A tie is not interesting numerically, but the source must still be honest about
        # which of the two is binding.
        budget = concurrency.hub_budget(3, operation="x", stated=3)
        self.assertEqual(budget.limit, 3)
        self.assertEqual(budget.source, concurrency.SOURCE_CURATED)


class DispatchReadsTheOperatorsNumberForALocationTest(unittest.TestCase):
    """The half that was missing: `_provider_budget` never read the settings file for a Hub."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "provider-concurrency.local.json"

    def _budget(self, slots: int, stated: int | None):
        self.path.write_text(json.dumps(
            {"providers": {"studio-hub-test": {"limit": stated}}} if stated is not None else {}
        ), encoding="utf-8")
        snap = {"locations": [{
            "provider_id": "studio-hub-test",
            "capacity": {"by_operation": {"image.text_to_image": {"available_worker_slots": slots}}},
        }]}
        worker = Worker(_FakeStore([], claim_always_fails=False), interval_s=10.0)
        with patch.object(concurrency, "SETTINGS_PATH", self.path), \
                patch("gateway.fleet.snapshot", lambda **_: snap):
            concurrency.reset_for_tests()
            return worker._provider_budget("studio-hub-test", "image")

    def test_without_a_number_the_site_decides(self) -> None:
        self.assertEqual(self._budget(6, None).limit, 6)

    def test_with_a_tighter_number_the_operator_decides(self) -> None:
        self.assertEqual(self._budget(6, 2).limit, 2)

    def test_with_a_looser_number_the_site_still_decides(self) -> None:
        self.assertEqual(self._budget(6, 50).limit, 6)


class TheGlobalLimitMovesWithoutARestartTest(unittest.TestCase):
    """The global cap is read at admission, not captured when the worker starts.

    ``concurrency`` promises the owner that a settings edit takes effect without a
    restart: *"a fix to a rate limit should take effect without a restart"*. That was
    true of the lane and provider caps, which were always read per admission, and it was
    **not** true of the global one — ``Worker.__init__`` froze it, so raising `global` on
    the Providers page changed what ``/admin/api/settings/concurrency`` reported and
    nothing about how many jobs ran. The class docstring had named
    ``concurrency.global_limit`` as the check the whole time.

    On the old code, ``test_raising_the_setting_raises_the_live_cap`` fails: the worker
    keeps the value it captured. It is written against a *live* Worker that is never
    restarted, because a restart is exactly what the promise says is unnecessary.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "provider-concurrency.local.json"
        patcher = patch.object(concurrency, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_global(self, value: int) -> None:
        # Different content each time so the mtime/size stamp cannot collide.
        self.path.write_text(json.dumps({"global": value, "_pad": "x" * value}), encoding="utf-8")

    def test_raising_the_setting_raises_the_live_cap(self) -> None:
        self._write_global(1)
        self.assertEqual(concurrency.global_limit(), 1)

        # No pool_size: the worker must ask the live limit every time it admits.
        w = Worker(_FakeStore([], claim_always_fails=False), interval_s=10.0)
        self.assertEqual(w._global_limit(), 1, "the worker should read the file, not a captured value")

        self._write_global(3)
        self.assertEqual(concurrency.global_limit(), 3,
                         "global_budget() must re-read the file it was pointed at")
        self.assertEqual(w._global_limit(), 3,
                         "the LIVE worker still gates at 1 — the setting did not take effect "
                         "without a restart, which is the promise concurrency makes")

    def test_lowering_the_setting_lowers_the_live_cap(self) -> None:
        # The same rule in the other direction: turning the fleet down must bite at once.
        self._write_global(5)
        w = Worker(_FakeStore([], claim_always_fails=False), interval_s=10.0)
        self.assertEqual(w._global_limit(), 5)
        self._write_global(2)
        self.assertEqual(w._global_limit(), 2)

    def test_an_explicit_pool_size_is_still_a_pin(self) -> None:
        # The tests and any deliberate caller pass a number and mean it — the live
        # settings file must not override an explicit choice.
        self._write_global(9)
        w = Worker(_FakeStore([], claim_always_fails=False), interval_s=10.0, pool_size=2)
        self.assertEqual(w._global_limit(), 2,
                         "an explicit pool_size is a pin and outranks the settings file")


class ConcurrencySettingsTest(unittest.TestCase):
    """The owner supplies these numbers, so the file has to behave like a setting."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "provider-concurrency.local.json"
        patcher = patch.object(concurrency, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_all_three_ceilings_round_trip(self) -> None:
        concurrency.save_settings({
            "global": 8,
            "lanes": {"image": 2},
            "providers": {"openai-direct": {"limit": 4, "note": "tier 4"}},
        })
        self.assertEqual(concurrency.global_limit(), 8)
        self.assertEqual(concurrency.lane_budget("image").limit, 2)
        budget = concurrency.cloud_budget("openai-direct")
        self.assertEqual(budget.limit, 4)
        self.assertEqual(budget.source, "curated")
        self.assertEqual(budget.detail, "tier 4")

    def test_the_file_beats_the_environment_and_says_so(self) -> None:
        # Two sources must never disagree in silence: the editable file wins, and
        # the answer names which one answered.
        with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": "99"}):
            self.assertEqual(concurrency.global_budget().detail, "GATEWAY_JOB_CONCURRENCY is set to 99")
            concurrency.save_settings({"global": 6})
            budget = concurrency.global_budget()
            self.assertEqual(budget.limit, 6)
            self.assertIn("provider-concurrency.local.json", budget.detail)

    def test_a_key_left_out_is_left_alone(self) -> None:
        # The page edits one row at a time; a write that names only the pool must
        # not erase every provider limit with it.
        concurrency.save_settings({"global": 8, "providers": {"a": 3}})
        concurrency.save_settings({"global": 5})
        self.assertEqual(concurrency.cloud_budget("a").limit, 3,
                         "writing one setting cleared another")
        self.assertEqual(concurrency.global_limit(), 5)

    def test_clearing_is_explicit_and_returns_to_the_default(self) -> None:
        concurrency.save_settings({"providers": {"a": 3}})
        self.assertEqual(concurrency.cloud_budget("a").source, "curated")
        concurrency.save_settings({"providers": {"a": None}})
        self.assertEqual(concurrency.cloud_budget("a").source, "gateway")
        self.assertEqual(concurrency.cloud_budget("a").limit, 1)

    def test_a_fraction_is_refused_rather_than_truncated(self) -> None:
        # 0.5 must not become 0: a written 0 means "nobody said", which is the one
        # answer this module exists to keep distinct from a real one.
        for bad in ("lots", -1, 0.5, True):
            with self.assertRaises(concurrency.SettingsRefused, msg=f"{bad!r} was accepted"):
                concurrency.save_settings({"global": bad})
        self.assertNotIn("global", concurrency._read_file(), "a refused write reached the file")

    def test_zero_clears_rather_than_stopping_the_gateway(self) -> None:
        # The same "0 is not a stop" rule the worker follows for a busy site.
        concurrency.save_settings({"global": 6})
        concurrency.save_settings({"global": 0})
        self.assertEqual(concurrency.global_limit(), concurrency.DEFAULT_GLOBAL)

    def test_the_view_lists_a_provider_that_has_no_limit_yet(self) -> None:
        # A page that drew only what was already set would hide exactly the rows
        # that still need a number.
        view = concurrency.settings_view(["openai-direct", "brand-new"])
        rows = {row["provider_id"]: row for row in view["providers"]}
        self.assertIn("brand-new", rows)
        self.assertIsNone(rows["brand-new"]["stated"])
        self.assertEqual(rows["brand-new"]["limit"], 1)
        self.assertEqual(rows["brand-new"]["source"], "gateway")

    def test_the_view_reports_what_is_stated_separately_from_what_is_in_force(self) -> None:
        concurrency.save_settings({"providers": {"openai-direct": 4}})
        rows = {row["provider_id"]: row
                for row in concurrency.settings_view(["openai-direct", "other"])["providers"]}
        self.assertEqual(rows["openai-direct"]["stated"], 4)
        self.assertIsNone(rows["other"]["stated"], "an unset row must read as unset, not as 1")




class TheReasonAndTheVerdictCannotPartTest(unittest.TestCase):
    """`can_publish` is `withhold_reason(...) is None`, by construction.

    Measured 2026-09-25: eleven enabled models on the local location were withheld,
    every one of them correctly — nine with `files_state: not_installed`, two the
    Hub does not catalogue as models at all (a codec and a tokenizer) — and the
    console said "this model cannot be offered" eleven times. The owner asked why,
    and the answer was already in the row.
    """

    def _inventory(self):
        from gateway.providers.credentials import CredentialStore
        from gateway.providers.models import ModelInventory

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        return ModelInventory(
            CredentialStore(path=root / "credentials.json"),
            path=root / "models-cache.json",
        )

    def _definition(self):
        # A real definition rather than a hand-built one: `serves()` reads the
        # adapter registry, so a fabricated provider would answer a question this
        # gateway never actually asks.
        from gateway.providers import registry

        definition = registry.definition("studio-hub-local")
        self.assertIsNotNone(definition, "the local location must stay a known provider")
        return definition

    def test_a_hub_row_that_is_not_ready_says_which_gate_held_it(self) -> None:
        inventory = self._inventory()
        definition = self._definition()
        cases = [
            ({"dispatch_state": "ineligible", "files_state": "not_installed"},
             "not installed"),
            ({"dispatch_state": "ineligible", "files_state": "files_verified"},
             "not ready to dispatch"),
            ({"catalogued": False}, "codec or a tokenizer"),
            ({"memory_eligible": False}, "memory"),
        ]
        for raw, expected in cases:
            model = {"model_id": "m", "modality": "image", "raw": raw}
            reason = inventory.withhold_reason(definition, model)
            self.assertIsNotNone(reason, f"{raw} was allowed through")
            self.assertIn(expected, reason, f"{raw} gave an unhelpful sentence: {reason!r}")
            self.assertFalse(inventory.can_publish(definition, model))

    def test_a_ready_model_is_allowed_and_gives_no_reason(self) -> None:
        inventory = self._inventory()
        definition = self._definition()
        model = {
            "model_id": "m", "modality": "image",
            "raw": {"dispatch_state": "eligible", "files_state": "files_verified"},
        }
        self.assertIsNone(inventory.withhold_reason(definition, model))
        self.assertTrue(inventory.can_publish(definition, model))
        # Absence stays unverified: a Hub that does not publish these withholds
        # nothing, which is the rule the whole check is built on.
        bare = {"model_id": "m", "modality": "image", "raw": {}}
        self.assertIsNone(inventory.withhold_reason(definition, bare))
        self.assertTrue(inventory.can_publish(definition, bare))

    def test_the_two_answers_agree_for_every_shape(self) -> None:
        # The invariant: one function, two answers. A separate "why?" reader is
        # free to disagree with the chooser the first time either gains a step.
        inventory = self._inventory()
        definition = self._definition()
        for modality in ("image", "chat", "video", "music"):
            for raw in ({}, {"catalogued": False}, {"memory_eligible": False},
                        {"dispatch_state": "ineligible"}, {"dispatch_state": "eligible"}):
                model = {"model_id": "m", "modality": modality, "raw": raw}
                self.assertEqual(
                    inventory.can_publish(definition, model),
                    inventory.withhold_reason(definition, model) is None,
                    f"{modality} / {raw} disagreed",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
