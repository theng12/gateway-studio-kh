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

import hashlib
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
    "music": "music",
}

#: What the Hub's Music item accepts, per its two engines.
#:
#: ACE-Step/Ace-Step1.5 sings and does long form and takes `lyrics`,
#: `vocal_language`, `instrumental` and `shift`; stabilityai/stable-audio-3-optimized
#: is instrumental only and takes the rest. The gateway forwards whichever of these
#: the caller set and invents none of them — which engine accepts what is the
#: engine's own answer, and a Hub refusal names the field far better than a
#: default guessed here.
_HUB_MUSIC_PARAMS = (
    "duration_seconds",
    "steps",
    "guidance_scale",
    "shift",
    "seed",
    "instrumental",
    "lyrics",
    "vocal_language",
)


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
    from ..fleet import LOCAL_ID, provider_id_for
    from ..hub import hub_headers

    # `X-Hub-Token` carries the *called provider's* credential. Only this Mac's
    # own Hub has none stored — its operator token is its credential — so only
    # there is the local fallback correct. A remote location's absent credential
    # must stay absent: `hub_headers` used to fall back unconditionally, which
    # put this Mac's operator token in this header and sent it to the address the
    # owner had typed into the Fleet page.
    return hub_headers(
        request.values.get("hub_token", ""),
        local=request.provider_id == provider_id_for(LOCAL_ID),
    )


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


def _jobs_path(kind: str) -> str:
    """Where a modality's batches live.

    Image, voice and music share `/api/hub/jobs`; transcription has its own route
    and its batches are *not* visible from the other one — polling, fetching an
    artifact or cancelling on the wrong path answers 404 rather than a useful
    error. Every route for a modality is built from this one answer so they cannot
    disagree.
    """
    return "/api/hub/transcription/jobs" if kind == "transcription" else "/api/hub/jobs"


def _artifact_url(base: str, batch_id: str, item_index: int, kind: str) -> str:
    return f"{base}{_jobs_path(kind)}/{batch_id}/items/{item_index}/artifact"


def _ack_delivery(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    kind: str,
) -> dict[str, Any]:
    """Assert to the Hub that one delivered artifact was verified received.

    The Hub marks its output receipt and index protection only after a 2xx from
    this route, and the route is deliberately *not* reachable by downloading an
    artifact: it is the caller's assertion that it has the bytes. That is why the
    adapter hands this to the worker as a callback instead of calling it here —
    the bytes are only really delivered once they are on disk.

    Never raises. A refusal or an unreachable Hub is recorded and reported, not
    turned into a failed generation: the artifact is already saved and serveable,
    and losing it because a receipt was refused would be the worse outcome.
    """
    route = f"{_jobs_path(kind)}/{batch_id}/items/{item_index}/ack"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.post(f"{base}{route}", headers=headers)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "route": route,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        answer: Any = response.json()
    except ValueError:
        answer = response.text[:200]
    return {
        "ok": 200 <= response.status_code < 300,
        "status": response.status_code,
        "route": route,
        "answer": answer,
    }


def _cancel_batch(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    kind: str,
) -> dict[str, Any]:
    """Ask the Hub to stop a dispatched batch, and report what it answered.

    The Hub's own answer is the only evidence there is: it cancels what is still
    queued, *signals* what is already running, and cannot promise a worker that
    has accepted the work will stop. So the fields are recorded verbatim —
    `queued_cancelled`, `running_signalled`, `running_pending` — rather than
    flattened into a boolean, because "we asked and nothing was running to stop"
    and "we asked and a worker was told" are different facts.

    Never raises, for the same reason as the receipt: the caller is recording an
    outcome, and a cancel the Hub could not confirm is exactly the case that must
    not be reported as a confirmed stop.
    """
    route = f"{_jobs_path(kind)}/{batch_id}"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.request("DELETE", f"{base}{route}", headers=headers)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "route": route,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        answer: Any = response.json()
    except ValueError:
        answer = response.text[:200]
    return {
        "ok": 200 <= response.status_code < 300,
        "status": response.status_code,
        "route": route,
        "answer": answer,
    }


# Voice placement, keyed by **(voice, controller)**.
#:
#: The value is `(kind, id, at)`: either the controller's own library voice or an
#: asset staged for one attempt. Keyed by voice alone — which it was — a reference
#: staged on one location was handed to the next location that asked for the same
#: voice, and that location had never seen the asset. Invisible with a single
#: location, which is why it survived; with several it fails the moment a second
#: controller reuses a cached voice.
_REFERENCE_CACHE: dict[tuple[str, str, str], tuple[str, str, float]] = {}
_REFERENCE_TTL_S = 6 * 3600.0


def _remember_reference(key: tuple[str, str, str], kind: str, value: str) -> None:
    """Store a placement and drop the ones that have expired.

    The key carries the clip's digest, so every re-import of a voice adds a key
    whose predecessors are never asked for again. Without this they would sit in
    the dict for the process's life.

    `ponytail:` a full sweep per write, which is a handful of entries per voice per
    location. Above a few thousand voices this wants a heap or an ordered dict.
    """
    now = time.time()
    for stale in [k for k, v in _REFERENCE_CACHE.items() if (now - v[2]) >= _REFERENCE_TTL_S]:
        _REFERENCE_CACHE.pop(stale, None)
    _REFERENCE_CACHE[key] = (kind, value, now)


def _reference_cache_key(voice_id: str, base: str, digest: str) -> tuple[str, str, str]:
    """Cache identity for a voice placement: the voice, the location, **and the bytes**.

    The digest is the third part because a re-import keeps the voice's id and
    changes its audio. Keyed on `(voice_id, base)` — which is what this was — the
    controller's **old recording** kept being used for up to six hours, and nothing
    said so: the placement asked for that voice and got a confident answer about a
    clip that no longer exists. It is the stale build stamp again, one layer down.

    The digest is already the thing the library match is made on, so a placement
    cached for one recording must not answer for another.
    """
    return (voice_id, base.rstrip("/"), digest)


def _clip_digest(path: Path) -> str:
    """The sha256 of a clip's bytes.

    The same notion as `voice_import.clip_digest` and `shared_voice_id`, and
    deliberately not imported from there: an adapter depends on its definition and
    its base, not on the voice domain. Three one-line readers of the same fact is
    the price of that direction; what must not diverge is the *definition*, and
    "sha256 of the file, lowercase hex" is not a thing that can be interpreted
    two ways.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _library_voice_for(
    client: httpx.Client, base: str, headers: dict[str, str], digest: str
) -> str:
    """The controller's own voice id for a clip it already holds, or "".

    The library carries `audio_sha256`, which the Studio Hub session verified is
    the sha256 of the stored reference file byte-for-byte. Matching on it is exact,
    and it is also the only safe match: the gateway names this recording
    `voice-0616aec33b41` where the controller names it `0616aec33b41`, so matching
    on ids would be a translation this side would have to guess. The hash is a
    fact.

    Never raises. A controller that does not serve this route, or whose credential
    may not read it, falls back to staging the clip — which is what happened
    before this existed, so the failure mode is exactly the old behaviour.
    """
    try:
        response = client.get(f"{base}/studio/voice/api/voices", headers=headers)
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    try:
        voices = (response.json() or {}).get("voices") or []
    except ValueError:
        return ""
    for voice in voices:
        if str(voice.get("audio_sha256") or "") == digest:
            return str(voice.get("id") or "")
    return ""


def _voice_placement(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    reference: dict[str, Any],
) -> tuple[str, str]:
    """Where the controller should take this voice from: `("library"|"asset", id)`.

    A library voice is one the controller already holds; an asset is a clip staged
    for this one attempt. They are **mutually exclusive** in the Hub's job payload
    — its broker refuses both with "Use either voice_library_id or
    voice_reference_asset_id, not both" — so exactly one is returned.

    The library wins whenever the controller already has the recording. Skipping
    the upload there is not only a saving: the controller's own entry is the
    durable form of the voice, while a staged asset is a copy made to serve one
    job. On this Mac all ten reference clips already match, so every one of those
    uploads was avoidable.
    """
    path = Path(str(reference.get("path") or ""))
    if not path.exists():
        raise BackendError(
            f"voice reference audio is missing for '{voice_id}'",
            code="VOICE_PROVIDER_RESULT_INVALID",
        )
    # Read before the cache, not after it: which recording this is *is* part of the
    # question the cache answers. Hashing a reference clip per job is a few
    # hundred kilobytes of I/O, and the alternative is answering about the wrong
    # audio for six hours.
    digest = _clip_digest(path)

    key = _reference_cache_key(voice_id, base, digest)
    cached = _REFERENCE_CACHE.get(key)
    if cached and (time.time() - cached[2]) < _REFERENCE_TTL_S:
        return cached[0], cached[1]

    node_voice_id = _library_voice_for(client, base, headers, digest)
    if node_voice_id:
        _remember_reference(key, "library", node_voice_id)
        return "library", node_voice_id

    asset_id = _stage_voice_reference(client, base, headers, voice_id, path, reference)
    _remember_reference(key, "asset", asset_id)
    return "asset", asset_id


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
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    path: Path,
    reference: dict[str, Any],
) -> str:
    """Upload a voice's reference clip and return the Hub's asset id.

    Qwen3 *Base* models are cloning models: without a reference they fail with
    "Voice cloning needs a reference voice or private reference upload". So the
    clip is staged first and passed as `voice_reference_asset_id`.

    This is the **fallback** now: `_voice_placement` reaches it only when the
    controller does not already hold the recording.
    """
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

    # Caching happens in `_voice_placement`, which is the only caller: it caches
    # the *placement* (library or asset), not the asset id alone.
    return asset_id


def _generate_image_or_voice(request: BackendRequest) -> BackendResult:
    base = _resolve_base(request)
    headers = _headers(request)
    modality = _HUB_MODALITY[request.modality]

    # A caller-supplied `params` is passed through to the Hub, but it has to be an
    # object: `request.params.get("params")` could be a string or a list, and the
    # very next line assigns into it, so a malformed field became a `TypeError`
    # that settled the job as an internal-looking failure instead of saying what
    # was wrong.
    supplied = request.params.get("params")
    if supplied is not None and not isinstance(supplied, dict):
        raise BackendError(
            "'params' must be an object of Hub parameters.", code="INVALID_PARAMS"
        )
    item: dict[str, Any] = {"params": dict(supplied or {})}
    if request.modality in {"image", "music"}:
        # Both address the worker by `prompt`; Music then takes its own engine
        # parameters and no image geometry.
        item["prompt"] = str(request.params.get("prompt") or "")
        keys = (
            ("seed", "width", "height", "aspect_ratio", "resolution")
            if request.modality == "image"
            else _HUB_MUSIC_PARAMS
        )
        for key in keys:
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
                kind, value = _voice_placement(client, base, headers, voice_id, reference)
                # Exactly one of these, never both: the Hub's broker refuses the
                # pair with "Use either voice_library_id or
                # voice_reference_asset_id, not both."
                body["voice_library_id" if kind == "library" else "voice_reference_asset_id"] = value
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
        if request.checkpoint is not None:
            request.checkpoint(batch_id)

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
        # Music comes back as WAV like voice, so only image differs.
        media_type = "image/png" if request.modality == "image" else "audio/wav"

    terminal = (outcome.get("item") or {}).get("terminal_result") or {}
    payload: dict[str, Any] = {
        "hub_batch_id": batch_id,
        "hub_machine": terminal.get("machine_id"),
        "hub_worker": terminal.get("worker_id"),
        "resolved_seed": terminal.get("resolved_seed"),
        "sha256": terminal.get("sha256"),
        "bytes": terminal.get("bytes"),
    }

    def deliver() -> dict[str, Any]:
        # Mutating the payload the result already carries is how the receipt
        # reaches the job record: the worker calls this before `store.succeed`,
        # so the answer it records is the one saved with the job.
        payload["delivery_ack"] = _ack_delivery(
            base, headers, batch_id, 0, request.modality
        )
        return payload["delivery_ack"]

    return BackendResult(
        content=artifact.content,
        media_type=media_type,
        payload=payload,
        deliver=deliver,
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
        if request.checkpoint is not None:
            request.checkpoint(batch_id)

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

    payload: dict[str, Any] = {"hub_batch_id": batch_id}

    def deliver() -> dict[str, Any]:
        # Subtitle has its own receipt route; the SRT is only really delivered
        # once the worker has written it.
        payload["delivery_ack"] = _ack_delivery(
            base, headers, batch_id, 0, "transcription"
        )
        return payload["delivery_ack"]

    return BackendResult(
        content=artifact.content,
        media_type="application/x-subrip",
        payload=payload,
        deliver=deliver,
    )


class StudioHubBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality in {"image", "tts", "music"}:
            return _generate_image_or_voice(request)
        if request.modality == "transcription":
            return _generate_transcription(request)
        raise BackendError(
            f"Studio Hub does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )

    def cancel(self, request: BackendRequest) -> dict[str, Any]:
        """Stop a Hub batch this gateway dispatched.

        Cancellation that never reached the Hub was only ever a local request:
        the batch kept running at the location while Story Studio was told the
        job was cancelled. This is the request that actually reaches the work,
        and its answer is what the caller records.

        A job with no upstream id has nothing to stop — that is reported as
        "nothing dispatched", not as a successful stop.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if not batch_id:
            return {
                "ok": False,
                "status": None,
                "reason": "nothing was dispatched to the Hub for this job",
            }
        return _cancel_batch(
            _resolve_base(request), _headers(request), batch_id, request.modality
        )
