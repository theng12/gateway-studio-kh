"""Scoped late-label capability consumption: envelope gates, legacy isolation, probe query.

Offline: no socket, no real Hub. The genuine descriptor triple is staged by the main
session and never read here — every digest in this file is synthetic test data.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
import unittest

import httpx
from unittest import mock
from urllib.parse import parse_qs

from tests import test_render_service as fixtures  # sandbox + gateway import

from gateway import fleet, render_optin, runtime
from gateway.render_contract import LATE_LABEL_CONTRACT
from gateway.render_service import (RegisteredHubRenderClient, RenderDestination,
                                    RenderServiceError, probed_render_candidates)

NOW = 1_700_000_000.0
LEGACY = "a" * 64
V3 = "b" * 64
WRONG = "c" * 64
ENVELOPE_FORMAT = "studio.render.hub-late-label-capabilities.v1"
REQUESTED = {"container": "mp4", "requested_codec": "h264", "requested_effort": "medium",
             "crf": 23, "quality_cpu_preset": "medium", "bitrate_kbps": None}


def envelope(worker, *, reason=None, server_time=int(NOW), contract=LATE_LABEL_CONTRACT,
             fmt=ENVELOPE_FORMAT):
    return {"format": fmt, "requested_contract": contract, "server_time": server_time,
            "worker": worker, "reason": reason}


def capability_row(digest=V3, *, production=True, **extra):
    row = {
        "hardware_class": "Apple M4", "profile_digest": digest,
        "tools_digest": "4" * 64, "compiler_digest": "5" * 64,
        "qualification_digest": "6" * 64, "selected_tier": "software-h264",
        "exact_encoder": "libx264", "font_support": False,
        "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                   "min_available_memory_bytes": 7_900_000_000, "output_limit_bytes": 1},
        "profile_execution_bounds": {"max_duration_sec": 1},
        "production_eligible": production, "production_enabled": production,
        "available_capacity": {"slots": 1, "slots_total": 1},
    }
    row.update(extra)
    return row


def worker(machine, digest=LEGACY, **extra):
    """A snapshot worker row: operational facts plus the legacy capability Hub still publishes."""
    row = {
        "machine": machine, "physical_machine_id": machine,
        "online": True, "ready": True, "busy": False, "drained": False,
        "maintenance": False, "quarantined": False,
        "supported_operations": ["video.final_render"],
        **capability_row(digest),
    }
    row.update(extra)
    return row


def location(macs, *, location_id="roster-a", paused=False):
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
    def __init__(self, *locations):
        self.view = {"generated_at": NOW, "locations": list(locations)}
        self.patches = [
            mock.patch.object(fleet.time, "time", return_value=NOW),
            mock.patch.object(fleet, "snapshot", return_value=self.view),
            mock.patch.object(fleet, "roster",
                              return_value=[{"id": row["id"]} for row in locations]),
            mock.patch.object(render_optin, "local_networks", return_value=[]),
        ]

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


def destination_row():
    return {
        "site_id": "site-a", "roster_controller_id": "roster-a",
        "hub_controller_id": "hub-a", "node_id": "node-m4-a",
        "target_hub_controller_id": "node-m4-a", "qualification_vector_id": None,
        "capability": {"profile_digest": V3},
        "available_memory_source": {"conservative_bytes": 8_000_000_000},
        "selected_machine": {"id": "node-m4-a", "label": "M4", "chip": "Apple M4",
                             "memory_gb": 16, "hardware_class": "Apple M4"},
    }


class ScopedEnvelopeDecodeTests(unittest.TestCase):
    def decode(self, payload, *, contract=LATE_LABEL_CONTRACT, now=NOW):
        return fleet._decode_scoped_capabilities(payload, requested_contract=contract, now=now)

    def test_valid_worker_and_measured_absence_both_decode(self):
        row = capability_row()
        self.assertEqual(self.decode(envelope(row)), row)
        self.assertIsNone(self.decode(envelope(None, reason="no v3 worker on this node yet")))

    def test_every_malformed_shape_fails_closed(self):
        good = capability_row()
        cases = [
            {},  # not the closed five keys
            {**envelope(good), "extra": 1},
            {key: value for key, value in envelope(good).items() if key != "reason"},
            envelope(good, fmt="studio.render.hub-late-label-capabilities.v9"),
            envelope(good, contract="studio.render.late-label.v2"),
            envelope(good, server_time=True),
            envelope(good, server_time=0),
            envelope(good, server_time=int(NOW) - 1000),
            envelope(good, server_time=int(NOW) + 1000),
            envelope(good, reason="worker present cannot carry a reason"),
            envelope(None, reason=""),
            envelope(None, reason="x" * 1025),
            envelope("not a worker"),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(fleet.ScopedRenderCapabilitiesError):
                self.decode(payload)

    def test_unknown_contract_is_never_confused_with_the_late_label_read(self):
        with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
            self.decode(envelope(capability_row(), contract="studio.render.other.v1"),
                        contract=LATE_LABEL_CONTRACT)


class ScopedTransportTests(unittest.TestCase):
    def fetch(self, response):
        with mock.patch.object(fleet, "roster", return_value=[
            {"id": "registered", "url": "http://controller.invalid"},
        ]), mock.patch.object(fleet, "_token_for", return_value="fixture-token"), \
                mock.patch.object(fleet.httpx, "stream", return_value=nullcontext(response)) as send:
            result = fleet._scoped_capability_payload("registered", "node/escaped", LATE_LABEL_CONTRACT)
        return result, send

    def test_registered_route_escapes_node_and_uses_scoped_query(self):
        payload = envelope(capability_row())
        result, send = self.fetch(httpx.Response(200, json=payload))
        self.assertEqual(result, payload)
        method, url = send.call_args.args
        self.assertEqual(method, "GET")
        self.assertIn("/api/hub/fleet/nodes/node%2Fescaped/final-renders/capabilities?", url)
        self.assertEqual(parse_qs(url.split("?", 1)[1]),
                         {"requested_contract": [LATE_LABEL_CONTRACT]})
        self.assertFalse(send.call_args.kwargs["follow_redirects"])

    def test_http_oversize_and_duplicate_key_responses_fail_closed(self):
        for response in (httpx.Response(302, content=b""),
                         httpx.Response(200, content=b"x" * (256 * 1024 + 1)),
                         httpx.Response(200, content=b'{"worker":null,"worker":{}}')):
            with self.subTest(response=response.status_code), self.assertRaises(
                fleet.ScopedRenderCapabilitiesError,
            ):
                self.fetch(response)


class ScopedFleetCandidateTests(unittest.TestCase):
    def rows(self, *locations, digest=V3, contract=LATE_LABEL_CONTRACT, fetch=None):
        patcher = fetch or mock.patch.object(fleet, "_scoped_capability_payload",
                                             return_value=envelope(capability_row()))
        with Fleet(*locations), patcher as payload:
            rows = fleet.render_candidates(digest, requested_contract=contract)
        return [row["node_id"] for row in rows], payload

    def test_legacy_no_query_never_touches_the_scoped_route(self):
        macs = {"m1": {"worker": worker("m1")}}
        with Fleet(location(macs)), mock.patch.object(
                fleet, "_scoped_capability_payload",
                side_effect=AssertionError("scoped route touched")):
            rows = fleet.render_candidates(LEGACY)
        self.assertEqual([row["node_id"] for row in rows], ["m1"])

    def test_scoped_read_admits_the_matching_envelope_over_a_legacy_snapshot_row(self):
        macs = {"m1": {"worker": worker("m1")}}  # snapshot still pins the legacy profile
        names, payload = self.rows(location(macs))
        self.assertEqual(names, ["m1"])
        payload.assert_called_once_with("roster-a", "m1", LATE_LABEL_CONTRACT)

    def test_scoped_transport_or_envelope_failure_is_never_measured_zero(self):
        macs = {"m1": {"worker": worker("m1")}}
        for fetch in (mock.patch.object(fleet, "_scoped_capability_payload",
                                        side_effect=fleet.ScopedRenderCapabilitiesError("down")),
                      mock.patch.object(fleet, "_scoped_capability_payload",
                                        return_value={}),
                      mock.patch.object(fleet, "_scoped_capability_payload",
                                        return_value=envelope(capability_row(),
                                                              server_time=int(NOW) - 1000))):
            with fetch, Fleet(location(macs)), self.assertRaises(
                    fleet.ScopedRenderCapabilitiesError):
                fleet.render_candidates(V3, requested_contract=LATE_LABEL_CONTRACT)

    def test_absence_with_reason_is_a_measured_empty_answer(self):
        macs = {"m1": {"worker": worker("m1")}}
        names, _ = self.rows(location(macs),
                             fetch=mock.patch.object(fleet, "_scoped_capability_payload",
                                                     return_value=envelope(
                                                         None, reason="v3 authority not granted")))
        self.assertEqual(names, [])

    def test_a_row_pin_for_another_profile_fails_closed(self):
        macs = {"m1": {"worker": worker("m1")}}
        with Fleet(location(macs)), mock.patch.object(
                fleet, "_scoped_capability_payload",
                return_value=envelope(capability_row(WRONG))):
            with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
                fleet.render_candidates(V3, requested_contract=LATE_LABEL_CONTRACT)

    def test_protocol_support_without_production_admission_admits_nothing(self):
        macs = {"m1": {"worker": worker("m1")}}
        names, _ = self.rows(location(macs),
                             fetch=mock.patch.object(
                                 fleet, "_scoped_capability_payload",
                                 return_value=envelope(capability_row(production=False))))
        self.assertEqual(names, [])

    def test_paused_and_switched_off_nodes_are_never_fetched_or_admitted(self):
        paused = {"m1": {"worker": worker("m1")}}
        names, payload = self.rows(location(paused, paused=True))
        self.assertEqual(names, [])
        payload.assert_not_called()

        off = {"m1": {"worker": worker("m1"), "function_enabled": {"render": False}}}
        names, payload = self.rows(location(off))
        self.assertEqual(names, [])
        payload.assert_not_called()  # an Off node is dropped before any scoped read

    def test_unknown_contract_returns_legacy_empty_without_a_fetch(self):
        macs = {"m1": {"worker": worker("m1")}}
        with Fleet(location(macs)), mock.patch.object(
                fleet, "_scoped_capability_payload",
                side_effect=AssertionError("unknown contract must not fetch")):
            self.assertEqual(
                fleet.render_candidates(V3, requested_contract="studio.render.other.v1"), [])


class ScopedDestinationTests(unittest.TestCase):
    def setUp(self):
        self.saved = runtime.render_service.late_label_profile_digest

    def tearDown(self):
        runtime.render_service.late_label_profile_digest = self.saved

    def packet(self, digest):
        return {"recipe": {"profile_digest": digest, "encode": REQUESTED}}

    def test_only_the_late_profile_packet_threads_the_contract(self):
        runtime.render_service.late_label_profile_digest = V3
        legacy = hashlib.sha256(fixtures.PROFILE_RAW).hexdigest()
        row = destination_row()
        with mock.patch.object(runtime.fleet_module, "render_candidates",
                               side_effect=[[row]]) as selected, \
                mock.patch.object(runtime, "probed_render_candidates",
                                  side_effect=lambda rows, *_args, **_kwargs: rows) as probed:
            destination = runtime._render_destination(self.packet(V3))
        self.assertEqual(destination.node_id, "node-m4-a")
        self.assertEqual(selected.call_args.kwargs,
                         {"resolve_encode_authority": True,
                          "requested_contract": LATE_LABEL_CONTRACT})
        self.assertEqual(probed.call_args.kwargs.get("requested_contract"), LATE_LABEL_CONTRACT)

        with mock.patch.object(runtime.fleet_module, "render_candidates",
                               side_effect=[[row]]) as selected, \
                mock.patch.object(runtime, "probed_render_candidates",
                                  side_effect=lambda rows, *_args, **_kwargs: rows) as probed:
            runtime._render_destination(self.packet(legacy))
        self.assertEqual(selected.call_args.kwargs, {"resolve_encode_authority": True})
        self.assertNotIn("requested_contract", probed.call_args.kwargs)

    def test_scoped_read_failure_becomes_a_loud_destination_refusal(self):
        runtime.render_service.late_label_profile_digest = V3
        with mock.patch.object(runtime.fleet_module, "render_candidates",
                               side_effect=fleet.ScopedRenderCapabilitiesError("down")):
            with self.assertRaises(RenderServiceError) as caught:
                runtime._render_destination(self.packet(V3))
        self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")
        self.assertEqual(caught.exception.status, 503)


class ScopedCapabilitiesTests(fixtures.RenderServiceCase):
    def setUp(self):
        super().setUp()
        profile = json.loads(fixtures.PROFILE_RAW)
        profile.update(format='studio.render.profile.v3',
                       implementation='story-classic-late-label-v3',
                       combination_set='retained-classic-late-label-candidate-v3',
                       production_eligible=False)
        self.late_raw = fixtures.canonical(profile)
        self.service.late_label_profile_descriptor = self.late_raw
        self.service.late_label_profile = profile
        self.service.late_label_profile_digest = hashlib.sha256(self.late_raw).hexdigest()
        self.service.late_label_profile_facts = copy.deepcopy(self.service.profile_facts)

    def test_scoped_discovery_passes_the_contract_and_legacy_does_not(self):
        with mock.patch('gateway.render_service.fleet.render_candidates',
                        return_value=[]) as candidates:
            self.service.capabilities(fixtures.principal(),
                                      requested_contract=LATE_LABEL_CONTRACT)
        self.assertEqual(len(candidates.call_args_list), 2)
        for one in candidates.call_args_list:
            self.assertEqual(one.kwargs.get('requested_contract'), LATE_LABEL_CONTRACT)
        with mock.patch('gateway.render_service.fleet.render_candidates',
                        return_value=[]) as candidates:
            self.service.capabilities(fixtures.principal())
        for one in candidates.call_args_list:
            self.assertNotIn('requested_contract', one.kwargs)


class RecorderHub:
    def __init__(self):
        self.kwargs = None

    def encode_probe(self, destination, requested, **kwargs):
        self.kwargs = kwargs
        return {}


class EncodeProbeContractTests(unittest.TestCase):
    def destination(self):
        return RenderDestination("site-a", fleet.LOCAL_ID, "controller-a", fleet.LOCAL_ID,
                                 hub_credential_kind="hub")

    def test_probe_query_carries_the_contract_only_when_scoped(self):
        client = RegisteredHubRenderClient()
        with mock.patch.object(client, "json", return_value={}) as send:
            client.encode_probe(self.destination(), REQUESTED)
            args = send.call_args.args
            self.assertEqual(args[1], "GET")
            parsed = parse_qs(args[2].split("?", 1)[1])
            self.assertEqual(json.loads(parsed["requested_encode"][0]), REQUESTED)
            self.assertNotIn("requested_contract", parsed)

            client.encode_probe(self.destination(), REQUESTED, start=True)
            self.assertEqual(send.call_args.args[1:], ("POST", "/encode-probes"))

            client.encode_probe(self.destination(), REQUESTED,
                                requested_contract=LATE_LABEL_CONTRACT)
            parsed = parse_qs(send.call_args.args[2].split("?", 1)[1])
            self.assertEqual(parsed["requested_contract"], [LATE_LABEL_CONTRACT])

            client.encode_probe(self.destination(), REQUESTED, start=True,
                                requested_contract=LATE_LABEL_CONTRACT)
            self.assertEqual(send.call_args.args[1], "POST")
            self.assertIn("requested_contract=" + LATE_LABEL_CONTRACT, send.call_args.args[2])
            body = json.loads(send.call_args.kwargs["body"])
            self.assertEqual(set(body), {"format", "requested_encode"})

        with self.assertRaises(RenderServiceError) as caught:
            client.encode_probe(self.destination(), REQUESTED,
                                requested_contract="studio.render.other.v1")
        self.assertEqual(caught.exception.code, "RENDER_REQUEST_INVALID")

    def test_scoped_probe_unknown_is_loud_while_legacy_keeps_its_behavior(self):
        for failure in (httpx.ReadTimeout("fixture timeout"), ValueError("bad authority")):
            hub = mock.Mock()
            hub.encode_probe.side_effect = failure
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
                    probed_render_candidates([destination_row()], REQUESTED, hub,
                                              requested_contract=LATE_LABEL_CONTRACT)
                self.assertEqual(probed_render_candidates([destination_row()], REQUESTED, hub), [])

    def test_scoped_valid_unmeasured_authority_can_still_report_no_permission(self):
        with mock.patch("gateway.render_service.execution_authority", return_value=None):
            self.assertEqual(probed_render_candidates(
                [destination_row()], REQUESTED, RecorderHub(),
                requested_contract=LATE_LABEL_CONTRACT,
            ), [])

    def test_probed_candidates_forward_only_the_scoped_contract(self):
        authority = {"authority_kind": "measured", "qualification_digest": "6" * 64,
                     "selected_tier": "software-h264", "exact_encoder": "libx264",
                     "font_support": False,
                     "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                                "min_available_memory_bytes": 1, "output_limit_bytes": 1},
                     "profile_execution_bounds": {"max_duration_sec": 1}}
        hub = RecorderHub()
        with mock.patch("gateway.render_service.destination_for_candidate",
                        return_value=object()), \
                mock.patch("gateway.render_service.execution_authority",
                           return_value=authority), \
                mock.patch.object(fleet, "_render_candidates_order", side_effect=lambda rows: rows):
            result = probed_render_candidates([destination_row()], REQUESTED, hub,
                                              requested_contract=LATE_LABEL_CONTRACT)
            self.assertEqual(len(result), 1)
            self.assertEqual(hub.kwargs, {"requested_contract": LATE_LABEL_CONTRACT})
            probed_render_candidates([destination_row()], REQUESTED, hub)
            self.assertEqual(hub.kwargs, {})
        with self.assertRaises(RenderServiceError) as caught:
            probed_render_candidates([destination_row()], REQUESTED, RecorderHub(),
                                     requested_contract="studio.render.other.v1")
        self.assertEqual(caught.exception.code, "RENDER_REQUEST_INVALID")


if __name__ == '__main__':
    unittest.main()
