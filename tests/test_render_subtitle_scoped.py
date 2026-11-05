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
from gateway.render_service import PrivateRenderUnavailable, RegisteredHubRenderClient, probed_render_candidates

LATE = {"format": LATE_LABEL_CONTRACT, "motions": list(LATE_LABEL_MOTIONS)}


def subtitle_row(*, combined=False, **changes):
    row = scoped.destination_row()
    row["capability"] = {**row["capability"], "font_support": True,
                         "subtitles": {"format": SUBTITLE_CONTRACT}}
    if combined:
        row["capability"]["late_labels"] = copy.deepcopy(LATE)
    row["capability"].update(changes)
    return row


class SubtitleSupportTests(unittest.TestCase):
    def test_old_envelope_cannot_supply_unfrozen_subtitle_authority(self):
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


class SubtitleCapabilitiesTests(fixtures.RenderServiceCase):
    def setUp(self):
        super().setUp()
        profile = json.loads(fixtures.PROFILE_RAW)
        profile.update(format="subtitle-fixture-profile", production_eligible=False)
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

    def capabilities(self, rows):
        with mock.patch("gateway.render_service.fleet.render_candidates", return_value=rows):
            return self.service.capabilities(fixtures.principal(), requested_contract=SUBTITLE_CONTRACT)

    def test_missing_descriptor_refuses_and_legacy_stays_closed(self):
        self.service.subtitle_profile_descriptor = None
        with self.assertRaises(PrivateRenderUnavailable):
            self.capabilities([])
        with mock.patch("gateway.render_service.fleet.render_candidates", return_value=[]):
            result = self.service.capabilities(fixtures.principal())
        self.assertNotIn("subtitles", result)
        self.assertNotIn("late_labels", result)

    def test_empty_or_non_font_candidates_do_not_advertise_subtitles(self):
        for rows in ([], [subtitle_row(font_support=False)]):
            result = self.capabilities(rows)
            self.assertEqual(result["eligible"], 0)
            self.assertNotIn("subtitles", result)
            self.assertNotIn("late_labels", result)

    def test_joint_records_are_published_only_for_the_same_candidates(self):
        result = self.capabilities([subtitle_row(combined=True)])
        self.assertEqual(result["subtitles"], {"format": SUBTITLE_CONTRACT})
        self.assertEqual(result["late_labels"], LATE)
        result = self.capabilities([subtitle_row(combined=True), subtitle_row()])
        self.assertEqual(result["eligible"], 2)
        self.assertIn("subtitles", result)
        self.assertNotIn("late_labels", result)

    def test_fresh_scoped_read_failures_are_loud(self):
        with mock.patch("gateway.render_service.fleet.render_candidates", side_effect=ValueError("unreadable")):
            with self.assertRaises(PrivateRenderUnavailable):
                self.service.capabilities(fixtures.principal(), requested_contract=SUBTITLE_CONTRACT)

    def test_changed_destination_support_refuses_before_journal(self):
        value = json.loads(self.request())
        value["packet"]["recipe"]["profile_digest"] = self.service.subtitle_profile_digest
        from tests.test_render_late_labels import seal
        seal(value["packet"])
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
