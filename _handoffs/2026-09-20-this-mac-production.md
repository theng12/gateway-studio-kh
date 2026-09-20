# This Mac: fixed interim production build

Owner direction: continue generating content on the source Mac while the new Apple Silicon Mac develops the fixes. Keep development and production installations/data separate. This is an operating checkpoint, **not a declaration that the app is bug-free or a new release/tag**.

## Installed and running

[MEASURED] On 2026-09-20, `/Applications/gateway-kh.app` and the running localhost gateway both reported build **`214914c-202609201056`**. The serving Python executable was inside that app's `Contents/Resources/python/`; the listener was `127.0.0.1:8000`. All 33 stored jobs were terminal (23 succeeded, 9 failed, 1 cancelled); none were queued/running at this check.

[MEASURED] The installed app's 37 bundled Python sources match commit `214914c`. Its `gateway/admin_ui.html` differs from that stamped commit and matches the current checkout's HTML. Preserve the actual app archive for exact rollback; rebuilding `214914c` alone does not reproduce it.

[MEASURED] The newer local build `91b45db-202609201216` remains under `menubar/build/` and has not been installed or activated by this task. Its bundled Python/HTML sources match `91b45db`. Do not mistake source `main`, a built app, and the running app for the same version.

## Separation from development

[MEASURED, source and process] `menubar/main.swift:299-312` prefers the app's bundled Python/source; the checkout is only a fallback when the bundled runtime is missing. The current process confirms bundled execution. `menubar/build.sh:151-173` explicitly installs/replaces the app when run without `--no-install`. No app/source updater was found in the inspected Swift source. Normal Git changes on the new Mac do not replace this Mac's app.

- Use the installed `/Applications/gateway-kh.app` for this Mac's interim work. Leave its app bundle and live Application Support data alone while work runs.
- Develop/test on the new Mac using its own checkout and data profile. Prefer mock/isolated tests. Shared cloud accounts and Hub locations remain shared resources; two separate app installations do not isolate upstream work or spending.
- Do not synchronize a live SQLite database, development credentials/configuration or experimental state back into this Mac. Keep generated work in the consuming Story project or an explicit saved output location; Gateway's artifact storage has retention limits.
- Do not run `./menubar/build.sh` without `--no-install` here while preserving this build. Do not restart/quit the gateway or power down this Mac during active generation; interrupted-job recovery remains an open issue.
- Before promoting a replacement: pass required local tests, exercise the exact production workflow, preserve app/data backups, wait for no active jobs, then install and verify the new app **and serving process** report the intended build. A Git merge alone is not a production rollout.

## What was checked

[MEASURED] Read-only localhost checks returned HTTP 200 for all six authenticated catalogues: chat 81 rows, image 9, video 9, TTS 21, music 2, transcription 3. A deliberately wrong API key returned 403. These are published rows, not per-provider generation readiness measurements.

[MEASURED] Previously generated image, video, TTS and transcription outputs returned HTTP 200 through the Story-facing artifact routes and matched their saved files byte-for-byte. No new generation, paid provider call, real-Hub job, app restart, installation, or runtime-data restore was performed. Full visual interaction and new end-to-end production generation are not established by these read checks.

## Known limits and new packaging evidence

[MEASURED, audit evidence] The [open-issue ledger](2026-09-20-open-issues.md) still applies. In particular, interrupted-job recovery (B01), late cancellation completion (B02), and queued transcription input retention (B03) remain open. Native Create artifact preview/save/open has authentication gaps (U01); HTML Create/Keep mishandles already-parsed responses (U14). The production workflow has not yet been selected by the owner in this task. Story-facing API retrieval passed the read checks; that does not prove either Create UI works end to end.

[MEASURED] Strict code-signature verification **currently fails** for both the installed and newer local bundles. The diagnostic lists 529 resource differences in the installed app and 78 in the newer bundle, all `.pyc` Python bytecode files; it lists no non-`.pyc` resource differences. The earlier build-time signature pass in the main handoff was a historical observation, not a guarantee after imports/startup. The backup preserves the app as-is; it has not been re-signed or certified as a distributable release.

Recommended repair on the development Mac: prevent the launched bundled Python from writing bytecode inside the signed app, remove generated cache files from build staging before signing, and test signature integrity again after isolated startup/imports and a mock generation. Preserve normal application data under Application Support. Do not weaken signing verification to hide the issue. This task records the defect; it does not modify the production launcher.

## Rollback checkpoint

[MEASURED] The private checkpoint is under `~/Documents/Gateway-KH-Production-2026-09-20/` on the source Mac. It contains the installed app archive, `app-manifest.json`, `archive.sha256`, read-check results and these operating notes. The archive is an app/code snapshot, not a live-data snapshot. The separate earlier transfer bundle preserves assets as of its own snapshot time; neither should overwrite newer generated work.

Keep the current app running normally. If a future replacement needs rollback, stop work and inspect active jobs first, verify the saved archive, preserve the replacement and current data, then restore the app deliberately. Do not blindly roll a database back with it or replay unknown upstream work.
