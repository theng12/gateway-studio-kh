> **0.2.211 — a Hub batch this Gateway superseded is adopted, or stopped under the orphan guard; and an agent can work through a guest API key with a bundled MCP connector — 2026-10-30. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.211` in `/build/Developer/_worktrees/gateway-kh/release-0.2.211`, cut from `main` **`fc3edcb`** (0.2.210) and merged `--no-ff` with `fix/restart-no-orphans-20261030` (**`f1b4985`**) and `feat/agent-connector-20261030` (**`598233d`**), both cut from `fc3edcb`; `VERSION` 0.2.211. Evidence: `/build/Developer/_orchestra/gateway-0211/`. **The one merge conflict was this file** (`docs/tasks/HANDOFF.md`), resolved by intent: both records are kept below, the orphan fix first and the connector after it.
>
> **Why, and why the 0.2.80+ adopt logic did not cover it.** Three incidents — 1200's eight batches on 2026-10-29, then 1300's `693cd48661` and 1400's `614f0a0126` around the 0.2.210 restart. A site's controller dropped off the network mid-job; `studio_hub._poll_batch` raised `PROVIDER_SUBMISSION_UNCERTAIN` with `recovery.lost_contact`, and `worker._dispatch` moved the job to another site and submitted a **new** batch. The old batch kept running on the site that came back, the job succeeded, and no gate saw anything. `reconcile_holds` only asks about jobs in `failed / recovery_state = unknown_outcome`, and a re-dispatch never creates a hold — it moves a live job — so the old id never reached a hold's `recovery_context`. After the move the only record of it was one sentence in `attempt_log`, and `adopt_batch` was only ever called for the hold's *current* batch id. So adoption could not see the copy that was left behind.
>
> **1 — Ask before moving (`worker._hub_batch_answer`).** On `lost_contact`, and before `_next_machine`, the worker asks the old site once about the batch (`studio_hub.batch_snapshot`). Still running/queued → `resume_id = batch_id` and the loop follows the **same** batch (no submit). Done → `adopt_batch` collects it through the normal done path. No answer, 404, or a terminal-bad item → the move happens exactly as before, so a genuinely dead site costs one 15 s read and no delay. A read that cannot be made is `None` ("could not ask"), never a verdict.
>
> **2 — The structured record (`store.superseded_batches`).** A JSON column added through the existing idempotent `_ADDED_COLUMNS` path, with a partial index `jobs_superseded` created **after** the ALTER (a 0.2.210 store opens and migrates; a fresh one does too). Each entry carries `{batch_id, item_index, provider_id, location, modality, jobs_path, status, recorded_at, resolved_at, detail}`; `status` is a word — `open`, `adopted`, `stopped`, `finished_after_delivery`, `finished_after_cancel`, `finished_<state>`, `not_found`, `location_gone`. `record_superseded_batch` dedupes on the batch id, keeps the newest 16, and is written **before** the job is sent anywhere else. `_row_dict` parses it and the admin job row publishes it beside `recovery_context`. **One accept-after-settle hole is closed with it:** `record_upstream_acceptance` now records an id that arrives for an already-settled job as a superseded entry instead of dropping it — the race the first-result-wins adoption creates, and the same shape the liveness watchdog could produce; that batch is then owned by the cleanup rather than becoming `orphan_unknown`.
>
> **3 — Reconcile (`worker.reconcile_superseded`, riding the hold sweep's thread every 60 s).** One `batch_snapshot` per open entry, asked only of the site that owned it. **Finished:** if the job already delivered → `finished_after_delivery`, its bytes are never fetched (nothing double-delivered); if the owner cancelled → `finished_after_cancel`; otherwise the **first copy to finish wins** and is adopted through `settle_result`. **Still running:** stopped only when the job settled and `orphans.stop_allowed` — the Fleet Stop guard — allows it (a job succeeded with every artifact present and non-empty, or failed/cancelled with nothing left to collect); the cancel is the adapter's own `_cancel_batch`, and every outcome is written to `startup_audit.jsonl` (`superseded_stop`, `superseded_stop_refused`, `superseded_adopted`, `superseded_duplicate`). A job still running keeps **both** copies: the guard never stops work that is not provably kept. **Not found** closes as `not_found`; **no answer** leaves the entry open. `orphan_unknown` is unreachable by construction: only a job's own structured record is read, and the general `auto_stop` switch is untouched and still off by default — the Fleet page text now says the replaced-batch case is separate.
>
> **4 — The losing copy cannot dispatch again.** `_dispatch` returns immediately when the row is terminal (a stop that arrives after the other copy won was previously a re-route candidate and could submit a third batch). `settle_result` is serialized per job id, and returns whether **its** result settled the job, so a live collection and an adoption cannot both write the same artifact path; an adoption that loses that race is left for the next sweep and reported as a duplicate, not as adopted.
>
> **Tests.** `tests/test_superseded_batches.py` (**13** here, **26** after the review fixes in 5): the still-running batch is followed, not re-submitted; a finished batch is collected before any move; the field is written on a re-dispatch; the new copy finishes first and the old batch is stopped exactly once with an audit line; the old copy finishes first, is adopted, and the loser is stopped; a late duplicate is recorded and never fetched; a finished copy beside a failed job is kept on disk and never stopped; a 404 closes as `not_found`; a running job keeps both copies and an unrecorded batch is never touched; the guard's allowed/refused cases; a settled job never re-dispatches; an accept-after-settle becomes a superseded entry; a 0.2.210 database gains the column and index and keeps its rows. `test_free_location_reroute`, `test_restart_resend`, `test_job_recovery`, `test_fleet_orphans` and `test_whats_new_page` still pass, and every JS suite was run after the page-text change.
>
> **5 — Independent review of `3fe14f8`: four blocking defects and the smaller findings, all fixed.** (1) Following a still-running batch is **bounded** (`FOLLOW_LIMIT = 2`): a Hub flapping on about a minute's cycle restarts the poll's own budget each time and the liveness sweep skips rows carrying an upstream id, so after two follows the job falls through to `_next_machine` and the batch is recorded. (2) `open_superseded_batches` filters **in SQL** for an open entry (`instr(…, '"status": "open"')`) instead of taking the fifty newest movers regardless of state — more than fifty resolved movers could bury the oldest open batch for ever. (3) `_adopt_hub_batch` wraps `settle_result` (the rule `reconcile_holds` already follows): a refused artifact closes the entry as `result_rejected` with the code in the detail and is never downloaded again, and in `_dispatch` the job moves on instead of becoming an internal-error hold; `cancelled_items` joins the Hub's terminal words. (4) An unresolved hold (`failed` + `recovery_state = unknown_outcome`) is **not settled** for this cleanup, so its only copy is never stopped. **Smaller:** the cleanup now has **its own timer and thread**, so a slow cleanup can never queue a hold behind it, plus a **90 s wall-clock budget per sweep** so a dead site cannot pin that thread either (leftovers stay open; both choices are stated in the constant's and the function's comments); the row is re-read immediately before `_next_machine`; the batch is recorded **before** `_next_machine` (and closed `not_moved` when the job stays); the `DISK_LOW` path records the finished batch for a later collection; `_cancel_hub_batch` and `_stop_losing_copy` refuse any non-Studio-Hub lane, so a paid adapter's cancel is never called; a late accept on a settled job no longer bumps `updated_at`; `/admin/api/jobs` publishes `superseded_batches` **without `location`** (`admin.superseded_view`), because that door is guest-reachable (`require_guest_or_admin`); and the automatic stop is behind its own `superseded_stop` key — **on by default**, separate from the still-off `auto_stop`, on the Fleet page, in the same `fleet_orphans.json`, and included in `backup.sources()`. `tests/test_superseded_batches.py` is **27 tests**; each regression for the four blocking fixes fails on `3fe14f8` (verified in a detached worktree at that commit: 14 failed/errored), and the JS page test fails on the old page.
>
> **6 — Second independent check, on the merged release (2026-10-30): five defects and two stale sentences, each fixed with a regression test that fails on the pre-fix code.** (1) The re-read before `_next_machine` reported this attempt's failure for **any** terminal row, including an `unknown_outcome` hold — and `store.fail` settles a hold with the arriving failure, wiping the `recovery_context` that is the only place the abandoned batch's id lives until `record_superseded_batch` runs. A row holding `RECOVERY_UNKNOWN` is now left alone: the hold's own reconciliation owns that batch. (2) `_adopt_hub_batch` treated **every** exception from `settle_result` as a refused result and closed the entry `result_rejected` for good; only `BackendError` with `INVALID_ARTIFACT` is a verdict now — a disk, sqlite or delivery-receipt failure leaves the entry open for the next sweep. (3) `cancelled_items` was a **count** in the Hub's batch summary, not a state word, so it could never match; the word that was actually missing is `partial` (the Hub finished a job with some sections produced and others not), and the dead entry and its comment are gone rather than left as decoration. (4) `open_superseded_batches` matched only the spaced JSON spelling; it now accepts the compact one too, with the exact `status` test still the authority. (5) The cleanup's thread start could raise inside the dispatch loop: the thread is started through `Worker._start_superseded_sweep`, which clears `_superseded_sweeping` when `start()` fails so the cleanup is never silently dead and dispatch is never ended by it. **Stale sentences:** the 90 s budget is checked **between entries** (one slow read can overrun it), and `GET/PUT /api/settings/fleet-orphans` now describes two switches with two defaults instead of claiming both are off. `tests/test_superseded_batches.py` is **32 tests** (five new), and each new test was run against a reverted copy of the fix to see it fail.
>
> **The agent connector — a guest API key, and an MCP server that uses it.** Branch `feat/agent-connector-20261030` in `/build/Developer/_worktrees/gateway-kh/agent-connector`, cut from `main` `fc3edcb` (0.2.210) and merged here beside the orphan fix. It touches no dispatch or adapter code, so it merged cleanly against it; the page and `admin.py` changes auto-merged and were reviewed by intent.
>
> **What it adds.** (1) **A guest API key.** `admin_session.issue_guest_key` mints `gwk_…` once, stores only its SHA-256, and `guest_by_key` resolves it to the *same guest* identity the browser cookie resolves to. `admin.guest_identity` is the one function every guest rule already reads, so the key inherits the free/local gate, per-guest job and file ownership, the scoped idempotency keyspace and "Requested by <name> (guest)" **without a second copy of any of them**. Owner-only issue/revoke at `POST`/`DELETE /admin/api/guests/{id}/key`; the Guests page shows a fingerprint and the key once, `no-store`. (2) **`POST /admin/api/create/video`**, the one missing guest job: image-to-video from an uploaded scene image, staged like a transcription, because text-to-video is retired and the console had no image field. (3) **`connectors/mcp/`** — a stdlib-only MCP stdio server with 11 tools and its README. (4) Tests `tests/test_guest_api_keys.py` (22) and `tests/test_agent_connector.py` (15), plus three guest-key cases in `tests/test_admin_guests.js`.
>
> **Decisions a later reader must not undo by accident.** The connector speaks the **guest Create API** (`/admin/api/create*`), not `/v1`, because that is the surface a guest already has: the rules are shared rather than reimplemented, and nothing about the owner's `/v1` had to be widened. `/v1` **refuses every guest key** — `require_auth` refuses the prefix before loopback or the configured key is consulted, and a test walks every `/v1` route to prove it. A guest key is **not** trusted on loopback either: `require_admin`, `require_owner` and `require_local_app` refuse it *before* the loopback branch, or the credential an agent holds would be owner-level on the owner's own Mac. `_GUEST_UPLOAD_PATHS` is the whole list of upload doors a guest key may open (the two Create ones); everywhere else the body is refused at the door, before it is read.
>
> **Not done, deliberately:** no per-job spend per guest (the Gateway keeps no per-job money record — the page says "not recorded", never 0); driving whole Story Studio episodes through an API is the owner's later step; and the connector is not offered inside the console — it is a `python3` script the MCP client launches, shipped by `menubar/build.sh` beside the gateway source at `/Applications/gateway-kh.app/Contents/Resources/gateway/connectors/mcp/server.py`, or run from the repository's `connectors/mcp/server.py`; the README gives both paths. It is an MCP server, not a `/v1` route and not a new console page.
>
> **Evidence on the branch:** its own gate and JS logs in `/build/Developer/_orchestra/gateway-agent-connector/`; this release's final gate and JS evidence is under `/build/Developer/_orchestra/gateway-0211/`.
>
> **Gate and JS on this release's final source — `cd1aba7`, the release tree with both merges and the second review's five fixes; the only commit after it is this verification line, which changes no code, and the DMG is built from the commit this line makes, so the bundled build stamp names it.** The release gate of record (`scripts/run_gate.py`, default bounded pool): **175 files run, 175 passed, 0 failed, 2 skipped | 2687 tests in 174 files | `All 586 end-to-end checks passed` | 403.9 s wall, exit 0**. The two skips are the named `test_ui.py` (drives the installed app) and `test_fleet_standalone.py` (needs the live Hub); `summary.json` names **0 non-zero exits, 0 failed rows, no `Ran 0 tests` and no zero-test row**. Evidence: `/build/Developer/_orchestra/gateway-0211/release-20261030T032944Z/gate-of-record/` (console beside it). **Every JS suite** (`node --test`, one file at a time) on the same source: **19 files, 246 tests, 246 passed, every file exit 0**, logs in `…/release-20261030T032944Z/js-final/`. The orphan-fix branch's own earlier gate (on `dbb62b9`, before either merge) was 173 files / 2645 tests / 415.8 s, exit 0; it is history, not this release's proof. **Still open:** no push, no install, no `main` merge (by instruction). The owner's "prefer stopping the copy with less progress" is bounded by his own instruction that a stop happens only under the orphan guard: while neither copy has delivered the guard refuses both, so progress cannot pick a loser early; once one copy has delivered, that one is kept and the other is stopped. A pre-delivery stop would be a guard change with its own decision.
>
> **0.2.210 — the web Fleet page is the Fleet UI, and orphaned Hub batches get a guarded Stop — 2026-10-30. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.210` in `/build/Developer/_worktrees/gateway-kh/release-0.2.210`, cut from `main` **`adaf174`** (0.2.209) and merged with `feat/hub-orphan-batches-20261030` (**`6c02026`**) and `feat/web-fleet-like-app-20261030` (**`f7fd243`**), both cut from `ec5d7b1`; `VERSION` 0.2.210. This release's gate, JS, DMG, smoke, integration and screenshot evidence is in `/build/Developer/_orchestra/gateway-0210/`. **Merge conflicts were `docs/tasks/HANDOFF.md` in both merges and `gateway/admin_ui.html` in the second; resolved by intent, never by picking a side** — see below.
>
> **1 — The web console is an app shell, and Fleet is the native page as a web page (`a5653cb`).** `gateway/admin_ui.html` gains the native sidebar's groups (Overview; Create; Library; Connection; App), a top bar at phone width, and a Fleet page with one tab strip — Groups (the one list the browser page has always drawn), Locations, Machines, Models, Jobs, Story Studio — over panes holding the native tables and columns. `ConsoleSection.fleet` (`menubar/MainWindow.swift`) points the app's Fleet row at that page; the page's key is injected at document start (`__GATEWAY_ADMIN_TOKEN__`), **never in a URL**, and `__GATEWAY_IN_APP__` hides the page's own sidebar inside the app. The native `FleetSection.swift` and its views are **kept, unused, restorable — not deleted**; `AGENTS.md` now carries the mirror rule ("Gateway UI is built once, as a web page").
>
> **2 — Orphaned Hub batches: a warning, the evidence, and a Stop the Gateway guards (`aab0dc9`).** `gateway/orphans.py` classifies each site's running batches against this Gateway's own attempt log: `live` (not listed), `orphan_superseded` (a Gateway job names the batch and moved on, finished, failed or was cancelled) and `orphan_unknown` (no Gateway job at all). A batch's old id survives only in the attempt log, matched on what it ends with, so a longer id that merely contains it does not claim it. The Fleet payload gains `orphan_batches`, per-location `orphan_batches` / `orphans_checked` / `orphans_note`, and `orphans_auto_stop`. **Stop is server-guarded**: offered only when the owning job succeeded with every artifact present and non-empty, or failed/was cancelled with nothing left to collect; the cancel route is the existing one, the page posts `orphan_stop: true`, and the Hub's answer is drawn as *running signalled — not yet stopped*. **Automatic stop is off by default** (`DATA_DIR/fleet_orphans.json`, `GET/PUT /admin/api/settings/fleet-orphans`), applies the same guard, logs every stop, and **never touches `orphan_unknown`**. A site that did not answer, or whose jobs read failed, reads **Not checked**, never "none". All of it is behind `require_admin`: the Fleet read and the settings door refuse a guest session.
>
> **3 — Drop lengths, and each number's window (`aab0dc9`).** `fleet_history` gains per-subject drop length: longest, median, the typical gap between drops, and whether the last drop recovered by itself or after a startup action; and `problematic_basis` states the threshold the verdict came from. The 24 h / 7-day caption no longer prints a bare "N in 7 days" (the 0.2.209 wording) — each figure names its window — and an offline duration nobody measured says *offline time not measured*, never a zero. A drop length nobody measured says *Drop length not measured*.
>
> **Merge resolution by intent — the web page is the Fleet UI going forward, so the orphan UI lives inside it, not beside it.** `gateway/admin_ui.html` was rebuilt from the web branch's app shell and the orphan branch's behaviour was carried onto it: the warning line, the rows, the guarded **Stop** and the off-by-default **auto-stop switch** are the Jobs tab's (`#fleet-orphans`, `#fleet-orphan-setting`); the warning line is repeated in the Machines tab's warnings box (`fleetOrphanWarning`) so it is seen where the machines are; the **Not checked** state rides the orphan rows; the drop-length and problematic-basis text ride the strip tooltip and the Problematic badge. Nothing from either branch was dropped. `tests/test_fleet_orphans_view.js` — written against the pre-shell page's `#fleet-issues` — now reads `#fleet-orphans` and its harness holds the tab panes' nodes; every assertion it made before is unchanged.
>
> **4 — Every native Fleet action is on the web page now, with the routes, the bodies and the sentences the Swift views used (`1acbe40` + this change).** `gateway/admin_ui.html` carries the whole of `FleetStartup` / `FleetMacTrash` / `FleetTrash` / `FleetWorkPriority` / `FleetModelInstall` / `FleetModelPreset` / `FleetModelSpread` / `FleetRemoteRender` / `FleetStoryTab` / `FleetSection`'s actions, each posting the exact route, method and body the native code did, and confirming with the same words: **Machines** — startup actions the Mac's own report supports (Enable Auto-Start / Start Services / Restart Services, with the 409-and-`force: true` "is busy — Restart anyway?" second question), Rename (1–120 characters), Pause/Resume, Work Priorities (read-only when the Mac's Hub answers no policy; sends only `priority_order`), Lite Mode (every setting, with the native validation sentences), Network (normalised upper-case, empty goes back to the location's), Trash (read the folder, move to macOS Trash — **permanent delete stays in the app**, see below), macOS's 30-day Trash setting per Mac and for all Macs that are off, Enable Auto-Start on all Macs, Reset Job Counters, Refresh (`/fleet?force=1`), and the Remote final rendering switches (opt-in and the pause exemption). **Locations** — Credentials… (fleet token, then the Hub token for shared voices), Address… (test first, and "this address has not answered a test" before re-pointing), Network…, Refresh models, plus the Release card: publish a Studio Hub build with version/build/file name, the newer-release warning, `mode: all_at_once`, `force_full`, and the paced rollout's Stop/Resume with each step's own sentence. **Models** — install a model on one Mac (polling `model-downloads/{run}` with the native progress sentence and its 20-miss "lost track"), install on every Mac that can run it, each plan's progress by network, and Stop. **Jobs** — Stop a whole batch (the Hub's `queued_cancelled` / `running_signalled` / `running_pending` passed through) and Retry one failed item by index. **Story Studio** — Update Now per Mac and publishing a Story release. Every one is owner-only because the page's routes are: `require_admin` refuses a guest everywhere, and the owner-only switches (`render-optin`, `render-while-paused`, `model-preset`, remove a model) take `require_owner`, which the app's window and the owner's signed-in browser satisfy and a guest session never does. **The one action a web page truly cannot take** is `POST …/machines/{machine}/trash/delete`: it needs the app's own per-launch owner-console secret (`require_owner_console`, `X-Gateway-Owner-Console`), so the page offers the recoverable move and says the permanent delete is in the app; `tests/test_fleet_web_parity.js` allow-lists exactly that route, with its reason, and fails if the list grows or goes stale. **The parity test is the guard on all of it:** it reads every `/admin/api…` route literal in `menubar/Fleet*.swift` (including the `\(base)`-prefixed ones and the tails `FleetTrash.path` is called with), normalises Swift and JS interpolations to one wildcard, and fails when the Fleet page's own markup and script have no caller for a route. **Tests:** `tests/test_fleet_actions.js` (new, 23 — each action's scope, the two questions a busy restart asks, the validation sentences, the exact path/method/body, the Hub's answer read through `result.answer`, and the secrets' eyes), `tests/test_fleet_web_parity.js` (new, 4). The page's password inputs gained the existing eye control (`fleet-add-token` had none since `1acbe40`; `tests/test_secret_field.py` counts every one). Python page-reading suites re-run green (`test_settings_in_ui`, `test_custom_providers`, `test_price_evidence`, `test_secret_field`, `test_state_colours` — 47).
>
> **Files.** New: `gateway/orphans.py`; `tests/test_fleet_orphans.py` (34); `tests/test_fleet_orphans_view.js` (11 after the verification round); `tests/test_fleet_machines_view.js` (10); `tests/test_fleet_actions.js` (24 after the injection test); `tests/test_fleet_web_parity.js` (4); `tests/test_web_shell.js` (5). Changed: `gateway/admin.py`, `gateway/admin_ui.html`, `gateway/fleet.py`, `gateway/fleet_history.py`, `gateway/store.py`, `menubar/MainWindow.swift`, `menubar/render/render_main.swift`, `AGENTS.md`, `menubar/whats-new.md`, the native `Fleet*.swift`/harness files (the orphan branch's health and drop-length additions to the kept native page), and `tests/test_fleet_health_view.js` (17 after the merge), `tests/test_fleet_history.py` (40), `tests/fleet_health_harness.swift`.
>
> **0.2.210 release verification and the independent review's nine fixes — 2026-10-30. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** The release gate of record (`scripts/run_gate.py`, default bounded pool) on this branch's fixed tree (`df07855` plus the fixes below; **this paragraph is the only post-gate change**): **172 files run, 172 passed, 0 failed, 2 skipped | 2618 tests in 171 files | `All 586 end-to-end checks passed` | 429.9 s wall, exit 0**. The two skips are the named ones — `test_ui.py` (drives the installed app and takes over the owner's windows) and `test_fleet_standalone.py` (needs `GATEWAY_ALLOW_LIVE_GENERATION=1` and the real Hub). Evidence: `/build/Developer/_orchestra/gateway-0210/final-gated-20261029T194039/` (`summary.json` and 172 per-file logs; the console is the `…-console.log` beside it, kept outside because the runner refuses to reuse a non-empty dir). Every JS suite was run separately (`node --test tests/test_*.js`): **19 files, 242 tests, 242 passed, every file exit 0** (`…/gateway-0210/final-js/`). Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`) — version **0.2.210**, build **`df07855-202610291949`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild`, `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.210; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid (`codesign -v --strict`), every bundled `gateway/*.py` byte-equal to the worktree (`diff -r`, no differences), `whats-new.md`/`requirements.txt`/`requirements.lock` byte-equal, and the three `data/final-render` descriptors bundled (profile sha256 `ad3f1c9d47d5…`) — producing `dist/gateway-kh-0.2.210.dmg`, **53,120,609 bytes, SHA-256 `e8fbcc6004cb5cb99df44012a16d923bed5f860c916c9f1092bf86887e1a4d7c`**, `hdiutil verify` **VALID (exit 0)**, and the mounted bundle reading version 0.2.210 / build `df07855-202610291949`. A packaged headless smoke (bundled runtime + bundled source, temp profile, port 18790 and never 8787) answered `/health` with `build: df07855-202610291949`, 20 providers / 38 discovered models, `/api/version` 0.2.210, and exited on TERM with no listener left. `scripts/integration_check.py --gateway-root <this worktree>`, exit 0, **PASS** on every step: image job `succeeded`, 885,877 bytes in 118.8 s; TTS job `succeeded`, 261,180 bytes in 14.1 s (both machine `local`); render profile fps 24 at 1280x720; blob states `missing → complete`; render request refused with its stated reason `RENDER_DESTINATION_UNAVAILABLE`. **15 screenshots** in `/build/Developer/_orchestra/gateway-0210/shots/`: browser Fleet Machines/Locations/Jobs at 1400x900 light and dark and at 375 light, plus the native app showing the same web pages at 1100x800 light and dark (`menubar/render/render-page.sh "fleet:machines,fleet:locations,fleet:jobs"`). The fixture Gateway they came from ran on spare port 62647 (proxy) / 62124 (fixture), with the orphan rows produced by the real `gateway.orphans.annotate` over the render fixture's own snapshot. **The review's nine findings, all fixed in this tree:** **(1) script injection** — Hub-supplied ids no longer sit inside inline handlers; they ride `data-*` attributes read through `this.dataset` (machine actions, priority rows, remote-render switches, install Stop), and `tests/test_fleet_actions.js` renders ids containing `'` and `</script>` and asserts no escaped quote reaches a handler. **(2) the permanent Trash delete has a native entrance** — View → **Fleet Trash…** (`AppDelegate.openFleetTrash`) reads the fleet and opens the kept native `FleetTrash` sheet for a chosen Mac; the parity allow-list names it. **(3)** `fleet_history._drop_facts` reads the startup audit **once per Fleet read** and computes `recovered_by` only for the last drop; `tests/test_fleet_history.py` pins one audit call across three drops. **(4)** `problematic_basis` says **"… · longest X · median Y in 7 days"** — every number tied to its own window. **(5)** an **absent `orphan_batches` reads "not checked"**, never 0, on the Machines warning and the Jobs box (`test_fleet_orphans_view.js`). **(6)** the orphan **Stop asks for confirmation** first, and a declined question sends nothing. **(7)** `tests/test_fleet_orphans.py` asserts over the routes the page actually calls and **fails on an unknown path** (the old check named a `/admin/api/fleet-orphans` route that does not exist). **(8)** new `menubar/ConsoleNavigation.swift` plus `MainWindow`'s `decidePolicyFor` allow **only the console's origin in the main frame**; other web links open in the browser. `tests/test_console_navigation.py` drives the rule (another host, another port, a lookalike host) and asserts the web view asks it. **(9)** a Mac whose Hub reports no `completed`/`failed` says **"job counts not reported"**, never `0 done · 0 failed`. **One layout defect the screenshots caught:** at the app's 1100 pt window the Locations table pushed its action buttons past the pane; each button's label now stays on one line, the buttons wrap between each other, and Address… / Network… / Refresh models / Remove sit behind a **More…** disclosure, so the table fits with no clipped button. **Still open:** no push, no install, no merge to `main`; `test_ui.py` and `test_fleet_standalone.py` remain the two gate skips by their own rules.
>
> **Web Fleet — stage 1 of "one UI, built once, as web pages" — 2026-10-30. NOT PUSHED, NOT MERGED, NO DMG, VERSION NOT BUMPED (by instruction).** Branch `feat/web-fleet-like-app-20261030` in `/build/Developer/_worktrees/gateway-kh/web-fleet-0210`, cut from `release/0.2.209` **`ec5d7b1`**; evidence in `/build/Developer/_orchestra/gateway-web-fleet-0210/`. **The owner's ask:** *"can i view the fleet on the web and have it look like this? … why can't gateway?"*, then *"make sure anything built or updated in the future instantly appears [in both], like a mirror, instead of something we keep building each time."*
>
> `gateway/admin_ui.html` is now an **app shell** — the native sidebar's groups (Overview; Create; Library; Connection; App) from one `NAV` list, hidden in-app where the window already has one, a phone top bar with the same entries — and its **Fleet page is laid out like the native one**: the tab strip (Groups, Locations, Machines, Models, Jobs, Story Studio), Machines and Locations as the native tables with the native columns and words, all drawn from `/admin/api/fleet` (history, causes, `fleet_health`) and `/admin/api/story/instances`. **Drops wording (owner, 2026-10-30): every number names its window — "5 drops in 24 h · 42 h offline in 7 days".** A figure nobody measured says "—"/"Not measured yet", never a zero.
>
> **The Mac app's Fleet row now shows the web page** (`ConsoleSection.fleet`, key injected at document start — never a URL). The native **`FleetSection` / `Fleet*.swift` views are kept, unused, restorable**; `render-page.sh fleet-add` renders them in their own window. **Binding rule added to `AGENTS.md`:** Gateway UI is built once, as a web page; the app shows it; new UI never goes into a native Swift view except for what a web page cannot do — that list is in `AGENTS.md`.
>
> **Still native:** Overview, the six Create lanes' native forms, Voices, Analytics, What's New, Settings (the web shell shows a plain "in the app" note, no dead links). **Not in the web Fleet page yet:** the native-only actions (startup, Lite Mode, macOS Trash, install a model, a location's Test/Rename/Pause/Remove) show a plain note instead; the native Groups card's per-location nesting and the Models drill-down are future work.
>
> **Orphaned Hub batches, the drops cell's mixed windows, and drop lengths — 2026-10-30. NOT PUSHED, NOT INSTALLED, NOT MERGED, `VERSION` NOT BUMPED (by instruction).** Branch `feat/hub-orphan-batches-20261030` in `/build/Developer/_worktrees/gateway-kh/orphans-0210`, cut from `release/0.2.209` @ `ec5d7b1`. **The incident (2026-10-29):** a restart on site 1200 made the Gateway lose contact with 8 Hub batches, mark them `PROVIDER_SUBMISSION_UNCERTAIN`, re-dispatch each under a **new** upstream id — and all 8 Gateway jobs succeeded, while **3 old batches (12b2212bca, c2022ce32e, 60c5ccba54) kept running duplicate work on 1200 and only a person noticed**. **`gateway/orphans.py` (new) answers it at read time, not by polling.** `admin.fleet_snapshot` calls `orphans.annotate(out)` on the copies it already makes, so detection rides the existing fan-out: for each location's `running_jobs`, take only batches whose Hub `label` is `gateway-kh` (never another caller's work or local Hub use), and match each id against the jobs store — its current `upstream_job_id`, plus every id that survives in `attempt_log` prose (`"while polling batch <id>"`; matched as a whole token, never a substring). Classes: **`live`** (non-terminal and the current attempt — not listed), **`orphan_superseded`** (moved on, finished, failed or cancelled), **`orphan_unknown`** (no Gateway job names it at all). Each entry carries running seconds, the Hub's own `done/queued/running/retrying/total`, and `artifact_count`/`output_path` **when the Hub sends them — otherwise `null`, and the page says "not reported", never 0**. `/admin/api/fleet` gains per-location `orphan_batches` (a list, or **`None` when not checked**) + `orphans_checked`/`orphans_note`, and a top-level `orphan_batches` count, `orphans_checked_sites`, `orphans_unchecked_sites`, `orphans_auto_stop`. **A failed read is never an empty fleet:** `fleet._fetch_optional` now returns which optional paths failed, `_probe` publishes `jobs_read`, and a site that timed out *or* whose jobs route failed reads "not checked". **Stop** is the existing `POST /admin/api/fleet/controllers/{site}/jobs/{batch}/cancel` with `orphan_stop: true`; the server re-checks the guard (`orphans._stop_guard`) because "the button was not drawn" is not a refusal: allowed only when the job **succeeded with every artifact present and non-empty on disk**, or is **failed/cancelled with nothing left to collect**; `orphan_unknown` is never offered one. The Hub's answer passes through — `queued_cancelled` / `running_signalled` (**"signalled — not yet stopped"**) / `running_pending`. `cancel_fleet_batch` is the one implementation behind the route and the auto-stop. **Auto-stop** is `DATA_DIR/fleet_orphans.json`, **off by default**, validated as a real boolean (`"false"` refused), applied from `fleet._refresh` (the existing 15 s pass, no new poller), throttled per batch, using the same guard, **never touching `orphan_unknown`**, and written to `startup_audit.jsonl` (`orphan_stop` / `orphan_stop_refused`). Web console (`admin_ui.html`, small and add-only so the Fleet rebuild merges): one warning line, one row per orphan with progress/outcome/reason, a Stop button, and an "automatic stop" switch beside the problematic threshold. **Two Production fixes carried here:** the drops cell read **"5 in 24 h · 42 h off"**, mixing a 24 h count with 7-day offline time — fixed on **both** surfaces (`FleetHealth.shortSummary` and `fleetHistoryLine`), every figure names its window, and `annotate` now publishes `history_windows` so a reader cannot pair a number with the wrong span. Each recorded **drop** now carries its length (down→up), whether it recovered **by itself / after a startup action / unknown**, matched to `startup_control.recent_audit` (start/restart services, 10 min window); per subject: median, longest, typical gap, and `problematic_basis` ("4 drops in 24 h (threshold 4) · longest 2 min · median 1 min") shown wherever Problematic is shown; an unknown length says **"length not measured"**. **Tests:** `tests/test_fleet_orphans.py` (new, **34**) — every class, an attempt_log-only id, a longer id that must not claim it, label filtering, every guard branch, auto-stop off/on/unknown/logging, timed-out and failed-jobs reads, owner-only, the real store SQL, and the route guard; `tests/test_fleet_history.py` 30 → **39**; `tests/fleet_health_harness.swift` extended (decode + window labels + drop evidence); `tests/test_fleet_orphans_view.js` (new, **8**) and `test_fleet_health_view.js` 14 → **17**. **Still open:** no push, no `main`, no DMG, no install; the native Fleet **Stop button for orphans is not built** (the app's Fleet page still reads the web console for this), and the coordination with the web Fleet rebuild is by keeping this UI add-only.
>
> **0.2.209 — two Fleet/Content features and a claim that starts its own attempt — 2026-10-29. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.209` in `/build/Developer/_worktrees/gateway-kh/release-0.2.209`, cut from `main` **`757ff06`** (0.2.208) and merged with `feat/retire-clears-installs-20261029` and `feat/fleet-health-history-20261029`; merged head **`ec5d7b1`**; `VERSION` 0.2.209. This release's own gate, JS, DMG and smoke evidence is in `/build/Developer/_orchestra/gateway-0209/`; the two branches' own records stay in `…/gateway-retire-clears-0209/` and `…/gateway-fleet-health-0209/`. **The only merge conflict was this file** (both branches prepend an entry): both are kept and merged into the one 0.2.209 section below. `gateway/admin.py` and `gateway/admin_ui.html` — touched by both branches — auto-merged; both features were re-proved together by this release's gate, JS suites and packaged smoke, and the combined diff got an independent adversarial review (its findings are recorded with the verification line at the end of this entry).
>
>
> **0.2.209 — two Fleet/Content features and a claim that starts its own attempt — 2026-10-29. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.209` in `/build/Developer/_worktrees/gateway-kh/release-0.2.209`, cut from `main` **`757ff06`** (0.2.208) and merged with `feat/retire-clears-installs-20261029` and `feat/fleet-health-history-20261029`; merged head **`ec5d7b1`**; `VERSION` 0.2.209. This release's own gate, JS, DMG and smoke evidence is in `/build/Developer/_orchestra/gateway-0209/`; the two branches' own records stay in `…/gateway-retire-clears-0209/` and `…/gateway-fleet-health-0209/`. **The only merge conflict was this file** (both branches prepend an entry): both are kept and merged into the one 0.2.209 section below. `gateway/admin.py` and `gateway/admin_ui.html` — touched by both branches — auto-merged; both features were re-proved together by this release's gate, JS suites and packaged smoke, and the combined diff got an independent adversarial review (its findings are recorded with the verification line at the end of this entry).
>
>
> **1 — Fleet health: sort through offline, problematic, and a likely cause with its evidence.** `gateway/fleet_history.py` is new and does all of it. Transitions are recorded from the fan-out the Gateway already does — `fleet._refresh` calls `fleet_history.observe(rows, machines)` beside `machines_flat`, so there is **no second poller** — into `DATA_DIR/fleet_history.json`, written **only when a state actually changed** (a stable fleet rewrites nothing; pinned by a test that compares the file's mtime). The store is atomic and bounded three ways: events older than `WINDOW_DAYS` (7) are pruned on every write, `MAX_EVENTS_PER_SUBJECT` (400) caps each subject, `MAX_SUBJECTS` (2000) caps the file, and the last pre-cutoff event is kept so the window knows the state it opened in; it survives a restart because it is a file, not memory. Derived per machine and per site: `offline_since` / `online_since`, `drops_day` / `drops_week`, `flips_day` / `flips_week`, `offline_seconds_day` / `offline_seconds_week`, a 7-day `days` strip of offline fractions, a `pattern` hint, `last_drop` with the Gateway's own reason word, and **`problematic`** against the owner's own threshold (`DATA_DIR/fleet_health.json`, default **4 drops in 24 h**, `GET/PUT /admin/api/settings/fleet-health`, range 1–100; an out-of-range value is a 422 that names the range rather than a silent clamp). A likely cause is published **only with its evidence** — site down (blaming the controller, not its members), `no_hub`, `unreachable` with how long since it was heard, `unauthorized` (a credential, not a network), never-connected with its address, a slow Ethernet link with its measured speed, Wi-Fi when it drops, services not starting on launch / not opening at login, studios down while the Hub is up, a Hub behind (naming both versions), a nightly pattern — and **"No evidence yet"** when nothing supports one. `/admin/api/fleet` gains `history`, `causes` and a top-level `fleet_health` **on copies** of the machine rows, so the cached snapshot routing and admission read never gains a settings-dependent verdict (asserted). Both surfaces draw the one server-side verdict: the browser console a single list of machines and sites with `Show` (Everything / Offline now / Problematic) and `Sort` (Offline first / Problematic first / Site / Last seen, longest ago / Drops in 24 h / Name), each row's cause in plain words with its evidence in the tooltip, the 7-day strip and the threshold box; the app's Machines tab gains **Health** (Problematic / Offline *duration* / *N* drops / Stable / "—"), **Seen**, **Drops · 7 days** and **Likely cause** columns, the Locations tab gains Health and Likely cause per site, `FleetHealth.Filter` backs the `Show` popup, and the threshold popup is built from the bounds the Gateway publishes (a Gateway that reports none leaves it disabled saying "not reported"). **Three honesty traps, all pinned:** a whole site being down does **not** make its members problematic (a transition observed while its site was unreachable is stored with the `site_down` reason and **excluded from that machine's drop count**, so one dead controller is never five problematic Macs); a figure nobody measured is `null`, never `0` (an unobserved subject publishes `observed: false`, every number `null`, a strip whose days are `null` — drawn as a dashed box, not an empty bar — and `problematic: null`); and a Mac that is down now never reads "Stable" (the Health cell says `Offline 7d`, with the full duration and clock time in its tooltip).
>
> **2 — Retire a pack, and — if you mean it — remove it from every Story app.** **Owner decision, 2026-10-29:** retiring a Studio Library pack on the Gateway may **also** ask every Story app to remove its copy — a per-pack, deliberate option, **off by default**. Retire and Remove keep their meaning (Retire = mark, keep the file, hide it from the catalogue, existing installs keep working; Remove = retired packs only, moves the pack's whole library folder to `DATA_DIR/content-trash/` with a manifest, never `rm`). **Stored on the release row:** `remove_from_installs: true` and `remove_from_installs_at` as **int unix seconds** — `retired_at` stays the shelf's own ISO stamp and the two units differ on purpose, so do not "unify" them. `retire_pack(..., remove_from_installs=True)` writes it; `set_remove_from_installs(pack_id, version, enabled=)` sets **or clears** it later (`POST /admin/api/content-shelf/remove-from-installs`, where `enabled` must be a real bool); **un-retiring pops both fields**; and a plain retire **never clears an existing ask** — only those two explicit doors move it, because a default that clears a durable ask on customers' Macs is that defect shape with the sign flipped. A live pack refuses the ask `409 CONTENT_PACK_NOT_RETIRED`, and the retire route refuses a non-boolean `remove_from_installs` with `422 CONTENT_REQUEST_INVALID` rather than coercing `"false"` into true. **The catalogue shape chosen — `retired_packs` on `GET /v1/content/products`:** retired rows stay filtered out of `products` (Story must never see one as installable), so the same response now also carries `[{pack_id, release_id, retired_at, remove_from_installs, remove_from_installs_at}]`, sorted, inside the existing 1 MiB `_bounded`. It is an **extra top-level key**, which Story's parser tolerates (`parseContentStoreCatalog` reads `products` and keeps `raw`), and a real Story client consumed it in `tests/test_content_delivery_integration.py`. **The report back:** `POST /v1/apps/story/checkin` accepts an optional `pack_removals: [{pack_id, outcome: removed|kept_in_use|failed, channels?, detail?}]`; only those three outcomes are stored (anything else dropped, never guessed — "removed" is a claim about a customer's machine), one entry per pack (the later word wins), capped at 200 packs / 50 channels, and a check-in that **omits** the key keeps the Mac's last report (an older Story states nothing, which must not read as "removed nothing"). `story_releases.pack_removal_report()` merges the Macs into `GET /api/content-shelf` as **`remove_report` on packs that carry an ask only**, so every other row keeps its exact previous shape. **A Mac counts only if it checked in AFTER `remove_from_installs_at`** (the pack's latest ask across its versions) — a report made before the ask reads "not reported yet", never "removed"; a Mac that never checked in leaves `total_instances` 0 and the page says "no Story Mac has checked in yet". The shelf row draws the owner's sentence shape: `removed on 1 of 4 Story Macs · kept on macmini-angkor: used by channel Night Falls · failed on mac-c: … · not reported yet: mac-d`. **Admin UI:** Retire… opens a native `<dialog>` (the console's first) holding the retire sentence, **one checkbox — "Also remove from every Story app", unticked every time it opens** — and Retire / Cancel, where **Cancel closes and does nothing**; the second `confirm()` this replaced made Cancel mean "still retire", which reads backwards (review note, 2026-10-29). A retired row gains *Also remove from Story apps* / *Keep on Story apps*. A WebKit without `<dialog>.showModal` (the app's floor is macOS 11) gets the same controls inline via the `open` attribute rather than losing the choice; with no dialog element at all the row retires only and the removal stays on the retired row. **There is no native menubar equivalent** — `menubar/PackLibrarySection.swift` only opens `/admin#packs`, and nothing under `menubar/` knows `content-shelf`. **Still open on this feature:** **Story does not read `retired_packs` or send `pack_removals` yet** — until it does, the Gateway stores and publishes the ask and the shelf honestly reports "not reported yet".
>
> **3 — A claim begins its own attempt (the queued 0.2.208-review fix).** `store.mark_running` now also sets `attempt_started_at = NULL` and `restart_resent_at = NULL`. A claim is a new attempt and `begin_dispatch` is a separate step, so in that gap `worker._silent_since` still read the **previous** attempt's stamp: `probe_claim_window.py` (r3) failed the job `PROVIDER_TIMEOUT` **retryable** with `sends: []` — before anything was sent, which makes Story submit again. The same probe on this branch: `sweep.failed: 0`, `silent_since_age_s: 0`, one send, `succeeded` (`probes/claim_window_fixed.out`); reverting the two columns turns 2 of the 3 new probes red (`tests/test_dispatch_stalls_20261029.py`, `ClaimWindowTest`).
>
> **Backup.** `gateway/backup.py` now carries `DATA_DIR/fleet_health.json` among the local settings, because the *problematic* threshold is a number the owner chose and a restore has to bring it back; the 7-day history beside it is observed and comes back on its own. `tests/test_backup.py` pins the declared role against the file `fleet_history` actually reads (a plausible path nothing writes would look like a recovery point and not be one) and round-trips a chosen threshold through pack and restore — **19 tests, was 18**.
>
> **What's new.** `menubar/whats-new.md` gained one plain-words section per feature under `## 0.2.209 — 2026-10-29`: the Fleet page's Health / Seen / Drops / Likely cause columns with their `Show` and `Sort`, and the retire box with "remove from every Story app" and the Mac-by-Mac report. `tests/test_whats_new_page.py` keeps the newest heading matched to `VERSION`.
>
> **Two traps for the next reader.** The API's window fields are `drops_day` / `drops_week`, **not** `drops_24h`: `JSONDecoder`'s `.convertFromSnakeCase` turns a digit after an underscore into `drops24H` (measured, and it decodes to `nil` silently), so a `drops24h` property would publish nothing forever. And **the flapping verdict is never computed in a client** — both surfaces draw `history.problematic` as sent, so a changed threshold cannot mean two things on two pages. From the retire work: the two timestamp units (`remove_from_installs_at` unix seconds, `retired_at` ISO) are deliberate, and a plain retire must never clear an existing ask.
>
> **A layout lesson, learned the expensive way:** the health filter first went into the shell's own `Show` row, and `test_window_size` caught the window growing **39 pt on every section** — a required width in any page propagates to the window, and one wide row moves the whole app's minimum. It now lives on the Machines tab's accessory strip (which took the minimum height 1 pt over instead, fixed by lowering the table floor 120 → 90). **When you add a control, add it to the tab it belongs to, not to the shell's shared row.**
>
> **Files.** New: `gateway/fleet_history.py`; `menubar/FleetHealth.swift` (verdict, durations, last-seen, cause and strip logic, plus `FleetHistoryStripView`); `tests/test_fleet_history.py` (30); `tests/test_fleet_health_view.js` (14); `tests/test_fleet_health_native.py` (1) + `tests/fleet_health_harness.swift`. Changed: `gateway/fleet.py` (`_probe` now publishes a machine-readable `error_kind` — `refused` / `timeout` / `http` / `server_disconnected` — because the `error` sentence is for a person and a hint must not parse prose; `_refresh` records the observation), `gateway/admin.py` (the copy + annotate, the two settings routes, the remove-from-installs door), `gateway/admin_ui.html`, `gateway/content_delivery.py`, `gateway/store.py` (`mark_running`), `gateway/story_releases.py`, `gateway/backup.py`, `menubar/FleetModels.swift` (`FleetHistory`, `FleetCause`, `FleetHealthSetting`, and `history`/`causes` on both machine and location rows), `menubar/FleetSection.swift`, `menubar/FleetTabs.swift` (four machine columns, two site columns, `FleetFilter.health`; `fleetPinned`/`fleetColumnStack` are no longer file-private so `FleetHealth.swift` builds the same cell; the Machines table's height floor 120 → 90), `menubar/render/fleet_fixture.py` (**`seed_history`** drives the module's own `observe` over a fake week — a flapping Mac, a nightly dropper, a site down 6 h, one Mac nobody measured — so the rendered pages show the feature, and `ctrl-garage` now states `error_kind`), `menubar/whats-new.md`, plus `FleetHealth.swift` added to the four native-compile lists in `tests/` that name their sources and the JS suite's source slice moved to the new block start.
>
> **Tests, per branch (this release's gate re-runs all of them).** `tests/test_fleet_history.py` **30** (new), `test_fleet_health_view.js` **14** (new), `test_fleet_health_native.py` **1** (new), `test_content_retire_remove.py` 9 → **17** (retire with/without the option, set/clear later, live-pack refusal, un-retire clears, catalogue `retired_packs` over HTTP, admin validation incl. `"false"` refused, the shelf report incl. a pre-ask report), `test_story_releases.py` 25 → **31** (stored report, unknown outcome dropped, last-word-wins, omission keeps the last report, only post-ask Macs counted), `test_dispatch_stalls_20261029.py` 32 → **35** (`ClaimWindowTest`), `test_content_shelf_ui.js` 22 → **26** (the dialog: opening posts nothing, the box is unticked and cleared on every open, ticking posts `remove_from_installs: true`, Cancel posts nothing, and the no-`showModal` fallback still asks), `test_backup.py` 18 → **19**, and the new door is named in `tests/test_body_ceiling.py`'s `_SELF_BOUNDED`. **Screenshots** from `menubar/render/render-page.sh` (fixture history seeded by the fixture itself): `fleet:machines` at 1400x900 and 1600x900 and `fleet:locations` at 1400x900, kept in `_orchestra/gateway-fleet-health-0209/render/`.
>
> **Gate of record on the final source (`f609c01`, this release's own; the only commit after it is this verification line)**: **170 files run, 170 passed, 0 failed, 2 skipped | 2571 tests in 169 files | `All 586 end-to-end checks passed` | 424.3 s wall, exit 0**; the two skips are the named `test_ui.py` (drives the installed app) and `test_fleet_standalone.py` (needs the live Hub), and `summary.json` names **0 non-zero rows and no `Ran 0 tests`**. Evidence `/build/Developer/_orchestra/gateway-0209/gate-of-record-20261030T001120/` (console beside it); an earlier full run on the pre-review tree `353884a` gave the same counts (170/170/0, 2571 tests, 586 checks, 413.9 s, `gate-20261030T000147/`). **Every JS suite** (`node --test`, one file at a time, logs in `…-js/`): **15 files, 202 tests, 0 failed, every file exit 0** — `test_models_browser` 35, `test_content_shelf_ui` 26, `test_console_actions` 21, `test_release_mock_selfcheck` 17, `test_pools_view` 16, `test_fleet_health_view` 14, `test_queue_view` 13, `test_sfx_view` 13, `test_create_voice_picker` 12, `test_concurrency_tabs` 9, `test_price_ui` 8, `test_fleet_connection_view` 7, `test_admin_guests` 5, `test_admin_accessibility` 4, `test_create_empty_video` 2. **Build**: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.209**, build **`f609c01-202610291720`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.209; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid (`codesign -v --strict`), every bundled `gateway/*.py` byte-equal to the worktree (`diff -r --exclude=__pycache__` empty), `whats-new.md` byte-identical and the three `data/final-render` descriptors bundled and identical — DMG **`/build/Developer/_worktrees/gateway-kh/release-0.2.209/dist/gateway-kh-0.2.209.dmg`, 51,604,834 bytes, SHA-256 `1d9477ff9fd3a49e44e163ba1bbdb53288d9898bcae2cc46382f1e9c06a21409`**; `hdiutil verify` reports the checksum **VALID**; mounted read-only, the mounted app reports **0.2.209 / `f609c01-202610291720`**, its source and `whats-new.md` byte-equal to the worktree, `codesign` clean. **Packaged headless smoke** (the **mounted** bundle's own runtime and source, temp profile, port **18789** — never 8787, never Mac 1301): `/health` answered `status: ok`, `service: gateway-kh`, `build: f609c01-202610291720`, **20 providers / 38 discovered models**, the process exited on TERM (**143**) and left **no listener**. **Integration check** `scripts/integration_check.py --gateway-root <worktree>`: **exit 0, PASS** — throwaway Hub + Gateway on free loopback ports, the two model packages APFS-cloned from `~/StudioHubModels` (source untouched), image job `succeeded` 885,877 bytes in 116.3 s, TTS job `succeeded` 476,220 bytes in 16.1 s, both on machine `local` and routable, `python-multipart` present in both packaged runtimes, render profile fps 24 at 1280x720 with blob states `missing->complete`, and the render request refused with the honest `RENDER_DESTINATION_UNAVAILABLE`; its lab moved to `_trash/integration-check/studiohub-integration-check-20261030-002405-7745`. **Independent adversarial review of the combined diff (`757ff06..f609c01`), read from the code rather than the two branches' reports:** **merge interaction (`admin.py`, `admin_ui.html`) — none found.** No duplicated route or top-level definition (the two `set_models_enabled` functions are already on `757ff06`, one per route, and are untouched), no conflict markers, and the two branches' edits sit in different regions (content-shelf handlers and the shelf section vs the fleet snapshot/settings handlers and the fleet block). **Security:** both new admin doors take `Depends(require_admin)`, which **refuses a guest session outright** and keeps a cookie write behind `same_origin`; the fleet-health body is a JSON object and `set_settings` refuses a bool, a non-integer and anything outside 1–100 with a 422 naming the range; `remove-from-installs` requires `application/json`, reads through the strict `_json` (duplicate keys and non-finite numbers refused) and refuses a non-bool `enabled` rather than coercing `"false"`; `pack_removals` rides the existing authenticated Story check-in, is whitelisted to three outcome words, one entry per pack, 200 packs / 50 channels / 200-char ids / 400-char detail, and the whole body is already capped at 4 MiB by the ASGI body middleware (the new door is named in `_SELF_BOUNDED`). **Growth and blocking:** `fleet_history.json` is bounded by construction (7-day window, 400 events per subject, 2000 subjects), written **only on a real transition**, under `_refresh_lock` but **outside** `_cache_lock`, so a write can never delay a cached routing read; at every cap at once it would be ~30 MB re-serialised per transition, a fleet two orders of magnitude larger than this one. **Live dispatch is unchanged:** the history is annotation on copies (`admin.fleet_snapshot` copies the machine rows before `annotate`), `error_kind` is additive, and nothing routes on either. **An existing 0.2.208 store needs no migration:** no schema change (`attempt_started_at` and `restart_resent_at` already exist), `fleet_history.json` / `fleet_health.json` are new files that create themselves and are absent (defaults) until used, and every new key on a stored record (`instances.json`, `content-delivery/state.json`) is read with `.get()`. **Findings: two fixed, three left deliberately.** Fixed: `menubar/FleetSection.swift` had the `healthFilterChanged` doc block stacked on `updateThreshold` (`ce2f6a3`), and `tests/test_dispatch_stalls_20261029.py` ended with a `seconds_of` helper nothing called (same commit); the `whats-new` section also described the Show **and** Sort menus as if one surface had both, corrected to where each control actually lives (`f609c01`). Left as designed, stated so nobody "fixes" them back: the app's `Show` popup filters **machines only** while the browser console filters machines *and* sites (`FleetHealth.matches(_:location:)` is exercised by the harness but not wired to the Locations tab — the popup is labelled "Show … machines"); `retired_packs` rides inside the existing 1 MiB `_bounded`, so a catalogue already at the cap could answer 507 once packs retire (~25 KB at the shelf's real size); and `fleet_history._write` swallows an `OSError`, so an unwritable data dir stops persisting history rather than failing the fan-out (the page still derives from the in-memory store). **Nothing on the owner's machine was replaced:** `--no-install`, no `/Applications` write, port 8787 never bound, Mac 1301 never contacted. **Worktree prep, git-excluded:** `.venv` and `menubar/vendor` are symlinks into the primary checkout (a fresh worktree has neither the venv nor the vendored runtime), and `data/final-render/`'s three descriptors were copied in. **Still open:** no push, no `main` merge, no install; this worktree's `dist/` held no DMG before this build, so nothing was moved, and the 0.2.208 DMG stays in the 0.2.208 worktree.

> **0.2.208 review round 2 — a re-route begins a new attempt — 2026-10-29. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.208` in `/build/Developer/_worktrees/gateway-kh/release-0.2.208`, head **`adcdda6`**; probes in `/build/Developer/_orchestra/gateway-0208-review-r2/`. **HIGH — the liveness sweep failed a re-routed job while its new site was submitting.** `store.record_route` cleared `upstream_job_id` and stamped nothing, and `begin_dispatch` only stamps `dispatch_started_at` while it is NULL (the queue-wait statistic reads that column), so `_silent_since` still pointed at the job's **first** dispatch: `probe_reroute_sweep.py`'s job was 30 min old when it moved to a second site and the sweep failed it `PROVIDER_TIMEOUT` **retryable** mid-submit (`failed: 1`, `recovery_state: retryable`, and the second answer landed as `late_result_state: succeeded` on a failed row) — and Story re-submits a retryable failure, which risks a duplicate charge. New column **`attempt_started_at`**, written by `store.begin_dispatch` on every new send and by `store.record_route` on every re-route (the `_next_machine`/`DEADLINE_EXCEEDED` moves), with `restart_resent_at` kept for the restart re-send; `worker._silent_since` takes `max(dispatch_started_at, restart_resent_at, attempt_started_at)` and `dispatch_started_at` itself is untouched (`store.queue_wait_by_modality` reads it). **Audit of every other way a job's destination changes:** `rebind_unsent` only ever moves a job that has never been sent (`dispatch_started_at IS NULL`, no upstream id) and `begin_dispatch` stamps the send that follows; `defer(new_round=True)` returns the job to `accepted`, where the sweep never looks; no other store write sets `provider_id`/`model_id`. Probe after the fix: sweep `failed: 0`, the row stays `running` with its old `dispatch_started_at` age (1800 s shown), and it settles `succeeded` with no `late_result_state`. **Gate on `adcdda6`**: **168 files run, 168 passed, 0 failed, 2 skipped | 2523 tests in 167 files | `All 586 end-to-end checks passed` | 389.2 s wall**, evidence `gate-r2-final/` (console `gate-r2-final-console.log`). Every JS suite separately: **184 tests, every file exit 0** (`js-r2/`). **Build**: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`) — version 0.2.208, build **`adcdda6-202610290202`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` agree; bundled `VERSION` 0.2.208; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid, every bundled `gateway/*.py` byte-equal to the worktree, `whats-new.md` and the `data/final-render` profile descriptor identical — DMG **`/build/Developer/_worktrees/gateway-kh/release-0.2.208/dist/gateway-kh-0.2.208.dmg`, 52,871,046 bytes, SHA-256 `30ca72bbe01e3514f3a36ae96b138e7898eeb47bdfac70b31773b942dee4a107`**, `hdiutil verify` **VALID**. **Packaged headless smoke** (bundled runtime + source, temp profile, port 18789): `/health` `status: ok`, `build: adcdda6-202610290202`, 20 providers / 38 discovered models, TERM exit 143, no listener left. **Integration check**: **PASS** — image job 885,877 B in 112.5 s, TTS 353,340 B in 14.1 s, both machine `local`, render refused `RENDER_DESTINATION_UNAVAILABLE`; lab in `_trash/integration-check/studiohub-integration-check-20261029-090547-17378`. **Nothing on the owner's machine was replaced:** `--no-install`, no `/Applications` write; port 8787 never bound; Mac 1301 never contacted. **Still open:** no push, no `main`, no install; the previous DMG is in `_trash/dist/`.

> **0.2.208 pre-install review — four findings fixed — 2026-10-29. NOT PUSHED, NOT INSTALLED, NOT MERGED TO `main`.** Branch `release/0.2.208` in `/build/Developer/_worktrees/gateway-kh/release-0.2.208`, head **`9f19473`**; probes and findings in `/build/Developer/_orchestra/gateway-0208-review/`. Every fix has a test that fails before it (proved by reverting the source and running that test). **1 (HIGH) — the liveness sweep failed a job the restart had just re-sent.** `_resume_interrupted` stamps `restart_resent_at` and leaves `dispatch_started_at` at its pre-shutdown value, and `_check_running_liveness` measured silence from the old stamp: `probe_resend_race.py`'s two oldest jobs (2 h and 20 min) were failed `PROVIDER_TIMEOUT` **retryable** (`failed: 2`) 20 s into their own re-send — and a retryable failure on work that may be live upstream is what makes Story submit again, so it can be charged twice. `worker._silent_since` now measures from `max(dispatch_started_at, restart_resent_at)`; the probe re-sends each job once and all four succeed (`failed: 0`, `sent_times: 1`, no `late_result_state`). **The other paths that settle a running job were audited and all force an unknown-outcome hold once a send may be in flight** (`_dispatch`'s `except` through `never_sent`/`unknown`, `_run`'s catch keyed on `dispatch_started_at`/`upstream_job_id`, `resend` forcing `unknown` even when the adapter claims nothing was accepted, and the terminal failure after the attempt ceiling only on `unknown=False`); the sweep was the only plain-retryable one. **2 (MEDIUM, owner's decision) — the byte reservation is released with the slot again.** Holding it through the route refused a second wave of 8 × 12 MiB with 503 while every route simply answered for a second (`probe_wave.py`: **16 × 503 before, 16 × 200 after**, at both a 0 s and a 1 s route); Story does not retry an upload, so that fails a scene, and production traffic beats the memory concern (which only an authenticated caller could exploit). The budget bounds **simultaneous reception**, and `_declared` / `_bounded_stream` say so. `_busy(limit)` and its `limit` field stay. **3 (LOW) — `waiting.since` was a monotonic reading.** `_acquire_member` published `pool._clock()`; `api._iso` renders that field as a date, so the page would print a 1970 time or drop it. It publishes wall clock now, as the worker's own waiting reason does. **4 (LOW) — the capability snapshot could take a whole location's discovery down.** `discovery._hub_controls` called `response.json()` unguarded (an old Hub answering 200 with a page raised out of discovery) and used the catalogue's 12 s timeout. It has its own `CAPABILITIES_TIMEOUT` (3 s, connect 2 s) and answers `None` for an error status, a non-JSON 200, a non-object payload or a `models` that is not a list — the location still discovers its models and only the guidance is absent (pinned end to end through `_studio_hub`). **Gate on `9f19473`**: **168 files run, 168 passed, 0 failed, 2 skipped | 2520 tests in 167 files | `All 586 end-to-end checks passed` | 400.5 s wall**, evidence `gate-review-r1/` (console `gate-review-r1-console.log`); the two skips are the named `test_ui.py` and `test_fleet_standalone.py`. Every JS suite run separately: **184 tests, every file exit 0** (`js-review/`). **Build**: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.208**, build **`9f19473-202610290120`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.208; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid (`codesign -v --strict`), every bundled `gateway/*.py` byte-equal to the worktree (`diff -r` exit 0), `whats-new.md` byte-identical, and the three `data/final-render` descriptors bundled (profile sha256 `ad3f1c9d47d5…`) — producing `/build/Developer/_worktrees/gateway-kh/release-0.2.208/dist/gateway-kh-0.2.208.dmg`, **54,026,175 bytes, SHA-256 `2e28c251ef9079d9c4e5da96988738089f372ec7ad634bc4c7b80ef6a9430226`**; `hdiutil verify` reports the checksum **VALID**; mounted read-only, the app reports **0.2.208 / `9f19473-202610290120`**, its source byte-equal to the worktree and `codesign` clean. **Packaged headless smoke** (bundled runtime + bundled source, temp profile, port **18789** — never 8787, never Mac 1301): `/health` answered `status: ok`, `build: 9f19473-202610290120`, **20 providers / 38 discovered models**, and TERM left **no listener**. **Integration check** `scripts/integration_check.py --gateway-root <worktree>`: **PASS** — image job `succeeded` 885,877 bytes in 112.6 s, TTS job `succeeded` 199,740 bytes in 14.1 s, both machine `local`, and the render request refused with the stated reason `RENDER_DESTINATION_UNAVAILABLE` (one local Hub has no render node); its lab moved to `_trash/integration-check/studiohub-integration-check-20261029-082411-46774`. **Nothing on the owner's machine was replaced:** `--no-install`, no `/Applications` write at any point; port 8787 was never bound by this work and Mac 1301 was never contacted. **Still open:** no push, no `main`, no install; the previous DMG was moved to `_trash/dist/` by the build script itself.

> **0.2.208 release assembled and verified — 2026-10-29. NOT PUSHED, NOT MERGED TO `main`, NOT INSTALLED.** Branch `release/0.2.208` in `/build/Developer/_worktrees/gateway-kh/release-0.2.208`, cut from `ce72f54` (`fix/dispatch-stalls-20261029`) and merged with `fa2402f` (`feat/delivery-instruction-gateway-20261029`); merged head **`6ed1c58`** (tree `87fafdce`), `VERSION` 0.2.208. **One conflict, `docs/tasks/HANDOFF.md`** (both branches prepend an entry): kept both entries and added one combined delivery-instruction line, and `menubar/whats-new.md` gained the matching *Say how a line should be delivered* section. `gateway/api.py` auto-merged — the delivery guard `_refuse_a_delivery_instruction` sits in the TTS refusal chain, beside the dispatch/pool changes, so both behaviours are kept and neither was edited to fit the other. **Gate on `6ed1c58`** (`scripts/run_gate.py --evidence <dir>`, 3 workers): **168 files run, 168 passed, 0 failed, 2 skipped | 2514 tests in 167 files | `All 586 end-to-end checks passed` | 404.1 s wall | exit 0**; the two skips are the named `test_ui.py` (drives the installed app) and `test_fleet_standalone.py` (needs the live Hub); `summary.json` names 0 non-zero rows and no `Ran 0 tests`. Evidence `/build/Developer/_orchestra/gateway-0208-release-20261029/gate-20261029T001950Z/` (console beside it). **Every JS suite** (`node --test` one file at a time, logs in `…/js/`): **13 files, 167 tests, 0 failed, every file exit 0** — `test_models_browser` 35, `test_content_shelf_ui` 22, `test_console_actions` 21, `test_pools_view` 16, `test_queue_view` 13, `test_sfx_view` 13, `test_create_voice_picker` 12, `test_concurrency_tabs` 9, `test_price_ui` 8, `test_fleet_connection_view` 7, `test_admin_guests` 5, `test_admin_accessibility` 4, `test_create_empty_video` 2. **Build**: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.208**, build **`6ed1c58-202610290028`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.208; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid, every bundled `gateway/*.py` byte-equal (`diff -r --exclude=__pycache__ gateway <bundle>/gateway` empty), `whats-new.md` and `requirements*` byte-equal, the three `data/final-render` descriptors bundled — DMG **`/build/Developer/_worktrees/gateway-kh/release-0.2.208/dist/gateway-kh-0.2.208.dmg`, 53,648,874 bytes, SHA-256 `6a298b2a2759d337df112f62489ea3bb1d1857cc0d2f468c0d7d09edf7c24ec4`**; `hdiutil verify` reports the checksum **VALID**; mounted read-only and the mounted app reports **0.2.208 / `6ed1c58-202610290028`**, its source byte-equal to the worktree and `whats-new.md` identical, `codesign -v --strict` clean. **Packaged headless smoke** (bundled runtime + bundled source, temp profile, port **18789** — never 8787, never Mac 1301): `/health` answered `status: ok`, `build: 6ed1c58-202610290028`, **20 providers / 38 discovered models**, and the process exited on TERM (143) with **no listener left**. **Integration check** `scripts/integration_check.py --gateway-root <worktree>`: **exit 0, PASS** — throwaway Hub + Gateway on free loopback ports (59424), the two model packages APFS-cloned from `~/StudioHubModels` (source untouched), image job `succeeded` 885,877 bytes in 112.5 s, TTS job `succeeded` 276,540 bytes in 14.1 s, both on machine `local` and routable, `python-multipart` present in both packaged runtimes, render profile fps 24 at 1280x720, blob states `missing->complete`, and the render request refused with the honest `RENDER_DESTINATION_UNAVAILABLE`; its lab moved to `/build/Developer/_trash/integration-check/studiohub-integration-check-20261029-073112-16024`. **Nothing on the owner's machine was replaced:** `--no-install` at every point, `/Applications/gateway-kh.app` untouched (`Info.plist` mtime still `2026-10-27T11:19:16Z`), no gateway process left running here, and the live Gateway on Mac **1301** (127.0.0.1) was never contacted. **Install note for 1301:** copy that DMG there and right-click **Open** (ad-hoc signed, not notarised); it reports build `6ed1c58-202610290028`, so *Check Gateway* can confirm both halves. Not done here, by instruction.
> **0.2.208 — a per-model delivery (emotion) instruction: read live from the Hub, forwarded on speech, refused where the model lacks it — 2026-10-29. MERGED INTO `release/0.2.208`, TOGETHER WITH THE DISPATCH-STALL FIXES; NOT PUSHED, NOT INSTALLED.** The Audio lane now carries **Delivery / emotion** (`delivery_instruction`), an engine direction such as *warm, calm, slightly amused* — never a voice choice and never inferred from a model's name or family. Support is the model's own live Hub record: the `delivery-instruction` token in the catalogue row's `capabilities` **or** the snapshot's `controls.capabilities` (either is enough), with guidance at `controls.delivery_instruction` of `/api/hub/capabilities` (`kind`, `engine_form` — `instruct` for Breeze TTS 2, `text_prefix` for VoxCPM2 — `max_characters`, up to three `examples`, `language_rule`, `inline_events` `{syntax, event, where}`, `directed_cfg_scale`/`directed_cfg_parameter`, `source_url`, `note`), read at discovery (`providers/discovery._hub_controls`, carried as `raw['hub_controls']`) and published unchanged inside the model's control (`catalog.delivery_guidance`). The field is forwarded on both `txt2speech` and `txt2speech/reference` from the one body builder (`adapters/studio_hub.py`); a non-empty instruction on a model without the token is a **422** `DELIVERY_INSTRUCTION_UNSUPPORTED`, one over `DELIVERY_INSTRUCTION_LIMIT` (500) a **422** `DELIVERY_INSTRUCTION_TOO_LONG` — so it is never silently dropped and the caller never hears a plain reading believing it was applied. The Create page draws the box **hidden and reveals it only for a model whose row publishes the control**, clears it when the model changes to one that does not, and prints the Hub's own guidance (`tests/test_create_voice_picker.js`); nothing is shown for a field the Hub did not send. Tests: **16** in `tests/test_delivery_instruction.py` (a Breeze row `instruct` + directed cfg 4.0, a VoxCPM2 row `text_prefix` with inline events and no cfg, a Qwen3 row with no token — no control and the 422 at the door — and the discovery wiring across the three Hub routes). The Hub side is `studio-hub-kh` `feat/delivery-instruction-20261029` @ `39df613`. **Verified in this release worktree** (gate, all JS suites, the DMG and the packaged smoke are in the release record above), so the Create box is now bundled and mounted-verified, not just exercised in the VM. **Still open:** no push, no install.
> **0.2.208 — two live dispatch stalls (background gate, bound site, pool wait), liveness, and three upload-pool follow-ups — 2026-10-29. MERGED INTO `release/0.2.208` WITH THE DELIVERY-INSTRUCTION WORK; NOT PUSHED, NOT INSTALLED.** Branch `fix/dispatch-stalls-20261029` in `/build/Developer/_worktrees/gateway-kh/dispatch-stalls-20261029`, cut from `116abc8` and rebased onto the 0.2.207 head `071656d`; `VERSION` 0.2.208, notes in `menubar/whats-new.md`. Both stalls were diagnosed read-only on the live Gateway on Mac 1301, which was never contacted, changed or restarted. **1 — the background gate was per site, not per shape** (`worker._background_room`, `worker.py:1972`): it answered 0 for a whole site while *any* normal-priority waiting shape had an idle Mac there. Seven normal Breeze-TTS jobs sat bound to Terranash-1600 (often unreachable, and its `model_supply` does not publish Breeze) while every image site also lists Breeze, so all six image sites refused **132 background image jobs for 3.5 h** with their image Macs idle. The reservation is now the lane the background job competes in (`_background_room(..., modality)`); `tests/test_background_class.py`'s cross-lane test replaces the site-wide one, and its same-lane twin still holds. **2 — a job could stay bound to a place that cannot run it.** `worker._job_room` (`worker.py:1905`) now asks the location's own published supply (`fleet.publishes_model`, `fleet.py:4208`, three answers: yes / no / **unstated is unknown**) and whether it is reachable; either refusal is 0 room, which is what lets `_rebind` move the job — previously the operation budget counted slots rather than Macs with the model, so the site answered "yes" and the job replayed "hub submit failed: timed out" for 6–29 attempts. A pool job is exempt from the publication question (its `model_id` is one member while `fleet.pool_candidates` may answer through another). `worker._site_facts` (`worker.py:1831`) is one snapshot read giving supply + reachability + the row, so nothing new is read under `_budget_lock`. **3 — a pool member wait had no deadline and no reason** (`adapters/pool._acquire_member`, `pool.py:81`): it publishes `waiting: "waiting for a <member> slot"`, expires retryably after `pool.member_wait_seconds` (15 min, code `POOL_MEMBER_BUSY`, `upstream_accepted=False`, so `_defer_if_transient` puts the job back in the queue with that sentence), and a busy member advances the walk instead of ending it (`_acquire_member` moved inside the member `try`; `POOL_WAIT_CANCELLED` still stops the job). Cloud chat members get `pool.chat_member_deadline_seconds` (600 s) through the new `BackendRequest.deadline_seconds`, honoured by `openai_compat.chat_timeout` and the Anthropic lane; a Studio Hub member keeps the Hub's own ceiling. `BackendRequest.cancelled` is checked before each request an adapter sends. Both numbers are editable live in `provider-concurrency.local.json` under `pool` (`concurrency.pool_member_budget`). **4 — generic liveness** (`worker._check_running_liveness` (`worker.py:2266`), on the dispatch thread every 30 s, published as `/health`'s `running_sweep`): a `running` job with no `upstream_job_id` publishes what it is waiting for after `liveness.waiting_after_seconds` (120 s), and past the lane's own call ceiling (`worker.lane_deadline_s` (`worker.py:192`): 4 h chat / 600 s media, plus 120 s grace) it fails retryably `PROVIDER_TIMEOUT` rather than staying silent; a late answer is kept as `late_result_state` and never rewrites the verdict. **5 — three follow-ups from the 0.2.207 review, in `gateway/api.py`:** the upload pool's **slot** still frees when the body is complete (Story's 503 fix stays), but the **byte reservation** is now held until the route has finished (`_declared`'s `finally`, and `_bounded_stream`), because freeing both let five back-to-back 100 MB transcriptions be resident at once; `api.py:528`'s docstring said the bytes were held "for the duration" while the code released them early (it is true now, and states the two moments); and a 503 names **the limit that tripped** — `_spool_take` returns `"bytes"`/`"host"`, `_busy(limit)` quotes only that one and adds a `limit` field beside the code. **Tests:** `tests/test_dispatch_stalls_20261029.py` is new (26) — including the live rows (stranded normal TTS plus accepted background image rows settle in one pass with **no cancel, delete or state rewrite**) and the TTS status route reading `failed` for a row that failed on repeated submit timeouts; `test_background_class` 19, `test_body_ceiling` 42. **The gate was run on `aa44ad7`**: **167 files run, 167 passed, 0 failed, 2 skipped | 2497 tests in 166 files | `All 586 end-to-end checks passed` | 391.9 s wall**. Evidence `/build/Developer/_orchestra/gateway-dispatch-stalls-20261029/gate-of-record/`, console `gate-of-record-console.log`; two earlier green runs on the same branch (`d99dbfd` 2494 tests / 415.9 s, `f599606` 2495 / 397.4 s) are in `gate-20261029T1/` and `gate-final-20261029/`. **Every JS suite was run separately and all pass** (`js/` there): `models_browser` 35, `content_shelf_ui` 22, `console_actions` 21, `release_mock_selfcheck` 17, `pools_view` 16, `queue_view` 13, `sfx_view` 13, `create_voice_picker` 11, `concurrency_tabs` 9, `price_ui` 8, `fleet_connection_view` 7, `admin_guests` 5, `admin_accessibility` 4, `create_empty_video` 2 — **183 tests, every file exit 0**. Not run: `verify_story_content_delivery.mjs` (drives Story's own client), `check_admin_keyboard.mjs` and `check_native.py` (left to their own commands). **The Story side of the TTS report is not a Gateway defect:** `GET /v1/jobs/{id}` and `/v1/jobs/by-idempotency-key/{key}` both publish `state: failed` with the code and sentence for a job failed after repeated submit timeouts (pinned in the new test), so seven tasks still shown as `accepted` in Story are a Story-side reconciliation gap. **A trap for the next reader:** `_background_room`'s lane argument is the whole point — a caller that omits it gets the old site-wide rule, and the 132-job freeze is exactly what that rule does; and `_spool_take`'s refusal is a **reason**, not a boolean, because a 503 naming the wrong limit sends an operator to a number that was fine. **Still open:** no merge to `main`, no push, no install; the DMG, integration check and packaged smoke for the merged release are in the 0.2.208 release record above; the four dead magpie pool members on 1301 are untouched (the owner's call).
> **Delivery instruction — matched to the Hub's contract — 2026-10-29. MERGED INTO `release/0.2.208`; NOT PUSHED, NOT INSTALLED.** Branch `feat/delivery-instruction-gateway-20261029`, rebased onto the 0.2.207 head `071656d`; head **`a5c87ac`** (gated source `7c6cfc4`, then a test-only harness fix in a file the gate does not run — the gate was re-run on `a5c87ac` all the same). The Hub side is `studio-hub-kh` `feat/delivery-instruction-20261029` @ `39df613`, and its contract is what this branch now implements: capability `delivery-instruction` in the catalogue row's `capabilities` **or** the snapshot's `controls.capabilities` (either is enough; nothing inferred from a model id or family), guidance at `controls.delivery_instruction` of `/api/hub/capabilities` (`kind`, `engine_form` — `instruct` for Breeze TTS 2, `text_prefix` for VoxCPM2 — `max_characters`, ≤3 `examples`, `language_rule`, `inline_events` `{syntax, event, where}`, `directed_cfg_scale`/`directed_cfg_parameter`, `source_url`, `note`), read **live at discovery** (`providers/discovery._hub_controls`, carried as `raw["hub_controls"]`) and published unchanged inside the model's control (`catalog.delivery_guidance`). The field is forwarded on both `txt2speech` and `txt2speech/reference` from the one body builder; a non-empty instruction on a model without the token stays a 422. The Create page prints the Hub's fields and nothing for a field the Hub did not send. Tests: **16** in `tests/test_delivery_instruction.py` — a Breeze row (`instruct`, directed cfg 4.0), a VoxCPM2 row (`text_prefix`, inline events, no cfg), a Qwen3 row with no token (no control, and the 422 at the door), and the discovery wiring end to end across the three Hub routes. **The gate on `a5c87ac`**: **167 files run, 167 passed, 0 failed, 2 skipped | 2485 tests in 166 files | `All 586 end-to-end checks passed` | 409.1 s wall** (evidence `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-delivery2-20261029T070633/`); it ran **alongside** the `fix/dispatch-stalls-20261029` helper's own gate with no flake in either. Every JS suite: **167 tests, all exit 0**. **Still open:** no push, no install; the DMG and packaged smoke for the merged release are in the 0.2.208 release record above, so the delivery box is proven in the packaged bundle as well as the VM.

> **0.2.207 sixth review — five items — 2026-10-29.** The four security fixes and every legitimate client were verified; five regressions of the *page and the pool* fixed (probes in `/build/Developer/_orchestra/gateway-guest-review-20261029e/`). **1 (HIGH)** the Create page bound `onchange=createModelChanged` only when the lane had a voice field (`admin_ui.html:3800`), so the Subtitle model select had no handler and its upload-limit note kept the first model's number; every lane is bound, tested through the real `renderCreateForm` and the rendered select. **2 (HIGH)** `CreateSection.swift`'s `modelChanged` never recomputed the file verdict, so Generate stayed disabled (or enabled) on the limit of the model chosen when the file was picked, and a restored draft was judged before any model had loaded; the verdict is re-made on a model change (`:743`), once the models arrive (`:725`), and again at generate time (`:1081`), which is what shows the limit's own sentence. **3 (MEDIUM)** a slot and its reservation were released only after the response was on the wire, so back-to-back batches of eight met 503 in most rounds — and Story's image upload does not retry, so that fails a scene. Both paths now release when the body has been fully received or rejected (`api.py:532` `release()`, called from the declared path's byte counter at `:554` and before the chunked path replays at `:622`), keeping the `finally` as the cover for a body never read. Proven at the middleware boundary (the pool is empty while a slow route is still answering) and over HTTP (ten rounds of eight in parallel, and two 90 MB declared uploads back to back, none refused). **4 (LOW)** the admin transcription route compared the whole multipart `Content-Length` with the *file* limit, so a file of exactly the published size was refused for its envelope; `UPLOAD_ENVELOPE_BYTES` (`api.py:142`, used at `admin.py:2088`) is allowed and the file's own size decides. **5 (LOW)** `readUpload` read `maxUploadBytes` inside its background block — AppKit state off the main thread; the limit is captured before dispatch (`CreateSection.swift:968`). The two menubar findings are checked against the source in `tests/test_create_upload_limit_source.py` (calls in the functions that must make them, and no hardcoded 25 MiB) because this lane does not build the app — a weaker check than pressing the button, and stated as such in the file. **The gate was re-run on `56f4b5b`**: **166 files run, 166 passed, 0 failed, 2 skipped | 2468 tests in 165 files | `All 586 end-to-end checks passed` | 398.7 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-r6-20261029T063711/`; console `gate-console-r6.log`. Every JS suite run separately: **166 tests, all exit 0**. **Note for the helper on `fix/dispatch-stalls-20261029`:** that branch was cut from `116abc8`; the final head here is `56f4b5b`, so it needs a rebase onto that. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built; delivery/emotion remains alone on `feat/delivery-instruction-gateway-20261029`.

> **0.2.207 fifth review — four items — 2026-10-29.** Credentials-before-the-body, the ceilings, guests, login and every legitimate client were confirmed to pass; four more fixed (probes in `/build/Developer/_orchestra/gateway-guest-review-20261029d/`). **1 (HIGH)** a declared-length upload had no idle watchdog, so `Content-Length: 100 MiB` with a hundred bytes sent held its slot and its reservation for ever and any key holder or guest could block Story's uploads; `_BodyWatch` (`api.py:357`) now wraps `receive` on **both** paths — silence longer than 30 s **or** fewer than `_SPOOL_MIN_PROGRESS_BYTES` (64 KiB, `api.py:209`) inside a window is a stall, answered 408 `UPLOAD_STALLED`. The declared path flags it and lets the middleware answer (`api.py:503`), because the raise happens inside FastAPI's parser, which would otherwise turn it into a 400. **2 (MED-HIGH)** `_spool_take` (`api.py:242`) re-tested the in-flight count `_spool_slot` had just raised, so the **eighth** concurrent upload was always refused — Story sends references with `Promise.all` and does not retry; eight slots are eight now, pinned by a unit test and eight real 1 MiB uploads in parallel. **3 (MEDIUM)** the pre-body check trusted any loopback peer, but `require_admin` trusts loopback only while the gateway is **bound** to loopback: on a fleet bind a credential-less loopback POST had its body read before the 401. `_upload_credentials_ok` (`api.py:307`, `is_loopback()` at `:326`) now uses that exact rule, and `_cross_origin` (`api.py:288`) refuses any upload whose `Origin` is not this Gateway's own (403 `CROSS_SITE_UPLOAD`), even with a key. **4 (LOW)** `CreateSection.swift` hardcoded 25 MiB; `/admin/api/create/models` publishes `upload_limit_bytes` for every transcription row (`admin.py:1978`) from `upload_limit_bytes()`, the same function the door refuses with, and the app (`CreateSection.swift:294`) and the web page (`admin_ui.html:3818`) show it where the file is chosen; a row that states none shows nothing. **Also fixed, and it was ours:** splitting the deferred delivery feature left `createModelChanged` calling `applyCreateModelControls`, whose definition had moved to that branch — a `ReferenceError` on every model change of the Create page, which no JS suite caught. A test now drives that function. **The gate was re-run on `040ee8c`**: **165 files run, 165 passed, 0 failed, 2 skipped | 2457 tests in 164 files | `All 586 end-to-end checks passed` | 390.1 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-r5-20261029T030011/`; console `gate-console-r5.log`. Every JS suite run separately: **165 tests, all exit 0**. **A trap for the next reader:** the ASGI body middleware must keep the order *ceiling (header) → origin → credentials → read*; moving the credential check ahead of the ceiling or behind the parse re-opens finding 3, and the walk in `tests/test_body_ceiling.py` only proves the table, not the order — the HTTP tests prove the order. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built; delivery/emotion remains alone on `feat/delivery-instruction-gateway-20261029`.

> **0.2.207 fourth review — five items — 2026-10-29.** The 2 MiB default broke real traffic and declared-length uploads were still read before auth; all five fixed, probes in `/build/Developer/_orchestra/gateway-guest-review-20261029c/`. **1 (HIGH regression)** the Content Pack import doors read `request.stream()` themselves, so no `File`/`Form` parameter existed for the walk to find them and a real 4.26 MiB `.sspack` met the 2 MiB default; both are named at `MAX_ARCHIVE` + 2 MiB (`api.py:136`, `:176`), and the walk now also collects every handler that reads `request.stream()`/`request.body()` — a route that reads its own body must be named or be in the documented self-bounded set, asserted against the default. **2 (HIGH regression)** the chat doors refused images: `/v1/chat/completions` and `/v1/chat/responses` now take `VISION_MAX_IMAGE_BYTES` (the 12 MiB limit this gateway publishes) × 2 images × 4/3 for base64 + 2 MiB (`api.py:146`) — **the gateway's own number, stated as ours because no provider publishes one** — and `/v1/vision/analyze` takes its own advertised 12 MiB decoded (`api.py:149`). Tests send a 5 MB image in a chat turn and a 10 MB vision image and assert the *route* answers. **3 (MED-HIGH)** a declared-length body was passed through unauthenticated and unbudgeted, so FastAPI parsed the multipart before `require_auth`: named doors now check **credentials in the middleware** (`api.py:278` — bearer key, valid session cookie, or a direct loopback peer, the trust `require_admin` already extends; proxied requests never count) and answer 401 from the headers, and a declared length is reserved against the same budget as a chunked body (`api.py:418`). **4 (MEDIUM)** the 64 MiB budget was below the 102 MiB largest ceiling, so a legitimate large transcription could 503 for ever: the budget is now the largest ceiling + 16 MiB (`api.py:198`). **5 (LOW)** a per-host share (`api.py:202`) and a 30 s idle-read timeout (`api.py:204`, answered 408 `UPLOAD_STALLED`) stop dribbling uploads from holding slots. Two refinements worth keeping: the default ceiling is 4 MiB because the largest self-bounded JSON reader (`admin._json_body`) accepts 4 MiB, and **only upload doors are budgeted** (`api.py:229` `upload_door`) — a picture in a chat turn is parsed into memory and dropped, so budgeting it would have made a batch of image chats compete with eight parallel uploads. **The gate was re-run on `5d29dc7`**: **165 files run, 165 passed, 0 failed, 2 skipped | 2447 tests in 164 files | `All 586 end-to-end checks passed` | 814.2 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-r4c-20261029T021003/`; console `gate-console-r4c.log`. Every JS suite run separately: **163 tests, all exit 0**. **One pre-existing failure surfaced and was fixed at its cause:** `test_fit_aware_scheduling`'s queue-view test failed on `ba140db` as well (measured in a worktree at that commit) because `queue_view` reads `catalog.all_rows()`, which rebuilds the catalogue under the test's bare fake provider definitions; the catalogue is now stubbed in that test. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built; delivery/emotion remains alone on `feat/delivery-instruction-gateway-20261029`.

> **0.2.207 third review — four items fixed — 2026-10-29.** Login, the two named upload doors and the main confusables were confirmed fixed; the verdict was still NOT SAFE on four more (probes in `/build/Developer/_orchestra/gateway-guest-review-20261029b/`), all fixed here with tests that fail on the pre-fix code and are driven over HTTP where transport is involved. **A (HIGH)** the body guard matched **two paths by hand**, and three more multipart doors — `/v1/images/source-images`, `/v1/videos/source-images` and `/admin/api/fleet/controllers/{id}/machines/{id}/transcribe` — spooled 120 MB unauthenticated before answering 401. `BoundedUploadBody` now bounds **every request that carries a body** (`api.py:172` `body_ceiling_for`): an explicit ceiling per door that accepts a file (`api.py:145`), a 2 MiB default for every other body (`api.py:156`), and the private render-blob door exempt on purpose because it streams a whole video against its own declared size (`api.py:160`). `tests/test_body_ceiling.py` **walks the app's own route table** (`inspect.signature`, `UploadFile`/`File`/`Form`) and fails if a file route is not named — a route added later cannot slip through by being forgotten. **B (MED-LOW)** the guard runs before `_cap_one_client`, so 40 parallel chunked uploads from one host became 40 spool files (320 MB); a process-wide spool budget (64 MB held, 8 uploads at once, `api.py:166`) is reserved **before** the bytes are read and released in a `finally`, and past it the door answers 503 `UPLOAD_BUDGET_BUSY` with `Retry-After`. **C (LOW)** the table missed Armenian `Օ`, Coptic `Ⲟ` and small-capital `ᴡ`; folding is now a **skeleton** (`admin_session.py:394` `_fold_char`): the curated table, then NFKD minus combining marks, then the character's own Unicode name (`COPTIC CAPITAL LETTER O`, `LATIN LETTER SMALL CAPITAL W`), with `OH`/`OMICRON` the only named exceptions — a whole family of scripts rather than three entries. **D (bug)** a Hub answering `done` with **no segments** returned `[]`, which the writer treated as success and wrote over good segments; an empty answer is now a failure (`voice_import.py:1527`), the next Hub is tried, and nothing is written. **The gate was re-run on `5165ed0`**: **165 files run, 165 passed, 0 failed, 2 skipped | 2434 tests in 164 files | `All 586 end-to-end checks passed` | 443.4 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-r3-20261029T012159/`; console `gate-console-r3.log`. Every JS suite run separately: **163 tests, all exit 0**. One expectation moved with A: `tests/test_image_source_assets.py`'s oversize case now sees the door's 413 (`UPLOAD_TOO_LARGE`) rather than the route's own (`PAYLOAD_TOO_LARGE`), because the body is refused before the route runs — the route keeps its check for the boundary. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built; the deferred delivery/emotion feature remains alone on `feat/delivery-instruction-gateway-20261029`.

> **0.2.207 second review — six items fixed — 2026-10-29.** The re-review (`/build/Developer/_orchestra/gateway-guest-review-20261029/`, probes a–j) returned NOT SAFE TO SHIP; all six are fixed on `fix/create-page-20261028`, each with a test that **fails on the pre-fix code** (verified by reverting `gateway/` to the previous commit and re-running the new suite). **1 (MED-HIGH, pre-existing)** `/admin/login` accepted the gateway key and minted an **owner** session, defeating `require_owner`'s rule that a browser may be the owner while a key holder may not — any Story client or anything holding `.env` could sign in and flip owner-only switches; the form now takes the browser password only (403 with a sentence naming Settings when none is set), and bearer-key API access is untouched. `tests/test_browser_signin.py` was rewritten to the new contract. **2 (MED)** the upload bound ran *after* FastAPI had parsed (and spooled) the multipart body, and a declared `Content-Length` of 512 MB with one byte sent hung; the guard is now `BoundedUploadBody`, an **ASGI middleware** on `/admin/api/create/transcription` and `/v1/audio/transcriptions/quotes`, refusing a declared oversize on the header alone and spooling/stopping an undeclared chunked body at the ceiling. **The trap, and it cost a rewrite: FastAPI parses the form *before* it solves dependencies**, so the first attempt — a `Depends` guard — was the defect it was meant to fix. **3** the same door on `/v1/audio/transcriptions/quotes`. **4 (LOW)** reserved guest names are folded first (NFKC + a lookalike table: Cyrillic `О`/`е`, Greek, `0`→`o`), so `Оwner`/`0wner` cannot read as the owner, and two guests cannot differ only by a lookalike. **5** a local voice's `gender` (m/f/n) is accepted by `/api/voices/settings`, stored as the letter, published on the voice row (`story_row.gender`, `""` when unstated — never "neutral") and shown in the Create picker. **6** annotation can use **any Hub in the fleet**: `voice_import.annotation_targets()` lists this Mac's Hub first and then every reachable location with its own Hub token (one without a token is skipped, never handed this Mac's operator token), `_run_annotation` tries them in order and says which one transcribed, `start_annotation` refuses only when there is nowhere to ask — tonight's case was 1301's own transcription Macs all paused (`NO_AVAILABLE_MACHINE`) while the fleet had workers free. A failed forced re-annotation now **writes nothing** over existing segments, and a test pins that guarantee rather than the accident that used to provide it. **The gate was re-run on `0b5348d`** with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded pool, 3 workers): **164 files run, 164 passed, 0 failed, 2 skipped | 2419 tests in 163 files | `All 586 end-to-end checks passed` | 418.1 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-r2c-20261029T005503/`; console `gate-console-r2c.log`. Every JS suite run separately: **163 tests, all exit 0** (13 files). **Deferred by the owner and NOT in 0.2.207:** the TTS delivery/emotion setting (`delivery_instruction`) was written and then moved off this branch to **`feat/delivery-instruction-gateway-20261029`** at `e7eaeea` (published per-model from the Hub's `delivery-instruction` capability, the Hub's guidance forwarded unchanged, refused with a 422 where unsupported, and a Create-page box). **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built.

> **0.2.207 guest security review: seven findings fixed — 2026-10-29.** An independent review (`/build/Developer/_orchestra/gateway-guest-review-20261028/`, probes 1–5) found seven verified problems in the guest work; all are fixed on the same branch, each with a regression test that **fails on the pre-fix code** (verified by reverting `gateway/` to the previous commit and re-running the new suite: 19 of its 22 tests go red, and the three that stay green are the controls that must keep working). **1 (HIGH)** a client-supplied `_`-prefixed param reached the adapters, which read that path off disk and sent it to a provider — `{"_reference_path": "~/.ssh/id_rsa"}` was enough; the admin create routes now refuse every `_` key from a client (`422 INTERNAL_PARAM`, owner and guest alike) and `adapters/base.staged_path` confines `_reference_path`, `_reference_paths`, `_end_reference_path` and `_upload_path` to `ARTIFACT_DIR` with `is_relative_to`, with `openai_compat` and `studio_hub`'s direct readers routed through it. **2 (HIGH)** an idempotency key was one flat namespace, so a guest replaying the owner's key was handed the owner's job (and its output text) and a squatted key could sit on a later owner/Story submission; keys are now scoped per requester (`guest:<id>:<key>` — owner and key keep the raw form, so a retry in flight across the upgrade still finds its job) and a reply is only ever the requester's own. **3 (MEDIUM)** an empty password skipped the scrypt, so a real guest name answered in microseconds and a fake one in ~100 ms — enumeration; `_matches` now hashes every path. **4 (MEDIUM)** `await file.read()` held the whole upload before the size limit was read; the declared Content-Length is checked first and an undeclared body is streamed and stopped at the limit, 413 either way. **5 (LOW)** a guest's job list carried the owner's gateway-wide `stats` and `bulk` counts — both are omitted for a guest, not zeroed. **6 (LOW)** a guest named "Owner"/"This Mac"/"Story Studio" read as the owner in the very column that exists to tell them apart — those names are reserved, and every guest's label ends in " (guest)". **7 (LOW)** the Guests page put the guest's name inside `onclick` attributes, where `esc()`'s `&#39;` becomes a real quote and killed every button on that row — actions take the id and the name comes from state. **The gate was re-run on `367c440`** with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded pool, 3 workers): **163 files run, 163 passed, 0 failed, 2 skipped | 2397 tests in 162 files | `All 586 end-to-end checks passed` | 502.7 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-final-20261029T000020/`; console `gate-console-review2.log`. Every JS suite run separately: **162 tests, all exit 0** (13 files; `test_admin_guests` now 5, including the apostrophe case). **Four fixtures were staging files outside `ARTIFACT_DIR`** (`test_adapter_checkpoints`, `test_video_capabilities`, `test_studio_hub_contract` twice) — a shape the gateway never produces, updated to stage where it does; **one earlier gate run on this branch was red for `test_task6_packaging`'s `hdiutil create ... Resource busy` while other work ran beside it**, and it passed alone and in the clean re-run. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` not run; the app was not built.

> **0.2.207 source prepared — 2026-10-28. NOT INSTALLED, NOT PUSHED, NOT MERGED, NO DMG.** Branch `fix/create-page-20261028` in `/build/Developer/_worktrees/gateway-kh/create-page-20261028`, cut from `9654165` (the gated HEAD of `release/0.2.206`, not yet landed). Commits: `eb0dc97` (the Create page), `5d3fd63` (guests), `5938213` (the eye on the guest password), `7c3249a` (the two JS suites the gate does not run), `5307040` (the seven review findings), `367c440` (their fixtures). The gate on **`7c3249a`** — the state before the review fixes — was with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded pool, 3 workers): **162 files run, 162 passed, 0 failed, 2 skipped | 2375 unittest tests in 161 files | `All 586 end-to-end checks passed` | 417.1 s wall**. Evidence: `/build/Developer/_orchestra/gateway-create-page-20261028/gate/gate-20261028T232553/` (`summary.json` + 162 per-file logs); console `/build/Developer/_orchestra/gateway-create-page-20261028/gate-console-final2.log`. **Every JS suite the gate does not run was run separately and all pass** (`/build/Developer/_orchestra/gateway-create-page-20261028/js/`, per-file logs): `test_models_browser` 35, `test_content_shelf_ui` 22, `test_console_actions` 21, `test_pools_view` 16, `test_queue_view` 13, `test_sfx_view` 13, `test_concurrency_tabs` 9, `test_price_ui` 8, `test_create_voice_picker` 7 (new), `test_fleet_connection_view` 7, `test_admin_guests` 4 (new), `test_admin_accessibility` 4, `test_create_empty_video` 2 — 161 tests, every file exit 0. **Two of them were red at `5d3fd63` and are fixed in `7c3249a`:** `test_price_ui` ran `setView` in a VM with no `GUEST` constant (ReferenceError — guarded with `typeof`), and `test_pools_view`'s `SOON` was `Date.now() + 3600`, which crosses midnight and makes `poolBackAt` print a weekday, so it passed all day and failed after 23:00 (pinned to a fixed time today). **A first gate run on `5d3fd63` was red for two reasons, both worth knowing:** (a) `test_secret_field` counts `type="password"` inputs against `onclick="toggleSecret(this)"` and the new guest password field had no eye — a real defect, fixed in `5938213`; (b) `test_render_service` and `test_task6_packaging` failed on a missing `data/final-render/profile.json`, the documented worktree-prep step (copy the three descriptors from `/build/Developer/gateway-kh/repository/data/final-render/`), not a code defect — both pass after the copy. **What 0.2.207 carries:** the Create page's Voice field now shows the published name and provider (`display_name`, not the id the page was reading as `x.name` through a `<datalist>` that shows its value), lists only the voices `voices_for_model(model_id)` says that model can use and re-asks on every model change, says "Unnamed voice · ba75c0" when nothing is published, and still takes a typed id; every Create result (audio, subtitle, image, music, video, chat) and every Recent row downloads through the route's `?download=1` (`Content-Disposition: attachment` + the file's own suffix, so the saved name has an extension). Guests: a named account with an scrypt password beside the owner's, the same cookie/session/backoff; `require_admin` refuses a guest before anything else so **every route that does not explicitly opt in is owner-only**, with the `/admin` middleware carrying the same allow-list as a backstop; status/cancel/downloads/pins are scoped to the job's stored `requested_by`, answering a byte-identical 404 for another guest's job; free and local models only by default with a per-guest paid switch that is off until the owner turns it on (enforced again on submit); every job records who asked (owner / key / guest), `null` → "not recorded" for older jobs. **Learned here, and easy to rediscover the hard way:** FastAPI 0.141 keeps a mounted router inside one `_IncludedRouter` in `app.routes`, so a route walk over the top level finds **zero** admin routes — `tests/test_admin_guests.py` expands `original_router`, which is the difference between a test that proves the refusal and one that passes having checked nothing. **Still open:** no merge to `main`, no push, no DMG, no install; `scripts/integration_check.py` was not run; the app was not built, so `menubar/JobsWindow.swift`'s "Requested by" line is unbuilt source (compiles by inspection only); the browser console has no Settings page (settings live in the native app), so the "admin settings pages" audit covered the six console pages and the Create tabs, not the app's Settings page.

> **0.2.206 gate and build verified — 2026-10-28. NOT INSTALLED, NOT PUSHED, NOT MERGED.** Release gate on `40c8cc9` (tree `7e42a9ff`) in `/build/Developer/_worktrees/gateway-kh/release-0.2.206` with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded pool, 3 workers): **160 files run, 160 passed, 0 failed, 2 skipped | 2342 unittest tests in 159 files | `All 586 end-to-end checks passed` | 422.2 s wall, exit 0**. The two skips are the named ones — `test_ui.py` (drives the installed app and takes over the owner's windows) and `test_fleet_standalone.py` (needs `GATEWAY_ALLOW_LIVE_GENERATION=1` and the real Hub). No file needed a rerun and none was retried: the first full run was green. `summary.json` names no failing row, no non-zero child exit and no `Ran 0 tests`; the one row with no test count is `test_e2e` (script-style, 586 checks). The gated source is `40c8cc9`; this HANDOFF entry rides a docs-only commit above it. Evidence: `/build/Developer/_orchestra/gateway-0206-release-20261028/gate-20261028T190500Z/` (`summary.json` and 160 per-file logs); the console is `/build/Developer/_orchestra/gateway-0206-release-20261028/0206-gate-console.log`, kept outside the evidence dir because the runner refuses to reuse a non-empty one. **The +29 tests over 0.2.205's 2313 are this release's own:** `tests/test_render_ranking.py` is new (26 tests, 0.5 s) and `tests/test_function_priority.py` went 25 → 28. **The JS suites the gate does not run were run separately and all pass** (`/build/Developer/_orchestra/gateway-0206-release-20261028/js/`, per-file logs): `test_models_browser` 35, `test_content_shelf_ui` 22, `test_console_actions` 21, `test_release_mock_selfcheck` 17, `test_pools_view` 16, `test_queue_view` 13, `test_sfx_view` 13, `test_concurrency_tabs` 9, `test_price_ui` 8, `test_fleet_connection_view` 7, `test_admin_accessibility` 4, `test_create_empty_video` 2 — 167 tests, every file exit 0. Not run: `tests/verify_story_content_delivery.mjs` (it drives Story's own content client against an isolated Gateway — a Story-side cross-repo check, outside this gate) and `tests/check_admin_keyboard.mjs` / `tests/check_native.py` (the gate docstring leaves them to their own commands). Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.206**, build **`40c8cc9-202610281352`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.206; `CFBundleIdentifier` `kh.gateway.menubar`), ad-hoc signature valid (`codesign -v --strict`), every bundled `gateway/*.py` byte-equal to the worktree (`diff -r gateway …`, no differences), `whats-new.md` byte-identical, `requirements.txt`/`requirements.lock` byte-identical, and the three `data/final-render` descriptors bundled (profile sha256 `ad3f1c9d47d5…`, identical in the bundle) — producing `/build/Developer/_worktrees/gateway-kh/release-0.2.206/dist/gateway-kh-0.2.206.dmg`, **51,652,613 bytes, SHA-256 `2f876a31cc7127f70b51a1623806307b5a2273513b431497f9536ee873c2b144`**, and `hdiutil verify` reports the image checksum `VALID`. Console: `0206-build-console.log` in the same evidence directory. **Two bundling details worth knowing:** `whats-new.md` lands at `Contents/Resources/whats-new.md`, not under `Resources/gateway/`; and `tests/test_fleet_action_banner_layout.py` keeps its evidence PNGs under `dist/fleet-action-banner-*` unless `FLEET_BANNER_ARTIFACTS` names another directory, so a gate run leaves one such directory beside the DMG (gitignored, left in place). **Worktree prep, both git-excluded:** `.venv` is a symlink to the primary checkout's venv, and `menubar/vendor/` plus `data/final-render/`'s three descriptors were copied in from `/build/Developer/gateway-kh/repository/` — with the descriptors absent `build.sh` stops at "selected render authority source is unavailable", and with `vendor/` absent it fetches the 26 MB runtime. **Nothing on the owner's machine was replaced:** `--no-install` (no `/Applications` write at any point) — `/Applications/gateway-kh.app` is untouched at 0.2.204 build `99f0344-202610270419`, its `Info.plist` mtime still `2026-10-27T11:19:16Z` — and no `gateway-kh` process is running on this Mac after the build (`pgrep` empty), so the live Gateway on Mac 1301 was neither reached nor affected. **What the release carries:** `a14df43`/`3d17efe`/`fdc8dc9` make a final render one entry in every Mac's own work order instead of a rule that jumps the queue, and among the Macs that will take it the Gateway prefers the one whose own Hub measured the fastest render (`machines[].render_speed`, from the Hub's `render_history`), falling back to chip then memory only for a Mac with no measurement — unmeasured stays `None`, never `0`; the page publishes that order from `fleet.render_candidates` itself (`render_optin.rank_view`) rather than recomputing it, so the sheet and the dispatcher cannot disagree. `06d96f1` adds a per-Mac on/off switch for every kind of work (`function_enabled`, absent = on, only a real boolean `false` is off, malformed values dropped rather than guessed), relayed to the Mac's Studio Hub, and honour it for render inside `render_candidates` too. It also adds the owner's per-Mac `render_while_paused` exemption — off by default, relayed through the controller, and it overrules neither the render opt-in, Lite Mode, drain, maintenance, quarantine nor an unreachable/stale location, only the pause itself, for final render only. **Still open:** no install, no push, no merge to `main` (the branch is `main` 0.2.205 + five commits), `scripts/integration_check.py` was not run for this tree, and the packaged headless `/health` smoke 0.2.204/0.2.205 recorded was not repeated here.

> **0.2.205 source prepared — 2026-10-28.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.205` in the release worktree `/build/Developer/_worktrees/gateway-kh/release-0.2.205`, cut from `feat/full-push-timeout-20261027` (`439267d` — the build the live Mac 1301 runs, on top of `release/0.2.204`, which is not on `main`), **not from `main` (`5cde803`)**, so this release carries the full-push timeout that Mac already has. Five finished branches merged in order: `unlock-models-20261027` (`c2092be`), `feat/chat-vision-hub-20261027` (`2cfe207`), `feat/model-picker-20261028` (`f664cf7`), `feat/link-speed-20261028` (`47a0981`, `fbf2e4e`), `test/pool-refused-fallthrough-20261028` (`dedf874`). **One real conflict, in two files**, both from `unlock-models-20261027` (branched at `5cde803`, before 0.2.204): `gateway/providers/models.py` — the two Gemma ids leave `RETIRED_MODEL_IDS` and the owner's reason stays written beside the two LTX entries that remain, while HEAD's stronger "one line here *and* one line in the Hub's `retired_models.py`, keep the two lists equal" wording is kept because it still governs the video pack; `tests/test_retired_video.py` — HEAD (`f63cc5c`, inside 0.2.204) had rewritten the file to read its ids from `RETIRED_MODEL_IDS` rather than write names out, so the branch's version of those hunks was dropped as a superseded shape, and its two still-valid intentions were re-applied on top of HEAD's shape (a text encoder is not withheld by name under any casing or package id; its video-engine Hub row is still withheld by shape, with a reason that does not say "retired"). The other four merged clean. <br>**A fresh worktree does not build as it stands:** `data/final-render/` is git-excluded per checkout, so `profile.json`, `compiler.json` and `tools.json` were copied in from the primary checkout (`/build/Developer/gateway-kh/repository/data/final-render/`), and `.venv` is a symlink to the primary checkout's venv (also git-excluded). With the descriptors present the build is 0.2.204's own invocation, `./menubar/build.sh --no-install` (the default `descriptor-only` bundles all three); `--render-authority tool-only` is the other route — it bundles no authority and leaves the remote-render lane unavailable at runtime — and is what a worktree needs when the descriptors are *not* copied in. Without either, `build.sh` stops at "selected render authority source is unavailable". **`test/pool-refused-fallthrough-20261028` is a fix worth knowing about**: a pool member whose connection was refused (a relay switched off answers before a byte leaves) used to stop the chain because the send had been counted; nothing can have been accepted or charged when nothing was transmitted, so the pool advances to the next member, a paid one included, while a genuinely unknown submission outcome stays a durable recovery hold. `feat/render-ranking-20261028` was **not included** — it had no branch and no worktree on this Mac at any point while this release was prepared, `feat/render-ranking-20261028` querying empty and `_worktrees/gateway-kh/render-ranking` absent.

> **0.2.205 gate, build and packaged smoke verified — 2026-10-28. STILL NOT INSTALLED, NOT PUSHED.** Release gate on `75a6345` (tree `a036ed62`) with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded pool, 3 workers): **159 files run, 159 passed, 0 failed, 2 skipped | 2313 unittest tests in 158 files | `All 586 end-to-end checks passed` | 420.4 s wall, exit 0**. The two skips are the named ones — `test_ui.py` (drives the installed app and takes over the owner's windows) and `test_fleet_standalone.py` (needs `GATEWAY_ALLOW_LIVE_GENERATION=1` and the real Hub). Evidence: `/build/Developer/_trash/gateway-0205-gate-20261028T021340Z/` (`summary.json` and per-file logs; the console is `/build/Developer/_trash/0205-gate-console.log`, kept outside the evidence dir because the runner refuses to reuse a non-empty one). Every JS suite was run separately (the gate does not run them) and all pass: `test_pools_view` 16, `test_models_browser` 35, `test_console_actions` 21, `test_content_shelf_ui` 22, `test_queue_view` 13, `test_sfx_view` 13, `test_concurrency_tabs` 9, `test_price_ui` 8, `test_fleet_connection_view` 7, `test_admin_accessibility` 4, `test_create_empty_video` 2. **Two of them were red at the base commit and are fixed here:** `test_pools_view.js` asserted a POST/PATCH body of `{name, display_name, members}`, but the image-pool work (`a08f242`, before `main`) had added `modality` and `tier_models` — a stale expectation in a suite the Python gate never sees, not a 0.2.205 regression; commit `75a6345` pins both keys. Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.205**, build **`75a6345-202610280222`** (`CFBundleShortVersionString`, `CFBundleVersion`, `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.205), ad-hoc signature valid (`codesign -v --strict`), every bundled `gateway/*.py` byte-equal to the worktree (`diff -r`, no differences) and `whats-new.md` byte-equal, and the three `data/final-render` descriptors bundled (profile sha256 `ad3f1c9d47d5…`, identical in the bundle) — producing `/build/Developer/_worktrees/gateway-kh/release-0.2.205/dist/gateway-kh-0.2.205.dmg`, 53,004,559 bytes, SHA-256 `16f0fd1c05fba6cbc47a1a5461dc4a8d91906b092d9ae088dd00f0dcea8b9bf1`. A packaged headless smoke (bundled runtime + bundled source, temp profile, port 18787 and never 8787) answered `/health` with `build: 75a6345-202610280222`, 20 providers / 38 discovered models, and exited on TERM with no listener left. **Nothing on the owner's machine was replaced:** `/Applications/gateway-kh.app` is untouched (`--no-install`, no `/Applications` write at any point), and **no Gateway is listening on 8787 on this Mac** — a `curl` to it returns nothing and `lsof` names no listener, so the live build on Mac 1301 was not reachable or affected from here.

> **Gate, build, packaged smoke and the on-demand integration check verified — 2026-10-27. STILL NOT INSTALLED, NOT PUSHED.** Release gate on `99f0344` (tree `a3d28305`) with `.venv/bin/python scripts/run_gate.py --evidence <dir>` (default bounded parallel pool, 3 workers): **155 files run, 155 passed, 0 failed, 2 skipped | 2248 unittest tests in 154 files | `All 586 end-to-end checks passed` | 431.8 s wall, exit 0**. The two skips are the named ones — `test_ui.py` (drives the installed app and takes over the owner's windows) and `test_fleet_standalone.py` (needs `GATEWAY_ALLOW_LIVE_GENERATION=1` and the real Hub). Evidence: `/build/Developer/_trash/gateway-0204-gate-20261027T041041Z/` (per-file logs, `summary.json`, console). The JS suites the gate does not cover were run separately and are green: `tests/test_content_shelf_ui.js` 22/22, `tests/test_console_actions.js` 21/21, `tests/test_admin_accessibility.js` 4/4, `tests/test_sfx_view.js` 13/13. Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`; stamp reused, no network fetch) — version **0.2.204**, build **`99f0344-202610270419`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree; bundled `VERSION` 0.2.204), ad-hoc signature valid, every bundled `gateway/*.py` byte-equal to the worktree and `whats-new.md` byte-equal too, and the three `data/final-render` descriptors bundled (profile sha256 `ad3f1c9d47d5…`) — producing `/build/Developer/_worktrees/gateway-kh/release-0.2.204/dist/gateway-kh-0.2.204.dmg`, 55,334,184 bytes, SHA-256 `db2c55cc04f1f8fcbd449779b06ec8da66abd2acd971fddbe07f2feadaec3405`. A packaged headless smoke (bundled runtime + bundled source, temp profile, port 18787 and never 8787) answered `/health` with `build: 99f0344-202610270419` and exited on TERM with no listener left. **The on-demand integration check passed** on this tree's predecessor (`61ab984`), `scripts/integration_check.py --gateway-root <release worktree>`, exit 0: a throwaway Hub + Gateway on temp paths and a free loopback port, the two model packages APFS-cloned from `~/StudioHubModels` (source untouched), nothing live read or written — image job `succeeded`, 885,877 bytes in 120.6 s; TTS job `succeeded`, 322,620 bytes in 18.1 s; both named machine `local` and routable at idle 1 of 1; `python-multipart` present in both packaged runtimes; render profile fps 24 at canvas 1280x720; blob states `missing → complete` for both audio and base; and the render request refused with the honest reason `RENDER_DESTINATION_UNAVAILABLE`, because a single local Hub has no render node. Its lab moved to `/build/Developer/_trash/integration-check/studiohub-integration-check-20261027-083841-52567`. **The live Gateway was untouched throughout:** PID 75107 unchanged, `/Applications/gateway-kh.app` still 0.2.203 build `5cde803-202610251816`, port 8787 served by it. `scripts/migrate_retired_wars.py` **ships but was not run** — the owner runs it against live data after install.

> **0.2.204 source prepared — 2026-10-27.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.204` off `main` 0.2.203 (`5cde803`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-0.2.204`, with seven finished branches merged in order: `feat/ref-to-image-20261026` (`1b4b284`, fast-forward), `feat/video-lanes-20261026` (`cb23454`, fast-forward), `docs/rules-refresh-20261026` (`f63cc5c`, **one conflict in `gateway/catalog.py`**), `feat/integration-check-20261026` (`4dffd2b`, clean cut), `feat/pack-retire-remove-20261027` (`6729f29`, clean cut), `feat/backup-carries-local-settings-20261027` (`5b4841c`, clean cut), and `feat/release-force-full-20261027` (this branch, from `release/0.2.204`). The conflict was a comment over the same `_video_row` branch: `f63cc5c` had removed the retired model names from it while the video-lanes record lookup was added above it — **both sides' intent kept**, the names stay out and the record lookup stays in. 

> **Reference to Image on FLUX.2 Klein (431f04d).** New `gateway/image_assets.py` is the content-addressed reference store (id = sha256 of the bytes, TTL 900 s, 25 MB, png/jpeg/webp, header-only size reads) behind `POST /v1/images/source-images`, which answers Story's own field names (`id`, `media_type`, `byte_size`, `width`, `height`, `expires_at`) and refuses a body it cannot decode. `create_image` stages every reference and `store._INPUT_PARAM_KEYS` joins `_reference_paths` with list expansion so artifact GC does not sweep a second reference; the same Idempotency-Key with the same digests replays and with different digests is `409 IDEMPOTENCY_KEY_REUSED`; an unknown or expired id is `400 REFERENCE_ASSET_EXPIRED`, never a silent text-to-image render. The adapter sends the references inside `item["params"]["reference_images"]` as `[{b64,mime}]` and fails an item whose returned `reference_count` differs from what was sent. The catalogue suppression is deleted: `min/max/max_dimension` come from the lane contract's own numbers (the `1, 4` and `2048` literals are gone; unstated publishes null), and across locations the capability is the UNION while the limits are the intersection of the sites that advertise it. Routing asks for `require_capability="edit"` through `catalog.resolve` → `fleet.route` → `routing_candidates` (also the pool and re-route paths), and no capable lane answers the existing 503 naming the missing capability. 

> **Three fixes carried in the same branch.** `e313838`: `_next_machine` assigned the room hook after the pool branch had already read it, so the name was local and every pool re-route raised `UnboundLocalError`; the lookup now happens before either branch. `41fe7b1`: every remote final render to a joined fleet Mac failed its first status poll because Studio Hub reports the executing machine by its broker's self-label `"local"` (the only production construction, stamped at `render_jobs.claim_launch` and relayed verbatim); the answering Hub **is** the destination, so `FinalRenderService._hub_job` accepts null, `"local"` and the row's node id, and any other machine name is still foreign and refused. The poll no longer crashes the monitor either: an answer it cannot accept is logged with the Hub's HTTP status and a bounded body excerpt (the status call keeps that body opt-in, so the shapes other callers compare are unchanged), an accepted answer resets the count, and 30 consecutive rejections settle the row through the existing `remote_state="failed"` path Story falls back from locally, while a transport failure stays an unknown outcome and keeps being retried. `6e60e69`: the private final-render envelope accepts an optional `display` object (channel and/or episode, 1–256 characters, NUL-free) and copies it into the Hub request body only when present; it is never part of the inbound body hash, the idempotency identity, the dispatch key or the acceptance decision, and a request without one produces exactly the bytes it produced before. 

> **Video lanes: one capability record per model (809f59f).** `gateway/video_capabilities.py` is the only place a cloud video option is typed — modes, aspect ratios, resolutions, `duration_seconds {min,max,step,default?}`, the audio switch field, the start/end/reference image fields, the reference-image limit, and a `source` (`provider` = the endpoint's own OpenAPI schema, `curated` = the provider's docs on `source_date`) with `source_url`. `catalog._video_row` builds a cloud row from it, so the flat `["16:9","9:16","1:1"]` / 5 s + 10 s @ `"720p"` row every cloud video model published is gone; a model with no record publishes its provider-stated mode and **no variants**, so Story's `selectLiveImageToVideoModel` cannot offer a canvas nobody published. The record is also the request mapping: `adapters/fal.py` names no model and sends each field only where the record declares it, and an unpublished duration, ratio or resolution is refused naming the accepted set, never clamped. First records are **Wan 3.0** (T2V/I2V/R2V, 2–30 s, 480p/720p/1080p, six ratios, `start_image_url`/`end_image_url`/`reference_image_urls`, `audio`) and **Seedance 2.5** (T2V/I2V/R2V, 4–30 s, seven ratios, `image_url`/`end_image_url`/`image_urls`, `generate_audio`, `duration` as a string enum) — both via fal, both **disabled** (nothing enabled, contacted or spent). **The source image now reaches the provider:** `api._stage_video_reference` also stages `end_image_asset_id` and `reference_asset_ids` and refuses an expired asset by name instead of letting the job become a text-to-video request with no picture; fal sends a `data:` URI in the endpoint's declared field, xAI `image: {url}`, MiniMax v2 a `content[]` item with `role: "first_frame"` (its v2 route never read `first_frame_image`), and Kie uploads to its `file-base64-upload` route and uses `downloadUrl` in `image_urls`. `POST /v1/videos/generations` answers a replay of an Idempotency-Key before it looks at uploads and refuses a replay whose request changed. Text-to-video stays accepted for models that publish it (AGENTS.md, owner 2026-10-26, keep the lanes listed); image-to-video without its image, a mode a model does not publish, and reference-to-video with no reference are each a named refusal. `tests/test_video_capabilities.py` is new — 24 decision-logic tests over fake provider responses only. 

> **Retired video names scrubbed (f63cc5c, a8709e0).** `tests/test_retired_video.py` now reads `RETIRED_MODEL_IDS` instead of writing the ids out (every id, upper-case, as a model id and as a package id, under chat and video, plus a lookalike that must not be caught), and the comment prose in `providers/models.py`, `providers/discovery.py`, `catalog.py`, `tests/test_e2e.py` and `docs/studio-hub-wire-contract.md` no longer names a retired model. Kept deliberately: `RETIRED_MODEL_IDS` and its entries (the backstop for a Hub that has not dropped a retired pack), `("runway", "video")` in `_MODALITY_RULES` (a vendor-family rule, not a model), the text-to-video lanes, and the dated logs. The same branch corrects stale paths in AGENTS.md (the installed bundle is `/Applications/Studio Hub.app` and the dev checkout is `/build/Developer/studio-hub-kh`), moves `model-limits.local.json` to `DATA_DIR` with the repo copy named as the legacy fallback, drops the dead `com.genstudiokh.local-api` restore steps and the `_trash/README.md` pointer, gives the local-only reason GitHub Actions stays disabled, and states `test_fleet_standalone.py`'s two preconditions. 

> **A release can be published as the whole image (feat/release-force-full-20261027).** `release_publisher._publish` takes `force_full: bool = False`, and the one plan line is now `planned = "full" if force_full or not (capable and manifest and delta_descriptor) else "delta"`. `admin.release_publish` passes `force_full` from the payload on both paths, `release_rollout` carries it in the rollout state and hands it to each site's push, `catch_up` re-sends a missed release in the same shape (a delta the controllers refused would be refused again), and the release record (`shown`) keeps the plain descriptor rather than a delta one nobody stored. Why: Hub 0.6.201 advertises `delta` and refuses a stage over `MAX_DELTA_FRACTION` 0.5 — the 0.6.201→0.6.203 delta is 713,165,943 B of a 1,413,754,368 B image, 50.44% — and the old app then asks for a disk image a files-only release never had, which 404s. The test double (`tests/mock_controller_release.mjs`) gained the delta routes behind `--delta`: it advertises the capability, reads the digests out of the gzip TSV groups, answers `need`, verifies each blob's hash and serves the release. `tests/test_release_publish.py` proves both halves against it — `force_full` sends `artifact_base64`, the readback carries no `files_sha256` and `/api/hub/release/artifact` serves the image; a normal publish takes the delta path, sends only blobs and reads back `files_sha256`. Both tests fail on the pre-fix code (the force-full one lands on `mode: delta`). Hub side, on a separate branch: `scripts/hub_rollout.py --full` in the studio-hub-kh repo (`feat/rollout-full-20261027`, `ed1fe49`) sets `force_full` in the publish body only when asked for and records it in the receipt; `scripts/test_hub_rollout.py` 33/33. 

> **Local settings in backups (5b4841c).** `gateway/backup.py`'s `sources()` now declares the ten UI-only settings files as archive roles: `providers.local.json`, `pools.json`, `provider-concurrency.local.json`, `model-limits.local.json`, `telegram.json`, `render_optin.json`, `fleet_warnings.json`, `voices_state.json`, `models_cache.json` and `retention.json`. Restoring a backup replaces them safely (moving existing copies to trash) and `target_for` rejects unlisted paths like `gateway.sqlite3`. Tests: `tests/test_backup.py` (18/18).

> **Studio Library packs: retire, un-retire, remove (6729f29).** `gateway/content_delivery.py` gains the three actions and `admin.py`/`admin_ui.html` the controls with confirmation dialogs. **Retire** marks a published pack retired — hidden from the catalogue, new installs blocked, existing installs continue — and retiring the newest version needs an explicit second confirmation. **Un-retire** restores it to active. **Remove** moves the archive and its sidecars into the Gateway's Trash with a manifest entry (what, when, from_where, sha256) and drops it from the active records; a pack that is not retired is refused, so there is no hard delete. `scripts/backup_content_authority.py` treats retired packs as present-and-retired and removed packs as recorded-in-trash, so neither reads as a missing archive. `scripts/migrate_retired_wars.py` records the September 21 Wars archive as removed without moving or rewriting any file — **the owner runs it against live data after install; it is shipped, not run here.** Tests: `tests/test_content_retire_remove.py` (new), `tests/test_content_authority_backup.py`, `tests/test_content_shelf_ui.js`. 

> **Gate and integration check (1b4b284, 4dffd2b).** `scripts/run_gate.py` is now the tracked Gateway release gate — one file per process, same counts as serial, a bounded parallel pool, `Ran 0 tests` is a FAILURE, and `test_ui.py` / `test_fleet_standalone.py` are skipped by name with their reason printed; `scripts/test_run_gate.py` pins its own guarantees. `scripts/integration_check.py` starts a throwaway Studio Hub and Gateway on temporary paths and a free loopback port with generated credentials, drives one image job, one short TTS clip and a final-render request through the real wire, checks machine naming, packaged `python-multipart`, blob-state spellings and the render profile fps, prints a PASS/FAIL table, and moves its lab directory to `_trash/integration-check/` at exit — it never touches the live Gateway, the owner's Hub data, the fleet or the mini. 

> **Release gate, build and integration check: recorded in the entry above after they ran; nothing here is installed, pushed or served.** 
> **Release gate — 2026-10-26 (tracked runner).** `scripts/run_gate.py` is now the Gateway release gate — tracked in the repo, not copied per release — and the owner's **"yes. in parallel"** (2026-10-26) is in force. It replaces `_orchestra/gateway-*/run-gate.sh`, whose 0.2.203 copy ran every file through `python -m unittest` and so counted `test_e2e.py`, `test_ui.py` and `test_fleet_standalone.py` (a `main()`, no TestCase) as passes at `Ran 0 tests`. One file per process (the `_NetworkDisabled` leak requires it), each with a fresh profile the runner makes itself (`GATEWAY_APP_SUPPORT`, `GATEWAY_DATA`, `STUDIOHUB_READY_FILE`, `GATEWAY_NO_MIGRATE=1`; only the two roots are pinned — pinning the derived paths shadowed `test_e2e.py`'s own and made it discover 0 voices). **`Ran 0 tests` is a FAILURE.** Script-style files are named in `SCRIPT_STYLE`; everything else runs under `-m unittest`. `test_ui.py` and `test_fleet_standalone.py` are skipped by name with the reason printed. Files run in parallel (`min(3, cores/2)`; `GATEWAY_GATE_WORKERS` or `--workers N`, `1` = serial) except the serial list: the nine native/offscreen AppKit suites, `test_window_size.py`, and the wall-clock-ceiling suites (`test_catalog_fast.py`, `test_jobs_list_fast.py`, `test_lossless_restart.py`). Per-file logs, exit codes and `summary.json` go to `--evidence`, which it refuses to reuse; any failure exits non-zero. `scripts/test_run_gate.py` pins zero-tests-is-a-failure, the SKIP lines, the evidence refusal and the serial list (5 tests, 1.2 s). **Proof on This Mac, 2026-10-26, alongside the Story 1.4.360 release gate:** serial `580.3 s` and two parallel runs `424.5 s` / `406.7 s`, each **153 files run, 153 passed, 0 failed, 2 skipped, 2211 unittest tests in 152 files, `All 586 end-to-end checks passed`, exit 0** — identical counts in all three. Evidence: `_trash/gateway-gate-20261026/`.

> **Unreleased branch `feat/video-lanes-20261026` — 2026-10-26. Video lanes: the model's own
> capability record, and the picture that now reaches the provider.** New
> `gateway/video_capabilities.py` holds one `VideoCapability` per model and is the only place a
> video option is typed: modes, aspect ratios, resolutions, `duration_seconds {min,max,step,default?}`,
> the audio switch field, start/end/reference-image fields, the reference-image limit, and a
> `source` (`provider` = read from the endpoint's own OpenAPI schema, `curated` = from the
> provider's docs on `source_date`) with `source_url`. `catalog._video_row` builds a cloud row from
> it — the flat `["16:9","9:16","1:1"]` / 5 s+10 s @ `"720p"` row for every cloud video model is
> **gone** — and a model with no record publishes its provider-stated mode and **no variants**, so
> Story's `selectLiveImageToVideoModel` (which needs one) cannot offer a canvas nobody published.
> The record is also the request mapping: `adapters/fal.py` names no model and sends each field
> only where the record declares it; an unpublished duration/ratio/resolution is refused naming
> the accepted set, never clamped. First records: **Wan 3.0** (T2V/I2V/R2V, 2–30 s,
> 480p/720p/1080p, six ratios, `start_image_url`/`end_image_url`/`reference_image_urls`, `audio`)
> and **Seedance 2.5** (T2V/I2V/R2V, 4–30 s, seven ratios, `image_url`/`end_image_url`/
> `image_urls`, `generate_audio`, `duration` as a *string* enum) — both via fal, both **disabled**
> (nothing was enabled, contacted or spent). **Source image actually reaches the provider:**
> `api._stage_video_reference` now also stages `end_image_asset_id` and `reference_asset_ids`, and
> refuses an expired asset by name instead of letting the job become a text-to-video request with
> no picture; fal sends a `data:` URI in the endpoint's declared field, xAI `image: {url}`,
> MiniMax v2 a `content[]` item with `role: "first_frame"` (its v2 route never read
> `first_frame_image`), and Kie uploads to its `file-base64-upload` route and uses
> `downloadUrl` in `image_urls` (its inputs take URLs, not base64). `POST /v1/videos/generations`
> answers a replay of an Idempotency-Key before it looks at uploads, and refuses a replay whose
> request changed. `tests/test_video_capabilities.py` is new — 24 decision-logic tests (the
> published ladders, the four adapters' image fields, the refusals, staging, expired assets, and
> the text-to-video / reference-to-video gates) over fake provider responses only.
> **Adding the next model is one record** —
> the recipe is in `AGENTS.md` ("A video model describes itself"). Story still needs, for T2V and
> reference-to-video: a request body carrying `mode` (`text-to-video` | `image-to-video` |
> `reference-to-video`), `reference_asset_ids[]`, `end_image_asset_id`, and only the options the
> row publishes; and a picker that reads `duration_seconds {min,max,step}` instead of enumerating
> variants.
> **Gate, 2026-10-27:** `scripts/run_gate.py` — **154 files run, 154 passed, 0 failed, 2 skipped,
> 2235 unittest tests in 153 files, `All 586 end-to-end checks passed`**, `395.4 s` wall, exit 0.
> Evidence `_trash/gateway-video-gate-20261026-final/`. A first run had two failures
> (`test_render_service`, `test_task6_packaging`) both `FileNotFoundError` on
> `data/final-render/profile.json`: that directory is git-excluded per checkout, and a worktree
> does not inherit it — copying `profile.json`/`compiler.json`/`tools.json` in made both pass.

> **0.2.203 — 2026-10-26.** The **SFX tab** (Providers page) manages sound sources like providers: Openverse's access token, client id and secret **save and clear** through `POST|DELETE /admin/api/sounds/sources/{id}/credentials`, in the same `CredentialStore` under the key the lane has always read (`openverse-sound`, deliberately unchanged so a stored token keeps working). `CredentialStore.set_values_for` is the one store write that does not resolve a `ProviderDefinition`; it takes the field list from the caller's own declaration and **refuses** an unknown key instead of dropping it. **A sound source is still not a model provider** — no row in `providers/definitions.py`, no catalogue rows, and `test_e2e`'s "every provider has a working adapter" stays green. Sources are declared once in `sound_sources.SOUND_SOURCE_DEFINITIONS` with one class per `kind` in `_ADAPTERS` (checked against each other at import) and projected by `describe_all`, which masks with the same `mask()` the provider console uses; secrets are asserted absent from every response and every captured log line. Anonymous is not unconfigured: `configured` and `registered` are two facts, and the quota line reads "not read yet" until a search has read the upstream's headers. The provider list is **grouped by what it serves**: `definitions.service_group()` files each provider under its first *served* lane in `MODALITIES` order (`serves()`, not the declared list — Kie declares chat and cannot run it, so it is filed under Image), `group_by_service()` orders the headings off the same rows, and `/admin/api/providers` publishes `service_group`, `service_group_label` and `service_groups`. Display only: dispatch, enablement and discovery read nothing new. Tests: `tests/test_sfx_providers.py` (19), `tests/test_sfx_view.js` (13); `tests/test_secret_field.py`'s eye count is now derived from the password inputs instead of pinned at 2.

> **0.2.202 — 2026-10-25.** Render envelope v2 (44505e2): bounds_version 2, canvas_range {max_edge 3840, max_pixels 8294400}, working_fps [24,25,30,60], max_pixel_frames 5374771200000; profile sha256 ad3f1c9d47d5f594…. `data/final-render` is git-excluded: a release must copy the new profile.json/compiler.json/tools.json/voices.json into the installed checkout. Smart Sound library (111de91): `/v1/sounds/{search,library,quota}`, Openverse adapter not a model provider (key read straight from the credential store under `openverse-sound`). Tests: 19 files now `sandbox_env()` before importing gateway (aa7be0a, 36e217e) so no test binds the owner's real Application Support data. Gate 152/152 per file (`PYTHONPATH=.:tests`). Remote render stays off until a real 1080p30 remote-vs-local proof.

> **0.2.201 — 2026-10-25.** Backup: opt-in `include_fleet` (default false) adds `fleet.json` to the archive; restore applies it only when present, otherwise the destination roster is untouched. Credentials (`.env`, `credentials.json`) are backed up exactly as before (owner: unencrypted 0600 archive). Controller move design: `_orchestra/hub-controller-repoint-design-20261025.md`; decision: a move keeps the Gateway row/provider id via `replace_address`, the new Hub may have its own controller_id. Tests: `tests/test_backup.py`.

> **0.2.200 — 2026-10-25.** Jobs window: a **Node** column and an "Executed by" detail line show the Mac that ran each job. `admin.list_jobs` publishes `executed_by` from `result_json.payload.hub_machine` (the path success and failure evidence already use); `None` when the Hub did not report one. Never derived from `provider_id`/`provider_name` (those name the site controller). Test: `tests/test_executed_by_field.py`.

> **0.2.199 — 2026-10-25.** Diagnostics only. `api._note_final_render_refusal` prints one `[final-render] create|lookup refused: <exc> code= status= key=<8 chars> cause=<type>: <≤200 chars>` line for any exception out of `render_service.accept` / `by_key` (stdout reaches `~/Library/Logs/gateway-kh.log`; `logging` warnings do not). Why: after 0.2.198 fixed blob status, WI0005/WI0007 (job 800f7820 / 1c4ec7ac) uploaded their inputs and then the create came back as Story's `send_unknown` with nothing in `render_staging`/`render_journal`/`render_delivery` and no Gateway record of why. Cause still unknown at this release; the next retry names it. Test: `test_final_render_api.py` `test_a_refused_create_names_its_cause_in_the_log_without_the_key`.

> **0.2.198 — 2026-10-25.** Remote final-render blob status: `FinalRenderService._blob_public` mapped only `open` → `uploading`, but the asset store's upload head state is `incomplete` (`render_assets.py`, states `incomplete | published | recovery_required`), so `GET /v1/final-renders/blobs/{sha}/status` on a part-uploaded blob answered `state: "incomplete"` and Story (`lib/final-render-gateway-client.ts:563`, `missing | uploading | complete`) refused it: WI0005 job 800f7820, the first production remote attempt, parked with "Invalid private final render blob response state is invalid". Now `incomplete` (and `open`) → `uploading`. Test: `test_render_assets.py` `test_public_blob_status_speaks_the_story_wire_states` (real store: missing → incomplete→uploading with offset/upload_id → complete with asset_handle).

> **0.2.197 — 2026-10-24.** Optional reference transcript per voice (owner: "our transcriber can't understand khmer"). Catalogue field `include_reference_transcript` (absent = on, so every older voice is unchanged); off = audio only: `adapters/studio_hub._reference_for` stages no transcript and no segments, `voice_import.start_annotation` never transcribes it, `push_fields` shares no words. The Hub needs no change: VoxCPM2 with no words runs reference-only (`generation.py` `_mlx_kwargs_voxcpm_flex`, `ref_text=""`), and with words it runs transcript-aware (`prompt_text`). Add Voices sends `include_reference_transcript` from the panel's **Transcribe the clips** box; More → Language and Transcript… changes it (on needs words: typed, saved, or segments; off keeps the saved text). The reference report adds `language`, `include_reference_transcript` and `transcript_text`. One route sets both: `POST /admin/api/voices/settings {voice_id, language?, include?, transcript?}` (`voice_import.update_voice`; language = code or short name, `LANGUAGE_RE`, ≤40). **Provider voices can be removed** (owner: old ElevenLabs voices gone, keep own cloned/generated + Fish + local): `VoiceRegistry.hide` records them under `voices_state.json` `hidden`, `_merged` drops them from every list and from `resolve`, nothing is deleted at the provider; `POST /admin/api/voices/remove {items, confirm_in_use?}` (409 `VOICE_IN_USE` names recently narrated ones, nothing half-done), `DELETE /admin/api/voices/<provider id>` now hides instead of 404, `GET …/removed` adds `provider_voices`, `POST …/restore {voice_id}` unhides. Test: `tests/test_voice_settings_and_removal.py`. Staging a reference now also sends `display_name` (the voice name) so Hub 0.6.197 job rows can say which voice spoke; older Hubs ignore it. **Language guard** (owner: Khmer channels preset to Breeze babbled and failed section quality 4× each): `gateway/language_guard.py` + `api._refuse_an_unspoken_language`, called in `_submit` for tts and in `/v1/tts/quotes` (quote answers `generation_enabled: false` + `notice`). Refuses 422 `LANGUAGE_UNSUPPORTED` when a stated `language`, or the text's dominant non-Latin script (≥30% of letters: Khmer, Thai, Lao, Myanmar, Hangul, kana, Han, Arabic, Hebrew, Greek, Devanagari, Cyrillic…), is not in the model's published `languages`; an empty list is never refused; Latin text is never judged by script. Test: `tests/test_language_guard.py`. **Remote rendering uses the owner's networks**: `render_optin.workers` rows carry `owner_network` (Fleet → Networks, `model_share.network_of`) and are grouped by it; the page shows "Network C" before any address guess (every Mac is on Tailscale, so the guess called all of them "same network"). Test: `test_render_optin.py` owner-network case.

> **0.2.196 — 2026-10-24.** Image pools (`pools.py` modality `image` + `tier_models`; `image_assignment.py`: per-Mac model = Auto by RAM tier or the owner's pin in `image-model-overrides.json`, synced to each controller's `assigned_image_model` on every fleet refresh; routing (`fleet.pool_candidates`/`route_pool`) trusts only the Hub-reported assignment; pool jobs go to the Hub with `routing: "assigned"`, which needs Hub 0.6.196 — an older Hub shows "Hub too old" and gets no pool work). Remote final-render opt-in per Mac (`render_optin.py`), switch guarded by `require_owner` (app on its Mac or the owner's signed-in browser; never the bare key Story holds). Capacity page: queue wait limit (`concurrency.save_settings` `queue.wait_ceiling_seconds`, 60 s–7 d) and curated model limits (`limits.set_entry`, atomic, unreadable file refused). `os_metadata.py`: Finder/AppleDouble names ignored by every strict folder check (incident 2026-10-24: a `.DS_Store` in `data/render-assets` crash-looped the Gateway after a restart). Tests: every `tests/test_*.py` passes run one file per process (145; a single-process `discover` leaks `_NetworkDisabled` across files and is not the gate), `test_e2e.py` 586/586; `test_fleet_standalone.py` skipped (needs `GATEWAY_ALLOW_LIVE_GENERATION=1`); `test_ui.py` runs after install. Moving the Gateway to another Mac: Settings → Backup carries keys, signing identities, fleet roster, voices and content packs; re-enter pools, capacity, queue wait, model limits, retention and Telegram.

> **0.2.195 source prepared (candidate) — 2026-10-21.** CANDIDATE SOURCE ONLY: NOT QUALIFIED, NOT GATED, NOT BUILT, NOT INSTALLED, NOT PUSHED, NOT RELEASED, NO LIVE PROOF. Branch `feat/fleet-render-finish-20261021` in `/build/Developer/_worktrees/gateway-kh/fleet-render-finish-20261021`, based on `be67c16a`; the version bump and these notes are the metadata for the candidate, and no test, build, network call or live run was made for them. In plain English: **a final render can be made by the fleet, an interrupted render recovers, and an archive can be kept exactly as it is.** Three areas. **Private final-render transport** (`gateway/render_service.py`, `render_assets.py`, `render_contract.py`, `render_hash_v1.py`, with `api.py`, `runtime.py`, `fleet.py`, `store.py`, `worker.py`): a bearer-scoped, non-chargeable lane over registered Hub relays — capabilities, durable chunked blob uploads, create by the caller's `Idempotency-Key`, lookup/status/cancel by key or job id, and output served with `Range`/`If-Match`; the packet is a closed validated format and is never execution authority, and the render identity bytes are shared byte-for-byte with the Hub (`studio.render.*` v1). Destination selection prefers production capacity and only falls back to a qualification candidate when none exists, and each machine advertises the exact encode combinations its pinned compiler can run, derived by compiling a minimal packet per combination; the Gateway publishes only the intersection across eligible machines and says `null` when it cannot tell. **Recovery and cancel** (`render_service.py`, `store.py`): durable journal with restart reconciliation — launch and `uncertain` states are reconciled against the Hub's answer rather than assumed, a job never falsely succeeds, a pre-acceptance cancel settles as never launched, and a cancel stays a request until confirmed and can never later become a success. **Portable archive bank** (`admin_ui.html`, Preserved Archives on the Content Packs page): preserve exact `.sspack` bytes up to 50 MiB each, list them with size/integrity, and download the same file; preserving installs nothing, adds nothing to the shelf and publishes nothing — possession alone is enough to install it elsewhere. **Build authority is an explicit choice:** `menubar/build.sh` selects `descriptor-only` by default and then requires exactly the closed `data/final-render` set (`profile.json`, `tools.json`, `compiler.json`); `--render-authority tool-only` bundles no authority, so the lane stays unavailable at runtime, and `sealed-candidate` stages that same exact set. A partial, extra or digest-inconsistent source stops the build rather than guessing, and no qualification vector is bundled. Tests exist in the candidate (`test_final_render_api.py`, `test_render_assets.py`, `test_render_contract.py`, `test_render_hash_v1.py`, `test_render_journal_g1.py`, `test_render_service.py`, extended `test_content_shelf_ui.js` and `test_task6_packaging.py`) and focused checks passed for this candidate (assets 65, service 57, retention 14, packaging 18, content library 11 — run `bhkd045vk`, 165 total, exit 0); the full offline gate, build, install, push and any live render have **not** run, so nothing here is shipped proof. Open: qualification of the render descriptors and a live fleet render have not happened.
>
> **0.2.194 gated, built, pushed and installed — 2026-10-20.** Strict offline gate on commit `fbe05ce` (tree `992d60cb`): synthetic suite exit 0, `Ran 5 tests` / `OK`; strict runner `--execute` exit 0 with **147 discovered, 145 executed, 145 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 26.23 s, `test_window_size.py` PASS in 79.23 s, `test_content_delivery_integration.py` PASS in 4.96 s, the new `test_fleet_checkin_node.py` PASS, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). Story root: pinned worktree `gate-0191` (`fa53a250`). Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`) — version **0.2.194**, build **`fbe05ce-202610201524`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.194.dmg`, 51,653,687 bytes, SHA-256 `2eccda3bd36739af0e9dcfaeb4085695756956f574871a6a228db7adf3a3f484`, copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0194/dist/`. Packaged headless smoke answered `/health` with that build and exited cleanly on TERM with no listener left. Pushed to `main` (`9464ca1..fbe05ce`). **Installed on This Mac** over 0.2.193 (`239d6e4-202610201216`): the old bundle archived to `gateway-0194/installed-archive/0.2.193-20261020T152702Z/`, new pid 52753 bound on 8787, `/health` build `fbe05ce-202610201524`, worker alive, `dispatch_paused: false`, `hold_sweep` all zero, `consecutive_failures: 0`; 24 Image jobs in flight at the moment of install, 23 immediately after and 19 two minutes later, no incident. `/admin/api/status`, `/alerts`, `/fleet` and `/settings/concurrency` all answered 200 afterwards. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0194/`.
>
> **0.2.194 source prepared — 2026-10-20.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.194` off origin/main 0.2.193 (`9464ca1`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-0194`, with `pull-fleet` (`ffdc7f4`, one commit) merged in — **auto-merging cleanly, no conflict**. In plain English: **a Mac the controller cannot dial is shown as the Mac it is, not as one that never answered.** It is the Gateway half of Studio Hub 0.6.193's fleet pull channel (design note `docs/fleet-pull-channel.md` in the Hub tree; rollout order is Hub first, then the node's app, then this Gateway, so both directions stay safe). Two changes. **`machines_flat` now carries `node_via`** — `"probe"` when the controller dialled the Mac, `"check-in"` when the Mac's own authenticated call home is what the controller heard; `reachable` alone cannot tell those apart, and the three Shops Macs behind one-way Tailscale node sharing can only ever be heard from, never dialled. `None` from a Hub older than 0.6.193 and **never read as `check-in`**. **`_node_post` recognises `202` with `queued: true`** as the controller having taken the change rather than the Mac having applied it: delivery belongs to the controller from that moment, keeping a copy here would send the same Lite Mode change twice, and the answer now carries a plain `detail` saying so and the audit line records `queued`. Nothing softens an ordinary offline Mac. Gateway-side changes: `gateway/fleet.py`, `gateway/startup_control.py`; no app-side change. Tests: `tests/test_fleet_checkin_node.py` (new, 203 lines); `tests/test_lite_mode_offline.py` and `tests/test_fleet_lite_mode.py` unchanged and green. **Gate note:** the strict gate's Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-0191` (`fa53a250`, `node_modules` and `frontend/node_modules` symlinked), as in 0.2.191–0.2.193.
>
> **Offline gate and build verified — 2026-10-20. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.193 was pushed (`origin/main` = `9464ca1`) and installed on This Mac at 12:18Z the same day.** Strict offline gate on commit `239d6e4` (tree `33c18ea5`): synthetic suite exit 0, `Ran 5 tests` / `OK`; strict runner `--execute` exit 0 with **146 discovered, 144 executed, 144 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 26.67 s, `test_window_size.py` PASS in 80.51 s, `test_content_delivery_integration.py` PASS in 5.55 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). Story root: pinned worktree `gate-0191` (`fa53a250`, `node_modules` symlinked). Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime (`43887e81afa60c03`, archive `50424fa4…`) — version **0.2.193**, build **`239d6e4-202610201216`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.193.dmg`, 57,128,624 bytes, SHA-256 `7cf39c4f81d07045b9afb8cff748effa20415a3934b6dd01e40474a532419c5c`, copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0193/dist/`. Packaged headless smoke answered `/health` with that build and exited cleanly on TERM with no listener left. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0193/`.
>
> **0.2.193 source prepared — 2026-10-20.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.193` off origin/main 0.2.192 (`a363626`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-0193`, with `fix/lite-offline` (`6ffb422`, one commit) merged in — **auto-merging cleanly, no conflict**. In plain English: **a Mac the controller has never reached even once is its own issue, instead of being folded into "Lite Mode, off".** A Mac that *was* working and is now off is expected and stays quiet; a Mac that has *never* answered since it joined is a different fault — it may be holding an address its controller cannot route to, or be on a network the controller cannot dial, and it can never correct itself, because the roster address is written at join and only at join. Both states read `offline`, so for a Lite Mode Mac the "expected off" fold hid it for ever: measured 2026-10-20, the Shops office iMac sat at "offline · never connected" for 51 hours while the Hub's own app on its controller already said "never connected since it joined". One fault, one row: the new `machine_never_connected` warning names the machine, states the address its controller kept and how long it has sat on the roster without ever answering, and replaces the generic offline row (and the quiet `lite_mode_long_off` line) rather than joining them; the machine still folds its workers and models in as any offline Mac does. The Hub's own `never_connected` flag is carried through as a bool or nothing, like every other field from another machine. Gateway-side changes: `gateway/fleet.py`; no app-side change. Tests: `tests/test_lite_mode_offline.py` (extended). **Gate note:** the strict gate's Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-0191` (`fa53a250`, `node_modules` and `frontend/node_modules` symlinked), as in 0.2.191/0.2.192.
>
> **Offline gate and build verified — 2026-10-20. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.192 was pushed (`origin/main` = `a363626`) and installed on This Mac at 06:03Z the same day — see the 0.2.193 entry above.** Strict offline gate on commit `b7ae836` (tree `39b3359e`): synthetic suite exit 0, `Ran 5 tests` / `OK`; strict runner `--execute` exit 0 with **146 discovered, 144 executed, 144 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged, all 9 native/offscreen suites PASS, `test_e2e.py` 26.10 s, `test_window_size.py` 79.44 s, `test_fault_provider_answers.py` 14.02 s, `test_content_delivery_integration.py` 4.78 s, every child exit 0 / `process_absent` / no remaining group, pins byte-identical, empty candidate digest. Story root: pinned worktree `gate-0191` (`fa53a250`, `node_modules` symlinked). Build: `./menubar/build.sh --no-install` exit 0 against the vendored runtime — version **0.2.192**, build **`b7ae836-202610200602`** (all four stamps agree), ad-hoc signature valid, 38 bundled `gateway/*.py` and `whats-new.md` byte-equal — and `dist/gateway-kh-0.2.192.dmg`, 57,127,028 bytes, SHA-256 `5d5ed68852be7a1fdccae02f091c21102c796ca25f09949bb38e57778a6d8271`, copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0192/dist/`. Packaged headless smoke answered `/health` with that build and exited cleanly. The installed app was still 0.2.191 (`7bb2e73-202610200310`) at this point. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0192/`.
>
> **0.2.192 source prepared — 2026-10-20.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.192` off origin/main 0.2.191 (`5c20111`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-192`, with `fix/pool-json-answer-check` (`7e7b986`, one commit) merged in — **auto-merging cleanly, no conflict**. In plain English: **an answer that was asked to be JSON, and is not, counts as unusable.** A chat request with `response_format` `json_object`/`json_schema` now fails a member whose text is not valid JSON (settled, retryable; a cut-off is told from other breakage and stated as a plain reason such as "cut off at N characters"; the claimed `finish_reason` is kept as attempt evidence), so a pool moves on to its next member instead of returning the broken text as success, and a single model fails with that reason. One shared check in `adapters/base.py`, called by the OpenAI-compatible, Studio Hub and Anthropic chat lanes; non-JSON requests cost a dict lookup; the text is never repaired. It extends the 0.2.191 rule (a provider's completed-but-unusable answer is a settled failure, never a hold). Gateway-side changes: `gateway/adapters/base.py`, `anthropic.py`, `openai_compat.py`, `studio_hub.py`; no app-side change. Tests: `tests/test_fault_provider_answers.py` (extended), `tests/test_anthropic_structured.py`, `tests/mock_upstream.py`. **Gate note:** Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-0191` (`fa53a250`, `node_modules` and `frontend/node_modules` symlinked), as in 0.2.191.
>
> **Offline gate and build verified — 2026-10-20. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.191 was pushed (`origin/main` = `5c20111`) and installed on This Mac at 03:13Z the same day — see the 0.2.192 entry above.** Strict offline gate on commit `7bb2e73` (tree `ec713afe`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **146 discovered, 144 executed, 144 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 28.56 s, `test_window_size.py` PASS in 112.87 s, the fault-injection suite `tests/test_fault_provider_answers.py` PASS in 8.06 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 146 rather than 0.2.190's 145 because this change adds `tests/test_fault_provider_answers.py`. **The Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-0191` (detached at `fa53a250`, tree `efd6da00`, clean, with `node_modules` and `frontend/node_modules` symlinked to the main checkout's)** — the Story main checkout is in a release gate of its own and must not be used as the gate root; `tests/test_content_delivery_integration.py` runs Story code with `tsx` and passes in 9.12 s against the pin. Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-191> --story-root /build/Developer/_worktrees/story-studio-kh/gate-0191 --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`. **The merge was clean — no conflict to resolve**, and the branch's own suite plus every adapter suite it touches (`test_anthropic_structured.py`, `test_chat_max_tokens.py`, `test_chat_deadline_forwarding.py`, `test_studio_hub_contract.py`, `test_hub_lane_contract.py`, `test_job_recovery.py`) were run green on the merged tree before the version bump. Build: `./menubar/build.sh --no-install` in `release-191` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.191**, build **`7bb2e73-202610200310`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.191.dmg`, 54,781,877 bytes, SHA-256 `3d2ae362bedaf8719e3a6be312c368f32738ed3a8f2105a1e508a3eb522938df`, **copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0191/dist/` (with its `.sha256`)**. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0191/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.190** build `9b59ba6-202610191555`, PIDs 25326/25339 unchanged, port 8787 still served, worker alive. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0191/`.
>
> **0.2.191 source prepared — 2026-10-20.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.191` off origin/main 0.2.190 (`c43274f`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-191`, with `fix/fault-injection` (`1779ac5`, one commit) merged in — **auto-merging cleanly, no conflict**. In plain English: **an unusable answer is a failure with a reason, not a mystery.** The provider *answered* — its bytes are on the wire — so nothing is outstanding upstream, and every shape a bad answer can take must settle as a *known* failure carrying the reason, with a Retry able to get a fresh answer; it must never become an `unknown_outcome` hold, because that verdict means "we cannot tell whether work is still running" and a hold with no attempt to aim at has no settle path at all. Measured through the real adapters and the real worker before this change: (1) a chat lane answered **200 with a CDN's HTML error page** — `response.json()` raised, the escaping `ValueError` reached the worker's generic handler *after* a request had been sent, and the job parked `failed / unknown_outcome`, `retryable: False`, "Upstream outcome is unknown; reconcile the provider request before retrying" — an unresolvable hold for a request that provably finished; (2) **the Hub's own empty chat answer** did the same (`upstream_accepted=True` with no `upstream_settled`); (3) **an OpenAI-compatible empty answer succeeded with blank text** — a success with no output, which nothing downstream can tell from a real one. One rule in one place (`base.answer_json` / `base.unusable_answer`): a refusal raised while reading the provider's own completed 200 answer is settled — `upstream_accepted=True`, `upstream_settled=True`, `retryable=True` — applied at every site that reads that answer on the three chat lanes and the image lane (a body that is not JSON, no choices, an empty chat answer, no image, neither `b64_json` nor `url`). The empty-answer rule is the one the Hub lane already stated ("an empty answer is a failure here too rather than a successful blank job"), now shared instead of spelled once. Gateway-side changes: `gateway/adapters/base.py`, `anthropic.py`, `openai_compat.py`, `studio_hub.py`; no app-side change. Tests: `tests/test_fault_provider_answers.py` (new) drives all four shapes through the real adapters against a loopback provider, asserts each ends a settled retryable failure (never a hold, never a blank success), that a Retry against a working provider succeeds, and that three machines answering garbage end the job terminal after exactly three attempts — bounded, never a loop. **Gate note:** the strict gate's Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-0191` (detached at `fa53a250`, clean, `node_modules` and `frontend/node_modules` symlinked to the main checkout's) because `tests/test_content_delivery_integration.py` runs Story code with `tsx`; the Story main checkout is in a release gate of its own, so it must not be used as the gate root (see `gateway-0190/GATE-STORY-ROOT-NOTE.md`).
>
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.190 was pushed (`origin/main` = `c43274f`) and installed on This Mac at 15:58Z the same day — see the 0.2.191 entry above.** Strict offline gate on commit `9b59ba6` (tree `1ef465c5`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **145 discovered, 143 executed, 143 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 25.72 s, `test_window_size.py` PASS in 80.15 s, the music regression `tests/test_music_output_format_advertised.py` PASS in 0.49 s and `tests/test_chat_deadline_forwarding.py` PASS in 0.79 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 145 rather than 0.2.189's 143 because the two branches add two suites (`test_music_output_format_advertised.py` and `test_chat_deadline_forwarding.py`; the audited artifact `tests/audit_regression_music_output_format.py` is deliberately named so the gate's `test_*.py` discovery does not run it directly — its entry point does). **The Story root is the pinned worktree `/build/Developer/_worktrees/story-studio-kh/gate-music-format` (`2e94d598`, tree `c500b2b3`), clean, with `node_modules` symlinked to the main checkout's**, not the moving main Story checkout — a first attempt against a fresh Story worktree without `node_modules` stops at `tests/test_content_delivery_integration.py` with `Cannot find package tsx`, which is why the pin is stated here; with it, that suite PASSes in 5.11 s and the gate runs to completion. Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-190> --story-root /build/Developer/_worktrees/story-studio-kh/gate-music-format --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`. **Both merges were clean — no conflict to resolve**, and the only overlap (`gateway/adapters/studio_hub.py`) auto-merged; both branches' own suites were run on the merged tree before the version bump. Build: `./menubar/build.sh --no-install` in `release-190` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.190**, build **`9b59ba6-202610191555`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.190.dmg`, 57,510,866 bytes, SHA-256 `a01bf9db272a162798db2ddc1c1c047b7d232482e631fcc86263a74a48e230fd`, **copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0190/dist/` (with its `.sha256`)**. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0190/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.189** build `e9a29db-202610191436`, PIDs 88246/88259 unchanged, port 8787 still served, worker alive. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0190/`.
>
> **0.2.190 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.190` off origin/main 0.2.189 (`512d536`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-190`, with two finished branches merged in order — `fix/music-output-format` (`1b6c0adf`) and `feat/dynamic-chat-deadline` (`c5314e5e`) — **both auto-merging cleanly, no conflict** (the only overlap is `gateway/adapters/studio_hub.py`). In plain English: **a music lane's own file formats are accepted.** A Studio Hub music lane advertises its own delivery formats on its catalogue row — a YuE2 lane declares `["flac", "m4a"]` and its native file *is* FLAC — while the gateway's music request validator allowed only wav/m4a, so a caller that read the advertised row and asked for `flac` was refused `400 INVALID_PARAMS`: the catalogue offered a container the door would not take. The row the catalogue publishes is now the authority — `api._validate_output_format` admits the named model's own `output_formats`, read back through the same `catalog._output_formats` the row builder calls, and falls back to the flat per-modality list when the lane states no enum (a request naming no model, a model this gateway does not offer, a cloud provider); only a value the flat list refuses pays for the resolution, so `m4a` and `png` cost nothing extra. The flat music list also gains `flac`, because a Studio Hub music lane really delivers it and `adapters/studio_hub._FORMAT_BY_MEDIA` already maps `audio/flac` to `.flac` — the door was refusing a container it could already describe and receive. Image (`png|jpeg`) and voice (`wav|m4a`) are untouched, and Story sends a fixed `m4a` and never reads `output_formats`, so it is unaffected. **A long writing job is no longer cut off before the Hub is done.** Studio Hub now computes each chat job's deadline from the job and cannot tell a 100-prompt scene pack from a one-line question by reading the body, so Story Studio's own expectation travels as **`X-StudioHub-Expected-Output-Tokens`** on both Hub-reaching chat lanes — a header, not a body field, because the Hub forwards the body verbatim to the chat worker and the worker refuses a field it does not know. `openai_compat.CHAT_TIMEOUT` was a flat 300 s and `lane_deadline_seconds` read the Hub's per-lane contract (1,800 s for chat); both cut a job the Hub was still legitimately running (a 100-prompt pack is ~2,500 s of generation on the fleet's slowest measured Mac before load, prompt and margin). The chat lane is now bounded by the Hub's own ceiling (**4 h**) on every path, including the two that state nothing at all — the lane is known from the request even when the contract is not, and the old 900 s fallback would abandon a job whose answer exists. Every other lane keeps its own stated number. Gateway-side changes: `gateway/api.py`, `gateway/adapters/studio_hub.py`, `base.py`, `openai_compat.py`; docs: `docs/studio-hub-wire-contract.md`; no app-side change. Tests: `tests/test_music_output_format_advertised.py` (the entry point, driving the audited artifact `tests/audit_regression_music_output_format.py` byte for byte, sha256 `d924705e…`; the artifact requires `GATEWAY_REPO` and has no fallback, so the entry point supplies the tree it lives in, which the offline gate's stripped child environment withholds) plus `tests/test_studio_hub_contract.py`; and `tests/test_chat_deadline_forwarding.py` plus `tests/test_hub_lane_contract.py` and `tests/mock_upstream.py`. **Gate note:** the music regression and `tests/test_content_delivery_integration.py` need the pinned Story worktree to have `node_modules` — the gate runs against `/build/Developer/_worktrees/story-studio-kh/gate-music-format` (`2e94d598`, clean, with `node_modules` symlinked to the main checkout's), not the moving main Story checkout.
>
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.189 was pushed (`origin/main` = `512d536`) and installed on This Mac at 14:52Z the same day — see the 0.2.190 entry above.** Strict offline gate on commit `e9a29db` (tree `9984dcbf`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **143 discovered, 141 executed, 141 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 29.62 s, `test_window_size.py` PASS in 88.15 s, and both branches' own suites PASS on the merged tree (`test_pools.py` 0.72 s, `test_seam_20261001b.py` 1.14 s, `test_job_recovery.py` 2.08 s, `test_location_readiness.py` 0.29 s, `test_pause_location.py` 3.17 s, `test_studio_hub_contract.py` 1.29 s, `test_free_location_reroute.py` 1.41 s), every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 143, the same as 0.2.188's: both branches add cases to existing suites rather than new files. **The two merges were both clean — no conflict to resolve**, and the overlap (`gateway/worker.py`, `tests/test_studio_hub_contract.py`) auto-merged; the branches' own suites were run on the merged tree before the version bump to confirm the auto-merge was semantically sound as well as textual. Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-189> --story-root /build/Developer/story-studio-kh --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`, with Story pinned at `2e94d598b4fb5dc180bc815566ce6306647b1d41` (tree `c500b2b3…`), clean. Build: `./menubar/build.sh --no-install` in `release-189` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.189**, build **`e9a29db-202610191436`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.189.dmg`, 54,466,635 bytes, SHA-256 `657c3fb1e0eae06ffab0b05c397ba39b66813d25aa18f33962a4f7b91be72537`, **copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0189/dist/` (with its `.sha256`)**, because `_trash/worktrees/gateway-kh/` was emptied between releases and 0.2.187's DMG was lost with it. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0189/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.188** build `d900198-202610191330`, PIDs 94308/94352 unchanged, port 8787 still served, worker alive. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0189/`.
>
> **0.2.189 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.189` off origin/main 0.2.188 (`63c4d95`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-189`, with two finished branches merged in order — `fix/hunt-pool-holds-settle` (`0f2d4714`) and `fix/hunt-fixed-numbers` (`9638272`) — **both auto-merging cleanly, no conflict**. In plain English: **a held job now ends as a known answer.** Every Gateway job must end KNOWN — a held job either settles from a real late answer or is closed — and three holes are closed. (1) The pool lane had no `discard`/`reconcile`, so `api.recovery_action` found no route on the adapter and every pool hold was closed locally with a sentence admitting the member was never asked (68 `HOLD_CLOSED` in 14 days; 47 of the 121 rows in the live database are `provider_id = 'pool'`); both actions are now forwarded to the member that ran the attempt, named in the error's `recovery` (`pool_member` plus the member's own `provider_id`) and carried on the job as its recovery context, which `api._discard_recovery_hold`/`_reconcile_recovery_hold` pass as the request's params because nothing else can name that member. (2) A **free** member's silence was allowed to mark the whole job ambiguous, contradicting the module's own rule ("advance on settled member failures; stop on an uncertain paid send") and producing a hold nobody could settle because the pool names no upstream id to aim at (measured `994394bc-e6e4-4e36-b9f6-e8bbacf914ca`, whose only outstanding member was the free `magpie::group/story-llm-free`); a paid send that may have been accepted still stops the walk with its member preserved. (3) `store.succeed()` adopted a late result for an open hold but excluded `HOLD_CLOSED` — the one close that says nothing about the attempt — so a real answer was written to `late_result_state` beside a `failed` verdict, where Story's by-key lookup (which reads `state`) never saw it (`5986ef32-2bd7-4293-93bf-37b1817dd448` recorded its own later attempt minutes past its close); the same rule now covers `HOLD_CLOSED` in `succeed()` and in the `_settle_late_successes` startup repair, while a hold closed from the *upstream's own record* (`close_hold`) keeps its verdict because that close is a proved answer, not a wait that ended. **A Mac installing an update is left alone:** `studio_hub.py` correctly maps the Hub's `MACHINE_BUSY` to a retryable `LOCATION_NOT_READY`, but the *selection* re-offered the same installing Mac (`97bdce60-90f4-4f47-b789-073744e87ea7` was sent to it 124 times in 80 minutes, 885 log lines, before failing with a sentence naming that one machine 124 times); `fleet.note_drain_refusal`/`is_draining_provider` now rest a location that says it is installing an update, and `worker._takes_no_new_work` — the owner's pause and the install, one predicate — is consulted by `_admit`, `_rebind` and `routing_candidates`; the marker is the Hub's install *sentence*, not the code, so a Mac that is merely busy ("local is reserved for other work on this Mac", 27 measured) is a moment and stays a candidate. **The queue wait ceiling is the owner's setting:** `QUEUE_WAIT_CEILING_S` was a module constant in the Studio Hub adapter (3 h) while the editable budget beside it lived in `concurrency.queue_wait_budget()`, which the worker and the API already read for deferrals and `Retry-After` — so the owner could edit his number and be overruled in silence (measured: **146 jobs failed `QUEUE_TIMEOUT`** between 2026-10-16 20:16 and 2026-10-19 09:40 local, every one `mflux-community/qwen-image-2-1-turbo-mflux-q4` with the same "No Mac was free for 3 hours…" sentence, every one having waited 3.00–3.32 h, and 139 of them at `attempt_count=0`, never dispatched at all, because his `provider-concurrency.local.json` carries no `queue` key so the shipped ~20-hour deferral budget was in force); `concurrency.DEFAULT_QUEUE_WAIT_CEILING_S` is now the default of `queue.wait_ceiling_seconds`, read live from the same file as the pool size, and `worker._expired_in_queue` and `studio_hub.queue_wait_ceiling_s()` (the poll loop's withdrawal and its two sentences) read that one number — behaviour unchanged until the owner edits it. Gateway-side changes: `gateway/adapters/pool.py`, `studio_hub.py`, `api.py`, `fleet.py`, `store.py`, `worker.py`, `concurrency.py`; no app-side change. Tests: `tests/test_pools.py::PoolHoldSettleTests`, `tests/test_studio_hub_contract.py::DiscardActionTests`, `tests/test_job_recovery.py`, `tests/test_location_readiness.py::UpdateDrainTests`, `tests/test_pause_location.py::DrainTest` (all in the first branch), and `tests/test_seam_20261001b.py` (three cases: the owner's 15-minute ceiling ends a half-hour-old job, a raised 24 h ceiling leaves a 3 h job alone, a nonsense value falls back to the shipped default) plus `tests/test_free_location_reroute.py` in the second. Each new test was proved to fail on the pre-fix code.
>
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.188 was pushed (`origin/main` = `63c4d95`) and installed on This Mac at 13:48Z the same day — see the 0.2.189 entry above.** Strict offline gate on commit `d900198` (tree `dda52aa5`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **143 discovered, 141 executed, 141 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 26.04 s, `test_window_size.py` PASS in 80.98 s, `test_chat_max_tokens.py` PASS in 0.18 s and `test_anthropic_structured.py` PASS in 0.24 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (Gateway HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes, Story HEAD/tree), and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 143 rather than 0.2.187's 142 because this change adds `tests/test_chat_max_tokens.py`. Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-188> --story-root /build/Developer/story-studio-kh --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`, with Story pinned at `53a0b5ad703271d1d44873faead9fe51cc27af41` (tree `216c4fbf…`), clean. **An earlier run on the superseded tip `b26ad0a` is kept under `gateway-0188/preliminary-b26ad0a/`**: the owner changed the fallback answer length to 128,000 mid-flight, so that run was replaced rather than shipped. Build: `./menubar/build.sh --no-install` in `release-188` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.188**, build **`d900198-202610191330`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.188.dmg`, 56,045,201 bytes, SHA-256 `6e0474dd563640144f3e95d5cfa2066c8081eddce742a6bde9bf9467e4a7421e`, **copied to the permanent record `_orchestra/codex-takeover-20261012/gateway-0188/dist/` (with its `.sha256`), not left only in `_trash`**. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0188/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.187** build `ce0e3af-202610190731`, PIDs 56072/56090 unchanged, port 8787 still served, worker alive. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0188/`.
>
> **0.2.188 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.188` off origin/main 0.2.187 (`b9801a8`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-188`, with `fix/output-limit-audit` at its final tip `408d73b` merged in — no conflict. In plain English: **a request that names its answer length in any accepted way is honoured on every provider** — `max_tokens`, `max_output_tokens` and `max_completion_tokens` all mean the same number, and `adapters.base.caller_max_output_tokens` is now the one reader of the caller's side, beside `catalog.published_output_cap` (the one chooser for the published side), called by all three lanes that build a chat body (`adapters/anthropic.py`, `openai_compat.py`, `studio_hub.py`). Before `b26ad0a` the Studio Hub lane read all three names while Anthropic and the OpenAI-compatible lane read two, so the same request was answered differently depending on which provider served it, and on Anthropic — where `max_tokens` is mandatory — a caller's `max_completion_tokens` was replaced by the adapter's private 4,096; measured against the live job store (4,000 most recent chat jobs) callers use `max_output_tokens` and `max_tokens` and never `max_completion_tokens`, so nothing in production was truncated by this — it is a hole a caller falls into, not one it has fallen into. **Otherwise the model's own published limit is used, and 128,000 tokens when a model publishes none** (owner decision 2026-10-19: *"4096 is too small. all models write more than that. the minimum is at least 128K tokens."*): `anthropic.DEFAULT_MAX_TOKENS` goes 4096 → **128,000**, which is also what `catalog._output_default` publishes on an Anthropic row nothing else states a figure for (`max_output_tokens_source: "gateway"`), so the row and the request still describe one allowance; it remains the last resort only, since a caller's number wins and a provider-published figure wins over both. **If a provider says that is too large, the request is sent once more at the provider's own maximum**: the Messages API requires a number, so a model whose real ceiling is lower answers `400 "max_tokens: 128000 > 8192, which is the maximum allowed number of output tokens"`, and that refusal is definite (nothing ran, nothing billed) so the call is re-sent **once** with the number the provider itself named, exactly as `_temperature_refused` re-sends without a knob the model refuses. Nothing is invented — `_max_tokens_too_large` reads the provider's own sentence, an unreadable 400 stays the failure it was, and **a caller's own figure is never rewritten**: only a number this gateway chose (the published cap, or the fallback) is corrected, and any reduction that does happen is recorded on the job as `max_tokens_lowered_to` because a reduction is never quiet. Two spellings are read because two are measured: Anthropic's `max_tokens: 128000 > 8192, which is the maximum` and the OpenAI-compatible `Expected a value <= 16384` a proxied lane answers with. Only this lane needs it — `openai_compat` and `studio_hub` forward the caller's figure and state none of their own. Gateway-side changes: `gateway/adapters/base.py`, `anthropic.py`, `openai_compat.py`, `studio_hub.py`, `gateway/catalog.py`; no app-side change. Tests: `tests/test_chat_max_tokens.py` (new) is a **class** test — one table of every lane that builds a chat body, asserted for each spelling — so a lane added later that reads one spelling goes red rather than dropping a customer's number in production; it fails on all three spellings without `b26ad0a` (the OpenAI-compatible lane answered `None`, Anthropic answered `4096`, where the caller asked for `1234`), and its second test holds that a lane which need not state a number states none. `tests/test_anthropic_structured.py` gains the 128,000 figure pinned as a number (not read back through the constant), the correction and its `max_tokens_lowered_to` record, the OpenAI-compatible spelling, that a caller's number is not rewritten, and that an unreadable 400 is not a licence to guess; `tests/mock_upstream.py`'s `HUB_CHAT_MAX_OUTPUT_TOKENS` follows the real Hub to 128,000 with the deadline arithmetic recorded beside it (the Hub's floor stayed 1,800 s).
>
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED. Superseded: 0.2.187 was pushed (`origin/main` = `b9801a8`) and installed on This Mac at 08:56Z the same day — see the 0.2.188 entry above.** Strict offline gate on commit `ce0e3af` (tree `369ac649`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **142 discovered, 140 executed, 140 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 26.18 s and `test_window_size.py` PASS in 80.34 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, Gateway pins byte-identical before and after, and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 142 rather than 0.2.186's 139 because the four merged branches add three suites (`test_gateway_liveness.py`, `test_fleet_lite_mode.py`, `test_macos_trash_auto_empty.py`). **The gate caught one real gap on its first run, test-side only:** `tests/test_fleet_banner.py` and `tests/test_lite_mode_offline.py` compile `menubar/FleetTabs.swift` against a fixed source list, and the two branches' new `menubar/FleetMacTrashModel.swift` and `menubar/FleetLiteMode.swift` were added to the sibling layout tests' lists but not to these two, so both failed with "cannot find 'FleetMacTrash' / 'FleetLiteMode' in scope" (`ce0e3af` adds the same two files to both lists; no production change). **The Story pin moved underneath the run** — another session committed Story 1.4.327 at 14:14 and 14:17 — so `pins.diff` carries the two Story lines and nothing else; the Gateway pins (HEAD/tree, `VERSION`, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, `menubar/build.sh`, `gateway/retention.py`, `gateway/startup_control.py`, interpreter link, runner and synthetic bytes) are byte-identical, and the only two suites that read `GATEWAY_STORY_SOURCE` (`tests/test_content_delivery.py`, `tests/test_content_delivery_integration.py`) were re-run green against the newer Story `2ef5af41`. Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-187> --story-root /build/Developer/story-studio-kh --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`. Build: `./menubar/build.sh --no-install` in `release-187` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.187**, build **`ce0e3af-202610190731`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 38 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.187.dmg`, 55,709,645 bytes, SHA-256 `d62d568f7a7e29d2f30e3bdb0853b8de1058253fc933715a827c6accff44e94c`. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0187/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.185** build `c6c6af0-202610181741`, PIDs 12116/12123 unchanged, port 8787 still served, worker alive. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0187/`.
>
> **0.2.187 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.187` off 0.2.186 (`ade58c49`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-187`, **superseding the built-but-never-installed 0.2.186**, with four finished branches merged in order: `feat/fleet-watchdog` (`dea45ec`), `feat/macos-trash-auto-empty` (`d908bd8` macOS Trash auto-empty and its routes; `26c17d2` the Lite Mode column), `fix/rediscover-scope` (`0a636e7`), and `fix/false-restart-settle-known-failure` (`7187e9a` a second opener of the job store is not a restart; `0e703ff` a hold settles as the failure that answered it; `776bb66` an omitted `max_tokens` takes the model's published cap). In plain English: **the Gateway now raises the alert instead of waiting for someone to notice** — the Telegram notifier's existing one-incident-per-fault machinery is pointed at the five faults that used to be found days late (a Mac more than six hours behind the published Studio Hub, a site whose Macs cannot run a model it offers, a Story Mac whose updater failed or stopped moving, a lane with work waiting and nothing in flight for thirty minutes, and the Gateway's own freeze, watched from the menu bar app's 3 s `/health` probe, which posts to Telegram itself because the Gateway's own notifier is the thing that is down), with an open/cleared history in `state["alerts"]`, a persisted pending-open clock, `GET /admin/api/alerts`, an Alerts card in Settings and three event types on by default (`fleet_watchdog`, `story_update`, `gateway_health`); **macOS's 30-day Trash emptying is visible and switchable per Mac** — a "Trash auto-empty" column reading On / Off / Unknown (Unknown is never drawn as Off, because a Hub older than 0.6.187 says nothing and so does a Mac nobody has heard from), a per-Mac "Turn on", and a bar that names every Mac it will touch and every one it skips, one way only (nothing turns it off), written through `cfprefsd` and read back so a write that did not stick is `NOT_APPLIED` rather than a false success, and **no Finder restart** (nothing in either repo kills Finder); **the Machines view now shows each Mac's Lite Mode** (On / Off / Turning on… / Turning off… / Change waiting / Unknown, sorted Off first, show-only, no Hub change); **Re-discover refreshes the provider you chose** — the Models page posts the per-provider route the filter names instead of `/refresh-all` whatever the filter said, with a picker for the fleet-wide run, a Re-discover on every provider card's row, "Re-discover all providers" instead of "Refresh All Models", and a result sentence that survives the `loadProviders()` redraw (both `refreshProvider` and `saveCreds` wrote their message *before* the reload that destroyed its span); **a job that failed for a known reason no longer reads as "the gateway restarted"** — the queue is claimed with an `flock` on `<db>.owner` so only the starter that actually takes it over sweeps, and `fail()` settles a hold when its own attempt answers with a known code (measured live: nine `pool::writing-haiku-gemini-luna-ordered` jobs sat at `failed / UNKNOWN_OUTCOME` naming a restart that never happened, while their attempt log read `LOCATION_NOT_READY` / `PROVIDER_UNAVAILABLE` / `LOCATION_UNREACHABLE`); and **a model asked for no length now gets its own published answer limit** — `BackendRequest.max_output_tokens` carries `catalog.published_output_cap` (the same chooser `_chat_row` uses), filled in at the worker's dispatch, the synchronous `/v1/chat/completions` route and the pool for each member, so `DEFAULT_MAX_TOKENS` (4096) survives only as the last resort (measured live: `story_idea_campaign`, 257 kB of prompt asking for 75 ideas, answered `stop_reason: max_tokens` on every Anthropic attempt and ended as `LOCATION_NOT_READY`). Gateway-side changes: `gateway/notifier.py`, `story_releases.py`, `fleet.py`, `startup_control.py`, `admin.py`, `admin_ui.html`, `store.py`, `worker.py`, `api.py`, `catalog.py`, `adapters/{__init__,base,pool,anthropic}.py`. App-side: `menubar/GatewayLiveness.swift`, `AlertsSettings.swift`, `FleetLiteMode.swift`, `FleetMacTrash.swift`, `FleetMacTrashModel.swift`, `FleetTabs.swift`, `FleetSection.swift`, `FleetStartupModel.swift`, `FleetTrash.swift`, `SettingsSection.swift`, `main.swift`. One conflict, in `docs/tasks/HANDOFF.md` (both sides added entries at the top), resolved as a superset: the two branch entries stay above 0.2.186's, with their "BRANCH ONLY" status marked superseded. **Retention is repeated in this release's What's New on purpose:** 0.2.186 was never installed, so the owner's app still reads 0.2.185's 8 GB default and the 3 days / 10 GB change has not reached him yet. Docs: `menubar/whats-new.md` and a new 0.2.187 section in `docs/fleet-deployment-notes.md`; the branch's "docs still owed" note is discharged.
>
> **Lite Mode column on Fleet > Machines — 2026-10-19. BRANCH ONLY: NO VERSION BUMP, NOT BUILT, NOT INSTALLED, NOT PUSHED. Superseded by 0.2.187 — merged; see the top entry.** Owner addition: the Machines view never showed each Mac's Lite Mode, so the owner could see whether a Mac starts its services and whether it answers, but not whether it lends the fleet its idle time. The Machines table now has a plain **"Lite Mode"** column reading **On / Off / Turning on… / Turning off… / Change waiting / Unknown**, sorted off-first (a Mac that will not lend its idle time is the one being looked for). **Show only, and no new Hub change**: every field it reads is already on the machine row — `workstation` (the Hub's own block), `lite_off` (the Gateway's verdict for a Mac switched off *in* Lite Mode, which reads **On**, not Unknown) and `lite_mode_pending` (a change held for a Mac that was offline, which wins over the last report because it is the owner's latest word). A waiting change that leaves `enabled` alone is **"Change waiting"**, not a change of mode. The Lite Mode sheet itself is unchanged and still on the Mac's "…" menu (`FleetStartup.editWorkstation`). App-side: `menubar/FleetLiteMode.swift` (new, pure — no networking, no windows), `menubar/FleetTabs.swift` (the column), and the two Fleet layout tests' compile lists. Tests: `tests/test_fleet_lite_mode.py` (5 cases: that `lite_off` and `lite_mode_pending` really come out of `fleet.machines_flat` / `stamp_lite_mode_pending`, and a compiled Swift check of the six labels, their colours and their tooltips).
>
> **macOS Trash auto-empty, seen and turned on across the fleet — 2026-10-19. BRANCH ONLY: NO VERSION BUMP, NOT BUILT, NOT INSTALLED, NOT PUSHED. Superseded by 0.2.187 — merged; see the top entry.** Owner request: Finder > Settings > Advanced > "Remove items from the Trash after 30 days" (`com.apple.finder FXRemoveOldTrashItems`) must be **on** on every fleet Mac, because every Studio Hub retirement ends in macOS Trash — a Mac without it fills its disk until someone empties the Trash by hand — and the owner could not tell which of ~53 Macs had it. In plain English: the Fleet > Machines view now has a **"Trash auto-empty"** column reading **On / Off / Unknown**, a per-Mac **"Turn on"** button, and a bar with **"Turn On for All Macs That Are Off"** (the confirm names every Mac it will touch and every one it skips; an offline or stale Mac is listed with why and never blocks the rest). **Unknown is never drawn as Off** — a Hub older than 0.6.187 says nothing about the setting, and so does a Mac nobody has heard from since. **One way only:** there is no route, here or on the Hub, that turns it off. Gateway-side: `gateway/fleet.py` (`_startup_fact` republishes `macos_trash_auto_empty` as a checked boolean — anything else becomes `None`), `gateway/startup_control.py` (`enable_macos_trash_auto_empty` relayed through the Mac's controller on the fleet credential; `enable_macos_trash_auto_empty_where_off`; the bulk helper `enable_autostart_where_off` was refactored onto one shared `_enable_where_off`, so both bulk runs have exactly one implementation), `gateway/admin.py` (`POST /api/fleet/controllers/{c}/machines/{m}/macos-trash-auto-empty/enable`, `POST /api/fleet/trash/enable-macos-auto-empty`). App-side: `menubar/FleetMacTrashModel.swift` (new, the pure half), `menubar/FleetMacTrash.swift` (new), `menubar/FleetTabs.swift`, `menubar/FleetSection.swift`, `menubar/FleetStartupModel.swift` (`macosTrashAutoEmpty` decoded from the same `startup` object), `menubar/FleetTrash.swift` (`listView` made reusable for the confirm's per-Mac list). Tests: `tests/test_macos_trash_auto_empty.py` (21 cases: the checked republish, the relay and its `HUB_TOO_OLD` / `NOT_APPLIED` refusals, the bulk's offline-and-stale handling, the admin-only routes, and a compiled Swift check that On / Off / Unknown read apart); `tests/test_fleet_layout.py` and `tests/test_fleet_action_banner_layout.py` gained `FleetMacTrashModel.swift` in their compile lists. **Requires Studio Hub 0.6.187** for the Hub-side route and the reported value (the Hub half is the companion branch `feat/macos-trash-auto-empty` in `studio-hub-kh`; its `MACOS_TRASH_HUB` / this file's `MACOS_TRASH_HUB` must agree). A Mac on an older Hub answers `HUB_TOO_OLD` and reads `Unknown`, never `Off`. **A Finder restart is not needed and is deliberately not done**: the Hub writes the preference through cfprefsd — the same path Finder's own Settings pane writes it through — and verifies by reading it back, so a write that did not stick is a refusal (`NOT_APPLIED`), not a success. The `killall Finder` in the usual recipe is conventional, not documented as required; nothing in either repo kills Finder. Docs still owed at release-prep time (a version bump is required first): `menubar/whats-new.md` and `docs/fleet-deployment-notes.md`.
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED.** Strict offline gate on commit `0ee3d2f` (tree `3eaa639`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **139 discovered, 137 executed, 137 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 27.32 s and `test_window_size.py` PASS in 79.45 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, pre/post pins byte-identical (interpreter link, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, runner and synthetic bytes, Gateway HEAD/tree, Story HEAD/tree) and a candidate fingerprint of the empty digest (`dirty_paths: []`). The file count is 139 rather than 0.2.185's 137 because the two merged branches each add one suite (`test_lock_inversion_health.py`, `test_settings_retention_fields.py`). Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-186> --story-root /build/Developer/story-studio-kh --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`, with Story pinned at `4d89f014d97975cacc9d5c203d51bd096ff09526` (tree `5c653829…`), clean apart from the owner's untracked `p18-lane-pause.json`. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0186/offline-gate/` (manifest, outcomes, per-file logs) and `pins-before.sha256` / `pins-after.sha256` / `pins.diff` beside it. Build: `./menubar/build.sh --no-install` in `release-186` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, archive `50424fa4…`, no network fetch — `menubar/vendor/` was copied from the canonical checkout, which is the only place it exists), producing `menubar/build/gateway-kh.app` — version **0.2.186**, build **`0ee3d2f-202610190322`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, all 8 bundled `gateway/*.py` byte-equal to the worktree and the bundled `whats-new.md` byte-equal too — and `dist/gateway-kh-0.2.186.dmg`, 54,101,349 bytes, SHA-256 `cd9cd95b45faa016f2a1f2674056e1f5498d811b2b9f3ac99f86cdc66163c00a`. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787, `gateway-0186/smoke.sh`) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` is still **0.2.185** build `c6c6af0-202610181741`, PIDs 12116/12123 unchanged, port 8787 still served, worker alive.
>
> **0.2.186 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED, NOT PUSHED. Branch `release/0.2.186` off 0.2.185 main (`94ebb39`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-186`, with two finished branches merged in: `fix/lock-inversion-health` (`1cd9823` lock-order inversion + lock-free `/health` + per-client cap; `8344604` artifact default 3 days / 10 GB) and `fix/update-key-and-retention-fields` (`b24d798` one App Update Key, instant retention limits read). In plain English: **the Gateway can no longer freeze while the fleet is slow** — `/health` no longer walks the fleet, so the app's probe always answers and no longer decides the Gateway is dead when a controller goes quiet, and `api._CLIENT_MAX_IN_FLIGHT` (32) stops one Mac's burst from taking every worker thread; **the App Update Key is one whole hex key** with a Copy button and the "should start with e6b9eece" caption, instead of a fingerprint followed by two encodings with the middle ellipsised (the fingerprint-first order is what put the wrong value into a Hub's App Update Key field and left a Mac broken for days); **the Keep for / Trash Folder boxes show their values again**, because `GET /admin/api/settings/retention` is now an instant limits read and the disk walk moved to `GET /admin/api/settings/retention/usage` (measured on this Mac: 3.1 s warm, past 15 s cold, minutes once walks queued, against the app's 15 s ceiling, which is why all four fields rendered empty); and **received artifacts are kept 3 days or 10 GB** by default (was 8 GB). Gateway-side changes: `gateway/api.py`, `worker.py`, `concurrency.py`, `constants.py`, `fleet.py`, `admin.py`, `release_publisher.py`, `retention.py`; app-side: `menubar/main.swift`, `SettingsSection.swift`, `FleetTabs.swift`, `render/fleet_fixture.py`. One conflict, in `tests/test_catalog_fast.py`, resolved keeping both sides: the slow hog is the usage walk on its own route and the 45 requests still come from two client addresses, because the per-client cap stops one address filling the shared pool. Docs: `AGENTS.md` and `docs/fleet-deployment-notes.md` follow the 10 GB default; the 0.2.141 entries' "fingerprint first / Technical details" text in this file and in `menubar/whats-new.md` is marked superseded rather than rewritten.
>
> **0.2.185 source prepared — 2026-10-19.** SOURCE PREPARED, NOT INSTALLED. Branch `feat/fleet-trash-freshness` (`5304cad` Fleet Trash freshness labels + `e183ac9` adjustable trash days/cap, log/voice-backup/job-archive retirement, backup prune keeps in place, Settings UI) fast-forwarded onto 0.2.184 main (`ab3e7229`) in the release worktree `/build/Developer/_worktrees/gateway-kh/release-185`, then the version bump. In plain English: the Trash Folder's own limits are now the owner's to set (default **7 days / 20 GB**, editable in Settings beside the received-artifact limits); the fleet Trash view shows when each Mac's Trash Folder was last checked and leaves an offline, unreachable or stale reading out of the total while still listing that Mac; and old log rotations, voice backups and archived job records age out into the Gateway Trash instead of being deleted or kept forever (backup pruning now moves an archive rather than unlinking it, and keeps it in place when the move fails). Gateway-side changes: `gateway/retention.py`, `startup_control.py`, `store.py`, `voice_import.py`, `backup.py`, `admin.py`; app-side: `menubar/FleetTrash.swift`, `SettingsSection.swift`, `main.swift`. **Also noted on this Mac, configuration not source:** the Magpie custom provider (`openai_compatible`, `127.0.0.1:3425/v1`) was added through `providers.local.json` in this Mac's app support; it is owner configuration, is not in the repository, and no source change or release step depends on it.
>
> **Offline gate and build verified — 2026-10-19. STILL NOT INSTALLED, NOT PUSHED.** Strict offline gate on commit `c6c6af0` (tree `e77093f`): synthetic suite exit 0, `Ran 5 tests` / `OK`; then the strict runner `--execute` exited 0 with **137 discovered, 135 executed, 135 PASS, 0 FAIL, 0 TIMEOUT, 0 NOT_RUN**, the two expected exclusions unchanged (`test_ui.py` installed-app promotion gate, `test_fleet_standalone.py` real-inference gate), all 9 native/offscreen suites PASS, `test_e2e.py` PASS exit 0 in 26.22 s, every executed child exit 0 with `process_absent: true` / `remaining_owned: false`, the independent PGID probe finding no owned group, pre/post pins byte-identical (interpreter link, `requirements.txt`/`requirements.lock`, `tests/_isolation.py`, runner and synthetic bytes, Gateway HEAD/tree, Story HEAD/tree) and a candidate fingerprint of the empty digest (`dirty_paths: []`). Command: `.venv/bin/python gateway_offline_gate_strict.py --root <release-185> --story-root /build/Developer/story-studio-kh --python /build/Developer/gateway-kh/repository/.venv/bin/python --report-dir <out> --execute`, with Story pinned at `b9ea4384c04cba7fbdf59aadfe0aa530cf2b2cab` (tree `dc2ab95a…`), clean. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0185/strict-run-20261018T173101Z/` (manifest, outcomes, per-file logs, pins-before/after, pins.diff, outputs.sha256). Build: `./menubar/build.sh --no-install` in `release-185` exited 0 against the trusted vendored runtime (`43887e81afa60c03`, no network fetch), producing `menubar/build/gateway-kh.app` — version **0.2.185**, build **`c6c6af0-202610181741`** (`CFBundleShortVersionString` / `CFBundleVersion` / `GatewayBuild` and `Resources/gateway/BUILD` all agree), ad-hoc signature valid, bundled `gateway/*.py` byte-equal to the worktree for every changed module — and `dist/gateway-kh-0.2.185.dmg`, 52,793,632 bytes, SHA-256 `d60f683bb128178984f2407b7fd02ec4332d5048975ac4b1967e84f70870adf1`. A headless packaged smoke (bundled runtime + bundled source, temp profile, port 18787) answered `/health` with that build id, worker alive and dispatch not paused, then exited cleanly on TERM with no listener left. The installed app was untouched throughout: `/Applications/gateway-kh.app` still build `ab3e722-202610180746`, PIDs 85315/85327 unchanged, port 8787 still served.
>
> **0.2.184 source prepared — 2026-10-18.** SOURCE PREPARED, NOT INSTALLED. Story release file range resume is frozen on branch `fix/story-release-range-resume-worktree` as commit `cc96ef5dd1dde8aa24df9d568c5ea6915506ea15` (tree `3cfa49b3cc60ad5335b540b83b76de94372d6a18`), exactly two files: `gateway/api.py` SHA-256 `dabb04f40585fd313765125662b19f2c42062105bf09aba8a4c6c11317bddc60` and `tests/test_story_releases.py` SHA-256 `0ccbb695ee9128b9ce9c581c930d06600ab4d35700481cfc157476f77835e7e7`; 2-file patch SHA-256 `e9af6800282c6a5db8aaaf7a2b509eba76f52fb35987b60cced85213937cb4bd`; base `c5d9637b4dc8eb8723d80fd5c25d0177ba4ab37d`.
>
> Focused canonical-runtime check PASSED: exit 0, 22/22 tests OK in 9.012s, pins and status unchanged, no remaining owned PIDs (receipt `~/Developer/_orchestra/codex-takeover-20261012/gateway-story-range-corrected-gate-20261018/02-focused-canonical-runtime/`). BROADER RELEASE GATES ARE STILL OWED. The canonical `gateway_offline_gate.py` is NOT suitable unchanged for the current stop-first-nonzero / TERM-only contract: its row loop continues after FAIL and `stop_owned_group` includes SIGKILL. Do not execute that old runner; the parent owns the external minimal-adapter review. No DMG, install, restart, push, or main mutation has happened. The current Story client sends no `Range`/`If-Range`, so its full-body download path is unchanged apart from the added `Accept-Ranges: bytes` header.

> **0.2.183 installed and verified — 2026-10-17.** Product `5a36ee967d63390cc7558cf23998b7d34608b10e` is on pushed main. Reviewed Gateway `426193f` (chat lane records the Mac that answered a completion, from the Hub's `X-StudioHub-Machine` header) was integrated by merge `2696b00` atop the installed-0.2.182 main, preserving `e1eeeb5` and 0.2.182 history; version bump `5a36ee9`. Offline gate: 125 files, 124 passed including 582 E2E checks; `test_retention_inputs.py` failed identically on base `436b616` and all 8 cases passed in fresh profiles. Packaged source/signature/boot checks passed. Primary `dist/gateway-kh-0.2.183.dmg`: 54,371,121 bytes, SHA-256 `aebefd1695763ed0fd43216c564a57f1f13066b448cc99843a98f90e6aadbec6`. Pairs with Studio Hub 0.6.183, which relays a controller's Chat request for a node-only model to one node.
>
> Installation: drain safe, app quit and one SIGTERM at 09:14:54; the replaced 0.2.182 backend exited by itself in 0.3 s and build `5a36ee9-202610170203` served at 09:15:03 (about 9 s of downtime, no SIGKILL). needs_recovery 12 → 12/12/12 at 10, 30 and 60 seconds; worker alive; dispatch not paused. Previous app: `_trash/app/gateway-kh-0.2.182-20261017-091455.app`. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0183/`.

> **0.2.182 installed and verified — 2026-10-16.** Product `7f27461f0f39728fdc4505d5686aaec4a81617de` is on pushed main (reviewed fix `e1eeeb55e39fd33d7eee03aa5b3c4c93fdaa94cb`, independent review ACCEPT, fast-forwarded over `3f02707`; version-bump commit `7f27461`): a Hub catalogue read that does not answer now keeps the last answer that had per-Mac facts and retries, so a model only a node has is no longer withheld on the controller's own disk while the read fails (1607 Gemma 4 26B, 2026-10-16). A first discovery with no earlier answer is unchanged. Offline gate: 125 files, 124 passed including 582 E2E checks; `test_retention_inputs.py` failed identically on `3f02707` (`10000000 != 6000000`) and all 8 cases passed in fresh profiles. Packaged source/signature/boot checks passed, including the two files that carry the fix. Primary `dist/gateway-kh-0.2.182.dmg`: 55,081,348 bytes, SHA-256 `499a2c4c26e92a5d5e7b3987d8905d5835cb0382af681b08d98503df879edf39`; previous DMG under `_trash/dist/20261016-234209/`.
>
> Installation: drain safe, app quit and one SIGTERM at 23:42:37; the replaced 0.2.181 backend exited by itself in 0.2 s and build `7f27461-202610161641` served at 23:42:50 — about 13 s of downtime, against 66.6 minutes when 0.2.180 (no graceful bound) was replaced. This is the 0.2.181 `--timeout-graceful-shutdown 3` fix working in practice. needs_recovery 12 → 12/12/12 at 10, 30 and 60 seconds; worker alive; dispatch not paused; no SIGKILL. Previous app: `_trash/app/gateway-kh-0.2.181-20261016-234238.app`. Live checks: fleet Trash route returns supported with real listings on This Mac (2 items) and 1100 (1,922 items). Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0182/`.

> **0.2.181 installed and verified — 2026-10-16.** Product `5692631303b62c3bda989765ec01960b294d913d` is on pushed main (reviewed feat/remote-trash-and-graceful-shutdown `c12421c`, fast-forwarded over `9393d35`): uvicorn now starts with `--timeout-graceful-shutdown 3`; Fleet > Machines > Trash… lists, moves to macOS Trash, or (owner app only, confirmed and re-measured) permanently deletes each Mac's Studio Hub Trash Folder; Macs on Hub older than 0.6.182 answer `supported: false, needs_hub: "0.6.182"`. Offline gate: 125 files, 124 passed including 582 E2E checks; `test_retention_inputs.py` failed identically on `9393d35` (`10000000 != 6000000`) and all 8 cases passed in fresh profiles. Packaged source/signature/boot, owner-console 403 and launcher-flag checks passed. Primary `dist/gateway-kh-0.2.181.dmg`: 54,892,895 bytes, SHA-256 `cd0b4c0e8ddea9c085ae62e401b504ecafbd0b360fe4539c3fdab3fdcd798be4`; previous DMG under `_trash/dist/20261016-190110/`.
>
> Installation: drain safe (unsettled 0), app quit and one SIGTERM at 19:01:46 local. The old 0.2.180 backend (no graceful bound) waited on 55 open requests from the mini; the guarded installer waited 30 min, aborted at 19:31:46 without force, and the no-signal continuation saw it exit by itself at 20:08:08. Build `5692631-202610161159` served at 20:08:24 (Gateway down about 66.6 min); needs_recovery stayed 11 at 10, 30 and 60 seconds; worker alive, dispatch not paused. Previous app: `_trash/app/gateway-kh-0.2.180-20261016-200808.app`. From 0.2.181 on the backend bounds this wait to 3 s. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0181/`.

> **0.2.180 installed and verified — 2026-10-16.** Product `f389620966e3be1341fba81a7a2833c3d3801734` is on pushed main (reviewed `7bf7b3b`, independent Opus review ACCEPT, integrated over `445a720`). On a final TTS section quality failure the Gateway binds the Hub's inactive review-only candidate in the same commit as the failure and serves it through admin-only `GET /admin/api/jobs/{id}/review-candidate` and `/audio` (byte-verified); the failure is unchanged and an older Hub answers 404. Offline gate: 124 files, 123 passed including 582 E2E checks; `test_retention_inputs.py` failed identically on `445a720` (`10000000 != 6000000`) and all 8 cases passed in fresh profiles, without application changes. Packaged source/signature/boot and route checks passed. Primary `dist/gateway-kh-0.2.180.dmg`: 54,147,264 bytes, SHA-256 `0fb9e28a92d65a6efdec4976388f73ac2469ed8c855eece08eecd8dc02694b96`; previous DMG under `_trash/dist/20261016-123348/`.
>
> Installation: the drain was safe (unsettled 0), the app quit and one SIGTERM was sent at 13:04:15 local, but the old backend waited in uvicorn's graceful shutdown on about 33 open inbound requests from the mini (uvicorn 0.53 has no graceful timeout), so the guarded installer aborted at 13:04:35 without SIGKILL. The Gateway did not serve from 13:04:15 to 13:15:52 (about 11.6 min). The old backend exited by itself at 13:15:45, and the installer's unchanged swap/start path brought up build `f389620-202610160529` at 13:15:52; needs_recovery stayed 5 at 10, 30 and 60 seconds; worker alive, dispatch not paused. Previous app: `_trash/app/gateway-kh-0.2.179-20261016-131546.app`. Lesson: the drain guard does not cover long open client requests; a future installer needs a bounded graceful-shutdown timeout or an explicit decision on uvicorn's second-signal force quit. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0180/`.

> **0.2.179 installed and verified — 2026-10-14.** Product `d8b4b8ac0990636d3b5eeaf7a1f9c96d632940d4` is on pushed main. The normal model-sharing POST now accepts an optional validated destination scope; existing cached peers outside that scope still lend through the guarded strict-LAN scheduler. Independent review, isolated nine focused checks, 25 LAN regressions, 122-file offline gate including 582 E2E checks, and packaged source/signature/boot checks passed. Retained gate failures were resolved by fresh-profile retention isolation and the matching release heading, without weakening application validation. Primary `dist/gateway-kh-0.2.179.dmg`: 61,334,315 bytes, SHA-256 `3b2884b843a6775a508eb4b1cbb19efb8f57c4378900b092f5a622c2ec1a2804`.
>
> The sole normal maintenance-drain installer completed with graceful shutdown, no SIGKILL, live build `d8b4b8a-202610140306`, and all four original recovery holds unchanged at 10, 30 and 60 seconds. Previous app is retained under `_trash/app/gateway-kh-0.2.178-20261014-103110.app`; previous primary DMG under `_trash/dist/20261014-031757/`. Exact source/artifact/installed receipts are in `~/Developer/_orchestra/codex-takeover-20261012/`. Story remains 1.4.304 and Hub 0.6.177. One accepted normal scoped Turbo continuation `e63e6a4aa65c` targets only returned 1503; follow its durable current run identity. Owner-skipped 1603, pauses, explicit model-off decisions and partial files remain preserved. No episode generation was dispatched by fixes. This closing provenance note does not alter the verified product or installer.

> **0.2.178 source verified, installer/install pending.** Opt-in persisted strict-order writing pools and typed refusal/incomplete outcomes preserve partial text/attempt evidence. Independent actual-diff/fixwave reviews are clean. Full123isolatedfiles exercised:121passed directly; retention8freshprofiles and unchanged late-binding10suite passed after retained initial failures. No scheduler/retention/validation change was made for those fixture/transient failures. Prior582isolatedE2E and native/offscreen suites passed. All paid LLM attempts are owner-authorized; this fixes coordinator dispatches no episodes. Main/push/primaryDMG/guardedinstallation/newnormalpool remain pending. Evidence gateway-0178-gate/{outcomes,resolution}.json.

> **0.2.177 installed and verified — 2026-10-13.** Product commit `f4382cc3662dbedcb4dd30edcd16f6763dbc33ae` is on pushed main. All 122 isolated gate files were exercised; the two retained fixture failures were reproduced and resolved by eight fresh retention profiles and the offscreen window test, without changing production retention. Primary `dist/gateway-kh-0.2.177.dmg` has SHA-256 `ec6075d4936482fd07e69e40d629612b570df4e61bfb94215c75548e6c46b2db`. Safe drain/double-check installation verified running build `f4382cc-202610130355`; the previous backend exited naturally and four recovery holds remained unchanged at 10, 30 and 60 seconds. Exact receipts are in `/build/Developer/_orchestra/codex-takeover-20261012/`. Hub 0.6.174 normal rollout remains in progress; new model-copy assignments stay stopped until accepted transfers settle and target/controller support is freshly verified. These notes update delivery provenance only; the shipped product remains the exact verified commit above.

> **0.2.176 installed — 2026-10-13.** Product source `5f8a504629fb2508dea6fee10c19136f03310bc1` is on main and pushed; installed build `5f8a504-202610121919` is verified live. Primary `dist/gateway-kh-0.2.176.dmg`: 61,572,585 bytes, SHA-256 `c215e76891dc0004d0db646ed3ebf988b9339bc1844d3d3fe3970ce0ca30b310`. Guarded drain completed normally, the old backend exited without SIGKILL, and the four pre-existing recovery holds remained unchanged at 10/30/60-second checks. Previous app is retained under `_trash/app/`; unrelated `.DS_Store` remains untouched.
>
> Authenticated `GET /admin/api/jobs/{job_id}/upstream` follows stored Hub provider/batch identity and exposes only four allowlisted fields: job identity, state, selected machine and bounded fixed-code dispatch evidence. No raw Hub summary, prompt, URL or credential is returned. Live authorized read returned 200; unauthorized read returned 401. New traces require Hub 0.6.172; older/historical jobs may have no trace. Routing is unchanged.
>
> Verification: focused trace five, hold sixteen, Hub contract/admin queue checks, all 582 isolated E2E checks, and the remaining 117 isolated files completed. The original retention harness contamination was resolved by running all eight cases in fresh isolated profiles without deleting preserved `_trash`; all eleven release-note checks passed after the missing entry was added. Other 115 files passed unchanged. No application retention code changed. Original failures and passing repairs remain inspectable. Receipts: `~/Developer/_orchestra/codex-takeover-20261012/gateway-0176-rest-gate/resolution.json`, `gateway-installed-0176.json`, `gateway-0176-artifact.json`, `gateway-0176-live-route.json`. This closing documentation commit leaves the verified product source and installer unchanged.

> **0.2.175 — 2026-10-12; YuE2 deadline from the measured slowest Mac.** `YUE2_SLOWEST_S_PER_SONG_S` 12 → 7.1 (M1 16 GB, 1,415.5 s for 199.5 s, load included; M2 16 GB 5.92, M4 16 GB 4.77, M4 24 GB 4.90), load 120 s, × 2: a 210 s request gets 3,222 s. Hub 0.6.170 states the same number on the item. Table: `_orchestra/audit-20261012/lyrics-flow/SPEED-fleet.md`.

> **0.2.174 — 2026-10-12; a YuE2 song's own deadline.** `studio_hub._yue2_length_deadline`: music + `vanch007/mlx-Yue2-3B` → `YUE2_DEADLINE_MARGIN (2) × (YUE2_SLOWEST_S_PER_SONG_S × duration_seconds + YUE2_SLOWEST_LOAD_S)`, joined into `lane_deadline_seconds` by `max`. Owner's rule: one number for every Mac, from the slowest measured class, × 2. Constants provisional (12 s per song-second, 120 s load) until the per-class measurement in `_orchestra/audit-20261012/lyrics-flow/SPEED-fleet.md`. Why: the music lane's 900 s cut 7 of 18 fleet songs at 15:44. Hub 0.6.170 states the same on the item (`deadline_s`). Test: `test_breeze_deadline.py` `Yue2DeadlineTests`.

> **0.2.172 — 2026-10-12; `site` pins a job to a location.** `fleet.pinned_location` also honours `params["site"]` (a controller id or `studio-hub-<id>`) for any modality, after the voice keys; it returns None when that location does not take customer jobs. It stays in the stored params, so `_rebind` (which checks `pinned_location`) keeps the job there; it is not in `studio_hub._HUB_MUSIC_PARAMS`, so it never reaches a Hub. Asked for by the YuE2 fleet benchmark (provider-scoped model ids do not pin). Test: `test_fleet_routing.py` `test_a_request_can_name_its_site_and_an_agent_site_is_not_pinned`.

> **0.2.171 — 2026-10-12; no default YuE2 cap; capped models keep the caller's priority.** Owner 2026-10-12: one generation per Mac, scene images always low priority, thumbnails priority, songs between. `DEFAULT_MODEL_FLEET_CAPS = {}`; `_submit` no longer forces background for a capped model (the 0.2.170 lines removed); the cap machinery (`model_fleet_cap`, `_job_room` room under the cap, `/v1/fleet/capacity` clamp + `fleet_cap`) stays for an owner-set `"models"` entry. Story 1.4.294 sends sung songs as normal priority, one per eligible Mac. Note for the owner (not changed): Story sends thumbnails and scene images both as normal priority, so the Gateway cannot order thumbnails > songs > scene images today; doing so needs Story to send scene images as background.

> **0.2.170 — 2026-10-12; YuE2 fleet-wide cap (songs with lyrics).** Owner approved the Story "songs with lyrics" flow (plan: `/build/Developer/_orchestra/audit-20261012/lyrics-flow/PLAN.md`). `concurrency.model_fleet_cap(model_id)`: built-in `DEFAULT_MODEL_FLEET_CAPS = {"vanch007/mlx-Yue2-3B": 2}`, overridden by `"models": {id: N}` in `provider-concurrency.local.json` (`0`/invalid = no cap). `worker._job_room`: a capped model's room is `min(room, cap − running of that model on every studio-hub-* site + 1 if this job is already running)` (the +1 so a running job re-routing after a failed attempt does not count against itself). `api._submit`: a capped model on a fleet provider is stored `priority: background` whatever the caller sent, so it only takes idle Macs and never goes ahead of normal work (`_background_room`). `GET /v1/fleet/capacity`: a capped model's `idle_macs` = `max(0, min(idle, cap − running))` and the row carries `fleet_cap`, so Story's music window (`lib/music-window.ts`, idle + 2 − queued) keeps ~2 waiting instead of one per idle Mac. Measured [M] 2026-10-12: YuE2 had never succeeded through the Gateway; probe `f97b7ba8` failed on 1300/1100/1200 with `missing=['LICENSE']` (Hub package bug, fixed in Hub 0.6.168). Tests: `tests/test_background_class.py` `ModelFleetCapTest`.

> **0.2.169 — 2026-10-11; Breeze clones every voice that has segments.** Outage: Breeze TTS 2 (`studio-hub::mlx-community/Breeze-TTS-2-mlx-4bit`) refused Mark and every voice without a saved transcript ("…clones a voice from a reference clip **and its exact transcript**…"). Root cause: the owner's 14-file import at 22:38 ran on 0.2.167, whose `import_folder` rebuilt the row of every recording it already had from the loose file, wiping `reference_transcript`, language, gender and description on Vincent, Samuel, Todd, Archer, Aiden, Mark and Leona (0.2.168 already skips same-sha re-imports). The Hub did not change: Breeze has always needed `ref_text`, and Hub `reference_audio.prepare` derives words from segments only when it cuts a window, which Breeze (no published maximum) never does. Data restored from each folder's `transcript.txt` and the 12:45 voice list (`_orchestra/audit-20261011/breeze-fix/restore_wiped.py`, backups beside it). Code: `studio_hub._reference_for` sends the saved transcript, else `segments_text(segments)` (segments joined; a segment of >8 words with under a quarter distinct is a Whisper loop and is skipped); `_voice_placement`'s cache key now includes a hash of transcript+segments+language, because keyed on the clip bytes alone the transcript-less asset staged at 23:22 kept being reused after the restore (live, measured). Tests: `test_voice_reference_containers.py` `test_a_voice_without_a_transcript_stages_its_segments_words`. Receipt: `/build/Developer/_orchestra/audit-20261011/breeze-fix/`.

> **0.2.168 — 2026-10-11; Add Voices picks files; duplicates are asked about; Remove/Restore.** Owner 2026-10-11: "it forces me to pick the whole folder" (adding Christopher imported all 14 files and made second Chris/Christian/Leo rows). **Picker** (`VoicesWindow.pickVoices`): files and folders, multi-select, audio types; one folder alone → `folder`, anything else → `files` (`voice_import.import_paths`; a folder in it is read like `folder`). **Import** (`voice_import.import_found`): same sha256 under any name → `skipped` with `duplicate: true` ("already here as X") — it used to re-import over the voice, resetting language/gender/permission; a different clip under a taken name (casefold) → `conflicts` `[{name, source, message, duration_seconds, suggested_name, existing: [{id, display_name, duration_seconds, imported_at}]}]`, nothing added; the app asks per name and re-posts the same body with `resolutions[source]` = `keep_both` ("X 2") | `skip` | `replace:<id>`. Replace keeps the id, name, language/gender (unless the new clip states them), drops segments (re-transcribed; annotation is not started while conflicts are open), resets fleet permission (note), and moves the old folder to `DATA_DIR/voice-backups/<id>--<UTC>--replaced/` + `entry.json`. A new voice whose `voice-<sha[:12]>` is held by a replaced voice takes the next 12-hex window (`_free_id`). **Remove/Restore**: `DELETE /admin/api/voices/<id>` now works for this Mac's own voices (either id spelling): folder → `voice-backups/<id>--<UTC>--removed/`; 409 `VOICE_IN_USE` when the gateway's own TTS jobs used it in 30 days unless `confirm_in_use=true` (`store.voice_use`, `GET /admin/api/voices/<id>/usage`); `GET /admin/api/voices/removed`, `POST /admin/api/voices/restore {backup}` (same id; refused if the id or recording is back). App: More → Remove Voice… / Restore Removed Voice…, row right-click Remove Voice…, a second confirmation naming callers and last use. The gateway cannot read Story's channel settings, so a voice set on a channel but unused for 30 days is not caught. **List**: new Clip column `12s · added <date>` from `duration_seconds` (import, else WAV header) and `imported_at` (import, else the voice folder's birth time). The owner's three duplicates (`voice-33fb0102b827` Chris, `voice-bdec2c68601a` Christian, `voice-8eba1f35a71f` Leo) were left alone and disabled by the coordinator; the owner removes them himself. **Private vs Share with Fleet (checked, unchanged):** a Private gateway-held voice narrates on any fleet Mac — `fleet._clip_travels_with_job` drops the pin, `studio_hub._voice_placement` stages the clip at whichever location runs the job, nothing on that path reads `permission_acknowledged`; Share with Fleet installs the voice in each controller's own Voice Studio library (persistent, for work not sent through the gateway) and is the per-voice license attestation. **Staging (owner 2026-10-11, "make it last 3 days")**: `studio_hub._REFERENCE_TTL_S` 6 h → 3 days, `_REUSE_HEADROOM_S` 12 h, `_STAGED_TTL_S` = 3 d 12 h requested; reuse ends at `min(staged + 3 d, Hub expires_at − 12 h)` (`_reuse_until`) because **Hub 0.6.166 clamps staged clips to 48 h** (`execution_assets.MAX_TTL_SECONDS`) — measured: asked 84 h, got 48 h, so reuse is 36 h until the Hub constant is raised (not done here: a Hub release; `fix/tts-memory-ceiling` was in flight in studio-hub-kh). The cache's third field is now the reuse deadline. Staged audio is FLAC when `voice_audio.lossless_flac` proves it (afconvert encode, decode back, PCM `data` chunk byte-equal; 16/24-bit integer PCM only), else the WAV; the Hub accepts `.flac` (`ALLOWED_EXTENSIONS`) and Voice Studio's libsndfile decode of it is sample-identical to the WAV (checked with the Hub's voice runtime, soundfile 0.14.0 / libsndfile 1.2.2). Christopher (105 s, 44.1 kHz 16-bit mono): 9,300,736 → 3,673,570 bytes per staging; live stage at the local Hub answered `audio/flac`, 3,673,570 bytes, 48 h. Tests: `tests/test_voice_import_duplicates.py`, `test_studio_hub_contract.py` (reuse capped by Hub expiry), `test_voice_reference_containers.py` (FLAC lossless); e2e section 16 updated.

> **0.2.167 — 2026-10-11; `POST /v1/vision/analyze` on the fleet's vision engine.** Owner approved 2026-10-11 (Hub 0.6.166 engine `vision-mlx`). `api.vision_analyze`: `{tasks ⊆ depth|subject|points|segments, image: {b64, mime?} | {url}, model?, text?, regions?, source_job_id?, site?, priority?}` (unknown field 422; decoded image over 12 MB 422) → the Hub's answer with model ids as `studio-hub::<id>`, plus `served_by {location_id, machine, why}`, `attempts`, `network {request_bytes, response_bytes}`. Only fleet ids serve (`MODEL_NOT_FOUND` 404 otherwise), so it cannot spend. Model: the named ids (every task must be published by one, else 422 listing each row's tasks), else per task the switched-on row publishing it, `default: true` first. Mac (`_vision_macs`): only Macs where `fleet._eligible_macs` holds for every needed model and `cache_state == "cached"`, at locations that take work; ordered source Mac (job store `provider_id` + `result_json.payload.hub_machine`), then the caller's site (`site`, else the location whose `url` or a node `address` has the caller's IP; loopback = `127.0.0.1`), then any (random within a tier). Transport (`studio_hub._generate_vision`): machine `local` → `{base}/studio/vision/v1/vision/analyze`, a node → `{base}/api/hub/fleet/nodes/{machine}/vision/analyze`; 90 s per Mac (connect 5 s). Hub 422 → 422 at once; 404/405, refusals, unreachable → next Mac, at most `VISION_MAX_MACS` = 3; none → `503 NO_VISION_ENGINE` (`Retry-After: 30`). New modality `vision` everywhere `embeddings` is; discovery keeps the catalogue's `tasks`/`default` in `raw`; rows `kind: vision`, `capabilities: ["vision"]`, `operation: image.analysis`, `tasks`, `default`, free. `GET /v1/vision/models`. Tests: `tests/test_vision_route.py`. Receipt: `/build/Developer/_orchestra/audit-20261011/vision/gateway/`.

> **0.2.166 — 2026-10-11; `POST /v1/embeddings` on the fleet's Embeddings Studio.** Owner decision 2026-10-11 (ideas plan Q1: our own apps, no Ollama). `api.embeddings`: OpenAI shape `{model, input}` (a string or up to 2,048 non-empty strings; `encoding_format` float only; other fields 422) → `{object: "list", data: [{object, index, embedding}], model, usage}`. Only fleet ids (`studio-hub::<id>`, or the bare id) serve, so it cannot spend. Synchronous, no job and no queue: `fleet.route(model, "embeddings")` picks the site, a refusing or unreachable site is excluded and the next tried (`EMBEDDINGS_MAX_LOCATIONS` = 3); none → `503 NO_EMBEDDINGS_ENGINE` (`Retry-After: 30`). Adapter `studio_hub._generate_embeddings` posts the Hub peer route `/studio/embeddings/v1/embeddings` (Hub 0.6.165: admitted, never leased); a Hub 422 is the caller's error. New modality `embeddings` in `catalog.MODALITIES`, the studio-hub kind's served set, both studio-hub definitions and `discovery._HUB_STATED_MODALITY`; rows publish `kind: embeddings`, `capabilities: ["embeddings"]`, `operation: text.embedding`, `dimensions`/`normalized`/`max_inputs`/`max_input_tokens` from the Hub's lane contract, zero prices and `chargeable: false`. `GET /v1/embeddings/models` lists them. Model today: `studio-hub::mlx-community/bge-small-en-v1.5-bf16` (384 dims, normalised). Tests: `tests/test_embeddings_route.py`. Receipt: `/build/Developer/_orchestra/audit-20261011/embeddings/DELIVERY.md`.

> **0.2.165 — 2026-10-09; background job class (bulk music fills idle Macs, production first).** Owner: "say i approve a pool of 500 songs, can we fill up the fleet if there's nothing else generating?". Request field `priority`: `"normal"` (default; absent/null) or `"background"`, on any generation route (popped in `api._submit`, never forwarded; `/v1/chat/completions` checks and drops it; the transcription confirm carries it from the JSON body); any other value is `400 INVALID_PARAMS`. Stored in the new `jobs.priority` column (NULL = normal) and published on the job envelope. Dispatch (`worker.scarcest_first` + `Worker._job_room`, the one path `_admit`, `_rebind` and re-route claims `_site_room` all use): background sorts after every normal job and takes turns by `fair_key` (job_group, else caller) counting its running background jobs; a background job at a Hub location is bounded by `_background_room`: the Hub's own idle-Mac count for its model (`model_supply[model].available`) less our sends in `_JUST_SENT_S`, and 0 while any normal fleet job waiting (due or deferred, `store.waiting_normal_shapes`) has an idle Mac at that site. So background never enters a Hub queue and never takes a Mac production could use; running background work is never stopped. Lane limits and Hub memory floors still apply (`_room`). `max_queued_jobs`: a normal submit counts only normal waiting jobs. `queue_rank` puts background behind normal. New `GET /v1/fleet/capacity?modality=&model=` (API key): per fleet model `eligible_macs`, `idle_macs`, `locations`, `running`/`queued` by class, plus `max_queued_jobs`; `Cache-Control: private, max-age=5`. Visibility: `store.stats()["background"]`, `/admin/api/queue` `background`, `/admin/api/fleet` `gateway_queue`; Jobs window shows a queued background job as "Background" (grey) and "N background music waiting" in the summary; Fleet → Jobs shows the Gateway queue line. Ceiling: the 3-hour `QUEUE_TIMEOUT` applies to background too, so a client should submit in windows sized from the capacity read. Tests: `tests/test_background_class.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0165/DELIVERY.md`.

> **0.2.164 — 2026-10-09; Claude rows publish Anthropic's own output limit.** `anthropic-direct::claude-haiku-5-5` published `max_output_tokens: 4096` (`max_output_tokens_source: gateway`, the adapter's `DEFAULT_MAX_TOKENS`), so Story Studio packed scene plans 10 per request (~46 Haiku requests per 165-minute episode). Anthropic states 128K for Haiku 5.5 (docs `models/haiku-5-5/overview`: "Max output 128K tokens", the synchronous Messages API limit) and its Models API returns `max_tokens` / `max_input_tokens` per model. `providers.discovery._anthropic_models` now keeps `max_tokens` in `raw` as `max_completion_tokens` (the spelling `catalog._chat_row` already reads), so every Claude row publishes its own figure with source `provider`; it wins over `model-limits.local.json` (gap-filling only). Absent stays as before (curated file, else 4,096 `gateway`). Context window is deliberately not read from `max_input_tokens` yet. Refresh `anthropic-direct` discovery after install to fill `raw`. Tests: `PublishedOutputLimitTests` in `tests/test_anthropic_structured.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0164/DELIVERY.md`.

> **0.2.163 — 2026-10-09; Claude models get guaranteed structured output.** Owner: Claude Haiku 5.5 is the main writing model for Night Atlas ("Haiku first, Gemini as backup"); Story refused it for scene plan and image prompts because `anthropic-direct::claude-haiku-5-5` published `supports_structured_output: null`. (1) `providers.discovery._anthropic_models` keeps Anthropic's `/v1/models` `capabilities` tree in `raw`; `catalog._chat_row` reads `capabilities.structured_outputs.supported` (a bool; absent stays `null`) and publishes `true` + `structured_output_mode: "request_json_schema"`, so Story sends its native schema. Refresh `anthropic-direct` discovery after install to fill `raw`. (2) `adapters.anthropic`: a `response_format: {type: json_schema, json_schema: {schema}}` (the shape every lane receives) becomes `output_config.format: {type: json_schema, schema}` (GA, constrained decoding; JSON in the text block, so `BackendResult.text` is the JSON as on other lanes). Not forced tool use: `tool_choice` `any`/`tool` is a 400 on the newest Claude models. `_wire_schema` removes the keywords the API 400s on (`minLength`/`maxLength`/`pattern`/`minimum`/`maximum`/exclusive/`multipleOf`/`minItems` except 0-1/`maxItems`/`uniqueItems`/`min|maxProperties`/`patternProperties`) and closes every object (`additionalProperties: false`), as Anthropic's SDKs do; property names are never treated as keywords. `_schema_problem` checks the answer against the caller's original schema (subset: type, enum, const, anyOf, properties/required/additionalProperties, items and the removed keywords; `$ref` unchecked). `stop_reason` refusal/max_tokens, non-JSON or a violation → `BackendError(PROVIDER_FAILED, retryable=True, upstream_accepted=True, upstream_settled=True)`: a known failure, no recovery hold, a pool moves to its next member. `json_object` without a schema and plain requests send the same body as before. Tests: `tests/test_anthropic_structured.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0163/DELIVERY.md`.

> **0.2.162 — 2026-10-09; the catalogue answers while the thread pool is full.** Live at 16:15 on 0.2.161: `/v1/models`, `/admin/api/{models,fleet,queue}` timed out (150–180 s) while `/health` took 0.03 s; `sample` showed the uvloop idle and ~40 anyio worker threads in rglob/lstat. Cause: the menubar Jobs window's 5 s tick → `GET /admin/api/artifacts/kept` → `retention.usage()` (walks ~21k artifact + ~30k trash files, `_safe_path` per file, the trash twice) once per tick; the walks stacked until every sync route (and the sync `require_auth`) waited for a thread. Fix: `/api/artifacts/kept` no longer returns `usage` (the Jobs window never read it); Settings/prune/keep use `retention.shared_usage()` (one walk at a time, waiters take its answer) and `usage()` walks the trash once; `Catalog.start_refresher()` (lifespan, 1 s tick: full rebuild on a signature change or the 20 s TTL) so a read never does the TTL rebuild (it still rebuilds at once on a signature change, ~1 ms check, so an enable is in the next answer), and `/v1/models` + `/v1/<modality>/models` are `async`, run on their own `anyio.CapacityLimiter(4)` (`_CATALOG_THREADS`, the `/health` pattern) behind `require_auth_async`; `JobsWindow` skips a quiet tick while a read is out and backs off (2× the read time, ≤ 60 s) after a read slower than 2 s. Tests: `tests/test_catalog_fast.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0162/DELIVERY.md`.

> **0.2.161 — 2026-10-09; never `succeeded` with an empty or broken artifact.** Story ref `20b773bc` (tts, `studio-hub-b8ccda585b97`, worker terranash-1504) settled `succeeded` with a 0-byte m4a: the Hub published the item `done` after its own voice verification failed (`broker._cache_voice_artifact_metadata` → `artifact_metadata_error`, no `sha256`/`bytes`), its artifact route streamed 0 bytes, and `_collect_batch` had no digest to compare. `worker.artifact_problem(content, media_type)` (header bytes only: size > 0; anything opening with `ftyp` and not labelled an image walks its top-level boxes for `moov` with none cut short; mp4/m4a labels need `ftyp`; png/jpeg/webp labels need an image header, PNG with a non-zero IHDR; wav/flac/ogg labels need an audio signature; mp3 and unknown types size only; lenient to a wrong label so a paid lane is never refused a good file). `_check_artifacts` raises `BackendError(INVALID_ARTIFACT, upstream_settled=True, retryable=True)` inside `_dispatch`'s try (a known failure → A7 `_next_machine`; no write, no ack) and at the top of `settle_result` (the adopt path keeps its hold). Over all 20,761 stored artifacts it flags only that file. Tests: `tests/test_artifact_guard.py`; fixtures with fake bytes given real headers. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0161/DELIVERY.md`.

> **0.2.160 — 2026-10-09; a fleet send unanswered at a restart is re-sent, not held** (owner: "allow re-send"). `JobStore._recover_interrupted` leaves a dispatched, id-less `running` job on a `studio-hub-*` site in `interrupted` when its modality is image/tts/music/video (`HUB_RESENDABLE_MODALITIES`) and no cancel was asked; `Worker._resume_interrupted` re-sends it when `restart_can_resend` (kind `studio_hub`) via `execute_job(resume=True)` → `_dispatch(..., resend=True)`: same `provider_id`, same `_client_request_id`. Hub `/api/hub/jobs` answers a repeated `clientRequestId` with the batch it made (ledger `batch_requests`, durable across a Hub restart; readiness/pause refusals skipped for a replay), so it attaches or runs once. Any re-send failure short of `upstream_settled` is a hold (`unknown=True`; no replay, no move, no A7). Recorded in `jobs.restart_resent_at`. Cloud lanes, transcription (Hub dedupes only while active), chat and cancel-requested jobs keep the restart hold. Tests: `tests/test_restart_resend.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0160/DELIVERY.md`.

> **0.2.159 — 2026-10-09; TERM pauses dispatch at once.** `api._pause_dispatch_on_term()` (called first in `lifespan`) chains onto uvicorn's TERM handler, which uvicorn installs before startup: `worker.pause_dispatch()` then uvicorn's own handler. Before, dispatch went on while uvicorn waited for open requests (Story's connections) and only stopped when the lifespan drain ran; the 0.2.157 backend took >20 s to exit on the 0.2.158 install. Test: `tests/test_lossless_restart.py` `TermStopsDispatchAtOnce`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0158/DELIVERY.md`.

> **0.2.158 — 2026-10-09; a Gateway restart never strands a job.** 0.2.153/0.2.156/0.2.157 installs held 2/10/1 jobs `UNKNOWN_OUTCOME` because the app SIGKILLed the backend 2 s after TERM while sends awaited their id (backend drain was 4 s). (1) `Worker.drain(timeout, pause_s)`: dispatch takes no new work (`_paused_until`; `_loop` skips `due()`), a job claimed but not sent is `JobStore.unclaim`ed back to `accepted` (attempt not counted), then it waits for every job thread in `_running` until `restart_can_follow(row)` (an upstream id on a lane with `resume`, not a Hub `chat:` id) or it settles. (2) `api.lifespan` shutdown = `worker.drain(_SHUTDOWN_DRAIN_S=15)` then `worker.stop()`; still-unanswered sends are logged and held as before. (3) Swift `terminateBackends` waits `backendDrainWait` (20 s) before SIGKILL. (4) `POST /admin/api/maintenance/drain?timeout_s=&pause_s=` (default 15/120; pause auto-expires; `DELETE` resumes) returns `safe`/`unsettled`; `/health` `worker.dispatch_paused`. Install scripts call it before quitting the app. Ceiling: a cloud lane without `resume` (fal/kie/xai/bfl/minimax video) is still held if it runs past the drain. Tests: `tests/test_lossless_restart.py`, `tests/test_gateway_lifecycle.py` (drain mode). Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0158/DELIVERY.md`.

> **0.2.157 — 2026-10-09; the Jobs list reads through indexes; `/health` never waits behind other requests.**
> Live (51k jobs, 300 MB DB): `GET /admin/api/jobs?limit=200` took 6-21 s and `/health` stalled up to 7 s, so the app's 2.5 s probe flipped to "Starting…" and pages said "not running". Mechanism (measured live and on a DB copy): **not** the event loop (`list_jobs` is a `def` route; a 404, served on the loop, stayed <0.2 s) but anyio's shared 40-thread pool, filled by requests waiting on `JobStore._lock` behind full-table scans (`list_jobs` `ORDER BY created_at`, `stats` `GROUP BY modality`/`MAX(created_at)`, `due()` `state NOT IN (...)` every 0.4 s pass, bulk `holds(limit=1000)`). (1) `_SCHEMA` adds `jobs_created(created_at)`, `jobs_state_created(state, created_at)`, `jobs_modality_state(modality, state)` and partial `jobs_open(created_at, id) WHERE state NOT IN ('succeeded','failed','cancelled')` (created on open via `CREATE INDEX IF NOT EXISTS`, ~1.4 s once on the live-size copy). (2) `bulk.needs_recovery` = `store.needs_recovery_count(as_of)` (indexed COUNT; was 1,000 hold rows counted in Python; same rule, no 1,000 cap). (3) `/health` = `health_route` running `health()` in its own `anyio.CapacityLimiter(2)`. (4) Swift `GatewayAPI.request`: a GET that times out while `isRunning` is `GatewayAPIError.busy` ("The gateway is busy, retrying…"). Paging, `include_removed` and bulk rules unchanged (checked against 0.2.156 on the DB copy). Tests: `tests/test_jobs_list_fast.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0157/DELIVERY.md`.

> **0.2.156 — 2026-10-09; fixes from the overnight review (`_orchestra/audit-20261009/overnight-review/REPORT.md` §1, §3, §5) plus the owner's Qwen speed decision.**
> (1) Re-routes (`worker._next_machine`, both the lost-contact and the known-failure moves) rank with the worker's live books via `fleet.route(load=)` → `fleet.apply_load` (`room` = lane budget left from `Worker._room`, `ours` = our running jobs as a floor under the snapshot queue, `just_sent` = sends in the last `_JUST_SENT_S` = 30 s taken off the snapshot's free count); `rank_candidate` puts a full lane last. The claim (`Worker._site_room(claim=True)`) books the new site under `_budget_lock` only while it has room; every lane full → `QUEUED_IN_GATEWAY`: `record_route` to the best site + `store.defer(unsent=True)` (clears batch id and dispatch stamp, keeps tried locations and attempt log), so `_admit`/`_rebind` send it later. `_rebind` uses the same `apply_load`. (2) `studio_hub._poll_batch`: past `POLL_DROP_GRACE_S`, if `FLEET_OUTAGE_SITES` = 3 remote sites (loopback excluded) went silent within `FLEET_OUTAGE_WINDOW_S` = 60 s of each other and still are (`_SILENT`, shared by poll threads), keep polling until an answer or the job's deadline; one silent site raises `lost_contact` as before. (3) `_rebind` failure: per-job backoff 1 s doubling to 60 s (`_rebind_backoff`), one warning per job. (4) Owner 2026-10-09 "Prefer fast Macs for Qwen": `fleet.speed_ranked` (`qwen-image` in the model id; both Qwen packages, no other model) adds `fast_s`/`idle_s` per site from `store.speed_samples` (per-Mac median `generation_seconds`, cached 60 s) → class median (`fleet.mac_class`, moved from `admin`) → `_CHIP_SECONDS` (10-08 medians); `rank_candidate` orders those by `expected_finish_s` (idle Mac's time, else fastest × (1.5 + backlog/slots)); `_rebind` stays in line for a full fast site (with `_ahead`, our jobs already waiting there this pass) unless a site with room finishes sooner. The Hub still picks the Mac inside a site. Tests: `tests/test_overnight_fixes.py`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0156/DELIVERY.md`.

> **0.2.155 — 2026-10-09; Anthropic follow-ups from 0.2.154.**
> (1) `adapters/anthropic.py` retries once without `temperature` when Anthropic answers `400` and `openai_compat._temperature_refused` matches (now also "deprecated" / "not supported"); `params_dropped: ["temperature"]` is recorded and `providers.definitions.learn_default_temperature_only` adds the model to an in-memory set that `default_temperature_only` reads, so the adapter sends none next time and `catalog._chat_row` withholds the control from its next rebuild. Lost on restart (first job after a restart pays one retried 400 again). The OpenAI retry still does not remember. Only `temperature` is handled: the adapter sends no `top_p`/`top_k`. (2) `store.usage_samples` reads `usage.input_tokens`/`output_tokens` when `prompt_tokens`/`completion_tokens` are absent (`COALESCE`), so `/admin/api/qualification` has `anthropic-direct` rows. (3) Claude Haiku 5.5's price is runtime data (`<DATA_DIR>/price-evidence.json`, imported via `POST /admin/api/pricing/evidence`), not code. Tests: `tests/test_chat_temperature.py` (`AnthropicTemperatureRetryTests`), `tests/test_qualification.py` (`test_anthropic_token_names_are_read`). Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0155/DELIVERY.md`.

> **0.2.154 — 2026-10-09; Claude models that refuse sampling are sent no temperature.**
> Owner: *"Claude Haiku 5.5 ... temperature has been deprecated for this model."* Three Story jobs (`night-atlas-history/ideas`, from 01:26 local) failed `PROVIDER_FAILED` with `anthropic-direct: HTTP 400: \`temperature\` is deprecated for this model.`; Story sends `temperature: 0.7` because the catalogue offered the control. The curated table moved from `catalog._DEFAULT_TEMPERATURE_ONLY` to `providers.definitions.DEFAULT_TEMPERATURE_ONLY` + `default_temperature_only(provider_id, model_id)`, beside `IMAGE_SIZE_BY_RATIO`, so the catalogue (withholds the control, no-`supported_parameters` branch only) and the Anthropic adapter (drops a temperature the caller sent, records `params_dropped`) read one table. New `anthropic-direct` row: `claude-(fable|mythos|opus-4-[7-9]|(opus|sonnet|haiku)-[5-9])`; Haiku 4.5, Sonnet 4.5/4.6, Opus 4.1/4.5/4.6 keep temperature. Anthropic's `/v1/models/{id}` `capabilities` were read for `claude-haiku-5-5` and `claude-haiku-4-5`: no sampling field, hence curated. OpenAI's table entry and the `openai_compat._chat` retry are unchanged; the Anthropic adapter has no retry backstop, so a new Claude family under a new name fails one settled `400` until the table learns it. Tests: `tests/test_chat_controls.py`, `tests/test_chat_temperature.py` (`AnthropicTemperatureTests`). Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0154/DELIVERY.md`.

> **0.2.153 — 2026-10-09; Jobs: "Remove Failed From List" and "Resolve All Needing Recovery".**
> Owner (Jobs > Failed showed 479): *"how can I clear it all in one go?"* (1) `jobs.dismissed_at` (REAL, `_ADDED_COLUMNS`) is a view flag: `store.dismiss_failed(before)` sets it on `state='failed'` rows that are not `unknown_outcome` holds and were last changed at or before `before`; it never writes `updated_at` (analytics, durations and retention read that). `list_jobs(hide_dismissed=True)` hides a row while `dismissed_at >= updated_at`, so a later change (late result, retry) shows it again. Routes: `POST /admin/api/jobs/clear-failed?before=` and `GET /admin/api/jobs?include_removed=1`. `GET /admin/api/jobs` adds `bulk {as_of, removable_failed, needs_recovery}`; the window sends `as_of` back as `before`, so a press touches only what the confirmation counted. (2) `POST /admin/api/jobs/resolve-recovery?before=` -> `api._resolve_all_recovery_holds`: for each `failed` + `recovery_required` hold, when `recovery_action == discard_upstream` it calls `_reconcile_recovery_hold` first (a `reconciled` + done item counts as recovered; the hold sweep collects it as before); otherwise, or when nothing is recoverable, `_discard_recovery_hold`. A refusal is listed in `left` and that hold stays. There are no new semantics: Story is released through the same `close_recovery_hold`. Swift: `JobsWindow` toolbar shows **Show removed**, **Remove Failed From List (N)** (Failed filter) and **Resolve All Needing Recovery (N)** (Failed and Needs recovery), each confirmed (`confirmBulk` for tests). Tests: `tests/test_jobs_bulk_clear.py`, `tests/test_studio_hub_contract.py` (`DiscardActionTests.test_resolve_all_*`), `tests/test_jobs_outputs.swift`. Receipt: `/build/Developer/_orchestra/audit-20261009/gateway-0153/DELIVERY.md`.

> **0.2.152 — 2026-10-09; installed models are switched on per location; nothing follows This Mac.**
> Plan 80e0f8dd921a installed Qwen Image 2.1 Low-RAM on 1100-1500, but those locations had no decision for it and a remote location's default is off, so `routing_candidates` (built from `enabled_models`) saw only This Mac and `/admin/api/queue` said `eligible_macs: 1`. (1) `model_share._switch_on`: when a plan's Mac reaches `done`, the package's model ids at that location (from the Fleet read's `local_models`) get an explicit enable via `ModelInventory.enable_undecided`; an existing decision, on or off, stands. (2) Owner 2026-10-09: *"a new group starts with every model it actually has installed switched on ... Nothing copied from This Mac."* `fleet.seed_enabled_installed` replaces `seed_enabled_from_local`: on add, enable what the location's Hub reports `files_verified` + `eligible`, catalogued, publishable. `fleet.NEVER_AUTO_ENABLED` (Phonon-2 parked, Pocket-TTS disqualified, the Hub's shelved list; the Hub publishes no such flag) is never auto-enabled by either path. **0.2.150 (`e75c0ef`) is withdrawn here, never built or installed:** its `_is_enabled` "a remote location with no decision follows This Mac" rule is removed (owner: *"nothing should follow this mac's setting"*). Tests: `tests/test_fleet_new_location_models.py` (was `test_fleet_inherit_models.py`), `tests/test_lan_model_share.py`. Receipt: `/build/Developer/_orchestra/audit-20261008/gateway-0150/DELIVERY.md (0.2.152)`.

> **0.2.151 — 2026-10-09; a switched-off Lite Mode Mac is expected, not a warning.**
> `fleet.machines_flat` sets `lite_off` on a machine whose state is `offline` (not `unauthorized`) and whose Lite Mode is on as best known (`_lite_mode_on`): a waiting change's `enabled` (`lite_mode_pending.json`, the owner's latest word, wins), else the reported `workstation.enabled`, else the last reported value kept in `<DATA_DIR>/lite_mode_known.json` (`remember_lite_mode`, called by `_refresh`). `_issues_for` raises no `machine_offline` for it and folds its workers and unreachable models away; off for more than `LITE_OFF_NOTE_S` (14 days, from `node_last_seen_at`) it gets one `info` `lite_mode_long_off`. `_summarise`: `machines_offline` excludes it, `machines_off_lite` counts it. `notifier.offline_conditions` skips it. The `state` stays `offline`, so routing and pending apply are unchanged (it gets no work). Swift: `FleetMachine.liteOff` -> State "Off", grey (`activityHealth .off`), `FleetOffline.liteOffNote`; `GroupSummary` excludes it from offline/attention and says "N of M online · K off (Lite Mode)". Tests: `tests/test_lite_mode_offline.py` (Gateway rules, pending, Telegram, swiftc group card). Receipt: `/build/Developer/_orchestra/audit-20261008/gateway-0149/DELIVERY.md`.

> **0.2.150 — 2026-10-08; withdrawn by 0.2.152 before any build** (see above).


> **0.2.148 — 2026-10-08; fleet model installs skip Macs below the package's memory tier.**
> `fleet.installable_models(location, machine, memory_gb)` also requires the Mac's total `memory_gb` to reach the catalog row's `min_unified_memory_gb` (the package tier): Image workers publish a host-level `memory_eligible`, so Hub 0.6.158 rows offered 16 GB Qwen Image 2.1 Low-RAM to 8 GB Macs and plan 80e0f8dd921a queued 34 of them. Plan start uses that list, so those Macs are "Skipped: Not enough memory for this model." at plan time. `model_share._start` turns a Hub install refusal matching "needs a Mac with at least N GB of memory" (`standalone._memory_refusal`) into the same skip instead of Failed. Unknown memory or tier: unchanged (the Hub decides). Tests: `tests/test_lan_model_share.py`. Receipt: `/build/Developer/_orchestra/audit-20261008/gateway-0147/DELIVERY.md`.

> **0.2.147 — 2026-10-08; Image jobs poll to their own per-job limit.**
> Hub 0.6.159 puts each Image batch item's own limit on the item as `deadline_s` (the worker's `120 + 1.5 x steps x that Mac's seconds per step`, clamped 900-7,200 s; `components/image/app/backend/generation.py:job_deadline_s`). `_poll_batch` (`gateway/adapters/studio_hub.py`) reads it on every poll beside `elapsed_s` and uses it when it is larger than the lane's `deadline_seconds`; it never lowers that number, and a non-finite or non-numeric value is ignored. Older Hubs send no `deadline_s`, so 0.2.147 behaves as 0.2.146 against Hub 0.6.158. No Gateway copy of the formula exists: the Hub owns the number. Tests: `tests/test_breeze_deadline.py` (raise, enforce, never lower). Delivery receipt: `/build/Developer/_orchestra/audit-20261008/gateway-0147/DELIVERY.md`.

> **0.2.146 — 2026-10-08; model management and compact installation reports. Delivery identities are receipt-owned.**
> The shared model filter folds case and common ID separators (`-`, `_`, `/`, `.` and whitespace), so "gemma 4" finds the local hyphenated Gemma model. Meaningful punctuation is preserved; nonblank separator-only queries match nothing. Browse, facets and bulk selection use the same filter. Identity, discovery, publication, saved model choices and owner pauses are unchanged.
> Fleet > Models retains all active installation plans and the newest completed report, collapsed by default. Each card exposes progress, active/skipped/failed counts, the first failure and Stop while running. Show details opens all network/Mac results in a 220 pt scroller. Expansion and scroll position survive polling; terminal transitions collapse; vanished-plan UI state is pruned. This changes presentation only, not installation or cancellation APIs.
> Live diagnosis before this release found the local `mlx-community/gemma-4-26B-A4B-it-qat-4bit` already enabled and publishable; the search spelling hid it. Existing Models row checkboxes are the model controls. This Mac's owner pause is separate and must remain unchanged. No new model qualification or inference was performed for these fixes.
> Scoped isolated integration: 57 Python tests and 579 end-to-end checks passed; 27 existing model-browser checks passed. Native regression, visual review, commit/push, optimized build, DMG and local running verification are recorded in `/build/Developer/_orchestra/model-management-20261008/DELIVERY.md`; consult that receipt for completion and exact source identities. No Hub/Story release, fleet/SSD rollout or Qwen qualification change belongs to this release.

> **0.2.145 — 2026-10-08; per-Mac function priority, source verified. Delivery identities are receipt-owned.**
> Fleet's machine menu offers Job priority for updated Hubs: No preference or Custom order using producer-owned function names and accessible move controls. Checked functions rank first; unchecked enabled functions tie below them. Disabled saved ranks stay in place. The authenticated controller/node relay reads and writes exactly one Mac's canonical work policy, preserving pauses and function switches; older Hubs report an update requirement instead of pretending a save succeeded.
> Gateway orders eligible waiting local work by the available target Mac's function preference, then FIFO at equal rank. Placement selects available eligible capacity before comparing pressure/preference; busy or ineligible preferred Macs cannot donate a rank to another Mac. Canonical Hub Voice `voice` is normalized against Gateway `tts`. Existing memory/qualification/owner-control checks, conditional claims, late rebinding, paid fairness and uncertain accepted-work holds remain authoritative. Running work is never preempted by a rank change.
> Final isolated integration gate: 16 files passed, comprising 366 tests and 579 end-to-end checks (`gateway-priority-focused/outcomes.json`). Native policy form behavior and light/dark renders passed; all-menubar Swift typecheck passed with two pre-existing deprecation warnings. Independent review findings were repaired and covered. These are scoped integration checks, not a full-repository or physical-fleet inference claim.
> Authoritative commit/push, optimized build, DMG, local installation, live policy/pause preservation and graphs: `/build/Developer/_orchestra/function-priority-20261008/DELIVERY.md`. The coordinator completes that receipt after this source commit; source VERSION is not proof of a running app. Local delivery only: no fleet/SSD rollout. Preserve the owner's This Mac manual pause, unrelated `.DS_Store`, and Qwen Image 2.1 qualification worktree.

# HANDOFF — gateway-kh

**Read this first.** Updated 2026-10-08 for **VERSION `0.2.146`** (the release blocks below are
newest first; 0.2.76-0.2.79 are recorded in `menubar/whats-new.md` and their commits). The running build
stamp is on `/health` and in `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD`.
If this file disagrees with the code, **the code wins — fix this file in the same change**.
Every path below is inside the Git checkout `repository/` unless it says otherwise.
`[MEASURED]` = observed in the session that wrote this; `[reason not recorded]` = the
project does not say why, and nobody should guess.

> **0.2.144 — 2026-10-08; Gateway source verified, delivery identities are receipt-owned.**
> Source is based on `7efedb6` / 0.2.143. A reachable, unpaused Studio Hub must advertise a valid model-specific pending contract before Gateway adds one waiting Chat allowance alongside execution. Wait time and request count are bounded; old Hubs, cloud providers and explicitly limited providers keep their established ceilings. Hub 0.6.156 supplies optional per-Mac LLM priority: current work finishes, eligible queued LLM work runs first, then other enabled TTS/image work uses idle capacity.
> Gateway checkpoints the opaque synchronous `chat:` identity before posting, reports the waiting phase and asks the Hub to cancel waiting work when cancellation/disconnect intervenes. Running/unknown cancellation is not proof of stopped work. Uncertain synchronous Chat outcomes stay held and are not replayed automatically, rerouted as free lost-contact work or reconciled through the durable batch 404 rule. Definite pre-execution refusals retain their settled classification.
> The coordinator's final isolated source gate passed all 115 eligible test files with exit 0, including end-to-end, all nine native/offscreen suites and JavaScript checks. Independent review found no high-impact issue. Installed `tests/test_ui.py` was excluded because this release changes no native UI and AGENTS reserves that owner-screen workflow; real-fleet inference was excluded while manual pauses remain. These exclusions are limits of the gate, not passing live UI or inference claims.
> **Running identity must be checked against the authoritative delivery receipt:** `/build/Developer/_orchestra/codex-remediation-20261007/WORK-POLICY-DELIVERY.md`. The coordinator writes/updates it with final commits, push, installer hashes and controlled installation evidence. This source record does not establish installed 0.2.144; do not infer live status from VERSION, a source gate or a built bundle. Keep the owner's This Mac fleet pause and Keep Gateway Running preference intact. Gate evidence is retained in that external evidence directory.
>

> **0.2.143 (2026-10-07) — reliable Gateway shutdown and installed UI verification (release candidate; build and installation pending).**
> [MEASURED] Backend PID 71449 had lost its listening port but remained alive with the job database open for about seven hours. The coordinator stopped that exact stale process under the owner's interruption authorization. `menubar/main.swift` now retains the PID and start-time identity of a spawned or health-adopted backend. Stop and Restart wait two seconds after TERM, then up to one second after KILL only for that same known process, and refuse a new backend while the old one remains. An unrelated port listener is never killed. Keep Gateway Running remains the owner's setting. The user benefit is that closing the port no longer lets the old backend keep working against the database while a replacement starts.
> Seven isolated lifecycle cases failed against base `aa5336d` and passed against the final candidate (31.255 seconds): spawned stop, adopted stop, adopted backend whose listener already closed, normal restart, clean restart, cancelled pending start, and preservation of an unrelated listener. Full native typechecking passed with two existing deprecation warnings. Independent source review also checked delayed callback ownership, PID reuse protection and the existing bounded restart-attempt limit. Installed release verification remains separate.
> `tests/test_ui.py` follows the current 20-row sidebar, Fleet tabs, actual row-owned action menus and Settings field geometry. It discovers the direct Machines table by its header and column count despite an optional warning scroller, matches names within combined accessibility labels, and corrects the undefined `fleet_summary` reference. Each page starts from the same measured 700 pt window height. The initial installed 0.2.142 run recorded 119 checks, 23 failures and a `NameError`; the next corrected run recorded 140 checks, six Fleet accessibility-read failures and one skip, with all window-size checks passing. The final focused Fleet check passed in 58.69 seconds after using the menu's advertised AXCancel dismissal and limiting full cell reads to API-matching local rows. The full installed 0.2.143 gate remains a promotion check. A separate offscreen Image-page check with four loaded 2048×2048 images retained a 1920×700 window, so that check did not justify a product geometry change.
> Final verification, commit/push, DMG, installed-app and running-backend identity evidence belong in `/build/Developer/_orchestra/codex-remediation-20261007/gateway-delivery-receipt.md`. This candidate note does not establish deployment; check that receipt before treating 0.2.143 as installed or live.

> **0.2.142 (2026-10-07) — serialize the shared job database connection (source verification complete; release integration pending).**
> [MEASURED] The installed 0.2.139 Gateway reported 19 durable running rows but only 14 live worker bookings. Sanitized log correlation identified the five-row gap exactly: four Hub transcription workers and one pool chat worker exited after SQLite `database is locked` errors while checkpointing upstream acceptance, artifacts or terminal success. The same window recorded `another row available`, commit `SystemError`, and repeated late-rebind lock failures. These were real configured providers, not test leakage. Evidence is outside the checkout in `_orchestra/codex-remediation-20261007/gateway-preinstall-diagnosis.md` and `gateway-sqlite-stranded-evidence.md`; no prompt, payload, URL, token or credential is retained there.
> Root cause: one `sqlite3.Connection(check_same_thread=False)` serves API, dispatcher, job, notifier and maintenance threads. Writers used `JobStore._lock`, but runtime readers did not, so a read could overlap a write on the same connection. `JobStore` now uses an `RLock` and every runtime execute/fetch/commit/VACUUM/close shares it. Constructor-only migration and restart recovery remain internal; analytics copy their scalar rows under the lock and run Python grouping or caller callbacks after releasing it. The `RLock` is required because locked paths re-enter read helpers (`submit() -> get()`).
> The deterministic base test is retained at `_orchestra/codex-remediation-20261007/gateway-sqlite-red.log`: it failed because a reader reached the connection while terminal settlement owned it. The fixed candidate passed 333 tests across 14 focused store, recovery, paid-pool, concurrency, analytics, pagination, retention and release files, then all 579 end-to-end checks; every file ran in a fresh isolated sandbox and exited 0. Exact per-file logs and counts are in `_orchestra/codex-remediation-20261007/gateway-0142-focused/`. Independent source review found every runtime direct connection access covered by the same lock, no network or await under that lock, and no production direct `_conn` access outside `store.py`; the per-instance scope matches the verified process-global store architecture. Current work was not cancelled, retried, resumed or mutated. At the latest read-only snapshot, 14 jobs remained booked and five records remained stranded, so the running Gateway was not restarted and 0.2.141 was not installed. A patched build and installation remain promotion work; restart recovery must stay conservative for those rows.

> **0.2.141 (2026-10-07) — audit repairs and clearer key names (verification/release integration pending).**
> Paid pool ambiguity stops fallback with the responsible member's recovery evidence. Reachable Macs and memory cooldowns share the same capacity population. Hub recovery uses bounded keyset rotation and excludes non-Hub holds; account history uses offsets and a matching scalar total. Story Update Now adds an optional request identity acknowledgement while preserving existing fields and legacy behavior. A malformed nested startup update phase now fails closed.
> Settings uses the owner's exact Gateway API Key and App Update Key copy; the signing fingerprint is first and public base64/hex values start collapsed under Technical details. **Superseded in 0.2.186 — do not act on this sentence:** the App Update Key section now shows one whole hex key with a Copy button, and the fingerprint, the base64 form and the "Technical details" disclosure are gone. No credential, environment or existing wire identifiers are renamed. Retention 0.2.140 remains intact. Worker proof and limitations: `_orchestra/codex-remediation-20261007/gateway-implementation.md` outside this worktree; coordinator owns full gates, review, commit/push, DMG and installation.
> Source verification completed at 2026-10-07T11:44Z from retention 0.2.140 base `1e5a0220ca54adac8488cb842aea3d4b1083a977`; the final fixture source was clean commit `874b7008de642e0d3a42188c028d3561b3bc8d0d`. The worker's earlier focused run passed 318 tests. The complete isolated source gate covered 112 eligible files: 102 Python and 10 Node, including e2e (579 checks), all nine native/offscreen suites and all Node VM/release-controller suites. Its discovery run recorded 110 pass and two stale exact-copy assertions; those two files were corrected without production changes and fresh isolated reruns passed 13/13 and 14/14. The only exclusions were installed `tests/test_ui.py` and owner-qualified real-inference `tests/test_fleet_standalone.py`. The first actual Settings fixture retained 12 before-proof captures that exposed narrow App Update Key help clipping. The targeted width-aware intrinsic-height fix then passed the affected window-size suite (1 test, 76.345 s). A clean-source fixture produced and passed 12 replacement captures: three disclosure/scroll states at 1100×1200 and 860×700 in light/dark. Required and actual help heights matched at 30/30 pt wide and 45/45 pt narrow, the full exact copy is visible in every key-focused state, secrets remain masked, and visual inspection found no clipping. The external `/build/Developer/_orchestra/codex-remediation-20261007/gateway-delivery-receipt.md` is authoritative for build, DMG, push, install and running-identity status; do not infer live status from this source handoff.
> Fleet's empty-trust help tells the owner to copy the raw public key from Gateway → Settings → App Update Key → Technical details into Hub → Connections → App updates, then compare fingerprints. A fingerprint is display evidence, not the value pasted into the Hub. **Superseded in 0.2.186:** the Technical details step no longer exists — the whole hex key is shown in Gateway → Settings → App Update Key with a Copy button, and the short prefix beside it is what is compared.

> **0.2.140 (2026-10-07) — recoverable automatic artifact retention (implementation and offline checks verified).**
> First-stage artifact retention keeps its 3-day / 8-GiB defaults and saved overrides, pins, one-hour grace period, and active or recovery-held input protection. Retired files go under the application's `DATA_DIR/_trash/`; existing `DATA_DIR/trash/` remains recognized. After seven days, or as needed to keep the combined runtime archive under 20 GiB, the oldest retired entries move to native macOS Trash. A failed transfer keeps the source and reports the failure. No permanent-deletion fallback is permitted, including under low disk space. Moving to macOS Trash does not free disk space until emptied; its 30-day emptying setting is owned by macOS.
> Verification: 71 focused tests passed; the safe offline gate passed across 102 files (101 files passed initially; content-delivery integration passed on rerun in 4.81 s after resolving the missing `tsx` dependency); 11 live/GUI checks were excluded; e2e passed in 25.46 s. Build, packaged native-bridge, integration and graph verification are recorded in the final release receipt linked below. The app has not been installed or restarted, and no live generation was verified. See `/build/Developer/_orchestra/codex-audit-20261007/retention/RELEASE.md` for the final build, commit and graph receipt.

> **0.2.139 (2026-10-07) — the Fleet page shows which Mac's Studio Hub update failed, and why.**
> Hub 0.6.152 reports `startup.last_update_error {phase: verifying|staging|downloading|installing, reason (<=300 chars), build, at}` or null, controllers and nodes alike. `fleet._startup_fact` republishes it through `_STARTUP_KEYS`, cleaned by `_update_error_fact` (those four keys only; unknown phase -> null; reason one line <=300; build <=40; `at` a finite number or null; nothing usable -> null). Swift (`FleetStartupModel.swift`, `FleetUpdateFailure`): a row shows the amber line "Update failed while <phase>: <reason> · <time>" (`FleetGroups.nodeRow`, a controller row too) while the error is under 24 h old, has a time, and the Mac's `version_status` is not `latest` - the gateway cannot see a Mac's running build, so "now running that build" is read as "on the newest published release" (the Hub also clears the error on a successful install). The group header's version line counts them ("1 Mac's update failed", amber). `release_rollout.status()` adds `update_error` to a step when its controller's own Mac reports an error for **this rollout's build** and the step is not `updated`; the panel appends it to the step line (`FleetRolloutStep.panelText`, moved out of `FleetTabs`). Tests: `tests/test_fleet_update_errors.py` (cleaning + a swiftc check of the row line, the count and the step text), `tests/test_release_rollout.py` (step `update_error`). Render: `GATEWAY_RENDER_FIXTURE=health-rollout-errors menubar/render/render-page.sh fleet:groups,fleet:locations`; evidence `_orchestra/audit-20261007/gateway-update-errors/`. This was asked as 0.2.138, but 0.2.138 (the pool 400 fix, ca9087f) was already built and live, so it is 0.2.139.
>
> **0.2.138 (2026-10-07) — provider-specific HTTP 400 must not defeat pool fallback.**
> Live `openrouter-free` failures stopped at Ling's generic `PROVIDER_FAILED` HTTP 400 before either GPT-6 Luna fallback was called. The request used `response_format: json_object`; the retained provider message did not state the underlying reason for its refusal. A provider HTTP 400 is not proof of a globally invalid request. The pool now stops only for an explicit Gateway `INVALID_REQUEST`, and continues to other members after settled provider refusals. The request and JSON requirement remain unchanged; quota memory, the 15-minute free wait, paid-last-resort ordering, cancellation and uncertain upstream outcome semantics are preserved.
> This fix is prepared in an isolated worktree. The running Gateway was `3c985a4-202610070531` and had 58 active jobs when inspected, so no live backend restart was performed. Do not describe this change as live until the backend is restarted and its new build verified. Evidence: `_orchestra/audit-20261007/pool-provider-400/` outside the checkout.

> **0.2.137 (2026-10-07) — Story releases: 30-minute check, no half-published window.**
> Story Macs check in every **30 min** (`story_releases.CHECKIN_EVERY_S`); `OFFLINE_AFTER_S` is **75 min**; each instance row carries `next_check_by` (last_seen + 30 min) and the Fleet tab shows "Next check by ~HH:MM" on a pending Update Now. Publish: the artifact is copied to `.incoming-<uuid>`, fsynced, renamed to `<DATA_DIR>/story-release/<sha256>/<filename>` (content-addressed, so a re-publish never overwrites the file a descriptor offers), and only then is `release.json` written (atomic, `config.write_secret`); the old artifact goes to Trash after that. `current()` returns None unless the descriptor's file exists with the signed size, so `/v1/apps/story/release` never offers a missing/short file. `/release/file` opens the file itself (open fd survives the Trash move) and streams it with Content-Length; it has no Range support. 0.2.136 stored files flat in `story-release/` (none had been published); that layout is no longer read.
>
> **0.2.136 (2026-10-07) — Story Studio remote releases.**
> `gateway/story_releases.py` signs a Story DMG/.zip with the Hub release publisher key (`release_publisher`; version and build read from the bundle's Info.plist) and offers it. `POST /admin/api/story/release/publish {artifact_path}` publishes (the descriptor and file live under `<DATA_DIR>/story-release/`, the old file goes to trash); Story calls `GET /v1/apps/story/release`, `/file` and `POST /v1/apps/story/checkin` with its ordinary bearer key. Publishing never pushes: each Story Mac checks in about every 2 min, records itself in `<DATA_DIR>/story-instances.json`, and installs once its queue has drained. `GET /admin/api/story/instances` lists every Mac (status up_to_date / update_available / updating / failed / offline after 15 min silence) with `gateway_jobs` {waiting, running} from `store.active_counts_by_caller()` (caller `story-studio@<Mac>`); `POST /admin/api/story/instances/{instance}/update` ("Update Now") makes the next check-in answer `update_now` until the Mac reaches the release or the update fails there. `/admin/api/queue` also carries `by_caller` / `by_group`. App: Fleet -> Story Studio (`menubar/FleetStoryTab.swift`); render it with `menubar/render/render-page.sh fleet:story` (the fixture seeds a release and five Story Macs). No Story release is published by this version; the owner publishes after Story 1.4.259 is built. Evidence: `_orchestra/audit-20261007/gateway-story-releases/`.

> **0.2.135 (2026-10-07) — fleet scheduling fixes from the 7-day fleet audit (F1-F3).**
> F1: an `INSUFFICIENT_MEMORY` hand-back starts a 15 min cool-down for the named (location, Mac, model) (`fleet.note_memory_refusal`); `fleet.usable_slots` removes resting Macs from the model's eligible count in `Worker._room` and `routing_candidates`, and the cool-down ends early when the fleet read shows the Mac's `available_gb` at least 1 GB above what it said it needed (idle 8 GB Macs read ~3.2 GB, at the need itself). A site where every fitting Mac is resting is routed to only if no other site offers the model (a submit with no route is refused).
> F2: the 10-05 Breeze jobs (attempt_count <= 2) never reached the defer ladder; the verified gap was **booking**: an in-thread re-route (`_next_machine`) left the job booked at the site that refused it for the rest of its run, so that site read fuller and the new site emptier. a re-route now calls the worker's per-job hook (`worker._MOVE_HOOKS`, so `execute_job` keeps its `(job, store)` shape) and `Worker._move` moves the booking. The defer ladder did bite on 10-04 (34 jobs at 256-300 s): fleet-location deferrals are capped at 15 s (`_FLEET_DEFER_CAP_S`); `queue.max_deferrals` still bounds them (240 at 15 s is ~1 h of back-to-back Hub refusals; 10-04's worst was 13). `_defer_delay` clamps its exponent (2.0**10000 overflowed).
> F3: `fleet.reachable_machines` caps a site's operation ceiling (`Worker._provider_budget`) and per-model supply at Macs not known to be offline.
> Observability: `/admin/api/jobs?since=&before=` + `next_before`; `/admin/api/analytics/daily?days=` (per day x model: outcomes, failure codes, queue-wait p50/p90); queue rows carry `eligible_macs` ("N Macs eligible").

> **0.2.134 (2026-10-07) — pool JSON and readable errors.**
> `savePool` passed raw object bodies for both POST and PATCH; fetch coerced them into `[object Object]`, yielding FastAPI 422 dictionary validation errors. Both callers now explicitly encode JSON with the JSON content type. The shared console API extracts human messages from string, structured, and validation-array errors without rendering rejected input or context. Authentication, ambiguous request handling, multipart bodies, and failed-save draft retention remain intact.
> Isolated HTTP reproduction measured malformed body 422 and equivalent proper JSON 200; spaces in a pool name are valid. Root independently ran all offline Node checks: 127 passed, including actual production savePool→api→fetch boundaries and safe error display. No provider generation was submitted. Owner requested creation of `openrouter-free` with the seven models selected in the screenshot, in the same order. Deployment and pool verification are retained in `_orchestra/audit-20261007/gateway-pool-json/` outside this checkout. Keep This Mac paused, and preserve the active backend when reopening the UI.

> **0.2.133 (2026-10-07) — Fleet action notice spacing.**
> Offscreen rendering reproduced zero bottom padding with the exact refresh copy, and clipped text at narrow widths. The shared `FleetBanner` now uses balanced 12 pt vertical / 16 pt horizontal insets with width-aware wrapping. Success, warning, and error notices share the fix; long titles truncate without widening the window. Title-only notices, the six-line detail cap, and success dismissal are preserved.
> Gateway layouts were audited against the current source and existing design scale. Attention cards and warning-list cards already use explicit inset constraints; no unrelated redesign was applied. Validation: 24 focused checks passed, including 36 notice states at 320/640/1643 pt in light/dark appearances, plus neighboring Fleet cards and lifecycle behavior. Full Swift parsing passed; mechanical layout scans returned no findings. Offscreen captures verify geometry; their layer composition is not a live-screen color guarantee. Release evidence is retained in `_orchestra/audit-20261007/gateway-banner-spacing/` outside this checkout. This Mac must remain paused.
> Deployment must distinguish the installed native app from the backend: the owner has Keep Gateway Running enabled, so a UI-only reopen retains the active backend and jobs.

> **0.2.132 (2026-10-06) — small-site routing and false swap warning.**
> `fleet.rank_candidate` used absolute free slots before its weighted lottery: a one-slot local site always lost an equal-pressure tie to a larger group with more free slots. Use a positive-availability class, then the existing model-eligible capacity lottery, in submission and late rebinding. Queue pressure remains first; eligibility and memory admission are unchanged.
> `memory_fault` treated cumulative swap >=4 GB as active heavy swapping, even with 22 GB available and normal kernel pressure. Swap remains published as information; faults require critical pressure or five minutes idle below the start floor.
> `menubar/build.sh` now moves replaced build/install paths into `_trash/build/<UTC timestamp>-<pid>/`, including DMG staging and bytecode. It refuses archive paths and never cleans the archive.
> Validation and live deployment evidence are recorded in `_orchestra/HANDOFF-20261006.md` outside this checkout. The Mini's Story update is a separate install: Story Studio 1.4.255 DMG is built; Mini installation is not yet verified.

> **0.2.93 (2026-10-02) - cancelled jobs publish `state: cancelled`; Gemma 3 12B is retired.** `gateway/api.py` now publishes a cancelled job as `cancelled`
> (it was `failed` + `cancelled: true`; Story still reads the legacy form). `gateway/providers/models.py` `RETIRED_MODEL_IDS` withholds LTX-2.3 **and** both
> Gemma 3 12B ids (`gemma-3-12b-it-4bit`, `mlx-community/gemma-3-12b-it-4bit`) by id in any modality - keep it equal to the Hub's `components/retired_models.py`.
> 0.2.90-0.2.92 are recorded in `menubar/whats-new.md` and their commits.
>
> **0.2.89 — every Create page redesigned to Studio Hub's generator shape; two root-cause fixes (`menubar/PageKit.swift`, `CreatePreview.swift`, `CreateSection.swift`).**
> Owner: *"all the generators on the gateway is small and ugly. unlike how the studio hub is ... as the preview and everything too."*
>
> * **One layout, six pages.** `CreatePageLayout` (PageKit) is the frame: heading, form column left (310-440 pt, ideal 390, its own scroller), result
>   pane right taking all remaining width/height (min 260), a `CardView` result card, a header with the page's result actions, and a caption. `CreateWorkspace`
>   only fills the form and swaps the card's content (`setPreview`); `CreateLane` gained `previewTitle/emptyTitle/emptyDetail`. The old stacked "form above
>   result, 330 pt controls" build is gone. Form order (final): Model, then inputs, then Generate; the sentence below describes the first cut: fields, Generate (accent, large), status line, then Model (+ Refresh, **Manage <lane>
>   Models** -> `MainWindow.show(section: "models")`) and capabilities.
> * **Previews (`CreatePreview.swift`), fetched automatically through the authenticated `GatewayAPI.download`:** `AudioPlayerView` + `WaveformView` (Audio, Music;
>   peaks read in chunks off-main; click/drag seeks), `ImageGalleryView` (large image + thumbnails, all outputs fetched), `VideoPreviewView` (AVPlayerView), `TranscriptText`
>   (SRT -> timestamp gutter + words; Copy, Save as SRT), `ConversationView` (You/Model cards). Several outputs of a non-image lane get a "Result N" switch. Header: Copy (text
>   lanes), Save..., and a menu: Open, Show in Finder (the saved copy, else the downloaded one), Pin/Unpin, Clear Conversation (chat), Reload Result, All Jobs. The "Preview"/"Open"/"Save"/"Pin"
>   row of buttons per asset is gone. Chat is now a real conversation: `chatHistory()` sends earlier completed turns with each message (a failed try and its message are left out); the transcript is
>   in memory only. `build.sh` links AVFoundation + AVKit.
> * **Form order (coordinator review):** Model (picker, Refresh, Manage Models, capabilities) -> divider -> the lane's inputs -> Generate + status line last. `test_create_layout.py` asserts, for all six lanes, model picker above the first input and Generate the lowest control (mutation-proved). Result caption is human ("WAV · 117 KB", "4 images · PNG · 120 KB"), not asset ids.
> * **Root cause 1 - `mock-job` in the real app.** `CreateStateStore` fell back to the production suite `kh.gateway.create` whenever `GATEWAY_NATIVE_DEFAULTS_SUITE` was unset, so
>   *any* process that built a `CreateWorkspace` without remembering the env var wrote the fixture's `mock-job` into the owner's Audio state. The committed tests have set a suite since `1e516e1`, so the run that leaked was an
>   uncommitted harness variant (`gateway-task5-debug-*` and `gateway-review-*` plists in `~/Library/Preferences` are their leftovers; the exact run is not recoverable) - [MEASURED] with the guard reverted a plain test run does write the real suite. A suite per run also left ~240 `gateway-*-test-*.plist` files there; moved to `_trash/prefs-litter/`. Fix: only a process whose bundle id is `kh.gateway.menubar` reads/writes the real suite; every other process keeps state in memory
>   (the env override still exists for the app under `serve_ui_fixture.py`). Second layer: the page no longer says "Job X is selected. Reload it"; on first open it reloads the saved job itself
>   (`restoreSelectedJob`, once per page life), and a **404** from `/admin/api/create/status/<id>` drops the id (`forgetJob`) and shows the empty state. Non-terminal reloaded jobs now poll (they used to sit
>   "still running" forever). **The owner's live `kh.gateway.create` audio state still holds `mock-job` until the installed app is updated and the Audio page is opened once**; nothing edits it by hand.
> * **Root cause 2 - Subtitle "Your selected model is no longer available", empty picker, "Capabilities: unknown".** [MEASURED live] `GET /admin/api/create/models` lists **2** enabled transcription models (Studio Hub
>   `whisper-small-mlx` / `whisper-base-mlx`; the admin models list has `studio-hub-local` and `studio-hub-de7250e146a7` enabled), so the lane was not empty and the page read the right lane/field (`by_modality["transcription"]`).
>   What broke is identity: those rows are now published as `studio-hub::mlx-community/...` with the previous ids in `legacy_ids` (e.g. `studio-hub-local::...`); the page had saved the old id, did not find it, reported it gone and reset the selection.
>   Fix: a saved id matching a row's `legacy_ids` is the same model (carried to its new id, no message). An empty lane now says "No subtitle models are switched on. Open Models and switch one on" (never "no longer available"); a genuinely
>   removed model is named ("hub::x is no longer switched on ..."), and its id is now kept while the lane is empty so switching it back on reselects it. `generate()` also refuses a model id that is not in the current list.
>   (`/v1/models/transcription` is not a route - 404; the catalogue is `/v1/transcription/models`.)
> * **Tests.** `tests/test_create_layout.py` (rewritten): for all six lanes at 1600x1000, 1100x800 and the app's true minimum detail width 660: one `CreatePageLayout`, form and preview side by side sharing a top,
>   form 310-440, preview reaching the right margin and taking the extra width, columns filling the height, window not forced wider, Generate/picker/script-editor minimum sizes, an empty state; then the real page restores a job and each lane
>   must show its preview (audio player + waveform, 3-image gallery, AVPlayerView, timestamped transcript, conversation) filling the card. `tests/test_create_workspace.swift`: no suite/env at all now, plus the real suite read before/after ("a test wrote to the real Create suite"),
>   unknown saved job dropped and not looked up again, renamed model carried over, empty Subtitle lane text + Manage Models opens Models, removed model named, chat conversation carries history, output fetched without a Preview click.
>   Mutation-proved (each fails on its assertion when reverted): store guard removed, 404 not forgotten, `legacy_ids` ignored, old empty text, preview off the right margin, form 700 pt, image gallery not built, preview not filling the card, small Generate.
>   **A mutation run that pointed the fallback at the real suite really did write the owner's `kh.gateway.create` audio modelID away (restored byte-for-byte; values were empty); the store-guard mutation is now proved against a throwaway suite name.**
> * Offscreen renders (no window shown): `tests/`-independent harness in the session scratchpad, before/after PNGs for all six lanes, empty and with a result, at 1600x1000 / 1100x800 / 660x600. AVPlayerView draws black offscreen (layer-backed); the player itself is asserted in the test.
>   Gate: `test_e2e.py` **578/578**, 59 focused Python suites, 9 Node suites, all exit 0; `test_ui.py`, `check_native.py`, `test_fleet_standalone.py` not run. Not installed.

> **0.2.88 — release push: a transport error is not a verdict (`gateway/release_publisher.py`).** (0.2.87 = `controller_ids`
> staged rollout, see `menubar/whats-new.md`.)
>
> * **Live pilot [MEASURED by the coordinator]:** `POST /admin/api/release/publish` with `controller_ids=["b8ccda585b97"]` (Hub 0.6.116,
>   delta-capable) took 550 s and returned `RELEASE_PUSH_FAILED "Server disconnected without sending a response"`, yet the controller then held
>   0.6.117 with `delta.manifest true`. So the push worked and the gateway misreported it.
> * **Fix:** any `httpx.HTTPError` after the request left (except a connect failure on the very first POST = `LOCATION_UNREACHABLE`) goes to
>   `_recover`: poll the controller's `GET /api/hub/release` every 5 s for 60 s (`_RECOVER_SECONDS/_RECOVER_INTERVAL`); if its record's
>   `signature` equals the pushed descriptor's (so version+build too) the outcome is `ok` with a `note` ("the reply was lost ..."); for a
>   delta `GET /need` must also be `[]` (non-empty -> `RELEASE_INCOMPLETE`; unreadable -> ok + note). Otherwise the original failure, with
>   what the controller holds. Same for the full push. The idempotent steps (blob PUT, `/need`, read-back) also `_retry` 3x on
>   `RemoteProtocolError/ReadError/WriteError/ConnectError` (a stale keep-alive), and use a 120 s read timeout (`_STEP_TIMEOUT`) instead of
>   600 s; only the manifest POST keeps 600 s (the controller may hash its installed app first).
> * **Every outcome** now has `mode`, `blobs_sent`, `bytes_sent` (failure paths included); a delta outcome adds `blobs_needed` and
>   `timings` (`manifest_post`, `blob_puts`, `need_check`, `readback`, `recover`, or `<step>_failed_after`); the state file has publish-level
>   `timings` (`hash_image`, `sign`, `probe`, `mount_manifest_sign`, `push`), and each controller logs one `[release] ...` line.
> * **Why 550 s [MEASURED gateway side; controller side is code reading, NOT measured]:** on the real 0.6.117 DMG the gateway's own steps
>   are ~3 s (hash 1.3 s, sign x2 0.2 s, mount 0.3 s, `load_manifest` 0.4 s; the manifest POST body is 6 MB, 47,340 unique blobs, 93,042
>   files). So the time is in the controller/transport. Suspect (studio-hub-kh `components/hub`): `PUT /release/blob/<sha>` is `async def` and
>   answers with `len(app_release.missing_blobs())`, which per blob lists the blob dir, rebuilds `installed_by_sha` over ~93k entries and sorts
>   ~47k, all synchronously on the event loop; with a few thousand small blobs that is minutes. Hub fix: return the count lazily or keep
>   an incremental set. The gateway cannot batch PUTs (contract), so 4 parallel PUTs stay. The new `timings` will show it on the next pilot.
> * `SUPPORTED_STUDIO_HUB_VERSION` 0.6.83 -> 0.6.117 (the "built against Hub" stamp on the Fleet page; a label, never a gate).
> * Tests: `tests/test_release_delta.py` (mock controller can hang up before/after storing, or drop the first N blob PUT replies): reply lost
>   after store -> ok via re-probe; before store -> real failure naming what it holds; stored but lacking blobs -> `RELEASE_INCOMPLETE`;
>   blob PUT retry; full push recovery; fields on every path. Mutation-proved (no recovery, recovery trusting any answer, need ignored, no
>   retry, no full-push recovery, no default fields). Nothing was pushed to a real controller. Not installed.

> **0.2.86 — artifact pruning goes to a self-cleaning Trash Folder (`gateway/retention.py`).**
>
> Owner decision 2026-09-30: the artifact store is a cache (Story Studio has the originals), so pruning must not hard-delete.
> * `prune()` now `rename`s each doomed file into `DATA_DIR/trash/<UTC date of the move>/<path under artifacts/>` (`_move_to_trash`;
>   stamped suffix on a same-day collision; a failed rename — e.g. a cross-volume `ARTIFACT_DIR` — skips the file, never unlinks).
>   The dated folder is the move-in record (a moved file keeps its old mtime). `_purge_trash` `rmtree`s folders named `YYYY-MM-DD`
>   whose date is more than **15 days** old — the only hard delete, owner-approved; non-date entries are never touched. It runs at
>   the top of every prune (also when input protection is unreadable and pruning is blocked).
> * Defaults **3 days / 8 GB** (was 7 / 10). `max_bytes` measures the live store only (`trash/` is a sibling of `artifacts/`).
>   `usage()` (so `GET/PUT /admin/api/settings/retention`, `POST /admin/api/artifacts/prune`) adds `trash_files`, `trash_bytes`,
>   `trash_days`, `trash_path`; prune result adds `purged_files/purged_bytes` (`deleted` now means "moved"). Native Settings >
>   Received artifacts shows "Trash Folder: X, emptied automatically after 15 days". The Create page text says 3 days.
> * **The LIVE `retention.json` was NOT changed** — a saved file keeps its old numbers; the coordinator applies 3 / 8 GB
>   (`PUT /admin/api/settings/retention`) after install.
> * Tests: `tests/test_retention_inputs.py::TrashFolderTests` (moved not deleted, collision, failed move skips, purge only >15 days,
>   max_bytes excludes trash + usage reports it) and `tests/test_e2e.py` section 19. Mutation-proved (unlink instead of move;
>   purge window 1 day; trash counted in the cap). Existing referenced-input / young-file / pin tests unchanged and green.
>   `test_ui.py` / `test_fleet_standalone.py` not run (owner's screen / real Hub). Not installed.

> **0.2.85 — prices visible in the app; Providers opens on the list.**
>
> The native Models and Providers pages are the gateway console (`gateway/admin_ui.html`) in a `WKWebView`
> (`ConsoleSection`, `menubar/MainWindow.swift`), so both fixes are in that page, not in Swift table code.
> * **Prices:** admin model rows (`/admin/api/models`, `/admin/api/providers/{id}/models`) now carry the catalogue's
>   own `price_source/price_usd/price_as_of[/price_url/price_variants]` (`admin._price` = the same `row_price` call as
>   `catalog._story_row`). Console: `priceInfo/priceCell` (Models "Price" column + each provider model row): per-1M
>   chat, per image/video/second/1k chars/request, `Free (your Macs)`, `Not published`, `Varies — N prices` (hover lists
>   every variant), source + as-of, `price_url` as a link (https only). Provider cards list "Rate limits (from pricing
>   research)" from `GET /admin/api/pricing/evidence` (`loadEvidence`; also re-shows accepted/dropped on load).
> * **Import price research… was dead in the app:** WKWebView shows no `<input type=file>` picker without
>   `runOpenPanelWith`; added it (and `createWebViewWith`, so `target=_blank` pricing links open in the browser).
> * **Providers opened on Concurrency — root cause:** the HTML's first pane is Concurrency (list pane is
>   `display:none`), `providerTab` starts `'list'`, and `setView` only repainted the tab buttons (`renderProviderTabs`),
>   never applied the tab. Strip said "Providers" over the Concurrency pane until a tab was clicked. Now
>   `setView` calls `selectProviderTab(providerTab)`: list on first and every activation; a tab the owner clicked
>   is remembered for the session (the WebView lives until quit).
> * Tests: `tests/test_price_ui.js` (node VM: price text per source incl. variants/unpublished, rate limits, initial
>   pane state seeded from the real HTML, first/repeated activation, remembered tab, Swift delegate present),
>   `tests/test_models_grouping.py` (admin rows carry price). Mutation-proved (each listed assertion fails when the
>   code is reverted). Not installed. The native Fleet "Models" tab (Hub models, all local/free) was not changed.

> **0.2.84 — delta release push, concurrent controllers, streamed full push (`gateway/release_publisher.py`).**
>
> Contract: `_orchestra/audit-20260930/CONTRACT-hub-release-delta.md` (Hub 0.6.115). `publish()` now: hashes the DMG
> streamed (no more `read_bytes`), probes every controller (`GET /api/hub/release`; a `delta` key with schema
> `studiohub.file-manifest.v1` = capable), mounts the DMG read-only ONCE only if some controller is capable
> (`_mounted`: `hdiutil attach -readonly -nobrowse -noverify`, `detach -force` in `finally`), reads
> `Contents/Resources/file-manifest/` (`load_manifest` re-verifies group sha, `files_sha256`, counts), then pushes
> all controllers concurrently (`ThreadPoolExecutor`, one worker per controller; each outcome caught, so one
> failure never stops the rest). Delta (`push_delta`): POST descriptor + `file_manifest` (no artifact) -> `need` ->
> parallel `PUT /blob/<sha>` (4 per controller, Content-Length, streamed from the mounted files) -> `GET /need`
> must be `[]` and the read-back `GET /api/hub/release` must carry this descriptor's signature, else
> `RELEASE_INCOMPLETE` / `RELEASE_NOT_STORED`. 404/405 on the manifest POST -> full push. Other refusals (400/409)
> are final and reported verbatim (409 appends the App Update Key public-key path and comparison fingerprint).
> * **Two descriptors are signed:** the delta one carries `files_sha256/files_count/files_bytes` inside the signed bytes;
>   the legacy one (no files fields) goes to controllers without the capability, so a 0.6.114 controller sees exactly
>   what it always saw. `state.release` reports the delta one (with the files fields) when there is one.
> * The full push is streamed (`_full_body`: same JSON, base64 in 3 MB chunks, computed Content-Length); the controller
>   still buffers it. If the image has no/invalid manifest the capable controllers get the full push with
>   `note` saying why. Per-location outcomes gained `mode` (`delta`/`full`), `blobs_sent`, `bytes_sent`.
> * Expect the 0.6.115 release to be a full push everywhere (installed apps have no manifest yet); delta from the next.
> * **Also 0.2.84 — variant-only prices publish.** `price_evidence.page_price` publishes a row as `provider_page` for ANY accepted
>   evidence (prices AND `other`-unit statements): `price_usd` = only units with one number (may be `{}`), `price_variants` =
>   every accepted entry (`usd` or `unit_text`). Before, a row whose every unit had variants stayed `unpublished`. Provider-API
>   price still wins. Fixture: `tests/fixtures/price-evidence-2026-09-30.json` (the real imported file; deepseek-v4-pro,
>   gpt-6-luna, fish s2.1-pro must come out provider_page). Mutation-proved (return None on no resolved unit; collapse variants;
>   `other` not indexed). CONTRACT-pricing.md updated.
> * **Also 0.2.84 — ACE-Step 1.5 retired from the fleet (owner):** Studio Hub location description now says "YuE2 and Stable Audio 3
>   music"; the music-params comment and `docs/studio-hub-wire-contract.md` no longer describe ACE-Step as shipped. The forwarded
>   params (`lyrics`, `vocal_language`, `instrumental`, `shift`, ...) are model-agnostic and unchanged; tests still use the ACE-Step
>   repo id purely as a wire fixture.
> * Tests: `tests/test_release_delta.py` (13; Python controller mock in delta/legacy/noroutes modes, fake app tree instead of
>   a DMG, `_mounted` injectable). Mutation-proved: need-only, 404 fallback, capability check, files fields inside the
>   signature, concurrency (barrier), detach, per-controller exception catch. A real `hdiutil` attach/detach of a small
>   DMG was smoke-tested by hand. NOT tested against a real controller (none pushed to). Not installed.

> **0.2.83 — imported price research: `price_source: "provider_page"`, validated per number.**
>
> Owner decision: the $250 is Claude Code session credit, not API credit, so **the gateway builds no
> Anthropic/Batch client and calls no model for research** (an earlier plan to do so was dropped before any
> code). A research session elsewhere (`research/pricing/README.md`) writes one JSON file
> (`_orchestra/audit-20260930/CONTRACT-price-evidence.md`); the gateway only validates and stores it.
>
> * `gateway/price_evidence.py` (stdlib only at import): `DOMAINS` is the one allow-list table. A price is kept
>   only with an `https` source URL on the provider's domain (exact or subdomain) AND a quote that contains the
>   number (commas stripped, `K`/`M` expanded; the quote must have words, not be a bare number); unit must fit the
>   modality (`other` is stored, never published); `model_id` must be a model of that provider with the same
>   modality (skipped only by the CLI, which has no model list). Rate-limit tiers keep only their verified
>   numbers. Everything else is dropped with a reason.
> * `POST/GET /admin/api/pricing/evidence` (stored in `DATA_DIR/price-evidence.json`; a provider in the file replaces
>   its old evidence). `pricing.row_price` gained `provider_id, model_id`: order of trust is local, provider's API
>   (`provider`), page (`provider_page` + `price_url` + `price_variants`, `price_as_of` = research date), unpublished.
>   A unit with several different numbers (variants) is not published as one price. Rate limits are informational
>   (admin GET only), never applied to concurrency.
> * CLI `python3 -m gateway.price_evidence check <file>` = the same `validate()`; exit 1 on any drop / bad schema.
> * Providers page: **Import price research…** (file picker -> accepted/dropped per provider with reasons).
> * Tests: `tests/test_price_evidence.py` (14; validation, cap-equivalents mutation-proved: host, quote, number
>   compare, model check, variants, scheme, rate-limit quote; template parses; import enables/routes nothing).
> * Ceiling: "quote contains the number" is textual; the gateway cannot prove the quote is on the page. Not installed
>   (coordinator does that). CONTRACT-pricing.md section 6 tells Story what to show.

> **0.2.82 — scarcest-first scheduling at the gateway: spread, late binding, queue ahead.**
>
> Owner policy + measured findings: `_orchestra/audit-20260930/DESIGN-scheduler.md` ("Gateway design").
> Measured failure: 20 same-second music jobs all bound to one location, this Mac's location got 0.
> Test: `tests/test_late_binding.py` (real `JobStore` + real `Worker` + patched fleet; it fails on 0.2.81
> and every piece below is mutation-proved).
>
> 1. **Per-job routing key.** `catalog.resolve(..., job_key=)`; `api._submit` passes `uuid4().hex`, so the
>    weighted lottery (`fleet.rank_candidate`, extracted from `route`) no longer draws the same winner for every
>    job of a model. Note the rank is still `(queue/slots, -available, lottery, id)`: the lottery only spreads
>    genuine ties, the next two items do the rest.
> 2. **Late binding** (`Worker._rebind`, `JobStore.rebind_unsent`). When `_admit` refuses because the bound
>    *site* is full (not the pool or the lane) the job is re-ranked over candidates that have room
>    (`_room` = `hub_budget` limit - our own in-flight there) and moved. **The store's single conditional UPDATE is
>    the authority**: it matches only `state='accepted' AND dispatch_started_at IS NULL AND upstream_job_id empty`
>    and the expected old provider, so a dispatched/sent/held job cannot move. Voice-pinned jobs and sites the job
>    already failed at are excluded. `record_route` (failure re-route, `_next_machine`) is unchanged.
> 3. **Queue ahead.** `hub_budget(free, ceiling=)` is now the site's eligible TOTAL (min the operator's number),
>    not `min(free + in_flight, ceiling)`; the `in_flight` argument is gone. No total stated -> the free count as
>    before. The 3 h `QUEUE_WAIT_CEILING_S` is untouched. Numbers are the Hub's/settings' "up to" ceilings.
> 4. **Scarcest first** (`worker.scarcest_first`, `fleet.pool_size`). Each pass orders `store.due()` by
>    `(pool, created_at)` with `pool` = sum of the model's eligible slots over reachable locations from one
>    snapshot (`routing_candidates`' `slots`: per-model supply, then the operation's
>    `eligible_physical_machine_slots_total`, then site-wide — consumed as-is, RAM-aware when the Hub's is). Cloud
>    jobs (no Mac) sort first; a model nothing can run right now sorts last. `store.due()` itself is unchanged
>    (FIFO); the sort is stable.
> 5. Local (this Mac's Hub) is an ordinary location throughout — no special case.
>
> Also: `tests/test_fleet_routing.py` had `unittest.main()` in the middle of the file, so its last class never
> ran; moved to the end (all 70 pass). **Not done / for the Hub side:** the tie-break "stay on the Mac's loaded
> model" and pinned pool=1 are Hub decisions; the gateway does not model them. Not installed or restarted
> (coordinator does that).

> **0.2.81 — controller handover + resume in the app; price truth beside the zero shape.**
>
> Owner decisions of 2026-09-30 (fix agent gateway-r3). Contract for the Story side:
> `_orchestra/audit-20260930/CONTRACT-pricing.md`.
>
> 1. **Fleet -> Locations: `Change address…` and `Resume`** (`menubar/FleetSection.swift`,
>    `FleetTabs.swift`, `FleetModels.swift`). Handover is `PUT /admin/api/fleet/controllers/{id}/address`
>    (keeps the row id, credential and per-model enable decisions; clears `site_id`); Remove+Add would make
>    a second location and orphan both. The sheet's **Test this address** is `POST .../{id}/test` with an
>    optional `{url, token}` body: it probes the *candidate* and stores nothing (blank token = the stored
>    one; the address is validated exactly as a re-point is). Confirming an address that has not answered a
>    test asks first. `FleetLocation` now decodes `paused_at`/`pause_reason`/`unreachable_since`; the Status
>    cell shows the gateway's pause sentence and a **Resume** button (`POST .../{id}/resume`) appears only
>    while paused. The Actions column is 420 wide (was 300) for the two extra buttons.
>    Tests: `test_fleet_layout.py` (decode + buttons + status text, mutation-proved),
>    `test_fleet_members_outage.py::TestingACandidateAddressStoresNothingTest`.
> 2. **Pricing** (`gateway/pricing.py`, wired in `catalog._story_row`, after `zero_pricing`, which is
>    untouched). Every row publishes `price_source` (`provider|local|unpublished`), `price_usd` (units per
>    modality) and `price_as_of`. Only provider-stated numbers: OpenRouter/Groq (`pricing` per token),
>    Token Harbor (`*_usd_per_1m`), xAI (`*_token_price`, USD cents per 100M tokens), fal (new
>    `GET api.fal.ai/v1/models/pricing` in `discovery._fal_prices`, best effort — **not verified live**, no
>    cloud calls; prices arrive on the next fal-ai model refresh). Studio Hub locations are `local`, price 0.
>    Everything else `unpublished` (`price_usd: null`). The envelope adds `cost_usd` + `cost_source:
>    "provider"` only when `usage.cost` is stated beside `usage.is_byok` (OpenRouter shape; measured on
>    OpenRouter and Experiential Labs jobs). Test: `tests/test_pricing.py`.
>
> **Open, not done:** fal tts units (chars) are not mapped; OmniRoute's snapshot `pricing` table, BFL's submit
> `cost` (credits) and ElevenLabs' credit multiplier are candidates for a later pass; xAI's `cost_in_usd_ticks`
> on job usage was not observed on a stored job, so it is not read.

> **0.2.80 — Story<->gateway<->Hub seam: adopt finished work, queue wait is not lane budget, a real waiting state.**
>
> Owner decisions of 2026-09-30, from the two seam audits (`_orchestra/audit-20260930/seam-*.md`):
>
> 1. **Adopt.** A hold whose Hub item is `done` is collected by `worker.reconcile_holds` through
>    `studio_hub.adopt_batch` -> the same `_collect_batch`/`_collect_transcription` a live job uses ->
>    `worker.settle_result` (extracted from `execute_job`: one done path) -> `store.succeed`, which now
>    settles a `failed / unknown_outcome` job as `succeeded` (any other terminal job keeps its verdict and
>    only records `late_result_state`). The sweep asks `holds(sweepable=True)` (unknown_outcome with an
>    upstream id), resolves the local Hub's address at runtime and never falls back to *this* Mac's Hub for
>    a remote hold (a 404 there reads as "never started" and would re-run work). `JobStore.__init__` settles
>    rows stranded before the rule (`_settle_late_successes`).
> 2. **Queue wait is not lane budget.** `_poll_batch` counts only time the item is `running` against
>    `lane_deadline_seconds`; `queued` pauses the clock and reports `on_wait` -> `store.record_waiting` ->
>    `waiting_json`. `QUEUE_WAIT_CEILING_S` (3 h, ponytail: one flat number) withdraws a batch still queued and
>    only re-routes if the Hub confirms `queued_cancelled >= 1`. A running item is never DELETEd on a guess.
> 3. **Envelope contract** (`CONTRACT-job-envelope.md`): every `_job_envelope` carries `dispatch_state`,
>    `cancellable`, `cancellation_requested`, `cancelled`, `waiting`. A cancelled job is published
>    `state: "cancelled"`, `cancelled: true`, `failure_code: "CANCELLED"` (0.2.93: it used to say `failed`; Story maps
>    both spellings to the same terminal fact). **`_job_envelope(job, native=True)` is the
>    menu bar app's reading** (same state, does not scrub) - the admin routes use it. All published
>    text goes through `api.scrub` (no `host:port`, no `/api/hub`).
>
> Also: `POST /v1/videos/{id}/cancel`, `/v1/transcriptions/{id}/cancel`; a cancel set before the batch id exists
> is sent at `record_upstream_acceptance` (`worker.cancel_upstream`, moved out of `api.py`); `GET
> /v1/voices/{id:path}/preview` and `preview_url`/`preview_available` only when a preview exists;
> `/v1/voice-library/{elevenlabs|fish}` (browse, `/add`, `/{id}/preview`) over `search_library` and the admin
> `import_voice`; `paid_active_jobs`/`paid_queued_jobs` on `/v1/account/overview`; `poll_after_ms` per lane;
> the video source image staged with the job (`_reference_path`, protected from pruning); transcription
> `language`/`word_timestamps` carried quote -> confirm -> Hub form; `supports_reference_images` never `true`
> (no upload route and no adapter mapping; delete the override in `catalog._image_row` when both exist);
> `gateway.__version__` reads `VERSION` (build.sh copies it beside the package); Hub-seam fixes F1-F10 of
> `seam-gateway-hub.md` (refusal code in `detail.code`, item `error_code`, artifact retry + sha256, poll grace for
> 5xx, chat ConnectTimeout, transcription job id as `item_ids`).
>
> **P0 found live: `fleet.json` lost updates.** `fleet.locations()` wrote back the roster it read *before* a
> multi-second probe, and `last_reachable_at` made that every probe (~10-30 s): a rename answered 200 and was
> reverted 3 s later. Now `_ROSTER_LOCK` (RLock) guards every read-modify-write, the write is atomic (temp +
> `os.replace`), the snapshot persists only the reachability keys merged into a **fresh** read
> (`_persist_reachability`), `add_controller` reads after its probe, and `last_reachable_at` moves the write
> trigger only in 5-minute steps (it is read only as "ever seen up"). `tests/test_fleet_roster_writes.py`.
>
> **Tests:** the 7 red ones (`test_worker_concurrency`, `test_live_capacity`) assert invariants against the
> effective settings (`cloud_budget`, `lane_budget`, `global_budget`), not fixed numbers, and pass with the
> owner's settings and a clean default (`GATEWAY_PROVIDER_CONCURRENCY=<none>`). `test_fleet_routing`'s ~1-in-8
> flake was `mock_upstream.Server`'s listen backlog of 5 against the probe's 9 simultaneous connections
> (`request_queue_size = 128`). New: `test_story_envelope_contract.py`, `test_fleet_roster_writes.py`, and
> additions to `test_studio_hub_contract.py`, `test_hold_reconciliation.py`, `test_hub_chat_lane.py`.
>
> **Open, not done:** Story-side vocabulary (seam-story-gateway F8), Story must treat `recovery_required` as
> non-final for ~5 minutes, PNG-only image check (F12), transcription `total_pages` etc. F4's owner decision is
> the queue-wait bound above. The 09-26 `MACHINE_BUSY` rows were **chat** jobs (peer route, no batch).

> **0.2.75 — the Fleet page stops timing out, and a hold becomes discardable.**
>
> `_CACHE_TTL` was 5 s while `start_refresher` runs every 15 s, so the cache was stale two thirds of
> the time and the request path did the fan-out itself — the opposite of what the refresher's own
> docstring promises (*"the request path only ever reads a value that is already there"*). With ten
> sequential Hub calls per location at a 6 s timeout, one snapshot measured **22.7 s** against the
> app's **15 s** timeout. The owner saw it as *"this isn't showing. but when there's no job running.
> it did show."*
>
> Now: **stale-while-revalidate** on the plain read (0.000 s), the nine enrichment reads fetched
> **concurrently** (forced refresh 22.7 s → **8.3 s**), and **one** sweep shared by concurrent
> readers. `force=1` is the Refresh button, the only path that waits.
>
> **The lock is an `RLock`, and that is load-bearing.** A refresh reaches `_attach_admission`, which
> lazily imports `runtime` to find the live worker; that import builds the `Catalog`, whose
> `rebuild()` calls `usable_location_ids()` and therefore calls back into `snapshot()`. A plain
> `Lock` deadlocked the whole read on the first refresh after boot — caught with `faulthandler`, stuck
> in `snapshot` -> `usable_location_ids` -> `catalog.rebuild` -> `_attach_admission` -> `snapshot`.
>
> `_named_attempt` now reads `batch_id` / `item_index` as well as the restart's spelling. The Hub
> adapter writes the former, so **Discard** on an uncertain job answered `RECOVERY_CONTEXT_MISSING`
> for a hold that named its batch perfectly well; `worker.py` already read `batch_id`, so the gap was
> in this reader alone.
>
> `tests/test_fleet_snapshot_freshness.py` (7) and a case in `tests/test_job_recovery.py`. Gate:
> **577 e2e checks**.

> **0.2.74 — pools: one name for several models, tried in order.**
>
> The owner, on why a second router sat in front of this gateway: *"do we really need omniroute for
> these backend model? can't we just create one ourself from our own gateway and offer it?"* The
> answer was mostly yes, and the case for it was measured rather than argued: this gateway's
> discovery is **live** and OmniRoute's is a **shipped snapshot dated 2026-08-18**, so OmniRoute was
> still offering models Google had retired and refusing ones Google had shipped — while this gateway
> had all of them (`gemini-3.5/3.6/3.7/3.8-flash` were enabled here and unknown there).
>
> A pool is a **provider with no credential** whose models are its pools. That is the whole trick:
> `is_configured()` is true for a provider with no required fields and `models_for()` already reads
> `static_models`, so pools reach the catalogue, the enable toggle and `/v1/models` through code that
> already existed — `catalog.py` needed **no change at all**. The trying-in-order lives in
> `adapters/pool.py`, so the chat route needed no change either.
>
> **The rule is asymmetric on purpose: only an un-retryable `400` stops the walk.** Every other
> failure — 401, 402, 403, 404, 429, 5xx, timeout, connection error, retryable 400 — advances.
> Advancing when we should have stopped costs a few round-trips and the caller still gets the same
> error; stopping when we should have advanced costs the job. All four provider-side status families
> are asserted in `tests/test_pools.py`, and the stop rule is proved by a mutant (remove the rule and
> the test fails, which it did).
>
> Members are **gateway ids** (`provider::model`), resolved through `catalog.resolve` — so a member
> the owner has since disabled is skipped for exactly the reason it would be refused on its own, and
> `pool::pool-name` nesting is refused when written rather than detected per request.
>
> `GET/POST/PATCH/DELETE /admin/api/pools`, plus a **Pools** tab on the Providers page that lists
> every member with whether it still resolves. **Edits are live — no restart**, and the route says
> `restart_required: false` rather than implying one out of symmetry: a pool is read from its file on
> every catalogue build, unlike a custom provider which is read at import.
>
> `tests/test_pools.py` (30) and `tests/test_pools_view.js` (8). Gate: **577 e2e checks**, all passing.

> **0.2.73 — providers the owner adds himself, and OmniRoute.**
>
> **The file was never the missing part.** `providers.local.json` has been read at import since
> custom providers were supported, and `/admin/api/providers` already publishes
> `skipped_custom_providers`. Nothing could **write** it — so adding a provider meant finding a file
> in Application Support and getting the schema right by hand. The owner: *"i think we can allow an
> add custom option. that would make future providers really easy until we can update native
> support."*
>
> `GET/POST/DELETE /admin/api/custom-providers` plus a **+ Add a provider** form on the Providers
> page. Every refusal the *loader* would apply silently is applied up front with the reason, because
> a skipped entry simply never appears and reads as "my file did not load" rather than "nothing can
> run that kind": unknown kind (naming the kinds that work), a built-in id, the reserved
> `studio-hub` namespace, the location prefix with a non-`studio_hub` kind, a non-URL base, a bad id,
> and a duplicate.
>
> ⚠ **A new provider needs a restart, and both the route and the page say so.** Definitions are
> loaded at import — deliberate, so the catalogue cannot change shape under an in-flight job. The
> route answers `restart_required: true` and the form prints it; a form that looked like it had
> added a working provider and left it invisible would be a control that does nothing.
>
> **Two bugs in my own first cut, both caught by the suite:** `admin.py` signals refusals with its
> own `AdminError`, not FastAPI's `HTTPException` (which is not imported there), and `fleet` is one
> package up (`.fleet`), not two.
>
> **OmniRoute** is a provider: `http://127.0.0.1:20128/v1`, `openai_compatible`,
> `DISCOVERY_OPENAI_MODELS`. `[MEASURED]` against his running instance: `POST /v1/chat/completions`
> with `model: "auto"` and **no key** answers **200** with a real completion, while
> `GET /v1/models` answers **401 `invalid_api_key`** — so the key is required by *listing*, which is
> what discovery does. Keys come from `http://127.0.0.1:20128/dashboard/api-keys`. `127.0.0.1`, not
> `localhost`: a name that resolves to `::1` first would miss an IPv4-only bind.
>
> 20 new tests in `tests/test_custom_providers.py`. Every `tests/test_*.js` suite and the focused
> Python pass green.
>
> **0.2.72 — the Providers page is four tabs.** The owner: *"inside the gateway kh's providers,
> there's so many stuff that i have to scroll. can you make it like fleet which it is split and have
> separate tabs for different stuff?"* Twenty-three provider cards, the concurrency settings, the
> live queue and the measured numbers were stacked in one column, so reaching any one meant scrolling
> past the other three — the concurrency block had already been moved to the top once for exactly
> that reason, which is a fix that does not scale.
>
> `PROVIDER_TABS` is one list: the strip is drawn from it and `selectProviderTab` shows exactly one
> pane, so **a pane no tab names, or a tab that opens nothing, is not expressible**. Pane elements
> are named `provider-pane-<key>` from the same keys.
>
> ⚠ **`#queue` changed mechanism, and the test that guarded it was updated rather than deleted.**
> It used to `scrollIntoView` the queue section, which only worked while every section shared one
> column; behind a tab that scrolls a hidden element. It now calls `selectProviderTab('queue')` from
> `setView` — still **after** the view is shown, which is the guarantee the original assertion
> existed for. `tests/test_queue_view.js` asserts the tab selection and asserts the scroll is **gone**,
> so the old mechanism cannot creep back.
>
> Four assertions in `tests/test_models_browser.js`, one **proved to fail** with the "hide the
> others" loop removed (a stale pane staying on screen).
>
> **0.2.71 — Ollama Cloud is a provider, and it needs no local install.** The owner went looking
> for an API on ollama.com, found a download button, and asked whether running Ollama on his Mac was
> the only way to reach the free cloud models. **It used to be; it is not any more.** Ollama's own
> docs say it outright: *"No Ollama installation or local server is required."* `https://ollama.com/v1`
> is an OpenAI-compatible endpoint reached with a key from `ollama.com/settings/keys`.
>
> `[MEASURED]` `GET https://ollama.com/v1/models` → **HTTP 200**, OpenAI-shaped, **no key needed to
> list**, **17 models** (`glm-5.3`, `kimi-k3`, `gpt-oss:120b`, `minimax-m3`, `mistral-large-3:675b`,
> `deepseek-v4-pro`, `nemotron-3-*`, …). With a key, `discovery.discover()` returns all 17; with none
> it returns **0**, which is the correct gate — a provider whose models cannot run is not offered.
>
> **The whole change is data.** `KIND_OPENAI_COMPATIBLE` is already in `IMPLEMENTED_KINDS` and already
> carries chat, so the entry is a `ProviderDefinition` reusing `DISCOVERY_OPENAI_MODELS` — the same
> shape as the `openrouter-main` entry beside it. No adapter, no discovery strategy, no route.
>
> ⚠ **Free here is not unlimited, and the plan name should not be left to imply it.** Ollama's free
> tier is *"starter usage credits"* over *"starter models"* with **one concurrent request**, then
> pay-as-you-go — not a standing free allowance like the `google-ai-free` lane next door. Running
> models on the owner's own hardware stays unlimited, which is the Studio Hub lane's job. Told to him
> in those words rather than as "free models".
>
> **Not added, and deliberately:** a *local* Ollama provider (`http://localhost:11434/v1`, no key for
> his own models). That is a second local runtime standing beside Studio Hub, which is a different
> question from the one he asked, and it is his to decide — the cloud lane does not depend on it.
>
> **0.2.70 — the Models page can re-discover, and says how old its list is.** The owner, after
> enabling an OpenRouter model that never reached the Chat picker: *"i just enabled it in model tab
> but it's not refreshed or shown in the chat tab. do i have to click sth or restart the app? i
> prefer a refresh button in the model's tab."*
>
> **Restarting would not have helped, and that is the part worth keeping.** Nothing re-discovers
> cloud providers at startup or on a timer, so a relaunch re-reads the same `models_cache.json`. The
> console's only re-discover control was on the **Providers** tab; the page that actually lists
> models offered no way to ask for a newer list and no hint that its inventory could be old.
> Measured on this Mac: `openai-direct` and its neighbours were last fetched **2026-09-23 09:39**
> while `openrouter-main` was re-fetched 2026-09-29 12:47 — a **6.16-day** inventory age, and the
> model he enabled had been released ~12 h after the previous OpenRouter snapshot.
>
> `[MEASURED]` The Chat tab was **not** withholding it: `GET /admin/api/create/models` returned
> `openrouter-main::stealth/space-bunny-alpha` in `by_modality.chat` at the time of writing. So for
> a model already in the catalogue the whole fix is a sidebar switch — `select()` calls
> `activated()` unconditionally — or the Chat lane's existing *Refresh models* button. Nothing on
> the Chat side changed.
>
> Two changes, both on the Models page:
>
> * **`Re-discover models`** beside *Clear filters*, calling the **same** `POST /admin/api/refresh-all`
>   the Providers page uses, so the two cannot disagree about what refreshing means. It is a separate
>   function only because this page must also redraw itself — `refreshAll` reloads providers and
>   status, which is all the Providers page shows.
> * **The inventory age**, beside the count. It reads `/admin/api/catalog`'s `freshness` rather than
>   being added to the models route on purpose: that route runs on **every keystroke of the filter
>   box** and reads only the provider registry, so answering the age from there would pull a full
>   catalogue rebuild into it. The age changes on discovery, not on filtering.
>
> **Read `inventoryAgeMs` / `inventoryOldestProvider`; never `stale`** — that flag is false by
> construction (see the freshness bullet in this repo's `AGENTS.md`), so a badge wired to it would
> never light. Past a day the age is marked ⚠, because that is when a model can be missing from the
> list for a reason the operator cannot see. A `null` age says "unknown", not "fresh" — verified by
> test, including the case where the secondary read fails and must not take the model list down.
>
> `[MEASURED]` `node --test tests/test_models_browser.js` → **23 pass** (6 new, and the button test
> was **proved to fail** with the button removed); every other `tests/test_*.js` suite green; the
> focused `tests/test_*.py` pass green. `tests/test_models_grouping.py` needed `_Credentials.is_configured`
> added to its stub **only while the age was briefly on the models route**; that change was reverted
> with the design.
>
> **The fleet-hardening batch.** The work order is
> `_orchestra/JOB-gateway-studiohub-fleet-hardening-20260928.md` (Batch A → B → C) and the
> evidence is `_orchestra/AUDIT-gateway-studiohub-20260928.md`. **From this repo: A1 (0.2.61),
> A5 + A6 + A7 (0.2.62), and B1 + B3 + C1–C5 + C7 (0.2.63).** A2, A3, A4 and A8 are Hub-side and
> ship from `studio-hub-kh`; the Hub's own half of B1/B4/C6 is there too. **What is still open is
> named at the end of this file** — read that section before assuming anything is finished.

## WHERE THINGS STAND

| Thing | Value |
|---|---|
| Current release | **0.2.69** — `repository/VERSION`, one line, the only place to bump. **This row read `0.2.63` for four releases while the header three lines above said the file was written against `0.2.67`** — a handoff contradicting itself in its own table. What 0.2.64–0.2.67 add: the rehearsal's four findings (id spelling, handover, re-point), the What's New **page above Settings**, and the four owner answers that had not been built — Q7 a controller unreachable for 7 days is **paused, never removed**; Q8 an unknown-outcome hold **auto-closes when the Hub's record settles** (404 → the work never existed → **requeue**); Q6 the video lane's deadline on the queue view; C3 `provider_submit_concurrency` **deleted** because nothing read it. See the dated sections below |
| Declared Studio Hub | **0.6.83** — `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION`, published on `/health`; a controller on another version is reported, never refused. Set on 2026-09-27 from a **running** Hub (`/api/version` → `0.6.83` build `f072f17`), not a changelog; 0.6.83 is the release whose video lane publishes the `audio` switch this gateway relays `[MEASURED]`. 0.6.81 was declared earlier the same day from `0.6.81` build `3d8ce32` — that one publishes `lane_contract.canvases`. Controllers older than both still publish, through the cross-product fallback in `_video_row`, and a lane that publishes no switch simply carries no `controls` key. **Note for the 0.6.97 Hub: this declaration is a floor, not a match.** The Hub has moved 0.6.83 → **0.6.97** and every route the gateway reads is still answered; a mismatch here is reported, not refused, which is the design |
| Branch | `main` = `origin/main`, clean tree; remote `github.com/theng12/gateway-kh` |
| Running build | `/Applications/gateway-kh.app`, port **8787**, pid **65061**, stamp **`9a7f455-202609281524`** — the 0.2.69 commit, read off `/health` `[MEASURED 2026-09-28]`. **This row is a snapshot, not a fact about the release**: every restart changes the pid and the stamp, so re-measure it rather than trusting it. It has already been stale once — it named `01c5992-202609281329` on pid 28544 within the hour. **Rebuild ordering:** `menubar/build.sh` writes `<short-sha>-<UTC>` from `git HEAD` at build time, so a build that runs before its own commit stamps the **parent** — measured here on 0.2.68 (`01c5992` against a pushed `01f1eac`) and again on 0.2.69. **Commit, then build, then push.** `pkill -f` remains forbidden `[MEASURED 2026-09-26]` |
| Distribution | DMG in `repository/dist/`, named by version alone; the previous ones moved to `_trash/dist/` |
| SSD/setup kit | `repository/dist/setup-kit/`, **pinned to GatewayKH 0.2.44** and refreshed onto all four `KH-STUDIO-*` drives on **2026-09-26**, `shasum -a 256 -c` OK on every one. The drives also carry `Studio Hub.app` **0.6.70**. Superseded kits are in each drive's `_trash/superseded-kit-2026-09-26/` (`GatewayKH-0.2.27`, `GatewayKH-0.2.43`) |

## The §9 fleet rehearsal — where the receipts are, and what it found

`qualification/S9-fleet-rehearsal.md` is the acceptance record: **five real Hub processes** (the
real `components.hub.standalone.app`, each with its own data dir, token and loopback port, auth
enforced) plus one isolated Gateway, driven over HTTP by
`_orchestra/rehearsal/run_rehearsal.py`. Each Mac runs a stub that answers the Hub's real worker
contract, so the Hub, the catalogue, routing, admission, the queue, the retry policy and the
membership routes are all the real thing.

**Eleven of the twelve checks pass with measured readings**, and the three that this release fixes
were found by it rather than by reading the code:

| # | check | result | the reading that matters |
|---|---|---|---|
| 1 | a node joins and both UIs show it | PASS | controller roster 3/3; gateway `node_summary {total: 3, reachable: 3}` |
| 2 | one model row, "N of M machines", one `/v1/models` row | PASS | 2 locations, **one** row; one enable action → 2 of 2 enabled |
| 3 | a site of M Macs accepts M concurrent jobs | PASS | M=4: admission limit 4, peak concurrent 4 |
| 4 | a node leaves and a removed node stays removed | PASS | leave 200 → `removed: 1`; re-join **403** with the date sentence |
| 5 | the location is re-pointed and keeps routing + enablement | PASS | same row id, model still 2 of 2, job routed there succeeded |
| 6 | a failed Mac is re-run on another Mac | PASS | succeeded on a different machine after the hand-off |
| 7 | queue position, its bound, and `/health` concurrency | PASS | positions [1,2,3] of [3,3,3]; published **30 == live fleet 30** |
| 8 | promotion + handover with **zero touches per node** | PASS | `verified 1/1`, followers with **0 touches**, enablement survived the re-point |
| 9 | eviction is permanent, the neighbour still works | PASS | own credential 401; Fleet-token join 403 by name; neighbour ran the work; readmit re-joined |
| 10 | one cluster down does not hurt the others | PASS | down row still shows `node_summary {total: 1}` with `members_from` and `stale 41.6 s` |
| 11 | published capacity is the fleet's, live | PASS | **36 → 18 → 36** with no settings edited |
| 12 | three attempts on three machines, no duplicates | **fixed in 0.2.65** | the unknown-outcome half passes over HTTP (a Mac killed mid-poll is a hold with `retryable: false`, never re-run). The three-attempt half exposed the id-spelling defect this release corrects — see the receipt's addendum |

**Check 12 failed, and it was right to.** The three-attempt half stopped at `attempts_used: 2`
with `attempts_allowed: 3` even with three locations enabled, and chasing it found **the same
id-namespace defect A7 exists to close, inside A7's own new code** — the attempt log stores provider
ids (`studio-hub-<controller>`) while `fleet.route` compares bare controller ids. Fixed in **0.2.65**,
pinned by three tests, and re-measured on the live code path (the addendum at the end of the
receipt). The receipt is left as measured rather than rewritten: the run that failed is the evidence
that the step earns its place.

## What 0.2.68 changes — this file was contradicting itself, and two rows were four releases stale

**No code change. This is the handoff corrected, and the corrections are the record.**

The header, three lines above the table, said *"Written 2026-09-28 against **VERSION `0.2.67`**"* —
while the table's own **Current release** row said **0.2.63**, and the **Running build** row
described the **0.2.60** commit at stamp `7d87724-202609270548`. A reader who trusted the table
would have measured this Mac against four releases ago and concluded the fleet work was unshipped.

Both rows are now measured rather than remembered `[MEASURED 2026-09-28]`:

- **Current release `0.2.67`**, with what 0.2.64–0.2.67 actually added, so the row stops describing
  0.2.63 as current while the version file says otherwise.
- **Running build `/Applications/gateway-kh.app`, port 8787, pid 90800, stamp
  `01c5992-202609280823`** — read off `/health`, not carried forward. It publishes 23 providers (22
  enabled), 1824 models of which **91 publishable and 19 withheld**, and **10 video rows**.

**And the reason this matters beyond bookkeeping.** The same sweep checked what this gateway offers
in the video lane, because the Studio Hub's own handoff claimed its video lane was entirely unbuilt.
This gateway publishes **`studio-hub::baa-ai/LTX-2.3-22B-RAM-12GB-MLX`** — `modality: video`,
`operation: video.generation`, `available: true`, `activation_blockers: []`, modes
`image-to-video` and `text-to-video`, priced at zero — beside nine cloud video lanes. So the local
video lane reaches Story Studio today, and **two handoffs were each describing it as missing.** The
Hub's side is corrected there; the row here now records what this side publishes.

**`jobs_processed: 0` on `/health` is a restarted process, not a stalled worker.** `worker_alive:
true` and `consecutive_failures: 0` beside it say so. Worth stating because a reader scanning for a
fault finds that zero first.

## What 0.2.67 changes — the four answers that had not been built

Before the fleet rollout, the job's own list was re-read against the code and **four items the owner
had answered were still open**. They are closed here, and the lesson is worth keeping: three of them
were *answers*, not findings, and an answer nobody implements looks exactly like a finished item.

**Q7 · A controller unreachable for 7 days is paused, never removed.** `fleet.note_reachability`
stamps `unreachable_since` on a change of reachability — persisted in `fleet.json`, because "a week
down" is a question about the calendar and a gateway restarted yesterday would otherwise reset every
controller's clock. Past the threshold the row gains `paused_at` and `pause_reason`; routing skips it
(`_takes_customer_jobs`) and **nothing is deleted**: the credential and every per-model enable
decision stay exactly as they are, so switching a house back on costs one resume.
`POST /api/fleet/controllers/{id}/resume` is that action, and the threshold is
`locations.pause_unreachable_after_days` in the owner's own settings file.

**Q8 · A hold is closed when the Hub's own record settles it.** `worker.reconcile_holds` asks the
Hub about each hold's batch, on a 60-second timer riding the dispatch loop, and the three answers are
kept strictly apart:

| The Hub's answer | What happens | Why it is safe |
|---|---|---|
| the batch is **terminal** | the hold is closed as failed, recording the Hub's state and its error | nothing is running, so the hold protects nothing |
| **404 — never heard of it** | the job goes back in the queue if it has attempts left, else fails cleanly | the work does not exist; this is the *same proof* the restart sweep acts on |
| **no answer, or still running** | **left exactly where it is** | "I could not ask" is not "it is finished", and a re-run could duplicate a render |

New `adapters.studio_hub.batch_snapshot` is the one-shot read (the poll loop walks to a terminal
state; this asks once), and it distinguishes `known: False` from `None` for exactly the reason in
that third row. `store.holds()` and `store.close_hold()` are new; the sweep's own counts are on
`/health` under `worker.hold_sweep`. Only Studio Hub is asked — it is the lane with a batch record —
and every other lane keeps the operator's discard action.

**C3 · `provider_submit_concurrency` is deleted, not enforced.** The literal `1` on every TTS lane,
described by its own `ponytail:` note as invented, and read by **nothing** in either repo. C3 said
"enforced or deleted", and deleting is the answer that does not require making a number up: a lane's
real concurrency comes from the location's published capacity and the operator's cap, both already
enforced. Same defect as `available_workers` (M11), same fix.

**Q6 · The lane deadline is on the queue view.** He asked for the video lane's deadline to be
visible, and the reason is the one a waiting operator actually has — *why is my chat job behind
that?* Each waiting job now carries `lane_deadline_seconds` from its model's own
`lane_contract.deadline_seconds`, each lane row carries the **longest** among its jobs, and a lane
that states none publishes `null` rather than a guess.

## What 0.2.66 changes — What's New is a page, above Settings

The owner asked for Studio Hub's shape: *"i want the what's new just like in the studio hub for the
gateway because right now it's still a modal and i want it moved up just above the setting tab just
like in studio hub too."*

**What's New is a sidebar row now**, under the *App* heading and directly **above Settings** — the
same place Studio Hub keeps it, because the two apps are read side by side. It was an `NSWindow`
opened from a small link at the bottom of the sidebar and from the Overview button; every door
(the row, the View menu, the Overview button, the app menu) now lands on the same page, so there is
one place the notes live and no second surface to keep in step. The bottom-of-sidebar link is gone:
with the row visible it was the same destination twice in one column.

**The renderer moved out of the window it used to live in.** `menubar/WhatsNewPage.swift` now owns
`makePage`, `notes()`, `appVersion`, `NotesDocumentView` and `NotesMarkdown`; `WhatsNewWindow.swift`
keeps the panel for a headless launch and routes `show()` to the page. That split is not
housekeeping: a renderer inside a window cannot be compiled or measured without the window, and the
one thing worth proving about this page is that the markdown becomes **type** — headings, bullets,
bold leads — rather than literal `##` and `**` in the reader's face, which is the defect the page was
first rebuilt for. `tests/test_whats_new_page.py` compiles the renderer on its own and walks the
resulting view for exactly that.

**The notes themselves say what changed for the person using the app.** The first entry still
described the old window, so it was rewritten, and the fleet work shipped today now has entries a
reader can use: members on the Fleet page, a queue you can look at, the number Story is told being
the fleet's own, one tick enabling a model for every Mac, image geometry at the size the Macs really
render, and the retry policy in plain words. `qualification/whats-new-page-0.2.66.png` is the page
rendered offscreen and looked at, which is how a UI change is verified here.

## What 0.2.65 changes — the id spelling A7 was written to fix, in A7's own code

**The §9 rehearsal caught the original defect inside its own fix, and this is that correction.**

A7 exists because the re-route compared a *provider* id (`studio-hub-9701220bf30f`) against a
*controller* id (`9701220bf30f`), so the exclusion set never matched and a busy Mac deferred work at
the same Mac (audit H5). The new attempt log stores the provider id — deliberately, because that is
what names a machine to an operator — and `fleet.route` compares `exclude_locations` against the bare
controller id. **The same mismatch, one layer along.**

Measured in the rehearsal with three locations verified enabled (`locations_total: 3`,
`locations_enabled: 3`, every location `enabled: true`, all reachable with `supply
{eligible: 1, available: 1}`): every job stopped at **`attempts_used: 2`** while publishing
**`attempts_allowed: 3`**, on two different machines. Driving the product's own
`fleet.routing_candidates`, the difference is visible directly:

```
excluded_by_provider_id  : ["02da…","6f14…","f64c…"]   <- asked to exclude f64c…, still returned
excluded_by_controller_id: ["02da…","6f14…"]            <- the same exclusion, correctly dropped
```

The lottery could therefore re-pick a site that had already failed; `_next_machine`'s
`chosen.id in tried` guard then rejected it and returned `None`, and the worker read that as
"nothing left to try" — failing a job at two attempts with no reason on the job and nothing in the
log to say why.

`worker._next_machine` now normalises **both sides** through `fleet.controller_id_from(provider_id)
or provider_id`, in one place, and applies the same function to the route's answer so the two cannot
drift. The attempt log still stores the readable provider id.

**Why the suite missed it and what now pins it.** The end-to-end test's fake router returned its
sites in order and ignored `exclude_locations` — so it never exercised the exclusion at all, and a
fake that ignores the thing under test cannot fail. `tests/test_job_retry_policy.py` now has three
tests for it: the exclusion set is asserted to be in the **bare controller-id spelling**, the fake
router **honours the exclusion the way the real one does**, and a router that hands back an
already-tried site is asserted to produce no repeat attempt.

## What 0.2.64 changes — what the rehearsal found

The §9 rehearsal ran the whole fleet end to end (two and three real Hubs and an isolated Gateway)
and four checks failed. Three were product defects; this release is their fix, plus the last audit
item that had been left open.

**A down cluster now still names its members.** The roster only ever came from the live probe, so a
controller that was switched off published `nodes: []` and `node_summary: null` — "this house is
offline with five Macs" and "this house has no Macs" read the same, which is exactly the
distinction the owner needs when a site loses power. The Gateway now remembers the last roster a
controller published (in-process; a display of what we last knew, not a second source of truth) and
publishes it **only** while that location is not answering, with `members_from` and
`members_stale_seconds` so a stale count is never mistaken for a live one. Nothing is invented for
a site never seen up, and an empty roster never overwrites a real one.

**A location can be re-pointed at a promoted controller, keeping its identity and its decisions.**
`PUT /admin/api/fleet/controllers/{id}/address` (audit A3 step 4). Adding the new head created a
**second** location — a promoted Mac has a different address *and* a different controller id — so
the first one's credential and every per-model enable decision were orphaned; the rehearsal measured
the replacement row publishing `model_id: null`, `locations_enabled: null`. The row id, and
therefore the provider id Story's presets and the operator's ticks are keyed on, is preserved. The
**site id is cleared rather than carried**, because it named the identity of the Mac that used to
answer there and reusing it for a different Mac is the shared-identity defect `_stable_identity`
exists to prevent.

**`H13` — the Gateway no longer sends a Fleet token to a public address.** `_validate_address`
accepted any `http(s)` host, so a mistyped or pasted public hostname received a location's Fleet
token in `X-Hub-Token` on the very next probe. A controller answers on the owner's own network —
loopback, LAN, link-local, or CGNAT (which is Tailscale) — and a **hostname** is accepted only if
every address it resolves to is private, so MagicDNS names keep working while a name that resolves
publicly is refused by name. Resolution happens once, at add or re-point time, never on the probe
path. The owner's live roster is Tailscale literals, so nothing of his changes.

**`/health` publishes the capacity Story is offered.** `paid_concurrency_limit` and
`concurrency_by_modality` live on `/v1/account/overview`, which is Story's surface; the same numbers
are now on the health read beside `concurrency_pool`, so "the fleet can do 24 while the pool says
60" is visible without knowing which endpoint to ask.

## What 0.2.63 changes — seeing the fleet, and the correctness underneath it

### B1 · Members on the Fleet page (audit H10, M20, M21)

The gateway **fetched** each controller's `nodes` and `node_summary` and never decoded them, so the
owner could not see who had joined a controller without opening Studio Hub on it. The menubar app
now decodes both: a **Members** column on Locations (`reachable / total`, plus `· N refused`), a
**Node** column and a **Node last seen** column on Machines, and a State cell that draws
**`credential refused`** for an `unauthorized` node instead of the machine's activity word
`offline`. That state also raises a **`node_credential_rejected`** issue carrying the Hub's own
sentence, and suppresses the `machine_offline` row for the same Mac — one fault, one row.

Every decoded field is optional on purpose. Measured live: a controller on Hub 0.6.65 publishes
node rows with `id`/`name`/`state`/`role`/`address` and **no** `last_seen_at`, so the column draws
`—` rather than a zero, and a controller that states no summary draws `—` rather than `0 / 0`.

### B3 · Queue visibility (audit H8, H9)

`api.py` published `queue_position`/`queue_eta_seconds`/`queue_reason` **only when `defer_reason`
was set**, so a waiting job looked identical to a running one; and when it was set, the position
counted only *deferred* jobs and ignored the fresh ones dispatched ahead of it. Now **every**
`accepted` job publishes its position and a new `queue_size`, where "ahead" means another job in
state `accepted`, in the same modality, created earlier (ties by id) — deferred or not.

New **`GET /admin/api/queue`**: waiting jobs with provider, model, modality, age, position, why it
is waiting, and *attempt n of 3* with the last failure; plus the live `worker._in_flight`,
`worker.budgets` — **written "so the console can say what is bounding a lane" and read by nothing
until now** — the per-site admission budgets, the pool, and the queue wait settings. One console
view over it, under Concurrency on the Providers page (`#queue` lands there).

### C1 · Fairness and survival (audit M3, M4, M6)

- **`due()` is ordered by `created_at`, not `next_action_at`.** Deferral pushes the readiness clock
  forward, so a job that had waited kept losing its place to work that arrived later. A queue where
  waiting costs you your position is not a queue. `next_action_at` still decides *whether* a job is
  ready; `created_at` (ties by id, so the order is total) decides who goes first.
- **The deferral budget survives a long render.** Eight deferrals capped at a minute is about four
  minutes, and the live settings file did not override it, so the shipped default was the ceiling in
  practice: a job waiting on a busy site was **failed after ~4 minutes**. It is now 240 deferrals
  capped at five minutes — about twenty hours — which is the owner's decision C verbatim (*"hold,
  don't fail, for a temporarily absent cluster … this replaces the audit's four-minute deferral
  budget"*). The **retry policy is what keeps this honest**: A7 declares a job failed after three
  real machines have tried it, so this bounds "nothing can take it yet" and A7 bounds "everything
  tried it and failed".
- **The dispatcher cannot die silently.** The loop guarded `due()` and nothing else, so one raise
  from `_admit`/`mark_running`/the thread spawn ended dispatch for ever while `/health` still said
  ok. The whole pass is wrapped, and `worker.liveness()` publishes `worker_alive`, `last_pass_at`
  and `consecutive_failures`.

### C2 · Restart recovery (audit M5, M7)

- **A job that never left this process goes back in the queue.** The startup sweep failed
  *everything* `running` as an unknown-outcome hold; six of the live holds were work that had never
  been dispatched (`upstream_job_id: NULL`). The line is drawn at `dispatch_started_at`: before the
  adapter was called there is no window in which a request could have reached a worker, so
  re-queueing is provably safe. **A dispatched job is still a hold and is never replayed** — an
  upstream id proves a container was created, and *no* id is not proof that none was.
- **A Mac that dies mid-poll is a named hold.** `_poll_batch` had no `try/except`, so an
  `httpx.HTTPError` became a durable hold whose detail was the exception's repr — no batch, item or
  location. It now raises a settled-coded hold naming all three, with the recovery context the
  discard action needs.

### C3 · Capacity is derived, never configured (the owner's decision D)

`paid_concurrency_limit` was the **settings pool** — live `60` while the fleet could do about `4`.
It is now `concurrency.live_capacity()`: the sum, over **reachable** locations, of the very budget
admission enforces (`hub_slot_reading` + `hub_budget`, the same two functions `Worker._provider_budget`
calls), per modality and in total. `Live on this Mac: 24` — four reachable locations × six
modalities × one Mac. A Mac that is sold leaves the number the next time Story asks; nothing is
cached beyond the fleet's own five-second snapshot.

The **pool is still published beside it** as `concurrency_pool`, and the per-modality breakdown as
`concurrency_by_modality`, so a reader who sees the two disagree can tell which is which rather
than discovering it by counting.

**`acquire_permit` now reserves against that number** instead of returning an id and
`expires_at: null` for ever: it answers `429` + `Retry-After` ("never 402" — a 402 locks the
caller's key permanently) when the fleet has no headroom, and otherwise issues a permit that
**expires**. **`max_queued_jobs` is enforced** at the one path every generation route funnels
through, with the same `429` + `Retry-After`, and its shipped default is raised from 32 to 256 —
53 Macs running multi-hour video jobs make 32 a busy afternoon.

### C4 · Merged ceilings and one invented number (audit M10, M11)

`_merge_capabilities` returned immediately for anything that was not TTS, so a merged row kept the
**first location's** token window, output ceiling, prompt ceiling and upload cap: a Hub-side limit
change on the second site never reached Story, and the row could advertise a window one of the
machines would refuse. Every published ceiling now takes the **smallest** value, with its
`*_source` and `*_source_detail` travelling beside it, for every modality. `available_workers: 1` —
the literal, invented, published on every transcription lane for a model the fleet reported on
sixteen machines — is **deleted**; the real concurrency comes from the location's own capacity.

### C5 · The Hub's own verdict, and quantisation (audit M13, M14)

The Hub states `type: "dependency"` for a package that exists only to serve another model, and the
gateway read it and dropped it — so the guard that keeps tokenizers and codecs out of the picker
rested entirely on a *second* HTTP call succeeding, which is the failure `models.py` records as
having happened once already. The withhold rule now consults **either** source (the package list's
own row or the catalogue's fact).

**Every one of the 30 manifests now states its quantisation**, with the sentence saying where it
came from — `"MLX 4-bit"` from *"the publisher's model id states it: mlx-community/chatterbox-4bit"*,
or `null` plus the reason when the publisher names no precision. It is carried through discovery and
published on the row as `quantization` + `quantization_source`, against the owner's standing rule
that a model named without its quantisation cannot be checked against what he has actually run.
`packaging/capture_model_manifests.py` preserves both keys across a re-capture, and the frozen
digests in `tests/test_model_manifest_catalog.py` were refreshed in the same change (the manifest
content genuinely changed).

### C7 · Documentation truths (audit M17, M22)

The Hub's `docs/gatewaykh-handoff-0.6.63.md` documented a **two-step** join and omitted
`POST /api/hub/fleet/adopt`, so a scripted enrolment produced Macs the controller calls
`unauthorized`. It now states all three steps, what the join returns, and what the node must keep.
The Hub README's "rotate independently" claim now names the route that does it. And
`fleet-deployment-notes.md` gained the removal caveat: **a removal is an eviction, and it makes that
Mac's finished jobs unretrievable through the controller (503)** — fetch anything you need first.

## What 0.2.62 changes — three fleet items: what the fleet offers, what it renders, and what happens when a machine fails

### A5 · Models enabled once, for the fleet (audit H1, H2)

Controllers added on the Fleet page arrived with `auto_enable_discovered=False`, so each remote Mac
contributed **5, 5, 2, 2, 2** of its 19 publishable models and only 5 of the 13 Hub rows had more
than one location: the operator ticked a model once and believed the fleet offered it. A newly
added location now **inherits this gateway's own decisions for the same model ids** — enabled
there ⇒ enabled here, disabled there ⇒ recorded disabled, a model the local Hub has never seen
gets no entry and stays off — and the add result says so in real counts. A re-add of an existing
location is never seeded, so per-Mac choices survive a token or address change.

The console's Models page no longer draws one row per Mac (108 rows for 21 models). Each model is
drawn **once**, with a **Machines** column reading *"N of M machines"* and a per-Mac sub-row; the
row's own tick enables or disables that model on every machine that has it. New additive fields on
`/admin/api/models`: `is_location`, `locations[]`, `locations_enabled`, `locations_total`,
`provider_ids`; on the add result: `controller.created` and `enable_seed{sentence}`. Nothing was
renamed or removed — Story Studio and the SwiftUI app read this API.

### A6 · The geometry the Macs actually render (audit H3, H4)

Story Studio was published the **cloud** table: 16:9 → 1536×1024, 4:3 → 1536×1024. The Macs render
16:9 → **1280×720** and 4:3 → **1152×864**, and the Hub's own lane contract accepts **13** ratios
including `16:9 small` = **1024×576**, the video-valid frame the Image studio added on purpose.
Four of five published sizes were wrong by 23–33% linearly and 8 of 13 accepted ratios were hidden
— Story composed at 1536×1024 and received a 1280×720 file.

For a `studio-hub` model that carries a `lane_contract`, `aspect_ratios` and `output_options` are
now built from **the Hub's own contract and preset table** (`HUB_IMAGE_SIZE_BY_RATIO`), read
through the existing lane-contract reader. `16:9 → 1280×720`, `16:9 small → 1024×576`,
`4:3 → 1152×864`, `1:1 → 1024×1024`, and so on. A ratio the Hub states with no size publishes in
`aspect_ratios` and gets **no** option row (Story renders `width.toLocaleString()`, so a sizeless
option is a crash, not a gap). The old table is the fallback only when the Hub states nothing, and
**non-`studio-hub` providers are byte-identical**.

`available` was a hardcoded `true`, so a model whose every Mac was down still published as
available and Story rendered the outage as *"Locked for this GenStudio account."* `available` is
now `not locked`, and the outage publishes its own sentence:
*"No Mac offering this model is reachable — every Studio Hub location that offers it is offline or
not taking customer work."* Fixing this exposed a fourth inconsistency: unlocking a merged row left
the outage sentence in `activation_blockers`, so a row could read `locked: false` beside an outage
blocker. `_publish_lock` now reconciles it.

`gateway/providers/discovery.py` also carries the Hub's `image_sizes` into the model row, so the
catalogue prefers the Hub's live table over the copy taken from the 0.6.91 bundle the moment a
preset changes.

### A7 · Three attempts, different machines, never a duplicate (audit H5, H6; owner's decision E)

The re-route was a no-op: the exclusion set was built from `provider_id`
(`studio-hub-9701220bf30f`) and compared against `location_id` (`9701220bf30f`), so a busy Mac
deferred the job **at the same Mac** while an idle one offered the model — 39 live `MACHINE_BUSY`
failures (all **chat** jobs, 2026-09-26, on the synchronous peer route: no Hub batch exists for them, so
they are not batch-level rows; they are the deferral budget running out against one busy location) — and a Mac that failed *after* the batch started was never re-routed at all
(`ffmpeg not found on PATH`, `[METAL] Command buffer execution failed`).

The policy, in full, is **[docs/job-retry-policy.md](../job-retry-policy.md)**, referenced from
`fleet-deployment-notes.md`. In short: attempt 1 on the routed machine; on any **known** failure,
attempt 2 on a different machine; attempt 3 on another never tried; after the third, the job is
declared failed to Story Studio with a terminal reason naming every machine and why each failed.
`retry.max_attempts` (default **3**) is live-editable beside the pool size.

**No duplicates, absolutely.** The re-route is guarded by `if not unknown and pinned is None:` —
an upstream that accepted the work and went silent is an unknown outcome, becomes a reconciliation
hold, and is never re-run automatically. A pinned voice is excluded (its reference clip lives on
one controller). A job with nowhere else to go **waits** rather than spending an attempt on a
machine that was never going to run it.

The exclusion is now a **column** (`attempted_locations`, `attempt_log`), not a local set: the old
`tried` set lived inside one dispatch call, so a job that deferred and came back — or was
redelivered after a restart — could be sent to the machine that had just failed it. Every retried
job publishes `attempts[]`, `attempts_used`, `attempts_allowed` and `attempt_policy` on its
envelope; a job on its first machine carries no `attempts` key rather than an empty list.

Also in this release: routing skips a controller that has **handed its group over** — the Hub
publishes `capacity.accepting_customer_jobs: false` and `fleet._takes_customer_jobs` now reads it,
so a retired Mac stops receiving work in the window before the operator removes it (A3 step 4).

## What 0.2.61 changes — admission is a capacity, not a remainder

**A1 of the fleet batch, and it is the one that decides whether a 53-Mac rollout can use its
Macs at all.** The Hub publishes, per operation, how many of its Macs are *free*
(`available_physical_machine_slots`). Admission used that free count as its ceiling and then
refused when this gateway's own in-flight jobs reached it — so with **M** Macs and **T** jobs
already dispatched, admission needed `T < M − T`. **At most half of every site could ever be
busy**, and the arithmetic got worse the busier the fleet got. It was invisible only because
the live fleet has one reachable Mac per site.

**The fix is one sum, bounded by the Hub's own total:**

```python
budget = min(free + in_flight, eligible_physical_machine_slots_total)
budget = min(budget, stated_provider_limit)      # the operator's cap still throttles
budget = 0                                        # when the location is not reachable
```

The Hub's free count is *already* net of the Macs this gateway is using, so adding its own
in-flight count back puts those Macs back in the number instead of charging the site twice.
With M=6 and four running: free=2, in_flight=4, budget=6 — the site takes two more.

**The sum is only taken when the Hub states its total.** A Hub that publishes free slots and no
eligible total cannot tell "three free of three" from "three free of thirty", so its free count
stands as-is. That is the conservative branch, not the fix.

**Two more defects in the same subtraction**, both closed here:

- **M1 — worker services versus physical machines.** One Mac publishes `available_worker_slots: 2`
  for `audio.transcription` while routing ranks it by `available_physical_machine_slots: 1`
  `[MEASURED on this Mac, live]`. Admission believed the service count, so it scheduled two jobs
  onto one Mac and the second came back `MACHINE_BUSY` — a live failure with 39 occurrences.
  `concurrency.hub_slot_reading` is now the **single reader** for both admission and the console,
  and it prefers the physical count.
- **M2 — an offline location still got a budget of 1.** Two unreachable rows each computed 1, so
  every pass spent a global slot and a connection timeout before failing. A location with
  `reachable: false` now gets **0**, which is what routing already assumed.

**The provider counter is now per site-and-modality.** It was per site, so a site running one
image job refused a chat job while a second, free Mac sat there — the owner's *"distribute them
properly"* complaint reproduced inside the fix. `Worker._per_lane` holds `(provider, modality)`
counts; a missing reading is never read as an offline location (`reachable is not False`), because
"no answer" is not "no work".

**One table, one copy.** `HUB_OPERATION_BY_MODALITY` lived in both `fleet.py` and `worker.py`, and
the two had drifted — the fleet copy was **missing `chat`**, so routing fell through to the
site-wide total for a chat job while admission read `chat.completion`. Since the two numbers are
now compared with each other, `fleet.HUB_OPERATION_BY_MODALITY` is the single definition and
`worker` imports it.

**Published, so the arithmetic is checkable from outside.** Every location row on
`GET /admin/api/fleet` now carries:

```json
"admission": {"lanes": {"image": {"limit": 6, "source": "hub",
  "operation": "image.text_to_image", "hub_free": 2, "hub_total": 6,
  "hub_unit": "physical_machine", "in_flight": 4, "detail": "…"}}}
```

The Hub's own reading sits beside the number admission computed from it. A reader who can only
see `available_worker_slots` cannot tell a site at half capacity from one that is genuinely full,
which is exactly how this survived four audits.

**Verified by:** `tests/test_worker_concurrency.py::AdmissionIsACapacityNotASecondSubtractionTest`
— a live fake site whose free count falls as jobs start and rises as they finish, held at M=1, 3
and 6; the M1 reading; the unreachable case; the operator's cap still binding; and the module's
standing *"a limit of zero never stalls the queue"* property. Plus
`tests/test_fleet_routing.py::TheFleetReadPublishesTheAdmissionBudgetTest` for the published
receipt.

## What 0.2.60 changes — the lane's switch reaches the caller, and the caller's choice reaches the lane

**The owner asked for one thing and it needed both halves:** *"for the video generations … can that
be a button to select/deselect? also make it available in story studio too as an option."* Studio Hub
0.6.83 gave its LTX lane an `audio` switch and published it on `lane_contract.parameters`; Story
Studio reads the catalogue **through this gateway**, so a switch this gateway does not publish is a
switch Story Studio cannot offer, and a value this gateway does not forward is a switch that changes
nothing. Both halves are here.

**Published: `controls` on a local video row.** `catalog._local_video_controls` relays the lane's
**boolean** parameters — name, type, `default`, and the lane's own `note` — and nothing else. The
rest of a lane's parameter list is the vocabulary the row already states better (`modes`,
`aspect_ratios`, `variants`), and restating it would be a second copy free to disagree with the
variants a caller is billed for. Measured live against the running Hub 0.6.83 `[MEASURED]`:

```json
[{"name": "audio", "type": "boolean", "required": false, "default": true,
  "note": "the model generates picture and sound together, so this does not change generation
           time; unticking it skips the audio decode and writes a silent MP4. The track can be
           added back in an editor."}]
```

**The note is why relaying matters.** The lane, not this gateway, knows that unticking `audio` skips
a *decode* rather than a denoise step; a caller that renders the switch without that sentence implies
a saving the lane does not make. It travels verbatim and is shown verbatim.

**Forwarded: `audio` in `_HUB_VIDEO_PARAMS`.** The Hub reads `params.audio` on a video item. Only
what the caller set travels — the loop's `is not None` test — so an unstated choice leaves the lane's
own default in force rather than letting this gateway decide it. `false` is the value that must
survive, which is why the wire test asserts a *silent* request specifically.

**Three states kept distinct, deliberately:** a switch (relayed), a lane that publishes none (no
`controls` key at all — not an empty list), and an entry this gateway cannot read (dropped alone,
with a `null` default rather than a coerced one). A model withheld over an unreadable extra field
would lose a working picker row to a decoration.

**Not in this release:** Story Studio's half — the switch in Motion Review, the request field, and
the idempotency digest that has to include it — lands in its own cycle (1.4.231).

## What 0.2.59 changes — WIRE-3: the fields Story read that nothing published

**The audit's `WIRE-3` is now closed, with a decision recorded for every one of the twenty-five
fields.** Story Studio's `GenStudioChatModel` carried twenty-five fields this catalogue never
published. Three of them were a *bug*, not a gap, and the other twenty-two have no source anywhere
on this Mac — which is a fact worth writing down rather than leaving as a standing suspicion.

**The bug: a feature that could not run.** `routes/vision.ts` gated the thumbnail-reference vision
picker on `max_input_images >= 1` and a non-empty `supported_detail_levels`. **No gateway publishes
either**, and Story's parse turns their absence into `0` and `[]` — so every model failed the gate,
the vision model list was always empty, and the one fact that decides it (`supports_vision: true`,
published from the provider's own `architecture.input_modalities`) sat unread beside them. Fixed on
Story's side in **1.4.230**: eligibility rests on what is published, the per-model limits are
enforced when stated, and the menu is told whose choice each number is
(`detail_source` / `reference_limits_source`).

**The bug this release fixes: a fact fetched and thrown away.** `_openai_models` kept a fixed
whitelist of provider keys:

```python
raw={k: v for k, v in entry.items() if k in {"id", "name", "owned_by", "context_length", ...}}
```

It had already been patched twice by adding one more key — `top_provider` for the real output
allowance, `maximum_text_length_per_request` for the TTS limit — and each patch left the next
provider fact needing a discovery change before anyone could read it. OpenRouter publishes
`expiration_date` on a model it is retiring, and the whitelist discarded it **at fetch time**, which
is why the retirement family read `None` for every model and Story Studio's retirement surface (a
model-selector badge and `genstudio-model-retirement.ts`) could never fire.

`raw` is now `dict(entry)` — the whole entry. A `/models` response is public metadata, so nothing is
being withheld on a model's behalf; what the catalogue *publishes* stays explicit, key by key.

**Published from that:**
- `retires_at`, `deprecation_warning`, `deprecation_warning_at`, `scheduled_retirement_at` — from
  `expiration_date` and every spelling beside it, with **`retirement_source: "provider"`**.
  `upstream_shutdown_at` and `replacement_model_id` stay `null`: nothing states them, and a
  `deprecation_warning: false` on a model nobody has said anything about means "unstated", not
  "will not retire".
- **`activation_blockers`** on every row — the gateway's own reasons a row cannot be used: the lock
  reason, and a location's own reason for a package it cannot place. It was already in the row as
  `lock_reason`; nothing published it, so Story's operator-facing sentence was its generic fallback
  while the real reason sat one field away. An empty list means usable.

**The decision table for the other twenty-two** (nothing states them; nothing is invented):

| Field group | Decision |
|---|---|
| `availability_reason`, `available_again_at` | **Deleted in Story 1.4.230.** They described a temporary absence the old GenStudio published; the fact that replaced them is a *job's* retry window (`next_action_at`, `defer_reason`), which rides on the job and is already rendered in the Jobs window. Story's cooldown still works from its own measurement (`noteLlmLineupQuotaExhaustion`). |
| `discount`, `discount_disclaimer` | **No source.** Nothing in this gateway has promotional pricing; a `false` would imply a mechanism that does not exist. Story treats absence as "no discount", which is honest. |
| `max_input_images`, `accepted_image_mime_types`, `max_image_bytes`, `max_image_dimension`, `supported_detail_levels` | **No source** — no provider states per-image limits. Story enforces what is published when it is, and its own limits otherwise, labelling which (`reference_limits_source`). |
| `citation_format`, `supports_domain_filters`, `research_qualified`, `supports_claim_attribution`, `web_search_credits_per_search`, `web_research_credits_per_request`, `max_web_searches` | **No source** beyond `supports_web_search` / `returns_citations`, which are published. Research pricing would have to be curated before it could be published, and a curated number must be labelled as the owner's, not the model's. |
| `max_structured_items_per_request` | **No source.** Story already handles its absence (STORY-10) — the reader-side fix stands. |
| `relaxed_queue_hint` | The shelf (`queue_class`) is published; the hint is the old GenStudio's scheduling prose. No source. |

**Evidence.** `tests/test_wire_fields.py` (8 tests): a stated expiry becomes the retirement family
with its source; every spelling is read; nothing stated publishes nothing retired; the six names
stay in Story's vocabulary; `activation_blockers` carries the lock reason; and the discovery
whitelist cannot come back. Every focused suite green, **e2e 577/577**.

## What 0.2.58 changes — What's New is a page, and there is a way to look at it

**The window dumped the notes file raw.** `WhatsNewWindow` handed `whats-new.md` to one `NSTextView`
and set `systemFont(ofSize: 12.5)`, so the reader saw literal `##`, `**bold**` and backticks at one
size, with `Build …` glued to the first line. The owner's complaint was exact: *"i had this ugly
modal and bad font arrangement"*, next to Studio Hub's What's New page.

- **The same page furniture as Studio Hub.** `PageKit.label` + a header row of *What's New* (26pt
  semibold), the bundle's version in monospaced digits beside it, and this build's stamp pushed
  right in tertiary grey; then a rule, then the notes as blocks. `NotesMarkdown` carries the same
  block rules as Studio Hub's `MarkdownText` (headings by level, bullets with a hanging indent,
  paragraphs joined, `---` → a real separator), so a note reads the same in both apps.
- **The inline parser is hand-rolled, and the reason is the deployment target.** Studio Hub uses
  Foundation's `AttributedString(markdown:)`; that needs macOS 12 and this app runs on **11.0**
  (`MACOS_MIN`). Raising the floor for one window would trade a supported Mac for a renderer, so the
  three span kinds the notes actually contain are parsed here — audited: no tables, block quotes,
  numbered or nested lists, links, code fences, strikethrough or double-backtick spans. It is
  recursive, so `**bold with `code` inside**` keeps both, and an unclosed marker is plain text.
- **Two other defects in the same window.** macOS merged it into the Overview window as a **tab**
  (`tabbingMode` is now `.disallowed` — that is what made it look like a modal bolted on), and a
  programmatic `NSWindow` is released when closed while this class keeps a reference, so the second
  open could have messaged a dead window (`isReleasedWhenClosed = false`).
- **`menubar/tools/whats-new-preview/main.swift` — a visual change can now be looked at without
  fronting the app.** It compiles the *real* `WhatsNewWindow` against `PageKit`, renders the page
  offscreen at a given size and appearance, and writes a PNG; `--window` calls `show()` itself and
  reports the window it created. Not compiled into the app (`build.sh` globs `menubar/*.swift`).

  ```sh
  # Offscreen render — only the renderer and PageKit, no window, nothing on screen.
  swiftc -target arm64-apple-macos11.0 -o /tmp/whats-new-preview \
      menubar/tools/whats-new-preview/main.swift menubar/WhatsNewPage.swift menubar/PageKit.swift
  /tmp/whats-new-preview menubar/whats-new.md /tmp/notes.png 720 640 dark 2400   # 2400 = scroll offset
  /tmp/whats-new-preview menubar/whats-new.md /tmp/notes-narrow.png 470 640 dark

  # The window half needs the whole app (it reaches MainWindow), so it is behind a flag.
  swiftc -target arm64-apple-macos11.0 -D PREVIEW_WINDOW -o /tmp/whats-new-preview-window \
      menubar/tools/whats-new-preview/main.swift menubar/*.swift
  /tmp/whats-new-preview-window --window
  ```

  **Corrected 2026-09-28 — the command above did not compile.** It named `WhatsNewWindow.swift` in
  the offscreen build and no `WhatsNewPage.swift`, so `WhatsNewWindow.appVersion` and
  `.makePage` no longer existed (both moved to `WhatsNewPage` when the renderer was split out in
  0.2.66), and `WhatsNewWindow.swift` itself reaches `MainWindow`, which the offscreen build does not
  have. **The one harness for *looking* at a visual change had been unusable since that split**, which
  is why the notes' own rendering was not checked when the version headings were added — see below.
  `--window` is now behind `-D PREVIEW_WINDOW` and needs `menubar/*.swift`.

  Verified with it: dark and light, 720pt and 470pt wide (wrapping holds, no horizontal scroller),
  a scrolled section of bullets, and `show()` twice → **one** window, reused, `tabbing=disallowed`,
  720x672. `menubar/whats-new.md` carries a section describing this change, so the new window's
  first entry is the one that explains it.

## What 0.2.57 changes — a video variant names the shape it has

**The lane's two parameter enums were never a set of canvases, and this catalogue built their cross
product.** `_local_video_shape` read `parameters.aspect_ratio.enum` and `parameters.resolution.enum`
and `_video_row` emitted one variant per `(mode, duration, resolution)` with `aspect_ratios` taken
from the ratio enum — so for Studio Hub 0.6.81's video lane the gateway published `12` variants
across two classes while only `4` pairs exist, and Story Studio's picker offered `448p + 16:9`.

- **Root cause, measured on the worker side:** the LTX pack's two-stage pipeline floors each output
  dimension to a multiple of 64 (`snap_output_dimensions(..., two_stage=True)` in the pinned
  runtime). The lane used to publish `288p 16:9 = 512x288`, which the pack returns as **512x256**
  (2:1) while the job's acceptance still said `512x288`. Studio Hub 0.6.81 replaced that table with
  snap-stable pairs (`256p`: `7:4`, `4:7`; `448p`: `1:1`, `11:7`) and published them as
  `lane_contract.canvases` — every pair with its exact pixels and the durations that canvas accepts.
- **This release reads them.** `catalog._local_video_canvases` reads `canvases` (strictly: a
  malformed list makes the whole row undescribable, because a partially-described variant is worse
  than none), `_video_row` builds one variant per published pair per accepted duration, each
  carrying its own `aspect_ratio`, and `aspect_ratios` is the set the canvases actually carry rather
  than the enum's union.
- **A worker older than 0.6.81 still publishes.** With no `canvases` key the old cross product is
  built exactly as before — every one of those rows was published before this release, and a caller
  that picks a pair the lane refuses is told so by the lane. The branch is marked `ponytail:` with
  its removal condition.
- `tests/mock_upstream.py`'s video lane now publishes real canvases (including one that accepts only
  the shorter duration, so a per-canvas duration list is exercised rather than assumed);
  `tests/test_hub_lane_contract.py` and `tests/test_e2e.py` pin the published variants as a set of
  `(duration, resolution, aspect_ratio)` triples and assert the set is a subset of the published
  pairs. **`tests/test_e2e.py`: all 577 checks pass.**

## What 0.2.56 changes — the lane ceilings have a control

**The owner asked for a UI for the per-lane concurrency ceilings; the API had carried them since
the pool was split and the page never drew one.** `concurrency.settings_view` had returned
`lanes.values` (whatever was in the settings file) and nothing that a page could iterate, so a
lane cap could only be set by editing `provider-concurrency.local.json` by hand.

- **The route now passes the lane vocabulary in** (`admin.get_concurrency` →
  `concurrency.settings_view(..., lane_names=catalog.MODALITIES)`). One source of truth, no second
  list inside `concurrency.py`: a lane the gateway serves cannot be missing from the page, and a
  lane it does not serve cannot appear on it. Settings keys the caller did not name are still
  listed, so a cap set by hand survives a vocabulary change.
- **`settings_view` publishes `lanes.rows`** — one row per lane with the number in force, its
  source and the sentence explaining it, exactly like a provider row, plus the `stated` value so a
  blank box means "not set" rather than "1".
- **The console draws a "Lanes" table above the provider tabs**, with an input per lane. The save
  payload carries `lanes` (already supported by `save_settings`), and the inputs are read from the
  document like every other row, so switching provider tabs cannot drop an unsaved lane cap.
- `tests/test_concurrency_tabs.js` gains three checks — every lane has a control and its unset
  state is readable, an empty lane list still renders the table, and the lane inputs survive a
  provider-tab switch. **9/9.**

**Verified:** e2e **575/575**, `test_worker_concurrency.py` 45/45, console suites 9/9.

## What 0.2.55 changes — the reference travels where the Hub reads it

**Found by the first real chain job, and it is the kind of defect only a live chain finds.**
`0.2.54` sent an image-to-video request's `reference_images` at the **item's top level**. Studio
Hub builds an item from exactly three keys — `prompt`, `seed` and `params` — and then merges
`shared_params` with `item["params"]` before popping `reference_images` off the resulting *body*
(`broker._submit_batch_locked`). An item-level reference is dropped without a word, the broker takes
its JSON door instead of its multipart one, and the worker receives a prompt-only request: measured
2026-09-26, the first chain job generated **text-to-video from a request that had uploaded a source
image**. The reference now travels inside `item["params"]`, and
`tests/test_studio_hub_contract.py::VideoLaneTests` pins both halves — the reference is in `params`,
and nothing item-level claims to be one.

The same release carries the **tier gate asserted at the public surface**: the e2e mock now offers a
second local video model whose Hub verdict is `memory_eligible: false` (the same field a real 16 GB
controller publishes for the 22B model), and the suite asserts it is **not** in
`/v1/videos/models`. Recorded check count: **575/575**.

## What 0.2.54 changes — the local video lane is published

**Studio Hub 0.6.76 shipped a real Video worker, so this gateway now has a local video lane to
carry.** Until today `video` was the one modality where every published row was a cloud one: the
catalogue's `MODALITIES`, the `/v1/videos/models` route, the console's Create lanes and
`fleet._HUB_OPERATION_BY_MODALITY` all knew the modality, and `adapters/__init__.py`'s kind table
was the single switch that withheld every local row. That switch is on now, and four things follow
from it:

- **The lane's own vocabulary is relayed, not guessed.** A Studio Hub video row's `modes`,
  `aspect_ratios` and `variants` come from the model's `lane_contract` — the worker's own published
  answer — instead of the flat 5s/10s@720p ladder the cloud path uses. `_local_video_shape` reads
  it, and `withhold_reason` refuses to publish a local video model whose contract cannot describe
  what it accepts: a video row the gateway cannot describe is one Story Studio would offer a
  control it cannot honour.
- **The tier is the Hub's own verdict, read.** The worker publishes `memory_eligible: false` on a
  machine below the model's qualified tier (LTX-2.3 22B is 24 GB, Lance 3B is 16 GB), and
  `withhold_reason` already withholds on exactly that field. Asserted both ways in
  `tests/test_hub_lane_contract.py::VideoPublicationTests`.
- **Both of the adapter's video doors work.** Text-to-video goes through the existing
  `_generate_image_or_voice` path (`modality: "video"`, `_HUB_VIDEO_PARAMS`); image-to-video sends
  Story's uploaded scene as an inline-base64 `reference_images` entry, which is the only one of the
  Hub's three reference forms that crosses two systems — the Hub has never seen this gateway's
  asset ids, and a URL would make the Hub fetch back from here. A `reference_asset_id` that has
  aged out is a named caller error, never a silent text-to-video job.
- **`POST /v1/videos/source-images` exists**, because Story Studio's image-to-video flow uploads the
  exact scene first and submits with the returned id. The bytes are held in
  `gateway/video_assets.py` for 15 minutes and base64'd into the job at submit.

**One thing this release deliberately does not do: publish Lance 3B.** The Hub offers the package,
with `dispatch_state: ineligible` because its runner is not adopted and its files are not installed,
so the existing gates withhold it and the reason is the Hub's. When the `lance-mlx` runner lands,
the same row publishes with no gateway change.

**Verified:** e2e **574/574**, the 38 focused suites green, and `test_hub_lane_contract.py`
20/20 including the new video publication and tier tests.

## What 0.2.52/0.2.53 change

Two things, both measured live on 2026-09-26.

**A Hub batch id is a container, not a promise that work is pending.** Studio Hub creates a batch and
only *then* validates the item, so a refused or failed item leaves an upstream id on the job — and
`worker.py` treated a recorded id as ambiguity. Every ordinary item failure therefore became a durable
`unknown_outcome` hold: *"reconcile the provider request before retrying; the gateway will not resubmit
it automatically."* Reproduced twice: a **422 `Choose a qualified 1K aspect ratio`** (a caller's
geometry mistake) and a **409 `local is reserved for other work on this Mac`** (a busy machine). On a
fleet of ~50 small Macs, every engine OOM would land in the operator's recovery queue for work that
never ran.

`BackendError` gains **`upstream_settled`**, which an adapter sets only after polling its own upstream
identifier to a terminal answer. The worker lets it outrank **every** heuristic — including the
`dispatch_started_at` one, which also fired on this case and defeated the first attempt at the fix. The
paths that are genuinely ambiguous do not set it and keep their hold: an item the Hub calls `uncertain`,
a poll that hits its deadline, and a failure **fetching an artifact for an item that succeeded**. All
six are pinned in `tests/test_hub_item_settles.py` and `tests/test_job_recovery.py`.

**The gateway has had a queue since 2026-09-25 and published nothing about it.** `store.defer()` puts a
job back with a `next_action_at` and a reason when every location offering the model is merely busy; the
envelope hardcoded `queue_position`, `queue_eta_seconds` and `queue_reason` to `null`, so a queued job
and a slow one were identical to the caller. Story reads all three — its contract types `queue_reason`
as exactly `provider_pace | null`, and its Jobs page renders *"Waiting in the GenStudio provider
queue… position N… about 2 minutes… pacing new requests"* — so this fills **Story's** vocabulary rather
than inventing a second one. `queue_position` counts waiting jobs of the same modality (1 is next);
`queue_eta_seconds` is **when this gateway will try again**, never a claim about when the provider will
finish; a job that is not waiting publishes nothing at all.

The two `ponytail:` constants that bounded the wait — eight deferrals, backoff capped at a minute,
about four minutes in total — are now settings beside the pool size (`queue.max_deferrals`,
`queue.defer_cap_seconds`), read live with the same defaults. That ceiling is why a fleet busy for
longer failed work it would eventually have run, which is the owner's own requirement inverted.

**0.2.53 answers the other half of the queue: showing it.** Publishing the three fields was only
useful where something draws them, and the surface the owner actually watches is the menu-bar app's
Jobs window — which showed *"Waiting for a free machine — <reason>"* and nothing about **how far back**
the job was or **when** it would be tried again. `/admin/api/jobs` now carries `queue_position` (a real
rank among the waiting jobs of the same modality, and only when the job *is* waiting) and `retry_in_s`
(seconds to this gateway's next attempt — never a prediction about the provider), and the Jobs window
renders them as *"Waiting for a free machine — position 2, trying again in 45s"*. Each part is drawn
only when present, so a queue the gateway cannot count does not read as *position zero*. The console's
recent-jobs list carries a `waiting #N` badge for the same reason.

**One caveat worth keeping.** `store.defer()` and its wait have still never fired in live traffic —
zero jobs with a `defer_reason` since 2026-09-25. Everything above is therefore verified by tests and
by construction, not by a queue the owner has seen fill. The first real busy-fleet refusal will be the
first live evidence, and `tests/test_queue_visibility.py` plus `tests/test_job_deferral.py` are what
stand in for it until then.

**Verified live after the rebuild**, on the very request that used to hold: `recovery_required: false`,
`recovery_state: null`, and the batch id still recorded for the operator.

- **Versions are patch-only for now:** `0.2.1`, `0.2.2`, … up to `0.2.1000`. Increment the
  third number by one per shipped change; never roll to `0.3.x`/`1.0.0` until the owner says so.
- **Every release's notes carry its version and date, and the gate enforces it.** Add a
  `## <VERSION> — <YYYY-MM-DD>` heading at the **top** of `menubar/whats-new.md` with the release's
  entries under it as `###`. `tests/test_whats_new_page.py` fails if the newest section is not the
  current `VERSION`, so bumping without writing notes is caught rather than shipped. Two things this
  prevents, both measured on 2026-09-28: a page where a fix you had been waiting for looks exactly
  like one that was always there, and **a release whose notes were never written at all** —
  `0.2.67`'s four user-facing changes had no entry anywhere, and a flat list has no place where a
  missing release is absent from. Also: **the notes are prose for a renderer.** `#`, `- `, `---` and
  paragraphs are implemented, with bold, italic and code **inline**; `>` blockquotes, `*`/`+`
  bullets, ordered lists, tables and fences are **not**, and are printed at the reader as their own
  marker. The same test rejects them. Look at the rendered page — `/tmp/whats-new-preview` below —
  before believing a note reads well.
- **Finished work is committed and pushed, and a release carries a DMG.** Do not call work
  done while uncommitted, unpushed, or without an installable artifact. `dist/` stays out of Git.
- **The setup kit is a snapshot, not a build output — and it is current as of 0.2.44.**
  `repository/dist/setup-kit/` pins GatewayKH **0.2.44** (`installers/gateway-kh-0.2.44.dmg`,
  sha256 `1ea5c796fd826ecd…` in `SHA256SUMS.txt`) and its `06-troubleshooting.md` gained the
  two sections this release needs: **"A node shows offline"** (the Hub's `no_hub` /
  `unreachable` / `unauthorized`, read from the installed Hub's own `peers.py` and
  `fleet_nodes.py`) and **"A job says Manual recovery required"** (discard vs close).
  Rebuild it deliberately for a rollout; never hand-edit a copy on a drive.
- **The four SSDs were last refreshed from it on 2026-09-26 at 0.2.44** `[MEASURED]` (and on
  2026-09-24, 2026-09-25 and twice on 2026-09-26 before that, at 0.2.22 → 0.2.27 → 0.2.43):
  `/Volumes/KH-STUDIO-{ATLAS,NOVA,ORICO,UGREEN}/Video Generation Kit/GatewayKH/`, each
  verified with `shasum -a 256 -c SHA256SUMS.txt` and each keeping the superseded kit in
  `<drive>/_trash/superseded-kit-2026-09-24/` (the only place a superseded installer lives;
  the repo's `dist/gateway-kh-*.dmg` is the build output, not an archive).
- **Two kit indexes exist on each drive and both name every app's version** — the top-level
  `Video Generation Kit/README.md` and the copy beside the Hub kit, `Studio Hub KH/README.md`.
  They had drifted (top said Studio Hub 0.6.54 and GatewayKH 0.2.21; the Hub copy said
  GatewayKH 0.2.2 and Studio Hub 0.6.57, while the bundle in the folder reported **0.6.60**).
  Both now read the version each bundle actually reports. **Update both on every kit refresh**,
  or the drive's front page lies about one of the three apps.
- The port is **8787** everywhere that matters: the default in `gateway/config.py`
  and the app's `AppConfig`, this Mac's `.env`/`menubar.json`, and `README.md`'s
  "Why port 8787". It was 8000 until the owner moved it (2026-09-21, `Project
  Status.md` §10h: 8000 is the most contested dev port and Story Studio's own
  default). Every Story Studio client points at `http://<host>:8787`.

## WHAT 0.2.51 CHANGED — the two kinds of provider are separate tabs

The owner, looking at the page: *"please make this into tabs for each function it's too spread
out… and also why is studio hub showing as several providers?"*

**The second question is the reason for the first, and both have the same answer.** A Studio Hub
location **is** a separate provider — six controllers, each with its own fleet of Macs, each sized
on its own — but the page drew eighteen cloud vendors and six controllers in **one list under one
column heading**, where the two are governed by **opposite** rules: a cloud limit is a number the
owner chooses, while a location's is a **cap on what the site reports for itself**. So the same
column meant two different things, and you had to read each row's sentence to know which.

- **`scope` is now on every concurrency row** (`cloud` | `local`), from the same
  `registry.describe_all()` the rest of this console reads. The derivation itself stays in one
  place — `credentials.py` reads it off the provider's **kind**, `studio_hub` being the only local
  one — and the admin route passes it in rather than `settings_view` re-deriving it.
- **The page draws two tabs, with counts**: *Cloud APIs* and *Your fleet*. The pool box stays above
  them, because it is scope-independent.
- **An unclassified provider gets its own tab, never Cloud.** `scope: null` means the caller did
  not say, and folding it into Cloud would explain its limit by a rule that does not apply to it.
- **A row the page cannot classify is worth looking at**, so that tab's hint says so rather than
  inviting a number.

**The one design decision that is not cosmetic: a tab HIDES rows, it does not re-render them.**
`concurrencyPayload()` reads the inputs that are in the document, so a switch that re-rendered would
drop the other tab's unsaved number — type a cloud limit, glance at the fleet tab, save, and it is
gone. Every row is in the HTML at all times and the inactive ones carry `display:none`.
`tests/test_concurrency_tabs.js` fails if that changes: **proved 3 of 6 on a simulated re-render.**

**Also fixed: a sentence this session's own work made false.** The page's hint read *"A Studio Hub
location sizes itself from the free workers it reports and **is not set here**."* That was true
until 0.2.49 made the control a real cap; it now says a number here caps the site and can never
raise it. A page whose explanation contradicts its behaviour is worse than a page with no
explanation.

- **Verified:** `tests/test_e2e.py` **570/570** · all **36** focused suites · all **5** console
  (Node VM) suites, including the new one.

## WHAT 0.2.50 CHANGED — the declared Studio Hub is 0.6.72 (DECIDE-1)

One line, and it took the owner to say yes: `SUPPORTED_STUDIO_HUB_VERSION` moves **`0.6.65` →
`0.6.72`**.

**The claim is earned, not assumed.** This file's own rule is that a bump means *measured against*
rather than *available* — it was deliberately **not** raised to 0.6.64 when that build was only held
by controllers and running nowhere. Everything this session did ran against a **0.6.72** Hub: the
fleet reads, the `model_limits` relay, `source_kind` end to end, the stall fields, and the whole
`0.6.71` / `0.6.72` release cycle. It is installed on this Mac and answering.

**Expect every other controller to now read `older` on the Fleet page — that is the point.** Four sit
at 0.6.65 and one at 0.6.64, and the page saying so is a true and useful prompt to roll them forward.

**It is still never a gate.** Those controllers keep working exactly as they did; nothing branches on
this number. That is the owner's 2026-09-24 decision — report drift, keep working — and it is why the
constant is a label rather than a check.

## WHAT 0.2.49 CHANGED — a Studio Hub location's number is a cap, and now it is read

The owner, planning for ~50 Macs: *"it can't be just 12. It must be a UI editable number… i want the
concurrency for local to be able to util more."* He is right, and the box he means has existed since
0.2.37 — the Providers page renders **one row per provider with an editable limit**, plus a "Jobs
running at once" box for the pool. There is **no upper bound** in the code:

```python
def _positive_int(value): ...   # any positive integer; 50, 200, whatever the fleet is
```

**The defect was that the control lied for local locations.** `Worker._provider_budget` returned
`hub_budget(slots)` and **never consulted the settings file**, so a number typed against a Studio Hub
row described nothing. 0.2.47 fixed the *display* of that (the page stopped showing a fake `1`) and
said in the row that the control was not read. Making the control **real** is the better answer.

- **`hub_budget(slots, operation, stated)` now takes the smaller of the two.** The site's count is a
  live measurement of what it can physically take; the operator's number is policy. Policy may
  **throttle** a site below what it offers; it may **never over-schedule** one, because that is how a
  queue becomes a crash. So it is a `min`, not a substitution.
- **A tie and an absent side are both labelled honestly.** `stated` with no answer from the site
  publishes `curated`, not `hub` — a number the operator set must never be dressed as the site's.
- **The console shows what will run, not what was offered**, and says which of the two is binding —
  including, when the operator's number is the *looser* one, that raising it would change nothing.
  The first version of that sentence said "raise it to use the rest" whichever way the comparison
  fell: advice that does nothing, which is the same defect as the sentence this row was fixed for.
- **The pool can be set to any number**, and since 0.2.45 a change to it takes effect without a
  restart. It is now **60**, chosen so a fleet growing past today's nine online machines never meets
  it; the per-location budgets are what actually bound each site.
- **Guard:** `AHubLocationsNumberIsACapNotALicenceTest` (6) and
  `DispatchReadsTheOperatorsNumberForALocationTest` (3). **Proved to fail on the defect** — restoring
  the old call fails with `6 != 2`, the operator's number ignored.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) · all
  **36** focused suites.

**Still open, and named rather than implied:** the console renders the pool and the per-provider rows,
but **not the per-lane (per-modality) ceilings** — `settings_view` publishes `lanes` and the page
ignores it, so those have an API and no UI. Nothing needs them today (no lane cap is set), and the
next person to want "images may use at most N of the pool" should build the control rather than
assume it is there.

## WHAT 0.2.48 CHANGED — a Hub's number arrives with the Hub's own token (HUB-20)

`_chat_row` has published `*_source` beside every limit since 2026-09-24, precisely so Story Studio
can tell a model's own figure from somebody's fallback. For a **Studio Hub location** the value is
relayed from the Hub, and `_limit_with_source` answers `provider` for *any* stated value. So the live
row published:

```
max_output_tokens: 16384
max_output_tokens_source: "provider"
max_output_tokens_source_detail: "worker default: the checkpoint ships no generation_config.json"
```

**The token said the model stated it; the sentence directly beside it said the opposite.** Story Studio
reads the token. Its own row says what that costs — *"16,384 currently renders as if the model stated
it"* — and 16,384 is the Hub's `FALLBACK_MAX_OUTPUT_TOKENS`, the number a checkpoint that states
nothing is given. Two published fields disagreeing about one number, with the machine-readable one
wrong.

- **The Hub now states `source_kind`** (Studio Hub 0.6.72): `model` — the checkpoint's own file states
  it; `worker_default` — the Hub's own fallback; `curated` — the Hub's packaged metadata rather than
  the checkpoint's word; `unknown` — nothing states it, and the value travels as `null`.
- **This gateway relays it and never parses the sentence.** Matching a word in a sentence is what the
  Hub asked us not to do, and three of their defects came from exactly that.
- **A kind travels only beside a value the Hub supplied.** When the Hub states `null` this gateway
  publishes one of its **own** (nulling the field is a breaking change for the reader, which
  substitutes 8,192 and prints it as the model's), and the honest token for *that* number is
  `gateway` — not `unknown`, which would say "nobody stated this" while the row carries a number we
  chose.
- **An older Hub that states no token behaves exactly as before.** The change is additive, and the
  absent case is pinned so it cannot become a new guess.
- **Guard:** `tests/test_hub_limit_provenance.py`, 9 tests, **proved to fail on the defect (9 of 9,
  `'provider' != 'worker_default'`)**. `LIMIT_SOURCES`' comment now names the relayed vocabulary, and
  the test reads it, so the constant is load-bearing rather than decorative.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against the
  owner's real Hub · all **36** focused suites green.

**Also fixed, and found by this change rather than looked for.** `ThePublishedLimitIsTheRealOneTest`
read the **live** `provider-concurrency.local.json` instead of isolating it, so it passed only while
that file had no `global` key. Setting one — which is 0.2.45's entire point — made two of its tests
fail **on this Mac and nowhere else**: `global_budget()` consults the file *before* the environment, so
the operator's 12 beat the `GATEWAY_JOB_CONCURRENCY` the test had just set. A test that depends on the
operator's own configuration is not measuring the code. It now patches `SETTINGS_PATH`, and a new test
states the precedence explicitly so the rule is not rediscovered.

## WHAT 0.2.47 CHANGED — the console reports the concurrency that actually runs

`settings_view` drew **every** provider from `cloud_budget`, Studio Hub locations included. A
location is governed by its own Hub's live `available_worker_slots` — `Worker._provider_budget`
reads `capacity.by_operation[<operation>].available_worker_slots` and **never consults the settings
file** — so the console showed *"no limit set, so one at a time; set it on the Providers page"* for
sites running several at once.

**Measured live before the fix:** dispatch allowed **4** for `studio-hub-9701220bf30f` while the
console said **1** for it. Two defects in one row:

- the number did not describe the system — the same shape as the `paid_concurrency_limit` literal
  that once read 4 while the worker ran one job at a time; and
- **the advice does nothing.** A limit written on the Providers page is *not read* for a Hub
  location, so the page was inviting the owner to set a number that would never take effect.

- **`settings_view`** now asks the same question dispatch asks. A Hub row reports the site's own
  slots with `source: hub`, and **names the spread** when the lanes disagree — the page draws one row
  per provider while the Hub publishes one budget per operation, so a site whose image lane takes four
  and whose voice lane takes one is not described by either number alone. Both branches now end with
  **it is not a setting**, because the old sentence said the opposite.
- **Cloud providers are untouched**: they keep the settings-file answer they are governed by.
- Failure is swallowed on purpose — an unreadable fleet must not stop the page drawing.
- **Guard:** `tests/test_worker_concurrency.py::TheConsoleReportsWhatWillActuallyRunTest`, **8 tests
  today** (6 when this was written). **Proved to fail on the defect: 4 of the 6 there were**, with
  `1 != 4 : the widest lane is the headline number`.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` · all **35**
  focused suites.

**The fleet's real ceiling, measured while doing this — this is the number that matters for the
~50-Mac rollout.** The global cap is **4**. The six locations together publish **10 free image
slots**, and `9701220bf30f` alone declares 4 (its busiest lane 8, `audio.transcription`). So the
binding constraint is **`global`**, not any one site — and since 0.2.45 it can be raised from the
console without a restart. Nothing else in the dispatch path under-uses the fleet.

## WHAT 0.2.46 CHANGED — the mode we publish is one Story Studio can read

`_chat_row` distinguishes the two structured-output modes deliberately — `response_format` is
JSON-object mode, `structured_outputs` is JSON-schema mode — and it published the stronger one as
`"json_schema"`. Story Studio's union is `'none' | 'json_object' | 'request_json_schema'`
(`genstudio-client.ts:1708`) and its parser maps anything that is neither of those onto
`'json_object'` (`:4938-4944`). So a provider offering a native JSON schema was described to Story
Studio as the weaker `json_object`: **this gateway's own correct distinction, erased on the wire.**

**It costs behaviour, not a label.** `productResponseFormat` sends a provider-native `json_schema`
with `strict: true` **only** when the product publishes `request_json_schema` (`:6234`). Read as
`json_object`, the identical request falls through to a wrapper instruction that *describes* the
schema in prose and asks the model to comply — the shape of the incident this project already paid
for, where a model *"complied for two packs of forty scenes and wrote prose for the third."*

- **`json_schema` stays correct on the request side**, where it is the provider's own
  `response_format.type` (`adapters/studio_hub.py`, `adapters/openai_compat.py`). Only the
  *published* value moved. The two spellings are not interchangeable in either place.
- **Why the spelling is ours to change:** this gateway is Story Studio's only language surface, and
  its contract is that Story needs **zero code changes** to talk to it — so the published vocabulary
  is GenStudio's, not ours.
- **Guard:** `tests/test_story_model_vocabulary.py` — the four publishing routes, plus a sweep that
  no route through `_chat_row` can publish outside Story's union. **Proved to fail on the defect:**
  restoring `json_schema` fails 3 of its 7 tests, with `'json_schema' != 'request_json_schema'`.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` · all **35**
  focused suites green.

**Still open, and named here so it is not read as fixed.** Story Studio reads **25** chat fields this
gateway never publishes — `max_structured_items_per_request` among them. That is Hub row **HUB-21**:
the field is in no installed bundle and no gateway, so the producer change is Studio Hub's. And every
`*_source` provenance field the gateway *does* publish had, when this section was written, **no Story
Studio consumer at all** — that is Hub row **HUB-20**, whose producer half has since shipped (Studio
Hub **0.6.72** publishes `source_kind`, this gateway **0.2.48** relays it as
`max_output_tokens_source`), leaving the consumer half on Story's side. Neither is a vocabulary bug of
this kind; both need work on the Story side.

## WHAT 0.2.45 CHANGED — the global cap moves without a restart

`Worker.__init__` captured `concurrency.global_limit()` once into `self.pool_size`, and `_admit`
gated on that captured number. Lanes and providers were always read **per admission**; the global
cap was the one that was not. So raising `global` on the Providers page changed what
`/admin/api/settings/concurrency` reported and **nothing about how many jobs actually ran** — the
new number would not bite until the gateway restarted, which is exactly the restart `concurrency`
promises is unnecessary: *"a fix to a rate limit should take effect without a restart while a stat
per admitted job stays cheap."* The class docstring had named `concurrency.global_limit` as this
check since it was written; now the code does too.

- **`Worker._global_limit()`** resolves it per admission. An explicit `pool_size` stays a **pin** —
  the tests pass one, and so would any caller that means to hold a fixed size.
- **Why this is the fleet's throughput and not a tidy-up.** `DEFAULT_GLOBAL` is **4**. Measured on
  the running gateway before the change: `global: {limit: 4, source: gateway, detail: "nothing set
  it, so 4; edit it on Providers"}`. Four concurrent jobs is the whole fleet's ceiling, and until
  this release the console could not move it.
- **Guard:** `tests/test_worker_concurrency.py::TheGlobalLimitMovesWithoutARestartTest` — raising
  *and* lowering the setting against a **live Worker that is never restarted**, plus the pin. **Proved
  to fail on the old behaviour:** restoring the capture fails 2 of its 3 tests, and the failure
  message names the promise that was broken.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against
  the owner's real Hub · all **34** focused suites green · `test_ui.py` **not** run (release-gate
  only; it takes over the owner's screen).

**The related finding, and the one that is *not* a defect.** While measuring this, the RAM
admission path was re-read end to end. `memory_admission`'s total-memory floor is **deliberately
never in force** — *"no dispatch is refused for the size of the machine"* (`:140-142`), *"never
enforced"* (`:163`) — because FLUX.2 Klein 4B MLX 4-bit *"has completed thousands of generations on
the owner's 8 GB Apple-silicon nodes"* (`:33-36`). The **free-memory floor** is the live guard; the
published tier **ranks** (HUB-17) and **warns**. A small Mac being offered a model above its
published tier is intended, measured behaviour. Do not "fix" it.

## WHAT 0.2.44 CHANGED — the first consumer of the Hub's stall signal

Studio Hub added a progress signal to their job rows and offered it, 2026-09-26: *"it is
on the row now… nothing in our native app consumes it yet, so you would be first."*
This is that first consumer. It exists because of a number we asked for — the chat and
voice deadlines are **1,800 s**, and the Hub's argument for why thirty minutes is *safe*
rather than merely long is that a stall is visible. On this path, that something is us:
**Story Studio cannot reach a Hub.**

- **`HubProgress`** collects what the poll already returns — `/api/hub/jobs/{id}` carries
  `stalled`, `no_progress_s` and `stalled_after_s` — and the image, voice and
  transcription lanes publish it as `hub_progress` on the settled job. **No extra
  request**: the figures were already in the response we read.
- **A record, not a live signal.** A caller watching an unfinished job still sees
  `running` until it settles. The live half needs a write back to our own job row while
  the poll is in flight, and `BackendRequest` carries no job id — deliberately not built;
  see GW-13.
- **What silence must not be read as**, which is most of what the tests pin:
  * `no_progress_s: None` is the Hub saying *not applicable* — a queued or finished job
    has no progress to be missing — and folding it in as `0.0` would make every
    completed job look perfect and drag the worst case down with it;
  * a Hub that never states a figure publishes **`None`**, never a reassuring
    `stalled: false` invented out of our own silence;
  * **`stalled` latches** — one poll past the threshold is a fact about the run, and a
    later fresh poll must not erase it. The **worst** figure is kept, not the last.
- **The timeout message now says whether it had stalled.** A job that dies at 1,800 s
  having reported no progress for 1,700 of them reads as *"did not finish within 1800s;
  the Hub reported it stalled, with no progress for 1700s against a 900s threshold"*.
  That is the case the whole feature is for, and it is the one that **raises** — which is
  why `HubProgress` is passed in rather than returned.
- **Two stall vocabularies, and they are not the same field.** These are the
  *progress-based* ones (`broker.py`: threshold `max(15 min, 5 × average)`). The
  *silence-based* `stalled_seconds` (120 s, `stall.py`) is on `/api/hub/stats` →
  `fleet_activity`, a surface we do not poll for a job. Studio Hub's note described the
  second as being "on every job row"; on the row we poll it is **not there**, and a first
  consumer would have gone looking for it.

- **Proven live, not just unit-tested.** `[MEASURED 2026-09-26]` A voice job through
  the running gateway settled with
  `hub_progress: {"stalled": false, "worst_no_progress_s": 19.2, "stalled_after_s": 900,
  "samples": 22}` — and the Hub's own `/api/hub/jobs` reported that same batch climbing
  `no_progress_s` 4.4 → 19.2 while it was active. **22 of our polls carried a figure**,
  which is the field working rather than the plumbing merely compiling.
- **It covers image, voice and music, and NOT transcription — measured, not assumed.**
  `/api/hub/transcription/jobs/{id}` returns a different summary
  (`transcription_jobs.py::summary`) that carries **none** of `no_progress_s`,
  `stalled_after_s` or `stalled`. So a transcription job publishes `hub_progress: null`
  for ever, and that `null` means *"the Hub publishes no progress on this route"* — the
  honest answer, and **not** the same as the field being broken. Confirmed against a real
  transcription before and a real voice job after, on the same build.
  **Told to Studio Hub:** their offer described the signal as being on the job row
  generally; it is on the `/api/hub/jobs` family only.

`tests/test_hub_progress.py` → **11 checks**.

## WHAT 0.2.43 CHANGED — the record was blending prose with JSON, and the fleet gate had never run

Two things, and the first one is a correction to a number we published to both projects.

### The qualification record now says which kind of work it measured

`measured` published **one** median over every job, and the jobs are not all the same
kind of work. `[MEASURED 2026-09-26, from the live job store]` Of 48 settled chat jobs,
**31 asked for `json_object`** and 17 were prose — and the two measure differently:

| output shape | samples | median tokens/word | range |
|---|---|---|---|
| **text** (prose, a chapter) | 10 | **1.26** | 1.14–1.78 |
| **structured** (a JSON contract, a scene-prompt pack) | 30 | **3.15** | 1.39–25.23 |

Local model alone: prose **1.21**, structured **1.50**. **The 1.45 this gateway
published to Story Studio and to Studio Hub was a blend of the two and described
neither** — and it is the number a chunking threshold gets sized against. The single
sample we retracted earlier (1.22) was *closer to right for prose* than the better-sampled
blend that replaced it, which is a lesson about averaging across things that are not the
same.

- **`by_output_shape`** now sits on every row: `text` and `structured`, each with
  `samples`, `max_completion_tokens` and `median_tokens_per_second`. **Both keys are
  always present**, `samples: 0` when one has never run — a key that appears only once
  there is data is a key a reader has to guess at, and a missing shape split is exactly
  the state this replaced.
- **The headline figures stay whole-envelope, deliberately.** "Can this lane carry N
  tokens" is answered by the largest that ran whatever it was; splitting the headline
  would hide the case a ceiling decision is about.
- **The shape comes from the request**, read in SQL from `params.response_format`:
  `json_object` and `json_schema` are structured, **anything else including absent is
  prose** — a caller that asked for nothing asked for text.
- **The fleet merge carries the split**, or the fix would be right per location and
  wrong on the one row Story Studio reads.

`tests/test_qualification.py` → **38 checks** (5 new); `test_e2e.py` → 2 new.

### The fleet gate ran for the first time in fifteen releases — and the gateway was right

`test_fleet_standalone.py` had not run since 0.2.28 because it needs a live Hub and an
explicit opt-in. Run on 2026-09-26 against Hub **0.6.70**: **green — 47 checks in that
run**, including a real transcription end to end and the artifact returning through the
gateway. **The suite's total follows what the live Hub reports** (this checkout now has
**54 check sites**, some in mutually exclusive branches), and it prints the count it
actually executed; quote that, not this number.

**Three defects, all in the test, none in the product.** Worth recording because each
would have made the gate unrun or dishonest:

- **It could not find its own input.** The clip search accepted only `.mp3` under the
  artifacts dir and this gateway writes **`.wav`** for every TTS artifact — 4 `.wav`,
  0 `.mp3`, measured — so that half could never match and the gate stopped at section 3
  with `a real clip is available to transcribe — None`. It also took `next(glob(...))`,
  i.e. whatever the directory happened to order first, with a 298-second render sitting
  beside a 3-second one: **the gate's runtime was a lottery**. Now the smallest real
  clip, any audio type.
- **It named a model instead of reading the fixture** — `mlx-community/whisper-base-mlx`,
  which on this Hub is `not_installed`, so the gateway **correctly** withheld it as
  unservable. One absent model cascaded into **five** failures, and **three of them
  passed vacuously** because `all(...)` over an empty set is `True`. This is the defect
  the tracker already records for a hardcoded Hub version — *"a test is a row, and it
  lies the same way"* — and the fix is the same: derive it from what the Hub offers.
- **`the Hub refused every transcription model it offers:` printed with nothing after
  it**, because nothing had been submitted. A refusal and an empty list must not share a
  sentence.

## WHAT 0.2.42 CHANGED — the Hub moved its numbers, and we moved our mock first

Studio Hub raised `FALLBACK_MAX_OUTPUT_TOKENS` `4096 → 16384` and made the deadline
**per lane** (chat 1,800 s; image/voice/transcription/music 900 s; video 7,200 s),
replacing the single `PEER_PROXY_TIMEOUT_S = 900.0`. Their release is **not cut yet** —
working tree only, nothing installed, nothing on the drives, and **GW-5 cannot
re-discover until it is**. Until then the local lane is still 4,096 / 900 s.

- **They warned that their own suite lost eight chat tests to it** — several asserting
  the bare literal `4096`, several fixtures hand-tuned to sit *just above* the old
  fallback, which **silently inverted** at 16,384: the fixture became the binding
  constraint instead of the fallback. *"Any number of yours that is written as a
  literal, or sized relative to our fallback, is exposed to the same inversion."*
- **Answered empirically, not by inspection: our mock Hub was moved to the new values
  first and the whole suite run against it.** Two tests went red, both quoting old
  numbers rather than the fixture — `max_output_tokens == 4096` and
  `deadline_seconds == 600.0`. **The gateway code was correct in both cases.** Every
  other suite passed unchanged, e2e included.
- `tests/mock_upstream.py` now **names** `HUB_CHAT_MAX_OUTPUT_TOKENS`,
  `HUB_CHAT_CONTEXT_TOKENS`, `HUB_CHAT_DEADLINE_S`, `HUB_VOICE_DEADLINE_S`, and both
  fixtures and assertions use them — the next move is one line and no test can quote a
  stale number. That is the `AGENTS.md` rule (*"asserted a hand-written key list from
  the live Hub, not the fixture"*) applied to a constant instead of a key set.
- **The inversion is now impossible by construction**, with a test on the mechanism
  rather than the value: *"a model that states less than the fallback keeps its own
  number"* asserts whatever the Hub states reaches the row, so it stays true at 16,384,
  at 32,768, and at whatever they pick next.
- **Per-lane deadlines have a test too.** Two models, two deadlines, asked in one
  process, plus a repeat of the first to catch a cache — because the hazard they named
  is not reading the wrong field, it is reading it once and reusing it, which passes
  every single-lane test there is.
- **`JOB_DEADLINE_S` stays a flat 900, deliberately**, and its docstring now says why
  so nobody "fixes" it to the largest lane. It is reached only when a lane states
  nothing, and a Hub that states nothing is a Hub from *before* the change, whose one
  proxy timeout *was* 900 for every lane. Per-modality numbers would be a second copy of
  their table.
- **Video's 7,200 s and its `NON_DURABLE → DURABLE_ASYNC` reclassification do not reach
  us**: `KIND_STUDIO_HUB` serves `{chat, image, tts, music, transcription}` — no video.
  Recorded so nobody goes looking for a lane we do not have.
- **Our framing was wrong and their correction lowers the cost we had accepted.** We
  said `PEER_PROXY_TIMEOUT_S` was *"the one constant that drives the whole ceiling"*; it
  was, until they derived the deadline per generator. **The 30-minute hang on fast jobs
  is not paid** — only chat moved.

`test_hub_lane_contract.py` → **17 checks** (2 new); full sweep and `test_e2e.py`
568/568 green against the new Hub values.

## WHAT 0.2.41 CHANGED — the median was hiding the slow machine

One field, and it came from the owner measuring his own fleet: **M1 12 tokens/second,
M2 14, M4 18.** This gateway's per-location medians (16.8, 16.6, 14.2, 11.6) are an
independent measurement of the same machines and agree with that range.

- **Throughput is a property of the chip, and the fleet is mixed.** So the number a
  ceiling must be safe against is the **slowest** machine's, not the fleet's median —
  and `measured` published only the median, which was 15.4 and hid the 12.
  `slowest_site_tokens_per_second` now sits beside it.
- **It corrects a claim this file made one version earlier.** 0.2.40's notes said 8,192
  takes 532 s with 1.6–1.7× headroom; that used the median. Sized against an M1 it is
  **683 s, 1.32×**. The conclusion survives — 8,192 is still the ask, and 16,384 now
  **times out on every chip in the fleet** (910 s even on an M4) — but the margin did
  not, and a margin is the whole reason the number was quoted.
- **Why the caller cannot do this sizing itself:** the gateway picks the location at
  dispatch, so "which Mac will run this" is not a question a caller can answer. The
  spread has to be published, not averaged away.
- `null` when no rate was measured — `min()` over nothing is not zero, and a lane
  nobody timed must not read as the slowest thing in the fleet.

`tests/test_qualification.py` → **33 checks** (2 new).

## WHAT 0.2.40 CHANGED — the measurement reaches Story Studio, and it is read in SQL

Two changes, both about the same record. `measured` is now on **every catalogue row**,
so Story Studio reads it from `/v1/models` and `/v1/chat/models` — the surface it
already polls — instead of the admin route it cannot reach. The shape, and what it is
and is not for, is STORY-7 in the master tracker.

- **`max_output_tokens` is untouched.** Still exactly what the producer states, with
  its own `*_source`; `measured` sits beside it and `source: "gateway"` is **inside**
  the object, so the label cannot drift from the numbers it describes. A model nobody
  has run publishes `measured: null` — unknown, not zero — and there is an e2e check
  for each half.
- **A location's work is published under the maker.** A job routed to
  `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`, and the same
  model at six sites is one row summarised **across all six** — `samples` summed, the
  maximum taken as a maximum, the rate as a median of medians. Keying the join on the
  raw provider id left every local row reading as unmeasured, which is the one lane
  the question is about. `published_index` always emits the same keys whether one
  location contributed or six: a shape that changes with the number of contributors is
  a shape a reader has to guess at.
- **The read moved into SQL, and that is the second change.** `store.usage_samples`
  extracts four numbers with `json_extract` and leaves the generated prose in the file.
  `result_json` holds the *whole* completion — ~16 KB on a 4,096-token answer — so
  pulling `SELECT *` over 500 jobs to read four fields would materialise megabytes
  every rebuild, which is every 20 s on a path Story Studio polls. Measured: the query
  returns 54 rows in **0.4 ms** on the live store.
- **Two guards, both of which a test forced, and both of which would have shipped
  silently:**
  * `CASE WHEN json_valid(...)` rather than `WHERE json_valid(...)` — a WHERE clause is
    not a promise about evaluation order, SQLite may evaluate `json_extract` first, and
    it *raises* `malformed JSON` rather than returning NULL. One unreadable row would
    have taken every other row's measurement with it.
  * `json_type(...) IN ('integer','real')` — **`json_extract` returns `1` for a JSON
    `true`**, so the `bool` guard that rejected `{"completion_tokens": true}` stopped
    working the moment the read moved into SQL, and `True` re-entered the record as a
    one-token completion. The focused suite caught it. The type has to be checked where
    it is still a JSON type; a stringified `"7"` is refused by the same clause.
- **The catalogue never imports the store.** `runtime` imports `catalog` at its own
  module level, so a module-level import here is a cycle — and the lazy-import version
  produced **no measurement at all**, silently, because `catalog = Catalog()` runs
  during that module's import, before `runtime` has built the store. It is
  `attach_store(store)`, called explicitly after both exist.
- **Known limit, stated rather than hidden:** one envelope per (provider, model)
  whatever the work was, so a prose chapter and a scene-plan JSON body share a row and
  `max_completion_tokens` is the largest of either. That matters because JSON is denser
  in tokens per word than the prose the ratios were measured on. Every job already
  stores its `params`, so splitting on `json_extract(params, '$.response_format.type')`
  is a read change and not a schema change — do it when a lane has run enough of both
  to be worth telling apart. `ponytail:` in `qualification.py` names this.

`tests/test_qualification.py` → **31 checks** (10 new); `test_e2e.py` → 4 new,
**568/568**.

## WHAT 0.2.39 CHANGED — a model is qualified by what it has really carried



Asked 2026-09-26: *"we need to keep these for that when I want to qualify a new LLM,
we can apply the same parameter check too."* Before this the figures were read once by
hand and lost, so every question about a ceiling was answered from a producer's
advertisement. `gateway/qualification.py` reads them back off the work itself.

**Nothing new is recorded for it.** Every figure was already in
`result_json.payload` on each settled job: `usage.{prompt,completion}_tokens`, the
worker's own `generation_seconds` (after the model is resident, so it excludes a cold
turn's load), and `hub_machine`/`hub_worker` on the image, voice and music lanes. The
change is a reader, not a schema.

- **`GET /admin/api/qualification`** — per (provider, model): samples, the largest
  prompt and completion **that actually ran**, the median rate, per-machine splits,
  first/last seen. `?provider_id=`, `?model_id=`, `?limit=` (1–500, default 500).
- **The measured maximum is the headline, not the stated one.** "Can this lane carry
  8,000 tokens" is answered by the biggest it has carried; "may I ask" is a different
  question with a different owner.
- **`stated` is joined on the *published* namespace.** A job routed to
  `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`; comparing raw ids
  made every local row read as unknown, which is the one lane the ceiling question is
  about. `stated: null` means *not in the catalogue* (disabled or withheld) —
  unknown, never unlimited.
- **`source: "gateway"` on every row, and the stated figures sit beside it, never
  merged into it.** This is the rule the whole feature rests on, and it has its own
  test.
- **The console draws it** — Providers → **Measured**, under Concurrency. A field
  nothing draws is the same defect as a control that does nothing. An unmeasured
  figure renders as a dash, never a zero; a chat model's machine column reads
  **not named** rather than guessing.
- **What is deliberately *not* counted**: a queued job is not a sample; a lane that
  states no `usage` is skipped rather than folded in as zero tokens; a missing
  `generation_seconds` is an absent rate, never `0`; `True` is not a token count
  (`bool` is an `int` in Python, and read as a number it would enter the record as a
  one-token completion). Each of those turns a record of real work into a
  plausible-looking invention.
- **`limit` is declared with `Annotated`, and that is load-bearing.** Written the
  usual way — `limit: int = Query(50, ge=1, le=500)` — the default *is* a `Query`
  object, so calling the function directly handed it to SQLite:
  `type 'Query' is not supported`. The route was reachable **only** over HTTP, which
  is a worse test surface and a trap for the next in-process caller. The focused
  suite caught it. `/admin/api/jobs` still has the old shape; nobody has needed to
  call it directly.

`tests/test_qualification.py` → 21 checks; `test_console_actions.js` → 5 new;
`test_e2e.py` → 7 new, **564/564**.

### What it measured, while it was being built `[MEASURED 2026-09-26]`

Read off the **running** 0.2.39 gateway, `GET /admin/api/qualification`:

| lane / model | jobs | largest that ran | stated | stated by | tok/s |
|---|---|---|---|---|---|
| `openrouter-main` deepseek-v4-flash | 1 | 15,139 | 384,000 | provider | — |
| `experiential-labs` deepseek-v4.1-flash | 1 | 8,483 | *(not catalogued)* | — | — |
| `openai-direct` gpt-6-luna | 19 | 6,221 | null | gateway | — |
| `tokenharbor` mimo-v2.5:free | 1 | 5,045 | 128,000 | curated | — |
| **`studio-hub` Qwen3.5-9B-4bit** *(4 locations)* | **23** | **4,096** | **4,096** | **provider** | **15.4** |
| `studio-hub-b8ccda585b97` Qwen3.5-9B-4bit | 2 | 2,350 | 4,096 | provider | 16.6 |
| `studio-hub-de7250e146a7` Qwen3.5-9B-4bit | 5 | 841 | 4,096 | provider | 14.2 |
| `studio-hub-local` Qwen3.5-9B-4bit | 3 | 27 | 4,096 | provider | 11.6 |

- The **chat lane names no machine.** Its payload carries `finish_reason`, `model`,
  `usage` and `generation_seconds` and nothing that identifies a Mac, so a chat
  model's envelope is per location until Studio Hub names one. The image, voice and
  music lanes do name one.
- **4 of the 13 stopped at exactly 4,096 completion tokens, and all 13 reported
  `finish_reason: "stop"`.** The cap is being hit and reported as a clean stop — so
  *"finish_reason must be a clean stop"* does not, on its own, detect truncation.
  Only the measured maximum next to the stated ceiling shows it. This is HUB-18 and
  GW-10 in the master tracker, with the arithmetic below.
- **The same model measured 15.4 tokens/second across the fleet** — 23 jobs over 4
  locations, published as `measured.median_tokens_per_second`, and a median of the
  per-location medians (16.8, 16.6, 14.2, 11.6) so one Mac with many jobs cannot
  dominate it. Use this one; earlier notes here said 16.8 (the best-sampled single
  location) and an even earlier one said 17.3 (**one 489-token sample**, retracted).
  Also measured: 1.22 tokens/word, 7.0 characters/word, 5.7 characters/token. Against
  the chat lane's own stated deadline — **900 s then, 1,800 s since Hub 0.6.70**:
  4,096 → 266 s, 8,192 → **532 s**, 16,384 → **1,064 s, and it now fits the window
  rather than dying at it**.
- **The round trip is the Hub's `FALLBACK_MAX_OUTPUT_TOKENS`, and Story Studio's
  chunker is not the defect.** An earlier version of this file implied the chunker was
  over-tuned. **That was wrong and is retracted** — see HUB-19. Their
  `OUTPUT_TOKENS_PER_WORD` was 1.3 then and is **1.6** today (raised 2026-09-26 on both
  sides' measurements), and their threshold is `chunkThresholdWords(cap)` computed live
  from the ceiling **we** publish: **372 words** at 4,096, **2,932** at 8,192, **8,052**
  at the **16,384** the Hub now ships. At 16,384 a 1,200-word chapter is one call, with
  **no Story Studio change at all**. Removing the chunker instead would lose chapters
  silently, because a generation cut off at the ceiling still reports
  `finish_reason: "stop"`.
- **A source can arrive with no number.** `openai-direct::gpt-6-luna` publishes
  `max_output_tokens: null` **with** `max_output_tokens_source: "gateway"` — that is
  `_limit_with_source`'s `if fallback is None: return None, "gateway"` branch, and it
  is deliberate: the three-word vocabulary (`provider`/`curated`/`gateway`) has no
  fourth word for "nobody said", and adding one is a breaking change for a reader that
  knows only three. **Left exactly as it is.** What it did cost was a bug in the new
  console panel, which collapsed *"not in the catalogue"* and *"in the catalogue, but
  it states no ceiling"* into one string and so told the reader a model was missing
  when it was present. The panel now has **three** states, and there is a test.
  The live record is what showed it; a fixture would not have.


**This is the change that makes a fleet worth owning.** The worker ran exactly one
job at a time — chat, image, TTS, subtitle and music all shared a single slot — so
nineteen Macs sat idle behind one render. It was deliberate, and the reason is
still recorded in the file it was written in: *"a second thread would add
concurrency bugs (double-claiming, artifact races) for no benefit."* Both risks
were re-checked and both are smaller than that note claims; the cost of the old
shape is not.

- **Three ceilings**, in `gateway/concurrency.py`, all of which must pass before a
  job is admitted: the **pool** (`GATEWAY_JOB_CONCURRENCY`, default 4), the
  **lane** (`GATEWAY_LANE_CONCURRENCY=image=1,chat=4`, empty by default), and the
  **provider**. Each answer carries its `source` — `hub` (a site's own
  `available_worker_slots`), `curated` (`data/provider-concurrency.local.json`), or
  `gateway` (a labelled default of 1, naming the file to edit).
- **`paid_concurrency_limit` is now derived, not written.** It was the literal `4`
  while one job ran at a time: a published number that did not describe the system,
  in a field Story Studio reads. It now comes from the same function that sizes the
  pool, so the two cannot drift.
- **Daemon threads, not a `ThreadPoolExecutor`.** An executor joins its threads at
  interpreter exit, so quitting during a two-minute render would hang the process
  instead of stopping it. The dispatcher is single-threaded and is the only
  admitter; correctness of the claim does not depend on that, only tidiness of the
  counters.
- **A limit of zero is not a stop.** A Hub reporting no free slots is busy right
  now, not closed; treating `0` as a cap would leave jobs due for ever with nothing
  wrong and nothing to see. Zero falls back to 1, and there is a test for it.
- **Two bugs the new tests caught**, both of the "setting that silently does
  nothing" shape this project keeps finding: `_positive_int` rejected numeric
  **strings**, so `GATEWAY_JOB_CONCURRENCY=7` and `GATEWAY_LANE_CONCURRENCY=image=1`
  were ignored and fell back to the default; and `test_job_claim`'s *"the loop
  returned, therefore the job ran"* became a race the moment `_loop` stopped
  executing inline. `Worker.wait_idle()` exists for the second one.

`tests/test_worker_concurrency.py` → 13 checks then (**45 tests today**), stable over
five runs. Focused suites 30/30, `test_e2e.py` 555/555.

## WHAT 0.2.28 CHANGED — and the three things it cost to learn

- **GW-5: a location's models are re-read when its Hub changes build.** The refresher
  already probed every location and already knew the version, and did nothing with a
  change; discovery ran only when somebody pressed Refresh. `fleet.refresh_models_when_hub_changes`
  now compares the probed version against **the discovery cache's own `hub_version`** —
  not memory, because the gateway restart usually follows the update — and re-discovers
  on a difference. A location with no record is refreshed once, so it seeds itself. A
  **failed** discovery records nothing, so the next pass retries instead of marking
  stale rows current. The manual Refresh records the same baseline, so it is not redone.
  `tests/test_fleet_hub_version_refresh.py` → 10 checks. Retires OWNER-5.
- **GW-4: the declared Studio Hub is `0.6.65`**, set from a running Hub rather than a
  changelog. It was deliberately **not** set to `0.6.64` while that build was only held
  by controllers and running nowhere: the constant means *measured against*, and that is
  a claim a version nobody has run cannot support.
- **A test is a row, and it lies the same way.** `test_studio_hub_contract.py` asserted
  `hub_version_relation("0.6.64") == newer` with the numbers written in by hand. Bumping
  the declaration turned it red — correctly. It now derives every comparison from
  `SUPPORTED_STUDIO_HUB_VERSION`, so the next bump cannot break it. **When you move the
  declaration, grep the tests for the old number.**
- **The first fleet-wide release push, and what it found** `[MEASURED 2026-09-25]`:
  `POST /admin/api/release/publish` → **5 of 5 controllers accepted**, HTTP 200 in
  2563 s, and every controller countersigned with a **different** key, so each mirror is
  its own. Two things to carry forward:
  - **The request body is base64**, so a 1.34 GB artifact becomes 1.79 GB in one body and
    the gateway's RSS peaked at **8.0 GB**. That is the ceiling to watch on a smaller
    host, and the reason the controller-mirror beats pushing to every node.
  - **What is held and what the kit has are different facts.** The push landed 0.6.64
    while all four kit drives already carried **0.6.65**. Check both before aiming a push.

## THE WORKING LOOP

Run everything from `repository/`. Python tooling is the checkout venv `.venv/bin/python`.

| # | Step | Exact command / action | Footgun next to it |
|---|---|---|---|
| 1 | Make the change | edit under `repository/` | Never `rm` — move removals to `_trash/` (see below) |
| 2 | Bump the version | edit `repository/VERSION` | Patch-only; `build.sh` reads it for `CFBundleShortVersionString` and the DMG name |
| 3 | Build + DMG | `./menubar/build.sh` | Installs to `/Applications/gateway-kh.app` **and** writes `dist/gateway-kh-<VERSION>.dmg`. `--no-install` builds only into `menubar/build/` |
| 4 | Test narrowly | console/JS → `node --test tests/<file>.js`; Python → `.venv/bin/python tests/<file>.py`; Swift → `tests/check_native.py <check>` in the background | See the test rules below |
| 5 | Restart the app | kill the **PID** of `GatewayKH`, confirm port 8787 is free, then `open /Applications/gateway-kh.app` | Relaunching is pre-approved; `pkill -f "GatewayKH"` kills the shell running it |
| 6 | Ship | `git add -A && git commit && git push` on `main` | The DMG already exists from step 3; nothing else is required to hand it over |
| 7 | Publish a Hub app release (optional) | Fleet → Locations → *Publish a Studio Hub release…* → `POST /admin/api/release/publish` | Signing key stays in the data dir; trust install on a controller is the **owner's** action |

- **Verify the running build, not the bundle.** Read
  `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD` and `curl /health`; both must
  show the stamp you just built. A gateway outlives the app and the app **adopts** whatever
  answers `/health`.
- **`tests/test_ui.py` is a release gate, not a routine check.** It forces the window to 700 pt
  and presses controls, taking over the owner's screen. Reserve it for an explicit owner request
  or a promotion gate, run it in the background, and never run it for a change that touches no
  app file.
- **The real-Hub fleet suite is opt-in:** `GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python
  tests/test_fleet_standalone.py`. Without the variable it skips before contacting the Hub.
- **Always rebuild before claiming it works.** "It builds" is not "it works."
- **The release gate reads the Fleet page's *five tabs*, not the old single scroll.** In
  `tests/test_ui.py` §7 the tabs are AX **radio buttons** (`select_fleet_tab`: 1 Overview,
  2 Locations, 3 Machines, 4 Models, 5 Jobs), the page *remembers* the last tab, and each
  tab addresses its table differently — Locations and Jobs are nested one level
  (`table_texts(n, nested=True)`), Machines is a bare `table 1 of scroll area 2`
  (`nested=False`). Change the Fleet tabs and §7 must change with them, or the gate reads
  a loading view and reports the tables missing.
- Structural change → keep the graph current: `graphify extract . --update --code-only`.

## PATHS

| What | Where |
|---|---|
| Git checkout (all source) | `repository/` |
| Container (not a repo) | `..` = `gateway-kh/`; holds `test-instance/`, `backups/`, `_trash/`, `worktrees/`, `store-analysis/` |
| Build script | `repository/menubar/build.sh` |
| App source | `repository/menubar/*.swift`, `build.sh`, `whats-new.md` |
| Gateway source | `repository/gateway/` |
| Tests | `repository/tests/` |
| Build output / DMGs | `repository/dist/` (gitignored); container-root `dist` is a symlink to it |
| SSD setup kit | `repository/dist/setup-kit/` — copied to `/Volumes/KH-STUDIO-*/Video Generation Kit/GatewayKH/` |
| Release notes | `repository/menubar/whats-new.md` (newest first; shown by the app's What's New) |
| Live data dir | `~/Library/Application Support/gateway-kh/` (`.env`, `data/`) |
| Release publisher / state | `~/Library/Application Support/gateway-kh/data/release-publisher.pem` (0600), `release-state.json` |
| Test instance (code-only) | `test-instance/` — `./run.sh`, `./status.sh`, `./stop.sh`, port 8100 |
| Graph | `repository/graphify-out/` (gitignored, rebuildable) |

## RULES THAT COST US TIME

- **Never `rm` — every removal goes to `_trash/`.** That includes clutter you decide on
  yourself, keeping sub-structure, and say where it went. `_trash/` is the **owner's** to
  empty: never delete or tidy inside it. Git branches/refs are the exception — record name and
  SHA in a note under `_trash/` first. (`_trash/` is the owner's: it may be nearly
  empty, and its README may list items he has since removed — that is his call, not
  a defect to fix.)
- **`pkill -f "GatewayKH"` kills the shell running it** because the shell's own command line
  holds the pattern; the command never finishes and the old gateway survives. Kill by **PID**,
  then prove the PID changed and 8787 is free before measuring.
- **Never hand another session a description of behaviour that is not deployed.** Either
  restart and measure the running service, or say "built but not live."
- **Never return 402.** Story Studio holds a credit lock keyed to the API key; one 402 kills it
  permanently. A provider out of funds is a settled `failed` job — `test_e2e.py` asserts no 402.
- **Absence is `null`/absent, never a plausible `0` or default.** "Nobody stated it" and "it is
  zero" must not render the same. A stated `0` is a claim — read with a first-non-`None`
  lookup, never an `or` chain.
- **`freshness.stale` is always `false` by construction — do not "fix" it.** Making the gate
  fire would disable every model on a stale inventory. `inventoryAgeMs` is the honest number.
- **Never identify a machine, model, location or batch by its label.** Ids are identities;
  labels are the owner's to change. A location's provider id derives from its controller id.
- **A Hub's `site_id` is not unique until it is assigned one.** Studio Hub defaults it to the
  constant `"local-site"` (`components/hub/backend/control_plane.py::defaults`), so matching a
  new controller on it made the second "Add a location" overwrite the first. `fleet._stable_identity`
  falls back to the per-machine `controller_id`; do the same anywhere a controller is keyed.
- **A definite 4xx from any provider is its own refusal, never an unknown-outcome hold;**
  only a 5xx may have been accepted. The worker enforces this centrally on `BackendError.status`,
  so every adapter inherits it. A **connect timeout/refusal** is an unreachable location, not a
  job failure — but a read timeout *after* the request left is still ambiguous. A `400` that
  names `temperature` as unsupported is retried once without it (recorded as `params_dropped`),
  because newer reasoning models accept only their default — and `catalog._DEFAULT_TEMPERATURE_ONLY`
  now withholds that control from the known families on the provider that refuses it, so the
  retry is a backstop rather than the fix.
- **A page must never resize the window.** A page with a required height wraps its body in an
  `NSScrollView` with a `FlippedView`; keep table floors low and cap wrapping labels.
- **The bundled Python runtime's build is explicit:** `swiftc -target
  "${TARGET_ARCH}-apple-macos${MACOS_MIN}"` (default 11.0). Without `-target`, `swiftc` stamps
  the SDK's macOS version and macOS 14 refuses the app with "requires macOS 27".
- **`graphify` reports a syntax error in `menubar/VoicesWindow.swift` — a parser limitation,
  not a defect.** `swiftc -parse` is clean; trust the compiler.
- **Mark deliberate simplifications with a `ponytail:` comment** naming the ceiling and the
  upgrade path, or the next reader cannot tell a trade-off from an oversight.
- **`finish_reason` must be a clean stop** (`length` is read as truncated), and generation
  artifact URLs stay relative on the API origin.
- **A field nothing draws is a defect, and so is a fault repeated once per consequence.** The
  node roster's per-node `detail` and each machine's `health` and `available_gb` were fetched,
  published and read by nobody, so eight dead nodes were eight identical "offline" rows; the
  same fleet produced **148 issues for nine conditions** because one dead machine was reported
  as state + health + one row per studio + one per unreachable model. Before adding a field,
  name its reader; before emitting an issue, ask whether it is a *different* fault or the same
  one in another vocabulary.
- **A user-visible sentence is a contract when a test reads it.** `78fee1d` rewrote the Fleet
  page and lowercased "Could not discover" while moving "Retry failed" into `FleetTabs.swift`;
  `tests/test_task4_native.py` went red on 2026-09-23 and nobody noticed because that suite is
  not in the documented verification list. **Run every `tests/test_*.py`, not only the three in
  the table below**, or a red gate hides for days.
- **A version is declared, never a gate — and read what the far side states.** Two rules
  bought together on 2026-09-24. (1) Studio Hub auto-updates itself, so a runtime check on its
  version would turn *its* update into *this* fleet's outage; the gateway names the release it
  targets (one constant), reports a drift as information, and keeps every gate a capability
  question. (2) The same instinct in discovery: keying a Hub package to a modality by its
  **engine name** made a new engine invisible until this repo was edited, while the Hub was
  already stating `hub_modality` on every model. **When the far side states a fact, read it and
  keep your own table as the fallback** — the table's entries were guesses about a name, the
  stated field is the Hub's own answer. Anything left over is *reported by name*, because "it
  has nothing" and "I did not understand it" must never be the same answer.

## DO NOT TOUCH

- **Other repositories.** `~/Developer/studio-hub-kh`, `~/Projects/studiohub-standalone` and
  `theng12/Studio-Hub-KH` belong to other sessions and are read-only from here. The Hub fact
  that matters is the **installed bundle**
  (`~/Applications/Studio Hub.app/Contents/Resources/voice/`), which can differ from a checkout.
- **Other sessions' worktrees.** `worktrees/` and `test-instance/` are not yours to edit; the
  owner decided the test instance stays **code-only** (no keys, no Hub).
- **`backups/`, `_trash/`, `store-analysis/` and any archive/trash folder.** Recoverable
  history; moving things *in* is allowed, deleting or emptying is the **owner's**.
- **GitHub Actions stays disabled**, and do not include the real-Hub fleet suite in offline
  smoke testing.
- Do not restart the app without a reason — it interrupts the owner — though relaunching is
  otherwise pre-approved (he asked not to be asked, and to quit it yourself).

## OPEN / IN FLIGHT

> **Read this section as a dated log, not as current status — corrected 2026-09-26.**
> Most of it was written across 2026-09-24/25 and several of its facts have moved since.
> Rather than rewrite each bullet (which would destroy the record of what was true when
> it was written), here is what changed:
>
> | then | now `[MEASURED 2026-09-26]` |
> |---|---|
> | both controllers on Studio Hub **0.6.54** | `0.6.65` × 4, `0.6.64` × 1 |
> | this Mac's Hub **0.6.60/0.6.63** | **0.6.70** build `30ecef1` |
> | "Studio Hub is not running on this Mac" | it is running, and all four workers report ready |
> | the chat lane publishes `max_output_tokens: 4096` | **16384**, and `deadline_seconds` **1800** |
> | `test_e2e.py` **546–555/555** | **570/570** |
> | the remote chat proof "blocked on our side" | **closed** — GW-1, and the fleet gate is green (54 check sites; it prints its own total) with a real remote dispatch |
>
> **A stale handoff is worse than none, because it is trusted.** If a number here
> disagrees with the table at the top of this file, the top of the file was measured.

- **Shipped in 0.2.25 (`0b03441`): the Hub chat lane is dispatchable, proved live on
  0.6.63.** Studio Hub 0.6.63 opened `POST /studio/chat/v1/chat/completions` to a
  **Fleet** credential, which is what made a Hub LLM possible here at all — before
  it, every chat route refused a fleet token. Reply:
  `_handoffs/2026-09-25-gateway-chat-lane-shipped.md`.
  1. `chat` is declared in all three places: `adapters._MODALITIES_BY_KIND[KIND_STUDIO_HUB]`,
     the `studio-hub-local` definition, and the **per-controller** definitions the
     roster derives (`providers/registry.py`) — the third is what lets a *remote*
     controller serve chat.
  2. `adapters/studio_hub._generate_chat` posts the OpenAI body to the **peer**
     surface through the Hub (admission, machine lease, worker signing), never a
     worker's own route and never the Hub-token-only `/api/hub/chat/completions`.
     `stream: false`; only the lane's honoured parameters are sent (asserted on the
     wire); `MODEL_NOT_PACKAGED`/`MODEL_NOT_READY` → `LOCATION_NOT_READY` so the
     worker re-routes; a `422` is a definite refusal, never an unknown-outcome hold;
     an empty answer is a failure.
  3. The catalogue publishes the Hub's own `model_limits` (262,144 read from the
     checkpoint, 4096 as a labelled worker default) with `*_source_detail` carrying
     the Hub's provenance sentence; `verified_token_usage: false` on every chat row
     (we validate what a provider states, we do not re-measure); and
     `generation_seconds` on the job envelope — the worker's own number, never
     derived from our round trip.
  `tests/test_hub_chat_lane.py` (7 checks, **wire** assertions),
  `tests/test_fleet_routing.py::HubChatRoutingTests`, `test_e2e.py` **555/555**.
- **Live, on this Mac's 0.6.63 Hub `[MEASURED 2026-09-25]`:** a real completion
  through the gateway — `succeeded`, `output_text: "Red, Blue, Green"`,
  `finish_reason: stop`, `usage 22/5/27`, `generation_seconds 0.573`,
  `verified_token_usage: false` — and the Hub's own `GET /api/hub/jobs` records
  those runs as `"modality": "chat"`. A `json_object` request returned parseable
  JSON first try, with the Hub's caveat recorded: **no grammar-constrained
  decoding**, so a strict-JSON call site is prompting plus validation.
- **The published model id is undecided on purpose.** The Hub's handoff says Story
  Studio's `lmstudio:` two-pass behaviour is keyed on an id **we** control and their
  tracked source has no code selecting two-pass from a model id, so they asked us to
  ask the Story session rather than guess. It publishes as
  `studio-hub::mlx-community/Qwen3.5-9B-4bit` (the normal Hub namespace) meanwhile —
  nothing is blocked. **Open question for the Story session.**
- ~~**The remote chat proof (their test-plan item 4) is blocked on our side:** both
  controllers are on **0.6.54**, which refuses the chat lane to a Fleet credential.~~
  **CLOSED 2026-09-25 (GW-1) and re-proved 2026-09-26.** The controllers are on
  `0.6.65`/`0.6.64` and the fleet gate runs a real remote dispatch end to end —
  `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against Hub `0.6.70`, including a
  transcription and the artifact returning through the gateway. Struck through rather
  than deleted because it was the reason the declared version moved at all.
- **A stale `dispatch_state` reads as "not installed".** The first live chat attempt
  was withheld because the cached package row said `files_state: not_installed` /
  `dispatch_state: ineligible` while the Hub answered a completion perfectly; a
  discovery refresh cleared it. Recorded as a property of the cache, not a defect on
  either side — but worth knowing before believing a withheld chat row.
- **Memory policy (Hub §3) is untouched by this gateway** — not read, not written,
  not on any page — so the five-mode change and the `auto` skew cannot break us. When
  it is surfaced: show `resolved_mode` and the resolved `idle_seconds`, never `mode`,
  because a null idle threshold renders as "kept loaded" — the opposite.
- **Shipped in 0.2.24 (`18408cc`):** the lane contract Studio Hub 0.6.61 published
  (`f20fd1a`, spec `docs/lane-execution-contract.md`) is read, and the one thing it
  does not yet give us is reported back in
  `_handoffs/2026-09-25-gateway-lane-contract-reply.md`.
  1. **Three readers place a model, strongest first** — the package row's own
     `modality` (0.6.61; present whenever the engine is registered, independent of
     the best-effort catalogue read), the catalogue's `hub_modality`, then
     `_HUB_ENGINE_MODALITY` in `providers/discovery.py`. Which reader placed the row
     is published as `raw.modality_source` (`package` / `stated` / `engine`), and a
     package the Hub itself refuses to place is reported **in the Hub's own words**
     — the sentence naming `_ENGINE_ROUTE`.
  2. **`lane_contract` is carried verbatim** on the model's cached row, and two
     fields are read: `deadline_seconds` (`adapters/studio_hub.lane_deadline_seconds`,
     replacing the flat 900 s poll ceiling for that model) and
     `states.discard_supported` (`api._lane_discard_state`). The Hub's chat lane says
     `false`, so a hold on it now publishes `close_hold` and `discard-uncertain`
     closes the record locally — before, the Jobs window offered a discard the Hub
     would refuse.
  3. **`model_limits` on chat rows** are published as the model's own numbers, with
     the Hub's provenance sentence in a new `*_source_detail` beside the value
     (`catalog._hub_stated_limit`). A Hub chat row no longer wears the owner's
     curated flat `1,000,000`: measured live, the Hub states **262,144** read from
     `model/config.json text_config.max_position_embeddings`, and **4096** as a
     labelled worker default.
  Not adopted, deliberately: `parameters`, `artifact`, `cancel.semantics`,
  `runtime_seconds` — on the row, consumed by nothing yet, and said out loud in the
  reply so the Hub session knows which half landed.
  `test_e2e.py` **552/552**, `tests/test_hub_lane_contract.py` (new, 10 checks),
  `test_studio_hub_contract.py` 54, every focused suite green.
- **The live 0.6.61 verification `[MEASURED 2026-09-25]`:** this Mac's Hub came up on
  0.6.61 mid-session, so the reads were measured against a real one and not only the
  mock — **23 models discovered, all 23 placed by `package`, 21 carrying a lane
  contract** with the canonical operation tokens and `deadline_seconds: 900.0`. The
  chat model `mlx-community/Qwen3.5-9B-4bit` arrives with `operation:
  chat.completion` and `discard_supported: false`, is **withheld** with the reason
  "the studio_hub adapter does not serve chat…", and carries both `model_limits`
  provenance sentences verbatim. Two new image models (`AITRADER/FLUX2-klein-4B-mlx-4bit`,
  `filipstrand/Z-Image-Turbo-mflux-4bit`) flow through as `image.text_to_image` — the
  owner's new models arriving with no gateway change.
- **The chat lane cannot be dispatched, and that is not a gateway defect.** The
  contract names the studio's `POST /v1/chat/completions`; on 0.6.61 the Hub serves it
  only at `/api/hub/chat/completions`, whose own docstring says it is for "this app's
  own Chat page" and which proxies to **this Mac's** worker. Nothing calls
  `chat_jobs.create_batch` (`transcription_jobs.create_batch` *is* called from
  `standalone.py`), so no chat batch can be created, polled or collected by a
  coordinator. Until the Hub exposes submit/status/artifact for chat, Hub chat models
  are **discovered, listed and withheld** — never dropped, never offered and then
  failed. The four routes we need are named in the reply note.
- **Declared Hub version stays `0.6.60`, deliberately.** 0.6.61 is installed here and
  staged on all four drives, but it is **not pushed to GitHub** and no *controller*
  runs it, so declaring it would be a claim we have not measured against — the exact
  thing this exchange exists to avoid. The local location reads `newer` today, which
  is the report doing its job. Raise the constant when 0.6.61 lands and a controller
  runs it.
- **The 401 on `/studio/voice/api/transcribe/availability` is ours to fix**, per the
  Hub session's trace: that node must be **re-joined with the controller's current
  Fleet token**. An owner action on that Mac. The refusal is now self-describing on
  their side (method, path, credential presented, credential required), which is what
  turned it from a mystery into a one-line fix.
- **Shipped in 0.2.23 (`e42be96`):** a declared Studio Hub version, and discovery that reads
  what the Hub states. Owner's decisions, 2026-09-24: **report drift, keep working**;
  **adopt whatever the gateway can already serve**.
  1. `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION = "0.6.60"` is the one declaration.
     `/health` publishes it as `studio_hub.supported_version`; `fleet._probe` publishes
     `hub_version_relation` (`match` / `older` / `newer` / null) per controller; Fleet →
     Locations shows it in the Hub column; drift is one **info** issue, so it never moves
     `summary.issues`. **No gate anywhere** — routing, the catalogue and the worker ask
     capability questions only, asserted by
     `test_studio_hub_contract.HubVersionDriftTests` and
     `test_fleet_routing.HubVersionReportTests`.
  2. **Discovery reads the Hub's stated `hub_modality` per model**, with
     `_HUB_ENGINE_MODALITY` only as the fallback. Before this, a package on an engine not in
     that table was `continue`d in silence — so a model added on the Hub on a new engine (the
     owner is adding an image model now; LLM and video are being researched) produced no row,
     no count and no reason. A lane this build serves arrives with no gateway release; a lane
     it cannot serve (video, chat) is discovered and **withheld**, listed with the reason; a
     package nothing can place is reported by engine (`DiscoveryResult.skipped` → the model
     cache → `/admin/api/providers` and `/admin/api/providers/{id}/models`, drawn as an
     "unrecognised engine" badge). Live: 35 models per controller, unchanged, **30 placed by
     the Hub's statement** and 5 by the engine fallback `[MEASURED]`.
  `test_e2e.py` **550/550**, console suites **35/35**, every focused suite green.
- **The first true thing the declaration said:** both controllers run Studio Hub **0.6.54**
  while this Mac and all four drives ship **0.6.60**, so both now read `0.6.54 · older`
  `[MEASURED 2026-09-24]`. That is a report, not a fault — the controllers keep taking work.
  Bringing them to 0.6.60 is `Studio Hub KH/1 - Install the app.command` on each controller.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00: TCP
  to every node address times out or refuses from this Mac too, so the report is accurate).
  The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.22 (`3e254c4`):** a fleet/node and Jobs-window audit. Six defects:
  1. `machines_flat` now joins the **node roster on the node id** and carries `available_gb`,
     so a dead node's health word (`no_hub` / `unreachable`) and the controller's own
     "could not reach the node: …" sentence reach the Machines tab, and memory reads
     "17.18 GB (7.14 GB free)" rather than an unlabelled total.
  2. `fleet._issues_for` no longer repeats one fault: the health row folds into the offline
     row, workers on an already-offline machine are one counted line instead of four, and
     `worker_unreachable` on an offline machine is left to that machine. Measured live:
     **148 issues → 19**, and the 7 that survive are a genuine `401` fault, not the offline
     machines. `tests/test_fleet_routing.py::OneFaultOneRowTests`.
  3. `catalog._publish_lock` writes `locked` / `lock_reason` / `availability` together. The
     merge updated only the first two, so a model offered by three locations with one
     answering published `locked: false` beside `availability: "unavailable"`.
     `tests/test_audit_regressions.py::CatalogueMergeTests`.
  4. `summary.models_available` counts a model once per location, like its denominator — the
     tile read **"175/102 local models available"** (rows over models) and now reads 60/102.
  5. The `78fee1d` tabbed-Fleet rewrite lowercased "Could not discover" and moved
     "Retry failed" into `FleetTabs.swift`, leaving `tests/test_task4_native.py` red since
     2026-09-23. **A user-visible sentence this file or a test quotes is a contract: change
     the check in the same commit.**
  6. **The 0.2.19 gap is closed.** `store._recover_interrupted` records the upstream attempt
     (`{upstream_job_id, upstream_item_index, recorded_by: "gateway_restart"}`), a lane with
     no discard route is **closed locally** (`HOLD_CLOSED`, with a recorded sentence saying
     the provider was not told), and `api.recovery_action` is published per job so the Jobs
     window draws `Discard uncertain attempt…` / `Close this hold…` / nothing. Six holds
     from before this release are in the live library — three OpenAI `gpt-6-luna` (created
     2026-09-24 00:35, four minutes before `8bd434d`) and three provider/network holds; all
     six now publish `close_hold` `[MEASURED]`. `tests/test_studio_hub_contract.py::DiscardActionTests`,
     `tests/test_job_recovery.py`, `tests/test_jobs_outputs.swift`.
  Verification for the release: `test_e2e.py` **546/546**, `test_ui.py` **130 passed /
  5 skipped** (the skips are Hub-dependent; **Studio Hub was not running when this was
  written — it is now**, rebuilt 2026-09-26 as `0.6.70`),
  `check_native.py jobs-recovery` passes against the installed app, and `/v1/models`
  publishes **0** `locked`/`availability` disagreements `[MEASURED]`.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00:
  TCP to every node address times out or refuses from this Mac too, so the report is
  accurate). The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.21 (`987721e`):** `fleet._stable_identity` — a location is keyed by the
  controller's real `site_id`, or its per-machine `controller_id` when the Hub still answers the
  shared default `"local-site"`. Before this, adding a controller updated the previous one.
  `tests/test_fleet_identity.py`.
- **Shipped in 0.2.20 (`66e8afe`):** `catalog._DEFAULT_TEMPERATURE_ONLY` withholds the
  `temperature` control from default-only reasoning families **on official OpenAI only** — so
  Story Studio stops sending the `0.7` it sends whenever the control is offered. Scoped that way
  because a proxy (Experiential Labs' `gpt-5.6-luna`) accepts `0.7` and must keep it; a provider
  that publishes `supported_parameters` is always believed. `tests/test_chat_controls.py`.
- **Shipped in 0.2.19 (`8bd434d`):** a definite provider `4xx` settles as a failure instead of a
  durable unknown-outcome hold, and a model that refuses `temperature` is retried once without it
  (`params_dropped`). Covered by `tests/test_job_recovery.py` and `tests/test_chat_temperature.py`.
  **The gap it exposed — a cloud hold with no `recovery_context` could never be cleared — is
  closed in 0.2.22; see above.**
- **Shipped in 0.2.18 (`9b7876a`):** the gateway-side **downgrade guard** (refuse/warn when a
  controller already holds a newer version) and the per-controller **`trusted_publisher`** display
  in Fleet → Locations → Release. The guard is proven against the contract-enforcing mock only.
- **Unproven on a real controller:** the release push is proven only against the **local** Hub
  on 0.6.50. The two configured remote controllers were **off** (`LOCATION_UNREACHABLE`,
  ConnectTimeout), so a real remote push and the no-trusted-key (409) path are **not** verified.
  Installing the publisher key on a controller (`POST /api/hub/release/trust`) is the **owner's**
  action.
- **`release-state.json` is misleading if read quickly:** its last publish records a **15-byte**
  artifact `StudioHub-0.6.50-test0001.dmg`, not the 1.28 GB real DMG, and both controllers
  `LOCATION_UNREACHABLE`. `[reason not recorded]` for the tiny test descriptor; do not read it as
  "the real release is on those controllers."
- **Held at the owner's request:** a per-node image + TTS generation sweep; it waits for the
  fleet to be up.
- **No independent review** of the release-publisher work. The gate itself was fixed in this
  release: it had been reading the pre-rework single-scroll Fleet page (and a pinned `/18`
  provider count), so it failed against a correct app; §7 now walks the five tabs and the AX
  helpers retry a transient window read. If the Fleet tabs change again, that section must change
  with them.
- **Docs reconciled 2026-09-23.** `README.md`, `AGENTS.md` and `Project Status.md`
  (§1/§2/§9/§11) now agree with the code: port 8787, `main` as the only worktree,
  current release. The dated records — `_handoffs/`, `docs/superpowers/plans/`,
  and the dated sections of `Project Status.md` — deliberately keep the numbers
  they were written with; history is not rewritten. `docs/fleet-deployment-notes.md`
  remains the pre-deployment plan, marked `[confirm]` where Hub behaviour was then unknown.
- An unused remote branch `origin/codex/local-release-dmg` exists; left alone
  deliberately (not this session's ref to delete). `[reason not recorded]` for why
  it still exists.

## SECRETS

**There are no secrets in this file, and none belong in it.** Everything sensitive lives under
the live data dir `~/Library/Application Support/gateway-kh/` (gitignored): `.env`, and in
`data/` the `credentials.json`, `fleet.json` (controller tokens), `models_cache.json` and the
`release-publisher.pem` private key (mode 0600, never returned by an API). Provider keys and
Studio Hub tokens are the **owner's** and are installed by hand — ask him; do not search the
tree, and never print an address, token or private key into a note or a test.

## WHY THIS FILE IS TRUSTWORTHY

It was written by reading, not remembering: `AGENTS.md`, `Project Status.md`, `README.md`,
`gateway/config.py`, `gateway/release_publisher.py`, `menubar/build.sh`, `docs/*.md`,
`tests/check_native.py`, the last 40 commits, and the live `VERSION` / build stamp / data dir.
Where the project did not record a reason, this file says **`[reason not recorded]`** instead of
inventing one.
