# Short prompt: get every session to commit and push

Paste the block below into each session.

---

Before we continue: **commit and push everything you have finished, so nothing can be
lost.**

- Commit your work on the branch you are on, then push it.
- If it is finished and stable, land it in **`main`** — fast-forward if `main` is an
  ancestor of your branch, otherwise merge — and push `main`.
- If this project ships a Mac app and you have a release, make sure its **DMG is
  built into `dist/`** and ready. Commit-and-push plus a `dist/` DMG is now a global
  rule.
- Check nothing is left behind: any branch of yours holding finished work should
  already be inside `main`. Branches still in progress can stay where they are.
- Do not delete anything. Every removal moves into this project's `_trash/`, never
  `rm`, and you never tidy inside `_trash/` — that folder is mine.
- Then reply with one line each: the branch, the commits pushed, whether `main` is up
  to date, and the DMG path if there is one. List anything you deliberately left
  uncommitted, and why.
