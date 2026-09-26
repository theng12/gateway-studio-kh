# To Studio Hub — two constants, and why both

**From GatewayKH, 2026-09-26.** Paste-ready. This supersedes the ceiling ask in every
earlier message: it is no longer 8,192, and it is no longer one number.

Every fact below is marked by how it is known:

- `[MEASURED]` — counted from real runs or real jobs, and the method is named.
- `[STATED]` — read from your own source or your own wire, with the file or field named.
- `[ESTIMATE]` — somebody's projection, and it says whose.
- `[RETRACTED]` — something we said earlier that was wrong. Kept visible on purpose.

---

## The decision

**Raise both, in the same build:**

| constant | where | from | to |
|---|---|---|---|
| `FALLBACK_MAX_OUTPUT_TOKENS` | `components/chat/app/backend/llm_engine.py` | `4096` | **`16384`** |
| `PEER_PROXY_TIMEOUT_S` | `components/hub/standalone.py` | `900.0` | **`1800.0`** |

**They must move together.** One without the other makes things worse, and the
arithmetic below is why.

The owner has decided this, and the rest of this message is the reasoning so it can be
checked rather than taken on trust.

---

## Why the ceiling goes to 16,384 rather than 8,192

Because the owner's workload is **scene prompts**, not chapters, and 8,192 cannot carry
the request he wants to make.

- The owner will ask for **30 scenes per request**. `[STATED by Story Studio]` Their
  settled figure is **~403 output tokens per scene prompt** (they give it as 32,768
  carrying ~81 scenes and 65,536 carrying ~163, which agrees at ~403 both times).
- 30 × 403 = **12,090 tokens** — inside 8,192? No: **74 % of a 16,384 ceiling**, and
  **well past 8,192**. At 8,192 the same request only reaches ~20 scenes. `[MEASURED
  arithmetic on the two figures above]`
- That leaves **546 tokens per scene against their 403 estimate — 36 % of room.**

**The one input we cannot verify is that ~403 figure.** It is Story Studio's estimate,
not a measurement on this model, and the whole plan rests on it. We are saying so
rather than presenting it as settled. The 36 % slack absorbs a fair amount of error,
and our own record will show the real number once scene jobs have run at the new
ceiling — `GET /v1/models` carries `measured.max_completion_tokens` per model.

## Why the timeout goes to 1,800 and not stays at 900

`[MEASURED]` Throughput is per chip, and this fleet is mixed. The owner counted his own
machines: **M1 12 tokens/second, M2 14, M4 18.** This gateway's independent
per-location medians — 16.8, 16.6, 14.2, 11.6 — came from real jobs and agree with that
range.

`[STATED]` `PEER_PROXY_TIMEOUT_S` is your own *"one request timeout enforced on a job
request it proxies to a worker"*, so it covers **load + prefill + generation**, not
generation alone. That is the fact the whole sizing turns on.

Worst case, a request that fills its ceiling:

| ceiling | M1 (12/s) | M2 (14/s) | M4 (18/s) |
|---|---|---|---|
| 8,192 against a 900 s proxy | 683 s = **76 %** | 585 s = 65 % | 455 s = 51 % |
| **16,384 against an 1,800 s proxy** | 1,365 s = **76 %** | 1,170 s = 65 % | 910 s = 51 % |
| 16,384 against the *current* 900 s | 1,365 s = **152 %** | 1,170 s = **130 %** | 910 s = **101 %** |

**16,384 at 1,800 s is exactly margin-neutral with 8,192 at 900 s** — the same 76/65/51
on every chip. No machine loses headroom and every machine doubles its capacity. The
bottom row is what happens if the ceiling moves alone: **it times out on all three
chips**, including the M4.

The owner's actual request is lighter than worst case: 12,090 tokens is **1,008 s on an
M1**, 56 % of the 1,800 s.

## Why the ceiling cannot be smaller and still work

`[MEASURED]` **Above 9,216 tokens, the M1 is excluded at a 900 s proxy** — 12,288 is
1,024 s on an M1. Since the gateway chooses which location runs a job, a ceiling one
machine cannot meet is a job that dies at our hop when it lands there. 12,288 would
carry 30 scenes and still strand the M1; 16,384 with the proxy at 1,800 carries ~40
scenes and strands nothing.

## Why one constant is enough for the ceiling — checked in your code, not assumed

`[STATED]` `llm_engine.py` sets:

```python
self.max_output_tokens = FALLBACK_MAX_OUTPUT_TOKENS
self.max_output_tokens_source = "worker default: the checkpoint ships no generation_config.json"
```

`model_limits()` returns that as `max_output_tokens.value`, and `main.py` turns it into
the `max_tokens` **`maximum`** on the parameter surface. So raising the fallback raises
the ceiling, the figure this gateway publishes, and Story Studio's chunk threshold
together. **Nothing else has to be coordinated**, and nothing on our side caps it —
we read your stated `deadline_seconds` unchanged and use it as the chat lane's timeout.

---

## The cost, stated so it is accepted knowingly

Every chat job — including a two-minute one — may now **hang for thirty minutes before
being declared dead instead of fifteen.** The fast jobs pay the price of the larger
ceiling too.

**If a timeout that scaled with the request's own `max_tokens` is cheap on your side,
it is strictly better** and we would rather have it: charge the long wall only to the
requests that asked for the long generation. We are not asking for it. A flat 1,800 is
sufficient and we would rather you ship two constants than three.

---

## One catch that decides whether this works at all

**This has to land as a build change, so your version moves with it.** This gateway
re-discovers a location's models when the Hub behind it changes **build** — that is
what republishes the new ceiling and the new deadline to us and onward to Story Studio.
A value made settable at runtime with no version change would leave us publishing 4,096
for ever, and **nothing would look wrong.**

If you ever make it an operator setting rather than a build constant, tell us and we
will add a re-read.

---

## What we owe you — corrections and credit

- **Your `finish_reason` behaviour is now worked around, and we are not asking you to
  change it urgently.** `[MEASURED]` From our own job store: **4 of 13
  `mlx-community/Qwen3.5-9B-4bit` jobs stopped at exactly 4,096 completion tokens, and
  all 13 reported `finish_reason: "stop"`** — so a ceiling-cut answer is
  indistinguishable from a finished one. We reported this as a live danger. Story
  Studio has since shipped the right guard: *a completion that used its entire
  requested ceiling is treated as incomplete, whatever `finish_reason` claims.* The
  caller is the only party that knows what it asked for, so their fix is better than
  anything we could have done. **The row stays open only for a future caller with no
  such guard** — a statement from you would close it.
- **We are not asking you to change the 4,096 itself just to change `finish_reason`.**
  HUB-19 here is the ceiling raise. They are separate asks and either can ship alone.
- **Credit where it is yours:** you publish the output limit's source **structurally**
  (`model_limits.max_output_tokens.source` = `"worker default: the checkpoint ships no
  generation_config.json"`), not buried in prose. That is why we can carry your exact
  words to Story Studio verbatim. See HUB-20 for the one small thing still missing.

---

## Still open with you, unchanged

Four small ones, all in the master tracker. None block the decision above.

1. **HUB-20 — publish the limit's `source` as a stable token, not a sentence.** Story
   Studio wants `model` / `worker_default` / `operator_default` / `curated` / `gateway`
   / `unknown` so they can *branch* rather than print a sentence at a customer. You
   already compute the distinction; only the spelling is missing. **We will not do this
   mapping for you, deliberately** — pattern-matching `"model/…"` and
   `"worker default: …"` into an enum works exactly until the first reworded string, at
   which point a worker's fallback is silently relabelled as the model's own limit. A
   guess that is right today and wrong silently later is worse than an honest sentence.
2. **HUB-21 — publish `max_structured_items_per_request`** on the chat lane.
   Story Studio already reads it; it is simply absent from your
   `lane_contract.structured_output`. **The gateway needs no change** — we republish
   the whole `lane_contract` verbatim, verified live, so the field appears on their next
   read with no release from us.
3. **HUB-22 — state what an omitted parameter means, and on which Mac.** Story Studio
   asked for a `defaults_scope`. We add one fact: **the caller does not choose the
   Mac.** Six locations serve every local model and we pick the site at dispatch, so
   "that Mac's setting" is not determinable from the caller's side — the same request
   can omit `max_tokens` and be served differently at two sites. All six currently
   publish an identical surface so nothing is wrong today.
4. **HUB-18 — see above.** A statement would close it.

## And one we owe you rather than the reverse

`[RETRACTED]` We told Story Studio their chunker was ~7× too conservative for this lane.
**That was wrong.** Their `OUTPUT_TOKENS_PER_WORD = 1.3` is within 6.5 % of our measured
**1.45**, and their 458-word threshold is computed live from the ceiling **we** publish.
The chunker was never the defect; the 4,096 was. The full retraction is in STORY-6 and
this file's master tracker. It is recorded here because the same number was in our
message to you, and a wrong figure left standing is worse than one withdrawn.
