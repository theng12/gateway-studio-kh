# Prompt: roll the `_trash` convention out to every project

Paste the block below into **each** project's session. It is self-contained, and the
first line tells that session which case it is in. The per-project notes after the
block are for your reference — don't paste those.

---

We are making one convention coherent across all of the owner's projects, and this
repo is one of them. It is about **deletions**, and it should take you under five
minutes.

**The rule is already global.** It now sits in the owner's global instructions at
`~/.codex/AGENTS.md`, so every session in every project already has it. What is
missing here is the *folder* — and this repo is one of the few that does not have one
yet. Do not re-litigate the rule; just install it locally.

## The rule

**Every removal goes to this project's `_trash/` — never `rm`.** That covers *all* of
it, not only a file the owner names: equally a file you decide is clutter while
cleaning up. Move it into `_trash/` (create the folder if it is absent), keep any
sub-structure so it is obvious where the file came from, and say where it went.

**`_trash/` itself is the owner's territory.** Never delete or tidy anything *inside*
it — if something in there should go, that is his call, not yours.

The one thing that is not a file is a **Git branch or remote ref**: record its name
and SHA in a note under `_trash/` before deleting the ref, and never rewrite
published history.

Nothing in `_trash/` is read by the app, tests or build. It stays **outside Git**.

## Do these four things here

1. **Create `_trash/` if it does not exist.** If it exists, leave its contents alone.
2. **Write `_trash/README.md`** — if the file is missing, create it; if it exists,
   bring it in line with this text (keep any table rows already there):

```markdown
# `_trash` — recoverable "delete"

Owner convention (2026-09-22): instead of permanently deleting anything, move it
here. It is out of the way but recoverable by digging in this folder.

**This folder is the owner's territory.** An agent must never delete or tidy
anything *inside* it, and **every removal it performs — requested or its own
cleanup — is a move into this project's `_trash/`, never `rm`**. Keep any
sub-structure so it is obvious where a file came from, and say where it went.

Git branches and remote refs are the one exception because they are not files:
record the name and SHA in a note here first, then delete the ref. Never rewrite
published history.

Nothing here is read by the app, tests or build. It is outside Git.

## Contents

| Item | Moved | What it is |
|---|---|---|
| _nothing yet_ | | |
```

3. **Add `_trash/` to this project's `.gitignore`** if it is not already there, so the
   folder and its contents can never be committed.
4. **Optionally mirror the rule in this project's own working rules.** It already
   applies from the global file, so this is only for local visibility (add a
   `## Working rules` section if none exists):

```markdown
- **Every removal goes to this project's `_trash/` — never `rm`.** This covers *all*
  of it, not only what the owner asks for: a file he names, and equally a file you
  decide is clutter while cleaning up. Move it into `_trash/` (create the folder if
  absent), keep any sub-structure, and say where it went. `_trash/` itself is his to
  sort out: never delete or tidy anything *inside* it. The one thing that is not a
  file is a Git branch or remote ref — record its name and SHA in a note under
  `_trash/` before deleting it, and never rewrite published history.
```

## What NOT to do

- **Do not go looking for things to clean up.** This is not a cleanup task; it is
  installing the convention. The existing contents of `_trash/`, if any, stay exactly
  as they are.
- Do not `rm` anything as part of this. Moving a file is the whole point.
- Do not move build inputs, live configuration, or anything the app reads.
- Do not commit `_trash/` — confirm `.gitignore` covers it before you finish.

Finish by telling the owner, in one line each: whether `_trash/` already existed,
whether you created `README.md`, whether `.gitignore` already covered it, and where
you put the rule. Then commit those changes the way this repo normally commits, and
push.

---

## Per-project notes (for the owner, not for pasting)

Checked 2026-09-22:

| Project | `_trash/` now | README | `.gitignore` | Rules file |
|---|---|---|---|---|
| `story-studio-kh` | exists (1 file: `format-niche-refactor-plan.md`) | **missing** | already ignored | `AGENTS.md` |
| `studio-hub-kh` | **none** | — | **not ignored** | `AGENTS.md` (19 lines, no rules section) |
| `media-studio-kh` | **none** | — | **not ignored** | `AGENTS.md` **and** `CLAUDE.md` (mirrored, 496 lines each) |
| `gateway-kh` (done) | exists, with README | present | outside Git (container root) | `AGENTS.md` |

So `story-studio-kh` mainly needs the README; `studio-hub-kh` and `media-studio-kh`
need the folder, the gitignore entry and the rule. For `media-studio-kh`, put the rule
in **both** files since they mirror each other.
