"""What each cloud video model can actually do — one record per model, and the only place we type it.

**Why a record and not a constant.** `catalog._video_row` used to publish `["16:9","9:16","1:1"]`
and `5s`/`10s` at `720p` for every cloud video model. That is true of almost none of them: Wan 3.0
does 2–30 s at 480p/720p/1080p across six ratios, Seedance 2.5 does 4–30 s at 480p/720p/1080p
across seven, and neither takes the other's start-frame field name. A picker built on the old row
offered a canvas the provider refuses, and hid the durations and ratios the owner asked for.

**The record is the request mapping too.** `gateway/adapters/fal.py` reads the same record for the
field a model actually declares (`start_image_url` on Wan 3.0, `image_url` on Seedance 2.5,
`reference_image_urls` vs `image_urls`), so a model is never sent a field it does not read. That is
the second half of the same rule: capabilities and the request must be one fact.

**Adding the next model is one record.** Append a `VideoCapability` to `VIDEO_CAPABILITIES` below;
nothing in `catalog.py`, `api.py` or the fal adapter names a model. A capability we cannot describe
is not published as a plausible default — an unknown model keeps whatever mode its provider states
and publishes **no** variants, so Story Studio cannot offer a canvas nobody published.

**Every value says whose answer it is** (`source`), in the vocabulary the rest of this gateway uses:

  * `provider` — read from the provider's own machine-readable endpoint schema (fal publishes one at
    `https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>`). `source_url` names it.
  * `curated` — typed from the provider's own documentation page on `source_date`. The page is the
    authority; our transcription of it is not, which is why the date travels with the value.

`source_date` is the date the value was **read**, not the date the model shipped. A record older
than the provider's current schema is stale — re-read it rather than editing from memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Reading a value is one thing; publishing it is another. These are the two provenance words a
#: record may use, and they are the same words `catalog.LIMIT_SOURCES` publishes for limits.
PROVIDER = "provider"
CURATED = "curated"


@dataclass(frozen=True, slots=True)
class VideoCapability:
    """One video model's published capabilities and the request fields that carry them.

    A field left `None` (or an empty tuple) means **the model does not publish it**, never that it
    publishes a default. `catalog._video_row` publishes nothing for an unstated option, and the
    adapter sends no field for one.
    """

    provider_id: str
    model_id: str
    modes: tuple[str, ...]

    #: The ratios the model accepts, in the provider's own spelling and order.
    aspect_ratios: tuple[str, ...] = ()
    #: The resolution tiers it accepts.
    resolutions: tuple[str, ...] = ()
    #: Output counts whose transport is verified; empty means no count override is published.
    output_counts: tuple[int, ...] = ()
    #: The whole seconds it accepts, as a closed range with a step — one shape for a slider and for
    #: the enumerated variants Story Studio reads today (`duration_min..duration_max` by step).
    duration_min: int = 0
    duration_max: int = 0
    duration_step: int = 1
    #: The provider's own default, where it states one.
    duration_default: int | None = None
    #: `integer` or `string`: the same number on the wire in the type the endpoint declares.
    duration_style: str = "integer"
    #: Does the endpoint also accept `"auto"` (the model picks a length from the prompt)?
    duration_auto: bool = False

    #: Does the produced clip carry its own audio track? `None` when the provider states nothing.
    generates_audio: bool | None = None
    #: The boolean field that turns generated audio off, where the endpoint declares one.
    audio_field: str | None = None

    #: The field a first frame goes in, where the model takes one.
    start_frame_field: str | None = None
    #: The field a final frame goes in, where the model takes one.
    end_frame_field: str | None = None
    #: The field reference images go in, and how many the endpoint accepts.
    reference_field: str | None = None
    max_reference_images: int = 0
    #: The reference media kinds the endpoint names. Images only, for every model here.
    reference_kinds: tuple[str, ...] = ()

    source: str = CURATED
    source_date: str = ""
    source_url: str = ""

    def durations(self) -> tuple[int, ...]:
        """Every whole second this model accepts, in order. Empty when it states no range."""
        if self.duration_min <= 0 or self.duration_max < self.duration_min:
            return ()
        step = self.duration_step if self.duration_step > 0 else 1
        return tuple(range(self.duration_min, self.duration_max + 1, step))

    def duration_record(self) -> dict[str, int | bool] | None:
        """`{min, max, step}` (and the default, and `auto`, where stated) — what a slider needs."""
        if self.duration_min <= 0 or self.duration_max < self.duration_min:
            return None
        record: dict[str, int | bool] = {
            "min": self.duration_min,
            "max": self.duration_max,
            "step": self.duration_step if self.duration_step > 0 else 1,
        }
        if self.duration_default is not None:
            record["default"] = self.duration_default
        if self.duration_auto:
            # The endpoint also accepts `"auto"`, where the model picks a length from the prompt.
            # Published, not assumed: a caller that wants the model to choose has to be told it may.
            record["auto"] = True
        return record


def _fal(endpoint: str, **kwargs) -> VideoCapability:
    """A fal endpoint's record, sourced from that endpoint's own OpenAPI schema."""
    return VideoCapability(
        provider_id="fal-ai",
        model_id=endpoint,
        source=PROVIDER,
        source_date="2026-10-26",
        source_url=f"https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={endpoint}",
        **kwargs,
    )


#: The models this gateway can offer, with the exact options their own endpoint declares.
#:
#: Read 2026-10-26. The two families differ in more than their number ranges, which is the point:
#: Wan names its first frame `start_image_url` and calls the audio switch `audio`; Seedance names
#: the same two things `image_url` and `generate_audio`, declares `duration` as a *string* enum,
#: and its image-to-video endpoint accepts no aspect ratio but `auto`.
VIDEO_CAPABILITIES: tuple[VideoCapability, ...] = (
    _fal(
        "alibaba/wan-3.0/text-to-video",
        modes=("text-to-video",),
        aspect_ratios=("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"),
        resolutions=("480p", "720p", "1080p"),
        duration_min=2, duration_max=30, duration_default=5,
        generates_audio=True, audio_field="audio",
    ),
    _fal(
        "alibaba/wan-3.0/image-to-video",
        modes=("image-to-video",),
        aspect_ratios=("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"),
        resolutions=("480p", "720p", "1080p"),
        duration_min=2, duration_max=30, duration_default=5,
        generates_audio=True, audio_field="audio",
        start_frame_field="start_image_url",
        end_frame_field="end_image_url",
    ),
    _fal(
        "alibaba/wan-3.0/reference-to-video",
        modes=("reference-to-video",),
        aspect_ratios=("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"),
        resolutions=("480p", "720p", "1080p"),
        duration_min=2, duration_max=30, duration_default=5,
        generates_audio=True, audio_field="audio",
        reference_field="reference_image_urls",
        max_reference_images=10,
        reference_kinds=("image",),
    ),
    _fal(
        "bytedance/seedance-2.5/text-to-video",
        modes=("text-to-video",),
        aspect_ratios=("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        resolutions=("480p", "720p", "1080p"),
        duration_min=4, duration_max=30,
        duration_style="string",
        duration_auto=True,
        generates_audio=True, audio_field="generate_audio",
    ),
    _fal(
        "bytedance/seedance-2.5/image-to-video",
        modes=("image-to-video",),
        # The endpoint declares `aspect_ratio` as a constant: the output follows the first frame.
        aspect_ratios=("auto",),
        resolutions=("480p", "720p", "1080p"),
        duration_min=4, duration_max=30,
        duration_style="string",
        duration_auto=True,
        generates_audio=True, audio_field="generate_audio",
        start_frame_field="image_url",
        end_frame_field="end_image_url",
    ),
    _fal(
        "bytedance/seedance-2.5/reference-to-video",
        modes=("reference-to-video",),
        aspect_ratios=("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"),
        resolutions=("480p", "720p", "1080p"),
        duration_min=4, duration_max=30,
        duration_style="string",
        duration_auto=True,
        generates_audio=True, audio_field="generate_audio",
        reference_field="image_urls",
        max_reference_images=30,
        reference_kinds=("image",),
    ),
)

#: The browser-extension worker's Flow models, copied by script from Relay's tested catalogue.
EXTENSION_PROVIDER_ID = "story-studio-video"
EXTENSION_MODELS_PATH = Path(__file__).with_name("extension_video_models.json")


def _extension_records() -> tuple[VideoCapability, ...]:
    """One record per Flow key. A text key takes no picture; an image key takes exactly its one
    reference (without one the extension would generate a picture itself, spending credits
    nobody asked for — so `chrome_extension` refuses it)."""
    data = json.loads(EXTENSION_MODELS_PATH.read_text())
    return tuple(
        VideoCapability(
            provider_id=EXTENSION_PROVIDER_ID,
            model_id=model["key"],
            modes=("text-to-video",) if model["mode"] == "text" else ("reference-to-video",),
            aspect_ratios=tuple(data["aspect_ratios"]),
            output_counts=(1,),  # Existing single-request path; no multi-output binding is captured.
            duration_min=int(model["duration_seconds"]),
            duration_max=int(model["duration_seconds"]),
            duration_default=int(model["duration_seconds"]),
            reference_field=None if model["mode"] == "text" else "referenceImages",
            max_reference_images=0 if model["mode"] == "text" else int(data["max_reference_images"]),
            reference_kinds=() if model["mode"] == "text" else ("image",),
            source=CURATED,
            source_date=data["source_date"],
            source_url=f"{data['source']} sha256:{data['source_sha256']}",
        )
        for model in data["models"]
    )


VIDEO_CAPABILITIES = VIDEO_CAPABILITIES + _extension_records()

_BY_MODEL: dict[tuple[str, str], VideoCapability] = {
    (record.provider_id, record.model_id): record for record in VIDEO_CAPABILITIES
}


def for_model(provider_id: str, model_id: str) -> VideoCapability | None:
    """The record for one model, or `None` — meaning **nobody has described it here**.

    `None` is not "no capabilities". It is the gateway saying it holds no published answer, which
    is why the caller must publish nothing rather than a default (AGENTS: a value a provider does
    not state is `null`, never a plausible guess).
    """
    return _BY_MODEL.get((str(provider_id or ""), str(model_id or "").strip("/")))


def audio_switch(record: VideoCapability) -> list[dict[str, object]]:
    """The record's audio switch as a catalogue `control`, or `[]` when it declares none.

    Same shape the Studio Hub lane's switches use (`catalog._local_video_controls`), because a
    caller renders both the same way: the field name it keys a value by, the type, and the
    provider's own default. A model with no switch (`audio_field` absent) publishes none.
    """
    if not record.audio_field:
        return []
    return [{
        "name": record.audio_field,
        "type": "boolean",
        "default": True if record.generates_audio else None,
        "note": None,
    }]
