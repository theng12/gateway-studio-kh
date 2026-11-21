#!/usr/bin/env python3
"""A Fleet row says what it knows, when it knew it, and how to ask one Mac again.

The whole-fleet `/api/fleet?force=true` read waits for every controller, so it cannot answer
"is *this* Mac still amber?" while the owner is looking at that row. These tests pin the
one-location read: it asks only that site, inside that site's own bound, and either returns a
fresh measured row or a carried row that says "Not checked in since HH:MM" — never a stale
fault drawn as current.

Run with::

    PYTHONPATH="$PWD:$PWD/tests" python -B -m unittest tests.test_fleet_attention_current
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-fleet-attention-current-")

from gateway import admin, fleet, fleet_history  # noqa: E402

CONTROLLER = "c1"
URL = "http://10.0.0.7:47873"


def _machine(mid: str, *, state: str = "ready") -> dict:
    return {"machine": mid, "label": mid, "reachable": True, "state": state,
            "chip": "Apple M4", "memory_gb": 32, "available_gb": 18.0,
            "disk_free_gb": 61.0, "disk_total_gb": 460.0}


def _probed(*, machines: list[dict] | None = None, mid: str = "m1") -> dict:
    """The shape `_probe` publishes for a controller that answered."""
    machines = [_machine(mid)] if machines is None else machines
    return {
        "reachable": True, "error": None, "error_kind": None, "role": "controller",
        "version": "0.6.247", "machines": [dict(machine) for machine in machines],
        "machine_summary": {"total": len(machines), "reachable": len(machines)},
        "nodes": [{"id": m["machine"], "state": "reachable",
                   "last_seen_at": time.time()} for m in machines],
        "node_summary": {}, "workers": [], "local_models": [], "model_health": [],
        "running_jobs": [], "jobs_read": True,
        "activity": {"pulse": {}, "machines": [
            {"machine": m["machine"], "state": m.get("state", "ready"),
             "observed_at": time.time()} for m in machines], "observed_at": time.time()},
        "latency_ms": 120,
    }


def _down_worker_probe() -> dict:
    row = _probed(machines=[_machine("m1", state="needs_attention")])
    row["workers"] = [{"machine": "m1", "id": "voice", "studio": "voice",
                       "online": False, "ready": False}]
    row["activity"]["machines"][0]["state"] = "needs_attention"
    return row


def _timeout_probe() -> dict:
    """What `_probe` returns when this site misses its bound but answered before."""
    return {"reachable": False, "slow": True, "error_kind": "timeout",
            "error": "No answer within 10s. The address is reachable but nothing replied.",
            "machines": [], "nodes": [], "activity": {"pulse": {}, "machines": []}}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in (("FLEET_PATH", root / "fleet.json"),
                            ("HISTORY_PATH", root / "fleet_history.json"),
                            ("MEMBERS_PATH", root / "fleet_members.json")):
            target = fleet_history if name == "HISTORY_PATH" else fleet
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fleet_history.reset_for_tests()
        self.addCleanup(fleet_history.reset_for_tests)
        fleet.reset_members_for_tests()
        self.addCleanup(fleet.reset_members_for_tests)
        fleet._write([{"id": CONTROLLER, "label": "Terranash", "url": URL}])
        with fleet._cache_lock:
            fleet._cache.update(at=0.0, value=None, last=None, gen=0)
        self.addCleanup(fleet.invalidate)
        self.probes: list[dict] = []

    def _answer(self, probed: dict) -> dict:
        """Make one refresh read that answers with this controller payload."""
        self.probes.clear()

        def probe(base: str, token: str, **kwargs) -> dict:
            self.probes.append({"url": base, "token": token, **kwargs})
            return dict(probed)

        stack = patch.object(fleet, "_probe", probe), patch.object(fleet, "_token_for", lambda _: "tok")
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        return admin.fleet_refresh_location(CONTROLLER)

    def _warm_cache(self, probed: dict) -> float:
        """One earlier whole-fleet read measured this Mac; keep its real answer time."""
        answered_at = time.time() - 180
        row = {**probed, "id": CONTROLLER, "label": "Terranash", "url": URL, "local": False,
               "provider_id": fleet.provider_id_for(CONTROLLER), "answered_at": answered_at}
        value = {"locations": [row], "machines": fleet.machines_flat([row]),
                 "last_known_machines": [], "summary": {}, "generated_at": answered_at}
        with fleet._cache_lock:
            fleet._cache.update(at=answered_at, value=value, last=value)
        return answered_at


class AOneLocationRefreshIsMeasured(_Case):
    def test_it_asks_only_that_controller_and_inside_its_own_bound(self) -> None:
        before = time.time()
        body = self._answer(_probed())

        self.assertEqual(len(self.probes), 1, "one Mac's row is one probe, not a fleet fan-out")
        self.assertEqual(self.probes[0]["url"], URL)
        self.assertEqual(self.probes[0]["token"], "tok")
        self.assertEqual(self.probes[0]["timeout"], fleet._probe_timeout(CONTROLLER))
        self.assertEqual(body["bound_seconds"], fleet._probe_timeout(CONTROLLER))
        self.assertGreaterEqual(body["measured_at"], before)
        self.assertTrue(body["reachable"])
        self.assertEqual([row["machine"] for row in body["machines"]], ["m1"])

    def test_a_current_amber_row_carries_its_cause_and_when_it_was_measured(self) -> None:
        before = time.time()
        body = self._answer(_down_worker_probe())
        row = body["machines"][0]

        self.assertEqual(row["state"], "needs_attention")
        self.assertEqual(row["attention"], "Voice studio not running")
        self.assertEqual(row["attention_code"], "studio_down")
        self.assertEqual(row["attention_source"], "current")
        self.assertGreaterEqual(row["attention_measured_at"], before)
        self.assertLessEqual(row["attention_measured_at"], time.time())
        self.assertEqual(row["attention_status"],
                         "Measured at " + time.strftime("%H:%M", time.localtime(row["attention_measured_at"])))

    def test_a_fresh_answer_with_no_fault_clears_the_old_amber(self) -> None:
        self._warm_cache(_down_worker_probe())
        body = self._answer(_probed(machines=[_machine("m1", state="ready")]))
        row = body["machines"][0]

        self.assertEqual(row["state"], "ready")
        self.assertIsNone(row["attention"], "the old fault is not carried into a fresh answer")
        self.assertIsNone(row["attention_code"])
        self.assertEqual(row["attention_source"], "current")
        self.assertNotIn("Not checked in", row["attention_status"])

    def test_an_unreachable_mac_says_when_it_was_last_checked(self) -> None:
        answered_at = self._warm_cache(_probed())

        body = self._answer(_timeout_probe())
        self.assertFalse(body["reachable"])
        self.assertIn("No answer within", body["error"], "the bound says why there is no answer")
        self.assertTrue(body["not_checked"])
        self.assertEqual(len(body["machines"]), 1)
        row = body["machines"][0]
        self.assertTrue(row["last_known"])
        self.assertIsNone(row["reachable"], "unknown now, not broken")
        self.assertEqual(row["attention_source"], "last_known")
        self.assertAlmostEqual(row["attention_measured_at"], answered_at, places=3)
        self.assertEqual(row["attention_status"],
                         "Not checked in since " + time.strftime("%H:%M", time.localtime(answered_at)))

    def test_an_unknown_location_says_so_without_probing_anything(self) -> None:
        result = admin.fleet_refresh_location("no-such-site")
        self.assertEqual(len(self.probes), 0)
        self.assertEqual(result.status_code, 404)
        self.assertIn("No such location", result.body.decode())


if __name__ == "__main__":
    unittest.main()
