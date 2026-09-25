# Relay to Story Studio — four rows that have never been sent

**Date:** 2026-09-25 · **From:** GatewayKH · **To:** Story Studio
**Status:** all four are Story Studio's. None needs a gateway release to answer.

**Why this file.** Story Studio has never been pasted its rows. They live in
`_handoffs/2026-09-25-MASTER-open-items-tracker.md` under *"A. Story Studio owes /
we ask"*, and that repo is not readable from the Story Studio session — the same
miss that kept HUB-12 invisible to Studio Hub for three rounds. **Paste the section
below in full.** It points at no file you cannot open, and every field quoted in it
was read live from the running gateway on 2026-09-25.

**Who owns what.** STORY-1b/2/4 are Story Studio's. STORY-5 is shared. And
**GW-2 is ours and blocked on STORY-1b** — the gateway will implement whatever
Story Studio names there, keeping today's model id as a `legacy_id`, so that one
answer unblocks a GatewayKH row as well.

---

## PASTE FROM HERE

### Context, in one paragraph

GatewayKH is the single endpoint you already point at (`http://127.0.0.1:8787`).
It fronts every local Studio Hub location and 18 cloud providers so your model
pickers fill from one URL. Four rows in the cross-project tracker are yours, and
none of them has reached you before — that is our fault for not pasting them.

### 1. STORY-1b — do you want two-pass for the local model, and is this row enough to decide it at runtime?

This is the one that blocks work on our side. Read live today, this is the chat row
you get for the local Hub model:

```
id                                  studio-hub::mlx-community/Qwen3.5-9B-4bit
context_window_tokens               262144
  source                            "provider"
  source_detail                     "model/config.json text_config.max_position_embeddings"
max_output_tokens                   4096
  source                            "provider"
  source_detail                     "worker default: the checkpoint ships no generation_config.json"
verified_token_usage                false
supports_structured_output          true
structured_output_mode              null
lane_contract.structured_output     formats: ["json_object", "json_schema"]
                                    enforcement: "prompt_validate_repair"
                                    grammar_constrained: false
                                    repair_attempts: 1
```

**The ask:** do you want two-pass, and **is this row enough to decide it at
runtime?** If two-pass needs another fact, name it and we publish it from the
producer's statement rather than inventing a value. **Our GW-2 is blocked on this
answer** — we will implement whatever you name, keeping today's id as a
`legacy_id` so saved presets keep matching.

### 2. STORY-2 — the acceptance run on the local model

An **outline**, a **chapter**, a **thumbnail prompt set** and **metadata**, all from
the local model, with receipts. This is item 5 of Studio Hub's test plan; items 1–4
are done. We have dispatched a remote chat job end to end and proved it
(`state=succeeded`, `output_text="remote ok"`, `generation_seconds=1.485`, ran on a
controller over Tailscale) — what is missing is a real Story Studio run.

**The ask:** run it, and tell us what the row did not tell you.

### 3. STORY-4 — label a limit from `*_source_detail`

Every limit we publish carries a companion saying **whose number it is**:
`source` is `provider`, `curated` (the owner's own file) or `gateway` (ours), and
`*_source_detail` is the sentence behind it.

Studio Hub's caveat, and it matters: the **`4096` is their worker's fallback**, not
the model's limit — the checkpoint ships no `generation_config.json`. The context
window *is* the model's own, read from the checkpoint.

```
max_output_tokens                    4096
max_output_tokens_source             "provider"
max_output_tokens_source_detail      "worker default: the checkpoint ships no generation_config.json"
```

**The ask:** is reading `*_source_detail` enough to label that in your UI, or would
you rather have a **machine-readable discriminator** — an enum — instead of a
sentence to parse? We publish either; we would rather you name it than guess at it.

Note the trap this exists to prevent: `normalizeMaxOutputTokens` substitutes 8,192
when the value is `null` and presents it as the model's own figure. We keep
publishing `4096` rather than `null` for exactly that reason — a `null` here becomes
an invented number on your side. **A published `0` is also a claim**, which is why
nothing here uses an `or` chain.

### 4. STORY-5 — an omitted parameter is *that Mac's* operator setting, not the model's default

Studio Hub's statement, 2026-09-25: every Studio lets its operator set their own
default for each parameter a model publishes, **per model per machine**. A worker
applies the stored default when the request *omits* the parameter, and a parameter
the request supplies always wins.

The gateway forwards **only what the caller sets** — it adds nothing. So from your
side, an omitted field does not mean "the model's default"; it means "whatever that
Mac is set to", which is the more useful meaning for a scheduler and the one the Hub
now guarantees. The resolved values are published too, on `generation_defaults` and
on the lane contract's per-parameter `default`, from one call.

**The ask:** should the row carry a **statement** about this, so the distinction is
visible in the model list rather than only in this note? Say yes and we will publish
it; say no and we will leave the row alone.

### What we need back

One line each is plenty. STORY-1b is the one holding up our work.

## PASTE TO HERE

---

## Notes for our side, not theirs

- **GW-2** (`gateway/catalog.py`) stays blocked until STORY-1b names the shape.
- **STORY-4** may become a gateway row if they want an enum — publishing it is
  small, and `_source` / `_source_detail` already come from one function per limit
  (the reason the quote route stopped disagreeing with the catalogue).
- **STORY-5** is the same shape as the "publish what is known, and nothing else"
  rule: if they want the distinction visible, it belongs on the row, not in prose.
- The four rows stay open in the master tracker; this file is the paste, not a
  second copy of the status.
