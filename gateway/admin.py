"""Admin API — provider credentials, enable/disable, discovery, model curation.

This is the "in-app" surface: paste a key, discover that provider's models, turn
providers and individual models on or off, and see exactly what Story Studio
will be offered.

Authentication posture: the server binds loopback by default, so admin routes
are open from loopback and require the bearer token otherwise. That keeps the
browser experience click-free without leaving an unauthenticated control plane
on the LAN if someone changes `GATEWAY_HOST`. `is_loopback()` is the switch.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .activity import story_activity
from .adapters import serves, support_status
from .catalog import catalog
from . import fleet
from .config import is_loopback
from .hub import hub_status
from .providers import definitions, registry
from .runtime import STARTED_AT, store, worker
from .voices import search_library, voice_registry

router = APIRouter(prefix="/admin")

_UI_PATH = Path(__file__).resolve().parent / "admin_ui.html"


class AdminError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """Loopback is trusted; anything else must present the gateway key."""
    if is_loopback():
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AdminError(401, "Admin API requires a bearer token when not bound to loopback.")
    from .config import gateway_api_key

    expected = gateway_api_key()
    if expected and authorization[7:].strip() != expected:
        raise AdminError(403, "Invalid API key.")


# FastAPI needs the exception registered on the app, so the router returns a
# plain JSONResponse from handlers that can fail instead of raising a custom type.
def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    if not _UI_PATH.exists():
        return HTMLResponse("<h1>gateway-kh</h1><p>Admin UI file is missing.</p>", status_code=500)
    return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@router.get("/api/status", dependencies=[Depends(require_admin)])
def status() -> dict[str, Any]:
    providers = registry.describe_all()
    return {
        "hub": hub_status(),
        # Proof of life for the integration question: did Story Studio call this
        # gateway, when, and did it authenticate?
        "story_studio": story_activity(),
        "models": registry.models.summary(),
        "providers": {
            "total": len(providers),
            "configured": sum(1 for p in providers if p["configured"]),
            "enabled": sum(1 for p in providers if p["enabled"]),
        },
        "jobs_processed": worker.processed,
        "jobs": store.stats(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "catalog": {
            modality: len(catalog.rows(modality))
            for modality in ("chat", "image", "video", "tts", "transcription", "music")
        },
    }


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
@router.get("/api/providers", dependencies=[Depends(require_admin)])
def list_providers(scope: str | None = None) -> dict[str, Any]:
    """Cloud APIs and local runners are different things and are listed separately.

    `scope=cloud` is the provider console (a key and a model list per vendor);
    `scope=local` is the fleet (a machine, its studios and its running work).
    Both are still providers to the dispatch path — this is about how they are
    managed, not about who can generate.
    """
    items = []
    for described in registry.describe_all():
        if scope and described.get("scope") != scope:
            continue
        definition = registry.definition(described["id"])
        supported, reason = support_status(definition) if definition else (False, "unknown")
        described["generation_supported"] = supported
        described["unsupported_reason"] = reason
        # A provider can be supported *as a kind* and still declare a lane its
        # adapter cannot serve — Google declares six modalities and the adapter
        # serves three. The catalogue already withholds those rows, so the console
        # should be able to say which ones rather than leaving the owner to
        # discover it by enabling a model that never appears.
        described["unserved_modalities"] = (
            sorted(
                m for m in definition.modalities
                if not serves(definition, m)
            )
            if definition
            else []
        )
        items.append(described)
    # Configured and enabled first: the list is long and the useful rows matter.
    items.sort(key=lambda p: (not p["enabled"], not p["configured"], p["display_name"].lower()))
    return {
        "providers": items,
        # An entry in `providers.local.json` that was not loaded. The file is
        # hand-written, so a skip is otherwise indistinguishable from "my file did
        # not load": the provider is simply not in the list, and nothing says why.
        "skipped_custom_providers": list(definitions.skipped_custom_providers()),
        "scopes": {
            "cloud": sum(1 for p in registry.describe_all() if p.get("scope") == "cloud"),
            "local": sum(1 for p in registry.describe_all() if p.get("scope") == "local"),
        },
    }


@router.post("/api/providers/{provider_id}/credentials", dependencies=[Depends(require_admin)])
def set_credentials(provider_id: str, payload: dict[str, Any]) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    values = payload.get("values")
    if not isinstance(values, dict):
        return _error(422, "expected {'values': {field: value}}")
    registry.set_credentials(provider_id, {str(k): str(v) for k, v in values.items()})
    # No `reload_env()` here any more: `.env` is read live by `config.get`, so
    # there is no snapshot to invalidate and no way for the two readers of one
    # file to disagree about whether an edit had landed.
    described = registry.describe(provider_id)
    # Discovering immediately is what makes "paste key -> models appear" work
    # without a second click.
    if payload.get("discover", True) and described and described["configured"]:
        registry.refresh(provider_id)
    return {"provider": registry.describe(provider_id)}


@router.post("/api/providers/{provider_id}/enabled", dependencies=[Depends(require_admin)])
def set_enabled(provider_id: str, payload: dict[str, Any]) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    registry.set_enabled(provider_id, bool(payload.get("enabled")))
    return {"provider": registry.describe(provider_id)}


@router.delete("/api/providers/{provider_id}/credentials", dependencies=[Depends(require_admin)])
def clear_credentials(provider_id: str) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    registry.clear_credentials(provider_id)
    return {"provider": registry.describe(provider_id)}


@router.post("/api/providers/{provider_id}/refresh", dependencies=[Depends(require_admin)])
def refresh_provider(provider_id: str) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    result = registry.refresh(provider_id)
    return {
        "provider_id": provider_id,
        "discovered": len(result.models),
        "error": result.error,
    }


@router.post("/api/refresh-all", dependencies=[Depends(require_admin)])
def refresh_all() -> dict[str, Any]:
    return {"outcomes": registry.models.refresh_all()}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@router.get("/api/providers/{provider_id}/models", dependencies=[Depends(require_admin)])
def provider_models(provider_id: str) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    definition = registry.definition(provider_id)
    cached = registry.models.cached(provider_id)
    rows = registry.models.models_for(provider_id, include_disabled=True)
    return {
        # Whether each row could ever reach Story Studio. Without it the console
        # said "live" for models the catalogue silently drops — the same two
        # readers of one rule that `can_publish` exists to keep from drifting.
        "models": [
            {**model, "publishable": registry.models.can_publish(definition, model)}
            for model in rows
        ],
        "last_refresh": cached.get("fetched_at"),
        "last_error": cached.get("error"),
    }


@router.post("/api/providers/{provider_id}/models/enabled", dependencies=[Depends(require_admin)])
def set_models_enabled(provider_id: str, payload: dict[str, Any]) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    model_ids = payload.get("model_ids")
    if not isinstance(model_ids, list):
        return _error(422, "expected {'model_ids': [...], 'enabled': bool}")
    changed = registry.models.set_many(
        provider_id, [str(m) for m in model_ids], bool(payload.get("enabled"))
    )
    return {"changed": changed}


@router.post("/api/providers/{provider_id}/models", dependencies=[Depends(require_admin)])
def add_model(provider_id: str, payload: dict[str, Any]) -> Any:
    """Add a model a provider released but discovery cannot see."""
    definition = registry.definition(provider_id)
    if definition is None:
        return _error(404, f"unknown provider '{provider_id}'")
    model_id = str(payload.get("model_id") or "").strip()
    modality = str(payload.get("modality") or "")
    if not model_id or not modality:
        return _error(422, "expected {'model_id': ..., 'modality': ...}")
    try:
        registry.models.add_user_model(
            provider_id, model_id, str(payload.get("display_name") or model_id), modality
        )
    except ValueError as exc:
        return _error(422, str(exc))
    return {"models": registry.models.models_for(provider_id, include_disabled=True)}


@router.delete("/api/providers/{provider_id}/models/{model_id:path}", dependencies=[Depends(require_admin)])
def remove_model(provider_id: str, model_id: str) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    registry.models.remove_user_model(provider_id, model_id)
    return {"models": registry.models.models_for(provider_id, include_disabled=True)}


# ---------------------------------------------------------------------------
# Global model browser
#
# Discovery and availability are deliberately separate: a provider publishing
# 300 models should not put 300 rows into Story Studio's picker. These routes
# exist so the owner can search across every provider at once and switch models
# on, rather than opening eighteen panels to do it.
# ---------------------------------------------------------------------------
def _flat_models() -> list[dict[str, Any]]:
    """Every model the console can show, minus the ones that could never run.

    A model whose modality its provider's adapter does not serve is **not listed
    at all**. It could never be published, so offering it as something to tick is
    a control that does nothing — and the owner asked for exactly that to stop
    happening with OpenRouter's two Lyria rows, which its catalogue lists while
    publishing no music request path for them. The cache still holds them, so
    nothing discovered is lost; the console just stops advertising the impossible.
    """
    rows: list[dict[str, Any]] = []
    for definition in registry.definitions():
        enabled_provider = registry.credentials.is_enabled(definition.id)
        for model in registry.models.models_for(definition.id, include_disabled=True):
            if not registry.models.can_publish(definition, model):
                continue
            rows.append(
                {
                    "provider_id": definition.id,
                    "provider_name": definition.display_name,
                    "provider_enabled": enabled_provider,
                    "model_id": model["model_id"],
                    "display_name": model["display_name"],
                    "modality": model["modality"],
                    "source": model["source"],
                    "enabled": model["enabled"],
                }
            )
    return rows


def _filter_models(
    rows: list[dict[str, Any]],
    *,
    q: str | None,
    modality: str | None,
    provider_id: str | None,
    only_enabled: bool,
) -> list[dict[str, Any]]:
    if provider_id:
        rows = [r for r in rows if r["provider_id"] == provider_id]
    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in r["display_name"].lower() or needle in r["model_id"].lower()
        ]
    if only_enabled:
        rows = [r for r in rows if r["enabled"]]
    return rows


@router.get("/api/models", dependencies=[Depends(require_admin)])
def search_models(
    q: str | None = None,
    modality: str | None = None,
    provider_id: str | None = None,
    only_enabled: bool = False,
    limit: int = Query(400, ge=1, le=2000),
) -> dict[str, Any]:
    everything = _flat_models()
    matched = _filter_models(
        everything, q=q, modality=modality, provider_id=provider_id, only_enabled=only_enabled
    )
    return {
        "models": matched[:limit],
        "matched": len(matched),
        "returned": min(len(matched), limit),
        "total": len(everything),
        "enabled_total": sum(1 for r in everything if r["enabled"]),
        "modalities": sorted({r["modality"] for r in everything}),
        "providers": sorted(
            {(r["provider_id"], r["provider_name"]) for r in everything}  # type: ignore[arg-type]
        ),
    }


def _apply(
    rows: list[dict[str, Any]], enabled: bool
) -> int:
    by_provider: dict[str, list[str]] = {}
    for row in rows:
        by_provider.setdefault(row["provider_id"], []).append(row["model_id"])
    changed = 0
    for provider_id, model_ids in by_provider.items():
        changed += registry.models.set_many(provider_id, model_ids, enabled)
    return changed


@router.post("/api/models/enabled", dependencies=[Depends(require_admin)])
def set_models_enabled(payload: dict[str, Any]) -> Any:
    """Toggle an explicit list of models — what a checkbox click sends."""
    items = payload.get("items")
    if not isinstance(items, list):
        return _error(422, "expected {'items': [{'provider_id','model_id'}], 'enabled': bool}")
    rows = [
        {"provider_id": str(i["provider_id"]), "model_id": str(i["model_id"])}
        for i in items
        if isinstance(i, dict) and i.get("provider_id") and i.get("model_id")
    ]
    changed = _apply(rows, bool(payload.get("enabled")))
    return {"changed": changed, "enabled": bool(payload.get("enabled"))}


@router.post("/api/models/bulk", dependencies=[Depends(require_admin)])
def bulk_models(payload: dict[str, Any]) -> Any:
    """Enable or disable everything matching a filter.

    This is the "search for a family, click once" path: the same filters the
    browser uses, applied server-side so it covers every match rather than only
    the rows the page happened to load.

    **`only_enabled` is honoured**, and it has to be. It was hardcoded off here
    while the page sent it, so with "Enabled only" ticked the button said *"Disable
    all 23 matching"* and the request would have disabled **all 445** OpenRouter
    models — a bulk action doing far more than the sentence the owner agreed to.
    A confirmation that names a number must be the number acted on.

    **An unfiltered call is refused.** With no filter at all this matched every
    model in the catalogue and disabled all 1,624 of them; a single malformed
    request (`{}`) was enough, and it took the whole picker with it. A bulk action
    is only ever meant to act on a *set the owner described* — a search, a
    modality, a provider, or the enabled ones — so one of those has to be present.
    Doing it to everything is still possible, but it has to be said out loud with
    `scope: "all"`, because "I did not say which" and "I said all of them" must
    not look the same. That is the same rule the voice permission follows, and for
    the same reason.
    """
    described = any(
        payload.get(field) for field in ("q", "modality", "provider_id", "only_enabled")
    )
    if not described and payload.get("scope") != "all":
        return _error(
            400,
            "A bulk action has to say which models it means: pass a search, a "
            "modality, a provider, or only_enabled. To act on every model in the "
            "catalogue, pass scope: \"all\".",
        )
    matched = _filter_models(
        _flat_models(),
        q=payload.get("q"),
        modality=payload.get("modality"),
        provider_id=payload.get("provider_id"),
        only_enabled=bool(payload.get("only_enabled")),
    )
    enabled = bool(payload.get("enabled"))
    return {"changed": _apply(matched, enabled), "matched": len(matched), "enabled": enabled}


@router.get("/api/settings/retention", dependencies=[Depends(require_admin)])
def get_retention() -> dict[str, Any]:
    """How long received artifacts are kept, and how much is there now."""
    from . import retention

    return {**retention.settings(), "usage": retention.usage()}


@router.put("/api/settings/retention", dependencies=[Depends(require_admin)])
def set_retention(payload: dict[str, Any]) -> Any:
    """Change the limits. Whichever is reached first prunes."""
    from . import retention

    saved = retention.save(
        max_age_days=payload.get("max_age_days"),
        max_bytes=payload.get("max_bytes"),
    )
    return {**saved, "usage": retention.usage()}


@router.post("/api/artifacts/prune", dependencies=[Depends(require_admin)])
def prune_artifacts() -> dict[str, Any]:
    """Run the policy now, and report what it did rather than that it ran."""
    from . import retention

    return {**retention.prune(), "usage": retention.usage()}


# ---------------------------------------------------------------------------
# Voices
#
# Every provider models voices differently, so the registry does the unifying
# and these routes just expose it. Library search is live and uncached on
# purpose: ElevenLabs' public library holds tens of thousands of voices and the
# owner wants a handful, so browsing is a query rather than a download.
# ---------------------------------------------------------------------------
@router.get("/api/voices", dependencies=[Depends(require_admin)])
def list_voices(
    provider_id: str | None = None,
    q: str | None = None,
    source: str | None = None,
    only_enabled: bool = False,
) -> dict[str, Any]:
    voices = voice_registry()
    rows = [
        voice.as_dict() | {"enabled": voices.is_enabled(voice)}
        for voice in voices.voices(provider_id=provider_id)
    ]
    if source:
        rows = [r for r in rows if r["source"] == source]
    if only_enabled:
        rows = [r for r in rows if r["enabled"]]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in r["display_name"].lower()
            or needle in (r["description"] or "").lower()
            or needle in (r["language"] or "").lower()
        ]

    discovered = voice_registry().voices(provider_id=provider_id)
    return {
        "voices": rows,
        "matched": len(rows),
        "total": len(discovered),
        "enabled": sum(1 for v in discovered if voice_registry().is_enabled(v)),
        "summary": voice_registry().summary(),
        "providers": [
            {"id": d.id, "name": d.display_name, "kind": d.kind}
            for d in registry.definitions()
            if d.kind in {"elevenlabs", "fish", "studio_hub"}
        ],
    }


@router.get("/api/voices/{voice_id:path}/reference", dependencies=[Depends(require_admin)])
def voice_reference(voice_id: str) -> dict[str, Any]:
    """How one voice's clip stands against every local model that could use it.

    The question this answers is the one the app could not: *will my recording
    actually be used on this model, and if not, what happens instead?* Three
    different things can happen to a clip, and they look identical from the
    outside — it is used whole, the Hub picks a shorter aligned window out of it,
    or the job is refused. Silence about which is what makes a clone sound wrong
    with nothing to point at.

    Every number here is the Hub's own, read from its catalogue; nothing is
    derived from a model name or from a default. Where the Hub states nothing,
    the answer says so rather than guessing a window, because a plausible default
    is exactly what this codebase keeps auditing out.
    """
    from .fleet import publish_id
    from .providers.definitions import KIND_STUDIO_HUB
    from .voices import (
        ID_SEPARATOR,
        reference_contract,
        reference_support,
        resolve,
        voices_for_model,
    )

    voice = resolve(voice_id)
    if voice is None:
        # `_error`, not a raised HTTPException: that is the shape every other
        # route in this module answers with, and `HTTPException` is not imported
        # here at all — raising it was a NameError, which surfaced as a 500 for a
        # voice id that simply does not exist.
        return _error(404, f"no voice {voice_id!r}")

    raw = voice.raw or {}
    segments = [s for s in (raw.get("reference_segments") or []) if isinstance(s, dict)]
    transcript = str(raw.get("reference_transcript") or "").strip()
    duration = raw.get("duration_seconds")
    path = str(raw.get("reference_path") or "")

    # A provider voice — ElevenLabs', Fish's — has **no clip on this Mac at all**;
    # its audio lives with the provider and the provider applies its own rules.
    # Computing a verdict per local model from a clip that does not exist said
    # "used whole" for every one of them: a confident answer to a question nobody
    # asked, and exactly the kind of output the rest of this file exists to avoid.
    if not path and not segments:
        return {
            "voice_id": voice.id,
            "display_name": voice.display_name,
            "clip": {
                "present": False,
                "duration_seconds": duration,
                "speech_seconds": None,
                "segments": 0,
                "has_transcript": bool(transcript),
                "path": "",
            },
            "models": [],
            "note": (
                f"This voice has no reference clip on this Mac. It belongs to "
                f"{voice.provider_id} and is used on that provider's side, which "
                "applies its own rules — reference windows here govern local "
                "voices only."
            ),
        }
    # The Hub trims to speech before it compares against a window, so the span
    # the transcript covers is the honest measure of "will this be cut" — and it
    # is reported next to the clip length rather than instead of it.
    speech_span = None
    if segments:
        try:
            speech_span = round(max(float(s.get("end") or 0) for s in segments)
                                - min(float(s.get("start") or 0) for s in segments), 2)
        except (TypeError, ValueError):
            speech_span = None

    models: list[dict[str, Any]] = []
    for definition, model in registry.models.enabled_models("tts"):
        if definition.kind != KIND_STUDIO_HUB:
            continue
        raw_model = str(model.get("model_id"))
        support = reference_support(definition.id, raw_model)
        contract = reference_contract(definition.id, raw_model)
        maximum = contract.get("maximum_duration_seconds")
        minimum = contract.get("minimum_duration_seconds")
        needs_transcript = str(contract.get("transcript") or "").lower() == "required"

        verdict: str
        warning: str | None = None
        if support is False:
            verdict = "takes no reference clip"
        elif support is None:
            verdict = "limits unverified — the clip is passed through unchanged"
        elif isinstance(minimum, (int, float)) and speech_span is not None and speech_span < minimum:
            verdict = "refused"
            warning = (f"this voice's speech covers {speech_span}s, under this "
                       f"model's {minimum}s floor")
        elif needs_transcript and not transcript:
            verdict = "refused"
            warning = "this model needs a transcript and the voice has none"
        elif isinstance(maximum, (int, float)) and speech_span is not None and speech_span > maximum:
            verdict = f"the Hub picks a shorter aligned window (max {maximum}s)"
        elif isinstance(maximum, (int, float)):
            verdict = f"used whole (under this model's {maximum}s maximum)"
        else:
            verdict = "used whole"

        if not segments and support is not False:
            warning = warning or ("no aligned segments: a clip longer than this "
                                  "model's window cannot be cut to match its transcript")
        # The id Story Studio would ask with: locations publish under the maker,
        # so reporting the concrete `studio-hub-<controller>` here would name a
        # provider no picker ever sends. The voice count is asked the same way,
        # through the published id, so it exercises the real lookup path.
        published = f"{publish_id(definition.id)}{ID_SEPARATOR}{raw_model}"
        models.append({
            "model_id": published,
            "display_name": model.get("display_name") or raw_model,
            "support": support,
            "contract": contract,
            "verdict": verdict,
            "warning": warning,
            "voices": len(voices_for_model(published)),
        })

    return {
        "voice_id": voice.id,
        "display_name": voice.display_name,
        "clip": {
            "present": True,
            "duration_seconds": duration,
            "speech_seconds": speech_span,
            "segments": len(segments),
            "has_transcript": bool(transcript),
            "path": path,
        },
        "models": models,
    }


@router.get("/api/voices/library", dependencies=[Depends(require_admin)])
def voice_library(provider_id: str, q: str = "", page: int = 0) -> Any:
    """Live search of a provider's public voice library."""
    definition = registry.definition(provider_id)
    if definition is None:
        return _error(404, f"unknown provider '{provider_id}'")
    if not registry.credentials.is_configured(provider_id):
        return _error(409, f"no API key configured for '{provider_id}'")

    values = registry.credentials.resolved(provider_id)
    voices = search_library(definition, values, q, page)
    known = {v.provider_voice_id for v in voice_registry().voices(provider_id=provider_id)}
    return {
        "provider_id": provider_id,
        "voices": [
            v.as_dict() | {"already_owned": v.provider_voice_id in known, "enabled": False}
            for v in voices
        ],
        "count": len(voices),
        "page": page,
    }


@router.post("/api/voices/add", dependencies=[Depends(require_admin)])
def add_voices(payload: dict[str, Any]) -> Any:
    """Stock the catalogue from a folder the owner picked.

    Two shapes are accepted, because both are things people actually have: a folder
    of per-voice folders, and a folder of loose audio files. Each voice's clip is
    copied into the gateway's own library so the voice survives the source folder
    being moved, and then transcribed through Studio Hub to produce the segments
    Qwen3 Base requires. Named `add` rather than `import` because
    `POST /api/voices/import` already means something else here — copying a
    provider's library voice into the owner's account at that provider.
    """
    from . import voice_import

    folder = str(payload.get("folder") or "").strip()
    if not folder:
        return _error(400, "Pick a folder to add voices from.")
    result = voice_import.import_folder(Path(folder).expanduser())
    if not result["imported"] and not result["skipped"]:
        return _error(400, "That folder has no audio files in it.")
    if payload.get("annotate", True):
        result["annotation"] = voice_import.start_annotation(
            str(payload.get("transcription_model") or ""),
            force=bool(payload.get("force")),
        )
    return result


@router.get("/api/voices/add/status", dependencies=[Depends(require_admin)])
def voices_add_status() -> dict[str, Any]:
    """Progress of the transcription pass that makes added voices usable."""
    from . import voice_import

    return voice_import.annotation_state()


@router.post("/api/voices/shared", dependencies=[Depends(require_admin)])
def set_voices_shared(payload: dict[str, Any]) -> Any:
    """Grant or withdraw permission to clone and distribute these voices.

    An attestation, so it is explicit and per voice. Import leaves every voice
    private: the owner has voices he does not want distributed, and a default of
    "allowed" would distribute them by accident.
    """
    from . import voice_import

    items = payload.get("items")
    if not isinstance(items, list):
        return _error(422, "expected {'items': [voice_id, ...], 'acknowledged': bool}")
    result = voice_import.set_permission(
        [str(i) for i in items],
        bool(payload.get("acknowledged")),
        license=str(payload.get("license") or ""),
    )
    return result


@router.post("/api/voices/share", dependencies=[Depends(require_admin)])
def share_voices(payload: dict[str, Any]) -> Any:
    """Push voices the owner has allowed to every location.

    Controllers only: the gateway never talks to nodes, and each controller fans out
    to its own. A location needs its **Hub** token for this, not its fleet token —
    the Hub refuses these routes to a fleet token on purpose.
    """
    from . import voice_import

    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return _error(422, "expected {'items': [voice_id, ...]}")

    unshared = [
        v.get("display_name")
        for v in voice_import.voices()
        if voice_import.catalogue_id(v.get("id")) in {voice_import.catalogue_id(i) for i in items}
        and not v.get("permission_acknowledged")
    ]
    if unshared:
        # The permission is the owner's to grant, per voice, and this is the one
        # place it could be bypassed by asking to push something never allowed.
        return _error(
            403,
            "These voices have not been allowed for cloning and distribution: "
            + ", ".join(str(n) for n in unshared),
        )
    return voice_import.share([str(i) for i in items])


@router.put("/api/fleet/controllers/{controller_id}/hub-token", dependencies=[Depends(require_admin)])
def fleet_set_admin_token(controller_id: str, payload: dict[str, Any]) -> Any:
    """Store a controller's Hub token, used only to administer shared voices."""
    try:
        row = fleet.set_admin_token(controller_id, str(payload.get("token") or ""))
    except ValueError as exc:
        return _error(400, str(exc))
    if row is None:
        return _error(404, "No such controller.")
    return {"controller": row, "controllers": fleet.controllers()}


@router.get("/api/voices/shared", dependencies=[Depends(require_admin)])
def list_shared_voices() -> dict[str, Any]:
    """The voices that currently carry permission, and why any could not be pushed."""
    from . import voice_import

    entries = voice_import.shareable()
    return {
        "voices": [
            {
                "id": v.get("id"),
                "display_name": v.get("display_name"),
                "license": v.get("license"),
                "shared_id": voice_import.shared_voice_id(v),
                "readiness": voice_import.shared_readiness(v),
            }
            for v in entries
        ]
    }


@router.post("/api/voices/refresh", dependencies=[Depends(require_admin)])
def refresh_voices(provider_id: str | None = None) -> dict[str, Any]:
    voices = voice_registry()
    if provider_id:
        if registry.definition(provider_id) is None:
            return _error(404, f"unknown provider '{provider_id}'")
        return {"outcomes": {provider_id: voices.refresh(provider_id)}}
    return {"outcomes": voices.refresh_all()}


@router.post("/api/voices/enabled", dependencies=[Depends(require_admin)])
def set_voices_enabled(payload: dict[str, Any]) -> Any:
    items = payload.get("items")
    if not isinstance(items, list):
        return _error(422, "expected {'items': [voice_id, ...], 'enabled': bool}")
    enabled = bool(payload.get("enabled"))
    changed = sum(
        1
        for item in items
        if isinstance(item, str) and voice_registry().set_enabled(item, enabled)
    )
    return {"changed": changed, "enabled": enabled}


@router.post("/api/voices/import", dependencies=[Depends(require_admin)])
def import_voice(payload: dict[str, Any]) -> Any:
    """Copy a library voice into the owner's account so it can be used."""
    provider_id = str(payload.get("provider_id") or "")
    provider_voice_id = str(payload.get("provider_voice_id") or "")
    if not provider_id or not provider_voice_id:
        return _error(422, "expected {'provider_id', 'provider_voice_id'}")

    definition = registry.definition(provider_id)
    if definition is None:
        return _error(404, f"unknown provider '{provider_id}'")

    values = registry.credentials.resolved(provider_id)
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    name = str(payload.get("display_name") or provider_voice_id)

    if definition.kind == "elevenlabs":
        owner_id = str(payload.get("owner_id") or "")
        if not owner_id:
            return _error(422, "ElevenLabs needs the library voice's owner_id")
        try:
            response = httpx.post(
                f"{base}/v1/voices/add/{owner_id}/{provider_voice_id}",
                headers={"xi-api-key": values.get("api_key", ""),
                         "Content-Type": "application/json"},
                json={"new_name": name},
                timeout=httpx.Timeout(30.0, connect=8.0),
            )
        except httpx.HTTPError as exc:
            return _error(502, f"could not reach ElevenLabs: {exc}")
        if response.status_code >= 400:
            return _error(502, f"ElevenLabs rejected the import: {response.text[:200]}")
        payload_body = response.json() if response.content else {}
        added_id = str((payload_body or {}).get("voice_id") or provider_voice_id)
        voice = voice_registry().add_custom(
            provider_id, added_id, name, description="Imported from the voice library.",
            raw={"owner_id": owner_id},
        )
        return {"voice": voice.as_dict() | {"enabled": True}}

    # Fish voices are used directly by id — nothing to copy into an account.
    voice = voice_registry().add_custom(
        provider_id, provider_voice_id, name, description="Added from the voice library."
    )
    return {"voice": voice.as_dict() | {"enabled": True}}


@router.get("/api/voices/{voice_id:path}/preview", dependencies=[Depends(require_admin)])
def voice_preview(voice_id: str) -> Any:
    try:
        content, media_type = voice_registry().preview(voice_id)
    except KeyError as exc:
        return _error(404, f"no preview available: {exc}")
    if not content:
        return _error(404, "the provider returned no preview audio")
    return Response(content=content, media_type=media_type)


@router.delete("/api/voices/{voice_id:path}", dependencies=[Depends(require_admin)])
def forget_voice(voice_id: str) -> Any:
    removed = voice_registry().remove_custom(voice_id)
    if not removed:
        return _error(404, "that voice is discovered from the provider, not added here")
    return {"removed": voice_id}


# ---------------------------------------------------------------------------
# What Story Studio will actually see
# ---------------------------------------------------------------------------
@router.get("/api/catalog", dependencies=[Depends(require_admin)])
def catalog_preview(modality: str | None = None) -> dict[str, Any]:
    if modality:
        return {"modality": modality, "models": catalog.rows(modality), "freshness": catalog.freshness()}
    return {
        "modalities": {
            m: [{"id": r["id"], "display_name": r["display_name"]} for r in catalog.rows(m)]
            for m in ("chat", "image", "video", "tts", "transcription", "music")
        },
        "freshness": catalog.freshness(),
    }


@router.get("/api/jobs", dependencies=[Depends(require_admin)])
def list_jobs(
    limit: int = Query(50, ge=1, le=500),
    state: str | None = None,
    modality: str | None = None,
) -> dict[str, Any]:
    """Recent jobs, shaped for the app's Jobs window.

    Display names are resolved here rather than in the client, and the failure
    reason travels with the row: a job that failed is only useful if you can see
    why without opening a log file.

    **`limit` is bounded, and it has to be**: it is bound straight into SQL as
    `LIMIT ?`, so `?limit=99999999999999999999999` raised
    `OverflowError: Python int too large to convert to SQLite INTEGER` and
    answered **500** — measured on the live gateway, and the same defect as the
    one already fixed on `/v1/account/jobs`. `limit=-1` was quieter and worse:
    that is SQLite's "no limit", so the Jobs window could be handed every job
    ever recorded. The app asks for 200, so 500 is headroom rather than a wall.
    """
    labels: dict[tuple[str, str], str] = {}
    for definition in registry.definitions():
        for model in registry.models.models_for(definition.id, include_disabled=True):
            labels[(definition.id, model["model_id"])] = model["display_name"]

    TERMINAL = {"succeeded", "failed", "cancelled"}
    rows: list[dict[str, Any]] = []
    for job in store.list_jobs(limit=limit, state=state, modality=modality):
        provider_id = str(job.get("provider_id") or "")
        model_id = str(job.get("model_id") or "")
        definition = registry.definition(provider_id)
        created = job.get("created_at")
        updated = job.get("updated_at")
        duration = (
            round(updated - created, 2)
            if created and updated and job["state"] in TERMINAL
            else None
        )
        rows.append(
            {
                "id": job["id"],
                "short_id": job["id"][:8],
                "modality": job["modality"],
                "provider_id": provider_id,
                "provider_name": definition.display_name if definition else provider_id,
                "model_id": model_id,
                "model_name": labels.get((provider_id, model_id), model_id),
                "state": job["state"],
                "created_at": created,
                "updated_at": updated,
                "duration_s": duration,
                "has_artifact": bool(job.get("asset_path")),
                "error_code": job.get("error_code"),
                "error_detail": job.get("error_detail"),
            }
        )

    return {"jobs": rows, "stats": store.stats()}


# --------------------------------------------------------------------------
# Fleet — the location controllers this gateway aggregates for Story Studio


@router.get("/api/fleet", dependencies=[Depends(require_admin)])
def fleet_snapshot() -> dict[str, Any]:
    """Every location, its machines and its in-flight work.

    Read-only and safe to poll: it fans out to each controller with a per-request
    timeout and reports an unreachable one as unreachable rather than failing the
    whole read. One dead location must not hide the others.
    """
    return fleet.snapshot()


@router.get("/api/fleet/controllers", dependencies=[Depends(require_admin)])
def fleet_controllers() -> dict[str, Any]:
    return {"controllers": fleet.controllers()}


@router.post("/api/fleet/controllers", dependencies=[Depends(require_admin)])
def fleet_add_controller(payload: dict[str, Any]) -> Any:
    # Strings only. `str(7)` is "7", which is a valid-looking hostname, so a number
    # sent by mistake used to become a real location at `http://7` — complete with a
    # credential, a place in every probe, and a row to delete by hand.
    for field in ("label", "url", "token"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return _error(400, f"'{field}' must be text.")
    try:
        row = fleet.add_controller(
            str(payload.get("label") or ""),
            str(payload.get("url") or ""),
            str(payload.get("token") or ""),
        )
    except ValueError as exc:
        return _error(400, str(exc))
    # Discover the new location's models as part of adding it. Nothing refreshes
    # models on a timer, so without this an added controller simply shows none, and
    # "no models" reads as "the location has nothing" rather than "nobody has asked
    # it yet". Discovery is not availability: these arrive disabled, as everything
    # from a remote provider must.
    discovered: int | None = None
    discovery_error: str | None = None
    provider_id = fleet.provider_id_for(str(row.get("id") or ""))
    try:
        result = registry.refresh(provider_id)
        discovered, discovery_error = len(result.models), result.error
    except Exception as exc:  # noqa: BLE001 - the location is added either way
        discovery_error = f"{type(exc).__name__}: {exc}"
    # Check the credential as part of adding, not later: both of a controller's
    # credentials reach every route this gateway calls, so an over-privileged one
    # behaves perfectly and is never noticed. Reported now, while the token is
    # still in the person's hand.
    return {
        "controller": row,
        "credential": fleet.credential_scope_for(str(row.get("id") or "")),
        "discovered": discovered,
        "discovery_error": discovery_error,
        "controllers": fleet.controllers(),
    }


@router.put("/api/fleet/controllers/{controller_id}/credential", dependencies=[Depends(require_admin)])
def fleet_set_credential(controller_id: str, payload: dict[str, Any]) -> Any:
    """Replace a location's credential without touching anything else."""
    try:
        row = fleet.set_token(controller_id, str(payload.get("token") or ""))
    except ValueError as exc:
        return _error(400, str(exc))
    if row is None:
        return _error(404, "No such controller.")
    # A different credential can legitimately see different models — a fleet token
    # scoped to fewer routes, or one minted after the controller gained a model.
    discovered: int | None = None
    try:
        discovered = len(registry.refresh(fleet.provider_id_for(controller_id)).models)
    except Exception:  # noqa: BLE001 - replacing the credential is the job here
        pass
    return {
        "controller": row,
        "credential": fleet.credential_scope_for(controller_id),
        "discovered": discovered,
        "controllers": fleet.controllers(),
    }


@router.delete("/api/fleet/controllers/{controller_id}", dependencies=[Depends(require_admin)])
def fleet_remove_controller(controller_id: str) -> Any:
    if not fleet.remove_controller(controller_id):
        return _error(404, "No such controller.")
    return {"controllers": fleet.controllers()}


@router.patch("/api/fleet/controllers/{controller_id}", dependencies=[Depends(require_admin)])
def fleet_rename_controller(controller_id: str, payload: dict[str, Any]) -> Any:
    """Rename a location -- any location, at any time.

    Only the label changes. The controller id, its address, its token and whether
    it is enabled are all untouched, so renaming can never require re-entering a
    credential. `local` is accepted here too: this Mac sits in the same list and
    is renamed the same way.
    """
    try:
        row = fleet.rename_controller(controller_id, str(payload.get("label") or ""))
    except ValueError as exc:
        return _error(400, str(exc))
    if row is None:
        return _error(404, "No such controller.")
    return {"controller": row, "controllers": fleet.controllers()}


@router.post("/api/fleet/controllers/{controller_id}/test", dependencies=[Depends(require_admin)])
def fleet_test_controller(controller_id: str) -> Any:
    """Connection test: identity, role and release, straight from the controller."""
    row = next((c for c in fleet._read() if c.get("id") == controller_id), None)  # noqa: SLF001
    if row is None:
        return _error(404, "No such controller.")
    # The credential comes from the store, not the roster: the roster deliberately
    # holds no secrets, so reading a token off the row probed with an empty one and
    # reported every correctly configured controller as unreachable.
    probed = fleet._probe(str(row["url"]), fleet._token_for(controller_id))  # noqa: SLF001
    return {
        "id": controller_id,
        "url": row["url"],
        "reachable": probed["reachable"],
        "error": probed["error"],
        "role": probed["role"],
        "version": probed["version"],
        "machines": len(probed["machines"]),
        "nodes": len(probed["nodes"]),
        "latency_ms": probed["latency_ms"],
        # Only when it answered: an unreachable controller has no scope to report,
        # and a second doomed request would just add the same timeout to the wait.
        "credential": (
            fleet.credential_scope(str(row["url"]), fleet._token_for(controller_id))  # noqa: SLF001
            if probed["reachable"]
            else {"scope": "unknown", "detail": "Not reachable, so the credential was not checked."}
        ),
    }
