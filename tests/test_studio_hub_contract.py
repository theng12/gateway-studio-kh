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

def _artifact_path(name: str) -> Path:
    """A staged upload where the Gateway really stages one — inside ARTIFACT_DIR.

    The adapters now refuse a path outside the artifact store (review 2026-10-28), because a
    params dict can name any file; a fixture writing beside the profile was testing a shape
    the gateway never produces.
    """
    artifacts = Path(os.environ["GATEWAY_ARTIFACTS"])
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts / name


from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest, BackendResult  # noqa: E402
from gateway.store import JobStore  # noqa: E402
import gateway.worker as worker_module  # noqa: E402

HUB = "https://hub.test"
BATCH = "hub-batch-1"
#: Real headers: the Gateway refuses an artifact that is not the file its type says (0.2.161).
ARTIFACT_BYTES = b"RIFF\x24\x00\x00\x00WAVE" + b"artifact-bytes"
M4A_BYTES = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00isom\x00\x00\x00\x08moov"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"


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
        artifact: bytes = ARTIFACT_BYTES,
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
        self.reconciles: list[str] = []
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
        #: The Hub's review-candidate answer for a refused narration item (a callable is asked
        #: each time), or `None` for a Hub that has none (404).
        self.review: Any = None

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
        if method == "POST" and path.endswith("/reconcile"):
            self.reconciles.append(path)
            if self.discard_status >= 400:
                return httpx.Response(self.discard_status, json={"detail": "Image item is not uncertain"})
            return httpx.Response(200, json={"batch_id": BATCH, "reconciled": True,
                                             "item": {"state": "done"}})
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
        if method == "GET" and path == f"/api/hub/jobs/{BATCH}/items/0/review-candidate":
            answer = self.review() if callable(self.review) else self.review
            if answer is None:
                return httpx.Response(404, json={"detail": "this item has no review candidate"})
            return httpx.Response(200, json=answer)
        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})


#: A refused narration section's WAV and the Hub's answer about it (`job_review_candidate`).
REVIEW_PCM = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(200))
REVIEW_SHA = hashlib.sha256(REVIEW_PCM).hexdigest()


def canonical_sha256(value: Any) -> str:
    """The Voice worker's receipt digest, spelled out here rather than borrowed from the adapter."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


SECTION_REFUSAL = ("SectionQualityError: Section 6 of 7 came out silent on all 4 attempts, so the "
                   "audio was not delivered (attempt 4: voiced 2.54 s < 4.06 s).")


def review_answer(*, batch: str = BATCH, pcm: bytes = REVIEW_PCM, text: str = "section text",
                  **changes: Any) -> dict[str, Any]:
    receipt = {"schema": "studiohub.rejected-voice-review-candidate.v1", "active": False,
               "disposition": "review_only", "worker_job_id": "c85e8c5d0a1b",
               "client_request_id": f"studiohub:{batch}:0",
               "section": {"index": 6, "total": 7, "attempt": 4, "text": text},
               "audio": {"media_type": "audio/wav", "bytes": len(pcm),
                         "sha256": hashlib.sha256(pcm).hexdigest()}}
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return {"batch_id": batch, "item_index": 0, "active": False, "disposition": "review_only",
            "deliverable": False, "imported": False, "receipt": receipt,
            "audio_url": f"/api/hub/jobs/{batch}/items/0/review-candidate/audio", **changes}


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

    def test_the_measured_audio_length_is_recorded_for_the_daily_summary(self) -> None:
        for terminal, expected in (({"audio_duration_s": 61.5}, 61.5), ({"audio_duration_ms": 90000}, 90.0), ({}, None)):
            hub = _Hub(item={"terminal_result": {"machine_id": "node-b", **terminal}})
            result = self.generate_then_deliver(hub, self.request(modality="tts", params={"text": "hello"}))
            self.assertEqual(result.payload["audio_duration_s"], expected)

    def test_the_hubs_section_quality_check_reaches_the_payload(self) -> None:
        # Studio Hub 0.6.137 keeps its per-section TTS check on the batch item (not in
        # `terminal_result`); Story shows it per chapter, so it must survive collection.
        quality = {"sections": 9, "remade": [{"section": 4, "attempts": 2, "code": "TTS_SECTION_SILENT"}]}
        hub = _Hub(item={"terminal_result": {"machine_id": "node-b"}, "section_quality": quality})
        result = self.generate_then_deliver(hub, self.request(modality="tts", params={"text": "hello"}))
        self.assertEqual(result.payload["section_quality"], quality)
        hub = _Hub(item={"terminal_result": {"machine_id": "node-b"}})
        result = self.generate_then_deliver(hub, self.request(modality="tts", params={"text": "hello"}))
        self.assertIsNone(result.payload["section_quality"])


class DeliveryReceiptTests(_HubCase):
    """Item 1 — a verified delivery is acknowledged, on the modality's own route."""

    def test_image_delivery_is_acknowledged(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(hub, self.request(modality="image", params={"prompt": "a cat"}))
        self.assertEqual(result.content, ARTIFACT_BYTES)
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])
        self.assertTrue(result.payload["delivery_ack"]["ok"])
        self.assertEqual(result.payload["delivery_ack"]["status"], 200)

    def test_voice_delivery_is_acknowledged_on_the_jobs_route(self) -> None:
        hub = _Hub()
        result = self.generate_then_deliver(hub, self.request(modality="tts", params={"text": "hello"}))
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])
        self.assertTrue(result.payload["delivery_ack"]["ok"])

    def test_transcription_delivery_is_acknowledged_on_its_own_route(self) -> None:
        upload = _artifact_path("audio.wav")
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
        self.assertEqual(result.content, ARTIFACT_BYTES)

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
            content=PNG_BYTES, media_type="image/png", payload=payload, deliver=deliver
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
        _artifact_path("a.mp3").write_bytes(b"audio")
        hub = _Hub(connect_error=True)
        with self.assertRaises(hub_module.BackendError) as caught:
            self.generate(hub, self.request(
                modality="transcription",
                # A real staged upload inside the artifact store: the connection is
                # refused before a byte of it matters, but the path the Gateway would have
                # written is the only kind an adapter reads at all (review 2026-10-28).
                params={"_upload_path": str(_artifact_path("a.mp3")), "_upload_name": "a.mp3"},
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
            "name": "Yim Mengly",
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
                )[0]
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
            # So the Hub's job rows can say which voice spoke (owner, 2026-10-24).
            ("display_name", b"Yim Mengly"),
            ("ttl_seconds", str(int(hub_module._STAGED_TTL_S)).encode()),
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
        # The clip travels with the job: the controller's own library is never asked, because a
        # library id names an entry on one Mac and the broker may run the job on another.
        self.assertNotIn(("GET", "/studio/voice/api/voices"), hub.calls)

    def test_the_clip_is_staged_once_per_location_and_reused(self) -> None:
        clip = self._clip()
        hub = _Hub(reference=self._staged_payload(
            clip, expires_at=time.time() + hub_module._STAGED_TTL_S))
        reference = self._reference(clip)
        hub_module._REFERENCE_CACHE.clear()
        client = httpx.Client(transport=httpx.MockTransport(hub.handler))
        try:
            with self.hub_client(hub):
                for _ in range(3):
                    asset = hub_module._voice_placement(
                        client, HUB, {"X-Hub-Token": "t"}, "voice-abc123", reference)
                    self.assertEqual(asset, "staged-asset-1")
        finally:
            client.close()
            hub_module._REFERENCE_CACHE.clear()
        self.assertEqual(hub.calls.count(("POST", "/api/hub/voice-references")), 1)
        # A reused asset must outlive the reuse window, or a queued job finds it expired.
        self.assertGreater(hub_module._STAGED_TTL_S, hub_module._REFERENCE_TTL_S)
        self.assertEqual(hub_module._REFERENCE_TTL_S, 3 * 86400)  # owner, 2026-10-11

    def test_a_hub_that_keeps_the_clip_for_less_shortens_the_reuse(self) -> None:
        """Hub 0.6.166 clamps a staged clip to 48 h: reuse must end before it expires there."""
        now = time.time()
        self.assertEqual(hub_module._reuse_until(now, now + 48 * 3600), now + 36 * 3600)
        self.assertEqual(hub_module._reuse_until(now, now + 400 * 3600), now + hub_module._REFERENCE_TTL_S)
        self.assertEqual(hub_module._reuse_until(now, None), now + hub_module._REFERENCE_TTL_S)
        clip = self._clip()
        hub = _Hub(reference=self._staged_payload(clip, expires_at=now + 3600))
        hub_module._REFERENCE_CACHE.clear()
        client = httpx.Client(transport=httpx.MockTransport(hub.handler))
        try:
            with self.hub_client(hub):
                for _ in range(2):
                    hub_module._voice_placement(
                        client, HUB, {"X-Hub-Token": "t"}, "voice-abc123", self._reference(clip))
        finally:
            client.close()
            hub_module._REFERENCE_CACHE.clear()
        # An hour left at the Hub is inside the headroom, so it is staged again, never reused.
        self.assertEqual(hub.calls.count(("POST", "/api/hub/voice-references")), 2)


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
            idempotency_key=f"discard-{self.id()}-{context}",
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

    def _reconcile(self, job_id: str, hub: _Hub) -> Any:
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(
            self.api.registry, "definition", return_value=definition
        ), patch.object(
            self.api.registry.credentials, "resolved", return_value={"hub_token": "t"}
        ), self.hub_client(hub):
            return self.client.post(f"/admin/api/jobs/{job_id}/reconcile-hold")

    def test_the_operator_can_ask_the_hub_to_reconcile_a_hold(self) -> None:
        """2026-10-05: five finished node renders were discarded for want of this from the Gateway."""
        hub = _Hub()
        job_id = self._hold()
        response = self._reconcile(job_id, hub)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["reconciled"], response.json()["item_state"]), (True, "done"))
        self.assertEqual(hub.reconciles, [f"/api/hub/jobs/{BATCH}/items/0/reconcile"])
        self.assertEqual(hub.discards, [])
        self.assertTrue(self.api.store.get(job_id)["recovery_required"],
                        "the hold sweep settles it from the Hub's record, not this route")

    def test_a_refused_reconcile_and_a_non_hold_are_reported(self) -> None:
        job_id = self._hold()
        response = self._reconcile(job_id, _Hub(discard_status=400))
        self.assertEqual((response.status_code, response.json()["code"]), (409, "RECONCILE_REFUSED"))
        self.assertIn("not uncertain", response.json()["detail"])
        row, _ = self.api.store.submit(modality="music", model_id="model/test", params={"prompt": "x"},
                                       idempotency_key=f"live-{self.id()}", provider_id="studio-hub-test")
        hub = _Hub()
        response = self._reconcile(row["id"], hub)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "NO_RECOVERY_HOLD"))
        self.assertEqual(hub.reconciles, [])

    def _resolve_all(self, hub: _Hub, query: str = "") -> Any:
        definition = SimpleNamespace(
            id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub"
        )
        with patch.object(self.api.registry, "definition", return_value=definition), patch.object(
            self.api.registry.credentials, "resolved", return_value={"hub_token": "t"}
        ), self.hub_client(hub):
            return self.client.post(f"/admin/api/jobs/resolve-recovery{query}")

    def _only_these_holds(self) -> None:
        for job in self.api.store.holds(limit=1000):  # earlier tests' leftovers share this store
            self.api.store.close_recovery_hold(job["id"], code="HOLD_CLOSED", detail="test reset")

    def test_resolve_all_recovers_what_the_hub_has_and_discards_the_rest(self) -> None:
        """0.2.153: the per-job buttons, once for every needs-recovery job, and nothing else."""
        self._only_these_holds()
        named, unnamed = self._hold(), self._hold(context=False)
        row, _ = self.api.store.submit(modality="music", model_id="model/test", params={"prompt": "x"},
                                       idempotency_key=f"live-{self.id()}", provider_id="studio-hub-test")
        self.api.store.mark_running(row["id"])
        running = dict(self.api.store.get(row["id"]))
        hub = _Hub()
        response = self._resolve_all(hub)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({k: response.json()[k] for k in ("recovered", "discarded", "left")},
                         {"recovered": 1, "discarded": 1, "left": []})
        self.assertEqual(hub.reconciles, [f"/api/hub/jobs/{BATCH}/items/0/reconcile"])
        self.assertEqual(hub.discards, [], "a finished result is kept, never discarded")
        self.assertTrue(self.api.store.get(named)["recovery_required"], "the hold sweep collects it")
        closed = self.api.store.get(unnamed)
        self.assertEqual((closed["recovery_required"], closed["error_code"]), (False, "HOLD_CLOSED"))
        self.assertEqual(self.api.store.get(row["id"]), running, "running work is never touched")

    def test_resolve_all_discards_when_nothing_can_be_recovered(self) -> None:
        self._only_these_holds()
        job_id = self._hold()
        hub = _Hub()
        refused = self.api.GatewayError(409, "no", "RECONCILE_REFUSED")
        with patch.object(self.api, "_reconcile_recovery_hold", side_effect=refused):
            response = self._resolve_all(hub)
        self.assertEqual((response.json()["recovered"], response.json()["discarded"]), (0, 1))
        self.assertEqual(hub.discards, [f"/api/hub/jobs/{BATCH}/items/0/discard"])
        self.assertEqual(self.api.store.get(job_id)["error_code"], "DISCARDED_UNCERTAIN")

    def test_resolve_all_reports_a_refusal_and_leaves_newer_holds_alone(self) -> None:
        self._only_these_holds()
        job_id = self._hold()
        response = self._resolve_all(_Hub(discard_status=400))
        self.assertEqual(response.json()["left"][0]["id"], job_id)
        self.assertTrue(self.api.store.get(job_id)["recovery_required"])
        hub = _Hub()
        response = self._resolve_all(hub, f"?before={time.time() - 60}")
        self.assertEqual((response.json()["recovered"], response.json()["discarded"], hub.reconciles),
                         (0, 0, []), "a hold newer than the confirmation is not touched")

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

    def test_a_hold_recorded_without_context_is_closed_here_and_says_so(self) -> None:
        """It used to be refused with RECOVERY_CONTEXT_MISSING and drawn with no button, so nothing
        could clear it (three 2026-10-03 submit-timeout holds). Nothing is sent upstream."""
        hub = _Hub()
        job_id = self._hold(context=False)
        response = self._discard(job_id, hub)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["closed"], "local")
        self.assertEqual(hub.discards, [])
        job = self.api.store.get(job_id)
        self.assertFalse(job["recovery_required"])
        self.assertIn("does not name the upstream attempt", job["error_detail"])

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
        # A Hub hold that names no attempt can only be closed here (nothing upstream to aim at).
        unnamed = dict(job, recovery_context=None)
        with patch.object(self.api.registry, "definition", return_value=hub_definition):
            self.assertEqual(api_module.recovery_action(unnamed), "close_hold")
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

    def _pool_hold(self) -> str:
        """A pool job settled as uncertain on one member — the shape `pool.generate` leaves.

        `pool_member` and the member's own `provider_id` are what `PoolBackend` writes into the
        error's `recovery` when the walk stops on an uncertain send; the job carries that as its
        recovery context.
        """
        row, created = self.api.store.submit(
            modality="music",
            model_id="writing-haiku-gemini-luna-ordered",
            params={"prompt": "x"},
            idempotency_key=f"pool-discard-{self.id()}",
            provider_id="pool",
        )
        self.assertTrue(created)
        self.assertTrue(self.api.store.mark_running(row["id"]))
        self.api.store.fail(
            row["id"],
            code="PROVIDER_SUBMISSION_UNCERTAIN",
            detail="the paid member may still be running this attempt",
            recovery_state="unknown_outcome",
            recovery_detail="batch hub-batch-1 item 0",
            recovery_context={
                "hub_batch_id": BATCH,
                "hub_item_index": 0,
                "hub_discard_route": f"/api/hub/jobs/{BATCH}/items/0/discard",
                "pool_member": "studio-hub-test::model/test",
                "provider_id": "studio-hub-test",
                "pool_attempts": [{"member": "studio-hub-test::model/test", "outcome": "failed"}],
            },
        )
        return row["id"]

    def test_a_pool_hold_is_discarded_at_the_member_that_ran_the_attempt(self) -> None:
        """**The pool lane had no discard route at all**, so `api` closed every pool hold here.

        47 of the 121 `HOLD_CLOSED` rows in the live database are `provider_id = 'pool'`, each
        carrying the sentence admitting the provider was never told — and the member that may
        still have been running was never asked. A pool has no upstream id of its own (the attempt
        belongs to a member), so the member rides in the job's recovery context and the pool
        adapter forwards the action to it.
        """
        from gateway import api as api_module
        from gateway.adapters.pool import PoolBackend

        hub = _Hub()
        job_id = self._pool_hold()
        job = self.api.store.get(job_id)
        backend = PoolBackend()
        backend.attach(
            lambda _modality, _member: (
                SimpleNamespace(id="studio-hub-test", kind="studio_hub", base_url=HUB,
                                display_name="Hub"),
                {"model_id": "model/test"},
            ),
            lambda _definition: hub_module.StudioHubBackend(),
        )
        with patch.object(self.api.registry, "definition", return_value=SimpleNamespace(
                    id="pool", kind="pool", base_url="", display_name="Pool")), \
                patch.object(self.api, "adapter_for", return_value=backend), \
                patch.object(self.api.registry.credentials, "resolved",
                             return_value={"hub_token": "t"}), \
                self.hub_client(hub):
            self.assertEqual(api_module.recovery_action(job), "discard_upstream")
            response = self.client.post(f"/admin/api/jobs/{job_id}/discard-uncertain")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["closed"], "upstream")
        self.assertEqual(hub.discards, [f"/api/hub/jobs/{BATCH}/items/0/discard"],
                         "the member that ran the attempt was asked, and only it")
        closed = self.api.store.get(job_id)
        self.assertEqual(closed["error_code"], "DISCARDED_UNCERTAIN")
        self.assertFalse(closed["recovery_required"])
        self.assertNotIn("no discard route", closed["error_detail"],
                         "the pool lane has one now")

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
        upload = _artifact_path("harden.wav")
        upload.write_bytes(b"audio")
        params = {"_upload_path": str(upload), "_upload_name": "same-name.wav", **params}
        return self.generate(hub, self.request(modality="transcription", params=params))

    # F6
    def test_a_transient_artifact_error_is_retried(self) -> None:
        hub = _Hub(artifact_failures=2)
        result = self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        self.assertEqual(result.content, ARTIFACT_BYTES)

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

    def test_a_memory_handback_is_its_own_known_failure_not_a_wait(self) -> None:
        text = "Mac mini 1101 needs about 3.2 GB free and has about 3.0 GB free for m (it refused 3 times)."
        hub = _Hub(states=["error"], item={"state": "error", "error_code": "INSUFFICIENT_MEMORY",
                                           "error": text})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x"}))
        error = caught.exception
        self.assertEqual(error.code, "INSUFFICIENT_MEMORY")
        self.assertIn("needs about 3.2 GB free", str(error))
        self.assertTrue(error.upstream_settled, "the Hub finished with it: nothing is pending")
        self.assertIs(error.upstream_accepted, False)
        self.assertNotEqual(error.code, "LOCATION_NOT_READY",
                            "a LOCATION_* code would make the worker wait on the same Mac")

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
        self.assertEqual(result.content, ARTIFACT_BYTES)

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

    def _poll(self, hub: _Hub, *, deadline: float, waits: list | None = None,
              **kwargs: Any) -> dict[str, Any]:
        clock = _Clock()
        fake = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=clock.time)
        with patch.object(hub_module, "time", fake), self.hub_client(hub), \
                httpx.Client() as client:
            return hub_module._poll_batch(
                client, HUB, {}, BATCH, 0, deadline_seconds=deadline,
                on_wait=(waits.append if waits is not None else None), **kwargs,
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

    def test_running_time_still_bounds_the_job_and_giving_up_cancels_it_on_the_hub(self) -> None:
        # Owner decision 2026-10-02: when the Gateway gives up it sends a real cancel down, so no
        # render is left running with nobody collecting it. (Was: "never DELETE a running item".)
        # A Hub that never says the worker stopped leaves the outcome unknown.
        hub = _Hub(states=["queued"] * 5 + ["running"])
        with patch.object(hub_module, "CANCEL_SETTLE_S", 0.0):
            with self.assertRaises(BackendError) as caught:
                self._poll(hub, deadline=10)
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])

    # A poll that starts mid-render (a Gateway restart resuming the batch) keeps the job's clock.
    def test_a_resumed_job_near_its_deadline_times_out_on_the_hubs_elapsed_not_from_zero(self) -> None:
        hub = _Hub(states=["running"], item={"elapsed_s": 890.0})  # 10 s left of 900
        with patch.object(hub_module, "CANCEL_SETTLE_S", 0.0):
            clock = _Clock()
            with self.assertRaises(BackendError) as caught:
                self._poll_with(clock, hub, deadline=900)
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertLess(clock.now, 30, "counted from the Hub's 890 s, not from a fresh 900 s")

    def test_a_resumed_job_already_past_its_deadline_is_stopped_at_the_first_answer(self) -> None:
        hub = _Hub(states=["running"], item={"elapsed_s": 1500.0})
        with patch.object(hub_module, "CANCEL_SETTLE_S", 0.0):
            clock = _Clock()
            with self.assertRaises(BackendError):
                self._poll_with(clock, hub, deadline=900)
        self.assertLess(clock.now, 5)

    def test_a_fresh_job_is_unaffected_by_the_catch_up(self) -> None:
        hub = _Hub(states=["running"] * 20 + ["done"], item={"elapsed_s": 3.0})
        outcome = self._poll(hub, deadline=100)  # 20 s of running, well inside 100 s
        self.assertEqual(outcome["item"]["state"], "done")
        self.assertEqual(hub.deletes, [])

    def test_a_hub_that_reports_no_elapsed_falls_back_to_the_recorded_dispatch_time(self) -> None:
        hub = _Hub(states=["running"])
        clock = _Clock()
        with patch.object(hub_module, "CANCEL_SETTLE_S", 0.0):
            with self.assertRaises(BackendError):
                # dispatched 890 s before this poll began, no `elapsed_s` anywhere
                self._poll_with(clock, hub, deadline=900, dispatched_at=clock.time() - 890.0)
        self.assertLess(clock.now, 30)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])

    def test_the_dispatch_time_fallback_counts_once_and_not_for_a_fresh_job(self) -> None:
        hub = _Hub(states=["running"] * 5 + ["done"])
        outcome = self._poll(hub, deadline=900, dispatched_at=_Clock().time() - 100.0)
        self.assertEqual(outcome["item"]["state"], "done")  # 100 + 5, not 100 per poll
        fresh = _Hub(states=["running"] * 5 + ["done"])
        self.assertEqual(self._poll(fresh, deadline=900)["item"]["state"], "done")

    def _poll_with(self, clock: _Clock, hub: _Hub, **kwargs: Any) -> dict[str, Any]:
        fake = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=clock.time)
        deadline = kwargs.pop("deadline")
        with patch.object(hub_module, "time", fake), self.hub_client(hub), \
                httpx.Client() as client:
            return hub_module._poll_batch(client, HUB, {}, BATCH, 0,
                                          deadline_seconds=deadline, **kwargs)


    def test_a_batch_queued_past_the_ceiling_is_withdrawn_and_may_be_rerouted(self) -> None:
        hub = _Hub(states=["queued"])
        with patch.object(hub_module, "queue_wait_ceiling_s", lambda: 20.0):
            with self.assertRaises(BackendError) as caught:
                self._poll(hub, deadline=10)
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        self.assertIs(caught.exception.upstream_accepted, False)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])


    def test_the_ceiling_counts_queued_time_only_so_a_late_starter_is_not_withdrawn(self) -> None:
        hub = _Hub(states=["queued"] * 15 + ["running"] * 10 + ["done"])
        with patch.object(hub_module, "queue_wait_ceiling_s", lambda: 20.0):
            outcome = self._poll(hub, deadline=100)  # 25 s of wall time, 15 s of it queued
        self.assertEqual(outcome["item"]["state"], "done")
        self.assertEqual(hub.deletes, [], "a rendering item must never be withdrawn at the ceiling")


class _HoldRegistry(_Registry):
    def definitions(self):
        return [self.definition("studio-hub-test")]

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
        self.assertEqual(Path(job["asset_path"]).read_bytes(), ARTIFACT_BYTES)
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


class RestartResumesInFlightTests(_HubCase):
    """A Gateway restart reconnects to Hub work in flight instead of giving it up.

    Measured 2026-10-03: two Breeze narrations 8 and 30 minutes into rendering ended
    `failed / UNKNOWN_OUTCOME` on a Gateway install, while the Hub kept the batch and its result.
    """

    def _interrupted(self, name: str, *, upstream: str = BATCH, modality: str = "tts") -> tuple[JobStore, str]:
        store = JobStore(_TEMP_ROOT / f"{name}.sqlite3")
        row, _ = store.submit(modality=modality, model_id="model/test", params={"text": "x"},
                              idempotency_key=name, provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))
        self.assertTrue(store.begin_dispatch(row["id"]))
        if upstream:
            store.record_upstream_acceptance(row["id"], upstream)
        store.close()
        reopened = JobStore(store.path)  # the restart
        self.addCleanup(reopened.close)
        return reopened, row["id"]

    def _resume(self, hub: _Hub, store: JobStore, registry: Any = None) -> None:
        worker = worker_module.Worker(store)
        with patch.object(worker_module, "registry", registry or _HoldRegistry()), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), self.hub_client(hub):
            worker._resume_interrupted()
            self.assertTrue(worker.wait_idle(timeout=10))

    def test_an_in_flight_hub_job_is_resumed_and_succeeds(self) -> None:
        store, job_id = self._interrupted("resume-ok")
        self.assertEqual(store.get(job_id)["state"], "running", "a restart must not fail it")
        hub = _Hub(states=["running", "running", "done"])
        self._resume(hub, store)
        job = store.get(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertIsNone(job["error_code"])
        self.assertEqual(Path(job["asset_path"]).read_bytes(), ARTIFACT_BYTES)
        self.assertNotIn(("POST", "/api/hub/jobs"), hub.calls, "resumed, never resubmitted")
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])

    def test_a_resumed_job_that_already_ran_past_its_limit_is_stopped_not_given_a_fresh_budget(self) -> None:
        store, job_id = self._interrupted("resume-late")
        hub = _Hub(states=["running"], item={"elapsed_s": 5000.0})  # far past the 900 s fallback
        seen: list[Any] = []
        real = hub_module.StudioHubBackend.resume

        def spy(backend: Any, request: BackendRequest) -> Any:
            seen.append(request.dispatch_started_at)
            return real(backend, request)

        with patch.object(hub_module, "CANCEL_SETTLE_S", 0.0), \
                patch.object(hub_module.StudioHubBackend, "resume", spy):
            self._resume(hub, store)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"], "the Hub is told to stop it")
        self.assertEqual(seen, [store.get(job_id)["dispatch_started_at"]])
        self.assertIsNotNone(seen[0])

    def test_a_batch_the_hub_does_not_know_is_an_unknown_outcome(self) -> None:
        store, job_id = self._interrupted("resume-404")
        hub = _Hub(poll_faults=[404])
        self._resume(hub, store)
        job = store.get(job_id)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertTrue(job["recovery_required"])
        self.assertIn("no record of batch", job["error_detail"])
        self.assertEqual(job["recovery_context"]["batch_id"], BATCH, "the hold sweep can ask again")
        self.assertNotIn(("POST", "/api/hub/jobs"), hub.calls)

    def test_no_upstream_id_is_re_sent_not_held(self) -> None:
        """0.2.160: a fleet send unanswered at shutdown is re-sent with the same key (see
        `test_restart_resend.py`); a lane the Hub does not dedupe that way keeps the hold."""
        store, job_id = self._interrupted("resume-no-id", upstream="")
        self.assertEqual(store.interrupted, [job_id])
        self.assertEqual(store.get(job_id)["state"], "running")
        store, job_id = self._interrupted("resume-no-id-srt", upstream="", modality="transcription")
        self.assertEqual(store.interrupted, [])
        job = store.get(job_id)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertTrue(job["recovery_required"])

    def test_a_lane_that_cannot_poll_by_id_keeps_the_restart_hold(self) -> None:
        class _Cloud(_HoldRegistry):
            @staticmethod
            def definition(_provider_id: str) -> Any:
                return SimpleNamespace(id="studio-hub-test", kind="openai_compat", base_url=HUB,
                                       display_name="Cloud")

        store, job_id = self._interrupted("resume-cloud", upstream="task-9")
        hub = _Hub()
        self._resume(hub, store, _Cloud())
        job = store.get(job_id)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertEqual(job["recovery_context"]["recorded_by"], "gateway_restart")
        self.assertEqual(hub.calls, [])

    def _past_restart_hold(self, name: str) -> tuple[JobStore, str]:
        """The exact row gateway <= 0.2.102 left behind: `failed / UNKNOWN_OUTCOME`, restart context."""
        store, job_id = self._interrupted(name, modality="tts")
        store.hold_interrupted(job_id)
        held = store.get(job_id)
        self.assertEqual(held["recovery_context"]["recorded_by"], "gateway_restart")
        self.assertIn(f"Upstream job id: {BATCH}", held["error_detail"])
        return store, job_id

    def test_reconcile_adopts_a_past_restart_hold_the_hub_finished(self) -> None:
        store, job_id = self._past_restart_hold("past-restart-done")
        hub = _Hub(states=["done"])
        with patch.object(worker_module, "registry", _HoldRegistry()), self.hub_client(hub):
            out = worker_module.reconcile_holds(store)
        job = store.get(job_id)
        self.assertEqual(out["adopted"], 1)
        self.assertEqual((job["state"], job["error_code"]), ("succeeded", None))
        self.assertFalse(job["recovery_required"])
        self.assertIn(("GET", f"/api/hub/jobs/{BATCH}"), hub.calls, "asked by the upstream id")

    def test_reconcile_settles_a_past_restart_hold_the_hub_failed_truthfully(self) -> None:
        store, job_id = self._past_restart_hold("past-restart-error")
        hub = _Hub(states=["error"], item={"error": "engine out of memory"})
        with patch.object(worker_module, "registry", _HoldRegistry()), self.hub_client(hub):
            out = worker_module.reconcile_holds(store)
        job = store.get(job_id)
        self.assertEqual(out["closed_finished"], 1)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["recovery_required"])
        self.assertEqual(job["error_code"], "PROVIDER_FAILED", "settled, so no longer 'unknown'")
        self.assertIn("engine out of memory", job["error_detail"])

    def test_reconcile_leaves_a_past_restart_hold_the_hub_is_still_running(self) -> None:
        store, job_id = self._past_restart_hold("past-restart-running")
        hub = _Hub(states=["running"])
        with patch.object(worker_module, "registry", _HoldRegistry()), self.hub_client(hub):
            out = worker_module.reconcile_holds(store)
        self.assertEqual(out["still_open"], 1)
        self.assertTrue(store.get(job_id)["recovery_required"])


class OutputFormatTests(_HubCase):
    """`output_format`: the optional request to compress at the Hub (AAC / JPEG).

    Absent is today's request byte for byte; present travels in the Hub item's params, the delivered
    media type decides what the artifact is called, and a node that ignored the field is not an error.
    """

    def _item_params(self, modality: str, params: dict[str, Any]) -> dict[str, Any]:
        hub = _Hub()
        self.generate_then_deliver(hub, self.request(modality=modality, params=params))
        return hub.payloads[0]["items"][0]["params"]

    def test_it_is_forwarded_for_image_music_and_voice(self) -> None:
        self.assertEqual(
            self._item_params("image", {"prompt": "x", "seed": 3, "output_format": "jpeg"}),
            {"seed": 3, "output_format": "jpeg"})
        self.assertEqual(
            self._item_params("music", {"prompt": "x", "duration_seconds": 90, "output_format": "m4a"}),
            {"duration_seconds": 90, "output_format": "m4a"})
        self.assertEqual(
            self._item_params("tts", {"text": "hi", "voice_id": "v", "output_format": "m4a"}),
            {"voice_id": "v", "output_format": "m4a"})

    def test_absent_leaves_the_request_exactly_as_it_was(self) -> None:
        # The golden: no `output_format` key anywhere, so an old Story build and a toggled-off one
        # produce the request every Hub has always received.
        self.assertEqual(self._item_params("image", {"prompt": "x", "seed": 3}), {"seed": 3})
        self.assertEqual(self._item_params("music", {"prompt": "x", "duration_seconds": 90}),
                         {"duration_seconds": 90})
        self.assertEqual(self._item_params("tts", {"text": "hi", "voice_id": "v"}), {"voice_id": "v"})

    def test_a_compressed_artifact_is_recorded_as_delivered(self) -> None:
        hub = _Hub(artifact=M4A_BYTES, artifact_media_type="audio/mp4")
        result = self.generate_then_deliver(
            hub, self.request(modality="music", params={"prompt": "x", "output_format": "m4a"}))
        self.assertEqual(result.media_type, "audio/mp4")
        self.assertEqual(result.payload["output_format_requested"], "m4a")
        self.assertEqual(result.payload["output_format_applied"], "m4a")

    def test_a_node_that_ignored_the_request_still_succeeds_with_the_native_file(self) -> None:
        hub = _Hub(artifact_media_type="audio/wav")
        result = self.generate_then_deliver(
            hub, self.request(modality="tts", params={"text": "hi", "output_format": "m4a"}))
        self.assertEqual(result.media_type, "audio/wav")
        self.assertEqual(result.payload["output_format_requested"], "m4a")
        self.assertEqual(result.payload["output_format_applied"], "wav")
        self.assertEqual(hub.acks, [f"/api/hub/jobs/{BATCH}/items/0/ack"])

    def test_no_request_records_none_requested(self) -> None:
        result = self.generate_then_deliver(
            _Hub(artifact_media_type="image/png"), self.request(modality="image", params={"prompt": "x"}))
        self.assertIsNone(result.payload["output_format_requested"])
        self.assertEqual(result.payload["output_format_applied"], "png")

    def test_media_types_map_to_real_extensions(self) -> None:
        for media_type, ext in {
            "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
            "audio/flac": "flac", "audio/x-flac": "flac", "image/jpeg": "jpg",
            "audio/wav": "wav", "image/png": "png", "audio/mp4; codecs=mp4a.40.2": "m4a",
        }.items():
            self.assertEqual(worker_module._ext_for(media_type), ext, media_type)
        self.assertEqual(worker_module._ext_for("application/x-nothing"), "bin")

    def test_a_cloud_provider_never_sees_it(self) -> None:
        # ElevenLabs would put `m4a` into its own `?output_format=` query and refuse a paid job.
        params = {"text": "hi", "output_format": "m4a", "voice_id": "v"}
        hub_definition = SimpleNamespace(kind="studio_hub")
        cloud_definition = SimpleNamespace(kind="elevenlabs")
        self.assertIs(worker_module._params_for(hub_definition, params), params)
        stripped = worker_module._params_for(cloud_definition, params)
        self.assertNotIn("output_format", stripped)
        self.assertEqual(params["output_format"], "m4a", "the stored job still records the request")
        plain = {"text": "hi"}
        self.assertIs(worker_module._params_for(cloud_definition, plain), plain)

    def test_the_artifact_is_stored_with_its_real_extension(self) -> None:
        store = JobStore(_TEMP_ROOT / "output-format.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(
            modality="music", model_id="model/test", params={"prompt": "x", "output_format": "m4a"},
            idempotency_key="output-format-ext", provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))

        class _M4a:
            def generate(self, request: BackendRequest) -> BackendResult:
                self.seen = dict(request.params)
                return BackendResult(content=M4A_BYTES, media_type="audio/mp4",
                                     payload={"hub_batch_id": BATCH})

        class _HubRegistry(_Registry):
            @staticmethod
            def definition(_provider_id: str) -> Any:
                return SimpleNamespace(id="studio-hub-test", kind="studio_hub", base_url=HUB,
                                       display_name="Hub")

        adapter = _M4a()
        with patch.object(worker_module, "registry", _HubRegistry()), patch.object(
            worker_module, "adapter_for", return_value=adapter
        ):
            worker_module.execute_job(row, store)
        job = store.get(row["id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertTrue(job["asset_path"].endswith(f"{row['id']}.m4a"), job["asset_path"])
        self.assertEqual(adapter.seen["output_format"], "m4a")


class OutputFormatSubmitTests(unittest.TestCase):
    """Unknown values are refused with 400 before anything is queued; good ones are normalised."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        self.api = api
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": f"ofmt-{self.id()}"}

    def tearDown(self) -> None:
        self.client.close()

    ROUTES = {
        "image": "/v1/images/generations",
        "music": "/v1/music/generations",
        "tts": "/v1/audio/jobs",
    }

    def _post(self, modality: str, extra: dict[str, Any]) -> Any:
        body = {"model_id": "model/test", "prompt": "x", "text": "x", **extra}
        return self.client.post(self.ROUTES[modality], json=body, headers=self.headers)

    def test_unknown_values_are_a_400_for_every_modality(self) -> None:
        bad = {
            "image": ["webp", "m4a", "", 7, ["png"]],
            # `flac` is no longer unknown to a music request: a Studio Hub music lane delivers it
            # (YuE2's native file is FLAC), so the music door admits it. It is asserted good below.
            "music": ["mp3", "jpeg", 1],
            "tts": ["mp3", "png", "flac", "mp3_44100_128"],
        }
        for modality, values in bad.items():
            for value in values:
                with self.subTest(modality=modality, value=value):
                    response = self._post(modality, {"output_format": value})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("INVALID_PARAMS", response.text)
                    self.assertIn("output_format", response.text)

    def test_good_values_reach_the_queue_normalised(self) -> None:
        seen: list[tuple[str, dict[str, Any]]] = []

        def fake_submit(modality: str, _model: str, params: dict[str, Any], *_a: Any, **_k: Any):
            seen.append((modality, dict(params)))
            return {"id": "job_x", "modality": modality, "state": "accepted"}, False

        with patch.object(self.api, "_submit", side_effect=fake_submit), patch.object(
            self.api, "_job_envelope", side_effect=lambda job: job
        ):
            for modality, value, expected in [
                ("image", "jpeg", "jpeg"), ("image", " PNG ", "png"),
                ("music", "m4a", "m4a"), ("music", "wav", "wav"), ("music", "flac", "flac"),
                ("tts", "M4A", "m4a"), ("tts", "wav", "wav"),
            ]:
                with self.subTest(modality=modality, value=value):
                    self.assertEqual(self._post(modality, {"output_format": value}).status_code, 200)
                    self.assertEqual(seen[-1], (modality, {
                        "model_id": "model/test", "prompt": "x", "text": "x", "output_format": expected}))

    def test_a_lane_declared_format_is_admitted_even_off_the_flat_list(self) -> None:
        """**The row the catalogue publishes is the authority on the door's vocabulary.**

        The lane states its own `output_format` enum; the catalogue relays it onto the row, so a
        format the lane publishes must be admitted even when it is not in the modality's flat list.
        This is the seam that let a YuE2 row advertise `flac` while the door answered 400.
        """
        lane = SimpleNamespace(id="studio-hub-test", kind="studio_hub")
        raw = {"lane_contract": {"parameters": [
            {"name": "output_format", "type": "string", "required": False,
             "enum": ["opus", "m4a"], "request_only": True}]}}
        seen: list[tuple[str, dict[str, Any]]] = []

        def fake_submit(modality: str, _model: str, params: dict[str, Any], *_a: Any, **_k: Any):
            seen.append((modality, dict(params)))
            return {"id": "job_x", "modality": modality, "state": "accepted"}, False

        with patch.object(self.api.catalog, "resolve",
                          side_effect=lambda *_a, **_k: (lane, {"model_id": "m", "raw": raw})), \
                patch.object(self.api, "_submit", side_effect=fake_submit), \
                patch.object(self.api, "_job_envelope", side_effect=lambda job: job):
            self.assertEqual(self._post("music", {"output_format": " OPUS "}).status_code, 200)
            self.assertEqual(seen[-1][1]["output_format"], "opus")
            # A lane that states nothing still gets the flat list, and nothing else.
            with patch.object(self.api.catalog, "resolve", side_effect=lambda *_a, **_k: (lane, None)):
                self.assertEqual(self._post("music", {"output_format": "opus"}).status_code, 400)

    def test_absent_and_null_are_the_same_native_request(self) -> None:
        seen: list[dict[str, Any]] = []

        def fake_submit(_modality: str, _model: str, params: dict[str, Any], *_a: Any, **_k: Any):
            seen.append(dict(params))
            return {"id": "job_x", "modality": "image", "state": "accepted"}, False

        with patch.object(self.api, "_submit", side_effect=fake_submit), patch.object(
            self.api, "_job_envelope", side_effect=lambda job: job
        ):
            self._post("image", {})
            self._post("image", {"output_format": None})
        self.assertEqual(seen[0], seen[1])
        self.assertNotIn("output_format", seen[0])


class _Body(httpx.SyncByteStream):
    """A Hub audio body that counts what it handed out and whether it was closed."""

    def __init__(self, chunks: list[bytes], *, endless: bool = False, fail: bool = False) -> None:
        self.chunks, self.endless, self.fail = chunks, endless, fail
        self.sent = self.closed = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.sent += len(chunk)
            yield chunk
        if self.fail:
            raise httpx.ReadError("the Hub dropped the connection")
        while self.endless:
            self.sent += 4096
            yield b"x" * 4096

    def close(self) -> None:
        self.closed += 1


class ReviewCandidateAdapterTests(_HubCase):
    """A refused narration section is bound by its verified locator, never by a guess."""

    def refused(self, code: str = "TTS_SECTION_SILENT") -> _Hub:
        return _Hub(states=["error"], item={"state": "error", "error": SECTION_REFUSAL,
                                            "error_code": code})

    def failure(self, hub: _Hub, modality: str = "tts") -> BackendError:
        with self.assertRaises(BackendError) as raised:
            self.generate(hub, self.request(modality=modality, params={"text": "x"}))
        return raised.exception

    def test_a_refused_section_carries_its_verified_locator_and_the_same_failure(self) -> None:
        hub = self.refused()
        hub.review = review_answer()
        exc = self.failure(hub)
        self.assertEqual(exc.code, "PROVIDER_FAILED")
        self.assertTrue(exc.upstream_settled)
        self.assertEqual(str(exc), f"hub item error: TTS_SECTION_SILENT: {SECTION_REFUSAL}")
        self.assertEqual(exc.evidence["hub_batch_id"], BATCH)
        self.assertEqual(exc.review_candidate, {
            "batch_id": BATCH, "item_index": 0,
            "receipt_sha256": review_answer()["receipt"]["receipt_sha256"],
            "pcm_bytes": len(REVIEW_PCM), "pcm_sha256": REVIEW_SHA})

    def test_nothing_is_bound_unless_the_hub_proves_an_inactive_candidate(self) -> None:
        tampered = review_answer()
        tampered["receipt"]["section"]["text"] = "other words"
        for name, answer in (
            ("none", None), ("tampered", tampered), ("active", review_answer(active=True)),
            ("deliverable", review_answer(deliverable=True)),
            ("other batch", review_answer(batch="other-batch")),
            ("not a wav", review_answer(pcm=b"")),
        ):
            with self.subTest(name):
                hub = self.refused()
                hub.review = answer
                exc = self.failure(hub)
                self.assertEqual(exc.code, "PROVIDER_FAILED")
                self.assertIsNone(exc.review_candidate)

    def test_only_a_section_quality_failure_asks_for_a_candidate(self) -> None:
        hub = self.refused(code="MODEL_PAUSED")
        hub.review = review_answer()
        self.assertIsNone(self.failure(hub).review_candidate)
        self.assertNotIn(("GET", f"/api/hub/jobs/{BATCH}/items/0/review-candidate"), hub.calls)

    def run_job(self, hub: _Hub, store: JobStore, key: str, moves: list[Any]) -> dict[str, Any]:
        row, _ = store.submit(modality="tts", model_id="model/test", params={"text": "x"},
                              idempotency_key=key, provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))
        with patch.object(worker_module, "registry", _Registry()), patch.object(
            worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
        ), patch.object(worker_module, "_next_machine", side_effect=moves), self.hub_client(hub):
            worker_module.execute_job(row, store)
        return row

    def test_the_final_failure_binds_the_locator_with_the_job_in_one_commit(self) -> None:
        hub = self.refused()
        hub.review = review_answer()
        store = JobStore(_TEMP_ROOT / "review-bind.sqlite3")
        try:
            row = self.run_job(hub, store, "review-bind", [None])
            failed = store.get(row["id"])
            self.assertEqual((failed["state"], failed["error_code"]), ("failed", "PROVIDER_FAILED"))
            self.assertIn(SECTION_REFUSAL, failed["error_detail"])
            locator = store.review_locator(row["id"])
            self.assertEqual({key: locator[key] for key in ("provider_id", "batch_id", "item_index",
                                                             "pcm_bytes", "pcm_sha256")},
                             {"provider_id": "studio-hub-test", "batch_id": BATCH, "item_index": 0,
                              "pcm_bytes": len(REVIEW_PCM), "pcm_sha256": REVIEW_SHA})
            self.assertIsNone(failed.get("artifacts_json"))
        finally:
            store.close()

    def test_an_attempt_that_moved_on_to_another_location_binds_nothing(self) -> None:
        answers = iter([review_answer(text="first location"), review_answer(text="second location")])
        hub = self.refused()
        hub.review = lambda: next(answers)
        store = JobStore(_TEMP_ROOT / "review-reroute.sqlite3")
        try:
            row = self.run_job(hub, store, "review-reroute", ["studio-hub-second", None])
            locator = store.review_locator(row["id"])
            self.assertEqual(locator["provider_id"], "studio-hub-second")
            self.assertEqual(locator["receipt_sha256"],
                             review_answer(text="second location")["receipt"]["receipt_sha256"])
        finally:
            store.close()


class ReviewCandidateAudioTests(unittest.TestCase):
    """The admin playback body: read at most one byte past the receipt, and proved before use."""

    LOCATOR = {"provider_id": "studio-hub-test", "batch_id": BATCH, "item_index": 0,
               "receipt_sha256": review_answer()["receipt"]["receipt_sha256"],
               "pcm_bytes": len(REVIEW_PCM), "pcm_sha256": REVIEW_SHA}

    def read(self, body: _Body, headers: list[tuple[str, str]] | None = None, *,
             meta: dict[str, Any] | None = None, values: dict[str, str] | None = None):
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            if request.url.path.endswith("/review-candidate"):
                return httpx.Response(200, json=meta or review_answer())
            return httpx.Response(200, stream=body, headers=headers if headers is not None else [
                ("content-type", "audio/wav"), ("content-length", str(len(REVIEW_PCM))),
                ("x-content-sha256", REVIEW_SHA)])

        real = httpx.Client

        def factory(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(hub_module.httpx, "Client", side_effect=factory):
            try:
                return hub_module.review_candidate_audio(
                    values if values is not None else {"hub_token": "site-token"},
                    HUB, self.LOCATOR, local=False), sent
            except hub_module.ReviewCandidateUnavailable as exc:
                return exc, sent

    def test_exact_bytes_are_returned_and_every_hop_closed(self) -> None:
        body = _Body([REVIEW_PCM[:7], REVIEW_PCM[7:]])
        answer, sent = self.read(body)
        self.assertEqual(answer, REVIEW_PCM)
        self.assertEqual(body.closed, 1)
        self.assertEqual([request.method for request in sent], ["GET", "GET"])
        self.assertEqual(sent[0].headers["x-hub-token"], "site-token")

    def test_a_body_that_differs_anywhere_is_never_returned(self) -> None:
        flipped = REVIEW_PCM[:-1] + bytes([REVIEW_PCM[-1] ^ 1])
        typed = [("content-type", "audio/wav"), ("content-length", str(len(REVIEW_PCM)))]
        cases = {
            "one byte over": (_Body([REVIEW_PCM, b"x"]), None),
            "endless": (_Body([REVIEW_PCM], endless=True), None),
            "early end": (_Body([REVIEW_PCM[:-1]]), None),
            "same length, other bytes": (_Body([flipped]), None),
            "dropped mid-body": (_Body([REVIEW_PCM[:5]], fail=True), None),
            "other type": (_Body([REVIEW_PCM]), [("content-type", "audio/mpeg"),
                                                 ("content-length", str(len(REVIEW_PCM))),
                                                 ("x-content-sha256", REVIEW_SHA)]),
            "no digest": (_Body([REVIEW_PCM]), typed),
            "two digests": (_Body([REVIEW_PCM]), typed + [("x-content-sha256", REVIEW_SHA)] * 2),
            "other digest": (_Body([REVIEW_PCM]), typed + [("x-content-sha256", "0" * 64)]),
            "other length": (_Body([REVIEW_PCM]), [("content-type", "audio/wav"),
                                                   ("content-length", str(len(REVIEW_PCM) + 1)),
                                                   ("x-content-sha256", REVIEW_SHA)]),
        }
        for name, (body, headers) in cases.items():
            with self.subTest(name):
                answer, _sent = self.read(body, headers)
                self.assertIsInstance(answer, hub_module.ReviewCandidateUnavailable)
                self.assertEqual(answer.status, 502)
                self.assertEqual(body.closed, 1)
                self.assertLessEqual(body.sent, len(REVIEW_PCM) + 4096)

    def test_a_receipt_that_is_not_the_bound_one_reads_no_audio(self) -> None:
        body = _Body([REVIEW_PCM])
        answer, sent = self.read(body, meta=review_answer(text="another take"))
        self.assertEqual((answer.status, len(sent), body.sent), (502, 1, 0))

    def test_a_remote_location_without_a_current_credential_is_not_asked(self) -> None:
        answer, sent = self.read(_Body([REVIEW_PCM]), values={})
        self.assertEqual((answer.status, sent), (503, []))


class ImageReferenceDispatchTests(_HubCase):
    """"Reference to Image": the references reach the Hub, and a dropped one fails the item.

    The Hub's broker builds an item from exactly `prompt`, `seed` and `params`, then pops
    `reference_images` off the *body* — so a reference beside `params` is dropped without a word and
    the worker renders a prompt with no characters in it (the 2026-09-26 video regression, in its
    image spelling). The Hub states `reference_count` back precisely so this side can tell a
    reference render from a text-to-image one that looks identical in the envelope.
    """

    def staged(self, count: int = 2) -> dict:
        # Inside the artifact store, which is the only place the Gateway stages a reference
        # and the only place the adapters will read one (review 2026-10-28).
        artifacts = Path(os.environ["GATEWAY_ARTIFACTS"])
        artifacts.mkdir(parents=True, exist_ok=True)
        directory = artifacts / f"refs-{time.time_ns()}"
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(count):
            path = directory / f"reference{index}.png"
            path.write_bytes(PNG_BYTES + bytes([index]))
            paths.append(str(path))
        return {"_reference_paths": paths, "_reference_types": ["image/png"] * count,
                "_reference_digests": [f"digest{index}" for index in range(count)]}

    def test_the_references_ride_inside_params_as_base64_entries_in_order(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png",
                   item={"reference_count": 2})
        params = {"prompt": "two characters at a night market", **self.staged(2)}
        result = self.generate_then_deliver(hub, self.request(modality="image", params=params))
        item = hub.payloads[0]["items"][0]
        self.assertNotIn("reference_images", item,
                         "an item-level key is dropped by the broker without a word")
        references = item["params"]["reference_images"]
        self.assertEqual(len(references), 2)
        self.assertEqual([entry["mime"] for entry in references], ["image/png", "image/png"])
        self.assertEqual(base64.b64decode(references[0]["b64"]), PNG_BYTES + b"\x00")
        self.assertEqual(base64.b64decode(references[1]["b64"]), PNG_BYTES + b"\x01",
                         "order is the caller's; the worker conditions on them in sequence")
        self.assertEqual(result.payload["reference_count"], 2)

    def test_a_prompt_only_image_job_carries_no_reference_key(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png")
        result = self.generate_then_deliver(
            hub, self.request(modality="image", params={"prompt": "a cat"}))
        self.assertNotIn("reference_images", hub.payloads[0]["items"][0]["params"])
        self.assertIsNone(result.payload["reference_count"])

    def test_a_reference_count_mismatch_fails_the_item_and_names_both_numbers(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png",
                   item={"reference_count": 1})
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(
                modality="image", params={"prompt": "two of them", **self.staged(2)}))
        self.assertEqual(caught.exception.code, "PROVIDER_RESULT_INVALID")
        self.assertIn("2 reference images were sent", str(caught.exception))
        self.assertIn("applied 1", str(caught.exception))

    def test_a_hub_that_states_no_count_does_not_prove_the_references_were_used(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png")
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(
                modality="image", params={"prompt": "two of them", **self.staged(2)}))
        self.assertEqual(caught.exception.code, "PROVIDER_RESULT_INVALID")
        self.assertIn("without stating how many", str(caught.exception))

    def test_a_mismatched_count_fails_before_the_artifact_is_downloaded(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png",
                   item={"reference_count": 1})
        with self.assertRaises(BackendError):
            self.generate(hub, self.request(
                modality="image", params={"prompt": "two of them", **self.staged(2)}))
        self.assertFalse([call for call in hub.calls if call[1].endswith("/artifact")],
                         "a wrong render is refused, not downloaded and served")

    def test_a_staged_reference_that_is_gone_is_refused_before_anything_is_sent(self) -> None:
        hub = _Hub(artifact=PNG_BYTES, artifact_media_type="image/png",
                   item={"reference_count": 1})
        params = self.staged(1)
        Path(params["_reference_paths"][0]).unlink()
        with self.assertRaises(BackendError) as caught:
            self.generate(hub, self.request(modality="image", params={"prompt": "x", **params}))
        self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.assertIn("no longer held", str(caught.exception))
        self.assertEqual(hub.payloads, [], "nothing was submitted with a missing reference")


if __name__ == "__main__":
    unittest.main(verbosity=2)
