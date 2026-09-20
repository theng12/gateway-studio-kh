# Handoff — multiple artifacts, the Create surface, and Tailscale access

Written 2026-09-20 at the end of a long session, for a **fresh session with no
memory of it**. Read `AGENTS.md` first — it is the authoritative instruction file for
this repository and is expected to be at the top of your context already. This
document is the *state*, not the rules.

## The repository

`/build/Developer/gateway-kh` — one API in front of every local Studio
Hub location and ~18 cloud providers, so **Story Studio KH points at a single URL**
(`http://127.0.0.1:8000`). Python/FastAPI gateway + a macOS menu-bar app (AppKit) in
`menubar/`. Python lives in `.venv` — use `.venv/bin/python` for everything.

Live state at handoff: gateway pid reported by `/health`, build
`214914c-202609201056`, **125 models offered**, gateway bound to `127.0.0.1`.

## What this session built

| Commit | What |
|---|---|
| `8d3069d` | **A job holds every artifact a provider returns.** Suno V6's second clip and `n>1` images were parsed then thrown away. `BackendResult` carries a list now; the worker writes each; `jobs.artifacts_json` stores them behind `store.artifacts_of()`; the envelope publishes `outputs: [{asset_id, url}]`; `/v1/jobs/{id}/outputs/{asset_id}` reaches any of them. Also: `get_music_output` used to accept an `asset_id` and ignore it, so every index answered track 0. |
| `ee2dc34` | **The Keep pin.** `retention.keep()` outranks both prune limits; pins live in their own `kept.json`; `pinned_files` is reported apart from `protected_files`. |
| `e487f1d` | **Non-chat rows withheld** (`chat_can_serve`) and **free lanes marked** (`data_use`, `free_tier`). NVIDIA NIM publishes embeddings, safety classifiers, a reward model, OCR parsers and translators as `modality: chat`; 34 rows across six providers were tickable and would have failed on use. |
| `feb8db9` | **Button contrast** in the console: enabled and disabled were one drawing at two opacities. |
| `a5ffa68` | **Console Create surface** at `#create` — six lanes, poll, preview, Save, Keep. |
| `9430bad` | **Native Create pages** in the app: `Section("Create")` with the six lanes, plus the View menu rebuilt from the section list. |
| `75ddb9e` | **Tailscale**: the bind host is honoured and the console can identify itself. |
| `ab6559f` | Decisions closed (below) and how to reach this gateway from another device. |

`AGENTS.md` and `AUDIT-BACKLOG.md` carry the reasoning and the evidence; the backlog
is the detailed record and is worth reading before changing anything in the areas
above.

## How work is done here

1. **Verify with the three suites**, and never claim a suite you did not run:
   - `.venv/bin/python tests/test_e2e.py` — isolated, mock upstream, **521 checks**.
   - `.venv/bin/python tests/test_fleet_standalone.py` — uses the **real Studio Hub**,
     **50 checks**. Run it knowingly.
   - `.venv/bin/python tests/test_ui.py` — drives the **real installed app** through
     Accessibility, **128 checks**. It takes over the owner's windows. Run it when the
     change touches an app file; **never for a change that touches no app file.**
2. **Rebuild before claiming the app works**: `./menubar/build.sh` installs to
   `/Applications/gateway-kh.app`. Rebuilding is safe; **restarting the app is the
   intrusive part**.
3. **To make a gateway change live**: rebuild, kill the gateway **by PID**
   (`pkill -f` kills your own shell), let the app respawn it, then confirm the PID
   changed. The console page is read from disk per request, so HTML edits need only a
   reload. Check `curl /health` for the build stamp.
4. Record each fix in `AUDIT-BACKLOG.md` with how it was measured, and add a check
   that **fails without it** where you can.

Traps learned this session:

- **The build stamp can name a commit that does not contain the code**: it is written
  at build time from HEAD, so a build from uncommitted changes records the parent.
  Commit, then rebuild, if the stamp matters.
- **A running app stamps a respawned gateway with its own launch-time build**, so
  after a rebuild the gateway serves new code under the old label. It errs safe.
- **`screencapture` is refused** (no Screen Recording permission), so the app cannot
  be screenshotted. Headless Chrome works for the console:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=old --disable-gpu --no-sandbox --user-data-dir=/tmp/cs --screenshot=/tmp/x.png --window-size=1200,900 <url>`.
  **`--headless=new` hangs**; `--headless=old` works.
- **If `test_fleet_standalone.py` fails the two Hub-transcription checks**, restart
  Studio Hub and re-run — do not re-run the suite hoping. The Hub can answer
  `/api/health` in 1.7 ms while its worker holds 38 MB RSS and has loaded no model.
- **Never bind `GATEWAY_HOST` to the Tailscale address alone** — loopback goes dark
  and Story Studio's `127.0.0.1:8000` breaks. Use `0.0.0.0`.

## Open, unverified, or waiting on the owner

- **The native Create pages have never been *looked at*.** They are verified
  structurally (UI 128/128) but `screencapture` is blocked, so no one has seen them.
  The owner was asked to click through Create → Audio, Subtitle, Image, Music, Video,
  Chat. *The console's Create page has* been rendered and looked at.
- **Only two lanes have been exercised on a real generation**: chat (Groq, free) and
  Subtitle (local Hub). Image, music, video and TTS are built and their endpoints are
  tested, but no paid lane has been run end to end — deliberately, to avoid spending.
- **NVIDIA NIM's prompt-retention terms are unverified.** It is published as
  `data_use: "unverified"`, which is honest, not safe.
- **Kie V6 end-to-end** (~12 credits): the owner will run it himself; fix it if it
  fails. Do not spend the credits.
- **OpenCode Zen: rejected and closed. Do not raise it again.**

## Owner decisions recorded this session

- OpenCode Zen / Go — **not accepted** (see `AUDIT-BACKLOG.md` for the measurement).
- Kie V6 — **deferred to him**, fix on failure.
- Free providers — NVIDIA NIM, OpenRouter, Token Harbor, Google's free tier and Groq
  chat were enabled and are the intended free capacity. 26 free rows are offered.
- The generators were to match **Studio Hub's Create workspaces** in look and flow;
  its *implemented* pages are only Audio, Subtitle and Image (Music, Video and Chat
  are stubs there), so those three follow the same grammar rather than an existing page.

## Suggested first move in a fresh session

Ask the owner what he wants next rather than picking for him, but the obvious
candidates are: his verdict on the native Create pages, proving a paid lane end to
end, or the next feature he names. Read `AUDIT-BACKLOG.md`'s most recent sections
before touching the Create surface, retention, or the catalogue's publish rules.
