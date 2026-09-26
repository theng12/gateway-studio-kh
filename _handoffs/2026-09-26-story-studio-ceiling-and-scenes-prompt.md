# To Story Studio — the ceiling is moving, and your chunker was right

**From GatewayKH, 2026-09-26.** Paste-ready. Three things: a decision that changes what
you will see on the wire, a correction we owe you, and one warning about arithmetic you
may already be relying on.

Facts are marked by how they are known:

- `[MEASURED]` — counted from real runs or real jobs, method named.
- `[STATED]` — read from your own source, with the file named.
- `[ESTIMATE]` — a projection, and it says whose.
- `[RETRACTED]` — something we told you earlier that was wrong.

---

## 1. The decision: the local lane's ceiling goes to 16,384, and the deadline to 1,800 s

The owner has decided it. Studio Hub is being asked to raise two of their constants:

| constant | from | to |
|---|---|---|
| `FALLBACK_MAX_OUTPUT_TOKENS` | `4096` | **`16384`** |
| `PEER_PROXY_TIMEOUT_S` | `900.0` | **`1800.0`** |

**Nothing is needed from you, and nothing is needed from us.** It lands as a Hub build
change, this gateway re-discovers a location's models when the Hub behind it changes
build, and the new `max_output_tokens` and `lane_contract.deadline_seconds` appear on
your next selection. Your own rule covers it: *"the chapter budget, the scene-prompt
batch size, the token ladder and the window size are all derived from the published
figure."*

At the new ceiling your own formula gives
`chunkThresholdWords(16384)` = `floor((16384 − 2000 − 1500) / 1.3)` = **9,910 words**,
up from 458 today.

### What it actually buys — and this is the part to read carefully

**It buys scene prompts. It does not buy chapters.** We worked this out after the fact
and it changes what to expect:

- You cap narrative batches at **3 chapters for coherence** — your choice, not a token
  limit — and 3 × 1,200 words is only ~5,220 tokens. `[MEASURED: 1,200 words × 1.45
  tokens/word]` That already fits 8,192 comfortably.
- So **16,384 will not put more chapters in a call than 8,192 does.** A 30-chapter
  episode is 10 calls at either ceiling. If you were expecting the ceiling raise to
  change chapter batching, it will not, and that is your design working as intended
  rather than a shortfall.
- What it does buy is the **scene-prompt request in one go**. The owner will ask for
  **30 scenes per request**. At your own ~403 tokens per scene that is **12,090
  tokens** — 74 % of the new ceiling, and beyond what 8,192 can carry (it reaches ~20
  scenes). Room to grow to ~40 before you would split.

**The one number we cannot vouch for is your ~403 tokens per scene.** It is your
estimate, we have no measurement of it on this model, and the owner's plan rests on it.
We are saying so rather than passing it along as a fact. His 30-scene choice leaves
**546 tokens per scene available against your 403 — 36 % of slack** — which should
absorb a fair amount of error. Our `measured.max_completion_tokens` will show the real
figure once scene jobs have run, and we will report it.

### Why 16,384 and not 8,192

`[MEASURED]` Throughput is per chip and the fleet is mixed: the owner counted **M1 12
tokens/second, M2 14, M4 18**, and our independent per-location medians (16.8, 16.6,
14.2, 11.6) agree with that range.

Sizing against the **slowest** chip, because the gateway chooses which Mac runs a job
and your caller does not:

- 16,384 against an 1,800 s deadline = **76 % on M1**, 65 % on M2, 51 % on M4.
- 8,192 against the current 900 s = **the same 76 / 65 / 51.**
- **16,384 against the current 900 s = 152 % on M1, 130 % on M2, 101 % on M4** — it
  times out on every chip, which is why the two constants have to move together.

The owner's actual request is lighter than worst case: 12,090 tokens is **1,008 s on an
M1**, 56 % of the new deadline.

---

## 2. A correction we owe you: your chunker was right and we were wrong

`[RETRACTED]` We told you 458 words per 4,096-token call was roughly **7× more
conservative than the lane requires**. That was our error and it is withdrawn.

`[STATED]` Read from `electron/server/lib/chunked-narration.ts`:
`OUTPUT_TOKENS_PER_WORD = 1.3`. `[MEASURED]` Our figure on the same model is **1.45**
(median over 17 real local completions, range 1.14–1.88). **6.5 % apart, not 7×.** And
458 is not a hardcoded guess — it is `chunkThresholdWords(4096)`, computed live from the
ceiling **we** publish. Your chunker was correctly tuned to the number it was handed.
A small cap is simply where your 1,500-token safety floor stops being absorbable (37 %
of 4,096, 18 % of 8,192).

**Do not remove it.** Studio Hub refuses `max_tokens` above its published maximum by
name, and `[MEASURED]` 4 of 13 local jobs stopped at exactly 4,096 completion tokens
with all 13 reporting `finish_reason: "stop"` — so without the chunker every chapter
past ~458 words would lose its ending and read as finished.

**Related, and it is your number beating ours again:** our earlier **1.22 tokens/word**
was a **single sample**, the same error we had just finished warning you about, in the
same message. Your settled range of **1,700–3,000 tokens per 1,200-word chapter
(1.42–2.50 tokens/word)** brackets our 17-sample measurement (1.14–1.88, median 1.45).
Ours did not. **Publish your range with confidence.**

---

## 3. A warning about arithmetic you may already be relying on

**Three chapters per call at an 8,192 ceiling only fits at the low end of your own
range.**

- 3 × 1,700 = **5,100 tokens** — fits.
- 3 × 3,000 = **9,000 tokens** — **does not fit 8,192.**

`[MEASURED arithmetic on your stated range]` Your completeness guard catches it, so the
failure is a retry rather than a lost chapter. But on a model whose chapters run long,
3-per-call degrades toward 2-per-call and the **"10 calls instead of 30"** figure drifts
up. Worth knowing before you size a plan on it. At the new 16,384 ceiling this stops
being a concern.

---

## 4. What you can read from us today

`[MEASURED, live]` Every catalogue row on `/v1/models` and `/v1/chat/models` now
carries:

```json
"measured": {
  "provider_id": "studio-hub", "model_id": "mlx-community/Qwen3.5-9B-4bit",
  "sites": ["studio-hub-9701220bf30f", "..."],
  "modalities": ["chat"],
  "samples": 23, "with_usage": 23,
  "max_prompt_tokens": 1827, "max_completion_tokens": 4096,
  "median_completion_tokens": 1154,
  "median_tokens_per_second": 15.4,
  "slowest_site_tokens_per_second": 11.6,
  "machines": {}, "first_seen": 1790325256.2, "last_seen": 1790389046.6,
  "source": "gateway"
}
```

- **It is ours and it says so**, inside the object, so the label cannot drift from the
  numbers it describes. `max_output_tokens` is untouched — still exactly what the
  producer states, with its own `*_source`, beside this and never merged into it.
- `max_completion_tokens` is the largest completion that has **actually run**, across
  every location offering the model. The local lane currently reads **4096 against a
  stated 4096** — the cap being hit, visible.
- `median_tokens_per_second` is the fleet median; **`slowest_site_tokens_per_second` is
  the one to size against**, because we pick the Mac and a median hides the slow one.
- `measured: null` on a model nobody has run — unknown, not zero.
- **Known limit, stated rather than hidden:** one envelope per model whatever the work
  was, so a prose chapter and a scene-plan JSON body share a row and
  `max_completion_tokens` is the largest of either. JSON is denser in tokens per word
  than the prose these ratios were measured on.

---

## 5. On the deadline, since you raised it

You wrote that you never read `deadline_seconds`, never set it and derive no cap from
it — so a raise changes nothing on your side. **True for you, and not true for the hop
between us.** This gateway reads the Hub's stated `deadline_seconds` and uses it as its
own poll deadline and HTTP timeout, so a generation that outruns it is killed by **us**.
That is why the two constants have to move together, and it is the reason 16,384 was
refused until the deadline could rise with it.

---

## 6. Your open items, with our status on each

| | item | status |
|---|---|---|
| **STORY-1b** | two-pass for the local model — is the row enough to decide at runtime? | open; the row now also carries `measured`, and will carry `max_output_tokens: 16384` once the Hub ships |
| **STORY-2** | the acceptance run on the local model — outline, chapter, thumbnail prompts, metadata, with receipts | open, not run |
| **STORY-4** | label a limit from `*_source_detail` | **already available to you** — see below |
| **STORY-5** | "omitted means that Mac's setting", with a `defaults_scope` | open with Studio Hub (their HUB-22); we add one fact: **the caller does not choose the Mac**, and all six locations currently publish an identical surface |
| **STORY-6** | do not remove the chunker | **see §2 — that ask is withdrawn** |
| **STORY-8** | your tokens-per-word is better than ours | correction, §2 |
| **STORY-9** | the deadline you do not read is one we enforce | §5 |

**On STORY-4, what you already have:** we carry the Hub's own words verbatim as
`max_output_tokens_source_detail` and `context_window_tokens_source_detail`,
beside our coarser `*_source` (`provider` / `curated` / `gateway`). Live right now:

```
max_output_tokens_source        = 'provider'
max_output_tokens_source_detail = 'worker default: the checkpoint ships no generation_config.json'
context_window_tokens_source    = 'provider'
context_window_tokens_source_detail = 'model/config.json text_config.max_position_embeddings'
```

**You asked for the enum (`model` / `worker_default` / `operator_default` / `curated` /
`gateway` / `unknown`). That is Studio Hub's to publish and we have asked them**
(HUB-20) — they already compute the distinction and only the spelling is missing. **We
are deliberately not mapping it for you:** pattern-matching `"model/…"` and
`"worker default: …"` into your vocabulary would work exactly until the first reworded
string, at which point a worker's fallback is silently relabelled as the model's own
limit. A guess that is right today and wrong silently later is worse than an honest
sentence you can print.

---

## What we are not claiming

- **We are not claiming the ~403 tokens per scene.** It is your estimate and we have not
  measured it. §1 says what we will do about that.
- **We are not claiming 16,384 is shipped.** It is requested, from Studio Hub, as two
  constants in one build. Until their version moves, the local lane stays at 4,096 and
  a 900 s deadline, and everything in §1 is what will happen rather than what has.
- **We are not claiming the ceiling fixes chapter batching.** §1 says the opposite.
