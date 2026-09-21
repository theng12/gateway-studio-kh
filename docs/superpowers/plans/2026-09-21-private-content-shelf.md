# Private Content Shelf Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax; the owner authorized execution and bounded parallel workers. Workers do not commit, publish, deploy or delegate.

**Goal:** Qualify new private Content Packs in Gateway and deliver them through Story's real verified installer.

**Architecture:** Extend the existing Gateway authoring and private archive shelf with a durable single-owner publishing authority. Reuse Story's existing wire protocol and installer, keeping historical Store identities outside the new authority.

**Tech Stack:** Existing Python/FastAPI/SQLite or atomic JSON, Node crypto and Story authoring/schema tooling, existing vanilla admin UI.

**Spec:** `docs/superpowers/specs/2026-09-21-private-content-shelf-design.md`.

## Execution status — 2026-09-21 takeover

The previous orchestrator session failed after implementing Tasks 1–3 but before
committing or recording them. A recovery session re-verified the source and found
the checkboxes below stale. Verified on the current uncommitted worktree
(`codex/content-pack-delivery`, base `b12550f`):

- **Task 1 — implemented and passing.** `tests/test_content_delivery.py` 15/15.
- **Task 2 — implemented and passing.** `tests/test_content_shelf_ui.js` with the
  existing UI suites 19/19. No rendered browser visual pass was performed.
- **Task 3 — implemented and passing.** `tests/test_content_delivery_integration.py`
  1/1 against real Story `6f3c4e3`; `tests/test_e2e.py` 521/521.
- **Regression found and fixed:** unconfigured `products`, `shelves` and `downloads`
  returned 422/415 instead of `503 CONTENT_STORE_NOT_CONNECTED`. See
  `Project Status.md` packet P0.
- **Not done (deliberately):** code-only Graphify refresh (the `graphify` binary is
  not on PATH in this session), and installed-app replacement/start (not
  authorized; the verified build is left at `menubar/build/gateway-kh.app`).
- Current durable record: `Project Status.md`.

## Global Constraints

- Base Gateway commit: `b12550f8a952c5209c2980b2581a2c4840a70af8`. Story source baseline: `6f3c4e3316854b5eb9bf6100f078064254f61445` (1.4.213).
- Four standalone delivery kinds only: series-formula, topic-pack, visual-style, thumbnail-recipe. Reject other kinds rather than mislabeling them.
- Never return 402; never create quotes, purchase receipts or historical ownership; never silently replace a pack/version or regenerate a lost signing identity.
- Private data, keys, transfer assets and generated graphs stay outside Git. No paid calls, real generation, live profile mutation or GitHub Actions.
- One test suite at a time. Workers coordinate before test execution. Independent source edits may run concurrently only with disjoint file ownership.
- Use current Story source as the authority for all wire fields, canonical signatures, manifest and registry validation; prove behavior with real consumers.

## Review Focus

- Missing identity after prior publication must not become a fresh authority.
- A valid archive from the historical transfer must remain archive-only even if its registry record still exists.
- A credential rotation must immediately revoke the old key and any still-live grant bound to it without changing durable owner identity.
- Host/header manipulation or redirects must not disclose grants or credentials.
- Publishing a newer version must preserve older downloads/project pins and must not install anything automatically.

## Task 1: Private authority, qualification and wire delivery

**Owned files:** create `gateway/content_delivery.py`, `gateway/content_signing.mjs`, `gateway/story_pack_validation.mjs`, `tests/test_content_delivery.py`; modify only `gateway/api.py`, `gateway/admin.py` as needed to wire routes. Existing helpers may be imported; request ownership before editing other files.

**Interfaces:** implement the exact admin and Story routes in the spec. `content_delivery` owns all durable private-shelf state and typed safe errors. `story_pack_validation.mjs` calls the real Story registry receipt verifier and archive validator and returns only validated public manifest/content metadata, registry revision and receipt identity; never executes archive content. `content_signing.mjs` accepts bounded stdin for Ed25519 operations using Node crypto; no command-line secrets, no shell.

- [ ] Write a behavior test before implementation. The initial missing-route assertion is:

```python
response = client.get('/admin/api/content-shelf')
self.assertEqual(response.status_code, 200)
self.assertFalse(response.json()['configured'])
```

- [ ] Run `.venv/bin/python -m unittest discover -s tests -p 'test_content_delivery.py' -v`; record the expected missing-feature failure.
- [ ] Implement durable setup, fresh-registry eligibility and one publication path shared by preserved archives and local draft exports. Derive all metadata from verified bytes, not editable cache claims. Enforce acknowledgement, collisions, closure and namespace/history exclusion.
- [ ] Implement current Story response shapes and signed restore/grant behavior. Source references: `electron/server/lib/genstudio-content-store.ts`, `genstudio-content-store-trust.ts`, `services/content-store-delivery.ts`, `content-packs/delivery-attestation.ts`. Read current source rather than copying mock authority code into production.
- [ ] Add real behavior cases for unconfigured reads, explicit setup/idempotency, corrupt/missing identity, historical identity, invalid manifest/receipt, same-version conflict, numeric update ordering, missing closure, symlink/bounds failures, wrong key, rotated key, expired/tampered grants and archive mutation. Signed payload assertions use independent Node verification, not the signing helper as its own oracle.
- [ ] Run covering tests under isolated environment and hand off route examples and exact results; do not run broader suites concurrently or mark real Story integration complete.

## Task 2: Content Packs page and repeatable authoring handoff

**Owned files:** `gateway/admin_ui.html`, new `tests/test_content_shelf_ui.js`, new `docs/content-packs-authoring.md`, and minimal entry-point naming/copy in `menubar/PackLibrarySection.swift` and `menubar/MainWindow.swift`. Do not edit API/backend files. Start once Task 1 response schema is frozen; source editing can overlap its backend tests, but run UI tests only with the orchestrator's test slot.

**Consumes:** admin shelf setup/status/publication routes in the spec plus existing archive import and authoring routes. Status/publication schema examples come from Task 1 and must be agreed before UI wiring.

- [ ] Write failing VM/controller behavior tests for unconfigured setup, explicit acknowledgement, successful shelf refresh, refused publication with preserved draft, repeated click/pending state and escaped pack metadata. Reuse the existing tests' harness style, not source-string assertions.
- [ ] Adapt existing Pack Library into Content Packs with Drafts, Shelved and Preserved archives clearly distinguished. Keep existing Topic Pack editor behavior. Add Copy authoring brief and package-import/review/shelve actions. State four supported types and actionable prerequisite errors; no pretend installed status.
- [ ] Make the authoring brief self-contained: setup first; use standalone skill/schema/registry; new reservation issued after private-shelf baseline; one original reusable product per pack; preserve provenance/licenses; validate/build/review deterministically; no commerce, paid generation or old-identity reuse; deliver `.sspack` and concise per-pack evidence. Drafts must not be auto-published.
- [ ] Run `node --test tests/test_content_shelf_ui.js tests/test_content_library_ui.js tests/test_pack_authoring_ui.js` with the test slot. Report observed successes and any limitations; main performs browser visual verification.

## Task 3: Real Story round trip and integration gate

**Owned files:** new `tests/test_content_delivery_integration.py`, new `tests/verify_story_content_delivery.mjs`; optionally extend `tests/prepare_authoring_fixture.mjs` without changing existing callers. No production or Story source edits without reassignment.

**Consumes:** Task 1 HTTP contract. Real Story source and dependencies at the current checked-out baseline; Node imports via Story's existing tsx loader. Never use live app API/profile.

- [ ] Build a temporary valid registry, initialize an isolated Gateway on a random port, then reserve/build a synthetic standalone pack using the real Story authoring APIs after the baseline. Point all Gateway support/data/env/Hub paths and Story data at fresh temp directories before importing runtime code.
- [ ] Run the test against missing delivery routes to capture expected failure, or against incomplete Task 1 with the exact observed gap. The core consumer assertions must include:

```javascript
assert.equal(installed.body.install.activated, true)
assert.equal(installed.body.install.version, '1.0.0')
assert.equal(after.body.account.owned[0].installState, 'installed_current')
```

- [ ] Use Story's REAL content-store router/service/installer against the actual isolated Gateway, not its mock Store. Reuse its `_helpers` test-only encrypted storage double in isolation. Verify restore and signature binding, download and installation, restart/revalidation, deliberate v2 update and retained v1, no purchases, and a tampered archive refusal.
- [ ] Run `.venv/bin/python tests/test_content_delivery_integration.py` only with the test slot. Return exit code, test count and semantic completion evidence; distinguish test-double OS encryption from real Keychain behavior.
- [ ] Main independently reviews all source changes, runs existing isolated backend/e2e and UI tests serially, updates the code-only graph, verifies the built app and then handles safe app replacement/start. Any failed consumer gate blocks a usable-pack claim.

## Integration decisions

The owner approved the product flow and continuous execution; no extra technical approval pauses are required for reversible work. New live authority setup, actual publication of a user pack and Story installation remain explicit UI actions. Source implementation and isolated verification do not exercise those actions against live data. No Story application modification is assumed; if the real consumer rejects this contract, reassess the smallest complete change before editing the separate project.
