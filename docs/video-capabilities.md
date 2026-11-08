# A video model describes itself, and the record is also the request mapping

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

**One record per model, in `gateway/video_capabilities.py`, and nothing else in the gateway types a
video option.** `catalog._video_row` published `["16:9","9:16","1:1"]` and `5s`/`10s` at `"720p"`
for every cloud video model — a canvas almost none of them accept, published as the model's own.
The record now states, per model: modes (T2V/I2V/R2V), aspect ratios, resolutions, the duration
range as `{min, max, step, default?}`, whether the clip carries audio and which field turns it off,
which request field a first frame, a final frame and reference images go in, how many reference
images are accepted, and the source of every value.

- **Two provenance words, the same ones limits use.** `provider` means read from the provider's own
  machine-readable endpoint schema (fal publishes one per endpoint:
  `https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>`). `curated` means typed from the
  provider's documentation page, and then `source_date` is the date it was **read** — a record older
  than the provider's current schema is stale and must be re-read, not edited from memory.
- **A model with no record publishes its mode and no variants.** It is not offered with a guess:
  Story Studio's `selectLiveImageToVideoModel` requires at least one variant, so an undescribed
  model is visible in the console (and enableable) but never selectable. "The answer is none" and
  "we could not work it out" must not produce the same row.
- **The record is the field mapping, not a second opinion.** Wan 3.0 puts its first frame in
  `start_image_url` and its audio switch in `audio`; Seedance 2.5 puts the same two things in
  `image_url` and `generate_audio`, declares `duration` as a **string** enum, and names its reference
  images `image_urls` where Wan says `reference_image_urls`. So a new endpoint is one record, and
  `adapters/fal.py` names no model.
- **A value the record does not publish is refused, never clamped or dropped.** fal's queue accepts
  any body and validates at the worker, so a wrong option reports the job `COMPLETED` and fails when
  the result is fetched — long after the caller could have been told the accepted list.
- **The picture must actually arrive.** Story Studio uploads the exact scene image to
  `POST /v1/videos/source-images`, submits its id, and `api._stage_video_reference` copies the bytes
  beside the job (`_reference_path`). Each adapter sends those bytes in **its provider's own field**:
  fal as a `data:` URI in the endpoint's declared image field, xAI as `image: {url}`, MiniMax v2 as
  a `content[]` item with `role: "first_frame"`, and Kie — whose inputs take URLs, not base64 — by
  uploading to its `file-base64-upload` route and using the returned `downloadUrl` in `image_urls`.
  A staged file that has gone is a named refusal: a video generated without the scene image is a
  video of something else and looks exactly like a success.

**Recipe — adding the next video model is one record plus, at most, one mapping.**
1. Read the endpoint's own schema (not a blog post, not a memory): for fal,
   `curl 'https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>'`.
2. Append a `VideoCapability` to `VIDEO_CAPABILITIES` in `gateway/video_capabilities.py` — modes,
   ratios, resolutions, `duration_min/max/step`, audio field, start/end/reference fields, source and
   date. `_fal()` fills the provider-sourced defaults.
3. That is all, for a provider whose adapter already speaks the model's shape. A **new provider**
   needs an adapter that maps the same staged inputs — and the test in
   `tests/test_video_capabilities.py` that pins it.
4. Run `tests/test_video_capabilities.py`. Enabling the model is the owner's switch in the console;
   nothing here enables one.
