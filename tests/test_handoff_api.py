"""Isolated contract checks for B04–B07/B09; no provider dispatch."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-api-contract-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "fixture-key",
})

import httpx
from fastapi.testclient import TestClient
from gateway import admin, api


class HandoffAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.headers = {"Authorization": "Bearer fixture-key"}

    def tearDown(self):
        self.client.close()
        api._UPLOADS.clear()

    def test_quote_identity_and_legacy_fallback(self):
        api._UPLOADS["q"] = (b"audio", "audio.wav", "audio/wav", time.time(), "model-a")
        with self.assertRaises(api.GatewayError) as caught:
            api._quote_model("q", "model-b")
        self.assertEqual(caught.exception.code, "TRANSCRIPTION_QUOTE_MODEL_MISMATCH")
        self.assertEqual(api._quote_model("q", None), "model-a")
        self.assertEqual(api._quote_model("q", "model-a"), "model-a")
        api._UPLOADS["q"] = (b"audio", "audio.wav", "audio/wav", time.time(), "")
        self.assertEqual(api._quote_model("q", "explicit-model"), "explicit-model")
        with patch.object(api.catalog, "rows", return_value=[{"id": "enabled-model"}]):
            self.assertEqual(api._quote_model("q", None), "enabled-model")

    def test_quote_has_real_expiry_and_old_quote_is_refused(self):
        from datetime import datetime
        before = time.time()
        response = self.client.post("/v1/audio/transcriptions/quotes", headers=self.headers, json={"model": "model-a"})
        self.assertEqual(response.status_code, 200)
        row = response.json()
        self.assertIsInstance(row["expires_at"], str)
        expiry = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).timestamp()
        self.assertAlmostEqual(expiry - before, api._UPLOAD_TTL_S, delta=2)
        api._UPLOADS[row["id"]] = (b"audio", "a.wav", "audio/wav", before - api._UPLOAD_TTL_S - 1, "model-a")
        response = self.client.post(f"/v1/audio/transcriptions/quotes/{row['id']}/confirm",
            headers={**self.headers, "Idempotency-Key": "expired-confirm"}, json={})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["code"], "TRANSCRIPTION_QUOTE_EXPIRED")

    def test_confirmed_quote_reattaches_after_upload_expired(self):
        job, _ = api.store.submit(modality="transcription", model_id="m", params={}, idempotency_key="already-confirmed")
        response = self.client.post("/v1/audio/transcriptions/quotes/gone/confirm",
            headers={**self.headers, "Idempotency-Key": "already-confirmed"}, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], job["id"])
        response = self.client.post("/v1/audio/transcriptions/quotes/gone/confirm",
            headers={**self.headers, "Idempotency-Key": "already-confirmed"}, json={"model": "different"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "TRANSCRIPTION_QUOTE_MODEL_MISMATCH")

    def test_confirm_replay_accepts_stored_location_and_published_identity(self):
        job, _ = api.store.submit(modality="transcription", provider_id="studio-hub-local",
            model_id="whisper-fixture", params={}, idempotency_key="location-confirm")
        for identity in ("studio-hub-local::whisper-fixture", "studio-hub::whisper-fixture"):
            response = self.client.post("/v1/audio/transcriptions/quotes/gone/confirm",
                headers={**self.headers, "Idempotency-Key": "location-confirm"}, json={"model": identity})
            self.assertEqual(response.status_code, 200, identity)
            self.assertEqual(response.json()["id"], job["id"])
        for identity in ("studio-hub-local::other", "studio-hub-other::whisper-fixture"):
            response = self.client.post("/v1/audio/transcriptions/quotes/gone/confirm",
                headers={**self.headers, "Idempotency-Key": "location-confirm"}, json={"model": identity})
            self.assertEqual(response.status_code, 409, identity)

    def test_actual_read_deletion_is_typed_gone_on_both_surfaces(self):
        path = base / "race.png"
        job = {"id": "race", "state": "succeeded", "asset_path": str(path), "media_type": "image/png"}
        real_read = Path.read_bytes
        def disappear(target):
            if target == path:
                target.unlink(missing_ok=True)
            return real_read(target)
        for surface in ("story", "admin"):
            path.write_bytes(b"fixture")
            with patch.object(Path, "read_bytes", disappear):
                if surface == "story":
                    with self.assertRaises(api.GatewayError) as caught:
                        api._artifact_response(job, "image/png")
                    self.assertEqual((caught.exception.status, caught.exception.code), (410, "ARTIFACT_GONE"))
                else:
                    with patch.object(admin.store, "get", return_value=job):
                        response = self.client.get("/admin/api/artifacts/race/outputs/0")
                    self.assertEqual(response.status_code, 410)
                    self.assertEqual(response.json()["code"], "ARTIFACT_GONE")

    def test_create_omits_model_when_key_is_absent(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        definition = PROVIDERS_BY_ID["openai-direct"]
        model = {"model_id": "mock-model", "display_name": "Fixture", "modality": "chat", "source": "test", "enabled": True, "raw": {}}
        with patch.object(admin.registry, "definitions", return_value=[definition]), \
             patch.object(admin.registry.models, "_every_definition", return_value=[(definition.id, definition)]), \
             patch.object(admin.registry.models, "models_for", return_value=[model]), \
             patch.object(admin.registry.models, "can_publish", return_value=True), \
             patch.object(admin.registry.credentials, "is_enabled", return_value=True), \
             patch.object(admin.registry.credentials, "is_configured", return_value=False):
            admin.catalog._built_at = 0
            self.assertEqual(admin.create_models()["by_modality"]["chat"], [])
            with patch.object(admin.registry.credentials, "is_configured", return_value=True):
                admin.catalog._built_at = 0
                rows = admin.create_models()["by_modality"]["chat"]
                self.assertEqual([row["id"] for row in rows], ["openai-direct::mock-model"])
                self.assertIn("context_window_tokens", rows[0])

    def test_admin_history_exposes_recovery_and_every_retained_output(self):
        import json
        from gateway import retention
        present = base / "recovered.png"
        present.write_bytes(b"fixture")
        missing = base / "missing.png"
        job = {"id": "recovery-fixture", "state": "failed", "modality": "image",
               "model_id": "fixture", "provider_id": "fixture", "created_at": 1,
               "updated_at": 2, "asset_path": str(present),
               "recovery_required": True, "recovery_state": "unknown_outcome",
               "recovery_detail": "Check upstream; do not replay.",
               "cancel_requested": True, "cancel_state": "requested",
               "artifacts_json": json.dumps([
                   {"asset_id": "0", "path": str(present), "media_type": "image/png"},
                   {"asset_id": "1", "path": str(missing), "media_type": "image/png"}])}
        with patch.object(admin.store, "list_jobs", return_value=[job]), \
             patch.object(admin.store, "get", return_value=job), \
             patch.object(retention, "kept", return_value=[{"path": str(present)}]):
            row = self.client.get("/admin/api/jobs").json()["jobs"][0]
            self.assertTrue(row["recovery_required"])
            self.assertTrue(row["cancel_requested"])
            self.assertEqual([r["present"] for r in row["outputs"]], [True, False])
            self.assertEqual([r["kept"] for r in row["outputs"]], [True, False])
            self.assertEqual(self.client.get(row["outputs"][0]["url"]).content, b"fixture")
            detail = self.client.get("/admin/api/create/status/recovery-fixture").json()
            self.assertEqual(len(detail["outputs"]), 2)

    def test_external_peer_and_forwarding_cannot_get_loopback_bypass(self):
        for peer, headers in (("192.0.2.2", {}), ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}), ("127.0.0.1", {"Forwarded": "for=127.0.0.1"})):
            with TestClient(api.app, client=(peer, 3333)) as client:
                self.assertIn(client.get("/admin/api/status", headers=headers).status_code, (401, 403))
                self.assertEqual(client.get("/admin/api/status", headers={**headers, **self.headers}).status_code, 200)

    def test_keyless_external_story_request_is_not_development_access(self):
        client = TestClient(api.app, client=("192.0.2.2", 3333))
        try:
            with patch.dict(os.environ, {"GATEWAY_API_KEY": ""}):
                response = client.get("/v1/models", headers={"Authorization": "Bearer arbitrary"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "API_KEY_NOT_CONFIGURED")
        finally:
            client.close()

    def test_content_compatibility_routes_never_invent_ownership(self):
        for method, route in (("GET", "account"), ("GET", "receipts"), ("GET", "products"),
                              ("POST", "quotes"), ("POST", "purchases"), ("POST", "downloads")):
            response = self.client.request(method, "/v1/content/" + route, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["code"], "CONTENT_STORE_NOT_CONNECTED")
            self.assertNotEqual(response.json().get("state"), "completed")

    def test_real_socket_bind_mismatch(self):
        # An ephemeral private-interface server with only this disposable profile.
        candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            candidate.connect(("192.0.2.1", 9))  # route lookup, sends no datagram
            host = candidate.getsockname()[0]
        finally:
            candidate.close()
        if host.startswith("127."):
            self.skipTest("No non-loopback interface available")
        listener = socket.socket()
        listener.bind((host, 0))
        port = listener.getsockname()[1]
        listener.close()
        with tempfile.TemporaryDirectory(prefix="gateway-bind-contract-") as isolated:
            env = dict(os.environ)
            for key in ("GATEWAY_APP_SUPPORT", "GATEWAY_DATA", "GATEWAY_ARTIFACTS", "GATEWAY_DB", "GATEWAY_CREDENTIALS", "GATEWAY_MODEL_CACHE", "GATEWAY_ENV_FILE", "GATEWAY_FLEET", "GATEWAY_VOICES", "GATEWAY_VOICES_OUT", "STUDIOHUB_READY_FILE"):
                env[key] = str(Path(isolated) / key)
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.api:app", "--host", host, "--port", str(port), "--no-proxy-headers"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(base_url=f"http://{host}:{port}", trust_env=False, timeout=2) as client:
                    for _ in range(80):
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            time.sleep(.05)
                    else:
                        self.fail("Isolated Uvicorn did not start")
                    self.assertEqual(client.get("/admin/api/status").status_code, 401)
                    self.assertEqual(client.get("/admin/api/status", headers={"Authorization": "Bearer wrong"}).status_code, 403)
                    self.assertEqual(client.get("/admin/api/status", headers=self.headers).status_code, 200)
                    self.assertEqual(client.get("/admin/api/status", headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 401)
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        api.store.close()
        TEMP.cleanup()
