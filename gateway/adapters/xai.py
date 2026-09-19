"""xAI (Grok) adapter — OpenAI-compatible chat, Grok's own image and video.

**Chat is delegated; the media lanes are not.** xAI serves
`/v1/chat/completions` in OpenAI's shape, so the shim handles it unchanged. Its
image and video APIs are *not* that shape:

  * `/v1/images/generations` rejects `size` outright — measured:
    `HTTP 400: Argument not supported: size` — and takes `aspect_ratio` and
    `resolution` instead. The OpenAI-compatible shim sends `size` for every
    provider, so delegating images published three models that failed on use.
  * `/v1/videos/generations` is a **deferred** endpoint: it returns a
    `request_id` and the video arrives by polling, which no OpenAI-shaped handler
    does.

Both media shapes come from xAI's OpenAPI (`https://docs.x.ai/openapi.json`) and
its video-generation guide, not from inference.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult
from .openai_compat import OpenAICompatibleBackend

SUBMIT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
MEDIA_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

#: Video generation is documented as taking "up to several minutes".
POLL_INTERVAL_S = 5.0
JOB_DEADLINE_S = 600.0

#: xAI's documented resolution values. The gateway's own catalogue speaks in
#: these already, so they travel through unchanged when the caller names one.
_XAI_RESOLUTIONS = {"480p", "720p", "1080p"}


def _base(request: BackendRequest) -> str:
    return (request.values.get("base_url") or request.base_url).rstrip("/")


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
    }


def _image_body(request: BackendRequest) -> dict[str, Any]:
    """xAI's image request: no `size`, and `aspect_ratio`/`resolution` instead."""
    body: dict[str, Any] = {
        "model": request.model_id,
        "prompt": str(request.params.get("prompt") or ""),
        "n": 1,
        # `b64_json` avoids a second fetch and sidesteps the same-origin rule.
        "response_format": "b64_json",
    }
    if request.params.get("aspect_ratio"):
        body["aspect_ratio"] = str(request.params["aspect_ratio"])
    resolution = str(request.params.get("resolution") or "")
    if resolution in _XAI_RESOLUTIONS:
        body["resolution"] = resolution
    return body


def _generate_image(request: BackendRequest) -> BackendResult:
    try:
        response = httpx.post(
            f"{_base(request)}/images/generations",
            headers=_headers(request),
            json=_image_body(request),
            timeout=MEDIA_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise BackendError(f"network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True) from exc

    if response.status_code >= 400:
        status = response.status_code
        raise BackendError(
            f"xAI image HTTP {status}: {response.text[:200]}",
            code="PROVIDER_AUTH_FAILED" if status in {401, 403} else "PROVIDER_FAILED",
            status=status,
            retryable=status in {408, 425, 429} or status >= 500,
        )

    entries = (response.json() or {}).get("data") or []
    if not entries:
        raise BackendError("xAI returned no image", code="IMAGE_FAILED")
    entry = entries[0] or {}
    if entry.get("b64_json"):
        content = base64.b64decode(entry["b64_json"])
    elif entry.get("url"):
        try:
            fetched = httpx.get(entry["url"], timeout=ARTIFACT_TIMEOUT)
            fetched.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError(
                f"could not fetch the produced image: {exc}",
                code="PROVIDER_UNAVAILABLE",
                retryable=True,
            ) from exc
        content = fetched.content
    else:
        raise BackendError("xAI returned no image data", code="IMAGE_FAILED")

    return BackendResult(
        content=content,
        media_type=str(entry.get("mime_type") or "image/png"),
        payload={"model": request.model_id},
    )


def _video_body(request: BackendRequest) -> dict[str, Any]:
    """xAI's video request.

    `prompt` is required for text-to-video and optional for image-to-video, where
    the first frame comes from `image` — an **object**, not the `image_url` string
    the Python SDK accepts.
    """
    body: dict[str, Any] = {"model": request.model_id}
    prompt = str(request.params.get("prompt") or "")
    if prompt:
        body["prompt"] = prompt
    if request.params.get("reference_asset_url"):
        body["image"] = {"url": str(request.params["reference_asset_url"])}
    seconds = request.params.get("duration_seconds")
    if isinstance(seconds, (int, float)):
        body["duration"] = max(1, min(15, int(seconds)))  # documented range 1-15
    if request.params.get("aspect_ratio"):
        body["aspect_ratio"] = str(request.params["aspect_ratio"])
    resolution = str(request.params.get("resolution") or "")
    if resolution in _XAI_RESOLUTIONS:
        body["resolution"] = resolution
    return body


def _generate_video(request: BackendRequest) -> BackendResult:
    base = _base(request)
    headers = _headers(request)
    with httpx.Client(timeout=SUBMIT_TIMEOUT) as client:
        try:
            submitted = client.post(
                f"{base}/videos/generations", headers=headers, json=_video_body(request)
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
                f"xAI video submit HTTP {status}: {submitted.text[:200]}",
                code=code,
                status=status,
                retryable=status in {408, 425, 429} or status >= 500,
            )

        request_id = str((submitted.json() or {}).get("request_id") or "")
        if not request_id:
            raise BackendError("xAI returned no request_id", code="PROVIDER_FAILED")

        deadline = time.monotonic() + JOB_DEADLINE_S
        payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                polled = client.get(f"{base}/videos/{request_id}", headers=headers)
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"poll network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                ) from exc

            # 202 is xAI's documented "still in progress"; 200 carries a body whose
            # `status` decides the outcome — **including failure**, which arrives as
            # a 200. Branching on the HTTP status alone would read a failed
            # generation as a success.
            if polled.status_code == 202:
                time.sleep(POLL_INTERVAL_S)
                continue
            if polled.status_code == 404:
                raise BackendError(
                    f"xAI does not know request {request_id}", code="PROVIDER_FAILED"
                )
            if polled.status_code >= 400:
                raise BackendError(
                    f"xAI video poll HTTP {polled.status_code}: {polled.text[:200]}",
                    code="PROVIDER_FAILED",
                    retryable=polled.status_code >= 500,
                )

            payload = polled.json() or {}
            state = str(payload.get("status") or "").lower()
            if state == "done":
                break
            if state in {"failed", "expired"}:
                error = payload.get("error") or {}
                detail = error.get("message") or error.get("code") or state
                raise BackendError(f"xAI video {state}: {detail}", code="PROVIDER_FAILED")
            time.sleep(POLL_INTERVAL_S)
        else:
            raise BackendError(
                "xAI video did not finish in time",
                code="PROVIDER_SUBMISSION_UNCERTAIN",
                retryable=True,
            )

        video = payload.get("video") or {}
        url = str(video.get("url") or "")
        if not url:
            # Documented as empty when the output failed moderation.
            raise BackendError(
                "xAI returned no video url"
                + (" (the output was withheld by moderation)"
                   if video.get("respect_moderation") is False else ""),
                code="PROVIDER_FAILED",
            )

        # Fetched immediately: xAI documents the URL as ephemeral and states no
        # lifetime for the interactive flow, so waiting is not an option.
        try:
            artifact = client.get(url, timeout=ARTIFACT_TIMEOUT)
            artifact.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError(
                f"could not fetch the produced video: {exc}",
                code="PROVIDER_UNAVAILABLE",
                retryable=True,
            ) from exc

    return BackendResult(
        content=artifact.content,
        media_type=artifact.headers.get("content-type", "video/mp4").split(";")[0] or "video/mp4",
        payload={"provider_url": url, "model": request.model_id},
    )


class XaiBackend:
    """Chat through the OpenAI shim; image and video in xAI's own shapes."""

    def __init__(self) -> None:
        self._chat = OpenAICompatibleBackend()

    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality == "chat":
            return self._chat.generate(request)
        if request.modality == "image":
            return _generate_image(request)
        if request.modality == "video":
            return _generate_video(request)
        raise BackendError(
            f"xAI does not serve '{request.modality}'", code="MODEL_MODALITY_MISMATCH"
        )
