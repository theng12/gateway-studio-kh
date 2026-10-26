"""fal.ai adapter — queue submit, poll, download.

fal runs on `queue.fal.run` and lists models on `api.fal.ai`. Both are covered
by the provider definition (`base_url` / `catalog_base_url`).

Image and video results both come back as URLs on fal's CDN, so the artifact is
downloaded here rather than handed to Story Studio -- Story Studio requires
artifact URLs to share the gateway's origin, and fal's do not.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import (
    BackendError,
    BackendRequest,
    BackendResult,
    staged_data_uri,
    staged_data_uris,
)
from .. import video_capabilities

POLL_INTERVAL_S = 2.0
JOB_DEADLINE_S = 900.0
SUBMIT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Key {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
    }


def _input_for(request: BackendRequest) -> dict[str, Any]:
    """Translate gateway params into fal's input shape.

    fal publishes a per-endpoint JSON schema at
    `https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>`, and reading
    it is what showed the two mistakes this function used to make:

      * **`duration` is not one type.** `bytedance/seedance-*` declares it as a
        *string* enum (`"auto"`, `"5"` …) while `minimax/h3-max` and
        `google/gemini-omni-flash` declare an *integer*. Sending `str(...)` to
        every lane therefore worked for Seedance and was a 422 for the other four
        — which fal only reports when you fetch the result, so the job looked
        `COMPLETED` and then failed on download.
      * **`minimax/h3-max` requires `prompt_expansion_mode`.** It has a documented
        default of `balanced`, but omitting it is a validation failure, not a
        defaulted field.

    Everything here is a *documented* field of the endpoints this gateway offers,
    with the types its schema states. A field a model does not declare is not
    sent, because fal's queue accepts anything and validates later.
    """
    payload: dict[str, Any] = {}
    if request.modality == "image":
        payload["prompt"] = str(request.params.get("prompt") or "")
        if request.params.get("aspect_ratio"):
            payload["aspect_ratio"] = request.params["aspect_ratio"]
        if request.params.get("seed") is not None:
            payload["seed"] = request.params["seed"]
        for key in ("num_images", "num_inference_steps", "guidance_scale", "output_format"):
            if request.params.get(key) is not None:
                payload[key] = request.params[key]
    elif request.modality == "video":
        payload = _video_input(request)
    elif request.modality == "tts":
        payload["text"] = str(request.params.get("text") or "")
        # fal wants a *name* ("Rachel"), not our catalogue id, so it goes through
        # the same resolver every other TTS lane uses.
        payload["voice"] = _fal_voice(request.params.get("voice_id")) or _FAL_DEFAULT_VOICE
        speed = request.params.get("speed")
        if isinstance(speed, (int, float)):
            # **Refused, not dropped.** fal's endpoints take a narrower speed range
            # than the catalogue used to publish (0.5–2.0), and this silently
            # discarded anything outside 0.7–1.2 — so a caller picked a speed the
            # gateway had told it was valid and got normal speed, with no signal in
            # either direction. The catalogue now publishes this lane's real range,
            # so reaching here means the caller ignored it.
            if not (_FAL_MIN_SPEED <= float(speed) <= _FAL_MAX_SPEED):
                raise BackendError(
                    f"fal's TTS routes accept speed between {_FAL_MIN_SPEED} and "
                    f"{_FAL_MAX_SPEED}; {float(speed)} was asked for",
                    code="INVALID_REQUEST",
                    status=422,
                )
            payload["speed"] = float(speed)
        controls = request.params.get("controls")
        if isinstance(controls, dict):
            for key in ("stability", "similarity_boost", "style"):
                value = controls.get(key)
                if isinstance(value, (int, float)) and 0 <= float(value) <= 1:
                    payload[key] = float(value)
        if request.params.get("language"):
            payload["language_code"] = str(request.params["language"])
    return payload


#: Endpoints whose `duration` is a **string** enum, from their own schemas. Every
#: other video endpoint takes an integer. Kept as a list rather than a rule
#: because the two spellings are genuinely both in use and neither is derivable.
_DURATION_AS_STRING: frozenset[str] = frozenset(
    {
        "bytedance/seedance-2.0/text-to-video",
        "bytedance/seedance-2.0/image-to-video",
        "bytedance/seedance-2.5/text-to-video",
        "bytedance/seedance-2.5/image-to-video",
        "bytedance/seedance-2.5/reference-to-video",
    }
)


def _video_input(request: BackendRequest) -> dict[str, Any]:
    """One video request body, in the shape the **endpoint itself** declares.

    The field names are not ours to choose. Wan 3.0 puts its first frame in `start_image_url` and
    its audio switch in `audio`; Seedance 2.5 puts the same two things in `image_url` and
    `generate_audio`, declares `duration` as a *string* enum, and names its reference images
    `image_urls` (Wan calls them `reference_image_urls`). `video_capabilities` holds those facts
    per model, so this function names no model and a new endpoint is one record, not a new branch.

    **An option the record does not publish is refused, not sent.** fal's queue accepts any body
    and validates at the worker, so a field the endpoint never declared reports the job COMPLETED
    and fails when the result is fetched — a 422 in place of the artifact, hours later.
    """
    payload: dict[str, Any] = {"prompt": str(request.params.get("prompt") or "")}
    record = video_capabilities.for_model(request.provider_id, request.model_id)
    if record is None:
        # **Nobody here has described this endpoint.** Its mode was read from the provider, and
        # this is the generic body the adapter has always sent — kept for a model named directly
        # rather than through the catalogue (the mock lanes, or an endpoint whose record is not
        # written yet). No image field is guessed: `reference_asset_url` is the one the caller
        # named explicitly.
        if request.params.get("reference_asset_url"):
            payload["image_url"] = request.params["reference_asset_url"]
        if request.params.get("duration_seconds") is not None:
            seconds = request.params["duration_seconds"]
            payload["duration"] = (
                str(seconds) if request.model_id in _DURATION_AS_STRING else int(seconds)
            )
        if request.params.get("aspect_ratio"):
            payload["aspect_ratio"] = request.params["aspect_ratio"]
        if request.model_id in _REQUIRES_PROMPT_EXPANSION:
            payload["prompt_expansion_mode"] = "balanced"
        return payload

    seconds = request.params.get("duration_seconds")
    if seconds is not None:
        seconds = _published_duration(record, seconds)
        # The two spellings are the endpoint's, not ours: see `_DURATION_AS_STRING`.
        payload["duration"] = (
            seconds if seconds == "auto"
            else (str(seconds) if record.duration_style == "string" else int(seconds))
        )
    aspect_ratio = request.params.get("aspect_ratio")
    if aspect_ratio:
        payload["aspect_ratio"] = _published(
            record, "aspect_ratio", aspect_ratio, record.aspect_ratios
        )
    resolution = request.params.get("resolution")
    if resolution:
        payload["resolution"] = _published(
            record, "resolution", resolution, record.resolutions
        )

    image = staged_data_uri(request, "_reference_path", "_reference_type")
    if image is None and request.params.get("reference_asset_url"):
        image = str(request.params["reference_asset_url"])
    if image:
        payload[_required_field(record, record.start_frame_field, "start frame",
                                record.modes)] = image

    end_image = staged_data_uri(request, "_end_reference_path", "_end_reference_type")
    if end_image:
        payload[_required_field(record, record.end_frame_field, "final frame",
                                record.modes)] = end_image

    references = staged_data_uris(request, "_reference_paths", "_reference_types")
    if references:
        field = _required_field(record, record.reference_field, "reference images",
                                record.modes)
        if len(references) > record.max_reference_images:
            raise BackendError(
                f"{record.model_id} takes at most {record.max_reference_images} reference "
                f"image{'s' if record.max_reference_images != 1 else ''}; "
                f"{len(references)} were named",
                code="INVALID_REQUEST",
                status=422,
                upstream_accepted=False,
            )
        payload[field] = references

    mode = str(request.params.get("mode") or request.params.get("operation") or "").strip().lower()
    if mode and mode not in record.modes:
        raise BackendError(
            f"{record.model_id} publishes {', '.join(record.modes) or 'no mode'}; {mode} was "
            "asked for",
            code="INVALID_REQUEST",
            status=422,
            upstream_accepted=False,
        )
    if mode == "reference-to-video" and not references:
        raise BackendError(
            f"{record.model_id} needs at least one reference image for reference-to-video; "
            "upload it and name its id in reference_asset_ids.",
            code="INVALID_PARAMS",
            status=422,
            upstream_accepted=False,
        )
    # **A model that cannot generate from text alone must be given its picture.** Without this,
    # an image-to-video request whose upload was lost reached the endpoint as a prompt-only body,
    # and fal answered a text-to-video clip that nothing distinguishes from the one asked for.
    if not image and not references and "text-to-video" not in record.modes:
        raise BackendError(
            f"{record.model_id} generates a clip from an image and this request carries none. "
            "Upload the scene image to POST /v1/videos/source-images and name its id as "
            "reference_asset_id (or as reference_asset_ids for reference-to-video).",
            code="INVALID_PARAMS",
            status=422,
            upstream_accepted=False,
        )

    audio = _audio_value(request, record.audio_field)
    if audio is not None and record.audio_field:
        payload[record.audio_field] = audio
    return payload


def _published(record: "video_capabilities.VideoCapability", field: str, value: Any,
               published: tuple[str, ...]) -> str:
    """A value the record publishes, or a refusal naming everything it does publish.

    Never clamped and never dropped: a caller told a canvas is valid, whose value is then silently
    replaced, buys a different video from the one it asked for and nothing says so.
    """
    text = str(value)
    if published and text not in published:
        raise BackendError(
            f"{record.model_id} accepts {field} {', '.join(published)}; {text!r} was asked for",
            code="INVALID_REQUEST",
            status=422,
            upstream_accepted=False,
        )
    return text


def _published_duration(record: "video_capabilities.VideoCapability", seconds: Any) -> int | str:
    """The whole seconds a record accepts — or `"auto"`, where it publishes that.

    Refused (not clamped) when the record states a different set: a caller told a duration is
    valid, whose value is then silently replaced, buys a different clip and nothing says so.
    """
    if isinstance(seconds, str) and seconds.strip().lower() == "auto":
        if record.duration_auto:
            return "auto"
        raise BackendError(
            f"{record.model_id} needs a whole number of seconds, not 'auto'",
            code="INVALID_REQUEST",
            status=422,
            upstream_accepted=False,
        )
    try:
        whole = int(seconds)
    except (TypeError, ValueError):
        raise BackendError(
            f"{record.model_id} takes whole seconds, not {seconds!r}",
            code="INVALID_REQUEST",
            status=422,
            upstream_accepted=False,
        ) from None
    accepted = record.durations()
    if accepted and whole not in accepted:
        raise BackendError(
            f"{record.model_id} accepts {record.duration_min}-{record.duration_max}s in steps of "
            f"{record.duration_step}; {whole}s was asked for",
            code="INVALID_REQUEST",
            status=422,
            upstream_accepted=False,
        )
    return whole


def _required_field(record: "video_capabilities.VideoCapability", field: str | None,
                    what: str, modes: tuple[str, ...]) -> str:
    """The field a staged input goes in — or a refusal, because this model takes no such input.

    This is where "never send a provider a field it doesn't read" is enforced for the picture: a
    Wan text-to-video endpoint has no image field at all, and quietly dropping the upload would
    answer a text-to-video request with nobody told the scene image was ignored.
    """
    if field:
        return field
    raise BackendError(
        f"{record.model_id} takes no {what} (its modes are {', '.join(modes) or 'none stated'}); "
        "pick a model whose catalogue row publishes that input",
        code="INVALID_REQUEST",
        status=422,
        upstream_accepted=False,
    )


def _audio_value(request: BackendRequest, field: str | None) -> bool | None:
    """The caller's audio switch, under the endpoint's name or the gateway's generic one."""
    if not field:
        return None
    controls = request.params.get("controls")
    if isinstance(controls, dict) and isinstance(controls.get(field), bool):
        return bool(controls[field])
    if isinstance(request.params.get(field), bool):
        return bool(request.params[field])
    # Story Studio's own name for the switch, whatever the endpoint calls it.
    if isinstance(request.params.get("audio"), bool):
        return bool(request.params["audio"])
    return None

#: Endpoints that declare `prompt_expansion_mode` as **required** despite also
#: giving it a default, so it has to be sent explicitly.
_REQUIRES_PROMPT_EXPANSION: frozenset[str] = frozenset(
    {"minimax/h3-max/text-to-video", "minimax/h3-max/image-to-video"}
)

#: fal's own documented default voice for its ElevenLabs endpoints.
_FAL_DEFAULT_VOICE = "Rachel"

#: The speed range fal's ElevenLabs routes accept. The catalogue publishes it, so
#: what a caller is offered is what this adapter will send.
#:
#: ponytail: one range for every fal TTS route, hand-recorded. Ceiling — the range
#: is per *endpoint* and fal publishes it in each endpoint's OpenAPI schema, which is
#: never fetched here, so a route with a different range is over- or under-promised.
#: Upgrade path — read `https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>`
#: at discovery time, the way the body schemas already are.
_FAL_MIN_SPEED = 0.7
_FAL_MAX_SPEED = 1.2


def _fal_voice(voice_id: Any) -> str:
    """A catalogue voice id resolved to the name fal's `voice` field wants."""
    if not voice_id:
        return ""
    from ..voices import resolve  # local import: avoids a cycle

    voice = resolve(str(voice_id))
    if voice is not None and voice.provider_voice_id:
        return str(voice.provider_voice_id)
    # A bare name the caller already knows ("Rachel") is passed through: fal
    # validates it, and refusing it here would block a voice fal accepts.
    text = str(voice_id)
    return text.rsplit("::", 1)[-1] if "::" in text else text


def _extract_artifact_url(payload: dict[str, Any]) -> str:
    """Find the produced asset across fal's several response shapes."""
    if not isinstance(payload, dict):
        raise BackendError("unexpected fal result payload", code="PROVIDER_FAILED")

    for key in ("video", "audio"):
        entry = payload.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return str(entry["url"])

    for key in ("images", "image", "output"):
        entry = payload.get(key)
        if isinstance(entry, list) and entry:
            first = entry[0]
            if isinstance(first, dict) and first.get("url"):
                return str(first["url"])
            if isinstance(first, str):
                return first
        if isinstance(entry, dict) and entry.get("url"):
            return str(entry["url"])

    if payload.get("url"):
        return str(payload["url"])

    raise BackendError("fal returned no artifact url", code="PROVIDER_FAILED")


class FalBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality not in {"image", "video", "tts"}:
            raise BackendError(
                f"fal does not serve '{request.modality}'", code="MODEL_MODALITY_MISMATCH"
            )

        base = (request.values.get("base_url") or request.base_url).rstrip("/")
        headers = _headers(request)
        endpoint = request.model_id.strip("/")

        with httpx.Client(timeout=SUBMIT_TIMEOUT) as client:
            try:
                submitted = client.post(
                    f"{base}/{endpoint}", headers=headers, json=_input_for(request)
                )
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                ) from exc

            if submitted.status_code >= 400:
                status = submitted.status_code
                code = "PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED"
                if status == 429:
                    code = "PROVIDER_QUOTA_EXHAUSTED"
                raise BackendError(
                    f"fal submit HTTP {status}: {submitted.text[:200]}",
                    code=code,
                    status=status,
                    retryable=status in {408, 425, 429} or status >= 500,
                )

            payload = submitted.json() or {}
            status_url = payload.get("status_url")
            response_url = payload.get("response_url")
            if not status_url or not response_url:
                raise BackendError("fal returned no status/response url", code="PROVIDER_FAILED")
            request_id = str(payload.get("request_id") or "")
            if request_id and request.checkpoint is not None:
                request.checkpoint(request_id)

            deadline = time.monotonic() + JOB_DEADLINE_S
            while time.monotonic() < deadline:
                try:
                    status_response = client.get(status_url, headers=headers, timeout=POLL_TIMEOUT)
                except httpx.HTTPError as exc:
                    raise BackendError(
                        f"poll network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                    ) from exc

                if status_response.status_code >= 400:
                    raise BackendError(
                        f"fal status HTTP {status_response.status_code}",
                        code="PROVIDER_FAILED",
                        retryable=status_response.status_code >= 500,
                    )

                state = str((status_response.json() or {}).get("status") or "").upper()
                if state == "COMPLETED":
                    break
                if state in {"FAILED", "ERROR", "CANCELLED"}:
                    raise BackendError(f"fal job {state.lower()}", code="PROVIDER_FAILED")
                time.sleep(POLL_INTERVAL_S)
            else:
                raise BackendError(
                    "fal job did not finish in time",
                    code="PROVIDER_SUBMISSION_UNCERTAIN",
                    retryable=True,
                )

            try:
                result_response = client.get(response_url, headers=headers, timeout=POLL_TIMEOUT)
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"result network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                ) from exc

            if result_response.status_code >= 400:
                raise BackendError(
                    f"fal result HTTP {result_response.status_code}", code="PROVIDER_FAILED"
                )
            artifact_url = _extract_artifact_url(result_response.json() or {})

            try:
                artifact = client.get(artifact_url, timeout=ARTIFACT_TIMEOUT)
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"artifact download failed: {exc}",
                    code="PROVIDER_UNAVAILABLE",
                    retryable=True,
                ) from exc

            if artifact.status_code >= 400:
                raise BackendError(
                    f"artifact HTTP {artifact.status_code}", code="PROVIDER_FAILED"
                )

        media_type = artifact.headers.get("content-type", "").split(";")[0].strip()
        if not media_type:
            media_type = {"image": "image/png", "tts": "audio/mpeg"}.get(
                request.modality, "video/mp4"
            )

        return BackendResult(
            content=artifact.content,
            media_type=media_type,
            payload={"provider_url": artifact_url, "model": request.model_id},
        )
