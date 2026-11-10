"""`chrome_extension`: a generation run by a browser-extension worker (wire v1, `extension_workers`).

The adapter offers the job, waits for a worker to claim it, and waits for the worker's answer.
**Paid credits decide every failure:**

- nobody claimed it → withdrawn, never sent: an ordinary retryable refusal;
- claimed → `upstream_job_id` is checkpointed first, so from then on any answer that is not a
  definite one (`ambiguous` fail, a silent lease, a restart) is a durable hold, never replayed;
- a definite `ambiguous: false` fail is settled: the worker says nothing was spent.
"""
from __future__ import annotations

import base64
import json
import math
import re
import time
from pathlib import Path

from .. import extension_workers as ew, video_capabilities
from .base import BackendError, BackendRequest, BackendResult, staged_bytes, STAGED_INPUT_GONE, staged_path

_POLL_SECONDS = 1.0

#: Image capability records, loaded once from the copied Relay catalogue.
_IMAGE_MODELS_PATH = Path(__file__).parent.parent / "extension_image_models.json"
_IMAGE_DATA = json.loads(_IMAGE_MODELS_PATH.read_text())
_IMAGE_KEYS = {m["key"] for m in _IMAGE_DATA["models"]}
_IMAGE_MAX_REFS = int(_IMAGE_DATA["max_reference_images"])
_IMAGE_ASPECTS = tuple(_IMAGE_DATA["aspect_ratios"])

#: Audio capability records, loaded once from the copied Relay catalogue.
_AUDIO_MODELS_PATH = Path(__file__).parent.parent / "extension_audio_models.json"
_AUDIO_DATA = json.loads(_AUDIO_MODELS_PATH.read_text())
_AUDIO_PROVIDERS = _AUDIO_DATA["providers"]

_PLACEHOLDER = ("{{", "}}")


def _is_placeholder(key: str) -> bool:
    return any(marker in key for marker in _PLACEHOLDER)


def _audio_model_record(provider: str, model_key: str) -> dict | None:
    """The exact model row from the copied catalogue, or `None`. Placeholder keys are never
    returned: they are withheld from publication and refused by name."""
    if _is_placeholder(model_key):
        return None
    for m in _AUDIO_PROVIDERS.get(provider, {}).get("models", []):
        if m.get("key") == model_key and not _is_placeholder(str(m.get("key") or "")):
            return m
    return None


def _audio_selectable_keys(provider: str) -> set[str]:
    """Model keys that are publishable for this provider. Placeholder ids are excluded."""
    return {
        m["key"] for m in _AUDIO_PROVIDERS.get(provider, {}).get("models", [])
        if not _is_placeholder(str(m.get("key") or ""))
    }


# Build selectable-key sets once at import; placeholder keys are withheld.
_AUDIO_MODEL_KEYS: dict[str, set[str]] = {
    pid: _audio_selectable_keys(pid) for pid in _AUDIO_PROVIDERS
}


def _uncertain(message: str) -> BackendError:
    return BackendError(message, code="PROVIDER_SUBMISSION_UNCERTAIN", upstream_accepted=True)


def _references(request: BackendRequest) -> list[dict[str, str]]:
    """Every staged picture, in order, as the worker's `referenceImages`. A missing one refuses."""
    out: list[dict[str, str]] = []
    single = staged_bytes(request, "_reference_path", "_reference_type")
    if single is not None:
        out.append({"mediaType": single[1], "dataBase64": base64.b64encode(single[0]).decode("ascii")})
    paths = request.params.get("_reference_paths")
    types = request.params.get("_reference_types")
    types = types if isinstance(types, list) else []
    for index, path in enumerate(paths if isinstance(paths, list) else []):
        staged = staged_path(path)
        if staged is None or not staged.exists():
            raise BackendError(STAGED_INPUT_GONE, code="INVALID_PARAMS", upstream_accepted=False)
        media_type = str(types[index]) if index < len(types) and types[index] else "image/png"
        out.append({"mediaType": media_type,
                    "dataBase64": base64.b64encode(staged.read_bytes()).decode("ascii")})
    return out


def _refuse(message: str) -> BackendError:
    return BackendError(message, code="INVALID_REQUEST", status=422, upstream_accepted=False)


def _unavailable(message: str) -> BackendError:
    """Fail closed: the wire cannot confirm what it needs to, so nothing is offered."""
    return BackendError(message, code="WORKER_INVENTORY_UNAVAILABLE", retryable=False,
                        upstream_accepted=False)


#: A safe id token (no whitespace, control, URL or query characters). Relay's own model ids use
#: this shape (`FLOW_ID`); a project id must at least be one safe token.
#: shortcut: a safe-token check, not Flow's exact project grammar; upgrade if Relay publishes it.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _payload(request: BackendRequest, references: list[dict[str, str]]) -> dict:
    """The §4 job, from the model's `VideoCapability` record. What the record does not publish is
    refused here, before any worker sees it (the extension would otherwise improvise and spend)."""
    params = request.params
    record = video_capabilities.for_model(request.provider_id, request.model_id)
    if record is None:
        raise _refuse(f"{request.model_id} has no capability record; it is not selectable")
    text = "text-to-video" in record.modes
    duration = params.get("duration_seconds", params.get("duration"))
    if duration is None:
        duration = record.duration_default
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise _refuse(f"duration {duration!r} is not a whole number of seconds") from None
    if duration not in record.durations():
        raise _refuse(f"{request.model_id} makes {', '.join(map(str, record.durations()))} s clips; "
                      f"{duration} s was asked for")
    aspect = params.get("aspect_ratio") or record.aspect_ratios[0]
    if aspect not in record.aspect_ratios:
        raise _refuse(f"{request.model_id} publishes {', '.join(record.aspect_ratios)}; {aspect} was asked for")
    if text and references:
        raise _refuse(f"{request.model_id} is text-to-video and takes no picture")
    if not text and not references:
        raise _refuse(f"{request.model_id} needs its reference picture")
    if len(references) > record.max_reference_images:
        raise _refuse(f"{request.model_id} takes at most {record.max_reference_images} reference "
                      f"image(s); {len(references)} were named")
    return {
        "kind": request.modality,
        "videoMode": "text" if text else "image",
        "prompt": str(params.get("prompt") or ""),
        "modelKey": request.model_id,
        "modelLabel": request.display_name or request.model_id,
        "durationSec": duration,
        "aspectRatio": aspect,
        "flowProjectId": request.values.get("flow_project_id") or None,
        "referenceImages": references,
    }


def _detect_image_signature(raw: bytes) -> str | None:
    """Return 'png', 'jpeg', or 'webp' when the bytes match a known image header, else `None`.
    WebP requires both the RIFF container and the WEBP marker at offset 8; RIFF alone is WAV/AVI."""
    if raw[:4] == b"\x89PNG":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:4] == b"RIFF" and len(raw) >= 12 and raw[8:12] == b"WEBP":
        return "webp"
    return None


_SIG_TO_MEDIA = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _image_payload(request: BackendRequest, references: list[dict[str, str]]) -> dict:
    """The §4 image job, from the copied Relay catalogue. What the record does not publish is
    refused here, before any worker sees it (the extension would otherwise improvise and spend)."""
    params = request.params
    model_key = request.model_id
    if model_key not in _IMAGE_KEYS:
        raise _refuse(f"{model_key} has no capability record; it is not selectable")
    prompt = str(params.get("prompt") or "")
    if not prompt:
        raise _refuse("an image generation needs a prompt")
    aspect = params.get("aspect_ratio") or _IMAGE_ASPECTS[0]
    if aspect not in _IMAGE_ASPECTS:
        raise _refuse(f"image models publish {', '.join(_IMAGE_ASPECTS)}; {aspect} was asked for")
    if len(references) > _IMAGE_MAX_REFS:
        raise _refuse(f"image models take at most {_IMAGE_MAX_REFS} reference image(s); "
                      f"{len(references)} were named")
    # Optional provider: Relay's image contract allows only 'flow'. Never overwrite silently.
    provider = str(params.get("provider") or "flow").strip()
    if provider != "flow":
        raise _refuse(f"image provider {provider!r} is not supported; only 'flow' is")
    # Optional Flow project: if named, it must be one safe id token, from payload or settings.
    project = request.values.get("flow_project_id") or params.get("flowProjectId") \
        or params.get("flow_project_id")
    if project is not None:
        project = str(project).strip()
        if project and not _SAFE_ID.match(project):
            raise _refuse(f"flowProjectId {project!r} is not a valid project id")
    # Validate each reference: decodable base64, known image signature, mediaType agrees.
    for idx, ref in enumerate(references):
        try:
            raw = base64.b64decode(ref["dataBase64"], validate=True)
        except Exception:
            raise _refuse(f"reference image {idx + 1} dataBase64 does not decode") from None
        detected = _detect_image_signature(raw)
        if detected is None:
            raise _refuse(f"reference image {idx + 1} is not PNG, JPEG or WebP")
        stated = (ref.get("mediaType") or "").split(";")[0].strip().lower()
        expected = _SIG_TO_MEDIA[detected]
        if stated and stated != expected:
            raise _refuse(f"reference image {idx + 1} mediaType {stated!r} disagrees with "
                          f"detected {expected}")
    return {
        "kind": "image",
        "prompt": prompt,
        "modelKey": model_key,
        "provider": provider,
        "aspectRatio": aspect,
        "flowProjectId": project or None,
        "referenceImages": references,
    }


def _first_stated(*values):
    """First non-`None` value. A stated `0` is a claim; never fall through to a default."""
    for v in values:
        if v is not None:
            return v
    return None


def _audio_payload(request: BackendRequest) -> dict:
    """Validate audio against the copied policy and authenticated worker discovery.

    Schema 1 claim inventories establish exact live membership, not compatible pairs or required
    provider limits. Until those are established, no audio job is offered. Caller params never
    supply discovery authority or bypass the missing compatibility evidence.
    """
    params = request.params
    provider = str(params.get("provider") or "").strip()
    if provider not in _AUDIO_PROVIDERS:
        raise _refuse(f"audio provider {provider!r} is not known; "
                      f"publish {', '.join(sorted(_AUDIO_PROVIDERS))}")
    pdata = _AUDIO_PROVIDERS[provider]
    model_key = str(request.model_id or "").strip()
    # Placeholder ids are withheld: unselectable and refused by name.
    if _is_placeholder(model_key):
        label = next((m.get("label", model_key)
                      for m in pdata.get("models", []) if m.get("key") == model_key), model_key)
        raise _refuse(f"the audio model id for {label} is a template placeholder; "
                      "use an exact public model id")
    valid_keys = _AUDIO_MODEL_KEYS.get(provider, set())
    if model_key not in valid_keys:
        raise _refuse(f"{provider} model {model_key!r} has no capability record; it is not selectable")
    record = _audio_model_record(provider, model_key)
    voice = str(params.get("voice_id") or params.get("voice") or "").strip()
    if not voice:
        raise _refuse("an audio generation needs a voice: send 'voice_id' naming one")
    text = str(params.get("text") or "").strip()
    if not text:
        raise _refuse("an audio generation needs text")
    # Per-model max_chars wins over provider-level; first-stated semantics (a stated 0 is a claim).
    model_max = record.get("max_chars") if record else None
    provider_max = pdata.get("max_chars")
    max_chars = _first_stated(model_max, provider_max)
    if max_chars is not None:
        max_chars = int(max_chars)
        if len(text) > max_chars:
            raise _refuse(f"Gateway's copied Relay catalogue policy allows at most {max_chars} "
                          f"characters for {provider}/{model_key}; {len(text)} were given")
    speed = params.get("speed")
    if speed is not None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise _refuse(f"speed {speed!r} is not a number") from None
        if not math.isfinite(speed):
            raise _refuse(f"speed {speed!r} is not finite")
        # Model-level speed:false refuses any speed; model/provider speed_range gates the value.
        model_speed = record.get("speed") if record else None
        if model_speed is False:
            raise _refuse(f"{provider}/{model_key} does not accept speed")
        model_range = record.get("speed_range") if record else None
        provider_range = pdata.get("speed_range")
        speed_range = _first_stated(model_range, provider_range)
        if not speed_range:
            raise _refuse(f"{provider}/{model_key} publishes no speed range; speed is refused")
        lo, hi = float(speed_range.get("min", 0)), float(speed_range.get("max", 0))
        if lo <= 0 or hi <= 0 or lo > hi:
            raise _refuse(f"{provider}/{model_key} speed range is invalid")
        if speed < lo or speed > hi:
            raise _refuse(f"{provider}/{model_key} speed must be between {lo} and {hi}; "
                          f"{speed} was asked for")
    # Optional switches, validated against what the provider actually supports.
    settings = pdata.get("settings") or {}
    auto_tag = params.get("autoTag")
    if auto_tag is not None and not settings.get("auto_tag"):
        raise _refuse(f"{provider} does not support autoTag")
    provider_settings = params.get("providerSettings")
    allowed_settings = set(settings.get("provider_settings") or [])
    if provider_settings is not None:
        if not isinstance(provider_settings, dict):
            raise _refuse("providerSettings must be an object")
        unknown = set(provider_settings) - allowed_settings
        if unknown:
            raise _refuse(f"{provider} does not support providerSettings "
                          f"{', '.join(sorted(unknown))}")
    if not ew.broker.audio_member({"provider": provider, "model": model_key, "voice": voice}):
        raise _unavailable(
            f"{provider} audio needs fresh authenticated worker discovery for {model_key!r}/{voice!r}; "
            "no audio job is offered")
    raise _unavailable(
        f"{provider} discovery confirms model and voice membership, but compatible pairs and required "
        "provider limits are not established; no audio job is offered")


class ChromeExtensionBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.upstream_job_id:
            # A restart lost the hand-off of a job a worker had claimed: it may have spent credits.
            raise _uncertain("a browser worker had this job when the Gateway restarted; "
                             "its outcome is unknown, so it is held and not sent again")
        if not ew.enabled():
            raise BackendError("browser-extension workers are switched off on this Gateway",
                               code="PROVIDER_DISABLED", upstream_accepted=False)
        kind = "audio" if request.modality == "tts" else request.modality
        if kind not in ew.KINDS:
            raise BackendError(f"browser workers do not serve {kind}", code="INVALID_REQUEST",
                               upstream_accepted=False)
        if kind == "image":
            payload = _image_payload(request, _references(request))
        elif kind == "audio":
            payload = _audio_payload(request)
        else:
            payload = _payload(request, _references(request))
        if not ew.broker.online(kind):
            raise BackendError("no browser worker is online for this model", code="WORKER_OFFLINE",
                               retryable=True, upstream_accepted=False)
        job = ew.broker.offer(kind, payload)
        result = self._follow(request, job)
        # Forgotten only once its answer is in hand; a held job stays so a late answer can find it.
        ew.broker.forget(job)
        return result

    def _follow(self, request: BackendRequest, job: ew.WorkerJob) -> BackendResult:
        offered = time.time()
        checkpointed = False
        while True:
            job.changed.wait(_POLL_SECONDS)
            job.changed.clear()
            if job.state == "pending":
                if request.cancelled is not None and request.cancelled() and ew.broker.withdraw(job):
                    raise BackendError("cancelled before a worker took it", code="CANCELLED",
                                       upstream_accepted=False)
                if time.time() - offered > ew.claim_wait_seconds() and ew.broker.withdraw(job):
                    raise BackendError("no browser worker took this job; nothing was sent",
                                       code="WORKER_OFFLINE", retryable=True, upstream_accepted=False)
                continue
            if not checkpointed:
                # Before anything else: from here the worker may spend credits on it.
                if request.checkpoint is not None:
                    request.checkpoint(ew.upstream_id(job.owner or "", job.job_id))
                checkpointed = True
            if job.state == "succeeded":
                return BackendResult(content=job.content, media_type=job.media_type,
                                     payload={"worker_id": job.owner})
            if job.state == "failed":
                taken = ew.broker.take_failure(job)
                if taken == "held":   # from here a later result is adopted into the row
                    raise _uncertain(f"the browser worker could not tell whether it was charged: "
                                     f"{job.error}")
                if taken == "failed":
                    ew.broker.forget(job)
                    raise BackendError(job.error, code="PROVIDER_FAILED", upstream_accepted=True,
                                       upstream_settled=True)
                continue   # a result replaced the ambiguous failure: read it next turn
            # `hold` is the one transition that can lose a race with the worker's answer: only a
            # hold that actually happened raises; otherwise the answer landed, so read it next turn.
            if time.time() - job.last_heard > ew.lease_seconds() and ew.broker.hold(job):
                raise _uncertain(f"the browser worker went silent for over {int(ew.lease_seconds())} s "
                                 "with this job; it is held, never sent again")
