# Deploying the fleet: one controller per location

Notes for the plan as described: **1 machine per location is the controller, the other ~9 are nodes**;
everything is set up once at home; an employee updates apps across town when needed; the gateway is
given **the controller's address and token**.

The plan is the right shape. A controller per site turns 54 addresses into 6 and 54 credentials into
6, which is exactly what this gateway wants. What follows is what will otherwise cost a site visit.

Marked **[measured]** where I verified it here, **[confirm]** where it depends on Studio Hub's
internals, which are still being written.

---

## 1. Hand the gateway the fleet token, not the operator token

**[measured]** Both tokens reach 100% of the routes this gateway calls (`fleet/overview`, `nodes`,
`jobs`, `stats`, `workers`, `capabilities`, `model-packages`). They differ only where the gateway has
no business: `/api/hub/settings`, `/controller`, `/storage`, `/maintenance`, `/token`, `/shutdown`,
`/releases` answer **200 to the operator token and 403 to the fleet token**.

So the fleet token is sufficient and cannot change a controller's settings or shut it down. Paste
that one. And paste a **distinct value per controller** — the same value in six rows is one
credential in practice, whatever the files say.

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
