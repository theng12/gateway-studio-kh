"""Provider definitions — the declarative core of the gateway.

Everything provider-specific lives here as data, not as bespoke code. Adding a
new provider means adding one `ProviderDefinition`; adding a new model to an
existing provider means either letting discovery find it, or adding one static
entry to that provider's `static_models`.

This mirrors the 17 cloud providers GenStudio KH already had configured, plus
the local Studio Hub lane. The ids are kept identical to GenStudio's so that
credentials and mental models transfer directly.

Design rules:

  * `kind` selects the adapter implementation (how to talk to the provider).
  * `discovery` selects how its model list is obtained.
  * `modalities` gates which Story Studio catalogues a provider can contribute to.
  * `credential_fields` drives the admin UI, so adding a field never requires
    touching frontend code.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..config import CUSTOM_PROVIDERS_PATH

log = logging.getLogger("gateway.providers")

# ---------------------------------------------------------------------------
# Adapter kinds
# ---------------------------------------------------------------------------
KIND_OPENAI_COMPATIBLE = "openai_compatible"
KIND_ANTHROPIC = "anthropic"
KIND_GOOGLE = "google"
KIND_FAL = "fal"
KIND_KIE = "kie"
KIND_BFL = "bfl"
KIND_MINIMAX = "minimax"
KIND_ELEVENLABS = "elevenlabs"
KIND_FISH = "fish"
KIND_STUDIO_HUB = "studio_hub"
#: xAI is OpenAI-compatible for chat, but its image and video APIs are its own
#: shape (no `size`, and video is deferred), so it needs a dedicated adapter.
KIND_XAI = "xai"
#: A pool is not a vendor — it is a list of *this gateway's own* model ids, tried
#: in order until one answers. It has its own kind because its adapter's job is
#: dispatch rather than HTTP: it resolves each member through the catalogue and
#: hands the request to whichever adapter that member belongs to, so a pool can
#: mix vendors freely and no member needs to know it is in one.
KIND_POOL = "pool"

#: Kinds that actually have a generation adapter.
#:
#: This is the single source of truth, and it is deliberately declared here
#: rather than discovered from `gateway.adapters` — `providers` must not depend
#: on `adapters`, and more importantly the catalogue needs to know *before*
#: offering a model whether it can be generated. A provider whose kind is
#: missing from this set is discoverable and configurable, but its models are
#: withheld from Story Studio: showing a model that fails on use is worse than
#: not showing it.
IMPLEMENTED_KINDS: frozenset[str] = frozenset(
    {
        KIND_OPENAI_COMPATIBLE,
        KIND_ANTHROPIC,
        KIND_GOOGLE,
        KIND_FAL,
        KIND_KIE,
        KIND_FISH,
        KIND_STUDIO_HUB,
        KIND_BFL,
        KIND_MINIMAX,
        KIND_ELEVENLABS,
        KIND_XAI,
        KIND_POOL,
    }
)

#: Transcription models that cannot honour the request this gateway makes.
#:
#: The gateway asks every cloud transcription lane for
#: `response_format: "srt"`, because that is what Story Studio consumes. On
#: OpenAI's side only `whisper-1` supports SRT: the `gpt-4o-transcribe` and
#: `gpt-transcribe` families accept `json`/`text` only and answer **400** to
#: anything else, and `gpt-4o-transcribe-diarize`, `gpt-live-transcribe` and
#: `gpt-realtime-whisper` are diarising or streaming endpoints rather than
#: transcription-with-timestamps. Google's OpenAI-compatible transcription route
#: does not exist at all, so every Google transcription row is impossible — that
#: one is refused by the modality gate in `gateway.adapters`, not here.
#:
#: Left tickable, each of these is a model the owner can switch on that then fails
#: the moment anything uses it — the state this codebase calls the worst one, and
#: the same trap as OpenRouter's `:batch` variants. They stay discoverable and
#: visible in the inventory; they are simply never published.
#:
#: ponytail: a curated exception list, because no provider *publishes* which
#: response formats it accepts. Ceiling — a new model that cannot produce SRT is
#: published until it is added here. Upgrade path — read the capability from the
#: provider where it states one, or request a format every model supports and
#: synthesise the SRT here; the timestamp-less families cannot support that
#: either, so this list would survive the change.
TRANSCRIPTION_SRT_UNSUPPORTED: frozenset[str] = frozenset(
    {
        "gpt-4o-mini-transcribe",
        "gpt-4o-mini-transcribe-2025-03-20",
        "gpt-4o-mini-transcribe-2025-12-15",
        "gpt-4o-transcribe",
        "gpt-4o-transcribe-diarize",
        "gpt-transcribe",
        "gpt-live-transcribe",
        "gpt-realtime-whisper",
        # **Groq's Whisper cannot answer in SRT either**, which is why its
        # transcription rows are withheld rather than offered: measured live on
        # 2026-09-20, `HTTP 400: response_format must be one of [json text
        # verbose_json]`. An earlier note in AGENTS.md claimed Groq transcription
        # worked through this gateway; it did not, and the cost of believing it is a
        # published model that fails the moment it is used.
        #
        # Matched on the bare id, which is safe **only** because the local Hub's
        # Whisper carries a namespaced id (`mlx-community/whisper-large-v3-turbo`)
        # that this does not match. ponytail: provider-blind, because every id here
        # happens to belong to one provider. Ceiling — a second provider serving
        # `whisper-large-v3` *with* SRT support would be withheld wrongly. Upgrade
        # path — key this on `(provider_id, model_id)` and pass the provider from
        # `can_publish`, which already holds the definition.
        "whisper-large-v3",
        "whisper-large-v3-turbo",
    }
)


#: Music models that cannot answer a one-shot generation.
#:
#: `lyria-realtime-exp` is a **bidirectional WebSocket session** — Google documents
#: only `client.aio.live.music.connect(...)` for it, with `set_weighted_prompts`,
#: `play()` and a stream of audio chunks. There is no unary generate call, so a
#: request through this gateway could never complete; it would publish and then
#: hang or fail. Withheld rather than implemented.
MUSIC_UNSERVABLE: frozenset[str] = frozenset({"lyria-realtime-exp"})


def music_can_serve(model_id: str) -> bool:
    """Whether this music model can answer a normal, single-shot request."""
    return model_id not in MUSIC_UNSERVABLE


#: The kinds whose **image** lane can actually put the caller's reference image
#: on the wire. `studio_hub` stages references and forwards them over the Hub's
#: contract. `kie` is deliberately excluded from this gate: although Kie's image
#: body carries fields like `image_url`, its image adapter does not read or
#: forward the Gateway's staged references (`_reference_paths`), so publishing
#: its reference-only/edit rows falsely claims the references would reach the
#: worker. Kie's text-to-image models continue to publish normally.
#: Every other image body here (`fal`, the OpenAI-compatible shim behind
#: `google`, `xai`, `minimax`, `bfl`) sends a prompt and nothing else — so an
#: endpoint those providers classify as `image-to-image` would be dispatched
#: without its required input. fal's queue accepts the body and validates at the
#: worker, which is how that mistake reads as a job that `COMPLETED` and then
#: failed on download.
#:
#: Measured live on 2026-09-20: fal classifies **116** of its image rows
#: `image-to-image` (upscalers, background removers, inpainters, `*/edit`
#: endpoints) and every one of them is unpublishable under this rule. None is
#: enabled, so nothing the owner could pick changed.
#:
#: ponytail: the lane is withheld rather than implemented blind. Ceiling — fal's
#: and the shim's image-to-image models are unavailable through the gateway.
#: Upgrade path — read the per-endpoint OpenAPI schema (the same source the body
#: schemas already come from) for the reference field's real name and arity
#: (`image_url` on some endpoints, `image_urls` on others), send it, and drop the
#: kind from this gate.
IMAGE_REFERENCE_CAPABLE_KINDS: frozenset[str] = frozenset({KIND_STUDIO_HUB})

#: The pixel geometry each aspect ratio is **sent as**, shared by the two things
#: that have to agree about it: the catalogue, which publishes it as the model's
#: `output_options`, and the OpenAI-compatible image body, which must send a size
#: the endpoint accepts. They were two different answers — the catalogue
#: advertised 1344x768 for 16:9 while `_size_for` sent 1792x1024, a DALL·E-3-era
#: size that `gpt-image-1` answers `400` for — so what was offered was not what
#: was requested.
#:
#: Only the three sizes the current OpenAI image APIs document are used; `4:3` and
#: `3:4` snap to the nearest of them rather than to a size nothing accepts.
#:
#: ponytail: two ratios share one geometry. Ceiling — a 4:3 request is actually
#: sent as 3:2. Upgrade path — a per-model size table read from the provider,
#: which is also what would let a lane that accepts more ratios offer them.
IMAGE_SIZE_BY_RATIO: dict[str, tuple[int, int]] = {
    "16:9": (1536, 1024),
    "4:3": (1536, 1024),
    "1:1": (1024, 1024),
    "3:4": (1024, 1536),
    "9:16": (1024, 1536),
}

#: The pixel geometry a **Studio Hub location's** Image lane actually renders, keyed by
#: the ratio ids its `lane_contract.parameters[aspect_ratio].enum` publishes.
#:
#: **This is the Hub's own preset table, not this gateway's**, and it is a separate table
#: because the two lanes are two different products: the cloud table above is what an
#: OpenAI-compatible endpoint is *sent*, while a Hub model was being described by that same
#: table and rendered by this one — 16:9 published 1536x1024 against a rendered 1280x720,
#: 4:3 published 1536x1024 against 1152x864, four of five sizes wrong by 23–33% linearly,
#: so Story composed at one geometry and received a file of another.
#:
#: Read from the installed Hub bundle, **not** a checkout, on 2026-09-28:
#: `/Applications/Studio Hub.app/Contents/Resources/components/image/app/backend/generation.py`
#: `SIZE_PRESETS` (Studio Hub **0.6.91**, the version the running Hub reported on
#: `/api/version` — build `92d7642`), and cross-checked against that Hub's own
#: `/api/hub/catalog` `image_sizes` for both image models, which listed these thirteen
#: presets and no others. Thirteen keys, twelve of them ratios: `16:9 small` is the same
#: 16:9 frame at 1024x576, the video-valid canvas (720 is not a multiple of 32) the Image
#: studio added on purpose, so it cannot share the `16:9` key.
#:
#: ponytail: a copy of the Hub's table, so a Hub that re-sizes a preset is described by the
#: old number until this is edited — the honest read is the Hub's `image_sizes` on its own
#: catalogue row, which `catalog._hub_image_sizes` already prefers the day discovery
#: carries it. Ceiling — a ratio the Hub adds publishes with no geometry rather than with a
#: guessed one; the ratio still reaches Story Studio, its size does not.
HUB_IMAGE_SIZE_BY_RATIO: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1024, 1024),
    "16:9 small": (1024, 576),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "3:2": (1200, 800),
    "2:3": (800, 1200),
    "5:4": (1120, 896),
    "4:5": (896, 1120),
    "21:9": (1680, 720),
    "2:1": (1408, 704),
    "1:2": (704, 1408),
}


def image_size_for(aspect_ratio: str, width: object = None, height: object = None) -> str:
    """The `size` string to send, given a ratio and optional explicit geometry.

    An explicit width/height is honoured only in *orientation* — the exact numbers
    a caller sends are not forwardable, because OpenAI-family endpoints reject
    arbitrary sizes — and the result always comes from `IMAGE_SIZE_BY_RATIO`, so
    the request cannot disagree with the geometry the catalogue published.
    """
    ratio = str(aspect_ratio or "16:9")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        chosen = IMAGE_SIZE_BY_RATIO.get(ratio) or IMAGE_SIZE_BY_RATIO["16:9"]
    elif width > height:
        chosen = IMAGE_SIZE_BY_RATIO["16:9"]
    elif width < height:
        chosen = IMAGE_SIZE_BY_RATIO["9:16"]
    else:
        chosen = IMAGE_SIZE_BY_RATIO["1:1"]
    return f"{chosen[0]}x{chosen[1]}"


#: Chat model families that accept only their own default temperature, keyed by
#: the provider whose API refuses any other value. Read by **both** the catalogue
#: (`catalog._chat_row` withholds the `temperature` control) and the adapters
#: (which drop a temperature the caller sent anyway) — what is offered is what is
#: sent. Story Studio sends `0.7` on every chat call, so without the adapter half a
#: model in this table failed every job with a definite `400`.
#:
#: * `openai-direct`: the reasoning families (`o1`-`o9`, `gpt-5`+). OpenAI's API
#:   publishes no `supported_parameters`.
#: * `anthropic-direct`: measured 2026-10-09 on the owner's gateway,
#:   `claude-haiku-5-5` answered `HTTP 400: \`temperature\` is deprecated for this
#:   model.` on three Story jobs. Anthropic documents sampling parameters as
#:   removed on Opus 4.7/4.8/5/5.5, Sonnet 5/5.5 and the Fable/Mythos line; Haiku
#:   4.5, Sonnet 4.5/4.6 and Opus 4.5/4.6 still take them. Anthropic's
#:   `/v1/models/{id}` `capabilities` tree states thinking, effort and the rest but
#:   nothing about sampling (read for both Haikus the same day).
#:
#: Proxies are left out **deliberately**: OpenRouter publishes its own list, and a
#: third party that accepts `0.7` (Experiential Labs' `gpt-5.6-luna` does) must
#: keep the control. `ponytail:` a hand-listed family table, not a measured
#: capability — no provider states it, the same shape as `_HARD_CAP_CHARACTERS`.
#: Ceiling — a default-only family reached through another provider keeps the
#: control, and a new Claude family under a new name is sent its temperature once:
#: `adapters.anthropic` retries that `400` without it and `learn_default_temperature_only`
#: remembers the model. Upgrade path — delete the table once a provider publishes
#: "temperature: default-only"; until then the retries in `openai_compat._chat` and
#: `adapters.anthropic` are the backstops.
DEFAULT_TEMPERATURE_ONLY: dict[str, re.Pattern[str]] = {
    "openai-direct": re.compile(r"(?:^|[/-])(?:o[1-9]|gpt-[5-9])(?:[-.]|$)"),
    "anthropic-direct": re.compile(
        r"^claude-(?:fable|mythos|opus-4-[7-9]|(?:opus|sonnet|haiku)-[5-9])(?:-|$)"
    ),
}


#: Models a provider refused a temperature for at run time (a definite `400`), so the
#: adapter stops sending it and the catalogue stops offering it from its next rebuild.
#: ponytail: in memory, lost on restart — the first such job after a restart pays one
#: retried `400` again. Upgrade path — add the family to the table above.
_LEARNED_DEFAULT_TEMPERATURE_ONLY: set[tuple[str, str]] = set()


def learn_default_temperature_only(provider_id: str, model_id: str) -> None:
    """Remember that this provider refused a temperature for this model."""
    _LEARNED_DEFAULT_TEMPERATURE_ONLY.add((provider_id, str(model_id or "").lower()))


def default_temperature_only(provider_id: str, model_id: str) -> bool:
    """Whether this provider refuses any temperature but the model's own default."""
    model = str(model_id or "").lower()
    if (provider_id, model) in _LEARNED_DEFAULT_TEMPERATURE_ONLY:
        return True
    pattern = DEFAULT_TEMPERATURE_ONLY.get(provider_id)
    return bool(pattern and pattern.search(model))

#: The words providers use for "this endpoint takes a reference image" and for
#: "this one takes text only". Read from the model's own statement — the Hub's
#: `capabilities` list (`["txt2img"]`, `["img2img"]`), fal's `category`
#: (`text-to-image`, `image-to-image`), or the endpoint id.
_IMAGE_REFERENCE_WORDS = ("img2img", "image-to-image", "reference-to-image", "edit")
_IMAGE_TEXT_WORDS = ("txt2img", "text-to-image")


def hub_image_modes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The Hub's modes, reduced across its actual worker rows, with 0.6.215 compatibility."""
    workers = raw.get("image_workers")
    workers = workers if isinstance(workers, list) and workers else [raw]
    modes: dict[str, dict[str, Any]] = {}
    labels = {"txt2img": "Text to image", "edit": "Reference to image"}
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        ready = all(worker.get(key) is not False for key in (
            "hub_cached", "memory_eligible", "runtime_ready", "available",
        ))
        if "image_modes" in worker:
            entries = worker["image_modes"]
            entries = entries if isinstance(entries, list) else []
        else:
            capabilities = worker.get("capabilities")
            capabilities = capabilities if isinstance(capabilities, list) else []
            contract = worker.get("lane_contract")
            references = contract.get("references") if isinstance(contract, dict) else None
            lane = worker.get("reference_lane")
            lane = lane if isinstance(lane, dict) else {}
            entries = []
            if "txt2img" in capabilities or "text-to-image" in capabilities:
                entries.append({"id": "txt2img", "available": ready, "reason": None})
            # Bounds describe a supported lane even when this Mac cannot run it now.
            reference_capability = any(token in capabilities for token in _IMAGE_REFERENCE_WORDS)
            if reference_capability or isinstance(references, dict) and references:
                entries.append({"id": "edit", "available": ready and (
                    lane.get("available") is True if "available" in lane else reference_capability
                ), "reason": lane.get("reason"), "references": references})
        for item in entries:
            if not isinstance(item, dict) or item.get("id") not in labels:
                continue
            mode = dict(item)
            mode["label"] = labels[mode["id"]]
            mode["available"] = ready and item.get("available") is True
            if mode["available"]:
                mode["reason"] = None
            elif not mode.get("reason"):
                mode["reason"] = "No ready Mac currently offers this mode."
            existing = modes.get(mode["id"])
            workers_evidence = list(existing.get("workers") or []) if existing else []
            workers_evidence.extend(mode.get("workers") or [])
            machine = worker.get("hub_machine")
            if isinstance(machine, str) and machine:
                workers_evidence.append({"machine_id": machine, "available": mode["available"],
                                         "reason": mode.get("reason"),
                                         **({"references": mode["references"]} if "references" in mode else {})})
            if existing is None or mode["available"] and not existing["available"]:
                modes[mode["id"]] = mode
            elif mode["available"] and existing["available"] and mode["id"] == "edit":
                first, second = existing.get("references"), mode.get("references")
                references = dict(first) if isinstance(first, dict) else {}
                for key in ("min", "max", "max_dimension"):
                    values = [source.get(key) if isinstance(source, dict) else None for source in (first, second)]
                    references[key] = (max(values) if key == "min" else min(values)) if all(type(value) is int for value in values) else None
                existing["references"] = references
            if workers_evidence:
                modes[mode["id"]]["workers"] = workers_evidence
    return [modes[key] for key in labels if key in modes]


def hub_image_mode(raw: dict[str, Any], mode_id: str) -> dict[str, Any] | None:
    return next((mode for mode in hub_image_modes(raw) if mode["id"] == mode_id), None)


def image_reference_state(kind: str, model_id: str, raw: dict[str, Any]) -> bool | None:
    """Does this image model take a reference image? `True`, `False`, or `None`.

    Three answers, for the same reason every other capability here has three: a
    model that *states* it takes text only must not be offered a reference
    control, a model that states it takes an image must not be offered as
    text-only, and a model that says nothing must not be turned into a definite
    `False` — which is what this used to publish for every kind except
    `studio_hub`, while the same row's `operations` still claimed
    `image-to-image`.

    `None` means **unstated**. It is not a claim that the answer is no.
    """
    raw = raw or {}
    if kind == KIND_STUDIO_HUB:
        modes = hub_image_modes(raw)
        reference = next((mode for mode in modes if mode["id"] == "edit"), None)
        if reference is not None:
            return reference["available"]
        return False if modes or "image_modes" in raw else None
    capabilities = raw.get("capabilities")
    haystack = ""
    if isinstance(capabilities, list) and capabilities:
        haystack = " ".join(str(item) for item in capabilities)
    if not haystack.strip():
        haystack = " ".join(
            str(raw.get(key) or "")
            for key in ("category", "output_type", "mode")
        )
        if kind != KIND_STUDIO_HUB:
            haystack = haystack + " " + str(model_id or "")
    haystack = haystack.lower()
    if not haystack.strip():
        return None
    if any(word in haystack for word in _IMAGE_REFERENCE_WORDS):
        return True
    if any(word in haystack for word in _IMAGE_TEXT_WORDS):
        return False
    return None


def image_reference_limits(raw: dict[str, Any]) -> dict[str, int | None]:
    """How many reference images a lane states it takes, and how large: `min`, `max`, `max_dimension`.

    Read from `lane_contract.references`, the block the worker publishes about its own edit lane —
    never from a constant here. `None` means the lane **stated nothing**, which is a different
    answer from zero: the gateway publishes the silence (Story then offers no reference control)
    rather than a number the worker never agreed to. In particular the old `1, 4` and `2048`
    literals are gone: 4 references and 2048 px were this file's guesses, and the measured lane
    takes 2 at 1024.

    `counts_against_prompt`, `field` and `media_types` are the lane's own business (the Hub builds
    the multipart), so only the three figures the catalogue has to publish are returned.
    """
    contract = raw.get("lane_contract") if isinstance(raw, dict) else None
    mode = hub_image_mode(raw, "edit") if isinstance(raw, dict) else None
    references = mode.get("references") if mode is not None else (
        contract.get("references") if isinstance(contract, dict) else None
    )
    references = references if isinstance(references, dict) else {}

    def stated(key: str) -> int | None:
        value = references.get(key)
        return value if type(value) is int and value > 0 else None

    return {key: stated(key) for key in ("min", "max", "max_dimension")}


def transcription_can_serve(model_id: str) -> bool:
    """Whether this transcription model can answer with the SRT we must return.

    `whisper-1` and every local Studio Hub Whisper model can; see
    `TRANSCRIPTION_SRT_UNSUPPORTED` for the ones that cannot.
    """
    if model_id in TRANSCRIPTION_SRT_UNSUPPORTED:
        return False
    # **The family, not just the spellings.** OpenAI keeps minting `gpt-*-transcribe*`
    # variants that accept `json`/`text` only, and each new one is publishable until
    # someone runs it and finds out — which is exactly what happened next:
    # `gpt-4o-mini-transcribe-api-ev3` failed with *"response_format 'srt' is not
    # compatible with model 'gpt-4o-mini-transcribe-api-ev3'"* while its four
    # siblings sat in the list above. A version suffix is not a new capability, so
    # the rule matches the family and the list keeps the ids that are not simply
    # `gpt-*transcribe*` (the diarising and streaming endpoints).
    lowered = str(model_id or "").lower()
    return not (lowered.startswith("gpt-") and "transcribe" in lowered)


#: Model families a provider lists in the **same `/models` catalogue as its chat
#: models**, but whose endpoint is not a chat completion. Every provider here
#: publishes only `{id, owned_by}` — measured on NVIDIA NIM 2026-09-20: all 82 rows
#: come back with no type field at all — so nothing in the catalogue says
#: `nvidia/embed-qa-4` is an embedding endpoint, `nemotron-4-340b-reward` scores
#: completions, `nemotron-parse` is OCR, or `riva-translate-4b-instruct` translates.
#: They were therefore all published as `modality: chat` and marked publishable, so
#: ticking one produced a row that reached Story Studio and failed the moment it was
#: picked — the same shape as the OpenAI-SRT and OpenRouter-`:batch` traps.
#:
#: Matched on the id, because a family name is the only signal there is. Vision
#: models are deliberately **not** in this list: `meta/llama-3.2-11b-vision-instruct`,
#: `microsoft/phi-3-vision-128k-instruct`, `nvidia/vila` and `nvidia/neva-22b` are
#: chat models that happen to accept an image, and the chat lane serves them.
#:
#: ponytail: a curated family list, because no provider *publishes* the endpoint
#: type. Ceiling — a new embedding or classifier family is published until its name
#: is added here. Upgrade path — read the type from the provider where it states one
#: (NVIDIA's build pages do, its API does not), or probe the model once at discovery
#: and record the verdict; either would retire this list.
NON_CHAT_MODEL_WORDS: tuple[str, ...] = (
    "embed",
    "nvclip",
    "nemoguard",
    "guard",
    "content-safety",
    "topic-control",
    "reward",
    "parse",
    "deplot",
    "translate",
    "calibration",
    # Groq's catalogue carries `canopylabs/orpheus-v1-english` and
    # `orpheus-arabic-saudi` beside its chat models. Those are **TTS** models — the
    # same Orpheus the local Studio Hub lane runs for speech — so a chat completion
    # against them cannot work, and Groq declares no tts modality for the gateway to
    # route them to instead. Withholding applies to the `chat` modality only, so the
    # local Orpheus speech lane is untouched.
    "orpheus",
)


def chat_can_serve(model_id: str) -> bool:
    """Whether a chat row can actually answer a chat completion.

    Measured live before this rule existed: it withholds **31** rows across six
    providers (NVIDIA NIM 21, OpenRouter 4, Experiential Labs 3, OpenAI 1,
    Google 1, Google free 1) and **not one of them was enabled**, so nothing the
    owner could have picked changed — the same standard the fal image-to-image fix
    was held to. The non-NVIDIA catches are real: `gpt-realtime-translate`,
    `gemini-3.5-live-translate-preview`, `gpt-oss-safeguard-20b`/`120b` and
    `llama-guard-4-12b` are all classifiers or streaming translators, not chat.
    """
    haystack = str(model_id or "").lower()
    return not any(word in haystack for word in NON_CHAT_MODEL_WORDS)

# ---------------------------------------------------------------------------
# Discovery strategies
# ---------------------------------------------------------------------------
DISCOVERY_OPENAI_MODELS = "openai_models"      # GET {base}/models  (OpenAI-compatible)
DISCOVERY_ANTHROPIC_MODELS = "anthropic_models"  # GET {base}/models
DISCOVERY_GOOGLE_MODELS = "google_models"      # GET {base}/models?key=
DISCOVERY_FAL_CATALOG = "fal_catalog"          # GET api.fal.ai/v1/models
DISCOVERY_ELEVENLABS = "elevenlabs_models"     # GET {base}/v1/models
DISCOVERY_STUDIO_HUB = "studio_hub"            # GET {hub}/api/hub/model-packages
DISCOVERY_STATIC = "static"                    # curated list only (doc-driven providers)


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str
    label: str
    env_var: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""
    help_text: str = ""


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    id: str
    display_name: str
    category: str          # language | image | voice | video | music | multi
    kind: str
    base_url: str
    modalities: tuple[str, ...]      # chat | image | video | tts | transcription | music
    discovery: str
    description: str = ""
    docs_url: str = ""
    # Extra host used only for listing models, when the generation host differs
    # (varies by provider: fal runs jobs on queue.fal.run but lists on api.fal.ai).
    catalog_base_url: str = ""
    credential_fields: tuple[CredentialField, ...] = ()
    static_models: tuple[dict, ...] = ()
    default_enabled: bool = True
    #: Whether newly discovered models become available to Story Studio
    #: immediately.
    #:
    #: **False by default, deliberately.** Discovery and availability are
    #: different things: a provider publishing 300 models should not put 300
    #: rows into Story Studio's picker. Models are discovered here, then
    #: explicitly enabled in the admin console — the same model GenStudio used.
    #:
    #: The local Studio Hub provider opts back in (see its definition): those
    #: are the owner's own runners, there are about a dozen, and the local lane
    #: is the point of the thing.
    auto_enable_discovered: bool = False
    notes: str = ""
    #: What the provider says it does with the prompts it is sent.
    #:
    #: Three values, never two, and **`unverified` is the default** — the honest
    #: value when nobody has checked is "I do not know", and defaulting to
    #: `private` would publish a promise the provider never made. So the field
    #: means: `non_private` (the provider states it may retain or train on what
    #: you send), `private` (it states it does not), `unverified` (no statement
    #: found — **not** a claim of safety).
    #:
    #: Only what is documented is marked. Google's free tier is published as using
    #: content to improve their products while the paid tier is not, so
    #: `google-ai-free` is `non_private` and `google-ai` is left `unverified`
    #: until the paid statement is cited here too. Token Harbor's own note already
    #: says its free routes may retain prompts.
    #:
    #: This exists because "free" and "your prompts may train someone's model" are
    #: the same offer here, and the owner has private voices — a picker that shows
    #: the price but not the trade is the thing this field fixes.
    data_use: str = "unverified"
    #: Whether the whole provider *is* a free tier, rather than a paid vendor that
    #: happens to carry some free rows. Stated explicitly because deriving it from
    #: the id would be the identity-from-a-label mistake, and because the console
    #: marks the two cases differently: a free provider charges nothing for
    #: everything, a paid aggregator charges for everything except the rows whose
    #: own id says `:free`.
    free_tier: bool = False

    @property
    def primary_field(self) -> str:
        return self.credential_fields[0].key if self.credential_fields else "api_key"


def _api_key(env: str, label: str = "API key", **kw) -> CredentialField:
    # setdefault, not assignment: callers legitimately override the placeholder,
    # and passing it both ways is a TypeError.
    kw.setdefault(
        "placeholder",
        "sk-…" if ("OPENAI" in env or "DEEPSEEK" in env) else "",
    )
    return CredentialField(key="api_key", label=label, env_var=env, **kw)


# When the generation host and the catalogue host differ, this is the one that
# answers "what models exist". Only fal currently needs it.
FAL_QUEUE = "https://queue.fal.run"
FAL_CATALOG = "https://api.fal.ai/v1"

PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    # ------------------------------------------------------------------ language
    ProviderDefinition(
        id="openrouter-main",
        display_name="OpenRouter",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://openrouter.ai/api/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Aggregator. One key reaches Claude, GPT, Gemini, DeepSeek and many more.",
        docs_url="https://openrouter.ai/docs",
        credential_fields=(_api_key("OPENROUTER_API_KEY"),),
    ),
    ProviderDefinition(
        id="deepseek-main",
        display_name="DeepSeek",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.deepseek.com",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="DeepSeek direct.",
        docs_url="https://api-docs.deepseek.com",
        credential_fields=(_api_key("DEEPSEEK_API_KEY"),),
    ),
    # **Ollama Cloud, and the one thing to know is that it needs no local install.**
    #
    # The owner went looking for an API on ollama.com, found a download button instead, and asked
    # whether running Ollama on his Mac was the only way to reach the free cloud models. It used to
    # be; it is not any more. `https://ollama.com/v1` is an OpenAI-compatible endpoint reached with
    # an API key from ollama.com/settings/keys, and Ollama's own documentation says so in as many
    # words: *"No Ollama installation or local server is required."* (The local server at
    # `http://localhost:11434/v1` still works and still needs no key for models he runs himself —
    # that is a different offer, and a second local runtime beside Studio Hub, so it is not this
    # entry.)
    #
    # `KIND_OPENAI_COMPATIBLE` is already implemented and already carries chat, so this is **data,
    # not code**: no adapter, no discovery strategy, no route. `GET https://ollama.com/v1/models`
    # answers HTTP 200 with an OpenAI-shaped list and needs no key to list — measured 2026-09-29,
    # 17 models — so `DISCOVERY_OPENAI_MODELS` reads it unchanged.
    #
    # **Free here does not mean unlimited, and the difference is worth stating rather than letting
    # the plan name imply it.** Ollama's free tier is *"starter usage credits"* over *"starter
    # models"* with **one concurrent request**, then pay-as-you-go — not a standing free quota like
    # Google's free key next door. Running models on his own hardware stays unlimited, which is what
    # the Studio Hub lane is for. So this is a useful third or fourth lane to swap to when a free
    # quota elsewhere is spent, and the catalogue publishes its models only once a key is entered.
    ProviderDefinition(
        id="ollama-cloud",
        display_name="Ollama Cloud",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://ollama.com/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Open models served by Ollama. No local install; free accounts get starter credits and a starter model set.",
        docs_url="https://docs.ollama.com/api/openai-compatibility",
        credential_fields=(_api_key("OLLAMA_API_KEY"),),
    ),
    # **OmniRoute, which runs on this Mac.** The owner: *"it's a local hosted on this computer but i
    # add apis or sth into so it's good for me as it allows me to use the opencode that i can't
    # personally use directly. i've paid it's subscription. it can run through omniroute."*
    #
    # It is an OpenAI-compatible gateway on `127.0.0.1:20128` that fans out across subscription,
    # API-key, cheap and free tiers — so one entry here reaches everything he has wired into it,
    # including the coding plans he cannot call directly.
    #
    # `127.0.0.1` and not `localhost`: the Gateway resolves this itself, and a name that can
    # resolve to `::1` first would fail against a server bound to IPv4 only.
    #
    # **The key is required by *listing*, not by use — and that is why it is a credential here.**
    # Measured 2026-09-29 against his running instance: `POST /v1/chat/completions` with
    # `model: "auto"` and **no** key answers **200** with a real completion, while
    # `GET /v1/models` answers **401 `invalid_api_key`**. Every other OpenAI-compatible lane needs
    # its key for both, so this asymmetry is worth stating: the Gateway discovers models by listing
    # them, so without a key this provider would be configured-and-empty. Make one at
    # `http://127.0.0.1:20128/dashboard/api-keys` and paste it in.
    ProviderDefinition(
        id="omniroute",
        display_name="OmniRoute (this Mac)",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1:20128/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="A local gateway that fans out across your subscription, API and free tiers behind one OpenAI-compatible endpoint.",
        docs_url="https://github.com/diegosouzapw/OmniRoute",
        credential_fields=(_api_key("OMNIROUTE_API_KEY"),),
    ),
    ProviderDefinition(
        id="anthropic-direct",
        display_name="Anthropic",
        category="language",
        kind=KIND_ANTHROPIC,
        base_url="https://api.anthropic.com/v1",
        modalities=("chat",),
        discovery=DISCOVERY_ANTHROPIC_MODELS,
        description="Claude direct, via the Messages API.",
        docs_url="https://docs.anthropic.com",
        credential_fields=(_api_key("ANTHROPIC_API_KEY", placeholder="sk-ant-…"),),
    ),
    ProviderDefinition(
        id="openai-direct",
        display_name="OpenAI",
        category="multi",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.openai.com/v1",
        modalities=("chat", "image", "tts", "transcription"),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="GPT chat, image generation, speech and transcription.",
        docs_url="https://platform.openai.com/docs",
        credential_fields=(_api_key("OPENAI_API_KEY"),),
    ),
    ProviderDefinition(
        id="google-ai",
        display_name="Google AI",
        category="multi",
        kind=KIND_GOOGLE,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        modalities=("chat", "image", "video", "tts", "transcription", "music"),
        discovery=DISCOVERY_GOOGLE_MODELS,
        description="Gemini chat, Imagen images, Veo video, TTS, music.",
        docs_url="https://ai.google.dev/gemini-api/docs/models",
        credential_fields=(_api_key("GOOGLE_AI_API_KEY", label="API key (paid tier)"),),
    ),
    ProviderDefinition(
        id="google-ai-free",
        display_name="Google AI (free tier)",
        category="language",
        kind=KIND_GOOGLE,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        modalities=("chat",),
        discovery=DISCOVERY_GOOGLE_MODELS,
        description="Separate free-tier key, ranked against the paid Google route.",
        docs_url="https://ai.google.dev/gemini-api/docs/models",
        credential_fields=(_api_key("GOOGLE_AI_FREE_API_KEY", label="API key (free tier)"),),
        # Google publishes that the **free** tier uses content to improve their
        # products while the paid tier does not. That is the whole trade for a free
        # key, so it is recorded as a fact about the lane rather than left in a doc.
        data_use="non_private",
        free_tier=True,
    ),
    ProviderDefinition(
        id="groq-main",
        display_name="Groq",
        category="multi",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.groq.com/openai/v1",
        # Chat *and* transcription, both through the OpenAI-compatible shim that is
        # already this kind's whole adapter — so the second lane needs no dispatch
        # code. Transcription is the reason to care: Groq serves Whisper
        # large-v3-turbo at roughly a tenth of OpenAI's price per audio-minute, and
        # the gateway's transcription path is already proven end to end.
        modalities=("chat", "transcription"),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Very fast open-model inference, and cheap Whisper transcription.",
        docs_url="https://console.groq.com/docs",
        credential_fields=(_api_key("GROQ_API_KEY", placeholder="gsk_…"),),
    ),
    ProviderDefinition(
        id="nvidia-nim-main",
        display_name="NVIDIA NIM",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://integrate.api.nvidia.com/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="NVIDIA-hosted open models.",
        docs_url="https://build.nvidia.com",
        credential_fields=(_api_key("NVIDIA_NIM_API_KEY", placeholder="nvapi-…"),),
    ),
    ProviderDefinition(
        id="xai-direct",
        display_name="xAI",
        category="multi",
        # Chat is OpenAI-compatible; image and video are xAI's own shapes.
        kind=KIND_XAI,
        base_url="https://api.x.ai/v1",
        modalities=("chat", "image", "video"),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Grok chat, image and video generation.",
        docs_url="https://docs.x.ai",
        credential_fields=(_api_key("XAI_API_KEY", placeholder="xai-…"),),
    ),
    ProviderDefinition(
        id="tokenharbor",
        display_name="Token Harbor",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://tokenharbor.ai/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="Free language routes. Requires dashboard opt-in; may retain prompts.",
        docs_url="https://tokenharbor.ai",
        credential_fields=(_api_key("TOKENHARBOR_API_KEY"),),
        notes="Free routes may retain prompts and responses. Treat as non-private.",
        data_use="non_private",
    ),
    ProviderDefinition(
        id="experiential-labs",
        display_name="Experiential Labs",
        category="language",
        kind=KIND_OPENAI_COMPATIBLE,
        base_url="https://api.experientiallabs.ai/v1",
        modalities=("chat",),
        discovery=DISCOVERY_OPENAI_MODELS,
        description="OpenAI-compatible gateway with a public model catalogue.",
        docs_url="https://platform.experientiallabs.ai/docs",
        credential_fields=(_api_key("EXPERIENTIAL_LABS_API_KEY"),),
    ),

    # ----------------------------------------------------------- image / video
    ProviderDefinition(
        id="fal-ai",
        display_name="fal.ai",
        category="multi",
        kind=KIND_FAL,
        base_url=FAL_QUEUE,
        catalog_base_url=FAL_CATALOG,
        # `tts` is served through fal's ElevenLabs routes. Its *catalogue* also
        # carries llm/music/speech-to-text endpoints, but this adapter does not
        # implement those, and a modality the adapter cannot serve is withheld
        # rather than offered — so they are not declared here.
        modalities=("image", "video", "tts"),
        discovery=DISCOVERY_FAL_CATALOG,
        description="Image, video and ElevenLabs speech aggregator with a live catalogue API.",
        docs_url="https://fal.ai/models",
        credential_fields=(
            CredentialField(
                key="api_key", label="API key", env_var="FAL_API_KEY",
                placeholder="key-id:key-secret",
                help_text="fal keys are a key id and secret joined by a colon.",
            ),
        ),
    ),
    ProviderDefinition(
        id="kie-ai-images",
        display_name="Kie.ai",
        category="multi",
        kind=KIND_KIE,
        base_url="https://api.kie.ai",
        modalities=("image", "video", "chat", "music"),
        discovery=DISCOVERY_STATIC,
        description="Image, video and music aggregator. Model list is curated: Kie publishes no catalogue endpoint.",
        docs_url="https://docs.kie.ai",
        credential_fields=(_api_key("KIE_AI_API_KEY"),),
        static_models=(
            # Kie publishes no catalogue endpoint, so this list is curated from
            # Kie's own documented model ids (GenStudio's verified
            # `docs/providers/kie-ai.md` tables). These are the exact strings
            # Kie's API expects in `model` -- deriving them from documentation
            # URLs produces ids the API rejects.
            #
            # Image
            {"id": "gpt-image-2-text-to-image", "display_name": "GPT Image 2", "modality": "image"},
            {"id": "gpt-image-2-image-to-image", "display_name": "GPT Image 2 (edit)", "modality": "image"},
            {"id": "nano-banana-2", "display_name": "Nano Banana 2", "modality": "image"},
            {"id": "nano-banana-2-lite", "display_name": "Nano Banana 2 Lite", "modality": "image"},
            {"id": "gemini-3-pro-image", "display_name": "Gemini 3 Pro Image", "modality": "image"},
            {"id": "seedream/5-pro-text-to-image", "display_name": "Seedream 5 Pro", "modality": "image"},
            {"id": "seedream/5-lite-text-to-image", "display_name": "Seedream 5 Lite", "modality": "image"},
            {"id": "qwen2/text-to-image", "display_name": "Qwen2 Image", "modality": "image"},
            {"id": "z-image", "display_name": "Z-Image", "modality": "image"},
            {"id": "wan/2-7-image", "display_name": "WAN 2.7 Image", "modality": "image"},
            {"id": "flux-2/pro-text-to-image", "display_name": "FLUX 2 Pro", "modality": "image"},
            {"id": "flux-2/pro-image-to-image", "display_name": "FLUX 2 Pro (edit)", "modality": "image"},
            {"id": "flux-2/flex-text-to-image", "display_name": "FLUX 2 Flex", "modality": "image"},
            {"id": "flux-2/flex-image-to-image", "display_name": "FLUX 2 Flex (edit)", "modality": "image"},
            # Video
            {"id": "happyhorse-1-1/text-to-video", "display_name": "HappyHorse 1.1", "modality": "video"},
            {"id": "happyhorse-1-1/image-to-video", "display_name": "HappyHorse 1.1 (image)", "modality": "video"},
            {"id": "kling/v3-turbo-text-to-video", "display_name": "Kling 3.0 Turbo", "modality": "video"},
            {"id": "kling/v3-turbo-image-to-video", "display_name": "Kling 3.0 Turbo (image)", "modality": "video"},
            {"id": "pixverse-v6/text-to-video", "display_name": "PixVerse V6", "modality": "video"},
            {"id": "bytedance/seedance-2-mini", "display_name": "Seedance 2.0 Mini", "modality": "video"},
            {"id": "grok-imagine-video-1-5-preview", "display_name": "Grok Imagine Video 1.5", "modality": "video"},
            {"id": "gemini-omni-video", "display_name": "Gemini Omni Video", "modality": "video"},
            # **Music is back, and it is the route this adapter already speaks.**
            # Kie moved its Suno family off the dedicated `/api/v1/generate*` API and
            # onto the unified Market `POST /api/v1/jobs/createTask`, where the
            # top-level api-model is the constant `ai-music-api/generate` and the
            # **tier** travels in `input.model`. Verified live 2026-09-20: such a
            # body queued a task, `recordInfo` reported `state: "success"` with a
            # `data` array of tracks carrying `audio_url`, and the run cost 12
            # credits. Kie's own page now strikes V4/V4_5/V5_5 through and lists
            # **V6, V6_MINI, V6_WILD** as the live tiers, so those are the three
            # offered here — a deprecated tier is the "offered, then fails" shape.
            # `KieBackend` sends the constant at the top level and the id below in
            # `input.model`; the ids here are therefore the tiers, which is also
            # what the picker should show.
            {"id": "V6", "display_name": "Suno V6", "modality": "music"},
            {"id": "V6_MINI", "display_name": "Suno V6 Mini", "modality": "music"},
            {"id": "V6_WILD", "display_name": "Suno V6 Wild", "modality": "music"},
        ),
        notes="Kie publishes no model catalogue endpoint. This list is curated from Kie's documented model ids; add new ones here or via Add model manually.",
    ),
    ProviderDefinition(
        id="bfl-direct",
        display_name="Black Forest Labs",
        category="image",
        kind=KIND_BFL,
        base_url="https://api.bfl.ai",
        modalities=("image",),
        discovery=DISCOVERY_STATIC,
        description="FLUX direct from the model authors.",
        docs_url="https://docs.bfl.ai",
        credential_fields=(_api_key("BFL_API_KEY"),),
        static_models=(
            {"id": "flux-pro-1.1", "display_name": "FLUX 1.1 Pro", "modality": "image"},
            {"id": "flux-pro", "display_name": "FLUX Pro", "modality": "image"},
            {"id": "flux-dev", "display_name": "FLUX Dev", "modality": "image"},
            {"id": "flux-kontext-pro", "display_name": "FLUX Kontext Pro", "modality": "image"},
        ),
    ),
    ProviderDefinition(
        id="minimax-direct",
        display_name="MiniMax",
        category="multi",
        kind=KIND_MINIMAX,
        base_url="https://api.minimax.io",
        modalities=("image", "video", "tts", "music"),
        discovery=DISCOVERY_STATIC,
        description="Image, video, speech and music.",
        docs_url="https://platform.minimaxi.com/document",
        credential_fields=(
            _api_key("MINIMAX_API_KEY", label="API key"),
            CredentialField(
                key="group_id", label="Group ID", env_var="MINIMAX_GROUP_ID",
                secret=False, required=False,
                help_text="Required by some MiniMax endpoints.",
            ),
        ),
        static_models=(
            {"id": "image-01", "display_name": "MiniMax Image-01", "modality": "image"},
            {"id": "video-01", "display_name": "MiniMax Video-01", "modality": "video"},
            {"id": "music-01", "display_name": "MiniMax Music-01", "modality": "music"},
            {"id": "speech-2.8-turbo", "display_name": "MiniMax Speech 2.8 Turbo", "modality": "tts"},
            {"id": "speech-2.8-hd", "display_name": "MiniMax Speech 2.8 HD", "modality": "tts"},
        ),
    ),

    # -------------------------------------------------------------------- voice
    ProviderDefinition(
        id="elevenlabs-direct",
        display_name="ElevenLabs",
        category="voice",
        kind=KIND_ELEVENLABS,
        base_url="https://api.elevenlabs.io",
        modalities=("tts",),
        discovery=DISCOVERY_ELEVENLABS,
        description="Speech synthesis and a shared voice library.",
        docs_url="https://elevenlabs.io/docs",
        credential_fields=(_api_key("ELEVENLABS_API_KEY"),),
    ),
    ProviderDefinition(
        id="fish-audio",
        display_name="Fish Audio",
        category="voice",
        kind=KIND_FISH,
        base_url="https://api.fish.audio",
        modalities=("tts",),
        discovery=DISCOVERY_STATIC,
        description="Private voice cloning and S2.1 speech.",
        docs_url="https://docs.fish.audio",
        credential_fields=(_api_key("FISH_AUDIO_API_KEY"),),
        static_models=(
            # Fish's documented `model` header values. The free tier runs the
            # SAME S2.1 Pro model as paid -- `s2.1-pro-free` is a real,
            # first-class model string, not a trial shim, and it is simply not
            # listed in the API reference table. Do not remove it for being
            # absent from the docs.
            {"id": "s2.1-pro-free", "display_name": "Fish S2.1 Pro (free)", "modality": "tts"},
            {"id": "s2.1-pro", "display_name": "Fish S2.1 Pro", "modality": "tts"},
            {"id": "s2-pro", "display_name": "Fish S2 Pro", "modality": "tts"},
            {"id": "s1", "display_name": "Fish S1", "modality": "tts"},
        ),
        notes="Free tier (s2.1-pro-free) runs the same S2.1 Pro model as paid, under a Fair Use Policy.",
    ),

    # --------------------------------------------------------------------- local
    ProviderDefinition(
        id="studio-hub-local",
        display_name="Studio Hub (this Mac)",
        category="multi",
        kind=KIND_STUDIO_HUB,
        base_url="",  # discovered at runtime from hub-ready.json
        modalities=("chat", "image", "tts", "music", "transcription", "video", "embeddings", "vision"),
        discovery=DISCOVERY_STUDIO_HUB,
        description="Your own Apple Silicon runners: FLUX images, Qwen3/VoxCPM2/OmniVoice voice, YuE2 and Stable Audio 3 music, Whisper transcription. (Video is not offered: the local video models were retired.)",
        docs_url="",
        credential_fields=(
            CredentialField(
                key="hub_token", label="Hub token", env_var="STUDIOHUB_HUB_TOKEN",
                required=False,
                help_text="Read from the Hub's .hub_token file. Loopback Hub is passwordless unless STUDIOHUB_REQUIRE_AUTH=1.",
            ),
            CredentialField(
                key="base_url", label="Hub base URL", env_var="STUDIOHUB_BASE_URL",
                secret=False, required=False, placeholder="auto-discovered from hub-ready.json",
            ),
        ),
        # The one provider that opts back in to auto-enable: these are the
        # owner's own local runners, there are about a dozen, and requiring a
        # click for each would be friction for no benefit.
        auto_enable_discovered=True,
        notes="Local execution. Story Studio is contractually forbidden from calling this directly; the gateway is the sanctioned path.",
    ),
)

PROVIDERS_BY_ID: dict[str, ProviderDefinition] = {p.id: p for p in PROVIDER_DEFINITIONS}


# ---------------------------------------------------------------------------
# User-defined providers
#
# The built-in list covers everything the owner already had in GenStudio, but a
# gateway that can only ever speak to 18 hard-coded vendors is not modular. This
# reads `data/providers.local.json` at import time and merges whatever it finds.
#
# Loaded at import, so adding one needs a restart. That is a deliberate
# trade-off: provider definitions are structural (they select an adapter), not
# runtime state, and keeping them static means the catalogue cannot change shape
# underneath an in-flight job.

#: Custom-provider entries that were **not** loaded, and why.
#:
#: `providers.local.json` is hand-written, so being skipped is silent by default:
#: the provider simply never appears, which reads as "my file did not load"
#: rather than "nothing can run that kind". Recorded and published instead — a
#: page that omits a row must say that it did.
_SKIPPED_CUSTOM: list[dict[str, str]] = []


def _skip_custom(provider_id: str, reason: str) -> None:
    _SKIPPED_CUSTOM.append({"id": provider_id, "reason": reason})
    log.warning("providers.local.json: skipped '%s' — %s", provider_id, reason)


def skipped_custom_providers() -> tuple[dict[str, str], ...]:
    """Entries in `providers.local.json` that were not loaded, with the reason."""
    return tuple(_SKIPPED_CUSTOM)


def _load_custom_providers() -> tuple[ProviderDefinition, ...]:
    # Lazy, and only for the two namespace constants: `fleet` owns them, and a
    # module-level edge would be a second copy waiting to drift (which is exactly
    # how `_VALID_KINDS` fell behind `IMPLEMENTED_KINDS`).
    from ..fleet import FLEET_PROVIDER_ID, PROVIDER_PREFIX

    if not CUSTOM_PROVIDERS_PATH.exists():
        return ()
    try:
        raw = json.loads(CUSTOM_PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    entries = raw.get("providers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return ()

    # Rebuilt from scratch: this is called once at import, but a second call
    # (a reload in a long-lived process) must not double every reason.
    del _SKIPPED_CUSTOM[:]
    loaded: list[ProviderDefinition] = []
    for entry in entries:
        if not isinstance(entry, dict):
            _skip_custom("?", "the entry is not an object")
            continue
        provider_id = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        if not provider_id:
            _skip_custom("?", "it has no 'id'")
            continue
        if provider_id in PROVIDERS_BY_ID:
            # Never let a local file silently redefine a built-in provider.
            _skip_custom(provider_id, "that id is already a built-in provider")
            continue
        if kind not in _VALID_KINDS:
            # Named, and *why*: the owner edits this file by hand, so a skipped
            # entry that says nothing simply never appears — which reads as "my
            # file did not load" rather than "that kind cannot run".
            _skip_custom(provider_id, f"unknown kind '{kind}' — nothing can run it")
            continue
        if provider_id == FLEET_PROVIDER_ID:
            # The bare `studio-hub` id is the **published** namespace every
            # location's models collapse into (`fleet.publish_id`). A provider
            # claiming it would be both the maker and a site, and Story Studio
            # would read one id for two different things.
            _skip_custom(
                provider_id,
                "that id is reserved: it is the published namespace every Studio "
                "Hub location's models are collapsed into",
            )
            continue
        if provider_id.startswith(PROVIDER_PREFIX) and kind != KIND_STUDIO_HUB:
            # A **cloud** provider squatting the location prefix. `fleet` decides
            # "is this a location" from the id prefix, so this would be given a
            # site's treatment — a controller id parsed out of its name, a place in
            # `fleet.json`'s world — while having no controller behind it. A
            # `studio_hub`-kind entry is a location by every measure the code uses
            # (that is the shape the Fleet page itself writes), so it is allowed;
            # the prefix is not free for anything else.
            _skip_custom(
                provider_id,
                f"that id uses the reserved Studio Hub location prefix "
                f"('{PROVIDER_PREFIX}*') but its kind is '{kind}', not 'studio_hub'",
            )
            continue
        fields = tuple(
            CredentialField(
                key=str(f.get("key", "api_key")),
                label=str(f.get("label", "API key")),
                env_var=str(f.get("env_var") or f"CUSTOM_{provider_id.upper().replace('-', '_')}_KEY"),
                secret=bool(f.get("secret", True)),
                required=bool(f.get("required", True)),
                placeholder=str(f.get("placeholder", "")),
                help_text=str(f.get("help_text", "")),
            )
            for f in (entry.get("credential_fields") or [{"key": "api_key", "label": "API key"}])
        )
        loaded.append(
            ProviderDefinition(
                id=provider_id,
                display_name=str(entry.get("display_name") or provider_id),
                category=str(entry.get("category") or "custom"),
                kind=kind,
                base_url=str(entry.get("base_url") or ""),
                catalog_base_url=str(entry.get("catalog_base_url") or ""),
                modalities=tuple(entry.get("modalities") or ("chat",)),
                discovery=str(entry.get("discovery") or DISCOVERY_OPENAI_MODELS),
                description=str(entry.get("description") or "User-defined provider."),
                docs_url=str(entry.get("docs_url") or ""),
                credential_fields=fields,
                static_models=tuple(entry.get("static_models") or ()),
                default_enabled=bool(entry.get("default_enabled", True)),
                # Must match ProviderDefinition's own default. Defaulting to
                # True here silently undid the opt-in policy for every
                # user-defined provider.
                auto_enable_discovered=bool(entry.get("auto_enable_discovered", False)),
                notes=str(entry.get("notes") or "User-defined."),
            )
        )
    return tuple(loaded)


#: Kinds a `providers.local.json` entry may declare.
#:
#: **The same set as `IMPLEMENTED_KINDS`, not a second copy of it.** It was a
#: second copy, and it had already fallen behind — `KIND_XAI` was missing — so
#: `"kind": "xai"` in a local file was dropped without a word and the provider
#: never appeared anywhere. "May this be loaded?" and "can anything run it?" are
#: one question, because a provider whose kind has no adapter can only ever
#: publish models that fail on use, and it must have one answer.
_VALID_KINDS = IMPLEMENTED_KINDS

for _custom in _load_custom_providers():
    PROVIDERS_BY_ID[_custom.id] = _custom

ALL_PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = tuple(PROVIDERS_BY_ID.values())


def providers_for_modality(
    modality: str, definitions: Iterable[ProviderDefinition] | None = None
) -> tuple[ProviderDefinition, ...]:
    """Built-in providers offering a modality, or the ones you pass in.

    **Pass `definitions`.** The default is the static built-in list, which cannot
    see a Studio Hub location added on the Fleet page — four separate bugs in this
    codebase came from iterating the static map and silently omitting derived
    providers, and this helper had exactly that shape with no caller to reveal it.
    Callers that mean "every provider" must pass `registry.definitions()`; the
    default exists only for code that genuinely means "built-ins".
    """
    source = ALL_PROVIDER_DEFINITIONS if definitions is None else tuple(definitions)
    return tuple(p for p in source if modality in p.modalities)


# ---------------------------------------------------------------------------
# Grouping the console's provider list
#
# The owner, 2026-10-25: *"make sure all the providers are organised properly
# too."* Nineteen cloud cards in one column, ordered by nothing in particular,
# meant the answer to "which of these speaks?" was found by reading every card.
#
# **Derived from `modalities`, never a second list of providers.** A provider is
# filed under the *first* lane it serves in `MODALITIES` order — the lane it is
# bought for — and every lane it serves is still drawn as a badge on its card, so
# grouping hides nothing and duplicating the vendor list is not expressible. The
# lane order and the group order are the same list, so a modality added to
# `MODALITIES` gets a group without anyone editing this.
#
# **Display only.** Nothing here is read by dispatch, enablement, discovery or
# routing: `registry.definitions()` remains the single source of truth for what
# exists, and this only decides which heading a card is drawn under.
# ---------------------------------------------------------------------------

#: What each lane is *called* on the page. A label, not an order and not a list of
#: providers — an unlabelled modality falls back to its own name, so a new one is
#: never left out of the console for want of a nicer word here.
_SERVICE_GROUP_LABELS: dict[str, str] = {
    "chat": "Text & chat",
    "image": "Image",
    "video": "Video",
    "tts": "Voice & TTS",
    "transcription": "Transcription",
    "music": "Music",
    "embeddings": "Embeddings",
    "vision": "Vision",
}

#: The heading a definition with no modality at all is filed under, and the key
#: `service_group` returns for it. Every defined provider serves something today;
#: a hand-written `providers.local.json` entry can declare none, and such a
#: provider must still be on the page rather than dropped from every group.
UNGROUPED_KEY = "other"


def service_group_keys() -> tuple[str, ...]:
    """Every group key, in the order the console draws the headings."""
    from ..catalog import MODALITIES  # local: catalog imports this module

    return tuple(MODALITIES)


def service_group_label(key: str) -> str:
    return _SERVICE_GROUP_LABELS.get(key, key.replace("_", " ").title())


def service_group(modalities: Iterable[str] | None) -> str:
    """The group key a provider's card is filed under — its first lane.

    Pass the lanes it **can serve**, not only the ones it declares, where the caller
    knows the difference: Kie.ai declares chat and has no adapter for it, and filing
    it under "Text & chat" would group a card by a lane the gateway will never run.

    First in `MODALITIES` order rather than in the order the definition happens to
    list them: two providers serving the same set must land under one heading, or the
    same capability is filed two ways depending on how someone typed a tuple.
    """
    served = set(modalities or ())
    for key in service_group_keys():
        if key in served:
            return key
    return UNGROUPED_KEY


def group_by_service(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Group described providers by the key each one already carries.

    `rows` are `/admin/api/providers` entries — the same dicts the page draws — so
    the key a provider is filed under is decided **once**, by whoever knew its
    definition, and this only orders the headings. Returns
    `({key, label, ids}, …)`, in `MODALITIES` order with the ungrouped heading last;
    groups with no members are omitted, and every row lands in exactly one group
    (asserted in `tests/test_sfx_providers.py`).
    """
    buckets: dict[str, dict[str, Any]] = {
        key: {"key": key, "label": service_group_label(key), "ids": []}
        for key in (*service_group_keys(), UNGROUPED_KEY)
    }
    for row in rows:
        key = row.get("service_group") or UNGROUPED_KEY
        bucket = buckets.get(key)
        if bucket is None:
            # A key nothing declared: keep the row visible under its own heading
            # rather than dropping it out of the page entirely.
            bucket = buckets[key] = {"key": key, "label": service_group_label(key), "ids": []}
        bucket["ids"].append(row["id"])
    return tuple(bucket for bucket in buckets.values() if bucket["ids"])
