# Relay to Studio Hub — everything we owe you, in one paste

**Date:** 2026-09-25 (late) · **From:** GatewayKH · **To:** Studio Hub
**Status:** eight rows are yours. One of them unblocks a row of ours.

**Why this file, and why it supersedes the last one.**
`_handoffs/2026-09-25-gateway-push-proven-and-the-two-gaps.md` carried HUB-14 and
HUB-15 and is still accurate about those. This one is the whole list as it stands
now, including four rows added since, so you are not assembling a picture from two
messages. **Paste the section below in full** — it points at no file you cannot open.
Every measurement in it was taken against the running system on 2026-09-25.

---

## PASTE FROM HERE

### Where things stand, in four lines

1. **Your mirror works, and it has now run on a real fleet.** We published
   `StudioHub-0.6.64-fabe498.dmg` to five controllers: **5 of 5 accepted**, HTTP 200
   in 2563 s, each one countersigning with a **different** key (`e93de129`,
   `35f3ae97`, `e2d837fc`, `f94424d2`, `2ce2a568`). Every mirror is genuinely its
   own object, which is what makes a node pinning its own Hub's key workable.
2. **Our gateway no longer runs one job at a time.** It dispatches to a bounded
   pool — default 4, owner-settable — with a cap per lane and **per provider**, and
   `paid_concurrency_limit` is now derived from the real pool instead of being a
   literal 4. `tests/test_worker_concurrency.py`, 24 checks.
3. **That makes your site capacity the binding number.** With the gateway able to
   run several at once, what limits a location is now its own
   `capacity.by_operation[op].available_worker_slots` — we read it and schedule
   against it. If that count is ever optimistic or stale, it is now the number that
   decides how much work you receive, so it is worth a look from your side.
4. **A version skew worth noticing:** the five controllers hold **0.6.64** while the
   kit drives carry **0.6.65** and `origin/main` is at **0.6.66**. Three different
   answers to "what is current" on one evening.

### The one row of ours this unblocks — HUB-15

**Get a held release onto the nodes.** Your design is right and it is in your own
code: *"a controller mirrors a signed release and its nodes pull it."* The mirror is
real on five controllers. **Nothing pulls.**

Verified in 0.6.64, not inferred:

- A node's `GET /api/hub/release` returns `app_release.load_release()`, which reads
  **its own local files** — no relay, no proxy to its controller.
- The app reads `api/hub/release`, `release/identity` **and `release/artifact`**
  through `makeRequest`, which targets its **own** Hub.
- `peers.release_reconciler.wake_peer(machine)` sits behind
  `if release_reconciler is not None`, and `release_reconciler` is a module global
  **that nothing ever assigns**. The call is dead code.

**Two ways, and one is ours.** (a) You wire the reconciler — the controller forwards
the same gateway-signed descriptor to each node's `POST /api/hub/release`, already
in your fleet allowlist. (b) **We push to the nodes directly**: we already read every
node's address from `/api/hub/fleet/nodes` and hold the group's Fleet token.

**We prefer (a).** (b) puts 1.34 GB per node through this Mac's uplink with an 8 GB
RSS spike per push — which is exactly what the mirror exists to avoid. **A one-word
answer — (a) or (b) — unblocks our GW-7.**

**Both need first:** every node must have the **gateway's** publisher key installed
(`POST /api/hub/release/trust`, Hub-token only, UI under Connections → App updates),
or `accept_release` refuses with 409. Only the five controllers have it today.

**One question we would rather ask than assume.** The app pins the key its **own**
Hub reports on `release/identity` and refuses a descriptor naming a different
`controller_key_id`. Sending the *same* descriptor to every Mac, with each Hub
countersigning under its own key, keeps that consistent — which is what the five
distinct signatures suggest is intended. But if a node is meant to pin the
**controller's** key, the forwarding half has to say so.

### HUB-17 — placement follows the modality, not the model's own memory requirement

We got this wrong the first time and have restated it. Here is the precise version,
and your own published data makes the case.

`_placement_key` decides by **artifact class**: audio and video get
`_audio_placement_key` (**high-memory first**), image and subtitle get
`_flexible_placement_key` (**smallest capable first**), and only chat/transcription
consult the model's published tier. Both are *preferences, never filters* — the
model's own memory floor already excludes a machine that cannot run it.

The floors you publish, read from the Hub:

```
Qwen3-TTS-12Hz-0.6B-Base-8bit   8 GB      VoxCPM2-4bit / -8bit / -bf16   16 GB
chatterbox-4bit / -8bit         8 GB      fish-audio-s2-pro-8bit         24 GB
OmniVoice-bfloat16              8 GB      fish-audio-s2-pro-bf16         32 GB
Qwen3-TTS-12Hz-1.7B-Base-4bit  12 GB
```

So the **0.6B voice model fits an 8 GB Mac and is placed on the largest machine in
the group**, while the 1.7B, the 16 GB VoxCPM2s and the 24–32 GB fish-audio models
— the ones that genuinely need the big Macs — queue behind it.

**The ask, stated precisely:** placement should follow the **model's RAM
requirement**, not its generator. The 0.6B fits 8 GB and should take the small
machines first; the 1.7B should wait for a machine that fits it.

**And it collapses to one rule**, which is the useful part: *"smallest capable
first, everywhere"* already gives exactly that, because **capable is the model's own
floor** — a 16 GB model cannot land on an 8 GB Mac whichever key is used. The only
thing the audio key still buys is **latency** on big machines. So the real question
is whether that is worth spending the machines only a Z-Image-class job can use.

### HUB-16 — a manual listen tick silently drops every address you did not tick

Your code says it plainly (`native/StudioViews.swift::pinListenHosts`): *"A manual
address choice — ticking one, unticking one, or the wildcard — ends automatic mode…
a manual choice pins exactly what is ticked at that moment."*

**Measured on three controllers at once.** The owner ticked a Mac's LAN address;
that ended automatic mode, the list froze to loopback + LAN, and the **tailnet
address was gone**. The gateway dials what the location was added with — all five are
stored as `100.x`, e.g. 1200 at `http://100.114.4.52:47873` — so three locations read
*"Nothing is listening on that address"* and `locations_up` fell to 3/6.

**The behaviour is deliberate** — an untick that silently undoes itself would be
worse — and the checkbox does show that automatic is off. What is missing is the
**consequence**: nothing says "you just dropped the tailnet", and the failure is
invisible from our end, because the gateway only knows that nothing answered.

**The ask:** when a manual tick turns automatic off, say which addresses that pins
and name any it dropped. One sentence at the control.

### HUB-14 — install a pushed release without a click, once the Mac is safely drained

Your reason for the click is sound, so this is not a request to shrink the warning.
It is one click **per Mac**, and this fleet is heading for ~50.

What we would build against if you name it: a per-Mac **opt-in** ("install releases
from the trusted publisher automatically") and a state machine that (1) marks itself
`drained`, (2) waits for in-flight work with a stated ceiling, (3) installs the
verified artifact, (4) restarts services, (5) clears the drain and takes work again.

**You do not need a new flag for step (1).** The gateway already routes nothing to a
controller whose `capabilities.controller.drained` is true, and raises a
`controller_drained` issue naming it. The moment a Hub marks itself drained, new
dispatches stop.

### HUB-13 — the held release still cannot be cleared, and its evidence moved

The row cited the 0.6.50 mirror of 2026-09-23 and the banner *"Older release
available — this Mac is on a newer version"*. That was true then. **Now the same
five controllers hold 0.6.64 against 0.6.63/0.6.65, so the banner should read
"Update available"** — which is what the owner will see when he opens Connections.

Both halves of the ask still stand: **there is no route or control to clear a held
release** (two files deleted by hand, and that artifact is the rollback copy), and
**the heading still does not say it is a mirror for nodes.** "Older release
available" read to the owner as a fault report, which is how this row came to exist.

### HUB-8, HUB-9, HUB-10 — and a question about your tracker

**HUB-8 — validate `lane_contract` on publish.** We treat *absent* as "unstated" and
fall back safely. A **malformed** one is worse than absent, because it looks
authoritative. Omit what you cannot validate.

**HUB-9 — `model_limits` on non-chat lanes where it applies** (a music or video model
with a stated maximum). We would publish it instead of our own cap.

**HUB-10 — state per-lane `uncertain_is_terminal` semantics in prose.** The flag
exists; our poll loop holds `uncertain` unconditionally, so we do not act on it yet.
Name the lane and we will read it from the contract rather than hardcode a list.

**And the question:** these three, plus **HUB-13**, appear nowhere in your tracker —
not under *Open*, not under *Closed*. Four rows may be invisible to you in exactly
the way SH-10 was invisible to us. If your tracker deliberately holds only your own
rows, **say so in one line** and we will stop reading its silence as an answer. That
line is worth more than the four rows.

### What we need back, shortest first

| Row | What we need | Size |
|---|---|---|
| **HUB-15** | **(a) or (b)** — it unblocks our GW-7 | one word |
| **HUB-10** | which lanes, and the semantics in prose | one paragraph |
| **HUB-17** | is latency on big machines worth the machines only big jobs can use? | a decision |
| **HUB-16** | one sentence at the listen control naming what a manual tick dropped | small |
| **HUB-13** | a clear route, and a heading that says *mirror for nodes* | small |
| **HUB-14** | the drain-and-wait design | real |
| **HUB-8 / HUB-9** | omit what you cannot validate; publish a stated maximum where it applies | small |
| **tracker** | whether your file holds only your own rows | one line |

## PASTE TO HERE

---

## Notes for our side, not theirs

- **GW-7** stays blocked on HUB-15 (a) or (b). Nothing to start until then.
- **GW-2** stays blocked on STORY-1b — separate relay,
  `_handoffs/2026-09-25-story-studio-prompts.md`.
- **HUB-17's correction is recorded in the master tracker**, including the note that
  the earlier framing was ours to get right, not the owner's.
- Two release gates have still not been run for tonight's five releases:
  `test_ui.py` (takes over the owner's app — his call) and
  `test_fleet_standalone.py` (needs Studio Hub running, which it is not).
