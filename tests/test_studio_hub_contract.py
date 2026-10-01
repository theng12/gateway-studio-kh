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

import base64

import atexit
import hashlib
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
        "GATEWAY_HOST": "127.0.0.1",
        "GATEWAY_API_KEY": "fixture-key",
        "GATEWAY_FLEET": str(_TEMP_ROOT / "fleet.json"),
        "GATEWAY_VOICES": str(_TEMP_ROOT / "voices.json"),
        "GATEWAY_VOICES_OUT": str(_TEMP_ROOT / "voices.json"),
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
        discard_status: int = 200,
        submit_refusal: dict[str, Any] | None = None,
        connect_error: bool = False,
        read_timeout: bool = False,
        server_error: bool = False,
        artifact_media_type: str = "audio/wav",
        artifact_sha256: str = "",
        artifact_failures: int = 0,
        poll_faults: list[Any] | None = None,
        duplicate: bool = False,
        queued_item: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.acks: list[str] = []
        self.deletes: list[str] = []
        self.discards: list[str] = []
        self.payloads: list[Any] = []
        self._states = list(states or ["done"])
        self.artifact = artifact
        self.ack_status = ack_status
        self.cancel_status = cancel_status
        self.item = item
        self.reference = reference
        self.voice_reference_status = voice_reference_status
        self.discard_status = discard_status
        self.submit_refusal = submit_refusal
        self.connect_error = connect_error
        self.read_timeout = read_timeout
        self.server_error = server_error
        # The artifact route states its media type, and a video lane's is not WAV. A fixture that
        # always answered `audio/wav` could not tell "the adapter read the type" from "the adapter
        # fell back to the audio default".
        self.artifact_media_type = artifact_media_type
        self.artifact_sha256 = artifact_sha256
        #: How many artifact GETs answer 503 before one succeeds.
        self.artifact_failures = artifact_failures
        #: Answers handed out to the first polls: an int is that status, a str a 200 non-JSON body.
        self.poll_faults = list(poll_faults or [])
        self.duplicate = duplicate
        #: Fields the item carries while it is `queued` (a deferral's `error` and `retry_at`).
        self.queued_item = queued_item or {}

    # ── the transport ────────────────────────────────────────────────────
    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.connect_error:
            # A refused connection, as a switched-off controller gives.
            raise httpx.ConnectError("connection refused")
        if self.read_timeout:
            # The request left; the answer never came. Genuinely ambiguous.
            raise httpx.ReadTimeout("timed out")
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
        if method == "POST" and path.endswith("/discard"):
            self.discards.append(path)
            if self.discard_status >= 400:
                return httpx.Response(
                    self.discard_status,
                    json={"detail": "Discard is available only for an uncertain job"},
                )
            return httpx.Response(
                200, json={"ok": True, "batch_id": BATCH, "item_index": 0}
            )
        if method == "POST" and path.endswith("/ack"):
            self.acks.append(path)
            if self.ack_status >= 400:
                return httpx.Response(self.ack_status, json={"detail": "only completed output"})
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/api/hub/jobs":
            self.payloads.append(json.loads(request.content or b"{}"))
            if self.server_error:
                return httpx.Response(500, json={"detail": "the Hub failed"})
            if self.submit_refusal is not None:
                return httpx.Response(409, json=self.submit_refusal)
            return httpx.Response(200, json={"batch_id": BATCH})
        if method == "POST" and path == "/api/hub/transcription/jobs":
            self.payloads.append({"content_type": request.headers.get("content-type", ""),
                                  "body": request.content})
            if self.submit_refusal is not None:
                return httpx.Response(409, json=self.submit_refusal)
            return httpx.Response(200, json={"batch_id": BATCH, "duplicate": self.duplicate})
        if method == "POST" and path == "/api/hub/voice-references":
            self.payloads.append(request.content)
            if self.voice_reference_status >= 400:
                return httpx.Response(self.voice_reference_status, json={"detail": "refused"})
            return httpx.Response(self.voice_reference_status, json=self.reference or {})
        if method == "GET" and path.endswith("/artifact"):
            if self.artifact_failures > 0:
                self.artifact_failures -= 1
                return httpx.Response(503, json={"detail": "restarting"})
            return httpx.Response(
                200,
                content=self.artifact,
                headers={
                    "content-type": self.artifact_media_type,
                    "X-Content-SHA256": self.artifact_sha256
                    or hashlib.sha256(self.artifact).hexdigest(),
                },
            )
        if method == "GET" and (
            path == f"/api/hub/jobs/{BATCH}"
            or path == f"/api/hub/transcription/jobs/{BATCH}"
        ):
            if self.poll_faults:
                fault = self.poll_faults.pop(0)
                if isinstance(fault, int):
                    return httpx.Response(fault, json={"detail": "fault"})
                return httpx.Response(200, content=fault.encode())
            state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
            item = {"index": 0, "state": state, "terminal_result": {"machine_id": "local"}}
            item.update(self.item or {})
            if state == "queued":
                item.update(self.queued_item)
            return httpx.Response(
                200, json={"batch_id": BATCH, "items": [item]}
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
        upstream_job_id: str = "",
    ) -> BackendRequest:
        return BackendRequest(
            provider_id="studio-hub-test",
            model_id=model_id,
            modality=modality,
            params=params or {},
            values=values if values is not None else {"hub_token": "test-token"},
            base_url=HUB,
            checkpoint=checkpoint,
            upstream_job_id=upstream_job_id,
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


class GenerationTimingTests(_HubCase):
    """The Hub's own runtime reaches the job record — on every lane, not only chat.

    Chat has always recorded `generation_seconds` (from `elapsed_seconds`). The image,
    voice, music and video lanes did not, because this adapter read the sibling fields of
    `terminal_result` and skipped `runtime_s` — so the generation record held a rate for
    chat and `None` for the lanes that do the real work, and no analytics built on it
    could answer "how long does this model take" for an image at all.
    """

    def test_an_image_job_records_the_hubs_runtime(self) -> None:
        hub = _Hub(item={"terminal_result": {"machine_id": "node-a", "runtime_s": 64.9}})
        result = self.generate_then_deliver(
            hub, self.request(modality="image", params={"prompt": "a cat"})
        )
        self.assertEqual(result.payload["generation_seconds"], 64.9)
        # The sibling field it travels with, so a reader can tell which Mac ran it.
        self.assertEqual(result.payload["hub_machine"], "node-a")

    def test_an_unstated_runtime_stays_absent_rather_than_zero(self) -> None:
        # **The distinction the whole page depends on.** A Hub that states no runtime is not
        # a Hub that reported an instant job: `0.0` would enter every average and drag the
        # model's real rate down, and it would render as a real measurement.
        hub = _Hub(item={"terminal_result": {"machine_id": "local"}})
        result = self.generate_then_deliver(
            hub, self.request(modality="image", params={"prompt": "a cat"})
        )
        self.assertIsNone(result.payload["generation_seconds"])

    def test_a_voice_job_records_it_too(self) -> None:
        # Voice is the lane the timing gap hid longest: it shares this code path with
        # image, and a voice rate is what decides which Mac a clone is pushed to.
        hub = _Hub(item={"terminal_result": {"machine_id": "node-b", "runtime_s": 12.25}})
        result = self.generate_then_deliver(
            hub, self.request(modality="tts", params={"text": "hello"})
        )
        self.assertEqual(result.payload["generation_seconds"], 12.25)


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


class UpstreamCancelTests(_HubCase):
    """Item 2 — the cancel request reaches the Hub and its answer is kept."""

    def test_cancel_asks_the_hub_to_stop_the_batch(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().cancel(
                self.request(modality="image", upstream_job_id=BATCH)
            )
        self.assertTrue(answer["ok"])
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertEqual(answer["answer"]["queued_cancelled"], 1)
        self.assertEqual(answer["answer"]["running_signalled"], 0)

    def test_transcription_cancel_uses_the_transcription_route(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().cancel(
                self.request(modality="transcription", upstream_job_id=BATCH)
            )
        self.assertEqual(hub.deletes, [f"/api/hub/transcription/jobs/{BATCH}"])
        self.assertTrue(answer["ok"])

    def test_a_job_that_never_dispatched_asks_the_hub_nothing(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().cancel(self.request(modality="image"))
        self.assertEqual(hub.deletes, [])
        self.assertFalse(answer["ok"])
        self.assertIn("nothing was dispatched", answer["reason"])

    def test_a_hub_that_refuses_the_cancel_did_not_stop_anything(self) -> None:
        hub = _Hub(cancel_status=404)
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().cancel(
                self.request(modality="image", upstream_job_id=BATCH)
            )
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["status"], 404)
        self.assertEqual(answer["answer"]["detail"], "no such batch")


class SubmitConnectFailureTests(_HubCase):
    """A submit that never opened a connection delivered nothing.

    Measured live: a controller that was switched off turned every submit into a
    durable `unknown_outcome` recovery hold — a reconciliation problem the caller
    never created. A refused connection cannot have delivered the request, so it is
    a site that is down; a read timeout after the request left is genuinely
    ambiguous and keeps the careful path.
    """

    def test_a_refused_connection_is_a_site_that_is_down(self) -> None:
        hub = _Hub(connect_error=True)
        with self.assertRaises(hub_module.BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        exc = caught.exception
        self.assertEqual(exc.code, "LOCATION_UNREACHABLE")
        self.assertTrue(exc.retryable)
        self.assertIs(exc.upstream_accepted, False)

    def test_the_same_applies_to_the_transcription_route(self) -> None:
        hub = _Hub(connect_error=True)
        with self.assertRaises(hub_module.BackendError) as caught:
            self.generate(hub, self.request(
                modality="transcription",
                # Any existing file will do: the connection is refused before a
                # byte of it matters.
                params={"_upload_path": __file__, "_upload_name": "a.mp3"},
            ))
        self.assertEqual(caught.exception.code, "LOCATION_UNREACHABLE")
        self.assertIs(caught.exception.upstream_accepted, False)

    def test_a_silent_read_stays_ambiguous(self) -> None:
        hub = _Hub(read_timeout=True)
        with self.assertRaises(hub_module.BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        exc = caught.exception
        self.assertEqual(exc.code, "SITE_CAPACITY_UNAVAILABLE")
        self.assertIsNone(exc.upstream_accepted, "a read timeout may have been accepted")


class CancelRemotePersistenceTests(unittest.TestCase):
    def test_the_hub_answer_round_trips_on_the_job(self) -> None:
        store = JobStore(_TEMP_ROOT / "cancel-remote.sqlite3")
        try:
            row, _ = store.submit(
                modality="image",
                model_id="model/test",
                params={},
                idempotency_key="cancel-remote",
                provider_id="studio-hub-test",
            )
            store.record_cancel_remote(
                row["id"], {"ok": True, "status": 200, "answer": {"queued_cancelled": 2}}
            )
            got = store.get(row["id"])
            self.assertEqual(got["cancel_remote"]["answer"]["queued_cancelled"], 2)
        finally:
            store.close()


class CancelEndpointTests(_HubCase):
    """The public cancel route asks the owner of the work and stores the answer."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        self.api = api
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.headers = {"Authorization": "Bearer fixture-key"}

    def tearDown(self) -> None:
        self.client.close()

    def _dispatched_job(self, modality: str) -> str:
        row, created = self.api.store.submit(
            modality=modality,
            model_id="model/test",
            params={"prompt": "x"},
            idempotency_key=f"cancel-{modality}-{self.id()}",
            provider_id="studio-hub-test",
        )
        self.assertTrue(created)
        self.assertTrue(self.api.store.mark_running(row["id"]))
        self.api.store.record_upstream_acceptance(row["id"], BATCH)
        return row["id"]

    def _cancel(self, path: str, hub: _Hub) -> dict[str, Any]:
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(
            self.api.registry, "definition", return_value=definition
        ), patch.object(
            self.api.registry.credentials, "resolved", return_value={"hub_token": "t"}
        ), self.hub_client(hub):
            response = self.client.post(path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_image_cancel_records_what_the_hub_answered(self) -> None:
        hub = _Hub()
        job_id = self._dispatched_job("image")
        body = self._cancel(f"/v1/images/{job_id}/cancel", hub)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertTrue(body["cancel_remote"]["ok"])
        self.assertTrue(body["cancel_requested"])
        # The local vocabulary still says "requested": the Hub cancelling what it
        # had queued is not the same claim as the gateway stopping work itself.
        self.assertEqual(body["cancel_state"], "requested")

    def test_a_refused_cancel_is_never_reported_as_a_remote_stop(self) -> None:
        hub = _Hub(cancel_status=404)
        job_id = self._dispatched_job("image")
        body = self._cancel(f"/v1/images/{job_id}/cancel", hub)
        self.assertFalse(body["cancel_remote"]["ok"])
        self.assertEqual(body["cancel_remote"]["status"], 404)
        self.assertEqual(body["cancel_state"], "requested")

    def test_a_music_cancel_reaches_the_hub(self) -> None:
        hub = _Hub()
        job_id = self._dispatched_job("music")
        body = self._cancel(f"/v1/music/jobs/{job_id}/cancel", hub)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertTrue(body["cancel_remote"]["ok"])


class MusicModalityTests(_HubCase):
    """Item 3 — Music is dispatched, discovered and offered like any other lane."""

    def test_music_submits_its_own_modality_with_its_engine_parameters(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(
            hub,
            self.request(
                modality="music",
                model_id="ACE-Step/Ace-Step1.5",
                params={
                    "prompt": "a warm lofi loop",
                    "duration_seconds": 120,
                    "steps": 30,
                    "guidance_scale": 7.5,
                    "shift": 1.0,
                    "seed": 4,
                    "instrumental": True,
                    "lyrics": "la la la",
                    "vocal_language": "en",
                },
            ),
        )
        body = hub.payloads[0]
        self.assertEqual(body["modality"], "music")
        self.assertEqual(body["model"], "ACE-Step/Ace-Step1.5")
        item = body["items"][0]
        self.assertEqual(item["prompt"], "a warm lofi loop")
        self.assertEqual(
            item["params"],
            {
                "duration_seconds": 120,
                "steps": 30,
                "guidance_scale": 7.5,
                "shift": 1.0,
                "seed": 4,
                "instrumental": True,
                "lyrics": "la la la",
                "vocal_language": "en",
            },
        )
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])
        self.assertEqual(result.media_type, "audio/wav")

    def test_the_instrumental_only_engine_takes_the_shared_parameters(self) -> None:
        """Stable Audio 3 is instrumental only — its item carries no lyrics."""
        hub = _Hub()
        self.generate_then_deliver(
            hub,
            self.request(
                modality="music",
                model_id="stabilityai/stable-audio-3-optimized",
                params={
                    "prompt": "brushed drums",
                    "duration_seconds": 200,
                    "steps": 20,
                    "guidance_scale": 6.0,
                    "seed": 11,
                },
            ),
        )
        item = hub.payloads[0]["items"][0]
        self.assertEqual(
            item["params"],
            {"duration_seconds": 200, "steps": 20, "guidance_scale": 6.0, "seed": 11},
        )

    def test_music_never_receives_image_geometry(self) -> None:
        hub = _Hub()
        self.generate_then_deliver(
            hub,
            self.request(
                modality="music",
                params={"prompt": "x", "width": 1024, "height": 1024, "aspect_ratio": "1:1"},
            ),
        )
        self.assertEqual(hub.payloads[0]["items"][0]["params"], {})

    def test_image_never_receives_music_parameters(self) -> None:
        hub = _Hub()
        self.generate_then_deliver(
            hub,
            self.request(
                modality="image",
                params={"prompt": "x", "lyrics": "la", "duration_seconds": 90, "seed": 3},
            ),
        )
        self.assertEqual(hub.payloads[0]["items"][0]["params"], {"seed": 3})

    def test_a_location_can_run_music(self) -> None:
        from gateway.adapters import _MODALITIES_BY_KIND, serves
        from gateway.providers import registry

        self.assertIn("music", _MODALITIES_BY_KIND["studio_hub"])
        self.assertTrue(serves(registry.definition("studio-hub-local"), "music"))


class HubDiscoveryTests(unittest.TestCase):
    """The engine table is what makes a Hub model offerable at all."""

    def test_the_music_engine_is_discovered_as_music(self) -> None:
        from gateway.providers import discovery

        packages = {
            "models": [
                {
                    "model_id": "ACE-Step/Ace-Step1.5",
                    "package_id": "ace-step-1-5-turbo",
                    "engine": "music-mlx",
                    "files_state": "files_verified",
                    "type": "model",
                },
                {
                    "model_id": "stabilityai/stable-audio-3-optimized",
                    "package_id": "stable-audio-3-medium",
                    "engine": "music-mlx",
                    "files_state": "files_verified",
                    "type": "model",
                },
                {
                    "model_id": "Qwen/Qwen3-TTS-12Hz-0.6B-Base-4bit",
                    "package_id": "qwen3-tts",
                    "engine": "voice-mlx",
                    "files_state": "files_verified",
                    "type": "model",
                },
                {
                    "model_id": "some/unknown-engine",
                    "package_id": "mystery",
                    "engine": "mystery-mlx",
                    "files_state": "files_verified",
                    "type": "model",
                },
            ]
        }
        catalog = {"models": [{"repo": "ACE-Step/Ace-Step1.5", "capabilities": ["music"]}]}

        def fake_get(url: str, **_kwargs: Any) -> httpx.Response:
            if url.endswith("/api/hub/model-packages"):
                return httpx.Response(200, json=packages)
            return httpx.Response(200, json=catalog)

        definition = SimpleNamespace(
            id="studio-hub-local", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(discovery.httpx, "get", side_effect=fake_get):
            result = discovery._studio_hub(definition, {})

        by_id = {model.model_id: model for model in result.models}
        self.assertEqual(by_id["ACE-Step/Ace-Step1.5"].modality, "music")
        self.assertEqual(by_id["stabilityai/stable-audio-3-optimized"].modality, "music")
        self.assertEqual(
            by_id["Qwen/Qwen3-TTS-12Hz-0.6B-Base-4bit"].modality, "tts"
        )
        # An engine nobody mapped stays undiscovered rather than becoming a
        # model some lane would then refuse to run.
        self.assertNotIn("some/unknown-engine", by_id)


class VoiceReferenceMetadataTests(_HubCase):
    """Item 4 — the reference carries its metadata, and the answer is checked."""

    def _clip(self) -> Path:
        clip = _TEMP_ROOT / "reference.wav"
        clip.write_bytes(b"reference-clip-bytes")
        return clip

    def _reference(self, clip: Path) -> dict[str, Any]:
        return {
            "path": str(clip),
            "transcript": "hello there",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
            "language": "en",
            "source_id": "voice-abc123",
        }

    def _staged_payload(self, clip: Path, **overrides: Any) -> dict[str, Any]:
        audio = clip.read_bytes()
        payload: dict[str, Any] = {
            "asset_id": "staged-asset-1",
            "sha256": hashlib.sha256(audio).hexdigest(),
            "bytes": len(audio),
            "media_type": "audio/wav",
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
            "transcript_supplied": True,
            "origin": "genstudio",
        }
        payload.update(overrides)
        return payload

    def _stage(self, hub: _Hub, clip: Path, reference: dict[str, Any] | None = None) -> str:
        client = httpx.Client(transport=httpx.MockTransport(hub.handler))
        try:
            with self.hub_client(hub):
                return hub_module._stage_voice_reference(
                    client,
                    HUB,
                    {"X-Hub-Token": "test-token"},
                    "voice-abc123",
                    clip,
                    reference if reference is not None else self._reference(clip),
                )
        finally:
            client.close()

    def test_the_reference_carries_language_and_source_identity(self) -> None:
        clip = self._clip()
        voice = SimpleNamespace(
            id="voice-abc123",
            display_name="Narrator",
            language="en",
            raw={
                "reference_path": str(clip),
                "reference_transcript": "hello there",
                "reference_segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
            },
        )
        from gateway import voices as voices_module

        with patch.object(voices_module, "resolve", return_value=voice):
            reference = hub_module._reference_for(
                self.request(modality="tts", params={"voice_id": "voice-abc123"})
            )
        self.assertIsNotNone(reference)
        self.assertEqual(reference["language"], "en")
        self.assertEqual(reference["source_id"], "voice-abc123")
        self.assertEqual(reference["segments"][0]["text"], "hello there")

    def test_every_accepted_field_is_sent(self) -> None:
        clip = self._clip()
        hub = _Hub(reference=self._staged_payload(clip))
        asset_id = self._stage(hub, clip)
        self.assertEqual(asset_id, "staged-asset-1")
        body = hub.payloads[0]
        self.assertIsInstance(body, bytes)
        digest = hashlib.sha256(clip.read_bytes()).hexdigest()
        for field, value in (
            ("language", b"en"),
            ("sha256", digest.encode()),
            ("source_asset_id", b"voice-abc123"),
            ("ttl_seconds", str(int(hub_module._REFERENCE_TTL_S)).encode()),
            ("transcript_segments", b"hello there"),
        ):
            self.assertIn(f'name="{field}"'.encode(), body, f"{field} was not sent")
            self.assertIn(value, body, f"{field} did not carry its value")

    def test_a_reference_that_came_back_different_is_refused(self) -> None:
        """A wrong digest means the staged clip is not the voice's recording."""
        clip = self._clip()
        for overrides in (
            {"sha256": "0" * 64},
            {"bytes": 1},
            {"media_type": "text/plain"},
            {"expires_at": time.time() - 10},
            {"origin": "local"},
            {"transcript_supplied": False},
        ):
            with self.subTest(overrides=overrides):
                hub = _Hub(reference=self._staged_payload(clip, **overrides))
                with self.assertRaises(BackendError) as raised:
                    self._stage(hub, clip)
                self.assertEqual(raised.exception.code, "VOICE_PROVIDER_RESULT_INVALID")

    def test_a_correct_answer_that_states_less_is_not_a_mismatch(self) -> None:
        """A Hub that omits a field is unverified there, never 'wrong'."""
        clip = self._clip()
        payload = self._staged_payload(clip)
        for field in ("bytes", "media_type", "expires_at", "transcript_supplied", "origin"):
            payload.pop(field, None)
        hub = _Hub(reference=payload)
        self.assertEqual(self._stage(hub, clip), "staged-asset-1")

    def test_the_staged_asset_is_the_reference_the_job_uses(self) -> None:
        """The placement still reaches the job as `voice_reference_asset_id`."""
        clip = self._clip()
        hub = _Hub(reference=self._staged_payload(clip))
        payload = self._staged_payload(clip)
        hub.reference = payload
        params = {"text": "speak", "voice_id": "voice-abc123"}
        voice = SimpleNamespace(
            id="voice-abc123",
            display_name="Narrator",
            language="en",
            raw={
                "reference_path": str(clip),
                "reference_transcript": "hello there",
                "reference_segments": [{"start": 0.0, "end": 1.0, "text": "hello there"}],
            },
        )
        from gateway import voices as voices_module

        with patch.object(voices_module, "resolve", return_value=voice), self.hub_client(hub):
            # The controller does not already hold the clip, so it is staged.
            hub_module._REFERENCE_CACHE.clear()
            result = hub_module.StudioHubBackend().generate(
                self.request(modality="tts", params=params)
            )
        submitted = next(
            body for body in hub.payloads if isinstance(body, dict) and body.get("modality")
        )
        self.assertEqual(submitted["voice_reference_asset_id"], "staged-asset-1")
        self.assertNotIn("voice_library_id", submitted)
        self.assertIsNotNone(result.content)


class UncertainIsARecoveryHoldTests(_HubCase):
    """Item 5 — an unknown worker outcome is a hold, never an ordinary failure."""

    def test_the_adapter_reports_uncertain_as_an_unknown_outcome(self) -> None:
        hub = _Hub(
            states=["uncertain"],
            item={
                "state": "uncertain",
                "error": "The Voice worker did not confirm cancellation",
                "error_code": "VOICE_CANCEL_OUTCOME_UNCERTAIN",
                "worker_id": "voice-worker-2",
                "machine": "local",
                "recovery": {"phase": "uncertain", "reason": "must be reconciled"},
            },
        )
        with self.assertRaises(BackendError) as raised:
            self.generate(hub, self.request(modality="tts", params={"text": "x"}))
        exc = raised.exception
        self.assertEqual(exc.code, "UNKNOWN_OUTCOME")
        self.assertTrue(exc.upstream_accepted, "an unknown outcome must not look retryable")
        self.assertFalse(exc.retryable)
        self.assertEqual(exc.recovery["hub_batch_id"], BATCH)
        self.assertEqual(exc.recovery["hub_item_index"], 0)
        self.assertEqual(exc.recovery["hub_worker"], "voice-worker-2")
        self.assertEqual(exc.recovery["hub_machine"], "local")
        self.assertEqual(exc.recovery["hub_error_code"], "VOICE_CANCEL_OUTCOME_UNCERTAIN")
        self.assertEqual(exc.recovery["hub_phase"], "uncertain")
        self.assertIn("/reconcile", exc.recovery["hub_reconcile_route"])
        self.assertIn("/discard", exc.recovery["hub_discard_route"])

    def test_an_ordinary_hub_failure_is_still_an_ordinary_failure(self) -> None:
        hub = _Hub(
            states=["error"],
            item={"state": "error", "error": {"message": "worker refused the prompt"}},
        )
        with self.assertRaises(BackendError) as raised:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(raised.exception.code, "PROVIDER_FAILED")
        self.assertIsNone(raised.exception.recovery)
        self.assertFalse(raised.exception.upstream_accepted)

    def test_a_cancelled_item_is_still_a_failure_not_a_hold(self) -> None:
        hub = _Hub(states=["cancelled"], item={"state": "cancelled"})
        with self.assertRaises(BackendError) as raised:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(raised.exception.code, "PROVIDER_FAILED")
        self.assertIsNone(raised.exception.recovery)

    def test_the_worker_settles_uncertain_as_a_recovery_hold(self) -> None:
        hub = _Hub(
            states=["uncertain"],
            item={
                "state": "uncertain",
                "error": "the accepted Music canonical request settings are missing",
                "error_code": "MUSIC_OUTCOME_UNCERTAIN",
                "worker_id": "music-worker-1",
                "machine": "local",
            },
        )
        store = JobStore(_TEMP_ROOT / "uncertain-hold.sqlite3")
        try:
            row, created = store.submit(
                modality="music",
                model_id="model/test",
                params={"prompt": "a warm loop"},
                idempotency_key="uncertain-hold",
                provider_id="studio-hub-test",
            )
            self.assertTrue(created)
            self.assertTrue(store.mark_running(row["id"]))
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
            ), self.hub_client(hub):
                worker_module.execute_job(row, store)

            held = store.get(row["id"])
            self.assertEqual(held["state"], "failed")
            self.assertEqual(held["error_code"], "UNKNOWN_OUTCOME")
            self.assertEqual(held["recovery_state"], "unknown_outcome")
            self.assertTrue(held["recovery_required"])
            self.assertFalse(held["retryable"], "a recovery hold is never auto-retried")
            # The operator gets the identifiers, not just a sentence.
            self.assertIn("music-worker-1", held["recovery_detail"])
            self.assertIn("MUSIC_OUTCOME_UNCERTAIN", held["recovery_detail"])
            self.assertIn(BATCH, held["recovery_detail"])
            # And structured, so an operator action can name the exact attempt.
            self.assertEqual(held["recovery_context"]["hub_batch_id"], BATCH)
            self.assertEqual(held["recovery_context"]["hub_item_index"], 0)
            self.assertEqual(
                held["recovery_context"]["hub_discard_route"],
                f"/api/hub/jobs/{BATCH}/items/0/discard",
            )
            # And nothing resubmits it.
            self.assertEqual(store.due(time.time() + 60), [])
        finally:
            store.close()


class HubVersionDriftTests(unittest.TestCase):
    """The Hub this build targets is **declared**, and the declaration never gates.

    Studio Hub auto-updates itself — this fleet has run 0.6.18, 0.6.23, 0.6.29,
    0.6.31, 0.6.54 and 0.6.60 in a week — and gateway-kh moves on its own too. So
    the pairing is stated rather than assumed: the relation is published per
    controller and shown on the Fleet page, and **nothing refuses work on a
    mismatch**, because a Hub updating itself must never take a fleet down with no
    action by its owner (the owner's decision, 2026-09-24).
    """

    def test_a_version_compares_against_the_declared_one(self) -> None:
        from gateway.adapters.studio_hub import (
            HUB_VERSION_MATCH,
            HUB_VERSION_NEWER,
            HUB_VERSION_OLDER,
            SUPPORTED_STUDIO_HUB_VERSION,
            hub_version_relation,
        )

        # **Derived from the declaration, never written in.** These assertions used
        # to name 0.6.62 and 0.6.64 by hand, and they went stale the moment the
        # declared version moved to 0.6.65 — `hub_version_relation("0.6.64")` then
        # answered `older`, correctly, and the test called it a failure. A test is
        # a row: one that quotes a version instead of reading it lies the same way
        # a tracker row does, and the fix is the same one — read the source.
        parts = [int(part) for part in SUPPORTED_STUDIO_HUB_VERSION.split(".")[:3]]
        major, minor, patch = (parts + [0, 0, 0])[:3]
        older = f"{major}.{minor}.{patch - 1}" if patch > 0 else f"{major}.{minor - 1}.99"
        newer = f"{major}.{minor}.{patch + 1}"

        self.assertEqual(hub_version_relation(SUPPORTED_STUDIO_HUB_VERSION), HUB_VERSION_MATCH)
        self.assertEqual(hub_version_relation(older), HUB_VERSION_OLDER)
        self.assertEqual(hub_version_relation(newer), HUB_VERSION_NEWER)
        self.assertEqual(hub_version_relation(f"{major}.{minor + 1}.0"), HUB_VERSION_NEWER)
        # A shorter version is padded, not unreadable: 0.6 is older than 0.6.60.
        self.assertEqual(hub_version_relation(f"{major}.{minor}"), HUB_VERSION_OLDER)
        # Pre-release and build suffixes do not change the ordering.
        self.assertEqual(hub_version_relation(f"{newer}-rc1"), HUB_VERSION_NEWER)

    def test_an_absent_or_unreadable_version_is_not_a_match(self) -> None:
        # "I could not read the version" must never render as "it matched" — the
        # same rule every other field on this gateway follows.
        from gateway.adapters.studio_hub import hub_version_relation

        for value in (None, "", "   ", "unknown", "v0.6.60", "not-a-version"):
            with self.subTest(value=value):
                self.assertIsNone(hub_version_relation(value))

    def test_the_declaration_lives_in_one_place(self) -> None:
        """One source of truth, so the number and the verdict cannot disagree.

        The constant is declared in the adapter that owns the wire contract, and
        the two reports of it — the per-controller field and `/health` — read it
        from there rather than spelling the version again.
        """
        from pathlib import Path

        from gateway.adapters.studio_hub import SUPPORTED_STUDIO_HUB_VERSION

        root = Path(__file__).resolve().parents[1]
        for name in ("gateway/fleet.py", "gateway/api.py", "gateway/admin.py"):
            source = (root / name).read_text()
            self.assertNotIn(
                f'"{SUPPORTED_STUDIO_HUB_VERSION}"', source,
                f"{name} spells the supported Hub version instead of importing it",
            )
        # And the paths that decide what runs never mention the relation at all:
        # routing, the catalogue and the worker ask capability questions.
        for name in ("gateway/catalog.py", "gateway/worker.py"):
            source = (root / name).read_text()
            self.assertNotIn("hub_version_relation", source, name)
            self.assertNotIn("SUPPORTED_STUDIO_HUB_VERSION", source, name)


class DiscardRecoveryHoldTests(_HubCase):
    """The controller may now act on a hold — explicitly, and never on a timer."""

    def test_discard_calls_the_hubs_route_for_a_hold(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().discard(
                self.request(modality="music", upstream_job_id=BATCH)
            )
        self.assertTrue(answer["ok"])
        self.assertEqual(hub.discards, [f"/api/hub/jobs/{BATCH}/items/0/discard"])
        self.assertTrue(answer["answer"]["ok"])

    def test_a_hub_that_refuses_the_discard_says_so(self) -> None:
        """Only an uncertain item can be discarded — the Hub answers 400."""
        hub = _Hub(discard_status=400)
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().discard(
                self.request(modality="image", upstream_job_id=BATCH)
            )
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["status"], 400)
        self.assertIn("uncertain", answer["answer"]["detail"])

    def test_transcription_has_no_discard_route_to_call(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().discard(
                self.request(modality="transcription", upstream_job_id=BATCH)
            )
        self.assertEqual(hub.discards, [])
        self.assertFalse(answer["ok"])
        self.assertIn("no discard route for transcription", answer["reason"])

    def test_a_job_that_never_dispatched_has_nothing_to_discard(self) -> None:
        hub = _Hub()
        with self.hub_client(hub):
            answer = hub_module.StudioHubBackend().discard(self.request(modality="image"))
        self.assertEqual(hub.discards, [])
        self.assertFalse(answer["ok"])
        self.assertIn("nothing was dispatched", answer["reason"])


class DiscardActionTests(_HubCase):
    """The admin action is the only thing that discards, and only for a hold."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        self.api = api
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))

    def tearDown(self) -> None:
        self.client.close()

    def _hold(self, *, context: bool = True) -> str:
        """A job settled as an uncertain Hub item, the way the worker leaves it."""
        row, created = self.api.store.submit(
            modality="music",
            model_id="model/test",
            params={"prompt": "x"},
            idempotency_key=f"discard-{self.id()}",
            provider_id="studio-hub-test",
        )
        self.assertTrue(created)
        self.assertTrue(self.api.store.mark_running(row["id"]))
        self.api.store.record_upstream_acceptance(row["id"], BATCH)
        self.api.store.fail(
            row["id"],
            code="UNKNOWN_OUTCOME",
            detail="the Hub settled the item without knowing the worker's outcome",
            recovery_state="unknown_outcome",
            recovery_detail="batch hub-batch-1 item 0",
            recovery_context=(
                {
                    "hub_batch_id": BATCH,
                    "hub_item_index": 0,
                    "hub_worker": "music-worker-1",
                    "hub_discard_route": f"/api/hub/jobs/{BATCH}/items/0/discard",
                }
                if context
                else None
            ),
        )
        return row["id"]

    def _discard(self, job_id: str, hub: _Hub) -> Any:
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(
            self.api.registry, "definition", return_value=definition
        ), patch.object(
            self.api.registry.credentials, "resolved", return_value={"hub_token": "t"}
        ), self.hub_client(hub):
            return self.client.post(
                f"/admin/api/jobs/{job_id}/discard-uncertain"
            )

    def test_the_operator_action_discards_and_closes_the_hold(self) -> None:
        hub = _Hub()
        job_id = self._hold()
        response = self._discard(job_id, hub)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(hub.discards, [f"/api/hub/jobs/{BATCH}/items/0/discard"])
        job = self.api.store.get(job_id)
        self.assertEqual(job["error_code"], "DISCARDED_UNCERTAIN")
        self.assertFalse(job["recovery_required"], "a resolved hold must stop asking")
        self.assertIsNone(job["recovery_context"])
        self.assertEqual(job["discard_remote"]["status"], 200)

    def test_a_job_that_is_not_a_hold_is_refused(self) -> None:
        hub = _Hub()
        row, _ = self.api.store.submit(
            modality="music",
            model_id="model/test",
            params={"prompt": "x"},
            idempotency_key=f"not-a-hold-{self.id()}",
            provider_id="studio-hub-test",
        )
        response = self._discard(row["id"], hub)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "NO_RECOVERY_HOLD")
        self.assertEqual(hub.discards, [], "live work must never be discarded")

    def test_a_hold_recorded_without_context_is_refused_plainly(self) -> None:
        hub = _Hub()
        job_id = self._hold(context=False)
        response = self._discard(job_id, hub)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "RECOVERY_CONTEXT_MISSING")
        self.assertEqual(hub.discards, [])

    def test_a_refused_discard_keeps_the_hold(self) -> None:
        hub = _Hub(discard_status=400)
        job_id = self._hold()
        response = self._discard(job_id, hub)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["code"], "DISCARD_REFUSED")
        job = self.api.store.get(job_id)
        self.assertTrue(job["recovery_required"], "a refused discard resolves nothing")
        self.assertEqual(job["discard_remote"]["status"], 400)

    def test_a_hold_on_a_lane_with_no_discard_route_is_closed_here(self) -> None:
        """**The hold that had no action at all.**

        Measured live: a job the gateway restarted underneath was left on
        "manual recovery required" by `_recover_interrupted`, which records no
        `recovery_context`; this endpoint answered `RECOVERY_CONTEXT_MISSING`
        forever and the Jobs window offered no button, because it read the context
        itself. No cloud lane has a discard route, so there is nothing upstream to
        tell — the operator closing the gateway's own record is the only action,
        and it must say that nothing was sent, not quote a discard.
        """
        hub = _Hub()
        job_id = self._hold(context=False)
        openrouter = SimpleNamespace(
            id="openrouter-test", kind="openai_compatible", display_name="OpenRouter",
            base_url="https://openrouter.example/api/v1",
        )
        with patch.object(
            self.api.registry, "definition", return_value=openrouter
        ), patch.object(
            self.api.registry.credentials, "resolved", return_value={"api_key": "k"}
        ):
            response = self.client.post(f"/admin/api/jobs/{job_id}/discard-uncertain")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["closed"], "local")
        self.assertIsNone(body["discard"], "no discard happened, so none is reported")
        self.assertEqual(hub.discards, [], "no lane with a discard route was called")
        job = self.api.store.get(job_id)
        self.assertEqual(job["error_code"], "HOLD_CLOSED")
        self.assertFalse(job["recovery_required"], "the hold must stop asking")
        self.assertIsNone(job["recovery_context"])
        # The record says the provider was not told, so nobody later reads this as
        # "the upstream abandoned it".
        self.assertIn("has no discard route", job["error_detail"])
        self.assertIn("not told", job["error_detail"])

    def test_a_cloud_hold_is_published_as_a_local_close_to_the_app(self) -> None:
        """The action the Jobs window draws comes from the gateway, not a guess."""
        from gateway import api as api_module

        job_id = self._hold()
        job = self.api.store.get(job_id)
        openrouter = SimpleNamespace(
            id="openrouter-test", kind="openai_compatible", display_name="OpenRouter",
            base_url="https://openrouter.example/api/v1",
        )
        hub_definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", display_name="Hub",
            base_url="http://hub.test",
        )
        with patch.object(self.api.registry, "definition", return_value=openrouter):
            self.assertEqual(api_module.recovery_action(job), "close_hold")
        with patch.object(self.api.registry, "definition", return_value=hub_definition):
            self.assertEqual(api_module.recovery_action(job), "discard_upstream")
        # A Hub hold that names no attempt has no honest action, so the window
        # must draw no button rather than one that fails on press.
        unnamed = dict(job, recovery_context=None)
        with patch.object(self.api.registry, "definition", return_value=hub_definition):
            self.assertIsNone(api_module.recovery_action(unnamed))
        # And a settled job is not a hold at all.
        self.assertIsNone(api_module.recovery_action(dict(job, recovery_required=False)))

    def test_a_lane_that_says_it_cannot_discard_is_closed_here(self) -> None:
        """**The lane's own contract outranks the adapter's capability.**

        Hub 0.6.61 states `lane_contract.states.discard_supported` per lane, and
        its chat lane says `false` — a synchronous completion has no queued attempt
        to abandon. The adapter has a discard call, so before this the operator was
        offered "Discard uncertain attempt…" for a lane that would refuse it: a
        button that fails on press. Now the action published for the job is
        `close_hold`, and the endpoint takes the same local path as a lane with no
        discard route at all.
        """
        from gateway import api as api_module

        hub = _Hub()
        job_id = self._hold()
        job = self.api.store.get(job_id)
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        chat_lane = {
            "model_id": "model/test",
            "raw": {
                "lane_contract": {
                    "schema": "studiohub.lane-contract.v1",
                    "modality": "chat",
                    "states": {"uncertain": "uncertain", "discard_supported": False},
                }
            },
        }
        with patch.object(self.api.registry, "definition", return_value=definition):
            # A lane that states nothing keeps the discard it has always had.
            self.assertEqual(api_module.recovery_action(job), "discard_upstream")
            with patch.object(
                self.api.registry.models, "models_for", return_value=[chat_lane]
            ):
                self.assertEqual(api_module.recovery_action(job), "close_hold")
                with patch.object(
                    self.api.registry.credentials, "resolved", return_value={"hub_token": "t"}
                ), self.hub_client(hub):
                    response = self.client.post(
                        f"/admin/api/jobs/{job_id}/discard-uncertain"
                    )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["closed"], "local")
        self.assertEqual(hub.discards, [], "a lane that says it cannot discard is not asked")
        closed = self.api.store.get(job_id)
        self.assertFalse(closed["recovery_required"])
        self.assertEqual(closed["error_code"], "HOLD_CLOSED")
        # The recorded sentence says *why* nothing was sent, in the lane's terms.
        self.assertIn("own contract", closed["error_detail"])
        self.assertIn("not told", closed["error_detail"])

    def test_a_lane_that_states_nothing_keeps_its_discard(self) -> None:
        from gateway import api as api_module

        hub = _Hub()
        job_id = self._hold()
        job = self.api.store.get(job_id)
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(self.api.registry, "definition", return_value=definition), \
                patch.object(self.api.registry.models, "models_for", return_value=[]):
            self.assertEqual(api_module.recovery_action(job), "discard_upstream")
        response = self._discard(job_id, hub)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(hub.discards, [f"/api/hub/jobs/{BATCH}/items/0/discard"])

    def test_nothing_discards_on_its_own(self) -> None:
        """An uncertain outcome must not walk itself into a discard."""
        hub = _Hub(
            states=["uncertain"],
            item={
                "state": "uncertain",
                "error": "the worker outcome is not known",
                "error_code": "MUSIC_OUTCOME_UNCERTAIN",
                "worker_id": "music-worker-1",
            },
        )
        store = JobStore(_TEMP_ROOT / "no-auto-discard.sqlite3")
        try:
            row, _ = store.submit(
                modality="music",
                model_id="model/test",
                params={"prompt": "x"},
                idempotency_key="no-auto-discard",
                provider_id="studio-hub-test",
            )
            self.assertTrue(store.mark_running(row["id"]))
            with patch.object(worker_module, "registry", _Registry()), patch.object(
                worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
            ), self.hub_client(hub):
                worker_module.execute_job(row, store)
            held = store.get(row["id"])
            self.assertEqual(hub.discards, [], "the gateway must never discard by itself")
            self.assertTrue(held["recovery_required"])
            self.assertIsNone(held["discard_remote"])
        finally:
            store.close()


class LocationRefusalTests(_HubCase):
    """A Hub 409 MODEL_NOT_READY is a site property, not a provider failure.

    The adapter must name it so the worker can try another location instead of
    settling the job failed, and it must say the Hub accepted nothing so the job
    is never held as an unknown outcome.
    """

    def test_a_not_ready_refusal_is_a_location_refusal(self) -> None:
        # The REAL batch-submit wire shape: `HTTPException(409, {dict})` is rendered by FastAPI as
        # `{"detail": {dict}}`. (Measured: the owner's DB holds exactly this text for a 409.)
        hub = _Hub(submit_refusal={"detail": {
            "code": "MODEL_NOT_READY",
            "detail": "model files, runtime, OS, or chip are not ready",
        }})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertFalse(caught.exception.upstream_accepted)
        self.assertTrue(caught.exception.retryable)

    def test_a_not_packaged_refusal_is_a_location_refusal(self) -> None:
        hub = _Hub(submit_refusal={"detail": {
            "code": "MODEL_NOT_PACKAGED",
            "detail": "model is not in the packaged catalog",
        }})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertFalse(caught.exception.upstream_accepted)

    def test_the_site_properties_reroute_and_the_operator_codes_stay_terminal(self) -> None:
        for code, reroutes in (("STUDIO_NOT_RUNNING", True), ("OUTPUT_STORAGE_BLOCKED", True),
                               ("MODEL_CATALOG_UNAVAILABLE", True), ("MODEL_PAUSED", False),
                               ("MACHINE_DISABLED", False), ("STUDIO_DISABLED", False)):
            hub = _Hub(submit_refusal={"detail": {"code": code, "detail": "refused"}})
            with self.assertRaises(BackendError) as caught:
                self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
            self.assertEqual(caught.exception.code,
                             "LOCATION_NOT_READY" if reroutes else "PROVIDER_FAILED", code)
            self.assertIs(caught.exception.upstream_accepted, False)

    def test_another_refusal_is_definite_not_unknown(self) -> None:
        # The Hub answered and refused, so it accepted nothing. `None` here meant
        # "nobody knows", which turned a definite 4xx into a durable unknown-outcome
        # hold — measured live on a 409 "This Hub is in agent mode".
        hub = _Hub(submit_refusal={"code": "VALIDATION_FAILED", "detail": "bad prompt"})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.code, "PROVIDER_FAILED")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertFalse(caught.exception.retryable)

    def test_an_agent_mode_refusal_is_definite_not_unknown(self) -> None:
        # The live shape: a 409 carrying only `detail`, no code. The Hub refused to
        # take customer work, so the job must fail cleanly rather than become a hold.
        hub = _Hub(submit_refusal={
            "detail": "This Hub is in agent mode; submit customer jobs to a controller."
        })
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertIs(caught.exception.upstream_accepted, False)

    def test_a_server_error_stays_ambiguous(self) -> None:
        # A 5xx may have been accepted before it failed, so it keeps the careful
        # path — the distinction the 4xx rule must not swallow.
        hub = _Hub(submit_refusal=None)
        hub.server_error = True
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.status, 500)
        self.assertIsNone(caught.exception.upstream_accepted)
        self.assertTrue(caught.exception.retryable)


class VideoLaneTests(_HubCase):
    """The local video lane's two doors, through the adapter that opens them.

    Text-to-video is the broker's JSON door (`/api/hub/jobs` → `/api/generate/txt2video`) and
    image-to-video is its multipart door (`reference_images` → `/api/generate/video2video`). What
    is asserted here is what the gateway *sends*: the Hub's own vocabulary (`duration`, not the
    lane's `duration_seconds`), the reference as inline base64 — the only one of the Hub's three
    reference forms that works across two systems — and the video media type on the way back.
    """

    def test_a_text_to_video_request_carries_the_lane_s_parameters(self) -> None:
        hub = _Hub(artifact=b"\x00\x00\x00\x18ftypmp42", artifact_media_type="video/mp4")
        request = self.request(modality="video", model_id="mock-hub/video-model",
                               params={"prompt": "a harbour at dawn",
                                       "aspect_ratio": "11:7", "resolution": "448p",
                                       "duration_seconds": 4})
        result = self.generate(hub, request)
        payload = hub.payloads[-1]
        self.assertEqual(payload["modality"], "video")
        self.assertEqual(payload["model"], "mock-hub/video-model")
        item = payload["items"][0]
        self.assertEqual(item["prompt"], "a harbour at dawn")
        self.assertNotIn("reference_images", item,
                         "a prompt-only job must not claim a source image")
        # The broker flattens form fields and carries `duration`; the lane's published name is
        # `duration_seconds`. Both travel, and the worker's validator refuses them if they ever
        # disagree rather than picking one.
        self.assertEqual(item["params"]["duration"], 4)
        self.assertEqual(item["params"]["aspect_ratio"], "11:7")
        self.assertEqual(item["params"]["resolution"], "448p")
        self.assertEqual(result.media_type, "video/mp4")

    def test_an_image_to_video_request_sends_the_reference_inline(self) -> None:
        from gateway import video_assets

        scene = b"\x89PNG\r\n\x1a\n" + b"scene-bytes" * 8
        asset_id = video_assets.store(scene, "scene.png", "image/png")
        hub = _Hub(artifact=b"\x00\x00\x00\x18ftypmp42", artifact_media_type="video/mp4")
        request = self.request(modality="video", model_id="mock-hub/video-model",
                               params={"prompt": "push in", "mode": "image-to-video",
                                       "reference_asset_id": asset_id})
        self.generate(hub, request)
        item = hub.payloads[-1]["items"][0]
        # The Hub keeps only `prompt`, `seed` and `params` from an item, and pops
        # `reference_images` off the merged body — so the reference must be inside `params`. At
        # the item's top level it is dropped silently and the job becomes text-to-video.
        self.assertNotIn("reference_images", item,
                         "an item-level reference is dropped by the Hub, not forwarded")
        self.assertEqual(len(item["params"]["reference_images"]), 1)
        reference = item["params"]["reference_images"][0]
        self.assertEqual(reference["mime"], "image/png")
        self.assertEqual(base64.b64decode(reference["b64"]), scene)

    def test_a_silent_request_is_forwarded_rather_than_dropped(self) -> None:
        """`audio: false` reaches the Hub's item body, and an unstated choice stays unstated.

        Hub 0.6.83 gave its video lane an `audio` switch and publishes it on the lane contract, so
        a caller may ask for a silent MP4. **A parameter this gateway publishes and then drops is
        a lie with a schema**: Story Studio would render a switch that changed nothing while the
        caller paid for a track it had asked not to be written. `false` is the value that must
        survive — a truthiness test that forwarded on `if value:` would lose exactly the half of
        the switch that a silent-video request is made of.
        """
        hub = _Hub(artifact=b"\x00\x00\x00\x18ftypmp42", artifact_media_type="video/mp4")
        request = self.request(modality="video", model_id="mock-hub/video-model",
                               params={"prompt": "a harbour at dawn", "aspect_ratio": "11:7",
                                       "resolution": "448p", "duration_seconds": 4,
                                       "audio": False})
        self.generate(hub, request)
        item = hub.payloads[-1]["items"][0]
        self.assertIn("audio", item["params"],
                      "a dropped choice is a control that does nothing")
        self.assertIs(item["params"]["audio"], False)
        # And the lane's own default applies where the caller stated nothing: sending `True` on
        # every request would override a lane whose default is the other way.
        quiet = _Hub(artifact=b"\x00\x00\x00\x18ftypmp42", artifact_media_type="video/mp4")
        self.generate(quiet, self.request(
            modality="video", model_id="mock-hub/video-model",
            params={"prompt": "a harbour at dawn", "duration_seconds": 4}))
        self.assertNotIn("audio", quiet.payloads[-1]["items"][0]["params"])

    def test_a_reference_that_aged_out_is_a_caller_error(self) -> None:
        hub = _Hub()
        request = self.request(modality="video", params={
            "prompt": "push in", "reference_asset_id": "00000000-0000-0000-0000-000000000000"})
        with self.assertRaises(BackendError) as raised:
            self.generate(hub, request)
        self.assertEqual(raised.exception.code, "INVALID_PARAMS")
        self.assertIn("no longer held", str(raised.exception))
        self.assertEqual(hub.payloads, [], "nothing may be submitted without its source image")

    def test_the_artifact_type_is_read_not_assumed(self) -> None:
        hub = _Hub(artifact=b"\x00\x00\x00\x18ftypmp42", artifact_media_type="video/mp4")
        result = self.generate(hub, self.request(modality="video", params={"prompt": "x"}))
        self.assertEqual(result.media_type, "video/mp4",
                         "a video artifact must not be relabelled as audio")


class CancelReachesRunningWorkTests(_HubCase):
    """A cancel requested while the job runs must end `cancelled`, and must reach the Hub."""

    def _run(self, key: str, *, cancel_on: str, states: list[str]):
        hub = _Hub(states=states)
        store = JobStore(_TEMP_ROOT / f"{key}.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="image", model_id="model/test", params={"prompt": "x"},
                              idempotency_key=key, provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))
        real_handler, fired = hub.handler, []

        def handler(request: httpx.Request) -> httpx.Response:
            reply = real_handler(request)
            if not fired and f"{request.method} {request.url.path}" == cancel_on:
                fired.append(1)  # the owner presses cancel at exactly this moment
                store.cancel(row["id"])
            return reply

        hub.handler = handler
        with patch.object(worker_module, "registry", _Registry()), patch.object(
            worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
        ), patch.object(hub_module, "POLL_INTERVAL_S", 0), self.hub_client(hub):
            worker_module.execute_job(row, store)
        return hub, store.get(row["id"])

    def test_a_hub_confirmed_cancel_settles_cancelled_not_failed(self) -> None:
        hub, job = self._run("cancel-mid", cancel_on=f"GET /api/hub/jobs/{BATCH}",
                             states=["running", "cancelled"])
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(job["cancel_state"], "confirmed")
        self.assertEqual(hub.calls.count(("POST", "/api/hub/jobs")), 1, "never re-submitted")

    def test_a_cancel_before_the_batch_id_exists_is_sent_once_it_does(self) -> None:
        # The API read the row before there was an id, so it had nothing to DELETE.
        hub, job = self._run("cancel-early", cancel_on="POST /api/hub/jobs",
                             states=["running", "cancelled"])
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertEqual(job["state"], "cancelled")


class HubContractHardeningTests(_HubCase):
    """Findings F6-F10 of the 2026-09-30 gateway<->Hub audit, one check each."""

    def setUp(self) -> None:
        for name, value in (("POLL_INTERVAL_S", 0), ("ARTIFACT_RETRY_S", 0)):
            patcher = patch.object(hub_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _transcribe(self, hub: _Hub, **params: Any) -> BackendResult:
        upload = _TEMP_ROOT / "harden.wav"
        upload.write_bytes(b"audio")
        params = {"_upload_path": str(upload), "_upload_name": "same-name.wav", **params}
        return self.generate(hub, self.request(modality="transcription", params=params))

    # F6
    def test_a_transient_artifact_error_is_retried(self) -> None:
        hub = _Hub(artifact_failures=2)
        result = self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(result.content, b"artifact-bytes")

    def test_an_artifact_that_never_arrives_is_a_hold_not_a_crash(self) -> None:
        hub = _Hub(artifact_failures=99)
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.status, 503)

    def test_a_digest_mismatch_is_refused_and_never_acknowledged(self) -> None:
        hub = _Hub(artifact_sha256="0" * 64)
        with self.assertRaises(BackendError) as caught:
            self.generate_then_deliver(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertIn("sha256", str(caught.exception))
        self.assertTrue(caught.exception.upstream_accepted)
        self.assertEqual(hub.acks, [])

    def test_the_terminal_result_digest_is_checked_when_no_header_is_sent(self) -> None:
        hub = _Hub(artifact_sha256=" ", item={"terminal_result": {"sha256": "f" * 64}})
        with self.assertRaises(BackendError):
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))

    # F7
    def test_a_busy_machine_item_is_a_location_refusal(self) -> None:
        hub = _Hub(states=["error"], item={"state": "error", "error_code": "MACHINE_BUSY",
                                           "error": "local is reserved for other work"})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertTrue(caught.exception.upstream_settled)

    def test_an_operator_decision_item_stays_terminal_and_names_its_code(self) -> None:
        hub = _Hub(states=["error"], item={"state": "error", "error_code": "MODEL_PAUSED",
                                           "error": "paused by the owner"})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.code, "PROVIDER_FAILED")
        self.assertIn("MODEL_PAUSED", str(caught.exception))

    # F8 + F10
    def test_transcription_records_its_time_and_machine(self) -> None:
        hub = _Hub(item={"duration_seconds": 41.5, "studio": "Studio-3@node-b"})
        result = self._transcribe(hub)
        self.assertEqual(result.payload["generation_seconds"], 41.5)
        self.assertEqual(result.payload["hub_machine"], "node-b")

    def test_transcription_sends_the_job_id_so_same_named_uploads_do_not_share_a_batch(self) -> None:
        hub = _Hub()
        self._transcribe(hub, _job_id="job-123")
        body = hub.payloads[0]["body"]
        self.assertIn(b'name="item_ids"\r\n\r\njob-123', body)

    def test_a_duplicate_batch_is_logged(self) -> None:
        with self.assertLogs(hub_module.log, "WARNING") as logged:
            self._transcribe(_Hub(duplicate=True))
        self.assertIn("duplicate", logged.output[0])

    # F9
    def test_a_5xx_or_unreadable_poll_is_ridden_out_like_a_dropped_socket(self) -> None:
        hub = _Hub(poll_faults=[502, "<html>proxy</html>", 503], states=["running", "done"])
        result = self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(result.content, b"artifact-bytes")

    def test_a_4xx_poll_is_still_fatal_at_once(self) -> None:
        hub = _Hub(poll_faults=[404])
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(caught.exception.status, 404)


class _Clock:
    """A fake monotonic clock: `sleep` advances it, nothing else does."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return 1_700_000_000.0 + self.now


class QueueWaitTests(_HubCase):
    """Queue wait does not consume the lane budget (owner decision, 2026-09-30)."""

    def _poll(self, hub: _Hub, *, deadline: float, waits: list | None = None) -> dict[str, Any]:
        clock = _Clock()
        fake = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=clock.time)
        with patch.object(hub_module, "time", fake), self.hub_client(hub), \
                httpx.Client() as client:
            return hub_module._poll_batch(
                client, HUB, {}, BATCH, 0, deadline_seconds=deadline,
                on_wait=(waits.append if waits is not None else None),
            )

    def test_time_spent_queued_does_not_count_against_the_lane_deadline(self) -> None:
        hub = _Hub(states=["queued"] * 40 + ["running"] * 3 + ["done"],
                   queued_item={"error": "waiting for a free machine: local is reserved",
                                "retry_at": 1_700_000_030.0})
        waits: list = []
        outcome = self._poll(hub, deadline=10, waits=waits)  # 40 s queued > the 10 s budget
        self.assertEqual(outcome["item"]["state"], "done")
        self.assertEqual(hub.deletes, [])
        self.assertEqual(waits[0]["reason"], "waiting for a free machine: local is reserved")
        self.assertEqual(waits[0]["next_try_at"], 1_700_000_030.0)
        self.assertIsNone(waits[-1], "the wait ends when a Mac starts rendering")

    def test_running_time_still_bounds_the_job_and_a_running_item_is_never_cancelled(self) -> None:
        hub = _Hub(states=["queued"] * 5 + ["running"])
        with self.assertRaises(BackendError) as caught:
            self._poll(hub, deadline=10)
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")
        self.assertEqual(hub.deletes, [], "never DELETE a running item on a guess")

    def test_a_batch_queued_past_the_ceiling_is_withdrawn_and_may_be_rerouted(self) -> None:
        hub = _Hub(states=["queued"])
        with patch.object(hub_module, "QUEUE_WAIT_CEILING_S", 20.0):
            with self.assertRaises(BackendError) as caught:
                self._poll(hub, deadline=10)
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])


class _HoldRegistry(_Registry):
    @staticmethod
    def definition(_provider_id: str) -> Any:
        return SimpleNamespace(id="studio-hub-test", kind="studio_hub", base_url=HUB,
                               display_name="Hub")


class AdoptedHoldTests(_HubCase):
    """A held job whose Hub item finished is adopted: verified, saved, acked, `succeeded`."""

    def _held_job(self, store: JobStore, key: str) -> dict[str, Any]:
        row, _ = store.submit(modality="image", model_id="model/test", params={"prompt": "x"},
                              idempotency_key=key, provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))
        self.assertTrue(store.begin_dispatch(row["id"]))
        store.record_upstream_acceptance(row["id"], BATCH)
        store.fail(row["id"], code="UNKNOWN_OUTCOME", detail="the gateway restarted",
                   recovery_state="unknown_outcome", recovery_detail="restart",
                   recovery_context={"batch_id": BATCH, "item_index": 0})
        return store.get(row["id"])

    def _sweep(self, hub: _Hub, store: JobStore) -> dict[str, int]:
        with patch.object(worker_module, "registry", _HoldRegistry()), self.hub_client(hub):
            return worker_module.reconcile_holds(store)

    def test_a_finished_hold_is_settled_succeeded_with_its_artifact_and_acked(self) -> None:
        store = JobStore(_TEMP_ROOT / "adopt.sqlite3")
        self.addCleanup(store.close)
        held = self._held_job(store, "adopt-1")
        self.assertTrue(held["recovery_required"])
        hub = _Hub(states=["done"])
        out = self._sweep(hub, store)
        job = store.get(held["id"])
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(job["state"], "succeeded")
        self.assertFalse(job["recovery_required"])
        self.assertIsNone(job["error_code"])
        self.assertEqual(Path(job["asset_path"]).read_bytes(), b"artifact-bytes")
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])

    def test_bytes_that_do_not_verify_leave_the_hold_alone(self) -> None:
        store = JobStore(_TEMP_ROOT / "adopt-bad.sqlite3")
        self.addCleanup(store.close)
        held = self._held_job(store, "adopt-2")
        hub = _Hub(states=["done"], artifact_sha256="0" * 64)
        out = self._sweep(hub, store)
        job = store.get(held["id"])
        self.assertEqual((out["adopted"], out["still_open"]), (0, 1))
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["recovery_required"])
        self.assertEqual(hub.acks, [], "an artifact that failed its check is never acknowledged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
