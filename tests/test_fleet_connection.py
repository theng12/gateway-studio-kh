#!/usr/bin/env python3
"""A Mac's network link on the Fleet page: its kind, the media its Ethernet negotiated, and the
cables worth replacing.

The owner, 2026-10-28: Mac 1301 was linked at `100baseTX` on an old cable — `10baseT` before that —
and every other field on this page read it as a healthy Mac, because a thin link does not fail, it
*negotiates*. So the Hub publishes a `connection` block and this carries it into the machine rows
and the fleet summary.

What this file pins:

* **The Hub's numbers are read, never inferred.** A stated `1000baseT <full-duplex>` is 1000/full;
  a stated 100 is 100 and `slow`; Wi-Fi is named as Wi-Fi and is **not** called slow.
* **Unknown is not fine.** A Hub older than the block publishes nothing; that row is `None` and the
  summary counts it as `unmeasured`, never as Ethernet or as a healthy link.
* **A zero never stands in for an unreadable link** (§6): `unmeasured` carries its reason and draws
  "—", and no path invents a speed the Mac did not state.
* **The summary and the table cannot disagree**: both are counted from the same machine rows.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_connection
"""
import unittest

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import fleet


def _hub_connection(**overrides) -> dict:
    """What Hub 0.6.206 publishes, as `components/hub/backend/network_link.collect` builds it."""
    block = {
        "status": "measured", "reason": None, "interface": "en0", "kind": "ethernet",
        "speed_mbps": 1000, "duplex": "full", "media": "autoselect (1000baseT <full-duplex>)",
        "autoselect": True, "link_up": True,
        "wifi": {"interface": "en1", "up": False, "rate_mbps": None},
        "sampled_at": 1793000000.0,
    }
    block.update(overrides)
    return block


def _machine(**overrides) -> dict:
    machine = {"machine": "1301", "label": "1301", "reachable": True,
               "connection": _hub_connection()}
    machine.update(overrides)
    return machine


def _row(machines: list[dict]) -> dict:
    return {
        "id": "ctrl", "label": "Ctrl", "local": False, "reachable": True,
        "machines": machines,
        "activity": {"machines": [{"machine": m["machine"], "state": "ready"} for m in machines],
                     "pulse": {}},
        "running_jobs": [],
        "nodes": [],
    }


class ConnectionReadingTests(unittest.TestCase):
    """The Hub's block, read as stated."""

    def test_a_gigabit_full_duplex_link_is_not_slow(self):
        connection = fleet.machine_connection(_machine())
        self.assertEqual(connection["status"], "measured")
        self.assertEqual(connection["speed_mbps"], 1000)
        self.assertEqual(connection["duplex"], "full")
        self.assertFalse(connection["slow"])
        self.assertEqual(connection["text"], "Ethernet 1000 Mb/s full")

    def test_a_100_megabit_link_is_slow_and_says_its_speed(self):
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            speed_mbps=100, duplex="full", media="autoselect (100baseTX <full-duplex>)")))
        self.assertTrue(connection["slow"])
        self.assertEqual(connection["text"], "Ethernet 100 Mb/s full")

    def test_a_10_megabit_half_duplex_link_is_slow(self):
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            speed_mbps=10, duplex="half", media="10baseT/UTP <half-duplex>")))
        self.assertTrue(connection["slow"])
        self.assertEqual(connection["text"], "Ethernet 10 Mb/s half")

    def test_a_gigabit_link_at_half_duplex_is_still_slow(self):
        """Half duplex is a fault whatever the rate: it is a duplex mismatch, not a fast link."""
        connection = fleet.machine_connection(_machine(connection=_hub_connection(duplex="half")))
        self.assertTrue(connection["slow"])

    def test_a_wifi_primary_is_named_wifi_and_is_not_called_slow(self):
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            interface="en1", kind="wifi", speed_mbps=None, duplex=None, media="autoselect",
            wifi={"interface": "en1", "up": True, "rate_mbps": 432})))
        self.assertFalse(connection["slow"])
        self.assertEqual(connection["text"], "Wi-Fi 432 Mb/s")

    def test_a_wifi_link_with_no_rate_states_no_number(self):
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            kind="wifi", speed_mbps=None, duplex=None,
            wifi={"interface": "en1", "up": True, "rate_mbps": None})))
        self.assertEqual(connection["text"], "Wi-Fi")

    def test_ethernet_with_no_negotiated_media_shows_no_speed(self):
        """A cable out, or a forcing mismatch: not 0 Mb/s and not a silent gigabit."""
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            speed_mbps=None, duplex=None, media="autoselect (none)", link_up=False,
            reason="The Ethernet link has no negotiated media (the cable may be unplugged), "
                   "so its speed is not known.")))
        self.assertIsNone(connection["speed_mbps"])
        self.assertFalse(connection["slow"])
        self.assertEqual(connection["text"], "Ethernet · speed not reported")

    def test_wifi_being_up_beside_ethernet_travels(self):
        """An Ethernet Mac that has fallen back to Wi-Fi as well looks identical otherwise."""
        connection = fleet.machine_connection(_machine(connection=_hub_connection(
            wifi={"interface": "en1", "up": True, "rate_mbps": 866})))
        self.assertTrue(connection["wifi_up"])
        self.assertEqual(connection["wifi_rate_mbps"], 866)

    def test_an_unmeasured_block_keeps_its_reason_and_never_reads_as_a_link(self):
        connection = fleet.machine_connection(_machine(connection={
            "status": "unmeasured", "reason": "This Mac's routing table could not be read.",
            "interface": None, "kind": None, "speed_mbps": None, "duplex": None,
            "media": None, "autoselect": None, "link_up": None, "wifi": None, "sampled_at": None}))
        self.assertEqual(connection["status"], "unmeasured")
        self.assertEqual(connection["reason"], "This Mac's routing table could not be read.")
        self.assertEqual(connection["text"], "—")
        self.assertFalse(connection["slow"])
        self.assertIsNone(connection["speed_mbps"])

    def test_a_zero_speed_the_hub_stated_is_not_read_as_a_gigabit_link(self):
        """§6 in the other direction: a stated 0 is read, and it is slow — never absent."""
        connection = fleet.machine_connection(_machine(connection=_hub_connection(speed_mbps=0)))
        self.assertEqual(connection["speed_mbps"], 0)
        self.assertTrue(connection["slow"])


class OlderHubTests(unittest.TestCase):
    """A Hub that does not publish the block at all."""

    def test_an_older_hub_reports_none_rather_than_a_link(self):
        machine = {"machine": "1207", "label": "1207", "reachable": True}
        self.assertIsNone(fleet.machine_connection(machine))

    def test_an_empty_or_malformed_block_is_not_a_measurement(self):
        for value in ({}, None, "1000baseT", [], 1000):
            self.assertIsNone(fleet.machine_connection({"connection": value}), repr(value))

    def test_a_block_that_claims_a_measurement_but_names_nothing_is_not_drawn_as_a_link(self):
        """A producer bug must not read as a link of unknown type on the page."""
        connection = fleet.machine_connection({"connection": {"status": "measured"}})
        self.assertEqual(connection["status"], "unmeasured")
        self.assertEqual(connection["text"], "—")
        self.assertIsNone(connection["speed_mbps"])

    def test_the_machine_row_carries_none_and_the_page_draws_an_em_dash(self):
        flat = fleet.machines_flat([_row([{"machine": "1207", "label": "1207"}])])
        self.assertIn("connection", flat[0])
        self.assertIsNone(flat[0]["connection"])

    def test_a_machine_known_only_from_the_activity_list_has_no_link(self):
        row = {"id": "ctrl", "label": "Ctrl", "local": False, "reachable": True,
               "machines": [], "activity": {"machines": [{"machine": "m9", "state": "ready"}],
                                            "pulse": {}}, "nodes": []}
        flat = fleet.machines_flat([row])
        self.assertEqual([machine["machine"] for machine in flat], ["m9"])
        self.assertIsNone(flat[0]["connection"])


class MachineRowTests(unittest.TestCase):
    def test_the_normalised_link_reaches_the_row_the_page_draws(self):
        flat = fleet.machines_flat([_row([_machine()])])
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["connection"]["text"], "Ethernet 1000 Mb/s full")

    def test_two_macs_with_different_links_keep_their_own(self):
        flat = fleet.machines_flat([_row([
            _machine(machine="1301", label="1301",
                     connection=_hub_connection(speed_mbps=100,
                                                media="autoselect (100baseTX <full-duplex>)")),
            _machine(machine="1207", label="1207"),
        ])])
        by_id = {row["machine"]: row["connection"] for row in flat}
        self.assertTrue(by_id["1301"]["slow"])
        self.assertFalse(by_id["1207"]["slow"])


class SummaryTests(unittest.TestCase):
    """The fleet-wide line, which must agree with the rows the table draws."""

    def test_the_slow_macs_are_counted_and_named(self):
        summary = fleet._connection_summary(fleet.machines_flat([_row([
            _machine(machine="1301", label="1301", connection=_hub_connection(speed_mbps=100)),
            _machine(machine="1207", label="1207"),
            _machine(machine="1304", label="1304"),
        ])]))
        self.assertEqual(summary["slow_ethernet"], 1)
        self.assertEqual(summary["slow_machines"], ["1301"])
        self.assertEqual(summary["ethernet"], 3)
        self.assertEqual(summary["unmeasured"], 0)
        self.assertEqual(summary["total"], 3)

    def test_a_wifi_mac_is_counted_as_wifi_not_as_slow_ethernet(self):
        summary = fleet._connection_summary(fleet.machines_flat([_row([
            _machine(connection=_hub_connection(kind="wifi", speed_mbps=None, duplex=None,
                                                wifi={"interface": "en1", "up": True,
                                                      "rate_mbps": 432}))])]))
        self.assertEqual(summary["slow_ethernet"], 0)
        self.assertEqual(summary["wifi"], 1)
        self.assertEqual(summary["ethernet"], 0)

    def test_a_mac_with_no_block_is_unmeasured_not_healthy(self):
        summary = fleet._connection_summary(fleet.machines_flat([_row([
            {"machine": "1207", "label": "1207"},
            _machine(machine="1301", label="1301", connection=_hub_connection(speed_mbps=100)),
        ])]))
        self.assertEqual(summary["unmeasured"], 1)
        self.assertEqual(summary["slow_ethernet"], 1)
        self.assertEqual(summary["total"], 2)

    def test_an_unreadable_block_counts_as_unmeasured(self):
        summary = fleet._connection_summary(fleet.machines_flat([_row([
            _machine(connection={"status": "unmeasured", "reason": "timed out"})])]))
        self.assertEqual(summary["unmeasured"], 1)
        self.assertEqual(summary["slow_ethernet"], 0)

    def test_the_fleet_summary_publishes_the_count(self):
        summary = fleet._summarise([_row([_machine(connection=_hub_connection(speed_mbps=100))])])
        self.assertEqual(summary["connections"]["slow_ethernet"], 1)
        self.assertEqual(summary["connections"]["slow_machines"], ["1301"])

    def test_an_empty_fleet_publishes_zero_of_everything_not_none(self):
        summary = fleet._summarise([])
        self.assertEqual(summary["connections"], {
            "slow_ethernet": 0, "slow_machines": [], "wifi": 0, "wifi_machines": [],
            "ethernet": 0, "unmeasured": 0, "total": 0})


if __name__ == "__main__":
    unittest.main()
