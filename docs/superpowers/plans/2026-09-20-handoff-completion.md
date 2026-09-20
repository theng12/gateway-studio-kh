# Gateway KH handoff completion implementation plan

Execution status is recorded in [`_handoffs/2026-09-20-local-completion.md`](../../../_handoffs/2026-09-20-local-completion.md).
The checklist below preserves the original acceptance specification; do not
mistake a planned item for a completed feature. Tasks 1–7 have implemented repairs
and isolated evidence. Task 8 is an archive-only partial delivery: trusted
installation has a trust/ownership blocker, while offline authoring/preview/export
is separately unfinished. Tasks 9–10 record local integration and installation in
the execution record.

> **For agentic workers:** Use subagent-driven-development or executing-plans for each assigned task. Read the task brief, global rules and project contracts; do not commit or delegate. Only the orchestrator edits this plan and the handoff ledger.

**Goal:** Resolve the handoff's B01–B09, U01–U16, P01–P05 and F01–F05, preserve restored private data, and install/start the verified Apple Silicon app ready for local credential entry.

**Architecture:** Keep the existing SQLite worker, FastAPI routes, provider registry and native AppKit shell. Repair shared lifecycle and eligibility rules first; reuse authenticated API helpers and existing artifact/voice routes for the interface. Implement Content Pack delivery only against the current Story Studio consumer contract and preserve its trust checks.

**Tech stack:** Python 3.11, FastAPI, SQLite, AppKit/Swift, embedded HTML/JavaScript; existing dependencies first.

**Spec:** `_handoffs/2026-09-20-open-issues.md`, `_handoffs/2026-09-20-new-computer-handoff.md`, `AGENTS.md`, and the owner's instruction to complete repairs and install/start locally.

## Global constraints

- Astra orchestrates and independently verifies; bounded workers use gpt-5.6-luna at max effort.
- Always apply Ponytail and Graphify. Apply Impeccable to UI and relevant Superpowers workflows. Existing owner authorization permits continuous execution and disjoint parallel workers.
- Work in `codex/handoff-completion` under the Gateway container. Keep the original checkout and its modified .DS_Store intact.
- Never return HTTP 402; preserve zero-credit catalogue/settlement fields, source labels, unknown values and non-blocking catalogue freshness.
- No paid generation, live Hub generation, hosted Actions, voice sharing, key disclosure, automatic replay of interrupted jobs, private-data commits or replacement of the configured profile.
- Installation/start of Gateway is authorized. Story/Hub application data and trust/ownership remain protected. Optional transferred job history remains archived.
- Workers own disjoint files; hand back code and focused test evidence. Astra reviews and runs integrated checks before closure.

## Review focus

1. Cancellation racing upstream completion must preserve truthful state and any produced artifacts (Task 1).
2. A crash after provider acceptance must never cause automatic resubmission (Task 1).
3. A lost submit response must retain the attempt's idempotency key across retries/restarts (Tasks 3, 5).
4. Rotated keys, redirects, missing artifacts and malformed payloads must not leak credentials or look successful (Tasks 2, 4).
5. Pack traversal, symlinks, duplicate members, hash failures and absent ownership/signatures must never bypass the Story installer (Task 8).

## Execution and ownership

Wave A: Tasks 1, 3, 4, 6 can run in parallel; Astra owns Task 2 and contract investigation for Task 8. Task 5 follows Tasks 1/4. Task 7 follows the backend data contract. Task 8's implementation follows consumer inspection. Tasks 9/10 integrate all results.

### Task 1: Durable lifecycle and protected inputs — B01, B02, B03, B08

**Files:** `gateway/store.py`, `gateway/worker.py`, `gateway/retention.py`, `gateway/adapters/base.py`; new `tests/test_job_recovery.py`, `tests/test_retention_inputs.py`.
**Interface:** Keep existing job states compatible with Story. Add durable recovery/cancellation/retry metadata as needed and expose it through stored rows. A requested running cancellation is distinct from confirmed cancellation. Never dispatch unknown accepted work again. Keep any late artifact discoverable. Retention reads protected input paths from nonterminal/recovery-held rows.

- [ ] Reproduce crash/reopen, late success/failure, simultaneous cancellation and pruning old referenced uploads using disposable databases and mock adapters.
- [ ] Implement conditional transitions, persisted retry classification and explicit unknown-outcome recovery. For asynchronous identity not yet durably available, fail honestly with actionable recovery rather than pretending automatic reconciliation is supported.
- [ ] Test crash before dispatch, after simulated acceptance and after artifact creation; assert at most one upstream generation. Test both cancellation/completion orderings and preservation of outputs.
- [ ] Test age/size pruning: referenced old uploads survive, unreferenced old uploads are reclaimed, recent/pinned outputs remain protected.
- [ ] Independent review; report any adapter-specific reconciliation gap rather than silently close B01.

### Task 2: HTTP/auth/eligibility contracts — B04, B05, B06, B07, B09

**Files:** `gateway/api.py`, `gateway/admin.py`, `gateway/config.py`; new `tests/test_handoff_api.py`.
**Interface:** Quote confirmation rejects mismatched models; expires_at represents the actual quote TTL. Create options derive from `registry.models.enabled_models(modality)`. Both public/admin guards consider actual peer/locality as well as configured bind; forwarded headers cannot grant loopback trust. Existing typed artifact error remains 410 ARTIFACT_GONE.

- [ ] Add failing isolated tests for quote mismatch/expiry, read-time deletion and configured-but-keyless Create options.
- [ ] Implement fixes in the shared readers/helpers and trace all callers, including admin artifact reads.
- [ ] Test an isolated real Uvicorn socket with misleading loopback configuration: non-loopback client missing/wrong token rejected, valid token succeeds. Disable proxy-header trust for the test and define supported launch behavior.
- [ ] Preserve legacy model-less quote fallback and all Story catalogue/settlement fields.

### Task 3: Browser action correctness — U04, U07, U13, U14

**Files:** `gateway/admin_ui.html`; new `tests/test_console_actions.js`.
**Interface:** `api()` returns parsed objects; Generate attaches the accepted job, Keep renders server state; ambiguous submission retains an idempotency key. Poll reads are serialized and cancelled jobs terminal. Provider structural support is labelled supported, distinct from configured availability.

- [ ] Turn the existing offline Generate/Keep diagnostic into failing acceptance tests with fake DOM/network.
- [ ] Remove double parsing, preserve submission identity after lost responses, and guard stale callbacks when switching jobs.
- [ ] Render provider errors/retry and preserve form edits; test 401/500/network failure and successful recovery.
- [ ] Browser fixture inspection after integration; ensure no action silently reissues generation.

### Task 4: Native transport and visible failures — U01 helper, U05–U12, U15, U16

**Files:** `menubar/GatewayAPI.swift`, `menubar/MainWindow.swift`, `menubar/OverviewWindow.swift`, `menubar/VoicesWindow.swift`, `menubar/FleetSection.swift`, `menubar/SettingsSection.swift`; new focused native tests.
**Interface:** Provide `GatewayAPI.download(_ path: String, completion: @escaping (Result<Data, GatewayAPI.APIError>) -> Void)` or the equivalent using the helper's actual error type; publish its exact signature to Task 5. Authenticate same-origin requests, reject redirects that would leak credentials, validate HTTP status and empty bodies, return on the main queue.

- [ ] Read existing native helper/error types, add asynchronous byte downloads and meaningful errors; test authenticated success, 401, 410, invalid/empty responses and slow responses.
- [ ] Add WebKit loading/error/retry and refreshed key injection on rotation without discarding healthy edits.
- [ ] Separate Overview failure from empty history; surface voice refresh/bulk failures, retry annotation status reads without restarting transcription, and report per-site fleet discovery failures.
- [ ] Reuse download behavior for voice previews; distinguish missing audio, bad audio, authentication and transport errors. Correct key-rotation instructions and stale lifecycle comments.
- [ ] Compile and inspect native failure states with fixtures; preserve window size/accessibility.

### Task 5: Complete native Create and Outputs — U01 consumers, U02, U03, F01–F04

**Files:** `menubar/CreateSection.swift`, `menubar/JobsWindow.swift`, `menubar/PageKit.swift`; helper files only where existing files cannot reasonably own the behavior.
**Dependencies:** Task 1 cancellation/recovery semantics and Task 4 download helper.

- [ ] Persist per-lane drafts, selected job and ambiguous-attempt idempotency key; never submit on app startup or navigation.
- [ ] Add searchable model-compatible voice choice with explicit advanced ID entry; changing models never silently substitutes a speaker.
- [ ] Use catalogue capability limits/source labels in fields; unknown stays unknown and chosen values survive refresh.
- [ ] Use flexible form controls/top labels and scrolling; minimum window cannot grow or clip primary actions.
- [ ] Wire Cancel with truthful backend semantics and make Jobs a usable output library: all variations, authenticated preview/save/open, pin/unpin, missing-file state and reattachment.
- [ ] Test state restoration, lost response, model changes, cancelled/running jobs and multiple outputs; visually inspect all lanes at minimum size.

### Task 6: Reproducible packaging and honest defaults — P01, P02, P03, P05

**Files:** `data/voices.json`, `requirements.txt`, `menubar/build.sh`, dependency lock/constraint file, new build tests. Orchestrator owns README/governing docs.

- [ ] Replace the shipped source-Mac voice catalogue with an honest empty/default resource; never edit the owner's writable catalogue.
- [ ] Use one tested dependency input for development and bundling; cache stamp includes Python asset/architecture as well as dependency identity.
- [ ] Fail required icon/signing operations and verify the resulting bundle; inject failure to prove unsuccessful build cannot claim Built/Installed.
- [ ] Test fresh Application Support shows no ghost usable voices and imported fixture voice resolves; preserve arm64/macOS 13+ workflow.
- [ ] Build locally with --no-install; report dependency/runtime identity and reproducibility limitations.

### Task 7: Portable private-data references — P04

**Files:** new `scripts/relocate_profile.py` and fixture tests; existing path readers only if needed.

- [ ] Inventory catalogue reference_path, asset_path, artifacts_json, params._upload_path and kept-pin paths from a supplied backup in read-only/dry-run mode.
- [ ] Map only paths under an explicit old root to validated existing destination files; preserve IDs, permissions, licenses and bytes, reject escapes/conflicts.
- [ ] Apply only to an explicit copied profile with backup and atomic database/JSON updates; test different-home fixtures and failure rollback.
- [ ] Keep the owner's optional history archived; document validated migration capability separately from a live restore.

### Task 8: Real personal Content Pack workflow — F05

**Files:** new `gateway/content_packs.py`, associated isolated tests, existing content API stubs, library UI after route contract is settled.

**Continuation finding:** Story's registry validates at revision 289 (271 records).
Its existing builder supports unsigned Store-profile archives and keeps that
structural validation separate from signed ownership receipts. Do not treat
missing Store credentials as a reason to stop offline authoring work. Reuse the
actual schema/builder and registry authority; do not copy a reduced schema into
Gateway or create a second identity registry. Story dependencies are not installed
in its checkout yet. Production trust and receipt/Keychain checks remain mandatory
for installation, independently of authoring/export readiness.

- [ ] Read current `/build/Developer/story-studio-kh` consumer source and applicable instructions. Record required manifests, account/quote/purchase/receipt/download fields, trust/identity/keychain rules and supported install entry point before implementation.
- [ ] Implement local import/inspect/hash/archive validation and inventory in private Application Support. Preserve imported archives; reject unsafe archives and incompatible manifests.
- [ ] Implement real same-origin delivery using verified consumer protocol. Never manufacture signatures, ownership or fulfilled purchases for undeliverable files.
- [ ] Add library UI with inspect/import/download and one supported authoring/preview/export kind using actual schema; keep transferred authoring sources intact.
- [ ] Validate real fixture download/install/reinstall through supported consumer code in a disposable profile. If external trust identity is required, keep it a named blocker instead of weakening checks.

### Task 9: Integrated verification and independent review

- [ ] Run all new focused tests plus isolated e2e, audit regression, job claim and native state suites. Confirm no 402 and no paid dispatch.
- [ ] Rebuild and verify signature/runtime; refresh code-only Graphify graph and inspect changed call chains.
- [ ] Conduct independent whole-diff review; fix important findings and verify corrections.
- [ ] Run rendered browser/native fixture checks. Real-Hub generation tests remain outside authorized smoke testing; record that limitation.
- [ ] Update the issue ledger and AUDIT-BACKLOG with measured evidence per ID; leave any incomplete acceptance explicitly open.

### Task 10: Safe installation, startup and credential handoff

- [ ] Inspect current Gateway process/profile and make a fresh backup inside the Gateway container before any migration or replacement.
- [ ] Install verified app through documented build workflow, start on configured loopback endpoint and confirm health build matches bundle with zero generation.
- [ ] Reverify restored recordings, selections and permission fields; retain backup/transfer and archived history.
- [ ] Open Settings/Providers for local credential entry and notify owner. Provider keys are not required to finish mocked repairs; never request secret values in chat.
- [ ] Report installed/running build, verified acceptance, remaining external limitations and exact local credential next steps.
