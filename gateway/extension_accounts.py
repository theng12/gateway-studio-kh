"""Authenticated browser account observations; neither a quote nor a profile is a balance."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def timestamp(value: Any, *, now: float, future: bool = False) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("observation times must be timezone-qualified ISO timestamps")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError("invalid observation timestamp") from None
    if stamp.tzinfo is None or not math.isfinite(result) or result <= 0 or (not future and result > now):
        raise ValueError("observation time must be positive, timezone-qualified and not in the future")
    return result


def slug(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError(f"{name} must be an opaque source/unit/provider slug")
    return value


def credits(value: Any, *, now: float) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("credits must be an observed amount or null")
    amount = value.get("value")
    if type(amount) not in (int, float) or not 0 <= amount <= 2**53 - 1 or not math.isfinite(amount):
        raise ValueError("credit amount must be a finite nonnegative safe number")
    return {"value": amount, "unit": slug(value.get("unit"), "unit"),
            "source": slug(value.get("source"), "source"),
            "observed_at": timestamp(value.get("observedAt"), now=now)}


def account(value: Any, *, now: float) -> dict:
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("account must use schema 1")
    identity = value.get("accountId")
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError("accountId must be an opaque stable identity, never an email or credential")
    result = {"provider": slug(value.get("provider"), "provider"), "account_id": identity,
              "source": slug(value.get("source"), "source"),
              "observed_at": timestamp(value.get("observedAt"), now=now)}
    result.update(worker_state(value, now=now))
    result["balance"] = credits(value.get("balance"), now=now)
    for wire, key in (("resetAt", "reset_at"),):
        result[key] = timestamp(value[wire], now=now, future=True) if value.get(wire) is not None else None
    plan = value.get("plan")
    if plan is not None and (not isinstance(plan, str) or not plan.strip() or len(plan) > 120):
        raise ValueError("plan must be an observed label or null")
    result["plan"] = plan
    buckets = value.get("creditBuckets")
    if buckets is not None:
        if not isinstance(buckets, list) or len(buckets) > 32:
            raise ValueError("creditBuckets must be observed buckets or null (at most 32)")
        clean = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError("each credit bucket must be an observed object")
            item = credits(bucket, now=now)
            item["id"] = slug(bucket.get("id"), "bucket id")
            item["reset_at"] = timestamp(bucket["resetAt"], now=now, future=True) if bucket.get("resetAt") is not None else None
            clean.append(item)
        if len({item["id"] for item in clean}) != len(clean):
            raise ValueError("credit bucket ids must be distinct")
        result["credit_buckets"] = clean
    else:
        result["credit_buckets"] = None
    return result


def pacing(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"minSeconds", "maxSeconds"}:
        raise ValueError("pacing needs minSeconds and maxSeconds")
    lo, hi = value["minSeconds"], value["maxSeconds"]
    if any(type(n) not in (int, float) or not 0 <= n <= 2**53 - 1 or not math.isfinite(n) for n in (lo, hi)) or hi < lo:
        raise ValueError("pacing must be finite nonnegative seconds with max >= min")
    return {"minSeconds": lo, "maxSeconds": hi}


def worker_state(value: Any, *, now: float) -> dict:
    if not isinstance(value, dict):
        raise ValueError("state must be an observed object")
    result = {"observed_at": timestamp(value.get("observedAt"), now=now),
              "source": slug(value.get("source"), "state source")}
    for key, wire, allowed in (
        ("connection", "connection", ("connected", "disconnected", "offline", "error", "unknown")),
        ("sign_in", "signIn", ("signed_in", "signed_out", "unknown")),
        ("readiness", "readiness", ("ready", "not_ready", "unknown")),
        ("activity", "activity", ("idle", "generating", "cooling", "resting", "waiting", "recovering", "unknown")),
    ):
        selected = value.get(wire, "unknown")
        if not isinstance(selected, str) or selected not in allowed:
            raise ValueError(f"{wire} is not a supported state")
        result[key] = selected
    for key in ("enabled", "paused", "attention"):
        selected = value.get(key, False if key != "paused" else True)
        if type(selected) is not bool:
            raise ValueError(f"{key} must be an explicit boolean")
        result[key] = selected
    result["next_eligible_at"] = timestamp(value["nextEligibleAt"], now=now, future=True) if value.get("nextEligibleAt") is not None else None
    revision = value.get("controlRevision")
    if revision is not None and (not isinstance(revision, str) or not _ID.fullmatch(revision)):
        raise ValueError("controlRevision must echo the Gateway control revision or be null")
    result["control_revision"] = revision
    result["pacing"] = pacing(value["pacing"]) if value.get("pacing") is not None else None
    return result


def quote(value: Any, *, now: float) -> dict | None:
    if value is None:
        return None
    result = credits(value, now=now)
    digest = value.get("payloadSha256")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("a quote must name the exact pending payloadSha256")
    result["payload_sha256"] = digest
    result["provider"] = slug(value.get("provider"), "quote provider")
    identity = value.get("accountId")
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise ValueError("a quote must name its observed accountId")
    result["account_id"] = identity
    return result


def payload_sha256(payload: dict) -> str:
    """Hash exact server-built JSON including jobId; no selected controls can be dropped."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
