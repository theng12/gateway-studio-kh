# gateway-kh

**Efficient delegation and parallel work (owner 2026-11-10):** Invoke the global `efficient-orchestration` skill for multi-step coordination after required ponytail; follow the authoritative [Efficient delegation and parallel work section](/build/Developer/_orchestra/developer-handoff/LEAD-RULES.md#efficient-delegation-and-parallel-work). Leads own complete bounded implementation/check/review/fix outcomes and brief workers with required skills, graph queries, exact working folders, source and owned files. Run independent source/light work in parallel; serialize only conflicting writers, the existing KH Apps heavy-job schedule and conflicting target actions. This supersedes blanket one-app source/light parks; existing permission, release, device, security, paid/public and Production holds remain.

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
- Browser `/admin` (phone over Tailscale): a remote peer without a session gets the login page;
  password (owner-set in the app's Settings, scrypt hash in `APP_SUPPORT/admin-password.json`) or the
  gateway key mints an HttpOnly SameSite=Strict cookie (not Secure: plain http). `admin_session.py`
  holds sessions/backoff/CSRF; the password routes accept only this Mac with the key
  (`require_local_app`). Never set a password in the owner's real state; tests use a temp profile.
  The app injects `__GATEWAY_IN_APP__`, which hides the browser page menu inside its window.
- The real-Hub fleet suite requires explicit `GATEWAY_ALLOW_LIVE_GENERATION=1`;
  do not include it in offline smoke testing. Keep GitHub Actions disabled: the owner runs
  development locally and will not use GitHub-hosted compute (Hub `AGENTS.md` line 10).


One API in front of every local Studio Hub location and 18 cloud providers, so
**Story Studio KH points at a single URL** (`http://127.0.0.1:8787`) and its model
pickers fill. Story Studio's architecture contract requires it to call exactly one
endpoint and treat it as GenStudio — this is that endpoint. It needs **zero code
changes**, only `genstudio_url` and an API key.

Read `README.md` for the design; this file is the part that is expensive to
rediscover.

## Gateway UI is built once, as a web page

**The browser console is the Gateway's UI; the Mac app shows it.** The owner, 2026-10-30:
*"make sure anything built or updated in the future instantly appears [in both], like a mirror,
instead of something we keep building each time."*

- A page is built once, in `gateway/admin_ui.html` (and the routes around it), and appears in a
  browser and in the app's window with no native work. The app loads it through
  `ConsoleSection`, which injects its own key at document start and sets `__GATEWAY_IN_APP__` so
  the page hides its sidebar — **never a key in a URL**.
- **New or changed UI goes into the web page, never into a new native Swift view.** When a
  section's web page is verified, the app's sidebar row is pointed at it and the native page is
  **switched off but kept in the repository, unused, so it can be restored** — never deleted.
- The only exceptions are the things a web page cannot do, and they stay native: the **menu-bar
  icon** and its menu, the app menu and keyboard shortcuts, **notifications and alerts**,
  **launching / quitting / restarting the gateway**, the file panels (`NSOpenPanel`) and macOS
  permissions. This list is the whole list; "a web page cannot do it" is not a reason to add a
  native view without adding it here.
- **Retired, kept for restoration:** the native Fleet page (`FleetSection.swift` and the
  `Fleet*.swift` views it builds). Fleet is `gateway/admin_ui.html` now.

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

- A field a provider does not state is published **`null`**, never a plausible default: "the answer is none" and "I could not work it out" must never be the same value.
- When a provider states a figure, read it (spellings differ: `context_length`/`inputTokenLimit`, `top_provider.max_completion_tokens`/`outputTokenLimit`, `maximum_text_length_per_request`). Read with `_first_stated()` (first non-`None`), **never an `or` chain**: a stated `0` is a claim.
- A voice's language is stated (folder `metadata.json`, or a sibling `<name>.json`) or it is `""`, and `story_row` publishes `"multilingual"`. Never assume `"en"`. The Hub requires a language, so a language-less voice is refused a fleet push: that is the gate working.
- Changing an int to `null` breaks the reader (Story's `normalizeMaxOutputTokens` substitutes 8,192). List every field whose *guarantee* changed.
- The one invented value is TTS `maximum_input_characters` (Story calls `.toLocaleString()` on it): the provider's figure, else 15,000 on a Hub location and 25,000 on a cloud lane, with `maximum_input_characters_source`.
- The local lane publishes one number, 15,000 (`catalog._TTS_CHARACTERS_LOCAL`); chunking is the Hub's job, and the Hub's `text_max_characters` is not mirrored. The only exception is `catalog._HARD_CAP_CHARACTERS` (bark 150, orpheus 170), applied only while that model's `long_form_delivery` is absent. `spark-tts-mlx` is deliberately absent. Section budgets are per family, never a uniform ≤200.
- Every limit carries a `*_source` (`provider` / `curated` / `gateway`), and the value and its source come from **one function**.

History and measurements: [docs/publish-what-is-known.md](docs/publish-what-is-known.md).

## A video model describes itself, and the record is also the request mapping

- One `VideoCapability` record per model in `gateway/video_capabilities.py`, and nothing else types a video option; `adapters/fal.py` names no model. Provenance is `provider` (the endpoint's own schema) or `curated` with `source_date` = the date it was read. A record older than the provider's schema is re-read, never edited from memory.
- A model with no record publishes its mode and no variants: visible and enableable, never selectable.
- A value the record does not publish is **refused**, never clamped or dropped (fal validates at the worker, after reporting `COMPLETED`).
- The scene image must arrive: `POST /v1/videos/source-images` → `api._stage_video_reference` → each adapter sends the bytes in its provider's own field. A staged file that has gone is a named refusal.
- Adding a model: read the endpoint schema (fal: `curl 'https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>'`), append a record, run `tests/test_video_capabilities.py`. A new provider also needs an adapter and its pinning test. Enabling is the owner's switch.

History and measurements: [docs/video-capabilities.md](docs/video-capabilities.md).

## Discovery ≠ availability

- Nothing reaches Story until it is enabled; the local Studio Hub (`auto_enable_discovered`) is the sole exception. A kind with no adapter stays configurable but its models are withheld. `IMPLEMENTED_KINDS` (`providers/definitions.py`) is the single source of truth.
- An adapter for a kind ≠ every modality works. Transcription is `openai_compat` and `studio_hub` only, and always asks for `srt`: enable OpenAI's `whisper-1`, not Google's. Groq's Whisper is withheld. Non-SRT models are in `TRANSCRIPTION_SRT_UNSUPPORTED`. Groq chat needs `GROQ_API_KEY`.
- fal: read its per-endpoint schema (field types differ per model; its queue validates at the worker). fal TTS is its ElevenLabs routes, with voices curated from the schema's `examples` (`voices._fal_tts_voices`). Discovery keeps only `text-to-speech` / `text-to-audio`.
- xAI is `KIND_XAI` with its own adapter: images take `aspect_ratio`/`resolution`, never `size`. Video is deferred: branch on the JSON `status` (202 = working; a failure arrives as HTTP 200). The first frame is `image: {url}`.
- Text-to-video lanes stay listed (owner, 2026-10-26).
- Lyria is Gemini `:generateContent`: scan **every** part for `inlineData`. `lyria-realtime-exp` is withheld, Lyria is paid-tier only, a 200 with no audio is `retryable`, and a refusal is in `promptFeedback.blockReason`. OpenRouter's Lyria rows are withheld (no music path).
- `ModelInventory.can_publish` is the single rule for both the catalogue and the console list.
- fal's ElevenLabs routes accept ElevenLabs voice ids (`accepted_voice_providers`), keyed on the provider *kind* plus `elevenlabs` in the endpoint, never on a provider id.
- A voice can be saved by id (`POST /admin/api/voices/import`, `provider_voice_id`); ElevenLabs also needs the owner id.
- An upload past 25 MB is refused **413** (never 402).
- A voice is never invented: a missing `voice_id` refuses (fal's `Rachel` and ElevenLabs' account default stay). Speed outside `catalog._TTS_SPEED_RANGES` refuses: what is offered is what is sent.
- The worker re-checks `enabled_models(modality)` at run time (`MODEL_NOT_OFFERABLE`), and `add_user_model` refuses a modality nothing serves.
- OpenRouter `:batch` variants stay off (owner, 2026-09-19); use the sibling without the suffix.
- "Hub is running" is `hub.hub_probe()` (`/api/health`, 5 s cache), with three states. It gates `start_annotation`. Each location publishes its `provider_id`.
- `fleet._is_probe_machine` drops `probe-fleet`/`probe-hub` by **id**, never label (`probe_machines_hidden`); online/offline counts are per machine.

History and measurements: [docs/discovery-and-availability.md](docs/discovery-and-availability.md).

## Browser-extension workers (`chrome_extension`) poll; they are not servers

- Wire v1 (`/api/workers/*`) is Relay's handoff §4, kept exactly; `gateway/extension_workers.py` serves it and `adapters/chrome_extension.py` hands jobs to it. **Off by default** (`GATEWAY_EXTENSION_WORKERS`, live): off, every route there answers 404. The owner approved (via Developer, 2026-11-09) turning it on for **this Mac's** Gateway, LAN/Tailscale access, scoped token pairing and direct Relay coordination; never 1301, and no paid Flow generation without a go that names it.
- One record per thing: the job is the `JobStore` row (`upstream_job_id = ext:<worker>:<job>`, checkpointed at claim), tokens are sha256 digests in `APP_SUPPORT/extension-workers.json` through `admin_session`'s 0600 writer, and the hand-off is in memory. Never a second job store.
- Paid credits: unclaimed = withdrawn, unsent; claimed then `ambiguous` (anything but `false`), a silent lease or a restart = `PROVIDER_SUBMISSION_UNCERTAIN` hold, never re-offered. A late result adopts into the held row via `worker.settle_result`.
- A token opens only `/api/workers/*` for its own worker id and kind; the gateway key is not a worker token. Minting/revoking is `require_local_app`. Credits not reported are `null`.
- Provider `story-studio-video` ships **disabled** (`default_enabled=False`). Its models and their `VideoCapability` records both come from `gateway/extension_video_models.json`, copied by script from Relay's catalogue (never hand-edit; re-copy and update `source_sha256`). The adapter refuses anything a record does not publish before offering, including an image key with no reference picture: the extension would make one itself and spend credits. The zero-price catalogue shape stays (contract trap), and `chargeable` stays truthful: true, because Flow spends credits.
- Audio discovery uses optional authenticated `audioInventory` on claim (schema 1); health advertises live age/body limits. Snapshots are token-issuance bound, replaced on each audio claim and invalidated on remint/revoke/restart. Actual pending assignment rechecks exact membership under the broker lock; expiry never abandons claimed/held work. Discovery does **not** establish compatible pairs or required limits, so browser TTS remains withheld and generation refuses before offer. See `docs/extension-audio-inventory.md`. Image now joins video in the adapter modality table; all provider/worker switches remain off by default.
- A late `fail` with `ambiguous: false` settles the held row through `JobStore.fail`; an ambiguous one stays held. Late answers reach only the row whose **current** `upstream_job_id` matches exactly. `broker.hold` returns whether claimed→held happened; the adapter raises only when it did. An ambiguous fail is not a first answer: until the adapter takes it (`broker.take_failure`, one locked step that makes it `held`), a following result or definite fail replaces it, so a finished result is never dropped as `taken`; `taken` (answered `ignored`) is only ever behind a definite answer. Telemetry/`creditsAfter` credits are kept only as a finite non-negative number with a `source` slug and a timezone-qualified ISO `observedAt`; anything else stays unknown.

## Sound sources are managed like providers, and are not providers

- A sound source has no entry in `providers/definitions.py`. It is declared once in `sound_sources.SOUND_SOURCE_DEFINITIONS` with one class per kind in `_ADAPTERS`, and the two are checked against each other at import.
- The SFX tab is a pane of the Providers page (`#sfx`); no native work.
- Credentials go in `CredentialStore` under the source's `credential_id` (`openverse-sound`; never rename it). `set_values_for` refuses an unknown key.
- A secret is never echoed, logged or put in error text (`mask()`; `tests/test_sfx_providers.py`).
- `configured` and `registered` are separate facts; quota reads "not read yet", never `0`.
- Provider cards are grouped by `definitions.service_group()` (first lane the adapter actually serves). It is display only, and the filter matches lanes too.

History and measurements: [docs/sound-sources.md](docs/sound-sources.md).

## Received artifacts are temporary

- The gateway is a receiver; Story keeps the copy that matters. `gateway/retention.py` prunes at **3 days or 10 GB**, whichever comes first. A pin (`/admin/api/artifacts/keep`) outranks both. API: `/admin/api/settings/retention`, `/admin/api/artifacts/prune`.
- Nothing younger than an hour is removed; active and recovery-held inputs are protected.
- Pruning **moves**: first to `DATA_DIR/_trash/` (old `DATA_DIR/trash/` still recognised), then entries over 7 days old, or the oldest above 20 GiB, go to macOS Trash. A failed move keeps the source and is reported. Never a permanent delete. `max_bytes` counts the live store only; `usage()` reports retired files separately.
- Settings live in `DATA_DIR/retention.json`, not `.env`. This is the owner's explicit exception to the project `_trash/` rules.

History and measurements: [docs/received-artifacts.md](docs/received-artifacts.md).

## `.env` is read live, and the key is checked per request

- `config.get()` re-stats `ENV_PATH` and the credential store reads through it; `require_auth` reads `gateway_api_key()` **per request**, so a key rotation works with no restart.
- In `tests/test_e2e.py` the key lives in the temp `.env`, not the spawned environment, and a wrong key is asserted refused first.
- `/admin/api/*` is deliberately open from loopback; measure auth on `/v1/...`.

History and measurements: [docs/env-and-auth.md](docs/env-and-auth.md).

## Remote final rendering is the owner's per-Mac switch, and two keys must both be turned

- A Mac renders for Story only when **the owner switched it on** (`render_optin.json`, absent = off; Fleet > a Mac's "…" > Remote Final Rendering…) **and** its Hub's `video.final_render` row carries `accepted_unqualified` with `provenance: "owner-opt-in-unqualified"`. Production-shaped is not qualified; any other claim is excluded (`render_optin.unqualified_claim`).
- The switch travels via `startup_control.set_node_render_authority` (no SSH). **On** is kept only if the Hub took it (`202 queued` counts); **off** is written here first. The Gateway sends no encode.
- `fleet.render_candidates` ranks qualified first, then accepted-unqualified, same network first (`render_optin.classify_machine`; Tailscale is never "ours", unknown is never guessed). `RenderService.submit` re-checks the flag against the evidence.
- Nothing is added to `/v1`: the unqualified label lives in the stored snapshot and `/admin/api/fleet/remote-rendering`. `_trim_worker` copies `accepted_unqualified` only when the Hub sent it.

History and measurements: [docs/remote-final-rendering.md](docs/remote-final-rendering.md).

## Ports

- **8787** is this gateway's port — the default in `gateway/config.py` and the
  app's `AppConfig`, and where this Mac's working instance is bound. Every Story
  Studio client points its Base URL at `http://<host>:8787`. The tests read the
  configured port, never a hardcoded one.
- **It was 8000** because that is Story Studio's *default* `genstudio_url` and
  needed no configuration. The owner moved it to 8787 on 2026-09-21 (8000 is the
  most contested dev port and is also Story's own default, which kept "is this
  pointed at the right thing?" ambiguous). A fresh install now defaults to 8787.
- **The older GenStudio KH API used 8000 too.** It is **stopped and disabled**
  (`launchctl bootout` plus `launchctl disable
  gui/$(id -u)/com.genstudiokh.local-api`), and its LaunchAgent plist has since
  been **removed** — there is nothing to restore, and it must not be re-added.
  The gateway on 8787 replaced it.
- **Curated limits**: `DATA_DIR/model-limits.local.json`
  (`~/Library/Application Support/gateway-kh/data/`) fills gaps for lanes with
  none (24 chat rows across six providers); the repo `data/model-limits.local.json`
  is a legacy fallback (`gateway/limits.py`). It **fills gaps only** — a
  provider's published value always wins.
## Locations are providers — but not separate products

- A Studio Hub location is a provider through the `studio_hub` adapter, standalone or controller; Story never learns a second address.
- `fleet.publish_id` collapses locations into one `studio-hub` namespace (namespace by maker; a cloud vendor keeps its id). `catalog_encode` applies it, so the id Story sent is the id it is told back.
- The gateway chooses the site, never the machine: `fleet.route` ranks by pressure, then a weighted lottery. The weight is per model (`model_supply`, then `capacity.by_operation`, then the site total), `available` breaks ties, and speed is not a routing input. Capacity goes in the weight, never as a rank term. Admission is fit-aware (`Worker._room`); fleet lanes follow published capacity unless the owner types a ceiling, and are not counted against `global`.
- `can_publish` withholds a model whose `dispatch_state` is present and not `eligible`. A submit `409 MODEL_NOT_READY/PACKAGED` becomes `LOCATION_NOT_READY` and re-routes. A voice (`studio-hub-<controller>::<raw>`) pins the route and is never re-routed.
- Secrets are in the credential store and the roster in `fleet.json`. Iterate `registry.definitions()`, never `PROVIDERS_BY_ID`. An address is `values.get("base_url") or definition.base_url`.
- Adding a location discovers its models (they arrive disabled) and says what it found. A credential is checked (`/api/hub/settings`: 200 operator, 403 fleet, 401 wrong). `PUT /api/fleet/controllers/{id}/credential` replaces only the token.
- **An absent credential stays absent:** `hub_headers(token)` needs the token; the local fallback is only `local=True`, derived from the provider id, and local-only paths use `local_hub_headers()`. `tests/mock_upstream.py` asserts on the wire.
- A custom provider's kind is checked against `IMPLEMENTED_KINDS`; a skipped one is published as `skipped_custom_providers` and rendered.
- A location's name is a label (`fleet.rename_controller`); ids derive from the controller id; preserve `local_label`.
- Test row actions by pressing them (`table_button_titles`), not only through the API.

History and measurements: [docs/locations-are-providers.md](docs/locations-are-providers.md).

## Voices are stocked, not configured

Moved to [docs/voices-and-reference-clips.md](docs/voices-and-reference-clips.md).

## Batch 4 — the remaining audit items (2026-09-20)

Moved to [docs/audit-batch-4-2026-09-20.md](docs/audit-batch-4-2026-09-20.md).

## Verification

```bash
.venv/bin/python scripts/run_gate.py --evidence <fresh dir>   # the release gate: every tests/test_*.py
.venv/bin/python scripts/test_run_gate.py                     # the gate runner's own self-test
```

**`scripts/run_gate.py` is the release gate, and it is tracked here.** The per-release
`run-gate.sh` copies under `_orchestra/` were untracked and drifted: 0.2.203's copy ran every
file through `python -m unittest`, so the script-style suites (`test_e2e.py`, `test_ui.py`,
`test_fleet_standalone.py` — a `main()` and no TestCase) printed `Ran 0 tests` and **counted as
passes**. The runner:

- runs **one file per process** (`_NetworkDisabled` leaks when one process imports them all),
  each with a fresh profile it makes itself (`GATEWAY_APP_SUPPORT`, `GATEWAY_DATA`,
  `STUDIOHUB_READY_FILE`, `GATEWAY_NO_MIGRATE=1`), so even a suite that forgets `sandbox_env()`
  cannot read his real Application Support;
- runs unittest-style files through `-m unittest` and the script-style files named in
  `SCRIPT_STYLE` as scripts. **`Ran 0 tests` is a FAILURE, never a pass**, and a new
  script-style file fails loudly with "collected no tests" instead of passing silently;
- **runs files in parallel — the owner's decision, 2026-10-26: "yes. in parallel."** Default
  `min(3, cores/2)` workers; `GATEWAY_GATE_WORKERS` or `--workers N` overrides it, `1` is serial.
  Every file keeps its own process, temp profile and log either way;
- runs a short list **alone** (`SERIAL`): the native/offscreen AppKit suites
  (`test_create_layout.py`, `test_fleet_action_banner_layout.py`, `test_fleet_banner.py`,
  `test_fleet_layout.py`, `test_jobs_outputs.py`, `test_secret_field.py`,
  `test_task5_native.py`, `test_whats_new_page.py`), `test_window_size.py`, and the suites that
  assert a real wall-clock ceiling (`test_catalog_fast.py`, `test_jobs_list_fast.py`,
  `test_lossless_restart.py`). A busy Mac already turned a timing test red on 2026-10-24;
- skips `test_ui.py` (it drives the installed app and takes over his windows) and
  `test_fleet_standalone.py` (`GATEWAY_ALLOW_LIVE_GENERATION=1` plus the real Hub as a
  location), printing the reason for each;
- writes per-file logs, exit codes and `summary.json` into `--evidence`, which it **refuses to
  reuse**, and exits non-zero on any failure. The suites print their own totals — quote those,
  never a hard-coded check count.

The console/JS suites (`node --test tests/<file>.js`) and the native single-element checks
(`tests/check_native.py`) keep their own commands; they are not part of this gate.

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
is the suite to run knowingly: it is the owner-sanctioned live gate, and it must
never run without `GATEWAY_ALLOW_LIVE_GENERATION=1` and the Hub reachable.

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

Always rebuild when the claim is about the app bundle: `./menubar/build.sh` builds
and installs to `/Applications/gateway-kh.app`. Use `--no-install` unless
installing at a quiet point. **"It builds" is not "it works."**

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

  * `applicationShouldTerminate` calls `stop()`, which waits for each known
    backend's PID/start-time identity to exit (TERM for ≤2 s, then KILL for ≤1 s).
    A closed port alone is not proof of exit. Stop refuses replacement startup
    if a known backend remains or an unrelated service holds the port; unrelated
    listeners are never signalled. Quit is refused if Stop fails. The
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
    (2414) and started the app's own (3252, stamp `cd866d5-…`). Since 0.2.143,
    Restart Gateway uses the same identity-checked `stop()` → `start()` path.

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
  `_trash/` is outside Git and nothing in it is read by the app, tests or build.
- **Every change finishes through qualified main integration, push, DMG, release and intended-machine verification.**
  Do not call source checkpoints or builds stable, done or shipped. Owner, 2026-11-10: canonical
  `dist/` keeps the latest **two verified released** DMGs, named `Gateway Studio KH-<version>.dmg`.
  `menubar/build.sh` stages an exact candidate under `dist/.candidates/`, including its build stamp;
  a failed or same-version rebuild never moves the accepted pair. After install/launch verification
  and quiet/active-job/dependency checks, the release lead calls `scripts/accept_release.py` with
  the exact candidate, installed identity and real acceptance receipt. It invokes the canonical
  shared `release-dist.py` helper to promote verified bytes and retire only obsolete managed DMGs
  recoverably to `_trash/dist/`. Adopt an existing verified pair using trusted exact pins before
  the first promotion. Preserve working rollback/installers and active release inputs until proof.
  `dist/` and the helper's root `.release-dist.lock` stay outside Git.
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
- Prefer the **graphify** graph over grepping for architecture, caller and impact
  questions: `graphify query "<q>" --graph /build/Developer/_graphs/gateway/graphify-out/graph.json`.
  It refreshes itself hourly from `main`; nobody runs `graphify update` or
  `graphify extract` by hand, and unreleased code is checked against the worktree. It reports a **syntax error in
  `menubar/VoicesWindow.swift`**: a parser limitation, not a defect. `swiftc -parse`
  is clean, so check the compiler before believing it.
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
  *installed bundle* whose `/api/health` answers, and check its version against
  that answer.**
  `/Applications/Studio Hub.app/Contents/Resources/voice/app/backend/catalog.py`
  serves. `/build/Developer/studio-hub-kh` is a development checkout and can be
  **ahead of it** — 0.6.31 while the running Hub reported 0.6.29 — so a
  number read there is a claim until the bundle agrees. The file you can find is
  not automatically the running one, and the Hub auto-updates (0.6.18 → 0.6.23
  → 0.6.29 → 0.6.31) enough that the difference is not hypothetical.
- **Every release updates `docs/tasks/HANDOFF.md`.** Read it first in a new
  session, and keep it current: the version, anything newly learned, anything
  newly forbidden, and what is still open. A stale handoff is worse than none,
  because it is trusted — if it disagrees with the code, the code wins and the
  file gets fixed.
