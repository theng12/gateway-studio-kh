#!/usr/bin/env python3
"""Phase 1 routing regressions for remote final rendering.

Three separate facts are pinned here, and each one is a decision rather than a detail:

* **Support is not availability.** A Mac that is busy, out of free slots or short of
  memory today still *supports* the render profile, so it still belongs in the
  advertised capability set (`fleet.render_candidates(..., available=False)`). It is
  still never *dispatched* to, because dispatch uses the live availability gates.
* **The requested encode chooses the node.** `runtime._render_destination` keeps only
  the candidates whose own published encode document states the requested tuple
  exactly; the first of those wins, and no such candidate means no destination.
* **An unknown document is not a guess.** A candidate with no valid encode document
  cannot select a destination. The legacy unscoped response keeps its intersection.

Offline: no socket, no renderer, no real Hub. The fleet snapshot is a literal fixture,
this Mac's own Studio Hub is pointed away, and the runtime is imported only after the
sandbox environment is set.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-render-routing-")

from gateway import config, fleet, render_optin, runtime  # noqa: E402
from gateway.render_service import RenderServiceError  # noqa: E402


#: The profile digest this Mac's bundled render authority pins.
PROFILE = "a" * 64
NOW = 1_700_000_000.0

#: The exact six fields a Hub's encode document carries, and the tuple a caller asks
#: for. Equality is on all six: a null bitrate is a value, never a wildcard.
REQUESTED = {
    "container": "mp4", "requested_codec": "h264", "requested_effort": "medium",
    "crf": 23, "quality_cpu_preset": "medium", "bitrate_kbps": None,
}
OTHER = {
    "container": "mov", "requested_codec": "h265", "requested_effort": "slow",
    "crf": 18, "quality_cpu_preset": "slow", "bitrate_kbps": 8000,
}

#: What a Hub that was told to accept unqualified work adds to its render row.
ACCEPTING = {"provenance": render_optin.PROVENANCE, "accepted_by": "gateway-admin",
             "accepted_at": NOW}


def encode_document(*entries: dict) -> dict:
    """One Hub's published encode document: the closed format and exact entries."""
    return {"format": fleet.RENDER_ENCODE_FORMAT, "entries": list(entries)}


def worker(machine: str, *, hardware: str = "Apple M4", production: bool = True,
           accepted: object = None, encode: dict | None = None, busy: bool = False,
           slots: int = 1, slots_total: int = 1, **extra: object) -> dict:
    """A `video.final_render` worker row as the controller republishes it.

    A production row is the Hub's own capability row, so it carries the digests, the
    bounds and the capacity as well as the row's own state. `accepted` is the Hub's
    evidence that its owner opted in to unqualified work; such a row is
    production-shaped, exactly as Studio Hub publishes it.
    """
    row: dict[str, object] = {
        "machine": machine, "physical_machine_id": machine,
        "online": True, "ready": True, "busy": busy, "drained": False,
        "maintenance": False, "quarantined": False,
        "supported_operations": ["video.final_render"],
        "hardware_class": hardware, "profile_digest": PROFILE,
        "tools_digest": "4" * 64, "compiler_digest": "5" * 64,
        "qualification_digest": "6" * 64, "selected_tier": "software-h264",
        "exact_encoder": "libx264", "font_support": False,
        "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                   "min_available_memory_bytes": 7_900_000_000, "output_limit_bytes": 1},
        "profile_execution_bounds": {"max_duration_sec": 1},
        "production_eligible": production, "production_enabled": production,
        "available_capacity": {"slots": slots, "slots_total": slots_total},
    }
    if accepted is not None:
        row.update(production_eligible=True, production_enabled=True)
        row[render_optin.OPT_IN_KEY] = accepted
    if encode is not None:
        row["encode"] = encode
    row.update(extra)
    return row


def location(macs: dict[str, dict], *, location_id: str = "roster-a",
             paused: bool = False) -> dict:
    """One reachable roster location. `macs` maps machine id -> its row facts."""
    return {
        "id": location_id, "label": "Shop", "local": False, "reachable": True,
        "ready": True, "drained": False, "paused_at": NOW if paused else None,
        "site_id": "site-a", "controller_id": "hub-a",
        "members_from": None, "members_stale_seconds": None,
        "machines": [{
            "machine": mid, "reachable": True,
            "label": spec.get("label", mid), "chip": spec.get("chip", "Apple M4"),
            "memory_gb": spec.get("memory_gb", 16.0),
            "available_gb": spec.get("available_gb", 8.0),
            "disk_free_gb": spec.get("disk_free_gb", 100.0),
            "current_job": spec.get("current_job", None),
            **({"function_enabled": spec["function_enabled"]}
               if "function_enabled" in spec else {}),
        } for mid, spec in macs.items()],
        "nodes": [{
            "id": mid, "state": spec.get("state", "reachable"),
            "paused": spec.get("paused", False), "current_job": None,
            "on_previous_token": False, "address": spec.get("address"),
            "priority_order": ["video"], "workstation": None,
        } for mid, spec in macs.items()],
        "workers": [spec["worker"] for spec in macs.values()],
    }


class Fleet:
    """Patches the clock, the snapshot and the roster around one block."""

    def __init__(self, *locations: dict) -> None:
        self.view = {"generated_at": NOW, "locations": list(locations)}
        self.patches = [
            mock.patch.object(fleet.time, "time", return_value=NOW),
            mock.patch.object(fleet, "snapshot", return_value=self.view),
            mock.patch.object(fleet, "roster",
                              return_value=[{"id": row["id"]} for row in locations]),
            mock.patch.object(render_optin, "local_networks", return_value=[]),
        ]

    def __enter__(self) -> "Fleet":
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for item in reversed(self.patches):
            item.stop()


def candidate(node_id: str, *, encode: dict | None = None,
              qualification_vector_id: str | None = None) -> dict:
    """One row shaped as `render_candidates` builds it, for the destination selector."""
    capability: dict[str, object] = {"profile_digest": PROFILE}
    if encode is not None:
        capability["encode"] = encode
    return {
        "site_id": "site-a", "roster_controller_id": "roster-a",
        "hub_controller_id": "hub-a", "node_id": node_id,
        "target_hub_controller_id": node_id,
        "qualification_vector_id": qualification_vector_id,
        "capability": capability,
        "selected_machine": {"id": node_id, "label": node_id, "chip": "Apple M4",
                             "memory_gb": 16.0, "hardware_class": "apple-m4-16gb"},
    }


def packet(*, encode: dict = REQUESTED) -> dict:
    """The two facts the destination selector reads: the profile and the encode."""
    return {"recipe": {"profile_digest": PROFILE, "encode": encode}}


class AvailabilityTests(unittest.TestCase):
    """Support (what a Mac can do) and availability (what it can do right now)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="render-routing-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, *locations: dict, available: bool = True) -> list[str]:
        with Fleet(*locations):
            return [row["node_id"] for row in
                    fleet.render_candidates(PROFILE, available=available)]

    def test_a_busy_mac_is_still_support_but_is_never_a_destination(self) -> None:
        """A Mac busy with another job still supports the profile; it is not sent work."""
        macs = {"busy": {"worker": worker("busy", busy=True)}}
        self.assertEqual(self.rows(location(macs), available=False), ["busy"])
        self.assertEqual(self.rows(location(macs)), [])

    def test_a_mac_with_no_free_slot_is_still_support_but_never_a_destination(self) -> None:
        """No free slot is a moment, not a missing capability."""
        macs = {"full": {"worker": worker("full", slots=0)}}
        self.assertEqual(self.rows(location(macs), available=False), ["full"])
        self.assertEqual(self.rows(location(macs)), [])

    def test_a_mac_short_of_memory_is_still_support_but_never_a_destination(self) -> None:
        """Free memory is a moment too: the bound the render needs is the Mac's own."""
        macs = {"tight": {"worker": worker("tight"), "available_gb": 7.0}}
        self.assertEqual(self.rows(location(macs), available=False), ["tight"])
        self.assertEqual(self.rows(location(macs)), [])

    def test_exact_probe_defers_only_the_old_tiers_memory_bound(self) -> None:
        macs = {"tight": {"worker": worker("tight"), "available_gb": 7.0}}
        with Fleet(location(macs)):
            self.assertEqual(fleet.render_candidates(PROFILE), [])
            rows = fleet.render_candidates(PROFILE, resolve_encode_authority=True)
            self.assertEqual([row["node_id"] for row in rows], ["tight"])
            self.assertLess(rows[0]["available_memory_source"]["conservative_bytes"],
                            rows[0]["capability"]["bounds"]["min_available_memory_bytes"])
        macs["tight"]["worker"]["busy"] = True
        with Fleet(location(macs)):
            self.assertEqual(fleet.render_candidates(PROFILE, resolve_encode_authority=True), [])

    def test_a_drained_mac_is_not_support_at_all(self) -> None:
        """Drained is the Mac saying it takes no work, so it is not published either."""
        macs = {"drained": {"worker": worker("drained", drained=True)}}
        self.assertEqual(self.rows(location(macs), available=False), [])
        self.assertEqual(self.rows(location(macs)), [])

    def test_the_owners_switch_still_gates_an_unqualified_mac_in_both_modes(self) -> None:
        """The owner's per-Mac switch is authority, not availability: it always applies."""
        macs = {"opted-in": {"worker": worker("opted-in", accepted=ACCEPTING)}}
        self.assertEqual(self.rows(location(macs), available=False), [])
        # A legacy unqualified tier is provisional when another tuple may be measured.
        with Fleet(location(macs)):
            provisional = fleet.render_candidates(PROFILE, available=False, resolve_encode_authority=True)
            self.assertEqual([row["node_id"] for row in provisional], ["opted-in"])
            self.assertEqual(provisional[0]["authority"], render_optin.ACCEPTED)
        render_optin.set_enabled("roster-a", "opted-in", True)
        self.assertEqual(self.rows(location(macs), available=False), ["opted-in"])

    def test_a_paused_location_keeps_its_macs_out_unless_render_is_exempt(self) -> None:
        """The pause exemption is that same pause, for render only, and it is not availability."""
        macs = {"paused": {"worker": worker("paused")}}
        self.assertEqual(self.rows(location(macs, paused=True), available=False), [])
        exempt = {"paused": {"worker": worker("paused", render_while_paused=True)}}
        self.assertEqual(self.rows(location(exempt, paused=True), available=False), ["paused"])

    def test_a_worker_whose_acceptance_evidence_is_invalid_is_never_a_candidate(self) -> None:
        """A claim that is not the Hub's evidence cannot be told from a qualified row."""
        macs = {"bogus": {"worker": worker("bogus", accepted={"provenance": "something-else"})}}
        self.assertEqual(self.rows(location(macs), available=False), [])
        render_optin.set_enabled("roster-a", "bogus", True)
        self.assertEqual(self.rows(location(macs), available=False), [])

    def test_the_render_function_switch_keeps_a_mac_out_in_both_modes(self) -> None:
        """Final render switched off is the owner's work order, not a busy moment."""
        macs = {"off": {"worker": worker("off"), "function_enabled": {"render": False}}}
        self.assertEqual(self.rows(location(macs), available=False), [])
        self.assertEqual(self.rows(location(macs)), [])

    def test_a_mac_of_any_chip_family_is_admitted_on_its_capability(self) -> None:
        """The capability decides, not the name of the chip: an M3 renders like an M4."""
        macs = {"m3": {"worker": worker("m3", hardware="Apple M3"), "chip": "Apple M3"}}
        self.assertEqual(self.rows(location(macs)), ["m3"])


class EncodeSupportTests(unittest.TestCase):
    """An exact tuple is supported only when the node explicitly states it."""

    def test_supports_matches_only_an_exactly_published_entry(self) -> None:
        document = encode_document(REQUESTED)
        self.assertTrue(fleet.render_encode_supports(document, REQUESTED))
        # One field off is another tuple, so it is not the one this Mac published.
        self.assertFalse(fleet.render_encode_supports(
            document, {**REQUESTED, "crf": 18}))
        self.assertFalse(fleet.render_encode_supports(
            document, {**REQUESTED, "requested_effort": "hardware"}))
        # A null bitrate is a value, not a wildcard over every bitrate.
        self.assertFalse(fleet.render_encode_supports(
            document, {**REQUESTED, "bitrate_kbps": 8000}))
        # An unknown document supports nothing, and neither does a malformed request.
        self.assertFalse(fleet.render_encode_supports(None, REQUESTED))
        self.assertFalse(fleet.render_encode_supports({}, REQUESTED))
        self.assertFalse(fleet.render_encode_supports(document, {**REQUESTED, "extra": 1}))


class DestinationTests(unittest.TestCase):
    """Routing order with an exact-resolver stub; integration tests validate real evidence."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="render-routing-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

        resolver = mock.patch.object(runtime, "probed_render_candidates", side_effect=
            lambda rows, requested, _client, **_kwargs: [row for row in rows
                if fleet.render_encode_supports(row["capability"].get("encode"), requested)])
        resolver.start()
        self.addCleanup(resolver.stop)

    def select(self, *rows: list[dict]) -> object:
        """The destination for one packet, with the candidate list patched in."""
        with mock.patch.object(runtime.fleet_module, "render_candidates",
                               side_effect=list(rows)):
            return runtime._render_destination(packet())

    def test_the_first_candidate_that_states_the_requested_encode_wins(self) -> None:
        """A candidate that cannot do this exact encode is skipped, not failed."""
        destination = self.select([
            candidate("no-encode"),
            candidate("other-encode", encode=encode_document(OTHER)),
            candidate("matching", encode=encode_document(REQUESTED)),
        ])
        self.assertEqual(destination.node_id, "matching")

    def test_the_qualification_fallback_is_also_filtered_by_the_requested_encode(self) -> None:
        """No production candidate states it, so a qualification candidate may."""
        destination = self.select(
            [candidate("production", encode=encode_document(OTHER))],
            [candidate("qualification", encode=encode_document(REQUESTED),
                       qualification_vector_id="vec-1")],
        )
        self.assertEqual(destination.node_id, "qualification")
        self.assertEqual(destination.qualification_vector_id, "vec-1")

    def test_no_candidate_states_the_requested_encode_so_there_is_no_destination(self) -> None:
        with self.assertRaises(RenderServiceError) as caught:
            self.select(
                [candidate("production", encode=encode_document(OTHER))],
                [candidate("qualification", encode=encode_document(OTHER),
                           qualification_vector_id="vec-1")],
            )
        self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")

    def test_controller_self_destination_pins_the_verified_credential_kind(self) -> None:
        own = candidate(fleet.LOCAL_ID, encode=encode_document(REQUESTED))
        own["target_hub_controller_id"] = own["hub_controller_id"]
        for stated, expected in (("operator", "hub"), ("fleet", "fleet")):
            with self.subTest(scope=stated), mock.patch.object(
                    fleet, "credential_scope_for", return_value={"scope": stated}):
                selected = self.select([own])
                self.assertEqual(selected.hub_credential_kind, expected)
        with mock.patch.object(fleet, "credential_scope_for", return_value={"scope": "unknown"}):
            with self.assertRaises(RenderServiceError):
                self.select([own])
            selected = self.select([own, candidate("leaf", encode=encode_document(REQUESTED))])
            self.assertEqual(selected.node_id, "leaf")
            self.assertEqual(selected.hub_credential_kind, "fleet")
        own["roster_controller_id"] = fleet.LOCAL_ID
        with mock.patch.object(fleet, "credential_scope_for") as probe:
            self.assertEqual(self.select([own]).hub_credential_kind, "hub")
            probe.assert_not_called()

    def test_a_busy_mac_is_never_picked_over_an_idle_one_that_states_the_encode(self) -> None:
        """The live availability gates still decide; the encode only narrows the list."""
        macs = {
            "busy": {"worker": worker("busy", busy=True, encode=encode_document(REQUESTED))},
            "idle": {"worker": worker("idle", encode=encode_document(REQUESTED))},
        }
        with Fleet(location(macs)):
            self.assertEqual([row["node_id"] for row in fleet.render_candidates(PROFILE)],
                             ["idle"])
            destination = runtime._render_destination(packet())
        self.assertEqual(destination.node_id, "idle")


if __name__ == "__main__":
    unittest.main()
