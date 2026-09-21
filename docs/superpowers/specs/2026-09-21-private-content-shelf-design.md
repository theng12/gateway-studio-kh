# Gateway private Content Packs

Owner-approved product direction, 2026-09-21: Gateway is the workshop and shelf; an authoring session produces consistent packages; Story Studio discovers shelved packs and downloads/installs through its one configured Gateway endpoint. This is private content distribution, not a marketplace or a restore of the old Store authority.

## Experience

Gateway's existing Pack Library becomes a Content Packs page with clearly separate authoring drafts, shelved releases and preserved archives. A short creation brief can be copied into an authoring session that follows the existing standalone Content Pack skill and registry. Existing Topic Pack draft editing remains available. Imported packages are validated and reviewed before an explicit Add to shelf action; upload alone never publishes anything. Show actual prerequisite/validation failures and immutable pack/version identity. Story discovers compatible shelved products and its existing Download/Install action activates a chosen version. Discovery never auto-installs.

Use the existing quiet administration UI, accessible labels, escaped text, status messages and pending/error states. Do not add a second rich editor for every content kind. A ready package produced by the dedicated session is a first-class input.

## Architecture and boundaries

Reuse `.sspack`, Story's deterministic builder/schema validator, registry identity receipts, and the existing account/public-key/restore/download protocol. Gateway will be a genuine NEW single-owner private publishing authority for NEW locally authored packs. It does not impersonate the original Store, sign historical purchases, charge credits, or turn a copied archive into ownership. Its local account and Ed25519 identity are durable, private and independent of the generation API token; token rotation revokes API access, not existing pack identities.

Explicit setup snapshots the current valid Story registry revision and historical pack/family identities. New shelf submissions must match a real registry receipt issued after this baseline, for a pack and family absent from the baseline; exact byte/hash/schema/identity checks apply. An existing private pack may publish a new independently reserved version. This deliberate conservative boundary prevents legacy transferred Store packs or old authored stock from being automatically reissued. No copied trust evidence or user-supplied key bootstraps this authority. No authority or keys are created on a read, import, app launch or test against live data.

Authority setup requires an existing nonempty Gateway API key, local Node and valid Story authoring tools. Explicit Gateway admin setup creates an owner-only durable account/signing identity. Missing or damaged identity after setup fails closed; never regenerate silently. Keep it under Gateway's private data, outside Git and generation-artifact pruning. Existing Store receipts, signing identity and Story OS-backed anchors are not modified. Story's existing account/origin-bound multi-restore journal and OS-backed anchor remain the trust boundary; prove compatibility rather than adding a bypass.

New publications require a positive authorship/distribution acknowledgement, a supported standalone manifest with exactly one item, no bundles, no undeclared/executable files and a maximum 50 MiB archive. Story's real validator is authoritative, not Gateway's lightweight archive inspection alone. Derive display metadata from the validated content; preserve declared license and provenance, and never invent permission. Dependencies, when declared, must resolve to exact already-shelved releases; reject missing/conflicting/cyclic closure. Same pack/version with different bytes is a conflict; identical retries are idempotent. Version ordering is numeric semver, not lexical. Never delete previous releases or change an installed Story version as a shelving side effect.

Initial complete delivery slice supports Story 1.4.213's four standalone wire kinds: Series Formula, Topic Pack, Visual Style and Thumbnail Recipe. Its current commerce parser does not expose subtitle-style, scene-effect, overlay, lettering-style or metadata-formula. Reject those with a clear coordinated-app-update requirement; do not mislabel them as Font Packs or Kits. Their authoring/archive preservation remains separate. No new Channel Kits.

## HTTP and storage

- Preserve `/admin/api/content-library` as an archive-only shelf: its entries do not acquire ownership merely by import.
- Add `GET /admin/api/content-shelf` for setup state, prerequisite detail and real published releases; never expose a secret.
- Add `POST /admin/api/content-shelf/setup` with JSON `{ "acknowledge": true }` for explicit durable initialization.
- Add `POST /admin/api/content-shelf/publish` with JSON `{ "archive_sha256": "…", "acknowledge_authorship": true }` to qualify and publish an already-preserved archive.
- Add `POST /admin/api/content-shelf/drafts/{draft_id}/publish` with JSON `{ "revision": 1, "acknowledge_authorship": true }` to export the saved draft, preserve its exact candidate and qualify it through the same publisher.
- Admin mutations require existing admin authentication and strict JSON/non-simple requests. Unknown/extra fields and absent acknowledgement fail closed.
- Implement existing authenticated `/v1/content/account`, `/products`, `/shelves`, `/public-key`, `/receipts`, `/entitlements/{family}/releases` and `/downloads` from the durable private shelf. Unconfigured remains `CONTENT_STORE_NOT_CONNECTED`.
- Quotes and purchases remain unavailable. Published packs are private owner entitlements with `purchase_id: null` and an empty purchase-receipt list, never fabricated orders. Signed restore statements attest only to actual shelf releases.
- Download grants last 900 seconds, bind account/release/archive/bytes, and are verified on every archive read. Archive URLs are absolute same-Gateway-origin, not an arbitrary forwarding target. Do not trust Host input to send grants/credentials to an attacker. No redirects or upstream network calls.
- Never return 402. Preserve existing generation behavior and credentials.
- Use atomic durable writes, owner-only modes, process-safe publication locking, bounded reads and symlink refusal. Public catalog/receipt responses stay within Story's 1 MiB limit; no unbounded lists or silent truncation. Stale/missing/changed archive bytes cannot remain downloadable.

Production signing uses a maintained platform primitive (Node `crypto` already required by authoring), not handwritten cryptography. Canonical JSON must match Story's exact sorted-key UTF-8 protocol. The production code must not depend on Story's test fixtures.

### Frozen admin response contract

`GET /admin/api/content-shelf` and successful setup return `{configured, available, detail, authority, supported_kinds, releases}`. `configured` means an intact durable authority exists; `available` means prerequisites are usable; `detail` is a safe human-readable string; `authority` is null or `{account_id, key_id, public_key_fingerprint, created_at, baseline_revision}` (public identifiers only). `supported_kinds` contains the four hyphenated document kinds. Each release has `{release_id, pack_id, version, archive_sha256, bytes, title, kind, current, available, published_at}`; `kind` is the hyphenated document kind. Listing a corrupt release may retain its identity but must set `available:false` and never claim current download integrity. Empty arrays are real empty state, not substitutes for errors.

Successful publication returns `{release: <same release row>, replayed: <boolean>}`. Repeated identical publication is `replayed:true`. All refusals use an appropriate non-402 HTTP status and a safe string `detail`; no file contents or secrets. The UI refreshes shelf and archive state after publication and does not derive ownership from an archive row.

## Acceptance

An isolated synthetic registry and two versions of a real schema-valid package go through setup, authoring/import, review/shelve, HTTP discovery, signed restore, grant issuance, exact-byte download and the REAL Story installer. After restart/revalidation, installed content remains usable; v2 is offered without silently replacing v1 and is activated only on explicit install. Prove tampering, invalid signatures/grants, wrong API key, expired grants, key rotation, identity collisions, historical-identity submission, failed validation, missing dependency, concurrent/idempotent publication and failed durable writes do not publish bad state. Existing archive behavior and generation e2e tests stay green.

Use temporary Gateway support/data/env/Hub-ready paths and temporary Story data/registry. The test-only OS storage double may be used ONLY within the existing isolated Story test harness; production Keychain behavior stays unchanged. Never run paid generation or a real Hub suite. An isolated installer test is not a claim that the owner's live packs were restored.

Ship/build verification follows source integration and independent review. Back up existing app/profile before any installed-app replacement; report built vs running identity honestly. Do not activate the new live authority, replace credentials, reserve a real example product, or install a synthetic test pack into the owner's profile.
