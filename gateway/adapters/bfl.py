"""Black Forest Labs adapter — FLUX image generation, direct from the authors.

Async submit/poll/download, like fal and Kie but with BFL's own state names:

    Pending | Reasoning | Generating   -> still working
    Ready                              -> done, `result.sample` holds the URL
    Request Moderated | Content Moderated | Task not found | Error | Failed

Two details that matter:

  * The result URL **expires after about ten minutes**, so the artifact is
    downloaded here rather than handed to Story Studio. Story Studio requires
    artifact URLs to share the gateway's origin anyway.
  * `Request Moderated` is the *prompt* being refused and `Content Moderated`
    the *generated image*. They are distinct causes and are labelled as such
    instead of collapsing into one generic failure.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

POLL_INTERVAL_S = 1.5
JOB_DEADLINE_S = 600.0
SUBMIT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

_RUNNING_STATES = frozenset({"Pending", "Reasoning", "Generating"})
_MODERATED_STATES = frozenset({"Request Moderated", "Content Moderated"})
_FAILED_STATES = frozenset({"Task not found", "Error", "Failed"})

# BFL takes pixel dimensions, not aspect ratios.
_DIMENSIONS = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
    "21:9": (1536, 640),
}


def _headers(request: BackendRequest) -> dict[str, str]:
    return {"Accept": "application/json", "x-key": request.values.get("api_key", "")}


def _input_for(request: BackendRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"prompt": str(request.params.get("prompt") or "")}

    width = request.params.get("width")
    height = request.params.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        payload["width"], payload["height"] = width, height
    else:
        ratio = str(request.params.get("aspect_ratio") or "16:9")
        payload["width"], payload["height"] = _DIMENSIONS.get(ratio, (1344, 768))

    if request.params.get("seed") is not None:
        payload["seed"] = request.params["seed"]
    for key in ("guidance", "steps", "prompt_upsampling", "safety_tolerance", "output_format"):
        if request.params.get(key) is not None:
            payload[key] = request.params[key]
    return payload


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:250]
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            if payload.get(key):
                value = payload[key]
                return str(value.get("message") if isinstance(value, dict) else value)[:250]
    return response.text[:250]


def generate(request: BackendRequest) -> BackendResult:
    if request.modality != "image":
        raise BackendError(
            f"Black Forest Labs does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )

    base = (request.values.get("base_url") or request.base_url).rstrip("/")
    headers = _headers(request)

    with httpx.Client(timeout=SUBMIT_TIMEOUT) as client:
        try:
            submitted = client.post(
                f"{base}/v1/{request.model_id}", headers=headers, json=_input_for(request)
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
                f"bfl submit HTTP {status}: {_detail(submitted)}",
                code=code,
                status=status,
                retryable=status in {408, 425, 429} or status >= 500,
            )

        payload = submitted.json() or {}
        polling_url = str(payload.get("polling_url") or "")
        if not polling_url:
            raise BackendError("bfl returned no polling_url", code="PROVIDER_FAILED")

        deadline = time.monotonic() + JOB_DEADLINE_S
        artifact_url = ""
        while time.monotonic() < deadline:
            try:
                polled = client.get(polling_url, headers=headers, timeout=POLL_TIMEOUT)
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"poll network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                ) from exc

            if polled.status_code >= 400:
                raise BackendError(
                    f"bfl status HTTP {polled.status_code}",
                    code="PROVIDER_FAILED",
                    retryable=polled.status_code >= 500,
                )

            body = polled.json() or {}
            status = str(body.get("status") or "")

            if status == "Ready":
                result = body.get("result")
                sample = result.get("sample") if isinstance(result, dict) else None
                if not isinstance(sample, str) or not sample:
                    raise BackendError(
                        "bfl returned Ready without an artifact url", code="IMAGE_FAILED"
                    )
                artifact_url = sample
                break

            if status in _MODERATED_STATES:
                raise BackendError(
                    f"bfl refused this request: {status}",
                    code="IMAGE_CONTENT_REFUSED",
                )
            if status in _FAILED_STATES:
                raise BackendError(f"bfl task {status}", code="IMAGE_FAILED")
            if status not in _RUNNING_STATES:
                raise BackendError(
                    f"bfl returned an unrecognised status '{status}'", code="PROVIDER_FAILED"
                )
            time.sleep(POLL_INTERVAL_S)
        else:
            raise BackendError(
                "bfl task did not finish in time",
                code="PROVIDER_SUBMISSION_UNCERTAIN",
                retryable=True,
            )

        # The delivery URL expires in ~10 minutes: fetch it now.
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
                f"artifact HTTP {artifact.status_code}", code="IMAGE_FAILED"
            )

    media_type = artifact.headers.get("content-type", "").split(";")[0].strip() or "image/png"
    return BackendResult(
        content=artifact.content,
        media_type=media_type,
        payload={"model": request.model_id, "provider_url": artifact_url},
    )


class BflBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        return generate(request)
