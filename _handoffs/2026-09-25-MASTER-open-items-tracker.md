# MASTER TRACKER — cross-project open items

> **If a row disagrees with the code, the code wins and the row gets fixed.**
> Adopted 2026-09-25 from Studio Hub's tracker. It is the single reason their file
> stays short: when a row goes stale, fix the row — don't argue with it.

**How to use this file.** Paste the **RELAY BLOCK** below to the session it names.
Each row says **who owes it**, **where the truth lives** (a file, a route), and
**how it was last verified** as a command and its observed result — so the reader
verifies instead of trusting. Closed rows carry a date and a commit, never the word
"done". Nothing here restates another project's own tracker: theirs is
`docs/cross-project-open-items.md` in the Studio Hub repo (`1ea1691`), linked, not
copied.

Last updated **2026-09-25** by GatewayKH. Current release **0.2.27** (`b440449`).

---

## RELAY BLOCK — paste this whole section to Studio Hub

Two things from us, then the rows.

### 1. Our asks, in one place — because you cannot read our repo

You were right that `_handoffs/…-chat-lane-shipped.md` was invisible to you. I read
it back: it contains **one** standing ask to you — the exposure notification, HUB-12
below. Everything else we want is in the row list underneath, and none of it points
at a file you cannot open. From here the tracker *is* the relay, so a single paste
carries every ask.

### 2. One row of yours contradicts the running build — measured, not argued

SH-11 says the field is *"live on every chat row"*. It is not in the build running
here.

```
$ PlistBuddy -c "Print :CFBundleShortVersionString" "/Applications/Studio Hub.app/Contents/Info.plist"
0.6.63
$ curl -s -H "X-Hub-Token: $TOKEN" $HUB/api/hub/catalog | jq '.models[0].lane_contract | keys'
["artifact","cancel","dispatch","modality","operation","parameters","schema","states"]
$ curl -s -H "X-Hub-Token: $TOKEN" $HUB/studio/chat/api/catalog | jq '.models[0].lane_contract | keys'
["artifact","cancel","modality","operation","parameters","schema","states"]
$ curl -s -H "X-Hub-Token: $TOKEN" $HUB/api/hub/model-packages | jq '.models[0].lane_contract | keys'
["artifact","cancel","deadline_seconds","modality","operation","parameters","runtime_seconds","schema","states"]
```

`structured_output` is absent from all three surfaces. Not a complaint — the
gateway reads it verbatim and publishes `null` until it appears, so **no gateway
release is needed when it lands**. We need one thing from you: **which version ships
it**, so our release notes say "arrives with Hub X" instead of promising it early.
Also useful: those three surfaces carry *different* key sets — the package row adds
`deadline_seconds` and `runtime_seconds`, the Hub catalog adds `dispatch`. If that is
deliberate, one line in `docs/lane-execution-contract.md` naming which surface is
authoritative for which field would save the next reader the diff.

### Rows we owe you

- [ ] **HUB-1 — default `listenHost` to the tailnet when the Mac is joined to one.**
  owes: SH · truth: `app-preferences.json` → `listenHost`, read by
  `Contents/MacOS/StudioHub`.
  last verified: 2026-09-25, six controllers → five refused `:47873` while answering
  Tailscale SSH, i.e. installed and bound to loopback only.
  Why it matters most: ~50 Macs to install, each needing a GUI tick *on that Mac*.

- [ ] **HUB-2 — name ONE supported non-interactive way to set the listen host**
  (a CLI flag, an env var honoured at launch, or a documented file write that is read
  at launch and not rewritten from cached UI state). owes: SH ·
  truth: `app-preferences.json`; the app binary reads it, and `defaults read
  local.studiohub.standalone` holds no `listenHost`, so nothing shadows it.
  last verified: 2026-09-25 → the key is a comma-separated list
  (`127.0.0.1,192.168.18.248,100.126.154.54` on this Mac).
  We write the installer step against the one you name, and not before.

- [ ] **HUB-3 — read and write the bind over the API** (`GET`/`PUT
  /api/hub/settings`). owes: SH · truth: `/api/hub/settings`.
  last verified: 2026-09-25 → the gateway can only *infer* "bound to loopback" from
  a refused socket, because the bind is not published anywhere.

- [ ] **HUB-4 — a scriptable join** (controller address + Fleet token, no tick).
  owes: SH · last verified: 2026-09-25 → joining is a per-Mac GUI step today, and it
  is the second half of provisioning a Mac by script. Blocked on HUB-2 for us.

- [ ] **HUB-5 — (optional) one "fleet mode"** = listen + start-at-login + no-sleep,
  or the `pmset` parts listed for the installer. owes: SH.

- [ ] **HUB-6 — show the live bind, not the tick; and apply a listen change when it
  is made** (or say "restart services to apply" at the tick). owes: SH ·
  truth: the Settings/Connections page.
  last verified: **proved on `terranash-1200`, 2026-09-25** → Tailscale ticked,
  settings identical to the Macs that worked, red for a whole session; stopping and
  starting its services — nothing else — brought it up in 9 ms and it stayed up. The
  preference was already right and the *running process* was still on loopback. A
  preference that silently does nothing until an unrelated restart is
  indistinguishable from a broken one, and it was reported to us as *"it might
  possibly be a bug"*.

- [ ] **HUB-7 — guard a stale `listenHost` address** (a LAN it left, a tailnet
  address from before a re-auth): does the bind fail loudly, fall back, or bind the
  rest? Say which, on the page. owes: SH · last verified: not tested — needs a Mac
  whose address changed.

- [ ] **HUB-8 — validate `lane_contract` on publish.** We treat *absent* as
  "unstated" and fall back safely; a **malformed** one is worse than absent because it
  looks authoritative. Omit what you cannot validate. owes: SH · truth: the contract's
  producer path.

- [ ] **HUB-9 — `model_limits` on non-chat lanes where it applies** (a music or
  video model with a stated maximum). We would publish it instead of our own cap.
  owes: SH.

- [ ] **HUB-10 — state per-lane `uncertain_is_terminal` semantics in prose.** The
  flag exists; our poll loop holds `uncertain` unconditionally, so we do not act on it
  yet. Name the lane and we will read it from the contract rather than hardcode a
  list. owes: SH.

- [ ] **HUB-11 — which version ships `lane_contract.structured_output`** (evidence
  above). owes: SH · last verified: 2026-09-25 → absent in 0.6.63 on all three
  contract surfaces. **The GatewayKH side is already written** (GW-6), so this is a
  version number, not work.

- [ ] **HUB-12 — tell us if model exposure becomes an approval step.** Our path to a
  model is **not** `genstudio_candidate`: the gateway withholds with its own
  `can_publish` (files / memory / dispatch state / lane served) and the owner enables
  the row. So `model_exposure.approve()` having no caller costs us nothing today — but
  if it becomes a gate, our catalogue must read it instead of inferring availability.
  owes: SH (a notification, no work) · this is the one ask that was hiding in the
  chat-lane note.

### Rows you closed, recorded so we stop asking

- ~~`modality` on `/api/hub/model-packages`~~ — closed 2026-09-25 by Hub 0.6.63.
  Verified: 24 package rows, all carrying `modality`; 23 of 23 local models placed by
  it (`modality_source: package`).
- ~~per-lane `lane_contract` on every catalog row~~ — closed 2026-09-25 by 0.6.63;
  21 of 23 local models carry one.
- ~~chat dispatchable by a Fleet credential~~ — closed 2026-09-25 by 0.6.63;
  gateway-side proof in GW-1.
- ~~node `detail` naming peer/exception/reason~~ — closed 2026-09-25 (0.6.61).
- ~~`workers[].studio_version` as the literal `"standalone"`~~ — closed 2026-09-25
  (0.6.61).
- ~~the 401 on `/studio/voice/api/transcribe/availability`~~ — closed as **ours**
  2026-09-25: re-join that node with the controller's current Fleet token (OWNER-1).
  Your self-describing refusal is what made it a one-line fix.
- ~~video~~ — shelved by the owner; nothing published; we do not schedule it.
- ~~the Story two-pass id question~~ — closed 2026-09-25 by your ruling: no id shape,
  a field. Adopted; see GW-6 and STORY-1b.
- ~~the 185.61 s image runtime~~ — closed 2026-09-25 by your correction to the
  sourced 166 s. The gateway carries `runtime_seconds` verbatim and averages nothing,
  so the corrected value is what the next discovery stores; it is also not a number we
  act on (the only runtime we read is `deadline_seconds`).

---

## A. Story Studio owes / we ask

- [ ] **STORY-1b — do you want two-pass for the local model, and is the row enough
  to decide it at runtime?** owes: Story · truth: the `GET /v1/models` chat row.
  last verified: 2026-09-25 → the row publishes `context_window_tokens 262144`
  (the checkpoint's own, source named), `max_output_tokens 4096` (a labelled worker
  fallback), `verified_token_usage false`, `supports_structured_output true`,
  `structured_output_mode null`, and `lane_contract.structured_output` as soon as the
  Hub build carrying it runs. If two-pass needs another fact, name it and we publish
  it from the producer's statement.

- [ ] **STORY-2 — the acceptance run on the local model**: an outline, a chapter, a
  thumbnail prompt set and metadata, with receipts. owes: Story ·
  last verified: not run. This is item 5 of Studio Hub's test plan; items 1–4 are done
  (GW-1).

- [ ] **STORY-4 — label a limit from `*_source_detail`**, and tell us if you want a
  machine-readable discriminator instead of a sentence. Studio Hub's caveat: the `4096`
  is **their worker's fallback**, not the model's limit — the context window is the
  model's own. owes: Story · truth: `max_output_tokens_source_detail`.

- [ ] **STORY-5 — an omitted parameter is that Mac's operator setting**, not the
  model's default (Studio Hub, 2026-09-25). The gateway forwards only what the caller
  sets. Tell us if the row should carry a statement about it. owes: Story/GatewayKH.

---

## B. GatewayKH owes

- [x] **GW-1 — remote chat dispatch proof.** closed 2026-09-25, commit `5183355` ·
  truth: `tests/test_hub_chat_lane.py` plus a live run.
  last verified: `POST /v1/chat/responses` with the local copy disabled →
  `state=succeeded`, `output_text="remote ok"`, `generation_seconds=1.485`,
  `RAN ON studio-hub-de7250e146a7` — a controller, over Tailscale, with its Fleet
  token. Closes item 4 of Studio Hub's test plan.

- [x] **GW-3 — kit pushed to the four SSDs.** closed 2026-09-25, commit `b440449` ·
  truth: `repository/dist/setup-kit/`.
  last verified: `diff -r --exclude=.DS_Store dist/setup-kit "/Volumes/KH-STUDIO-*/…"`
  → identical on all four; `shasum -a 256 -c SHA256SUMS.txt` → `2 OK` on each.

- [x] **GW-6 — publish the producer's structured-output facts.** closed 2026-09-25,
  commit `b440449` · truth: `gateway/catalog.py::_chat_row`,
  `_hub_declared_parameter`.
  last verified: `GET /v1/chat/models` → `supports_structured_output: true`,
  `structured_output_mode: null`, `lane_contract: null` (HUB-11).

- [ ] **GW-2 — implement whatever STORY-1b names**, keeping today's id as a
  `legacy_id`. Blocked on STORY-1b · truth: `gateway/catalog.py`.

- [ ] **GW-4 — re-declare the target Hub version** when a controller runs something
  newer than `0.6.63`. truth:
  `gateway/adapters/studio_hub.py::SUPPORTED_STUDIO_HUB_VERSION`.

- [ ] **GW-5 — refresh a location's models automatically when its Hub version
  changes.** Found 2026-09-25 · truth: `gateway/fleet.py` (probe) +
  `gateway/providers/registry.py`.
  last verified: a controller updated to 0.6.63 still had a cached model list from
  before the update, so **the chat model was invisible until Refresh was pressed** —
  the gateway knew the version had changed and did nothing with it. Until it exists,
  OWNER-5 is the manual step.

---

## C. Owner

- [x] **OWNER-2 — SSDs mounted and kit refreshed.** closed 2026-09-25: kit 0.2.27 on
  all four drives, SHA256-verified.
- [ ] **OWNER-1 — re-join `vengthengs-mac-studio-hub` with the controller's current
  Fleet token** (the fix for the 401). truth: that node's own app.
- [ ] **OWNER-3 — 9 nodes still offline.** last verified: 2026-09-25 →
  `summary.machines_offline: 9`, against `machines_online: 6`.
- [ ] **OWNER-4 — controllers before nodes** for Studio Hub's memory-policy change (a
  Hub older than 0.6.63 rejects `auto`). The gateway does not read or write memory
  policy, so nothing here breaks us.
- [ ] **OWNER-5 — after a Hub update on a controller, press Refresh for that
  location** so its new models are discovered. Retired by GW-5.

---

## Appendix — measurements and paths

**Fleet, 2026-09-25** — `curl -s -H "Authorization: Bearer $KEY"
http://127.0.0.1:8787/admin/api/fleet` → `locations_up 6/6`, every controller
`0.6.63`, every one `match`; `machines_online 6`, `machines_offline 9`.

**Chat lane through the gateway** — `POST /v1/chat/responses` with
`"model_id": "studio-hub::mlx-community/Qwen3.5-9B-4bit"`: local `"Red, Blue, Green"`
/ `finish_reason stop` / `usage 22/5/27` / `generation_seconds 0.573`; remote
`"remote ok"` / `generation_seconds 1.485` on `terranash-1100`. Studio Hub's own
`GET /api/hub/jobs` records them as `"modality": "chat"`.

**Local chat row** (`GET /v1/models`) — `context_window_tokens 262144` with
`source_detail "model/config.json text_config.max_position_embeddings"`;
`max_output_tokens 4096` with `source_detail "worker default: the checkpoint ships no
generation_config.json"`; `verified_token_usage false`;
`supports_structured_output true`; `structured_output_mode null`;
`lane_contract null` (HUB-11).

| What | Path |
|---|---|
| GatewayKH checkout | `/build/Developer/gateway-kh/repository/` |
| Hub lane + version declaration | `gateway/adapters/studio_hub.py` |
| Discovery (what the Hub states) | `gateway/providers/discovery.py` |
| Catalogue rows (`_chat_row`) | `gateway/catalog.py` |
| Per-controller lanes | `gateway/providers/registry.py` |
| Holds, recovery action, chat envelope | `gateway/api.py` |
| Chat lane tests (wire) | `tests/test_hub_chat_lane.py` |
| Lane contract tests | `tests/test_hub_lane_contract.py` |
| Setup kit | `dist/setup-kit/` |
| Kit on a drive | `/Volumes/KH-STUDIO-*/Video Generation Kit/GatewayKH/` |
| Controller install script | `/Volumes/KH-STUDIO-*/Video Generation Kit/Studio Hub KH/1 - Install the app.command` |
| Hub settings (the listen host) | `~/Library/Application Support/StudioHub Standalone/app-preferences.json` |
| Hub ready file (bound URL, version, pid) | `~/Library/Application Support/StudioHub Standalone/hub-ready.json` |
| Studio Hub's own tracker | `docs/cross-project-open-items.md` @ `1ea1691` (theirs) |
| Studio Hub spec | `docs/lane-execution-contract.md` (theirs) |
