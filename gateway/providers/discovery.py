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
                raw={
                    k: v
                    for k, v in entry.items()
                    if k in {"id", "name", "owned_by", "context_length", "pricing",
                             "top_provider", "architecture", "supported_parameters",
                             "context_window", "input_token_limit", "inputTokenLimit",
                             "max_completion_tokens", "output_token_limit",
                             "outputTokenLimit",
                             # The TTS character limit. `_tts_characters` reads
                             # both spellings, and the catalogue check that a
                             # provider's own limit wins could not see either.
                             "maximum_text_length_per_request",
                             "max_characters_request_subscribed_user"}
                },
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
            raw={"id": e.get("id"), "created_at": e.get("created_at")},
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
    return DiscoveryResult(models=models)


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
    "subtitle-mlx": "transcription",
}


def _hub_catalog(base: str, token: str, *, local: bool = False) -> dict[str, dict[str, Any]]:
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
    """
    from ..hub import hub_headers  # local import: avoids a cycle

    try:
        response = httpx.get(
            f"{base.rstrip('/')}/api/hub/catalog",
            headers=hub_headers(token, local=local),
            timeout=TIMEOUT,
        )
    except httpx.HTTPError:
        return {}
    if response.status_code >= 400:
        return {}
    payload = response.json() or {}
    rows = payload.get("models") or payload.get("items") or []
    if not isinstance(rows, list):
        return {}

    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("repo") or row.get("model_id") or "").strip()
        if key:
            indexed[key] = row
    return indexed


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

    models: list[DiscoveredModel] = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        engine = str(package.get("engine") or "")
        modality = _HUB_ENGINE_MODALITY.get(engine)
        if modality is None:
            continue
        model_id = str(package.get("model_id") or package.get("package_id") or "").strip()
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
        }
        # The Hub states these per model, so read them rather than deriving
        # either from the engine name: Qwen3's Base tiers clone and its
        # CustomVoice tiers do not, and they share an engine.
        facts = catalog.get(model_id) or {}
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
        if facts:
            raw["capabilities"] = facts.get("capabilities") or []
            raw["reference_audio"] = facts.get("reference_audio") or {}
            raw["family"] = facts.get("family")
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
    return DiscoveryResult(models=models)


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
