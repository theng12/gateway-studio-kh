# Extension-provider handoff — read this first

**Canonical handoff:** `/build/Developer/chrome-extensions/_handoffs/2026-11-09-gateway-extension-provider-handoff.md`

It covers:

- **Objective:** expose the three Chrome extensions (video, image, audio) as Gateway Studio KH providers/models that Story Studio can pick, with **remaining credits shown**.
- **Already built:** the video extension's third `gateway` mode, worker contract v1, mock gateway, and the captured Flow `batchexecute` contract.
- **Work in this repo:** a `chrome_extension` provider kind + adapter, the worker API (`health` / `claim` / `progress` / `result` / `fail`), provider definitions for the three extensions, credits storage/display, admin UI, and tests.
- **Remaining extension work:** `getCredits` via Flow `nzlxg`, 6s/10s model keys, and porting gateway mode to image/audio.
- **Constraints:** paid credits never auto-replay; worker tokens hashed at rest; unknown credits must not render as zero; MV3/tab visibility; do not break the existing `studio`/`standalone` modes.

**First milestone:** `chrome_extension` kind + adapter + worker routes; one text-to-video job end-to-end through this gateway; credits visible.

Read `../AGENTS.md` before changing gateway behavior.
