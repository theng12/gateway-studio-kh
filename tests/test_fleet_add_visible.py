#!/usr/bin/env python3
"""A location that is in the roster is on the Fleet page — at once, and through a slow read.

Owner, 2026-10-04 (Gateway 0.2.117), adding the Shops controller: no new row after "Added", then
"it takes a while for it to show". Live, each Terranash controller took 15-19 s to probe, so:

1. the poller's sweep, started before the add, finished after it and cached the old roster as
   fresh — the next read had no new location until the sweep after that;
2. a plain read just after the add (`invalidate` dropped the cache) waited for a whole fan-out,
   longer than the app's 15 s timeout, so the page showed an error instead of the list;
3. a controller that timed out lost its role and version, reading as an empty location.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()  # never bind to the owner's real Application Support

from gateway import fleet  # noqa: E402


def _row(rid: str, label: str, **extra) -> dict:
    out = fleet._unread_location(rid, label, f"http://10.0.0.{len(rid)}:47873", local=rid == "local")
    out.update(reachable=True, role="controller", version="0.6.141", **extra)
    return out


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(fleet, "FLEET_PATH", Path(tmp.name) / "fleet.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        fleet._write([{"id": "c1", "label": "Terranash", "url": "http://10.0.0.1:47873"}])
        with fleet._cache_lock:
            fleet._cache.update(at=0.0, value=None, last=None)
        self.addCleanup(fleet.invalidate)

    def _fan_out_from_roster(self, gate: threading.Event | None = None):
        """Stand-in for `locations()`: reads the roster first, then takes as long as the gate says."""
        def fan_out() -> list[dict]:
            rows = [_row("local", "This Mac")] + [_row(r["id"], r["label"]) for r in fleet._read()]
            if gate is not None:
                gate.wait(timeout=10)
            return rows
        return fan_out

    def _join_revalidation(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "fleet-revalidate":
                thread.join(timeout=10)

    @staticmethod
    def _ids(value: dict) -> list[str]:
        return [str(row.get("id")) for row in value["locations"]]


class AnAddDuringASweepTest(_Case):
    def test_a_sweep_that_read_the_old_roster_does_not_cache_it(self) -> None:
        gate = threading.Event()
        with patch.object(fleet, "locations", self._fan_out_from_roster(gate)):
            sweep = threading.Thread(target=fleet.snapshot, kwargs={"force": True})
            sweep.start()
            time.sleep(0.2)  # the sweep has read the roster and is waiting on the slow controller
            fleet._write(fleet._read() + [{"id": "shops", "label": "Shops", "url": "http://10.0.0.9:47873"}])
            fleet.invalidate()
            gate.set()
            sweep.join(timeout=10)
        self.assertIsNone(fleet._cache["value"], "a pre-add sweep must not be cached as fresh")
        gate2 = threading.Event()
        with patch.object(fleet, "locations", self._fan_out_from_roster(gate2)):
            shown = fleet.snapshot()
            gate2.set()
            self._join_revalidation()
        self.assertIn("shops", self._ids(shown), "the page's next read must carry the new location")


class APlainReadAfterAnAddTest(_Case):
    def test_it_does_not_wait_and_shows_the_new_location_pending(self) -> None:
        with patch.object(fleet, "locations", self._fan_out_from_roster()):
            fleet.snapshot(force=True)
        fleet._write([{"id": "c1", "label": "Terranash 1100", "url": "http://10.0.0.1:47873"},
                      {"id": "shops", "label": "Shops", "url": "http://10.0.0.9:47873"}])
        fleet.invalidate()

        gate = threading.Event()
        answer: list[dict] = []
        with patch.object(fleet, "locations", self._fan_out_from_roster(gate)):
            reader = threading.Thread(target=lambda: answer.append(fleet.snapshot()))
            reader.start()
            reader.join(timeout=5)
            waited = reader.is_alive()
            gate.set()
            reader.join(timeout=10)
            self._join_revalidation()
        self.assertFalse(waited, "a plain read after a roster change must not wait for the fan-out")
        rows = {row["id"]: row for row in answer[0]["locations"]}
        self.assertEqual(["local", "c1", "shops"], self._ids(answer[0]))
        self.assertTrue(rows["shops"]["pending"])
        self.assertEqual(fleet.provider_id_for("shops"), rows["shops"]["provider_id"])
        self.assertEqual("Terranash 1100", rows["c1"]["label"], "a rename shows on the next read")
        # The fan-out it scheduled lands, and the location is then read for real.
        self.assertNotIn("pending", next(r for r in fleet._cache["value"]["locations"] if r["id"] == "shops"))

    def test_a_removed_location_is_gone_from_that_read(self) -> None:
        with patch.object(fleet, "locations", self._fan_out_from_roster()):
            fleet.snapshot(force=True)
        fleet._write([])
        fleet.invalidate()
        with patch.object(fleet, "_revalidate_in_background"):
            self.assertEqual(["local"], self._ids(fleet.snapshot()))


class ASlowLocationTest(_Case):
    def test_a_timed_out_location_keeps_what_it_last_reported(self) -> None:
        with patch.object(fleet, "locations", self._fan_out_from_roster()):
            fleet.snapshot(force=True)
        slow = fleet._unread_location("c1", "Terranash", "http://10.0.0.1:47873")
        slow.update(reachable=False, slow=True, error="No answer within 6s.")
        with patch.object(fleet, "locations", lambda: [_row("local", "This Mac"), slow]):
            value = fleet.snapshot(force=True)
        row = next(r for r in value["locations"] if r["id"] == "c1")
        self.assertFalse(row["reachable"], "a slow location is never routed to as if it answered")
        self.assertEqual("0.6.141", row["version"])
        self.assertEqual("controller", row["role"])
        self.assertIn("Slow to answer", row["error"])
        self.assertIn("didn't answer within 6 s", row["error"])
        self.assertIn("last report", row["error"])


class TheAddAnswerTest(_Case):
    def test_the_add_answers_with_the_row_the_page_inserts(self) -> None:
        from gateway import admin

        class _Found:
            models: list = []
            error = None

        with patch.object(admin.registry, "refresh", return_value=_Found()), \
                patch.object(fleet, "credential_scope_for", return_value={"scope": "fleet", "detail": ""}), \
                patch.object(fleet, "seed_enabled_from_local", return_value={"sentence": ""}):
            out = admin.fleet_add_controller({"label": "Shops", "url": "http://100.120.246.9:47873", "token": ""})
        location = out["location"]
        self.assertTrue(location["pending"])
        self.assertEqual("Shops", location["label"])
        self.assertEqual(out["controller"]["id"], location["id"])
        self.assertEqual(fleet.provider_id_for(location["id"]), location["provider_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
