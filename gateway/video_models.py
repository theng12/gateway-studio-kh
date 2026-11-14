"""The live video catalogue, with the evidence Story's preflight needs.

**Read-only and never cached past the live reads.** Every row is rebuilt from
`catalog.rows("video")` — the same rows the model picker publishes — plus one
warm fleet read. Nothing here generates, persists or invents a value: an option
nobody states is `null` and named in `not_stated`, never `0` or a plausible guess.

Each option carries **its own source beside it**, in the convention the catalogue
already uses for `modes` (`modes` + `modes_source`):

    declared   the provider's own schema, docs or a lane's contract states it
    observed   read off the catalogue's published variants, not a typed record
    default    this gateway's fallback (`modes_source` == ``gateway``)

`aspect_ratios` and `resolutions` are the catalogue's existing keys and are
published unchanged (Story and the e2e contract read them as plain lists); the
source travels beside them as `<field>_source`. A sourced wrapper object for
those two names would have been a second spelling of one fact.

**Eligibility is the fleet's answer, not a second opinion.** A model is
`available` only when `fleet.dispatch_eligibility` says a reachable location can
run it now; otherwise the row carries the dispatch contract's own rejection
reason and rows. A cloud model is not dispatched by the fleet at all, so its own
catalogue lock is the answer there — labelled as such rather than reported as
"no machine", which would be untrue.

**`machines`** is a count, never a list: `{serving} of {publishers}` locations.
A location's address is exactly the fact Story Studio is never handed, so no
location ever becomes a row here.
"""

from __future__ import annotations

import logging
from typing import Any

from . import fleet
from .catalog import catalog, decode_model_id

log = logging.getLogger("gateway.video_models")

NOT_STATED = "not stated"

#: The catalogue's `modes` words -> the t2v/i2v segments Story plans in.
_KINDS: dict[str, str] = {
    "text-to-video": "t2v",
    "image-to-video": "i2v",
    # A reference-to-video endpoint is fed images, so it is our image-guided kind.
    "reference-to-video": "i2v",
}

#: The catalogue's own provenance words -> this endpoint's three.
_SOURCE_WORDS: dict[str, str] = {
    "provider": "declared",
    "curated": "declared",
    "gateway": "default",
}

PRICE_NOT_STATED = "not stated"


# ---------------------------------------------------------------------------
# Option helpers
# ---------------------------------------------------------------------------

def _source_word(raw: Any) -> str | None:
    """`declared` / `default` for one of the catalogue's provenance words, else `None`."""
    return _SOURCE_WORDS.get(str(raw or "").strip().lower())


def _kinds(row: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    kinds: list[str] = []
    for mode in row.get("modes") or []:
        kind = _KINDS.get(str(mode))
        if kind is not None and kind not in kinds:
            kinds.append(kind)
    if not kinds:
        return None, None
    return kinds, _source_word(row.get("modes_source"))


def _durations(row: dict[str, Any]) -> tuple[dict[str, int] | None, str | None]:
    """Every whole second the row states, as `{min, max, step}`; or null."""
    record = row.get("duration_seconds")
    if isinstance(record, dict):
        low, high = record.get("min"), record.get("max")
        if isinstance(low, int) and isinstance(high, int) and high >= low >= 0:
            step = record.get("step")
            step = step if isinstance(step, int) and step > 0 else 1
            source = _source_word(row.get("capability_source") or row.get("modes_source"))
            return {"min": low, "max": high, "step": step}, source
    seconds = sorted({
        int(variant["duration_seconds"])
        for variant in row.get("variants") or []
        if isinstance(variant.get("duration_seconds"), int)
        and variant["duration_seconds"] > 0
    })
    if seconds:
        step = seconds[1] - seconds[0] if len(seconds) > 1 else 1
        source = _source_word(row.get("modes_source")) or "observed"
        return {"min": seconds[0], "max": seconds[-1], "step": step}, source
    return None, None


def _from_variants(row: dict[str, Any], key: str) -> tuple[list[str] | None, str | None]:
    values: list[str] = []
    for variant in row.get("variants") or []:
        value = variant.get(key)
        if isinstance(value, str) and value.strip() and value not in values:
            values.append(value)
    if not values:
        return None, None
    source = _source_word(row.get("capability_source") or row.get("modes_source")) or "observed"
    return values, source


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------

def price_evidence(row: dict[str, Any]) -> dict[str, Any]:
    """What this model costs, from the row the Providers page already shows.

    The fields are the catalogue's own (`pricing.row_price`, which is what the
    Providers page calls) — nothing here re-reads the provider or the research
    file. An unpublished price is `null` with `"not stated"`, because "nobody
    told us" must never render as `0`.
    """
    source = row.get("price_source") or "unpublished"
    usd = row.get("price_usd")
    as_of = row.get("price_as_of")
    if not isinstance(usd, dict) or not usd:
        evidence: dict[str, Any] = {
            "unit": None, "amount": None, "currency": None, "free": None,
            "source": source, "observed_at": as_of,
        }
        if source == "local":
            # A local lane with no unit key still costs nothing; the unit is the
            # catalogue's own video unit rather than an invented one.
            evidence.update({"unit": "per_video", "amount": 0, "currency": "USD",
                             "free": True, "label": "Free (your Macs)"})
            return evidence
        evidence["note"] = PRICE_NOT_STATED
        return evidence
    unit = row.get("price_basis") or next(iter(usd))
    amount = usd.get(unit)
    free = row.get("chargeable") is False
    evidence = {
        "unit": unit,
        "amount": amount,
        "currency": "USD" if amount is not None else None,
        "free": free if amount is not None else None,
        "source": source,
        "observed_at": as_of,
    }
    if source == "local":
        evidence["label"] = "Free (your Macs)"
    elif free:
        evidence["label"] = "Free"
    if amount is None:
        # Researched, but the page gave several different numbers for one unit:
        # the variants are not collapsed, and no single amount is invented here.
        evidence["note"] = PRICE_NOT_STATED
    return evidence


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _rejection(raw: dict[str, Any]) -> dict[str, Any]:
    """One rejection row, kept inside the dispatch contract's fixed vocabulary."""
    reason = str(raw.get("reason") or "unknown")
    if reason not in fleet.REJECTION_REASONS:
        reason = "unknown"
    row = {
        "location_id": raw.get("location_id"),
        "provider_id": raw.get("provider_id"),
        "label": raw.get("label"),
        "machine": raw.get("machine"),
        "reason": reason,
        "detail": raw.get("detail"),
    }
    if raw.get("last_seen_at") is not None:
        row["last_seen_at"] = raw["last_seen_at"]
    return row


def _machines(eligibility: dict[str, Any]) -> dict[str, Any]:
    """`{serving} of {publishers}` — locations, counted, never listed."""
    serving = {str(c.get("location_id")) for c in eligibility.get("candidates") or []}
    publishing = set(serving)
    for rejection in eligibility.get("rejections") or []:
        if rejection.get("reason") == "model_not_published":
            continue
        publishing.add(str(rejection.get("location_id")))
    return {
        "serving": len(serving),
        "publishers": len(publishing),
        "label": f"{len(serving)} of {len(publishing)}",
    }


def _is_fleet_row(provider_id: str) -> bool:
    return provider_id == fleet.FLEET_PROVIDER_ID or fleet.is_location_provider(provider_id)


def _eligibility(model_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Whether a reachable location can run this model right now, and why not."""
    try:
        eligibility = fleet.dispatch_eligibility(model_id, "video")
    except Exception as exc:  # noqa: BLE001 - an unread fleet is unknown, never "available"
        log.warning("video eligibility read failed for %s: %s", model_id, exc)
        return {
            "available": False,
            "reason": "unknown",
            "reason_detail": f"the fleet could not be read: {exc}",
            "rejections": [],
            "machines": {"serving": 0, "publishers": 0, "label": "0 of 0"},
        }
    rejections = [_rejection(r) for r in eligibility.get("rejections") or []]
    available = bool(eligibility.get("available"))
    if available:
        return {"available": True, "reason": None, "reason_detail": None,
                "rejections": rejections, "machines": _machines(eligibility)}
    if rejections:
        first = rejections[0]
        reason, detail = first["reason"], first.get("detail")
    else:
        reason, detail = "unknown", "no location publishes this video model"
    return {"available": False, "reason": reason, "reason_detail": detail,
            "rejections": rejections, "machines": _machines(eligibility)}


def _cloud_availability(row: dict[str, Any]) -> dict[str, Any]:
    """A cloud model is not dispatched by the fleet; its own catalogue lock answers."""
    locked = bool(row.get("locked"))
    return {
        "available": (row.get("available") is not False) and not locked,
        "reason": "unknown" if locked else None,
        "reason_detail": (str(row.get("lock_reason") or "") or None) if locked else None,
        "rejections": [],
        "machines": {"serving": 0, "publishers": 0, "label": "not on the fleet"},
    }


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def row(catalogue_row: dict[str, Any]) -> dict[str, Any]:
    """One video model: the catalogue row plus its preflight evidence."""
    provider_id, model_id = decode_model_id(str(catalogue_row.get("id") or ""))
    out = dict(catalogue_row)
    out["provider"] = catalogue_row.get("provider_name")

    kinds, kinds_source = _kinds(out)
    out["kinds"], out["kinds_source"] = kinds, kinds_source
    durations, durations_source = _durations(out)
    out["durations"], out["durations_source"] = durations, durations_source

    # `aspect_ratios` / `resolutions` keep their existing shape; the source rides beside.
    for field, variant_key in (("aspect_ratios", "aspect_ratio"), ("resolutions", "resolution")):
        values = out.get(field)
        if not isinstance(values, list) or not values:
            derived, derived_source = _from_variants(out, variant_key)
            if derived is not None:
                out[field] = derived
                out[f"{field}_source"] = derived_source
            else:
                out[field] = [] if isinstance(values, list) else None
                out[f"{field}_source"] = None
        else:
            out[f"{field}_source"] = _source_word(
                out.get("capability_source") or out.get("modes_source"))

    out["price_evidence"] = price_evidence(out)

    if _is_fleet_row(provider_id):
        out.update(_eligibility(model_id, out))
    else:
        out.update(_cloud_availability(out))

    out["not_stated"] = [
        name for name, value in (
            ("kinds", out.get("kinds")),
            ("durations", out.get("durations")),
            ("resolutions", out.get("resolutions")),
            ("aspect_ratios", out.get("aspect_ratios")),
        )
        if not value
    ]
    return out


def rows() -> list[dict[str, Any]]:
    """Every video row the live catalogue publishes, enriched with its evidence."""
    return [row(catalogue_row) for catalogue_row in catalog.rows("video")]


def envelope() -> dict[str, Any]:
    """`{"models": …, "freshness": …}`, the shape `/v1/videos/models` has always returned."""
    return {"models": rows(), "freshness": catalog.freshness()}
