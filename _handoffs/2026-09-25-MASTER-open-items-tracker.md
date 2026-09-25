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

Last updated **2026-09-25** by GatewayKH. Current release **0.2.28**.

---

## RELAY BLOCK — paste this whole section to Studio Hub

Two things from us, then the rows. **The one-line status: nothing on this list is
blocked on an answer any more.** Every row we owed you that 0.6.64 answers is closed
(HUB-1..HUB-7); HUB-11 and HUB-12 are closed; the two rows you were waiting on us for
(SH-10, SH-12) are answered below. What is left is not correspondence — it is a
**rollout**: 0.6.64 is installed on this Mac and on no controller, so the fleet still
answers `0.6.63` build `a4a78dc`.

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

- [x] **HUB-1 — default `listenHost` to the tailnet when the Mac is joined to one.**
  closed 2026-09-25 by **0.6.64** (`7fcb0fb`, *"listen on this Mac's own networks, and
  keep doing it"*): an absent `listenHostAutomatic` now means **automatic**, and
  automatic is **additive** — the saved list plus every current private and Tailscale
  address, recomputed on every launch, never removing an address. Verified in the
  shipped artifact rather than the source: `listenHostAutomatic` is a 20-byte literal in
  `/Applications/Studio Hub.app/Contents/MacOS/StudioHub`, past Swift's inline-string
  limit and therefore a valid `strings` test. **This was the row that mattered** — it is
  why 1101 and 1102 advertise stale LAN addresses.

- [x] **HUB-2 — name ONE supported non-interactive way to set the listen host.**
  closed 2026-09-25: **`--listen-auto`** is the named setter, with `--listen-host
  <comma-list>` to pin exact addresses and `--fleet-mode` for the node case. All three
  **persist** — a flag is a setting, not a one-launch override, which is the half that
  did not exist before. Verified in the tagged source at the build commit `fabe498`;
  not machine-visible, because both flag literals are 12–13 bytes and `strings` cannot
  see them (see the rules at the top of this file).

- [x] **HUB-3 — read the bind as a fact over the API.** closed 2026-09-25, **and better
  than asked**: not `/api/hub/settings` (owner-only — so a controller, the exact reader
  that needs it, cannot read it) but **`/api/health`**, reachable with a fleet
  credential, publishing `bind: {known, hosts, classes, port, loopback_only,
  reachable_off_this_mac, tailnet, wildcard}`, where `known: false` is a different answer
  from loopback and carries its reason. Verified in the shipped artifact: `_bind_facts`
  is present in the installed bundle's `components/hub/standalone.py` — 0 occurrences on
  0.6.63, 4 now. **The write half is dropped**: the gateway only ever reads a bind.

- [x] **HUB-4 — a scriptable join.** closed 2026-09-25: `PUT /api/hub/controller` on the
  node's own Hub with its Hub token (owner-only by design, so a remote caller cannot
  point a Mac at a controller), then `POST /api/hub/fleet/nodes` on the controller with
  the Fleet token. The app's Join button is a UI over those two steps, not a separate
  mechanism. Route verified present at `components/hub/standalone.py:1009`. Our kit
  writes its install step against it.

- [x] **HUB-5 — one "fleet mode".** closed 2026-09-25: **`--fleet-mode`** = listen
  automatic + start at login + start services at login, and **deliberately not sleep** —
  their reasoning is right, holding a Mac awake is a power decision with its own costs.
  **Our scope answer, which is the half they are waiting on: yes, wire `--fleet-mode`
  into the kit's install step. Sleep stays out of the flag**; where a site wants nodes
  always reachable the kit sets `pmset` separately and says so. Answered as SH-12 below.

- [x] **HUB-6 — show the live bind, not the tick; and apply a listen change when it is
  made.** closed 2026-09-25 by HUB-3 + HUB-1: the live bind is a fact on `/api/health`
  rather than a tick on a page, and automatic listening is recomputed every launch, so
  "the preference was right and the running process was on loopback" is no longer
  reachable by construction. The proof was `terranash-1200` on 0.6.63; re-prove on a
  0.6.64 node during the rollout.

- [x] **HUB-7 — guard a stale `listenHost` address.** closed 2026-09-25 by HUB-1:
  automatic is additive and recomputed every launch, so a stale address is no longer the
  whole list and an address the Mac has left cannot strand it. The narrow remaining
  question — what a deliberately **pinned** (`listenHostAutomatic: false`) stale address
  does — is still unstated, and is too small to carry a row.

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
  **Confirmed by Studio Hub 2026-09-25:** all four checks reproduce on their side —
  zero callers in `components/` *and* `packaging/`, no exposure reference in
  `standalone.py`, no state file, and the dispatch gate at `standalone.py:444` refusing
  on modality, files, runtime, macOS, chip and studio liveness with exposure absent from
  it. They verified the decisive half themselves: `_model_capability` builds availability
  from the worker's own `available` / `runtime_ready` / `memory_eligible` /
  `execution_ready` booleans and reads the candidate only as evidence, so an unapproved
  model still publishes. **The conditional is closed too, not merely unlikely:** wiring
  exposure into the dispatch gate would *reduce* what is dispatchable, and the owner's
  standing rule is the opposite — *"all models and generators should be shown and
  runnable"* — so it is on nobody's list, and adding it would be a behaviour change in
  the release notes rather than a switch thrown quietly. The watch is dropped for the
  stronger reason: a tracked row that protects a re-route is not worth a row.

- [ ] **HUB-13 — make the held release clearable, and re-word the banner that
  announces it.** owes: SH (small) · truth: `hub/release/{release.json,artifact}`
  under the Hub's data dir; `POST /api/hub/release` stores one and there is no route
  or control to clear it.
  last verified: 2026-09-25, and the **evidence has moved** — this row has now gone
  stale once, so both states are kept. **Then:** the controller held a real 1.28 GB
  `StudioHub-0.6.50-d6bc438.dmg` (pushed 2026-09-23, signed by the trusted gateway key)
  while running 0.6.63, so Connections read **"Older release available — this Mac is on a
  newer version"**. The guard worked exactly as designed — nothing offered, nothing
  rolled back — but the owner read it as *"did I install the latest one? should I stop
  and start the service?"*, which is the whole reason this row exists. **Now:** the same
  five controllers hold **0.6.64** while running 0.6.63, so the same banner should read
  "Update available" instead. **The ask is unchanged** — there is still no route or
  control to clear a held release, and the heading still does not say it is a *mirror for
  nodes*; only the example moved. Clearing is still two files deleted by hand, and still
  a decision rather than housekeeping, because that artifact is the rollback copy.
  Note for our side, not yours: that artifact is also the only rollback copy on
  disk, so clearing it is a decision, not housekeeping.

- [ ] **HUB-14 — install a pushed release without a click, once the Mac is safely
  drained.** owes: SH · truth: `StudioViews.releaseUpdateSection` (the Install button)
  and `ReleaseInstaller.live.install`.
  Why: the **push** half works and needs nobody — the gateway signs an artifact and the
  Hub verifies, stores and countersigns it on receipt. The **install** half is a button
  on Connections, on every Mac, for ~50 Macs.
  What we would build against if you name it: a per-Mac **opt-in** ("install releases
  from the trusted publisher automatically") and a state machine that (1) marks itself
  `drained`, (2) waits for in-flight work, with a stated ceiling, (3) installs the
  verified artifact, (4) restarts services, (5) clears the drain and takes work again.
  **Our side of step (1) already exists**: the gateway routes nothing to a controller
  whose `capabilities.controller.drained` is true (`gateway/fleet.py` →
  `row.get("drained") is not True`) and raises a `controller_drained` issue naming it.
  No second flag is needed from you.
  The part we will not guess: installing restarts the services and stops work in
  progress, which is *why* the click exists. We are asking for the **drain-and-wait**
  half to be explicit, not for the warning to shrink.

- [ ] **HUB-15 — get a held release onto the nodes.** owes: SH, or us — see (b) ·
  truth: `peers.release_reconciler`, a module global that nothing ever assigns.
  last verified: 2026-09-25 in **0.6.64** → a node's `GET /api/hub/release` returns
  `app_release.load_release()`, which reads **its own local files**, and the app reads
  `api/hub/release`, `release/identity` and `release/artifact` through `makeRequest`,
  which targets its **own** Hub. So a node shows nothing, and the `wake_peer` call in
  `peers.py` is dead code. Your own handoff already says it: *"built on both sides …
  never exercised across a real fleet."*
  **Two ways, and one of them is ours.**
  **(a)** You wire the reconciler: the controller forwards the same gateway-signed
  descriptor to each node's `POST /api/hub/release`, which is already fleet-reachable.
  The mirror half is now **live on all five controllers** (appendix, 2026-09-25), so
  option (a) has something real to hand out — this is no longer a design on paper.
  **(b)** **We push to the nodes directly.** The gateway already reads every node's
  address from `/api/hub/fleet/nodes` and holds the group's Fleet token, and
  `POST /api/hub/release` is in your fleet allowlist — so this needs nothing from you
  but a yes and a sanity check. See GW-7.
  **What both need first:** every node must have the **gateway's** publisher key
  installed (`POST /api/hub/release/trust`, Hub-token only, UI under Connections → App
  updates), or `accept_release` refuses with 409. Only the five controllers have it
  today.
  One question we would rather ask than assume: the app pins the key its **own** Hub
  reports on `release/identity`, and refuses a descriptor whose `controller_key_id` is a
  different key. Sending the *same* descriptor to every Mac, with each Hub
  countersigning under its own key, keeps that consistent — but if the intent is that a
  node pins the **controller's** key, the forwarding half has to say so.

- [ ] **HUB-16 — a manual listen tick silently drops every address you did not
  tick, and nothing on the page says so.** owes: SH (small) · truth:
  `native/StudioViews.swift::pinListenHosts` — *"A manual address choice — ticking
  one, unticking one, or the wildcard — **ends automatic mode**… a manual choice
  pins exactly what is ticked at that moment."*
  last verified: **2026-09-25, on three controllers at once.** The owner ticked a
  Mac's LAN address; that ended automatic mode and the list froze to loopback + LAN,
  so the **tailnet address was gone**. The gateway dials what the location was added
  with — all five are stored as `100.x` (`fleet.json`), e.g. 1200 at
  `http://100.114.4.52:47873` — so three locations read
  *"Nothing is listening on that address"*, and `locations_up` fell to 3/6.
  **The behaviour is deliberate** (an untick that silently undoes itself is worse),
  and the checkbox does show that automatic is off. What is missing is the
  *consequence*: nothing says "you just dropped the tailnet", and the failure is
  invisible from our end — the gateway only knows nothing answered.
  Ask: when a manual tick turns automatic off, say which addresses that pins, and
  name any that were dropped. One sentence at the control.
  Note for our side: **the address is what a reader needs most when a location is
  down**, and our Status cell replaced it with the reason — three rows said
  "Nothing is listening on that address" without ever naming the address. Fixed in
  0.2.29 (`menubar/FleetTabs.swift`): the cell now shows the address and the reason.

- [ ] **HUB-17 — placement follows the *modality*, not the model's own memory
  requirement, so an 8 GB voice model is sent to the biggest Mac in the group.**
  owes: SH (a policy answer; no code until decided) ·
  truth: `components/hub/backend/broker.py::_placement_key`.
  last verified: 2026-09-25, read from the source **and** from the floors the Hub
  publishes. `_placement_key` decides by artifact class: anything whose class is
  audio or video gets `_audio_placement_key` (**high-memory first**), image and
  subtitle get `_flexible_placement_key` (**smallest capable first**), and only
  chat/transcription consult the model's published tier. Both are *preferences,
  never filters* — the model's own floor already excludes a machine that cannot run
  it.
  **The floors the Hub already publishes make the case by themselves**
  (`min_unified_memory_gb`, per model, measured here):
  `Qwen3-TTS-12Hz-0.6B-Base-8bit` **8 GB**, `chatterbox-4bit` **8 GB**,
  `OmniVoice-bfloat16` **8 GB**, `Qwen3-TTS-12Hz-1.7B-Base-4bit` **12 GB**,
  `VoxCPM2-*` **16 GB**, `fish-audio-s2-pro-8bit` **24 GB**, `-bf16` **32 GB**.
  So the 0.6B voice model fits an 8 GB Mac and is still placed on the **largest**
  machine in the group, while the 1.7B and the 16 GB VoxCPM models — which genuinely
  need the big ones — queue behind it.
  **The ask, in the owner's words and stated precisely:** *"this should be model‑RAM
  requirement specific… qwen3 tts 0.6 can run on 8gb machine so it must take up the
  small ones first. but qwen3 tts 1.7b needs [more] so it will wait for freed up
  [bigger] machines."* Not "audio should go small" — **capability should decide for
  every lane**, which the model's published floor already answers.
  **And that collapses to one rule**: "smallest capable first, everywhere" gives him
  exactly this, because *capable* is already defined by the model's own floor — a
  16 GB model cannot land on an 8 GB Mac whichever key is used. The only thing the
  audio key still buys is **latency** on big machines, and the question is whether
  that is still worth spending the machines only a Z-Image-class job can use.
  Recorded because his test will show it, and the answer should not be rediscovered
  from a queue.
  *Correction to an earlier note in this file:* it framed the owner's position as
  "audio belongs on the small machines". That was imprecise and it was ours to get
  right, not his — the ask is capability-based placement, and the two concrete
  models above are the example.

### Rows you are waiting on us for

- [x] **SH-10 — `_handoffs/2026-09-25-gateway-chat-lane-shipped.md` has never been read
  by you.** closed 2026-09-25: **the one-line summary was the whole of it.** That
  document carried exactly one standing ask — the exposure notification, which became
  HUB-12 — and HUB-12 is now closed, answered from your own code. Nothing else in it
  asks you for anything. You were right to keep asking for the document rather than
  assume; never pasting it was our fault, twice over.

- [x] **SH-12 — whether GatewayKH wants `--fleet-mode` wired into its install flow.**
  closed 2026-09-25: **yes.** The kit's install step runs
  `open -a "Studio Hub" --args --fleet-mode` — the one call that makes a
  freshly-provisioned Mac reachable after a reboot with nobody opening the app, which is
  the whole reason HUB-1 existed. **Sleep stays out of scope for the flag**: your
  reasoning is right that holding a Mac awake is a power decision with its own costs.
  Where a site wants its nodes always reachable the kit sets `pmset` as its own visible
  step, so the power decision is named where it is made rather than hidden inside a flag
  that already means three things.

### Still open — and worth checking that you can see them

**HUB-8, HUB-9, HUB-10 and HUB-13 appear nowhere in your tracker**: not under *Open —
Studio Hub owes*, not under *Closed*. Four rows may be invisible to you in exactly the
way SH-10 was invisible to us, so this is written out rather than assumed — HUB-8
(validate a `lane_contract` on publish: a malformed one is worse than an absent one
because it looks authoritative), HUB-9 (`model_limits` on non-chat lanes where a music
or video model states one), HUB-10 (per-lane `uncertain_is_terminal` semantics in prose —
our poll loop holds `uncertain` unconditionally until you name the lanes), and HUB-13 (a
held release that cannot be cleared, and a banner that reads as an update waiting to be
applied when it is a mirror for nodes).

If your tracker deliberately holds only your own rows, **say so in one line** and we will
stop reading its silence as an answer. That line is worth more than the four rows.

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

- [x] **GW-4 — re-declare the target Hub version.** closed 2026-09-25 in **0.2.28** ·
  `SUPPORTED_STUDIO_HUB_VERSION = "0.6.65"`.
  **Set from a running Hub, not a changelog** — which is the whole point of the row.
  `/api/version` → `0.6.65` build `37cb07b` on this Mac, and the contract was read live:
  the chat lane's `lane_contract` carries `structured_output` (`formats: [json_object,
  json_schema]`, `enforcement: prompt_validate_repair`, `grammar_constrained: false`)
  beside the keys this file already read, and `/api/health` publishes `bind` with
  `loopback_only: false` and `reachable_off_this_mac: true`.
  **It was deliberately not set to 0.6.64** while that build was only *held* by the
  controllers and running nowhere — "measured against" is a claim, and nobody had run
  it. The row that forced this also caught a stale test: `test_studio_hub_contract.py`
  asserted `hub_version_relation("0.6.64") == newer` with the numbers written in by
  hand, so it went red the moment the declaration moved. It now derives them from the
  constant, because a test that quotes a version is a row that lies the same way.

- [x] **GW-5 — refresh a location's models automatically when its Hub version
  changes.** closed 2026-09-25 in **0.2.28** · truth: `gateway/fleet.py`
  (`refresh_models_when_hub_changes`, `hub_version_for`) + `gateway/providers/models.py`
  (`entry["hub_version"]`) + `gateway/providers/registry.py` + the Refresh route.
  The baseline is the **discovery cache**, not memory, so it survives the gateway
  restart that usually follows an update; a location with no record is refreshed once
  and then recorded, so it seeds itself instead of staying inert. A **failed** discovery
  records nothing about the build, so the next pass retries rather than marking stale
  rows current.
  last verified: `tests/test_fleet_hub_version_refresh.py` → 10 checks, covering the
  change, the repeat (no discovery loop), the unstated version (a Hub that is down must
  not trigger), the failed fetch, and one bad location not stopping the others; plus the
  write path itself. OWNER-5 is retired by this.

- [ ] **GW-7 — offer a pushed release to the node roster too, if Studio Hub prefers
  HUB-15(b).** truth: `gateway/release_publisher.publish`, which walks `fleet._read()`
  — the **controller** roster — and stops there. The node addresses already arrive on
  `/api/hub/fleet/nodes` and the group's Fleet token already opens
  `POST /api/hub/release`, so the pieces are in hand.
  ceiling, and it is a real one: a 1.34 GB artifact per node over the tailnet, and this
  push was ~19 min **per controller** at the measured rate. Worth doing only alongside
  the controller-mirror the design already names, or for the few nodes a site actually
  needs current. Blocked on their answer to HUB-15.

- [x] **GW-8 — the gateway runs one job at a time, for every lane, and publishes a
  concurrency limit of 4.** closed 2026-09-25 in **0.2.30** — every item below is
  done, and the numbers are the ones the tests hold.
  Found 2026-09-25 from the owner's own question — *"if I
  have 20 machines and 19 sit idle it's useless to me"* — and it is the single
  biggest thing between this fleet and working like one fleet.
  truth: `gateway/worker.py::_loop` (`for job in due(): execute_job(job)`, one
  thread), `store.due()` (no modality filter), `constants.ADMISSION`
  (`paid_concurrency_limit: 4`), `api.acquire_permit` (grants unconditionally).
  last verified: 2026-09-25 → 5 chat jobs, **1 running and 3 accepted at once**; two
  jobs sent to 1500 in the same second took 172.3 s and 312.4 s, and 312.4 − 172.3 is
  the second one *waiting*. **It is not one lane per modality — it is one lane
  total**: an image job blocks chat, TTS, subtitle and music.
  **And the number we publish is not the number we enforce**: Story Studio is told it
  may run 4 at once and `/v1/images/admission/permits` always grants, so its admission
  check cannot catch this. A published figure that does not describe the system is the
  defect this project keeps fixing.
  **The premise is written down and the fleet invalidates it** — `store.py`: *"fencing
  tokens, lease expiry, admission permits and failover columns because multiple
  replicas and a fleet of sites must be safe. None of that applies here."* With ~20
  Macs, a fleet of sites is exactly what applies.
  **Scoped, and the scary half is already done:**
  0. **Shipped in 0.2.30.** `gateway/concurrency.py` resolves three ceilings;
     `worker.Worker` is now a single dispatcher feeding **daemon threads** — daemon
     on purpose, because a `ThreadPoolExecutor` joins its threads at interpreter
     exit and a quit during a two-minute render would hang the process instead of
     stopping it. `store.due()` never returns a running job (`mark_running` clears
     `next_action_at`), so the reserve-then-claim order cannot wedge on its own
     work. `Worker.wait_idle()` exists because `_loop` now returns on *dispatch*.
     `tests/test_worker_concurrency.py` → 13 checks, stable over five runs:
     concurrency proven with a `Barrier` (a serial worker cannot open it), the pool
     size as a real ceiling, a cloud lane at 1 by default, a curated limit honoured,
     a Hub site sized from its own `available_worker_slots`, **a Hub reporting zero
     slots still running one** (a limit of 0 must never wedge the queue), a lane
     cap honoured, a failed claim releasing its reservation, and the published
     `paid_concurrency_limit` equal to the pool size.
     Two bugs the tests caught, both worth naming: `_positive_int` rejected numeric
     **strings**, so `GATEWAY_JOB_CONCURRENCY=7` and `GATEWAY_LANE_CONCURRENCY=image=1`
     were silently ignored and fell back to the default — and `test_job_claim`'s
     "the loop returned, therefore the job ran" became a race the moment `_loop`
     stopped executing inline. Both fixed in the same change.
  1. N workers instead of one — the claim is **already atomic** (`mark_running` is a
     conditional `UPDATE … WHERE state = 'accepted'` returning `rowcount == 1`), so
     double-claiming is handled.
  2. **Per-lane budgets**, not one global N, or eight image jobs starve chat.
  3. **Per-provider budgets.** Sites sized from the `by_operation` capacity the
     gateway already reads (`available_worker_slots`, `available_physical_machine_slots`);
     cloud lanes from stated or curated limits — see the note below.
  4. `paid_concurrency_limit` must become the sum of what is actually run, or the
     permits route must start refusing. Today it is a lie in a field Story Studio reads.
  5. **Artifact path — smaller than the code comment claims.** Paths are per job
     (`{job_id}.{ext}`, `{job_id}-{index}.{ext}`), so concurrent jobs cannot collide;
     retention has a one-hour floor so nothing fresh is pruned; `record_artifacts`
     merges into the job row, which is safe with one worker per job.
  6. Cancel and dispatch-window semantics per job (`begin_dispatch`,
     `cancel_requested`), and holds — an unknown-outcome job must not be re-claimed.
  Note on per-provider concurrency, which the owner raised and `catalog.py` already
  flagged: *"ponytail: a conservative concurrency of 1 for every TTS lane. Ceiling — a
  provider that accepts several submissions at once is under-used… Upgrade path —
  per-provider values once stated."* (`provider_submit_concurrency: 1`). **We read no
  rate-limit headers at all** — OpenAI and OpenRouter return `x-ratelimit-*` on every
  response and we ignore them, reacting only to 429s (every adapter marks 429
  retryable; several have bespoke branches). The honest sources are the same three this
  project already uses for limits: **stated** (headers, where a provider sends them),
  **curated** (`data/model-limits.local.json` pattern), **labelled default** — each
  carrying which of the three it is.

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
- [x] **OWNER-5 — after a Hub update on a controller, press Refresh for that
  location.** retired 2026-09-25 by GW-5 in 0.2.28 — the refresher now re-discovers a
  location by itself when the Hub behind it changes build, and the manual Refresh still
  works and records the same baseline.

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

**Release push, 2026-09-25 — the first fleet-wide one.** `POST
/admin/api/release/publish` with `StudioHub-0.6.64-fabe498.dmg` (1 336 353 020 bytes,
sha256 `528ebadc…`): **5 of 5 controllers accepted, HTTP 200 in 2563 s (42.7 min)**,
about 8.5 min each at ~1.6 MB/s (12.6 Mbps) on this Mac's uplink. Re-read from each
controller afterwards rather than trusting the push's own answer: identical bytes and
digest on all five, and **each `controller_key_id` different** — `e93de129`,
`35f3ae97`, `e2d837fc`, `f94424d2`, `2ce2a568` — so every controller countersigned
under its own key instead of copying ours. All five then held 0.6.64 **while still
running 0.6.63**, which is the mirror doing its job and explicitly not an install.
Cost on the gateway: RSS peaked at **8.0 GB** while the 1.34 GB artifact was base64'd
into a single 1.79 GB body — the ceiling to watch on a smaller host, and the reason a
controller-mirror beats pushing to every node.

**The pushed build was superseded within the hour — check this before the SSD run.**
While verifying GW-4, the local Hub answered `/api/version` → **0.6.65** build
`37cb07b`, and all four kit drives carry `Studio Hub.app` **0.6.65** (refreshed
21:34–21:40, `release-manifest.json` → `37cb07b`). So the 1.34 GB now mirrored on the
five controllers is **0.6.64, one release behind what the kits install**. The SSD-first
plan therefore lands 0.6.65, not 0.6.64 — and any future gateway push should be aimed
at 0.6.65. Nothing is broken by the skew; it is recorded because "what is held" and
"what the kit has" are two different facts and only one of them was checked at push
time.

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
