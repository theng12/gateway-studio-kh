"""Which image model each Mac serves for the image pool (owner, 2026-10-24).

Each Mac is **Auto** - the image pool's model for its RAM tier (`pools.tier_models_of`) - or the
owner's own pick, kept here. The result is sent to the Mac's controller, which stores it
(`assigned_image_model` in its machine flags), refuses pool work for any other model on that Mac,
and reports it back on every Mac row. Routing (`fleet.pool_candidates`) reads that reported value,
so the Gateway and the Hub can never disagree about where pool work may go.

Only image-pool work reads the assignment. A job that names its model runs on any Mac that has it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("gateway.image_assignment")

_LOCK = threading.Lock()


def _path() -> Path:
    return Path(config.DATA_DIR) / "image-model-overrides.json"


def overrides() -> dict[str, str]:
    """`"<controller>/<machine>" -> Hub model id` for every Mac the owner pinned by hand."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"image-model-overrides.json is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("image-model-overrides.json is unreadable: expected an object")
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v.strip()}


def set_override(controller_id: str, machine_id: str, model: str | None) -> dict[str, str]:
    """Pin a Mac to a model (`None` returns it to Auto). Written atomically."""
    from . import fleet
    key = f"{controller_id}/{machine_id}"
    with _LOCK:
        current = overrides()
        if model is None or not str(model).strip():
            current.pop(key, None)
        else:
            current[key] = fleet.pool_member_model(str(model))
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        Path(name).replace(path)
        return current


def purge(controller_id: str, machine_id: str | None = None) -> int:
    """Drop image-model pin overrides for one machine or an entire controller."""
    if not controller_id:
        return 0
    prefix = f"{controller_id}/"
    exact = f"{controller_id}/{machine_id}" if machine_id else None
    with _LOCK:
        try:
            current = overrides()
        except RuntimeError:
            return 0
        gone = [
            key for key in current
            if (key == exact if exact is not None else key.startswith(prefix))
        ]
        if not gone:
            return 0
        for key in gone:
            current.pop(key, None)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        Path(name).replace(path)
        return len(gone)


def _tier_map() -> dict[str, str]:
    """RAM tier (GB, as text) -> Hub model id, from the image pool. The first image pool by name
    decides Auto; one pool is the intended setup, and Auto with two disagreeing pools has no answer."""
    from . import fleet, pools
    for pool in sorted(pools.image_pools(), key=lambda p: str(p.get("name"))):
        mapping = pools.tier_models_of(pool)
        if mapping:
            return {tier: fleet.pool_member_model(model) for tier, model in mapping.items()}
    return {}


def pool_models(pool: dict[str, Any] | None) -> list[str]:
    """Every model a Mac may be assigned for this pool: its per-tier models plus the owner's pins."""
    from . import fleet, pools
    models = [fleet.pool_member_model(m) for m in pools.tier_models_of(pool).values()]
    models += list(overrides().values())
    return list(dict.fromkeys(m for m in models if m))


def desired(controller_id: str, machine: dict[str, Any], *, pins: dict[str, str] | None = None,
            tiers: dict[str, str] | None = None) -> tuple[str | None, str]:
    """`(model, how)` this Mac should serve: `how` is "pinned", "auto" or why it has none."""
    from . import fleet
    machine_id = str(machine.get("machine") or "")
    pins = overrides() if pins is None else pins
    if f"{controller_id}/{machine_id}" in pins:
        return pins[f"{controller_id}/{machine_id}"], "pinned"
    tier = fleet.machine_tier(machine.get("memory_gb"))
    if tier is None:
        return None, "its memory size is not reported"
    model = (_tier_map() if tiers is None else tiers).get(str(tier))
    return (model, "auto") if model else (None, f"the image pool names no model for {tier} GB")


def view() -> list[dict[str, Any]]:
    """Every Mac with its Auto/pinned model, what its Hub reports, and whether they agree."""
    from . import fleet
    pins, tiers = overrides(), _tier_map()
    out: list[dict[str, Any]] = []
    for row in fleet.snapshot(allow_stale=True).get("locations") or []:
        controller_id = str(row.get("id") or "")
        for machine in row.get("machines") or []:
            if not isinstance(machine, dict) or not machine.get("machine"):
                continue
            model, how = desired(controller_id, machine, pins=pins, tiers=tiers)
            reported = machine.get("assigned_image_model")
            installed = {str(m.get("model_id") or m.get("model") or "") for m in row.get("local_models") or []
                         if isinstance(m, dict) and str(m.get("machine") or "") == str(machine["machine"])}
            out.append({
                "controller_id": controller_id, "controller": row.get("label") or controller_id,
                "machine": machine["machine"], "label": machine.get("label") or machine["machine"],
                "memory_gb": machine.get("memory_gb"), "tier": fleet.machine_tier(machine.get("memory_gb")),
                "mode": "pinned" if how == "pinned" else "auto", "model": model,
                "reason": None if model else how, "reported": reported,
                # A Hub that predates this setting reports nothing at all (no key): say so.
                "hub_supports": "assigned_image_model" in machine,
                "in_sync": reported == model, "installed": bool(model) and model in installed,
            })
    return out


def sync() -> list[dict[str, Any]]:
    """Send each reachable Mac's model to its controller where the Hub reports something else.

    Called after every pool or pin change and on each fleet refresh. A failure is logged and the
    next refresh tries again; routing only ever trusts what the Hub reports.
    """
    from . import startup_control
    sent: list[dict[str, Any]] = []
    for item in view():
        if item["in_sync"] or not item["hub_supports"]:
            continue
        # No model because this refresh could not read the Mac's memory is not a decision to clear
        # it: site 1300 was sent None and then its model again, hundreds of times an hour.
        if item["model"] is None and item["tier"] is None:
            continue
        try:
            startup_control.set_node_image_model(item["controller_id"], item["machine"], item["model"])
            sent.append({"controller_id": item["controller_id"], "machine": item["machine"], "model": item["model"]})
        except Exception as exc:  # noqa: BLE001 - one unreachable Mac must not stop the others
            log.warning("image model for %s/%s not sent: %s", item["controller_id"], item["machine"], exc)
    return sent
