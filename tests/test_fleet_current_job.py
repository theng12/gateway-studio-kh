#!/usr/bin/env python3
"""Fleet Groups "Doing now": which job a node runs is republished when the Hub states it, and only then.

The Hub reports a machine as `working` and the batches without saying which machine runs which, so the
Gateway cannot work it out. `machines_flat` carries `current` from the machine row or its activity entry
(`{batch_id, model, label, studio, started_at}`), nothing else the node sent, and `None` when it stated
none - which the page reads as unknown (it shows the busy studios), never as idle.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_current_job
"""
import unittest

from gateway import fleet


def _row(machine: dict, entry: dict) -> dict:
    return {
        "id": "ctrl", "label": "Ctrl", "local": False, "reachable": True,
        "machines": [{"machine": "m1", "label": "m1", "reachable": True, **machine}],
        "activity": {"machines": [{"machine": "m1", "state": "working", **entry}]},
        "nodes": [],
    }


class CurrentJobIsRepublishedWhenTheHubStatesIt(unittest.TestCase):
    def test_a_stated_current_job_reaches_the_machine_row_with_only_the_known_keys(self) -> None:
        stated = {"batch_id": "b-1", "model": "mlx/qwen", "label": "Chapter 3", "studio": "voice",
                  "started_at": 1790000000, "secret": "not republished"}
        flat = fleet.machines_flat([_row({"current": stated}, {})])
        self.assertEqual(flat[0]["current"], {"batch_id": "b-1", "model": "mlx/qwen", "label": "Chapter 3",
                                              "studio": "voice", "started_at": 1790000000})

    def test_the_activity_entry_is_read_when_the_machine_row_has_none(self) -> None:
        flat = fleet.machines_flat([_row({}, {"current": {"id": "b-2", "modality": "image"}})])
        self.assertEqual(flat[0]["current"]["batch_id"], "b-2")
        self.assertEqual(flat[0]["current"]["studio"], "image")

    def test_nothing_stated_is_unknown_not_idle(self) -> None:
        self.assertIsNone(fleet.machines_flat([_row({}, {})])[0]["current"])
        self.assertIsNone(fleet.machines_flat([_row({"current": "busy"}, {})])[0]["current"])
        self.assertIsNone(fleet.machines_flat([_row({"current": {}}, {})])[0]["current"])


if __name__ == "__main__":
    unittest.main()
