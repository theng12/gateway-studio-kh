"""Price and rate-limit evidence researched from providers' own pages, imported by the owner.

A research session (outside the gateway) reads each provider's official pricing and rate-limit
pages and writes one JSON file (`_orchestra/audit-20260930/CONTRACT-price-evidence.md`). The gateway
never fetches those pages and never calls a model: it only **validates and stores** that file.

**A number is published only with both a source URL on the provider's own domain and a verbatim
quote that contains that number.** Anything else is dropped with a reason, per number. Accepted
prices become `price_source: "provider_page"` (distinct from `"provider"`, which is the provider's
own API stating the price); rate limits are informational tables and are never applied to
concurrency settings — they are "up to" ceilings the owner sets.

ponytail: the check that a quote "contains the number" is textual (every number token in the quote,
commas removed, `K`/`M` suffix expanded). Ceiling — the gateway cannot prove the quote is really on
the page, or that the number in it is the one meant (a version like `2.5` also reads as a number).
The URL and quote ride along with the price so the owner can check them; the gateway only refuses
what is unverifiable on its face.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

SCHEMA = "gateway-kh.price-evidence.v1"


def evidence_path() -> Path:
    # Imported lazily: `python3 -m gateway.price_evidence check` runs in a research session with
    # only the stdlib and must not touch (or create) the gateway's data directory.
    from .config import DATA_DIR

    return DATA_DIR / "price-evidence.json"

#: The one table of who may vouch for whom: a source URL's host must equal one of these or be a
#: subdomain of it. The first block is the contract's list; the rest are providers the research
#: may also reach, each on its own base_url / docs host.
DOMAINS: dict[str, tuple[str, ...]] = {
    "anthropic-direct": ("anthropic.com", "claude.com"),
    "deepseek-main": ("deepseek.com",),
    "elevenlabs-direct": ("elevenlabs.io",),
    "experiential-labs": ("experientiallabs.ai",),
    "fish-audio": ("fish.audio",),
    "google-ai": ("ai.google.dev", "cloud.google.com", "google.com"),
    "google-ai-free": ("ai.google.dev", "cloud.google.com", "google.com"),
    "kie-ai-images": ("kie.ai",),
    "nvidia-nim-main": ("nvidia.com",),
    "openai-direct": ("openai.com",),
    "fal-ai": ("fal.ai",),
    "xai-direct": ("x.ai",),
    "bfl-direct": ("bfl.ai",),
    "minimax-direct": ("minimax.io", "minimaxi.com"),
    "ollama-cloud": ("ollama.com",),
    "openrouter-main": ("openrouter.ai",),
    "groq-main": ("groq.com",),
    "tokenharbor": ("tokenharbor.ai",),
}

#: Unit keys per modality (CONTRACT-pricing.md section 2). `other` is stored as evidence only.
UNITS: dict[str, frozenset[str]] = {
    "chat": frozenset({"input_per_1m_tokens", "output_per_1m_tokens", "cached_input_per_1m_tokens"}),
    "image": frozenset({"per_image"}),
    "video": frozenset({"per_video", "per_second"}),
    "tts": frozenset({"per_1k_chars"}),
    "transcription": frozenset({"per_second"}),
    "music": frozenset({"per_request"}),
}
_LIMIT_FIELDS = ("rpm", "tpm", "rpd", "concurrency", "images_per_min")
_STATUSES = {"found", "partial", "not_found"}
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?|\.\d+)([KkMm]\b)?")
_LOCK = threading.Lock()


# -- validation -------------------------------------------------------------
def host_ok(url: Any, provider_id: str) -> bool:
    """`https` URL whose host is on this provider's allow-list (exact or a subdomain)."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return any(host == d or host.endswith("." + d) for d in DOMAINS.get(provider_id, ()))


def quote_has(quote: Any, number: Any) -> bool:
    """Does the verbatim quote contain this number as written (`$0.04`, `0.040`, `30K` = 30000)?"""
    if not isinstance(quote, str) or not re.search(r"[A-Za-z]", quote):
        return False
    try:
        wanted = Decimal(str(number))
    except InvalidOperation:
        return False
    for digits, suffix in _NUMBER.findall(quote):
        try:
            value = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            continue
        if suffix:
            value *= 1_000 if suffix in "Kk" else 1_000_000
        if value == wanted:
            return True
    return False


def _finite(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() and number >= 0 else None


def _iso(value: Any) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _check_price(provider_id: str, item: Any, models: dict[str, str] | None) -> tuple[dict | None, str | None]:
    """`(accepted entry, None)` or `(None, reason)`."""
    if not isinstance(item, dict):
        return None, "not an object"
    model_id, modality, unit = _text(item.get("model_id")), item.get("modality"), item.get("unit")
    if not model_id:
        return None, "model_id is missing"
    if modality not in UNITS:
        return None, f"modality '{modality}' is not one of {sorted(UNITS)}"
    if models is not None:  # the CLI has no model list; the import route always does
        if model_id not in models:
            return None, "model_id is not a model of this provider (refresh its models first)"
        if models[model_id] != modality:
            return None, f"modality '{modality}' does not match the model's ({models[model_id]})"
    if not host_ok(item.get("source_url"), provider_id):
        return None, "source_url is not https on the provider's official domain"
    quote = item.get("quote")
    base = {"model_id": model_id, "modality": modality, "variant": _text(item.get("variant")),
            "source_url": item["source_url"].strip(), "quote": quote,
            "page_date": _iso(item["page_date"]) if item.get("page_date") else None}
    if unit == "other":
        if not _text(item.get("unit_text")) or not isinstance(quote, str) or not quote.strip():
            return None, "unit 'other' needs unit_text and a quote"
        return {**base, "unit": "other", "unit_text": item["unit_text"].strip(), "usd": None}, None
    if unit not in UNITS[modality]:
        return None, f"unit '{unit}' is not a {modality} unit"
    usd = _finite(item.get("usd"))
    if usd is None:
        return None, "usd is not a non-negative number"
    if not quote_has(quote, usd):
        return None, "quote does not contain the number"
    return {**base, "unit": unit, "usd": float(usd)}, None


def _check_limit(provider_id: str, item: Any) -> tuple[dict | None, list[str]]:
    """The tier with only its verified numbers, plus a reason per number that was dropped."""
    if not isinstance(item, dict) or not _text(item.get("tier")):
        return None, ["tier has no name"]
    if not host_ok(item.get("source_url"), provider_id):
        return None, ["source_url is not https on the provider's official domain"]
    kept: dict[str, int] = {}
    reasons: list[str] = []
    for field in _LIMIT_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        number = _finite(value)
        if number is None or number != number.to_integral_value():
            reasons.append(f"{field} is not a whole non-negative number")
        elif not quote_has(item.get("quote"), number):
            reasons.append(f"{field}: quote does not contain {value}")
        else:
            kept[field] = int(number)
    if not kept:
        return None, reasons or ["no numbers"]
    return {"tier": item["tier"].strip(), "how_reached": _text(item.get("how_reached")),
            "scope": _text(item.get("scope")), **kept, "source_url": item["source_url"].strip(),
            "quote": item["quote"]}, reasons


def validate(doc: Any, models_of: Callable[[str], dict[str, str]] | None = None) -> dict[str, Any]:
    """Split an evidence file into accepted entries and dropped ones, per provider.

    `models_of(provider_id)` -> `{model_id: modality}` of the gateway's rows for that provider;
    `None` (the CLI) skips only the "is this one of our models" check.
    Raises `ValueError` for a file that is not this schema at all (nothing is imported then).
    """
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"not a price evidence file (expected schema '{SCHEMA}')")
    researched_at = _iso(doc.get("researched_at"))
    if researched_at is None or not isinstance(doc.get("providers"), list):
        raise ValueError("researched_at (ISO date) and providers[] are required")
    out: dict[str, Any] = {}
    for entry in doc["providers"]:
        pid = entry.get("provider_id") if isinstance(entry, dict) else None
        if not isinstance(pid, str):
            continue
        result = {"provider_id": pid, "researched_at": researched_at,
                  "status": entry.get("status") if entry.get("status") in _STATUSES else "partial",
                  "notes": _text(entry.get("notes")), "prices": [], "other": [], "rate_limits": [],
                  "dropped": []}
        out[pid] = result
        if pid not in DOMAINS:
            result["dropped"].append({"kind": "provider", "reason": "no official-domain allow-list for this provider"})
            continue
        models = models_of(pid) if models_of else None
        for item in entry.get("prices") or []:
            accepted, reason = _check_price(pid, item, models)
            if accepted is None:
                named = item if isinstance(item, dict) else {}
                result["dropped"].append({"kind": "price", "model_id": named.get("model_id"),
                                          "unit": named.get("unit"), "reason": reason})
            else:
                result["other" if accepted["unit"] == "other" else "prices"].append(accepted)
        for item in entry.get("rate_limits") or []:
            accepted, reasons = _check_limit(pid, item)
            if accepted is not None:
                result["rate_limits"].append(accepted)
            for reason in reasons:
                tier = item.get("tier") if isinstance(item, dict) else None
                result["dropped"].append({"kind": "rate_limit", "tier": tier, "reason": reason})
    return out


# -- storage ----------------------------------------------------------------
def _read() -> dict[str, Any]:
    try:
        data = json.loads(evidence_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("providers") if isinstance(data, dict) and isinstance(data.get("providers"), dict) else {}


def import_evidence(doc: Any, models_of: Callable[[str], dict[str, str]] | None = None) -> dict[str, Any]:
    """Validate and store. Each provider in the file replaces its previous evidence wholesale."""
    checked = validate(doc, models_of)
    with _LOCK:
        stored = _read()
        stored.update(checked)
        tmp = evidence_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": SCHEMA, "providers": stored}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, evidence_path())
    return summary(checked)


def summary(providers: dict[str, Any]) -> dict[str, Any]:
    rows = [{"provider_id": pid, "status": p["status"], "researched_at": p["researched_at"],
             "notes": p["notes"], "accepted_prices": len(p["prices"]), "kept_other_units": len(p["other"]),
             "accepted_rate_limits": len(p["rate_limits"]), "dropped": p["dropped"]}
            for pid, p in providers.items()]
    return {"providers": rows,
            "accepted": sum(r["accepted_prices"] + r["accepted_rate_limits"] for r in rows),
            "dropped": sum(len(r["dropped"]) for r in rows)}


def current() -> dict[str, Any]:
    """What is stored: the summary plus the informational rate-limit tables per provider."""
    stored = _read()
    return {**summary(stored), "rate_limits": {pid: p.get("rate_limits") or [] for pid, p in stored.items()}}


# -- publication ------------------------------------------------------------
_CACHE: tuple[int, dict[tuple[str, str, str], list[dict[str, Any]]], dict[str, str]] = (-1, {}, {})


def _index() -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], dict[str, str]]:
    global _CACHE
    try:
        stamp = evidence_path().stat().st_mtime_ns
    except OSError:
        stamp = -1
    if stamp != _CACHE[0]:
        prices: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        dates: dict[str, str] = {}
        for pid, p in _read().items():
            dates[pid] = p.get("researched_at")
            for item in p.get("prices") or []:
                prices.setdefault((pid, item["modality"], item["model_id"]), []).append(item)
        _CACHE = (stamp, prices, dates)
    return _CACHE[1], _CACHE[2]


def page_price(provider_id: str, modality: str, model_id: str) -> dict[str, Any] | None:
    """The `provider_page` price fields for one row, or `None` when nothing was researched.

    A unit is published only when the page gave one number for it (or every variant gave the same);
    several different numbers (sizes, qualities, tiers) are never collapsed into one — they ride in
    `price_variants` and the unit is left out of `price_usd`. No resolved unit -> `None`.
    """
    prices, dates = _index()
    entries = prices.get((provider_id, modality, model_id))
    if not entries:
        return None
    resolved: dict[str, float] = {}
    url = None
    for unit in dict.fromkeys(e["unit"] for e in entries):
        same = [e for e in entries if e["unit"] == unit]
        if len({e["usd"] for e in same}) == 1:
            resolved[unit] = same[0]["usd"]
            url = url or same[0]["source_url"]
    if not resolved:
        return None
    return {"price_source": "provider_page", "price_usd": resolved, "price_url": url,
            "price_as_of": dates.get(provider_id),
            "price_variants": [{"unit": e["unit"], "usd": e["usd"], "variant": e["variant"],
                                "source_url": e["source_url"]} for e in entries]}


def main(argv: list[str]) -> int:
    """`python3 -m gateway.price_evidence check <file.json>`: the import's validation, printed."""
    if len(argv) != 2 or argv[0] != "check":
        print("usage: python3 -m gateway.price_evidence check <file.json>", file=sys.stderr)
        return 2
    try:
        checked = validate(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 1
    dropped = 0
    for pid, p in checked.items():
        print(f"{pid}: {p['status']}, {len(p['prices'])} prices, {len(p['other'])} other-unit, "
              f"{len(p['rate_limits'])} rate-limit tiers accepted")
        for item in p["prices"]:
            print(f"  ok    price {item['model_id']} {item['unit']} = {item['usd']}")
        for item in p["rate_limits"]:
            print(f"  ok    limit {item['tier']}")
        for item in p["dropped"]:
            dropped += 1
            what = " ".join(str(item.get(k)) for k in ("model_id", "unit", "tier") if item.get(k))
            print(f"  DROP  {item['kind']} {what}: {item['reason']}")
    print(f"{dropped} dropped")
    return 1 if dropped else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
