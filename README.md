# gateway-kh

**Moving to another computer or starting implementation?** Read the current [new-computer handoff](_handoffs/2026-09-20-new-computer-handoff.md) and [open-issue ledger](_handoffs/2026-09-20-open-issues.md). They distinguish verified defects, proposed features, committed code and live state.

The [handoff completion plan](docs/superpowers/plans/2026-09-20-handoff-completion.md) preserves the intended implementation and acceptance checks. The [current execution record](_handoffs/2026-09-20-local-completion.md) records what passed, what is installed and what remains blocked.

For voices, settings and Content Packs, see the [asset transfer inventory](_handoffs/2026-09-20-asset-transfer.md). Owner-private transfer files are separate from the source repository.

The owner is keeping the source Mac available for content work while developing on the new Mac. Read the [fixed production-build notes](_handoffs/2026-09-20-this-mac-production.md) before replacing the source Mac's app or runtime data.

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

**Cloud APIs and local runners are separate things.** A cloud provider is a key and a model
list; a local runner is a *machine* with studios, work and a state. The **Providers** page is now
cloud-only — 17 vendors — and the local Hub lives on **Fleet**, where machines and running work
belong. The split is decided by the provider's `kind`, not by its name, so a second Hub is
classified correctly the moment it is added, and `/admin/api/providers?scope=cloud|local` makes
the distinction explicit for anything else that reads the API.

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
keyless development accepts a nonempty bearer token only when configured for loopback. A
non-loopback bind requires a configured key; keep Uvicorn's actual bind aligned with `GATEWAY_HOST`.
Launching with `--background` (which is what the login item does) skips the window and leaves only
the menu bar icon.

**Settings** is where the gateway's own API key lives, with a **Copy** button — that is the value
Story Studio needs. Paste it into **Story Studio → Settings → This Mac → GenStudio API key**, and set
**Base URL** to `http://127.0.0.1:8787`. It is the last item in the sidebar, under "Library & app".

**Launch at login** uses a LaunchAgent (`~/Library/LaunchAgents/kh.gateway.menubar.plist`) rather
than `SMAppService`, because `SMAppService` requires a signed, registered bundle and this app is
ad-hoc signed. The agent is `KeepAlive` only on an unsuccessful exit, so quitting the app
deliberately stays quit.

The app **supervises** the gateway: if the process dies it restarts with exponential backoff, and
the menu says so. Quitting stops the gateway by default. Enable **Keep the gateway running after
I quit** in Settings only when you want Story Studio to keep using that process after the app closes.

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
GATEWAY_API_KEY=your-key .venv/bin/python -m uvicorn gateway.api:app --host 127.0.0.1 --port 8787 --no-proxy-headers
```

Then open **http://127.0.0.1:8787/admin** for the provider console.

For a network bind, set `GATEWAY_HOST` to the same address as `--host` and configure
`GATEWAY_API_KEY`. Admin requests from a non-loopback peer require that key even
if `GATEWAY_HOST` was mistakenly left at its default. Proxied requests also
require authentication; forwarded headers never grant the loopback bypass.

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
address and its **Fleet token**. Its nodes belong to it and are reported by it — this gateway
never contacts a node directly, which is the topology Studio Hub's own plan specifies:

> "GenStudio connects once to each location's controller using its address/IP and Hub token. Each
> controller manages its joined nodes; nodes do not contact GenStudio directly."

(The plan uses "Hub token" for what this gateway calls the **Fleet token** — the work badge it
asks for, sufficient for everything here and unable to administer the controller. The controller's
own **Hub token** is needed only to manage shared voices. See
`docs/credential-rotation-decision.md`.)

**The local Mac is always the first location**, discovered the same way the rest of the gateway
discovers the Hub, so a single-Mac install shows something true with nothing configured.

The **Fleet** page (⌘6) answers three questions: *how many machines are online*, *which machine did
the work*, and *what is running right now*. Its first tab, **Groups**, is one card per controller
group: a needs-attention banner, then every node with its state, what it is doing, free disk and
memory (amber under 12 % disk or 20 % memory, red under 6 % or 10 %), and what it starts at login.

It is built to hold **hundreds** of locations and machines:

- three **tables** (locations, machines, running work), so only the visible rows are built;
- controllers probed **concurrently** in a bounded pool, not one after another;
- a short-lived cache on the read, plus a **background refresher** on a timer, so no request ever
  waits on an unreachable site — an off controller costs the poller time, not the person looking
  at the page. Measured: 200 controllers probed in 0.55s, and a cached read in 0.0ms.

The counts come from the Hub's own `fleet_activity` report — `working`, `just_finished`, `ready`,
`long_idle`, `offline`, `needs_attention` — rather than being re-derived here, because only the Hub
knows the difference between idle and gone. Per machine it shows the state, jobs **completed** and
**failed**, median runtime, chip, health, memory, free disk and modality mix. The header reads
`1 of 2 locations reachable · 3 machines online, 1 offline · 12 jobs completed, 1 failed`.

That data is read from `/api/hub/stats`, not `/api/hub/fleet/overview`: the overview only knows
reachable/unreachable, so it cannot say which machine ran what. Where the Hub reports that it could
not measure something, the page repeats its own words — the local machine currently says
*"Direct activity partially unavailable"* — instead of implying a confident number.

Two rules it follows, from Studio Hub's own operational contract:

- **Unknown is not zero.** A metric the Hub did not report is shown as absent, never as `0`.
- **One dead location must not hide the others.** Reads fan out with a per-request timeout; an
  unreachable controller is reported as unreachable while the rest of the page still renders.

**Standalone and controller both work, and that is deliberate.** A new machine can be added while
it is still standalone and keep working after it is promoted to a controller — nothing in the
gateway reads the role except to display it. This is checked end to end by
`tests/test_fleet_standalone.py`, which adds a *standalone* Hub as a location, discovers its models
through the fleet provider path, and transcribes a real clip on it.

**A location is also a provider.** Adding one does not just add a dashboard row: it becomes a
`studio_hub` provider, so its models are discovered, can be enabled, and reach Story Studio's
catalogue like any other — and work is dispatched to it through the same adapter as the local Hub,
which already takes a per-provider address and token. Its models are **opt-in**, like every provider
except the local Hub, so adding a site never silently publishes its whole model list.

Add a location with its address and Fleet token. The token is stored in the **credential
store** (`data/credentials.json`, `0600`) under the provider id the location maps to — one secret
store, not two that can drift. `data/fleet.json` holds only the roster: id, name, address. Tokens
are never returned by an API and are masked in the UI, and adding the same address again updates it
rather than duplicating it.

The contract this depends on — and what it would like next from Studio Hub — is written up for the
other session in `~/Developer/_handoffs/2026-09-18-gateway-kh-fleet-contract-to-studiohub.md`.


## Verification

```bash
.venv/bin/python tests/test_e2e.py            # isolated mock upstream
.venv/bin/python tests/test_ui.py             # drives the installed app
```

The real-Hub fleet suite is **not** an offline smoke test. Only after explicitly
authorizing real generation, run
`GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python tests/test_fleet_standalone.py`.
Without that opt-in it skips before contacting the Hub.

### Recovery, outputs and private pack archives

Interrupted running work is marked with an explicit unknown-outcome recovery
state, with the upstream job ID when an asynchronous provider returned one.
It is never automatically resubmitted. Check the provider before creating new
work; provider-specific reconciliation is not implemented. Queued cancellation
is confirmed locally; already-dispatched work can only receive a cancellation
request, and a late output remains visible in Jobs.

The six Create pages (Audio, Subtitle, Image, Music, Video, Chat) share one layout,
`CreatePageLayout` in `menubar/PageKit.swift`, after Studio Hub's generator pages: the form
column on the left, the result on the right filling the rest of the window. The result is
previewed in place (`menubar/CreatePreview.swift`): an audio player with waveform, a large
image with thumbnails, an inline video player, a timestamped transcript, a conversation.

Create retains drafts, selected jobs and ambiguous submission identities across
restarts (a saved job the gateway answers 404 for is dropped, not shown). Retrying an ambiguous submission uses its original payload and key;
changed or missing upload bytes cannot silently become that original request.
Jobs lists all output variations, with authenticated local-file downloads and
pin/unpin controls. Retention protects inputs referenced by unfinished or
recovery-held jobs and refuses pruning if their protection cannot be read.

#### Content Packs: author, review, shelve, download

Open **Content Packs** in the native app, then open its browser page. Gateway
is the private workshop and shelf; Story Studio discovers shelved packs through
its existing Gateway URL and downloads/installs only when you choose it. No
second Store server or model-provider key is needed for content delivery.

Enable the private shelf **before reserving new pack identities**, then use
the page's authoring brief in a dedicated session. The
[private authoring guide](docs/content-packs-authoring.md) describes that handoff.
An imported `.sspack` is preserved first; **Add to shelf** is a separate,
explicit action that verifies the real Story schema and registry receipt and
requires acknowledgement of authorship/distribution rights. Draft export alone
does not publish. Versions are immutable: retries preserve identical bytes,
and changes require a new reserved version.

This delivery version supports **Series Formula, Topic Pack, Visual Style and
Thumbnail Recipe**. Other standalone kinds require a coordinated Story delivery
update; Gateway refuses them rather than advertising an unusable pack.

Setup creates a new private owner account and signing identity, with a snapshot
of historical registry identities. Only newly reserved eligible identities may
be shelved. Back up the private shelf identity together with its releases; a
missing or damaged identity is a recovery error, not an invitation to generate
a replacement. Story keeps its normal account/origin-bound signatures,
OS-backed key anchors, compatibility checks and installer. There is no checkout:
private-owner entitlements have no purchase ID and no invented purchase receipt.
Quotes and purchases remain unavailable; an unconfigured shelf reports
`CONTENT_STORE_NOT_CONNECTED`.

**Preserved archives are separate from shelved releases.** Archive safety and
SHA-256 checks alone do not establish ownership or make historical Store packs
installable. Existing Store packs retain their original trust and licenses;
the new private authority cannot restore, redownload or update them. Do not
re-sign, rename or reissue transferred packs to bypass that boundary. Existing
installed packs still require their original valid offline trust. Keep original
transfer copies, including legacy bundle-only archives that the conservative
standalone archive importer may reject.

#### Private Topic Pack drafts and candidates

The Content Packs page's **Drafts** section uses a local Story Studio
source checkout's actual registry, schema and archive builder. It requires Node
and that checkout's locked dependencies (`npm ci --ignore-scripts --no-audit
--no-fund` in the Story checkout). Set `GATEWAY_STORY_SOURCE` in Gateway's local
`.env` if the checkout is not at `~/Developer/story-studio-kh`, and
`GATEWAY_AUTHORING_NODE` if Node is not available on PATH or at
`/opt/homebrew/bin/node`. These are local prerequisites, not provider API keys.

1. Obtain a reserved Topic Pack identity/version through Story Studio's existing
   authoring-lead registry workflow (`scripts/content-packs/README.md` in that
   checkout). Gateway reads and verifies that reservation; it does not create
   product identities or modify the registry. No reservations produces an
   explicit prerequisite message, not an invented pack.
2. Choose the reservation and optionally an authored Topic Pack template, then
   create a private draft. The template is copied, never edited in place.
3. Edit and save the JSON document. Unfinished JSON can be saved; preview and
   export require a valid document matching the reserved identity. Revision
   checks refuse stale saves instead of overwriting newer work.
4. Validate and preview, then export a candidate `.sspack`. The first successful
   export freezes this version; download retries return the same archive. To
   change an exported candidate, use a new registry reservation/version.

An interrupted export can freeze the reserved version before the download
succeeds. If either the saved hash metadata or its exact candidate bytes are
missing or damaged, Gateway refuses recovery rather than rebuilding different
bytes under that identity. Keep the private draft and use a new reservation/version;
do not delete candidate metadata to force another export.

Adding a draft to the shelf also exports an immutable candidate first. A later
publication refusal does not undo that export or discard its bytes; inspect the
refreshed draft/candidate state before deciding whether a new version is needed.

Drafts and candidates live in private `DATA_DIR/content-authoring`, separate
from the imported archive shelf and transfer sources. Saved drafts remain
readable without the authoring tools. Export uses the Store delivery profile
without a publisher private key: **structural validation is not a signed Store
receipt, ownership, stocking, publication or installation**. Story Studio's
supported trust and installation checks are still required. No generation runs
as part of authoring.

Optional local integration checks, using only a temporary synthetic registry:

```bash
node --test tests/test_story_authoring.mjs
.venv/bin/python tests/test_pack_authoring_integration.py
.venv/bin/python tests/serve_ui_fixture.py --authoring
```

`scripts/relocate_profile.py --help` documents a dry-run-first migration for
copied profiles. Do not replay archived job history or replace a live profile
wholesale. Keep voice recordings and pack libraries outside Git.

The end-to-end suite uses a **real HTTP upstream** (a local mock provider), exercising the admin API,
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

The UI checks drive the macOS **accessibility layer**: they open the window through the real menus,
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

## Knowing whether Story Studio is actually talking to you

Story Studio's credential is encrypted inside its own app data, and this gateway logs at warning
level, so "is it connected?" used to be unanswerable from outside — a real question, asked for real,
when Story Studio's cached catalogue did not match what this gateway serves.

`GET /admin/api/status` now reports `story_studio`: the last calls on the Story surface, with their
status and **whether a bearer token was presented** (never which one). Set the endpoint and key in
Story Studio, then read it:

```
GET /v1/account/overview -> 200  authenticated: true     # connected
GET /v1/models           -> 401  authenticated: false    # key missing or wrong
(nothing)                                                # never called
```

### Publishing what is known, and nothing else

Story Studio's own rule — *never let "the answer is none" and "I could not work out the answer"
produce the same value, and never invent a number the customer will mistake for the model's own* —
turned out to be the most useful test to run against this catalogue. Applying it found, across
several passes:

| Was published | Why it was wrong | Now |
|---|---|---|
| `context_window_tokens: 0` (37 rows) | reads as "accepts nothing", not "unknown" | `null` |
| `max_output_tokens: 8192` for every model | a model allowing 131,072 was described as 8,192; providers publish the real figure and discovery was discarding it | per model, `null` if unstated |
| no `provider_name` on video rows | provider reads blank | published |
| transcription `provider` only | same field, wrong key | `provider_name` (plus `provider`) |
| transcription `language: "en"` + `auto_detect: true` | self-contradictory; Whisper is multilingual | `"multilingual"` sentinel |
| TTS `["{'language_id': 'en', …}"]` | stringified Python dicts in a `string[]` field | 32 real codes |
| TTS/Voices `["en"]` when unknown | claims English-only | `[]` / `"multilingual"` |

**And one value that must stay invented:** `maximum_input_characters`. No provider states it for
every lane, but Story Studio calls `.toLocaleString()` on it unconditionally, so `null` would crash
the page rather than degrade it. Whose number it is now travels with it in
`maximum_input_characters_source`: `provider` where the model states one, and otherwise the local
lane's single **15,000** (`catalog._TTS_CHARACTERS_LOCAL` — chunking is Studio Hub's job) or the
cloud lanes' 25,000. This paragraph said 40,000 for a while after the catalogue had stopped
publishing it, which is the drift the companion field exists to make impossible.

### Curated limits, where a provider publishes none

Six lanes state no limits at all — Anthropic's `/v1/models` returns only
`id/display_name/created_at/type`, DeepSeek's returns `id/object/owned_by`, and OpenAI's and
NVIDIA's return non-JSON. Publishing `null` is honest but it is not the same as knowing, and the
owner does know these models. That knowledge lives in a small file outside the code:

```
~/Library/Application Support/gateway-kh/data/model-limits.local.json
```

```json
{ "models": { "anthropic-direct::claude-opus-5": {
    "context_window_tokens": 1000000, "max_output_tokens": 128000 } } }
```

**It fills gaps; it never overrides.** A value a provider publishes always wins, because live data
is current and a file goes stale — verified by a test that asserts both directions. An absent file,
absent entry or unreadable file all mean "no opinion", which is the same `null` as before, so
nothing here can make the catalogue less honest. Set `GATEWAY_MODEL_LIMITS` to point elsewhere.

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
- The server binds `127.0.0.1` by default. Admin routes are open only for direct loopback
  requests while the configured bind is loopback. Other peers and proxy-marked requests
  require a bearer token, including when the actual socket bind differs from configuration.
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
menubar/PageKit.swift         design tokens (DS, TextRole, Health) and shared page furniture (heading, cards, badges, buttons)
menubar/OverviewWindow.swift  Overview page, including the modality bars
menubar/JobsWindow.swift      Jobs page
menubar/VoicesWindow.swift    Voices page
menubar/SettingsSection.swift Settings page: two-column form for port, API key, startup
menubar/FleetSection.swift    Fleet page: locations, machines, running work
menubar/FleetGroups.swift     Fleet > Groups: one card per controller group, with its nodes
tests/test_fleet_standalone.py   proves a standalone Hub works as a location
menubar/GatewayAPI.swift      authenticated admin client + formatting helpers
menubar/build.sh              compiles and installs the .app
menubar/make_icon.py          generates the app icon (stdlib only)
scripts/import_voices.py      imports voices from the old Voice Studio
scripts/annotate_voice_segments.py   generates aligned reference timestamps
```

### Why port 8787

The gateway listens on **8787**, the default in both `gateway/config.py` and the app's
`AppConfig`, and every Story Studio client points at `http://<host>:8787`.

It was 8000 originally because that is Story Studio's *default* `genstudio_url` — no
configuration needed. The owner moved it to **8787** (2026-09-21, `Project Status.md`
§10h): 8000 is the most contested dev port and is also Story's own default, which kept
"is this pointed at the right thing?" permanently ambiguous. Story Studio clients set
the Base URL to `http://…:8787`.

Earlier still it was 8010 while the older GenStudio KH API held 8000. That API is now
**stopped and disabled** (2026-09-18): its LaunchAgent carried `KeepAlive: true`, so a
plain `kill` restarted it within seconds and only `launchctl disable` made it stay down.

The port is still configurable in Settings, and `tests/test_ui.py` reads the app's configured port
rather than assuming one.

## Deliberately absent

Credits, customers, pricing, tiers, entitlement checks, publishing, model qualification/testing,
multi-tenancy, and API-key scopes. Scopes are ignored: this is a single-owner gateway, and Story
Studio only uses them to produce user-facing hints.

This project does not modify GenStudio KH. Manage its service status separately and check that it is not competing for the gateway port.
