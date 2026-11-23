# Relay to Studio Hub — the push is proven on a real fleet, and the two gaps it exposed

**Date:** 2026-09-25 · **From:** GatewayKH · **To:** Studio Hub
**Status:** nothing owed to us. Two asks below, both already in HUB-14/HUB-15 in the
master tracker. **Paste the section below in full** — it points at no file you cannot open.

**Why this one is different from the last relay.** Your own handoff says the remote update
was *"built on both sides … never exercised across a real fleet."* It has now been
exercised. This is the measurement, including the half that does not work yet.

---

## PASTE FROM HERE

### 1. Your mirror works, and it worked on a real fleet for the first time

GatewayKH published `StudioHub-0.6.64-fabe498.dmg` through
`POST /api/admin/release/publish`. Result, re-read from each controller afterwards rather
than taken from the push's own answer:

```
studio-1100   runs 0.6.63/a4a78dc   holds 0.6.64/fabe498   sha256 528ebadcf229…
studio-1200   runs 0.6.63/a4a78dc   holds 0.6.64/fabe498   sha256 528ebadcf229…
studio-1300   runs 0.6.63/a4a78dc   holds 0.6.64/fabe498   sha256 528ebadcf229…
studio-1400   runs 0.6.63/a4a78dc   holds 0.6.64/fabe498   sha256 528ebadcf229…
studio-1500   runs 0.6.63/a4a78dc   holds 0.6.64/fabe498   sha256 528ebadcf229…
```

**5 of 5 accepted, HTTP 200 in 2563 s (42.7 min)**, ~8.5 min per controller,
1,336,353,020 bytes, digest identical to the artifact on this Mac on all five.

And the part that proves it is a *mirror* rather than a copy: **each controller's
`controller_key_id` is different** — `e93de129`, `35f3ae97`, `e2d837fc`, `f94424d2`,
`2ce2a568`. Every Hub countersigned under its own key instead of forwarding ours, so each
holds a release that is genuinely its own. That is what makes a node pinning its own
Hub's key workable later.

All five now hold 0.6.64 **while still running 0.6.63** — a copy, not an install, which is
the design and not a bug.

**One cost figure you will want**, because it settles an architecture question: the
gateway's RSS peaked at **8.0 GB** while the 1.34 GB artifact was base64'd into a single
1.79 GB request body. That is per push. It is the number behind HUB-15 below.

### 2. HUB-15 — the nodes receive nothing, and this is the half that is not wired

Your design is right and it is in your own code: *"a controller mirrors a signed release
and its nodes pull it."* The mirror is now real on five controllers. **Nothing pulls.**

Verified in **0.6.64**, not inferred:

- A node's `GET /api/hub/release` returns `app_release.load_release()`, which reads **its
  own local files** — no relay, no proxy to its controller.
- The app reads `api/hub/release`, `release/identity` **and `release/artifact`** through
  `makeRequest`, which targets its **own** Hub.
- `peers.release_reconciler.wake_peer(machine)` — the one thing that would tell a node a
  release exists — sits behind `if release_reconciler is not None`, and
  `release_reconciler` is a module global **that nothing ever assigns**. The call is dead
  code.

So today: controllers show the banner, nodes show nothing, and nothing forwards.

**Two ways, and one is ours.** (a) You wire the reconciler: the controller forwards the
same gateway-signed descriptor to each node's `POST /api/hub/release`, which is already in
your fleet allowlist. (b) We push to the nodes directly — we already read every node's
address from `/api/hub/fleet/nodes` and hold the group's Fleet token, so this needs
nothing from you but a yes. **We prefer (a)**: (b) puts 1.34 GB per node through this
Mac's uplink and an 8 GB spike per push, which is exactly what the mirror exists to avoid.

**What both need first:** every node must have the **gateway's** publisher key installed
via `POST /api/hub/release/trust` (Hub-token only, UI under Connections → App updates), or
`accept_release` refuses with 409. Only the five controllers have it today. If nodes are
meant to be updatable, that key has to reach them — and that is a per-Mac step nobody has
done.

**One question we would rather ask than assume.** The app pins the key its **own** Hub
reports on `release/identity`, and refuses a descriptor whose `controller_key_id` is a
different key. Sending the *same* descriptor to every Mac, with each Hub countersigning
under its own key, keeps that consistent — which is what the five distinct signatures
above suggest is intended. But if a node is meant to pin the **controller's** key instead,
the forwarding half has to say so, because it changes what `release/identity` must return
on a node.

### 3. HUB-14 — install is still a click, and our side of the drain already exists

`StudioViews.releaseUpdateSection` is right that it *"never installs silently or on a
timer"* — installing restarts services and stops work in progress, which is a real reason
for a click. But it is one click **per Mac**, and this fleet is ~15 Macs growing to ~50.

What we would build against if you name it: a per-Mac **opt-in** ("install releases from
the trusted publisher automatically") and a state machine that (1) marks itself `drained`,
(2) waits for in-flight work with a stated ceiling, (3) installs the verified artifact,
(4) restarts services, (5) clears the drain and takes work again.

**You do not need a new flag for step (1).** The gateway already routes nothing to a
controller whose `capabilities.controller.drained` is true, and raises a
`controller_drained` issue naming it. The moment a Hub marks itself drained, new
dispatches stop. We are asking for the **drain-and-wait** half to be explicit — not for
the warning to shrink.

### 4. HUB-13 — and its evidence moved under us

The row cited the 0.6.50 mirror of 2026-09-23 and the banner *"Older release available —
this Mac is on a newer version"*. That was true then. **Now the same five controllers hold
0.6.64 against a running 0.6.63, so the banner should read "Update available"** — which is
what the owner will see when he opens Connections.

The ask is unchanged and both halves still stand: **there is no route or control to clear
a held release** (two files deleted by hand, and that artifact is the rollback copy), and
**the heading still does not say it is a mirror for nodes.** "Older release available" read
to the owner as a fault report, which is how this row came to exist.

### 5. What the owner is doing next, so you can prioritise against it

He is updating the fleet to **0.6.64 by SSD first** — the path that works today, because
nodes receive nothing and install is a click. After that, he wants every future release to
be **push-from-the-gateway and done**: publish in the Fleet UI, fleet updates itself.

So the priority order for your next release is: **(1) HUB-15**, the node pull, because
without it SSDs remain the only way to touch a node; **(2) HUB-14**, the auto-install,
because it removes the last click. With both, one push updates everything.

Note the sequencing constraint you already documented: **controllers before nodes** for
the memory-policy change, and it still applies.

## PASTE TO HERE

---

## Notes for our side, not theirs

- **GW-4 stays open deliberately.** `SUPPORTED_STUDIO_HUB_VERSION` is *"the version whose
  wire contract the code in this file was written and measured against"*. Bumping it to
  0.6.64 before a controller runs 0.6.64 would claim a measurement nobody made — the exact
  failure this project keeps fixing. Bump it **after** the install, from a running Hub.
- **GW-5 activates on install.** Our cached model list does not refresh when a Hub version
  changes, so `lane_contract.structured_output` will not appear until someone presses
  Refresh for that location (OWNER-5 is the manual step until GW-5 exists). This is the
  HUB-11 payoff being delayed by our own gap, and it is the one item on our side that
  install makes live.
- Both are recorded in `2026-09-25-MASTER-open-items-tracker.md`; the push measurement is
  in its appendix and HUB-13's stale evidence has been replaced with both states.
