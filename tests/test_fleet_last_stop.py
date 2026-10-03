#!/usr/bin/env python3
"""`last_stop` - why a Mac's Studio Hub last stopped - flows from the controller to the fleet snapshot.

The Hub publishes it on `machines[]` and `fleet/nodes` rows beside `startup`. It is another machine's
word, so the gateway republishes only the known keys, checks each, and never guesses a reason.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-last-stop-")

import httpx  # noqa: E402

from gateway import fleet  # noqa: E402

STOP = {"reason": "force_quit", "at": 1_760_000_000, "label": "Force-quit", "relaunched": False}


def _controller(machines: list[dict], nodes: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/hub/fleet/overview":
            return httpx.Response(200, json={"machines": machines, "summary": {}})
        if request.url.path == "/api/hub/fleet/nodes":
            return httpx.Response(200, json={"nodes": nodes, "summary": {}})
        return httpx.Response(404, json={"detail": "not served by this mock"})
    return httpx.MockTransport(handler)


def _snapshot(machines: list[dict], nodes: list[dict]) -> list[dict]:
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=_controller(machines, nodes), **kwargs)

    with patch.object(fleet.httpx, "Client", side_effect=factory):
        location = fleet._probe("http://100.64.0.2:47873", "token")
    location.update({"id": "ctl1", "label": "Site A", "local": False})
    return fleet.machines_flat([location])


class LastStopTests(unittest.TestCase):
    def test_it_flows_from_the_controllers_machine_row_into_the_snapshot(self) -> None:
        rows = _snapshot([{"machine": "m1", "reachable": False, "last_stop": STOP}], [])
        self.assertEqual(rows[0]["last_stop"], {"reason": "force_quit", "at": 1_760_000_000.0,
                                                "label": "Force-quit", "relaunched": False, "stale": None})

    def test_the_node_row_is_the_fallback_and_carries_stale(self) -> None:
        rows = _snapshot([{"machine": "n1", "reachable": False}],
                         [{"id": "n1", "state": "unreachable", "reason": "unreachable",
                           "last_stop": {**STOP, "reason": "crashed", "stale": True}}])
        self.assertEqual((rows[0]["last_stop"]["reason"], rows[0]["last_stop"]["stale"]), ("crashed", True))

    def test_the_machine_rows_value_wins_over_the_node_rows(self) -> None:
        rows = _snapshot([{"machine": "n1", "last_stop": {**STOP, "reason": "quit"}}],
                         [{"id": "n1", "last_stop": {**STOP, "reason": "crashed"}}])
        self.assertEqual(rows[0]["last_stop"]["reason"], "quit")

    def test_a_hub_that_says_nothing_is_unknown(self) -> None:
        self.assertIsNone(_snapshot([{"machine": "old", "reachable": True}], [])[0]["last_stop"])

    def test_junk_and_unknown_keys_are_dropped_and_an_unknown_reason_is_never_guessed(self) -> None:
        stop = {**STOP, "reason": "gremlins", "evil": "<script>", "relaunched": "yes",
                "label": "  Force-quit \n by\tgremlins " + "x" * 200}
        fact = _snapshot([{"machine": "m1", "last_stop": stop}], [])[0]["last_stop"]
        self.assertNotIn("evil", fact)
        self.assertIsNone(fact["reason"], "an unfamiliar reason is not mapped to a familiar one")
        self.assertIsNone(fact["relaunched"])
        self.assertTrue(fact["label"].startswith("Force-quit by gremlins x"))
        self.assertLessEqual(len(fact["label"]), 80)

    def test_a_fact_with_no_usable_time_or_no_words_is_dropped(self) -> None:
        for bad in ({**STOP, "at": "09:36"}, {**STOP, "at": True},
                    {"reason": "gremlins", "at": 1.0}, {"reason": "quit"}, "quit", []):
            self.assertIsNone(_snapshot([{"machine": "m1", "last_stop": bad}], [])[0]["last_stop"], bad)

    def test_a_non_finite_time_is_dropped(self) -> None:
        for at in (float("nan"), float("inf")):
            self.assertIsNone(fleet._last_stop_fact({"last_stop": {**STOP, "at": at}}, {}))

    def test_every_reason_the_hub_defines_is_kept(self) -> None:
        for reason in ("quit", "crashed", "updated", "force_quit", "low_memory", "mac_restarted"):
            fact = _snapshot([{"machine": "m1", "last_stop": {"reason": reason, "at": 5}}], [])[0]["last_stop"]
            self.assertEqual(fact["reason"], reason)


def _epoch(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


SWIFT = r'''
import AppKit

enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct LastStopCheck {
    static func main() {
        let data = try! Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let rows = try! decoder.decode([String: FleetMachine].self, from: data)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: \(NOW))
        func why(_ id: String) -> String? { FleetOffline.why(rows[id]!, now: now, calendar: calendar) }
        func tip(_ id: String) -> String? { FleetOffline.tooltip(rows[id]!, now: now, calendar: calendar) }
        func expect(_ actual: String?, _ wanted: String?, _ what: String) {
            if actual != wanted { print("FAIL \(what): got \(actual ?? "nil"), wanted \(wanted ?? "nil")"); exit(1) }
        }
        expect(why("newer"), "Studio Hub isn't running on this Mac · Force-quit at 09:36", "no_hub with a newer stop")
        expect(why("older"), "Studio Hub isn't running on this Mac", "a stop older than the last time it was seen")
        expect(why("noseen"), "Studio Hub isn't running on this Mac", "no last-seen time to compare with")
        expect(why("dark"), "Mac restarted, asleep or lost network (reason unknown)", "unreachable")
        expect(why("up"), nil, "a Mac that answers is not offline")
        expect(why("refused"), nil, "a refused credential is not an outage")
        expect(tip("older"), "Studio Hub isn't running on this Mac\nLast stopped: Crashed (restarted) at Oct 2, 09:36",
               "the tooltip always shows the last stop, with the date when not today")
        expect(tip("up"), "Last stopped: Stopped for low memory at 11:15", "the reason falls back to the same words")
        expect(tip("dark"), "Mac restarted, asleep or lost network (reason unknown)\nLast stopped: Quit at 07:00 (as last reported by the Mac)",
               "a repeated word says so")
        expect(tip("nothing"), nil, "an unknown reason with no label says nothing")
        print("PASS: offline reasons read in plain English")
    }
}
'''


class OfflineWhySwiftTests(unittest.TestCase):
    """The Fleet page's wording, driven by rows the gateway's own assembly produced."""

    def test_the_fleet_page_says_why_a_mac_is_offline(self) -> None:
        now = _epoch(3, 12)
        seen = _epoch(3, 8)
        stop = {"relaunched": False}
        machines = [
            {"machine": "newer", "reachable": False, "last_stop": {**stop, "reason": "force_quit", "at": _epoch(3, 9, 36), "label": "Force-quit"}},
            {"machine": "older", "reachable": False, "last_stop": {"reason": "crashed", "at": _epoch(2, 9, 36), "label": "Crashed (restarted)"}},
            {"machine": "noseen", "reachable": False},
            {"machine": "dark", "reachable": False, "last_stop": {"reason": "quit", "at": _epoch(3, 7), "label": "Quit", "stale": True}},
            {"machine": "up", "reachable": True, "last_stop": {"reason": "low_memory", "at": _epoch(3, 11, 15)}},
            {"machine": "refused", "reachable": False},
            {"machine": "nothing", "reachable": True, "last_stop": {"reason": "gremlins", "at": 1.0}},
        ]
        nodes = [
            {"id": "newer", "state": "unreachable", "reason": "no_hub", "last_seen_at": seen},
            {"id": "older", "state": "unreachable", "reason": "no_hub", "last_seen_at": seen},
            {"id": "noseen", "state": "unreachable", "reason": "no_hub"},
            {"id": "dark", "state": "unreachable", "reason": "unreachable", "last_seen_at": seen},
            {"id": "refused", "state": "unauthorized", "reason": "no_hub", "last_seen_at": seen},
        ]
        rows = {row["machine"]: row for row in _snapshot(machines, nodes)}
        with tempfile.TemporaryDirectory(prefix="gateway-last-stop-") as directory:
            source, binary, data = (Path(directory) / name for name in ("check.swift", "check", "rows.json"))
            source.write_text(SWIFT.replace("\\(NOW)", repr(now)))
            data.write_text(json.dumps(rows))
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit", "menubar/PageKit.swift",
                 "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift",
                 str(source), "-o", str(binary)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(binary), str(data)], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
