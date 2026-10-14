# Deploying the fleet: one controller per location

Notes for the plan as described: **1 machine per location is the controller, the other ~9 are nodes**;
everything is set up once at home; an employee updates apps across town when needed; the gateway is
given **the controller's address and token**.

The plan is the right shape. A controller per site turns 54 addresses into 6 and 54 credentials into
6, which is exactly what this gateway wants. What follows is what will otherwise cost a site visit.

Marked **[measured]** where I verified it here, **[confirm]** where it depends on Studio Hub's
internals, which are still being written.

---

## 0. Before anything else: the controller must be told to answer on the tailnet

**[measured, Studio Hub 0.5.1]** This is the one setting that silently blocks every remote location,
and it is not obvious from either side. Studio Hub binds **loopback only** by default:

```
app-preferences.json   {"listenHost":"127.0.0.1", ...}
lsof                   TCP 127.0.0.1:53039 (LISTEN)
```

Its own code says the same: *"Every build before fleet support was loopback-only, and that stays the
default."* A controller left that way is running perfectly and is **unreachable from here**, and no
token, address or firewall change fixes it.

**On each controller, in Studio Hub:** stop the services → tick the network it should answer on
(the **Tailscale** address, not "Any address on any network") under **"Listening on"** → start them
again. The toggles are read-only while it runs, and the change needs the restart — the UI says so
itself.

**The port is now fixed at `47873`**, so a controller's address is stable:
`http://<tailscale-ip>:47873`. That is a recent fix and it matters for exactly this step — the same
code comments that *"a controller's address in a node's **Join** box, and a node's address in the
controller's registry, both died at the next restart, and nothing repaired them"* when the port was
the operating system's choice.

The gateway now says which of these it is when a location does not answer: a **refused** connection
gets the loopback instruction above, a **timeout** says the address is routed but silent, and a
401/403 says the token was rejected. Three problems, three fixes, and only one of them is a drive.

## 1. Hand the gateway the fleet token, not the operator token

**[measured]** Both tokens reach 100% of the routes this gateway calls (`fleet/overview`, `nodes`,
`jobs`, `stats`, `workers`, `capabilities`, `model-packages`). They differ only where the gateway has
no business: `/api/hub/settings`, `/controller`, `/storage`, `/maintenance`, `/token`, `/shutdown`,
`/releases` answer **200 to the operator token and 403 to the fleet token**.

So the fleet token is sufficient and cannot change a controller's settings or shut it down. Paste
that one. And paste a **distinct value per controller** — the same value in six rows is one
credential in practice, whatever the files say.

**[built]** Because both work everywhere, a wrong-but-valid token is silent. The gateway now checks
`/api/hub/settings` as part of adding or replacing a credential and says which kind was pasted, and
a location's credential can be replaced without re-typing its address (**Token** in the row actions).


## 2. The likeliest cause of a surprise visit is the Mac, not the app

A machine that reboots and does not come back is dark until someone drives there. Each of these is a
one-time setting, easy to forget on machine 47:

- **FileVault.** After a power cut the Mac stops at the login window and nothing starts. Auto-login
  and FileVault are mutually exclusive, so this is a real decision per site: physical security
  against remote recovery.
- **`pmset autorestart 1`** — come back after power failure.
- **`pmset sleep 0` / `disksleep 0`** — an idle node that sleeps shows offline and looks broken.
- **Start at login** for Studio Hub. This Mac already has `"startAtLogin": true`.
- **A smart plug per site** is the cheapest insurance in this whole plan: it makes a hard power cycle
  a remote action instead of a drive.

## 3. Tailscale key expiry is a time bomb

**[measured]** All machines are on one tailnet already (58 macOS peers), MagicDNS is on, and `serve`
is unconfigured. But node keys **expire by default (180 days)**; when one does, the machine silently
leaves the tailnet and only a visit restores it. Mark fleet nodes non-expiring, or use pre-approved
auth keys.

Also give an employee a **tagged node with an ACL**, not their personal tailnet account, so
withdrawing access later is one action at the network layer rather than a tour of six sites.

## 4. Same-site traffic should not leave the building

**[confirm]** If a node reports its artifacts to its controller over the controller's *Tailscale*
address, ten nodes' uploads hairpin through the tailnet — and through DERP if the site cannot get a
direct path. Prefer the **LAN address for node → controller** and reserve Tailscale for
**gateway → controller**. Worth checking per site that the gateway's path is direct rather than
relayed; for video artifacts that difference is large.

## 5. The recurring physical cost is weights, not the app

The app is small; weights are tens of GB × ~10 machines × 6 sites. "Update the app" is a download.
"Add a model" is the expensive one, and it is the reason an employee ends up driving. Decide now
which of these it is:

- a drive carried to each site, or
- each site downloading independently (needs the bandwidth), or
- the controller distributing to its nodes over the LAN.

Keep every site's model set identical, or the same model becomes available in some places and not
others, and the picker stops being predictable.

## 6. Configure nodes from the controller

**[confirm]** If each node holds its own configuration, there are 54 config surfaces and they drift
silently between visits. If the controller pushes configuration, there are 6. This is the single
biggest efficiency lever in the plan, and it is a Studio Hub design question — worth settling with
that session before deploying, because it is painful to retrofit.

## 7. One row per model, not one per location

**[measured]** Model ids are namespaced `provider::model` (`gateway/catalog.py:78`), and this Mac
already publishes `studio-hub-local::AITRADER/FLUX2-klein-4B-mlx-4bit` with
`provider_name: "Studio Hub (this Mac)"`. Each location is its own provider
(`studio-hub-<controller-id>`).

So six locations publish **six rows with the same `display_name`**, differing only by
`provider_name`. Story Studio's picker would show the same model six times and turn "which model?"
into "which building?" — and a saved preset would break when its site went offline.

The namespacing exists for a real reason: `flux-pro` from fal and from BFL are different products.
Six copies of *your* hub are the same product at six addresses. So a location should be a **route**,
not a separate model:

- **one catalogue row per model**, and
- the gateway dispatches to any site with an eligible idle machine.

Pinning a job to a particular site is then a separate "prefer this location" feature, not six model
rows. Routing also needs per-machine load data, which is the same data behind the "how many machines
are online, and which generated how many jobs" view — the two features are one feature.

**[confirm]** Whether a controller can report per-node eligibility and current load is the question
that decides how good this routing can be.

## 8. The controller is a single point of failure for its whole site

Ten machines go unreachable when their controller dies. Because the role is a **label** rather than a
capability, a node can be promoted — but that re-points the gateway, so keep a record of each site's
intended controller. Prefer the most reliable, always-on, least-loaded machine; and **[confirm]**
whether a controller that is also a busy worker loses API responsiveness, because the gateway's
discovery would then time out on a site that is merely busy rather than broken.

**Superseded in 0.2.62 / Hub 0.6.92: promotion is now a first-class action, not a re-point by
hand.** *Promote to controller* on the node mints that Mac's own group credential, and *Hand over
to…* on the old controller pushes the new address and token to every node in its roster over the
channel it already has — so the cluster follows a promotion with **nobody touching a node**. The
old controller retires itself (it keeps its history and stops accepting dispatch); the operator
then uses a *replace address* action so routing follows without losing per-model enablement. **If
the old controller is already dead there is no channel to the nodes**, and that case is the
runbook plus SSH over Tailscale — see
[credential-rotation-decision.md](credential-rotation-decision.md). The Hub's own UI says which
case applies.

## 9. What happens when a job fails

A failed job is retried on a **different machine**, up to three attempts, and is then declared
failed to Story Studio with a reason naming every machine it was tried on. A retry happens only
when the previous attempt is **known** to have failed — an upstream that accepted the work and
went silent is never re-run automatically. The full policy, including what an operator sees on the
job envelope, is [job-retry-policy.md](job-retry-policy.md).

## 10. Removing a node is permanent — and it makes that node's finished jobs unretrievable

Two things an operator must know before pressing **Remove**, both recorded here because neither is
visible in the UI (M17, and the owner's decision B):

- **A removal is an eviction, not a bookmark.** The controller drops the machine's roster row *and*
  revokes the credential it issued that machine, and it records the removal by name and date. The
  removed Mac is refused on every later join attempt with *"this controller removed this Mac on
  <date>; re-admit it from Connections to join again"* — it cannot re-add itself, which is the point:
  the owner removes Macs to **sell** them. Re-admitting is a deliberate action on the controller, and
  only then may that id join again (it is issued a fresh credential).
- **Its finished jobs stop being fetchable through the controller.** Jobs a removed node ran are still
  listed as done, but the controller can no longer reach that machine to fetch the artifact, so a
  request for one answers **503**. If an artifact on a Mac you are about to remove matters, fetch it
  before the removal — this is the caveat the remove confirmation does not yet spell out.

The `_orchestra` audit that produced this section is
`_orchestra/AUDIT-gateway-studiohub-20260928.md`, finding M17.
# Scoped model-sharing destinations (0.2.179)

The normal `POST /admin/api/fleet/model-installs` accepts an optional `targets`
array of exact `location_id/machine` keys alongside `package_id`. Omitting it
keeps the existing fleet-wide behavior. A present scope must be nonempty,
unique, canonical, and known to the current fleet snapshot; malformed, null,
or unknown values fail before a plan is admitted.

Only those machines become destinations. Verified cached peers outside the
scope can still lend within their network, using the existing strict LAN
protocol, source leases, capacity, and retry rules. The scope persists through
Gateway restart. Existing memory, offline, and model-off decisions remain in
effect; a returning offline machine needs fresh eligibility and a later safe
continuation. This does not add controls to the individual download route.
