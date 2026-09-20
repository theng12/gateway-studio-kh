# Gateway KH: product and reliability audit

Audit baseline: `e360fbb`. Findings below distinguish direct measurements from recommendations. Source line numbers refer to that baseline; named functions remain the reference after repairs.

`[MEASURED]` means personally checked against source, a runnable check, or the running interface. `[REPORTED]` means a worker finding awaiting that check. `[ASSUMED]` means a stated planning assumption, not an established product fact.

## Assessment

[MEASURED] The baseline isolated end-to-end suite passed **521 checks** (`.venv/bin/python tests/test_e2e.py`, log `/tmp/gateway-audit-e2e-baseline.log`). The native app and `/health` answered during this audit. This establishes existing contract coverage; it does not prove every cloud model works, restart recovery works, or the interface is usable.

My design assessment: retain the native app and the single gateway API. Concentrate on completing the everyday workflow—choose, configure, generate, inspect, save, recover—before increasing the number of features. A calmer interface with reliable state will help more than a cosmetic reskin of the same incomplete forms.

## Prioritized findings

### P1: keyless network mode admits arbitrary bearer tokens

[MEASURED, source] `gateway/api.py:167` and `gateway/admin.py:69` only reject a token mismatch when the configured key is nonempty. `gateway/config.py:267` explicitly describes keyless use as a loopback development convenience; changing the bind address does not add an equivalent key requirement to either guard. The live host was loopback during the audit; this is a configuration-triggered defect, not a claim of an observed intrusion.

Repair: refuse authenticated API access when bound beyond loopback without a configured key. Preserve the documented local development behavior and live key rotation.

### P1: cancelling queued work can still dispatch it

[MEASURED, source] `Worker._loop` reads a list from `due()` before processing it (`gateway/worker.py:185–197`). `JobStore.mark_running` unconditionally changes a row to running (`gateway/store.py:189–196`). Cancelling the second job while the first is executing therefore does not invalidate the worker's already-read list. The claim must check the current database state, and the worker must respect a failed claim.

Repair: an atomic queued-only claim. This is separate from stopping an already accepted upstream request; do not imply a provider cancellation has happened merely because a local row changed.

### P1: interrupted running work has no recovery path

[MEASURED, source and isolated reproduction] `mark_running` clears `next_action_at`; `due()` requires a non-null value. `JobStore.__init__` initializes schema but does not reconcile interrupted jobs. A temporary database was created, a job was claimed, and the store was closed/reopened: `state_after_reopen='running'`, `next_action_at=None`, `due_count_after_reopen=0`. A service restart can therefore leave a stored running job outside the worker's queue indefinitely.

Next work: persist upstream job identity and reconcile before retrying. If acceptance cannot be established, expose an interrupted/unknown outcome with an explicit recovery action. Do not blindly resubmit: a cloud provider may already have accepted the first request. This requires adapter-by-adapter work, not a global “set running back to queued” patch.

### P1: Create can silently change the model or lose job state

[MEASURED, UI] Selected **Eleven Turbo v2.5** in Audio, pressed **Refresh models**, and observed the selection reset to **Eleven Flash v2.5**. `CreateWorkspace.refreshModels` removes all menu items without restoring the selected identity. Activation also invokes this refresh.

[MEASURED, source] `updateGenerateEnabled` checks only whether models exist. It can re-enable Generate during pending work. Poll failures are discarded (`startPolling`), and a cancelled job renders through the non-success spinner branch (`render`).

Repair: preserve explicit selection, protect pending state, retry status reads without resubmitting, and render every terminal state clearly. Follow-up: retain drafts and job identity across app restarts, with recovery from Jobs.

### P1: artifact Save and preview bypass the authenticated client

[MEASURED, source] `CreateSection.swift` loads images with `NSImage(contentsOf:)`, transcripts with `String(contentsOf:)`, and Save bytes with `Data(contentsOf:)`; these calls do not attach the gateway bearer token. `saveArtifact` also discards both download and write errors (`:555–562`). The admin guard requires a token when configured beyond loopback, even for the native app's loopback requests.

Next work: one asynchronous authenticated byte-download method in the existing `GatewayAPI`; validate HTTP status, derive a useful extension, write atomically, and report failures. Use it for image/transcript preview and Save. External Open needs a downloaded local file. Test the same actions with network binding enabled. No paid generation is necessary: mock artifacts suffice.

### P2: known batch-only chat rows are still offerable

[MEASURED, UI and source] The Models console displayed `:batch` OpenRouter rows with enabled-capable checkboxes. `ModelInventory.can_publish` and `chat_can_serve` did not exclude those routes, despite the recorded failure contract in `AGENTS.md`. Manually switching existing rows off does not protect a later bulk-enable operation.

Repair: enforce the restriction in the shared publication rule for the actual OpenRouter route, retaining ordinary siblings and unrelated providers' identifiers.

### P2: Audio lacks a usable voice-selection flow

[MEASURED, UI and source] Audio offers a plain Voice text field (`CreateField.Kind.voice` becomes `NSTextField`). The model-aware `/v1/voices?model_id=…` endpoint already exists (`gateway/api.py:510`), but this screen does not use it.

Next work: searchable compatible voices, language and provider labels, preview, and a clear route to import/manage voices. Keep explicit-ID entry as an advanced escape hatch. Never choose an unrequested speaker silently or broaden private-voice permissions.

### P2: Create layout has conflicting width requirements

[MEASURED, UI and source] Audio help text was clipped in the rendered app. The form column is fixed at 390 points, but `PageKit.formRow` requests a 150-point label, a 12-point gap, and a 330-point control: 492 points. The output requires at least 320 points, and completed text output requests 520 points. See `CreateSection.swift:172–173, 211, 242–257, 461` and `PageKit.swift:122–149`.

Next work: labels above controls within Create, flexible controls, and an output pane that stacks below the form in a narrow window. Preserve system fonts, native focus rings, keyboard access, and light/dark appearance. Verify empty, long-result, failure, and upload states at the minimum window size.

### P2: provider badges confuse compatibility with actual availability

[MEASURED, UI and source] Providers showed **4/4 offered** for Black Forest Labs beside **no key** and **Enable**. `registry.describe_all` counts all `can_publish` rows, including disabled rows, and the console labels that count “offered” (`gateway/providers/registry.py:158–160`, `gateway/admin_ui.html:614`).

Next work: label this count “supported”, and show an independently derived enabled/available count when useful. Keep discovered, supported, enabled and reachable distinct. An offline Hub's cached models are not proof that a job can run now.

### P2: loading lacks visible progress and recovery

[MEASURED, UI] Models and Providers initially showed blank native content panes; the same console rendered in the browser. Providers later rendered successfully. No elapsed-time benchmark or root-cause diagnosis was completed, so this is **not** recorded as a permanent loading failure.

[MEASURED, source] `ConsoleSection` failure callbacks only set `lastLoadFailed`. Add a visible loading/error state and Retry; retain current form data when the page is healthy. Measure cold and warm navigation before attributing the delay to API speed or WebKit.

## Additional operational follow-ups

These remain open; they were not silently folded into the first repair batch.

| Priority | Evidence | Required next check / repair |
|---|---|---|
| P1 | [MEASURED, source] `JobStore.cancel` reads then updates, while `succeed`/`fail` settle unconditionally. Running cancellation can later be replaced by completion. | Define truthful in-flight cancellation semantics and test cancellation against delayed completion. Keep accepted upstream work distinguishable from work cancelled before dispatch. |
| P1 | [MEASURED, source] `retention._files` includes `uploads/`, and `prune` selects by age/size/pin without consulting active jobs. | An upload older than the one-hour protection window is eligible under retention pressure even when its job still needs it. Reproduce with a delayed mock job, then protect referenced inputs until terminal state; do not disable retention globally. |
| P2 | [MEASURED, source] `_quote_model` says the quoted model wins, but checks `asked` before the stored model (`gateway/api.py`, `_quote_model`). | A mismatched confirm can choose another model. Add a quote-A/confirm-B regression and enforce quote identity or explicitly reject the mismatch. |
| P2 | [MEASURED, source] `_artifact_response` checks existence, then reads; retention may unlink between those operations. | Reproduce deletion during download and translate missing bytes into the existing typed 410 response. |
| P2 | [MEASURED, source] `create_models` filters `_flat_models` by enablement but does not check credential configuration, unlike `enabled_models`. | Clear a mock provider key while leaving its enablement on; compare Create options to submit eligibility. Reuse the same availability rule. |

## Content Packs: what is missing

[MEASURED] The tracked native sidebar, console views, backend and tests contain no `.sspack` library, import/export pipeline, or Content Pack editor. The only Content Store implementation is `gateway/api.py:1170–1200`: empty account and receipt lists, zero quote responses claiming `owned: true`, and no-op purchases reporting `completed`. These are compatibility responses, not a product catalogue or delivery system. Adding a sidebar item alone would not solve the gap.

Recommended order:

1. **Establish the consumer contract.** Read the current Story Studio pack schemas, publisher trust requirements, registry and install/download protocol. Inventory existing owned archives without changing the old GenStudio or customer records.
2. **Pack Library.** Browse and inspect validated packs; show kind, version, compatibility, preview, origin and validation status. Import existing archives, reject unsafe archives, and preserve identities. Display an honest empty state when no packs exist.
3. **Delivery to Story Studio.** Publish real manifests and download bytes through the one gateway origin. Test listing, import, dependency resolution, incompatible versions and reinstallation. Replace the current fictitious ownership responses only as part of this verified contract.
4. **Authoring workspace.** Start with one supported pack kind and existing schema tooling: edit, preview, validate and export. Add other editors only when their preview and validation paths are ready.

[ASSUMED, planning only] The intended first version is a personal, zero-credit library, not a multi-customer storefront. This assumption follows the request but does not authorize changing legacy prices, purchases, grants or publishing state. The exact Story Studio contract and existing archive inventory remain to be inspected before implementation.

## Design direction and work order

My proposed navigation is **Create**, **Library** (Outputs, Voices, Content Packs), and **Manage** (Models, Providers, Fleet, Settings), with Jobs accessible as the persistent work queue. Keep creation close to its results and operational configuration out of the main creative form.

1. **Trust and recovery:** the first repair batch, authenticated artifact saving, interrupted-job reconciliation, and truthful status labels.
2. **Complete one excellent Create workspace:** Audio first, with compatible voices, visible limits, retained inputs, progress/cancel semantics, preview, save and actionable errors. Apply its shared structure to the other lanes.
3. **A real output library:** reopen results from Jobs, preview every output, distinguish temporary versus pinned files, save with correct file names, and link to the existing retention settings.
4. **Content Pack Library and delivery:** inspect and then reuse Story Studio's external consumer contract; authoring follows successful import/delivery. That contract has not been verified in this audit.
5. **Finish the visual system:** consistent content widths and spacing, restrained status color, readable hierarchy, adaptive panes and keyboard checks. Avoid introducing a new UI framework just for appearance.

Suggested acceptance scenario: a user can configure one provider, select a compatible model and voice, produce a mock-backed result, navigate away and back without losing choices, save it, find it later, understand a connection failure, and recover an interrupted job without accidentally submitting twice.

## Scope and limits

[MEASURED] Three Luna workers supplied bounded source inventories. The findings above were checked against source and/or the running interface by the primary agent; worker agreement alone was not used as sign-off. The existing Graphify graph helped locate job/store/catalogue relationships, but its references were checked against current source.

Paid cloud generations, upstream provider compatibility for every model, real fleet transcription, full VoiceOver behavior, light appearance, load/stress behavior and Story Studio Content Pack installation were not exercised in this pass. They need separate targeted verification; this audit is not a clean bill of health for those areas.

Implementation and verification results for the first repair batch are recorded in `AUDIT-BACKLOG.md`.
