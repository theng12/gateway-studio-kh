"""Hub-owned image admission tables and explicit, unconfirmed owner intent."""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.Lock()
SCHEMA = "studiohub.image-reference-limits.v1"


def table(raw: dict) -> dict | None:
    contract = raw.get("lane_contract")
    contract = contract if isinstance(contract, dict) else {}
    value = contract.get("reference_limits_table")
    if value is None:
        references = contract.get("references")
        value = references.get("limits_table") if isinstance(references, dict) else None
    if value is None:
        for mode in raw.get("image_modes") or []:
            if isinstance(mode, dict) and isinstance(mode.get("references"), dict):
                value = mode["references"].get("limits_table")
                if value is not None:
                    break
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or not isinstance(value.get("revision"), str) or not value["revision"].strip()
            or not isinstance(value.get("rows"), list)):
        raise ValueError("The Hub image limits table is missing a supported schema, revision or rows.")
    return value


def validate_cell(value: Any, *, provenance: bool = False) -> dict:
    if (not isinstance(value, dict) or type(value.get("allowed")) is not bool
            or type(value.get("max_reference_images")) is not int or value["max_reference_images"] < 0
            or type(value.get("swap_allowance_gb")) not in (int, float)
            or not math.isfinite(value["swap_allowance_gb"]) or value["swap_allowance_gb"] < 0):
        raise ValueError("A cell needs On/Off, a non-negative whole reference maximum and a finite non-negative swap allowance.")
    if provenance and (not isinstance(value.get("source"), str) or not value["source"].strip()):
        raise ValueError("The Hub did not state this cell's provenance.")
    return value


def job_type(params: dict) -> tuple[str, int]:
    return ("edit", len(params.get("_reference_digests") or [])) if "_reference_digests" in params else ("txt2img", 0)


def worker_tier(worker: dict, tiers: dict) -> str | None:
    contract = worker.get("lane_contract") or {}
    refs = contract.get("references") if isinstance(contract, dict) else None
    if not isinstance(refs, dict):
        refs = next((m.get("references") for m in worker.get("image_modes") or []
                     if isinstance(m, dict) and m.get("id") == "edit"), None)
    stated = refs.get("tier") if isinstance(refs, dict) else None
    if stated is not None:
        return str(stated) if str(stated) in tiers else None
    memory = worker.get("memory_gb")
    if isinstance(memory, bool) or not isinstance(memory, (int, float)) or not math.isfinite(memory) or memory <= 0:
        return None
    physical = round(memory * 1_000_000_000 / 1024 ** 3)
    bands = [int(k) for k in tiers if isinstance(k, str) and k.isdecimal() and int(k) > 0]
    return str(max((band for band in bands if band <= physical), default=0)) if bands else None


def cell_for(value: dict, model_id: str, mode: str, count: int, worker: dict) -> dict:
    rows = [row for row in value["rows"] if isinstance(row, dict)
            and row.get("model_id") == model_id and row.get("mode") == mode
            and (row.get("reference_count") is None or type(row.get("reference_count")) is int
                 and row["reference_count"] == count)]
    exact = [row for row in rows if row.get("reference_count") is not None]
    rows = exact or rows
    if len(rows) != 1 or not isinstance(rows[0].get("tiers"), dict):
        raise ValueError("The Hub did not publish one unambiguous cell for this model, mode and reference count.")
    tiers = rows[0]["tiers"]
    tier = worker_tier(worker, tiers)
    if tier not in tiers:
        raise ValueError("This node's physical RAM tier is not reported in the Hub table.")
    return validate_cell(tiers[tier], provenance=True)


def assess(raw: dict, model_id: str, params: dict) -> dict | None:
    workers = raw.get("image_workers")
    workers = workers if isinstance(workers, list) and workers else [raw]
    mode, count = job_type(params)
    target = (params.get("measurement_target") or {}).get("machine")
    known = False
    eligible = set()
    reasons = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            continue
        try:
            value = table(worker)
            if value is None:
                continue
            known = True
            if target and worker.get("hub_machine") != target:
                continue
            if any(worker.get(k) is False for k in ("hub_cached", "memory_eligible", "runtime_ready", "available")):
                continue
            declared = next((m for m in worker.get("image_modes") or []
                             if isinstance(m, dict) and m.get("id") == mode), None)
            if declared is not None and declared.get("available") is not True:
                continue
            cell = cell_for(value, model_id, mode, count, worker)
            if cell["allowed"] and (mode == "txt2img" or 0 < count <= cell["max_reference_images"]):
                eligible.add(str(worker.get("hub_machine") or f"unidentified:{index}"))
            else:
                reasons.append(f"{cell['source']}: this cell is Off or below the requested reference count")
        except ValueError as error:
            known = True
            reasons.append(str(error))
    if not known:
        return None
    reason = f"No node allows {model_id}, {mode}, {count} reference image(s)."
    if reasons:
        reason += " " + "; ".join(dict.fromkeys(reasons))
    return {"allowed": bool(eligible), "eligible": len(eligible),
            "machines": sorted(m for m in eligible if not m.startswith("unidentified:")), "reason": reason}


def published(raw: dict, model_id: str) -> dict | None:
    text = assess(raw, model_id, {})
    if text is None:
        return None
    boundaries = {1}
    maximum = 0
    for worker in raw.get("image_workers") or [raw]:
        try:
            value = table(worker)
            for row in value["rows"] if value else []:
                if row.get("model_id") != model_id or row.get("mode") != "edit":
                    continue
                for cell in (row.get("tiers") or {}).values():
                    maximum = max(maximum, validate_cell(cell, provenance=True)["max_reference_images"])
                count = row.get("reference_count")
                if type(count) is int and count > 0:
                    boundaries.update((count, count + 1))
        except (ValueError, AttributeError, TypeError):
            continue
    boundaries.add(maximum)
    cap = maximum
    for count in sorted(n for n in boundaries if 0 < n <= maximum):
        decision = assess(raw, model_id, {"_reference_digests": range(count)})
        if not decision or not decision["allowed"]:
            cap = count - 1
            break
    return {"txt2img": text["allowed"], "edit": cap > 0, "max": cap}


def for_provider(provider_id: str, model_id: str, params: dict) -> dict | None:
    from .providers import registry
    for definition, model in registry.models.enabled_models("image"):
        if definition.id == provider_id and definition.kind == "studio_hub" and model["model_id"] == model_id:
            return assess(model.get("raw") or {}, model_id, params)
    return None


def refusal(compound: str, params: dict, only_location: str | None = None) -> str | None:
    from . import fleet, image_assignment, pools
    from .providers import registry
    provider, _, model_id = compound.partition("::")
    pool_name = pools.image_pool_name(compound)
    members = image_assignment.pool_models(pools.get(pool_name)) if pool_name is not None else [model_id]
    decisions = []
    for definition, model in registry.models.enabled_models("image"):
        location = fleet.controller_id_from(definition.id)
        if location is None or model["model_id"] not in members:
            continue
        if only_location is not None and location != only_location:
            continue
        if pool_name is None and provider not in (fleet.FLEET_PROVIDER_ID, definition.id):
            continue
        decision = assess(model.get("raw") or {}, model["model_id"], params)
        if decision is None or decision["allowed"]:
            return None
        decisions.append(decision)
    return decisions[0]["reason"] if decisions else None


def _path() -> Path:
    return Path(config.DATA_DIR) / "image-limits-intent.json"


def changes() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError("Saved image limit changes cannot be read; no change was applied.") from error
    if not isinstance(value, dict):
        raise ValueError("Saved image limit changes must be an object.")
    for key, change in value.items():
        if not isinstance(key, str) or not isinstance(change, dict) or set(change) != {"allowed", "max_reference_images", "swap_allowance_gb"}:
            raise ValueError("Saved image limit changes contain an invalid cell.")
        validate_cell(change)
    return value


def identity(row: dict, tier: str) -> str:
    return json.dumps([row["provider_id"], row["model_id"], row["mode"], row["reference_count"], tier], separators=(",", ":"))


def view() -> dict:
    from .providers import registry
    saved = changes()
    rows = []
    units = set()
    for definition in registry.definitions():
        if definition.kind != "studio_hub":
            continue
        seen = set()
        for model in registry.models.models_for(definition.id):
            if model.get("modality") != "image":
                continue
            raw = model.get("raw") or {}
            for worker in raw.get("image_workers") or [raw]:
                value = table(worker)
                if value is None:
                    continue
                units.add(str(value.get("swap_allowance_units") or "Not reported"))
                for supplied in value["rows"]:
                    if not isinstance(supplied, dict) or supplied.get("model_id") != model["model_id"]:
                        continue
                    row = {"provider_id": definition.id, "provider_label": definition.display_name,
                           "model_id": model["model_id"], "mode": supplied.get("mode"),
                           "reference_count": supplied.get("reference_count"), "revision": value["revision"]}
                    key = json.dumps([row, supplied.get("tiers")], sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                    row["tiers"] = {}
                    for tier, default in (supplied.get("tiers") or {}).items():
                        validate_cell(default, provenance=True)
                        change = saved.get(identity(row, tier))
                        # ponytail: Hub222 has no writer; connect the agreed signed Hub action before any intent becomes effective.
                        row["tiers"][tier] = {"default": copy.deepcopy(default), "effective": copy.deepcopy(default),
                                              "change": change, "status": "awaiting_hub_support" if change is not None else "hub_default",
                                              "detail": "Saved here; awaiting Hub override support. Not applied." if change is not None else "Hub default"}
                    rows.append(row)
    return {"rows": rows, "swap_allowance_units": "; ".join(sorted(units)) or "Not reported"}


def save(payload: dict) -> dict:
    keys = {"provider_id", "model_id", "mode", "reference_count", "tier", "change"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("Send one published model/mode/count/tier cell and its change, or null to Reset.")
    if (any(not isinstance(payload[k], str) or not payload[k] for k in ("provider_id", "model_id", "mode", "tier"))
            or payload["reference_count"] is not None and (type(payload["reference_count"]) is not int or payload["reference_count"] < 0)):
        raise ValueError("Use the published string identities and a non-negative whole reference count or null.")
    change = payload["change"]
    if change is not None:
        if not isinstance(change, dict) or set(change) != {"allowed", "max_reference_images", "swap_allowance_gb"}:
            raise ValueError("A change contains only allowed, max_reference_images and swap_allowance_gb.")
        validate_cell(change)
    with _LOCK:
        published = view()["rows"]
        match = [row for row in published if all(row.get(k) == payload[k] and type(row.get(k)) is type(payload[k])
                 for k in ("provider_id", "model_id", "mode", "reference_count")) and payload["tier"] in row["tiers"]]
        if not match:
            raise ValueError("This cell is no longer published by a registered Hub. Refresh the table.")
        current = changes()
        key = identity(match[0], payload["tier"])
        if change is None:
            current.pop(key, None)
        else:
            current[key] = change
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(current, stream, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        Path(temporary).replace(path)
    return view()
