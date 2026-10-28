#!/usr/bin/env python3
"""Per-Mac function rankings steer Gateway placement without reserving idle Macs."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

sandbox_env("gateway-function-priority-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, fleet, startup_control  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.worker import scarcest_first  # noqa: E402


MODEL = "mlx-community/model"


def definition(location: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{location}")


def location(location_id: str, order: object, *, queue: int = 0, available: int = 1) -> dict:
    return {
        "id": location_id,
        "label": location_id.upper(),
        "local": False,
        "reachable": True,
        "capacity": {"queue_depth": queue},
        "model_supply": {MODEL: {"eligible": 1, "available": available}},
        "machine_summary": {"reachable": 1},
        "machines": [{"machine": f"{location_id}-mac", "priority_order": order}],
        "nodes": [],
        "activity": {"machines": [{"machine": f"{location_id}-mac", "state": "ready"}], "pulse": {}},
        "local_models": [{
            "model_id": MODEL,
            "machine": f"{location_id}-mac",
            "memory_eligible": True,
        }],
        "running_jobs": [],
        "workers": [],
        "probe_machines_hidden": 0,
    }


def candidates(rows: list[dict], modality: str) -> list[dict]:
    definitions = [definition(row["id"]) for row in rows]
    enabled = [(item, {"model_id": MODEL, "modality": modality, "enabled": True})
               for item in definitions]
    with patch.object(fleet, "snapshot", return_value={"locations": rows}), \
            patch.object(registry.models, "enabled_models", return_value=enabled):
        return fleet.routing_candidates(MODEL, modality)


def chosen(rows: list[dict], modality: str) -> str:
    ranked = candidates(rows, modality)
    return min(ranked, key=lambda row: fleet.rank_candidate(row, "job-1"))["location_id"]


class PlacementTests(unittest.TestCase):
    def test_a_matching_first_choice_wins_an_equally_loaded_route(self) -> None:
        rows = [location("chat", ["chat", "image"]), location("image", ["image", "chat"])]
        self.assertEqual(chosen(rows, "image"), "image")
        self.assertEqual(chosen(rows, "chat"), "chat")

    def test_voice_uses_the_hubs_voice_token(self) -> None:
        rows = [location("voice", ["voice"]), location("chat", ["chat"])]
        self.assertEqual(chosen(rows, "tts"), "voice")

    def test_real_hub_voice_model_rows_are_eligible_for_the_gateway_tts_lane(self) -> None:
        voice = location("voice", ["voice"])
        voice["local_models"] = fleet._local_models({"models": [{
            "repo": MODEL, "model_id": MODEL, "hub_machine": "voice-mac",
            "hub_modality": "voice", "memory_eligible": True, "runtime_ready": True,
            "available": True,
        }]}, {})
        rows = [voice, location("chat", ["chat"])]
        self.assertEqual(chosen(rows, "tts"), "voice")

    def test_no_preference_beats_an_explicitly_unlisted_function(self) -> None:
        rows = [location("equal", []), location("chat", ["chat"])]
        self.assertEqual(chosen(rows, "image"), "equal")

    def test_legacy_chat_priority_is_read_only_when_the_array_is_absent(self) -> None:
        legacy = location("legacy", None)
        legacy["machines"][0].pop("priority_order")
        legacy["machines"][0]["chat_priority"] = True
        rows = [legacy, location("image", ["image"])]
        self.assertEqual(chosen(rows, "chat"), "legacy")

    def test_explicit_malformed_priority_is_equal_not_legacy_chat(self) -> None:
        malformed = location("malformed", "chat")
        malformed["machines"][0]["chat_priority"] = True
        rows = [malformed, location("image", ["image"])]
        self.assertEqual(chosen(rows, "chat"), "malformed")

    def test_pressure_and_free_capacity_still_outrank_preference(self) -> None:
        queued = location("preferred", ["image"], queue=2, available=0)
        idle = location("lending", ["chat"], queue=0, available=1)
        self.assertEqual(chosen([queued, idle], "image"), "lending")

    def test_an_idle_site_beats_a_busy_site_even_when_its_pressure_is_higher(self) -> None:
        busy = {"location_id": "busy", "queue": 1, "slots": 10, "available": 0,
                "preference": 0.0, "preference_slots": 10}
        idle = {"location_id": "idle", "queue": 2, "slots": 1, "available": 1,
                "preference": 7.0, "preference_slots": 1}
        self.assertEqual(
            min([busy, idle], key=lambda row: fleet.rank_candidate(row, "job-1"))["location_id"],
            "idle",
        )

    def test_same_ordinal_rank_is_equal_in_partial_and_full_orders(self) -> None:
        partial = location("partial", ["voice", "image"])
        full = location("full", ["voice", "image", "chat", "music", "video"])
        ranked = candidates([partial, full], "image")
        self.assertEqual(ranked[0]["preference"], ranked[1]["preference"])

    def test_busy_preferred_mac_does_not_lend_its_rank_to_an_idle_neutral_mac(self) -> None:
        row = location("mixed", ["image"], available=1)
        row["model_supply"][MODEL] = {"eligible": 2, "available": 1}
        row["machine_summary"] = {"reachable": 2}
        row["machines"] = [
            {"machine": "preferred", "priority_order": ["image"]},
            {"machine": "idle", "priority_order": []},
        ]
        row["local_models"] = [
            {"model_id": MODEL, "machine": "preferred", "memory_eligible": True,
             "available": False},
            {"model_id": MODEL, "machine": "idle", "memory_eligible": True,
             "available": True},
        ]
        self.assertEqual(candidates([row], "image")[0]["preference"],
                         float(len(startup_control.WORK_PRIORITY_FUNCTIONS)))

    def test_model_specific_gate_excludes_a_preferred_mac(self) -> None:
        row = location("gated", ["image"], available=1)
        row["model_supply"][MODEL] = {"eligible": 1, "available": 1}
        row["machine_summary"] = {"reachable": 2}
        row["machines"] = [
            {"machine": "preferred", "priority_order": ["image"]},
            {"machine": "neutral", "priority_order": []},
        ]
        row["local_models"] = [
            {"model_id": MODEL, "machine": "preferred", "memory_eligible": True,
             "available": True, "gated": True},
            {"model_id": MODEL, "machine": "neutral", "memory_eligible": True,
             "available": True},
        ]
        self.assertEqual(candidates([row], "image")[0]["preference"],
                         float(len(startup_control.WORK_PRIORITY_FUNCTIONS)))

    def test_a_mac_without_the_requested_function_does_not_supply_its_rank(self) -> None:
        row = location("function", ["image"], available=1)
        row["machines"] = [
            {"machine": "wrong-function", "priority_order": ["image"]},
            {"machine": "image", "priority_order": []},
        ]
        row["local_models"] = [
            {"model_id": MODEL, "machine": "wrong-function", "memory_eligible": True,
             "available": True, "modality": "tts"},
            {"model_id": MODEL, "machine": "image", "memory_eligible": True,
             "available": True, "modality": "image"},
        ]
        self.assertEqual(candidates([row], "image")[0]["preference"],
                         float(len(startup_control.WORK_PRIORITY_FUNCTIONS)))


class QueueOrderingTests(unittest.TestCase):
    @staticmethod
    def job(name: str, modality: str, location_id: str = "site") -> dict:
        return {
            "id": name,
            "provider_id": fleet.provider_id_for(location_id),
            "model_id": MODEL,
            "modality": modality,
        }

    @staticmethod
    def candidate(location_id: str, preference: float) -> dict:
        return {"location_id": location_id, "preference": preference}

    def test_one_mac_gets_highest_ranked_due_work_then_fifo_within_rank(self) -> None:
        jobs = [
            self.job("old-voice", "tts"),
            self.job("old-chat", "chat"),
            self.job("new-image", "image"),
            self.job("newer-image", "image"),
        ]
        scores = {"tts": 1 / 6, "chat": 2 / 6, "image": 0.0}

        def route(model: str, modality: str, *, only_location: str) -> list[dict]:
            self.assertEqual((model, only_location), (MODEL, "site"))
            return [self.candidate("site", scores[modality])]

        with patch.object(fleet, "routing_candidates", side_effect=route):
            ordered = scarcest_first(jobs)
        self.assertEqual([job["id"] for job in ordered],
                         ["new-image", "newer-image", "old-voice", "old-chat"])

    def test_no_preference_keeps_due_fifo(self) -> None:
        jobs = [self.job("voice", "tts"), self.job("chat", "chat"), self.job("image", "image")]
        with patch.object(
            fleet, "routing_candidates",
            side_effect=lambda model, modality, *, only_location: [
                self.candidate(only_location, 1.0)
            ],
        ):
            self.assertEqual([job["id"] for job in scarcest_first(jobs)],
                             ["voice", "chat", "image"])

    def test_each_jobs_bound_location_supplies_its_own_rank(self) -> None:
        jobs = [self.job("old-image", "image", "chat-first"),
                self.job("new-chat", "chat", "image-first")]
        scores = {("chat-first", "image"): 1 / 6, ("image-first", "chat"): 1 / 6}

        def route(model: str, modality: str, *, only_location: str) -> list[dict]:
            return [self.candidate(only_location, scores[(only_location, modality)])]

        with patch.object(fleet, "routing_candidates", side_effect=route):
            self.assertEqual([job["id"] for job in scarcest_first(jobs)],
                             ["old-image", "new-chat"])

    def test_job_with_no_eligible_bound_candidate_follows_eligible_work(self) -> None:
        jobs = [self.job("blocked-image", "image"), self.job("eligible-chat", "chat")]

        def route(model: str, modality: str, *, only_location: str) -> list[dict]:
            return [] if modality == "image" else [self.candidate(only_location, 2.0)]

        with patch.object(fleet, "routing_candidates", side_effect=route):
            self.assertEqual([job["id"] for job in scarcest_first(jobs)],
                             ["eligible-chat", "blocked-image"])


class SnapshotTests(unittest.TestCase):
    def test_machine_rows_publish_the_validated_order_and_compatibility_flag(self) -> None:
        row = location("site", ["image", "chat"])
        row["machines"][0]["chat_priority"] = False
        flattened = fleet.machines_flat([row])[0]
        self.assertEqual(flattened.get("priority_order"), ["image", "chat"])
        self.assertIs(flattened.get("chat_priority"), False)

    def test_node_priority_fills_an_overview_row_that_does_not_state_it(self) -> None:
        row = location("site", [])
        row["machines"][0].pop("priority_order")
        row["nodes"] = [{"id": "site-mac", "priority_order": ["voice"], "chat_priority": False}]
        flattened = fleet.machines_flat([row])[0]
        self.assertEqual(flattened.get("priority_order"), ["voice"])

    def test_malformed_array_is_equal_even_when_the_legacy_flag_is_true(self) -> None:
        row = location("site", ["image", "image"])
        row["machines"][0]["chat_priority"] = True
        flattened = fleet.machines_flat([row])[0]
        self.assertEqual(flattened.get("priority_order"), [])
        self.assertIs(flattened.get("chat_priority"), False)


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        target = patch.object(startup_control, "_target", return_value=("http://controller:47873", "fleet-token"))
        target.start()
        self.addCleanup(target.stop)
        self.seen: list[httpx.Request] = []

    def client(self, answer: httpx.Response) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return answer
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_get_and_post_use_the_controller_node_policy_contract(self) -> None:
        if not hasattr(startup_control, "get_node_work_policy"):
            self.fail("Gateway has no per-node work-policy relay")
        read = startup_control.get_node_work_policy(
            "ctl", "mac", client=self.client(httpx.Response(200, json={
                "ok": True, "node": "mac", "work_policy": {
                    "priority_functions": [{"id": "image", "title": "Image"}],
                    "priority_order": ["image"], "machine_enabled": False, "studios": [],
                    "chat_priority": False,
                },
            })),
        )
        self.assertEqual(read["priority_order"], ["image"])
        request = self.seen.pop()
        self.assertEqual((request.method, request.url.path),
                         ("GET", "/api/hub/fleet/nodes/mac/work-policy"))
        self.assertEqual(request.headers["x-hub-token"], "fleet-token")

        saved = startup_control.set_node_work_policy(
            "ctl", "mac", ["voice", "chat"],
            client=self.client(httpx.Response(200, json={
                "ok": True, "node": "mac",
                "work_policy": {"priority_order": ["voice", "chat"]},
            })),
        )
        self.assertEqual(saved["priority_order"], ["voice", "chat"])
        request = self.seen.pop()
        self.assertEqual((request.method, request.url.path),
                         ("POST", "/api/hub/fleet/nodes/mac/work-policy"))
        self.assertEqual(json.loads(request.content), {"priority_order": ["voice", "chat"]})

    def test_invalid_order_never_reaches_a_controller(self) -> None:
        if not hasattr(startup_control, "set_node_work_policy"):
            self.fail("Gateway has no per-node work-policy relay")
        for bad in ("chat", ["chat", "chat"], [""], [1]):
            with self.assertRaises(startup_control.StartupError) as caught:
                startup_control.set_node_work_policy(
                    "ctl", "mac", bad,
                    client=self.client(httpx.Response(200, json={})),
                )
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.seen, [])

    def test_an_old_controller_has_a_clear_read_only_error(self) -> None:
        if not hasattr(startup_control, "get_node_work_policy"):
            self.fail("Gateway has no per-node work-policy relay")
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.get_node_work_policy(
                "ctl", "mac", client=self.client(httpx.Response(404, json={"detail": "Not Found"})),
            )
        self.assertEqual(caught.exception.body["code"], "HUB_TOO_OLD")
        self.assertIn("work priorities", caught.exception.body["detail"].lower())


class AdminRouteTests(unittest.TestCase):
    def test_admin_reads_and_replaces_exactly_one_macs_order(self) -> None:
        client = TestClient(api.app, client=("127.0.0.1", 41001))
        policy = {
            "priority_functions": [{"id": "image", "title": "Image"}],
            "priority_order": ["image"], "machine_enabled": False, "studios": [],
            "chat_priority": False,
        }
        with patch.object(startup_control, "get_node_work_policy", return_value=policy) as read, \
                patch.object(startup_control, "set_node_work_policy", return_value=policy) as save:
            response = client.get("/admin/api/fleet/controllers/site/machines/mac/work-policy")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["priority_order"], ["image"])
            read.assert_called_once_with("site", "mac")

            response = client.post(
                "/admin/api/fleet/controllers/site/machines/mac/work-policy",
                json={"priority_order": ["image"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            save.assert_called_once_with("site", "mac", ["image"])

    def test_admin_rejects_extra_fields_instead_of_silently_dropping_them(self) -> None:
        client = TestClient(api.app, client=("127.0.0.1", 41001))
        with patch.object(startup_control, "set_node_work_policy") as save:
            response = client.post(
                "/admin/api/fleet/controllers/site/machines/mac/work-policy",
                json={"priority_order": ["image"], "enabled": True},
            )
        self.assertEqual(response.status_code, 400, response.text)
        save.assert_not_called()


class NativeEditorTests(unittest.TestCase):
    def test_native_editor_preserves_partial_order_and_has_button_reordering(self) -> None:
        harness = r'''
import AppKit

enum TextRole { case body }
enum DS { enum Space { static let s: CGFloat = 8; static let m: CGFloat = 12 } }
enum PageKit {
    static func label(_ text: String, _ role: TextRole, colour: NSColor = .labelColor,
                      wrapping: Bool = false) -> NSTextField {
        wrapping ? NSTextField(wrappingLabelWithString: text) : NSTextField(labelWithString: text)
    }
}
struct FleetMachine {
    var locationId: String?; var machine: String?; var label: String?; var priorityOrder: [String]?
}
enum GatewayAPI {
    static func get(_ path: String, timeout: TimeInterval = 15,
                    completion: @escaping (Result<[String: Any], Error>) -> Void) {}
    static func post(_ path: String, body: [String: Any]? = nil, timeout: TimeInterval = 15,
                     completion: ((Result<[String: Any], Error>) -> Void)? = nil) {}
}
enum FleetStartup { static func show(_ title: String, _ detail: String) {} }

func buttons(_ view: NSView) -> [NSButton] {
    (view as? NSButton).map { [$0] } ?? view.subviews.flatMap(buttons)
}

func capture(_ view: NSView, to url: URL) {
    let cached = view.bitmapImageRepForCachingDisplay(in: view.bounds)!
    view.cacheDisplay(in: view.bounds, to: cached)
    let rendered = NSImage(data: cached.tiffRepresentation!)!
    let image = NSImage(size: view.bounds.size)
    image.lockFocus()
    view.effectiveAppearance.performAsCurrentDrawingAppearance {
        NSColor.windowBackgroundColor.setFill()
        NSRect(origin: .zero, size: image.size).fill()
        rendered.draw(in: NSRect(origin: .zero, size: image.size))
    }
    image.unlockFocus()
    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
    try! bitmap.representation(using: .png, properties: [:])!.write(to: url)
}

@main struct Check {
    static func main() {
        let raw: [String: Any] = [
            "priority_functions": [["id": "chat", "title": "Chat Studio"],
                                   ["id": "transcription", "title": "Subtitle Studio"],
                                   ["id": "image", "title": "Image Studio"]],
            "priority_order": ["chat", "transcription"],
            "machine_enabled": false,
            "studios": [["id": "chat@local", "modality": "chat", "enabled": true],
                        ["id": "subtitle@local", "modality": "transcription", "enabled": false],
                        ["id": "image@local", "modality": "image", "enabled": true]]
        ]
        guard let policy = FleetWorkPriorityPolicy(raw) else { fatalError("policy did not parse") }
        assert(policy.machineEnabled == false)
        let form = FleetWorkPriorityForm(policy)
        assert(form.priorityOrder == ["chat", "transcription"])
        assert(buttons(form.view).contains {
            $0.title == "Subtitle Studio — Disabled" && $0.state == .on && !$0.isEnabled
        })
        let image = buttons(form.view).first { $0.title == "Image Studio" }!
        image.performClick(nil)
        assert(form.priorityOrder == ["chat", "transcription", "image"])
        let imageUp = buttons(form.view).first { $0.title == "Up" && $0.isEnabled }!
        imageUp.performClick(nil)
        assert(form.priorityOrder == ["image", "transcription", "chat"],
               "disabled function did not keep its saved slot: \(form.priorityOrder)")
        assert(FleetWorkPriorityPolicy(["priority_order": []] as [String: Any]) == nil)
        assert(FleetWorkPriorityPolicy([
            "priority_functions": [["id": "voice", "title": "Voice"]],
            "priority_order": ["unknown"]
        ] as [String: Any]) == nil)

        let equal = FleetWorkPriorityForm(FleetWorkPriorityPolicy([
            "priority_functions": [["id": "voice", "title": "Voice"],
                                   ["id": "image", "title": "Image"]],
            "priority_order": []
        ])!)
        assert(equal.priorityOrder == [])
        buttons(equal.view).first { $0.title == "Custom order" }!.performClick(nil)
        assert(equal.priorityOrder == ["voice", "image"])

        let full = FleetWorkPriorityForm(FleetWorkPriorityPolicy([
            "priority_functions": [["id": "voice", "title": "A very long Voice Studio title that must not widen the work-priority sheet beyond its fixed content width"],
                                   ["id": "image", "title": "Image"]],
            "priority_order": ["voice", "image"]
        ])!)
        assert(full.priorityOrder == ["voice", "image"])

        // **Final render in the ranking, plain words** (owner, 2026-10-28).
        assert(FleetPriorityNames.title(for: "render", from: nil) == "Final render")
        assert(FleetPriorityNames.title(for: "render", from: "Final render") == "Final render")
        let withRender = FleetWorkPriorityForm(FleetWorkPriorityPolicy([
            "priority_functions": [["id": "chat", "title": "Chat Studio"],
                                   ["id": "render"]],
            "priority_order": ["chat", "render"]
        ])!)
        assert(withRender.priorityOrder == ["chat", "render"])
        assert(buttons(withRender.view).contains { $0.title == "Final render" && $0.state == .on })
        full.view.frame = NSRect(x: 0, y: 0, width: 480, height: full.view.fittingSize.height)
        full.view.layoutSubtreeIfNeeded()
        let long = buttons(full.view).first { $0.title.hasPrefix("A very long") }!
        assert(long.frame.width <= 301, "long title propagated width: \(long.frame.width)")

        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let app = NSApplication.shared
        let window = NSWindow(contentRect: full.view.frame, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = full.view
        for (name, appearance) in [("light", NSAppearance.Name.aqua),
                                   ("dark", NSAppearance.Name.darkAqua)] {
            app.appearance = NSAppearance(named: appearance)
            window.appearance = NSAppearance(named: appearance)
            full.view.layoutSubtreeIfNeeded()
            capture(full.view, to: output.appendingPathComponent("work-priority-\(name).png"))
        }
        print("PASS: native work-priority editor")
    }
}
'''
        with tempfile.TemporaryDirectory(prefix="gateway-work-priority-") as directory:
            source = Path(directory) / "check.swift"
            binary = Path(directory) / "check"
            render_dir = Path(os.environ.get("GATEWAY_PRIORITY_RENDER_DIR", directory))
            render_dir.mkdir(parents=True, exist_ok=True)
            source.write_text(harness)
            built = subprocess.run(
                ["swiftc", "-swift-version", "5", "-framework", "AppKit",
                 "menubar/FleetWorkPriority.swift", str(source), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            checked = subprocess.run([str(binary), str(render_dir)], capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("PASS:", checked.stdout)
            for appearance in ("light", "dark"):
                shot = render_dir / f"work-priority-{appearance}.png"
                self.assertTrue(shot.exists() and shot.stat().st_size > 1000, appearance)


if __name__ == "__main__":
    unittest.main()
