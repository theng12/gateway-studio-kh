# Audit backlog — recurring bug shapes, and every instance found

## Product audit and first repairs — 2026-09-20

The broader product audit is in [docs/2026-09-20-product-audit.md](docs/2026-09-20-product-audit.md).
Its new findings remain open except for the scoped repairs below. Older “all
closed” statements in this file refer to the earlier six audits only.

[MEASURED] This repair batch changes:

- API guards reject keyless access when the configured bind is beyond loopback.
- The worker atomically claims only still-accepted jobs, so a queued cancellation
  is respected even when the worker already fetched its pending list.
- Chat models ending in `:batch` on the OpenRouter host are withheld from the
  synchronous catalogue, including custom providers using that host.
- Create preserves the selected model across refreshes, requires a new selection
  if it disappears, prevents duplicate pending submissions, reports polling
  failures without resubmitting, and renders cancellation as terminal.

[MEASURED] Verification: the isolated end-to-end suite passed **521 checks** before
and after the repairs; `tests/test_audit_regressions.py` passed **6 tests**;
`tests/test_job_claim.py` passed **2 tests**. The offscreen native check in
`tests/test_create_workspace.swift` passed selection, delayed refresh, pending
submission, polling-failure and cancellation cases. `uv pip check --python
.venv/bin/python` found all **23 installed packages compatible**; this is dependency
consistency, not a vulnerability audit.

The full installed-app UI suite and real-Hub fleet suite were not run in this
pass. The native check validates state, not rendered layout. Paid generations,
Content Pack installation and restart reconciliation remain unverified. The
running app has not been restarted to activate this batch.

---

Written 2026-09-20 after the owner asked: *"can you see any patterns of bug we
encounter across our sessions that has a shape and check to see if it's still
lingering across other locations too? then fix it once and for all?"*

Six read-only audits were run against this repository, one per shape below. This
file is the durable record: what each shape is, every instance found, and the
smallest honest fix. It is not a design document and it does not re-litigate
anything the code already marks as deliberate.

**Status of the six audits:** **all six returned.** Shapes 1, 2, 3, 4, 5 and 6
below each hold one audit's findings. Two of them audited the tree while the fixes
in commit `ccc05ba` were being written, so a few of their line numbers moved and
one of their findings (the status menu reading `enabled_models`) was already fixed
by then and is recorded as closed rather than open. Findings are quoted with
`file:line` from the tree at the time of the audit.

**Fixed from this backlog so far:** Shape 1 (the `tts_model_id` misattribution —
customer-visible), Shape 4 (#2, the Swift app reading a field the API never
serialises; #3, `add_user_model` accepting an unserved modality), Shape 6 (#1, the
destructive `.env` rewrite; #7, the menu-bar count) and the count-propagation
instances across the console and the menu bar.

---

## Decisions closed — do not reopen (2026-09-20)

Three threads the owner settled. Recorded so a later session does not spend time
re-investigating them.

- **OpenCode Zen / Go — not accepted. Do not raise it again.** Kept only so nobody
  repeats the work: `GET https://opencode.ai/zen/v1/models` *is* public (200, 74
  models), which is what makes third-party "no API key, no signup" plugins look
  functional — but `POST …/chat/completions` with a free model answers
  `403 FreeTierError: "OpenCode's free tier can only be used from within OpenCode"`,
  identically across four User-Agents, and that wording gates on the *client* rather
  than on credentials, so an API key would not lift it. Paid models keyless answer
  `401 AuthError: Missing API key`. The free capacity the owner wanted is already
  wired elsewhere — 26 free rows offered through OpenRouter and Token Harbor,
  including `deepseek-v4-flash:free`, the same model Zen gives away.
- **Kie V6 end-to-end — deferred to the owner.** He will run the generation himself
  and report; if it fails we fix it then. The lane is restored and reachable
  (`kie-ai-images::V6` is published in `/v1/music/models`); what has never been proven
  is a *real* generation through the gateway, which costs about 12 credits. Do not
  spend them.
- **The fleet suite's two Hub-transcription failures — resolved, and not a defect.**
  "a transcription ran on the location end to end" and "the artifact comes back
  through the gateway" failed on three consecutive runs while the Hub's voice runtime
  held a worker at 38 MB RSS that had loaded no model. A Studio Hub quit/reopen
  cleared it: **fleet 50/50**. The documented remedy on that Mac — restart the Hub,
  do not file it as a gateway bug — was correct, and re-running the suite was never
  going to fix it.

## Reaching this gateway from another device (Tailscale)

Verified on this Mac on 2026-09-20, whose Tailscale address is `100.118.161.120`:

1. Set `GATEWAY_HOST=0.0.0.0` in `.env` (the app writes `127.0.0.1` there when the key
   is absent, and Settings → Save touches it).
2. Restart the gateway. `http://100.118.161.120:8000` then answers **and**
   `http://127.0.0.1:8000` still does. That second half matters: binding the Tailscale
   address *alone* leaves loopback dark and silently breaks Story Studio's configured
   `genstudio_url`. Use `0.0.0.0`, never the Tailscale address by itself.
3. The first request from another device is refused, and so is one from this Mac:
   `/admin/*` demands the gateway key whenever the bind is not loopback, because the
   check is about the bind rather than the caller. The console then shows a key field
   naming that reason — paste the key from Settings once and that browser remembers
   it. The menu-bar app injects the key itself, so it never asks.
4. `/v1/...` still wants the Story Studio key, unchanged.

Both halves were broken before `75ddb9e`: `spawn` passed `--host 127.0.0.1` on the
command line whatever `.env` said, so the setting was a lie, and the console had no way
to send a key at all. Contents needed fixing too — an `<img src>` sends no headers, so
previews and downloads fetch bytes and use a blob URL rather than putting the key in a
URL.

## A second result from one request, and the index that was accepted and ignored (2026-09-20)

The owner asked: *"for the cloud api providers. do we retain the generated assets
also? i mean suno v6 returns 2 audio clips so we should be able to keep both."*

**We kept the first and dropped the rest.** Shape 4 (a control that does nothing)
and shape 6 (state that changes but cannot be seen), in three places:

- `adapters/kie.py` parsed **every** entry of the music `data` array — the comment
  beside it even said so — and then downloaded `urls[0]` only.
- `adapters/openai_compat.py` took `entries[0]`, so a response carrying more than
  one image dropped the rest; and `n` was hardcoded to `1`, so the multi-image case
  was unreachable through the gateway at all.
- `api.get_music_output(job_id, asset_id)` accepted `asset_id` and never read it, so
  `/outputs/0`, `/outputs/1` and `/outputs/anything` all answered track 0 — the one
  route that exists to reach a variation, pointing at the wrong one.

This was structural rather than an oversight: `BackendResult.content` was a single
`bytes`, so "one artifact per job" lived in the result *type*.

**Fix.** `BackendResult.artifacts` is the ordered list (`content` remains the first,
so every single-artifact lane and caller is unchanged); the worker writes each;
`jobs.artifacts_json` stores them with `store.artifacts_of()` as the single reader,
falling back to `asset_path` so a job written by an older build still answers; the
envelope publishes `outputs: [{asset_id, url}]`; and a new modality-agnostic
`GET /v1/jobs/{job_id}/outputs/{asset_id}` serves any of them. The music route
honours the index it was always given, and an index the job does not have is refused
404 naming what it does have. `n` is caller-settable 1..10 and a count outside that
is refused naming the range rather than clamped — returning fewer than were asked for
is the same defect as dropping the extras. A variation that will not download is
counted in `variations_failed` instead of failing a call that did produce a usable
result. Retention needed no change: `retention._files()` walks the whole artifact
directory, so extra artifacts fall under the same 7-day / 10 GB policy.

**Evidence.** e2e **495/495**, fleet **50/50**. Reverting both halves of the fix
fails exactly **4 of 495** checks and nothing else — the two-track assertions, the
404 for a missing index (it answered 200 instead), and the generic route. The mock
now answers two byte-different tracks and per-index images, so the old hardcoded
one-entry array cannot pass. Verified live against the real database on the pre-fix
job `643ba467`: a legacy row carrying only `asset_path` still publishes one output
and serves 744,608 bytes, while `outputs/5` answers
`404 {"code": "ARTIFACT_NOT_FOUND", "detail": "This job has no artifact '5'; it has 1."}`.

**Left deliberately.** Artifacts lost before this fix are not recoverable — the
payload stored only `provider_url: urls[0]`, so the second track's url was never
kept. Existing jobs hold one artifact and answer honestly about it.

**A deploy note, not a gateway defect.** After `menubar/build.sh`, a *running* app
respawns a gateway from the new bundle but stamps it with the app's own launch-time
`GatewayBuild`, so a rebuilt bundle can serve new code under the old stamp. Measured:
`openapi.json` carried the new route while `/health` said `fb45670-202609200900`. It
errs safe — it reports stale when the code is current, never the reverse — and
restarting the app makes the label agree. Anything that *acts* on a build stamp
should read the route table or restart the app, not trust the label.

---

## A chat catalogue is not a list of chat models (2026-09-20)

The owner asked to look at free providers — *"nvidia, openrouter and all the other
providers who offers free"*. Auditing what was actually offerable found the
**third** instance of the same shape, and it was live on NVIDIA.

**NVIDIA NIM answers every one of its 82 rows with only `{id, owned_by}`** —
measured, no type field at all. So its embeddings (`nvidia/embed-qa-4`,
`nemotron-3-embed-1b`, `nvclip`), safety classifiers (`nemotron-3.5-content-safety`,
the `nemoguard` pair, `llama-guard-4-12b`), a reward model (`nemotron-4-340b-reward`),
OCR/parsers (`nemotron-parse`, `google/deplot`), translators (`riva-translate-4b-*`)
and a calibration model were all published as `modality: chat`, **publishable**, and
offered as something to tick. Enabling one produced a model that reached Story
Studio and failed the moment it was picked. Free NVIDIA capacity was the obvious
next pool to drain, so this was about to be hit.

`chat_can_serve()` withholds the families by id, consulted from `can_publish` beside
the SRT and music rules. Measured live before the change: it withholds **34 rows
across six providers** (NVIDIA 21, OpenRouter 4, Experiential Labs 3, OpenAI 1,
Google 1, Google free 1) and **not one was enabled** — nothing the owner could pick
changed, the same standard the fal image-to-image fix was held to. The non-NVIDIA
catches are real: `gpt-realtime-translate`, `gemini-3.5-live-translate-preview`,
`gpt-oss-safeguard-20b`/`120b` and `llama-guard-4-12b` are classifiers or streaming
translators. Vision models are deliberately excluded from the rule — a vision chat
model is a chat model that takes an image, and withholding those would remove
working lanes.

**Groq's Whisper does not serve this gateway, and the notes here said it did.**
`whisper-large-v3` answers `HTTP 400: response_format must be one of [json text
verbose_json]` — measured live by *running* a transcription, not by reading — and
this gateway must ask for `srt`. Both Groq Whisper ids are now withheld.
`AGENTS.md` had claimed since 2026-09-19 that Groq transcription worked through the
shim; that claim was wrong in the direction that publishes a model failing on use,
and it is corrected. The list matches the bare id, which is safe only because the
local Hub's Whisper carries a namespaced id (`mlx-community/whisper-large-v3-turbo`)
that it cannot match — asserted, so a future edit that breaks that cannot pass
silently.

**Free lanes now say what they do with your prompts.** `ProviderDefinition.data_use`
is three-valued with **`unverified` as the default**, because "nobody checked" must
not publish as "safe": `non_private` for Google's free tier (published as using
content to improve their products, unlike the paid tier) and Token Harbor (its own
note already said so), `unverified` for everything else. `free_tier` marks a
provider that *is* a free tier, and `_is_free_lane` reads a declaration rather than a
price, so a merely-cheap model is not called free. The console draws it on the model
rows and the provider cards — a field nothing renders is the same defect as a
control that does nothing — and the e2e suite asserts the HTML actually calls it.

**Enabled in the same pass:** 17 curated models (0 missing, each id verified against
the live catalogue first) — Groq chat (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`),
6 OpenRouter `:free` rows, and 7 NVIDIA chat models. Offered went **110 → 127**.
Verified live, not assumed: a Groq chat completion returned a real answer.

---

## Create — generating an asset without Story Studio in the loop (2026-09-20)

The console could manage models and providers but could not make anything: the
`/v1` routes want the Story Studio API key, which a browser page does not hold. So
`#create` is a sixth console view with the same six lanes as Studio Hub's Create
section — **Audio, Subtitle, Image, Music, Video, Chat** — in its order and with its
owner-facing names (`Subtitle` is what it calls transcription), laid out the way its
workspaces are: form on the left, result on the right, empty state until there is
something to show. The one thing deliberately **not** copied is its wording: those
pages say *"generate it locally"* because their engines are local, and these lanes
are cloud models, so this says so, and warns that an unkept asset is pruned in 7 days.

**`POST /admin/api/create` wraps the same `_submit` the customer routes call**, not
a second copy of the rules — resolution, the enablement gate, the cross-modality
idempotency refusal and the modality dispatch all stay in one place. The picker is
built from `_flat_models()` filtered to what is *enabled*, because that is exactly
what `_submit` resolves against, so it cannot offer a row the submit would refuse.
Transcription has its own route (it carries a file) and stages the upload the way
the quote/confirm lane does, so both end in the same `_submit("transcription", …)`.
Every artifact carries Save and **Keep** — an asset made here directly has no
project holding the copy that matters, so the prune would otherwise be its only copy.
Music renders one player per variation, which is what the multi-artifact fix was for.

**Two defects found while building it, both in existing code.**

1. **`admin._error` dropped the machine-readable `code`.** The console's refusals
   came back as `{"detail": …}` where `/v1` says `MODEL_NOT_FOUND`: one fact spelled
   two ways, and the caller that has to branch on it got less. Caught by a new check
   asserting the refusal names the code, which is why the check was written before
   the fix existed to satisfy it.
2. **The SRT withhold list was spelling-by-spelling.** A real job failed with
   `gpt-4o-mini-transcribe-api-ev3: response_format 'srt' is not compatible` — an id
   nobody had listed, while four of its siblings were. A version suffix is not a new
   capability, so `transcription_can_serve` now matches the `gpt-*transcribe*`
   family, and the check names a spelling that does not exist yet.

**Evidence.** e2e **521/521** — eight new checks, including a generation started
*and* polled through the console's own routes, and a check that the view actually
ships with the page (`Create` must be reachable at `#create`, not merely
implemented). A real Groq chat completion through `/admin/api/create` returned `ok`
(free lane). The Create view and the button states were rendered headlessly and
looked at. Live refusal now reads
`404 {"detail": …, "code": "MODEL_NOT_FOUND"}`. Committed `a5ffa68`.

### Not claimed green: the fleet suite's two Hub-transcription checks

`test_fleet_standalone.py` failed **the same two checks on three consecutive runs**
("a transcription ran on the location end to end" — job left `running`; "the
artifact comes back through the gateway" — no job succeeded), so this is **not** the
one-off transient `AGENTS.md` describes, and it is recorded here rather than
re-run until it passes.

What the diagnosis showed: the Hub answers `/api/health` in **1.7 ms**, and its voice
runtime holds a worker at **38 MB RSS / 10 s old** — the exact signature this repo
already records for "the worker is alive and answering but has loaded no model".
The gateway dispatches the job, the Hub never completes it, and the gateway waits.
Nothing in this batch touches the `studio_hub` transcription path (the family rule
does not match `mlx-community/whisper-base-mlx`, and the job reaches `running`, so it
was dispatched rather than withheld) — but that is reasoning, not proof, so it is
left **open** and flagged. Worth checking next: whether this Mac's Hub has
idle-release set to unload immediately (16 GB machine), which would make every
transcription a cold load, and whether one cancelled mid-flight transcription
earlier in the session left its worker wedged.



## The six native Create pages, and a menu that had fallen behind (2026-09-20)

Phase 3. The app now has `Section("Create")` above `Section("Library & app")` with
Studio Hub's six lanes in its order and with its owner-facing names — **Audio,
Subtitle, Image, Music, Video, Chat**. `PageKit` already carried Studio Hub's house
grammar (`heading`, `sectionTitle`, `empty`, `formRow`, `help`), so the page is **one
view driven by a `CreateLane` value**: heading, form left, result right with its empty
state, Generate, poll, preview. Studio Hub frames its three workspaces with one shared
body so they *"cannot drift apart one fix at a time"*; six near-copies here would
drift the same way, faster, so there are none.

Images preview in an `NSImageView`; audio and video get an Open (in whatever the owner
already plays them with) rather than a reimplemented transport, plus Save… and
**Keep** — an asset made here directly has no project holding the copy that matters.
`GatewayAPI.upload` is new, because transcription is the only lane whose payload is
not JSON, and it keeps the same `{"detail": …}` refusal handling as every other call:
the value of a 413 is the sentence naming the limit.

**The defect this exposed: the View menu was a hand-kept list and fell behind
immediately.** Adding the lanes to the sidebar left the menu without them — the UI
suite found it by clicking "Audio" and getting `Can't get menu item "Audio"`. It is now
built from `MainWindow.viewMenuSections` through a single action, so a section that
exists in the sidebar cannot exist without a door. Key equivalents for pages that
already had them are deliberately unchanged (⌘1 Overview, ⌘2 Jobs, ⌘3 Voices, ⌘4
Models, ⌘5 Providers, ⌘6 Fleet): renumbering to match the new order would retrain the
owner's fingers for no gain.

**Evidence: UI 128/128** against the real installed app — every lane appears in the
sidebar and the View menu, each selects its own window, and none resizes the window
(the 700pt guard now visits all fifteen rows). The build stamp names HEAD
(`e26f650-202609201039`). Two failures on the way were each diagnosed rather than
re-run until green: the missing menu items (a real defect, fixed above) and a status
line still reading *"Starting on 127.0.0.1:8000…"* seconds after a relaunch (the app's
status timer had not ticked — the re-run is clean).

**Not verified, and not claimed:** the native page was never *looked at*.
`screencapture` is refused without Screen Recording permission, so the claim above is
structural — from the accessibility tree — not visual. The console's Create page *was*
rendered and looked at, and it is the same design, but that is a different surface.

## Batch 4 — every remaining item, and what closed it (2026-09-20)

The `### Open (as written — every item below is closed; see Batch 4 at the top)` lists below are kept as the audit wrote them, for the reasoning and
the line numbers. **Every item in them is now closed** except where a note says
otherwise; this section is the outcome, with the evidence, so the lists are not
misread as a to-do.

All three suites were re-run after this batch: **e2e 485/485**, **fleet 50/50**,
**UI 109/109**. The e2e count went 444 → 465 → 478 → 485 as checks were added
(the last seven for Kie's music lane); the fleet
count 47 → 50.

### Shape 4 — a control that does nothing, or one rule computed twice

- **4.5, Kie music** — **withdrawn, then restored properly the same day.** The
  first fix removed the lane: an out-of-date third-party reference (verified
  2026-08-26) said Kie's Suno family was a dedicated `/api/v1/generate*` API that
  this adapter could not speak, and Kie's docs do keep a separate "Music
  Generation" section, so the curated `V6` row looked like a lane that could only
  fail on use. **That reading was wrong.** Kie has since moved music onto the same
  unified `POST /api/v1/jobs/createTask` route the adapter already speaks: the
  top-level `model` is the constant **`ai-music-api/generate`** and the Suno tier
  (`V6`, `V6_MINI`, `V6_WILD` — the page now strikes V4/V4_5/V5_5 through) rides
  in **`input.model`**. Measured live on 2026-09-20, at a cost of **12 credits**:
  such a body queued a task, `recordInfo` reported `state: "success"` with
  `status: "SUCCESS"`, and `resultJson` parsed to
  `{"code": 200, "data": [{audio_url, stream_audio_url, image_url, id, duration,
  model_name, …}]}` — a **`data` array of tracks**, none of the `resultUrls` keys
  the image and video lanes use. So there were two real bugs hidden behind the
  withdrawal: the body named the tier where the route wants the api-model, and the
  result parser would have reported *"no artifact url"* on a **successful**
  generation. Both are fixed: `KieBackend` sends the constant at the top level and
  the tier inside `input`, `_music_input` maps this gateway's music contract onto
  Suno's (`lyrics`→`prompt`, camelCase and snake_case both accepted, `custom_mode`
  inferred from `style`/`title` when the caller does not say), and
  `_urls_from_result_json` reads `data[].audio_url`. Music is back in `KIND_KIE`,
  the definition declares it, and the three live tiers are curated. The e2e suite
  drives the whole lane against the mock and asserts the **wire body** — which is
  the check that would have caught the original mistake.
  - **What is still unproven:** that the *gateway* can carry a real song end to
    end. The route, the body and the result shape are measured; a generation
    through the gateway itself would cost another 12 credits and was left for the
    owner to trigger by picking the model.
  - **The 12 credits were spent by accident.** A probe intended to be rejected (a
    valid api-model with no prompt) was accepted and queued; the balance read
    1270.59 → 1258.59 confirmed it. Recorded here rather than quietly absorbed.
- **4.6, `summary()` and `is_configured`** — **was already fixed** by deriving
  `publishable_models`/`by_modality` from `enabled_models()`, which applies the
  gate. Verified by reading; nothing to change.
- **4.7, "Browse in Models tab"** — `openInBrowserFilter` set `filtersPopulated =
  true` before the select was populated, so on a Providers-first launch the filter
  silently became `""`. It now awaits `loadModels()` first.
- **4.8, the two console toggle paths** — `setModel` had no try/catch (the promise
  rejected into the console, the checkbox kept the clicked state and nothing said
  so) and `toggleModel` re-rendered a locally mutated row. Both now re-read the
  server's answer and alert on refusal.
- **4.3's remainder, rendering `unserved_modalities`** — the console renders the
  lanes a provider declares but its adapter cannot serve, as a badge with the
  reason. Asserted at the level available without a browser: the served console
  source references the field.
- **4.9, `admin.py`'s unguarded `response.json()`** — a 200 with a non-JSON body
  raised `JSONDecodeError` out of the route, a 500 *after* the voice had been added
  upstream. The id that was asked for is the answer, so an unreadable body falls
  back to it. Proven with a mock route that answers 200 `text/html`.
- **4.11, cloning computed twice** — `catalog._supports_cloning` and
  `voices.reference_support` were two implementations with different fallbacks. One
  read now: `voices.supports_cloning` bridges the three-valued `reference_support`
  to the bool the catalogue must publish, with `CLONING_KINDS` as the single
  per-kind default. Asserted three ways, including a new mock model whose only
  statement (`capabilities`) omits cloning — the old code published `true` for it
  while the picker was empty. Measured: **no currently-published row changed**.
- **4.12, the TTS price shape defined twice** — `voices.story_row` hand-listed four
  of the six keys and was missing `cost_tier` and `standard_credits`. It is merged
  from `constants.zero_pricing("tts")` now, and the e2e check reads the key set from
  that function rather than listing it again.

### Shape 5 — identity from a label, or two namespaces for one thing

- **5.4, the reserved location namespace** — the bare `studio-hub` id (the
  published namespace every location collapses into) is refused, and so is any
  other kind wearing the `studio-hub-*` prefix. A `studio_hub`-kind entry under it
  still loads, because that is the shape the Fleet page itself writes — the first
  version of this check rejected the test suite's own mock location, which is how
  that distinction was found.
- **5.5, kind judged by id prefix** — the remaining `provider_id.startswith(
  "elevenlabs")` is gone with 2.7: the container comes from the provider's kind.
- **5.6, `set_many` writing non-provider ids** — it resolves through `_definition`
  like `set_model_enabled` always has, and the console's row path skips ids that do
  not resolve. Asserted: enablement sent for `studio-hub` writes nothing.
- **5.7, curated limits keyed by the concrete id only** — `for_model` tries the
  concrete id then `publish_id(provider_id)`, so an entry written the documented
  `studio-hub::<model>` way is read. The concrete id still wins.
- **5.8, `_preview_model` reading only `static_models`** — ElevenLabs publishes
  none, so every preview for a discovered voice named `model_id=""` and 404'd. An
  enabled discovered row is preferred now.
- **5.9, `add_controller` deduping on the address** — the Hub's stable `site_id`
  (already read by `_probe`) decides identity, with the address as the fallback.
  `site_id` is published on the roster row. Proven in the fleet suite: the same Hub
  re-added at its `localhost` alias is the same location, and no third row is
  minted. The suite's own second location had to be written straight into the roster
  (a supported input) because the API is now right to refuse it.
- **5.10 / 1.5, a merged row keeping the first location's facts** — `locked` was the
  only field that merged. `catalog._merge_capabilities` now folds in the rest with a
  stated direction per field: cloning is the **union**, `maximum_input_characters`
  the **smallest** (with its source), the speed range the **intersection**. And
  `voices._model_record` prefers a location's record that can actually publish the
  model rather than the first in iteration order.
- **5.11, `SettingsSection` silently rebinding to 8000** — a mistyped or empty port
  is refused, the real value is put back, and the label says nothing was saved.

### Shape 1 — one value answering two different questions

- **1.4, machine online counting** — a missing state was coerced to `unknown` and
  counted **online**, and the headline total came from a different list than the
  table. Both now derive from `machines_flat`, an unknown state counts as neither,
  and `machines_unknown` is published and shown ("N with no state reported").
- **1.6, one word for two totals** — `/admin/api/models` publishes
  `listable_total` (what the browser can list) instead of `total`, because
  `/health`'s `total_models` answers a different question (models on providers that
  are switched on). The console reads the new key.
- **1.9, modality falling back to `modalities[0]`** — the fallback is load-bearing
  for single-modality providers and for providers that state nothing (measured: a
  strict "must be stated" rule would have dropped **265** live chat models). What
  was actually wrong is narrower and is fixed: a provider's own classification is
  now read first — fal's `category` via its strategy, and OpenRouter's
  `architecture.modality`, which was fetched, stored and never read. Measured
  effect on the live cache: **14 reclassifications**, all corrections (5
  `thinkingmachines/inkling` rows video→chat; 9 fal understanding endpoints
  image/video→chat, which fal's own filter then drops). None was enabled.
  `_modality_from_architecture` reads only an unambiguous output side, because
  `openrouter/auto` produces `text+image` and `google/lyria-3-*` produce
  `text+audio` — reading either as definite would have published a router as an
  image model and a music model as chat.

### Shape 2 — an unknown collapsing into a definite value

- **2.1, video `modes`** — read from `raw["category"]` and the model id, with
  `modes_source` (`provider` / `gateway` / `null`). A model that states nothing
  publishes `[]` and no variants rather than a definite image-to-video. Measured
  live: the fal `text-to-video` rows stop claiming image-to-video, and xAI
  genuinely gains text-to-video (its adapter sends a prompt-only body).
- **2.3 / 2.4, image reference capability** — three answers from the model's own
  statement (the Hub's `capabilities`, fal's `category`, the id), never a definite
  `False` for everything that is not a location while `operations` claims
  `image-to-image`. Measured: the live Studio Hub FLUX2 row says `capabilities:
  ["txt2img"]` and now publishes `supports_reference_images: false` with
  `operations: ["text-to-image"]`, where it previously claimed both. And a stated
  `image-to-image` endpoint on a kind whose image body cannot send a reference (fal,
  the shim, xAI, MiniMax) is **withheld**: measured **116** fal image rows, none
  enabled, so nothing the owner could pick changed.
- **2.6, `structured_output_mode`** — `response_format` is JSON-object mode and
  `structured_outputs` is JSON-schema mode; the field was a flat `"json_object"`
  for either. Measured: 15 rows `json_object`, 1 `json_schema` (previously
  `json_object`), 46 `null`.
- **2.7 / 2.8, `output_format` guessed from an id prefix** — one function
  (`voices.tts_output_container`) reads the kind's container from the adapter's real
  media type, and both the voice row and the TTS quote use it. A fal ElevenLabs
  voice published `wav` while `adapters/fal.py` downloaded `audio/mpeg`.

### Shape 3 — a value we invented, read as the provider's

- **3.3, the 21 curated fal example names** — `SOURCE_EXAMPLE`, `owned=False`:
  they are fal's documentation examples, not the owner's account entries. The tier
  now follows `owned` (one rule, used by the voice row and the quote), so they
  publish `library` with a public id and no `custom_voice_id` — and a fleet push
  cannot look authorised for a voice the owner never added.
- **3.4, `estimated_duration_ms`** — kept (the field is an estimate by name and the
  UI wants it) but now carries `estimated_duration_ms_source: "gateway"` and a
  `ponytail:` naming the flat 14 chars/second as ours and the upgrade path.
- **3.5, `anthropic.DEFAULT_MAX_TOKENS`** — the Messages API requires the field, so
  a number is inevitable; the number the gateway will send is now **published on
  the Anthropic rows** with `max_output_tokens_source: "gateway"`, from the same
  constant the adapter reads. The row and the request cannot disagree again.
- **3.6, image geometry** — one table (`IMAGE_SIZE_BY_RATIO`) is read by both the
  catalogue and the OpenAI-compatible body, so what is offered is what is sent. The
  catalogue advertised 1344x768 for 16:9 while the adapter sent 1792x1024, a
  DALL·E-3-only size `gpt-image-1` answers 400 for. `reference_image_max_dimension`
  is published only where references are supported, and the counts carry a
  `ponytail:`.
- **3.7, music lyrics/title caps** — the existing `ponytail:` now names all three
  invented caps and the upgrade path, instead of describing a different key.
- **3.8, the DALL·E-3 size table** — replaced by the shared three-size table (see
  3.6). The `alloy` default voice was already removed in an earlier batch.
- **3.10, OpenRouter's `output_modalities`** — answered under 1.9.
- **3.11, a comment asserting a field ElevenLabs does not publish** — corrected, and
  the dead `can_use_style` read removed.
- **3.12, `supported_languages[:40]`** — the truncation is gone; a provider's stated
  list is published whole.

### Shape 6 — state that changes but cannot be seen

- **6.8, quoted uploads pruned lazily** — the retention timer sweeps the in-memory
  quote table as well as the artifacts. Deliberately **not** popped on confirm: a
  retry of the same idempotency key is only idempotent while the entry is readable,
  and dropping it would turn a legitimate retry into a 410.
- **6.9, `limits.py` keyed on mtime only** — mtime **and size**, the pair
  `registry.py` documents for this hazard.
- **6.10, two fleet paths** — `providers/registry.py` imports `fleet.FLEET_PATH`
  (which honours `GATEWAY_FLEET`) instead of keeping its own `DATA_DIR/fleet.json`,
  which meant the cache watched a file nothing wrote.

### Not closed, and why

- **`voxtral-tts`'s 15,000** is still a presentation decision with nothing behind
  it on the Hub's side (no section budget, no cliff, no audited contract). It is the
  Hub session's to close; flagged in the handoff.
- **`freshness.stale`** is deliberately left unable to fire (see Shape 6.4): making
  it fire would disable every model on a stale inventory, an outage dressed as a
  fix. The honest number, `inventoryAgeMs`, is published beside it.
- **fal's image-to-image lane** is withheld rather than implemented, because the
  reference field's name and arity differ per endpoint and fal validates at the
  worker. The `ponytail:` names reading the per-endpoint OpenAPI schema as the
  upgrade path.
- **The ten voices imported before the language fix still say `"en"`.** The old
  import wrote it either way, so the original is unrecoverable and clearing them
  would be a second guess. The owner's call.

---


## Shape 4 — a control that does nothing, or one rule computed twice

### Fixed (this session)
- Fleet's `testController` / `removeController` were wired to nothing (earlier).
- `_flat_models` and the catalogue used different conditions for "can this be
  offered" (earlier: `can_publish` is now the single rule).
- The console headline, per-provider badge, expanded list and menu-bar status all
  counted "enabled" as "live". They now read the offered count.
- **4.1 — the voice permission routes and the app used two namespaces.** FIXED by
  `voice_import.catalogue_id()`, which both spellings now go through
  (`set_permission`, `share`, and `admin.py`'s 403 guard). **The backlog's own
  suggested fix — "match on the trailing `::` segment" — was wrong**, and was not
  used: stripping whatever precedes `::` makes `elevenlabs-direct::voice-a` and
  `volc::voice-a` the same voice, which is shape 5 exactly. Only **this Mac's own**
  namespace is stripped (`provider_id_for(LOCAL_ID)`), because the catalogue holds
  this Mac's clips and nothing else, so `studio-hub-local` is the only prefix that
  can be on an id it knows. Asserted both ways in section 18a.
- **4.2 — the app read voice state out of `raw`, which is never serialised.**
  FIXED. `as_dict()` now publishes `owner_id`, `permission_acknowledged` and
  `license` top-level (verified live: 87 rows, `raw` absent, 10 local rows carrying
  real bools, non-local rows `None`), and `VoicesWindow.swift` reads the fields
  rather than a key that never existed. Two consequences had been invisible:
  - "Add to my account" sent no `owner_id`, so **every ElevenLabs library import
    was refused 422** — and `VoicesWindow.swift:725` discarded the result, so the
    table simply refreshed and looked successful. The failure now names the voices
    that were refused and quotes the gateway's own sentence.
  - every local voice rendered **"Private" for ever**, and "N already allow it" was
    always 0, so the grant looked like it had not worked.
- **Shape 5.5, the app's half — local was decided by `id.hasPrefix(...)`.** FIXED:
  all four sites read the row's `provider_id` against one named
  `VoicesSection.localProviderId`. A prefix test on a *composed* id is right only
  by coincidence of spelling; which provider a voice belongs to is a field.
- One thing found while fixing 4.2: writing the refusals into the one-line
  `summaryLabel` would have been clipped (so the reason is unreadable — the same
  bug) or grown the window's required height. They go in a dialog instead.
- **4.10 — `_VALID_KINDS` was a second copy of `IMPLEMENTED_KINDS`, and it had
  already fallen behind.** FIXED, and it is now *the same object* rather than an
  equal set: `kind: "xai"` in a hand-written `providers.local.json` was dropped
  without a word, because `KIND_XAI` was missing from the copy while every adapter
  and the catalogue both had it. "May this be loaded?" and "can anything run it?"
  are one question — a kind with no adapter can only publish models that fail on
  use — so they must have one answer.
  The silent half is fixed too: every refusal is recorded with a reason, logged, and
  published as `skipped_custom_providers` on `/admin/api/providers`, which the
  console renders above the provider list. **A field the console does not render is
  the same defect as a control that does nothing** — the audit's own 4.3 is exactly
  that — so it was rendered in the same commit rather than left computed and unread.

### Open (as written — every item below is closed; see Batch 4 at the top)
1. **FIXED this session — see the list above** (voice share/permission namespaces).
2. **FIXED this session — see the list above** (`row["raw"]`, and the swallowed
   import error).
3. **FIXED this session (the gate).** `add_user_model` now refuses a modality
   nothing serves, naming it — so "Add model manually" cannot create a row that is
   enabled, counted in the header and never published. Proven by toggling the gate
   off: the unserved modality is accepted with HTTP 200 and both checks fail.
   **Still open: `admin.py:128-135` computes `unserved_modalities` and nothing
   renders it** — the console does not yet show which declared modalities a provider
   cannot serve, which is the difference between "the form refused me" and "I can see
   why before trying".
4. **FIXED this session.** The worker re-checked **provider** enablement only, so
   three states could change between submit and run and the job was dispatched
   anyway: the model switched off (the toggle was a control that did nothing here), a
   Hub refresh flipping `catalogued`/`memory_eligible` (exactly the "cannot serve"
   state `can_publish` withholds), and a modality the adapter does not serve. It now
   asks `enabled_models(modality)` — the one function the catalogue is built from —
   and fails **`MODEL_NOT_OFFERABLE`**, naming the model, provider and modality. The
   `PROVIDER_DISABLED` check stays above it because that code names the cause more
   precisely. Proven by toggling the gate off: the job reached the adapter, which is
   what the check asserts it must not.
5. **MED — Kie music publishes with no music handler.** `KIND_KIE` includes
   `music`, the Suno `V6` row is curated, and `adapters/kie.py:83-90` posts every
   modality down the same `createTask` image/video route. *Fix: drop `music` from
   the kind, the definition and the row, or implement and prove the route.*
6. **MED — `providers/models.py:383` (`summary`) omits the `is_configured` gate
   that `enabled_models` applies.** A provider switched on with its key cleared
   inflates `publishable_models` — the number the app now labels "offered".
   *Fix: skip unconfigured providers in `summary()`.*
7. **MED-LOW — `admin_ui.html:463` "Browse in Models tab" cannot set its own
   filter** (`filtersPopulated` is set before the select is populated, and on a
   Providers-first launch the options do not exist yet). *Fix: `await loadModels()`
   first.*
8. **MED-LOW — the two console toggle paths can display a state the server
   rejected** (`setModel` has no try/catch; `toggleModel` re-renders a stale list).
   *Fix: try/catch + alert + re-read.*
9. **LOW — `admin.py:836` has an unguarded `response.json()`** in the ElevenLabs
   import path, which 500s on a non-JSON 200.
10. **FIXED this session — see the Fixed list above** (`_VALID_KINDS`, 4.10,
    same finding as Shape 1.10).
11. **MED — `catalog._supports_cloning` and `voices.reference_support` are two
    implementations of one rule with different fallbacks**, so a published
    `supports_voice_cloning: true` can sit beside an empty voice picker. *Fix: have
    `_tts_row` call `voices.reference_support`.*
12. **MED — the TTS price shape is defined twice** (`constants.zero_pricing` vs the
    hand-listed fields in `voices.story_row`), and the copy is already missing
    `cost_tier` and `standard_credits`. *Fix: build it from `zero_pricing("tts")`.*

---

## Shape 5 — identity from a label, or two namespaces for one thing

### Fixed (this session)
- **5.1 — this Mac's Hub operator token was sent to a remote location.** FIXED.
  `hub_headers(token)` was `token or hub_token()` *unconditionally*, so a controller
  whose credential was absent — added on the Fleet page with no token, or its token
  since forgotten — had **this Mac's operator token** put in `X-Hub-Token` and sent
  to the owner-supplied address. That token is full operator scope on this Mac's
  Hub: it can change its settings and identity, reach its storage and shut it down.
  It is the same mistake `fleet.credential_scope` exists to prevent the owner making
  by hand, made by the code instead.
  `token` is now a required argument and the fallback is reachable **only** through
  `local=True`, which the callers derive from the provider id
  (`definition.id == provider_id_for(LOCAL_ID)`) — never from the address, because
  the address is the one thing an owner types. The three provider-facing call sites
  (`adapters/studio_hub._headers`, `discovery._studio_hub`, `discovery._hub_catalog`)
  pass their own credential; the one inherently-local path
  (`voice_import._run_annotation`) calls the named `local_hub_headers()`.
  **Proven, not asserted**: the mock records every `X-Hub-Token` it is sent, and
  with the old line restored the new checks fail with the sentinel on the wire —
  `/api/hub/model-packages` and `/api/hub/catalog`, twice each. A test that passes
  both before and after a fix is not a test.
  Verified live after restart: the local Hub still discovers **34 models** through
  the operator token, so the local half is not merely *silent* now, it is correct.
- **The app's half of 5.2/5.5 — "is this voice ours" asked as an id prefix.** FIXED:
  `VoicesWindow.swift` now reads the row's `provider_id` against one named
  `VoicesSection.localProviderId`, at all four sites. A prefix test on a composed
  id gives the right answer only while ids happen to be spelled that way.
- **5.2 — voice share/permission used the wrong namespace** (see Shape 4.1, now
  fixed, including why the trailing-`::` fix this file originally suggested was
  itself a shape-5 mistake).

### Open (as written — every item below is closed; see Batch 4 at the top)
1. **FIXED this session — see the Fixed list above** (the operator token, 5.1).
2. **FIXED this session — see the Fixed list above** (voice share/permission
   namespace).
3. **FIXED this session — see the Shape 6 Fixed list** (a voice's id is now its
   clip's bytes; the stale-segment guard for pre-digest rows went with it).
4. **MED — the reserved location namespace is claimable.** `definitions.py:562`
   only rejects ids already built in, so `providers.local.json` may define
   `studio-hub` or `studio-hub-*` — and `fleet.py:56,79` then treat that prefix as
   "location". Such a provider publishes but can never be routed. Related: "is a
   location" is defined by *kind* in `catalog.py:115` and by *id prefix* in
   `fleet.py:92`. *Fix: reject the reserved ids, and use one predicate.*
5. **MED-LOW — kind judged by id prefix.** `voices.py:823`
   (`provider_id.startswith("elevenlabs")`) is the remaining half: the app's
   `VoicesWindow.swift` sites were fixed this session (they read `provider_id`,
   not a prefix on the composed id). The API already publishes `kind` and
   `source`. *Fix: compare the kind.*
6. **MED-LOW — `models.py:284` (`set_many`) writes enable state for ids that are
   not providers**, unlike `set_model_enabled` at `:278`; the published
   `studio-hub` id can be written and is never swept (`fleet.py:481`). *Fix:
   resolve through `_definition`.*
7. **LOW — curated limits are keyed by the concrete provider id** (`limits.py:85`)
   while README documents the visible `studio-hub::<model>` form, so an entry
   written that way is silently ignored for every location. *Fix: also try the
   published id.*
8. **MED-LOW — `voices.py:742` (`_preview_model`) reads `static_models` only**, so a
   discovered ElevenLabs voice with no preview synthesises against `model_id=""`
   and 404s. *Fix: prefer the discovered TTS rows.*
9. **LOW/SUSPECTED — `fleet.py:419` (`add_controller`) dedupes on the address, not
   the Hub's stable `controller_id`/`site_id`** that `_probe` already reads. Re-adding
   the same controller at a new address mints a second location and orphans the old
   id's credential and every per-model enable decision. *Fix: match on the probed id
   when available.*
10. **MED — the merged location row keeps the first location's capability facts**
    (`catalog.py:612-625`, `voices.py:873`), while routing picks by pressure. With
    two Hubs the picker and the reference report can describe a location that will
    not run the job. *Fix: prefer a record from a location that can publish it.*
    (Same finding as Shape 1.5.)
11. **LOW — `SettingsSection.swift:330`** does `Int(portField.stringValue) ?? 8000`,
    silently rebinding the gateway to 8000 on an empty or mistyped port while
    reporting "Saved. Restarting…".


---

## Shape 1 — one value answering two different questions

The most productive shape in this project. A flag or counter means "switched on"
where the reader needs "will it work", or "exists" where the reader needs "is it
verified".

### Fixed (this session)
- **1.2 — `hub_status()` published `running: bool(base)`, i.e. file existence.**
  FIXED by `hub.hub_probe()`, which asks the Hub's own `/api/health` — the same
  route the menu bar already used — so both readers answer the same question. Three
  answers now, never two: no ready-file, a ready-file with nothing behind it, and one
  that answers; `advertised` keeps the old meaning for anything that needs it, the
  probe detail names which failure it is, and the age is published (cached 5 s, so a
  poll does not stall exactly when the Hub is down). The Overview window now says
  *"not answering"* with the reason rather than *"not running"* — sending the owner
  to start a Hub that is already starting is the failure that distinction prevents.
  Verified by restoring the old line: the check fails with `running: True` for a dead
  address, which is the bug.
- **1.3 — `start_annotation` gated on URL presence** while promising "refuses
  without a reachable Hub". FIXED: it probes the base it is about to use, so a stale
  ready-file gets the one actionable sentence instead of every voice failing in turn
  with a network error. `hub_probe` takes that base explicitly, because resolving it
  twice let the gate and the work disagree about which Hub they meant.
- **6.7 — "Studio Hub running (local models available)" came from a bare
  `/api/health` 200**, and this session measured a Hub answering health in 1.7 ms
  with no model loaded. FIXED: the item now claims only "Studio Hub running"; the
  stronger sentence needs `/api/hub/model-packages`, and the gateway's own status
  route already answers it with real numbers.
- `/health` `enabled_models` was displayed as "live" while nine of them could not
  be picked. Now three fields: `enabled_models`, `publishable_models`,
  `withheld_models`; `by_modality` counts the offered rows.
- `supports_voice_cloning` was per provider *kind* instead of per model.
- A local voice was matched to a model by an exact `tts_model_id` string stamp.
- `voices_for_model` compared a published (maker-namespaced) id against a concrete
  provider id and silently missed → `voices._model_record`.
- `_studio_hub` discovery skipped `definition.base_url`, so every remote location
  discovered *this Mac's* models.
- `tts_model_id` was published as the tier a voice was *minted against*, which
  Story Studio reads as "the model that serves it" and uses to drop the row.
  Every local voice now publishes `null` there — the honest answer, since a local
  voice serves every model that declares cloning. Verified live: 10 of 10 voices
  now survive Story Studio's boundary filter, up from 0.
- **1.1 — `routing_candidates` and the catalogue used two different tests for "can
  this run".** FIXED. It selected locations on `is_enabled` plus a row match, while
  `enabled_models` — the set the catalogue is built from — also applies
  `is_configured` and `can_publish`. Worse than an extra candidate: `route` chose a
  location on the weaker test and `catalog.resolve` then rejected that pair on the
  stronger one, so the request answered **`MODEL_NOT_FOUND`** with a perfectly good
  copy of the same model enabled on another site, never tried.
  The fix is **not** what this file first suggested ("filter candidates through
  `can_publish`") — that would have made a *third* reader of the same rule, which is
  the shape this audit exists to remove. `routing_candidates` now iterates
  `registry.models.enabled_models(modality)`, so there is one rule with two readers
  still. The new check drives a controlled registry in which both locations look
  identical to `models_for` and only one is in `enabled_models`: a `models_for`-based
  reader returns both and **fails**, which was verified by restoring the old body.

### Open (as written — every item below is closed; see Batch 4 at the top)
1. **FIXED this session — see the Fixed list above** (`routing_candidates` ×
   `can_publish`, 1.1).
2. **FIXED this session — see the Fixed list above** (`hub_status` running).
3. **FIXED this session — see the Fixed list above** (`start_annotation` gate).
4. **`gateway/fleet.py:876-883` — machine online counting.** A machine is online
   whenever its state `!= "offline"`, and a missing state is coerced to `"unknown"`,
   which is then counted online; `machines_total` comes from the activity list
   while the table below is the union of overview + activity. The two headline
   numbers can disagree and an incomplete report overstates reachability. *Fix:
   count unknown as neither, or derive both from the same list.*
5. **`gateway/catalog.py:612-625` + `voices.py:853-873` — a merged row keeps the
   first location's capability fields.** `rebuild` reconciles only `locked` and
   `legacy_ids`, and `_model_record` returns the first location holding the id, so
   one published `supports_voice_cloning` / `reference_audio` stands for N
   locations' separate Hub catalogues while routing may pick another. *Fix: union
   the capability across locations, the way `locked` already merges.*
6. **`gateway/providers/models.py:368-400` — `summary()["total_models"]` counts
   only models on enabled providers and includes non-publishable ones**, yet
   `OverviewWindow.swift:416` labels it "discovered"; `admin.py:316-338` uses a
   different definition under the same word. *Fix: one definition, or two names.*
7. **FIXED this session.** Both refuse, naming the field: "this model needs a
   voice: send 'voice_id' …". `alloy` and `English_expressive_narrator` were *our*
   choices presented as the caller's, and the failure was silent in both directions —
   the caller hears a voice it did not pick and has nothing to act on. fal's `Rachel`
   and ElevenLabs' account-first-voice stay, because those providers genuinely
   default and the catalogue offers that default *as a voice*. Caught by the suite
   immediately: an existing `minimax-speech` lane check had been relying on the
   silent default, and now names a voice explicitly.
8. **FIXED this session, both halves.** The adapter **refuses** an out-of-range
   speed instead of dropping it, and the catalogue publishes fal's real range
   (`_TTS_SPEED_RANGES`, 0.7–1.2) so what is offered is what is accepted. fal
   publishes the range per endpoint in its OpenAPI schema, which is still not
   fetched — the `ponytail:` names that as the upgrade path, the same source the body
   schemas already come from.
9. **`gateway/providers/discovery.py:124` — modality falls back to
   `definition.modalities[0]`.** `fal-ai/…-video-understanding` (category `vision`)
   and `openrouter/router/video` (category `video-to-text`) are classified as video
   *generation* rows. *Fix: withhold/flag when neither the id nor a provider hint
   states a served modality.*
10. **FIXED this session — see Shape 4.10 above.** (`_VALID_KINDS` was a second
    copy of `IMPLEMENTED_KINDS` and had already fallen behind; it is now the same
    object, and every refusal is recorded, logged and published.)

---

## Shape 2 — an unknown collapsing into a definite value

The project's rule: never let "the answer is none" and "I could not work out the
answer" produce the same value.

### Open (as written — every item below is closed; see Batch 4 at the top)
1. **`gateway/catalog.py:179-199` — video `modes` / `variants[].operation`.** An
   unknown mode still becomes the definite `["image-to-video"]`: `text_capable` only
   looks for the literal `text-to-video`/`t2v` in the model id, while `raw["category"]`
   — which discovery **does** store — is never read. Live: `xai-direct::grok-imagine-video`,
   `grok-imagine-video-1.5` and `kie-ai-images::gemini-omni-video` publish
   image-to-video only, although the xAI adapter has a documented prompt-only
   text-to-video path. Cached `fal-ai/veo3.1`, `fal-ai/ltx-video` and
   `google/gemini-omni-flash` carry `category: "text-to-video"` and would publish
   wrongly. *Fix: read `raw["category"]` before the id heuristic; do not assert a
   mode when nothing states one.*
2. **FIXED this session.** Nothing is invented now: a folder stores what its
   metadata states, a loose clip reads a sibling `<name>.json` (the way it already
   read a sibling `.txt`), and `push_fields` sends the value verbatim. **The Hub
   really does require a language** (measured from its own validator: `raise
   ValueError("language is required")`), so the readiness gate's "it has no language"
   is a mirror of a real remote refusal — and it was unreachable precisely because
   the import always wrote `"en"`. The consequence, which is honest rather than
   avoidable: **a loose clip whose language nobody states cannot be pushed to the
   fleet until one is given**, and the gateway now says so locally in one sentence
   instead of the Hub answering 400 naming only a field.
   The owner's ten existing voices still say `"en"`, and that is **not** corrected
   here: the old import wrote `"en"` whether or not the source stated one, so the
   original is not recoverable and clearing them would be a second guess. Correcting
   them is his call.
3. **`gateway/catalog.py:123-124,133-134,148` — image
   `supports_reference_images` / `supports_image_to_image` / `min`/`max_reference_images`
   are a definite `False`/`0` for everything non-`studio_hub`**, while the same row's
   `operations` still claims `image-to-image`. fal's `category: "image-to-image"` and
   KIE ids like `gpt-image-2-image-to-image` are discarded, so live rows
   self-contradict. *Fix: derive from the model's own id/raw category; null where
   unstated.*
4. **`gateway/catalog.py:143-144` — image `reference_strength_mode` is `"none"`**
   and `supported_reference_strengths` is `[]` for every model, including
   `studio_hub` rows that do use reference images. *Fix: read from the Hub raw, else null.*
5. **FIXED this session.** `_first_stated()` replaces the `or` chain, so a
   published `0` survives to `_stated` and `limits.gap` — both of which were written
   specifically to preserve one and could never see one. ("It said zero" and "it said
   nothing" are different answers; the chain made them the same answer, and then the
   curated value was published *as the provider's*.)
6. **`gateway/catalog.py:511` — chat `structured_output_mode` becomes
   `"json_object"`** when the provider merely lists `response_format`/`structured_outputs`,
   even if it only supports `json_schema`. *Fix: derive the mode from the stated
   parameter, else null.*
7. **`gateway/voices.py:819` — voice `output_format` guessed from the provider id**
   (`"mp3" if provider_id.startswith("elevenlabs") else "wav"`). Live: a fal
   ElevenLabs voice publishes `wav` while `adapters/fal.py:261` downloads
   `audio/mpeg` — and one fal model's voice list contains rows claiming **both**.
   *Fix: publish the adapter's actual media type, or null.*
8. **`gateway/api.py:766` — the TTS quote publishes `output_format: "wav"` for
   every model**, while ElevenLabs actually returns `mp3_44100_128`. *Fix: as above.*

---

## Shape 3 — a value we invented, read as the provider's

Every deliberate simplification must carry a `ponytail:` comment naming the ceiling
**and** the upgrade path.

### Misattribution (our number in a field the consumer reads as the model's own)
1. **FIXED — companion `*_source` fields are published.** `context_window_tokens`,
   `max_output_tokens`, `max_prompt_characters` and `maximum_input_characters` each
   carry one, with values `provider` / `curated` / `gateway`. The value is still
   published rather than nulled — Story Studio substitutes its own 8,192 for a
   missing window, which is worse than a labelled number — and it now travels with
   a statement of whose number it is. Measured live: 48 chat rows `provider`,
   **18 `curated`** (the file's identically-valued `1000000/128000`), 0 unsourced.
   Value and provenance come from **one function**, because a separate "where did
   that come from?" reader is free to disagree with the chooser — which is exactly
   how the TTS quote route kept publishing a flat 40,000 after the catalogue had
   stopped.
2. **FIXED — the one that was already wrong.** Orpheus published
   `maximum_input_characters: 25000` while its engine silences past **170**. The
   Hub declares this in its own catalogue and does not serve it, so the cliff is
   recorded in `catalog._HARD_CAP_CHARACTERS` (bark 150, orpheus 170), **gated on
   the served `long_form_delivery`** so it stops applying by itself if the Hub
   learns to section that family. `spark-tts-mlx` (750 in the Hub's table) is
   deliberately *not* capped: it is `auto-split`, so 750 is a soft best, not a
   limit. Verified live: the Orpheus row publishes 170.
2. **`gateway/catalog.py:188-199` — video `duration_seconds` pinned to `(5, 10)`
   and `resolution: "720p"` for every video model**, published as the model's own
   variants; the ponytail at `:158` names only the prompt cap.
3. **`gateway/voices.py:195,198,205-207` — the 21 curated fal example names publish
   as the owner's own account voices** (`voice_tier: "custom"`,
   `custom_voice_id`). fal never returned them as account entries; they are
   documentation examples. The comment says "twenty" and the tuple holds 21.
   *Fix: a distinct source, not `SOURCE_ACCOUNT`/`owned=True`.*
4. **`gateway/api.py:759` — `estimated_duration_ms` from an invented 14
   characters/second rate**, for every voice and language.

### Unmarked deliberate defaults (add a `ponytail:`)
5. **`gateway/adapters/anthropic.py:21` — `DEFAULT_MAX_TOKENS = 4096`**: the removed
   `8192` one layer down. Anthropic requires the field, so an invented number
   silently truncates real generations while the catalogue publishes
   `max_output_tokens: null` for those models.
6. **`gateway/catalog.py:135,133-134` + `_geometry()` `:88-93`** —
   `reference_image_max_dimension: 2048`, reference-image counts and pixel geometry
   published as capabilities; the adjacent ponytail describes only the prompt cap.
   They also contradict the adapter: the catalogue advertises 1344x768 for 16:9
   while `openai_compat._size_for` sends 1536x1024.
7. **`gateway/catalog.py:547-548` — `max_lyrics_characters: 4000`,
   `max_title_characters: 200`**, invented, under a ponytail naming a different key.
8. **`gateway/adapters/openai_compat.py:124-144,246`** — a DALL·E-3-only size table
   snapped for every OpenAI-compatible image model (`1792x1024` is a 400 on
   `gpt-image-1`), and `"alloy"` invented as the default voice.
9. **`gateway/adapters/fal.py:86`** — the `0.7–1.2` speed clamp (see Shape 1.8).

### Oversight
10. **`gateway/providers/discovery.py:59-94` — modality is inferred from a keyword
    table while OpenRouter's own `architecture.output_modalities` is fetched and
    never read for classification.** *Fix: prefer the stated field.*
11. **`gateway/providers/discovery.py:379-388` — a comment asserts ElevenLabs states
    per-model speed capability; its documented model object has no such field.**
    `can_use_style` is read, stored and never consumed; `supports_speed` is really
    the curated `_TTS_NO_SPEED`. *Fix: correct the comment, drop the dead read.*
12. **`gateway/catalog.py:361` — `supported_languages[:40]` silently truncates a
    provider-stated list.** No effect today (ElevenLabs publishes 32), no comment.

### Must stay invented — do not "fix"
- TTS `maximum_input_characters` fallback **25,000** (`catalog.py:264`): Story
  Studio calls `.toLocaleString()` unconditionally, so `null` crashes the page.
  **But the trigger for it being wrong has already fired** — see the next item.
  Also: `README.md:536` still says 40,000 and should be corrected.
- `finish_reason: "stop"` when the provider states none (`api.py:228,600`) — a hard
  gate; `null` reads as truncated.
- All price fields pinned to 0; `provider_submit_concurrency`/`available_workers`
  of 1; `_TTS_NO_SPEED`; retention's 7 days / 10 GB and its clamp bounds.

### The one that was already wrong — FIXED, then simplified by the owner's rule
Orpheus published 25,000 while its engine silences past 170. The owner's call was
**not to untick it**: the model works, only its advertised limit was wrong.

The first fix mirrored the Hub's own guidance per model, and briefly its audited
`execution_contract.text_max_characters`. **Both were withdrawn on 2026-09-20 in
favour of a simpler rule the owner set**: the local lane presents **one** number
(15,000) and **chunking is Studio Hub's job**, so the gateway never has to model a
per-engine limit at all. An engine the Hub does not yet section keeps its cliff as a
ceiling and joins the lane at 15,000 automatically once the Hub publishes
`long_form_delivery` for it. See
`_handoffs/2026-09-20-gateway-local-tts-limit-15000-and-chunking-ownership.md`.

Still open on the Hub's side, and now the only row where the uniform limit is a
claim rather than a consequence: **`voxtral-tts` has no section budget, no cliff and
no audited contract**, so nothing backs the 15,000 the gateway publishes for
`mlx-community/Voxtral-4B-TTS-2603-mlx-4bit`.

---

## Shape 6 — state that changes but cannot be seen

### Fixed (this session)
- `_local_voices` resolved `VOICES_PATH` once at import, so a newly imported voice
  never appeared.
- The app adopted a stale gateway through four rebuilds → build stamp on `/health`
  plus Check Gateway / Restart Gateway.
- **6.2 — `.env` was a snapshot taken at import, in two places.** FIXED, and the
  second copy deleted. `config.get()` now re-stats `ENV_PATH` (mtime + size) and
  re-reads on a change, `providers/credentials.py` reads through it instead of
  keeping its own `load_env_file()` copy, and `api.require_auth` compares against
  `gateway_api_key()` per request rather than an import-time `API_KEY` constant.
  `CredentialStore.reload_env()` / `Registry.reload_env()` are gone — there is no
  snapshot left to invalidate, so a control whose only job was to invalidate one
  is a control that does nothing.
  **The failure that matters is a rotation**: edit `GATEWAY_API_KEY` in `.env`,
  restart nothing, and the old key kept opening the door — a rotation that appears
  to have happened and has not. Proven three ways: the e2e suite writes a new key
  into the temp `.env` (it no longer passes the key through the environment, or
  `os.environ` would shadow the file and there would be nothing to test), with a
  *wrong* key refused first so a keyless gateway cannot make the check vacuous —
  and against the **live** gateway, where the old key answered 403 and the new one
  200 with no restart. Restoring the frozen snapshot fails exactly those two checks.
- **6.3 — a voice placement outlived the recording it was made for.** FIXED: the
  cache key is `(voice_id, location, clip digest)` and the digest is computed
  *before* the cache is consulted. A re-import keeps the voice's id and changes its
  audio, so the controller kept using the **old recording** for up to six hours and
  nothing said so. Entries that have expired are now dropped as new ones are
  written, because a key carrying the clip revision means every re-import adds a
  key whose predecessors are never asked for again.
- **6.5 — the same voice was listed twice.** FIXED: `voices()` merges the discovered
  and custom lists by id, the owner's own entry winning. They are one list seen
  twice, and `find()` takes the first match, so the preview and tier you saw
  depended on which list happened to hold the row.
- **5.3 — a local voice's identity was derived from a file name.** FIXED: the id is
  `voice-<sha256[:12]>` of the clip's bytes, and an import matches an existing voice
  by **digest first** — so the same recording under any name is that voice, and a
  re-import of the owner's library lands on the ten voices he already has. Two
  consequences worth naming:
  - a *different* recording claiming a name that is already used becomes a **second
    voice** rather than overwriting the first, and the import says so in `notes`
    (rendered in the Voices window). Overwriting would destroy a take the owner may
    have no other copy of; silence would leave two rows called "Morgan" with no
    explanation. The name is a label the last import owns — the identity does not
    move.
  - the stale-segment guard now compares the digest **including** the case where the
    older row has none. `previous.get("audio_sha256") and ...` skipped exactly the
    rows that could not be vouched for, which is the case that most needs the guard;
    the cost of the fix is one unnecessary re-transcription.
  Verified on a **copy** of the real library: ten reference clips re-imported under
  ten names the catalogue had never seen → ten voices before, ten after, every id
  unchanged, no notes. The id scheme also now shares its 12 hex with the id the Hub
  receiver demands (`shared_voice_id`), so a voice's catalogue id and its id on the
  wire are the same bytes.

### Open (as written — every item below is closed; see Batch 4 at the top)
1. **`menubar/main.swift:172-186` — saving the API key rewrote `.env` wholesale**,
   deleting every provider API key the owner had put there (the documented no-UI
   way to add one) and silently resetting a deliberate `GATEWAY_HOST` LAN bind to
   loopback. **FIXED this session**: read-modify-write, only the keys the app owns,
   an owner-set `GATEWAY_HOST` preserved.
2. **FIXED this session — see the Fixed list above** (`.env` was a frozen snapshot).
3. **FIXED this session — see the Fixed list above** (voice placement outlived the
   recording it was made for).
4. **FIXED as decided — the honest age, not a gate that fires.** `freshness` now
   publishes `inventoryAgeMs` / `inventoryOldestProvider` / `inventoryOldestAt`
   alongside the unchanged `stale` / `ageMs`, recorded during the rebuild from the
   oldest contributing provider snapshot (`null` when no provider ever reported a
   fetch time: unknown, not zero). **`stale` was left exactly as it was**, on
   purpose: making the gate fire would disable every model on a stale inventory —
   an outage dressed as a fix — and the diagnosis is now available without it.
   Measured live: `ageMs` **4.8 s** against an inventory **19 hours** old
   (`openrouter-main`, read the previous morning). That gap is the fact the old
   envelope hid, and the e2e suite asserts both halves — that `ageMs` cannot reach
   the gate, and that the inventory age is published next to it.
5. **FIXED this session — see the Fixed list above** (the same voice listed twice).
6. **FIXED this session.** The Fleet page's Refresh now re-reads reachability **and**
   each location's models, and each location row publishes `provider_id` — the
   provider its models are managed under, derived from the controller id — so no
   client has to build it. Both branches of the local row publish it, deliberately:
   a page that can only name the provider of a location that is *up* cannot offer to
   refresh the one that is down. Deliberately not `/refresh-all`, which would also
   re-read every cloud provider and outlast the client's 15-second timeout.
7. **FIXED this session — see Shape 1's Fixed list above** (the menu bar's claim).
8. **`gateway/api.py:57,69,855-857` — quoted uploads are TTL'd but pruned lazily.**
   `_prune_uploads` runs only when a *new* quote arrives and `confirm_transcription`
   never pops, so expired quotes and their audio bytes stay in memory. *Fix: prune
   from the retention timer, or on confirm.*
9. **`gateway/limits.py:69-79` — cache key is mtime only**, where `registry.py`
   documents this exact hazard and guards it with a revision counter. *Fix: add
   `st_size`.*
10. **`gateway/providers/registry.py:25` vs `fleet.py:50` — the fleet cache watches
    `DATA_DIR/fleet.json`** while `fleet.FLEET_PATH` honours `GATEWAY_FLEET`. *Fix:
    import `fleet.FLEET_PATH`.*

---

## The shapes, as a checklist for future work

1. **One value, two questions.** Does this flag mean "switched on", "exists" or
   "declared" where the reader needs "will it work", "is it verified" or
   "is it supported"? If so, split it and publish both.
2. **Absent ≠ no.** Does an unknown become `false`, `0`, `[]`, `"en"` or a default?
   It must become `null`, and the consumer must be able to tell.
3. **Our number, their field.** Is a value we invented sitting in a field the
   consumer reads as the provider's own? If it must be invented, mark it with
   `ponytail:` and say so in the payload.
4. **A control that does nothing.** Can the user switch this on and never see an
   effect? Does any two layers compute the same rule?
5. **Identity from a label.** Is any id, key or path derived from a display name?
6. **State that cannot be seen.** Is this value resolved once, cached without an
   invalidator, or changed only by a step nobody is told about?

## What to do next, in order

**Nothing from these six audits is still open.** Batch 4 closed the last of them —
see the section at the top of this file for what each fix was and how it was
measured. What follows is the record of how the work was sequenced, kept because
the ordering reasoning is still the right way to approach a new finding.

Every HIGH, MED-HIGH **and the three open decisions** were closed first. Batch 4
then closed the MED-and-below remainder:

1. **Shape 1.2 and 6.6** — closed earlier (the `/health` "running" measurement and
   the Fleet page's Refresh).
2. **Shape 2.1, 2.2** — closed (video modes read from the provider's category and
   labelled; voice language stated or absent).
3. **Shape 4.4** — closed (the worker re-asks `enabled_models` and fails
   `MODEL_NOT_OFFERABLE`).
4. **The rest of Shape 3** — closed; the `*_source` convention is in use for every
   one of them.
5. **Shape 5.4** — closed (the reserved namespace is refused with a reason, and
   "is a location" is one predicate, `fleet.is_location_provider`).

Done in this batch: 3.1 (the `*_source` fields), Shape 3's Orpheus cap, 6.4 (the
honest inventory age — decision taken: report it, do not make `stale` trip).
Done in Batch 3: 6.2 (`.env` read live), 6.3 (placement cache keyed on the clip),
6.5 (a voice listed twice), 5.3 (bytes are a voice's identity).
Done in Batch 2: 5.1 (the operator token), 1.1 (routing × `can_publish`), 4.10 /
1.10 (`_VALID_KINDS`).
Done in Batch 1: 4.1, 4.2, 5.2, and Shape 5.5's app half.
Done in Batch 4: everything else — see the top of this file.
