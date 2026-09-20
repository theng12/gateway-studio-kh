# Audit backlog — recurring bug shapes, and every instance found

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

### Open
1. **FIXED this session — see the list above** (voice share/permission namespaces).
2. **FIXED this session — see the list above** (`row["raw"]`, and the swallowed
   import error).
3. **MED — `providers/models.py:308-311` (`add_user_model`) accepts any *declared*
   modality; `can_publish` also requires `serves()`.** The "Add model manually"
   form therefore accepts a modality the adapter cannot serve (Google declares six,
   serves four), the row is enabled and counted, and it never publishes.
   `admin.py:128-135` even computes `unserved_modalities` "so the console can say
   which ones" and nothing renders it. *Fix: reject in `add_user_model` when
   `not serves(...)`, and render the field.*
4. **MED — `worker.py:62-68` re-checks only the provider at execution time.** A
   model disabled after submit still runs; a Hub refresh flipping
   `catalogued`/`memory_eligible` mid-queue does not stop the job; and
   `adapter_for` never consults `serves()`. *Fix: re-resolve the model and fail
   `MODEL_DISABLED` / `MODEL_NOT_OFFERABLE`.*
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
10. **MED-LOW — `definitions.py:604-615` `_VALID_KINDS` is a second copy of
    `IMPLEMENTED_KINDS` that omits `KIND_XAI`**, and a custom provider with an
    unknown kind is skipped silently. *Fix: `_VALID_KINDS = frozenset(IMPLEMENTED_KINDS)`
    and report skipped entries.* (Same finding as Shape 1.10 — one fix closes both.)
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
- **The app's half of 5.2/5.5 — "is this voice ours" asked as an id prefix.** FIXED:
  `VoicesWindow.swift` now reads the row's `provider_id` against one named
  `VoicesSection.localProviderId`, at all four sites. A prefix test on a composed
  id gives the right answer only while ids happen to be spelled that way.
- **5.2 — voice share/permission used the wrong namespace** (see Shape 4.1, now
  fixed, including why the trailing-`::` fix this file originally suggested was
  itself a shape-5 mistake).

### Open
1. **HIGH — this Mac's Hub operator token is sent to a remote location.**
   `hub.py:139` is `token or hub_token()`, and a controller may legitimately have an
   empty `hub_token` (added with no token, then refreshed). `hub_headers("")` then
   falls back to **this Mac's** operator token and sends it in `X-Hub-Token` to the
   owner-supplied remote address. Called from `adapters/studio_hub.py:60` and
   `providers/discovery.py:481,430`. Same fallback shape as the already-fixed
   `_studio_hub` base-url bug; the header path kept it. *Fix: never fall back for a
   non-local provider — resolve the definition and pass the credential verbatim,
   even empty.*
2. **FIXED this session — see the Fixed list above** (voice share/permission
   namespace).
3. **MED-HIGH — a local voice's identity is derived from the source file name.**
   `voice_import.py:574,587` mints `voice-<slug(entry.name)>`, which is the catalogue
   key *and* the on-disk path. So the same clip re-imported under a different name
   becomes a second voice, two clips whose names slug alike silently replace each
   other, and the stale-segment guard at `:659` is skipped for the legacy entries
   that have no `audio_sha256`. The bytes are the real identity. *Fix: dedupe and
   update on `audio_sha256`, reusing the existing id.*
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

### Open
1. **`gateway/fleet.py:1106` — `routing_candidates` never calls `can_publish`.** It
   selects a location from `models_for(..., include_disabled=False)`, i.e. "the
   toggle is on", where the job needs "the model will run". With two locations, a
   model withheld at A but publishable at B can route to A, then `catalog.resolve`
   re-filters through `can_publish`, finds nothing, and answers **404
   MODEL_NOT_FOUND** — a model another location could have served. Worse when the
   request names a voice, which pins the location. *Fix: filter candidates through
   `registry.models.can_publish(definition, model)`, the same predicate the
   catalogue uses.* **Latent today (one location); real the moment a second is added.**
2. **`gateway/hub.py:148` and `:143-152` — `"running": bool(base)`.** A URL in
   `hub-ready.json` is reported as "Studio Hub is running" on the Overview page and
   in the console, while the menu bar probes `/api/health` and fleet `_probe`
   reports the local location unreachable. Two readers of one fact disagree, and
   the one on the page that exists to say what is available is file-existence.
   *Fix: probe `/api/health` before claiming running.*
3. **`gateway/voice_import.py:905` — `start_annotation` gates on URL presence**,
   while its docstring promises "Refuses without a reachable Hub". With a stale
   ready-file the pass starts and every voice fails one at a time with a network
   error instead of the one actionable sentence. *Fix: gate on a reachability probe.*
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
7. **`gateway/adapters/minimax.py:193-197`, `openai_compat.py:246` — a missing TTS
   voice becomes a specific English speaker** (`English_expressive_narrator`,
   `alloy`). The narration runs in a voice the caller never chose, silently. *Fix:
   refuse, naming the missing voice.* (fal's `"Rachel"` and ElevenLabs'
   account-first-voice are documented deliberate.)
8. **`gateway/adapters/fal.py:86` vs `catalog.py:323-324`** — fal publishes
   `minimum_speed 0.5 / maximum_speed 2.0` while the adapter silently drops any
   speed outside `0.7–1.2`. A caller picks a speed Story Studio was told is valid
   and gets normal speed, with no signal. *Fix: publish fal's stated range (its
   per-endpoint OpenAPI schema is never fetched), or refuse out-of-range.*
9. **`gateway/providers/discovery.py:124` — modality falls back to
   `definition.modalities[0]`.** `fal-ai/…-video-understanding` (category `vision`)
   and `openrouter/router/video` (category `video-to-text`) are classified as video
   *generation* rows. *Fix: withhold/flag when neither the id nor a provider hint
   states a served modality.*
10. **`gateway/providers/definitions.py:565,604-615` — `_VALID_KINDS` omits
    `KIND_XAI`** although `IMPLEMENTED_KINDS` has it, and `_load_custom_providers`
    silently `continue`s on an unknown kind: a `providers.local.json` entry with
    `kind: "xai"` vanishes with no provider, no error and no diagnostic. *Fix:
    derive `_VALID_KINDS` from `IMPLEMENTED_KINDS`, and report skipped entries.*

---

## Shape 2 — an unknown collapsing into a definite value

The project's rule: never let "the answer is none" and "I could not work out the
answer" produce the same value.

### Open
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
2. **`gateway/voice_import.py:577,590,399` → `voices.py:802` — voice `language`
   becomes `"en"`.** Folder imports do `metadata.get("language") or "en"`, loose
   files are hardcoded, and `push_fields` does `or "en"`. It is stored and
   republished, so `story_row`'s `"multilingual"` sentinel never applies. Live: all
   ten `studio-hub-local::voice-*` rows publish `language: "en"` for clips whose
   language nobody stated, and the Voices window prints "EN". It also makes the
   "it has no language" readiness gate unreachable. *Fix: store `""`.*
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
5. **`gateway/catalog.py:428-440` — a provider-published `0` is erased by the `or`
   chain** before `_stated` and `limits.gap` (both written specifically to preserve
   a stated zero) ever see it, so the curated number is published as the provider's.
   *Fix: a first-non-`None` lookup instead of `or`.*
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
1. **`gateway/catalog.py:501,504` — the curated `model-limits.local.json` is
   published in `context_window_tokens` / `max_output_tokens`.** The live file's 24
   rows all use the identical `1000000/128000` pair, which is provably a flat
   default rather than per-model knowledge, and Story Studio renders it as the
   model's own window. The fix pattern already exists at `api.py:392`
   (`limit_source: "gateway"`). *Fix: emit a companion `*_source` field, or keep
   curated values out of the model-owned field.* **This is the highest-value item
   in this section** — it is the same class as the `8192` bug already removed.
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

### The one that is now actually wrong
**`gateway/catalog.py:264-284,331` — TTS `maximum_input_characters` is 25,000 for
every lane that states none, and Orpheus is now published.** The Hub's
`mlx-community/orpheus-3b-0.1-ft-4bit` is live (inherited from the migration) with
`maximum_input_characters: 25000`, while AGENTS.md itself records Orpheus as
hard-capping at **170** characters. Story Studio validates narration against
25,000 and the engine truncates or fails far earlier. *Fix: per-model from the
Hub's `TextGuidance` (the Hub does not serve it today) or curate it — keeping the
field a positive integer. **Needs the owner's call on whether to untick Orpheus
instead.***

---

## Shape 6 — state that changes but cannot be seen

### Fixed (this session)
- `_local_voices` resolved `VOICES_PATH` once at import, so a newly imported voice
  never appeared.
- The app adopted a stale gateway through four rebuilds → build stamp on `/health`
  plus Check Gateway / Restart Gateway.

### Open
1. **`menubar/main.swift:172-186` — saving the API key rewrote `.env` wholesale**,
   deleting every provider API key the owner had put there (the documented no-UI
   way to add one) and silently resetting a deliberate `GATEWAY_HOST` LAN bind to
   loopback. **FIXED this session**: read-modify-write, only the keys the app owns,
   an owner-set `GATEWAY_HOST` preserved.
2. **`gateway/config.py:203-210` + `api.py:52` — `.env` is a module-level snapshot,
   and the only invalidator fixes half the file.** `get()` never re-reads, so
   `gateway_api_key()`, `bind_host()`, `bind_port()` and hub's `STUDIOHUB_*`
   overrides are frozen for the process's life; `providers/credentials.py` holds a
   **second** copy with a real `reload_env()`. Editing `.env` by hand silently does
   nothing for the gateway key — and after rotating it, the old key still works.
   *Fix: re-stat `ENV_PATH` in `get()` and reload on mtime change.*
3. **`gateway/adapters/studio_hub.py:128-129,193-212` — voice placement cached 6 h,
   keyed `(voice_id, base)` with no invalidator.** A re-import keeps the id and
   changes the audio, so the controller's **old recording** is used for six hours.
   *Fix: add the clip digest to the cache key.*
4. **`gateway/catalog.py:731-738` — the 30-minute `freshness.stale` gate is
   unreachable.** `freshness()` calls `rebuild()` first, which resets `_built_at`,
   so the reported `ageMs` is always under the 20 s rebuild TTL. `rebuild()` runs no
   discovery, so Story Studio is told `stale: false, ageMs ≈ 0` about a model list
   assembled from `models_cache.json`, whose `fetched_at` can be days old. The hard
   gate recorded in AGENTS.md is dead code. **Needs a decision:** report a real
   inventory age (and let the gate fire — which would disable every model on a
   stale inventory, possibly a big visible outage) or keep `stale` as-is and add a
   separate, honest `inventory_age`. *Do not change this one silently.*
5. **`gateway/voices.py:574-596` — the same voice is listed twice.** `voices()`
   concatenates the persisted `_from_cached` list and the never-expiring `_custom`
   list with no de-duplication, and `find()` takes the first, so preview and tier
   can differ per row. *Fix: merge by id, custom winning.*
6. **`gateway/providers/models.py:89-119` + `menubar/FleetSection.swift:269-290` —
   discovery is manual and the Fleet page's "Refresh" does not discover.** It hits
   `/admin/api/fleet`, i.e. the reachability probe, so a location whose models were
   never read still shows none afterwards. The web console has "Refresh all models";
   the native button says nothing. *Fix: point it at refresh-all, or label it
   "Refresh reachability".*
7. **`menubar/main.swift:1232-1237` — "local models available" from a bare
   `/api/health` 200.** This session measured a Hub answering health while loading
   no model. *Fix: say only "Studio Hub running", or check `/api/hub/model-packages`.*
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

1. Shape 2.1, 2.2 (video modes, voice language) — both are published wrong facts
   with a stated source available to read instead.
2. Shape 3.1 (curated limits in the model's own field) — the `8192` bug's sibling,
   and the same `limit_source` fix already exists in the codebase.
3. Shape 1.1 (`routing_candidates` × `can_publish`) — latent, but it becomes a 404
   the day a second location is added.
4. Shape 6.2 and 6.3 — a hand-edited `.env` that does nothing, and a re-imported
   voice that keeps playing the old take.
5. Shape 3 "the one that is now actually wrong" — Orpheus's 170-character cap:
   needs the owner's decision.
