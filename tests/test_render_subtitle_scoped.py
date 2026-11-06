"""Offline subtitle routing fixtures; never hardware or production evidence."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from dataclasses import replace
from unittest import mock
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests import test_render_service as fixtures
from tests import test_render_late_scoped as scoped
from gateway import fleet, runtime
from gateway.render_contract import LATE_LABEL_CONTRACT, LATE_LABEL_MOTIONS, SUBTITLE_CONTRACT
from gateway.render_service import (PrivateRenderUnavailable, RegisteredHubRenderClient,
                                    RenderServiceError, probed_render_candidates)

LATE = {"format": LATE_LABEL_CONTRACT, "motions": list(LATE_LABEL_MOTIONS)}
SUBTITLE_ENVELOPE_FORMAT = "studio.render.hub-subtitle-capabilities.v1"


def subtitle_row(*, combined=False, **changes):
    row = scoped.destination_row()
    row["capability"] = {**row["capability"], "font_support": True,
                         "subtitles": {"format": SUBTITLE_CONTRACT}}
    if combined:
        row["capability"]["late_labels"] = copy.deepcopy(LATE)
    row["capability"].update(changes)
    return row


def subtitle_capability(digest=scoped.V3, *, production=True, **extra):
    row = scoped.capability_row(digest, production=production,
                                font_support=True, subtitles={"format": SUBTITLE_CONTRACT})
    row.update(extra)
    return row


def subtitle_envelope(worker, *, reason=None, server_time=int(scoped.NOW),
                      contract=SUBTITLE_CONTRACT, fmt=SUBTITLE_ENVELOPE_FORMAT):
    return scoped.envelope(worker, reason=reason, server_time=server_time,
                           contract=contract, fmt=fmt)


class SubtitleSupportTests(unittest.TestCase):
    def test_late_label_envelope_cannot_supply_subtitle_authority(self):
        payload = scoped.envelope(subtitle_row()["capability"], contract=SUBTITLE_CONTRACT)
        with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
            fleet._decode_scoped_capabilities(payload, requested_contract=SUBTITLE_CONTRACT,
                                              now=scoped.NOW)

    def test_closed_support_requires_font_executor_and_exact_record(self):
        good = subtitle_row()["capability"]
        self.assertTrue(fleet.render_subtitle_support(good))
        for bad in ({**good, "font_support": False}, {**good, "subtitles": None},
                    {**good, "subtitles": {"format": SUBTITLE_CONTRACT, "extra": True}},
                    {**good, "late_labels": None}, {**good, "late_labels": {"format": LATE_LABEL_CONTRACT}}):
            with self.subTest(bad=bad):
                self.assertFalse(fleet.render_subtitle_support(bad))

    def test_combined_support_cannot_be_a_union_of_workers(self):
        self.assertFalse(fleet.render_subtitle_support(subtitle_row()["capability"], late_labels=True))
        self.assertFalse(fleet.render_subtitle_support({"late_labels": LATE}, late_labels=True))
        self.assertTrue(fleet.render_subtitle_support(subtitle_row(combined=True)["capability"], late_labels=True))

    def test_probe_query_keeps_exact_subtitle_scope_on_get_and_post(self):
        client = RegisteredHubRenderClient()
        destination = scoped.EncodeProbeContractTests().destination()
        with mock.patch.object(client, "json", return_value={}) as send:
            for start in (False, True):
                client.encode_probe(destination, scoped.REQUESTED, start=start,
                                    requested_contract=SUBTITLE_CONTRACT)
                self.assertEqual(parse_qs(send.call_args.args[2].split("?", 1)[1])[
                    "requested_contract"], [SUBTITLE_CONTRACT])

    def test_exact_encode_authority_that_loses_font_support_is_not_offered(self):
        authority = {"authority_kind": "measured", "qualification_digest": "6" * 64,
                     "selected_tier": "software-h264", "exact_encoder": "libx264",
                     "font_support": False, "bounds": {"min_available_memory_bytes": 1},
                     "profile_execution_bounds": {}}
        with mock.patch("gateway.render_service.execution_authority", return_value=authority):
            self.assertEqual(probed_render_candidates([subtitle_row()], scoped.REQUESTED,
                             scoped.RecorderHub(), requested_contract=SUBTITLE_CONTRACT), [])


class SubtitleScopedEnvelopeTests(unittest.TestCase):
    def decode(self, payload, *, contract=SUBTITLE_CONTRACT, now=scoped.NOW):
        return fleet._decode_scoped_capabilities(payload, requested_contract=contract, now=now)

    def test_frozen_subtitle_worker_and_measured_absence_both_decode(self):
        row = subtitle_capability()
        self.assertEqual(self.decode(subtitle_envelope(row)), row)
        self.assertIsNone(self.decode(subtitle_envelope(None, reason="no subtitle executor")))
        joint = subtitle_capability(late_labels=copy.deepcopy(LATE))
        self.assertEqual(self.decode(subtitle_envelope(joint)), joint)

    def test_closed_shape_contract_and_clock_fail_closed(self):
        good = subtitle_capability()
        cases = [
            {},
            {**subtitle_envelope(good), "extra": 1},
            {key: value for key, value in subtitle_envelope(good).items() if key != "reason"},
            subtitle_envelope(good, fmt="studio.render.hub-late-label-capabilities.v1"),
            subtitle_envelope(good, contract=LATE_LABEL_CONTRACT),
            subtitle_envelope(good, server_time=True),
            subtitle_envelope(good, server_time=0),
            subtitle_envelope(good, server_time=int(scoped.NOW) - 1000),
            subtitle_envelope(good, server_time=int(scoped.NOW) + 1000),
            subtitle_envelope(good, reason="worker present cannot carry a reason"),
            subtitle_envelope(None, reason=""),
            subtitle_envelope(None, reason="x" * 1025),
            subtitle_envelope("not a worker"),
        ]
        for index, payload in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(fleet.ScopedRenderCapabilitiesError):
                self.decode(payload)

    def test_subtitle_worker_must_support_its_exact_closed_record(self):
        for bad in (subtitle_capability(font_support=False),
                    subtitle_capability(subtitles=None),
                    subtitle_capability(subtitles={"format": SUBTITLE_CONTRACT, "extra": True}),
                    subtitle_capability(late_labels=None),
                    subtitle_capability(late_labels={"format": LATE_LABEL_CONTRACT}),
                    subtitle_capability(late_labels={**LATE, "motions": []}),
                    subtitle_capability(late_labels=True)):
            with self.subTest(bad=bad), self.assertRaises(fleet.ScopedRenderCapabilitiesError):
                self.decode(subtitle_envelope(bad))

    def test_cross_contract_envelopes_never_supply_each_other(self):
        late_format = subtitle_envelope(
            subtitle_capability(), fmt="studio.render.hub-late-label-capabilities.v1")
        with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
            self.decode(late_format)
        with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
            self.decode(subtitle_envelope(subtitle_capability()), contract=LATE_LABEL_CONTRACT)
        with self.assertRaises(fleet.ScopedRenderCapabilitiesError):
            self.decode(scoped.envelope(subtitle_capability()), contract=SUBTITLE_CONTRACT)


class SubtitleScopedCandidateTests(unittest.TestCase):
    def rows(self, *locations, digest=scoped.V3, payload=None):
        response = subtitle_envelope(subtitle_capability()) if payload is None else payload
        with scoped.Fleet(*locations), mock.patch.object(
                fleet, "_scoped_capability_payload", return_value=response) as read:
            result = fleet.render_candidates(digest, requested_contract=SUBTITLE_CONTRACT)
        return result, read

    def test_scoped_subtitle_row_is_admitted_over_a_legacy_snapshot_row(self):
        macs = {"m1": {"worker": scoped.worker("m1")}}
        rows, read = self.rows(scoped.location(macs))
        self.assertEqual([row["node_id"] for row in rows], ["m1"])
        self.assertEqual(rows[0]["capability"]["profile_digest"], scoped.V3)
        read.assert_called_once_with("roster-a", "m1", SUBTITLE_CONTRACT)

    def test_wrong_pin_or_untrusted_subtitle_read_is_loud_not_empty(self):
        macs = {"m1": {"worker": scoped.worker("m1")}}
        for payload in (subtitle_envelope(subtitle_capability(scoped.WRONG)),
                        {},
                        subtitle_envelope(subtitle_capability(font_support=False))):
            with self.subTest(payload=payload), self.assertRaises(
                    fleet.ScopedRenderCapabilitiesError):
                self.rows(scoped.location(macs), payload=payload)

    def test_legacy_no_query_never_reads_subtitle_authority(self):
        macs = {"m1": {"worker": scoped.worker("m1")}}
        with scoped.Fleet(scoped.location(macs)), mock.patch.object(
                fleet, "_scoped_capability_payload",
                side_effect=AssertionError("scoped route touched")):
            rows = fleet.render_candidates(scoped.LEGACY)
        self.assertEqual([row["node_id"] for row in rows], ["m1"])


class SubtitleCapabilitiesTests(fixtures.RenderServiceCase):
    def setUp(self):
        super().setUp()
        profile = json.loads(fixtures.PROFILE_RAW)
        profile.update(format="subtitle-fixture-profile", production_eligible=False)
        profile["execution_bounds"].update(
            max_subtitle_fonts=8, max_subtitle_font_bytes=32 * 1024 * 1024,
            max_subtitle_ass_bytes=4 * 1024 * 1024)
        raw = fixtures.canonical(profile)
        self.service.subtitle_profile_descriptor = raw
        self.service.subtitle_profile = profile
        self.service.subtitle_profile_digest = hashlib.sha256(raw).hexdigest()
        self.service.subtitle_profile_facts = copy.deepcopy(self.service.profile_facts)

    def subtitle_request(self, *, ass_bytes=None, font_bytes=None):
        from tests.test_render_subtitles import ass, font, operation
        from tests.test_render_late_labels import seal
        value = json.loads(self.request())
        value["packet"]["recipe"]["profile_digest"] = self.service.subtitle_profile_digest
        op = operation()
        value["packet"]["recipe"]["operations"] = [op]
        for name, role, suffix, data in (
                ("subtitle_ass", "subtitle_ass", "ass", ass() if ass_bytes is None else ass_bytes),
                ("subtitle_font_0000", "subtitle_font", "ttf", font() if font_bytes is None else font_bytes)):
            entry, handle = self.publish(name, data, suffix)
            entry["role"] = role
            value["packet"]["manifest"].append(entry)
            value["assets"][name] = handle
            if role == "subtitle_font":
                op["fonts"][0]["sha256"] = entry["sha256"]
        seal(value["packet"])
        return fixtures.canonical(value)

    def subtitle_destination(self):
        capability = {**self.destination.capability_snapshot,
                      "profile_digest": self.service.subtitle_profile_digest,
                      "profile_execution_bounds":
                          self.service.subtitle_profile["execution_bounds"],
                      "font_support": True, "subtitles": {"format": SUBTITLE_CONTRACT}}
        return replace(self.destination, capability_snapshot=capability,
            capability_snapshot_sha256=hashlib.sha256(fixtures.canonical(capability)).hexdigest())

    def test_valid_leased_assets_accept_and_replay_original_lineage(self):
        raw = self.subtitle_request()
        with mock.patch.object(self.service, "destination_selector", return_value=self.subtitle_destination()) as select:
            first = self.service.accept(fixtures.principal(), "subtitle-valid", raw)
            replay = self.service.accept(fixtures.principal(), "subtitle-valid", raw)
        self.assertEqual(select.call_count, 1)
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["idempotent_replay"])

    def test_invalid_leased_content_refuses_before_selection_and_journal(self):
        from tests.test_render_subtitles import ass, font
        for index, options in enumerate((
                {"ass_bytes": ass(r'{\fnMissing}Missing font')},
                {"font_bytes": font("Other Family")},
                {"ass_bytes": ass() + b'[Fonts]\nfontname: attachment\n'})):
            key = "subtitle-invalid-" + str(index)
            raw = self.subtitle_request(**options)
            with mock.patch.object(self.service, "destination_selector") as select:
                self.assert_code("RENDER_SUBTITLE_ASSET_INVALID", self.service.accept,
                                 fixtures.principal(), key, raw)
                select.assert_not_called()
            self.assertIsNone(self.store.get_render_staging_by_attempt_key(
                owner_scope=fixtures.OWNER, gateway_attempt_key=key))

    def test_missing_or_mismatched_asset_handles_refuse_before_selection(self):
        for missing in (True, False):
            value = json.loads(self.subtitle_request())
            if missing:
                del value["assets"]["subtitle_font_0000"]
            else:
                value["assets"]["subtitle_font_0000"] = value["assets"]["audio"]
            key = "subtitle-handles-" + str(missing)
            with mock.patch.object(self.service, "destination_selector") as select:
                self.assert_code("RENDER_REQUEST_INVALID" if missing else "RENDER_ASSET_CONFLICT",
                                 self.service.accept, fixtures.principal(), key, fixtures.canonical(value))
                select.assert_not_called()
            self.assertIsNone(self.store.get_render_staging_by_attempt_key(
                owner_scope=fixtures.OWNER, gateway_attempt_key=key))

    def test_published_four_asset_bound_is_not_expanded_for_fonts(self):
        from tests.test_render_late_labels import seal
        value = json.loads(self.subtitle_request())
        self.service.subtitle_profile["execution_bounds"]["max_assets"] = 4
        self.service._require_packet_bounds(value["packet"], self.service.subtitle_profile)
        entry = copy.deepcopy(next(row for row in value["packet"]["manifest"]
                                   if row["role"] == "subtitle_font"))
        entry.update(logical_id="subtitle_font_0001", safe_name="subtitle_font_0001.ttf")
        value["packet"]["manifest"].append(entry)
        value["assets"][entry["logical_id"]] = value["assets"]["subtitle_font_0000"]
        value["packet"]["recipe"]["operations"][0]["fonts"].append({
            "asset": {"type": "asset_ref", "logical_id": entry["logical_id"]},
            "family": "Fixture Family", "sha256": entry["sha256"]})
        seal(value["packet"])
        with mock.patch.object(self.service, "destination_selector") as select:
            self.assert_code("RENDER_PROFILE_MISMATCH", self.service.accept,
                             fixtures.principal(), "subtitle-over-bound", fixtures.canonical(value))
            select.assert_not_called()

    def test_v4_subtitle_input_bounds_are_exact_and_fail_closed(self):
        profile = self.service.subtitle_profile
        packet = json.loads(self.subtitle_request())["packet"]
        self.service._require_subtitle_packet_bounds(packet, profile)
        fonts = [item for item in packet["manifest"] if item["role"] == "subtitle_font"]
        ass = next(item for item in packet["manifest"] if item["role"] == "subtitle_ass")
        extras = [{**copy.deepcopy(fonts[0]), "logical_id": f"subtitle_font_{index:04d}",
                   "safe_name": f"subtitle_font_{index:04d}.ttf"} for index in range(1, 8)]
        eight = {**packet, "manifest": packet["manifest"] + extras}
        self.service._require_subtitle_packet_bounds(eight, profile)
        ninth = {**copy.deepcopy(fonts[0]), "logical_id": "subtitle_font_0008",
                 "safe_name": "subtitle_font_0008.ttf"}
        with self.assertRaises(RenderServiceError) as caught:
            self.service._require_subtitle_packet_bounds(
                {**eight, "manifest": eight["manifest"] + [ninth]}, profile)
        self.assertEqual(caught.exception.code, "RENDER_PROFILE_MISMATCH")
        for role, key in (("subtitle_font", "max_subtitle_font_bytes"),
                          ("subtitle_ass", "max_subtitle_ass_bytes")):
            target = ass if role == "subtitle_ass" else fonts[0]
            cap = profile["execution_bounds"][key]
            at_cap = [{**item, "size_bytes": cap} if item is target else item
                      for item in packet["manifest"]]
            self.service._require_subtitle_packet_bounds({**packet, "manifest": at_cap}, profile)
            over = [{**item, "size_bytes": cap + 1} if item is target else item
                    for item in packet["manifest"]]
            with self.assertRaises(RenderServiceError) as caught:
                self.service._require_subtitle_packet_bounds({**packet, "manifest": over}, profile)
            self.assertEqual(caught.exception.code, "RENDER_PROFILE_MISMATCH")
        second_ass = {**copy.deepcopy(ass), "logical_id": "subtitle_ass_extra",
                      "safe_name": "subtitle_ass_extra.ass"}
        with self.assertRaises(RenderServiceError) as caught:
            self.service._require_subtitle_packet_bounds(
                {**packet, "manifest": packet["manifest"] + [second_ass]}, profile)
        self.assertEqual(caught.exception.code, "RENDER_PROFILE_MISMATCH")
        execution = profile["execution_bounds"]
        for changed in (
                {key: value for key, value in execution.items() if key != "max_subtitle_fonts"},
                {**execution, "max_subtitle_font_bytes": 0},
                {**execution, "max_subtitle_ass_bytes": True}):
            with self.assertRaises(RenderServiceError) as caught:
                self.service._require_subtitle_packet_bounds(
                    packet, {**profile, "execution_bounds": changed})
            self.assertEqual(caught.exception.code, "RENDER_PROFILE_MISMATCH")

    def test_over_bound_subtitle_inputs_refuse_before_selection_and_journal(self):
        from tests.test_render_late_labels import seal
        for index, (role, size) in enumerate((
                ("subtitle_font", 32 * 1024 * 1024 + 1),
                ("subtitle_ass", 4 * 1024 * 1024 + 1))):
            value = json.loads(self.subtitle_request())
            entry = next(item for item in value["packet"]["manifest"] if item["role"] == role)
            entry["size_bytes"] = size
            seal(value["packet"])
            key = f"subtitle-over-bound-{index}"
            with mock.patch.object(self.service, "destination_selector") as select:
                self.assert_code("RENDER_PROFILE_MISMATCH", self.service.accept,
                                 fixtures.principal(), key, fixtures.canonical(value))
                select.assert_not_called()
            self.assertIsNone(self.store.get_render_staging_by_attempt_key(
                owner_scope=fixtures.OWNER, gateway_attempt_key=key))

    def test_subtitle_leased_reads_use_the_frozen_minimum_and_exact_size(self):
        for role in ("subtitle_font", "subtitle_ass"):
            value = json.loads(self.subtitle_request())
            packet, handles = value["packet"], value["assets"]
            entry = next(item for item in packet["manifest"] if item["role"] == role)
            entry["size_bytes"] += 1
            with self.assertRaises(RenderServiceError) as caught:
                self.service._require_subtitle_assets(fixtures.principal(), packet, handles,
                                                      self.service.subtitle_profile)
            self.assertEqual(caught.exception.code, "RENDER_SUBTITLE_ASSET_INVALID")
        value = json.loads(self.subtitle_request())
        packet, handles = value["packet"], value["assets"]
        font = next(item for item in packet["manifest"] if item["role"] == "subtitle_font")
        self.service.subtitle_profile["execution_bounds"]["max_asset_bytes"] = \
            font["size_bytes"] - 1
        with self.assertRaises(RenderServiceError) as caught:
            self.service._require_subtitle_assets(fixtures.principal(), packet, handles,
                                                  self.service.subtitle_profile)
        self.assertEqual(caught.exception.code, "RENDER_SUBTITLE_ASSET_INVALID")

    def capabilities(self, rows):
        with mock.patch("gateway.render_service.fleet.render_candidates", return_value=rows):
            return self.service.capabilities(fixtures.principal(), requested_contract=SUBTITLE_CONTRACT)

    def subtitle_candidate(self, *, combined=False, **changes):
        row = subtitle_row(combined=combined)
        capability = {**self.destination.capability_snapshot,
                      "profile_digest": self.service.subtitle_profile_digest,
                      "profile_execution_bounds":
                          self.service.subtitle_profile["execution_bounds"],
                      "font_support": True, "subtitles": {"format": SUBTITLE_CONTRACT}}
        if combined:
            capability["late_labels"] = copy.deepcopy(LATE)
        capability.update(changes)
        row["capability"] = capability
        return row

    def test_missing_descriptor_refuses_and_legacy_stays_closed(self):
        self.service.subtitle_profile_descriptor = None
        with self.assertRaises(PrivateRenderUnavailable):
            self.capabilities([])
        with mock.patch("gateway.render_service.fleet.render_candidates", return_value=[]):
            result = self.service.capabilities(fixtures.principal())
        self.assertNotIn("subtitles", result)
        self.assertNotIn("late_labels", result)

    def test_empty_or_non_font_candidates_do_not_advertise_subtitles(self):
        for rows in ([], [self.subtitle_candidate(font_support=False)]):
            result = self.capabilities(rows)
            self.assertEqual(result["eligible"], 0)
            self.assertEqual(result["idle"], 0)
            self.assertNotIn("subtitles", result)
            self.assertNotIn("late_labels", result)

    def test_joint_records_are_published_only_for_the_same_candidates(self):
        result = self.capabilities([self.subtitle_candidate(combined=True)])
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["idle"], 1)
        self.assertEqual(result["subtitles"], {"format": SUBTITLE_CONTRACT})
        self.assertEqual(result["late_labels"], LATE)
        result = self.capabilities([self.subtitle_candidate(combined=True),
                                    self.subtitle_candidate()])
        self.assertEqual(result["eligible"], 2)
        self.assertIn("subtitles", result)
        self.assertNotIn("late_labels", result)

    def test_identity_or_bound_mismatch_is_not_advertised(self):
        good = self.subtitle_candidate(combined=True)
        capability = good["capability"]
        for changed in (
                {**capability, "tools_digest": "f" * 64},
                {**capability, "compiler_digest": "f" * 64},
                {**capability, "profile_execution_bounds":
                    {**capability["profile_execution_bounds"], "unexpected": 1}},
                {**capability, "bounds": {**capability["bounds"], "output_limit_bytes": 0}},
                {**capability, "bounds": {**capability["bounds"], "output_limit_bytes": True}}):
            with self.subTest(changed=changed):
                result = self.capabilities([{**good, "capability": changed}])
                self.assertEqual(result["eligible"], 0)
                self.assertEqual(result["idle"], 0)
                self.assertNotIn("subtitles", result)
                self.assertNotIn("late_labels", result)

    def test_subtitle_qualification_row_carries_its_feature_record_and_pins(self):
        capability = {**self.destination.capability_snapshot, "hardware_class": "Apple M4",
                      "vector_id": "subtitle-qualification", "exact_encoder": "libx264",
                      "profile_digest": self.service.subtitle_profile_digest,
                      "profile_execution_bounds":
                          self.service.subtitle_profile["execution_bounds"],
                      "font_support": True, "subtitles": {"format": SUBTITLE_CONTRACT},
                      "production_eligible": False, "qualification_only": True,
                      "available_capacity": {"slots": 1, "slots_total": 1}}
        capability.pop("production_enabled")

        def select(value, vector_id="subtitle-qualification"):
            return replace(self.destination, qualification_vector_id=vector_id,
                           capability_snapshot=value,
                           capability_snapshot_sha256=hashlib.sha256(
                               fixtures.canonical(value)).hexdigest())

        raw = self.subtitle_request()
        with mock.patch.object(self.service, "destination_selector", return_value=select(capability)):
            accepted = self.service.accept(fixtures.principal(), "subtitle-qualification", raw)
        self.assertEqual(accepted["state"], "staging")

        for index, (key, value) in enumerate((
                ("compiler_digest", "f" * 64), ("tools_digest", "f" * 64),
                ("profile_execution_bounds",
                 {**capability["profile_execution_bounds"], "unexpected": 1}),
                ("bounds", {**capability["bounds"], "output_limit_bytes": 0}),
                ("subtitles", {"format": SUBTITLE_CONTRACT, "extra": True}),
                ("late_labels", {"format": LATE_LABEL_CONTRACT}))):
            changed = {**capability, key: value}
            with mock.patch.object(self.service, "destination_selector",
                                   return_value=select(changed)):
                self.assert_code("RENDER_DESTINATION_UNAVAILABLE", self.service.accept,
                                 fixtures.principal(), f"subtitle-qualification-{index}", raw)
        with mock.patch.object(self.service, "destination_selector",
                               return_value=select(capability, vector_id=None)):
            self.assert_code("RENDER_DESTINATION_UNAVAILABLE", self.service.accept,
                             fixtures.principal(), "subtitle-qualification-no-intent", raw)

    def test_fresh_scoped_read_failures_are_loud(self):
        with mock.patch("gateway.render_service.fleet.render_candidates", side_effect=ValueError("unreadable")):
            with self.assertRaises(PrivateRenderUnavailable):
                self.service.capabilities(fixtures.principal(), requested_contract=SUBTITLE_CONTRACT)

    def test_changed_destination_support_refuses_before_journal(self):
        value = json.loads(self.subtitle_request())
        raw = fixtures.canonical(value)
        capability = {**self.destination.capability_snapshot,
                      "profile_digest": self.service.subtitle_profile_digest}
        destination = replace(self.destination, capability_snapshot=capability,
            capability_snapshot_sha256=hashlib.sha256(fixtures.canonical(capability)).hexdigest())
        with mock.patch.object(self.service, "destination_selector", return_value=destination):
            self.assert_code("RENDER_DESTINATION_UNAVAILABLE", self.service.accept,
                             fixtures.principal(), "subtitle-changed", raw)
        self.assertIsNone(self.store.get_render_staging_by_attempt_key(
            owner_scope=fixtures.OWNER, gateway_attempt_key="subtitle-changed"))


class SubtitleDestinationTests(unittest.TestCase):
    def test_combined_packet_excludes_unrelated_candidates_before_probe(self):
        joint, only_subtitle = subtitle_row(combined=True), subtitle_row()
        joint["node_id"] = joint["selected_machine"]["id"] = "joint"
        packet = {"recipe": {"profile_digest": scoped.V3, "encode": scoped.REQUESTED,
                             "operations": [{"kind": "late_label_stage"}]}}
        with mock.patch.object(runtime.render_service, "subtitle_profile_digest", scoped.V3), \
                mock.patch.object(runtime.fleet_module, "render_candidates", return_value=[only_subtitle, joint]) as read, \
                mock.patch.object(runtime, "probed_render_candidates", side_effect=lambda rows, *_a, **_k: rows) as probe:
            result = runtime._render_destination(packet)
        self.assertEqual(result.node_id, "joint")
        self.assertEqual(probe.call_args.args[0], [joint])
        self.assertEqual(read.call_args.kwargs["requested_contract"], SUBTITLE_CONTRACT)


if __name__ == "__main__":
    unittest.main()
