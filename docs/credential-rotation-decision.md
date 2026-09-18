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

My recommendation: **use each controller's fleet credential, distinct per machine, and rotate over
the remote channel you already have — SSH over Tailscale — rather than wait for a new Hub endpoint.**
Neither B nor C is required to avoid a site visit, because every machine is already reachable.
Build C when someone else's compute joins the fleet, because that is the one case SSH cannot cover.

---

## What I measured

### The fleet as it actually is

Measured from this Mac over Tailscale, not assumed:

- **58 macOS peers**, of which **25 were online** when I looked. The rest are off, not gone.
- The names encode the layout: `terranash-0307` is **location 03, machine 07**. Reading them:
  locations `00`–`04` hold **10–11 machines each**, and `06` holds one. So this is roughly
  **6 sites of ~10 machines**, not 10 sites of 5.
- **Online/offline is lumpy per site.** Location `00` was fully online; location `02` had **zero**
  machines online. A "rotate the fleet" action is therefore never one uniform operation — it has to
  tolerate whole sites being unreachable, which is an argument for overlap windows and expiry over
  any design that assumes every machine answers.

### The finding that changes the options

**The Hub's API binds to loopback.** `hub-ready.json` publishes `http://127.0.0.1:63628`, and
`app-preferences.json` carries `"listenHost": "127.0.0.1"` — a *setting*, so it can be widened, but
by default no remote Hub is reachable from here, and `tailscale serve` is unconfigured.

**But SSH and Screen Sharing are open on the machines over Tailscale.** Verified against two online
peers: ports **22** and **5900** both answer, and sshd offers
`publickey,password,keyboard-interactive`.

So the remote administration path exists today, and it does **not** go through the Hub API. That
path — not B and not C — is what makes a site visit unnecessary. Two consequences worth stating
plainly:

- Rotating a credential remotely is already possible: reach the machine, rotate, done.
- An SSH key is **more** powerful than any Hub token, not less — it is the whole machine. If this is
  automated, it wants a dedicated key and a Tailscale ACL, never a personal key, and Screen Sharing
  stays break-glass.

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

### A. Least privilege, no new machinery

- The gateway uses **each controller's fleet credential**, and each machine's value is distinct.
- **Blast radius of a leak:** read fleet state, submit jobs, stage voice references. It **cannot**
  change settings, identity, storage or maintenance on any controller.
- **Revocation:** `Remove` the location in the gateway (one row), then rotate that machine's fleet
  token on the machine. One machine, not fifty.
- **Cost:** you must generate a distinct credential per machine. Nothing else changes, and no Hub
  work is needed.
- **Why this one:** it is reversible, it shrinks the blast radius today, and it makes the *question*
  of a rotation endpoint smaller rather than urgent.

### A+. Per-machine credential, rotated over the channel you already have — recommended

- Each controller keeps its **own distinct** fleet credential (already true: each Hub mints one).
- Rotation is a **command run over SSH**, not a new Hub route. Zero Hub work, works today, no visit.
- **Cost:** a dedicated SSH key scoped by Tailscale ACL, plus the *replace credential* flow in the
  gateway.
- **Why this one:** it removes the site visit without asking the Hub team for anything, and it is the
  only option that is available *now*. Its ceiling is that it does not scale to machines you do not
  administer — see C.

### B. A rotation endpoint on the Hub

- `POST /api/hub/token/rotate` mints a new local credential, invalidates the old, returns the new
  value exactly once.
- **Pros:** small and reviewable Hub change; per-machine blast radius; keeps the credential under the
  Hub's own control; and it is the only path that lets you *confirm* the old value stopped working
  from the API side.
- **Cons:** the authentication is self-referential. If the endpoint accepts the credential being
  rotated, then whoever holds a stolen copy can rotate it and lock you out, while a credential you
  have merely *lost* cannot be rotated at all — in both cases the trigger has to come from somewhere
  else. It also adds **no expiry**, so rotation stays an event you must remember to cause.
- **Availability:** unusable remotely until the Hub API is reachable — it is loopback-bound today.
- **Net:** it lowers the cost of a rotation you have already decided to do. It does not remove the
  need to decide, which is the actual burden at six sites.

### C. Per-client credentials issued by each controller — for when the fleet is not all yours

- Each controller **issues** a credential bound to a client (the gateway), scoped, independently
  revocable, with an expiry. Read the direction carefully: the **Studio Hub controller grants it and
  the gateway is the client that receives it** — so C is Hub-side work, not gateway-side work.
- **Pros:** revocation uses the *owner's* credential, so cutting off the gateway never locks you out
  of your own machine; **expiry** means a leaked credential dies by itself and no one has to
  remember a rotation date; per-tenant scoping is exactly what renting someone else's compute needs
  — they hand you a credential instead of their operator token; blast radius is one site.
- **Cons:** a real feature on the Hub (issuance, storage, scope, listing, revocation, expiry), not an
  endpoint; each controller needs a reachable admin surface, which loopback blocks today; an expiry
  bug takes a site down on a timer; and it needs an **overlap window** — with a whole site able to be
  offline, a rotation will land while some machines are unreachable.
- **Net:** C's value is not "better tokens". It is **taking the human out of the loop** and making
  someone else's compute safe to use. Neither matters while the fleet is one owner's and reachable
  by SSH; both matter the moment a stranger's machine joins.

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

**Per-machine credentials, rotated over SSH today; ask the Hub for per-client credentials only when
someone else's compute joins the fleet.** Everything else is detail. Choosing one shared value across
all machines instead is legitimate — but it should be a choice, made knowing that revoking one site
then means visiting all of them.
