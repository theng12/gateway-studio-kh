#!/usr/bin/env python3
"""Fleet reachability history, and the likely cause a reader can actually check.

The owner, 2026-10-29: *"there's no enough detail for me to sort through, i want to be able to sort
for the offline site. and also 'problematic' machine, that goes in and out over a period of time or
sth. like a bad pattern. and also a way for me to see which ones could be the likely cause or sth
too."*

What this file pins, in the order it matters:

* a transition is recorded **once**, from the fan-out the Gateway already does, and survives a
  restart (`observe` → file → a fresh process);
* the store is **bounded** — old events go, and the caps hold;
* *problematic* is the owner's **threshold**, not a constant: the same history reads both ways when
  the number changes;
* a **site outage is never its members flapping**: one dead controller must not produce five
  problematic Macs;
* every cause **cites its evidence**, and where nothing supports one the answer is "No evidence
  yet" — never a plausible-sounding guess;
* a figure nobody measured is `None`, never `0` (`"no data" is not zero`).

Run with::

    .venv/bin/python -m unittest tests.test_fleet_history
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-fleet-history-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import fleet_history  # noqa: E402

#: A fixed instant, so a test never depends on when it runs.
T0 = 1_760_000_000.0


def local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """`time.mktime` on the reader's own timezone: the pattern hint reads local clock time."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def machine(machine_id: str = "m1", up: bool = True, **extra) -> dict:
    row = {"machine": machine_id, "label": machine_id, "reachable": up,
           "location_id": "L1", "location": "Blue Room", "location_reachable": True}
    row.update(extra)
    return row


def location(up: bool = True, **extra) -> dict:
    row = {"id": "L1", "label": "Blue Room", "reachable": up, "error": None, "error_kind": None}
    row.update(extra)
    return row


class HistoryCase(unittest.TestCase):
    """A fresh store and settings file per test, and a clock the test drives."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="history-", dir=_TEMP_ROOT))
        self.history_path = self.work / "fleet_history.json"
        self.settings_path = self.work / "fleet_health.json"
        self._old_path, self._old_settings, self._old_clock = (
            fleet_history.HISTORY_PATH, fleet_history.SETTINGS_PATH, fleet_history._clock)
        fleet_history.HISTORY_PATH = self.history_path
        fleet_history.SETTINGS_PATH = self.settings_path
        self.now = T0
        fleet_history._clock = lambda: self.now
        fleet_history.reset_for_tests()
        self.addCleanup(self.restore)

    def restore(self) -> None:
        fleet_history.HISTORY_PATH = self._old_path
        fleet_history.SETTINGS_PATH = self._old_settings
        fleet_history._clock = self._old_clock
        fleet_history.reset_for_tests()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def observe(self, machines: list[dict], sites: list[dict] | None = None) -> None:
        fleet_history.observe(sites if sites is not None else [location()], machines)


class ATranstionIsRecordedOnce(HistoryCase):
    def test_the_first_observation_is_not_a_drop(self) -> None:
        self.observe([machine("m1", up=False)])
        history = fleet_history.summary("m:L1/m1")
        self.assertTrue(history["observed"])
        self.assertEqual(history["drops_day"], 0)
        self.assertEqual(history["flips_day"], 0)
        self.assertFalse(history["online"])
        self.assertEqual(history["since"], T0)

    def test_a_drop_and_a_recovery_are_recorded_with_the_reason(self) -> None:
        self.observe([machine()])
        self.advance(600)
        self.observe([machine(up=False, reach_reason="no_hub")])
        self.advance(600)
        self.observe([machine()])
        history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["drops_day"], 1)
        self.assertEqual(history["flips_day"], 2)
        self.assertEqual(history["offline_seconds_day"], 600)
        self.assertEqual(history["last_drop"]["reason"], "no_hub")
        self.assertIn("Studio Hub is not listening", history["last_drop"]["text"])

    def test_an_unchanged_state_writes_nothing(self) -> None:
        self.observe([machine()])
        written = self.history_path.read_text(encoding="utf-8")
        stamp = self.history_path.stat().st_mtime_ns
        for _ in range(5):
            self.advance(15)
            self.observe([machine()])
        self.assertEqual(self.history_path.stat().st_mtime_ns, stamp,
                         "an unchanged fleet rewrote the history file")
        self.assertEqual(self.history_path.read_text(encoding="utf-8"), written)

    def test_an_unknown_machine_state_is_not_a_state(self) -> None:
        # `reachable: None` means the Hub said nothing about this Mac. That is not "offline".
        self.observe([machine()])
        self.advance(60)
        self.observe([{**machine(), "reachable": None}])
        history = fleet_history.summary("m:L1/m1")
        self.assertTrue(history["online"])
        self.assertEqual(history["drops_day"], 0)


class ADropHasALengthAndARecoveryCause(HistoryCase):
    """How long a drop lasted, and whether it came back by itself (owner, 2026-10-30).

    A count alone cannot tell a Mac that dropped once for two minutes from one that drops every
    eight minutes. Every figure here is derived from the recorded transitions; a length nobody
    watched end is `None`, never a short one.
    """

    def drop_once(self, *, away: float, back: bool = True) -> None:
        self.observe([machine()])
        self.advance(60)
        self.observe([machine(up=False, reach_reason="unreachable")])
        self.advance(away)
        if back:
            self.observe([machine()])

    def test_a_completed_drop_has_its_length(self) -> None:
        self.drop_once(away=120)
        history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["last_drop"]["duration_seconds"], 120)
        self.assertEqual(history["drop_median_seconds"], 120)
        self.assertEqual(history["drop_longest_seconds"], 120)
        self.assertEqual(history["drops_completed"], 1)
        # One drop has no gap between drops — `None`, not zero.
        self.assertIsNone(history["drop_gap_seconds"])

    def test_a_recovery_with_no_startup_action_is_by_itself(self) -> None:
        with mock.patch.object(fleet_history, "_startup_events", return_value=[]):
            self.drop_once(away=60)
            history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["last_drop"]["recovered_by"], "by_itself")

    def test_a_recovery_after_a_restart_request_names_it(self) -> None:
        # The audit proves a restart was asked for two minutes before the Mac answered again.
        events = [{"at": self.now - 120, "event": "requested", "controller": "L1",
                   "machine": "m1", "action": "restart_services"}]
        with mock.patch.object(fleet_history, "_startup_events", return_value=events):
            self.drop_once(away=180)
            history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["last_drop"]["recovered_by"], "after_startup_action")

    def test_an_action_for_another_machine_is_not_this_one(self) -> None:
        events = [{"at": self.now - 60, "event": "requested", "controller": "L1",
                   "machine": "other", "action": "restart_services"}]
        with mock.patch.object(fleet_history, "_startup_events", return_value=events):
            self.drop_once(away=60)
            history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["last_drop"]["recovered_by"], "by_itself")

    def test_a_drop_still_running_has_no_length(self) -> None:
        self.drop_once(away=120, back=False)
        history = fleet_history.summary("m:L1/m1")
        self.assertIsNone(history["last_drop"]["duration_seconds"])
        self.assertTrue(history["last_drop"]["ongoing"])
        self.assertEqual(history["last_drop"]["recovered_by"], "unknown")
        self.assertIsNone(history["drop_longest_seconds"])

    def test_median_longest_and_the_gap_between_drops(self) -> None:
        # Three drops, each lasting a different time, with a different wait before each.
        self.observe([machine()])
        for away, gap_before in ((100, 60), (300, 600), (200, 1200)):
            self.advance(gap_before)
            self.observe([machine(up=False, reach_reason="unreachable")])
            self.advance(away)
            self.observe([machine()])
        history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["drops_completed"], 3)
        self.assertEqual(history["drop_median_seconds"], 200)
        self.assertEqual(history["drop_longest_seconds"], 300)
        self.assertIsNotNone(history["drop_gap_seconds"])

    def test_the_problematic_basis_says_what_was_counted(self) -> None:
        snapshot = {"locations": [location()], "machines": [machine()]}
        self.observe([machine()], [location()])
        for _ in range(5):
            self.advance(60)
            self.observe([machine(up=False, reach_reason="unreachable")], [location()])
            self.advance(120)
            self.observe([machine()], [location()])
        fleet_history.annotate(snapshot)
        history = snapshot["machines"][0]["history"]
        self.assertTrue(history["problematic"])
        basis = history["problematic_basis"]
        self.assertIn("5 drops in 24 h", basis)
        self.assertIn("threshold 4", basis)
        self.assertIn("longest 2 min", basis)
        self.assertIn("median 2 min", basis)
        # The count is a 24 h figure and the lengths are the last 7 days' drops; each number says
        # which window it belongs to.
        self.assertIn("in 7 days", basis)

    def test_the_startup_audit_is_read_once_per_fleet_read(self) -> None:
        # Only the last drop's recovery cause is published, so the 500-row audit is read once
        # rather than once per recorded drop.
        calls = []

        def audit():
            calls.append(1)
            return []

        with mock.patch.object(fleet_history, "_startup_events", side_effect=audit):
            self.observe([machine()])
            for _ in range(3):
                self.advance(600)
                self.observe([machine(up=False, reach_reason="unreachable")])
                self.advance(120)
                self.observe([machine()])
            history = fleet_history.summary("m:L1/m1")
        self.assertEqual(history["drops_completed"], 3)
        self.assertEqual(len(calls), 1)

    def test_an_unmeasured_length_says_so_and_is_not_zero(self) -> None:
        snapshot = {"locations": [location()], "machines": [machine()]}
        self.observe([machine()], [location()])
        self.advance(60)
        self.observe([machine(up=False, reach_reason="unreachable")], [location()])
        fleet_history.annotate(snapshot)
        basis = snapshot["machines"][0]["history"]["problematic_basis"]
        self.assertIn("length not measured", basis)

    def test_every_window_is_named_in_the_read(self) -> None:
        snapshot = {"locations": [location()], "machines": [machine()]}
        self.observe([machine()], [location()])
        fleet_history.annotate(snapshot)
        windows = snapshot["history_windows"]
        self.assertEqual(windows["offline_seconds_day"], "last 24 h")
        self.assertEqual(windows["offline_seconds_week"], "last 7 days")


class TheStoreIsBounded(HistoryCase):
    def test_the_history_survives_a_restart(self) -> None:
        self.observe([machine()])
        self.advance(600)
        self.observe([machine(up=False, reach_reason="unreachable")])
        self.advance(600)
        self.observe([machine()])
        before = fleet_history.summary("m:L1/m1")
        # A new process: the module's memory is gone, the file is not.
        fleet_history.reset_for_tests()
        after = fleet_history.summary("m:L1/m1")
        self.assertEqual(after["drops_day"], before["drops_day"])
        self.assertEqual(after["flips_day"], before["flips_day"])
        self.assertEqual(after["offline_seconds_day"], before["offline_seconds_day"])
        self.assertTrue(after["online"])

    def test_events_older_than_the_window_are_pruned(self) -> None:
        self.observe([machine()])
        for _ in range(3):
            self.advance(600)
            self.observe([machine(up=False, reach_reason="unreachable")])
            self.advance(600)
            self.observe([machine()])
        self.assertEqual(fleet_history.summary("m:L1/m1")["drops_week"], 3)
        # Eight days later a single new transition is all that is left.
        self.advance(8 * 86400)
        self.observe([machine(up=False, reach_reason="unreachable")])
        stored = json.loads(self.history_path.read_text(encoding="utf-8"))
        events = stored["subjects"]["m:L1/m1"]["events"]
        self.assertLessEqual(len(events), 2, f"old events survived the window: {events}")
        self.assertEqual(fleet_history.summary("m:L1/m1")["drops_week"], 1)

    def test_each_subject_keeps_a_bounded_number_of_events(self) -> None:
        self.observe([machine()])
        for index in range(2 * fleet_history.MAX_EVENTS_PER_SUBJECT + 40):
            self.advance(30)
            self.observe([machine(up=index % 2 == 1)])
        stored = json.loads(self.history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["subjects"]["m:L1/m1"]["events"]),
                         fleet_history.MAX_EVENTS_PER_SUBJECT)

    def test_the_number_of_subjects_is_capped(self) -> None:
        original = fleet_history.MAX_SUBJECTS
        fleet_history.MAX_SUBJECTS = 3
        try:
            for index in range(6):
                self.advance(1)
                self.observe([machine(f"m{index}")])
            stored = json.loads(self.history_path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["subjects"]), 3)
            # The newest are kept: an old Mac is less interesting than the one just seen.
            self.assertIn("m:L1/m5", stored["subjects"])
        finally:
            fleet_history.MAX_SUBJECTS = original


class ProblematicIsTheOwnersThreshold(HistoryCase):
    def flapping_machine(self, drops: int) -> None:
        self.observe([machine()])
        for _ in range(drops):
            self.advance(300)
            self.observe([machine(up=False, reach_reason="unreachable")])
            self.advance(300)
            self.observe([machine()])
        self.advance(30)

    def verdict(self, drops: int) -> bool | None:
        snapshot = {"locations": [location()], "machines": [machine()], "summary": {}}
        fleet_history.annotate(snapshot)
        return snapshot["machines"][0]["history"]["problematic"]

    def test_the_default_is_four_drops_in_a_day(self) -> None:
        self.assertEqual(fleet_history.settings()["flapping_drops_day"], 4)
        self.flapping_machine(3)
        self.assertIs(self.verdict(3), False)
        self.flapping_machine(1)
        self.assertIs(self.verdict(4), True)

    def test_the_threshold_can_be_changed_and_shows_on_the_next_read(self) -> None:
        self.flapping_machine(4)
        self.assertIs(self.verdict(4), True)
        fleet_history.set_settings({"flapping_drops_day": 10})
        self.assertIs(self.verdict(4), False, "the new threshold did not reach the read")
        fleet_history.set_settings({"flapping_drops_day": 2})
        self.assertIs(self.verdict(4), True)
        self.assertEqual(fleet_history.settings()["default"], 4)

    def test_a_threshold_outside_the_range_is_refused_not_clamped(self) -> None:
        for value in (0, -3, 10_000):
            with self.assertRaises(ValueError):
                fleet_history.set_settings({"flapping_drops_day": value})
        with self.assertRaises(ValueError):
            fleet_history.set_settings({"flapping_drops_day": "4"})
        with self.assertRaises(ValueError):
            fleet_history.set_settings({"flapping_drops_day": True})
        self.assertEqual(fleet_history.settings()["flapping_drops_day"], 4)

    def test_a_never_observed_machine_has_no_verdict(self) -> None:
        # "Not checked" must not read as "fine".
        snapshot = {"locations": [], "machines": [machine("ghost")], "summary": {}}
        fleet_history.annotate(snapshot)
        history = snapshot["machines"][0]["history"]
        self.assertIsNone(history["problematic"])
        self.assertIsNone(history["drops_day"])
        self.assertIsNone(history["offline_seconds_day"])
        self.assertTrue(all(day["offline_fraction"] is None for day in history["days"]))


class ASiteOutageIsNotItsMembersFlapping(HistoryCase):
    def test_one_dead_controller_does_not_make_five_problematic_macs(self) -> None:
        members = [machine(f"m{index}") for index in range(5)]
        self.observe(members)
        failing = location(up=False, error_kind="timeout", error="No answer within 6s.")
        down = [{**member, "reachable": False, "location_reachable": False} for member in members]
        # Five outages of the whole site: every member reads unreachable each time, because the
        # controller cannot be reached. That is the site's fault, not each Mac's.
        for _ in range(5):
            self.advance(600)
            self.observe(down, [failing])
            self.advance(600)
            self.observe(members)
        # And the site is down now, which is the read a person actually has in front of them.
        self.advance(600)
        self.observe(down, [failing])
        self.advance(30)
        snapshot = {"locations": [failing], "machines": down, "summary": {}}
        fleet_history.annotate(snapshot)
        for row in snapshot["machines"]:
            history = row["history"]
            self.assertEqual(history["drops_day"], 0, f"{row['machine']} blamed for the site")
            self.assertIs(history["problematic"], False)
            # The gap is still drawn — the Mac really was unreachable — it is just not its fault.
            self.assertGreater(history["offline_seconds_day"], 0)
            codes = [cause["code"] for cause in row["causes"]]
            self.assertIn("site_down", codes)
        site = snapshot["locations"][0]
        self.assertIs(site["history"]["problematic"], True, "the site itself is the flapping thing")
        self.assertEqual(site["history"]["drops_day"], 6)

    def test_the_site_cause_blames_the_controller_and_quotes_it(self) -> None:
        self.observe([machine("m1")])
        self.advance(600)
        failing = location(up=False, error_kind="refused",
                           error="Nothing is listening on that address.")
        self.observe([{**machine("m1"), "reachable": False, "location_reachable": False}], [failing])
        snapshot = {"locations": [failing], "machines": [{**machine("m1"), "reachable": False,
                                                         "location_reachable": False}],
                    "summary": {}}
        fleet_history.annotate(snapshot)
        cause = snapshot["machines"][0]["causes"][0]
        self.assertEqual(cause["code"], "site_down")
        self.assertIn("Blue Room", cause["text"])
        self.assertIn("controller, not the Mac", cause["text"])
        self.assertIn("Nothing is listening", cause["evidence"])
        site_cause = snapshot["locations"][0]["causes"][0]
        self.assertEqual(site_cause["code"], "controller_refused")
        self.assertIn("listening", site_cause["evidence"])


class ACauseCitesItsEvidence(HistoryCase):
    def causes(self, row: dict, sites: list[dict] | None = None) -> list[dict]:
        snapshot = {"locations": sites or [location()], "machines": [row], "summary": {}}
        fleet_history.annotate(snapshot)
        return snapshot["machines"][0]["causes"]

    def test_no_hub_says_the_mac_answered(self) -> None:
        cause = self.causes(machine(up=False, reach_reason="no_hub", node_detail="(no_hub)"))[0]
        self.assertEqual(cause["code"], "hub_not_running")
        self.assertIn("not running", cause["text"])
        self.assertIn("no_hub", cause["evidence"])

    def test_unreachable_says_asleep_or_off_and_how_long(self) -> None:
        self.now = T0
        cause = self.causes(machine(up=False, reach_reason="unreachable",
                                    node_last_seen_at=T0 - 7200))[0]
        self.assertEqual(cause["code"], "asleep_or_off")
        self.assertIn("2 h ago", cause["evidence"])
        self.assertIn("its controller", cause["text"])
        self.assertIn("frozen", cause["text"])
        self.assertIn("permission prompt", cause["text"])
        self.assertEqual(cause["severity"], "bad")

    def test_unauthorized_is_a_credential_not_a_network(self) -> None:
        cause = self.causes(machine(up=False, node_state="unauthorized",
                                    node_detail="Hub token rejected"))[0]
        self.assertEqual(cause["code"], "unauthorized")
        self.assertIn("credential", cause["text"])
        self.assertIn("Hub token rejected", cause["evidence"])

    def test_never_connected_names_the_address(self) -> None:
        cause = self.causes(machine(up=False, node_never_connected=True,
                                    node_last_seen_at=T0 - 3 * 86400,
                                    node_address="100.82.91.7"))[0]
        self.assertEqual(cause["code"], "never_connected")
        self.assertIn("100.82.91.7", cause["evidence"])

    def test_a_slow_link_is_named_with_its_speed(self) -> None:
        cause = self.causes(machine(connection={
            "status": "measured", "kind": "ethernet", "speed_mbps": 100, "duplex": "full",
            "slow": True, "text": "Ethernet 100 Mb/s full"}))[0]
        self.assertEqual(cause["code"], "slow_link")
        self.assertIn("100 Mb/s", cause["text"])
        self.assertIn("Ethernet 100 Mb/s full", cause["evidence"])

    def test_wifi_is_named_when_it_drops(self) -> None:
        self.observe([machine()])
        self.advance(300)
        self.observe([machine(up=False, reach_reason="unreachable")])
        self.advance(300)
        self.observe([machine()])
        self.advance(30)
        cause = self.causes(machine(connection={
            "status": "measured", "kind": "wifi", "speed_mbps": None, "wifi_rate_mbps": 432,
            "slow": False, "text": "Wi-Fi 432 Mb/s"}))[0]
        self.assertEqual(cause["code"], "on_wifi")
        self.assertIn("Wi-Fi 432 Mb/s", cause["evidence"])
        self.assertIn("1 time in the last 24 h", cause["evidence"])

    def test_startup_flags_are_quoted_from_the_macs_own_report(self) -> None:
        causes = self.causes(machine(up=False, reach_reason="no_hub", startup={
            "start_services_on_launch": False, "open_at_login": False}))
        codes = [cause["code"] for cause in causes]
        self.assertIn("services_not_starting", codes)
        self.assertIn("not_open_at_login", codes)
        for cause in causes:
            if cause["code"] in {"services_not_starting", "not_open_at_login"}:
                self.assertIn("startup report", cause["evidence"])

    def test_studios_down_while_the_hub_is_up(self) -> None:
        cause = self.causes(machine(startup={
            "services_running": {"up": 2, "total": 5, "studios": {}}}))[0]
        self.assertEqual(cause["code"], "studios_down")
        self.assertIn("2 of 5", cause["evidence"])

    def test_a_hub_behind_names_both_versions(self) -> None:
        snapshot = {"locations": [location()],
                    "machines": [machine(up=False, reach_reason="unreachable",
                                         version_status="behind", hub_version="0.6.190")],
                    "summary": {"hub_latest": {"version": "0.6.206"}}}
        fleet_history.annotate(snapshot)
        cause = next(c for c in snapshot["machines"][0]["causes"] if c["code"] == "hub_behind")
        self.assertIn("0.6.190", cause["evidence"])
        self.assertIn("0.6.206", cause["evidence"])

    def test_a_nightly_pattern_is_named_with_its_window(self) -> None:
        night = [local_epoch(2026, 10, 20, 23, 5), local_epoch(2026, 10, 21, 23, 12),
                 local_epoch(2026, 10, 22, 22, 58), local_epoch(2026, 10, 23, 23, 20)]
        self.observe([machine()])
        for at in night:
            self.now = at
            self.observe([machine(up=False, reach_reason="unreachable")])
            self.now = at + 3600
            self.observe([machine()])
        self.now += 60
        snapshot = {"locations": [location()], "machines": [machine()], "summary": {}}
        fleet_history.annotate(snapshot)
        pattern = snapshot["machines"][0]["history"]["pattern"]
        self.assertIsNotNone(pattern, "four drops at the same time of night is a pattern")
        self.assertEqual(pattern["code"], "nightly_drops")
        self.assertIn("22:", pattern["text"] + pattern["evidence"])
        self.assertIn("4 of 4", pattern["evidence"])

    def test_no_evidence_yet_rather_than_a_guess(self) -> None:
        causes = self.causes(machine(up=False))
        self.assertEqual([cause["code"] for cause in causes], ["no_evidence"])
        self.assertEqual(causes[0]["text"], "No evidence yet")

    def test_a_healthy_mac_does_not_read_as_a_mystery(self) -> None:
        causes = self.causes(machine())
        self.assertEqual([cause["code"] for cause in causes], ["fine"])

    def test_a_mac_its_hub_flagged_never_says_nothing_wrong(self) -> None:
        # Seen live on terranash-1403, 2026-10-30: the row said "Needs attention — X studio not
        # running" in amber beside "Likely cause: Nothing wrong reported". The Hub's own sentence
        # is the smallest truthful answer for a verdict this Gateway cannot see into; it is quoted
        # rather than restated as a finding of its own. (A reason this Gateway *can* name now gets
        # its own cause instead — see `AttentionReasonsNameTheirOwnCause`.)
        causes = self.causes(machine(state="needs_attention",
                                     attention="video studio not running"))
        self.assertEqual([cause["code"] for cause in causes], ["hub_flagged"])
        self.assertNotEqual(causes[0]["text"], "Nothing wrong reported")
        self.assertIn("video studio not running", causes[0]["evidence"])
        self.assertEqual(causes[0]["severity"], "warn")

    def test_a_cause_that_explains_the_attention_still_wins(self) -> None:
        causes = self.causes(machine(state="needs_attention",
                                     attention="video studio not running",
                                     startup={"services_running": {"up": 1, "total": 3}}))
        self.assertEqual(causes[0]["code"], "studios_down")


class AttentionReasonsNameTheirOwnCause(HistoryCase):
    """A studio this Gateway watched go down is not "the Hub reports a problem it cannot see".

    `fleet._attention` names every reason it stands up with a code, and the Likely-cause cell draws
    a plain sentence from that code with the reason as its evidence. Five of these are this
    Gateway's own read of the Mac; only a reason with no code of its own — the Hub's verdict alone,
    an older Gateway's cached row, or a code this build does not know — keeps the Hub's wording.
    """

    #: (attention_code, the `attention` sentence, the cause text, the cause evidence)
    REASONS = [
        ("studio_down", "Voice studio not running",
         "One of its studios is not running, so this Mac cannot take that kind of work.",
         "Voice studio not running"),
        ("studio_not_ready", "Voice studio not ready",
         "One of its studios is up but not ready, so it cannot take work yet.",
         "Voice studio not ready"),
        ("job_stalled", "No progress for 12 min",
         "Its current job has stopped making progress.", "No progress for 12 min"),
        ("health_word", "Health: degraded",
         "Its Studio Hub reports its own health as something other than up.", "Health: degraded"),
        ("recent_job_failed", "Last job failed: HTTP 500",
         "Its most recent job failed.", "Last job failed: HTTP 500"),
        ("no_model_ready", "No model is ready on this Mac, so it is never given a job",
         "No model is ready on this Mac, so routing never gives it work.",
         "No model is ready on this Mac, so it is never given a job"),
        ("hub_flagged", "Flagged by its Hub",
         "Its Hub reports a problem this Gateway cannot see from outside.",
         "Its Hub says: Flagged by its Hub"),
    ]

    def causes(self, row: dict) -> list[dict]:
        snapshot = {"locations": [location()], "machines": [row], "summary": {}}
        fleet_history.annotate(snapshot)
        return snapshot["machines"][0]["causes"]

    def test_one_plain_sentence_per_code(self) -> None:
        for code, why, text, evidence in self.REASONS:
            with self.subTest(code=code):
                causes = self.causes(machine(state="needs_attention", attention=why,
                                             attention_code=code))
                self.assertEqual([cause["code"] for cause in causes], [code])
                self.assertEqual(causes[0]["text"], text)
                self.assertEqual(causes[0]["evidence"], evidence)
                self.assertEqual(causes[0]["severity"], "warn")

    def test_an_older_row_with_a_reason_but_no_code_keeps_the_hub_sentence(self) -> None:
        causes = self.causes(machine(state="needs_attention", attention="Voice studio not running"))
        self.assertEqual([cause["code"] for cause in causes], ["hub_flagged"])
        self.assertNotEqual(causes[0]["text"], "Nothing wrong reported")
        self.assertIn("Voice studio not running", causes[0]["evidence"])

    def test_needs_attention_with_no_reason_at_all_is_still_not_nothing_wrong(self) -> None:
        causes = self.causes(machine(state="needs_attention"))
        self.assertEqual([cause["code"] for cause in causes], ["hub_flagged"])
        self.assertNotEqual(causes[0]["text"], "Nothing wrong reported")

    def test_a_code_this_build_does_not_know_degrades_to_the_hub_sentence(self) -> None:
        causes = self.causes(machine(state="needs_attention", attention="Something newer",
                                     attention_code="something_new"))
        self.assertEqual([cause["code"] for cause in causes], ["hub_flagged"])
        self.assertIn("Something newer", causes[0]["evidence"])


class EveryCauseSaysHowBadItIs(HistoryCase):
    """The owner, 2026-10-30: red is broken or down, yellow is degraded but running, blue is fine.

    The severity lives beside the code in `fleet_history.py` so the page reads it instead of
    keeping a second list of cause codes in JavaScript. An unknown or missing severity is neutral
    (never red), and a new code cannot be added without stating one.
    """

    #: Code → the only severity it may carry. A new code must be added here, which is the point:
    #: it forces a conscious colour at the same time as the code.
    EXPECTED = {
        "no_evidence": "none", "fine": "none",
        "site_down": "bad", "unauthorized": "bad", "never_connected": "bad",
        "hub_not_running": "bad", "asleep_or_off": "bad", "probe_failed": "bad",
        "controller_refused": "bad", "controller_silent": "bad",
        "controller_disconnected": "bad", "controller_error": "bad",
        "local_hub_down": "bad", "controller_failed": "bad",
        "services_not_starting": "warn", "not_open_at_login": "warn",
        "studios_down": "warn", "studios_missing": "warn", "slow_link": "warn",
        "hub_behind": "warn", "flapping": "warn", "nightly_drops": "warn",
        "studio_down": "warn", "studio_not_ready": "warn", "job_stalled": "warn",
        "health_word": "warn", "recent_job_failed": "warn", "hub_flagged": "warn",
        "no_model_ready": "warn",
        "on_wifi": "info",
    }

    def severities(self, causes: list[dict]) -> dict[str, str]:
        return {cause["code"]: cause["severity"] for cause in causes}

    #: Which codes each builder can produce, so the two tests together cover every one.
    SITE_CODES = {"controller_refused", "controller_silent", "controller_disconnected",
                  "controller_error", "local_hub_down", "controller_failed",
                  "flapping", "fine", "no_evidence"}
    MACHINE_CODES = set(EXPECTED) - (SITE_CODES - {"flapping", "fine", "no_evidence"})

    def test_every_machine_cause_code_and_its_severity(self) -> None:
        site = {"id": "L1", "label": "Blue Room", "reachable": False,
                "error": "no answer", "members_from": T0 - 60}
        wifi = {"status": "measured", "kind": "wifi", "speed_mbps": None, "slow": False,
                "text": "Wi-Fi 432 Mb/s"}
        slow = {"status": "measured", "kind": "ethernet", "speed_mbps": 100, "duplex": "full",
                "slow": True, "text": "Ethernet 100 Mb/s full"}
        seen: dict[str, str] = {}
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, location_reachable=False), {}, site, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, node_state="unauthorized", node_detail="token refused"), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, node_never_connected=True), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, reach_reason="no_hub"), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, reach_reason="unreachable"), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, node_detail="the controller's own sentence"), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, reach_reason="no_hub", startup={
                "start_services_on_launch": False, "open_at_login": False}), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(startup={"services_running": {"up": 2, "total": 5}}), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(not_installed_studios=["Music"]), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(connection=slow), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, reach_reason="no_hub", connection=wifi), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(up=False, reach_reason="unreachable", version_status="behind",
                    hub_version="0.6.190"), {}, None, 4, {"version": "0.6.206"}))
        seen |= self.severities(fleet_history.machine_causes(
            machine(), {"observed": True, "problematic": True, "drops_day": 5,
                        "drops_week": 20, "problematic_basis": "5 drops in 24 h"}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(
            machine(), {"observed": True, "pattern": {
                "code": "nightly_drops", "severity": "warn", "text": "nightly", "evidence": "4 of 4"}},
            None, 4))
        seen |= self.severities(fleet_history.machine_causes(machine(), {}, None, 4))
        # Every attention reason `fleet._attention` can stand up on a row.
        for code, why, _, _ in AttentionReasonsNameTheirOwnCause.REASONS:
            seen |= self.severities(fleet_history.machine_causes(
                machine(state="needs_attention", attention=why, attention_code=code), {}, None, 4))
        seen |= self.severities(fleet_history.machine_causes(machine(up=False), {}, None, 4))

        self.assertEqual(seen, {code: self.EXPECTED[code] for code in seen},
                         f"unexpected code/severity: {seen}")
        self.assertEqual(set(seen), self.MACHINE_CODES,
                         "every cause code machine_causes can build is classified here")

    def test_every_site_cause_code_and_its_severity(self) -> None:
        seen: dict[str, str] = {}
        for kind in ("refused", "timeout", "server_disconnected", "http", "local_hub_down"):
            seen |= self.severities(fleet_history.site_causes(
                location(up=False, error_kind=kind, error="the probe's sentence"), {}, 4))
        seen |= self.severities(fleet_history.site_causes(
            location(up=False, error="the probe's sentence"), {}, 4))
        seen |= self.severities(fleet_history.site_causes(
            location(up=False), {}, 4))
        seen |= self.severities(fleet_history.site_causes(
            location(), {"observed": True, "problematic": True, "drops_day": 5,
                         "drops_week": 20}, 4))
        seen |= self.severities(fleet_history.site_causes(location(), {}, 4))

        self.assertEqual(seen, {code: self.EXPECTED[code] for code in seen},
                         f"unexpected code/severity: {seen}")
        self.assertEqual(set(seen), self.SITE_CODES,
                         "every cause code site_causes can build is classified here")

    def test_a_cause_cannot_be_built_without_a_known_severity(self) -> None:
        # A new code with no severity is a TypeError, not a silently red line on the page.
        with self.assertRaises(TypeError):
            fleet_history._cause("a_new_code", "text", None)
        with self.assertRaises(ValueError):
            fleet_history._cause("a_new_code", "text", None, severity="critical")
        with self.assertRaises(ValueError):
            fleet_history._cause("a_new_code", "text", None, severity="")

    def test_an_unclassified_pattern_is_neutral_never_red(self) -> None:
        causes = fleet_history.machine_causes(
            machine(), {"observed": True, "pattern": {"code": "mystery", "text": "a pattern"}},
            None, 4)
        self.assertEqual(causes[0]["severity"], "none")

    def test_every_cause_call_site_states_its_severity(self) -> None:
        # Source-level, so a code added later fails here even before a test scenario reaches it.
        import ast
        tree = ast.parse(Path(fleet_history.__file__).read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "_cause"]
        self.assertGreaterEqual(len(calls), 20)
        for call in calls:
            stated = {keyword.arg: keyword.value for keyword in call.keywords}
            self.assertIn("severity", stated, f"line {call.lineno}: {ast.unparse(call)[:90]}")
            value = stated["severity"]
            # A literal is checked here; the pattern pass-through is normalised at runtime and
            # `_cause` refuses anything outside CAUSE_SEVERITIES.
            if isinstance(value, ast.Constant):
                self.assertIn(value.value, fleet_history.CAUSE_SEVERITIES,
                              f"line {call.lineno}: {value.value!r}")


class TheSnapshotCarriesIt(HistoryCase):
    def test_annotate_publishes_history_causes_and_the_threshold(self) -> None:
        self.observe([machine()])
        snapshot = {"locations": [location()],
                    "machines": [machine(), machine("m2", up=False, reach_reason="no_hub")],
                    "summary": {}}
        returned = fleet_history.annotate(snapshot)
        self.assertIs(returned, snapshot)
        self.assertEqual(snapshot["fleet_health"]["flapping_drops_day"], 4)
        for row in snapshot["machines"]:
            self.assertIn("history", row)
            self.assertIn("causes", row)
            self.assertIn("days", row["history"])
            self.assertEqual(len(row["history"]["days"]), fleet_history.WINDOW_DAYS)
        for row in snapshot["locations"]:
            self.assertIn("history", row)
            self.assertIn("causes", row)

    def test_the_route_publishes_it_and_does_not_poison_the_cache(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import admin, api, fleet

        cached = {"locations": [location()],
                  "machines": [machine(up=False, reach_reason="no_hub")],
                  "running": [], "summary": {}, "issues": [], "generated_at": T0}
        self.observe([machine()])
        self.advance(60)
        self.observe([machine(up=False, reach_reason="no_hub")])
        with mock.patch.object(fleet, "snapshot", lambda **_: cached), \
                mock.patch.object(admin.release_rollout, "status", lambda *a, **k: {}), \
                mock.patch.object(admin.model_share, "stamp_networks", lambda *a, **k: None), \
                mock.patch.object(admin.store, "stats", lambda: {"by_state": {}, "background": 0}):
            client = TestClient(api.app, client=("127.0.0.1", 1234))
            answer = client.get("/admin/api/fleet")
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertIn("fleet_health", body)
        row = body["machines"][0]
        self.assertEqual(row["history"]["drops_day"], 1)
        self.assertEqual(row["causes"][0]["code"], "hub_not_running")
        # **The cached snapshot the router reads must not gain these.**
        self.assertNotIn("history", cached["machines"][0])
        self.assertNotIn("causes", cached["machines"][0])
        self.assertNotIn("fleet_health", cached)

    def test_the_threshold_route_reads_and_writes_and_refuses_a_bad_number(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        client = TestClient(api.app, client=("127.0.0.1", 1234))
        self.assertEqual(client.get("/admin/api/settings/fleet-health").json()["flapping_drops_day"], 4)
        answer = client.put("/admin/api/settings/fleet-health", json={"flapping_drops_day": 7})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["flapping_drops_day"], 7)
        bad = client.put("/admin/api/settings/fleet-health", json={"flapping_drops_day": 0})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["code"], "FLEET_HEALTH_SETTING_INVALID")
        self.assertEqual(client.get("/admin/api/settings/fleet-health").json()["flapping_drops_day"], 7)


class TheProbeSaysWhichKindOfFailure(HistoryCase):
    def test_each_probe_failure_carries_a_kind(self) -> None:
        import httpx

        from gateway import fleet

        class Refused:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def get(self, *a, **k): raise httpx.ConnectError("refused")
            def stream(self, *a, **k): raise httpx.ConnectError("refused")

        class Silent:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def get(self, *a, **k): raise httpx.ReadTimeout("timed out")
            def stream(self, *a, **k): raise httpx.ReadTimeout("timed out")

        for fake, expected in ((Refused(), "refused"), (Silent(), "timeout")):
            with mock.patch.object(fleet.httpx, "Client", lambda *a, **k: fake):
                result = fleet._probe("http://127.0.0.1:47873", "token")
            self.assertEqual(result["error_kind"], expected, result.get("error"))
            self.assertFalse(result["reachable"])


if __name__ == "__main__":
    unittest.main()
