"""The gateway never answers again: the 2026-10-19 lock-order inversion, and its fix.

**What happened.** At 08:14 on 2026-10-19 the gateway stopped answering *everything*,
`/health` included: a connection was accepted and no byte came back. A `sample` of the
process found 63 of 73 threads parked in `RLock.acquire`, and the log showed the fleet
fan-out's slowest controller taking 435 s.

**Why.** Two locks, taken in opposite orders.

  * The fleet fan-out holds `fleet._refresh_lock` for the whole sweep, and
    `fleet._attach_admission` reads this worker's own in-flight count from inside it
    (`_site_lane_in_flight`, which took `_budget_lock`).
  * `Worker._admit` held `_budget_lock` and reached `fleet.snapshot()` through
    `_job_room` → `_background_room`/`_room` (and, for a background job, the store).

A slow sweep therefore did not merely delay admission: it deadlocked it, and `/health`
took **both** locks (through `worker.active()` and `admission()` →
`concurrency.live_capacity()` → `fleet.snapshot`), so the probe that would have reported
the outage was the next thing to park.

**The rule these tests hold.** `_budget_lock` is a **leaf**: fleet and store reads happen
before it is taken (`Worker._admission_reads`), the counts `/health` publishes are read
without it, `fleet.snapshot(allow_stale=True)` never waits for a fan-out, and one client
cannot fill the process with requests it will never get an answer for.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

# Isolated before `gateway.api` is imported: `STUDIOHUB_READY_FILE` inside the temp dir is what
# stops an import-time fleet read from finding the owner's live Hub as this Mac's location.
_SANDBOX = sandbox_env("gateway-lock-inversion-")

from gateway import api  # noqa: E402
from gateway import concurrency  # noqa: E402
from gateway import constants  # noqa: E402
from gateway import fleet  # noqa: E402
from gateway.store import BACKGROUND  # noqa: E402
from gateway.worker import Worker  # noqa: E402

#: Long enough that a real deadlock is unambiguous, short enough that a hung suite still ends.
_DEADLOCK_TIMEOUT_S = 5.0


class _EmptyStore:
    """A store with nothing in it: these tests are about locks, not about jobs."""

    def due(self):
        return []

    def get(self, _job_id):
        return None


def _fleet_job(job_id: str = "job-1") -> dict:
    """A **background** job at a fleet location.

    Background is deliberate: it is the shape whose admission reads the fleet and the store from
    under `_budget_lock` (`_job_room` → `_background_room`), which is the inversion.
    """
    return {
        "id": job_id,
        "provider_id": fleet.PROVIDER_PREFIX + "site",
        "modality": "image",
        "model_id": "model",
        "priority": BACKGROUND,
        "state": "accepted",
    }


class _ColdFleetTest(unittest.TestCase):
    """A cold fleet cache, an empty roster, and no background fan-out to race the assertions.

    `gateway.api`'s import already reads the fleet once, so the cache is cleared per test rather
    than assumed empty; the roster is stubbed empty so no location is ever probed from a test,
    and the background revalidation is stubbed out because what is under test here is what a
    reader does **before** a fan-out, not what the fan-out does.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self._cache = dict(fleet._cache)
        fleet._cache.update({"at": 0.0, "value": None, "last": None})
        self.addCleanup(lambda: fleet._cache.update(self._cache))
        for name, replacement in (("locations", lambda: []),
                                  ("_revalidate_in_background", lambda: None)):
            patched = patch.object(fleet, name, replacement)
            patched.start()
            self.addCleanup(patched.stop)


class AdmissionNeverWaitsForAFanOutTest(_ColdFleetTest):
    """The inversion itself, in two threads, with the order it actually happened in.

    Thread A is the fan-out: it holds `fleet._refresh_lock` and — exactly as
    `_attach_admission` does — reads the worker's in-flight count while holding it.

    Thread B is admission: `_admit` for a **background** job at a fleet location, on a **cold**
    fleet cache. That is the shape that reaches the fleet from inside admission
    (`_background_room` → `fleet.snapshot` → `store.waiting_normal_shapes`), and a cold cache is
    the case that used to fall through to the serialised fan-out branch.

    Before the fix B held `_budget_lock` and waited for `_refresh_lock` while A held
    `_refresh_lock` and waited for `_budget_lock`; neither returned. `join(timeout)` is the
    assertion — a deadlock is a test that never finishes, so it is asserted as one.
    """

    def test_admission_finishes_while_a_fan_out_holds_the_refresh_lock(self) -> None:
        worker = Worker(_EmptyStore(), pool_size=8)
        job = _fleet_job()
        in_the_fan_out = threading.Event()
        release = threading.Event()
        admitted: list[bool] = []
        admitted_while_held: list[bool] = []

        def fan_out() -> None:
            with fleet._refresh_lock:
                in_the_fan_out.set()
                worker._site_lane_in_flight(job["provider_id"], "image")
                release.wait(_DEADLOCK_TIMEOUT_S * 2)

        def admit() -> None:
            self.assertTrue(in_the_fan_out.wait(_DEADLOCK_TIMEOUT_S))
            admitted.append(worker._admit(job))
            admitted_while_held.append(not release.is_set())

        self.addCleanup(release.set)
        fan = threading.Thread(target=fan_out, name="fan-out", daemon=True)
        adm = threading.Thread(target=admit, name="admit", daemon=True)
        fan.start()
        self.assertTrue(in_the_fan_out.wait(_DEADLOCK_TIMEOUT_S), "the fan-out never started")
        adm.start()
        adm.join(_DEADLOCK_TIMEOUT_S)
        self.assertFalse(
            adm.is_alive(),
            "admission never returned while a fan-out held the refresh lock — the 2026-10-19 "
            "inversion, which took every thread including /health's",
        )
        release.set()
        fan.join(_DEADLOCK_TIMEOUT_S)
        self.assertFalse(fan.is_alive())
        # What it answered is not the point — with an empty fleet, "no room" is the honest
        # answer — only that it answered without the fan-out.
        self.assertEqual(len(admitted), 1, "admission never returned a decision")
        self.assertEqual(
            admitted_while_held, [True],
            "admission finished only once the fan-out let go: it still depends on the fan-out",
        )

    def test_the_in_flight_reading_answers_while_the_budget_lock_is_held(self) -> None:
        """The other direction of the same inversion, on its own.

        `fleet._attach_admission` calls `_site_lane_in_flight` from inside the fan-out. If that
        took `_budget_lock`, a fan-out would queue behind whatever admission was doing — which is
        how a slow sweep turned into a deadlock rather than a delay.
        """
        worker = Worker(_EmptyStore(), pool_size=8)
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with worker._budget_lock:
                held.set()
                release.wait(_DEADLOCK_TIMEOUT_S * 2)

        self.addCleanup(release.set)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(_DEADLOCK_TIMEOUT_S), "the lock was never taken")

        answer: list[int] = []

        def read() -> None:
            answer.append(worker._site_lane_in_flight("studio-hub-site", "image"))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(_DEADLOCK_TIMEOUT_S)
        # Read before letting go: a blocked reader unblocks the moment the lock is released, so
        # asking afterwards would pass whatever the code did.
        blocked = reader.is_alive()
        release.set()
        holder.join(_DEADLOCK_TIMEOUT_S)
        self.assertFalse(blocked, "the fan-out's in-flight read waited on admission")
        self.assertEqual(answer, [0])

    def test_a_stale_read_is_served_rather_than_waiting_for_the_fan_out(self) -> None:
        """`allow_stale` says "must not block", and now means it.

        It fell through to the serialised branch on a cold cache and waited for the slowest
        controller — 435 s measured — while `_provider_budget`, `_model_supply`,
        `_background_room` and `live_capacity` all read the fleet this way. The lock is held by
        **another thread**: holding it here would prove nothing, because it is an `RLock` and the
        fan-out's own thread is meant to re-enter it.
        """
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with fleet._refresh_lock:
                held.set()
                release.wait(_DEADLOCK_TIMEOUT_S * 2)

        self.addCleanup(release.set)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(_DEADLOCK_TIMEOUT_S), "the refresh lock was never taken")

        answers: list[dict] = []
        started = time.monotonic()
        reader = threading.Thread(target=lambda: answers.append(fleet.snapshot(allow_stale=True)),
                                  daemon=True)
        reader.start()
        reader.join(_DEADLOCK_TIMEOUT_S)
        blocked = reader.is_alive()
        elapsed = time.monotonic() - started
        release.set()
        holder.join(_DEADLOCK_TIMEOUT_S)

        self.assertFalse(blocked, "a stale read waited for the fan-out")
        self.assertLess(elapsed, 1.0, "a stale read waited for the fan-out")
        self.assertEqual(answers[0]["locations"], [])
        # The shape is the fan-out's, so no reader has to special-case an empty fleet.
        # `last_known_machines` is a slow site's rows on the Fleet read only; an empty fleet has
        # none, and routing never sees it (see `fleet._carry_last_known_machines`).
        self.assertEqual(
            set(answers[0]),
            {"locations", "machines", "last_known_machines", "running", "summary", "issues",
             "generated_at"},
        )
        self.assertEqual(answers[0]["last_known_machines"], [])


class HealthNeverWaitsForAdmissionTest(_ColdFleetTest):
    """/health is the app's 2.5 s readiness probe, so it reads published numbers and nothing else."""

    def test_health_answers_while_the_budget_lock_is_held(self) -> None:
        worker = Worker(_EmptyStore(), pool_size=4)
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with worker._budget_lock:
                held.set()
                release.wait(_DEADLOCK_TIMEOUT_S * 2)

        self.addCleanup(release.set)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(_DEADLOCK_TIMEOUT_S), "the lock was never taken")

        answered: list[dict] = []
        failed: list[BaseException] = []

        def probe() -> None:
            try:
                with patch.object(api, "worker", worker):
                    answered.append(api.health())
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                failed.append(exc)

        prober = threading.Thread(target=probe, daemon=True)
        prober.start()
        prober.join(_DEADLOCK_TIMEOUT_S)
        # Both read before the lock is let go: a blocked probe unblocks the instant it is
        # released, so asking afterwards would pass whatever the code did.
        blocked = prober.is_alive()
        still_held = not release.is_set()
        release.set()
        holder.join(_DEADLOCK_TIMEOUT_S)

        self.assertEqual(failed, [])
        self.assertFalse(blocked, "/health waited on `_budget_lock`")
        self.assertTrue(still_held, "the lock was released early; the probe was not tested")
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["status"], "ok")
        # The two in-flight fields it publishes, both from the counter and neither from the lock.
        self.assertEqual(answered[0]["worker"]["in_flight"], 0)
        self.assertEqual(answered[0]["concurrency"]["in_flight"], 0)

    def test_the_published_counter_follows_the_books(self) -> None:
        """The number `/health` reads is the number the books hold, not a second tally."""
        worker = Worker(_EmptyStore(), pool_size=4)
        job = _fleet_job()
        self.assertEqual(worker.active(), 0)
        worker._book(job)
        self.assertEqual(worker.active(), 1)
        self.assertEqual(worker.active(), sum(worker._in_flight.values()))
        worker._release(job)
        self.assertEqual(worker.active(), 0)
        self.assertEqual(worker.active(), sum(worker._in_flight.values()))

    def test_admission_publishes_the_capacity_health_reads(self) -> None:
        """`admission()` takes the summary the fan-out published rather than walking the fleet."""
        published = {"total": 12, "by_modality": {"image": 4}, "locations": [],
                     "pool": {"limit": 7, "source": "environment", "detail": "GATEWAY_JOB_CONCURRENCY"},
                     "source": "the live fleet, summed over reachable locations"}
        concurrency.remember_capacity(published)
        self.addCleanup(lambda: concurrency._capacity_cache.update({"value": None}))
        record = constants.admission()
        self.assertEqual(record["paid_concurrency_limit"], 12)
        self.assertEqual(record["concurrency_by_modality"], {"image": 4})
        self.assertEqual(record["concurrency_pool"], 7)


def _request(path: str, client: str):
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": path, "root_path": "",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": (client, 51234),
    })


class OneClientCannotFillTheGatewayTest(unittest.TestCase):
    """The blast radius: a client that piles up work it cannot get an answer for is refused.

    `/health` is exempt and must stay exempt — it is the one route that has to answer while
    everything else is stuck, and a cap that could refuse it would turn a busy gateway into an
    app that says "the gateway is not running".
    """

    def setUp(self) -> None:
        self.addCleanup(api._CLIENT_IN_FLIGHT.clear)

    def test_a_client_past_the_cap_is_refused_and_health_is_not(self) -> None:
        from starlette.responses import Response

        limit = 2
        release = asyncio.Event()
        blocked: list[str] = []

        async def call_next(request):
            if len(blocked) < limit:
                blocked.append(request.url.path)
                await release.wait()
            return Response("ok")

        async def scenario():
            with patch.object(api, "_CLIENT_MAX_IN_FLIGHT", limit):
                first = asyncio.create_task(
                    api._cap_one_client(_request("/v1/jobs", "10.0.0.9"), call_next))
                second = asyncio.create_task(
                    api._cap_one_client(_request("/v1/jobs", "10.0.0.9"), call_next))
                while len(blocked) < limit:
                    await asyncio.sleep(0)
                over = await api._cap_one_client(_request("/v1/jobs", "10.0.0.9"), call_next)
                health = await api._cap_one_client(_request("/health", "10.0.0.9"), call_next)
                other = await api._cap_one_client(_request("/v1/jobs", "10.0.0.10"), call_next)
                release.set()
                await asyncio.gather(first, second)
                # The slot is given back, so a client that is answered is never refused.
                after = await api._cap_one_client(_request("/v1/jobs", "10.0.0.9"), call_next)
            return over, health, other, after

        over, health, other, after = asyncio.run(scenario())
        self.assertEqual(over.status_code, 429)
        self.assertEqual(over.headers["retry-after"], "1")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(other.status_code, 200, "a different client was refused")
        self.assertEqual(after.status_code, 200, "the slot was never given back")
        self.assertEqual(api._CLIENT_IN_FLIGHT, {}, "in-flight counts leaked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
