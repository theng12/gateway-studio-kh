"""Account-bound HTTP/claim/recovery fixtures; no browser, provider or paid calls."""
import json
import os
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_a_zero_priced_job_claims_end_to_end(self):
        """A free image is a literal 0 quote: it must claim, and 0 must never read as "no price"."""
        self.assertEqual(ea.credits({"value": 0, "unit": "credits", "source": "flow.nzlxg",
                                     "observedAt": iso()}, now=time.time())["value"], 0)
        self.ready(observed_account(balance={"value": 0, "unit": "credits",
                                             "source": "fixture.balance", "observedAt": iso()}))
        job = ew.broker.offer("video", {"prompt": "scene"})
        answer = self.claim(self.quote(job, value=0))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["reservation"]["value"], 0)
        self.assertEqual(answer.json()["reservation"]["unit"], "credits")
        self.assertEqual(answer.json()["job"], job.payload)

    def test_unknown_balance_and_reset_never_become_free_daily_allowance(self):
        """A null balance stays unknown: the claim may proceed on a stated price, never on a zero."""
        self.ready(observed_account(balance=None, resetAt=None))
        job = ew.broker.offer("video", {"prompt": "scene"})
        answer = self.claim(self.quote(job))
        self.assertEqual(answer.status_code, 200, answer.text)
        # The price is still what authorizes the spend; only the balance is unknown.
        self.assertGreater(answer.json()["reservation"]["value"], 0)
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

    def test_close_requires_an_explicit_pause_observation(self):
        for path in ("account", "state"):
            for paused in (None, False, True):
                with self.subTest(path=path, paused=paused):
                    revision = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]["revision"]
                    observation = observed_account(paused=paused, enabled=False, controlRevision=revision)
                    if paused is None:
                        observation.pop("paused")
                        observation["paused_observed"] = True  # Untrusted input cannot invent presence.
                    if path == "account":
                        response = self.report(observation)
                    else:
                        state = {k: v for k, v in observation.items() if k in (
                            "source", "observedAt", "paused", "paused_observed", "enabled", "activity", "controlRevision")}
                        response = self.client.post("/api/workers/telemetry", headers=self.auth(), json={
                            "workerId": "account-video", "kind": "video", "telemetry": {"state": state}})
                    self.assertEqual(response.status_code, 200)
                    row = self.status()["workers"][0]
                    self.assertTrue(row["fresh"])
                    self.assertEqual(row["safeToClose"], paused is True)
                    self.assertEqual(row["state"]["paused"], True if paused is None else paused)

    def test_omitted_pause_still_blocks_paid_claims(self):
        observation = observed_account()
        observation.pop("paused")
        self.ready(observation)
        job = ew.broker.offer("video", {"prompt": "scene"})
        self.assertEqual(self.claim(self.quote(job)).status_code, 204)
        self.assertEqual(job.state, "pending")
        self.assertIsNone(runtime.store.get(job.gateway_job_id)["upstream_job_id"])

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
            for prefix in ("Bearer ", "Bearer   ", "bearer ", "Token ", "Token\t", " \tToken\t  ", "Bearer\t", "", " \t"):
                headers = {"Authorization": prefix + token}
                with self.subTest(prefix=prefix, manager=token == self.manager):
                    read = self.local.get("/admin/api/status", headers=headers)
                    mint = self.local.post("/admin/api/extension-workers/manager", headers=headers,
                                           json={"manager_id": "forbidden", "worker_ids": ["account-video"]})
                    enable = self.local.put("/admin/api/extension-workers/accounts/enabled", headers=headers,
                                            json={"provider": "flow", "account_id": "fixture-account", "enabled": True})
                    for response in (read, mint, enable):
                        self.assertEqual(response.status_code, 403)
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

    def test_pacing_only_preserves_an_intervening_dispatch_or_close_change(self):
        for cached, current in (((False, True), (True, False)), ((True, False), (False, True))):
            with self.subTest(current=current):
                stale = self.control(dispatchEnabled=cached[0], closeRequested=cached[1]).json()
                changed = self.control(dispatchEnabled=current[0], closeRequested=current[1]).json()
                self.assertNotEqual(stale["workers"][0]["control"]["revision"], changed["workers"][0]["control"]["revision"])
                response = self.client.post("/api/workers/manager/control", headers=self.auth(self.manager), json={
                    "worker_ids": ["account-video"], "pacing": {"minSeconds": 0, "maxSeconds": 0}})
                self.assertEqual(response.status_code, 200)
                control = response.json()["workers"][0]["control"]
                self.assertEqual((control["dispatchEnabled"], control["closeRequested"]), current)
                self.assertEqual(control["pacing"], {"minSeconds": 0, "maxSeconds": 0})
                self.assertNotEqual(control["revision"], changed["workers"][0]["control"]["revision"])

    def test_bulk_pacing_only_preserves_each_workers_independent_controls(self):
        ew.mint("account-image", "image")
        manager = ew.mint("pacing-manager", "manager", worker_ids=["account-video", "account-image"])
        self.control(dispatchEnabled=True)
        self.client.post("/api/workers/manager/control", headers=self.auth(manager), json={
            "worker_ids": ["account-image"], "dispatchEnabled": False, "closeRequested": True})
        response = self.client.post("/api/workers/manager/control", headers=self.auth(manager), json={
            "worker_ids": ["account-video", "account-image"], "pacing": {"minSeconds": 0, "maxSeconds": 5}})
        self.assertEqual(response.status_code, 200)
        rows = {row["worker_id"]: row["control"] for row in response.json()["workers"]}
        self.assertEqual((rows["account-video"]["dispatchEnabled"], rows["account-video"]["closeRequested"]), (True, False))
        self.assertEqual((rows["account-image"]["dispatchEnabled"], rows["account-image"]["closeRequested"]), (False, True))
        for row in rows.values():
            self.assertEqual(row["pacing"], {"minSeconds": 0, "maxSeconds": 5})
        self.assertEqual(rows["account-video"]["revision"], rows["account-image"]["revision"])

    def test_incomplete_or_mixed_pacing_only_requests_do_not_write(self):
        revision = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]["revision"]
        for invalid in ({}, {"pacing": None}, {"pacing": {"minSeconds": 0, "maxSeconds": 1}, "closeRequested": True},
                        {"pacing": {"minSeconds": 0, "maxSeconds": 1}, "dispatchEnabled": None}):
            response = self.client.post("/api/workers/manager/control", headers=self.auth(self.manager), json={
                "worker_ids": ["account-video"], **invalid})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(self.status()["workers"][0]["control"]["revision"], revision)

    def test_credential_generation_persists_and_remint_invalidates_close_readback(self):
        response = self.control(dispatchEnabled=False, closeRequested=True)
        control = response.json()["workers"][0]["control"]
        generation = control["credentialGeneration"]
        self.assertEqual(len(generation), 32)
        self.assertEqual(self.client.get("/api/workers/health", headers=self.auth()).json()["control"]["credentialGeneration"], generation)
        self.report(observed_account(paused=True, controlRevision=control["revision"]))
        self.assertTrue(self.status()["workers"][0]["safeToClose"])
        ew.broker.reset()
        self.assertEqual(self.status()["workers"][0]["control"]["credentialGeneration"], generation)
        self.report(observed_account(paused=True, controlRevision=control["revision"]))
        self.assertTrue(self.status()["workers"][0]["safeToClose"])
        replacement = ew.mint("account-video", "video")
        row = self.status()["workers"][0]
        self.assertNotEqual(row["control"]["credentialGeneration"], generation)
        self.assertFalse(row["control"]["dispatchEnabled"])
        self.assertIsNone(row["control"]["revision"])
        self.assertFalse(row["safeToClose"])
        self.assertIsNone(row["state"])
        self.assertIsNone(row["accountBinding"])
        self.assertEqual(self.report().status_code, 401)
        health = self.client.get("/api/workers/health", headers=self.auth(replacement))
        self.assertEqual(health.json()["control"]["credentialGeneration"], row["control"]["credentialGeneration"])

    def test_missing_legacy_generation_cannot_acknowledge_safe_close(self):
        revision = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]["revision"]
        self.report(observed_account(paused=True, controlRevision=revision))
        with ew.admin_session._lock:
            rows = ew._rows()
            rows[ew.admin_session._digest(self.token)].pop("credential_generation")
            ew.admin_session._write(ew._tokens_path(), {"workers": rows})
        row = self.status()["workers"][0]
        self.assertIsNone(row["control"]["credentialGeneration"])
        self.assertFalse(row["safeToClose"])

    def test_bulk_pause_keeps_a_revoked_member_explicitly_unsafe(self):
        ew.mint("account-image", "image")
        manager = ew.mint("bulk-manager", "manager", worker_ids=["account-video", "account-image"])
        ew.revoke("account-image")
        response = self.client.post("/api/workers/manager/control", headers=self.auth(manager), json={
            "worker_ids": ["account-video", "account-image"], "dispatchEnabled": False, "closeRequested": True})
        self.assertEqual(response.status_code, 200)
        rows = {row["worker_id"]: row for row in response.json()["workers"]}
        self.assertTrue(rows["account-video"]["control"]["revision"])
        self.assertFalse(rows["account-image"]["has_live_token"])
        self.assertIsNone(rows["account-image"]["control"]["credentialGeneration"])
        self.assertIsNone(rows["account-image"]["control"]["revision"])
        revision = rows["account-video"]["control"]["revision"]
        self.report(observed_account(paused=True, controlRevision=revision))
        rows = self.client.get("/api/workers/manager", headers=self.auth(manager)).json()["workers"]
        self.assertTrue(next(row for row in rows if row["worker_id"] == "account-video")["safeToClose"])
        self.assertFalse(all(row["safeToClose"] for row in rows))

    def test_unavailable_failed_and_invalid_readback_never_grants_close(self):
        self.assertEqual(self.client.get("/api/workers/manager").status_code, 401)
        with patch.dict(os.environ, {ew.SWITCH: "0"}):
            self.assertEqual(self.client.get("/api/workers/manager", headers=self.auth(self.manager)).status_code, 404)
        revision = self.control(dispatchEnabled=False, closeRequested=True).json()["workers"][0]["control"]["revision"]
        invalid = self.control(dispatchEnabled=True, closeRequested=True)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "MANAGER_CONTROL_INVALID")
        self.assertEqual(self.status()["workers"][0]["control"]["revision"], revision)
        with patch.dict(os.environ, {"GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES": "64"}):
            oversized = self.client.post("/api/workers/manager/control", headers=self.auth(self.manager), content=b" " * 65)
        self.assertEqual(oversized.status_code, 413)
        failed_client = TestClient(api.app, client=("100.64.0.9", 40102), raise_server_exceptions=False)
        with patch.object(runtime.store, "extension_account_worker_busy", side_effect=OSError("fixture readback failure")):
            failed = failed_client.get("/api/workers/manager", headers=self.auth(self.manager))
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("safeToClose", failed.text)

    def test_claim_wins_pause_race_and_remains_authoritatively_busy(self):
        self.ready()
        job = ew.broker.offer("video", {"prompt": "scene"})
        entered, release, pause_parsed = threading.Event(), threading.Event(), threading.Event()
        reserve, parse = runtime.store.extension_account_reserve, ew._account_body

        def blocked_reserve(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture reservation barrier timed out")
            return reserve(*args, **kwargs)

        async def parsed_body(request):
            body = await parse(request)
            if request.url.path == "/api/workers/manager/control":
                pause_parsed.set()
            return body

        with patch.object(runtime.store, "extension_account_reserve", blocked_reserve), patch.object(ew, "_account_body", parsed_body):
            with ThreadPoolExecutor(max_workers=2) as pool:
                claim = pool.submit(self.claim, self.quote(job))
                try:
                    self.assertTrue(entered.wait(5))
                    pause = pool.submit(self.control, dispatchEnabled=False, closeRequested=True)
                    self.assertTrue(pause_parsed.wait(5))
                finally:
                    release.set()
                self.assertEqual(claim.result(timeout=5).status_code, 200)
                response = pause.result(timeout=5)
        self.assertEqual(response.status_code, 200)
        row = response.json()["workers"][0]
        self.assertFalse(row["control"]["dispatchEnabled"])
        self.assertEqual(row["current_job"]["jobId"], job.job_id)
        self.assertTrue(row["durableBusy"])
        self.assertFalse(row["safeToClose"])

    def test_pause_wins_claim_race_without_reservation_or_execution(self):
        self.ready()
        job = ew.broker.offer("video", {"prompt": "scene"})
        entered, release = threading.Event(), threading.Event()
        claim_original = ew.broker.claim

        def blocked_claim(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture claim barrier timed out")
            return claim_original(*args, **kwargs)

        with patch.object(ew.broker, "claim", blocked_claim):
            with ThreadPoolExecutor(max_workers=1) as pool:
                claim = pool.submit(self.claim, self.quote(job))
                try:
                    self.assertTrue(entered.wait(5))
                    response = self.control(dispatchEnabled=False, closeRequested=True)
                    self.assertEqual(response.status_code, 200)
                finally:
                    release.set()
                self.assertEqual(claim.result(timeout=5).status_code, 204)
        self.assertEqual(job.state, "pending")
        self.assertIsNone(runtime.store.get(job.gateway_job_id)["upstream_job_id"])
        self.assertFalse(self.status()["workers"][0]["durableBusy"])
        revision = response.json()["workers"][0]["control"]["revision"]
        self.report(observed_account(paused=True, controlRevision=revision))
        self.assertTrue(self.status()["workers"][0]["safeToClose"])

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
