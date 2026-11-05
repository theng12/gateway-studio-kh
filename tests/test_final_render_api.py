#!/usr/bin/env python3
"""Normal-origin route contract for private final renders, with no network or renderer."""
from __future__ import annotations

import os
import hashlib
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN_ROOT = ROOT / "_trash" / "tests" / f"gateway-final-render-api-{uuid.uuid4().hex}"
RUN_ROOT.mkdir(parents=True, mode=0o700)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(RUN_ROOT),
    "GATEWAY_DATA": str(RUN_ROOT / "data"),
    "GATEWAY_ARTIFACTS": str(RUN_ROOT / "artifacts"),
    "GATEWAY_DB": str(RUN_ROOT / "gateway.sqlite3"),
    "GATEWAY_CREDENTIALS": str(RUN_ROOT / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(RUN_ROOT / "models.json"),
    "GATEWAY_ENV_FILE": str(RUN_ROOT / ".env"),
    "GATEWAY_FLEET": str(RUN_ROOT / "fleet.json"),
    "GATEWAY_POOLS": str(RUN_ROOT / "pools.json"),
    "GATEWAY_VOICES": str(RUN_ROOT / "voices.json"),
    "GATEWAY_VOICES_OUT": str(RUN_ROOT / "voices.json"),
    "GATEWAY_VOICE_LIBRARY": str(RUN_ROOT / "voice-library"),
    "GATEWAY_BACKUP_HUB_DIR": str(RUN_ROOT / "backup-hub"),
    "STUDIOHUB_READY_FILE": str(RUN_ROOT / "no-hub.json"),
    "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1",
    "GATEWAY_API_KEY": "ordinary-fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402
from gateway import api  # noqa: E402
from gateway.render_service import AuthenticatedPrivateRender  # noqa: E402


STORY = "44444444-4444-4444-8444-444444444444"
PRINCIPAL = AuthenticatedPrivateRender(
    owner_scope="22222222-2222-4222-8222-222222222222",
    gateway_instance_id="33333333-3333-4333-8333-333333333333",
    story_instance_id=STORY,
)


class FakeOutputLease:
    offset = 0
    length = 1
    total = 1
    sha256 = "a" * 64
    status = 206

    def __enter__(self):
        return self

    def chunks(self):
        yield b"x"

    def __exit__(self, *_args):
        return None


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def capabilities(self, principal):
        self._record("capabilities", principal)
        descriptor = '{"format":"studio.render.profile.v1","combination_set":"fake"}'
        return {
            "format": "studio.render.gateway-capabilities.v1",
            "private": True, "chargeable": False, "eligible": 0, "idle": 0,
            "owner_scope": principal.owner_scope,
            "gateway_instance_id": principal.gateway_instance_id,
            "story_instance_id": principal.story_instance_id,
            "server_time": 1_700_000_000,
            "profile_descriptor_utf8": descriptor,
            "profile_digest": hashlib.sha256(descriptor.encode()).hexdigest(),
            "tools": {"descriptor_utf8": '{"format":"studio.render.tools.v1"}',
                      "digest": "a" * 64},
            "compiler": {"descriptor_utf8": '{"format":"studio.render.compiler.v1"}',
                         "digest": "b" * 64},
        }

    def accept(self, principal, key, raw_body):
        self._record("accept", principal, key, raw_body)
        return {"id": "55555555-5555-4555-8555-555555555555", "state": "staging",
                "idempotent_replay": False}

    async def put_blob(self, principal, sha256, size_bytes, offset, length, body):
        payload = b"".join([piece async for piece in body])
        self._record("put_blob", principal, sha256, size_bytes, offset, length, payload)
        return {"format": "studio.render.gateway-blob.v1", "sha256": sha256,
                "size_bytes": size_bytes, "state": "uploading", "offset": offset + length}

    def by_key(self, principal, key):
        self._record("by_key", principal, key)
        return {"id": "55555555-5555-4555-8555-555555555555", "state": "staging"}

    def request_cancel_by_key(self, principal, key, expected_request_sha256):
        self._record("request_cancel_by_key", principal, key, expected_request_sha256)
        return {"id": "55555555-5555-4555-8555-555555555555", "cancel_state": "requested"}

    def open_output(self, principal, gateway_job_id, range_header, if_match):
        self._record("open_output", principal, gateway_job_id, range_header, if_match)
        if if_match != '"' + "a" * 64 + '"':
            raise api.RenderServiceError("OUTPUT_PRECONDITION_FAILED", 412)
        return FakeOutputLease()


class RouteShapeTests(unittest.TestCase):
    def test_every_private_route_uses_only_the_private_dependency_and_key_lookup_precedes_id(self) -> None:
        expected = (
            ("GET", "/v1/final-renders/capabilities"),
            ("GET", "/v1/final-renders/blobs/{sha256}/status"),
            ("PUT", "/v1/final-renders/blobs/{sha256}"),
            ("POST", "/v1/final-renders"),
            ("GET", "/v1/final-renders/by-idempotency-key/{key}"),
            ("POST", "/v1/final-renders/by-idempotency-key/{key}/cancel"),
            ("GET", "/v1/final-renders/{gateway_job_id}"),
            ("POST", "/v1/final-renders/{gateway_job_id}/cancel"),
            ("GET", "/v1/final-renders/{gateway_job_id}/output"),
            ("POST", "/v1/final-renders/{gateway_job_id}/ack"),
        )
        indexed = [(next(iter(route.methods)), route.path, route) for route in api.app.routes
                   if getattr(route, "path", "").startswith("/v1/final-renders")]
        self.assertEqual([(method, path) for method, path, _ in indexed], list(expected))
        for _, _, route in indexed:
            calls = [dependency.call for dependency in route.dependant.dependencies]
            self.assertIn(api.require_private_render, calls)
            self.assertNotIn(api.require_auth, calls)
            self.assertNotIn(api.require_admin if hasattr(api, "require_admin") else object(), calls)
        paths = [path for _, path, _ in indexed]
        self.assertLess(paths.index("/v1/final-renders/by-idempotency-key/{key}"),
                        paths.index("/v1/final-renders/{gateway_job_id}"))
        self.assertLess(paths.index("/v1/final-renders/by-idempotency-key/{key}/cancel"),
                        paths.index("/v1/final-renders/{gateway_job_id}"))


class RequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.service_patch = patch.object(api, "render_service", self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        api.app.dependency_overrides[api.require_private_render] = lambda: PRINCIPAL
        self.addCleanup(api.app.dependency_overrides.clear)
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)

    def test_late_label_contract_query_is_scoped_exact_and_not_silently_ignored(self) -> None:
        contract = "studio.render.late-label.v3"
        with patch.object(self.service, "capabilities", return_value={}) as capabilities:
            response = self.client.get("/v1/final-renders/capabilities",
                                       params={"requested_contract": contract})
            self.assertEqual(response.status_code, 200)
            capabilities.assert_called_once_with(PRINCIPAL, requested_contract=contract)
        for query in ("requested_contract=", "requested_contract=unknown",
                      f"requested_contract={contract}&requested_contract={contract}",
                      f"requested_contract={contract}&requested_contract=unknown"):
            with self.subTest(query=query), patch.object(self.service, "capabilities") as capabilities:
                response = self.client.get("/v1/final-renders/capabilities?" + query)
                self.assertEqual(response.status_code, 422)
                capabilities.assert_not_called()

    def test_subtitle_query_is_exact_and_cannot_be_combined_as_two_queries(self) -> None:
        contract = "studio.render.subtitle.v1"
        with patch.object(self.service, "capabilities", return_value={}) as capabilities:
            response = self.client.get("/v1/final-renders/capabilities",
                                       params={"requested_contract": contract})
            self.assertEqual(response.status_code, 200)
            capabilities.assert_called_once_with(PRINCIPAL, requested_contract=contract)
        for other in (contract, "studio.render.late-label.v3"):
            with patch.object(self.service, "capabilities") as capabilities:
                response = self.client.get("/v1/final-renders/capabilities", params=[
                    ("requested_contract", contract), ("requested_contract", other)])
                self.assertEqual(response.status_code, 422)
                capabilities.assert_not_called()

    def test_output_settings_negotiation_is_closed_unavailable_without_selection_or_dispatch(self) -> None:
        response = self.client.get("/v2/final-renders/capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {
            "format", "private", "chargeable", "owner_scope", "gateway_instance_id",
            "story_instance_id", "server_time", "supported", "reason",
            "profile_descriptor_utf8", "profile_digest",
        })
        self.assertEqual(body["format"], "studio.render.output-settings-capabilities.v1")
        self.assertIs(body["private"], True)
        self.assertIs(body["chargeable"], False)
        self.assertIs(body["supported"], False)
        self.assertTrue(body["reason"].strip())
        self.assertIn("comparison", body["reason"])
        self.assertIsNone(body["profile_descriptor_utf8"])
        self.assertIsNone(body["profile_digest"])
        self.assertIs(type(body["server_time"]), int)
        self.assertGreater(body["server_time"], 0)
        self.assertEqual((body["owner_scope"], body["gateway_instance_id"], body["story_instance_id"]),
                         (PRINCIPAL.owner_scope, PRINCIPAL.gateway_instance_id, STORY))
        self.assertEqual(self.service.calls, [])
        route = next(route for route in api.app.routes
                     if getattr(route, "path", "") == "/v2/final-renders/capabilities")
        self.assertEqual([dependency.call for dependency in route.dependant.dependencies],
                         [api.require_private_render])

    def test_unavailable_best_never_falls_back_to_explicit_submit(self) -> None:
        for path in ("/v2/final-renders", "/v2/final-renders/selections"):
            response = self.client.post(path, json={"format": "studio.render.gateway-request.v2"})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.calls, [])

    def test_submit_forwards_exact_raw_bytes_and_header_key_to_the_service(self) -> None:
        raw = b'{"format":"studio.render.gateway-request.v1","story_uuid":"fake"}'
        response = self.client.post(
            "/v1/final-renders",
            content=raw,
            headers={"Authorization": "Bearer " + "ab" * 32,
                     "Idempotency-Key": "gateway-attempt-001",
                     "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.service.calls, [
            ("accept", (PRINCIPAL, "gateway-attempt-001", raw), {})
        ])

    def test_a_refused_create_names_its_cause_in_the_log_without_the_key(self) -> None:
        # WI0005/7 (2026-10-25): Story files any refusal as an unknown outcome, so the log must say why.
        import contextlib, io
        from gateway.render_service import RenderServiceError

        def refuse(principal, key, raw_body):
            try:
                raise KeyError("packet.recipe")
            except KeyError as cause:
                raise RenderServiceError("RENDER_REQUEST_INVALID", 422) from cause
        self.service.accept = refuse
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            response = self.client.post(
                "/v1/final-renders", content=b"{}",
                headers={"Idempotency-Key": "gateway-attempt-secret-tail", "Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 422)
        line = out.getvalue()
        self.assertIn("[final-render] create refused: RenderServiceError code=RENDER_REQUEST_INVALID status=422", line)
        self.assertIn("cause=KeyError", line)
        self.assertIn("key=gateway-", line)
        self.assertNotIn("secret-tail", line)

    def test_submit_requires_idempotency_key_before_calling_service(self) -> None:
        response = self.client.post(
            "/v1/final-renders", content=b"{}", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_KEY_REQUIRED")
        self.assertEqual(self.service.calls, [])

    def test_blob_put_requires_query_identity_and_matching_content_range(self) -> None:
        digest = "c" * 64
        response = self.client.put(
            f"/v1/final-renders/blobs/{digest}?size_bytes=9&offset=3",
            content=b"abc",
            headers={"Content-Length": "3", "Content-Range": "bytes 3-5/9"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls, [
            ("put_blob", (PRINCIPAL, digest, 9, 3, 3, b"abc"), {})
        ])

        self.service.calls.clear()
        refused = self.client.put(
            f"/v1/final-renders/blobs/{digest}?size_bytes=9&offset=3",
            content=b"abc",
            headers={"Content-Length": "3", "Content-Range": "bytes 0-2/9"},
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["error"]["code"], "BLOB_RANGE_INVALID")
        self.assertEqual(self.service.calls, [])

    def test_capabilities_are_private_nonchargeable_and_expose_no_transport_identity(self) -> None:
        response = self.client.get("/v1/final-renders/capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["private"], body["chargeable"], body["eligible"], body["idle"]),
                         (True, False, 0, 0))
        self.assertEqual(hashlib.sha256(body["profile_descriptor_utf8"].encode()).hexdigest(),
                         body["profile_digest"])
        self.assertEqual(set(body["tools"]), {"descriptor_utf8", "digest"})
        self.assertEqual(set(body["compiler"]), {"descriptor_utf8", "digest"})
        serialized = response.text.lower()
        for forbidden in ("token", "address", "url", "path", "node_id", "proof"):
            self.assertNotIn(forbidden, serialized)

    def test_key_lookup_is_not_captured_as_a_gateway_job_id(self) -> None:
        response = self.client.get("/v1/final-renders/by-idempotency-key/gateway-attempt-001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[-1],
                         ("by_key", (PRINCIPAL, "gateway-attempt-001"), {}))

    def test_key_cancel_is_not_captured_as_a_gateway_job_id(self) -> None:
        response = self.client.post(
            "/v1/final-renders/by-idempotency-key/gateway-attempt-001/cancel",
            json={"expected_request_sha256": "a" * 64},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.service.calls[-1],
            ("request_cancel_by_key", (PRINCIPAL, "gateway-attempt-001", "a" * 64), {}),
        )

    def test_key_cancel_requires_the_exact_request_identity_even_for_an_existing_key(self) -> None:
        for body in (None, {}, {"expected_request_sha256": "not-a-digest"}):
            with self.subTest(body=body):
                response = self.client.post(
                    "/v1/final-renders/by-idempotency-key/gateway-attempt-001/cancel",
                    json=body,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "CANCEL_REQUEST_INVALID")

    def test_output_requires_and_forwards_exact_strong_if_match_with_range(self) -> None:
        etag = '"' + "a" * 64 + '"'
        response = self.client.get(
            "/v1/final-renders/55555555-5555-4555-8555-555555555555/output",
            headers={"Range": "bytes=0-0", "If-Match": etag},
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["etag"], etag)
        self.assertEqual(self.service.calls[-1], (
            "open_output",
            (PRINCIPAL, "55555555-5555-4555-8555-555555555555", "bytes=0-0", etag),
            {},
        ))

        for bad in (None, "a" * 64, 'W/"' + "a" * 64 + '"', '"bad"'):
            with self.subTest(if_match=bad):
                headers = {"Range": "bytes=0-0"}
                if bad is not None:
                    headers["If-Match"] = bad
                refused = self.client.get(
                    "/v1/final-renders/55555555-5555-4555-8555-555555555555/output",
                    headers=headers,
                )
                self.assertEqual(refused.status_code, 412)
                self.assertEqual(refused.json()["error"]["code"],
                                 "OUTPUT_PRECONDITION_FAILED")


class AuthenticationBoundaryTests(unittest.TestCase):
    """The regular Gateway API key, strict and without the loopback placeholder exemption."""

    def setUp(self) -> None:
        self.service = FakeService()
        service_patch = patch.object(api, "render_service", self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        # Loopback on purpose: the private routes must never inherit `require_auth`'s exemption.
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)

    def get(self, headers: dict[str, str]):
        return self.client.get("/v1/final-renders/capabilities", headers=headers)

    def test_unconfigured_key_is_typed_unavailable_even_on_loopback_and_health_still_works(self) -> None:
        with patch.object(api, "gateway_api_key", return_value=""):
            for headers in ({}, {"Authorization": "Bearer anything", "X-Story-Instance": STORY}):
                with self.subTest(headers=bool(headers)):
                    private = self.get(headers)
                    self.assertEqual(private.status_code, 503)
                    self.assertEqual(private.json()["error"]["code"],
                                     "PRIVATE_RENDER_UNAVAILABLE")
            self.assertNotEqual(self.client.get("/health").status_code, 503)
        self.assertEqual(self.service.calls, [])

    def test_missing_key_or_missing_or_invalid_story_header_is_401(self) -> None:
        good = "Bearer ordinary-fixture-key"
        for headers in (
            {},
            {"X-Story-Instance": STORY},
            {"Authorization": "Bearer ", "X-Story-Instance": STORY},
            {"Authorization": "Basic ordinary-fixture-key", "X-Story-Instance": STORY},
            {"Authorization": good},
            {"Authorization": good, "X-Story-Instance": ""},
            {"Authorization": good, "X-Story-Instance": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
            {"Authorization": good, "X-Story-Instance": "not-a-uuid"},
            {"Authorization": good, "X-Story-Instance": "{" + STORY + "}"},
            {"Authorization": good, "X-Story-Instance": "00000000-0000-0000-0000-000000000000"},
        ):
            with self.subTest(headers=headers):
                response = self.get(headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED")
        self.assertEqual(self.service.calls, [])

    def test_wrong_key_is_403_and_compared_in_constant_time(self) -> None:
        with patch.object(api.hmac, "compare_digest", wraps=api.hmac.compare_digest) as compared:
            for token in ("wrong", "ordinary-fixture-ke", "ordinary-fixture-key2", "ab" * 32):
                with self.subTest(token=token):
                    response = self.get({"Authorization": "Bearer " + token,
                                         "X-Story-Instance": STORY})
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["code"], "FORBIDDEN")
        self.assertEqual(compared.call_count, 4)
        self.assertEqual(self.service.calls, [])

    def test_output_settings_negotiation_uses_the_same_strict_owner_story_boundary(self) -> None:
        path = "/v2/final-renders/capabilities"
        valid = {"Authorization": "Bearer ordinary-fixture-key", "X-Story-Instance": STORY}
        for headers, status in (({}, 401),
                                ({"Authorization": valid["Authorization"]}, 401),
                                ({**valid, "X-Story-Instance": "not-a-uuid"}, 401),
                                ({**valid, "Authorization": "Bearer wrong"}, 403)):
            with self.subTest(status=status, headers=list(headers)):
                self.assertEqual(self.client.get(path, headers=headers).status_code, status)
        with patch.object(api, "gateway_api_key", return_value=""):
            self.assertEqual(self.client.get(path, headers=valid).status_code, 503)
        response = self.client.get(path, headers=valid)
        self.assertEqual(response.status_code, 200)
        installation = api.store.render_installation()
        body = response.json()
        self.assertEqual((body["owner_scope"], body["gateway_instance_id"], body["story_instance_id"]),
                         (installation["owner_scope"], installation["gateway_instance_id"], STORY))
        self.assertIs(body["supported"], False)
        self.assertEqual(self.service.calls, [])

    def test_regular_key_and_story_header_bind_the_installation_identity(self) -> None:
        response = self.get({"Authorization": "Bearer ordinary-fixture-key",
                             "X-Story-Instance": STORY})
        self.assertEqual(response.status_code, 200)
        installation = api.store.render_installation()
        expected = AuthenticatedPrivateRender(
            owner_scope=installation["owner_scope"],
            gateway_instance_id=installation["gateway_instance_id"],
            story_instance_id=STORY,
        )
        self.assertEqual(self.service.calls, [("capabilities", (expected,), {})])
        body = response.json()
        self.assertEqual(
            (body["owner_scope"], body["gateway_instance_id"], body["story_instance_id"]),
            (installation["owner_scope"], installation["gateway_instance_id"], STORY),
        )
        # Created once and never regenerated: a second request sees the same installation.
        self.get({"Authorization": "Bearer ordinary-fixture-key", "X-Story-Instance": STORY})
        self.assertEqual(self.service.calls[-1], ("capabilities", (expected,), {}))


if __name__ == "__main__":
    unittest.main()
