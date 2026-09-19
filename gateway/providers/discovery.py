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


def infer_modality(model_id: str, definition: ProviderDefinition, hint: str = "") -> str:
    """Best-effort modality for a discovered model id.

    `hint` is a provider-supplied category (fal publishes one). It wins when it
    is a modality we actually serve.
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

    lowered = model_id.lower()
    for needle, modality in _MODALITY_RULES:
        if needle in lowered:
            return modality

    # Fall back to the provider's declared scope. A single-modality provider
    # (every language provider) is unambiguous here.
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
        modality = infer_modality(model_id, definition)
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
        if modality not in {"image", "video"}:
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
                # ElevenLabs states the real per-request text limit and whether
                # the model supports speed control at all — eleven_v3 does not.
                # Both were being discarded while the catalogue published one
                # invented number and `supports_speed: true` for every model.
                "maximum_text_length_per_request": e.get("maximum_text_length_per_request"),
                "max_characters_request_subscribed_user": e.get(
                    "max_characters_request_subscribed_user"
                ),
                "can_use_style": e.get("can_use_style"),
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


def _studio_hub(definition: ProviderDefinition, values: dict[str, str]) -> DiscoveryResult:
    """Read the Hub's model packages.

    Deliberately NOT `/api/hub/capabilities`: in standalone packaging that
    endpoint builds its model rows from a GenStudio audit candidate which
    returns None unconditionally, so it advertises **zero models**. The
    authoritative sources are `/api/hub/model-packages` (readiness) and
    `/api/hub/catalog`.
    """
    from ..hub import hub_base_url, hub_headers  # local import: avoids a cycle

    base = (values.get("base_url") or "").strip() or hub_base_url()
    if not base:
        return DiscoveryResult(error="Studio Hub is not running (no hub-ready.json found)")

    try:
        response = httpx.get(
            f"{base.rstrip('/')}/api/hub/model-packages",
            headers=hub_headers(values.get("hub_token", "")),
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
                raw={
                    "package_id": package.get("package_id"),
                    "engine": engine,
                    # `files_verified` never means "ready": readiness combines
                    # files, runtime, OS and chip. Surface the raw state and let
                    # the operator see it rather than collapsing it to a boolean.
                    "files_state": files_state,
                    "dispatch_state": package.get("dispatch_state"),
                    "revision": package.get("revision"),
                },
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
