# MASTER TRACKER — everything open across the three sessions

**How to use this file.** Paste it whole into the session you are talking to. Each
item has an ID; tick the box and add a line when it is done, so the same file can
go back and forth and nobody has to remember anything. If an item is not yours,
leave it unticked — it is in here so the *board* is complete, not so you do all of
it.

Last updated 2026-09-25 by GatewayKH. Everything below was measured, not
remembered; where a number is a measurement it is marked `[M]`.

---

## 0. Where things stand right now

| Thing | State |
|---|---|
| GatewayKH release | **0.2.26** (`/Applications/gateway-kh.app`, port 8787) |
| Studio Hub on the fleet | **0.6.63** on all six locations `[M]` |
| Fleet | **6 of 6 locations up**, all reading `match`; 9 nodes still offline `[M]` |
| Local LLM | runs through the gateway, **local and remote both proved** `[M]` |
| Drives | four `KH-STUDIO-*` SSDs **unmounted**, so they still carry kit **0.2.24** |

### Already shipped by GatewayKH — do not re-ask for these

- **0.2.23** — declared Studio Hub version (`SUPPORTED_STUDIO_HUB_VERSION`, published
  on `/health`), and discovery reads the modality the Hub states (package `modality`
  → catalogue `hub_modality` → engine table), reporting unplaceable engines by name.
- **0.2.24** — reads `lane_contract` (`deadline_seconds`, `states.discard_supported`),
  `model_limits` with the Hub's provenance sentence in a new `*_source_detail`, and
  `verified_token_usage`; a package the Hub refuses to place is reported **in the
  Hub's own words**.
- **0.2.25** — the **chat lane**: declared in all three places, dispatched through
  the Hub's peer surface, refusal vocabulary mapped, `generation_seconds` on the job
  envelope. Declared Hub version raised to `0.6.63`.
- **0.2.26** — the Fleet page re-reads when you return to the app (it only read on
  entry before, which made a fixed Mac look unfixed).

### Already answered by Studio Hub — recorded so nobody repeats it

- `modality` on `/api/hub/model-packages` — **done (0.6.61+, and 0.6.63 today)**
- per-lane `lane_contract` on every catalog row — **done**
- chat dispatchable with a Fleet credential, on the peer surface — **done (0.6.63)**
- `model_limits` on chat rows with provenance — **done**
- node `detail` naming peer/exception/reason — **done**
- `workers[].studio_version` real versions — **done**
- the 401 on `/studio/voice/api/transcribe/availability` — **traced; the fix is ours**
  (see OWNER-1)
- video — **shelved by the owner; nothing published, do not build** (recorded)

---

## A. Studio Hub — open items

Detail for A1–A7 is in
`/build/Developer/gateway-kh/repository/_handoffs/2026-09-25-studio-hub-listen-by-default-prompt.md`.

- [ ] **HUB-1 — default `listenHost` to the tailnet when the Mac is joined to one.**
  Why: today the default is loopback-only, so every freshly installed Mac is
  unreachable until somebody ticks a box *on that Mac*. With ~50 Macs to install,
  that is the single biggest manual step left. Loopback stays; add the tailnet
  address (never `0.0.0.0` — the tailnet is the trust boundary).
  Where: the app's settings default (`app-preferences.json` → `listenHost`).

- [ ] **HUB-2 — name ONE supported non-interactive way to set the listen host.**
  A CLI flag on `Contents/MacOS/StudioHub`, an environment variable honoured at
  launch, **or** a documented write to
  `~/Library/Application Support/StudioHub Standalone/app-preferences.json` that is
  read at launch and not rewritten from cached UI state. Tell us which, and we write
  the installer against that one — we will not have the owner hand-edit another
  app's preferences on 50 machines on a guess.

- [ ] **HUB-3 — read and write the bind over the API.**
  `GET /api/hub/settings` should carry the current `listenHost`; `PUT` should accept
  it. Then the gateway can *report* "bound to loopback" as a fact instead of
  inferring it from a refused socket, and any already-reachable Mac can be fixed
  from a distance.

- [ ] **HUB-4 — a scriptable join.** Joining a controller is a tick on that Mac
  today. With HUB-2, a whole Mac should be provisionable by one script: install,
  set the listen host, join. Tell us the command shape.

- [ ] **HUB-5 — (optional, cheap) one "fleet mode" switch** that does listen +
  start-services-at-login + no-sleep, or lists the `pmset` parts for the installer.
  One thing to document instead of five.

- [ ] **HUB-6 — show the live bind, not the tick; and apply the change when it is
  made.** **Proved today, not guessed:** controller `terranash-1200` read red for a
  whole session with Tailscale ticked and settings identical to the Macs that worked;
  stopping and starting its services — nothing else — brought it up in 9 ms and it
  stayed up. So the preference was already right and the *running process* was still
  on loopback. Either apply on change, or say **"restart services to apply"** at the
  moment of ticking. A preference that silently does nothing until an unrelated
  restart is indistinguishable from a broken one.

- [ ] **HUB-7 — guard a stale `listenHost` address.** If it holds an address the Mac
  no longer has (a LAN it left, a tailnet address from before a re-auth), does the
  bind fail loudly, fall back, or bind the rest? Say which — on the page.

- [ ] **HUB-8 — validate `lane_contract` on publish.** We treat an *absent* contract
  as "unstated" and fall back safely; a **malformed** one is worse than absent
  because it looks authoritative. Omit a contract you cannot validate.

- [ ] **HUB-9 — `model_limits` on non-chat lanes where it applies.** A music or
  video model with a stated maximum duration/dimension is the same kind of fact, and
  we would publish it rather than our own cap.

- [ ] **HUB-10 — state per-lane `uncertain_is_terminal` semantics in prose.** The
  flag exists; we do not act on it yet because our poll loop holds `uncertain`
  unconditionally. When a lane states `true`, we should settle it as terminal — tell
  us the lane and we will read it from the contract rather than hardcode a list.

- [ ] **HUB-11 — (record only) video stays shelved.** Nothing published; we do not
  schedule it; the `video` rows we see are discovered and withheld.

- [ ] **HUB-12 — (record only, already answered by us) exposure.** Our path to a
  model is **not** `genstudio_candidate`: we withhold with our own `can_publish`
  (files / memory / dispatch state / lane served) and the owner enables a row. So
  `model_exposure.approve()` having no caller costs us nothing today — but if that
  becomes an approval step, tell us, because our catalogue would have to read it
  instead of inferring availability.

Files on our side, if you want the detail:
`repository/_handoffs/2026-09-25-gateway-chat-lane-shipped.md` (what we built and
what we verified), `repository/_handoffs/2026-09-25-gateway-lane-contract-reply.md`
(the point-by-point answer to your 0.6.63 note).

---

## B. Story Studio — open items

Detail in
`/build/Developer/gateway-kh/repository/_handoffs/2026-09-25-story-studio-model-id-prompt.md`.

- [ ] **STORY-1 — the published id, and what triggers two-pass.** Studio Hub raised
  this and would not decide it for us: *"Story Studio's `lmstudio:` two-pass
  behaviour is keyed on an id YOU control, and the tracked Story source has no code
  that selects two-pass from a model id."* Questions: what is the behaviour; is the
  trigger an id shape, a provider setting, a capability field, or nothing yet; and
  if it is an id, **what exactly should we publish?** We publish
  `studio-hub::mlx-community/Qwen3.5-9B-4bit` today (the normal Hub namespace) and
  will add today's id to `legacy_ids` in the same change, so nothing saved now
  breaks. **Nothing is blocked by this** — the model runs; this only decides whether
  it switches on two-pass.

- [ ] **STORY-2 — the acceptance run on the local model.** Generate an outline, a
  chapter, a thumbnail prompt set and metadata **from the local Hub model** and
  record the receipts. (This is item 5 of Studio Hub's test plan; items 1–4 are done
  — see the appendix.)

- [ ] **STORY-3 — confirm the live row parses in your picker.** The row is in
  `GET /v1/models` now; tell us if anything in it reads wrong. Known and stated
  rather than smoothed over: this lane has **no grammar-constrained decoding**, so a
  strict-JSON call site relies on prompting plus validation, and
  `verified_token_usage: false` because we forward the worker's counts without
  re-measuring them.

---

## C. GatewayKH — open items (ours)

- [x] **GW-1 — remote chat dispatch proof.** Done 2026-09-25 `[M]`: a chat job
  submitted through the gateway was routed to `studio-hub-de7250e146a7`
  (`terranash-1100`, over Tailscale, with that controller's Fleet token) and
  returned `"remote ok"`, `finish_reason: stop`, `generation_seconds: 1.485`. This
  was the last blocked item of Studio Hub's test plan.
- [ ] **GW-2 — implement whatever STORY-1 names** (an id map, or a row field on
  every chat row), with the current id kept as a `legacy_id`.
- [ ] **GW-3 — push kit 0.2.26 to the four SSDs** once they are mounted.
  Kit source: `repository/dist/setup-kit/`.
- [ ] **GW-4 — re-declare the target Hub version** when a controller runs something
  newer than `0.6.63` (one constant, one release).
- [ ] **GW-5 — (proposed) refresh a location's models automatically when its Hub
  version changes.** Found today: the controllers' cached model lists predated their
  0.6.63 install, so the **chat model was invisible until someone pressed Refresh**
  — the gateway knew the version had changed and did nothing with it. Candidates:
  refresh on version change in the fleet refresher, or a "Refresh all locations"
  action. Not built; say the word.

---

## D. Owner actions

- [ ] **OWNER-1 — re-join `vengthengs-mac-studio-hub` with the controller's current
  Fleet token.** That is the fix for the `401` on
  `/studio/voice/api/transcribe/availability` (Studio Hub traced it: the node
  accepts only its own tokens, and the two Macs hold different Fleet tokens). The
  node is off at the moment, so the error is stale — fix it when it is next on.
- [ ] **OWNER-2 — mount the four `KH-STUDIO-*` SSDs** so the kit can be refreshed
  from 0.2.24 to 0.2.26.
- [ ] **OWNER-3 — the 9 nodes are still offline** (6 machines online, 9 offline `[M]`).
- [ ] **OWNER-4 — controllers before nodes** for Studio Hub's memory-policy change:
  a Hub older than 0.6.63 rejects `auto`, so either update controllers first or keep
  sending an explicit four-mode value. (The gateway does not read or write memory
  policy, so nothing here breaks us.)
- [ ] **OWNER-5 — after any Hub update on a controller, press Refresh for that
  location** (Fleet → Locations) so its new models are discovered. Nothing refreshes
  models on a timer — see GW-5, which would retire this step.

---

## Appendix — the measurements this file is built on

**Fleet, 2026-09-25 `[M]`**

| Controller | Tailscale SSH | Hub :47873 | Gateway |
|---|---|---|---|
| This Mac | — | open | 0.6.63 `match` |
| terranash-1100 | open | open | 0.6.63 `match` |
| terranash-1200 | open | open (after stop/start) | 0.6.63 `match` |
| terranash-1300 | open | open | 0.6.63 `match` |
| terranash-1400 | open | open | 0.6.63 `match` |
| terranash-1500 | open | open | 0.6.63 `match` |

**Chat lane, through the gateway `[M]`**

- local: `"Red, Blue, Green"`, `finish_reason: stop`, `usage 22/5/27`,
  `generation_seconds: 0.573`, `verified_token_usage: false`.
- remote (`terranash-1100`, Fleet token): `"remote ok"`, `finish_reason: stop`,
  `generation_seconds: 1.485`.
- `json_object` request returned parseable JSON first try — with the caveat above
  about no grammar-constrained decoding.
- Studio Hub's own `GET /api/hub/jobs` records chat runs as `"modality": "chat"`.

**Catalogue row for the local chat model `[M]`**

```
studio-hub::mlx-community/Qwen3.5-9B-4bit
  context_window_tokens  262144   source=provider
    detail "model/config.json text_config.max_position_embeddings"
  max_output_tokens      4096     source=provider
    detail "worker default: the checkpoint ships no generation_config.json"
  verified_token_usage   false
  controls               ["temperature", "max_output_tokens"]
```

**Where things live**

| What | Path |
|---|---|
| GatewayKH checkout | `/build/Developer/gateway-kh/repository/` |
| Gateway source (Hub lane) | `repository/gateway/adapters/studio_hub.py` |
| Gateway source (discovery) | `repository/gateway/providers/discovery.py` |
| Gateway source (catalogue rows) | `repository/gateway/catalog.py` |
| Gateway source (per-controller lanes) | `repository/gateway/providers/registry.py` |
| Gateway source (holds, chat envelope) | `repository/gateway/api.py` |
| Chat lane tests (wire-level) | `repository/tests/test_hub_chat_lane.py` |
| Lane contract tests | `repository/tests/test_hub_lane_contract.py` |
| Setup kit (drives) | `repository/dist/setup-kit/` |
| Kit on a drive | `/Volumes/KH-STUDIO-*/Video Generation Kit/GatewayKH/` |
| Controller install script | `/Volumes/KH-STUDIO-*/Video Generation Kit/Studio Hub KH/1 - Install the app.command` |
| Hub's own settings file | `~/Library/Application Support/StudioHub Standalone/app-preferences.json` |
| Hub's ready file (bound URL, version, pid) | `~/Library/Application Support/StudioHub Standalone/hub-ready.json` |
| Studio Hub spec (theirs) | `docs/lane-execution-contract.md` |
| Studio Hub handoff to us (theirs) | `docs/gatewaykh-handoff-0.6.63.md` |
