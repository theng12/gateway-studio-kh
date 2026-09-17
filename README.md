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
there is no Dock icon and no window until you ask for one.

**The menu:**

| Item | What it does |
|---|---|
| status line | live: provider and model counts, read from the gateway's own `/health` |
| **Open Provider Console…** | opens the browser page where you paste provider API keys |
| Start / Stop / Restart Gateway | supervises the service directly |
| Studio Hub status | whether your local runner is up, and a shortcut to start it |
| **Settings…** | gateway directory, port, API key, launch at login |
| Open data folder / Open log | where credentials and logs live |
| Quit | quits the menu bar app; **leaves the gateway running** |

**Settings** is where the gateway's own API key lives, with a **Copy** button — that is the value
Story Studio needs. Paste it into **Story Studio → Settings → This Mac → GenStudio API key**, and set
**Base URL** to `http://127.0.0.1:8010`.

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

**A new voice** — `tts_model_id` is matched as a suffix of the namespaced catalogue id, so voices
survive a provider being re-enabled.

Local narration needs one extra step, because Studio Hub's Qwen3 **Base** models are *cloning*
models. A generation with no reference clip fails outright, and a reference with a flat transcript
fails with `REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED` — it needs **timestamped** segments.

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

## Verification

```bash
.venv/bin/python tests/test_e2e.py
```

68 checks against a **real HTTP upstream** (a local mock provider), exercising the admin API,
credential storage, live discovery, modality inference, the Story catalogue contract, chat/image/TTS
generation, transcription upload, the worker, artifacts, idempotency, provider/model enable-disable
on both the catalogue and the dispatch path, manual model addition, and the 402 invariant. Nothing
inside the gateway is stubbed.

Bugs this caught that were invisible until it ran: modality inference had no generic `image`/`video`
rule (so `foo-image-v2` silently fell into the chat bucket), and the mock's `HTTPServer` stalled in
`socket.getfqdn()` *after* bind but *before* listen — which presents as a connect timeout, not a
refusal.

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
gateway/hub.py                Studio Hub discovery + auth
data/voices.json              voice catalogue
data/providers.local.json     user-defined providers (gitignored)
tests/mock_upstream.py        local mock provider
tests/test_e2e.py             the end-to-end suite
menubar/main.swift            the native menu bar app
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
