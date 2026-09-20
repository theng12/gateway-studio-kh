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

import time
from datetime import datetime, timezone
from typing import Any

from . import limits
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
    max_prompt_characters, max_prompt_source = _limit_with_source(
        "max_prompt_characters", None, 4000, definition.id, model_id
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
        # ponytail: `true` for every image model. Ceiling — a model with no seed
        # parameter is offered a control it rejects. It is `true` because both
        # lanes we serve *do* accept one: the studio_hub adapter passes `seed`
        # through explicitly (see `_generate_image_or_voice`), and the curated
        # cloud image models take one. A deliberate over-offer rather than an
        # invented figure. Upgrade path — read it per model where a provider
        # states it.
        "supports_seed": True,
        "min_reference_images": 1 if definition.kind == "studio_hub" else 0,
        "max_reference_images": 4 if definition.kind == "studio_hub" else 0,
        "reference_image_max_dimension": 2048,
        # ponytail: flat prompt cap and ratio list for every image model. Ceiling
        # — the previous GenStudio published 4,000 on some models and 4,800 on
        # others, and its ratio lists varied; a flat 4,000 is deliberately the
        # *conservative* end (it warns early rather than letting a request fail
        # at the provider, which is the violation Story Studio's own notes record
        # as "a 4,000-character cap overruling a published 4,800"). Upgrade path
        # — per-model values once a provider publishes them.
        "reference_strength_mode": "none",
        "supported_reference_strengths": [],
        "max_prompt_characters": max_prompt_characters,
        "max_prompt_characters_source": max_prompt_source,
        "operations": ["text-to-image", "image-to-image"],
        "requires_admission_permit": False,
        "availability_note": None,
    }


def _video_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    # Story Studio accepts a video variant ONLY when all six fields are present
    # and valid, so a partially-described variant is worse than none.
    #
    # ponytail: one flat variant list for every video model. Ceiling — no
    # provider here publishes durations or resolutions (fal and Kie give only a
    # `category` and an `endpoint_id`), and the previous GenStudio's contract had
    # them per model: Grok 4-15s at 480/720p, MiniMax 5-15s at 480p, HappyHorse
    # 4-15s at 720/1080p. Offering 10s@720p to a model that tops out at 5s@480p
    # fails at the provider, and it is only harmless today because the single
    # enabled video model (Gemini Omni, 4-10s@720p) happens to accept both.
    # Upgrade path — a curated per-model variant table, or read it from the
    # provider once one publishes it.
    #
    # `modes` and `generates_audio` are NOT part of that simplification, and
    # leaving them flat was a defect of the same kind as `supports_vision: false`:
    #
    #   * every video model was advertised as image-to-video only, while the
    #     curated list contains text-to-video endpoints whose own ids say so
    #     (`happyhorse-1-1/text-to-video`, `kling/v3-turbo-text-to-video`). A
    #     model that only takes text was offered a control it cannot honour, and
    #     one that takes text was not offered the control it needs;
    #   * `generates_audio: False` was a definite "no" for every model, and Google
    #     Veo 3 generates audio. Story Studio reads a definite false as "do not
    #     offer this", so the honest answer for a silent provider is `null`.
    raw_id = str(model.get("model_id") or model_id or "").lower()
    text_capable = "text-to-video" in raw_id or "t2v" in raw_id
    modes = ["text-to-video", "image-to-video"] if text_capable else ["image-to-video"]
    raw = model.get("raw") or {}
    stated_audio = raw.get("generates_audio") if isinstance(raw, dict) else None
    return {
        "modes": modes,
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "generates_audio": stated_audio if isinstance(stated_audio, bool) else None,
        "variants": [
            {
                "operation": mode,
                "duration_seconds": seconds,
                "resolution": "720p",
                "credits_per_video": 0,
                "minimum_credits": 0,
                "price_version": PRICE_VERSION,
            }
            for mode in modes
            for seconds in (5, 10)
        ],
    }


#: Where a published limit came from. The values are deliberately dull, and
#: `provider` is the only one that means "the model's own figure".
#:
#:   provider — the provider (or the Hub, for a location) stated it.
#:   curated  — the owner's `model-limits.local.json` filled a silence.
#:   gateway  — our own documented default or recorded table.
LIMIT_SOURCES = ("provider", "curated", "gateway")


def _limit_with_source(
    field: str, published: Any, fallback: int | None, provider_id: str, model_id: str
) -> tuple[Any, str]:
    """A limit and where it came from, decided **once**.

    Story Studio renders these fields as the model's own window or character
    budget, and it cannot tell a number the provider published from one this
    gateway chose. The owner's `model-limits.local.json` makes that concrete: its
    24 rows all carry the identical `1000000/128000`, which is a flat default
    wearing the clothes of per-model knowledge. The value is still published —
    nulling it is a breaking change for the reader, and Story Studio substitutes
    its own 8,192 when a window is absent, which is worse — but it now arrives
    with a companion `*_source` saying whose number it is.

    Value and provenance are returned together because they are one decision. A
    separate "where did that come from?" function is free to disagree with the
    chooser the first time either gains a step.
    """
    if published is not None:
        return published, "provider"
    curated = limits.gap(field, None, provider_id, model_id)
    if isinstance(curated, (int, float)) and not isinstance(curated, bool) and curated > 0:
        return int(curated), "curated"
    if fallback is None:
        return None, "gateway"
    return fallback, "gateway"


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
#:
#: ponytail: one flat default for every lane that states nothing. Ceiling — a
#: hard-capping engine is over-promised (Bark silences past 150 characters,
#: Orpheus past 170) and the request fails at the far end rather than being
#: refused here. Acceptable today only because those families are not exposed;
#: it becomes wrong the moment one is enabled locally. Upgrade path — read the
#: Hub's own per-model `TextGuidance` when it is served.
_TTS_CHARACTERS_DEFAULT = 25000

#: What every **Studio Hub location** publishes, whatever engine is behind it.
#:
#: The owner's rule, and it is a *presentation* decision rather than a measurement:
#: *"we do internal chunking on studio hub for the voices generate picking a safe
#: sentence end… i want the totality of it to be presenting 15000 as the max tts.
#: this way we can allow for longer generations from the story studio. it doesn't
#: need to know what we do backend."*
#:
#: So the gateway stops trying to mirror a per-engine limit and states one number
#: for the whole local lane. The Hub is what makes that honest: it takes the
#: complete script and renders sentence-safe sections, so a 15,000-character
#: request is not 15,000 characters handed to any one engine. Story Studio is told
#: what it may submit, not how the work is cut up.
#:
#: This replaces reading the Hub's own `execution_contract.text_max_characters`
#: (which is audited per model and ranges 10,000–40,000 across the installed set).
#: Mirroring it was correct in principle and wrong in effect: it made the picker's
#: limit vary per engine for a difference the caller cannot act on and should never
#: have to know about.
#:
#: ponytail: one flat number for every location. Ceiling — it is deliberately below
#: what the audited engines can do (40,000), so a long script is refused here that
#: the Hub could have sectioned. Upgrade path — raise it once the Hub's own
#: long-form work has a measured ceiling for a 15,000-character script end to end.
_TTS_CHARACTERS_LOCAL = 15000

#: Engines the Hub declares as `chunking="hard-cap"`, with the character cliff its
#: own catalogue records.
#:
#: **Read from the Hub, not guessed, and not served over its API.** `/api/hub/catalog`
#: carries `family`, `capabilities`, `reference_audio` and `long_form_delivery`
#: (including a per-model `section_max_characters` for everything it *does*
#: section) but **no** `text_guidance` — so `soft_max_chars` is the one fact that
#: has to be recorded here.
#:
#: **Read the copy in the installed bundle, and check its version.** These two
#: numbers come from
#: `~/Applications/Studio Hub.app/Contents/Resources/voice/app/backend/catalog.py`
#: at **0.6.31**, read 2026-09-20: bark 150, orpheus 170, both `chunking="hard-cap"`
#: — the only two in the file (a direct count, not an impression). The bundle is
#: the copy that matters; `~/Projects/studiohub-standalone` is a development
#: checkout and can be **ahead of what is serving** (it was 0.6.31 while the running
#: Hub still reported 0.6.29), so a fact read there is only verified once the
#: bundle's own file agrees.
#:
#: The Hub's field comment states the intent plainly: *"`hard-cap` — fixed
#: training-window past which output silences / hallucinates (Bark, Orpheus,
#: XTTS). **UI should soft-block.**"* Publishing this as `maximum_input_characters`
#: is that instruction carried out, not a number of ours. Its own wording is
#: approximate ("~170"); it is used as a block threshold because the Hub says to.
#:
#: `spark-tts-mlx` is 750 there and deliberately **absent**: it is `auto-split`,
#: so 750 is where quality is best, not a limit on what may be sent — publishing
#: it as a maximum would refuse text that works.
#:
#: ponytail: two families, hand-listed, and only consulted while the Hub does not
#: long-form split that model (see `_hard_cap_characters`). Ceiling — a third
#: hard-cap family the Hub adds is over-promised until this list is extended. The
#: Hub's own comment names **XTTS** as a third hard-cap engine, and it is absent
#: here only because no shipped family for it exists yet; if one appears it lands
#: in this table before it can be picked. Upgrade path — the Hub serves
#: `text_guidance.soft_max_chars`, at which point this table and its staleness
#: both disappear.
_HARD_CAP_CHARACTERS = {"orpheus": 170, "bark": 150}


def _hard_cap_characters(raw: dict[str, Any]) -> int | None:
    """The character cliff for a model the Hub does **not** long-form split.

    `long_form_delivery` is the served half of this decision: when it is present
    the Hub takes a complete script for that model and sections it itself, so its
    section size is not a limit on what may be submitted. When it is `null` the
    script goes to the engine whole, and an engine with a hard cap silences past
    it.

    Gating on that field rather than on the family alone means the recorded cap
    **stops applying by itself** the day the Hub learns to section that family —
    the numbers go stale, the rule does not.
    """
    if raw.get("long_form_delivery"):
        return None
    return _HARD_CAP_CHARACTERS.get(str(raw.get("family") or "").strip().lower())


def _tts_characters(
    model_id: str, model: dict[str, Any], definition: ProviderDefinition
) -> tuple[int, str]:
    """One TTS text limit **and where it came from**, decided once.

    Returned together deliberately. The value and its provenance answer one
    question — "who chose this number?" — and two functions asking the same
    questions in the same order are free to disagree the first time one of them
    gains a step. That drift is the shape this codebase keeps finding.

    Order: what the provider states, then the owner's curated value, then the
    engine's own hard cap, then the flat 25,000 default. The curated step was
    missing once, even though `maximum_input_characters` is one of the four fields
    `limits.py` advertises and loads — so the owner could write a limit for a
    model and the *invented* default beat his own curated knowledge, which is the
    wrong way round.

    The hard cap is a **ceiling**, not a preference: past it the engine silences
    rather than refusing, so a curated value may be more conservative than the
    cliff but never past it. Before this, Orpheus published 25,000 while its engine
    stops at 170 — Story Studio validated against a number that was never true,
    which is the whole of Shape 3.
    """
    raw = model.get("raw") or {}
    for key in ("maximum_text_length_per_request", "max_characters_request_subscribed_user"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value), "provider"

    curated = limits.gap("maximum_input_characters", None, definition.id, model_id)
    curated = int(curated) if isinstance(curated, (int, float)) and curated > 0 else None

    cap = _hard_cap_characters(raw)
    if cap is not None and (curated is None or curated > cap):
        # Our own recorded number, standing in for a limit the Hub does not serve.
        return cap, "gateway"
    if curated:
        return curated, "curated"
    if definition.kind == "studio_hub":
        # A location, and the Hub sections long scripts, so the whole lane states
        # one number rather than an engine's own.
        return _TTS_CHARACTERS_LOCAL, "gateway"
    return _TTS_CHARACTERS_DEFAULT, "gateway"


#: Models that offer no speed control.
#:
#: ElevenLabs' model list states no speed field, so this cannot be discovered —
#: it is curated knowledge, carried as an explicit exception rather than a rule.
#: `eleven_v3` is the model the previous GenStudio published as speed-less (1-1)
#: while publishing 0.5-2 for every other lane.
#:
#: ponytail: a hand-curated exception list. Ceiling — any *other* model that
#: rejects a speed parameter is still advertised as supporting one, and the
#: control fails on use. Upgrade path — the Hub already declares per-engine
#: `TextGuidance` (soft_max_chars / chunking) that it does not serve over its
#: API; read per-model speed capability from whichever provider states it.
_TTS_NO_SPEED = {"eleven_v3"}


def _supports_cloning(definition: ProviderDefinition, raw: dict[str, Any]) -> bool:
    """Whether a model clones, as the provider states it.

    Read from the model's own `reference_audio` block when it has one. An
    explicit `declared: false` or `supported: false` is the only thing that
    withholds the claim — a model that says *nothing* keeps the per-kind default,
    because "unstated" must not silently become "cannot", which is how ten
    working voices disappear from a picker with no error to explain it.
    """
    reference = raw.get("reference_audio")
    if isinstance(reference, dict) and reference:
        if reference.get("declared") is False or reference.get("supported") is False:
            return False
        if reference.get("declared") is True or reference.get("supported") is True:
            return True
    return definition.kind in {"studio_hub", "fish", "elevenlabs"}


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
    maximum_input_characters, characters_source = _tts_characters(model_id, model, definition)
    return {
        "maximum_input_characters": maximum_input_characters,
        # Where that number came from. Story Studio renders the field as the
        # model's own figure, so "we chose this" has to be findable next to it —
        # a companion field rather than a comment, because the reader is a
        # program.
        "maximum_input_characters_source": characters_source,
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
        # Per model where the provider states it. The Hub publishes, for every
        # model it catalogues, whether a reference clip is `declared` and
        # `supported` — and Qwen3's CustomVoice and VoiceDesign tiers answer
        # `false`, because they use preset speakers and a described voice rather
        # than a recording. The per-kind rule this replaced claimed `true` for
        # them, which tells Story Studio to offer a voice picker with nothing in
        # it: the same "control that does nothing" the rest of this file removes.
        #
        # ponytail: the per-kind default still stands behind it for kinds that
        # publish nothing per model (fish, elevenlabs) — their cloning is a
        # provider fact today. Ceiling — a cloning-capable provider outside these
        # three is under-described. Upgrade path — the same read, once they state
        # it, which is how `maximum_input_characters` already works.
        "supports_voice_cloning": _supports_cloning(definition, raw),
        "preview_policy": "none",
        "generation_enabled": True,
        "queue_class": "direct",
        "supported_languages": languages[:40],
        # ponytail: a conservative concurrency of 1 for every TTS lane. Ceiling —
        # a provider that accepts several submissions at once is under-used.
        # Deliberately the safe direction: over-scheduling is what makes a provider
        # reject work, and nothing here publishes its real limit. Upgrade path —
        # per-provider values once stated.
        "provider_submit_concurrency": 1,
        # ponytail: one invented temperature range for every TTS model that takes
        # one. Ceiling — a model with a narrower or wider range is described
        # wrongly, and the control fails at the provider outside its real bounds.
        # The local MLX engines and Fish do accept a temperature, so the knob is
        # real rather than decorative; only its range is assumed. Upgrade path —
        # read it per model, and drop the control entirely where a provider states
        # no `temperature` parameter.
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
        # ponytail: a conservative worker count of 1 for every transcription lane.
        # Ceiling — a provider that accepts several uploads at once is under-used,
        # and nothing here publishes its real limit. Deliberately the safe
        # direction, because over-scheduling is what makes a provider reject work.
        # Upgrade path — per-provider values once stated.
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
    def _stated(value: Any) -> int | None:
        """The number the provider published, including a published `0`.

        This deliberately does **not** filter out zero. `0` here is a claim — a
        model that accepts no context — and collapsing it to `None` made the
        curated file's figure publish as though the provider had stated it, which
        is the attribution rule this whole section is about. A missing or
        non-numeric value is the only thing that means "it did not say".
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

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
    #
    # A provider that states no `supported_parameters` at all keeps both controls.
    # That is a deliberate over-offer, not an invented measurement: temperature is
    # near-universal for chat, and `max_output_tokens` is enforced by this gateway
    # rather than by the provider, so a model that never mentioned either still
    # accepts both. Gating them on a silent provider would hide working controls;
    # `max_output_tokens` is therefore kept even when the list is present but lacks
    # it, for the same reason.
    controls = ["temperature", "max_output_tokens"]
    if parameters is not None:
        controls = [c for c in controls if c == "max_output_tokens" or c in parameters]

    structured = _supports("structured_outputs", "response_format")
    # Where the provider published nothing, the owner's curated file may know.
    # Gap-filling only: a live provider value always wins, because a file goes
    # stale and a provider does not.
    context_window, context_source = _limit_with_source(
        "context_window_tokens", _stated(context), None, definition.id, model_id
    )
    max_output, output_source = _limit_with_source(
        "max_output_tokens", _stated(completion), None, definition.id, model_id
    )
    return {
        "queue_class": "direct",
        "context_window_tokens": context_window,
        "context_window_tokens_source": context_source,
        "max_output_tokens": max_output,
        "max_output_tokens_source": output_source,
        "controls": controls,
        "input_modalities": inputs,
        "supports_vision": ("image" in inputs) if inputs else None,
        "supports_structured_output": structured,
        "structured_output_mode": "json_object" if structured else None,
        "supports_web_search": _supports("web_search", "web_search_options"),
        # Neither of these is stated by any provider here, and a definite `false`
        # is not the same statement as "unknown": Story Studio reads
        # `supports_vision === true` to decide whether to offer a control at all,
        # and the same shape applies to these. `returns_citations: False` was
        # published for every model — including ones whose provider states web
        # search — so "none" and "could not work it out" shared a value, which is
        # the rule this whole section exists to enforce.
        "returns_citations": None,
    }


def _music_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    """A music lane row.

    `supported_languages: ["en"]` and `supports_instrumental: True` were published
    for every music model where nothing states either — byte-for-byte the bug
    already fixed in `_tts_row`, which now publishes an empty list for a provider
    that does not enumerate its languages. English-only is a claim about Suno and
    Lyria that nobody made, and an instrumental toggle offered against a model
    that cannot do it fails on use.
    """
    raw = model.get("raw") or {}
    languages = _language_codes(raw.get("languages")) if isinstance(raw, dict) else []
    instrumental = raw.get("supports_instrumental") if isinstance(raw, dict) else None
    max_prompt_characters, max_prompt_source = _limit_with_source(
        "max_prompt_characters", None, 2000, definition.id, model_id
    )
    return {
        "coming_soon": False,
        # ponytail: flat caps for every music model, deliberately at the
        # conservative end — the same trade the image lane records at
        # `max_prompt_characters`. Upgrade path — per-model values from the
        # provider, or the owner's curated file (which is now actually read; it
        # advertised `max_prompt_characters` and nothing consulted it).
        "max_prompt_characters": max_prompt_characters,
        "max_prompt_characters_source": max_prompt_source,
        "max_lyrics_characters": 4000,
        "max_title_characters": 200,
        "supports_instrumental": instrumental if isinstance(instrumental, bool) else None,
        "supported_languages": languages,
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
        self._built_at = 0.0
        self._inventory_oldest: tuple[str, float] | None = None
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
        # One fleet read for the whole rebuild, not one per row: the catalogue has
        # to know which locations are answering so a merged row is locked or
        # offered truthfully.
        from . import fleet

        usable = fleet.usable_location_ids()
        contributed: dict[str, float] = {}
        for modality in MODALITIES:
            # One row per model, however many locations offer it. A location is a
            # provider internally — that is what gives it discovery, a credential
            # and its own enable toggle — but locations are not separate products.
            # The same model at six sites is one entry in the picker; publishing
            # six showed one display_name six times and broke a saved preset
            # whenever its site went offline.
            merged: dict[str, dict[str, Any]] = {}
            for definition, model in registry.models.enabled_models(modality):
                # Recorded while assembling, not recomputed when asked: the
                # freshness of the *inventory* is a property of the rows that were
                # built from it. One `fetched_at` per contributing provider.
                if definition.id not in contributed:
                    fetched = registry.models.cached(definition.id).get("fetched_at")
                    if isinstance(fetched, (int, float)) and fetched > 0:
                        contributed[definition.id] = float(fetched)
                row = self._story_row(modality, definition, model, usable=usable)
                existing = merged.get(row["id"])
                if existing is None:
                    merged[row["id"]] = row
                    continue
                # Locking means "no location offering this can take it", so one
                # answering location is enough to offer it.
                if not row["locked"]:
                    existing["locked"] = False
                    existing["lock_reason"] = None
                # Every location's previous site-scoped id stays an alias, so a
                # preset naming any of them still finds this one row.
                existing["legacy_ids"] = list(
                    dict.fromkeys([*existing["legacy_ids"], *row["legacy_ids"]])
                )
            rows[modality] = list(merged.values())

        self._rows = rows
        self._signature = signature
        self._built_at = time.time()
        # The oldest snapshot these rows were assembled from, or `None` when no
        # provider ever reported one — which is *unknown*, not zero.
        self._inventory_oldest = (
            min(contributed.items(), key=lambda item: item[1]) if contributed else None
        )

    def _story_row(
        self,
        modality: str,
        definition: ProviderDefinition,
        model: dict[str, Any],
        *,
        usable: set[str] | None = None,
    ) -> dict[str, Any]:
        from . import fleet

        is_location = fleet.is_location_provider(definition.id)
        # Published under the maker's namespace, never the site's.
        compound = encode_model_id(fleet.publish_id(definition.id), model["model_id"])
        # A local model whose Hub is not running is offered as LOCKED, not as
        # ready. This endpoint's own principle is that "offering a model that
        # fails on use is worse than not offering it" — and with Studio Hub down,
        # all twelve local rows were still published `locked: false`, so Story
        # Studio's picker showed twelve models that could only fail. Locking keeps
        # the row (so a saved preset does not read as `vanished`) while giving the
        # picker a reason to refuse it.
        #
        # For a location this asks the fleet and not this process: this Mac being
        # up says nothing about a site across town. `rebuild` merges the rows, so
        # the published row unlocks when any location offering it is answering.
        locked, lock_reason = False, None
        if is_location:
            location_id = fleet.controller_id_from(definition.id) or ""
            answering = usable if usable is not None else fleet.usable_location_ids()
            if location_id not in answering:
                locked = True
                lock_reason = "No Studio Hub location offering this model is answering."
        description = definition.description
        if is_location and definition.id != fleet.provider_id_for(fleet.LOCAL_ID):
            # A controller's description names its address, which is precisely the
            # fact Story Studio must never be handed.
            description = f"{fleet.FLEET_DISPLAY_NAME} location."
        row: dict[str, Any] = {
            "id": compound,
            "display_name": model["display_name"],
            "description": description,
            # Identity aliases: without these a saved preset reads as `vanished`
            # and the picker prints a "no longer offered" sentence.
            "customer_handle": compound,
            "public_model_id": compound,
            # The id this model was published under *before* every location
            # collapsed into one namespace, so a preset saved back then still
            # resolves. Story Studio matches a stored selection against the id,
            # `customer_handle`, `public_model_id` **and every legacy id**, which
            # is precisely what keeps an older preset from reading as `vanished`.
            # `rebuild` unions these across locations.
            "legacy_ids": (
                [encode_model_id(definition.id, model["model_id"])] if is_location else []
            ),
            # Publish availability EXPLICITLY. The transcription parser reads
            # `available === true` (absent means unavailable) while reading
            # `qualification_only !== false` (absent means hidden). Both
            # defaults are unsafe to rely on.
            "available": True,
            "qualification_only": False,
            "locked": locked,
            "lock_reason": lock_reason,
        }

        # A location's rows are attributed to the maker, never the site. The old
        # GenStudio's own migration for this is named
        # `0338_kokoro_names_the_maker_not_the_machines`.
        maker = fleet.FLEET_DISPLAY_NAME if is_location else definition.display_name
        provider_field = _PROVIDER_FIELD.get(modality)
        if provider_field:
            row[provider_field] = maker
        alias = _PROVIDER_ALIAS.get(modality)
        if alias:
            row[alias] = maker
        if modality == "tts":
            # The *published* namespace, so a TTS row never carries a site id
            # Story Studio could echo back as a model it cannot resolve.
            row["provider_id"] = fleet.publish_id(definition.id)

        builder = _ROW_BUILDERS.get(modality)
        if builder is not None:
            row.update(builder(model["model_id"], definition, model))

        # Price constants merged LAST so they always win and stay consistent.
        row.update(zero_pricing(modality))
        return row

    # -- reads ------------------------------------------------------------
    def rows(self, modality: str) -> list[dict[str, Any]]:
        self.rebuild()
        return self._rows.get(modality, [])


    def all_rows(self) -> list[dict[str, Any]]:
        self.rebuild()
        out: list[dict[str, Any]] = []
        for modality in MODALITIES:
            out.extend(self._rows[modality])
        return out

    def freshness(self) -> dict[str, Any]:
        """How fresh this catalogue is — which is **two different questions**.

        `ageMs` is how long ago the rows were last *assembled*, and it can never
        exceed `REBUILD_TTL_S`: `rebuild()` sets `_built_at` and this calls
        `rebuild()` first, so `ageMs ≈ 0` and `stale` is unreachable by
        construction. That is not a bug to fix by making the gate fire — every
        model would be disabled the moment a build aged past 30 minutes — but it
        does mean `stale` is not the fact it appears to be, and a reader that
        trusted it was trusting an artifact of being asked.

        `inventoryAgeMs` is the honest one: how old the **oldest provider snapshot**
        these rows were assembled from is. `rebuild()` runs no discovery — it
        re-sorts `models_cache.json` — so a row can be assembled now from a model
        list read days ago. `stale` is left exactly as it was, deliberately: this
        adds the fact that was missing rather than silently changing a gate Story
        Studio reads. `null` means no provider ever reported a fetch time, which is
        *unknown*, not zero.
        """
        self.rebuild()
        age_ms = max(0, int((time.time() - self._built_at) * 1000))
        oldest = self._inventory_oldest
        inventory_age_ms = (
            max(0, int((time.time() - oldest[1]) * 1000)) if oldest else None
        )
        return {
            "stale": age_ms > STALE_AFTER_MS,
            "ageMs": age_ms,
            "loadedAt": datetime.fromtimestamp(self._built_at, tz=timezone.utc).isoformat(),
            "inventoryAgeMs": inventory_age_ms,
            "inventoryOldestProvider": oldest[0] if oldest else None,
            "inventoryOldestAt": (
                datetime.fromtimestamp(oldest[1], tz=timezone.utc).isoformat()
                if oldest
                else None
            ),
        }

    def envelope(self, modality: str) -> dict[str, Any]:
        return {"models": self.rows(modality), "freshness": self.freshness()}

    def resolve(
        self,
        modality: str,
        compound: str,
        *,
        only_location: str | None = None,
    ) -> tuple[ProviderDefinition | None, dict | None]:
        """Resolve a namespaced model id against currently enabled models.

        Never trust the caller's id: resolution is scoped to enabled providers
        and enabled models so a disabled toggle is honoured on the generation
        path, not just in the catalogue.

        A `studio-hub::` id names the maker, not a site, so resolving it is also
        where the location is chosen. `only_location` pins that choice — it is how
        a voice job is kept at the location that actually holds the voice, since a
        reference clip lives on one Hub and not on the fleet.
        """
        from . import fleet

        provider_id, model_id = decode_model_id(compound)
        if provider_id == fleet.FLEET_PROVIDER_ID:
            chosen, _note = fleet.route(
                model_id, modality, routing_key=compound, only_location=only_location
            )
            if chosen is None:
                return None, None
            # Return the *chosen location's* definition: it carries the address the
            # job is sent to, while the job keeps the published id so the id Story
            # Studio is told back is the id it sent.
            for definition, model in registry.models.enabled_models(modality):
                if definition.id == chosen.id and model["model_id"] == model_id:
                    return definition, model
            return None, None
        for definition, model in registry.models.enabled_models(modality):
            if definition.id == provider_id and model["model_id"] == model_id:
                return definition, model
        return None, None


catalog = Catalog()
