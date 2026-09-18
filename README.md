# gateway-kh

A personal **model gateway**: one API in front of many backends, so Story Studio KH points at a
single URL and its model pickers fill up — with no stocking step, no credits, no customers and no
per-model setup.

It fronts two classes of backend behind one facade:

- **Local runners** — Studio Hub's studios on Apple Silicon (FLUX image, Qwen3/VoxCPM2/OmniVoice
  voice, Whisper transcription), reached over its `/api/hub/*` surface.
- **Remote providers** — 17 cloud providers, the same set the old GenStudio KH had configured:
  OpenRouter, DeepSeek, Anthropic, OpenAI, Google (paid + free tier), Groq, NVIDIA NIM, xAI,
  Token Harbor, Experiential Labs, fal.ai, Kie.ai, Black Forest Labs, MiniMax, ElevenLabs,
  Fish Audio.

## Why this exists (and why it is the sanctioned design)

Story Studio KH's own architecture contract requires that it talks to **exactly one endpoint**:

> `AGENTS.md` — *"Model/provider inventory and dispatch belong to GenStudio. Story Studio reads the
> live GenStudio catalog and **must not restore direct Studio Hub or sibling-Studio routing**."*

> `docs/architecture/security-reliability.md` — *"**Story Studio sends generation requests only to
> GenStudio. It does not call sibling Studio apps, Studio Hub, or upstream model providers
> directly.**"*

That is enforced by a boundary test suite (`audio-genstudio-only`, `genstudio-client-boundary`,
`private-routing-identity`, `legacy-network-boundary`, `scene-genstudio-boundary`). So a gateway is
not a workaround — it is the only architecturally legal way to route local Apple Silicon generation
into Story Studio. Story Studio sees this service as "GenStudio" and needs **zero code changes**.

## The one design decision that matters

**There is no billing here. But the catalogue still publishes prices — all of them zero.**

Story Studio's catalogue parsers build explicit object literals and **drop any row whose price
fields are missing**. The generators then refuse with *"this model is not in your GenStudio KH
catalogue."* A gateway that simply omits pricing therefore does not get "unpriced but working"
models — it gets an empty picker, and these stop working outright:

| Feature | Why it dies |
|---|---|
| TTS narration | row dropped without `credits_per_1000_characters` |
| Video generation | variant dropped without `credits_per_video` + `price_version` |
| Music generation | row dropped without `credits_per_request` |
| Image-to-image | `chargeable` must be published truthy |
| Durable LLM lineup | fallback advances only on a *proven zero settlement* |
| Paid replacement paths | need finite `credits` + non-empty `price_version` |

The fix is not to build pricing — it is to publish the **shape** of a priced catalogue with every
amount pinned to zero. All of it lives in `gateway/constants.py`, the single source of truth.

The settlement triple is load-bearing, not decoration. Every settled job returns:

```json
{ "reserved_credits": 0, "settled_credits": 0, "released_credits": 0 }
```

Story Studio advances its LLM lineup to the next candidate only when it can prove a failed attempt
cost nothing, and the proof requires all three fields present and balanced. The free-retry proof
actively *wants* a balanced zero.

## Architecture

```
                    ┌──────────────────────────────────────────┐
   Story Studio ───▶│  gateway/api.py   (the GenStudio contract)│
   (one endpoint)   └────────────────┬─────────────────────────┘
                                     │ accept + report only
                              ┌──────▼──────┐
                              │ JobStore    │  SQLite, idempotent
                              └──────┬──────┘
                                     │ due jobs
                              ┌──────▼──────┐
                              │ Worker      │  one thread, off the request path
                              └──────┬──────┘
                                     │ adapter.generate()
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
 openai_compat                  anthropic                    fal / kie
 (9 providers)                  (Messages API)               (async queue)
        │                                                          │
        └────────────────────────┬─────────────────────────────────┘
                                 ▼
                          studio_hub (local)
                    POST /api/hub/jobs → poll → artifact
```

Adapters implement one method returning a `BackendResult`. That is deliberately narrower than the
`GenerationProvider` port in `gateway/ports.py`: that port models a *remote asynchronous task*
(create/poll/download), which fits fal, Kie and Studio Hub but is an awkward fit for an
OpenAI-compatible chat call that answers in one response. Both shapes exist; nothing is forced.

Generation runs on a worker thread, never inside the HTTP handler — a local FLUX image can take
minutes, and blocking a Story Studio poll for that long would trip its timeouts.

## Quick start — the menu bar app

This is the normal way to run it. No terminal.

```bash
./menubar/build.sh          # compiles and installs to /Applications/gateway-kh.app
open /Applications/gateway-kh.app
```

It installs to **`/Applications`**, so it shows up in Launchpad, Spotlight and the Finder sidebar like
any other app. (An earlier version installed to `~/Applications`, which macOS hides by default — the
app was running but effectively unfindable. `build.sh` now prefers `/Applications` and removes any
stale copy from the other location.)

A bolt icon appears in the menu bar. It is a real native app (Swift + AppKit, `LSUIElement`), so
there is no Dock icon.

**Everything lives in one window**, arranged the way the sibling **Studio Hub KH** app arranges its
window — same grouped sidebar, same large-title pages, same status line pinned at the bottom. The
two apps are used side by side, so they are meant to be navigated the same way:

| Sidebar | What it shows |
|---|---|
| **Overview** (⌘1) | dashboard — status, counts, per-modality breakdown, recent activity |
| **Library & app** | |
| &nbsp;&nbsp;**Jobs** (⌘2) | what has actually run, with failure reasons and artifact URLs |
| &nbsp;&nbsp;**Voices** (⌘3) | voice sources, search, preview, enable |
| &nbsp;&nbsp;**Models** (⌘4) | search every provider's models and tick the ones Story Studio may use |
| &nbsp;&nbsp;**Providers** (⌘5) | provider keys and discovery |
| &nbsp;&nbsp;**Fleet** (⌘6) | every location this gateway can send work to |
| &nbsp;&nbsp;**Settings** (⌘,) | port, API key, startup |

**Models and Providers are separate pages.** Both are views of one console page, which used to be
shown whole under a single sidebar entry — so "where do I enable a model" and "where do I paste a
provider key" were the same destination with its own inner tabs. Each page now opens the console
directly on its own view, so the sidebar entry is the thing you want.


Below the list, a live line stays put on every page: a green dot, the endpoint, and how many
providers and models are on offer. It replaced a lone Settings gear, and it is the same
`safeAreaInset(edge: .bottom)` convention Studio Hub uses.

**Settings is a page here**, not its own window. It was a separate window first, on the reasoning
that macOS puts Settings in the app menu — but that reasoning assumes an app menu, and an accessory
app never shows one. The owner went looking for Settings and could not find it. Studio Hub lists it
in the sidebar under "Library & app", so this does too.

Every page opens with a large title and a one-line description of what it is for
(`PageKit.heading`), section titles are `title3` semibold, and page bodies share one padding —
all mirroring Studio Hub's `heading` / `sectionTitle` / `empty` helpers.

**Settings is a two-column form** (`PageKit.formRow`), the way Studio Hub's `Form` of `LabeledContent`
rows is: the label sits in a fixed 150pt column, right-aligned, and every control starts at the same
x. It first stacked a caption *above* each control, which put every label and every field at a
different x and read as unaligned. A test now asserts that all fields share one column.

**The menu:**

| Item | What it does |
|---|---|
| status line | live: provider and model counts, read from the gateway's own `/health` |
| Overview / Jobs / Voices | select a page in the main window |
| Open Provider Console (⌘P) | jump straight to the Providers page |
| Start / Stop / Restart Gateway | supervises the service directly |
| Studio Hub status | whether your local runner is up, and a shortcut to start it |
| **Settings…** (⌘,) | the Settings page |
| Open data folder / Open log | where credentials and logs live |
| Quit | quits the menu bar app; **leaves the gateway running** |



## The app is self-contained

`build.sh` bundles a **relocatable CPython** (from python-build-standalone) and a copy of the gateway
source into the `.app`:

```
gateway-kh.app/Contents/Resources/
  python/     relocatable CPython 3.11 + fastapi, uvicorn, httpx, python-multipart
  gateway/    the gateway source, plus the shipped default voice catalogue
```

Nothing outside the bundle is needed to run it — **the `.app` can be copied to another Apple Silicon
Mac and will work**. That is verified, not assumed: a copy was run from a fresh location with an
empty data directory and served the full Story Studio contract.

**Where your data lives** — never inside the bundle, which is read-only in normal use and replaced
wholesale on update:

```
~/Library/Application Support/gateway-kh/
  .env                 the API key Story Studio presents
  data/credentials.json  provider keys (0600)
  data/gateway.sqlite3   job history
  data/voices.json       voice catalogue
  data/artifacts/        generated images, audio, video
  menubar.json           app settings
```

An install from before this change is migrated automatically on first run — files are **copied, not
moved**, so the old directory remains as a fallback.

**Requirements:** Apple Silicon, macOS 13 or newer. The bundle is arm64-only and ad-hoc signed, so
Gatekeeper warns on first launch of a copied copy (right-click → Open).

**First run** opens the main window on Overview, and the app generates an API key if none exists —
the gateway treats an empty key as "accept any bearer token", which should never be the state you
are left in without being told. Launching with `--background` (which is what the login item does)
skips the window and leaves only the menu bar icon.

**Settings** is where the gateway's own API key lives, with a **Copy** button — that is the value
Story Studio needs. Paste it into **Story Studio → Settings → This Mac → GenStudio API key**, and set
**Base URL** to `http://127.0.0.1:8010`. It is the last item in the sidebar, under "Library & app".

**Launch at login** uses a LaunchAgent (`~/Library/LaunchAgents/kh.gateway.menubar.plist`) rather
than `SMAppService`, because `SMAppService` requires a signed, registered bundle and this app is
ad-hoc signed. The agent is `KeepAlive` only on an unsuccessful exit, so quitting the app
deliberately stays quit.

The app **supervises** the gateway: if the process dies it restarts with exponential backoff, and
the menu says so. It deliberately does *not* stop the gateway when the app quits — it is a service,
and closing a menu bar helper should not silently take the API away from Story Studio.

### Headless helpers

```bash
GatewayKH --status                  # print the app's current view as JSON
GatewayKH --install-login-item      # equivalent to the Settings checkbox
GatewayKH --uninstall-login-item
```

`--status` reads `~/Library/Application Support/gateway-kh/menubar-state.json`, which the app keeps
updated. That file is also how the build is verified without a GUI.

### Running it without the app

```bash
uv venv --python 3.11
uv pip install --python .venv/bin/python -r requirements.txt
GATEWAY_API_KEY=your-key .venv/bin/python -m uvicorn gateway.api:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000/admin** for the provider console.

### The flow

**Discovery and availability are separate things.** A provider publishing 300 models must not put
300 rows into Story Studio's picker. So nothing reaches Story Studio until you say so.

1. Open the console — **Providers** tab. Paste a provider's API key → **Save & discover models**.
   The key is stored `0600` and the provider's models are discovered live. The provider switches on;
   its models do not.
2. Switch to the **Models** tab. This is a single searchable list across *every* provider:
   - type to search by model name or id (`claude`, `flux`, `whisper`…)
   - filter by modality and provider
   - tick the models you want — each tick is one click and takes effect immediately
   - **Enable all matching** applies the current filter server-side, so "everything matching
     `seedream`" is one click even across thousands of rows
3. Story Studio's pickers now show exactly the ticked set, and nothing else.

The one exception is **Studio Hub (this Mac)**: the local runners auto-enable, because there are
about a dozen and requiring a click for each would be friction for no benefit. Everything else is
opt-in.

Counts are always visible in the header: models *discovered* versus models *live*.

## Adding things

**A new provider** — append an entry to `data/providers.local.json` and restart:

```json
{ "providers": [{
  "id": "my-provider", "display_name": "My Provider", "category": "language",
  "kind": "openai_compatible", "base_url": "https://api.example.com/v1",
  "modalities": ["chat"], "discovery": "openai_models"
}]}
```

Kinds: `openai_compatible`, `anthropic`, `google`, `fal`, `kie`, `fish`, `bfl`, `minimax`,
`elevenlabs`, `studio_hub` — **every kind has a working adapter**. Discovery strategies:
`openai_models`, `anthropic_models`, `google_models`, `fal_catalog`, `elevenlabs_models`,
`studio_hub`, `static`.

`IMPLEMENTED_KINDS` in `providers/definitions.py` is the single source of truth for which kinds can
generate, and `adapters/__init__.py` asserts at import that its adapter table agrees with it. A
provider whose kind has no adapter stays discoverable and configurable, but **its models are
withheld from Story Studio** — offering a model that fails on use is worse than not offering it.

## Voices

Providers do not agree about what a voice *is*, and the app shows the difference rather than hiding
it. The **Voices** page (⌘3) has three sources:

| Source | What it is | Default |
|---|---|---|
| **local** | this Mac's own cloning clips (Studio Hub) | **on** — yours, and only a handful |
| **account** | voices the provider already has for you (your ElevenLabs workspace, your Fish clones) | off — opt in |
| **library** | the provider's public catalogue | off — search, then *add* |

Only **enabled** voices reach Story Studio. That is not a nicety: ElevenLabs' public library alone
holds tens of thousands of voices, and Fish's `/model` list returns a thousand unfiltered.

The section lets you filter by provider, search, **preview** a voice before choosing it, tick the
ones Story Studio should offer, and — for library voices — add one to your own account. Library
search is live and uncached, because browsing is a query rather than a download.

Voices are addressed as `provider::voice`, so a voice id is unambiguous and resolves to exactly one
provider and one upstream id.

**Matching rules differ by source, because they have to:** a local cloning voice belongs to *one*
model (Qwen3 Base needs its own clip), while a provider's voices work with every TTS model that
provider offers.

### Local narration needs one extra step

Studio Hub's Qwen3 **Base** models are *cloning* models. A generation with no reference clip fails
outright, and a reference with a flat transcript fails with
`REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED` — it needs **timestamped** segments.

```bash
# 1. import voices (reference audio + transcript) from the old Voice Studio
.venv/bin/python scripts/import_voices.py

# 2. generate the required timestamped segments, using this Mac's own Whisper
.venv/bin/python scripts/annotate_voice_segments.py
```


Step 2 is not busy-work: the old Voice Studio stored only a flat transcript, so the timestamps are
produced by transcribing each reference clip locally. Both scripts are idempotent. The gateway then
stages each reference with the Hub once (cached 6 h, inside the Hub's 24 h asset TTL) and passes
`voice_reference_asset_id` on every generation.

Reference paths and transcripts are deliberately **never** sent to Story Studio — they stay inside
the gateway's voice records.

**A new provider model** — usually nothing. Discovery picks it up on the next refresh. For a model
the provider ships but does not list, use **Add model manually** in that provider's panel.

## Fleet — more than one location

Story Studio talks to exactly one endpoint, and this is it. So the list of *locations* lives here:
a location is another Mac (or site) running Studio Hub in **controller** mode, reached by its
address and its Hub service token. Its nodes belong to it and are reported by it — this gateway
never contacts a node directly, which is the topology Studio Hub's own plan specifies:

> "GenStudio connects once to each location's controller using its address/IP and Hub token. Each
> controller manages its joined nodes; nodes do not contact GenStudio directly."

**The local Mac is always the first location**, discovered the same way the rest of the gateway
discovers the Hub, so a single-Mac install shows something true with nothing configured.

The **Fleet** page (⌘6) shows, per location: whether it answers (with the reason when it does not),
its role and Hub version, each machine's chip, health, memory, free disk, job count, average
generation time and modality mix — and any work running right now.

Two rules it follows, from Studio Hub's own operational contract:

- **Unknown is not zero.** A metric the Hub did not report is shown as absent, never as `0`.
- **One dead location must not hide the others.** Reads fan out with a per-request timeout; an
  unreachable controller is reported as unreachable while the rest of the page still renders.

Add a location with its address and Hub service token. Tokens are stored `0600` in
`data/fleet.json`, are never returned by an API, and are masked in the UI. Adding the same address
again updates it rather than duplicating it.

The contract this depends on — and what it would like next from Studio Hub — is written up for the
other session in `~/Developer/_handoffs/2026-09-18-gateway-kh-fleet-contract-to-studiohub.md`.


## Verification

```bash
.venv/bin/python tests/test_e2e.py
```

110 checks against a **real HTTP upstream** (a local mock provider), exercising the admin API,
credential storage, live discovery, modality inference, the Story catalogue contract, chat/image/TTS
generation, transcription upload, the worker, artifacts, idempotency, provider/model enable-disable
on both the catalogue and the dispatch path, manual model addition, the voice registry, and the 402
invariant. Nothing inside the gateway is stubbed.

Bugs this caught that were invisible until it ran: modality inference had no generic `image`/`video`
rule (so `foo-image-v2` silently fell into the chat bucket), and the mock's `HTTPServer` stalled in
`socket.getfqdn()` *after* bind but *before* listen — which presents as a connect timeout, not a
refusal.

### UI tests — real clicks, not just layout

```bash
.venv/bin/python tests/test_ui.py
```

74 checks that drive the macOS **accessibility layer**: they open the window through the real menus,
click sidebar rows, read what it renders, type into fields, tick checkboxes and press buttons — then
verify the *gateway* changed, not merely that the widget repainted.

```
the sidebar is grouped like Studio Hub's        PASS
clicking a sidebar row selects the same section PASS
opens with a page heading like Studio Hub's     PASS
modality rows do not overlap                    PASS
typing filters the table (66 → 3)               PASS
ticking a voice reaches the gateway (12 → 11)   PASS
```

This is the layer that catches what the e2e suite cannot: a window that builds perfectly while a
button does nothing. It found five such issues — four in the test rather than the app, but each
looked exactly like a broken feature until diagnosed:

- an **accessory app is never frontmost**, so System Events sent keystrokes to whatever was — the
  search box stayed empty and looked broken;
- `keystroke` nested inside `tell process` resolves against the process and goes nowhere;
- with several windows open, the target window must be **raised**, or a web view behind the field
  swallows the keys;
- **button 1 of a window is not the close button.** The test meant to close Settings and instead
  clicked "Copy", so the next run started with two windows and every single-window check failed.
  Only content buttons sit at window level; the traffic lights are identified by
  `subrole is "AXCloseButton"`.

Because the first three are timing-dependent rather than deterministic, `type_into` now **reads the
field back and retries** instead of sleeping and hoping. A test that fails half the time is worse
than no test.

The fourth and fifth are the same story one level down: focusing **any** text field in this app makes
macOS create a nameless `AXDialog` alongside the window (the field editor / input method owns it, not
this app), and it is ordered *first* often enough that addressing `window 1` was a coin flip. When the
dialog won, every helper died with `Can't get splitter group 1 of window 1 ... Invalid index (-1719)`.
The suite now addresses `first window whose subrole is "AXStandardWindow"` and counts only standard
windows, so z-order stops mattering — and `osa()` retries that one transient error rather than
failing the run.

The layout checks are regression tests with numbers attached, not vibes: the modality bars were once
unreadable because the rows had no height constraint and collapsed into each other. The suite now
asserts that rows do not overlap and are evenly spaced.

Requires **Accessibility permission** for the terminal running it (System Settings → Privacy &
Security → Accessibility). Without it, `osascript` fails and the suite reports that rather than
pretending to pass.

### Verified against the real Studio Hub

Not just the mock. On this Mac, with Studio Hub 0.5.0 and 28 GB of local models:

| Lane | Result |
|---|---|
| Image (FLUX.2 Klein 4B) | real 971 KB PNG in **103 s** |
| Transcription (Whisper Base) | accurate transcript of a real clip in **22 s** |
| Narration (Qwen3 Base + cloned voice) | 7.3 s of real audio in **65 s**, non-silent (RMS 3906) |

Four bugs only the real Hub exposed: `hub-ready.json` lives under `StudioHub Standalone` (my
candidate list had plausible-but-wrong spellings), the Hub publishes its **token inside**
`hub-ready.json`, transcription uploads cannot be carried in JSON job params, and transcription
batches use `/api/hub/transcription/jobs` rather than `/api/hub/jobs`.

## Contract traps (each cost real debugging time to identify)

- **Never return 402.** Story Studio holds a credit lock keyed on the API key. If it ever sees 402
  and no balance is published, that hold **never clears** — the key is dead permanently. A provider
  running out of funds must surface as a settled `failed` job. `constants.py` says so explicitly.
- **`freshness` is a hard gate.** `stale: true` disables every model for new work, and a copy older
  than 30 minutes is refused outright. It is computed from the last catalogue rebuild, not from
  process start — otherwise the gateway would stop working 31 minutes after boot.
- **Artifact URLs must stay on the API origin.** Relative paths only.
- **`cost_tier` is all-or-nothing.** One row missing it hides the entire tier-tab strip.
- **Publish availability explicitly.** The transcription parser reads `available === true` (absent
  means unavailable) while reading `qualification_only !== false` (absent means hidden).
- **`chargeable` must be explicit.** The TTS quote parser does `payload?.chargeable === true`.
- **`paid_concurrency_limit` must be a valid integer** or the admission loader throws before
  falling back to its safe default.
- **`catalog_status` must be exactly `'published'`** on voices; anything else hides them.
- **`finish_reason` must be a clean stop.** `length` is refused as truncated, so it is passed
  through honestly rather than laundered.

## Security posture

- API keys live in `data/credentials.json` (mode `0600`, gitignored) or `.env` (gitignored).
  Secrets are never returned to the browser in full — the admin API returns a masked form.
- The server binds `127.0.0.1` by default. Admin routes are open from loopback and require the
  bearer token otherwise, so changing `GATEWAY_HOST` does not silently expose a control plane.
- `.env` and the credential file are created `0600` up front rather than chmod-ed afterwards, so
  there is no world-readable window.

## Layout

```
gateway/constants.py          the zero-valued commercial constants (read this first)
gateway/ports.py              the 3-method provider abstraction, lifted from GenStudio KH
gateway/config.py             paths, .env loading, secret-file hygiene
gateway/providers/
  definitions.py              the 18 providers, declarative
  credentials.py              0600 credential store, .env fallback, masking
  discovery.py                per-kind live model discovery
  models.py                   inventory: discovered + curated + user-added, with toggles
  registry.py                 composition
gateway/adapters/             openai_compat, anthropic, google, fal, kie, studio_hub
gateway/catalog.py            registry models -> Story Studio rows + freshness
gateway/worker.py             background execution
gateway/store.py              SQLite jobs, idempotency
gateway/api.py                the Story Studio contract
gateway/admin.py + admin_ui.html   provider console
gateway/fleet.py              location controllers: registry + live aggregation
gateway/hub.py                Studio Hub discovery + auth
data/voices.json              voice catalogue
data/providers.local.json     user-defined providers (gitignored)
tests/mock_upstream.py        local mock provider
tests/test_e2e.py             the end-to-end suite
tests/test_ui.py              drives the real app through the accessibility layer
menubar/main.swift            app shell: gateway supervision, status menu, launch agent
menubar/MainWindow.swift      the one window: grouped sidebar, status line, console sections
menubar/PageKit.swift         shared page furniture (heading / sectionTitle / empty), from Studio Hub
menubar/OverviewWindow.swift  Overview page, including the modality bars
menubar/JobsWindow.swift      Jobs page
menubar/VoicesWindow.swift    Voices page
menubar/SettingsSection.swift Settings page: two-column form for port, API key, startup
menubar/FleetSection.swift    Fleet page: locations, machines, running work
menubar/GatewayAPI.swift      authenticated admin client + formatting helpers
menubar/build.sh              compiles and installs the .app
menubar/make_icon.py          generates the app icon (stdlib only)
scripts/import_voices.py      imports voices from the old Voice Studio
scripts/annotate_voice_segments.py   generates aligned reference timestamps
```

### Why port 8010

Story Studio's default endpoint is `http://127.0.0.1:8000`, and the existing GenStudio KH API
already listens there. The gateway defaults to **8010** so the two can coexist — GenStudio keeps
serving the commercial side untouched. Point Story Studio at 8010 when you want the gateway, or
stop GenStudio and change the port in Settings to take 8000.

## Deliberately absent

Credits, customers, pricing, tiers, entitlement checks, publishing, model qualification/testing,
multi-tenancy, and API-key scopes. Scopes are ignored: this is a single-owner gateway, and Story
Studio only uses them to produce user-facing hints.

GenStudio KH is **not** modified by any of this and keeps running as-is.
