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

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import image_assignment
from . import limits
from . import pools
from . import video_capabilities
from . import voices
from .constants import PRICE_VERSION, zero_pricing
from .pricing import chargeable, row_price
from .providers import registry
from .providers.definitions import (
    HUB_IMAGE_SIZE_BY_RATIO,
    IMAGE_SIZE_BY_RATIO,
    KIND_ANTHROPIC,
    KIND_KIE,
    KIND_MINIMAX,
    KIND_STUDIO_HUB,
    KIND_XAI,
    ProviderDefinition,
    default_temperature_only,
    image_reference_limits,
    image_reference_state,
)

ID_SEPARATOR = "::"
REBUILD_TTL_S = 20.0
#: How often the background refresher checks the signature (a ~1 ms read); a full rebuild
#: still happens only on a change or after `REBUILD_TTL_S`.
REFRESH_EVERY_S = 1.0
STALE_AFTER_MS = 30 * 60 * 1000

MODALITIES = ("chat", "image", "video", "tts", "transcription", "music", "embeddings", "vision")

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
    "embeddings": "provider_name",
    "vision": "provider_name",
}

#: Extra aliases published alongside `provider_name`, for readers that already
#: expected this spelling.
_PROVIDER_ALIAS = {
    "transcription": "provider",
}

# Story Studio's legacy capability catalogue reads `GET /v1/models` and keeps a
# row only when `modality` and `operation` come from fixed vocabularies. This
# endpoint published 124 rows without them, so every row was dropped and Story's
# diagnostics reported an empty catalogue while the per-modality pickers (which
# use their own routes) worked. Publishing the two fields is additive; music has
# no operation in Story's vocabulary, so it stays out of that one view by design.
_CAPABILITY_MODALITY = {
    "chat": "text",
    "image": "image",
    "video": "video",
    "tts": "audio",
    "transcription": "audio",
    "music": "audio",
    "embeddings": "text",
    "vision": "image",
}
_CAPABILITY_OPERATION = {
    "chat": "chat.response",
    "image": "image.generation",
    "video": "video.generation",
    "tts": "voice.tts",
    "transcription": "audio.transcription",
    "music": "music.generate",
    # Not in Story's capability vocabulary, so its legacy reader drops the row; the dedicated
    # `/v1/embeddings/models` route and `/v1/embeddings` are how embeddings are used.
    "embeddings": "text.embedding",
    # Image analysis (Hub 0.6.166 `vision-mlx`): likewise outside Story's legacy vocabulary; use
    # `/v1/vision/models` and `/v1/vision/analyze`.
    "vision": "image.analysis",
}

#: The cloud fallback ratio list, and **only** that: a `studio_hub` row builds its own
#: from the lane contract (see `_local_image_shape`) because the lane that renders owns the
#: numbers. Kept exactly as it was for every other kind — see `IMAGE_SIZE_BY_RATIO`.
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
    """The published width/height for a ratio, from the **one** size table.

    `providers.definitions.IMAGE_SIZE_BY_RATIO` is what the OpenAI-compatible
    image body sends; publishing a different geometry here meant Story Studio was
    shown 1344x768 for 16:9 while 1792x1024 was sent — a size `gpt-image-1`
    rejects. What is offered is what is sent.
    """
    width, height = IMAGE_SIZE_BY_RATIO.get(ratio) or IMAGE_SIZE_BY_RATIO["16:9"]
    return {"width": width, "height": height}


#: The mode words a provider may state in `category`/`output_type` or in the
#: model id itself. Longest phrases first so `image-to-video` is not read as
#: `video-to-video`, and the short forms are matched as whole hyphen-separated
#: tokens.
_VIDEO_MODE_PHRASES: tuple[tuple[str, str], ...] = (
    ("reference-to-video", "reference-to-video"),
    ("text-to-video", "text-to-video"),
    ("image-to-video", "image-to-video"),
    ("video-to-video", "video-to-video"),
    ("extend-video", "video-to-video"),
    ("t2v", "text-to-video"),
    ("i2v", "image-to-video"),
    ("r2v", "reference-to-video"),
)


def _video_modes_stated(model_id: str, raw: dict[str, Any]) -> list[str]:
    """The video operations something the *provider* states, in a stable order.

    Read from `raw`'s own classification first — fal publishes `category`
    (`text-to-video`, `image-to-video`, `video-to-text`, `vision`) per endpoint
    and discovery stores it verbatim — and then from the model id, which is the
    provider's own name for the endpoint (`happyhorse-1-1/text-to-video`).

    Empty means **nobody stated a mode**. That is deliberately not the same answer
    as "image-to-video", which is what this used to publish for every video model:
    a text-only endpoint was offered an image control it cannot honour, and a
    model that takes text was not offered the control it needs.
    """
    haystack = " ".join(
        str(value)
        for value in (
            (raw or {}).get("category"),
            (raw or {}).get("output_type"),
            (raw or {}).get("mode"),
            model_id,
        )
        if value
    ).lower()
    tokens = set(haystack.replace("/", "-").replace("_", "-").split("-"))
    stated: list[str] = []
    for phrase, mode in _VIDEO_MODE_PHRASES:
        found = phrase in tokens if "-" not in phrase else phrase in haystack
        if found and mode not in stated:
            stated.append(mode)
    return stated


#: The video operations a kind's adapter implements for a model that states
#: nothing itself. This is **our** claim about our own code, never a provider's,
#: so a row that falls back to it publishes `modes_source: "gateway"`.
#:
#:   * `xai` genuinely serves both: `adapters/xai.py` sends a prompt-only body
#:     when there is no image, which is its documented text-to-video path;
#:   * `minimax` likewise: `first_frame_image` is optional in
#:     `adapters/minimax.py`, so a prompt alone is the text-to-video request;
#:   * `kie` posts whatever the caller supplied down one `createTask` route, and
#:     its curated video ids name no mode — image-to-video is what the previous
#:     default was, kept and labelled rather than silently asserted;
#:   * `fal` names the mode in `category` and in the endpoint id, so it needs no
#:     fallback; a fal row that stated neither publishes nothing rather than a
#:     guess.
_VIDEO_MODES_BY_KIND: dict[str, tuple[str, ...]] = {
    KIND_XAI: ("text-to-video", "image-to-video"),
    KIND_MINIMAX: ("text-to-video", "image-to-video"),
    KIND_KIE: ("image-to-video",),
}


def _hub_image_sizes(raw: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """The pixel preset behind each ratio, from the Hub's own statement where it makes one.

    The Hub's catalogue row carries `image_sizes` — one entry per preset, `{"id": "16:9",
    "label": "16:9", "width": 1280, "height": 720}`, thirteen of them on 0.6.91 including
    the video-valid `16:9 small` = 1024x576. That list is the sizes its worker renders, so
    when it is present **it is the whole answer**: it is read entry by entry and nothing is
    added to it, which is what makes a ratio the Hub states without a usable width/height
    publish with no geometry at all rather than with a plausible one.

    Discovery does not carry `image_sizes` into `raw` yet, so `HUB_IMAGE_SIZE_BY_RATIO` —
    the same table, read from the installed Hub 0.6.91 bundle and checked against this
    Hub's `/api/hub/catalog` on 2026-09-28 — is the source in force today. It is consulted
    **only while the Hub states no list**, the same direction every other provider fact
    travels: the producer's own answer wins, our copy fills a silence.
    """
    entries = raw.get("image_sizes")
    if not isinstance(entries, list) or not entries:
        return dict(HUB_IMAGE_SIZE_BY_RATIO)
    sizes: dict[str, tuple[int, int]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ratio = entry.get("id")
        width, height = entry.get("width"), entry.get("height")
        if not (isinstance(ratio, str) and ratio.strip()):
            continue
        # A stated size is a positive integer; anything else is the Hub saying it has no
        # geometry for that ratio, and it stays out rather than falling back to our table.
        if type(width) is int and type(height) is int and width > 0 and height > 0:
            sizes[ratio.strip()] = (width, height)
    return sizes


def _local_image_shape(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The ratios, sizes and defaults a Studio Hub location's Image lane states.

    **The lane that renders is the authority on the geometry.** Every image model used to be
    described by the *cloud* table (`IMAGE_SIZE_BY_RATIO`), so a location's 16:9 published
    1536x1024 while the Hub renders 1280x720 and its 4:3 published 1536x1024 against
    1152x864 — four of the five published sizes wrong by 23–33% linearly, Story composing at
    one geometry and receiving a file of another — and eight of the thirteen ratios the lane
    accepts were hidden, including the video-valid `16:9 small` = 1024x576 the Image studio
    added so a still destined for the video lane needs no crop.

    Read from the contract the Hub publishes, with the reader this file already has for it
    (`_hub_declared_parameter`) rather than a second parser:

      * `parameters[aspect_ratio].enum` — every ratio the lane will parse, in the lane's own
        order, published verbatim as `aspect_ratios`;
      * `parameters[aspect_ratio].default` — the lane's own default, published as
        `default_aspect_ratio` (and the `is_default` option) when it names one of those
        ratios;
      * `parameters[resolution].default`/`.enum` — the resolution class the lane states;
      * each ratio's pixels, from `_hub_image_sizes`.

    `None` means this is not a Hub image lane: no `lane_contract`, or one that declares no
    `aspect_ratio` parameter at all. That is a lane which stated nothing, and the caller
    keeps the table it has always used — absence must not read as "no ratios".
    """
    contract = (raw or {}).get("lane_contract")
    if not isinstance(contract, dict):
        return None
    declared = _hub_declared_parameter(contract, "aspect_ratio")
    if not isinstance(declared, dict):
        return None
    ratios = list(
        dict.fromkeys(
            str(entry).strip()
            for entry in declared.get("enum") or []
            if isinstance(entry, str) and str(entry).strip()
        )
    )
    if not ratios:
        return None
    stated_default = declared.get("default")
    default_ratio = (
        stated_default.strip()
        if isinstance(stated_default, str) and stated_default.strip() in ratios
        else ratios[0]
    )
    resolution_param = _hub_declared_parameter(contract, "resolution")
    resolution = None
    if isinstance(resolution_param, dict):
        stated_resolution = resolution_param.get("default")
        if isinstance(stated_resolution, str) and stated_resolution.strip():
            resolution = stated_resolution.strip()
        else:
            allowed = [
                str(value).strip()
                for value in resolution_param.get("enum") or []
                if isinstance(value, str) and str(value).strip()
            ]
            resolution = allowed[0] if allowed else None
    return {
        "aspect_ratios": ratios,
        "sizes": _hub_image_sizes(raw),
        "default_aspect_ratio": default_ratio,
        "resolution": resolution,
    }


def _output_formats(definition: ProviderDefinition, raw: dict[str, Any]) -> dict[str, list[str]]:
    """`{"output_formats": [...]}` when a Studio Hub lane declares an `output_format` parameter.

    Empty (the key is absent) for a lane that states none and for every cloud provider, which return
    their native file. A hint only: the request field is ignored by a node that predates it, so
    nothing depends on this being published.
    """
    if definition.kind != KIND_STUDIO_HUB or not isinstance(raw, dict):
        return {}
    contract = raw.get("lane_contract")
    declared = _hub_declared_parameter(contract, "output_format") if isinstance(contract, dict) else None
    values = [v for v in (declared or {}).get("enum") or [] if isinstance(v, str) and v]
    return {"output_formats": list(dict.fromkeys(values))} if values else {}


def _image_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("raw") or {}
    local = _local_image_shape(raw) if definition.kind == KIND_STUDIO_HUB else None
    if local is not None:
        # **One option per ratio the lane stated a size for.** A ratio it accepts but gives
        # no pixels for still publishes in `aspect_ratios` — the caller is told the lane
        # takes it — and gets no `output_options` row, because there is no size to publish
        # and none may be invented. A row is deliberately not emitted with a null width:
        # Story Studio filters on `resolution` and `aspect_ratio` alone and then renders
        # `width.toLocaleString()`, so a sizeless row is a crash, not an admission of
        # ignorance (the same shape as the TTS character cap).
        aspect_ratios = local["aspect_ratios"]
        default_aspect_ratio = local["default_aspect_ratio"]
        default_resolution = local["resolution"]
        options = [
            {
                "resolution": default_resolution,
                "aspect_ratio": ratio,
                "width": local["sizes"][ratio][0],
                "height": local["sizes"][ratio][1],
                "is_default": ratio == default_aspect_ratio,
                "credits_per_image": 0,
                "base_credits_per_image": 0,
                "price_version": PRICE_VERSION,
                "reference_generation_credits": 0,
                "reference_generation_price_version": PRICE_VERSION,
            }
            for ratio in aspect_ratios
            if ratio in local["sizes"]
        ]
    else:
        # The cloud lane: unchanged, and deliberately so — what is offered here is what
        # `image_size_for` sends, which is why the two share `IMAGE_SIZE_BY_RATIO`.
        aspect_ratios = _IMAGE_RATIOS
        default_aspect_ratio = "16:9"
        default_resolution = "1K"
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
    # **Three answers, read from the model's own statement.** This used to be a
    # definite `True` for `studio_hub` and a definite `False` for everything
    # else, while `operations` claimed `image-to-image` on every row — so a
    # location whose Hub says `capabilities: ["txt2img"]` advertised references it
    # does not take, and a fal `text-to-image` endpoint advertised an
    # image-to-image operation it does not serve. The contradiction was the tell.
    reference_state = image_reference_state(definition.kind, model_id, raw)
    # **The figures are the lane's, not this file's.** They used to be the literal `1, 4` beside a
    # `2048` pixel ceiling: four guesses, published for every reference-capable model, and only one
    # of them (the prompt cap) had ever been measured. The worker states what its edit lane takes,
    # in the same lane contract the ratios and resolutions come from, so that is what is published —
    # and a lane that states none publishes `None` (unstated), never a gateway number.
    references = image_reference_limits(raw)
    if reference_state is True:
        operations = ["text-to-image", "image-to-image"]
        minimum_references = references["min"]
        maximum_references = references["max"]
    else:
        # Every image adapter here can send a *prompt*, so text-to-image stands
        # whatever else is unstated; image-to-image is claimed only when the model
        # says it takes one.
        operations = ["text-to-image"]
        minimum_references, maximum_references = (
            (0, 0) if reference_state is False else (None, None)
        )
    return {
        "execution_label": "Local" if definition.kind == "studio_hub" else "Remote",
        # The lane's own ratios and presets for a Studio Hub location (`local` above), the
        # untouched cloud table for everything else.
        "aspect_ratios": aspect_ratios,
        "default_aspect_ratio": default_aspect_ratio,
        "default_resolution": default_resolution,
        "output_options": options,
        "supports_reference_images": reference_state,
        "supports_image_to_image": reference_state,
        # ponytail: `true` for every image model. Ceiling — a model with no seed
        # parameter is offered a control it rejects. It is `true` because both
        # lanes we serve *do* accept one: the studio_hub adapter passes `seed`
        # through explicitly (see `_generate_image_or_voice`), and the curated
        # cloud image models take one. A deliberate over-offer rather than an
        # invented figure. Upgrade path — read it per model where a provider
        # states it.
        "supports_seed": True,
        "min_reference_images": minimum_references,
        "max_reference_images": maximum_references,
        # The lane's own pixel ceiling for one reference, published so Story can fit a portrait to
        # it before uploading. `None` when the lane states none — an unstated ceiling is not 2048,
        # which is what this said before it was read from the contract.
        "reference_image_max_dimension": (
            references["max_dimension"] if reference_state is True else None
        ),
        # ponytail: flat prompt cap and cloud ratio list for every image model that states
        # neither. Ceiling — the previous GenStudio published 4,000 on some models and
        # 4,800 on others, and its ratio lists varied; a flat 4,000 is deliberately the
        # *conservative* end (it warns early rather than letting a request fail
        # at the provider, which is the violation Story Studio's own notes record
        # as "a 4,000-character cap overruling a published 4,800"). The ratios are the
        # cloud table; a Studio Hub row reads its lane's own list above. Upgrade path
        # — per-model values once a provider publishes them.
        #
        # Reference *strength* is a claim only a model that takes references can
        # make, so it is read from the model's own block and is otherwise `none`
        # (a model that does not take one) or `null` (unstated) — never `none`
        # for a location row that does use them.
        "reference_strength_mode": (
            (raw.get("reference_strength_mode") or None) if reference_state is True
            else ("none" if reference_state is False else None)
        ),
        "supported_reference_strengths": (
            (raw.get("supported_reference_strengths")
             if isinstance(raw.get("supported_reference_strengths"), list) else [])
            if reference_state is True
            else ([] if reference_state is False else None)
        ),
        "max_prompt_characters": max_prompt_characters,
        "max_prompt_characters_source": max_prompt_source,
        "operations": operations,
        "requires_admission_permit": False,
        "availability_note": None,
        **_output_formats(definition, raw),
    }


#: The modes, canvases and durations a Studio Hub location's own lane contract publishes.
#:
#: The worker is the only thing that knows what its lane accepts, so the gateway relays what it
#: said instead of restating it. `None` means the contract is absent or incomplete, and
#: `providers.models.withhold_reason` refuses to publish such a row at all — a video model the
#: gateway cannot describe is one Story Studio would offer a control it cannot honour.
def _local_video_shape(raw: dict[str, Any]) -> dict[str, Any] | None:
    contract = (raw or {}).get("lane_contract")
    if not isinstance(contract, dict):
        return None
    parameters = {
        str(entry.get("name")): entry
        for entry in contract.get("parameters") or []
        if isinstance(entry, dict) and entry.get("name")
    }

    def enum(name: str) -> list[Any]:
        value = (parameters.get(name) or {}).get("enum")
        return list(value) if isinstance(value, list) else []

    modes = [mode for mode in contract.get("modes") or [] if isinstance(mode, str) and mode]
    ratios = [ratio for ratio in enum("aspect_ratio") if isinstance(ratio, str) and ratio]
    resolutions = [item for item in enum("resolution") if isinstance(item, str) and item]
    durations = [item for item in enum("duration_seconds")
                 if type(item) is int and item > 0]
    if not modes or not resolutions or not durations:
        return None
    return {"modes": modes, "aspect_ratios": ratios, "resolutions": resolutions,
            "durations": durations, "canvases": _local_video_canvases(contract)}


def _local_video_canvases(contract: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The lane's published `(aspect_ratio, resolution)` pairs, when it publishes them.

    **The two parameter enums are not a set of canvases.** A lane can offer `448p` and `1:1` and
    `11:7` while the pair `448p + 16:9` does not exist, and the cross product offered exactly
    that — measured on 2026-09-27 against a Hub video lane, whose published 16:9 canvas is the one its
    pack floors to 2:1. So when the lane publishes the pairs, those pairs are the variants.

    `None` means the lane does not publish a canvas list at all (a worker older than 0.6.81);
    a list means every entry in it must be well-formed, because a canvas this gateway cannot read
    is one it would have to describe to Story Studio — and a partially-described variant is worse
    than none.
    """
    entries = contract.get("canvases")
    if not isinstance(entries, list):
        return None
    canvases: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        ratio = entry.get("aspect_ratio")
        resolution = entry.get("resolution")
        accepted = [item for item in entry.get("durations") or []
                    if type(item) is int and item > 0]
        if not (isinstance(ratio, str) and ratio.strip()
                and isinstance(resolution, str) and resolution.strip() and accepted):
            return None
        canvases.append({"aspect_ratio": ratio.strip(), "resolution": resolution.strip(),
                         "durations": accepted})
    return canvases or None


def _local_video_controls(contract: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The lane's **switch** parameters, relayed so a caller can offer exactly those.

    A boolean lane parameter is a choice the lane honours, and the caller cannot know it exists
    without being told: measured 2026-09-27, when Studio Hub gave its video lane an `audio` switch
    (0.6.83) and this gateway published no trace of it, so Story Studio had no way to offer the
    one thing the owner asked for — a video with no sound.

    **Only booleans, and only fields the lane itself stated.** The rest of a lane's parameter list
    is the vocabulary this row already publishes in a better form (`modes`, `aspect_ratios`,
    `variants`), and restating it here would be a second copy free to disagree with the variants a
    caller is actually billed for. `type`/`default`/`note` are relayed verbatim: the note is the
    lane's own sentence about what the switch does, which is the sentence a caller should show —
    this gateway has no idea what a given lane's switch costs.

    `None` means the lane publishes no boolean parameter (or a contract this gateway cannot read),
    which is not the same statement as an empty list: one is "nothing offered", the other "nothing
    stated". A malformed entry is dropped alone rather than failing the row, because a switch this
    gateway cannot describe is one a caller must not render.
    """
    entries = contract.get("parameters")
    if not isinstance(entries, list):
        return None
    controls: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not (isinstance(name, str) and name.strip()):
            continue
        if str(entry.get("type") or "").strip().lower() != "boolean":
            continue
        controls.append({
            "name": name.strip(),
            "type": "boolean",
            # A stated default travels; an absent one stays absent, and the caller shows the
            # switch from the lane's own `default` rather than inventing one here.
            "default": entry.get("default") if isinstance(entry.get("default"), bool) else None,
            "note": entry.get("note") if isinstance(entry.get("note"), str) else None,
        })
    return controls or None


def _video_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    # Story Studio accepts a video variant ONLY when all six fields are present
    # and valid, so a partially-described variant is worse than none.
    #
    # **A cloud model's options come from `video_capabilities`**, one record per
    # model, and nowhere else. Before that table the row was one flat guess —
    # 5 s and 10 s at "720p" in `["16:9","9:16","1:1"]` for every cloud video
    # model — which Wan 3.0 (2–30 s, 480p/720p/1080p, six ratios) and Seedance
    # 2.5 (4–30 s, seven ratios) both refuse. A model with no record publishes its
    # mode and no variants, which is how "we could not work it out" stays distinct
    # from "the answer is none".
    #
    # `modes` and `generates_audio` are read from the record the same way, and a
    # model with no record keeps what its provider stated:
    #
    #   * every video model used to be advertised as image-to-video only, while
    #     the curated list contains text-to-video endpoints whose own ids say so
    #     (`happyhorse-1-1/text-to-video`, `kling/v3-turbo-text-to-video`). A
    #     model that only takes text was offered a control it cannot honour, and
    #     one that takes text was not offered the control it needs;
    #   * `generates_audio: False` was a definite "no" for every model, and Google
    #     Veo 3 generates audio. Story Studio reads a definite false as "do not
    #     offer this", so the honest answer for a silent provider is `null`.
    raw_id = str(model.get("model_id") or model_id or "").lower()
    raw = model.get("raw") or {}
    local = _local_video_shape(raw) if definition.kind == KIND_STUDIO_HUB else None
    if local is not None:
        # A location's own lane states its modes, canvases and durations. That is a *provider*
        # statement — the Studio produced it — and it is the only source for a local row: the
        # model-id heuristic below is for cloud ids, and the flat 5s/10s@720p fallback is a
        # gateway guess that would offer this lane work it refuses.
        modes, modes_source = local["modes"], "provider"
        aspect_ratios = local["aspect_ratios"]
        # Each variant names the aspect ratio that goes with its exact size and duration. The
        # caller has to send all three, because the lane's canvases are pairs: its 448p class
        # carries 704x448 and 448x448, so "448p, 4s" alone names two different pictures.
        canvases = local.get("canvases")
        if canvases:
            # The ratios that exist, from the canvases rather than from the parameter enum: the
            # enum is what the lane will *parse*, the canvas list is what it will *produce*.
            aspect_ratios = list(dict.fromkeys(canvas["aspect_ratio"] for canvas in canvases))
            variants = [
                {
                    "operation": mode,
                    "duration_seconds": seconds,
                    "resolution": canvas["resolution"],
                    "aspect_ratio": canvas["aspect_ratio"],
                    "credits_per_video": 0,
                    "minimum_credits": 0,
                    "price_version": PRICE_VERSION,
                }
                for mode in modes
                for canvas in canvases
                for seconds in canvas["durations"]
            ]
        else:
            # A lane that publishes no canvas list states its modes, ratios, classes and durations
            # but not which of them pair, so this is the cross product it has always been. Kept for
            # a worker older than 0.6.81 rather than withheld: every one of these rows was published
            # before that release, and a caller that picks a pair the lane refuses is told so by the
            # lane. `ponytail:` remove this branch when no supported worker predates the canvas list.
            variants = [
                {
                    "operation": mode,
                    "duration_seconds": seconds,
                    "resolution": resolution,
                    "credits_per_video": 0,
                    "minimum_credits": 0,
                    "price_version": PRICE_VERSION,
                }
                for mode in modes
                for seconds in local["durations"]
                for resolution in local["resolutions"]
            ]
        stated_audio = raw.get("generates_audio") if isinstance(raw, dict) else None
        controls = _local_video_controls((raw or {}).get("lane_contract") or {})
        return {
            "modes": modes,
            "modes_source": modes_source,
            "aspect_ratios": aspect_ratios,
            "generates_audio": stated_audio if isinstance(stated_audio, bool) else None,
            "variants": variants,
            # The lane's own switches, where it publishes any. Absent for a lane that publishes
            # none — a cloud row below states no `controls` either, and a caller must show a
            # control only where one is published rather than on every video model alike.
            **({"controls": controls} if controls else {}),
        }
    # **Read what the provider states, then the model's own published record, and
    # label which it was.** The id heuristic alone was the defect: fal's
    # `category` — which discovery stores — was never read, so a row published
    # as `category: "text-to-video"` came out as image-to-video only, and a
    # text-only endpoint was offered an image control it cannot honour.
    record = video_capabilities.for_model(
        definition.id, str(model.get("model_id") or model_id)
    )
    if record is not None:
        return _capability_video_row(record, raw)
    stated_modes = _video_modes_stated(raw_id, raw)
    if stated_modes:
        modes, modes_source = stated_modes, "provider"
    else:
        fallback = list(_VIDEO_MODES_BY_KIND.get(definition.kind, ()))
        modes, modes_source = fallback, ("gateway" if fallback else None)
    stated_audio = raw.get("generates_audio") if isinstance(raw, dict) else None
    # **A model nobody here has described publishes its mode and nothing else.**
    # The flat row this replaced offered every cloud video model 5 s and 10 s at
    # "720p" in `["16:9","9:16","1:1"]`, which most of them refuse — a canvas the
    # caller was told was the model's own. No variants means Story Studio's
    # `selectLiveImageToVideoModel` cannot select it, which is exactly right: a
    # model we cannot describe is not offered, rather than offered with a guess.
    return {
        "modes": modes,
        # Whose answer that list is, in the same discipline every other
        # gateway-chosen value follows: `provider` when the model or its id said
        # so, `gateway` when it is our adapter's capability, `null` when nobody
        # stated one and the list is therefore empty.
        "modes_source": modes_source,
        "aspect_ratios": [],
        "generates_audio": stated_audio if isinstance(stated_audio, bool) else None,
        "variants": [],
    }


def _capability_video_row(record: "video_capabilities.VideoCapability",
                          raw: dict[str, Any]) -> dict[str, Any]:
    """One cloud video row from the model's own record — every option, or none.

    The variants are enumerated from the record's own range (`duration_min` × step × resolution),
    because that is the contract Story Studio reads today; `duration_seconds: {min, max, step}` rides
    alongside it so a caller can render a slider instead of a 29-item list. Both come from the same
    record, so neither can describe a different model.
    """
    durations = record.durations()
    variants = [
        {
            "operation": mode,
            "duration_seconds": seconds,
            "resolution": resolution,
            "credits_per_video": 0,
            "minimum_credits": 0,
            "price_version": PRICE_VERSION,
        }
        for mode in record.modes
        for seconds in durations
        for resolution in record.resolutions
    ]
    stated_audio = raw.get("generates_audio") if isinstance(raw, dict) else None
    row: dict[str, Any] = {
        "modes": list(record.modes),
        "modes_source": record.source,
        "aspect_ratios": list(record.aspect_ratios),
        "resolutions": list(record.resolutions),
        "duration_seconds": record.duration_record(),
        # The record is an answer; a provider's own discovery row is another. The record wins
        # because it is the endpoint's schema, and `None` still means nobody stated one.
        "generates_audio": (
            record.generates_audio if record.generates_audio is not None
            else (stated_audio if isinstance(stated_audio, bool) else None)
        ),
        "variants": variants,
        # Which request shapes this model takes, so a caller offers only those. `False` here is a
        # statement from the endpoint's schema (it declares no such field), never a default.
        "supports_start_frame": bool(record.start_frame_field),
        "supports_end_frame": bool(record.end_frame_field),
        "supports_reference_images": bool(record.reference_field),
        "max_reference_images": record.max_reference_images or None,
        "reference_image_kinds": list(record.reference_kinds),
        # Whose answer every option above is, and when it was read. A curated record and a
        # provider-published one are different claims and must not read the same.
        "capability_source": record.source,
        "capability_as_of": record.source_date or None,
    }
    controls = video_capabilities.audio_switch(record)
    if controls:
        row["controls"] = controls
    return row


#: Where a published limit came from. The values are deliberately dull, and
#: `provider` is the only one that means "the model's own figure".
#:
#:   provider — the provider stated it. A **cloud** lane's class.
#:   curated  — the owner's `model-limits.local.json` filled a silence.
#:   gateway  — our own documented default or recorded table.
#:
#: **A Studio Hub location brings its own vocabulary, and it is relayed rather than
#: reinterpreted** (HUB-20): `model` — the checkpoint's own file states it;
#: `worker_default` — the Hub's own fallback; `unknown` — nothing states it, and the
#: value travels as `null`. `curated` is the Hub's packaged metadata rather than the
#: checkpoint's word: the same token as above for a different producer, deliberately,
#: because it makes the same claim (*somebody chose this; the model did not say it*).
#: The tuple below is the set **this gateway** chooses. A Hub row's kind is the Hub's
#: answer, and `worker_default` is the one that used to arrive here as `provider`.
LIMIT_SOURCES = ("provider", "curated", "gateway")


def _output_default(definition: ProviderDefinition) -> int | None:
    """A limit the gateway *must* supply because the provider requires the field.

    Only Anthropic's Messages API makes `max_tokens` mandatory (`400` without it).
    Every other chat lane treats it as optional, so an absent value stays `null`
    and no number is invented. Anthropic's adapter has to send one, so the number
    it will send is **published as ours** (`max_output_tokens_source: "gateway"`)
    instead of the row saying `null` while the request carries a figure of ours —
    the same rule the TTS character limit follows.

    The figure is `anthropic.DEFAULT_MAX_TOKENS` (**128,000** since the owner's
    2026-10-19 decision: "4096 is too small. all models write more than that. the
    minimum is at least 128K tokens"), so an Anthropic row nothing else states a
    figure for is not described more narrowly than any model the catalogue knows.
    A model that publishes its own smaller ceiling is unaffected: a published value
    always wins over this fallback, and a `400` naming the real maximum is corrected
    from the provider's own sentence (`anthropic._max_tokens_too_large`).

    One copy: the adapter imports this constant too.
    """
    if getattr(definition, "kind", None) == KIND_ANTHROPIC:
        from .adapters.anthropic import DEFAULT_MAX_TOKENS

        return DEFAULT_MAX_TOKENS
    return None


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
    # **The Hub's own recommended request size beats every default and every curated figure**
    # (it is per model, sized so one request finishes inside the Hub's deadline on the slowest Mac
    # allowed to run it — Breeze about 5,000). Read from the catalogue row or its lane contract.
    contract = raw.get("lane_contract")
    for value in (raw.get("maximum_input_characters"),
                  contract.get("maximum_input_characters") if isinstance(contract, dict) else None):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value), "provider"
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

#: Speed ranges a lane accepts but does not publish, so what a caller is *offered*
#: is what the adapter will actually send.
#:
#: The generic range is 0.5–2.0. fal's ElevenLabs routes take **0.7–1.2**, and
#: `adapters/fal.py` used to *drop* anything outside it — so a caller picked a speed
#: this catalogue had told it was valid and got normal speed, with no signal in
#: either direction. The adapter refuses out-of-range now; this makes the published
#: range the accepted one.
#:
#: ponytail: keyed by model id, hand-recorded from what the adapter already
#: enforced. Ceiling — a route with a different range is still over-promised.
#: Upgrade path — fal publishes the range per endpoint in its OpenAPI schema
#: (`?endpoint_id=<id>`), which is the same source the body schemas are read from.
_TTS_SPEED_RANGES: dict[str, tuple[float, float]] = {
    "fal-ai/elevenlabs/tts/turbo-v2.5": (0.7, 1.2),
}


def _tts_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("raw") or {}
    supports_speed = model_id not in _TTS_NO_SPEED
    if supports_speed:
        minimum_speed, maximum_speed = _TTS_SPEED_RANGES.get(model_id, (0.5, 2.0))
    else:
        minimum_speed, maximum_speed = 1.0, 1.0
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
        # **One read of the rule.** The value comes from `voices.supports_cloning`
        # — the same function the voice picker's `reference_support` is built on —
        # because a second implementation here had different fallbacks, so a row
        # could claim cloning while its picker was empty. `voices.CLONING_KINDS`
        # is the per-kind default that stands behind an unstated model (fish,
        # elevenlabs), and it lives there so there is exactly one copy.
        "supports_voice_cloning": voices.supports_cloning(definition.id, model_id),
        "preview_policy": "none",
        "generation_enabled": True,
        "queue_class": "direct",
        # The provider's own list, whole. This was `languages[:40]` — a silent
        # truncation of a stated fact, invisible today only because ElevenLabs
        # publishes 32. A provider that states 60 languages is describing 60
        # languages; a cap here would make the catalogue quietly wrong about the
        # rest, which is the "our number in their field" shape with no label.
        "supported_languages": languages,
        # **`provider_submit_concurrency` is not invented here** (C3, audit H7): it was the
        # literal `1` on every TTS lane. Story Studio *does* read it — it is how many sections
        # of one narration it keeps in flight, and with it gone every narration ran one
        # section at a time — so a Studio Hub row now carries the location's live TTS total,
        # written in `rebuild` (`_slots_by_location`). A cloud row states nothing.
        # ponytail: one invented temperature range for every cloud TTS model that takes
        # one. Ceiling — a model with a narrower or wider range is described
        # wrongly, and the control fails at the provider outside its real bounds.
        # Upgrade path — read it per model where a provider states a range.
        #
        # **A Studio Hub lane is different: it states its parameters, so it is read.** The
        # old comment said "the local MLX engines and Fish do accept a temperature"; the
        # voice worker reads a top-level `temperature` for Chatterbox only (Fish reads
        # `fish_temperature`, Qwen3/VoxCPM2/Breeze/OmniVoice/Khmer read none) and no lane
        # contract lists it, while the adapter forwards Story's `controls` as one nested
        # object the worker's request model drops. A knob published on every Hub voice that
        # changed nothing in any of them (audit 2026-10-01).
        "controls": _tts_controls(definition, raw),
        **_output_formats(definition, raw),
        **_tts_pause_parameters(definition, raw),
    }


#: The optional join pauses a Studio Hub voice lane may declare (Hub, owner 2026-10-03: Sleep wants a
#: 1.0 s pause at a paragraph break). The adapter forwards them; a node that predates them drops them.
TTS_PAUSE_PARAMETERS = ("paragraph_pause_sec", "sentence_pause_sec")


def _tts_pause_parameters(definition: ProviderDefinition, raw: dict[str, Any]) -> dict[str, Any]:
    """`{"pause_parameters": [...]}` with the lane's own bounds and default, when the Hub declares them.

    Absent for a lane that states none (an older Hub, every cloud provider), so a caller knows the
    request field would do nothing there.
    """
    if definition.kind != KIND_STUDIO_HUB or not isinstance(raw, dict):
        return {}
    contract = raw.get("lane_contract")
    published = []
    for name in TTS_PAUSE_PARAMETERS:
        declared = _hub_declared_parameter(contract, name) if isinstance(contract, dict) else None
        if declared is None:
            continue
        published.append({"name": name, **{
            key: declared[key] for key in ("minimum", "maximum", "default")
            if isinstance(declared.get(key), (int, float)) and not isinstance(declared.get(key), bool)
        }})
    return {"pause_parameters": published} if published else {}


#: The Hub capability that says a TTS model takes a free-text delivery/emotion instruction.
#: **Read from the model's own `capabilities` list, never a model name** (§1): the Hub is the
#: authority on which engine has the feature, and it may spell it with an underscore.
DELIVERY_INSTRUCTION_CAPABILITY = "delivery-instruction"
DELIVERY_INSTRUCTION_LIMIT = 500


def _delivery_names() -> set[str]:
    return {DELIVERY_INSTRUCTION_CAPABILITY, DELIVERY_INSTRUCTION_CAPABILITY.replace("-", "_")}


def _hub_controls(raw: Any) -> dict[str, Any]:
    """The Hub's per-model `controls` block, as `/api/hub/capabilities` published it.

    Carried onto the model's raw record by discovery
    (`providers/discovery._hub_controls`), re-read on every refresh.
    """
    controls = (raw or {}).get("hub_controls") if isinstance(raw, dict) else None
    return controls if isinstance(controls, dict) else {}


def takes_delivery_instruction(raw: Any) -> bool:
    """Does this model publish the delivery-instruction capability?

    **Two places, both the Hub's own, and either is enough:** the catalogue row's
    `capabilities` (the Voice catalogue derives the token from the family's engine, so it is
    stated per model) and the capability snapshot's `controls.capabilities`. A Hub that
    publishes it in only one of the two still publishes it — and nothing here derives support
    from a model id, a family name or a repo (§1).
    """
    if not isinstance(raw, dict):
        return False
    wanted = _delivery_names()
    for block in (raw.get("capabilities"), _hub_controls(raw).get("capabilities")):
        if isinstance(block, (list, tuple, set)):
            for item in block:
                name = (str(item.get("name") or item.get("id") or "")
                        if isinstance(item, dict) else str(item))
                if name in wanted:
                    return True
    return False


def delivery_guidance(raw: Any) -> Any:
    """The Hub's guidance for the capability, **exactly as it sent it**, or `None`.

    The one source is the snapshot's `controls.delivery_instruction`
    (`components/hub/backend/capabilities.py`): `kind`, `engine_form` (`instruct` for Breeze
    TTS 2, `text_prefix` for VoxCPM2), `max_characters`, up to three `examples`, the
    `language_rule`, the `inline_events` the engine documents, a directed `cfg_scale` and the
    parameter it belongs to, the card's `source_url` and a `note`. Forwarded without a field
    changed — a consumer that reshaped it would be describing an engine it has not read.

    `None` when the Hub published none, and the control is then published without a `guidance`
    field: nothing is invented for a Hub that said nothing.
    """
    guidance = _hub_controls(raw).get("delivery_instruction")
    if guidance in (None, "", [], {}):
        return None
    return guidance


def _delivery_control(raw: Any = None) -> dict[str, Any]:
    """One text control, in the same shape the numeric controls already publish.

    **`guidance` is the Hub's object, unchanged**, present only when the Hub published one —
    free text and its examples, a language rule, the inline vocal events the engine accepts,
    and the source URL. A consumer prints what is there and nothing when it is not.
    """
    control: dict[str, Any] = {
        "name": "delivery_instruction",
        "label": "Delivery / emotion",
        "type": "text",
        "max_length": DELIVERY_INSTRUCTION_LIMIT,
    }
    guidance = delivery_guidance(raw)
    if guidance is not None:
        control["guidance"] = guidance
    return control


def _tts_controls(definition: ProviderDefinition, raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The TTS controls a row publishes: a Hub lane's own, or the cloud default."""
    if takes_delivery_instruction(raw):
        # **Only where the Hub says the model takes it.** A control on every TTS row would
        # offer a field the engine ignores, which is the "control that does nothing" defect
        # this file exists to avoid.
        extra = [_delivery_control(raw)]
    else:
        extra = []
    if definition.kind == KIND_STUDIO_HUB:
        contract = raw.get("lane_contract") if isinstance(raw, dict) else None
        declared = _hub_declared_parameter(contract, "temperature") if isinstance(contract, dict) else None
        if declared is None:
            # No temperature on this lane — but the delivery instruction is its own capability
            # and must not be lost with it. (A Hub row with neither publishes `[]`, as before.)
            return extra
        def number(key: str, fallback: float) -> float:
            value = declared.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else fallback
        return [{
            "name": "temperature",
            "label": "Temperature",
            "type": "number",
            "minimum": number("minimum", 0.1),
            "maximum": number("maximum", 2.0),
            "default": number("default", 0.7),
            "step": 0.05,
        }] + extra
    return [{
        "name": "temperature",
        "label": "Temperature",
        "type": "number",
        "minimum": 0.1,
        "maximum": 2.0,
        "default": 0.7,
        "step": 0.05,
    }] + extra


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
        # **`available_workers` is deliberately absent (C4/M11).** It was the literal `1` on
        # every transcription lane with a `ponytail:` note admitting it was invented — while the
        # fleet reported the same model on sixteen machines. A number nobody measured, published
        # as though the lane had stated it, is the exact defect this project has a standing rule
        # against: *value + source, or `null` + why*. Nothing reads it; the real concurrency for
        # a location comes from that location's own published capacity
        # (`concurrency.hub_slot_reading`), and a cloud lane's from the settings file.
        # Removing the key is the fix; inventing a better number would not be.
    }


def _first_stated(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """The first key the provider actually stated, **including a stated `0`**.

    An `or` chain erases a published zero: `0` is falsy, so the lookup falls through
    to the next spelling, then to `None`, and the owner's curated value is published
    as though the provider had stated it. `_stated` and `limits.gap` were both
    written specifically to preserve a stated zero — and neither could ever see one,
    because the chain ran first and threw it away.

    "It said zero" and "it said nothing" are different answers, and the whole of
    Shape 2 is the cost of letting them share a value.
    """
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _hub_declared_parameter(contract: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The lane's own declaration of one parameter, or `None` when it lists none.

    Studio Hub's `lane_contract.parameters` is a list of what the lane **accepts**,
    each entry `{"name", "type", "required", …}` and — where it applies — the
    allowed values. "If you send something we list, it is validated, not ignored",
    which is what makes the *presence of a name* a fact worth publishing rather
    than a hint: a lane that declares `response_format` takes one, and a lane that
    does not will refuse it.

    Deliberately keyed on the parameter's **name and declared type**, never on the
    prose in its `note`: the Hub session asked us not to match on names-in-strings
    where a fact could be published, and three defects on their side came from
    doing exactly that.
    """
    declared = contract.get("parameters")
    if not isinstance(declared, list):
        return None
    for entry in declared:
        if isinstance(entry, dict) and str(entry.get("name") or "") == name:
            return entry
    return None


def _hub_stated_limit(raw: dict[str, Any], field: str) -> tuple[int | None, str | None]:
    """A Hub chat model's own limit, with the sentence saying where it read it.

    Hub 0.6.61 publishes `model_limits` on chat rows, one object per limit:

        {"context_window_tokens": {"value": 262144,
                                   "source": "model/config.json text_config.max_position_embeddings"},
         "max_output_tokens": {"value": 4096,
                               "source": "worker default: the checkpoint ships no generation_config.json"}}

    **A `value` of `null` beside a `source` is the Hub stating that it does not
    know**, so it reads as unstated — the same rule as everywhere else: "the answer
    is none" and "I could not work it out" must not share a value. The sentence is
    carried through rather than replaced, because it says *which* number this is:
    a checkpoint's own window and a worker's fallback are both worth publishing and
    are not the same claim.

    **`source_kind` is the Hub's own token for that same decision (HUB-20), and this
    gateway relays it rather than interpreting it.** It used to have no choice: the
    Hub published only the sentence, and `_limit_with_source` labels any stated value
    `provider`, so the live row told Story Studio that `max_output_tokens: 16384` was a
    number the *model* stated while the Hub's own sentence beside it said *"worker
    default: the checkpoint ships no generation_config.json"*. Two published fields
    disagreeing about one number, with the machine-readable one wrong. Matching a word
    in the sentence is what the Hub asked us not to do, so the token comes from them.
    """
    limits = raw.get("model_limits")
    if not isinstance(limits, dict):
        return None, None, None
    entry = limits.get(field)
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        # A Hub that publishes the bare number rather than the object: still its
        # own statement, just without the sentence.
        return int(entry), None, None
    if not isinstance(entry, dict):
        return None, None, None
    value = entry.get("value")
    source = entry.get("source")
    source = str(source).strip() if isinstance(source, str) and source.strip() else None
    kind = entry.get("source_kind")
    kind = str(kind).strip() if isinstance(kind, str) and kind.strip() else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, source, kind
    return int(value), source, kind


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


def _published_context_limit(
    definition: ProviderDefinition, model_id: str, model: dict[str, Any]
) -> tuple[Any, str, str | None]:
    """The context window this gateway publishes for a chat model — **one chooser**.

    Read every spelling a provider uses (`context_length`, `context_window`,
    `inputTokenLimit`, …), let a Hub's own sentence win, then the owner's curated
    file, and publish `None` when nobody stated one. The output chooser reads this
    too, because an output allowance above the context window is not a limit the
    model can honour (`_published_output_limit`).
    """
    raw = model.get("raw") or {}
    context = _first_stated(
        raw, ("context_length", "context_window", "inputTokenLimit", "input_token_limit")
    )
    hub_context, hub_context_detail, hub_context_kind = _hub_stated_limit(
        raw, "context_window_tokens")
    if hub_context is not None:
        context = hub_context
    value, source = _limit_with_source(
        "context_window_tokens", _stated(context), None, definition.id, model_id
    )
    if hub_context is not None and hub_context_kind:
        source = hub_context_kind
    return value, source, hub_context_detail


def _published_output_limit(
    definition: ProviderDefinition, model_id: str, model: dict[str, Any]
) -> tuple[Any, str, str | None, int | None]:
    """The output allowance this gateway publishes for a chat model — **one chooser**.

    Providers spell the same figure differently: OpenRouter uses
    `top_provider.max_completion_tokens`, Google `outputTokenLimit`, Anthropic
    `max_completion_tokens`. Read every spelling, then let a Hub's own sentence
    win (it states where its number came from), then the owner's curated file
    fill a gap, then the gateway's fallback answer. Returns the value, whose
    number it is, the Hub's sentence when it gave one, and — when the value was
    lowered — the number that was lowered.

    **An output allowance above the context window is not a limit.** Measured live
    2026-10-30: `studio-hub::mlx-community/gemma-4-26B-A4B-it-qat-4bit` published
    `context_window_tokens: 32768` beside `max_output_tokens: 128000`, because the
    Hub relayed its own worker fallback (`llm_engine.FALLBACK_MAX_OUTPUT_TOKENS`),
    which its lane contract says "is not a number the model states". A model cannot
    answer more tokens than fit in its window, so the published value is the smaller
    of the two. Nothing is invented: the number published is the model's own
    published context, and `max_output_tokens_capped_from` names the figure that was
    withheld so a reader can see both.

    **The generation path reads this too** (`published_output_cap`), because a
    separate "what should I send?" reader disagreed with the chooser the moment
    either gained a step: Anthropic published the model's own 128,000 while the
    adapter sent a silent 4,096 whenever the caller asked for nothing.
    """
    raw = model.get("raw") or {}
    top = raw.get("top_provider")
    top = top if isinstance(top, dict) else {}
    completion = _first_stated(top, ("max_completion_tokens",))
    if completion is None:
        completion = _first_stated(
            raw, ("max_completion_tokens", "outputTokenLimit", "output_token_limit")
        )
    hub_output, hub_output_detail, hub_output_kind = _hub_stated_limit(
        raw, "max_output_tokens")
    if hub_output is not None:
        completion = hub_output
    value, source = _limit_with_source(
        "max_output_tokens", _stated(completion), _output_default(definition),
        definition.id, model_id,
    )
    # **The Hub's own token wins when the Hub supplied the value.** `_limit_with_source` knows
    # three classes — provider, curated, gateway — and a value relayed from a Hub is none of
    # them, so it answered `provider`: a worker's fallback published to Story Studio as a number
    # the model stated. When the Hub names the kind, that is the honest answer and this gateway
    # has nothing to add to it.
    if hub_output is not None and hub_output_kind:
        source = hub_output_kind
    capped_from = None
    context, _context_source, _context_detail = _published_context_limit(
        definition, model_id, model)
    if (isinstance(value, int) and not isinstance(value, bool)
            and isinstance(context, int) and not isinstance(context, bool)
            and context > 0 and value > context):
        capped_from, value = value, context
    return value, source, hub_output_detail, capped_from


def published_output_cap(
    definition: ProviderDefinition | None, model_id: str, model: dict[str, Any] | None
) -> int | None:
    """What to send as `max_tokens` when the caller does not ask for one.

    `None` means this gateway has no number to offer, and a lane that treats the
    field as optional keeps sending nothing rather than inventing a cap. Only a
    lane that **must** state one (Anthropic) falls back to its own constant.
    """
    if definition is None or not model:
        return None
    value, _source, _detail, _capped_from = _published_output_limit(
        definition, model_id, model)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _chat_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    raw = model.get("raw") or {}
    # Providers spell the same two figures differently: OpenRouter uses
    # `context_length` / `top_provider.max_completion_tokens`, Google uses
    # `inputTokenLimit` / `outputTokenLimit`. Read every spelling before
    # concluding a provider said nothing — that reading lives in
    # `_published_context_limit` / `_published_output_limit` below, which the
    # generation path uses too, so the row and the request cannot disagree.

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

    # The withdrawal date, under every spelling a provider uses for it. A
    # `None` here means nobody stated one — never "this model does not retire".
    expiry = _first_stated(
        raw, ("expiration_date", "deprecation_date", "retirement_date", "retires_at")
    )
    expiry = str(expiry) if isinstance(expiry, (str, int, float)) and str(expiry).strip() else None

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
    elif default_temperature_only(definition.id, model_id):
        # The provider stated nothing, and this family refuses any temperature but
        # its own default. Withhold the control rather than offer a knob that comes
        # back as a `400`. Only the no-list branch: when a provider does publish
        # `supported_parameters`, its own answer is authoritative and wins.
        controls = [c for c in controls if c != "temperature"]

    structured = _supports("structured_outputs", "response_format")
    # **A Studio Hub lane states the same fact in its own contract**, and the Hub
    # session asked for it to be read rather than inferred: a lane that declares a
    # `response_format` parameter accepts one, which is exactly what this field
    # means. Read from the parameter's **name and declared presence**, never from
    # its prose — matching a word in a sentence is what they asked us not to do
    # (three defects on their side came from matching a name where a fact could
    # have been published).
    #
    # The *mode* stays unstated until the producer says it machine-readably:
    # `lane_contract.structured_output.formats` is their field for that, and it is
    # not in the running 0.6.63 (measured: the contract's keys are schema,
    # modality, operation, parameters, artifact, states, cancel,
    # deadline_seconds), so a Hub row publishes `structured_output_mode: null` —
    # "it takes a response_format, the choice of mode is unstated" — rather than
    # this gateway guessing which of the two the note meant.
    lane_contract = raw.get("lane_contract")
    lane_contract = lane_contract if isinstance(lane_contract, dict) else {}
    hub_response_format = _hub_declared_parameter(lane_contract, "response_format")
    if structured is None and hub_response_format is not None:
        structured = True
    # **The mode comes from the parameter that states it.** This published a flat
    # `"json_object"` for every model whose provider listed *either* parameter, so
    # a model offering only `json_schema` (OpenRouter states that as
    # `structured_outputs`) was described as the weaker mode — a claim the provider
    # never made. `response_format` is JSON-object mode; `structured_outputs` is
    # JSON-schema mode. When both are stated, `json_object` is still true and is
    # the value this field has always carried, so it stays.
    # **THE STRONG MODE IS SPELT `request_json_schema`, AND THE SPELLING IS LOAD-BEARING.**
    # The distinction above was correct and was being *erased on the wire*: this used to
    # publish `"json_schema"`, Story Studio's union is
    # `'none' | 'json_object' | 'request_json_schema'`, and its parser maps anything that is
    # neither of those onto `'json_object'`. So the stronger mode arrived as the weaker one
    # and the provider's claim was lost again, one layer further out.
    #
    # It costs behaviour, not a label. `productResponseFormat` in Story Studio sends a
    # provider-native `json_schema` with `strict: true` **only** when the product publishes
    # `request_json_schema`; read as `json_object`, the same request falls through to an
    # instruction that *describes* the schema and asks the model to comply — which is how a
    # model "complied for two packs of forty scenes and wrote prose for the third".
    #
    # This gateway is Story Studio's only language surface, and its contract is that Story
    # needs **zero code changes** to talk to it — so the published vocabulary is GenStudio's,
    # not ours. `json_schema` stays correct on the *request* side, where it is the provider's
    # own `response_format.type` (`adapters/studio_hub.py`, `adapters/openai_compat.py`).
    # The two spellings are not interchangeable in either place.
    # **A Hub lane now states its formats machine-readably** (`structured_output.formats`, Hub
    # 0.6.126; the comment above predates it). Read, not guessed: a lane that takes `json_schema`
    # is published as the strong mode Story sends a native schema for, one that takes only
    # `json_object` as the plain one. The Hub validates the answer and repairs it once
    # (`enforcement: prompt_validate_repair`), which is why this is "request" and not "enforced".
    hub_formats = lane_contract.get("structured_output")
    hub_formats = hub_formats.get("formats") if isinstance(hub_formats, dict) else None
    hub_mode = None
    if isinstance(hub_formats, list):
        if "json_schema" in hub_formats:
            hub_mode = "request_json_schema"
        elif "json_object" in hub_formats:
            hub_mode = "json_object"
    if hub_mode is not None:
        structured = True
    # **Anthropic states it per model** in `/v1/models` `capabilities.structured_outputs`
    # (`supported: true/false`), and `adapters.anthropic` turns a `json_schema` request into
    # `output_config.format`, where the API constrains decoding to the schema. That is the
    # strong mode, so it is published as `request_json_schema` and Story sends its schema.
    capabilities = raw.get("capabilities")
    stated = capabilities.get("structured_outputs") if isinstance(capabilities, dict) else None
    stated = stated.get("supported") if isinstance(stated, dict) else None
    if structured is None and hub_mode is None and isinstance(stated, bool):
        structured = stated
    if hub_mode is not None:
        structured_mode = hub_mode
    elif stated is True and structured:
        structured_mode = "request_json_schema"
    elif parameters is None or not structured:
        structured_mode = None
    elif "response_format" in parameters:
        structured_mode = "json_object"
    elif "structured_outputs" in parameters:
        structured_mode = "request_json_schema"
    else:
        structured_mode = None
    # Where the provider published nothing, the owner's curated file may know.
    # Gap-filling only: a live provider value always wins, because a file goes
    # stale and a provider does not.
    #
    # **A Hub states its own limits first.** It publishes `model_limits` per chat
    # model with the sentence saying where each number came from, and reading it
    # is the difference between a model's real window and this gateway's curated
    # flat `1,000,000` wearing the model's name — measured on 2026-09-24: the Hub
    # states 262,144 for Qwen3.5-9B-4bit while the curated file said 1,000,000,
    # two orders of magnitude out. The Hub's sentence travels beside the number in
    # `*_source_detail`, so a worker's fallback (its own words) is never mistaken
    # for the checkpoint's fact.
    #
    # **Both limits come from the same two choosers the generation path reads**
    # (`_published_context_limit` / `_published_output_limit`), so the row and the
    # request cannot describe different numbers — and the output chooser caps an
    # allowance above the context window, which is not a limit the model can honour.
    context_window, context_source, hub_context_detail = _published_context_limit(
        definition, model_id, model)
    max_output, output_source, hub_output_detail, max_output_capped_from = \
        _published_output_limit(definition, model_id, model)

    # The Hub states vision support per model, read from its published flag, never the name.
    hub_vision = raw.get("supports_vision")
    if hub_vision is True or (hub_vision is None and "vision" in (raw.get("capabilities") or [])):
        supports_vision = True
        if inputs is None:
            inputs = ["text", "image"]
        elif "image" not in inputs:
            inputs = list(inputs) + ["image"]
    elif hub_vision is False:
        supports_vision = False
        if inputs and "image" in inputs:
            inputs = [x for x in inputs if x != "image"]
    else:
        supports_vision = ("image" in inputs) if inputs else None

    return {
        "queue_class": "direct",
        "context_window_tokens": context_window,
        "context_window_tokens_source": context_source,
        # The provider's own sentence about where the number came from, when it
        # gave one (`null` otherwise) — additive, so a reader that only knows the
        # three classes above is unaffected.
        "context_window_tokens_source_detail": hub_context_detail,
        "max_output_tokens": max_output,
        "max_output_tokens_source": output_source,
        "max_output_tokens_source_detail": hub_output_detail,
        # **The figure that was withheld, when the allowance was above the context window.**
        # A number the model cannot answer within is not a limit, so the published value is the
        # context; this names what a producer stated and why it did not survive. Absent (not
        # null) on every row that needed no cap, so an ordinary row is byte-identical to before.
        **({"max_output_tokens_capped_from": max_output_capped_from}
           if max_output_capped_from is not None else {}),
        # **Where the token counts came from, said plainly.** Every chat lane here
        # forwards the usage its provider stated; none of them re-measures it, so
        # `false` is the honest answer on every row rather than a claim that the
        # numbers were checked. Studio Hub asks for exactly this and draws the
        # distinction itself: "we VALIDATE what the worker states; validating is not
        # re-measuring".
        "verified_token_usage": False,
        # **The producer's own statement about the JSON guarantee**, at the path it
        # named (`lane_contract.structured_output`), carried verbatim and `null`
        # when no lane stated one.
        #
        # What goes here is the *fact* — which formats are accepted, how the
        # guarantee is kept (prompt + validate + repair, or a grammar), and what it
        # costs — never a technique: two-pass is a Story Studio decision, and a
        # model cannot know whether a caller wants it. The Hub session was explicit
        # that this is deliberately not a `two_pass` flag, and they were right.
        #
        # **The lane's own contract, as the producer states it.** This published a
        # one-key whitelist — `structured_output` and nothing else — so every other
        # fact the Hub sends was dropped on the floor. `deadline_seconds` was one of
        # them: the number was in this row's `raw` the whole time, the adapter read
        # it to size its own poll, and the caller could not see it. Measured
        # 2026-09-26: Story Studio **discovered the 900-second window by hitting
        # it**, because a stated fact was being withheld.
        #
        # That is the "publish what is known, and nothing else" rule inverted.
        # Withholding what a producer states is as wrong as inventing what it does
        # not, and it costs the caller the same way — they find out by failing.
        # Anything the Hub adds later arrives with no gateway release, which is the
        # point of relaying a contract rather than restating one.
        "lane_contract": dict(lane_contract) or None,
        "controls": controls,
        "input_modalities": inputs,
        "supports_vision": supports_vision,
        # **The precision this model is published at, where its own maker states one**
        # (C5/M14). Carried from the manifest the Hub read, with the sentence that says where the
        # value came from — a bare "4-bit" a reader cannot check is the same defect as a token
        # window with no source. `null` is a real answer here and travels with its reason: a
        # publisher that names no precision gets `null`, never a plausible default.
        "quantization": raw.get("quantization"),
        "quantization_source": raw.get("quantization_source"),
        "supports_structured_output": structured,
        "structured_output_mode": structured_mode,
        "supports_web_search": _supports("web_search", "web_search_options"),
        # Neither of these is stated by any provider here, and a definite `false`
        # is not the same statement as "unknown": Story Studio reads
        # `supports_vision === true` to decide whether to offer a control at all,
        # and the same shape applies to these. `returns_citations: False` was
        # published for every model — including ones whose provider states web
        # search — so "none" and "could not work it out" shared a value, which is
        # the rule this whole section exists to enforce.
        "returns_citations": None,
        # **The withdrawal date, which a provider does state and this catalogue
        # was throwing away.** OpenRouter publishes `expiration_date` on a model it
        # is retiring, and discovery's key whitelist discarded it at fetch time —
        # so Story Studio's retirement warning (a whole surface: model selector
        # badge, `genstudio-model-retirement.ts`) could never fire. Measured
        # 2026-09-27: the key never reached `raw`, which is why this reads `None`
        # for every model today.
        #
        # `retirement_source` names whose statement it is: `provider` when a date
        # arrived, `None` when nobody said. Nothing is derived from the date — a
        # model with no stated expiry is not "not retiring", it is unstated, and
        # `replacement_model_id` is published only if a provider ever states one.
        "retires_at": expiry,
        "deprecation_warning": expiry is not None,
        "deprecation_warning_at": expiry,
        "scheduled_retirement_at": expiry,
        "upstream_shutdown_at": None,
        "replacement_model_id": _first_stated(raw, ("replacement_model_id",)) or None,
        "retirement_source": "provider" if expiry is not None else None,
    }


def _embeddings_row(model_id: str, definition: ProviderDefinition,
                    model: dict[str, Any]) -> dict[str, Any]:
    """An embeddings row: what the vectors are, read from the Hub's own lane contract.

    `null` where the Hub stated nothing (an older Hub), never a guessed dimension.
    """
    contract = (model.get("raw") or {}).get("lane_contract") or {}
    artifact = contract.get("artifact") if isinstance(contract.get("artifact"), dict) else {}
    texts = next((item for item in contract.get("parameters") or []
                  if isinstance(item, dict) and item.get("name") == "input"), {})
    return {"kind": "embeddings", "capabilities": ["embeddings"],
            "dimensions": artifact.get("dimensions"), "normalized": artifact.get("normalized"),
            "max_inputs": texts.get("max_items"),
            "max_input_tokens": texts.get("max_tokens_per_item")}


def vision_tasks(model: dict[str, Any]) -> list[str] | None:
    """The analysis tasks a vision model publishes (`depth`, `subject`, ...): `None` if the Hub said nothing."""
    raw = model.get("raw") or {}
    contract = raw.get("lane_contract") if isinstance(raw.get("lane_contract"), dict) else {}
    tasks = raw.get("tasks") or contract.get("tasks")
    return [str(task) for task in tasks] if isinstance(tasks, list) else None


def _vision_row(model_id: str, definition: ProviderDefinition, model: dict[str, Any]) -> dict[str, Any]:
    """A vision (image analysis) row: which tasks the Hub says this model does, and whether it is the default."""
    return {"kind": "vision", "capabilities": ["vision"], "tasks": vision_tasks(model),
            "default": (model.get("raw") or {}).get("default") is True}


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
    # **What a Hub music lane states about lyrics, relayed.** YuE2's contract declares `lyrics`
    # as `required: true` ("this engine sings; there is no instrumental switch") and the Hub
    # refuses a request without them; Stable Audio 3 declares none. Published as
    # `requires_lyrics` / `supports_vocals` / `supports_instrumental` — fields Story Studio
    # already parses — because it picks the first catalogue row when the Music page names no
    # model, and the first row here is YuE2 (audit 2026-10-01): every instrumental top-up
    # would have been refused at the Hub. Only what the lane stated is published.
    contract = raw.get("lane_contract") if isinstance(raw, dict) else None
    lyrics = _hub_declared_parameter(contract, "lyrics") if isinstance(contract, dict) else None
    vocal: dict[str, Any] = {}
    # The lane's own track-length bounds and default, so a caller can clamp and explain instead of
    # being refused: Stable Audio 3 takes 1-380 s (default 30), YuE2 8-360 s (default 36).
    length = _hub_declared_parameter(contract, "duration_seconds") if isinstance(contract, dict) else None
    if length is not None:
        for field, key in (("min_duration_seconds", "minimum"), ("max_duration_seconds", "maximum"),
                           ("default_duration_seconds", "default")):
            value = length.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                vocal[field] = int(value) if float(value).is_integer() else float(value)
    if lyrics is not None:
        vocal["supports_vocals"] = True
        if lyrics.get("required") is True:
            vocal["requires_lyrics"] = True
            vocal["supports_instrumental"] = False
    row = {
        "coming_soon": False,
        # ponytail: flat caps for every music model, deliberately at the
        # conservative end — the same trade the image lane records at
        # `max_prompt_characters`. Upgrade path — per-model values from the
        # provider, or the owner's curated file (which is now actually read; it
        # advertised `max_prompt_characters` and nothing consulted it).
        "max_prompt_characters": max_prompt_characters,
        "max_prompt_characters_source": max_prompt_source,
        # ponytail: flat metadata caps for every music model, ours, at the
        # conservative end — the same trade the image lane records at
        # `max_prompt_characters`. Ceiling — a model that accepts longer lyrics or
        # a longer title is described narrowly, and one that accepts less is
        # over-described. Upgrade path — per-model values from the provider, or the
        # owner's curated file, once a provider states either field.
        "max_lyrics_characters": 4000,
        "max_title_characters": 200,
        "supports_instrumental": instrumental if isinstance(instrumental, bool) else None,
        "supported_languages": languages,
    }
    row.update(vocal)
    row.update(_output_formats(definition, raw))
    return row


def _slots_by_location(modality: str) -> dict[str, int]:
    """Each Studio Hub location's live total for one modality, as admission enforces it.

    Published per row as `provider_submit_concurrency`. For TTS Story Studio reads it as how many
    sections of **one** narration it may have in flight (`resolveGenStudioTtsFanout`); absent, it
    runs them one at a time. Images carry it the same way. It is the same `live_capacity` figure
    `paid_concurrency_limit` sums (the owner's decision D: "what the fleet can actually do"), per
    site rather than per model, so a model the smaller Macs of a site cannot run is offered a
    little more than it gets; the surplus waits in the gateway's queue, which re-binds it to
    whichever site frees first.
    """
    try:
        from . import concurrency  # lazy: concurrency reads the fleet, which reads this module
        rows = concurrency.live_capacity()["locations"]
    except Exception:  # noqa: BLE001 - a figure must never break the catalogue
        return {}
    return {
        str(row.get("provider_id")): int((row.get("by_modality") or {}).get(modality) or 0)
        for row in rows
    }


_ROW_BUILDERS = {
    "image": _image_row,
    "video": _video_row,
    "tts": _tts_row,
    "transcription": _transcription_row,
    "chat": _chat_row,
    "music": _music_row,
    "embeddings": _embeddings_row,
    "vision": _vision_row,
}


#: Why a row is locked, in words true for **both** conditions that lock one: no site answering,
#: and every answering site stating it has no Mac for this model. The earlier sentence named only
#: the first — "every Studio Hub location that offers it is offline or not taking customer work" —
#: and read as a lie the moment a controller was up and merely had no Mac for the model (1600
#: answers, nine Macs reachable, `eligible 0` for Gemma 4). Story Studio prints this sentence
#: beside the row and the submit refuses with these same words, so both must be true together.
_NO_MAC_REASON = (
    "No Mac able to run this model is available right now — every Studio Hub location "
    "that offers it is offline or has no eligible machine."
)


def _publish_lock(row: dict[str, Any], locked: bool, reason: str | None) -> None:
    """Write every spelling of "can this be offered right now" together.

    `locked`/`lock_reason` and `availability` are one fact in two vocabularies —
    the gateway's own pair and the capability catalogue's. They were written by
    hand in `_story_row` and only `locked`/`lock_reason` were re-merged, so a
    model offered by three locations with one answering published
    `locked: false` beside `availability: "unavailable"` (measured live: two
    `studio-hub` TTS rows while this Mac's Hub was down and both controllers
    were up). A reader that branches on one and prints the other gets whichever
    location happened to be iterated first. One writer, so they cannot disagree.

    **`available` is the same fact a third time**, and it is the one Story Studio's
    transcription parser gates on (`available === true`; absent means unavailable).
    It was a hardcoded `True` on every row, so a model whose every Mac was down —
    or whose only Hub was an agent, drained or unreachable — published
    `locked: true` beside `available: true`, and Story's picker offered it. Written
    here rather than in the row literal for the same reason as the other two: the
    merge loop calls this to unlock a row a later location can serve, and a field
    written anywhere else keeps the first site's answer.

    `lock_reason` is the **fleet's own** sentence about the outage. It is never a
    billing or account code: Story Studio reads this field to explain a locked row,
    and "every Mac offering this model is unreachable" and "requires a paid
    account" send the owner to two different remedies.

    **And the same sentence is the first entry of `activation_blockers`**, the list
    Story Studio's health route joins into the sentence it shows. So it is replaced
    here rather than left behind: `rebuild` unlocks a row the moment *any* location
    offering it answers (`_publish_lock(existing, False, None)`), and the first site's
    outage sentence must not survive that — the merged row published `locked: false`
    beside "No Mac offering this model is reachable", the same disagreement in a
    fourth spelling.
    """
    previous = row.get("lock_reason")
    row["locked"] = locked
    row["lock_reason"] = reason
    row["availability"] = "unavailable" if locked else "available"
    row["available"] = not locked
    blockers = [entry for entry in row.get("activation_blockers") or [] if entry != previous]
    if reason and str(reason) not in blockers:
        blockers.insert(0, str(reason))
    row["activation_blockers"] = blockers


def _merge_capabilities(existing: dict[str, Any], row: dict[str, Any], modality: str) -> None:
    """Fold a second location's capability facts into the row already published.

    A location is a provider internally, but its models are published once per
    **maker** — so the merged row answers for every site that offers it, and each
    field needs a direction rather than "whichever site came first":

      * **`supports_voice_cloning`** — the union. A voice picker is offered when
        *any* site can use a reference clip; a location that cannot simply never
        receives that work.
      * **`maximum_input_characters`** — the **smallest**. The caller is told what
        it may submit, and it does not choose the site, so the number that is safe
        everywhere is the only honest one (the local lane's 15,000 is per-location
        because sectioning is the Hub's own decision). Its `_source` follows the
        value it came with, or the two would describe different numbers.
      * **speed range** — the **intersection** (`max` of the minimums, `min` of the
        maximums, and `supports_speed` only if every site has it), for the same
        reason: a caller picks one speed and may land anywhere.
      * **`supports_reference_images`** — the **union**, and its limits the **intersection of the
        locations that advertise it**. See `_merge_reference_capability` for why this is the one
        capability that goes the opposite way to `supports_voice_cloning`'s.

    **And every other published ceiling takes the minimum, for every modality.** This
    function used to `return` immediately for anything that was not TTS, so a merged row kept
    **the first location's** token limits, prompt ceiling and file-size cap (C4/M10). A
    Hub-side limit change on the second site then never reached Story Studio, and — worse —
    the row could advertise a context window that one of the machines offering it would
    refuse. The rule is the same one the TTS numbers already followed: the caller does not
    choose the site, so the number that is safe everywhere is the only honest one. Each
    value's `*_source` and `*_source_detail` travel with the number they describe, because a
    value and its provenance must come from one place or they disagree.
    """
    # **Ceilings: the smallest wins, whatever the modality.** `(value, source, detail)` — the
    # three keys that describe one stated limit, kept together so a merge can never publish a
    # number beside another site's explanation of it.
    for value_key, source_key, detail_key in _MERGED_CEILINGS:
        current = existing.get(value_key)
        incoming = row.get(value_key)
        if isinstance(incoming, (int, float)) and not isinstance(incoming, bool):
            if not isinstance(current, (int, float)) or isinstance(current, bool) \
                    or incoming < current:
                existing[value_key] = incoming
                if source_key in row:
                    existing[source_key] = row.get(source_key)
                if detail_key in row:
                    existing[detail_key] = row.get(detail_key)
    # **Summed, unlike every ceiling above**: each site offering the model adds its own Macs to
    # how many sections of one narration (or images of one episode) can run at once. `None`
    # (absent) when no site states one.
    if "provider_submit_concurrency" in existing or "provider_submit_concurrency" in row:
        existing["provider_submit_concurrency"] = (
            (existing.get("provider_submit_concurrency") or 0)
            + (row.get("provider_submit_concurrency") or 0)
        ) or None
    if modality == "image":
        _merge_reference_capability(existing, row)
    if modality != "tts":
        return
    if row.get("supports_voice_cloning"):
        existing["supports_voice_cloning"] = True
    if not row.get("supports_speed"):
        existing["supports_speed"] = False
    for key, pick in (("minimum_speed", max), ("maximum_speed", min)):
        current = existing.get(key)
        incoming = row.get(key)
        if isinstance(current, (int, float)) and isinstance(incoming, (int, float)):
            existing[key] = pick(current, incoming)


def _merge_reference_capability(existing: dict[str, Any], row: dict[str, Any]) -> None:
    """Reference capability and its limits across the locations offering one image model.

    **The capability is the union; the limits are the intersection of the sites that have it**
    (owner, 2026-10-26, replacing the plan's intersection-of-everyone).

    Reference support is a fact about *which Macs* hold the weights and the memory, and the fleet is
    uneven: one location whose Macs are all below the edit floor must not hide the feature from every
    other site. The defect that decision guards against — publishing a capability a site cannot
    serve — is handled where it belongs, at routing: `fleet.routing_candidates` skips a location whose
    worker does not advertise `edit`, so a non-capable location never receives reference work.

    The numbers therefore may only be constrained by locations that can receive the work. `min` is
    the **largest** of theirs and `max` / `max_dimension` the **smallest**, so every location an
    edit job can reach honours what is published. A location that does not advertise contributes
    nothing — its own rows say `0` references, and taking that as a ceiling would be the same
    fleet-wide suppression in a different spelling.

    These two ceilings are deliberately **not** in `_MERGED_CEILINGS`: that table takes the smallest
    value from *every* location, which is the right rule for a caller who may land anywhere and the
    wrong one for a capability that routes.
    """
    current = existing.get("supports_reference_images")
    incoming = row.get("supports_reference_images")
    # Taken before either is overwritten: a location that does not advertise must contribute
    # nothing to the limits below, and its own rows say `0` references.
    advertising = [candidate for candidate, state in ((existing, current), (row, incoming))
                   if state is True]
    if current is True or incoming is True:
        state: bool | None = True
    elif current is False or incoming is False:
        state = False
    else:
        state = None  # nobody stated it: unstated is not "no"
    existing["supports_reference_images"] = state
    existing["supports_image_to_image"] = state
    # **`operations` follows the merged capability, not the first location's.** Otherwise a fleet
    # whose first-iterated site cannot edit would publish `supports_reference_images: true` beside
    # an operation list that omits image-to-image — the same self-contradiction that gave the
    # original suppression away.
    if state is True:
        existing["operations"] = ["text-to-image", "image-to-image"]
    elif state is False:
        existing["operations"] = ["text-to-image"]
    if state is not True:
        existing["min_reference_images"] = 0 if state is False else None
        existing["max_reference_images"] = 0 if state is False else None
        existing["reference_image_max_dimension"] = None
        return

    def bound(key: str, pick: Any) -> int | None:
        stated = [
            candidate.get(key)
            for candidate in advertising
            if isinstance(candidate.get(key), int) and not isinstance(candidate.get(key), bool)
        ]
        return pick(stated) if stated else None

    existing["min_reference_images"] = bound("min_reference_images", max)
    existing["max_reference_images"] = bound("max_reference_images", min)
    existing["reference_image_max_dimension"] = bound("reference_image_max_dimension", min)


#: One stated upper bound, and the two keys that say where it came from.
#:
#: `maximum_input_characters` was the only entry before C4 and only for TTS; the token windows,
#: the prompt ceiling and the upload cap were left at the first location's value on every other
#: modality. Listed as data rather than repeated in the loop so the next ceiling added to a row
#: has one obvious place to be registered — and so a reader can see the whole set at once.
_MERGED_CEILINGS: tuple[tuple[str, str, str], ...] = (
    ("context_window_tokens", "context_window_tokens_source",
     "context_window_tokens_source_detail"),
    ("max_output_tokens", "max_output_tokens_source", "max_output_tokens_source_detail"),
    ("max_prompt_characters", "max_prompt_characters_source",
     "max_prompt_characters_source_detail"),
    ("maximum_input_characters", "maximum_input_characters_source", ""),
)


class Catalog:
    def __init__(self) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {m: [] for m in MODALITIES}
        self._built_at = 0.0
        self._inventory_oldest: tuple[str, float] | None = None
        self._signature: tuple = ()
        # **Injected, never imported.** `runtime` imports this module at its own module
        # level, so a module-level `from .runtime import store` here would be a cycle —
        # and the lazy-import version silently produced no measurement at all, because
        # `catalog = Catalog()` runs during this module's import, before `runtime` has
        # built the store. Explicit attachment after construction is the only shape
        # that is both cycle-free and actually wired.
        self._store: Any = None
        self._refresher: threading.Thread | None = None
        self.rebuild(force=True)

    def attach_store(self, store: Any) -> None:
        """Give the catalogue the job store it measures from, and rebuild once."""
        self._store = store
        self.rebuild(force=True)

    def start_refresher(self, interval: float = REFRESH_EVERY_S) -> None:
        """Rebuild in the background so a catalogue read never builds anything.

        0.2.161 rebuilt inside the request whenever the 20 s TTL had passed, and every
        catalogue route was a sync handler, so a read needed one of the server's 40
        worker threads. When those were all busy (16:15 on 2026-10-09, 40 artifact-tree
        walks), `/v1/models` waited minutes behind them while `/health` answered at once.
        With this running, reads skip the TTL rebuild, and the routes run on threads of their
        own (`api._CATALOG_THREADS`) with an async auth check. Started from
        the app's lifespan, so a test or CLI without it keeps rebuilding on read.
        """
        if self._refresher is not None and self._refresher.is_alive():
            return

        def loop() -> None:
            while True:
                try:
                    self.rebuild()
                except Exception:  # noqa: BLE001 - the refresher must outlive any one bad rebuild
                    logging.getLogger("gateway.catalog").exception("catalogue rebuild failed")
                time.sleep(interval)

        self._refresher = threading.Thread(target=loop, name="catalog-refresher", daemon=True)
        self._refresher.start()

    def _fresh(self) -> None:
        """With the refresher running, a read rebuilds only for a change it can see (the ~1 ms
        signature), so a model switched on is in the very next answer; the TTL rebuild, which
        reads the fleet and the job store, stays in the background."""
        if self._refresher is None or not self._refresher.is_alive():
            self.rebuild()
        elif self._current_signature() != self._signature:
            self.rebuild(force=True)

    # -- building ---------------------------------------------------------
    def _current_signature(self) -> tuple:
        """Cheap fingerprint of catalogue-affecting state.

        Rebuilding on a timer alone would leave a toggle invisible for up to
        `REBUILD_TTL_S`; rebuilding on every request would re-sort hundreds of
        models per poll. The signature gives immediacy with a cheap check.

        **Which locations are answering is part of the fingerprint.** A row's
        `locked`/`availability` is derived from it (`_story_row`), and leaving it out
        of the signature meant a site that answered again — or went quiet — kept
        publishing the previous verdict until the 20 s TTL ran out. Measured live
        2026-10-30: terranash-1600 came back at 14:28:26 and its model stayed locked
        until 14:28:46. The fleet read is the same cached, non-blocking one
        `rebuild` already takes (`snapshot(allow_stale=True)`), so the check stays
        ~O(1) and the catalogue lock/unlock is never older than the fleet answer it
        describes.
        """
        parts: list[tuple] = []
        for definition in registry.definitions():
            if not registry.credentials.is_enabled(definition.id):
                continue
            if not registry.credentials.is_configured(definition.id):
                continue
            for model in registry.models.models_for(definition.id, include_disabled=False):
                parts.append((definition.id, model["model_id"], model["modality"]))
        from . import fleet

        return tuple(sorted(parts)), tuple(sorted(fleet.usable_location_ids()))

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

        # **What the work itself measured, once per rebuild.** Read here rather than at
        # request time because this is already the cached boundary — `REBUILD_TTL_S`
        # is 20 s — and because the read is a single SQL query that extracts four
        # numbers and leaves the generated prose in the file. `{}` when no store is
        # attached: a model with no measurement publishes `measured: null`, which is
        # unknown and not zero.
        measured: dict[tuple[str, str], dict[str, Any]] = {}
        if self._store is not None:
            from . import qualification

            measured = qualification.published_index(self._store)

        # The same answer the signature was measured from, so the rows and the
        # fingerprint that decides whether to rebuild cannot describe two moments.
        # (A test that stubs the signature with `()` gets a fresh read, never an
        # IndexError — the tuple's second half is an addition, not a contract.)
        usable = set(signature[1]) if len(signature) > 1 else fleet.usable_location_ids()
        # **The site rows, read once for the whole rebuild.** `usable` above came from this same
        # cached `snapshot(allow_stale=True)` — the one `usable_location_ids` and the signature
        # already took — so this is the same fleet read, not a second one. Each location's own row
        # goes to `_story_row` with its model, because a site can be answering and still state it
        # has no Mac for that model (see `states_no_mac`); a second walk per row would be the
        # per-model cost this cache exists to avoid.
        location_rows = {str(r.get("id")): r for r in fleet.snapshot(allow_stale=True).get("locations") or []}
        slots = {m: _slots_by_location(m) for m in ("tts", "image")}
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
                row = self._story_row(
                    modality, definition, model, usable=usable,
                    location_row=location_rows.get(fleet.controller_id_from(definition.id) or ""),
                )
                if modality in slots and definition.kind == KIND_STUDIO_HUB:
                    row["provider_submit_concurrency"] = slots[modality].get(definition.id) or None
                # **The publisher's number and ours, side by side and never merged.**
                # `max_output_tokens` above stays exactly what the producer states;
                # `measured` is this gateway's own record of what the lane has really
                # carried, labelled `source: "gateway"` inside it. A reader that only
                # wants the ceiling is unaffected — the key is additive — and a reader
                # that wants to know whether the ceiling is real can now see how close
                # the work has actually come to it.
                row["measured"] = measured.get(
                    (fleet.publish_id(definition.id), model["model_id"])
                )
                existing = merged.get(row["id"])
                if existing is None:
                    merged[row["id"]] = row
                    continue
                # Locking means "no location offering this can take it", so one
                # answering location is enough to offer it — and `availability` is
                # the same fact in the capability vocabulary, so it is written by
                # the same call rather than left holding the first site's answer.
                if not row["locked"]:
                    _publish_lock(existing, False, None)
                # Every location's previous site-scoped id stays an alias, so a
                # preset naming any of them still finds this one row.
                existing["legacy_ids"] = list(
                    dict.fromkeys([*existing["legacy_ids"], *row["legacy_ids"]])
                )
                # **The merged row is one model with N possible sites, so its
                # capability facts have to answer for all of them.** Only `locked`
                # used to merge; everything else kept the *first* location's answer,
                # so a model two Hubs describe differently was published as whichever
                # site happened to be iterated first — a `supports_voice_cloning`
                # beside an empty picker, or a character budget only one site
                # honours. Merged in one place, with a stated direction per field.
                _merge_capabilities(existing, row, modality)
            rows[modality] = list(merged.values())

        # Image pools: synthesize one combined row per image pool (alias layer).
        try:
            image_rows_by_id = {r["id"]: r for r in rows["image"]}
            for pool in pools.image_pools():
                alias = f"studio-hub::{pool['name']}"
                display = pool.get("display_name") or pool["name"]
                # Every model a Mac may be assigned for this pool (per-RAM tier, plus the owner's pins).
                member_ids = image_assignment.pool_models(pool)
                if not member_ids:
                    continue
                member_rows = []
                for member in member_ids:
                    row_id = f"studio-hub::{member}"
                    if row_id in image_rows_by_id:
                        member_rows.append((member, image_rows_by_id[row_id]))
                if not member_rows:
                    continue
                first_row = member_rows[0][1]
                # intersection of aspect ratios
                ratio_sets = [set(r.get("aspect_ratios") or []) for _, r in member_rows]
                common_ratios = set.intersection(*ratio_sets) if ratio_sets else set()
                # preserve first member order
                ordered_common = [r for r in (first_row.get("aspect_ratios") or []) if r in common_ratios]
                # output options intersection by (ratio,width,height)
                option_map = {}
                for member, r in member_rows:
                    opts = r.get("output_options") or []
                    keys = set()
                    for o in opts:
                        keys.add((o.get("aspect_ratio"), o.get("width"), o.get("height")))
                    option_map[member] = keys
                common_option_keys = set.intersection(*[v for v in option_map.values()]) if option_map else set()
                # build options from first member matching common keys
                new_options = []
                for o in (first_row.get("output_options") or []):
                    if (o.get("aspect_ratio"), o.get("width"), o.get("height")) in common_option_keys:
                        new_options.append(dict(o))
                default_ratio = first_row.get("default_aspect_ratio")
                if default_ratio not in common_ratios:
                    default_ratio = ordered_common[0] if ordered_common else first_row.get("default_aspect_ratio")
                supports_seed = all(bool(r.get("supports_seed")) for _, r in member_rows)
                # **A pool alias is a menu of models, so what it publishes must hold for every
                # member** — the member that answers is chosen at routing, not by the caller. That
                # is the opposite of a location merge above (`_merge_reference_capability`), where
                # the capability is a union because the reference job is routed to a location that
                # has it; here any member may take the job, so one that takes no references takes
                # the whole alias down to none.
                supports_refs = all(bool(r.get("supports_reference_images")) for _, r in member_rows)
                if supports_refs:
                    min_refs = max((r.get("min_reference_images") or 0) for _, r in member_rows)
                    max_refs = min((r.get("max_reference_images") or 0) for _, r in member_rows)
                    dimensions = [r.get("reference_image_max_dimension") for _, r in member_rows
                                  if isinstance(r.get("reference_image_max_dimension"), int)]
                    max_dimension = min(dimensions) if dimensions else None
                else:
                    min_refs, max_refs, max_dimension = 0, 0, None
                # operations intersection
                op_sets = [set(r.get("operations") or []) for _, r in member_rows]
                common_ops = set.intersection(*op_sets) if op_sets else set()
                if not common_ops:
                    common_ops = {"text-to-image"}
                # max_prompt_characters min
                mpc = None
                mpc_source = None
                for _, r in member_rows:
                    val = r.get("max_prompt_characters")
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        if mpc is None or val < mpc:
                            mpc = val
                            mpc_source = r.get("max_prompt_characters_source")
                if mpc is None:
                    mpc = first_row.get("max_prompt_characters")
                    mpc_source = first_row.get("max_prompt_characters_source")
                locked = not any(not r.get("locked") for _, r in member_rows)
                lock_reason = None
                if locked:
                    lock_reason = _NO_MAC_REASON
                row = dict(first_row)
                row["id"] = alias
                row["display_name"] = display
                row["customer_handle"] = alias
                row["public_model_id"] = alias
                row["legacy_ids"] = [alias]
                row["pool_members"] = [{"id": m, "display_name": image_rows_by_id.get(f"studio-hub::{m}", {}).get("display_name", m)} for m in member_ids]
                row["aspect_ratios"] = ordered_common
                row["default_aspect_ratio"] = default_ratio
                row["output_options"] = new_options
                row["supports_seed"] = supports_seed
                row["supports_reference_images"] = supports_refs
                row["supports_image_to_image"] = supports_refs
                row["min_reference_images"] = min_refs
                row["max_reference_images"] = max_refs
                row["reference_image_max_dimension"] = max_dimension
                row["operations"] = sorted(common_ops)
                row["max_prompt_characters"] = mpc
                row["max_prompt_characters_source"] = mpc_source
                row["chargeable"] = False
                row["locked"] = locked
                row["lock_reason"] = lock_reason
                row["availability"] = "unavailable" if locked else "available"
                row["available"] = not locked
                row["activation_blockers"] = [lock_reason] if lock_reason else []
                # price fields already zero/local from first row
                row["pool_id"] = pool["name"]
                rows["image"].append(row)
        except Exception:
            pass

        # A pool can cost money only if a member that can answer can.
        chat_by_id = {r["id"]: r for r in rows["chat"]}
        for row in rows["chat"]:
            provider_id, name = decode_model_id(row["id"])
            if provider_id == pools.POOL_PROVIDER_ID:
                members = [chat_by_id.get(m) for m in pools.members(name)]
                row["chargeable"] = any(m["chargeable"] for m in members if m is not None)

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
        location_row: dict[str, Any] | None = None,
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
                # The outage in the fleet's own words, naming what the caller can act on
                # (a Mac to check) rather than a vague "unavailable" — and never an
                # account or billing code, which is what Story Studio's locked-row
                # sentence is built from: `requires_paid_account` is the only reason it
                # renders as an account restriction, and a dead fleet is not one.
                lock_reason = _NO_MAC_REASON
            elif location_row is not None and fleet.states_no_mac(location_row, model["model_id"]):
                # **The site answers, but it states it has no Mac for this model.** The reachable
                # count is not the question: 1600 answers with nine Macs up and still has none
                # for Gemma 4, so the old test — "is the controller answering?" — left the row
                # unlocked and the submit accepted a job nothing could run. `states_no_mac` is
                # the same function `routing_candidates` asks, so the row and the door can never
                # tell two stories; and when no row is at hand this check is skipped rather than
                # guessed — "not checked" is not "failed".
                locked = True
                lock_reason = _NO_MAC_REASON
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
            # Publish `qualification_only` EXPLICITLY: the parser reads
            # `qualification_only !== false` (absent means hidden). Availability is
            # **not** written here — `available`, `locked`, `lock_reason` and
            # `availability` are one fact in three vocabularies, and `_publish_lock`
            # below writes all of them. This literal used to carry a hardcoded
            # `"available": True` beside a `locked: true` from that same call.
            "qualification_only": False,
            # The capability catalogue's own vocabulary; see _CAPABILITY_* above.
            "modality": _CAPABILITY_MODALITY.get(modality, "text"),
            "operation": _CAPABILITY_OPERATION.get(modality, "chat.response"),
        }
        _publish_lock(row, locked, lock_reason)

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

        # **Why this row cannot be used right now, in the gateway's own words.**
        #
        # Story Studio reads `activation_blockers` and joins it into the sentence
        # it shows when a model is not usable (`routes/health.ts`), falling back to
        # "This model is not currently available in GenStudio". Nothing published
        # the field, so that fallback is what an operator always saw — while the
        # reason sat in this very row as `lock_reason`, unread by the only caller
        # that wanted it.
        #
        # Which is *not* the same fact as "there is no reason". An empty list means
        # this row is usable; a non-empty one names each reason, and each entry is
        # something a producer stated — the gateway's lock, or the Hub's own reason
        # for a package it cannot place. Nothing is inferred from a model name.
        blockers: list[str] = []
        if lock_reason:
            blockers.append(str(lock_reason))
        hub_reason = str((model.get("raw") or {}).get("reason") or "").strip()
        if hub_reason and hub_reason not in blockers:
            blockers.append(hub_reason)
        row["activation_blockers"] = blockers

        # Price constants merged LAST so they always win and stay consistent.
        row.update(zero_pricing(modality))
        # **The truth beside the shape.** `zero_pricing` above only keeps Story's parser from
        # dropping the row; these say whether a price exists and who stated it. Additive, and
        # never derived from the zeros.
        row.update(row_price(
            definition.kind, modality, model.get("raw"),
            None if is_location else registry.models.cached(definition.id).get("fetched_at"),
            definition.id, model["model_id"],
        ))
        if modality == "video":
            # **What the price is per.** Every cloud video endpoint here bills by the second, and
            # `price_usd`'s own key says which unit the provider stated. Published separately so a
            # caller multiplying it by a duration does not have to guess, and absent when nobody
            # stated a price — never "free".
            usd = row.get("price_usd")
            if isinstance(usd, dict) and usd:
                row["price_basis"] = next(iter(usd))
        # **Free or paid, truthfully, on every row** — the Models page's Free/Paid as a bool, so a
        # model on the owner's Macs (`price_source: local`) says `false`. `zero_pricing` above only
        # publishes the shape. Story decides reference images and paid replacement by capability
        # (since its fix/media-truth), not by this flag; an older Story read `false` on an image row
        # as "no references". A pool's answer is its members', set once the chat rows exist.
        row["chargeable"] = chargeable(getattr(definition, "free_tier", False), model["model_id"], row)
        return row

    # -- reads ------------------------------------------------------------
    def rows(self, modality: str) -> list[dict[str, Any]]:
        self._fresh()
        return self._rows.get(modality, [])


    def all_rows(self) -> list[dict[str, Any]]:
        self._fresh()
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

        Since 0.2.162 the live gateway rebuilds in the background (`start_refresher`), so
        `ageMs` can reach `REBUILD_TTL_S` (20 s); `stale` (30 min) is still unreachable
        while the refresher runs.
        """
        self._fresh()
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
        job_key: str | None = None,
        require_capability: str | None = None,
    ) -> tuple[ProviderDefinition | None, dict | None]:
        """Resolve a namespaced model id against currently enabled models.

        Never trust the caller's id: resolution is scoped to enabled providers
        and enabled models so a disabled toggle is honoured on the generation
        path, not just in the catalogue.

        A `studio-hub::` id names the maker, not a site, so resolving it is also
        where the location is chosen. `only_location` pins that choice — it is how
        a voice job is kept at the location that actually holds the voice, since a
        reference clip lives on one Hub and not on the fleet.

        `job_key` is the lottery's seed. It used to be the model id, which is the same for
        every job of a model, so a burst of equal-rank jobs all drew the same location
        (measured: 20 same-second music jobs, all bound to one site, this Mac given none).
        A submit passes one key per job, so equal ranks are spread rather than stacked.

        `require_capability` is a token the chosen location's worker must advertise (`edit` for a
        reference-guided image). It is the request's own requirement rather than a preference, so a
        location that does not advertise it is not a candidate at all — see
        `fleet.routing_candidates`. `None` asks nothing extra and resolves exactly as before.
        """
        from . import fleet
        from . import pools

        provider_id, model_id = decode_model_id(compound)
        pool_name = pools.image_pool_name(compound)
        if pool_name is not None and modality == "image":
            chosen_def, chosen_member, _note = fleet.route_pool(
                image_assignment.pool_models(pools.get(pool_name)), modality,
                routing_key=job_key or compound, only_location=only_location,
                require_capability=require_capability,
            )
            if chosen_def is None:
                return None, None
            for definition, model in registry.models.enabled_models(modality):
                if definition.id == chosen_def.id and model["model_id"] == chosen_member:
                    return definition, model
            return None, None
        if fleet.is_paused_provider(provider_id):
            # A legacy `studio-hub-<site>::…` id names one location; while the owner has it paused
            # it routes like the fleet id instead of running there.
            provider_id = fleet.FLEET_PROVIDER_ID
        if provider_id == fleet.FLEET_PROVIDER_ID:
            chosen, _note = fleet.route(
                model_id, modality, routing_key=job_key or compound, only_location=only_location,
                require_capability=require_capability,
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

    def why_no_route(
        self, modality: str, compound: str, *, only_location: str | None = None,
        require_capability: str | None = None,
    ) -> str | None:
        """Why an **offered** fleet model has no route this moment.

        Returns `None` when the id is not an enabled fleet model at all — a real
        `MODEL_NOT_FOUND` — and a sentence naming the site condition otherwise.
        The 404 this replaces said "not available on this gateway" while the model
        sat in the picker and every location offering it was simply offline:
        measured live with both controllers down, which sends the reader looking for
        a catalogue problem instead of a network one.

        `require_capability` is the request's own demand (`edit` for a reference-guided image), and
        it is named in the sentence rather than left as "no free slot": when every location offering
        the model simply cannot do this kind of work, a caller told to retry later would retry for
        ever.
        """
        from . import fleet
        from . import pools

        provider_id, model_id = decode_model_id(compound)
        if pools.image_pool_name(compound) is not None and modality == "image":
            pool = pools.get(pools.image_pool_name(compound))
            members = image_assignment.pool_models(pool) if pool else []
            return ("no Mac is assigned one of this pool's models and free to run it: " +
                    ", ".join(members)) if members else "this image pool names no model for any Mac"
        if fleet.is_paused_provider(provider_id):
            provider_id = fleet.FLEET_PROVIDER_ID
        if provider_id != fleet.FLEET_PROVIDER_ID:
            return None
        offered: list[str] = []
        incapable = 0
        for definition, model in registry.models.enabled_models(modality):
            if model["model_id"] != model_id:
                continue
            location_id = fleet.controller_id_from(definition.id)
            if location_id is None:
                continue
            if only_location is not None and location_id != only_location:
                continue
            offered.append(location_id)
            if require_capability and not fleet.advertises(model, require_capability):
                incapable += 1
        if not offered:
            return None
        rows = {
            str(row.get("id")): row
            for row in (fleet.snapshot(allow_stale=True).get("locations") or [])
        }
        offline = cannot = full = paused = 0
        for location_id in offered:
            row = rows.get(location_id) or {}
            if not row.get("reachable"):
                offline += 1
            elif fleet._location_disk_stopped(row):  # noqa: SLF001
                full += 1
            elif fleet.owner_paused_at(location_id) or fleet.no_free_mac(row, model_id):
                paused += 1
            elif not fleet._takes_customer_jobs(row):  # noqa: SLF001 - one rule, one place
                cannot += 1
        if incapable == len(offered) and not (offline or cannot or full or paused):
            return (
                f"No location offering this model can run this request right now — none of the "
                f"{len(offered)} location{'' if len(offered) == 1 else 's'} offering it reports the "
                f"{require_capability!r} capability it needs."
            )
        if not (offline or cannot or full or paused or incapable):
            return "No location offering this model has a free slot right now."
        reasons: list[str] = []
        if incapable:
            reasons.append(f"{incapable} that cannot run this request")
        if offline:
            reasons.append(f"{offline} location{'' if offline == 1 else 's'} offline")
        if cannot:
            reasons.append(f"{cannot} that cannot take customer work")
        if paused:
            reasons.append(f"{paused} you paused")
        if full:
            reasons.append(f"{full} whose Mac{'' if full == 1 else 's'} ran out of disk space")
        return (
            "No location offering this model can take work right now — "
            + " and ".join(reasons) + "."
        )


catalog = Catalog()
