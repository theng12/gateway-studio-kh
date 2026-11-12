# Gateway Studio KH — current state

Rewritten 2026-11-12. This file describes only what is true now. Release-by-release history, gate receipts and superseded plans live in `git log` and in `_orchestra/gateway-lead/`; do not re-add dated status logs here.

## What it is

A macOS menubar app (`/Applications/gateway-kh.app`, bundle id `kh.gateway.menubar`) that runs a bundled Python gateway (`uvicorn gateway.api:app`) on port 8787. It serves providers and models to the other KH apps, holds the fleet view, and hands browser-extension jobs to Relay's worker contract.

## Where it runs

- **This Mac** — development and test installs.
- **1301** (`127.0.0.1`) — the gateway machine, 645 GB free, 2–3 parallel sessions available. Also the updater feed host.

## Versions

- **0.2.246** — current, installed here and on 1301, published to the feed (2026-11-12).
- Release builds come from `menubar/build.sh`; `dist/` keeps the **latest three** verified DMGs, older ones go to `_trash` after reference checks.

## Browser-extension workers (wire v1)

- Served by `gateway/extension_workers.py`; jobs handed to it by `adapters/chrome_extension.py`. Routes: `/api/workers/health|claim|progress|result|fail`, plus telemetry.
- **Switch:** `GATEWAY_EXTENSION_WORKERS`, read live from the environment, then `~/Library/Application Support/gateway-kh/.env`, then default. Off means every route answers 404.
- **On for 1301** (owner, 2026-11-12: "it is to be run on the 1301 mac … we can have 2-3 sessions running"). The earlier "this Mac only, never 1301" restriction is void.
- **Pairing:** Gateway → Providers → Browser workers → name + kind → Pair. Token shown once, `ekw_` + 43 chars, stored only as a sha256 digest in `extension-workers.json` (0600). Paste it into the extension with URL `http://127.0.0.1:8787` when the extension is on the same machine.
- **Pairing does not arm dispatch:** tokens mint dispatchEnabled false; dispatch is turned on per worker scope afterwards.
- Account observations and quotes must come from inside the signed-in page — the extension produces them, Gateway validates them, Relay only reads and dispatches.

## Configuration and data

`~/Library/Application Support/gateway-kh/` holds `.env`, `data/` (sqlite, artifacts, credentials), `extension-workers.json` and the menubar state file. None of it is inside the bundle, so an update does not disturb it — keep it that way.

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
