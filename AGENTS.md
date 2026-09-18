# gateway-kh

One API in front of every local Studio Hub location and 18 cloud providers, so
**Story Studio KH points at a single URL** (`http://127.0.0.1:8000`) and its model
pickers fill. Story Studio's own architecture contract requires it to call
exactly one endpoint and treat it as GenStudio — this is that endpoint. Story
Studio needs **zero code changes**, only `genstudio_url` and an API key.

Read `README.md` for the design; this file is the part that is expensive to
rediscover.

## Contract traps — each cost real debugging time

- **Never return 402.** Story Studio holds a credit lock keyed on the API key. If
  it ever sees 402 and no balance is published, that hold **never clears** and the
  key is dead permanently. A provider running out of funds surfaces as a settled
  `failed` job. The e2e suite asserts that no response in a whole run is 402.
- **Publish the shape of a priced catalogue with every amount at zero.** Do not
  omit price fields: Story Studio's parsers build explicit object literals and
  **drop any row whose price fields are absent**, which empties the picker
  entirely. `credits_per_*: 0`, `minimum_credits: 0`, `price_version: "flat"`,
  `chargeable: true`, and a balanced `0/0/0` settlement triple are all
  load-bearing.
- **`freshness` is a hard gate.** `stale: true` disables every model, and a copy
  older than 30 minutes is refused. Computed from the last catalogue rebuild, not
  process start.
- **`finish_reason` must be a clean stop** — `length` is refused as truncated.
- **Artifact URLs stay on the API origin** (relative paths only).

## Publish what is known, and nothing else

The single most productive rule to audit against, taken from Story Studio's own
notes: *never let "the answer is none" and "I could not work out the answer"
produce the same value, and never invent a number the customer will mistake for
the model's own.*

Every field a provider does not state is published as **`null`**, never as a
plausible default. Auditing for this found, in order: `context_window_tokens: 0`
on 37 rows, `max_output_tokens` hardcoded to `8192` for every model on every
provider, `supports_vision: false` for every model while 273 of OpenRouter's 445
accept images, `provider_name` missing on video and keyed differently on
transcription, transcription claiming English-only *and* auto-detect, TTS
languages published as stringified Python dicts, and Google's
`inputTokenLimit`/`outputTokenLimit` being discarded.

**When a provider states it, read it.** Providers spell the same figure
differently — `context_length` vs `inputTokenLimit`, `top_provider.max_completion_tokens`
vs `outputTokenLimit`, `maximum_text_length_per_request` — so read every spelling
before concluding a provider said nothing.

**Changing an int to `null` is a breaking change for the reader.** It made an
unreachable fallback in Story Studio live (their `normalizeMaxOutputTokens`
substitutes 8,192 and prints it as the model's own figure). The field-change
audit sent with the handoff is the pattern: list every field whose *guarantee*
changed, not just its value.

**One value must stay invented:** TTS `maximum_input_characters` — Story Studio
calls `.toLocaleString()` on it unconditionally, so `null` would crash the page
rather than degrade it. Per model where the provider states one (ElevenLabs
publishes it), 25,000 otherwise, documented in the handoff as theirs to move.

## Discovery ≠ availability

A provider publishing 300 models must not put 300 rows in the picker. Nothing
reaches Story Studio until it is enabled. The local Studio Hub is the sole
exception (`auto_enable_discovered`), and that is deliberate. A provider whose
kind has no adapter stays discoverable and configurable but **its models are
withheld** — offering a model that fails on use is worse than not offering it.
`IMPLEMENTED_KINDS` in `providers/definitions.py` is the single source of truth,
and `adapters/__init__.py` asserts at import that its table agrees.

## Ports

- **8000** is this gateway, chosen because it is Story Studio's *default*
  `genstudio_url` — so no configuration is needed and a settings reset is
  harmless. The tests read the configured port rather than assuming one.
- **The older GenStudio KH API used 8000 too.** As of 2026-09-18 it is
  **stopped and disabled**: `launchctl bootout` plus `launchctl disable
  gui/$(id -u)/com.genstudiokh.local-api`, so it will not return at login either.
  Its LaunchAgent had `KeepAlive: true`, which is why a plain `kill` brought it
  straight back — stopping it for good needs the disable, not a signal.
  To restore: `launchctl enable gui/$(id -u)/com.genstudiokh.local-api` then
  `launchctl bootstrap gui/$(id -u)
  ~/Library/LaunchAgents/com.genstudiokh.local-api.plist`.
- **Curated limits**: `data/model-limits.local.json` supplies values for lanes
  that publish none (24 chat rows across six providers). It **fills gaps only** —
  a provider's published value always wins, or a stale file would quietly beat
  live data.

## Locations are providers

A Studio Hub location added on the Fleet page is also a **provider**: its models
are discovered and work is dispatched through the same `studio_hub` adapter,
which takes a per-provider address and token. **Standalone and controller both
work** — nothing gates on the role; it is read only to display it. Story Studio
must never learn a second address: the gateway is the only thing that knows the
locations.

Secrets live in one place, the credential store (`0600`); `fleet.json` holds only
the roster. Lookups of "the providers" must go through
`registry.definitions()`, not the static `PROVIDERS_BY_ID` map — four separate
bugs came from iterating the static map and silently omitting derived providers.

## Verification

```bash
.venv/bin/python tests/test_e2e.py              # 122 checks, isolated mock upstream
.venv/bin/python tests/test_ui.py               # 84 checks, drives the real app
.venv/bin/python tests/test_fleet_standalone.py # a real Hub used as a location
```

`test_e2e.py` is fully isolated (`GATEWAY_DATA`/`GATEWAY_APP_SUPPORT` to a temp
dir, `GATEWAY_NO_MIGRATE=1`, random port, own key) — it cannot disturb the live
config. `test_ui.py` needs **Accessibility permission** and skips its
Hub-dependent checks with a stated reason when Studio Hub is stopped, because a
stopped Hub is an environment state, not a gateway defect.

Always rebuild and reinstall before claiming the app works:
`./menubar/build.sh`, then check `/Applications/gateway-kh.app` is newer than the
sources. **"It builds" is not "it works."**

## Working rules

- Prefer the **graphify** graph (`graphify-out/`, gitignored, rebuildable) over
  grepping for architecture, caller and impact questions. Keep it current with
  `graphify extract . --update` after structural work.
- **A page must never be able to resize the window.** A section pinned straight
  to the host propagates its content's *required* height outwards, and AppKit
  satisfies it by growing the NSWindow — the Fleet page did exactly that, to
  1162pt on a 1080pt screen, putting its bottom edge and resize corner off-screen
  where they could not be reached. A page whose content has a required height
  (tables with minimums, forms, dashboards) wraps its body in an `NSScrollView`
  with a `FlippedView` document, as `FleetSection` and `OverviewSection` do. Pages
  that fill the pane (Jobs, Voices, Providers) need no scroller, because a table
  with a flexible height has nothing to propagate. Verify with: force the window
  to 700pt, visit every section, and confirm the height does not change.
- Mark deliberate simplifications with a `ponytail:` comment naming **the ceiling
  and the upgrade path** — a curated exception list, a flat default, a fixed pool
  size. Unmarked, the next reader cannot tell a considered trade-off from an
  oversight.
- **The Hub's `TextGuidance`** (`soft_max_chars`, `chunking`) is authoritative
  about local voice engines but is **not served over its API**. Our local
  families are `auto-split` with no soft max; the hard-capping ones (Bark 150,
  Orpheus 170) are not exposed. If one ever is, the flat 25,000 default becomes
  wrong.
