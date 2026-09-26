# To Story Studio — one gate on the 30-scene pack that the ceiling raise does not open

**From GatewayKH, 2026-09-26.** Paste-ready. One finding, one ask, and a clear answer on
sequencing. Facts are marked by how they are known:

- `[READ]` — read from your source, file and line given. **Not run** — we cannot execute
  your pipeline, so these are findings to confirm rather than observed failures.
- `[MEASURED]` — counted from real jobs or real runs, method named.
- `[STATED]` — from your own message or your own wire.

---

## The finding: your two readers of one field disagree about `null`

`max_structured_items_per_request` **is published by nobody.** It is not in Studio Hub's
installed bundle, not in their `lane_contract.structured_output`, and not in this
gateway. `[MEASURED: grep over the installed bundle, our repo, and the live
`/v1/chat/models` row]` Your `genstudio-client.ts:4866` parses it from the model row and
correctly yields `null`.

What happens next depends on **which of your own generators asks** `[READ]`:

| consumer | on `null` |
|---|---|
| `services/contextual-cues.ts:132-143` → `resolveContextualCueBatchSize` | `return published === null ? policySize : Math.min(policySize, published)` — **proceeds with your own batch size** |
| `generators/scene-plan.ts:496-510` → `assertScenePromptPackCapability` | `typeof structuredItemLimit !== 'number'` → **throws** `this JSON contract requires N structured items, above its published unpublished item limit` |

**The first is right and the second reads absence as zero.** *"Nobody published a limit"*
and *"the limit is zero items"* are different facts, and that is the one conflation both
our projects are built to refuse. Here it costs a whole feature rather than a wrong
number.

**Where it fires** `[READ]`: `scene-plan.ts:1531`, inside `beforeModelAttempt` of the
`callAiWithModelPolicy` path — so it **throws before the model is called**, and it is
independent of pack size. It fires for a 1-scene pack exactly as it does for 30.

---

## The two gates on a 30-scene pack, and only one of them is the ceiling

| gate | what passes it | whose change |
|---|---|---|
| **1. pack size** — `resolveScenePromptPackSize` → `maxSafeScenePromptPackSize` = `max(1, floor(max_output_tokens × safetyFraction ÷ tokensPerScene))` `[READ: scene-prompt-batching.ts:112-114 and 240-248 — 0.75 and 300]` | at 4,096 → **10 scenes**; at 16,384 → **40 scenes** | **Studio Hub's raise** |
| **2. item assert** — `max_structured_items_per_request >= itemCount` `[READ: scene-plan.ts:504-508]` | a published limit, **or** treating `null` as no published cap | **yours** |

`[MEASURED arithmetic]` `floor(4096 × 0.75 ÷ 300)` = **10**; `floor(16384 × 0.75 ÷ 300)` =
**40**. Gate 1 is exactly what your letter described as *"~10 scenes per request instead
of 30"* for this lane, and it is the number the ceiling raise moves.

**The token assert in the same function is not a third gate — it is vacuous on a fresh
run** `[READ: scene-plan.ts:498 against scene-prompt-batching.ts:266-273]`:
`resolveScenePromptTokenBudget` returns `min(requested, modelMaximum)`, so `maxTokens`
can never exceed the model's own ceiling and `model.max_output_tokens < maxTokens` cannot
be true. It is a guard for a **stale saved request plan** carrying a larger model's
figure (`scene-plan.ts:1697`: `pack.params.max_tokens ?? visualMaxTokens`), not a live
constraint. We are saying so because an earlier draft of this message listed it as a
gate the raise would open, and that was wrong.

**Your own comment already says gate 1 follows the published figure** `[READ:
scene-plan.ts:769-776]`: *"The batch is already bounded from the published figure by
`resolveScenePromptPackSize` (16,384 × 0.75 ÷ 300 ≈ 40 motion scenes per call), which is
the protection the old cap was reaching for — derived instead of declared."* That is
exactly right, and removing the motion stage's 4,096 clamp on 2026-09-26 was the same
principle applied one level up.

**So gate 2 is the only thing standing between the owner and a 30-scene pack**, and the
ceiling raise cannot open it. At 16,384 your derived maximum is **40**, so his 30 sits
comfortably inside — but the pack is asserted before the model is called, and that assert
fails on an absent field. **Note it fails at 4,096 too**: the pack would be 10 scenes and
the limit is still unpublished, so this is not a consequence of the raise, it is a
consequence of the field being absent at any size.

---

## The ask

**Make `assertScenePromptPackCapability` treat an unpublished item limit the way
`resolveContextualCueBatchSize` already does** — as *no published cap*, not as zero —
and proceed. Your two generators then agree, the field becomes optional, and the 30-scene
pack is gated only by the token budget that is genuinely the binding constraint.

**If the strictness is deliberate** — *"we cannot hold a lane to a JSON contract for N
items without a published guarantee"* — that is a defensible position and the sibling
generator is then the one that should change. Either way they should agree, and the
choice is yours to make, not ours.

**Please confirm the finding either way.** We read your source; we did not run your
pipeline, and a `[READ]` that turns out not to fire is worth less than a sentence from
you saying which branch runs.

---

## Sequencing — asked directly by the owner, answered

**It needs both, and neither of them is ours.**

1. **Studio Hub raises the ceiling to 16,384** — gate 1, and it takes this lane from
   **10 scenes per call to 40**. Requested; sitting in their working tree; **not
   released**, so the local lane is still 4,096 / 900 s today.
2. **You relax the `null` handling in `scene-plan`** — gate 2, the item assert.
3. **Nothing from this gateway.** There is no number for us to change; we read the Hub's
   `max_output_tokens` and republish it, which is why the raise propagates to you with no
   release from us.

The two are **independent** — either can land first and neither blocks the other. Both
must land before a 30-scene pack completes. If you would rather not change gate 2, tell
us and we will put the field on the row instead (see below), which opens it a different
way.

---

## If that field is ever published, it must be at the row's top level

`genstudio-client.ts:4866` reads `value?.max_structured_items_per_request` — a **sibling
of `max_output_tokens`**, not a path inside `lane_contract`. A value published only under
`lane_contract.structured_output` would reach you as `null` and look identical to nobody
publishing at all.

**We have not built that lift, deliberately.** The field does not exist anywhere yet, so
writing a key path for it would be inventing one. If Studio Hub names where it will live,
or you tell us the shape you want, it is a small change on our side.

---

## What we are not claiming

- **We are not claiming the fetch throws in practice.** It is read from your source at
  the lines given, not observed. See the ask above.
- **We are not claiming the field is yours to publish.** It is on the row *we* serve;
  what is missing is anybody stating a value, and the fastest fix is the `null` handling,
  not a new number.
- **We are not claiming the ceiling is raised.** It is requested from Studio Hub and in
  their working tree; the local lane is still 4,096 / 900 s.
