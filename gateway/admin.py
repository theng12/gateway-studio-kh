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
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .adapters import support_status
from .catalog import catalog
from . import fleet
from .config import is_loopback
from .hub import hub_status
from .providers import registry
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
def list_providers() -> dict[str, Any]:
    items = []
    for described in registry.describe_all():
        definition = registry.definition(described["id"])
        supported, reason = support_status(definition) if definition else (False, "unknown")
        described["generation_supported"] = supported
        described["unsupported_reason"] = reason
        items.append(described)
    # Configured and enabled first: the list is long and the useful rows matter.
    items.sort(key=lambda p: (not p["enabled"], not p["configured"], p["display_name"].lower()))
    return {"providers": items}


@router.post("/api/providers/{provider_id}/credentials", dependencies=[Depends(require_admin)])
def set_credentials(provider_id: str, payload: dict[str, Any]) -> Any:
    if registry.definition(provider_id) is None:
        return _error(404, f"unknown provider '{provider_id}'")
    values = payload.get("values")
    if not isinstance(values, dict):
        return _error(422, "expected {'values': {field: value}}")
    registry.set_credentials(provider_id, {str(k): str(v) for k, v in values.items()})
    registry.reload_env()
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
    cached = registry.models.cached(provider_id)
    return {
        "models": registry.models.models_for(provider_id, include_disabled=True),
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
    rows: list[dict[str, Any]] = []
    for definition in registry.definitions():
        enabled_provider = registry.credentials.is_enabled(definition.id)
        for model in registry.models.models_for(definition.id, include_disabled=True):
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
    limit: int = 400,
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
    """
    matched = _filter_models(
        _flat_models(),
        q=payload.get("q"),
        modality=payload.get("modality"),
        provider_id=payload.get("provider_id"),
        only_enabled=False,
    )
    enabled = bool(payload.get("enabled"))
    return {"changed": _apply(matched, enabled), "matched": len(matched), "enabled": enabled}


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
    limit: int = 50,
    state: str | None = None,
    modality: str | None = None,
) -> dict[str, Any]:
    """Recent jobs, shaped for the app's Jobs window.

    Display names are resolved here rather than in the client, and the failure
    reason travels with the row: a job that failed is only useful if you can see
    why without opening a log file.
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
    try:
        row = fleet.add_controller(
            str(payload.get("label") or ""),
            str(payload.get("url") or ""),
            str(payload.get("token") or ""),
        )
    except ValueError as exc:
        return _error(400, str(exc))
    return {"controller": row, "controllers": fleet.controllers()}


@router.delete("/api/fleet/controllers/{controller_id}", dependencies=[Depends(require_admin)])
def fleet_remove_controller(controller_id: str) -> Any:
    if not fleet.remove_controller(controller_id):
        return _error(404, "No such controller.")
    return {"controllers": fleet.controllers()}


@router.post("/api/fleet/controllers/{controller_id}/test", dependencies=[Depends(require_admin)])
def fleet_test_controller(controller_id: str) -> Any:
    """Connection test: identity, role and release, straight from the controller."""
    row = next((c for c in fleet._read() if c.get("id") == controller_id), None)  # noqa: SLF001
    if row is None:
        return _error(404, "No such controller.")
    probed = fleet._probe(str(row["url"]), str(row.get("token") or ""))  # noqa: SLF001
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
    }
