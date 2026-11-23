# gateway-kh — current rules

One API in front of every local Studio Hub location and 18 cloud providers, so **Story Studio KH
points at a single URL** (`http://127.0.0.1:8787`) and its model pickers fill. Story Studio calls
exactly one endpoint and treats it as GenStudio; it needs no code change, only `genstudio_url` and a
key. `README.md` has the design; this file is the part that is expensive to rediscover.

Delegation, parallelism and the shared lead workflow: invoke the global `efficient-orchestration`
skill and follow `~/Developer/_orchestra/developer-handoff/LEAD-RULES.md`.
Ponytail/Graphify apply here and to delegated workers.

## Gateway UI is a web page; the Mac app shows it

- A page is built once in `gateway/admin_ui.html`; the app shows it through `ConsoleSection`, which
  injects its key at document start and sets `__GATEWAY_IN_APP__` so the page hides its sidebar —
  **never a key in a URL**. New or changed UI goes into the web page, never a new native view.
- Native-only exceptions, the whole list: menu-bar icon and menu, app menu and shortcuts,
  notifications and alerts, launching/quitting/restarting the gateway, `NSOpenPanel`, macOS
  permissions. "A web page cannot do it" is not a reason to add a native view without adding it here.
- **Page dialogs are compliant-by-delegate — do not reopen this in a `window.confirm` sweep.**
  `gateway/admin_ui.html` makes 44 `confirm`/`alert`/`prompt` calls (28 confirm, 10 alert, 6 prompt
  as of 0.2.250). The app's only `WKWebView` sets `web.uiDelegate = self`
  (`menubar/MainWindow.swift:126-133`) and implements all three panels as native in-app dialogs —
  an action-verb confirm whose Cancel is Escape, a text-field prompt, and an error alert — so none
  is a silent no-op in the packaged app. They are not rewrites waiting to happen.
- A verified web page replaces a native one; the native page is **switched off but kept** in the
  repository so it can be restored — never deleted. Retired this way: the native Fleet page
  (`FleetSection.swift` and the `Fleet*.swift` views).
- **Settings → Appearance offers exactly System, Light and Dark** (owner, 2026-11-12), System the
  starting value for a new install; a stored fourth value reads as System.

## Contract traps — each cost real debugging time

- **Never return 402.** Story Studio holds a credit lock on the API key; 402 with no published
  balance kills the key permanently. A provider out of funds is a settled `failed` job.
- **Publish a priced catalogue with every amount at zero, never omitted.** Story's parsers drop any
  row whose price fields are absent. `credits_per_*: 0`, `minimum_credits: 0`, `price_version:
  "flat"`, an explicit `chargeable`, and a balanced `0/0/0` settlement triple are load-bearing.
  `chargeable` is truthful (`pricing.chargeable`).
- **`freshness.stale` must stay `false`** by construction: it is computed from the last catalogue
  *rebuild*, which `freshness()` performs before reading it. Making it fire would disable every
  model on a stale inventory. The honest numbers are `inventoryAgeMs` / `inventoryOldestProvider` /
  `inventoryOldestAt`; `null` means unknown, not zero.
- **`finish_reason` must be a clean stop** — `length` is refused as truncated.
- **Generation artifact URLs stay on the API origin** (relative). Content Pack signed downloads are
  the one absolute same-origin exception; never derive an origin from untrusted Host headers.

## Publish what is known, and nothing else

- A field a provider does not state is published **`null`**, never a plausible default: "none" and
  "could not work it out" must never be the same value.
- Read stated figures with `_first_stated()` (first non-`None`), **never an `or` chain**: a stated
  `0` is a claim. Spellings differ (`context_length`/`inputTokenLimit`, …).
- Every limit carries a `*_source` (`provider` / `curated` / `gateway`), value and source from one
  function. The one invented value is TTS `maximum_input_characters` (Story calls
  `.toLocaleString()` on it): provider figure, else 15,000 local / 25,000 cloud, with its source.
- A voice's language is stated or `""`; never assume `"en"`.
- Changing an int to `null` breaks the reader (Story substitutes 8,192): list every field whose
  *guarantee* changed.
- Depth: [docs/publish-what-is-known.md](docs/publish-what-is-known.md).

## A video model describes itself, and the record is the request mapping

- One `VideoCapability` record per model in `gateway/video_capabilities.py`; nothing else types a
  video option and `adapters/fal.py` names no model. Provenance is `provider` or `curated` with
  `source_date`. A record older than the provider's schema is re-read, never edited from memory.
- No record → mode published, no variants: visible, never selectable. A value the record does not
  publish is **refused**, never clamped or dropped.
- The scene image must arrive: `POST /v1/videos/source-images` → `api._stage_video_reference` → each
  adapter sends the bytes in its provider's own field; a staged file that has gone is a named refusal.
- Adding a model: read the endpoint schema, append a record, run `tests/test_video_capabilities.py`.
- Depth: [docs/video-capabilities.md](docs/video-capabilities.md).

## Discovery ≠ availability

- Nothing reaches Story until it is enabled; the local Studio Hub (`auto_enable_discovered`) is the
  sole exception. A kind with no adapter stays configurable with its models withheld.
  `IMPLEMENTED_KINDS` (`providers/definitions.py`) is the single source of truth.
- An adapter for a kind ≠ every modality works: transcription is `openai_compat` and `studio_hub`
  only and always asks for `srt`; non-SRT models are in `TRANSCRIPTION_SRT_UNSUPPORTED`.
- `ModelInventory.can_publish` is the single rule for the catalogue and the console list. The worker
  re-checks `enabled_models(modality)` at run time (`MODEL_NOT_OFFERABLE`).
- fal reads its per-endpoint schema (field types differ; its queue validates at the worker); its TTS
  is the ElevenLabs routes. xAI is `KIND_XAI`: images take `aspect_ratio`/`resolution`, never `size`.
  Lyria is Gemini `:generateContent` — scan every part for `inlineData`. OpenRouter `:batch`
  variants stay off.
- An upload past 25 MB is refused **413**. A voice is never invented: a missing `voice_id` refuses.
- `fleet._is_probe_machine` drops `probe-fleet`/`probe-hub` by **id**, never label.
- Depth: [docs/discovery-and-availability.md](docs/discovery-and-availability.md).

## Browser-extension workers (`chrome_extension`) poll; they are not servers

- **Wire v1** (`/api/workers/*`), kept exactly as Relay's handoff §4 defines it:
  `GET /api/workers/health` → `{ok, workerId, kind, maxResultBytes, leaseSeconds, …}`;
  `POST /api/workers/claim` → `204` or `{job}` or `{job: null, quoteRequired: {job, payloadSha256,
  executionAuthorized: false}}`; `POST /api/workers/jobs/{id}/progress` `{phase, message}`;
  `POST /api/workers/jobs/{id}/result` (multipart file + mediaType);
  `POST /api/workers/jobs/{id}/fail` `{error, ambiguous}`; plus `/api/workers/telemetry` and the
  scoped manager routes. `gateway/extension_workers.py` serves it; `adapters/chrome_extension.py`
  hands jobs to it.
- **The switch is `GATEWAY_EXTENSION_WORKERS`** (read live): off, every route answers 404. It is
  **on for the 1301 gateway** (owner, 2026-11-12) — the owner's words: *"it is to be run on the
  1301 mac. as it's a gateway machine and still left a lot of ram we can have 2-3 sessions
  running"*. The earlier "never 1301" restriction is void. No paid Flow generation without a go
  that names it; the first claim is a free image job.
- **Pairing.** `POST /admin/api/extension-workers {worker_id, kind}` (`require_local_app`: loopback
  plus the gateway key) returns the token **once**, shaped `ekw_` + 43 chars; only its sha256 digest
  is stored, in `APP_SUPPORT/extension-workers.json` (0600, via `admin_session`). Re-minting revokes
  the earlier token. A token opens only `/api/workers/*` for its own worker id and kind — the gateway
  key is not a worker token. **A minted token has `dispatch_enabled: false`**: pairing alone still
  answers 204, and a manager's `/api/workers/manager/control {worker_ids, dispatchEnabled: true}`
  (or the app's Browser workers pane) is what arms it.
- **Providers ship off.** `story-studio-video`, `story-studio-image` and `story-studio-audio` are
  `default_enabled=False`; their models come from `gateway/extension_*_models.json`, copied by
  script from Relay's catalogue (never hand-edit; re-copy and update `source_sha256`). Enabling the
  provider is not enough — the model must be switched on in Models too, or Create offers nothing.
- **Automatic pairing is one narrow door.** Settings → Local apps → "Allow local apps to pair
  automatically" (on by default) writes `~/.kh/gateway-endpoint.json` (0600, rewritten on launch)
  with `{url, machineId, pairingToken, schema}` and never the gateway key. The pairing token can
  only `POST /admin/api/extension-workers/pair {kind, worker_id}` over loopback, returning a worker
  token through the ordinary mint path; it cannot read providers, change settings, publish, or act
  as admin. Off removes the file and revokes the token. Manual pairing stays the other-Mac fallback.
- **A claim needs an account observation and an exact quote in the same request**
  (`extension_workers.py` refuses otherwise): schema-1 account (`provider`, opaque `accountId`,
  `source`, `observedAt`, connection/sign-in/readiness/activity, `nextEligibleAt`), then a quote
  naming `payloadSha256`, provider, accountId, value, unit, source, observedAt. `readiness: ready`
  with a **null balance is claimable**; a quote of **0 is a real price** (free images); a quote above
  a known balance is refused (`ACCOUNT_CREDITS`).
- **One record per thing.** The job is the `JobStore` row (`upstream_job_id = ext:<worker>:<job>`,
  checkpointed at claim); the hand-off is in memory. Never a second job store.
- **Paid credits.** Unclaimed = withdrawn, unsent. Claimed then `ambiguous` (anything but `false`), a
  silent lease or a restart = `PROVIDER_SUBMISSION_UNCERTAIN` hold, never re-offered; a late result
  adopts into the held row. A late `fail` with `ambiguous: false` settles it; an ambiguous one stays
  held. Late answers reach only the row whose **current** `upstream_job_id` matches.
- **Audio discovery** uses optional authenticated `audioInventory` on claim (schema 1, live, complete,
  fresh); health advertises the age/body limits. Membership is the pair evidence there is — neither
  provider publishes per-pair compatibility — so an audio job is offered when both ids are members of
  one fresh observation and the copied catalogue's limits pass. Depth:
  [docs/extension-audio-inventory.md](docs/extension-audio-inventory.md).

## Sound sources are managed like providers, and are not providers

- No entry in `providers/definitions.py`: declared once in `sound_sources.SOUND_SOURCE_DEFINITIONS`
  with one class per kind in `_ADAPTERS`, cross-checked at import. The SFX tab is a pane of the
  Providers page (`#sfx`).
- Credentials live in `CredentialStore` under the source's `credential_id` (`openverse-sound`, never
  renamed). A secret is never echoed, logged or put in error text. `configured` and `registered` are
  separate facts; quota reads "not read yet", never `0`.
- Depth: [docs/sound-sources.md](docs/sound-sources.md).

## Received artifacts are temporary

- The gateway is a receiver; Story keeps the copy that matters. `gateway/retention.py` prunes at
  **3 days or 10 GB**, whichever comes first; a pin outranks both; nothing younger than an hour is
  removed and active/held inputs are protected.
- Pruning **moves** (`DATA_DIR/_trash/`, then macOS Trash) — never a permanent delete. Settings live
  in `DATA_DIR/retention.json`, not `.env`.
- Depth: [docs/received-artifacts.md](docs/received-artifacts.md).

## `.env` is read live, and the key is checked per request

- `config.get()` re-stats `ENV_PATH`; `require_auth` reads `gateway_api_key()` **per request**, so a
  key rotation works with no restart. `/admin/api/*` is deliberately open from loopback — measure
  auth on `/v1/...`. Depth: [docs/env-and-auth.md](docs/env-and-auth.md).

## Remote final rendering is the owner's per-Mac switch, and two keys must both be turned

- A Mac renders for Story only when the owner switched it on (`render_optin.json`, absent = off) and
  its Hub's `video.final_render` row carries `accepted_unqualified` with
  `provenance: "owner-opt-in-unqualified"`. The switch travels via
  `startup_control.set_node_render_authority` (no SSH); "on" is kept only if the Hub took it.
- Depth: [docs/remote-final-rendering.md](docs/remote-final-rendering.md).

## Ports and limits

- **8787** is this gateway's port (`gateway/config.py`, the app's `AppConfig`, every Story client's
  base URL). Tests read the configured port, never a hardcoded one. 8000 is contested and belongs to
  Story's own default; the old GenStudio KH API on 8000 is stopped, disabled and must not return.
- Curated limits: `DATA_DIR/model-limits.local.json` **fills gaps only** — a provider's published
  value always wins (`gateway/limits.py`).

## Locations are providers — but not separate products

- A Studio Hub location is a provider through the `studio_hub` adapter; Story never learns a second
  address. `fleet.publish_id` collapses locations into one `studio-hub` namespace, and
  `catalog_encode` applies it so the id Story sent is the id it is told back.
- The gateway chooses the site, never the machine: `fleet.route` ranks by pressure, then a weighted
  lottery; capacity is a weight, never a rank term; speed is not a routing input.
- `can_publish` withholds a model whose `dispatch_state` is present and not `eligible`. A voice
  (`studio-hub-<controller>::<raw>`) pins the route and is never re-routed.
- Secrets are in the credential store, the roster in `fleet.json`. Iterate `registry.definitions()`,
  never `PROVIDERS_BY_ID`. An address is `values.get("base_url") or definition.base_url`. An absent
  credential stays absent. Test row actions by pressing them, not only through the API.
- Depth: [docs/locations-are-providers.md](docs/locations-are-providers.md).

## Lifecycle: release everything on quit, and never adopt a foreign gateway

- **Quit takes the service down** — helper stopped, port freed, launchd job left unloaded. `stop()`
  waits on each backend's PID/start-time identity (TERM ≤2 s, then KILL ≤1 s); a closed port alone is
  not proof of exit. Quit is refused if stop fails; the Settings opt-out is *"Keep the gateway
  running after I quit"* (`keepGatewayRunning`, default `false`).
- **Never adopt another build's gateway.** A listener answering `/health` with a different `build` is
  stopped and replaced; if it will not stop, the app says which build is holding the port. Measured
  on 1301: a 0.2.241 helper out of an updater trash folder was adopted by a 0.2.246 app.
- **A second instance exits quietly** (`exit(0)`, stderr "gateway-kh is already running; exiting this
  copy.") and never activates or fronts a window — launchd's `KeepAlive` must not read it as a crash.
- **Kill by PID, never by pattern.** `pkill -f "Gateway Studio KH"` kills the shell running it, and
  the next `open` adopts the survivor. Confirm 8787 is free and the gateway PID *changed* before
  measuring anything.
- **Uninstall** removes the app, the helper, the `kh.gateway.menubar` job and the LaunchAgent plist,
  and keeps the user's data. Known gap: stale background-item rows survive (`sfltool dumpbtm`);
  the planned fix is moving launch-at-login to `SMAppService.agent` so it can be unregistered.
- **Build identity:** `menubar/build.sh` writes `Contents/Resources/gateway/BUILD`
  (`<short-sha>-<UTC yyyymmddHHMM>`) and sets `GatewayBuild`; `/health` publishes it, and Check
  Gateway names both stamps when they differ.

## Verification

```bash
.venv/bin/python scripts/run_gate.py --evidence <fresh dir>   # the release gate: every tests/test_*.py
.venv/bin/python scripts/test_run_gate.py                     # the gate runner's self-test
```

**Never run `python -m unittest discover -s tests` on this repo.** It reports ~96 failures and
~107 errors that are not regressions: `_NetworkDisabled` leaks across files when discover imports
them all into one process. The gate runner exists for exactly that reason; use it, or the focused
one-file command above. (Cost: a person who runs discover first reads the suite as red.)

- The gate runs **one file per process** with its own profile (`GATEWAY_APP_SUPPORT`,
  `GATEWAY_DATA`, `STUDIOHUB_READY_FILE`, `GATEWAY_NO_MIGRATE=1`), in parallel
  (`min(3, cores/2)`), with a `SERIAL` list for the native/offscreen AppKit suites and the
  wall-clock suites. **`Ran 0 tests` is a FAILURE, never a pass.** It refuses to reuse an evidence
  directory, writes per-file logs and `summary.json`, and exits non-zero on any failure. Quote the
  suites' own totals.
- It skips `test_ui.py` (drives the installed app and takes over the owner's windows) and
  `test_fleet_standalone.py` (`GATEWAY_ALLOW_LIVE_GENERATION=1` plus the real Hub), printing why.
- **Test narrowly and off the owner's screen.** Console/JS → `node --test tests/<file>`; Python →
  the focused isolated suite; native Swift → build, then a named read-only `tests/check_native.py`
  check. Reserve `test_ui.py` for an explicit request or a promotion gate; it needs Accessibility
  permission and takes over his app. **Never take the owner's desktop** (owner, 2026-11-12): no
  `open` without `-g -j`, no `activate`, no keystrokes or clicks, no visible window, nothing
  restarted on his Mac to "prove" something — ask first and name what would appear and for how long;
  test-machine work needs no ask.
- `hub.find_ready_file()` falls back to the owner's real Hub: any isolated suite must set
  `STUDIOHUB_READY_FILE` in its temp dir, and any test reaching a *location* points it at
  `tests/mock_upstream.py`.
- **Always rebuild when the claim is about the app bundle:** `./menubar/build.sh` (build + install);
  use `--no-install` unless installing at a quiet point. **"It builds" is not "it works."**

## Where a built artifact is — two paths, and they are not interchangeable

- **A fresh worktree cannot build until `data/final-render/` is copied into it** (`cp -R
  data/final-render <worktree>/data/`). Without it the build stops with "selected render authority
  source is unavailable" — met again on 2026-11-20 in a worktree created an hour after the previous
  one, so it is a per-worktree step, not a one-time setup.
- **Unaccepted build → a candidate inside the app's own worktree:**
  `worktrees/<task>/dist/.candidates/<commit>-<stamp>.<rand>/Gateway Studio KH-<version>-<commit>-<stamp>.dmg`.
  `menubar/build.sh` stages it here and says so on its last line.
- **Accepted release → canonical `repository/dist/`**, and only after the build is installed and
  verified. That folder means "accepted" and has for every release since 0.2.287; an uninstalled
  DMG put there breaks the one convention that makes it readable.
- **Quote the absolute candidate path beside every hash.** A hash without a path is not verifiable,
  and two people have already looked in `repository/dist/` for a build that had not installed yet.
- `My Own Apps/gateway-studio-kh/dist` is a **dangling** link to the pre-reorganisation path and
  answers nothing; it is pending a decision, so do not use it as a way to find anything.

## Working rules

- **Every removal goes to that project's `_trash/` — never `rm`**, including files you decide are
  clutter. Keep sub-structure and say where it went. `_trash/` itself is the owner's: never tidy
  inside it. For a Git branch or remote ref, record name and SHA in a note under `_trash/` before
  deleting, and never rewrite published history.
- **Every change finishes through qualified main integration, push, DMG, release and
  intended-machine verification.** Do not call source checkpoints or builds stable, done or shipped.
  Canonical `dist/` keeps the latest **three verified released** DMGs (owner, 2026-11-12), named
  `Gateway Studio KH-<version>.dmg`; `menubar/build.sh` stages an exact candidate under
  `dist/.candidates/`, and a failed or same-version rebuild never moves the accepted set. After
  install/launch verification and quiet/active-job checks, the release lead runs
  `scripts/accept_release.py` with the exact candidate, installed identity and real acceptance
  receipt; it calls the shared `release-dist.py` helper, which promotes verified bytes and retires
  only obsolete managed DMGs recoverably to `_trash/dist/`. `dist/` and its ledger stay outside Git.
  **Publishing does not wait for an approval ceremony** (owner, 2026-11-12): a stable, gated
  release goes out immediately; the owner's holds on paid, public and Production actions stand.
- **Version bumps are patch-only:** `VERSION` at the repo root is the one place to bump
  (`menubar/build.sh` reads it). Increment the third number per shipped change; do not roll to
  `0.3.x` or `1.0.0` until the owner says so.
- **Never hand another session a description of behaviour that is not deployed yet.** Either restart
  and measure the **running** service, or say the change is built but not live.
- Prefer **graphify** over grepping for architecture, caller and impact questions:
  `graphify query "<q>" --graph ~/Developer/_graphs/gateway/graphify-out/graph.json`.
  It refreshes hourly from `main`; nobody runs `graphify update` by hand. It reports a false syntax
  error in `menubar/VoicesWindow.swift` — a parser limitation; `swiftc -parse` is clean.
- **A page must never be able to resize the window.** A page with a *required* height propagates it
  and AppKit grows the `NSWindow`; such a page wraps its body in an `NSScrollView` with a
  `FlippedView`. Keep a table's floor low (140) and cap any wrapping label with
  `maximumNumberOfLines`.
- Mark deliberate simplifications with a `ponytail:` comment naming **the ceiling and the upgrade
  path** — unmarked, the next reader cannot tell a trade-off from an oversight.
- **Every release updates `docs/tasks/HANDOFF.md`** with the version, what was learned, what is
  forbidden and what is still open. If it disagrees with the code, the code wins and the file gets
  fixed.

## Where the depth lives

[studio-hub wire contract](docs/studio-hub-wire-contract.md) ·
[voices and reference clips](docs/voices-and-reference-clips.md) ·
[giving the app away](docs/giving-the-app-away.md) ·
[extension account workers](docs/extension-account-workers.md) ·
[job retry policy](docs/job-retry-policy.md) ·
[backup and restore](docs/backup-restore.md) ·
[developer mode](docs/developer-mode.md) ·
[fleet deployment notes](docs/fleet-deployment-notes.md)
