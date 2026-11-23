# Continue — GatewayKH, from 2026-09-26

**Read this first, then `docs/tasks/HANDOFF.md`, then the master tracker.** This file
exists so a new session can pick up without re-deriving anything. It is a **snapshot with
dates on it** — re-measure before trusting any number here (the commands are below).

---

## 1. What this project is, in three lines

One API (`http://127.0.0.1:8787`) in front of every local Studio Hub location plus 18
cloud providers, so **Story Studio KH points at a single URL** and its model pickers fill.
Story Studio needs **zero code changes** — only `genstudio_url` and an API key. There is
no broker and no daemon: one SQLite store, one process, and `next_action_at` is the whole
scheduling primitive.

**Three projects, three repos, one owner who shuttles messages between them by hand:**

| project | repo | role |
|---|---|---|
| **GatewayKH** (you) | `~/Developer/gateway-kh/repository/` | the coordinator. Branch `main`, clean tree |
| **Studio Hub** | `~/Developer/studio-hub-kh/` | runs the models on each Mac. Installed at `/Applications/Studio Hub.app` |
| **Story Studio** | `~/Developer/story-studio-kh/` | where the owner works. Talks only to you |

**The owner's standing instruction:** *"stop asking me, just land it in and I'll tell studio
to look in master open items."* Write the message as a file in `_handoffs/` and tell him
which project to paste it to. He relays; you do not have a channel.

---

## 2. Live state, measured at handoff `[MEASURED 2026-09-26]`

| | |
|---|---|
| release | **`0.2.44`** — `VERSION`, one line, the only place to bump |
| HEAD | `f1675b5`, `main` = `origin/main`, clean tree |
| running build | `2da713b-202609260646` — `/health` and `/Applications/gateway-kh.app/Contents/Resources/gateway/BUILD` |
| declared Hub | `0.6.65` — `adapters.studio_hub.SUPPORTED_STUDIO_HUB_VERSION` |
| this Mac's Hub | **`0.6.70`** build `30ecef1` |
| controllers | `0.6.65` × 4, `0.6.64` × 1 — one, `a311a0a9f4f7`, is behind |
| drives | all four `KH-STUDIO-*` carry GatewayKH **0.2.44** + `Studio Hub.app` **0.6.70** |
| what Story reads for the local lane | `max_output_tokens: 16384`, `deadline_seconds: 1800.0` |

**Re-measure these rather than trusting the table.** Every command below is run from
`~/Developer/gateway-kh/repository/` — `cd` there first, and stay there for everything in
this project:

```bash
cd ~/Developer/gateway-kh/repository

cat VERSION; git log --oneline -1; git status --porcelain      # release, HEAD, clean tree
curl -s localhost:8787/health | python3 -m json.tool            # running build, declared hub
curl -s localhost:47873/api/version                             # this Mac's Hub (needs it running)

KEY=$(grep '^GATEWAY_API_KEY=' "$HOME/Library/Application Support/gateway-kh/.env" | cut -d= -f2-)
curl -s -H "Authorization: Bearer $KEY" localhost:8787/admin/api/fleet | python3 -m json.tool

for d in /Volumes/KH-STUDIO-*; do echo "$(basename $d): $(ls "$d/Video Generation Kit/GatewayKH/installers/")"; done
```

---

## 3. What the last session did — do not redo it

- **`0.2.39` → `0.2.44`**, all shipped: the per-model **qualification record**
  (`measured` on every catalogue row), the **prose/JSON split** (`by_output_shape`), the
  **stall record** (`hub_progress`), and the Hub's raised ceiling carried end to end.
- **Studio Hub shipped `0.6.70`** with `FALLBACK_MAX_OUTPUT_TOKENS` `4096 → 16384` and
  per-generator deadlines (chat/voice 1800 s; image/transcription/music 900 s; video
  7200 s). **The chain carried it automatically inside ~35 s** — Hub restart → fleet
  refresher → GW-5 → catalogue → Story Studio. **HUB-19 is closed.**
- **The fleet gate ran again — it had not run since `0.2.28`: green, 47 checks in that
  run** (the suite's total follows what the live Hub reports; this checkout has **54
  check sites** now and the suite prints the total it executed), including a real
  transcription end to end. Three defects were found and all three were **in the test**.
- **Two corrections were issued to both teams**, both retractions of our own numbers:
  the chunker was never over-tuned, and a tokens-per-word figure of `1.45` was a blend
  of prose and JSON (prose is **1.21–1.26**, structured **1.50–3.15**).

---

## 4. The goal being worked toward, and what gates it

**30 scene prompts in one request**, so a 30-chapter episode stops costing 30 round trips.

The owner is raising the ceiling (done, `0.6.70`) so a 30-scene pack fits one call —
at 16,384 Story Studio's own derived maximum is **40 scenes**. **One thing still gates it,
and it is not ours:** see **STORY-10**.

---

## 5. Open work, by whose it is

**Ours and unblocked: nothing.** Every open GatewayKH row is blocked, latent, or waiting
on evidence — which is worth saying plainly rather than inventing work.

| row | state |
|---|---|
| **GW-2** | blocked on STORY-1b (their call) |
| **GW-7** | blocked on HUB-15 (their call) |
| **GW-10** | truncation signal; Story shipped its own guard, so this is "any other caller" |
| **GW-11** | **latent, not live** — a merged row takes one location's `lane_contract` while routing to any of six. All six agree today |
| **GW-12** | route-aware ceiling; **worth nothing** at 16,384/1,800, where every Mac qualifies |
| **GW-13 step 2** | **deliberately waiting.** Step 1 (`hub_progress`) now records whether stalls happen here at all — that data decides whether step 2 is worth building |

**Theirs — the two that matter most:**

- **STORY-10 — closed 2026-09-26.** Story's two consumers of
  `max_structured_items_per_request` disagreed about what an unpublished value means:
  `contextual-cues` proceeded, `scene-plan` **threw** — and that is what gated the
  30-scene pack. Both halves are fixed and shipped (v1.4.224, then v1.4.225 `38b87ae3`,
  installed), so an unpublished item limit is no longer a limit of zero. The prompt at
  `_handoffs/2026-09-26-story-studio-scene-pack-item-limit-prompt.md` is the record.
- **HUB-21 / HUB-22 / HUB-18** — three small Studio Hub asks, none blocking.
  **HUB-20 is closed** — Studio Hub 0.6.72 publishes `source_kind`, this gateway 0.2.48
  relays it as `max_output_tokens_source`; Story's consumer half is still theirs.

**Owner:**

- **OWNER-1** — re-join `owner-mac-studio-hub` with the controller's current Fleet token
- **OWNER-3** — 9 machines offline
- **OWNER-4** — controllers before nodes, for the Hub's memory-policy change

**Prompt files written in `_handoffs/` — check with the owner which he has already sent.**
`ls -t _handoffs/2026-09-26-*.md` is the live list, newest first:

| file | to | state |
|---|---|---|
| `2026-09-26-story-studio-scene-pack-item-limit-prompt.md` | Story | **the important one** — this is STORY-10, and it gates the 30-scene pack |
| `2026-09-26-story-studio-ceiling-and-scenes-prompt.md` | Story | the ceiling decision and the tokens-per-word correction |
| `2026-09-26-studio-hub-two-constants-prompt.md` | Hub | **do not send as-is — superseded.** They shipped both constants in `0.6.70` |
| `2026-09-25-MASTER-open-items-tracker.md` | both | the standing file; its **RELAY BLOCK** section is what gets pasted to the Hub |

---

## 6. Two decisions waiting on the owner

1. **Declare Studio Hub `0.6.65` → `0.6.70`?** GW-4's rule is to set the declaration from
   a *running* Hub, so `0.6.70` is defensible — but every controller would then read
   `older` on the Fleet page. That is **true** and a useful prompt to roll out; it is his
   call whether to declare it before or after the controllers move. **One line to change.**
2. **GW-13 step 2 — build the live stall signal?** A caller watching an unfinished job
   still sees `running` until it settles. Needs a write back to our own job row mid-flight
   and `BackendRequest` carries no job id. **Wait for step 1's data.**

---

## 7. Traps that cost real debugging time — the short list

Full list in `docs/tasks/HANDOFF.md`; these are the ones you will hit today.

- **Never return 402.** Story Studio holds a credit lock keyed on the API key that never
  clears without a published balance. A provider out of funds is a settled `failed` job.
- **Kill the gateway by PID, never by pattern.** `pkill -f GatewayKH` kills the shell
  running it and the next `open` adopts the old process. Quit the app → `kill <pid>` →
  `open`. Then confirm the stamp **changed** before measuring anything.
- **A stale handoff is worse than none, because it is trusted.** If `HANDOFF.md`
  disagrees with the code, the code wins — fix the file in the same change. This was
  exercised twice in one session.
- **Read the installed bundle, never the checkout.** `~/Developer/studio-hub-kh/` can lead
  `/Applications/Studio Hub.app`, and the Hub auto-updates.
- **Never name a moving part in a test.** Two failures came from asserting the literal
  `4096` and from naming `whisper-base-mlx`, which this Hub does not have installed — one
  absent model cascaded into five failures and **three passed vacuously** because
  `all(...)` over an empty set is `True`. Read the fixture.
- **`Annotated[int, Query(...)]`, not `limit: int = Query(50, ...)`.** The latter makes the
  *default* a `Query` object, so a direct call reaches SQLite as `type 'Query' is not
  supported` and the route is reachable only over HTTP.
- **A better-sampled average of two different things is worse than a badly-sampled one of
  the right thing.** We made that mistake twice with tokens-per-word.
- **`bool` is an `int` in Python, and `json_extract` returns `1` for a JSON `true`.** Every
  numeric field needs the guard at the layer where the type is still real.
- **Every removal goes to the project's `_trash/`** — never `rm`. `_trash/` itself is his.
- **Versions are patch-only:** `0.2.44 → 0.2.45`. Never `0.3.x`.

---

## 8. How to verify, and how to ship

**Verify — the smallest thing that proves it. Do not take over his screen.**

```bash
# already inside ~/Developer/gateway-kh/repository
.venv/bin/python tests/test_e2e.py                 # 570/570, isolated, uses the mock
.venv/bin/python tests/<the one focused file>.py   # seconds, own temp profile
node --test tests/<file>.js                        # console/HTML changes
GATEWAY_ALLOW_LIVE_GENERATION=1 .venv/bin/python tests/test_fleet_standalone.py   # green, HIS REAL HUB (54 check sites; it prints its own total)
.venv/bin/python tests/test_ui.py                  # release gate, TAKES OVER HIS APP — ask first
```

`test_fleet_standalone.py` **needs Studio Hub running** and spends his machines; it is the
one that proves the fleet path. `test_ui.py` drives the real installed app and interrupts
him — run it only when asked or at a promotion gate.

**Ship:**

```bash
echo "0.2.45" > VERSION          # patch only
./menubar/build.sh               # writes dist/gateway-kh-<VERSION>.dmg, installs to /Applications
git add -A && git commit && git push
# restart: quit app -> kill <pid> -> open /Applications/gateway-kh.app -> check /health build
```

The build stamp is **one commit behind `main` by construction** (`build.sh` stamps
`git rev-parse --short HEAD`, so building before committing always leaves that gap).
That is every release's shape and is not a fault.

**Every release updates `docs/tasks/HANDOFF.md`**, and the setup kit is a snapshot —
after shipping, refresh `dist/setup-kit/` and the four drives, keeping the superseded kit
in each drive's `_trash/superseded-kit-<date>/`.

---

## 9. Authoritative files, in reading order

**All paths below are relative to `~/Developer/gateway-kh/repository/`** — `cd` there
first; everything in this project is run from inside it.

| file | what it is |
|---|---|
| `docs/tasks/HANDOFF.md` | **the orientation.** Standing facts, every release's section, the rules, the traps |
| `_handoffs/2026-09-25-MASTER-open-items-tracker.md` | **every cross-project row.** HUB-* (Hub), STORY-* (Story), GW-* (us), OWNER-*. Read the top box first |
| `AGENTS.md` | the design contract in full. Expensive to rediscover |
| `gateway/qualification.py` | the measurement record: what each model has really carried |
| `gateway/adapters/studio_hub.py` | the Hub lane: deadlines, stall record, chat, artifacts |
| `gateway/worker.py`, `gateway/concurrency.py` | the dispatcher and its three ceilings |

**One habit worth keeping:** the owner says *"just land it in."* Write the finding where
the other project will read it, mark every number `[MEASURED]` / `[STATED]` /
`[ESTIMATE]`, and never describe behaviour that is not deployed — a note that promises
something the running service does not do has cost both projects a day.
