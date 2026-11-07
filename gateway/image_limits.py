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


_ABSENT = object()


def _revision(value: Any) -> str | None:
    if (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)):
        return value
    return None


def _validated_rows(rows: list) -> None:
    """One row owns one count identity; malformed or duplicate identities refuse."""
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or "reference_count" not in row:
            raise ValueError(
                "The producer's reference policy has a count row without a reference_count identity.")
        count = row["reference_count"]
        if count is None:
            identity = None
        elif type(count) is int and count >= 1:
            identity = count
        else:
            raise ValueError(
                "The producer's reference policy states a malformed reference_count "
                f"({count!r}); it must be null or a positive whole number.")
        if identity in seen:
            raise ValueError(
                "The producer's reference policy states duplicate rows for "
                + ("the generic fallback." if identity is None
                   else f"{identity} reference image(s)."))
        seen.add(identity)


def _snapshot_view(snapshot: Any, worker: dict, model_id: str, mode: str) -> dict:
    """The producer's r5 reference view for one node/model/mode, validated.

    A present ``hub_memory_admission`` is authoritative: an invalid or unavailable snapshot
    raises, so the caller refuses rather than falling back to the lane-contract table.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("The producer published an invalid memory-admission snapshot (not an object).")
    node = snapshot.get("node_id")
    if not isinstance(node, str):
        raise ValueError("The producer's memory-admission snapshot states a non-string node id.")
    node = node.strip().lower()
    if not node:
        raise ValueError("The producer's memory-admission snapshot names no node.")
    machine = worker.get("hub_machine")
    if machine is not None and not isinstance(machine, str):
        raise ValueError("This row's machine id is not a string.")
    machine = (machine or "").strip().lower()
    # `local` is the Hub's own alias for the row it produced; every other row must name
    # exactly the node it describes, so a snapshot is never borrowed across nodes.
    if machine != "local" and machine != node:
        raise ValueError(
            f"The memory-admission snapshot describes node {snapshot.get('node_id')!r}, "
            f"not this node {worker.get('hub_machine')!r}.")
    producer_model = snapshot.get("model")
    if not isinstance(producer_model, str):
        raise ValueError("The producer's memory-admission snapshot states a non-string model.")
    producer_model = producer_model.strip().lower()
    if not producer_model:
        raise ValueError("The producer's memory-admission snapshot names no model.")
    if not isinstance(model_id, str) or producer_model != model_id.strip().lower():
        raise ValueError(
            f"The memory-admission snapshot names model {snapshot.get('model')!r}, "
            f"not {model_id!r}.")
    reference = snapshot.get("reference")
    view = reference.get(mode) if isinstance(reference, dict) else None
    if not isinstance(view, dict):
        raise ValueError(f"The producer published no reference policy for mode {mode!r}.")
    if view.get("mode") != mode:
        raise ValueError(
            f"The reference policy mode {view.get('mode')!r} is not the selected mode {mode!r}.")
    if view.get("available") is not True:
        reason = view.get("reason")
        raise ValueError("The producer's reference policy is unavailable"
                         + (f": {reason}" if isinstance(reason, str) and reason else "."))
    tier = view.get("physical_tier")
    if not isinstance(tier, str) or not tier:
        raise ValueError("The producer's reference policy states no physical tier.")
    if tier != "<8" and not (tier.isascii() and tier.isdigit()
                              and int(tier) > 0 and str(int(tier)) == tier):
        raise ValueError("The producer's reference policy states an invalid physical tier.")
    tier_gb = view.get("physical_tier_gb")
    if tier_gb is not None and (type(tier_gb) is not int or str(tier_gb) != tier):
        raise ValueError(
            f"The reference policy tier {tier!r} disagrees with its band {tier_gb!r}.")
    total = view.get("total_memory_bytes")
    if total is not None and (isinstance(total, bool) or not isinstance(total, (int, float))
                              or not math.isfinite(total) or total <= 0):
        raise ValueError("The producer's reference policy states an invalid host memory total.")
    # The Hub chooses its physical tier; legacy table cells may omit bands and cannot rederive it.
    revision = _revision(view.get("revision"))
    if revision is None:
        raise ValueError("The producer's reference policy states no valid mode revision.")
    covers = view.get("revision_covers")
    if (not isinstance(covers, list) or not covers
            or not all(isinstance(item, str) and item for item in covers)):
        raise ValueError("The producer's reference policy states no revision scope.")
    # ponytail: the covers list is checked for shape and for RAM/reference equality, not
    # pinned to the Hub's prose list; pin it if a producer can disagree inside one mode.
    ram = snapshot.get("ram")
    ram_modes = ram.get("modes") if isinstance(ram, dict) else None
    ram_view = ram_modes.get(mode) if isinstance(ram_modes, dict) else None
    ram_model = ram_view.get("model") if isinstance(ram_view, dict) else None
    if (not isinstance(ram_view, dict)
            or not isinstance(ram_model, str)
            or ram_model.strip().lower() != producer_model
            or ram_view.get("mode") != mode
            or ram_view.get("revision") != revision
            or ram_view.get("revision_covers") != covers):
        raise ValueError(
            f"The reference policy revision and scope do not match the producer's {mode!r} "
            "RAM snapshot.")
    rows = view.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("The producer's reference policy states no count rows.")
    _validated_rows(rows)
    return view


def _snapshot_row(view: dict, count: int) -> dict:
    rows = view["rows"]
    exact = next((row for row in rows if row["reference_count"] == count), None)
    generic = next((row for row in rows if row["reference_count"] is None), None)
    row = exact if exact is not None else generic
    if row is None:
        raise ValueError(
            f"The producer's reference policy has no row for {count} reference image(s) "
            "and no generic fallback.")
    return row


def _snapshot_decision(snapshot: Any, worker: dict, model_id: str, mode: str,
                       count: int) -> None:
    """Refuse unless the producer's selected row allows the exact requested count."""
    view = _snapshot_view(snapshot, worker, model_id, mode)
    row = _snapshot_row(view, count if mode == "edit" else 0)
    effective = row.get("effective")
    if not isinstance(effective, dict):
        raise ValueError("The selected reference row states no effective policy.")
    allowed = effective.get("allowed")
    if allowed is None:
        raise ValueError(
            "The selected reference row does not state whether references are allowed (unknown).")
    if type(allowed) is not bool:
        raise ValueError("The selected reference row's allowed value is not a boolean.")
    if allowed is False:
        raise ValueError("The selected reference row is Off.")
    swap = effective.get("swap_allowance_gb")
    if swap is None:
        raise ValueError(
            "The selected reference row does not state a swap allowance (unknown).")
    if (isinstance(swap, bool) or not isinstance(swap, (int, float))
            or not math.isfinite(swap) or swap < 0):
        raise ValueError(
            "The selected reference row's swap allowance is not a finite number >= 0.")
    if mode == "edit":
        maximum = effective.get("max_reference_images")
        if maximum is None:
            raise ValueError(
                "The selected reference row does not state a reference-image cap (unknown).")
        if type(maximum) is not int or maximum < 0:
            raise ValueError(
                "The selected reference row's reference-image cap is not a whole number >= 0.")
        if maximum < count:
            raise ValueError(
                f"The selected reference row allows at most {maximum} reference image(s).")


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
        snapshot = worker["hub_memory_admission"] if "hub_memory_admission" in worker else _ABSENT
        if snapshot is not _ABSENT:
            known = True
        if target and worker.get("hub_machine") != target:
            continue
        if snapshot is not _ABSENT:
            try:
                _snapshot_decision(snapshot, worker, model_id, mode, count)
            except ValueError as error:
                reasons.append(str(error))
                continue
        else:
            try:
                value = table(worker)
            except ValueError as error:
                known = True
                reasons.append(str(error))
                continue
            if value is None:
                continue
            known = True
        if any(worker.get(k) is False for k in ("hub_cached", "memory_eligible", "runtime_ready", "available")):
            continue
        declared = next((m for m in worker.get("image_modes") or []
                         if isinstance(m, dict) and m.get("id") == mode), None)
        if declared is not None and declared.get("available") is not True:
            continue
        if snapshot is _ABSENT:
            try:
                cell = cell_for(value, model_id, mode, count, worker)
            except ValueError as error:
                known = True
                reasons.append(str(error))
                continue
            if not (cell["allowed"] and (mode == "txt2img" or 0 < count <= cell["max_reference_images"])):
                reasons.append(f"{cell['source']}: this cell is Off or below the requested reference count")
                continue
        eligible.add(str(worker.get("hub_machine") or f"unidentified:{index}"))
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
    for worker in raw.get("image_workers") or [raw]:
        if not isinstance(worker, dict):
            continue
        if "hub_memory_admission" in worker:
            try:
                view = _snapshot_view(worker["hub_memory_admission"], worker, model_id, "edit")
            except ValueError:
                continue
            for row in view["rows"]:
                if not isinstance(row, dict):
                    continue
                count = row.get("reference_count")
                if type(count) is int and count >= 1:
                    boundaries.update((count, count + 1))
                elif count is None:
                    effective = row.get("effective")
                    maximum = effective.get("max_reference_images") if isinstance(effective, dict) else None
                    if type(maximum) is int and maximum >= 0:
                        boundaries.add(maximum + 1)
            continue
        try:
            value = table(worker)
            for row in value["rows"] if value else []:
                if not isinstance(row, dict) or row.get("model_id") != model_id or row.get("mode") != "edit":
                    continue
                count = row.get("reference_count")
                if type(count) is int and count >= 1:
                    boundaries.update((count, count + 1))
                for cell in (row.get("tiers") or {}).values():
                    maximum = validate_cell(cell, provenance=True)["max_reference_images"]
                    if count is None:
                        boundaries.add(maximum + 1)
        except (ValueError, AttributeError, TypeError):
            continue
    cap = 0
    for count in sorted(n for n in boundaries if n > 0):
        decision = assess(raw, model_id, {"_reference_digests": range(count)})
        if not decision or not decision["allowed"]:
            cap = count - 1
            break
        cap = count
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
        if not fleet._location_takes_customer_jobs(location) or fleet.is_draining_provider(definition.id):
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
