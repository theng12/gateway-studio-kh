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
  **promoted 2026-09-21 to build `5f94b4a-202609210554`**, port **8000**, real
  profile `~/Library/Application Support/gateway-kh/`. Signature valid; profile
  verified byte-identical across the promotion; rollback bundle at
  `backups/rollback-20260921/`.
- **Test instance** — where new work is verified: `test-instance/` in the
  container root, source `worktrees/handoff-completion`, port **8100**, isolated
  profile (`test-instance/support`, `test-instance/data`), no real credentials,
  no Studio Hub. Commands: `./run.sh`, `./status.sh`, `./stop.sh`; see
  `test-instance/README.md`. Currently running (Content Packs work + P0 fix).
- Promotion procedure and rollback: `test-instance/README.md`.
- Remaining known issues and the ordered fix plan:
  `docs/superpowers/plans/2026-09-21-stabilization-plan.md`.

## 2. Current checkout, branch, base SHA, runtime

- Container: `/build/Developer/gateway-kh`
- Normal checkout: `repository/` — branch `main`, HEAD `736d7b0`, clean except a
  preserved `.DS_Store` modification. Untouched.
- **Working checkout: `worktrees/handoff-completion/`, branch
  `codex/content-pack-delivery`, base SHA `b12550f`** (one commit behind `main`;
  `main` only adds PR #7, an unrelated sidebar version footer).
- Story source dependency: `/build/Developer/story-studio-kh` at
  `6f3c4e3` (1.4.213), `node_modules` present. Installed
  `/Applications/Story Studio KH.app` 1.4.213.
- Running gateway: build `b12550f-202609201849`, pid 52952 —
  **older than this worktree's source. The new Content Packs code is NOT live.**
  `[MEASURED]`
- Worktree `.venv` Python 3.11.16; Node v26.8.2.

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

## 11. Exact next action / resumable handoff

- Branch `codex/content-pack-delivery`: promoted code at `5f94b4a`, documentation
  commit on top; worktree clean.
- Working instance promoted, healthy and verified; test instance running on 8100.
- Stabilization plan `docs/superpowers/plans/2026-09-21-stabilization-plan.md`:
  S1, S2 and S8 done. Next unblocked packet: S3 (authority read cost), then S12.
- Cleanup convention (owner, 2026-09-21): old material is **moved to `_trash/`**,
  never hard-deleted. Applies to all future cleanups.
- Verification gaps needing the owner or environment: S4 native UI suite on the
  promoted build, S5 real cloud + local generation, S6 real-Hub fleet suite,
  S7 code-only Graphify refresh, S12 keyboard-access pass.
