#!/usr/bin/env python3
"""A site that does not answer keeps its Macs in the total — counted as **not checked**.

Measured on the live Fleet, 2026-11-18 21:45: 1100, 1200 and Shops timed out in one fan-out and
the headline fell from 29 online to 8, with nothing on the page saying that ~20 Macs had not been
looked at. The cause was a 6 s probe bound catching busy controllers; the defect is that silence
was presented as a smaller fleet.

What this file pins:

1. **`machines_not_checked` is its own count** — every Mac at a site that did not answer, from the
   roster that site last published. It is never folded into online, offline or unknown, and the
   other counts do not move when a site goes silent.
2. **The total does not shrink**: `machines + machines_not_checked` equals the total the same
   read knew when every site answered.
3. **A site that answers slowly is measured, not "not checked".** The bound is 10 s by default and
   follows the site's own last duration (3×, capped at 20 s), so 5.9 s and 6.1 s answers both
   count; only a probe that exceeds its bound becomes "not checked".
4. **An empty snapshot carries the keys at zero**, so a reader never has to guess.

The fixture is the live shape from `_orchestra/gateway-lead/fleet-stall-20261118/fleet_stall_repro2.py`
(three silent sites, 16 Macs), reduced to the machines each site reports.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_not_checked
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-fleet-not-checked-")

from gateway import fleet, fleet_history  # noqa: E402


def _machine(mid: str) -> dict:
    return {"machine": mid, "label": mid, "reachable": True, "state": "working",
            "chip": "Apple M4", "memory_gb": 32, "available_gb": 18.0,
            "disk_free_gb": 61.0, "disk_total_gb": 460.0}


def _location(rid: str, label: str, count: int) -> dict:
    ids = [f"{rid}-{i}" for i in range(count)]
    row = fleet._unread_location(rid, label, f"http://10.0.0.{len(rid)}:47873")
    row.update(
        reachable=True, role="controller", version="0.6.247",
        machines=[_machine(mid) for mid in ids],
        machine_summary={"total": count, "reachable": count},
        nodes=[{"id": mid, "state": "working"} for mid in ids],
        node_summary={"total": count, "reachable": count, "unauthorized": 0},
        latency_ms=400,
        activity={"pulse": {"working": count},
                  "machines": [{"machine": mid, "state": "working",
                                "completed": 0, "failed": 0} for mid in ids],
                  "observed_at": time.time()},
    )
    return row


def _silent(rid: str, label: str, roster: int, *, error_kind: str = "timeout") -> dict:
    """What `locations()` publishes for a site that did not answer, with its last roster."""
    row = fleet._unread_location(rid, label, f"http://10.0.0.{len(rid)}:47873")
    row.update(
        reachable=False, slow=error_kind == "timeout", error_kind=error_kind,
        error="No answer within 10s. The address is reachable but nothing replied.",
        nodes=[{"id": f"{rid}-{i}", "state": "working"} for i in range(roster)],
        node_summary={"total": roster, "reachable": roster, "unauthorized": 0},
        members_from=time.time() - 600,
    )
    return row


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in (("FLEET_PATH", root / "fleet.json"),
                            ("HISTORY_PATH", root / "fleet_history.json"),
                            ("MEMBERS_PATH", root / "fleet_members.json")):
            patcher = patch.object(fleet if name in ("FLEET_PATH", "MEMBERS_PATH") else fleet_history, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fleet_history.reset_for_tests()
        self.addCleanup(fleet_history.reset_for_tests)
        fleet.reset_members_for_tests()
        self.addCleanup(fleet.reset_members_for_tests)
        fleet._write([])
        with fleet._cache_lock:
            fleet._cache.update(at=0.0, value=None, last=None, gen=0)
        self.addCleanup(fleet.invalidate)

    def _read(self, rows: list[dict]) -> dict:
        with patch.object(fleet, "locations", lambda: [dict(r) for r in rows]):
            return fleet.snapshot(force=True)

    def _restart(self) -> None:
        """What a process start looks like: the in-memory rosters and the cache are gone, the
        file on disk is not."""
        fleet.reset_members_for_tests()
        fleet.invalidate()
        with fleet._cache_lock:
            fleet._cache.update(at=0.0, value=None, last=None, gen=0)


class TheSilentSitesMacsAreNotLostTest(_Case):
    def _answering(self) -> list[dict]:
        return [_location("l1100", "1100", 11), _location("l1200", "1200", 11),
                _location("l1400", "1400", 8), _location("local", "This Mac", 1)]

    def _warm(self) -> dict:
        """Every site answered once: 47 Macs, and their rows are what a later silence carries."""
        return self._read(self._answering() + [_location("lshops", "Shops", 9),
                                               _location("l1500", "1500", 5),
                                               _location("l1600", "1600", 2)])

    def _during(self) -> dict:
        """The live shape at 21:45: 1100 and 1200 answered; Shops, 1500 and 1600 did not."""
        return self._read(self._answering() + [_silent("lshops", "Shops", 9),
                                               _silent("l1500", "1500", 5),
                                               _silent("l1600", "1600", 2)])

    def test_they_are_counted_apart_and_the_other_counts_do_not_move(self) -> None:
        self.assertEqual(self._warm()["summary"]["machines_not_checked"], 0)
        bad = self._during()
        self.assertEqual(bad["summary"]["machines"], 31,
                         "the answering sites still count exactly as before")
        self.assertEqual(bad["summary"]["machines_online"], 31,
                         "silence is never folded into online")
        self.assertEqual(bad["summary"]["machines_offline"], 0)
        self.assertEqual(bad["summary"]["machines_unknown"], 0)
        self.assertEqual(bad["summary"]["machines_not_checked"], 16)
        self.assertEqual(bad["summary"]["locations_not_answering"], 3)
        self.assertEqual(len(bad["last_known_machines"]), 16,
                         "the carried rows and the count are the same Macs")

    def test_the_total_never_shrinks(self) -> None:
        good = self._warm()
        bad = self._during()
        before = good["summary"]["machines"] + good["summary"]["machines_not_checked"]
        after = bad["summary"]["machines"] + bad["summary"]["machines_not_checked"]
        self.assertEqual(before, 47)
        self.assertEqual(after, before)

    def test_a_refused_site_counts_its_roster_too(self) -> None:
        bad = self._read(self._answering() + [_silent("l1500", "1500", 4, error_kind="refused")])
        self.assertEqual(bad["summary"]["machines_not_checked"], 4)

    def test_an_empty_snapshot_publishes_the_keys_at_zero(self) -> None:
        empty = fleet._empty_snapshot()["summary"]
        self.assertEqual(empty["machines_not_checked"], 0)
        self.assertEqual(empty["locations_not_answering"], 0)


class TheSlowSiteIsMeasuredTest(unittest.TestCase):
    def setUp(self) -> None:
        fleet._probe_durations.clear()
        self.addCleanup(fleet._probe_durations.clear)

    def test_the_bound_is_ten_seconds_for_a_site_with_no_history(self) -> None:
        self.assertEqual(fleet._probe_timeout("never-seen"), 10.0)
        self.assertEqual(fleet.PROBE_TIMEOUT_S, 10.0)
        self.assertEqual(fleet.PROBE_TIMEOUT_MAX_S, 20.0)

    def test_a_previous_slow_probe_raises_that_sites_bound(self) -> None:
        # 5.9 s and 6.1 s are both inside their own 3x bound (17.7 s and 18.3 s): an answer at
        # either is measured, not "not checked".
        fleet._probe_durations["slow-59"] = 5.9
        fleet._probe_durations["slow-61"] = 6.1
        self.assertAlmostEqual(fleet._probe_timeout("slow-59"), 17.7, places=3)
        self.assertAlmostEqual(fleet._probe_timeout("slow-61"), 18.3, places=3)

    def test_the_bound_has_a_floor_and_a_ceiling(self) -> None:
        fleet._probe_durations["fast"] = 0.2
        fleet._probe_durations["very-slow"] = 9.0
        self.assertEqual(fleet._probe_timeout("fast"), 10.0)
        self.assertEqual(fleet._probe_timeout("very-slow"), 20.0)


if __name__ == "__main__":
    unittest.main()


class TheRosterSurvivesARestartTest(_Case):
    """Proven the way the Developer asked: a restart with a site unreachable, not by reading code.

    Every site answers (46 Macs across 1100, 1200, 1300, 1400, 1500, 1600, 1700 and This Mac).
    The process then restarts (in-memory rosters and cache cleared, the persisted file kept) and
    one site — 1700, four Macs — is unreachable. The fleet still totals 46: 42 measured plus 4
    not checked, and 1700 says when it was last heard from.
    """

    def _sites(self) -> list[dict]:
        return [_location("l1100", "1100", 11), _location("l1200", "1200", 11),
                _location("l1300", "1300", 4), _location("l1400", "1400", 8),
                _location("l1500", "1500", 5), _location("l1600", "1600", 2),
                _location("l1700", "1700", 4), _location("local", "This Mac", 1)]

    def _after_restart_row(self, controller_id: str, label: str) -> dict:
        """The row `locations()` makes for an unreachable site: its persisted roster attached."""
        known = fleet._last_known_members(controller_id)
        row = _silent(controller_id, label, 0)
        row.update(nodes=known.get("nodes") or [], node_summary=known.get("node_summary") or {},
                   members_from=known.get("members_from"),
                   members_stale_seconds=known.get("members_stale_seconds"))
        return row

    def _answer_all(self) -> None:
        """What a live fan-out does on its way past each answering controller."""
        for row in self._sites():
            fleet._remember_members(row["id"], row["nodes"], row["node_summary"])

    def test_the_file_holds_the_roster_and_when_it_answered(self) -> None:
        before = time.time()
        self._read(self._sites())
        self._answer_all()
        raw = json.loads(fleet.MEMBERS_PATH.read_text(encoding="utf-8"))
        entry = raw["controllers"]["l1700"]
        self.assertEqual(len(entry["nodes"]), 4)
        self.assertGreaterEqual(float(entry["at"]), before)
        self.assertEqual(entry["summary"]["total"], 4)

    def test_after_a_restart_the_unreachable_sites_macs_are_still_in_the_total(self) -> None:
        warm = self._read(self._sites())
        self._answer_all()
        self.assertEqual(warm["summary"]["machines"], 46)
        self._restart()

        rows = [row for row in self._sites() if row["id"] != "l1700"]
        rows.append(self._after_restart_row("l1700", "1700"))
        after = self._read(rows)

        self.assertEqual(after["summary"]["machines_not_checked"], 4,
                         "1700's four Macs are not checked, not gone")
        self.assertEqual(after["summary"]["sites_roster_unknown"], 0,
                         "the roster was read back from disk, so the count is known")
        self.assertEqual(after["summary"]["machines"] + after["summary"]["machines_not_checked"], 46,
                         "the fleet total does not shrink across the restart")
        site = next(row for row in after["locations"] if row["id"] == "l1700")
        self.assertGreater(site["members_from"], 0)
        self.assertGreater(site["members_stale_seconds"], 0)

    def test_a_site_never_reached_says_its_mac_count_is_unknown(self) -> None:
        self._restart()
        rows = [_location("l1400", "1400", 8), _location("local", "This Mac", 1),
                _silent("l9999", "9999", 0)]
        after = self._read(rows)
        self.assertEqual(after["summary"]["sites_roster_unknown"], 1)
        self.assertEqual(after["summary"]["machines_not_checked"], 0)
