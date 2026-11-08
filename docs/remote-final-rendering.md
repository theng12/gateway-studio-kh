# Remote final rendering is the owner's per-Mac switch, and two keys must both be turned

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

Owner decision 2026-10-23: use fleet rendering without formal qualification and fix what breaks. A Mac
renders for Story only when **the owner switched it on** (`render_optin.json`, absent = off, edited from
Fleet > a Mac's "…" > Remote Final Rendering…) **and its Studio Hub publishes the evidence** on its
`video.final_render` worker row: `accepted_unqualified: {provenance: "owner-opt-in-unqualified",
accepted_by, accepted_at}` (Hub `RenderExecutor.capability`). That row is **production-shaped**
(`production_enabled`/`production_eligible` true), so the Gateway must never read "production-shaped" as
"qualified" when the evidence is there: such a row is excluded unless the owner switched it on, and a
row whose `accepted_unqualified` is present but is not that evidence is excluded outright
(`render_optin.unqualified_claim`). Hub 0.6.195 never sends the key and is never affected.

- The switch travels on the existing controller relay (`startup_control.set_node_render_authority`, route
  `.../fleet/nodes/{machine}/render-authority`, body `{"accept_unqualified": bool, "requested_by"}`) - no SSH,
  port or shell. **On** is kept only if the Hub took it (a `202 queued` for a Mac reachable only at its next
  check-in counts, and the Mac stays unused until its row shows the evidence); **off** is written here first
  (sending stops at once) and relayed after. The Hub pins one encode (its default h264 medium) unless a body
  names another; the Gateway sends none, and Story only sends what the capabilities `encode` intersection offers.
- `fleet.render_candidates` ranks qualified Macs first, then accepted-unqualified ones, **same network first**
  (`render_optin.classify_machine`: the Mac's own interface list `machine.network` when its Hub sent one, else
  the address its controller dials, against this Mac's own LAN interfaces; Tailscale 100.64.0.0/10 is never
  "ours"; a hostname or missing address is `unknown`, never guessed).
- `RenderService.submit` re-checks that the destination's flag and the snapshot's evidence agree exactly.
- **Nothing is added to Story's responses.** Story's parsers are closed records, so the unqualified label lives in
  the stored capability snapshot and the admin page (`/admin/api/fleet/remote-rendering`), not in `/v1`.
- `_trim_worker` copies `accepted_unqualified` only when the Hub sent it, so a 0.6.195 row and the snapshot made
  from it are byte-identical to before.
- A render on such a Mac is `purpose: "production"` to the Hub and the Hub's receipt echoes the row's
  `qualification_digest`, so `FinalRenderService._hub_job` needed no change.
