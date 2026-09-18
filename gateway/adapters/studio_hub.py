"""Studio Hub adapter — the local Apple Silicon lane.

Routes image, voice and transcription work to the Hub on this Mac.

Two things about the Hub shape this code:

  * It is genuinely asynchronous (batch id + item index + poll + artifact), so
    this adapter polls internally until the item is terminal. That is why the
    gateway executes jobs on a worker thread rather than inside the HTTP request
    -- a five-minute image job must not hold a Story Studio poll open.
  * Transcription uses a *different* route (`/api/hub/transcription/jobs`,
    multipart) from image/voice (`/api/hub/jobs`, JSON).

Errors are normalised onto the same vocabulary as the remote adapters so Story
Studio's retry logic behaves identically regardless of where work ran.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

POLL_INTERVAL_S = 1.0
# Aligned with the Hub's own claim TTL floor (request timeout 300s + margin).
JOB_DEADLINE_S = 900.0

# Hub item states -> our terminal interpretation.
_TERMINAL_OK = {"done", "succeeded", "complete", "completed"}
_TERMINAL_BAD = {"error", "failed", "cancelled", "cancelled_items", "uncertain"}

_HUB_MODALITY = {
    "image": "image",
    "tts": "voice",
}


def _resolve_base(request: BackendRequest) -> str:
    from ..hub import hub_base_url

    base = (request.values.get("base_url") or request.base_url or hub_base_url()).strip()
    if not base:
        raise BackendError(
            "Studio Hub is not running (no hub-ready.json found)",
            code="SITE_CAPACITY_UNAVAILABLE",
            retryable=True,
        )
    return base.rstrip("/")


def _headers(request: BackendRequest) -> dict[str, str]:
    from ..hub import hub_headers

    return hub_headers(request.values.get("hub_token", ""))


def _poll_batch(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    jobs_path: str = "/api/hub/jobs",
) -> dict[str, Any]:
    """Poll a Hub batch to a terminal state.

    `jobs_path` differs by modality: image and voice share `/api/hub/jobs`,
    while transcription batches live under `/api/hub/transcription/jobs` and
    are *not* visible from the other route -- polling the wrong one returns 404
    rather than a useful error.
    """
    deadline = time.monotonic() + JOB_DEADLINE_S
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{base}{jobs_path}/{batch_id}", headers=headers)
        if response.status_code >= 400:
            raise BackendError(
                f"hub status HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )
        summary = response.json() or {}
        last = summary

        items = summary.get("items") or []
        item = items[item_index] if item_index < len(items) else None
        state = str((item or {}).get("state") or summary.get("state") or "").lower()

        if state in _TERMINAL_OK:
            return {"summary": summary, "item": item or {}}
        if state in _TERMINAL_BAD:
            error = (item or {}).get("error") or summary.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise BackendError(
                f"hub item {state}: {message or 'no detail'}",
                code="IMAGE_FAILED" if item is None else "PROVIDER_FAILED",
            )
        time.sleep(POLL_INTERVAL_S)

    raise BackendError(
        f"hub job did not finish within {int(JOB_DEADLINE_S)}s",
        code="PROVIDER_SUBMISSION_UNCERTAIN",
        retryable=True,
    )


def _artifact_url(base: str, batch_id: str, item_index: int, kind: str) -> str:
    if kind == "transcription":
        return f"{base}/api/hub/transcription/jobs/{batch_id}/items/{item_index}/artifact"
    return f"{base}/api/hub/jobs/{batch_id}/items/{item_index}/artifact"


# Staged voice references, keyed by voice id. The Hub gives an asset a TTL
# (default 24h), so re-staging on every generation would waste work and disk;
# refreshing well inside that window is enough.
#: Staged voice references, keyed by **(voice, controller)**.
#:
#: The asset id is minted by the controller that received the upload, so it is
#: only meaningful there. Keyed by voice alone — which it was — a reference staged
#: on one location was handed to the next location that asked for the same voice,
#: and that location had never seen the asset. Invisible with a single location,
#: which is why it survived; with several it fails the moment a second controller
#: reuses a cached voice.
_REFERENCE_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_REFERENCE_TTL_S = 6 * 3600.0


def _reference_cache_key(voice_id: str, base: str) -> tuple[str, str]:
    """Cache identity for a staged reference: the voice *and* the location."""
    return (voice_id, base.rstrip("/"))


def _reference_for(request: BackendRequest) -> dict[str, Any] | None:
    """The clone reference for a voice, looked up in the voice registry.

    Story Studio sends back the compound id it was given, so the registry is the
    single place that knows how to turn that into a local clip.
    """
    voice_id = str(request.params.get("voice_id") or "")
    if not voice_id:
        return None
    from ..voices import resolve  # local import: avoids a cycle

    voice = resolve(voice_id)
    if voice is None:
        return None
    raw = voice.raw or {}
    path = str(raw.get("reference_path") or "")
    if not path:
        return None
    return {
        "path": path,
        "transcript": str(raw.get("reference_transcript") or ""),
        "segments": raw.get("reference_segments") or [],
        "name": voice.display_name,
    }


def _stage_voice_reference(
    client: httpx.Client, base: str, headers: dict[str, str], voice_id: str, reference: dict[str, Any]
) -> str:
    """Upload a voice's reference clip and return the Hub's asset id.

    Qwen3 *Base* models are cloning models: without a reference they fail with
    "Voice cloning needs a reference voice or private reference upload". So the
    clip is staged first and passed as `voice_reference_asset_id`.
    """
    key = _reference_cache_key(voice_id, base)
    cached = _REFERENCE_CACHE.get(key)
    if cached and (time.time() - cached[1]) < _REFERENCE_TTL_S:
        return cached[0]

    path = Path(str(reference.get("path") or ""))
    if not path.exists():
        raise BackendError(
            f"voice reference audio is missing for '{voice_id}'",
            code="VOICE_PROVIDER_RESULT_INVALID",
        )

    files = {"audio": (path.name, path.read_bytes(), "application/octet-stream")}
    # `transcript_segments` is not optional for Qwen3 Base: the worker refuses a
    # reference without aligned timestamps
    # (REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED). They are generated by
    # scripts/annotate_voice_segments.py.
    segments = reference.get("segments") or []
    data = {
        "transcript": str(reference.get("transcript") or ""),
        "transcript_segments": json.dumps(segments) if segments else "",
    }
    try:
        response = client.post(
            f"{base}/api/hub/voice-references", headers=headers, files=files, data=data
        )
    except httpx.HTTPError as exc:
        raise BackendError(
            f"voice reference upload failed: {exc}",
            code="SITE_CAPACITY_UNAVAILABLE",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        raise BackendError(
            f"hub voice-reference HTTP {response.status_code}: {response.text[:200]}",
            code="VOICE_PROVIDER_RESULT_INVALID",
            status=response.status_code,
            retryable=response.status_code >= 500,
        )

    asset_id = str((response.json() or {}).get("asset_id") or "")
    if not asset_id:
        raise BackendError(
            "hub returned no voice reference asset_id", code="VOICE_PROVIDER_RESULT_INVALID"
        )

    _REFERENCE_CACHE[key] = (asset_id, time.time())
    return asset_id


def _generate_image_or_voice(request: BackendRequest) -> BackendResult:
    base = _resolve_base(request)
    headers = _headers(request)
    modality = _HUB_MODALITY[request.modality]

    item: dict[str, Any] = {"params": request.params.get("params") or {}}
    if request.modality == "image":
        item["prompt"] = str(request.params.get("prompt") or "")
        for key in ("seed", "width", "height", "aspect_ratio", "resolution"):
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]
    else:
        item["text"] = str(request.params.get("text") or "")
        for key in ("speed", "voice_id", "language", "controls", "temperature", "seed"):
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]

    body = {
        "modality": modality,
        "model": request.model_id,
        "items": [item],
        "clientRequestId": str(request.params.get("_job_id") or ""),
        "label": "gateway-kh",
    }

    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        if request.modality == "tts":
            voice_id = str(request.params.get("voice_id") or "")
            reference = _reference_for(request)
            if reference is not None:
                body["voice_reference_asset_id"] = _stage_voice_reference(
                    client, base, headers, voice_id, reference
                )
        try:
            response = client.post(f"{base}/api/hub/jobs", headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise BackendError(
                f"hub submit failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise BackendError(
                f"hub submit HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )

        submitted = response.json() or {}
        batch_id = str(submitted.get("batch_id") or "")
        if not batch_id:
            raise BackendError("hub returned no batch_id", code="PROVIDER_FAILED")

        outcome = _poll_batch(client, base, headers, batch_id, 0)

        artifact = client.get(
            _artifact_url(base, batch_id, 0, request.modality),
            headers=headers,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        if artifact.status_code >= 400:
            raise BackendError(
                f"hub artifact HTTP {artifact.status_code}",
                code="PROVIDER_FAILED",
                status=artifact.status_code,
                retryable=artifact.status_code >= 500,
            )

    media_type = artifact.headers.get("content-type", "").split(";")[0].strip()
    if not media_type:
        media_type = "image/png" if request.modality == "image" else "audio/wav"

    terminal = (outcome.get("item") or {}).get("terminal_result") or {}
    return BackendResult(
        content=artifact.content,
        media_type=media_type,
        payload={
            "hub_batch_id": batch_id,
            "hub_machine": terminal.get("machine_id"),
            "hub_worker": terminal.get("worker_id"),
            "resolved_seed": terminal.get("resolved_seed"),
            "sha256": terminal.get("sha256"),
            "bytes": terminal.get("bytes"),
        },
    )


def _generate_transcription(request: BackendRequest) -> BackendResult:
    base = _resolve_base(request)
    headers = _headers(request)

    audio_path = request.params.get("_upload_path")
    if not audio_path or not Path(audio_path).exists():
        raise BackendError("transcription needs an uploaded audio file", code="PROVIDER_FAILED")
    audio = Path(audio_path).read_bytes()

    # Field names are `files` / `item_ids`, NOT `files[]` / `item_ids[]`. The
    # Hub declares `files: list[UploadFile]` and `item_ids: list[str]` as
    # repeated form fields, so a bracketed name is rejected with a bare 422.
    files = {
        "files": (
            request.params.get("_upload_name") or "audio.mp3",
            audio,
            request.params.get("_upload_type") or "application/octet-stream",
        )
    }
    data: dict[str, str] = {"model": request.model_id, "item_ids": "0"}
    if request.params.get("language"):
        data["language"] = str(request.params["language"])

    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        try:
            response = client.post(
                f"{base}/api/hub/transcription/jobs", headers=headers, files=files, data=data
            )
        except httpx.HTTPError as exc:
            raise BackendError(
                f"hub transcription submit failed: {exc}",
                code="SITE_CAPACITY_UNAVAILABLE",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise BackendError(
                f"hub transcription HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )

        submitted = response.json() or {}
        batch_id = str(submitted.get("batch_id") or "")
        if not batch_id:
            raise BackendError("hub returned no batch_id", code="PROVIDER_FAILED")

        outcome = _poll_batch(
            client, base, headers, batch_id, 0, jobs_path="/api/hub/transcription/jobs"
        )

        artifact = client.get(
            _artifact_url(base, batch_id, 0, "transcription") + "?format=srt",
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        if artifact.status_code >= 400:
            raise BackendError(
                f"hub transcript artifact HTTP {artifact.status_code}",
                code="PROVIDER_FAILED",
                status=artifact.status_code,
            )

    return BackendResult(
        content=artifact.content,
        media_type="application/x-subrip",
        payload={"hub_batch_id": batch_id},
    )


class StudioHubBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality in {"image", "tts"}:
            return _generate_image_or_voice(request)
        if request.modality == "transcription":
            return _generate_transcription(request)
        raise BackendError(
            f"Studio Hub does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )
