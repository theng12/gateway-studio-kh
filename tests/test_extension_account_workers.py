"""Account-bound HTTP/claim/recovery fixtures; no browser, provider or paid calls."""
import json
import os
import time
import unittest
from unittest.mock import patch

from tests._isolation import sandbox_env
sandbox_env("gateway-extension-accounts-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")
from fastapi.testclient import TestClient
from gateway import api, extension_workers as ew, extension_accounts as ea, runtime
from tests.extension_account_fixture import fixture_offers, observed_account, iso


class AccountWorkersTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {ew.SWITCH: "1"})
        env.start()
        self.addCleanup(env.stop)
        ew.reset_for_tests()
        self.addCleanup(ew.reset_for_tests)
        fixture_offers(self)
        self.client = TestClient(api.app, client=("100.64.0.9", 40100))
        self.local = TestClient(api.app, client=("127.0.0.1", 40101))
        self.token = ew.mint("account-video", "video")
        self.manager = ew.mint("manager-fixture", "manager", worker_ids=["account-video"])

    def auth(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def control(self, **changes):
        return self.client.post("/api/workers/manager/control", headers=self.auth(self.manager),
                                json={"worker_ids": ["account-video"], "dispatchEnabled": True, **changes})

    def status(self):
        return self.client.get("/api/workers/manager", headers=self.auth(self.manager)).json()

    def report(self, account=None, **changes):
        observation = account or observed_account()
        response = self.client.post("/api/workers/telemetry", headers=self.auth(),
                                    json={"workerId": "account-video", "kind": "video",
                                          "account": observation, **changes})
        if response.status_code == 200:
            self.last_account = observation
        return response

    def ready(self, account=None):
        self.assertEqual(self.report(account).status_code, 200)
        self.assertTrue(runtime.store.extension_account_set_enabled("flow", (account or {}).get("accountId", "fixture-account"), True))
        control = self.control()
        self.assertEqual(control.status_code, 200)
        revision = control.json()["workers"][0]["control"]["revision"]
        self.assertEqual(self.report({**(account or observed_account()), "observedAt": iso(), "controlRevision": revision}).status_code, 200)

    def claim(self, quote=None, **changes):
        body = {"workerId": "account-video", "kind": "video", **changes}
        if quote is not None:
            body["quote"] = quote
            if hasattr(self, "last_account"):
                body["account"] = self.last_account
        return self.client.post("/api/workers/claim", headers=self.auth(), json=body)

    def quote(self, job, **changes):
        value = {"provider": "flow", "accountId": "fixture-account",
                 "payloadSha256": ea.payload_sha256(job.payload), "value": 10,
                 "unit": "credits", "source": "fixture.quote", "observedAt": iso()}
        value.update(changes)
        return value

    def test_default_off_cannot_be_overridden_by_local_enabled_or_quote(self):
        self.report()
        job = ew.broker.offer("video", {"kind": "video", "prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job)).status_code, 204)
        revision = self.control().json()["workers"][0]["control"]["revision"]
        self.report(observed_account(controlRevision=revision))
        refused = self.claim(self.quote(job))
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(job.state, "pending")
        self.assertIsNone(runtime.store.get(job.gateway_job_id)["upstream_job_id"])

    def test_preview_is_unclaimed_and_executable_claim_is_durably_checkpointed(self):
        self.ready()
        job = ew.broker.offer("video", {"kind": "video", "prompt": "scene", "durationSec": 4, "outputCount": 1})
        preview = self.claim().json()
        self.assertIsNone(preview["job"])
        self.assertIs(preview["quoteRequired"]["executionAuthorized"], False)
        self.assertEqual(job.state, "pending")
        answer = self.claim(self.quote(job))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["job"], job.payload)
        self.assertEqual(answer.json()["accountBinding"], {"provider": "flow", "accountId": "fixture-account"})
        self.assertEqual(runtime.store.get(job.gateway_job_id)["upstream_job_id"], ew.upstream_id("account-video", job.job_id))
        repeated = self.claim(self.quote(job))
        self.assertEqual(repeated.status_code, 409)
        self.assertNotIn("job", repeated.json())

    def test_unknown_balance_and_reset_never_become_free_daily_allowance(self):
        self.ready(observed_account(balance=None, resetAt=None))
        job = ew.broker.offer("video", {"prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job)).status_code, 409)
        account = self.status()["accounts"][0]
        self.assertIsNone(account["balance"])
        self.assertIsNone(account["available"])
        self.assertIsNone(account["reset_at"])
        self.assertIsNone(account["credit_buckets"])

    def test_quote_matches_every_payload_field_and_the_observed_account(self):
        self.ready()
        job = ew.broker.offer("video", {"durationSec": 4, "outputCount": 1, "referenceImages": []})
        for changes in ({"accountId": "other"}, {"provider": "fish"}, {"payloadSha256": "0" * 64}):
            response = self.claim(self.quote(job, **changes))
            self.assertNotIn("job", response.json() if response.content else {})
            self.assertEqual(job.state, "pending")
        for changes in ({"unit": "dollars"}, {"observedAt": iso(time.time() - 1000)}, {"value": True}):
            self.assertEqual(self.claim(self.quote(job, **changes)).status_code, 409)
            self.assertEqual(job.state, "pending")

    def test_pause_close_waits_for_matching_fresh_ack_and_survives_broker_restart(self):
        response = self.control(dispatchEnabled=False, closeRequested=True, pacing={"minSeconds": 0, "maxSeconds": 0})
        revision = response.json()["workers"][0]["control"]["revision"]
        self.assertFalse(response.json()["workers"][0]["safeToClose"])
        state = observed_account(paused=True, enabled=False, controlRevision=revision, pacing={"minSeconds": 0, "maxSeconds": 0})
        self.report(state)
        row = self.status()["workers"][0]
        self.assertTrue(row["safeToClose"])
        self.assertEqual(row["state"]["pacing"], {"minSeconds": 0, "maxSeconds": 0})
        with patch.object(ew.time, "time", return_value=time.time() + 1000):
            self.assertFalse(self.status()["workers"][0]["safeToClose"])
        ew.broker.reset()
        row = self.status()["workers"][0]
        self.assertFalse(row["control"]["dispatchEnabled"])
        self.assertFalse(row["safeToClose"])

    def test_unknown_account_can_ack_pause_without_inventing_a_binding(self):
        response = self.control(dispatchEnabled=False, closeRequested=True)
        revision = response.json()["workers"][0]["control"]["revision"]
        state = {k: v for k, v in observed_account(paused=True, controlRevision=revision).items()
                 if k in ("source", "observedAt", "paused", "controlRevision", "enabled", "activity")}
        response = self.client.post("/api/workers/telemetry", headers=self.auth(), json={
            "workerId": "account-video", "kind": "video", "telemetry": {"state": state}})
        self.assertEqual(response.status_code, 200)
        row = self.status()["workers"][0]
        self.assertTrue(row["safeToClose"])
        self.assertIsNone(row["accountBinding"])

    def test_held_paid_job_prevents_account_rebind_and_safe_close_even_after_memory_reset(self):
        self.ready()
        job = ew.broker.offer("video", {"prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job)).status_code, 200)
        self.assertTrue(ew.broker.hold(job))
        self.assertEqual(self.report(observed_account("another-account")).status_code, 422)
        control = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]
        self.report(observed_account(paused=True, controlRevision=control["revision"]))
        ew.broker.reset()
        row = self.status()["workers"][0]
        self.assertTrue(row["durableBusy"])
        self.assertFalse(row["safeToClose"])
        reservation = runtime.store.extension_account_reservation("account-video", ew.worker_for(self.token)["_token_id"], job.job_id)
        self.assertEqual(reservation["state"], "held")

    def test_scope_revocation_and_app_only_minting_never_leak_tokens(self):
        self.assertEqual(self.client.get("/api/workers/health", headers=self.auth(self.manager)).status_code, 401)
        self.assertEqual(self.client.get("/api/workers/manager", headers=self.auth()).status_code, 401)
        self.assertEqual(self.control(worker_ids=["unpaired"]).status_code, 403)
        self.assertEqual(self.client.post("/admin/api/extension-workers/manager", headers=self.auth(self.manager),
                                         json={"manager_id": "evil", "worker_ids": ["unpaired"]}).status_code, 403)
        status = json.dumps(self.status())
        self.assertNotIn(self.token, status)
        self.assertNotIn(self.manager, status)
        self.assertNotIn(ew.admin_session._digest(self.token), status)
        ew.revoke("manager-fixture")
        self.assertEqual(self.client.get("/api/workers/manager", headers=self.auth(self.manager)).status_code, 401)

    def test_scoped_credentials_never_inherit_loopback_admin_trust(self):
        for token in (self.token, self.manager):
            for prefix in ("Bearer ", "Bearer   ", "bearer "):
                headers = {"Authorization": prefix + token}
                with self.subTest(prefix=prefix, manager=token == self.manager):
                    read = self.local.get("/admin/api/status", headers=headers)
                    mint = self.local.post("/admin/api/extension-workers/manager", headers=headers,
                                           json={"manager_id": "forbidden", "worker_ids": ["account-video"]})
                    enable = self.local.put("/admin/api/extension-workers/accounts/enabled", headers=headers,
                                            json={"provider": "flow", "account_id": "fixture-account", "enabled": True})
                    for response in (read, mint, enable):
                        self.assertEqual(response.status_code, 403, response.text)
                        self.assertEqual(response.json()["code"], "WORKER_SCOPE_REFUSED")

    def test_new_control_revision_requires_ack_before_execution(self):
        self.ready()
        job = ew.broker.offer("video", {"prompt": "scene"})
        previous = self.last_account["controlRevision"]
        revision = self.control(pacing={"minSeconds": 0, "maxSeconds": 0}).json()["workers"][0]["control"]["revision"]
        self.assertNotEqual(previous, revision)
        self.assertEqual(self.claim(self.quote(job)).status_code, 204)
        self.assertIsNone(runtime.store.get(job.gateway_job_id)["upstream_job_id"])
        self.assertEqual(self.report(observed_account(controlRevision=revision)).status_code, 200)
        self.assertEqual(self.claim(self.quote(job)).status_code, 200)

    def test_older_pause_ack_cannot_replace_new_generating_state(self):
        revision = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]["revision"]
        paused = observed_account(paused=True, controlRevision=revision)
        self.assertEqual(self.report(paused).status_code, 200)
        self.assertTrue(self.status()["workers"][0]["safeToClose"])
        self.assertEqual(self.report(observed_account(activity="generating", paused=False, controlRevision=revision)).status_code, 200)
        self.assertEqual(self.report(paused).status_code, 200)
        worker = self.status()["workers"][0]
        self.assertEqual(worker["state"]["activity"], "generating")
        self.assertFalse(worker["safeToClose"])

    def test_image_and_video_share_one_atomic_account_budget(self):
        self.ready()
        image_token = ew.mint("account-image", "image")
        shared_manager = ew.mint("shared-manager", "manager", worker_ids=["account-video", "account-image"])
        control = self.client.post("/api/workers/manager/control", headers=self.auth(shared_manager), json={
            "worker_ids": ["account-video", "account-image"], "dispatchEnabled": True})
        revision = control.json()["workers"][0]["control"]["revision"]
        self.report(observed_account(controlRevision=revision))
        image_account = observed_account(controlRevision=revision)
        self.assertEqual(self.client.post("/api/workers/telemetry", headers=self.auth(image_token), json={
            "workerId": "account-image", "kind": "image", "account": image_account}).status_code, 200)
        video = ew.broker.offer("video", {"prompt": "video"})
        image = ew.broker.offer("image", {"prompt": "image"})
        self.assertEqual(self.claim(self.quote(video, value=60)).status_code, 200)
        response = self.client.post("/api/workers/claim", headers=self.auth(image_token), json={
            "workerId": "account-image", "kind": "image", "account": image_account,
            "quote": self.quote(image, value=60)})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(image.state, "pending")
        scoped = self.status()
        self.assertEqual(len(scoped["accounts"]), 1)
        self.assertEqual(scoped["accounts"][0]["reserved"], 60)
        self.assertEqual(scoped["accounts"][0]["available"], 40)
        self.assertEqual(scoped["accounts"][0]["workers"], ["account-video"])
        self.assertNotIn("account-image", json.dumps(scoped))
        lower = observed_account(controlRevision=revision, balance={
            "value": 10, "unit": "credits", "source": "fixture.balance", "observedAt": iso()})
        self.assertEqual(self.report(lower).status_code, 200)
        deficit = self.status()["accounts"][0]
        self.assertEqual(deficit["reserved"], 60)
        self.assertEqual(deficit["available"], -50)

    def test_future_observation_and_unbounded_bodies_fail_before_assignment(self):
        self.assertEqual(self.report(observed_account(observedAt=iso(time.time() + 1000))).status_code, 422)
        with patch.dict(os.environ, {"GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "64"}):
            response = self.client.post("/api/workers/telemetry", headers=self.auth(), content=iter([b" " * 40, b" " * 40]))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(ew.broker.depth("video"), 0)

    def test_actual_pacing_and_provider_cooldown_gate_claim(self):
        self.ready(observed_account(nextEligibleAt=iso(time.time() + 1000), pacing={"minSeconds": 0, "maxSeconds": 0}))
        job = ew.broker.offer("video", {"prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job)).status_code, 204)
        self.assertEqual(job.state, "pending")
        self.report(observed_account(nextEligibleAt=None))
        self.assertEqual(self.claim(self.quote(job)).status_code, 204)

    def test_wire_length_boundaries_survive_store_and_claim(self):
        unit = "u" * 64
        account = observed_account(plan="p" * 120, balance={
            "value": 100, "unit": unit, "source": "fixture.balance", "observedAt": iso()})
        self.ready(account)
        job = ew.broker.offer("video", {"prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job, unit=unit)).status_code, 200)
        self.assertEqual(self.report({**account, "plan": "p" * 121}).status_code, 422)

    def test_malformed_numeric_observations_refuse_without_coercion(self):
        for value in (True, -1, float("inf"), "50", 10**1000):
            with self.subTest(value=str(value)[:40]), self.assertRaises(ValueError):
                ea.credits({"value": value, "unit": "credits", "source": "fixture.balance", "observedAt": iso()}, now=time.time())
        self.assertEqual(ea.credits({"value": 0, "unit": "credits", "source": "fixture.balance", "observedAt": iso()}, now=time.time())["value"], 0)


if __name__ == "__main__":
    unittest.main()
