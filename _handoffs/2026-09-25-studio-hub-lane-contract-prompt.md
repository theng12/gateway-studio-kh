# Studio Hub → GatewayKH: what a new lane has to state (image · LLM · video)

**A self-contained request. Paste this whole file into the Studio Hub session.**

## Who is asking, and why now

GatewayKH is the single coordinator URL in front of Story Studio KH and every
Studio Hub location. It was changed on 2026-09-24 (release `0.2.23`) so that it
**reads the modality the Hub states for each model** instead of mapping a model to
a lane by its engine name. That change was made because the owner is adding models
now — an image model today, with LLM and video being researched — and the old rule
made anything on a new engine **invisible**: no row, no count, no reason.

Measured against Hub **0.6.54** (two controllers, 2026-09-24):

- 35 packages per controller, all on four known engines; the gateway placed **30
  of 35 by the Hub's own stated modality** and the rest by its engine fallback.
- `hub_modality` values seen on `/api/hub/catalog`: `voice` 197,
  `transcription` 65, `music` 10, `image` 5.

Discovery therefore works for the lanes the gateway already has. What is missing
is the half that matters **when a lane is new**: the gateway can see *that* a model
exists and knows nothing about *how to run it*.

## What already works — please keep these exactly as they are

Read per location, measured live:

- `/api/hub/capabilities` — `controller{controller_id, role, studiohub_version,
  online, ready, drained}`, `site_id`, `authority`, `capacity{by_operation,
  queue_depth, eligible_*/available_*}`, `model_supply[]`, `machines[]`,
  `workers[]`.
- `model_supply[]` — `internal_model_id`, **`operation`**, `eligible_physical_slots_total`,
  `available_physical_slots`, `online/offline/quarantined_machine_count`,
  `machines[]`. This is what weights routing per model, and it works.
- `capacity.by_operation` — `audio.transcription`, `voice.tts`,
  `image.text_to_image`, `music.generation`.
- `workers[]` — `service_id`, `studio_type`, `online`, `ready`, `busy`,
  `machine_quarantined`, `maintenance`, `physical_machine_id`,
  `available_capacity{slots, slots_total}`, `models[{operation,
  internal_model_id}]`, `supported_operations`.
- `/api/hub/catalog` and `/api/hub/models` — **`hub_modality`**, `repo`,
  `family`, `capabilities`, `reference_audio`, `long_form_delivery`,
  `min_unified_memory_gb`, `memory_eligible`, `hub_catalog_error`, `fit`.
- `/api/hub/fleet/overview` — per-machine `reachable`, `health`, `detail`, `chip`,
  `memory_gb`, `available_gb`, `disk_*`, `studios{total,up}`, `modalities`.
- `/api/hub/fleet/nodes` — the node roster with `state`, `detail`, `role`,
  `studios`. Node states `reachable` / `unreachable` / `unauthorized` / `error`
  are drawn verbatim on our Machines tab, and the distinction is load-bearing.
- The job contract we already drive: submit to `/api/hub/jobs` (or
  `/api/hub/transcription/jobs`), poll the batch, fetch
  `…/{batch_id}/items/{item_index}/artifact`, acknowledge, cancel, and discard an
  `uncertain` item.

## The ask, in priority order

### 1. State the lane on `/api/hub/model-packages` — cheap, removes a whole class of failure

Those rows carry `package_id, state, files_state, runtime_state, os_state,
chip_state, dispatch_state, engine, minimum_macos, verification, worker_id, type,
paused*` — and **no modality**. The gateway has to make a *second* read
(`/api/hub/catalog`) and join on `model_id` to learn what each package is. That
read is best-effort by design, so when it fails or is slow, placement falls back
to the engine table — exactly the mechanism that hid a new engine.

**Ask:** add `hub_modality` (and `operation`, see #2) to each `model-packages` row.

### 2. Publish each lane's execution contract — the one that makes new lanes free

`execution_contract` exists today on **voice models only**, and it carries TTS
*presentation* facts (`text_max_characters`, `long_form_strategy`,
`private_section_max_characters`, `qualification_source`, `quality_guardrails`).
It is `null` on every image, music and transcription row we read.

What a lane needs, per model or per operation:

1. **the operation name** it should be asked for, from the vocabulary
   `model_supply.operation` and `capacity.by_operation` already use — and the full
   list when one model supports several (`image.text_to_image` vs an
   image-to-image operation; text-to-video vs image-to-video);
2. **the accepted parameters**, with types and any enum or range. The gateway
   forwards what the caller set and invents nothing, so a parameter the Hub does
   not state is a parameter our callers never get;
3. **the artifact**: media type, and how it comes back (`…/items/{i}/artifact` is
   the shape we drive for every lane; video may need several files or a manifest);
4. **the terminal vocabulary** for the lane — including which state means
   *uncertain* (we turn that into a recovery hold, never a retry) and whether the
   lane supports **discard** at all (transcription does not, and we have to tell
   the operator so);
5. **cancel** semantics for the lane.

**With this, a new lane is a data change on your side and no gateway release at
all.** Without it, every new lane costs a release and a guess — and a guess is what
produced the silent drop we just removed.

### 3. If video is next, this is what we would need stated

- the **operations** (`video.text_to_video`, `video.image_to_video`, …) and which
  each model accepts;
- **duration(s), resolution(s)/aspect ratios, fps**, and whether audio is
  generated with it;
- **expected runtime** — our per-job deadline for a Hub lane is **900 s** today,
  and a lane that legitimately runs longer needs to say so;
- **artifact form**: media type (`video/mp4`?), one file or several;
- whether it needs the deferred/poll contract or the same batch contract as image.

Story Studio's video parser wants a **variants** shape built from those numbers: a
variant whose price fields are absent is dropped, and a duration/resolution we
guessed wrong is a job that fails on use. Stating them is what makes the rows
offerable.

### 4. If an LLM lane is next, this is what we would need stated

- the **request route and body shape** (chat `messages` vs a single prompt), and
  whether it streams;
- the **answer shape** we can map to a clean stop — our consumer *refuses*
  anything that is not a clean `finish_reason` as a truncated response;
- **`context_window_tokens` and `max_output_tokens` per model.** This one matters
  most: where nothing states them, the gateway publishes the owner's curated file
  (a flat `1,000,000` / `128,000`) and labels it `curated` — the label is honest,
  but the number is not the model's own, and Story Studio shows it as if it were.
  Stating them makes the row truthful.

### 5. Small things, measured today

- **A node `detail` is empty.** `/api/hub/fleet/nodes` returns
  `"could not reach the node: "` — nothing after the colon — for every node at
  `studio-1100-m4-16-256`, while the other site says `"All connection
  attempts failed"`. The gateway now quotes that sentence on the Machines tab, so
  an empty one is a dead end for the owner.
- **`workers[].studio_version` is the literal string `"standalone"`** on the
  image, subtitle and music workers; only the voice worker states a version. If it
  is a role, a different key would be clearer — as it stands it cannot be read as
  a version.
- **Keep `no_hub` / `unreachable` / `unauthorized` exactly as they are.** They are
  now drawn with your meaning — TCP refused (Hub not running there) vs packets
  dropped (asleep/off/firewall) vs the node refusing *this controller* — and they
  are the difference between three different fixes for the owner.
- **Keep `hub_modality` on `/api/hub/catalog` and `/api/hub/models`.** It is the
  primary placement signal now.

### 6. One fault we can see from here, in case it is yours

The node `owner-mac-studio-hub` answers **`401 Unauthorized`** on
`/studio/voice/api/transcribe/availability` (and the `/studio/transcription/…`
spelling) for controller `studio-1100-m4-16-256`; 7 model rows carry that as
their catalogue error. The node is otherwise joined, and it is unreachable at the
moment of writing, so this may be a stale reading. From our side it looks like
either a credential mismatch between that controller and that node's voice studio,
or a route that should be fleet-scoped and is operator-scoped.

### 7. Version reporting

The gateway now **declares** the Studio Hub release it is built against:
`/health` → `studio_hub.supported_version = "0.6.60"`, and each controller is
labelled `older` / `newer` on Fleet → Locations. It is a report, never a refusal —
a Hub that auto-updates must not take a fleet down while nobody touched it.

Measured 2026-09-24: **both controllers report `0.6.54`**, while this Mac and all
four SSDs ship **`0.6.60`**. If that is expected, fine; if not, it is a rollout gap
that is now visible. Please keep `capabilities.controller.studiohub_version`
accurate — it is the only version we read.

## What GatewayKH will do with each of these

| If the Hub states… | Then the gateway… |
|---|---|
| `hub_modality` + `operation` on `model-packages` | discovers a lane from one read, with no engine guessing and no dependence on a second call succeeding |
| a lane's execution contract | runs a **new** lane with no gateway release: parameters, artifacts, cancel and discard all come from the Hub's own statement |
| video durations / resolutions / fps | publishes a video catalogue Story Studio accepts, instead of dropping the variant or failing on use |
| LLM context and output limits | publishes the model's **own** numbers instead of a curated flat default |
| a filled node `detail` | tells the owner exactly why a node is offline, instead of a sentence with nothing after the colon |
| an accurate `studiohub_version` | reports drift truthfully and names which Macs are behind the kit |

## What we do on our side, either way

- Anything the Hub states in a lane the gateway has no adapter for is
  **discovered and withheld**: listed on the provider with the reason, never
  offered to Story Studio, and it starts publishing the day an adapter lane
  exists — the same row, no re-discovery.
- Anything the gateway cannot place at all is **reported by engine name** with a
  count and example model ids, so "the Hub has no models" and "I did not
  understand what it sent" are never the same answer.
