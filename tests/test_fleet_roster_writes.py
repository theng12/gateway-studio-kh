#!/usr/bin/env python3
"""`fleet.json` is edited by the owner and by the snapshot loop; neither may undo the other.

Measured live 2026-09-30: a controller rename answered 200, showed in the file, and was reverted
3 s later. `fleet.locations()` read the roster *before* a multi-second probe and wrote that stale
list back whenever a location's reachability changed -- and `last_reachable_at`, restamped on every
reachable probe, made that every probe. Every edit that landed during the probe window was lost.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-roster-writes-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT), "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import fleet  # noqa: E402


def _roster() -> None:
    fleet._write([{"id": "c1", "label": "Old name", "url": "http://10.0.0.1:47873"},
                  {"id": "c2", "label": "Other", "url": "http://10.0.0.2:47873"}])


class _SlowProbe:
    """A probe that is in flight until the test lets it finish."""

    def __init__(self, reachable: bool = True) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.reachable = reachable

    def __call__(self, base: str, token: str, **_) -> dict:
        self.started.set()
        self.release.wait(10)
        return {"reachable": self.reachable, "error": None, "nodes": [], "node_summary": None,
                "machines": [], "local_models": [], "workers": [], "capacity": {}}


class AnEditDuringAProbeSurvivesTest(unittest.TestCase):
    def setUp(self) -> None:
        _roster()
        fleet.invalidate()

    def _locations_during(self, probe: _SlowProbe, edit):
        result: list = []
        with patch.object(fleet, "_probe", probe):
            thread = threading.Thread(target=lambda: result.append(fleet.locations()))
            thread.start()
            self.assertTrue(probe.started.wait(5), "the snapshot never began probing")
            edit()  # the owner presses Save while the snapshot is still waiting on the network
            probe.release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        return result[0]

    def test_a_rename_made_while_the_probe_is_in_flight_is_not_reverted(self) -> None:
        self._locations_during(_SlowProbe(), lambda: fleet.rename_controller("c1", "New name"))
        labels = {row["id"]: row["label"] for row in fleet._read()}
        self.assertEqual(labels["c1"], "New name")
        # ...and the snapshot's own finding (this controller answered) was still recorded.
        self.assertTrue(next(r for r in fleet._read() if r["id"] == "c1").get("last_reachable_at"))

    def test_an_address_change_and_a_removal_survive_too(self) -> None:
        def edit() -> None:
            fleet.replace_address("c1", "http://10.9.9.9:47873", "")
            fleet.remove_controller("c2")

        self._locations_during(_SlowProbe(), edit)
        rows = {row["id"]: row for row in fleet._read()}
        self.assertEqual(rows["c1"]["url"], "http://10.9.9.9:47873")
        self.assertNotIn("c2", rows, "a controller removed during the probe must stay removed")

    def test_the_roster_is_not_rewritten_by_every_probe_of_a_location_that_stays_up(self) -> None:
        probe = _SlowProbe()
        probe.release.set()
        with patch.object(fleet, "_probe", probe):
            fleet.locations()  # first sighting: stamps `last_reachable_at`, persisted once
            with patch.object(fleet, "_write_locked", wraps=fleet._write_locked) as wrote:
                fleet.locations()
                fleet.locations()
        self.assertEqual(wrote.call_count, 0, "an unchanged reachability is not a reason to write")

    def test_a_reachability_change_is_still_persisted(self) -> None:
        down = _SlowProbe(reachable=False)
        down.release.set()
        with patch.object(fleet, "_probe", down):
            fleet.locations()
        self.assertTrue(next(r for r in fleet._read() if r["id"] == "c1").get("unreachable_since"))


class AnAddDuringItsOwnProbeDoesNotUndoOtherEditsTest(unittest.TestCase):
    def test_add_controller_re_reads_the_roster_after_its_probe(self) -> None:
        _roster()
        fleet.invalidate()
        probe = _SlowProbe()
        added: list = []
        with patch.object(fleet, "_probe", probe):
            thread = threading.Thread(target=lambda: added.append(
                fleet.add_controller("Third", "http://10.0.0.3:47873", "tok")))
            thread.start()
            self.assertTrue(probe.started.wait(5))
            fleet.rename_controller("c1", "Renamed meanwhile")  # lands during the add's probe
            probe.release.set()
            thread.join(10)
        rows = {row["id"]: row["label"] for row in fleet._read()}
        self.assertEqual(rows["c1"], "Renamed meanwhile")
        self.assertIn("Third", rows.values())


class TheRosterFileIsNeverHalfWrittenTest(unittest.TestCase):
    def test_a_reader_during_a_write_sees_a_whole_roster(self) -> None:
        _roster()
        stop = threading.Event()
        seen: list[int] = []

        def read() -> None:
            while not stop.is_set():
                seen.append(len(fleet._read()))

        thread = threading.Thread(target=read)
        thread.start()
        for n in range(200):
            fleet._write([{"id": f"c{n % 2 + 1}", "label": "x" * 200, "url": "http://10.0.0.1:1"},
                          {"id": "c9", "label": "y", "url": "http://10.0.0.9:1"}])
        stop.set()
        thread.join()
        self.assertTrue(seen)
        self.assertNotIn(0, seen, "a torn read looks like an empty roster, and the next write keeps it")


if __name__ == "__main__":
    unittest.main()
