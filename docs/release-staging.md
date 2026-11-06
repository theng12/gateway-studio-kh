# Release files belong on the Gateway Mac

Place release files in the **Gateway release folder**, shown live on Fleet's Studio Hub and Story Studio publish forms. This folder is `DATA_DIR/releases` in the active Gateway profile; the default is `~/Library/Application Support/gateway-kh/data/releases/`. Changing the existing data-directory configuration changes this folder too.

Use Finder or the already-authorized deployment copy to place the file there **before** asking Gateway to publish. Enter its file name in the console, or pass that name as `artifact_path` to the existing publication route. A relative path is resolved inside this folder. Existing absolute deployment paths outside Downloads remain supported.

Gateway does not fetch or copy a source file from Downloads. Both Hub and Story publication reject direct Downloads paths and aliases into Downloads with HTTP 400 `RELEASE_ARTIFACT_NEEDS_STAGING`, before reading the artifact. The response explains where to place it. A paced Hub rollout refuses at creation, before storing or starting a rollout; old catch-up records pointing into Downloads are withheld with the same explanation rather than opening the file later.

The owned folder is created with mode 0700. Its path is published on the existing owner-only Fleet and release-identity responses as `artifact_directory`; no new route or generation catalogue field is added. These responses report the active profile's path, rather than assuming the browser's Mac is the Gateway Mac. An unknown folder is shown as not reported. Polls never replace a typed file name.

No byte-upload route is introduced. A server-side copy from Downloads would still need the first protected open, so it would reproduce the same permission wait. The sender must place the bytes in the owned folder through its existing authorized channel. Release/build operators using Downloads must update that intake step before using the new build; callers already passing absolute staging paths outside Downloads keep working. Story still makes its existing verified internal copy before publishing.

## Why this is separate from signing

Gateway's macOS application is currently ad-hoc signed. Its bundle ID stays fixed, but a rebuilt application's code-hash identity is not a stable certificate identity, so protected-folder permission can be requested again after an update. The fix avoids depending on that permission rather than granting broader access.

The Ed25519 App Update Key is separate: it lives in persistent Gateway data and is reused across ordinary application updates. Neither the staging change nor an application rebuild rotates it.

On 2026-11-06, the original Hub 222 publication waited at a Downloads file open for most of its 3,400.85-second hash phase. After the owner allowed the prompt, that same POST completed; its actual delta push took 4.82 seconds. All three target Macs were verified on 222 at 09:31 UTC, with both public mflux Qwen prompt limits at 8000. Exactly one publication POST was sent. A timed-out client is therefore not proof that the server stopped: reconcile existing state before considering another offer.

For this source change, the existing publication-origin and authentication requirements remain unchanged. The retired native Fleet views remain unused. No runtime file move, permission change or new publication is performed by adding this staging support.
