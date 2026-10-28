#!/usr/bin/env python3
"""Fastest-first final-render choice, and the render-only pause exemption, on the Gateway side.

Owner, 2026-10-28:

* when several opted-in render Macs are eligible the render goes to the fastest, and only a busy or
  ineligible one sends it down the list;
* "fastest" is measured evidence first (a Mac's own timed renders), hardware as a stated fallback;
* a paused location still offers the Macs whose own pause exempts final render — and nothing else.

Every case names which of those it pins. `Fleet` and `location` reuse the fixtures of
`test_render_optin.py` so a row here is a row that module's own tests also accept.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_render_optin as base  # noqa: E402
from gateway import fleet, render_optin  # noqa: E402
from test_render_optin import (  # noqa: E402
    ACCEPTING, LAN, NOW, Fleet, location, worker,
)

DIGEST = "a" * 64


def machine_row(*, chip: str = "Apple M4", memory_gb: float = 16.0,
                speed: float | None = None, renders: int = 0) -> dict:
    """One machine row as a Hub publishes it, with its measured render speed when it has one."""
    row = {"chip": chip, "memory_gb": memory_gb}
    if speed is not None:
        row["render_speed"] = {"speed": speed, "renders": renders}
    return row


def spec(worker_row: dict, machine: dict, address: str) -> dict:
    """One Mac as `location()` takes it: its address, its machine-row facts, its render worker row.

    `test_render_optin.location` builds the machine row from these fields, so the measured speed
    travels the same way the chip and memory do.
    """
    out = {"address": address, "worker": worker_row}
    for key in ("chip", "memory_gb", "render_speed"):
        if key in machine:
            out[key] = machine[key]
    return out


def candidate(name: str, *, chip: str = "M4", speed: float | None = None,
              renders: int = 0, authority: bool = False, memory_gb: float = 16.0) -> dict:
    """One row shaped as `render_candidates` builds it, for the ordering function alone."""
    return {
        "site_id": "site-a", "roster_controller_id": "roster-a",
        "hub_controller_id": "hub-a", "target_hub_controller_id": name,
        "node_id": name, "hardware_class": chip, "profile_digest": DIGEST,
        "available_slots": 1, "priority_rank": 0,
        "measured_render_speed": ({"output_seconds_per_wall_second": speed, "renders": renders}
                                  if speed is not None else None),
        "selected_machine": {"id": name, "label": name, "chip": chip,
                             "memory_gb": memory_gb, "hardware_class": chip},
        **({"authority": render_optin.ACCEPTED, "network": "same_network"} if authority else {}),
    }


class OrderTests(unittest.TestCase):
    """The ordering function itself, called through `render_candidates`."""

    def order(self, rows: list[dict]) -> list[str]:
        """The dispatcher's own ordering function, on rows already accepted.

        Eligibility is `test_render_optin`'s subject and is exercised there; what is under test here
        is only the order, so the candidates go straight to the function `render_candidates` sorts
        with.
        """
        return [row["node_id"] for row in fleet._render_candidates_order(rows)]

    def test_a_measured_mac_outranks_an_unmeasured_one_even_on_slower_hardware(self) -> None:
        """Measured evidence first: an M1 that has timed renders beats an unmeasured M4."""
        order = self.order([
            candidate("fresh-m4", chip="M4"),
            candidate("timed-m1", chip="M1", speed=2.4, renders=6),
        ])
        self.assertEqual(order, ["timed-m1", "fresh-m4"])

    def test_the_faster_measured_mac_goes_first(self) -> None:
        order = self.order([
            candidate("slow", speed=0.9, renders=3),
            candidate("fast", speed=3.1, renders=2),
            candidate("middle", speed=1.8, renders=9),
        ])
        self.assertEqual(order, ["fast", "middle", "slow"])

    def test_unmeasured_macs_fall_back_to_chip_then_memory(self) -> None:
        """The stated fallback, never a hand-written machine list."""
        order = self.order([
            candidate("m1", chip="M1"),
            candidate("m4-small", chip="M4"),
            candidate("m2", chip="M2"),
        ])
        self.assertEqual(order, ["m4-small", "m2", "m1"])

    def test_chip_generation_beats_memory_between_two_unmeasured_macs(self) -> None:
        order = self.order([candidate("m2-big", chip="M2", memory_gb=32.0),
                            candidate("m4-small", chip="M4")])
        self.assertEqual(order, ["m4-small", "m2-big"])

    def test_memory_breaks_a_tie_between_the_same_chip(self) -> None:
        order = self.order([candidate("m4-small", chip="M4"),
                            candidate("m4-big", chip="M4", memory_gb=32.0)])
        self.assertEqual(order, ["m4-big", "m4-small"])

    def test_qualified_and_owner_accepted_tiers_still_come_first(self) -> None:
        """The existing tiers are untouched: the fastest *eligible* Mac wins within its tier."""
        order = self.order([
            candidate("accepted", authority=render_optin.ACCEPTED, speed=9.0, renders=40),
            candidate("production"),
        ])
        self.assertEqual(order, ["production", "accepted"])

    def test_a_zero_speed_is_not_read_as_a_measurement(self) -> None:
        """A stated 0 is a claim this code must not act on, and absence must not become 0."""
        order = self.order([
            candidate("zero", speed=0.0, renders=4),
            candidate("timed", chip="M1", speed=1.0, renders=1),
        ])
        self.assertEqual(order, ["timed", "zero"], "0 must be treated as no measurement")


class Case(base.Case):
    """`test_render_optin`'s own fixtures, with this Mac's owner switches turned on.

    Every candidate here is an owner-accepted Mac, which is what makes it eligible at all; that
    switch is `test_render_optin`'s subject and is exercised there, so it is pinned open here and
    the ranking is what each case below varies.
    """

    #: Every machine id a case below switches on. Absent from here, a Mac is simply not eligible,
    #: which is the row `render_candidates` has always skipped.
    ACCEPTED = ("slow", "fast", "next-best", "only", "timed", "fresh", "mac-a", "mac-b")

    def setUp(self) -> None:
        super().setUp()
        keys = frozenset(f"roster-a/{mid}" for mid in self.ACCEPTED)
        patcher = mock.patch.object(render_optin, "enabled_keys", return_value=keys)
        patcher.start()
        self.addCleanup(patcher.stop)


class EligibleFallthroughTests(Case):
    """The real candidate list: eligibility, fastest-first, and busy fallthrough."""

    def test_the_fastest_idle_mac_heads_the_list(self) -> None:
        with Fleet(render_location({
            "slow": spec(worker("slow", accepting=ACCEPTING),
                         machine_row(speed=0.8, renders=4), "http://192.168.1.11:1"),
            "fast": spec(worker("fast", accepting=ACCEPTING),
                         machine_row(chip="Apple M2", speed=2.9, renders=2),
                         "http://192.168.1.12:1"),
        })):
            rows = fleet.render_candidates(DIGEST)
        self.assertEqual([row["node_id"] for row in rows], ["fast", "slow"],
                         "measured speed outranks chip generation")

    def test_a_busy_fastest_mac_falls_through_to_the_next(self) -> None:
        busy = render_location({
            "fast": spec(worker("fast", accepting=ACCEPTING),
                         machine_row(speed=2.9, renders=2), "http://192.168.1.12:1"),
            "next-best": spec(worker("next-best", accepting=ACCEPTING),
                              machine_row(speed=1.4, renders=5), "http://192.168.1.13:1"),
        })
        # The busy Mac's own worker says so, which is how the Hub reports a running render.
        for row in busy["workers"]:
            if row.get("physical_machine_id") == "fast":
                row["busy"] = True
        with Fleet(busy):
            rows = fleet.render_candidates(DIGEST)
        self.assertEqual([row["node_id"] for row in rows], ["next-best"],
                         "an ineligible Mac is not a candidate at all, so the next best takes it")

    def test_an_unmeasured_mac_still_renders_when_it_is_the_only_one(self) -> None:
        with Fleet(render_location({
            "only": spec(worker("only", accepting=ACCEPTING),
                         machine_row(chip="Apple M1"), "http://192.168.1.14:1"),
        })):
            rows = fleet.render_candidates(DIGEST)
        self.assertEqual([row["node_id"] for row in rows], ["only"])
        self.assertIsNone(rows[0]["measured_render_speed"],
                          "no measurement is None, never 0 and never a guess")

    def test_a_malformed_speed_on_the_machine_row_is_no_measurement(self) -> None:
        for bad in (0, -1.0, "2.4", True, {"speed": "x", "renders": 2},
                    {"speed": 1.2, "renders": 0}, {"speed": 1.2}):
            machine = {"chip": "Apple M4", "memory_gb": 16.0, "render_speed": bad}
            self.assertIsNone(fleet._measured_render_speed(machine), f"{bad!r} must read as absent")

    def test_the_measured_speed_reaches_the_candidate(self) -> None:
        with Fleet(render_location({
            "timed": spec(worker("timed", accepting=ACCEPTING),
                          machine_row(speed=1.75, renders=7), "http://192.168.1.15:1"),
        })):
            row = fleet.render_candidates(DIGEST)[0]
        self.assertEqual(row["measured_render_speed"],
                         {"output_seconds_per_wall_second": 1.75, "renders": 7})


class PauseExemptionTests(Case):
    """A paused location offers only the Macs whose own pause exempts final render."""

    def paused(self, macs: dict[str, dict]) -> dict:
        row = render_location(macs)
        row["paused_at"] = NOW - 60
        row["paused_by"] = "owner"
        for node in row["nodes"]:
            node["paused"] = True
        return row

    def test_a_paused_location_offers_nothing_by_default(self) -> None:
        with Fleet(self.paused({
            "mac-a": spec(worker("mac-a", accepting=ACCEPTING),
                          machine_row(), "http://192.168.1.20:1"),
        })):
            self.assertEqual(fleet.render_candidates(DIGEST), [])

    def test_a_paused_location_offers_a_mac_whose_own_pause_exempts_render(self) -> None:
        mac = spec({**worker("mac-a", accepting=ACCEPTING),
                    render_optin.PAUSED_RENDER_KEY: True},
                   machine_row(), "http://192.168.1.21:1")
        with Fleet(self.paused({"mac-a": mac})):
            rows = fleet.render_candidates(DIGEST)
        self.assertEqual([row["node_id"] for row in rows], ["mac-a"])

    def test_the_exemption_covers_only_the_pause(self) -> None:
        """A drained or unreachable location, and an ineligible Mac, are refused as before."""
        mac = spec({**worker("mac-a", accepting=ACCEPTING),
                    render_optin.PAUSED_RENDER_KEY: True},
                   machine_row(), "http://192.168.1.22:1")
        drained = self.paused({"mac-a": mac})
        drained["drained"] = True
        with Fleet(drained):
            self.assertEqual(fleet.render_candidates(DIGEST), [],
                             "a drained location still refuses")
        unreachable = self.paused({"mac-a": mac})
        unreachable["reachable"] = False
        with Fleet(unreachable):
            self.assertEqual(fleet.render_candidates(DIGEST), [],
                             "an unreachable location still refuses")
        busy = self.paused({"mac-a": mac})
        for row in busy["workers"]:
            row["busy"] = True
        with Fleet(busy):
            self.assertEqual(fleet.render_candidates(DIGEST), [], "a busy Mac is still busy")

    def test_an_unpaused_location_is_unaffected_by_the_exemption(self) -> None:
        plain = spec(worker("mac-a", accepting=ACCEPTING),
                     machine_row(), "http://192.168.1.23:1")
        exempt = {**plain, "worker": {**plain["worker"], render_optin.PAUSED_RENDER_KEY: True}}
        with Fleet(render_location({"mac-a": plain, "mac-b": exempt})):
            rows = fleet.render_candidates(DIGEST)
        self.assertEqual(sorted(row["node_id"] for row in rows), ["mac-a", "mac-b"])


class PageTests(Case):
    """The Fleet page's Remote final rendering section: the order, its reason, and the switch."""

    def rank(self) -> dict:
        """The page's own function, asked about this suite's render profile."""
        with mock.patch.object(render_optin, "local_networks", return_value=LAN):
            return render_optin.rank_view(DIGEST)

    def test_the_page_publishes_the_order_and_why_each_mac_is_where_it_is(self) -> None:
        with Fleet(render_location({
            "timed": spec(worker("timed", accepting=ACCEPTING),
                          machine_row(chip="Apple M1", speed=2.5, renders=4),
                          "http://192.168.1.30:1"),
            "fresh": spec(worker("fresh", accepting=ACCEPTING),
                          machine_row(chip="Apple M4"), "http://192.168.1.31:1"),
        })):
            rank = self.rank()
        self.assertEqual([row["machine_id"] for row in rank["order"]], ["timed", "fresh"],
                         "measured evidence outranks the faster chip")
        self.assertEqual(rank["order"][0]["reason"], render_optin.RANK_MEASURED)
        self.assertEqual(rank["order"][1]["reason"], render_optin.RANK_HARDWARE)
        self.assertEqual(rank["measured_macs"], 1)
        self.assertEqual(rank["hardware_macs"], 1)
        self.assertIn("Fastest first", rank["basis"])
        self.assertIn("measured render speed", rank["basis"])
        self.assertIn("chip, then memory", rank["basis"], "the fallback is stated, not implied")
        self.assertNotIn("chosen by hand", rank["basis"].replace("picked by hand", ""))

    def test_the_order_is_the_dispatchers_own_list(self) -> None:
        """The page cannot describe a ranking the dispatcher does not use."""
        with Fleet(render_location({
            "slow": spec(worker("slow", accepting=ACCEPTING),
                         machine_row(speed=0.7, renders=2), "http://192.168.1.35:1"),
            "fast": spec(worker("fast", accepting=ACCEPTING),
                         machine_row(speed=2.2, renders=2), "http://192.168.1.36:1"),
        })):
            rank = self.rank()
            dispatched = fleet.render_candidates(DIGEST)
        self.assertEqual([row["machine_id"] for row in rank["order"]],
                         [row["node_id"] for row in dispatched])

    def test_a_mac_with_no_measurement_has_none_rather_than_zero(self) -> None:
        with Fleet(render_location({
            "fresh": spec(worker("fresh", accepting=ACCEPTING), machine_row(chip="Apple M4"),
                          "http://192.168.1.32:1"),
        })):
            rank = self.rank()
        self.assertIsNone(rank["order"][0]["render_speed"])
        self.assertEqual(rank["order"][0]["reason"], render_optin.RANK_HARDWARE)

    def test_each_mac_row_says_its_speed_and_its_exemption(self) -> None:
        timed = spec({**worker("mac-a", accepting=ACCEPTING),
                      render_optin.PAUSED_RENDER_KEY: True},
                     machine_row(speed=1.5, renders=3), "http://192.168.1.33:1")
        plain = spec(worker("mac-b", accepting=ACCEPTING), machine_row(), "http://192.168.1.34:1")
        with Fleet(render_location({"mac-a": timed, "mac-b": plain})):
            rows = render_optin.workers(fleet.snapshot(allow_stale=True), networks=LAN)
        by_id = {row["machine_id"]: row for row in rows}
        self.assertEqual(by_id["mac-a"]["render_speed"], 1.5)
        self.assertEqual(by_id["mac-a"]["render_speed_renders"], 3)
        self.assertTrue(by_id["mac-a"]["render_while_paused"])
        self.assertIsNone(by_id["mac-b"]["render_speed"], "an unmeasured Mac states none")
        self.assertFalse(by_id["mac-b"]["render_while_paused"], "off unless the owner set it")

    def test_the_exemption_survives_the_workers_snapshot_trim(self) -> None:
        """`_trim_worker` copies the key only when the Hub sent it, so an older row is unchanged."""
        sent = worker("mac-a", accepting=ACCEPTING)
        sent[render_optin.PAUSED_RENDER_KEY] = True
        self.assertIs(fleet._trim_worker(sent)[render_optin.PAUSED_RENDER_KEY], True)
        plain = worker("mac-b", accepting=ACCEPTING)
        self.assertNotIn(render_optin.PAUSED_RENDER_KEY, fleet._trim_worker(plain))


class RelayTests(unittest.TestCase):
    def test_the_exemption_switch_is_relayed_through_the_controller(self) -> None:
        from gateway import startup_control
        seen: dict = {}

        def post(controller_id, machine_id, route, body, **kwargs):
            seen.update(controller=controller_id, machine=machine_id, route=route, body=body)
            return {"ok": True, "render_while_paused": body["render_while_paused"]}

        with mock.patch.object(startup_control, "_node_post", side_effect=post), \
                mock.patch.object(startup_control, "audit"):
            answer = startup_control.set_node_render_while_paused("roster-a", "mac-a", True)
        self.assertEqual(seen["route"], "render-while-paused")
        self.assertEqual(seen["body"], {"render_while_paused": True,
                                        "requested_by": "gateway-admin"})
        self.assertTrue(answer["render_while_paused"])

    def test_a_non_boolean_is_refused_before_anything_is_sent(self) -> None:
        from gateway import startup_control
        with mock.patch.object(startup_control, "_node_post") as post:
            for bad in ("yes", 1, None, [True]):
                with self.assertRaises(startup_control.StartupError) as caught:
                    startup_control.set_node_render_while_paused("roster-a", "mac-a", bad)
                self.assertEqual(caught.exception.status, 400)
        post.assert_not_called()

    def test_render_is_a_rankable_function_at_the_wire(self) -> None:
        from gateway import startup_control
        self.assertIn("render", startup_control.WORK_PRIORITY_FUNCTIONS)
        self.assertIn("render", fleet._PRIORITY_FUNCTIONS)
        with mock.patch.object(startup_control, "_node_post",
                               return_value={"ok": True}) as post, \
                mock.patch.object(startup_control, "audit"):
            startup_control.set_node_work_policy("roster-a", "mac-a", ["chat", "render"])
        self.assertEqual(post.call_args.args[3], {"priority_order": ["chat", "render"]})


if __name__ == "__main__":
    unittest.main()
