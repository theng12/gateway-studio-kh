#!/usr/bin/env python3
"""The Fleet page's read path is a cache read, not a fan-out.

Run with::

    .venv/bin/python tests/test_fleet_snapshot_freshness.py

**The bug this pins.** `_CACHE_TTL` was 5 s while `start_refresher` runs every
15 s, so the cached snapshot was stale two thirds of the time — and the request
path, which is supposed to *"only ever read a value that is already there"* (the
refresher's own docstring), did the fan-out itself instead. Measured on the
owner's six-location fleet: **22.7 s** cold, against the app's **15 s** request
timeout. So the Fleet page loaded only when the fleet was idle — which is exactly
when nobody is looking at it. The owner, twice: *"this isn't showing. but when
there's no job running. it did show."*

Three properties, each of which alone would have prevented it:

1. **A read that did not ask to be fresh never waits.** Stale is served, and the
   refresh happens behind it.
2. **Concurrent readers do not multiply the fan-out.** Ten readers arriving on a
   stale cache used to mean ten sweeps of every controller.
3. **The refresh path is re-entrant, and must stay so.** A refresh reaches
   `_attach_admission`, which lazily imports `runtime`; that import builds the
   `Catalog`, whose `rebuild()` calls `usable_location_ids()` and therefore calls
   back into `snapshot()`. A plain `Lock` deadlocked the whole read on the first
   refresh after boot. This is checked directly, because a deadlock is invisible
   until someone waits forever for a page.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class FleetSnapshotFreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls._env = patch.dict(
            os.environ,
            {
                "GATEWAY_DATA": str(tmp / "data"),
                "GATEWAY_APP_SUPPORT": str(tmp / "app-support"),
                "GATEWAY_ENV_FILE": str(tmp / ".env"),
                "GATEWAY_FLEET": str(tmp / "data" / "fleet.json"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(tmp / "hub-ready.json"),
                "GATEWAY_HOST": "127.0.0.1",
            },
        )
        cls._env.start()
        sys.path.insert(0, str(ROOT))

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        cls._tmp.cleanup()

    def setUp(self):
        from gateway import fleet

        self.fleet = fleet
        self.calls = 0
        self.calls_lock = threading.Lock()

    def _fake_locations(self, delay: float = 0.0):
        """Stand in for the fan-out, counting how often it actually happens."""
        def fake():
            with self.calls_lock:
                self.calls += 1
            if delay:
                time.sleep(delay)
            return []
        return fake

    def _reset_cache(self):
        with self.fleet._cache_lock:
            self.fleet._cache["at"] = 0.0
            self.fleet._cache["value"] = None

    # ── 1. a plain read is a cache read ───────────────────────────────
    def test_a_plain_read_serves_a_stale_cache_without_fanning_out(self):
        self._reset_cache()
        with patch.object(self.fleet, "locations", self._fake_locations()):
            first = self.fleet.snapshot(force=True)   # warms the cache
            self.assertEqual(1, self.calls)

            # Age it well past the TTL. The old code would now fan out and make
            # the caller wait for every controller.
            with self.fleet._cache_lock:
                self.fleet._cache["at"] = time.time() - (self.fleet._CACHE_TTL * 10)

            fresh = self.fleet.snapshot()
            self.assertIs(first, fresh, "a stale read must still be served the cache")

    def test_the_read_does_not_wait_even_when_the_fan_out_is_slow(self):
        # The shape of the real failure: a busy Hub makes the fan-out take
        # seconds, and the reader that triggered it is the one that times out.
        # Deterministic: the fan-out is held shut, so a read that waited on it would never return.
        self._reset_cache()
        with patch.object(self.fleet, "locations", self._fake_locations(delay=0.0)):
            self.fleet.snapshot(force=True)
        gate = threading.Event()

        def stuck_fan_out():
            gate.wait(timeout=20)
            return []

        with patch.object(self.fleet, "locations", stuck_fan_out):
            with self.fleet._cache_lock:
                self.fleet._cache["at"] = time.time() - (self.fleet._CACHE_TTL * 10)
            reader = threading.Thread(target=self.fleet.snapshot)
            reader.start()
            reader.join(timeout=10)
            returned = not reader.is_alive()
            gate.set()
            reader.join(timeout=10)
            for t in threading.enumerate():
                if t.name == "fleet-revalidate":
                    t.join(timeout=10)
        self.assertTrue(returned, "a plain read must not wait on the fan-out it just scheduled")

    def test_force_does_fan_out(self):
        # The Refresh button is the one path allowed to wait, so it must actually
        # refresh -- a Refresh that only re-reads the cache is a control that
        # does nothing.
        self._reset_cache()
        with patch.object(self.fleet, "locations", self._fake_locations()):
            self.fleet.snapshot(force=True)
            self.fleet.snapshot(force=True)
            self.assertEqual(2, self.calls)

    # ── 2. no stampede ────────────────────────────────────────────────
    def test_concurrent_readers_share_one_fan_out(self):
        self._reset_cache()
        with patch.object(self.fleet, "locations", self._fake_locations(delay=0.3)):
            threads = [
                threading.Thread(target=self.fleet.snapshot, kwargs={"force": True})
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
        self.assertLessEqual(
            self.calls, 2,
            f"8 readers must not each sweep the fleet — {self.calls} fan-outs",
        )

    def test_a_stale_read_only_schedules_one_revalidation(self):
        # Deterministic: the fan-out is held on an Event while all ten reads happen, so no wall-clock
        # sleep can let one refresh finish (and the cache go fresh, or the flag clear) between reads.
        self._reset_cache()
        gate = threading.Event()
        started = threading.Event()
        hold = threading.Event()  # set once the stale cache is armed; the warm-up call must not block

        def fan_out():
            with self.calls_lock:
                self.calls += 1
            if hold.is_set():
                started.set()
                gate.wait(timeout=10)
            return []

        with patch.object(self.fleet, "locations", fan_out):
            self.fleet.snapshot(force=True)
            with self.fleet._cache_lock:
                self.fleet._cache["at"] = time.time() - (self.fleet._CACHE_TTL * 10)
            before = self.calls
            hold.set()
            for _ in range(10):
                self.fleet.snapshot()
            self.assertTrue(started.wait(timeout=10), "the first stale read must start a refresh")
            gate.set()
            for t in threading.enumerate():
                if t.name == "fleet-revalidate":
                    t.join(timeout=10)
        self.assertEqual(
            self.calls - before, 1,
            f"ten stale reads must schedule exactly one refresh — {self.calls - before} ran",
        )

    # ── 3. the refresh path stays re-entrant ──────────────────────────
    def test_a_reentrant_read_during_a_refresh_does_not_deadlock(self):
        # This is the deadlock that a plain `Lock` caused, reproduced without
        # needing `runtime`: a refresh whose fan-out calls back into `snapshot()`,
        # which is exactly what `_attach_admission` -> `runtime` -> `Catalog` ->
        # `usable_location_ids` does on the first refresh after boot.
        self._reset_cache()
        done = threading.Event()

        # **Once**, which is what the real chain does: the lazy `runtime` import
        # inside `_attach_admission` completes on the nested call, so the second
        # `snapshot()` is the last one. A fake that recursed forever would be
        # testing unbounded recursion, not the deadlock.
        self.reentered = False

        def reentrant_fan_out():
            if not self.reentered:
                self.reentered = True
                self.fleet.snapshot(allow_stale=True)
            return []

        with patch.object(self.fleet, "locations", reentrant_fan_out):
            def run():
                self.fleet.snapshot(force=True)
                done.set()

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(
                done.wait(timeout=10),
                "a refresh that reads the snapshot again deadlocked — the lock must be re-entrant",
            )

    def test_allow_stale_never_refreshes(self):
        # The catalogue and the worker read on this path; neither may ever block
        # on the fleet, which is the guarantee they were written against.
        self._reset_cache()
        with patch.object(self.fleet, "locations", self._fake_locations()):
            self.fleet.snapshot(force=True)
            before = self.calls
            for _ in range(5):
                self.fleet.snapshot(allow_stale=True)
            self.assertEqual(before, self.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
