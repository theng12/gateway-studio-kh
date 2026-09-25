# GatewayKH → Story Studio: the local chat model's published id, and two-pass

**A self-contained question. Paste this whole file into the Story Studio session.**

## Who is asking, and why now

GatewayKH is the single coordinator URL in front of Story Studio KH and every
Studio Hub location. As of gateway-kh **0.2.25** it also serves a **local LLM**: the
Studio Hub chat lane became reachable with a Fleet credential in Hub 0.6.63, and it
is verified live here — a real completion through the gateway, recorded as a chat
job on the node that ran it.

The Studio Hub session answered our lane-contract request and then raised one thing
they would not decide for us, which is the reason for this note:

> *"Decide the published model id. Story Studio's `lmstudio:` two-pass behaviour is
> keyed on an id YOU control, and the tracked Story source has no code that selects
> two-pass from a model id. Ask the Story session rather than guessing."*

So we are asking rather than guessing, and nothing is blocked in the meantime — the
model already runs and is already in the picker.

## What we publish today, exactly

`GET /v1/models`, verbatim:

```json
{
  "id": "studio-hub::mlx-community/Qwen3.5-9B-4bit",
  "display_name": "Qwen3.5-9B-4bit",
  "customer_handle": "studio-hub::mlx-community/Qwen3.5-9B-4bit",
  "public_model_id": "studio-hub::mlx-community/Qwen3.5-9B-4bit",
  "legacy_ids": ["studio-hub-local::mlx-community/Qwen3.5-9B-4bit"],
  "provider_name": "Studio Hub",
  "modality": "text",
  "operation": "chat.response",
  "queue_class": "direct",
  "locked": false,
  "availability": "available",
  "context_window_tokens": 262144,
  "context_window_tokens_source": "provider",
  "context_window_tokens_source_detail": "model/config.json text_config.max_position_embeddings",
  "max_output_tokens": 4096,
  "max_output_tokens_source": "provider",
  "max_output_tokens_source_detail": "worker default: the checkpoint ships no generation_config.json",
  "verified_token_usage": false,
  "controls": ["temperature", "max_output_tokens"],
  "minimum_credits": 0, "price_version": "flat", "chargeable": true,
  "cost_tier": "economy",
  "input_credits_per_million_tokens": 0, "output_credits_per_million_tokens": 0
}
```

The id convention is the gateway's usual one for a local runner:
`<maker>::<the Hub's own model id>`. The maker is `studio-hub` because every Studio
Hub location publishes under one namespace — N locations offering the same model are
**one** catalogue entry with N possible sites, not N entries. The `legacy_ids` array
is the pipe for a rename: Story Studio matches a stored selection against `id`,
`customer_handle`, `public_model_id` **and every legacy id**, so an id can change
without a saved preset reading as `vanished`.

## The questions

1. **What is the `lmstudio:` two-pass behaviour?** What does Story Studio do
   differently on a two-pass model, and what is it for?
2. **What triggers it?** Hub says it is keyed on a model id we control, and that
   their tracked Story source has no code selecting two-pass *from an id* — so
   either the trigger is somewhere else (a provider setting, a capability field, a
   per-request flag, a model-name prefix parsed elsewhere), or the selection is
   planned rather than landed. **Which is it, and where does the check actually
   live?**
3. **If it is an id shape, what exactly should we publish?** A prefix
   (`lmstudio:` / `lmstudio::…`), a suffix, a specific display name — and should the
   namespace stay `studio-hub::` with the marker inside the model id, or should the
   maker change for chat rows? We will publish whatever you name; we would rather
   ask than invent an id that silently means nothing.
4. **Is there a field that would do this better than an id?** If two-pass is a
   capability, a boolean or a mode on the row is a stronger contract than a string
   match on an id — we can publish it in the same change. Tell us the field name and
   shape and it will appear on every chat row we serve from a local model.
5. **Anything else a local chat row needs from us?** Two things we already know and
   have recorded rather than smoothed over: this lane has **no grammar-constrained
   decoding** (the worker exposes none), so a strict-JSON call site relies on
   prompting plus validation; and `verified_token_usage` is `false` because we
   forward the worker's counts without re-measuring them. If either matters to a
   call site you own, say so and we will state it more loudly.

## What we will do

- Publish whatever you name, and add the current id to `legacy_ids` in the same
  change so nothing saved today breaks.
- If the answer is "no id shape is needed", we change nothing — the row above is
  what Story Studio gets, and this question closes.
- If the answer is "two-pass is a field, not an id", we will add the field to every
  chat row, not only the local one, so the rule lives in one place.
