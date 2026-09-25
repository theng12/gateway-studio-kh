"""Studio Hub adapter — the local Apple Silicon lane.

Routes image, voice and transcription work to the Hub on this Mac.

Two things about the Hub shape this code:

  * It is genuinely asynchronous (batch id + item index + poll + artifact), so
    this adapter polls internally until the item is terminal. That is why the
    gateway executes jobs on a worker thread rather than inside the HTTP request
    -- a five-minute image job must not hold a Story Studio poll open.
  * Transcription uses a *different* route (`/api/hub/transcription/jobs`,
    multipart) from image/voice (`/api/hub/jobs`, JSON).

Errors are normalised onto the same vocabulary as the remote adapters so Story
Studio's retry logic behaves identically regardless of where work ran.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from .base import BackendError, BackendRequest, BackendResult

POLL_INTERVAL_S = 1.0
# Aligned with the Hub's own claim TTL floor (request timeout 300s + margin).
JOB_DEADLINE_S = 900.0

#: The Studio Hub release this gateway is **built and verified against**.
#:
#: Studio Hub moves on its own schedule — it auto-updates itself, and this fleet
#: has run 0.6.18, 0.6.23, 0.6.29, 0.6.31, 0.6.54, 0.6.60, 0.6.63, 0.6.64 and
#: 0.6.65 — while this gateway moves on its own too. So the pairing is *declared*
#: rather than assumed: this string is the version whose wire contract the code in
#: this file was written and measured against, and it is the number the kit and
#: the release notes state.
#:
#: **Measured, not assumed.** 0.6.65 was set on 2026-09-25 from a running Hub
#: (`/api/version` → `0.6.65`, build `37cb07b`), not from a changelog: the chat
#: lane's `lane_contract` was read live and carries `structured_output`
#: (`formats: [json_object, json_schema]`, `enforcement: prompt_validate_repair`,
#: `grammar_constrained: false`) beside the keys this file already read, and
#: `/api/health` publishes the `bind` block with `loopback_only` and
#: `reachable_off_this_mac`. Both are read verbatim where this code reads them, and
#: nothing here branches on the number. It was deliberately **not** bumped to
#: 0.6.64 when that build was only *held* by the controllers and running nowhere —
#: "measured against" is a claim, and a version nobody had run could not support it.
#:
#: **It is never a gate.** A controller on another version keeps working: the
#: adapter reads what the Hub states instead of branching on a version number, so
#: a newer Hub offering something new is a thing to *report*, not a thing to
#: refuse. Refusing would turn Studio Hub's own auto-update into an outage on a
#: fleet nobody touched (owner's decision, 2026-09-24: report drift, keep
#: working). `hub_version_relation()` says how a controller's version compares,
#: and the Fleet page shows it beside the Hub version.
SUPPORTED_STUDIO_HUB_VERSION = "0.6.65"

#: The answers `hub_version_relation` can give. `match` is the tested pairing; the
#: other two are informational — a newer Hub may serve things this gateway has no
#: lane for, and an older one may be missing a route this code knows how to read.
HUB_VERSION_MATCH = "match"
HUB_VERSION_OLDER = "older"
HUB_VERSION_NEWER = "newer"


def _version_tuple(value: object) -> tuple[int, ...] | None:
    """A dotted version as comparable integers, or `None` if it is not one.

    Deliberately forgiving about a trailing pre-release or build suffix
    (`0.6.60`, `0.6.60-rc1`, `0.6.60+build`) and deliberately `None` rather than a
    guess for anything else: "I could not read the version" must never render as
    "the version matched".
    """
    text = str(value or "").strip()
    if not text:
        return None
    head = text.split("+", 1)[0].split("-", 1)[0]
    numbers: list[int] = []
    for part in head.split("."):
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            return None
        numbers.append(int(digits))
    return tuple(numbers) if numbers else None


def hub_version_relation(version: object) -> str | None:
    """How a controller's Hub version compares to the one this build targets.

    `None` when the Hub stated no version, or stated one that is not a dotted
    number — absence stays absence. A shorter version is padded (`0.6` reads as
    `0.6.0`), so `0.6` is older than `0.6.60` rather than unreadable.
    """
    stated = _version_tuple(version)
    if stated is None:
        return None
    supported = _version_tuple(SUPPORTED_STUDIO_HUB_VERSION) or ()
    width = max(len(stated), len(supported))
    left = stated + (0,) * (width - len(stated))
    right = supported + (0,) * (width - len(supported))
    if left == right:
        return HUB_VERSION_MATCH
    return HUB_VERSION_OLDER if left < right else HUB_VERSION_NEWER


# Hub item states -> our terminal interpretation.
_TERMINAL_OK = {"done", "succeeded", "complete", "completed"}
_TERMINAL_BAD = {"error", "failed", "cancelled", "cancelled_items"}
#: The Hub settled an item **after dispatch** without knowing the worker's
#: outcome. That is a recovery hold, never an ordinary failure: the work may have
#: completed, so treating it as failed invites a retry that generates it twice,
#: and treating it as failed also hides the one thing an operator can act on.
#: Kept out of `_TERMINAL_BAD` so the two cannot be collapsed again.
_TERMINAL_UNCERTAIN = {"uncertain"}

#: Hub submit refusals that mean "this controller cannot run that model", not
#: "the job is broken". The Hub itself said so with a 409; the worker may try
#: another location that offers the same model. `MODEL_MODALITY_MISMATCH` is
#: deliberately not here — that is a routing bug, not a site property.
#:
#: **`MACHINE_BUSY` belongs here and did not used to be.** Measured 2026-09-25:
#: **twenty-seven** chat jobs settled `failed` on
#: `409 {"detail":"local is reserved for other work on this Mac","code":"MACHINE_BUSY"}`.
#: A busy machine is the plainest site property there is — it is true for a minute
#: and false the next — and it became common rather than rare the moment the worker
#: started dispatching to a pool, because we now ask a site for more than one job at
#: a time. Failing the caller's job for it is the same mistake the 409 in agent mode
#: used to make, in the other direction.
_LOCATION_REFUSAL_CODES = {"MODEL_NOT_READY", "MODEL_NOT_PACKAGED", "MACHINE_BUSY"}


def _hub_refusal_code(response: httpx.Response) -> str | None:
    """The Hub's own `code` on a refusal, when it carries one."""
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return str(code) if code else None


def _uncertain_result(batch_id: str, item_index: int, item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The operator-facing description of an item the Hub cannot vouch for.

    Built from what the Hub states — its own error code, the worker and machine
    that took the work, and its reason — plus the two routes that resolve it.
    Nothing here is inferred: an absent field stays absent.
    """
    recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
    reason = str(
        item.get("error") or recovery.get("reason") or "the worker outcome is not known"
    )
    context: dict[str, Any] = {
        "hub_batch_id": batch_id,
        "hub_item_index": item_index,
        "hub_error_code": item.get("error_code"),
        "hub_worker": item.get("worker_id"),
        "hub_machine": item.get("machine"),
        "hub_reason": reason,
        "hub_phase": recovery.get("phase"),
        # Operator actions. The Hub owns them; the gateway never discards or
        # reconciles on its own, because both are decisions about real work.
        "hub_reconcile_route": f"/api/hub/jobs/{batch_id}/items/{item_index}/reconcile",
        "hub_discard_route": f"/api/hub/jobs/{batch_id}/items/{item_index}/discard",
    }
    detail = (
        f"Studio Hub settled batch {batch_id} item {item_index} after dispatch without "
        f"knowing the worker's outcome: {reason} Reconcile it on the Hub "
        f"({context['hub_reconcile_route']}) or discard it "
        f"({context['hub_discard_route']}); the gateway will not dispatch it again."
    )
    return detail, context

_HUB_MODALITY = {
    "image": "image",
    "tts": "voice",
    "music": "music",
}

#: What the Hub's Music item accepts, per its two engines.
#:
#: ACE-Step/Ace-Step1.5 sings and does long form and takes `lyrics`,
#: `vocal_language`, `instrumental` and `shift`; stabilityai/stable-audio-3-optimized
#: is instrumental only and takes the rest. The gateway forwards whichever of these
#: the caller set and invents none of them — which engine accepts what is the
#: engine's own answer, and a Hub refusal names the field far better than a
#: default guessed here.
_HUB_MUSIC_PARAMS = (
    "duration_seconds",
    "steps",
    "guidance_scale",
    "shift",
    "seed",
    "instrumental",
    "lyrics",
    "vocal_language",
)


def _resolve_base(request: BackendRequest) -> str:
    from ..hub import hub_base_url

    base = (request.values.get("base_url") or request.base_url or hub_base_url()).strip()
    if not base:
        raise BackendError(
            "Studio Hub is not running (no hub-ready.json found)",
            code="SITE_CAPACITY_UNAVAILABLE",
            retryable=True,
        )
    return base.rstrip("/")


def _headers(request: BackendRequest) -> dict[str, str]:
    from ..fleet import LOCAL_ID, provider_id_for
    from ..hub import hub_headers

    # `X-Hub-Token` carries the *called provider's* credential. Only this Mac's
    # own Hub has none stored — its operator token is its credential — so only
    # there is the local fallback correct. A remote location's absent credential
    # must stay absent: `hub_headers` used to fall back unconditionally, which
    # put this Mac's operator token in this header and sent it to the address the
    # owner had typed into the Fleet page.
    return hub_headers(
        request.values.get("hub_token", ""),
        local=request.provider_id == provider_id_for(LOCAL_ID),
    )


def lane_deadline_seconds(request: BackendRequest | None) -> float:
    """How long to poll this job, as the **lane that enforces it** states it.

    Hub 0.6.61 puts `deadline_seconds` inside each model's `lane_contract` — its
    own peer-proxy timeout, "one number, owned by the party that enforces it".
    Reading it is what stops this gateway from cutting a lane short at its own flat
    `JOB_DEADLINE_S` after the Hub has raised its ceiling, which would fail a job
    that was about to succeed.

    Falls back to `JOB_DEADLINE_S` whenever nothing states one: an older Hub, a
    lane with no contract, or a job whose model is not in the cache. The read is
    from the cached discovery row, so it costs no network time on the poll path.
    """
    if request is None:
        return JOB_DEADLINE_S
    try:
        from ..providers import registry  # local import: avoids a cycle

        for model in registry.models.models_for(request.provider_id, include_disabled=True):
            if str(model.get("model_id") or "") != str(request.model_id or ""):
                continue
            raw = model.get("raw") or {}
            contract = raw.get("lane_contract")
            if not isinstance(contract, dict):
                return JOB_DEADLINE_S
            stated = contract.get("deadline_seconds")
            if isinstance(stated, bool) or not isinstance(stated, (int, float)):
                return JOB_DEADLINE_S
            # A stated deadline of zero or less is not a deadline; keep ours rather
            # than failing every poll instantly on a nonsensical number.
            return float(stated) if float(stated) > 0 else JOB_DEADLINE_S
    except Exception:  # noqa: BLE001 - an unreadable contract is "unstated"
        return JOB_DEADLINE_S
    return JOB_DEADLINE_S


def _poll_batch(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    jobs_path: str = "/api/hub/jobs",
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """Poll a Hub batch to a terminal state.

    `jobs_path` differs by modality: image and voice share `/api/hub/jobs`,
    while transcription batches live under `/api/hub/transcription/jobs` and
    are *not* visible from the other route -- polling the wrong one returns 404
    rather than a useful error.

    `deadline_seconds` is the lane's own ceiling when it published one (see
    `lane_deadline_seconds`), and this gateway's flat fallback otherwise.
    """
    limit = float(deadline_seconds) if deadline_seconds else JOB_DEADLINE_S
    deadline = time.monotonic() + limit
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{base}{jobs_path}/{batch_id}", headers=headers)
        if response.status_code >= 400:
            raise BackendError(
                f"hub status HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )
        summary = response.json() or {}
        last = summary

        items = summary.get("items") or []
        item = items[item_index] if item_index < len(items) else None
        state = str((item or {}).get("state") or summary.get("state") or "").lower()

        if state in _TERMINAL_OK:
            return {"summary": summary, "item": item or {}}
        if state in _TERMINAL_UNCERTAIN:
            # `upstream_accepted=True` is what makes the worker record this as a
            # recovery hold rather than a retryable failure, and the code is the
            # one the store already uses for "do not resubmit this".
            detail, context = _uncertain_result(batch_id, item_index, item or {})
            raise BackendError(
                detail,
                code="UNKNOWN_OUTCOME",
                upstream_accepted=True,
                recovery=context,
            )
        if state in _TERMINAL_BAD:
            error = (item or {}).get("error") or summary.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise BackendError(
                f"hub item {state}: {message or 'no detail'}",
                code="IMAGE_FAILED" if item is None else "PROVIDER_FAILED",
            )
        time.sleep(POLL_INTERVAL_S)

    raise BackendError(
        f"hub job did not finish within {int(limit)}s",
        code="PROVIDER_SUBMISSION_UNCERTAIN",
        retryable=True,
    )


def _jobs_path(kind: str) -> str:
    """Where a modality's batches live.

    Image, voice and music share `/api/hub/jobs`; transcription has its own route
    and its batches are *not* visible from the other one — polling, fetching an
    artifact or cancelling on the wrong path answers 404 rather than a useful
    error. Every route for a modality is built from this one answer so they cannot
    disagree.
    """
    return "/api/hub/transcription/jobs" if kind == "transcription" else "/api/hub/jobs"


def _artifact_url(base: str, batch_id: str, item_index: int, kind: str) -> str:
    return f"{base}{_jobs_path(kind)}/{batch_id}/items/{item_index}/artifact"


def _recorded_call(
    method: str, url: str, headers: dict[str, str], route: str
) -> dict[str, Any]:
    """One authenticated Hub call whose answer is recorded and never raised.

    A delivery receipt, a cancellation and a discard are all "tell the Hub
    something, then report what it said". None of them may fail the job: each is a
    recording, and the caller decides what its answer means. Kept in one place so
    no one of them quietly grows a retry, a raise or a different timeout.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.request(method, url, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "route": route,
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        answer: Any = response.json()
    except ValueError:
        answer = response.text[:200]
    return {
        "ok": 200 <= response.status_code < 300,
        "status": response.status_code,
        "route": route,
        "answer": answer,
    }


def _ack_delivery(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    kind: str,
) -> dict[str, Any]:
    """Assert to the Hub that one delivered artifact was verified received.

    The Hub marks its output receipt and index protection only after a 2xx from
    this route, and the route is deliberately *not* reachable by downloading an
    artifact: it is the caller's assertion that it has the bytes. That is why the
    adapter hands this to the worker as a callback instead of calling it here —
    the bytes are only really delivered once they are on disk.
    """
    route = f"{_jobs_path(kind)}/{batch_id}/items/{item_index}/ack"
    return _recorded_call("POST", f"{base}{route}", headers, route)


def _cancel_batch(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    kind: str,
) -> dict[str, Any]:
    """Ask the Hub to stop a dispatched batch, and report what it answered.

    The Hub's own answer is the only evidence there is: it cancels what is still
    queued, *signals* what is already running, and cannot promise a worker that
    has accepted the work will stop. So the fields are recorded verbatim —
    `queued_cancelled`, `running_signalled`, `running_pending` — rather than
    flattened into a boolean, because "we asked and nothing was running to stop"
    and "we asked and a worker was told" are different facts.
    """
    route = f"{_jobs_path(kind)}/{batch_id}"
    return _recorded_call("DELETE", f"{base}{route}", headers, route)


def _retry_item(
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    kind: str,
) -> dict[str, Any]:
    """Ask the Hub to re-queue one failed item, and report what it answered.

    New in Hub 0.6.48. **Only an item in state `error` is accepted**: the Hub
    answers 400 for a queued/running/done/cancelled item, for a cancelled batch,
    and for an `uncertain` item — which must be discarded before it can be retried,
    and the Hub offers discard only on the jobs path. The answer is recorded, never
    raised, like every other operator call; the caller reads `ok`/`status`.
    """
    route = f"{_jobs_path(kind)}/{batch_id}/items/{item_index}/retry"
    return _recorded_call("POST", f"{base}{route}", headers, route)


# Voice placement, keyed by **(voice, controller)**.
#:
#: The value is `(kind, id, at)`: either the controller's own library voice or an
#: asset staged for one attempt. Keyed by voice alone — which it was — a reference
#: staged on one location was handed to the next location that asked for the same
#: voice, and that location had never seen the asset. Invisible with a single
#: location, which is why it survived; with several it fails the moment a second
#: controller reuses a cached voice.
_REFERENCE_CACHE: dict[tuple[str, str, str], tuple[str, str, float]] = {}
_REFERENCE_TTL_S = 6 * 3600.0


def _remember_reference(key: tuple[str, str, str], kind: str, value: str) -> None:
    """Store a placement and drop the ones that have expired.

    The key carries the clip's digest, so every re-import of a voice adds a key
    whose predecessors are never asked for again. Without this they would sit in
    the dict for the process's life.

    `ponytail:` a full sweep per write, which is a handful of entries per voice per
    location. Above a few thousand voices this wants a heap or an ordered dict.
    """
    now = time.time()
    for stale in [k for k, v in _REFERENCE_CACHE.items() if (now - v[2]) >= _REFERENCE_TTL_S]:
        _REFERENCE_CACHE.pop(stale, None)
    _REFERENCE_CACHE[key] = (kind, value, now)


def _reference_cache_key(voice_id: str, base: str, digest: str) -> tuple[str, str, str]:
    """Cache identity for a voice placement: the voice, the location, **and the bytes**.

    The digest is the third part because a re-import keeps the voice's id and
    changes its audio. Keyed on `(voice_id, base)` — which is what this was — the
    controller's **old recording** kept being used for up to six hours, and nothing
    said so: the placement asked for that voice and got a confident answer about a
    clip that no longer exists. It is the stale build stamp again, one layer down.

    The digest is already the thing the library match is made on, so a placement
    cached for one recording must not answer for another.
    """
    return (voice_id, base.rstrip("/"), digest)


def _clip_digest(path: Path) -> str:
    """The sha256 of a clip's bytes.

    The same notion as `voice_import.clip_digest` and `shared_voice_id`, and
    deliberately not imported from there: an adapter depends on its definition and
    its base, not on the voice domain. Three one-line readers of the same fact is
    the price of that direction; what must not diverge is the *definition*, and
    "sha256 of the file, lowercase hex" is not a thing that can be interpreted
    two ways.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _library_voice_for(
    client: httpx.Client, base: str, headers: dict[str, str], digest: str
) -> str:
    """The controller's own voice id for a clip it already holds, or "".

    The library carries `audio_sha256`, which the Studio Hub session verified is
    the sha256 of the stored reference file byte-for-byte. Matching on it is exact,
    and it is also the only safe match: the gateway names this recording
    `voice-0616aec33b41` where the controller names it `0616aec33b41`, so matching
    on ids would be a translation this side would have to guess. The hash is a
    fact.

    Never raises. A controller that does not serve this route, or whose credential
    may not read it, falls back to staging the clip — which is what happened
    before this existed, so the failure mode is exactly the old behaviour.
    """
    try:
        response = client.get(f"{base}/studio/voice/api/voices", headers=headers)
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    try:
        voices = (response.json() or {}).get("voices") or []
    except ValueError:
        return ""
    for voice in voices:
        if str(voice.get("audio_sha256") or "") == digest:
            return str(voice.get("id") or "")
    return ""


def _voice_placement(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    reference: dict[str, Any],
) -> tuple[str, str]:
    """Where the controller should take this voice from: `("library"|"asset", id)`.

    A library voice is one the controller already holds; an asset is a clip staged
    for this one attempt. They are **mutually exclusive** in the Hub's job payload
    — its broker refuses both with "Use either voice_library_id or
    voice_reference_asset_id, not both" — so exactly one is returned.

    The library wins whenever the controller already has the recording. Skipping
    the upload there is not only a saving: the controller's own entry is the
    durable form of the voice, while a staged asset is a copy made to serve one
    job. On this Mac all ten reference clips already match, so every one of those
    uploads was avoidable.
    """
    path = Path(str(reference.get("path") or ""))
    if not path.exists():
        raise BackendError(
            f"voice reference audio is missing for '{voice_id}'",
            code="VOICE_PROVIDER_RESULT_INVALID",
        )
    # Read before the cache, not after it: which recording this is *is* part of the
    # question the cache answers. Hashing a reference clip per job is a few
    # hundred kilobytes of I/O, and the alternative is answering about the wrong
    # audio for six hours.
    digest = _clip_digest(path)

    key = _reference_cache_key(voice_id, base, digest)
    cached = _REFERENCE_CACHE.get(key)
    if cached and (time.time() - cached[2]) < _REFERENCE_TTL_S:
        return cached[0], cached[1]

    node_voice_id = _library_voice_for(client, base, headers, digest)
    if node_voice_id:
        _remember_reference(key, "library", node_voice_id)
        return "library", node_voice_id

    asset_id = _stage_voice_reference(client, base, headers, voice_id, path, reference)
    _remember_reference(key, "asset", asset_id)
    return "asset", asset_id


def _reference_for(request: BackendRequest) -> dict[str, Any] | None:
    """The clone reference for a voice, looked up in the voice registry.

    Story Studio sends back the compound id it was given, so the registry is the
    single place that knows how to turn that into a local clip.
    """
    voice_id = str(request.params.get("voice_id") or "")
    if not voice_id:
        return None
    from ..voices import resolve  # local import: avoids a cycle

    voice = resolve(voice_id)
    if voice is None:
        return None
    raw = voice.raw or {}
    path = str(raw.get("reference_path") or "")
    if not path:
        return None
    return {
        "path": path,
        "transcript": str(raw.get("reference_transcript") or ""),
        "segments": raw.get("reference_segments") or [],
        "name": voice.display_name,
        # Sent so the Hub's staged copy carries the same facts this gateway knows.
        # `language` is part of the Hub's asset identity, so leaving it out made
        # two different voices' references hash alike. Nothing stated stays empty
        # rather than becoming a guessed "en".
        "language": str(getattr(voice, "language", "") or ""),
        # The durable source this execution copy is made from. The gateway *is*
        # the GenStudio in this architecture — it owns the clip and Story Studio
        # talks to it — so its own voice id is the origin identity, and the Hub
        # fingerprints it and reports origin "genstudio".
        "source_id": str(getattr(voice, "id", "") or voice_id),
    }


def _verify_staged_reference(
    payload: dict[str, Any], *, digest: str, size: int, transcript: bool
) -> None:
    """Check the Hub staged *these* bytes, from this origin, still valid.

    An `asset_id` says only that something was stored. The digest is the part that
    matters: a reference whose bytes differ from the clip is a clone of the wrong
    voice, and the Hub reports the digest it kept, so the two can be compared
    instead of assumed. It is therefore **required** — an unconfirmable digest is
    the wrong-voice risk this exists to catch.

    Everything else is checked only **when stated**, the same rule the rest of the
    gateway follows: a Hub that omits a byte count or an origin has not said
    something different, it has said less, and reading that as a mismatch would
    refuse a reference for a field nobody disagreed about. An expiry that *is*
    stated must still be in the future, and a stated media type must still be
    audio.
    """
    problems: list[str] = []
    stored = str(payload.get("sha256") or "").lower()
    if stored != digest:
        problems.append(f"digest {stored or '(none)'} is not the clip's {digest}")
    if payload.get("bytes") not in (None, size):
        problems.append(f"byte count {payload.get('bytes')} is not the clip's {size}")
    media_type = str(payload.get("media_type") or "")
    if media_type and not media_type.startswith("audio/"):
        problems.append(f"media type {media_type!r} is not audio")
    expires_at = payload.get("expires_at")
    if expires_at is not None and (
        not isinstance(expires_at, (int, float)) or expires_at <= time.time()
    ):
        problems.append(f"expiry {expires_at!r} is not in the future")
    if payload.get("transcript_supplied") is not None and bool(
        payload["transcript_supplied"]
    ) != transcript:
        problems.append("transcript coverage differs from what was sent")
    origin = payload.get("origin")
    if origin is not None and origin != "genstudio":
        problems.append(f"origin {origin!r} is not the source that was sent")
    if problems:
        raise BackendError(
            "the hub accepted the reference but described it differently: "
            + "; ".join(problems),
            code="VOICE_PROVIDER_RESULT_INVALID",
        )


def _stage_voice_reference(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    path: Path,
    reference: dict[str, Any],
) -> str:
    """Upload a voice's reference clip and return the Hub's asset id.

    Qwen3 *Base* models are cloning models: without a reference they fail with
    "Voice cloning needs a reference voice or private reference upload". So the
    clip is staged first and passed as `voice_reference_asset_id`.

    This is the **fallback** now: `_voice_placement` reaches it only when the
    controller does not already hold the recording.

    Everything the route accepts is sent, and the answer is checked. The clip's
    language, its declared checksum and the durable source it came from are all
    part of what the Hub stores and fingerprints: sending only the audio left the
    staged copy a poorer description of the voice than the gateway already had.
    """
    audio = path.read_bytes()
    # One read feeds both the upload and the declared digest: hashing the file a
    # second time would let the two describe different bytes.
    digest = hashlib.sha256(audio).hexdigest()
    files = {"audio": (path.name, audio, "application/octet-stream")}
    # `transcript_segments` is not optional for Qwen3 Base: the worker refuses a
    # reference without aligned timestamps
    # (REFERENCE_TRANSCRIPT_ALIGNMENT_REQUIRED). They are generated by
    # scripts/annotate_voice_segments.py.
    segments = reference.get("segments") or []
    transcript = str(reference.get("transcript") or "")
    data = {
        "transcript": transcript,
        "transcript_segments": json.dumps(segments) if segments else "",
        "language": str(reference.get("language") or ""),
        # Declared integrity: the Hub refuses an upload whose bytes do not match
        # this, so a mangled copy fails at the door instead of quietly becoming
        # the reference a voice is cloned from.
        "sha256": digest,
        "source_asset_id": str(reference.get("source_id") or voice_id)[:500],
        # At least as long as this gateway will reuse the placement, so an asset
        # cannot expire while the cache still believes it is staged.
        "ttl_seconds": str(int(_REFERENCE_TTL_S)),
    }
    try:
        response = client.post(
            f"{base}/api/hub/voice-references", headers=headers, files=files, data=data
        )
    except httpx.HTTPError as exc:
        raise BackendError(
            f"voice reference upload failed: {exc}",
            code="SITE_CAPACITY_UNAVAILABLE",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        raise BackendError(
            f"hub voice-reference HTTP {response.status_code}: {response.text[:200]}",
            code="VOICE_PROVIDER_RESULT_INVALID",
            status=response.status_code,
            retryable=response.status_code >= 500,
        )

    payload = response.json() or {}
    asset_id = str(payload.get("asset_id") or "")
    if not asset_id:
        raise BackendError(
            "hub returned no voice reference asset_id", code="VOICE_PROVIDER_RESULT_INVALID"
        )
    _verify_staged_reference(
        payload,
        digest=digest,
        size=len(audio),
        transcript=bool(transcript.strip() or segments),
    )

    # Caching happens in `_voice_placement`, which is the only caller: it caches
    # the *placement* (library or asset), not the asset id alone.
    return asset_id


def _generate_image_or_voice(request: BackendRequest) -> BackendResult:
    base = _resolve_base(request)
    headers = _headers(request)
    modality = _HUB_MODALITY[request.modality]

    # A caller-supplied `params` is passed through to the Hub, but it has to be an
    # object: `request.params.get("params")` could be a string or a list, and the
    # very next line assigns into it, so a malformed field became a `TypeError`
    # that settled the job as an internal-looking failure instead of saying what
    # was wrong.
    supplied = request.params.get("params")
    if supplied is not None and not isinstance(supplied, dict):
        raise BackendError(
            "'params' must be an object of Hub parameters.", code="INVALID_PARAMS"
        )
    item: dict[str, Any] = {"params": dict(supplied or {})}
    if request.modality in {"image", "music"}:
        # Both address the worker by `prompt`; Music then takes its own engine
        # parameters and no image geometry.
        item["prompt"] = str(request.params.get("prompt") or "")
        keys = (
            ("seed", "width", "height", "aspect_ratio", "resolution")
            if request.modality == "image"
            else _HUB_MUSIC_PARAMS
        )
        for key in keys:
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]
    else:
        item["text"] = str(request.params.get("text") or "")
        for key in ("speed", "voice_id", "language", "controls", "temperature", "seed"):
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]

    body = {
        "modality": modality,
        "model": request.model_id,
        "items": [item],
        "clientRequestId": str(request.params.get("_job_id") or ""),
        "label": "gateway-kh",
    }

    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        if request.modality == "tts":
            voice_id = str(request.params.get("voice_id") or "")
            reference = _reference_for(request)
            if reference is not None:
                kind, value = _voice_placement(client, base, headers, voice_id, reference)
                # Exactly one of these, never both: the Hub's broker refuses the
                # pair with "Use either voice_library_id or
                # voice_reference_asset_id, not both."
                body["voice_library_id" if kind == "library" else "voice_reference_asset_id"] = value
        try:
            response = client.post(f"{base}/api/hub/jobs", headers=headers, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # **Nothing left this Mac.** A connection that was never established
            # cannot have delivered the request, so this is a site that is down —
            # safe to re-route, and never an "unknown outcome" hold. Measured live:
            # a controller that was switched off turned every submit into a durable
            # unknown-outcome hold, which is a recovery problem the caller never
            # created. A read or write timeout is different: the request may have
            # arrived, so it keeps the ambiguous path below.
            raise BackendError(
                f"could not reach the Hub at {base} ({type(exc).__name__})",
                code="LOCATION_UNREACHABLE",
                retryable=True,
                upstream_accepted=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"hub submit failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True
            ) from exc

        if response.status_code >= 400:
            code = _hub_refusal_code(response)
            if code in _LOCATION_REFUSAL_CODES:
                # A site property, not a job failure: the controller cannot serve
                # this model right now, and it accepted nothing. `upstream_accepted`
                # is therefore False, so the worker may safely try the next
                # location that offers the same model instead of calling the job
                # failed (or, worse, holding it as an unknown outcome).
                raise BackendError(
                    f"the location cannot serve this model right now ({code})",
                    code="LOCATION_NOT_READY",
                    status=response.status_code,
                    retryable=True,
                    upstream_accepted=False,
                )
            raise BackendError(
                f"hub submit HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
                # **A 4xx is the Hub's own decision**, so it accepted nothing and
                # there is no unknown outcome to reconcile. Measured live: a 409
                # "This Hub is in agent mode; submit customer jobs to a controller"
                # became a durable `unknown_outcome` hold — reconciliation work the
                # caller never created. Only a 5xx leaves room for "it may have been
                # accepted before it failed".
                upstream_accepted=None if response.status_code >= 500 else False,
            )

        submitted = response.json() or {}
        batch_id = str(submitted.get("batch_id") or "")
        if not batch_id:
            raise BackendError("hub returned no batch_id", code="PROVIDER_FAILED")
        if request.checkpoint is not None:
            request.checkpoint(batch_id)

        outcome = _poll_batch(
            client, base, headers, batch_id, 0,
            deadline_seconds=lane_deadline_seconds(request),
        )

        artifact = client.get(
            _artifact_url(base, batch_id, 0, request.modality),
            headers=headers,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        if artifact.status_code >= 400:
            raise BackendError(
                f"hub artifact HTTP {artifact.status_code}",
                code="PROVIDER_FAILED",
                status=artifact.status_code,
                retryable=artifact.status_code >= 500,
            )

    media_type = artifact.headers.get("content-type", "").split(";")[0].strip()
    if not media_type:
        # Music comes back as WAV like voice, so only image differs.
        media_type = "image/png" if request.modality == "image" else "audio/wav"

    terminal = (outcome.get("item") or {}).get("terminal_result") or {}
    payload: dict[str, Any] = {
        "hub_batch_id": batch_id,
        "hub_machine": terminal.get("machine_id"),
        "hub_worker": terminal.get("worker_id"),
        "resolved_seed": terminal.get("resolved_seed"),
        "sha256": terminal.get("sha256"),
        "bytes": terminal.get("bytes"),
    }

    def deliver() -> dict[str, Any]:
        # Mutating the payload the result already carries is how the receipt
        # reaches the job record: the worker calls this before `store.succeed`,
        # so the answer it records is the one saved with the job.
        payload["delivery_ack"] = _ack_delivery(
            base, headers, batch_id, 0, request.modality
        )
        return payload["delivery_ack"]

    return BackendResult(
        content=artifact.content,
        media_type=media_type,
        payload=payload,
        deliver=deliver,
    )


def _generate_transcription(request: BackendRequest) -> BackendResult:
    base = _resolve_base(request)
    headers = _headers(request)

    audio_path = request.params.get("_upload_path")
    if not audio_path or not Path(audio_path).exists():
        raise BackendError("transcription needs an uploaded audio file", code="PROVIDER_FAILED")
    audio = Path(audio_path).read_bytes()

    # Field names are `files` / `item_ids`, NOT `files[]` / `item_ids[]`. The
    # Hub declares `files: list[UploadFile]` and `item_ids: list[str]` as
    # repeated form fields, so a bracketed name is rejected with a bare 422.
    files = {
        "files": (
            request.params.get("_upload_name") or "audio.mp3",
            audio,
            request.params.get("_upload_type") or "application/octet-stream",
        )
    }
    data: dict[str, str] = {"model": request.model_id, "item_ids": "0"}
    if request.params.get("language"):
        data["language"] = str(request.params["language"])

    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        try:
            response = client.post(
                f"{base}/api/hub/transcription/jobs", headers=headers, files=files, data=data
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Same rule as the shared jobs route: a connection that was never
            # established delivered nothing, so this is a site that is down rather
            # than an unknown outcome.
            raise BackendError(
                f"could not reach the Hub at {base} ({type(exc).__name__})",
                code="LOCATION_UNREACHABLE",
                retryable=True,
                upstream_accepted=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise BackendError(
                f"hub transcription submit failed: {exc}",
                code="SITE_CAPACITY_UNAVAILABLE",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            code = _hub_refusal_code(response)
            if code in _LOCATION_REFUSAL_CODES:
                raise BackendError(
                    f"the location cannot serve this model right now ({code})",
                    code="LOCATION_NOT_READY",
                    status=response.status_code,
                    retryable=True,
                    upstream_accepted=False,
                )
            raise BackendError(
                f"hub transcription HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
                # Same rule as the shared jobs route: a 4xx is the Hub deciding,
                # so nothing was accepted and there is no unknown outcome.
                upstream_accepted=None if response.status_code >= 500 else False,
            )

        submitted = response.json() or {}
        batch_id = str(submitted.get("batch_id") or "")
        if not batch_id:
            raise BackendError("hub returned no batch_id", code="PROVIDER_FAILED")
        if request.checkpoint is not None:
            request.checkpoint(batch_id)

        outcome = _poll_batch(
            client, base, headers, batch_id, 0,
            jobs_path="/api/hub/transcription/jobs",
            deadline_seconds=lane_deadline_seconds(request),
        )

        artifact = client.get(
            _artifact_url(base, batch_id, 0, "transcription") + "?format=srt",
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=10.0),
        )
        if artifact.status_code >= 400:
            raise BackendError(
                f"hub transcript artifact HTTP {artifact.status_code}",
                code="PROVIDER_FAILED",
                status=artifact.status_code,
            )

    payload: dict[str, Any] = {"hub_batch_id": batch_id}

    def deliver() -> dict[str, Any]:
        # Subtitle has its own receipt route; the SRT is only really delivered
        # once the worker has written it.
        payload["delivery_ack"] = _ack_delivery(
            base, headers, batch_id, 0, "transcription"
        )
        return payload["delivery_ack"]

    return BackendResult(
        content=artifact.content,
        media_type="application/x-subrip",
        payload=payload,
        deliver=deliver,
    )


def _chat_body(request: BackendRequest) -> dict[str, Any]:
    """The OpenAI-shaped body the Hub's chat lane accepts.

    **Only the parameters that lane honours.** Hub 0.6.63 trims its own allowlist
    to `temperature`, `top_p`, `max_tokens`/`max_completion_tokens`, `seed`,
    `thinking` and `response_format`, and states it in the lane contract; a
    parameter outside it is dropped at the far end, so sending one would be a
    promise made to the caller that the answer does not keep. `stream` is sent
    explicitly as `false`: this adapter's contract is one request for one answer,
    and the Hub accepts the flag.

    Messages go through the **same normaliser the OpenAI-compatible lane uses**
    (Story Studio sends `input`, older callers send `messages`, content may be a
    string or a list of parts) — one implementation, so a shape change lands in
    both lanes rather than in whichever one was edited.
    """
    from .openai_compat import _messages  # one message normaliser, two lanes

    body: dict[str, Any] = {
        "model": request.model_id,
        "messages": _messages(request.params),
        "stream": False,
    }
    for key in ("temperature", "top_p", "seed", "thinking"):
        if request.params.get(key) is not None:
            body[key] = request.params[key]
    max_tokens = (
        request.params.get("max_completion_tokens")
        or request.params.get("max_output_tokens")
        or request.params.get("max_tokens")
    )
    if max_tokens:
        body["max_tokens"] = max_tokens
    response_format = request.params.get("response_format")
    # **Exactly the two the lane publishes.** The Hub's contract states
    # `structured_output.formats: ["json_object", "json_schema"]` and it refuses
    # anything else with 422 — measured 2026-09-25: **fourteen** jobs settled
    # `failed` on `response_format.type: must be json_object or json_schema;
    # received str 'text'`, and this allowlist carried `"text"`.
    #
    # Dropping `text` is a faithful translation rather than a silent loss, and the
    # contract is what says so: there is no `parameters` block on this lane, so
    # `response_format` is not an operator default, and `enforcement:
    # prompt_validate_repair` requests a format **only when the caller names one**.
    # Omitted therefore means unstructured here in exactly the way `text` does —
    # which is this adapter's own rule: send only the parameters the lane honours,
    # because "sending one would be a promise made to the caller that the answer
    # does not keep".
    if isinstance(response_format, dict) and response_format.get("type") in {
        "json_object", "json_schema",
    }:
        body["response_format"] = response_format
    return body


def _generate_chat(request: BackendRequest) -> BackendResult:
    """Run one chat completion on a Hub location.

    **Through the Hub, never straight to a worker.** The route is the Hub's peer
    surface — `POST {base}/studio/chat/v1/chat/completions` — so admission, the
    machine lease and the worker's token signing all apply exactly as they do for a
    request the Hub's own app makes. Hub 0.6.63 opened that route to a Fleet
    credential; before it, every chat route refused a Fleet token, which is why
    this lane did not exist here (and why the Hub's own `/api/hub/chat/completions`
    is deliberately not used — it is Hub-token only, and a second spelling of one
    operation is a second vocabulary).

    The answer **is** the artifact for this lane: a synchronous completion has no
    batch to poll, which is why its contract states `discard_supported: false`.
    `finish_reason` and `usage` pass through as the worker stated them — a gateway
    that rewrote `length` into a clean stop would be hiding a truncated answer from
    the one reader that refuses those.
    """
    base = _resolve_base(request)
    headers = {**_headers(request), "Content-Type": "application/json"}
    # The lane's own ceiling applies to the call itself, as it does to a poll: the
    # Hub holds this request open while the model runs.
    timeout = httpx.Timeout(lane_deadline_seconds(request), connect=10.0)
    try:
        response = httpx.post(
            f"{base}/studio/chat/v1/chat/completions",
            headers=headers,
            json=_chat_body(request),
            timeout=timeout,
        )
    except httpx.ConnectError as exc:
        # Nothing accepted the connection: an unreachable location, not a job
        # failure, so the worker may try the next site that offers the model.
        raise BackendError(
            f"could not reach the Hub at {base} ({type(exc).__name__})",
            code="LOCATION_UNREACHABLE",
            retryable=True,
            upstream_accepted=False,
        ) from exc
    except httpx.HTTPError as exc:
        # A read timeout *after* the request left is ambiguous: the model may have
        # run and been recorded as a job on that node with nobody to collect it.
        raise BackendError(
            f"hub chat failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True
        ) from exc

    if response.status_code >= 400:
        code = _hub_refusal_code(response)
        if code in _LOCATION_REFUSAL_CODES:
            raise BackendError(
                f"the location cannot serve this model right now ({code})",
                code="LOCATION_NOT_READY",
                status=response.status_code,
                retryable=True,
                upstream_accepted=False,
            )
        raise BackendError(
            # A 422 here is the Hub's own validation — a prompt past the model's
            # stated context, or a non-boolean `thinking` — and its sentence names
            # the field and the measured count, so it is quoted, not replaced.
            f"hub chat HTTP {response.status_code}: {response.text[:200]}",
            code="PROVIDER_FAILED",
            status=response.status_code,
            retryable=response.status_code >= 500,
            upstream_accepted=None if response.status_code >= 500 else False,
        )

    payload = response.json() or {}
    choices = payload.get("choices") or []
    if not choices:
        raise BackendError(
            "the Hub returned no choices for a chat completion", code="PROVIDER_FAILED"
        )
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    text = text if isinstance(text, str) else ""
    if not text.strip():
        # The Hub has its own code for this (`CHAT_EMPTY_ANSWER`), so an empty
        # answer is a failure here too rather than a successful blank job.
        raise BackendError(
            "the Hub returned an empty chat answer",
            code="PROVIDER_FAILED",
            upstream_accepted=True,
        )
    hub = payload.get("studio_hub") if isinstance(payload.get("studio_hub"), dict) else {}
    return BackendResult(
        text=text,
        payload={
            "finish_reason": choice.get("finish_reason"),
            "usage": payload.get("usage"),
            "model": payload.get("model") or request.model_id,
            # The worker's **own** generation time, measured after the model is
            # resident — it excludes a cold turn's model load, so it is neither
            # this gateway's round trip nor a substitute for one.
            "generation_seconds": hub.get("elapsed_seconds"),
        },
    )


class StudioHubBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality == "chat":
            return _generate_chat(request)
        if request.modality in {"image", "tts", "music"}:
            return _generate_image_or_voice(request)
        if request.modality == "transcription":
            return _generate_transcription(request)
        raise BackendError(
            f"Studio Hub does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
        )

    def discard(self, request: BackendRequest) -> dict[str, Any]:
        """Abandon an uncertain Hub attempt, releasing the machine it reserved.

        An uncertain item keeps its Mac reserved because the accepted worker may
        still be running, and only the Hub's own operator could previously release
        it. From Hub `639fcf9` the controller's fleet credential may, which is what
        lets this gateway act on a hold it recorded.

        **Operator-initiated, never automatic.** The Hub already discards after
        thirty minutes of silence as the walk-away backstop; the gateway abandoning
        real work on a timer would be a different and much worse decision. Nothing
        but the explicit action calls this.

        Only an item the Hub already settled `uncertain` can be discarded — it
        answers 400 otherwise — and it has no discard route for transcription
        batches, which is reported plainly rather than attempted.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if not batch_id:
            return {
                "ok": False,
                "status": None,
                "reason": "nothing was dispatched to the Hub for this job",
            }
        if request.modality == "transcription":
            return {
                "ok": False,
                "status": None,
                "reason": "the Hub has no discard route for transcription batches",
            }
        route = (
            f"/api/hub/jobs/{batch_id}/items/{request.upstream_item_index}/discard"
        )
        return _recorded_call("POST", f"{_resolve_base(request)}{route}", _headers(request), route)

    def cancel(self, request: BackendRequest) -> dict[str, Any]:
        """Stop a Hub batch this gateway dispatched.

        Cancellation that never reached the Hub was only ever a local request:
        the batch kept running at the location while Story Studio was told the
        job was cancelled. This is the request that actually reaches the work,
        and its answer is what the caller records.

        A job with no upstream id has nothing to stop — that is reported as
        "nothing dispatched", not as a successful stop.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if not batch_id:
            return {
                "ok": False,
                "status": None,
                "reason": "nothing was dispatched to the Hub for this job",
            }
        return _cancel_batch(
            _resolve_base(request), _headers(request), batch_id, request.modality
        )

    def retry(self, request: BackendRequest) -> dict[str, Any]:
        """Re-queue one failed item of a Hub batch this gateway can see.

        The mirror of `cancel` on the same handle: the batch id names the batch,
        the item index names the work, and the modality picks the route. A job with
        no upstream id has nothing to retry — reported as "nothing dispatched", not
        as a successful retry.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if not batch_id:
            return {
                "ok": False,
                "status": None,
                "reason": "nothing was dispatched to the Hub for this job",
            }
        return _retry_item(
            _resolve_base(request), _headers(request), batch_id,
            request.upstream_item_index, request.modality,
        )
