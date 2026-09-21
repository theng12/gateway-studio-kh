# Private Content Packs authoring handoff

Use this brief when starting a dedicated authoring session for the Gateway
private Content Packs shelf. The destination is the local Gateway private shelf,
not a marketplace, Store restore, or Story visual gallery.

## 1. Set up the destination first

In Gateway's **Content Packs** page, inspect the prerequisite detail and
explicitly set up the private shelf before authoring. Setup creates a new,
owner-only Gateway authority and snapshots the current valid Story registry
revision and historical identities. It does not alter the original Store
account, receipts, signing identity, OS-backed trust anchors, or installed
content.

After setup, obtain a fresh Story registry reservation issued after that
baseline. The reservation must be for a new pack and family absent from the
baseline. A later private version may use an existing private pack identity,
but it still needs a newly reserved version. Never reuse an old Store identity,
copied receipt, transferred archive identity, or historical reservation as the
new authority's proof.

The authoring session receives no Gateway or provider secrets. Do not request,
paste, store, or include API keys, passwords, tokens, private account data, or
credentials in its prompt, files, evidence, or package.

## 2. Use the existing Story authoring contract

Use the existing standalone Story Studio Content Pack skill, schema, registry,
deterministic validator, and archive builder. Override its commercial defaults
for this private workflow:

- no marketplace listing, pricing, quotes, purchases, or paid generation;
- no external publication or fabricated Store ownership;
- no old-identity reuse and no copied trust evidence;
- no automatic publish, download, or install.

Each pack contains exactly one original reusable product and one supported
standalone item. The complete v1 delivery slice supports these four wire kinds:

- `series-formula` — Series Formula;
- `topic-pack` — Topic Pack;
- `visual-style` — Visual Style;
- `thumbnail-recipe` — Thumbnail Recipe.

The existing Gateway Topic Pack editor remains available for unfinished
`topic-pack` drafts. Series Formula, Visual Style, and Thumbnail Recipe work
arrives as a ready authored package input for Gateway review; do not add a
second rich editor for those kinds. Do not create bundles, Kits, Channel Kits,
subtitle styles, scene effects, overlays, lettering styles, or metadata
formulas. Unsupported kinds require a coordinated Story app update and must not
be relabelled as another kind.

## 3. Preserve provenance and assets

Keep the product original and reusable. Preserve source provenance, attribution,
licenses, declared dependencies, and package assets exactly as authored. Never
invent permission, authorship, license, compatibility, or metadata. Dependencies
must name exact already-shelved releases and resolve without missing,
conflicting, or cyclic closure.

If a package includes a cover or marketing URL, it must be a canonical HTTPS
URL. Never invent a localhost URL with a port, and do not promise that Story
will show a visual gallery. Keep the pack's real assets intact; Gateway review
may display only the supported metadata it actually receives.

## 4. Validate, build, and review deterministically

Before delivery, validate the manifest and document schema with the real Story
tools, then build the `.sspack` deterministically. Review the exact candidate
identity and evidence:

1. fresh registry reservation and post-baseline eligibility;
2. pack id, version, kind, and the one-item boundary;
3. declared files, exact bytes, hashes, and archive size;
4. provenance, license, attribution, dependencies, and preserved assets;
5. validator output and deterministic builder result;
6. archive SHA-256 and concise review notes.

Deliver the ready `.sspack` plus concise per-pack evidence to the local private
Gateway destination. Gateway preserves an import first, then shows the review
result. A reviewer must explicitly check the authorship/distribution
acknowledgement and choose **Add to shelf**. A draft or imported archive is
never auto-published. A refusal leaves the draft or preserved archive intact
for correction or review.

Shelving is private distribution only. It does not claim Store ownership,
charge credits, fabricate purchase receipts, replace an installed Story
version, or install anything. Story Studio's existing discovery and explicit
Download/Install flow remains the consumer boundary.

## Back up the private shelf identity

Setup creates three files under the Gateway data directory's
`content-delivery/` folder: `initialized`, `authority.json`, and
`signing-key.pem`. The signing key is deliberately **never regenerated** — if it
is lost, future private releases cannot be signed and Story Studio's trust
binding for them breaks. A damaged or missing identity is a recovery error, not
an invitation to set up again.

Back it up after setup, and again after important releases:

```bash
cd /path/to/gateway-checkout
.venv/bin/python scripts/backup_content_authority.py ~/Documents/gateway-authority-backup
```

The script copies all three files with mode `0600`, verifies each copy by
SHA-256, and refuses to overwrite an existing backup. Keep the result on local,
encrypted storage: **never** a cloud-synced folder, Git, a transfer bundle, or a
chat. It prints no secret material.

Restore only after real damage: stop the gateway, copy the three files back into
`content-delivery/` with mode `0600`, start the gateway, and confirm Content
Packs reports the shelf as configured and available. The backup script's own
test suite (`tests/test_content_authority_backup.py`) covers the copy rules, and
the restore path is drilled end to end on an isolated profile.
