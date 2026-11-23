# Gateway Studio KH 0.2.305 — the first release with an updater

Backfilled 2026-11-23 from the working receipt (`$HOME/Developer/_orchestra/developer-media/gateway-studio-kh/lead/gateway-lead/0.2.305-sparkle/RECEIPT-0.2.305.md`), which stays the live copy.
Small files this receipt cites are beside it; suite logs and downloads stay untracked on purpose.

## Two facts this receipt states plainly

1. **The appcast was served with zero items while 0.2.305 was published.** The feed said "nothing to
   update to" for a version that was on the feed row and downloadable — worse than a 404, because it
   reads as an answer. Issue row **`20261123-144`**. A `appcast-serves-version` check exists but was
   not in the release plugin at the time of this release: **a known gap, named here rather than
   implied**.
2. **0.2.305 is released and installable; self-update begins at 0.2.306.** This build carries the
   pre-fix updater library and a `~/` token path, so every update check it makes is refused. The
   static proof needs no launch: the library it linked (`62ca9510…`) could not expand `~/…`,
   and the shipped plist carries `KHHoldTokenFile = ~/…`. 0.2.306 replaces both.

## Verdict

**GATE PASS** on `f95fff15f1200f42622c751457e3104501296f21` (2026-11-23T12:45:04Z). `gate-run.sh`
sha256 `bc0dbcd8c3621012aea845de04714e4295496a426b94ea928ab5c2ea0c68f8f3`, **identical before and
after** the run. Config `_orchestra/tools/gate-configs/gateway-0305.sh`.

- Artifact `Gateway Studio KH-0.2.305-f95fff15-202611231244.dmg`, sha256
  `ecd95740fb7d42076761ed2e428d06caa5c91e71bfada0376ccfde618ce007e0`, **68,502,240 bytes**.
- `scan-dmg: clean`, bound to that sha (3,163 files, scanner `scan-dmg/2026-11-20`).
- Steps included `bundle-carries-the-updater` (four plist keys, framework present,
  `codesign --verify --deep --strict`), `appcast-url-is-live`, the eight Python suites, the three
  node suites, `desktop-safety` and `commit-identity`. `idle-footprint` **UNMEASURED**.

## What changed in 0.2.305

- The app now carries Sparkle behind the `kh-sparkle` shim: `SUFeedURL`
  `https://theng12.github.io/gateway-studio-kh/appcast.xml`, `SUPublicEDKey`, `KHHoldURL`
  `http://127.0.0.1:8787/admin/api/maintenance/drain`, `KHHoldTokenFile`, `SUEnableAutomaticChecks`.
  No window, no prompt, no click: it holds work before it fetches.
- macOS 12 floor.
- **`CFBundleVersion` became the marketing version.** It had been the build stamp
  (`78311f2e-202611231147`), which string-sorts *below* the previous release's stamp, so Sparkle would
  have compared the new build as older and never offered it. The exact stamp still travels as
  `GatewayBuild`. Issue row `20261123-101`.

## Published

- Public asset: `…/releases/download/0.2.305/ecd95740fb7d-Gateway.Studio.KH-0.2.305.dmg`, created
  2026-11-23T13:04:38Z by App Publisher on Main Orchestrator's instruction — **not** by this session,
  which was refused the public-surface action.
- Pages feed: `https://theng12.github.io/kh-releases/release.json`, row `0.2.305`, `type media`, the
  approved tagline and description, `published_at 13:56:52Z`; `feed-verify.sh` **PASS** with the sha
  above, read back from Pages.
- **A row was first written to the retired host at 12:45:57Z by this session** (`publish-0305.sh`),
  because the brief named that feed and no script refused it; the row has no `url` and is inert. Nothing
  was pulled. Issue `20261123-100` covers the prose-not-a-check gap.
- Appcast item: `appcast-item.xml` beside this file. Its `edSignature` was verified with
  `openssl pkeyutl -verify -rawin` against the `SUPublicEDKey` **read from inside the published DMG**,
  over that DMG's bytes → `Signature Verified Successfully`. Enclosure `length="68502240"`, and an
  anonymous `curl -L` of it returns **200, 68,502,240 bytes**.

## Merge and retention

- Merged `--no-ff` into `main` as `d0119dd7`; `main` and `work/gateway-0305-sparkle` pushed.
- Canonical `dist/` holds **0.2.305 + 0.2.304**. 0.2.303 moved recoverably to the Desktop trash with
  a manifest (sha `1c441090…`, 67,347,969 B) before promotion; nothing deleted.

## Not done

- **The install/update proof.** No installed Gateway on the releasing Mac, and this build cannot
  update itself anyway (see fact 2). Owed on a Mac that already has Gateway, when 0.2.306 lands.
- **The appcast item's commit** was held at release time and staged verified; its enclosure and
  signature are proven above.