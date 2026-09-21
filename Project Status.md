# Project Status — gateway-kh

Durable recovery, execution and handoff record. Written and maintained
2026-09-21 after the previous orchestrator session failed. `[MEASURED]` = ran or
observed in this recovery session; `[INHERITED]` = documented by an earlier
session, not independently re-verified; `[UNKNOWN]` = not yet established.

## 1. Project purpose and architecture

A personal **model gateway** that presents one GenStudio-compatible API
(`http://127.0.0.1:8000`) in front of local Apple-Silicon Studio Hub runners and
~18 cloud providers, so **Story Studio KH** talks to a single endpoint with no
code changes. It has grown a second capability: a **private Content Pack
workshop and shelf** that authors, qualifies and serves `.sspack` packages
through Story Studio's real installer.

- Backend: Python 3.11 / FastAPI (`gateway/`), SQLite job store + worker thread.
- Native shell: Swift / AppKit menu-bar app (`menubar/`), installed to
  `/Applications/gateway-kh.app`, opens the browser admin page.
- Node bridges: Story authoring/validation tooling (`gateway/*.mjs`).
- Tests: isolated Python suites plus Node VM and compiled-Swift fixtures.
- Contract traps and working rules: `AGENTS.md` (updated this session with the
  private-publisher rules) and `README.md`.

## 1b. Two-instance workflow (owner-facing) — added 2026-09-21

- **Working instance** — what Story Studio uses: `/Applications/gateway-kh.app`,
  **promoted 2026-09-21 to build `c74ae22-202609211712`** (regular Dock app;
  earlier `c50c05e-…`, `df2566c-…`, `e14d19e-…`, `97bb3a5-…`, `6ed4c44-…`,
  `5f94b4a-…`), port **8787** (was 8000), bound `0.0.0.0`, real profile
  `~/Library/Application Support/gateway-kh/`. Signature valid; profile verified
  byte-identical across promotion; single rollback at `backups/rollback-20260921/`.
- **Test instance** — where new work is verified: `test-instance/` in the
  container root, source `worktrees/handoff-completion`, port **8100**, isolated
  profile (`test-instance/support`, `test-instance/data`), no real credentials,
  no Studio Hub. Commands: `./run.sh`, `./status.sh`, `./stop.sh`; see
  `test-instance/README.md`.
- **Owner decision 2026-09-21:** the test instance stays **code-only** (no keys,
  no Hub) so it can never spend money or touch the owner's Hub; it is for code
  and UI verification only. The browser admin page is enough — no second Dock
  app. The working instance is the only place real generation runs and it changes
  **only when the owner explicitly says to promote**.
- Story Studio talks to exactly one endpoint; switching it to 8100 is a
  `genstudio_url` + API-key change and is documented in `test-instance/README.md`.
- Promotion procedure and rollback: `test-instance/README.md`.
- Remaining known issues and the ordered fix plan:
  `docs/superpowers/plans/2026-09-21-stabilization-plan.md`.

## 2. Current checkout, branch, base SHA, runtime

- Container: `/build/Developer/gateway-kh`
- Normal checkout: `repository/` — branch `main`, HEAD `736d7b0`, clean except a
  preserved `.DS_Store` modification. Untouched.
- **Working checkout: `worktrees/handoff-completion/`, branch
  `codex/content-pack-delivery`.** Fast-forwarded to `main` `736d7b0`, with the
  Content Packs work committed on top; latest commit `665f38c`. Clean tree.
- Story source dependency: `/build/Developer/story-studio-kh` at
  `6f3c4e3` (1.4.213), `node_modules` present. Installed
  `/Applications/Story Studio KH.app` 1.4.213.
- Running gateway (stable): build `97bb3a5-202609210714` on port 8000, regular
  Dock app, 124 models offered, real jobs and provider keys in use. `[MEASURED]`
- Worktree `.venv` Python 3.11.16; Node v26.8.2.
- **Sections 3–7 below record the recovery baseline from earlier on 2026-09-21**
  (what was found before any edit). They are historical; the current state is in
  §1b and §11.

## 3. Inherited work preserved before any edit

The previous session was still writing files while recovery began (last write
11:35; recovery first read 11:56). Nothing was live. Before touching anything,
the complete uncommitted work was copied outside Git:

- `backups/pre-recovery-20260921-115619/tracked-modifications.patch` (53,473 B)
- `backups/pre-recovery-20260921-115619/untracked-files.tar` (173,056 B, 10 files)
- `backups/pre-recovery-20260921-115619/ui-render/` — rendered evidence (below)

No file was reset, stashed, overwritten, rebased or cleaned. The copy is not a
commit.

### Partial work found

The prior session had implemented essentially all of the private Content Packs
feature (plan Tasks 1–3) but left it **uncommitted, undocumented, with the plan
checkboxes unchecked**, and introduced one regression. See
`docs/superpowers/plans/2026-09-21-private-content-shelf.md` and
`docs/superpowers/specs/2026-09-21-private-content-shelf-design.md`.

- New: `gateway/content_delivery.py` (796 lines), `gateway/content_signing.mjs`,
  `gateway/story_pack_validation.mjs`, `tests/test_content_delivery.py`,
  `tests/test_content_delivery_integration.py`, `tests/test_content_shelf_ui.js`,
  `tests/verify_story_content_delivery.mjs`, `docs/content-packs-authoring.md`,
  the design spec and the plan.
- Modified: `gateway/api.py`, `gateway/admin.py`, `gateway/admin_ui.html`,
  `menubar/MainWindow.swift`, `menubar/PackLibrarySection.swift`,
  `tests/prepare_authoring_fixture.mjs`, `README.md`, `AGENTS.md`, `.gitignore`,
  and this session's `tests/test_handoff_api.py`.

## 4. Verified baseline (before this session's fix)

Run from `worktrees/handoff-completion`. No paid calls, no live-Hub suite, no
real generation, no app restart, no profile mutation.

| Suite | Result |
|---|---|
| `test_content_delivery.py` | 15/15 pass |
| `test_content_delivery_integration.py` (real Story) | 1/1 pass |
| Content Packs + library + pack-authoring UI (Node) | 19/19 pass |
| `test_e2e.py` | 521/521 pass |
| `test_content_library.py` / pack authoring (21/5/1) | pass |
| `test_audit_regressions.py`, job/recovery/retention/relocation/native/packaging | pass |
| `test_console_actions.js` / `test_story_authoring.mjs` | 9/9, 12/12 pass |
| **`test_handoff_api.py`** | **10/11 — 1 FAIL** |

### The failing check — defect P0

`test_content_compatibility_routes_never_invent_ownership` asserts every
unconfigured `/v1/content/*` route returns **503 `CONTENT_STORE_NOT_CONNECTED`**.
Measured instead: `GET products` → 422, `GET shelves` → 422 (required
`story_version`), `POST downloads` → 415 (body read first). Root cause: the
rewritten Story routes validated request shape before consulting the durable
authority, so an unconfigured Store was reported as a bad request.

## 5. Packets — objective, evidence, disposition

| ID | Priority | State | Objective / disposition |
|---|---|---|---|
| P0 | integrity / contract | **done** | Unconfigured precedence on Story content routes |
| P1 | reporting | **done** | Durable status record; plan execution note; built/running truth |
| P2 | release prep | **done** | Build app without installing; verify it compiles |
| P3 | review | **done** | Self-review of the full change set (not independent) |
| P4 | performance | **watch** | Per-request authority reads; measured, not a demonstrated problem |
| P5 | owner decision | **blocked** | Install the verified build (requires explicit authorization) |

### P0 — unconfigured precedence (done)

- Files: `gateway/api.py` (+ `require_content_authority` dependency applied to all
  nine Story content routes), `tests/test_handoff_api.py` (route list extended to
  `shelves` and `public-key`).
- Root cause and fix: FastAPI raises a missing required query parameter or a
  malformed body as 422/415 before the handler runs. A dependency runs first, so
  `require_content_authority` now performs authentication and the configured
  check ahead of parameter/body validation, and `ensure_configured()` preserves
  the `CONTENT_STORE_NOT_CONNECTED` contract. Validated behavior is unchanged
  once a shelf exists.
- Proving evidence `[MEASURED]`: all eight unconfigured routes return 503
  `CONTENT_STORE_NOT_CONNECTED`; missing header still 401; wrong key still 403;
  configured `products` without `story_version` still 422, with it 200; a bad
  `downloads` body still 422 `CONTENT_REQUEST_INVALID`. Recovery paths also
  measured: a damaged durable state returns `503 CONTENT_AUTHORITY_DAMAGED` for
  `products` (no `story_version`) and `downloads` (no body) instead of 422/415,
  and `POST /admin/api/content-shelf/setup` refuses with the same code rather
  than silently regenerating a lost identity. `test_handoff_api` 11/11,
  `test_content_delivery` 15/15, integration 1/1, `test_e2e` 521/521.
- Behavior before/after retained in the baseline table and section 4.

### P1 — reporting (done)

- `Project Status.md` (this file) is the durable record.
- The plan carries an "Execution status — 2026-09-21 takeover" section.
- Honest split recorded: source is implemented and verified; the **installed**
  app is still `b12550f-202609201849` and does **not** contain it.

### P2 — build (done)

- Command: `sh menubar/build.sh --no-install`. Exit 0. Swift compiled, Python
  runtime bundled, ad-hoc signature valid.
- Result `[MEASURED]`: `menubar/build/gateway-kh.app`, build
  `b12550f-202609210501`, arm64, Python 3.11.16.
- **Not installed.** The installed app and the running gateway were not touched.

### P3 — self-review (done; not independent)

Reviewed `gateway/content_delivery.py`, `gateway/content_signing.mjs`,
`gateway/story_pack_validation.mjs`, the `api.py`/`admin.py` diffs, the
`admin_ui.html` diff, and the new tests.

Confirmed sound:
- Authority storage: dir mode exactly 0700, `O_NOFOLLOW`, `nlink == 1`, file mode
  exactly 0600, dev/ino/size/mtime/ctime stability, state size bound, marker
  tombstone written before the key, and the signing identity re-derived and
  compared on every state read — a missing or replaced key fails closed and is
  never regenerated.
- Signing helper: bounded stdin, Ed25519 via Node `crypto`, key read with
  `O_NOFOLLOW|O_NONBLOCK` and stability checks, `O_EXCL` `0600` generation,
  canonical sorted-key JSON, no command-line secrets.
- Archive validation: 50 MiB bound, symlink refusal, stability check, exactly one
  standalone item and no bundles, real Story registry receipt and signature
  verification, derived metadata only, archive content never executed.
- Download grants: HMAC-bound to account/key/release/digest/bytes, 900 s expiry,
  re-verified on every read; the absolute download origin comes from the accepted
  ASGI endpoint, never the `Host` header.
- UI: every interpolated field passes through `esc()`; stale-response generations
  guard setup/refresh/publish; the setup button is gated on an explicit
  acknowledgement; refused publication preserves the draft.
- Tests are behavioral (VM/controller state and assertions), not source-string
  checks.

Findings deliberately **not** changed:
- `_available()` reads and SHA-256-verifies every archive on each catalogue
  request. With the 1000-release / 50 MiB caps this is potentially heavy, but it
  is the module's explicit integrity guarantee and no performance problem was
  demonstrated. Upgrade path: a publish-time digest+size sidecar, or thread one
  request-scoped state read through the handlers (see P4).
- `status()` reports `configured: false` for a damaged authority. This matches the
  frozen contract ("`configured` means an intact durable authority exists"), the
  detail explains the damage, and the UI disables setup in that state, so it does
  not invite silent regeneration.
- `POST /admin/api/content-shelf/setup` is reachable from loopback without a
  token. That is the project's existing admin model and it still requires a
  nonempty Gateway API key; unchanged.

### P4 — per-request authority reads (watch)

`[MEASURED]` `_configured_state()` costs ≈55 ms (it spawns Node to re-inspect the
signing key). A configured content request measured ≈113 ms because the P0
dependency plus the handler each read state once. This is acceptable for an
occasional Story catalogue refresh and is not a demonstrated defect. Upgrade path
if it ever matters: pass the dependency's state to the handler, or cache the
signing-identity inspection keyed on the key file's dev/ino/size/mtime/ctime with
a cheap re-stat. Do not weaken the fail-closed check to gain speed.

### P5 — install (blocked, owner decision)

The verified build is ready but **replacing `/Applications/gateway-kh.app` is a
consequential action this session is not authorized to take**. Exact request and
rollback are in section 7.

## 6. Rendered UI verification `[MEASURED]`

Served by a disposable isolated Gateway (`tests/serve_ui_fixture.py`: temp
support/data, random port, mock provider, no live Hub or credentials) with the
new `admin_ui.html`, then rendered by headless Chrome at
`/admin#packs`. Evidence preserved at
`backups/pre-recovery-20260921-115619/ui-render/`:

- `content-packs-desktop-1280.png` — setup shows "Not set up" and a disabled
  "Set up private shelf" until acknowledged; all four supported kinds are listed;
  Drafts, Shelved and Preserved archives are visually separate; empty states are
  honest ("Not installed here").
- `content-packs-narrow-760.png` — no horizontal overflow at 760 px; controls
  stack and text wraps.

The fixture was stopped afterwards; the live gateway (pid 52952) was verified
untouched. Keyboard access was not exercised; the behavioral suite covers
disabled/pending states and escaping.

## 7. Final gate on the frozen source snapshot

All sequential, no competing suites, from `worktrees/handoff-completion`:

- Python: `test_e2e` 521/521; `test_content_delivery` 15/15;
  `test_content_delivery_integration` 1/1; `test_content_library` 9/9;
  `test_pack_authoring` 21/21; `..._http` 5/5; `..._integration` 1/1;
  `test_handoff_api` 11/11; `test_audit_regressions` 6/6; `test_job_claim` 2/2;
  `test_job_recovery` 9/9; `test_jobs_outputs` 2/2;
  `test_adapter_checkpoints` 3/3; `test_retention_inputs` 3/3;
  `test_profile_relocation` 7/7; `test_task4_native` 2/2;
  `test_task5_native` 1/1; `test_task6_packaging` 10/10;
  `test_create_layout` 1/1. **All exit 0.**
- Node: `test_content_shelf_ui` 7/7, `test_content_library_ui` 2/2,
  `test_pack_authoring_ui` 10/10, `test_console_actions` 9/9,
  `test_story_authoring` 12/12. **All exit 0.**
- Build: `sh menubar/build.sh --no-install` exit 0, valid ad-hoc signature.

Deliberately not run, with reason:
- `test_fleet_standalone.py` — requires `GATEWAY_ALLOW_LIVE_GENERATION=1` and the
  owner's real Studio Hub; real generation must not be triggered casually.
- `test_ui.py` — drives the **installed** app, which is still the old build; it
  would either test unrelated code or force an intrusive restart.
- Code-only Graphify refresh — the `graphify` binary is not on PATH here; graph
  findings were replaced by direct source reading.

## 8. Decisions, limitations, blockers

- Development stays in `worktrees/handoff-completion`; the owner's `main` checkout
  is untouched. The branch was fast-forwarded to `main` (PR #7) and the verified
  work committed as `5f94b4a`.
- No push, deploy, credential change, paid call or destructive migration occurred.
- `[INHERITED]` reports in `_handoffs/2026-09-20-*` are evidence, not truth; the
  `[MEASURED]` tables here supersede them where they disagree.
- Legacy transferred Store packs remain archive-only by design; the historical
  material now lives in `_trash/` (recoverable) per the cleanup convention.

## 9. Two-instance state (current)

- **Working:** `/Applications/gateway-kh.app` build `5f94b4a-202609210554`,
  port 8000, signature valid, profile byte-identical. Story Studio's endpoint
  unchanged.
- **Test:** `test-instance/` on port 8100, isolated profile, running the same
  committed source. `run.sh` / `status.sh` / `stop.sh`.
- **Rollback:** `backups/rollback-20260921/` (previous bundle, profile hash
  record, rendered UI evidence).

## 10. Promotion record — 2026-09-21

- Commit `5f94b4a` "Add private Content Packs shelf and Story delivery" on
  `codex/content-pack-delivery`, fast-forwarded from `b12550f` to `main`
  `736d7b0` first. Worktree clean after commit.
- Full gate on the committed snapshot: e2e 521/521, content delivery 15/15, real
  Story integration 1/1, handoff contract 11/11, content library 9/9, pack
  authoring 21/5/1, audit/job/recovery/retention/relocation/native/packaging,
  UI 7/3/10/9/12 — all exit 0.
- Build `sh menubar/build.sh --no-install` → `5f94b4a-202609210554`, arm64,
  ad-hoc signature valid.
- Promotion: quit the app, freed port 8000 by PID, `ditto` the verified bundle
  into `/Applications`, relaunch. `/health` reports `5f94b4a-202609210554`
  (new pid `81547`); strict signature still valid after startup; `.env`,
  `menubar.json`, `voices.json`, `models_cache.json` and `model-limits.local.json`
  byte-identical to before.
- Cleanup: old app-version backups and both `menubar/build` outputs removed and
  consolidated to the single rollback folder (92 MB). Container ~1.6 GB → ~982 MB.
- Historical transfer archives (586 MB) and other superseded material were moved
  to `_trash/` (container root) under the owner's recoverable-delete convention;
  see `_trash/README.md`. Nothing in `_trash` was hard-deleted.

## 10b. User-requested app change — regular Dock app (done 2026-09-21)

- Problem: the app was an agent (`LSUIElement=true`, `setActivationPolicy(.accessory)`),
  so it had no Dock icon and never appeared in ⌘-Tab; the owner could only reach
  it through the menu bar bolt.
- Change: `LSUIElement` now `false`, activation policy `.regular`. Added
  `applicationShouldHandleReopen` so clicking the Dock icon re-opens the window,
  and `applicationShouldTerminateAfterLastWindowClosed` returns `false` so closing
  the window keeps the app and gateway running. Menu-bar bolt unchanged. Stale
  accessory-process comments in `test_ui.py` and `SettingsSection.swift` updated.
- Commit `6ed4c44`; built `6ed4c44-202609210701`; promoted and verified:
  `LSUIElement=false`, signature valid, `lsappinfo` reports
  `type="Foreground"`, `/health` on the new build. The owner's 10 entered cloud
  provider keys survived the app swap.

## 10c. User-requested UI fixes (done 2026-09-21)

- **Providers page squished / buttons clipped.** The web console's provider card
  header was one non-wrapping flex row inside `overflow:hidden`, so in a narrow
  pane Configure/Disable were cut off. Now wraps, the name block shrinks, long
  URLs wrap, and the field grid uses `minmax(min(260px,100%),1fr)`. Verified by a
  620 px headless render: every button is visible.
- **Key-status filter.** Added All / Key set / No key plus a live "N without a
  key" count, so after entering a key the owner can keep viewing only the
  keyless ones. Behavioral test in `test_console_actions.js`.
- **Voices controls were a footer below the scrolling table.** Moved to two rows
  above the table, and added **Select all shown** (selects the currently filtered
  rows) and **Select none**, so search → select all shown → Enable selected works.
- Commit `97bb3a5`; built `97bb3a5-202609210714`; promoted and verified
  (`type="Foreground"`, signature valid, cloud keys preserved). The native Voices
  layout could not be machine-verified because Accessibility is still not granted
  (S4); it compiles and the build is installed.

## 10d. Native UI suite on the promoted build (S4) — verified 2026-09-21

Accessibility was granted, and `tests/test_ui.py` ran against the installed
build `97bb3a5-202609210714`:

- **All 131 UI checks pass**, with 2 environment skips (no fleet controllers
  configured). Re-run after two real chat jobs existed, so the previously
  skipped Jobs checks now execute and pass: table populated, page heading, and
  summary line.
- The window-resize invariant holds: every section stayed at the forced 700 pt
  height. An earlier run showed 12 transient resize failures while the app was
  still settling after launch; later runs passed. The one-point window-restore
  difference was AX integer rounding and now has a ±1 tolerance.
- Test drift fixed (it could not run before Accessibility): `SIDEBAR` and
  `SECTIONS` now include **Content Packs**; the Jobs heading string was stale;
  the Jobs data checks skip on an empty gateway; and the "job detail" check was
  reading the table's scroll area instead of the Jobs detail pane
  (`scroll area 3`) with an out-of-date expected string.
- Command: `.venv/bin/python tests/test_ui.py` (drives the real app; needs
  Accessibility for the host process).

## 10e. Private shelf identity backup (S10) — done 2026-09-21

- `scripts/backup_content_authority.py` copies the three authority files
  (`initialized`, `authority.json`, `signing-key.pem`) at mode `0600`, verifies
  each copy by SHA-256, and refuses to overwrite an existing backup or copy a
  missing/world-readable identity. It prints no secrets.
- `tests/test_content_authority_backup.py` covers those rules (4/4).
- Restore drill on an isolated profile against the real Story registry
  (read-only): setup → backup → delete `signing-key.pem` (shelf reports
  unavailable) → copy the backup back → shelf reports configured and available.
- Procedure documented in `docs/content-packs-authoring.md`. The identity is
  created only when the owner runs explicit shelf setup.

## 10f. Real generation through the working instance (S5) — verified 2026-09-21

Owner authorized one cheap paid call; two tiny calls were made and disclosed.

- Route: `POST /v1/chat/responses` (the durable worker path Story Studio uses),
  model `deepseek-main::deepseek-flash`, isolated idempotency keys.
- Call 1: succeeded, no 402, settlement `0/0/0`, usage 45 tokens. Its visible text
  was empty because `max_tokens: 8` was fully consumed by 8 reasoning tokens
  (`finish_reason: length`) — a parameter choice, not a defect.
- Call 2: succeeded, `finish_reason: stop`, settlement `0/0/0`, usage 71 tokens,
  `output_text: "ok"`. Both jobs are recorded and visible in `/v1/account/jobs`.
- Provider discovery is live for 16 of 17 cloud providers with no errors, and
  `/v1/models` serves **124 models** to the Gateway key.

## 10g. Real-Hub fleet suite (S6) — verified 2026-09-21

The owner confirmed the Hub was free.
`GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python tests/test_fleet_standalone.py`
reported **all 47 fleet checks passed**: a real transcription ran on the location
via `studio-hub::mlx-community/whisper-base-mlx`, the artifact came back through
the gateway, the id Story Studio sent was the id it was told back, and no
response in the whole run was 402. The suite needs a real speech clip in
`data/*.wav`; the owner's artifacts had been pruned, so a synthetic clip was made
with macOS `say`, used for the run, then removed — `data/` is unchanged.

## 10h. Tailscale access for a remote Story Studio (2026-09-21)

The owner runs Story Studio on a **different machine**; its default
`http://127.0.0.1:8000` means *that* machine, so it could never reach this
gateway. Enabled the tailnet path on the working instance:

- Changed `GATEWAY_HOST=127.0.0.1` → **`GATEWAY_HOST=0.0.0.0`** in the working
  profile's `.env` (mode preserved `0600`), then restarted the gateway by PID;
  the app respawned it (`--host 0.0.0.0`, new pid). The project's rule is to
  bind `0.0.0.0` and **never the Tailscale address alone** — that would take
  loopback dark and break local Story Studio.
- This Mac is `m2-max-32-512`, Tailscale IPv4 **100.126.154.54**, MagicDNS
  `m2-max-32-512.tail4e72cf.ts.net`.
- Verified `[MEASURED]`: `/v1/models` from the Tailscale IP with the key → 200
  (124 models); without a key → 401; `/admin/api/status` without a key → 401;
  `/health` → 200 (health is unauthenticated by design); loopback still 200.
- Remote Story Studio must set **Base URL `http://100.126.154.54:8000`** and the
  GenStudio API key = this gateway's key (Settings → Copy). Both machines must
  be on the same tailnet account (`owner-account@`).
- Exposure note: `0.0.0.0` also listens on the LAN, and the macOS application
  firewall is **disabled** on this Mac. The API key protects `/v1` and `/admin`;
  `/health` is open. Tighten with the firewall or Tailscale ACLs if wanted.
- Tailscale node keys expire by default; mark this Mac (and the client Mac)
  non-expiring so the link does not silently die
  (`docs/fleet-deployment-notes.md` §3).
- **Port moved 8000 → 8787** (owner choice): 8000 is the most contested dev port
  and is also Story Studio's default, which kept "is this pointed at the right
  thing?" permanently ambiguous. Set `GATEWAY_PORT=8787` in `.env` and
  `"port" : 8787` in `menubar.json`; port 8000 is now closed. Every Story Studio
  client must update its Base URL to `http://…:8787`.
- **Copyable Base URL** (commit `e14d19e`, build `e14d19e-202609211633`):
  Settings has a Story Studio section showing the Base URL with a **Copy**
  button — Tailscale address when bound beyond loopback, private LAN fallback,
  loopback when local — and the sidebar connection line shows that host instead
  of `127.0.0.1`.
- Verified from the tailnet on 8787: `/health` 200, `/v1/models` with key 200
  (124 models), without key 401, admin without key 401; MagicDNS form also 200.

## 10i. Remote Story compatibility + private shelf setup (2026-09-21)

Two fixes while the owner connected Story Studio from another machine.

- **Capability catalogue fields** (commit `df2566c`, build
  `df2566c-202609211649`). Story's legacy capability catalogue reads
  `GET /v1/models` and keeps a row only when `modality` and `operation` are in
  fixed vocabularies; the endpoint published 124 rows without them, so the
  "GenStudio Catalog" diagnostics showed every tile at 0/0 while the
  per-modality pickers (their own routes) worked. `catalog._story_row` now
  publishes `modality`, `operation` and `availability`. Verified with Story's
  own client: **122 capability rows** (chat 80, image 9, video 9, voice.tts 21,
  transcription 3; music has no operation in Story's vocabulary by design).
- **Private Content Packs shelf enabled** on the working instance via explicit
  setup: `configured: true`, `available: true`, account
  `gateway.private.account.b190739b…`, baseline registry revision **289**, zero
  releases. `GET /v1/content/account` now answers **200** (was the 503 Story
  reported), on loopback and over Tailscale.
- **Identity backed up** to
  `backups/content-authority-20260921-165052/` (three files, mode `0600`, SHA-256
  verified by `scripts/backup_content_authority.py`). This is a first copy on
  this Mac; move it to encrypted local storage and keep it out of cloud folders
  and Git.
- Diagnosed but **not** a gateway problem: Story's per-modality fetches failed
  because the new Base URL had not been saved in Story's settings (the field
  showed it; the **Save URL** button had not been pressed), so it was still
  addressing the previous endpoint. Verified the gateway serves Story's client
  correctly on loopback and over the Tailscale URL.

## 10j. First real Content Pack end-to-end (2026-09-21) — ACCEPTED

The owner approved a first real pack, and the whole path ran with real
identities and Story's real consumer:

1. **Reserved** in the real Story registry (revision **289 → 290**, receipt
   `authoring:a42cafe7…`):
   `com.storystudio.pack.topic.keeping-food-before-refrigeration@1.0.0`, family
   `…family.topic.keeping-food-before-refrigeration`, kind `topic-pack`,
   compatible with `formula.history-explained`. The registry was backed up first
   to `backups/story-registry-before-content-pack-20260922-000106.json`.
2. **Authored** as a Gateway draft, **previewed**, and **published** to the
   private shelf through Story's own schema/receipt validation:
   release `gateway.private.release.52c86781e484b629c24954c1`, 1210 bytes,
   `current: true`, `available: true`.
3. **Verified Story's wire**: `/v1/content/products` lists it (`topic_pack`,
   `owned: true`, `compatible: true`), `/v1/content/account` owns it,
   `/v1/content/entitlements/<family>/releases` reports it current and available,
   and `/v1/content/downloads` issues a 900 s grant.
4. **Installed with Story's real installer** on an isolated Story profile:
   discovered `not_installed` → installed → `activated: true`, version `1.0.0`,
   `installState: installed_current`, and Story's content-pack state holds the
   pack. The owner's real Story data was not touched.
5. **Backup gap closed**: `scripts/backup_content_authority.py` now copies the
   referenced release archives as well as the identity, and refuses to run when
   an archive is missing. `tests/test_content_authority_backup.py` 6/6. Fresh
   complete backup: `backups/content-authority-20260921-170315/` (identity +
   archive).

**Owner-confirmed 2026-09-22 `[REPORTED, owner]`:** Story Studio on the video Mac
listed *Keeping Food Before Refrigeration* as owned and **Download/Install
worked** end to end. With the isolated-profile install above (measured here) and
this consumer confirmation, the private delivery path is proven for a real pack
from reservation through installation.

## 10k. What's New link beside the build stamp (2026-09-21)

The notes window already existed but was reachable only from Overview and the
status menu. Added a small accent link under the version line in the persistent
sidebar footer (`MainWindow`), opening the same `WhatsNewWindow`, and refreshed
`menubar/whats-new.md` with this build's changes (Content Packs, remote Story via
Tailscale, the port move, the provider key filter, Voices bulk controls, the Dock
app, and the light-mode fix).

- Build `c50c05e-202609211705`, promoted. Verified `[MEASURED]`: the sidebar
  exposes the button, and pressing it opened `gateway-kh — What's New`.
- `tests/test_ui.py` gained "the sidebar offers What's New beside the version";
  the suite passes **127/127** with 7 environment skips (no fleet controllers,
  and Studio Hub is not running on this Mac right now).

## 10l. Sidebar regrouped (2026-09-21)

The owner reported the sidebar grouping was muddled. "Library & app" was
answering three questions at once — what you keep, where work runs, and the
app's own settings — with Content Packs stranded after Settings.

- New groups: **Create** (Audio…Chat) · **Library** (Jobs, Voices, Models,
  Content Packs) · **Connection** (Providers, Fleet) · **App** (Settings).
- The View menu follows the same order; ⌘1–⌘6 keep their original pages.
- Build `c74ae22-202609211712`, promoted. `tests/test_ui.py` pins the new
  grouping and rows: **132/132 pass** (2 environment skips, no fleet
  controllers).
- One run failed with a stray second window (`What's New`) mid-walk and the
  titles cascaded; it reproduced clean manually and the next full run was green,
  so it was interference rather than a defect.

## 10m. Jobs list shows the provider (2026-09-21)

The owner asked why the Jobs tab did not name the provider. `/api/jobs` already
publishes `provider_name` (the gateway resolves the display name); the native
table simply never drew it.

- Added a **Provider** column (`When · Type · Provider · Model · State ·
  Outputs`) and set `.uniformColumnAutoresizingStyle` so the wider list truncates
  cells instead of pushing them past the edge at the minimum window size.
- Build `4081601-202609211720`, promoted. Verified live:
  `25m ago | Chat | NVIDIA NIM | deepseek-ai/deepseek-v4-flash-0731 | failed | 0 files`.

### Two real failures found while checking

The owner's 7 jobs are 5 succeeded / 2 failed, both provider-side and both
handled correctly by the gateway:

- `PROVIDER_FAILED` — NVIDIA NIM `deepseek-ai/deepseek-v4-flash-0731` answered
  **HTTP 410: reached its end of life on 2026-09-21**. The gateway surfaced the
  provider's own message. **The model is still `enabled: true` and
  `publishable: true`**, so Story Studio can pick a model that can only fail —
  the "enabled but cannot serve" class the project warns about. Disabling it is a
  configuration change for the owner.
- `PROVIDER_UNAVAILABLE` — NVIDIA NIM `z-ai/glm-5.3-flash` read timed out. The
  job is a durable unknown-outcome hold that is **not** resubmitted
  automatically, which is the designed behaviour.

### Sweep result (owner-requested)

- **Disabled** `nvidia-nim-main::deepseek-ai/deepseek-v4-flash-0731`
  (`changed: 1`); it has no `enabled` flag now and offered dropped 124 → 123.
- **No other dead model found.** No provider reports a discovery error, and no
  other enabled model has a terminal failure in the job history.
- Two other providers list the same model name; **OpenRouter's
  `deepseek/deepseek-v4-flash-0731` is live** (`expiration_date: None` from its
  public model list — checked without a paid call), and the `:batch` sibling is
  the already-known-bad class and stays off. Experiential Labs' entry is
  unverified but has no failure record.
- **Groq's `whisper-large-v3` and `whisper-large-v3-turbo` are enabled but
  withheld** — by design: the gateway must ask for SRT and Groq's Whisper cannot
  return it. They never reach Story, so they are harmless; disabling them would
  only tidy the enabled count.
- `minimax-direct` has a key but zero models enabled — nothing to do.

## 10n. Bulk, self-contained Content Pack import (2026-09-21)

The owner asked for one button that imports 1/3/10/100 packs with nothing typed
in, plus search/sort/filter, and asked whether the authoring skills must carry
that metadata.

- **Confirmed self-describing.** `story_pack_validation.mjs` derives `title`,
  `description`, `tags` and `kind` from the pack's own documents, and
  `import_archive` stores them. Import never needed manual fields; only the UI
  was single-file.
- **Built:** a multi-file picker plus a folder picker (`webkitdirectory`), one
  **Validate and preserve** button that loops the same validated import over every
  `.sspack`, reports progress while running, reports failures per file without
  stopping the batch, and clears the picker afterwards.
- **Built:** search (name, id, kind, tag), sort (name, name-desc, kind, numeric
  version, integrity) and an integrity filter over the preserved-archive list,
  rendering from the one fetched list rather than re-reading the Gateway.
- Build `7aa0f35-202609211734`, promoted. `test_content_library_ui.js` 6/6
  (retry, search/sort/filter including numeric version order, bulk batch with a
  mid-batch failure, empty selection); all Node UI suites pass; `test_ui.py`
  132/132.
- **Kind boundary:** the authoring skills support nine kinds including
  `lettering-style` (fonts), but Story 1.4.213's parser exposes only four, so
  Gateway's shelf delivers `series-formula`, `topic-pack`, `visual-style`,
  `thumbnail-recipe` only. Fonts can be authored and preserved but not installed
  until Story exposes the kind.

## 10o. The historical Store library re-homed as new `-v2` packs (2026-09-22)

The owner's old channels depend on the Store library, and the private shelf
refuses historical identities, so the content was re-authored as **new** packs
with `-v2` identities. Nothing historical was reissued or re-signed.

**Analysis** (`store-analysis/INVENTORY.md`, `analyze_store_archives.py`,
read-only): 272 archives → 266 distinct bytes (6 exact duplicate copies) → **180
packs** across 180 families, **0 identity conflicts**, one overlap (the
Wars/Treaties pair, merged). The current Story registry agrees pack for pack and
still holds 208 deliverable source directories, so sources were used, not the
archives.

- **141 deliverable packs**: series-formula 19, topic-pack 46, visual-style 40,
  thumbnail-recipe 36. **39 not deliverable**: lettering-style (fonts) 20,
  metadata-formula 8, channel-kit 11 — kinds Story 1.4.213's parser cannot serve.
- **Text-only phase complete. 63 packs are shelved** — every series-formula and
  topic-pack (minus the merged pair, plus the Gateway-authored pack already
  shelved) — and both kinds were installed through Story's own installer
  (`Backyard Creatures` topic, `How Did Ancient Humans…?` formula, both
  `activated: true`). Shelf: 66 release rows / 64 current products.
- **Pipeline, and its order trap:** sources must **not** exist when reserving
  (`registered authored source does not exist` and `…is not registered` are the
  two faces of the same check). Correct order: reserve → create sources →
  transition to authored → build with `--delivery-profile
  genstudio-store-receipt-v1 --min-app-version <release floor>` → import →
  publish. Tooling: `store-analysis/reauthor/` (`reauthor.py` generates,
  `finish_batch.py` finishes, `bump_corrected.py` bumps a version).
- **Two defects found by building, not by reading:** a nested reference
  (`production_kit.recommended_visual_style_id`) was missed by a field-list
  remap — fixed by remapping recursively on exact id match; and the merged pack's
  document id came out double-suffixed. `economics-businesses-v2` and
  `natural-world-animals-v2` were republished as **1.0.1** (1.0.0 kept) so source
  and archive agree.
- **Known untidiness — since resolved**, see §10v: the merged Wars/Treaties record
  had pack id `…historical-wars-treaties-world-order-v2` and item id
  `…archival-ledger.wars-treaties-world-order-v2`.
- Registry revisions: 291 and 294 (text-only batches), 356 and 358 (the two
  1.0.1 bumps). Old→new mapping for every deliverable pack:
  `store-analysis/reauthor/mapping.json`.
- **Remaining: 76 asset packs** (40 visual-style, 36 thumbnail-recipe) need real
  preview bytes, which per the skills come from Studio Hub production or the
  owner's originals — this Mac must not generate them, and Studio Hub was quit.
- **The 39 blocked packs have a named owner now.** Sent Story
  `_handoffs/2026-09-22_wire-kind-parity-lettering-and-metadata-formula.md`
  (our record: `_handoffs/2026-09-22-wire-kind-parity-handoff.md`). The earlier
  claim that Story "cannot serve fonts" was too broad: Story already has the wire
  kind `font_pack`, the archive `case 'lettering-style'`
  (`content-packs/archive.ts:325`) and `letteringCatalogue()`
  (`services/lettering-catalogue.ts:80`) — what is missing is any bridge, since
  `font_pack` appears nowhere in `electron/server` outside the kind union, so a
  delivered font product maps to no archive and installs nothing.
  `metadata-formula` genuinely has no wire kind. The handoff leads with the
  ordering rule from Story's own comment
  (`genstudio-content-store.ts:45-53`): one unknown kind kills the whole
  catalogue parse, so Story must accept a kind before Gateway publishes one.

## 10p. The whole deliverable library is live, and a catalogue guard (2026-09-22)

**140 current products on the shelf — the entire deliverable historical library**
— and Story's own installer activates all four kinds. Measured: 45 topic packs,
19 series formulas, 40 visual styles, 36 thumbnail recipes; Story's real
`parseContentStoreCatalog` returns 200 over the whole catalogue.

- **No Studio Hub was needed after all.** The preview bytes the skills require
  are the owner's own, already preserved: each visual-style archive/source holds
  3 WebP (`face`/`hero`/`scene`) and each thumbnail recipe 3–4, and
  owner-supplied originals are an accepted preview source. The generator copies
  the whole source directory now, not just the JSON, so assets travel with the
  pack.
- **Two packs had no source directory** (`centered-hero-headline`,
  `dreamscape-story-window`); `rebuild_from_archive.py` reconstructed them from
  their preserved 1.0.1 archives. Both shelved.
- **An outage I caused, and the guard that prevents it.** Four visual styles
  carried **14 tags**; Story's `parseTags` caps at 12 and
  `parseContentStoreCatalog` throws on the first bad row, so Story answered
  **500 for the whole Content Store** — one pack emptied every product. Fixed by
  trimming those four to 12 and republishing as 1.0.1.
  **`content_delivery._validate_catalog_row` is the guard**: at publish it builds
  the product row and runs it through Story's **real** `parseContentStoreCatalog`
  via a new `validate-catalog-row` action in `story_pack_validation.mjs`, and
  refuses with the parser's own words (`422 CONTENT_CATALOG_ROW_INVALID`). The
  limits are called, not copied, so a kind or field Story learns later is covered
  for free. Regression test:
  `test_content_delivery.ContentDeliveryTest.test_catalog_row_guard_uses_storys_own_parser`.
- **A mistake worth recording.** A version bump was invoked with slugs missing
  their `-v2` suffix, so it targeted the **historical** pack ids: it created four
  stray `originals@1.0.1` records and source copies. The shelf refused them (as
  designed). Cleanup is not a delete — a retired record's source must **not**
  exist, and a reserved record goes to **withdrawn**, not retired, so: remove the
  source directory, then retire (authored) or withdraw (reserved). Registry still
  validates, revision 448. Always pass the `-v2` slug to `bump_corrected.py`.
- Build `ab44302-202609211835`, promoted; the guard is in the installed bundle.

## 10q. S12 — the admin page is keyboard-accessible and tested (2026-09-22)

Tabbing through `/admin#packs` is now a check rather than an exercise.
`tests/check_admin_keyboard.mjs` drives real Chrome over the DevTools protocol
with only Node built-ins — global `fetch`/`WebSocket`, no puppeteer to install and
keep current — presses Tab, and fails if a control in the view is unreached or
focused without a visible ring. Measured on the test instance: **12 controls, all
reached, all ringed.**

Three genuine gaps found and fixed in `gateway/admin_ui.html`:

- **Text fields removed the outline** — `input:focus, select:focus` and
  `textarea:focus` set `outline: none`, leaving a 1px border-colour change as the
  only cue while every button had a 2px ring. They keep the accent border and now
  draw the ring too.
- **Focus could scroll under the sticky table header**, landing invisibly;
  focusable controls carry `scroll-margin-top`.
- **The Create tabs used `aria-selected` without `role="tab"`**, which announces
  nothing to assistive tech, and the tablist pattern's roving tabindex would have
  pushed the unselected tabs out of the Tab order. They are a labelled
  `role="group"` of `aria-pressed` toggles now: every tab stays reachable and its
  selection is announced.

`tests/test_admin_accessibility.js` (4 checks) holds the same invariants
statically, so a handler on a non-focusable element, a bare `outline: none`, a
lost scroll margin, or a tab strip that stops announcing selection fails without
a browser. Deliberate limit: the browser check is Chrome-only and not wired into
the routine suite, because it needs a gateway URL.

## 10r. S3 — content requests are ~37× faster, without weakening a check (2026-09-22)

The recorded cost was stale and understated. At 140 products a configured content
request measured **~204 ms**; it is now **~5.5 ms** over HTTP (`products` 391 → 8 ms
in-process, `account` 406 → 6 ms).

Three separate costs, all found by profiling rather than assumption:

- **The double read** (`require_content_authority` plus the handler):
  `require_content_authority` now *returns* the authority state and every handler
  passes it down; `_configured_state()` remains the fallback for non-HTTP callers.
  Pinned by `test_a_content_request_reads_the_authority_once`.
- **The signing inspection**, 116 ms a request — a Node spawn inside
  `_read_state`. Cached against the key file's `dev/ino/mode/size/mtime_ns/ctime_ns`,
  with the stamp re-stated before storing so a key swapped mid-inspection is never
  cached under the new stamp. Nothing is cached when the key cannot be stated
  (missing, symlinked, not a regular file), so deletion still fails closed. Signing
  is never cached.
- **The real dominant cost**: `_available()` called `content_packs.read_archive`
  for every release, so one catalogue request **read and hashed every archive in the
  library** — 140 packs, the thumbnail recipes ~7 MB each. It keeps the identical
  SHA-256 guarantee and caches the verified answer against the archive file's stat;
  a changed file is re-hashed, and a same-size corruption reads as unavailable
  (`test_availability_verifies_once_and_rechecks_a_changed_archive`).

Nothing was traded away: the payload is byte-identical in shape (140 products),
Story's parser returns 200, a visual style and a thumbnail recipe still install
`activated: true`, and a damaged signing key still answers
`CONTENT_AUTHORITY_DAMAGED`. Build `37a5969-202609211856`, promoted.

## 10s. Hardening: exposure measured, one action done, two for the owner (2026-09-22)

Full record and the exact commands: `_handoffs/2026-09-22-hardening-owner-actions.md`.

- **The exposure is authenticated, and the owner's read is right: no key, no
  access.** Measured from the LAN address and over Tailscale, keyless
  `/v1/models` and `/admin/api/status` both answer **401**; only `/health` answers,
  and it carries build/pid/counts. The `pf` anchor below is therefore **optional
  defence in depth**, not a fix.
- **FileVault stays off — owner decision, closed, do not re-raise.** He keeps it
  off deliberately so autologin keeps working; FileVault needs the password at the
  pre-boot window, so the two are mutually exclusive. The disk is therefore readable
  at rest, and that is an understood trade-off. `tmutil` also reports no Time Machine
  destinations, so nothing copies the Story sources; an encrypted backup is the
  mitigation that would not touch autologin, should that ever be wanted. Not raised
  again unless he asks.
- **Tailscale key expiry is confirmed by the owner: it no longer expires.** This
  item is closed.
- **The authority-backup passphrase is optional.** An earlier note called that
  backup unrecoverable; that was wrong. The pack content is recoverable (the
  sources are in the Story checkout and the pipeline is scripted) — what is lost is
  the shelf's **identity**, and re-founding it snapshots the current registry as a
  baseline, so every `-v2` identity becomes historical and recovery costs a
  **third** identity round (`-v3`) for ~140 packs, moving the channel mapping
  again. A day of re-issuing, not lost content. Worth encrypting when a copy of the
  backup leaves this Mac; it never does today.
- **Closing the LAN needs `pf`, which needs sudo this session cannot get**
  (approval prompts are disabled). The app firewall is the wrong tool — it is
  per-application and the gateway *is* the listener, so it would cut the tailnet
  too. The handoff carries a ready `pf` anchor that matches the tailnet by
  **address** (`100.126.154.54`) rather than by `utunN`, which renumbers across
  restarts, with the two curl checks that prove both directions.
- **The authority backup can be encrypted when wanted**:
  `scripts/encrypt_content_authority_backup.sh` seals the whole backup directory
  into one AES-256 archive. Verified — it round-trips to byte-identical files,
  refuses a wrong passphrase, refuses to overwrite, verifies the written file
  decrypts to the same digest before reporting success, and leaves no plaintext in
  the output. It prompts for the passphrase and never stores or echoes it.

## 10t. S7 — the graph is current again (2026-09-22)

`graphify-out/` was dated before this session's structural work. Refreshed from the
worktree: `graphify extract . --update --code-only`, then `graphify cluster-only .
--no-label` (the `--no-label` keeps `Community N` placeholders instead of paying for
LLM naming). **2785 nodes / 7008 edges / 154 communities**, with
`GRAPH_REPORT.md`, `graph.json` and `graph.html` all written at 2026-09-22 02:01.

It carries this session's additions — `tests/check_admin_keyboard.mjs` is a node
with 9 edges, and `tests/check_native.py`,
`scripts/encrypt_content_authority_backup.sh` and
`tests/test_admin_accessibility.js` are all present. The working tree stayed clean:
`graphify-out/` is ignored at `.gitignore:29`, so nothing generated was staged.

Two extractor limitations noted, neither a repo defect: `cocoa` and `webkit` are
minted by more than one `menubar/` file and the extractor drops the duplicate (its
remedy, per-subfolder extract plus `merge-graphs`, is not worth two framework
imports); and `swiftc -parse menubar/VoicesWindow.swift` is clean, confirming the
parse complaint in `AGENTS.md` is the extractor's, not broken Swift. The 0.8.36
skill copy was left alone — refreshing it edits the owner's Codex skill config.

## 10u. Spot review of the re-authored packs (2026-09-22)

The approved review approach was "automated rebuild from your own sources, with a
sample review plus every conflict". The sample became **every pack**, because the
comparison is cheap and a sample would hide exactly the kind of drift worth
catching.

`store-analysis/reauthor/review_rebuilds.py` compares all **145** rebuilt packs
field by field against the originals, normalising only what must change — the item
`id` and every remapped reference — so any other difference is a finding. A `1.0.1`
is compared against the `1.0.0` it corrects; a pack whose source directory is gone
is compared against its preserved archive.

**Result: 4 differences, all of them the intentional tag trim, and 0 asset
deviations.**

- The four are `baroque-mythological-sky`, `bold-gory-cartoon`,
  `golden-epic-poster` and `inked-crosshatch-storybook` — each carried 14 tags and
  Story's catalogue parser caps tags at 12, so the last two were dropped from each.
  The report lists the exact tags removed, so the decision is reviewable rather
  than merely asserted.
- **No preview asset differs by a single byte** from the original: the WebP files
  travelled through the rebuild unchanged, which is what makes the "owner-supplied
  originals" claim true rather than aspirational.
- The merged Wars/Treaties pack and the two archive-rebuilt thumbnails are included
  and clean.

Full record: `_handoffs/2026-09-22-re-authored-pack-spot-review.md`. Tooling:
`store-analysis/reauthor/` in the container, beside `mapping.json` and
`INVENTORY.md`.

## 10v. The merged Wars pack has one consistent identity now (2026-09-22)

Owner-approved cascade. The merged pack carried a pack id from one original and an
item id from the other's alias, and nothing can be renamed in place: the shelf
freezes `identity_fields = (publisher_id, product_id, entitlement_family, item_id,
kind)` for a pack and exposes no retract, and the registry requires unique item ids
including retired records. So the content was re-published under one slug and the
mismatched release hidden.

- **New pack**: `com.storystudio.pack.topic.wars-treaties-world-order-v2` with item
  id `com.storystudio.topic.wars-treaties-world-order-v2` — pack and item agree.
- **The item id move had a cascade.** Two published formulas referenced the old
  item id (`compatible_topic_pack_ids` / `default_topic_pack_ids`), so both were
  corrected and republished as **1.0.1**: `formula.archival-ledger-v2` and
  `formula.history-explained-v2`. Verified from the *published archives*, not the
  sources — both now list `…wars-treaties-world-order-v2`.
- **The mismatched release is hidden, not deleted.** Its archive was moved to
  `backups/retired-wars-merge-20260922/<sha>/`, so availability reads false and the
  old product leaves the catalogue; restoring the directory would bring it back, so
  the duplicate stays suppressed only while it is absent. Its registry record is
  `retired` (revision 453) and its source removed.
- **Order trap, again.** A `retired` record's source **must not exist**, but an
  `authored` one's source **must** — so delete-then-retire fails validation from
  both sides. Correct order: restore the source if needed, retire, then delete it.
- **Result**: catalogue still **140 products**, exactly **one** Wars/Treaties
  product, both formulas current at 1.0.1, Story's parser `delivery_ready`, and
  `mapping.json` points both original Wars item ids at the new one.
- Re-review: **147** packs, 6 differences — the 4 tag trims plus the two formulas'
  corrected reference fields, all expected; **0 asset mismatches**.

## 10w. Story answered the wire-kind handoff — verified, and we hold (2026-09-22)

Story's reply: `_handoffs/2026-09-22_story-response-wire-kind-parity.md` (theirs);
ours: `_handoffs/2026-09-22-gateway-response-to-story-wire-kind-parity.md`, also
delivered into their `_handoffs/`.

**Both asks accepted and implemented.** `font_pack` confirmed as the `lettering-style`
wire kind; `metadata-formula` accepted by the owner and given the wire kind
`metadata_formula`. Minimum accepting version for both: **1.4.216**, which is **not
released** — no DMG for it exists (1.4.214 and 1.4.215 do), the installed app is
**1.4.213**, and the changes are uncommitted in the Story tree.

**Verified rather than accepted.** `PRODUCT_KINDS` and the union now carry
`metadata_formula`; the two product-kind lists are one leaf module
(`frontend/content-store/product-kinds.ts`) consumed by both the wire parser and
`persisted-catalog.ts`; `letteringPhrase`/`letteringPhraseMaps`
(`services/lettering-catalogue.ts:115,146`) are imported and called by
`generators/image-prompts.ts`. **We ran their test** —
`npx tsx --test electron/server/__tests__/lettering-pack-bridge.test.ts` →
**7 pass / 0 fail** (note: `vitest` finds no suite; the repo runs `node:test` via
tsx).

**Our diagnosis was wrong and Story's correction is right.** Installation is
kind-agnostic — `installContentPackLocked` verifies the signed manifest, which
already declares `kind: lettering-style`, and `letteringCatalogue()` has listed
installed pack styles since 1.4.57. The real gap was the **prompt** layer: a pack
style id resolved against the hardcoded `FONT_STYLES`/`TEXT_STYLES` maps, so the
owner would have seen the swatch he paid for while the model was told to draw the
built-in serif. A silent wrong-output defect, which our handoff pointed at the wrong
layer.

**Criterion 4 on our side already agrees**: `/v1/content/products` publishes
`topic_pack` 45, `series_formula` 19, `visual_style` 40, `thumbnail_recipe` 36 —
exactly Story's spelling table.

**A trap recorded for the enabling change.** `_wire_kind()` is
`kind.replace("-", "_")`, which yields `metadata_formula` correctly but would yield
**`lettering_style` for `lettering-style`** where Story requires `font_pack` — the
one kind whose wire spelling is not mechanical. The exception goes in with the kind,
not before.

**Why holding is load-bearing, not polite.** The installed app is 1.4.213: an
unknown `metadata_formula` product throws in `parseContentStoreCatalog` and takes the
whole catalogue down — the exact outage class we already hit with 14 tags — and a
`font_pack` would parse but the persisted-catalogue defect would discard the saved
catalogue on every load.

**When 1.4.216 ships**: add both kinds to `SUPPORTED_KINDS`, add the
`lettering-style → font_pack` exception, re-author the 20 + 8 archives as `<slug>-v2`
with `min_app_version: "1.4.216"`, shelve, and install both kinds through Story's
installer; then re-test the saved/restored catalogue path.

## 10x. Content Packs reorganized: tabs, type pills, one row per pack (2026-09-22)

The page had grown four jobs into one long scroll — 149 release rows plus 149
preserved archives, each rendered as its own card.

- **Tabs**: **Shelved · Preserved · Drafts · Setup**, using the same `aria-pressed`
  toggle pattern as the Create tabs (every tab stays in the Tab order and announces
  selection). Shelved is the default; only the visible panel's controls are in the
  Tab order because hidden panels are not focusable.
- **Grouped by type, with pills**: each list is grouped by kind (Topic Pack, Series
  Formula, Visual Style, Thumbnail Recipe) with a count pill per group and a small
  kind pill on **every row**, so a pack's type reads at a glance.
- **A type filter row** on the shelved list — `All 149 · Topic Pack 48 · Series
  Formula 21 · Visual Style 44 · Thumbnail Recipe 36` — which narrows to one type.
- **One row per pack instead of a card**, which is what actually removed the
  scrolling: 149 releases and 149 archives now read as compact lists.
- **A regression this uncovered**: S12 moved the Create tabs from `aria-selected`
  to `aria-pressed` but the CSS still keyed the selected style on `aria-selected`,
  so the selected Create tab had silently lost its highlight. Styled on both now.
- **Preserved rows showed 60-character pack ids, not names.** Cause was
  pre-existing: `inspect_archive` recorded identity but never the pack's `name`, so
  there was nothing to show. It reads the document's `name` now, and the 149
  existing library entries were backfilled in place (`store-analysis/
  backfill_archive_titles.py`, atomic temp-and-rename so a running gateway never sees
  a partial file) — all 149 titled.
- **Verified**: all Node suites green (6 + 7 + 10 + 10 + 4); the library harness now
  runs the shelf helpers in the same script scope, since the new renderer groups by
  kind through them; `tests/check_admin_keyboard.mjs` passes on the new tabs (5
  controls, all reached and ringed); the 18-test delivery suite and the 9-test
  library suite pass; and both tabs were checked as rendered screenshots against the
  live shelf. Build `e32b0de-202609211938`, promoted.

## 10y. The preserved archives are load-bearing; the clutter was elsewhere (2026-09-22)

The owner asked whether the "Preserved archives" could be cleaned out. **They
cannot: they are the packs.** The shelf state holds metadata and a SHA-256; the
bytes Story downloads live only in `data/content-library/`. Proof already on
record — hiding the Wars archive (moving **one** file) made that product vanish from
the catalogue. Deleting the library would empty all 140 products and fail every
download.

- Size and shape: **148 archives / 155 MB** — thumbnail-recipe 36 (111 MB, the
  bulk), visual-style 45 (38 MB), topic-pack 47 (6.6 MB), series-formula 21 (0.1 MB).
- **One genuinely dead entry was removed**: an orphan archive for the historical
  `inked-crosshatch-storybook` identity, left by the mis-targeted bump whose publish
  the shelf rightly refused. Nothing referenced it; the library is now **148
  entries, 0 unreferenced**, with 140 products still live.
- **The real clutter was `_trash/superseded-rollback-*.app`** — every promote
  rotated the previous rollback there instead of deleting it, so 14 dead app builds
  (92 MB each) had accumulated. Pruned to **one** fallback beyond the sanctioned
  rollback in `backups/rollback-20260921/`: `_trash` went **1.8 GB → 678 MB**.
  `_trash/transfer-staging/` (586 MB) is deliberately kept — it is the original
  Store archive set and the recovery source for the whole re-authoring.

Rollback labels were checked while pruning: each `.app` contains exactly the build
its filename claims.

## 10z. The Gateway no longer depends on every Mac updating first (2026-09-22)

The owner asked whether publication really has to wait for the 1.4.216 DMG. **The
DMG was never the gate — the parsing client is.** `min_story_version` only narrated
compatibility; the row was still sent, and a kind an older Story does not know
throws inside `parseContentStoreCatalog`, which answers 500 for the **whole**
Content Store. Sending the row is the hazard, not the version field.

**Fixed in `products()`**: a product whose `min_story_version` is newer than the
client's `story_version` is now **withheld** rather than listed as incompatible.
So an old app never receives a row it cannot parse — publishing a new kind no longer
waits on every Mac updating. Two guards:

- Withheld only when the client **stated a parseable version**. A malformed
  `story_version` keeps the old behaviour and sees everything, because silently
  emptying the catalogue would be a worse failure than a stale row.
- Behaviour change to know: a client below a product's floor now sees **nothing**
  rather than an "incompatible" row. Measured live — `story_version=1.4.216` → 140
  products, `not-a-version` → 140, `1.0.0` → 0. Nothing below the 1.0.843 floor was
  installable anyway, and it is what prevents the outage.

Test: `test_a_client_too_old_for_a_product_does_not_receive_it` (19/19 in the
delivery suite). Build `6c7aefb-202609211958`, promoted.

**On the DMG itself.** `dist/Story Studio KH 1.4.216-arm64.dmg` exists and its
`app.asar` carries the support (17 `metadata_formula` hits). But it was built from an
**uncommitted tree** — 59 modified and 22 untracked files, and the last commit
touching `genstudio-content-store.ts` is an unrelated one. So it is a real build of a
real fix, but not a traceable release: if that tree is reset, the DMG's source is
gone. That is Story's to close before 1.4.216 is treated as shipped.

## 11. Exact next action / resumable handoff

- Branch `codex/content-pack-delivery`: promoted app code at `97bb3a5`
  (build `97bb3a5-202609210714`), later documentation/backup commits on top;
  worktree clean.
- Working instance promoted, healthy and verified (`type="Foreground"`, signature
  valid); test instance running on 8100.
- Stabilization plan `docs/superpowers/plans/2026-09-21-stabilization-plan.md`:
  **S1, S2, S4, S5, S6, S7, S8, S9, S10 done.** S3 is a documented watch item
  (measured, not demonstrated). Remaining: S11 (needs a coordinated Story Studio
  release) and S12 (optional keyboard-access pass on the admin page).
- Cloud keys are in: 16 of 17 providers configured, all discovering without
  errors, **124 models offered** and `/v1/models` serves 124 to the gateway key.
- Real work is verified both ways: a cloud chat job through the durable route
  (`output_text: "ok"`, settled `0/0/0`) and a local Hub transcription with the
  artifact returned.
- Cleanup convention (owner, 2026-09-21): old material is **moved to `_trash/`**,
  never hard-deleted. Applies to all future cleanups.
- Unverified corners: none blocking. S11 is external; S12 is a nice-to-have; the
  legacy Store restore boundary stays archive-only by decision.
