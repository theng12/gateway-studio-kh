"""Focused tests for the frozen Developer Mode wire contract validator.

Run detached with the suite's own interpreter:
    .venv/bin/python -m unittest tests.test_developer_contract

The builders (capability / request / receipt / status) are exported so the
Developer Mode service suite can construct valid wire values without duplicating
the frozen field lists.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import developer_contract as dc
from gateway.developer_contract import (
    DeveloperTestError,
    decode,
    validate,
    validate_receipt,
    validate_request,
    validate_status,
)


def capability(**overrides):
    """A fully valid, self-consistent capabilities document."""
    cap = {
        "format": "studio.developer-test.capabilities.v1",
        "contract": "studio.developer-test.v1",
        "node_id": "node-a",
        "controller_id": "ctrl-1",
        "server_time": "2026-11-05T05:00:00Z",
        "supported": True,
        "reason": None,
        "runner_id": "studio.image-generation-test.v1",
        "runner_revision": "runner-3",
        "runner_sha256": "1" * 64,
        "setting": {
            "format": "studio.developer-test.node-setting-result.v1",
            "node_id": "node-a",
            "controller_id": "ctrl-1",
            "enabled": True,
            "revision": "rev-7",
            "verified": True,
            "reason": None,
            "updated_at": "2026-11-05T04:00:00Z",
            "request_id": None,
        },
        "test_kinds": ["reference_counts", "prompt_length", "low_ram_mode",
                       "memory_pressure", "new_model"],
        "checks": ["decoded-output", "dimensions"],
        "concurrency": 1,
        "bounds": {
            "request_bytes": 65536,
            "response_bytes": 65536,
            "log_total_bytes": 1048576,
            "log_chunk_bytes": 65536,
            "input_refs": 16,
            "fixtures": 64,
            "fixture_sets": 8,
            "models": 8,
            "prompt_pattern_code_points": 64,
        },
        "safety_policy": {
            "revision": "policy-1",
            "sha256": "2" * 64,
            "requested_limits": {
                "max_runtime_seconds": {"minimum": 1, "maximum": 600, "unit": "seconds",
                                        "source": "packaged policy", "revision": "policy-1",
                                        "provenance": "packaged_safety_policy",
                                        "enforcement": "deadline"},
                "max_process_tree_rss_bytes": {"minimum": 0, "maximum": 17179869184,
                                               "unit": "bytes", "source": "packaged policy",
                                               "revision": "policy-1",
                                               "provenance": "packaged_safety_policy",
                                               "enforcement": "sampled"},
                "max_system_swap_growth_bytes": {"minimum": 0, "maximum": 4294967296,
                                                 "unit": "bytes", "source": "packaged policy",
                                                 "revision": "policy-1",
                                                 "provenance": "packaged_safety_policy",
                                                 "enforcement": "sampled"},
                "test_start_min_free_bytes": {"minimum": 0, "maximum": 8589934592,
                                              "unit": "bytes", "source": "packaged policy",
                                              "revision": "policy-1",
                                              "provenance": "packaged_safety_policy",
                                              "enforcement": "admission"},
            },
        },
        "measurement_method": {
            "start_memory": "psutil.virtual_memory().available",
            "start_memory_scope": "host_available_physical_unified_memory",
            "rss": "sampled_owned_process_tree_rss_sum",
            "swap": "sampled_system_swap_used",
            "sample_interval_ms": 1000,
            "elapsed_clock": "monotonic",
        },
        "stop_proof": {"stop_latency_seconds": 5.0, "source": "packaged proof",
                       "revision": "proof-1", "proof_sha256": "3" * 64},
        "fixture_sets": [fixture_set()],
        "models": [model_capability()],
    }
    cap.update(overrides)
    return cap


def model_capability(**overrides):
    model = {
        "model": {"id": "flux-2-klein", "revision": "r1"},
        "package_id": "pkg-flux",
        "manifest_sha256": "4" * 64,
        "installed": True,
        "installable": True,
        "reason": None,
        "engine": "flux-engine",
        "engine_version": "1.2.3",
        "memory_modes": ["default", "bounded-staged"],
        "settings_bounds": {
            "width_px": {"minimum": 64, "maximum": 4096},
            "height_px": {"minimum": 64, "maximum": 4096},
            "dimension_multiple": 8,
            "steps": {"minimum": 1, "maximum": 50},
            "guidance": {"minimum": 0.0, "maximum": 20.0},
            "seed": {"minimum": 0, "maximum": 2147483647},
            "reference_count": {"minimum": 0, "maximum": 10},
            "prompt_characters": {"minimum": 0, "maximum": 1000},
        },
        "intrinsic_reference_max": 10,
        "intrinsic_reference_source": "engine schema",
        "published_reference_max": 10,
        "published_prompt_max": 1000,
        "published_limits_source": "engine schema",
    }
    model.update(overrides)
    return model


def fixture_set(**overrides):
    fset = {
        "id": "set-a",
        "sha256": "5" * 64,
        "fixtures": [
            {"fixture_id": "ref-1", "sha256": "6" * 64, "size_bytes": 100,
             "width_px": 1024, "height_px": 1024, "media_type": "image/png"},
            {"fixture_id": "ref-2", "sha256": "7" * 64, "size_bytes": 200,
             "width_px": 1024, "height_px": 1024, "media_type": "image/jpeg"},
        ],
        "reference_order": ["ref-1", "ref-2"],
        "prompt_construction": "repeat_pattern_then_codepoint_prefix_v1",
        "prompt_pattern": "A cat. ",
    }
    fset.update(overrides)
    return fset


def request(cap=None, **overrides):
    """A valid submit request bound to cap. Nested fields are replaced whole via overrides."""
    cap = cap or capability()
    fset = cap["fixture_sets"][0]
    count = 2
    characters = 20
    packaged = {fixture["fixture_id"]: fixture for fixture in fset["fixtures"]}
    refs = [{key: packaged[name][key] for key in ("fixture_id", "sha256", "size_bytes")}
            for name in fset["reference_order"][:count]]
    pattern = fset["prompt_pattern"]
    prompt = (pattern * ((characters + len(pattern) - 1) // len(pattern)))[:characters]
    body = {
        "format": "studio.developer-test.request.v1",
        "run_id": "run-1",
        "test_id": "test-1",
        "runner_id": cap["runner_id"],
        "model": dict(cap["models"][0]["model"]),
        "kind": "reference_counts",
        "settings": {
            "width_px": 1024, "height_px": 1024, "steps": 20, "guidance": 7.5, "seed": 42,
            "memory_mode": "default", "reference_count": count, "prompt_characters": characters,
            "fixture_set_id": fset["id"],
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        "input_refs": refs,
        "requested_limits": {
            "max_runtime_seconds": 60,
            "max_process_tree_rss_bytes": 4294967296,
            "max_system_swap_growth_bytes": 1073741824,
            "test_start_min_free_bytes": 2147483648,
        },
        "runner_revision": cap["runner_revision"],
        "runner_sha256": cap["runner_sha256"],
        "fixture_set_sha256": fset["sha256"],
        "node_setting_revision": cap["setting"]["revision"],
    }
    body.update(overrides)
    return body


def status(req=None, cap=None, **overrides):
    cap = cap or capability()
    req = req or request(cap)
    body = {
        "format": "studio.developer-test.status.v1",
        "node_id": cap["node_id"],
        "controller_id": cap["controller_id"],
        "idempotency_key": "key-1",
        "run_id": req["run_id"],
        "test_id": req["test_id"],
        "attempt_id": "attempt-1",
        "request_sha256": "8" * 64,
        "accepted": True,
        "state": "waiting_idle",
        "reason": {"code": "production_busy", "detail": "Waiting for a free slot."},
        "cancel_requested": False,
        "created_at": "2026-11-05T05:00:10Z",
        "updated_at": "2026-11-05T05:00:20Z",
        "receipt_sha256": None,
        "resource_release_confirmed": False,
    }
    body.update(overrides)
    return body


def receipt(req=None, cap=None, **overrides):
    """A fully complete, passed receipt bound to req and cap."""
    cap = cap or capability()
    req = req or request(cap)
    declared = next(m for m in cap["models"] if m["model"] == req["model"])
    body = {
        "format": "studio.developer-test.receipt.v1",
        "run_id": req["run_id"],
        "test_id": req["test_id"],
        "attempt_id": "attempt-1",
        "idempotency_key": "key-1",
        "request_sha256": "8" * 64,
        "node_id": cap["node_id"],
        "controller_id": cap["controller_id"],
        "node_setting_revision": req["node_setting_revision"],
        "runner_id": req["runner_id"],
        "model": {"id": req["model"]["id"], "revision": req["model"]["revision"],
                  "package_id": declared["package_id"], "manifest_sha256": declared["manifest_sha256"],
                  "engine": declared["engine"], "engine_version": declared["engine_version"]},
        "hardware": {"chip_family": "Apple M", "chip_model": "M3 Max", "ram_bytes": 36864000000,
                     "os_name": "macOS", "os_version": "15.1", "os_build": "24B83"},
        "builds": {"hub": "0.6.31", "runner": req["runner_revision"]},
        "runner_sha256": req["runner_sha256"],
        "kind": req["kind"],
        "settings": dict(req["settings"]),
        "installation": {"required": not declared["installed"], "started": False,
                         "completed": False, "download_id": None,
                         "verified_manifest_sha256": None, "reason": None},
        "started_at": "2026-11-05T05:01:00Z",
        "finished_at": "2026-11-05T05:02:00Z",
        "outcome": "passed",
        "reason": None,
        "measurements": {"elapsed_seconds": 60.0, "generation_seconds": 40.5,
                         "peak_process_memory_bytes": 5000000000,
                         "system_swap_baseline_bytes": 1000, "system_swap_peak_bytes": 3000,
                         "swap_growth_bytes": 2000},
        "measurement_metadata": {
            "method": dict(cap["measurement_method"]),
            "sample_count": 40,
            "sample_gaps_ms": [],
            "available_memory_before_start_bytes": 12000000000,
            "preflight_observed_at": "2026-11-05T05:01:30Z",
            "safety_policy": copy.deepcopy(cap["safety_policy"]),
            "published_limits": {"reference_max": 10, "prompt_max": 1000,
                                 "intrinsic_reference_max": 10, "source": "engine schema",
                                 "intrinsic_source": "engine schema"},
            "requested_limits": copy.deepcopy(req["requested_limits"]),
        },
        "metric_unavailable_reasons": [],
        "checks": [{"name": name, "passed": True, "reason": None} for name in cap["checks"]],
        "log": {"sha256": "9" * 64, "size_bytes": 512, "truncated": False},
        "output": {"sha256": "a" * 64, "size_bytes": 1048576,
                   "width_px": req["settings"]["width_px"],
                   "height_px": req["settings"]["height_px"],
                   "validation": "decoded_dimensions_match"},
        "resource_release": {"confirmed": True, "confirmed_at": "2026-11-05T05:02:01Z",
                             "reason": None},
        "runner_revision": req["runner_revision"],
        "fixture_set_sha256": req["fixture_set_sha256"],
    }
    body.update(overrides)
    return body


def mutated(value, **fields):
    clone = copy.deepcopy(value)
    clone.update(fields)
    return clone


class ContractError(unittest.TestCase):
    def refusal(self, function, *args, status=None, code=None):
        with self.assertRaises(DeveloperTestError) as caught:
            function(*args)
        error = caught.exception
        if status is not None:
            self.assertEqual(error.status, status, error)
        if code is not None:
            self.assertEqual(error.code, code, error)
        self.assertTrue(error.detail)
        return error


class FrozenSchema(ContractError):
    def test_data_file_bytes_match_the_frozen_hash(self):
        raw = (Path(__file__).resolve().parents[1] / "data" / "developer-test-v1.schema.json").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), dc._SCHEMA_SHA256)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "3ce2175fb7456161cc45bb6ffc7b3930f6c09eb76492b9ef917d1ec240bd3e7a")

    def test_unknown_kind_fails_closed(self):
        self.refusal(validate, {}, "not-a-def", status=500, code="UNKNOWN_KIND")

    def test_unsupported_schema_keyword_fails_closed(self):
        saved = dc._SCHEMA
        dc._SCHEMA = {"$defs": {"probe": {"type": "object", "properties": {}, "required": [],
                                          "additionalProperties": False, "bogus_keyword": 1}}}
        try:
            self.refusal(validate, {}, "probe", status=500, code="UNSUPPORTED_SCHEMA_KEYWORD")
        finally:
            dc._SCHEMA = saved

    def test_builders_validate_clean(self):
        self.assertEqual(validate(capability(), "capabilities")["node_id"], "node-a")
        self.assertEqual(validate(request(), "request")["run_id"], "run-1")
        self.assertEqual(validate(receipt(), "receipt")["outcome"], "passed")
        self.assertEqual(validate(status(), "status")["state"], "waiting_idle")


class Decode(ContractError):
    def test_valid_request_bytes(self):
        raw = json.dumps(request()).encode("utf-8")
        self.assertEqual(decode(raw, "request", 1 << 20)["test_id"], "test-1")

    def test_duplicate_keys_refused(self):
        self.refusal(decode, b'{"run_id": "a", "run_id": "b"}', "request", 1 << 20,
                     status=400, code="INVALID_JSON")

    def test_nonfinite_constants_refused(self):
        self.refusal(decode, b'{"x": NaN}', "request", 1 << 20, status=400, code="INVALID_JSON")
        self.refusal(decode, b'{"x": 1e400}', "request", 1 << 20, status=400, code="INVALID_JSON")

    def test_non_utf8_and_bom_refused(self):
        self.refusal(decode, b'{"a": "\xff"}', "request", 1 << 20, status=400, code="INVALID_JSON")
        self.refusal(decode, b"\xef\xbb\xbf{}", "request", 1 << 20, status=400, code="INVALID_JSON")

    def test_not_bytes_refused(self):
        self.refusal(decode, "{}", "request", 1 << 20, status=400, code="INVALID_JSON")

    def test_byte_ceiling_checked_before_parsing(self):
        self.refusal(decode, b"{}", "request", 1, status=413, code="PAYLOAD_TOO_LARGE")

    def test_unknown_field_refused(self):
        body = dict(request(), extra=1)
        self.refusal(decode, json.dumps(body).encode(), "request", 1 << 20,
                     status=400, code="SCHEMA_VIOLATION")

    def test_missing_required_field_refused(self):
        body = dict(request())
        del body["run_id"]
        self.refusal(decode, json.dumps(body).encode(), "request", 1 << 20,
                     status=400, code="SCHEMA_VIOLATION")

    def test_pattern_and_const_refused(self):
        self.refusal(decode, json.dumps(mutated(request(), run_id=" bad")).encode(),
                     "request", 1 << 20, status=400, code="SCHEMA_VIOLATION")
        self.refusal(decode, json.dumps(mutated(request(), format="nope")).encode(),
                     "request", 1 << 20, status=400, code="SCHEMA_VIOLATION")

    def test_unsafe_integer_magnitude_refused(self):
        self.refusal(decode, b'{"x": 99999999999999999999}', "request", 1 << 20,
                     status=400, code="INVALID_JSON")

    def test_capabilities_decode_includes_semantics(self):
        body = mutated(capability(), stop_proof=None)
        self.refusal(decode, json.dumps(body).encode(), "capabilities", 1 << 20,
                     status=502, code="INVALID_HUB_RESPONSE")


class CapabilitySemantics(ContractError):
    def test_valid_capabilities_pass(self):
        self.assertEqual(validate(capability(), "capabilities")["supported"], True)

    def test_supported_requires_verified_stop_proof(self):
        self.refusal(validate, mutated(capability(), stop_proof=None), "capabilities",
                     status=502, code="INVALID_HUB_RESPONSE")
        cap = capability()
        cap["stop_proof"]["stop_latency_seconds"] = 0
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")

    def test_supported_requires_live_inventory(self):
        self.refusal(validate, mutated(capability(), test_kinds=[]), "capabilities",
                     status=502, code="INVALID_HUB_RESPONSE")
        self.refusal(validate, mutated(capability(), checks=[]), "capabilities",
                     status=502, code="INVALID_HUB_RESPONSE")
        self.refusal(validate, mutated(capability(), models=[]), "capabilities",
                     status=502, code="INVALID_HUB_RESPONSE")
        self.refusal(validate, mutated(capability(), fixture_sets=[]), "capabilities",
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_unsupported_requires_a_reason(self):
        cap = capability(supported=False)
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(supported=False, reason={"code": "unsupported", "detail": "No runner yet."})
        self.assertFalse(validate(cap, "capabilities")["supported"])

    def test_non_installable_model_requires_a_reason(self):
        cap = capability(models=[model_capability(installable=False)])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(models=[model_capability(
            installable=False, reason={"code": "model_unavailable", "detail": "No package."})])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")  # no installable peer

    def test_inverted_range_refused(self):
        cap = capability()
        cap["models"][0]["settings_bounds"]["width_px"] = {"minimum": 4096, "maximum": 64}
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability()
        cap["safety_policy"]["requested_limits"]["max_runtime_seconds"]["minimum"] = 601
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")

    def test_safety_units_must_match_the_field(self):
        cap = capability()
        cap["safety_policy"]["requested_limits"]["max_runtime_seconds"]["unit"] = "bytes"
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")

    def test_log_bounds_and_wire_counts(self):
        cap = capability()
        cap["bounds"]["log_chunk_bytes"] = cap["bounds"]["log_total_bytes"] + 1
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(bounds=mutated(capability()["bounds"], models=1),
                         models=[model_capability(),
                                 model_capability(model={"id": "qwen-2", "revision": "r1"})])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")

    def test_identity_uniqueness_and_fixture_order(self):
        cap = capability(models=[model_capability(), model_capability()])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(checks=["decoded-output", "decoded-output"])
        self.refusal(validate, cap, "capabilities", status=400, code="SCHEMA_VIOLATION")
        cap = capability(fixture_sets=[fixture_set(reference_order=["ref-1", "ghost"])])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(fixture_sets=[fixture_set(prompt_pattern="x" * 65)])
        self.refusal(validate, cap, "capabilities", status=502, code="INVALID_HUB_RESPONSE")


class ApprovalSemantics(ContractError):
    def approval(self, changes):
        return {
            "format": "studio.developer-test.approval.v1",
            "proposal_sha256": "1" * 64,
            "receipt_sha256s": ["2" * 64],
            "owner_action_id": "act-1",
            "actor": "owner",
            "approved_at": "2026-11-05T05:00:00Z",
            "expected_store_revision": "rev-1",
            "scope": {"node_ids": ["node-a"], "chip_model": "M3 Max", "ram_bytes": 36864000000,
                      "model": {"id": "flux-2-klein", "revision": "r1"},
                      "runner_sha256": "1" * 64, "settings": request()["settings"]},
            "changes": changes,
        }

    def test_valid_approval_passes(self):
        answer = validate(self.approval([
            {"field": "measured_runtime_seconds", "previous_value": None,
             "value": 40.5, "unit": "seconds"},
            {"field": "measured_peak_process_tree_rss_bytes", "previous_value": None,
             "value": 5000000000, "unit": "bytes"},
        ]), "approval")
        self.assertEqual(answer["owner_action_id"], "act-1")

    def test_duplicate_fields_refused(self):
        self.refusal(validate, self.approval([
            {"field": "measured_runtime_seconds", "previous_value": None,
             "value": 1.0, "unit": "seconds"},
            {"field": "measured_runtime_seconds", "previous_value": None,
             "value": 2.0, "unit": "seconds"},
        ]), "approval", status=400, code="SCHEMA_VIOLATION")

    def test_runtime_field_needs_seconds_unit(self):
        self.refusal(validate, self.approval([
            {"field": "measured_runtime_seconds", "previous_value": None,
             "value": 1.0, "unit": "bytes"},
        ]), "approval", status=400, code="SCHEMA_VIOLATION")


class RequestGate(ContractError):
    def test_valid_request_passes(self):
        cap, body = capability(), request()
        self.assertIs(validate_request(body, cap), body)

    def test_unsupported_and_unverified_authority(self):
        cap = capability(supported=False, reason={"code": "unsupported", "detail": "No runner yet."})
        self.refusal(validate_request, request(cap), cap, status=503, code="UNSUPPORTED")
        cap = capability(setting=mutated(capability()["setting"], enabled=False))
        self.refusal(validate_request, request(cap), cap, status=503, code="UNVERIFIED")
        cap = capability(setting=mutated(capability()["setting"], verified=False))
        self.refusal(validate_request, request(cap), cap, status=503, code="UNVERIFIED")

    def test_stale_setting_revision(self):
        cap = capability()
        body = request(cap, node_setting_revision="rev-6")
        self.refusal(validate_request, body, cap, status=409, code="STALE_SETTING")

    def test_runner_and_fixture_pins(self):
        cap = capability()
        body = request(cap, runner_sha256="9" * 64)
        self.refusal(validate_request, body, cap, status=409, code="PIN_CONFLICT")
        body = request(cap, fixture_set_sha256="9" * 64)
        self.refusal(validate_request, body, cap, status=409, code="PIN_CONFLICT")

    def test_kind_and_model_advertisement(self):
        cap = capability(test_kinds=["reference_counts"])
        body = request(cap, kind="new_model")
        self.refusal(validate_request, body, cap, status=409, code="KIND_NOT_ADVERTISED")
        cap = capability()
        body = request(cap, model={"id": "flux-2-klein", "revision": "r9"})
        self.refusal(validate_request, body, cap, status=409, code="MODEL_UNAVAILABLE")

    def test_requested_model_must_be_installable(self):
        cap = capability(models=[
            model_capability(),
            model_capability(model={"id": "qwen-2", "revision": "r1"}, installable=False,
                             reason={"code": "model_unavailable", "detail": "No package yet."}),
        ])
        body = request(cap, model={"id": "qwen-2", "revision": "r1"})
        self.refusal(validate_request, body, cap, status=503, code="UNSUPPORTED")

    def test_fixture_set_advertisement(self):
        cap = capability()
        body = request(cap, settings=mutated(request(cap)["settings"], fixture_set_id="set-b"))
        self.refusal(validate_request, body, cap, status=409, code="FIXTURE_UNAVAILABLE")

    def test_settings_outside_live_bounds(self):
        cap = capability()
        body = request(cap, settings=mutated(request(cap)["settings"], width_px=4097))
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")
        body = request(cap, settings=mutated(request(cap)["settings"], steps=51))
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")

    def test_dimension_multiple_guidance_and_memory_mode(self):
        cap = capability()
        body = request(cap, settings=mutated(request(cap)["settings"], width_px=65))
        self.refusal(validate_request, body, cap, status=400, code="INVALID_SETTINGS")
        cap = capability(models=[model_capability(settings_bounds=mutated(
            model_capability()["settings_bounds"], guidance=None))])
        body = request(cap)
        self.refusal(validate_request, body, cap, status=400, code="INVALID_SETTINGS")
        body = request(cap, settings=mutated(request(cap)["settings"], guidance=None))
        cap2 = capability()
        self.refusal(validate_request, body, cap2, status=400, code="INVALID_SETTINGS")
        cap = capability()
        body = request(cap, settings=mutated(request(cap)["settings"], guidance=99.0))
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")
        cap = capability(models=[model_capability(memory_modes=["default"])])
        body = request(cap, settings=mutated(request(cap)["settings"], memory_mode="bounded-staged"))
        self.refusal(validate_request, body, cap, status=400, code="INVALID_SETTINGS")

    def test_reference_order_and_fixture_bytes(self):
        cap = capability()
        body = request(cap, input_refs=list(reversed(request(cap)["input_refs"])))
        self.refusal(validate_request, body, cap, status=400, code="FIXTURE_ORDER_MISMATCH")
        body = request(cap, input_refs=request(cap)["input_refs"][:1])
        self.refusal(validate_request, body, cap, status=400, code="FIXTURE_ORDER_MISMATCH")
        refs = copy.deepcopy(request(cap)["input_refs"])
        refs[0]["sha256"] = "f" * 64
        body = request(cap, input_refs=refs)
        self.refusal(validate_request, body, cap, status=400, code="FIXTURE_MISMATCH")
        body = request(cap, settings=mutated(request(cap)["settings"], reference_count=3))
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")

    def test_prompt_digest_and_zero_characters(self):
        cap = capability()
        body = request(cap, settings=mutated(request(cap)["settings"], prompt_sha256="f" * 64))
        self.refusal(validate_request, body, cap, status=400, code="PROMPT_DIGEST_MISMATCH")
        settings = mutated(request(cap)["settings"], prompt_characters=0, reference_count=0)
        settings["prompt_sha256"] = hashlib.sha256(b"").hexdigest()
        body = request(cap, settings=settings, input_refs=[])
        self.assertEqual(validate_request(body, cap), body)
        cap = capability(models=[model_capability(settings_bounds=mutated(
            model_capability()["settings_bounds"],
            prompt_characters={"minimum": 1, "maximum": 1000}))])
        body = request(cap, settings=mutated(request(cap)["settings"],
                                             prompt_characters=0, reference_count=0,
                                             prompt_sha256=hashlib.sha256(b"").hexdigest()),
                       input_refs=[])
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")

    def test_requested_limits_must_fit_the_packaged_policy(self):
        cap = capability()
        body = request(cap, requested_limits=mutated(request(cap)["requested_limits"],
                                                     max_runtime_seconds=601))
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")

    def test_input_refs_fit_the_wire_bound(self):
        cap = capability(bounds=mutated(capability()["bounds"], input_refs=1))
        body = request(cap)
        self.refusal(validate_request, body, cap, status=400, code="OUT_OF_RANGE")


class StatusBindings(ContractError):
    def test_valid_status_passes(self):
        cap, body = capability(), status()
        self.assertIs(validate_status(body, request(cap), cap), body)

    def test_node_controller_and_run_bindings(self):
        cap = capability()
        body = status(request(cap), cap, node_id="node-b")
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="IDENTITY_MISMATCH")
        body = status(request(cap), cap, controller_id="ctrl-2")
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="IDENTITY_MISMATCH")
        body = status(request(cap), cap, run_id="run-2")
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="IDENTITY_MISMATCH")

    def test_reason_and_receipt_identities(self):
        cap = capability()
        body = status(request(cap), cap, reason=None)
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")
        body = status(request(cap), cap, state="passed", reason=None, receipt_sha256=None)
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")
        body = status(request(cap), cap, state="passed",
                      reason={"code": "internal_error", "detail": "Odd."}, receipt_sha256="1" * 64)
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")
        body = status(request(cap), cap, state="failed", reason=None)
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_unaccepted_attempt_cannot_work(self):
        cap = capability()
        body = status(request(cap), cap, accepted=False, state="running")
        self.refusal(validate_status, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")


class ReceiptEvidence(ContractError):
    def test_valid_passed_receipt_passes(self):
        cap, body = capability(), receipt()
        self.assertIs(validate_receipt(body, request(cap), cap), body)

    def test_identity_bindings(self):
        cap = capability()
        self.refusal(validate_receipt, receipt(), request(cap), mutated(cap, node_id="node-b"),
                     status=502, code="IDENTITY_MISMATCH")
        body = receipt(run_id="run-2")
        self.refusal(validate_receipt, body, request(cap), cap, status=502, code="IDENTITY_MISMATCH")
        body = receipt(settings=mutated(receipt()["settings"], steps=21))
        self.refusal(validate_receipt, body, request(cap), cap, status=502, code="IDENTITY_MISMATCH")
        body = receipt(model=mutated(receipt()["model"], package_id="pkg-other"))
        self.refusal(validate_receipt, body, request(cap), cap, status=502, code="IDENTITY_MISMATCH")
        meta = mutated(receipt()["measurement_metadata"])
        meta["requested_limits"]["max_runtime_seconds"] = 61
        body = receipt(measurement_metadata=meta)
        self.refusal(validate_receipt, body, request(cap), cap, status=502, code="IDENTITY_MISMATCH")
        meta = mutated(receipt()["measurement_metadata"])
        meta["safety_policy"] = mutated(meta["safety_policy"], revision="policy-9")
        body = receipt(measurement_metadata=meta)
        self.refusal(validate_receipt, body, request(cap), cap, status=502, code="IDENTITY_MISMATCH")

    def test_reason_release_and_unconfirmed_release(self):
        self.refusal(validate_receipt, receipt(reason={"code": "oom", "detail": "Died."}),
                     request(), capability(), status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(resource_release={"confirmed": False, "confirmed_at": None,
                                         "reason": {"code": "release_unknown", "detail": "Not yet."}})
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(resource_release={"confirmed": True, "confirmed_at": None, "reason": None})
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_failed_receipt_needs_a_reason_and_passes_when_complete(self):
        measurements = copy.deepcopy(receipt()["measurements"])
        measurements["generation_seconds"] = None
        body = receipt(outcome="failed",
                       reason={"code": "oom", "detail": "Worker died."},
                       measurements=measurements,
                       metric_unavailable_reasons=[{"field": "generation_seconds",
                                                    "reason": "Worker died before sampling."}],
                       output=None,
                       checks=[{"name": "decoded-output", "passed": False,
                                "reason": {"code": "output_invalid", "detail": "No image."}},
                               {"name": "dimensions", "passed": True, "reason": None}])
        self.assertEqual(validate_receipt(body, request(), capability())["outcome"], "failed")

    def test_metric_reason_bijection(self):
        base = receipt(outcome="failed",
                       reason={"code": "oom", "detail": "Worker died."},
                       output=None,
                       metric_unavailable_reasons=[])
        body = mutated(base, metric_unavailable_reasons=[
            {"field": "generation_seconds", "reason": "No sampling."}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")  # observed field carries a reason
        body = mutated(base, metric_unavailable_reasons=[
            {"field": "generation_seconds", "reason": "No sampling."},
            {"field": "generation_seconds", "reason": "Again."}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")  # duplicate
        body = receipt()  # passed, nothing null, entries must be empty
        body["metric_unavailable_reasons"] = [{"field": "hardware.os_build",
                                               "reason": "Unreadable."}]
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_passed_requires_complete_measurements(self):
        measurements = copy.deepcopy(receipt()["measurements"])
        measurements["generation_seconds"] = None
        body = receipt(measurements=measurements)
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INCOMPLETE_EVIDENCE")
        hardware = mutated(receipt()["hardware"], chip_model=None)
        body = receipt(hardware=hardware, metric_unavailable_reasons=[
            {"field": "hardware.chip_model", "reason": "Not read."}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INCOMPLETE_EVIDENCE")
        meta = mutated(receipt()["measurement_metadata"],
                       available_memory_before_start_bytes=None)
        body = receipt(measurement_metadata=meta, metric_unavailable_reasons=[
            {"field": "available_memory_before_start_bytes", "reason": "Not observed."}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INCOMPLETE_EVIDENCE")

    def test_measurement_bounds_relations(self):
        body = receipt(measurements=mutated(receipt()["measurements"], generation_seconds=90.0))
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(measurements=mutated(receipt()["measurements"], swap_growth_bytes=999))
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(measurements=mutated(receipt()["measurements"],
                                            system_swap_peak_bytes=500))
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_timestamp_window(self):
        body = receipt(started_at="2026-11-05T05:03:00Z")
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        meta = mutated(receipt()["measurement_metadata"],
                       preflight_observed_at="2026-11-05T05:03:00Z")
        body = receipt(measurement_metadata=meta)
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_checks_must_be_advertised_unique_and_all_passed(self):
        body = receipt(checks=[{"name": "decoded-output", "passed": True, "reason": None}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INCOMPLETE_EVIDENCE")
        body = receipt(checks=[{"name": "ghost-check", "passed": True, "reason": None},
                               {"name": "decoded-output", "passed": True, "reason": None},
                               {"name": "dimensions", "passed": True, "reason": None}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(checks=[{"name": "decoded-output", "passed": False,
                                "reason": {"code": "output_invalid", "detail": "No image."}},
                               {"name": "dimensions", "passed": True, "reason": None}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        body = receipt(checks=[{"name": "decoded-output", "passed": True,
                                "reason": {"code": "internal_error", "detail": "Odd."}},
                               {"name": "dimensions", "passed": True, "reason": None}])
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")

    def test_output_and_installation_requirements(self):
        body = receipt(output=None)
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INCOMPLETE_EVIDENCE")
        body = receipt(output=mutated(receipt()["output"], width_px=512))
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")
        cap = capability(models=[model_capability(installed=False)])
        body = receipt(request(cap), cap,
                       installation={"required": False, "started": False, "completed": False,
                                     "download_id": None, "verified_manifest_sha256": None,
                                     "reason": None})
        self.refusal(validate_receipt, body, request(cap), cap,
                     status=502, code="INVALID_HUB_RESPONSE")  # uninstalled model without a required install
        body = receipt(request(cap), cap,
                       installation={"required": True, "started": False, "completed": False,
                                     "download_id": None, "verified_manifest_sha256": None,
                                     "reason": {"code": "model_install_failed", "detail": "No space."}})
        self.refusal(validate_receipt, body, request(cap), cap,
                     status=502, code="INCOMPLETE_EVIDENCE")

    def test_not_run_never_claims_work(self):
        null_measurements = {field: None for field in receipt()["measurements"]}
        reasons = [{"field": field, "reason": "No execution."} for field in (
            "elapsed_seconds", "generation_seconds", "peak_process_memory_bytes",
            "system_swap_baseline_bytes", "system_swap_peak_bytes", "swap_growth_bytes",
            "available_memory_before_start_bytes", "hardware.chip_family", "hardware.chip_model",
            "hardware.ram_bytes", "hardware.os_version", "hardware.os_build")]
        body = receipt(outcome="not_run",
                       reason={"code": "offline_before_submit", "detail": "Never sent."},
                       measurements=null_measurements,
                       metric_unavailable_reasons=reasons,
                       measurement_metadata=mutated(
                           receipt()["measurement_metadata"],
                           available_memory_before_start_bytes=None,
                           preflight_observed_at=None),
                       hardware=mutated(receipt()["hardware"], chip_family=None,
                                        chip_model=None, ram_bytes=None, os_version=None,
                                        os_build=None),
                       output=None, checks=[],
                       installation={"required": False, "started": False, "completed": False,
                                     "download_id": None, "verified_manifest_sha256": None,
                                     "reason": None})
        self.assertEqual(validate_receipt(body, request(), capability())["outcome"], "not_run")
        body = receipt(outcome="not_run",
                       reason={"code": "offline_before_submit", "detail": "Never sent."},
                       measurements=mutated(null_measurements, generation_seconds=5.0),
                       metric_unavailable_reasons=[r for r in reasons
                                                   if r["field"] != "generation_seconds"],
                       measurement_metadata=mutated(
                           receipt()["measurement_metadata"],
                           available_memory_before_start_bytes=None,
                           preflight_observed_at=None),
                       hardware=mutated(receipt()["hardware"], chip_family=None,
                                        chip_model=None, ram_bytes=None, os_version=None,
                                        os_build=None),
                       output=None, checks=[],
                       installation={"required": False, "started": False, "completed": False,
                                     "download_id": None, "verified_manifest_sha256": None,
                                     "reason": None})
        self.refusal(validate_receipt, body, request(), capability(),
                     status=502, code="INVALID_HUB_RESPONSE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
