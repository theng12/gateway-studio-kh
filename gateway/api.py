"""gateway-kh HTTP surface — the contract Story Studio KH already speaks.

Story Studio needs **zero code changes**: point its `genstudio_url` at this
service and its model pickers fill up. So this module mirrors GenStudio KH's
client contract, including the parts that look like vestigial commerce.

Four contract rules are easy to violate and expensive to debug:

  * **Never return 402.** Story Studio holds a credit lock keyed on the API key.
    If it ever sees 402 and a balance is never published, that hold never
    clears -- the key is dead permanently. A provider running out of funds is a
    settled `failed` job here, never insufficient credits.
  * **Artifact URLs must be same-origin with the configured base**, or the
    client rejects them. We return relative paths for this reason.
  * **Every settled job carries a balanced `0/0/0` settlement triple.** This is
    not display data: Story Studio advances its LLM lineup to the next candidate
    only when it can prove the failed attempt cost nothing.
  * **`freshness` must be true.** `stale: true` disables every model for new
    work, so it is computed from the last catalogue rebuild, not from process
    start.

Generation runs on a worker thread (see `worker.py`); this module only accepts
work and reports on it, so a long local job never blocks a Story Studio poll.
"""

from __future__ import annotations


import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .adapters import BackendError, BackendRequest, adapter_for
from .catalog import MODALITIES, catalog
from .config import ARTIFACT_DIR, gateway_api_key, is_loopback
from .constants import ADMISSION, CREDIT_BALANCE, ZERO_PURCHASE, ZERO_SETTLEMENT
from .providers import registry
from . import fleet
from . import retention
from .activity import is_story_path, record
from .runtime import store, worker
from .voices import story_row, voice_registry, voices_for_model

API_KEY = gateway_api_key()

# Uploaded audio for transcription quotes, held between the quote and its
# confirm. In memory on purpose: it is a single-process gateway and the payload
# is a transient request body, not state worth persisting.
_UPLOADS: dict[str, tuple[bytes | None, str, str, float, str]] = {}
#: A staged audio upload, held between the quote and the confirm.
#:
#: `content` is `None` for a quote that carried a model but no audio — the
#: price-only call. It is stored rather than left out so that confirm can tell
#: "you never sent audio with this quote" (400, and the caller can fix it) from
#: "this quote has expired" (410, and Story Studio must re-quote). Both used to
#: be 410, which sent the caller to re-quote a request that would fail again.
#: The model is stored with it because the quote's whole purpose is to name the
#: model that will run; confirm used to ignore it and run the first enabled
#: transcription row instead — a different model, possibly a different provider,
#: than the one quoted and confirmed.
_UPLOAD_TTL_S = 3600.0


@asynccontextmanager
async def lifespan(_: FastAPI):
    worker.start()
    # Keeps the fleet snapshot warm so no request waits on an unreachable site.
    fleet.start_refresher()
    # The gateway only *receives* artifacts; Story Studio keeps the copy that
    # matters. Without this the directory grew without bound.
    retention.start_pruner()
    # Cached models belonging to a location that is no longer on the roster. The
    # cache outlived the locations: six `studio-hub-*` providers were still in it
    # with no controllers configured, one carrying a switched-on model. Inert,
    # but a stale enable/disable decision must never be inherited by a location
    # that did not make it.
    try:
        fleet.reconcile_cached_locations()
    except Exception:  # noqa: BLE001
        # A tidy-up must never stop the gateway from starting.
        pass
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="gateway-kh", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Record what the Story surface is asked for, so "is Story Studio connected?"
# is answerable from the gateway rather than by archaeology in another app's
# encrypted state. The ring itself lives in `activity.py` — see that module for
# why it is not here.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _record_story_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    path = request.url.path
    if is_story_path(path):
        record(
            request.method,
            path,
            response.status_code,
            bool(request.headers.get("authorization")),
        )
    return response


# ---------------------------------------------------------------------------
# Errors — Story Studio reads exactly two fields: `detail` and optional `code`.
# ---------------------------------------------------------------------------
class GatewayError(Exception):
    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        self.status = status
        self.detail = detail
        self.code = code


@app.exception_handler(GatewayError)
async def _gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail}
    if exc.code:
        body["code"] = exc.code
    return JSONResponse(status_code=exc.status, content=body)


# ---------------------------------------------------------------------------
# Auth. Scopes are deliberately ignored: this is a single-owner gateway, not a
# multi-tenant API, and Story Studio only uses scopes for user-facing hints.
# ---------------------------------------------------------------------------
def require_auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayError(401, "Missing bearer token.", "UNAUTHORIZED")
    token = authorization[7:].strip()
    if not token:
        raise GatewayError(401, "Empty bearer token.", "UNAUTHORIZED")
    if API_KEY and token != API_KEY:
        raise GatewayError(403, "Invalid API key.", "FORBIDDEN")


def _require_idempotency_key(value: str | None) -> str:
    if not value:
        raise GatewayError(
            422, "Idempotency-Key header is required.", "IDEMPOTENCY_KEY_REQUIRED"
        )
    if not 8 <= len(value) <= 128:
        raise GatewayError(
            422, "Idempotency-Key must be 8..128 characters.", "IDEMPOTENCY_KEY_INVALID"
        )
    return value


def _load_job(job_id: str, modality: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None or job["modality"] != modality:
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return job


def _result_of(job: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(job.get("result_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _job_envelope(job: dict[str, Any]) -> dict[str, Any]:
    modality = job["modality"]
    result = _result_of(job)
    payload = result.get("payload") or {}

    body: dict[str, Any] = {
        "id": job["id"],
        "state": job["state"],
        "model_id": catalog_encode(job),
        "failure_code": None,
        "failure_class": None,
        "failure_reason": None,
        "routing_status_code": None,
        "queue_position": None,
        "queue_eta_seconds": None,
        "queue_reason": None,
        "retry_after_seconds": None,
        "retry_at": None,
        "poll_after_ms": 1000,
        **ZERO_SETTLEMENT,
    }

    if job["state"] == "succeeded":
        if modality == "chat":
            body["output_text"] = result.get("text") or ""
            body["finish_reason"] = payload.get("finish_reason") or "stop"
        elif modality == "image":
            url = f"/v1/images/{job['id']}/file"
            body["image_url"] = url
            body["output_url"] = url
        elif modality == "tts":
            url = f"/v1/jobs/{job['id']}/audio"
            body["audio_url"] = url
            body["output_url"] = url
        elif modality == "transcription":
            url = f"/v1/transcriptions/{job['id']}/srt"
            body["srt_url"] = url
            body["output_url"] = url
        elif modality == "video":
            url = f"/v1/videos/{job['id']}/file"
            body["video_url"] = url
            body["output_url"] = url
        elif modality == "music":
            # Story Studio reads music results as an array of tracks, each with
            # its own asset id, so a single-artifact shape would not be read.
            url = f"/v1/music/jobs/{job['id']}/outputs/0"
            body["outputs"] = [{"asset_id": "0", "url": url}]
            body["output_url"] = url
        if payload.get("usage"):
            body["usage"] = payload["usage"]

    if job["state"] == "failed":
        body["failure_code"] = job.get("error_code") or "PROVIDER_FAILED"
        body["failure_class"] = "provider_error"
        body["failure_reason"] = job.get("error_detail") or "generation failed"

    return body


def catalog_encode(job: dict[str, Any]) -> str:
    """Rebuild the namespaced id Story Studio sent, so its bookkeeping matches.

    Which location actually ran the job is deliberately not part of this. The job
    stores the concrete location it was routed to, but the id Story Studio is told
    back has to be the id it sent, or a saved preset stops matching its own jobs.
    `publish_id` collapses a location back to the maker's namespace, which is the
    namespace the catalogue published.
    """
    from . import fleet
    from .catalog import encode_model_id

    provider_id = job.get("provider_id") or ""
    model_id = job.get("model_id") or ""
    if not provider_id:
        return model_id
    return encode_model_id(fleet.publish_id(str(provider_id)), model_id)


def _pinned_location(params: dict[str, Any]) -> str | None:
    """The location a request already names, if it names one.

    A voice id carries its owner (`studio-hub-<controller>::<raw>`), so voice work
    must go to that location and no other — the reference clip exists on one Hub,
    not on the fleet. A voice from a cloud provider names no location and so pins
    nothing.
    """
    from . import fleet

    for key in ("voice_id", "voice_reference_source_id", "voice_library_id"):
        location_id = fleet.location_for_voice(str(params.get(key) or ""))
        if location_id:
            return location_id
    return None


def _submit(
    modality: str,
    compound_id: str,
    params: dict[str, Any],
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Resolve a catalogue id and queue a job.

    Resolution is against *enabled* models, so a disabled toggle is honoured on
    the generation path and the catalogue and dispatch can never disagree.
    """
    definition, model = catalog.resolve(
        modality, compound_id, only_location=_pinned_location(params)
    )
    if definition is None or model is None:
        raise GatewayError(
            404,
            f'Model "{compound_id}" is not available on this gateway.',
            "MODEL_NOT_FOUND",
        )
    # An idempotency key identifies one request, and a request has a modality.
    # The replay lookup matched on the key alone, so posting to
    # `/v1/images/generations` with a key already used on `/v1/chat/responses`
    # returned the *chat* job's envelope with HTTP 200 — the image was never
    # made, and the caller was told to poll a chat job. The read routes already
    # treat a cross-modality key as a mismatch (404); the write path has to
    # refuse it rather than answer with another modality's work.
    if idempotency_key:
        existing = store.find_by_idempotency_key(idempotency_key)
        if existing is not None and existing["modality"] != modality:
            raise GatewayError(
                409,
                f"That Idempotency-Key was already used for a {existing['modality']} request. "
                f"A key identifies one request; use a new one for this {modality} request.",
                "IDEMPOTENCY_KEY_REUSED",
            )
    return store.submit(
        modality=modality,
        model_id=model["model_id"],
        provider_id=definition.id,
        params=params,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    providers = registry.describe_all()
    return {
        "status": "ok",
        "service": "gateway-kh",
        # The app adopts a gateway it did not start (quitting the app leaves the
        # service running, deliberately). Reporting the pid is what lets it
        # still stop that process on request rather than only pretending to.
        "pid": os.getpid(),
        "providers": {
            "total": len(providers),
            "configured": sum(1 for p in providers if p["configured"]),
            "enabled": sum(1 for p in providers if p["enabled"]),
        },
        "models": registry.models.summary(),
        "jobs_processed": worker.processed,
    }


@app.get("/api/version")
def version() -> dict[str, Any]:
    return {"version": __version__, "service": "gateway-kh"}


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
@app.get("/v1/account/overview", dependencies=[Depends(require_auth)])
def account_overview() -> dict[str, Any]:
    return {
        **ADMISSION,
        "credit_balance": CREDIT_BALANCE,
        "credits_used_30d": 0,
        "currency": "credits",
        "tier": "owner",
        "lifetime_paid_cents": 0,
        "service_tier": "owner",
        "admission": dict(ADMISSION),
        "capacity_verified": True,
        "limit_source": "gateway",
    }


@app.get("/v1/account/jobs", dependencies=[Depends(require_auth)])
def account_jobs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)
) -> dict[str, Any]:
    # Bounded, because an unbounded `int` query parameter is not a small thing:
    # FastAPI parses arbitrary-precision Python ints, and `page_size` went
    # straight into a SQL `LIMIT ?`, so `?page_size=99999999999999999999999`
    # raised `OverflowError: Python int too large to convert to SQLite INTEGER`
    # — a 500, from a URL. `page_size=-1` was worse in a quieter way: that is
    # SQLite's "no limit", so a caller could ask for every job ever recorded.
    rows = store.recent(page_size)
    return {
        "items": [
            {
                "id": r["id"],
                "operation": r["modality"],
                "state": r["state"],
                "model_id": catalog_encode(r),
                **ZERO_SETTLEMENT,
            }
            for r in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": len(rows),
    }


@app.get("/v1/account/credit-activity", dependencies=[Depends(require_auth)])
def credit_activity(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    return {"items": [], "page": page, "page_size": page_size, "total": 0}


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
@app.get("/v1/models", dependencies=[Depends(require_auth)])
def models_registry() -> dict[str, Any]:
    # Consumed by nothing in Story Studio's frontend; served for completeness.
    return {"models": catalog.all_rows(), "freshness": catalog.freshness()}


def _catalog_handler(modality: str):
    def handler() -> dict[str, Any]:
        return catalog.envelope(modality)

    return handler


for _modality, _path in (
    ("chat", "/v1/chat/models"),
    ("image", "/v1/image/models"),
    ("video", "/v1/videos/models"),
    ("tts", "/v1/tts/models"),
    ("music", "/v1/music/models"),
    ("transcription", "/v1/transcription/models"),
):
    app.add_api_route(
        _path,
        _catalog_handler(_modality),
        methods=["GET"],
        dependencies=[Depends(require_auth)],
        name=f"catalog_{_modality}",
    )


@app.get("/v1/voices", dependencies=[Depends(require_auth)])
def voices(
    model_id: str | None = None,
    collection: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Voices usable with a TTS model.

    Only *enabled* voices are returned — discovery and availability are separate
    here exactly as they are for models, because a provider's public library can
    hold tens of thousands of voices.

    Which voices match a model depends on the provider, and the two rules are
    genuinely different: a local cloning voice belongs to one specific model,
    while a provider's voices work with every TTS model that provider offers.
    `voices_for_model` encodes both.
    """
    rows = (
        voices_for_model(model_id)
        if model_id
        else [story_row(v) for v in voice_registry().enabled_voices()]
    )
    if search:
        term = search.lower()
        rows = [
            r
            for r in rows
            if term in r["display_name"].lower() or term in (r["description"] or "").lower()
        ]
    return {"voices": rows, "models": rows}


@app.post("/v1/voices/{voice_id}/save", dependencies=[Depends(require_auth)])
def save_voice(voice_id: str) -> dict[str, Any]:
    return {"id": voice_id, "is_saved": True}


@app.delete("/v1/voices/{voice_id}/save", dependencies=[Depends(require_auth)])
def unsave_voice(voice_id: str) -> dict[str, Any]:
    return {"id": voice_id, "is_saved": False}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/v1/chat/responses", dependencies=[Depends(require_auth)])
def create_chat_response(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("chat", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/chat/responses/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _job_envelope(job)


@app.get("/v1/chat/responses/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def chat_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "chat":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/chat/responses/{job_id}", dependencies=[Depends(require_auth)])
def get_chat_response(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "chat"))


@app.post("/v1/chat/responses/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_chat_response(job_id: str) -> dict[str, Any]:
    _load_job(job_id, "chat")
    return _job_envelope(store.cancel(job_id))


@app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    """Synchronous rollback route.

    Story Studio only selects this when
    `STORY_STUDIO_GENSTUDIO_CHAT_TRANSPORT=completions`. It blocks for the whole
    generation, which is exactly why the durable route is the default -- but it
    is implemented so switching transports does not dead-end.
    """
    model_id = str(payload.get("model") or payload.get("model_id") or "")
    definition, model = catalog.resolve("chat", model_id)
    if definition is None or model is None:
        raise GatewayError(404, f'Model "{model_id}" is not available.', "MODEL_NOT_FOUND")

    request = BackendRequest(
        provider_id=definition.id,
        model_id=model["model_id"],
        modality="chat",
        params=payload,
        values=registry.credentials.resolved(definition.id),
        base_url=definition.base_url,
        display_name=definition.display_name,
    )
    try:
        result = adapter_for(definition).generate(request)
    except BackendError as exc:
        # Never 402: a drained provider account must not lock Story Studio's key.
        raise GatewayError(502, str(exc), exc.code) from exc
    except Exception as exc:  # noqa: BLE001
        # Catch *everything*, not just the errors we predicted.
        #
        # This is the only synchronous route — the durable one runs in a worker
        # that already settles any exception as a failed job — so an adapter bug
        # is the one place where "the provider sent something odd" leaves the
        # gateway as a 500, and a 500 from the rollback transport is a wall Story
        # Studio cannot get past. A provider answering 200 with a non-JSON body,
        # or a JSON list instead of an object, is enough: `response.json()` and
        # the `.get` right after it are both unguarded upstream.
        raise GatewayError(
            502, f"Provider '{definition.display_name}' could not be read: {exc}", "BACKEND_ERROR"
        ) from exc

    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model["model_id"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text or ""},
                "finish_reason": (result.payload or {}).get("finish_reason") or "stop",
            }
        ],
        "usage": (result.payload or {}).get("usage"),
    }


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
@app.post("/v1/images/admission/permits", dependencies=[Depends(require_auth)])
def acquire_permit(payload: dict[str, Any]) -> dict[str, Any]:
    import uuid

    return {
        "id": str(uuid.uuid4()),
        "model_id": payload.get("model_id"),
        "operation": payload.get("operation"),
        "expires_at": None,
    }


@app.post("/v1/images/generations", dependencies=[Depends(require_auth)])
def create_image(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("image", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/images/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _job_envelope(job)


@app.get("/v1/images/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def image_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "image":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/images/{job_id}", dependencies=[Depends(require_auth)])
def get_image_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "image"))


def _artifact_response(job: dict[str, Any], default_media: str) -> Response:
    if job["state"] != "succeeded" or not job.get("asset_path"):
        raise GatewayError(409, "Job is not complete.", "JOB_NOT_COMPLETE")
    path = Path(job["asset_path"])
    if not path.exists():
        raise GatewayError(410, "Artifact is gone.", "ARTIFACT_GONE")
    return Response(content=path.read_bytes(), media_type=job.get("media_type") or default_media)


@app.get("/v1/images/{job_id}/file", dependencies=[Depends(require_auth)])
def get_image_file(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "image"), "image/png")


@app.post("/v1/images/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_image_job(job_id: str) -> dict[str, Any]:
    _load_job(job_id, "image")
    return _job_envelope(store.cancel(job_id))


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
@app.post("/v1/videos/generations", dependencies=[Depends(require_auth)])
def create_video(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("video", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/videos/{job['id']}"
        response.headers["Retry-After"] = "2"
    return _job_envelope(job)


@app.get("/v1/videos/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def video_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "video":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/videos/{job_id}", dependencies=[Depends(require_auth)])
def get_video_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "video"))


@app.get("/v1/videos/{job_id}/file", dependencies=[Depends(require_auth)])
def get_video_file(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "video"), "video/mp4")


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
def _voice_tier(voice_id: Any) -> str:
    """The tier of the voice the quote names, in the catalogue's own words.

    A quote used to answer a flat `"standard"` for every voice, while the voice
    catalogue publishes `custom` for the owner's own stock and `library` for a
    provider's public library — so the same voice was described two ways
    depending on which endpoint answered. An unknown or absent voice keeps
    `standard`, which is what the caller is actually asking about in that case.
    """
    from . import voices as voices_module

    if not voice_id:
        return "standard"
    found = voices_module.voice_registry().find(str(voice_id))
    if found is None:
        return "standard"
    return "custom" if found.source != voices_module.SOURCE_LIBRARY else "library"


@app.post("/v1/tts/quotes", dependencies=[Depends(require_auth)])
def tts_quote(payload: dict[str, Any]) -> dict[str, Any]:
    model_id = str(payload.get("model_id") or "")
    definition, model = catalog.resolve("tts", model_id)
    if definition is None or model is None:
        raise GatewayError(404, f'TTS model "{model_id}" is not available.', "MODEL_NOT_FOUND")
    text = str(payload.get("text") or "")
    # The limit comes from the same function that builds the catalogue row, so the
    # two cannot disagree. This used to be a flat 40,000 — the FIGURE the
    # catalogue had already stopped publishing: it over-promised the local lane by
    # 60% and Fish by eight times, and it meant Story Studio validated a narration
    # against a limit that contradicted the model row it came from.
    #
    # It cannot be read off the catalogue row here: `catalog.resolve` returns the
    # *inventory* model (raw provider fields), not the published row.
    from .catalog import _TTS_CHARACTERS_DEFAULT, _tts_characters

    limit = _tts_characters(str(model.get("model_id") or ""), model, definition)
    if not isinstance(limit, int) or limit <= 0:
        limit = _TTS_CHARACTERS_DEFAULT
    return {
        "model_id": model_id,
        "model_name": model["display_name"],
        "voice_id": payload.get("voice_id"),
        "voice_tier": _voice_tier(payload.get("voice_id")),
        "billable_characters": len(text),
        "speed": payload.get("speed", 1.0),
        "estimated_duration_ms": int(max(len(text) / 14.0, 0.5) * 1000),
        "maximum_input_characters": limit,
        "controls": payload.get("controls", {}),
        "credits": 0,
        "maximum_credits": 0,
        "credits_per_1000_characters": 0,
        "price_version": "flat",
        "output_format": "wav",
        # Published EXPLICITLY: the parser does `payload?.chargeable === true`,
        # so an absent field renders a claim the gateway never made.
        "chargeable": True,
        "generation_enabled": True,
        "notice": None,
    }


@app.post("/v1/audio/jobs", dependencies=[Depends(require_auth)])
def create_tts_job(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("tts", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/jobs/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _job_envelope(job)


@app.get("/v1/jobs/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def tts_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "tts":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_tts_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "tts"))


@app.get("/v1/jobs/{job_id}/audio", dependencies=[Depends(require_auth)])
def get_tts_audio(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "tts"), "audio/wav")


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_tts_job(job_id: str) -> dict[str, Any]:
    _load_job(job_id, "tts")
    return _job_envelope(store.cancel(job_id))


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------
@app.post("/v1/music/generations", dependencies=[Depends(require_auth)])
def create_music(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("music", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/music/jobs/{job['id']}"
        response.headers["Retry-After"] = "2"
    return _job_envelope(job)


@app.get("/v1/music/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def music_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "music":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/music/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_music_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "music"))


@app.get("/v1/music/jobs/{job_id}/outputs/{asset_id}", dependencies=[Depends(require_auth)])
def get_music_output(job_id: str, asset_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "music"), "audio/mpeg")


# ---------------------------------------------------------------------------
# Transcription — quote (multipart upload) -> confirm -> poll.
# ---------------------------------------------------------------------------
def _prune_uploads(now: float) -> None:
    for key in [k for k, v in _UPLOADS.items() if now - v[3] > _UPLOAD_TTL_S]:
        _UPLOADS.pop(key, None)


@app.post("/v1/audio/transcriptions/quotes", dependencies=[Depends(require_auth)])
async def transcription_quote(
    request: Request,
    file: UploadFile | None = File(default=None),
    model: str | None = Form(default=None),
) -> dict[str, Any]:
    """Accept the audio upload and quote it.

    Story Studio uploads the media here (multipart) and only later confirms, so
    the bytes are held between the two calls.

    A JSON body is also accepted, and it is a **price-only** quote: it names a
    model and holds no audio, so the confirm for it will refuse for lack of audio
    rather than pretending the quote expired. It used to be advertised as "for
    callers that upload separately", which was never true — there is no route
    that uploads separately, so every confirm of a JSON quote answered 410 and
    the caller's only option was to re-quote the same way and fail again.
    """
    import uuid

    quote_id = str(uuid.uuid4())
    body_model = model
    if file is None and request.headers.get("content-type", "").startswith("application/json"):
        # A body that cannot be read is the caller's problem, and it has to be
        # answered as one. `await request.json()` raises on an empty body or
        # malformed JSON, and unhandled that became a **500** — for a request that
        # is merely incomplete, and on the endpoint Story Studio uses to send audio.
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any unreadable body is a bad request
            raise GatewayError(
                400,
                "Expected a JSON body with a model, or an audio file to upload.",
                "INVALID_REQUEST",
            )
        if not isinstance(payload, dict):
            raise GatewayError(400, "Expected a JSON object.", "INVALID_REQUEST")
        body_model = str(body_model or payload.get("model") or "")
        _prune_uploads(now := time.time())
        _UPLOADS[quote_id] = (None, "", "", now, body_model)
        return {
            "id": quote_id,
            "quoted_credit_cost": 0,
            "price_version": "flat",
            "expires_at": None,
            "model": body_model,
        }

    if file is not None:
        content = await file.read()
        _prune_uploads(now := time.time())
        _UPLOADS[quote_id] = (
            content,
            file.filename or "audio.mp3",
            file.content_type or "application/octet-stream",
            now,
            str(body_model or ""),
        )

    return {
        "id": quote_id,
        "quoted_credit_cost": 0,
        "price_version": "flat",
        "expires_at": None,
        "model": body_model,
    }


@app.post(
    "/v1/audio/transcriptions/quotes/{quote_id}/confirm",
    dependencies=[Depends(require_auth)],
)
async def confirm_transcription(
    quote_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    model: str | None = Form(default=None),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    upload = _UPLOADS.get(quote_id)
    # An expired quote is a typed terminal condition for Story Studio (410),
    # which forces it to re-quote rather than retry a job it cannot satisfy.
    if upload is None:
        raise GatewayError(
            410, "transcription quote has expired", "TRANSCRIPTION_QUOTE_EXPIRED"
        )

    content, name, content_type, _, _quoted_model = upload
    # The model may arrive as a form field or a JSON body. It has to be read from
    # both: `model` is declared as a Form field, and Starlette returns an *empty*
    # form for a non-form content type, so a JSON confirm silently fell through to
    # the fallback and ran a model the caller never asked for.
    if model is None and request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an unreadable body just means "no model given"
            body = None
        if isinstance(body, dict) and body.get("model"):
            model = str(body["model"])

    if content is None:
        raise GatewayError(
            400,
            "That quote carries a price but no audio. Upload the audio with the "
            "quote — POST /v1/audio/transcriptions/quotes with the file — then confirm.",
            "TRANSCRIPTION_AUDIO_MISSING",
        )

    # Resolve the model from the quote's own model when the confirm omits it, so
    # the model that runs is the model that was quoted.
    model_id = _quote_model(quote_id, model)
    if not model_id:
        raise GatewayError(
            404, "No transcription model is available.", "MODEL_NOT_FOUND"
        )
    definition, _ = catalog.resolve("transcription", model_id)
    if definition is None:
        raise GatewayError(
            404, f'Model "{model_id}" is not available.', "MODEL_NOT_FOUND"
        )

    # Job params are persisted as JSON, so raw audio cannot travel inside them.
    # Stage the upload on disk and pass a path; the adapter reads it back. This
    # also survives a gateway restart mid-queue, which an in-memory handoff
    # would not.
    upload_dir = ARTIFACT_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f"{quote_id}{Path(name).suffix or '.bin'}"
    staged.write_bytes(content)

    payload = {
        "_upload_path": str(staged),
        "_upload_name": name,
        "_upload_type": content_type,
    }
    job, created = _submit("transcription", model_id, payload, key)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/transcriptions/{job['id']}"
    return _job_envelope(job)


def _quote_model(quote_id: str, asked: str | None) -> str:
    """The model this quote will actually run.

    The quote's own model wins, because that is the model the caller was quoted
    and the one it will confirm. Only a quote that named none falls back, and the
    fallback is the first *enabled* transcription row — not merely the first row,
    which could be one the owner has switched off.
    """
    stored = _UPLOADS.get(quote_id)
    if asked:
        return asked
    if stored and stored[4]:
        return stored[4]
    rows = catalog.rows("transcription")
    return str(rows[0]["id"]) if rows else ""


@app.get("/v1/transcriptions/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def transcription_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "transcription":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/transcriptions/{job_id}", dependencies=[Depends(require_auth)])
def get_transcription_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "transcription"))


@app.get("/v1/transcriptions/{job_id}/srt", dependencies=[Depends(require_auth)])
def get_transcription_srt(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "transcription"), "application/x-subrip")


# ---------------------------------------------------------------------------
# Content Store — commerce half answered with zero constants.
#
# Everything on this gateway is pre-owned and free, so a purchase is a no-op
# reporting a successful zero-credit acquisition. The fields are strict integers
# because Story Studio refuses a purchase payload missing any of them.
# ---------------------------------------------------------------------------
@app.get("/v1/content/account", dependencies=[Depends(require_auth)])
def content_account() -> dict[str, Any]:
    return {"credit_balance": CREDIT_BALANCE, "entitlements": [], "purchases": []}


@app.post("/v1/content/quotes", dependencies=[Depends(require_auth)])
def content_quote(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": payload.get("product_id"),
        "credits": 0,
        "discount_credits": 0,
        "price_version": "flat",
        "sufficient_credits": True,
        "owned": True,
    }


@app.post("/v1/content/purchases", dependencies=[Depends(require_auth)])
def content_purchase(payload: dict[str, Any]) -> dict[str, Any]:
    return {**ZERO_PURCHASE, "product_id": payload.get("product_id"), "state": "completed"}


@app.get("/v1/content/receipts", dependencies=[Depends(require_auth)])
def content_receipts() -> dict[str, Any]:
    return {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------
from .admin import AdminError  # noqa: E402 - mounted after app setup
from .admin import router as admin_router  # noqa: E402

app.include_router(admin_router)


@app.exception_handler(AdminError)
async def _admin_error_handler(_: Request, exc: AdminError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
