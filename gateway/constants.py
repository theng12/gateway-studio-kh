"""Zero-valued commercial constants — the single most important design decision.

gateway-kh has no billing, no credits, no customers and no pricing. Story Studio
KH, however, **cannot** simply be told the prices are absent.

Its catalogue parsers build explicit object literals and *drop any row* whose
price fields are missing. The generators then fail with:

    The selected voice model "..." is not in your GenStudio KH catalogue.

So a gateway that omits pricing does not get "unpriced but working" models — it
gets an empty picker, and the following features stop working outright:

  * TTS narration      (row dropped without credits_per_1000_characters)
  * video generation   (variant dropped without credits_per_video/price_version)
  * music generation   (row dropped without credits_per_request)
  * image-to-image     (chargeable must be published truthy)
  * durable LLM lineup (fallback advances only on a proven zero settlement)
  * paid-replacement   (needs finite credits + non-empty price_version)

The fix is not to build pricing — it is to publish the *shape* of a priced
catalogue with every amount pinned to zero. Every gate is satisfied; nothing is
charged; no billing system exists behind it.

Keep this module the single source of truth. If a modality ever needs a new
price-shaped field to survive Story Studio's parser, add it here and nowhere
else.
"""

from __future__ import annotations

from typing import Any

# A non-empty string is required by several parsers (the video variant parser
# rejects a blank price_version outright). The literal value is never displayed
# as a number, so any stable token works.
PRICE_VERSION = "flat"

# Story Studio's LLM cost tiers are promotional|value|economy|premium; media
# tiers are economy|balanced|premium.
#
# `cost_tier` is ALL-OR-NOTHING: if even one row in a modality omits it, the
# frontend hides the entire tier-tab strip and renders every row unfiltered.
# Publish it on every row of a modality, or on none of them. Never partially.
MEDIA_COST_TIER = "economy"
LLM_COST_TIER = "economy"

# `chargeable` is read asymmetrically by Story Studio. Most parsers compare
# explicitly, but the image parser keeps it absent (undefined) while the
# reference-image provider requires it truthy -- which turns into a hard throw
# on the img2img path. Publish it explicitly, everywhere.
CHARGEABLE = True


# --------------------------------------------------------------------------
# Every settled job must carry a *balanced* settlement triple.
#
# This is not display data. Story Studio advances its durable LLM lineup to the
# next candidate only when it can PROVE the failed attempt cost nothing:
#
#     reserved_credits, settled_credits, released_credits all finite and >= 0
#     AND settled_credits == 0
#     AND released_credits == reserved_credits
#
# Omitting the fields makes the lineup stop at `failed_terminal` instead of
# trying the next model. A constant 0/0/0 satisfies the predicate exactly --
# the free-retry proof actively *wants* a balanced zero.
# --------------------------------------------------------------------------
ZERO_SETTLEMENT: dict[str, int] = {
    "reserved_credits": 0,
    "settled_credits": 0,
    "released_credits": 0,
}


def zero_pricing(modality: str) -> dict[str, Any]:
    """Return the zero-valued price fields a modality's row must publish.

    Each key exists because a specific Story Studio gate requires it.
    """
    base: dict[str, Any] = {
        "minimum_credits": 0,
        "price_version": PRICE_VERSION,
        "chargeable": CHARGEABLE,
        "cost_tier": LLM_COST_TIER if modality == "chat" else MEDIA_COST_TIER,
    }

    match modality:
        case "chat":
            base |= {
                "input_credits_per_million_tokens": 0,
                "output_credits_per_million_tokens": 0,
                # `prefer_savings` ordering reads all three; a missing rate is
                # deliberately NOT read as zero, so publish them to be grouped
                # as free rather than falling back to authored order.
                "standard_input_credits_per_million_tokens": 0,
                "standard_output_credits_per_million_tokens": 0,
                "standard_minimum_credits": 0,
            }
        case "image":
            base |= {
                "credits_per_image": 0,
                "base_credits_per_image": 0,
            }
        case "video":
            base |= {
                "credits_per_video": 0,
            }
        case "tts":
            base |= {
                "credits_per_1000_characters": 0,
                "standard_credits": 0,
            }
        case "transcription":
            base |= {
                "credits_per_second": 0,
            }
        case "music":
            base |= {
                "credits_per_request": 0,
            }

    return base


# --------------------------------------------------------------------------
# Content Store commerce constants.
#
# Story Studio's purchase path requires these as strict integers and refuses a
# purchase if any is absent. Everything on this gateway is pre-owned and free,
# so a purchase is a no-op that reports a successful zero-credit acquisition.
# --------------------------------------------------------------------------
ZERO_PURCHASE: dict[str, Any] = {
    "credits_charged": 0,
    "discount_credits": 0,
    "balance_before": 0,
    "balance_after": 0,
    "sufficient_credits": True,
    "discount": None,
    "receipt": None,
}

# --------------------------------------------------------------------------
# Account / admission constants.
#
# Two hard requirements:
#   1. `paid_concurrency_limit` must be a valid integer. The admission loader
#      throws on anything else and only then falls back to its safe default.
#   2. `credit_balance` must be published. If a 402 is ever returned and the
#      balance is never published, Story Studio locks that API key out
#      PERMANENTLY -- the hold clears only on a fresh balance greater than the
#      held balance.
#
# Therefore: never return 402 from this gateway. Ever.
# --------------------------------------------------------------------------
#: The parts of the admission record that really are constant.
#:
#: **`paid_concurrency_limit` deliberately moved out of here.** It was the literal
#: `4` while the worker ran exactly one job at a time — a published number that did
#: not describe the system, in a field Story Studio reads and acts on. It now comes
#: from `concurrency.global_limit()`, so the figure the caller is given is the figure
#: the gateway enforces. Leaving it here would drift the moment the pool is resized.
ADMISSION: dict[str, int] = {
    "max_queued_jobs": 32,
    "generation_submissions_per_minute": 60,
}
CREDIT_BALANCE = 0


def admission() -> dict[str, int]:
    """The admission record as published, with the concurrency actually in force."""
    from . import concurrency

    return {**ADMISSION, "paid_concurrency_limit": concurrency.global_limit()}
