# Hardening: what was verified, and what is actually worth doing

**Date:** 2026-09-22
**Status:** one of three items is done; two need the owner (sudo / Tailscale console)

## Verified on this Mac (measured, not assumed)

| Fact | Measurement |
|---|---|
| Gateway listener | `python3.1` pid 60989, `TCP *:8787 (LISTEN)` — every interface |
| Primary LAN address | `192.168.1.239` on **en1** |
| Tailscale address | `100.126.154.54` on **utun1** (`m2-max-32-512.tail4e72cf.ts.net`) |
| macOS application firewall | **disabled** (`State = 0`) |
| Unauthenticated `/v1/models` over LAN | **401** |
| Unauthenticated `/admin/api/status` over LAN | **401** |
| Unauthenticated `/v1/models` over Tailscale | **401** |
| `/health` over LAN | 200 — public by design, and it publishes no secret |

**So the port is exposed but the door is locked.** Every content and admin route
already refuses a keyless caller from any interface; only `/health` answers, and it
carries build/pid/counts. Hardening here is defence in depth, not closing an open
door. **The owner's read is correct: without the API key there is no access**, so
item 1 below is optional.

## Item 0 — the one that actually matters: **FileVault is off**

Discovered while checking whether the passphrase item was worth doing:

| | |
|---|---|
| `fdesetup status` | **FileVault is Off** |
| `tmutil destinationinfo` | **No Time Machine destinations** |

The disk is not encrypted at rest, so anyone holding this Mac or its disk reads —
with no password — the signing key, the gateway key in `.env`, and whatever the
provider credential store holds. That is a far bigger exposure than the port, and
the remedy is a switch, not a script: **System Settings → Privacy & Security →
FileVault → Turn On** (`sudo fdesetup enable` does the same).

Second, there are **no backups at all**. "The packs are free, just re-install" holds
only while the Story sources survive; Time Machine is not configured, so nothing
copies them.

## Item 2 — Tailscale key expiry: **closed, confirmed by the owner**

The owner has confirmed the node's key **no longer expires**. Nothing to do; the
note is kept only so the reasoning is not rediscovered: `tailscale status --json`
publishes no `KeyExpiry`/`Expiry` on `Self`, which is what a non-expiring node key
looks like. If it ever expires, only this Mac needs re-authentication — the video
Mac has its own node key.

## Item 1 — keep the LAN out: optional, needs sudo

The owner has judged this unnecessary because the API key already refuses everyone,
and the measurements above agree. Kept for the day a second device or a future bug
makes defence in depth worth it.

The application firewall is the wrong tool here: it is per-application, and the
gateway *is* the listener, so blocking it blocks the tailnet too. The right rule is
per-interface, which means `pf`.

This session cannot run it — `sudo` requires a password and approval prompts are
disabled — so these are for the owner to run and test. **Read the whole block
first; a wrong interface name can cut Tailscale access to this Mac.**

```bash
# 1. Allow loopback, allow what is addressed to this Mac's tailnet IP, drop the
#    rest on 8787. `quick` means first match wins, so order matters.
#
#    Match the tailnet by ADDRESS, not by interface name. Tailscale takes the next
#    free utunN, so `on utun1` breaks the day it becomes utun3 — and `on utun`
#    would quietly allow every other tunnel too, a corporate VPN included.
sudo tee /etc/pf.anchors/gateway-kh >/dev/null <<'EOF'
pass  in quick on lo0 proto tcp to any port 8787
pass  in quick        proto tcp to 100.126.154.54 port 8787
block drop in quick   proto tcp to any port 8787
EOF

# 2. Load the anchor from the main config (append once, if not already present).
grep -q 'gateway-kh' /etc/pf.conf || printf '\nanchor "gateway-kh"\nload anchor "gateway-kh" from "/etc/pf.anchors/gateway-kh"\n' | sudo tee -a /etc/pf.conf >/dev/null

# 3. Turn pf on and load it.
sudo pfctl -e
sudo pfctl -f /etc/pf.conf
```

Two things to know before you run it: the gateway listens on **IPv4 only**
(`TCP *:8787`), so no IPv6 line is needed today; and if this Mac's tailnet address
ever changes, the second `pass` line has to change with it.

Then prove both directions — the LAN must fail and the tailnet must still work:

```bash
curl -s -o /dev/null -w 'LAN      %{http_code}\n' --max-time 5 http://192.168.1.239:8787/health   # expect a failure/timeout
curl -s -o /dev/null -w 'tailnet  %{http_code}\n' --max-time 5 http://100.126.154.54:8787/health   # expect 200
```

If the tailnet call fails, `pfctl -d` disables pf again and the anchor line in
`/etc/pf.conf` is the thing to fix.

**Belt and braces, in the Tailscale admin console:** an ACL that only your own
user/devices may reach this node on 8787, so a mistakenly shared node cannot reach
the shelf even though it is on the tailnet.

## Item 3 — encrypt the authority backup: optional, and lower value than I first said

`backups/content-authority-20260921-165052/` holds `authority.json`, `signing-key.pem`
and the marker as plain files, plus **every archive the authority references** — that
is what `scripts/backup_content_authority.py` collects.

**An earlier draft of this note called it "the one thing whose loss cannot be
undone", and that was wrong.** The pack content is recoverable: the sources are in
the Story checkout and the re-authoring pipeline is scripted. What is actually lost
is the shelf's **identity and state** — and re-founding it is not free, because a new
authority snapshots the current registry as its baseline, so every existing `-v2`
identity becomes historical and cannot be republished. Recovery would mean a
**third** identity round (`-v3`) for ~140 packs, and the mapping the owner's channels
use would move again. The real cost is a day of re-issuing, not lost content.

So this is optional too, and its value is limited while the backup never leaves this
Mac (no Time Machine is configured). It becomes worth doing the moment a copy does —
which is the only reason to encrypt at all.

`scripts/encrypt_content_authority_backup.sh` seals it into one AES-256 archive
(openssl, `-pbkdf2 -iter 600000`). Verified: it round-trips to byte-identical
files, **refuses a wrong passphrase**, **refuses to overwrite** an existing
archive, verifies the written file decrypts to the same digest before reporting
success, and leaves no plaintext of the backup inside the output.

It reads the passphrase from a prompt and never stores or echoes it, so **run it
yourself in a terminal** — that keeps the secret out of this session:

```bash
cd /build/Developer/gateway-kh/worktrees/handoff-completion
scripts/encrypt_content_authority_backup.sh
```

Store that passphrase somewhere that survives this Mac (password manager, or
paper). **Without it the archive cannot be opened**, so the plaintext backup stays
on disk until you have decrypted the archive once and are satisfied:

```bash
scripts/encrypt_content_authority_backup.sh decrypt backups/content-authority-<stamp>.tar.enc /tmp/restore-check
```

Only then remove the plaintext directory — and only if you want to.
