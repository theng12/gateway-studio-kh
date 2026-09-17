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

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .adapters import support_status
from .catalog import catalog
from .config import is_loopback
from .hub import hub_status
from .providers import registry
from .runtime import store, worker

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
def recent_jobs(limit: int = 25) -> dict[str, Any]:
    return {"jobs": store.recent(limit)}
