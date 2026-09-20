# Gateway KH — new-computer handoff

Owner direction: audit the replacement for GenStudio, identify bugs and proposed fixes, and continue implementation on a new **Apple Silicon Mac**. The intended product is a beautiful, smooth, simple and dependable single-owner gateway, with Content Pack workflows to be added deliberately.

## Start here

- Repository: **https://github.com/theng12/gateway-kh**. Use the private repository and its `main` branch after this handoff's PR is merged. Do not start from an old ZIP or the installed app's bundled source.
- Read this file, [the open-issue ledger](2026-09-20-open-issues.md), then `AGENTS.md` and `README.md`.
- Moving owner assets too? Read the [asset transfer inventory and restore notes](2026-09-20-asset-transfer.md). The private transfer payload is separate from GitHub.
- [MEASURED] Audit source baseline: `fcfde43`; first repair code: `91b45db`; previous baseline: `e360fbb`. Source locations in the issue ledger refer to `fcfde43` unless stated otherwise.
- [MEASURED] Existing evidence and design direction: [product audit](../docs/2026-09-20-product-audit.md). Earlier audit outcomes: [AUDIT-BACKLOG.md](../AUDIT-BACKLOG.md). Statements there that older batches are “all closed” do not close the new ledger.
- [MEASURED] The audit used three Luna workers for backend, UI, and portability inventories. The primary agent checked the promoted findings against source and reproduced five backend failures offline. Worker-only findings remain explicitly `[REPORTED]`; they are not signed off by agreement alone.
- This is a bounded audit and implementation handoff, **not a claim that every possible bug has been found**. Each unresolved item has a proposed fix and acceptance check. Do not implement a candidate before checking its trigger and counter-evidence.

## What is committed, built, and live

[MEASURED] The first repair code is committed: keyless non-loopback guards when the configured bind is accurate; atomic claiming of still-accepted jobs; OpenRouter batch-only route filtering; Create selection preservation, pending-submission protection, serialized polling, visible polling failures and terminal cancellation. Regression files: `tests/test_audit_regressions.py`, `tests/test_job_claim.py`, `tests/test_create_workspace.swift`.

[MEASURED] A local arm64 app bundle was built and signature-checked as `91b45db-202609201216`. The modified bundled Python sources matched `91b45db`, and its isolated API import succeeded. **It was not installed or activated.** Neither this handoff nor a successful clone proves which code the old computer is running. Check `/health` and the app's build comparison before making a live-behavior claim.

[MEASURED] This handoff turn adds documentation, an offline diagnostic and the tested dependency snapshot; it removes tracked Finder metadata and ignores `.DS_Store`/`.serena/`. It does not implement the newly listed defects, restart the service, transfer credentials, run paid generations, or change old GenStudio/Story Studio data.

## Clone and establish a clean development baseline

Prerequisites: Apple Silicon, macOS 13+, Xcode Command Line Tools (`swiftc`, `iconutil`, `codesign`, Git), and `uv`. Authenticate your GitHub account so it can read the private repository. Run `xcode-select --install` only if Command Line Tools are absent. Install `uv` through its official distribution if absent; it is development tooling, not a product dependency.

```sh
xcode-select -p
command -v uv
uname -m                      # arm64 on the intended machine
git clone https://github.com/theng12/gateway-kh.git
cd gateway-kh
git switch main
git pull --ff-only
git status --short
git log -1 --oneline
uv venv --python 3.11
uv pip install --python .venv/bin/python -r _handoffs/checks/2026-09-20-tested-dependencies.txt
uv pip check --python .venv/bin/python
.venv/bin/python tests/test_e2e.py
.venv/bin/python tests/test_audit_regressions.py
.venv/bin/python tests/test_job_claim.py
swiftc -swift-version 5 -framework AppKit menubar/PageKit.swift menubar/CreateSection.swift tests/test_create_workspace.swift -o /tmp/gateway-create-check
/tmp/gateway-create-check
```

[MEASURED] The dependency snapshot is an observed `uv pip freeze` of the 23-package environment that passed the audit checks; it is not a security audit or a wheel/hash lock. `requirements.txt` remains unpinned, and `menubar/build.sh` separately installs it into its cached bundled runtime. A fresh bundle may therefore resolve different dependency versions; see the portability issues before claiming reproducibility.

[MEASURED] Tests above use temporary data and the mock upstream. The new diagnostic is also isolated:

```sh
.venv/bin/python _handoffs/checks/audit_probes.py
```

It **observes open defects**, not a passing acceptance suite. Compare its output with [the recorded results](checks/2026-09-20-probe-results.json). Fixing the bugs should change those results. The diagnostic overrides runtime paths before imports and never dispatches upstream work.

Build without touching an installed application:

```sh
./menubar/build.sh --no-install
codesign --verify --deep --strict menubar/build/gateway-kh.app
```

[MEASURED] `./menubar/build.sh` without the flag installs/replaces the app in `/Applications` (or `~/Applications` when needed). The first build downloads the bundled Python and dependencies. The app is arm64-only. Building, installing, launching and restarting the gateway are distinct steps. Do not run an old and new app against the same data simultaneously.

For an isolated manual API session, keep **both** Uvicorn's `--host` and `GATEWAY_HOST` on loopback, choose a free development port, use disposable `GATEWAY_APP_SUPPORT`, `GATEWAY_DATA`, `GATEWAY_DB`, `GATEWAY_ARTIFACTS`, `GATEWAY_CREDENTIALS`, `GATEWAY_MODEL_CACHE`, `GATEWAY_ENV_FILE`, `GATEWAY_FLEET`, `GATEWAY_VOICES`, `GATEWAY_VOICES_OUT`, and `STUDIOHUB_READY_FILE` paths, and set `GATEWAY_NO_MIGRATE=1`. Do not assume setting only the data directory prevents Hub discovery or legacy migration. The test suites contain working examples.

Optional offline browser-logic diagnostic (requires Node.js as a development tool, not a product dependency):

```sh
node _handoffs/checks/console_action_probe.js
```

[MEASURED] Its recorded results in `checks/2026-09-20-console-results.json` show successful mocked Generate/Keep requests followed by JavaScript type errors. No browser, live API or paid provider is called.

## Source sync is not a data migration

[MEASURED] A Git clone contains code, tests, docs and the shipped `data/voices.json`. That tracked default contains ten absolute reference paths on the source Mac; the recordings themselves are not in Git. Do not overwrite a real writable catalogue with that default, or assume it makes voices usable on a fresh machine. It does not contain the owner's configured keys, model selections, fleet roster, jobs, generated artifacts or imported voice recordings. Default live state is under `~/Library/Application Support/gateway-kh/`; environment overrides can relocate individual files (`gateway/config.py`).

Use a **fresh development profile** first. If continuity of the owner's working data is wanted, transfer a private backup separately; do not put it in GitHub. Stop the old gateway before taking a filesystem copy so SQLite and JSON files form a consistent snapshot. Preserve the original backup. Copying only a live `.sqlite3` file is insufficient if sidecars or writes are active.

| State | Treatment on the new Mac |
|---|---|
| `~/Library/Application Support/gateway-kh/.env` and `~/Library/Application Support/gateway-kh/data/credentials.json` | Secret state: securely transfer or re-enter through existing settings. Preserve restrictive permissions. Never paste values into a handoff, commit, issue or log. |
| `~/Library/Application Support/gateway-kh/data/{providers.local.json,models_cache.json,model-limits.local.json}` | Preserve custom definitions, owner model selections and curated limits if continuity is needed. Do not bulk-enable discovered models after a refresh. |
| `~/Library/Application Support/gateway-kh/data/fleet.json` | Roster only: IDs, labels and addresses; tokens stay in the separate credential store above. Preserve stable identities and verify each controller/address/token relationship. A new Mac's local Hub is a separate dependency. |
| `~/Library/Application Support/gateway-kh/data/{voices.json,voices_state.json,voice-library/}` | Keep reference audio, transcripts, segments and metadata together, preserving permissions and IDs. A clip's digest defines identity. Never auto-grant clone/distribution permission. |
| `~/Library/Application Support/gateway-kh/data/{gateway.sqlite3,artifacts/,kept.json,retention.json}` | Preserve together only when job/output history is wanted. Absolute stored file paths and pins need validation against the new home directory; copying bytes alone is not a completed migration. Interrupted jobs need the recovery fix, not blind replay. |
| `.venv/`, `menubar/vendor/`, `menubar/build/`, `graphify-out/`, `.serena/`, Finder metadata | Recreate locally. These are ignored development/build artifacts, not the portable source of truth. |
| macOS app preferences, login item, Accessibility grants | Reconfigure on the new machine if needed; a Git clone does not transport them. Verify paths in the app before enabling startup. |
| Studio Hub installation, ready file and credentials | Install/configure independently if testing local generation. Do not copy a stale ready file as proof of a reachable Hub. |

Do not perform blanket search-and-replace on stored identities or paths. Validate the actual owner data first; separate path relocation from job recovery and credential rotation. The handoff transfers no private state itself.

## Non-negotiable contracts and prior owner decisions

[MEASURED, repository contract] `AGENTS.md` records these requirements; preserve them while repairing:

- Story Studio talks to one gateway origin. Keep the zero-credit price/settlement fields, `chargeable: true`, and never return HTTP 402.
- Unknown provider facts remain null; preserve a stated zero. Keep source labels and the recorded local TTS presentation/engine-cap exceptions. Do not change the deliberately non-blocking catalogue freshness gate.
- Discovery does not imply enablement, configuration, reachability or successful execution. Reuse the shared eligibility rules instead of inventing a second list.
- Voices remain private until explicitly allowed. Keep digest identity, per-voice permission checks, model-aware matching, location identities and token-scope boundaries.
- The gateway chooses a site; the Hub chooses a machine and sections local speech. Do not transfer scheduling responsibility into this UI redesign.
- The no-credit gateway does not make upstream paid requests free. No paid acceptance tests, deployment, publishing, tagging or destructive migration without the necessary owner authority.
- GitHub is code-only: Actions remain disabled. Run verification locally. Do not dispatch hosted checks or weaken unrelated repository safeguards to merge.
- Older handoffs are historical. In particular, do not redo completed batches, revive rejected OpenCode access, or spend the deferred Kie generation credits.

## Implementation order and ownership

1. **Reliability and data protection:** network bind/auth consistency, truthful cancellation, interrupted-job recovery, protecting queued inputs, quote identity and artifact read errors. Use isolated delayed/mock requests. Never resubmit work whose upstream acceptance is uncertain.
2. **Complete Create and result handling:** authenticated asynchronous preview/save/open; shared model eligibility; compatible voice picker; durable drafts and job reattachment. Fix one Audio flow end to end, then share its behavior across modalities.
3. **UI clarity and robustness:** adaptive form/result layout, visible loading/error/retry, credential refresh, truthful availability counts, surfaced action failures, keyboard/focus and minimum-window checks. Proposed navigation: Create / Library (Outputs, Voices, Content Packs) / Manage, with Jobs always accessible.
4. **Content Packs:** inspect current Story Studio schemas, manifests, trust/identity rules and install/download contract; build import/inspect/validate/library/delivery first; authoring and preview follow one supported kind at a time. No pretend ownership or completed purchase without real deliverable semantics. See the ledger's Content Pack section.
5. **Portability and release quality:** define reproducible dependency/runtime inputs, handle ignored signing/icon failures, migrate paths safely, correct stale docs, then test fresh install and upgrade behavior.

These are proposed implementation decisions, not features already shipped. The ledger supplies the bounded tasks and acceptance checks. Reassess severity when reproduction or the consumer contract changes the evidence.

## Verification, limits and completion criteria

[MEASURED] On the source Mac, the handoff reran **521 end-to-end checks**, **6 audit regression tests**, **2 job-claim tests**, the native offscreen Create state check, and dependency consistency across **23 packages**: all passed. These do not contradict the reproduced open defects; those cases were outside the passing coverage.

Not run: full installed-app `tests/test_ui.py`, real-Hub `tests/test_fleet_standalone.py`, every paid/provider model, sustained load, full VoiceOver/light-mode coverage, Content Pack installation, or migration of the owner's actual data. The UI suite drives the installed app and can alter its state; the fleet suite deliberately reaches a real Hub. Run them knowingly, on the new environment, with owner-private data protected. Baseline counts in old README prose are historical; trust fresh output.

For each repair: establish a failure first; trace all callers; make the smallest shared fix; prove the exact failure is gone; run relevant broader checks; get independent review; update this ledger with commit/evidence. For UI work, inspect the rendered app as well as source/state tests. A compiled bundle alone does not prove usability.

Final acceptance for the first implementation batch: configure one mock-backed provider; choose a compatible model and voice; submit once; navigate away/back without changing choices; preview/save all outputs; find them in Jobs/Library; explain a connection failure; cancel queued work; interrupt/restart without hidden duplicate submission; and confirm no forbidden HTTP 402 or credit behavior changed.

## Prompt for the next Codex session

> Work in this clone of theng12/gateway-kh. Read `_handoffs/2026-09-20-new-computer-handoff.md`, its open-issue ledger, AGENTS.md and README.md before editing. Confirm main is current and preserve unrelated work. Establish the isolated baseline, then start with the reliability/data-protection batch. Treat this handoff's source evidence, local built artifact and live application state as different things. Delegate narrow enumeration/mechanical work to Luna; keep design decisions and evidence verification in the primary agent. Do not silently resubmit interrupted upstream work, migrate private data, make paid calls, deploy, or enable GitHub Actions. Update the ledger and report tested outcomes and concrete remaining blockers.
