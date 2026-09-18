# Credential rotation and revocation for a 50-machine fleet

**A decision document, not a change.** Everything below is measured on this Mac against a running
Hub 0.5.0 and this gateway. The question came from the Studio Hub session, and it is the sharpest
thing either side has raised: *every Hub mints its own token, a fleet adopts one shared credential,
and there is no rotation or revocation path.*

Read the summary and the recommendation; the details are there if you want to check the reasoning.

---

## Summary

There are **two different credentials** being called "the shared one", and they carry different
risks. The choice that actually matters is one you make, not one a session can make for you:

> **Does each machine get its own credential, or is one value reused across all of them?**

That single decision determines whether revoking one site is a five-minute job or a fifty-machine
outage. Everything else follows from it.

My recommendation: **use each controller's fleet credential, distinct per machine, and ask the Hub
session for a rotation endpoint before the fleet gets large.** No new architecture needed on either
side.

---

## What I measured

### The credentials that exist on a Hub

| File | Scope |
|---|---|
| `hub/.hub_token` | **operator** — full control |
| `hub/.fleet_token` | **narrow allowlist** |
| `hub/.kh_studio_token` | narrow (same value as above on this Mac) |
| `voice/fleet_token` | the Voice Studio's own; 401 on every hub route |

`hub-ready.json` publishes the operator token, which is what this gateway reads for the **local**
Mac.

### What each can reach — the part that matters

Every route this gateway calls works with **either** credential:

```
fleet/overview · fleet/nodes · jobs · stats · workers · capabilities · model-packages   → 200 / 200
```

They diverge exactly where the gateway has no business:

| route | operator | fleet |
|---|---|---|
| `/api/hub/settings` | 200 | **403** |
| `/api/hub/controller` | 200 | **403** |
| `/api/hub/storage` · `maintenance` · `token` · `shutdown` · `releases` | 404 (authorised, no route) | **403** |

So the fleet credential is **sufficient for 100% of what the gateway does**, and it cannot change a
controller's settings or control-plane identity. It is a real allowlist, not a weaker token.

### Rotation does not exist

`/api/hub/token` answers **404 to the operator** (authorised; the route is not there) and **403 to
the fleet credential**. There is no endpoint that mints a new credential and invalidates the old
one. Today, "rotating" means hand-editing a file on the machine.

---

## The two credentials, separated

These are conflated in the phrase "one shared credential", and they are not the same problem:

**1. Gateway → controller** (what I store). One entry per controller in
`~/Library/Application Support/gateway-kh/data/credentials.json` (`0600`), keyed
`studio-hub-<controller-id>`, never returned by any API. **Already per-controller.** Deleting one
location already removes its credential and nothing else.

The risk here is *operational, not technical*: if you paste the **same token value** into several
locations, then it is one credential in practice whatever the files say, and revoking one revokes
them all. Distinct values per machine keep revocation per-machine with no code change.

**2. Node → controller enrolment** (the Hub session's side). Nodes present a fleet credential to
join. If one value is enrolled fleet-wide, revoking one node means re-enrolling every node. I
cannot verify this from here — the Hub session is the authority, and their note is where that lives.

---

## Options

### A. Least privilege, no new machinery — recommended now

- The gateway uses **each controller's fleet credential**, and each machine's value is distinct.
- **Blast radius of a leak:** read fleet state, submit jobs, stage voice references. It **cannot**
  change settings, identity, storage or maintenance on any controller.
- **Revocation:** `Remove` the location in the gateway (one row), then rotate that machine's fleet
  token on the machine. One machine, not fifty.
- **Cost:** you must generate a distinct credential per machine. Nothing else changes, and no Hub
  work is needed.
- **Why this one:** it is reversible, it shrinks the blast radius today, and it makes the *question*
  of a rotation endpoint smaller rather than urgent.

### B. A rotation endpoint on the Hub — recommended before the count is large

- `POST /api/hub/token/rotate` mints a new local credential, invalidates the old, returns the new
  value exactly once.
- **Revocation becomes:** rotate on the machine, paste the new value into the gateway.
- **Cost:** a small, reviewable Hub change (the Hub session already offered a comparable one for
  `fleet-sync`), plus an *edit credential* flow in the gateway — today you can only add or remove a
  location, not replace its token.
- **Why it matters at 50:** without it, rotating means hand-editing a file on a machine that may be
  in another room or another city, and there is no way to confirm the old value stopped working.

### C. Per-client credentials issued by each controller — only if the fleet stops being single-owner

- Each controller issues a credential bound to the gateway, independently rotatable, with an expiry.
- **Best security model; most work**; overlaps heavily with the allowlist the fleet credential
  already provides.
- **Worth it if** the machines are not all yours, or a controller must be shared with someone else.
  Not worth it for one owner and fifty of their own machines.

### D. Do nothing

- **Honest cost:** no expiry, no revocation short of re-provisioning, and a leak is fleet-wide and
  permanent. At three or four machines this is tolerable. At fifty I would call it the largest
  operational risk in the plan — larger than anything in the gateway, because it is the one failure
  you cannot walk back.

---

## What I would do on my side, regardless of your choice

Three things, all small, and all independent of the decision above:

1. **Ask for the fleet credential in the Fleet page.** Today it says "Hub service token", which
   invites the operator token — over-privileged for no gain. Wording changed.
2. **Detect and warn about the scope of a pasted credential.** `/api/hub/settings` answers 200 to an
   operator token and 403 to a fleet one, so the gateway can say which it was given *before* you
   rely on it. This is the check that turns the table above into something you never have to
   remember. Not built yet — say the word.
3. **Let a credential be replaced, not just added or removed.** Needed for option B, and useful
   without it (a token can change for reasons other than rotation).

Also fixed while checking: seven orphaned credential entries had accumulated from my test
controllers. `clear()` keeps an empty entry on purpose ("this was configured, now cleared"), which
is right for a disabled provider and wrong for a deleted location — removing a location now forgets
its credential.

---

## The decision, in one line

**Choose per-machine credentials, then ask for a rotate endpoint before the fleet is large.**
Everything else is detail. If you want one value across all machines instead, that is a legitimate
choice — but it should be a choice, made knowing that revoking one site then means visiting all of
them.
