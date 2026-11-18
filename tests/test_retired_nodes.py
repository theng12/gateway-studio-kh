"""A machine removed here stays gone, even while its site still reports it.

Measured on 0.2.275 against a sold Mac (`grace-2fs-imac-hub`, Shops): `DELETE ...?force=true` answered
`{"ok": true, "forced_local": true}` and the row came back on the next read, rebuilt from the
controller's report — a success that was only true from one side.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path

TEMP = tempfile.TemporaryDirectory(prefix="retired-nodes-")
os.environ["GATEWAY_RETIRED_NODES"] = str(Path(TEMP.name) / "retired-nodes.json")

retired = importlib.import_module("gateway.retired_nodes")
fleet = importlib.import_module("gateway.fleet")


class RetiredNodes(unittest.TestCase):
    def setUp(self) -> None:
        retired.reset_for_tests()

    def test_mark_survives_a_restart_and_clears(self) -> None:
        retired.mark("c1", "m1")
        # A new import reads the same file: the tombstone is on disk, not in memory.
        again = importlib.reload(retired)
        self.assertEqual([entry["machine_id"] for entry in again.entries()], ["m1"])
        self.assertTrue(again.clear("c1", "m1"))
        self.assertEqual(again.entries(), [])
        self.assertFalse(again.clear("c1", "m1"))

    def test_a_removed_row_is_dropped_even_though_the_controller_still_lists_it(self) -> None:
        now = time.time()
        retired.mark("c1", "m1", at=now - 60)
        machines = [
            {"location_id": "c1", "machine": "m1", "state": "offline"},
            {"location_id": "c1", "machine": "m2", "state": "working"},
        ]
        rows = [{"id": "c1", "machines": list(machines),
                 "activity": {"machines": [{"machine": "m1"}, {"machine": "m2"}]}}]
        removed = fleet._apply_removed(rows, machines, [])  # noqa: SLF001
        self.assertEqual([m["machine"] for m in machines], ["m2"], "the sold Mac is gone from capacity")
        self.assertEqual([m["machine"] for m in rows[0]["machines"]], ["m2"])
        self.assertEqual([m["machine"] for m in rows[0]["activity"]["machines"]], ["m2"])
        self.assertEqual([entry["machine_id"] for entry in removed], ["m1"])
        self.assertIn("still registered", fleet.removal_sentence("grace-2fs-iMac",
                                                                {"ok": False, "forced_local": True}))

    def test_a_machine_that_registers_again_comes_back(self) -> None:
        marked_at = time.time() - 600
        retired.mark("c1", "m1", at=marked_at)
        machines = [{"location_id": "c1", "machine": "m1", "state": "working",
                     "node_registered_at": marked_at + 60, "node_last_seen_at": marked_at + 90}]
        removed = fleet._apply_removed([], machines, [])  # noqa: SLF001
        self.assertEqual(removed, [], "a genuine re-registration is not removed")
        self.assertEqual([m["machine"] for m in machines], ["m1"], "the row is shown again")
        self.assertEqual(retired.entries(), [], "and the tombstone is cleared")

    def test_the_very_report_that_resurrected_it_does_not_clear_the_tombstone(self) -> None:
        # Measured 2026-11-18: the controller kept listing the sold Mac and its last-seen stamp kept
        # moving; a newer `last_seen_at` must never be read as "it is back".
        marked_at = time.time() - 60
        retired.mark("c1", "m1", at=marked_at)
        machines = [{"location_id": "c1", "machine": "m1", "state": "offline",
                     "node_last_seen_at": marked_at + 120}]
        fleet._apply_removed([], machines, [])  # noqa: SLF001
        self.assertEqual(machines, [], "still removed after the controller re-reports it")
        self.assertEqual([entry["machine_id"] for entry in retired.entries()], ["m1"],
                         "and the tombstone is still there")

    def test_a_registration_from_before_the_removal_does_not_resurrect_it(self) -> None:
        marked_at = time.time()
        retired.mark("c1", "m1", at=marked_at)
        machines = [{"location_id": "c1", "machine": "m1", "state": "offline",
                     "node_registered_at": marked_at - 30}]
        fleet._apply_removed([], machines, [])  # noqa: SLF001
        self.assertEqual(machines, [], "an old registration never brings it back")

    def test_the_sentence_says_which_happened(self) -> None:
        self.assertIn("and from its site's Studio Hub", fleet.removal_sentence("m1", {"ok": True}))
        self.assertIn("still registered", fleet.removal_sentence("m1", {"ok": False, "forced_local": True}))
        self.assertIn("no longer had it", fleet.removal_sentence("m1", {"ok": True, "already_gone": True}))


if __name__ == "__main__":
    unittest.main()
