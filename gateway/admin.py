"""Admin API — provider credentials, enable/disable, discovery, model curation.

This is the "in-app" surface: paste a key, discover that provider's models, turn
providers and individual models on or off, and see exactly what Story Studio
will be offered.

Authentication posture: the server binds loopback by default, so admin routes
are open from loopback and require the bearer token otherwise. That keeps the
browser experience click-free without leaving an unauthenticated control plane
on the LAN. Both the configured bind and actual request peer are checked.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .activity import story_activity
from .adapters import BackendError, BackendRequest, adapter_for, serves, support_status
from .catalog import MODALITIES, catalog
from . import fleet
from . import release_publisher
from .config import is_loopback, request_is_loopback
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
    if is_loopback() and request_is_loopback(request):
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AdminError(401, "Admin API requires a bearer token when not bound to loopback.")
    from .config import gateway_api_key

    expected = gateway_api_key()
    if not expected:
        raise AdminError(503, "Set GATEWAY_API_KEY before using the gateway beyond loopback.")
    if authorization[7:].strip() != expected:
        raise AdminError(403, "Invalid API key.")


# FastAPI needs the exception registered on the app, so the router returns a
# plain JSONResponse from handlers that can fail instead of raising a custom type.
def _error(status: int, detail: str, code: str | None = None) -> JSONResponse:
    """A refusal the console can act on.

    `code` is carried through where there is one, because the console and Story
    Studio must read the same vocabulary for the same refusal — a 404 that says
    `MODEL_NOT_FOUND` on `/v1` and nothing on `/admin` is one fact spelled two ways,
    and the caller that has to branch on it is the one that gets less.
    """
    body: dict[str, Any] = {"detail": detail}
    if code:
        body["code"] = code
    return JSONResponse(status_code=status, content=body)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_ui() -> HTMLResponse:
    if not _UI_PATH.exists():
        return HTMLResponse("<h1>gateway-kh</h1><p>Admin UI file is missing.</p>", status_code=500)
    # **`no-store`, because the page is read from disk on every request and a cached
    # copy is a UI that cannot be seen to have changed.** The app hosts this in a
    # WKWebView, so without it a reload after an edit can serve the previous page and
    # a fix looks like it did not apply — which is exactly how a styling change gets
    # reported as broken twice.
    return HTMLResponse(
        _UI_PATH.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


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
        # **A free lane is a trade, not just a price.** The console shows this
        # beside the provider so "free" never silently means "your prompts may train
        # someone's model" — the owner has private voices, and this is the one place
        # the difference is visible. `unverified` is published as itself rather than
        # omitted: "nobody has checked" must not render as "safe".
        described["data_use"] = (definition.data_use if definition else "unverified")
        described["free_tier"] = bool(definition.free_tier) if definition else False
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
def _is_free_lane(definition: Any, model_id: str) -> bool:
    """Whether a row is a provider's free offer, read from what is stated.

    Two cases, and nothing guessed between them: the provider **is** the free tier
    (`free_tier`, set on the definition), or the id carries the suffix its provider
    uses to mark a free route — `:free` on OpenRouter and Token Harbor, `-free` on
    Fish.

    A model that is merely cheap, or on a provider whose trial credits happen to be
    unspent, is **not** free, and calling it that would be a number the owner acts
    on. That is why this reads a declaration rather than inferring from a price.
    """
    if definition.free_tier:
        return True
    lowered = str(model_id or "").lower()
    return lowered.endswith(":free") or lowered.endswith("-free")


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
                    # **The trade travels with the row.** A free model's privacy
                    # stance is its provider's, so the picker shows it without a
                    # second lookup, and both facts come from one place so they
                    # cannot disagree.
                    "free_lane": _is_free_lane(definition, model["model_id"]),
                    "data_use": definition.data_use,
                }
            )
    return rows


#: How the browser may order rows. `modality` **is** the operation a model performs —
#: the axis the owner shops by ("LLM, image, video, transcription"), so it sorts as a
#: whole tier rather than a badge to scan. Every key falls through to a stable,
#: human-readable tiebreak so two rows never swap places between refreshes.
_MODEL_SORTS: dict[str, Any] = {
    "modality": lambda r: (r["modality"], r["display_name"].lower(), r["provider_name"].lower()),
    "name": lambda r: (r["display_name"].lower(), r["provider_name"].lower(), r["modality"]),
    "provider": lambda r: (r["provider_name"].lower(), r["modality"], r["display_name"].lower()),
    "status": lambda r: (not r["enabled"], r["modality"], r["display_name"].lower()),
}


def _filter_models(
    rows: list[dict[str, Any]],
    *,
    q: str | None = None,
    modality: str | None = None,
    provider_id: str | None = None,
    source: str | None = None,
    status: str | None = None,
    lane: str | None = None,
    privacy: str | None = None,
    provider_lane: str | None = None,
    only_enabled: bool = False,
) -> list[dict[str, Any]]:
    """The model browser's one filter, shared by the browse route and the bulk route.

    An unrecognised value for any filter is a no-op rather than a refusal: the
    console sends `""` for "any", and a filter that means "everything" must not be
    the one thing that empties the page. `only_enabled` is the older spelling of
    `status=enabled`, kept because the bulk route's own test and a saved query both
    still speak it.
    """
    if status is None and only_enabled:
        status = "enabled"
    if provider_id:
        rows = [r for r in rows if r["provider_id"] == provider_id]
    if modality:
        rows = [r for r in rows if r["modality"] == modality]
    if source:
        rows = [r for r in rows if r["source"] == source]
    if status == "enabled":
        rows = [r for r in rows if r["enabled"]]
    elif status == "disabled":
        rows = [r for r in rows if not r["enabled"]]
    if lane == "free":
        rows = [r for r in rows if r["free_lane"]]
    elif lane == "paid":
        rows = [r for r in rows if not r["free_lane"]]
    if privacy:
        rows = [r for r in rows if r["data_use"] == privacy]
    if provider_lane == "on":
        rows = [r for r in rows if r["provider_enabled"]]
    elif provider_lane == "off":
        rows = [r for r in rows if not r["provider_enabled"]]
    if q:
        needle = q.strip().lower()
        rows = [
            r
            for r in rows
            if needle in r["display_name"].lower() or needle in r["model_id"].lower()
        ]
    return rows


def _counts(
    rows: list[dict[str, Any]], value: Any, universe: tuple[str, ...] | list[str] = ()
) -> dict[str, int]:
    """Count a dimension, with every value in `universe` present even at zero.

    A key that is simply absent and a key that is zero are the same thing only if
    the reader already knows the full value set — and the whole point of a facet is
    that the reader does not. So the universe is stated: "no free lanes match this"
    is published as `0`, never as a missing key that renders as an empty chip.
    """
    counts = {str(key): 0 for key in universe}
    for row in rows:
        key = str(value(row))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _model_facets(everything: list[dict[str, Any]], **filters: Any) -> dict[str, Any]:
    """Chip counts for each filter, each computed with *its own* filter removed.

    A facet that counted the set it also filters would read zero for every value
    once one was chosen — the operation chips would tell the owner there is no
    image the moment they look at image. Excluding only the dimension being counted
    keeps every other choice in force, so each number says what picking that chip
    *would* show. A dimension with no active filter is unaffected.
    """

    def subset(exclude: str) -> list[dict[str, Any]]:
        narrowed = {k: v for k, v in filters.items() if k != exclude}
        return _filter_models(everything, **narrowed)

    status_rows = subset("status")
    lane_rows = subset("lane")
    provider_rows = subset("provider_lane")
    return {
        # The operation set is whatever the catalogue actually holds, so a chip that
        # appears is one the console can filter to. Source and privacy are fixed
        # vocabularies, so they keep their shape even when a value matches nothing.
        "modality": _counts(
            subset("modality"), lambda r: r["modality"], sorted({r["modality"] for r in everything})
        ),
        "source": _counts(
            subset("source"), lambda r: r["source"], ("curated", "discovered", "user")
        ),
        "privacy": _counts(
            subset("privacy"), lambda r: r["data_use"], ("non_private", "private", "unverified")
        ),
        "status": {
            "enabled": sum(1 for r in status_rows if r["enabled"]),
            "disabled": sum(1 for r in status_rows if not r["enabled"]),
        },
        "lane": {
            "free": sum(1 for r in lane_rows if r["free_lane"]),
            "paid": sum(1 for r in lane_rows if not r["free_lane"]),
        },
        "provider_lane": {
            "on": sum(1 for r in provider_rows if r["provider_enabled"]),
            "off": sum(1 for r in provider_rows if not r["provider_enabled"]),
        },
    }


@router.get("/api/models", dependencies=[Depends(require_admin)])
def search_models(
    q: str | None = None,
    modality: str | None = None,
    provider_id: str | None = None,
    source: str | None = None,
    status: str | None = None,
    lane: str | None = None,
    privacy: str | None = None,
    provider_lane: str | None = None,
    only_enabled: bool = False,
    sort: str | None = None,
    order: str = "asc",
    limit: int = Query(400, ge=1, le=2000),
) -> dict[str, Any]:
    everything = _flat_models()
    filters: dict[str, Any] = {
        "q": q,
        "modality": modality,
        "provider_id": provider_id,
        "source": source,
        "status": status,
        "lane": lane,
        "privacy": privacy,
        "provider_lane": provider_lane,
    }
    matched = _filter_models(everything, only_enabled=only_enabled, **filters)
    # **Sort before slicing, not after.** With more models than one page holds, a
    # client-side sort could only reorder the rows it was given — "sort by operation"
    # would order a page that was chosen without regard to operation. The full match
    # is ordered first, then the page is cut.
    descending = order == "desc"
    if sort in _MODEL_SORTS:
        matched = sorted(matched, key=_MODEL_SORTS[sort], reverse=descending)
    return {
        "models": matched[:limit],
        "matched": len(matched),
        "returned": min(len(matched), limit),
        # **Not `total`.** `/health` publishes `total_models`, which is a different
        # question — models belonging to providers that are switched on, publishable
        # or not — and one word answering two questions is how a reader ends up
        # comparing two numbers that were never about the same set. This one is
        # exactly what the browser can list: every model whose provider and adapter
        # could actually serve it, whether or not it is ticked.
        "listable_total": len(everything),
        "enabled_total": sum(1 for r in everything if r["enabled"]),
        "modalities": sorted({r["modality"] for r in everything}),
        "sources": sorted({r["source"] for r in everything}),
        "privacy_values": sorted({r["data_use"] for r in everything}),
        "providers": sorted(
            {(r["provider_id"], r["provider_name"]) for r in everything}  # type: ignore[arg-type]
        ),
        # Echoed so the page can show what it actually asked for rather than what it
        # meant to ask for — an ignored sort would otherwise look applied.
        "sort": sort if sort in _MODEL_SORTS else None,
        "order": "desc" if descending else "asc",
        "facets": _model_facets(everything, **filters),
    }


def _apply(
    rows: list[dict[str, Any]], enabled: bool
) -> int:
    by_provider: dict[str, list[str]] = {}
    for row in rows:
        by_provider.setdefault(row["provider_id"], []).append(row["model_id"])
    changed = 0
    for provider_id, model_ids in by_provider.items():
        # A row can only reach here from `_flat_models`, whose ids are concrete.
        # This guard is for the *other* route into `_apply` — the explicit-items
        # endpoint, which a client can hand any string. `studio-hub` is the
        # published namespace Story Studio reads; it is not a provider, and
        # enablement written under it would never be swept.
        if registry.definition(provider_id) is None:
            continue
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
        payload.get(field)
        for field in (
            "q",
            "modality",
            "provider_id",
            "source",
            "status",
            "lane",
            "privacy",
            "provider_lane",
            "only_enabled",
        )
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
        source=payload.get("source"),
        status=payload.get("status"),
        lane=payload.get("lane"),
        privacy=payload.get("privacy"),
        provider_lane=payload.get("provider_lane"),
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

    result = retention.prune()
    if result.get("blocked_reason"):
        return _error(503, result["blocked_reason"], "RETENTION_PROTECTION_UNAVAILABLE")
    return {**result, "usage": retention.usage()}


@router.get("/api/artifacts/kept", dependencies=[Depends(require_admin)])
def list_kept_artifacts() -> dict[str, Any]:
    """Every pinned artifact, each saying whether its file is still on disk."""
    from . import retention

    return {"artifacts": retention.kept(), "usage": retention.usage()}


@router.post("/api/artifacts/keep", dependencies=[Depends(require_admin)])
def set_artifact_kept(payload: dict[str, Any]) -> Any:
    """Pin or release one artifact of one job.

    Addressed by **job and asset id**, not by path: a path is an implementation
    detail, and the asset id is exactly the one the job envelope publishes, so the
    control names the thing the owner can see rather than something they cannot.
    """
    from . import retention
    from .runtime import store
    from .store import artifacts_of

    job_id = str(payload.get("job_id") or "")
    asset_id = str(payload.get("asset_id", "0"))
    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    rows = artifacts_of(job)
    chosen = next((r for r in rows if str(r.get("asset_id")) == asset_id), None)
    if chosen is None:
        return _error(
            404, f"job '{job_id}' has no artifact '{asset_id}'; it has {len(rows)}"
        )
    result = (
        retention.keep(chosen["path"], job_id=job_id, asset_id=asset_id)
        if bool(payload.get("keep", True))
        else retention.unkeep(chosen["path"])
    )
    return {**result, "artifacts": retention.kept(), "usage": retention.usage()}


@router.get(
    "/api/artifacts/{job_id}/outputs/{asset_id}",
    dependencies=[Depends(require_admin)],
)
def download_artifact(
    job_id: str, asset_id: str, download: bool = Query(default=False)
) -> Any:
    """Serve one artifact's bytes to the console, so preview and Save both work.

    The customer route (`/v1/jobs/…/outputs/…`) wants the Story Studio API key,
    which a browser page does not hold — this is the same bytes behind the door the
    console already authenticates against. `download=1` adds an attachment
    disposition for Save; without it the bytes stay inline, because an attachment
    disposition makes `<img>` and `<audio>` fetch nothing.
    """
    from pathlib import Path

    from .runtime import store
    from .store import artifacts_of

    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    rows = artifacts_of(job)
    chosen = next((r for r in rows if str(r.get("asset_id")) == asset_id), None)
    if chosen is None:
        return _error(
            404, f"job '{job_id}' has no artifact '{asset_id}'; it has {len(rows)}"
        )
    path = Path(str(chosen["path"]))
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return _error(410, "the artifact file is gone — it was pruned or removed", "ARTIFACT_GONE")
    headers: dict[str, str] = {}
    if download:
        stem = f"{job.get('modality') or 'artifact'}-{str(job_id)[:8]}-{asset_id}"
        headers["Content-Disposition"] = f'attachment; filename="{stem}{path.suffix}"'
    return Response(
        content=content,
        media_type=str(chosen.get("media_type") or "application/octet-stream"),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Create — the console's own door to generation.
#
# The customer routes under `/v1` need the Story Studio API key, which a browser
# page does not hold, so the console cannot start work through them. These wrap
# **the same `_submit`** the customer routes call rather than a second copy of the
# rules, so resolution, the enablement gate, the idempotency-key cross-modality
# refusal and the modality dispatch all stay in one place: a model the catalogue
# offers is exactly a model this can run, and one it withholds cannot be started
# here either.
# ---------------------------------------------------------------------------

#: The modalities the Create surface can start. Transcription is absent on purpose:
#: it carries a file, so it has its own route rather than a JSON body.
CREATE_MODALITIES = ("chat", "image", "video", "music", "tts")


@router.get("/api/create/models", dependencies=[Depends(require_admin)])
def create_models() -> dict[str, Any]:
    """What Create may pick, per modality — the offered, switched-on catalogue.

    Reuses the published catalogue and its configured/enabled eligibility rule.
    Keep the merged fleet identity and capabilities instead of listing each site.
    """
    wanted = set(CREATE_MODALITIES) | {"transcription"}
    by_modality: dict[str, list[dict[str, Any]]] = {m: [] for m in wanted}
    for modality in wanted:
        metadata = {}
        for definition, model in registry.models.enabled_models(modality):
            provider_id = fleet.publish_id(definition.id)
            compound = f"{provider_id}::{model['model_id']}"
            metadata[compound] = {
                "model_id": model["model_id"], "provider_id": provider_id,
                "provider_name": fleet.FLEET_DISPLAY_NAME if fleet.is_location_provider(definition.id) else definition.display_name,
                "free_lane": _is_free_lane(definition, model["model_id"]), "data_use": definition.data_use,
            }
        for row in catalog.rows(modality):
            if row["id"] in metadata:
                by_modality[modality].append({**row, **metadata[row["id"]]})
    for rows in by_modality.values():
        rows.sort(key=lambda m: (m["provider_name"].lower(), m["model_id"]))
    return {
        "by_modality": by_modality,
        "counts": {k: len(v) for k, v in by_modality.items()},
    }


@router.post("/api/create", dependencies=[Depends(require_admin)])
def create_submit(payload: dict[str, Any]) -> Any:
    """Queue one generation. Returns the job envelope, which is also how it polls."""
    from .api import GatewayError, _job_envelope, _submit

    modality = str(payload.get("modality") or "")
    if modality not in CREATE_MODALITIES:
        return _error(
            422, f"modality must be one of {', '.join(CREATE_MODALITIES)}"
        )
    model_id = str(payload.get("model_id") or "").strip()
    if not model_id:
        return _error(422, "model_id is required")
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    # The model id belongs in the params the adapter reads; the console sends the
    # rest of the fields and never has to know which of them a lane wants.
    params = {**params, "model_id": model_id}
    # A key per attempt: the console sends one so a retry after a dropped response
    # reattaches to the job it already made rather than making a second one.
    key = str(payload.get("idempotency_key") or "").strip() or str(uuid4())
    try:
        job, created = _submit(modality, model_id, params, key)
    except GatewayError as exc:
        return _error(exc.status, exc.detail, exc.code)
    return {"created": created, **_job_envelope(job)}


@router.post("/api/create/transcription", dependencies=[Depends(require_admin)])
async def create_transcription(
    model_id: str = Form(...),
    file: UploadFile = File(...),
    language: str = Form(default=""),
    idempotency_key: str = Form(default=""),
) -> Any:
    """Queue a transcription from an uploaded file.

    The `/v1` lane stages the upload between a quote and a confirm; the console has
    the bytes already, so it stages them once and queues. Both end in the same
    `_submit("transcription", …)` with a `_upload_path`, which is why this is a
    route and not a second implementation.
    """
    from .api import GatewayError, _job_envelope, _submit, _MAX_UPLOAD_BYTES
    from .config import ARTIFACT_DIR
    from pathlib import Path

    content = await file.read()
    if not content:
        return _error(422, "the uploaded file is empty")
    # **413, never 402**, and with the number in it: every cloud transcription API
    # on this gateway caps one upload at 25 MB, so a bigger file is refused here
    # rather than staged, queued, and failed after the bytes have travelled.
    if len(content) > _MAX_UPLOAD_BYTES:
        return _error(
            413,
            f"That file is {len(content) // (1024 * 1024)} MB; this gateway accepts "
            f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB per request.",
        )
    name = file.filename or "upload.bin"
    upload_dir = Path(str(ARTIFACT_DIR)) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f"console-{uuid4().hex}{Path(name).suffix or '.bin'}"
    staged.write_bytes(content)

    key = idempotency_key.strip() or str(uuid4())
    params: dict[str, Any] = {
        "_upload_path": str(staged),
        "_upload_name": name,
        "_upload_type": file.content_type or "application/octet-stream",
    }
    if language.strip():
        params["language"] = language.strip()
    try:
        job, created = _submit("transcription", model_id, params, key)
    except GatewayError as exc:
        return _error(exc.status, exc.detail, exc.code)
    return {"created": created, **_job_envelope(job)}


@router.get("/api/create/status/{job_id}", dependencies=[Depends(require_admin)])
def create_status(job_id: str) -> Any:
    """Poll any modality the console started, through one route."""
    from .api import _job_envelope

    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    return {**_job_envelope(job), "outputs": _admin_outputs(job)}


@router.post("/api/create/status/{job_id}/cancel", dependencies=[Depends(require_admin)])
def create_cancel(job_id: str) -> Any:
    from .api import _job_envelope

    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    return _job_envelope(store.cancel(job_id))


@router.post("/api/jobs/{job_id}/discard-uncertain", dependencies=[Depends(require_admin)])
def discard_uncertain_job(job_id: str) -> Any:
    """Abandon an uncertain upstream attempt and release the machine it reserved.

    The controller was allowed to do this from Hub `639fcf9`; before that an
    uncertain item pinned a node's machine until the Hub's own thirty-minute
    sweep. This is the **only** caller of the discard path — it is an explicit
    operator decision, and nothing schedules it.
    """
    from .api import GatewayError, _discard_recovery_hold

    try:
        return _discard_recovery_hold(job_id)
    except GatewayError as exc:
        return _error(exc.status, exc.detail, exc.code)


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
        # A 200 whose body is not JSON used to raise `json.JSONDecodeError` straight
        # out of the route — a 500 with no sentence, *after* the voice had already
        # been added upstream, which is the worst moment to lose the answer. The id
        # asked for is what the route names and what the provider echoes, so an
        # unreadable body falls back to it rather than dropping the import.
        try:
            payload_body = response.json() if response.content else {}
        except ValueError:
            payload_body = {}
        added_id = provider_voice_id
        if isinstance(payload_body, dict) and payload_body.get("voice_id"):
            added_id = str(payload_body["voice_id"])
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


def _admin_outputs(job: dict[str, Any], pinned: set[str] | None = None) -> list[dict[str, Any]]:
    """Retained outputs remain inspectable even after an interrupted settlement."""
    from . import retention
    from .store import artifacts_of

    if pinned is None:
        pinned = {str(row["path"]) for row in retention.kept()}
    return [{
        "asset_id": str(row.get("asset_id", index)),
        "url": f"/admin/api/artifacts/{job['id']}/outputs/{row.get('asset_id', index)}",
        "media_type": row.get("media_type"), "bytes": row.get("bytes"),
        "present": Path(row["path"]).is_file(), "kept": str(row["path"]) in pinned,
    } for index, row in enumerate(artifacts_of(job))]


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
    from . import retention
    from .api import _lifecycle_fields, recovery_action

    pinned = {str(row["path"]) for row in retention.kept()}
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
                **_lifecycle_fields(job),
                # **What an operator can do about this hold, decided here.** The
                # Jobs window used to infer it from `recovery_context`, so a hold
                # recorded by the restart sweep — or one on a cloud lane, which has
                # no discard route at all — showed no action and could never be
                # cleared. Only the gateway knows which lanes can discard, so it
                # publishes the action rather than letting the client guess:
                # `discard_upstream`, `close_hold`, or null when there is no honest
                # action. The context and the last discard answer ride along.
                "recovery_action": recovery_action(job),
                "recovery_context": job.get("recovery_context"),
                "discard_remote": job.get("discard_remote"),
                "outputs": _admin_outputs(job, pinned),
            }
        )

    return {"jobs": rows, "stats": store.stats()}


@router.get("/api/content-library", dependencies=[Depends(require_admin)])
def content_library() -> dict[str, Any]:
    from . import content_packs
    return {"archives": content_packs.inventory(), "store_connected": False,
            "notice": "Archive integrity is not ownership or installation approval. Story Studio must verify receipts/signatures and compatibility."}


@router.get("/api/content-shelf", dependencies=[Depends(require_admin)])
def content_shelf() -> dict[str, Any]:
    from . import content_delivery
    return content_delivery.status()


@router.post("/api/content-shelf/setup", dependencies=[Depends(require_admin)])
async def content_shelf_setup(request: Request) -> Any:
    from . import content_delivery
    body = await _json_body(request, {"acknowledge": bool})
    if isinstance(body, Response):
        return body
    if body["acknowledge"] is not True:
        return _error(422, "Explicit acknowledgement is required.", "CONTENT_AUTHORITY_ACKNOWLEDGEMENT_REQUIRED")
    try:
        return await run_in_threadpool(content_delivery.setup)
    except content_delivery.DeliveryError as exc:
        return _error(exc.status, str(exc), exc.code)


@router.post("/api/content-library/import", dependencies=[Depends(require_admin)])
async def import_content_archive(request: Request) -> Any:
    from . import content_packs
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > content_packs.MAX_ARCHIVE:
            return _error(413, "Content Packs are limited to 50 MiB.")
        data.extend(chunk)
    try:
        return content_packs.import_archive(bytes(data))
    except content_packs.PackError as exc:
        return _error(422, str(exc), "CONTENT_ARCHIVE_INVALID")


@router.post("/api/content-library/import-and-publish", dependencies=[Depends(require_admin)])
async def import_and_publish_archive(request: Request) -> Any:
    """One pack in, one product available — what a received `.sspack` needs.

    The two steps stay separate for the owner, who reviews an archive before adding
    it to the shelf; this is the same pair without the review, for an install that
    was sent a pack that was already authored and validated elsewhere.
    """
    from . import content_delivery, content_packs

    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > content_packs.MAX_ARCHIVE:
            return _error(413, "Content Packs are limited to 50 MiB.")
        data.extend(chunk)
    if not data:
        return _error(422, "A pack archive is required.", "CONTENT_ARCHIVE_INVALID")
    try:
        return await run_in_threadpool(content_delivery.import_and_publish, bytes(data))
    except content_packs.PackError as exc:
        return _error(422, str(exc), "CONTENT_ARCHIVE_INVALID")
    except content_delivery.DeliveryError as exc:
        return _error(exc.status, str(exc), exc.code)


@router.get("/api/content-library/{digest}/archive", dependencies=[Depends(require_admin)])
def export_content_archive(digest: str) -> Any:
    from . import content_packs
    try:
        data = content_packs.read_archive(digest)
    except content_packs.PackError as exc:
        return _error(410, str(exc), "CONTENT_ARCHIVE_UNAVAILABLE")
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{digest}.sspack"',
        "X-Content-Type-Options": "nosniff",
    })


# --------------------------------------------------------------------------
# Content shelf request bodies


async def _json_body(request: Request, fields: dict[str, type]) -> Any:
    from .content_packs import _json, PackError
    # Loopback admin trust must not let a third-party site's simple POST mutate the shelf.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _error(415, "This request requires Content-Type: application/json.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 4 * 1024**2:
            return _error(413, "Request bodies are limited to 4 MiB.")
        data.extend(chunk)
    try:
        body = _json(bytes(data))
    except PackError:
        return _error(422, "Send one unambiguous JSON object.")
    if (not isinstance(body, dict) or set(body) != set(fields)
            or any(type(body[key]) is not kind for key, kind in fields.items())):
        return _error(422, "Required fields: " + ", ".join(fields) + ". Extra fields are not accepted.")
    return body


# --------------------------------------------------------------------------
# Fleet — the location controllers this gateway aggregates for Story Studio


def _enrich_location(row: dict[str, Any]) -> dict[str, Any]:
    """A location row plus the gateway-side model counts for its provider.

    Keyed on the row's existing `provider_id` — the provider whose models this
    location offers, which is not the same string as the location id and must
    never be derived by prefixing in the client.
    """
    enriched = dict(row)
    provider_id = str(row.get("provider_id") or "")
    definition = registry.definition(provider_id) if provider_id else None
    if definition is None:
        # A location that answers no provider definition has no gateway-side
        # counts; `None` says "unknown", which is not zero.
        enriched.update({
            "models_discovered": None,
            "models_enabled": None,
            "models_publishable": None,
        })
        return enriched
    models = registry.models.models_for(provider_id, include_disabled=True)
    enriched.update({
        "models_discovered": len(models),
        "models_enabled": sum(1 for model in models if model.get("enabled")),
        "models_publishable": sum(
            1 for model in models if registry.models.can_publish(definition, model)
        ),
    })
    return enriched


@router.get("/api/fleet", dependencies=[Depends(require_admin)])
def fleet_snapshot() -> dict[str, Any]:
    """Every location, its machines and its in-flight work.

    Read-only and safe to poll: it fans out to each controller with a per-request
    timeout and reports an unreachable one as unreachable rather than failing the
    whole read. One dead location must not hide the others.
    """
    data = fleet.snapshot()
    # `fleet.snapshot()` returns the **cached** dict the catalogue and the
    # background refresher also read. Enriching it in place would persist these
    # gateway-side counts into the cache, so the top-level dict and every location
    # row touched are copied instead.
    out = dict(data)
    out["locations"] = [_enrich_location(row) for row in data.get("locations") or []]
    # **What the fleet should hold, and what it does.** The publisher identity and
    # the last push travel with the fleet read so the page has one source for both
    # halves of the report — the version that was signed and the version each
    # controller read back. `identity()` creates the key on first use; a failure is
    # reported rather than hidden, because the page then has to say why there is no
    # fingerprint to install.
    push = release_publisher.last_push()
    try:
        out["release"] = {"identity": release_publisher.identity(), "last_push": push}
    except release_publisher.ReleaseError as exc:
        out["release"] = {"identity": None, "last_push": push, "error": exc.message}
    return out


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


def _fleet_job_action(
    controller_id: str, modality: str, batch_id: str, item_index: int = 0
) -> Any:
    """Resolve a location into the adapter and request that acts on one batch.

    Shared by cancel and retry so the two can never disagree about which
    controller, which credential and which route a batch belongs to — the mistake
    that matters, because a job action aimed at the wrong Hub is either a no-op or
    someone else's work. Returns `(definition, location, request)` on success, or a
    JSON error response to return as-is.
    """
    if modality not in MODALITIES:
        return _error(422, f"modality must be one of {', '.join(MODALITIES)}")
    # `allow_stale`: this is a click, and a live probe of every controller to find
    # one url would make the action wait on the whole fleet. The page has just read
    # the snapshot, so it is warm.
    location = next(
        (
            row
            for row in (fleet.snapshot(allow_stale=True).get("locations") or [])
            if str(row.get("id")) == controller_id
        ),
        None,
    )
    if location is None:
        return _error(404, "No such location.")
    provider_id = str(location.get("provider_id") or "")
    definition = registry.definition(provider_id)
    if definition is None:
        return _error(404, f"no provider '{provider_id}'")
    request = BackendRequest(
        provider_id=provider_id,
        model_id="",
        modality=modality,
        params={},
        values=registry.credentials.resolved(provider_id),
        # The address the Fleet page already carries, so the credential and the
        # route are aimed at the same controller. Never this Mac's Hub by default.
        base_url=str(location.get("url") or ""),
        display_name=definition.display_name,
        upstream_job_id=batch_id,
        upstream_item_index=item_index,
    )
    return definition, location, request


@router.post(
    "/api/fleet/controllers/{controller_id}/jobs/{batch_id}/cancel",
    dependencies=[Depends(require_admin)],
)
def fleet_cancel_job(controller_id: str, batch_id: str, payload: dict[str, Any]) -> Any:
    """Ask a location's Hub to stop a batch this gateway can see running.

    **The Hub owns the work, so this is a request, not a result.** Its answer says
    what it actually did — `queued_cancelled`, `running_signalled`,
    `running_pending` — and those are passed through rather than flattened into a
    boolean, because a worker that has already accepted an item cannot be promised
    to stop. Cancelling only locally is how a batch keeps running at the location
    while the caller is told it was stopped, which is why this goes through the
    same adapter the worker uses instead of marking a row here.

    `modality` picks the route: transcription batches live at
    `/api/hub/transcription/jobs` and are invisible to the shared jobs route, so a
    cancel aimed at the wrong one answers 404 at the Hub.
    """
    resolved = _fleet_job_action(controller_id, str(payload.get("modality") or "").strip(), batch_id)
    if not isinstance(resolved, tuple):
        return resolved
    definition, location, request = resolved
    # `getattr`, not a protocol check: "this lane cannot cancel upstream" is an
    # answer to report, and most adapters issue a one-shot request with no remote
    # state to stop.
    cancel = getattr(adapter_for(definition), "cancel", None)
    if cancel is None:
        return _error(400, f"{definition.display_name} cannot cancel work upstream.")
    try:
        result = cancel(request)
    except BackendError as exc:
        return _error(exc.status, exc.detail, exc.code)
    return {"batch_id": batch_id, "location": location.get("label"), "result": result}


@router.post(
    "/api/fleet/controllers/{controller_id}/jobs/{batch_id}/items/{item_index}/retry",
    dependencies=[Depends(require_admin)],
)
def fleet_retry_job_item(
    controller_id: str, batch_id: str, item_index: int, payload: dict[str, Any]
) -> Any:
    """Ask a location's Hub to re-queue one failed item of a batch.

    **The Hub refuses most of what a page could ask for, and that is the useful
    part.** Only an item in state `error` is retried: a queued/running/done or
    cancelled item, a cancelled batch, and an `uncertain` item all answer 400 with
    their own message — an uncertain one must be discarded first, and discard
    exists only on the jobs path. The Hub's refusal is returned as its own
    sentence, never translated into a generic failure.
    """
    resolved = _fleet_job_action(
        controller_id, str(payload.get("modality") or "").strip(), batch_id, item_index
    )
    if not isinstance(resolved, tuple):
        return resolved
    definition, location, request = resolved
    retry = getattr(adapter_for(definition), "retry", None)
    if retry is None:
        return _error(400, f"{definition.display_name} cannot retry work upstream.")
    try:
        result = retry(request)
    except BackendError as exc:
        return _error(exc.status, exc.detail, exc.code)
    return {
        "batch_id": batch_id,
        "item_index": item_index,
        "location": location.get("label"),
        "result": result,
    }


# ---------------------------------------------------------------------------
# App releases — the Studio Hub app pushed from this gateway
#
# The gateway is the source of truth: it signs a release descriptor with its own
# Ed25519 publisher key and offers it to each controller, which verifies the
# signature against the key its owner installed and countersigns. The gateway then
# **reads back** what the controller holds; "the request was accepted" is not the
# answer to "what does it hold".
# ---------------------------------------------------------------------------

@router.get("/api/release/identity", dependencies=[Depends(require_admin)])
def release_identity() -> Any:
    """The publisher public key and fingerprint, plus the last push.

    This is the only place the owner can read the fingerprint to install on a
    controller, and the private half is never part of the answer.
    """
    try:
        return {
            "identity": release_publisher.identity(),
            "last_push": release_publisher.last_push(),
        }
    except release_publisher.ReleaseError as exc:
        return _error(exc.status, exc.message, exc.code)


@router.post("/api/release/publish", dependencies=[Depends(require_admin)])
def release_publish(payload: dict[str, Any]) -> Any:
    """Sign one artifact and offer it to every configured controller.

    The artifact is the DMG exactly as built — never repackaged, never re-signed
    after the fact — and the answer is per controller: the release it **read back**
    after storing, or the controller's own refusal sentence verbatim.

    A controller that already holds a **newer** version is refused
    (`RELEASE_DOWNGRADE`) unless `allow_downgrade` is true: the Hub's updater has
    no ordering, so only this side can stop a rollback.
    """
    artifact = str(payload.get("artifact_path") or "").strip()
    if not artifact:
        return _error(422, "expected {'artifact_path', 'version', 'build'}")
    filename = payload.get("filename")
    try:
        return release_publisher.publish(
            artifact_path=artifact,
            version=str(payload.get("version") or ""),
            build=str(payload.get("build") or ""),
            filename=str(filename) if filename else None,
            allow_downgrade=bool(payload.get("allow_downgrade")),
        )
    except release_publisher.ReleaseError as exc:
        return _error(exc.status, exc.message, exc.code)
