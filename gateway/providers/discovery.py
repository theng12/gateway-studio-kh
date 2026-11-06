"""Live model discovery.

Each provider kind answers "what models do you have?" differently, and several
answer "nothing, here is a list you maintain". This module hides that behind one
function so the rest of the gateway never branches on provider kind.

Every strategy returns a `DiscoveryResult` and **never raises**: a provider being
down, rate-limited, or misconfigured must not break the catalogue for the other
seventeen. Failures are recorded and surfaced in the admin UI instead.

Modality inference is deliberately conservative. Guessing wrong means a chat
model appears in the image picker, which is worse than the model not appearing
at all -- so unknown ids fall back to the provider's declared first modality
rather than being invented into a random bucket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .definitions import (
    DISCOVERY_ANTHROPIC_MODELS,
    DISCOVERY_ELEVENLABS,
    DISCOVERY_FAL_CATALOG,
    DISCOVERY_GOOGLE_MODELS,
    DISCOVERY_OPENAI_MODELS,
    DISCOVERY_STATIC,
    DISCOVERY_STUDIO_HUB,
    ProviderDefinition,
)

TIMEOUT = httpx.Timeout(12.0, connect=6.0)


@dataclass(slots=True)
class DiscoveredModel:
    model_id: str
    display_name: str
    modality: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryResult:
    models: list[DiscoveredModel] = field(default_factory=list)
    error: str | None = None
    fetched_at: float = field(default_factory=time.time)
    #: Packages the provider listed that this gateway could not classify at all,
    #: one entry per engine: `{engine, count, model_ids, reason}`.
    #:
    #: **"I could not work out what this is" must never look like "there is
    #: nothing".** A Studio Hub package whose engine and stated modality are both
    #: unknown used to be `continue`d in silence, so a Hub serving a lane this
    #: gateway has never heard of produced the same empty inventory as a Hub with
    #: no models at all. Kept so the count and the engine names can be published.
    skipped: list[dict[str, Any]] = field(default_factory=list)
    #: The Hub's catalogue did not answer, so these rows carry no per-Mac facts (`ModelInventory.refresh`
    #: keeps the last answer instead of replacing it with these).
    catalogue_missing: bool = False


# ---------------------------------------------------------------------------
# Modality inference
# ---------------------------------------------------------------------------
# Ordered rules; the first match wins. Kept explicit rather than fuzzy so that a
# surprising classification is easy to trace and correct.
_MODALITY_RULES: tuple[tuple[str, str], ...] = (
    ("whisper", "transcription"),
    ("transcribe", "transcription"),
    ("dall-e", "image"),
    ("dalle", "image"),
    ("gpt-image", "image"),
    ("imagen", "image"),
    ("flux", "image"),
    ("stable-diffusion", "image"),
    ("sdxl", "image"),
    ("ideogram", "image"),
    ("recraft", "image"),
    ("veo", "video"),
    ("kling", "video"),
    ("runway", "video"),
    ("minimax-video", "video"),
    ("video-01", "video"),
    ("hailuo", "video"),
    ("lyria", "music"),
    ("music", "music"),
    ("suno", "music"),
    ("tts", "tts"),
    ("text-to-speech", "tts"),
    ("speech", "tts"),
    ("voice", "tts"),
    # Generic fallbacks, deliberately AFTER the specific vendor rules above so
    # that e.g. `gpt-image-1` classifies by its vendor rule first. A model
    # whose name literally contains "image" or "video" is far more likely to be
    # one than not; without these, `foo-image-v2` fell through to the
    # provider's first declared modality and vanished from the image picker.
    ("image", "image"),
    ("video", "video"),
    ("embedding", "skip"),
    ("moderation", "skip"),
    ("rerank", "skip"),
)


#: fal categories that name an **understanding** endpoint — it consumes media and
#: answers with text (`vision`, a captioner, a video describer). Read as chat so
#: the provider's own words decide, rather than the id: `...-video-understanding`
#: contains "video" and was classified as a video *generator*, which is the one
#: thing it cannot be. fal serves no chat lane, so its own filter then drops the
#: row instead of publishing a model that fails on use.
_UNDERSTANDING_CATEGORIES = frozenset(
    {"vision", "video-to-text", "image-to-text", "audio-to-text", "text-to-text"}
)


def _modality_from_architecture(stated: str) -> str:
    """Read OpenRouter's `architecture.modality` — `text+image->text`, and so on.

    The **output** side decides, and only when it is unambiguous:

      * `->text` is a chat model;
      * `->image` and `->video` are generation models;
      * a *mixed* output (`text+image`, `text+audio`) decides nothing. Measured:
        `openrouter/auto` produces `text+image` because it can route to an image
        model, and both `google/lyria-3-*` rows produce `text+audio` because the
        preview also returns lyrics — reading either as a definite modality would
        have published the router as an image model and a **music** model as chat.
        They keep their id-based classification instead.

    Returns `""` when the statement does not decide, leaving the id rules to
    speak — this is a statement to read, not a replacement for every signal.
    """
    if "->" not in stated:
        return ""
    _inputs, _, outputs = stated.partition("->")
    produced = {part.strip() for part in outputs.split("+") if part.strip()}
    if produced == {"text"}:
        return "chat"
    if produced == {"image"}:
        return "image"
    if produced == {"video"}:
        return "video"
    return ""


def infer_modality(model_id: str, definition: ProviderDefinition, hint: str = "") -> str:
    """Best-effort modality for a discovered model id.

    `hint` is a provider-supplied statement: fal publishes a `category`, and
    OpenRouter publishes `architecture.modality`. Either wins when it names a
    modality we actually serve, because a provider's own words beat a guess made
    from the model's name.
    """
    lowered_hint = (hint or "").strip().lower()
    if lowered_hint in {"text-to-image", "image", "text-to-image-to-image"}:
        return "image"
    if lowered_hint in {"text-to-video", "image-to-video", "video"}:
        return "video"
    if lowered_hint in {"text-to-speech", "tts", "voice"}:
        return "tts"
    if lowered_hint in {"speech-to-text", "transcription"}:
        return "transcription"
    if lowered_hint in {"music", "text-to-music"}:
        return "music"
    if lowered_hint in {"llm", "text", "chat"}:
        return "chat"
    if lowered_hint in _UNDERSTANDING_CATEGORIES:
        return "chat"
    if "->" in lowered_hint:
        stated = _modality_from_architecture(lowered_hint)
        if stated:
            return stated

    lowered = model_id.lower()
    for needle, modality in _MODALITY_RULES:
        if needle in lowered:
            return modality

    # Fall back to the provider's declared scope. A single-modality provider
    # (every language provider) is unambiguous here; for a provider that declares
    # several, this is the ordering the definition deliberately puts first.
    return definition.modalities[0] if definition.modalities else "chat"


def _clean(name: str, fallback: str) -> str:
    return (name or "").strip() or fallback


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
def _openai_models(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    key = values.get("api_key", "")
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    if not key:
        return DiscoveryResult(error="no API key configured")
    try:
        response = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    payload = response.json()
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return DiscoveryResult(error="unexpected /models payload")

    models: list[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or entry.get("name") or "").strip()
        if not model_id:
            continue
        # **The provider's own classification, when it states one.** OpenRouter
        # publishes `architecture.modality` (`text+image->text`, `video->text`)
        # and it was fetched, stored, and never read — so `router/video`, a video
        # *describer*, was classified as a video generator by the word in its id.
        # fal's `category` arrives through its own strategy; this is the same read.
        architecture = entry.get("architecture")
        hint = ""
        if isinstance(architecture, dict):
            hint = str(architecture.get("modality") or "")
        modality = infer_modality(model_id, definition, hint)
        if modality == "skip":
            continue
        models.append(
            DiscoveredModel(
                model_id=model_id,
                display_name=_clean(str(entry.get("name") or ""), model_id),
                modality=modality,
                # `top_provider` carries the real `max_completion_tokens`, which
                # the catalogue publishes as the model's output allowance. It was
                # being discarded, and the catalogue then invented a number for
                # every model instead.
                #
                # Every spelling the chat encoder reads has to survive here, or the
                # encoder's fallbacks are dead code: it looks for
                # `context_window`, `input_token_limit`, `max_completion_tokens`
                # and `output_token_limit` after the OpenRouter spellings, and
                # those are exactly what a vLLM- or LiteLLM-style
                # `/v1/models` publishes — so a provider that stated its limits
                # plainly was published as `null`, which reads as "it said
                # nothing". Reading every spelling is only meaningful if the
                # spellings reach the reader.
                # **The whole entry, not a whitelist.**
                #
                # This kept a fixed set of keys and threw the rest away at fetch
                # time, and it had already been patched twice by adding one more
                # key — `top_provider` for the real output allowance, and
                # `maximum_text_length_per_request` for the TTS limit. Both times
                # the fix was another entry in this set, which means the *next*
                # provider fact also needed a discovery change before anyone could
                # read it. Two measured consequences of the whitelist:
                #
                #   * Story Studio carries twenty-five chat fields the catalogue
                #     never publishes (`WIRE-3`), and for the retirement family the
                #     provider's own answer — OpenRouter's `expiration_date` — was
                #     in the response and discarded here;
                #   * the catalogue's own fallbacks were dead code, because the
                #     spellings they look for never reached the reader.
                #
                # A `/models` response is public metadata by definition, so there
                # is nothing here to withhold on the model's behalf; what the
                # catalogue *publishes* stays explicit (each row names the keys it
                # reads), so keeping more raw can only turn "it said nothing" into
                # "it said this".
                raw=dict(entry),
            )
        )
    return DiscoveryResult(models=models)


def _anthropic_models(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    key = values.get("api_key", "")
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    if not key:
        return DiscoveryResult(error="no API key configured")
    try:
        response = httpx.get(
            f"{base}/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    entries = (response.json() or {}).get("data", [])
    models = [
        DiscoveredModel(
            model_id=str(e.get("id", "")).strip(),
            display_name=_clean(str(e.get("display_name") or ""), str(e.get("id", ""))),
            modality="chat",
            # `capabilities` is Anthropic's own per-model tree (`structured_outputs.supported`,
            # `thinking`, `effort`, …); `catalog._chat_row` reads `structured_outputs` from it.
            #
            # `max_tokens` is Anthropic's per-model output cap (Models API; Haiku 5.5 states
            # 128K). It is kept under `max_completion_tokens`, the spelling `catalog._chat_row`
            # already reads, so the row publishes the model's own figure (source `provider`)
            # instead of the adapter's 4,096 fallback. Absent stays `None`, and the owner's
            # `model-limits.local.json` or that fallback answer as before.
            raw={"id": e.get("id"), "created_at": e.get("created_at"),
                 "capabilities": e.get("capabilities"),
                 "max_completion_tokens": e.get("max_tokens")},
        )
        for e in entries
        if isinstance(e, dict) and e.get("id")
    ]
    return DiscoveryResult(models=models)


def _google_models(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    key = values.get("api_key", "")
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    if not key:
        return DiscoveryResult(error="no API key configured")
    try:
        response = httpx.get(
            f"{base}/models",
            params={"key": key, "pageSize": 200},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    entries = (response.json() or {}).get("models", [])
    models: list[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_name = str(entry.get("name") or "")
        model_id = raw_name.split("/", 1)[1] if raw_name.startswith("models/") else raw_name
        if not model_id:
            continue
        # Google reports the operations a model supports; prefer that over
        # keyword guessing because it is authoritative.
        methods = " ".join(entry.get("supportedGenerationMethods") or []).lower()
        if "embedcontent" in methods or "embed" in model_id.lower():
            continue
        modality = infer_modality(model_id, definition)
        models.append(
            DiscoveredModel(
                model_id=model_id,
                display_name=_clean(str(entry.get("displayName") or ""), model_id),
                modality=modality,
                raw={
                    "name": raw_name,
                    "methods": methods,
                    # Google states both limits on every model. They were being
                    # discarded, so every Google row published a null context
                    # window and a null output allowance while the real figures
                    # were in the response.
                    "inputTokenLimit": entry.get("inputTokenLimit"),
                    "outputTokenLimit": entry.get("outputTokenLimit"),
                    "description": entry.get("description"),
                },
            )
        )
    return DiscoveryResult(models=models)


#: fal categories whose endpoints accept **text** and produce audio. The TTS lane
#: sends `text`, so anything else in fal's audio family — a voice changer, a
#: realtime voice session, speech-to-speech, speech-to-text — cannot serve it.
_FAL_TEXT_AUDIO_CATEGORIES: frozenset[str] = frozenset({"text-to-speech", "text-to-audio"})


def _fal_catalog(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    key = values.get("api_key", "")
    base = (definition.catalog_base_url or definition.base_url).rstrip("/")
    if not key:
        return DiscoveryResult(error="no API key configured")
    try:
        response = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Key {key}"},
            params={"limit": 500},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    payload = response.json() or {}
    entries = payload.get("models") or payload.get("items") or payload.get("data") or []
    if not isinstance(entries, list):
        return DiscoveryResult(error="unexpected fal catalogue payload")

    models: list[DiscoveredModel] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(
            entry.get("endpoint_id") or entry.get("id") or entry.get("model_id") or ""
        ).strip()
        if not model_id:
            continue
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        hint = str(metadata.get("category") or entry.get("category") or "")
        modality = infer_modality(model_id, definition, hint)
        # Only what the adapter actually serves. fal's catalogue carries 20-odd
        # categories; this keeps the ones behind a working code path and drops the
        # rest, rather than discovering models that could never run.
        if modality not in {"image", "video", "tts"}:
            continue
        if modality == "tts" and hint not in _FAL_TEXT_AUDIO_CATEGORIES:
            # The adapter sends `text`, so only endpoints that *take* text work.
            # Category is fal's own declaration, and it is the only reliable signal
            # here: id-based inference pulled in a voice *changer*
            # (`audio-to-audio`), a realtime voice, and speech-to-speech models —
            # all of which would publish and then reject a text-to-speech request.
            continue
        models.append(
            DiscoveredModel(
                model_id=model_id,
                display_name=_clean(
                    str(entry.get("display_name") or metadata.get("display_name") or ""),
                    model_id,
                ),
                modality=modality,
                raw={"endpoint_id": model_id, "category": hint},
            )
        )
    _fal_prices(base, key, models)
    return DiscoveryResult(models=models)


def _fal_prices(base: str, key: str, models: list[DiscoveredModel]) -> None:
    """Attach fal's own stated price to each model's `raw["pricing"]` (`catalog` reads it).

    `GET {base}/models/pricing?endpoint_id=a,b,...` (1-50 ids) answers
    `{prices: [{endpoint_id, unit_price, unit, currency}]}`. Best effort by design: the price is
    additive, so a failure here leaves every model discovered and unpriced (`unpublished`) rather
    than failing discovery — and stops at the first failure instead of repeating the timeout.
    """
    for start in range(0, len(models), 50):
        batch = models[start:start + 50]
        try:
            response = httpx.get(
                f"{base}/models/pricing",
                headers={"Authorization": f"Key {key}"},
                params={"endpoint_id": ",".join(m.model_id for m in batch)},
                timeout=TIMEOUT,
            )
            prices = response.json().get("prices") if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return
        by_id = {p.get("endpoint_id"): p for p in prices or [] if isinstance(p, dict)}
        for model in batch:
            stated = by_id.get(model.model_id)
            if stated:
                model.raw["pricing"] = {k: stated.get(k) for k in ("unit_price", "unit", "currency")}


def _elevenlabs_models(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    key = values.get("api_key", "")
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    if not key:
        return DiscoveryResult(error="no API key configured")
    try:
        response = httpx.get(f"{base}/v1/models", headers={"xi-api-key": key}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    entries = response.json()
    if not isinstance(entries, list):
        return DiscoveryResult(error="unexpected ElevenLabs payload")
    models = [
        DiscoveredModel(
            model_id=str(e.get("model_id", "")).strip(),
            display_name=_clean(str(e.get("name") or ""), str(e.get("model_id", ""))),
            modality="tts",
            raw={
                "model_id": e.get("model_id"),
                "languages": e.get("languages"),
                # **Only the character limit is stated here.** This comment used to
                # claim ElevenLabs also publishes per-model *speed* support; its
                # documented model object has no such field, and `can_use_style`
                # was read, stored and never consumed. What decides
                # `supports_speed` is the curated `catalog._TTS_NO_SPEED`, so the
                # comment was the only place the false claim lived — removed
                # rather than left for the next reader to believe.
                "maximum_text_length_per_request": e.get("maximum_text_length_per_request"),
                "max_characters_request_subscribed_user": e.get(
                    "max_characters_request_subscribed_user"
                ),
            },
        )
        for e in entries
        if isinstance(e, dict) and e.get("model_id")
    ]
    return DiscoveryResult(models=models)


_HUB_ENGINE_MODALITY = {
    "image-mflux": "image",
    "voice-mlx": "tts",
    # The Hub's own engine->worker table, and the one the brief names.
    "music-mlx": "music",
    "subtitle-mlx": "transcription",
    # No video engine here. A local video engine the Hub no longer ships is absent from this
    # table, so a package that still names one and states no modality of its own is reported as
    # unrecognised rather than offered. A video model the Hub states a modality for is still
    # placed by that statement.
}

#: The Hub's **stated** modality for a model, in this gateway's vocabulary.
#:
#: `/api/hub/catalog` (and `/api/hub/models`) publish `hub_modality` per model —
#: measured live: `voice` 197, `transcription` 65, `music` 10, `image` 5 — and
#: that statement is what to read, exactly as a provider's `context_length` beats
#: a curated guess. `_HUB_ENGINE_MODALITY` above is only the fallback for a Hub
#: that states nothing (an older build, or a catalogue read that failed), because
#: keying on the engine name means a new engine is invisible here until this file
#: is edited — which is what a model the owner adds on the Hub would hit.
#: The Hub's **stated** modality for a model, in this gateway's vocabulary.
#:
#: Two Hub routes state it and both are read: `modality` on each
#: `/api/hub/model-packages` row (Hub 0.6.61+, from its own `_ENGINE_ROUTE` table,
#: "so a scheduler does not have to make a second, best-effort catalog read and
#: join on model id") and `hub_modality` on `/api/hub/catalog` and
#: `/api/hub/models` (measured live on 0.6.54: `voice` 197, `transcription` 65,
#: `music` 10, `image` 5). Either statement beats `_HUB_ENGINE_MODALITY` below,
#: which is only the fallback for a Hub that says nothing — keying on the engine
#: name means a new engine is invisible here until this file is edited, which is
#: what a model the owner adds on the Hub would hit.
#:
#: `subtitle` is the node roster's spelling of the Subtitle studio and
#: `transcription` is the capability roster's; both are the same studio.
_HUB_STATED_MODALITY = {
    "voice": "tts",
    "tts": "tts",
    "image": "image",
    "transcription": "transcription",
    "subtitle": "transcription",
    "music": "music",
    # Not served by `studio_hub` today (see `_MODALITIES_BY_KIND`), so these are
    # discovered and **withheld** by `can_publish` rather than dropped here: the
    # owner sees the row and the reason, and the day the adapter gains the lane
    # the same row starts publishing with no change to this table.
    "video": "video",
    "chat": "chat",
    # Hub 0.6.165: the Embeddings Studio (engine `embed-mlx`).
    "embeddings": "embeddings",
    # Hub 0.6.166: image analysis (engine `vision-mlx`): depth, subject, points, segments.
    "vision": "vision",
    "text": "chat",
    "llm": "chat",
}


def _hub_catalog(base: str, token: str, *, local: bool = False) -> dict[str, dict[str, Any]] | None:
    """Per-model capabilities and reference contracts, from the Hub's catalogue.

    `/api/hub/model-packages` answers *what is installed*; `/api/hub/catalog`
    answers *what a model is* — including whether it clones at all and what
    reference contract it declares. Neither endpoint answers the other's
    question, so both are read. Verified against Hub 0.6.18: the catalogue
    publishes, per model, `capabilities` (with `voice-cloning`) and a
    `reference_audio` block carrying `declared`/`supported`/`profile_audited`
    plus `minimum`/`target`/`recommended`/`maximum_duration_seconds` and
    whether a transcript is required.

    **A missing catalogue is not a missing capability.** If this endpoint is
    unavailable or slow the models still publish; they simply carry no
    reference facts, and every reader treats that as *unverified* rather than
    as "cannot clone". That distinction is the whole point: a model wrongly
    offered as non-cloning loses its voices silently, while an unverified one
    is offered and reported honestly.

    Indexed by `repo` (the Hub's spelling of a model id), falling back to any
    `model_id` key so a future rename does not silently empty this map.

    `None` when the read did not answer (timeout, HTTP error, no rows): "unread" is not "empty".
    """
    from ..hub import hub_headers  # local import: avoids a cycle

    try:
        response = httpx.get(
            f"{base.rstrip('/')}/api/hub/catalog",
            headers=hub_headers(token, local=local),
            timeout=TIMEOUT,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    payload = response.json() or {}
    rows = payload.get("models") or payload.get("items") or []
    if not isinstance(rows, list):
        return None

    # **One row per worker, not per model.** The Hub's aggregate catalogue passes every studio's
    # rows through, one per Mac (`hub_machine`), and each states `memory_eligible` for *its own*
    # Mac. Last-wins let whichever Mac happened to be listed last speak for the whole site: an
    # 8 GB node at the end of 1100/1300/1400 withheld VoxCPM2, Breeze and Qwen3 1.7B from those
    # locations although their 16/24 GB Macs run them, so 86 TTS jobs queued on 1500/1600 while
    # those sites sat idle (measured 2026-10-04). A row from a Mac that fits is the site's answer.
    # The same holds for files and readiness: a site serves a model when **any** of its Macs has it
    # installed, ready and fitting, so that Mac's row is the one kept (see `mac_ready`).
    indexed: dict[str, dict[str, Any]] = {}
    image_workers: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("repo") or row.get("model_id") or "").strip()
        if key and (key not in indexed or _site_rank(row) > _site_rank(indexed[key])):
            indexed[key] = row
        capabilities = row.get("capabilities")
        if key and ("image_modes" in row or "reference_lane" in row or (
            isinstance(capabilities, list) and any(token in capabilities for token in ("txt2img", "edit"))
        )):
            # A ready text worker must not hide another Mac's qualified reference lane.
            image_workers.setdefault(key, []).append({field: row[field] for field in (
                "hub_machine", "hub_cached", "memory_eligible", "runtime_ready", "available",
                "image_modes", "capabilities", "reference_lane", "lane_contract",
            ) if field in row})
    return {key: {**row, **({"image_workers": image_workers[key]} if key in image_workers else {})}
            for key, row in indexed.items()} or None


#: A **separate, short** timeout for the capability snapshot. It is guidance, not the catalogue:
#: an old Hub that never answers this route must not add the full `TIMEOUT` (12 s) per location to
#: every discovery — the catalogue list beside it is the call that decides whether a location can
#: be served at all (review 2026-10-29f, finding 4).
CAPABILITIES_TIMEOUT = httpx.Timeout(3.0, connect=2.0)


def _hub_controls(base: str, token: str, *, local: bool = False) -> dict[str, dict[str, Any]] | None:
    """Per-model `controls` from the Hub's capability snapshot (`/api/hub/capabilities`).

    **This is where a model's delivery-instruction guidance lives.** The catalogue says *that*
    a model takes a per-request direction (`capabilities` carries `delivery-instruction`); the
    snapshot says *how* — the engine form, the card's examples, its language rule, the inline
    vocal events and the source link — under the model's `controls.delivery_instruction`. The
    gateway forwards that object unchanged, so the page shows the Hub's own words.

    Indexed by the snapshot's canonical `model_id` **and** by `repo`, because the package list
    spells a model `repo` while the snapshot publishes a canonical id.

    `None` when the read did not answer — "unread" is not "publishes nothing". A Hub without the
    route (or an older one) leaves every model's guidance absent, and the catalogue then
    publishes the control with no guidance rather than an invented one.

    **A route that cannot be read is guidance missing, never a location failing to refresh.** It
    is one optional request beside the two that carry the catalogue, so a hang, an error status,
    a 200 that is not JSON or a payload of the wrong shape all answer `None` here: the location's
    models still discover, and only the guidance is absent (review 2026-10-29f, finding 4 — an
    unguarded `response.json()` took the whole location's discovery down with it).
    """
    from ..hub import hub_headers  # local import: avoids a cycle

    try:
        response = httpx.get(
            f"{base.rstrip('/')}/api/hub/capabilities",
            headers=hub_headers(token, local=local),
            timeout=CAPABILITIES_TIMEOUT,
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except ValueError:  # includes JSONDecodeError: a 200 that is a page of HTML
        return None
    if not isinstance(payload, dict):
        return None
    index: dict[str, dict[str, Any]] = {}
    rows = payload.get("models")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        controls = row.get("controls")
        if not isinstance(controls, dict):
            continue
        for key in (row.get("model_id"), row.get("repo")):
            name = str(key or "").strip()
            if name:
                index.setdefault(name, controls)
    return index or None


def mac_ready(row: dict[str, Any]) -> bool:
    """One Mac's catalogue row says that Mac can run the model: installed, runtime ready, fits.

    Only an explicit `False` disqualifies `memory_eligible`/`runtime_ready`/`available`, as on the
    Hub's own `_model_capability`; `None` is "not reported", never "cannot".
    """
    return (
        row.get("hub_cached") is True
        and row.get("memory_eligible") is not False
        and row.get("runtime_ready") is not False
        and row.get("available") is not False
    )


def _site_rank(row: dict[str, Any]) -> int:
    """Which Mac's row speaks for the site: one that can run it, then one that fits, then unknown."""
    if mac_ready(row):
        return 3
    return {True: 2, None: 1}.get(row.get("memory_eligible"), 0)


def _package_stated_modality(package: dict[str, Any]) -> str | None:
    """What the **package row itself** declares, in this gateway's vocabulary.

    Hub 0.6.61 puts `modality` on every `/api/hub/model-packages` row, read from
    its own engine table. It is present whenever the engine is registered — even
    on a row whose manifest is unusable — and absent (or `null`) when the engine
    is not, which the Hub states rather than guesses. `None` here therefore means
    "the Hub did not place it", never "it has no modality".
    """
    if "modality" not in package:
        return None
    stated = str(package.get("modality") or "").strip().lower()
    return _HUB_STATED_MODALITY.get(stated) if stated else None


def _stated_hub_modality(facts: dict[str, Any] | None) -> str | None:
    """The catalogue's modality for a model, in this gateway's vocabulary.

    `None` when the catalogue said nothing this gateway names, so the caller
    falls back to the package row and then to the engine table, and the published
    `modality_source` says which reader placed the row.
    """
    if not isinstance(facts, dict):
        return None
    stated = str(facts.get("hub_modality") or facts.get("modality") or "").strip().lower()
    return _HUB_STATED_MODALITY.get(stated) if stated else None


def _hub_modality(
    package: dict[str, Any], facts: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """Which of this gateway's modalities a Hub package belongs to, and who said so.

    Three readers, strongest first — each one added because the one below it was a
    guess that went stale:

      1. **`package["modality"]`** — the Hub's own answer on the row (0.6.61+),
         placed from its engine table and never dependent on a second call;
      2. **the catalogue's `hub_modality`** — the Hub's per-model statement
         (0.6.54+, and what discovery used before 1 existed);
      3. **`_HUB_ENGINE_MODALITY`** — this repo's table, keyed on the engine name.
         Only for a Hub that says nothing: an older build, a catalogue read that
         failed, or a package the catalogue does not describe.

    `(None, None)` means nothing could place it, and the caller reports it by name
    rather than dropping it.
    """
    from_package = _package_stated_modality(package)
    if from_package is not None:
        return from_package, "package"
    from_catalog = _stated_hub_modality(facts)
    if from_catalog is not None:
        return from_catalog, "stated"
    from_engine = _HUB_ENGINE_MODALITY.get(str(package.get("engine") or ""))
    if from_engine is not None:
        return from_engine, "engine"
    return None, None


def _studio_hub(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    """Read the Hub's model packages, enriched with its catalogue's per-model facts.

    Deliberately NOT `/api/hub/capabilities`: in standalone packaging that
    endpoint builds its model rows from a GenStudio audit candidate which
    returns None unconditionally, so it advertises **zero models**. The
    authoritative sources are `/api/hub/model-packages` (readiness) and
    `/api/hub/catalog` (capabilities and reference contracts).
    """
    from ..fleet import LOCAL_ID, provider_id_for
    from ..hub import hub_base_url, hub_headers  # local import: avoids a cycle

    # `definition.base_url` before the local Hub, like every other strategy in
    # this file. It was missing here, and the omission was not harmless: a
    # controller added on the Fleet page declares **no** `base_url` credential
    # (its address lives on the definition), so `values` never carried one and
    # every remote location fell through to `hub_base_url()` — this Mac's own
    # Hub. Each location therefore discovered the *local* machine's models, and
    # a job routed to site B could be dispatched for a model only site A has.
    base = (
        (values.get("base_url") or "").strip()
        or getattr(definition, "base_url", "")
        or hub_base_url()
    )
    if not base:
        return DiscoveryResult(error="Studio Hub is not running (no hub-ready.json found)")

    # Which credential is this provider's. Only this Mac's own Hub has none
    # stored, so only there may the operator token stand in — see `hub_headers`.
    # Resolved from the **provider id**, never from the address: the address of a
    # remote location is whatever the owner typed on the Fleet page, and that is
    # exactly the thing this must not trust with this Mac's operator token.
    local = definition.id == provider_id_for(LOCAL_ID)

    try:
        response = httpx.get(
            f"{base.rstrip('/')}/api/hub/model-packages",
            headers=hub_headers(values.get("hub_token", ""), local=local),
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(error=f"network: {exc}")

    if response.status_code >= 400:
        return DiscoveryResult(error=f"HTTP {response.status_code}: {response.text[:180]}")

    payload = response.json() or {}
    packages = payload.get("packages") or payload.get("items") or payload.get("models") or []
    if not isinstance(packages, list):
        return DiscoveryResult(error="unexpected Hub model-packages payload")

    # What each model *is*, as opposed to whether it is installed. Best effort:
    # an empty map leaves every model's reference facts absent, which readers
    # report as unverified rather than as "this model cannot clone".
    catalog = _hub_catalog(base, values.get("hub_token", ""), local=local)
    catalogue_missing = catalog is None
    catalog = catalog or {}
    # The snapshot's per-model `controls`: the one place a model states *how* it takes a
    # delivery instruction. Read live on every discovery, never cached past it (§1).
    controls_by_model = _hub_controls(base, values.get("hub_token", ""), local=local) or {}

    models: list[DiscoveredModel] = []
    #: engine -> {count, model_ids} for packages this gateway cannot classify.
    unclassified: dict[str, dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        engine = str(package.get("engine") or "")
        model_id = str(package.get("model_id") or package.get("package_id") or "").strip()
        # The Hub states these per model, so read them rather than deriving
        # either from the engine name: Qwen3's Base tiers clone and its
        # CustomVoice tiers do not, and they share an engine.
        facts = catalog.get(model_id) or {}
        modality, modality_source = _hub_modality(package, facts)
        if modality is None:
            # **Not silently.** This is the one case the gateway genuinely cannot
            # place: a package nothing could answer for. Recorded with its engine
            # and model ids so the inventory can say "5 packages on engine
            # `video-mlx` were not recognised" instead of reporting no models at
            # all — and, when the Hub itself refused to place it, with **the Hub's
            # own sentence**, which names the allowlist a person has to edit.
            bucket = unclassified.setdefault(
                engine or "(no engine stated)",
                {"count": 0, "model_ids": [], "reason": None},
            )
            bucket["count"] += 1
            if model_id and len(bucket["model_ids"]) < 5:
                bucket["model_ids"].append(model_id)
            hub_reason = str(package.get("reason") or "").strip()
            if hub_reason and not bucket["reason"]:
                bucket["reason"] = hub_reason
            continue
        if not model_id:
            continue
        files_state = str(package.get("files_state") or "")
        raw: dict[str, Any] = {
            "package_id": package.get("package_id"),
            "engine": engine,
            # `files_verified` never means "ready": readiness combines
            # files, runtime, OS and chip. Surface the raw state and let
            # the operator see it rather than collapsing it to a boolean.
            "files_state": files_state,
            "dispatch_state": package.get("dispatch_state"),
            "revision": package.get("revision"),
            # Which reader placed this model — `package` is the Hub's own row
            # (0.6.61+), `stated` its per-model catalogue field, `engine` the
            # fallback table in this file — so a surprising classification is
            # traceable rather than a mystery.
            "modality_source": modality_source,
            # **The lane's dispatch contract, verbatim, when a studio published
            # one.** Hub 0.6.61 joins it onto the package row (schema
            # `studiohub.lane-contract.v1`) and adds its own `deadline_seconds`.
            # It is the model's own answer about how to run it — operation,
            # accepted parameters, artifact, terminal states including which one
            # means *uncertain* and whether the lane can discard, cancel, and
            # measured runtimes — so it is carried unread-but-available rather
            # than paraphrased into this file's vocabulary.
            "lane_contract": package.get("lane_contract"),
            # `model` for something a person can pick; `dependency` for a package
            # that only exists to serve another. The Hub's own answer to the
            # question this gateway otherwise infers from the catalogue.
            "package_type": package.get("type"),
        }
        if isinstance(package.get("supports_vision"), bool):
            raw["supports_vision"] = package["supports_vision"]
        # The Hub states these per model, so read them rather than deriving
        # either from the engine name: Qwen3's Base tiers clone and its
        # CustomVoice tiers do not, and they share an engine.
        if catalog:
            # Whether the Hub's catalogue describes this as a **model** at all.
            # A location's package list also holds the codecs and tokenizers that
            # other models load, and there is nothing in it to tell them apart —
            # every row reads `engine=voice-mlx, state=files_verified,
            # *_state=ready`. The catalogue lists models and omits the
            # companions, so it is the only thing that separates them. Recorded
            # only when the catalogue actually answered: a read that failed must
            # not come out as "no models here".
            raw["catalogued"] = bool(facts)
            # **The Hub's own verdict on what kind of thing this is** (C5/M13). The catalogue
            # states `type: "model"` or `type: "dependency"`, and the *package list* states the
            # same fact on its own rows. It used to be read and dropped — no reader anywhere —
            # so the guard that keeps tokenizers and codecs out of the picker rested entirely on
            # this second HTTP call succeeding, which is exactly the failure `models.py` records
            # as having happened once already. Carried under its own name so the withhold rule
            # can consult either source.
            raw["catalogue_type"] = facts.get("type")
            # **The quantisation, and the sentence saying where it came from** (C5/M14). The
            # owner's standing rule is that a model named without its quantisation cannot be
            # checked against what he has actually run — "not 'Z-Image-Turbo' but
            # 'Z-Image-Turbo 6B MLX 4-bit'". Every shipped manifest now states one, and states
            # `null` with a reason when the publisher names no precision, so this carries both
            # halves rather than a bare label.
            if "quantization" in facts:
                raw["quantization"] = facts.get("quantization")
                raw["quantization_source"] = facts.get("quantization_source")
        # The Hub's recommended request size for this model (TTS): the catalogue row states it,
        # a package row may too. Carried unread; `catalog._tts_characters` decides what it means.
        raw["maximum_input_characters"] = (facts or {}).get("maximum_input_characters") \
            or package.get("maximum_input_characters")
        hub_controls = controls_by_model.get(model_id) or controls_by_model.get(
            str(package.get("package_id") or "")) or {}
        if hub_controls:
            # Carried verbatim: the catalogue's `_tts_controls` reads the capability and the
            # guidance from here, and publishes the guidance unchanged.
            raw["hub_controls"] = hub_controls
        if facts:
            for field in ("image_modes", "reference_lane", "image_workers", "max_prompt_characters"):
                if field in facts:
                    raw[field] = facts[field]
            raw["capabilities"] = facts.get("capabilities") or []
            raw["reference_audio"] = facts.get("reference_audio") or {}
            raw["family"] = facts.get("family")
            # The Hub states vision support per model, read from its published flag, never the name.
            if isinstance(facts.get("supports_vision"), bool):
                raw["supports_vision"] = facts["supports_vision"]
            elif "vision" in (facts.get("capabilities") or []):
                raw["supports_vision"] = True
            # The Hub's own limit for a chat model, each value with the sentence
            # saying where it came from (`model/config.json …` vs `worker
            # default: …`). Carried so the catalogue publishes the model's number
            # rather than this gateway's curated flat default.
            raw["model_limits"] = facts.get("model_limits")
            # The Hub builds a package row's `lane_contract` from the studios'
            # catalogue rows, so a catalogue that carries one is the same fact —
            # read it when the package row had none (an older package list, or a
            # contract joined after this row was cached).
            if not isinstance(raw.get("lane_contract"), dict) and isinstance(
                facts.get("lane_contract"), dict
            ):
                raw["lane_contract"] = facts["lane_contract"]
            # **The preset table the Hub renders with** (A6/H3). The Hub publishes its own
            # `image_sizes` — the pixel dimensions behind each aspect-ratio name — and this
            # gateway is the side that tells Story Studio *the geometry the Macs actually
            # render*. Without this carry, `catalog._hub_image_sizes` had to fall back on a copy
            # of the table taken from the Hub's installed bundle, so a Hub that re-sized a
            # preset would be described by the old number. Carried unread, like every other Hub
            # fact here; the catalogue decides what it means.
            if "image_sizes" in facts:
                raw["image_sizes"] = facts["image_sizes"]
            # Whether the Hub takes a **complete script** for this model and
            # sections it itself, and how. Read, never derived: `null` means it
            # does not long-form split this model at all, which is exactly the set
            # whose engine has a hard character cliff. It is also the gate that
            # lets a recorded cap stop applying by itself if the Hub gains
            # long-form delivery for that family.
            raw["long_form_delivery"] = facts.get("long_form_delivery")
            # The floor *and* the Hub's own verdict for the machine it runs on,
            # so "too big for this location" is read rather than computed here —
            # and it is that location's verdict, not this Mac's, which matters
            # when the same model is offered by several sites.
            raw["min_unified_memory_gb"] = facts.get("min_unified_memory_gb")
            raw["memory_eligible"] = facts.get("memory_eligible")
            # **Whether any Mac at this site can run it**, from the Hub's per-Mac rows. The
            # package row above is the controller's own disk only, so a model a joined node has
            # installed read `not_installed` for the whole group (1500 and Fish S2 Pro on 1504,
            # 2026-10-04). `None` when the catalogue states no install fact (an older Hub).
            raw["ready_on_a_mac"] = (
                mac_ready(facts) if isinstance(facts.get("hub_cached"), bool) else None
            )
            # Whether a video clip carries its own audio track: the worker measured it, and a
            # definite `true` is the one thing that lets a caller know the finished episode will
            # discard it. Tri-state, like every other unstated fact — absent stays None.
            raw["generates_audio"] = facts.get("generates_audio")
            # A vision model's analysis tasks and whether it is the Hub's default for them (0.6.166).
            for key in ("tasks", "default"):
                if key in facts:
                    raw[key] = facts[key]
        models.append(
            DiscoveredModel(
                model_id=model_id,
                display_name=_clean(
                    str(package.get("display_name") or ""),
                    # Local model ids are long HuggingFace repo strings; the
                    # short tail is far more readable in a picker.
                    model_id.split("/")[-1],
                ),
                modality=modality,
                raw=raw,
            )
        )
    return DiscoveryResult(
        models=models,
        catalogue_missing=catalogue_missing,
        skipped=[
            {
                "engine": engine,
                "count": bucket["count"],
                "model_ids": bucket["model_ids"],
                # The Hub's own sentence when it refused to place the package (it
                # names the engine allowlist to edit); ours only when no statement
                # existed at all.
                "reason": bucket["reason"] or (
                    "this gateway has no lane for it: the package states no modality "
                    "and its engine is not in the engine table"
                ),
            }
            for engine, bucket in sorted(unclassified.items())
        ],
    )


_STRATEGIES = {
    DISCOVERY_OPENAI_MODELS: _openai_models,
    DISCOVERY_ANTHROPIC_MODELS: _anthropic_models,
    DISCOVERY_GOOGLE_MODELS: _google_models,
    DISCOVERY_FAL_CATALOG: _fal_catalog,
    DISCOVERY_ELEVENLABS: _elevenlabs_models,
    DISCOVERY_STUDIO_HUB: _studio_hub,
}


def discover(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    """Fetch the model list for one provider. Never raises."""
    if definition.discovery == DISCOVERY_STATIC:
        return DiscoveryResult(
            models=[
                DiscoveredModel(
                    model_id=str(m["id"]),
                    display_name=_clean(str(m.get("display_name", "")), str(m["id"])),
                    modality=str(m.get("modality") or definition.modalities[0]),
                    raw={"curated": True},
                )
                for m in definition.static_models
            ]
        )

    strategy = _STRATEGIES.get(definition.discovery)
    if strategy is None:
        return DiscoveryResult(error=f"no discovery strategy for '{definition.discovery}'")

    try:
        return strategy(definition, values)
    except Exception as exc:  # noqa: BLE001 - a provider must never break the gateway
        return DiscoveryResult(error=f"unexpected: {type(exc).__name__}: {exc}")
