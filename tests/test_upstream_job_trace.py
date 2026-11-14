"""The admin trace read follows a stored Hub job and exposes only safe evidence."""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-upstream-trace-")

from gateway import admin  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB  # noqa: E402


class UpstreamTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.job = {
            "id": "gateway-job-1",
            "provider_id": "studio-hub-site-a",
            "upstream_job_id": "hub-batch-1",
            "modality": "music",
        }
        self.definition = SimpleNamespace(
            id=self.job["provider_id"],
            kind=KIND_STUDIO_HUB,
            base_url="http://definition.invalid",
        )

    def test_success_uses_stored_mapping_and_returns_only_allowlisted_trace(self) -> None:
        raw_trace = {
            "version": 1,
            "blocked_at": 10.0,
            "queued_items": [0],
            "candidates": [{
                "machine": "1603",
                "studio": "music@1603",
                "gate": "peer_not_dispatch_ready",
                "detail": "token=never-return-this",
            }],
            "selected": {
                "at": 11.0,
                "item_index": 0,
                "machine": "local",
                "studio": "music",
                "prompt": "never return this",
            },
            "truncated": False,
        }
        snapshot = {
            "known": True,
            "state": "done",
            "item": {"prompt": "private prompt"},
            "summary": {
                "dispatch_trace": raw_trace,
                "artifact_url": "http://worker.invalid/private",
                "prompt": "private prompt",
            },
        }
        values = {
            "hub_token": "top-secret-token",
            "base_url": "http://site-a.internal",
        }
        with patch.object(admin.store, "get", return_value=self.job), \
                patch.object(admin.registry, "definition", return_value=self.definition), \
                patch.object(admin.registry.credentials, "resolved", return_value=values), \
                patch.object(studio_hub, "batch_snapshot", return_value=snapshot) as called:
            answer = admin.upstream_job_trace(self.job["id"])

        called.assert_called_once_with(
            values,
            values["base_url"],
            self.job["upstream_job_id"],
            kind="music",
            local=False,
        )
        self.assertEqual(set(answer), {
            "job_id", "state", "selected_machine", "dispatch_trace",
            # 0.2.257: the same fields answer for a job with no Hub batch, and a cancelled
            # job's cause travels with its trace.
            "error_code", "error_detail", "failed_at", "origin", "candidates",
            "selection", "cancellation", "previous_cancellation",
        })
        self.assertEqual(answer["job_id"], self.job["id"])
        self.assertEqual(answer["state"], "done")
        self.assertEqual(answer["selected_machine"], "local")
        self.assertEqual(answer["dispatch_trace"]["candidates"], [{
            "machine": "1603",
            "studio": "music@1603",
            "gate": "peer_not_dispatch_ready",
        }])
        serialized = json.dumps(answer)
        for forbidden in (
            "top-secret-token", "site-a.internal", "worker.invalid", "private prompt"
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIsNone(answer["error_code"])
        self.assertEqual(answer["candidates"], [])
        self.assertIsNone(answer["cancellation"])

    def test_projection_rejects_oversized_numerics_and_enforces_final_byte_bound(self) -> None:
        huge = 10 ** 4000
        trace = admin._bounded_dispatch_trace({
            "version": 1,
            "blocked_at": huge,
            "queued_items": [huge] * 65,
            "candidates": [{
                "machine": "m" * 80,
                "studio": "s" * 80,
                "gate": "catalog_missing",
            }] * 65,
            "selected": {
                "at": float("inf"),
                "item_index": huge,
                "machine": "local",
                "studio": "music",
            },
            "truncated": False,
        })

        self.assertIsNotNone(trace)
        self.assertLessEqual(
            len(json.dumps(trace, separators=(",", ":")).encode()), 32 * 1024
        )
        self.assertEqual(trace["queued_items"], [])
        self.assertNotIn("blocked_at", trace)
        self.assertNotIn("at", trace["selected"])
        self.assertNotIn("item_index", trace["selected"])
        self.assertTrue(trace["truncated"])

    def test_an_unaccepted_job_still_answers_with_its_dispatch_evidence(self) -> None:
        """A job the Gateway never sent has an answer too (0.2.257): why not, and who stopped it."""
        row = {
            **self.job, "upstream_job_id": "", "state": "failed",
            "error_code": "DISPATCH_NO_ELIGIBLE_MACHINE",
            "error_detail": "No reachable Mac can run it — a: offline, last seen 7d",
            "finished_at": 123.0, "caller": "Story Studio",
            "result_json": json.dumps({"payload": {"dispatch": {
                "code": "DISPATCH_NO_ELIGIBLE_MACHINE",
                "checked_at": 120.0, "candidates": [
                    {"location_id": "a", "provider_id": "studio-hub-a", "label": "A",
                     "machine": None, "reason": "offline", "detail": "not answering",
                     "last_seen_at": 10.0}],
                "selected": None,
            }}}),
        }
        with patch.object(admin.store, "get", return_value=row), \
                patch.object(admin.registry, "definition", return_value=self.definition):
            answer = admin.upstream_job_trace("gateway-job-1")
        self.assertEqual(answer["state"], "failed")
        self.assertEqual(answer["error_code"], "DISPATCH_NO_ELIGIBLE_MACHINE")
        self.assertEqual(answer["failed_at"], 123.0)
        self.assertEqual(answer["candidates"][0]["reason"], "offline")
        self.assertIsNone(answer["selected_machine"])
        self.assertIsNone(answer["dispatch_trace"])
        self.assertIsNone(answer["cancellation"])

    def test_rejects_unknown_non_hub_jobs(self) -> None:
        cases = (
            (None, self.definition, 404),
            (self.job, None, 409),
            (self.job, SimpleNamespace(kind="openai_compat", base_url=""), 409),
        )
        for row, definition, status in cases:
            with self.subTest(row=row, definition=definition), \
                    patch.object(admin.store, "get", return_value=row), \
                    patch.object(admin.registry, "definition", return_value=definition):
                self.assertEqual(admin.upstream_job_trace("gateway-job-1").status_code, status)

    def test_distinguishes_unknown_hub_batch_from_hub_outage(self) -> None:
        for snapshot, status in (({"known": False}, 404), (None, 502)):
            with self.subTest(snapshot=snapshot), \
                    patch.object(admin.store, "get", return_value=self.job), \
                    patch.object(admin.registry, "definition", return_value=self.definition), \
                    patch.object(admin.registry.credentials, "resolved", return_value={}), \
                    patch.object(studio_hub, "batch_snapshot", return_value=snapshot):
                self.assertEqual(admin.upstream_job_trace(self.job["id"]).status_code, status)

    def test_route_is_admin_authenticated(self) -> None:
        route = next(
            route for route in admin.router.routes
            if getattr(route, "path", "") == "/admin/api/jobs/{job_id}/upstream"
        )
        self.assertIn(admin.require_admin, [dependency.call for dependency in route.dependant.dependencies])


class ReviewCandidateAdminTest(unittest.TestCase):
    """The owner's playback of a refused narration section, read through the protected locator."""

    PCM = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(64))
    SHA = hashlib.sha256(PCM).hexdigest()

    def setUp(self) -> None:
        receipt = {"schema": "studiohub.rejected-voice-review-candidate.v1", "active": False,
                   "disposition": "review_only", "worker_job_id": "c85e8c5d0a1b",
                   "client_request_id": "studiohub:hub-batch-1:0",
                   "section": {"index": 6, "total": 7, "attempt": 4, "text": "ខ្សាវៗ"},
                   "settings": {"voice_library_id": "voice-af496dcef199", "speed": 1.0},
                   "quality": {"rejection_code": "TTS_SECTION_SILENT",
                               "final": {"voiced_s": 2.54, "min_voiced_s": 4.06}},
                   "audio": {"media_type": "audio/wav", "bytes": len(self.PCM), "sha256": self.SHA},
                   "terminal": {"state": "error", "rejection_code": "TTS_SECTION_SILENT"}}
        receipt["receipt_sha256"] = hashlib.sha256(json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.receipt = receipt
        self.locator = {"gateway_job_id": "gateway-job-1", "provider_id": "studio-hub-site-a",
                        "batch_id": "hub-batch-1", "item_index": 0,
                        "receipt_sha256": receipt["receipt_sha256"], "pcm_bytes": len(self.PCM),
                        "pcm_sha256": self.SHA, "created_at": 1.0, "schema_version": 1}
        self.definition = SimpleNamespace(id="studio-hub-site-a", kind=KIND_STUDIO_HUB,
                                          base_url="http://definition.invalid")
        self.values = {"hub_token": "top-secret-token", "base_url": "http://site-a.internal"}

    def patched(self, *, locator: object = "default", definition: object = "default"):
        stack = ExitStack()
        stack.enter_context(patch.object(admin.store, "get", side_effect=AssertionError(
            "the live job row is not the authority")))
        stack.enter_context(patch.object(admin.store, "review_locator", return_value=(
            self.locator if locator == "default" else locator)))
        stack.enter_context(patch.object(admin.registry, "definition", return_value=(
            self.definition if definition == "default" else definition)))
        stack.enter_context(patch.object(admin.registry.credentials, "resolved", return_value=self.values))
        return stack

    def hub(self, body: bytes, headers: dict[str, str] | None = None, meta: dict | None = None):
        answer = {"batch_id": "hub-batch-1", "item_index": 0, "active": False,
                  "disposition": "review_only", "deliverable": False, "imported": False,
                  "receipt": meta or self.receipt}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("x-hub-token") != "top-secret-token":
                return httpx.Response(401)
            if request.url.path.endswith("/review-candidate"):
                return httpx.Response(200, json=answer)
            return httpx.Response(200, content=body, headers=headers or {
                "content-type": "audio/wav", "x-content-sha256": self.SHA})

        real = httpx.Client

        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(handler), **kwargs)

        return patch.object(studio_hub.httpx, "Client", side_effect=factory)

    def test_metadata_is_the_inactive_projection_and_leaks_no_location_or_credential(self) -> None:
        with self.patched(), self.hub(self.PCM):
            response = admin.job_review_candidate("gateway-job-1")
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual({key: body[key] for key in ("active", "disposition", "deliverable", "imported")},
                         {"active": False, "disposition": "review_only", "deliverable": False,
                          "imported": False})
        self.assertEqual((body["gateway_job_id"], body["hub_batch_id"], body["hub_item_index"],
                          body["worker_job_id"]),
                         ("gateway-job-1", "hub-batch-1", 0, "c85e8c5d0a1b"))
        self.assertEqual(body["section"]["text"], "ខ្សាវៗ")
        self.assertEqual(body["audio_url"], "/admin/api/jobs/gateway-job-1/review-candidate/audio")
        self.assertEqual(response.headers["cache-control"], "no-store, private, max-age=0")
        for private in ("top-secret-token", "site-a.internal", "definition.invalid", "handle"):
            self.assertNotIn(private, response.body.decode())

    def test_audio_is_returned_only_after_every_byte_matched(self) -> None:
        with self.patched(), self.hub(self.PCM):
            response = admin.job_review_candidate_audio("gateway-job-1")
        self.assertEqual((response.status_code, response.body), (200, self.PCM))
        self.assertEqual(response.media_type, "audio/wav")
        self.assertEqual(response.headers["x-content-sha256"], self.SHA)
        self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
        flipped = self.PCM[:-1] + bytes([self.PCM[-1] ^ 1])
        for name, body, headers in (
            ("one byte over", self.PCM + b"x", None),
            ("early end", self.PCM[:-1], None),
            ("same length, other bytes", flipped, None),
            ("other type", self.PCM, {"content-type": "audio/mpeg", "x-content-sha256": self.SHA}),
            ("other digest", self.PCM, {"content-type": "audio/wav", "x-content-sha256": "0" * 64}),
        ):
            with self.subTest(name), self.patched(), self.hub(body, headers):
                response = admin.job_review_candidate_audio("gateway-job-1")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.media_type, "application/json")
            self.assertNotIn(self.PCM[:12], response.body)

    def test_unknown_unbound_or_non_hub_jobs_never_reach_a_hub(self) -> None:
        for name, kwargs, status in (
            ("no locator", {"locator": None}, 404),
            ("provider removed", {"definition": None}, 409),
            ("no longer a hub", {"definition": SimpleNamespace(kind="openai_compat", base_url="")}, 409),
        ):
            for route in (admin.job_review_candidate, admin.job_review_candidate_audio):
                with self.subTest(name, route=route.__name__), self.patched(**kwargs), \
                        patch.object(studio_hub.httpx, "Client", side_effect=AssertionError("asked a Hub")):
                    self.assertEqual(route("gateway-job-1").status_code, status)

    def test_a_remote_location_without_its_credential_is_refused(self) -> None:
        self.values = {"base_url": "http://site-a.internal"}
        with self.patched(), patch.object(studio_hub.httpx, "Client", side_effect=AssertionError("asked")):
            self.assertEqual(admin.job_review_candidate("gateway-job-1").status_code, 503)
            self.assertEqual(admin.job_review_candidate_audio("gateway-job-1").status_code, 503)

    def test_both_routes_are_admin_authenticated_reads(self) -> None:
        for path in ("/admin/api/jobs/{job_id}/review-candidate",
                     "/admin/api/jobs/{job_id}/review-candidate/audio"):
            routes = [route for route in admin.router.routes if getattr(route, "path", "") == path]
            self.assertEqual(len(routes), 1, path)
            self.assertEqual(routes[0].methods, {"GET"})
            self.assertIn(admin.require_admin,
                          [dependency.call for dependency in routes[0].dependant.dependencies])


if __name__ == "__main__":
    unittest.main()
