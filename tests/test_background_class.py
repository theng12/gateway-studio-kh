#!/usr/bin/env python3
"""The background job class (0.2.165, owner 2026-10-09: "say i approve a pool of 500 songs, can we fill
up the fleet if there's nothing else generating?").

  * `priority: "background"` on any request; absent/null is normal, anything else is refused;
  * normal work is dispatched before queued background work, whenever it arrived;
  * background fills idle Macs (the Hub's own idle count), never the Hub's queue, and never a site
    with an idle Mac a waiting normal job could use;
  * no pre-emption: running background work finishes, then the Mac takes normal work;
  * two background clients take turns;
  * `GET /v1/fleet/capacity` and the queue ceiling that background never fills for production.
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

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-background-"))
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

from fastapi import Response  # noqa: E402

from gateway import api, concurrency, fleet  # noqa: E402
from gateway.api import GatewayError  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker, scarcest_first  # noqa: E402

MUSIC, IMAGE, SONG2 = "stable-audio-3", "flux-klein", "yue2"
OPS = {"music": "music.generation", "image": "image.text_to_image"}


def _pid(location_id: str) -> str:
    return f"{fleet.PROVIDER_PREFIX}{location_id}"


def _site(location_id: str, macs: int, idle: dict[str, int]) -> dict:
    """One location of `macs` Macs that all run both models; `idle` is the Hub's free count per model."""
    return {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": True, "machines": [], "machine_summary": {"reachable": macs}, "running_jobs": [],
        "capacity": {"queue_depth": 0, "by_operation": {op: {
            "eligible_physical_machine_slots_total": macs,
            "available_physical_machine_slots": macs} for op in OPS.values()}},
        "model_supply": {model: {"eligible": macs, "available": idle.get(model, 0)}
                         for model in set(idle) | {MUSIC, IMAGE}},
    }


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Path(tmp.name) / "provider-concurrency.local.json"
        settings.write_text(json.dumps({"global": 50}), encoding="utf-8")
        self.sites: list[dict] = []
        rows = {modality: [] for modality in OPS}

        def enabled(modality):
            return [(types.SimpleNamespace(id=row["provider_id"]),
                     {"model_id": MUSIC if modality == "music" else IMAGE, "modality": modality, "raw": {}})
                    for row in self.sites] if modality in rows else []

        for patcher in (patch.object(concurrency, "SETTINGS_PATH", settings),
                        patch.object(fleet, "snapshot", lambda *a, **k: {"locations": self.sites}),
                        patch.object(registry.models, "enabled_models", enabled)):
            patcher.start()
            self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def normal(self, modality: str, model: str, *, site: str = "g") -> str:
        """A normal job of any lane, waiting and due now (nothing is dispatched for it here)."""
        self._n += 1
        row, _ = self.store.submit(
            modality=modality, model_id=model, params={}, idempotency_key=f"n{self._n}",
            provider_id=_pid(site))
        with self.store._lock:
            self.store._conn.execute("UPDATE jobs SET next_action_at = 0, created_at = ? WHERE id = ?",
                                     (time.time() - 60 + self._n, row["id"]))
            self.store._conn.commit()
        return row["id"]

    def job(self, model: str = MUSIC, *, background: bool = False, group: str | None = None,
            site: str = "g") -> str:
        self._n += 1
        row, _ = self.store.submit(
            modality="image" if model == IMAGE else "music", model_id=model, params={},
            idempotency_key=f"k{self._n}", provider_id=_pid(site), job_group=group,
            priority="background" if background else None)
        # Due now, in arrival order (the 0.8 s first-dispatch delay is not what is tested here).
        with self.store._lock:
            self.store._conn.execute("UPDATE jobs SET next_action_at = 0, created_at = ? WHERE id = ?",
                                     (time.time() - 60 + self._n, row["id"]))
            self.store._conn.commit()
        return row["id"]


class _Running:
    """A real Worker over the real store; every job holds its Mac until `finish(id)`."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.gates: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def execute(self, job, store) -> None:
        gate = threading.Event()
        with self.lock:
            self.gates[job["id"]] = gate
            self.started.append(job["id"])
        gate.wait(10)
        store.succeed(job["id"], result={})

    def __enter__(self):
        self.patch = patch.object(worker_module, "execute_job", self.execute)
        self.patch.start()
        self.worker = Worker(self.store, interval_s=0.01, pool_size=0)
        self.worker.start()
        return self

    def settle(self, seconds: float = 0.4) -> list[str]:
        time.sleep(seconds)
        with self.lock:
            return list(self.started)

    def finish(self, job_id: str) -> None:
        self.gates[job_id].set()
        deadline = time.time() + 5
        while self.store.get(job_id)["state"] != "succeeded" and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # the job thread releases its booking after `succeed`

    def __exit__(self, *exc):
        for gate in list(self.gates.values()):
            gate.set()
        self.worker.wait_idle(10)
        self.worker.stop()
        self.patch.stop()
        return False


class SchedulingTest(_Base):
    def test_normal_beats_queued_background(self) -> None:
        # One idle Mac; three songs queued first, then one narration-class job: the normal one runs.
        self.sites = [_site("g", macs=1, idle={MUSIC: 1})]
        songs = [self.job(background=True) for _ in range(3)]
        normal = self.job()
        with _Running(self.store) as run:
            self.assertEqual(run.settle(), [normal])
        self.assertTrue(all(self.store.get(s)["state"] in ("accepted", "succeeded") for s in songs))

    def test_background_fills_idle_macs_and_never_the_hub_queue(self) -> None:
        # Four idle Macs, ten songs: four run now; the other six wait here, not in the Hub's queue.
        self.sites = [_site("g", macs=4, idle={MUSIC: 4})]
        songs = [self.job(background=True) for _ in range(10)]
        with _Running(self.store) as run:
            started = run.settle()
            self.assertEqual(started, songs[:4])
            # The Hub says every Mac is busy now: no more go, whatever the lane budget would allow.
            self.sites = [_site("g", macs=8, idle={MUSIC: 0})]
            self.assertEqual(run.settle(), songs[:4])

    def test_a_site_with_an_idle_mac_a_waiting_normal_job_could_use_takes_no_background(self) -> None:
        # A normal job waiting **in this lane** for a model this site has an idle Mac for: that Mac
        # is the normal job's, and no background job of that lane is sent here.
        self.sites = [_site("g", macs=2, idle={MUSIC: 1, SONG2: 1})]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        self.job(MUSIC)  # a normal music job, waiting (the worker is not running)
        song = self.store.get(self.job(SONG2, background=True))
        self.assertFalse(worker._admit(song))
        # Once no Mac that could run the normal job is idle, the song may take its own Mac.
        self.sites = [_site("g", macs=2, idle={MUSIC: 0, SONG2: 1})]
        worker._normal_waiting = None  # a new pass reads the waiting work again
        self.assertTrue(worker._admit(song))

    def test_a_normal_job_in_another_lane_does_not_reserve_this_lane_s_idle_macs(self) -> None:
        """Live, 2026-10-29: seven normal Breeze-TTS jobs stranded on a site that did not publish
        Breeze made all six image sites refuse **132 background image jobs for 3.5 hours** while
        their image Macs sat idle, because each image site also lists Breeze.

        Capacity is published per model and per lane (`model_supply[model]`,
        `capacity.by_operation`), so a waiting narration job does not reserve an image Mac: it
        cannot run there and no image job competes with it for one.
        """
        self.sites = [_site("g", macs=2, idle={MUSIC: 1, IMAGE: 1, "breeze-tts": 1})]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        self.normal("tts", "breeze-tts", site="h")  # stranded on another site, as it was live
        self.assertIn(("breeze-tts", "tts"), self.store.waiting_normal_shapes())
        self.assertTrue(worker._admit(self.store.get(self.job(MUSIC, background=True))))
        # A second worker is a site that has not already spent `_JUST_SENT_S` on the admission
        # above: `_background_room` discounts every send at the site, whatever its lane.
        image_worker = Worker(self.store, interval_s=10.0, pool_size=0)
        self.assertTrue(image_worker._admit(self.store.get(self.job(IMAGE, background=True))))

    def test_no_preemption_then_the_mac_takes_normal_work_next(self) -> None:
        self.sites = [_site("g", macs=1, idle={MUSIC: 1})]
        first = self.job(background=True)
        with _Running(self.store) as run:
            self.assertEqual(run.settle(), [first])
            self.sites = [_site("g", macs=1, idle={MUSIC: 0})]
            later_song = self.job(background=True)
            normal = self.job()
            # The song keeps running: nothing cancels or re-sends it, and the normal job waits.
            self.assertEqual(run.settle(), [first])
            self.assertEqual(self.store.get(first)["state"], "running")
            self.assertIsNone(self.store.get(first).get("cancel_requested_at"))
            # The song ends, the Mac is idle again: the normal job goes, not the queued song.
            self.sites = [_site("g", macs=1, idle={MUSIC: 1})]
            run.finish(first)
            self.assertEqual(run.settle(), [first, normal])
            self.assertEqual(self.store.get(later_song)["state"], "accepted")

    def test_two_background_clients_take_turns(self) -> None:
        self.sites = [_site("g", macs=4, idle={MUSIC: 4})]
        a = [self.job(background=True, group="channel-a/pool") for _ in range(6)]
        b = [self.job(background=True, group="channel-b/pool") for _ in range(6)]
        with _Running(self.store) as run:
            started = run.settle()
        self.assertEqual(sorted(started), sorted(a[:2] + b[:2]))

    def test_turns_count_the_songs_already_running(self) -> None:
        jobs = [{"id": f"a{i}", "priority": "background", "job_group": "a", "provider_id": "cloud"}
                for i in range(3)]
        jobs += [{"id": f"b{i}", "priority": "background", "job_group": "b", "provider_id": "cloud"}
                 for i in range(3)]
        jobs.append({"id": "n", "provider_id": "cloud"})
        order = [j["id"] for j in scarcest_first(jobs, {}, {"a": 2})]
        # a has two running, so b goes first until the turns are level; normal work ahead of both.
        self.assertEqual(order, ["n", "b0", "b1", "a0", "b2", "a1", "a2"])


class RequestFieldTest(_Base):
    def submit(self, payload: dict, key: str):
        self.sites = [_site("g", macs=2, idle={MUSIC: 2})]
        with patch.object(api, "store", self.store), \
                patch.object(api.catalog, "resolve", lambda *a, **k: (
                    types.SimpleNamespace(id=_pid("g")), {"model_id": MUSIC})):
            return api._submit("music", f"{fleet.FLEET_PROVIDER_ID}::{MUSIC}", payload, key)[0]

    def test_an_old_client_with_no_field_is_normal(self) -> None:
        job = self.submit({"prompt": "rain"}, "old")
        self.assertIsNone(job["priority"])
        self.assertEqual(api._job_envelope(job)["priority"], "normal")

    def test_background_is_stored_and_never_forwarded(self) -> None:
        job = self.submit({"prompt": "rain", "priority": "Background"}, "bg")
        self.assertEqual(job["priority"], "background")
        self.assertNotIn("priority", json.loads(job["params"]))
        self.assertEqual(api._job_envelope(job)["priority"], "background")
        self.assertEqual(self.submit({"priority": "normal"}, "n")["priority"], None)

    def test_an_unknown_value_is_refused_and_nothing_is_queued(self) -> None:
        for value in ("urgent", 1, ""):
            with self.assertRaises(GatewayError) as caught:
                self.submit({"priority": value}, f"bad-{value}")
            self.assertEqual(caught.exception.status, 400)
            self.assertIn("priority must be one of normal, background", caught.exception.detail)
        self.assertEqual(self.store.waiting_jobs(), [])

    def test_a_background_backlog_never_fills_the_queue_for_normal_work(self) -> None:
        with patch.object(api, "admission", lambda: {"max_queued_jobs": 2}):
            for i in range(3):
                self.submit({"priority": "background"}, f"song-{i}")
            with self.assertRaises(GatewayError) as caught:
                self.submit({"priority": "background"}, "song-over")
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(self.submit({}, "narration")["priority"], None)

    def test_a_background_job_queues_behind_every_normal_job_of_its_lane(self) -> None:
        song = self.submit({"priority": "background"}, "song")
        normal = self.submit({}, "later")
        self.assertEqual(self.store.queue_rank(normal["id"]), {"position": 1, "size": 2})
        self.assertEqual(self.store.queue_rank(song["id"]), {"position": 2, "size": 2})


class ModelFleetCapTest(_Base):
    """A fleet-wide cap per model (0.2.170, owner 2026-10-12: YuE2 sings, at most N at once)."""

    def cap(self, n) -> None:
        concurrency.SETTINGS_PATH.write_text(json.dumps({"global": 50, "models": {MUSIC: n}}), encoding="utf-8")
        concurrency.reset_for_tests()

    def test_no_model_is_capped_by_default_and_the_owner_can_set_or_lift_one(self) -> None:
        self.assertIsNone(concurrency.model_fleet_cap("vanch007/mlx-Yue2-3B"))
        self.assertIsNone(concurrency.model_fleet_cap(MUSIC))
        self.cap(3)
        self.assertEqual(concurrency.model_fleet_cap(MUSIC), 3)
        self.cap(0)
        self.assertIsNone(concurrency.model_fleet_cap(MUSIC))

    def test_at_most_the_cap_runs_across_every_site(self) -> None:
        # Two sites with four idle Macs each, ten songs: two run, whichever sites they landed on.
        self.cap(2)
        self.sites = [_site("g", macs=4, idle={MUSIC: 4}), _site("h", macs=4, idle={MUSIC: 4})]
        songs = [self.job(background=True, site="g" if i % 2 else "h") for i in range(10)]
        with _Running(self.store) as run:
            started = run.settle()
            self.assertEqual(len(started), 2)
            run.finish(started[0])
            self.assertEqual(len(run.settle()), 3)
            self.sites = [_site("g", macs=4, idle={MUSIC: 0}), _site("h", macs=4, idle={MUSIC: 0})]
        self.assertTrue(set(started) <= set(songs))

    def test_a_capped_model_keeps_the_callers_priority(self) -> None:
        # 0.2.171 (owner 2026-10-12: images are not the priority): the cap bounds YuE2, not the queue class.
        self.cap(2)
        self.assertIsNone(RequestFieldTest.submit(self, {"prompt": "rain"}, "sung")["priority"])
        self.assertEqual(RequestFieldTest.submit(self, {"prompt": "rain", "priority": "background"}, "bg")["priority"],
                         "background")

    def test_a_running_song_re_routing_does_not_count_against_itself(self) -> None:
        self.cap(1)
        self.sites = [_site("g", macs=2, idle={MUSIC: 2}), _site("h", macs=2, idle={MUSIC: 2})]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        song = self.store.get(self.job(background=True))
        self.assertTrue(worker._admit(song))
        other = self.store.get(self.job(background=True))
        self.assertLessEqual(worker._job_room(other, _pid("h")), 0)
        worker._running[song["id"]] = song
        self.assertGreater(worker._site_room(song, _pid("h"))[0], 0)

    def test_capacity_shows_only_the_room_under_the_cap(self) -> None:
        self.cap(2)
        self.sites = [_site("a", macs=11, idle={MUSIC: 9})]
        response = Response()
        with patch.object(api, "store", self.store):
            [row] = api.fleet_capacity(response, modality="music", model=MUSIC)["models"]
        self.assertEqual((row["idle_macs"], row["fleet_cap"], row["eligible_macs"]), (2, 2, 11))


class CapacityEndpointTest(_Base):
    def test_capacity_per_model_with_queued_counts_by_class(self) -> None:
        self.sites = [_site("a", macs=11, idle={MUSIC: 3}), _site("b", macs=5, idle={MUSIC: 0})]
        for _ in range(4):
            self.job(background=True, site="a")
        self.job(site="b")
        response = Response()
        with patch.object(api, "store", self.store):
            body = api.fleet_capacity(response, modality="music", model=f"{fleet.FLEET_PROVIDER_ID}::{MUSIC}")
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=5")
        [row] = body["models"]
        self.assertEqual((row["model_id"], row["locations"], row["eligible_macs"], row["idle_macs"]),
                         (MUSIC, 2, 16, 3))
        self.assertEqual(row["queued"], {"normal": 1, "background": 4})
        self.assertEqual(row["running"], {"normal": 0, "background": 0})
        self.assertIn("max_queued_jobs", body)

    def test_capacity_needs_the_api_key_and_refuses_an_unknown_lane(self) -> None:
        route = next(r for r in api.app.routes if getattr(r, "path", "") == "/v1/fleet/capacity")
        self.assertIn(api.require_auth, [d.dependency for d in route.dependencies])
        with self.assertRaises(GatewayError):
            api.fleet_capacity(Response(), modality="songs")


if __name__ == "__main__":
    unittest.main()
