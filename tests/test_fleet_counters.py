"""Fleet -> Machines "Reset counters": a baseline every Hub counts from, never a deletion.

Owner, 2026-10-04: the per-Mac done/failed numbers were mostly last night's memory refusals, already
fixed; he wants a clean slate so new work is counted accurately. The reset is a timestamp this
Gateway keeps and sends with the stats read (`activity_since`); the Hubs keep their history. A Hub
too old to honour it is reported as counting everything, never as reset.
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-counters-")

from gateway import admin, fleet  # noqa: E402


class ResetCounters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(fleet, "FLEET_COUNTERS_PATH", Path(self.tmp.name) / "fleet_counters.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetched_paths(self) -> list[str]:
        seen: list[str] = []

        def fetch(_client, _base, _token, path):
            seen.append(path)
            return {}

        with patch.object(fleet, "_fetch", side_effect=fetch):
            fleet._fetch_optional(None, "http://hub", "tok")
        return seen

    def test_no_reset_asks_for_nothing_new(self):
        self.assertIsNone(fleet.counters_since())
        self.assertIn("/api/hub/stats", self._fetched_paths())

    def test_the_reset_route_sets_a_baseline_every_stats_read_carries(self):
        with patch.object(fleet.time, "time", return_value=1791115000.0):
            body = admin.reset_fleet_counters()
        self.assertEqual(body, {"counters_since": 1791115000.0})
        self.assertEqual(fleet.counters_since(), 1791115000.0)
        self.assertIn("/api/hub/stats?activity_since=1791115000.0", self._fetched_paths())
        self.assertIn("/api/hub/fleet/nodes", self._fetched_paths())  # the other reads are untouched

    def test_a_hub_is_shown_as_reset_only_when_it_honoured_the_baseline(self):
        fleet.reset_counters(now=5000.0)
        machine = {"machine": "node-a", "completed": 3, "failed": 0, "state": "ready"}

        def location(window):
            return {"id": "c", "label": "C", "local": False, "reachable": True,
                    "machines": [{"machine": "node-a", "reachable": True}], "nodes": [],
                    "workers": [], "activity": {"machines": [machine], "window": window}}

        honoured = fleet.machines_flat([location({"since_s": 5000.0, "now": 6000.0})])[0]
        self.assertEqual(honoured["counted_since"], 5000.0)
        # An older Hub ignores the parameter and keeps its 30-day window: not a reset.
        older = fleet.machines_flat([location({"since_s": 10.0, "now": 6000.0})])[0]
        self.assertIsNone(older["counted_since"])
        self.assertIsNone(fleet.machines_flat([location(None)])[0]["counted_since"])
        self.assertEqual(fleet._summarise([{**location({"since_s": 5000.0}), "running_jobs": [],
                                             "activity": {"machines": [machine], "pulse": {},
                                                          "window": {"since_s": 5000.0}}}])["counters_since"],
                         5000.0)


if __name__ == "__main__":
    unittest.main()
