"""Concurrency is bounded, and the bound is the one we publish.

The gateway ran exactly one job at a time. That was right for one owner and one
Mac — and with a fleet it means nineteen idle machines, which is the whole reason
this changed.

Three ceilings, and these tests hold each of them to a number a reader could check:

  * **global** — the pool size, and the `paid_concurrency_limit` Story Studio reads;
  * **lane** — a modality cap, off by default;
  * **provider** — a cloud lane's curated limit, or a Studio Hub site's own
    `available_worker_slots`.

Two properties matter more than the numbers and are tested the same way each time:
**a limit is never exceeded**, and **a limit of zero never stalls the queue** — a
site reporting no free slots must still get work, or a busy fleet would wedge
behind a number that only meant "busy right now".
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
        self._patches = [
            patch.object(worker_module, "execute_job", self._execute),
            # No live fleet: an empty location list makes every provider fall through
            # to its cloud budget, which is what these tests are about.
            patch("gateway.fleet.snapshot", lambda **_: {"locations": self.locations}),
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

    def test_a_cloud_lane_runs_one_at_a_time_by_default(self) -> None:
        # The shipping default: over-scheduling is what produces 429s, and a 429 on
        # a submitted job is a durable hold rather than a clean retry.
        jobs = [_job(f"job-{n}") for n in range(4)]
        with _Harness(jobs, pool_size=4, hold=0.2) as h:
            self.assertTrue(h.wait_for(4))
            self.assertEqual(h.tracker.provider("openai-direct"), 1)

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
            "capacity": {"by_operation": {"image.text_to_image": {"available_worker_slots": 0}}},
        }]
        jobs = [_job(f"job-{n}", provider="studio-hub-abc") for n in range(2)]
        with _Harness(jobs, pool_size=2, hold=0.1, locations=locations) as h:
            self.assertTrue(h.wait_for(2), "a site with no free slots stalled the queue")
            self.assertEqual(h.tracker.provider("studio-hub-abc"), 1)


class LaneBudgetsTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_a_lane_cap_bounds_that_lane(self) -> None:
        with patch.dict(os.environ, {"GATEWAY_LANE_CONCURRENCY": "image=1"}):
            concurrency.reset_for_tests()
            jobs = [_job(f"job-{n}", provider=f"provider-{n}") for n in range(3)]
            with _Harness(jobs, pool_size=4, hold=0.25) as h:
                self.assertTrue(h.wait_for(3))
                self.assertEqual(h.tracker.provider("provider-0"), 1)
                self.assertLessEqual(h.tracker.peak, 1, "the lane cap did not bound the lane")

    def test_a_lane_with_no_cap_may_use_the_whole_pool(self) -> None:
        concurrency.reset_for_tests()
        budget = concurrency.lane_budget("image")
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
            self.assertEqual(h.worker.budgets["openai-direct"]["source"], "gateway")
            self.assertEqual(h.worker.budgets["openai-direct"]["limit"], 1)


class ThePublishedLimitIsTheRealOneTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_paid_concurrency_limit_is_the_pool_size(self) -> None:
        # It was the literal 4 while the worker ran one job at a time — a published
        # number that did not describe the system, in a field Story Studio reads.
        with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": "7"}):
            concurrency.reset_for_tests()
            self.assertEqual(constants.admission()["paid_concurrency_limit"], 7)
            self.assertEqual(concurrency.global_limit(), 7)

    def test_a_bad_value_falls_back_rather_than_stopping_the_gateway(self) -> None:
        # "0" or "many" must not mean "take no work at all".
        for bad in ("0", "-2", "many", ""):
            with patch.dict(os.environ, {"GATEWAY_JOB_CONCURRENCY": bad}):
                self.assertEqual(concurrency.global_limit(), concurrency.DEFAULT_GLOBAL)

    def test_the_admission_record_still_carries_the_other_limits(self) -> None:
        published = constants.admission()
        self.assertIn("max_queued_jobs", published)
        self.assertIn("generation_submissions_per_minute", published)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
