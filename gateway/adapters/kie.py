"""Kie.ai adapter — createTask, recordInfo, download.

Kie's flow is create → poll `recordInfo` → parse a `resultJson` **string**
containing the artifact URLs. That embedded-JSON step is the only unusual part;
everything else matches the standard async-task shape.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

POLL_INTERVAL_S = 2.0
JOB_DEADLINE_S = 900.0
SUBMIT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

_SUCCESS_STATES = {"success", "succeeded", "completed", "done"}
_FAILURE_STATES = {"fail", "failed", "error", "cancelled"}


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
    }


def _input_for(request: BackendRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if request.params.get("prompt") is not None:
        payload["prompt"] = str(request.params["prompt"])
    for key in (
        "aspect_ratio",
        "resolution",
        "duration_seconds",
        "seed",
        "image_url",
        "reference_asset_url",
        "negative_prompt",
    ):
        if request.params.get(key) is not None:
            payload[key] = request.params[key]
    return payload


def _urls_from_result_json(raw: Any) -> list[str]:
    """Kie returns `resultJson` as a JSON *string*, sometimes already parsed."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return []

    urls: list[str] = []
    for key in ("resultUrls", "result_urls", "urls"):
        value = parsed.get(key)
        if isinstance(value, list):
            urls.extend(str(u) for u in value if isinstance(u, (str,)))
    for key in ("resultUrl", "url", "videoUrl", "imageUrl"):
        if isinstance(parsed.get(key), str):
            urls.append(str(parsed[key]))
    return urls


class KieBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality not in {"image", "video", "music"}:
            raise BackendError(
                f"Kie does not serve '{request.modality}'", code="MODEL_MODALITY_MISMATCH"
            )

        base = (request.values.get("base_url") or request.base_url).rstrip("/")
        headers = _headers(request)
        body = {"model": request.model_id, "input": _input_for(request)}

        with httpx.Client(timeout=SUBMIT_TIMEOUT) as client:
            try:
                submitted = client.post(
                    f"{base}/api/v1/jobs/createTask", headers=headers, json=body
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
                    f"kie submit HTTP {status}: {submitted.text[:200]}",
                    code=code,
                    status=status,
                    retryable=status in {408, 425, 429} or status >= 500,
                )

            payload = submitted.json() or {}
            if payload.get("code") not in (200, "200", None):
                raise BackendError(
                    f"kie rejected submit: {payload.get('msg') or payload.get('message') or payload.get('code')}",
                    code="PROVIDER_FAILED",
                )

            task_id = str((payload.get("data") or {}).get("taskId") or "")
            if not task_id:
                raise BackendError("kie returned no taskId", code="PROVIDER_FAILED")

            deadline = time.monotonic() + JOB_DEADLINE_S
            result_json: Any = None
            while time.monotonic() < deadline:
                try:
                    polled = client.get(
                        f"{base}/api/v1/jobs/recordInfo",
                        headers=headers,
                        params={"taskId": task_id},
                        timeout=POLL_TIMEOUT,
                    )
                except httpx.HTTPError as exc:
                    raise BackendError(
                        f"poll network: {exc}", code="PROVIDER_UNAVAILABLE", retryable=True
                    ) from exc

                if polled.status_code >= 400:
                    raise BackendError(
                        f"kie status HTTP {polled.status_code}",
                        code="PROVIDER_FAILED",
                        retryable=polled.status_code >= 500,
                    )

                data = (polled.json() or {}).get("data") or {}
                state = str(data.get("state") or data.get("status") or "").lower()
                if state in _SUCCESS_STATES:
                    result_json = data.get("resultJson") or data.get("result")
                    break
                if state in _FAILURE_STATES:
                    raise BackendError(
                        f"kie task {state}: {data.get('failMsg') or data.get('failCode') or 'no detail'}",
                        code="PROVIDER_FAILED",
                    )
                time.sleep(POLL_INTERVAL_S)
            else:
                raise BackendError(
                    "kie task did not finish in time",
                    code="PROVIDER_SUBMISSION_UNCERTAIN",
                    retryable=True,
                )

            urls = _urls_from_result_json(result_json)
            if not urls:
                raise BackendError("kie returned no artifact url", code="PROVIDER_FAILED")

            try:
                artifact = client.get(urls[0], timeout=ARTIFACT_TIMEOUT)
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"artifact download failed: {exc}",
                    code="PROVIDER_UNAVAILABLE",
                    retryable=True,
                ) from exc

            if artifact.status_code >= 400:
                raise BackendError(f"artifact HTTP {artifact.status_code}", code="PROVIDER_FAILED")

        media_type = artifact.headers.get("content-type", "").split(";")[0].strip()
        if not media_type:
            media_type = {
                "image": "image/png",
                "video": "video/mp4",
                "music": "audio/mpeg",
            }.get(request.modality, "application/octet-stream")

        return BackendResult(
            content=artifact.content,
            media_type=media_type,
            payload={"kie_task_id": task_id, "provider_url": urls[0]},
        )
