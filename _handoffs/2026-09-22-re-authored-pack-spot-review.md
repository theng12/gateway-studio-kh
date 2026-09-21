# Spot review of the re-authored packs

**Date:** 2026-09-22 (re-run after the Wars/Treaties identity cascade)
**Verdict:** 147 rebuilt packs compared field by field against the originals.
**6 differ** — the 4 tag trims forced by Story's 12-tag catalogue limit, and the
two formulas whose Wars/Treaties reference was repointed when that merged pack was
re-published under matching slugs. **No preview asset differs by a byte.**

Produced by `store-analysis/reauthor/review_rebuilds.py` in the container (the
re-authoring tooling lives outside the Git worktree, beside `mapping.json` and
`INVENTORY.md`). It normalises only what must change — the item `id` and every
remapped reference — so any other difference is a finding, on every pack rather
than a sample. A `1.0.1` is compared against the `1.0.0` it corrects, and a pack
whose source directory is gone is compared against its preserved archive.

Every pack was compared field by field against its original, normalising only the item `id` and the remapped references. A difference is a finding, not a sample.

## Content differences

### `com.storystudio.pack.formula.archival-ledger-v2@1.0.0`

- `compatible_topic_pack_ids`
  - was: `["com.storystudio.topic.archival-ledger.states-revolutions-collapse-v2", "com.storystudio.topic.archival-ledger.money-debt-financial-institutions-v2", "com.storystudio.topic.archival-ledger.empires-trade-extraction-v2", "com.storystudio.topic.archival-ledger.wars-treaties-world-order-v2", "com.story`
  - now: `["com.storystudio.topic.archival-ledger.states-revolutions-collapse-v2", "com.storystudio.topic.archival-ledger.money-debt-financial-institutions-v2", "com.storystudio.topic.archival-ledger.empires-trade-extraction-v2", "com.storystudio.topic.wars-treaties-world-order-v2", "com.storystudio.topic.arc`
- `default_topic_pack_ids`
  - was: `["com.storystudio.topic.archival-ledger.states-revolutions-collapse-v2", "com.storystudio.topic.archival-ledger.money-debt-financial-institutions-v2", "com.storystudio.topic.archival-ledger.empires-trade-extraction-v2", "com.storystudio.topic.archival-ledger.wars-treaties-world-order-v2", "com.story`
  - now: `["com.storystudio.topic.archival-ledger.states-revolutions-collapse-v2", "com.storystudio.topic.archival-ledger.money-debt-financial-institutions-v2", "com.storystudio.topic.archival-ledger.empires-trade-extraction-v2", "com.storystudio.topic.wars-treaties-world-order-v2", "com.storystudio.topic.arc`

### `com.storystudio.pack.formula.history-explained-v2@1.0.0`

- `compatible_topic_pack_ids`
  - was: `["com.storystudio.topic.archival-ledger.wars-treaties-world-order-v2", "com.storystudio.topic.economics-ancient-civilizations-v2"]`
  - now: `["com.storystudio.topic.wars-treaties-world-order-v2", "com.storystudio.topic.economics-ancient-civilizations-v2"]`
- `default_topic_pack_ids`
  - was: `["com.storystudio.topic.archival-ledger.wars-treaties-world-order-v2"]`
  - now: `["com.storystudio.topic.wars-treaties-world-order-v2"]`

### `com.storystudio.pack.style.baroque-mythological-sky-v2@1.0.0`

- `tags`
  - was: `["baroque painting", "mythology", "gods and heroes", "cloud bank", "diagonal composition", "billowing drapery", "crimson and gold", "cherubs", "divine attributes", "chiaroscuro", "oil on canvas", "fresco", "stormy sky", "sleep narration"]`
  - now: `["baroque painting", "mythology", "gods and heroes", "cloud bank", "diagonal composition", "billowing drapery", "crimson and gold", "cherubs", "divine attributes", "chiaroscuro", "oil on canvas", "fresco"]`

### `com.storystudio.pack.style.bold-gory-cartoon-v2@1.0.0`

- `tags`
  - was: `["cartoon", "comic book", "thick outlines", "flat colour", "saturated palette", "cel shadows", "exaggerated expression", "shock face", "disgust face", "slapstick gore", "listicle", "countdown explainer", "plain backdrop", "high contrast"]`
  - now: `["cartoon", "comic book", "thick outlines", "flat colour", "saturated palette", "cel shadows", "exaggerated expression", "shock face", "disgust face", "slapstick gore", "listicle", "countdown explainer"]`

### `com.storystudio.pack.style.golden-epic-poster-v2@1.0.0`

- `tags`
  - was: `["cinematic poster", "painterly digital illustration", "hyper-detailed", "regalia portrait", "civilisation vista", "sunset", "light rays", "gold", "amber", "bronze", "crimson", "volumetric haze", "history", "cover art"]`
  - now: `["cinematic poster", "painterly digital illustration", "hyper-detailed", "regalia portrait", "civilisation vista", "sunset", "light rays", "gold", "amber", "bronze", "crimson", "volumetric haze"]`

### `com.storystudio.pack.style.inked-crosshatch-storybook-v2@1.0.0`

- `tags`
  - was: `["ink illustration", "cross-hatching", "engraving", "graphic novel", "line art", "dark teal and amber", "candlelit interior", "archive", "parchment", "stone corridor", "single light source", "dark history", "scene art", "low-fidelity tolerant"]`
  - now: `["ink illustration", "cross-hatching", "engraving", "graphic novel", "line art", "dark teal and amber", "candlelit interior", "archive", "parchment", "stone corridor", "single light source", "dark history"]`

