"""Focused tests for the Remote Developer Gate wire contract validator.

Run detached with the suite's own interpreter:
    .venv/bin/python -m unittest tests.test_gate_contract
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._isolation import sandbox_env
PROFILE = sandbox_env('gate-contract-')

from gateway.developer_contract import DeveloperTestError
from gateway import gate_contract as gc
from gateway.gate_contract import (
    GateError,
    decode,
    encoded,
    validate,
    validate_request,
    validate_status,
    verify_result,
    verify_source,
)
from tests.gate_fixtures import (
    fixture,
    make_acceptance,
    make_capabilities,
    make_check,
    make_gate,
    make_gate_request,
    make_policy,
    make_result_manifest,
    make_result_tar_bytes,
    make_source_tar_bytes,
    make_status,
    result_tar,
)


class TestGateContractIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="gate-test-")
        tmp_path = Path(cls._tmp.name)
        os.environ["GATEWAY_APP_SUPPORT"] = str(tmp_path / "support")
        os.environ["GATEWAY_DATA"] = str(tmp_path / "data")
        os.environ["STUDIOHUB_READY_FILE"] = str(tmp_path / "absent-hub-ready.json")
        os.environ["GATEWAY_NO_MIGRATE"] = "1"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()


class TestGateErrorAndEncoding(TestGateContractIsolation):
    def test_gate_error_subclass_and_attributes(self):
        err = GateError(409, "TEST_CODE", "Test detail message")
        self.assertIsInstance(err, DeveloperTestError)
        self.assertIsInstance(err, Exception)
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "TEST_CODE")
        self.assertEqual(err.detail, "Test detail message")
        self.assertIn("409 TEST_CODE: Test detail message", str(err))

        err_dict = err.to_dict()
        self.assertEqual(err_dict["format"], "studio.developer-gate.error.v1")
        self.assertEqual(err_dict["reason"]["code"], "TEST_CODE")
        self.assertEqual(err_dict["reason"]["detail"], "Test detail message")
        self.assertIsNone(err_dict["acceptance"])

    def test_encoded_canonical(self):
        val = {"z": 1, "a": [3, 2], "m": {"b": True, "a": None}}
        enc = encoded(val)
        self.assertIsInstance(enc, bytes)
        # Sorted keys, compact separators
        self.assertEqual(enc, b'{"a":[3,2],"m":{"a":null,"b":true},"z":1}')

        # Rejects NaN / Infinity
        with self.assertRaises(ValueError):
            encoded({"bad": float("nan")})
        with self.assertRaises(ValueError):
            encoded({"bad": float("inf")})


class TestDecode(TestGateContractIsolation):
    def test_decode_valid(self):
        pol = make_policy(controller="c1", node="n1")
        raw = encoded(pol)
        decoded = decode(raw, "policy")
        self.assertEqual(decoded["revision"], pol["revision"])

    def test_decode_rejects_non_bytes(self):
        with self.assertRaises(GateError) as ctx:
            decode("not bytes", "policy")  # type: ignore
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")

    def test_decode_rejects_ceiling_exceeded(self):
        pol = make_policy()
        raw = encoded(pol)
        with self.assertRaises(GateError) as ctx:
            decode(raw, "policy", ceiling=10)
        self.assertEqual(ctx.exception.status, 413)
        self.assertEqual(ctx.exception.code, "PAYLOAD_TOO_LARGE")

    def test_decode_rejects_utf8_bom(self):
        pol = make_policy()
        raw = b"\xef\xbb\xbf" + encoded(pol)
        with self.assertRaises(GateError) as ctx:
            decode(raw, "policy")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")
        self.assertIn("BOM", ctx.exception.detail)

    def test_decode_rejects_invalid_utf8(self):
        with self.assertRaises(GateError) as ctx:
            decode(b"\xff\xfe\x00\x00", "policy")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")

    def test_decode_rejects_duplicate_keys(self):
        raw = b'{"format":"studio.developer-gate.cancel.v1","request_id":"r1","request_id":"r2"}'
        with self.assertRaises(GateError) as ctx:
            decode(raw, "cancelRequest")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")
        self.assertIn("duplicate", ctx.exception.detail)

    def test_decode_rejects_nonfinite_and_unsafe_constants(self):
        raw = b'{"format":"studio.developer-gate.cancel.v1","request_id":NaN}'
        with self.assertRaises(GateError) as ctx:
            decode(raw, "cancelRequest")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")

    def test_decode_rejects_unsafe_keys(self):
        raw = b'{"format":"studio.developer-gate.cancel.v1","request_id":"r1","__proto__":"bad"}'
        with self.assertRaises(GateError) as ctx:
            decode(raw, "cancelRequest")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_JSON")

    def test_decode_unknown_kind(self):
        raw = encoded({"test": 123})
        with self.assertRaises(GateError) as ctx:
            decode(raw, "nonExistentDef")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(ctx.exception.code, "UNKNOWN_KIND")

    def test_decode_closed_schema_rejects_extra_fields(self):
        pol = make_policy()
        pol["extra_field"] = "unexpected"
        raw = encoded(pol)
        with self.assertRaises(GateError) as ctx:
            decode(raw, "policy")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "SCHEMA_VIOLATION")


class TestValidateRequest(TestGateContractIsolation):
    def setUp(self):
        self.req, self.cap, self.pol = fixture(controller="c1", node="n1")

    def test_validate_request_happy_path(self):
        val = validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(val["format"], "studio.developer-gate.request.v1")
        self.assertEqual(val["run_id"], "run-001")

    def test_nominal_512gb_class_uses_owner_editable_measured_minimum(self):
        drive = self.cap['inventory']['internal_physical_drive']
        drive['size_bytes'] = 500277792768
        validate_request(self.req, self.cap, self.pol, 'c1', 'n1')
        drive['size_bytes'] = 499999999999
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, 'c1', 'n1')
        self.assertEqual(ctx.exception.code, 'INSUFFICIENT_PHYSICAL_DRIVE')
        drive['size_bytes'] = 500277792768
        self.pol['minimum_internal_physical_bytes'] = 512000000000
        self.cap['admission_policy']['minimum_internal_physical_bytes'] = 512000000000
        self.req['minimum_internal_physical_bytes'] = 512000000000
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, 'c1', 'n1')
        self.assertEqual(ctx.exception.code, 'INSUFFICIENT_PHYSICAL_DRIVE')

    def test_empty_allowlist_refuses_even_with_matching_revisions(self):
        self.pol['allowed_targets'] = []
        self.cap['admission_policy']['allowed_targets'] = []
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, 'c1', 'n1')
        self.assertEqual(ctx.exception.code, 'TARGET_NOT_ALLOWED')

    def test_requested_safety_minimums_cannot_be_lowered(self):
        for key in ('minimum_free_bytes', 'start_min_free_memory_bytes'):
            with self.subTest(key=key):
                req = copy.deepcopy(self.req)
                req['requested_limits'][key] -= 1
                with self.assertRaises(GateError) as ctx:
                    validate_request(req, self.cap, self.pol, 'c1', 'n1')
                self.assertEqual(ctx.exception.code, 'LIMIT_BELOW_GATE')

    def test_validate_request_unsupported_cap(self):
        self.cap["supported"] = False
        self.cap["reason"] = {"code": "NODE_OFFLINE", "detail": "Node is offline."}
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_CAPABILITIES")

    def test_validate_request_target_excluded(self):
        self.pol["excluded_targets"] = [{"controller_id": "c1", "node_id": "n1"}]
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "TARGET_EXCLUDED")

    def test_validate_request_target_not_allowed(self):
        self.pol["allowed_targets"] = [{"controller_id": "c2", "node_id": "n2"}]
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "TARGET_NOT_ALLOWED")

    def test_validate_request_policy_revision_mismatch(self):
        self.req["admission_policy_revision"] = "different-rev"
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "POLICY_REVISION_MISMATCH")

    def test_validate_request_node_setting_mismatch(self):
        self.req["node_setting_revision"] = "wrong-setting-rev"
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "SETTING_REVISION_MISMATCH")

    def test_validate_request_setting_disabled(self):
        self.cap["setting"]["enabled"] = False
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "SETTING_NOT_ENABLED")

    def test_validate_request_runner_mismatch(self):
        self.req["runner"]["revision"] = "other-runner-rev"
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "RUNNER_MISMATCH")

    def test_validate_request_gate_not_found(self):
        self.req["gate"]["id"] = "unknown-gate"
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "GATE_NOT_FOUND")

    def test_validate_request_duplicate_check_id(self):
        self.req["gate"]["check_ids"] = ["lint", "lint"]
        self.cap["gates"][0]["check_ids"] = ["lint", "lint"]
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "DUPLICATE_CHECK_ID")

    def test_validate_request_stale_inventory(self):
        stale_ts = "2020-01-01T00:00:00Z"
        self.cap["inventory"]["observed_at"] = stale_ts
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "STALE_INVENTORY")

    def test_validate_request_non_internal_drive(self):
        self.cap["inventory"]["internal_physical_drive"]["internal"] = False
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "NOT_INTERNAL_DRIVE")

    def test_validate_request_drive_mapping_mismatch(self):
        self.cap["inventory"]["internal_physical_drive"]["workspace_backing_device_id"] = "disk9"
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "DRIVE_MAPPING_MISMATCH")

    def test_validate_request_insufficient_physical_drive(self):
        self.cap["inventory"]["internal_physical_drive"]["size_bytes"] = 256000000000  # 256 GB < 512 GB
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_PHYSICAL_DRIVE")

    def test_validate_request_limit_exceeds_gate(self):
        self.req["requested_limits"]["runtime_seconds"] = 1000  # Gate limits runtime_seconds is 300
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDS_GATE")

    def test_validate_request_limit_exceeds_bounds(self):
        # Set gate limit high, but cap bounds lower
        self.req["gate"]["limits"]["workspace_bytes"] = 30 * 1024 * 1024 * 1024
        self.cap["gates"][0]["limits"]["workspace_bytes"] = 30 * 1024 * 1024 * 1024
        self.req["requested_limits"]["workspace_bytes"] = 25 * 1024 * 1024 * 1024  # Bounds is 20 GiB
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "LIMIT_EXCEEDS_BOUNDS")

    def test_validate_request_insufficient_free_space(self):
        self.cap["inventory"]["free_bytes"] = 1024  # Only 1 KB free
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_FREE_SPACE")

    def test_validate_request_spool_reservation_exceeded(self):
        # Spool limit is 5 GiB, request wants 1 GiB, already using 4.5 GiB
        self.cap["inventory"]["developer_gate_space"]["result_spool_bytes"] = int(4.5 * 1024 * 1024 * 1024)
        with self.assertRaises(GateError) as ctx:
            validate_request(self.req, self.cap, self.pol, controller_id="c1", node_id="n1")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "RESULT_SPOOL_LIMIT_EXCEEDED")


class TestVerifySource(TestGateContractIsolation):
    def setUp(self):
        self.req, self.cap, self.pol = fixture(controller="c1", node="n1")
        self.tar_bytes, self.src_desc = make_source_tar_bytes()
        self.req["source"] = self.src_desc
        self.req["gate"]["source"] = self.src_desc
        self.cap["gates"][0]["source"] = self.src_desc

    def test_verify_source_happy_path(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.tar_bytes)
            tmp.flush()
            # Does not raise
            verify_source(tmp.name, self.req, self.cap)

    def test_verify_source_not_found(self):
        with self.assertRaises(GateError) as ctx:
            verify_source("/nonexistent/file.tar", self.req, self.cap)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "INVALID_SOURCE")

    def test_verify_source_size_mismatch(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.tar_bytes + b"\x00")
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_source(tmp.name, self.req, self.cap)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "SOURCE_SIZE_MISMATCH")

    def test_verify_source_hash_mismatch(self):
        self.req["source"]["sha256"] = "0" * 64
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.tar_bytes)
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_source(tmp.name, self.req, self.cap)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "SOURCE_HASH_MISMATCH")

    def test_verify_source_rejects_symlinks(self):
        # Create a tar with a symlink
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tar:
            ti = tarfile.TarInfo(name="safe.txt")
            ti.size = 4
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(b"safe"))

            ti_sym = tarfile.TarInfo(name="link.txt")
            ti_sym.type = tarfile.SYMTYPE
            ti_sym.linkname = "safe.txt"
            tar.addfile(ti_sym)
        tar_data = buf.getvalue()

        req_copy = copy.deepcopy(self.req)
        req_copy["source"]["size_bytes"] = len(tar_data)
        req_copy["source"]["sha256"] = hashlib.sha256(tar_data).hexdigest()

        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(tar_data)
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_source(tmp.name, req_copy, self.cap)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "UNSAFE_TAR_MEMBER")

    def test_verify_source_rejects_path_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tar:
            ti = tarfile.TarInfo(name="../escape.txt")
            ti.size = 4
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(b"test"))
        tar_data = buf.getvalue()

        req_copy = copy.deepcopy(self.req)
        req_copy["source"]["size_bytes"] = len(tar_data)
        req_copy["source"]["sha256"] = hashlib.sha256(tar_data).hexdigest()

        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(tar_data)
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_source(tmp.name, req_copy, self.cap)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "UNSAFE_TAR_MEMBER")

    def test_verify_source_file_count_mismatch(self):
        self.req["source"]["file_count"] = 99
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.tar_bytes)
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_source(tmp.name, self.req, self.cap)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "SOURCE_FILE_COUNT_MISMATCH")


class TestVerifyResult(TestGateContractIsolation):
    def setUp(self):
        self.req, self.cap, self.pol = fixture(controller="c1", node="n1")
        self.acc = make_acceptance(self.req, controller="c1", node="n1")
        self.res_bytes, self.manifest = make_result_tar_bytes(
            self.req, self.cap, self.acc, outcome="passed"
        )

    def test_verify_result_happy_path(self):
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.res_bytes)
            tmp.flush()
            man = verify_result(tmp.name, self.req, self.cap, self.acc)
            self.assertEqual(man["format"], "studio.developer-gate.result.v1")
            self.assertEqual(man["outcome"], "passed")
            self.assertEqual(man["exit_code"], 0)

    def test_verify_result_forbidden_dir_entry(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tar:
            ti_dir = tarfile.TarInfo(name="logs")
            ti_dir.type = tarfile.DIRTYPE
            tar.addfile(ti_dir)

            mf_bytes = json.dumps(self.manifest).encode("utf-8")
            ti = tarfile.TarInfo(name="manifest.json")
            ti.size = len(mf_bytes)
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(mf_bytes))

        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(buf.getvalue())
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_result(tmp.name, self.req, self.cap, self.acc)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "FORBIDDEN_DIR_ENTRY")

    def test_verify_result_missing_manifest(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tar:
            ti = tarfile.TarInfo(name="logs/lint.log")
            ti.size = 4
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(b"log\n"))

        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(buf.getvalue())
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_result(tmp.name, self.req, self.cap, self.acc)
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "MISSING_MANIFEST")

    def test_verify_result_acceptance_mismatch(self):
        wrong_acc = copy.deepcopy(self.acc)
        wrong_acc["attempt_id"] = "different-attempt"
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.res_bytes)
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_result(tmp.name, self.req, self.cap, wrong_acc)
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "ACCEPTANCE_MISMATCH")

    def test_verify_result_log_hash_mismatch(self):
        # Tamper with log content
        tampered_bytes, _ = make_result_tar_bytes(
            self.req, self.cap, self.acc, outcome="passed",
            custom_logs={"lint": b"tampered log output\n"}
        )
        # But modify the tar archive member bytes without changing manifest sha
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(self.res_bytes)
            tmp.seek(0)
            # Rewrite lint log in a new tar with wrong data
            buf = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(self.res_bytes), mode="r:") as src_tar:
                with tarfile.open(fileobj=buf, mode="w:") as dst_tar:
                    for m in src_tar.getmembers():
                        if m.name == "logs/lint.log":
                            bad_data = b"wrong content!!\n"
                            ti = tarfile.TarInfo(name=m.name)
                            ti.size = len(bad_data)
                            dst_tar.addfile(ti, io.BytesIO(bad_data))
                        else:
                            f = src_tar.extractfile(m)
                            ti = tarfile.TarInfo(name=m.name)
                            ti.size = m.size
                            dst_tar.addfile(ti, f)
            with tempfile.NamedTemporaryFile(suffix=".tar") as bad_tmp:
                bad_tmp.write(buf.getvalue())
                bad_tmp.flush()
                with self.assertRaises(GateError) as ctx:
                    verify_result(bad_tmp.name, self.req, self.cap, self.acc)
                self.assertEqual(ctx.exception.status, 400)
                self.assertIn(ctx.exception.code, ("LOG_SIZE_MISMATCH", "LOG_HASH_MISMATCH"))

    def test_verify_result_passed_with_nonzero_exit_refused(self):
        bad_bytes, _ = make_result_tar_bytes(self.req, self.cap, self.acc, outcome="passed")
        # Decode manifest, change exit_code to 1, repackage
        with tarfile.open(fileobj=io.BytesIO(bad_bytes), mode="r:") as src_tar:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:") as dst_tar:
                for m in src_tar.getmembers():
                    if m.name == "manifest.json":
                        man = json.loads(src_tar.extractfile(m).read().decode())
                        man["exit_code"] = 1
                        m_data = json.dumps(man).encode()
                        ti = tarfile.TarInfo(name=m.name)
                        ti.size = len(m_data)
                        dst_tar.addfile(ti, io.BytesIO(m_data))
                    else:
                        dst_tar.addfile(m, src_tar.extractfile(m))
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(buf.getvalue())
            tmp.flush()
            with self.assertRaises(GateError) as ctx:
                verify_result(tmp.name, self.req, self.cap, self.acc)
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(ctx.exception.code, "PASSED_OUTCOME_NONZERO_EXIT")

    def test_result_tar_convenience_helper(self):
        tar_data = result_tar(self.req, self.cap, self.acc)
        self.assertIsInstance(tar_data, bytes)
        with tempfile.NamedTemporaryFile(suffix=".tar") as tmp:
            tmp.write(tar_data)
            tmp.flush()
            man = verify_result(tmp.name, self.req, self.cap, self.acc)
            self.assertEqual(man["outcome"], "passed")


class TestValidateStatus(TestGateContractIsolation):
    def setUp(self):
        self.req, self.cap, self.pol = fixture(controller="c1", node="n1")
        self.acc = make_acceptance(self.req, controller="c1", node="n1")
        self.res_desc = {"media_type": "application/x-tar", "sha256": "a" * 64, "size_bytes": 1024}

    def test_validate_status_happy_path_preack(self):
        st = make_status(self.req, self.acc, self.cap, state="passed", result_descriptor=self.res_desc)
        val = validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertEqual(val["state"], "passed")
        self.assertFalse(val["result_acknowledged"])
        self.assertIsNone(val["acknowledged_result"])

    def test_validate_status_happy_path_postack_pending_removal(self):
        # Normal ordering: ACK persisted before workspace removal
        st = make_status(
            self.req, self.acc, self.cap, state="passed", result_descriptor=self.res_desc,
            result_acknowledged=True, acknowledged_result=self.res_desc,
            acknowledged_at="2026-11-08T06:00:01Z", workspace_removed=False, cleanup_after=None,
        )
        val = validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertTrue(val["result_acknowledged"])
        self.assertFalse(val["workspace_removed"])
        self.assertIsNone(val["cleanup_after"])

    def test_validate_status_happy_path_postack_completed_removal(self):
        # After workspace removal: workspace_removed=True with cleanup_after.workspace_bytes == 0
        cleanup = {
            "free_bytes": 250 * 1024 * 1024 * 1024,
            "workspace_bytes": 0,
            "result_spool_bytes": 1024,
            "shared_cache_bytes": 0,
            "observed_at": "2026-11-08T06:00:02Z",
        }
        st = make_status(
            self.req, self.acc, self.cap, state="passed", result_descriptor=self.res_desc,
            result_acknowledged=True, acknowledged_result=self.res_desc,
            acknowledged_at="2026-11-08T06:00:01Z", workspace_removed=True, cleanup_after=cleanup,
        )
        val = validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertTrue(val["result_acknowledged"])
        self.assertTrue(val["workspace_removed"])
        self.assertEqual(val["cleanup_after"]["workspace_bytes"], 0)

    def test_validate_status_workspace_removed_nonzero_workspace_refused(self):
        cleanup = {
            "free_bytes": 250 * 1024 * 1024 * 1024,
            "workspace_bytes": 100,  # Must be 0 when workspace_removed is True
            "result_spool_bytes": 1024,
            "shared_cache_bytes": 0,
            "observed_at": "2026-11-08T06:00:02Z",
        }
        st = make_status(
            self.req, self.acc, self.cap, state="passed", result_descriptor=self.res_desc,
            result_acknowledged=True, acknowledged_result=self.res_desc,
            acknowledged_at="2026-11-08T06:00:01Z", workspace_removed=True, cleanup_after=cleanup,
        )
        with self.assertRaises(GateError) as ctx:
            validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "NONZERO_REMOVED_WORKSPACE")

    def test_validate_status_terminal_outcome_requires_result_descriptor(self):
        # A terminal state (e.g. failed) MUST carry result descriptor
        st = make_status(
            self.req, self.acc, self.cap, state="failed", result_descriptor=None,
        )
        with self.assertRaises(GateError) as ctx:
            validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "MISSING_RESULT_DESCRIPTOR")

    def test_validate_status_preack_has_result_refused(self):
        st = make_status(
            self.req, self.acc, self.cap, state="passed", result_descriptor=self.res_desc,
            result_acknowledged=False, acknowledged_result=self.res_desc,
        )
        with self.assertRaises(GateError) as ctx:
            validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "PREACK_HAS_RESULT")

    def test_validate_status_nonterminal_release_confirmed_refused(self):
        st = make_status(
            self.req, self.acc, self.cap, state="running",
            resource_release_confirmed=True,
        )
        with self.assertRaises(GateError) as ctx:
            validate_status(st, request=self.req, capabilities=self.cap, acceptance=self.acc)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "INVALID_RELEASE_STATE")


class TestArchiveAliases(TestGateContractIsolation):
    def test_single_aliased_member_is_refused_even_without_duplicate(self):
        req, cap, _ = fixture()
        for name in ('./src/a.py', 'src//a.py', 'src/./a.py', r'src\\a.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                raw, descriptor = make_source_tar_bytes({name: b'fixture'})
                req['source'] = descriptor
                path = Path(directory) / 'source.tar'
                path.write_bytes(raw)
                with self.assertRaises(GateError) as ctx:
                    verify_source(path, req, cap)
                self.assertEqual(ctx.exception.code, 'UNSAFE_TAR_MEMBER')


if __name__ == "__main__":
    unittest.main()
