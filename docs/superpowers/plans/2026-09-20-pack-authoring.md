# Private Topic Pack authoring implementation plan

> **For agentic workers:** Use subagent-driven-development or executing-plans. Owner authorized continuous implementation; workers own only their assigned files and must not commit, publish or delegate.

**Goal:** Finish the offline authoring/preview/export part of F05 for Topic Packs without changing installed packs, inventing identities or weakening Story trust.

**Architecture:** Extend the existing Pack Library with private drafts. A small Node bridge calls the configured Story checkout's actual authoring registry, schema and builder. Gateway persists drafts and immutable candidates in private Application Support; the Story checkout and registry are read-only. No parallel schema or identity registry.

**Tech Stack:** Existing Python/FastAPI and HTML/JavaScript; local Node and Story's locked dependencies for optional authoring only.

**Spec:** F05 in `_handoffs/2026-09-20-open-issues.md`, Task 8 in `2026-09-20-handoff-completion.md`, and the design below.

## Design and constraints

- Scope: one `topic-pack` document per candidate. Native Pack Library already opens this HTML surface. Keep its platform-neutral system-color visual language; add an Operate-mode editor, not a redesign.
- User flow: choose a registry reservation and optional existing Topic Pack template; create a private draft; edit JSON with an explicit identity; save; validate/preview; export a candidate `.sspack`. Drafts survive relaunch, including temporarily invalid JSON. Existing authored sources/templates never change.
- Identity comes from an existing `reserved` Topic Pack record and its exact verified authoring receipt. No automatic reservations, new product identities, stock mutations, or changes to published versions. Empty reservation state explains the prerequisite and points to Story's supported registry command.
- Story path and Node executable are operator-local settings (`GATEWAY_STORY_SOURCE` and `GATEWAY_AUTHORING_NODE`); no request may select an executable, module, source path or output path. Defaults may find this Mac's existing `~/Developer/story-studio-kh` and normal Node installation.
- The bridge imports Story's `scripts/content-packs/authoring-registry.mjs`, `scripts/content-packs/lib.mjs`, and actual `ContentPackDocumentSchema`. It verifies receipts before creating sources, and rechecks registry state for preview/export. It must not copy a reduced schema or manufacture signatures/receipts/trust anchors.
- Drafts live at `DATA_DIR/content-authoring/<id>/draft.json`, mode0600 in mode0700 directories. IDs derive from the reserved key+receipt to make creation idempotent. Serialized writes and revision preconditions prevent lost updates. First successful export freezes that draft; retries download the same hash-checked candidate. Editing requires a new reservation/version.
- Bound requests/documents to 4 MiB; reject non-object, duplicate-key and nonfinite JSON for validation/export. Do not leak raw tool stdout/stderr or inherited provider credentials to subprocesses. Use a fixed argument array, no shell, finite timeout, and a bounded output contract.
- Build Store-profile candidates with Story's supported `genstudio-store-receipt-v1` builder and exact current release floor. A structurally valid candidate is not ownership, a publishable certification, or installation approval. No keys, purchases, signing, installation, paid calls or generation.
- Missing Node, source checkout, dependencies or valid reservation is an actionable prerequisite state. Saved drafts remain readable even when tools are unavailable.

## Interfaces

Node CLI: `node gateway/story_authoring.mjs <story-source> [output-path]`, one bounded JSON request on stdin and one JSON result on stdout. Actions:

- `catalogue` → `{registry_revision, reservations:[{key,item_id,pack_id,version,receipt_ref}], templates:[{key,name}]}`.
- `template` with `key` → `{document}` from a validated, single-document authored Topic Pack source.
- `reservation` with `key` → `{key,item_id,pack_id,version,receipt_ref}` after exact receipt validation.
- `validate` with `key,expected_reservation,document` → `{document,manifest}` using the shared schema and exact saved reserved identity. `expected_reservation` contains the draft's original `{key,item_id,pack_id,version,receipt_ref}`; compare with the verified registry record, never replace it with the current record silently.
- `build` with `key,expected_reservation,document` and separately supplied output path → `{sha256,bytes,pack_id,version}`; same validation plus supported builder and a saved-identity recheck immediately before output commit. Every returned identity/hash/length field is required. Temporary source directories are private, removed after this operation only.
- Errors → `{error: <safe actionable message>}`, nonzero exit. Never log raw document or environment values.

Python `gateway/pack_authoring.py` exposes `AuthoringError(message,status=422)`, `catalogue()`, `template(key)`, `create_draft(key,document_text)`, `list_drafts()`, `read_draft(id)`, `save_draft(id,document_text,revision)`, `preview_draft(id,revision)`, `export_draft(id,revision)` returning `(bytes,metadata)`. Draft shape: `{id,key,receipt_ref,pack_id,version,item_id,document_text,revision,sealed}`. Catalogue adds `available` and prerequisite `detail` when unavailable; saved draft listing is independent.

HTTP under authenticated `/admin/api/pack-authoring`: GET catalogue and `/drafts`; POST `/template` `{key}`; POST `/drafts` `{key,document_text}`; GET/PUT `/drafts/{id}`; POST `/drafts/{id}/preview` and `/export` with `{revision}`. Export returns attachment bytes, never a server path. Mutations stream-limit and strictly parse request JSON.

## Review focus

1. Receipt withdrawn/changed between draft creation and export: refuse before writing candidate.
2. Stale save/export or simultaneous requests: no lost edit, overwrite or alternate bytes under a sealed identity.
3. Missing tools or malformed documents: preserve saved draft and show an actionable failure, not an empty successful library.
4. Cross-draft delayed UI response: never overwrite the newly selected editor; dirty changes require a deliberate discard.
5. Real consumer compatibility: validate exported fixture with Story's actual archive verifier and ensure deterministic bytes; no fake ownership or install claim.

### Task 1: Shared Story bridge

**Owned files:** `gateway/story_authoring.mjs`, `tests/test_story_authoring.mjs`.

- [ ] Write failing CLI tests for valid reservation/preview/build, unknown/withdrawn receipt, mismatched ID, shared-schema rejection and unavailable dependencies.
- [ ] Implement the fixed action protocol above with read-only registry/source access and safe temporary build staging.
- [ ] Use actual Story functions in isolated fixtures; validate a deterministic exported archive with its consumer verifier. Never mutate the real registry or source packs.
- [ ] Report commands, red/green evidence, files and limits. Parent performs integration review.

### Task 2: Private draft persistence and API

**Owned files:** `gateway/pack_authoring.py`, `tests/test_pack_authoring.py`; parent owns `gateway/admin.py` routes.

- [ ] Prove missing-tools state, idempotent create, invalid-text persistence, stale revision conflict, traversal/symlink rejection and immutable export before implementation.
- [ ] Implement the Python interface above, sanitized subprocess environment, bounded execution and atomic private persistence.
- [ ] Revalidate authority for preview/export, verify returned archive digest/length and refuse sealing invalid bytes. Preserve original drafts on every failure.
- [ ] Parent adds bounded authenticated routes and HTTP tests; use isolated fixture data only.

### Task 3: Editor and integration

**Parent-owned files:** `gateway/admin.py`, `gateway/admin_ui.html`, `tests/test_pack_authoring_ui.js`, HTTP test file, README and handoff.

- [ ] Add the editor using existing styles, explicit save/dirty/validation/sealed states, readable topic preview, empty/prerequisite states and safe candidate download.
- [ ] Test original document preservation on failed/stale actions and the save/preview/export flow; inspect rendered desktop/narrow layouts with fixture-only data.
- [ ] Independently review the bridge and persistence changes; run focused checks plus existing offline e2e/native/build checks.
- [ ] Update the code-only graph, rebuild/install safely only after verification, then integrate via reviewed PR with Actions disabled. Report remaining trusted Store restoration blocker separately.
