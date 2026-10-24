"""Model pools — one name, several models, tried in order.

A pool is a *pick* rather than a *product*. Story Studio saves one model id, and
the gateway walks the members until one answers, so a spent free lane costs a
retry instead of the job.

Why this lives here rather than in a router in front
----------------------------------------------------
The gateway already discovers each provider's models **live** and already records
what each provider does with a prompt (`data_use`). A pool built on top of those
two facts inherits both, which is exactly what a second router in the path did
not: it kept its own snapshot of which models a provider had, and that snapshot
went stale — it was still offering models the provider had retired and refusing
ones it had shipped, while this gateway had them all along. A pool is a list of
this gateway's own ids, so it cannot describe a model this gateway cannot reach.

How it plugs in
---------------
A pool is published as a **provider with no credential** whose models are its
pools. That is deliberate: `is_configured()` is true for a provider with no
required fields, and `models_for()` already reads `static_models`, so pools reach
the catalogue, the enable toggle and `/v1/models` through code that already
exists — there is no second listing path to keep in step with the first.

The trying-in-order happens in `adapters/pool.py`, not in the chat route. The
route resolves one model and calls `generate`, exactly as it does for every other
provider, so the synchronous and durable paths behave identically and neither
needed changing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, write_secret
from .providers.definitions import (
    DISCOVERY_STATIC,
    KIND_POOL,
    ProviderDefinition,
)

#: The provider id every pool is published under.
POOL_PROVIDER_ID = "pool"
ROUTING_STRICT_ORDER = "strict_order"
ROUTING_FIRST_FREE = "first_free"
MODALITY_CHAT = "chat"
MODALITY_IMAGE = "image"

POOLS_PATH = Path(os.environ.get("GATEWAY_POOLS") or (DATA_DIR / "pools.json"))

#: Bumped writes are detected by this, not by mtime alone — a coarse filesystem
#: can repeat an mtime inside one second and silently drop a change.
_revision = 0

#: Parsed-file cache, keyed on (revision, mtime) exactly like the fleet roster.
_cache: dict[str, Any] = {"key": None, "pools": [], "invalid_routing_policies": []}


def revision() -> int:
    return _revision


def _load() -> list[dict[str, Any]]:
    """Every pool, newest read wins when the file changed under us."""
    global _cache
    try:
        key: tuple = (_revision, os.path.getmtime(POOLS_PATH))
    except OSError:
        key = (_revision, None)
    if _cache["key"] == key:
        return _cache["pools"]

    pools: list[dict[str, Any]] = []
    invalid_routing_policies: list[tuple[str, str]] = []
    try:
        parsed = json.loads(POOLS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        for item in parsed.get("pools") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            modality = str(item.get("modality") or "chat").strip().lower()
            if modality not in ("chat", "image"):
                modality = "chat"
            tier_models = item.get("tier_models")
            if not isinstance(tier_models, dict):
                tier_models = None
            if tier_models is not None:
                tier_models = {
                    str(k).strip(): str(v).strip()
                    for k, v in tier_models.items()
                    if str(k).strip() and isinstance(v, str) and str(v).strip()
                } or None
            members = [
                str(m).strip()
                for m in (item.get("members") or [])
                if isinstance(m, str) and str(m).strip()
            ]
            if modality == "image" and tier_models:
                members = list(dict.fromkeys(tier_models.values()))
            if not name or not members:
                continue
            try:
                routing_policy = _clean_routing_policy(item.get("routing_policy"))
            except ValueError:
                invalid_routing_policies.append(
                    (name, str(item.get("routing_policy") or "").strip())
                )
                continue
            pool = {
                "name": name,
                "display_name": str(item.get("display_name") or "").strip() or name,
                "description": str(item.get("description") or "").strip(),
                "members": list(dict.fromkeys(members)),
                "modality": modality,
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                **({"routing_policy": routing_policy} if routing_policy else {}),
            }
            if tier_models:
                pool["tier_models"] = tier_models
            pools.append(pool)
    pools.sort(key=lambda p: p["name"])
    _cache = {
        "key": key,
        "pools": pools,
        "invalid_routing_policies": invalid_routing_policies,
    }
    return pools


def _save(pools: list[dict[str, Any]]) -> None:
    global _revision
    _load()
    invalid = _cache.get("invalid_routing_policies") or []
    if invalid:
        name, policy = invalid[0]
        raise ValueError(
            f"cannot modify pools while '{name}' has unknown routing policy '{policy}'; "
            "repair that saved policy first"
        )
    payload = {"version": 1, "pools": pools}
    write_secret(POOLS_PATH, json.dumps(payload, indent=2) + "\n")
    _revision += 1
    _cache["key"] = None


# -- reads ----------------------------------------------------------------
def pools() -> list[dict[str, Any]]:
    return [dict(p) for p in _load()]


def names() -> list[str]:
    return [p["name"] for p in _load()]


def get(name: str) -> dict[str, Any] | None:
    wanted = (name or "").strip()
    for pool in _load():
        if pool["name"] == wanted:
            return dict(pool)
    return None


def members(name: str) -> list[str]:
    pool = get(name)
    return list(pool["members"]) if pool else []


def routing_policy(name: str) -> str | None:
    pool = get(name)
    return pool.get("routing_policy") if pool else None


def definition() -> ProviderDefinition | None:
    """The synthetic provider pools are published under.

    Chat pools only. Image pools publish under the Studio Hub maker namespace
    through the catalogue (alias layer), so they stay free fleet. `None` when
    there are no chat pools.
    """
    current = [p for p in _load() if modality_of(p) == "chat"]
    if not current:
        return None
    return ProviderDefinition(
        id=POOL_PROVIDER_ID,
        display_name="Model pools",
        category="language",
        kind=KIND_POOL,
        base_url="",
        modalities=("chat",),
        discovery=DISCOVERY_STATIC,
        description=(
            "One name for several models, tried in order. A pool answers with the "
            "first member that can serve the request."
        ),
        static_models=tuple(
            {
                "id": pool["name"],
                "display_name": pool["display_name"],
                "modality": "chat",
            }
            for pool in current
        ),
        credential_fields=(),
        default_enabled=True,
        auto_enable_discovered=True,
        notes=(
            "A pool is a list of this gateway's own model ids. Editing it does not "
            "touch any provider."
        ),
    )


# -- writes ---------------------------------------------------------------
def describe(name: str) -> dict[str, Any] | None:
    pool = get(name)
    if pool is None:
        return None
    return _with_health(pool)


def describe_all() -> list[dict[str, Any]]:
    return [_with_health(pool) for pool in _load()]


def _with_health(pool: dict[str, Any]) -> dict[str, Any]:
    """Attach whether each member still resolves, so a stale member is visible.

    **Not a guarantee that the member will answer** -- resolution only says the
    provider is configured and the model is enabled. Whether it answers right now
    is what the pool itself is for. But "this entry names a model you have since
    disabled" is knowable here and is the failure that otherwise shows up as a
    pool that mysteriously works less well than its list suggests.
    """
    from . import pool_cooldowns, pricing
    from .catalog import catalog

    out = dict(pool)
    cooling = pool_cooldowns.active()
    rows: list[dict[str, Any]] = []
    _mod = "image" if modality_of(pool) == "image" else "chat"
    for member in pool["members"]:
        definition, model = catalog.resolve(_mod, member)
        rows.append(
            {
                "id": member,
                "resolves": definition is not None and model is not None,
                "provider_id": definition.id if definition else "",
                "provider_name": (
                    definition.display_name if definition else _provider_name(member)
                ),
                "model_id": model["model_id"] if model else "",
                # Out of quota until this epoch time (None when not cooling down).
                "cooldown_until": cooling[member]["until"] if member in cooling else None,
                "cooldown_kind": cooling[member]["kind"] if member in cooling else None,
                "cooldown_reason": cooling[member]["reason"] if member in cooling else "",
                "state": "ok" if (definition is not None and model is not None) else _why_unresolved(member),
                "model_name": (model.get("display_name") or model["model_id"]) if model else "",
                # Can cost money, by the catalogue's own rule (None when it does not resolve).
                "paid": pricing.model_chargeable(definition, model) if definition and model else None,
            }
        )
    out["members_detail"] = rows
    # Default pools use free-first. An explicitly ordered pool uses the stored
    # sequence, while every member's live paid/free fact remains visible above.
    out["paid_last_resort"] = (
        pool.get("routing_policy") != ROUTING_STRICT_ORDER
        and any(r["paid"] for r in rows)
        and any(r["paid"] is False for r in rows)
    )
    out["cooling_count"] = sum(1 for r in rows if r["cooldown_until"])
    out["resolvable_count"] = sum(1 for r in rows if r["resolves"])
    return out


def _provider_name(member: str) -> str:
    """The provider's display name for a member that no longer resolves (its row still names it)."""
    from .providers import registry

    found = registry.definition(member.partition("::")[0])
    return found.display_name if found else ""


def _why_unresolved(member: str) -> str:
    """`provider_off`, `disabled` (model switched off) or `missing` (provider/model unknown)."""
    from .providers import registry

    provider_id, _, model_id = member.partition("::")
    definition = registry.definition(provider_id)
    if definition is None:
        return "missing"
    if not registry.credentials.is_enabled(provider_id):
        return "provider_off"
    offered = {m["model_id"] for m in registry.models.models_for(provider_id, include_disabled=True)}
    return "disabled" if model_id in offered else "missing"


def create(
    name: str,
    members: list[str],
    *,
    display_name: str = "",
    description: str = "",
    routing_policy: str | None = None,
    modality: str = "chat",
    tier_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    modality = str(modality or "chat").strip().lower()
    if modality not in ("chat", "image"):
        modality = "chat"
    if modality == "image":
        if not isinstance(tier_models, dict) or not tier_models:
            raise ValueError("an image pool needs a RAM-tier to model mapping")
        members = list(dict.fromkeys(str(v).strip() for v in tier_models.values() if str(v).strip()))
    cleaned = _clean(name, members)
    policy = _clean_routing_policy(routing_policy)
    now = time.time()
    pool = {
        "name": cleaned["name"],
        "display_name": (display_name or "").strip() or cleaned["name"],
        "description": (description or "").strip(),
        "members": cleaned["members"],
        "modality": modality,
        "created_at": now,
        "updated_at": now,
        **({"routing_policy": policy} if policy else {}),
    }
    if modality == "image" and tier_models:
        pool["tier_models"] = {
            str(k).strip(): str(v).strip() for k, v in tier_models.items()
            if str(k).strip() and str(v).strip()
        }
    _save([p for p in _load() if p["name"] != pool["name"]] + [pool])
    return pool


def update(
    name: str,
    *,
    members: list[str] | None = None,
    display_name: str | None = None,
    description: str | None = None,
    routing_policy: str | None = None,
    modality: str | None = None,
    tier_models: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    existing = get(name)
    if existing is None:
        return None
    if modality is not None:
        existing["modality"] = str(modality or "chat").strip().lower()
        if existing["modality"] not in ("chat", "image"):
            existing["modality"] = "chat"
    if tier_models is not None:
        existing["tier_models"] = {
            str(k).strip(): str(v).strip() for k, v in tier_models.items()
            if str(k).strip() and str(v).strip()
        }
        existing["members"] = list(dict.fromkeys(existing["tier_models"].values()))
    elif members is not None:
        existing["members"] = _clean(name, members)["members"]
    if display_name is not None:
        existing["display_name"] = display_name.strip() or existing["name"]
    if description is not None:
        existing["description"] = description.strip()
    if routing_policy is not None:
        policy = _clean_routing_policy(routing_policy)
        if policy:
            existing["routing_policy"] = policy
        else:
            existing.pop("routing_policy", None)
    existing["updated_at"] = time.time()
    _save([existing if p["name"] == existing["name"] else p for p in _load()])
    return existing


def remove(name: str) -> bool:
    current = _load()
    kept = [p for p in current if p["name"] != (name or "").strip()]
    if len(kept) == len(current):
        return False
    _save(kept)
    return True


def _clean(name: str, members: list[str]) -> dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("a pool needs a name")
    if "::" in clean_name or "/" in clean_name:
        # The id Story Studio sends back is `pool::<name>`, so a name containing
        # the separator would resolve to a different id than the one published.
        raise ValueError("a pool name cannot contain '::' or '/'")
    seen: list[str] = []
    for member in members or []:
        text = str(member).strip()
        if not text:
            continue
        if "::" not in text:
            raise ValueError(
                f"'{text}' is not a namespaced model id — pools hold this gateway's "
                "own ids, which look like 'provider::model'"
            )
        if text.startswith(f"{POOL_PROVIDER_ID}::"):
            # A pool of pools is a loop, and a one-member pool of itself is an
            # infinite one. Refused here rather than detected at request time.
            raise ValueError("a pool cannot contain another pool")
        if text not in seen:
            seen.append(text)
    if not seen:
        raise ValueError("a pool needs at least one member")
    return {"name": clean_name, "members": seen}


def _clean_routing_policy(value: Any) -> str | None:
    policy = str(value or "").strip()
    if not policy:
        return None
    if policy != ROUTING_STRICT_ORDER:
        raise ValueError(f"unknown pool routing policy '{policy}'")
    return policy


def modality_of(pool: dict[str, Any] | None) -> str:
    if not isinstance(pool, dict):
        return "chat"
    value = str(pool.get("modality") or "chat").strip().lower()
    return value if value in ("chat", "image") else "chat"


def image_pools() -> list[dict[str, Any]]:
    return [p for p in _load() if modality_of(p) == "image"]


def is_image_pool_name(compound_or_name: str) -> bool:
    text = str(compound_or_name or "").strip()
    if text.startswith(f"{POOL_PROVIDER_ID}::"):
        text = text.split("::", 1)[1]
    # image pools publish under studio-hub::, so accept that too
    if text.startswith("studio-hub::"):
        text = text.split("::", 1)[1]
    for pool in image_pools():
        if pool["name"] == text:
            return True
    return False


def image_pool_name(compound_or_name: str) -> str | None:
    text = str(compound_or_name or "").strip()
    if text.startswith(f"{POOL_PROVIDER_ID}::") or text.startswith("studio-hub::"):
        text = text.split("::", 1)[1]
    for pool in image_pools():
        if pool["name"] == text:
            return pool["name"]
    return None


def tier_models_of(pool: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(pool, dict):
        return {}
    value = pool.get("tier_models")
    if not isinstance(value, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in value.items()
            if str(k).strip() and isinstance(v, str) and str(v).strip()}
