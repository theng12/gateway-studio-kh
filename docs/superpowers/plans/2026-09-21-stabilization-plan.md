# gateway-kh stabilization plan — 2026-09-21

Remaining known issues, with fix steps and acceptance checks, after the recovery
session that fixed the story content-route regression (P0) and stood up the
two-instance workflow. Work through the packets in order; the owner promotes test
work to the working instance explicitly.

State this plan starts from: working instance `/Applications/gateway-kh.app`,
promoted 2026-09-21 to build `5f94b4a-202609210554` on port 8000 (real profile);
test instance on port 8100 running the same committed source. Full evidence:
`Project Status.md`.

**Execution log.**
- 2026-09-21: S1 committed (`5f94b4a`), S2 fixed (light-mode hover + guard),
  S8 promoted to the working instance and verified. Old app-version backups and
  build outputs consolidated to `backups/rollback-20260921/`.
- S9 resolved: historical material moved to `_trash/` under the owner's
  recoverable-delete convention (never hard-deleted).
- User-requested app change (`6ed4c44`): the app is now a regular Dock app
  (Dock icon, ⌘-Tab, re-openable window) instead of a menu-bar-only agent;
  promoted as `6ed4c44-202609210701`. This also removes the accessory-process
  caveats that S4's `test_ui.py` runs relied on; its comments were updated, but
  the suite still needs Accessibility to run.
- User-requested UI fixes (`97bb3a5`, promoted as `97bb3a5-202609210714`):
  provider cards no longer clip their buttons in a narrow pane; a key-status
  filter (All / Key set / No key) with a keyless count; the Voices bulk controls
  moved above the table with Select all shown / Select none. The native Voices
  layout still needs the Accessibility-enabled S4 run to confirm screen geometry.
- S4 completed on the promoted build: 127/127 UI checks pass after fixing test
  drift (Content Packs sidebar row, stale Jobs heading, empty-Jobs skip).
- S7 completed: code-only Graphify refresh (2692 nodes / 6795 edges).
- S10 completed: identity backup script, its test suite, and a verified
  backup → damage → restore drill on an isolated profile.

## Packet index

| ID | Kind | Priority | Summary |
|---|---|---|---|
| S1 | defect | high | ~~Commit the verified work so it cannot be lost~~ — **done 2026-09-21 (`5f94b4a`)** |
| S2 | defect | medium | ~~Light-mode table hover is unreadable~~ — **done 2026-09-21** |
| S3 | perf | watch | Content authority read twice per request — measured, not demonstrated |
| S4 | verification | high | ~~Native UI (`test_ui.py`) on the promoted build~~ — **done 2026-09-21, 127/127** |
| S5 | verification | medium | Real generation through the working instance |
| S6 | verification | low | Real-Hub fleet suite, knowingly |
| S7 | verification | low | Code-only Graphify refresh |
| S8 | decision | high | ~~Promote Content Packs build to working~~ — **done 2026-09-21 (`5f94b4a-202609210554`)** |
| S9 | decision | medium | ~~Historical Store material~~ — **done 2026-09-21: moved to `_trash/`** |
| S10 | decision | medium | ~~Back up the private shelf signing identity~~ — **done 2026-09-21** |
| S11 | scope | low | Content kinds beyond the four standalone kinds |
| S12 | scope | low | Keyboard-access pass on the admin page |

"Defect" = reproducible defect; "verification" = unverified area; "decision" =
owner product/operational decision; "scope" = deliberate limitation or
Story-side dependency.

---

## S1 — Commit the verified work (high)

**Why.** Everything in `worktrees/handoff-completion` is uncommitted; one
`git clean`/reset loses the whole Content Packs feature and the P0 fix. The
branch is also one commit behind `main` (PR #7, unrelated UI version footer).

**Steps.**
1. Review the final change set (`git status`, `git diff`, untracked files) and the
   preserved copy at `backups/pre-recovery-20260921-115619/`.
2. On branch `codex/content-pack-delivery`, commit the Content Packs feature and
   the P0 fix as one reviewable commit (or a small series). Do not touch the
   owner's `repository/` main checkout or its `.DS_Store`.
3. Decide whether to fast-forward/merge `main` (PR #7) into this branch. It is
   unrelated UI code; merging keeps the eventual promotion based on current main.
4. Re-run the full gate on the committed snapshot (same list as `Project
   Status.md` section 7).

**Acceptance.** `git status` clean in the worktree; full gate green on the
committed SHA; `main` untouched.

**Blocked by.** Owner's commit policy (routine commit is normally fine, but this
project's mission forbids committing without authorization).

## S2 — Light-mode table hover is unreadable (medium)

**Why.** `gateway/admin_ui.html` line ~154:
`tbody tr:hover { background: #1a202b; }` sits **outside** the
`prefers-color-scheme: dark` block. The page is `color-scheme: light dark` and
`--text` is `CanvasText`, so in light mode a hovered table row is near-black with
black text. Verified by CSS inspection; the rest of light mode renders correctly
(screenshot reviewed). Pre-existing, not introduced by the Content Packs work.

**Steps.**
1. Replace the literal with the same token pattern the striped rows use:
   `tbody tr:hover { background: color-mix(in srgb, CanvasText 9%, Canvas); }`.
2. Add a one-line check to `tests/test_content_library_ui.js` or a CSS assertion
   that no `tbody tr:hover` rule uses a fixed dark literal, so it cannot regress.
3. Render `/admin#models` in light mode and hover a row (or assert the computed
   background token) to confirm contrast.

**Acceptance.** Light-mode hover uses a light surface; dark mode is unchanged;
UI suites stay green.

**Done 2026-09-21.** Changed `tbody tr:hover` to
`background: color-mix(in srgb, CanvasText 9%, Canvas)`. Added a regression guard
to `tests/test_content_library_ui.js`; a positive control confirms the guard
rejects the old `#1a202b` literal. UI suites 20/20. The running test instance was
restarted and now serves the token-based rule.

## S3 — Content authority read twice per request (medium)

**Why.** `_configured_state()` costs ≈55 ms because it spawns Node to re-inspect
the signing identity. The P0 `require_content_authority` dependency and the
handler each call it, so a configured content request measured ≈113 ms. Correct
and fail-closed, but wasteful; not yet a demonstrated problem at private-shelf
scale.

**Steps (choose one, smallest first).**
1. Thread the dependency's state into the handler instead of re-reading: have
   `require_content_authority` return the state (or store it on `request.state`)
   and let the public functions accept it. Keep `_configured_state()` as the
   fallback for non-HTTP callers.
2. Or cache the signing-identity inspection keyed on the key file's
   `dev/ino/size/mtime/ctime`, re-statting on every call so deletion/replacement
   still fails closed.
3. Do **not** weaken or remove the signing-identity check for speed.

**Acceptance.** One authority read per request (measure as in `Project Status.md`
P4); all content delivery tests, `test_handoff_api` and `test_e2e` green;
damaged-key recovery still returns `CONTENT_AUTHORITY_DAMAGED`.

**Disposition 2026-09-21 — watch, not scheduled.** The cost is measured but not
demonstrated to matter: content routes are hit only when Story Studio opens its
Content Packs page, the shelf is private and single-owner, and 113 ms is well
inside any user-visible budget. Both repairs carry real risk (threading state
through nine functions, or a cache that must stay fail-closed against a
replaced key), so per the project rule against speculative work this stays open
as a monitor item with the measured number and upgrade paths above. Revisit if a
real shelf ever shows the latency.

## S4 — Native UI verification on the promoted build (high)

**Why.** `tests/test_ui.py` drives the **installed** app. It has not run against
the Content Packs build, so the renamed sidebar section ("Pack Library" →
"Content Packs"), the window-size invariant, and the browser-open action are
unverified in the real native shell.

**Steps.**
1. After S8 promotion, run `.venv/bin/python tests/test_ui.py` (needs
   Accessibility permission; skips Hub checks when the Hub is stopped).
2. Confirm every section still opens and the window does not resize at the
   860×560 minimum (the documented invariant).
3. Press "Open Content Packs in Browser" and confirm it reaches
   `http://127.0.0.1:8000/admin#packs`.

**Acceptance.** 128 checks pass (or every skip is explained as environment);
no window growth; the Content Packs entry opens the page.

**Done 2026-09-21.** Accessibility granted; `tests/test_ui.py` ran against the
installed `97bb3a5-202609210714` and reported **all 127 checks passed** with 5
environment skips (no jobs yet; no fleet controllers). The window-size invariant
held on every section at the forced 700 pt height. Test drift found and fixed:
`SIDEBAR`/`SECTIONS` now include Content Packs, the Jobs heading string was
stale, and the Jobs data checks skip on an empty gateway. Details in
`Project Status.md` §10d.

## S5 — Real generation through the working instance (medium)

**Why.** No paid/generation call has ever been made in this recovery. Model
routing, provider credentials and Hub dispatch are exercised only by mocks.

**Steps (owner-mediated; costs money or local compute).**
1. In the working instance, enter the provider keys you intend to use
   (Providers page, local only — never in chat).
2. Run one cheap text/chat generation from Story Studio, then one local
   Studio Hub generation (image or TTS) if the Hub is running.
3. Confirm the job appears in Jobs with balanced `0/0/0` settlement and no 402.
4. Record provider/model, outcome and any error verbatim (sanitized).

**Acceptance.** One cloud and one local job complete end to end; no 402 anywhere.

## S6 — Real-Hub fleet suite (low)

**Why.** `tests/test_fleet_standalone.py` (~50 checks) deliberately uses the
owner's real Studio Hub and requires `GATEWAY_ALLOW_LIVE_GENERATION=1`.

**Steps.** Run knowingly when the Hub is healthy:
`GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python tests/test_fleet_standalone.py`.
If the two transcription checks fail, restart Studio Hub before reporting a
defect (documented transient).

**Acceptance.** 50/50, or a Hub-restart-recovered run.

## S7 — Code-only Graphify refresh (low)

**Why.** `AGENTS.md` asks for `graphify extract . --update --code-only` after
structural work. The binary is not on PATH in this session.

**Steps.** From a shell where `graphify` is available, run it (code-only, no paid
LLM extraction) after S1 commits. Confirm `graphify-out/` stays gitignored.

**Acceptance.** Graph rebuilt; no generated files staged.

## S8 — Promote the Content Packs build to working (high, owner command)

**Why.** The feature and the P0 fix live only in the test instance. Story Studio
cannot use the Content Packs shelf until the working instance carries it. This is
the only step that changes what the owner uses.

**Steps.** Follow `test-instance/README.md`: freeze the SHA (S1), run the full
gate, `sh menubar/build.sh --no-install`, back up the installed bundle, quit the
app, confirm port 8000 free and PID changed, replace, relaunch, verify `/health`
build and signature, confirm profile files unchanged, record it. Rollback =
restore the backed-up bundle.

**Acceptance.** `/health` reports the new build; strict/deep signature valid;
real profile unchanged; Content Packs page reachable at `/admin#packs`.

**Note.** Even after promotion, Story Studio's content routes return
`CONTENT_STORE_NOT_CONNECTED` until the owner runs explicit shelf setup; that is
correct and changes nothing for model generation.

## S9 — Legacy Store restore stays archive-only? (medium, owner decision)

**Why.** Transferred historical Store packs (167 archives / 115 pack IDs) cannot
be restored or reinstalled without the original Store account/receipt/signing
authority. The new private authority deliberately refuses to reissue them.

**Steps (choose one).**
1. **Accept archive-only** (recommended): keep them preserved and documented; the
   private shelf serves newly reserved packs only. No code.
2. **Investigate a supported bridge** if the original authority can be brought
   back upstream without changing Story's single endpoint: produce the concrete
   missing contract and owner decision first; never mint ownership or re-sign.

**Acceptance.** Decision recorded in `Project Status.md`; no trust bypass added.

**Done 2026-09-21.** The owner chose to retire the historical material rather
than keep it live. It was moved to `_trash/transfer-staging/` (586 MB) under the
new recoverable-delete convention — not hard-deleted — and the private shelf
still refuses to reissue historical identities. See `_trash/README.md`.

## S10 — Back up the private shelf signing identity (medium)

**Why.** Setup writes `DATA_DIR/content-delivery/{authority.json,signing-key.pem,
initialized}`. The key is deliberately never regenerated; if it is lost, future
releases cannot be signed and Story's trust binding breaks. Nothing backs it up
automatically, and `README.md` only tells the owner to do it.

**Steps.**
1. After the owner runs shelf setup, add a documented backup step (encrypted
   local copy of the three files, kept with the release archives, outside Git).
2. Optionally add an admin route or startup check that warns when the identity
   exists but has never been backed up (do not copy the key automatically, and
   never to a synced/cloud location).
3. Test that restoring the three files makes `GET /admin/api/content-shelf`
   report `configured: true, available: true` again.

**Acceptance.** Documented procedure plus a verified restore drill on the test
instance.

**Done 2026-09-21.** Added `scripts/backup_content_authority.py`: it copies the
three authority files at mode `0600`, verifies each by SHA-256, and refuses to
overwrite an existing backup or to copy a missing/world-readable identity. Its
rules are covered by `tests/test_content_authority_backup.py` (4/4). A real
restore drill ran on an isolated profile against the actual Story registry
(read-only): setup → backup → delete `signing-key.pem` (shelf reports
unavailable) → copy the backup back → shelf reports configured and available. The
procedure is documented in `docs/content-packs-authoring.md`. The optional
"never been backed up" warning was deliberately not added — it would need new
durable state to be truthful, and the documented step is enough for a
single-owner shelf.

## S11 — Content kinds beyond the four standalone kinds (low)

**Why.** Story 1.4.213's content parser exposes only Series Formula, Topic Pack,
Visual Style and Thumbnail Recipe. Subtitle-style, scene-effect, overlay,
lettering-style and metadata-formula kinds are refused with a clear message.

**Steps.** No Gateway work. When the owner wants those kinds, coordinate a Story
Studio delivery update first, then extend `SUPPORTED_KINDS` in
`gateway/content_delivery.py`, `story_pack_validation.mjs` and the UI labels, and
re-verify the wire shape against the updated Story source.

**Acceptance.** Either left as documented scope, or green after the Story update.

## S12 — Keyboard-access pass on the admin page (low)

**Why.** Rendered desktop/narrow screenshots and behavioral DOM tests exist, but
keyboard focus order, focus visibility, and the acknowledgement controls were not
exercised with a keyboard.

**Steps.** Tab through `/admin#packs` on the test instance: setup acknowledgement,
Refresh, Copy brief, draft controls, archive import. Check focus is visible and
no control is unreachable. Fix any gaps with native focus styles.

**Acceptance.** Every interactive control reachable and visibly focused.

---

## Already closed (for context — do not re-open without evidence)

- **P0** unconfigured `/v1/content/*` precedence — fixed and covered
  (`test_handoff_api` 11/11).
- **B01–B09, U01–U16, P01–P05** — implemented and verified per
  `_handoffs/2026-09-20-local-completion.md`; no re-verification in this session
  beyond the suites they cover.
- **F01–F04** (Cancel action, Outputs library, draft persistence, adaptive Create
  form) — recorded as done; not re-verified here.
- **F05 authoring + private delivery + archive shelf** — implemented and verified
  (`test_content_delivery` 15/15, real-Story integration 1/1, `test_e2e` 521/521).

## Working method

1. Pick the highest-priority unblocked packet; state objective, files, proving
   checks.
2. Reproduce before fixing; smallest complete root-cause repair; add regression
   coverage; verify success, failure and recovery.
3. Run the focused suite, then the full gate at coherent checkpoints.
4. Self-review the whole diff in a separate pass (not independent review).
5. Update `Project Status.md` and the packet row here; continue.
6. Promotion to the working instance only on the owner's explicit instruction.
