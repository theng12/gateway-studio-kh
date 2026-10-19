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
  * image-to-image     (chargeable must be published; catalog rows overwrite it truthfully)
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
# reference-image provider required it truthy -- which turned into a hard throw
# on the img2img path. Publish it explicitly, everywhere. This is only the shape:
# catalogue rows, voice rows and the TTS quote replace it with `pricing.chargeable`
# (false for a model on the owner's Macs), which Story reads as price, not capability.
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
        case "embeddings":
            base |= {
                "input_credits_per_million_tokens": 0,
            }
        case "vision":
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
ADMISSION: dict[str, Any] = {
    # **256, and now enforced.** It was 32 and read by nothing on this side (audit H7/M8), while
    # Story Studio *does* read it and holds itself to it. With 53 Macs and multi-hour video jobs
    # a 32-deep queue is one busy afternoon, so the shipped default is raised and the gateway
    # refuses past it with a clean `429` + `Retry-After` rather than growing an unbounded
    # backlog.
    "max_queued_jobs": 256,
    "generation_submissions_per_minute": 60,
}
CREDIT_BALANCE = 0


def admission() -> dict[str, Any]:
    """The admission record as published, with the concurrency the fleet actually has.

    **`paid_concurrency_limit` is the fleet's own live capacity** (the owner's decision D:
    *"setting a number isn't correct … it must be dynamic of reality"*). It used to be the
    settings pool — live **60** while the fleet could do about **4** — so Story Studio acted on a
    configured number that described nothing (H7). It is now the total from
    `concurrency.live_capacity()`, which sums the very budget admission enforces over every
    reachable location.

    **The pool is still published, beside it, under its own name.** It bounds how many jobs
    *this* gateway runs at once; it is not the fleet's capacity, and a reader who sees the two
    disagree should be able to tell which is which rather than discovering it by counting. The
    extra fields are additive — Story Studio ignores what it does not know.
    """
    from . import concurrency

    worker = None
    try:
        from .runtime import worker as live_worker  # noqa: PLC0415 - cycle, by design
        worker = live_worker
    except Exception:  # noqa: BLE001 - a figure is not worth failing a response over
        worker = None
    # **The summary the fleet refresher published, not a fresh walk of the fleet.** `/health` is
    # this record's main reader and the app's 2.5 s probe: computing it here meant every poll
    # re-summed every location, and — before `fleet.snapshot(allow_stale=True)` stopped waiting
    # for the fan-out — parked the probe behind a slow controller until the window decided the
    # gateway was not running. A cold cache (the boot window, before the first fan-out) still
    # computes it, which is the one moment there is nothing to read.
    capacity = concurrency.published_capacity()
    if capacity is None:
        capacity = concurrency.live_capacity(worker=worker)
    by_modality = dict(capacity["by_modality"])
    published: dict[str, Any] = {
        **ADMISSION,
        "paid_concurrency_limit": capacity["total"],
        "concurrency_source": capacity["source"],
        "concurrency_by_modality": by_modality,
        "concurrency_pool": capacity["pool"]["limit"],
    }
    # **`burst_concurrency_ceiling`: how many requests Story may stage at once.** Story reads this
    # one integer on `/v1/account/overview` to size its image fan-out (and caps TTS fan-out with
    # it); unpublished, it sends scene images one at a time -- ~9 h an episode on a fleet that
    # could take dozens. It is the fleet's own live capacity, never an invented wide wave: the
    # widest single lane (Story fans out one modality at a time), held under what this gateway
    # will itself run at once (the pool) and queue (`max_queued_jobs`). Zero means unknown, and
    # unknown is *omitted* -- Story then stays serial, which is the safe direction.
    ceiling = min(
        max((int(v) for v in by_modality.values() if isinstance(v, int)), default=0),
        int(capacity["pool"]["limit"]),
        int(ADMISSION["max_queued_jobs"]),
    )
    if ceiling > 0:
        published["burst_concurrency_ceiling"] = ceiling
    return published
