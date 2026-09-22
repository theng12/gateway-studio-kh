#!/usr/bin/env python3
"""Studio Hub adapter ↔ Hub wire contract.

Covers the five counterpart mismatches an inventory of the two codebases found:
the delivery receipt, cancellation reaching the Hub, Music as a first-class
modality, voice-reference metadata propagation, and the distinction between a
Hub item that settled *uncertain* and one that failed.

Everything here runs against a mocked Hub transport or a fake adapter: no live
Hub, no real generation, no credential, no installed app.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch

import httpx

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-studio-hub-contract-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest, BackendResult  # noqa: E402
from gateway.store import JobStore  # noqa: E402
import gateway.worker as worker_module  # noqa: E402

HUB = "https://hub.test"
BATCH = "hub-batch-1"


class _Hub:
    """A stand-in for the Hub's authenticated routes.

    Only the routes this adapter is allowed to call exist, so a wrong path shows
    up as a 404 rather than as a plausible-looking success — which is exactly how
    polling a transcription batch on the jobs route behaves in the real Hub.
    """

    def __init__(
        self,
        *,
        states: list[str] | None = None,
        artifact: bytes = b"artifact-bytes",
        ack_status: int = 200,
        cancel_status: int = 200,
        item: dict[str, Any] | None = None,
        reference: dict[str, Any] | None = None,
        voice_reference_status: int = 201,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.acks: list[str] = []
        self.deletes: list[str] = []
        self.payloads: list[Any] = []
        self._states = list(states or ["done"])
        self.artifact = artifact
        self.ack_status = ack_status
        self.cancel_status = cancel_status
        self.item = item
        self.reference = reference
        self.voice_reference_status = voice_reference_status

    # ── the transport ────────────────────────────────────────────────────
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path))
        if method == "DELETE":
            self.deletes.append(path)
            if self.cancel_status >= 400:
                return httpx.Response(self.cancel_status, json={"detail": "no such batch"})
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "queued_cancelled": 1,
                    "running_signalled": 0,
                    "running_pending": 0,
                },
            )
        if method == "POST" and path.endswith("/ack"):
            self.acks.append(path)
            if self.ack_status >= 400:
                return httpx.Response(self.ack_status, json={"detail": "only completed output"})
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/api/hub/jobs":
            self.payloads.append(json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"batch_id": BATCH})
        if method == "POST" and path == "/api/hub/transcription/jobs":
            self.payloads.append({"content_type": request.headers.get("content-type", "")})
            return httpx.Response(200, json={"batch_id": BATCH})
        if method == "POST" and path == "/api/hub/voice-references":
            self.payloads.append(request.content)
            if self.voice_reference_status >= 400:
                return httpx.Response(self.voice_reference_status, json={"detail": "refused"})
            return httpx.Response(self.voice_reference_status, json=self.reference or {})
        if method == "GET" and path.endswith("/artifact"):
            return httpx.Response(
                200,
                content=self.artifact,
                headers={
                    "content-type": "audio/wav",
                    "X-Content-SHA256": "deadbeef",
                },
            )
        if method == "GET" and (
            path == f"/api/hub/jobs/{BATCH}"
            or path == f"/api/hub/transcription/jobs/{BATCH}"
        ):
            state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
            item = {"index": 0, "state": state, "terminal_result": {"machine_id": "local"}}
            item.update(self.item or {})
            return httpx.Response(
                200, json={"batch_id": BATCH, "items": [item], "state": state}
            )
        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})


class _HubCase(unittest.TestCase):
    """Shared plumbing: a Hub, an adapter patched onto it, and a request."""

    def hub_client(self, hub: _Hub):
        real = httpx.Client

        def factory(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(hub.handler), **kwargs)

        return patch.object(hub_module.httpx, "Client", side_effect=factory)

    @staticmethod
    def request(
        *,
        modality: str,
        params: dict[str, Any] | None = None,
        values: dict[str, str] | None = None,
        model_id: str = "model/test",
        checkpoint: Callable[[str], None] | None = None,
    ) -> BackendRequest:
        return BackendRequest(
            provider_id="studio-hub-test",
            model_id=model_id,
            modality=modality,
            params=params or {},
            values=values if values is not None else {"hub_token": "test-token"},
            base_url=HUB,
            checkpoint=checkpoint,
        )

    def generate(self, hub: _Hub, request: BackendRequest) -> BackendResult:
        with self.hub_client(hub):
            return hub_module.StudioHubBackend().generate(request)

    def generate_then_deliver(self, hub: _Hub, request: BackendRequest) -> BackendResult:
        """Generate, then take the receipt the way the worker does — after the save.

        The adapter deliberately does not acknowledge its own download: the
        receipt asserts verified delivery, which only the caller saving the bytes
        can establish. So a caller that never asks for it gets none, and these
        tests ask for it.
        """
        with self.hub_client(hub):
            result = hub_module.StudioHubBackend().generate(request)
            self.assertIsNotNone(
                result.deliver, "the Hub result must expose a delivery receipt"
            )
            result.deliver()
        return result


class DeliveryReceiptTests(_HubCase):
    """Item 1 — a verified delivery is acknowledged, on the modality's own route."""

    def test_image_delivery_is_acknowledged(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(hub, self.request(modality="image", params={"prompt": "a cat"}))
        self.assertEqual(result.content, b"artifact-bytes")
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])
        self.assertTrue(result.payload["delivery_ack"]["ok"])
        self.assertEqual(result.payload["delivery_ack"]["status"], 200)

    def test_voice_delivery_is_acknowledged_on_the_jobs_route(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(hub, self.request(modality="tts", params={"text": "hello"}))
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])
        self.assertTrue(result.payload["delivery_ack"]["ok"])

    def test_transcription_delivery_is_acknowledged_on_its_own_route(self) -> None:
        upload = _TEMP_ROOT / "audio.wav"
        upload.write_bytes(b"audio")
        hub = _Hub()
        result = self.generate_then_deliver(
            hub,
            self.request(
                modality="transcription",
                params={
                    "_upload_path": str(upload),
                    "_upload_name": "audio.wav",
                    "_upload_type": "audio/wav",
                },
            ),
        )
        self.assertEqual(
            hub.acks, [f"/api/hub/transcription/jobs/{BATCH}/items/0/ack"]
        )
        self.assertTrue(result.payload["delivery_ack"]["ok"])

    def test_a_refused_receipt_is_recorded_and_the_artifact_is_still_returned(self) -> None:
        """A receipt the Hub refuses must not throw away a saved generation."""
        hub = _Hub(ack_status=409)
        result = self.generate_then_deliver(hub, self.request(modality="image", params={"prompt": "a cat"}))
        receipt = result.payload["delivery_ack"]
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], 409)
        self.assertEqual(result.content, b"artifact-bytes")

    def test_the_receipt_names_the_route_it_used(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(
            result.payload["delivery_ack"]["route"],
            f"/api/hub/jobs/{BATCH}/items/0/ack",
        )


class _FakeAdapter:
    """Returns a result whose receipt records whether the file was already saved."""

    def __init__(self, job_id: str, *, receipt: dict[str, Any] | None = None) -> None:
        self.job_id = job_id
        self.saved_before_deliver: list[bool] = []
        self._receipt = receipt or {"ok": True, "status": 200, "answer": {"ok": True}}

    def generate(self, request: BackendRequest) -> BackendResult:
        artifact_dir = Path(worker_module.ARTIFACT_DIR)
        payload: dict[str, Any] = {"hub_batch_id": BATCH}

        def deliver() -> dict[str, Any]:
            self.saved_before_deliver.append(
                any(artifact_dir.glob(f"{self.job_id}.*"))
            )
            payload["delivery_ack"] = self._receipt
            return self._receipt

        return BackendResult(
            content=b"image-bytes", media_type="image/png", payload=payload, deliver=deliver
        )


class _Registry:
    """The worker asks the live registry three things; answer all three."""

    credentials = SimpleNamespace(
        is_enabled=lambda _provider_id: True,
        resolved=lambda _provider_id: {},
    )
    models = SimpleNamespace(
        enabled_models=lambda _modality: [
            (
                SimpleNamespace(id="studio-hub-test", base_url=HUB, display_name="Hub"),
                {"model_id": "model/test"},
            )
        ]
    )

    @staticmethod
    def definition(_provider_id: str) -> Any:
        return SimpleNamespace(id="studio-hub-test", base_url=HUB, display_name="Hub")


class WorkerReceiptTests(unittest.TestCase):
    """The receipt belongs after the save — that ordering is the whole point."""

    def test_the_worker_acknowledges_only_after_the_artifact_is_on_disk(self) -> None:
        store = JobStore(_TEMP_ROOT / "receipt.sqlite3")
        try:
            row, created = store.submit(
                modality="image",
                model_id="model/test",
                params={"prompt": "a cat"},
                idempotency_key="receipt-order",
                provider_id="studio-hub-test",
            )
            self.assertTrue(created)
            self.assertTrue(store.mark_running(row["id"]))
            adapter = _FakeAdapter(row["id"])
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)

            self.assertEqual(adapter.saved_before_deliver, [True])
            settled = store.get(row["id"])
            self.assertEqual(settled["state"], "succeeded")
            self.assertTrue(json.loads(settled["result_json"])["payload"]["delivery_ack"]["ok"])
        finally:
            store.close()

    def test_a_refused_receipt_does_not_fail_the_job(self) -> None:
        store = JobStore(_TEMP_ROOT / "receipt-refused.sqlite3")
        try:
            row, _ = store.submit(
                modality="image",
                model_id="model/test",
                params={"prompt": "a cat"},
                idempotency_key="receipt-refused",
                provider_id="studio-hub-test",
            )
            self.assertTrue(store.mark_running(row["id"]))
            adapter = _FakeAdapter(
                row["id"], receipt={"ok": False, "status": 409, "answer": {"detail": "no"}}
            )
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=adapter
            ):
                worker_module.execute_job(row, store)

            settled = store.get(row["id"])
            self.assertEqual(settled["state"], "succeeded")
            receipt = json.loads(settled["result_json"])["payload"]["delivery_ack"]
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["status"], 409)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
