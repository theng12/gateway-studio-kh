# Gateway Studio KH — current state

Rewritten 2026-11-12. This file describes only what is true now. Release-by-release history, gate receipts and superseded plans live in `git log` and in `_orchestra/gateway-lead/`; do not re-add dated status logs here.

## What it is

A macOS menubar app (`/Applications/gateway-kh.app`, bundle id `kh.gateway.menubar`) that runs a bundled Python gateway (`uvicorn gateway.api:app`) on port 8787. It serves providers and models to the other KH apps, holds the fleet view, and hands browser-extension jobs to Relay's worker contract.

## Where it runs

- **This Mac** — development and isolated tests; no installed Gateway is claimed here.
- **1301** (`127.0.0.1`) — the live gateway and updater feed host. Intended-host updates belong to the Launcher's safe automatic install path; never restart live work to manufacture proof.

## Versions

- **0.2.274** — 2026-11-18. **Token usage on every job, whatever the outcome.** `_job_envelope`
  publishes `usage` (the lane's own figures) and `usage_source: "provider"` for a failed, cancelled,
  filtered or held job, from the evidence the adapters now always attach
  (`openai_compat`/`anthropic`/`studio_hub` set `evidence["usage"]` on a filter stop or truncation;
  a window no longer hides it). Unreported is `null`, never 0. A pool walk records one entry per
  member in `attempts`/`pool.attempts` with that member's `usage` or `null`, published as
  `pool_usage`. `verified_token_usage` keeps its meaning (passed through, not re-measured) and now
  sits beside `usage_source`. The Jobs page gains a **Tokens** column: the job total, and a line per
  attempted pool member, with "Not reported by the provider" where a lane stated nothing. Tests
  `tests/test_token_usage.py` (6) and `tests/test_jobs_evidence_view.js`.
- **0.2.273** — 2026-11-18. Three Production-confirmed fixes. **A pool job gets one attempt**
  (`concurrency.DEFAULT_POOL_MAX_ATTEMPTS = 1`, editable as `retry.pool_max_attempts`; the row
  prints `attempts_allowed: 1`): its chain already walked every member, so replaying it three times
  paid every provider again — eight `POOL_EXHAUSTED` chat jobs on 1301 today. **A member's own
  content filter advances the pool**: `BackendError.refusal_scope="response"` is set by the
  openai-compatible and Anthropic adapters when the provider refused its own answer, and
  `adapters/pool._stops_chain` now walks past it (recording member + code + detail on the attempt);
  `INVALID_REQUEST` and any unmarked refusal about the request still stop the walk. **The Jobs
  window's Preview goes through an injectable opener** (`JobsSection.openFile`), so the native test
  no longer hands an `.mp3` to LaunchServices — it launched Music in the foreground during a gate
  run. The shipped app keeps `NSWorkspace.shared.open`.
- **0.2.272** — 2026-11-18. Per-job retry (`POST /admin/api/jobs/{id}/retry` and the Jobs page's
  **Retry** button). Takes one terminal job id and queues a fresh job carrying the same request,
  params, routing, group, priority and requester, with `retry_of: <id>` published on the new row and
  in `/admin/api/jobs`. Refuses a non-terminal or still-held row (`409 JOB_NOT_TERMINAL` /
  `JOB_STILL_HELD`) with a plain sentence; never reopens or mutates the old row; counts the retry as
  a new job.
- **0.2.271** — 2026-11-18. A job now ends when its upstream stops progressing. A batch the Hub
  reports as stalled ends at the first stalled answer (`UPSTREAM_STALLED`), naming the seconds and
  its threshold, with the batch recorded for the cleanup; a `running` row with an upstream batch
  that nothing in the process is polling is ended by the hold sweep (same thread and 60 s timer)
  once past `BATCH_NO_POLL_CEILING_S` (7,200 s + grace), after one Hub read — collected if it had
  finished, `UPSTREAM_UNPOLLED` otherwise, retryable only when the Hub never heard of the batch.
  A re-follow no longer restarts the window: the follow loop stops once the Hub's own `run_seconds`
  reaches the lane ceiling. The sweep's counters are published as `wedged_sweep` in `/health`.
- **0.2.270** — 2026-11-18. Removing a Mac or a location now removes every Gateway record of it
  (roster, credential, models, reachability history, Lite Mode settings, render opt-in, image pins,
  network names, limit overrides, jobs, summaries, review locators, render rows and artifact files),
  and a removed snapshot is scrubbed so the page stops drawing it. One Mac can be removed on its own
  and a Mac can be connected back. **The Hub half is not in this release**: the Gateway speaks the
  contract at `_orchestra/gateway-lead/evidence-node-removal-20261118/DESIGN.md` §3 and names
  `HUB_TOO_OLD` / offers `can_force` until a Hub release implements unregister / `NODE_REMOVED` /
  `nodes/connect`. Also in this release: a hold whose Hub keeps answering "running" past the 4h2m
  ceiling now ends as `HOLD_EXPIRED`, and the free-space warning carries the time it was measured.
- **0.2.262** — source prepared, not yet built or published at this checkpoint. The Launcher owns
  `~/Library/KHUpdater/state/hold-token` (0600, 64 hex) and atomically replaces it; Gateway reads it
  per request. Only direct-loopback POST/DELETE `/admin/api/maintenance/drain` accepts it, via
  `X-Hub-Token` and/or matching Bearer. Missing/invalid/replaced credentials, Origin, forwarded
  headers, cookies and credential query parameters are refused before queue changes. No Gateway
  issuance, enrollment, verifier, generation or rotation exists. Publish this release with
  `HOLD_URL=http://127.0.0.1:8787/admin/api/maintenance/drain`; read the field and artifact hash back.
  The Launcher owner also needs that exact admin path in its URL allowlist and a test proving a
  refused hold starts no download, quit or replacement. Gateway's focused checks: 55 auth tests
  plus 11 drain/restart tests; this is not packaged or intended-host update proof.
- **0.2.261** — published from `01f1b32`, build `01f1b32-202611160820`, SHA-256
  `ff62b3632df229226332a6d20f8a6e6f36da217032b1bf3cfb3883f3d344f1f2`, 61,535,461 bytes.
  Served bytes independently hash identically. Gate: 248 files, 3,728 unit tests and 586 e2e checks.
  Its feed entry lacks `holdURL` and remains immutable; do not rebuild, republish or edit its metadata.
  Receipt: `_orchestra/youtube-stack/release-261/release-receipt.json`. Installed acceptance remains
  pending; an unauthenticated drain call must refuse even on loopback, and the Launcher holds the
  private token required by this release's code.
- **0.2.260** — previous feed release; last intended-host measurement on 1301 still named build
  `10289af-202611150645`. Installation of 0.2.261 was not verified. Retained 0.2.259/0.2.260 installers,
  source, caches and rollback evidence stay untouched under the owner's stable-version hold.
- **1301 login item:** the bundled `SMAppService.agent` plist is correct (`RunAtLoad`, `KeepAlive
  SuccessfulExit=false`). The broken `partial import`/`EX_CONFIG 78` record was replaced on
  2026-11-15 by `launchctl bootout` + `bootstrap` of the bundled plist; `/health` stayed up.
  Remaining proof (quiet point only): fully quit the app or reboot 1301 and confirm `/health`
  without a kickstart. See `_orchestra/gateway-lead/release-0257-20261114/1301-login-item-finding.md`.
- **1301 route:** the live Gateway answers at `http://127.0.0.1:8787`, and the authoritative
  feed at `http://127.0.0.1:8790/release.json`. Use the existing pinned publisher SSH identity;
  do not copy credentials between apps or change network settings.
- Release builds come from `menubar/build.sh --no-install` and stage exact candidates under
  `dist/.candidates/`. Preserve retained stable installers and caches; no automatic cleanup here.

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
