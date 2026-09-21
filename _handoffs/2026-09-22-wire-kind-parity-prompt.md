# Prompt to send to the Story Studio KH session

Copy everything between the rules into the Story session.

---

You are working in the **Story Studio KH** repository. **gateway-kh** (the private
Content Packs shelf that serves the owner's own packs to Story over one
`/v1/content/*` endpoint) is asking you for a small, specific piece of work, and
for one product decision. Full detail is in
`_handoffs/2026-09-22_wire-kind-parity-lettering-and-metadata-formula.md` in this
repository — read it, then act. Summary of what it needs:

## Context you need

The Gateway re-homes the owner's historical Content Pack library under **new**
identities (`<slug>-v2`, new family and SKU — it never reissues a historical
Store identity). It publishes only kinds that Story's **wire contract** already
accepts, because of a rule you documented yourself:

> `electron/server/lib/genstudio-content-store.ts:45-53` — `PRODUCT_KINDS` **is**
> the wire contract, and *one unknown kind kills the whole parse*. The 1.4.62
> `font_pack` incident: the frontend learned `font_pack`, this set did not, and
> the first catalogue containing a font pack took all 82 products down.

So the Gateway must not publish a new kind before Story accepts it. Publishing
first would not degrade one product; it would empty the catalogue.

Four kinds work today and 63 re-authored packs are already shelved and
install-tested: `series-formula`, `topic-pack`, `visual-style`,
`thumbnail-recipe`. Two kinds cannot be delivered, and this is the whole ask.

## Ask 1 — fonts: implement the missing bridge (small, high value)

Story already has three of the four layers for fonts:

- wire kind `font_pack` — `genstudio-content-store.ts:52` and `:56-62`;
- frontend representation — `frontend/content-store/library-organisation.ts`,
  `frontend/content-store/catalog-presentation.ts`,
  `frontend/composables/useContentStore.ts`,
  `frontend/components/content-store/ContentStoreFilters.vue`;
- archive preview handling — `case 'lettering-style':` at
  `electron/server/content-packs/archive.ts:325`;
- a runtime catalogue — `letteringCatalogue()` at
  `electron/server/services/lettering-catalogue.ts:80`, which already reads
  `lettering-style` catalog entries beside built-ins.

What is missing is the **bridge**: `font_pack` appears nowhere in
`electron/server` outside the kind union and its parse set, so a delivered
`font_pack` product maps to no archive and installs/activates nothing.

Do one of:

1. Confirm `font_pack` is the wire kind for `lettering-style`, implement
   delivery → install → `letteringCatalogue()`, and **name the released minimum
   Story version** that carries it; or
2. Name the wire kind you want instead, and the Gateway will emit that.

20 preserved `lettering-style` packs are waiting on this.

## Ask 2 — `metadata-formula`: make a product decision

Story's pack layer supports `metadata-formula` (`content-packs/schemas.ts`,
`content-packs/project-pins.ts`,
`electron/server/__tests__/metadata-formula-catalogue.test.ts`), but the wire
contract has no such kind, so a delivered metadata-formula product **cannot be
parsed at all**.

Decide whether Story will sell it. If yes: add the wire kind (the Gateway
proposes `metadata_formula`), the frontend representation, the runtime consumer,
and the install path, and name the released minimum version. If no: say so
plainly — the Gateway will leave its 8 archives preserved and undelivered rather
than push a kind that cannot land.

## Not asked for (just confirming the rule)

`subtitle-style`, `scene-effect` and `overlay` are in the pack schemas but not
the wire contract, and this library has none of them. No action needed; the same
rule applies if they are ever sold.

## What the Gateway will do on your answer

Add the agreed kind to `gateway/content_delivery.py` `SUPPORTED_KINDS`
(lines 21–26), map pack kind → wire kind in one place, re-author the affected
packs under new `<slug>-v2` identities, shelve them, and verify installation
through Story's own installer. It will not publish ahead of your accepting
release.

## What to return

- A decision on each ask (accept / reject / different wire kind).
- If accepted: the implementation, and the released Story version ("minimum
  app version") that accepts the kind.
- Any correction to the file:line claims above — if `font_pack` is already
  bridged somewhere we missed, say where and the Gateway will test it instead.

## Acceptance criteria (joint)

1. A catalogue containing a `font_pack` product parses without throwing, lists
   in the Store UI, installs, and the installed style appears in
   `letteringCatalogue()`.
2. The same for `metadata-formula` once its wire kind exists.
3. No historical identity is delivered or signed; only `<slug>-v2`.
4. Gateway's `/v1/content/products` and Story's parse agree on the kind spelling
   for every published pack.

## Evidence

- 272 historical archives → 266 distinct bytes → 180 packs; 141 deliverable
  today, 39 blocked (20 `lettering-style`, 8 `metadata-formula`, 11 legacy
  Channel Kits, which the authoring skills deliberately keep read-only).
- Shelf state: 66 release rows / 64 current products across the four accepted
  kinds; a re-authored `series-formula` and `topic-pack` both installed through
  Story's installer with `activated: true`.
- Inventory: `gateway-kh/store-analysis/INVENTORY.md`; identity map:
  `gateway-kh/store-analysis/reauthor/mapping.json`.
