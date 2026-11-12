"""Isolated ledger and transport recovery checks; never contacts a real Hub."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = tempfile.TemporaryDirectory(prefix="gateway-developer-service-")
base = Path(PROFILE.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_ENDPOINT_FILE": str(base / "gateway-endpoint.json"),
    "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "GATEWAY_NO_MIGRATE": "1", "STUDIOHUB_READY_FILE": str(base / "no-hub.json"),
})

import httpx
from gateway.developer_contract import DeveloperTestError
from gateway.developer_store import DeveloperStore, encoded
from gateway.developer_tests import DeveloperTests, HubDeveloperClient
from gateway.qualification import developer_measurements


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DeveloperStore(Path(temporary.name) / "developer.sqlite3")
        self.row = {"id": "attempt-one", "submitted": False, "state": "not_run",
                    "request_bytes": b'{"original":true}'}
        self.store.create("run-one", b"original wrapper", [self.row])

    def test_exact_replay_retains_original_bytes_and_input(self):
        self.assertFalse(self.store.create("run-one", b"original wrapper", []))
        self.assertEqual(self.store.get("attempt-one")["request_bytes"], self.row["request_bytes"])
        with self.assertRaises(DeveloperTestError):
            self.store.create("run-one", b"changed wrapper", [])

    def test_claim_survives_restart_and_cannot_be_claimed_twice(self):
        self.assertTrue(self.store.claim_send("attempt-one"))
        restarted = DeveloperStore(self.store.path)
        self.assertFalse(restarted.claim_send("attempt-one"))
        self.assertEqual(restarted.get("attempt-one")["state"], "recovery_hold")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_terminal_receipt_is_immutable(self):
        self.store.update("attempt-one", receipt=b"receipt-one")
        with self.assertRaises(DeveloperTestError):
            self.store.update("attempt-one", receipt=b"receipt-two")
        self.assertEqual(self.store.get("attempt-one")["receipt_bytes"], b"receipt-one")

    def test_switch_uncertainty_replays_without_becoming_fresh(self):
        self.assertEqual(self.store.setting_action("switch-one", b"exact"), (True, None))
        restarted = DeveloperStore(self.store.path)
        self.assertEqual(restarted.setting_action("switch-one", b"exact"), (False, None))
        with self.assertRaises(DeveloperTestError):
            restarted.setting_action("switch-one", b"changed")
        restarted.setting_result("switch-one", b"original-result")
        self.assertEqual(restarted.setting_action("switch-one", b"exact"), (False, b"original-result"))
        with self.assertRaises(DeveloperTestError):
            restarted.setting_result("switch-one", b"changed-result")

    def test_only_active_approved_measurements_are_read(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO measured VALUES(?,?)", ("approved", json.dumps({"approval_id": "owner-one", "values": {"runtime": 0}})))
            db.execute("INSERT INTO measured VALUES(?,?)", ("retired", json.dumps({"approval_id": None})))
        result = developer_measurements(self.store)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["values"]["runtime"], 0)

    def test_disappeared_unsent_target_stays_unsent(self):
        self.store.update("attempt-one", controller_id="controller", node_id="node")
        service = DeveloperTests(self.store, inventory=lambda: {})
        result = service.submit("attempt-one")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["state"], "not_run")

    def test_campaign_replay_does_not_need_fresh_inventory_or_send(self):
        wrapper = {"run_id": "wrapper-one", "inventory_at": 0, "targets": [],
                   "kind": "reference_counts", "model_id": "model", "settings": {}, "requested_limits": {}}
        raw = encoded(wrapper)
        self.store.create("wrapper-one", raw, [{**self.row, "id": "wrapper-attempt"}])
        service = DeveloperTests(self.store, inventory=lambda: self.fail("Replay must not route again"))
        self.assertEqual(service.run(raw)["runs"][0]["id"], "wrapper-attempt")
        with self.assertRaises(DeveloperTestError) as caught:
            service.run(raw + b" ")
        self.assertEqual(caught.exception.code, "RUN_CONFLICT")

    def test_transport_loss_is_durable_before_wire_and_refresh_never_resubmits(self):
        operations = []
        store = self.store
        class LostHub:
            def send(inner, controller, node, operation, **kwargs):
                operations.append(operation)
                self.assertTrue(store.get("attempt-one")["submitted"])
                raise DeveloperTestError(502, "TRANSPORT_UNCERTAIN", "Fixture lost contact.")
        self.store.update("attempt-one", controller_id="controller", node_id="node", key="key",
                          transport_pin="pin", capabilities={"bounds": {"request_bytes": 128, "response_bytes": 128}})
        service = DeveloperTests(self.store, LostHub(), inventory=lambda: {})
        with patch.object(service, "target", return_value={"paused": False, "online": True}):
            self.assertEqual(service.submit("attempt-one")["state"], "recovery_hold")
        restarted = DeveloperTests(DeveloperStore(self.store.path), LostHub(), inventory=lambda: {})
        self.assertEqual(restarted.refresh("attempt-one")["state"], "recovery_hold")
        self.assertEqual(operations, ["submit", "status"])

    def test_rollback_restores_prior_approval_and_receipt_references(self):
        from gateway.developer_tests import sha
        scope = {"node_ids": ["node-one"]}
        key = sha(encoded(scope))
        previous = {"scope": scope, "values": {"measured_runtime_seconds": 4},
                    "approval_id": "owner-before", "receipt_sha256s": ["a" * 64], "source": "measured"}
        current = {**previous, "approval_id": "owner-now", "receipt_sha256s": ["b" * 64]}
        approval = {"scope": scope, "changes": [{"field": "measured_runtime_seconds", "previous_value": 4}]}
        with self.store.connection() as db:
            revision = self.store.revision(db)
            db.execute("INSERT INTO approvals VALUES(?,?)", ("owner-now", encoded(approval)))
            db.execute("INSERT INTO approval_previous VALUES(?,?)", ("owner-now", encoded(previous).decode()))
            db.execute("INSERT INTO measured VALUES(?,?)", (key, encoded(current).decode()))
        service = DeveloperTests(self.store, inventory=lambda: {})
        self.assertTrue(service.rollback("owner-now", revision, "Owner")["restored"])
        self.assertEqual(self.store.evidence()["measurements"], [previous])
        self.assertEqual(len(self.store.evidence()["approvals"]), 1)
        with self.assertRaises(DeveloperTestError):
            service.rollback("owner-now", revision, "Owner")

    def test_invalid_collected_log_keeps_durable_recovery_hold(self):
        self.store.claim_send("attempt-one")
        self.store.update("attempt-one", receipt=b"{}", capabilities={})
        service = DeveloperTests(self.store, inventory=lambda: {})
        with patch.object(service, "collect_log", side_effect=ValueError("invalid base64")):
            result = service.refresh("attempt-one")
        self.assertEqual(result["state"], "recovery_hold")
        self.assertTrue(DeveloperStore(self.store.path).get("attempt-one")["submitted"])

    def test_rechecking_sent_attempt_never_changes_it_to_unsent(self):
        self.store.claim_send("attempt-one")
        service = DeveloperTests(self.store, inventory=lambda: {})
        with patch.object(service, "target", side_effect=AssertionError("No new routing")):
            result = service.submit("attempt-one")
        self.assertTrue(result["submitted"])
        self.assertEqual(result["state"], "recovery_hold")


class TransportTests(unittest.TestCase):
    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return HubDeveloperClient(client)

    def test_request_and_response_bounds_are_independent(self):
        requests = []
        def answer(request):
            requests.append(request)
            return httpx.Response(202, content=b"{}")
        transport = self.client(answer)
        with patch("gateway.startup_control._target", return_value=("http://controller.invalid", "private-test-token")):
            status, raw = transport.send("controller", "node", "submit", key="key-one",
                                         raw=b"x" * 100, request_ceiling=100, ceiling=2)
        self.assertEqual((status, raw), (202, b"{}"))
        self.assertEqual(requests[0].headers["Idempotency-Key"], "key-one")
        self.assertEqual(requests[0].url.path, "/api/hub/fleet/nodes/node/developer-tests")

    def test_destination_change_refuses_before_any_wire_request(self):
        transport = self.client(lambda request: self.fail("Changed destination must not be contacted"))
        with patch("gateway.startup_control._target", return_value=("http://controller.invalid", "first-token")):
            pin = transport.identity("controller")[2]
        with patch("gateway.startup_control._target", return_value=("http://controller.invalid", "second-token")):
            with self.assertRaises(DeveloperTestError) as caught:
                transport.send("controller", "node", "status", key="key-one", pin=pin)
        self.assertEqual(caught.exception.code, "DESTINATION_CHANGED")

    def test_redirect_and_oversized_response_are_rejected(self):
        for response in (httpx.Response(302, headers={"Location": "http://elsewhere.invalid"}),
                         httpx.Response(200, content=b"xxx")):
            transport = self.client(lambda request: response)
            with patch("gateway.startup_control._target", return_value=("http://controller.invalid", "private-test-token")):
                with self.assertRaises(DeveloperTestError):
                    transport.send("controller", "node", "capabilities", ceiling=2)


class CompleteWorkflowTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_developer_contract import capability, request, receipt, status
        from gateway.developer_tests import sha
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = DeveloperStore(Path(temporary.name) / "developer.sqlite3")
        self.cap = capability(server_time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.cap["bounds"]["log_chunk_bytes"] = 1
        self.log = "ខ".encode("utf-8")
        self.operations = []
        self.tamper_receipt = False
        test = self
        class FakeHub:
            def identity(inner, controller):
                return "fixture", "unused-private-token", "fixture-pin"
            def send(inner, controller, node, operation, *, raw=None, key=None, query=None, **kwargs):
                test.operations.append(operation)
                if operation == "capabilities":
                    return 200, encoded(test.cap)
                if operation == "submit":
                    req = json.loads(raw)
                    result = receipt(req, test.cap, idempotency_key=key, request_sha256=sha(raw))
                    result["log"].update(size_bytes=len(test.log), sha256=sha(test.log))
                    test.receipt = result
                    test.receipt_bytes = encoded(result)
                    test.status = status(req, test.cap, idempotency_key=key, request_sha256=sha(raw),
                                         state="passed", reason=None, receipt_sha256=sha(test.receipt_bytes),
                                         resource_release_confirmed=True)
                    return 202, encoded(test.status)
                if operation == "status":
                    return 200, encoded(test.status)
                if operation == "receipt":
                    return 200, test.receipt_bytes + (b" " if test.tamper_receipt else b"")
                if operation == "log":
                    offset = query["offset_bytes"]
                    data = test.log[offset:offset + query["length_bytes"]]
                    chunk = {k: test.receipt[k] for k in ("node_id", "controller_id", "attempt_id", "request_sha256")}
                    chunk.update(format="studio.developer-test.log-chunk.v1", log=test.receipt["log"],
                                 offset_bytes=offset, length_bytes=len(data), data_base64=base64.b64encode(data).decode(),
                                 chunk_sha256=sha(data), eof=offset + len(data) == len(test.log))
                    return 200, encoded(chunk)
                raise AssertionError(operation)
        at = time.time()
        snapshot = {"generated_at": at,
                    "locations": [{"id": "site", "controller_id": self.cap["controller_id"], "enabled": True, "reachable": True}],
                    "machines": [{"location_id": "site", "machine": self.cap["node_id"], "reachable": True}]}
        self.transport = FakeHub()
        self.service = DeveloperTests(self.store, self.transport, lambda: snapshot)
        req = request(self.cap)
        settings = {k: v for k, v in req["settings"].items() if k != "prompt_sha256"}
        self.wrapper = encoded({"run_id": "complete-run", "inventory_at": at,
                                "targets": [{"controller_id": "site", "node_id": self.cap["node_id"]}],
                                "kind": req["kind"], "model_id": req["model"]["id"],
                                "settings": settings, "requested_limits": req["requested_limits"]})

    def test_filters_bind_exact_inventory_target_and_survive_replay(self):
        snapshot = self.service.inventory_source()
        snapshot["machines"][0].update(chip="Apple M2 Pro", memory_gb=32)
        filters = {"chip": "Apple M2 Pro", "ram_bytes": 32 * 1024**3,
                   "query": self.cap["node_id"]}
        wrapper = encoded({**json.loads(self.wrapper), "filters": filters})
        first = self.service.run(wrapper)["runs"][0]
        self.assertEqual(first["selection_filters"], filters)
        self.assertEqual(first["ram_bytes"], 32 * 1024**3)
        self.assertEqual(first["inventory_at"], snapshot["generated_at"])
        snapshot["machines"] = []
        replay = self.service.run(wrapper)["runs"][0]
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay["selection_filters"], filters)
        self.assertEqual(self.operations.count("submit"), 1)

    def test_nonmatching_or_invalid_filters_refuse_before_capabilities_or_submit(self):
        for change, code in (({"chip": "Apple M2 Pro"}, "TARGET_FILTER_MISMATCH"),
                             ({"ram_bytes": 32 * 1024**3}, "TARGET_FILTER_MISMATCH"),
                             ({"query": "some-other-node"}, "TARGET_FILTER_MISMATCH"),
                             ({"ram_bytes": True}, "INVALID_TARGET_FILTER"),
                             ({"chip": ""}, "INVALID_TARGET_FILTER")):
            filters = {"chip": None, "ram_bytes": None, "query": "", **change}
            with self.subTest(change=change), self.assertRaises(DeveloperTestError) as caught:
                self.service.run(encoded({**json.loads(self.wrapper), "filters": filters}))
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.operations, [])
        self.assertEqual(self.store.rows(), [])

    def test_log_collection_progress_distinguishes_unknown_total_and_split_bytes(self):
        attempt = self.service.run(self.wrapper)["runs"][0]
        self.assertEqual(attempt["log_collected_bytes"], 0)
        self.assertIsNone(attempt["log_total_bytes"])
        partial = self.service.refresh(attempt["id"])
        self.assertEqual(partial["log_collected_bytes"], 1)
        self.assertEqual(partial["log_total_bytes"], len(self.log))
        self.assertFalse(partial["log_complete"])
        self.assertIsNone(self.service.log(attempt["id"])["text"])
        self.assertEqual(developer_measurements(self.store)["rows"], [])

    def test_split_utf8_collection_requires_explicit_approval_and_rolls_back(self):
        row = self.service.run(self.wrapper)["runs"][0]
        attempt = row["id"]
        self.assertEqual(row["state"], "recovery_hold")
        self.assertEqual(self.store.evidence()["approvals"], [])
        self.service.refresh(attempt)
        self.assertIsNone(self.service.log(attempt)["text"])
        with self.assertRaises(DeveloperTestError):
            self.service.proposal(attempt)
        restarted = DeveloperTests(DeveloperStore(self.store.path), self.transport, self.service.inventory_source)
        restarted.refresh(attempt)
        row = restarted.refresh(attempt)
        self.assertEqual(row["state"], "passed")
        self.assertEqual(restarted.log(attempt)["text"], "ខ")
        self.assertEqual(developer_measurements(self.store)["rows"], [])
        proposal = restarted.proposal(attempt)
        payload = {k: proposal[k] for k in ("proposal_sha256", "expected_store_revision")}
        payload["owner_action_id"] = "owner-approve-one"
        approval = restarted.approve(attempt, payload, "Owner")
        self.assertEqual(restarted.approve(attempt, payload, "Owner"), approval)
        self.assertEqual(len(developer_measurements(self.store)["rows"]), 1)
        stale = {**payload, "owner_action_id": "owner-approve-two"}
        with self.assertRaises(DeveloperTestError):
            restarted.approve(attempt, stale, "Owner")
        restarted.rollback("owner-approve-one", self.store.evidence()["store_revision"], "Owner")
        self.assertEqual(developer_measurements(self.store)["rows"], [])
        self.assertEqual(len(self.store.evidence()["approvals"]), 1)
        self.assertEqual(self.operations.count("submit"), 1)

    def test_timed_out_switch_reconciles_by_readback_without_post_replay(self):
        raw = encoded({"format": "studio.developer-test.node-setting.v1", "enabled": False,
                       "expected_revision": self.cap["setting"]["revision"], "request_id": "switch-action-one"})
        answer = {**self.cap["setting"], "enabled": False, "revision": "revision-after",
                  "request_id": "switch-action-one"}
        operations = []
        def uncertain(controller, node, operation, **kwargs):
            self.assertEqual(kwargs["pin"], "fixture-pin")
            operations.append(operation)
            if operation == "set_setting":
                raise DeveloperTestError(502, "TRANSPORT_UNCERTAIN", "Fixture reply lost.")
            return 200, encoded(answer)
        with patch.object(self.transport, "send", side_effect=uncertain):
            with self.assertRaises(DeveloperTestError):
                self.service.setting("site", self.cap["node_id"], raw)
            restarted = DeveloperTests(DeveloperStore(self.store.path), self.transport, self.service.inventory_source)
            self.assertEqual(restarted.setting("site", self.cap["node_id"], raw), answer)
            self.assertEqual(restarted.setting("site", self.cap["node_id"], raw), answer)
            with self.assertRaises(DeveloperTestError):
                restarted.setting("site", self.cap["node_id"], raw + b" ")
        self.assertEqual(operations, ["set_setting", "setting"])

    def test_off_pause_and_offline_refuse_before_submission(self):
        self.cap["setting"]["enabled"] = False
        with self.assertRaises(DeveloperTestError) as caught:
            self.service.run(self.wrapper)
        self.assertEqual(caught.exception.code, "UNVERIFIED")
        self.assertNotIn("submit", self.operations)
        self.assertEqual(self.store.rows(), [])
        self.cap["setting"]["enabled"] = True
        original = self.service.inventory_source()
        for field, expected in (("node_paused", "TARGET_PAUSED"), ("reachable", "TARGET_OFFLINE")):
            snapshot = json.loads(json.dumps(original))
            snapshot["machines"][0][field] = field == "node_paused"
            self.service.inventory_source = lambda: snapshot
            self.operations.clear()
            with self.assertRaises(DeveloperTestError) as caught:
                self.service.run(self.wrapper)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(self.operations, [])
            self.assertEqual(self.store.rows(), [])

    def test_changed_receipt_bytes_cannot_support_approval(self):
        self.tamper_receipt = True
        attempt = self.service.run(self.wrapper)["runs"][0]["id"]
        self.assertEqual(self.service.refresh(attempt)["state"], "recovery_hold")
        self.assertIn("receipt bytes", self.store.get(attempt)["reason"])
        self.assertIsNone(self.store.get(attempt)["receipt_bytes"])
        with self.assertRaises(DeveloperTestError):
            self.service.proposal(attempt)
        self.assertEqual(self.operations.count("submit"), 1)


class OwnerRouteTests(unittest.TestCase):
    def test_remote_bare_key_cannot_read_or_mutate_developer_mode(self):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient
        from gateway import admin, developer_admin
        app = FastAPI()
        app.include_router(developer_admin.router, prefix="/admin")
        @app.exception_handler(admin.AdminError)
        async def refused(request, error):
            return JSONResponse({"detail": error.detail}, status_code=error.status)
        with patch.object(developer_admin, "service", side_effect=AssertionError("Owner guard must run first")):
            with TestClient(app) as client:
                for method, path in (("GET", "/admin/api/developer-tests"),
                                     ("POST", "/admin/api/developer-tests/runs")):
                    response = client.request(method, path, headers={"Authorization": "Bearer story-test-key"})
                    self.assertEqual(response.status_code, 403)

    def test_every_developer_route_has_owner_guard(self):
        from gateway import developer_admin
        for route in developer_admin.router.routes:
            self.assertTrue(any(item.call is developer_admin.owner for item in route.dependant.dependencies), route.path)


if __name__ == "__main__":
    unittest.main()
