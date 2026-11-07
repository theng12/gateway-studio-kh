"""Focused offline proof for Gateway remote parity admission and transport readiness.

Verifies the joint subtitle v4 descriptor triple, false production eligibility,
strict input bounds (8 fonts / 32 MiB font / 4 MiB ASS), destination filtering,
staging store lifecycle, Hub job contract adherence, and frozen frame records.
No live inference, no socket I/O, no network calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gateway import fleet, runtime
from gateway.render_contract import LATE_LABEL_CONTRACT, LATE_LABEL_MOTIONS, SUBTITLE_CONTRACT
from gateway.render_service import (
    _hub_owner_scope,
    AuthenticatedPrivateRender,
    FinalRenderService,
    PrivateRenderUnavailable,
    RenderDestination,
    RenderServiceError,
)
from tests import test_render_service as fixtures
from tests.test_render_late_labels import asset_row, label_op, seal
from tests.test_render_late_service import png
from tests.test_render_subtitles import ass, font, operation

LATE_MOTIONS = list(LATE_LABEL_MOTIONS)
LATE_RECORD = {"format": LATE_LABEL_CONTRACT, "motions": LATE_MOTIONS}
SUBTITLE_BUNDLE_DIR = ROOT / "data" / "final-render-subtitle"
FRAME_RECORD_PATH = ROOT / "docs" / "tasks" / "subtitle-parity-frame-record.json"


class RemoteParityAdmissionTests(fixtures.RenderServiceCase):
    def setUp(self) -> None:
        super().setUp()
        profile_bytes = (SUBTITLE_BUNDLE_DIR / "profile.json").read_bytes()
        tools_bytes = (SUBTITLE_BUNDLE_DIR / "tools.json").read_bytes()
        compiler_bytes = (SUBTITLE_BUNDLE_DIR / "compiler.json").read_bytes()

        self.subtitle_profile_raw = profile_bytes
        self.subtitle_profile = json.loads(profile_bytes)
        self.subtitle_profile_digest = hashlib.sha256(profile_bytes).hexdigest()
        self.subtitle_tools_raw = tools_bytes
        self.subtitle_tools_digest = hashlib.sha256(tools_bytes).hexdigest()
        self.subtitle_compiler_raw = compiler_bytes
        self.subtitle_compiler_digest = hashlib.sha256(compiler_bytes).hexdigest()

        self.service.subtitle_profile_descriptor = profile_bytes
        self.service.subtitle_profile = self.subtitle_profile
        self.service.subtitle_profile_digest = self.subtitle_profile_digest
        self.service.subtitle_profile_facts = {
            "tools": {"descriptor_utf8": tools_bytes.decode(), "digest": self.subtitle_tools_digest},
            "compiler": {"descriptor_utf8": compiler_bytes.decode(), "digest": self.subtitle_compiler_digest},
        }

    def subtitle_request(self, *, ass_bytes=None, font_bytes=None, include_late_label=False):
        value = json.loads(self.request())
        recipe = value["packet"]["recipe"]
        recipe["profile_digest"] = self.subtitle_profile_digest
        ops = [operation()]
        if include_late_label:
            ops.append(label_op(index=1))
        recipe["operations"] = ops

        for name, role, suffix, data in (
            ("subtitle_ass", "subtitle_ass", "ass", ass() if ass_bytes is None else ass_bytes),
            ("subtitle_font_0000", "subtitle_font", "ttf", font() if font_bytes is None else font_bytes),
        ):
            entry, handle = self.publish(name, data, suffix)
            entry["role"] = role
            value["packet"]["manifest"].append(entry)
            value["assets"][name] = handle
            if role == "subtitle_font":
                ops[0]["fonts"][0]["sha256"] = entry["sha256"]

        if include_late_label:
            lbl_entry, lbl_handle = self.publish("cue_0001_label", png(), "png")
            lbl_entry["role"] = "cue_label"
            value["packet"]["manifest"].append(lbl_entry)
            value["assets"]["cue_0001_label"] = lbl_handle

        seal(value["packet"])
        return fixtures.canonical(value)

    def destination_for_subtitle(self, *, qualification_only=True, production_eligible=False,
                                  font_support=True, combined=False, extra_changes=None):
        capability = {
            **self.destination.capability_snapshot,
            "hardware_class": "Apple M4",
            "vector_id": "subtitle-test-intent" if qualification_only else None,
            "exact_encoder": "libx264",
            "profile_digest": self.subtitle_profile_digest,
            "tools_digest": self.subtitle_tools_digest,
            "compiler_digest": self.subtitle_compiler_digest,
            "profile_execution_bounds": self.subtitle_profile["execution_bounds"],
            "font_support": font_support,
            "subtitles": {"format": SUBTITLE_CONTRACT},
            "qualification_only": qualification_only,
            "production_eligible": production_eligible,
            "available_capacity": {"slots": 1, "slots_total": 1} if qualification_only else None,
            "bounds": {"output_limit_bytes": 1024 * 1024},
        }
        if qualification_only:
            capability.pop("production_enabled", None)
        else:
            capability["production_enabled"] = True
            capability.pop("vector_id", None)
            capability.pop("available_capacity", None)
        if combined:
            capability["late_labels"] = copy.deepcopy(LATE_RECORD)
        if extra_changes:
            capability.update(extra_changes)

        raw = fixtures.canonical(capability)
        return replace(
            self.destination,
            qualification_vector_id="subtitle-test-intent" if qualification_only else None,
            capability_snapshot=capability,
            capability_snapshot_sha256=hashlib.sha256(raw).hexdigest(),
        )

    # 1. Descriptor Triples & False Production Eligibility
    def test_committed_descriptor_triple_preserves_false_production_eligibility(self) -> None:
        profile = self.subtitle_profile
        self.assertEqual(profile["format"], "studio.render.profile.v4")
        self.assertEqual(profile["implementation"], "story-classic-subtitle-v1")
        self.assertEqual(profile["recipe_namespace"], "studio.render.final.v1")
        self.assertEqual(profile["combination_set"], "retained-classic-subtitle-candidate-v1")
        self.assertEqual(profile["tier_policy"], "story-final-chain-v1")
        self.assertEqual(profile["output_policy"], "story-final-output-v1")
        self.assertIs(profile["production_eligible"], False,
                      "Subtitle v4 profile must strictly preserve approved false production eligibility")

        # Bounds check
        bounds = profile["execution_bounds"]
        self.assertEqual(bounds["bounds_version"], 2)
        self.assertEqual(bounds["max_subtitle_fonts"], 8)
        self.assertEqual(bounds["max_subtitle_font_bytes"], 33554432)
        self.assertEqual(bounds["max_subtitle_ass_bytes"], 4194304)

        # Compiler and tools linkage
        self.assertEqual(profile["compiler_artifact_sha256"], self.subtitle_compiler_digest)
        self.assertEqual(profile["tools_artifact_sha256"], self.subtitle_tools_digest)

        compiler = json.loads(self.subtitle_compiler_raw)
        self.assertEqual(compiler["format"], "studio.render.compiler.v1")
        self.assertEqual(compiler["implementation"], "hub-final-render-subtitle-v1")

        tools = json.loads(self.subtitle_tools_raw)
        self.assertEqual(tools["format"], "studio.render.tools.v1")
        self.assertEqual(tools["platform"], "macos-arm64")

    # 2. Admission Lifecycle & Staging Store
    def test_subtitle_admission_creates_staging_and_preserves_idempotency(self) -> None:
        dest = self.destination_for_subtitle()
        raw_request = self.subtitle_request()
        attempt_key = "subtitle-parity-attempt-001"

        with mock.patch.object(self.service, "destination_selector", return_value=dest):
            accepted = self.service.accept(fixtures.principal(), attempt_key, raw_request)

        self.assertEqual(accepted["state"], "staging")
        gateway_job_id = accepted["id"]
        self.assertTrue(gateway_job_id)

        # Verify staged job in store
        staged = self.store.get_render_staging(gateway_job_id)
        self.assertIsNotNone(staged)
        self.assertEqual(staged["gateway_attempt_key"], attempt_key)
        self.assertEqual(staged["destination_node"], dest.node_id)
        self.assertEqual(staged["destination_site"], dest.site_id)
        self.assertEqual(staged["state"], "staging")

        # Verify idempotency replay returns same job
        with mock.patch.object(self.service, "destination_selector", return_value=dest):
            replayed = self.service.accept(fixtures.principal(), attempt_key, raw_request)
        self.assertEqual(replayed["id"], gateway_job_id)

    # 3. Remote Transport & Hub Job Contract Validation
    def test_hub_job_contract_demands_qualification_purpose_and_exact_digests(self) -> None:
        dest = self.destination_for_subtitle()
        raw_request = self.subtitle_request()
        with mock.patch.object(self.service, "destination_selector", return_value=dest):
            accepted = self.service.accept(fixtures.principal(), "hub-contract-attempt-001", raw_request)

        staged = self.store.get_render_staging(accepted["id"])

        # Hub response fixture helper
        def make_hub_response(purpose="qualification", profile_digest=None, tools_digest=None,
                              compiler_digest=None, state="accepted", compute_lease=False):
            return {
                "id": "11111111-1111-4111-8111-111111111111",
                "owner_scope": _hub_owner_scope(staged),
                "dispatch_key": staged["dispatch_key"],
                "accepted_request_sha256": staged["outbound_hub_body_sha256"],
                "purpose": purpose,
                "state": state,
                "machine": staged["destination_node"],
                "dispatch_generation": 1,
                "attempt_id": "0123456789abcdef0123456789abcdef",
                "stopped_evidence_digest": None if state != "cancelled" else "e" * 64,
                "profile_digest": profile_digest or self.subtitle_profile_digest,
                "tools_digest": tools_digest or self.subtitle_tools_digest,
                "compiler_digest": compiler_digest or self.subtitle_compiler_digest,
                "qualification_digest": dest.capability_snapshot["qualification_digest"],
                "cancel_requested_at": None,
                "leases": {
                    "compute": compute_lease,
                    "reference": 1,
                    "reader": 1,
                    "transfer": 1,
                    "ack": 1,
                },
                "replayed": False,
                "output_receipt": None,
            }

        # Valid qualification response succeeds
        valid_response = make_hub_response(purpose="qualification")
        validated = self.service._hub_job(staged, valid_response)
        self.assertEqual(validated["purpose"], "qualification")
        self.assertEqual(validated["state"], "accepted")

        # Refusal: production purpose is rejected because qualification_only is True
        bad_purpose = make_hub_response(purpose="production")
        with self.assertRaises(RenderServiceError) as caught:
            self.service._hub_job(staged, bad_purpose)
        self.assertEqual(caught.exception.code, "HUB_RESPONSE_UNKNOWN")

        # Refusal: mismatched descriptor digests from Hub fail closed
        for key, bad_digest in (("profile_digest", "f" * 64),
                                ("tools_digest", "f" * 64),
                                ("compiler_digest", "f" * 64)):
            with self.subTest(key=key):
                bad_digests = make_hub_response(**{key: bad_digest})
                with self.assertRaises(RenderServiceError) as caught:
                    self.service._hub_job(staged, bad_digests)
                self.assertEqual(caught.exception.code, "HUB_RESPONSE_UNKNOWN")

    # 4. Destination Qualification Gate Refusals
    def test_admission_refuses_candidates_claiming_production_eligibility(self) -> None:
        # A candidate claiming production_eligible: True for qualification-only subtitle contract must be rejected
        unapproved = self.destination_for_subtitle(qualification_only=True, production_eligible=True)
        raw_request = self.subtitle_request()
        with mock.patch.object(self.service, "destination_selector", return_value=unapproved):
            with self.assertRaises(RenderServiceError) as caught:
                self.service.accept(fixtures.principal(), "unapproved-prod-attempt", raw_request)
            self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")

    def test_admission_refuses_candidate_without_subtitle_support(self) -> None:
        no_font = self.destination_for_subtitle(font_support=False)
        raw_request = self.subtitle_request()
        with mock.patch.object(self.service, "destination_selector", return_value=no_font):
            with self.assertRaises(RenderServiceError) as caught:
                self.service.accept(fixtures.principal(), "no-font-attempt", raw_request)
            self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")

    # 5. Joint Feature Execution (Subtitles + Late Labels)
    def test_combined_subtitle_and_late_labels_requires_both_on_same_worker(self) -> None:
        combined_request = self.subtitle_request(include_late_label=True)

        # Dest supporting both succeeds
        both = self.destination_for_subtitle(combined=True)
        with mock.patch.object(self.service, "destination_selector", return_value=both):
            accepted = self.service.accept(fixtures.principal(), "combined-attempt-001", combined_request)
        self.assertEqual(accepted["state"], "staging")

        # Dest supporting subtitles only fails for combined request
        subtitles_only = self.destination_for_subtitle(combined=False)
        with mock.patch.object(self.service, "destination_selector", return_value=subtitles_only):
            with self.assertRaises(RenderServiceError) as caught:
                self.service.accept(fixtures.principal(), "combined-attempt-002", combined_request)
            self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")

    # 6. Frame Record & Cross-App Prerequisites Verification
    def test_parity_frame_record_preserves_contract_and_deferred_live_status(self) -> None:
        self.assertTrue(FRAME_RECORD_PATH.is_file(), "subtitle-parity-frame-record.json must exist")
        record = json.loads(FRAME_RECORD_PATH.read_text())

        self.assertEqual(record["format"], "gateway.subtitle-parity-fixture.v1")
        self.assertEqual(record["comparison_boundary"], "both final encoded MP4 video streams decoded to RGB24")
        self.assertEqual(record["comparison"], "exact byte equality; zero tolerance")
        self.assertEqual(record["frame_index_origin"], 0)
        self.assertEqual(record["fps"], {"numerator": 30, "denominator": 1})
        self.assertEqual(record["canvas"], {"width": 1280, "height": 720})
        self.assertEqual(record["duration_sec"], 6)

        # Cross-app prerequisites and live generation hold
        self.assertIn("not run", record["live_proof_status"])
        self.assertIn("requires reviewed Hub executor release installed on permitted node", record["live_proof_status"])
        self.assertEqual(record["prohibited_node"], "paused Studio")

        # English fixture points
        en = record["cases"]["en"]
        self.assertEqual(en["font_family"], "Anton")
        self.assertEqual(en["frame_indices"]["first_cue_first"], 12)
        self.assertEqual(en["frame_indices"]["first_cue_middle"], 35)
        self.assertEqual(en["frame_indices"]["first_cue_last"], 59)
        self.assertEqual(en["frame_indices"]["longest_cue_inside"], 95)
        self.assertEqual(en["frame_indices"]["no_subtitle"], 0)

        # Khmer fixture points
        km = record["cases"]["km"]
        self.assertEqual(km["font_family"], "Battambang")
        self.assertEqual(km["frame_indices"]["first_cue_first"], 12)
        self.assertEqual(km["frame_indices"]["first_cue_middle"], 37)
        self.assertEqual(km["frame_indices"]["first_cue_last"], 62)
        self.assertEqual(km["frame_indices"]["longest_cue_inside"], 104)
        self.assertEqual(km["frame_indices"]["no_subtitle"], 0)


if __name__ == "__main__":
    unittest.main()
