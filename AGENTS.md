# gateway-kh

One API in front of every local Studio Hub location and 18 cloud providers, so
**Story Studio KH points at a single URL** (`http://127.0.0.1:8000`) and its model
pickers fill. Story Studio's own architecture contract requires it to call
exactly one endpoint and treat it as GenStudio — this is that endpoint. Story
Studio needs **zero code changes**, only `genstudio_url` and an API key.

Read `README.md` for the design; this file is the part that is expensive to
rediscover.

## Contract traps — each cost real debugging time

- **Never return 402.** Story Studio holds a credit lock keyed on the API key. If
  it ever sees 402 and no balance is published, that hold **never clears** and the
  key is dead permanently. A provider running out of funds surfaces as a settled
  `failed` job. The e2e suite asserts that no response in a whole run is 402.
- **Publish the shape of a priced catalogue with every amount at zero.** Do not
  omit price fields: Story Studio's parsers build explicit object literals and
  **drop any row whose price fields are absent**, which empties the picker
  entirely. `credits_per_*: 0`, `minimum_credits: 0`, `price_version: "flat"`,
  `chargeable: true`, and a balanced `0/0/0` settlement triple are all
  load-bearing.
- **`freshness` is a hard gate.** `stale: true` disables every model, and a copy
  older than 30 minutes is refused. Computed from the last catalogue rebuild, not
  process start.
- **`finish_reason` must be a clean stop** — `length` is refused as truncated.
- **Artifact URLs stay on the API origin** (relative paths only).

## Publish what is known, and nothing else

The single most productive rule to audit against, taken from Story Studio's own
notes: *never let "the answer is none" and "I could not work out the answer"
produce the same value, and never invent a number the customer will mistake for
the model's own.*

Every field a provider does not state is published as **`null`**, never as a
plausible default. Auditing for this found, in order: `context_window_tokens: 0`
on 37 rows, `max_output_tokens` hardcoded to `8192` for every model on every
provider, `supports_vision: false` for every model while 273 of OpenRouter's 445
accept images, `provider_name` missing on video and keyed differently on
transcription, transcription claiming English-only *and* auto-detect, TTS
languages published as stringified Python dicts, and Google's
`inputTokenLimit`/`outputTokenLimit` being discarded.

**When a provider states it, read it.** Providers spell the same figure
differently — `context_length` vs `inputTokenLimit`, `top_provider.max_completion_tokens`
vs `outputTokenLimit`, `maximum_text_length_per_request` — so read every spelling
before concluding a provider said nothing.

**Changing an int to `null` is a breaking change for the reader.** It made an
unreachable fallback in Story Studio live (their `normalizeMaxOutputTokens`
substitutes 8,192 and prints it as the model's own figure). The field-change
audit sent with the handoff is the pattern: list every field whose *guarantee*
changed, not just its value.

**One value must stay invented:** TTS `maximum_input_characters` — Story Studio
calls `.toLocaleString()` on it unconditionally, so `null` would crash the page
rather than degrade it. Per model where the provider states one (ElevenLabs
publishes it), 25,000 otherwise, documented in the handoff as theirs to move.

## Discovery ≠ availability

A provider publishing 300 models must not put 300 rows in the picker. Nothing
reaches Story Studio until it is enabled. The local Studio Hub is the sole
exception (`auto_enable_discovered`), and that is deliberate. A provider whose
kind has no adapter stays discoverable and configurable but **its models are
withheld** — offering a model that fails on use is worse than not offering it.
`IMPLEMENTED_KINDS` in `providers/definitions.py` is the single source of truth,
and `adapters/__init__.py` asserts at import that its table agrees.

**A kind having an adapter does not mean every modality of it works.** That assert
compares *kinds*, and a modality can be missing inside one. The Google backend
delegates to the OpenAI-compatible shim, which is what serves
`/audio/transcriptions` — so a Google transcription row would publish and then fail
on use, which is exactly what this whole section exists to prevent. Transcription is
implemented for `openai_compat` and `studio_hub`: **enable OpenAI's transcription
models, not Google's.** Verified end to end — `whisper-1` through the gateway
returns a real SRT. **Groq was added to that list on 2026-09-19** (chat and
transcription, through the same shim, at roughly a tenth of OpenAI's price per
audio-minute); it needs `GROQ_API_KEY` before its models appear, because nothing
is published from an unconfigured provider.

**The gateway asks every cloud transcription lane for `response_format: "srt"`,
so a model that cannot answer in SRT must not be offered.** On OpenAI's side only
`whisper-1` can — the `gpt-4o-transcribe` and `gpt-transcribe` families accept
`json`/`text` only and answer 400, and `gpt-4o-transcribe-diarize`,
`gpt-live-transcribe` and `gpt-realtime-whisper` are diarising or streaming
endpoints rather than timestamped transcription. Ticking one of those published a
model that failed the moment anything used it, which is the `:batch` trap again.
They are listed in `TRANSCRIPTION_SRT_UNSUPPORTED` (`providers/definitions.py`)
and withheld from Story Studio; they stay visible in the inventory, so ticking one
still works as a control, it just never publishes. Google's transcription rows are
impossible for a different reason (the route does not exist) and are refused by
the modality gate. Curated rather than discovered, because no provider publishes
which formats it accepts.

**fal publishes a per-endpoint JSON schema, and reading it is not optional.**
`https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>` states the exact
fields and **types** for any fal endpoint. Two silent mistakes came from not
reading it: `duration` is a *string* enum on `bytedance/seedance-*` but an
*integer* on `minimax/h3-max` and `google/gemini-omni-flash`, and
`minimax/h3-max` declares `prompt_expansion_mode` required despite giving it a
default. The failure mode is what makes this expensive: **fal's queue accepts any
body and validates at the worker**, so a wrong body reports the job `COMPLETED`
and only fails when the result is fetched — a `422 {"detail":[{"type":"missing"…}]}`
in place of the artifact. Verified live: submitting `{}` to four fal endpoints
queued and "completed" all four and cost nothing, because the worker rejected
each one for a missing `prompt`/`text`.

**fal's TTS lane is its ElevenLabs routes, and it has no voice list.** The model
schema carries no voice endpoint and no `enum` on `voice` — only twenty
`examples` and a default of `Rachel` — so `voices._fal_tts_voices` curates exactly
those names as the lane's voices, or a TTS job would have nothing to name and
Story Studio's picker would be empty. Discovery keeps only fal categories
`text-to-speech` and `text-to-audio`: its audio family also holds a voice
*changer*, a realtime voice and speech-to-speech endpoints, none of which accept
the `text` this adapter sends, and id-based inference was pulling them in.

**xAI is OpenAI-compatible for chat and nothing else.** Its image API rejects
`size` outright — measured: `HTTP 400: Argument not supported: size` — so
delegating images to the OpenAI shim published three models that failed on use;
it takes `aspect_ratio` and `resolution` instead. Its video API is a **deferred**
endpoint (`POST /v1/videos/generations` → `request_id` → poll
`GET /v1/videos/{id}`), which no OpenAI-shaped handler is. Hence `KIND_XAI` with
its own adapter, delegating only chat. Two traps in the video poll: **202 means
still working, and a failure arrives as HTTP 200** with `status: "failed"` — so
branch on the JSON `status`, never on the HTTP code, or a failed generation reads
as a success. The first frame is `image: {url}` (an object), not the `image_url`
string the Python SDK accepts.

**Lyria music is Gemini's `:generateContent`, not a `:predict`, and the audio is
inline base64.** It lives at `candidates[0].content.parts[N].inlineData` — and
Google warns explicitly that the lyrics are interleaved in the same array, so
**scan every part**; indexing `[0]` silently yields no audio. `lyria-realtime-exp`
is withheld: it is a bidirectional WebSocket session with no unary call, so it
could never answer a one-shot request. Lyria is **paid-tier only**, which is why
`google-ai-free` does not declare the music modality. A 200 carrying candidates
but no audio part is transient on the preview model — observed live, succeeding on
the next identical call — so it is raised `retryable`; a safety refusal is
different and carries `promptFeedback.blockReason`.

**OpenRouter publishes no music path at all.** Its catalogue genuinely lists
`google/lyria-3-*` with a live upstream provider, but its OpenAPI has zero
occurrences of "lyria" or "music" across 92 paths — the only audio-output shape it
documents is the TTS-shaped chat contract. Those rows are therefore withheld
rather than implemented: a listing is not a serving contract.

**The console must not list a model that can never run.** `ModelInventory.can_publish`
is the single rule, and both readers use it: the catalogue asks it to decide what
to publish, and `_flat_models` asks it to decide what to *show*. They were two
separate sets of conditions and the console's was the weaker one, so it advertised
rows the catalogue silently dropped — nine OpenAI transcription models of which
only `whisper-1` can return SRT, four `google-ai-free` Lyria rows on a tier that
has no Lyria, and OpenRouter's two Lyria rows. A row you can tick that will never
publish is a control that does nothing, which is what the owner asked to stop.

**A proxy that speaks another provider's ids is the one exception to "a voice
belongs to its provider".** fal's ElevenLabs routes accept an ElevenLabs voice id
unchanged — the owner has driven them that way many times, and it is now verified
through the gateway (his own voice, 51,871 bytes of MP3). Matching a voice on its
provider alone meant the picker offered only the twenty names fal happens to
`example`, so a voice saved from his own account was invisible for that lane while
working perfectly through it. `accepted_voice_providers` keys the exception on the
provider's *kind* and the word `elevenlabs` in the endpoint — never on a hardcoded
provider id, and never for a lane that is not ElevenLabs.

**A provider voice known by its id can be saved without searching for it.** The
library search only finds what the provider will *search* for, and
"Add to my account" only ever acted on rows already in the table, so a voice known
from a share link had no way in. `POST /admin/api/voices/import` always accepted an
explicit `provider_voice_id`; the Voices window now has fields for it and for the
owner id. **ElevenLabs is the one provider that needs both**, because its import
route is `/v1/voices/add/{owner_id}/{voice_id}` — refused locally with that
sentence rather than as an opaque 422 from the far end.

**An upload past 25 MB is refused 413, not queued and then failed.** Every cloud
transcription API on the gateway caps a single upload there, and a caller that
crosses it should be told the number rather than handed a provider error after
the bytes have been staged and a job queued that cannot succeed. The ceiling is
one flat number because no provider *publishes* its limit over its API —
`ponytail:` in `api.py` names the upgrade path (read it per model, the way TTS
limits come from `maximum_text_length_per_request`). **413, never 402.**

**A model that is enabled but cannot serve is the worst state, and OpenRouter's
`:batch` variants are exactly that.** They are the providers' Batch APIs — half
price, turnaround in hours — and a normal `/chat/completions` request answers
**404 "This model is only available through the Batch API."** So an enabled `:batch`
row publishes to Story Studio and fails the moment it is picked. **Nine of the
owner's twenty-three enabled OpenRouter models were these, and on 2026-09-19 he
had them switched off** — each had its working non-`:batch` sibling enabled, so
nothing was lost and OpenRouter went from 23 enabled to 14. Do not re-enable them;
the sibling without the suffix is the same model on a route that answers.

**A Hub lists its own connectivity probes as machines, and they must not be
counted as the owner's hardware.** Studio Hub reports `probe-fleet` and
`probe-hub` — labelled "probe", permanently unreachable — beside the real machine.
This Mac read as "3 machines, 2 offline", which is exactly the figure the owner
wants the Fleet page for; at six sites it would read as ~12 broken machines when
nothing is wrong. `fleet._is_probe_machine` drops them from the machine list, the
machine table, the routing capacity and the activity list, from one place, and
`probe_machines_hidden` says how many were hidden — a page that hides rows without
saying so is the same failure as one that invents them. The online/offline counts
are derived **per machine** from the filtered activity list rather than from the
Hub's aggregate `pulse`, because the aggregate carries no machine identity and a
probe counted in it could not be taken back out. Matched on the id, never the
label: the label is the owner's to change.

## Received artifacts are temporary

The gateway is a **receiver**, not the owner: a node renders an artifact, this Mac
downloads it and serves it to Story Studio, and **Story Studio keeps the copy that
matters**. Nothing removed them at all before `gateway/retention.py`, so
`data/artifacts/` grew without bound.

Two limits, whichever is reached first, both editable in Settings or over the API
(`/api/settings/retention`, `/api/artifacts/prune`) — default **7 days or 10 GB**:

- **Age** is the normal one. An artifact Story Studio has not fetched in a week is
  not going to be.
- **Size** is the backstop for a burst. Long videos can pass the cap inside a day,
  so the oldest go first and the newest stay fetchable.

**Nothing younger than an hour is ever removed**, whatever the limits say. A
freshly-written artifact is about to be fetched and a staged upload is being read
by a running job, so the newest files are safe by construction rather than by luck.
The settings live in `DATA_DIR/retention.json`, not `.env`: the menu bar app
rewrites `.env` wholesale when it saves the key and the gateway reads that file once
at import, so a setting there would be dropped or ignored until a restart.

## Ports

- **8000** is this gateway, chosen because it is Story Studio's *default*
  `genstudio_url` — so no configuration is needed and a settings reset is
  harmless. The tests read the configured port rather than assuming one.
- **The older GenStudio KH API used 8000 too.** As of 2026-09-18 it is
  **stopped and disabled**: `launchctl bootout` plus `launchctl disable
  gui/$(id -u)/com.genstudiokh.local-api`, so it will not return at login either.
  Its LaunchAgent had `KeepAlive: true`, which is why a plain `kill` brought it
  straight back — stopping it for good needs the disable, not a signal.
  To restore: `launchctl enable gui/$(id -u)/com.genstudiokh.local-api` then
  `launchctl bootstrap gui/$(id -u)
  ~/Library/LaunchAgents/com.genstudiokh.local-api.plist`.
- **Curated limits**: `data/model-limits.local.json` supplies values for lanes
  that publish none (24 chat rows across six providers). It **fills gaps only** —
  a provider's published value always wins, or a stale file would quietly beat
  live data.

## Locations are providers — but not separate products

A Studio Hub location added on the Fleet page is also a **provider**: its models
are discovered and work is dispatched through the same `studio_hub` adapter,
which takes a per-provider address and token. **Standalone and controller both
work** — nothing gates on the role; it is read only to display it. Story Studio
must never learn a second address: the gateway is the only thing that knows the
locations.

**But a location is not a product, so its models publish under one namespace.**
`fleet.publish_id` collapses every location into `studio-hub`, and
`catalog.rebuild` merges the rows, so N locations offering the same model are
**one** catalogue entry with N possible sites — not N entries sharing one
`display_name`. The alternative showed the same model once per site, turned "which
model?" into "which building?", and broke a saved preset whenever its site went
offline. The old GenStudio reached the same conclusion, and one of its migrations
is named `0338_kokoro_names_the_maker_not_the_machines`. Namespace by **maker**;
a cloud vendor keeps its own id, because two vendors genuinely offering the same
model *are* different products.

Routing follows the split Studio Hub declares for itself in
`/api/hub/capabilities` — `authority: {global: "genstudio", site_local_scheduler:
"sqlite", global_job_claiming: false}`:

- **The gateway chooses the site, never the machine.** `fleet.route` ranks
  candidates by pressure (`queue / eligible_physical_machine_slots`) then a
  weighted lottery whose weight is the eligible slot count. Which machine has the
  memory for a model is machine-specific and belongs to the controller.
- **Capacity belongs in the weight, never as a rank term.** Ported from the old
  `MultiSiteAssignment`, whose comment records both failures: sorting by raw size
  made a small site unreachable, ignoring size overloaded the small sites.
- **A voice pins the route.** A voice id already names its owner
  (`studio-hub-<controller>::<raw>`), and a reference clip exists on one Hub, not
  on the fleet, so voice work must stay at that location.
- **The id Story Studio sent is the id it is told back.** The job stores the
  concrete location it was routed to, but `catalog_encode` applies `publish_id`
  before answering, or a saved preset stops matching its own jobs. This is
  asserted end to end in `test_fleet_standalone.py`.

Secrets live in one place, the credential store (`0600`); `fleet.json` holds only
the roster. Lookups of "the providers" must go through
`registry.definitions()`, not the static `PROVIDERS_BY_ID` map — four separate
bugs came from iterating the static map and silently omitting derived providers.

**A location's address comes from its *definition*, and one strategy forgot that.**
Every discovery strategy resolves its base as `values.get("base_url") or
definition.base_url` — a credential may override the address, but the address
itself is structural. `_studio_hub` was the only one that went straight from the
credential values to `hub_base_url()`, and that fallback is **this Mac's own Hub**.
A controller added on the Fleet page declares no `base_url` credential at all (its
address lives on the derived definition), so every remote location discovered the
*local* machine's models — each site showing the same list, and a job routed to
site B dispatchable for a model only site A has. Found while pointing a test
location at the mock: the provider reported "Studio Hub is not running (no
hub-ready.json found)" and discovered zero models, which is what
`test_e2e.py`'s section 20 now asserts against. The bug was latent only because no
second location was configured yet.

**Adding a location must also discover its models, and say what it found.**
Nothing refreshes models on a timer (`fleet.start_refresher` refreshes
*reachability*, not models), so a location added without a refresh shows none —
and "no models" reads as "it has nothing" rather than "nobody asked it yet".
Discovered models arrive **disabled**, as everything remote must, so the Fleet page
says how many were found and that none are available until enabled.

**A credential must be checked where it is entered, because both of a controller's
credentials work everywhere.** `/api/hub/settings` answers 200 to the operator
token, 403 to a fleet credential, and **401 to a wrong or absent one** (measured
against Hub 0.5.1 — a rejected token must never be reported as the fleet
credential). The gateway classifies it when a location is added, when its
credential is replaced, and on every connection test, and reports an operator token
in orange with what it can do. `PUT /api/fleet/controllers/{id}/credential`
replaces a token without touching the name, address or models.

**A location's name is a label, never an identity, and renaming must stay safe at
any time.** `fleet.rename_controller` changes only the label — the controller id,
its address, its token and its enabled state are untouched — so renaming can never
cost a re-entry of a credential, and it is safe after that location's models are
discovered and enabled. That holds only because the provider id is derived from
the **controller id** (`studio-hub-<controller-id>`) and the model id from the
provider, so never derive an id, key or path from a label. `fleet.json` also
carries `local_label`: this Mac is a location in the same list and is renamed the
same way, so a write that adds or removes a controller must preserve it.

Row actions in a table are worth testing through the UI, not just the API: the
Fleet page had `testController` and `removeController` **wired to nothing** — no
button ever called them — so Test and Remove were unreachable from the app while
the endpoints worked. `tests/test_ui.py` now reads the buttons themselves
(`table_button_titles`), because `table_texts` sees static text only.

## Voices are stocked, not configured

A voice is a reference clip plus a transcript, and **the transcript is not
decoration**: Qwen3 Base is a cloning model and refuses a reference without
aligned timestamps (`REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED`). So
`voice_import.start_annotation` transcribes each clip **through Studio Hub**, which
makes a working transcription model a prerequisite for stocking voices at all.
With no Hub it refuses and says so; silently doing nothing would look exactly like
success.

- **The gateway owns the clip, and the folder is the whole voice.** A voice folder
  is `reference.<ext>` + `transcript.txt` + `segments.json` + `metadata.json`, so a
  lost `voices.json` is recoverable from the folders and a folder is exactly the
  unit that would later be copied to a node. Import copies into
  `DATA_DIR/voice-library/<voice-id>/` and never points at the folder it read from:
  that folder is often a Downloads folder the owner will empty, and a catalogue
  pointing into it breaks the voice the day he does.
- **Two shapes are accepted, because both are things people have**: a folder of
  per-voice folders (the old library layout) and a folder of loose audio files. The
  old importer read only the first, so a clip dropped in by hand was silently
  skipped — which is exactly what "I copied it in and nothing happened" was.
- **Re-importing the same name updates that voice, never duplicates it**, and
  segments are dropped only when the audio itself changed, since otherwise they
  would describe audio that is no longer there.
- **`_local_voices` prefers the writable catalogue at read time.** `VOICES_PATH` is
  resolved once at process start, so on a fresh install it points at the shipped
  default and keeps pointing there — an added voice would not appear until a
  restart, which reads as the import having failed.
- **Permission to clone and distribute a voice is the owner's, per voice, and never
  assumed.** He has private voices: *"that needs to be sth i intentionally tick. as
  some voices are private."* Import therefore grants nothing — no
  `permission_acknowledged`, no `license` — because a default of "allowed" would
  copy a private voice to every machine by accident, and adding a clip must never
  be mistaken for granting permission. Granting sits behind a confirmation naming
  what is granted and to how many voices; withdrawing is one act with no dialog,
  since the safe direction should not be the slow one. Withdrawing drops the
  `license` too, or a later push would look authorised. The state is written into
  the voice's own folder metadata and shown per voice in the Voices window, so it
  travels with the voice rather than living only in a catalogue.
- **Across locations, a voice is staged per job.** The gateway uploads the clip to
  whichever location runs the work, so every location can use every gateway voice
  with nothing pre-copied; the `audio_sha256` match skips that upload where the
  controller already holds the clip.
- **Sharing a voice to the fleet needs two different tokens, and that is the point.**
  `POST /api/hub/shared-voices` (Hub 0.5.3+) creates a **Hub-owned** voice on a
  controller, which then fans out to its own joined nodes. Those routes require the
  controller's **Hub token** — a *fleet* token is refused on purpose, *"and that
  difference is what keeps a sold node from being a sold controller."* So a
  controller holds two stored values: `hub_token` (fleet credential: dispatch and
  fleet reads) and `admin_token` (Hub token: shared voices only, never used to run
  work). A location without an `admin_token` is skipped with that reason rather than
  failing obscurely. The gateway pushes to **controllers only** — six requests, not
  a hundred — because it never talks to nodes.
- **The two wire shapes differ, and getting it wrong is silent.** Stored segments are
  `{start, end, text}` (the Hub *receiver*'s shape); the shared-voice *route* wants
  `{start_seconds, end_seconds, text}`, sent as `transcript_segments` JSON.
  `voice_import.shared_segments` converts, and a transcript without timings is
  refused 400 by the Hub — deliberately, because the alternative is a hundred nodes
  installing a voice that then refuses to clone with it.
- **`pending` is a wait, not a failure.** A node that is switched off reports
  `pending` and the controller's reconciler retries every 30 s;
  `voice_import.summarise_targets` says *"will install when that Mac is next on"*,
  because calling it an error would send someone to a site that was never broken.
- **The id is derived from the bytes**, `audio_sha256[:12]` — 12 lowercase hex, which
  the Hub requires and keeps, so one clip carries one id across all six controllers
  instead of one per controller. Bytes decide identity: a re-push refreshes, and an
  id already holding different audio is a 409.

### A reference clip's contract belongs to the model, and the Hub states it

**Read it, never derive it.** `/api/hub/catalog` publishes, per model, a
`reference_audio` block — measured on Hub 0.6.18 and **re-verified unchanged on
0.6.23**, which the Hub auto-updated itself to mid-session:

| | min | target | preferred | max | transcript |
|---|---|---|---|---|---|
| Qwen3-TTS `*-Base-*` (4 tiers) | 3 s | 8 s | 8–12 s | **15 s** | required |
| OmniVoice-bfloat16 | 3 s | — | 3–10 s | **10 s** | optional |
| VoxCPM2 (4-bit, bf16) | — | — | — | **not stated** (`profile_audited: false`) | — |
| Qwen3 `CustomVoice` / `VoiceDesign` | — | — | — | **`declared: false`** — not cloning models | — |

`providers/discovery.py` reads this alongside `/api/hub/model-packages` (readiness)
— neither endpoint answers the other's question — and carries `capabilities` +
`reference_audio` in the model's `raw`. `voices.reference_contract` /
`reference_support` read it back. **The engines are more permissive than the Hub**:
Qwen3-TTS has no reference cap in its own code at all, and OmniVoice's
`_trim_long_audio` does nothing below **20 s** (measured by calling it: a 13.45 s
clip comes back untouched at any `max_duration`). So the 10 s and 15 s above are
Voice Studio's audited guardrails, and the gateway must not mirror, round or
invent them — a model that wants a 60 s reference will publish 60 and be honoured.

**A local voice belongs to whichever models clone — never to the tier it was
minted against.** Every voice the owner imported was stamped
`tts_model_id: "Qwen3-TTS-12Hz-0.6B-Base-4bit"` (`voice_import.DEFAULT_TTS_MODEL`),
and `voices_for_model` filtered local voices on an exact match with that string.
**One of the eight clone-capable local rows offered any voices**; the picker was
empty for the other seven, which reads as "you have no voices" rather than "those
clips belong to another build". The match is now the **model's own statement**.

**Three answers, never two.** `reference_support` returns `True`, `False`, or
`None`:
- `declared`/`supported: false` → the model says it does not clone (preset
  speakers, voice design). Voices are **withheld**; offering one fails on use.
- `true` → offered.
- **absent** → *unverified*, and the voice is **still offered**. A miss must not
  read as "cannot": VoxCPM2 8-bit is installed but absent from the catalogue, and
  treating that as non-cloning would hide all ten voices again. The report says
  "limits unverified" instead of leaving a blank.

`catalog._supports_cloning` applies the same read to the published
`supports_voice_cloning`. It used to be per **kind** (`studio_hub`, `fish`,
`elevenlabs`), which claimed cloning for a preset-speaker row and so offered Story
Studio a picker with nothing in it. **No currently-published row changed**: all
eight local rows declare `true`, and fish/elevenlabs keep the per-kind default.

**A location's package list cannot tell a codec from a model, and `can_publish` now
asks the catalogue.** `/api/hub/model-packages` lists the companions a model loads
beside the models themselves, and there is **nothing in a row to separate them** —
`snac_24khz`, `S3TokenizerV2`, `umt5-base`, `fish-s1-dac-min`,
`MOSS-Audio-Tokenizer-Nano` and a real model all read `engine=voice-mlx,
worker=voice, state=files_verified, runtime_state=ready`. The Hub's catalogue is
what distinguishes them: it lists models and omits the companions. So
`can_publish` withholds a package whose `raw["catalogued"]` is **False**, and one
whose `raw["memory_eligible"]` is **False** — the Hub's own verdict for the
machine it runs on, which keeps a bigger location's copy publishable where this
Mac cannot run it. Both are **read, and only when stated**: an absent key still
means *unverified*, never *withhold*.

**This is not theoretical — the migration reintroduced exactly the failure this file
exists to prevent.** Enabling the 34 migrated packages published **nine bad rows**:
five companions (a *tokenizer* offered as a clone-capable voice model), an
uncatalogued variant, and the three models the Hub says do not fit this 16 GB Mac
(`echo-tts-base` 24 GB, `fish-audio-s2-pro` 8-bit 24 GB and bf16 32 GB). The local
lane's `auto_enable_discovered` is why they went straight out. 27 local TTS rows
became 18 with the gate in place. **`available: true` on `/api/hub/models` is not a
promise that a job will run** — the Hub's submission gate refuses anything not in
its packaged catalogue with `MODEL_NOT_PACKAGED` ("model is not in the packaged
catalog", `components/hub/standalone.py`), which is why discovery must keep reading
`model-packages` and must **never** be "fixed" to publish cache-only models. The
Studio Hub session measured that gate; it is the reason the tempting change is
wrong.

**The header reports the offered count, not the enabled one.** `models.summary()`
publishes `enabled_models`, **`publishable_models`** and **`withheld_models`**, and
`by_modality` counts the *offered* rows — the same question the Overview chart's
"Offered to Story Studio" label asks. The tile used to show `enabled_models`, so it
read "121 live" while nine of those could not be picked; it now shows the offered
number and says `· N withheld` when there are any, because a model that is switched
on and silently absent from the picker is the one thing an operator cannot diagnose
from that page. Two questions, two numbers — never one field answering both.

**Nothing in the gateway truncates a reference; the Hub does the cutting.**
`studio_hub._stage_voice_reference` uploads `path.read_bytes()` verbatim, and
`reference_audio.prepare()` on the Hub side trims silence, then — only if the clip
still exceeds the model's maximum — keeps the longest run of **whole timed
segments** that fits and rewrites the transcript to match. So the recording stays
whole and each model takes what its own contract allows; that is why B (a baked
10 s copy) was dropped in favour of deriving at use time.

**The segments are cue-level because the installed engine emits no words.**
`voice_import._transcribe` prefers the job's own `metadata.segments` over the SRT
artifact (the artifact route serves *only* SRT — it has no `format` parameter at
all), and expands per-word timings when an engine provides them. Measured:
`whisper-base-mlx` returns an empty `words` list even with
`word_timestamps: "true"`, so window selection lands on cue boundaries — a 13.45 s
clip in three cues yields an ~8.7 s window under a 10 s cap rather than a 10 s
one. Finer segments would fit closer, and `_words_to_segments`-style expansion
already handles it; the `ponytail:` names that upgrade path.

**`GET /admin/api/voices/{voice_id}/reference` answers "what happens to my
recording?"** — per enabled local model: the contract as the Hub stated it, and a
verdict (used whole / the Hub picks a shorter aligned window / refused / takes no
reference / limits unverified) with a warning when the clip is under a model's
floor, a required transcript is missing, or there are no aligned segments. The
Voices window's **Reference…** button shows it. Nothing in it is computed from a
model name.

## Verification

```bash
.venv/bin/python tests/test_e2e.py              # 351 checks, isolated mock upstream
.venv/bin/python tests/test_fleet_standalone.py # 45 checks, real Hub(s) as locations
.venv/bin/python tests/test_ui.py               # 109 checks, drives the real app
```

`test_e2e.py` is fully isolated (`GATEWAY_DATA`/`GATEWAY_APP_SUPPORT` to a temp
dir, `GATEWAY_NO_MIGRATE=1`, random port, own key) — it cannot disturb the live
config. `test_fleet_standalone.py` starts its own gateway and Hub on a random
port, so it is equally safe.

**"Fully isolated" had a hole, and it was measured.** `hub.find_ready_file()`
falls back to the owner's real
`~/Library/Application Support/StudioHub Standalone/hub-ready.json`, so the
isolated gateway saw his **live Hub as its local location** — and the new
shared-voice fan-out test pushed a voice onto it. It had to be deleted from his
Hub by hand. `test_e2e.py` now sets **`STUDIOHUB_READY_FILE`** to a path inside
the temp dir, so there is no local Hub in a test run at all. Any new isolated
suite must do the same, and any test that reaches a *location* must point it at
the mock — the mock serves `/api/hub/shared-voices` (multipart, recording both the
payload and the `X-Hub-Token` it was sent) precisely so the fan-out can be
exercised without touching his machine. `test_fleet_standalone.py` is deliberately
different: it uses his real Hub on purpose, which is why it is the suite to run
knowingly.

**Two of its checks were failing for a Hub-side worker restart, and it was
transient — check before reporting it as a defect.** "a transcription ran on the
location end to end" and "the artifact comes back through the gateway" drive a real
transcription through the Hub's subtitle worker. Measured on 2026-09-20: the Hub
**auto-updated itself from 0.6.18 to 0.6.23 mid-suite**, restarting its workers
underneath an in-flight job, and the suite reported `Hub lost contact with a
possibly accepted Subtitle worker attempt`. In that window the worker was alive and
answering (401 on `/health` in 1.7 ms) but had loaded no model — 11 open files,
38 MB RSS — and a direct `_transcribe` of a 13 s clip that had taken ~30 s earlier
**timed out at 400 s**. **It recovered by itself**: a later worker (new pid, then
85 s old) transcribed the same clip in under 100 s and the suite went **45/45 on
re-run**. So: a worker answering `/health` is not a worker that transcribes, the
gateway's voice annotation depends on that same route, and the right response to
this failure is to re-run before filing it — an auto-update mid-run is the usual
cause.

**`test_ui.py` drives the real, installed app**, so run it deliberately rather than
reflexively. It takes over the app's windows through Accessibility, and loading new
code needs the app restarted, which interrupts whatever the owner is doing — he
asked for a pause on that once, and then lifted it. So: run it when the change is
UI-specific or when a claim about the app needs proving, and prefer the two isolated
suites plus a direct `curl` for everything else. Rebuilding is always safe;
**restarting the app is the part that intrudes**. It also needs **Accessibility
permission**, and it skips its Hub-dependent checks with a stated reason when Studio
Hub is stopped, because a stopped Hub is an environment state, not a gateway defect.

Verifying a change without touching his session: the isolated suites above, plus a
direct `curl` against the configured port.

Always rebuild before claiming the app works: `./menubar/build.sh`, which installs
to `/Applications/gateway-kh.app`. **"It builds" is not "it works."**

**Restarting the app is not the same as restarting the gateway, and a stale one
survives a rebuild.** The gateway deliberately outlives the app, so on launch the
app **adopts** whatever already answers `/health` instead of spawning a new one.
Two ways this bit:

  * `pkill -f "GatewayKH"` **kills the shell running it**, because that shell's own
    command line contains the pattern. The rest of the command never runs, so the
    uvicorn is never killed and the next `open` adopts it. Measured: a gateway from
    21:47 was still serving at 23:33, through four "rebuild and restart" cycles —
    one of which had already been reported as verified.
  * even without that, killing the app only triggers a spawn that **fails to bind**
    if the old gateway is still holding 8000, and the app then adopts the old one.

So: **kill by PID, never by pattern**, confirm port 8000 is actually free, and
confirm the gateway PID *changed* before measuring anything. A verification is only
evidence if it names the process it measured. `pgrep -f` for reading is fine; only
`pkill -f` is dangerous, and only when the pattern appears in your own command.

**Quitting stops the gateway, deliberately, and the app can say which build is
running.** This was the owner's request: *"if i want to restart or update or sth i
want to be able to do it by hand instead of relying on you to help me."* So:

  * `applicationShouldTerminate` stops the child, runs `killPortHolders()`, and
    waits (≤2 s) until `portIsHeld()` is false. Verified: quit → **port 8000 free**,
    no uvicorn left. The opt-out is a Settings checkbox, *"Keep the gateway running
    after I quit"* (`AppConfig.keepGatewayRunning`, default `false`) — default off,
    because an adopted stale gateway is the failure the owner actually hit.
  * **The build stamp is the whole mechanism.** `menubar/build.sh` writes
    `Contents/Resources/gateway/BUILD` (`<short-sha>-<UTC yyyymmddHHMM>`) and sets
    `GatewayBuild` in `Info.plist`; the app passes it as `GATEWAY_BUILD` when it
    spawns, and `gateway/build.py` resolves env → `BUILD` beside the package →
    `"unknown"`. `/health` publishes it. **Check Gateway** compares the running
    stamp with the app's own and answers in one sentence; the stale answer names
    *both* builds. Measured against a process started by hand with
    `GATEWAY_BUILD=fake-old-build-000000000000`: *"The gateway is running build
    fake-old-build-000000000000, but this app is cd866d5-202609191712. It is
    serving older code — restart it."* **Restart Gateway** then killed that pid
    (2414) and started the app's own (3252, stamp `cd866d5-…`), which is why it
    kills by port holder rather than calling the ordinary `restart()`.

**A killed gateway comes back within seconds, so testing adoption takes a quit.**
Measured: killing only the gateway left the app running, its health timer
respawned a gateway stamped with the app's own build, and my hand-started "stale"
one lost the race for the port (`[Errno 48] address already in use`) — the
detection looked broken when it was the simulation that was. To exercise adoption,
**quit the app first**, start the foreign process, then `open` the app: it adopts
what answers `/health` instead of spawning. That is the only order that proves the
path.

**Read a control's title to know it was built; press it to know it works.**
`tests/test_ui.py` gained `press_detail_button(title)` — the same three-level walk
as `detail_button_titles`, using `AXPress`, because `entire contents` is refused as
a specifier (-1700) and a title-only check would have passed for Fleet's Test and
Remove while they were wired to nothing. The suite now presses **Check Gateway**
and asserts the answer names a build (either sentence — which one is the
environment's business, and the stale case was verified by hand).

## Working rules

- **Never hand another session a description of behaviour that is not deployed yet.** A handoff
  written from a build while the running service is still on the previous one reads as a false claim
  to whoever measures it, and they will measure it. This happened: a note promised `legacy_ids` on
  every Studio Hub row while the live gateway still published `[]`, because the code was in the
  bundle and the process had not been restarted. The Story Studio session checked, found the note
  wrong, and was right. Either restart and measure the **running** service first, or say plainly
  that the change is built but not yet live. Both sessions lost time in one day to notes asserting
  behaviour nobody had verified.
- Prefer the **graphify** graph (`graphify-out/`, gitignored, rebuildable) over
  grepping for architecture, caller and impact questions. Keep it current with
  `graphify extract . --update` after structural work. Two things it needs around
  it: the docs in the tree need an LLM key, so run it as
  `graphify extract . --update --code-only` unless someone wants to spend a key on
  the prose (the code graph is the part that answers architecture questions); and
  it reports a **syntax error in `menubar/VoicesWindow.swift`** that is a parser
  limitation, not a defect — `swiftc -parse` is clean and the app builds, so check
  the compiler before believing it.
- **A page must never be able to resize the window.** A section pinned straight
  to the host propagates its content's *required* height outwards, and AppKit
  satisfies it by growing the NSWindow — the Fleet page did exactly that, to
  1162pt on a 1080pt screen, putting its bottom edge and resize corner off-screen
  where they could not be reached. A page whose content has a required height
  (tables with minimums, forms, dashboards) wraps its body in an `NSScrollView`
  with a `FlippedView` document, as `FleetSection`, `OverviewSection` and
  `SettingsSection` do. Pages that fill the pane (Jobs, Voices, Providers) need no
  scroller, because a table with a flexible height has nothing to propagate — but
  that only holds while the table's minimum really is *small*. Voices declared
  `heightAnchor >= 320` and Jobs `>= 260`: a **required** floor is exactly the
  thing that propagates, so both could still grow the window at the 860x560
  minimum, and Jobs' wrapping detail label could grow it from a single long
  `error_detail`. Keep a table's floor low (140 is enough to stop it collapsing)
  and cap any wrapping label with `maximumNumberOfLines`. Settings had the mirror
  -image problem: `bottomAnchor <= host` did stop the growth, and left the surplus
  with nowhere to go, so its bottom rows were clipped with no scrollbar — a form
  is a scroller page. **This is tested now**: `test_ui.py` forces the window to
  700pt, visits every section, and asserts the size does not change, then puts the
  owner's size back. Do not rely on the manual procedure alone; it is automated.
- Mark deliberate simplifications with a `ponytail:` comment naming **the ceiling
  and the upgrade path** — a curated exception list, a flat default, a fixed pool
  size. Unmarked, the next reader cannot tell a considered trade-off from an
  oversight.
- **The Hub's `TextGuidance`** (`soft_max_chars`, `chunking`) is authoritative
  about local voice engines but is **not served over its API**. Our local
  families are `auto-split` with no soft max; the hard-capping ones (Bark 150,
  Orpheus 170) are not exposed. If one ever is, the flat 25,000 default becomes
  wrong.
