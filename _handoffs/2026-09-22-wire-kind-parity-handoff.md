# Handoff sent to Story: wire-kind parity for fonts and metadata-formula

**Date:** 2026-09-22
**Sent to:** Story Studio KH —
`_handoffs/2026-09-22_wire-kind-parity-lettering-and-metadata-formula.md`
in the Story checkout.
**Status:** awaiting a Story-side decision; Gateway publishes nothing of these
kinds until Story's wire contract accepts them.

## What we asked for

The private shelf refuses to publish a pack whose kind Story's wire contract
does not accept, so 39 preserved archives cannot be delivered: 20
`lettering-style` (fonts), 8 `metadata-formula`, and 11 legacy Channel Kits
which the authoring skills keep read-only on purpose.

- **Fonts are nearly done on Story's side.** Its wire kind `font_pack`, its
  archive `case 'lettering-style'` (`content-packs/archive.ts:325`) and its
  `letteringCatalogue()` (`services/lettering-catalogue.ts:80`) all exist; what
  is missing is any bridge — `font_pack` appears nowhere in `electron/server`
  outside the kind union and its parse set, so a delivered font product maps to
  no archive and installs nothing. We asked Story to confirm the mapping and
  implement delivery → install → catalogue, or to name the wire kind it wants.
- **`metadata-formula` has a pack schema but no wire kind**, so it cannot be
  parsed at all. We asked for a decision plus, if it is to be sold, a wire kind
  (proposed `metadata_formula`), frontend representation, runtime consumer and
  install path.

## The ordering rule, which is the real content of the handoff

`electron/server/lib/genstudio-content-store.ts:45-53` records that
`PRODUCT_KINDS` **is** the wire contract and that one unknown kind kills the
whole parse — the 1.4.62 `font_pack` incident took all 82 products down. So
Story must accept a kind **before** the Gateway publishes one, and we will not
publish ahead of it. Publishing first would not degrade one product; it would
empty the catalogue.

## Gateway's own side

`gateway/content_delivery.py` `SUPPORTED_KINDS` (lines 21–26) stays the narrow
side. When Story confirms a kind, we add it there and emit the agreed wire kind,
re-author the affected packs under new `<slug>-v2` identities, shelve them and
verify installation. No historical identity is ever reissued.
