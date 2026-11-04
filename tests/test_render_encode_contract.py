"""Offline exact tuple validation and synthetic Hub/Gateway hash agreement."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.render_contract import validate_render_encode
from gateway.render_hash_v1 import decode_render_json
from gateway.render_encode import evidence_digest, execution_authority


ENCODE = {"container": "mp4", "requested_codec": "h264", "requested_effort": "hardware",
          "crf": 23, "quality_cpu_preset": "medium", "bitrate_kbps": None}


class EncodeContractTests(unittest.TestCase):
    def test_exact_null_and_bounded_positive_caps_keep_their_values(self):
        for cap in (None, 500, 8000, 100000):
            value = {**ENCODE, "bitrate_kbps": cap}
            self.assertEqual(validate_render_encode(value), value)
        self.assertNotEqual(validate_render_encode(ENCODE),
                            validate_render_encode({**ENCODE, "bitrate_kbps": 8000}))

    def test_query_and_packet_share_closed_keys_enums_quality_and_number_bounds(self):
        bad = [None, [], {}, {**ENCODE, "extra": True},
               {key: value for key, value in ENCODE.items() if key != "crf"}]
        bad += [{**ENCODE, key: value} for key, value in (
            ("container", "mkv"), ("requested_codec", "av1"),
            ("requested_effort", "automatic"), ("quality_cpu_preset", "fast"),
            ("crf", True), ("crf", 22), ("bitrate_kbps", True),
            ("bitrate_kbps", 0), ("bitrate_kbps", 499),
            ("bitrate_kbps", 100001), ("bitrate_kbps", 8000.5),
        )]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_render_encode(value)

    def test_duplicate_encode_json_keys_are_rejected_before_validation(self):
        raw = json.dumps(ENCODE).replace('"crf": 23', '"crf": 23, "crf": 18')
        with self.assertRaises(ValueError):
            decode_render_json(raw.encode("utf-8"))

    def test_actual_hub_generated_synthetic_response_matches_gateway_reader(self):
        fixture = json.loads((ROOT / "tests/fixtures/hub_render_encode_execution.json").read_text())
        self.assertIs(fixture["synthetic"], True)
        self.assertIs(fixture["admissible_runtime_evidence"], False)
        self.assertEqual(fixture["source_commit"], "dabf830af78163e6be0c1f3b7cf0cf800a7bc9d6")
        answer = fixture["probe_response"]
        expected = answer["execution_authority"]
        capability = {key: expected[key] for key in (
            "profile_digest", "tools_digest", "compiler_digest", "hardware_class", "profile_execution_bounds")}
        actual = execution_authority(answer, answer["requested_encode"],
            physical_node_id="node-01", capability=capability, chip="Synthetic fixture")
        self.assertEqual(actual, expected)
        self.assertEqual(evidence_digest(answer["evidence"]), answer["authority_digest"])
        self.assertEqual(answer["authority_digest"], "37aac1b6aada249b96703fe94318b08017d38b202832aad27915b035a5495bd7")
        self.assertNotEqual(actual["qualification_digest"], answer["authority_digest"])
        for timestamp in (actual["grant"]["v1"]["accepted_at"], actual["grant"]["migration"]["authorized_at"]):
            self.assertIs(type(timestamp), int)
            self.assertEqual(timestamp, 1793790000)
            self.assertLessEqual(timestamp, 2**53 - 1)
        with self.assertRaises(ValueError):
            execution_authority(answer, answer["requested_encode"], physical_node_id="another-node",
                                capability=capability, chip="Synthetic fixture")

    def test_shared_synthetic_evidence_has_exact_utf8_bytes_and_hash(self):
        fixture = json.loads((ROOT / "tests/fixtures/render_encode_probe_evidence.json").read_text())
        self.assertIs(fixture["synthetic"], True)
        digests = []
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                encoded = json.dumps(case["evidence"], sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")
                self.assertEqual(encoded, case["canonical_utf8"].encode("utf-8"))
                digest = hashlib.sha256(encoded).hexdigest()
                self.assertEqual(digest, case["authority_digest"])
                digests.append(digest)
        self.assertEqual(len(digests), 2)
        self.assertNotEqual(*digests)


class ProbeAuthorityTests(unittest.TestCase):
    def setUp(self):
        fixture = json.loads((ROOT / "tests/fixtures/render_encode_probe_evidence.json").read_text())
        case = fixture["cases"][0]
        evidence = copy.deepcopy(case["evidence"])
        self.requested = evidence["requested_encode"]
        self.capability = {key: evidence[key] for key in
                           ("profile_digest", "tools_digest", "compiler_digest")}
        self.capability.update(hardware_class="fixture-class", profile_execution_bounds={"max_duration_sec": 10})
        binding = {"format": "studio.render.encode-binding.v1", "authority_digest": case["authority_digest"],
                   "selected_tier": evidence["selected_tier"], "exact_encoder": evidence["exact_encoder"]}
        execution = {**self.capability, "format": "studio.render.encode-execution-authority.v1",
                     "requested_encode": self.requested, "encode_authority": binding,
                     "authority_kind": "measured", "qualification_digest": "f" * 64,
                     "selected_tier": evidence["selected_tier"], "exact_encoder": evidence["exact_encoder"],
                     "font_support": False, "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                         "min_available_memory_bytes": 1, "output_limit_bytes": 1}, "grant": None}
        self.answer = {"format": "studio.render.encode-probe-state.v1", "requested_encode": self.requested,
                       "state": "supported", "probe_key": "1" * 64, "reason": None,
                       "evidence": evidence, "authority_digest": case["authority_digest"],
                       "execution_authority": execution}

    def validate(self, answer=None, **overrides):
        return execution_authority(self.answer if answer is None else answer, self.requested,
            **{"physical_node_id": "fixture-node-01", "capability": self.capability,
               "chip": "fixture-chip", **overrides})

    def test_exact_measured_permission_keeps_qualification_separate_from_probe(self):
        actual = self.validate()
        self.assertEqual(actual["qualification_digest"], "f" * 64)
        self.assertNotEqual(actual["qualification_digest"], actual["encode_authority"]["authority_digest"])

    def test_supported_probe_without_execution_permission_is_not_admission(self):
        self.answer["execution_authority"] = None
        self.assertIsNone(self.validate())

    def test_pending_unmeasured_and_unsupported_do_not_invent_evidence(self):
        for state in ("pending", "unmeasured", "unsupported"):
            value = {**self.answer, "state": state, "evidence": None,
                     "authority_digest": None, "execution_authority": None}
            with self.subTest(state=state):
                self.assertIsNone(self.validate(value))
                with self.assertRaises(ValueError):
                    self.validate({**value, "evidence": self.answer["evidence"]})

    def test_different_node_chip_descriptors_or_tuple_are_refused(self):
        for change in ({"physical_node_id": "another-node"}, {"chip": "another-chip"},
                       {"capability": {**self.capability, "tools_digest": "0" * 64}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate(**change)
        with self.assertRaises(ValueError):
            self.validate({**self.answer, "requested_encode": {**self.requested, "bitrate_kbps": None}})

    def test_evidence_hash_binding_tier_and_qualification_are_all_checked(self):
        for key, value in (("selected_tier", "another-tier"), ("exact_encoder", "libx264"),
                           ("qualification_digest", self.answer["authority_digest"])):
            changed = copy.deepcopy(self.answer)
            changed["execution_authority"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(changed)
        changed = copy.deepcopy(self.answer)
        changed["evidence"]["probe_result_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            self.validate(changed)
        with self.assertRaises(ValueError):
            self.validate({**self.answer, "extra": True})

    def test_capability_only_probe_cannot_supply_render_execution_permission(self):
        value = copy.deepcopy(self.answer)
        value["evidence"]["selected_tier"] = None
        value["execution_authority"]["selected_tier"] = None
        value["authority_digest"] = evidence_digest(value["evidence"])
        value["execution_authority"]["encode_authority"].update(
            authority_digest=value["authority_digest"], selected_tier=None)
        with self.assertRaises(ValueError):
            self.validate(value)

    def test_unqualified_execution_requires_its_separate_v2_grant(self):
        self.answer["execution_authority"]["authority_kind"] = "accepted_unqualified"
        with self.assertRaises(ValueError):
            self.validate()
        grant = {"format": "studio.render.accept-unqualified-probed-v2.v1", "digest": "2" * 64,
                 "v1": {"sha256": "3" * 64, "provenance": "owner-opt-in-unqualified",
                        "accepted_by": "fixture-owner", "accepted_at": 1700000000},
                 "migration": {"authorization_id": "fixture-authorization", "authorized_at": 1700000001},
                 "node": {"physical_node_id": "fixture-node-01"}}
        self.answer["execution_authority"]["grant"] = grant
        self.assertEqual(self.validate()["grant"], grant)
        for block, key, value in (("node", "physical_node_id", "another-node"),
                                  ("v1", "sha256", "not-a-digest"),
                                  ("v1", "provenance", "measured"),
                                  ("migration", "authorized_at", True),
                                  ("migration", "extra", "not-closed")):
            changed = copy.deepcopy(self.answer)
            changed["execution_authority"]["grant"][block][key] = value
            with self.subTest(block=block, key=key), self.assertRaises(ValueError):
                self.validate(changed)


if __name__ == "__main__":
    unittest.main()
