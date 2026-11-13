# Gateway Studio KH — acceptance for create/editor pages

Owner rule (2026-11-13): a page is accepted only when it passes all three checks. Appearance,
contrast and layout are necessary but not sufficient. Reference:
`~/.claude/plugins/cache/studio-orchestration/studio-orchestration/0.3.0/skills/efficient-orchestration/references/completeness-gates.md`.

## 1. Task list + one end-to-end test per task (real API, form → request → result)

| Page | Tasks | E2E target |
|---|---|---|
| Image | text-to-image at a supported ratio/resolution; image-to-image with 1 and N references; character reference where accepted; unsupported pair refused with a reason | form values → create API → gateway job → result; refusal code for an over-limit/unsupported pair |
| Music | lyrics-driven generation (Yue2-3B); instrumental; style/title; duration/seed/language where declared; required lyrics disable Generate with the reason | lyrics reach the model in the request; non-lyrics model hides the field |
| Audio | saved voice; provider voice ID; Add a new voice opens Voices and returns; required voice disables Generate with the reason | chosen voice/ID in the request → audio job → result |
| Video | duration and ratio/resolution per the model; image-to-video with a reference/first frame; character references (Characters job) | form values → video job; refusal of unsupported combinations |
| Subtitle | source, language, model; timestamps/format where offered | source → transcription job → result |

## 2. Capability cross-check (live catalog, fails on drift)

For every selectable model and every declared capability — kind, max references, aspect ratios,
resolutions, lyrics, instrumental, voice, duration, language, seed, price-by-size — the rendered
form exposes a control or a recorded exception. The catalog is read live, never hard-coded; a new
capability without a control fails the test. Unknown reads "not stated", never 0 or no. One row per
model/voice; locations are `N of M` with Show locations, never separate rows.

## 3. Standard-practice comparison

| Page | Reference | Present / deliberately absent (reason) / missing (finding) |
|---|---|---|
| Image | Midjourney / Automatic1111 | prompt, negative prompt, aspect ratio, resolution, seed, references (count), character reference, price |
| Music | Suno / Udio | lyrics, style, title, instrumental, duration, voice if declared |
| Audio | ElevenLabs | saved voice profile + preview, provider voice ID, language, speed/stability |
| Video | Runway / Kling | duration, ratio, resolution, first frame/reference, character (stage 4) |
| Subtitle | Whisper / Descript | source, language, model, timestamps/format |

A missing control is a finding and blocks the page's gate. The independent reviewer runs the task
list and confirms the cross-check; a 4/4 on appearance alone is not a pass.
