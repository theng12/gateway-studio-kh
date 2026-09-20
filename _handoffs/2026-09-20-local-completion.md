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
| P01–P03, P05 | Honest empty bundled voice defaults, locked dependencies/runtime identity, required icon/signature validation | `[MEASURED]` 9 packaging tests pass; final app build/install pending. Reproducibility is dependency/runtime identity, not byte-identical bundle output. |
| P04 | Dry-run-first copied-profile relocation with validated paths, backup and rollback | `[MEASURED]` 7 fixture tests pass, including unavailable old home. Optional live history remains archived. |
| F05 | Private archive import/inspect/inventory/download; truthful unavailable commerce responses | `[MEASURED]` 9 backend + 2 browser checks pass. Full trusted delivery/install and authoring/preview/export are NOT complete: original store account/receipt/signing authority is unavailable. No ownership/signature/Keychain bypass. |

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

Pending final native integration, independent review and signed build. Do not
read the backup or source-test results above as proof that the installed app is
already running.
