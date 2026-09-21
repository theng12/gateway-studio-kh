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
- **Known untidiness:** the merged Wars/Treaties record has pack id
  `…historical-wars-treaties-world-order-v2` and item id
  `…archival-ledger.wars-treaties-world-order-v2`. Both are new and valid, and
  the mapping points both old ids at the item id; the slugs simply disagree.
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
