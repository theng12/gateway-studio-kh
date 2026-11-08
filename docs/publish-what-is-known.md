# Publish what is known, and nothing else

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

**Story Studio's notes:** *never let "the answer is none" and "I could not work
out the answer" produce the same value, and never invent a number the customer
will mistake for the model's own.*

Every field a provider does not state is published **`null`**, never as a
plausible default. The audit found `context_window_tokens: 0` on 37 rows,
`max_output_tokens` hardcoded to `8192` on every provider,
`supports_vision: false` for every model while 273 of OpenRouter's 445 accept
images, `provider_name` missing on video, keyed differently on transcription,
transcription claiming English-only *and* auto-detect, TTS languages as
stringified Python dicts, and Google's `inputTokenLimit`/`outputTokenLimit`
discarded.

**When a provider states it, read it.** Providers spell the same figure —
`context_length` vs `inputTokenLimit`, `top_provider.max_completion_tokens` vs
`outputTokenLimit`, `maximum_text_length_per_request`.

**A published `0` is a claim, and an `or` chain throws it away.**
`_first_stated()` exists because
`raw.get("context_length") or raw.get("context_window") or …` treats a stated
`0` as absent: `_stated` and `limits.gap` — both preserving a stated zero —
never see one, and the curated value is published *as the provider's*. Read with
a first-non-`None` lookup, never with `or`.

**A voice's language is stated or it is absent — never assumed English.**
Imports stored `"en"` for any clip whose metadata said nothing, making the
readiness gate's "it has no language" unreachable. A folder reads its
`metadata.json`, a loose clip a sibling `<name>.json`; nothing stated means
`""`, and `story_row` publishes the `"multilingual"` sentinel.
The ten voices imported before this still say `"en"`; the old import wrote it
either way, so the original is unrecoverable and clearing them would be a guess.
**The Hub requires a language** (`raise ValueError("language is required")`), so
a language-less voice is refused a fleet push locally in one sentence — the gate
working, not a regression.

**Changing an int to `null` is a breaking change for the reader.** It made an
unreachable fallback in Story Studio live (`normalizeMaxOutputTokens`
substitutes 8,192 and prints it as the model's own figure). The handoff's
field-change audit is the pattern: list every field whose *guarantee* changed,
not just its value.

**One value must stay invented:** TTS `maximum_input_characters` — Story Studio
calls `.toLocaleString()` unconditionally, so `null` would crash the page. Per
model where the provider states one (ElevenLabs publishes it), otherwise
**15,000** on a Studio Hub location, 25,000 on a cloud lane — each carrying
`maximum_input_characters_source` (`provider` / `curated` / `gateway`), because
"we chose this" has to be readable next to the number.

**The local lane presents one number, and chunking is Studio Hub's job.** Every
Studio Hub location publishes **15,000** (`catalog._TTS_CHARACTERS_LOCAL`),
because the Hub takes the complete script and renders sentence-safe sections.
The caller is told what it may submit; it is never told how the work is cut up.
That is the owner's *presentation* decision rather than a measurement, so the
gateway does **not** mirror the Hub's audited
`execution_contract.text_max_characters` (10,000–40,000 across the installed
set), wrong for a field the caller cannot act on. The mirroring was tried and
withdrawn on 2026-09-20.

**The one exception, and it sequences itself.** A model the Hub does not section
gets the whole script, so an engine cliff applies:
`catalog._HARD_CAP_CHARACTERS` (bark 150, orpheus 170) is consulted
**only while `long_form_delivery` is absent** for that model, read from the Hub's
own table. So Orpheus
publishes 170 today and **becomes 15,000 automatically** the moment the Hub
starts sectioning it — no gateway change, no coordination. The rule is one constant,
`catalog._TTS_CHARACTERS_LOCAL` (15,000), beside `catalog._HARD_CAP_CHARACTERS`; the
handoff that recorded the decision was removed from `_handoffs/`, so this file names the
code rather than a dead pointer.
`spark-tts-mlx` is *deliberately absent* from that table despite the Hub's 750:
as `auto-split`, 750 is where quality is best, not a limit on what may be sent.

**A uniform section size is not ≤200.** The Hub's two hard-cap engines are Bark
at **150** and Orpheus at **170**, so a 200-character section is past both and a
180-character one is still past Bark. Every other family sections at 280–3000; a
uniform 150 would mean 100 sections per 15,000-character request for engines
needing 5, against the Hub's measured coverage curve (99.4% at 288, 83% at 600).
Per-family budgets, which the Hub already has and justifies, are the right
shape; the gap was only that two families had none.

**Every limit now says whose number it is.** `context_window_tokens`,
`max_output_tokens`, `max_prompt_characters` and `maximum_input_characters` each
carry a companion `*_source` — `provider`, `curated` (the owner's
`model-limits.local.json`) or `gateway` (ours: a default or the recorded cliff).
Measured live: 48 chat rows `provider`, **18 `curated`**; the curated file's 24
entries are all `1000000/128000`, a flat default rendered as the model's own
window. The value is still published, now labelled (nulling it makes Story
Studio substitute 8,192). **The value and its source come from one function**,
because a separate "where did that come from?" reader is free to disagree with
the chooser the first time either gains a step — how the quote route kept
publishing a flat 40,000 after the catalogue stopped.
