# What's new in gateway-kh

Newest first. This is the same list the **What's New** page in the app shows.

Each release carries its version and date, the way Studio Hub's notes do, so an entry says *when* it
arrived instead of only what it does. `tests/test_whats_new_page.py` fails if the newest section is
not a `## <version> — <date>` heading matching `VERSION`, so the next release cannot forget. The rows
below the newest group are the history from before this shape existed: their titles are `###` like
every feature title, and they are left as they were written.

---

---

---

## 0.2.104 — 2026-10-03

A paid model in a pool is a last resort.

### Free first, paid only when the free ones are out for the day
A pool can end with a paid model as a safety net. The free models are always tried first; if they are only on a
short per-minute limit the job waits a moment for them instead of paying. The paid model is used only when every free
model is out for the day (or switched off), and the gateway log says so when it happens. The Pools tab marks such a
model "Paid · last resort".

## 0.2.103 — 2026-10-03

Restarting the gateway no longer throws away work in progress.

### A restart picks running jobs back up
A job a Studio Hub Mac was working on when the gateway restarted (an update, a quit) used to be marked "outcome
unknown" and had to be redone, even though the Mac finished it. The gateway now reconnects to it by its job number
and collects the result as if nothing happened. Jobs left "unknown" by earlier restarts are settled from the Mac's
own record when it finishes.

## 0.2.102 — 2026-10-03

Pause any group or Mac yourself.

### Pause and Resume
Fleet → a group's "…" → **Pause Group** (This Mac included), or a Mac's "…" → **Pause This Mac**. A paused group or
Mac gets no new work; anything already running there finishes, and the pause survives restarts. It shows as a grey
"Paused by you", never as a problem, and only you can lift it — the automatic pause after an outage never resumes
something you paused. **Resume** brings it straight back. Pausing a single Mac inside a group needs Studio Hub 0.6.135
on that group.

### Voices on a paused Mac
A voice whose sample this gateway keeps still goes to the fleet. A voice that exists only on a Mac you paused stops
with a plain message instead of quietly running there.

## 0.2.101 — 2026-10-03

Narration with your own cloned voices uses the whole fleet.

### Cloned voices are no longer stuck on this Mac
A voice whose sample this gateway keeps (like "Chris") used to be tied to this Mac, so every chapter of every
episode was narrated here, one after another. The sample now travels with each job to any Mac that can run the
voice model, checked byte for byte on arrival, so chapters are narrated on many Macs at once. A voice that lives
only on one Studio Hub still stays there.

### Story Studio narrates several chapters at once
Each voice model now says how many requests the fleet can take at once, so Story Studio sends that many chapters
together instead of one at a time.

## 0.2.100 — 2026-10-03

Failed jobs tell the truth, so Story Studio can redo them.

### A job that never reached a model is a plain failure
If something went wrong inside the gateway before any model was asked, the job is now an ordinary failure you can
redo — not "outcome unknown, check before retrying". Only a request that really left for a model and never came
back stays held. The 20 Gemini pool jobs from the night of 3 October are released automatically.

### Story Studio can tell free models from paid ones
Each writing model now says whether it can cost money (a pool counts as paid if any model in it is), so Story Studio
never quietly lands on a paid model when you chose free ones.

### Repeats are labelled
When Story Studio sends the same request twice, the gateway now says it is handing back the earlier job.

## 0.2.99 — 2026-10-03

Rename a Mac from the Fleet page, and groups that missed a Studio Hub release catch up by themselves.

### Fixed: every Gemini pool request failed
A request to a pool (such as "Gemini Free — all") failed in about a second with
"module 'gateway.catalog' has no attribute 'resolve'" before any model was asked. Pools work again; jobs that
failed this way can simply be retried.

### Rename a Mac from the Fleet page
Fleet → a Mac's "…" menu → **Rename…**. The new name is set on the Mac itself, so its own Studio Hub, its
group's main Mac and this page all show it. Its machine ID stays the same, so nothing re-joins and no work is
re-routed. Needs Studio Hub 0.6.133 on the group; an older group says so instead of failing quietly.

### Groups that are behind catch up automatically
A group added after a release went out, or one that was offline at the time, is sent the newest Studio Hub
release this gateway published — on its own, one group at a time, only the files that changed. It never sends
an older version over a newer one, never touches a group that doesn't trust this gateway's key (it says why),
and waits until a rollout has reached most groups so a test release on one group doesn't spread early.
Fleet → Locations shows "Sent automatically at …", "Catching up…", or why it didn't.

## 0.2.98 — 2026-10-02

Fleet stops crying wolf, and every Mac shows whether it is on the latest Studio Hub.

### A healthy idle fleet reads green
The Fleet page no longer flags idle Macs as "Needs Attention", no longer lists "workers offline" for the
retired Video studio, and no longer shows "partial_read" or "(newer)" notes. A Mac is flagged only when there
is a cause it can name — a studio that should run is down, a job failed in the last 15 minutes, a stalled job,
or the Mac is offline — and the reason shows in "Doing now" and the tooltip.

### Latest, Updating…, Behind
Each Mac carries a version badge: **Latest** (on the newest Studio Hub this gateway published),
**Updating…** (its controller holds the release and it is picking it up), **Behind — 0.6.126** (with what to
do), or **Unknown** (offline). Group headers read "Hub 0.6.132 · Latest · 10 of 11 on latest", the summary
has an "On latest 41/41" tile, and the Machines and Locations tabs show the same verdict.

## 0.2.97 — 2026-10-02

Pools you can edit by picking models, pools that remember which models are out of quota, and tabs on Analytics.

### Pools: pick models instead of typing ids
Providers → Pools has a searchable model picker (enabled chat models, with Free/Paid and price), and each
pool can be edited in place: add, reorder with ↑/↓, remove one member with ✕, rename, or Delete Pool
(with a confirmation). Every member shows whether it can answer right now.

### Pools skip models that are out of quota
When a member runs out of quota, the pool remembers it and skips it until its limit resets — midnight
Pacific for Google's daily free quotas, or the time the provider says — instead of trying it first on
every job. If every member is out, the one that comes back soonest is tried. The Pools tab shows
"Out of quota, back at 15:00".

### Analytics has tabs
By Type, By Model and By Machine each get their own tab under the summary tiles, and the page remembers
the one you looked at last.

---

## 0.2.96 — 2026-10-02

The window stays the size you set.

### Overview no longer shrinks the window
Opening Overview used to snap the main window back to a narrow size, and every page after it stayed
that way. Switching pages now never changes the window: each page lays out inside whatever size you
chose, and a test visits every page at a large, a medium and the minimum size to keep it that way.

---

## 0.2.95 — 2026-10-02

Smaller fixes: the daily summary counts all audio, hidden keys stay private, and backups explain themselves.

- **Daily summary counts compressed audio.** The Telegram summary now totals the minutes of narration and music even when the
  files are m4a or FLAC, instead of saying "not measured".
- **Hidden keys no longer reveal their length.** A masked API key or token now shows the same row of dots whatever its length.
- **Backups explain when Hub voices live elsewhere.** If the Studio Hub's voices are on another Mac, the Backups card now says
  they are not in this backup and where to find them.
- **More reliable tests.** A test that sometimes failed under load now checks the same thing without depending on timing.

## 0.2.94 — 2026-10-02

Telegram alerts, backups, secrets that stay hidden until you ask, and a calmer Fleet page.

- **Telegram alerts.** Settings has a new Telegram card. Paste a bot token and chat ID and the Gateway messages your phone when a
  Mac or group goes offline (and again when it is back), when a disk or memory runs short, or when jobs keep failing or get
  stuck. You choose which events you want, set quiet hours, and get a daily summary of yesterday every morning. Nothing is sent
  until you save a token and chat ID, and the token is never shown again once saved.
- **Backups.** Settings has a new Backups card. **Back Up Now** makes one file holding what cannot be recreated: the signing keys,
  the API keys, the fleet roster, the voices library and the Content Packs (about 180 MB). It saves to iCloud Drive, to every
  attached KH-STUDIO drive and to this Mac, and can also run every night if you switch that on. **Import / Restore** puts a backup
  back, checking it first and moving anything it replaces to the trash.
- **Secrets are masked, with an eye.** Every API key and token field in the app and in the console now shows dots until you click
  the eye to show it, and Copy works without revealing it.
- **A calmer Fleet warning banner.** Warnings from many Macs are now grouped into one line per kind in a short, scrolling banner
  instead of a wall of text. A controller's own row is no longer labelled "This Mac", and you can hide the macOS-update and Time
  Machine warnings (disk-space warnings can never be hidden).

## 0.2.93 — 2026-10-02

A small release that fixes how cancelled jobs are reported and retires one more local model.

- **Cancelled jobs say "cancelled".** A job you cancel is now reported to Story Studio and other apps as cancelled, instead of as a
  failed job that happened to be cancelled, so it no longer shows up as an error.
- **Gemma 3 12B is removed.** The Gemma 3 12B text model is retired and is no longer offered, even if an older Hub still lists it.

## 0.2.92 — 2026-10-02

A release about keeping the Macs healthy and making the fleet easier to see and control.

- **Groups view.** A new Groups tab shows every Studio Hub group (controller plus its joined Macs) as one block, with what each Mac is
  doing right now, its disk space and its startup setting.
- **Disk guard and a Disk column.** The gateway now keeps its own Trash and logs small, warns before a disk gets full, and the Fleet table
  has a Disk column so you can see which Mac is running low. Finished jobs are archived instead of piling up.
- **Remote startup control and auto-start.** You can see, and change, whether a Mac starts Studio Hub when it logs in, from the Gateway,
  without walking over to it. Fleet Macs now start automatically.
- **Compressed files pass straight through.** When you ask for a compressed result (m4a audio, JPEG images) the gateway now hands that
  request to the Hub and stores the file with its real extension, so what you get is the small file, not a re-encoded copy.
- **Out-of-memory jobs are re-routed.** If a Mac reports it does not have enough memory for a job, the gateway now sends the job to a
  different Mac or location instead of failing it. A job pinned to one location fails with a plain sentence saying why.
- **The gateway decides time limits, and Cancel is real.** Time limits are now set by the gateway and told to the Hub, and Cancel stops
  the work on the Mac, not only in the queue.
- **Plain error text.** Failures now say what went wrong in ordinary words instead of showing internal codes.
- **LTX video is removed.** The retired LTX video model is no longer offered, even if an older Hub still lists it. Having no local video
  model is a normal, clean state.
- **A design refresh.** Blue buttons, consistent spacing, type and status colours, Title Case labels, aligned tables, in both the menu bar
  app and the web console.

## 0.2.91 — 2026-10-01

A reliability release from a full audit of how the gateway, Story Studio and Studio Hub talk to each other.

- **Nothing freezes when a Hub is unreachable.** Dispatch used to wait on a Hub that was switched off or off the network and hold up everything
  behind it. It now gives up on that Hub quickly and the job is routed elsewhere.
- **The 3-hour queue limit only counts time spent waiting.** A job that had already started could be cancelled for having been "queued" too
  long. The limit now measures queued time only, so running jobs are never cancelled by it.
- **Settings saves are crash-safe.** Settings and secrets are written to a temporary file and swapped in, so a crash or power cut mid-save can
  no longer leave a half-written file.
- **Downloads are streamed** instead of loaded into memory first, so large results use far less memory.
- **Status refreshes faster**, and a rare race that could lose a model-list save when two saves overlapped is fixed.
- **Music picks a model that can actually serve the request.** The gateway now publishes which music models need lyrics and which support
  vocals or instrumental-only, so a request for an instrumental, or one with no lyrics, is no longer sent to a model that cannot do it.
- **A failed pre-flight check re-routes the job** to another model or location instead of turning into a hold you have to clear by hand.
- **Uploads up to 100 MiB** are accepted for Hub transcription (long recordings used to be refused).
- **Cancel works while a job is waiting for a Mac**, and Cancel in the menu bar now reaches the Hub, so the work really stops there too.
- **Voice controls only appear when the chosen lane supports them**, so you no longer see speed or voice options that do nothing.
- **Repeated requests are answered from the first result.** A retried request that was already accepted is replayed before anything else is
  checked, so it never creates a duplicate job or a spurious refusal.
- **A Hub's own refusal wording is kept**, so the reason you see is the Hub's full sentence, not a shortened summary.
- **A downgrade refusal is shown in orange**, as a warning, instead of the red used for real errors.

## 0.2.90 — 2026-10-01

- **Whisper large-v3 turbo is now offered for subtitles.** Studio Hub marks Whisper turbo as a "dependency" because two Qwen3 voice models also
  use it, so the gateway hid it and the Subtitle model list was empty. The gateway now offers it, since the Hub also lists it as the Subtitle
  model. Real helper files (tokenizers, codecs) stay hidden.

## 0.2.89 — 2026-09-30

- **All six Create pages are redesigned to match Studio Hub's generator pages.** Audio, Subtitle, Image, Music, Video and Chat used to be a narrow
  column of small controls in the top-left corner with the result squeezed underneath. They now share one layout: the form on the left
  (about 390 pt, scrolling on its own) and the result on the right, filling the rest of the window at any size. Larger text, full-width
  controls, and the form reads top to bottom: pick the model (with a Refresh button and a **Manage Models** shortcut), say what to make, then a prominent Generate button.
- **Real previews, not a "Preview" button.** The result appears by itself: **Audio and Music** get an inline player with a waveform you can
  click to seek (music that returns two tracks has a Result 1 / Result 2 switch); **Image** shows one large picture with thumbnails when
  the model returned several; **Video** plays inline; **Subtitle** shows the transcript with a timestamp beside each line, plus Copy and Save (SRT);
  **Chat** is a conversation with You and Model turns, and each message now carries the earlier ones so the model answers in context.
  Save, Open, Show in Finder and Pin are in the result header.
- **Fixed: a test job id showed up on the Audio page** ("Job mock-job is selected. Reload it…"). A test had once saved its stand-in job id into your real
  saved Create state. Tests can no longer touch it at all (only the app itself reads or writes it), and a saved job the gateway does not know
  is now dropped instead of shown. A saved job it does know comes back by itself when you open the page.
- **Fixed: Subtitle said "Your selected model is no longer available" with an empty model list.** The Studio Hub Whisper models were switched on the whole
  time; the page had saved one under its old id (`studio-hub-local::…`) and the catalogue has since published the same model under a new one, so the page
  thought it had vanished. A renamed model is now recognised as the same model. If a lane really has no model switched on, the page says so
  ("No subtitle models are switched on. Open Models…") and **Manage … Models** opens the Models page.

## 0.2.88 — 2026-09-30

- **A release push that succeeded is no longer reported as failed.** When the connection to a controller broke during a push
  (the live pilot to controller 1500: "Server disconnected without sending a response" after 550 s, although the controller
  held the new release), the gateway now asks the controller what it holds, for up to a minute, before deciding. If the controller's
  own record carries this release (and has every file), the push is reported as a success with a note that the reply was lost; if not,
  the real failure is shown with what the controller does hold. Lost replies on the small per-file uploads are repeated automatically,
  and every result now says how it was sent (`mode`), how many files and bytes went over (`blobs_sent`, `bytes_sent`) and how long each
  step took (`timings`, also in the gateway log).
- **Fleet page: "built against Hub" now says 0.6.117** (it said 0.6.83). It is a label, never a gate.

## 0.2.87 — 2026-09-30

- **Publish a Studio Hub release to chosen controllers only.** A release can now go to a pilot controller first (`controller_ids` on the publish call) instead of every controller at once; an unknown id is refused before anything is sent.

## 0.2.86 — 2026-09-30

### Pruned artifacts go to a Trash Folder

Received artifacts are now kept **3 days or 8 GB** (was 7 days or 10 GB) — Story Studio already holds the originals.
Pruning no longer deletes: expired files move into a **Trash Folder** inside the gateway's data folder, and
anything in it is deleted for good after 15 days, automatically. The 8 GB cap counts only the live store, not the
Trash Folder. **Settings > Received artifacts** shows the Trash Folder size ("Trash Folder: 1.2 GB, emptied
automatically after 15 days"). Pinned files and anything newer than an hour are protected as before. Settings you
already saved keep their values; only the defaults changed.

## 0.2.85 — 2026-09-30

### Prices are visible in the app

The **Models** page has a **Price** column and every model under a provider on the **Providers** page shows
one. It reads "$1.50 / $9.00 per 1M", "$0.02 per image", "Free (your Macs)" for your own Studio Hub Macs,
"Not published" when nobody stated a price (never $0), or "Varies — N prices" when a pricing page gave several
numbers (peak and off-peak, short and long context); hover for every one of them. Under each price is who
stated it and when: the provider's own API, or "pricing page, <date>" as a link to that page. Each provider
card also lists the rate limits found by your price research, marked as informational "up to" numbers that
change nothing. **Import price research…** now opens its file picker inside the app (it did nothing there
before) and the page shows what is on file, accepted and dropped, each time it opens.

### Providers opens on the providers list

Opening **Providers** showed the Concurrency settings first even though the tab strip said "Providers"; you
had to click another tab and back. It now opens on the providers list every time. A tab you click stays
selected until you quit the app.

## 0.2.84 — 2026-09-30

### Publishing a Studio Hub release sends only what changed, to every controller at once

**Fleet → Publish release** used to read the whole ~1.9 GB disk image into memory and send it to each
controller one after another (the last real push: 5 controllers, 43 minutes, 8 GB of memory). Now the
gateway asks each controller what it can do. A controller running Studio Hub 0.6.115 or newer gets the
release's file list and then only the files it does not already have; every other controller gets the
whole image as before, but streamed instead of held in memory. Controllers are pushed together, each with
its own result in the report (how it was sent, how many files, and what the controller itself read back),
so one failing controller no longer stops the rest. The first release built by 0.6.115 is still a full push
to every controller; the delta starts with the release after it.

### Imported prices that vary are shown, not hidden

A model whose pricing page lists several numbers (peak and off-peak, short and long context) or a price in
another unit (per million bytes, "free of charge") used to show "price not published" even though the research
had it. It now shows the pricing-page source with every number listed as a variant; a single price is still
only shown where the page gave exactly one.

---

## 0.2.83 — 2026-09-30

### Import researched prices and rate limits, with the page they came from

**Providers → Import price research…** takes a JSON file produced by a research session that read
each provider's own pricing and rate-limit pages. The gateway fetches nothing and calls no model: it
only checks the file. **A number is kept only if it has a source URL on that provider's own domain
and a verbatim quote that contains the number** — everything else is dropped, and the page lists
each drop with its reason.

Kept prices appear on model rows as `price_source: "provider_page"` with `price_url` and the research
date (`price_as_of`), separate from `"provider"`, where the provider's own API stated the price.
A price the provider's API states always wins; where a page lists several sizes or qualities, none
is picked (they are listed in `price_variants`). Rate-limit tiers are shown as information only and
never change your concurrency settings. Importing enables nothing and routes nothing — Anthropic
models stay off for Story. The research session can check its file first with
`python3 -m gateway.price_evidence check <file>`.

## 0.2.82 — 2026-09-30

### A burst of jobs now reaches every Mac that can run it — including this one

**The failure, measured.** Twenty music jobs sent in the same second all went to one location; this
Mac (eligible, idle) was given none, and 16 of them bounced off that busy site before being re-routed.
Three causes, three fixes.

**Equal locations are spread.** The gateway picked a location by a lottery seeded with the model
name, so every job of a model drew the same winner. Each job now draws its own.

**A job waiting for a full location moves to one with room.** If the location chosen at submit is
full and another eligible one (this Mac included) has room, a job that was **never sent** is re-bound
to it. A job that has been sent — or that already has a Hub id — is never moved: that would be running
the same paid render twice.

**Each location holds up to its own eligible total.** The gateway used to hand a Hub only what was
free at that instant, keeping the Hub's own queue empty while the burst waited here. Now it admits up
to the number of Macs the Hub says can run the model (still capped by any limit you set on the
Providers page); the Hub queues the rest for whichever Mac frees first, and anything waiting longer
than 3 hours is still withdrawn.

**Scarcest work first.** The gateway now hands out slots to the model with the fewest eligible Macs
fleet-wide first, then oldest first, so work only a few Macs can do is not stuck behind work any Mac
can. It uses the Hub's published eligible-machine counts as they are (they become RAM-aware when the
Hub says so).

## 0.2.81 — 2026-09-30

### Hand a controller over from the app, resume a paused one, and see what a model really costs

**Change address… on the Fleet page.** When a controller is replaced (say a node is promoted to
head) you no longer have to Remove it and Add the new one — that makes a second location and orphans
the first one's credential and every enable decision you ticked. Locations now has **Change
address…**: type the new address (and a token if it is a different one), press **Test this address**
to see the new Mac answer as a controller before anything changes, then confirm. The location keeps
its identity, its credential and its enabled models; the old Mac's identity is dropped and the next
probe reads the new one. Confirming an address that has not answered a test asks you first.

**Paused controllers are visible, with a Resume button.** After a controller has been unreachable
for the pause window, the gateway stops routing to it (nothing is removed) — and its own message says
to resume it from the Fleet page, which had no way to. The Status column now shows the pause and its
reason, and a **Resume** button clears it.

**Prices say who stated them.** Every model row now carries `price_source`: `provider` (the provider's
own response carried the number), `local` (your own Macs — free), or `unpublished` (nobody stated
one), with `price_usd` and `price_as_of` beside it. Real prices today: OpenRouter, Groq, Token Harbor
and xAI chat models (per 1M tokens) and fal.ai image/video/voice models (fal's pricing endpoint). No
number is ever calculated or guessed; the old zero credit fields are unchanged so Story Studio keeps
every row. A finished job also reports `cost_usd` when — and only when — the provider states what it
charged (OpenRouter does).

## 0.2.80 — 2026-09-30

### Finished work is no longer lost when the gateway restarts, and a job waiting for a Mac says so

Three decisions of yours went in together, and one bug you would not have wanted to meet.

**A finished result is adopted, not left as a failure.** When the gateway restarted in the middle of
a job, it filed the job as *"outcome unknown"* — even when the Mac finished it seconds later and the
picture, voice or subtitle was sitting there. Now the gateway asks the Hub once a minute about every
held job, and when the Hub says *done* it fetches the result, checks its checksum, saves it, tells
the Hub it arrived, and settles the job as **succeeded** with a normal download link. Before this it
could not do that at all: the check that was meant to do it never saw the Hub's real answer, never
asked your own Mac's Hub, and only looked at the newest 50 holds — most of them not Hub work. Jobs
that had already been left that way are settled the first time this version starts.

**Waiting in the queue no longer uses up the job's time.** A job is now allowed its full render time
*once a Mac starts it*. Time spent waiting behind a busy fleet is not counted, and the job says so:
Story Studio now receives a `waiting` note ("waiting for a free machine", when it will try again)
that clears the moment a Mac starts. Nothing running is ever cancelled on a guess; a job still
queued after three hours is withdrawn from the Hub's queue, and only when the Hub confirms nothing
ran does it move to another location.

**Cancel works from Story Studio.** Every job now says whether it has been sent yet, whether cancel
will be honoured now, and whether a cancel was asked for. A cancelled job is reported as failed with
`cancelled: true` — Story Studio has no "cancelled" state, and it was polling such jobs for hours.
The video and subtitle cancel routes Story Studio was calling did not exist; they do now, and a
cancel that arrives just as the Hub accepts the batch is passed on to it.

### Smaller fixes on the same seam

- **Voice preview** works: a voice offers a preview only when it has one, and the button plays it.
- **ElevenLabs and Fish voice libraries** can be browsed and added to from Story Studio.
- Story Studio's capacity panel works: the account overview now reports how many jobs are running
  and queued.
- Polling is calmer: a long job is asked about every few seconds, not every second.
- A video's source image is kept with the job, so a video that waits more than 15 minutes, or spans
  a restart, no longer fails with "upload it again".
- Subtitle language and word timestamps chosen in Story Studio now reach the Hub.
- Story Studio no longer sees a Mac's address (`host:port` or `/api/hub/...`) in a failure message.
- Reference-image generation is no longer offered: the upload it needs does not exist here yet.
- `/api/version` reports the real version instead of `0.0.1`.
- The Hub's hold sweep, refusal codes (`STUDIO_NOT_RUNNING`, `OUTPUT_STORAGE_BLOCKED`,
  `MODEL_CATALOG_UNAVAILABLE` now move a job to another location), chat connect timeouts, artifact
  download retries with a checksum check, busy-Mac items from older controllers, subtitle timing and
  machine, and 5xx answers while polling were all corrected against the Hub's real answers.

### The Fleet page could undo your edits — that is fixed

Renaming a controller, changing its address, adding, removing or resuming one could be silently
reverted a few seconds later: the page's background refresh wrote back the list it had read before
it started probing. It also rewrote your roster file on every refresh. Every edit now takes effect and
stays, and the file is only written when something about it actually changed.

### The app

Quit, Stop, Restart and Save now warn before interrupting running jobs. The Jobs window shows attempts,
durations, absolute times and queue position, filters by state up to 500 rows, and stops rebuilding
the open detail on every refresh. Analytics shows model names with the id on hover, the Voices popup
refreshes properly, and a subtitle upload that is too large is refused before it is read.

### For whoever runs the tests

The seven tests that asserted "the pool is 60" and "two providers" now assert the rule against
whatever your settings are, so they pass on your Mac and on a clean install. The flaky Fleet routing
test was a listen-backlog limit in its own mock server, not the gateway.

## 0.2.79 — 2026-09-30

### The Fleet page stopped telling you to fix Studio Hub on Macs that are switched off

You noticed that one controller showed eight of its nine Macs unreachable and asked whether that
was a real outage or a reporting problem. **It was a real outage — those Macs are off** — but
checking it turned up a genuine mistake in what this page was telling you to do about it.

When a Mac does not answer, the Fleet page offers a next step. It said **"Studio Hub on that Mac"**
for every one of them. That is correct for exactly one of the two reasons a Mac goes unreachable:

- **The Mac answered, but Studio Hub is not running on it.** Then yes — go and start Studio Hub.
- **The Mac never answered at all** — it is asleep, switched off, or a firewall is blocking it.
  Telling you to look at Studio Hub there sends you to check something that is not the problem.

Those eight rows were all the second kind: switched-off Macs being reported as a Hub fault.

The page now says the right thing for each: **"Check that Mac is switched on and awake"** when the
Mac never answered, and **"Start Studio Hub on that Mac"** when it did answer without its Hub.

This works with the controllers you are running today. It also reads a new field from Studio Hub
**0.6.113** onward, so once your controllers are updated the reason comes from the Hub directly
rather than being worked out from its wording.

## 0.2.78 — 2026-09-30

### A new Analytics page: what you generated, and which Mac did it

You asked two things — whether the joined Macs are really generating (they are, and this shows it),
and whether you can see what your fleet has actually been doing. There is now an **Analytics** page
in the sidebar, under Library, directly below Jobs.

Jobs answers *"what happened to that job"*. Analytics answers *"what has this fleet been doing"* —
grouped three ways:

- **By generation type** — image, voice, video, music, chat, subtitle: how many, how many worked,
  how long they took, how much they produced.
- **By model** — so you can see which model is actually fast on your machines.
- **By machine** — every Mac that ran something, with the controller it belongs to. **`local` means
  the controller ran it on itself; a node id means a joined Mac did.** That distinction is the
  whole answer to "are the nodes working?" — they are, they just were not visible from here before,
  because your gateway picks the *site* and the controller picks the *machine*.

**Median and average are both shown, deliberately.** The first render after a model loads is much
slower than the ones after it, so an average over a few image jobs describes the model *loading*
rather than the machine working. The median is what you should expect; the average sits beside it so
you can see the difference.

### The number behind it was missing, and that was a real bug

This page could not have been built a day ago. Your Hub has always reported how long each item took
— the gateway was reading several fields out of the same reply (`which Mac`, `how many bytes`, the
checksum) and **skipping the runtime**.

Chat recorded a time; image, voice, music and video recorded **nothing**. So the generation record
held a rate for the one lane that is a cloud call and `None` for every lane that does the real work.

That is fixed, and it is the reason this page exists at all. One honest limit: **times are recorded
from now on.** Jobs that already finished ran before the fix, so they show a dash rather than a
number — a dash means *not stated*, and it is excluded from the averages rather than counted as
zero, because counting an unknown as zero would flatter every model. The counts, machines and output
sizes for those older jobs are all still there.

---


## 0.2.77 — 2026-09-30

### The gpt-image family generates again

`gpt-image-2.5-sunburst-2026-09-08` failed on every attempt with:

`openai-direct: HTTP 400: Unknown parameter: 'response_format'.`

The image adapter sent `response_format: "b64_json"` **unconditionally** — which the
older `dall-e-*` models accept and need, to avoid a second fetch for the URL. The
newer `gpt-image-*` family refuses the parameter outright and returns base64 anyway,
so omitting it is not a trade: it is the only spelling they accept.

Curated by model prefix rather than discovered, for the same reason
`TRANSCRIPTION_SRT_UNSUPPORTED` is a list — no provider *publishes* which parameters
it rejects, so the alternative is finding out one failed generation at a time.

## 0.2.76 — 2026-09-29

### A dropped poll no longer throws away a finished render

The Hub adapter polled a batch once a second against a 15-minute deadline, and treated the **first**
dropped connection as an unknown outcome. One blip out of as many as 900 polls discarded the job.

Measured on a real one: batch `0dcdc6331e` was reported failed at 23:10, and the Hub's own record
shows that same item finished **`done`, with an artifact**, four minutes and forty-five seconds
later. The image had been rendered and was thrown away.

Two things make this safe to fix. The batch was already accepted and the Hub **keeps** it, so asking
again is a GET on something that exists — idempotent, and no duplicate work can result. And the
question it answers is the only one that settles the outcome: *is this batch finished?*

A poll may now fail for up to **60 seconds** before the hold is raised, and any successful poll
clears the run — so a Hub that drops one request an hour is never mistaken for one that has gone
away. A Mac that really is gone still produces its named hold, and the sentence now says how long it
waited, so "gave up immediately" is distinguishable from "stopped answering".

## 0.2.75 — 2026-09-29

### The Fleet page stops timing out when the fleet is busy

**Measured, not guessed.** The snapshot cache lived for **5 s** while the background poller runs
every **15 s** — so for two thirds of every cycle the cache was stale, and a read in that window did
its own sweep of every controller. Ten Hub calls per location, one after another, each with a 6 s
timeout: **22.7 s** for one snapshot, against the app's **15 s** request timeout.

So the Fleet page appeared only when nothing was running — which is exactly when nobody is looking at
it. *"This isn't showing. But when there's no job running, it did show."*

Two changes, and the numbers now:

- **The Fleet page read: 22.7 s before, 0.000 s now.** It no longer waits at all.
- **A forced refresh: 22.7 s before, 8.3 s now.**

- **A read that did not ask to be fresh never waits.** The poller owns freshness, exactly as its own
  docstring always claimed; a stale read is served and refreshed behind it.
- **The nine enrichment reads are fetched at once**, not in sequence. None needs another's answer, so
  the cost is the slowest rather than the sum.
- Concurrent readers share **one** sweep rather than starting one each, and the Refresh button is the
  one path allowed to wait for a real fan-out.

### A hold the Hub wrote can now be discarded

`_named_attempt` — the reader behind **Discard** on an uncertain job — looked for
`hub_batch_id` / `upstream_job_id`, while the Hub adapter writes `batch_id` / `item_index`. So the one
route that clears a hold refused holds whose attempt was named perfectly well, answering
`RECOVERY_CONTEXT_MISSING` with the batch sitting right there in the context. The reader now accepts
every spelling its writers use.

## 0.2.74 — 2026-09-29

### Pools — one name for several models, tried in order

A model pool is a **list of this gateway's own models under a single name**. Story Studio saves
one id (`pool::free-all`) and the gateway walks the list until one answers, so a spent free lane,
a retired model, or a provider refusing one account costs a *retry* instead of the job.

The order is the policy: the first member is tried first.

**Only a 400 the provider says is about the request itself stops the walk.** Every other failure
moves on — 401, 402, 403, 404, 429, 5xx, a timeout, a connection error, or a 400 the adapter
marked retryable. The asymmetry is deliberate: advancing when we should have stopped costs a few
round trips and the caller still gets the same error, while stopping when we should have advanced
costs the job. Those provider-side states are exactly what a pool exists to walk past.

Three things that make it honest rather than merely convenient:

- **A member is a gateway id** (`provider::model`), never a vendor's, so a pool cannot name a
  model this gateway is unable to reach.
- **Every member says whether it still resolves.** A pool is a list of promises, and the failure
  that costs an afternoon is a member disabled weeks ago: the pool still works, just less often
  than its list suggests. The Pools tab shows each member and marks the stale ones.
- **Which member answered, and what the others said, is recorded on the result.** A pool that
  works less often than its list suggests would otherwise be unreadable.

Pools are edited live — nothing here needs a restart — and the members are resolved through the
same catalogue everything else uses, so a member you switch off is skipped for exactly the reason
it would be refused on its own.

**Providers page → Pools** to make one, or `POST /admin/api/pools`.

## 0.2.73 — 2026-09-29

### Add a provider yourself, from the Providers page

You asked for this: a provider you add yourself, without waiting for the app to learn it. There is
now a **+ Add a provider** button on the Providers page. Give it a name, an id, a base URL and the
kind of API it speaks, and it is saved and offered like any other provider.

**The reason this was not already possible is dull but worth one line:** the file behind it,
`providers.local.json`, has been read by the gateway for a long time — but nothing could *write* it,
so adding a provider meant finding a file in Application Support and getting its format exactly
right by hand. That is now a form.

**Two things it tells you rather than leaving you to find out:**

- **Restart the gateway after adding one.** Providers are read when the gateway starts, so a new one
  does not appear until then. The page says so in the message, not in a footnote.
- **If something you added is not loading, the page says why.** An entry with a kind the gateway
  cannot run is skipped — and until now it was skipped *silently*, which reads as "my file didn't
  load" rather than "nothing can run that kind". The form refuses it up front, naming the kinds that
  do work, and anything already in the file that will not load is listed with its reason.

Existing providers are untouched, and one you added can be removed again.

### OmniRoute is a provider now

Your local OmniRoute, on this Mac. It fans out across your subscription, API and free tiers behind
one endpoint, so everything you have wired into it — including the coding plans you cannot call
directly — is reachable from Story Studio through the gateway.

One thing to know: **OmniRoute lets you chat without a key, but listing models needs one.** The
gateway discovers models by listing them, so make a key at
`http://127.0.0.1:20128/dashboard/api-keys` and paste it in the Providers tab. Then restart the
gateway.

## 0.2.72 — 2026-09-29

### The Providers page is split into tabs

You asked for this one: the Providers page had become a single very long scroll — twenty-three
provider cards, then the concurrency settings, then the live queue, then the measured numbers — so
reaching any one of them meant scrolling past the other three.

It now has four tabs, in the same style as the Fleet page:

- **Providers** — the keys, your Studio Hub locations, and the models each one offers
- **Concurrency** — how much work runs at once, per lane
- **Queue** — what is waiting right now, and what is holding each lane
- **Measured** — what each model has actually carried, from real runs

Nothing moved out of reach: everything that was on the page is still on it, one tab across. A
`#queue` link still opens the queue — it selects the Queue tab now instead of scrolling to a
section that is no longer in the same column.

## 0.2.71 — 2026-09-29

### Ollama Cloud is a provider now

**Open models served by Ollama, with no local install.** Ollama's website points you at a download,
which makes it look like running their app on your Mac is the only way in — it is not any more.
`ollama.com` has an ordinary API: make a key on their settings page, paste it into the Providers
tab, and their cloud models appear in the catalogue like any other provider's.

Worth knowing before you rely on it: Ollama's free tier is **starter credits** over a **starter set**
of models with **one request at a time**, not a standing free allowance. Running models on your own
Macs stays unlimited, which is what your Studio Hub machines are for. Treat this as one more lane to
switch to when a free quota somewhere else runs out.

## 0.2.70 — 2026-09-29

### Refreshing the model list, and knowing how old it is

The Models page now has a **Re-discover models** button, and it tells you **how old the list is**
beside the model count.

This came from a real puzzle: a model was switched on in the Models tab and then never appeared in
the Chat tab. The missing piece was not the Chat tab — it was that this list is a **snapshot**, not
a live view. A provider's catalogue is fetched once and cached until something asks for it again, and
nothing does that on a timer or when the app starts. So a model the provider added since the last
fetch is simply absent, and nothing on the page said its inventory could be days old. On this Mac it
was **six days**.

Two things now, both on the Models page:

- **Re-discover models**, beside *Clear filters*. It does exactly what the Providers page's
  *Refresh all models* does, because there was no reason for two meanings of "refresh".
- **The inventory age**, beside the count. Past a day it is marked, because that is when a model can
  be missing for a reason you cannot see. If no provider has reported a fetch time it says
  **unknown** rather than showing nothing — unknown and current are different things.

**Restarting the app was never the answer**, and it is worth saying plainly: a relaunch re-reads the
same cached list. If a model you ticked is missing from the Chat tab and the inventory is recent,
just click the Chat row again — that page re-reads when you open it.

## 0.2.69 — 2026-09-28

### The notes say which release each change arrived in

This page listed every change as a heading of the same size, with **no version and no date** — so you
could read what the gateway does now but not when any of it appeared, and a fix you had been waiting
for looked exactly like one that had always been there. Studio Hub's notes have grouped by release
for a while; these do too now.

The first thing the new grouping exposed: **`0.2.67`'s changes were never written up here at all.**
Four things shipped that day and this page said nothing about any of them. They are written below.

### A controller that stays away is paused, not thrown out

A Studio Hub location that cannot be reached for a week used to keep taking its share of customer
work, so jobs waited on a machine that was never coming back. After **seven days** it is **paused**:
routing skips it and the Fleet page says why. **Nothing is removed** — its credential and every
per-model switch stay exactly as they were — so switching a house back on is one **Resume**, not a
re-enrolment. The seven days is a setting, and a gateway restarted yesterday does not reset the clock.

### A job whose outcome nobody could confirm now closes itself

When a Mac dies mid-job the gateway could not tell *"it never started"* from *"it finished and I never
heard"*, so the job sat held until someone cleared it by hand. It now asks the Hub about each held job
every minute, and the three answers are kept apart: the Hub says it finished → the job is closed; the
Hub has never heard of it → **nothing was done, so the job goes back in the queue**; the Hub cannot
answer → the hold stays, and stays visible. A hold that still needs you means the answer was genuinely
unknown, not merely slow.

### The queue says how long a video may take

A waiting video job now publishes the lane's own **deadline**, so a two-hour wait reads as normal
rather than stuck. The number comes from the model's published contract — the local video lane states
7,200 seconds — and not from a constant in this app.

---

## 0.2.66 — 2026-09-28

### What's New is a page now, in the sidebar above Settings

The notes used to be a window of their own, opened from a small link at the bottom of the sidebar.
They are a **page** now — a row in the sidebar directly **above Settings**, under the *App* heading,
which is exactly where Studio Hub keeps them. The View menu and the Overview button still reach it;
they select the page rather than opening a modal.

The page itself is unchanged, and it is Studio Hub's: *What's New* with the version beside it, this
build's stamp on the right where it annotates instead of interrupting, a rule, then the notes as
headings, paragraphs and bullets. Bold, italics and `code spans` render as themselves rather than
being shown to you as characters.

---

### The fleet page shows who has joined each controller

Every controller's roster was fetched and thrown away, so the only way to see which Macs had joined
one was to open Studio Hub on it. The **Locations** tab now has a **Members** column
(`reachable / total`, plus how many were refused), the **Machines** tab has a **Node** marker and a
**last seen** column, and a Mac whose credential the controller refuses says **credential refused**
instead of sharing a word with "switched off".

### There is a queue you can look at

Every waiting job now publishes its **position** and how many are waiting with it — before this, a
job sitting in the queue looked exactly like one that was running. The console's Providers page has
a **Queue** section: the waiting jobs with their age, why each one is waiting, and **attempt n of 3**
with the last reason. It also shows what is bounding each lane and each site, which is the number
that answers "why is nothing running?".

### The number Story is told is what the fleet can actually do

The gateway used to tell Story Studio it could run **60** jobs at once because that is what a
setting said, while the machines could do about **4**. That number is now measured: the sum, over
every reachable controller, of what admission itself will allow — per lane and in total. Sell two
Macs and it falls; buy three and it rises, with nothing to edit. The old pool is still shown beside
it, named as the pool, so the two cannot be confused.

### One model, ticked once, for the whole fleet

Adding a controller used to arrive with everything switched off, so the same model had to be ticked
on every Mac — and the console drew one row per Mac, so "Z-Image-Turbo 6B MLX 4-bit" appeared six
times with nothing to say it was the same model. A new location now **inherits this Mac's own
choices** for the same models, and the Models page draws each model **once** with a **Machines**
column reading *"N of M machines"*. One tick switches a model on for every Mac that has it.

### Images are published at the size the Macs really render

Story Studio was told 16:9 was 1536×1024 and 4:3 the same, while the Macs render **1280×720** and
**1152×864** — and the video-valid **1024×576** frame was hidden entirely. The geometry now comes
from each model's own lane contract and the Hub's own preset table, so what you pick is what you
get. Cloud providers are untouched.

### A failed job is retried on a different machine, and never twice

A job that fails for a reason the gateway can *prove* — a busy Mac, an engine out of memory, a Mac
that is off — is tried on a **different machine**, up to three attempts, and then fails properly
with a reason naming every machine it was tried on and why each one failed. A Mac that accepted the
work and then went quiet is **never** re-run automatically: it might be rendering right now, so it
waits for you to reconcile it. The whole policy is in `docs/job-retry-policy.md`.

### The local model says how it keeps a JSON promise

A chat row now carries its lane's own statement about structured output, at the path
Studio Hub named — `lane_contract.structured_output`: which formats it takes, how
the guarantee is kept (**prompt, validate, repair** — not a grammar), and how many
repairs it costs.

Two things this deliberately is not. It is **not** a `two_pass` flag: asking twice
is a caller's technique, and a model cannot know whether the caller wants it — the
row states the facts that decide it. And it is **not** guessed from the model's
name: the gateway reads the lane's declared parameters, because three defects in
this project came from matching a name where a fact could have been published.

Relatedly, a lane that declares a `response_format` parameter now publishes
`supports_structured_output: true`. The *mode* stays `null` until the producer says
which one machine-readably, rather than this gateway picking for it.

Note Studio Hub's own caveat, kept rather than smoothed over: **no
grammar-constrained decoding**, so a strict-JSON call site relies on prompting plus
validation.

### The Fleet page updates when you come back to it

It read the fleet when you opened the page and then never again on its own, so the
loop this fleet is actually built around — walk to a Mac, fix it, come back to the
window that was already open on **Fleet** — showed the fleet as it was *before*
your fix. Green machines still read red, and it looked like the fix had failed.

Now the page re-reads when you return to the app (or switch back from another of
its windows). It is one read, the same one the **Refresh** button makes, and only
for pages that are read-only — a page with a form open is never re-read behind you.

### Local LLMs now run through the gateway

Studio Hub 0.6.63 opened its chat lane to a Fleet credential, so a Hub LLM is now
a real lane here: enable it on **Models** and Story Studio can use it like any
other chat model.

- **It dispatches through the Hub**, not straight to a worker, so admission, the
  machine lease and worker signing all apply exactly as they do for the Hub's own
  Chat page.
- **Only the parameters that lane honours are sent** — a parameter it would
  silently drop is never offered to a caller as if it worked.
- **The worker's own generation time is published as its own number**
  (`generation_seconds`), never as this gateway's round trip, and `finish_reason`
  and token counts travel exactly as the worker stated them.
- **Token counts are labelled as passed-through, not re-measured**
  (`verified_token_usage: false`): the gateway validates what the worker states, it
  does not independently recount, and the catalogue now says so.

Two limits worth knowing before you rely on it: the lane has **no
grammar-constrained decoding**, so a strict-JSON call site depends on prompting
plus validation; and a prompt past the model's stated context is refused by name
rather than silently truncated.

### Studio Hub now tells the gateway how to run each model — and the gateway reads it

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

### The gateway says which Studio Hub it is built for — and keeps working

Studio Hub moves on its own schedule. It updates itself, and this fleet has run
`0.6.18`, `0.6.23`, `0.6.29`, `0.6.31`, `0.6.54` and `0.6.60` in a week. So the
pairing is now **declared** instead of assumed: this build targets **Studio Hub
0.6.60**, says so on `/health`, and shows it beside each controller on
Fleet → Locations — `0.6.60`, `0.6.54 · older`, `0.6.72 · newer`.

**Nothing is refused on a mismatch.** A Hub that updated itself must never take
your fleet down while nobody touched it. A drift is one line in the Fleet issues
under "informational — nothing to fix", naming both versions.

### A model you add on the Hub arrives here — even on an engine this build has never seen

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

### A dead node now says why, once

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

### A stuck job can be cleared again

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

### One model, one answer about whether it can run

A model offered by several locations is published as **one** row — and that row was
mixing two locations' answers. With this Mac's Studio Hub down and both controllers
up, a model read `locked: false` beside `availability: "unavailable"`: the gateway's
answer and the catalogue's, disagreeing in the same payload. Both spellings of that
one fact are now written together.

### The Models tile is a real fraction

Fleet → Overview showed **"175/102 local models available"** — more models available
than models. The numerator counted one entry per machine and the denominator one per
model. Both halves now count models, so the tile reads `N of M` and means it.

### Adding a location no longer replaces one

Every Studio Hub that has never been given a site id answers the **same** one —
`local-site`. The gateway had started identifying a controller by that value, so
adding a second such controller matched the first row and overwrote it: the roster
looked like it could only hold so many locations.

Add as many controllers as you like now. A Hub with its own site id keeps it, and a
Hub still answering the default is told apart by its per-machine id instead.

### The gateway stops offering a knob the model won't take

The last release made a refused temperature survive; this one stops asking for it.

- **A model that accepts only its own default temperature is no longer offered a
  temperature control.** Story Studio sends `0.7` whenever the catalogue offers the
  control, so OpenAI's reasoning models (`gpt-6-luna`, the `gpt-5`/`gpt-6` family,
  `o1`/`o3`/`o4`) were answering `400`. They now run at their own default instead.
- **Scoped to the API that refuses it.** A proxy that accepts `0.7` — Experiential
  Labs' `gpt-5.6-luna` did — keeps the control, and any provider that publishes its
  own parameter list is always believed. The one-retry-without-temperature from
  0.2.19 stays as the safety net for a family this table has not learned yet.

### A provider's “no” is a failure, not a mystery

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

### A release is never pushed backwards

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

### The default port is now 8787

A fresh install listens on **8787** — the port this Mac's working instance already
uses. It was 8000, which is Story Studio's *own* default and the most contested dev
port, so "is this pointed at the right thing?" was never answerable from the number
alone.

- **Existing installs are unaffected.** Their `.env` and Settings keep whatever port
  they already set; only a machine that has never chosen one gets 8787.
- The port stays editable in Settings, and `README.md`'s port section now says 8787.
- Point Story Studio's Base URL at `http://<host>:8787` (Settings → Copy).

### Publish Studio Hub app releases from the gateway

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

### Fleet: "the site is down" no longer reads as "no such model"

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

### Fleet: the Hub explains its own activity gaps

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

### Fleet: an agent-mode Hub is never sent customer work

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

### Fleet: the Hub's own answers, and Retry

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

### Fleet: only what is actionable, and one line per condition

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

### Fleet: placement weights the Macs that can run the model

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

### Fleet: stop a running batch

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

### Fleet: one page per question

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

### Models: browse the catalogue by operation

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

### Runs on older macOS

GatewayKH now states its own **macOS 11** minimum instead of inheriting whatever
macOS the build toolchain shipped. A build made before this change stamped a
macOS 27 minimum and would not open on macOS 14, reporting "requires macOS 27".
The same code was always fine on macOS 11; the build just never said so.

### Smarter fleet generation

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

### Content Packs: install a pack someone sent you

The **Content Packs** page sets up a private shelf once — your own signing
identity, separate from the old Store — then you add a `.sspack` someone sent
you, and Story Studio discovers it through the same Gateway URL and installs it
with its normal Download/Install. The shelf publishes; it does not author.

- The shelf accepts only **newly reserved** pack identities, so historical Store
  packs are never reissued, re-signed or claimed as new.
- Back up the identity *and* its releases with
  `scripts/backup_content_authority.py`; the shelf cannot serve a release whose
  archive is missing.

### Run Story Studio from another Mac

The Gateway can serve your whole Tailscale network now, so Story Studio on
another Mac connects by pointing its **Base URL** at this Mac's Tailscale
address. **Settings** shows that address with a **Copy** button. The gateway also
moved off port 8000 (a very common port, and Story's own default) to its own
port, so there is never a question of which service answered.

### Smaller things

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

### More predictable Create and queued jobs

Refreshing models keeps your selection. If that model disappears, Create asks
you to choose again. Generate stays disabled while the current request is pending;
a temporary status-check failure is shown and retried without submitting again.
Cancelled jobs no longer show an endless spinner.

Cancelling a queued job now prevents dispatch even when the worker has already
read its queue. This does not stop work already accepted by an upstream provider.
OpenRouter's batch-only models are withheld from ordinary chat, and network
access requires a configured gateway key.

---

### Your voices now work on every model that can clone

Only **one** of your eight local voice models offered any voices. Every voice you
imported was stamped with the model it happened to be tuned against, and a voice
was matched to a model by that exact string — so picking OmniVoice or VoxCPM2 for
quality gave you an empty voice list, which reads as "you have no voices" rather
than "those clips belong to another build". **All eight now offer all ten**, and a
model that says it does not clone still gets none.

### Your recordings are never shortened

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

### Fixed: a location showed this Mac's models

A Studio Hub location added on the Fleet page discovered **this Mac's** model list
instead of its own, because one discovery path skipped the location's address and
fell back to the local Hub. Two sites would have shown identical model lists, and
work routed to one could have been sent for a model only the other has. Fixed, and
now tested.

---

### The app now stops the gateway when you quit

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

### Every build identifies itself

The app and the gateway now both carry a build stamp (commit + build time).
`/health` reports it, and the app compares it, so "am I running the latest?" has an
answer instead of a guess.

---

### New providers and lanes

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

### Voices

- **Add by ID.** Paste a voice id from a share link and save it. ElevenLabs also
  needs the owner id, which the window asks for.
- **Search the provider's public library** still works, and now a voice you added
  by id is offered wherever it belongs.

### Nothing broken is offered any more

The Models page no longer lists a model that cannot run. That removed:

- nine OpenAI transcription models — only `whisper-1` can return the SRT this
  gateway must send
- Google's Lyria on the free tier, which has no Lyria
- OpenRouter's Lyria rows — it lists them but publishes no way to call them

If a row is there, it runs.

### Things that were wrong and are fixed

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

### Artifacts

Received artifacts are temporary: kept **7 days or 10 GB, whichever comes first**,
editable in Settings. Nothing under an hour old is ever removed, because a
freshly-written artifact is about to be fetched.
