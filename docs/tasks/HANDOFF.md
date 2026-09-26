# HANDOFF — gateway-kh

**Read this first.** Written 2026-09-26 against **VERSION `0.2.39`**. The running build
stamp is on `/health` and in `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD`.
If this file disagrees with the code, **the code wins — fix this file in the same change**.
Every path below is inside the Git checkout `repository/` unless it says otherwise.
`[MEASURED]` = observed in the session that wrote this; `[reason not recorded]` = the
project does not say why, and nobody should guess.

## WHERE THINGS STAND

| Thing | Value |
|---|---|
| Current release | **0.2.39** — `repository/VERSION`, one line, the only place to bump |
| Declared Studio Hub | **0.6.65** — `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION`, published on `/health`; a controller on another version is reported, never refused. Set on 2026-09-25 from a **running** Hub (`/api/version` → `0.6.65` build `37cb07b`), not a changelog `[MEASURED]` |
| Branch | `main` = `origin/main`, clean tree; remote `github.com/theng12/gateway-kh` |
| Running build | `/Applications/gateway-kh.app`, port **8787**, stamp `08a4831-202609251515` (`/health` and `…/Resources/gateway/BUILD`) — the gateway must be **respawned**, not adopted, to carry a new stamp: with *Keep the gateway running after I quit* on, quitting the app leaves the old process holding 8787 and the new app adopts it, so `/health` keeps reporting the old build while serving new code `[MEASURED 2026-09-25]` |
| Distribution | DMG in `repository/dist/` (`gateway-kh-0.2.39.dmg`), named by version alone |
| SSD/setup kit | `repository/dist/setup-kit/`. **The four `KH-STUDIO-*` drives now carry Studio Hub 0.6.65** `[MEASURED 2026-09-25]`, refreshed 21:34–21:40; the GatewayKH side on them is older — check before a rollout |

- **Versions are patch-only for now:** `0.2.1`, `0.2.2`, … up to `0.2.1000`. Increment the
  third number by one per shipped change; never roll to `0.3.x`/`1.0.0` until the owner says so.
- **Finished work is committed and pushed, and a release carries a DMG.** Do not call work
  done while uncommitted, unpushed, or without an installable artifact. `dist/` stays out of Git.
- **The setup kit is a snapshot, not a build output — and it is current as of 0.2.22.**
  `repository/dist/setup-kit/` pins GatewayKH **0.2.22** (`installers/gateway-kh-0.2.22.dmg`,
  sha256 `8446db07de94b397…` in `SHA256SUMS.txt`) and its `06-troubleshooting.md` gained the
  two sections this release needs: **"A node shows offline"** (the Hub's `no_hub` /
  `unreachable` / `unauthorized`, read from the installed Hub's own `peers.py` and
  `fleet_nodes.py`) and **"A job says Manual recovery required"** (discard vs close).
  Rebuild it deliberately for a rollout; never hand-edit a copy on a drive.
- **The four SSDs were refreshed from it on 2026-09-24** `[MEASURED]`:
  `/Volumes/KH-STUDIO-{ATLAS,NOVA,ORICO,UGREEN}/Video Generation Kit/GatewayKH/`, each
  verified with `shasum -a 256 -c SHA256SUMS.txt` and each keeping the superseded kit in
  `<drive>/_trash/superseded-kit-2026-09-24/` (the only place a superseded installer lives;
  the repo's `dist/gateway-kh-*.dmg` is the build output, not an archive).
- **Two kit indexes exist on each drive and both name every app's version** — the top-level
  `Video Generation Kit/README.md` and the copy beside the Hub kit, `Studio Hub KH/README.md`.
  They had drifted (top said Studio Hub 0.6.54 and GatewayKH 0.2.21; the Hub copy said
  GatewayKH 0.2.2 and Studio Hub 0.6.57, while the bundle in the folder reported **0.6.60**).
  Both now read the version each bundle actually reports. **Update both on every kit refresh**,
  or the drive's front page lies about one of the three apps.
- The port is **8787** everywhere that matters: the default in `gateway/config.py`
  and the app's `AppConfig`, this Mac's `.env`/`menubar.json`, and `README.md`'s
  "Why port 8787". It was 8000 until the owner moved it (2026-09-21, `Project
  Status.md` §10h: 8000 is the most contested dev port and Story Studio's own
  default). Every Story Studio client points at `http://<host>:8787`.

## WHAT 0.2.39 CHANGED — a model is qualified by what it has really carried

Asked 2026-09-26: *"we need to keep these for that when I want to qualify a new LLM,
we can apply the same parameter check too."* Before this the figures were read once by
hand and lost, so every question about a ceiling was answered from a producer's
advertisement. `gateway/qualification.py` reads them back off the work itself.

**Nothing new is recorded for it.** Every figure was already in
`result_json.payload` on each settled job: `usage.{prompt,completion}_tokens`, the
worker's own `generation_seconds` (after the model is resident, so it excludes a cold
turn's load), and `hub_machine`/`hub_worker` on the image, voice and music lanes. The
change is a reader, not a schema.

- **`GET /admin/api/qualification`** — per (provider, model): samples, the largest
  prompt and completion **that actually ran**, the median rate, per-machine splits,
  first/last seen. `?provider_id=`, `?model_id=`, `?limit=` (1–500, default 500).
- **The measured maximum is the headline, not the stated one.** "Can this lane carry
  8,000 tokens" is answered by the biggest it has carried; "may I ask" is a different
  question with a different owner.
- **`stated` is joined on the *published* namespace.** A job routed to
  `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`; comparing raw ids
  made every local row read as unknown, which is the one lane the ceiling question is
  about. `stated: null` means *not in the catalogue* (disabled or withheld) —
  unknown, never unlimited.
- **`source: "gateway"` on every row, and the stated figures sit beside it, never
  merged into it.** This is the rule the whole feature rests on, and it has its own
  test.
- **The console draws it** — Providers → **Measured**, under Concurrency. A field
  nothing draws is the same defect as a control that does nothing. An unmeasured
  figure renders as a dash, never a zero; a chat model's machine column reads
  **not named** rather than guessing.
- **What is deliberately *not* counted**: a queued job is not a sample; a lane that
  states no `usage` is skipped rather than folded in as zero tokens; a missing
  `generation_seconds` is an absent rate, never `0`; `True` is not a token count
  (`bool` is an `int` in Python, and read as a number it would enter the record as a
  one-token completion). Each of those turns a record of real work into a
  plausible-looking invention.
- **`limit` is declared with `Annotated`, and that is load-bearing.** Written the
  usual way — `limit: int = Query(50, ge=1, le=500)` — the default *is* a `Query`
  object, so calling the function directly handed it to SQLite:
  `type 'Query' is not supported`. The route was reachable **only** over HTTP, which
  is a worse test surface and a trap for the next in-process caller. The focused
  suite caught it. `/admin/api/jobs` still has the old shape; nobody has needed to
  call it directly.

`tests/test_qualification.py` → 21 checks; `test_console_actions.js` → 5 new;
`test_e2e.py` → 7 new, **557/557**.

### What it measured, while it was being built `[MEASURED 2026-09-26]`

- The **chat lane names no machine.** Its payload carries `finish_reason`, `model`,
  `usage` and `generation_seconds` and nothing that identifies a Mac, so a chat
  model's envelope is per location until Studio Hub names one. The image, voice and
  music lanes do name one.
- **4 of 13 `Qwen3.5-9B-4bit` jobs stopped at exactly 4,096 completion tokens, and all
  13 reported `finish_reason: "stop"`.** The cap is being hit and reported as a clean
  stop — so *"finish_reason must be a clean stop"* does not, on its own, detect
  truncation. Only the measured maximum next to the stated ceiling shows it. **This
  is the finding to carry to Studio Hub**, and it is exactly what this record exists
  to make visible.
- **The same model measured 17.3 tokens/second** (489 tokens in 28.2 s), 1.22
  tokens/word, 7.0 characters/word, 5.7 characters/token. At that rate **16,384
  tokens does not fit a 900-second deadline** (945 s) and 8,192 does (472 s).
- Highest completion seen per cloud lane: openrouter `deepseek-v4-flash` 15,139;
  experiential-labs `deepseek-v4.1-flash` 8,483; openai `gpt-6-luna` 6,221;
  tokenharbor `mimo-v2.5:free` 5,045.


**This is the change that makes a fleet worth owning.** The worker ran exactly one
job at a time — chat, image, TTS, subtitle and music all shared a single slot — so
nineteen Macs sat idle behind one render. It was deliberate, and the reason is
still recorded in the file it was written in: *"a second thread would add
concurrency bugs (double-claiming, artifact races) for no benefit."* Both risks
were re-checked and both are smaller than that note claims; the cost of the old
shape is not.

- **Three ceilings**, in `gateway/concurrency.py`, all of which must pass before a
  job is admitted: the **pool** (`GATEWAY_JOB_CONCURRENCY`, default 4), the
  **lane** (`GATEWAY_LANE_CONCURRENCY=image=1,chat=4`, empty by default), and the
  **provider**. Each answer carries its `source` — `hub` (a site's own
  `available_worker_slots`), `curated` (`data/provider-concurrency.local.json`), or
  `gateway` (a labelled default of 1, naming the file to edit).
- **`paid_concurrency_limit` is now derived, not written.** It was the literal `4`
  while one job ran at a time: a published number that did not describe the system,
  in a field Story Studio reads. It now comes from the same function that sizes the
  pool, so the two cannot drift.
- **Daemon threads, not a `ThreadPoolExecutor`.** An executor joins its threads at
  interpreter exit, so quitting during a two-minute render would hang the process
  instead of stopping it. The dispatcher is single-threaded and is the only
  admitter; correctness of the claim does not depend on that, only tidiness of the
  counters.
- **A limit of zero is not a stop.** A Hub reporting no free slots is busy right
  now, not closed; treating `0` as a cap would leave jobs due for ever with nothing
  wrong and nothing to see. Zero falls back to 1, and there is a test for it.
- **Two bugs the new tests caught**, both of the "setting that silently does
  nothing" shape this project keeps finding: `_positive_int` rejected numeric
  **strings**, so `GATEWAY_JOB_CONCURRENCY=7` and `GATEWAY_LANE_CONCURRENCY=image=1`
  were ignored and fell back to the default; and `test_job_claim`'s *"the loop
  returned, therefore the job ran"* became a race the moment `_loop` stopped
  executing inline. `Worker.wait_idle()` exists for the second one.

`tests/test_worker_concurrency.py` → 13 checks, stable over five runs. Focused
suites 30/30, `test_e2e.py` 555/555.

## WHAT 0.2.28 CHANGED — and the three things it cost to learn

- **GW-5: a location's models are re-read when its Hub changes build.** The refresher
  already probed every location and already knew the version, and did nothing with a
  change; discovery ran only when somebody pressed Refresh. `fleet.refresh_models_when_hub_changes`
  now compares the probed version against **the discovery cache's own `hub_version`** —
  not memory, because the gateway restart usually follows the update — and re-discovers
  on a difference. A location with no record is refreshed once, so it seeds itself. A
  **failed** discovery records nothing, so the next pass retries instead of marking
  stale rows current. The manual Refresh records the same baseline, so it is not redone.
  `tests/test_fleet_hub_version_refresh.py` → 10 checks. Retires OWNER-5.
- **GW-4: the declared Studio Hub is `0.6.65`**, set from a running Hub rather than a
  changelog. It was deliberately **not** set to `0.6.64` while that build was only held
  by controllers and running nowhere: the constant means *measured against*, and that is
  a claim a version nobody has run cannot support.
- **A test is a row, and it lies the same way.** `test_studio_hub_contract.py` asserted
  `hub_version_relation("0.6.64") == newer` with the numbers written in by hand. Bumping
  the declaration turned it red — correctly. It now derives every comparison from
  `SUPPORTED_STUDIO_HUB_VERSION`, so the next bump cannot break it. **When you move the
  declaration, grep the tests for the old number.**
- **The first fleet-wide release push, and what it found** `[MEASURED 2026-09-25]`:
  `POST /admin/api/release/publish` → **5 of 5 controllers accepted**, HTTP 200 in
  2563 s, and every controller countersigned with a **different** key, so each mirror is
  its own. Two things to carry forward:
  - **The request body is base64**, so a 1.34 GB artifact becomes 1.79 GB in one body and
    the gateway's RSS peaked at **8.0 GB**. That is the ceiling to watch on a smaller
    host, and the reason the controller-mirror beats pushing to every node.
  - **What is held and what the kit has are different facts.** The push landed 0.6.64
    while all four kit drives already carried **0.6.65**. Check both before aiming a push.

## THE WORKING LOOP

Run everything from `repository/`. Python tooling is the checkout venv `.venv/bin/python`.

| # | Step | Exact command / action | Footgun next to it |
|---|---|---|---|
| 1 | Make the change | edit under `repository/` | Never `rm` — move removals to `_trash/` (see below) |
| 2 | Bump the version | edit `repository/VERSION` | Patch-only; `build.sh` reads it for `CFBundleShortVersionString` and the DMG name |
| 3 | Build + DMG | `./menubar/build.sh` | Installs to `/Applications/gateway-kh.app` **and** writes `dist/gateway-kh-<VERSION>.dmg`. `--no-install` builds only into `menubar/build/` |
| 4 | Test narrowly | console/JS → `node --test tests/<file>.js`; Python → `.venv/bin/python tests/<file>.py`; Swift → `tests/check_native.py <check>` in the background | See the test rules below |
| 5 | Restart the app | kill the **PID** of `GatewayKH`, confirm port 8787 is free, then `open /Applications/gateway-kh.app` | Relaunching is pre-approved; `pkill -f "GatewayKH"` kills the shell running it |
| 6 | Ship | `git add -A && git commit && git push` on `main` | The DMG already exists from step 3; nothing else is required to hand it over |
| 7 | Publish a Hub app release (optional) | Fleet → Locations → *Publish a Studio Hub release…* → `POST /admin/api/release/publish` | Signing key stays in the data dir; trust install on a controller is the **owner's** action |

- **Verify the running build, not the bundle.** Read
  `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD` and `curl /health`; both must
  show the stamp you just built. A gateway outlives the app and the app **adopts** whatever
  answers `/health`.
- **`tests/test_ui.py` is a release gate, not a routine check.** It forces the window to 700 pt
  and presses controls, taking over the owner's screen. Reserve it for an explicit owner request
  or a promotion gate, run it in the background, and never run it for a change that touches no
  app file.
- **The real-Hub fleet suite is opt-in:** `GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python
  tests/test_fleet_standalone.py`. Without the variable it skips before contacting the Hub.
- **Always rebuild before claiming it works.** "It builds" is not "it works."
- **The release gate reads the Fleet page's *five tabs*, not the old single scroll.** In
  `tests/test_ui.py` §7 the tabs are AX **radio buttons** (`select_fleet_tab`: 1 Overview,
  2 Locations, 3 Machines, 4 Models, 5 Jobs), the page *remembers* the last tab, and each
  tab addresses its table differently — Locations and Jobs are nested one level
  (`table_texts(n, nested=True)`), Machines is a bare `table 1 of scroll area 2`
  (`nested=False`). Change the Fleet tabs and §7 must change with them, or the gate reads
  a loading view and reports the tables missing.
- Structural change → keep the graph current: `graphify extract . --update --code-only`.

## PATHS

| What | Where |
|---|---|
| Git checkout (all source) | `repository/` |
| Container (not a repo) | `..` = `gateway-kh/`; holds `test-instance/`, `backups/`, `_trash/`, `worktrees/`, `store-analysis/` |
| Build script | `repository/menubar/build.sh` |
| App source | `repository/menubar/*.swift`, `build.sh`, `whats-new.md` |
| Gateway source | `repository/gateway/` |
| Tests | `repository/tests/` |
| Build output / DMGs | `repository/dist/` (gitignored); container-root `dist` is a symlink to it |
| SSD setup kit | `repository/dist/setup-kit/` — copied to `/Volumes/KH-STUDIO-*/Video Generation Kit/GatewayKH/` |
| Release notes | `repository/menubar/whats-new.md` (newest first; shown by the app's What's New) |
| Live data dir | `~/Library/Application Support/gateway-kh/` (`.env`, `data/`) |
| Release publisher / state | `~/Library/Application Support/gateway-kh/data/release-publisher.pem` (0600), `release-state.json` |
| Test instance (code-only) | `test-instance/` — `./run.sh`, `./status.sh`, `./stop.sh`, port 8100 |
| Graph | `repository/graphify-out/` (gitignored, rebuildable) |

## RULES THAT COST US TIME

- **Never `rm` — every removal goes to `_trash/`.** That includes clutter you decide on
  yourself, keeping sub-structure, and say where it went. `_trash/` is the **owner's** to
  empty: never delete or tidy inside it. Git branches/refs are the exception — record name and
  SHA in a note under `_trash/` first. (`_trash/` is the owner's: it may be nearly
  empty, and its README may list items he has since removed — that is his call, not
  a defect to fix.)
- **`pkill -f "GatewayKH"` kills the shell running it** because the shell's own command line
  holds the pattern; the command never finishes and the old gateway survives. Kill by **PID**,
  then prove the PID changed and 8787 is free before measuring.
- **Never hand another session a description of behaviour that is not deployed.** Either
  restart and measure the running service, or say "built but not live."
- **Never return 402.** Story Studio holds a credit lock keyed to the API key; one 402 kills it
  permanently. A provider out of funds is a settled `failed` job — `test_e2e.py` asserts no 402.
- **Absence is `null`/absent, never a plausible `0` or default.** "Nobody stated it" and "it is
  zero" must not render the same. A stated `0` is a claim — read with a first-non-`None`
  lookup, never an `or` chain.
- **`freshness.stale` is always `false` by construction — do not "fix" it.** Making the gate
  fire would disable every model on a stale inventory. `inventoryAgeMs` is the honest number.
- **Never identify a machine, model, location or batch by its label.** Ids are identities;
  labels are the owner's to change. A location's provider id derives from its controller id.
- **A Hub's `site_id` is not unique until it is assigned one.** Studio Hub defaults it to the
  constant `"local-site"` (`components/hub/backend/control_plane.py::defaults`), so matching a
  new controller on it made the second "Add a location" overwrite the first. `fleet._stable_identity`
  falls back to the per-machine `controller_id`; do the same anywhere a controller is keyed.
- **A definite 4xx from any provider is its own refusal, never an unknown-outcome hold;**
  only a 5xx may have been accepted. The worker enforces this centrally on `BackendError.status`,
  so every adapter inherits it. A **connect timeout/refusal** is an unreachable location, not a
  job failure — but a read timeout *after* the request left is still ambiguous. A `400` that
  names `temperature` as unsupported is retried once without it (recorded as `params_dropped`),
  because newer reasoning models accept only their default — and `catalog._DEFAULT_TEMPERATURE_ONLY`
  now withholds that control from the known families on the provider that refuses it, so the
  retry is a backstop rather than the fix.
- **A page must never resize the window.** A page with a required height wraps its body in an
  `NSScrollView` with a `FlippedView`; keep table floors low and cap wrapping labels.
- **The bundled Python runtime's build is explicit:** `swiftc -target
  "${TARGET_ARCH}-apple-macos${MACOS_MIN}"` (default 11.0). Without `-target`, `swiftc` stamps
  the SDK's macOS version and macOS 14 refuses the app with "requires macOS 27".
- **`graphify` reports a syntax error in `menubar/VoicesWindow.swift` — a parser limitation,
  not a defect.** `swiftc -parse` is clean; trust the compiler.
- **Mark deliberate simplifications with a `ponytail:` comment** naming the ceiling and the
  upgrade path, or the next reader cannot tell a trade-off from an oversight.
- **`finish_reason` must be a clean stop** (`length` is read as truncated), and generation
  artifact URLs stay relative on the API origin.
- **A field nothing draws is a defect, and so is a fault repeated once per consequence.** The
  node roster's per-node `detail` and each machine's `health` and `available_gb` were fetched,
  published and read by nobody, so eight dead nodes were eight identical "offline" rows; the
  same fleet produced **148 issues for nine conditions** because one dead machine was reported
  as state + health + one row per studio + one per unreachable model. Before adding a field,
  name its reader; before emitting an issue, ask whether it is a *different* fault or the same
  one in another vocabulary.
- **A user-visible sentence is a contract when a test reads it.** `78fee1d` rewrote the Fleet
  page and lowercased "Could not discover" while moving "Retry failed" into `FleetTabs.swift`;
  `tests/test_task4_native.py` went red on 2026-09-23 and nobody noticed because that suite is
  not in the documented verification list. **Run every `tests/test_*.py`, not only the three in
  the table below**, or a red gate hides for days.
- **A version is declared, never a gate — and read what the far side states.** Two rules
  bought together on 2026-09-24. (1) Studio Hub auto-updates itself, so a runtime check on its
  version would turn *its* update into *this* fleet's outage; the gateway names the release it
  targets (one constant), reports a drift as information, and keeps every gate a capability
  question. (2) The same instinct in discovery: keying a Hub package to a modality by its
  **engine name** made a new engine invisible until this repo was edited, while the Hub was
  already stating `hub_modality` on every model. **When the far side states a fact, read it and
  keep your own table as the fallback** — the table's entries were guesses about a name, the
  stated field is the Hub's own answer. Anything left over is *reported by name*, because "it
  has nothing" and "I did not understand it" must never be the same answer.

## DO NOT TOUCH

- **Other repositories.** `~/Developer/studio-hub-kh`, `~/Projects/studiohub-standalone` and
  `theng12/Studio-Hub-KH` belong to other sessions and are read-only from here. The Hub fact
  that matters is the **installed bundle**
  (`~/Applications/Studio Hub.app/Contents/Resources/voice/`), which can differ from a checkout.
- **Other sessions' worktrees.** `worktrees/` and `test-instance/` are not yours to edit; the
  owner decided the test instance stays **code-only** (no keys, no Hub).
- **`backups/`, `_trash/`, `store-analysis/` and any archive/trash folder.** Recoverable
  history; moving things *in* is allowed, deleting or emptying is the **owner's**.
- **GitHub Actions stays disabled**, and do not include the real-Hub fleet suite in offline
  smoke testing.
- Do not restart the app without a reason — it interrupts the owner — though relaunching is
  otherwise pre-approved (he asked not to be asked, and to quit it yourself).

## OPEN / IN FLIGHT

- **Shipped in 0.2.25 (`0b03441`): the Hub chat lane is dispatchable, proved live on
  0.6.63.** Studio Hub 0.6.63 opened `POST /studio/chat/v1/chat/completions` to a
  **Fleet** credential, which is what made a Hub LLM possible here at all — before
  it, every chat route refused a fleet token. Reply:
  `_handoffs/2026-09-25-gateway-chat-lane-shipped.md`.
  1. `chat` is declared in all three places: `adapters._MODALITIES_BY_KIND[KIND_STUDIO_HUB]`,
     the `studio-hub-local` definition, and the **per-controller** definitions the
     roster derives (`providers/registry.py`) — the third is what lets a *remote*
     controller serve chat.
  2. `adapters/studio_hub._generate_chat` posts the OpenAI body to the **peer**
     surface through the Hub (admission, machine lease, worker signing), never a
     worker's own route and never the Hub-token-only `/api/hub/chat/completions`.
     `stream: false`; only the lane's honoured parameters are sent (asserted on the
     wire); `MODEL_NOT_PACKAGED`/`MODEL_NOT_READY` → `LOCATION_NOT_READY` so the
     worker re-routes; a `422` is a definite refusal, never an unknown-outcome hold;
     an empty answer is a failure.
  3. The catalogue publishes the Hub's own `model_limits` (262,144 read from the
     checkpoint, 4096 as a labelled worker default) with `*_source_detail` carrying
     the Hub's provenance sentence; `verified_token_usage: false` on every chat row
     (we validate what a provider states, we do not re-measure); and
     `generation_seconds` on the job envelope — the worker's own number, never
     derived from our round trip.
  `tests/test_hub_chat_lane.py` (7 checks, **wire** assertions),
  `tests/test_fleet_routing.py::HubChatRoutingTests`, `test_e2e.py` **555/555**.
- **Live, on this Mac's 0.6.63 Hub `[MEASURED 2026-09-25]`:** a real completion
  through the gateway — `succeeded`, `output_text: "Red, Blue, Green"`,
  `finish_reason: stop`, `usage 22/5/27`, `generation_seconds 0.573`,
  `verified_token_usage: false` — and the Hub's own `GET /api/hub/jobs` records
  those runs as `"modality": "chat"`. A `json_object` request returned parseable
  JSON first try, with the Hub's caveat recorded: **no grammar-constrained
  decoding**, so a strict-JSON call site is prompting plus validation.
- **The published model id is undecided on purpose.** The Hub's handoff says Story
  Studio's `lmstudio:` two-pass behaviour is keyed on an id **we** control and their
  tracked source has no code selecting two-pass from a model id, so they asked us to
  ask the Story session rather than guess. It publishes as
  `studio-hub::mlx-community/Qwen3.5-9B-4bit` (the normal Hub namespace) meanwhile —
  nothing is blocked. **Open question for the Story session.**
- **The remote chat proof (their test-plan item 4) is blocked on our side:** both
  controllers are on **0.6.54**, which refuses the chat lane to a Fleet credential.
  Bringing one controller to 0.6.63 proves it — and that is why the declared version
  moved to 0.6.63: a controller reads `older` until it can serve the lane.
- **A stale `dispatch_state` reads as "not installed".** The first live chat attempt
  was withheld because the cached package row said `files_state: not_installed` /
  `dispatch_state: ineligible` while the Hub answered a completion perfectly; a
  discovery refresh cleared it. Recorded as a property of the cache, not a defect on
  either side — but worth knowing before believing a withheld chat row.
- **Memory policy (Hub §3) is untouched by this gateway** — not read, not written,
  not on any page — so the five-mode change and the `auto` skew cannot break us. When
  it is surfaced: show `resolved_mode` and the resolved `idle_seconds`, never `mode`,
  because a null idle threshold renders as "kept loaded" — the opposite.
- **Shipped in 0.2.24 (`18408cc`):** the lane contract Studio Hub 0.6.61 published
  (`f20fd1a`, spec `docs/lane-execution-contract.md`) is read, and the one thing it
  does not yet give us is reported back in
  `_handoffs/2026-09-25-gateway-lane-contract-reply.md`.
  1. **Three readers place a model, strongest first** — the package row's own
     `modality` (0.6.61; present whenever the engine is registered, independent of
     the best-effort catalogue read), the catalogue's `hub_modality`, then
     `_HUB_ENGINE_MODALITY` in `providers/discovery.py`. Which reader placed the row
     is published as `raw.modality_source` (`package` / `stated` / `engine`), and a
     package the Hub itself refuses to place is reported **in the Hub's own words**
     — the sentence naming `_ENGINE_ROUTE`.
  2. **`lane_contract` is carried verbatim** on the model's cached row, and two
     fields are read: `deadline_seconds` (`adapters/studio_hub.lane_deadline_seconds`,
     replacing the flat 900 s poll ceiling for that model) and
     `states.discard_supported` (`api._lane_discard_state`). The Hub's chat lane says
     `false`, so a hold on it now publishes `close_hold` and `discard-uncertain`
     closes the record locally — before, the Jobs window offered a discard the Hub
     would refuse.
  3. **`model_limits` on chat rows** are published as the model's own numbers, with
     the Hub's provenance sentence in a new `*_source_detail` beside the value
     (`catalog._hub_stated_limit`). A Hub chat row no longer wears the owner's
     curated flat `1,000,000`: measured live, the Hub states **262,144** read from
     `model/config.json text_config.max_position_embeddings`, and **4096** as a
     labelled worker default.
  Not adopted, deliberately: `parameters`, `artifact`, `cancel.semantics`,
  `runtime_seconds` — on the row, consumed by nothing yet, and said out loud in the
  reply so the Hub session knows which half landed.
  `test_e2e.py` **552/552**, `tests/test_hub_lane_contract.py` (new, 10 checks),
  `test_studio_hub_contract.py` 54, every focused suite green.
- **The live 0.6.61 verification `[MEASURED 2026-09-25]`:** this Mac's Hub came up on
  0.6.61 mid-session, so the reads were measured against a real one and not only the
  mock — **23 models discovered, all 23 placed by `package`, 21 carrying a lane
  contract** with the canonical operation tokens and `deadline_seconds: 900.0`. The
  chat model `mlx-community/Qwen3.5-9B-4bit` arrives with `operation:
  chat.completion` and `discard_supported: false`, is **withheld** with the reason
  "the studio_hub adapter does not serve chat…", and carries both `model_limits`
  provenance sentences verbatim. Two new image models (`AITRADER/FLUX2-klein-4B-mlx-4bit`,
  `filipstrand/Z-Image-Turbo-mflux-4bit`) flow through as `image.text_to_image` — the
  owner's new models arriving with no gateway change.
- **The chat lane cannot be dispatched, and that is not a gateway defect.** The
  contract names the studio's `POST /v1/chat/completions`; on 0.6.61 the Hub serves it
  only at `/api/hub/chat/completions`, whose own docstring says it is for "this app's
  own Chat page" and which proxies to **this Mac's** worker. Nothing calls
  `chat_jobs.create_batch` (`transcription_jobs.create_batch` *is* called from
  `standalone.py`), so no chat batch can be created, polled or collected by a
  coordinator. Until the Hub exposes submit/status/artifact for chat, Hub chat models
  are **discovered, listed and withheld** — never dropped, never offered and then
  failed. The four routes we need are named in the reply note.
- **Declared Hub version stays `0.6.60`, deliberately.** 0.6.61 is installed here and
  staged on all four drives, but it is **not pushed to GitHub** and no *controller*
  runs it, so declaring it would be a claim we have not measured against — the exact
  thing this exchange exists to avoid. The local location reads `newer` today, which
  is the report doing its job. Raise the constant when 0.6.61 lands and a controller
  runs it.
- **The 401 on `/studio/voice/api/transcribe/availability` is ours to fix**, per the
  Hub session's trace: that node must be **re-joined with the controller's current
  Fleet token**. An owner action on that Mac. The refusal is now self-describing on
  their side (method, path, credential presented, credential required), which is what
  turned it from a mystery into a one-line fix.
- **Shipped in 0.2.23 (`e42be96`):** a declared Studio Hub version, and discovery that reads
  what the Hub states. Owner's decisions, 2026-09-24: **report drift, keep working**;
  **adopt whatever the gateway can already serve**.
  1. `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION = "0.6.60"` is the one declaration.
     `/health` publishes it as `studio_hub.supported_version`; `fleet._probe` publishes
     `hub_version_relation` (`match` / `older` / `newer` / null) per controller; Fleet →
     Locations shows it in the Hub column; drift is one **info** issue, so it never moves
     `summary.issues`. **No gate anywhere** — routing, the catalogue and the worker ask
     capability questions only, asserted by
     `test_studio_hub_contract.HubVersionDriftTests` and
     `test_fleet_routing.HubVersionReportTests`.
  2. **Discovery reads the Hub's stated `hub_modality` per model**, with
     `_HUB_ENGINE_MODALITY` only as the fallback. Before this, a package on an engine not in
     that table was `continue`d in silence — so a model added on the Hub on a new engine (the
     owner is adding an image model now; LLM and video are being researched) produced no row,
     no count and no reason. A lane this build serves arrives with no gateway release; a lane
     it cannot serve (video, chat) is discovered and **withheld**, listed with the reason; a
     package nothing can place is reported by engine (`DiscoveryResult.skipped` → the model
     cache → `/admin/api/providers` and `/admin/api/providers/{id}/models`, drawn as an
     "unrecognised engine" badge). Live: 35 models per controller, unchanged, **30 placed by
     the Hub's statement** and 5 by the engine fallback `[MEASURED]`.
  `test_e2e.py` **550/550**, console suites **35/35**, every focused suite green.
- **The first true thing the declaration said:** both controllers run Studio Hub **0.6.54**
  while this Mac and all four drives ship **0.6.60**, so both now read `0.6.54 · older`
  `[MEASURED 2026-09-24]`. That is a report, not a fault — the controllers keep taking work.
  Bringing them to 0.6.60 is `Studio Hub KH/1 - Install the app.command` on each controller.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00: TCP
  to every node address times out or refuses from this Mac too, so the report is accurate).
  The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.22 (`3e254c4`):** a fleet/node and Jobs-window audit. Six defects:
  1. `machines_flat` now joins the **node roster on the node id** and carries `available_gb`,
     so a dead node's health word (`no_hub` / `unreachable`) and the controller's own
     "could not reach the node: …" sentence reach the Machines tab, and memory reads
     "17.18 GB (7.14 GB free)" rather than an unlabelled total.
  2. `fleet._issues_for` no longer repeats one fault: the health row folds into the offline
     row, workers on an already-offline machine are one counted line instead of four, and
     `worker_unreachable` on an offline machine is left to that machine. Measured live:
     **148 issues → 19**, and the 7 that survive are a genuine `401` fault, not the offline
     machines. `tests/test_fleet_routing.py::OneFaultOneRowTests`.
  3. `catalog._publish_lock` writes `locked` / `lock_reason` / `availability` together. The
     merge updated only the first two, so a model offered by three locations with one
     answering published `locked: false` beside `availability: "unavailable"`.
     `tests/test_audit_regressions.py::CatalogueMergeTests`.
  4. `summary.models_available` counts a model once per location, like its denominator — the
     tile read **"175/102 local models available"** (rows over models) and now reads 60/102.
  5. The `78fee1d` tabbed-Fleet rewrite lowercased "Could not discover" and moved
     "Retry failed" into `FleetTabs.swift`, leaving `tests/test_task4_native.py` red since
     2026-09-23. **A user-visible sentence this file or a test quotes is a contract: change
     the check in the same commit.**
  6. **The 0.2.19 gap is closed.** `store._recover_interrupted` records the upstream attempt
     (`{upstream_job_id, upstream_item_index, recorded_by: "gateway_restart"}`), a lane with
     no discard route is **closed locally** (`HOLD_CLOSED`, with a recorded sentence saying
     the provider was not told), and `api.recovery_action` is published per job so the Jobs
     window draws `Discard uncertain attempt…` / `Close this hold…` / nothing. Six holds
     from before this release are in the live library — three OpenAI `gpt-6-luna` (created
     2026-09-24 00:35, four minutes before `8bd434d`) and three provider/network holds; all
     six now publish `close_hold` `[MEASURED]`. `tests/test_studio_hub_contract.py::DiscardActionTests`,
     `tests/test_job_recovery.py`, `tests/test_jobs_outputs.swift`.
  Verification for the release: `test_e2e.py` **546/546**, `test_ui.py` **130 passed /
  5 skipped** (the skips are Hub-dependent and Studio Hub is not running on this Mac),
  `check_native.py jobs-recovery` passes against the installed app, and `/v1/models`
  publishes **0** `locked`/`availability` disagreements `[MEASURED]`.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00:
  TCP to every node address times out or refuses from this Mac too, so the report is
  accurate). The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.21 (`987721e`):** `fleet._stable_identity` — a location is keyed by the
  controller's real `site_id`, or its per-machine `controller_id` when the Hub still answers the
  shared default `"local-site"`. Before this, adding a controller updated the previous one.
  `tests/test_fleet_identity.py`.
- **Shipped in 0.2.20 (`66e8afe`):** `catalog._DEFAULT_TEMPERATURE_ONLY` withholds the
  `temperature` control from default-only reasoning families **on official OpenAI only** — so
  Story Studio stops sending the `0.7` it sends whenever the control is offered. Scoped that way
  because a proxy (Experiential Labs' `gpt-5.6-luna`) accepts `0.7` and must keep it; a provider
  that publishes `supported_parameters` is always believed. `tests/test_chat_controls.py`.
- **Shipped in 0.2.19 (`8bd434d`):** a definite provider `4xx` settles as a failure instead of a
  durable unknown-outcome hold, and a model that refuses `temperature` is retried once without it
  (`params_dropped`). Covered by `tests/test_job_recovery.py` and `tests/test_chat_temperature.py`.
  **The gap it exposed — a cloud hold with no `recovery_context` could never be cleared — is
  closed in 0.2.22; see above.**
- **Shipped in 0.2.18 (`9b7876a`):** the gateway-side **downgrade guard** (refuse/warn when a
  controller already holds a newer version) and the per-controller **`trusted_publisher`** display
  in Fleet → Locations → Release. The guard is proven against the contract-enforcing mock only.
- **Unproven on a real controller:** the release push is proven only against the **local** Hub
  on 0.6.50. The two configured remote controllers were **off** (`LOCATION_UNREACHABLE`,
  ConnectTimeout), so a real remote push and the no-trusted-key (409) path are **not** verified.
  Installing the publisher key on a controller (`POST /api/hub/release/trust`) is the **owner's**
  action.
- **`release-state.json` is misleading if read quickly:** its last publish records a **15-byte**
  artifact `StudioHub-0.6.50-test0001.dmg`, not the 1.28 GB real DMG, and both controllers
  `LOCATION_UNREACHABLE`. `[reason not recorded]` for the tiny test descriptor; do not read it as
  "the real release is on those controllers."
- **Held at the owner's request:** a per-node image + TTS generation sweep; it waits for the
  fleet to be up.
- **No independent review** of the release-publisher work. The gate itself was fixed in this
  release: it had been reading the pre-rework single-scroll Fleet page (and a pinned `/18`
  provider count), so it failed against a correct app; §7 now walks the five tabs and the AX
  helpers retry a transient window read. If the Fleet tabs change again, that section must change
  with them.
- **Docs reconciled 2026-09-23.** `README.md`, `AGENTS.md` and `Project Status.md`
  (§1/§2/§9/§11) now agree with the code: port 8787, `main` as the only worktree,
  current release. The dated records — `_handoffs/`, `docs/superpowers/plans/`,
  and the dated sections of `Project Status.md` — deliberately keep the numbers
  they were written with; history is not rewritten. `docs/fleet-deployment-notes.md`
  remains the pre-deployment plan, marked `[confirm]` where Hub behaviour was then unknown.
- An unused remote branch `origin/codex/local-release-dmg` exists; left alone
  deliberately (not this session's ref to delete). `[reason not recorded]` for why
  it still exists.

## SECRETS

**There are no secrets in this file, and none belong in it.** Everything sensitive lives under
the live data dir `~/Library/Application Support/gateway-kh/` (gitignored): `.env`, and in
`data/` the `credentials.json`, `fleet.json` (controller tokens), `models_cache.json` and the
`release-publisher.pem` private key (mode 0600, never returned by an API). Provider keys and
Studio Hub tokens are the **owner's** and are installed by hand — ask him; do not search the
tree, and never print an address, token or private key into a note or a test.

## WHY THIS FILE IS TRUSTWORTHY

It was written by reading, not remembering: `AGENTS.md`, `Project Status.md`, `README.md`,
`gateway/config.py`, `gateway/release_publisher.py`, `menubar/build.sh`, `docs/*.md`,
`tests/check_native.py`, the last 40 commits, and the live `VERSION` / build stamp / data dir.
Where the project did not record a reason, this file says **`[reason not recorded]`** instead of
inventing one.
