# Copy-paste prompt for a fresh session

Paste the block below into a new session. It assumes nothing about the previous
conversation — everything it needs is on disk.

---

```
Work in /build/Developer/gateway-kh — the gateway-kh project (one API in
front of every local Studio Hub location and ~18 cloud providers, so Story Studio KH
points at a single URL: http://127.0.0.1:8000). Python/FastAPI gateway plus a macOS
menu-bar app in menubar/. Use .venv/bin/python for everything.

Start by reading these three files, in this order:
  1. AGENTS.md              — the authoritative instruction file for this repo. Its
                              rules override your defaults, especially the sections on
                              "publish what is known", verification, and working rules.
  2. _handoffs/2026-09-20-create-surface-and-tailscale.md
                            — the state of the work: what was built, what is open, the
                              traps, and the owner's decisions.
  3. AUDIT-BACKLOG.md       — the detailed record of each fix and how it was measured.
                              Read its most recent sections before changing the Create
                              surface, retention, or the catalogue's publish rules.

Then tell me what you understand the current state to be, in a few lines, and ask me
what you should work on. Do not start changing code before that.

Ground rules while you work here:
- Verify with the three suites and never claim one you did not run:
  tests/test_e2e.py (isolated, 521 checks), tests/test_fleet_standalone.py (uses my
  real Studio Hub, 50 checks), tests/test_ui.py (drives the real installed app, 128
  checks — run it only when the change touches an app file, since it takes over my
  windows).
- Rebuild with ./menubar/build.sh before claiming the app works. Rebuilding is safe;
  restarting the app interrupts me, so ask first unless I asked for the change.
- Make a gateway change live by rebuilding and killing the gateway BY PID (never
  pkill -f — it kills your own shell), then confirm the PID changed.
- Add a check that fails without your fix, and record each fix in AUDIT-BACKLOG.md
  with how it was measured.
- Tell me plainly when something is unverified or when you could not measure it. I
  would rather hear "I could not verify this" than a confident guess.
```
