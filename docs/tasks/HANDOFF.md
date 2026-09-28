# HANDOFF — gateway-kh

**Read this first.** Written 2026-09-28 against **VERSION `0.2.66`**. The running build
stamp is on `/health` and in `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD`.
If this file disagrees with the code, **the code wins — fix this file in the same change**.
Every path below is inside the Git checkout `repository/` unless it says otherwise.
`[MEASURED]` = observed in the session that wrote this; `[reason not recorded]` = the
project does not say why, and nobody should guess.

> **The fleet-hardening batch.** The work order is
> `_orchestra/JOB-gateway-studiohub-fleet-hardening-20260928.md` (Batch A → B → C) and the
> evidence is `_orchestra/AUDIT-gateway-studiohub-20260928.md`. **From this repo: A1 (0.2.61),
> A5 + A6 + A7 (0.2.62), and B1 + B3 + C1–C5 + C7 (0.2.63).** A2, A3, A4 and A8 are Hub-side and
> ship from `studio-hub-kh`; the Hub's own half of B1/B4/C6 is there too. **What is still open is
> named at the end of this file** — read that section before assuming anything is finished.

## WHERE THINGS STAND

| Thing | Value |
|---|---|
| Current release | **0.2.63** — `repository/VERSION`, one line, the only place to bump. **Fleet visibility and correctness:** the Fleet page shows **members** and a node marker (B1), the console has a **Queue** view with a real position for every waiting job (B3), capacity is **derived live** from the fleet and published to Story (C3/D), a job that provably never left this process is re-queued after a restart instead of becoming a hold (C2/M5), the queue is **bounded and enforced**, `due()` is ordered by arrival (C1/M3), merged rows take the **smallest** published ceiling (C4/M10), and the Hub's own `dependency` verdict withholds a tokenizer without a second HTTP call (C5/M13). See *What 0.2.63 changes* below |
| Declared Studio Hub | **0.6.83** — `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION`, published on `/health`; a controller on another version is reported, never refused. Set on 2026-09-27 from a **running** Hub (`/api/version` → `0.6.83` build `f072f17`), not a changelog; 0.6.83 is the release whose video lane publishes the `audio` switch this gateway relays `[MEASURED]`. 0.6.81 was declared earlier the same day from `0.6.81` build `3d8ce32` — that one publishes `lane_contract.canvases`. Controllers older than both still publish, through the cross-product fallback in `_video_row`, and a lane that publishes no switch simply carries no `controls` key |
| Branch | `main` = `origin/main`, clean tree; remote `github.com/theng12/gateway-kh` |
| Running build | `/Applications/gateway-kh.app`, port **8787**, stamp **`7d87724-202609270548`** — the 0.2.60 commit, measured on `/health` after the rollout `[MEASURED 2026-09-27]`. **The stale-stamp trap fired again, in a new shape:** killing the gateway by PID made the app respawn it with the *new code* (it published `studio_hub.supported_version: 0.6.83`, which only 0.2.60 states) but the *old* `GATEWAY_BUILD` stamp `05c0fc5-202609261948`, because the stamp is passed by the **menubar app process** from its own `Info.plist` — and that process was still the pre-rebuild one. Only quitting the app (`osascript -e 'quit app "gateway-kh"'`), killing the last gateway by PID (`pgrep -f "uvicorn gateway.api:app"`) and reopening produced a stamp that matches the code. So: **a stamp that disagrees with the code is not proof of old code, and a respawn is not a restart** — check `studio_hub.supported_version` too. `pkill -f` remains forbidden `[MEASURED 2026-09-26]` |
| Distribution | DMG in `repository/dist/`, named by version alone; the previous ones moved to `_trash/dist/` |
| SSD/setup kit | `repository/dist/setup-kit/`, **pinned to GatewayKH 0.2.44** and refreshed onto all four `KH-STUDIO-*` drives on **2026-09-26**, `shasum -a 256 -c` OK on every one. The drives also carry `Studio Hub.app` **0.6.70**. Superseded kits are in each drive's `_trash/superseded-kit-2026-09-26/` (`GatewayKH-0.2.27`, `GatewayKH-0.2.43`) |

## The §9 fleet rehearsal — where the receipts are, and what it found

`qualification/S9-fleet-rehearsal.md` is the acceptance record: **five real Hub processes** (the
real `components.hub.standalone.app`, each with its own data dir, token and loopback port, auth
enforced) plus one isolated Gateway, driven over HTTP by
`_orchestra/rehearsal/run_rehearsal.py`. Each Mac runs a stub that answers the Hub's real worker
contract, so the Hub, the catalogue, routing, admission, the queue, the retry policy and the
membership routes are all the real thing.

**Eleven of the twelve checks pass with measured readings**, and the three that this release fixes
were found by it rather than by reading the code:

| # | check | result | the reading that matters |
|---|---|---|---|
| 1 | a node joins and both UIs show it | PASS | controller roster 3/3; gateway `node_summary {total: 3, reachable: 3}` |
| 2 | one model row, "N of M machines", one `/v1/models` row | PASS | 2 locations, **one** row; one enable action → 2 of 2 enabled |
| 3 | a site of M Macs accepts M concurrent jobs | PASS | M=4: admission limit 4, peak concurrent 4 |
| 4 | a node leaves and a removed node stays removed | PASS | leave 200 → `removed: 1`; re-join **403** with the date sentence |
| 5 | the location is re-pointed and keeps routing + enablement | PASS | same row id, model still 2 of 2, job routed there succeeded |
| 6 | a failed Mac is re-run on another Mac | PASS | succeeded on a different machine after the hand-off |
| 7 | queue position, its bound, and `/health` concurrency | PASS | positions [1,2,3] of [3,3,3]; published **30 == live fleet 30** |
| 8 | promotion + handover with **zero touches per node** | PASS | `verified 1/1`, followers with **0 touches**, enablement survived the re-point |
| 9 | eviction is permanent, the neighbour still works | PASS | own credential 401; Fleet-token join 403 by name; neighbour ran the work; readmit re-joined |
| 10 | one cluster down does not hurt the others | PASS | down row still shows `node_summary {total: 1}` with `members_from` and `stale 41.6 s` |
| 11 | published capacity is the fleet's, live | PASS | **36 → 18 → 36** with no settings edited |
| 12 | three attempts on three machines, no duplicates | **fixed in 0.2.65** | the unknown-outcome half passes over HTTP (a Mac killed mid-poll is a hold with `retryable: false`, never re-run). The three-attempt half exposed the id-spelling defect this release corrects — see the receipt's addendum |

**Check 12 failed, and it was right to.** The three-attempt half stopped at `attempts_used: 2`
with `attempts_allowed: 3` even with three locations enabled, and chasing it found **the same
id-namespace defect A7 exists to close, inside A7's own new code** — the attempt log stores provider
ids (`studio-hub-<controller>`) while `fleet.route` compares bare controller ids. Fixed in **0.2.65**,
pinned by three tests, and re-measured on the live code path (the addendum at the end of the
receipt). The receipt is left as measured rather than rewritten: the run that failed is the evidence
that the step earns its place.

## What 0.2.66 changes — What's New is a page, above Settings

The owner asked for Studio Hub's shape: *"i want the what's new just like in the studio hub for the
gateway because right now it's still a modal and i want it moved up just above the setting tab just
like in studio hub too."*

**What's New is a sidebar row now**, under the *App* heading and directly **above Settings** — the
same place Studio Hub keeps it, because the two apps are read side by side. It was an `NSWindow`
opened from a small link at the bottom of the sidebar and from the Overview button; every door
(the row, the View menu, the Overview button, the app menu) now lands on the same page, so there is
one place the notes live and no second surface to keep in step. The bottom-of-sidebar link is gone:
with the row visible it was the same destination twice in one column.

**The renderer moved out of the window it used to live in.** `menubar/WhatsNewPage.swift` now owns
`makePage`, `notes()`, `appVersion`, `NotesDocumentView` and `NotesMarkdown`; `WhatsNewWindow.swift`
keeps the panel for a headless launch and routes `show()` to the page. That split is not
housekeeping: a renderer inside a window cannot be compiled or measured without the window, and the
one thing worth proving about this page is that the markdown becomes **type** — headings, bullets,
bold leads — rather than literal `##` and `**` in the reader's face, which is the defect the page was
first rebuilt for. `tests/test_whats_new_page.py` compiles the renderer on its own and walks the
resulting view for exactly that.

**The notes themselves say what changed for the person using the app.** The first entry still
described the old window, so it was rewritten, and the fleet work shipped today now has entries a
reader can use: members on the Fleet page, a queue you can look at, the number Story is told being
the fleet's own, one tick enabling a model for every Mac, image geometry at the size the Macs really
render, and the retry policy in plain words. `qualification/whats-new-page-0.2.66.png` is the page
rendered offscreen and looked at, which is how a UI change is verified here.

## What 0.2.65 changes — the id spelling A7 was written to fix, in A7's own code

**The §9 rehearsal caught the original defect inside its own fix, and this is that correction.**

A7 exists because the re-route compared a *provider* id (`studio-hub-9701220bf30f`) against a
*controller* id (`9701220bf30f`), so the exclusion set never matched and a busy Mac deferred work at
the same Mac (audit H5). The new attempt log stores the provider id — deliberately, because that is
what names a machine to an operator — and `fleet.route` compares `exclude_locations` against the bare
controller id. **The same mismatch, one layer along.**

Measured in the rehearsal with three locations verified enabled (`locations_total: 3`,
`locations_enabled: 3`, every location `enabled: true`, all reachable with `supply
{eligible: 1, available: 1}`): every job stopped at **`attempts_used: 2`** while publishing
**`attempts_allowed: 3`**, on two different machines. Driving the product's own
`fleet.routing_candidates`, the difference is visible directly:

```
excluded_by_provider_id  : ["02da…","6f14…","f64c…"]   <- asked to exclude f64c…, still returned
excluded_by_controller_id: ["02da…","6f14…"]            <- the same exclusion, correctly dropped
```

The lottery could therefore re-pick a site that had already failed; `_next_machine`'s
`chosen.id in tried` guard then rejected it and returned `None`, and the worker read that as
"nothing left to try" — failing a job at two attempts with no reason on the job and nothing in the
log to say why.

`worker._next_machine` now normalises **both sides** through `fleet.controller_id_from(provider_id)
or provider_id`, in one place, and applies the same function to the route's answer so the two cannot
drift. The attempt log still stores the readable provider id.

**Why the suite missed it and what now pins it.** The end-to-end test's fake router returned its
sites in order and ignored `exclude_locations` — so it never exercised the exclusion at all, and a
fake that ignores the thing under test cannot fail. `tests/test_job_retry_policy.py` now has three
tests for it: the exclusion set is asserted to be in the **bare controller-id spelling**, the fake
router **honours the exclusion the way the real one does**, and a router that hands back an
already-tried site is asserted to produce no repeat attempt.

## What 0.2.64 changes — what the rehearsal found

The §9 rehearsal ran the whole fleet end to end (two and three real Hubs and an isolated Gateway)
and four checks failed. Three were product defects; this release is their fix, plus the last audit
item that had been left open.

**A down cluster now still names its members.** The roster only ever came from the live probe, so a
controller that was switched off published `nodes: []` and `node_summary: null` — "this house is
offline with five Macs" and "this house has no Macs" read the same, which is exactly the
distinction the owner needs when a site loses power. The Gateway now remembers the last roster a
controller published (in-process; a display of what we last knew, not a second source of truth) and
publishes it **only** while that location is not answering, with `members_from` and
`members_stale_seconds` so a stale count is never mistaken for a live one. Nothing is invented for
a site never seen up, and an empty roster never overwrites a real one.

**A location can be re-pointed at a promoted controller, keeping its identity and its decisions.**
`PUT /admin/api/fleet/controllers/{id}/address` (audit A3 step 4). Adding the new head created a
**second** location — a promoted Mac has a different address *and* a different controller id — so
the first one's credential and every per-model enable decision were orphaned; the rehearsal measured
the replacement row publishing `model_id: null`, `locations_enabled: null`. The row id, and
therefore the provider id Story's presets and the operator's ticks are keyed on, is preserved. The
**site id is cleared rather than carried**, because it named the identity of the Mac that used to
answer there and reusing it for a different Mac is the shared-identity defect `_stable_identity`
exists to prevent.

**`H13` — the Gateway no longer sends a Fleet token to a public address.** `_validate_address`
accepted any `http(s)` host, so a mistyped or pasted public hostname received a location's Fleet
token in `X-Hub-Token` on the very next probe. A controller answers on the owner's own network —
loopback, LAN, link-local, or CGNAT (which is Tailscale) — and a **hostname** is accepted only if
every address it resolves to is private, so MagicDNS names keep working while a name that resolves
publicly is refused by name. Resolution happens once, at add or re-point time, never on the probe
path. The owner's live roster is Tailscale literals, so nothing of his changes.

**`/health` publishes the capacity Story is offered.** `paid_concurrency_limit` and
`concurrency_by_modality` live on `/v1/account/overview`, which is Story's surface; the same numbers
are now on the health read beside `concurrency_pool`, so "the fleet can do 24 while the pool says
60" is visible without knowing which endpoint to ask.

## What 0.2.63 changes — seeing the fleet, and the correctness underneath it

### B1 · Members on the Fleet page (audit H10, M20, M21)

The gateway **fetched** each controller's `nodes` and `node_summary` and never decoded them, so the
owner could not see who had joined a controller without opening Studio Hub on it. The menubar app
now decodes both: a **Members** column on Locations (`reachable / total`, plus `· N refused`), a
**Node** column and a **Node last seen** column on Machines, and a State cell that draws
**`credential refused`** for an `unauthorized` node instead of the machine's activity word
`offline`. That state also raises a **`node_credential_rejected`** issue carrying the Hub's own
sentence, and suppresses the `machine_offline` row for the same Mac — one fault, one row.

Every decoded field is optional on purpose. Measured live: a controller on Hub 0.6.65 publishes
node rows with `id`/`name`/`state`/`role`/`address` and **no** `last_seen_at`, so the column draws
`—` rather than a zero, and a controller that states no summary draws `—` rather than `0 / 0`.

### B3 · Queue visibility (audit H8, H9)

`api.py` published `queue_position`/`queue_eta_seconds`/`queue_reason` **only when `defer_reason`
was set**, so a waiting job looked identical to a running one; and when it was set, the position
counted only *deferred* jobs and ignored the fresh ones dispatched ahead of it. Now **every**
`accepted` job publishes its position and a new `queue_size`, where "ahead" means another job in
state `accepted`, in the same modality, created earlier (ties by id) — deferred or not.

New **`GET /admin/api/queue`**: waiting jobs with provider, model, modality, age, position, why it
is waiting, and *attempt n of 3* with the last failure; plus the live `worker._in_flight`,
`worker.budgets` — **written "so the console can say what is bounding a lane" and read by nothing
until now** — the per-site admission budgets, the pool, and the queue wait settings. One console
view over it, under Concurrency on the Providers page (`#queue` lands there).

### C1 · Fairness and survival (audit M3, M4, M6)

- **`due()` is ordered by `created_at`, not `next_action_at`.** Deferral pushes the readiness clock
  forward, so a job that had waited kept losing its place to work that arrived later. A queue where
  waiting costs you your position is not a queue. `next_action_at` still decides *whether* a job is
  ready; `created_at` (ties by id, so the order is total) decides who goes first.
- **The deferral budget survives a long render.** Eight deferrals capped at a minute is about four
  minutes, and the live settings file did not override it, so the shipped default was the ceiling in
  practice: a job waiting on a busy site was **failed after ~4 minutes**. It is now 240 deferrals
  capped at five minutes — about twenty hours — which is the owner's decision C verbatim (*"hold,
  don't fail, for a temporarily absent cluster … this replaces the audit's four-minute deferral
  budget"*). The **retry policy is what keeps this honest**: A7 declares a job failed after three
  real machines have tried it, so this bounds "nothing can take it yet" and A7 bounds "everything
  tried it and failed".
- **The dispatcher cannot die silently.** The loop guarded `due()` and nothing else, so one raise
  from `_admit`/`mark_running`/the thread spawn ended dispatch for ever while `/health` still said
  ok. The whole pass is wrapped, and `worker.liveness()` publishes `worker_alive`, `last_pass_at`
  and `consecutive_failures`.

### C2 · Restart recovery (audit M5, M7)

- **A job that never left this process goes back in the queue.** The startup sweep failed
  *everything* `running` as an unknown-outcome hold; six of the live holds were work that had never
  been dispatched (`upstream_job_id: NULL`). The line is drawn at `dispatch_started_at`: before the
  adapter was called there is no window in which a request could have reached a worker, so
  re-queueing is provably safe. **A dispatched job is still a hold and is never replayed** — an
  upstream id proves a container was created, and *no* id is not proof that none was.
- **A Mac that dies mid-poll is a named hold.** `_poll_batch` had no `try/except`, so an
  `httpx.HTTPError` became a durable hold whose detail was the exception's repr — no batch, item or
  location. It now raises a settled-coded hold naming all three, with the recovery context the
  discard action needs.

### C3 · Capacity is derived, never configured (the owner's decision D)

`paid_concurrency_limit` was the **settings pool** — live `60` while the fleet could do about `4`.
It is now `concurrency.live_capacity()`: the sum, over **reachable** locations, of the very budget
admission enforces (`hub_slot_reading` + `hub_budget`, the same two functions `Worker._provider_budget`
calls), per modality and in total. `Live on this Mac: 24` — four reachable locations × six
modalities × one Mac. A Mac that is sold leaves the number the next time Story asks; nothing is
cached beyond the fleet's own five-second snapshot.

The **pool is still published beside it** as `concurrency_pool`, and the per-modality breakdown as
`concurrency_by_modality`, so a reader who sees the two disagree can tell which is which rather
than discovering it by counting.

**`acquire_permit` now reserves against that number** instead of returning an id and
`expires_at: null` for ever: it answers `429` + `Retry-After` ("never 402" — a 402 locks the
caller's key permanently) when the fleet has no headroom, and otherwise issues a permit that
**expires**. **`max_queued_jobs` is enforced** at the one path every generation route funnels
through, with the same `429` + `Retry-After`, and its shipped default is raised from 32 to 256 —
53 Macs running multi-hour video jobs make 32 a busy afternoon.

### C4 · Merged ceilings and one invented number (audit M10, M11)

`_merge_capabilities` returned immediately for anything that was not TTS, so a merged row kept the
**first location's** token window, output ceiling, prompt ceiling and upload cap: a Hub-side limit
change on the second site never reached Story, and the row could advertise a window one of the
machines would refuse. Every published ceiling now takes the **smallest** value, with its
`*_source` and `*_source_detail` travelling beside it, for every modality. `available_workers: 1` —
the literal, invented, published on every transcription lane for a model the fleet reported on
sixteen machines — is **deleted**; the real concurrency comes from the location's own capacity.

### C5 · The Hub's own verdict, and quantisation (audit M13, M14)

The Hub states `type: "dependency"` for a package that exists only to serve another model, and the
gateway read it and dropped it — so the guard that keeps tokenizers and codecs out of the picker
rested entirely on a *second* HTTP call succeeding, which is the failure `models.py` records as
having happened once already. The withhold rule now consults **either** source (the package list's
own row or the catalogue's fact).

**Every one of the 30 manifests now states its quantisation**, with the sentence saying where it
came from — `"MLX 4-bit"` from *"the publisher's model id states it: mlx-community/chatterbox-4bit"*,
or `null` plus the reason when the publisher names no precision. It is carried through discovery and
published on the row as `quantization` + `quantization_source`, against the owner's standing rule
that a model named without its quantisation cannot be checked against what he has actually run.
`packaging/capture_model_manifests.py` preserves both keys across a re-capture, and the frozen
digests in `tests/test_model_manifest_catalog.py` were refreshed in the same change (the manifest
content genuinely changed).

### C7 · Documentation truths (audit M17, M22)

The Hub's `docs/gatewaykh-handoff-0.6.63.md` documented a **two-step** join and omitted
`POST /api/hub/fleet/adopt`, so a scripted enrolment produced Macs the controller calls
`unauthorized`. It now states all three steps, what the join returns, and what the node must keep.
The Hub README's "rotate independently" claim now names the route that does it. And
`fleet-deployment-notes.md` gained the removal caveat: **a removal is an eviction, and it makes that
Mac's finished jobs unretrievable through the controller (503)** — fetch anything you need first.

## What 0.2.62 changes — three fleet items: what the fleet offers, what it renders, and what happens when a machine fails

### A5 · Models enabled once, for the fleet (audit H1, H2)

Controllers added on the Fleet page arrived with `auto_enable_discovered=False`, so each remote Mac
contributed **5, 5, 2, 2, 2** of its 19 publishable models and only 5 of the 13 Hub rows had more
than one location: the operator ticked a model once and believed the fleet offered it. A newly
added location now **inherits this gateway's own decisions for the same model ids** — enabled
there ⇒ enabled here, disabled there ⇒ recorded disabled, a model the local Hub has never seen
gets no entry and stays off — and the add result says so in real counts. A re-add of an existing
location is never seeded, so per-Mac choices survive a token or address change.

The console's Models page no longer draws one row per Mac (108 rows for 21 models). Each model is
drawn **once**, with a **Machines** column reading *"N of M machines"* and a per-Mac sub-row; the
row's own tick enables or disables that model on every machine that has it. New additive fields on
`/admin/api/models`: `is_location`, `locations[]`, `locations_enabled`, `locations_total`,
`provider_ids`; on the add result: `controller.created` and `enable_seed{sentence}`. Nothing was
renamed or removed — Story Studio and the SwiftUI app read this API.

### A6 · The geometry the Macs actually render (audit H3, H4)

Story Studio was published the **cloud** table: 16:9 → 1536×1024, 4:3 → 1536×1024. The Macs render
16:9 → **1280×720** and 4:3 → **1152×864**, and the Hub's own lane contract accepts **13** ratios
including `16:9 small` = **1024×576**, the video-valid frame the Image studio added on purpose.
Four of five published sizes were wrong by 23–33% linearly and 8 of 13 accepted ratios were hidden
— Story composed at 1536×1024 and received a 1280×720 file.

For a `studio-hub` model that carries a `lane_contract`, `aspect_ratios` and `output_options` are
now built from **the Hub's own contract and preset table** (`HUB_IMAGE_SIZE_BY_RATIO`), read
through the existing lane-contract reader. `16:9 → 1280×720`, `16:9 small → 1024×576`,
`4:3 → 1152×864`, `1:1 → 1024×1024`, and so on. A ratio the Hub states with no size publishes in
`aspect_ratios` and gets **no** option row (Story renders `width.toLocaleString()`, so a sizeless
option is a crash, not a gap). The old table is the fallback only when the Hub states nothing, and
**non-`studio-hub` providers are byte-identical**.

`available` was a hardcoded `true`, so a model whose every Mac was down still published as
available and Story rendered the outage as *"Locked for this GenStudio account."* `available` is
now `not locked`, and the outage publishes its own sentence:
*"No Mac offering this model is reachable — every Studio Hub location that offers it is offline or
not taking customer work."* Fixing this exposed a fourth inconsistency: unlocking a merged row left
the outage sentence in `activation_blockers`, so a row could read `locked: false` beside an outage
blocker. `_publish_lock` now reconciles it.

`gateway/providers/discovery.py` also carries the Hub's `image_sizes` into the model row, so the
catalogue prefers the Hub's live table over the copy taken from the 0.6.91 bundle the moment a
preset changes.

### A7 · Three attempts, different machines, never a duplicate (audit H5, H6; owner's decision E)

The re-route was a no-op: the exclusion set was built from `provider_id`
(`studio-hub-9701220bf30f`) and compared against `location_id` (`9701220bf30f`), so a busy Mac
deferred the job **at the same Mac** while an idle one offered the model — 39 live `MACHINE_BUSY`
failures — and a Mac that failed *after* the batch started was never re-routed at all
(`ffmpeg not found on PATH`, `[METAL] Command buffer execution failed`).

The policy, in full, is **[docs/job-retry-policy.md](../job-retry-policy.md)**, referenced from
`fleet-deployment-notes.md`. In short: attempt 1 on the routed machine; on any **known** failure,
attempt 2 on a different machine; attempt 3 on another never tried; after the third, the job is
declared failed to Story Studio with a terminal reason naming every machine and why each failed.
`retry.max_attempts` (default **3**) is live-editable beside the pool size.

**No duplicates, absolutely.** The re-route is guarded by `if not unknown and pinned is None:` —
an upstream that accepted the work and went silent is an unknown outcome, becomes a reconciliation
hold, and is never re-run automatically. A pinned voice is excluded (its reference clip lives on
one controller). A job with nowhere else to go **waits** rather than spending an attempt on a
machine that was never going to run it.

The exclusion is now a **column** (`attempted_locations`, `attempt_log`), not a local set: the old
`tried` set lived inside one dispatch call, so a job that deferred and came back — or was
redelivered after a restart — could be sent to the machine that had just failed it. Every retried
job publishes `attempts[]`, `attempts_used`, `attempts_allowed` and `attempt_policy` on its
envelope; a job on its first machine carries no `attempts` key rather than an empty list.

Also in this release: routing skips a controller that has **handed its group over** — the Hub
publishes `capacity.accepting_customer_jobs: false` and `fleet._takes_customer_jobs` now reads it,
so a retired Mac stops receiving work in the window before the operator removes it (A3 step 4).

## What 0.2.61 changes — admission is a capacity, not a remainder

**A1 of the fleet batch, and it is the one that decides whether a 53-Mac rollout can use its
Macs at all.** The Hub publishes, per operation, how many of its Macs are *free*
(`available_physical_machine_slots`). Admission used that free count as its ceiling and then
refused when this gateway's own in-flight jobs reached it — so with **M** Macs and **T** jobs
already dispatched, admission needed `T < M − T`. **At most half of every site could ever be
busy**, and the arithmetic got worse the busier the fleet got. It was invisible only because
the live fleet has one reachable Mac per site.

**The fix is one sum, bounded by the Hub's own total:**

```python
budget = min(free + in_flight, eligible_physical_machine_slots_total)
budget = min(budget, stated_provider_limit)      # the operator's cap still throttles
budget = 0                                        # when the location is not reachable
```

The Hub's free count is *already* net of the Macs this gateway is using, so adding its own
in-flight count back puts those Macs back in the number instead of charging the site twice.
With M=6 and four running: free=2, in_flight=4, budget=6 — the site takes two more.

**The sum is only taken when the Hub states its total.** A Hub that publishes free slots and no
eligible total cannot tell "three free of three" from "three free of thirty", so its free count
stands as-is. That is the conservative branch, not the fix.

**Two more defects in the same subtraction**, both closed here:

- **M1 — worker services versus physical machines.** One Mac publishes `available_worker_slots: 2`
  for `audio.transcription` while routing ranks it by `available_physical_machine_slots: 1`
  `[MEASURED on this Mac, live]`. Admission believed the service count, so it scheduled two jobs
  onto one Mac and the second came back `MACHINE_BUSY` — a live failure with 39 occurrences.
  `concurrency.hub_slot_reading` is now the **single reader** for both admission and the console,
  and it prefers the physical count.
- **M2 — an offline location still got a budget of 1.** Two unreachable rows each computed 1, so
  every pass spent a global slot and a connection timeout before failing. A location with
  `reachable: false` now gets **0**, which is what routing already assumed.

**The provider counter is now per site-and-modality.** It was per site, so a site running one
image job refused a chat job while a second, free Mac sat there — the owner's *"distribute them
properly"* complaint reproduced inside the fix. `Worker._per_lane` holds `(provider, modality)`
counts; a missing reading is never read as an offline location (`reachable is not False`), because
"no answer" is not "no work".

**One table, one copy.** `HUB_OPERATION_BY_MODALITY` lived in both `fleet.py` and `worker.py`, and
the two had drifted — the fleet copy was **missing `chat`**, so routing fell through to the
site-wide total for a chat job while admission read `chat.completion`. Since the two numbers are
now compared with each other, `fleet.HUB_OPERATION_BY_MODALITY` is the single definition and
`worker` imports it.

**Published, so the arithmetic is checkable from outside.** Every location row on
`GET /admin/api/fleet` now carries:

```json
"admission": {"lanes": {"image": {"limit": 6, "source": "hub",
  "operation": "image.text_to_image", "hub_free": 2, "hub_total": 6,
  "hub_unit": "physical_machine", "in_flight": 4, "detail": "…"}}}
```

The Hub's own reading sits beside the number admission computed from it. A reader who can only
see `available_worker_slots` cannot tell a site at half capacity from one that is genuinely full,
which is exactly how this survived four audits.

**Verified by:** `tests/test_worker_concurrency.py::AdmissionIsACapacityNotASecondSubtractionTest`
— a live fake site whose free count falls as jobs start and rises as they finish, held at M=1, 3
and 6; the M1 reading; the unreachable case; the operator's cap still binding; and the module's
standing *"a limit of zero never stalls the queue"* property. Plus
`tests/test_fleet_routing.py::TheFleetReadPublishesTheAdmissionBudgetTest` for the published
receipt.

## What 0.2.60 changes — the lane's switch reaches the caller, and the caller's choice reaches the lane

**The owner asked for one thing and it needed both halves:** *"for the video generations … can that
be a button to select/deselect? also make it available in story studio too as an option."* Studio Hub
0.6.83 gave its LTX lane an `audio` switch and published it on `lane_contract.parameters`; Story
Studio reads the catalogue **through this gateway**, so a switch this gateway does not publish is a
switch Story Studio cannot offer, and a value this gateway does not forward is a switch that changes
nothing. Both halves are here.

**Published: `controls` on a local video row.** `catalog._local_video_controls` relays the lane's
**boolean** parameters — name, type, `default`, and the lane's own `note` — and nothing else. The
rest of a lane's parameter list is the vocabulary the row already states better (`modes`,
`aspect_ratios`, `variants`), and restating it would be a second copy free to disagree with the
variants a caller is billed for. Measured live against the running Hub 0.6.83 `[MEASURED]`:

```json
[{"name": "audio", "type": "boolean", "required": false, "default": true,
  "note": "the model generates picture and sound together, so this does not change generation
           time; unticking it skips the audio decode and writes a silent MP4. The track can be
           added back in an editor."}]
```

**The note is why relaying matters.** The lane, not this gateway, knows that unticking `audio` skips
a *decode* rather than a denoise step; a caller that renders the switch without that sentence implies
a saving the lane does not make. It travels verbatim and is shown verbatim.

**Forwarded: `audio` in `_HUB_VIDEO_PARAMS`.** The Hub reads `params.audio` on a video item. Only
what the caller set travels — the loop's `is not None` test — so an unstated choice leaves the lane's
own default in force rather than letting this gateway decide it. `false` is the value that must
survive, which is why the wire test asserts a *silent* request specifically.

**Three states kept distinct, deliberately:** a switch (relayed), a lane that publishes none (no
`controls` key at all — not an empty list), and an entry this gateway cannot read (dropped alone,
with a `null` default rather than a coerced one). A model withheld over an unreadable extra field
would lose a working picker row to a decoration.

**Not in this release:** Story Studio's half — the switch in Motion Review, the request field, and
the idempotency digest that has to include it — lands in its own cycle (1.4.231).

## What 0.2.59 changes — WIRE-3: the fields Story read that nothing published

**The audit's `WIRE-3` is now closed, with a decision recorded for every one of the twenty-five
fields.** Story Studio's `GenStudioChatModel` carried twenty-five fields this catalogue never
published. Three of them were a *bug*, not a gap, and the other twenty-two have no source anywhere
on this Mac — which is a fact worth writing down rather than leaving as a standing suspicion.

**The bug: a feature that could not run.** `routes/vision.ts` gated the thumbnail-reference vision
picker on `max_input_images >= 1` and a non-empty `supported_detail_levels`. **No gateway publishes
either**, and Story's parse turns their absence into `0` and `[]` — so every model failed the gate,
the vision model list was always empty, and the one fact that decides it (`supports_vision: true`,
published from the provider's own `architecture.input_modalities`) sat unread beside them. Fixed on
Story's side in **1.4.230**: eligibility rests on what is published, the per-model limits are
enforced when stated, and the menu is told whose choice each number is
(`detail_source` / `reference_limits_source`).

**The bug this release fixes: a fact fetched and thrown away.** `_openai_models` kept a fixed
whitelist of provider keys:

```python
raw={k: v for k, v in entry.items() if k in {"id", "name", "owned_by", "context_length", ...}}
```

It had already been patched twice by adding one more key — `top_provider` for the real output
allowance, `maximum_text_length_per_request` for the TTS limit — and each patch left the next
provider fact needing a discovery change before anyone could read it. OpenRouter publishes
`expiration_date` on a model it is retiring, and the whitelist discarded it **at fetch time**, which
is why the retirement family read `None` for every model and Story Studio's retirement surface (a
model-selector badge and `genstudio-model-retirement.ts`) could never fire.

`raw` is now `dict(entry)` — the whole entry. A `/models` response is public metadata, so nothing is
being withheld on a model's behalf; what the catalogue *publishes* stays explicit, key by key.

**Published from that:**
- `retires_at`, `deprecation_warning`, `deprecation_warning_at`, `scheduled_retirement_at` — from
  `expiration_date` and every spelling beside it, with **`retirement_source: "provider"`**.
  `upstream_shutdown_at` and `replacement_model_id` stay `null`: nothing states them, and a
  `deprecation_warning: false` on a model nobody has said anything about means "unstated", not
  "will not retire".
- **`activation_blockers`** on every row — the gateway's own reasons a row cannot be used: the lock
  reason, and a location's own reason for a package it cannot place. It was already in the row as
  `lock_reason`; nothing published it, so Story's operator-facing sentence was its generic fallback
  while the real reason sat one field away. An empty list means usable.

**The decision table for the other twenty-two** (nothing states them; nothing is invented):

| Field group | Decision |
|---|---|
| `availability_reason`, `available_again_at` | **Deleted in Story 1.4.230.** They described a temporary absence the old GenStudio published; the fact that replaced them is a *job's* retry window (`next_action_at`, `defer_reason`), which rides on the job and is already rendered in the Jobs window. Story's cooldown still works from its own measurement (`noteLlmLineupQuotaExhaustion`). |
| `discount`, `discount_disclaimer` | **No source.** Nothing in this gateway has promotional pricing; a `false` would imply a mechanism that does not exist. Story treats absence as "no discount", which is honest. |
| `max_input_images`, `accepted_image_mime_types`, `max_image_bytes`, `max_image_dimension`, `supported_detail_levels` | **No source** — no provider states per-image limits. Story enforces what is published when it is, and its own limits otherwise, labelling which (`reference_limits_source`). |
| `citation_format`, `supports_domain_filters`, `research_qualified`, `supports_claim_attribution`, `web_search_credits_per_search`, `web_research_credits_per_request`, `max_web_searches` | **No source** beyond `supports_web_search` / `returns_citations`, which are published. Research pricing would have to be curated before it could be published, and a curated number must be labelled as the owner's, not the model's. |
| `max_structured_items_per_request` | **No source.** Story already handles its absence (STORY-10) — the reader-side fix stands. |
| `relaxed_queue_hint` | The shelf (`queue_class`) is published; the hint is the old GenStudio's scheduling prose. No source. |

**Evidence.** `tests/test_wire_fields.py` (8 tests): a stated expiry becomes the retirement family
with its source; every spelling is read; nothing stated publishes nothing retired; the six names
stay in Story's vocabulary; `activation_blockers` carries the lock reason; and the discovery
whitelist cannot come back. Every focused suite green, **e2e 577/577**.

## What 0.2.58 changes — What's New is a page, and there is a way to look at it

**The window dumped the notes file raw.** `WhatsNewWindow` handed `whats-new.md` to one `NSTextView`
and set `systemFont(ofSize: 12.5)`, so the reader saw literal `##`, `**bold**` and backticks at one
size, with `Build …` glued to the first line. The owner's complaint was exact: *"i had this ugly
modal and bad font arrangement"*, next to Studio Hub's What's New page.

- **The same page furniture as Studio Hub.** `PageKit.label` + a header row of *What's New* (26pt
  semibold), the bundle's version in monospaced digits beside it, and this build's stamp pushed
  right in tertiary grey; then a rule, then the notes as blocks. `NotesMarkdown` carries the same
  block rules as Studio Hub's `MarkdownText` (headings by level, bullets with a hanging indent,
  paragraphs joined, `---` → a real separator), so a note reads the same in both apps.
- **The inline parser is hand-rolled, and the reason is the deployment target.** Studio Hub uses
  Foundation's `AttributedString(markdown:)`; that needs macOS 12 and this app runs on **11.0**
  (`MACOS_MIN`). Raising the floor for one window would trade a supported Mac for a renderer, so the
  three span kinds the notes actually contain are parsed here — audited: no tables, block quotes,
  numbered or nested lists, links, code fences, strikethrough or double-backtick spans. It is
  recursive, so `**bold with `code` inside**` keeps both, and an unclosed marker is plain text.
- **Two other defects in the same window.** macOS merged it into the Overview window as a **tab**
  (`tabbingMode` is now `.disallowed` — that is what made it look like a modal bolted on), and a
  programmatic `NSWindow` is released when closed while this class keeps a reference, so the second
  open could have messaged a dead window (`isReleasedWhenClosed = false`).
- **`menubar/tools/whats-new-preview/main.swift` — a visual change can now be looked at without
  fronting the app.** It compiles the *real* `WhatsNewWindow` against `PageKit`, renders the page
  offscreen at a given size and appearance, and writes a PNG; `--window` calls `show()` itself and
  reports the window it created. Not compiled into the app (`build.sh` globs `menubar/*.swift`).

  ```sh
  swiftc -target arm64-apple-macos11.0 -o /tmp/whats-new-preview \
      menubar/tools/whats-new-preview/main.swift menubar/WhatsNewWindow.swift menubar/PageKit.swift
  /tmp/whats-new-preview menubar/whats-new.md /tmp/notes.png 720 640 dark 2400   # 2400 = scroll offset
  /tmp/whats-new-preview menubar/whats-new.md /tmp/notes-narrow.png 470 640 dark
  /tmp/whats-new-preview --window
  ```

  Verified with it: dark and light, 720pt and 470pt wide (wrapping holds, no horizontal scroller),
  a scrolled section of bullets, and `show()` twice → **one** window, reused, `tabbing=disallowed`,
  720x672. `menubar/whats-new.md` carries a section describing this change, so the new window's
  first entry is the one that explains it.

## What 0.2.57 changes — a video variant names the shape it has

**The lane's two parameter enums were never a set of canvases, and this catalogue built their cross
product.** `_local_video_shape` read `parameters.aspect_ratio.enum` and `parameters.resolution.enum`
and `_video_row` emitted one variant per `(mode, duration, resolution)` with `aspect_ratios` taken
from the ratio enum — so for Studio Hub 0.6.81's video lane the gateway published `12` variants
across two classes while only `4` pairs exist, and Story Studio's picker offered `448p + 16:9`.

- **Root cause, measured on the worker side:** the LTX pack's two-stage pipeline floors each output
  dimension to a multiple of 64 (`snap_output_dimensions(..., two_stage=True)` in the pinned
  runtime). The lane used to publish `288p 16:9 = 512x288`, which the pack returns as **512x256**
  (2:1) while the job's acceptance still said `512x288`. Studio Hub 0.6.81 replaced that table with
  snap-stable pairs (`256p`: `7:4`, `4:7`; `448p`: `1:1`, `11:7`) and published them as
  `lane_contract.canvases` — every pair with its exact pixels and the durations that canvas accepts.
- **This release reads them.** `catalog._local_video_canvases` reads `canvases` (strictly: a
  malformed list makes the whole row undescribable, because a partially-described variant is worse
  than none), `_video_row` builds one variant per published pair per accepted duration, each
  carrying its own `aspect_ratio`, and `aspect_ratios` is the set the canvases actually carry rather
  than the enum's union.
- **A worker older than 0.6.81 still publishes.** With no `canvases` key the old cross product is
  built exactly as before — every one of those rows was published before this release, and a caller
  that picks a pair the lane refuses is told so by the lane. The branch is marked `ponytail:` with
  its removal condition.
- `tests/mock_upstream.py`'s video lane now publishes real canvases (including one that accepts only
  the shorter duration, so a per-canvas duration list is exercised rather than assumed);
  `tests/test_hub_lane_contract.py` and `tests/test_e2e.py` pin the published variants as a set of
  `(duration, resolution, aspect_ratio)` triples and assert the set is a subset of the published
  pairs. **`tests/test_e2e.py`: all 577 checks pass.**

## What 0.2.56 changes — the lane ceilings have a control

**The owner asked for a UI for the per-lane concurrency ceilings; the API had carried them since
the pool was split and the page never drew one.** `concurrency.settings_view` had returned
`lanes.values` (whatever was in the settings file) and nothing that a page could iterate, so a
lane cap could only be set by editing `provider-concurrency.local.json` by hand.

- **The route now passes the lane vocabulary in** (`admin.get_concurrency` →
  `concurrency.settings_view(..., lane_names=catalog.MODALITIES)`). One source of truth, no second
  list inside `concurrency.py`: a lane the gateway serves cannot be missing from the page, and a
  lane it does not serve cannot appear on it. Settings keys the caller did not name are still
  listed, so a cap set by hand survives a vocabulary change.
- **`settings_view` publishes `lanes.rows`** — one row per lane with the number in force, its
  source and the sentence explaining it, exactly like a provider row, plus the `stated` value so a
  blank box means "not set" rather than "1".
- **The console draws a "Lanes" table above the provider tabs**, with an input per lane. The save
  payload carries `lanes` (already supported by `save_settings`), and the inputs are read from the
  document like every other row, so switching provider tabs cannot drop an unsaved lane cap.
- `tests/test_concurrency_tabs.js` gains three checks — every lane has a control and its unset
  state is readable, an empty lane list still renders the table, and the lane inputs survive a
  provider-tab switch. **9/9.**

**Verified:** e2e **575/575**, `test_worker_concurrency.py` 45/45, console suites 9/9.

## What 0.2.55 changes — the reference travels where the Hub reads it

**Found by the first real chain job, and it is the kind of defect only a live chain finds.**
`0.2.54` sent an image-to-video request's `reference_images` at the **item's top level**. Studio
Hub builds an item from exactly three keys — `prompt`, `seed` and `params` — and then merges
`shared_params` with `item["params"]` before popping `reference_images` off the resulting *body*
(`broker._submit_batch_locked`). An item-level reference is dropped without a word, the broker takes
its JSON door instead of its multipart one, and the worker receives a prompt-only request: measured
2026-09-26, the first chain job generated **text-to-video from a request that had uploaded a source
image**. The reference now travels inside `item["params"]`, and
`tests/test_studio_hub_contract.py::VideoLaneTests` pins both halves — the reference is in `params`,
and nothing item-level claims to be one.

The same release carries the **tier gate asserted at the public surface**: the e2e mock now offers a
second local video model whose Hub verdict is `memory_eligible: false` (the same field a real 16 GB
controller publishes for the 22B model), and the suite asserts it is **not** in
`/v1/videos/models`. Recorded check count: **575/575**.

## What 0.2.54 changes — the local video lane is published

**Studio Hub 0.6.76 shipped a real Video worker, so this gateway now has a local video lane to
carry.** Until today `video` was the one modality where every published row was a cloud one: the
catalogue's `MODALITIES`, the `/v1/videos/models` route, the console's Create lanes and
`fleet._HUB_OPERATION_BY_MODALITY` all knew the modality, and `adapters/__init__.py`'s kind table
was the single switch that withheld every local row. That switch is on now, and four things follow
from it:

- **The lane's own vocabulary is relayed, not guessed.** A Studio Hub video row's `modes`,
  `aspect_ratios` and `variants` come from the model's `lane_contract` — the worker's own published
  answer — instead of the flat 5s/10s@720p ladder the cloud path uses. `_local_video_shape` reads
  it, and `withhold_reason` refuses to publish a local video model whose contract cannot describe
  what it accepts: a video row the gateway cannot describe is one Story Studio would offer a
  control it cannot honour.
- **The tier is the Hub's own verdict, read.** The worker publishes `memory_eligible: false` on a
  machine below the model's qualified tier (LTX-2.3 22B is 24 GB, Lance 3B is 16 GB), and
  `withhold_reason` already withholds on exactly that field. Asserted both ways in
  `tests/test_hub_lane_contract.py::VideoPublicationTests`.
- **Both of the adapter's video doors work.** Text-to-video goes through the existing
  `_generate_image_or_voice` path (`modality: "video"`, `_HUB_VIDEO_PARAMS`); image-to-video sends
  Story's uploaded scene as an inline-base64 `reference_images` entry, which is the only one of the
  Hub's three reference forms that crosses two systems — the Hub has never seen this gateway's
  asset ids, and a URL would make the Hub fetch back from here. A `reference_asset_id` that has
  aged out is a named caller error, never a silent text-to-video job.
- **`POST /v1/videos/source-images` exists**, because Story Studio's image-to-video flow uploads the
  exact scene first and submits with the returned id. The bytes are held in
  `gateway/video_assets.py` for 15 minutes and base64'd into the job at submit.

**One thing this release deliberately does not do: publish Lance 3B.** The Hub offers the package,
with `dispatch_state: ineligible` because its runner is not adopted and its files are not installed,
so the existing gates withhold it and the reason is the Hub's. When the `lance-mlx` runner lands,
the same row publishes with no gateway change.

**Verified:** e2e **574/574**, the 38 focused suites green, and `test_hub_lane_contract.py`
20/20 including the new video publication and tier tests.

## What 0.2.52/0.2.53 change

Two things, both measured live on 2026-09-26.

**A Hub batch id is a container, not a promise that work is pending.** Studio Hub creates a batch and
only *then* validates the item, so a refused or failed item leaves an upstream id on the job — and
`worker.py` treated a recorded id as ambiguity. Every ordinary item failure therefore became a durable
`unknown_outcome` hold: *"reconcile the provider request before retrying; the gateway will not resubmit
it automatically."* Reproduced twice: a **422 `Choose a qualified 1K aspect ratio`** (a caller's
geometry mistake) and a **409 `local is reserved for other work on this Mac`** (a busy machine). On a
fleet of ~50 small Macs, every engine OOM would land in the operator's recovery queue for work that
never ran.

`BackendError` gains **`upstream_settled`**, which an adapter sets only after polling its own upstream
identifier to a terminal answer. The worker lets it outrank **every** heuristic — including the
`dispatch_started_at` one, which also fired on this case and defeated the first attempt at the fix. The
paths that are genuinely ambiguous do not set it and keep their hold: an item the Hub calls `uncertain`,
a poll that hits its deadline, and a failure **fetching an artifact for an item that succeeded**. All
six are pinned in `tests/test_hub_item_settles.py` and `tests/test_job_recovery.py`.

**The gateway has had a queue since 2026-09-25 and published nothing about it.** `store.defer()` puts a
job back with a `next_action_at` and a reason when every location offering the model is merely busy; the
envelope hardcoded `queue_position`, `queue_eta_seconds` and `queue_reason` to `null`, so a queued job
and a slow one were identical to the caller. Story reads all three — its contract types `queue_reason`
as exactly `provider_pace | null`, and its Jobs page renders *"Waiting in the GenStudio provider
queue… position N… about 2 minutes… pacing new requests"* — so this fills **Story's** vocabulary rather
than inventing a second one. `queue_position` counts waiting jobs of the same modality (1 is next);
`queue_eta_seconds` is **when this gateway will try again**, never a claim about when the provider will
finish; a job that is not waiting publishes nothing at all.

The two `ponytail:` constants that bounded the wait — eight deferrals, backoff capped at a minute,
about four minutes in total — are now settings beside the pool size (`queue.max_deferrals`,
`queue.defer_cap_seconds`), read live with the same defaults. That ceiling is why a fleet busy for
longer failed work it would eventually have run, which is the owner's own requirement inverted.

**0.2.53 answers the other half of the queue: showing it.** Publishing the three fields was only
useful where something draws them, and the surface the owner actually watches is the menu-bar app's
Jobs window — which showed *"Waiting for a free machine — <reason>"* and nothing about **how far back**
the job was or **when** it would be tried again. `/admin/api/jobs` now carries `queue_position` (a real
rank among the waiting jobs of the same modality, and only when the job *is* waiting) and `retry_in_s`
(seconds to this gateway's next attempt — never a prediction about the provider), and the Jobs window
renders them as *"Waiting for a free machine — position 2, trying again in 45s"*. Each part is drawn
only when present, so a queue the gateway cannot count does not read as *position zero*. The console's
recent-jobs list carries a `waiting #N` badge for the same reason.

**One caveat worth keeping.** `store.defer()` and its wait have still never fired in live traffic —
zero jobs with a `defer_reason` since 2026-09-25. Everything above is therefore verified by tests and
by construction, not by a queue the owner has seen fill. The first real busy-fleet refusal will be the
first live evidence, and `tests/test_queue_visibility.py` plus `tests/test_job_deferral.py` are what
stand in for it until then.

**Verified live after the rebuild**, on the very request that used to hold: `recovery_required: false`,
`recovery_state: null`, and the batch id still recorded for the operator.

- **Versions are patch-only for now:** `0.2.1`, `0.2.2`, … up to `0.2.1000`. Increment the
  third number by one per shipped change; never roll to `0.3.x`/`1.0.0` until the owner says so.
- **Finished work is committed and pushed, and a release carries a DMG.** Do not call work
  done while uncommitted, unpushed, or without an installable artifact. `dist/` stays out of Git.
- **The setup kit is a snapshot, not a build output — and it is current as of 0.2.44.**
  `repository/dist/setup-kit/` pins GatewayKH **0.2.44** (`installers/gateway-kh-0.2.44.dmg`,
  sha256 `1ea5c796fd826ecd…` in `SHA256SUMS.txt`) and its `06-troubleshooting.md` gained the
  two sections this release needs: **"A node shows offline"** (the Hub's `no_hub` /
  `unreachable` / `unauthorized`, read from the installed Hub's own `peers.py` and
  `fleet_nodes.py`) and **"A job says Manual recovery required"** (discard vs close).
  Rebuild it deliberately for a rollout; never hand-edit a copy on a drive.
- **The four SSDs were last refreshed from it on 2026-09-26 at 0.2.44** `[MEASURED]` (and on
  2026-09-24, 2026-09-25 and twice on 2026-09-26 before that, at 0.2.22 → 0.2.27 → 0.2.43):
  `/Volumes/KH-STUDIO-{ATLAS,NOVA,ORICO,UGREEN}/Video Generation Kit/GatewayKH/`, each
  verified with `shasum -a 256 -c SHA256SUMS.txt` and each keeping the superseded kit in
  `<drive>/_trash/superseded-kit-2026-09-24/` (the only place a superseded installer lives;
  the repo's `dist/gateway-kh-*.dmg` is the build output, not an archive).
- **Two kit indexes exist on each drive and both name every app's version** — the top-level
  `Video Generation Kit/README.md` and the copy beside the Hub kit, `Studio Hub KH/README.md`.
  They had drifted (top said Studio Hub 0.6.54 and GatewayKH 0.2.21; the Hub copy said
  GatewayKH 0.2.2 and Studio Hub 0.6.57, while the bundle in the folder reported **0.6.60**).
  Both now read the version each bundle actually reports. **Update both on every kit refresh**,
  or the drive's front page lies about one of the three apps.
- The port is **8787** everywhere that matters: the default in `gateway/config.py`
  and the app's `AppConfig`, this Mac's `.env`/`menubar.json`, and `README.md`'s
  "Why port 8787". It was 8000 until the owner moved it (2026-09-21, `Project
  Status.md` §10h: 8000 is the most contested dev port and Story Studio's own
  default). Every Story Studio client points at `http://<host>:8787`.

## WHAT 0.2.51 CHANGED — the two kinds of provider are separate tabs

The owner, looking at the page: *"please make this into tabs for each function it's too spread
out… and also why is studio hub showing as several providers?"*

**The second question is the reason for the first, and both have the same answer.** A Studio Hub
location **is** a separate provider — six controllers, each with its own fleet of Macs, each sized
on its own — but the page drew eighteen cloud vendors and six controllers in **one list under one
column heading**, where the two are governed by **opposite** rules: a cloud limit is a number the
owner chooses, while a location's is a **cap on what the site reports for itself**. So the same
column meant two different things, and you had to read each row's sentence to know which.

- **`scope` is now on every concurrency row** (`cloud` | `local`), from the same
  `registry.describe_all()` the rest of this console reads. The derivation itself stays in one
  place — `credentials.py` reads it off the provider's **kind**, `studio_hub` being the only local
  one — and the admin route passes it in rather than `settings_view` re-deriving it.
- **The page draws two tabs, with counts**: *Cloud APIs* and *Your fleet*. The pool box stays above
  them, because it is scope-independent.
- **An unclassified provider gets its own tab, never Cloud.** `scope: null` means the caller did
  not say, and folding it into Cloud would explain its limit by a rule that does not apply to it.
- **A row the page cannot classify is worth looking at**, so that tab's hint says so rather than
  inviting a number.

**The one design decision that is not cosmetic: a tab HIDES rows, it does not re-render them.**
`concurrencyPayload()` reads the inputs that are in the document, so a switch that re-rendered would
drop the other tab's unsaved number — type a cloud limit, glance at the fleet tab, save, and it is
gone. Every row is in the HTML at all times and the inactive ones carry `display:none`.
`tests/test_concurrency_tabs.js` fails if that changes: **proved 3 of 6 on a simulated re-render.**

**Also fixed: a sentence this session's own work made false.** The page's hint read *"A Studio Hub
location sizes itself from the free workers it reports and **is not set here**."* That was true
until 0.2.49 made the control a real cap; it now says a number here caps the site and can never
raise it. A page whose explanation contradicts its behaviour is worse than a page with no
explanation.

- **Verified:** `tests/test_e2e.py` **570/570** · all **36** focused suites · all **5** console
  (Node VM) suites, including the new one.

## WHAT 0.2.50 CHANGED — the declared Studio Hub is 0.6.72 (DECIDE-1)

One line, and it took the owner to say yes: `SUPPORTED_STUDIO_HUB_VERSION` moves **`0.6.65` →
`0.6.72`**.

**The claim is earned, not assumed.** This file's own rule is that a bump means *measured against*
rather than *available* — it was deliberately **not** raised to 0.6.64 when that build was only held
by controllers and running nowhere. Everything this session did ran against a **0.6.72** Hub: the
fleet reads, the `model_limits` relay, `source_kind` end to end, the stall fields, and the whole
`0.6.71` / `0.6.72` release cycle. It is installed on this Mac and answering.

**Expect every other controller to now read `older` on the Fleet page — that is the point.** Four sit
at 0.6.65 and one at 0.6.64, and the page saying so is a true and useful prompt to roll them forward.

**It is still never a gate.** Those controllers keep working exactly as they did; nothing branches on
this number. That is the owner's 2026-09-24 decision — report drift, keep working — and it is why the
constant is a label rather than a check.

## WHAT 0.2.49 CHANGED — a Studio Hub location's number is a cap, and now it is read

The owner, planning for ~50 Macs: *"it can't be just 12. It must be a UI editable number… i want the
concurrency for local to be able to util more."* He is right, and the box he means has existed since
0.2.37 — the Providers page renders **one row per provider with an editable limit**, plus a "Jobs
running at once" box for the pool. There is **no upper bound** in the code:

```python
def _positive_int(value): ...   # any positive integer; 50, 200, whatever the fleet is
```

**The defect was that the control lied for local locations.** `Worker._provider_budget` returned
`hub_budget(slots)` and **never consulted the settings file**, so a number typed against a Studio Hub
row described nothing. 0.2.47 fixed the *display* of that (the page stopped showing a fake `1`) and
said in the row that the control was not read. Making the control **real** is the better answer.

- **`hub_budget(slots, operation, stated)` now takes the smaller of the two.** The site's count is a
  live measurement of what it can physically take; the operator's number is policy. Policy may
  **throttle** a site below what it offers; it may **never over-schedule** one, because that is how a
  queue becomes a crash. So it is a `min`, not a substitution.
- **A tie and an absent side are both labelled honestly.** `stated` with no answer from the site
  publishes `curated`, not `hub` — a number the operator set must never be dressed as the site's.
- **The console shows what will run, not what was offered**, and says which of the two is binding —
  including, when the operator's number is the *looser* one, that raising it would change nothing.
  The first version of that sentence said "raise it to use the rest" whichever way the comparison
  fell: advice that does nothing, which is the same defect as the sentence this row was fixed for.
- **The pool can be set to any number**, and since 0.2.45 a change to it takes effect without a
  restart. It is now **60**, chosen so a fleet growing past today's nine online machines never meets
  it; the per-location budgets are what actually bound each site.
- **Guard:** `AHubLocationsNumberIsACapNotALicenceTest` (6) and
  `DispatchReadsTheOperatorsNumberForALocationTest` (3). **Proved to fail on the defect** — restoring
  the old call fails with `6 != 2`, the operator's number ignored.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) · all
  **36** focused suites.

**Still open, and named rather than implied:** the console renders the pool and the per-provider rows,
but **not the per-lane (per-modality) ceilings** — `settings_view` publishes `lanes` and the page
ignores it, so those have an API and no UI. Nothing needs them today (no lane cap is set), and the
next person to want "images may use at most N of the pool" should build the control rather than
assume it is there.

## WHAT 0.2.48 CHANGED — a Hub's number arrives with the Hub's own token (HUB-20)

`_chat_row` has published `*_source` beside every limit since 2026-09-24, precisely so Story Studio
can tell a model's own figure from somebody's fallback. For a **Studio Hub location** the value is
relayed from the Hub, and `_limit_with_source` answers `provider` for *any* stated value. So the live
row published:

```
max_output_tokens: 16384
max_output_tokens_source: "provider"
max_output_tokens_source_detail: "worker default: the checkpoint ships no generation_config.json"
```

**The token said the model stated it; the sentence directly beside it said the opposite.** Story Studio
reads the token. Its own row says what that costs — *"16,384 currently renders as if the model stated
it"* — and 16,384 is the Hub's `FALLBACK_MAX_OUTPUT_TOKENS`, the number a checkpoint that states
nothing is given. Two published fields disagreeing about one number, with the machine-readable one
wrong.

- **The Hub now states `source_kind`** (Studio Hub 0.6.72): `model` — the checkpoint's own file states
  it; `worker_default` — the Hub's own fallback; `curated` — the Hub's packaged metadata rather than
  the checkpoint's word; `unknown` — nothing states it, and the value travels as `null`.
- **This gateway relays it and never parses the sentence.** Matching a word in a sentence is what the
  Hub asked us not to do, and three of their defects came from exactly that.
- **A kind travels only beside a value the Hub supplied.** When the Hub states `null` this gateway
  publishes one of its **own** (nulling the field is a breaking change for the reader, which
  substitutes 8,192 and prints it as the model's), and the honest token for *that* number is
  `gateway` — not `unknown`, which would say "nobody stated this" while the row carries a number we
  chose.
- **An older Hub that states no token behaves exactly as before.** The change is additive, and the
  absent case is pinned so it cannot become a new guess.
- **Guard:** `tests/test_hub_limit_provenance.py`, 9 tests, **proved to fail on the defect (9 of 9,
  `'provider' != 'worker_default'`)**. `LIMIT_SOURCES`' comment now names the relayed vocabulary, and
  the test reads it, so the constant is load-bearing rather than decorative.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against the
  owner's real Hub · all **36** focused suites green.

**Also fixed, and found by this change rather than looked for.** `ThePublishedLimitIsTheRealOneTest`
read the **live** `provider-concurrency.local.json` instead of isolating it, so it passed only while
that file had no `global` key. Setting one — which is 0.2.45's entire point — made two of its tests
fail **on this Mac and nowhere else**: `global_budget()` consults the file *before* the environment, so
the operator's 12 beat the `GATEWAY_JOB_CONCURRENCY` the test had just set. A test that depends on the
operator's own configuration is not measuring the code. It now patches `SETTINGS_PATH`, and a new test
states the precedence explicitly so the rule is not rediscovered.

## WHAT 0.2.47 CHANGED — the console reports the concurrency that actually runs

`settings_view` drew **every** provider from `cloud_budget`, Studio Hub locations included. A
location is governed by its own Hub's live `available_worker_slots` — `Worker._provider_budget`
reads `capacity.by_operation[<operation>].available_worker_slots` and **never consults the settings
file** — so the console showed *"no limit set, so one at a time; set it on the Providers page"* for
sites running several at once.

**Measured live before the fix:** dispatch allowed **4** for `studio-hub-9701220bf30f` while the
console said **1** for it. Two defects in one row:

- the number did not describe the system — the same shape as the `paid_concurrency_limit` literal
  that once read 4 while the worker ran one job at a time; and
- **the advice does nothing.** A limit written on the Providers page is *not read* for a Hub
  location, so the page was inviting the owner to set a number that would never take effect.

- **`settings_view`** now asks the same question dispatch asks. A Hub row reports the site's own
  slots with `source: hub`, and **names the spread** when the lanes disagree — the page draws one row
  per provider while the Hub publishes one budget per operation, so a site whose image lane takes four
  and whose voice lane takes one is not described by either number alone. Both branches now end with
  **it is not a setting**, because the old sentence said the opposite.
- **Cloud providers are untouched**: they keep the settings-file answer they are governed by.
- Failure is swallowed on purpose — an unreadable fleet must not stop the page drawing.
- **Guard:** `tests/test_worker_concurrency.py::TheConsoleReportsWhatWillActuallyRunTest`, **8 tests
  today** (6 when this was written). **Proved to fail on the defect: 4 of the 6 there were**, with
  `1 != 4 : the widest lane is the headline number`.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` · all **35**
  focused suites.

**The fleet's real ceiling, measured while doing this — this is the number that matters for the
~50-Mac rollout.** The global cap is **4**. The six locations together publish **10 free image
slots**, and `9701220bf30f` alone declares 4 (its busiest lane 8, `audio.transcription`). So the
binding constraint is **`global`**, not any one site — and since 0.2.45 it can be raised from the
console without a restart. Nothing else in the dispatch path under-uses the fleet.

## WHAT 0.2.46 CHANGED — the mode we publish is one Story Studio can read

`_chat_row` distinguishes the two structured-output modes deliberately — `response_format` is
JSON-object mode, `structured_outputs` is JSON-schema mode — and it published the stronger one as
`"json_schema"`. Story Studio's union is `'none' | 'json_object' | 'request_json_schema'`
(`genstudio-client.ts:1708`) and its parser maps anything that is neither of those onto
`'json_object'` (`:4938-4944`). So a provider offering a native JSON schema was described to Story
Studio as the weaker `json_object`: **this gateway's own correct distinction, erased on the wire.**

**It costs behaviour, not a label.** `productResponseFormat` sends a provider-native `json_schema`
with `strict: true` **only** when the product publishes `request_json_schema` (`:6234`). Read as
`json_object`, the identical request falls through to a wrapper instruction that *describes* the
schema in prose and asks the model to comply — the shape of the incident this project already paid
for, where a model *"complied for two packs of forty scenes and wrote prose for the third."*

- **`json_schema` stays correct on the request side**, where it is the provider's own
  `response_format.type` (`adapters/studio_hub.py`, `adapters/openai_compat.py`). Only the
  *published* value moved. The two spellings are not interchangeable in either place.
- **Why the spelling is ours to change:** this gateway is Story Studio's only language surface, and
  its contract is that Story needs **zero code changes** to talk to it — so the published vocabulary
  is GenStudio's, not ours.
- **Guard:** `tests/test_story_model_vocabulary.py` — the four publishing routes, plus a sweep that
  no route through `_chat_row` can publish outside Story's union. **Proved to fail on the defect:**
  restoring `json_schema` fails 3 of its 7 tests, with `'json_schema' != 'request_json_schema'`.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` · all **35**
  focused suites green.

**Still open, and named here so it is not read as fixed.** Story Studio reads **25** chat fields this
gateway never publishes — `max_structured_items_per_request` among them. That is Hub row **HUB-21**:
the field is in no installed bundle and no gateway, so the producer change is Studio Hub's. And every
`*_source` provenance field the gateway *does* publish had, when this section was written, **no Story
Studio consumer at all** — that is Hub row **HUB-20**, whose producer half has since shipped (Studio
Hub **0.6.72** publishes `source_kind`, this gateway **0.2.48** relays it as
`max_output_tokens_source`), leaving the consumer half on Story's side. Neither is a vocabulary bug of
this kind; both need work on the Story side.

## WHAT 0.2.45 CHANGED — the global cap moves without a restart

`Worker.__init__` captured `concurrency.global_limit()` once into `self.pool_size`, and `_admit`
gated on that captured number. Lanes and providers were always read **per admission**; the global
cap was the one that was not. So raising `global` on the Providers page changed what
`/admin/api/settings/concurrency` reported and **nothing about how many jobs actually ran** — the
new number would not bite until the gateway restarted, which is exactly the restart `concurrency`
promises is unnecessary: *"a fix to a rate limit should take effect without a restart while a stat
per admitted job stays cheap."* The class docstring had named `concurrency.global_limit` as this
check since it was written; now the code does too.

- **`Worker._global_limit()`** resolves it per admission. An explicit `pool_size` stays a **pin** —
  the tests pass one, and so would any caller that means to hold a fixed size.
- **Why this is the fleet's throughput and not a tidy-up.** `DEFAULT_GLOBAL` is **4**. Measured on
  the running gateway before the change: `global: {limit: 4, source: gateway, detail: "nothing set
  it, so 4; edit it on Providers"}`. Four concurrent jobs is the whole fleet's ceiling, and until
  this release the console could not move it.
- **Guard:** `tests/test_worker_concurrency.py::TheGlobalLimitMovesWithoutARestartTest` — raising
  *and* lowering the setting against a **live Worker that is never restarted**, plus the pin. **Proved
  to fail on the old behaviour:** restoring the capture fails 2 of its 3 tests, and the failure
  message names the promise that was broken.
- **Verified:** `tests/test_e2e.py` **570/570** · `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against
  the owner's real Hub · all **34** focused suites green · `test_ui.py` **not** run (release-gate
  only; it takes over the owner's screen).

**The related finding, and the one that is *not* a defect.** While measuring this, the RAM
admission path was re-read end to end. `memory_admission`'s total-memory floor is **deliberately
never in force** — *"no dispatch is refused for the size of the machine"* (`:140-142`), *"never
enforced"* (`:163`) — because FLUX.2 Klein 4B MLX 4-bit *"has completed thousands of generations on
the owner's 8 GB Apple-silicon nodes"* (`:33-36`). The **free-memory floor** is the live guard; the
published tier **ranks** (HUB-17) and **warns**. A small Mac being offered a model above its
published tier is intended, measured behaviour. Do not "fix" it.

## WHAT 0.2.44 CHANGED — the first consumer of the Hub's stall signal

Studio Hub added a progress signal to their job rows and offered it, 2026-09-26: *"it is
on the row now… nothing in our native app consumes it yet, so you would be first."*
This is that first consumer. It exists because of a number we asked for — the chat and
voice deadlines are **1,800 s**, and the Hub's argument for why thirty minutes is *safe*
rather than merely long is that a stall is visible. On this path, that something is us:
**Story Studio cannot reach a Hub.**

- **`HubProgress`** collects what the poll already returns — `/api/hub/jobs/{id}` carries
  `stalled`, `no_progress_s` and `stalled_after_s` — and the image, voice and
  transcription lanes publish it as `hub_progress` on the settled job. **No extra
  request**: the figures were already in the response we read.
- **A record, not a live signal.** A caller watching an unfinished job still sees
  `running` until it settles. The live half needs a write back to our own job row while
  the poll is in flight, and `BackendRequest` carries no job id — deliberately not built;
  see GW-13.
- **What silence must not be read as**, which is most of what the tests pin:
  * `no_progress_s: None` is the Hub saying *not applicable* — a queued or finished job
    has no progress to be missing — and folding it in as `0.0` would make every
    completed job look perfect and drag the worst case down with it;
  * a Hub that never states a figure publishes **`None`**, never a reassuring
    `stalled: false` invented out of our own silence;
  * **`stalled` latches** — one poll past the threshold is a fact about the run, and a
    later fresh poll must not erase it. The **worst** figure is kept, not the last.
- **The timeout message now says whether it had stalled.** A job that dies at 1,800 s
  having reported no progress for 1,700 of them reads as *"did not finish within 1800s;
  the Hub reported it stalled, with no progress for 1700s against a 900s threshold"*.
  That is the case the whole feature is for, and it is the one that **raises** — which is
  why `HubProgress` is passed in rather than returned.
- **Two stall vocabularies, and they are not the same field.** These are the
  *progress-based* ones (`broker.py`: threshold `max(15 min, 5 × average)`). The
  *silence-based* `stalled_seconds` (120 s, `stall.py`) is on `/api/hub/stats` →
  `fleet_activity`, a surface we do not poll for a job. Studio Hub's note described the
  second as being "on every job row"; on the row we poll it is **not there**, and a first
  consumer would have gone looking for it.

- **Proven live, not just unit-tested.** `[MEASURED 2026-09-26]` A voice job through
  the running gateway settled with
  `hub_progress: {"stalled": false, "worst_no_progress_s": 19.2, "stalled_after_s": 900,
  "samples": 22}` — and the Hub's own `/api/hub/jobs` reported that same batch climbing
  `no_progress_s` 4.4 → 19.2 while it was active. **22 of our polls carried a figure**,
  which is the field working rather than the plumbing merely compiling.
- **It covers image, voice and music, and NOT transcription — measured, not assumed.**
  `/api/hub/transcription/jobs/{id}` returns a different summary
  (`transcription_jobs.py::summary`) that carries **none** of `no_progress_s`,
  `stalled_after_s` or `stalled`. So a transcription job publishes `hub_progress: null`
  for ever, and that `null` means *"the Hub publishes no progress on this route"* — the
  honest answer, and **not** the same as the field being broken. Confirmed against a real
  transcription before and a real voice job after, on the same build.
  **Told to Studio Hub:** their offer described the signal as being on the job row
  generally; it is on the `/api/hub/jobs` family only.

`tests/test_hub_progress.py` → **11 checks**.

## WHAT 0.2.43 CHANGED — the record was blending prose with JSON, and the fleet gate had never run

Two things, and the first one is a correction to a number we published to both projects.

### The qualification record now says which kind of work it measured

`measured` published **one** median over every job, and the jobs are not all the same
kind of work. `[MEASURED 2026-09-26, from the live job store]` Of 48 settled chat jobs,
**31 asked for `json_object`** and 17 were prose — and the two measure differently:

| output shape | samples | median tokens/word | range |
|---|---|---|---|
| **text** (prose, a chapter) | 10 | **1.26** | 1.14–1.78 |
| **structured** (a JSON contract, a scene-prompt pack) | 30 | **3.15** | 1.39–25.23 |

Local model alone: prose **1.21**, structured **1.50**. **The 1.45 this gateway
published to Story Studio and to Studio Hub was a blend of the two and described
neither** — and it is the number a chunking threshold gets sized against. The single
sample we retracted earlier (1.22) was *closer to right for prose* than the better-sampled
blend that replaced it, which is a lesson about averaging across things that are not the
same.

- **`by_output_shape`** now sits on every row: `text` and `structured`, each with
  `samples`, `max_completion_tokens` and `median_tokens_per_second`. **Both keys are
  always present**, `samples: 0` when one has never run — a key that appears only once
  there is data is a key a reader has to guess at, and a missing shape split is exactly
  the state this replaced.
- **The headline figures stay whole-envelope, deliberately.** "Can this lane carry N
  tokens" is answered by the largest that ran whatever it was; splitting the headline
  would hide the case a ceiling decision is about.
- **The shape comes from the request**, read in SQL from `params.response_format`:
  `json_object` and `json_schema` are structured, **anything else including absent is
  prose** — a caller that asked for nothing asked for text.
- **The fleet merge carries the split**, or the fix would be right per location and
  wrong on the one row Story Studio reads.

`tests/test_qualification.py` → **38 checks** (5 new); `test_e2e.py` → 2 new.

### The fleet gate ran for the first time in fifteen releases — and the gateway was right

`test_fleet_standalone.py` had not run since 0.2.28 because it needs a live Hub and an
explicit opt-in. Run on 2026-09-26 against Hub **0.6.70**: **green — 47 checks in that
run**, including a real transcription end to end and the artifact returning through the
gateway. **The suite's total follows what the live Hub reports** (this checkout now has
**54 check sites**, some in mutually exclusive branches), and it prints the count it
actually executed; quote that, not this number.

**Three defects, all in the test, none in the product.** Worth recording because each
would have made the gate unrun or dishonest:

- **It could not find its own input.** The clip search accepted only `.mp3` under the
  artifacts dir and this gateway writes **`.wav`** for every TTS artifact — 4 `.wav`,
  0 `.mp3`, measured — so that half could never match and the gate stopped at section 3
  with `a real clip is available to transcribe — None`. It also took `next(glob(...))`,
  i.e. whatever the directory happened to order first, with a 298-second render sitting
  beside a 3-second one: **the gate's runtime was a lottery**. Now the smallest real
  clip, any audio type.
- **It named a model instead of reading the fixture** — `mlx-community/whisper-base-mlx`,
  which on this Hub is `not_installed`, so the gateway **correctly** withheld it as
  unservable. One absent model cascaded into **five** failures, and **three of them
  passed vacuously** because `all(...)` over an empty set is `True`. This is the defect
  the tracker already records for a hardcoded Hub version — *"a test is a row, and it
  lies the same way"* — and the fix is the same: derive it from what the Hub offers.
- **`the Hub refused every transcription model it offers:` printed with nothing after
  it**, because nothing had been submitted. A refusal and an empty list must not share a
  sentence.

## WHAT 0.2.42 CHANGED — the Hub moved its numbers, and we moved our mock first

Studio Hub raised `FALLBACK_MAX_OUTPUT_TOKENS` `4096 → 16384` and made the deadline
**per lane** (chat 1,800 s; image/voice/transcription/music 900 s; video 7,200 s),
replacing the single `PEER_PROXY_TIMEOUT_S = 900.0`. Their release is **not cut yet** —
working tree only, nothing installed, nothing on the drives, and **GW-5 cannot
re-discover until it is**. Until then the local lane is still 4,096 / 900 s.

- **They warned that their own suite lost eight chat tests to it** — several asserting
  the bare literal `4096`, several fixtures hand-tuned to sit *just above* the old
  fallback, which **silently inverted** at 16,384: the fixture became the binding
  constraint instead of the fallback. *"Any number of yours that is written as a
  literal, or sized relative to our fallback, is exposed to the same inversion."*
- **Answered empirically, not by inspection: our mock Hub was moved to the new values
  first and the whole suite run against it.** Two tests went red, both quoting old
  numbers rather than the fixture — `max_output_tokens == 4096` and
  `deadline_seconds == 600.0`. **The gateway code was correct in both cases.** Every
  other suite passed unchanged, e2e included.
- `tests/mock_upstream.py` now **names** `HUB_CHAT_MAX_OUTPUT_TOKENS`,
  `HUB_CHAT_CONTEXT_TOKENS`, `HUB_CHAT_DEADLINE_S`, `HUB_VOICE_DEADLINE_S`, and both
  fixtures and assertions use them — the next move is one line and no test can quote a
  stale number. That is the `AGENTS.md` rule (*"asserted a hand-written key list from
  the live Hub, not the fixture"*) applied to a constant instead of a key set.
- **The inversion is now impossible by construction**, with a test on the mechanism
  rather than the value: *"a model that states less than the fallback keeps its own
  number"* asserts whatever the Hub states reaches the row, so it stays true at 16,384,
  at 32,768, and at whatever they pick next.
- **Per-lane deadlines have a test too.** Two models, two deadlines, asked in one
  process, plus a repeat of the first to catch a cache — because the hazard they named
  is not reading the wrong field, it is reading it once and reusing it, which passes
  every single-lane test there is.
- **`JOB_DEADLINE_S` stays a flat 900, deliberately**, and its docstring now says why
  so nobody "fixes" it to the largest lane. It is reached only when a lane states
  nothing, and a Hub that states nothing is a Hub from *before* the change, whose one
  proxy timeout *was* 900 for every lane. Per-modality numbers would be a second copy of
  their table.
- **Video's 7,200 s and its `NON_DURABLE → DURABLE_ASYNC` reclassification do not reach
  us**: `KIND_STUDIO_HUB` serves `{chat, image, tts, music, transcription}` — no video.
  Recorded so nobody goes looking for a lane we do not have.
- **Our framing was wrong and their correction lowers the cost we had accepted.** We
  said `PEER_PROXY_TIMEOUT_S` was *"the one constant that drives the whole ceiling"*; it
  was, until they derived the deadline per generator. **The 30-minute hang on fast jobs
  is not paid** — only chat moved.

`test_hub_lane_contract.py` → **17 checks** (2 new); full sweep and `test_e2e.py`
568/568 green against the new Hub values.

## WHAT 0.2.41 CHANGED — the median was hiding the slow machine

One field, and it came from the owner measuring his own fleet: **M1 12 tokens/second,
M2 14, M4 18.** This gateway's per-location medians (16.8, 16.6, 14.2, 11.6) are an
independent measurement of the same machines and agree with that range.

- **Throughput is a property of the chip, and the fleet is mixed.** So the number a
  ceiling must be safe against is the **slowest** machine's, not the fleet's median —
  and `measured` published only the median, which was 15.4 and hid the 12.
  `slowest_site_tokens_per_second` now sits beside it.
- **It corrects a claim this file made one version earlier.** 0.2.40's notes said 8,192
  takes 532 s with 1.6–1.7× headroom; that used the median. Sized against an M1 it is
  **683 s, 1.32×**. The conclusion survives — 8,192 is still the ask, and 16,384 now
  **times out on every chip in the fleet** (910 s even on an M4) — but the margin did
  not, and a margin is the whole reason the number was quoted.
- **Why the caller cannot do this sizing itself:** the gateway picks the location at
  dispatch, so "which Mac will run this" is not a question a caller can answer. The
  spread has to be published, not averaged away.
- `null` when no rate was measured — `min()` over nothing is not zero, and a lane
  nobody timed must not read as the slowest thing in the fleet.

`tests/test_qualification.py` → **33 checks** (2 new).

## WHAT 0.2.40 CHANGED — the measurement reaches Story Studio, and it is read in SQL

Two changes, both about the same record. `measured` is now on **every catalogue row**,
so Story Studio reads it from `/v1/models` and `/v1/chat/models` — the surface it
already polls — instead of the admin route it cannot reach. The shape, and what it is
and is not for, is STORY-7 in the master tracker.

- **`max_output_tokens` is untouched.** Still exactly what the producer states, with
  its own `*_source`; `measured` sits beside it and `source: "gateway"` is **inside**
  the object, so the label cannot drift from the numbers it describes. A model nobody
  has run publishes `measured: null` — unknown, not zero — and there is an e2e check
  for each half.
- **A location's work is published under the maker.** A job routed to
  `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`, and the same
  model at six sites is one row summarised **across all six** — `samples` summed, the
  maximum taken as a maximum, the rate as a median of medians. Keying the join on the
  raw provider id left every local row reading as unmeasured, which is the one lane
  the question is about. `published_index` always emits the same keys whether one
  location contributed or six: a shape that changes with the number of contributors is
  a shape a reader has to guess at.
- **The read moved into SQL, and that is the second change.** `store.usage_samples`
  extracts four numbers with `json_extract` and leaves the generated prose in the file.
  `result_json` holds the *whole* completion — ~16 KB on a 4,096-token answer — so
  pulling `SELECT *` over 500 jobs to read four fields would materialise megabytes
  every rebuild, which is every 20 s on a path Story Studio polls. Measured: the query
  returns 54 rows in **0.4 ms** on the live store.
- **Two guards, both of which a test forced, and both of which would have shipped
  silently:**
  * `CASE WHEN json_valid(...)` rather than `WHERE json_valid(...)` — a WHERE clause is
    not a promise about evaluation order, SQLite may evaluate `json_extract` first, and
    it *raises* `malformed JSON` rather than returning NULL. One unreadable row would
    have taken every other row's measurement with it.
  * `json_type(...) IN ('integer','real')` — **`json_extract` returns `1` for a JSON
    `true`**, so the `bool` guard that rejected `{"completion_tokens": true}` stopped
    working the moment the read moved into SQL, and `True` re-entered the record as a
    one-token completion. The focused suite caught it. The type has to be checked where
    it is still a JSON type; a stringified `"7"` is refused by the same clause.
- **The catalogue never imports the store.** `runtime` imports `catalog` at its own
  module level, so a module-level import here is a cycle — and the lazy-import version
  produced **no measurement at all**, silently, because `catalog = Catalog()` runs
  during that module's import, before `runtime` has built the store. It is
  `attach_store(store)`, called explicitly after both exist.
- **Known limit, stated rather than hidden:** one envelope per (provider, model)
  whatever the work was, so a prose chapter and a scene-plan JSON body share a row and
  `max_completion_tokens` is the largest of either. That matters because JSON is denser
  in tokens per word than the prose the ratios were measured on. Every job already
  stores its `params`, so splitting on `json_extract(params, '$.response_format.type')`
  is a read change and not a schema change — do it when a lane has run enough of both
  to be worth telling apart. `ponytail:` in `qualification.py` names this.

`tests/test_qualification.py` → **31 checks** (10 new); `test_e2e.py` → 4 new,
**568/568**.

## WHAT 0.2.39 CHANGED — a model is qualified by what it has really carried



Asked 2026-09-26: *"we need to keep these for that when I want to qualify a new LLM,
we can apply the same parameter check too."* Before this the figures were read once by
hand and lost, so every question about a ceiling was answered from a producer's
advertisement. `gateway/qualification.py` reads them back off the work itself.

**Nothing new is recorded for it.** Every figure was already in
`result_json.payload` on each settled job: `usage.{prompt,completion}_tokens`, the
worker's own `generation_seconds` (after the model is resident, so it excludes a cold
turn's load), and `hub_machine`/`hub_worker` on the image, voice and music lanes. The
change is a reader, not a schema.

- **`GET /admin/api/qualification`** — per (provider, model): samples, the largest
  prompt and completion **that actually ran**, the median rate, per-machine splits,
  first/last seen. `?provider_id=`, `?model_id=`, `?limit=` (1–500, default 500).
- **The measured maximum is the headline, not the stated one.** "Can this lane carry
  8,000 tokens" is answered by the biggest it has carried; "may I ask" is a different
  question with a different owner.
- **`stated` is joined on the *published* namespace.** A job routed to
  `studio-hub-9701220bf30f` is catalogue row `studio-hub::~<model>`; comparing raw ids
  made every local row read as unknown, which is the one lane the ceiling question is
  about. `stated: null` means *not in the catalogue* (disabled or withheld) —
  unknown, never unlimited.
- **`source: "gateway"` on every row, and the stated figures sit beside it, never
  merged into it.** This is the rule the whole feature rests on, and it has its own
  test.
- **The console draws it** — Providers → **Measured**, under Concurrency. A field
  nothing draws is the same defect as a control that does nothing. An unmeasured
  figure renders as a dash, never a zero; a chat model's machine column reads
  **not named** rather than guessing.
- **What is deliberately *not* counted**: a queued job is not a sample; a lane that
  states no `usage` is skipped rather than folded in as zero tokens; a missing
  `generation_seconds` is an absent rate, never `0`; `True` is not a token count
  (`bool` is an `int` in Python, and read as a number it would enter the record as a
  one-token completion). Each of those turns a record of real work into a
  plausible-looking invention.
- **`limit` is declared with `Annotated`, and that is load-bearing.** Written the
  usual way — `limit: int = Query(50, ge=1, le=500)` — the default *is* a `Query`
  object, so calling the function directly handed it to SQLite:
  `type 'Query' is not supported`. The route was reachable **only** over HTTP, which
  is a worse test surface and a trap for the next in-process caller. The focused
  suite caught it. `/admin/api/jobs` still has the old shape; nobody has needed to
  call it directly.

`tests/test_qualification.py` → 21 checks; `test_console_actions.js` → 5 new;
`test_e2e.py` → 7 new, **564/564**.

### What it measured, while it was being built `[MEASURED 2026-09-26]`

Read off the **running** 0.2.39 gateway, `GET /admin/api/qualification`:

| lane / model | jobs | largest that ran | stated | stated by | tok/s |
|---|---|---|---|---|---|
| `openrouter-main` deepseek-v4-flash | 1 | 15,139 | 384,000 | provider | — |
| `experiential-labs` deepseek-v4.1-flash | 1 | 8,483 | *(not catalogued)* | — | — |
| `openai-direct` gpt-6-luna | 19 | 6,221 | null | gateway | — |
| `tokenharbor` mimo-v2.5:free | 1 | 5,045 | 128,000 | curated | — |
| **`studio-hub` Qwen3.5-9B-4bit** *(4 locations)* | **23** | **4,096** | **4,096** | **provider** | **15.4** |
| `studio-hub-b8ccda585b97` Qwen3.5-9B-4bit | 2 | 2,350 | 4,096 | provider | 16.6 |
| `studio-hub-de7250e146a7` Qwen3.5-9B-4bit | 5 | 841 | 4,096 | provider | 14.2 |
| `studio-hub-local` Qwen3.5-9B-4bit | 3 | 27 | 4,096 | provider | 11.6 |

- The **chat lane names no machine.** Its payload carries `finish_reason`, `model`,
  `usage` and `generation_seconds` and nothing that identifies a Mac, so a chat
  model's envelope is per location until Studio Hub names one. The image, voice and
  music lanes do name one.
- **4 of the 13 stopped at exactly 4,096 completion tokens, and all 13 reported
  `finish_reason: "stop"`.** The cap is being hit and reported as a clean stop — so
  *"finish_reason must be a clean stop"* does not, on its own, detect truncation.
  Only the measured maximum next to the stated ceiling shows it. This is HUB-18 and
  GW-10 in the master tracker, with the arithmetic below.
- **The same model measured 15.4 tokens/second across the fleet** — 23 jobs over 4
  locations, published as `measured.median_tokens_per_second`, and a median of the
  per-location medians (16.8, 16.6, 14.2, 11.6) so one Mac with many jobs cannot
  dominate it. Use this one; earlier notes here said 16.8 (the best-sampled single
  location) and an even earlier one said 17.3 (**one 489-token sample**, retracted).
  Also measured: 1.22 tokens/word, 7.0 characters/word, 5.7 characters/token. Against
  the chat lane's own stated deadline — **900 s then, 1,800 s since Hub 0.6.70**:
  4,096 → 266 s, 8,192 → **532 s**, 16,384 → **1,064 s, and it now fits the window
  rather than dying at it**.
- **The round trip is the Hub's `FALLBACK_MAX_OUTPUT_TOKENS`, and Story Studio's
  chunker is not the defect.** An earlier version of this file implied the chunker was
  over-tuned. **That was wrong and is retracted** — see HUB-19. Their
  `OUTPUT_TOKENS_PER_WORD` was 1.3 then and is **1.6** today (raised 2026-09-26 on both
  sides' measurements), and their threshold is `chunkThresholdWords(cap)` computed live
  from the ceiling **we** publish: **372 words** at 4,096, **2,932** at 8,192, **8,052**
  at the **16,384** the Hub now ships. At 16,384 a 1,200-word chapter is one call, with
  **no Story Studio change at all**. Removing the chunker instead would lose chapters
  silently, because a generation cut off at the ceiling still reports
  `finish_reason: "stop"`.
- **A source can arrive with no number.** `openai-direct::gpt-6-luna` publishes
  `max_output_tokens: null` **with** `max_output_tokens_source: "gateway"` — that is
  `_limit_with_source`'s `if fallback is None: return None, "gateway"` branch, and it
  is deliberate: the three-word vocabulary (`provider`/`curated`/`gateway`) has no
  fourth word for "nobody said", and adding one is a breaking change for a reader that
  knows only three. **Left exactly as it is.** What it did cost was a bug in the new
  console panel, which collapsed *"not in the catalogue"* and *"in the catalogue, but
  it states no ceiling"* into one string and so told the reader a model was missing
  when it was present. The panel now has **three** states, and there is a test.
  The live record is what showed it; a fixture would not have.


**This is the change that makes a fleet worth owning.** The worker ran exactly one
job at a time — chat, image, TTS, subtitle and music all shared a single slot — so
nineteen Macs sat idle behind one render. It was deliberate, and the reason is
still recorded in the file it was written in: *"a second thread would add
concurrency bugs (double-claiming, artifact races) for no benefit."* Both risks
were re-checked and both are smaller than that note claims; the cost of the old
shape is not.

- **Three ceilings**, in `gateway/concurrency.py`, all of which must pass before a
  job is admitted: the **pool** (`GATEWAY_JOB_CONCURRENCY`, default 4), the
  **lane** (`GATEWAY_LANE_CONCURRENCY=image=1,chat=4`, empty by default), and the
  **provider**. Each answer carries its `source` — `hub` (a site's own
  `available_worker_slots`), `curated` (`data/provider-concurrency.local.json`), or
  `gateway` (a labelled default of 1, naming the file to edit).
- **`paid_concurrency_limit` is now derived, not written.** It was the literal `4`
  while one job ran at a time: a published number that did not describe the system,
  in a field Story Studio reads. It now comes from the same function that sizes the
  pool, so the two cannot drift.
- **Daemon threads, not a `ThreadPoolExecutor`.** An executor joins its threads at
  interpreter exit, so quitting during a two-minute render would hang the process
  instead of stopping it. The dispatcher is single-threaded and is the only
  admitter; correctness of the claim does not depend on that, only tidiness of the
  counters.
- **A limit of zero is not a stop.** A Hub reporting no free slots is busy right
  now, not closed; treating `0` as a cap would leave jobs due for ever with nothing
  wrong and nothing to see. Zero falls back to 1, and there is a test for it.
- **Two bugs the new tests caught**, both of the "setting that silently does
  nothing" shape this project keeps finding: `_positive_int` rejected numeric
  **strings**, so `GATEWAY_JOB_CONCURRENCY=7` and `GATEWAY_LANE_CONCURRENCY=image=1`
  were ignored and fell back to the default; and `test_job_claim`'s *"the loop
  returned, therefore the job ran"* became a race the moment `_loop` stopped
  executing inline. `Worker.wait_idle()` exists for the second one.

`tests/test_worker_concurrency.py` → 13 checks then (**45 tests today**), stable over
five runs. Focused suites 30/30, `test_e2e.py` 555/555.

## WHAT 0.2.28 CHANGED — and the three things it cost to learn

- **GW-5: a location's models are re-read when its Hub changes build.** The refresher
  already probed every location and already knew the version, and did nothing with a
  change; discovery ran only when somebody pressed Refresh. `fleet.refresh_models_when_hub_changes`
  now compares the probed version against **the discovery cache's own `hub_version`** —
  not memory, because the gateway restart usually follows the update — and re-discovers
  on a difference. A location with no record is refreshed once, so it seeds itself. A
  **failed** discovery records nothing, so the next pass retries instead of marking
  stale rows current. The manual Refresh records the same baseline, so it is not redone.
  `tests/test_fleet_hub_version_refresh.py` → 10 checks. Retires OWNER-5.
- **GW-4: the declared Studio Hub is `0.6.65`**, set from a running Hub rather than a
  changelog. It was deliberately **not** set to `0.6.64` while that build was only held
  by controllers and running nowhere: the constant means *measured against*, and that is
  a claim a version nobody has run cannot support.
- **A test is a row, and it lies the same way.** `test_studio_hub_contract.py` asserted
  `hub_version_relation("0.6.64") == newer` with the numbers written in by hand. Bumping
  the declaration turned it red — correctly. It now derives every comparison from
  `SUPPORTED_STUDIO_HUB_VERSION`, so the next bump cannot break it. **When you move the
  declaration, grep the tests for the old number.**
- **The first fleet-wide release push, and what it found** `[MEASURED 2026-09-25]`:
  `POST /admin/api/release/publish` → **5 of 5 controllers accepted**, HTTP 200 in
  2563 s, and every controller countersigned with a **different** key, so each mirror is
  its own. Two things to carry forward:
  - **The request body is base64**, so a 1.34 GB artifact becomes 1.79 GB in one body and
    the gateway's RSS peaked at **8.0 GB**. That is the ceiling to watch on a smaller
    host, and the reason the controller-mirror beats pushing to every node.
  - **What is held and what the kit has are different facts.** The push landed 0.6.64
    while all four kit drives already carried **0.6.65**. Check both before aiming a push.

## THE WORKING LOOP

Run everything from `repository/`. Python tooling is the checkout venv `.venv/bin/python`.

| # | Step | Exact command / action | Footgun next to it |
|---|---|---|---|
| 1 | Make the change | edit under `repository/` | Never `rm` — move removals to `_trash/` (see below) |
| 2 | Bump the version | edit `repository/VERSION` | Patch-only; `build.sh` reads it for `CFBundleShortVersionString` and the DMG name |
| 3 | Build + DMG | `./menubar/build.sh` | Installs to `/Applications/gateway-kh.app` **and** writes `dist/gateway-kh-<VERSION>.dmg`. `--no-install` builds only into `menubar/build/` |
| 4 | Test narrowly | console/JS → `node --test tests/<file>.js`; Python → `.venv/bin/python tests/<file>.py`; Swift → `tests/check_native.py <check>` in the background | See the test rules below |
| 5 | Restart the app | kill the **PID** of `GatewayKH`, confirm port 8787 is free, then `open /Applications/gateway-kh.app` | Relaunching is pre-approved; `pkill -f "GatewayKH"` kills the shell running it |
| 6 | Ship | `git add -A && git commit && git push` on `main` | The DMG already exists from step 3; nothing else is required to hand it over |
| 7 | Publish a Hub app release (optional) | Fleet → Locations → *Publish a Studio Hub release…* → `POST /admin/api/release/publish` | Signing key stays in the data dir; trust install on a controller is the **owner's** action |

- **Verify the running build, not the bundle.** Read
  `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD` and `curl /health`; both must
  show the stamp you just built. A gateway outlives the app and the app **adopts** whatever
  answers `/health`.
- **`tests/test_ui.py` is a release gate, not a routine check.** It forces the window to 700 pt
  and presses controls, taking over the owner's screen. Reserve it for an explicit owner request
  or a promotion gate, run it in the background, and never run it for a change that touches no
  app file.
- **The real-Hub fleet suite is opt-in:** `GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python
  tests/test_fleet_standalone.py`. Without the variable it skips before contacting the Hub.
- **Always rebuild before claiming it works.** "It builds" is not "it works."
- **The release gate reads the Fleet page's *five tabs*, not the old single scroll.** In
  `tests/test_ui.py` §7 the tabs are AX **radio buttons** (`select_fleet_tab`: 1 Overview,
  2 Locations, 3 Machines, 4 Models, 5 Jobs), the page *remembers* the last tab, and each
  tab addresses its table differently — Locations and Jobs are nested one level
  (`table_texts(n, nested=True)`), Machines is a bare `table 1 of scroll area 2`
  (`nested=False`). Change the Fleet tabs and §7 must change with them, or the gate reads
  a loading view and reports the tables missing.
- Structural change → keep the graph current: `graphify extract . --update --code-only`.

## PATHS

| What | Where |
|---|---|
| Git checkout (all source) | `repository/` |
| Container (not a repo) | `..` = `gateway-kh/`; holds `test-instance/`, `backups/`, `_trash/`, `worktrees/`, `store-analysis/` |
| Build script | `repository/menubar/build.sh` |
| App source | `repository/menubar/*.swift`, `build.sh`, `whats-new.md` |
| Gateway source | `repository/gateway/` |
| Tests | `repository/tests/` |
| Build output / DMGs | `repository/dist/` (gitignored); container-root `dist` is a symlink to it |
| SSD setup kit | `repository/dist/setup-kit/` — copied to `/Volumes/KH-STUDIO-*/Video Generation Kit/GatewayKH/` |
| Release notes | `repository/menubar/whats-new.md` (newest first; shown by the app's What's New) |
| Live data dir | `~/Library/Application Support/gateway-kh/` (`.env`, `data/`) |
| Release publisher / state | `~/Library/Application Support/gateway-kh/data/release-publisher.pem` (0600), `release-state.json` |
| Test instance (code-only) | `test-instance/` — `./run.sh`, `./status.sh`, `./stop.sh`, port 8100 |
| Graph | `repository/graphify-out/` (gitignored, rebuildable) |

## RULES THAT COST US TIME

- **Never `rm` — every removal goes to `_trash/`.** That includes clutter you decide on
  yourself, keeping sub-structure, and say where it went. `_trash/` is the **owner's** to
  empty: never delete or tidy inside it. Git branches/refs are the exception — record name and
  SHA in a note under `_trash/` first. (`_trash/` is the owner's: it may be nearly
  empty, and its README may list items he has since removed — that is his call, not
  a defect to fix.)
- **`pkill -f "GatewayKH"` kills the shell running it** because the shell's own command line
  holds the pattern; the command never finishes and the old gateway survives. Kill by **PID**,
  then prove the PID changed and 8787 is free before measuring.
- **Never hand another session a description of behaviour that is not deployed.** Either
  restart and measure the running service, or say "built but not live."
- **Never return 402.** Story Studio holds a credit lock keyed to the API key; one 402 kills it
  permanently. A provider out of funds is a settled `failed` job — `test_e2e.py` asserts no 402.
- **Absence is `null`/absent, never a plausible `0` or default.** "Nobody stated it" and "it is
  zero" must not render the same. A stated `0` is a claim — read with a first-non-`None`
  lookup, never an `or` chain.
- **`freshness.stale` is always `false` by construction — do not "fix" it.** Making the gate
  fire would disable every model on a stale inventory. `inventoryAgeMs` is the honest number.
- **Never identify a machine, model, location or batch by its label.** Ids are identities;
  labels are the owner's to change. A location's provider id derives from its controller id.
- **A Hub's `site_id` is not unique until it is assigned one.** Studio Hub defaults it to the
  constant `"local-site"` (`components/hub/backend/control_plane.py::defaults`), so matching a
  new controller on it made the second "Add a location" overwrite the first. `fleet._stable_identity`
  falls back to the per-machine `controller_id`; do the same anywhere a controller is keyed.
- **A definite 4xx from any provider is its own refusal, never an unknown-outcome hold;**
  only a 5xx may have been accepted. The worker enforces this centrally on `BackendError.status`,
  so every adapter inherits it. A **connect timeout/refusal** is an unreachable location, not a
  job failure — but a read timeout *after* the request left is still ambiguous. A `400` that
  names `temperature` as unsupported is retried once without it (recorded as `params_dropped`),
  because newer reasoning models accept only their default — and `catalog._DEFAULT_TEMPERATURE_ONLY`
  now withholds that control from the known families on the provider that refuses it, so the
  retry is a backstop rather than the fix.
- **A page must never resize the window.** A page with a required height wraps its body in an
  `NSScrollView` with a `FlippedView`; keep table floors low and cap wrapping labels.
- **The bundled Python runtime's build is explicit:** `swiftc -target
  "${TARGET_ARCH}-apple-macos${MACOS_MIN}"` (default 11.0). Without `-target`, `swiftc` stamps
  the SDK's macOS version and macOS 14 refuses the app with "requires macOS 27".
- **`graphify` reports a syntax error in `menubar/VoicesWindow.swift` — a parser limitation,
  not a defect.** `swiftc -parse` is clean; trust the compiler.
- **Mark deliberate simplifications with a `ponytail:` comment** naming the ceiling and the
  upgrade path, or the next reader cannot tell a trade-off from an oversight.
- **`finish_reason` must be a clean stop** (`length` is read as truncated), and generation
  artifact URLs stay relative on the API origin.
- **A field nothing draws is a defect, and so is a fault repeated once per consequence.** The
  node roster's per-node `detail` and each machine's `health` and `available_gb` were fetched,
  published and read by nobody, so eight dead nodes were eight identical "offline" rows; the
  same fleet produced **148 issues for nine conditions** because one dead machine was reported
  as state + health + one row per studio + one per unreachable model. Before adding a field,
  name its reader; before emitting an issue, ask whether it is a *different* fault or the same
  one in another vocabulary.
- **A user-visible sentence is a contract when a test reads it.** `78fee1d` rewrote the Fleet
  page and lowercased "Could not discover" while moving "Retry failed" into `FleetTabs.swift`;
  `tests/test_task4_native.py` went red on 2026-09-23 and nobody noticed because that suite is
  not in the documented verification list. **Run every `tests/test_*.py`, not only the three in
  the table below**, or a red gate hides for days.
- **A version is declared, never a gate — and read what the far side states.** Two rules
  bought together on 2026-09-24. (1) Studio Hub auto-updates itself, so a runtime check on its
  version would turn *its* update into *this* fleet's outage; the gateway names the release it
  targets (one constant), reports a drift as information, and keeps every gate a capability
  question. (2) The same instinct in discovery: keying a Hub package to a modality by its
  **engine name** made a new engine invisible until this repo was edited, while the Hub was
  already stating `hub_modality` on every model. **When the far side states a fact, read it and
  keep your own table as the fallback** — the table's entries were guesses about a name, the
  stated field is the Hub's own answer. Anything left over is *reported by name*, because "it
  has nothing" and "I did not understand it" must never be the same answer.

## DO NOT TOUCH

- **Other repositories.** `~/Developer/studio-hub-kh`, `~/Projects/studiohub-standalone` and
  `theng12/Studio-Hub-KH` belong to other sessions and are read-only from here. The Hub fact
  that matters is the **installed bundle**
  (`~/Applications/Studio Hub.app/Contents/Resources/voice/`), which can differ from a checkout.
- **Other sessions' worktrees.** `worktrees/` and `test-instance/` are not yours to edit; the
  owner decided the test instance stays **code-only** (no keys, no Hub).
- **`backups/`, `_trash/`, `store-analysis/` and any archive/trash folder.** Recoverable
  history; moving things *in* is allowed, deleting or emptying is the **owner's**.
- **GitHub Actions stays disabled**, and do not include the real-Hub fleet suite in offline
  smoke testing.
- Do not restart the app without a reason — it interrupts the owner — though relaunching is
  otherwise pre-approved (he asked not to be asked, and to quit it yourself).

## OPEN / IN FLIGHT

> **Read this section as a dated log, not as current status — corrected 2026-09-26.**
> Most of it was written across 2026-09-24/25 and several of its facts have moved since.
> Rather than rewrite each bullet (which would destroy the record of what was true when
> it was written), here is what changed:
>
> | then | now `[MEASURED 2026-09-26]` |
> |---|---|
> | both controllers on Studio Hub **0.6.54** | `0.6.65` × 4, `0.6.64` × 1 |
> | this Mac's Hub **0.6.60/0.6.63** | **0.6.70** build `30ecef1` |
> | "Studio Hub is not running on this Mac" | it is running, and all four workers report ready |
> | the chat lane publishes `max_output_tokens: 4096` | **16384**, and `deadline_seconds` **1800** |
> | `test_e2e.py` **546–555/555** | **570/570** |
> | the remote chat proof "blocked on our side" | **closed** — GW-1, and the fleet gate is green (54 check sites; it prints its own total) with a real remote dispatch |
>
> **A stale handoff is worse than none, because it is trusted.** If a number here
> disagrees with the table at the top of this file, the top of the file was measured.

- **Shipped in 0.2.25 (`0b03441`): the Hub chat lane is dispatchable, proved live on
  0.6.63.** Studio Hub 0.6.63 opened `POST /studio/chat/v1/chat/completions` to a
  **Fleet** credential, which is what made a Hub LLM possible here at all — before
  it, every chat route refused a fleet token. Reply:
  `_handoffs/2026-09-25-gateway-chat-lane-shipped.md`.
  1. `chat` is declared in all three places: `adapters._MODALITIES_BY_KIND[KIND_STUDIO_HUB]`,
     the `studio-hub-local` definition, and the **per-controller** definitions the
     roster derives (`providers/registry.py`) — the third is what lets a *remote*
     controller serve chat.
  2. `adapters/studio_hub._generate_chat` posts the OpenAI body to the **peer**
     surface through the Hub (admission, machine lease, worker signing), never a
     worker's own route and never the Hub-token-only `/api/hub/chat/completions`.
     `stream: false`; only the lane's honoured parameters are sent (asserted on the
     wire); `MODEL_NOT_PACKAGED`/`MODEL_NOT_READY` → `LOCATION_NOT_READY` so the
     worker re-routes; a `422` is a definite refusal, never an unknown-outcome hold;
     an empty answer is a failure.
  3. The catalogue publishes the Hub's own `model_limits` (262,144 read from the
     checkpoint, 4096 as a labelled worker default) with `*_source_detail` carrying
     the Hub's provenance sentence; `verified_token_usage: false` on every chat row
     (we validate what a provider states, we do not re-measure); and
     `generation_seconds` on the job envelope — the worker's own number, never
     derived from our round trip.
  `tests/test_hub_chat_lane.py` (7 checks, **wire** assertions),
  `tests/test_fleet_routing.py::HubChatRoutingTests`, `test_e2e.py` **555/555**.
- **Live, on this Mac's 0.6.63 Hub `[MEASURED 2026-09-25]`:** a real completion
  through the gateway — `succeeded`, `output_text: "Red, Blue, Green"`,
  `finish_reason: stop`, `usage 22/5/27`, `generation_seconds 0.573`,
  `verified_token_usage: false` — and the Hub's own `GET /api/hub/jobs` records
  those runs as `"modality": "chat"`. A `json_object` request returned parseable
  JSON first try, with the Hub's caveat recorded: **no grammar-constrained
  decoding**, so a strict-JSON call site is prompting plus validation.
- **The published model id is undecided on purpose.** The Hub's handoff says Story
  Studio's `lmstudio:` two-pass behaviour is keyed on an id **we** control and their
  tracked source has no code selecting two-pass from a model id, so they asked us to
  ask the Story session rather than guess. It publishes as
  `studio-hub::mlx-community/Qwen3.5-9B-4bit` (the normal Hub namespace) meanwhile —
  nothing is blocked. **Open question for the Story session.**
- ~~**The remote chat proof (their test-plan item 4) is blocked on our side:** both
  controllers are on **0.6.54**, which refuses the chat lane to a Fleet credential.~~
  **CLOSED 2026-09-25 (GW-1) and re-proved 2026-09-26.** The controllers are on
  `0.6.65`/`0.6.64` and the fleet gate runs a real remote dispatch end to end —
  `tests/test_fleet_standalone.py` **green** (54 check sites; the suite prints the total it executed) against Hub `0.6.70`, including a
  transcription and the artifact returning through the gateway. Struck through rather
  than deleted because it was the reason the declared version moved at all.
- **A stale `dispatch_state` reads as "not installed".** The first live chat attempt
  was withheld because the cached package row said `files_state: not_installed` /
  `dispatch_state: ineligible` while the Hub answered a completion perfectly; a
  discovery refresh cleared it. Recorded as a property of the cache, not a defect on
  either side — but worth knowing before believing a withheld chat row.
- **Memory policy (Hub §3) is untouched by this gateway** — not read, not written,
  not on any page — so the five-mode change and the `auto` skew cannot break us. When
  it is surfaced: show `resolved_mode` and the resolved `idle_seconds`, never `mode`,
  because a null idle threshold renders as "kept loaded" — the opposite.
- **Shipped in 0.2.24 (`18408cc`):** the lane contract Studio Hub 0.6.61 published
  (`f20fd1a`, spec `docs/lane-execution-contract.md`) is read, and the one thing it
  does not yet give us is reported back in
  `_handoffs/2026-09-25-gateway-lane-contract-reply.md`.
  1. **Three readers place a model, strongest first** — the package row's own
     `modality` (0.6.61; present whenever the engine is registered, independent of
     the best-effort catalogue read), the catalogue's `hub_modality`, then
     `_HUB_ENGINE_MODALITY` in `providers/discovery.py`. Which reader placed the row
     is published as `raw.modality_source` (`package` / `stated` / `engine`), and a
     package the Hub itself refuses to place is reported **in the Hub's own words**
     — the sentence naming `_ENGINE_ROUTE`.
  2. **`lane_contract` is carried verbatim** on the model's cached row, and two
     fields are read: `deadline_seconds` (`adapters/studio_hub.lane_deadline_seconds`,
     replacing the flat 900 s poll ceiling for that model) and
     `states.discard_supported` (`api._lane_discard_state`). The Hub's chat lane says
     `false`, so a hold on it now publishes `close_hold` and `discard-uncertain`
     closes the record locally — before, the Jobs window offered a discard the Hub
     would refuse.
  3. **`model_limits` on chat rows** are published as the model's own numbers, with
     the Hub's provenance sentence in a new `*_source_detail` beside the value
     (`catalog._hub_stated_limit`). A Hub chat row no longer wears the owner's
     curated flat `1,000,000`: measured live, the Hub states **262,144** read from
     `model/config.json text_config.max_position_embeddings`, and **4096** as a
     labelled worker default.
  Not adopted, deliberately: `parameters`, `artifact`, `cancel.semantics`,
  `runtime_seconds` — on the row, consumed by nothing yet, and said out loud in the
  reply so the Hub session knows which half landed.
  `test_e2e.py` **552/552**, `tests/test_hub_lane_contract.py` (new, 10 checks),
  `test_studio_hub_contract.py` 54, every focused suite green.
- **The live 0.6.61 verification `[MEASURED 2026-09-25]`:** this Mac's Hub came up on
  0.6.61 mid-session, so the reads were measured against a real one and not only the
  mock — **23 models discovered, all 23 placed by `package`, 21 carrying a lane
  contract** with the canonical operation tokens and `deadline_seconds: 900.0`. The
  chat model `mlx-community/Qwen3.5-9B-4bit` arrives with `operation:
  chat.completion` and `discard_supported: false`, is **withheld** with the reason
  "the studio_hub adapter does not serve chat…", and carries both `model_limits`
  provenance sentences verbatim. Two new image models (`AITRADER/FLUX2-klein-4B-mlx-4bit`,
  `filipstrand/Z-Image-Turbo-mflux-4bit`) flow through as `image.text_to_image` — the
  owner's new models arriving with no gateway change.
- **The chat lane cannot be dispatched, and that is not a gateway defect.** The
  contract names the studio's `POST /v1/chat/completions`; on 0.6.61 the Hub serves it
  only at `/api/hub/chat/completions`, whose own docstring says it is for "this app's
  own Chat page" and which proxies to **this Mac's** worker. Nothing calls
  `chat_jobs.create_batch` (`transcription_jobs.create_batch` *is* called from
  `standalone.py`), so no chat batch can be created, polled or collected by a
  coordinator. Until the Hub exposes submit/status/artifact for chat, Hub chat models
  are **discovered, listed and withheld** — never dropped, never offered and then
  failed. The four routes we need are named in the reply note.
- **Declared Hub version stays `0.6.60`, deliberately.** 0.6.61 is installed here and
  staged on all four drives, but it is **not pushed to GitHub** and no *controller*
  runs it, so declaring it would be a claim we have not measured against — the exact
  thing this exchange exists to avoid. The local location reads `newer` today, which
  is the report doing its job. Raise the constant when 0.6.61 lands and a controller
  runs it.
- **The 401 on `/studio/voice/api/transcribe/availability` is ours to fix**, per the
  Hub session's trace: that node must be **re-joined with the controller's current
  Fleet token**. An owner action on that Mac. The refusal is now self-describing on
  their side (method, path, credential presented, credential required), which is what
  turned it from a mystery into a one-line fix.
- **Shipped in 0.2.23 (`e42be96`):** a declared Studio Hub version, and discovery that reads
  what the Hub states. Owner's decisions, 2026-09-24: **report drift, keep working**;
  **adopt whatever the gateway can already serve**.
  1. `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION = "0.6.60"` is the one declaration.
     `/health` publishes it as `studio_hub.supported_version`; `fleet._probe` publishes
     `hub_version_relation` (`match` / `older` / `newer` / null) per controller; Fleet →
     Locations shows it in the Hub column; drift is one **info** issue, so it never moves
     `summary.issues`. **No gate anywhere** — routing, the catalogue and the worker ask
     capability questions only, asserted by
     `test_studio_hub_contract.HubVersionDriftTests` and
     `test_fleet_routing.HubVersionReportTests`.
  2. **Discovery reads the Hub's stated `hub_modality` per model**, with
     `_HUB_ENGINE_MODALITY` only as the fallback. Before this, a package on an engine not in
     that table was `continue`d in silence — so a model added on the Hub on a new engine (the
     owner is adding an image model now; LLM and video are being researched) produced no row,
     no count and no reason. A lane this build serves arrives with no gateway release; a lane
     it cannot serve (video, chat) is discovered and **withheld**, listed with the reason; a
     package nothing can place is reported by engine (`DiscoveryResult.skipped` → the model
     cache → `/admin/api/providers` and `/admin/api/providers/{id}/models`, drawn as an
     "unrecognised engine" badge). Live: 35 models per controller, unchanged, **30 placed by
     the Hub's statement** and 5 by the engine fallback `[MEASURED]`.
  `test_e2e.py` **550/550**, console suites **35/35**, every focused suite green.
- **The first true thing the declaration said:** both controllers run Studio Hub **0.6.54**
  while this Mac and all four drives ship **0.6.60**, so both now read `0.6.54 · older`
  `[MEASURED 2026-09-24]`. That is a report, not a fault — the controllers keep taking work.
  Bringing them to 0.6.60 is `Studio Hub KH/1 - Install the app.command` on each controller.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00: TCP
  to every node address times out or refuses from this Mac too, so the report is accurate).
  The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.22 (`3e254c4`):** a fleet/node and Jobs-window audit. Six defects:
  1. `machines_flat` now joins the **node roster on the node id** and carries `available_gb`,
     so a dead node's health word (`no_hub` / `unreachable`) and the controller's own
     "could not reach the node: …" sentence reach the Machines tab, and memory reads
     "17.18 GB (7.14 GB free)" rather than an unlabelled total.
  2. `fleet._issues_for` no longer repeats one fault: the health row folds into the offline
     row, workers on an already-offline machine are one counted line instead of four, and
     `worker_unreachable` on an offline machine is left to that machine. Measured live:
     **148 issues → 19**, and the 7 that survive are a genuine `401` fault, not the offline
     machines. `tests/test_fleet_routing.py::OneFaultOneRowTests`.
  3. `catalog._publish_lock` writes `locked` / `lock_reason` / `availability` together. The
     merge updated only the first two, so a model offered by three locations with one
     answering published `locked: false` beside `availability: "unavailable"`.
     `tests/test_audit_regressions.py::CatalogueMergeTests`.
  4. `summary.models_available` counts a model once per location, like its denominator — the
     tile read **"175/102 local models available"** (rows over models) and now reads 60/102.
  5. The `78fee1d` tabbed-Fleet rewrite lowercased "Could not discover" and moved
     "Retry failed" into `FleetTabs.swift`, leaving `tests/test_task4_native.py` red since
     2026-09-23. **A user-visible sentence this file or a test quotes is a contract: change
     the check in the same commit.**
  6. **The 0.2.19 gap is closed.** `store._recover_interrupted` records the upstream attempt
     (`{upstream_job_id, upstream_item_index, recorded_by: "gateway_restart"}`), a lane with
     no discard route is **closed locally** (`HOLD_CLOSED`, with a recorded sentence saying
     the provider was not told), and `api.recovery_action` is published per job so the Jobs
     window draws `Discard uncertain attempt…` / `Close this hold…` / nothing. Six holds
     from before this release are in the live library — three OpenAI `gpt-6-luna` (created
     2026-09-24 00:35, four minutes before `8bd434d`) and three provider/network holds; all
     six now publish `close_hold` `[MEASURED]`. `tests/test_studio_hub_contract.py::DiscardActionTests`,
     `tests/test_job_recovery.py`, `tests/test_jobs_outputs.swift`.
  Verification for the release: `test_e2e.py` **546/546**, `test_ui.py` **130 passed /
  5 skipped** (the skips are Hub-dependent; **Studio Hub was not running when this was
  written — it is now**, rebuilt 2026-09-26 as `0.6.70`),
  `check_native.py jobs-recovery` passes against the installed app, and `/v1/models`
  publishes **0** `locked`/`availability` disagreements `[MEASURED]`.
- **Newly known, not ours to fix:** the node `vengthengs-mac-studio-hub` answers the
  controller's voice-transcribe availability route with **`401 Unauthorized`** (7 catalogue
  errors on `terranash-1100-m4-16-256`). That is a credential question between that
  controller and that node's voice studio, not a gateway defect — the gateway now reports it
  instead of burying it, and it is the only non-offline fleet fault left.
- **Both controllers are up; all 8 of their nodes are off** (measured 2026-09-24 ~23:00:
  TCP to every node address times out or refuses from this Mac too, so the report is
  accurate). The Hub says `health: unreachable` for site `terranash-1100-…` and `no_hub` for
  `terranash-1200-…`. Node `vengthengs-mac-studio-hub`'s own `detail` is an empty reason
  (`"could not reach the node: "`) while site 1200's says "All connection attempts failed" —
  a Hub-side wording gap, recorded here rather than worked around.
- **Shipped in 0.2.21 (`987721e`):** `fleet._stable_identity` — a location is keyed by the
  controller's real `site_id`, or its per-machine `controller_id` when the Hub still answers the
  shared default `"local-site"`. Before this, adding a controller updated the previous one.
  `tests/test_fleet_identity.py`.
- **Shipped in 0.2.20 (`66e8afe`):** `catalog._DEFAULT_TEMPERATURE_ONLY` withholds the
  `temperature` control from default-only reasoning families **on official OpenAI only** — so
  Story Studio stops sending the `0.7` it sends whenever the control is offered. Scoped that way
  because a proxy (Experiential Labs' `gpt-5.6-luna`) accepts `0.7` and must keep it; a provider
  that publishes `supported_parameters` is always believed. `tests/test_chat_controls.py`.
- **Shipped in 0.2.19 (`8bd434d`):** a definite provider `4xx` settles as a failure instead of a
  durable unknown-outcome hold, and a model that refuses `temperature` is retried once without it
  (`params_dropped`). Covered by `tests/test_job_recovery.py` and `tests/test_chat_temperature.py`.
  **The gap it exposed — a cloud hold with no `recovery_context` could never be cleared — is
  closed in 0.2.22; see above.**
- **Shipped in 0.2.18 (`9b7876a`):** the gateway-side **downgrade guard** (refuse/warn when a
  controller already holds a newer version) and the per-controller **`trusted_publisher`** display
  in Fleet → Locations → Release. The guard is proven against the contract-enforcing mock only.
- **Unproven on a real controller:** the release push is proven only against the **local** Hub
  on 0.6.50. The two configured remote controllers were **off** (`LOCATION_UNREACHABLE`,
  ConnectTimeout), so a real remote push and the no-trusted-key (409) path are **not** verified.
  Installing the publisher key on a controller (`POST /api/hub/release/trust`) is the **owner's**
  action.
- **`release-state.json` is misleading if read quickly:** its last publish records a **15-byte**
  artifact `StudioHub-0.6.50-test0001.dmg`, not the 1.28 GB real DMG, and both controllers
  `LOCATION_UNREACHABLE`. `[reason not recorded]` for the tiny test descriptor; do not read it as
  "the real release is on those controllers."
- **Held at the owner's request:** a per-node image + TTS generation sweep; it waits for the
  fleet to be up.
- **No independent review** of the release-publisher work. The gate itself was fixed in this
  release: it had been reading the pre-rework single-scroll Fleet page (and a pinned `/18`
  provider count), so it failed against a correct app; §7 now walks the five tabs and the AX
  helpers retry a transient window read. If the Fleet tabs change again, that section must change
  with them.
- **Docs reconciled 2026-09-23.** `README.md`, `AGENTS.md` and `Project Status.md`
  (§1/§2/§9/§11) now agree with the code: port 8787, `main` as the only worktree,
  current release. The dated records — `_handoffs/`, `docs/superpowers/plans/`,
  and the dated sections of `Project Status.md` — deliberately keep the numbers
  they were written with; history is not rewritten. `docs/fleet-deployment-notes.md`
  remains the pre-deployment plan, marked `[confirm]` where Hub behaviour was then unknown.
- An unused remote branch `origin/codex/local-release-dmg` exists; left alone
  deliberately (not this session's ref to delete). `[reason not recorded]` for why
  it still exists.

## SECRETS

**There are no secrets in this file, and none belong in it.** Everything sensitive lives under
the live data dir `~/Library/Application Support/gateway-kh/` (gitignored): `.env`, and in
`data/` the `credentials.json`, `fleet.json` (controller tokens), `models_cache.json` and the
`release-publisher.pem` private key (mode 0600, never returned by an API). Provider keys and
Studio Hub tokens are the **owner's** and are installed by hand — ask him; do not search the
tree, and never print an address, token or private key into a note or a test.

## WHY THIS FILE IS TRUSTWORTHY

It was written by reading, not remembering: `AGENTS.md`, `Project Status.md`, `README.md`,
`gateway/config.py`, `gateway/release_publisher.py`, `menubar/build.sh`, `docs/*.md`,
`tests/check_native.py`, the last 40 commits, and the live `VERSION` / build stamp / data dir.
Where the project did not record a reason, this file says **`[reason not recorded]`** instead of
inventing one.
