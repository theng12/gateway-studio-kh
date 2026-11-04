"""Offline exact evidence, scoped discovery, immutable dispatch and mixed-reader checks."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_render_service as fixture  # Installs the isolated profile before runtime imports.
from gateway import api, fleet, runtime
from gateway.render_encode import evidence_digest, requested_encode_query
from gateway.render_service import (
    RegisteredHubRenderClient, RenderServiceError, _GATEWAY_RECEIPT_KEYS,
    probed_render_candidates,
)
from starlette.requests import Request


class ExactEncodeTests(fixture.RenderServiceCase):
    _row = fixture.DispatchLifecycleTests._row
    _stage_inputs_and_prepare = fixture.DispatchLifecycleTests._stage_inputs_and_prepare

    def setUp(self):
        super().setUp()
        self.raw = self.request()
        self.requested = json.loads(self.raw)["packet"]["recipe"]["encode"]
        evidence = json.loads((fixture.ROOT / "tests/fixtures/render_encode_probe_evidence.json").read_text())["cases"][0]["evidence"]
        evidence.update(requested_encode=self.requested,
                        node_identity={"physical_node_id": self.destination.node_id},
                        selected_tier="software-h264", exact_encoder="libx264")
        evidence.update({key: fixture.CAPABILITY_SNAPSHOT[key] for key in
                         ("profile_digest", "tools_digest", "compiler_digest")})
        evidence["hardware_identity"].update(hardware_class="apple-m4-16gb", chip="Apple M4", backend="libx264")
        self.binding = {"format": "studio.render.encode-binding.v1", "authority_digest": evidence_digest(evidence),
                        "selected_tier": "software-h264", "exact_encoder": "libx264"}
        bounds = {"max_rss_bytes": 100, "max_wall_seconds": 100,
                  "min_available_memory_bytes": 100, "output_limit_bytes": 32 * 1024 * 1024}
        authority = {"format": "studio.render.encode-execution-authority.v1", "requested_encode": self.requested,
                     "encode_authority": self.binding, "authority_kind": "measured", "grant": None,
                     "hardware_class": "apple-m4-16gb", "font_support": False, "bounds": bounds,
                     "exact_encoder": "libx264", **{key: fixture.CAPABILITY_SNAPSHOT[key] for key in
                         ("profile_digest", "tools_digest", "compiler_digest", "qualification_digest",
                          "selected_tier", "profile_execution_bounds")}}
        self.probe = {"format": "studio.render.encode-probe-state.v1", "requested_encode": self.requested,
                      "state": "supported", "probe_key": "1" * 64, "reason": None, "evidence": evidence,
                      "authority_digest": evidence_digest(evidence), "execution_authority": authority}
        capability = {**fixture.CAPABILITY_SNAPSHOT, "hardware_class": "apple-m4-16gb",
                      "bounds": bounds, "exact_encoder": "libx264", "font_support": False,
                      "encode": {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [self.requested]}}
        self.destination = replace(self.destination, capability_snapshot=capability,
            capability_snapshot_sha256=hashlib.sha256(fixture.canonical(capability)).hexdigest(),
            target_hub_controller_id=self.destination.node_id, encode_probe=self.probe)
        self.candidate = {"site_id": self.destination.site_id,
                          "roster_controller_id": self.destination.roster_controller_id,
                          "hub_controller_id": self.destination.hub_controller_id,
                          "node_id": self.destination.node_id, "target_hub_controller_id": self.destination.node_id,
                          "selected_machine": self.destination.selected_machine, "capability": capability,
                          "available_memory_source": {"conservative_bytes": 1000},
                          "measured_render_speed": None, "priority_rank": 0}

    def _hub_job(self, row, **kwargs):
        return {**fixture.DispatchLifecycleTests._hub_job(self, row, **kwargs), "encode_authority": self.binding}

    def _receipt(self, row, **kwargs):
        receipt = fixture.DispatchLifecycleTests._receipt(row, **kwargs)
        receipt["encode_authority"] = self.binding
        receipt["receipt_digest"] = hashlib.sha256(fixture.canonical({
            key: value for key, value in receipt.items() if key != "receipt_digest"})).hexdigest()
        return receipt

    def test_scoped_discovery_echoes_only_exact_permission_and_separates_busy(self):
        self.hub.answers["encode_probe"] = [self.probe]
        with patch.object(fleet, "render_candidates", side_effect=[[self.candidate], []]):
            answer = self.service.capabilities(fixture.principal(), requested_encode=self.requested)
        self.assertEqual(answer["requested_encode"], self.requested)
        self.assertEqual(answer["encode"]["entries"], [self.requested])
        self.assertEqual((answer["eligible"], answer["idle"]), (1, 0))
        self.assertEqual([name for name, _ in self.hub.calls], ["encode_probe"])
        self.assertEqual(self.hub.calls[0][1][1], {})  # No explicit probe start.

    def test_probe_without_permission_or_wrong_node_is_never_offered(self):
        for changed in ({**self.probe, "execution_authority": None},
                        {**self.probe, "requested_encode": {**self.requested, "bitrate_kbps": 8000}}):
            self.hub.answers["encode_probe"] = [changed]
            self.assertEqual(probed_render_candidates([self.candidate], self.requested, self.hub), [])

    def test_dispatch_rechecks_exact_current_memory_requirement(self):
        row = {**self.candidate, "available_memory_source": {"conservative_bytes": 99}}
        self.hub.answers["encode_probe"] = [self.probe, self.probe]
        self.assertEqual(len(probed_render_candidates([row], self.requested, self.hub)), 1)
        self.assertEqual(probed_render_candidates([row], self.requested, self.hub, available=True), [])

    def test_measured_tuple_does_not_inherit_a_legacy_tiers_owner_grant(self):
        legacy = {**self.candidate, "authority": "accepted_unqualified", "network": "unknown",
                  "capability": {**self.candidate["capability"], "accepted_unqualified": {
                      "provenance": "owner-opt-in-unqualified", "accepted_by": "fixture-owner", "accepted_at": 1}}}
        self.hub.answers["encode_probe"] = [self.probe]
        with patch.object(fixture.render_optin, "enabled_keys", return_value=set()):
            resolved = probed_render_candidates([legacy], self.requested, self.hub)
        self.assertEqual(len(resolved), 1)
        self.assertNotIn("authority", resolved[0])
        self.assertNotIn("accepted_unqualified", resolved[0]["capability"])

    def test_owner_grant_cannot_override_off_or_replaced_original_provenance(self):
        original = {"sha256": "3" * 64, "provenance": "owner-opt-in-unqualified",
                    "accepted_by": "fixture-owner", "accepted_at": 1700000000}
        self.probe["execution_authority"].update(authority_kind="accepted_unqualified", grant={
            "format": "studio.render.accept-unqualified-probed-v2.v1", "digest": "2" * 64,
            "v1": original, "migration": {"authorization_id": "fixture-grant", "authorized_at": 1700000001},
            "node": {"physical_node_id": self.destination.node_id}})
        row = {**self.candidate, "authority": "accepted_unqualified", "network": "unknown",
               "capability": {**self.candidate["capability"], "accepted_unqualified": {
                   key: original[key] for key in ("provenance", "accepted_by", "accepted_at")}}}
        key = f"{row['roster_controller_id']}/{row['node_id']}"
        with patch.object(fixture.render_optin, "enabled_keys", return_value=set()):
            self.hub.answers["encode_probe"] = [self.probe]
            self.assertEqual(probed_render_candidates([row], self.requested, self.hub), [])
        with patch.object(fixture.render_optin, "enabled_keys", return_value={key}):
            self.hub.answers["encode_probe"] = [self.probe, self.probe]
            self.assertEqual(len(probed_render_candidates([row], self.requested, self.hub)), 1)
            row["capability"]["accepted_unqualified"]["accepted_at"] += 1
            self.assertEqual(probed_render_candidates([row], self.requested, self.hub), [])
        stored = {"destination_encode_probe": fixture.canonical(self.probe),
                  "destination_controller": row["roster_controller_id"], "destination_node": row["node_id"],
                  "owner_scope": fixture.OWNER, "gateway_instance_id": fixture.GATEWAY}
        with patch.object(fixture.render_optin, "enabled_keys", return_value=set()):
            self.assertFalse(self.service._fresh_send_allowed(stored))

    def test_runtime_selects_the_probed_destination(self):
        self.hub.answers["encode_probe"] = [self.probe]
        with patch.object(runtime, "render_service", self.service), patch.object(
                fleet, "render_candidates", return_value=[self.candidate]):
            destination = runtime._render_destination(json.loads(self.raw)["packet"])
        self.assertEqual(destination.encode_probe, self.probe)
        self.assertEqual(destination.node_id, self.destination.node_id)

    def test_exact_acceptance_persists_evidence_and_original_replay(self):
        accepted = self.service.accept(fixture.principal(), "exact-replay", self.raw)
        row = self._row(accepted["id"])
        self.assertEqual(self.service._destination(row).encode_probe, self.probe)
        self.service.destination_selector = lambda _packet: self.fail("replay selected another node")
        self.assertEqual(self.service.accept(fixture.principal(), "exact-replay", self.raw)["id"], accepted["id"])
        self.assertEqual(self.hub.calls, [])

    def test_acceptance_independently_refuses_different_exact_tuple(self):
        wrong = copy.deepcopy(self.probe)
        wrong["requested_encode"]["bitrate_kbps"] = 8000
        self.destination = replace(self.destination, encode_probe=wrong)
        with self.assertRaises(RenderServiceError):
            self.service.accept(fixture.principal(), "wrong-encode", self.raw)
        self.assertEqual(self.hub.calls, [])

    def test_v2_envelope_and_reader_require_the_exact_extension(self):
        accepted = self.service.accept(fixture.principal(), "v2-reader", self.raw)
        row = self._stage_inputs_and_prepare(accepted["id"])
        outbound = json.loads(bytes(row["outbound_hub_body"]))
        self.assertEqual(outbound["format"], "studio.render.hub-request.v2")
        self.assertEqual(outbound["encode_authority"], self.binding)
        answer = self._hub_job(row)
        self.assertEqual(self.service._hub_job(row, answer), answer)
        for changed in ({key: value for key, value in answer.items() if key != "encode_authority"},
                        {**answer, "encode_authority": {**self.binding, "exact_encoder": "other"}}):
            with self.assertRaises(RenderServiceError):
                self.service._hub_job(row, changed)
        legacy = {**row, "destination_encode_probe": None}
        with self.assertRaises(RenderServiceError):
            self.service._hub_job(legacy, answer)
        answer.pop("encode_authority")
        self.assertEqual(self.service._hub_job(legacy, answer), answer)

    def test_v2_output_is_validated_and_translated_to_frozen_story_receipt(self):
        fixture.DispatchLifecycleTests.test_verified_range_import_publishes_winner_before_hub_ack_and_serves_bounded_range(self)
        # The inherited workflow verifies import, SHA, ack, range and tamper refusal.
        self.assertNotIn("encode_authority", _GATEWAY_RECEIPT_KEYS)

    def test_output_refuses_missing_changed_binding_and_feature_losses_before_transfer(self):
        accepted = self.service.accept(fixture.principal(), "v2-output-refusal", self.raw)
        row = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        attempt = "a" * 32
        self.hub.answers["submit"] = [self._hub_job(row, job_id=hub_id)]
        self.service._one_step(row)
        row = self._row(row["id"])
        receipt = self._receipt(row, hub_job_id=hub_id, attempt_id=attempt)
        baseline = len(self.hub.calls)
        bad = [{key: value for key, value in receipt.items() if key != "encode_authority"},
               {**receipt, "encode_authority": {**self.binding, "authority_digest": "0" * 64}},
               {**receipt, "selected_tier": "another-tier"},
               {**receipt, "feature_losses": ["dropped-overlay"]}]
        for changed in bad:
            changed["receipt_digest"] = hashlib.sha256(fixture.canonical({
                key: value for key, value in changed.items() if key != "receipt_digest"})).hexdigest()
            with self.subTest(changed=changed), self.assertRaises(RenderServiceError):
                self.service._import_output(row, {"remote_descriptor_json": json.dumps(changed),
                    "remote_attempt_id": attempt}, self.destination)
        self.assertEqual(len(self.hub.calls), baseline)

    def test_probe_transport_uses_fixed_read_and_explicit_control(self):
        client = RegisteredHubRenderClient()
        with patch.object(client, "json", return_value=self.probe) as send:
            client.encode_probe(self.destination, self.requested)
            args = send.call_args.args
            self.assertEqual(args[1], "GET")
            self.assertEqual(json.loads(parse_qs(args[2].split("?", 1)[1])["requested_encode"][0]), self.requested)
            self.assertEqual(send.call_args.kwargs, {})
            client.encode_probe(self.destination, self.requested, start=True)
            self.assertEqual(send.call_args.args[1:], ("POST", "/encode-probes"))
            self.assertEqual(json.loads(send.call_args.kwargs["body"]), {
                "format": "studio.render.encode-probe-request.v1", "requested_encode": self.requested})

    def test_http_query_rejects_duplicate_parameters_and_duplicate_json_keys(self):
        duplicate_json = json.dumps(self.requested).replace('"crf": 23', '"crf": 23, "crf": 18')
        for values in ([json.dumps(self.requested)] * 2, [duplicate_json], ["null"], ["{}"], [""]):
            with self.assertRaises(ValueError):
                requested_encode_query(values)
            request = Request({"type": "http", "query_string": urlencode([
                ("requested_encode", value) for value in values]).encode()})
            with self.assertRaises(RenderServiceError) as caught:
                api.final_render_capabilities(request, fixture.principal())
            self.assertEqual(caught.exception.status, 422)
        with patch.object(api, "render_service", self.service), patch.object(
                fleet, "render_candidates", return_value=[]):
            plain = api.final_render_capabilities(Request({"type": "http", "query_string": b""}), fixture.principal())
            self.assertNotIn("requested_encode", plain)
            scoped = api.final_render_capabilities(Request({"type": "http", "query_string": urlencode({
                "requested_encode": json.dumps(self.requested)}).encode()}), fixture.principal())
            self.assertEqual(scoped["requested_encode"], self.requested)
            self.assertEqual(scoped["encode"]["entries"], [])


if __name__ == "__main__":
    unittest.main()
