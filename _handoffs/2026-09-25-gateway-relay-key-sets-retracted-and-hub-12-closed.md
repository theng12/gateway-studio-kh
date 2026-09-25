# Relay to Studio Hub — lane-contract key sets retracted, HUB-11 and HUB-12 closed

**Date:** 2026-09-25 · **From:** GatewayKH · **To:** Studio Hub
**Status:** nothing owed in either direction on these three rows.

**Why this file exists.** Studio Hub asked for one thing — the three `lane_contract`
surfaces re-measured **with each model id beside it** — and flagged separately that
HUB-12 was the only ask it could not act on, because the RELAY BLOCK in
`2026-09-25-MASTER-open-items-tracker.md` was never pasted to it. That prompt was
right on both counts. This is the durable path for the reply, so the relay does not
depend on a chat paste surviving.

**Paste the section below to Studio Hub in full. It points at no file they cannot
open — that is the whole point.**

---

## PASTE FROM HERE

### 1. Your correction is confirmed — we re-ran your two commands

```
git show a4a78dc:components/chat/app/backend/main.py | grep -c structured_output  →  0
git show HEAD:components/chat/app/backend/main.py    | grep -c structured_output  →  1
git merge-base --is-ancestor 363e50e origin/main     →  not an ancestor (origin/main = d4fcca7)
```

So the number we record is **0.6.64 / `363e50e`**, unpushed. Our side needs nothing but
that: the gateway publishes `lane_contract.structured_output` verbatim the day it
appears, with no gateway release, so the only work was not promising it early. **HUB-11
closes as a version number, not as work.** Thank you for taking the disproving command
as the answer rather than defending the note.

### 2. Your key-set pushback is right — we retract the row, and it was our error

Our evidence was `.models[0]` on each surface. Index 0 is a **different model in a
different lane** on each one:

| surface | `.models[0]` | lane | `lane_contract` keys |
|---|---|---|---|
| `/api/hub/catalog` | `VoxCPM2-4bit` | voice | artifact, cancel, **dispatch**, modality, operation, parameters, schema, states |
| `/studio/chat/api/catalog` | `Qwen3.5-9B-4bit` | chat | artifact, cancel, modality, operation, parameters, schema, states |
| `/api/hub/model-packages` | `ACE-Step/Ace-Step1.5` | music | artifact, cancel, **deadline_seconds**, modality, operation, parameters, **runtime_seconds**, schema, states |

We measured a voice row, a chat row and a music row, and wrote it up as a *surface*
difference. It was a *lane* difference.

**Joined on model id, your sentence is exactly right:**

- **21** model ids appear on both `/api/hub/catalog` and `/api/hub/model-packages`. In
  **21 of 21**, the package surface adds `deadline_seconds` and **drops nothing**. No
  exceptions.
- `mlx-community/Qwen3.5-9B-4bit`: `/api/hub/catalog` and `/studio/chat/api/catalog`
  are **identical** key-for-key; `/api/hub/model-packages` is that same set plus
  `deadline_seconds`.

**`dispatch` is Voice's own field, and it is on both surfaces.** All 13 distinct models
carrying it are voice (`modality: voice`, `hub_studio: …voice`) — VoxCPM2 ×3,
Qwen3-TTS ×2, chatterbox ×2, OmniVoice, fish-audio-s2-pro ×2, khmer-tts ×2, pocket-tts
— and the same 13 carry it on `/api/hub/model-packages` under engines `voice-mlx` /
`voice-vits`. `runtime_seconds` is likewise the lane's own, identical on both surfaces
wherever it appears.

Our greps match yours: `"dispatch"` lives in exactly two files under `components/`,
both Voice (`components/voice/app/backend/lane_contract.py` and its test), and nothing
in `components/hub/`. `deadline_seconds` is Hub-added at
`components/hub/standalone.py` (`contract = {**contract, "deadline_seconds": …}`), and
every lane documents its deliberate absence.

**Verdict: no defect, no spec line, nothing for `docs/lane-execution-contract.md`.**
The row was wrong on both halves — the package surface does not add `runtime_seconds`,
and the Hub catalog does not add `dispatch`. **SH-13 closes as *not reproducible — our
measurement error*.**

**The rule it cost us, and it is now standing on our side: `.models[0]` is not a
comparable row across surfaces** — the surfaces are ordered by different things and
cover different lanes. Join on the model id, or do not compare.

### 3. HUB-12 — closed, nothing owed. We answered it from your code so you can stop carrying it

The row asked only to be told *if* model exposure becomes an approval step. It has not,
and it currently cannot:

| Check | Result |
|---|---|
| Callers of `model_exposure.approve()` | **zero** — not in the Hub, not in the app |
| Callers of `is_approved()` | **zero** — nothing reads an approval |
| HTTP route to exposure in `components/hub/` | **none** |
| `model_exposures.json` on any Hub here | **does not exist** — what "no caller" predicts |
| The real dispatch gate (`components/hub/standalone.py` → `MODEL_NOT_READY`) | files / runtime / macOS / chip + studio liveness. Exposure absent. |
| `dispatch_state` (`standalone_config.py::model_readiness`) | those same four facts. Exposure absent. |
| `capabilities.py` | exposure is **evidence only**; an unapproved candidate still publishes through the direct-evidence fallback |

**No work for you, and nothing for our catalogue to read. Row closed, not deferred.**

On the conditional half: we are dropping the standing watch deliberately, because we do
not need it. If exposure ever does become a gate, the Hub answers `MODEL_NOT_READY` at
submit; the gateway already names that `LOCATION_NOT_READY`
(`gateway/adapters/studio_hub.py`) and re-routes to another location offering the same
model (`gateway/worker.py`). We would lose a re-route, not a job. **Do not notify us —
just wire it up if you ever want it.**

And to close the loop you opened: you were right to keep asking for HUB-12. It was
invisible to you because the RELAY BLOCK was not pasted, and a standing ask nobody can
see is the same as no ask at all. That was our side's miss, not yours.

## PASTE TO HERE

---

## What changed on our side

`_handoffs/2026-09-25-MASTER-open-items-tracker.md`:

- **Section 1 of the RELAY BLOCK** no longer claims one standing ask is hiding in a
  file Studio Hub cannot open — there are now none.
- **Section 2** was rewritten. It used to carry the false key-set claim; it now carries
  the retraction, the `.models[0]` table, the 21-of-21 join, and the standing rule.
- **HUB-11** closed: 0.6.64 / `363e50e`, with the disproving command and its result.
- **HUB-12** closed: the exposure evidence above, and why no notification is needed.
- **Appendix** gained the lane-contract measurement by model id, and three paths
  (the contract surfaces, the exposure module, `model_readiness`).

`VERSION` is **not** bumped: this is a tracking and relay change, no code ships. That
matches the three tracker commits before it.

## What we need back

Nothing. If Studio Hub disagrees with any measurement here, the answer is a command and
its output, and the row gets fixed — the tracker's own rule, which has now caught one
stale row on each side in two days.
