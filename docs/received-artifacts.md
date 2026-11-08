# Received artifacts are temporary

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

The gateway is a **receiver**, not the owner: a node renders an artifact, this
Mac serves it to Story Studio, and **Story Studio keeps the copy that matters**.
Nothing pruned them before `gateway/retention.py`, so `data/artifacts/` grew
unbounded.

Two limits, editable via the API (`/admin/api/settings/retention`,
`/admin/api/artifacts/prune`) — **3 days** or **10 GB**, whichever comes
first. A **pin** (`/admin/api/artifacts/keep`) outranks both:

- **Age** is the norm: three days unfetched by Story Studio and it is pruned.
- **Size** is the burst fallback: long videos can pass the cap in a day, so the
  oldest go first and the newest stay fetchable.

**Nothing younger than an hour is ever removed**, whatever the limits say: a
freshly-written artifact is about to be fetched and a staged upload is being
read, so the newest stay safe by construction, not luck.

**Pruning moves, it does not delete.** A pruned file is moved into
`DATA_DIR/_trash/`; existing `DATA_DIR/trash/` entries remain recognized. The first
stage keeps the existing defaults of **3 days / 10 GiB**, plus saved overrides,
pins, a one-hour grace period, and protection for active and recovery-held inputs.
The second stage moves entries older than **7 days**, or oldest entries as needed
to keep both runtime trash folders together under **20 GiB**, to macOS Trash. A
failed move keeps the source and is reported. Neither ordinary retention nor the
low-disk guard permanently deletes files. Moving to macOS Trash does not free disk
space until it is emptied; macOS owns the 30-day emptying setting. `max_bytes`
counts the live store only; `usage()` (and Settings > Received artifacts) reports
retired files separately. Settings live in
`DATA_DIR/retention.json`, not `.env`, which the menu bar app owns and rewrites.
This runtime policy is the owner's explicit exception to the developer/project
`_trash/` and agent cleanup rules; those rules remain unchanged.
