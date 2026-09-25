# What's new in gateway-kh

Newest first. This is the same list the **What's New** link in the app shows.

---

## Studio Hub now tells the gateway how to run each model — and the gateway reads it

Studio Hub 0.6.61 answers the lane-contract request this gateway sent. Three of
those answers are now in use:

- **The Hub states each model's lane on the package row itself** (`modality`), so
  placing a model no longer depends on a second call that could fail. The
  per-model statement and the engine table remain as fallbacks, in that order.
- **Each model carries its own dispatch contract** — operation, accepted
  parameters, artifact, states, cancel — and the gateway reads two things from it
  today: **the deadline the Hub enforces** (instead of this gateway's own flat
  900 s, so a lane the Hub lets run longer is not cut short here) and **whether
  the lane can discard**. That second one fixes a real button: a Studio Hub *chat*
  hold now offers **Close this hold…**, because the Hub's own contract says that
  lane has no discard route. Before, it offered a discard that would have been
  refused.
- **A Hub chat model's limits are published as the Hub's own numbers.** It states
  the context window it read from the checkpoint and the max output its worker
  falls back to, each with the sentence saying which is which — so a Hub LLM row
  no longer wears your curated flat `1,000,000` as if it were the model's. The
  sentence travels in a new `*_source_detail` field beside the value.

A package the Hub itself cannot place is now reported **in the Hub's own words**
(the sentence naming the engine allowlist a person has to edit), rather than this
gateway's generic one.

**Still withheld:** Hub LLMs. The lane contract says chat dispatches to
`/v1/chat/completions`, but on 0.6.61 that route exists only *inside* the chat
worker — the Hub's own `/api/hub/chat/completions` forwards to this Mac's worker,
and nothing creates a schedulable chat batch. So there is no fleet route for the
gateway to submit to yet: chat models are discovered, listed, and withheld with
the reason. That question is with the Hub session.

## The gateway says which Studio Hub it is built for — and keeps working

Studio Hub moves on its own schedule. It updates itself, and this fleet has run
`0.6.18`, `0.6.23`, `0.6.29`, `0.6.31`, `0.6.54` and `0.6.60` in a week. So the
pairing is now **declared** instead of assumed: this build targets **Studio Hub
0.6.60**, says so on `/health`, and shows it beside each controller on
Fleet → Locations — `0.6.60`, `0.6.54 · older`, `0.6.72 · newer`.

**Nothing is refused on a mismatch.** A Hub that updated itself must never take
your fleet down while nobody touched it. A drift is one line in the Fleet issues
under "informational — nothing to fix", naming both versions.

## A model you add on the Hub arrives here — even on an engine this build has never seen

The gateway used to work out what a model *was* from its **engine name**, through
a short list in its own code — `image-mflux`, `voice-mlx`, `music-mlx`,
`subtitle-mlx`. A model on any other engine was skipped **in silence**: no row, no
count, no reason. Your next image model would have landed on the Hub and simply
not appeared, and an LLM or video lane would have been invisible by construction.

- **The Hub's own word is what places a model now.** Every model it lists carries
  `hub_modality`, and that is read — the engine list is only the fallback for a
  Hub that says nothing.
- **A lane this build can already run works immediately.** A new image, voice,
  music or transcription engine needs no gateway release at all.
- **A lane it cannot run is listed, not hidden.** A video or LLM model is
  discovered, shown in Providers with a **withheld** badge and the reason, and
  never offered to Story Studio — so you can see what the Hub has and what this
  build would need to serve it.
- **Anything nothing can place is reported by engine name** ("2 packages on
  engine `fleet-mock`"), so "no models" can never be the answer while the Hub is
  listing models.

## A dead node now says why, once

The Fleet page could see *that* machines were down and never *why*, and it said the
same thing many times over. Both are fixed.

- **The reason travels with the machine.** Your controller already tells the gateway
  whether a node is `unreachable` or has `no_hub`, and quotes its own sentence about
  the failed connection. That now appears on the machine's row (hover its State) and
  in the issue that names it, instead of eight identical "offline" lines.
- **One fault, one row.** A dead machine used to produce six warnings — offline,
  health, and one per studio — beside a model warning for every model it could not
  check. The same fleet that read **148 things to look at** now reads the nine that
  are actually wrong: each machine once, its workers counted once, and its
  unreachable models left to the machine that explains them. A model fault that is
  *not* just the machine being off (a `401` from a node that answered, say) is still
  reported on its own.
- **The Machines table shows what is free now.** It read "Apple M4 · 17.18 GB ·
  118.11 GB free" — the disk labelled and the memory not. Memory now reads
  `17.18 GB (7.14 GB free)`, which is the number the Hub itself uses to decide
  whether a Mac can take the work.

## A stuck job can be cleared again

A job that was in flight when the gateway restarted was left on "manual recovery
required" with **no action anywhere in the app**: the discard needed identifiers the
restart sweep never recorded, and no cloud provider has a discard route to call at
all. Those jobs were stuck until someone edited the database.

- **The restart sweep now records the upstream attempt**, so a Studio Hub job gets
  its Discard button back and the Hub is asked to release the machine.
- **A job on a cloud lane gets "Close this hold…"** instead. Nothing is sent — there
  is nobody to send it to — and the record says exactly that, so it can never be
  misread later as "the provider abandoned it".
- **The gateway publishes which action applies**, instead of the app guessing from
  the job's context, so a button is only ever offered when pressing it works.

## One model, one answer about whether it can run

A model offered by several locations is published as **one** row — and that row was
mixing two locations' answers. With this Mac's Studio Hub down and both controllers
up, a model read `locked: false` beside `availability: "unavailable"`: the gateway's
answer and the catalogue's, disagreeing in the same payload. Both spellings of that
one fact are now written together.

## The Models tile is a real fraction

Fleet → Overview showed **"175/102 local models available"** — more models available
than models. The numerator counted one entry per machine and the denominator one per
model. Both halves now count models, so the tile reads `N of M` and means it.

## Adding a location no longer replaces one

Every Studio Hub that has never been given a site id answers the **same** one —
`local-site`. The gateway had started identifying a controller by that value, so
adding a second such controller matched the first row and overwrote it: the roster
looked like it could only hold so many locations.

Add as many controllers as you like now. A Hub with its own site id keeps it, and a
Hub still answering the default is told apart by its per-machine id instead.

## The gateway stops offering a knob the model won't take

The last release made a refused temperature survive; this one stops asking for it.

- **A model that accepts only its own default temperature is no longer offered a
  temperature control.** Story Studio sends `0.7` whenever the catalogue offers the
  control, so OpenAI's reasoning models (`gpt-6-luna`, the `gpt-5`/`gpt-6` family,
  `o1`/`o3`/`o4`) were answering `400`. They now run at their own default instead.
- **Scoped to the API that refuses it.** A proxy that accepts `0.7` — Experiential
  Labs' `gpt-5.6-luna` did — keeps the control, and any provider that publishes its
  own parameter list is always believed. The one-retry-without-temperature from
  0.2.19 stays as the safety net for a family this table has not learned yet.

## A provider's “no” is a failure, not a mystery

Two things made a refused chat call look like a lost one.

- **A definite `4xx` from any provider is the provider's own refusal** — nothing ran,
  nothing was charged. It now settles as an ordinary failed job instead of a durable
  *unknown outcome* hold you have to reconcile by hand. Only a `5xx`, or a connection
  that never got an answer, stays ambiguous.
- **A model that accepts only its default temperature no longer fails.** Newer
  reasoning models (OpenAI's `gpt-6-luna`, for one) answer
  `400 "temperature does not support 0.7 with this model"`. Because a `400` ran
  nothing, the gateway re-asks once without the temperature and records that it did
  (`params_dropped`) — so the call works, and you can see the knob was dropped rather
  than believe `0.7` was used.

## A release is never pushed backwards

A controller that already holds a **newer** build than the one being published is
now refused before anything is sent.

- **The gateway asks each controller what it holds first** and stops a downgrade with
  `RELEASE_DOWNGRADE`, naming both versions. Studio Hub's own updater has no ordering —
  it takes any build that differs — so this gateway is the only place a rollback can
  be caught.
- **The publish panel warns before the push**: if a controller holds a newer version,
  it lists them and asks, with **Publish anyway** for a deliberate rollback. A version
  with no numeric order (a date, a suffix) is never treated as a downgrade.
- **Fleet → Locations → Release shows each controller's trusted publisher** (Hub
  0.6.51+): the fingerprint it has installed for app releases. It reads “this gateway”
  when it matches, and says plainly when none is installed yet — the state that makes a
  push answer 409. “It trusts nobody” and “this Hub did not say” stay separate sentences.

## The default port is now 8787

A fresh install listens on **8787** — the port this Mac's working instance already
uses. It was 8000, which is Story Studio's *own* default and the most contested dev
port, so "is this pointed at the right thing?" was never answerable from the number
alone.

- **Existing installs are unaffected.** Their `.env` and Settings keep whatever port
  they already set; only a machine that has never chosen one gets 8787.
- The port stays editable in Settings, and `README.md`'s port section now says 8787.
- Point Story Studio's Base URL at `http://<host>:8787` (Settings → Copy).

## Publish Studio Hub app releases from the gateway

GatewayKH signs a Studio Hub app release with its own Ed25519 **publisher** key and
offers it to every configured controller, which verifies the signature against the
key its owner installed there and countersigns. The node then pins the controller's
countersignature, so trust chains from this gateway to the controller to the node.

- **Settings shows the publisher identity** — the fingerprint you install on a
  controller (with `POST /api/hub/release/trust`) so that controller accepts
  releases pushed from here, plus the public key. The private key stays in the
  gateway's data directory, is written `0600`, and is never returned by an API.
- **Publish a release** from Fleet → Locations: choose the `.dmg` exactly as
  built, give the version and build, and the gateway signs a descriptor — sorted
  keys, no whitespace, UTF-8, the sha256 *inside* the signed bytes — and pushes it
  to each controller with that controller's Fleet token.
- **The report is what the controller holds**, read back from
  `GET /api/hub/release`, never "the request was accepted". Each controller shows
  the release it holds, the last push outcome, and the controller's refusal
  reason **verbatim**. A controller that has not had the key installed yet refuses
  with its own sentence, and the gateway names the fingerprint to install — the one
  action that Mac needs.
- Nothing is re-signed or rewritten after signing, and the artifact is never
  repackaged.

## Fleet: "the site is down" no longer reads as "no such model"

With every controller switched off, submitting a fleet model answered
`404 Model "…" is not available on this gateway` — while the model sat in the
picker and every location offering it was simply offline. That sends you looking
for a catalogue problem instead of a network one.

- A fleet model that **is** offered but has no live site now answers **503**,
  naming the condition: *"No location offering this model can take work right now
  — 2 locations offline."* A genuinely unknown id still answers 404.
- A submit whose **connection is refused** — a controller that is off — is now
  recognised as having delivered **nothing**. It fails cleanly and the gateway
  tries the next location offering that model, instead of holding the job as an
  unknown outcome you have to reconcile by hand. A read timeout *after* the
  request left is still treated as ambiguous, because it may have arrived.
- **A definite refusal is never a hold either.** Any 4xx the Hub answers is its
  own decision, so nothing was accepted: a `409 "This Hub is in agent mode"`
  used to become a durable unknown-outcome hold. Only a 5xx — which may have been
  accepted before it failed — keeps that careful path.

## Fleet: the Hub explains its own activity gaps

Hub 0.6.49 adds a machine-readable reason beside the fleet-activity limitation it
already published — and fixes the gap underneath it: only the Voice worker answered
`/api/fleet/activity`, so Image, Subtitle and Music were recorded as "no reporter"
and any machine running more than one studio read as partially unavailable, with
its state unknown.

- Every machine now carries `limitation_code` (`reporter_unavailable`,
  `partial_read`, `stale_snapshot`, `waiting_for_worker`, or null), a short
  `limitation_reason`, when the standing condition began, and which readings are
  genuinely missing. The Machines tab draws the phrase and the code, and the Issues
  list names them beside the Hub's own sentence, which is kept verbatim.
- **A machine that reports fully carries null throughout**, and a null median under
  a null code is "not measured yet" — a different fact from "could not be
  measured". The activity warnings on this Mac's own machine are gone as a result:
  it now reads `ready`, 15 completed jobs, no limitation.

## Fleet: an agent-mode Hub is never sent customer work

A live probe caught it: one image job and one TTS job both failed with
`409 "This Hub is in agent mode; submit customer jobs to a controller."` The
gateway had routed them to **this Mac's own Hub**, which is an agent — it runs
workers for a controller, and the Hub refuses customer submissions outright. A
*drained* Hub is the same kind of "do not send me work".

Both are now excluded from routing and from the catalogue's "can any location take
this?" test, so a model only an agent offers reads as unavailable instead of being
published and then refused on use. Standalone Hubs and controllers are unaffected,
and a controller that is merely starting up is not treated as refusing.

The probe also exposed the configuration gap behind it: the fleet's models were
enabled on this Mac's **agent** provider and on neither controller. The controller
that owns this Mac can already serve them — all available and cached across four
machines — so the image and TTS lanes are enabled on the controllers.

**A voice still pins its owner — unless that owner cannot take the work.** A voice
belongs to the location that holds its reference clip, which is why voice work is
never re-routed. But when the owner is an agent, pinning there is a dead end: the
TTS job resolved to "model not available" because the only candidate was the agent
just excluded. The pin now holds only while the owner accepts customer work;
otherwise the job routes to a controller and the reference clip is staged there,
the same path a voice owned anywhere else already uses.

## Fleet: the Hub's own answers, and Retry

Studio Hub 0.6.48 publishes what the gateway had been approximating. Where a
location is on 0.6.48, the Fleet page now uses it:

- **The Hub's own per-model capacity** drives placement, instead of the gateway's
  reduction of the same rows. A Hub that publishes nothing still gets the
  reduction, so a 0.6.47 location keeps working exactly as it did.
- **Workers name the models they serve**, so a model's machines section can say
  which worker actually has it rather than inferring from the studio.
- **Per-item failure text.** A batch's `error` is a count; the Hub now sends
  `errors[]` — item, code and the Hub's own sentence — so the Jobs tab says which
  item failed and why, and offers **Retry** on it. The Hub refuses a retry of
  anything that is not in an error state, and its refusal is shown in its own
  words (a cancelled batch, or an uncertain item that must be discarded first).
- **Model health with a reason.** The Hub's per-(machine, model) verdict —
  `ready`, `download_required`, `runtime_missing`, `unsupported_os`,
  `unsupported_chip`, `insufficient_memory`, `quarantined` — is read verbatim.
  `ready` and `download_required` are normal, so only the states that need someone
  are reported on the Overview, one line per state.
- **One canonical model id.** A model row, a capacity row, a health row and a
  batch now carry the same `model_id`, so the joins no longer rely on the repo
  happening to match.

Also fixed: the Cancel confirmation reported "0 queued cancelled, 0 signalled, 0
pending" on a real cancellation — the adapter nests the Hub's answer one level
down and the counts were read from the wrong level.

## Fleet: only what is actionable, and one line per condition

The Overview's issue list repeated the same Hub condition once per machine —
"Direct activity partially unavailable" appeared four times for one controller —
and showed informational notes under a heading that said *Needs attention*, so a
Hub measurement gap read as something broken.

It is now **Issues**, split into *"N to look at"* (errors and warnings) and
*"N informational — nothing to fix"*. A condition the Hub repeats across machines
is one row with a count — "3 machines: Direct activity partially unavailable" —
rather than one row per machine, and the note that some machines "report no state"
says plainly that the Hub counts them as neither online nor offline. A fault that
names one machine (offline, unhealthy) still gets its own row.

## Fleet: placement weights the Macs that can run the model

Studio Hub 0.6.47 publishes no per-model capacity — `capabilities.model_supply`
comes back empty on every location — so placement had fallen back to counting
every machine at a site. A site with ten 8 GB image Macs could out-weigh one with
two 24 GB Macs for a model only the big ones can run.

The gateway now reduces the Hub's **own** per-machine rows instead:
`/api/hub/models` states, for every machine and model, whether that machine
clears the model's memory floor (`memory_eligible`) and whether it is ready with a
free slot (`available`). Counting those stated booleans is the same number
`model_supply` describes, so a four-machine controller reports *4 eligible, 3
available* for a model one of its Macs cannot hold, and the placement weight
follows. A Hub that does publish `model_supply` is still believed over the
reduction, and a machine that states neither boolean is never counted as "cannot".

## Fleet: stop a running batch

The **Jobs** tab can ask a location's Hub to stop a batch it is running.

- **Cancel, per running batch.** The button asks the controller that holds the
  batch, using the address and Fleet token already on file — never this Mac's Hub
  by default. The answer is the Hub's own: how many queued items it cancelled, how
  many workers it signalled, and how many are still pending, because a worker that
  has already taken the work cannot be promised to stop. The button goes away once
  a stop has been asked for, rather than offering a request already made.
- **The Hub's real status, not a guess.** A batch's `error` was read as a sentence
  when the Hub publishes a *count*, so a failed batch could read as "0". It now
  says "2 failed", and the Hub's own note about why it is holding a batch
  (`governor_note`) plus its stall and retry timings are shown beside it.
- **Uncertain items are named.** A batch the Hub could not confirm is reported as
  "N uncertain" — work it will not dispatch again on its own.
- Transcription batches are addressed on the Hub's transcription route, which is
  a different path from every other modality; the modality travels with the row so
  the cancel cannot land on the wrong one.

## Fleet: one page per question

The **Fleet** page is now five tabs instead of one long scroll, and every table
in it sorts by clicking a column header.

- **Overview** — the numbers at a glance, and an **Issues** list that names what
  is wrong and where to fix it: errors first, then warnings, each row saying
  which location and machine, the gateway's own sentence, and which tab to open.
- **Locations** — the controller table with per-location model counts
  (offered / discovered), and the add-a-location form. Every action now reports
  in a **confirmation banner** under the tabs — green when it worked, orange or
  red when it was refused — instead of a line at the bottom of the page.
- **Machines** — filterable by location and by search. Each row now shows which
  studios that Mac serves, and its per-kind job counts are labelled *work done by
  kind* so they are not mistaken for models offered. A machine's own limitation
  is shown in full rather than reduced to a dot.
- **Local Models** — pooled across every location: how many locations offer a
  model, how many machines can actually serve it, the best fit verdict, the
  memory floor and what is cached. Select a model and the table below lists every
  machine that reports it — with the Hub's own reason when it cannot serve it.
- **Jobs** — work in flight per location, with the Hub's exact error for a
  stalled or failed batch, and a refresh that re-reads the location's models.

The location filter and the search box above the tabs apply to Machines, Models
and Jobs.

## Models: browse the catalogue by operation

The **Models** page is a browser over the whole catalogue now, not one flat list.

- **Operations lead.** Chat, image, video, voice, transcription and music are
  chips with live counts, so what a model *does* is one click away. Enabled /
  not-enabled sits beside them in the same shape.
- **Filters that say what they mean.** Provider, discovered or added by hand,
  free or paid lane, the provider's stated privacy stance, and whether the
  provider itself is switched on — all clearable in one press.
- **Sort and group.** Order by operation, name, provider or enabled-first, with a
  direction toggle; group the result by operation, provider or state to read it
  as sections.
- **Every number is counted before you act.** Each chip shows how many models it
  would leave, and *Enable / Disable all matching* covers exactly that set — the
  page size pins to the whole catalogue while grouping, so a group header counts
  the group, never a page.
- **Roomier type.** Model names lead, ids sit beneath them in a quieter monospace.

## Runs on older macOS

GatewayKH now states its own **macOS 11** minimum instead of inheriting whatever
macOS the build toolchain shipped. A build made before this change stamped a
macOS 27 minimum and would not open on macOS 14, reporting "requires macOS 27".
The same code was always fine on macOS 11; the build just never said so.

## Smarter fleet generation

Generation now goes to the machines that can actually run **that model**, and a
controller that cannot serve one is stepped over instead of failing the job.

- **Numbers decide, not speed.** A site is weighted by how many of its Macs fit
  the requested model, so ten 8 GB image Macs no longer out-weigh two 24 GB Macs
  for a model only the big ones can run. Free capacity breaks a tie, and the
  optional `GATEWAY_PREFER_FASTEST_SITE=1` setting is a nudge, never a takeover.
- **A model a controller cannot run is not offered.** When Studio Hub reports a
  model as not dispatchable there, it is withheld from the picker rather than
  offered and then refused.
- **A refusal is handled, not fatal.** If a controller still refuses the model at
  submit, the job moves to the next location that offers it. A voice stays on the
  one controller that holds its reference clip.
- **Clearer Fleet tokens.** Adding a Studio Hub location asks for its **Fleet
  token** by name. The **Hub token** is that Mac's own key, needed only to manage
  shared voices; the **Fleet token** is the work badge that sends and collects
  work. Joining a controller stays that Mac's own decision, made on that Mac with
  the controller's **Fleet token** — not a Hub token.

## Content Packs: install a pack someone sent you

The **Content Packs** page sets up a private shelf once — your own signing
identity, separate from the old Store — then you add a `.sspack` someone sent
you, and Story Studio discovers it through the same Gateway URL and installs it
with its normal Download/Install. The shelf publishes; it does not author.

- The shelf accepts only **newly reserved** pack identities, so historical Store
  packs are never reissued, re-signed or claimed as new.
- Back up the identity *and* its releases with
  `scripts/backup_content_authority.py`; the shelf cannot serve a release whose
  archive is missing.

## Run Story Studio from another Mac

The Gateway can serve your whole Tailscale network now, so Story Studio on
another Mac connects by pointing its **Base URL** at this Mac's Tailscale
address. **Settings** shows that address with a **Copy** button. The gateway also
moved off port 8000 (a very common port, and Story's own default) to its own
port, so there is never a question of which service answered.

## Smaller things

- **Content Packs** is split into **Shelved · Preserved · Drafts · Setup** tabs
  instead of one long scroll, and every pack carries a small **type pill** with the
  list grouped by type. A filter row shows how many of each type you have and
  narrows to one, so 140 products read as a list rather than a card each.
- The **Content Packs** page is now in the app itself (no browser detour), and it
  imports archives in bulk: choose one file, several, or a whole folder, and one
  click preserves them all. Nothing is typed in — each archive carries its own
  name, description and kind. The preserved list has search, sort and an
  integrity filter.
- **Jobs** now lists the **Provider** that ran each job, so you can tell DeepSeek
  from fal from your local Studio Hub without opening the row.
- The sidebar is grouped by what each page is for now — **Create** (the six
  lanes), **Library** (Jobs, Voices, Models, Content Packs), **Connection**
  (Providers, Fleet) and **App** (Settings) — instead of one long "Library & app"
  group with Content Packs stranded at the bottom.
- **Providers** cards no longer clip their buttons in a narrow window, and a
  **No key** filter plus a live count keeps the ones you still need to configure
  in view — finished providers drop out.
- **Voices** bulk controls moved above the table, with **Select all shown** and
  **Select none**: search, select all shown, then Enable selected.
- The app is a **normal Dock app** now — Dock icon, ⌘-Tab, and clicking the Dock
  icon brings the window back. The menu-bar bolt is still there.
- **What's New?** now sits under the build stamp in the sidebar.
- Fixed: table rows read as black-on-black in light appearance.

---

## More predictable Create and queued jobs

Refreshing models keeps your selection. If that model disappears, Create asks
you to choose again. Generate stays disabled while the current request is pending;
a temporary status-check failure is shown and retried without submitting again.
Cancelled jobs no longer show an endless spinner.

Cancelling a queued job now prevents dispatch even when the worker has already
read its queue. This does not stop work already accepted by an upstream provider.
OpenRouter's batch-only models are withheld from ordinary chat, and network
access requires a configured gateway key.

---

## Your voices now work on every model that can clone

Only **one** of your eight local voice models offered any voices. Every voice you
imported was stamped with the model it happened to be tuned against, and a voice
was matched to a model by that exact string — so picking OmniVoice or VoxCPM2 for
quality gave you an empty voice list, which reads as "you have no voices" rather
than "those clips belong to another build". **All eight now offer all ten**, and a
model that says it does not clone still gets none.

## Your recordings are never shortened

Every model states its own reference window and the gateway now **reads** it
instead of guessing at it. Nothing here crops your clips: the recording stays
whole, and each model takes what its own contract allows — OmniVoice up to 10s,
Qwen3 up to 15s, VoxCPM2 as much as you give it. A model that wants a longer
reference in future is honoured automatically, because the number comes from the
model rather than from us.

**Reference…** on the Voices page shows, for the voice you select, exactly what
each local model will do with it — used whole, shortened to a shorter aligned
window, refused, or limits unverified — and warns when a clip is under a model's
floor or missing the transcript a model needs.

## Fixed: a location showed this Mac's models

A Studio Hub location added on the Fleet page discovered **this Mac's** model list
instead of its own, because one discovery path skipped the location's address and
fell back to the local Hub. Two sites would have shown identical model lists, and
work routed to one could have been sent for a model only the other has. Fixed, and
now tested.

---

## The app now stops the gateway when you quit

**This is the one to know about.** Quitting used to leave the gateway running, and
the next launch would *adopt* that old process — so "quit and reopen" quietly kept
serving the previous build. One gateway ran for two hours across four rebuilds
because of it, and the only symptom was a feature that silently did not work.

- Quitting now stops the gateway and releases port 8000.
- **Settings → "Keep the gateway running after I quit"** restores the old
  behaviour if you want Story Studio to stay up while the app is closed. Off by
  default.
- **Check Gateway** on the Overview page tells you whether the running gateway is
  the build this app installed, and **Restart Gateway** fixes it.

## Every build identifies itself

The app and the gateway now both carry a build stamp (commit + build time).
`/health` reports it, and the app compares it, so "am I running the latest?" has an
answer instead of a guess.

---

## New providers and lanes

- **xAI is connected properly.** Grok chat and all three Grok image models, plus
  **Grok video** — which needed its own adapter, because xAI's video API is
  deferred (submit, then poll) and its image API rejects the `size` field every
  other provider accepts.
- **fal is connected for image, video and speech.** ElevenLabs Turbo v2.5 works
  through fal, and **your own ElevenLabs voices are offered on that lane** — fal
  accepts their ids unchanged, so a voice you saved from your account works
  through either route.
- **Music works at all now.** Suno V6 and Google's Lyria 3 Clip, both verified
  with real audio.
- Your fal picks: Nano Banana 2 Lite, GPT Image 2.5 (Sunburst and Flare), Seedance
  2.5, H3 Max, Gemini Omni Flash 1.1 — text and image versions of each.

## Voices

- **Add by ID.** Paste a voice id from a share link and save it. ElevenLabs also
  needs the owner id, which the window asks for.
- **Search the provider's public library** still works, and now a voice you added
  by id is offered wherever it belongs.

## Nothing broken is offered any more

The Models page no longer lists a model that cannot run. That removed:

- nine OpenAI transcription models — only `whisper-1` can return the SRT this
  gateway must send
- Google's Lyria on the free tier, which has no Lyria
- OpenRouter's Lyria rows — it lists them but publishes no way to call them

If a row is there, it runs.

## Things that were wrong and are fixed

A full audit found roughly fifty defects. The ones you would have hit:

- **Claude models were broken before you used one.** Anthropic reports a finished
  answer as `end_turn`, and Story Studio only accepts a clean stop — so every
  successful Claude job would have looked truncated.
- **Your local voices had vanished from the narration picker.** Models had been
  renamed to publish under one namespace and voices under another, so they stopped
  matching. All ten are back.
- **Three URLs could return a 500 just by typing a silly number** — including the
  route the Jobs window uses. One of them (`-1`) meant "give me every job ever
  recorded".
- **You could not switch a location off** without a 500, and **Remove** on the
  Fleet page was pushed off the edge of the window where you could not click it.
- **Transcription quoted you one model and ran another.**
- **Reusing an Idempotency-Key across two different things returned the wrong
  job** as if it had succeeded.
- **The Fleet page counted Studio Hub's own test probes as your machines** — it
  read "3 machines, 2 offline" for one Mac. Now it reads one, and says how many
  probe entries it hid.

## Artifacts

Received artifacts are temporary: kept **7 days or 10 GB, whichever comes first**,
editable in Settings. Nothing under an hour old is ever removed, because a
freshly-written artifact is about to be fetched.
