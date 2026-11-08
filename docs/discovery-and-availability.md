# Discovery ≠ availability

Moved from `AGENTS.md` on 2026-11-08 (owner: "Trim everything"). `AGENTS.md` keeps the rules; this file keeps the incidents and measurements behind them.

A provider publishing 300 models must not put 300 rows in the picker. Nothing
reaches Story Studio until it is enabled. The local Studio Hub is the sole
exception (`auto_enable_discovered`), and that is deliberate. A provider whose
kind has no adapter stays discoverable and configurable but **its models are
withheld** — offering a model that fails on use is worse than not offering it.
`IMPLEMENTED_KINDS` in `providers/definitions.py` is the single source of truth,
asserted at import by `adapters/__init__.py`.

**A kind having an adapter does not mean every modality of it works.** That
assert compares *kinds*, and a modality can be missing inside one. Google's backend
delegates to the OpenAI-compatible shim serving `/audio/transcriptions`, so a
Google transcription row would publish and fail on use. Transcription is
implemented for `openai_compat` and `studio_hub`: **enable OpenAI's
transcription models, not Google's.** Verified end to end: `whisper-1` through
the gateway returns a real SRT. **Groq serves chat through the same shim** and
needs `GROQ_API_KEY` before its models appear. **Its Whisper cannot serve
transcription here** — measured live 2026-09-20,
`HTTP 400: response_format must be one of [json text verbose_json]`, and this
gateway must ask for `srt` — so `whisper-large-v3` and `whisper-large-v3-turbo`
are withheld.

**The gateway asks every cloud transcription lane for `response_format: "srt"`,
so a model that cannot answer in SRT must not be offered.** There, only
`whisper-1` can: the `gpt-4o-transcribe` and `gpt-transcribe` families accept
`json`/`text` only and answer 400, while `gpt-4o-transcribe-diarize`,
`gpt-live-transcribe` and `gpt-realtime-whisper` are diarising or streaming
endpoints. They are listed in `TRANSCRIPTION_SRT_UNSUPPORTED`
(`providers/definitions.py`) and withheld from Story Studio; they stay visible
in the inventory, so ticking one still works as a control, it just never
publishes. Google's transcription rows are impossible for a different reason (the
route does not exist) and are refused by the modality gate. Curated rather than
discovered, because no provider publishes which formats it accepts.

**fal publishes a per-endpoint JSON schema, and reading it is not optional.**
`https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=<id>` states the
exact fields and **types**. `duration` is a *string* enum on
`bytedance/seedance-*` but an *integer* on `minimax/h3-max` and
`google/gemini-omni-flash`, and `minimax/h3-max` declares
`prompt_expansion_mode` required despite a default. **fal's queue accepts any
body and validates at the worker**, so a wrong body reports the job `COMPLETED`
and fails when the result is fetched — a `422 {"detail":[{"type":"missing"…}]}`
in place of the artifact. Verified live: submitting `{}` to four fal endpoints
queued and completed all four, because the worker rejected each for a missing
`prompt`/`text`.

**fal's TTS lane is its ElevenLabs routes, and it has no voice list.** The model
schema carries no voice endpoint and no `enum` on `voice` — only twenty
`examples` and a default of `Rachel` — so `voices._fal_tts_voices` curates those
names as the lane's voices. Discovery keeps only fal categories `text-to-speech`
and `text-to-audio`; other audio endpoints do not accept the `text` this adapter
sends.

**xAI is OpenAI-compatible for chat and nothing else.** Its image API rejects
`size` outright — measured: `HTTP 400: Argument not supported: size` — so
delegating images to the OpenAI shim published three models that failed on use;
it takes `aspect_ratio` and `resolution` instead. Its video API is a
**deferred** endpoint (`POST /v1/videos/generations` → `request_id` → poll
`GET /v1/videos/{id}`). Hence `KIND_XAI` with its own adapter, chat only. Two
traps in the poll: **202 means still working, and a failure arrives as HTTP
200** with `status: "failed"` — so branch on the JSON `status`, never on the
HTTP code, or a failed generation reads as a success. The first frame is
`image: {url}`, not the `image_url` string the Python SDK accepts.

**Text-to-video lanes stay listed** (owner, 2026-10-26). Story Studio has had
multi-lane video, text-to-video included, since 2026-10-26, so do not "fix" a video lane by removing its text-to-video mode.

**Lyria music is Gemini's `:generateContent`, not a `:predict`, and the audio is
inline base64.** It lives at `candidates[0].content.parts[N].inlineData` — and
Google warns the lyrics are interleaved in the same array, so **scan every
part**; indexing `[0]` silently yields no audio. `lyria-realtime-exp` is
withheld: a bidirectional WebSocket session with no unary call could never
answer a one-shot request. Lyria is **paid-tier only**, which is why
`google-ai-free` does not declare the music modality. A 200 with candidates but
no audio part is transient on the preview model — observed live, succeeding on
the next identical call — so it is raised `retryable`; a safety refusal carries
`promptFeedback.blockReason`.

**OpenRouter publishes no music path at all.** Its catalogue lists
`google/lyria-3-*` with a live upstream provider, but its OpenAPI has zero
occurrences of "lyria" or "music" across 92 paths. Those rows are withheld: a
listing is not a serving contract.

**The console must not list a model that can never run.**
`ModelInventory.can_publish` is the single rule, used by both readers: the
catalogue asks what to publish, `_flat_models` what to *show*. The console's
weaker conditions advertised rows the catalogue dropped: nine OpenAI
transcription models of which only `whisper-1` can return SRT, four
`google-ai-free` Lyria rows, and OpenRouter's two Lyria rows.

**A proxy that speaks another provider's ids is the one exception to "a voice
belongs to its provider".** fal's ElevenLabs routes accept an ElevenLabs voice
id unchanged, now verified through the gateway (the owner's own voice, 51,871
bytes of MP3), and the owner has driven them that way many times. Matching a
voice on its provider alone offered only the twenty names fal happens to
`example`, so a voice saved from his own account was invisible for that lane
while working perfectly through it. `accepted_voice_providers` keys the exception
on the provider's
*kind* and the word `elevenlabs` in the endpoint — never on a hardcoded provider
id, and never for a lane that is not ElevenLabs.

**A provider voice known by its id can be saved without searching for it.** The
library search finds only what the provider will *search* for, so a voice known
from a share link had no way in. `POST /admin/api/voices/import` always accepted
an explicit `provider_voice_id`; the Voices window now has fields for it and for
the owner id. **ElevenLabs is the one provider that needs both**, because its
import route is `/v1/voices/add/{owner_id}/{voice_id}` — refused locally rather
than as an opaque 422.

**An upload past 25 MB is refused 413, not queued and then failed.** Every cloud
transcription API on the gateway caps a single upload there; a caller that
crosses it should be told the number, not handed a provider error after staging.
The ceiling is one flat number because no provider *publishes* its limit over
its API — `ponytail:` in `api.py` names the upgrade path (read it per model, as
TTS limits come from `maximum_text_length_per_request`). **413, never 402.**

**A voice the caller did not choose is never invented.** A missing `voice_id`
used to become a specific named speaker — `English_expressive_narrator` on
MiniMax, `alloy` on the OpenAI-compatible shim. Both refuse now, naming the
field. fal's `Rachel` and ElevenLabs' account-first-voice stay, because those
providers genuinely default and the catalogue offers that default *as a voice*.
**speed** follows the same rule: fal's routes accept 0.7–1.2 while the catalogue
published 0.5–2.0, and the adapter *dropped* anything outside its range — a
caller picked a speed it had been told was valid and got normal speed. It
refuses now, and `catalog._TTS_SPEED_RANGES` publishes the accepted range: what
is offered is what is sent.

**A toggle has to still be a toggle when the job runs.** The worker re-checked
*provider* enablement only, so a model switched off after submit still ran, a
mid-queue `catalogued`/`memory_eligible` flip did not stop the job, and a
modality the adapter cannot serve was still dispatched. It now asks
`enabled_models(modality)` and fails `MODEL_NOT_OFFERABLE` naming the model,
provider and modality. **The same rule is enforced at the door:**
`add_user_model` refuses a modality nothing serves, because `can_publish`
requires `serves()` and the form only asked whether the provider *declared* it
(Google declares six, serves four) — so it could create a row that was enabled,
counted, and never published.

**A model that is enabled but cannot serve is the worst state, and OpenRouter's
`:batch` variants are exactly that.** They are the providers' Batch APIs — half
price, turnaround in hours — and a normal `/chat/completions` request answers
**404 "This model is only available through the Batch API."** Such a row fails
the moment it is picked. **Nine of the owner's twenty-three enabled OpenRouter
models were these, and on 2026-09-19 he had them switched off** — each had a
working non-`:batch` sibling, so nothing was lost and OpenRouter went from 23
enabled to 14. Do not re-enable them; the sibling without the suffix is the same
model on a route that answers.

**"Studio Hub is running" is a measurement, not a file.** `hub_status()` used to
publish `running: bool(hub_base_url())` — the existence of a `hub-ready.json` —
so the page said "running" while the menu bar's `/api/health` probe and
`fleet._probe` reported it unreachable. `hub.hub_probe()` asks `/api/health` (no
token needed) and distinguishes **three** states, never two: no ready-file, a
ready-file with nothing behind it, and one that answers; the answer is cached 5
s. The same probe gates `start_annotation`, which promised "refuses without a
reachable Hub" while gating on URL presence — a stale file made every voice fail
without the actionable sentence. Anything acting on a location's *models* needs
its **`provider_id`** (`studio-hub-<controller-id>`), published per location on
both branches, so no client derives it.

**A Hub lists its own connectivity probes as machines, and they must not be
counted as the owner's hardware.** Studio Hub reports `probe-fleet` and
`probe-hub` — labelled "probe", permanently unreachable. This Mac read "3
machines, 2 offline"; six sites, ~12 broken machines. `fleet._is_probe_machine`
drops them from the machine list, machine table, routing capacity and activity
list, and `probe_machines_hidden` says how many were hidden. The online/offline
counts are derived **per machine** from the filtered activity list rather than
from the Hub's aggregate `pulse`, because the aggregate carries no machine
identity and a counted probe could not be taken out. Matched on the id, never
the label: the label is the owner's to change.
