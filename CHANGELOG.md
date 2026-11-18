# What's new in Gateway Studio KH

Newest first. This is the source for **What's New** in the app and web admin.

Each release carries its version and date, the way Studio Hub's notes do, so an entry says *when* it
arrived instead of only what it does. `tests/test_whats_new_page.py` fails if the newest section is
not a `## <version> — <date>` heading matching `VERSION`, so the next release cannot forget. The rows
below the newest group are the history from before this shape existed: their titles are `###` like
every feature title, and they are left as they were written.

---

## 0.2.282 — 2026-11-18

- **No machine name ships in the app's own history.** 0.2.281 removed the machine names and the tailnet address from the code comments bundled in the app; a read-only scan of the built DMG then found one more in the bundled release history ("kept on macmini-angkor…"), which is public text inside the download. It now reads "kept on one Mac". The history stays bundled — What's New reads it — and the release is scanned before it is published: no machine names and no tailnet address outside the generic ranges in the DMG.
- **The app carries no builder's home path either.** Seven of the bundled Python helper commands (dotenv, fastapi, httpx, idna, uvicorn, watchfiles, websockets) were written with the builder's absolute interpreter path at the top of the file, so the installed app still mentioned `/Users/…` after the name and address scrub. The build now points every bundled command at the interpreter that sits beside it — the same way the runtime's own commands already work — and each one was run to confirm it still starts.

---

## 0.2.281 — 2026-11-18

- **What the fleet knows about a site survives a restart.** The roster each site last reported lived only in memory, so 0.2.280's own install restarted the Gateway and every unreachable site came back with no known Macs and "never heard from" — the count read as zero, which is the same "no data looks like none" problem one restart later. Each site's last roster and the time it answered are now saved and read back at boot; after a restart, a site that is unreachable still says when it was last heard from, and its Macs are counted as **not checked**.
- **A site never reached says its Mac count is unknown, not zero.** The headline reads "at least N Macs not checked" when one of the silent sites has no known roster, names how many such sites there are, and the machine list says the total cannot include them. A fleet total can be incomplete only if the page says so.
- **No private fleet names or tailnet addresses ship with the app.** A scan of the 0.2.280 binary found nine machine names and one live tailnet address inside code comments that are bundled into the app; they now read "a fleet Mac" and "a tailnet address". Comments only — no behaviour changed. The generic `100.64.0.0/10` range and the `100.64.0.9` placeholder stay.
- **The worked example is a restart.** The test answers from all eight sites (46 Macs), restarts the process, then leaves one site (four Macs) unreachable: the fleet still totals 46, with four not checked and that site's last-heard time intact. A never-reached site reports its count as unknown.

---

## 0.2.280 — 2026-11-18

- **The Fleet says what it did not check.** When a site does not answer, its Macs were silently subtracted from every count — on 2026-11-18 three sites timed out in one read and the headline fell from 29 online to 8 with nothing on screen saying why. The summary now publishes **Macs not checked** as its own number, taken from the roster each silent site last reported, and the lead reads it beside the rest: `8 online · 42 Macs not checked · 6 sites not answering`. It is never folded into online, offline or unknown, and the fleet total does not shrink.
- **A silent site is "Not answering", not "Offline".** Its row says why in plain words — no answer in time, refused the connection, answered with an error — and when it was last heard from ("last heard 21:32", or "never heard from"). Its Members cell says those are the Macs it last reported, not a live count.
- **A slower probe bound, per site.** Six seconds was tight enough that busy controllers read as unreachable and flipped back within 32 s. The default is now 10 seconds, and a site that has answered slowly gets up to three times its own last response time, capped at 20 seconds. A site that answers — even at 5.9 or 6.1 seconds — is measured normally; only one that exceeds its own bound becomes "not checked", and its Macs stay in the total.

---

## 0.2.279 — 2026-11-18

- **Jobs leads with its answer.** The page opened on a bare "latest 100 jobs" table, with the state spelled the Gateway's way (`accepted`, `queued`). It now opens with the count of what the loaded rows are doing — running · waiting · succeeded · failed · cancelled — and each count is the filter for it: click "waiting" and the table holds only those, say how many of the read it is showing, and a Show all restores it. The raw Gateway word stays in the badge's tooltip. The line says plainly when it is the latest 100 and not every job the Gateway has run.
- **States read as words on the Create page too.** Its in-progress line and its recent list use the same words ("Waiting", "Running"), and a deferred job's badge now says `queue #3` beside the state instead of repeating "waiting".
- **A menu stays with its button.** Fixed-position overflow menus keep the coordinates they were given, so a menu left open while the page or a table scrolled sat away from its button, and one on a row near the window's bottom opened past the edge. Placement now measures the menu after it is shown and flips it above the button when there is no room below; any scroll or resize closes it. (0.2.278's note said scroll closed them; no listener existed.)
- **The audit is held by tests, not memory.** A new page-wide check fails if the console ever uses the browser's own `confirm`/`alert`/`prompt` (silent no-ops in the packaged webview) or if any inline handler names a function that does not exist, and every sortable header must be a real button that sorts its own table. The rest of the console was walked page by page in the same pass and needed no change.

---

## 0.2.278 — 2026-11-18

- **The Fleet page now answers the owner's question first.** A large "running now" count and its split (generating · ready · idle · need attention · just finished · offline) sit above the panes, then a worst-first list of what is failing per location ("Main — 1 offline", "1300 — whole site unreachable 46 h"). Each number and location filters the table to those rows; the page says how many it is showing.
- **The machine table is six columns.** Machine · State · Version · Disk free · RAM free · Network. The cause sentence, health history, drops, gate facts and every action moved into a Details row under the machine — one click, not the scan path. A carried row keeps no actions and says it was not measured this read.
- **Locations show one state word, not a paragraph.** Offline / Not answering / Slow / Working / Paused, with the old sentence in the cell's tooltip and the location's Details.
- **"More…" on a Locations row now opens.** It was rendered inside a scrolling table wrapper and clipped out of view; menus now open fixed under their button.
- **A removed machine stays removed.** Force removal purged this Gateway's records, but the next read rebuilt the row from the controller's report, so the success was true only here. A tombstone per (controller, machine) now suppresses it from every view and count; a fresh report from that machine clears it (a sold Mac never reports), Undo clears it on demand, and the page says once "1 removed here, still registered on its site" with Undo beside it. The removal response and the in-app message say which of those happened.
- **Shop Macs are hidden by default, behind a switch.** The Shops location starts hidden; "showing its Macs" and the "N machines from Shops hidden — show" line in the lead both flip it, the choice is remembered, and the Location filter still reaches them. They leave the headline counts too, so the number always matches the table.

## 0.2.277 — 2026-11-18

- **Every column on the Locations table now sorts.** Location, Health, Likely cause, Network, Hub, Members, Models and Status looked sortable and did nothing — they were the only headers on the Fleet page without a handler. Each one now sorts with an arrow and `aria-sort`, worst-cause and health first as the cards do, numbers as numbers, and a value nobody reported last whichever way the arrow points.
- **One line above the Fleet page says what the fleet is doing now.** "20 generating · 2 ready · 2 idle · 4 need attention · 1 just finished · 13 offline · 9 sites not reporting", in that order, every figure from the same read the tables draw. Clicking a number opens the table filtered to those rows and says "Showing 20 generating"; "Show all" clears it, and the existing Reset filters clears it too.

## 0.2.276 — 2026-11-18

- **"Start Hub Studio KH" opens the Hub — and says so when the Hub is not installed.** The Hub app was renamed from Studio Hub to Hub Studio KH, but the menu's start item still looked only for the old name, so on every Mac with the renamed app the item was there and did nothing. It now looks for `Hub Studio KH.app` first, then the old `Studio Hub.app` so a Mac that has not been renamed yet still starts, and when neither is installed the item is disabled and reads "Hub Studio KH is not installed on this Mac" instead of being a dead control. The menu's status line and the Overview hint use the new name too.
- **A test holds the lookup.** It pins the renamed app winning, the old name still starting a Mac that has not been renamed, and neither app resolving to nothing, so this cannot quietly rot again.

## 0.2.275 — 2026-11-18

- **The setup kit now sends you to the app that exists: Hub Studio KH.** The Hub was renamed from Studio Hub to Hub Studio KH, and this kit still told you to open a `Studio Hub KH/` folder and clear quarantine on `/Applications/Studio Hub.app` — a name that is no longer on any Mac. Every operator step now says `Hub Studio KH/` and `Hub Studio KH.app`. This release is documentation only: no code, no setting and no behaviour changed, and version numbers, bundle ids, ports and data paths are exactly as they were.
- **The fleet notes use the same name.** "On each controller, in Hub Studio KH" and "Start at login for Hub Studio KH" now match what is on the Mac.

## 0.2.274 — 2026-11-18

- **Token usage is now recorded for every job, including the ones that fail.** A job that ran out of room, was stopped, timed out or was cut short by a content filter has burned tokens too — until now only a successful job carried a usage block, so the cost of the ones that went wrong was invisible. Every job that reached a model now shows what it used, with the partial answer's figure kept on a filter stop.
- **A pooled model shows what each model in it cost.** A walk that stops on the third model records what the first two spent before they gave up, one line per model, and says "not reported" where a model stated nothing.
- **"Not reported" never reads as zero.** A number the provider did not state is blank and labelled, so an empty figure cannot be mistaken for a cheap one. The `verified_token_usage` flag now sits beside a `usage_source` field saying the figure came from the provider and was passed through rather than re-measured.
- **The Jobs page shows the tokens.** Each row gains a Tokens column with the job's own total and a line per attempted model in a pool.

## 0.2.273 — 2026-11-18

- **A job whose model is a pool is tried once, not three times.** The pool has already asked every model in it before it gives up, so running the whole chain three more times pays each provider again and again. A pool job now ends after one walk, and its row says "1" allowed so the number is visible; other models keep the three machines. The number is a setting, one by default.
- **A model's own content filter no longer ends the pool.** When one member refused its own answer mid-stream (a content filter on an ordinary script), the Gateway stopped there and never tried the healthy members after it — eight chat jobs failed that way. A refusal of *that model's answer* now moves to the next member and is recorded on the attempt, naming which model refused and why; only a refusal about the request itself still stops, because every member would give the same one.
- **The app's own test no longer opens Music.** The Jobs window's Preview hands a local copy to macOS; the test that clicks it handed over an audio file, and macOS launched Music in the foreground during a gate run. Preview now goes through an opener the test replaces, so the check proves the same thing (a local copy was written and opened) without touching the desktop.

## 0.2.272 — 2026-11-18

- **One finished job can now be retried on its own.** Cancelling or losing one job in a batch no longer forces the whole batch to be sent again: a finished row on the Jobs page has a **Retry** button that queues a fresh job with the same model, parameters, group, priority and requester, leaving its siblings and the finished row untouched. The new row says which job it came from, is counted as a separate job, and a job that is still running, still queued, or still waiting on a Hub recovery answer is refused with a sentence saying why.

## 0.2.271 — 2026-11-18

- **A job now ends when the thing running it stops making progress.** A job the Hub reports as stalled — no progress past its own threshold — used to ride on to the lane's deadline with its row still saying "running". It now ends at once, saying how many seconds nothing moved and what the Hub's threshold was, and keeps its batch id; the old batch is recorded so the cleanup stops it first, and the row says a retry is safe after that. A late answer is still kept beside the verdict.
- **A job nothing is watching can no longer stay "running" for ever.** If the part of the Gateway that follows a job is gone — it died, or a restart could not pick the job back up — the row used to sit there indefinitely. Once it is past the longest a Hub job can legitimately take, the Gateway asks the Hub once and ends it: collected if the Hub had actually finished it, ended with "the Hub still has it" if it is still there, and ended as retryable if the Hub never heard of it at all.
- **How long a job is held to now comes from how long that model actually takes on that Mac.** The bound is the machine's own measured run time (its median, else its average) times three, never shorter than 15 minutes and never longer than the lane's own ceiling; a Mac with no history for that model uses the fleet's usual figure for it, and a model nobody has run yet falls back to the lane's ceiling rather than a guessed number. The row prints the figure it is being held to and where it came from, so a wrong bound can be seen instead of guessed at.
- **A job waiting for a Mac to start it says so.** Its waiting line now separates "the Mac has not started it" from "it is running slowly", and names the same measured bound.
- **Following a job again no longer restarts its clock.** Each re-follow used to begin the wait over, so a flapping connection could keep one job alive for several windows. The whole wait is now the lane's own ceiling, however many times it is followed.

## 0.2.270 — 2026-11-18

- **Removing a Mac or a location removes every trace of it, and connecting it back works.** Removing a location used to forget only its name and its key: its history, its settings and its jobs stayed behind and the page kept drawing a site nobody had. One action now removes the whole record — the row, the credential, the models, its reachability history, its Lite Mode settings, its render opt-in, its image-model pins, its network names, its limits, and every job, summary, render row and file it owned. One Mac can be removed on its own the same way, and its controller is asked to delete it and revoke its credential. A Mac the controller no longer knows is still a clean removal; a controller that cannot be reached asks first, and can then be removed here only. Connecting a Mac back is its own action — the controller issues a fresh key and the Mac comes back as new, without its old jobs or history.
- **A job held on a Hub's answer now ends when the Hub keeps saying "running" past the ceiling.** The 0.2.269 fix covered holds no lane could be asked about; this is the other half: a hold whose Hub has answered "still running" for longer than the Hub's own four-hour ceiling plus the grace now ends as `HOLD_EXPIRED`, keeping its context, its batch id and a late result beside the verdict.
- **The Fleet page shows when the free-space warning was measured.** The disk warning and its Jobs-page banner now carry the time the Hub measured it, so an old number cannot read as right now.

## 0.2.269 — 2026-11-18

- **A job whose outcome can no longer be learned now ends, instead of saying it is still running.** A Chat call leaves no batch record any lane can be asked about, so when one was interrupted the Gateway kept answering `running` — for ever, with no failure and no Cancel. Such a hold now ends after its own ceiling (the Hub's four-hour Chat ceiling plus the usual grace) as a failure that says how long it waited and why nothing could settle it. Nothing is thrown away: its batch id, context and attempts stay on the row, and a late result is kept beside the verdict.

## 0.2.268 — 2026-11-17

- **The Fleet page says who paused a location.** A pause now records who made it: the app's own signed-in session reads "Paused by you", and a pause made through the API names its caller — "Paused by an API key", "Paused by a local API client (no credential)", or a name the caller gives. Before this, every pause, whoever made it, said "Paused by you"; three locations read that way on 2026-11-17 when no person had touched the page. Resume clears the name along with the pause.
- **A job no longer waits 45 minutes on a site that answers nothing.** If the site a job is waiting on cannot take it at all — it is not answering, or it says it has no Mac that can run the model — the job is moved or ended within five minutes, saying which it was, instead of standing in a line that will not move. A site whose Macs are merely busy still keeps the full wait.

## 0.2.267 — 2026-11-17

- **The Fleet page says when each warning was measured, and stops repeating old ones.** Every attention row now shows the cause with the time it was measured. A Mac nobody has heard from inside Hub's reporting interval reads "Not checked in since HH:MM" instead of looking broken, and a fault that has cleared disappears instead of waiting for the next successful job. How old a reading may be is adjustable.
- **Gateway no longer reports itself healthy while it is not dispatching.** If it cannot read its own queue, its health says so — it used to show a fresh check with no failures while every job sat waiting.
- **A render the destination keeps refusing ends, with a reason.** One render produced six thousand log lines over eighteen hours because a single good answer reset the failure count. It now ends after 30 refusals or two hours, says what the destination kept answering, and Story falls back to a local render.

## 0.2.266 — 2026-11-17

- **The app can no longer break its own signature.** A Python file written inside the signed app makes macOS refuse it at the next launch — on the release Mac that took Gateway down for three minutes on 2026-11-17. The bundled Python now refuses to write those files whoever starts it: the app, a script, or a person at a shell. A 30-second run of the built app now leaves the signature valid and writes nothing.
- **A job waiting for a Mac that never frees now ends instead of waiting for ever.** Past 45 minutes (editable in Settings) it either moves to another Mac that can run it or ends as retryable with the site and the minutes waited — "nothing was sent, retrying is safe". It no longer sits on "waiting for a free Mac" with no deadline.
- **Story no longer sees the interruption behind a job.** A work item whose Mac, site or Gateway was interrupted stays `running` until the Gateway has settled it, and reaches Story as one of `queued`, `running`, `done`, or a failure no retry can fix. The reconciliation hold is still recorded and still shown on the Jobs page for the operator.
- **"No Mac can run this right now" is a wait, not a failure.** A paused, offline, out-of-memory or not-yet-read Mac used to fail the job with "No reachable Mac can run …" after 30 seconds. The job now stays queued with that reason on its row and is ended only by its own time limit.
- **The one time limit is on the row.** Every queued job now says how long it may wait and how much of that is left. It is the same editable ceiling in Settings.

## 0.2.265 — 2026-11-17

- **Gateway starts at login again, and still needs no certificate.** After an update to 0.2.264 the app stayed down until someone started it by hand: an older registration was still holding the app's name, and 0.2.264 no longer carried the file it needed to cancel it, so its attempt to load its own login item was refused every time. The app carries that file again, cancels the old registration, and loads its own login item; if the old row is still in the way it is cleared once and the load is tried again. Both outcomes are written to the log instead of failing quietly.
- **The Fleet Trash page says which "off" a Mac is in.** One Mac's trash can be off because nobody ever turned it on, because the owner turned it off, or because the automatic rule is not running. The row said one sentence for all three; it now says **Off — never enabled**, **Off — you turned this off**, or **Off — automatic**, and a Mac that does not report it shows nothing rather than a guess.

## 0.2.264 — 2026-11-16

- **Fleet has a Trash tab: every Mac's trash on one page.** One row per Mac, with the Studio Hub Trash Folder, macOS Trash, free disk and free memory as separate columns, so a shortfall in memory never reads as a disk one. A field a Mac does not report says "not reported" rather than 0, and a Mac that cannot be read says why.
- **Empty All Trash: one button for every Mac at every linked site.** It reaches every node and controller, this Mac included, and removes the Studio Hub Trash Folder and everything in macOS Trash (needs Studio Hub 0.6.243). One confirmation lists each Mac's count and bytes and the total; each Mac's own Empty stays. A Mac whose switch is off, whose Hub is older, or that is offline is skipped and says why; every Mac ends emptied, skipped or failed, and a failure says what is left. Agents can run it too with the gateway key (`POST /admin/api/fleet/trash/empty`); each run writes one audit line naming who asked. Every other delete still needs this app's own window.

## 0.2.263 — 2026-11-16

- **A Mac that refused a job without starting it no longer uses up one of its three tries.** Out of memory, out of disk, not ready, unreachable or busy: nothing was sent, so the job moves to another Mac and keeps all three tries for real failures. Each Mac that refused is still named, and a job no Mac could take ends with "Nothing was charged; run it again".
- **A full disk is named and avoided.** A Mac with no room for the result now says `OUTPUT_STORAGE_FULL` and is left alone for 15 minutes. A site whose controller's disk is stopped, or whose every able Mac is out of disk, is offered voice, music and video work only when nothing else can take it. A job already waiting on such a site moves on instead of waiting there.
- **A site that refuses connections gets no new work until it answers again.** Gateway notices the refusal itself instead of waiting up to 30 seconds for the next fleet check, so voice sections stop being sent to a site that is down. A submit that could not connect costs no try. A job whose site dropped mid-job moves to another Mac instead of being held there.
- **"Attempts" now shows the count that ends a job.** Refusals that accepted nothing are listed but not counted, so a job never reads "6 of 3".
- **A restart no longer fails queued work.** Right after Gateway starts, a site it has not read yet is "not read yet", not offline. Queued jobs wait for the first fleet check (at most five minutes) instead of failing with "No reachable Mac".
- **A paused site gets no new work after a restart either.** A job whose send was cut by the restart goes back to the queue and moves to a live site, instead of being sent again to the site the owner paused.
- **A stop now leaves a reason behind.** The app writes who asked it to quit into its log, and the gateway writes a line when it is told to shut down.
- **Launch at login works for this build, and no longer depends on a signing certificate.** The login item is now a standard macOS login item file that names the app; the previous kind recorded a code signature that changes with every build, so macOS refused to start the app at login after an update. Uninstalling still removes it.
- **Launch at login keeps working after an update.** The app registers its login item again once for each new build, so the record macOS keeps always matches the installed app. A login launch that macOS refuses now stops after one try instead of retrying for ever.
- **Every lost-contact hold can be swept.** Each one now keeps the batch it is waiting on, so the hold sweep can collect a batch that finished instead of leaving the hold for a person to close.

## 0.2.262 — 2026-11-16

- **The Launcher can request an update pause with its own private token.** It works only for pausing and resuming Gateway's queue, and cannot open Gateway's other settings. Gateway never creates or copies this token.
- **Missing, incorrect or replaced update tokens leave the queue untouched.** Gateway reads the Launcher's current token for each request, so a replacement takes effect without restarting Gateway. Requests from a browser or another machine are refused. An update pause waits at most 30 seconds for work in flight; queued work stays queued, and the pause expires by itself if an update is abandoned.

## 0.2.261 — 2026-11-15

- **The Models page stops showing blank cards and counts it cannot draw.** Empty categories were full-height cards holding one sentence, and the chat card said "8 of 1613 offered" above a single inline model row, because the count came from the server summary while the list came from the rendered rows. One list now drives both: the count is the rows drawn, a subset says how many it is showing, and the rest is one action to see them; a category with nothing offered is a compact line with a Discover action, and the full-catalogue link is the primary action.

- **A stuck job can be ended for good from the app.** `POST /admin/api/jobs/{id}/discard` is the terminal escape for a row that is not a recovery hold: a `running` job whose upstream is gone used to be unkillable — `cancel` only *requests* for a running job and waits for a worker that will never answer, and the discard-uncertain action refused anything without a recovery hold (seven Story jobs measured 2026-11-15 sitting at top-level `running` with terminal linked Gateway jobs and no way to end them). Story now calls this route: an `accepted` row is cancelled outright, a `running` row tries the lane's own discard route and otherwise closes here with the same honest sentence the hold path uses (the provider was not told and cannot be; a late result is still the job's and is kept beside the verdict), a recovery hold delegates to today's Hub-discard path unchanged, and an already-terminal row is untouched — calling twice changes nothing. Who ended it and why (`by`, `reason`, origin `discard`) is recorded on the row; the row itself is never deleted.

- **A job waiting for a pool slot now shows its own deadline.** The wait was already bounded — `pool.member_wait_seconds` (15 min by default, editable), and past it the pool refuses with `POOL_MEMBER_BUSY` — but the published reason said only "waiting for a Magpie slot", so a row legitimately waiting could not be told from one that was wedged; seven pool jobs measured on 1301 sat that way for 943–954 s and then ran and succeeded. The reason now reads "waiting for a Magpie slot - up to 15 min, about 6 min left" and counts down, re-announced only when the remaining minutes change.

## 0.2.260 — 2026-11-15

- **A restart can no longer leave jobs looking like they are still running.** 0.2.259's fix — quitting the app when nobody can answer a dialog — made real restarts of a busy Gateway routine for the first time, and the first one on 1301 exposed this: the new copy opened the queue while the old copy was still draining, lost the queue lock, skipped the restart check and served anyway, leaving in-flight jobs reading "running" with nothing behind them and no way to clear them. A starting Gateway now waits for the old copy to let go (up to 20 seconds, longer than the shutdown wait), and if it still cannot take the queue it says so loudly in the log and does not dispatch work it cannot account for. Every launch now writes its build, pid and whether it owns the queue, so this is answerable from the log instead of from the database. A cancel that was waiting for a worker which is no longer there now settles with the job instead of sitting "requested" forever: a running job with a pending cancel ends **cancelled** — never held-with-cancel-pending — which is the terminal state Story's reconcile expects.

## 0.2.259 — 2026-11-15

- **An installer can quit the Gateway again.** The app asked a modal "Quit the gateway?" whenever jobs were running — but when an installer sends Quit and nobody is there to answer, that question never returned, so the app never quit and every install on a busy Mac failed. It now asks only when the window is actually in front of you; otherwise it stops the gateway and quits. A 40-second deadline now guarantees termination in every case, so a dialog can never hold an installer open. Quitting from the app with work in flight still shows the warning.

## 0.2.258 — 2026-11-15

- **A request with an output cap now works on a model that wants `max_completion_tokens`.** Some OpenAI-compatible models reject `max_tokens` outright ("Unsupported parameter … Use 'max_completion_tokens' instead"). The Gateway now recognises that refusal by the provider's own words, re-sends once with the field the model named (a 400 means nothing ran and nothing was charged), and says so in the job's `params_dropped`. The model is remembered for the rest of the process, per provider, so later requests go straight to the right spelling.

## 0.2.257 — 2026-11-15

- **Models answers "what have I got?" first.** It opens on **Your models** — a card per kind (Image, LLM, Voice/TTS, Music, Video, Transcription) with how many are offered, how many are installed on this Mac, and the models themselves. **Discover** holds the full catalogue with the filters, search and bulk actions.
- **A model is one row, wherever it runs.** The Macs are a count (`7 of 7`) and **Show locations** in the row's … menu; each Mac keeps its own switch. The provider filter now lists providers, with **Hub Studio KH (local)** as one entry, and the Macs are a separate Locations filter.
- **Voices names the real provider** (ElevenLabs, Fish Audio, Hub Studio KH (local)) and says how many locations can serve a local voice, with tone, best-for, sample length and the id behind Details.
- **Create asks only what the chosen model can answer.** Image offers its own aspect ratios and sizes with the price beside the size; Video offers its own durations, resolutions and shapes; Music shows a lyrics counter and an Instrumental switch where the model supports one; Audio asks **Saved voice or Provider voice ID**, one choice at a time. Generate is disabled with one line naming anything missing.
- **A stock footage library lives in the Gateway.** Clips are kept once by content (sha256), measured (length, size, frame rate, licence), and Story Studio reads and downloads them without ever changing them. The folder is chosen with a picker — mounted volumes and folders, no typed paths.
- **Free stock is on, paid stock is off.** Wikimedia Commons works with no key; Pexels and Pixabay stay off until their key is saved and their switch is on, and each shows its cost before anything is fetched. Clips your own Macs generate land in the same library, marked as AI-generated.
- **The video catalogue is ready for Studio.** Every video model publishes its kinds, durations, resolutions and ratios with where each fact came from, its price evidence, and whether a reachable Mac can run it now — never a made-up number.
- **A job nothing can run now fails in seconds, with the reason.** Every Mac that cannot take the work is listed with why ("offline, last seen 7d", paused, no free slot, model not installed), instead of a job waiting for hours with no explanation.
- **Retrying a cancelled job makes a new job.** The cancelled one keeps its place and its cause, and the retry carries that cause instead of answering "cancelled at the Gateway".
- **Image pools publish only the sizes they can actually render**, with one recommended size, so choosing a fleet never silently produces a square image.
- **Settings groups what it shows** — this Gateway, what it is doing — in wrapping rows instead of one run-on list.
- **The old Story Studio update/monitoring tab is gone.** Fleet no longer has a Stories tab or the Story-Mac version/check-in report behind it; Gateway signs and serves no app release, and Fleet keeps its Mac, model, job and location monitoring.

## 0.2.256 — 2026-11-13

- **Fleet's Machines table keeps its name column narrow at 768.** The one-line details under each Mac's name now wrap inside the pinned column, so more of the row's state and data is visible beside it while the table scrolls.
- **Fleet's tables stack on a phone.** Below 760 px each row becomes labelled lines instead of a 1020 px table that needed sideways scrolling.
- **Every question is asked in the page.** Confirms, warnings and the Rename/Network prompts are now in-app dialogs, so they work in the packaged app instead of depending on a browser panel (owner rule, 2026-11-13).
- **Free browser-worker work no longer waits for the spending switch.** A new account can take its first zero-priced job immediately, and also skips the paid-work cooldown; priced work still needs the account switched on. Worker status now carries one shared connection state for the Relay row and the extension panel.
- **An account signed in to Flow inside the page is recognized from its own address**, so a worker that signs in there does not have to be paired twice.
- **Login Items shows "Gateway Studio KH".** macOS listed the app as "gateway-kh"; it now shows the name you know, while the bundle, data folder and updates stay where they were, so nothing needs re-pairing.

---

## 0.2.255 — 2026-11-13

- **Fleet gets the same frame.** One status line, one Refresh action, one search field with the filters in a Filters menu that shows how many are on; each Mac or site card keeps its name, state and likely cause on the surface and puts its 7-day history and device facts behind Details.
- **Settings is a real page.** It shows this Gateway's version, Mac, address, Studio Hub state, providers, models and uptime, and says plainly that appearance, startup, keys and backups are changed in the Gateway app on this Mac.

---

## 0.2.254 — 2026-11-13

- **Providers reads as an app now.** Four tabs — Providers, SFX, Workers and Capacity — replace the seven that made you hunt; The queue, measured numbers and pools are sections of Capacity.
- **One search field, one Add, and a More menu.** "1 without a key" is a chip you can click to filter; Re-discover all and Import Price Research moved behind More; each card's Re-discover is in its ⋯ menu.
- **Price research is one sentence** — prices on file and when they were last researched — with the per-provider rows behind Show details.
- **Cards show what matters at a glance.** The id, kind and address are behind Configure, "no key" is a button that opens the key field, and one status line says what is down, why, and opens Fleet.
- **SFX, Workers and Capacity follow the same frame** — one status line, one primary action, state chips that say what is wrong, and the long provider table behind a "Show all providers" summary.
- **Version and What's New moved to the bottom-left pill**, next to the update control (whose hover text says updates are installed by Studio Launcher KH) and a Support this app row.

---

## 0.2.253 — 2026-11-13

- **Update opens Studio Launcher KH.** Gateway no longer signs or serves other apps' updates, and Settings says updates are installed by the Launcher.

---

## 0.2.252 — 2026-11-13

- **Settings says plainly that Gateway's own updates are installed by Studio Launcher KH and never use the App Update Key.** The App Update Key signs Studio Hub and Story Studio releases; it is not a Gateway update control.

---

## 0.2.251 — 2026-11-13

- **Update opens Studio Launcher KH.** Gateway checks the signed feed but never installs anything itself; the download button now opens the Launcher, the only app that installs updates. If the Launcher is not installed, Gateway says so instead of doing nothing.

---

## 0.2.250 — 2026-11-13

- **The sidebar footer is one tidy column.** Support, then the version pill on one line (the name shortens instead of wrapping), the icon-only Check for Updates button, and one status line — equal insets, pinned to the bottom.
- **Escape now works everywhere.** It closes dialogs, sheets and popovers, clears a search field and clears a selected row, in the app and in the console; typing into a prompt opened by the console (Fleet Rename, Network) now works instead of being ignored.
- **Apps on this Mac can pair automatically.** Settings → Local apps, on by default: the app writes `~/.kh/gateway-endpoint.json` (mode 0600, rewritten on every launch) with the Gateway's URL, a machine id and a narrow pairing token. That token can only mint a worker token for a worker id over loopback — never the Gateway API key. Turning the switch off removes the file and revokes the token; pairing a Gateway on another Mac stays manual.
- **Secrets stay in the app's own data folder.** Provider keys and guest records live in the app data directory, mode 0600. The agent connector now reads its guest key from the MCP client's env block, so nothing in the app or the connector touches the macOS Keychain.

---

## 0.2.249 — 2026-11-12

- **Launch at login is registered the supported way.** The agent's plist now ships inside the app and is registered with the system, so turning it off or deleting the app removes its record instead of leaving a dead entry pointing at a file that no longer exists. An older launch-at-login file from a previous version is removed on first launch, so a Mac that upgraded has one record, not two.

---

## 0.2.248 — 2026-11-12

- **Browser workers read at a glance.** One line per worker — name, online, last seen, paired, credits — with the actions beside it and the detail behind a Details expand.
- **A token can be shown, copied or replaced.** It is masked until Show, Copy is right there, and Rotate issues a new one. The page says plainly that a token is stored as a hash, so it can be replaced but never read again — and that **Rotate turns dispatch off** until you arm it again.

---

## 0.2.247 — 2026-11-12

- **A gateway from an older build is never adopted.** If something older is still serving the port, the app stops it and starts its own; if it will not stop, the app says which build is holding the port instead of pretending.
- **A leftover helper from a replaced app is stopped at launch.** Nothing the app starts can outlive its bundle: a helper nobody owns is stopped before the app starts its own. On 1301 this also removes the stale `gatewayDir` pointer, so that Mac no longer records a checkout path that does not exist.

---

## 0.2.246 — 2026-11-12

- **Support this app now explains the donation before opening your browser.** It opens a page in the app first — what support pays for, who receives it and how — and the donation page is a button on that page.
- **The version and What's New now work in the footer.** The app name and version are both readable again, and clicking them opens What's New.
- **Settings → Appearance** offers System, Light or Dark — the fourth "Default" palette is gone. An install that saved it falls back to System.
- Browser workers: an audio job is offered once the extension's live report lists both the model and the voice, instead of being refused after that check passed. The audio extension still cannot claim until it sends an account and a quote.

---

## 0.2.245 — 2026-11-12

- **One navigation list.** Create (Image, Audio, Video, Music, Subtitle, Chat), Library (Models, Voices, Jobs, Analytics, Content Packs), Connection (Providers, Fleet) and App (Settings) — no page appears twice, and What's New is reached from the version pill instead of taking a row of its own.
- **A calmer sidebar footer.** One line for the gateway's state, **Support this app** (the donation page), then the app name and version as a pill that opens What's New, with an icon-only **Check for Updates**. When the Update Studio feed offers a newer version a small download icon appears; the app never installs anything itself, it hands that to Update Studio KH.
- **Settings → Appearance:** System (the default), Light or Dark, applied to the whole app and its console and remembered across launches.
- **Overview** now names this Mac and its build, so the footer no longer has to.

## 0.2.244 — 2026-11-12

- **Quit Gateway Studio KH** at the bottom of the menu bar menu now really quits: the gateway service stops with the app, so nothing keeps serving the port with no icon to control it, and the next launch starts fresh instead of picking up an old service. It still asks first when jobs are running.

## 0.2.243 — 2026-11-12

- Every background name now says **Gateway Studio KH**. The gateway service runs inside `Gateway Studio KH Helper.app`, so Activity Monitor and App Background Activity show that name with the app icon instead of "Python"; the app itself shows as "Gateway Studio KH" instead of "GatewayKH", and its background item no longer reads "gateway-kh". Launch at login keeps working: an older login item is repaired in place, with the same switch and no second job.

## 0.2.242 — 2026-11-11

- Browser workers can pair image, video and audio credentials separately. Optional browser providers stay off until deliberately enabled; audio still needs confirmed compatible provider limits before it can take work.
- Browser account credits retain their observed source and age. Image and video workers on the same account share reservations; unknown charges stay held, and missing balances stay unknown.
- A scoped browser manager can read its paired workers, pause future claims and request a safe close. Closing waits for fresh acknowledgments from every mapped worker and for active or held work to clear.
- Saving browser pacing preserves the latest pause and close settings, including changes made by another manager. Saved pacing still needs acknowledgment from the worker.
- Video requests preserve the selected model's supported controls and reject unsupported choices before execution. Native and web creation use the same request fields.
- Full-image transfers to separate controllers can proceed together while transfers to the same controller remain protected. Release candidates are staged separately; the latest two verified released installers are retained after installed acceptance.
- The Fleet headline names Macs and workers separately, such as "35 of 47 Macs online · 246 of 364 workers ready". When a Mac is joined to more than one location it also says how many distinct Macs those rows are; a figure the gateway has not measured still shows as "?", never 0.

## 0.2.241 — 2026-11-09

- Browser workers: the Story Studio Video Chrome extension can make Flow video for this Gateway. It is off until you switch it on, and the provider is off until you enable it. Pair a worker in the app on this Mac under Providers > Browser workers; the token is shown once.
- A job a worker took and then went quiet on is held, never sent again, and a late result is still kept. A late "nothing was charged" failure settles the held job; an unsure one stays held.
- A worker that says "not sure whether it was charged" and then sends the finished video a moment later now has that video kept, wherever the Gateway was in handling the first message.
- Flow credits show as the worker reports them, or "unknown". A malformed figure is ignored instead of shown.
- Each Flow model takes exactly what it supports: its one length, 16:9, and for picture models exactly one picture. Anything else is refused before a worker sees it, so the extension never makes a picture you did not ask for.

## 0.2.240 — 2026-11-08

- The health check now says how many requests the gateway is answering right now, by kind (status checks, downloads, uploads and changes), so an update can wait for real work to finish instead of waiting for every connection to close.
- Launch at login is on by default. Turn it off in Settings and it stays off.

## 0.2.239 — 2026-11-08

- Developer app gates have a fixed v1 contract: a gate request, its inventory and its result are checked against one schema before anything is sent or shown.
- Fleet trusts a Mac's gate facts only when they name that Mac's own controller and node; facts for another Mac are ignored, and gate traffic uses only the Fleet credential, never this Mac's operator key.
- The app is signed with the shared KH signing identity, inside files first, so its signature stays the same from one build to the next.

## 0.2.238 — 2026-11-08

- Fleet Locations and Machines follow the page's Sort choice, while a selected Machines column keeps its own order. Unknown values stay last.
- Image admission follows the producing Hub's current memory policy. Controller-local pause state comes from that controller's own work policy.
- Run test handles loading and uncertain submissions more safely. Verification uses the real voice payload and saved catalogue readers.
- Remote subtitle support checks the matching Hub contract and validated subtitle/font inputs. This release does not qualify subtitle appearance or hardware performance.

## 0.2.237 — 2026-11-07

- Studio Hub updates verify the sealed app files before sending a release. Damaged manifests and files are refused with a clear reason. Hubs running 0.6.224 or an unknown version receive the full disk image; delta delivery starts with the confirmed 0.6.225 baseline.

## 0.2.236 — 2026-11-07

- Every admin page shows the Mac's computer name beside its version and under Gateway Studio KH in the sidebar, so you can tell which Gateway you opened.

## 0.2.235 — 2026-11-06

- The menu-bar dropdown shows the app version directly above Quit.
- Run test keeps chip, memory and Mac filters aligned with the selected targets, checks availability again before starting, and shows retained-log progress and available memory. Publishing measured limits still requires a separate approval; a test stays unavailable until the Hub supplies the required support.
- Image output choices retain each Hub model's own dimensions instead of substituting a generic size table. An explicitly empty or malformed size declaration no longer becomes an invented output option.
- Release tools check which Gateway you selected before publishing or asking a Story Mac to update, preventing an unbound helper from acting on the wrong Gateway.
- Hub and Story release forms show a Gateway-owned folder and accept a file name from it. Downloads paths are refused immediately with instructions, avoiding a hidden permission wait after an app update. Existing absolute deployment paths outside Downloads remain supported.
- The Content Packs shelf can retire and remove one exact version. Its files move to the Gateway's recoverable trash, Story apps keep the copies they already have, and removing the newest or only live version asks for a deliberate override. The pack's ownership and release history stay recorded.

## 0.2.234 — 2026-11-06

- Developer Mode has a separate owner page for supported Hub tests, collected logs, receipt review and explicit approval or rollback. It respects existing pause and availability controls; synthetic test results do not qualify a Mac.
- Model Limits shows the Hub's live image limits and where each value came from. Saved changes are clearly marked as awaiting Hub support until a verified override contract is available; Reset follows the current Hub default.
- Image measurements can name an exact registered Mac. Unknown pause or readiness evidence refuses the request, and recovery keeps the original target instead of silently choosing another Mac.
- Story subtitle inputs receive strict subtitle and font validation. Remote subtitle rendering remains unavailable until matching Hub support and permission are verified.
- Queue and recovery checks preserve dispatch capacity after launch failures, recognize explicit provider failures, retain stated zero limits and keep malformed measurement targets from disrupting other jobs.

## 0.2.233 — 2026-11-05

- Gateway checks matching Hub support and permission before accepting Story's late-label renders. Unavailable or unverified support is refused clearly; this update does not turn on remote rendering or certify label appearance.
- Image models publish the prompt-length limit stated by their Hub instead of a flat 4,000 characters. Unknown limits stay unknown; shared models and pools retain the smallest known safe limit across their active locations.

## 0.2.232 — 2026-11-05

- Fleet labels Story's queue as its last reported queue and shows the report age beside the counts. An unknown age is stated plainly.
- Story can check whether Best for this hardware is ready. It currently reports unavailable because verified speed, size and quality comparison evidence is not ready; explicit output settings remain usable.

## 0.2.231 — 2026-11-04

- Remote final rendering chooses a Mac that can use the requested video settings. Busy Macs still show what they support, with free capacity shown separately.
- An enabled controller can render on its own Mac too. Compatibility follows the Hub's published support, with the existing rendering and pause controls respected.

## 0.2.230 — 2026-11-04

- Each provider has a visible On/Off switch. New switches start On; saved choices and model selections stay as they are. Off stops new jobs from using that provider, including through a pool, without clearing its key.

## 0.2.229 — 2026-11-04

- An OpenAI-compatible model that states its own answer-length limit now publishes and sends that limit when the caller does not choose one. Magpie's stated 512,000-token output cap is no longer ignored. An explicit caller limit still wins, and an unstated request ceiling remains absent.

## 0.2.228 — 2026-11-03

- The menu now says Quit Gateway Studio KH.

## 0.2.227 — 2026-11-03

- Chat pools publish the smallest context, output and request token limits across their enabled members. A limit stays absent if any enabled member's value is unknown, and membership or enablement changes update the next catalogue answer.

## 0.2.226 — 2026-11-03

- Image jobs record Text to image or Reference to image and the submitted reference count. Jobs and Analytics show those alongside the Hub's confirmed used count, with a filter for either mode. Older records keep unknown values.
- Analytics is now the same page in the app and browser, with generation summaries by type, model, machine and Mac class.
- Image models relay the Hub's Text to image and Reference to image modes, including each worker's availability and reference limits. Routing refuses an unavailable mode and keeps compatibility with older Hubs.
- A reference request with no staged image is refused clearly instead of becoming a text-only image.
- A paused location's old model limits no longer lower the limits published by active locations.

## 0.2.225 — 2026-11-03

- The console shows its version beside a What's New button. About also links to the notes for the current version.
- Jobs show which Mac ran the work, how many reference images the Hub used, and recorded Gateway dispatch and finish times. Older finish times say not recorded.
- Remote controllers use their location names instead of “This Mac”. Their machine and site IDs stay unchanged.
- Kie image models that need an input image stay out of the picker until the Gateway can send that image. Text-to-image models remain available.

## 0.2.224 — 2026-11-02

- Music Create shows lyrics for models that support singing. A model that requires lyrics refuses a blank lyric sheet before queueing. Vocal-language choices follow the model's published options; no list is offered until the Hub publishes one, and instrumental models show none.
- Local music requests preserve language and duration aliases, including nested parameters, and use the same requested duration for the YuE2 time allowance. Vocal language is sent to the Hub as vocal_language for its style-prompt mapping.
- The fleet image pool appears in Create alongside its available member models. Image-pool retries obey the configured attempt limit.
- Writing pools count only enabled providers when admitting concurrent work, keeping excess jobs in the Gateway queue.

## 0.2.223 — 2026-11-02

- The app is now called Gateway Studio KH, including its menus, admin page and installer. Its app location and existing connections stay the same.
- Backups on attached install SSDs now go into 2 Backups. The app shows that folder name and no longer creates KH Backups on those drives.

## 0.2.222 — 2026-11-02

- The web admin now shows its version and What's New from the same changelog as the app.
- A model with no Mac able to run it right now is refused straight away, with the reason, instead of being accepted and left waiting. This covers a site that answers but has lost the only Mac for that model. One missed reading doesn't switch a model off.
- A job sent to a site whose Macs have all gone offline moves to another site after about 10 minutes, instead of waiting until someone restarts the site.
- Gemma 4 uses its qualified 49,152-token window once the Hub update is installed, with a published request budget of 39,960 tokens. A request is sized to fit. The answer is capped at 8,192 tokens, and if the request leaves less room the answer is shrunk to fit and the job says so. A request too big to leave room for any answer is refused straight away and asks you to shorten it.
- A writing job whose answer was cut off now fails and says so, keeping the partial text, instead of reading as finished.
- Fleet names what each "Needs attention" means: a studio down, a stalled job, a recent failure, or a flag only its Hub can see. A request too long for a model no longer marks the Mac as needing attention.

## 0.2.221 — 2026-11-01

- A model installed later on a Mac already in the fleet now switches on at the next inventory read. A model you switched off stays off, and removing a model no longer forgets that choice. A failed or partial read changes nothing, and a paused Mac stays paused.
- A Mac whose memory could not be read keeps its image model, rather than having it changed on a guess.

## 0.2.220 — 2026-10-31

- Fleet can apply one work order to the Macs you pick, by site, by memory size or by ticking them. Each Mac's result is listed: confirmed, queued for its next check-in, or refused and why. A silent Mac doesn't hold up the others.
- Every newly published pack gets a cover. One with none is made by Qwen Image 2.1 Turbo on your own Macs, with no lettering. Until it lands the pack is stored but not offered to Story, and the status says why; a failure can be retried. Packs already published and style packs are left alone.

## 0.2.219 — 2026-10-31

- A guest's Khmer voice is ready the moment it is added. It clones from the sound of the recording, because the transcriber cannot read Khmer. In 0.2.218 it sat on "transcribing" for many minutes and could then fail.
- A guest can type the words spoken in their recording. That is used as it is, with no transcribing.
- A voice in another language whose words can't be read still works, from the sound alone, and says so.

## 0.2.218 — 2026-10-31

- Generated sound clips can be added on a Mac with no audio tool installed. The Gateway reads WAV and FLAC files itself. For MP3, OGG or M4A it now says it has no ffprobe to read that format, instead of calling a good file unreadable.
- The Fleet page can set what work a Mac prefers and how long it holds for that work, with the choices that Mac publishes. A save reads as confirmed only once the Mac confirms it.
- A nightly settings backup, with keys and passwords left out. It is off until you turn it on, reports its last success and last failure, keeps the last 14, and comes with a recovery guide.
- Publishing a release unmounts its disk image by device, so it can never eject a real drive.
- Guests can add their own voice: upload a recording, name it, pick its language and confirm they have permission. Only that guest sees it, and they can remove it again (it is kept restorable).
- Every voice on the Voices page and in Create's Audio picker has a play button to hear its sample first. A voice with no stored sample says "No sample"; playing never starts a paid generation.

## 0.2.217 — 2026-10-30

### Organize voices with tags
Add or remove tags from voices in the Gateway's web console, and filter the list by tag. Tags travel with the voice catalogue and shared voice requests.

### Sounds a model made can join the library
Clips generated by Stable Audio can now be added to the shared sound library with their tags and a
description. They are labelled as generated, with the model and a licence note, and never as CC0.
The licence check on downloaded clips has not changed.

### A kept sound can be fetched by another Mac
The Gateway's sound library could list a clip but never hand over its audio, so Story on a second
Mac could not use one. `GET /v1/sounds/library/{sha256}/audio` now serves the stored bytes with
their real media type, guarded by the same key as the routes beside it. An id that is not a
sha256 is refused, and a clip the library does not hold answers 404.

### Sounds keep their tags
Openverse describes each clip with tags, a category and genres, and the Gateway used to drop them.
They now travel with a search result and are stored beside a kept clip, so a search can find a
sound by what it is. A clip kept before this release still loads, with no tags rather than an
error.

### A silent sound site no longer stalls a search
When Openverse accepted the connection and then sent nothing, a search could wait over 30 seconds
and then look like it had found nothing. A search now gives up in a few seconds. The answer names
the source that could not be reached and why, and the Gateway's own library answers in its place
when it has a matching clip.

### Packs can show a cover
A pack in Story's Studio Library can now show cover art. The cover is a file placed beside the
pack's archive on the Gateway, so adding one changes no pack version, archive or hash; Story
fetches it through its own address, and a pack without one still reads "no preview yet" rather than
a stand-in image.

### Fleet says what its controller actually knows
A Mac that does not answer its controller may be asleep, off the network, or have a Studio Hub
that is frozen or waiting on a permission prompt. The problem stays red, but the description no
longer claims the Mac is asleep. A location's Hub version is shown on its own Mac, rather than
being copied onto other Macs whose version was not reported.

### Fewer repeated fleet reads
The Gateway reuses its recent Fleet report between background refreshes, reducing extra work on
busy sites. Refresh still asks for a new report.

### Deadline stops leave a record
When the Gateway asks a Hub to stop a batch that reached its running limit, the stop record names
who asked and what the Hub answered. If the Gateway restarted during a batch that was still running
at the limit, its attempt history says so. The limit still counts time the Hub reports as running.

## 0.2.216 — 2026-10-30

### A model is not "unavailable" because its site missed one slow answer
A site that is busy rendering can miss the Gateway's six-second look at it. Until now that one
miss marked every model at that site as unavailable for a moment, then available again a few
seconds later — so a model could flip in and out of the picker while nothing was wrong, and a
research call could look "locked" on one read and fine on the next. A site that answered in the
last 45 seconds keeps its models offered through a slow read; one that has really gone quiet is
locked exactly as before. Nothing is sent to a site that did not answer this time, and a job that
cannot be routed is still refused on the spot with the reason.

### A queued job now says what it is waiting for
A job waiting for a free Mac used to read as `accepted` with nothing on it at all — which is how a
research call sat for twenty minutes before being cancelled. The row now publishes the Gateway's
own reason ("every Mac with this Chat model is busy", "waiting for Terranash-1600 to answer —
…") and when it will try again. Waiting on a busy Mac is unchanged: the job still waits, it just
stops being silent, and it still ends at the queue ceiling if nothing frees up.

### A model's output limit can no longer be larger than its context window
The local Gemma model published a 128,000-token output allowance beside a 32,768-token context
window: 128,000 is its worker's fallback, not anything the model states. A model cannot write more
than fits in its window, so the published allowance is now the smaller of the two, and the row says
the larger figure was dropped (`max_output_tokens_capped_from`). A caller that asks for more than
the published allowance has it lowered to that number and is told on the job, instead of the Hub
refusing it after measuring the prompt.

### The Fleet page answers "how many issues?"
The header now says how many issues there are, split into down, degraded and info — counted from
the same severity that colours each card, once per Mac or site at its worst cause. Macs shown from
a slow site's last report are listed separately as "not counted", so the number can never be read
as including them. Groups are sorted worst-first by default, there is now a visible **Sort** control
(issues, offline, most problematic, site, longest unseen, most drops, name), and the **Show**
dropdown gained Down, Degraded and Has any issue. The Machines table's Likely-cause header sorts by
the same severity order. A row shown from its site's last report is never counted as down or
healthy. Also: a Mac whose controller refused its credential now shows a red state pill, not the
blue of a working Mac.

## 0.2.215 — 2026-10-30

### Colours on the Fleet page now mean what they say
Red is only for something broken or down: a Mac that is offline, a site that is not answering, a
refused credential. Yellow is for a Mac that is still working but wants attention: a slow Ethernet
link, drops, a studio that is not running. Blue is information, such as a Mac running fine on
Wi-Fi. The words are unchanged — the colour just stops shouting "down" at a Mac that is up. A
cause the page does not recognise is drawn in the ordinary text colour, never red.

## 0.2.214 — 2026-10-30

### A retired pack no longer blocks every Content Pack install
When a pack was retired but its archive was still on the Gateway, the receipt said two different
things about it — download not available, archive available — and Story refuses the whole receipt
when they disagree. So every Content Pack install and every Restore failed on every Story Mac. A
retired pack now says the same thing in both places: no download, because the Gateway already
refuses to serve it. Nothing changes for a live pack, and the pack still appears in the owner's
Library so it can be removed rather than looking lost.

### A slow site no longer makes its Macs vanish from the Fleet page
When a site took longer than six seconds to answer, its Macs used to disappear from the list — so a
Mac could look missing only because its site was busy. They now stay in the list, marked **Last
report 12:31** (the time they last answered). A carried row is never counted as online and never
counted as down, and it is never given work: routing, capacity and the health history still see
only what was measured this read.

Disk, Memory, Network and the action buttons stay blank on such a row, because the Gateway did not
read them this time. A Mac that last answered more than an hour ago is not shown at all; the site's
own line still says when it last reported.

---

## 0.2.213 — 2026-10-30

### The Gateway stops nothing by itself until you switch it on
Both switches under Fleet → Jobs — stopping a Hub batch nothing owns, and stopping a copy the
Gateway replaced after a site went silent — now **start off** on a Gateway that has never been
told. An automatic stop is a deliberate choice, so nothing runs one on your behalf out of the box.

**A switch you already turned on stays on.** The Gateway keeps your answer in its settings file,
and a saved on is what it reads first: changing the default rewrote nothing and took nothing back.
So a Mac you had switched on keeps cleaning up exactly as before, and the page's heading reads the
real state of both switches, in words, either way.

The switch on the replaced copies is now written the same way as the other one: only a clear `on`
counts. A Gateway too old to report the value, or an answer this page cannot read, now reads
**off** instead of being treated as on.

---

## 0.2.212 — 2026-10-30

### Every automatic stop now leaves a list you can read
The Gateway already stopped a copy it had replaced, and an orphaned Hub batch when you switched
that on, and wrote each one to its audit log — a file nothing on the page ever showed you. A batch
could therefore stop on a Mac nobody was looking at, and afterwards there was no way to tell which
copy was stopped, why the Gateway thought it was safe, or what the Hub said it did.

Fleet → Jobs now carries **Recent automatic stops**, the last seven days, newest first. Each line
names the time, the site, the Hub batch, the Gateway job it belonged to, the reason the guard
proved before it asked — the kept copy delivered and on disk, or nothing left to collect — and
which switch did it, `superseded_stop` or `auto_stop`. The Hub's own answer is there too: how many
were queued and cancelled, how many running workers were **asked to stop, not yet confirmed**, and
how many are still pending. A Stop you pressed on an orphan appears in the same list, marked
**manual**, so it can never read as something the Gateway did by itself.

The two switches above it now say their current state in the heading — both of them, in words —
and the list is owner-only: a guest never sees it. If the audit log cannot be read, the list says
**not available**, never "nothing was stopped".

---

## 0.2.211 — 2026-10-30

### A site that drops off is cleaned up, not left with a duplicate batch
When a Mac's Hub went silent mid-job, the Gateway moved the work to another site — and the old
batch kept running on the Mac that came back. The job still succeeded, so nothing looked wrong, and
somebody had to notice the duplicate and stop it by hand.

Now the Gateway writes down the exact batch it left, in the job's own record. When that Mac answers
again, **whichever copy finished first is the one kept**: a finished old batch is collected if the
job still needs a result, and its bytes are never fetched twice if the job already delivered. A copy
still running is stopped **only** when the same check the Fleet Stop button uses proves the kept
work is safe — a job that succeeded with every file on disk, or one that failed with nothing left to
collect. Each automatic stop is written to the Gateway's audit log.

Before moving a job off a silent site, the Gateway now asks once more whether that batch is still
there: if the Mac has come back and the batch is still running, it keeps following it; if it has
finished, it collects it. No second batch is started in those cases.

This is only for batches the Gateway replaced itself, it gives up following a site that keeps
dropping after two tries, and when the kept copy cannot be saved at that moment — the disk is
full, or the Mac is busy — the batch stays on the list and is tried again a minute later rather
than being written off. It is a **switch of its own under Fleet → Jobs** — the owner asked for this
cleanup, and turning it off leaves the duplicate to the Stop button. (0.2.213 made it start off on
a Gateway that has never been told; a switch you had already turned on stays on.) The older
**Automatic stop** switch beside it is separate and keeps its meaning; a batch no Gateway job
names is still never stopped automatically.

### Agents can make things now — with a guest key
A guest can be given **one API key for an agent** — Claude Code, Claude Desktop, anything that
speaks MCP — from **Guests → Create key**. It is shown once and stored only as a hash; **Replace**
and **Revoke** are right there, and disabling or removing the guest stops it at once. The key is
*the same guest*, not a second account: free and local models only unless you tick paid models for
that guest, their own jobs and files only, and every job it starts says **Requested by … (guest)**.
It opens the Create API and nothing else — every other console route and **every `/v1` route**
refuses it, including on this Mac's own loopback. That API also gained the one job a guest could
not start before: **image-to-video from a scene image**, the same image-to-video-only rule the
rest of the Gateway follows. The connector itself is `connectors/mcp/` — a small `python3` MCP
server with a ready-to-paste config in its README, shipped inside the app so an installed Mac
can point its own client at it.

## 0.2.210 — 2026-10-30

### The Fleet page is the web page now — one page, built once
The browser console at `/admin` has the Gateway app's own shape: the groups sit in the left sidebar,
and **Fleet** is one page with tabs — Groups, Locations, Machines, Models, Jobs and Story Studio. On
a phone the sidebar becomes a top bar. The Mac app's **Fleet** row now shows this same page, so a
Fleet change is written once and appears in both places — nothing to build twice — and the orphan
check below reaches the app without any separate work. The old native Fleet page is still in the
repository, unused, so it can be brought back if it is ever wanted.

### Every Fleet action is on the web page now
Start or restart a Mac's services, turn on auto-start, rename it, pause or resume it, change Lite
Mode, its work priorities, its network, look at or empty its Studio Hub Trash, install a model on it
or on every Mac that can run it, set or remove a model, and switch remote final rendering per Mac —
all from the **Machines** tab. **Locations** gains Credentials, Address (tested before it is
re-pointed), Network, Refresh models, a Release card that publishes a Studio Hub build and rolls it
out with Stop/Resume, and a warning-kind hide/show. **Jobs** can stop a whole batch and retry one
failed item. **Story Studio** can ask a Mac to update now and publish a Story release. Everything
confirms in the same words the app used, and one action is still the app's alone: permanently
deleting a Mac's Trash Folder contents needs the app's own window and is not offered on the page.

### Hub batches still running for work that already finished
When a site restarted, an old Hub batch could keep running on work the Gateway had already finished
somewhere else. The **Jobs** tab lists those batches now: the site, the batch, how long it has been
running, what it has produced, and which Gateway job replaced it. **Stop** is offered only when the
Gateway can prove the work is safe to drop — its job succeeded with every artifact on disk, or it
failed or was cancelled with nothing left to collect — and the answer is the Hub's own: *running
signalled — not yet stopped*, never a claim that the work is done. A batch no Gateway job names gets
no Stop at all, only the reason. A site that did not answer says **Not checked**, never "none".
**Automatic stop is off by default**; when you switch it on it uses the very same check, and a batch
no Gateway job names is never stopped automatically.

### Drop lengths — and every number names its own window
The Drops cell's tooltip now says how long your drops lasted: the longest, the median, roughly how
often they come, and whether it recovered by itself or after a startup action. A length nobody
measured says so instead of reading as "0 s". The caption beside the strip now says **N drops in
7 days** instead of a bare "N in 7 days", so a 24-hour count and a 7-day count can no longer be
mistaken for each other, and offline time the Gateway did not measure says *offline time not
measured*.

## 0.2.209 — 2026-10-29

### The Fleet page sorts itself out: what is offline, what keeps dropping, and the likely cause
The Machines tab gained four columns. **Health** says *Problematic*, how long a Mac has been
**Offline** right now, how many **drops** it had, or *Stable*. **Seen** says when it was last up.
**Drops · 7 days** draws the week and the 24-hour count. **Likely cause** names the reason — a
credential, a dead site, a slow Ethernet link, Wi-Fi, services not starting at login, a Hub that is
behind — and the tooltip shows the evidence each answer came from.
A new **Show** menu — everything, offline now, problematic — narrows the list on the app's page and
in the browser console. The console also offers **Sort** (offline first, problematic first, site,
last seen, drops in 24 h, name) and a real **N problematic** short-cut in its summary, and the
Locations tab gained Health and Likely cause too, so a dead site is stated on its own row.
**Problematic** is your own number: *N or more drops in 24 hours*, 4 by default, saved on the Gateway
from the box on the same strip or the popup beside Show in the app. A whole site being down counts
against the site, not against every Mac on it — one dead controller is never five broken Macs. A Mac
that is down now never reads *Stable*, and a Mac the Gateway has not been able to watch says `—`
rather than `0` or *fine*. When nothing supports a cause it says **No evidence yet** instead of
guessing.
The Gateway keeps the last seven days in `data/fleet_history.json`; nothing new polls — it records
the check it already makes — and it writes only when something actually changed. The browser console
at `/admin` draws the same list from the same verdict.

### Retire a pack, and — if you mean it — remove it from every Story app
Retiring a pack in Content Packs still does exactly what it did: the file stays, customers stop
seeing it, and every Mac that already has it keeps working. Retire now opens one small question —
**Also remove from every Story app**, a box that starts unticked — with Retire and Cancel. Cancel
does nothing, and leaving the box alone changes nothing on anyone's Mac. Tick it and each Story app
drops its copy the next time it checks in — a Mac using that pack in a channel may keep it instead,
and says so. The shelf then reports what really happened, Mac by Mac: "removed on 3 of 4 Story Macs ·
kept on one Mac: used by channel Night Falls · not reported yet: mac-b". A Mac that has not
answered is **not reported yet**, never silently counted as removed.
You can also make or withdraw the request later on a pack that is already retired, and un-retiring a
pack withdraws it — a copy already removed does not come back, and the page says so.

### A re-tried job is never failed before it is sent
When the Gateway picked a job up for another attempt, it measured "is this stuck?" from the
**previous** attempt's clock. In the short gap between claiming the job and sending it, that old
clock could look hours old and the job was failed as timed out — before anything was sent, which
makes Story send it again. A claim now starts its own clock, so a job is only ever timed from its
own attempt.

## 0.2.208 — 2026-10-29

### Background work no longer waits on a job it cannot use
The Gateway held **all** background work at a site while any normal job anywhere was waiting for a
model that site could run — whatever lane that job was in. On 2026-10-29 that became a three-and-a-half
hour freeze: seven narration jobs were stuck on a site that could not run them, and every image site
also lists the narration model, so **132 background image jobs** were refused while the image Macs sat
idle. Production now reserves only the lane it competes in.

### A job bound to a place that cannot run it moves
A job was re-sent to its location over and over — "hub submit failed: timed out", 6 to 29 attempts —
because nothing checked that the location actually has its model before admitting it again. Admission
now asks the location's own per-model supply, and a job whose location does not publish its model, or
is unreachable, is re-bound to one that does. A location that states nothing stays unknown, so an
older Hub is never second-guessed.

### A pool job says what it is waiting for, and never waits for ever
Waiting for a free slot at one model behind a pool had no deadline and no reason: with a cloud limit
of 1 and one call that could run for hours, a job could sit "running" with nothing published. It now
says **"waiting for a <model> slot"**, gives up retryably after 15 minutes so the job returns to the
queue with that reason, and a cloud model that goes silent is given 10 minutes before the pool moves
to the next one. Both numbers are editable in `provider-concurrency.local.json` under `pool`.

### Nothing runs silently any more
A running job with no job id from the provider and no progress used to look exactly like a healthy
one. After two minutes it publishes what it is waiting for; past the lane's own time limit it fails
retryably instead of staying silent.

### Say how a line should be delivered
The Audio lane has a new **Delivery / emotion** box: type *warm and slow*, *whisper the last line*,
*slightly amused*, and the instruction goes to the engine with the text instead of a plain reading.
The box appears only for a model whose own catalogue row publishes the control — the Gateway reads
that live from your Studio Hub, never from the model's name — and it clears when you switch to a
model that does not take one. The panel shows the engine's own guidance (examples, maximum length,
inline-event syntax, language rule) exactly as the Hub published it, and nothing for a field the Hub
did not send. Ask a model without the control for an instruction and it is refused with a plain 422
that names why, rather than quietly reading the line plainly.

---

## 0.2.207 — 2026-10-28

### Voices have names in Create, and only the ones the model can use
The Audio page's Voice field showed the provider's own id — `studio-hub-local::voice-ba75c0a0aa4f`
— instead of the name the library publishes. It now reads **Christian · Gateway voice library**,
and a voice nobody named says so plainly ("Unnamed voice · ba75c0") rather than presenting an id
as a person. The list is also **the chosen model's**, re-read every time the model changes: a
voice the lane would refuse is no longer offered. Typing an id by hand is still there, for a voice
known from a share link.

### Download, with a real filename, on everything Create makes
Audio, subtitles, images, music, video and chat answers — and every row of the Recent list — now
have a **Download** that saves the file with its own extension (`tts-5417efcd-0.wav`,
`transcription-f0ff45a9-0.srt`), from the same authenticated door the page already uses, so it
works from a browser on another device.

### Guests: share Create without handing over the Gateway
A friend can now sign in with a **name and password** and use the Create page, and nothing else.
Every other page and every other route refuses them on the server; they see and download only
their own jobs; and removing an account retires it rather than deleting its history. By default a
guest may use **free and local models only** — paid models are one switch per guest, off unless
you turn it on. Every job now says **Requested by**: you, Story Studio, or the guest's name, and
"not recorded" for jobs from before this release.

## 0.2.206 — 2026-10-28

### Final renders follow each Mac's own work order, fastest Mac first
A render is no longer a rule that jumps ahead of everything: it is one entry in each Mac's work
order, which the owner can rearrange. Among the Macs that will take a render, the Gateway prefers
the one whose measured render times are fastest, and uses chip type only when a Mac has no
measurements yet.

### An on/off switch for every kind of work
Each Mac can switch any kind of work off, render included, and every kind is on by default. The
Gateway never sends a Mac a kind it has switched off, and the Fleet page relays the switch to the
Mac's Studio Hub.

## 0.2.205 — 2026-10-28

### A Mac's model preset, an added model and a removed one, from the Fleet page
A joined Mac's install screen is now reachable from its "…" menu without opening that Mac.
**Model Preset…** reads the Mac first and then offers *that Mac's own* presets with *that Mac's
own* counts — "9 of 14" is its count of its own verified receipts, read live each time, never a
list held here. Setting one records who asked, on that Mac, and an unknown preset id is the Hub's
own refusal naming the presets it does have; nothing is swapped for another. **Remove a Model…**
offers the models that Mac reports as installed and moves the chosen one into that library's trash,
never a delete, and the Hub refuses while a job, a paused model or a dependent package is using it,
in its own words. Both writes are owner-only, because a preset decides what a Mac installs. An
older Hub answers `HUB_TOO_OLD` naming 0.6.205 rather than hiding the entry, and a sleeping Mac is
reported as unreachable in its own words instead of failing the page.

### A thin cable is named before it wastes a day
Mac 1301 was linked at 100 Mbit/s on an old cable (10 Mbit/s before that). A thin link does not
fail — it negotiates — so the Mac stayed reachable, ready and taking work while everything it did
crawled, and nothing said so. The Fleet page now shows every Mac's **Network**: the connection
type from that Mac's own OS hardware port, the speed and duplex its Ethernet negotiated, and
whether Wi-Fi is also up. Anything the Gateway cannot read is `unmeasured` with its reason and
draws "—"; a Hub that predates the field publishes nothing, which reads as unknown, never as
"fine". The one verdict that matters is **slow** — Ethernet under 1 Gbit/s or at half duplex — and
it is drawn as an amber Network cell on the Machines tab (slow links first when sorted), an amber
note under any node on a slow link in Groups, and an amber "Slow link — check cable/port" line on
the web console's Mac tiles, with a summary that names the Macs: "3 Macs on slow Ethernet — check
the cable: 1301, 1304". Wi-Fi is shown as Wi-Fi and is never called a slow cable. Read-only: no
admin action, no routing change, nothing is sent to any Mac.

### A Hub's own vision flag is what publishes image input
A Studio Hub model that reads images published `supports_vision: null`, because the catalogue
derived it from a field only OpenRouter rows carry, and Story's bake-off then refused the model as
"does not publish image input". A Hub states the fact per model on its catalogue row — and on the
package row when it can — and the Gateway now reads that, publishes it, and fills
`input_modalities` to match. Nothing is inferred from a model id or a name: a row the Hub does not
describe still publishes `null`, and a stated `false` keeps the image input off.

### Gemma is no longer withheld by name
The owner removed the family lock: *"They are models we can run so we should have them installable.
Able to run on the machine or not is based off its RAM. We no longer block or lock any model."*
Gemma 3 12B 4-bit came off the retirement list, so it is judged by what it is — its modality,
whether this gateway serves it, and whether the Hub says it can run on that Mac — and not by its
name. The two LTX video products stay retired, and a text encoder's row on a Hub that still files
it under the retired video engine is still withheld, by shape: a video model that publishes no lane
contract cannot be described, so it is not offered.

### A pool advances past a connection that was never made
A pool member whose connection is refused — a relay that is switched off answers before a single
byte leaves — used to stop the chain, because the send had been counted. Nothing can have been
accepted or charged when nothing was transmitted, so the pool now moves to the next member, a paid
one included. A submission whose outcome is genuinely unknown is still a durable recovery hold and
is never retried.

## 0.2.204 — 2026-10-27

### Reference to Image: your characters and a prompt, on the edit lane
Story Studio can now send a few reference images and a prompt to the FLUX.2 Klein edit lane the
fleet already holds, and get back a scene image that keeps those characters. The Gateway has a new
reference store behind `POST /v1/images/source-images`: it answers Story's own field names, refuses
a body it cannot decode instead of holding it, and lets a stored reference expire after fifteen
minutes. Idempotency covers the references too, so the same key with the same images replays and the
same key with different images is refused; an unknown or expired reference is a clear error, never a
silent text-to-image render. The catalogue no longer hides the capability: a lane that publishes
`edit` is offered it, and across locations the capability is the union of the sites that run it
while the limits are their intersection. A run with no capable lane refuses with one sentence naming
the missing capability instead of "try again later".

### A remote final render names the Mac that ran it
Every remote final render to a joined fleet Mac used to fail at its first status poll. The Hub
reports the machine that ran the render by its broker's own label, `local`, and the Gateway treated
any name other than the destination Mac's id as a foreign machine. The answering Hub is the
destination, because a Mac only ever answers about its own job, so `local` is now accepted
alongside the empty answer and the Mac's own id; any other name is still refused. The poll no longer
crashes the monitor either: a status answer it cannot accept is logged with the Hub's HTTP status
and a short excerpt of the body, an accepted answer resets the count, and thirty rejected answers
in a row settle the row as a remote refusal, which Story already falls back from to a local render.
A transport failure is not a refusal and keeps being retried.

### A video lane publishes the model's own options, and the picture gets there
Cloud video models no longer answer with the same flat row every time. Each model's own capability
record now carries the modes, aspect ratios, resolutions and duration range it really accepts, the
switch that turns audio on, and where a first frame, an end frame and reference images go. Two first
records ship in this release; both are disabled, so nothing runs and nothing is spent until you turn
them on: **Wan 3.0** (text-to-video, image-to-video and reference-to-video; 2 to 30 seconds;
480p, 720p or 1080p; six aspect ratios; audio) and **Seedance 2.5** (text-to-video, image-to-video
and reference-to-video; 4 to 30 seconds; seven aspect ratios; its own audio switch and a duration
list). A model nobody has written a record for now publishes its provider's stated mode and no
variants, so Story cannot offer a canvas the provider never published. An unpublished duration,
ratio or resolution is refused with the accepted set named, never quietly clamped.

The scene image Story uploads now actually arrives at the provider. The Gateway stages the end
frame and the reference images as well as the first frame, and refuses an expired reference by name
instead of letting a job become a text-to-video request with no picture. The four cloud video
adapters send the image in each endpoint's own declared field. Text-to-video stays accepted for the
models that publish it; an image-to-video request with no image, a mode the model does not publish
and a reference-to-video request with no reference are each named in the refusal.

### Studio Library packs can be retired, un-retired and removed
A published pack has three more actions. **Retire** hides it from the catalogue and blocks new
installs while existing installs keep working, and retiring the newest version asks for a second
confirmation. **Un-retire** puts it back on the shelf. **Remove** moves the archive and its sidecars
into the Gateway's Trash with a manifest entry saying what was moved, when, from where and its
sha256, and drops it from the active records; a pack that has not been retired is refused, so there
is no hard delete. The content-authority backup understands both states, so a retired pack is not
reported missing and a removed pack is recorded as being in the Trash. A one-off migration script
records the September 21 Wars archive as removed without moving or rewriting anything.

### Backups carry your local settings and limits
Settings → Backup now includes the ten local configuration and limits files: `providers.local.json`,
`pools.json`, `provider-concurrency.local.json`, `model-limits.local.json`, `telegram.json`,
`render_optin.json`, `fleet_warnings.json`, `voices_state.json`, `models_cache.json` and
`retention.json`. Moving the Gateway to another Mac or restoring a backup preserves all configured
settings without having to re-enter pools, model limits or retention rules by hand.

### A release can be published as the whole image
Publishing a Studio Hub release to your controllers can now be forced to send the whole disk image
instead of only the changed files. That matters for a controller on an older Hub: Hub 0.6.201
advertises the changed-files capability but refuses a delta larger than half the image, and 0.6.203
is 50.44%, so it refuses the stage and then asks for a disk image a files-only release never had.
The publish answer, the catch-up after it and `scripts/hub_rollout.py --full` all carry the choice,
so a controller that was offline during the publish is sent the same shape when it comes back.

## 0.2.203 — 2026-10-26

### SFX: a sound source with a key you can save
Providers has a new **SFX** tab. Openverse — where Smart Sound's cues and beds come from — is listed
there the way a provider is: whether a key is set, its live search quota, a link to register an app,
and **Save** / **Clear Key**. You no longer have to edit a settings file to raise its rate limit, and
search works without a key at all on the anonymous quota.

### The provider list is grouped by what each one serves
Providers now files its cards under headings — **Text & chat**, **Image**, **Video**, **Voice &
TTS**, **Transcription**, **Music** — worked out from what each provider can actually run, so the
image cards sit together and the chat cards sit together. Each card still shows every lane it
serves. Nothing about routing, enabling or discovery changed: it is the same models and the same
providers, drawn under headings. The filter box also matches a lane, so typing `transcription` finds
every provider that can transcribe, whichever heading it is filed under.

## 0.2.202 — 2026-10-25

### Remote final renders accept real episode sizes
A final video sent to another of your Macs used to be refused unless it fitted a narrow size and
frame-rate window, so almost every real episode fell back to rendering on this Mac. The limits are
now ranges: any canvas up to 4K (3840 on the long edge), and 24, 25, 30 or 60 frames per second,
bounded by the total work a render may take. Remote rendering stays off until it has been compared
with a local render of the same episode.

### A shared sound effects library
The Gateway can search Openverse for sound effects, preferring CC0 sounds that need no credit, and
keeps the ones you use in one shared library with each clip's licence and credit link. When the free
search allowance runs out, search answers from the library instead.

## 0.2.201 — 2026-10-25

### Backups can carry the fleet roster
Settings → Backup has a new tick box, **Include fleet roster (fleet.json)**: the list of Studio Hub
locations this Gateway sends work to. It is off by default, so a backup restored on another Mac does
not quietly point that Mac at your locations. Tick it when you are moving the Gateway to a new Mac.
A backup without the roster leaves the restoring Mac's own roster untouched.

## 0.2.200 — 2026-10-25

### Jobs shows which Mac did the work
The Jobs list has a new **Node** column, and a job's details say **Executed by**: the Mac the Studio
Hub actually ran the job on. The Provider column still names the site that routed it, which is why
a 16 GB node's work used to look like it ran on the controller. A dash means the Hub did not say
(an older Hub, a job not yet sent, or work that never went through a Hub).

## 0.2.199 — 2026-10-25

### A refused remote render says why in the log
When Story Studio asks the Gateway to start a final render on another Mac and the Gateway turns
it down, the Gateway log now records the reason (one line, no request contents and no key).
Story only sees "unknown outcome", so this line is how the cause is found.

---

## 0.2.198 — 2026-10-25

### Remote final renders resume their upload
A final render sent to another Mac failed with "blob response state is invalid" the moment
Story asked how far an upload had got: the Gateway answered "incomplete", a word Story does not
use. It now answers "uploading" with the same position, so the upload carries on from where it
stopped.

---

## 0.2.197 — 2026-10-24

### A voice can clone from its audio alone
For a language the transcriber cannot understand, such as Khmer. When you **Add Voices**, untick
**Transcribe the clips**: those voices are never transcribed and are sent to the model as audio only,
so VoxCPM2 copies the voice without reading back wrong words. Change it for any voice later with
**More → Language and Transcript…**. Turn **Include reference transcript** off for audio only, or on
and type exactly what the clip says. Models that need a transcript (Qwen3 Base, Breeze) refuse an
audio-only voice and say so in **More → Reference…**.

### Label each voice's language
The same **Language and Transcript…** dialog sets a voice's language (km for Khmer, en for English, or
a short name). Story Studio shows it beside the voice and finds the voice by it.

### A voice job in a language its model cannot speak is refused at once
Each voice model publishes the languages it speaks (Breeze: English and Chinese; VoxCPM2: about
thirty, Khmer included). A job whose script is written in another language — Khmer text sent to
Breeze — is now refused before anything runs, and the message names the models here that can speak
it. A model that lists no languages is never refused.

### Remove provider voices you no longer use
Select any number of ElevenLabs or Fish voices and choose **More → Remove Voice…**. They leave this
list and Story Studio; nothing is deleted at the provider, and **More → Restore Removed Voice…** brings
any of them back. A voice narrated with in the last 30 days asks once more and names who used it.

---

## 0.2.196 — 2026-10-24

### One image choice for the whole fleet
A new kind of pool in **Providers → Pools**: an image pool, published to Story Studio as one entry
(e.g. "Standard image (fleet)"). Pick a model for each RAM size — 16 GB Macs Qwen Image 2.1 turbo,
8 GB Macs Z-Image to start — and every free Mac takes work with its own model. Under **Each Mac** you
can leave a Mac on **Auto (by RAM)** or pin it to a model of your choice; the Mac's Studio Hub keeps
that choice and refuses pool work for any other model. Jobs that name a model by itself are untouched.
Story is told which model made each image.

### Remote final rendering, switched on per Mac
**Fleet → Remote rendering** lists each Mac (same-network Macs first) with a switch. Only you can
flip it: the Gateway app on its own Mac, or your signed-in browser — never the key Story holds. A
Mac only takes renders while it is idle and switched on; a refused or failed remote render falls
back to rendering on the Story Mac and says why.

### Two settings that lived only in files now have controls
**Providers → Capacity** gains "Longest a job waits for a free Mac" (the queue wait limit, in hours)
and "Model limits you know" (context window and output allowance for models whose provider does
not publish them — these fill gaps and never override a provider's own number).

### Finder can no longer stop the Gateway
A `.DS_Store` or other macOS hidden file in a Gateway data folder (made whenever a folder is opened
in Finder) used to make the Gateway refuse to start after a restart. These files are now ignored in
every folder check; a genuinely unknown file is still refused, as before.

## 0.2.195 — 2026-10-21

### A final render can be made by the fleet

Story Studio can now hand a final render to this Gateway and have a registered Mac in the fleet make it, instead of the machine that holds the timeline doing all the work. The render packet is a closed, validated format that describes the work but never carries authority to run anything; the inputs are uploaded in committed chunks into a durable asset store beside the Gateway's own data, so a dropped connection resumes from the last accepted chunk instead of starting again. A job is named by the caller's own idempotency key, its state can be read and its cancel requested by that key or by the job id, and the finished file is served with byte ranges and an entity tag, so a download can resume and a file that changed is never mistaken for the expected one. This is a private, non-chargeable lane: it asks for the Gateway's API key and a Story instance id, and it is offered only through registered Hub relays.

### An interrupted render picks up its own trail, and a cancel stays cancelled

The render record is durable across restarts. A job caught between launch and render is reconciled from what the Hub reports rather than guessed at, and an outcome that cannot be established stays visibly unsettled instead of being reported as finished. A cancel is recorded as a request until the Hub confirms it, a cancelled job never later turns into a success, and a cancellation that arrives before acceptance is answered plainly as such, with proof that the work never launched.

### Preserved archives are kept exactly as they are

The Content Packs page now has an archive bank. Choose one or more `.sspack` files and the Gateway keeps their exact bytes, lists each with its size and integrity, and hands the same file back on demand. Preserving an archive adds nothing to the shelf, installs nothing and publishes nothing: it is the offline copy, and possession of the file is enough to install it on a machine that has never seen the shelf. Files up to 50 MiB each are accepted, matching the installer's own limit.

This entry describes a candidate: it is **unqualified and unreleased**, and nothing here has been gated, built, installed, released or proven with a live render.

## 0.2.194 — 2026-10-20

### A Mac the controller cannot dial is shown as the Mac it is

Some Macs sit on a network the controller can never dial into — a Shops Mac reaches its controller through one-way Tailscale node sharing, so the controller's own call to it always fails while the Mac is up and calling home every minute. The Fleet page could not tell "we reached it" from "it reached us", so such a Mac read as one that had never once answered. Each machine row now says how it was heard from: the controller dialled it, or it checked in. A Mac that checks in counts as online and raises no warning, and a Lite Mode change sent to one is taken by the controller on the Mac's behalf, answered plainly as "the controller has it" rather than as "the Mac has applied it". An ordinary Mac that is switched off is unchanged.

## 0.2.193 — 2026-10-20

### A Mac that has never once answered is named, not folded into "Lite Mode, off"

A Mac whose controller has never reached it even once used to be counted with the Macs that are simply switched off for the night, so the Fleet page said "Lite Mode, off" and nothing else — which reads as "expected, nothing to do", and sends you to switch on a Mac that is already on. It is its own issue now: the machine is named, the address its controller kept for it is shown, and how long it has sat on the roster without ever answering is stated. A Mac that was working and is now off is unchanged.

## 0.2.192 — 2026-10-20

### An answer that was asked to be JSON, and is not, counts as unusable

When a request asks for JSON and a model's answer does not parse as JSON, that answer used to be passed back as a success. It is now treated as unusable: a pool moves on to its next model, and a single model fails with a plain reason — for example that the answer was cut off at a stated number of characters — rather than a half-finished answer being handed on. Cut-off answers are told apart from other breakage, the finish reason the model claimed is kept with the attempt, and the text is never repaired or guessed at. Requests that do not ask for JSON are unaffected.

## 0.2.191 — 2026-10-20

### An unusable answer is a failure with a reason, not a mystery

When a provider answered but the answer could not be used — a CDN's HTML error page served with a 200, or an empty chat answer — the job was parked as "we cannot tell whether work is still running", which meant it could never be settled and a Retry could not be aimed at anything. The provider had answered; its bytes were on the wire, so nothing was outstanding. Those answers are now a settled failure carrying the reason, and a Retry gets a fresh answer. An empty answer from an OpenAI-compatible provider used to be reported as a success with blank text, which nothing downstream could tell apart from a real one; it is a failure now too, like the others.

## 0.2.190 — 2026-10-19

### A music lane's own file formats are accepted

A Studio Hub music lane advertises the file formats it delivers on its catalogue row — a YuE2 lane declares FLAC, and its native file really is FLAC — but a music request only accepted wav and m4a, so a caller that read the row and asked for FLAC was refused. The model's own row is now the authority for what a music request may ask for, and FLAC is added to the music list as well, because the lane can already describe and receive it. Image and voice are unchanged.

### A long writing job is no longer cut off before the Hub is done

Studio Hub works out each chat job's deadline from the job, and it cannot tell a 100-prompt scene pack from a one-line question by reading the body — so Story Studio's own expectation now travels with the request. The chat lanes also no longer time out first: they wait out the Hub's own ceiling of four hours on every path, where before a flat five-minute wait would abandon a job the Hub was still legitimately running. A 100-prompt pack is roughly 2,500 seconds of generation on the fleet's slowest measured Mac before load, prompt and margin. Every other kind of work keeps its own stated limit.

## 0.2.189 — 2026-10-19

### A held job now ends as a known answer

When the Gateway could not tell whether an attempt had been accepted, it held the job rather than guess — and some of those holds could never be settled. A pool's hold had nowhere to go: a pool names no upstream id of its own, so the Gateway closed it locally with a sentence admitting that the member which ran the attempt was never asked. A pool can now discard and reconcile at that member, named from the attempt's own record. A free member going quiet no longer makes the whole job ambiguous — only a paid send that may have been accepted still stops the walk. And an answer that arrives after a hold was closed is now adopted as the job's answer, instead of being filed beside a `failed` verdict where Story's lookup by job id never saw it. A hold closed from the provider's own record keeps its verdict: that close is a proved answer, not a wait that ended.

### A Mac installing an update is left alone

A Mac that answers "I am installing an update" was still offered the same job again and again — one job was sent to the same installing Mac 124 times in 80 minutes before failing with a sentence naming that one machine 124 times. A location that says it is updating now rests for fifteen minutes and takes no new work, through the same rule that honours your own pause. A Mac that is merely busy is a moment, not a state, and stays a candidate.

### The queue wait ceiling is your setting

How long a job may wait for a Mac to come free was a private three-hour constant inside the Studio Hub lane, while the editable budget beside it lived somewhere else — so you could edit your number and be overruled in silence. Measured: 146 jobs failed with the same "No Mac was free for 3 hours" sentence in three days, and 139 of them were never dispatched at all. There is now one number, read from the same setting you edit, and nothing changes until you edit it.

## 0.2.188 — 2026-10-19

### Your answer length is honoured wherever you ask for it

A request that names its answer length in any accepted way is honoured on every provider. Otherwise the model's own published limit is used, and 128,000 tokens when a model publishes none. If a provider says that is too large, the request is sent once more at the provider's own maximum.

## 0.2.187 — 2026-10-19

### The Gateway now raises the alert instead of waiting for someone to notice

Some faults used to sit unnoticed for hours — a Mac whose Studio Hub is many versions behind, a site whose Macs cannot run a model it offers, a Story Mac whose updater has stopped moving on the release it is fetching, and a lane with work waiting and nothing in flight. The Gateway watches for all of them now and raises an alert, and the menu bar app watches the Gateway itself, so a Gateway that has frozen still gets a message out even though the Gateway's own notifier is the thing that is down. Alerts keep an open-and-cleared history, there is a new Alerts card in Settings beside Telegram, and the three new alert kinds — Fleet watchdog, Story update and Gateway health — are on by default.

### See each Mac's Trash auto-empty, and turn it on

Every Studio Hub retirement ends up in macOS Trash, so a Mac whose Finder never empties that Trash fills its disk until somebody empties it by hand — and across a fleet there was no way to tell which Macs had the setting. Fleet → Machines now has a **Trash auto-empty** column reading **On / Off / Unknown**, a **Turn on** button on each Mac, and a bar that turns it on for every Mac that is off, naming each Mac it will touch and each one it skips. Unknown is never drawn as Off: a Hub that says nothing about the setting, and a Mac nobody has heard from since, both read Unknown. It is one way only — nothing here turns the setting off. This half needs Studio Hub 0.6.187; on an older Hub a Mac reads Unknown rather than a wrong answer.

### See each Mac's Lite Mode

The Machines view showed whether a Mac starts its services and whether it answers, but not whether it lends the fleet its idle time. There is now a plain **Lite Mode** column reading **On / Off / Turning on… / Turning off… / Change waiting / Unknown**, sorted with Off first, because a Mac that will not lend its idle time is the one being looked for. A change held for a Mac that was offline reads "Change waiting" — it is your latest word, not the last report.

### Re-discover refreshes the provider you chose, and says what it found

The Models page button re-read every provider's catalogue whatever the filter said, so a press meant for one provider refreshed all of them. It now acts on the provider the filter names — the button says "Re-discover Magpie" — and with nothing filtered a picker beside it chooses between every provider and one, so the fleet-wide run is chosen rather than silent. Every provider card on the Providers page carries its own Re-discover on the row, "Refresh All Models" is plainly "Re-discover all providers", and the page reports what happened: "Magpie: 175 models found", or the error, or the total plus the providers that failed.

### A job that failed for a known reason no longer reads as a gateway restart

Jobs were being shown as "failed / unknown outcome — the gateway restarted while this job was in flight" when no restart had happened. Two causes, both fixed: a second process that merely opened the job database was treated as a restart, and a job held that way kept the label even after its own attempt came back with a reason the Gateway knows. The queue is now claimed by the one process that actually takes it over, and a proven failure settles as that failure. A real restart still holds the work exactly as it did before.

### A model asked for no length now gets its own published answer-length limit

When a caller asked for no maximum length, the Gateway sent 4096 instead of the model's own published figure, so a long writing batch — 75 ideas from a 257 kB prompt — was cut off at the request limit and ended as a failed job. The model's published allowance now travels with the request, per model and per pool member, and 4096 survives only as the last resort when nothing is published at all.

### Received files are kept 3 days, or 10 GB

The default ceiling for files passing through the Gateway is now 10 GB rather than 8 GB. The 3-day default, the Trash Folder's own limits, pins, the one-hour grace period and in-flight protection are unchanged.

## 0.2.186 — 2026-10-19

### A slow Mac can no longer freeze the Gateway

Health checks used to be able to wait behind a fleet that had stopped answering, which made the menu bar decide the Gateway was not running and left the whole app looking dead. The health check now answers without waiting for the fleet at all, and the Gateway takes the summary it already published instead of re-adding every location on every poll. The result: the app stays in touch and shows what is really happening even when a Mac in the fleet goes quiet.

### One Mac cannot open too many connections

A single caller could open so many requests at once that it used up every worker the Gateway had, and everything else queued behind it. There is now a per-client ceiling, so one Mac's burst is refused in one sentence instead of starving every other caller.

### The App Update Key is one key, ready to paste

The App Update Key section used to show three numbers — a fingerprint first, then two encodings of the key, one of them shortened in the middle. Pasting the wrong one is exactly how a Mac stayed broken for days. It now shows one whole key with a Copy button, captioned with where it is pasted, and the short prefix to compare against is derived from the key right above it. The base64 form, the fingerprint and the "Technical details" disclosure are gone, and the Gateway's own sentences no longer point at them.

### The Keep for and Trash Folder boxes show their values again

Those four boxes came back empty because the limits were read in the same answer as a report that walks every file on disk — seconds warm, minutes cold. The limits are now their own instant read, and the file-count report has its own route, so a slow or silent report can never blank the numbers again.

### Received files are kept 3 days, or 10 GB

The default ceiling for files passing through the Gateway is now 10 GB rather than 8 GB. The 3-day default, the Trash Folder's own 7 days / 20 GB, pins, the one-hour grace period and in-flight protection are unchanged.

## 0.2.185 — 2026-10-19

### The Trash Folder's own limits are yours to set

The Trash Folder moves files on to macOS Trash after 7 days (the default), or once it holds 20 GB — and both numbers are now editable beside the received-artifact limits in Settings. Nothing is ever deleted: the Gateway only hands files to macOS Trash, and it never empties it. Pinned files, work in flight and files newer than an hour are still kept.

### Each Mac's Trash says when it was last checked

Fleet > Machines > Trash… shows when each Mac's Trash Folder was last measured and marks a stale or unreachable reading. Those Macs stay in the list, saying so, but they are left out of the total instead of being counted with a size that is no longer true.

### Old logs, voice backups and job archives age out by themselves

Log rotations, voice backups and archived job records older than the received-artifact limit are moved into the Gateway's Trash Folder instead of being deleted or kept forever, and backup pruning moves an old archive rather than unlinking it.

## 0.2.184 — 2026-10-18

### Story updates can resume a download

If a Story Studio release download stops partway, the Gateway can now send only the rest of the file when the Story app asks to resume. A saved partial from an older release is never mixed with a newer one: the app gets the fresh file in full instead. Current Story clients still download in one piece, because they do not ask to resume yet.

## 0.2.183 — 2026-10-17

### Chat jobs say which Mac ran them

When a Chat completion is served by another Mac in the fleet, the Gateway now records which Mac it was, the same way the other kinds of work already do. Fleet latency measurements for Chat name that Mac too. Nothing changes about where the work runs.

## 0.2.182 — 2026-10-16

### A slow Mac no longer hides its models

When a Mac's model list is slow to answer, the Gateway now keeps the list it already had and tries again instead of treating the Mac as having no models. Before, a slow answer could briefly withhold a model that only that Mac has, and a queued job waiting for it would fail.

## 0.2.181 — 2026-10-16

### See and empty each Mac's Trash Folder

Fleet > Machines > "…" > Trash… shows what Studio Hub moved to a Mac's Trash Folder, with totals by kind and age and the disk space left. You can move items to that Mac's macOS Trash, where they can still be recovered, or delete them permanently. Trash on All Macs… shows every Mac at once. Permanent delete works only from this app on its own Mac, and asks you to confirm the exact count and size first. Macs need Studio Hub 0.6.182; older ones say so.

### Updates no longer wait on slow open requests

When the Gateway stops for an update or restart, it now gives open requests a few seconds to finish instead of waiting on them indefinitely, so an update no longer leaves the Gateway down for minutes.

## 0.2.180 — 2026-10-16

### Rejected narration takes are kept for listening review

When a narration section still fails its quality check on the last try, the Gateway now keeps a link to that final take so you can listen to it with the reason it was rejected. The job still counts as failed, and the take is never delivered or used automatically.

## 0.2.179 — 2026-10-14

### Model distribution can target selected Macs

Guarded model distribution can now target selected Macs without assigning the rest of the fleet. Existing LAN sharing, memory checks, and your disabled-model choices are preserved.

## 0.2.178 — 2026-10-13

### Writing backups can follow your selected order

An ordered writing pool tries its members in the saved order. Existing pools keep their current routing. Known unfinished responses retain their text and attempt evidence; a refusal stops the chain, and an unknown accepted outcome waits for recovery.

## 0.2.177 — 2026-10-13

### One model download per network

Fleet model sharing waits for an existing verified copy when its Mac is busy or temporarily unavailable. LAN copies require Studio Hub 0.6.174 or later, so a stalled copy stays on the local network. Files already verified remain available.

## 0.2.176 — 2026-10-13

### Clearer evidence for waiting fleet jobs

Support can inspect why a Studio Hub job waited and which Mac eventually accepted it, using the job already recorded by Gateway. This requires Hub 0.6.172. Job routing and your pause settings keep their existing behavior.

## 0.2.175 — 2026-10-12

### Song time limits come from the measured slowest Mac

The YuE2 song limit now uses the measured speed of the slowest Mac (an M1 16 GB: about 7 seconds of work
per second of song), doubled: a 3½-minute song gets about 54 minutes. It was a provisional, larger figure.

---

## 0.2.174 — 2026-10-12

### Long songs are no longer cut off at 15 minutes

A YuE2 song now gets its own time limit from its length: twice the slowest Mac's speed for a song that
long, plus loading. A 3½-minute song gets about 1½ hours instead of the music lane's 15 minutes, which
had stopped 3–4 minute songs on the 16 GB and 24 GB M4 Macs part-way through.

---

## 0.2.173 — 2026-10-12

### The admin pages work from a phone, with a password

Open `http://<this Mac's Tailscale address>:8787/admin` in Safari on your phone. A menu at the top
switches between Fleet (which Macs are online, what each is doing, versions, queue), Jobs, Models,
Providers, Create and Content Packs, and every page fits the phone's width. Sign in with a browser
password you set in **Settings → Browser password** (the gateway API key also works); Sign Out ends
the session, and changing or removing the password signs every browser out. Fleet in the browser is
read-only: pausing, updates and installs stay in this app.

---

## 0.2.172 — 2026-10-12

### A job can name the site it runs at

A request may now say `"site": "<location id>"` (for example a YuE2 song you want measured on one
location). The job stays at that location, including when a Mac fails and it is tried again, and the
field is never sent on to a Mac. A site that cannot take work is ignored and the job routes as usual.

---

## 0.2.171 — 2026-10-12

### Sung songs take their turn like any other job

Every Mac makes one thing at a time, and scene pictures are the lowest-priority work, so YuE2 songs no
longer need a limit of two at a time or a background-only rule. A song request keeps the priority it
was sent with, and Story Studio sends sung songs as normal work. The per-model limit is still there if
you want one: `provider-concurrency.local.json` → `"models": {"vanch007/mlx-Yue2-3B": 4}`.

---

## 0.2.170 — 2026-10-12

### Sung songs (YuE2) take at most two Macs at a time

YuE2 sings on the 16 GB Macs, which also make most of the images, and one song keeps a Mac busy for
about half an hour. So at most **two** YuE2 songs now run across the whole fleet at once, and they
are always background work: a song only starts on an idle Mac and never ahead of a waiting image,
whatever priority the request asked for. The fleet capacity reading tells Story Studio the same, so
it keeps only a short line of songs waiting. The number can be changed in
`provider-concurrency.local.json` (`"models": {"vanch007/mlx-Yue2-3B": 3}`; `0` lifts it).

---

## 0.2.169 — 2026-10-11

### Breeze narration works for every voice that has segments

Breeze TTS 2 clones from a clip **and its words**, and refused every voice with no saved transcript.
A voice added from a loose clip never has one, so its words are now taken from the timestamped
segments its annotation made (a Whisper loop such as "stats stats stats…" is left out). A staged
clip is also staged again when its words change: before, a corrected transcript went unused until
the 36-hour reuse window ran out or the Gateway restarted.

---

## 0.2.168 — 2026-10-11

### Add one voice without adding the whole folder

- **Add Voices… now picks audio files.** Pick one clip, or ⌘-click several; each becomes a voice.
  Picking a folder still works as before.
- **The same recording is never added twice.** If a clip is already here, under any name, it is
  skipped and the result says which voice it already is.
- **A name that is already taken is a question, not a second voice.** If you add a different
  clip called "Chris" while a Chris exists, you choose: **Keep Both** (it becomes "Chris 2"),
  **Replace the Clip of the Existing Chris**, or **Skip**. Replace keeps that voice's id, so
  channels that use it keep working; the new clip is transcribed again and the old one is kept
  in the gateway's voice backups.
- **Remove a voice, and bring it back.** More → **Remove Voice…** (or right-click a row). The
  confirmation names the voice, its clip length and when it was added; if Story Studio narrated
  with it in the last 30 days, a second confirmation says who and when. Nothing is deleted: the
  files move to the gateway's voice backups, and More → **Restore Removed Voice…** puts it back
  under the same id.
- **Two voices with the same name can be told apart.** A new **Clip** column shows each of your
  voices' length and the date it was added.

### Voice clips travel lighter and are sent less often

- **A voice's clip is sent to a location in lossless FLAC instead of WAV**: the same sound, sample
  for sample, in about 40% of the bytes (Christopher: 3.7 MB instead of 9.3 MB). Each clip is
  checked before it is sent: if its FLAC would not decode to exactly the same samples, the WAV goes.
- **A clip sent to a location is reused for up to 3 days** (it was 6 hours), so narration does not
  re-upload it for every chapter. Today's Studio Hub keeps a sent clip for 2 days, so reuse stops
  12 hours before that (36 hours) until the Hub keeps clips longer.

## 0.2.167 — 2026-10-11

### Your Macs can now look at a picture and describe its shape

- **New: `POST /v1/vision/analyze`.** Story Studio can send a picture and ask for any of four
  things: how far away each part is (`depth`), where the main subject is (`subject`), points of
  interest (`points`), and areas like sky, water, clouds or fire (`segments`).
- **It runs on your Macs and costs nothing.** The work goes to Studio Hub's new vision engine
  (Hub 0.6.166) with Depth Anything V2 Small and Florence-2. Only fleet models serve here, so it
  can never spend money.
- **It stays close to the picture.** The Mac that made the picture is asked first, then a Mac at
  the same shop as the caller, then any other Mac, so less traffic crosses the internet. A Mac that
  is busy, paused or missing the model is skipped (up to three are tried).
- **When no Mac has the model,** the answer says so (`503 NO_VISION_ENGINE`) in plain words.
  `GET /v1/vision/models` lists the vision models and the tasks each one does.

## 0.2.166 — 2026-10-11

### A free meaning check for Story's ideas, on your own Macs

- **New: `POST /v1/embeddings`.** Story Studio can send a list of short texts and get back one
  vector per text, in the same shape OpenAI uses. Two ideas that say the same thing in different
  words get vectors that point the same way, which is how Story will spot a repeated idea.
- **It runs on your Macs and costs nothing.** The work goes to Studio Hub's new Embeddings Studio
  (Hub 0.6.165) with the model BGE small en v1.5 MLX bf16. Only fleet models serve here, so it can
  never spend money. It does not wait behind narration or image jobs.
- **When no Mac has the model,** the answer says so (`503 NO_EMBEDDINGS_ENGINE`) and Story keeps
  its word-overlap check.

## 0.2.165 — 2026-10-09

### Big music batches fill idle Macs without slowing production

- **Story Studio can now send work as "background".** Background jobs, such as a channel's song
  pool, run only on Macs that are idle and that no normal job is waiting for. Narration, scene
  images and chat always go first, even when they arrive after hundreds of queued songs.
- **Nothing is stopped halfway.** A song that has started finishes (about 2.5 minutes), and that
  Mac takes the waiting normal work next.
- **Two projects share fairly.** When two song pools are queued, they take turns instead of the
  first one using every Mac.
- **The queue says what is background.** The Jobs window shows a waiting background job as
  "Background", and its summary and Fleet → Jobs say how many are waiting, for example
  "312 background music waiting", so a big batch does not look like a jam.

---

## 0.2.164 — 2026-10-09

### Claude models show their real output limit

- **Claude Haiku 5.5 now lists 128,000 output tokens instead of 4,096.** The Gateway used to
  publish its own small default for every Claude model, so Story Studio split scene plans into
  small groups of 10 scenes and sent the whole episode context again for each group. It now reads
  the limit Anthropic states for each model in its model list.
- **Your own limits file still applies when Anthropic says nothing.** If Anthropic's list does not
  carry a limit for a model, the Gateway uses your `model-limits.local.json` value, and then the
  old default, as before.
- The limit shows up after the Anthropic model list is next refreshed. Nothing else changes.

---

## 0.2.163 — 2026-10-09

### Claude models can write scene plans and image prompts

- **Claude now returns JSON that follows Story Studio's layout exactly.** When Story Studio asks
  for a set layout (scene plans, image prompts), the Gateway sends that layout to Anthropic, which
  only lets the model write answers that fit it. Claude Haiku 5.5 is now listed as supporting
  structured output, so Story Studio can use it for those steps. Gemini can stay as the backup.
- **This is based on what Anthropic says about each model.** Any Claude model that Anthropic lists
  as supporting structured output (Haiku 5.5, Haiku 4.5, Sonnet, Opus) is marked as supporting it.
  The flag shows up after the Anthropic model list is next refreshed.
- **An answer that does not fit is a failed try, not a result.** If Claude stops early, refuses, or
  returns something outside the layout (for example the wrong number of scenes), the Gateway does
  not pass it on. A model pool moves straight on to its next model.
- Requests without a layout are sent exactly as before.

---

## 0.2.162 — 2026-10-09

### The model list answers at once, even with the Jobs window open

- **The model list no longer waits behind other work.** Story Studio's model and voice-model lists
  (`/v1/models`, `/v1/tts/models` and the rest) are now refreshed in the background and answered on
  threads of their own, so they answer in milliseconds even when the rest of the gateway is busy. A
  model you switch on or off still shows up in the very next answer.
- **The Jobs window no longer slows the gateway down.** Its 5-second refresh used to make the
  gateway count every stored file (about 50,000) on each tick. Those counts piled up until nothing
  else could be answered, and Story's model list timed out. The Jobs window no longer asks for that
  count. It also skips a tick while the previous read is still out, and waits longer when the
  gateway is slow.
- **Storage totals in Settings are counted once at a time.** If several reads arrive together, they
  share one count instead of each walking the disk, and the Trash Folder is walked once per count
  instead of twice.

---

## 0.2.161 — 2026-10-09

### An empty or broken file is never handed over as a finished job

- Before the Gateway marks a job done, it now checks the file it got back: it must not be empty, a sound or video file must be complete (an m4a or mp4 needs its index), and a picture must have a real image header. A file that fails the check is treated as a failed try, so a job for one of your Macs is sent to another Mac, as any other failure would be. The Mac that made the bad file is not told it was received, so it keeps its copy.
- This happened once: a narration for EP0038 came back from a Mac as an empty file and was marked done, and Story Studio failed three times collecting it. Of about 20,800 files kept, that is the only empty or broken one.

## 0.2.160 — 2026-10-09

### A Mac job caught mid-send by a restart is sent again, not parked

- If the Gateway restarts while a job for one of your Macs is still being sent, it now sends that job again to the same Mac group after the restart, instead of putting it in Jobs → Needs recovery. The Studio Hub recognises a repeat of the same job, so if the first send arrived it simply carries on with that one; it never runs the job twice, and it never sends it to a second group.
- If the repeat cannot be confirmed, the job goes to Needs recovery as before. Paid cloud jobs (fal, kie, xai, bfl, minimax) are unchanged: they still wait for you, so nothing is charged twice.

## 0.2.159 — 2026-10-09

### A stop pauses new jobs the moment it is asked

- The Gateway now stops starting new jobs as soon as it is told to stop, instead of only after it has finished answering open requests from Story Studio. Jobs being sent still get their 15 seconds, and waiting jobs stay queued.

## 0.2.158 — 2026-10-09

### Restarting or updating the Gateway no longer strands jobs

- When the Gateway stops, it first stops starting new jobs, then waits up to 15 seconds for jobs it is sending to be accepted by the Mac or the provider. Waiting jobs stay in the queue untouched and start after the restart. Jobs already rendering on the Macs carry on and are picked up again after the restart.
- Before, the app stopped the Gateway 2 seconds after asking, so a job caught mid-send landed in Jobs → Needs recovery (2, 10 and 1 jobs in the last three updates). Now the app waits for the Gateway to finish.
- Updates call a new "prepare restart" step that does the same before the app quits, so even an update from an older app version loses nothing.

## 0.2.157 — 2026-10-09

### The Jobs list loads in a blink again

- With about 51,000 jobs on record, opening Jobs took 6 to 21 seconds, and meanwhile the app could show "The gateway is not running" and "Starting…" although the Gateway was fine. The job database now has indexes for the list, its filters, the counts and the dispatcher's queue, so a 200- or 500-job page takes a few hundredths of a second on a copy of this Mac's data (was 0.6 to 1 s there, and much longer under live load). The first start after the update builds the indexes once, in about a second or two.
- The Gateway's health check now has its own lane, so a busy moment can no longer make the app think the Gateway has stopped.
- If a page ever does time out while the Gateway is answering, it says "The gateway is busy, retrying…" and tries again on its own, instead of "The gateway is not running".

## 0.2.156 — 2026-10-09

### Re-routed jobs no longer pile onto one Mac

- When a job is moved after a failure or a silent Mac, the Gateway now counts the jobs it has just sent and each site's lane limit. A burst of moves spreads across sites with room, and none goes past a site's limit. Overnight, This Mac (voice limit 1) had been given up to 39 moved narration jobs.
- If every site that could run the job is full, the job waits in the Gateway queue and goes out as soon as a lane frees, instead of queueing on a full Mac.

### Every site going quiet at once no longer abandons renders

- If 3 or more remote sites stop answering within the same minute, the problem is on this side (the Gateway or the network), not at the Macs. The Gateway keeps asking about those jobs instead of re-running them elsewhere, until the sites answer or the job's own time limit ends it. One site going quiet on its own is still moved after a minute, as before.

### Qwen thumbnails prefer fast Macs

- Qwen Image jobs now go where they will finish soonest: This Mac and the M4s first, using each Mac's measured times (or its chip when it has none yet). A slower M1 or M2 takes one only when waiting for a fast Mac would finish later. Other models route as before.

### Quieter retries when the job database is busy

- A job the Gateway could not move because the database was busy is retried after 1 s, then 2 s, 4 s and so on up to 1 minute, and the log says so once per job instead of every half-second.

## 0.2.155 — 2026-10-09

### Claude models fix themselves when they stop taking temperature

- If a Claude model the Gateway does not know yet answers "`temperature` is deprecated" (or "not supported"), the Gateway asks once more without it. The job succeeds, its result notes that the temperature was dropped, and Story Studio stops seeing a temperature control for that model. Any other refusal is not retried, and nothing is retried more than once. The Gateway remembers this until it restarts.
- **Qualification** now has rows for Anthropic models (Claude Haiku 5.5 among them). Their token counts were stored under Anthropic's own names and were not being read.

## 0.2.154 — 2026-10-09

### Claude Haiku 5.5 works again

- Claude Haiku 5.5 failed every chat with "`temperature` is deprecated for this model." The Gateway now leaves temperature out for Claude models that no longer accept it: Haiku 5.5, Sonnet 5 and 5.5, Opus 4.7 and later, and Fable. The job's result notes that the temperature was dropped.
- Story Studio no longer sees a temperature control for those models. Older Claude models (Haiku 4.5, Sonnet 4.5 and 4.6, Opus 4.5 and 4.6) keep it.

## 0.2.153 — 2026-10-09

### Clear the Jobs list in one go

- **Jobs > Failed** now has **Remove Failed From List (N)**. It hides settled failed jobs from the list after you confirm. Nothing is deleted: files, billing records, totals and analytics stay the same. Jobs that need recovery, are running or are waiting are never touched. Tick **Show removed** to see them again. A removed job that changes later (for example, a late result) comes back by itself.
- **Jobs > Failed** and **Jobs > Needs recovery** now have **Resolve All Needing Recovery (N)**. After you confirm, it does for every job what the per-job buttons do: it asks the Hub first and keeps a finished result, and otherwise discards or closes the attempt. Story Studio releases those jobs just as it does after the per-job buttons. The summary line says how many were recovered and how many discarded.
- Both buttons only act on jobs that were already there when you saw the count. Work that fails or goes on hold after that is left for you to see.

## 0.2.152 — 2026-10-09

### Models you install on every Mac get used on every Mac

- When **Fleet > Models > Install on every Mac** finishes on a Mac, that model is switched on for its group, so jobs go there too. If you had switched it off for that group, it stays off.
- A group you add now starts with every model it has installed switched on. Turn off what you don't want under **Models**. Nothing is copied from This Mac. Phonon-2, Pocket-TTS and shelved models are never switched on for you.

## 0.2.151 — 2026-10-09

### A shop Mac in Lite Mode that is switched off is no longer a warning

- A Mac in **Lite Mode** that is switched off (a shop Mac at night or on holiday) now shows **Off** in grey, with the line "Switched off — Lite Mode. Expected; it gets no work until it is back." It no longer counts toward "groups need attention" or "nodes offline", no longer turns its group amber, and sends no Telegram offline alert. The group's Nodes line says, for example, "1 of 3 online · 2 off (Lite Mode)".
- The Gateway remembers each Mac's last Lite Mode setting, so this works while the Mac is off. A Lite Mode change saved for an offline Mac (**Fleet > the Mac's "…" > Lite Mode…**) counts straight away.
- A Lite Mode Mac that stays off for more than 14 days gets one quiet information line, not a warning.
- Macs that are not in Lite Mode warn exactly as before when they go offline.

## 0.2.150 — 2026-10-08

- Replaced by 0.2.152 before it was installed.

## 0.2.148 — 2026-10-08

### Installing a model on every Mac skips Macs that are too small

- **Fleet > Models > Install on every Mac** no longer queues a Mac with less memory than the model needs. A Mac whose Studio Hub turns the model down for memory now shows **Skipped: Not enough memory for this model.** instead of Failed. Nothing was downloaded on those Macs either way.

## 0.2.147 — 2026-10-08

### Image jobs get time for their step count and Mac

- A Studio Hub image job now waits as long as that job needs on the Mac running it: more steps or a slower Mac get more time, up to 2 hours. Studio Hub 0.6.159 works this out for each job, and the Gateway uses the same number.
- Short jobs keep at least the 30 minutes they had before. With an older Studio Hub nothing changes.

## 0.2.146 — 2026-10-08

### Find local models and keep installation reports compact

- Searching **gemma 4** now finds Studio Hub models whose names use hyphens. Spaces, hyphens, underscores, slashes and dots match consistently in the list, counts and bulk actions.
- Manage availability in **Models** using the checkbox beside a model. Choose a Studio Hub provider to find its local models; model availability and a Mac's manual pause remain separate controls.
- **Fleet > Models** keeps installation reports compact, with progress, failures and Stop visible. **Show details** opens a bounded scrolling list of every Mac's result and keeps your place during refreshes.

## 0.2.145 — 2026-10-08

### Choose each Mac's work priorities

- In **Fleet > Machines**, open a Mac's action menu and choose **Work Priorities**. Choose no preference or rank its enabled functions. The setting belongs to that Mac and can also be changed locally in Studio Hub.
- Current work finishes first. Eligible waiting work follows the Mac's ranking, with older work first at the same rank. Other enabled work can use free capacity.
- Changing priorities keeps manual pauses and enabled functions in place. Older Hubs clearly require an update before these controls can be edited.
- Updated Hubs can keep a bounded local Chat request waiting regardless of its rank. Existing admission limits and uncertain-completion recovery stay in force.

## 0.2.144 — 2026-10-08

### Waiting for local LLM work

- A Studio Hub Mac that prefers LLM jobs can keep an additional eligible Chat request waiting. Current work finishes before queued LLM work starts, and TTS/image work uses idle time when no LLM is waiting. Older Hubs and cloud providers keep their existing limits.
- Waiting has a time limit. A request cancelled before it starts cannot begin later; cancellation after work starts does not claim the model has stopped.
- If a Chat completion's outcome is uncertain, Gateway keeps it available for recovery instead of automatically submitting it again.

## 0.2.143 — 2026-10-07

### Reliable Gateway shutdown

- Stop and Restart wait for the previous Gateway process to exit, so an old backend cannot keep working against the job database after its port closes. A restart refuses to overlap a process that has not stopped.
- Shutdown checks the known process identity before stopping it. The Keep Gateway Running setting still controls whether quitting the app leaves the Gateway running.

### Stronger interface verification

- The installed-app checks now follow the current sidebar, Fleet tabs and action menus, and Settings layout.
- Each page is checked independently at a 700 pt window height, so one page cannot affect the result for another. Fleet checks read the actual row menus and discover the Machines table even when warnings are shown.

## 0.2.142 — 2026-10-07

### Reliable job records under load

- API reads, queue dispatch, job completion, analytics and shutdown now take turns on the Gateway's shared job database connection. Concurrent activity can no longer leave a finished worker recorded as running because SQLite was used by two threads at once.
- Existing idempotency and restart-recovery rules are unchanged; this fix only makes access to the job record take turns.

## 0.2.141 — 2026-10-07

### Safer paid requests and fair recovery

- A paid pool stops when a sent request has an uncertain outcome and keeps the responsible member's evidence for recovery. Free fallback and definite provider refusals still advance.
- A Mac that is both offline and resting after a memory refusal is counted once. Recovery checks rotate through bounded batches of Hub holds, so older jobs get a turn; cloud holds stay available for operator review.
- Account job history now returns the requested page and the total number of jobs.
- A fresh Story Update Now request survives an older failure report. Updated Story Macs acknowledge the particular attempt, so a new failure stops that request without losing a later explicit retry.

### Clearer keys

API key is now called Gateway API Key; the release signing key is now the App Update Key

Settings explains which key is private and which fingerprint can be shared. Public key encodings are available under Technical details. *(Superseded in 0.2.186: the App Update Key section now shows one whole key with a Copy button — the base64 form, the fingerprint and the Technical details disclosure are gone.)*

## 0.2.140 — 2026-10-07

Received artifacts:
- Retired artifacts now pass through a recoverable Gateway archive before moving to macOS Trash: the archive keeps them for seven days and moves the oldest out when it exceeds 20 GiB. Existing three-day/eight-GiB retention, saved settings, pins, the one-hour grace period and active-job input protection remain. Failed transfers keep the files and report the problem. macOS Trash does not free disk space until emptied; its 30-day setting belongs to macOS.

## 0.2.139 — 2026-10-07

Fleet:
- The Fleet page shows when a Mac's Studio Hub update failed, and why. A Mac (controller or node) whose update failed in the last 24 hours and that is not yet on the newest release gets an amber line under its row: "Update failed while installing: <reason> · <time>". The group header counts them ("1 Mac's update failed"), and in a paced rollout the step of a controller that reported an error shows it too. Needs Studio Hub 0.6.152 on that Mac; older Hubs say nothing.

## 0.2.138 — 2026-10-07

Model pools:
- A model refusing a request with HTTP 400 no longer prevents the pool from trying its other members, including the paid fallback. Different models accept different parameter formats, so one provider's refusal does not prove that every model will refuse it.
- Requests the Gateway identifies as invalid still stop. JSON output requirements, the free-model wait, cancellation, and upstream outcome tracking are preserved.

## 0.2.137 — 2026-10-07

Story Studio:
- Story Macs now check in every 30 minutes. A Mac shows as Offline only after 75 minutes of silence, and Update Now says it applies at the Mac's next check (30 minutes at most), with "Next check by ~HH:MM" once the Mac has checked in.
- A Story release can no longer be offered half-published. The file is copied under a hidden name, flushed to disk and moved into place first; the signed release is written only after that, and a release whose file is missing or the wrong size is not offered at all. Publishing a newer one swaps both together and sends the old file to the Trash Folder.

## 0.2.136 — 2026-10-07

Story Studio:
- Publish Story Studio updates from the Gateway; see each Story Mac, its version and its jobs; Update Now per Mac.
- Fleet has a new Story Studio tab: every Story Mac that has checked in, its version against the published release, its queue, and how many of its jobs are waiting or running on this Gateway.
- A Story Mac checks in about every two minutes, installs a published update once its running jobs have finished, and goes back to its old version if the new one does not start.
- The queue view counts waiting and running jobs for each Story Mac and each Story job group.

## 0.2.135 — 2026-10-07

Fleet:
- A Mac that turns a job down for lack of memory is not offered that model again for 15 minutes, or until it clearly has the memory free. Jobs go to Macs that can run them instead of being turned down over and over.
- A job moved to another location no longer keeps its first location looking busy, so that location's Mac picks up the next job.
- A job waiting for a free Mac checks again within 15 seconds instead of up to 5 minutes. How many times it may be turned away before it stops waiting is unchanged.
- A location's offline Macs are no longer counted as room for work, so jobs are not held for Macs that are off.

Queue and jobs:
- Each waiting fleet job shows how many Macs could take it.
- The jobs list can be paged back past 500 jobs, and a new daily report shows each model's jobs, failures and queue wait.

## 0.2.134 — 2026-10-07

Pools:
- Creating and editing a pool sends the selected models correctly.
- Validation errors explain the problem instead of showing `[object Object]`; an unsuccessful save keeps your draft.

## 0.2.133 — 2026-10-07

Fleet:
- Action notices have balanced padding above and below their text.
- Success, warning, and error messages wrap to the available width without clipping their last line.
- Long notice headings truncate instead of forcing the window wider.

## 0.2.132 — 2026-10-06

Fleet:
- Idle Macs in small groups receive their share of jobs alongside larger groups. Free slot counts no longer exclude a one-Mac group from routing.
- Existing swap usage alone no longer raises a low-memory warning. Critical memory pressure and persistent idle low-memory warnings remain.

Builds:
- Replaced apps and generated build files are preserved in the project's trash archive.

## 0.2.131 — 2026-10-05

Jobs:
- Jobs that need recovery now show Recover Result / Close at the top of the job, and on right-click.

## 0.2.130 — 2026-10-05

Jobs:
- A job waiting on "Needs recovery" can now ask its Studio Hub to check the original Mac first: if that Mac finished the work, the result is kept instead of thrown away (`POST /admin/api/jobs/{id}/reconcile-hold`).
- A "Needs recovery" Studio Hub job that never learned its Hub batch (a submit that timed out) can now be closed from the Jobs window; it used to have no button at all.

## 0.2.129 — 2026-10-05

Fleet:
- Each group shows its network in the Groups tab.

## 0.2.128 — 2026-10-05

Fleet:
- See each Mac's network on its row (Groups and Machines), and on Locations when a Mac differs from its group.

## 0.2.127 — 2026-10-05

Installing a model on every Mac:
- Choose a Mac's or group's network from a list (or add a new one) — no more typos creating a separate network.

## 0.2.126 — 2026-10-05

Installing a model on every Mac:
- Fleet page stays responsive while a model installs on every Mac; a copy that fell back to the internet now says so right away.

## 0.2.125 — 2026-10-05

For testing models:
- Admin: run a test transcription on one chosen Mac (for model qualification).

## 0.2.124 — 2026-10-05

Steadier updates:
- A paced update no longer stops at a group that is still updating its Macs one by one; it waits while progress is being made.

## 0.2.123 — 2026-10-05

Fewer false alarms:
- A Mac no longer shows "Needs attention" when the quality check catches one bad narration piece and it's re-made; real failures still show.

## 0.2.122 — 2026-10-05

Macs on the same network share models:
- Macs on the same network now copy models from each other, so each network downloads a model from the internet only once.
- "Install on every Mac that can run it" puts a model on every Mac with the memory for it in one go.
- A new Network column on Fleet → Locations shows which network each group's Macs are on. This needs Studio Hub 0.6.146 on the groups.

Fewer false alarms and fewer failed narrations:
- A busy Mac with little free memory is no longer shown as a warning — only real trouble that stops a Mac from working is.
- Long narration pieces on slower Macs now get enough time: the time limit grows with the length of the text and the speed of the Mac.
- A narration piece that runs out of time is tried once more on another group instead of failing.
- Failed jobs now show which Mac they ran on.

## 0.2.121 — 2026-10-04

Smarter spreading of work across the fleet:
- A group only takes as many jobs of a model as it has Macs that can run that model, so heavy voice jobs no longer block a group's small Macs — light work starts on them right away.
- The fleet's limits for voice, image, music and transcription now follow the fleet automatically ("Automatic — follows the fleet"): add Macs and the limits grow by themselves. A number you set in Settings still works as a ceiling.
- Projects take turns: a small redo or a second project no longer waits behind a big batch of the same kind (needs Story Studio 1.4.252).
- Analytics shows real speed per model and Mac type (and real-time factor for voices). The old chip-based speed guess is removed.

## 0.2.120 — 2026-10-04

**Fleet loads faster.** The Gateway now reads each group's details all at the same time instead of one after another, so the Fleet page answers sooner when a group is busy.

**A group gets work when any of its Macs can run the model.** Before, a group could be skipped because its controller's own Mac lacked a model that another Mac in the group had ready. The Gateway also re-reads a group's models by itself when it starts and whenever that group's Macs change what they have, so you no longer need to refresh it by hand.

**Reset Job Counters…** on Fleet → Machines starts every Mac's done and failed counts from zero, and each count says "since …" so you know from when it is counting.

**A Mac with no model ready now says so.** Instead of showing "Ready", it shows "No model is ready on this Mac" as something that needs attention, because it will never be given a job until a model is downloaded.

**Install a model on any Mac from Fleet → Machines.** Open a Mac's menu and choose "Install a model…" to see the models that Mac has the memory for and start the download. This needs Studio Hub 0.6.143 on that group.

## 0.2.119 — 2026-10-04

The heavier voice models (VoxCPM2, Breeze, Qwen3 TTS 1.7B) are now sent to every group that has a 16 GB Mac able to run them. Before, a group was judged by whichever of its Macs happened to be listed last — so 1100, 1300 and 1400 were skipped whenever that Mac was an 8 GB one, and work queued at 1500 and 1600 while their 16 GB Macs sat idle.

## 0.2.118 — 2026-10-04

**Workstation mode is now called Lite Mode** everywhere you see it: the Fleet row menu, the settings sheet and its messages.

**A Lite Mode change for an offline Mac is kept and applied when the Mac is back.** You can change Lite Mode on a Mac that is switched off or out of reach; the Gateway saves the change and sends it as soon as the Mac answers again. Until then the Mac's row says "Lite Mode: Waiting to apply" and since when it has been offline. An offline Mac now shows "Offline" instead of the last Lite Mode state it reported.

**Adding a location on Fleet → Locations shows what is happening.** The button says "Adding…" while it connects, the new location appears in the list straight away ("Just added — reading its Macs…") and fills in by itself, and the list is tall enough to show it. A location that is slow to answer keeps its last details with "Slow to answer" instead of disappearing, and Refresh counts each location as it answers.

**A free chat job now moves to another group when a controller's own Mac is paused**, instead of failing. Nothing is charged and it does not use up a retry; if no other group can take it, it waits for a free Mac like other free work.

**Clearer messages when a Mac cannot be reached.** Connection problems on an offline Mac are said in one plain sentence instead of a list of technical errors.

## 0.2.117 — 2026-10-04

Work no longer waits at a group whose Macs are all paused; it goes to a group that can run it. If a job does end up queued there anyway, it is moved to another group within about ten minutes, with nothing charged. A job that can only run on a paused Mac stops straight away with "nothing was charged; run it again".

## 0.2.116 — 2026-10-04

When a Mac turns a job down because it is short of memory, the job now really moves on to another Mac. Since 0.2.114 the re-sent job carried a name the Mac refuses, so it failed and had to be retried by hand.

## 0.2.115 — 2026-10-04

A job meant for one particular shop Mac now stops straight away with "nothing was charged" when someone starts using that Mac, instead of waiting.

## 0.2.114 — 2026-10-03

Shop Macs can help the fleet without getting in the way.

### Workstation mode for shop Macs
A Mac the shop uses every day can now take fleet work in the background. On the Fleet page, choose **…** on that
Mac's row, then **Workstation Mode…**, and turn it on. Fleet jobs then run at background priority, so the person at
the Mac keeps a responsive computer. Two optional switches go further: **Only when idle** takes work only after the
Mac has had no keyboard or mouse use for a while (3 minutes unless you change it), and **Step aside when someone uses
it** lets the Mac finish the job it is on, then take no new work until it is free again. You can also set quiet
hours and hide the Studio Hub icon from the Mac's menu bar, so the app stays out of sight.

### Clear states on the Fleet page
Each shop Mac shows what it is doing: Working, Standing by (someone is using it), Stepped aside or Quiet hours —
in grey, like a paused Mac, never as a problem. Its row also counts today's jobs and step-asides. The sentence in the
sheet always describes exactly what your chosen switches will do.

### Handed-back jobs wait for another Mac
When a shop Mac steps aside before it starts a job, the job moves at once to another free Mac. If none is free, it
waits in line for one instead of failing, and it keeps its own job number the whole time.

## 0.2.113 — 2026-10-03

Every voice in your library can be previewed.

### Aiden, Mark, Vincent and Samuel play again
Their voice samples were QuickTime files saved with an ".mp3" name, which the player refused. The gateway now reads
what a file really is: these four are converted once to a proper WAV at startup (the originals are kept next to
them), new voices are converted when you add them, and previews always play. Narration with these voices uses the
converted sample on every Mac.

## 0.2.112 — 2026-10-03

Scene images go out across the whole fleet at once.

### Story Studio learns how much the fleet can take
The gateway now tells Story Studio how many jobs the fleet can run at the same time (about 55 today), so scene images
are sent to many Macs at once instead of one after another — an episode's images in well under an hour instead of
about nine. Image models also say how many requests their Macs can take.

## 0.2.111 — 2026-10-03

Story Studio can see the narration quality check.

### Per-section checks reach Story Studio
Studio Hub 0.6.137 checks every narration section and re-makes the ones that come out wrong. The gateway now passes
that record on, so Story Studio's Audio page can say "all 9 sections came out right the first time" or which section
had to be re-made.

## 0.2.110 — 2026-10-03

Time limits count a job's real running time.

### No fresh clock after a restart
A job the gateway picked back up after a restart used to start its time limit from zero again, so a stuck job could
run far past its limit if the gateway restarted a few times. It now counts the time the Mac has really spent on it.
Long narration is split into pieces sized for the slowest Mac (Studio Hub 0.6.137), so legitimate work fits.

## 0.2.109 — 2026-10-03

Faster writing through pools, and long narration split the way each Mac can handle.

### A pool writes several things at once
A pool such as "Gemini Free — all" used to answer one request at a time, so three scripts were written one after
another. It now runs as many at once as its free providers allow (about 5 for Google's free tier), and still only
touches its paid member as a last resort.

### Narration chunk size from the Macs
When a Studio Hub says how much text a voice model should get per request, Story Studio is told that number and
splits long chapters to match, and the gateway waits long enough for each piece.

### Your voice library, labelled honestly
Voices kept by this gateway (like "Chris") are shown as "Gateway voice library" instead of "Studio Hub (this Mac) ·
Qwen3 TTS" — they work on any Mac and any voice-cloning model.

## 0.2.108 — 2026-10-03

Honest prices for local models, and the paid safety net kicks in after a long wait.

### Your own Macs' models say they're free
Image, voice and music models running on your Studio Hub Macs now say they cost nothing, like their writing models
already did. (Story Studio 1.4.249 or newer is needed — older Story Studio read this as "no reference images".)

### Paid pool member after 15 minutes of rate limits
If the free models in a pool stay rate-limited for 15 minutes in a row, the pool now uses its paid member instead of
failing, and the log says so. Briefly limited free models still mean a short wait, never a charge.

## 0.2.107 — 2026-10-03

One colour rule for every state.

### Running is amber, failed is red, done is green
Jobs, the Overview, Fleet ("Doing now", running jobs, rollout steps) and the web console all use the same colours:
anything in progress or waiting is amber, anything that failed or needs recovery is red, anything finished is green,
and cancelled or paused is grey. A failed job that is held for checking now says "Needs recovery" in red instead of
an amber "failed".

## 0.2.106 — 2026-10-03

Free work keeps going when a Mac goes quiet, and the Fleet page says why a Mac is offline.

### Work moves on instead of getting stuck
If a group stops answering while it's making something on your own Macs, the job moves to the next group that has the
model (never to one you paused); the first result to arrive is kept and the other is cancelled. A busy Mac or a
hand-off that times out now means "waiting", not "failed". Paid cloud work keeps the careful rule.

### Why a Mac is offline
Offline Macs show the reason when it's known: "Force-quit at 09:36", "Crashed (restarted) at …", "Stopped by macOS for
low memory", "Mac restarted, asleep or lost network". Needs Studio Hub 0.6.136 on that Mac.

## 0.2.105 — 2026-10-03

Studio Hub updates go out one group at a time.

### Paced rollout
"Publish a Studio Hub Release…" now updates This Mac first, then each group's main Mac one after another, waiting
for each to come back on the new version before the next. Inside each group the Macs update one at a time, each
finishing its current job first, so no work is cut off. Fleet → Locations shows "Updating 1200 (3 of 6) · 1100 done ·
1300–1600 waiting" and how many Macs in each group are on the new version, with Stop and Resume. If a group's main Mac
doesn't come back within 30 minutes the rollout stops and tells you; a group that's offline is skipped and gets the
update automatically when it's back. It carries on by itself after a gateway restart. "All at once" is still there for
emergencies.

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
- **Fleet → Locations → Release shows each controller's trusted App Update Key** (Hub
  0.6.51+): the fingerprint it reports for app releases. It reads “this gateway”
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

- **Settings shows the App Update Key** — the public key you install on a
  controller (with `POST /api/hub/release/trust`) so that controller accepts
  releases pushed from here, plus the fingerprint to compare. The private key stays in the
  gateway's data directory, is written `0600`, and is never returned by an API.
- **Publish a release** from Fleet → Locations: choose the `.dmg` exactly as
  built, give the version and build, and the gateway signs a descriptor — sorted
  keys, no whitespace, UTF-8, the sha256 *inside* the signed bytes — and pushes it
  to each controller with that controller's Fleet token.
- **The report is what the controller holds**, read back from
  `GET /api/hub/release`, never "the request was accepted". Each controller shows
  the release it holds, the last push outcome, and the controller's refusal
  reason **verbatim**. A controller that has not had the key installed yet refuses
  with its own sentence, and the Gateway names the App Update Key public-key path
  and the fingerprint to compare.
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
