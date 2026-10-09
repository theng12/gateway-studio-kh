#!/usr/bin/env python3
"""0.2.156: the four fixes from the 2026-10-09 overnight review.

1. A re-route counts this Gateway's own jobs and the lane budget: a burst cannot pile onto one site
   (10-08: This Mac, TTS limit 1, held 39 re-routed TTS jobs), and when every lane is full the job
   waits here, not in a full site's Hub queue.
2. Every remote site going silent at once is our fault: polls keep asking instead of moving the job
   (10-09 06:26 and 06:57, 59 renders abandoned). One silent site is still moved.
3. A late rebind that raises ("database is locked", 2,758 loops per job on 10-07) backs off and logs once.
4. Qwen prefers fast Macs (owner 2026-10-09): ranked by expected finish; a slow Mac only when waiting
   for a fast one would finish later.

Mocked fleet snapshots, fake adapters and a temp store only: no Hub, no generation, no installed app.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402

from gateway import concurrency, fleet  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

FLUX = "flux-test"
QWEN = "mlx-community/Qwen-Image-2.1-mflux-q4"
IMAGE = "image"
OPERATION = "image.text_to_image"


def _pid(location_id: str) -> str:
    return f"{fleet.PROVIDER_PREFIX}{location_id}"


def _site(location_id: str, *, eligible: int, available: int | None = None, queue: int = 0,
          chip: str = "", busy: bool = False, model: str = FLUX) -> dict:
    """A location row as the snapshot has it. `chip` adds one fitting Mac for the speed rank."""
    available = eligible if available is None else available
    row = {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": True, "machines": [], "machine_summary": {"reachable": eligible},
        "running_jobs": [],
        "capacity": {"queue_depth": queue, "by_operation": {OPERATION: {
            "eligible_physical_machine_slots_total": eligible,
            "available_physical_machine_slots": available}}},
    }
    if chip:
        row.update(
            machines=[{"machine": "local", "chip": chip, "memory_gb": 17.18, "reachable": True}],
            local_models=[{"model_id": model, "machine": "local", "modality": IMAGE,
                           "memory_eligible": True, "available": True}],
            workers=[{"machine": "local", "models": [model], "online": True, "busy": busy}],
            model_supply={model: {"eligible": eligible, "available": available}},
        )
    return row


class _Fleet:
    """A patched fleet: the snapshot and the enabled-model rows (`extra` enabled but not in it)."""

    def __init__(self, sites: list[dict], model: str = FLUX, extra: tuple[str, ...] = ()) -> None:
        self.sites, self.model, self.extra = sites, model, extra

    def __enter__(self) -> "_Fleet":
        ids = [row["provider_id"] for row in self.sites] + [_pid(x) for x in self.extra]
        rows = [(types.SimpleNamespace(id=pid), {"model_id": self.model, "modality": IMAGE})
                for pid in ids]
        self._patches = [
            patch.object(fleet, "snapshot", lambda allow_stale=False, **_: {"locations": self.sites}),
            patch.object(registry.models, "enabled_models", lambda _modality: rows),
            patch.object(fleet, "_speed_samples", lambda _model: []),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc) -> bool:
        for item in self._patches:
            item.stop()
        return False


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(dir=_SANDBOX.name)
        self.addCleanup(tmp.cleanup)
        settings = Path(tmp.name) / "provider-concurrency.local.json"
        settings.write_text(json.dumps({"global": 100}), encoding="utf-8")
        patcher = patch.object(concurrency, "SETTINGS_PATH", settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.worker = Worker(self.store)

    def running_at(self, key: str, site: str, model: str = FLUX) -> dict:
        """A job running at `site`, booked by the worker and given its room hook, as `_run` does."""
        row, _ = self.store.submit(modality=IMAGE, model_id=model, params={"prompt": key},
                                   idempotency_key=key, provider_id=_pid(site))
        self.assertTrue(self.store.mark_running(row["id"]))
        job = self.store.get(row["id"])
        self.worker._book(job)
        worker_module._ROOM_HOOKS[job["id"]] = (
            lambda pid, claim=False, job=job: self.worker._site_room(job, pid, claim))
        self.addCleanup(worker_module._ROOM_HOOKS.pop, job["id"], None)
        return job

    def booked(self, site: str) -> int:
        return self.worker._per_lane.get((_pid(site), IMAGE), 0)


class RerouteLaneBudgetTests(_Base):
    def reroute(self, job: dict, source: str = "a") -> str | None:
        moved = worker_module._next_machine(self.store, job["id"], job["model_id"], IMAGE, _pid(source),
                                            code="LOCATION_NOT_READY", detail="not ready")
        if moved not in (None, worker_module.QUEUED_IN_GATEWAY):
            self.worker._move(job, moved)  # what `_moved` does after the claim: idempotent
        return moved

    def test_a_burst_of_40_never_exceeds_a_lane_and_the_rest_wait_in_the_gateway(self) -> None:
        limits = {"local": 1, "b": 4, "c": 10}
        with _Fleet([_site(name, eligible=n) for name, n in limits.items()]):
            answers = [self.reroute(self.running_at(f"burst-{i}", "a")) for i in range(40)]
        for name, limit in limits.items():
            self.assertEqual(self.booked(name), limit, f"{name} filled to its lane, never past it")
        queued = [a for a in answers if a == worker_module.QUEUED_IN_GATEWAY]
        self.assertEqual(len(queued), 40 - sum(limits.values()))
        waiting = [self.store.get(f"{j['id']}") for j in self.store.due()]
        self.assertEqual(len(waiting), len(queued), "the rest are due again here, not sent")
        for row in waiting:
            self.assertEqual(row["state"], "accepted")
            self.assertIsNone(row["dispatch_started_at"])
            self.assertIsNone(row["upstream_job_id"])
            self.assertIn(row["provider_id"], {_pid(n) for n in limits})
            self.assertIn("waiting for room", row["defer_reason"])

    def test_a_smaller_burst_spreads_by_free_budget(self) -> None:
        with _Fleet([_site("b", eligible=4), _site("c", eligible=10)]):
            for i in range(8):
                self.assertNotEqual(self.reroute(self.running_at(f"spread-{i}", "a")),
                                    worker_module.QUEUED_IN_GATEWAY)
        self.assertGreaterEqual(self.booked("b"), 1, "the smaller site still gets its share")
        self.assertGreater(self.booked("c"), self.booked("b"), "the bigger free budget takes more")
        self.assertLessEqual(self.booked("b"), 4)

    def test_without_the_worker_hook_a_reroute_is_unchanged(self) -> None:
        row, _ = self.store.submit(modality=IMAGE, model_id=FLUX, params={}, idempotency_key="plain",
                                   provider_id=_pid("a"))
        with _Fleet([_site("b", eligible=1, available=0, queue=50)]):
            moved = worker_module._next_machine(self.store, row["id"], FLUX, IMAGE, _pid("a"),
                                                code="LOCATION_NOT_READY", detail="x")
        self.assertEqual(moved, _pid("b"))

    def test_a_known_failure_with_every_lane_full_waits_here_instead_of_queueing_at_a_full_site(self) -> None:
        calls: list[str] = []

        class _Refuses:
            def generate(self, request):
                calls.append(request.provider_id)
                raise BackendError("not ready", code="LOCATION_NOT_READY", retryable=True,
                                   upstream_accepted=False)

        definition = lambda pid: types.SimpleNamespace(  # noqa: E731
            id=pid, kind="studio_hub", base_url="", display_name=pid)
        with _Fleet([_site("b", eligible=1)], extra=("a",)), \
                patch.object(registry, "definition", definition), \
                patch.object(registry.credentials, "is_enabled", lambda _p: True), \
                patch.object(registry.credentials, "resolved", lambda _p: {}), \
                patch.object(worker_module, "adapter_for", lambda _d: _Refuses()):
            self.running_at("fills-b", "b")
            job = self.running_at("full", "a")
            self.store._update(job["id"], state="accepted")  # execute_job claims it itself
            worker_module.execute_job(job, self.store)
        row = self.store.get(job["id"])
        self.assertEqual(calls, [_pid("a")], "never sent to the full site")
        self.assertEqual((row["state"], row["provider_id"]), ("accepted", _pid("b")))
        self.assertEqual(self.booked("b"), 1, "b's lane is not over its limit")


class FleetOutageTests(unittest.TestCase):
    def setUp(self) -> None:
        hub_module._SILENT.clear()
        self.addCleanup(hub_module._SILENT.clear)
        for name, value in (("POLL_INTERVAL_S", 0.01), ("POLL_DROP_GRACE_S", 0.05)):
            patcher = patch.object(hub_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def poll(self, base: str, silent_polls: int) -> dict:
        seen = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["n"] += 1
            if seen["n"] <= silent_polls:
                raise httpx.ConnectError("no route to host")
            return httpx.Response(200, json={"items": [{"index": 0, "state": "done"}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return hub_module._poll_batch(client, base, {}, "batch-1", 0, deadline_seconds=30)

    def silent_elsewhere(self, *bases: str, since: float = 0.0) -> None:
        now = time.monotonic()
        for base in bases:
            hub_module._SILENT[base] = [now - since, now]

    def test_one_silent_site_is_still_given_up_on_after_the_grace(self) -> None:
        with self.assertRaises(BackendError) as caught:
            self.poll("https://hub-a.test", silent_polls=10_000)
        self.assertTrue(caught.exception.recovery["lost_contact"])

    def test_every_remote_site_silent_at_once_keeps_polling_until_the_site_answers(self) -> None:
        self.silent_elsewhere("https://hub-b.test", "https://hub-c.test")
        started = time.monotonic()
        summary = self.poll("https://hub-a.test", silent_polls=30)
        self.assertGreater(time.monotonic() - started, hub_module.POLL_DROP_GRACE_S,
                           "it rode out a silence longer than the grace")
        self.assertEqual(summary["item"]["state"], "done")
        self.assertNotIn("https://hub-a.test", hub_module._SILENT, "the answer ends its silence")

    def test_sites_that_went_silent_long_before_do_not_make_an_outage(self) -> None:
        self.silent_elsewhere("https://hub-b.test", "https://hub-c.test", since=600)
        with self.assertRaises(BackendError):
            self.poll("https://hub-a.test", silent_polls=10_000)

    def test_this_macs_own_hub_is_not_counted_as_a_remote_site(self) -> None:
        self.silent_elsewhere("https://hub-b.test", "http://127.0.0.1:47873")
        with self.assertRaises(BackendError):
            self.poll("https://hub-a.test", silent_polls=10_000)


class RebindBackoffTests(_Base):
    def test_a_locked_database_is_retried_with_backoff_and_logged_once(self) -> None:
        calls: list[str] = []

        def locked(job_id, **_k):
            calls.append(job_id)
            raise sqlite3.OperationalError("database is locked")

        with _Fleet([_site("a", eligible=1), _site("b", eligible=4)]):
            self.running_at("holds-a", "a")
            row, _ = self.store.submit(modality=IMAGE, model_id=FLUX, params={}, idempotency_key="w",
                                       provider_id=_pid("a"))
            job = self.store.get(row["id"])
            with patch.object(self.store, "rebind_unsent", locked), \
                    self.assertLogs("gateway.worker", "WARNING") as logs:
                delays = []
                for _ in range(12):
                    for _ in range(20):  # twenty dispatch passes inside one backoff: one try
                        self.worker._rebind(job)
                    delays.append(self.worker._rebind_backoff[job["id"]][1])
                    self.worker._rebind_backoff[job["id"]] = (0.0, delays[-1])  # let it come due
            self.assertEqual(len(calls), 12, "one try per backoff, not one per pass")
            self.assertEqual(delays[:4], [1.0, 2.0, 4.0, 8.0])
            self.assertEqual(max(delays), worker_module._REBIND_BACKOFF_CAP_S)
            self.assertEqual(sum("late rebind" in line for line in logs.output), 1, "logged once")
            self.assertTrue(self.worker._rebind(job), "the lock clears and the job moves")
        self.assertNotIn(job["id"], self.worker._rebind_backoff)


class QwenSpeedTests(_Base):
    def sites(self, *, local_busy: bool = False, local_queue: int = 0) -> list[dict]:
        return [
            _site("local", eligible=1, available=0 if local_busy else 1, queue=local_queue,
                  chip="Apple M2 Max", busy=local_busy, model=QWEN),
            _site("m4", eligible=1, chip="Apple M4", model=QWEN),
            _site("m1", eligible=1, chip="Apple M1", model=QWEN),
        ]

    def chosen(self, sites: list[dict], keys: int = 12) -> set[str]:
        with _Fleet(sites, model=QWEN):
            return {fleet.route(QWEN, IMAGE, routing_key=f"k{i}")[0].id for i in range(keys)}

    def test_the_fastest_idle_mac_is_chosen_first(self) -> None:
        self.assertEqual(self.chosen(self.sites()), {_pid("local")})
        idle_m4 = [s for s in self.sites(local_busy=True, local_queue=5)]
        self.assertEqual(self.chosen(idle_m4), {_pid("m4")}, "then the M4, never the M1")

    def test_measured_history_outranks_the_chip(self) -> None:
        with patch.object(fleet, "_speed_samples", lambda _m: [(_pid("m1"), "local", 100.0)] * 3):
            sites = self.sites()
            with patch.object(fleet, "snapshot", lambda allow_stale=False, **_: {"locations": sites}), \
                    patch.object(registry.models, "enabled_models", lambda _m: [
                        (types.SimpleNamespace(id=s["provider_id"]), {"model_id": QWEN}) for s in sites]):
                self.assertEqual(fleet.route(QWEN, IMAGE, routing_key="h")[0].id, _pid("m1"))

    def test_a_slow_mac_is_used_only_when_waiting_for_a_fast_one_would_finish_later(self) -> None:
        only_local_and_m1 = lambda **k: [s for s in self.sites(**k) if s["id"] != "m4"]  # noqa: E731
        # This Mac busy, nothing queued: 228 x 1.5 = 342 s beats the idle M1's 949 s, so wait.
        self.assertEqual(self.chosen(only_local_and_m1(local_busy=True)), {_pid("local")})
        # Three queued ahead: 228 x 4.5 = 1,026 s, later than the M1 would finish.
        self.assertEqual(self.chosen(only_local_and_m1(local_busy=True, local_queue=3)), {_pid("m1")})

    def test_a_late_rebind_stays_for_the_fast_mac_until_the_line_is_too_long(self) -> None:
        sites = [s for s in self.sites() if s["id"] != "m4"]
        with _Fleet(sites, model=QWEN):
            self.running_at("on-this-mac", "local", model=QWEN)  # This Mac's one lane is ours
            row, _ = self.store.submit(modality=IMAGE, model_id=QWEN, params={}, idempotency_key="q",
                                       provider_id=_pid("local"))
            job = self.store.get(row["id"])
            self.assertFalse(self.worker._rebind(job), "570 s here beats 949 s on the M1: it waits")
            self.worker._ahead[(_pid("local"), QWEN)] = 2  # two of ours already waiting for it
            self.assertTrue(self.worker._rebind(job), "1,026 s here: the M1 finishes it sooner")
        self.assertEqual(self.store.get(row["id"])["provider_id"], _pid("m1"))

    def test_other_models_keep_the_pressure_rank(self) -> None:
        with _Fleet([_site("b", eligible=1, chip="Apple M1")]):
            candidate = fleet.routing_candidates(FLUX, IMAGE)[0]
        self.assertNotIn("fast_s", candidate)
        self.assertFalse(fleet.speed_ranked(FLUX))
        self.assertTrue(fleet.speed_ranked("ddalcu/Qwen-Image-2.1-MLX-Serve-4bit"))


if __name__ == "__main__":
    unittest.main()
