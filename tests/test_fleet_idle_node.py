"""A Mac with no model it can run reads "Needs attention", not "Ready" (2026-10-04, studio-1108).

1108 showed Ready, 0 done, 0 failed for days: its five studios were up, so its Hub called it ready,
but every model was `download_required` (no model files at all), so routing never picked it.
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-idle-node-")

from gateway import fleet  # noqa: E402


def _location(health: list[dict]) -> dict:
    return {
        "id": "c", "label": "1100", "local": False, "reachable": True,
        "machines": [{"machine": "n-1108", "reachable": True, "health": "up"},
                     {"machine": "n-1109", "reachable": True, "health": "up"}],
        "nodes": [], "workers": [], "model_health": health,
        "activity": {"machines": [{"machine": "n-1108", "state": "ready", "completed": 0, "failed": 0},
                                  {"machine": "n-1109", "state": "ready", "completed": 19, "failed": 0}]},
    }


class IdleNode(unittest.TestCase):
    def test_a_mac_with_no_ready_model_needs_attention_and_says_why(self):
        health = ([{"machine": "n-1108", "model_id": f"m{i}", "state": "download_required"} for i in range(3)]
                  + [{"machine": "n-1109", "model_id": "m0", "state": "ready"},
                     {"machine": "n-1109", "model_id": "m1", "state": "download_required"}])
        rows = {row["machine"]: row for row in fleet.machines_flat([_location(health)])}
        self.assertEqual(rows["n-1108"]["state"], "needs_attention")
        self.assertIn("No model is ready on this Mac (3 of 3 need downloading)", rows["n-1108"]["attention"])
        self.assertEqual(rows["n-1108"]["attention_code"], "no_model_ready")
        # One ready model is enough to take work: the sibling stays Ready.
        self.assertEqual(rows["n-1109"]["state"], "ready")
        self.assertIsNone(rows["n-1109"]["attention"])
        self.assertIsNone(rows["n-1109"]["attention_code"])

    def test_a_hub_that_reports_no_model_health_is_left_as_it_said(self):
        rows = {row["machine"]: row for row in fleet.machines_flat([_location([])])}
        self.assertEqual(rows["n-1108"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
