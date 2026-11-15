# Gateway Studio KH — current state

Rewritten 2026-11-12. This file describes only what is true now. Release-by-release history, gate receipts and superseded plans live in `git log` and in `_orchestra/gateway-lead/`; do not re-add dated status logs here.

## What it is

A macOS menubar app (`/Applications/gateway-kh.app`, bundle id `kh.gateway.menubar`) that runs a bundled Python gateway (`uvicorn gateway.api:app`) on port 8787. It serves providers and models to the other KH apps, holds the fleet view, and hands browser-extension jobs to Relay's worker contract.

## Where it runs

- **This Mac** — development and test installs.
- **1301** (`127.0.0.1`) — the gateway machine, 645 GB free, 2–3 parallel sessions available. Also the updater feed host.

## Versions

- **0.2.257** — current: built from `980379b`, accepted, installed here (2026-11-15). Published
  to the 1301 feed pending the Developer's publish + readback (this Mac has no ssh to 1301).
- **0.2.256** — previous **published** build, kept in `dist/` beside 0.2.257. Its released
  artifact is `2cd548aea731-gw-0256.dmg` (sha `2cd548ae…`, 63,254,947 bytes, 2026-11-13); the
  e0d4232 candidate of the same version was never published and sits in `_trash/dist/`.
- **1301 login item:** the bundled `SMAppService.agent` plist is correct (`RunAtLoad`, `KeepAlive
  SuccessfulExit=false`), but the registered record is a **partial import** (`EX_CONFIG 78`,
  `spawn failed`) — a reboot leaves the gateway down until `launchctl kickstart`. Fix at a quiet
  point by toggling launch-at-login off/on in the app and reboot-testing; see
  `_orchestra/gateway-lead/release-0257-20261114/1301-login-item-finding.md`.
- **1301 route:** SSH and the feed answer over the LAN at `terranash-1301-m1-16-1000`
  (192.168.0.105) with `~/.ssh/id_ed25519_gateway1301`; the `100.x` IP and the HTTP feed port
  time out even when Tailscale is off. Tailscale is not a prerequisite.
- Release builds come from `menubar/build.sh`; `dist/` keeps the **latest two** verified DMGs,
  older ones go to `_trash` after reference checks.

## 0.2.257 — what changed (source `0cedfe5..980379b`)

- **Models** opens on *Your models* (per-kind cards, offered/installed counts) with *Discover*
  as the catalogue; one row per model; `N of M` + Macs behind `…`; providers listed as
  providers (Hub Studio KH (local) one entry), Macs as a locations filter.
- **Voices**: real provider, `N of M locations`, tone/best-for/sample length/id in Details.
- **Create** draws the selected model's own options: image ratios/sizes with the size's price
  and a seed, video variants, music instrumental + lyrics count, TTS temperature where declared,
  Saved voice | Provider voice ID as one decision, and Generate disabled with the missing line.
- **Cloud image geometry** labels are the pixels really sent (3:2/1:1/2:3); legacy names still
  resolve to the same pixels.
- **Footage library** (Content Packs): `<sha256>` bytes + sidecar + `index.json` under a root
  chosen with a folder picker; measured length/size/licence, quota, free stock (Wikimedia on,
  Pexels/Pixabay key+switch), free AI stock from the fleet's t2v; Story reads `/v1/footage/*`.
- **Video catalogue** `/v1/videos/models` with sources, price evidence and live eligibility.
- **Dispatch**: eligibility vocabulary, stale-heartbeat offline, 30 s
  `DISPATCH_NO_ELIGIBLE_MACHINE` with reasons, `/upstream` before a Hub batch, cancelled
  who/why and a fresh-job retry.
- **Image pools** publish only ratios with options, one default.
- **The old Story Studio update/monitoring tab is removed** (Fleet Stories tab and
  `/admin/api/story/instances`); `/v1/apps/story/checkin` stays for the Content Pack removal
  report.
- **Fleet > Machines**: grouped live filters (State/Issues with counts, Version, Network,
  Location), chips + Reset, sortable columns with `aria-sort`, a fleet-wide warning summary
  with "Show warning kinds"/"Show Macs with issues", wrapping cause text, stacked rows at 900.
- **Jobs**: Location and Serving machine are separate; the serving Mac is the Hub's own
  machine from success/failure evidence, with honest queued/running/cloud states and concise
  Created clocks.
- **Pool failures**: a mixed pool reports `POOL_EXHAUSTED` with every member cause (not the
  last member's auth code); the OpenAI-compatible lane records `max_tokens_asked`/`sent` with
  the finish reason and usage. Story contract: `pool-failure-contract-0257.md`.

## What 0.2.257 learned

- The JS console suite is one process per file; a single multi-file run shares globals.
- The studio-gate COMPLETENESS check was broken in the shared scanner (macOS `find` rejects
  grep's `--exclude-dir`), fixed in `Shared Tools/studio-orchestration` `efaeb83`.
- A model can be ticked on a keyless provider; `/create/models` returns `not_ready(_detail)`
  and Create names the key.
- Keyboard: a render that replaces a focused button must re-focus it; a dialog opened from a
  menu returns focus to the menu trigger; an expanded Fleet detail closes on Escape.
- The publish needs 1301 access (no ssh from this Mac); the feed write and readback are the
  Developer's step.

## Browser-extension workers (wire v1)

- Served by `gateway/extension_workers.py`; jobs handed to it by `adapters/chrome_extension.py`. Routes: `/api/workers/health|claim|progress|result|fail`, plus telemetry.
- **Switch:** `GATEWAY_EXTENSION_WORKERS`, read live from the environment, then `~/Library/Application Support/gateway-kh/.env`, then default. Off means every route answers 404.
- **On for 1301** (owner, 2026-11-12: "it is to be run on the 1301 mac … we can have 2-3 sessions running"). The earlier "this Mac only, never 1301" restriction is void.
- **Pairing:** Gateway → Providers → Browser workers → name + kind → Pair. Token shown once, `ekw_` + 43 chars, stored only as a sha256 digest in `extension-workers.json` (0600). Paste it into the extension with URL `http://127.0.0.1:8787` when the extension is on the same machine.
- **Pairing does not arm dispatch:** tokens mint dispatchEnabled false; dispatch is turned on per worker scope afterwards.
- Account observations and quotes must come from inside the signed-in page — the extension produces them, Gateway validates them, Relay only reads and dispatches.

## Configuration and data

`~/Library/Application Support/gateway-kh/` holds `.env`, `data/` (sqlite, artifacts, credentials), `extension-workers.json` and the menubar state file. None of it is inside the bundle, so an update does not disturb it — keep it that way.

**After any update on 1301, re-check the linkage in three commands:** `/api/workers/health` answers 401 without a token (the switch is on) rather than 404; `kh.gateway.menubar` is loaded and running the installed bundle; and Relay's `settings.json` still reads `gateway_url: http://127.0.0.1:8787`. An update replaces the bundle, so a launch agent or login item that hard-codes a path is what breaks — the agents must self-heal to the running bundle, not trust that a plist exists.

## Invariants

- **Never take the owner's desktop:** no foreground launches, no synthetic input, hidden launches with `open -g -j`.
- **Every app releases everything on quit and on delete:** helper stopped, launchd job unloaded, port freed, no orphan; a second launch reports and stays behind; uninstall removes app, helper, launchd job and login item, keeping data.
- **No orphan adoption:** the app must never keep serving a gateway another build started.
- **No paid generation** without a go that names it; local models are off on this Mac until restored from the SSDs.
- **Build caches are kept** (`target/`, `.build/`, `node_modules/`, Go cache, `_graphs`).

## Pointers

- Contracts and invariants: `AGENTS.md`.
- Release evidence and receipts: `_orchestra/gateway-lead/`.
- Worker contract counterpart: Relay (`relay-studio-kh`), which also owns the three Chrome extensions.
