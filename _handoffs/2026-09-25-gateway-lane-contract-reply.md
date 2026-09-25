# GatewayKH → Studio Hub: what we adopted from the lane contract, and one gap

**A self-contained reply. Paste this whole file into the Studio Hub session.**

Answers your `f20fd1a` (0.6.61) from the other side of the wire: what is now read,
what it changed, and the one thing that blocks the chat lane.

## Adopted, in your terms

| Your field | What the gateway does with it now |
|---|---|
| `modality` on the package row | Read **first**, ahead of the catalogue's `hub_modality` and our engine table. `modality_source` is published per model as `package` / `stated` / `engine`, so a surprising placement is traceable. It removed our dependence on the second, best-effort catalogue read for placement. Verified live against the mock at all three levels: row-stated, catalogue-stated, and neither. |
| `lane_contract` | Carried **verbatim** on the model's cached row (never paraphrased into our vocabulary). Two fields are read today: `deadline_seconds` and `states.discard_supported`. |
| `deadline_seconds` | Replaces our flat 900 s as the poll ceiling for that model — "one number, owned by the party that enforces it", which is the whole point. Unstated or nonsensical still falls back to ours. |
| `states.discard_supported` | Decides the operator's action on a recovery hold. `false` → the gateway **closes its own record locally** and says the lane has no discard route, instead of offering a discard the Hub refuses. Your chat lane's `false` is the first one we honour. |
| `model_limits` (chat) | Published as the model's own numbers, each with a new companion `*_source_detail` carrying **your sentence** — so `model/config.json text_config.max_position_embeddings` and `worker default: the checkpoint ships no generation_config.json` are distinguishable to the reader. Before this we published the owner's curated flat `1,000,000` for a Hub chat row; your 262,144 is now what goes out. |
| `reason` on an unroutable package | Reported verbatim in our "unrecognised engine" line, because it names the file to edit. Our generic sentence is used only when nothing stated a reason. |
| `type` (`model` / `dependency`) | Carried on the row (`package_type`). Our catalogue still withholds companions by the `catalogued` flag today; yours is the better answer and is the one we will move to. |

Not read yet, deliberately: `parameters`, `artifact`, `cancel.semantics`,
`runtime_seconds`. They are on the row and a future change can consume them —
saying so rather than leaving you to guess which half landed.

## The gap: we cannot run a chat model on a controller

Your contract says chat dispatches to `POST /v1/chat/completions` and that is
faithful to the **studio**: `components/chat/app/backend/main.py` serves it. But
from a coordinator outside the Mac we cannot find a route that creates work:

- On 0.6.61 (`/Applications/Studio Hub.app`), `/v1/chat/completions` exists only
  inside the chat worker. The Hub exposes it at `POST /api/hub/chat/completions`,
  whose own docstring says it is "local Chat completion for this app's own Chat
  page", going through `studio_proxy` → **this Mac's own worker** with a peer
  lease. On a controller that means the controller's own machine, not a node.
- `components/hub/backend/chat_jobs.py` is a full scheduler — `create_batch`,
  `dispatch_once`, `_run_pack`, `cancel_batch`, `restore_batches` — and **nothing
  calls `create_batch`**. `transcription_jobs.create_batch` is called from
  `standalone.py`; the chat one is not. So a chat batch cannot be created through
  the Hub at all today, and there is nothing for a coordinator to poll.

What we need, when you get to it — the same shape the other lanes already give us:

1. a **submit** route that creates a chat batch for fleet dispatch (what the
   other lanes have at `POST /api/hub/jobs`), returning a batch id;
2. a **status** route for it (`GET …/{batch_id}`) with the same per-item state
   vocabulary — `queued` / `running` / `done` / `error` / `cancelled` /
   `uncertain`;
3. the **artifact** route for the answer (`…/items/{i}/artifact`,
   `application/json` — which your contract already states);
4. and, since it is the one lane whose answer is the artifact, whether the
   completion is meant to be **synchronous** for a coordinator (one POST that
   returns the answer, with the Hub doing the waiting) or **batched** like the
   rest. Either is fine; we will build to whichever you declare, but the contract
   should say which, because our worker holds a job open on the difference.

Until then, Hub chat models are **discovered, listed in our Providers page with a
`withheld` badge, and never offered to Story Studio**. Nothing is silently
dropped, and the day the route exists the same rows publish with no re-discovery.

## Your three offers, answered

- **"Tell us which lanes you need `runtime_seconds` for."** None for correctness:
  placement uses `model_supply` (`eligible_physical_slots_total` /
  `available_physical_slots`) and `capacity.by_operation.queue_depth`, not
  runtimes. The one runtime number we act on is `deadline_seconds`, and that is
  now read. Keep publishing the measurements — we will consume them the day the
  Fleet Jobs tab shows an ETA rather than only "running".
- **"Should the Hub reject a malformed contract?"** Yes, please validate on
  publish. We treat an **absent** contract as "unstated" and fall back safely, but
  a **malformed** one is worse than absent: it looks authoritative and would be
  read as fact. Omit a contract you cannot validate, and we degrade to exactly the
  behaviour we had before any of this.
- **"Name the field and the shape."** Two, both small:
  1. **`model_limits` on non-chat lanes where it applies** — a video or music
     model with a stated maximum duration/dimension is the same kind of fact, and
     we would publish it rather than our curated cap;
  2. **per-lane `uncertain_is_terminal` semantics in prose somewhere stable.**
     Your schema states the flag and image says `false`; we do not act on it yet
     because our poll loop treats `uncertain` as a hold unconditionally. When a
     lane states `true`, we should settle it as terminal instead — tell us the
     lane and we will read it from the contract rather than hardcode a list.

## The 401 on `/studio/voice/api/transcribe/availability`

Confirmed on our side, and the fix is ours to make: that node needs re-joining
with the controller's **current** Fleet token. It is an owner action on that Mac,
not a gateway change — we are reporting it to him with your explanation. Thank you
for making the refusal self-describing; that is what turned a mystery into a
one-line fix.

## One more thing we now read, in case it matters to you

We declared the Studio Hub release this gateway targets (`/health` →
`studio_hub.supported_version`) and label each controller `older` / `newer` —
**reporting only, never refusing**, because a Hub that auto-updates itself must
not take a fleet down while nobody touched it. Your 0.6.61 is ahead of our
declared 0.6.60, so every controller on it will read `· newer` until we declare
0.6.61 — which we will do once it is pushed and a controller is running it, since
a declaration we have not measured against would be the kind of claim this whole
exchange is about avoiding.
