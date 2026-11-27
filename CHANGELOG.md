# What's new in Gateway Studio KH

Newest first. This is the source for **What's New** in the app and web admin.

Each release carries its version and date, the way Studio Hub's notes do, so an entry says *when* it
arrived instead of only what it does. `tests/test_whats_new_page.py` fails if the newest section is
not a `## <version> — <date>` heading matching `VERSION`, so the next release cannot forget. The rows
below the newest group are the history from before this shape existed: their titles are `###` like
every feature title, and they are left as they were written.

---

## 0.2.309 — 2026-11-27

- **A site that has not answered for a model is no longer offered for it.** Routing and admission now require a live, complete per-model reading. A missing, stale or incomplete `model_supply`, or Macs that do not hold the model, keep the job off that site; it moves elsewhere or waits with a sentence that says which. "We cannot tell" and "this site cannot run it" still read differently.
- **A Mac with no live reading no longer counts as capacity.** `_eligible_macs` requires the site to list the machine and its heartbeat to be inside the 15-minute window, so a Mac that has gone quiet stops inflating the offered figure.

---

## 0.2.308 — 2026-11-25

- **API keys now rotate with a 24-hour overlap instead of cutting off sleeping Macs.** Settings now offers **Rotate** in place of Regenerate: pressing it writes a new key and restarts the Gateway so the new key works immediately, while the previous key keeps working across `/v1`, `/v1/content`, and admin routes until the expiry time shown in Settings (24 hours from the press). Unrelated `.env` entries are preserved, and a key inherited from the launch environment is refused plainly instead of pretending to rotate.
- **Hidden background launches stay windowless across relaunch.** The menu-bar app now honours `GATEWAY_BACKGROUND=1` alongside `--background` so background and self-update verification launches stay windowless and Dock-less.

---

## 0.2.307 — 2026-11-24

- **Every place a job was passed over now says why, where the job waits.** The Jobs view lists each site the Gateway tried and the reason it declined — busy, no such model, drained — instead of only naming the machine that failed.
- **The first look at a sleeping Mac is no longer mistaken for an outage.** After a quiet spell a site gets the full 20-second window instead of ten, so a Mac that was asleep stops reading as unreachable and the fleet count stops collapsing to whichever sites happened to be awake.

---

## 0.2.306 — 2026-11-23

- **Updates can hold your work again, and stop borrowing the shared token file.** Gateway now keeps its own hold token in its own folder, copying the shared one across once the first time it starts; the old shared file is never read again. When the token cannot be read, the app says so on screen in plain words instead of quietly refusing every update.

---

## 0.2.305 — 2026-11-23

- **Gateway can now update itself, and it holds your work first.** The app checks a signed feed in the background and installs a new version without a window, a prompt or a click. Before it fetches anything it puts its own work on hold through the same maintenance hold an install already uses, so no running job is cut off; if the hold cannot be taken, the update is refused instead of forced. Work resumes by itself when the check finishes.
- **Gateway now requires macOS 12 or later.** The updater's library is built for it.

---

## 0.2.304 — 2026-11-22

- **The Launcher card now says what Gateway is.** Gateway's row carries its category and its one-line description, so the app list describes what the app does instead of leaving it blank. No change in how Gateway works.

---

## 0.2.303 — 2026-11-22

- **One Mac's site can be asked again without waiting for the whole fleet.** "Refresh" on the Fleet page waits for every location to answer, so re-checking one amber row meant holding a full fan-out. Each location row and site card now has **Ask now**: it asks just that site, inside that site's own time limit, and says plainly whether the answer is current or carried from its last read — a silent site shows its Macs as "Not checked in since HH:MM" instead of as a fresh fault. Nothing is written by the question: it is an answer, not a refresh pass.
- **Every attention sentence now says when it was measured.** A row's cause carries "Measured at HH:MM" and a carried row says when it was last seen, so a stale figure can be shown without reading as current.

---

## 0.2.302 — 2026-11-22

- **No change in how Gateway works: this release is the one that proves the app's new home.** The project moved to its own folder, and 0.2.302 is the first version gated, built and published from there. The code is the same as 0.2.301; the version number and these notes are the only difference.

---

## 0.2.301 — 2026-11-21

- **Admission no longer counts capacity from a fleet read routing would refuse.** Routing has stopped choosing from a fleet snapshot older than 120 s since 0.2.289, but the two places that decide how much can run — the per-provider budget and the published live capacity — still summed whatever the cache held. A Gateway that lost contact with its sites could keep admitting work against a figure an hour old. Admission now reuses that same 120 s bound: past it, the per-provider budget falls back to the stated or cloud limit and says so — *"fleet read is 300 s old, past the 120 s routing bound; no live budget, so using the stated/cloud one: …"* — instead of presenting an old sum as live.
- **A displayed capacity figure now carries the time it was measured.** The live capacity publishes `generated_at`, `age_seconds` and `stale`, and its source line says *"the last fleet read, summed over reachable locations (fleet read is 300 s old, past the 120 s routing bound); not current"* when the read is old. A stale number may be shown; it may not read as current. Rows handed in by the caller's own fan-out are stamped as just measured, so the display never re-enters the fan-out to look fresher than it is.

---

## 0.2.300 — 2026-11-20

- **The app no longer stops itself when it clears an old login item.** Found the hard way on the publisher: 0.2.299 was installed, the app was started through its own launch job, and it quit **0.14 s later** — `launchservicesd` recorded `QUITTING: pid=13878` right after the one-time cleanup that removes an old `SMAppService` registration. Clearing that label stops the process wearing it, and the process wearing it was the job that had just started the app. The updater saw a process appear and never become healthy, rolled back, and the Mac was left with **no job and nothing running** for 17 minutes.
- **The cleanup now follows the rule the rest of the login-item code already follows:** the copy launchd started never touches its own job. A launch that is not the job still clears the row — that is the case the cleanup was written for, and a Mac in that state cannot be running under the job at all, because the job could not be imported while the label was taken. Both directions are tested natively.
- **A skipped cleanup is said out loud**, with its reason, so a Mac that legitimately still carries the row is visible in the log rather than silently doing nothing.

---

Older entries are in [docs/CHANGELOG-ARCHIVE.md](docs/CHANGELOG-ARCHIVE.md).
