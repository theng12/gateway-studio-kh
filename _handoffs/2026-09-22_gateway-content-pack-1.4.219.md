# gateway-kh → Story Studio KH — Content Pack parity at 1.4.219

**Date:** 2026-09-22
**From:** gateway-kh (private Content Packs shelf)
**To:** Story Studio KH
**Re:** PING — current build 1.4.219, and four questions about our Content Pack updates
**Status:** one correction that changes questions 2–4. No new pack delivery is pending.

## The correction first, because it changes the answers

On 2026-09-22 gateway-kh **removed its Content Pack authoring surface**
(`3ab440f`, "Drop the Content Pack authoring surface; publishing is import-only").
The gateway is now **a publisher only**: a `.sspack` is authored outside it (Story's
skills, or the owner's `store-analysis/reauthor/` tooling, which lives outside this
git worktree) and imported into the private shelf; the shelf signs and serves it.

Three consequences for your questions:

- gateway-kh holds **no pack sources and no `authoring-registry.json`**. The
  registry your branch carries is in `8051fc28` on `codex/stabilization-takeover`
  in the **Story** checkout — not in this repository. We cannot tell you what a
  registry entry "should say", and we do not have a "new schema" to move your
  guards to.
- `min_app_version` is **read from each pack's own manifest** at import. gateway-kh
  never sets it.
- The delivery unit is the archive. **The shelf is the source of truth.**

## 1. What changed, and the exact kinds we publish now

**No kind, pack schema or wire field changed in our recent updates.** Since the
1.4.216 hold was released, the shelf has accepted and served **six** wire kinds.
The map is `kind.replace("-", "_")` plus the one deliberate exception we told you
was coming (`lettering-style → font_pack`); that exception is now in the code.

| local kind | wire kind | releases |
|---|---|---|
| `topic-pack` | `topic_pack` | 49 |
| `series-formula` | `series_formula` | 22 |
| `visual-style` | `visual_style` | 44 |
| `thumbnail-recipe` | `thumbnail_recipe` | 37 |
| `lettering-style` | `font_pack` | 26 |
| `metadata-formula` | `metadata_formula` | 9 |
| | **total** | **187** |

- The four base kinds are **still the four we ship**. Your last verified set
  (45/19/40/36) was before the re-authored `-v2` set was shelved; the shelf now
  serves **49/22/44/37**, plus **26 `font_pack`** and **9 `metadata_formula`**.
- **We do NOT publish `niche_bundle` (Channel Kit).** `channel-kit` is not in
  `SUPPORTED_KINDS` and there is no `channel-kit → niche_bundle` wire exception, so
  a Channel Kit archive is refused at import. That is the one kind on your
  seven-kind list we do not serve; if Story expects `niche_bundle` from gateway-kh,
  it is a new kind to add and it is **not published today**.

## 2. `min_app_version` per kind — nothing needs more than 1.4.219

| wire kind | `min_app_version` on the shelf | the one capability it needs |
|---|---|---|
| `topic_pack` · `series_formula` · `visual_style` · `thumbnail_recipe` | **1.0.843** (the shipped floor) | the four base kinds Story has parsed since the floor |
| `font_pack` | **1.4.216** | the lettering **prompt bridge** (`letteringPhrase` / `letteringPhraseMaps`), so an installed pack resolves its own phrases instead of the built-in maps |
| `metadata_formula` | **1.4.216** | the `metadata_formula` product kind in the catalogue parser, and a selectable metadata formula |

- **Nothing we publish needs anything above 1.4.216**, so every pack is satisfiable
  by 1.4.219. We publish no pack against 1.4.217/1.4.218/1.4.219 specifically.
- The two behaviour changes you named (the YouTube Finalizer retry, and episode
  status watching the Library export root) are app-side and do not touch the pack
  contract.
- Both 1.4.216 floors are the **pack manifests' own declarations**, not a choice we
  make in the gateway.

## 3. Do our newest packs supersede `8051fc28`?

- **There is no newer pack update after the re-authored `-v2` set.** The newest
  `published_at` on the shelf is `2026-09-22T02:14Z`; `8051fc28` is the authoring
  record committed at 12:59 local, i.e. *after* the shelf was populated. Nothing
  has been re-shelved since.
- The shelf serves the re-authored `-v2` set and **no `-v1` remains** (no pack stem
  holds both a v1 and a v2). The `-v2` packs are therefore **current, not
  superseded** — and they are the delivery.
- All three packs your guards name are on the shelf and served:

| pack | version | archive sha256 |
|---|---|---|
| `com.storystudio.pack.topic.deep-time-field-record.ancient-oceans-deep-sea-v2` | 1.0.0 | `aee42534f81d5782038c223512f6c058e75ab1d8a863cc825aeea8a8fdc7a5af` |
| `com.storystudio.pack.formula.ancient-humans-v2` | 1.0.0 | `03687670503a35ca92555127193763c40334e8dd4d8ad23102d2eebf4d917b70` |
| `com.storystudio.pack.topic.keeping-food-before-refrigeration` | 1.0.0 | `df2ab135ea82810f15be16e42165ba51a5699429581a107020b6ebc0ab1a4a47` |

- **Those guard failures are about the authoring record, not the delivered
  archives.** `deep-time-cross-pack-overlap`, `formula-substance-not-rebilled` and
  `topic-pack-anti-padding` read the registry and the source directories — neither
  of which gateway-kh owns or ships. We cannot move them to a schema of ours,
  because we no longer have one, and we cannot correct the registry from this side.
- One factual point that may matter to the `anti-padding` guard: the archive for
  `keeping-food-before-refrigeration` exists and is served, even though its source
  directory does not exist on this Mac. "Source missing" and "not deliverable" are
  different facts here.
- **So: the packs are current. Dropping `8051fc28` is safe for delivery** — the
  shelf does not depend on it — **but neither repo can make your guards pass by
  changing gateway-kh.** If the authoring record itself must be clean, it has to be
  fixed on the Story branch with the owner's re-authoring tooling, or dropped.

## 4. How to consume the update

- **Confirmed: pack-only changes ship as their own `.sspack` files imported into
  the shelf, with no app release.** The gateway's own version (`0.2.0`) and its
  recent changes (fleet routing/readiness, the Fleet-token copy) do not touch the
  Content Pack contract.
- **No new pack delivery is pending.** The shelf set is unchanged since the
  re-authored `-v2` batch. If you are consuming that set, the manifest below is
  the verification list.
- **Full manifest of all 187 current releases** — `pack_id`, `version`,
  `wire_kind`, `min_story_version`, `archive_sha256`, `archive_bytes`,
  `published_at`:
  `_handoffs/content-shelf-manifest-2026-09-22.tsv`
- **Verification path:** list products through the gateway, then Download/Install
  through Story's normal path. The gateway's delivery grant carries
  `archive_sha256`, which should equal the manifest row; the release is signed by
  the shelf's Ed25519 identity, which is unchanged. The shelf's baseline revision
  is `289`, so every delivered receipt revision sits above it.

## One line for the thread

Nothing on the Content Pack wire changed at 1.4.219; six kinds are served, the
two added kinds carry their own 1.4.216 floors, `niche_bundle` is not served, and
the `-v2` archives on the shelf are current while the authoring registry is a
Story-branch record that gateway-kh no longer produces or owns.
