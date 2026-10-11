# Voices are stocked, not configured

Moved out of `AGENTS.md` when it reached the instruction budget; the rules and one-line pointers stay there.

A voice is a reference clip plus a transcript, and **the transcript is not
decoration**: Qwen3 Base refuses a reference without aligned timestamps
(`REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED`). `voice_import.start_annotation`
transcribes each clip **through Studio Hub**. With no Hub it refuses.

- **The gateway owns the clip, and the folder is the whole voice.** A voice folder
  is `reference.<ext>` + `transcript.txt` + `segments.json` + `metadata.json`, so a
  lost `voices.json` is recoverable from the folders. Import copies into
  `DATA_DIR/voice-library/<voice-id>/` and never points at the folder it read from.
- **Two shapes are accepted, because both are things people have**: a folder of
  per-voice folders and a folder of loose audio files. The old importer read only
  the first, so a hand-dropped clip was silently skipped — the "I copied it in and
  nothing happened" case. **Since 0.2.168 files can be picked one by one** (`files`
  on `POST /admin/api/voices/add`): the picker took folders only, so adding one voice
  (Christopher, 2026-10-11) meant importing the whole 14-file folder.
- **The clip's bytes are a voice's identity, never its name.** The id is
  `voice-<sha256[:12]>` (a later 12-hex window of the digest when a Replace holds that id).
  Deriving the id from a file name let `Morgan` and `morgan!` overwrite one another,
  because the on-disk path *was* the id.
  - **The same recording, under any name, is skipped and named as such** (`skipped`,
    `duplicate: true`). Until 0.2.167 it was re-imported over the voice it already was,
    which reset that voice's language, gender and fleet permission to whatever the file
    stated — usually nothing.
  - **A different recording claiming a taken name is not added.** It comes back in
    `conflicts` ("A voice named X already exists", with each existing voice's length and
    import date) and the app asks: *Keep Both* (added as "X 2"), *Replace the clip of the
    existing X*, or *Skip*; the answer is sent back as `resolutions[source]`. Adding it
    silently is how the owner got second Chris/Christian/Leo rows that looked identical.
  - **Replace keeps the voice id** (channels using it keep working), keeps its name,
    language and gender unless the new clip states them, drops its segments so it is
    transcribed again, and makes it private again (permission was given for the old
    recording). The old folder moves to `DATA_DIR/voice-backups/<id>--<time>--replaced/`
    with its catalogue row as `entry.json` — never deleted, never aged out.
- **Remove moves, Restore brings back.** `DELETE /admin/api/voices/<id>` on one of this
  Mac's own voices moves its folder to `voice-backups/<id>--<time>--removed/`; `GET
  /admin/api/voices/removed` lists them and `POST /admin/api/voices/restore` puts one back
  under the same id. A voice narrated with in the last 30 days (the gateway's own TTS job
  history — it cannot see Story's channel settings) is refused `409 VOICE_IN_USE` unless
  `confirm_in_use=true`; the app shows who used it (`GET …/usage`) and asks a second time.
- **The list shows each local voice's clip length and import date** (`duration_seconds`,
  `imported_at`): measured at import, else read from the WAV header and the voice
  folder's creation time, which re-imports never changed.
  - Segments are dropped whenever the digest differs **or the older row has none**.
    The guard read `previous.get("audio_sha256") and...`, which skipped exactly the
    rows that could not be vouched for. One unnecessary re-transcription is the price
    of never publishing timings for a recording nobody can match them to.
  - A voice's catalogue id and its id on the wire are the same bytes: this 12 hex is
    what `shared_voice_id` sends the receiver, which will take nothing else.
- **A provider's voice list is merged, not concatenated.** `voices()` appended both
  lists, so a voice in both was listed **twice**; `find()` takes the first match, so
  preview and tier depended on which list held the row. The owner's entry wins, keyed
  on the id both sides build with `make_id`.
- **`_local_voices` prefers the writable catalogue at read time.** `VOICES_PATH` is
  resolved once at process start, so a fresh install keeps the shipped default — an
  added voice needs a restart.
- **Permission to clone and distribute a voice is the owner's, per voice, and never
  assumed.** He has private voices: *"that needs to be sth i intentionally tick. as
  some voices are private."* Import grants nothing — no `permission_acknowledged`, no
  `license` — because a default of "allowed" would copy a private voice to every
  machine, and adding a clip must never be mistaken for granting permission. Granting
  needs a confirmation naming what is granted; withdrawing is one act with no dialog.
- **A field a reader needs is published top-level, and `raw` is never one of them.**
  `Voice.raw` is the *provider's* answer and `as_dict()` does not serialise it, but
  the app read `row["raw"]["owner_id"]` and `row["raw"]["permission_acknowledged"]`,
  keys that never existed: "Add to my account" sent no owner (every ElevenLabs
  library import was refused 422; the result was discarded), and every local voice
  rendered "Private". `owner_id`, `permission_acknowledged` and `license` are
  top-level now. **A discarded result is how a refusal becomes indistinguishable from
  success** — the shape to look for. `permission_acknowledged` is `True`/`False` for
  this Mac's own voices and **`None`** for a provider's, because a provider voice is
  not the owner's to grant and `False` would render as a definite "Private".
- **The voice admin routes take either spelling of an id, and strip only *this
  Mac's* namespace.** The app sends what `/api/admin/voices` published
  (`studio-hub-local::voice-x`); the catalogue stores `voice-x`. Matching the stored
  form exactly made a grant report `changed: 0` and let "Share with fleet" past the
  403 that refuses a voice the owner never allowed. `voice_import.catalogue_id()`
  normalises both. **It strips `provider_id_for(LOCAL_ID)` and nothing else**,
  deliberately: a general "take the part after `::`" would make
  `elevenlabs-direct::voice-a` and `volc::voice-a` the same voice, which is the
  identity-from-a-label mistake the fleet id rules exist to prevent. In the app, ask
  a row's `provider_id` whether a voice is ours — never `id.hasPrefix(...)`, right
  only by coincidence.
- **Across locations, a voice is staged per job.** The gateway uploads the clip to
  whichever location runs the work, so every location can use every gateway voice;
  the `audio_sha256` match skips that upload where the controller already holds the
  clip.
- **Sharing a voice to the fleet needs two tokens.** `POST /api/hub/shared-voices`
  (Hub 0.5.3+) creates a **Hub-owned** voice on a controller, which fans out to its
  joined nodes. Those routes require the controller's **Hub token**; a *fleet* token
  is refused on purpose, *"and that difference is what keeps a sold node from being a
  sold controller."* A controller holds `hub_token` (fleet credential: dispatch and
  fleet reads) and `admin_token` (Hub token: shared voices only, never used to run
  work). A location without an `admin_token` is skipped with that reason. The
  gateway pushes to **controllers only** — six requests, not a hundred — because it
  never talks to nodes.
- **The two wire shapes differ, and getting it wrong is silent.** Stored segments are
  `{start, end, text}` (the Hub *receiver*'s shape); the shared-voice *route* wants
  `{start_seconds, end_seconds, text}`, sent as `transcript_segments` JSON.
  `voice_import.shared_segments` converts, and a transcript without timings is
  refused 400 by the Hub — deliberately, because the alternative is a hundred nodes
  installing a voice that then refuses to clone with it.
- **`pending` is a wait, not a failure.** A node that is switched off reports
  `pending` and the reconciler retries every 30 s; `voice_import.summarise_targets`
  says *"will install when that Mac is next on"*, because calling it an error would
  send someone to a site that was never broken.
- **The id is derived from the bytes**, `audio_sha256[:12]` — 12 lowercase hex, which
  the Hub requires and keeps, so one clip carries one id across all six controllers.
  A re-push refreshes, and an id already holding different audio is a 409.

### A reference clip's contract belongs to the model, and the Hub states it

**Read it, never derive it.** `/api/hub/catalog` publishes, per model, a
`reference_audio` block — measured on Hub 0.6.18 and **re-verified unchanged on
0.6.23**, which the Hub auto-updated itself to mid-session:

| | min | target | preferred | max | transcript |
|---|---|---|---|---|---|
| Qwen3-TTS `*-Base-*` (4 tiers) | 3 s | 8 s | 8–12 s | **15 s** | required |
| OmniVoice-bfloat16 | 3 s | — | 3–10 s | **10 s** | optional |
| VoxCPM2 (4-bit, bf16) | — | — | — | **not stated** (`profile_audited: false`) | — |
| Qwen3 `CustomVoice` / `VoiceDesign` | — | — | — | **`declared: false`** — not cloning models | — |

`providers/discovery.py` reads this alongside `/api/hub/model-packages` (readiness)
and carries `capabilities` + `reference_audio` in the model's `raw`.
`voices.reference_contract` / `reference_support` read it back. **The engines are
more permissive than the Hub**: Qwen3-TTS has no reference cap, and OmniVoice's
`_trim_long_audio` does nothing below **20 s** (measured by calling it: a 13.45 s
clip comes back untouched at any `max_duration`). So the 10 s and 15 s above are Voice
Studio's audited guardrails, and the gateway must not mirror, round or invent them —
a model that wants a 60 s reference will publish 60 and be honoured.

**A local voice belongs to whichever models clone — never to the tier it was minted
against.** Every voice the owner imported was stamped
`tts_model_id: "Qwen3-TTS-12Hz-0.6B-Base-4bit"` (`voice_import.DEFAULT_TTS_MODEL`),
and `voices_for_model` filtered local voices on an exact match with that string. The
match is now the **model's own statement**.

**Three answers, never two.** `reference_support` returns `True`, `False`, or
`None`:
- `declared`/`supported: false` → the model says it does not clone (preset
  speakers, voice design). Voices are **withheld**; offering one fails on use.
- `true` → offered.
- **absent** → *unverified*, and the voice is **still offered**. A miss must not
  read as "cannot": VoxCPM2 8-bit is installed but absent from the catalogue, and
  treating that as non-cloning would hide all ten voices again. The report says
  "limits unverified".

`voices.supports_cloning` applies the same read to the published
`supports_voice_cloning` — the bridge from the three-valued `reference_support` to
the bool Story Studio needs, with `voices.CLONING_KINDS` as the single per-kind
default. It used to be implemented **twice**, here and in `catalog._supports_cloning`, with
different fallbacks, so a row could publish `supports_voice_cloning: true` beside an
empty picker. **No currently-published row changed** when the two became one: all
eight local rows declare `true`, and fish/elevenlabs keep the per-kind default.

**A location's package list cannot tell a codec from a model, and `can_publish` now
asks the catalogue.** `/api/hub/model-packages` lists the companions a model loads
beside the models themselves — `snac_24khz`, `S3TokenizerV2`, `umt5-base`,
`fish-s1-dac-min`, `MOSS-Audio-Tokenizer-Nano` and a real model all read
`engine=voice-mlx, worker=voice, state=files_verified, runtime_state=ready`. The
Hub's catalogue omits the companions, so `can_publish` withholds a package whose
`raw["catalogued"]` is **False**, and one whose `raw["memory_eligible"]` is
**False** — the Hub's own verdict for this machine. Both are **read, and only when
stated**: an absent key still means *unverified*, never *withhold*.

**The migration reintroduced this failure.** Enabling the 34 migrated packages
published **nine bad rows**: five companions, an uncatalogued variant, and the three
models the Hub says do not fit this 16 GB Mac (`echo-tts-base` 24 GB,
`fish-audio-s2-pro` 8-bit 24 GB and bf16 32 GB); `auto_enable_discovered` sent them
out, and 27 local TTS rows became 18 with the gate. **`available: true` on
`/api/hub/models` is not a promise that a job will run** — the submission gate
refuses anything not in its packaged catalogue with `MODEL_NOT_PACKAGED` ("model is
not in the packaged catalog", `components/hub/standalone.py`), so discovery must keep
reading `model-packages` and must **never** be "fixed" to publish cache-only models.

**Those counts are a snapshot; only the gate's *behaviour* is stable.** Measured on
2026-09-20 at Hub 0.6.18 the local lane was 34 packages with 9 withheld (five
companions and `VoxCPM2-8bit` uncatalogued, three too big for this Mac); the Hub
auto-updated to **0.6.23** then **0.6.29**, and by then it was
**33 packages, 23 published, 10 withheld** — six uncatalogued and **four** the Hub
says do not fit (`echo-tts-base`, `fish-audio-s2-pro` 8-bit and bf16, and
`VoxCPM2-bf16`, not flagged before). Readings went 121 enabled / 112 offered / 9 withheld → **120 / 110 / 10**
with no gateway change. So do not read a count out of this file as a fact about the
machine: ask `/admin/api/providers/studio-hub-local/models` for the current
`publishable` rows and the `catalogued`/`memory_eligible` flags that decided them. A
number in a comment is a measurement with a date, not a contract.

**Three counts, three questions, and only one is "discovered".** `summary()`
publishes `discovered_models` (every model), `total_models` (providers switched on),
`enabled_models` (ticked) and `publishable_models` (what can reach Story Studio). The
Overview tile labels the offered number "of N enabled" because that is the set the
withheld count comes from — offered + withheld = enabled — and it *used* to label
`total_models` "discovered", which made the discovered count fall when the owner
switched a provider off. `publishable_models` and `by_modality` are **derived from
`enabled_models()`**, one function rather than a second walk with its own conditions,
which missed two gates: a provider switched on with its key cleared inflated
"offered" by rows nobody could pick.

**The header reports the offered count, not the enabled one.** `models.summary()`
publishes `enabled_models`, **`publishable_models`** and **`withheld_models`**, and
`by_modality` counts the *offered* rows. The tile used to show `enabled_models`, so
it read "121 live" while nine of those could not be picked; it now shows the offered
number and says `· N withheld` when there are any, because a model that is switched
on and silently absent from the picker is the one thing an operator cannot diagnose
from that page. Two questions, two numbers — never one field answering both.

**Nothing in the gateway truncates a reference; the Hub does the cutting.**
`studio_hub._stage_voice_reference` uploads `path.read_bytes()` verbatim, and
`reference_audio.prepare()` trims silence, then — only if the clip still exceeds the
model's maximum — keeps the longest run of **whole timed segments** that fits and
rewrites the transcript to match. B (a baked 10 s copy) was dropped in favour of
deriving at use time.

**The segments are cue-level because the installed engine emits no words.**
`voice_import._transcribe` prefers the job's own `metadata.segments` over the SRT
artifact (the artifact route serves *only* SRT — it has no `format` parameter at
all). Measured: `whisper-base-mlx` returns an empty `words` list even with
`word_timestamps: "true"`, so window selection lands on cue boundaries — a 13.45 s
clip in three cues yields an ~8.7 s window under a 10 s cap rather than a 10 s one.
Finer segments would fit closer, and `_words_to_segments`-style expansion already
handles it; the `ponytail:` names that upgrade path.

**`GET /admin/api/voices/{voice_id}/reference` answers "what happens to my
recording?"** — per enabled local model: the contract as the Hub stated it, and a
verdict (used whole / a shorter aligned window / refused / takes no reference /
limits unverified), warning when the clip is under a model's floor, a required
transcript is missing, or there are no aligned segments. The Voices window's
**Reference…** button shows it. Nothing in it is computed from a model name.
