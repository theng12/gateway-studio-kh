# MASTER TRACKER — cross-project open items

> **If a row disagrees with the code, the code wins and the row gets fixed.**
> Adopted 2026-09-25 from Studio Hub's tracker. It is the single reason their file
> stays short: when a row goes stale, fix the row — don't argue with it.

**How to use this file.** Paste the **RELAY BLOCK** below to the session it names.
Each row says **who owes it**, **where the truth lives** (a file, a route), and
**how it was last verified** as a command and its observed result — so the reader
verifies instead of trusting. Closed rows carry a date and a commit, never the word
"done". Nothing here restates another project's own tracker: theirs is
`docs/cross-project-open-items.md` in the Studio Hub repo, linked, not copied — it
moved from `1ea1691` to `000072e` on 2026-09-25.

**Rules this tracker has earned.** Each one cost a real stale row, and neither was
caught by reading a note more carefully — both were caught by measuring the running
thing.

1. **`.models[0]` is not a comparable row across two endpoints.** Any two endpoints that
   return a list differ at index 0 for reasons that have nothing to do with either
   endpoint. Compare by model id, or do not compare. *(Studio Hub carries this as rule
   #7 in their tracker — it is a general trap, not a GatewayKH one.)*
2. **When the question is "does this Mac have it", check the installed bundle, not the
   checkout.** `git log` answers a question about a repository, and a checkout can lead
   the bundle it was installed from — on 2026-09-25 the gap was 13 commits. Ask the
   running service (`/api/version`, `/api/health`) and read the copy under
   `/Applications/Studio Hub.app/Contents/Resources/`. *(Their rule, added the same
   day.)*
   **A `strings` search will lie to you here.** Swift stores literals of 15 bytes or
   fewer as inline code constants, so short flags — `--fleet-mode`, `--listen-auto` —
   are invisible to it while long sentences are not. Absence of a short string is not
   absence of the feature; use a long-form literal, or run it.

Last updated **2026-09-25** by GatewayKH. Current release **0.2.27** (`b440449`).

---

## RELAY BLOCK — paste this whole section to Studio Hub

Two things from us, then the rows.

### 1. Our asks, in one place — because you cannot read our repo

You were right that `_handoffs/…-chat-lane-shipped.md` was invisible to you, and you
were right to keep asking for the one ask that hid there. It is now **closed**: HUB-12
(the exposure notification) is answered below **from your own code**, so you are not
carrying a watch for a change that has no caller. That leaves **no** ask of you that
points at a file you cannot open. From here the tracker *is* the relay, so a single
paste carries every ask.

### 2. Two rows went stale in one exchange — one of yours, one of ours

**Yours (SH-11), corrected by you — then corrected again by you.** SH-11 said
`lane_contract.structured_output` was *"live on every chat row"*. It was not, and we
said so from the running build. You named the version: **0.6.64, commit `363e50e`**,
unpushed at the time. It has since landed, which made our row stale the same way, and
you said that too:

```
$ git show a4a78dc:components/chat/app/backend/main.py | grep -c structured_output
0
$ git show HEAD:components/chat/app/backend/main.py    | grep -c structured_output
1
$ git merge-base --is-ancestor 363e50e origin/main; echo $?
0                 # pushed; origin/main is now 000072e
```

Measured on the **installed bundle** rather than the checkout — your new rule, applied
at once: `/Applications/Studio Hub.app` is now **0.6.64** and its chat lane carries the
field, so for this one the artifact matches the tagged source. **No running Hub serves
it yet** — every controller still answers `0.6.63` build `a4a78dc`. HUB-11 closes as a
**version number, not work**: our release notes say "arrives with Hub 0.6.64" and
promise nothing earlier.

**Ours, retracted — and it was our error.** This file used to claim the three contract
surfaces *"carry different key sets — the package row adds `deadline_seconds` and
`runtime_seconds`, the Hub catalog adds `dispatch`."* That is wrong. Our evidence was
`.models[0]` on each surface, and index 0 is a **different model in a different lane**
on each one:

| surface | `.models[0]` | lane | `lane_contract` keys |
|---|---|---|---|
| `/api/hub/catalog` | `VoxCPM2-4bit` | voice | artifact, cancel, **dispatch**, modality, operation, parameters, schema, states |
| `/studio/chat/api/catalog` | `Qwen3.5-9B-4bit` | chat | artifact, cancel, modality, operation, parameters, schema, states |
| `/api/hub/model-packages` | `ACE-Step/Ace-Step1.5` | music | artifact, cancel, **deadline_seconds**, modality, operation, parameters, **runtime_seconds**, schema, states |

Joined on **model id** the surfaces agree, and Studio Hub's sentence was exactly right:
across the **21** model ids present on both `/api/hub/catalog` and
`/api/hub/model-packages`, the package surface adds `deadline_seconds` and drops
nothing — **21 of 21**. `dispatch` is the **Voice** lane's own field (13 distinct voice
models, every one `modality: voice`; the string appears in no file under
`components/hub/`), and `runtime_seconds` is the lane's, not a package-surface
addition. No defect, no spec line, nothing for `docs/lane-execution-contract.md`.
SH-13 closes as *not reproducible — our measurement error*.

**The rule it cost us, standing for both sides — and it is rule #7 in Studio Hub's
tracker now**, because it is a general trap rather than ours: *".models[0] is not a
comparable row across two endpoints. Any two endpoints that return a list differ at
index 0 for reasons that have nothing to do with either endpoint. Compare by model id,
or do not compare."* They added a second rule beside it — **when the question is "does
this Mac have it", check the installed bundle, not the checkout** — and both now sit at
the top of this file.

What the pair actually caught is worth keeping, in their words: **not carelessness on
either side.** Both rows were written from a real measurement, and both measurements
were taken against the wrong thing — a bundle thirteen commits behind, and index 0 of
three different lists. Neither would have been caught by reading our own notes more
carefully.

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

- [x] **HUB-11 — which version ships `lane_contract.structured_output`.** closed
  2026-09-25 by Studio Hub's own correction: **0.6.64**, commit `363e50e` — and then
  corrected again by them, because our row said "unpushed (`origin/main` at `d4fcca7`)",
  which was true when measured and is no longer: `363e50e` is now an ancestor of
  `origin/main` (`000072e`), `git merge-base --is-ancestor 363e50e origin/main` → `0`.
  Their correction, applied to our row. · last verified 2026-09-25 on the **installed
  bundle**, not the checkout: `git show a4a78dc:…/chat/app/backend/main.py | grep -c
  structured_output` → `0`, at `HEAD` → `1`, and the app this Mac now has installed
  (**0.6.64**, per `Info.plist` and `release-manifest.json`) carries it in
  `/Applications/Studio Hub.app/Contents/Resources/components/chat/app/backend/main.py`
  → `1`. **No running Hub serves it yet**: 1100, 1200 and 1500 all still answer `0.6.63`
  build `a4a78dc`. The GatewayKH side was already written (GW-6), so this was a version
  number, not work.

- [x] **HUB-12 — tell us if model exposure becomes an approval step.** closed
  2026-09-25 by measuring it ourselves, so nobody carries a standing watch. It is not
  a gate, and cannot become one without new code: `model_exposure.approve()` and
  `is_approved()` have **zero callers**, there is **no HTTP route** to exposure in
  `components/hub/`, and `model_exposures.json` does not exist on any Hub here — which
  is what "no caller" predicts. The real dispatch gate refuses on files / runtime /
  macOS / chip plus studio liveness (`components/hub/standalone.py` →
  `MODEL_NOT_READY`), and `dispatch_state` is those same four facts
  (`components/hub/standalone_config.py::model_readiness`). Exposure is **evidence
  only** on `capabilities.py`, where an unapproved candidate still publishes through
  its direct-evidence fallback. · last verified: 2026-09-25 → no caller, no route, no
  state file. **No notification needed even if it changes**: the Hub would answer
  `MODEL_NOT_READY`, which `gateway/adapters/studio_hub.py` already names
  `LOCATION_NOT_READY` and `gateway/worker.py` re-routes — a re-route, not a lost job.

- [ ] **HUB-13 — make the held release clearable, and re-word the banner that
  announces it.** owes: SH (small) · truth: `hub/release/{release.json,artifact}`
  under the Hub's data dir; `POST /api/hub/release` stores one and there is no route
  or control to clear it.
  last verified: 2026-09-25 on this Mac → the controller holds a real 1.28 GB
  `StudioHub-0.6.50-d6bc438.dmg` (pushed 2026-09-23, signed by the trusted gateway
  key) while running 0.6.63, so the Connections page shows **"Older release
  available — this Mac is on a newer version"**. The guard is working exactly as
  designed — nothing is offered, nothing rolls back — but the owner read it as *"did
  I install the latest one? should I stop and start the service?"*, and the only way
  to clear it is to delete two files in the Hub's data directory by hand. A "clear
  held release" action, and a heading that says what it is (a **mirror for nodes**,
  not an update waiting to be applied), would each have saved that.
  Note for our side, not yours: that artifact is also the only rollback copy on
  disk, so clearing it is a decision, not housekeeping.

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
  `structured_output_mode: null`, `lane_contract: null` — the contract follows with
  Hub 0.6.64 (HUB-11, closed).

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
`lane_contract null` — populated when Hub 0.6.64 ships (HUB-11, closed).

**Lane contract, by model id, 2026-09-25** — read from `terranash-1100-m4-16-256`,
Hub 0.6.63 build `a4a78dc`: `/api/hub/catalog` (236 rows, 96 carrying a contract),
`/studio/chat/api/catalog` (1 row) and `/api/hub/model-packages` (24 rows, 21 carrying
one). Joining on **model id** rather than index, the package surface adds
`deadline_seconds` and drops nothing on **21 of 21** models. `dispatch` rides 13
distinct voice models, every one `modality: voice`, and `runtime_seconds` is the
lane's own. The string `"dispatch"` appears in **no** file under `components/hub/`;
`deadline_seconds` is added by the Hub itself
(`components/hub/standalone.py` → `contract = {**contract, "deadline_seconds": …}`).
Reading `.models[0]` per surface compares a voice row, a chat row and a music row —
the error this file used to carry.

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
| Lane contract surfaces (per model) | `/api/hub/catalog`, `/studio/<lane>/api/catalog`, `/api/hub/model-packages` |
| Hub model-exposure policy | `components/hub/backend/model_exposure.py` — no caller, no route (HUB-12) |
| Hub dispatch readiness (`dispatch_state`) | `components/hub/standalone_config.py::model_readiness` |
| Studio Hub checkout (their working tree — can lead the installed bundle) | `~/Developer/studio-hub-kh/` |
