# B1 + B3 + C1–C5 receipt — the fleet the Gateway can see, and what it will run

Gateway **0.2.63**, build `677ff0d-202609280520`, installed and running on 8787. Read live on 2026-09-28. `[MEASURED]`

## C3 / decision D — the number Story is offered is the fleet's, live

`GET /v1/account/overview`:

```json
{
 "paid_concurrency_limit": 24,
 "concurrency_source": "the live fleet, summed over reachable locations",
 "concurrency_by_modality": {
  "chat": 4,
  "image": 4,
  "video": 4,
  "tts": 4,
  "transcription": 4,
  "music": 4
 },
 "concurrency_pool": 60,
 "max_queued_jobs": 256,
 "generation_submissions_per_minute": 60
}
```

**Before:** `paid_concurrency_limit` was the settings pool — live **60** — while the fleet could do about **4**. **After:** it is the sum, over reachable locations, of the budget admission itself enforces, and the pool is published beside it so the two cannot be confused. Nothing was edited on disk to produce this number.

## B1 — members per controller

`GET /admin/api/fleet`, the `nodes` + `node_summary` the gateway fetched and never decoded:

| location | reachable | node_summary | node rows carry |
|---|---|---|---|
| This Mac | True | total 0 · reachable 0 · unauthorized 0 | — |
| studio-1100-m4-16-256 | True | total 5 · reachable 0 · unauthorized 0 | address, detail, id, joined_at, name, role, state, studios |
| studio-1200-m4-24-256 | True | total 6 · reachable 0 · unauthorized 0 | address, detail, host, id, joined_at, name, role, state, studios, version |
| studio-1300-m4-24-256 | True | total 1 · reachable 0 · unauthorized 0 | address, detail, host, id, joined_at, name, role, state, studios, version |
| studio-1400-m4-24-256 | False | total None · reachable None · unauthorized None | — |
| studio-1500-m4-24-256 | False | total None · reachable None · unauthorized None | — |

Every decoded field is optional on purpose: the rows above come from controllers on older Hubs and simply do not carry `last_seen_at`, so the app draws an em dash rather than a zero.

## C1 — the dispatcher says whether it is alive

`worker.liveness()` is published on `/health` as `worker_alive`, `last_pass_at` and `consecutive_failures`, so a stalled loop shows a stale timestamp instead of a green light (M6). The deferral budget is now 240 deferrals capped at five minutes — about twenty hours — because a two-hour video job must not be failed by its own queue (M4); `retry.max_attempts` is what declares a job failed after three real machines (A7).

## B3 — the queue

`GET /admin/api/queue`:

```json
{
 "waiting": 0,
 "truncated": false,
 "pool": {
  "limit": 60,
  "source": "curated",
  "detail": "provider-concurrency.local.json sets global to 60",
  "in_flight": 0
 },
 "queue_wait": {
  "max_deferrals": 240,
  "defer_cap_seconds": 300.0,
  "source": "gateway",
  "path": "~/Library/Application Support/gateway-kh/data/provider-concurrency.local.json"
 }
}
```

`queue_wait` is the live setting that bounds a wait; `pool` is the local pool; and each waiting job carries `queue_position`, `queue_size`, `defer_reason` and `attempts[]` with `attempt n of 3`. `worker.budgets` — written "so the console can say what is bounding a lane" and read by nothing until this release — is the `budgets` map.

