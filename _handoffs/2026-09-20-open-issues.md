# Gateway KH — open-issue ledger and proposed fixes

Source baseline: `fcfde43` (2026-09-20). Read [the new-computer handoff](2026-09-20-new-computer-handoff.md) first.

Labels: `[MEASURED]` = primary agent checked source or directly observed the named probe/UI; `[REPORTED]` = worker evidence not independently confirmed; `[ASSUMED]` = planning assumption. “Source” does not mean runtime reproduction. P1 prioritizes security, data integrity and broken core workflows; P2 covers other correctness/usability; P3 covers polish/documentation. These are audit judgements, not measured incident rates.

All items below are open unless explicitly marked otherwise. Proposed fixes are specifications for the next session, not implemented behavior. [Recorded offline results](checks/2026-09-20-probe-results.json) and [runnable diagnostic](checks/audit_probes.py) preserve the five main backend reproductions.

## Backend reliability and eligibility

### B01 — Interrupted running jobs remain stranded (P1)

- Evidence: [MEASURED, source + isolated probe] [gateway/store.py:193](../gateway/store.py#L193) — `"""UPDATE jobs SET state = 'running', next_action_at = NULL,`; [gateway/store.py:237](../gateway/store.py#L237) — `AND next_action_at IS NOT NULL`. Recorded probe: `state=running`, `next_action_at=null`, `due_count=0` after reopening SQLite.
- Trigger/impact: Restart after the worker claims a job. The row survives, but no worker pass takes it again.
- Proposed fix: Persist upstream acceptance/job identity for asynchronous adapters and reconcile on startup. Represent an unknown outcome honestly; expose recovery without automatically submitting twice. Handle synchronous lanes separately.
- Acceptance check: Crash/reopen before submit, after provider acceptance and after artifact return; reconcile or explain each state; assert exactly one upstream generation.
- Boundary/counter-evidence: Persistence of a job row is already present. That is not recovery. No paid upstream recovery was tested.

### B02 — Cancellation can be overwritten by late completion (P1)

- Evidence: [MEASURED, source + isolated probe] [gateway/store.py:201](../gateway/store.py#L201) — `def cancel(self, job_id: str) -> dict[str, Any]:`; [gateway/store.py:161](../gateway/store.py#L161) — `def succeed(`. Probe changed `cancelled` to `succeeded` after a late completion.
- Trigger/impact: Cancel while the adapter is running; later success/failure unconditionally replaces the terminal state. The cancel read/update also races completion.
- Proposed fix: Define cancel-requested versus actually-cancelled semantics. Use conditional atomic transitions, preserve any already-produced artifact, and cancel upstream only where supported.
- Acceptance check: Delayed mock adapter plus simultaneous cancel/completion; assert truthful stable state and no discarded output.
- Boundary/counter-evidence: The previous repair prevents dispatch of already-cancelled queued jobs; do not undo that fix or promise cancellation of work already accepted upstream.

### B03 — Retention deletes an upload still needed by a queued job (P1)

- Evidence: [MEASURED, source + isolated probe] [gateway/retention.py:198](../gateway/retention.py#L198) — `for path in ARTIFACT_DIR.rglob("*"):`; [gateway/admin.py:733](../gateway/admin.py#L733) — `"_upload_path": str(staged),`. A 2 MiB input aged two hours was removed under a 1 MiB cap while its job stayed `accepted`.
- Trigger/impact: A queued/stalled transcription outlives the one-hour protection window under retention pressure.
- Proposed fix: Exclude inputs referenced by nonterminal jobs from pruning; remove staging files when they are genuinely no longer needed. Keep age/size retention and explicit pins intact.
- Acceptance check: Old referenced upload survives size and age pruning until terminal state; unreferenced old upload is reclaimed; recent and pinned outputs remain protected.
- Boundary/counter-evidence: The one-hour protection currently works. It is a time grace, not a guarantee of job completion.

### B04 — Transcription confirmation can switch the quoted model (P2)

- Evidence: [MEASURED, source + isolated probe] [gateway/api.py:1139](../gateway/api.py#L1139) — `def _quote_model(quote_id: str, asked: str | None) -> str:`; [gateway/api.py:1148](../gateway/api.py#L1148) — `if asked:`. Quoted `model-a`, supplied `model-b`, resolved `model-b`.
- Trigger/impact: Confirming a valid upload quote with another model runs the requested override despite the quoted-model contract.
- Proposed fix: Use the stored quote identity or reject a mismatched confirmation. Preserve the documented fallback only for quotes that never named a model.
- Acceptance check: Quote A then confirm B must reject or use A; quote A with omitted model uses A; model-less quote follows the intended enabled-only fallback.
- Boundary/counter-evidence: The older missing-model bug was fixed. Explicit mismatch remains a separate case.

### B05 — Artifact deletion race escapes as an internal error (P2)

- Evidence: [MEASURED, source + controlled probe] [gateway/api.py:727](../gateway/api.py#L727) — `if not path.exists():`; [gateway/api.py:729](../gateway/api.py#L729) — `return Response(content=path.read_bytes(), media_type=chosen.get("media_type") or default_media)`. Simulated deletion between these calls raises `FileNotFoundError`, not a typed gateway error.
- Trigger/impact: Retention removes a file after the existence check and before its read.
- Proposed fix: Catch file absence at the actual read/open and return existing 410 ARTIFACT_GONE behavior. Avoid a global retention/download lock.
- Acceptance check: Delete at read time; HTTP returns typed 410, not 500; normal/multi-artifact downloads still select the correct bytes.
- Boundary/counter-evidence: This probe deliberately injects the race; no frequency claim is made.

### B06 — Create offers rows lacking configured credentials (P2)

- Evidence: [MEASURED, source] [gateway/admin.py:641](../gateway/admin.py#L641) — `if not row["enabled"] or not row["provider_enabled"]:` versus [gateway/providers/models.py:296](../gateway/providers/models.py#L296) — `if not self.credentials.is_configured(provider_id):`.
- Trigger/impact: Clear a provider key while leaving its provider/model toggles enabled; Create and submission eligibility can disagree.
- Proposed fix: Build Create options from the same eligible model/catalogue flow as submission, retaining display/privacy metadata and fleet namespace semantics.
- Acceptance check: With a mock provider enabled and its key absent, Create omits or explicitly disables it; restore key and it becomes selectable; compare IDs with accepted submission IDs.
- Boundary/counter-evidence: Do not confuse reachable with configured; no extra paid readiness probe is required.

### B07 — Headless bind can disagree with the auth guard (P1)

- Evidence: [MEASURED, source; deployment-triggered candidate] [gateway/config.py:288](../gateway/config.py#L288) — `return bind_host() in {"127.0.0.1", "localhost", "::1"}`; [gateway/admin.py:59](../gateway/admin.py#L59) — `if is_loopback():`. The native launcher passes matching host configuration; an independent Uvicorn command need not.
- Trigger/impact: Launching Uvicorn on a non-loopback interface or behind a proxy while GATEWAY_HOST stays loopback can retain the loopback admin bypass. The earlier keyless-network repair only covers accurate bind configuration.
- Proposed fix: Make the deployment/guard contract enforceable: reject non-loopback requests without authentication and prevent a supported headless launcher from diverging. Define trusted proxy behavior explicitly rather than trusting arbitrary forwarded headers.
- Acceptance check: Use a real Uvicorn subprocess in a disposable isolated network environment, with an explicit non-loopback CLI bind and misleading loopback configuration. Missing/wrong tokens must fail from a non-loopback client; valid tokens must work. An ASGI-only test cannot establish the startup/socket mismatch. Do not expose the owner's real service or data for this check.
- Boundary/counter-evidence: No public socket was opened for this audit. Native startup already aligns --host and environment, so this is not an assertion that its normal loopback launch leaks access.

### B08 — Retry classification is dropped at worker settlement (P2)

- Evidence: [MEASURED, source] [gateway/adapters/base.py:98](../gateway/adapters/base.py#L98) — `self.retryable = retryable`; [gateway/worker.py:117](../gateway/worker.py#L117) — `store.fail(job_id, code=exc.code, detail=str(exc)[:500])`.
- Trigger/impact: Adapters distinguish transient faults, but that classification does not survive into job recovery behavior.
- Proposed fix: Define recoverable versus unknown acceptance and expose useful recovery information. Only retry automatically where idempotency/reconciliation makes it safe; pair with B01.
- Acceptance check: Mock pre-acceptance transient failure and post-acceptance disconnect separately; preserve classification and ensure the latter cannot duplicate a generation.
- Boundary/counter-evidence: A terminal failure may be safer than a blind retry. This finding does not authorize automatically replaying every retryable exception.

### B09 — Quote lifetime contract omits its finite TTL (P2)

- Evidence: [MEASURED, source; contract gap] [gateway/api.py:69](../gateway/api.py#L69) — `_UPLOAD_TTL_S = 3600.0`; [gateway/api.py:1054](../gateway/api.py#L1054) — `"expires_at": None,`; [gateway/api.py:57](../gateway/api.py#L57) — `_UPLOADS: dict[str, tuple[bytes | None, str, str, float, str]] = {}`.
- Trigger/impact: The finite lifetime is not communicated by the quote reply. Restart also discards pending uploads by deliberate design; restart survival itself is not established as a product requirement.
- Proposed fix: Publish the actual finite lifetime and provide an explicit re-upload/re-quote recovery path. Add persistence only if the Story consumer requires restart survival.
- Acceptance check: Expire a quote and restart between quote/confirm; client gets an accurate typed expired response and can recover without duplicate confirmed jobs.
- Boundary/counter-evidence: The in-memory store is explicitly deliberate in source. Durable pre-confirm quotes are a product choice; truthful lifetime/recovery is the immediate issue.

## Native and browser UI

### U01 — Native artifact preview, save and open bypass authentication (P1)

- Evidence: [MEASURED, source] [menubar/CreateSection.swift:643](../menubar/CreateSection.swift#L643) — `if let data = try? Data(contentsOf: url) {`; [menubar/CreateSection.swift:635](../menubar/CreateSection.swift#L635) — `NSWorkspace.shared.open(url)`. Other preview paths also use URL-only native loaders.
- Trigger/impact: Network-bound admin routes require bearer auth, but these calls do not attach it. Save hides download/write errors; synchronous I/O can block the UI.
- Proposed fix: Add one asynchronous authenticated byte-download helper to GatewayAPI; validate response/status, use atomic writes and a useful filename/extension, report errors, open a downloaded local file. Reuse it for image/transcript preview and Save.
- Acceptance check: Authenticated mock artifacts preview/save/open; 401/410/disk-write failure remains visible; slow download does not freeze controls; multiple outputs remain distinct.
- Boundary/counter-evidence: The browser console already has apiBlob with auth. Reuse its behavior, not its JavaScript implementation. No real generation is needed.

### U02 — Audio uses a raw voice-ID field (P2)

- Evidence: [MEASURED, source and prior UI observation] [menubar/CreateSection.swift:260](../menubar/CreateSection.swift#L260) — `case .number, .text, .voice:`; [gateway/api.py:512](../gateway/api.py#L512) — `@app.get("/v1/voices", dependencies=[Depends(require_auth)])`.
- Trigger/impact: A user cannot discover compatible voices naturally from the Create form even though a model-aware endpoint exists.
- Proposed fix: Provide a searchable compatible voice picker with preview/language/provider, an import/manage route and advanced explicit-ID entry. Preserve explicit choice when model changes; explain incompatibility.
- Acceptance check: Changing models shows the right voice subset; no silent speaker substitution; private voices retain their permission boundaries; full keyboard selection works.
- Boundary/counter-evidence: This is a missing workflow, not proof that manually entered valid IDs fail.

### U03 — Create layout contains incompatible fixed widths (P2)

- Evidence: [MEASURED, source and prior UI clipping observation] [menubar/CreateSection.swift:169](../menubar/CreateSection.swift#L169) — `formColumn.widthAnchor.constraint(equalToConstant: 390).isActive = true`; [menubar/PageKit.swift:112](../menubar/PageKit.swift#L112) — `static let formLabelWidth: CGFloat = 150`; [menubar/PageKit.swift:141](../menubar/PageKit.swift#L141) — `row.spacing = 12`; [menubar/CreateSection.swift:212](../menubar/CreateSection.swift#L212) — `picker.widthAnchor.constraint(equalToConstant: 330).isActive = true`. The requested row width totals 492 points.
- Trigger/impact: Narrow Create panes clip or compress content. Text output also imposes a 520-point width.
- Proposed fix: Use top-aligned labels above flexible controls and an adaptive form/results arrangement; stack panes when narrow. Preserve native focus and accessibility.
- Acceptance check: Inspect every lane at minimum window size, long help/errors/results and light/dark appearance; no clipping, window growth or offscreen primary action.
- Boundary/counter-evidence: Do not remove protective validation or shrink text merely to fit.

### U04 — Provider badge labels support as availability (P2)

- Evidence: [MEASURED, source and prior UI observation] [gateway/providers/registry.py:158](../gateway/providers/registry.py#L158) — `item["publishable_model_count"] = sum(`; [gateway/admin_ui.html:614](../gateway/admin_ui.html#L614) — ``badges.push(`<span class="badge">${p.publishable_model_count}/${p.model_count} offered</span>`);``. Baseline UI showed BFL “4/4 offered” beside “no key” and “Enable”.
- Trigger/impact: A disabled/unconfigured provider appears usable because the badge counts structural support.
- Proposed fix: Label structural compatibility as supported; derive enabled/configured availability from the existing shared eligibility rule. Keep reachability separate.
- Acceptance check: Provider with no key, disabled provider, enabled supported model and unsupported modality produce distinct truthful labels and counts.
- Boundary/counter-evidence: Changing the label is smaller than changing the meaning of a backend count that other readers may use.

### U05 — Console load failure lacks a visible recovery state (P2)

- Evidence: [MEASURED, source and prior UI observation] [menubar/MainWindow.swift:104](../menubar/MainWindow.swift#L104) — `private var lastLoadFailed = false`. Initial Models/Providers panes were blank; Providers later rendered.
- Trigger/impact: A failed/slow initial WebKit load can look like missing content without an explanation or visible Retry.
- Proposed fix: Show loading, a bounded error state and Retry; preserve healthy form edits. Measure cold and warm navigation before changing network behavior.
- Acceptance check: Simulated load failure/offline gateway displays a usable recovery action; retry renders data; normal activation does not discard unsaved edits.
- Boundary/counter-evidence: Permanent failure and exact load duration were not established; do not report either.

### U06 — Embedded console retains the old gateway key (P2)

- Evidence: [MEASURED, source] [menubar/MainWindow.swift:54](../menubar/MainWindow.swift#L54) — `private lazy var webView: WKWebView = {`; [menubar/MainWindow.swift:63](../menubar/MainWindow.swift#L63) — `let key = EnvFile.apiKey()`; [gateway/admin_ui.html:302](../gateway/admin_ui.html#L302) — ``// The key comes from `window.__GATEWAY_ADMIN_TOKEN__` when the menu-bar app``.
- Trigger/impact: After key rotation, an already-created web view keeps the injected old key; reload/setView does not reconstruct the script. The injected value can override a token manually entered in the page.
- Proposed fix: Refresh the injected credential when the key changes, including future document loads, while preserving page edits; define recovery on unauthorized responses.
- Acceptance check: Rotate the key in an isolated profile while Models/Providers remain open: next request uses the new key, old key is rejected, no app restart required.
- Boundary/counter-evidence: Native GatewayAPI reads the current key for requests; this finding concerns the cached WebKit injection specifically.

### U07 — Providers request errors are not rendered (P2)

- Evidence: [MEASURED, source] [gateway/admin_ui.html:563](../gateway/admin_ui.html#L563) — `async function loadProviders() {`; [gateway/admin_ui.html:568](../gateway/admin_ui.html#L568) — `const { providers, scopes, skipped_custom_providers } = await api('/providers?scope=cloud');`. Unlike Models, this load path has no enclosing catch.
- Trigger/impact: A failed provider request rejects the promise and can leave blank or stale content; token prompt alone does not explain other failures.
- Proposed fix: Catch the request rejection in the page and render error/retry without dropping healthy form edits.
- Acceptance check: Mock 401, 500 and network failure, then successful retry; show an accurate state for each.
- Boundary/counter-evidence: This is separate from native WebKit navigation failure (U05).

### U08 — Overview treats failed activity reads as no jobs (P2)

- Evidence: [MEASURED, source] [menubar/OverviewWindow.swift:505](../menubar/OverviewWindow.swift#L505) — `self.activityStack.addArrangedSubview(self.dim("No jobs yet."))`. Its guard combines failed/malformed responses with an empty list.
- Trigger/impact: An outage or malformed payload can be presented as an empty history.
- Proposed fix: Separate failure, invalid payload and successful empty history; retain last known activity with a stale indicator when useful.
- Acceptance check: Failed request never says “No jobs yet”; valid empty response does.
- Boundary/counter-evidence: The Jobs page already reports request failures; align the Overview behavior.

### U09 — Voice refresh and bulk toggles discard results (P2)

- Evidence: [MEASURED, source] [menubar/VoicesWindow.swift:240](../menubar/VoicesWindow.swift#L240) — `GatewayAPI.post("/admin/api/voices/refresh") { [weak self] _ in`; [menubar/VoicesWindow.swift:459](../menubar/VoicesWindow.swift#L459) — `GatewayAPI.post("/admin/api/voices/enabled", body: ["items": ids, "enabled": enabled]) { [weak self] _ in`. Both callbacks ignore the result and reload.
- Trigger/impact: A refused/failed bulk action gives no reason that the requested state did not change.
- Proposed fix: Handle errors with the existing report label, preserve selection, and reload successful outcomes.
- Acceptance check: Mock failed refresh and partial/refused bulk toggle; visible actionable error, no false success, state matches server.
- Boundary/counter-evidence: Single-row toggles and voice import already contain error handling; reuse that pattern.

### U10 — Voice annotation stops polling silently on read failure (P2)

- Evidence: [MEASURED, source] [menubar/VoicesWindow.swift:337](../menubar/VoicesWindow.swift#L337) — `guard let self, case .success(let state) = result else { return }`. The next poll is scheduled only from the success/running branch.
- Trigger/impact: A transient status-read error leaves the old progress sentence without another poll.
- Proposed fix: Show status-check failure and retry the same annotation job with bounded cadence; do not restart transcription.
- Acceptance check: Fail one poll then recover: error visible, progress resumes, exactly one annotation operation.
- Boundary/counter-evidence: Terminal annotation failure payloads are already rendered; transport failure is the missing branch.

### U11 — Fleet batch refresh suppresses individual failures (P2)

- Evidence: [MEASURED, source] [menubar/FleetSection.swift:300](../menubar/FleetSection.swift#L300) — `GatewayAPI.post("/admin/api/providers/\(encoded)/refresh") { [weak self] _ in`. Callback counts completion but discards per-location results.
- Trigger/impact: One location refresh fails among successful locations and its reason is lost at the final reload.
- Proposed fix: Collect outcomes and display failed location/details with a retry action; retain successful discovery results.
- Acceptance check: One mock site succeeds and another fails; report both correctly and retry only the failed request.
- Boundary/counter-evidence: A fleet status refresh is not model discovery; preserve their separate meanings.

### U12 — Key-regeneration copy incorrectly requires restart (P3)

- Evidence: [MEASURED, source] [menubar/SettingsSection.swift:308](../menubar/SettingsSection.swift#L308) — `statusLabel.stringValue = "New key written. Restart the gateway to apply it."`. Live configuration/auth reads were already repaired.
- Trigger/impact: The user is told to restart a gateway for a key rotation that is applied on subsequent requests.
- Proposed fix: Update copy after fixing U06; distinguish clients needing their saved key updated from the gateway itself.
- Acceptance check: Rotate key, old key fails and new key succeeds without service restart; UI instructions match the observed behavior.
- Boundary/counter-evidence: Do not claim the embedded console rotates correctly until U06 is fixed.

### U13 — HTML Create polling still mishandles cancellation and errors (P2)

- Evidence: [MEASURED, source] [gateway/admin_ui.html:1016](../gateway/admin_ui.html#L1016) — `function pollCreate(id) {`; [gateway/admin_ui.html:1107](../gateway/admin_ui.html#L1107) — `if (job.state !== 'succeeded') {`. The polling catch stops silently; cancelled falls into the spinner branch.
- Trigger/impact: Use the browser Create surface with a cancelled job or failed status read. Native Create was repaired but this separate consumer was not.
- Proposed fix: Render cancellation as terminal, report/retry status reads for the same job, and serialize polls. Keep result ownership tied to the selected job so older callbacks cannot replace it.
- Acceptance check: Cancelled has no spinner; delayed/error polls do not overlap or overwrite another selected result; no retry resubmits work.
- Boundary/counter-evidence: The native Create regression check does not cover this HTML implementation.

### U14 — Successful HTML Generate and Keep are reported as JavaScript errors (P1)

- Evidence: [MEASURED, source + offline JavaScript probe] [gateway/admin_ui.html:1002](../gateway/admin_ui.html#L1002) — `const job = await res.json();`; [gateway/admin_ui.html:1086](../gateway/admin_ui.html#L1086) — `const d = await r.json();`. api() already returns parsed JSON. Probe: two successful mock requests followed by `res.json is not a function` and `r.json is not a function`.
- Trigger/impact: The server can accept a generation or pin, but the UI reports failure after treating a plain object as a Response. Generate is re-enabled and a later click creates a new idempotency key.
- Proposed fix: Consume the api() return object consistently and remove the second parse/status check. Reattach the accepted job and guard ambiguous submit outcomes before enabling another attempt.
- Acceptance check: Offline test and rendered browser fixture: Generate shows the returned job and begins its poll; Keep says Kept; lost-response retry does not submit twice.
- Boundary/counter-evidence: Browser Create is reachable via /admin#create even though native navigation uses the Swift Create screen. See checks/console_action_probe.js and its recorded results.

### U15 — Voice preview conflates transport/auth failures with no preview (P2)

- Evidence: [MEASURED, source] [menubar/VoicesWindow.swift:834](../menubar/VoicesWindow.swift#L834) — `URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in`; [menubar/VoicesWindow.swift:839](../menubar/VoicesWindow.swift#L839) — `self.summaryLabel.stringValue = "No preview available for that voice."`.
- Trigger/impact: Timeout, unauthorized, empty/corrupt response and genuinely unavailable preview are not distinguished.
- Proposed fix: Reuse authenticated byte-download/error handling and map each outcome to meaningful copy; keep the current preview permissions unchanged.
- Acceptance check: 401, timeout, missing preview and invalid audio produce distinct outcomes; valid audio plays and Stop works.
- Boundary/counter-evidence: This preview already attaches the bearer token. It is not another unauthenticated path like U01.

### U16 — Empty HTTP 200 is treated as a successful empty response (P2)

- Evidence: [MEASURED, source] [menubar/GatewayAPI.swift:212](../menubar/GatewayAPI.swift#L212) — `guard let data, !data.isEmpty else {`; [menubar/GatewayAPI.swift:213](../menubar/GatewayAPI.swift#L213) — `DispatchQueue.main.async { completion(.success([:])) }`.
- Trigger/impact: An empty HTTP 200 can become zero-count UI instead of malformed data, despite the helper rejecting malformed nonempty JSON.
- Proposed fix: Require the response object expected by each read operation; allow bodyless success only for endpoints explicitly defined that way.
- Acceptance check: An empty 200 from a JSON read produces a typed error, not 0 voices/locations; valid empty arrays inside a valid object remain legitimate empty results.
- Boundary/counter-evidence: Source-only response-contract gap; no live server producing empty 200 was observed.

## Portability and documentation

### P01 — Shipped voice catalogue contains source-Mac absolute paths (P1)

- Evidence: [MEASURED, tracked metadata + source] The tracked data/voices.json has 10 voices and 10 absolute reference_path values; git tracks no WAV/MP3/FLAC/M4A recordings. [menubar/build.sh:101](../menubar/build.sh#L101) — `[ -f "$ROOT/data/voices.json" ] && cp "$ROOT/data/voices.json" "$APP/Contents/Resources/gateway/data/"`; [gateway/voices.py:411](../gateway/voices.py#L411) — `"reference_path": entry.get("reference_path"),`.
- Trigger/impact: A clean clone/bundle carries catalogue entries but not their referenced recordings. A new home directory does not provide those old absolute paths.
- Proposed fix: Ship only genuinely bundled/default resources or an honest empty catalogue; use the existing private import flow for owner voices. Preserve any live writable catalogue and permissions.
- Acceptance check: Build/run with fresh temporary Application Support and no source-Mac paths; no ghost usable voices; imported voice then resolves its actual clip and transcript.
- Boundary/counter-evidence: The files exist on the source Mac; their absence on a fresh destination is a portability consequence, not a claim that the current recordings are lost.

### P02 — Dependency and bundled-runtime caching are not reproducible (P2)

- Evidence: [MEASURED, source] requirements.txt names four packages without versions. [menubar/build.sh:76](../menubar/build.sh#L76) — `STAMP="$RUNTIME_CACHE/.requirements-$REQS_HASH"`. The cached expanded runtime is keyed by requirements bytes, not the selected Python asset.
- Trigger/impact: Fresh versus cached builds can resolve different versions, and changing the Python asset while leaving requirements unchanged can reuse an old runtime.
- Proposed fix: Use one tested dependency constraint/lock input for development and bundling, and include Python asset/architecture in the existing cache stamp. Verify downloaded/runtime identity without adding a new framework.
- Acceptance check: Two clean builds resolve the intended same versions; changing the Python asset invalidates the cache; no version drift hidden by old cache.
- Boundary/counter-evidence: The committed freeze file records the tested environment but does not change the bundler or supply artifact hashes. Current dependencies passed consistency, not a vulnerability audit.

### P03 — Build succeeds despite icon or signing failure (P2)

- Evidence: [MEASURED, source] [menubar/build.sh:141](../menubar/build.sh#L141) — `"$PYTHON" "$HERE/make_icon.py" "$APP/Contents/Resources/AppIcon.icns" || echo "    (icon generation failed; continuing without one)"`; [menubar/build.sh:149](../menubar/build.sh#L149) — `codesign --force --deep --sign - "$APP" 2>/dev/null || echo "    (codesign unavailable; continuing)"`.
- Trigger/impact: Missing tools or failed generation/signing can still reach the Built/Installed message.
- Proposed fix: Treat required distribution/build checks as failures, or clearly mark an intentionally unsigned development artifact and prohibit claiming it release-ready. Verify the produced signature/icon.
- Acceptance check: Inject icon/signing failure; build exits nonzero or explicitly reports the reduced artifact status; normal bundle passes codesign verification.
- Boundary/counter-evidence: The previously built source-Mac bundle passed signature verification; this is a failure-path gap, not a claim it is unsigned.

### P04 — Private-data copies retain old absolute artifact/voice/pin paths (P1)

- Evidence: [MEASURED, source] [gateway/voice_import.py:783](../gateway/voice_import.py#L783) — `"reference_path": str(target),`; [gateway/retention.py:166](../gateway/retention.py#L166) — `resolved = str(Path(path))`; [gateway/store.py:173](../gateway/store.py#L173) — `asset_path=asset_path,`.
- Additional measured source: `gateway/worker.py:134-152` writes every artifact path, and `gateway/store.py:175` serializes that list in `artifacts_json`; `gateway/api.py:1123` stores `_upload_path` in job params, consumed by `gateway/adapters/openai_compat.py:346-352`. Include these fields, not only the legacy first `asset_path`.
- Trigger/impact: Moving Application Support to a differently named home directory can leave SQLite artifacts, staging references, voice clips and retention pins pointing at the old location.
- Proposed fix: Inventory relocated paths from a private backup, validate destination ownership/existence and migrate references atomically with backups. Keep identities and grants intact; do not rewrite arbitrary strings.
- Acceptance check: Restore a fixture into a different home directory; previews and job outputs resolve, pins protect the intended files, and no source path is read.
- Boundary/counter-evidence: Fresh development setup needs no history migration. Do not transfer private state into Git or modify the owner data just to make an audit pass.

### P05 — README and old handoffs contradict current behavior (P3)

- Evidence: [MEASURED, source] README.md previously stated unqualified keyless access, gateway survival after Quit, and obsolete test counts; old continuation prompts predate the repair branch.
- Trigger/impact: A new session can follow obsolete instructions and misidentify expected behavior or completion.
- Proposed fix: Use this current handoff as entry point; correct the README statements; mark old prompts historical and retain their context rather than executing them as current tasks.
- Acceptance check: Fresh clone identifies current entry point, auth/quit behavior and test limitations without reading a chat transcript.
- Status: README auth/quit/count guidance and the old continuation entry point are corrected in this handoff. Stale Swift lifecycle comments remain to align with the implementation.
- Boundary/counter-evidence: Documentation corrections change no product behavior. Historical test results are not fresh verification.

## Workflow/features to specify before implementing

These are proposed product work, not all runtime defects. They are separate from the repairs above so a new session does not mistake missing optional UI for broken existing code.

| ID | Evidence and missing workflow | Proposed scope | Acceptance |
|---|---|---|---|
| F01 | [MEASURED, source search] `gateway/admin.py:757` exposes `POST /api/create/status/{job_id}/cancel`; no `/cancel` consumer was found in `menubar/` or `gateway/admin_ui.html`. | Add an accessible Cancel action to Create/Jobs, after B02 defines truthful in-flight semantics. | Queued cancellation prevents dispatch; running cancellation accurately distinguishes request from upstream confirmation; terminal jobs cannot be cancelled again. |
| F02 | [MEASURED, source search] `gateway/admin.py:530` exposes `GET /api/artifacts/kept`; no `artifacts/kept` consumer was found in native or HTML UI. Jobs shows history but the intended workflow needs durable access to results. | An Outputs library with previews, all variations, save/open, pin/unpin and missing-file status; link it from Jobs. Reuse existing artifact IDs and retention APIs. | A user can revisit, preview and save each output after leaving Create; pinned status is accurate and a pruned result is explained. |
| F03 | [MEASURED, source] Create keeps fields/job IDs in the in-memory `CreateWorkspace`; its repair preserves selection during refresh, not across process restart. | Persist drafts and selected job identity locally, then reattach through Jobs/recovery. Keep an attempt's idempotency key across ambiguous submission failure. | Navigate away/back and restart without silently losing choices or issuing duplicate requests; distinguish a saved draft from active provider work. |
| F04 | [MEASURED, source] `CreateLanes`/`CreateField` describe shared static forms; the catalogue already publishes limits and capability provenance. [ASSUMED, proposed direction] Adapt the form using known model capabilities rather than expanding manual provider-specific fields. | Show relevant supported controls, known limits and source labels; preserve explicit values and clearly show unknown capabilities. Avoid a second configuration framework. | Switching models changes only applicable controls, never invents capability numbers or silently substitutes a voice; invalid input is caught with useful guidance. |
| F05 | [MEASURED, source] `/v1/content/account`, `/quotes`, `/purchases`, `/receipts` in `gateway/api.py:1175-1205` return empty/zero compatibility data, not real pack delivery. The inspected gateway has no `.sspack` library/import/export/editor. | Read the **current** `story-studio-kh` consumer schemas, trust/identity, manifests, registry and install/download protocol first. Then import/inspect/validate a personal Pack Library and deliver real files; add authoring/preview/export for one supported kind. | An actual validated pack lists, downloads, installs and reinstalls in Story Studio. Invalid/unsafe/incompatible archives are refused. No false completed purchase or ownership state. |

[ASSUMED] The initial pack scope is a personal zero-credit library, consistent with this gateway's direction. Multi-customer commerce, migration of legacy purchases, author trust grants, and publication are not approved by that assumption. The current consumer contract and pack installation remain unverified. Known related private repositories are `theng12/story-studio-kh` and `theng12/genstudiokh`; their GitHub existence was checked, their current schemas were not audited here.

[MEASURED, later asset inventory] The [asset transfer follow-up](2026-09-20-asset-transfer.md) preserves 167 installed Story archives across 115 pack IDs, their state journals, and 237 editable source pack definitions. Archive integrity and manifest file hashes were checked; ownership, signatures and install compatibility were not. Use that inventory instead of repeating the filesystem search. This does not close F05.

Optional later UX: catalogue diagnostics, per-location voice readiness, and an explicit Forget Imported Voice action already have backend routes (`catalog_preview`, `list_shared_voices`, `forget_voice`). Their absence from primary navigation is a product decision, not automatically a defect. Confirm exact route and ownership semantics before exposing destructive operations.

## Coverage, limits, and closed work

[REPORTED] The backend worker scanned the named gateway API/admin/store/worker/retention/fleet/hub/voice modules, all adapter/provider modules and targeted tests. The UI worker scanned all current `menubar/*.swift`, the HTML console and relevant consumers/tests. The portability worker inventoried source/config/build/state and older handoffs. Primary-agent checks promoted the source facts above and directly reran the recorded offline probes; no worker's severity or proposed implementation was accepted automatically.

[MEASURED] The prior keyless-configured-network guard, queued-claim cancellation fix, OpenRouter batch-route restriction, and native Create refresh/pending/polling/cancellation fixes are already in `91b45db`. They are not open tasks here. B07, B02 and U13 identify different remaining cases.

Not established by this audit: every provider/model generation path, paid calls, actual WAN exposure, load/stress behavior, complete dynamic-import/runtime reachability, real fleet transcription, every UI state, accessibility/light appearance, migration of actual owner data, pack consumer compatibility or supply-chain vulnerability freedom. No security vulnerability scanner was run. The Git history check was limited to secret patterns and exact matches against current gateway credentials; it found no matches, which is not proof that all sensitive information is absent.

No proposed source repair above was implemented during the handoff turn. README/entry-point corrections and Git hygiene are the only non-handoff edits. Keep open items until their acceptance checks pass and independent review is recorded.
