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

> **2026-09-26 — the two paste-ready messages are now separate files, and they
> supersede the ceiling ask in this block:**
> - **Studio Hub:** `_handoffs/2026-09-26-studio-hub-two-constants-prompt.md`
> - **Story Studio:** `_handoffs/2026-09-26-story-studio-ceiling-and-scenes-prompt.md`
>
> **The one-line version:** the owner has decided to raise **both** Hub constants —
> `FALLBACK_MAX_OUTPUT_TOKENS` `4096 → 16384` and `PEER_PROXY_TIMEOUT_S` `900 → 1800` —
> in the same build, because it is **exactly margin-neutral** with today (76 % on M1,
> 65 % on M2, 51 % on M4, the identical percentages) and it is what carries a
> **30-scene prompt request** that 8,192 cannot. **Nothing is needed from this gateway
> or from Story Studio** — the Hub build change re-discovers, republishes and
> recomputes every downstream threshold by itself. The earlier ask of 8,192 was the
> largest value that worked at a 900 s proxy; raising the proxy makes the larger
> ceiling the better answer, and this block's ceiling row is superseded by HUB-19.
> It is on HUB-19, HUB-20, HUB-21, HUB-22 and HUB-18.

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

- [ ] **HUB-18 — a completion cut off at your worker default still reports
  `finish_reason: "stop"`, so a truncated answer is indistinguishable from a finished
  one.** owes: SH (a statement, or a change) · truth: the chat lane's response on
  `/api/hub/chat`, and `max_output_tokens: 4096` in the local model row this gateway
  publishes from your own facts.
  Measured 2026-09-26 while building GW-9, from this gateway's own job store — real
  jobs, not a synthetic probe: **4 of 13 `mlx-community/Qwen3.5-9B-4bit` jobs stopped
  at exactly 4,096 completion tokens, and all 13 reported `finish_reason: "stop"`**,
  including those four. Your row states `max_output_tokens 4096` with
  `source_detail "worker default: the checkpoint ships no generation_config.json"`,
  so the cap was hit exactly and the answer still read as a clean stop.
  **Why this matters to you specifically:** the wire contract this gateway must honour
  refuses `length` as truncated and requires a clean stop, so the one signal a caller
  has cannot see this. Story Studio reads a cut-off chapter as a finished one.
  **The question, and it is yours to answer because the cap is yours:** when the worker
  stops at its own default, should the Hub say so — `length`, or a `truncated` flag
  beside the usage — or should this gateway infer it from
  `completion_tokens == max_output_tokens`? Either works; today neither happens, and
  nobody has decided. Recorded rather than guessed.
  **The numbers to size a decision with** (same model, same session, measured).
  **Throughput is per chip, and the fleet is mixed: M1 12 tokens/second, M2 14, M4 18**
  — the owner's own count of his machines, which this gateway's per-location medians
  (16.8, 16.6, 14.2, 11.6) independently corroborate. **Size a ceiling against the
  slowest, not the median**, because the caller does not choose the Mac: 8,192 takes
  **683 s on an M1** against the lane's stated 900 s (1.32×), and 16,384 times out on
  every chip in the fleet. A 1,200-word chapter is ~1,740 output tokens, ~145 s on an
  M1.
  **RESOLVED ON THEIR SIDE, 2026-09-26.** Story Studio shipped a completeness guard:
  *"a completion that used its entire requested ceiling is treated as incomplete,
  whatever `finish_reason` claims."* That is the correct fix and it is better than
  anything this gateway could have done, because the caller is the only party that
  knows what it asked for. **This row's urgency drops to "any other caller"** — the
  Hub still reports a ceiling-cut answer as a clean stop, so a future consumer with no
  such guard inherits the same trap, and the *statement* is still worth having. Story
  deliberately no longer depends on it.
  The guard does lean on this gateway: it compares the completion against
  `max_output_tokens`, so **we must keep publishing the number the lane actually
  enforces.** We do — it is read live from the Hub's `model_limits`, not chosen.

- [ ] **HUB-19 — raise `FALLBACK_MAX_OUTPUT_TOKENS`, and that one number is the whole
  of the round-trip problem.** owes: SH (a change) · truth: the installed Hub's own
  `components/chat/app/backend/llm_engine.py` line 26 and `main.py::_lane_output_budget`,
  read from the bundle running **0.6.69** build `49792c4`.
  **Stack Studio's chunker is not the defect and must not be removed.** Written here
  because an earlier version of this file said the opposite and it was wrong — the
  correction is the point of the row.
  The chain, every link of which is correct on its own:
  1. `mlx-community/Qwen3.5-9B-4bit` ships no `generation_config.json`, so the Hub has
     no model-stated output limit and falls back to `FALLBACK_MAX_OUTPUT_TOKENS = 4096`.
     It publishes that as **both `default` and `maximum`** on `max_tokens`, and the route
     **refuses a larger value by name** (`main.py` line 284).
  2. This gateway republishes it as `max_output_tokens: 4096`, `source: provider`.
  3. Story Studio reads that live and computes its own chunk threshold —
     `chunkThresholdWords(4096)` = `floor((4096 − 2000 prompt − 1500 safety) / 1.3)` =
     **458 words**.
  4. A 1,200-word chapter ÷ 458 = **2.6 calls**. That is the round trip.
  **Story Studio's ratio is right.** Their `OUTPUT_TOKENS_PER_WORD` is **1.3**; this
  gateway measured **1.22** on the same model — 6.5% apart. Their constant is not
  over-tuned, and 458 is not a hardcoded guess: it is derived live from the ceiling we
  publish. **The 458 is small because of the reserves, not the ratio** — 2,000 tokens
  of prompt allowance plus a 1,500-token safety floor is 3,500 of a 4,096 cap (85%).
  That floor is 37% of the cap at 4,096 and 18% at 8,192, so a small cap is exactly
  where a proportional margin stops being absorbable.
  **And removing the chunker would be actively dangerous**, which is why this row
  exists: the Hub refuses anything above 4,096, and a generation cut off at that
  ceiling reports `finish_reason: "stop"` — measured, 4 of 13 jobs stopped at exactly
  4,096 and all 13 said `stop` (HUB-18). Remove the chunker and every chapter past
  ~458 words silently loses its ending instead of costing a second call.
  **The ask: raise `FALLBACK_MAX_OUTPUT_TOKENS` to 8,192.** It is one constant, and
  everything downstream follows with **zero Story Studio changes** — this gateway
  re-reads your `max_output_tokens` on discovery, and their chunk threshold recomputes
  from it. At 8,192 their threshold becomes `floor((8192 − 2000 − 1500) / 1.3)` =
  **3,609 words**, so a 1,200-word chapter is **one call**.
  **Why 8,192 and not 16,384** — 16,384 was this gateway's earlier suggestion and it
  does not fit the hardware. **Sized against the slowest Mac, not the average one**,
  because the caller does not choose the machine: the owner measured his own fleet on
  2026-09-26 — **M1 12 tokens/second, M2 14, M4 18** — and this gateway's per-location
  medians (16.8, 16.6, 14.2, 11.6) independently agree with that range.

  | ceiling | M1 (12/s) | M2 (14/s) | M4 (18/s) | vs the 900 s deadline |
  |---|---|---|---|---|
  | 4,096 | 341 s | 293 s | 228 s | fits |
  | **8,192** | **683 s** | 585 s | 455 s | **fits, 1.32× on the slowest** |
  | 10,240 | 853 s | 731 s | 569 s | 1.05× — too tight to ask for |
  | 16,384 | 1,365 s | 1,170 s | 910 s | **times out on every chip in the fleet** |

  **An earlier version of this row said 1.7× headroom at 8,192.** That used the fleet
  median of 15.4 and is corrected above to **1.32×**, measured against the M1 a job can
  actually land on. 8,192 is still the right ask and 16,384 is still impossible — the
  conclusion survives the correction, the margin does not.
  **And 8,192 is a budget, not an expectation.** Three 1,200-word chapters generate
  ~5,220 tokens, which is **435 s on an M1**; only a batch that rambles to the full
  ceiling reaches 683 s. Both fit, which is the point.
  If a checkpoint ever *does* state its own limit, none of this is needed — the
  fallback is only consulted when the model says nothing, and a model that states less
  than 8,192 still wins (`min(FALLBACK_MAX_OUTPUT_TOKENS, output_limit)`).
  **These figures are now published, not asserted**: `GET /v1/models` carries
  `measured.median_tokens_per_second` per model, so you can check any of this against
  the running fleet rather than taking our word for it (STORY-7).
  **One operational catch, and it is the difference between this working and not:**
  the raise must land as a **build change**, so `FALLBACK_MAX_OUTPUT_TOKENS` moves and
  the Hub's version moves with it. GW-5 re-discovers a location when the Hub behind it
  changes **build**, and that is what would republish the new ceiling to us and onward
  to Story Studio. A value made settable at runtime with no version change would leave
  the gateway publishing 4,096 indefinitely, and nothing would look wrong. If it is
  ever made an operator setting rather than a build constant, say so and we will add a
  re-read; until then a build change is self-propagating and needs no action from
  anyone.

- [ ] **HUB-20 — publish the limit's *source* as a stable token, not as a sentence.**
  owes: SH (a small change) · truth: `components/chat/app/backend/main.py`
  (`_published_max_output_tokens` returns `entry.get("source")`) and the
  `model_limits` block you already put on the wire, read from the installed bundle
  running **0.6.69** build `49792c4`.
  You already publish this, structurally, and it is exactly what Story Studio is
  asking for:

  ```json
  "model_limits": {
    "context_window_tokens": {"value": 262144, "source": "model/config.json text_config.max_position_embeddings"},
    "max_output_tokens":    {"value": 4096,   "source": "worker default: the checkpoint ships no generation_config.json"}
  }
  ```

  **The ask is that `source` become a token rather than free prose** — Story Studio
  wants `model` / `worker_default` / `operator_default` / `curated` / `gateway` /
  `unknown` so they can *branch* on whose number it is rather than print a sentence at
  a customer. You compute the distinction already; only the spelling is missing.
  **Why this gateway will not do the mapping for you, deliberately:** your two current
  values begin `model/…` and `worker default: …`, and pattern-matching those into an
  enum would work exactly until the first reworded string, at which point a worker's
  fallback would be silently relabelled as the model's own limit — which is the single
  failure both our projects are built to avoid. A guess that is right today and wrong
  silently later is worse than an honest sentence.
  **What Story Studio has meanwhile, so this is not blocking:** we carry your `source`
  **verbatim** on their surface as `max_output_tokens_source_detail` and
  `context_window_tokens_source_detail` (verified live on `/v1/chat/models`), beside
  our own coarser `*_source` (`provider`/`curated`/`gateway`). They can read your
  exact words today; they just cannot switch on them.

- [ ] **HUB-21 — publish `max_structured_items_per_request` on the chat lane.** owes:
  SH (a change) · truth: your `lane_contract.structured_output`, which today carries
  `formats`, `enforcement`, `grammar_constrained`, `repair_attempts` and `note` —
  **and not this field**.
  Story Studio already reads it and it is simply absent, so it is a field nobody
  publishes rather than one we are dropping.
  **The gateway needs no change at all, and that is verified rather than assumed:**
  the whole `lane_contract` object is republished verbatim to `/v1/chat/models`
  (checked live — every key above reaches Story Studio, `structured_output` included).
  Put the field anywhere inside `lane_contract` and it appears on their next read with
  no release from us.

- [ ] **HUB-22 — state what an omitted parameter means, and on which Mac.** owes: SH
  (a statement) · truth: `main.py::_resolved_parameters`, which resolves each
  parameter's `default` through `model_defaults.resolved(data_dir, model_id, …)` —
  per Mac — and publishes the in-force value as `default`.
  Story Studio's ask (our STORY-5): a `defaults_scope` saying that an omitted
  parameter resolves to **that Mac's operator setting**, not to a model default.
  **The gateway adds one fact to that statement, and it is the reason it matters
  here:** a caller does not choose the Mac. Six locations serve every local model and
  the gateway picks the site at dispatch, so *"that Mac's setting"* is not
  determinable from the caller's side at all — the same request can omit `max_tokens`
  and be served 4,096 at one site and a different number at another. Today all six
  locations publish an identical surface so the answer is the same everywhere and
  nothing is wrong; **the moment one operator changes a default, our merged catalogue
  row would publish one site's value while routing to any**, which is the "a published
  number that does not describe the system" defect this project keeps fixing. Recorded
  as GW-11 rather than fixed, because it cannot fire today and guessing the right
  published shape for a case that has never occurred is how a field nobody reads gets
  invented. If you would rather state it Hub-side in a way that survives the merge,
  say what shape and we will carry it.


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

- [ ] **STORY-7 — the measurement is now published to you, on the row you already
  read. Use it to stop estimating.** owes: Story (a read, when you want it) · truth:  `GET /v1/models` and `GET /v1/chat/models`, the new `measured` key per row.
  Shipped 2026-09-26 in **0.2.40**. Every catalogue row now carries:

  ```json
  "measured": {
    "provider_id": "studio-hub", "model_id": "mlx-community/Qwen3.5-9B-4bit",
    "sites": ["studio-hub-9701220bf30f", "studio-hub-de7250e146a7"],
    "modalities": ["chat"],
    "samples": 20, "with_usage": 20,
    "max_prompt_tokens": 1827, "max_completion_tokens": 4096,
    "median_completion_tokens": 1603, "median_tokens_per_second": 15.4,
    "slowest_site_tokens_per_second": 11.6,
    "machines": {}, "first_seen": 1790354352.3, "last_seen": 1790389046.5,
    "source": "gateway"
  }
  ```

  **It is ours and it says so.** `source: "gateway"` is inside the object, so the
  label cannot drift from the numbers it describes. **`max_output_tokens` is
  untouched** — still exactly what the producer states, with its own `*_source`,
  beside this and never merged into it. A model nobody has run publishes
  `measured: null`: unknown, not zero.
  **What it is for.** `max_completion_tokens` is the largest completion that has
  **actually run** on that model, across every location offering it — so
  `measured.max_completion_tokens == max_output_tokens` means the ceiling is real and
  **being hit**, which is exactly the local lane today (4,096 against 4,096).
  `median_tokens_per_second` is a measured rate, so a time estimate stops being a
  guess; **`slowest_site_tokens_per_second` is the one to size a ceiling against**,
  because the gateway picks the Mac and a median hides the slow one. `sites` is how
  many locations contributed, and `machines` splits it per Mac
  where the lane names one — **the chat lane does not**, so a chat model's envelope is
  per location until Studio Hub names a machine.
  **What it is not.** Not a ceiling, not a target, and not a licence to raise
  `max_tokens` — the Hub still refuses above its own stated maximum (HUB-19). It is a
  record of what happened, and the only honest answer to "can this lane take a bigger
  ask".
  **A caveat we owe you, because it was in an earlier message of ours:** the 17.3
  tokens/second figure we quoted was **one 489-token sample on one Mac, plain prose, no
  JSON**. The fleet runs **12–18 tokens/second depending on the chip** — M1 12, M2 14,
  M4 18, the owner's own count, corroborated by this gateway's per-location medians
  (16.8, 16.6, 14.2, 11.6). `measured` publishes the **median**
  (`median_tokens_per_second`, currently 15.4) **and the slowest site**
  (`slowest_site_tokens_per_second`, currently 11.6) — the second one is the number a
  ceiling has to be safe against, because your caller does not choose which Mac runs
  it. Three machines × three models × a real run each is still the thing that would
  replace every estimate here with a measurement; the machinery for it now exists, and
  this field is where the answer will appear.

- [ ] **STORY-8 — your tokens-per-word number is better than ours, and we retract
  ours.** owes: Story (nothing — this is a correction and one number to check) ·
  truth: recomputed from **17 real local completions** in this gateway's own job
  store, text against `usage.completion_tokens`.
  **We said 1.22 tokens/word. That was one sample**, which is the same error we had
  just finished warning you about, made in the same message. Measured properly on
  `mlx-community/Qwen3.5-9B-4bit`:

  | | tokens/word |
  |---|---|
  | **your settled range** (1,700–3,000 per 1,200 words) | **1.42 – 2.50** |
  | **local model, 17 samples, measured here** | **1.14 – 1.88, median 1.45** |
  | our earlier claim | 1.22 *(one sample — retracted)* |
  | cloud lanes, 23 samples, for contrast | 1.34 – 25.23, median 3.95 |

  **Your range brackets the measurement and ours did not.** A 1,200-word chapter on
  the local model is **~1,740 output tokens** at the measured median — at your lower
  bound, not below it. Your `OUTPUT_TOKENS_PER_WORD = 1.3` and your 1,560 estimate are
  both slightly optimistic against this data, which is the safe direction for a
  chunking threshold and we are not asking you to move either. Publish 1,700–3,000
  with confidence.
  **And one consequence to check, because it is arithmetic you can act on:**
  **3 chapters per call at an 8,192 ceiling only fits at the low end.** 3 × 1,700 =
  5,100 fits; 3 × 3,000 = **9,000 does not**. Your completeness guard catches it, so
  the failure is a retry rather than a lost chapter — but on a model whose chapters run
  long, 3-per-call will degrade toward 2-per-call and the "10 calls instead of 30"
  figure will drift up. Worth knowing before you size the plan on it.

- [ ] **STORY-9 — the deadline you do not read is one this gateway enforces, and that
  bounds the ceiling from above.** owes: Story (nothing — a fact to hold) · truth:
  `gateway/adapters/studio_hub.py::lane_deadline_seconds`, `_poll_batch`,
  `httpx.Timeout(lane_deadline_seconds(request))`.
  You wrote that you never read `deadline_seconds`, never set it and derive no cap from
  it, so a raise changes nothing on your side — **true for you, and not true for the
  hop between us.** This gateway reads the Hub's stated `deadline_seconds` and uses it
  as its own poll deadline and HTTP timeout, so a generation that outruns it is killed
  by *us*, not by you. At the measured rates — **M1 12, M2 14, M4 18 tokens/second**,
  the owner's own count, which this gateway's per-location medians (16.8, 16.6, 14.2,
  11.6) independently corroborate — that puts a hard lid on the ask. Sized against the
  **slowest** chip, because the caller does not choose the Mac:

  | ceiling | M1 (12/s) | M2 (14/s) | M4 (18/s) | against the Hub's stated 900 s |
  |---|---|---|---|---|
  | 4,096 | 341 s | 293 s | 228 s | fits |
  | **8,192** | **683 s** | 585 s | 455 s | **fits, 1.32× on the slowest** |
  | 16,384 | 1,365 s | 1,170 s | 910 s | **times out on every chip** |

  So 8,192 is the largest value that works today, and **if anyone ever wants more, the
  ceiling and the deadline have to move together** — raising one alone produces jobs
  that die at our hop for no visible reason. That is the whole reason HUB-19 asks for
  8,192 and not 16,384. Recorded here because "nothing changes on our side" is the kind
  of true statement that gets read as "nothing changes anywhere".

  **Corollary worth keeping, and it is why publishing the median alone was not
  enough:** the number a ceiling must be safe against is the **slowest** machine's,
  and 15.4 — the median this gateway published first — hides the 12. `measured` now
  carries `slowest_site_tokens_per_second` beside the median for exactly that reason.

  **SIZING APPENDIX — what each ceiling actually buys, and the M1-safe number.**
  Asked by the owner 2026-09-26. **`PEER_PROXY_TIMEOUT_S = 900.0` is the Hub's own
  "one request timeout enforced on a job request it proxies to a worker"
  (`components/hub/standalone.py`), so it covers LOAD + PREFILL + GENERATION** — the
  generation budget is not the whole 900 s. That is the fact that decides the answer.

  What a ceiling carries, at the measured 1.45 tokens/word (median, local model,
  17 samples; 1.88 worst):

  | ceiling | script | chapters (1,200 w) | scene prompts (~403 tok each) |
  |---|---|---|---|
  | 4,096 | 2,180–2,830 words | 1.8–2.4 | ~10 |
  | 8,192 | 4,360–5,650 words | 3.6–4.7 | ~20 |
  | 16,384 | 8,715–11,299 words | 7.3–9.4 | ~41 |

  **For chapters, 16,384 buys nothing that 8,192 does not.** Story caps narrative
  batches at **3 chapters by choice, for coherence — not by tokens**, and 3 × 1,200
  words is only **~5,220 tokens actually generated**. Every ceiling at or above
  ~5,300 therefore yields the same 3 chapters per call, so both 8,192 and 16,384 give
  **10 calls for a 30-chapter episode**. The one thing 16,384 uniquely buys is
  **listicle mode's 8-chapter batch** (`LISTICLE_BATCH_SIZE = 8`, ~13,920 tokens),
  which 8,192 truncates. For **scene prompts** 16,384 reaches ~41 scenes, so it clears
  neither of Story's stated thresholds (50 scenes ≈ 20k, 100 ≈ 40k).

  | ceiling | M1 (12/s) | M2 (14/s) | M4 (18/s) | M1 % of the 900 s | left for load+prefill on M1 |
  |---|---|---|---|---|---|
  | 4,096 | 341 s | 293 s | 228 s | 38 % | 559 s |
  | 6,144 | 512 s | 439 s | 341 s | 57 % | 388 s |
  | **7,168** | **597 s** | 512 s | 398 s | **66 %** | **303 s** |
  | **8,192** | **683 s** | 585 s | 455 s | **76 %** | **217 s** |
  | 9,216 | 768 s | 658 s | 512 s | 85 % | 132 s |
  | 12,288 | 1,024 s | 878 s | 683 s | **over** | — |
  | 16,384 | 1,365 s | 1,170 s | 910 s | **over** | — |

  **At the 900 s proxy, 8,192 is the largest value that keeps every Mac in the fleet
  usable**, and the reasoning is the table above rather than a round number: it is
  where narrative work is **fully served** — 3 chapters per call, Story's own coherence
  cap, so a larger ceiling cannot improve it — while an M1's worst case still leaves
  **217 s** for model load and prefill. **7,168 is the more conservative choice and
  costs nothing**: 66 % of the deadline, 303 s of slack, and 3 chapters per call still
  fit. **Above 9,216 the M1 is excluded at this proxy** (12,288 = 1,024 s), because the
  gateway routes to any location and a job that lands on the slow one dies at our hop.
  **The owner's decision below resolves that by raising the proxy with the ceiling**,
  which is the better answer and supersedes this recommendation.

  **If you want 16,384, it takes two constants and not one.** `PEER_PROXY_TIMEOUT_S`
  must go **900 → 1,800** alongside the ceiling: 16,384 on an M1 is 1,365 s, which
  against an 1,800 s proxy is **76 %** — precisely the margin 8,192 has against 900 s
  today. The price is that a hung job holds a worker for thirty minutes instead of
  fifteen, which is a real cost at a site with more work than machines. See GW-12 for
  the route-aware alternative.

  **DECISION — the owner's, 2026-09-26: raise BOTH, ceiling 16,384 and proxy 1,800.**
  Ask for **both constants in the same build.** Verified before it was recorded:

  - **It is margin-neutral, exactly.** 16,384 against 1,800 s gives a worst case of
    **76 % on M1, 65 % on M2, 51 % on M4** — the *identical* percentages that 8,192
    against 900 s gives today. No chip loses headroom and every chip doubles capacity.
  - **One constant drives the whole ceiling, confirmed in your code rather than
    assumed.** `llm_engine.py` sets `self.max_output_tokens =
    FALLBACK_MAX_OUTPUT_TOKENS` when the checkpoint states nothing; `model_limits`
    returns that as `max_output_tokens.value`; that value becomes the `max_tokens`
    `maximum` on the parameter surface. Raising the fallback therefore raises the
    ceiling, our published figure and Story Studio's chunk threshold together —
    **nothing else needs coordinating.**
  - **Nothing on our side caps it.** `lane_deadline_seconds` returns your stated number
    unchanged and the chat lane's HTTP timeout *is* that number; the 600 s and 300 s
    timeouts in that adapter are artifact downloads on the image and transcription
    paths and do not apply.
  - **The workload fits with slack.** 30 scene prompts × your ~403 tokens per scene =
    **12,090 tokens**, 74 % of the ceiling, leaving **546 tokens per scene against your
    403 estimate — 36 % of room** before a scene is lost. On an M1 that is **1,008 s of
    the 1,800 s**.

  **One correction to the reasoning, so the expectation is right: this buys the scene
  prompts, not the script.** Story caps narrative batches at **3 chapters for
  coherence**, and 3 × 1,200 words is only ~5,220 tokens — already comfortably inside
  8,192. Raising to 16,384 does **not** put more chapters in a call. What it buys is
  the **30-scene prompt request in one go**, which 8,192 cannot carry (it holds ~20),
  and room to grow to ~40.
  **The one unverified input is the ~403 tokens per scene.** That is Story's figure,
  we have no local measurement of it, and the whole plan rests on it. The 36 % slack
  absorbs a fair amount of error, and `measured.max_completion_tokens` on
  `GET /v1/models` will show the real figure once scene jobs have run at the new
  ceiling — worth a look after the first batch rather than assuming.
  **And a cost to accept knowingly:** every chat job, including a two-minute one, may
  now hang for thirty minutes before being declared dead instead of fifteen. That is
  the price of the larger ceiling, and the fast jobs pay it too. A timeout scaling with
  the request's own `max_tokens` would pay it only where it is earned — worth doing on
  your side if it is cheap, and not something we need.


- [ ] **STORY-6 — do NOT remove the chunker. It is right, and the constant behind it
  is yours to keep.** owes: Story (nothing, if you read this) · truth:
  `electron/server/lib/chunked-narration.ts`.
  Written 2026-09-26 because the owner was about to ask for exactly the removal, on a
  number that came from us and was **wrong**. Correcting it before it costs a chapter.
  What we said: that 458 words per 4,096-token call was ~7× more conservative than the
  lane needs. **That was our error and it is retracted.** Read from your own source:
  `OUTPUT_TOKENS_PER_WORD = 1.3` is your tokens-per-word figure, and this gateway
  measured **1.22** on the same model — 6.5% apart. 458 is not a hardcoded guess
  either: it is `chunkThresholdWords(4096)` = `floor((4096 − 2000 prompt − 1500 safety)
  / 1.3)`, computed live from the `max_output_tokens` **we** publish. Your chunker is
  correctly tuned to the ceiling it was handed; a small cap is simply where your 1,500
  safety floor stops being absorbable (37% of 4,096, 18% of 8,192).
  **And removing it would lose chapters silently.** Studio Hub refuses `max_tokens`
  above 4,096 by name, and a generation cut off at that ceiling reports
  `finish_reason: "stop"` — measured from real jobs: 4 of 13 stopped at exactly 4,096
  and all 13 said `stop` (HUB-18). Without the chunker, every chapter past ~458 words
  is truncated and reported as finished.
  **The fix is upstream and needs nothing from you**: raise the Hub's
  `FALLBACK_MAX_OUTPUT_TOKENS` and your threshold recomputes from the new ceiling
  automatically — at 8,192 it becomes **3,609 words** and a 1,200-word chapter is
  **one call** instead of 2.6. That is HUB-19.
  **If you do want a change on your side, it is the margin, not the ratio.**
  `SAFETY_MARGIN_FLOOR_TOKENS = 1500` is 37% of a 4,096-token cap, where
  `SAFETY_MARGIN_FRACTION = 0.09` alone would be 369 — so at that cap the floor, not
  the fraction, is what binds: `floor((4096 − 2000 − 369) / 1.3)` = **1,328 words**
  instead of 458, and a 1,200-word chapter fits one call at the *current* ceiling.
  That is a real second option and it is yours to weigh, because the floor exists to
  keep small products protected and dropping it spends exactly that protection.
  **We are not asking for it**, and it is strictly worse than HUB-19: 1,328 words
  clears a 1,200-word chapter by 10%, so the next slightly-longer chapter chunks
  again, whereas 8,192 clears it by 3× and needs nothing from you at all.

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

- [x] **GW-9 — a per-model qualification record, built from work that really ran.**
  closed 2026-09-26 in **0.2.39** · asked as *"we need to keep these for that when I
  want to qualify a new LLM, we can apply the same parameter check too."*
  truth: `gateway/qualification.py::envelope`, `GET /admin/api/qualification`,
  Providers → **Measured** in the console.
  **Nothing new is recorded for it** — every figure was already in
  `result_json.payload` on each settled job: `usage.{prompt,completion}_tokens`, the
  worker's own `generation_seconds`, and `hub_machine`/`hub_worker` on the image,
  voice and music lanes. The change is a reader, not a schema.
  Per (provider, model) it publishes samples, the largest prompt and completion
  **that actually ran**, the **median** rate (a mean lets one cold turn move a number
  a ceiling gets sized against), per-machine splits, and first/last seen — each row
  carrying `source: "gateway"`. The **stated** ceiling is joined on the **published**
  namespace (`studio-hub-9701220bf30f` → catalogue row `studio-hub::~<model>`) and
  published *beside* the measured figures, never merged into them; `stated: null`
  means not in the catalogue — unknown, not unlimited.
  Deliberately **not** counted: a queued job is not a sample; a lane stating no
  `usage` is skipped rather than folded in as zero tokens; a missing
  `generation_seconds` is an absent rate, never `0`; `True` is not a token count
  (`bool` is an `int` in Python, and read as a number it entered the record as a
  one-token completion until a test caught it).
  last verified: `tests/test_qualification.py` → 21 checks; `test_console_actions.js`
  → 5 new; `test_e2e.py` → 7 new, **564/564**; focused suites all green. Read live off
  the **running** 0.2.39 gateway — 10 measured rows, and the local lane reads
  `studio-hub-9701220bf30f / Qwen3.5-9B-4bit` **13 jobs, largest that ran 4,096,
  stated 4,096 by `provider`** (the other three locations: 2,350 / 841 / 27).
  One defect the tests caught and it is worth naming because it will recur:
  `limit: int = Query(50, ge=1, le=500)` makes the **default a `Query` object**, so a
  direct call reaches SQLite as `type 'Query' is not supported` and the route is
  reachable only over HTTP. This route declares it with `Annotated`;
  `/admin/api/jobs` still has the old shape.
  And one defect the **live data** caught that no fixture would have: the panel
  collapsed *"not in the catalogue"* and *"in the catalogue, but it states no
  ceiling"* into one string — `openai-direct::gpt-6-luna` publishes
  `max_output_tokens: null` **with** `max_output_tokens_source: "gateway"` — so it
  told the reader a model was missing when it was present. Three states now, and a
  test. The `null`-with-a-source is deliberate (`_limit_with_source`: the three-word
  `provider`/`curated`/`gateway` vocabulary has no word for "nobody said", and adding
  one breaks readers that know only three) and is left exactly as it is.

- [ ] **GW-11 — a merged catalogue row publishes one location's `lane_contract` while
  the gateway routes to any of six.** found 2026-09-26 while answering HUB-22 · owe:
  whoever wants it · truth: `gateway/catalog.py::rebuild` — `merged[row["id"]] = row`
  is **first-wins**; on a duplicate only `_publish_lock` is updated, so the surviving
  `lane_contract`, `parameters` and their `default`s come from whichever location
  `registry.models.enabled_models()` yielded first.
  **Not live today**: all six locations publish an identical parameter surface, checked
  across all 21 models that carry one — zero disagreements. So this is a latent defect
  and that is why it is recorded rather than fixed.
  **What makes it fire**: Studio Hub resolving a parameter through an operator setting
  per Mac (`model_defaults.resolved`), which is exactly what HUB-22 is about. One
  operator changing `max_tokens` on one Mac and the merged row publishes one site's
  default while the gateway may dispatch to another — a caller omits the parameter and
  gets a number we never published, across a row that looks authoritative. That is the
  "a published number that does not describe the system" class this project keeps
  fixing.
  **Why it is not fixed now**: the honest published value for disagreeing sites is not
  obvious — `null` would collide with "no lane states one", and any single number is a
  lie about the others. Picking a shape for a case that has never occurred is how a
  field nobody reads gets invented. The cheap detection is a comparison of the
  contributing locations' `lane_contract` inside the existing merge loop; decide the
  published shape first.

- [ ] **GW-12 — a ceiling could be higher than any one Mac can meet, if routing
  respected the per-chip rate.** found 2026-09-26 answering the owner's sizing question
  · owe: whoever wants a ceiling above 9,216 · truth: `gateway/fleet.py::route`
  (ranks by pressure and fitting-machine count; **it does not read the request's
  `max_tokens`**) and `measured.slowest_site_tokens_per_second`.
  **The problem this solves:** a lane ceiling is one number for the whole fleet, so it
  is bounded by the slowest Mac — anything above 9,216 makes an M1 job die at the
  gateway's own poll deadline. The owner measured his fleet at **M1 12, M2 14, M4 18
  tokens/second**, so a 16,384 ask is 1,365 s on an M1 and 910 s on an M4: the ceiling
  that is unusable on one machine is comfortable on another.
  **The shape of the fix:** `route` already ranks candidate locations and already knows
  each one's `measured` rate; a request declaring a large `max_tokens` could be offered
  only to sites whose measured rate finishes it inside the lane's stated deadline, and
  fall back rather than fail when none can. That is capability-based placement applied
  to time instead of memory — the same rule HUB-17 asks for on the Hub's side, and the
  same reason the small-Mac-first question exists.
  **Why it is not built now:** it is only worth anything above 9,216, and the
  recommended ceiling is 8,192 — where every Mac in the fleet qualifies, so a
  route-aware rule would change nothing. Building it would be inventing a mechanism for
  a ceiling nobody has asked for yet. Recorded because it is the honest answer to
  *"could we just raise it higher"*, and because it is the only path to 16,384 that
  does not either raise the proxy timeout or strand the slow Macs.

- [ ] **GW-10 — nothing yet tells a caller that a completion was truncated at the


  cap.** found 2026-09-26 while building GW-9, owe: whoever wants it ·
  truth: `gateway/adapters/studio_hub.py::_generate_chat` (passes the Hub's
  `finish_reason` through) and `gateway/catalog.py` (publishes `max_output_tokens`).
  Measured: **4 of 13 `Qwen3.5-9B-4bit` jobs stopped at exactly 4,096 completion
  tokens, and all 13 reported `finish_reason: "stop"`.** The published
  `max_output_tokens` for that model is 4,096, so the cap was hit exactly and the
  answer still read as a clean stop — which is the one value the contract *requires*
  to be clean (`length` is refused as truncated). So the existing guard cannot see
  this, and a caller has no way to tell a finished answer from a cut-off one.
  The record in GW-9 is what makes it visible at all: **stated 4,096 next to measured
  4,096**. Not a gateway defect on its own — whether the Hub should report the cap as
  `length`, or this gateway should infer it from `completion_tokens ==
  max_output_tokens`, is a decision nobody has made. Recorded rather than guessed.

  **The arithmetic that goes with it, so the decision is not made blind.** Same model,
  measured 2026-09-26: **17.3 tokens/second** (489 tokens in 28.2 s), 1.22
  tokens/word, 7.0 characters/word, 5.7 characters/token. Against the chat lane's
  stated **900 s** deadline (`PEER_PROXY_TIMEOUT_S`): 4,096 → 236 s, 8,192 → 472 s,
  **16,384 → 945 s and does not fit**, 32,768 → 1,890 s. A **1,200-word chapter** is
  about 1,467 tokens / 8,358 characters / ~85 s. Story Studio's chunker sends **458
  words per 4,096-token call**, so one chapter is ~2.6 calls — that is the round trip
  the ceiling ask is really about. Highest completion seen on each cloud lane, for
  comparison: openrouter `deepseek-v4-flash` **15,139**, experiential-labs
  `deepseek-v4.1-flash` 8,483, openai `gpt-6-luna` 6,221, tokenharbor
  `mimo-v2.5:free` 5,045.

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
