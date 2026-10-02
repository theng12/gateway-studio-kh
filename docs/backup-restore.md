# Backups: what, where, and how to restore

Settings → **Backups** in the Gateway app. Code: `gateway/backup.py`, tests: `tests/test_backup.py`.

## What is in a backup

Only what cannot be recreated (about 170 MB today, almost all of it the Content Packs):

| In the archive | On this Mac |
|---|---|
| `gateway/.env` | `~/Library/Application Support/gateway-kh/.env` (the API key Story Studio uses) |
| `gateway/data/release-publisher.pem` | Hub release signing key — every Hub pinned its fingerprint |
| `gateway/data/content-delivery/` | Content Pack shelf identity (`signing-key.pem`, `authority.json`, `initialized`) |
| `gateway/data/content-library/` | the Content Packs: every released `<sha256>/archive.sspack` with its `metadata.json` (the shelf cannot serve a release without them) |
| `gateway/data/credentials.json` | provider API keys and the controllers' fleet tokens |
| `gateway/data/fleet.json` | fleet roster: controller ids, labels, addresses |
| `gateway/data/voices.json`, `gateway/data/voice-library/` | the Gateway's voice catalogue, reference clips, transcripts |
| `hub/voice/voices/` | this Mac's Studio Hub voices (`~/Library/Application Support/StudioHub Standalone/voice/voices/`) |
| `manifest.json`, `SHA256SUMS` | file list, sizes, sha256, Gateway version, host, date |

Not included: job history, analytics, generated audio/images, caches, settings, models, apps.

The file is **not encrypted**. It is created readable only by you (mode 600). Treat it like the
keys it holds: do not share it or post it anywhere.

## Where it goes

One file `kh-backup-YYYYMMDD-HHMMSS.tar.gz`, copied to each place that is ticked and present:

- iCloud Drive → `KH Backups/`
- every attached `KH-STUDIO-*` drive → `KH Backups/` (nothing else on the drive is touched)
- this Mac → `~/Developer/_backups/`

Each place keeps the newest 5 (setting; each is ~160 MB with the Content Packs, so mind the free space). Older backups are moved to `~/Developer/_trash/kh-backups/`
(SSD: the drive's own `_trash/kh-backups/`, or deleted if the drive has no `_trash`). Only files
named `kh-backup-…tar.gz` are ever pruned.

**When:** Back Up Now, or tick "Back up every night at 03:00" (off by default). A Mac asleep at
that time backs up when it is next awake. A Telegram alert is sent through
`gateway.notifier.notify("backup", text)` when that module is present (it is skipped silently otherwise).

## Restore with the app

Settings → Backups → **Import / Restore…** → pick the file. The Gateway checks every file against
the manifest first (a damaged file is refused and nothing changes), then asks to confirm. Each
current file that differs is moved to `~/Developer/_trash/kh-backup-restore-<time>/` before the
backed-up copy is written. Files that are not in the backup are left alone. Then restart the
gateway and Studio Hub.

## Restore by hand (no app needed)

```bash
mkdir ~/restore && cd ~/restore
tar -xzf "/path/to/kh-backup-20261002-030000.tar.gz"   # or double-click it in Finder
shasum -a 256 -c SHA256SUMS                             # every line must say OK
```

With the Gateway quit, copy each file to the path in the table above (`gateway/` maps to
`~/Library/Application Support/gateway-kh/`, `hub/` to `~/Library/Application Support/StudioHub Standalone/`),
then `chmod 600` the key files. After the move to another Mac with a different username, also run
`scripts/relocate_profile.py` (see GATEWAY-MOVE-CHECKLIST, step 3d): `voices.json` stores full paths.

## After the Gateway moves to its own Mac

The Hub voices are read straight from disk, so they are only included while the Gateway and the
Studio Hub voices share a Mac. Once they are on different Macs this Mac's Hub voices drop out of
the backup (shown as "Not on this Mac"). Keeping them would need a small Hub-side export route
(e.g. `GET /api/voices/export` returning a tar of `voice/voices/`) that the Gateway fetches with the
fleet token.
