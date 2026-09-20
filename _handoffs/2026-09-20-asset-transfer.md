# Assets to bring to the new Mac

This supplements the [new-computer handoff](2026-09-20-new-computer-handoff.md). The owner already has the API credentials and asked for the other assets to be organized. This is an asset transfer snapshot, not an app installer or an automatic restore.

## Gateway assets

[MEASURED] Source: `~/Library/Application Support/gateway-kh/` on the source Mac, inspected 2026-09-20. A private transfer copy was prepared outside Git, at `~/Desktop/Gateway-KH-Transfer-2026-09-20/`. Originals were left in place; no application was restarted. The folder's `manifest.json` records each payload's original absolute path, transfer path, size and SHA-256. The hash authenticates the **transfer payload**: for SQLite this is the online backup, not a claim that the live database has identical file bytes. The manifest names that operation separately.

| Category | What to bring | Verification / treatment |
|---|---|---|
| Voice library | `data/voice-library/` plus `data/voices.json` | [MEASURED] Ten folders, each with a reference recording, `transcript.txt`, `segments.json`, and `metadata.json`: 40 files / 9,972,153 bytes. All ten recordings match the SHA-256 stored in both their catalogue row and metadata. Keep the catalogue and folder IDs unchanged. |
| Voice choices | `data/voices_state.json` | [MEASURED] Contains `enabled`, `discovered`, and `custom` sections. Preserve account/custom voice choices; refresh provider discovery after configuring credentials. |
| Model choices and limits | `data/models_cache.json`, `data/model-limits.local.json` | [MEASURED] Present and copied. Keep owner selections and limit provenance; do not bulk-enable discoveries. Cache data is historical availability, not proof that a provider is ready on the new Mac. |
| Fleet roster and retention | `data/fleet.json`, `data/retention.json` | [MEASURED] Present and copied. A roster is not a credential store. Revalidate connections on the new Mac. |
| Optional job/output history | `data/gateway.sqlite3`, `data/artifacts/` | [MEASURED] SQLite online backup passed `PRAGMA integrity_check`; snapshot contains 23 succeeded, 9 failed, 1 cancelled jobs and no accepted/running jobs. Artifact storage contains 28 files / 5,668,323 bytes, including staging files. This is separated from the essential assets. |
| Additional old input clips | Two uploads in the checkout's older `data/artifacts/uploads/` | [MEASURED] These bytes were absent from current artifact storage; both original filenames are preserved under `03-optional-history/legacy-upload-inputs/`. They are duplicate recordings of each other, kept to preserve path provenance. The older database's nine job IDs are all present in the current database. |

[MEASURED] `providers.local.json` and `kept.json` were absent from the inspected default runtime data directory. Their absence is recorded, not replaced by invented empty settings. The repo's tracked `data/voices.json` is a shipped default, not this transfer's writable catalogue.

## Content Packs: existing material versus Gateway support

[MEASURED] Existing packs were found in **Story Studio**, not a Gateway pack library:

| Transfer folder | Original source | What is preserved |
|---|---|---|
| `02-content-packs/story-studio-library/` | `~/Library/Application Support/Story Studio KH/content-packs/` | [MEASURED] `installed/` has 544 files / 255,020,612 bytes, including **167 distinct `.sspack` archives representing 115 pack IDs and their versions**. `state.json` and `commerce-state.json` are included alongside it. |
| `04-authoring-reference/story-studio-source/content-packs/` | `~/Developer/story-studio-kh/content-packs/` | [MEASURED] **237 `pack.json` definitions in 23 collection directories**; 867 meaningful files / 193,928,241 bytes, including 320 WebP and 6 PNG preview assets. All 867 files are Git-tracked; ignored `.DS_Store` files are excluded. This is editable source/reference material, not 237 ready-to-install archives. |
| `04-authoring-reference/story-studio-source/developer-migration/codex-skills/` | The same path in Story Studio's source repository | [MEASURED] The 13 tracked files across five canonical authoring-skill packages are included, along with `scripts/developer/sync-story-authoring-skills.mjs` and `docs/operations/new-computer-migration.md`. Use the complete Story checkout for skill installation/validation; the subset here is a reference snapshot. |

[MEASURED] Story source snapshot: `fca27c955225134ae78c2ac51744c858b10772eb`. The copied pack source/skill/sync-script paths had no Git modifications. Pack kinds observed in archive manifests include Series Formula, Topic Pack, Visual Style, Thumbnail Recipe, Lettering Style, and Metadata Formula. Multiple versions are retained as preservation material; all 167 archive manifests state `lifecycle: active`, which alone does not establish current UI availability or ownership. The transfer's `pack-inventory.json` lists each archive's exact ID, version, path, kind(s), delivery profile and digest.

[MEASURED] All 167 archives passed ZIP CRC checks, member path/duplicate-name checks, and the byte-size/SHA-256 checks for every manifest-declared payload. **This does not verify schema compatibility, publisher signatures, ownership, offline access, or installation in either app.**

[MEASURED] Gateway `gateway/api.py:1181-1205` implements only the account/quote/purchase/receipt placeholders on its Content Store surface. Those routes return empty account/receipt lists and zero-credit ownership/completion assertions; they do not implement a pack library or deliver the files above. Copying Story's library into Gateway's data directory will not add a Content Packs UI. Keep it under `02-content-packs/` until the implementation follows Story's real import/trust/delivery contract.

[MEASURED, documented Story contract] `docs/operations/new-computer-migration.md:61-71` in the copied Story reference says `app-secrets.json` contains OS-protected Store public-key anchors as well as credentials. It requires a compatible Keychain migration or authenticated Store restore/revalidation. **That secret file and Keychain are not in this transfer.** The copied commerce journal and archives alone do not establish portable ownership/offline access. Do not edit receipts or bypass the checks to make the free gateway appear to own a pack.

Use installed archives for version-preserving recovery and the source collections for future authoring. Neither replaces Story projects, external project/media roots, or a full Story app migration. Follow the copied Story migration checklist if those are also moving.

## Restore deliberately

1. Clone current Gateway source and establish the fresh development baseline from the main handoff. Keep the transfer folder separate from the checkout.
2. Verify the extracted bundle with `python3 verify-transfer.py` before using it. Preserve an untouched copy. The archive is private owner data even though credential files are excluded; transfer it privately, not as a GitHub attachment.
3. With the new gateway stopped, inspect its existing Application Support data. Back it up before any merge. Do not overwrite a configured profile wholesale.
4. Restore the voice library and selected settings together under the new user's `~/Library/Application Support/gateway-kh/data/`. The transfer's `01-gateway-assets/data/` preserves that relative shape.
5. Relocate each copied catalogue `reference_path` to its verified copied recording; compare its SHA-256 to the saved digest first. Preserve the existing voice ID and all permission/license fields. Do not re-import blindly: identity and permission continuity matter. Do not infer new IDs from the current digest or enable sharing.
6. Treat `03-optional-history/` as an archive unless job history is needed. Restoring it requires checking `asset_path`, `artifacts_json`, staging paths in `params`, and any pins against the new location. Never blindly replay old work. A SQLite online backup is internally consistent; it does not make all JSON/media files a single atomic transaction.
7. Re-enter API credentials through the app. If remote Hub connections are needed, distinguish their fleet/operator tokens from cloud API keys. Configure the new Mac's own Hub separately and verify actual reachability.
8. Verify voices are readable, choices are preserved, permissions are unchanged, and optional history points to real files before enabling ordinary operation.

## Do not carry as working state

[MEASURED] The HTML console, native UI source, release notes and icon generator are already tracked in Gateway Git. `menubar/build.sh:98-114` bundles source/defaults/release notes, and `:141` calls `menubar/make_icon.py` to generate the icon. No separate manually transferred logo or font pack is needed for the current build.

[MEASURED] `menubar.json` contains a checkout path, while `menubar-state.json` contains PIDs and observed process state. Both were omitted; reconfigure the new app. `.env` and `credentials.json` were omitted as requested. Build output, `.venv`, downloaded build runtimes, generated graphs, and local model weights are not part of this transfer. Rebuild the app; install/configure Studio Hub and its model packages separately when needed.

[MEASURED] This transfer does not copy Story Studio's projects, its entire database, or the old GenStudio service into Gateway. It is not a whole-computer backup. Content Pack source/package inventories are described in this document; they must not be confused with AI model weights or generated job outputs.

## Verification and scope

[MEASURED] The preserved payload comprises 1,505 files / 475,433,061 bytes before guides, metadata and compression. Ordinary asset copies were SHA-256 compared against their originals before/after copying. SQLite used the backup API and passed its internal integrity check. The checksum verifier was tested with a matching fixture and deliberately altered/missing files: the latter fail. All 70 local links across the four affected Gateway documents resolved, and `git diff --check` passed. Application code was not changed, so generation suites and app builds were not rerun for this packaging task.

[MEASURED] A bounded private scan checked 16 known source/current Gateway secret values and selected private-key/GitHub/sk-token patterns against copied files; it found no matches. This is not an exhaustive secret audit. Credential files were excluded by the explicit copy list; voice recordings, prompts, outputs and Store proofs still make the bundle private.

Search/copy scope is Gateway's default runtime directory and relevant older checkout artifacts, Story's canonical tracked Content Pack/authoring directories, and Story's default installed Content Pack library. Unrelated project roots, backup disks, cloud-only store objects, Keychain, model weights and full app migrations are outside that scope. ZIP/hash checks establish byte integrity only; install/ownership/compatibility remains to be verified on the destination.

## Next-session instruction

> Read this file and the main handoff. Locate the extracted private transfer folder and run its checksum verifier. Inspect the new Mac's existing data before proposing a precise restore mapping. Preserve source copies, voice IDs, digest checks and private/share permissions. Do not overwrite state or replay jobs. Treat pack preservation and implementing Gateway's Content Pack delivery as separate tasks. Never commit the transfer payload or enable GitHub Actions.
