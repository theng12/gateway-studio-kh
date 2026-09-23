# HANDOFF — gateway-kh

**Read this first.** Written 2026-09-23 against **VERSION `0.2.18`**, release commit
`9b7876a`, running build `9b7876a-202609231626`.
If this file disagrees with the code, **the code wins — fix this file in the same change**.
Every path below is inside the Git checkout `repository/` unless it says otherwise.
`[MEASURED]` = observed in the session that wrote this; `[reason not recorded]` = the
project does not say why, and nobody should guess.

## WHERE THINGS STAND

| Thing | Value |
|---|---|
| Current release | **0.2.18** — `repository/VERSION`, one line, the only place to bump |
| Branch | `main` = `origin/main`, clean tree; remote `github.com/theng12/gateway-kh` |
| Running build | `/Applications/gateway-kh.app`, port **8787**, stamp `9b7876a-202609231626` (`/health` and `…/Resources/gateway/BUILD`) |
| Distribution | DMG in `repository/dist/` (`gateway-kh-0.2.18.dmg`), named by version alone |
| SSD/setup kit | `repository/dist/setup-kit/` — "copy this whole folder to each SSD/Mac" |

- **Versions are patch-only for now:** `0.2.1`, `0.2.2`, … up to `0.2.1000`. Increment the
  third number by one per shipped change; never roll to `0.3.x`/`1.0.0` until the owner says so.
- **Finished work is committed and pushed, and a release carries a DMG.** Do not call work
  done while uncommitted, unpushed, or without an installable artifact. `dist/` stays out of Git.
- **The setup kit is a snapshot, not a build output.** It currently pins GatewayKH **0.2.1**
  and Studio Hub **0.6.39** `[MEASURED]` — both far behind the current release. Rebuild it
  deliberately for a rollout; do not hand-edit it. `[reason not recorded]` for why it has not
  been refreshed.
- The port is **8787** everywhere that matters: the default in `gateway/config.py`
  and the app's `AppConfig`, this Mac's `.env`/`menubar.json`, and `README.md`'s
  "Why port 8787". It was 8000 until the owner moved it (2026-09-21, `Project
  Status.md` §10h: 8000 is the most contested dev port and Story Studio's own
  default). Every Story Studio client points at `http://<host>:8787`.

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
| SSD setup kit | `repository/dist/setup-kit/` |
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
- **A definite 4xx from the Hub is its own refusal, never an unknown-outcome hold;** only a 5xx
  may have been accepted. A **connect timeout/refusal** is an unreachable location, not a job
  failure — but a read timeout *after* the request left is still ambiguous.
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

- **Shipped in 0.2.18 (`9b7876a`):** the gateway-side **downgrade guard** (refuse/warn when a
  controller already holds a newer version) and the per-controller **`trusted_publisher`** display
  in Fleet → Locations → Release. The full `tests/test_ui.py` release gate passed **135/135** on
  this build `[MEASURED]`. The guard is proven against the contract-enforcing mock only.
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
