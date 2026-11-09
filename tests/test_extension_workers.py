"""Browser-extension workers (wire v1): auth, one job per worker, paid-credit holds, upload door."""
import base64
import struct
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests._isolation import sandbox_env
sandbox_env("gateway-extension-workers-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, extension_workers as ew, runtime, video_capabilities
from gateway.adapters import BackendError, BackendRequest, adapter_for, serves
from gateway.adapters.chrome_extension import ChromeExtensionBackend
from gateway.config import APP_SUPPORT, ARTIFACT_DIR
from gateway.providers.definitions import IMPLEMENTED_KINDS, KIND_CHROME_EXTENSION
from gateway.store import RECOVERY_UNKNOWN

MP4 = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0" + struct.pack(">I4s", 8, b"moov")
_FLOW = [r for r in video_capabilities.VIDEO_CAPABILITIES if r.provider_id == "story-studio-video"]
TEXT = next(r for r in _FLOW if "text-to-video" in r.modes)
IMAGE = next(r for r in _FLOW if "reference-to-video" in r.modes)
ON = {ew.SWITCH: "1", "GATEWAY_EXTENSION_WORKER_LEASE_SECONDS": "2",
      "GATEWAY_EXTENSION_WORKER_CLAIM_WAIT_SECONDS": "2"}


class _Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)
        ew.reset_for_tests()
        self.addCleanup(ew.reset_for_tests)
        self.client = TestClient(api.app, client=("100.64.0.9", 40000))  # not loopback
        self.token = ew.mint("chrome-a", "video")

    def auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def claim(self, token=None, worker="chrome-a", **extra):
        return self.client.post("/api/workers/claim", headers=self.auth(token),
                                json={"workerId": worker, "kind": "video", **extra})

    def request(self, record=TEXT, **params):
        captured = []
        req = BackendRequest(provider_id="story-studio-video", model_id=record.model_id, modality="video",
                             params={"prompt": "a cat", "duration_seconds": record.duration_min,
                                     "aspect_ratio": "16:9", **params},
                             values={}, base_url="", checkpoint=captured.append)
        return req, captured

    def run_adapter(self, req):
        box = {}

        def go():
            try:
                box["result"] = ChromeExtensionBackend().generate(req)
            except BackendError as exc:
                box["error"] = exc
        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread, box

    def wait_offered(self):
        for _ in range(100):
            if ew.broker.depth("video"):
                return
            time.sleep(0.02)
        self.fail("the adapter never offered a job")


class RegistrationTests(unittest.TestCase):
    def test_kind_is_registered_video_only(self):
        self.assertIn(KIND_CHROME_EXTENSION, IMPLEMENTED_KINDS)
        definition = type("D", (), {"kind": KIND_CHROME_EXTENSION})()
        self.assertIsInstance(adapter_for(definition), ChromeExtensionBackend)
        self.assertTrue(serves(definition, "video"))
        self.assertFalse(serves(definition, "image"))


class SwitchAndAuthTests(_Base):
    def test_routes_are_404_when_switched_off(self):
        with patch.dict(os.environ, {ew.SWITCH: "0"}):
            for method, path in (("get", "/api/workers/health"), ("post", "/api/workers/claim"),
                                 ("post", "/api/workers/jobs/x/progress"),
                                 ("post", "/api/workers/jobs/x/fail")):
                answer = getattr(self.client, method)(path, headers=self.auth())
                self.assertEqual(answer.status_code, 404, path)
            local = TestClient(api.app, client=("127.0.0.1", 40001))
            self.assertEqual(local.get("/admin/api/extension-workers").status_code, 404)

    def test_wrong_token_and_gateway_key_are_refused(self):
        self.assertEqual(self.client.get("/api/workers/health", headers=self.auth("ekw_wrong")).status_code, 401)
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "gw-key"}):
            self.assertEqual(self.client.get("/api/workers/health",
                                             headers=self.auth("gw-key")).status_code, 401)
        self.assertEqual(self.client.get("/api/workers/health", headers=self.auth()).status_code, 200)

    def test_token_is_stored_hashed_never_echoed(self):
        text = (APP_SUPPORT / "extension-workers.json").read_text()
        self.assertNotIn(self.token, text)
        self.assertEqual(oct((APP_SUPPORT / "extension-workers.json").stat().st_mode & 0o777), "0o600")
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        status = local.get("/admin/api/extension-workers").json()
        self.assertNotIn(self.token, json.dumps(status))
        self.assertNotIn(self.token, json.dumps(self.client.get("/api/workers/health",
                                                                 headers=self.auth()).json()))

    def test_revoke_is_immediate_and_remint_replaces(self):
        second = ew.mint("chrome-a", "video")
        self.assertIsNone(ew.worker_for(self.token))
        self.assertEqual(ew.revoke("chrome-a"), 1)
        self.assertEqual(self.client.get("/api/workers/health", headers=self.auth(second)).status_code, 401)

    def test_mint_is_this_macs_app_only(self):
        remote = self.client.post("/admin/api/extension-workers", json={"worker_id": "x", "kind": "video"})
        self.assertIn(remote.status_code, (401, 403))

    def test_token_bound_to_its_worker_and_kind(self):
        self.assertEqual(self.claim(worker="chrome-b").status_code, 403)


class ClaimTests(_Base):
    def test_no_job_is_204_and_unknown_credits_stay_null(self):
        self.assertEqual(self.claim().status_code, 204)
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        row = local.get("/admin/api/extension-workers").json()["workers"][0]
        self.assertIsNone(row["credits"])
        self.assertTrue(row["online"])
        self.claim(telemetry={"credits": {"value": 11, "source": "flow.nzlxg", "observedAt": "2026-11-09T10:00:00Z"}})
        row = local.get("/admin/api/extension-workers").json()["workers"][0]
        self.assertEqual(row["credits"]["value"], 11)
        self.claim(telemetry={"credits": {"value": None}})   # not known now: the last stated stays
        self.assertEqual(local.get("/admin/api/extension-workers").json()["workers"][0]["credits"]["value"], 11)

    def test_one_job_per_worker_and_claim_is_idempotent(self):
        self.claim()
        ew.broker.offer("video", {"prompt": "one"})
        ew.broker.offer("video", {"prompt": "two"})
        first = self.claim().json()["job"]
        again = self.claim().json()["job"]
        self.assertEqual(first["jobId"], again["jobId"])
        other = ew.mint("chrome-b", "video")
        second = self.claim(token=other, worker="chrome-b").json()["job"]
        self.assertNotEqual(second["jobId"], first["jobId"])

    def test_another_workers_job_is_404(self):
        self.claim()
        job = ew.broker.offer("video", {"prompt": "x"})
        self.claim()
        other = ew.mint("chrome-b", "video")
        for verb in ("progress", "fail"):
            answer = self.client.post(f"/api/workers/jobs/{job.job_id}/{verb}", headers=self.auth(other),
                                      json={"phase": "x", "error": "x", "ambiguous": False})
            self.assertEqual(answer.status_code, 404)


class AdapterTests(_Base):
    def test_no_worker_online_is_an_unsent_refusal(self):
        req, captured = self.request()
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "WORKER_OFFLINE")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertEqual(captured, [])

    def test_text_to_video_round_trip_checkpoints_before_the_answer(self):
        self.claim()
        req, captured = self.request()
        thread, box = self.run_adapter(req)
        self.wait_offered()
        job = self.claim().json()["job"]
        self.assertEqual((job["videoMode"], job["prompt"], job["durationSec"], job["referenceImages"]),
                         ("text", "a cat", TEXT.duration_min, []))
        self.client.post(f"/api/workers/jobs/{job['jobId']}/progress", headers=self.auth(),
                         json={"phase": "generating", "message": "…"})
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        current = local.get("/admin/api/extension-workers").json()["workers"][0]["current_job"]
        self.assertEqual((current["jobId"], current["phase"]), (job["jobId"], "generating"))
        answer = self.client.post(f"/api/workers/jobs/{job['jobId']}/result", headers=self.auth(),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c.mp4", b"MP4BYTES")})
        self.assertEqual(answer.status_code, 200)
        thread.join(5)
        self.assertEqual(box["result"].content, b"MP4BYTES")
        self.assertIsNone(ew.broker.workers["chrome-a"]["credits"])   # no creditsAfter: still unknown
        self.assertEqual(box["result"].media_type, "video/mp4")
        self.assertEqual(captured, [ew.upstream_id("chrome-a", job["jobId"])])

    def test_reference_images_are_staged_in_order(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / "ref-a.jpg"
        path.write_bytes(b"JPEG")
        self.claim()
        req, _ = self.request(IMAGE, _reference_paths=[str(path)], _reference_types=["image/jpeg"])
        thread, box = self.run_adapter(req)
        self.wait_offered()
        job = self.claim().json()["job"]
        self.assertEqual(job["videoMode"], "image")
        self.assertEqual(job["referenceImages"],
                         [{"mediaType": "image/jpeg", "dataBase64": base64.b64encode(b"JPEG").decode()}])
        self.client.post(f"/api/workers/jobs/{job['jobId']}/fail", headers=self.auth(),
                         json={"error": "flow refused the prompt", "ambiguous": False})
        thread.join(5)
        self.assertTrue(box["error"].upstream_settled)

    def test_a_missing_reference_refuses_before_offering(self):
        self.claim()
        req, _ = self.request(IMAGE, _reference_paths=[str(ARTIFACT_DIR / "gone.jpg")])
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.assertEqual(ew.broker.depth("video"), 0)

    def test_ambiguous_fail_is_a_hold(self):
        self.claim()
        req, captured = self.request()
        thread, box = self.run_adapter(req)
        self.wait_offered()
        job = self.claim().json()["job"]
        self.client.post(f"/api/workers/jobs/{job['jobId']}/fail", headers=self.auth(),
                         json={"error": "submit timed out", "ambiguous": True})
        thread.join(5)
        self.assertEqual(box["error"].code, "PROVIDER_SUBMISSION_UNCERTAIN")
        self.assertEqual(len(captured), 1)

    def test_a_fail_without_ambiguous_false_is_a_hold(self):
        self.claim()
        req, _ = self.request()
        thread, box = self.run_adapter(req)
        self.wait_offered()
        job = self.claim().json()["job"]
        self.client.post(f"/api/workers/jobs/{job['jobId']}/fail", headers=self.auth(), json={"error": "?"})
        thread.join(5)
        self.assertEqual(box["error"].code, "PROVIDER_SUBMISSION_UNCERTAIN")

    def test_silent_lease_is_a_hold_never_reoffered(self):
        self.claim()
        req, _ = self.request()
        thread, box = self.run_adapter(req)
        self.wait_offered()
        job = self.claim().json()["job"]
        thread.join(6)
        self.assertEqual(box["error"].code, "PROVIDER_SUBMISSION_UNCERTAIN")
        other = ew.mint("chrome-b", "video")
        self.assertEqual(self.claim(token=other, worker="chrome-b").status_code, 204)
        self.assertEqual(ew.broker.get(job["jobId"]).state, "held")
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        rows = {r["worker_id"]: r for r in local.get("/admin/api/extension-workers").json()["workers"]}
        self.assertEqual((rows["chrome-a"]["current_job"], rows["chrome-a"]["held_jobs"]), (None, [job["jobId"]]))
        self.assertIsNotNone(rows["chrome-a"]["created_at"])
        self.assertIsNone(rows["chrome-a"]["revoked_at"])

    def test_unclaimed_job_is_withdrawn_unsent(self):
        self.claim()
        req, captured = self.request()
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "WORKER_OFFLINE")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertEqual((captured, ew.broker.depth("video")), ([], 0))

    def test_resume_after_restart_with_a_claim_is_a_hold(self):
        req, _ = self.request()
        req.upstream_job_id = ew.upstream_id("chrome-a", "job_x")
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")


class ResultDoorTests(_Base):
    def _claimed(self):
        self.claim()
        job = ew.broker.offer("video", {"prompt": "x"})
        self.claim()
        return job

    def test_media_type_is_required_and_must_match_the_kind(self):
        job = self._claimed()
        for data in ({}, {"mediaType": "image/png"}):
            answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(),
                                      data=data, files={"file": ("c", b"x")})
            self.assertEqual(answer.status_code, 422)

    def test_credits_after_uses_the_telemetry_shape(self):
        job = self._claimed()
        after = {"value": 2, "source": "flow.nzlxg", "observedAt": "2026-11-09T10:00:00Z"}
        self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(),
                         data={"mediaType": "video/mp4", "creditsAfter": json.dumps(after)},
                         files={"file": ("c", b"x")})
        self.assertEqual(ew.broker.workers["chrome-a"]["credits"], after)

    def test_over_the_cap_is_413_never_402(self):
        job = self._claimed()
        with patch.object(ew, "MAX_RESULT_BYTES", 4):
            answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(),
                                      data={"mediaType": "video/mp4"}, files={"file": ("c", b"12345")})
        self.assertEqual(answer.status_code, 413)

    def test_door_refuses_before_the_body_without_a_worker_token(self):
        declared = {"Content-Length": str(api._EXTENSION_RESULT_BODY + 1), **self.auth()}
        too_big = self.client.post("/api/workers/jobs/x/result", headers=declared, content=b"")
        self.assertEqual(too_big.status_code, 413)
        refused = self.client.post("/api/workers/jobs/x/result", headers=self.auth("ekw_nope"),
                                   data={"mediaType": "video/mp4"}, files={"file": ("c", b"x")})
        self.assertEqual(refused.status_code, 401)

    def test_late_result_settles_the_held_job(self):
        held, _ = runtime.store.submit(modality="video", model_id=TEXT.model_id, params={"prompt": "x"},
                                       idempotency_key=None, provider_id="story-studio-video")
        job_id = held["id"]
        upstream = ew.upstream_id("chrome-a", "job_late")
        runtime.store.mark_running(job_id)
        runtime.store.record_upstream_acceptance(job_id, upstream)
        runtime.store.fail(job_id, code="UNKNOWN_OUTCOME", detail="held",
                           recovery_state=RECOVERY_UNKNOWN)
        answer = self.client.post("/api/workers/jobs/job_late/result", headers=self.auth(),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c", b"LATE")})
        self.assertEqual(answer.status_code, 422)   # not an MP4: refused, the hold stays
        self.assertEqual(runtime.store.get(job_id)["state"], "failed")
        answer = self.client.post("/api/workers/jobs/job_late/result", headers=self.auth(),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c", MP4)})
        self.assertEqual(answer.json(), {"ok": True, "adopted": True})
        self.assertEqual(runtime.store.get(job_id)["state"], "succeeded")
        other = ew.mint("chrome-b", "video")
        stolen = self.client.post("/api/workers/jobs/job_late/result", headers=self.auth(other),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c", b"X")})
        self.assertEqual(stolen.status_code, 404)


def _held_row(upstream: str) -> str:
    """A JobStore row held as an unknown outcome under `upstream`, the way the worker leaves it."""
    held, _ = runtime.store.submit(modality="video", model_id=TEXT.model_id, params={"prompt": "x"},
                                   idempotency_key=None, provider_id="story-studio-video")
    runtime.store.mark_running(held["id"])
    runtime.store.record_upstream_acceptance(held["id"], upstream)
    runtime.store.fail(held["id"], code="UNKNOWN_OUTCOME", detail="held", recovery_state=RECOVERY_UNKNOWN)
    return held["id"]


class LateFailTests(_Base):
    def fail(self, job, ambiguous):
        return self.client.post(f"/api/workers/jobs/{job}/fail", headers=self.auth(),
                                json={"error": "flow refused", "ambiguous": ambiguous})

    def test_late_ambiguous_fail_stays_held_and_definite_fail_settles(self):
        job_id = _held_row(ew.upstream_id("chrome-a", "job_f"))
        self.assertEqual(self.fail("job_f", True).json(), {"ok": True, "held": True})
        self.assertEqual(runtime.store.get(job_id)["recovery_state"], RECOVERY_UNKNOWN)
        self.assertEqual(self.fail("job_f", False).json(), {"ok": True, "settled": True})
        row = runtime.store.get(job_id)
        self.assertEqual((row["state"], row["error_code"]), ("failed", "PROVIDER_FAILED"))
        self.assertNotEqual(row["recovery_state"], RECOVERY_UNKNOWN)

    def test_late_fail_never_overturns_a_result_or_a_newer_attempt(self):
        won = _held_row(ew.upstream_id("chrome-a", "job_w"))
        self.client.post("/api/workers/jobs/job_w/result", headers=self.auth(),
                         data={"mediaType": "video/mp4"}, files={"file": ("c", MP4)})
        self.fail("job_w", False)
        self.assertEqual(runtime.store.get(won)["state"], "succeeded")
        moved = _held_row(ew.upstream_id("chrome-a", "job_old"))
        with runtime.store._lock:   # a newer attempt now owns the row
            runtime.store._conn.execute("UPDATE jobs SET upstream_job_id = ? WHERE id = ?",
                                        ("ext:chrome-b:job_new", moved))
            runtime.store._conn.commit()
        self.assertEqual(self.fail("job_old", False).status_code, 404)
        self.assertEqual(runtime.store.get(moved)["recovery_state"], RECOVERY_UNKNOWN)

    def test_a_fail_for_nobodys_job_is_404(self):
        self.assertEqual(self.fail("job_none", False).status_code, 404)


class ConcurrencyTests(_Base):
    def test_first_terminal_answer_wins_under_a_race(self):
        self.claim()
        job = ew.broker.offer("video", {"prompt": "x"})
        self.claim()
        worker = ew.worker_for(self.token)
        outcomes, gate = [], threading.Barrier(8)

        def answer(i):
            gate.wait()
            state = "succeeded" if i % 2 else "failed"
            outcomes.append((state, ew.broker.answer(worker, job.job_id, state=state, content=b"v",
                                                     media_type="video/mp4", ambiguous=False)))
        threads = [threading.Thread(target=answer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        live = [state for state, outcome in outcomes if outcome == "live"]
        self.assertEqual(len(live), 1)
        self.assertEqual(job.state, live[0])
        self.assertEqual(sorted(o for _, o in outcomes).count("taken"), 7)

    def test_hold_reports_whether_it_happened(self):
        self.claim()
        job = ew.broker.offer("video", {"prompt": "x"})
        self.claim()
        ew.broker.answer(ew.worker_for(self.token), job.job_id, state="succeeded", content=b"v",
                         media_type="video/mp4")
        self.assertFalse(ew.broker.hold(job))   # the answer landed first: nothing to hold
        self.assertEqual(job.state, "succeeded")

    def test_a_result_landing_at_lease_expiry_is_returned_not_held(self):
        self.claim()
        req, captured = self.request()
        real_hold = ew.broker.hold
        worker = ew.worker_for(self.token)

        def racing_hold(job):
            # The worker's result arrives between the adapter's lease check and its hold.
            ew.broker.answer(worker, job.job_id, state="succeeded", content=MP4, media_type="video/mp4")
            return real_hold(job)
        with patch.object(ew.broker, "hold", racing_hold):
            thread, box = self.run_adapter(req)
            self.wait_offered()
            job = self.claim().json()["job"]
            thread.join(8)
        self.assertNotIn("error", box)
        self.assertEqual(box["result"].content, MP4)
        self.assertIsNone(ew.broker.get(job["jobId"]))   # handed over, then forgotten
        self.assertEqual(captured, [ew.upstream_id("chrome-a", job["jobId"])])


class CreditValidationTests(_Base):
    def status(self):
        local = TestClient(api.app, client=("127.0.0.1", 40001))
        answer = local.get("/admin/api/extension-workers")
        self.assertEqual(answer.status_code, 200)
        self.assertNotRegex(answer.text, r"NaN|Infinity")
        return json.loads(answer.text)["workers"][0]["credits"]

    def test_unusable_credits_stay_unknown(self):
        good = '"source": "flow.nzlxg", "observedAt": "2026-11-09T10:00:00Z"'
        for credits in ('{"value": NaN, %s}' % good, '{"value": Infinity, %s}' % good,
                        '{"value": -1, %s}' % good, '{"value": true, %s}' % good,
                        '{"value": 5, "source": "<b>x</b>", "observedAt": "2026-11-09T10:00:00Z"}',
                        '{"value": 5, "source": "flow", "observedAt": "yesterday"}',
                        '{"value": 5, "source": "flow", "observedAt": "2026-11-09T10:00:00"}',
                        '{"value": 5, "observedAt": "2026-11-09T10:00:00Z"}'):
            body = '{"workerId": "chrome-a", "kind": "video", "telemetry": {"credits": %s}}' % credits
            answer = self.client.post("/api/workers/claim", content=body,
                                      headers={**self.auth(), "Content-Type": "application/json"})
            self.assertEqual(answer.status_code, 204, credits)
            self.assertIsNone(self.status(), credits)

    def test_credits_after_is_validated_the_same_way(self):
        self.claim()
        job = ew.broker.offer("video", {"prompt": "x"})
        self.claim()
        self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(),
                         data={"mediaType": "video/mp4",
                               "creditsAfter": '{"value": NaN, "source": "flow", '
                                               '"observedAt": "2026-11-09T10:00:00Z"}'},
                         files={"file": ("c", MP4)})
        self.assertIsNone(self.status())


class CapabilityTests(_Base):
    def test_every_flow_key_has_a_record_and_the_provider_is_off(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        definition = PROVIDERS_BY_ID["story-studio-video"]
        self.assertFalse(definition.default_enabled)
        self.assertEqual({m["id"] for m in definition.static_models}, {r.model_id for r in _FLOW})
        self.assertEqual(len(_FLOW), 4)

    def test_what_the_record_does_not_publish_is_refused_before_offering(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / "ref-b.png"
        path.write_bytes(b"PNG")
        self.claim()
        for record, params in ((TEXT, {"duration_seconds": TEXT.duration_min + 1}),
                               (TEXT, {"aspect_ratio": "9:16"}),
                               (TEXT, {"_reference_paths": [str(path)]}),
                               (IMAGE, {}),   # an image key with no picture would make one, paid
                               (IMAGE, {"_reference_paths": [str(path), str(path)]})):
            req, _ = self.request(record, **params)
            with self.assertRaises(BackendError) as caught:
                ChromeExtensionBackend().generate(req)
            self.assertEqual(caught.exception.code, "INVALID_REQUEST", params)
            self.assertIs(caught.exception.upstream_accepted, False)
        self.assertEqual(ew.broker.depth("video"), 0)


class DurableHoldTests(_Base):
    """The whole path: the real worker runs the job, the browser goes silent, the JobStore row is
    held (never replayed), and the late result is adopted into that same row."""

    def test_silent_worker_holds_the_row_and_a_late_result_adopts_it(self):
        from gateway.providers import registry
        from gateway.worker import Worker

        definition = registry.definition("story-studio-video")
        model = {"model_id": TEXT.model_id, "modality": "video", "raw": {}}
        for patcher in (patch.object(registry.models, "enabled_models",
                                     lambda modality: [(definition, model)] if modality == "video" else []),
                        patch.object(registry.credentials, "is_enabled", lambda pid: True),
                        patch.object(registry.credentials, "resolved", lambda pid: {})):
            patcher.start()
            self.addCleanup(patcher.stop)
        row, _ = runtime.store.submit(modality="video", model_id=TEXT.model_id,
                                      params={"prompt": "a cat", "duration_seconds": TEXT.duration_min},
                                      idempotency_key="durable-hold-1", provider_id="story-studio-video")
        self.claim()
        thread = threading.Thread(target=Worker(runtime.store, interval_s=10.0, pool_size=0)._run,
                                  args=(runtime.store.get(row["id"]),), daemon=True)
        thread.start()
        self.wait_offered()
        job = self.claim().json()["job"]
        thread.join(15)
        self.assertFalse(thread.is_alive())
        held = runtime.store.get(row["id"])
        self.assertEqual((held["state"], held["recovery_state"]), ("failed", RECOVERY_UNKNOWN), held)
        self.assertEqual(held["upstream_job_id"], ew.upstream_id("chrome-a", job["jobId"]))
        other = ew.mint("chrome-b", "video")
        self.assertEqual(self.claim(token=other, worker="chrome-b").status_code, 204)  # never re-offered
        answer = self.client.post(f"/api/workers/jobs/{job['jobId']}/result", headers=self.auth(),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c", MP4)})
        self.assertEqual(answer.json(), {"ok": True, "adopted": True})
        self.assertEqual(runtime.store.get(row["id"])["state"], "succeeded")
        self.assertIsNone(ew.broker.get(job["jobId"]))


if __name__ == "__main__":
    unittest.main()
