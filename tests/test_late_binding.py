#!/usr/bin/env python3
"""Scarcest-first scheduling at the gateway: spread, late binding, queue ahead.

Measured 2026-09-30 02:40: a 20-job music burst, submitted in the same second, was bound
to ONE location at submit (the lottery key was the model id, identical for every job of a
model, and the snapshot it ranked on is up to 15 s old). After binding a job only moved on
failure, so 16 of them bounced off a busy Hub with 409 while this Mac -- an ordinary
location, eligible and idle -- was given none.

What these hold, each mutation-proved (see docs/tasks/HANDOFF.md, 0.2.82):

  * equal-rank locations are SPREAD at submit (per-job routing key);
  * a job that was never sent is re-bound off a full site onto one with room, so the burst
    reaches every eligible location including local -- and a job that was dispatched, or
    has an upstream id, is NEVER moved (duplicate paid/render work is the worst outcome);
  * the queue is ordered scarcest-first (fewest eligible machines fleet-wide), then FIFO;
  * queue ahead: a site is admitted up to the ceiling it publishes (and the operator's
    number), whatever its free count says right now.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-late-binding-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})

from gateway import api, concurrency, fleet  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker, scarcest_first  # noqa: E402

MODEL = "stable-audio-open"
MODALITY = "music"
OPERATION = "music.generation"
COMPOUND = f"{fleet.FLEET_PROVIDER_ID}::{MODEL}"


def _pid(location_id: str) -> str:
    return f"{fleet.PROVIDER_PREFIX}{location_id}"


def _location(location_id: str, *, eligible: int, available: int, queue: int = 0) -> dict:
    return {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": True, "machines": [], "machine_summary": {"reachable": eligible},
        "running_jobs": [],
        "capacity": {
            "queue_depth": queue,
            "by_operation": {OPERATION: {
                "eligible_physical_machine_slots_total": eligible,
                "available_physical_machine_slots": available,
            }},
        },
    }


class _Fleet:
    """A patched fleet: a snapshot and the enabled-model rows that go with it."""

    def __init__(self, locations: list[dict]) -> None:
        self.locations = locations

    def __enter__(self):
        rows = [(types.SimpleNamespace(id=row["provider_id"]),
                 {"model_id": MODEL, "modality": MODALITY, "enabled": True, "raw": {}})
                for row in self.locations]
        self._patches = [
            patch.object(fleet, "snapshot", lambda allow_stale=False, **_: {"locations": self.locations}),
            patch.object(registry.models, "enabled_models", lambda modality: rows),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self._patches:
            item.stop()
        return False


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A settings file of our own: the owner's `global` must not bound a 20-job burst.
        settings = Path(tmp.name) / "provider-concurrency.local.json"
        settings.write_text(json.dumps({"global": 50}), encoding="utf-8")
        patcher = patch.object(concurrency, "SETTINGS_PATH", settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def submit(self, n: int) -> list[dict]:
        """Twenty same-second jobs through the real submit path (`api._submit`)."""
        with patch.object(api, "store", self.store):
            return [api._submit(MODALITY, COMPOUND, {}, f"key-{i}")[0] for i in range(n)]

    def bound(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.store._conn.execute("SELECT provider_id FROM jobs").fetchall():
            counts[row["provider_id"]] = counts.get(row["provider_id"], 0) + 1
        return counts


class SubmitSpreadsEqualRanksTest(_Base):
    def test_a_burst_is_not_all_bound_to_one_location(self) -> None:
        # Identical numbers everywhere: before, the lottery key was the model id, so every
        # job drew the same winner. One key per job spreads them.
        with _Fleet([_location(name, eligible=2, available=2) for name in ("a", "b", "local")]):
            self.submit(20)
            counts = self.bound()
        self.assertEqual(sum(counts.values()), 20)
        self.assertEqual(len(counts), 3, f"a burst of equal-rank jobs stacked up: {counts}")


class LateBindingTest(_Base):
    def _run_burst(self, locations: list[dict], *, jobs: int = 20, wait: float = 3.0):
        """Run a real Worker over a real store; every job holds its Mac until released."""
        release = threading.Event()
        started: dict[str, list[str]] = {}
        lock = threading.Lock()

        def execute(job, store):
            with lock:
                started.setdefault(job["provider_id"], []).append(job["id"])
            release.wait(10)
            store.succeed(job["id"], result={})

        with _Fleet(locations), patch.object(worker_module, "execute_job", execute):
            self.submit(jobs)
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            worker.start()
            try:
                time.sleep(wait)
                with lock:
                    snapshot = {k: list(v) for k, v in started.items()}
            finally:
                release.set()
                worker.wait_idle(10)
                worker.stop()
        return snapshot

    def test_the_burst_reaches_every_eligible_location_including_local(self) -> None:
        # The measured failure. `big` has the most free Macs, so every job is bound to it at
        # submit; only late binding gets work onto the others.
        locations = [
            _location("big", eligible=6, available=6),
            _location("mid", eligible=3, available=3),
            _location("local", eligible=1, available=1),
        ]
        started = self._run_burst(locations)
        ran = {pid: len(ids) for pid, ids in started.items()}
        self.assertGreaterEqual(ran.get(_pid("local"), 0), 1, f"this Mac got nothing: {ran}")
        self.assertGreaterEqual(ran.get(_pid("mid"), 0), 1, f"a location sat idle: {ran}")
        # And every eligible Mac is in use at once: 6 + 3 + 1, no more (queue ahead is a ceiling).
        self.assertEqual(sum(ran.values()), 10, ran)
        self.assertEqual(ran[_pid("local")], 1)

    def test_a_dispatched_job_is_never_rebound(self) -> None:
        # Sent (dispatch_started_at) and sent-with-an-id (upstream_job_id), then put back in
        # the queue as a deferral does: both must stay exactly where they are.
        with _Fleet([_location("full", eligible=1, available=0),
                     _location("free", eligible=4, available=4)]):
            sent, _ = self.store.submit(modality=MODALITY, model_id=MODEL, params={},
                                        idempotency_key="sent", provider_id=_pid("full"))
            held, _ = self.store.submit(modality=MODALITY, model_id=MODEL, params={},
                                        idempotency_key="held", provider_id=_pid("full"))
            self.store.mark_running(sent["id"])
            self.store.begin_dispatch(sent["id"])
            self.store.defer(sent["id"], delay_s=0, reason="test")
            self.store.mark_running(held["id"])
            self.store.begin_dispatch(held["id"])
            self.store.record_upstream_acceptance(held["id"], "batch-1")
            self.store.defer(held["id"], delay_s=0, reason="test")

            for job in (sent, held):
                self.assertFalse(self.store.rebind_unsent(
                    job["id"], from_provider=_pid("full"), to_provider=_pid("free")))
                self.assertEqual(self.store.get(job["id"])["provider_id"], _pid("full"))

            # ... and through the worker: the full site refuses, room exists elsewhere, and the
            # jobs -- read fresh from the store, as a pass does -- are still not moved.
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            with worker._budget_lock:
                worker._per_lane[(_pid("full"), MODALITY)] = 1
            for job in (self.store.get(sent["id"]), self.store.get(held["id"])):
                self.assertFalse(worker._rebind(job))
                self.assertEqual(job["provider_id"], _pid("full"))
                self.assertEqual(self.store.get(job["id"])["provider_id"], _pid("full"))

    def test_an_unsent_job_moves_and_a_second_move_from_the_old_place_does_not(self) -> None:
        job, _ = self.store.submit(modality=MODALITY, model_id=MODEL, params={},
                                   idempotency_key="k", provider_id=_pid("full"))
        self.assertTrue(self.store.rebind_unsent(
            job["id"], from_provider=_pid("full"), to_provider=_pid("free")))
        self.assertEqual(self.store.get(job["id"])["provider_id"], _pid("free"))
        self.assertFalse(self.store.rebind_unsent(
            job["id"], from_provider=_pid("full"), to_provider=_pid("other")))

    def test_a_voice_pinned_job_is_not_rebound(self) -> None:
        with _Fleet([_location("full", eligible=1, available=0),
                     _location("free", eligible=4, available=4)]):
            job, _ = self.store.submit(
                modality=MODALITY, model_id=MODEL, idempotency_key="v",
                params={"voice_id": f"{_pid('full')}::clip"}, provider_id=_pid("full"))
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            with worker._budget_lock:
                worker._per_lane[(_pid("full"), MODALITY)] = 1
            self.assertFalse(worker._rebind(self.store.get(job["id"])))
            self.assertEqual(self.store.get(job["id"])["provider_id"], _pid("full"))


class ScarcestFirstTest(unittest.TestCase):
    def test_fewest_machines_first_then_arrival_order(self) -> None:
        pools = {"scarce": 1, "common": 8, "nowhere": 0}
        jobs = [
            {"id": "c1", "provider_id": _pid("a"), "model_id": "common", "modality": "image"},
            {"id": "n1", "provider_id": _pid("a"), "model_id": "nowhere", "modality": "image"},
            {"id": "s1", "provider_id": _pid("a"), "model_id": "scarce", "modality": "image"},
            {"id": "c2", "provider_id": _pid("a"), "model_id": "common", "modality": "image"},
            {"id": "x1", "provider_id": "openai-direct", "model_id": "gpt", "modality": "image"},
            {"id": "s2", "provider_id": _pid("a"), "model_id": "scarce", "modality": "image"},
        ]
        with patch.object(fleet, "pool_size", lambda model, modality: pools[model]):
            ordered = [job["id"] for job in scarcest_first(jobs)]
        # cloud (takes no Mac) first, then the scarce model FIFO, then the common one FIFO,
        # and a model nothing can run right now last.
        self.assertEqual(ordered, ["x1", "s1", "s2", "c1", "c2", "n1"])

    def test_the_pool_is_the_sum_of_published_eligible_slots(self) -> None:
        with _Fleet([_location("a", eligible=3, available=0), _location("b", eligible=2, available=2),
                     dict(_location("c", eligible=9, available=9), reachable=False)]):
            self.assertEqual(fleet.pool_size(MODEL, MODALITY), 5, "unreachable sites do not count")


class QueueAheadTest(_Base):
    def _peak(self, locations: list[dict], jobs: int = 8) -> int:
        release = threading.Event()
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def execute(job, store):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            release.wait(10)
            with lock:
                live["now"] -= 1
            store.succeed(job["id"], result={})

        with _Fleet(locations), patch.object(worker_module, "execute_job", execute):
            for i in range(jobs):
                self.store.submit(modality=MODALITY, model_id=MODEL, params={},
                                  idempotency_key=f"q{i}", provider_id=locations[0]["provider_id"])
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            worker.start()
            try:
                time.sleep(1.0)
                peak = live["peak"]
            finally:
                release.set()
                worker.wait_idle(10)
                worker.stop()
        return peak

    def test_a_site_takes_up_to_its_eligible_total_even_when_every_mac_is_busy(self) -> None:
        # Free 0 (the Macs are running someone else's work): the Hub's durable queue holds
        # the next jobs for whichever Mac frees. It used to be one at a time.
        self.assertEqual(self._peak([_location("busy", eligible=4, available=0)]), 4)

    def test_the_operators_number_is_still_a_ceiling(self) -> None:
        settings = concurrency.SETTINGS_PATH
        settings.write_text(json.dumps({"global": 50, "providers": {
            _pid("busy"): {"limit": 2, "note": "test"}}}), encoding="utf-8")
        concurrency.reset_for_tests()
        self.assertEqual(self._peak([_location("busy", eligible=4, available=0)]), 2)

    def test_no_stated_total_keeps_the_free_count(self) -> None:
        self.assertEqual(concurrency.hub_budget(3, operation=OPERATION).limit, 3)
        self.assertEqual(concurrency.hub_budget(0, operation=OPERATION).limit,
                         concurrency.DEFAULT_PROVIDER)
        self.assertEqual(concurrency.hub_budget(0, operation=OPERATION, ceiling=4).limit, 4)
        self.assertEqual(concurrency.hub_budget(0, operation=OPERATION, ceiling=4,
                                                reachable=False).limit, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
