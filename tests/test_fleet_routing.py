#!/usr/bin/env python3
"""Fleet routing: weight a model by the machines that can actually run it.

The router used to weight a controller by **every** machine at the site, so a
controller with ten 8 GB image Macs and one 24 GB Mac out-weighed a controller
with two 24 GB Macs for a model only the big ones can run. These checks pin the
per-model weight the Hub's `model_supply` now feeds, its fallbacks for an older
Hub, and the `GATEWAY_PREFER_FASTEST_SITE` toggle.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-fleet-routing-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from mock_upstream import FLEET_MODEL_ROWS, FLEET_WORKERS, Handler, Server  # noqa: E402


MODEL = "mlx-community/heavy-image-model"
MODALITY = "image"


def _definition(location_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{location_id}")


def _model_row() -> dict:
    return {"model_id": MODEL, "modality": MODALITY, "enabled": True, "raw": {}}


def _location(location_id: str, **overrides) -> dict:
    row = {
        "id": location_id,
        "label": location_id.title(),
        "reachable": True,
        "capacity": {},
        "model_supply": {},
        "machines": [],
        "machine_summary": {"reachable": 0},
        "running_jobs": [],
    }
    row.update(overrides)
    return row


def _route_with(locations: list[dict], model_id: str = MODEL, modality: str = MODALITY):
    def snapshot(allow_stale: bool = False) -> dict:
        return {"locations": locations}

    definitions = [_definition(row["id"]) for row in locations]

    def enabled(modality_name: str):
        assert modality_name == modality, modality_name
        return [(definition, _model_row()) for definition in definitions]

    with patch.object(fleet, "snapshot", snapshot), \
            patch.object(registry.models, "enabled_models", enabled):
        return fleet.routing_candidates(model_id, modality)


class PerModelWeightTests(unittest.TestCase):
    def test_weight_counts_only_the_machines_that_fit_the_model(self) -> None:
        # A: ten 8 GB image Macs + one 24 GB. Site-wide capacity is 11, but only
        # one machine can run this model. B: two 24 GB Macs, both fitting.
        a = _location(
            "a",
            capacity={"eligible_physical_machine_slots_total": 11, "queue_depth": 0},
            model_supply={MODEL: {"eligible": 1, "available": 1}},
        )
        b = _location(
            "b",
            capacity={"eligible_physical_machine_slots_total": 2, "queue_depth": 0},
            model_supply={MODEL: {"eligible": 2, "available": 2}},
        )
        slots = {c["location_id"]: c["slots"] for c in _route_with([a, b])}
        self.assertEqual(slots, {"a": 1, "b": 2}, "the site-wide total must not dilute the weight")

    def test_pressure_uses_the_fitting_machine_count(self) -> None:
        # Equal queues: A's 11 machines would read as low pressure; its one fitting
        # machine is the truth, so B (two fitting machines) is the less pressured.
        a = _location(
            "a",
            capacity={"eligible_physical_machine_slots_total": 11, "queue_depth": 2},
            model_supply={MODEL: {"eligible": 1, "available": 1}},
        )
        b = _location(
            "b",
            capacity={"eligible_physical_machine_slots_total": 2, "queue_depth": 2},
            model_supply={MODEL: {"eligible": 2, "available": 2}},
        )
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_falls_back_to_operation_then_site_total(self) -> None:
        # No per-model row: the per-operation capacity is next best.
        a = _location(
            "a",
            capacity={
                "eligible_physical_machine_slots_total": 9,
                "queue_depth": 0,
                "by_operation": {"image.text_to_image": {"eligible_physical_machine_slots_total": 3}},
            },
        )
        # Then the old site-wide total, for a Hub that publishes neither.
        b = _location("b", capacity={"eligible_physical_machine_slots_total": 5, "queue_depth": 0})
        slots = {c["location_id"]: c["slots"] for c in _route_with([a, b])}
        self.assertEqual(slots, {"a": 3, "b": 5})

    def test_available_and_speed_are_read_from_the_hub(self) -> None:
        a = _location(
            "a",
            model_supply={MODEL: {"eligible": 2, "available": 1}},
            machines=[{"chip": "Apple M4 Pro", "memory_gb": 24.0}],
        )
        candidate, = _route_with([a])
        self.assertEqual(candidate["available"], 1)
        self.assertEqual(candidate["speed"], 424.0)  # 4 * 100 + 24

    def test_missing_supply_is_not_zero(self) -> None:
        # A Hub that says nothing about free capacity must not read as "none free".
        a = _location("a", capacity={"eligible_physical_machine_slots_total": 2})
        candidate, = _route_with([a])
        self.assertIsNone(candidate["available"])
        self.assertEqual(candidate["slots"], 2)


class RankTests(unittest.TestCase):
    def test_free_capacity_breaks_a_pressure_tie(self) -> None:
        # Same queue and same fitting count; only one site has a Mac free now.
        a = _location("a", model_supply={MODEL: {"eligible": 2, "available": 0}})
        b = _location("b", model_supply={MODEL: {"eligible": 2, "available": 3}})
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_prefer_fastest_site_ranks_the_faster_controller_first(self) -> None:
        # Equal pressure and equal capacity; only the chip differs (the Hub's proxy).
        a = _location(
            "a",
            model_supply={MODEL: {"eligible": 2, "available": 2}},
            machines=[{"chip": "Apple M2", "memory_gb": 8.0}],
        )
        b = _location(
            "b",
            model_supply={MODEL: {"eligible": 2, "available": 2}},
            machines=[{"chip": "Apple M4 Pro", "memory_gb": 16.0}],
        )
        with patch.dict(os.environ, {"GATEWAY_PREFER_FASTEST_SITE": "1"}), \
                patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": [a, b]}), \
                patch.object(registry.models, "enabled_models",
                             lambda modality: [(_definition("a"), _model_row()), (_definition("b"), _model_row())]):
            chosen, note = fleet.route(MODEL, MODALITY)
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}b", note)

    def test_speed_score_mirrors_the_hub_proxy(self) -> None:
        self.assertEqual(fleet._site_speed_score([{"chip": "Apple M2", "memory_gb": 8.0}]), 208.0)
        self.assertEqual(
            fleet._site_speed_score(
                [{"chip": "Apple M2", "memory_gb": 8.0}, {"chip": "Apple M4 Pro", "memory_gb": 24.0}]
            ),
            424.0,
        )
        self.assertEqual(fleet._site_speed_score([]), 0.0)
        self.assertEqual(fleet._site_speed_score(None), 0.0)


# --------------------------------------------------------------------------
# The Fleet page's additive tabs: per-location models and workers, the issues
# list, and the gateway-side model counts the admin route enriches in.


#: The exact keys `/admin/api/fleet` publishes per local model row. Checked as a
#: set so an accidental extra field — or a dropped one — fails here.
LOCAL_MODEL_KEYS = {
    "model", "model_id", "label", "family", "studio", "modality", "machine", "kind",
    "size_gb", "min_unified_memory_gb", "required_free_memory_gb",
    "memory_eligible", "runtime_ready", "available", "loaded", "fit",
    "cache_state", "catalog_error", "catalog_stale", "active_download",
    "gated", "dispatch_state",
}


def _unreachable_location() -> dict:
    """A location row shaped like `_probe`'s failure result."""
    return {
        "id": "dead", "label": "Dead", "local": False, "reachable": False,
        "error": "Nothing is listening on that address.",
        "role": None, "ready": None, "machines": [], "running_jobs": [],
        "activity": {"pulse": {}, "machines": []},
        "local_models": [], "workers": [], "probe_machines_hidden": 0,
    }


class FleetTabTests(unittest.TestCase):
    """`fleet._probe` and `snapshot` against the mock Hub."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _probe(self) -> dict:
        return fleet._probe(self.base, "mock-token")

    def test_local_models_are_trimmed_to_the_contract_keys(self) -> None:
        location = self._probe()
        self.assertTrue(location["reachable"], location["error"])
        rows = location["local_models"]
        self.assertEqual(len(rows), len(FLEET_MODEL_ROWS))
        for row in rows:
            self.assertEqual(set(row), LOCAL_MODEL_KEYS, row)

    def test_one_row_per_model_per_machine(self) -> None:
        rows = self._probe()["local_models"]
        pairs = [(row["model"], row["machine"]) for row in rows]
        self.assertEqual(len(pairs), len(set(pairs)), pairs)
        self.assertEqual(len({row["model"] for row in rows}), 3)
        self.assertEqual({row["machine"] for row in rows}, {"mac-a", "mac-b"})
        self.assertEqual({row["modality"] for row in rows}, {"image", "voice"})

    def test_the_probe_fills_model_supply_from_the_hubs_own_machine_rows(self) -> None:
        # Hub 0.6.47 publishes `capabilities.model_supply` empty. The router must
        # still get the per-model machine counts, reduced from the Hub's own
        # per-machine eligibility rows, or it weights every site by its whole
        # machine total for a model most of them cannot run.
        location = self._probe()
        expected = fleet._supply_from_local_models(location["local_models"])
        self.assertTrue(expected, "the mock must state at least one eligibility")
        self.assertEqual(location["model_supply"], expected)

    def test_availability_fit_error_and_dispatch_are_read_per_machine(self) -> None:
        by_machine = {
            (row["model"], row["machine"]): row
            for row in self._probe()["local_models"]
        }
        vision_a = by_machine[("mock-hub/vision-one", "mac-a")]
        vision_b = by_machine[("mock-hub/vision-one", "mac-b")]
        self.assertTrue(vision_a["available"])
        self.assertFalse(vision_b["available"])
        self.assertEqual(vision_a["fit"]["state"], "fits")
        self.assertIsNone(vision_b["fit"])
        # `dispatch_state` is joined from `/api/hub/model-packages` by the
        # `package_id` the row's cache names; a row with no package is `None`,
        # not a guess.
        self.assertEqual(vision_a["dispatch_state"], "eligible")
        self.assertEqual(vision_b["dispatch_state"], "eligible")
        voice_one = by_machine[("mock-hub/voice-one", "mac-a")]
        self.assertEqual(voice_one["dispatch_state"], "ineligible")
        self.assertEqual(voice_one["catalog_error"], "catalogue read failed")
        voice_two = by_machine[("mock-hub/voice-two", "mac-a")]
        self.assertIsNone(voice_two["dispatch_state"])
        self.assertTrue(by_machine[("mock-hub/voice-two", "mac-b")]["active_download"])
        self.assertFalse(voice_two["active_download"])

    def test_workers_come_from_the_capabilities_roster(self) -> None:
        location = self._probe()
        workers = location["workers"]
        self.assertEqual(len(workers), len(FLEET_WORKERS))
        by_id = {worker["id"]: worker for worker in workers}
        self.assertEqual(set(by_id), {
            "svc-image-a", "svc-image-b", "svc-voice-a", "svc-voice-b",
        })
        healthy = by_id["svc-image-a"]
        self.assertEqual(healthy["studio"], "image")
        self.assertEqual(healthy["modality"], "image")
        self.assertEqual(healthy["machine"], "mac-a")
        self.assertEqual((healthy["slots"], healthy["slots_total"]), (1, 2))
        self.assertFalse(healthy["quarantined"])
        self.assertTrue(by_id["svc-voice-b"]["quarantined"])

    def test_a_worker_modality_comes_from_its_studio_when_unstated(self) -> None:
        # **The live capabilities roster carries no `modality` field at all.**
        # Measured against Hub 0.6.47: reading one published `None` for every
        # worker, so the Machines tab lost the one thing it groups by. The studio
        # is what is stated, and `subtitle`/`transcription` are the same studio
        # under the two rosters' spellings.
        self.assertEqual(fleet._studio_modality("subtitle", None), "transcription")
        self.assertEqual(fleet._studio_modality("transcription", None), "transcription")
        self.assertEqual(fleet._studio_modality("voice", None), "voice")
        self.assertIsNone(fleet._studio_modality("mystery", None))
        # A stated modality always wins, so a Hub that starts publishing one is
        # believed rather than overridden by this table.
        self.assertEqual(fleet._studio_modality("image", "stated"), "stated")

    def test_workers_fall_back_to_nodes_when_capabilities_has_none(self) -> None:
        real_fetch = fleet._fetch

        def fetch(client, base, token, path):
            if path == "/api/hub/capabilities":
                return {}
            return real_fetch(client, base, token, path)

        with patch.object(fleet, "_fetch", fetch):
            location = fleet._probe(self.base, "mock-token")
        self.assertTrue(location["reachable"], location["error"])
        workers = location["workers"]
        self.assertEqual(
            [worker["id"] for worker in workers],
            ["mac-a:image", "mac-a:voice", "mac-b:image"],
        )
        self.assertEqual([worker["machine"] for worker in workers],
                         ["mac-a", "mac-a", "mac-b"])
        self.assertTrue(workers[0]["online"] and workers[1]["online"])
        self.assertFalse(workers[2]["online"])
        # A node states nothing about readiness, so the page must say `None`.
        self.assertIsNone(workers[0]["ready"])

    def test_snapshot_aggregates_issues_and_counts_the_summary(self) -> None:
        location = self._probe()
        location.update({
            "id": "mock", "label": "Mock", "local": False,
            "provider_id": "studio-hub-mock",
        })
        with patch.object(fleet, "locations", return_value=[location, _unreachable_location()]):
            snap = fleet.snapshot(force=True)

        issues = snap["issues"]
        codes = [(issue["severity"], issue["code"]) for issue in issues]
        # voice-one repeats its catalogue error per machine, but the page must
        # report the shared condition once.
        self.assertEqual(
            sum(1 for _, code in codes if code == "model_catalog_error"), 1)
        # Errors first, then warnings, then info.
        order = [fleet._ISSUE_SEVERITY[severity] for severity, _ in codes]
        self.assertEqual(order, sorted(order), codes)
        self.assertIn(("error", "location_unreachable"), codes)
        self.assertIn(("error", "machine_quarantined"), codes)
        self.assertIn(("warning", "worker_offline"), codes)
        self.assertIn(("warning", "worker_maintenance"), codes)
        self.assertIn(("info", "model_downloading"), codes)
        for issue in issues:
            self.assertEqual(
                set(issue),
                {"severity", "code", "location_id", "location", "subject",
                 "message", "action"},
            )
            self.assertTrue(issue["message"] and issue["action"], issue)

        summary = snap["summary"]
        self.assertEqual(summary["local_models_total"], 3)
        self.assertEqual(summary["models_available"], 2)
        self.assertEqual(summary["workers_total"], 4)
        self.assertEqual(summary["workers_ready"], 1)
        self.assertEqual(
            summary["issues"],
            sum(1 for issue in issues if issue["severity"] in ("error", "warning")),
        )

    def test_a_running_batch_carries_the_handle_a_cancel_needs(self) -> None:
        # The Fleet page cancels a Hub batch by naming the controller and the
        # batch. A label is not an identity, so the location id and the provider
        # id travel with the row; `batch_id` is the Hub's handle on work this
        # gateway did not itself create.
        rows = fleet.running_flat([{
            "id": "ctrl-1", "label": "Blue Room", "local": False,
            "provider_id": "studio-hub-ctrl-1",
            "running_jobs": [{
                "id": "batch-9", "modality": "image", "model": "m",
                "total": 4, "running": 1, "queued": 2, "retrying": 0, "done": 1,
                "error": 0, "stalled": True, "stalled_after_s": 900,
                "no_progress_s": 1200, "next_retry_at": None, "uncertain": 1,
                "cancel_requested": 0, "cancelled": False, "cancelled_items": 0,
                "routing": "pool", "governor_note": "waiting on a free slot",
                "avg_s": 9.2, "started_at": 5,
            }],
        }])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["batch_id"], "batch-9")
        self.assertEqual(row["location_id"], "ctrl-1")
        self.assertEqual(row["provider_id"], "studio-hub-ctrl-1")
        # The Hub's own reason a batch is held, and its own uncertainty count —
        # the page cannot get these anywhere else.
        self.assertEqual(row["governor_note"], "waiting on a free slot")
        self.assertEqual(row["uncertain"], 1)
        self.assertTrue(row["stalled"])


class IssueDerivationTests(unittest.TestCase):
    def test_machine_controller_and_job_conditions_are_reported(self) -> None:
        row = {
            "id": "site", "label": "Site", "local": False, "reachable": True,
            "role": "controller", "ready": False,
            "machines": [
                {"machine": "m1", "label": "M1", "health": "degraded"},
                {"machine": "m2", "label": "M2"},
                {"machine": "m3", "label": "M3"},
            ],
            "activity": {"pulse": {}, "machines": [
                {"machine": "m2", "state": "offline", "limitation": "no disk"},
            ]},
            "running_jobs": [{"id": "job-1", "stalled": True}],
            "local_models": [], "workers": [], "probe_machines_hidden": 2,
        }
        codes = {issue["code"] for issue in fleet._issues([row])}
        self.assertLessEqual(
            {"controller_drained", "machine_not_healthy", "machine_offline",
             "machine_limited", "machine_unknown_state", "running_stalled",
             "probe_machines_hidden"},
            codes,
        )

    def test_a_hub_condition_repeated_per_machine_is_one_issue(self) -> None:
        # "Direct activity partially unavailable" is the same sentence on every
        # machine at a site, and `state: unknown` is the same kind of measurement
        # gap. Emitted per machine they buried the list under identical rows; they
        # are one line with a count, so the top of the list is only what differs.
        row = {
            "id": "site", "label": "Site", "local": False, "reachable": True,
            "role": "controller", "ready": True,
            "machines": [{"machine": f"m{i}"} for i in range(1, 4)],
            "activity": {"pulse": {}, "machines": [
                {"machine": f"m{i}", "state": "unknown",
                 "limitation": "Direct activity partially unavailable"}
                for i in range(1, 4)
            ]},
            "running_jobs": [], "local_models": [], "workers": [],
            "probe_machines_hidden": 0,
        }
        issues = fleet._issues([row])
        limited = [issue for issue in issues if issue["code"] == "machine_limited"]
        unknown = [issue for issue in issues if issue["code"] == "machine_unknown_state"]
        self.assertEqual(len(limited), 1, limited)
        self.assertEqual(limited[0]["subject"], "3 machines")
        self.assertEqual(limited[0]["message"],
                         "3 machines: Direct activity partially unavailable")
        self.assertEqual(len(unknown), 1, unknown)
        self.assertEqual(unknown[0]["subject"], "3 machines")
        # The gateway says which count it left out of, so "unknown" is not read as
        # "offline".
        self.assertIn("neither online nor offline", unknown[0]["message"])
        self.assertIn("report no state", unknown[0]["message"])

        # One machine reads as one machine, not "1 machine report".
        single = dict(row)
        single["machines"] = [{"machine": "m1"}]
        single["activity"] = {"pulse": {}, "machines": [
            {"machine": "m1", "state": "unknown",
             "limitation": "Direct activity partially unavailable"}]}
        one = fleet._issues([single])
        note = next(issue for issue in one if issue["code"] == "machine_unknown_state")
        self.assertEqual(note["subject"], "1 machine")
        self.assertIn("1 machine reports no state", note["message"])

    def test_an_unreachable_location_is_named_by_its_address(self) -> None:
        # The row already shows the label; the subject is where to reach it, not
        # the bare controller id repeated beside its own name.
        row = {
            "id": "ctrl-1", "label": "Blue Room", "url": "http://10.0.0.9:47873",
            "reachable": False, "error": "No answer within 6s.",
            "machines": [], "activity": {"pulse": {}, "machines": []},
            "running_jobs": [], "local_models": [], "workers": [],
            "probe_machines_hidden": 0,
        }
        issue = next(
            issue for issue in fleet._issues([row]) if issue["code"] == "location_unreachable"
        )
        self.assertEqual(issue["subject"], "http://10.0.0.9:47873")
        self.assertEqual(issue["message"], "No answer within 6s.")


class AdminEnrichmentTests(unittest.TestCase):
    def test_locations_gain_counts_without_the_cache_being_mutated(self) -> None:
        from gateway import admin as admin_module

        original = {"id": "local", "label": "This Mac",
                    "provider_id": "studio-hub-local", "reachable": True}
        cached = {"locations": [original], "summary": {"locations": 1},
                  "issues": [], "generated_at": 0.0}
        models = [
            {"model_id": "m1", "enabled": True},
            {"model_id": "m2", "enabled": False},
            {"model_id": "m3", "enabled": True},
        ]
        definition = types.SimpleNamespace(id="studio-hub-local")
        with patch.object(fleet, "snapshot", return_value=cached), \
                patch.object(admin_module.registry, "definition", return_value=definition), \
                patch.object(admin_module.registry.models, "models_for", return_value=models), \
                patch.object(admin_module.registry.models, "can_publish",
                             side_effect=lambda _definition, model: model["model_id"] != "m2"):
            out = admin_module.fleet_snapshot()

        row = out["locations"][0]
        self.assertEqual(row["models_discovered"], 3)
        self.assertEqual(row["models_enabled"], 2)
        self.assertEqual(row["models_publishable"], 2)
        # The cached row and its list must be untouched: the counts are this
        # route's answer, not a mutation of the shared snapshot.
        self.assertNotIn("models_discovered", original)
        self.assertIsNot(row, original)
        self.assertIsNot(out["locations"], cached["locations"])
        self.assertIs(cached["locations"][0], original)

    def test_an_unknown_provider_publishes_none_not_zero(self) -> None:
        from gateway import admin as admin_module

        cached = {"locations": [{"id": "x", "provider_id": "studio-hub-gone"}],
                  "summary": {}, "issues": []}
        with patch.object(fleet, "snapshot", return_value=cached), \
                patch.object(admin_module.registry, "definition", return_value=None):
            out = admin_module.fleet_snapshot()
        row = out["locations"][0]
        self.assertIsNone(row["models_discovered"])
        self.assertIsNone(row["models_enabled"])
        self.assertIsNone(row["models_publishable"])


class SupplyReductionTests(unittest.TestCase):
    """The per-model counts the router weights by, when the Hub publishes none.

    Hub 0.6.47 sends `capabilities.model_supply` empty while stating, per machine
    and model, whether that machine is memory-eligible and available. Reducing
    those stated booleans is the same count `model_supply` describes — and the
    router must get it, or it keeps falling back to the site-wide machine total.
    """

    def test_per_machine_rows_reduce_to_the_counts_the_router_weights_by(self) -> None:
        rows = [
            {"model": "m/a", "machine": "mac-1", "memory_eligible": True, "available": True},
            {"model": "m/a", "machine": "mac-2", "memory_eligible": True, "available": False},
            {"model": "m/a", "machine": "mac-3", "memory_eligible": False, "available": False},
            {"model": "m/a", "machine": "mac-4", "memory_eligible": None, "available": None},
            {"model": "m/b", "machine": "mac-1", "memory_eligible": True, "available": True},
        ]
        supply = fleet._supply_from_local_models(rows)
        self.assertEqual(supply["m/a"], {"eligible": 2, "available": 1})
        self.assertEqual(supply["m/b"], {"eligible": 1, "available": 1})

    def test_an_unknown_boolean_is_never_counted_as_cannot(self) -> None:
        # `None` is "the Hub did not say", which is not "no machine fits".
        supply = fleet._supply_from_local_models([
            {"model": "m/a", "machine": "mac-1", "memory_eligible": None, "available": None},
        ])
        self.assertEqual(supply["m/a"], {"eligible": 0, "available": 0})

    def test_a_published_hub_set_is_authoritative(self) -> None:
        # Hub 0.6.48 publishes `model_supply`, so our reduction is not merged into
        # it: a model the Hub deliberately left out is "unknown", and re-adding it
        # from our own reduction would overrule a decision the Hub is better placed
        # to make. (The reduction still fills a Hub that publishes nothing — see the
        # probe test above.)
        index = fleet._model_supply_index(
            [{"internal_model_id": "m/a", "eligible_physical_slots_total": 1,
              "available_physical_slots": 1}],
            {"m/a": {"eligible": 9, "available": 9}, "m/b": {"eligible": 2, "available": 0}},
        )
        self.assertEqual(index["m/a"], {"eligible": 1, "available": 1})
        self.assertNotIn("m/b", index)

    def test_an_empty_hub_set_falls_back_wholesale(self) -> None:
        fallback = {"m/a": {"eligible": 2, "available": 1}}
        self.assertEqual(fleet._model_supply_index([], fallback), fallback)
        self.assertEqual(fleet._model_supply_index(None, fallback), fallback)


class TakesCustomerJobsTests(unittest.TestCase):
    """A Hub that refuses customer work is never a routing candidate.

    Measured live 2026-09-23: the gateway routed an image job and a TTS job to this
    Mac's Hub, which answered `409 "This Hub is in agent mode; submit customer jobs
    to a controller."` Both failed while two healthy controllers sat idle.
    """

    def test_an_agent_mode_hub_is_not_a_candidate(self) -> None:
        candidates = _route_with([
            _location("agent", role="agent", drained=True,
                      model_supply={MODEL: {"eligible": 9, "available": 9}}),
            _location("ctrl", role="controller", drained=False,
                      model_supply={MODEL: {"eligible": 1, "available": 1}}),
        ])
        self.assertEqual([c["location_id"] for c in candidates], ["ctrl"])

    def test_a_drained_controller_is_not_a_candidate(self) -> None:
        candidates = _route_with([
            _location("draining", role="controller", drained=True,
                      model_supply={MODEL: {"eligible": 9, "available": 9}}),
            _location("ctrl", role="controller", drained=False,
                      model_supply={MODEL: {"eligible": 1, "available": 1}}),
        ])
        self.assertEqual([c["location_id"] for c in candidates], ["ctrl"])

    def test_a_standalone_hub_still_takes_work(self) -> None:
        # Only `agent` and `drained` disqualify: a standalone Mac is a fleet of one
        # and must keep working. `ready: false` is a controller starting up, not a
        # refusal — treating it as one would lock the catalogue on every restart.
        self.assertTrue(fleet._takes_customer_jobs({"role": "standalone", "ready": False}))
        self.assertTrue(fleet._takes_customer_jobs({"role": "controller", "drained": False}))
        self.assertFalse(fleet._takes_customer_jobs({"role": "agent"}))
        self.assertFalse(fleet._takes_customer_jobs({"role": "controller", "drained": True}))

    def test_the_catalogue_does_not_call_an_agent_usable(self) -> None:
        locations = [
            _location("agent", role="agent", drained=True),
            _location("ctrl", role="controller", drained=False),
            _location("gone", reachable=False),
        ]
        with patch.object(fleet, "snapshot", return_value={"locations": locations}):
            self.assertEqual(fleet.usable_location_ids(), {"ctrl"})


class VoicePinTests(unittest.TestCase):
    """A voice pins its owner's route — unless the owner cannot take the work.

    A voice owned by an agent-mode Hub pinned TTS to a site that answers 409, and
    once agents are excluded from routing the same pin resolved to a 404. The pin
    now holds only while the owner accepts customer work; otherwise the job routes
    to a controller and the adapter stages the reference clip there.
    """

    def test_a_voice_owned_by_an_agent_is_not_pinned(self) -> None:
        rows = [
            _location("local", role="agent", drained=True),
            _location("ctrl", role="controller", drained=False),
        ]
        with patch.object(fleet, "snapshot", return_value={"locations": rows}):
            self.assertIsNone(
                fleet.pinned_location({"voice_id": "studio-hub-local::voice-a"})
            )

    def test_a_voice_owned_by_a_controller_still_pins_it(self) -> None:
        rows = [_location("ctrl", role="controller", drained=False)]
        with patch.object(fleet, "snapshot", return_value={"locations": rows}):
            self.assertEqual(
                fleet.pinned_location({"voice_id": "studio-hub-ctrl::voice-a"}), "ctrl"
            )

    def test_an_owner_the_snapshot_cannot_classify_keeps_the_pin(self) -> None:
        # Misrouting a voice whose clip lives on one Hub is worse than a refusal, so
        # an unknown owner keeps the old behaviour.
        with patch.object(fleet, "snapshot", return_value={"locations": []}):
            self.assertEqual(
                fleet.pinned_location({"voice_id": "studio-hub-ghost::voice-a"}), "ghost"
            )


if __name__ == "__main__":
    unittest.main()
