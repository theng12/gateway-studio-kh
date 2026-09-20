# Apple Silicon handoff completion — 2026-09-20

This is the current execution record for the inherited
[open-issue ledger](2026-09-20-open-issues.md). Historical findings remain intact;
their old source line numbers and source-Mac runtime observations are not claims
about this Mac. `[MEASURED]` means checked by the orchestrator; `[REPORTED]`
means inherited or worker evidence not yet independently confirmed.

## Boundaries and recovery

- Global owner rules are in `/build/.codex/AGENTS.md`: Ponytail and
  Graphify always; Impeccable and relevant Superpowers workflows as needed.
- Development is isolated in `worktrees/handoff-completion`, branch
  `codex/handoff-completion`, beneath the Gateway container. The original
  `repository` checkout and its unrelated `.DS_Store` modification are preserved.
- Private transfer, authoring sources and optional history remain outside Git.
  No old jobs are replayed, no voice sharing granted, no hosted Actions enabled.
- `[MEASURED]` Fresh pre-install backup:
  `/build/Developer/gateway-kh/backups/pre-handoff-install-20260920-qsDeJN`.
  Ten recording digests match both the transferred catalogue and metadata;
  all non-path catalogue fields match the transfer. Six live catalogue/settings
  files match this backup. The live job database is empty and its integrity
  check returns `ok`.

## Acceptance status

| Issues | Implemented result | Evidence / remaining boundary |
|---|---|---|
| B01, B08 | Durable unknown-outcome recovery, retry classification and asynchronous upstream ID checkpointing; no automatic resubmit | `[MEASURED]` 9 recovery tests and 3 adapter tests covering 12 async cases pass. Provider-specific reconciliation remains manual; a file-write-before-database-checkpoint crash can leave an orphan file. |
| B02 | Requested vs confirmed cancellation, guarded terminal transitions and preserved late outputs | `[MEASURED]` recovery/race tests pass; no promise of upstream cancellation. |
| B03 | Referenced uploads protected, including recovery-held inputs; pruning fails closed if protection cannot be read | `[MEASURED]` 3 retention tests pass. |
| B04–B07, B09 | Quote identity/TTL, read-time artifact 410, shared Create eligibility, actual-peer auth and disabled forwarded-header trust | `[MEASURED]` 11 HTTP contract tests, including real socket auth mismatch and location/published-ID replay, pass; 6 audit regressions pass. |
| U04, U07, U13, U14 | Browser Generate/Keep use parsed responses, exact-payload ambiguous retry, serialized status reads, provider error/retry | `[MEASURED]` 9 Node action tests pass; rendered mock Generate attaches an image and Keep becomes Kept. |
| U01, U05, U06, U08–U12, U15, U16 | Authenticated byte helper, bounded WebKit loading/retry, refreshed key injection, explicit native failures | `[MEASURED]` 2 native transport checks pass (compiled Swift and local HTTP fixtures); final integrated native inspection pending. |
| U02, U03, F01–F04 | Native drafts/recovery/voice choice/capabilities/scrolling form and Jobs outputs | `[MEASURED]` Native state regressions pass, including typed drafts, transport ambiguity and stale callbacks. Six-lane minimum-width geometry passes. Rendered native Image submits once, shows two mock outputs and previews authenticated bytes; Jobs pinning works. |
| P01–P03, P05 | Honest empty bundled voice defaults, locked dependencies/runtime identity, required icon/signature validation | `[MEASURED]` 10 packaging tests pass; signed arm64 app installed and running. Signature reverified after startup. Reproducibility is dependency/runtime identity, not byte-identical bundle output. |
| P04 | Dry-run-first copied-profile relocation with validated paths, backup and rollback | `[MEASURED]` 7 fixture tests pass, including unavailable old home. Optional live history remains archived. |
| F05 | Private archive import/inspect/inventory/download; truthful unavailable commerce responses | `[MEASURED]` 9 backend + 2 browser checks pass. Trusted delivery/install is blocked on original Store account/receipt/key authority. Gateway authoring/preview/export is separately unfinished, not inherently blocked by Store credentials. No ownership/signature/Keychain bypass. |

## Integrated evidence

- `[MEASURED]` 521 isolated mock-upstream end-to-end checks pass after backend,
  adapter checkpoint, empty-default and commerce-refusal changes. No paid calls.
- `[MEASURED]` Existing claim tests: 2 pass. Dependency compatibility: 23 packages.
- `[MEASURED]` GitHub repository Actions permissions report `enabled: false`.
  No workflow files exist in this checkout; no remote write or push was made.
- `[MEASURED]` Native Providers renders; Jobs displays two mock output variations
  and a Pin action becomes Unpin. The rendered Create overlap was corrected by
  using horizontal alignment for a vertical stack. Audio, Subtitle, Image,
  Music, Video and Chat were inspected; unavailable mock lanes disable Generate.
- A native fixture accidentally used the production Create preferences domain.
  Its changed draft preferences were restored from the fresh backup and equality
  verified. Tests now use disposable, uniquely named domains. Fixture downloads
  do not launch external media apps. Native MIME handling supplies usable local
  extensions, including PNG/MP3/SRT, instead of deriving `.bin` from route URLs.
- `[MEASURED]` Pack archive inspection accepted 145 standalone transfer archives;
  22 legacy bundle-only archives were conservatively rejected. The original 167
  archives remain untouched; none were installed into Story Studio.
- One mistakenly selected real-Hub suite performed connectivity/credential checks
  through a temporary Gateway profile, then exited because its required clip was
  absent, before any transcription/generation. It is not counted as a pass.
  The suite now requires `GATEWAY_ALLOW_LIVE_GENERATION=1` before contacting Hub;
  the default path was verified to skip. Real generation remains unverified.

## Still needed from the owner

Enter provider credentials locally in Providers after installation, never in
chat. Only configure providers you intend to use. Gateway's connection key is
separate from provider keys. Local generation needs Studio Hub's own model
weights/runtime readiness. Content Pack installation additionally needs the
original store authority or a supported publisher-signed export and Story Studio.

## Installation record

- `[MEASURED]` Installed `/Applications/gateway-kh.app`, build
  `1e516e1-202609201631`, arm64 executable and bundled Python 3.11.16.
  `/health` reports the identical build at `http://127.0.0.1:8000`.
- `[MEASURED]` Post-launch strict/deep signature verification passes. The first
  installation exposed Python bytecode writes invalidating the resource seal;
  the launcher now uses `-B`, builds remove generated caches before signing,
  and the post-launch bundle contains no `__pycache__` directories.
- `[MEASURED]` Wrong-key Story request returns 403; zero processed/live jobs.
  Ten recording SHA-256 values and six restored catalogue/settings files remain
  byte-identical to the pre-install backup after startup. No real generation.
- `[MEASURED]` The normal checkout is
  `/build/Developer/gateway-kh/repository`; local main was fast-forwarded
  to the reviewed work. The unrelated `.DS_Store` modification was retained with
  identical SHA-256. No push, publish, hosted workflow or remote mutation occurred.
- `[MEASURED]` Existing `menubar.json` was backed up and only its `gatewayDir`
  relocated to the nested repository. The bundled runtime is used in the app.
- `[MEASURED]` Providers is open for the owner's local key entry. The only
  configured provider at handoff is local Studio Hub; its health is reachable,
  but model generation and weight readiness were not tested.

The overall handoff is **not 100% closed**: F05 trusted delivery/install/reinstall
and authoring/preview/export remain open. The transfer archives and authoring
sources remain separate, private and untouched; the new archive shelf does not
silently import or install them. Trusted restoration needs original Store
account, receipt and signing-key authority (or a supported publisher-signed
export) and the actual Story Studio installation. Unknown accepted provider work
still needs manual reconciliation; no provider-specific recovery poller is claimed.

## Continuation audit

- `[MEASURED]` The Story source checkout's read-only command
  `node scripts/content-packs/authoring-registry.mjs validate` exits 0:
  `authoring registry revision 289: 271 records`. No reservation or source edit
  was made. Story's `node_modules` directory is absent on this Mac.
- `[MEASURED, source]` Story's publisher tooling can build Store-profile archives
  without a private signing key; ownership is established separately during
  installation. Therefore missing Store credentials are **not** a blanket blocker
  for authoring. The remaining Gateway editor/preview/export integration must
  reuse Story's schemas and builder, respect registry identities and immutable
  published versions, and avoid manufacturing a receipt or a new trust anchor.
- `[MEASURED]` Fresh remote inspection found source-Mac production checkpoint
  commit `dc557f8`; its documentation was merged into the development branch
  without conflicts. The fixed source-Mac app and its data were not touched.
- `[MEASURED]` The installed new-Mac app still serves build
  `1e516e1-202609201631` with zero processed jobs, and strict/deep signature
  verification passes. Documentation synchronization does not change that bundle.
- `[MEASURED]` After merging the remote documentation, fresh local checks exited
  0: e2e 521; HTTP 11; recovery 9; adapter checkpoints 3; retention 3;
  relocation 7; archive library 9; audit regressions 6; claims 2; packaging 10;
  JavaScript 11; native transport 2; Create state 1; Jobs outputs 2; layout 1.
  These are isolated/mock checks, not paid-provider or real-Hub generation tests.

## Authoring continuation — candidate, not yet deployed

- `[MEASURED]` PR #4 is merged at `2eab438`. The same isolated worktree now
  uses `codex/content-pack-authoring`; the normal checkout still has only its
  unrelated `.DS_Store` change, with its recorded digest preserved. Earlier
  “no push” statements above describe their checkpoints, not the later merge.
- `[MEASURED]` Story's locked dependencies are now present (installed with
  lifecycle scripts disabled). Registry revision remains 289 with 271 records
  and **zero reserved Topic Packs**. The earlier missing-dependencies observation
  is superseded; no reservation was invented or registry record changed.
- `[MEASURED]` The uninstalled candidate implements private Topic Pack drafts,
  shared-schema preview and Store-profile archive export. The real isolated
  HTTP → Python → Node → Story builder → consumer-verifier check passes; the
  staged bundled-Python smoke passes with identical download retries and zero
  generation jobs. These happy-path results do not establish completion:
  independent review found crash-recovery, original-receipt binding, stale
  editor-state and bounded-read defects. Fixes and re-review remain required.
- `[MEASURED]` The installed app remains build `1e516e1-202609201631`, healthy
  on loopback 8000 with zero processed jobs and a valid strict/deep signature.
  Ten recording hashes and all six restored catalogue/settings JSON files
  match the pre-install backup. No candidate installation or live data change
  has occurred during the authoring fix pass.
- `[MEASURED]` Global instructions now explicitly assign Astra orchestration
  and independent verification, Luna straightforward work, Terra cross-file
  work and Sol at no more than high reasoning for integrity/security/lifecycle.
  Ponytail and Graphify remain mandatory; Graphify is code-only and ignored.
- F05 stays open until both the authoring fixes are verified and the separate
  trusted delivery/install/reinstall acceptance is satisfied. Provider API
  credentials, registry reservations and Store ownership/signing authority are
  three distinct prerequisites; supplying model-provider keys does not grant
  pack ownership. Enter secrets only in local settings, never in a handoff or Git.

### Authoring review gate cleared

- `[MEASURED]` Independent review and two bounded correction passes cleared the
  original six findings plus FIFO-open blocking and raw sealed-flag recovery.
  Preview/export now bind the saved reservation at Node entry and precommit;
  candidate recovery requires exact durable metadata and bytes; persisted reads
  are bounded/no-follow/nonblocking; editor refresh is coherent and guarded.
- `[MEASURED]` Final source checks pass: Python authoring 21, Node bridge 12,
  HTTP 5, real Story integration 1, JavaScript 21, isolated e2e 521, packaging 10,
  archive library 9; 23 installed Python packages are dependency-compatible.
  Existing upstream deprecation and intentional duplicate-ZIP warnings remain.
- `[MEASURED]` Fresh private backup before installation:
  `/build/Developer/gateway-kh/backups/pre-authoring-install-20260921-DVfK1s`.
  Previous app signature, SQLite online-backup integrity and exported native
  Create preferences verify. No live application data was replaced.
- `[MEASURED]` Code-only Graphify update: 2,517 nodes / 6,235 edges. The graph is
  ignored and no paid model extraction ran. The installed app is still the old
  build until a later installation record explicitly supersedes it.
- Rendered desktop/narrow inspection previously passed. A later two-editor
  stale-export attempt stalled in browser automation at confirmation and is not
  claimed as a pass or application failure; automated state tests and real HTTP
  integration cover the corrected behavior. No independent visual signoff.
