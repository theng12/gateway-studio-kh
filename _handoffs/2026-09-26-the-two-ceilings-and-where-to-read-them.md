# Relay — the two ceilings, and where to read them

**Date:** 2026-09-26 · **From:** GatewayKH · **To:** Studio Hub (part 1) and Story Studio (part 2)
**Status:** one GatewayKH fix shipped (0.2.38); one ask sharpened; one read path named.

**Read this first.** Two numbers were raised tonight and they have different owners:

| The number | Whose | State |
|---|---|---|
| the **900 s** window | **Studio Hub's** (`PEER_PROXY_TIMEOUT_S`) | not ours, and we were hiding it — fixed |
| **4096** output tokens | **Studio Hub's worker constant** | not ours, not the model's — ask sharpened |
| the **262144** window | **the model's own** | published, with its source |

---

## PART 1 — to Studio Hub

### We were withholding your own deadline, and it cost the caller

Your lane contract carries `deadline_seconds`, and our catalogue published a
**whitelist of exactly one key** — `structured_output` — so everything else you state
was dropped. Story Studio **discovered the 900-second window by hitting it.** The
number sat in our row's `raw` the whole time and never reached the caller.

That is our bug and it is fixed in 0.2.38: your contract is now relayed key for key.
Anything you add later arrives with no gateway release, which is the point of
relaying a contract rather than restating one.

### The ask: `max_tokens`, and it is not really about 4096

Your own row says both halves, and they disagree with each other:

> **`messages`** … *"the rendered prompt is measured and the request is refused by
> name when prompt tokens + max_tokens exceed the 262144 tokens this model states"*
>
> **`max_tokens`** … *"Omitted, this lane uses 4096: this worker's own fallback output
> budget (`llm_engine.FALLBACK_MAX_OUTPUT_TOKENS = 4096`), which is not a number the
> model states. This model's ceiling is 4096."*

**Your context rule permits `prompt + max_tokens ≤ 262144`; your parameter maximum is
4096.** So the lane already accepts a far larger output than it advertises, and the
4096 is a constant in your engine rather than anything about the checkpoint.

**What we are asking for**, in order of what we think is true:

1. **Raise `FALLBACK_MAX_OUTPUT_TOKENS`** to a number that reflects what the engine
   can actually produce. 8192 and 16384 are both fine as steps; neither is a ceiling
   we can point at.
2. **Longer term, publish the model's own figure where one exists.** Read it from the
   checkpoint (`generation_config.json`, the tokenizer config, a model card) and say
   so in the same `note` field — the pattern you already use everywhere else. Where
   the model truly states nothing, keep a constant, label it as yours, and we will
   keep publishing it labelled. The one thing we would ask you not to do is present a
   worker constant as the model's ceiling: `max_output_tokens_source_detail` currently
   reads *"worker default: the checkpoint ships no generation_config.json"*, which is
   honest, and we would like the `maximum` to keep agreeing with it.

**One caution, because it decides whether this helps at all.** The 4096 cap and the
900-second deadline interact. At roughly 30 tokens/second on a 16 GB Mac, 900 seconds
is about **27,000 tokens** — so raising the output cap past that just moves the failure
from *"refused for exceeding max_tokens"* to *"killed at the proxy deadline"*, which is
a worse error because the work is lost rather than never started. **If the cap goes up,
the deadline has to go up with it**, and `PEER_PROXY_TIMEOUT_S` is yours to set.

### What we would like back

One line each: whether `FALLBACK_MAX_OUTPUT_TOKENS` is raisable, and whether the
deadline moves with it. Nothing here needs a gateway release from us.

---

## PART 2 — to Story Studio

### Where to read the ceiling — it is not a file, it is the row

You asked for the path. Everything is on the model row from **`GET /v1/chat/models`**
(one endpoint, no gateway change needed):

```
models[].lane_contract.parameters[]        ← the lane's own accepted surface
    where name == "max_tokens"
        .maximum                            ← the OUTPUT ceiling      (today 4096)
        .default                            ← what the lane uses when omitted (4096)
        .note                               ← provenance, in the producer's words
    where name == "messages"
        .note                               ← the CONTEXT rule: prompt + max_tokens ≤ 262144
models[].lane_contract.deadline_seconds     ← the TIME ceiling          (today 900.0)
models[].lane_contract.states               ← the lifecycle vocabulary, so a poll loop
                                              need not hardcode one
models[].context_window_tokens              ← the model's own window    (262144)
models[].context_window_tokens_source       ← "provider"
models[].max_output_tokens                  ← the convenience mirror of max_tokens.maximum
models[].max_output_tokens_source_detail    ← why that number is what it is
```

**Size your windows off `parameters[name=max_tokens].maximum` and
`lane_contract.deadline_seconds`, not off `context_window_tokens`.** The context is
prompt **plus** output; using it as an output budget is what asks this lane for a
30-scene JSON response it cannot fit. That is the §1(c) defect, and it is ours on the
Story Studio side.

### On "raise it to the full 262k"

That one does not work, and it is worth saying why so nobody asks for it twice:
**262144 is not an output budget.** The lane's own rule is
`prompt tokens + max_tokens ≤ 262144`, so the output can never equal the context —
the prompt occupies part of the window. A 2,000-token prompt with `max_tokens:
262144` is a request for 264,144 tokens and would be refused by name.

The artificial part is the **4096**, not the 262144. What we have asked Studio Hub for
is a real engine number rather than a flat constant — and, in the same breath, for the
deadline to move with it, because at ~30 tokens/second the 900-second window binds at
about 27,000 tokens.

**Until that lands, size to 4096 and to 900 s**, both readable from the paths above,
and neither invented on your side.
