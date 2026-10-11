"""What a model row says it costs, and where that statement came from.

Beside (never instead of) `constants.zero_pricing`, which keeps Story Studio's parser from dropping
rows. Those credit fields are zero-valued *shapes*; this module publishes the truth next to them:

    price_source  "provider" | "provider_page" | "local" | "unpublished"
    price_usd     {unit: number} | null      a number exists only when a source states it
    price_as_of   ISO-8601 | null            when the provider last stated it (source == "provider"),
                                             or the research date (source == "provider_page", which
                                             also carries price_url and price_variants)

**Nothing here is computed or guessed.** A number is published only when the provider's own
response carried it (read from the discovery `raw` row, or fal's pricing endpoint stored there),
and every reader below names the field it trusts and the unit that field is documented in.
Anything else is `unpublished` with `price_usd: null` — "nobody told us", never zero.
See `_orchestra/audit-20260930/CONTRACT-pricing.md`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from . import price_evidence
from .providers.definitions import KIND_STUDIO_HUB

#: The owner's own Macs are free: price 0, source `local`. The unit keys per modality mirror
#: `zero_pricing`'s shapes so the two never disagree about what a row is priced in.
_LOCAL_UNITS = {
    "chat": ("input_per_1m_tokens", "output_per_1m_tokens"),
    "image": ("per_image",),
    "video": ("per_video",),
    "tts": ("per_1k_chars",),
    "transcription": ("per_second",),
    "music": ("per_request",),
    "embeddings": ("input_per_1m_tokens",),
    "vision": ("per_request",),
}

#: fal's `unit` word (its pricing endpoint) -> our unit key. A unit not listed here is
#: unpublished rather than guessed at.
FAL_UNITS = {"image": "per_image", "video": "per_video", "second": "per_second",
             "seconds": "per_second"}


def _number(value: Any) -> Decimal | None:
    """A finite, non-negative number from a JSON number or numeric string; else `None`.

    Negative is a sentinel, not a price (OpenRouter states `-1` for a router whose price depends
    on the model it picks), so it is unpublished. Booleans are not numbers.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _out(value: Decimal) -> float:
    return float(value.normalize())


def _read_provider(modality: str, raw: dict[str, Any]) -> dict[str, float] | None:
    """The price the provider stated in this model's discovery row, in USD; `None` if none."""
    pricing = raw.get("pricing")
    if modality == "chat" and isinstance(pricing, dict):
        # OpenRouter (and Groq, which lists the same object): USD **per token**, as strings.
        # docs: "All pricing values are in USD per token/request/unit."
        inp, out = _number(pricing.get("prompt")), _number(pricing.get("completion"))
        if inp is not None and out is not None:
            price = {"input_per_1m_tokens": _out(inp * 1_000_000),
                     "output_per_1m_tokens": _out(out * 1_000_000)}
            cached = _number(pricing.get("input_cache_read"))
            if cached is not None:
                price["cached_input_per_1m_tokens"] = _out(cached * 1_000_000)
            return price
        # Token Harbor: the key names its own unit.
        inp, out = _number(pricing.get("input_usd_per_1m")), _number(pricing.get("output_usd_per_1m"))
        if inp is not None and out is not None:
            return {"input_per_1m_tokens": _out(inp), "output_per_1m_tokens": _out(out)}
    if modality == "chat":
        # xAI: integers in "USD cents per 100 million tokens" (docs.x.ai, list language models),
        # which is value / 10_000 USD per 1M tokens.
        inp, out = _number(raw.get("prompt_text_token_price")), _number(
            raw.get("completion_text_token_price"))
        if inp is not None and out is not None:
            price = {"input_per_1m_tokens": _out(inp / 10_000),
                     "output_per_1m_tokens": _out(out / 10_000)}
            cached = _number(raw.get("cached_prompt_text_token_price"))
            if cached is not None:
                price["cached_input_per_1m_tokens"] = _out(cached / 10_000)
            return price
    if isinstance(pricing, dict) and modality in {"image", "video", "tts"}:
        # fal: `GET /v1/models/pricing` -> {unit_price, unit, currency}, stored by discovery.
        unit, amount = FAL_UNITS.get(str(pricing.get("unit") or "").strip().lower()), _number(
            pricing.get("unit_price"))
        if unit and amount is not None and str(pricing.get("currency") or "").upper() == "USD":
            return {unit: _out(amount)}
    return None


def row_price(kind: str, modality: str, raw: dict[str, Any] | None,
              fetched_at: float | None, provider_id: str = "", model_id: str = "") -> dict[str, Any]:
    """The price-truth fields for one catalogue row.

    Order of trust: the owner's own Macs (`local`), then the provider's own API response
    (`provider`), then a number the owner imported from the provider's pricing page with its URL and
    quote (`provider_page`, `price_evidence`), else `unpublished`.
    """
    if kind == KIND_STUDIO_HUB:
        return {"price_source": "local",
                "price_usd": {unit: 0 for unit in _LOCAL_UNITS.get(modality, ())} or None,
                "price_as_of": None}
    stated = _read_provider(modality, raw) if isinstance(raw, dict) else None
    if stated is None:
        page = price_evidence.page_price(provider_id, modality, model_id)
        if page is not None:
            return page
        return {"price_source": "unpublished", "price_usd": None, "price_as_of": None}
    as_of = (datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat()
             if isinstance(fetched_at, (int, float)) and fetched_at > 0 else None)
    return {"price_source": "provider", "price_usd": stated, "price_as_of": as_of}


def is_free_lane(free_tier: bool, model_id: str) -> bool:
    """Whether a row is a provider's free offer, read from what is stated.

    Two cases, and nothing guessed between them: the provider **is** the free tier (`free_tier` on
    the definition), or the id carries the suffix its provider uses to mark a free route — `:free`
    on OpenRouter and Token Harbor, `-free` on Fish. A model that is merely cheap is not free.
    """
    lowered = str(model_id or "").lower()
    return bool(free_tier) or lowered.endswith(":free") or lowered.endswith("-free")


def chargeable(free_tier: bool, model_id: str, price: dict[str, Any]) -> bool:
    """Whether using this model can cost money — the Models page's Free/Paid, as one bool.

    Free when it is a free lane, runs on the owner's Macs (`price_source: local`), or the provider
    states a price of $0 for every unit. Anything else — including a price nobody published — is
    paid: "nobody told us" is never read as free.
    """
    if is_free_lane(free_tier, model_id) or price.get("price_source") == "local":
        return False
    usd = price.get("price_usd")
    stated = [v for v in usd.values() if v is not None] if isinstance(usd, dict) else []
    return not (stated and all(float(v) == 0 for v in stated))


def model_chargeable(definition: Any, model: dict[str, Any], modality: str = "chat") -> bool:
    """`chargeable` for one resolved (definition, model) pair, judged exactly as the catalogue row
    is (`catalog._story_row`), so a pool's free/paid split can never disagree with the Models page."""
    return definition_chargeable(definition, modality, str(model.get("model_id") or ""), model.get("raw"))


def definition_chargeable(definition: Any, modality: str, model_id: str = "",
                          raw: dict[str, Any] | None = None) -> bool:
    """`chargeable` for a row that is not built by the catalogue (a voice, a TTS quote), by the
    catalogue's own rule, so the three cannot describe the same model differently."""
    price = row_price(definition.kind, modality, raw, None, definition.id, model_id)
    return chargeable(getattr(definition, "free_tier", False), model_id, price)


def job_cost(payload: dict[str, Any] | None) -> dict[str, Any]:
    """`{cost_usd, cost_source}` when the provider's response states what it charged, else `{}`.

    Read only from OpenRouter's usage-accounting shape (`usage.cost` in USD beside `is_byok`;
    measured on OpenRouter and Experiential Labs jobs). A bare `cost` without that companion is
    not assumed to be USD.
    """
    usage = (payload or {}).get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("is_byok"), bool):
        cost = _number(usage.get("cost"))
        if cost is not None:
            return {"cost_usd": _out(cost), "cost_source": "provider"}
    return {}

