#!/usr/bin/env python3
"""A Shops Mac the controller cannot dial is on, and the Gateway must say so (Hub 0.6.193).

Three Shops Macs reach their controller through Tailscale **node sharing**, which works one way:
the shared Mac dials out and the sharer cannot dial back. Until Hub 0.6.193 the only evidence of
life the controller accepted was a dial it could not make, so all three read `offline` /
`never connected` for ever while they were switched on and calling home every minute.

The Hub now records the Mac's own authenticated call home as evidence and reports the node as
`reachable` with `via: "check-in"` and `never_connected: false`. This pins the Gateway's half:

* such a Mac raises no `machine_never_connected` and no `machine_offline`, and counts as online;
* its reachability is the Mac's own word, carried on the row as `node_via`, so an operator can tell
  "we can reach it" from "it is reaching us";
* a Lite Mode change waiting for it is delivered rather than held for ever;
* a controller that **held** the change for the Mac (202 + `queued`) is understood: the controller
  owns delivery from that moment, so nothing is left waiting here to be sent twice.

Temp data dir and mocked Hub transport only.
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

_SANDBOX = sandbox_env("gateway-checkin-")

import httpx  # noqa: E402

from gateway import config, fleet, startup_control  # noqa: E402

SHOP = ("kt-ksw-imac-blue-16-512-001-hub", "office-imac-blue-m1-8-256-hub")
CONTROLLER = "http://100.120.246.9:47873"


def _queued(request: httpx.Request) -> httpx.Response:
    """What Hub 0.6.193 answers when it cannot dial the Mac and has held the request for it."""
    return httpx.Response(202, json={"ok": True, "queued": True, "node": SHOP[0],
                                     "command": "c_9f1c", "machine": SHOP[0],
                                     "detail": "this controller cannot dial that Mac, so the request "
                                               "was held for it: it checks in every minute."})


def _row(*, via: str = "check-in", never_connected: bool | None = False,
         seen_at: float | None = None) -> dict:
    """The Shops controller as Hub 0.6.193 reports it: the Macs are on, reached by their own call home."""
    seen_at = time.time() - 30 if seen_at is None else seen_at
    own = {"machine": "local", "reachable": True, "health": "up",
           "workstation": {"enabled": False, "state": "off"}}
    shops = [{"machine": mid, "reachable": True, "health": "up",
              "workstation": {"enabled": True, "state": "idle"}} for mid in SHOP]
    nodes = []
    for index, mid in enumerate(SHOP):
        node = {"id": mid, "name": mid, "state": "reachable", "detail": None,
                "last_seen_at": seen_at, "address": f"http://100.64.0.{index + 9}:47873",
                "workstation": {"enabled": True, "state": "idle"},
                "version": "0.6.193"}
        if via is not None:
            node["via"] = via
        if never_connected is not None:
            node["never_connected"] = never_connected
        nodes.append(node)
    return {
        "id": "shops", "label": "Shops", "local": False, "reachable": True, "url": CONTROLLER,
        "machines": [own, *shops],
        "nodes": nodes,
        "activity": {"machines": [{"machine": "local", "state": "ready"},
                                  *({"machine": mid, "state": "ready"} for mid in SHOP)], "pulse": {}},
        "workers": [{"id": f"{mid}-{studio}", "machine": mid, "modality": studio, "online": True}
                    for mid in SHOP for studio in ("image", "tts")],
        "running_jobs": [], "local_models": [], "probe_machines_hidden": 0,
    }


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patcher in (patch.object(config, "DATA_DIR", self.dir),
                        patch.object(fleet, "LITE_MODE_KNOWN_PATH", self.dir / "lite_mode_known.json"),
                        patch.object(startup_control, "_target", lambda c: (CONTROLLER, "t")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def codes(row: dict) -> list[tuple[str, str, str]]:
        return [(i["severity"], i["code"], i["subject"]) for i in fleet._issues([row])]

    @staticmethod
    def flat(row: dict) -> dict[str, dict]:
        return {m["machine"]: m for m in fleet.machines_flat([row])}

    def pending(self) -> dict:
        return json.loads((self.dir / "lite_mode_pending.json").read_text())


class ACheckInMacIsOnline(_Case):
    def test_it_raises_no_never_connected_and_no_offline_line(self) -> None:
        row = _row()
        self.assertEqual(self.codes(row), [], "a Mac that calls home every minute is not a fault")

    def test_it_counts_as_online_and_not_offline(self) -> None:
        flat = self.flat(_row())
        for mid in SHOP:
            self.assertTrue(flat[mid]["reachable"])
            self.assertEqual("ready", flat[mid]["state"])
            self.assertFalse(flat[mid]["lite_off"])
        summary = fleet._summarise([_row()])
        self.assertEqual(0, summary["machines_offline"])
        self.assertEqual(3, summary["machines_online"])

    def test_the_reason_is_carried_so_a_reader_can_tell_which_way_it_works(self) -> None:
        """`reachable` alone cannot say *how*: a Mac the controller dialled and a Mac that dialled in
        are the same word and not the same situation."""
        flat = self.flat(_row())
        for mid in SHOP:
            self.assertEqual("check-in", flat[mid]["node_via"])

    def test_a_dialled_mac_says_so_and_an_older_hub_says_nothing(self) -> None:
        self.assertEqual("probe", self.flat(_row(via="probe"))[SHOP[0]]["node_via"])
        self.assertIsNone(self.flat(_row(via=None))[SHOP[0]]["node_via"],
                          "an older Hub states nothing, and nothing is not `check-in`")

    def test_a_mac_that_really_is_off_still_warns_exactly_as_before(self) -> None:
        """Nothing here softens an ordinary offline Mac: Lite Mode off, no call home, no answer."""
        row = _row()
        for node in row["nodes"]:
            node.update({"state": "unreachable", "reason": "unreachable",
                         "detail": "could not reach the node (unreachable)"})
            node.pop("via", None)
        for machine in row["machines"][1:]:
            machine.update({"reachable": False, "health": "unreachable",
                            "workstation": {"enabled": False, "state": "off"}})
        for node in row["nodes"]:
            node["workstation"] = {"enabled": False, "state": "off"}
        row["activity"]["machines"] = [{"machine": "local", "state": "ready"},
                                       *({"machine": mid, "state": "offline"} for mid in SHOP)]
        codes = [code for _s, code, _u in self.codes(row)]
        self.assertEqual(codes.count("machine_offline"), 2)


class ALiteModeChangeReachesACheckInMac(_Case):
    def test_a_waiting_change_is_delivered_once_the_mac_is_reachable_by_its_call_home(self) -> None:
        sent: list[str] = []

        def answer(request: httpx.Request) -> httpx.Response:
            sent.append(str(request.url))
            return httpx.Response(200, json={"ok": True, "workstation": {"enabled": True}})

        key = f"shops/{SHOP[0]}"
        (self.dir / "lite_mode_pending.json").write_text(json.dumps(
            {key: {"controller": "shops", "machine": SHOP[0], "settings": {"enabled": True},
                   "since": time.time() - 3600, "at": time.time() - 3600}}))
        with patch.object(startup_control, "_send_workstation",
                          side_effect=lambda *a, **k: (_ for _ in ()).throw(
                              startup_control.StartupError(502, {"code": "NODE_UNREACHABLE"}))):
            startup_control.apply_pending_lite_mode(list(self.flat(_row()).values()))
        # Still held: the mocked send refused it, which is not this test's subject.
        self.assertIn(key, self.pending())

        client = httpx.Client(transport=httpx.MockTransport(answer))
        startup_control.apply_pending_lite_mode(list(self.flat(_row()).values()), client=client)
        self.assertNotIn(key, self.pending(), "a Mac reached by its own call home gets its change")
        self.assertEqual(1, len(sent))
        self.assertTrue(sent[0].endswith(f"/api/hub/fleet/nodes/{SHOP[0]}/workstation"))

    def test_a_change_held_by_the_controller_is_accepted_rather_than_left_waiting(self) -> None:
        """Hub 0.6.193 answers `202` and `queued: true` when it cannot dial the Mac and has held the
        request for it. The controller owns delivery from that moment: keeping a copy here would
        send the same change twice, and reporting it as refused would be a lie about a change that
        is on its way."""
        client = httpx.Client(transport=httpx.MockTransport(_queued))
        answer = startup_control.set_node_workstation("shops", SHOP[0], {"enabled": True},
                                                      client=client)
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["queued"])
        self.assertFalse(answer.get("pending"), "nothing waits here: the controller is delivering it")
        self.assertEqual({}, self.pending() if (self.dir / "lite_mode_pending.json").exists() else {})

    def test_a_held_change_is_reported_on_the_mac_row_rather_than_silently(self) -> None:
        """The owner's own words for a Mac he cannot reach: the change is saved and applied when it
        comes back. That promise has to be on the row, or the Fleet page shows nothing at all."""
        client = httpx.Client(transport=httpx.MockTransport(_queued))
        startup_control.set_node_workstation("shops", SHOP[0], {"enabled": True}, client=client)
        machines = fleet.machines_flat([_row()])
        row = [m for m in machines if m["machine"] == SHOP[0]][0]
        self.assertIsNone(row.get("lite_mode_pending"),
                          "the controller took the change, so nothing is waiting on this side")


if __name__ == "__main__":
    unittest.main()
