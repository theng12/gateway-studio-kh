#!/usr/bin/env python3
"""`startup.last_update_error` - why a Mac's Studio Hub update failed (Hub 0.6.152) - reaches the Fleet page.

The Hub publishes `{phase, reason, build, at}` or null inside `startup`, for controllers and nodes alike.
It is another machine's word, so the gateway republishes only those keys, each checked. The Swift half
(the amber row line, the group header's count, the paced-rollout panel's step text) is compiled and run
against rows the gateway's own assembly produced.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-update-errors-")

import httpx  # noqa: E402

from gateway import fleet  # noqa: E402

NOW = 1_790_000_000.0
ERROR = {"phase": "installing", "reason": "The app that landed reports build 91ac2f0.", "build": "e6a8b80", "at": NOW - 3600}


def _snapshot(machines: list[dict]) -> dict[str, dict]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/hub/fleet/overview":
            return httpx.Response(200, json={"machines": machines, "summary": {}})
        if request.url.path == "/api/hub/fleet/nodes":
            return httpx.Response(200, json={"nodes": [], "summary": {}})
        return httpx.Response(404, json={"detail": "not served by this mock"})

    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    with patch.object(fleet.httpx, "Client", side_effect=factory):
        location = fleet._probe("http://100.64.0.2:47873", "token")
    location.update({"id": "ctl1", "label": "Site A", "local": False})
    return {row["machine"]: row for row in fleet.machines_flat([location])}


def _mac(mid: str, error: object) -> dict:
    return {"machine": mid, "reachable": True, "startup": {"open_at_login": True, "last_update_error": error}}


class CleaningTests(unittest.TestCase):
    def error(self, raw: object) -> dict | None:
        return _snapshot([_mac("m1", raw)])["m1"]["startup"]["last_update_error"]

    def test_it_flows_through_with_exactly_the_four_keys(self) -> None:
        self.assertEqual(self.error({**ERROR, "evil": "<script>"}),
                         {"phase": "installing", "reason": ERROR["reason"], "build": "e6a8b80", "at": NOW - 3600})

    def test_strings_are_one_line_and_capped(self) -> None:
        fact = self.error({**ERROR, "reason": "line one\n\tline two " + "x" * 400, "build": "b" * 90})
        self.assertNotIn("\n", fact["reason"])
        self.assertEqual((len(fact["reason"]), len(fact["build"])), (300, 40))
        self.assertTrue(fact["reason"].startswith("line one line two x"))

    def test_the_time_is_a_number_or_nothing(self) -> None:
        for bad in ("yesterday", True, None, [1]):
            self.assertIsNone(self.error({**ERROR, "at": bad})["at"], repr(bad))
        for bad in (float("inf"), float("nan")):  # not JSON, so straight to the cleaner
            self.assertIsNone(fleet._update_error_fact({**ERROR, "at": bad})["at"], repr(bad))
        self.assertEqual(self.error({**ERROR, "at": 7})["at"], 7.0)

    def test_an_unfamiliar_phase_is_never_guessed(self) -> None:
        self.assertIsNone(self.error({**ERROR, "phase": "gremlins"})["phase"])

    def test_nothing_usable_claims_no_failure(self) -> None:
        for raw in (None, "not an object", 7, [], {}, {"phase": "gremlins", "at": NOW}, {"reason": "  \n ", "build": "x"}):
            self.assertIsNone(self.error(raw), repr(raw))

    def test_a_hub_that_says_nothing_has_none(self) -> None:
        row = _snapshot([{"machine": "old", "reachable": True, "startup": {"open_at_login": True}}])["old"]
        self.assertIsNone(row["startup"]["last_update_error"])


SWIFT = r'''
import AppKit

enum GatewayAPI {
    static func relativeTime(_ epoch: Double?) -> String { epoch == nil ? "—" : "just now" }
}

@main struct UpdateErrorCheck {
    static func main() {
        let dir = CommandLine.arguments[1]
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let rows = try! decoder.decode([String: FleetMachine].self,
                                       from: try! Data(contentsOf: URL(fileURLWithPath: dir + "/rows.json")))
        let rollout = try! decoder.decode(FleetRollout.self,
                                          from: try! Data(contentsOf: URL(fileURLWithPath: dir + "/rollout.json")))
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: \(NOW))
        func expect(_ actual: String?, _ wanted: String?, _ what: String) {
            if actual != wanted { print("FAIL \(what): got \(actual ?? "nil"), wanted \(wanted ?? "nil")"); exit(1) }
        }
        func line(_ id: String) -> String? { FleetUpdateFailure.rowLine(rows[id]!, now: now, calendar: calendar) }

        // The row: amber line for a recent failure of a Mac that has not reached the release.
        expect(line("node"), "Update failed while installing: The app that landed reports build 91ac2f0 · Sep 20, 18:13",
               "a node, yesterday: the date comes with the time, and no full stop before the dot")
        expect(line("ctl"), "Update failed while downloading: HTTP 404 · 13:53", "a controller, same day, time only")
        expect(line("nophase"), "Update failed: it stopped · 14:03", "no phase, no 'while'")
        expect(line("old"), nil, "a failure more than 24 hours old is history")
        expect(line("latest"), nil, "a Mac on the newest release is past any failed attempt")
        expect(line("notime"), nil, "no time, no claim it is recent")
        expect(line("none"), nil, "no failure")
        expect(FleetUpdateFailure.sentence(FleetUpdateError(phase: "verifying", reason: nil, build: nil, at: nil)),
               "Update failed while verifying", "no reason")

        // The group header.
        let all = ["node", "ctl", "nophase", "old", "latest", "notime", "none"].map { rows[$0]! }
        expect(FleetUpdateFailure.countText(all, now: now), "3 Macs' updates failed", "three recent failures")
        expect(FleetUpdateFailure.countText([rows["ctl"]!], now: now), "1 Mac's update failed", "one failure, singular")
        expect(FleetUpdateFailure.countText([rows["old"]!, rows["none"]!], now: now), nil, "none to count")

        // The paced-rollout panel.
        let steps = Dictionary(uniqueKeysWithValues: rollout.orderedSteps.map { ($0.id, $0.step) })
        expect(steps["c12"]!.panelText(version: "0.6.152").text,
               "installing and restarting · Update failed while downloading: HTTP 404 for the image",
               "a step whose controller reported an error shows it")
        expect(steps["c13"]!.panelText(version: "0.6.152").text, "waiting", "a step without an error is unchanged")
        expect(steps["c11"]!.panelText(version: "0.6.152").text, "updated · 4 of 4 Macs on 0.6.152", "an updated step")
        print("PASS: update failures read in plain English")
    }
}
'''


class UpdateErrorSwiftTests(unittest.TestCase):
    """The Fleet page's wording, driven by rows the gateway's own assembly produced."""

    def test_the_fleet_page_says_which_update_failed_and_why(self) -> None:
        machines = [
            _mac("node", {**ERROR, "at": NOW - 20 * 3600}),
            _mac("ctl", {"phase": "downloading", "reason": "HTTP 404", "build": "e6a8b80", "at": NOW - 20 * 60}),
            _mac("nophase", {"phase": "gremlins", "reason": "it stopped", "at": NOW - 600}),
            _mac("old", {**ERROR, "at": NOW - 25 * 3600}),
            _mac("latest", ERROR),
            _mac("notime", {**ERROR, "at": None}),
            _mac("none", None),
        ]
        rows = _snapshot(machines)
        rows["latest"]["version_status"] = "latest"
        rollout = {"state": "running", "version": "0.6.152", "build": "e6a8b80", "order": ["c11", "c12", "c13"],
                   "steps": {"c11": {"label": "1100", "state": "updated", "nodes_updated": 4, "nodes_total": 4,
                                     "update_error": None},
                             "c12": {"label": "1200", "state": "installing",
                                     "update_error": {"phase": "downloading", "reason": "HTTP 404 for the image",
                                                      "build": "e6a8b80", "at": NOW - 60}},
                             "c13": {"label": "1300", "state": "waiting", "update_error": None}}}
        with tempfile.TemporaryDirectory(prefix="gateway-update-errors-") as directory:
            work = Path(directory)
            (work / "check.swift").write_text(SWIFT.replace("\\(NOW)", repr(NOW)))
            (work / "rows.json").write_text(json.dumps(rows))
            (work / "rollout.json").write_text(json.dumps(rollout))
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit", "menubar/PageKit.swift",
                 "menubar/FleetModels.swift", "menubar/FleetStartupModel.swift", "menubar/FleetLastStop.swift",
                 "menubar/FleetWorkstation.swift", str(work / "check.swift"), "-o", str(work / "check")],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(work / "check"), directory], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)


if __name__ == "__main__":
    unittest.main()
