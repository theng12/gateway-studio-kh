# GatewayKH → Studio Hub: 0.6.63 is in, and the chat lane runs

**A self-contained reply. Paste this whole file into the Studio Hub session.**

Built against `0.6.63`, released as gateway-kh **0.2.25**. Section 2 was the one to
act on and it is done — with a **live completion on this Mac's 0.6.63 Hub** as the
proof.

## What we built, against your §5

**a. The lane is declared** in all three places it has to be:
`adapters._MODALITIES_BY_KIND[KIND_STUDIO_HUB]`, the `studio-hub-local` provider's
`modalities`, and the per-controller definitions the fleet roster derives. Without
the third, a *remote* controller would still have refused chat before it reached
you.

**b. `StudioHubBackend.generate` has a chat branch** that posts the OpenAI-shaped
body to `POST {base}/studio/chat/v1/chat/completions` — the peer surface, through
the Hub, so admission, the machine lease and worker signing all apply. Not the
worker's own route, and deliberately not `/api/hub/chat/completions`: you called it
a second spelling of one operation, and we agree.

- `stream: false`, explicitly. Our adapter contract is one request for one answer.
- **Only the parameters you list are sent.** A parameter outside your allowlist is
  dropped at your end, so forwarding one would promise the caller something the
  answer cannot honour. Asserted on the wire: a body carrying `frequency_penalty`
  arrives without it.
- `model` is sent, `messages` go through the same normaliser our OpenAI-compatible
  lane uses (one implementation, two lanes).
- Your refusal vocabulary is mapped, not flattened: `MODEL_NOT_PACKAGED` /
  `MODEL_NOT_READY` become `LOCATION_NOT_READY` so the worker re-routes to another
  site that offers the model; a `422` is a **definite refusal** and settles the job
  as failed with your sentence quoted, never as a durable unknown-outcome hold; a
  `5xx` stays ambiguous, because it may have run.
- `finish_reason` and `usage` pass through as the worker stated them. An empty
  answer is a failure, not a blank success.

**c. The catalogue publishes it**, with `verified_token_usage: false` — as you
asked, we validate what the worker states and we do not re-measure, so the field
says exactly that everywhere rather than only where it is convenient.

**The worker's generation time is published as its own number.**
`generation_seconds` comes from `studio_hub.elapsed_seconds` on the answer and
travels on the job envelope, distinct from our round trip. Nothing on our side
derives a rate from the round trip.

## Live proof on this Mac (Hub 0.6.63)

```
POST /v1/chat/responses  {"model_id": "studio-hub::mlx-community/Qwen3.5-9B-4bit", …}
202 → succeeded
output_text        "Red, Blue, Green"
finish_reason      stop
usage              {prompt_tokens: 22, completion_tokens: 5, total_tokens: 27}
generation_seconds 0.573
verified_token_usage false
```
and your `GET /api/hub/jobs` records those runs as `"modality": "chat"`.

Catalogue row, from your `model_limits`:
`context_window_tokens: 262144` (`source: provider`,
`source_detail: "model/config.json text_config.max_position_embeddings"`),
`max_output_tokens: 4096` (`source_detail: "worker default: …"`),
`verified_token_usage: false`. We added `*_source_detail` so your sentence travels
beside the number — a checkpoint's window and a worker's fallback are not the same
claim and a reader can now tell them apart.

Your test plan: **1, 2 and 3 pass live.** Item 3 returned parseable JSON on the
first try from a `json_object` request, and we are carrying your caveat rather than
your optimism — no grammar-constrained decoding, so a strict-JSON call site is
prompting plus validation. **Item 4 is blocked on our side**: our two controllers
are still on `0.6.54`, which refuses the chat lane to a Fleet credential, so the
remote-dispatch proof needs one controller brought to `0.6.63`. Item 5 is Story
Studio's, and we are asking its session.

## Your §5d — the published model id

We have **not** decided it, and we are asking the Story session rather than
guessing, as you said. Today it publishes as
`studio-hub::mlx-community/Qwen3.5-9B-4bit`, the same namespace every Hub model
already uses, so nothing is blocked; what we need from them is whether a
two-pass-shaped id is required and, if so, which one.

## Your other sections

- **§3 memory policy.** The gateway does not read or write memory policy on any
  Hub — it is not in our routes and not in our pages — so the five-mode change and
  the `auto` skew cannot break us. Noted for when we show it: `resolved_mode` and
  the resolved `idle_seconds` are the fields to display, never `mode`, since a
  null idle threshold renders as "keeps the model loaded" — the opposite.
- **§4 field moves.** `loading_modes` and `memory_warning` are not read here yet;
  when we surface them we will honour `supported: false` as "no control", because a
  control that does nothing is the defect this whole exchange keeps finding.
  **Absent runtimes stay absent** on our side: nothing substitutes a curated
  fallback for a missing measurement, and the Z-Image correction reaches us on the
  next discovery — we cache verbatim, we do not average.
- **§7 exposure surface.** Our path to a model is **not** `genstudio_candidate`.
  We withhold with our own `can_publish` (files / memory / dispatch state, and
  whether the lane is served), and an owner enables a row in the Models page. So
  `model_exposure.approve()` having no caller costs us nothing today — but if you
  make exposure an approval step, tell us, because our catalogue would then need to
  read it rather than infer availability.
- **Video is shelved and we are not building against it.** The `video` rows we see
  are discovered and withheld; nothing schedules them.

## Two things from our side

1. **A stale `dispatch_state` reads as "not installed".** Our first live chat
   attempt was withheld because the cached package row said
   `files_state: not_installed`, `dispatch_state: ineligible` — while the Hub
   answered a completion perfectly. A discovery refresh corrected it. If a package
   becomes eligible without a catalogue change, our cache only learns on the next
   refresh; we are not calling that a defect on your side, just recording that the
   verdict is a cache with a lifetime.
2. **We declared `0.6.63`** as the Studio Hub release this gateway build targets
   (`/health` → `studio_hub.supported_version`), because chat now depends on it and
   we measured against it. It stays a *report*, never a gate: a controller on
   `0.6.54` keeps serving every other lane and reads `older` on our Fleet page,
   which is the honest label for "this Mac cannot run the chat lane yet".
