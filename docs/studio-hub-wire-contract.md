# Studio Hub wire contract — what the adapter owes the Hub

Moved out of `AGENTS.md` when it reached the instruction budget. The Hub's own source (`theng12/Studio-Hub-KH`, `components/hub/standalone.py` and `backend/auth.py`) remains the source of truth.

The Hub (`theng12/Studio-Hub-KH`, `components/hub/standalone.py` and
`backend/auth.py` are the source of truth) is an authenticated async job service.
An inventory at GatewayKH `736d7b0` found five counterpart mismatches; all five are
now closed, and these are the parts that were expensive to work out.

- **A delivery receipt is an assertion, not a formality.** The Hub marks its output
  receipt and index protection only on a 2xx from
  `POST /api/hub/jobs/{batch}/items/{i}/ack` (subtitle:
  `/api/hub/transcription/jobs/...`), and *downloading an artifact never reaches
  that route* — that is the whole point of it. So the adapter returns a `deliver`
  callback on `BackendResult` and the **worker** invokes it after every artifact is
  on disk. A refused receipt is recorded (`payload.delivery_ack`) and logged, never
  raised: the bytes are saved and serveable, and failing the job would discard real
  work over bookkeeping. Do not move the call back into the download.
- **Cancellation must reach the Hub, and its answer is the only evidence.** A
  dispatched batch used to keep running at the location while Story Studio was told
  the job was cancelled. `_cancel_upstream` now asks the lane that owns the work
  (`DELETE /api/hub/jobs/{batch}` or the transcription twin) and stores the answer
  verbatim on the job's `cancel_remote`: `ok`/`status`/`route` plus the Hub's
  `queued_cancelled` / `running_signalled` / `running_pending`. **`cancel_state`
  keeps its own meaning** — `requested` is us asking, `confirmed` is the gateway
  stopping it before dispatch — because the Hub signalling a worker is not the same
  claim, and a refused cancel must never read as a remote stop.
- **Music is a modality with two engines and no defaults.** Discovery maps the
  Hub's `music-mlx` engine to `music`; `_MODALITIES_BY_KIND[studio_hub]` and both
  provider definitions declare it. Items are `{"prompt", "params"}` on the same
  `/api/hub/jobs` route, artifacts come back as WAV. The fleet's music is YuE2 and
  Stable Audio 3 (ACE-Step 1.5 was retired 2026-09-30, owner decision); vocal engines take
  `lyrics`, `vocal_language`, `instrumental`, `shift`; stabilityai/stable-audio-3-optimized
  is instrumental only. The adapter forwards
  whichever parameters the caller set and **invents none** — which engine accepts
  what is the engine's own answer, and a guessed default would be silently wrong.
- **A staged voice reference carries its metadata, and the answer is checked.** The
  Hub's `POST /api/hub/voice-references` accepts `language`, a declared `sha256`, a
  durable `source_asset_id` and `ttl_seconds`, and returns the digest, byte count,
  media type, expiry, transcript flag and origin it stored. `language` is part of
  the Hub's **asset identity**, so omitting it made different voices' references
  hash alike; the declared checksum is what stops a mangled upload becoming the clip
  a voice is cloned from. The digest must match; the other fields are checked only
  when stated (absence is *unverified*, never *wrong*).
- **The controller may now discard a hold — explicitly, never automatically.** From
  Hub `639fcf9` the fleet credential may call
  `POST /api/hub/jobs/{batch}/items/{i}/discard` (owner decision 2026-09-22), which
  only accepts an item already `uncertain` (else 400), clears the worker/recovery
  identity, marks it cancelled with `DISCARDED_UNCERTAIN` and frees the node's
  machine. The gateway exposes that as the admin action
  `POST /admin/api/jobs/{job_id}/discard-uncertain` — the **only** caller of the
  discard path. The Hub already discards after thirty minutes of silence as the
  walk-away backstop, so the gateway must not add a timer: abandoning real work is a
  human decision. The action refuses a job that is not a hold (`NO_RECOVERY_HOLD`,
  because discarding live work would abandon a worker that may still be running), and
  a refusal keeps the hold and is recorded on `discard_remote`. There is **no
  transcription discard route** on the Hub, so that case is reported plainly.
- **`uncertain` is a recovery hold, not a failure.** A Hub item that settles
  `uncertain` after dispatch has an *unknown* worker outcome: the work may have
  completed, so reporting an ordinary failure invites a retry that generates twice.
  The adapter raises `UNKNOWN_OUTCOME` with `upstream_accepted=True` and a context
  dict, so the job settles `recovery_state: unknown_outcome`, `recovery_required`,
  non-retryable and never due again, with the batch, item, worker, machine, the
  Hub's error code and its reconcile/discard routes in `recovery_detail`. Nothing is
  reconciled or discarded automatically: the hub keeps both **operator-initiated**,
  and from `639fcf9` the discard is reachable by a controller's fleet credential
  (above) as well as by the node's own operator.
- **`output_format` asks a Hub lane to compress at the source — optional, validated, never a failure.**
  Image `png|jpeg`, music `wav|m4a`, voice `wav|m4a` (JPEG q90 and AAC are one fixed preset each in the Hub;
  there is no quality knob). `POST /v1/images/generations`, `/v1/music/generations` and `/v1/audio/jobs`
  answer **400 `INVALID_PARAMS`** for any other value, before anything is queued or charged. Absent (or `null`)
  is the request every caller sent before the field existed, byte for byte; a node that predates it drops the
  field and returns its native WAV/PNG, which is recorded (`payload.output_format_requested` /
  `output_format_applied`, the latter read from the delivered media type) and is **not** an error. The adapter
  stores whatever media type the Hub states, so `audio/mp4` lands as `{job}.m4a` and `audio/flac` as `.flac`
  (`worker._EXT_BY_MEDIA`) rather than `.bin`. Cloud providers never see the field (`worker._params_for`):
  ElevenLabs would copy `m4a` into its own `?output_format=` query and refuse a paid job. Hub rows publish
  `output_formats` when the lane declares an `output_format` parameter (a hint only). **Idempotency is by key
  alone**, so a caller that changes the format for an otherwise identical request must change the key (Story
  folds the format into it only when set).
