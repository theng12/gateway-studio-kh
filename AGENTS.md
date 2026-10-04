# gateway-kh

## New-Mac handoff repairs

The current acceptance record is `_handoffs/2026-09-20-local-completion.md`;
the historical open-issue ledger is a baseline, not current runtime status.
Global Ponytail/Graphify and conditional Impeccable/Superpowers instructions
apply here and to delegated workers.

- Unknown upstream outcomes are durable recovery holds, never automatic retries - **for paid providers.**
  A free Studio Hub location that goes silent (`recovery.lost_contact`) has its job moved to another
  location and the old attempt cancelled best effort; a submit that timed out is replayed at the same
  location first (the Hub dedupes on the job id). First result wins (`worker._dispatch`).
  Async adapters checkpoint safe upstream IDs before polling; never store a
  credential-bearing polling URL as an identity. Cancellation after dispatch
  is a request, not proof the provider stopped. Keep late outputs inspectable.
- Pruning must fail closed if referenced-input protection cannot be read.
- Create retries retain the exact original payload/key. Native multiline draft
  observers include nested editors, and callbacks must not unlock newer jobs.
  Only the app bundle (`kh.gateway.menubar`) reads or writes the real Create state (`CreateStateStore`); any other
  process (a test binary, a fixture) keeps it in memory, so a test cannot reach it even by forgetting to isolate.
  A saved job id the gateway answers 404 for is dropped, never shown (`mock-job` once was).
- Admin artifact URLs are extensionless: local files use stated media types,
  with authenticated same-origin downloads. Do not derive file types from URLs.
- Preserved Content Pack archives are not ownership or installation. The private
  Content Packs publisher is a separate, explicitly initialized authority for
  newly reserved local identities only; never reissue historical Store packs.
  Preserve Story Studio receipt/signature/Keychain checks. Unconfigured delivery
  returns `CONTENT_STORE_NOT_CONNECTED`; quotes and purchases remain unavailable.
  Private-owner entitlements have no purchase ID or fabricated purchase receipt.
  Missing signing identity fails closed, never silently regenerates. Download
  URLs are absolute same-Gateway-origin (the Content Store protocol exception
  to relative generation-artifact URLs), with exact-release short-lived grants.
- The real-Hub fleet suite requires explicit `GATEWAY_ALLOW_LIVE_GENERATION=1`;
  do not include it in offline smoke testing. Keep GitHub Actions disabled.


One API in front of every local Studio Hub location and 18 cloud providers, so
**Story Studio KH points at a single URL** (`http://127.0.0.1:8787`) and its model
pickers fill. Story Studio's architecture contract requires it to call exactly one
endpoint and treat it as GenStudio — this is that endpoint. It needs **zero code
changes**, only `genstudio_url` and an API key.

Read `README.md` for the design; this file is the part that is expensive to
rediscover.
## Contract traps — each cost real debugging time

- **Never return 402.** Story Studio holds a credit lock keyed on the API key. If
  it ever sees 402 and no balance is published, that hold **never clears** and the
  key is dead permanently. A provider out of funds surfaces as a settled `failed`
  job, and the e2e suite asserts that no response in a whole run is 402.
- **Publish the shape of a priced catalogue with every amount at zero.** Do not
  omit price fields: Story Studio's parsers build explicit object literals and
  **drop any row whose price fields are absent**, which empties the picker. `credits_per_*: 0`, `minimum_credits: 0`, `price_version: "flat"`,
  an explicit `chargeable`, and a balanced `0/0/0` settlement triple are
  load-bearing. `chargeable` itself is **truthful** (`pricing.chargeable`): `false` for a
  model on the owner's Macs (`price_source: local`) or a free lane, on every modality.
- **`freshness` is a hard gate Story Studio reads — and the one we publish cannot
  trip.** `stale: true` disables every model on their side, and the field is
  computed from the last catalogue *rebuild*, which `freshness()` performs before
  reading it, so it is always `false`. Do not "fix" that by making it fire (it
  would disable every model on a stale inventory); the honest number is
  `inventoryAgeMs`. See the freshness bullet in Working rules.
- **`finish_reason` must be a clean stop** — `length` is refused as truncated.
- **Generation artifact URLs stay on the API origin** (relative paths only).
  Content Pack signed-download URLs use the absolute same-origin Store contract
  described above; never derive their origin from untrusted Host headers.
## Publish what is known, and nothing else

**Story Studio's notes:** *never let "the answer is none" and "I could not work
out the answer" produce the same value, and never invent a number the customer
will mistake for the model's own.*

Every field a provider does not state is published **`null`**, never as a
plausible default. The audit found `context_window_tokens: 0` on 37 rows,
`max_output_tokens` hardcoded to `8192` on every provider,
`supports_vision: false` for every model while 273 of OpenRouter's 445 accept
images, `provider_name` missing on video, keyed differently on transcription,
transcription claiming English-only *and* auto-detect, TTS languages as
stringified Python dicts, and Google's `inputTokenLimit`/`outputTokenLimit`
discarded.

**When a provider states it, read it.** Providers spell the same figure —
`context_length` vs `inputTokenLimit`, `top_provider.max_completion_tokens` vs
`outputTokenLimit`, `maximum_text_length_per_request`.

**A published `0` is a claim, and an `or` chain throws it away.**
`_first_stated()` exists because
`raw.get("context_length") or raw.get("context_window") or …` treats a stated
`0` as absent: `_stated` and `limits.gap` — both preserving a stated zero —
never see one, and the curated value is published *as the provider's*. Read with
a first-non-`None` lookup, never with `or`.

**A voice's language is stated or it is absent — never assumed English.**
Imports stored `"en"` for any clip whose metadata said nothing, making the
readiness gate's "it has no language" unreachable. A folder reads its
`metadata.json`, a loose clip a sibling `<name>.json`; nothing stated means
`""`, and `story_row` publishes the `"multilingual"` sentinel.
The ten voices imported before this still say `"en"`; the old import wrote it
either way, so the original is unrecoverable and clearing them would be a guess.
**The Hub requires a language** (`raise ValueError("language is required")`), so
a language-less voice is refused a fleet push locally in one sentence — the gate
working, not a regression.

**Changing an int to `null` is a breaking change for the reader.** It made an
unreachable fallback in Story Studio live (`normalizeMaxOutputTokens`
substitutes 8,192 and prints it as the model's own figure). The handoff's
field-change audit is the pattern: list every field whose *guarantee* changed,
not just its value.

**One value must stay invented:** TTS `maximum_input_characters` — Story Studio
calls `.toLocaleString()` unconditionally, so `null` would crash the page. Per
model where the provider states one (ElevenLabs publishes it), otherwise
**15,000** on a Studio Hub location, 25,000 on a cloud lane — each carrying
`maximum_input_characters_source` (`provider` / `curated` / `gateway`), because
"we chose this" has to be readable next to the number.

**The local lane presents one number, and chunking is Studio Hub's job.** Every
Studio Hub location publishes **15,000** (`catalog._TTS_CHARACTERS_LOCAL`),
because the Hub takes the complete script and renders sentence-safe sections.
The caller is told what it may submit; it is never told how the work is cut up.
That is the owner's *presentation* decision rather than a measurement, so the
gateway does **not** mirror the Hub's audited
`execution_contract.text_max_characters` (10,000–40,000 across the installed
set), wrong for a field the caller cannot act on. The mirroring was tried and
withdrawn on 2026-09-20.

**The one exception, and it sequences itself.** A model the Hub does not section
gets the whole script, so an engine cliff applies:
`catalog._HARD_CAP_CHARACTERS` (bark 150, orpheus 170) is consulted
**only while `long_form_delivery` is absent** for that model, read from the Hub's
own table. So Orpheus
publishes 170 today and **becomes 15,000 automatically** the moment the Hub
starts sectioning it — no gateway change, no coordination. The rule is one constant,
`catalog._TTS_CHARACTERS_LOCAL` (15,000), beside `catalog._HARD_CAP_CHARACTERS`; the
handoff that recorded the decision was removed from `_handoffs/`, so this file names the
code rather than a dead pointer.
`spark-tts-mlx` is *deliberately absent* from that table despite the Hub's 750:
as `auto-split`, 750 is where quality is best, not a limit on what may be sent.

**A uniform section size is not ≤200.** The Hub's two hard-cap engines are Bark
at **150** and Orpheus at **170**, so a 200-character section is past both and a
180-character one is still past Bark. Every other family sections at 280–3000; a
uniform 150 would mean 100 sections per 15,000-character request for engines
needing 5, against the Hub's measured coverage curve (99.4% at 288, 83% at 600).
Per-family budgets, which the Hub already has and justifies, are the right
shape; the gap was only that two families had none.

**Every limit now says whose number it is.** `context_window_tokens`,
`max_output_tokens`, `max_prompt_characters` and `maximum_input_characters` each
carry a companion `*_source` — `provider`, `curated` (the owner's
`model-limits.local.json`) or `gateway` (ours: a default or the recorded cliff).
Measured live: 48 chat rows `provider`, **18 `curated`**; the curated file's 24
entries are all `1000000/128000`, a flat default rendered as the model's own
window. The value is still published, now labelled (nulling it makes Story
Studio substitute 8,192). **The value and its source come from one function**,
because a separate "where did that come from?" reader is free to disagree with
the chooser the first time either gains a step — how the quote route kept
publishing a flat 40,000 after the catalogue stopped.
## Discovery ≠ availability

A provider publishing 300 models must not put 300 rows in the picker. Nothing
reaches Story Studio until it is enabled. The local Studio Hub is the sole
exception (`auto_enable_discovered`), and that is deliberate. A provider whose
kind has no adapter stays discoverable and configurable but **its models are
withheld** — offering a model that fails on use is worse than not offering it.
`IMPLEMENTED_KINDS` in `providers/definitions.py` is the single source of truth,
asserted at import by `adapters/__init__.py`.

**A kind having an adapter does not mean every modality of it works.** That
assert compares *kinds*, and a modality can be missing inside one. Google's backend
delegates to the OpenAI-compatible shim serving `/audio/transcriptions`, so a
Google transcription row would publish and fail on use. Transcription is
implemented for `openai_compat` and `studio_hub`: **enable OpenAI's
transcription models, not Google's.** Verified end to end: `whisper-1` through
the gateway returns a real SRT. **Groq serves chat through the same shim** and
needs `GROQ_API_KEY` before its models appear. **Its Whisper cannot serve
transcription here** — measured live 2026-09-20,
`HTTP 400: response_format must be one of [json text verbose_json]`, and this
gateway must ask for `srt` — so `whisper-large-v3` and `whisper-large-v3-turbo`
are withheld.

**The gateway asks every cloud transcription lane for `response_format: "srt"`,
so a model that cannot answer in SRT must not be offered.** There, only
`whisper-1` can: the `gpt-4o-transcribe` and `gpt-transcribe` families accept
`json`/`text` only and answer 400, while `gpt-4o-transcribe-diarize`,
`gpt-live-transcribe` and `gpt-realtime-whisper` are diarising or streaming
endpoints. They are listed in `TRANSCRIPTION_SRT_UNSUPPORTED`
(`providers/definitions.py`) and withheld from Story Studio; they stay visible
in the inventory, so ticking one still works as a control, it just never
publishes. Google's transcription rows are impossible for a different reason (the
route does not exist) and are refused by the modality gate. Curated rather than
discovered, because no provider publishes which formats it accepts.

**fal publishes a per-endpoint JSON schema, and reading it is not optional.**
`https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>` states the
exact fields and **types**. `duration` is a *string* enum on
`bytedance/seedance-*` but an *integer* on `minimax/h3-max` and
`google/gemini-omni-flash`, and `minimax/h3-max` declares
`prompt_expansion_mode` required despite a default. **fal's queue accepts any
body and validates at the worker**, so a wrong body reports the job `COMPLETED`
and fails when the result is fetched — a `422 {"detail":[{"type":"missing"…}]}`
in place of the artifact. Verified live: submitting `{}` to four fal endpoints
queued and completed all four, because the worker rejected each for a missing
`prompt`/`text`.

**fal's TTS lane is its ElevenLabs routes, and it has no voice list.** The model
schema carries no voice endpoint and no `enum` on `voice` — only twenty
`examples` and a default of `Rachel` — so `voices._fal_tts_voices` curates those
names as the lane's voices. Discovery keeps only fal categories `text-to-speech`
and `text-to-audio`; other audio endpoints do not accept the `text` this adapter
sends.

**xAI is OpenAI-compatible for chat and nothing else.** Its image API rejects
`size` outright — measured: `HTTP 400: Argument not supported: size` — so
delegating images to the OpenAI shim published three models that failed on use;
it takes `aspect_ratio` and `resolution` instead. Its video API is a
**deferred** endpoint (`POST /v1/videos/generations` → `request_id` → poll
`GET /v1/videos/{id}`). Hence `KIND_XAI` with its own adapter, chat only. Two
traps in the poll: **202 means still working, and a failure arrives as HTTP
200** with `status: "failed"` — so branch on the JSON `status`, never on the
HTTP code, or a failed generation reads as a success. The first frame is
`image: {url}`, not the `image_url` string the Python SDK accepts.

**Lyria music is Gemini's `:generateContent`, not a `:predict`, and the audio is
inline base64.** It lives at `candidates[0].content.parts[N].inlineData` — and
Google warns the lyrics are interleaved in the same array, so **scan every
part**; indexing `[0]` silently yields no audio. `lyria-realtime-exp` is
withheld: a bidirectional WebSocket session with no unary call could never
answer a one-shot request. Lyria is **paid-tier only**, which is why
`google-ai-free` does not declare the music modality. A 200 with candidates but
no audio part is transient on the preview model — observed live, succeeding on
the next identical call — so it is raised `retryable`; a safety refusal carries
`promptFeedback.blockReason`.

**OpenRouter publishes no music path at all.** Its catalogue lists
`google/lyria-3-*` with a live upstream provider, but its OpenAPI has zero
occurrences of "lyria" or "music" across 92 paths. Those rows are withheld: a
listing is not a serving contract.

**The console must not list a model that can never run.**
`ModelInventory.can_publish` is the single rule, used by both readers: the
catalogue asks what to publish, `_flat_models` what to *show*. The console's
weaker conditions advertised rows the catalogue dropped: nine OpenAI
transcription models of which only `whisper-1` can return SRT, four
`google-ai-free` Lyria rows, and OpenRouter's two Lyria rows.

**A proxy that speaks another provider's ids is the one exception to "a voice
belongs to its provider".** fal's ElevenLabs routes accept an ElevenLabs voice
id unchanged, now verified through the gateway (the owner's own voice, 51,871
bytes of MP3), and the owner has driven them that way many times. Matching a
voice on its provider alone offered only the twenty names fal happens to
`example`, so a voice saved from his own account was invisible for that lane
while working perfectly through it. `accepted_voice_providers` keys the exception
on the provider's
*kind* and the word `elevenlabs` in the endpoint — never on a hardcoded provider
id, and never for a lane that is not ElevenLabs.

**A provider voice known by its id can be saved without searching for it.** The
library search finds only what the provider will *search* for, so a voice known
from a share link had no way in. `POST /admin/api/voices/import` always accepted
an explicit `provider_voice_id`; the Voices window now has fields for it and for
the owner id. **ElevenLabs is the one provider that needs both**, because its
import route is `/v1/voices/add/{owner_id}/{voice_id}` — refused locally rather
than as an opaque 422.

**An upload past 25 MB is refused 413, not queued and then failed.** Every cloud
transcription API on the gateway caps a single upload there; a caller that
crosses it should be told the number, not handed a provider error after staging.
The ceiling is one flat number because no provider *publishes* its limit over
its API — `ponytail:` in `api.py` names the upgrade path (read it per model, as
TTS limits come from `maximum_text_length_per_request`). **413, never 402.**

**A voice the caller did not choose is never invented.** A missing `voice_id`
used to become a specific named speaker — `English_expressive_narrator` on
MiniMax, `alloy` on the OpenAI-compatible shim. Both refuse now, naming the
field. fal's `Rachel` and ElevenLabs' account-first-voice stay, because those
providers genuinely default and the catalogue offers that default *as a voice*.
**speed** follows the same rule: fal's routes accept 0.7–1.2 while the catalogue
published 0.5–2.0, and the adapter *dropped* anything outside its range — a
caller picked a speed it had been told was valid and got normal speed. It
refuses now, and `catalog._TTS_SPEED_RANGES` publishes the accepted range: what
is offered is what is sent.

**A toggle has to still be a toggle when the job runs.** The worker re-checked
*provider* enablement only, so a model switched off after submit still ran, a
mid-queue `catalogued`/`memory_eligible` flip did not stop the job, and a
modality the adapter cannot serve was still dispatched. It now asks
`enabled_models(modality)` and fails `MODEL_NOT_OFFERABLE` naming the model,
provider and modality. **The same rule is enforced at the door:**
`add_user_model` refuses a modality nothing serves, because `can_publish`
requires `serves()` and the form only asked whether the provider *declared* it
(Google declares six, serves four) — so it could create a row that was enabled,
counted, and never published.

**A model that is enabled but cannot serve is the worst state, and OpenRouter's
`:batch` variants are exactly that.** They are the providers' Batch APIs — half
price, turnaround in hours — and a normal `/chat/completions` request answers
**404 "This model is only available through the Batch API."** Such a row fails
the moment it is picked. **Nine of the owner's twenty-three enabled OpenRouter
models were these, and on 2026-09-19 he had them switched off** — each had a
working non-`:batch` sibling, so nothing was lost and OpenRouter went from 23
enabled to 14. Do not re-enable them; the sibling without the suffix is the same
model on a route that answers.

**"Studio Hub is running" is a measurement, not a file.** `hub_status()` used to
publish `running: bool(hub_base_url())` — the existence of a `hub-ready.json` —
so the page said "running" while the menu bar's `/api/health` probe and
`fleet._probe` reported it unreachable. `hub.hub_probe()` asks `/api/health` (no
token needed) and distinguishes **three** states, never two: no ready-file, a
ready-file with nothing behind it, and one that answers; the answer is cached 5
s. The same probe gates `start_annotation`, which promised "refuses without a
reachable Hub" while gating on URL presence — a stale file made every voice fail
without the actionable sentence. Anything acting on a location's *models* needs
its **`provider_id`** (`studio-hub-<controller-id>`), published per location on
both branches, so no client derives it.

**A Hub lists its own connectivity probes as machines, and they must not be
counted as the owner's hardware.** Studio Hub reports `probe-fleet` and
`probe-hub` — labelled "probe", permanently unreachable. This Mac read "3
machines, 2 offline"; six sites, ~12 broken machines. `fleet._is_probe_machine`
drops them from the machine list, machine table, routing capacity and activity
list, and `probe_machines_hidden` says how many were hidden. The online/offline
counts are derived **per machine** from the filtered activity list rather than
from the Hub's aggregate `pulse`, because the aggregate carries no machine
identity and a counted probe could not be taken out. Matched on the id, never
the label: the label is the owner's to change.
## Received artifacts are temporary

The gateway is a **receiver**, not the owner: a node renders an artifact, this
Mac serves it to Story Studio, and **Story Studio keeps the copy that matters**.
Nothing pruned them before `gateway/retention.py`, so `data/artifacts/` grew
unbounded.

Two limits, editable via the API (`/admin/api/settings/retention`,
`/admin/api/artifacts/prune`) — **3 days** or **8 GB**, whichever comes
first. A **pin** (`/admin/api/artifacts/keep`) outranks both:

- **Age** is the norm: three days unfetched by Story Studio and it is pruned.
- **Size** is the burst fallback: long videos can pass the cap in a day, so the
  oldest go first and the newest stay fetchable.

**Nothing younger than an hour is ever removed**, whatever the limits say: a
freshly-written artifact is about to be fetched and a staged upload is being
read, so the newest stay safe by construction, not luck.

**Pruning moves, it does not delete.** A pruned file is renamed (same volume) into the
Trash Folder `DATA_DIR/trash/<UTC date moved>/<path under artifacts/>`; a failed move
is skipped, never unlinked. Dated folders older than **15 days** are purged
automatically, the only hard delete (owner decision 2026-09-30). `max_bytes` counts
the live store only; `usage()` (and Settings > Received artifacts) reports the Trash
Folder size separately. Settings live in
`DATA_DIR/retention.json`, not `.env`, which the menu bar app owns and rewrites.
## `.env` is read live, and the key is checked per request

`.env`, the documented no-UI key source, was not read live: `config.get()`
snapshotted at import, `providers/credentials.py` held a **second** copy
refreshed by `reload_env()`, and `api.require_auth` compared an import-time
`API_KEY` constant — fixing `GATEWAY_API_KEY`. **A rotation is the failure
that matters**: change the key, restart nothing, the old key still opens the
door.

`get()` re-stats `ENV_PATH` and re-reads on change; the credential store reads
through it, no `reload_env()` to forget; `require_auth` reads
`gateway_api_key()` per request. `admin.require_admin` already did. In
`tests/test_e2e.py` the suite key lives in the temp `.env`, not the spawned
environment, because `get()` prefers `os.environ` and a key passed there would
shadow the file and make the rotation untestable. It asserts a *wrong* key is
refused first: a failed `.env` leaves a keyless gateway open.

**`/admin/api/*` is deliberately open from loopback** (`require_admin` trusts
loopback and demands the token only when bound elsewhere), which is why a `curl`
against `/admin/api/status` returns 200 with no header. Measure auth on the
Story surface (`/v1/...`), which needs the key.
## Ports

- **8787** is this gateway's port — the default in `gateway/config.py` and the
  app's `AppConfig`, and where this Mac's working instance is bound. Every Story
  Studio client points its Base URL at `http://<host>:8787`. The tests read the
  configured port, never a hardcoded one.
- **It was 8000** because that is Story Studio's *default* `genstudio_url` and
  needed no configuration. The owner moved it to 8787 on 2026-09-21 (8000 is the
  most contested dev port and is also Story's own default, which kept "is this
  pointed at the right thing?" ambiguous). A fresh install now defaults to 8787.
- **The older GenStudio KH API used 8000 too.** As of 2026-09-18 it is
  **stopped and disabled**: `launchctl bootout` plus `launchctl disable
  gui/$(id -u)/com.genstudiokh.local-api`, so it does not return at login. Its
  LaunchAgent had `KeepAlive: true`, so a plain `kill` brings it back — stopping
  it needs the disable, not a signal. To restore: `launchctl enable
  gui/$(id -u)/com.genstudiokh.local-api` then `launchctl bootstrap
  gui/$(id -u)` with
  `~/Library/LaunchAgents/com.genstudiokh.local-api.plist`.
- **Curated limits**: `data/model-limits.local.json` fills gaps for lanes with
  none (24 chat rows across six providers). It **fills gaps only** — a
  provider's published value always wins.
## Locations are providers — but not separate products

A Studio Hub location is a provider: its models are discovered and dispatched
through the same `studio_hub` adapter. **Standalone and controller both work** —
the role only displays. Story Studio must never learn a second address: the
gateway alone knows the locations.

**But a location is not a product, so its models publish under one namespace.**
`fleet.publish_id` collapses every location into `studio-hub`, `catalog.rebuild`
merges the rows, so N locations offering the same model are **one** catalogue
entry with N possible sites — not N entries sharing one `display_name`.
Namespace by **maker**; a cloud vendor keeps its id, because two vendors
offering the same model *are* different products. The old GenStudio agreed, and
one of its migrations is named `0338_kokoro_names_the_maker_not_the_machines`.

Routing follows the split Studio Hub declares in `/api/hub/capabilities`
(`studio-hub-local`):

`authority: {global: "genstudio", site_local_scheduler: "sqlite",
global_job_claiming: false}`:

- **The gateway chooses the site, never the machine.** `fleet.route` ranks
  candidates by pressure (`queue / eligible fitting machines`), then a weighted
  lottery weighted by that count; which machine has the memory for a model
  belongs to the controller.

- **The weight is per model, not per site.** The Hub publishes `model_supply`
  (per model) and `capacity.by_operation` (per operation); `routing_candidates`
  reads the exact model's eligible machine count first, then the operation's, and
  falls back to the site-wide total only for a Hub that publishes neither. A site
  with ten 8 GB image Macs and one 24 GB Mac must not out-weigh a site with two
  24 GB Macs for a 24 GB model. `available` (ready with a free slot) breaks a
  pressure tie. Speed is **not** a routing input (the chip proxy and
  `GATEWAY_PREFER_FASTEST_SITE` were removed in 0.2.121: no gain in simulation);
  measured speed per model × Mac class is on Analytics, for ETAs and planning.
- **Admission is fit-aware (D1).** A group takes a job only while
  `min(operation room, slots(model) − our jobs there whose model fits no more Macs)`
  is above zero (`Worker._room`), so heavy jobs no longer fill a group's whole voice
  budget while its 8 GB Macs idle. Fleet lanes (tts/image/music/transcription) are
  automatic — they follow the fleet's published capacity — unless the owner types a
  ceiling, and fleet-lane work is not counted against `global` (D3).

- **A controller that cannot serve a model is a site property, not a job
  failure.** `can_publish` withholds a model whose `dispatch_state` is present and
  not `eligible` (absence stays unverified), so the picker never offers what the
  Hub will refuse. When the Hub still answers `409 MODEL_NOT_READY` /
  `MODEL_NOT_PACKAGED` at submit, the adapter names it `LOCATION_NOT_READY` and
  the worker re-routes to the next location that offers the same model. A voice is
  never re-routed — its reference clip lives on one controller.

- **Capacity belongs in the weight, never as a rank term.** Ported from the old
  `MultiSiteAssignment`: raw size as a rank made a small site unreachable, the
  same size ignored overloaded the small sites.

- **A voice pins the route.** A voice id names its owner
  (`studio-hub-<controller>::<raw>`), and a reference clip exists on one Hub, so
  voice work stays at that location.

- **The id Story Studio sent is the id it is told back.** The job stores the
  location it was routed to, but `catalog_encode` applies `publish_id` before
  answering, or a saved preset stops matching its jobs. Asserted end to end in
  `test_fleet_standalone.py`.

Secrets live in the credential store (`0600`); `fleet.json` holds the roster.
Lookups of "the providers" must go through `registry.definitions()`, not the
static `PROVIDERS_BY_ID` map: four bugs came from iterating it.

**A location's address comes from its *definition*, and one strategy forgot
that.** Every strategy resolves its base as `values.get("base_url") or
definition.base_url`. `_studio_hub` went from the credential values straight to
`hub_base_url()`, whose fallback is **this Mac's Hub**. A controller
declares no `base_url`, so every location discovered the *local* models; only
when pointed at the mock did the test surface "Studio Hub is not running (no
hub-ready.json found)" and zero models, which `tests/test_e2e.py`'s section 20
asserts against.

**Adding a location must also discover its models, and say what it found.**
Nothing refreshes them on a timer (`fleet.start_refresher` refreshes
*reachability*, not models), so a location added without a refresh shows none.
Models arrive **disabled**, as everything remote must, so the Fleet page says
how many were found and that none are available until enabled.

**A credential is checked, because both credentials work everywhere.**
`/api/hub/settings` answers 200 to the operator token, 403 to a fleet
credential, **401 to a wrong or absent one** (measured against Hub 0.5.1 — a
rejected token must never be reported as the fleet credential).
`PUT /api/fleet/controllers/{id}/credential` replaces a token without touching
the name, address or models.

**An absent credential stays absent — the operator token belongs to *this Mac's*
Hub and to no other address.** `hub_headers(token)` was `token or hub_token()`,
so a controller with no stored credential had **this Mac's operator token** put
in `X-Hub-Token` and sent to whatever address the owner typed. That token is
full operator scope: it can change this Hub's settings and identity, and reach
its storage. It is the mistake `credential_scope` exists to stop the owner, and
silent because a remote Hub that accepts it works. `token` is a **required**
argument and the fallback is reachable through `local=True`, derived by callers
from the **provider id** (`definition.id == provider_id_for(LOCAL_ID)`), never
the address, the one part an owner supplies. Local-only paths call
`local_hub_headers()`, so a future call site cannot inherit a credential by
omission: it fails with the wrong or absent token rather than with this Mac's.
`tests/mock_upstream.py` records every `X-Hub-Token` it receives, so the
assertion is about the **wire**, not a helper's return value.

**A custom provider's kind is checked against `IMPLEMENTED_KINDS` itself, not a
copy.** `_VALID_KINDS` was a second set, behind (`KIND_XAI` was missing), so
`"kind": "xai"` in `providers.local.json` was dropped without a word. Being
skipped is recorded with a reason, logged, and published as
`skipped_custom_providers` on `/admin/api/providers`, **which the console
renders** — a field nothing draws is the same defect as a control that does
nothing.

**A location's name is a label, never an identity, and renaming must stay safe
at any time.** `fleet.rename_controller` changes only the label — id, address,
token and enabled state untouched — so renaming can never cost a re-entry of a
credential. The provider id derives from the **controller id**
(`studio-hub-<controller-id>`) and the model id from the provider, so never
derive an id, key or path from a label. `fleet.json` also carries `local_label`,
which a write that adds or removes a controller must preserve.

Row actions are worth testing in the UI, not just the API: the Fleet page had
`testController` and `removeController` **wired to nothing** — no button ever
called them — so Test and Remove were unreachable from the app while the
endpoints worked. `tests/test_ui.py` now reads the buttons themselves
(`table_button_titles`); `table_texts` sees static text only.
## Voices are stocked, not configured

Moved to [docs/voices-and-reference-clips.md](docs/voices-and-reference-clips.md).

## Batch 4 — the remaining audit items (2026-09-20)

Moved to [docs/audit-batch-4-2026-09-20.md](docs/audit-batch-4-2026-09-20.md).

## Verification

```bash
.venv/bin/python tests/test_e2e.py              # 570 checks, isolated mock upstream
.venv/bin/python tests/test_fleet_standalone.py # ~50 checks (54 sites; the suite prints its own total), real Hub(s) as locations
.venv/bin/python tests/test_ui.py               # 130 checks + 5 Hub-dependent skips, drives the real app
```

**These three are the release gate, not the whole suite.** Every other
`tests/test_*.py` is a focused suite that runs in a second or two against its own
temp profile, and one of them went red for three days because it was not in this
list: run them all before a release (`for f in tests/test_*.py; do .venv/bin/python
"$f"; done`, skipping `test_e2e.py`, `test_ui.py` and `test_fleet_standalone.py`
for the quick pass).

`test_e2e.py` is isolated (`GATEWAY_DATA`/`GATEWAY_APP_SUPPORT` to a temp dir,
`GATEWAY_NO_MIGRATE=1`, random port, own key), cannot touch the live config;
`test_fleet_standalone.py` starts its own gateway and Hub on a random port, equally
safe.

**"Fully isolated" had a hole.** `hub.find_ready_file()` falls back to the owner's
real `~/Library/Application Support/StudioHub Standalone/hub-ready.json`, so an
isolated gateway saw his **live Hub as its local location** and a fan-out test
pushed a voice onto it, deleted by hand afterwards. `test_e2e.py` now sets
**`STUDIOHUB_READY_FILE`** inside the temp dir, so a test run has no local Hub. Any
new isolated suite must do the same, and any test that reaches a *location* must
point it at the mock — which serves `/api/hub/shared-voices` (multipart, recording
the payload and `X-Hub-Token`) so the fan-out is exercised without touching his
machine. `test_fleet_standalone.py` deliberately uses his real Hub, which is why it
is the suite to run knowingly.

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
re-run**. On 2026-09-20 a Studio Hub quit/reopen cleared a three-run failure of the
same two checks and gave **50/50**. So a worker answering `/health` is not a worker
that transcribes, voice annotation depends on that same route, and the remedy is a
Hub restart — re-running the suite will not do it.

**`test_ui.py` drives the real, installed app**, so run it deliberately: new code
needs a restart that interrupts him. Run it for UI-specific changes or proof;
else use the isolated suites and `curl`. Rebuilding is always safe;
**restarting the app is the part that intrudes**. **Never run it for a change that
touches no app file** — two runs against identical code gave two different failures.
It needs **Accessibility permission**, and skips Hub-dependent checks when Studio Hub
is stopped — an environment state, not a defect.

Verifying a change without touching his session: the isolated suites above, plus a
direct `curl` against the configured port.

Always rebuild before claiming it works: `./menubar/build.sh`, installs to
`/Applications/gateway-kh.app`. **"It builds" is not "it works."**

**Restarting the app is not the same as restarting the gateway, and a stale one
survives a rebuild.** The gateway outlives the app, so the app **adopts** whatever
answers `/health`.

  * `pkill -f "GatewayKH"` **kills the shell running it** — its own command line
    holds the pattern — so the command never finishes and the next `open` adopts it.
    Measured: a gateway from 21:47 still served at 23:33 through four "rebuild and
    restart" cycles.
  * otherwise a kill triggers a spawn that **fails to bind**
    (`[Errno 48] address already in use`) while the old gateway holds 8787, and the
    app adopts it.

So: **kill by PID, never by pattern**, confirm port 8787 is free and the gateway PID
*changed* before measuring anything. `pgrep -f` is safe for reading; `pkill -f` is
dangerous only when the pattern is your own.

**Quitting stops the gateway, deliberately, and the app can say which build is
running.** So:

  * `applicationShouldTerminate` stops the child, runs `killPortHolders()`, and
    waits (≤2 s) until `portIsHeld()` is false; quit → **port 8787 free**. The
    opt-out is a Settings checkbox, *"Keep the gateway running after I quit"*
    (`AppConfig.keepGatewayRunning`, default `false`).
  * `menubar/build.sh` writes
    `Contents/Resources/gateway/BUILD` (`<short-sha>-<UTC yyyymmddHHMM>`) and sets
    `GatewayBuild` in `Info.plist`; the app passes it as `GATEWAY_BUILD` on spawn,
    and `gateway/build.py` resolves env → `BUILD` beside the package → `"unknown"`.
    `/health` publishes it. **Check Gateway** compares running and app stamps,
    naming *both* when stale. Measured on a hand-started
    `GATEWAY_BUILD=fake-old-build-000000000000`: *"The gateway is running build
    fake-old-build-000000000000, but this app is cd866d5-202609191712. It is
    serving older code — restart it."* **Restart Gateway** then killed that pid
    (2414) and started the app's own (3252, stamp `cd866d5-…`), hence the
    port-holder kill, not `restart()`.

**A killed gateway comes back within seconds, so testing adoption takes a quit.**
**Quit the app first**, start the foreign process, then `open` the app: it adopts
what answers `/health`.

**Read a control's title to know it was built; press it to know it works.**
`tests/test_ui.py` gained `press_detail_button(title)` — the same three-level walk
as `detail_button_titles`, using `AXPress`, because `entire contents` is refused as
a specifier (-1700) and a title-only check passed for unwired buttons. The
suite presses **Check Gateway** and asserts a named build.
## Studio Hub wire contract — what the adapter owes the Hub

Moved to [docs/studio-hub-wire-contract.md](docs/studio-hub-wire-contract.md).

## Giving the app away — the credentials are already safe, the packs needed work

Moved to [docs/giving-the-app-away.md](docs/giving-the-app-away.md).

## Working rules

- **Every removal goes to that project's `_trash/` — never `rm`.** This covers
  *all* of it, not only what he asks for: a file he names, and equally a file you
  decide is clutter while cleaning up. Move it into the project's `_trash/` (create
  the folder if absent), keep any sub-structure, and say where it went. `_trash/`
  itself is **his** to sort out: never delete or tidy anything *inside* it. The one
  thing that is not a file is a Git branch or remote ref — record its name and SHA in
  a note under `_trash/` before deleting it, and never rewrite published history.
  `_trash/` is outside Git and nothing in it is read by the app, tests or build; see
  `_trash/README.md` for what is in it.
- **Every change is merged, committed, pushed, and built into a DMG — and the previous DMG moves
  to `_trash/dist/` automatically.** The owner's rule, 2026-09-26, and it is **the whole cycle every
  time**, not only for something you would call a release: a fix, a doc, a test, a one-line constant.
  Land it in `main`, push it, and leave an installable artefact in `dist/` so it can be handed to
  another Mac with no further steps. **Do not call work stable, done or shipped while any of those
  is missing.** `menubar/build.sh` moves the previous DMG aside itself — that line used to be
  `rm -f "$DMG"`, so a rebuild destroyed the previous artefact of the same version — which means
  `dist/` holds exactly the build you just made and `_trash/dist/` holds the history, with no
  tidy-up step for anyone to remember. Keep `dist/` out of Git. A project that ships a Mac app but
  has no DMG step gains one rather than being exempt.
- **Version bumps are patch-only for now: `0.2.1`, `0.2.2`, … up to `0.2.1000`.** The owner asked for this (2026-09-22). `VERSION` at the repo root is the one place to bump; `menubar/build.sh` reads it for `CFBundleShortVersionString` and the DMG name, and the sidebar shows it beside the build stamp. Increment the **third** number by one per shipped change — do not roll to `0.3.x` or `1.0.0` until the owner says so.
- **Test narrowly, and off the owner's screen. The full `tests/test_ui.py` is a
  release gate, not a routine check.** It walks every section, forces the window
  to 700 pt, selects sidebar rows and presses controls, so it takes over the
  owner's app — running it after each change interrupts his work, and he has said
  so. The default per change is the **smallest** check that proves it:
  - console/HTML/JS change → the Node VM suites (`node --test tests/<file>`),
    which never touch the app;
  - gateway/Python change → the isolated suites (`tests/test_e2e.py`, or the one
    focused file), which use their own profile;
  - native Swift change → build, then a **named, read-only check** from
    `tests/check_native.py` (it reads one element; it does not resize, walk
    menus or front windows). Run it in the background.
  Reserve the full `test_ui.py` for an explicit owner request or a promotion
  gate, and run it in the background. **Other sessions testing Studio Hub or
  Story Studio do not bother him; taking over this app's windows does.**
- **Never hand another session a description of behaviour that is not deployed
  yet.** A handoff from a build while the running service is still on the
  previous one is a false claim to whoever measures it: a note promised
  `legacy_ids` on every Studio Hub row while the live gateway still published
  `[]`, because the code was in the bundle and the process had not been
  restarted. The Story Studio session checked, found the note wrong, and was
  right. Either restart and measure the **running** service first, or say a
  change is built but not yet live. Both sessions lost a day to notes asserting
  behaviour nobody had verified.
- Prefer the **graphify** graph (`graphify-out/`, gitignored, rebuildable) over
  grepping for architecture, caller and impact questions, and keep it current
  with `graphify extract . --update` after structural work. Its docs need an LLM
  key, so run it as `graphify extract . --update --code-only` unless someone
  wants to spend a key on the prose. It reports a **syntax error in
  `menubar/VoicesWindow.swift`** — a parser limitation, not a defect:
  `swiftc -parse` is clean, so check the compiler before believing it.
- **A page must never be able to resize the window.** A page whose content has a
  *required* height propagates it, and AppKit satisfies it by growing the
  `NSWindow` — the Fleet page did that to 1162pt on a 1080pt screen. Such a
  page wraps its body in an `NSScrollView` with a `FlippedView`, as
  `FleetSection`, `OverviewSection` and `SettingsSection` do. Pages filling the
  pane (Jobs, Voices, Providers) need no scroller while their minimum is
  *small*: a flexible height has nothing to propagate. Voices declared
  `heightAnchor >= 320` and Jobs `>= 260`: a **required** floor propagates, so
  both could still grow the window at the 860x560 minimum, and Jobs' wrapping
  label from one long `error_detail`. Keep a table's floor low (140 stops it
  collapsing) and cap any wrapping label with `maximumNumberOfLines`. Settings:
  `bottomAnchor <= host` stopped growth but left the surplus nowhere, so bottom
  rows were clipped with no scrollbar. **The fix is tested**: `test_ui.py`
  forces the window to 700pt, visits every section, and asserts the size does
  not change.
- Mark deliberate simplifications with a `ponytail:` comment naming **the
  ceiling and the upgrade path** — a curated exception list, a flat default, a
  fixed pool. Unmarked, the next reader cannot tell a considered trade-off from
  an oversight.
- **`freshness` reports two ages, but only one can go stale.** `ageMs` times the
  last *assembly* and can never reach the 30-minute gate: `freshness()` calls
  `rebuild()` first, resetting the clock, and `rebuild()` only re-sorts
  `models_cache.json`, so `stale` is `false` by construction. **Left exactly as
  it was, deliberately**: making the gate fire would disable every model on a
  stale inventory, an outage not a fix. The `inventoryAgeMs` /
  `inventoryOldestProvider` / `inventoryOldestAt` pair is honest, recorded from
  the oldest contributing provider snapshot. Measured live: `ageMs` 4.8 s
  against an inventory **19 hours** old (`openrouter-main`). `null` means no
  provider reported a fetch time: unknown, not zero.
- **The Hub's `TextGuidance`** (`soft_max_chars`, `chunking`) is authoritative
  about local voice engines but is **not served over its API** (checked against
  0.6.29: `/api/hub/catalog` carries `long_form_delivery`, with a per-model
  `section_max_characters` for everything it *does* section, but no
  `text_guidance`). The hard-capping families are recorded in
  `catalog._HARD_CAP_CHARACTERS` — bark 150, orpheus 170 — gated on
  `long_form_delivery` so the Hub can obsolete it without anyone editing the
  gateway. **When the Hub serves `text_guidance`, that table and its staleness
  both disappear.**
- **When you read a fact out of the Hub's own source, read the copy in the
  *installed bundle* and check its version against the Hub answering.**
  `~/Applications/Studio Hub.app/Contents/Resources/voice/app/backend/catalog.py`
  serves. `~/Projects/studiohub-standalone` is a development checkout and can be
  **ahead of it** — 0.6.31 while the running Hub reported 0.6.29 — so a
  number read there is a claim until the bundle agrees. The file you can find is
  not automatically the running one, and the Hub auto-updates (0.6.18 → 0.6.23
  → 0.6.29 → 0.6.31) enough that the difference is not hypothetical.
- **Every release updates `docs/tasks/HANDOFF.md`.** Read it first in a new
  session, and keep it current: the version, anything newly learned, anything
  newly forbidden, and what is still open. A stale handoff is worse than none,
  because it is trusted — if it disagrees with the code, the code wins and the
  file gets fixed.
