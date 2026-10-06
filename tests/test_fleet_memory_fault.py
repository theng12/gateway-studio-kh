"""Low free memory is a fault only when the Mac cannot work (owner, 2026-10-05).

8 GB Macs generating Breeze sit at 1.6-1.8 GB free; "2 nodes low on memory" on every busy group was never read.
A fault is: critical memory pressure, or idle and under the start floor for 5 minutes.
"""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-memory-fault-")

from gateway import fleet  # noqa: E402

T0 = 1_000_000.0


def _mac(**kw) -> dict:
    return {"location_id": "c", "machine": "m", "reachable": True, "memory_gb": 8.0, "available_gb": 1.2,
            "state": "ready", "current": None, "workstation": None, "disk": None, **kw}


class MemoryFault(unittest.TestCase):
    def setUp(self):
        fleet._MEMORY_LOW_SINCE.clear()  # noqa: SLF001

    def test_low_memory_while_a_job_runs_is_not_a_fault_however_long(self):
        busy = _mac(current={"batch_id": "b"})
        for t in (0, 60, 600, 3600):
            self.assertIsNone(fleet.memory_fault(busy, T0 + t))
        # `working` with the job unnamed (older Hub) is busy too.
        self.assertIsNone(fleet.memory_fault(_mac(state="working"), T0 + 3600))

    def test_idle_and_low_is_a_fault_only_after_five_minutes(self):
        idle = _mac()
        self.assertIsNone(fleet.memory_fault(idle, T0))
        self.assertIsNone(fleet.memory_fault(idle, T0 + 299))
        self.assertIn("cannot take work", fleet.memory_fault(idle, T0 + 300))

    def test_a_job_or_recovery_restarts_the_five_minutes(self):
        fleet.memory_fault(_mac(), T0)
        fleet.memory_fault(_mac(current={"batch_id": "b"}), T0 + 200)  # picked up work
        self.assertIsNone(fleet.memory_fault(_mac(), T0 + 400))  # idle again: clock restarted at 400
        self.assertIsNone(fleet.memory_fault(_mac(available_gb=4.0), T0 + 1000))  # recovered
        self.assertIsNone(fleet.memory_fault(_mac(), T0 + 1100))  # low again: new clock
        self.assertIsNotNone(fleet.memory_fault(_mac(), T0 + 1400))

    def test_the_start_floor_is_the_macs_own_when_it_has_one(self):
        # 3 GB free is fine against the 20 % default of 8 GB, but under a 4 GB start floor it cannot start.
        low = _mac(available_gb=3.0, workstation={"start_free_gb": 4.0})
        fleet.memory_fault(low, T0)
        self.assertIsNotNone(fleet.memory_fault(low, T0 + 301))
        self.assertIsNone(fleet.memory_fault(_mac(available_gb=3.0, location_id="d"), T0 + 301))

    def test_critical_pressure_is_a_fault_even_while_working(self):
        busy = {"current": {"batch_id": "b"}}
        self.assertIn("pressure", fleet.memory_fault(_mac(memory_pressure="critical", **busy), T0))
        self.assertIsNone(fleet.memory_fault(_mac(memory_pressure="normal", **busy), T0))

    def test_cumulative_swap_is_informational_when_memory_is_healthy(self):
        mac = _mac(
            memory_gb=34.36,
            available_gb=22.0,
            memory_pressure="normal",
            disk={"swap_used_gb": 6.0},
        )
        self.assertIsNone(fleet.memory_fault(mac, T0))

    def test_unknown_or_offline_is_never_a_fault(self):
        self.assertIsNone(fleet.memory_fault(_mac(available_gb=None), T0 + 999))
        self.assertIsNone(fleet.memory_fault(_mac(reachable=False), T0 + 999))

    def test_machines_flat_carries_the_verdict_on_every_row(self):
        def site(current):
            machine = {"machine": "m", "reachable": True, "memory_gb": 8.0, "available_gb": 1.2, "health": "up"}
            if current:
                machine["current"] = {"batch_id": "b", "model": "x"}
            return {"id": "c", "label": "L", "local": False, "reachable": True, "machines": [machine], "nodes": [],
                    "workers": [], "model_health": [],
                    "activity": {"machines": [{"machine": "m", "state": "working" if current else "ready"}]}}
        with mock.patch.object(fleet.time, "time", return_value=T0):
            busy = fleet.machines_flat([site(True)])[0]
            idle = fleet.machines_flat([site(False)])[0]
        self.assertIsNone(busy["memory_fault"])
        self.assertIsNone(idle["memory_fault"])  # idle but only just
        with mock.patch.object(fleet.time, "time", return_value=T0 + 600):
            self.assertIn("cannot take work", fleet.machines_flat([site(False)])[0]["memory_fault"])


if __name__ == "__main__":
    unittest.main()
