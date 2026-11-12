# Product

<!-- impeccable:product-schema 1 -->

## Platform

macos

Native AppKit menu-bar app (`kh.gateway.menubar`) with the gateway's web console mirrored inside it.
The console is also reachable in a browser; the app is the everyday surface. Recorded as `macos`
because the shipped surface is native, not a web page — the schema's web/ios/android/adaptive values
have no native-desktop option.

## Users

One technical owner running the Gateway on their own Macs. They start it once, keep it in the menu
bar, and use it to see what the Mac is offering, to switch providers and models on or off, and to
diagnose a job that failed. Story Studio KH on the same network is the main client, not a second
audience with its own screens.

## Product Purpose

One OpenAI-compatible endpoint in front of every local runner and cloud provider, so other apps on
the Mac ask one address for a model instead of each integrating every provider. Success is that a
client app never has to know which runner or provider answered, and the owner can see and change
that routing without editing config files.

## Positioning

Per-provider and per-model switches, a shared job store with honest job outcomes, and a Fleet that
schedules across the Macs on the network — all behind the same OpenAI-compatible surface. A single
provider proxy could copy the endpoint but not the routing visibility, the job record or the
multi-Mac scheduling.

## Operating Context

- Runs on the owner's Macs, normally on port 8787, with the menu-bar app supervising the gateway
  service and a launch-at-login item.
- The web console (Settings, Providers, Fleet, Jobs, Voices, Models, Analytics, Content Packs) is
  the same content the native window shows; the native app wraps it and adds native actions
  (Check Gateway, Restart, Open Log, Open Data Folder).
- Fleet members are Macs on the owner's Tailnet; one Mac is the Controller.
- Releases go through the Gateway's own gate, a signed DMG, and the KH Updater/Launcher feed. The
  owner accepts a release before it is published.

## Capabilities and Constraints

- FastAPI gateway plus the AppKit menu-bar app in `menubar/`; Swift for the app and supervisor,
  Python for the gateway, a bundled python-build-standalone runtime inside the app.
- Providers are switched on individually and are off until the owner enables them; counts shown in
  the UI are live reads (`19 providers · 180 models live`), never cached guesses.
- Jobs are recorded with their real outcome; a late result is still kept, and an unknown charge
  stays unknown rather than becoming zero.
- Local-first: the gateway prefers runners on the owner's own Macs; cloud providers are opt-in.
- Fleet rows are per Mac per location, so the headline reports distinct Macs and shared rows
  separately rather than implying more machines than exist.
- The app folder is `/Applications/gateway-kh.app` and the gateway service runs inside
  `Gateway Studio KH Helper.app`; the folder rename waits on the Updater's configured path.
- No in-app updater yet: version and update controls must use the Launcher/Update Studio feed
  contract rather than a second updater.

## Brand Commitments

- Name: **Gateway Studio KH**; the background service is **Gateway Studio KH Helper**. No slug,
  repo name or lowercase identifier on any user-visible surface.
- Free app, all features unlocked, with an optional one-time donation link; never nagging.
- Plain words on screen; a control names its action and keeps that name through the flow.

## Evidence on Hand

- Live app on this Mac (0.2.243) with real data: 19 of 32 providers on, 180 models offered,
  147,582 jobs succeeded, 1,030 failed, 48 fleet rows = 43 distinct Macs.
- Screenshots of the running app, Activity Monitor and Login Items in
  `~/Desktop/All YouTube Stuff/Gateway Studio KH 0.2.243 preview/`.
- Release packets and gate receipts under `_orchestra/gateway-lead/release-*/`.
- No testimonials, benchmarks, pricing or customer claims exist; do not invent them.

## Product Principles

1. One address, honest routing: the client asks once, and what answered is visible.
2. Show live facts, never cached guesses; unknown stays unknown.
3. Off by default for anything that spends money or reaches a cloud provider.
4. The Macs are the fleet: scheduling and health are per machine, not per row.
5. Recoverable by default: retire rather than delete, and keep a way back.

## Accessibility & Inclusion

- Standard macOS editing, selection and menu shortcuts must keep working in the embedded console
  (⌘A, ⌘C, ⌘V, ⌘X, ⌘Z are dispatched through the app's main menu).
- Text and controls must stay legible in every appearance mode, and in a narrow window.
- Khmer text must not clip in any field the owner can type into.
