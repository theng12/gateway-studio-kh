# Studio Hub → GatewayKH: the capacity, worker and model-health contract

**A self-contained request. Paste this whole file into the Studio Hub session.**

## Who is asking, and what it costs us

GatewayKH is the single coordinator URL in front of Story Studio KH and every
Studio Hub location. Per location it reads a fixed set of Hub routes and turns
them into one Fleet page and one routing decision. Everything below was measured
against Hub **0.6.47** on 2026-09-23 — one local agent and two controllers — and
four of the fields we depend on are empty or missing. The result today:

- **Routing cannot weight by "machines that can run this model".** Story Studio's
  work is placed by the *site-wide* machine count, so a site with ten 8 GB Macs
  and one 24 GB Mac can be chosen for a 24 GB model. The gateway has a per-model
  weighting written and tested, but nothing feeds it.
- **The Fleet page cannot say why a model will not serve.** It assembles a reason
  from four fields on four routes, and about a fifth of model rows state nothing
  at all, so the honest answer is often "unknown".
- **A failed batch has no reason on the list route.** The batch row carries a
  *count*; the text is one call per batch away.

None of this is blocked on GatewayKH. All of it is a small, additive change on the
Hub side.

## What we already read, and works

- `/api/hub/capabilities` — controller identity/role/ready/drained, `capacity`,
  `model_supply`, `machines[]`, `workers[]`
- `/api/hub/fleet/overview` — per-machine reachability, chip, memory, disk,
  health, `detail`, `studios{total,up}`, jobs, `average_seconds`, `modalities`
- `/api/hub/fleet/nodes` — the joined-node roster and each node's studios
- `/api/hub/models` — **one row per machine per model**, with `hub_machine`,
  `hub_studio`, `hub_modality`, `min_unified_memory_gb`,
  `required_free_memory_gb`, `memory_eligible`, `runtime_ready`, `available`,
  `loaded`, `fit{state,label,hint,actual_gb,required_gb}`, `cache`, `kind`,
  `hub_catalog_error`, `hub_catalog_stale`, `active_download`, `gated`
- `/api/hub/model-packages` — `package_id`, `state`, `files_state`,
  `runtime_state`, `os_state`, `chip_state`, `dispatch_state`, `engine`,
  `minimum_macos`, `verification`
- `/api/hub/jobs` (+ the transcription equivalent), `/api/hub/stats`,
  `/api/hub/workers`

That is already a generous surface and it is why the Fleet page works. The four
items below are what remains.

## Gap 1 — per-model and per-operation dispatchable capacity

**Observed, on all three locations (verbatim):**

```json
"model_supply": [],
"capacity": {
  "queue_depth": 0,
  "available_physical_machine_slots": 0,
  "eligible_physical_machine_slots_total": 0,
  "eligible_worker_service_slots_total": 0,
  "eligible_worker_services": 0,
  "shared_physical_machine_slots": true,
  "by_operation": {}
}
```

**What we read, exactly** (`gateway/fleet.py::_model_supply_index` and
`routing_candidates`):

- `model_supply[]` → `internal_model_id`,
  `eligible_physical_slots_total`, `available_physical_slots`
- `capacity.by_operation[<operation>]` → `eligible_physical_machine_slots_total`,
  `available_physical_machine_slots`

Operation names are the Hub's own, which we already map:
`image.text_to_image`, `music.generation`, `voice.tts`, `audio.transcription`,
`video.generation`.

**What we need — pick either:**

1. **Publish it.** Populate `model_supply` for every local model, once per model,
   with the number of **real machines** (not services, not slots) whose memory
   floor the model clears, and how many of those are ready with a free slot now.
   Populate `capacity.by_operation` the same way per operation. On a controller
   that has joined 4 machines, an eligible count must be ≤ 4, and it must be 0 for
   a model no machine can hold.
2. **Or say it is retired** and name the replacement — then tell us the stable id
   that joins a `/api/hub/models` row to the id the router is given, and we will
   derive the counts from `/api/hub/models` ourselves (see Gap 2).

`ponytail:` today we fall back to `capacity.*` then to the site-wide machine
count, so the page shows *a* number and routes *somewhere*; it is just not the
number the decision needs.

## Gap 2 — a model id we can join on

`/api/hub/models` rows are keyed by `repo` (e.g. `SWivid/F5-TTS`,
`stabilityai/stable-audio-3-optimized`). The ids the gateway dispatches and Story
Studio picks are different strings, and the Hub's own batch rows carry a third
form. There is no field we can join on.

**What we need:** on each `/api/hub/models` row, the **canonical model id** used
by `/api/hub/jobs` (`model`) and by `model_supply.internal_model_id`, under a
documented key — e.g. `model_id`. On each batch row, the same id. One id, one
spelling, everywhere work is named.

## Gap 3 — per-worker model inventory

**Observed:** `workers[].models` is `[]` and `workers[].supported_operations` is
`[]` on **all 24 workers** across three locations. The fields exist and are always
empty.

**What we need:** populate both.

- `models` — the model ids that worker can serve on its machine.
- `supported_operations` — its operation names, in the same vocabulary as
  `capacity.by_operation` (Gap 1).

This is what turns "3 machines report this model" into "the Voice worker on
`terranash-1101` has it loaded and is free". `workers[].available_capacity`
already gives the slots; this is the missing half.

## Gap 4 — a model health answer with a reason

**Observed:** `/api/hub/model-health` returns `{"paused": []}` on the local agent
and `{"detail": …}` (~145 bytes) on a controller. There is no per-model health.

**What we need:** one row per (machine, model):

```json
{"model_id": "…", "machine": "…", "state": "ready",
 "reason": "…", "hint": "…", "observed_at": 1790132706.4}
```

with a documented, closed `state` vocabulary. We will map it verbatim; suggested
states, matching what the Hub already knows:

`ready` · `download_required` · `runtime_missing` · `unsupported_os` ·
`unsupported_chip` · `insufficient_memory` · `quarantined` · `unknown`

**Absence must stay `null`/absent, never a plausible default** — "nothing is
wrong" and "nobody checked" must not render the same. We currently publish the
`null` rows as unknown rather than inventing a reason, and we would rather have
the real one.

## Gap 5 — retry, and the per-item reason

- **Retry.** `/openapi.json` has `discard`, `reconcile` and `ack` per item, and
  `DELETE` per batch, but **no retry**. We need either
  `POST /api/hub/jobs/{batch_id}/items/{item_index}/retry` (and the transcription
  equivalent), or a written answer that re-submitting is the only path. If a
  retry exists, say whether an `uncertain` item must be discarded first.
- **Per-item reason on the list.** `/api/hub/jobs` batch rows carry `error` as a
  **count**. We would like the text alongside it — e.g.
  `"errors": [{"item_index": 0, "code": "…", "message": "…"}]` — or a written
  answer that `/api/hub/jobs/{batch_id}` is the only place and its exact shape, so
  we make one call per problem batch instead of guessing.

## Gap 6 — confirm the cancel contract

We now expose a Fleet Cancel that reaches the Hub's `DELETE` (see
`gateway/admin.py::fleet_cancel_job`). Please confirm, so the button tells the
truth:

- the response fields (`queued_cancelled`, `running_signalled`, `running_pending`)
  and that they are what the Hub actually did;
- whether `DELETE` returns before workers stop, and that
  `cancel_requested` / `cancelled` / `cancelled_items` are the durable state we
  should read back;
- whether a cancelled batch or item can be retried (ties to Gap 5).

## Conventions we rely on (already followed at the Hub, please keep)

- **Absence is `null`/absent, never a plausible `0`.** We publish `null` for a
  value nobody stated; a stated `0` is a claim and we preserve it.
- **Stable ids.** A machine id, a model id and a batch id are identities; labels
  are the owner's to change.
- **Epoch seconds** for timestamps, and the same key names on every route.

## How to verify it landed

```bash
# 1. Non-empty per-model capacity, bounded by the real machine count.
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/capabilities \
  | jq '.model_supply[0], (.model_supply | length), (.capacity.by_operation | keys)'

# 2. A joinable id on both sides.
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/models \
  | jq '.models[0].model_id'
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/jobs \
  | jq '.batches[0].model, .batches[0].errors'

# 3. Workers name what they serve.
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/capabilities \
  | jq '[.workers[] | select((.models | length) > 0)] | length'

# 4. Model health answers per machine.
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/model-health \
  | jq '.models[0]'
```

**Acceptance:** on the 4-machine controller, `model_supply` has one row per local
model with eligible counts between 0 and 4; every `/api/hub/models` row carries
the same id a batch carries; a worker names at least the models its studio
serves; `/api/hub/model-health` has one row per (machine, model) with a state.

## Non-goals

- No new auth scope. The Fleet token we already hold must reach every route above.
- No per-machine `model_supply` if the per-model counts are published.
- No change to the existing routes' shapes — additive keys only. We decode
  defensively and ignore what we do not know.

**When you ship it, tell us the Hub version and which keys changed** so we verify
against the running Hub rather than a checkout, and we will wire the router to the
new numbers the same day.

---

# Addendum (after 0.6.48): the activity reporter, in words

**Forward just this section.** Hub 0.6.48 (build 2c8dabe) answers all six gaps
above, verified against the running Hub on 2026-09-23. Two things remain, and they
are now the only entries left in the Fleet Issues list the owner cannot act on.

## A. Why `fleet_activity.limitation` fires

Every machine at a site carries the same sentence, and the Hub gives no reason:

```
This Mac (agent)         state=long_idle  completed=11  median_runtime_s=null
                         limitation="Direct activity partially unavailable"
terranash-1100 (controller, 4 machines)  state=unknown  completed=0
                         median_runtime_s=null
                         2 machines "Activity reporter temporarily unavailable"
                         2 machines "Direct activity partially unavailable"
                         pulse: 4 unknown
```

The gateway surfaces the sentence verbatim — it is the Hub's own wording and we
will not paraphrase it — but that is all it can do with it, so the owner reads
"Direct activity partially unavailable" and has nothing to act on. It is also the
reason every machine at a site reports `state: unknown`.

**What we ask** — a machine-readable reason beside the sentence, on each
`fleet_activity.machines[]` row:

- `limitation_code` — a closed vocabulary, e.g. `reporter_unavailable`,
  `partial_read`, `stale_snapshot`, `waiting_for_worker`. We will map it verbatim.
- `limitation_reason` — one short phrase for the page; null when there is nothing
  more to say.
- `limitation_since` — epoch seconds, so the page can say "for 12m" and distinguish
  a blip from a standing condition.
- `limitation_affects` — which fields are missing because of it, e.g.
  `["state", "completed", "median_runtime"]`. Today the page cannot tell whether a
  blank median is a limitation or simply not measured yet.
- If it is transient, whatever the Hub knows about recovery (`next_attempt_at`,
  `eta_seconds`) — the gateway renders "retry in Ns" for jobs already.

Absence stays `null`, as everywhere: no code is "the Hub did not say", which is not
the same as "nothing is wrong".

## B. Per-machine activity completeness

The same gap makes the Machines tab show `0 done` and `—` for Median on the
controller's four machines, because `fleet_activity.machines[].completed` is `0` and
`median_runtime_s` is `null` while `state` is `unknown`. On this Mac the state and
the count are real (`long_idle`, `completed: 11`); only the median is missing, and
the column falls back to `fleet/overview`'s `average_seconds`, labelled `avg` so the
two are never confused.

**What we ask:** populate the per-machine direct activity (state, completed, failed,
median) for joined machines, or tell us what the reporter is waiting for — a worker
route, a heartbeat, a periodic flush — so the gateway can say that instead of
showing a blank. If the honest answer is "a joined machine never reports direct
activity", say so and we will label the column that way and stop treating it as a
warning.

## How to verify

```bash
curl -s -H "X-Hub-Token: $TOKEN" http://<controller>:47873/api/hub/stats \
  | jq '.fleet_activity.machines[] | {machine, state, completed, median_runtime_s,
        limitation, limitation_code, limitation_reason, limitation_since,
        limitation_affects}'
```

**Acceptance:** a machine whose activity is limited carries a `limitation_code`
from the closed set; the affected fields are named; a machine that reports fully
has `limitation: null`.

