# What's new in gateway-kh

Newest first. This is the same list the **What's New** link in the app shows.

---

## Content Packs: make your own, and install them in Story Studio

You can now build your own Content Packs and have Story Studio install them with
no Store account, no purchase and no credits.

- The **Content Packs** page sets up a private shelf once — your own signing
  identity, separate from the old Store — then you author a Topic Pack draft,
  review it and choose **Add to shelf**.
- **Story Studio discovers shelved packs** through the same Gateway URL and
  installs them with its normal Download/Install.
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
