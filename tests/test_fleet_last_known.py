#!/usr/bin/env python3
"""A slow site's Macs stay on the owner's Fleet list — marked as old, never as live capacity.

Measured on the live fleet, 2026-10-30: `GET /admin/api/fleet` returned 36, 46, 48, 53, 57, 63 and
68 machines across reads seconds apart. A location that missed the 6 s `_TIMEOUT` got "Slow to
answer … last report 12:31" and its machines simply vanished, so the owner — walking between sites
fixing Macs — could not tell "this Mac is gone" from "its site was slow".

What this file pins:

1. **The rows are kept apart.** They are `value["last_known_machines"]`, never
   `value["machines"]`, so routing, admission, capacity, orphan detection and the reachability
   history see exactly what they saw before: no capacity from a row nobody measured.
2. **Every row is marked and honest.** `last_known`, `as_of`, `state_text` ("Last report HH:MM"),
   `last_known_state`; `reachable` is `None` — never online, never offline.
3. **Only a `slow` site** (timed out after answering) is carried. A refused or rejected one keeps
   its own reason; a site never seen up carries nothing.
4. **Nothing older than an hour** is carried; the location row still says when it last reported.
5. **The Fleet route adds them only to its own copies**, after the history is annotated, so
   `fleet_history.annotate` has no row to record a verdict for, and its summary reports how many
   were carried rather than counting them as online.
6. **The next read after recovery is fresh**, with no marks.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_last_known
"""
from __future__ import annotations

import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-fleet-last-known-")

from gateway import admin, fleet, fleet_history  # noqa: E402


def _machine(mid: str, *, state: str = "ready") -> dict:
    return {"machine": mid, "label": mid, "reachable": True, "state": state,
            "chip": "Apple M4", "memory_gb": 32, "available_gb": 18.0,
            "disk_free_gb": 61.0, "disk_total_gb": 460.0}


def _location(rid: str, label: str, machines=(), *, reachable: bool = True, **extra) -> dict:
    """A location row shaped like `_unread_location`, with the machines a probe would publish."""
    row = fleet._unread_location(rid, label, f"http://10.0.0.{len(rid)}:47873")
    row.update(
        reachable=reachable,
        role="controller",
        version="0.6.213",
        machines=[dict(machine) for machine in machines],
        machine_summary={"total": len(machines),
                         "reachable": sum(1 for machine in machines if machine.get("reachable"))},
        activity={
            "pulse": {"ready": len(machines)},
            "machines": [{"machine": machine["machine"],
                          "state": machine.get("state", "ready")} for machine in machines],
            "observed_at": time.time(),
        },
        **extra,
    )
    return row


def _slow(rid: str, label: str) -> dict:
    """What `_probe` returns when the controller misses the 6 s budget."""
    row = fleet._unread_location(rid, label, f"http://10.0.0.{len(rid)}:47873")
    row.update(reachable=False, slow=True, error_kind="timeout",
               error="No answer within 6s. The address is reachable but nothing replied.")
    return row


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in (
            ("FLEET_PATH", root / "fleet.json"),
            ("HISTORY_PATH", root / "fleet_history.json"),
        ):
            patcher = patch.object(fleet if name == "FLEET_PATH" else fleet_history, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fleet_history.reset_for_tests()
        self.addCleanup(fleet_history.reset_for_tests)
        fleet._write([{"id": "c1", "label": "Terranash", "url": "http://10.0.0.1:47873"}])
        with fleet._cache_lock:
            fleet._cache.update(at=0.0, value=None, last=None, gen=0)
        self.addCleanup(fleet.invalidate)

    # ── helpers ───────────────────────────────────────────────────────
    def _read(self, rows: list[dict]) -> dict:
        with patch.object(fleet, "locations", lambda: list(rows)):
            return fleet.snapshot(force=True)

    def _warm(self) -> dict:
        """One good read: two Macs at c1, measured."""
        return self._read([_location("c1", "Terranash", [_machine("m1"), _machine("m2")])])

    def _by_location(self, value: dict, location_id: str) -> list[dict]:
        return [row for row in value.get("machines") or [] if row.get("location_id") == location_id]


class ASlowSiteKeepsItsMachinesTest(_Case):
    def test_the_rows_are_kept_apart_from_machines_and_marked_as_old(self) -> None:
        warm = self._warm()
        answered_at = next(r for r in warm["locations"] if r["id"] == "c1")["answered_at"]
        self.assertEqual(len(self._by_location(warm, "c1")), 2)

        value = self._read([_slow("c1", "Terranash")])

        # **Not capacity.** `value["machines"]` is what routing, admission, capacity and the
        # history read, and it is exactly what it was before this change: nothing for a site
        # that did not answer.
        self.assertEqual(self._by_location(value, "c1"), [])
        self.assertEqual(value["summary"]["machines"], 0)
        self.assertEqual(value["summary"]["machines_online"], 0)

        carried = value["last_known_machines"]
        self.assertEqual([row["machine"] for row in carried], ["m1", "m2"])
        for row in carried:
            self.assertIs(row["last_known"], True)
            self.assertAlmostEqual(row["as_of"], answered_at, places=3)
            self.assertIsNone(row["reachable"], "unknown now: never online, never offline")
            self.assertEqual(row["state"], "last_known")
            self.assertEqual(row["last_known_state"], "ready")
            self.assertRegex(row["state_text"], r"^Last report \d{2}:\d{2}$")
            self.assertEqual(row["location_id"], "c1")

        # The location row still carries what it last reported and why, in its own sentence.
        row = next(r for r in value["locations"] if r["id"] == "c1")
        self.assertFalse(row["reachable"])
        self.assertIn("Slow to answer", row["error"])
        self.assertIn("last report", row["error"])

    def test_routing_and_history_see_no_carried_row(self) -> None:
        self._warm()
        value = self._read([_slow("c1", "Terranash")])
        self.assertEqual(len(value["last_known_machines"]), 2)

        # The snapshot the router reads has no machine there.
        cached = fleet.snapshot(allow_stale=True)
        self.assertEqual(self._by_location(cached, "c1"), [])
        # **The catalogue keeps the site's models on offer through the grace** (0.2.216): one
        # missed 6 s read is not an outage, and a site that answered seconds ago is not gone.
        # Routing and admission still read `reachable`, so no work is sent there while it is
        # silent — only the picker stops flickering.
        self.assertEqual(fleet.usable_location_ids(), {"c1"})

        # **Not observed as a drop.** The Macs were recorded up on the warm read; a carried row
        # must not make that look like going offline.
        history = fleet_history.summary("m:c1/m1")
        self.assertTrue(history["observed"], "the warm read was a real observation")
        self.assertEqual(history["drops_day"], 0)
        self.assertEqual(history["flips_day"], 0)
        self.assertIsNone(history["last_drop"])

    def test_a_second_slow_read_keeps_carrying_the_same_rows(self) -> None:
        warm = self._warm()
        as_of = next(r for r in warm["locations"] if r["id"] == "c1")["answered_at"]
        self._read([_slow("c1", "Terranash")])
        value = self._read([_slow("c1", "Terranash")])
        carried = value["last_known_machines"]
        self.assertEqual([row["machine"] for row in carried], ["m1", "m2"])
        self.assertAlmostEqual(carried[0]["as_of"], as_of, places=3)

    def test_the_read_after_recovery_is_fresh_with_no_marks(self) -> None:
        self._warm()
        self._read([_slow("c1", "Terranash")])
        value = self._read([_location("c1", "Terranash", [_machine("m1"), _machine("m2")])])

        self.assertEqual(value["last_known_machines"], [])
        rows = self._by_location(value, "c1")
        self.assertEqual([row["machine"] for row in rows], ["m1", "m2"])
        for row in rows:
            self.assertNotIn("last_known", row)
            self.assertNotIn("as_of", row)
        self.assertEqual(value["summary"]["machines_online"], 2)


class OnlyASlowSiteIsCarriedTest(_Case):
    def test_a_refused_location_is_not_carried_and_keeps_its_own_reason(self) -> None:
        self._warm()
        refused = fleet._unread_location("c1", "Terranash", "http://10.0.0.1:47873")
        refused.update(reachable=False, error_kind="refused",
                       error="Nothing is listening on that address.")
        value = self._read([refused])
        self.assertEqual(value["last_known_machines"], [])
        row = next(r for r in value["locations"] if r["id"] == "c1")
        self.assertEqual(row["error"], "Nothing is listening on that address.")

    def test_a_site_never_seen_up_carries_nothing(self) -> None:
        value = self._read([_slow("c1", "Terranash")])
        self.assertEqual(value["last_known_machines"], [],
                         "no previous answer is not an answer to invent")

    def test_rows_older_than_an_hour_are_not_carried(self) -> None:
        warm = self._warm()
        with fleet._cache_lock:
            cached = fleet._cache["value"]
        # Age the last answer past the cap, leaving the site slow now.
        for row in cached["locations"]:
            if row["id"] == "c1":
                row["answered_at"] = time.time() - (fleet.LAST_KNOWN_MAX_AGE_S + 100)
        value = self._read([_slow("c1", "Terranash")])
        self.assertEqual(value["last_known_machines"], [])
        row = next(r for r in value["locations"] if r["id"] == "c1")
        self.assertIn("last report", row["error"],
                      "the location row still says when it last reported")
        self.assertEqual(len(self._by_location(warm, "c1")), 2)


class TheFleetRouteCarriesThemOnlyOnItsCopiesTest(_Case):
    def _route(self, value: dict) -> dict:
        with patch.object(fleet, "snapshot", lambda **_: value), \
                patch.object(admin.release_rollout, "status", lambda *a, **k: {}), \
                patch.object(admin.model_share, "stamp_networks", lambda *a, **k: None), \
                patch.object(admin.store, "stats", lambda: {"by_state": {}, "background": 0}):
            return admin.fleet_snapshot()

    def test_the_page_gets_the_rows_the_summary_says_how_many_and_the_cache_does_not(self) -> None:
        self._warm()
        value = self._read([_slow("c1", "Terranash")])
        body = self._route(value)

        carried = [row for row in body["machines"] if row.get("last_known") is True]
        self.assertEqual([row["machine"] for row in carried], ["m1", "m2"])
        # **Added after `annotate`**, so no verdict or cause is invented for a row nobody observed.
        for row in carried:
            self.assertNotIn("history", row)
            self.assertNotIn("causes", row)
        self.assertEqual(body["summary"]["machines_last_known"], 2)
        self.assertEqual(body["summary"]["locations_last_known"], 1)
        # The measured counts are unchanged: carried rows are not online and not in the total.
        self.assertEqual(body["summary"]["machines"], 0)
        self.assertEqual(body["summary"]["machines_online"], 0)

        # **The cached snapshot routing reads gains none of it.**
        cached = fleet.snapshot(allow_stale=True)
        self.assertEqual(self._by_location(cached, "c1"), [])
        self.assertEqual(len(cached["last_known_machines"]), 2)
        self.assertNotIn("machines_last_known", cached["summary"])
        for row in cached["machines"]:
            self.assertNotIn("history", row)

    def test_carried_rows_are_ordered_beside_their_site(self) -> None:
        self._warm()
        value = self._read([
            _location("d1", "Garage", [_machine("g1")]),
            _slow("c1", "Terranash"),
        ])
        body = self._route(value)
        machines = [row["machine"] for row in body["machines"]]
        self.assertEqual(machines, ["g1", "m1", "m2"],
                         "the slow site's carried rows sit where its own rows would")

        # A carried row naming a location the roster no longer has is dropped, not shown.
        value["last_known_machines"][0]["location_id"] = "gone"
        body = self._route(value)
        self.assertNotIn("gone", [row["location_id"] for row in body["machines"]])


if __name__ == "__main__":
    unittest.main()
