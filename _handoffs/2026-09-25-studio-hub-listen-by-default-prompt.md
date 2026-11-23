# GatewayKH → Studio Hub: make "listening on the tailnet" not a per-Mac tick

**A self-contained request. Paste this whole file into the Studio Hub session.**

## What this is about, measured on the owner's fleet today

He is installing a fleet of ~50 Macs. Every Mac he finishes is reachable over
Tailscale, and the gateway still shows it **red**. Measured 2026-09-25 on five
controllers:

| Controller | Tailscale SSH (:22) | Hub (:47873) | Gateway says |
|---|---|---|---|
| studio-1100 | open | **open** | green, 0.6.63 |
| studio-1200 | open | **refused** | red — "Nothing is listening on that address…" |
| studio-1300 | open | **refused** | red |
| studio-1400 | open | **open** | green, 0.6.63 |
| studio-1500 | open | **refused** | red |

So the Machines are up, on the tailnet, and reachable. Their Studio Hub is bound
to `127.0.0.1` only. The gateway's red row is **correct** — it is reported as a
refused socket, which is a different fact from a Mac that is off (that reads as a
timeout), and the row's sentence already tells the owner to go and tick the box.

The problem is the step itself: **installing the app cannot fix it, because the
SSD carries the app and not the setting.** He installed the fleet and the fleet is
still loopback-only. For 50 Macs that is 50 GUI visits, and the gateway is not
allowed to fix it from a distance — correctly, since an absent credential must
never become this Mac's operator token, and a Mac answering only itself is exactly
that state.

## The setting is a plain file, so this is automatable

`~/Library/Application Support/StudioHub Standalone/app-preferences.json`:

```json
{"listenHost":"127.0.0.1,192.168.18.248,100.126.154.54","port":47873,
 "startServicesAtLogin":true,"startAtLogin":true, …}
```

`listenHost` is a comma-separated list, and on this Mac it holds loopback + LAN +
tailnet. It is read by the **app binary** (`Contents/MacOS/StudioHub`), and it is
**not** in `defaults read local.studiohub.standalone` — that domain holds only
window frames and a seen-version marker. So nothing shadows the file.

That is the whole opening: what he needs is not a new mechanism, it is a
**supported way to set that one key without the Settings window**.

## The asks, in the order that helps a 50-Mac rollout

### 1. Default to the tailnet when the Mac is joined to one

If Tailscale is installed, up, and this Mac is a member, include that address in
`listenHost` by default (loopback stays). A fresh install is then reachable with
**no step at all**, which is what "automatic" means here. Prefer the tailnet
address or the tailnet interface, never `0.0.0.0` — the tailnet is already the
trust boundary, and the public/LAN case can stay opt-in.

If a security review says a Hub must not open a socket on its own: make it a
**first-run default with a visible notice** ("reachable to your Tailscale network:
yes/no" on the Connections page), rather than a silent off that costs a site visit
per Mac.

### 2. A supported non-interactive setter, whatever the default becomes

So the kit's `1 - Install the app.command` can do it during install, on a Mac
nobody has to touch:

- a CLI flag (`…/MacOS/StudioHub --set-listen tailscale,lan` or similar), **or**
- an environment variable honoured at launch, **or**
- a documented file write to `app-preferences.json` that (a) is read at launch and
  (b) is not rewritten from cached UI state on start.

Please tell us which of those is supported. We will write the install step against
that one and nothing else — we are not going to have him hand-edit another app's
preferences on 50 machines on the strength of a guess.

### 3. Say the bind over the API, and let it be set there

`GET /api/hub/settings` (operator token) should carry the current `listenHost`,
and `PUT` should accept it. Two things fall out:

- the gateway can **report** "bound to loopback" as a named state instead of
  inferring it from a refused socket, and can offer the fix where the owner is
  looking;
- any Hub that is already reachable can be corrected from a distance — which is
  every Mac after the first, once the first one is right.

It does not solve a fresh loopback-only Mac (nothing can reach it), which is why
asks 1 and 2 matter more.

### 4. The other GUI-only step: joining

If joining a controller is also a tick someone makes on that Mac, the same 50-visit
problem repeats one layer up. A scriptable join (CLI/env, taking controller address
+ Fleet token) would make a whole Mac provisionable by one script — install the
app, set the listen host, join, done.

### 5. Optional, and cheap: one "fleet mode"

A single action that sets what a fleet member actually needs — listen on the
tailnet, start services at login, do not sleep — with the OS-level parts
(`pmset`) either applied or listed for the installer. One switch to document
instead of five.

### 6. Show what it actually bound, not what was ticked

This is the one that cost the owner a trip. He checked every Mac and reported
"they all have Tailscale ticked, but the gateway still shows red — it might be a
bug". On the gateway's side nothing was wrong: each Mac answered SSH and refused
`:47873`, so the Mac was up and its Hub was not listening on that interface; the
red rows went green one by one as he worked, and the gateway tracked every one the
moment it started listening.

What he could not see is the difference between **the preference** and **the
socket**. Please surface the bind itself — the addresses actually listening, from
the running process, on the Settings/Connections page and over
`GET /api/hub/settings`. "Ticked" and "listening" are two facts, and today only the
first is visible; the second is the one that decides whether anything can reach
that Mac. A Hub whose services are stopped, or whose bind failed on one address,
should say so where the tick is, rather than looking identical to a healthy one.

**And it was proved, not guessed, within the hour.** One controller
(`studio-1200`) read red for a whole session while its Settings showed Tailscale
ticked, identical to the Macs that worked. The owner stopped its services and
started them again — nothing else, no setting changed — and it came up green in
9 ms and has stayed up. So the preference was already correct and the *running
process* was still on loopback: the tick had never been applied to a socket.

Two consequences worth your consideration:

- **Apply the change when it is made**, or say so at the moment of ticking
  ("restart services to apply"). A preference that silently does nothing until an
  unrelated restart is indistinguishable from a broken one — which is exactly how
  it was reported to us: *"they all have Tailscale ticked, but it's still red. it
  might possibly be a bug."*
- **Show the live bind**, so the page can never again show a tick that is not a
  socket.

Related, and worth a guard: if `listenHost` holds an address that is **not on this
Mac any more** (a LAN address it left, a tailnet address from before a re-auth),
does the bind fail loudly, fall back, or silently bind the rest? Whichever it is,
the page should say which addresses it got.

## What GatewayKH already does about it, so we are not asking you to fix our half

- A refusal and a silence are reported as different faults with different fixes
  (refused = answering that Mac only; timeout = asleep/off/firewall). Measured
  above: 1200/1300/1500 refuse, and the row says why.
- The gateway never contacts a node and never uses a location's absent credential
  as this Mac's operator token, so "not reachable" stays an honest report rather
  than a workaround that silently half-works.
- Once a controller answers, everything else is automatic: discovery, model
  catalogue, routing, dispatch, release pushes.

## What we will do on our side

- Nothing until you answer #2: no unverified preferences write on anyone's Mac.
- When you do, the install step goes in the kit (ours is
  `repository/dist/setup-kit/`; the controller install is `Studio Hub KH/1 -
  Install the app.command`, which is yours to edit), and we will test it on one
  already-red Mac first — a Mac that is already unreachable is the cheapest place
  to prove it.
- If you expose the settings route (#3), we will add the Fleet-side report and the
  remote fix, and the red row will say "bound to loopback only" as a fact rather
  than as an inference from a refused connection.
