"""Catalogue assembly — live registry models become Story Studio KH rows.

Replaces the Phase 0 static catalogue. Rows are now generated from whatever the
provider registry reports as enabled, so enabling a provider or a model in the
admin UI is immediately visible to Story Studio without editing a file.

Two jobs, both load-bearing:

1. Merge the zero-valued price constants into every row, so no row is ever
   dropped by Story Studio's parsers for a missing price field. See
   `constants.py` for why a gateway with no billing must still publish prices.

2. Report `freshness` accurately. It is a **hard usability gate, not
   decoration**: `stale: true` disables every model for new work, and a copy
   older than 30 minutes is refused outright with paid work blocked. A static
   catalogue that reported "seconds since process start" would stop working 31
   minutes after boot, so this reports the age of the last rebuild and rebuilds
   often.

Model ids are namespaced as `provider::model`. Two providers can legitimately
offer the same underlying model (`flux-pro` from both fal and BFL), and an
unnamespaced id would make resolution ambiguous and saved presets unstable.
Story Studio treats the id as opaque and displays `display_name`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import VOICES_PATH
from .constants import PRICE_VERSION, zero_pricing
from .providers import registry
from .providers.definitions import ProviderDefinition

ID_SEPARATOR = "::"
REBUILD_TTL_S = 20.0
STALE_AFTER_MS = 30 * 60 * 1000

MODALITIES = ("chat", "image", "video", "tts", "transcription", "music")

# Which field carries the serving provider, per modality.
#
# Story Studio's own handoff states the contract plainly: "GenStudio publishes
# `provider_name` for all five model kinds". So **every** kind now publishes
# `provider_name` — video previously published nothing at all, and transcription
# published only `provider`, which meant that when Story Studio added the
# declaration for its transcription and video model types the field would have
# been missing rather than merely unused.
#
# `provider` is kept as well where it was already published (transcription
# models, and voice rows, which are built elsewhere) because a field that
# something may read is not worth removing to tidy up: extra keys are ignored,
# a missing one reads as blank.
_PROVIDER_FIELD = {
    "chat": "provider_name",
    "image": "provider_name",
    "tts": "provider_name",
    "video": "provider_name",
    "transcription": "provider_name",
    "music": "provider_name",
}

#: Extra aliases published alongside `provider_name`, for readers that already
#: expected this spelling.
_PROVIDER_ALIAS = {
    "transcription": "provider",
}

_IMAGE_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]


def encode_model_id(provider_id: str, model_id: str) -> str:
    return f"{provider_id}{ID_SEPARATOR}{model_id}"


def decode_model_id(compound: str) -> tuple[str, str]:
    """Split a namespaced id. A bare id resolves to ('', id)."""
    if ID_SEPARATOR in compound:
        provider_id, _, model_id = compound.partition(ID_SEPARATOR)
        return provider_id, model_id
    return "", compound


def _geometry(ratio: str) -> dict[str, int]:
    return {
        "16:9": {"width": 1344, "height": 768},
        "9:16": {"width": 768, "height": 1344},
        "1:1": {"width": 1024, "height": 1024},
        "4:3": {"width": 1152, "height": 864},
        "3:4": {"width": 864, "height": 1152},
    }.get(ratio, {"width": 1344, "height": 768})


def _image_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    options = []
    for index, ratio in enumerate(_IMAGE_RATIOS):
        geometry = _geometry(ratio)
        options.append(
            {
                "resolution": "1K",
                "aspect_ratio": ratio,
                "width": geometry["width"],
                "height": geometry["height"],
                "is_default": index == 0,
                "credits_per_image": 0,
                "base_credits_per_image": 0,
                "price_version": PRICE_VERSION,
                "reference_generation_credits": 0,
                "reference_generation_price_version": PRICE_VERSION,
            }
        )
    return {
        "execution_label": "Local" if definition.kind == "studio_hub" else "Remote",
        "aspect_ratios": _IMAGE_RATIOS,
        "default_aspect_ratio": "16:9",
        "default_resolution": "1K",
        "output_options": options,
        # Reference-image support is advertised so the UI offers it; a provider
        # that cannot do it will reject the request, which is recoverable,
        # whereas a hidden control is not.
        "supports_reference_images": definition.kind == "studio_hub",
        "supports_image_to_image": definition.kind == "studio_hub",
        "supports_seed": True,
        "min_reference_images": 1 if definition.kind == "studio_hub" else 0,
        "max_reference_images": 4 if definition.kind == "studio_hub" else 0,
        "reference_image_max_dimension": 2048,
        "reference_strength_mode": "none",
        "supported_reference_strengths": [],
        "max_prompt_characters": 4000,
        "operations": ["text-to-image", "image-to-image"],
        "requires_admission_permit": False,
        "availability_note": None,
    }


def _video_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    # Story Studio accepts a video variant ONLY when all six fields are present
    # and valid, so a partially-described variant is worse than none.
    return {
        "modes": ["image-to-video"],
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "generates_audio": False,
        "variants": [
            {
                "operation": "image-to-video",
                "duration_seconds": 5,
                "resolution": "720p",
                "credits_per_video": 0,
                "minimum_credits": 0,
                "price_version": PRICE_VERSION,
            },
            {
                "operation": "image-to-video",
                "duration_seconds": 10,
                "resolution": "720p",
                "credits_per_video": 0,
                "minimum_credits": 0,
                "price_version": PRICE_VERSION,
            },
        ],
    }


def _language_codes(languages: Any) -> list[str]:
    """Normalise a provider's language list into plain codes.

    ElevenLabs publishes 32 languages as objects — `{"language_id": "af",
    "name": "Afrikaans"}` — and this used to be flattened with `str(x)`, which
    published the Python repr of a dict as the language name. Story Studio types
    the field as `string[]` and joins it into a customer-visible line, so the
    narration page read "Languages {'language_id': 'en', 'name': 'English'}, …".

    A provider that states a language as a bare string (Fish) already works.
    """
    codes: list[str] = []
    for entry in languages or []:
        if isinstance(entry, dict):
            value = entry.get("language_id") or entry.get("code") or entry.get("name")
        else:
            value = entry
        text = str(value or "").strip()
        if text and text not in codes:
            codes.append(text)
    return codes


#: Per-request text limit for a lane that does not publish one, in characters.
#:
#: **25,000**, and the local lane is why that is safe rather than optimistic.
#: A local model does not hard-cap: the Hub's voice catalog declares our families
#: (`qwen3-tts`, `voxcpm-mlx`, `omnivoice`) as `soft_max_chars: None` with
#: `chunking: "auto-split"` — it splits long text at sentence boundaries, reuses
#: the reference for every section, then verifies and joins the audio. Story
#: Studio never has to chunk anything itself, and neither does this gateway.
#:
#: The families that *do* hard-cap are the ones we do not expose: Bark at 150
#: characters, Orpheus at 170, Spark at 750. If one is ever enabled locally, this
#: needs to become per model — the Hub already publishes the distinction, it just
#: does not serve it over its API yet.
#:
#: A provider that states its own limit always wins: ElevenLabs publishes
#: `maximum_text_length_per_request` per model (5,000 / 10,000 / 30,000 / 40,000).
_TTS_CHARACTERS_DEFAULT = 25000


def _tts_characters(model: dict[str, Any], definition: ProviderDefinition) -> int:
    raw = model.get("raw") or {}
    for key in ("maximum_text_length_per_request", "max_characters_request_subscribed_user"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return _TTS_CHARACTERS_DEFAULT


#: Models that offer no speed control.
#:
#: ElevenLabs' model list states no speed field, so this cannot be discovered —
#: it is curated knowledge, carried as an explicit exception rather than a rule.
#: `eleven_v3` is the model the previous GenStudio published as speed-less (1-1)
#: while publishing 0.5-2 for every other lane.
_TTS_NO_SPEED = {"eleven_v3"}


def _tts_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("raw") or {}
    supports_speed = model_id not in _TTS_NO_SPEED
    minimum_speed = 0.5 if supports_speed else 1.0
    maximum_speed = 2.0 if supports_speed else 1.0
    # Empty when the provider states none. It used to fall back to `["en"]`,
    # which claims a specific language — false for engines like Qwen3 and
    # VoxCPM2 that are not English-only. Story Studio renders this list only
    # when it is non-empty, so an empty list shows nothing rather than a lie.
    languages = _language_codes(raw.get("languages"))
    return {
        "maximum_input_characters": _tts_characters(model, definition),
        # Speed is per model, not per provider: eleven_v3 publishes no speed
        # control at all, and fal's turbo lane allows 0.7-1.2 where the direct
        # one allows 0.5-2. `controls` reflects that rather than claiming a
        # range every model would reject.
        #
        # The local lane is different in kind, not in truth: these engines take no
        # speed parameter, and the Hub slows the finished audio instead. The
        # adapter passes `speed` through either way, so the control is real — the
        # work just happens after generation rather than during it.
        "supports_speed": supports_speed,
        "minimum_speed": minimum_speed,
        "maximum_speed": maximum_speed,
        "supports_voice_cloning": definition.kind in {"studio_hub", "fish", "elevenlabs"},
        "preview_policy": "none",
        "generation_enabled": True,
        "queue_class": "direct",
        "supported_languages": languages[:40],
        "provider_submit_concurrency": 1,
        "controls": [
            {
                "name": "temperature",
                "label": "Temperature",
                "type": "number",
                "minimum": 0.1,
                "maximum": 2.0,
                "default": 0.7,
                "step": 0.05,
            }
        ],
    }


def _transcription_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    """Transcription capability.

    This row used to publish `language: "en"` with `supported_languages: ["en"]`
    **and** `supports_language_auto_detect: true` — a contradiction on its face
    (a model that only knows English cannot detect the language) and false for
    these models, which are MLX Whisper: multilingual, and auto-detecting by
    construction.

    Nothing in this stack publishes a language list — not the Hub's model
    packages, not its capabilities route (its per-worker `models` array is empty
    in standalone packaging), not the model manifest. So rather than enumerate a
    list we cannot support, this publishes Story Studio's own sentinel for "many,
    unenumerated": `language: "multilingual"` with an empty `supported_languages`.

    Empty rather than absent on purpose: Story Studio's rule is that a model
    publishing neither a concrete language nor auto-detect "has no valid
    submission state", so dropping the row to nothing would be worse than the
    bug being fixed.
    """
    return {
        "language": "multilingual",
        "supported_languages": [],
        "supports_language_auto_detect": True,
        "available_workers": 1,
    }


def _chat_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("raw") or {}
    # Providers spell the same two figures differently: OpenRouter uses
    # `context_length` / `top_provider.max_completion_tokens`, Google uses
    # `inputTokenLimit` / `outputTokenLimit`. Read every spelling before
    # concluding a provider said nothing.
    context = (
        raw.get("context_length")
        or raw.get("context_window")
        or raw.get("inputTokenLimit")
        or raw.get("input_token_limit")
    )
    top = raw.get("top_provider") or {}
    completion = (
        top.get("max_completion_tokens")
        or raw.get("max_completion_tokens")
        or raw.get("outputTokenLimit")
        or raw.get("output_token_limit")
    )

    # Unknown is published as `null`, never as a number or a `false`.
    #
    # The context window used to fall back to `0` — which does not read as "not
    # published", it reads as *this model accepts nothing*. The output allowance
    # was hardcoded to 8192 for every model. And `supports_vision: false` with
    # `input_modalities: ["text"]` was published for every model too, while 273
    # of OpenRouter's 445 models accept image input — so the catalogue said no
    # model had vision, and Story Studio hid the control that would have used it.
    #
    # Story Studio's rule: never let "the answer is none" and "I could not work
    # out the answer" share a value. Its check is `supports_vision === true`, and
    # its own message for a missing value reads "does not publish image-input
    # support" — so `null` is what it already expects for "unstated".
    def _positive(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    architecture = raw.get("architecture")
    inputs = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    inputs = [str(x) for x in inputs] if isinstance(inputs, list) and inputs else None

    parameters = raw.get("supported_parameters")
    parameters = [str(x) for x in parameters] if isinstance(parameters, list) else None

    def _supports(*names: str) -> bool | None:
        """Whether the provider states this capability. `None` when it is silent."""
        if parameters is None:
            return None
        return any(name in parameters for name in names)

    # Only control names already published here are gated — adding new ones would
    # offer Story Studio knobs it has no rendering for.
    controls = ["temperature", "max_output_tokens"]
    if parameters is not None:
        controls = [c for c in controls if c == "max_output_tokens" or c in parameters]

    structured = _supports("structured_outputs", "response_format")
    return {
        "queue_class": "direct",
        "context_window_tokens": _positive(context),
        "max_output_tokens": _positive(completion),
        "controls": controls,
        "input_modalities": inputs,
        "supports_vision": ("image" in inputs) if inputs else None,
        "supports_structured_output": structured,
        "structured_output_mode": "json_object" if structured else None,
        "supports_web_search": _supports("web_search", "web_search_options"),
        "returns_citations": False,
    }


def _music_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    return {
        "coming_soon": False,
        "max_prompt_characters": 2000,
        "max_lyrics_characters": 4000,
        "max_title_characters": 200,
        "supports_instrumental": True,
        "supported_languages": ["en"],
    }


_ROW_BUILDERS = {
    "image": _image_row,
    "video": _video_row,
    "tts": _tts_row,
    "transcription": _transcription_row,
    "chat": _chat_row,
    "music": _music_row,
}


class Catalog:
    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {m: [] for m in MODALITIES}
        self._voices: list[dict[str, Any]] = []
        self._voice_refs: dict[str, dict[str, Any]] = {}
        self._built_at = 0.0
        self._signature: tuple = ()
        self.rebuild(force=True)

    # -- building ---------------------------------------------------------
    def _current_signature(self) -> tuple:
        """Cheap fingerprint of catalogue-affecting state.

        Rebuilding on a timer alone would leave a toggle invisible for up to
        `REBUILD_TTL_S`; rebuilding on every request would re-sort hundreds of
        models per poll. The signature gives immediacy with a cheap check.
        """
        parts: list[tuple] = []
        for definition in registry.definitions():
            if not registry.credentials.is_enabled(definition.id):
                continue
            if not registry.credentials.is_configured(definition.id):
                continue
            for model in registry.models.models_for(definition.id, include_disabled=False):
                parts.append((definition.id, model["model_id"], model["modality"]))
        return tuple(sorted(parts))

    def rebuild(self, *, force: bool = False) -> None:
        signature = self._current_signature()
        fresh = (time.time() - self._built_at) < REBUILD_TTL_S
        if not force and fresh and signature == self._signature:
            return

        rows: dict[str, list[dict[str, Any]]] = {m: [] for m in MODALITIES}
        for modality in MODALITIES:
            for definition, model in registry.models.enabled_models(modality):
                rows[modality].append(self._story_row(modality, definition, model))

        self._rows = rows
        self._voices, self._voice_refs = self._load_voices()
        self._signature = signature
        self._built_at = time.time()

    def _story_row(
        self, modality: str, definition: ProviderDefinition, model: dict[str, Any]
    ) -> dict[str, Any]:
        compound = encode_model_id(definition.id, model["model_id"])
        row: dict[str, Any] = {
            "id": compound,
            "display_name": model["display_name"],
            "description": definition.description,
            # Identity aliases: without these a saved preset reads as `vanished`
            # and the picker prints a "no longer offered" sentence.
            "customer_handle": compound,
            "public_model_id": compound,
            "legacy_ids": [],
            # Publish availability EXPLICITLY. The transcription parser reads
            # `available === true` (absent means unavailable) while reading
            # `qualification_only !== false` (absent means hidden). Both
            # defaults are unsafe to rely on.
            "available": True,
            "qualification_only": False,
            "locked": False,
            "lock_reason": None,
        }

        provider_field = _PROVIDER_FIELD.get(modality)
        if provider_field:
            row[provider_field] = definition.display_name
        alias = _PROVIDER_ALIAS.get(modality)
        if alias:
            row[alias] = definition.display_name
        if modality == "tts":
            row["provider_id"] = definition.id

        builder = _ROW_BUILDERS.get(modality)
        if builder is not None:
            row.update(builder(model["model_id"], definition, model))

        # Price constants merged LAST so they always win and stay consistent.
        row.update(zero_pricing(modality))
        return row

    def _load_voices(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Returns (Story-facing rows, private reference map).

        Reference audio and transcripts are local assets that Studio Hub needs
        for cloning. They are kept OUT of the Story row deliberately: Story
        Studio has no use for them, and leaking a local filesystem path through
        a catalogue response would be careless.
        """
        if not VOICES_PATH.exists():
            return [], {}
        try:
            raw = json.loads(VOICES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return [], []
        entries = raw.get("voices") if isinstance(raw, dict) else raw
        entries = [v for v in (entries or []) if isinstance(v, dict)]

        references: dict[str, dict[str, Any]] = {}
        for voice in entries:
            path = voice.get("reference_path")
            if path:
                references[str(voice["id"])] = {
                    "path": str(path),
                    "transcript": voice.get("reference_transcript") or "",
                    "segments": voice.get("reference_segments") or [],
                    "name": voice.get("display_name") or voice["id"],
                }
        return [self._story_voice(v) for v in entries], references

    def voice_reference(self, voice_id: str) -> dict[str, Any] | None:
        """Reference clip for a voice, for providers that need cloning input."""
        self.rebuild()
        return self._voice_refs.get(voice_id)

    def _story_voice(self, voice: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": voice["id"],
            "display_name": voice["display_name"],
            "language": voice.get("language", "en"),
            "description": voice.get("description", ""),
            "provider": voice.get("provider", ""),
            "engine_name": voice.get("engine_name", ""),
            "engine_id": voice.get("engine_id", ""),
            "model_family": voice.get("model_family", ""),
            "model_revision_id": voice.get("model_revision_id"),
            "voice_tier": voice.get("voice_tier", "standard"),
            "voice_revision": voice.get("voice_revision", 1),
            "custom_voice_id": voice.get("custom_voice_id"),
            "public_voice_id": voice.get("public_voice_id"),
            "credits_per_1000_characters": 0,
            "output_format": voice.get("output_format", "wav"),
            # MUST be exactly 'published'; Story Studio treats any other value
            # as a generation gate and silently hides the voice.
            "catalog_status": "published",
            "quote_enabled": True,
            "generation_enabled": True,
            "preview_available": voice.get("preview_available", False),
            "preview_url": voice.get("preview_url"),
            "preview_text": voice.get("preview_text"),
            "preview_notice": voice.get("preview_notice"),
            "is_saved": voice.get("is_saved", False),
            "tts_model_id": voice.get("tts_model_id", ""),
            "tts_model_name": voice.get("tts_model_name", ""),
        }

    # -- reads ------------------------------------------------------------
    def rows(self, modality: str) -> list[dict[str, Any]]:
        self.rebuild()
        return self._rows.get(modality, [])

    def voices(self) -> list[dict[str, Any]]:
        self.rebuild()
        return self._voices

    def all_rows(self) -> list[dict[str, Any]]:
        self.rebuild()
        out: list[dict[str, Any]] = []
        for modality in MODALITIES:
            out.extend(self._rows[modality])
        return out

    def freshness(self) -> dict[str, Any]:
        self.rebuild()
        age_ms = max(0, int((time.time() - self._built_at) * 1000))
        return {
            "stale": age_ms > STALE_AFTER_MS,
            "ageMs": age_ms,
            "loadedAt": datetime.fromtimestamp(self._built_at, tz=timezone.utc).isoformat(),
        }

    def envelope(self, modality: str) -> dict[str, Any]:
        return {"models": self.rows(modality), "freshness": self.freshness()}

    def resolve(self, modality: str, compound: str) -> tuple[ProviderDefinition | None, dict | None]:
        """Resolve a namespaced model id against currently enabled models.

        Never trust the caller's id: resolution is scoped to enabled providers
        and enabled models so a disabled toggle is honoured on the generation
        path, not just in the catalogue.
        """
        provider_id, model_id = decode_model_id(compound)
        for definition, model in registry.models.enabled_models(modality):
            if definition.id == provider_id and model["model_id"] == model_id:
                return definition, model
        return None, None


catalog = Catalog()
