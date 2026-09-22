# Batch 4 — the remaining audit items (2026-09-20)

Moved out of `AGENTS.md` when it reached the instruction budget. Historical: every item is closed, and `AUDIT-BACKLOG.md` holds the outcome record.

**Nothing in the six audits is still open.** `AUDIT-BACKLOG.md` holds the full
outcome record — each fix, how it was measured, and what was deliberately left.
Four lessons worth carrying forward: read a provider's own classification rather
than guessing from a name (but keep `modalities[0]` for a provider that states
nothing — requiring one would have dropped 265 live chat models); a merged location
row answers for every site offering it (`_merge_capabilities` unions cloning, takes
the smallest character budget and the intersection of the speed range); a controller
is identified by its Hub's stable `site_id`, never its address (when that fix
invalidated the fleet suite's way of making a second location, the setup was relying
on the bug); and **a provider's API can move under you** — Kie took Suno off
`/api/v1/generate*` onto the *same* `jobs/createTask` route its images use, with the
api-model `ai-music-api/generate` at the top level and the tier
(`V6`/`V6_MINI`/`V6_WILD`) in `input.model`, the result in a `data` **array**, not
`resultUrls`. An out-of-date reference had it withdrawn and then restored (12
credits, in the backlog).
