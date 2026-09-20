"""Kie.ai adapter — createTask, recordInfo, download.

Kie's flow is create → poll `recordInfo` → parse a `resultJson` **string**
containing the artifact URLs. That embedded-JSON step is the only unusual part;
everything else matches the standard async-task shape.

**Music is the same route with a different api-model name.** Kie's Suno family
used to be a dedicated `/api/v1/generate*` API, and it has since moved onto the
unified Market route this adapter already speaks: the top-level `model` is the
constant `ai-music-api/generate` and the **Suno tier** (`V6`, `V6_MINI`,
`V6_WILD`) travels inside `input.model`. Measured live on 2026-09-20: that body
queued a task, `recordInfo` reported `state: "success"` and a `resultJson`
carrying a `data` **array of tracks** with `audio_url`, and the whole thing cost
12 credits. Sending the tier as the top-level `model` instead is what an earlier
version of this file did — the provider answers *"The model name you specified is
not supported"*, because `V6` is not an api-model name there.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .base import Artifact, BackendError, BackendRequest, BackendResult

POLL_INTERVAL_S = 2.0
JOB_DEADLINE_S = 900.0
SUBMIT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
POLL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ARTIFACT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

_SUCCESS_STATES = {"success", "succeeded", "completed", "done"}
_FAILURE_STATES = {"fail", "failed", "error", "cancelled"}

#: Used only when the download answers no `content-type` at all. Music is an mp3
#: (measured: the track url ends `.mp3` and serves `audio/mpeg`).
_DEFAULT_MEDIA = {
    "image": "image/png",
    "video": "video/mp4",
    "music": "audio/mpeg",
}


def _headers(request: BackendRequest) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {request.values.get('api_key', '')}",
        "Content-Type": "application/json",
    }


#: The api-model name Kie's music lane lives under. The **Suno tier** goes in
#: `input.model`; this name is the route's own and is the same for every tier.
MUSIC_API_MODEL = "ai-music-api/generate"

#: Music input fields forwarded unchanged. Kie spells them snake_case; a caller may
#: send either spelling, and only the provider's spelling goes on the wire.
_MUSIC_FIELDS: dict[str, tuple[str, ...]] = {
    "style": ("style",),
    "title": ("title",),
    "negative_tags": ("negative_tags", "negativeTags"),
    "vocal_gender": ("vocal_gender", "vocalGender"),
    "style_weight": ("style_weight", "styleWeight"),
    "weirdness_constraint": ("weirdness_constraint", "weirdnessConstraint"),
    "audio_weight": ("audio_weight", "audioWeight"),
    "persona_id": ("persona_id", "personaId"),
    "persona_model": ("persona_model", "personaModel"),
    "duration": ("duration", "duration_seconds"),
}


def _music_input(request: BackendRequest) -> dict[str, Any]:
    """The Suno `input` block, from this gateway's music contract.

    The music lane names the words `prompt`/`lyrics` and the knob `instrumental`;
    Suno calls the lyrics `prompt`, takes `custom_mode`, and only honours
    `style`/`title` in custom mode. `lyrics` wins over `prompt` when both are
    present because the lyrics field is the specific one.
    """
    params = request.params
    payload: dict[str, Any] = {"model": request.model_id}
    text = params.get("lyrics")
    if text is None:
        text = params.get("prompt")
    if text is not None:
        payload["prompt"] = str(text)
    if params.get("instrumental") is not None:
        payload["instrumental"] = bool(params["instrumental"])

    for wire_key, spellings in _MUSIC_FIELDS.items():
        for spelling in spellings:
            if params.get(spelling) is not None:
                payload[wire_key] = params[spelling]
                break

    # **Which of Suno's two modes this is.** The caller's own `custom_mode` always
    # wins. Without one, `style`/`title` are the tell: both are documented as
    # custom-mode-only fields, so a request carrying either is a custom-mode
    # request and one carrying only a prompt is Suno's simpler described-song mode.
    # (The old dedicated API spelled this `customMode`; this one spells it
    # `custom_mode`, so reading the payload beats defaulting to one spelling.)
    explicit = params.get("custom_mode")
    if explicit is None:
        explicit = params.get("customMode")
    if explicit is not None:
        payload["custom_mode"] = bool(explicit)
    else:
        payload["custom_mode"] = bool(payload.get("style") or payload.get("title"))
    return payload


def _input_for(request: BackendRequest) -> dict[str, Any]:
    if request.modality == "music":
        return _music_input(request)
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
    else:
        parsed = raw
    # A string that parses to a list or a null — `"[]"`, `"null"` — is a shape a
    # provider can legitimately return when it produced no result. It used to
    # reach `.get()` and raise `AttributeError`, which the worker turned into a
    # failure whose detail read like an internal crash rather than "the provider
    # returned nothing to download".
    if not isinstance(parsed, dict):
        return []

    urls: list[str] = []
    for key in ("resultUrls", "result_urls", "urls"):
        value = parsed.get(key)
        if isinstance(value, list):
            urls.extend(str(u) for u in value if isinstance(u, (str,)))
    for key in ("resultUrl", "url", "videoUrl", "imageUrl"):
        if isinstance(parsed.get(key), str):
            urls.append(str(parsed[key]))
    # **Music answers a list of tracks, not a url key.** Measured live: the parsed
    # `resultJson` for `ai-music-api/generate` is
    # `{"code": 200, "data": [{audio_url, stream_audio_url, image_url, id, …}, …]}`
    # — one entry per variation Suno made. None of the keys above exist, so the job
    # would have reported "no artifact url" on a *successful* generation.
    #
    # ponytail: only the **first** track is kept, because a job holds one artifact
    # and every other music lane here answers one url. Ceiling — a caller cannot
    # reach the second variation. Upgrade path — store the array and serve
    # `/v1/music/jobs/{id}/outputs/{n}`, which the route already takes an index for.
    data = parsed.get("data")
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for key in ("audio_url", "video_url", "image_url", "resultUrl", "url"):
                if isinstance(entry.get(key), str) and entry[key]:
                    urls.append(str(entry[key]))
                    break
    return urls


class KieBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality not in {"image", "video", "music"}:
            raise BackendError(
                f"Kie does not serve '{request.modality}'", code="MODEL_MODALITY_MISMATCH"
            )

        base = (request.values.get("base_url") or request.base_url).rstrip("/")
        headers = _headers(request)
        # **Music names its api-model, not the tier.** `ai-music-api/generate` is
        # what the route accepts at the top level; the Suno tier (`V6`, `V6_MINI`,
        # `V6_WILD`) rides inside `input.model`. Sending the tier at the top level
        # is answered *"The model name you specified is not supported"* — measured
        # live on 2026-09-20, which is how this line came to be written.
        api_model = MUSIC_API_MODEL if request.modality == "music" else request.model_id
        body = {"model": api_model, "input": _input_for(request)}

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

            # **Every url is downloaded, not just the first.** Suno answers a `data`
            # array holding one entry per variation, and fetching only `urls[0]` meant
            # the second track was parsed and then thrown away, with no way for a
            # caller to reach it. The *first* must still succeed, because that is the
            # artifact the single-artifact routes serve and the guarantee the old
            # behaviour made; a later variation that will not fetch is counted in
            # `variations_failed` instead of failing a call that did produce a usable
            # track.
            downloaded: list[tuple[bytes, str | None]] = []
            failed = 0
            for index, url in enumerate(urls):
                try:
                    fetched = client.get(url, timeout=ARTIFACT_TIMEOUT)
                except httpx.HTTPError as exc:
                    if index == 0:
                        raise BackendError(
                            f"artifact download failed: {exc}",
                            code="PROVIDER_UNAVAILABLE",
                            retryable=True,
                        ) from exc
                    failed += 1
                    continue

                if fetched.status_code >= 400:
                    if index == 0:
                        raise BackendError(
                            f"artifact HTTP {fetched.status_code}", code="PROVIDER_FAILED"
                        )
                    failed += 1
                    continue

                media_type = fetched.headers.get("content-type", "").split(";")[0].strip()
                downloaded.append(
                    (
                        fetched.content,
                        media_type or _DEFAULT_MEDIA.get(request.modality, "application/octet-stream"),
                    )
                )

        payload: dict[str, Any] = {
            "kie_task_id": task_id,
            "provider_url": urls[0],
            # How many variations the provider *offered*, so "this model makes one
            # track" and "we dropped the rest" cannot read the same.
            "variation_count": len(urls),
        }
        if failed:
            payload["variations_failed"] = failed

        first_content, first_media = downloaded[0]
        return BackendResult(
            content=first_content,
            media_type=first_media,
            payload=payload,
            additional_artifacts=[Artifact(c, m) for c, m in downloaded[1:]],
        )
