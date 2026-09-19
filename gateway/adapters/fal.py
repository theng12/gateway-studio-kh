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

from .base import BackendError, BackendRequest, BackendResult

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
        payload["prompt"] = str(request.params.get("prompt") or "")
        if request.params.get("reference_asset_url"):
            payload["image_url"] = request.params["reference_asset_url"]
        if request.params.get("duration_seconds") is not None:
            seconds = request.params["duration_seconds"]
            # The two spellings are the endpoint's, not ours: see above.
            payload["duration"] = (
                str(seconds) if request.model_id in _DURATION_AS_STRING
                else int(seconds)
            )
        if request.params.get("aspect_ratio"):
            payload["aspect_ratio"] = request.params["aspect_ratio"]
        if request.model_id in _REQUIRES_PROMPT_EXPANSION:
            payload["prompt_expansion_mode"] = "balanced"
    elif request.modality == "tts":
        payload["text"] = str(request.params.get("text") or "")
        # fal wants a *name* ("Rachel"), not our catalogue id, so it goes through
        # the same resolver every other TTS lane uses.
        payload["voice"] = _fal_voice(request.params.get("voice_id")) or _FAL_DEFAULT_VOICE
        speed = request.params.get("speed")
        if isinstance(speed, (int, float)) and 0.7 <= float(speed) <= 1.2:
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

#: Endpoints that declare `prompt_expansion_mode` as **required** despite also
#: giving it a default, so it has to be sent explicitly.
_REQUIRES_PROMPT_EXPANSION: frozenset[str] = frozenset(
    {"minimax/h3-max/text-to-video", "minimax/h3-max/image-to-video"}
)

#: fal's own documented default voice for its ElevenLabs endpoints.
_FAL_DEFAULT_VOICE = "Rachel"


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
