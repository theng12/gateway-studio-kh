"""An explicit owner image measurement target is a restriction, never a preference."""
from __future__ import annotations

import json
import math
import re
import time

# ponytail: identifiers fit one existing Hub routing segment (200 characters);
# follow a published Hub identifier bound if that protocol grows.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")


def read(params: dict) -> dict | None:
    if "measurement_target" not in params:
        return None
    target = params["measurement_target"]
    if (type(target) is not dict or set(target) != {"location_id", "machine"}
            or any(type(value) is not str or not _SEGMENT.fullmatch(value)
                   for value in target.values())):
        raise ValueError("measurement_target must name exactly a registered location_id and machine.")
    return target


def same_request(job: dict, params: dict) -> bool:
    try:
        stored = job["params"]
        if isinstance(stored, str):
            stored = json.loads(stored)
        return isinstance(stored, dict) and read(stored) == read(params)
    except (TypeError, ValueError):
        return False


def matches_provider(target: dict, provider_id: str) -> bool:
    from . import fleet
    return fleet.controller_id_from(provider_id) == target["location_id"]


def unavailable(target: dict) -> str | None:
    from . import fleet
    snapshot = fleet.snapshot(allow_stale=True)
    now = time.time()

    def fresh(value):
        return (type(value) in (int, float) and math.isfinite(value)
                and 0 <= now - value <= fleet._CACHE_TTL)

    if not fresh(snapshot.get("generated_at")):
        return f"Measurement inventory needs a Fleet read at most {fleet._CACHE_TTL:g} seconds old."
    locations = [row for row in snapshot.get("locations", [])
                 if row.get("id") == target["location_id"]]
    machines = [row for row in snapshot.get("machines", [])
                if (row.get("location_id"), row.get("machine"))
                == (target["location_id"], target["machine"])]
    if len(locations) != 1 or len(machines) != 1:
        return "The exact measurement Mac is not uniquely present in the registered fleet."
    location, machine = locations[0], machines[0]
    if (location.get("reachable") is not True or machine.get("reachable") is not True
            or location.get("enabled") is False):
        return "The measurement Mac or its location is unavailable."
    if (fleet.owner_paused_at(target["location_id"]) or location.get("paused_at")
            or location.get("paused") or machine.get("node_paused") is not False):
        return "The measurement Mac is paused or its pause state is unknown."
    if (not fresh((location.get("activity") or {}).get("observed_at"))
            or machine.get("state") not in ("ready", "long_idle", "just_finished")
            or machine.get("current") is not None or not fleet._takes_customer_jobs(location)
            or (machine.get("function_enabled") or {}).get("image") is False):
        return "The measurement Mac is not confirmed idle and ready in fresh Fleet activity."
    return None
