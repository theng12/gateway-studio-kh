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

import json
import re
import time
from pathlib import Path
from typing import Annotated, Any
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
from .catalog import MODALITIES, catalog, decode_model_id
from . import fleet
from . import price_evidence
from . import pricing
from .pricing import row_price
from . import qualification
from . import release_publisher, release_rollout
from . import model_share, startup_control
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
@router.post("/api/maintenance/drain", dependencies=[Depends(require_admin)])
def maintenance_drain(timeout_s: float = 15.0, pause_s: float = 120.0) -> dict[str, Any]:
    """Prepare a restart: take no new work, wait for in-flight sends, say when a restart is safe.

    `safe` means a restart now holds nothing: every running job has an id the next start resumes.
    Queued jobs stay queued. Dispatch resumes by itself after `pause_s`, so an install that aborts
    never leaves the queue paused, or at once on `DELETE`."""
    pause_s = min(max(pause_s, 1.0), 900.0)
    left = worker.drain(min(max(timeout_s, 0.0), 60.0), pause_s=pause_s)
    return {"safe": not left, "unsettled": left, "dispatch_paused_s": pause_s,
            "in_flight": worker.active()}


@router.delete("/api/maintenance/drain", dependencies=[Depends(require_admin)])
def maintenance_resume() -> dict[str, Any]:
    worker.resume_dispatch()
    return {"dispatch_paused": worker.dispatch_paused()}


@router.get("/api/status", dependencies=[Depends(require_admin)])
def status() -> dict[str, Any]:
    return {
        "hub": hub_status(),
        # Proof of life for the integration question: did Story Studio call this
        # gateway, when, and did it authenticate?
        "story_studio": story_activity(),
        "models": registry.models.summary(),
        "providers": registry.provider_counts(),
        "jobs_processed": worker.processed,
        "jobs": store.stats(),
        "uptime_seconds": int(time.time() - STARTED_AT),
        "catalog": {
            modality: len(catalog.rows(modality))
            for modality in ("chat", "image", "video", "tts", "transcription", "music")
        },
    }


# ---------------------------------------------------------------------------
# Providers that the owner adds himself
#
# **The file already existed; this is the half that was missing.** `providers.local.json` has been
# read at import since the custom-provider support was written, and the console already *reports* an
# entry it could not load. What it never had was a way to write one — so adding a provider meant
# finding a file in Application Support and getting its schema right by hand. The owner asked for
# the other half: *"i think we can allow an add custom option. that would make future providers
# really easy until we can update native support."*
#
# **A new provider needs a restart, and the answer says so rather than pretending otherwise.**
# Definitions are loaded at import — a deliberate trade-off recorded in `definitions.py`, because a
# provider definition selects an adapter and the catalogue must not change shape underneath an
# in-flight job. A form that appeared to add a working provider and left it invisible until the next
# restart would be a control that does nothing, which is the state this project keeps having to fix.
# The route returns `restart_required: true` and the page says it in words.
_CUSTOM_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def _custom_providers_file() -> Path:
    from .config import CUSTOM_PROVIDERS_PATH
    return CUSTOM_PROVIDERS_PATH


def _read_custom_providers() -> list[dict[str, Any]]:
    """The entries in the file, as written. Never the loaded definitions: an entry that was
    *skipped* is precisely what the operator needs to see, and it has no definition."""
    path = _custom_providers_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("providers") if isinstance(raw, dict) else raw
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _write_custom_providers(entries: list[dict[str, Any]]) -> None:
    # Through `write_secret`: the file names which vendors are configured and, for a provider whose
    # key is stored rather than read from the environment, can carry it. Same reasoning as
    # `models_cache.json`, which is owner-only for the same reason.
    from .config import write_secret
    write_secret(_custom_providers_file(), json.dumps({"providers": entries}, indent=2) + "\n")


@router.get("/api/custom-providers", dependencies=[Depends(require_admin)])
def list_custom_providers() -> dict[str, Any]:
    """Providers the owner added, and which of them could not be loaded."""
    from .providers.definitions import IMPLEMENTED_KINDS

    skipped = {row["id"]: row["reason"] for row in definitions.skipped_custom_providers()}
    return {
        "providers": [
            {
                "id": str(entry.get("id") or ""),
                "display_name": str(entry.get("display_name") or entry.get("id") or ""),
                "kind": str(entry.get("kind") or ""),
                "base_url": str(entry.get("base_url") or ""),
                "modalities": list(entry.get("modalities") or ["chat"]),
                # An entry in the file that is not a running provider is the failure this page
                # exists to make visible. `None` means it loaded.
                "skipped_reason": skipped.get(str(entry.get("id") or "")),
            }
            for entry in _read_custom_providers()
        ],
        # What a new entry may declare. From `IMPLEMENTED_KINDS` itself, never a second list: a
        # copy is how `_VALID_KINDS` fell behind and silently dropped `xai`.
        "kinds": sorted(IMPLEMENTED_KINDS),
        "path": str(_custom_providers_file()),
    }


@router.post("/api/custom-providers", dependencies=[Depends(require_admin)])
def add_custom_provider(body: dict[str, Any]) -> dict[str, Any]:
    """Add one provider to `providers.local.json`.

    Refused, with the reason, for every way an entry can be unloadable — so the operator learns now
    rather than after a restart that the provider they just added will not appear.
    """
    from .providers.definitions import IMPLEMENTED_KINDS
    from .fleet import FLEET_PROVIDER_ID, PROVIDER_PREFIX

    provider_id = str(body.get("id") or "").strip().lower()
    if not _CUSTOM_ID_PATTERN.match(provider_id):
        raise AdminError(400, "The id must be 2–64 characters of lower-case letters, digits, dot, dash or underscore, and start with a letter or digit.")
    if provider_id in definitions.PROVIDERS_BY_ID:
        raise AdminError(400, f"'{provider_id}' is already a built-in provider. Choose another id.")
    if provider_id == FLEET_PROVIDER_ID:
        raise AdminError(400, f"'{provider_id}' is reserved — it is the namespace every Studio Hub location's models are published under.")
    kind = str(body.get("kind") or "").strip()
    if kind not in IMPLEMENTED_KINDS:
        # Named, and naming what *is* possible: the loader skips an unknown kind in silence, which
        # reads as "my file did not load" rather than "nothing can run that kind".
        raise AdminError(400, f"'{kind}' is not a kind this gateway can run. Choose one of: {', '.join(sorted(IMPLEMENTED_KINDS))}.")
    if kind != "studio_hub" and provider_id.startswith(PROVIDER_PREFIX):
        raise AdminError(400, f"An id starting with '{PROVIDER_PREFIX}' is treated as a Studio Hub location. Use another id, or the 'studio_hub' kind.")
    base_url = str(body.get("base_url") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise AdminError(400, "The base URL must start with http:// or https://")
    modalities = [str(m).strip() for m in (body.get("modalities") or ["chat"]) if str(m).strip()]
    if not modalities:
        raise AdminError(400, "Choose at least one operation this provider serves.")
    key_env = str(body.get("key_env_var") or "").strip() or f"CUSTOM_{provider_id.upper().replace('-', '_')}_KEY"

    entries = _read_custom_providers()
    if any(str(e.get("id") or "").strip().lower() == provider_id for e in entries):
        raise AdminError(400, f"'{provider_id}' is already in {_custom_providers_file().name}.")
    entries.append({
        "id": provider_id,
        "display_name": str(body.get("display_name") or provider_id).strip(),
        "category": "custom",
        "kind": kind,
        "base_url": base_url,
        "modalities": modalities,
        "discovery": str(body.get("discovery") or "openai_models"),
        "description": str(body.get("description") or "Added by the operator.").strip(),
        "credential_fields": ([{"key": "api_key", "label": "API key", "env_var": key_env}]
                              if body.get("needs_key", True) else []),
    })
    _write_custom_providers(entries)
    return {
        "added": provider_id,
        # Stated, not implied. See the note above this route.
        "restart_required": True,
        "note": f"Added to {_custom_providers_file().name}. Restart the gateway to use it — providers are read when it starts.",
    }


@router.delete("/api/custom-providers/{provider_id}", dependencies=[Depends(require_admin)])
def remove_custom_provider(provider_id: str) -> dict[str, Any]:
    """Remove one, and say if it was not there — the file is the authority, not this process."""
    entries = _read_custom_providers()
    kept = [e for e in entries if str(e.get("id") or "").strip().lower() != provider_id.strip().lower()]
    if len(kept) == len(entries):
        raise AdminError(404, f"'{provider_id}' is not in {_custom_providers_file().name}.")
    _write_custom_providers(kept)
    return {"removed": provider_id, "restart_required": True,
            "note": "Removed. Restart the gateway for it to stop being offered."}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Model pools
# ---------------------------------------------------------------------------
# A pool is one name for several of this gateway's own models, tried in order.
# It is what a caller saves when it wants "whichever free lane still works"
# rather than one vendor.
#
# **Edits are live, and that is the point.** A custom provider is read when the
# process starts, so adding one answers `restart_required: true`; a pool is read
# from its file by `pools.definition()` on every catalogue build, so a pool the
# owner just wrote is pickable without a restart. Answering "restart" here out of
# symmetry would be false, and a control that appears to do nothing is the
# failure this codebase keeps paying for.
@router.get("/api/pools", dependencies=[Depends(require_admin)])
def list_pools() -> dict[str, Any]:
    """Every pool, with whether each member still resolves."""
    from . import pools

    return {
        "pools": pools.describe_all(),
        "provider_id": pools.POOL_PROVIDER_ID,
        "path": str(pools.POOLS_PATH),
        # What a caller sends back to use one.
        "namespace": f"{pools.POOL_PROVIDER_ID}::",
    }


@router.post("/api/pools", dependencies=[Depends(require_admin)])
def create_pool(body: dict[str, Any]) -> Any:
    from . import pools

    try:
        pool = pools.create(
            str(body.get("name") or ""),
            [str(m) for m in (body.get("members") or [])],
            display_name=str(body.get("display_name") or ""),
            description=str(body.get("description") or ""),
        )
    except ValueError as exc:
        raise AdminError(422, str(exc)) from exc
    return {
        "created": pool["name"],
        "pool": pools.describe(pool["name"]),
        "restart_required": False,
    }


@router.patch("/api/pools/{name}", dependencies=[Depends(require_admin)])
def update_pool(name: str, body: dict[str, Any]) -> Any:
    from . import pools

    try:
        pool = pools.update(
            name,
            members=[str(m) for m in body["members"]] if "members" in body else None,
            display_name=str(body["display_name"]) if "display_name" in body else None,
            description=str(body["description"]) if "description" in body else None,
        )
    except ValueError as exc:
        raise AdminError(422, str(exc)) from exc
    if pool is None:
        raise AdminError(404, f"no pool named '{name}'")
    return {
        "updated": pool["name"],
        "pool": pools.describe(pool["name"]),
        "restart_required": False,
    }


@router.delete("/api/pools/{name}", dependencies=[Depends(require_admin)])
def delete_pool(name: str) -> Any:
    from . import pools

    if not pools.remove(name):
        raise AdminError(404, f"no pool named '{name}'")
    return {"removed": name, "restart_required": False}


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
    every = registry.describe_all()
    for described in every:
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
            "cloud": sum(1 for p in every if p.get("scope") == "cloud"),
            "local": sum(1 for p in every if p.get("scope") == "local"),
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
    # Which Hub build is answering, so the manual path records the same baseline
    # the automatic one does. Without it a manual Refresh would be redone by the
    # next refresher pass — one wasted discovery per press, and a cache that
    # cannot say what it describes.
    result = registry.refresh(provider_id, hub_version=fleet.hub_version_for(provider_id) or None)
    return {
        "provider_id": provider_id,
        "discovered": len(result.models),
        "error": result.error,
    }


@router.post("/api/refresh-all", dependencies=[Depends(require_admin)])
def refresh_all() -> dict[str, Any]:
    return {"outcomes": registry.models.refresh_all()}


# ---------------------------------------------------------------------------
# Price research evidence (imported, never fetched: CONTRACT-price-evidence.md)
# ---------------------------------------------------------------------------
@router.get("/api/pricing/evidence", dependencies=[Depends(require_admin)])
def pricing_evidence() -> dict[str, Any]:
    """What is accepted and dropped, and the informational rate-limit tiers per provider."""
    return price_evidence.current()


@router.post("/api/pricing/evidence", dependencies=[Depends(require_admin)])
def import_pricing_evidence(payload: dict[str, Any]) -> Any:
    """Validate and store a research file. Enables, routes and changes nothing else: it only adds
    `provider_page` prices to rows that the provider itself does not price."""
    try:
        result = price_evidence.import_evidence(
            payload,
            lambda pid: {m["model_id"]: m["modality"] for m in registry.models.models_for(pid)},
        )
    except ValueError as exc:
        return _error(422, str(exc))
    catalog.rebuild(force=True)
    return result


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def _price(definition: Any, model: dict[str, Any]) -> dict[str, Any]:
    """The catalogue's own price fields (`price_source`, `price_usd`, `price_as_of`, and for a
    page price `price_url` + `price_variants`) for one admin model row, so the console shows exactly
    what `/v1/models` publishes. Same call, same arguments as `catalog._story_row`."""
    location = fleet.is_location_provider(definition.id)
    return row_price(
        definition.kind, str(model["modality"]), model.get("raw"),
        None if location else registry.models.cached(definition.id).get("fetched_at"),
        definition.id, str(model["model_id"]),
    )


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
        #
        # **And why, when it cannot.** A Hub gains lanes on its own schedule, so a
        # model in one this build has no lane for is discovered and withheld —
        # and a tick box that changes nothing, with no sentence beside it, is the
        # "control that does nothing" the console is supposed to have stopped
        # shipping. `withhold_reason` is that sentence, and it is the same function
        # `can_publish` is derived from, so the reason and the verdict cannot part.
        "models": [
            {
                **model,
                "publishable": registry.models.can_publish(definition, model),
                **_price(definition, model),
                # **The reason, from the same function that decides.** It used to
                # fall back to a lane-level sentence and then to a flat "this model
                # cannot be offered", which printed eleven times on this Mac for
                # eleven correctly-withheld models — nine with no files installed,
                # two that are a codec and a tokenizer rather than models. The owner
                # asked why, and the answer was already in the row.
                "withheld_reason": registry.models.withhold_reason(definition, model),
            }
            for model in rows
        ],
        "last_refresh": cached.get("fetched_at"),
        "last_error": cached.get("error"),
        # What this provider listed that discovery could not place at all, by
        # engine. Published beside the models because the *models* list cannot
        # answer it: an unrecognised engine produces no row here, so without this
        # "the Hub has nothing" and "I did not understand what it sent" are the
        # same answer.
        "skipped": list(cached.get("skipped") or []),
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
    """The Models page's Free badge; the rule lives in `pricing.is_free_lane` so the catalogue's
    `chargeable` cannot disagree with it."""
    return pricing.is_free_lane(definition.free_tier, model_id)


def _model_row(
    definition: Any, model: dict[str, Any], provider_enabled: bool, *, location: bool
) -> dict[str, Any]:
    """One row of the model browser, before location merging.

    Every field the page and the API already publish stays exactly as it was —
    this is a presentation change, so nothing is renamed, removed or re-typed.
    The fields a location row adds are additive:

      * `is_location` — whether this row stands for machines rather than a vendor;
      * `locations` — one sub-row per machine, each carrying the `provider_id`
        that addresses **that** Mac's toggle, its own name, enabled state and
        modality (a cloud row publishes `null`, never an invented one-entry list);
      * `locations_enabled` / `locations_total` — the "N of M machines" cell;
      * `provider_ids` — every provider id in the row, which is what the provider
        filter matches and what a bulk action must act on. Without it a merged
        row's bulk action would change one Mac and call the model done.
    """
    model_id = str(model["model_id"])
    return {
        "provider_id": definition.id,
        "provider_name": definition.display_name,
        "provider_enabled": provider_enabled,
        "provider_ids": [definition.id],
        "model_id": model["model_id"],
        "display_name": model["display_name"],
        "modality": model["modality"],
        "source": model["source"],
        "enabled": model["enabled"],
        # **The trade travels with the row.** A free model's privacy
        # stance is its provider's, so the picker shows it without a
        # second lookup, and both facts come from one place so they
        # cannot disagree.
        "free_lane": _is_free_lane(definition, model_id),
        "data_use": definition.data_use,
        "is_location": location,
        "locations": [] if location else None,
        "locations_enabled": (1 if model["enabled"] else 0) if location else None,
        "locations_total": 1 if location else None,
        **_price(definition, model),
    }


def _merge_location_row(
    merged: dict[str, Any], definition: Any, model: dict[str, Any], provider_enabled: bool
) -> None:
    """Fold one machine's copy of a model into the row it shares with the others."""
    merged["provider_ids"].append(definition.id)
    merged["locations"].append(
        {
            "provider_id": definition.id,
            "provider_name": definition.display_name,
            "provider_enabled": provider_enabled,
            "display_name": model["display_name"],
            "modality": model["modality"],
            "source": model["source"],
            "enabled": model["enabled"],
            "free_lane": _is_free_lane(definition, str(model["model_id"])),
            "data_use": definition.data_use,
        }
    )


def _finish_location_row(merged: dict[str, Any]) -> None:
    """Set the row's own state from its machines, once every machine is in it."""
    machines = merged["locations"]
    merged["locations_total"] = len(machines)
    merged["locations_enabled"] = sum(1 for machine in machines if machine["enabled"])
    # **"Enabled" on a merged row means "offered to Story Studio"**, which is the
    # question the catalogue asks: one machine serving it is enough. The N-of-M
    # cell beside it is where a partially-rolled-out model is visible, and the
    # sub-rows are where it is fixed.
    merged["enabled"] = merged["locations_enabled"] > 0
    merged["provider_enabled"] = any(machine["provider_enabled"] for machine in machines)
    # This Mac first: it is the source of the inherited decisions, so a row whose
    # display name, modality or privacy stance differ between machines reads from
    # the Hub the owner is looking at. Otherwise the first location that reported it.
    primary = next(
        (
            machine
            for machine in machines
            if machine["provider_id"] == fleet.provider_id_for(fleet.LOCAL_ID)
        ),
        machines[0],
    )
    merged["provider_id"] = primary["provider_id"]
    # `Studio Hub`, exactly as Story Studio is told: a location is not a product,
    # and the machines are named in the sub-rows.
    merged["provider_name"] = fleet.FLEET_DISPLAY_NAME
    merged["display_name"] = primary["display_name"]
    merged["modality"] = primary["modality"]
    merged["source"] = primary["source"]
    merged["free_lane"] = any(machine["free_lane"] for machine in machines)
    merged["data_use"] = primary["data_use"]


def _flat_models() -> list[dict[str, Any]]:
    """Every model the console can show, minus the ones that could never run.

    A model whose modality its provider's adapter does not serve is **not listed
    at all**. It could never be published, so offering it as something to tick is
    a control that does nothing — and the owner asked for exactly that to stop
    happening with OpenRouter's two Lyria rows, which its catalogue lists while
    publishing no music request path for them. The cache still holds them, so
    nothing discovered is lost; the console just stops advertising the impossible.

    **A location's models are published once, not once per Mac.** This used to be
    one row per (model, provider), so a model every location ran appeared six
    times with the same name, differing only by a provider name, and nothing said
    it was already running elsewhere — the owner ticked the same model on every
    Mac, one at a time. Every location provider now contributes to **one** merged
    row per (model_id, modality), which names the model once, counts the machines
    that offer it (`locations_enabled` of `locations_total`) and lists each Mac's
    own state beneath it. The namespace is keyed by model **and modality**: a
    group whose rows disagreed about the operation could not be filtered or
    faceted honestly, and the modality is what the operator shops by.

    A cloud provider keeps one row per model. Two vendors offering the same model
    *are* different products, and there is no machine in the question — its
    `locations` is `null`, never a fabricated single-entry list. The two shapes
    share every field the page already read, so filters, facets, sorting and the
    bulk route see one row list either way.
    """
    rows: list[dict[str, Any]] = []
    merged_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for definition in registry.definitions():
        provider_enabled = registry.credentials.is_enabled(definition.id)
        by_location = fleet.is_location_provider(definition.id)
        for model in registry.models.models_for(definition.id, include_disabled=True):
            if not registry.models.can_publish(definition, model):
                continue
            if not by_location:
                rows.append(_model_row(definition, model, provider_enabled, location=False))
                continue
            key = (str(model["model_id"]), str(model["modality"]))
            merged = merged_rows.get(key)
            if merged is None:
                merged = _model_row(definition, model, provider_enabled, location=True)
                merged["locations"] = []
                merged["provider_ids"] = []
                merged_rows[key] = merged
                rows.append(merged)
            _merge_location_row(merged, definition, model, provider_enabled)
    for merged in merged_rows.values():
        _finish_location_row(merged)
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


def _model_search_text(value: object) -> str:
    """Fold case and make common model-id separators read like spaces."""
    separated = "".join(
        " " if character in "-_/." else character
        for character in str(value).casefold()
    )
    return " ".join(separated.split())


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
        # A merged location row belongs to **every** machine in it, so selecting
        # one Mac has to find the model it shares rather than only the rows whose
        # primary provider happens to be that Mac. Cloud rows carry the single id.
        rows = [
            r
            for r in rows
            if provider_id in (r.get("provider_ids") or [r["provider_id"]])
        ]
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
        literal = q.strip().casefold()
        needle = _model_search_text(literal)
        if needle:
            rows = [
                r
                for r in rows
                if needle in _model_search_text(r["display_name"])
                or needle in _model_search_text(r["model_id"])
            ]
        elif literal:
            # A query made only of model-id separators normalises to empty. An
            # empty string matches every row, which would turn a bulk `-` search
            # into "all models", so a nonblank empty key matches nothing.
            rows = []
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
        "providers": _provider_options(everything),
        # Echoed so the page can show what it actually asked for rather than what it
        # meant to ask for — an ignored sort would otherwise look applied.
        "sort": sort if sort in _MODEL_SORTS else None,
        "order": "desc" if descending else "asc",
        "facets": _model_facets(everything, **filters),
    }


def _provider_options(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The (id, name) pairs the provider filter can select.

    A merged location row offers each **machine**, never the `studio-hub`
    namespace: the namespace is what Story Studio reads, it is not a provider,
    and enablement written under it goes nowhere (see `_apply`).
    """
    options: set[tuple[str, str]] = set()
    for row in rows:
        machines = row.get("locations")
        if machines:
            options.update(
                (str(machine["provider_id"]), str(machine["provider_name"]))
                for machine in machines
            )
        else:
            options.add((str(row["provider_id"]), str(row["provider_name"])))
    return sorted(options)


def _apply(
    rows: list[dict[str, Any]], enabled: bool
) -> int:
    by_provider: dict[str, list[str]] = {}
    for row in rows:
        # **Every machine in a merged row.** This read `row["provider_id"]`
        # alone, which for a merged location row is one Mac: a bulk "disable all
        # matching" would have switched the model off on the local Hub, left the
        # other five serving it, and reported the row done — the same "the number
        # in the sentence is not the set acted on" defect the bulk route already
        # paid for once with `only_enabled`.
        members = row.get("provider_ids") or [row["provider_id"]]
        for provider_id in dict.fromkeys(members):
            by_provider.setdefault(provider_id, []).append(row["model_id"])
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


@router.get("/api/settings/telegram", dependencies=[Depends(require_admin)])
def get_telegram() -> dict[str, Any]:
    """The Telegram notification settings. The bot token is never returned: `token_set` says only that one is saved."""
    from .notifier import notifier

    return notifier.public_settings()


@router.put("/api/settings/telegram", dependencies=[Depends(require_admin)])
def set_telegram(payload: dict[str, Any]) -> Any:
    """Change them. A key that is absent is left alone; `bot_token` replaces the token, `clear_token` removes it."""
    from .notifier import SettingsRefused, notifier

    try:
        return notifier.save_settings(payload)
    except SettingsRefused as exc:
        return _error(422, str(exc), "TELEGRAM_SETTINGS_REFUSED")


@router.post("/api/settings/telegram/test", dependencies=[Depends(require_admin)])
def test_telegram(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send one test message now. `bot_token` / `chat_id` in the body are tried without being saved."""
    from .notifier import notifier

    body = payload or {}
    return notifier.send_test(body.get("bot_token"), body.get("chat_id"))


@router.get("/api/settings/fleet-warnings", dependencies=[Depends(require_admin)])
def get_fleet_warnings() -> dict[str, Any]:
    """Which kinds of Fleet warning the owner hid (macOS automatic updates, Time Machine)."""
    return fleet.warnings_setting()


@router.put("/api/settings/fleet-warnings", dependencies=[Depends(require_admin)])
def set_fleet_warnings(payload: dict[str, Any]) -> Any:
    """Replace the hidden list. Disk-space warnings are never hideable: that is a 422."""
    try:
        fleet.set_hidden_warning_kinds(payload.get("hidden"))
    except ValueError as exc:
        return _error(422, str(exc), "WARNING_NOT_HIDEABLE")
    return fleet.warnings_setting()


@router.get("/api/settings/concurrency", dependencies=[Depends(require_admin)])
def get_concurrency() -> dict[str, Any]:
    """How many jobs may run at once — the pool, each lane, and each provider.

    Every row carries the `source` of its number, so the page can say *why* a lane
    is running at one rather than leaving the owner to guess. The provider list is
    seeded from the providers this gateway actually knows about, because a
    provider missing from the settings file is the row that most needs a control.

    Each row also carries its **`scope`** (`cloud` | `local`), taken from the same
    `describe_all()` the rest of this console reads, so the page can draw the two kinds as
    separate tabs. They are governed by different rules — a cloud limit is a number the owner
    chooses, a location's is a cap on what the site reports — and one list mixing them put
    eighteen vendors and six controllers under a single heading.
    """
    from . import concurrency

    from . import catalog

    described = registry.describe_all()
    known = [row["id"] for row in described if row.get("id")]
    scopes = {str(row["id"]): row.get("scope") for row in described if row.get("id")}
    # The lane vocabulary comes from the catalogue's own modality tuple, so a lane the gateway
    # serves cannot be missing from the page and a lane it does not serve cannot appear on it.
    return concurrency.settings_view(known, scopes, lane_names=catalog.MODALITIES)


@router.put("/api/settings/concurrency", dependencies=[Depends(require_admin)])
def set_concurrency(payload: dict[str, Any]) -> Any:
    """Change the ceilings. A key that is absent is left alone, never cleared."""
    from . import concurrency

    try:
        return concurrency.save_settings(payload)
    except concurrency.SettingsRefused as exc:
        return _error(422, exc.message, "SETTINGS_REFUSED")


@router.get("/api/backup", dependencies=[Depends(require_admin)])
def get_backup() -> dict[str, Any]:
    """What a backup holds, where it goes, and how the last one went."""
    from . import backup

    return backup.status()


@router.put("/api/settings/backup", dependencies=[Depends(require_admin)])
def set_backup(payload: dict[str, Any]) -> Any:
    from . import backup

    try:
        backup.save_settings(payload)
    except backup.BackupError as exc:
        return _error(422, str(exc), "BACKUP_SETTINGS_REFUSED")
    return backup.status()


@router.post("/api/backup/run", dependencies=[Depends(require_admin)])
def run_backup() -> Any:
    """Back Up Now. About 180 MB with the Content Packs, so it answers when it is done (seconds)."""
    from . import backup

    try:
        result = backup.run("manual")
    except backup.BackupError as exc:
        return _error(409, str(exc), "BACKUP_BUSY")
    return {"result": result, **backup.status()}


@router.post("/api/backup/inspect", dependencies=[Depends(require_admin)])
def inspect_backup(payload: dict[str, Any]) -> Any:
    """Check a backup file before restoring it: what it holds, when and where it was made."""
    from . import backup

    try:
        return backup.inspect(Path(str(payload.get("path") or "")))
    except backup.BackupError as exc:
        return _error(422, str(exc), "BACKUP_UNREADABLE")


@router.post("/api/backup/restore", dependencies=[Depends(require_admin)])
def restore_backup(payload: dict[str, Any]) -> Any:
    """Restore a checked backup; every file it replaces is moved to _trash first."""
    from . import backup

    try:
        return backup.restore(Path(str(payload.get("path") or "")))
    except backup.BackupError as exc:
        return _error(422, str(exc), "BACKUP_UNREADABLE")
    except OSError as exc:
        return _error(500, f"The restore stopped part way: {exc}", "BACKUP_RESTORE_FAILED")


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
    headers: dict[str, str] = {}
    if download:
        stem = f"{job.get('modality') or 'artifact'}-{str(job_id)[:8]}-{asset_id}"
        headers["Content-Disposition"] = f'attachment; filename="{stem}{path.suffix}"'
    from .api import stream_file

    try:
        return stream_file(
            path, str(chosen.get("media_type") or "application/octet-stream"), headers
        )
    except FileNotFoundError:
        return _error(410, "the artifact file is gone — it was pruned or removed", "ARTIFACT_GONE")


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
    return {"created": created, **_job_envelope(job, native=True)}


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
    from .api import GatewayError, _job_envelope, _submit, upload_limit_bytes
    from .config import ARTIFACT_DIR
    from pathlib import Path

    content = await file.read()
    if not content:
        return _error(422, "the uploaded file is empty")
    # **413, never 402**, and with the number in it: every cloud transcription API
    # on this gateway caps one upload at 25 MB, so a bigger file is refused here
    # rather than staged, queued, and failed after the bytes have travelled.
    limit = upload_limit_bytes(model_id)
    if len(content) > limit:
        return _error(
            413,
            f"That file is {len(content) // (1024 * 1024)} MB; this model accepts "
            f"{limit // (1024 * 1024)} MB per request.",
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
    return {"created": created, **_job_envelope(job, native=True)}


@router.get("/api/create/status/{job_id}", dependencies=[Depends(require_admin)])
def create_status(job_id: str) -> Any:
    """Poll any modality the console started, through one route."""
    from .api import _job_envelope

    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    return {**_job_envelope(job, native=True), "outputs": _admin_outputs(job)}


@router.post("/api/create/status/{job_id}/cancel", dependencies=[Depends(require_admin)])
def create_cancel(job_id: str) -> Any:
    from .api import _job_envelope
    from .worker import cancel_upstream

    job = store.get(job_id)
    if job is None:
        return _error(404, f"no job '{job_id}'")
    store.cancel(job_id)
    # **Reach the work, not just the row.** This is the route the menu-bar Jobs window and the
    # Create page both call, and it only stamped the cancel locally: a job already dispatched to a
    # Hub kept rendering there while the app said "cancelling". The public `/v1/.../cancel` routes
    # have always asked the Hub (`worker.cancel_upstream`); this one now does the same, with the
    # row as it was before the cancel, as they do (audit 2026-10-01).
    cancel_upstream(job, store)
    return _job_envelope(store.get(job_id), native=True)


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


@router.post("/api/jobs/{job_id}/reconcile-hold", dependencies=[Depends(require_admin)])
def reconcile_hold_job(job_id: str) -> Any:
    """Ask the Hub to settle a "Needs recovery" job from its original worker (keeps a finished result).

    Explicit operator action, as `discard-uncertain` is; nothing schedules it. The hold sweep then
    adopts or closes the job from the Hub's answer within a minute.
    """
    from .api import GatewayError, _reconcile_recovery_hold

    try:
        return _reconcile_recovery_hold(job_id)
    except GatewayError as exc:
        return _error(exc.status, exc.detail, exc.code)


@router.post("/api/jobs/clear-failed", dependencies=[Depends(require_admin)])
def clear_failed_jobs(before: float | None = Query(None, ge=0)) -> dict[str, Any]:
    """"Remove Failed From List": hide settled failed jobs (last changed at or before `before`).

    Non-destructive: only `dismissed_at` is written. Rows, totals, analytics, billing and the
    audit trail stay; needs-recovery, running and queued jobs are never touched.
    """
    return {"ok": True, "removed": store.dismiss_failed(before)}


@router.post("/api/jobs/resolve-recovery", dependencies=[Depends(require_admin)])
def resolve_all_recovery(before: float | None = Query(None, ge=0)) -> dict[str, Any]:
    """"Resolve All Needing Recovery": Recover Result where the Hub has it, else Discard/Close."""
    from .api import _resolve_all_recovery_holds

    return _resolve_all_recovery_holds(before)


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


@router.get("/api/qualification", dependencies=[Depends(require_admin)])
def qualification_envelope(
    provider_id: str | None = None,
    model_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = qualification.DEFAULT_WINDOW,
) -> dict[str, Any]:
    """What each model has **actually been measured doing**, beside what was stated.

    The catalogue answers "what may I ask"; this answers "what has this lane really
    carried", which is the only figure that qualifies a model for a bigger ask. The
    two are published **side by side and never merged**: `stated` is the producer's
    number with its own `*_source`, every figure on the measured row is ours and the
    row says `source: "gateway"`.

    The join is on the **published** namespace, not the job's provider id, because a
    job routed to `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`.
    Comparing raw ids made every local row read as unknown, which is exactly the lane
    the ceiling question is about.

    `stated: null` means the model is not in the catalogue (disabled, or withheld) —
    *unknown*, not unlimited. A model that has never run is **absent from `rows`**
    rather than present with zeros.

    **The bound lives in `Annotated`, and the default is a real int.** Written the
    usual way — `limit: int = Query(50, ge=1, le=500)` — the default *is* a `Query`
    object, so calling the function directly (a test, or any later caller inside the
    process) hands that object to SQLite and raises `type 'Query' is not supported`.
    The route was reachable only over HTTP. `/api/jobs` above still has the old
    shape; this one does not, and the constraint is identical over the wire.
    """
    stated: dict[tuple[str, str], dict[str, Any]] = {}
    for row in catalog.all_rows():
        key = decode_model_id(str(row.get("id", "")))
        if key[0]:
            stated[key] = {
                "max_output_tokens": row.get("max_output_tokens"),
                "max_output_tokens_source": row.get("max_output_tokens_source"),
                "context_window_tokens": row.get("context_window_tokens"),
                "context_window_tokens_source": row.get("context_window_tokens_source"),
            }

    rows = qualification.envelope(
        store, provider_id=provider_id, model_id=model_id, limit=limit,
    )
    for row in rows:
        row["stated"] = stated.get(
            (fleet.publish_id(row["provider_id"]), row["model_id"])
        )
    return {
        "rows": rows,
        "window": limit,
        "measured_rows": len(rows),
        "note": (
            "Every figure on a row is this gateway's own measurement of real work, "
            "labelled source: gateway. `stated` is what the producer declares, with "
            "its own source; stated: null means the model is not in the catalogue, "
            "which is unknown rather than unlimited."
        ),
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


@router.get("/api/analytics", dependencies=[Depends(require_admin)])
def generation_analytics(
    limit: int = Query(20000, ge=1, le=50000),
) -> dict[str, Any]:
    """What this gateway has generated, grouped by lane, model and machine.

    **This is the surface that answers "where should the next job go?"** — totals by
    generation type, mean and median time per model, and what each machine actually
    produced. Until the adapter started carrying `runtime_s` the generation record held a
    figure for chat and `None` for image, voice, music and video, so the lanes that do the
    real work could not be compared at all.

    `limit` is bounded for the reason `/api/jobs` documents: it is bound straight into SQL
    as `LIMIT ?`, and an unbounded `int` here is both an `OverflowError` waiting to be a
    500 and a way to ask for every job ever recorded. The grouping needs individual
    durations rather than a SQL summary, so the bound is real work, not a formality.

    `by_model_class` (D8) is the same record grouped by model × Mac class, the class read from
    the fleet snapshot's machine rows (chip + memory class). Machines no longer in the fleet
    read as "unknown Mac". For ETAs and planning; routing does not read it.
    """
    classes: dict[tuple[str, str], str] = {}
    try:
        for row in fleet.snapshot(allow_stale=True).get("locations") or []:
            for machine in row.get("machines") or []:
                classes[(str(row.get("provider_id") or ""), str(machine.get("machine") or ""))] = \
                    mac_class(machine.get("chip"), machine.get("memory_gb"))
    except Exception:  # noqa: BLE001 - no fleet read leaves every class unknown, not the page empty
        pass
    return store.generation_analytics(
        limit=limit,
        mac_class=lambda provider_id, machine: classes.get((str(provider_id or ""), str(machine or "")), "unknown Mac"),
    )


#: "M4 · 16 GB". Lives in `fleet` now (routing ranks Qwen by it); re-exported for its old callers.
mac_class = fleet.mac_class


@router.get("/api/analytics/daily", dependencies=[Depends(require_admin)])
def daily_analytics(days: int = Query(7, ge=1, le=31)) -> dict[str, Any]:
    """Per day and model: jobs, failures by code, and queue wait p50/p90 (fleet audit 2026-10-07).

    The 500-row `/api/jobs` window was all a reader had, so a 27,000-job day could not be measured
    from the Gateway; this answers the scheduling questions directly. Days are this Mac's local days.
    """
    since = time.time() - days * 86400.0
    return {"since": since, "days": days, "rows": store.daily_by_model(since)}


@router.get("/api/jobs", dependencies=[Depends(require_admin)])
def list_jobs(
    limit: int = Query(50, ge=1, le=500),
    state: str | None = None,
    modality: str | None = None,
    since: float | None = Query(None, ge=0),
    before: float | None = Query(None, ge=0),
    include_removed: bool = False,
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
    labels = _model_labels()

    TERMINAL = {"succeeded", "failed", "cancelled"}
    rows: list[dict[str, Any]] = []
    as_of = time.time()
    for job in store.list_jobs(limit=limit, state=state, modality=modality, since=since, before=before,
                               hide_dismissed=not include_removed):
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
                # **Who asked.** Stored on every job and, until this line, published
                # by nothing — the store had it and the surface the Jobs window
                # reads did not, which is the same defect as a control wired to
                # nothing. `None` means the request said nothing about itself.
                "caller": job.get("caller"),
                "job_group": job.get("job_group"),
                # Why a job is queued rather than running. `accepted` alone cannot
                # tell "not dispatched yet" from "the fleet was busy and we are
                # waiting", and only one of those is worth the owner's attention.
                "defer_reason": job.get("defer_reason"),
                "next_action_at": job.get("next_action_at"),
                # **How far back in the queue this job is, and when it will be tried again.**
                # `defer_reason` alone says *why* it is waiting and leaves the two questions the
                # owner actually has — "am I next?" and "when?" — to be guessed at. The position is
                # a real rank among the waiting jobs of the same modality, and `retry_in_s` is when
                # this gateway will make its next attempt, not a prediction about the provider.
                # **Every `accepted` job is waiting**, not only a deferred one: gating this on
                # `defer_reason` left a freshly queued job looking exactly like a running one.
                "queue_position": (
                    store.queue_position(job["id"]) if job["state"] == "accepted" else None
                ),
                "queue_size": (
                    (store.queue_rank(job["id"]) or {}).get("size")
                    if job["state"] == "accepted" else None
                ),
                "retry_in_s": (
                    max(0, int(round(float(job["next_action_at"]) - time.time())))
                    if job.get("defer_reason") and job.get("next_action_at") is not None
                    else None
                ),
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

    # **Paging past 500** (fleet audit): ask again with `before` = this, until it is null.
    # ponytail: jobs created in the very same microsecond as the page edge could be skipped.
    next_before = rows[-1]["created_at"] if len(rows) == limit else None
    # The two bulk actions' counts, and the instant they were read: the window passes `as_of` back
    # as `before`, so a press touches only what the confirmation counted.
    bulk = {
        "as_of": as_of,
        "removable_failed": store.dismissable_failed_count(as_of),
        "needs_recovery": store.needs_recovery_count(as_of),
    }
    return {"jobs": rows, "stats": store.stats(), "next_before": next_before, "bulk": bulk}


def _model_labels() -> dict[tuple[str, str], str]:
    """`(provider_id, model_id) -> display_name`, for every model on every provider.

    One reader for the jobs list and the queue view, so a model named on one surface
    cannot be named differently on the other.
    """
    labels: dict[tuple[str, str], str] = {}
    for definition in registry.definitions():
        for model in registry.models.models_for(definition.id, include_disabled=True):
            labels[(definition.id, model["model_id"])] = model["display_name"]
    return labels


#: The lane caps a queue view explains, in the catalogue's own order. `lane_budget`
#: answers for every one of them, so a lane with no cap says "the whole pool" rather
#: than being omitted and leaving the reader to wonder.
_QUEUE_LANES = MODALITIES


@router.get("/api/queue", dependencies=[Depends(require_admin)])
def queue_view(limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """What is waiting, where it stands, and **what is bounding each lane**.

    The owner asked for this in as many words — *"when the machines get busy, i want
    it queue… i need the UI for that"* — and the queue had no surface at all: the
    console's four views are models, providers, create and packs, and `worker.budgets`
    was written *"so the console can say what is bounding a lane"* and read by
    nothing outside tests. The two halves belong together, because a position with no
    ceiling beside it is a number without a reason: "position 3" cannot be told from
    "position 3 and this lane may run one at a time".

    Every job here is in state `accepted` — waiting, deferred or not — in the order
    the queue will dispatch them (`store.waiting_jobs`). Each carries its own
    `queue_position`/`queue_size` from the store's single rule, its `defer_reason`,
    its age, and its **retry policy evidence** (`attempts_used`/`attempts_allowed`/
    `attempts[]`, the same fields the job envelope publishes) so the owner's decision
    E — *"the same policy stated on the Gateway's Jobs view so an operator can see
    attempt n of 3"* — is readable where the work is waiting rather than only after
    it has failed.

    `in_flight` and `budgets` are the live worker's own books, read from the
    singleton in `runtime` and never reconstructed: `budgets` holds the last budget
    resolved per provider, with the source and the sentence explaining it, and
    `in_flight` is how many jobs that provider is running right now. `lanes` states
    each modality's cap, so the two halves meet: a provider row bounded at 1 and a
    lane row bounded at the pool describe different ceilings and both are shown.
    """
    from . import concurrency
    from .api import _publish_attempt_evidence

    now = time.time()
    labels = _model_labels()
    waiting = store.waiting_jobs(limit=limit)
    eligible: dict[tuple[str, str], int | None] = {}

    def eligible_macs(provider_id: str, model_id: str, modality: str) -> int | None:
        """Macs in the fleet that could take this job now (reachable, not resting after a memory refusal)."""
        if not fleet.is_location_provider(provider_id):
            return None
        if (model_id, modality) not in eligible:
            try:
                eligible[(model_id, modality)] = sum(
                    0 if c.get("spent") else c["slots"] for c in fleet.routing_candidates(model_id, modality))
            except Exception:  # noqa: BLE001 - no fleet read is "not stated", not an empty queue view
                eligible[(model_id, modality)] = None
        return eligible[(model_id, modality)]

    jobs: list[dict[str, Any]] = []
    for job in waiting:
        provider_id = str(job.get("provider_id") or "")
        model_id = str(job.get("model_id") or "")
        definition = registry.definition(provider_id)
        rank = store.queue_rank(job["id"]) or {}
        created = job.get("created_at")
        row: dict[str, Any] = {
            "id": job["id"],
            "short_id": job["id"][:8],
            "state": job["state"],
            "modality": job["modality"],
            "provider_id": provider_id,
            "provider_name": definition.display_name if definition else provider_id,
            "model_id": model_id,
            "model_name": labels.get((provider_id, model_id), model_id),
            "caller": job.get("caller"),
            "job_group": job.get("job_group"),
            "created_at": created,
            "age_seconds": round(now - created, 1) if isinstance(created, (int, float)) else None,
            "queue_position": rank.get("position"),
            "queue_size": rank.get("size"),
            "defer_reason": job.get("defer_reason"),
            "next_action_at": job.get("next_action_at"),
            # When a *deferred* job will be tried again. A fresh job also carries a
            # `next_action_at` (the 0.8 s dispatch delay), and rendering that as
            # "retry in 1s" would describe a first attempt as a retry.
            "retry_in_s": (
                max(0, int(round(float(job["next_action_at"]) - now)))
                if job.get("defer_reason") and isinstance(job.get("next_action_at"), (int, float))
                else None
            ),
            # How many times this job has been dispatched and put back, against the
            # ceiling that eventually stops waiting on a busy fleet. Distinct from
            # `attempts_*`, which count *machines*: a deferral never reached one, so
            # the retry policy does not count it.
            "deferrals": int(job.get("attempt_count") or 0),
            "eligible_macs": eligible_macs(provider_id, model_id, str(job.get("modality") or "")),
        }
        # The same evidence the job envelope publishes, from the same function, so
        # the console and Story read one policy rather than two spellings of it.
        _publish_attempt_evidence(row, job)
        # **The policy is stated for every waiting job, not only one that has already
        # failed.** `_publish_attempt_evidence` is deliberately additive — a job on
        # its first machine carries no `attempts` key, so "never retried" stays
        # distinguishable from "we did not look" — but the operator's question here,
        # in the owner's own words, is *"attempt n of 3"* asked **while the job
        # waits**. So the two counts and the wording are filled from the same policy
        # reader when the evidence had nothing to add.
        row.setdefault("attempts", [])
        row.setdefault("attempts_used", 0)
        row.setdefault("attempts_allowed", int(concurrency.retry_policy()["max_attempts"]))
        row.setdefault("attempt_policy", concurrency.retry_policy_wording())
        jobs.append(row)

    # The live books. Copied under no lock: each is replaced key by key on the
    # worker's own thread, and a reader that sees a count move between two keys is
    # reading two instants, not a corrupted book.
    in_flight = dict(getattr(worker, "_in_flight", {}) or {})
    lane_in_flight = dict(getattr(worker, "_lane_in_flight", {}) or {})
    budgets = dict(getattr(worker, "budgets", {}) or {})

    def provider_name(provider_id: str) -> str:
        definition = registry.definition(provider_id)
        return definition.display_name if definition else provider_id

    sites = [
        {
            "provider_id": provider_id,
            "provider_name": provider_name(provider_id),
            "in_flight": int(in_flight.get(provider_id) or 0),
            "budget": budgets.get(provider_id),
        }
        for provider_id in sorted(set(in_flight) | set(budgets))
    ]

    # **How long a model in this lane may hold a machine** (Q6, the owner's answer). He asked
    # that the video lane's deadline be visible in the queue view, and the reason is the one a
    # waiting operator actually has: *why is my chat job behind that?* A two-hour video lane is
    # the answer, and until now the page could not say it. Read from each model's own
    # `lane_contract.deadline_seconds` — the lane that enforces the number states it — and `null`
    # where a lane states none, because a guess here would be quoted as the lane's own ceiling.
    deadlines: dict[str, float] = {}
    for row in catalog.all_rows():
        contract = row.get("lane_contract") or {}
        value = contract.get("deadline_seconds")
        model_id = str(row.get("id") or "")
        if isinstance(value, (int, float)) and value > 0 and model_id:
            deadlines[model_id] = float(value)

    def _deadline_for(job: dict) -> float | None:
        for key in ("model_id", "requested_model_id"):
            value = deadlines.get(str(job.get(key) or ""))
            if value is not None:
                return value
        return None

    for job in jobs:
        value = _deadline_for(job)
        if value is not None:
            job["lane_deadline_seconds"] = value

    waiting_by_lane: dict[str, int] = {}
    lane_deadlines: dict[str, float] = {}
    for job in jobs:
        lane = str(job.get("modality") or "")
        waiting_by_lane[lane] = waiting_by_lane.get(lane, 0) + 1
        value = _deadline_for(job)
        if value is not None:
            lane_deadlines[lane] = max(lane_deadlines.get(lane, 0.0), value)
    lanes = []
    for lane in _QUEUE_LANES:
        budget = concurrency.lane_budget(lane)
        running = int(lane_in_flight.get(lane) or 0)
        # A lane is shown when something is happening in it or when somebody capped
        # it; an uncapped, empty lane would be six rows of "the whole pool".
        if not waiting_by_lane.get(lane) and not running and budget.source != concurrency.SOURCE_CURATED:
            continue
        lanes.append({
            "modality": lane,
            "waiting": waiting_by_lane.get(lane, 0),
            "in_flight": running,
            # The **longest** deadline among this lane's jobs, because that is what a wait can
            # cost: a lane holding one two-hour video can hold it for two hours.
            "deadline_seconds": lane_deadlines.get(lane),
            **budget.as_dict(),
        })

    return {
        "generated_at": now,
        "waiting": len(jobs),
        # Who is waiting and running, by caller (`story-studio@<Mac>`) and by Story's job group,
        # counted over the whole table rather than the `limit` rows above.
        "by_caller": store.active_counts_by_caller(),
        "by_group": store.active_counts_by_caller("job_group"),
        "truncated": len(jobs) >= limit,
        "jobs": jobs,
        "lanes": lanes,
        "sites": sites,
        "pool": {
            **concurrency.global_budget().as_dict(),
            **concurrency.fleet_share(),
            "in_flight": int(worker.active()),
        },
        # The raw books the worker keeps, published so the view can say what is
        # bounding a lane without re-deriving it — and so a reader comparing the
        # console with a test is looking at the same numbers.
        "in_flight": in_flight,
        "budgets": budgets,
        "queue_wait": concurrency.queue_wait_budget(),
        "attempt_policy": concurrency.retry_policy_wording(),
    }


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
def fleet_snapshot(force: bool = False) -> dict[str, Any]:
    """Every location, its machines and its in-flight work.

    Read-only and safe to poll: it fans out to each controller with a per-request
    timeout and reports an unreachable one as unreachable rather than failing the
    whole read. One dead location must not hide the others.

    **`force` is what the page's Refresh button sends**, and it is the only thing
    that waits for a fan-out. Without it this is a cache read however old the
    cache is -- the poller owns freshness -- because a read that blocks on every
    controller is a read that times out on a busy fleet. Measured before the
    split: 22.7 s here against the app's 15 s timeout, so the Fleet page loaded
    only when nothing was running.
    """
    data = fleet.snapshot(force=force)
    # `fleet.snapshot()` returns the **cached** dict the catalogue and the
    # background refresher also read. Enriching it in place would persist these
    # gateway-side counts into the cache, so the top-level dict and every location
    # row touched are copied instead.
    out = dict(data)
    out["locations"] = [_enrich_location(row) for row in data.get("locations") or []]
    # Read live, never from the cache: hiding a warning kind must show on the next read.
    out["fleet_warnings"] = fleet.warnings_setting()
    # **What the fleet should hold, and what it does.** The publisher identity and
    # the last push travel with the fleet read so the page has one source for both
    # halves of the report — the version that was signed and the version each
    # controller read back. `identity()` creates the key on first use; a failure is
    # reported rather than hidden, because the page then has to say why there is no
    # App Update Key public key and comparison fingerprint to show.
    push = release_publisher.last_push()
    try:
        out["release"] = {"identity": release_publisher.identity(), "last_push": push}
    except release_publisher.ReleaseError as exc:
        out["release"] = {"identity": None, "last_push": push, "error": exc.message}
    # The paced rollout, live: which controller is updating, and each group's Macs on the new version.
    out["release"]["rollout"] = release_rollout.status(data)
    # LAN model sharing (2026-10-05): each row's network, and every "Install on every Mac" in progress, live.
    model_share.stamp_networks(out)
    out["model_installs"] = model_share.status()
    return out


@router.post("/api/fleet/counters/reset", dependencies=[Depends(require_admin)])
def reset_fleet_counters() -> dict[str, Any]:
    """Every Mac's done/failed/median count from now on (Fleet -> Machines, "Reset counters").

    A baseline, not a deletion: each Hub keeps its whole job history and is asked to count from
    this moment (`activity_since`). A Hub too old to honour it keeps counting everything, and the
    page says so per Mac instead of showing a reset that did not happen.
    """
    return {"counters_since": fleet.reset_counters()}


@router.get("/api/fleet/disk", dependencies=[Depends(require_admin)])
def fleet_disk() -> dict[str, Any]:
    """Every Mac's disk, least free space first, plus this gateway Mac's own.

    Compact, and a cache read like the fleet snapshot: the banner polls it.
    """
    return fleet.disk_summary(fleet.snapshot(allow_stale=True).get("machines") or [])


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
    # **A newly added location starts with what it has installed switched on**
    # (`fleet.seed_enabled_installed`, owner 2026-10-09): nothing is copied from
    # This Mac, the owner's ruled-out models stay off, and one that was already on
    # the roster keeps whatever the owner decided about it, because re-adding a
    # moved controller or re-pasting its token must not rewrite those choices.
    enable_seed: dict[str, Any] | None = None
    if row.get("created") and discovery_error is None:
        try:
            enable_seed = fleet.seed_enabled_installed(provider_id)
        except Exception as exc:  # noqa: BLE001 - and never silence about it
            enable_seed = {
                "provider_id": provider_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
    # Check the credential as part of adding, not later: both of a controller's
    # credentials reach every route this gateway calls, so an over-privileged one
    # behaves perfectly and is never noticed. Reported now, while the token is
    # still in the person's hand.
    return {
        "controller": row,
        # **The row the page inserts at once**, in the shape a fleet read carries, marked pending: the
        # first fan-out that includes it takes as long as the slowest controller, and until then the
        # page must show the location it was just told exists ("Just added — reading its Macs…").
        "location": fleet.pending_location(row),
        "credential": fleet.credential_scope_for(str(row.get("id") or "")),
        "discovered": discovered,
        "discovery_error": discovery_error,
        "enable_seed": enable_seed,
        "controllers": fleet.controllers(),
    }


@router.post("/api/fleet/controllers/{controller_id}/resume", dependencies=[Depends(require_admin)])
def fleet_resume(controller_id: str) -> Any:
    """Un-pause a location, deliberately, without touching its identity or its decisions.

    Q7's other half: the owner chose *"pause after 7 days, never auto-remove"*, and a pause with
    no resume is a removal with extra steps. This clears the clock and the pause — the row, its
    credential and every per-model enable decision were never touched, so the site comes back
    exactly as it was.
    """
    row = fleet.resume_location(controller_id)
    if row is None:
        return _error(404, "No such controller.")
    return {"controller": row, "controllers": fleet.controllers()}


@router.post("/api/fleet/controllers/{controller_id}/pause", dependencies=[Depends(require_admin)])
def fleet_pause(controller_id: str) -> Any:
    """**Pause a location by hand** (`local` is this Mac). It gets no new work: routing, late binding and
    retry-elsewhere skip it, while jobs already running there finish. Kept across restarts; the automatic
    outage resume never lifts it — only `/resume` does. Nothing is removed, so it comes back as it was."""
    row = fleet.set_owner_pause(controller_id, True)
    if row is None:
        return _error(404, "No such controller.")
    return {"controller": row, "controllers": fleet.controllers()}


@router.put("/api/fleet/controllers/{controller_id}/address", dependencies=[Depends(require_admin)])
def fleet_replace_address(controller_id: str, payload: dict[str, Any]) -> Any:
    """**Re-point a location at a new address, keeping its identity and its decisions.**

    A3 step 4, and the difference from remove-then-add is the whole reason it exists: when a
    controller is replaced by promoting one of its nodes, the new head has a different address and
    a different controller id, so adding it makes a *second* location and orphans the first one's
    credential and every per-model enable decision. A rehearsal measured it — after a handover the
    replacement row published `model_id: null` and `locations_enabled: null`, because it had
    inherited nothing.

    The row id, and therefore the provider id Story's presets and the operator's ticks are keyed on,
    is preserved. The **site id is cleared**: it named the identity of the Mac that used to answer
    here, and carrying it to a different Mac is the shared-identity defect `_stable_identity`
    exists to prevent. The next probe fills in the new one.
    """
    try:
        row = fleet.replace_address(controller_id, str(payload.get("url") or ""),
                                    str(payload.get("token") or ""))
    except ValueError as exc:
        return _error(400, str(exc))
    if row is None:
        return _error(404, "No such controller.")
    # The models must be re-read: a different Mac may offer a different set, and the per-model
    # enable decisions this action exists to preserve only mean something against what it offers.
    discovered: int | None = None
    discovery_error: str | None = None
    try:
        discovered = len(registry.refresh(fleet.provider_id_for(controller_id)).models)
    except Exception as exc:  # noqa: BLE001 - re-pointing is the job here
        discovery_error = str(exc)[:300]
    return {
        "controller": row,
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
def fleet_test_controller(controller_id: str, payload: dict[str, Any] | None = None) -> Any:
    """Connection test: identity, role and release, straight from the controller.

    **An optional `url` (and `token`) tests a *candidate* address instead, storing nothing** — the
    "test first" step of a handover (`PUT .../address`): probe where the replacement head is going
    to be before the roster is re-pointed at it. A blank `token` keeps the stored credential, the
    same rule the re-point itself uses. The address is validated exactly as a re-point validates it.
    """
    row = next((c for c in fleet._read() if c.get("id") == controller_id), None)  # noqa: SLF001
    if row is None:
        return _error(404, "No such controller.")
    payload = payload or {}
    url = str(row["url"])
    token = fleet._token_for(controller_id)  # noqa: SLF001
    if str(payload.get("url") or "").strip():
        try:
            url = fleet._validate_address(str(payload["url"]))  # noqa: SLF001
        except ValueError as exc:
            return _error(400, str(exc))
        token = str(payload.get("token") or "").strip() or token
    # The credential comes from the store, not the roster: the roster deliberately
    # holds no secrets, so reading a token off the row probed with an empty one and
    # reported every correctly configured controller as unreachable.
    probed = fleet._probe(url, token)  # noqa: SLF001
    return {
        "id": controller_id,
        "url": url,
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
            fleet.credential_scope(url, token)
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
# Startup control — what each Mac starts when it starts, seen and changed from here
#
# `/api/fleet` carries a `startup` object on every machine row. These routes change it:
# "Start services on startup", "Start at login", and starting or restarting a Mac's services.
# Each goes to the Mac's *controller* with the Fleet token this gateway already holds (the same
# credential as every other fleet action), is relayed to the node, and is refused by the node
# while work is running unless `force` is true. Every request is appended to the audit log.
# ---------------------------------------------------------------------------

@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/startup",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_startup(controller_id: str, machine_id: str, payload: dict[str, Any]) -> Any:
    """Change one Mac's startup setting, or start/restart its services.

    Body: `{"action": "set_start_services_on_launch" | "set_open_at_login" | "start_services" |
    "restart_services", "value": true|false (the two setters), "force": true|false}`. The answer is
    the Hub's own: `state` is `applied`, `queued` (the app collects it on its next check),
    `handed_to_app` (a restart), `already_running`, or `failed` with the app's reason. A refusal keeps
    the Hub's status and code - `WORK_IN_PROGRESS` (409, with the running/queued counts) is the one a
    restart meets on a busy Mac - and a Mac with no Hub running is `NODE_UNREACHABLE` with
    `reason: no_hub`, which no remote action can fix.
    """
    try:
        return startup_control.request(
            controller_id, machine_id, payload.get("action"), payload.get("value"),
            payload.get("force", False),
        )
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/name",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_rename(controller_id: str, machine_id: str, payload: dict[str, Any]) -> Any:
    """Rename one Mac. Body `{"name": "…"}` (1-120 characters).

    Relayed through the Mac's controller, which renames it on the Mac itself and then in its roster
    (`startup_control.rename`). Answers `{"ok": true, "node", "name"}`, or the controller's refusal:
    `NODE_UNREACHABLE` / `NODE_REFUSED` (the Mac keeps its name), `HUB_TOO_OLD`, `BAD_NAME`.
    """
    try:
        return startup_control.rename(controller_id, machine_id, payload.get("name"))
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/pause",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_pause(controller_id: str, machine_id: str, payload: dict[str, Any]) -> Any:
    """Pause or resume one Mac by hand. Body `{"paused": true|false}`.

    Relayed through the Mac's controller, which keeps the flag in its roster and sends that Mac no new
    work (`startup_control.set_node_pause`); jobs already running there finish. Answers the controller's
    `{"ok": true, "node", "paused"}`, or its refusal: `HUB_TOO_OLD` (needs Studio Hub 0.6.135),
    `NODE_UNKNOWN`, `CONTROLLER_REFUSED`.
    """
    try:
        return startup_control.set_node_pause(controller_id, machine_id, payload.get("paused"))
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.get(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/work-policy",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_work_policy(controller_id: str, machine_id: str) -> Any:
    """Read one Mac's rankable local functions and saved work order."""
    try:
        return startup_control.get_node_work_policy(controller_id, machine_id)
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/work-policy",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_set_work_policy(
    controller_id: str, machine_id: str, payload: dict[str, Any]
) -> Any:
    """Replace one Mac's ranked work order; an empty order means no preference."""
    if set(payload) != {"priority_order"}:
        return _error(400, "provide only priority_order", "BAD_PRIORITY_ORDER")
    try:
        return startup_control.set_node_work_policy(
            controller_id, machine_id, payload.get("priority_order")
        )
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/workstation",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_workstation(controller_id: str, machine_id: str, payload: dict[str, Any]) -> Any:
    """Change one Mac's Lite Mode (internally "workstation"). Body: any subset of `startup_control.WORKSTATION_SETTINGS`.

    Relayed through the Mac's controller (`startup_control.set_node_workstation`); `machine_id` may be
    `local` or the controller's own Mac. Answers the controller's `{"ok": true, "node", "workstation"}`, or
    its refusal (`BAD_SETTING`, `NODE_UNKNOWN`, `NODE_UNREACHABLE`, `NODE_REFUSED`), or `HUB_TOO_OLD`.
    """
    try:
        return startup_control.set_node_workstation(controller_id, machine_id, payload)
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/model-packages/{package_id}/download",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_install_model(controller_id: str, machine_id: str, package_id: str) -> Any:
    """Install a model on one Mac (owner decision 2026-10-04). Relayed through the Mac's controller.

    Answers `{"ok": true, "node", "run": {"id", "state", "total_bytes", ...}}`, or the Mac's refusal in its
    own words (unknown package, shelved, not enough disk, not enough memory), `NODE_UNREACHABLE`,
    `NODE_REFUSED` (that Mac's Hub is too old) or `HUB_TOO_OLD` (the controller needs Studio Hub 0.6.143).
    """
    try:
        return startup_control.install_model(controller_id, machine_id, package_id)
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.get(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/model-downloads/{run_id}",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_model_download(controller_id: str, machine_id: str, run_id: str) -> Any:
    """How a model install started above is going: `run.state`, `run.bytes_done` / `run.total_bytes`."""
    try:
        return startup_control.model_install_status(controller_id, machine_id, run_id)
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.post(
    "/api/fleet/controllers/{controller_id}/machines/{machine_id}/transcribe",
    dependencies=[Depends(require_admin)],
)
def fleet_machine_transcribe(
    controller_id: str,
    machine_id: str,
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str = Form(default=""),
    word_timestamps: bool = Form(default=True),
) -> Any:
    """Run one test transcription on one chosen Mac and answer its word timings (model qualification).

    Relayed through the Mac's controller (Studio Hub 0.6.147, `startup_control.transcribe_on_machine`);
    `machine_id` may be `local` or the controller's own Mac. Synchronous (up to ~16 min), no job record,
    no catalog change. Answers the Mac's full Subtitle payload, or its refusal (`HUB_TOO_OLD`,
    `NODE_UNKNOWN`/`UNKNOWN_NODE`, `NODE_UNREACHABLE`, `NODE_REFUSED`, the worker's 409/422 sentence).
    """
    content = file.file.read()
    if not content:
        return _error(422, "the uploaded file is empty")
    try:
        return startup_control.transcribe_on_machine(
            controller_id, machine_id, content, Path(file.filename or "").name or "audio.wav",
            model=model.strip(), language=language.strip(), word_timestamps=word_timestamps)
    except startup_control.StartupError as error:
        return JSONResponse(status_code=error.status, content=error.body)


def _share_call(fn) -> Any:
    try:
        return fn()
    except model_share.ShareError as error:
        return JSONResponse(status_code=error.status, content=error.body)


@router.put("/api/fleet/controllers/{controller_id}/network", dependencies=[Depends(require_admin)])
def fleet_location_network(controller_id: str, payload: dict[str, Any]) -> Any:
    """Name the network a location's Macs are on (`{"network": "A"}`; empty clears it). LAN model sharing pairs
    only Macs on the same named network."""
    return _share_call(lambda: {"networks": model_share.set_network(controller_id, payload.get("network"))})


@router.put("/api/fleet/controllers/{controller_id}/machines/{machine_id}/network",
            dependencies=[Depends(require_admin)])
def fleet_machine_network(controller_id: str, machine_id: str, payload: dict[str, Any]) -> Any:
    """One Mac's own network, over its location's (`{"network": "C"}`; empty goes back to the location's)."""
    return _share_call(lambda: {"networks": model_share.set_network(controller_id, payload.get("network"),
                                                                    machine_id)})


@router.post("/api/fleet/model-installs", dependencies=[Depends(require_admin)])
def fleet_install_everywhere(payload: dict[str, Any]) -> Any:
    """Install a model on every Mac that can run it (`{"package_id"}`): one internet download per network, then
    Mac-to-Mac copies on the LAN (`model_share`). Answers `{"plan"}`; progress rides on `GET /api/fleet`."""
    return _share_call(lambda: {"plan": model_share.start(str(payload.get("package_id") or ""))})


@router.get("/api/fleet/model-installs", dependencies=[Depends(require_admin)])
def fleet_install_everywhere_status() -> dict[str, Any]:
    return {"plans": model_share.status()}


@router.post("/api/fleet/model-installs/{plan_id}/stop", dependencies=[Depends(require_admin)])
def fleet_install_everywhere_stop(plan_id: str) -> Any:
    """Start nothing new; copies and downloads already running finish on their Macs."""
    return _share_call(lambda: {"plan": model_share.stop(plan_id)})


@router.post("/api/fleet/startup/enable-autostart", dependencies=[Depends(require_admin)])
def fleet_enable_autostart(payload: dict[str, Any]) -> Any:
    """Turn "Start services on startup" on for every Mac that reported it off.

    `{"dry_run": true}` lists what it would do and touches nothing. Macs that said nothing (an older
    Hub) are not touched, and a Mac that is not answering is listed with why, not tried.
    """
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return _error(400, "dry_run must be true or false", "BAD_REQUEST")
    return startup_control.enable_autostart_where_off(dry_run=dry_run)


@router.get("/api/fleet/startup/audit", dependencies=[Depends(require_admin)])
def fleet_startup_audit(limit: int = 50) -> dict[str, Any]:
    """The last startup requests this gateway sent, newest first."""
    return {"audit": startup_control.recent_audit(limit)}


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

    This is the only place the owner can read the App Update Key public key to
    install on a controller and its comparison fingerprint. The private half is
    never part of the answer.
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

    **Paced by default** (`release_rollout`): this Mac's Hub, then one controller at a
    time, each waited for until it reports the new version. The answer is the rollout
    (`{"rollout": …}`) and the pushes run in the background. `"mode": "all_at_once"`
    is the emergency path: every controller now, in parallel, answered when done (it
    stops a running rollout first). Naming `controller_ids` pushes to exactly those, now.
    """
    artifact = str(payload.get("artifact_path") or "").strip()
    if not artifact:
        return _error(422, "expected {'artifact_path', 'version', 'build'}")
    filename = payload.get("filename")
    mode = str(payload.get("mode") or "paced")
    if mode not in ("paced", "all_at_once"):
        return _error(422, "mode is 'paced' (the default) or 'all_at_once'")
    try:
        if mode == "paced" and not isinstance(payload.get("controller_ids"), list):
            return {"rollout": release_rollout.start(
                artifact_path=artifact, version=str(payload.get("version") or ""),
                build=str(payload.get("build") or ""), filename=str(filename) if filename else None,
                allow_downgrade=bool(payload.get("allow_downgrade")))}
        if mode == "all_at_once":
            release_rollout.stop("replaced by an all-at-once publish")
        return release_publisher.publish(
            artifact_path=artifact,
            version=str(payload.get("version") or ""),
            build=str(payload.get("build") or ""),
            filename=str(filename) if filename else None,
            allow_downgrade=bool(payload.get("allow_downgrade")),
            controller_ids=([str(c) for c in payload["controller_ids"]]
                            if isinstance(payload.get("controller_ids"), list) else None),
        )
    except release_publisher.ReleaseError as exc:
        return _error(exc.status, exc.message, exc.code)


@router.get("/api/release/rollout", dependencies=[Depends(require_admin)])
def release_rollout_status() -> Any:
    return {"rollout": release_rollout.status(fleet.snapshot())}


@router.post("/api/release/rollout/stop", dependencies=[Depends(require_admin)])
def release_rollout_stop() -> Any:
    """Stop after the push in flight (if any); nothing is undone, and Resume carries on."""
    release_rollout.stop()
    return {"rollout": release_rollout.status(fleet.snapshot())}


@router.post("/api/release/rollout/resume", dependencies=[Depends(require_admin)])
def release_rollout_resume() -> Any:
    release_rollout.resume()
    return {"rollout": release_rollout.status(fleet.snapshot())}


# ---------------------------------------------------------------------------
# Story Studio releases — the same publisher key, offered to Story Macs on their
# own check-in (`story_releases`). Publishing never pushes: each Story Mac fetches,
# verifies and installs when its queue is drained.
# ---------------------------------------------------------------------------

def _story_call(call: Any) -> Any:
    from . import story_releases
    try:
        return call()
    except story_releases.StoryReleaseError as exc:
        return _error(exc.status, exc.message, exc.code)
    except release_publisher.ReleaseError as exc:
        return _error(exc.status, exc.message, exc.code)


@router.post("/api/story/release/publish", dependencies=[Depends(require_admin)])
def story_release_publish(payload: dict[str, Any]) -> Any:
    """Sign a Story DMG or .zip (version and build read from the bundle) and offer it."""
    from . import story_releases
    artifact = str(payload.get("artifact_path") or "").strip()
    if not artifact:
        return _error(422, "expected {'artifact_path'}")
    return _story_call(lambda: story_releases.publish(artifact))


@router.get("/api/story/instances", dependencies=[Depends(require_admin)])
def story_instances() -> Any:
    """Every Story Mac, its version against the release, and its jobs on this Gateway."""
    from . import story_releases
    counts = store.active_counts_by_caller()
    out = story_releases.instances()
    for row in out["instances"]:
        row["gateway_jobs"] = counts.get(f"story-studio@{row['instance_id']}",
                                         {"waiting": 0, "running": 0})
    return out


@router.post("/api/story/instances/{instance}/update", dependencies=[Depends(require_admin)])
def story_instance_update(instance: str) -> Any:
    """"Update now": the Mac installs on its next check-in (at most 30 minutes away), after draining."""
    from . import story_releases
    return _story_call(lambda: {"instance": story_releases.request_update(instance)})
