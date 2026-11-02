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

import base64
import hashlib
import json
import logging
import math
import re
import time
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx

from .base import (
    CHAT_DEADLINE_CEILING_S,
    BackendError,
    BackendRequest,
    BackendResult,
    answer_json,
    bounded_max_output_tokens,
    caller_max_output_tokens,
    expected_output_header,
    note_request_started,
    require_json_answer,
    staged_path,
    unusable_answer,
)

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0


def queue_wait_ceiling_s() -> float:
    """The longest a batch may sit `queued` on the Hub before this gateway withdraws it (and only
    if the Hub confirms nothing ran). The lane deadline does not count queue time, so without this
    a job behind a machine that never frees up would poll for ever.

    **The owner's number, read from the one place he edits it.** This was a module constant here
    (`QUEUE_WAIT_CEILING_S = 3 * 3600.0`) with a `ponytail:` note naming the upgrade path, while
    the editable budget lived in `concurrency.queue_wait_budget()` — which the worker and the API
    already read for deferrals and `Retry-After`. Two copies of one ceiling meant the owner could
    edit his and be silently overruled: 146 jobs failed `QUEUE_TIMEOUT` in the seven days to
    2026-10-19 on this constant. One read, one number (`queue.wait_ceiling_seconds`).

    ponytail: one flat number for every lane (Story's own wait is four hours); a per-lane figure
    if some lane's queue is legitimately longer.
    """
    from .. import concurrency
    return float(concurrency.queue_wait_budget()["wait_ceiling_seconds"])


#: How long a poll may keep failing before a batch becomes an unknown-outcome hold.
#:
#: **A dropped socket is not an unknown outcome; it is a dropped socket.** This ran
#: at one poll per second against a 900 s deadline and raised the hold on the very
#: *first* `httpx.HTTPError` -- so one blip out of as many as 900 polls discarded a
#: job that was still running. Measured on the owner's fleet 2026-09-29: batch
#: `0dcdc6331e` was reported failed at 23:10, and the Hub's own record shows that
#: same item finished `done` **with an artifact** 4m45s later. The image was
#: rendered and thrown away.
#:
#: The outcome is only genuinely unknown once the Hub has been unreachable for long
#: enough that we cannot say whether it is working. Sixty seconds of consecutive
#: failures is well past a blip and still far short of the lane's own ceiling, and
#: the deadline below bounds it either way -- so a Mac that really is gone still
#: produces its named hold, just not on the first refused connection.
POLL_DROP_GRACE_S = 60.0

#: **Every site silent at once is our fault, not theirs** (10-09 06:26 and 06:57: all remote sites
#: went quiet together and 59 renders still in progress were abandoned and re-run). When this many
#: remote sites lost contact within `FLEET_OUTAGE_WINDOW_S` of each other and are still silent, a
#: poll keeps asking past the grace instead of raising `lost_contact`, until the site answers or the
#: job's own deadline ends it. One site going silent alone is moved after the grace, as before.
#: ponytail: a fixed count fits today's 8-site fleet; scale it with the remote site count if that shrinks.
FLEET_OUTAGE_SITES = 3
FLEET_OUTAGE_WINDOW_S = 60.0
#: `base -> [first, last]` monotonic times of a remote Hub's current run of failed polls, shared by
#: every poll thread. Dropped on the first answer.
_SILENT: dict[str, list[float]] = {}
#: When each base last answered, so `_fleet_outage` can tell our network from a site that died.
_OK: dict[str, float] = {}
_SILENT_LOCK = threading.Lock()


def _is_local_base(base: str) -> bool:
    return urlsplit(base).hostname in {"127.0.0.1", "localhost", "::1"}


def _note_poll(base: str, now: float, *, failed: bool) -> None:
    """Record one poll's outcome at `base` for `_fleet_outage`."""
    with _SILENT_LOCK:
        if not failed:
            _SILENT.pop(base, None)
            _OK[base] = now
            return
        entry = _SILENT.get(base)
        if entry is None or now - entry[1] > FLEET_OUTAGE_WINDOW_S:
            _SILENT[base] = [now, now]  # a new silence, not the tail of an old one
        else:
            entry[1] = now


def _fleet_outage(base: str, now: float) -> bool:
    """`FLEET_OUTAGE_SITES` remote sites, `base` among them, fell silent within one window and still are.

    **And no other remote site answered after that silence began.** Losing our own network silences
    every remote site at once — the 2026-10-09 case this guard exists for, where giving up would have
    re-run 59 renders — while controllers dying on their own silence only themselves: on 2026-11-01
    four sites went together while the rest of the fleet kept answering. A success recorded after `mine[0]` means the path out to a remote site still works, so the
    silence is theirs. The local Hub answers whatever our network does, so it is never evidence.
    """
    if _is_local_base(base):
        return False
    with _SILENT_LOCK:
        mine = _SILENT.get(base)
        if mine is None:
            return False
        together = sum(1 for other, (first, last) in _SILENT.items()
                       if not _is_local_base(other) and abs(first - mine[0]) <= FLEET_OUTAGE_WINDOW_S
                       and now - last <= FLEET_OUTAGE_WINDOW_S)
        if together < FLEET_OUTAGE_SITES:
            return False
        return not any(not _is_local_base(other) and seen >= mine[0]
                       for other, seen in _OK.items())
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
#:
#: **Bumped 0.6.65 → 0.6.72 on 2026-09-26 at the owner's decision (DECIDE-1)**, and the
#: claim is earned rather than assumed: this whole session's work — the fleet reads, the
#: `model_limits` relay, `source_kind` end to end, the stall fields — ran against a **0.6.72**
#: Hub, and 0.6.72 is installed on this Mac and answering. The rule the paragraph above sets
#: is that a bump means *measured against*, not *available*, and that holds here.
#:
#: **Expect every other controller to read `older` on the Fleet page, and that is the point.**
#: This Mac sits at 0.6.77 and the others at 0.6.64–0.6.72; the page saying so is a true and
#: useful prompt to roll them forward, not a fault. **It is still never a gate** — they keep
#: working exactly as they did, which is the owner's 2026-09-24 decision and the reason this
#: number is a label and not a check.
#:
#: 0.6.76 declared on 2026-09-26 from the running Hub (`/api/version` → `0.6.76` build `1c0297b`),
#: and 0.6.77 the same day from `0.6.77` build `ebbc757` — both measured, not read off a changelog.
#: The video lane is why: a controller older than 0.6.76 serves no `video` worker at all.
#:
#: **0.6.83 declared 2026-09-27, and this one is load-bearing in a way the number usually is not.**
#: It is the release whose video lane publishes the `audio` switch this gateway now relays and
#: forwards; against an older Hub the switch is simply absent from the contract, so nothing is
#: offered and nothing is dropped. The declaration is read off the running Hub, and the Hub release
#: it names is the one this build was measured against.
#:
#: **0.6.117 declared 2026-09-30** (owner/coordinator; the Hub release this gateway now publishes and
#: pilots). It is a label, never a gate: 0.6.83 was stale by 34 releases and the Fleet page said so.
SUPPORTED_STUDIO_HUB_VERSION = "0.6.126"

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
#:
#: The pre-acceptance site properties `_submission_readiness` / `_peer_admission_refusal` also
#: emit (`STUDIO_NOT_RUNNING`, `OUTPUT_STORAGE_BLOCKED`, `MODEL_CATALOG_UNAVAILABLE`) are the same
#: kind of thing — true here now, maybe not on another Mac — so they re-route too. The
#: operator-decision codes (`MODEL_PAUSED`, `MACHINE_DISABLED`, `STUDIO_DISABLED`) stay terminal
#: (except `MACHINE_DISABLED` on the chat route: a free hand-back, see `_generate_chat`).
_LOCATION_REFUSAL_CODES = {
    "MODEL_NOT_READY", "MODEL_NOT_PACKAGED", "MACHINE_BUSY",
    "STUDIO_NOT_RUNNING", "OUTPUT_STORAGE_BLOCKED", "MODEL_CATALOG_UNAVAILABLE",
    "NO_AVAILABLE_MACHINE",
}

#: The Hub has no unpaused Mac that can run this model (Hub 0.6.141, `INTERFACES-0141.md`): a 503 on
#: create, or a queued item handed back with this `error_code`. Kept as its own code, not
#: `LOCATION_NOT_READY`, so the worker treats it as a hand-back - moved at once, a fresh request id
#: there, nothing charged, not counted against the retry ceiling - instead of waiting on that group.
NO_AVAILABLE_MACHINE = "NO_AVAILABLE_MACHINE"

#: A free job still queued after this long at a group whose fleet read shows no unpaused Mac able
#: to run its model is withdrawn and handed back the same way: the safety net for a Hub too old to
#: say `NO_AVAILABLE_MACHINE` itself (measured 2026-10-04: 21 TTS jobs queued ~7,000 s at a group
#: whose every Mac was paused).
STRANDED_QUEUE_S = 600.0


def _hub_refusal_code(response: httpx.Response) -> str | None:
    """The Hub's own `code` on a refusal, when it carries one.

    Two shapes, both real: the peer/chat route puts `code` beside `detail`, while the batch-submit
    routes raise `HTTPException(409, {"code": ...})`, which FastAPI renders as `detail: {code}`.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    code = body.get("code") or (detail.get("code") if isinstance(detail, dict) else None)
    return str(code) if code else None


def _hub_refusal_reason(response: httpx.Response) -> str:
    """The Hub's own sentence for a refusal, or `""`.

    The code says *what kind* of refusal it was; the Hub's `detail` says *why* ("managed output
    storage has insufficient free space", "needs a ~16GB machine; this one has 8GB"). The adapter
    built its message from the code alone, so the owner read "(OUTPUT_STORAGE_BLOCKED)" and never
    that a Mac's disk was nearly full. Same two shapes as `_hub_refusal_code`.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    detail = body.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("detail")
    return " ".join(str(detail).split())[:240] if isinstance(detail, str) else ""


def _submit_unconfirmed(base: str, exc: Exception) -> dict[str, Any]:
    """The marker for a submit whose answer never came: the Hub may or may not have accepted it.

    `replayable`: the Hub answers a repeated submit with the batch it already made (the image, voice
    and music routes by `clientRequestId` - the job id - and the transcription route by its active
    duplicate), so asking the **same** location again cannot start a second run. The worker does that
    for a free Studio Hub job before it gives up on the location (`lost_contact`).
    """
    return {"location": base, "reason": str(exc).strip() or type(exc).__name__,
            "lost_contact": True, "replayable": True}


def _is_location_refusal(code: str | None, response: httpx.Response) -> bool:
    """A refusal that is a fact about this location *now*, not about the job.

    A named refusal (`_LOCATION_REFUSAL_CODES`), or an unnamed **503** from the Hub's submit door: the
    Hub raises it before it accepts anything (readiness could not be verified, no machine fit), so
    nothing was accepted and the job may wait or go elsewhere. It used to fall through as an
    ambiguous 5xx and file a recovery hold for work that never started. A *named* refusal outside the
    set (`MODEL_PAUSED`) stays terminal, and chat keeps its own stricter rule: it is synchronous, so a
    bare 503 there may have run.
    """
    return code in _LOCATION_REFUSAL_CODES or (code is None and response.status_code == 503)


def _refusal_sentence(code: str | None, response: httpx.Response) -> str:
    reason = _hub_refusal_reason(response)
    what = f"cannot serve this model right now ({code})" if code else "has no capacity for this right now (HTTP 503)"
    return f"the location {what}" + (f": {reason}" if reason else "")


def _refusal_error(code: str | None, response: httpx.Response) -> BackendError:
    """A location refusal at submit: nothing was accepted, so the worker may try elsewhere."""
    return BackendError(
        _refusal_sentence(code, response),
        code=NO_AVAILABLE_MACHINE if code == NO_AVAILABLE_MACHINE else "LOCATION_NOT_READY",
        status=response.status_code,
        retryable=True,
        upstream_accepted=False,
    )


#: The Hub's own sentence when a *named* `max_tokens` does not fit beside the prompt it
#: measured (`llm_engine._budget`, `asked=True`). Captured verbatim from the live refusal of
#: 2026-11-02 on gemma-4: "messages: the prompt is 13604 tokens and max_tokens asks for 32768
#: more, 46372 together, above the 32768 tokens this model states (...)". The three counts it
#: states are the Hub's own measurement, so they may be used — a characters-per-token ratio
#: may not (`base.bounded_max_output_tokens`).
_HUB_ROOM_LEFT = re.compile(
    r"the prompt is (?P<prompt>\d+) tokens and max_tokens asks for (?P<asked>\d+) more"
    r", \d+ together, above the (?P<window>\d+) tokens this model states"
)
#: How much of the window is held back for the answer, and the smallest answer still worth
#: running. The owner's 2026-11-02 figures for gemma-4 (window 32768, ceiling 23000).
_ROOM_LEFT_MARGIN_TOKENS = 1000
_MIN_ANSWER_TOKENS = 1024


#: Recognition, deliberately narrow: the Hub's own words for "a figure the *request* named does
#: not fit beside the prompt I measured". The prompt-only refusal says "the prompt is N tokens,
#: above the" and never this, so it is never re-sent. Matched on the phrase rather than the
#: whole sentence because the counts around it vary, and because a Hub that rewords the tail
#: must not turn a resize into a silent second guess.
_HUB_NAMED_FIGURE_REFUSAL = "max_tokens asks for"


def _hub_detail(response: httpx.Response) -> str:
    """The Hub's own `detail` sentence, or `""`. Read whole, never through a truncating helper."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError, TypeError):
        return ""
    return " ".join(detail.split()) if isinstance(detail, str) else ""


def _room_left_after_prompt(response: httpx.Response) -> dict[str, int] | None:
    """The Hub's own counts from its room-left refusal, or `None` when it named no window.

    Read from the whole body, not `_hub_refusal_reason`: that helper truncates to 240
    characters and this sentence runs past it, so the tail — and with it a window stated late —
    would be lost. `None` here means "no figure to resize to", not "not this refusal": the
    caller decides that from `_hub_detail`.
    """
    match = _HUB_ROOM_LEFT.search(_hub_detail(response))
    return ({name: int(value) for name, value in match.groupdict().items()}
            if match is not None else None)


def _room_left_resend(body: dict[str, Any], counts: dict[str, int]) -> dict[str, Any] | None:
    """The body to re-send once, or `None` when the answer room is too small to be worth it.

    The Hub refused at the door — `check_prompt_fits` runs before any token is produced — so
    nothing has run and a second request costs only a second door check. The new figure is
    `min(asked, window - prompt - margin)`, taken from the Hub's own measurement; a prompt that
    already leaves no room for a real answer is a refusal, not a smaller ask.
    """
    room = counts["window"] - counts["prompt"] - _ROOM_LEFT_MARGIN_TOKENS
    if room < _MIN_ANSWER_TOKENS:
        return None
    resent = dict(body)
    asked = body.get("max_tokens")
    resent["max_tokens"] = min(int(asked), room) if isinstance(asked, int) else room
    return resent


def _stranded(request: BackendRequest) -> Callable[[], bool]:
    """Whether the fleet read shows no unpaused Mac at this job's location able to run its model."""
    from ..fleet import location_has_no_free_mac

    return lambda: location_has_no_free_mac(request.provider_id, request.model_id)


def _withdrawn(base: str, headers: dict[str, str], batch_id: str, jobs_path: str) -> bool:
    """Cancel a queued batch; True only when the Hub says it took it out of the queue unrun."""
    kind = "transcription" if "transcription" in jobs_path else "image"
    cancel = _cancel_batch(base, headers, batch_id, kind)
    answer = cancel.get("answer") if isinstance(cancel.get("answer"), dict) else {}
    return bool(cancel.get("ok")) and int(answer.get("queued_cancelled") or 0) >= 1 \
        and not answer.get("running_signalled") and not answer.get("running_pending")


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
    "video": "video",
}

#: The video item keys the Hub's broker forwards to its Video worker.
#:
#: `duration` rather than `duration_seconds` in the *forwarded* body, because the broker's
#: image-to-video door flattens form fields and only carries `duration`; the lane's own contract
#: publishes `duration_seconds` and the worker accepts either spelling, so a text-to-video job
#: (which goes as JSON) and an image-to-video job (multipart) resolve to the same request.
#: `mode` is deliberately not sent: the broker sets `mode=img2video` for the reference path
#: itself, and the worker derives text-to-video when no image is attached.
#:
#: `audio` was added 2026-09-27 with Hub 0.6.83, which published the switch. **Forwarding it is
#: the whole reason it is in this tuple**: a lane parameter the caller sets and this gateway drops
#: is a lie with a schema — Story Studio would have shown a switch that changed nothing, and the
#: caller would have paid for a track it asked not to be written. Only what the caller set travels;
#: the lane's own default applies where the field is absent, and inventing a value here would
#: decide a lane's behaviour on the caller's behalf.
_HUB_VIDEO_PARAMS = (
    "duration",
    "aspect_ratio",
    "resolution",
    "width",
    "height",
    "frames",
    "fps",
    "seed",
    "audio",
)


def _video_reference(request: BackendRequest) -> list[dict[str, Any]] | None:
    """The uploaded scene image, as the Hub's reference-image item entry.

    Story Studio uploads the exact scene image to this gateway and submits its id; the Hub's
    broker resolves `reference_images[]` entries itself (`b64`, a tailnet `url`, or one of *its*
    asset ids) and takes the img2video multipart door when one is present. Inline base64 is the
    only one of the three that works across the two systems: the Hub has never seen this
    gateway's asset ids, and handing it a URL would make the Hub fetch back from the gateway.

    An id that has aged out is a caller problem and named as one, rather than silently becoming
    a text-to-video job that animates nothing.
    """
    from .. import video_assets

    staged = staged_path(request.params.get("_reference_path"))
    if staged is not None and staged.exists():
        return [{"b64": base64.b64encode(staged.read_bytes()).decode("ascii"),
                 "mime": str(request.params.get("_reference_type") or "image/png")}]
    asset_id = str(request.params.get("reference_asset_id") or "").strip()
    if not asset_id:
        return None
    asset = video_assets.get(asset_id)
    if asset is None:
        raise BackendError(
            "the source image for this video job is no longer held; upload it again and retry",
            code="INVALID_PARAMS",
            upstream_accepted=False,
        )
    return [{"b64": base64.b64encode(asset["content"]).decode("ascii"),
             "mime": asset["media_type"]}]


def _image_reference(request: BackendRequest) -> list[dict[str, Any]] | None:
    """The uploaded reference images, as the Hub's `reference_images` item entries, in order.

    "Reference to Image": Story Studio uploads each character/reference image to this gateway and
    submits the ids; `create_image` stages every one of them beside the transcription uploads, so
    the bytes are here at dispatch time. The Hub's broker resolves `reference_images[]` entries
    itself and takes its edit door when they are present; inline base64 is the only one of its
    accepted shapes that works across the two systems (the Hub has never seen this gateway's asset
    ids, and a URL would make the Hub fetch back from the gateway).

    An entry that has aged out is a caller problem and named as one — with its position, so a
    three-reference job twice the size of an ordinary one is not sent with one image missing.

    `None` means the request names no references at all, which is an ordinary text-to-image job.
    """
    paths = request.params.get("_reference_paths")
    if not isinstance(paths, list) or not paths:
        return None
    types = request.params.get("_reference_types")
    types = types if isinstance(types, list) else []
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    for index, path in enumerate(paths):
        staged = staged_path(path)
        if staged is None or not staged.exists():
            missing.append(f"{index + 1} of {len(paths)}")
            continue
        media_type = str(types[index]) if index < len(types) and types[index] else "image/png"
        entries.append({"b64": base64.b64encode(staged.read_bytes()).decode("ascii"),
                        "mime": media_type})
    if missing:
        raise BackendError(
            f"reference image {', '.join(missing)} for this image job is no longer held; "
            "upload the references again and retry",
            code="INVALID_PARAMS",
            upstream_accepted=False,
        )
    return entries


def _reference_count(item: dict[str, Any], terminal: dict[str, Any]) -> int | None:
    """How many references the Hub says the worker actually attached — `None` when it did not say.

    Read from the worker's `terminal_result` first and the item beside it second, because the Hub
    states it wherever it files the worker's job summary. `None` is "unstated", which is a different
    fact from zero and must not be read as one.
    """
    for source in (terminal, item):
        if not isinstance(source, dict):
            continue
        value = source.get("reference_count")
        if type(value) is int and value >= 0:
            return value
    return None

#: What the Hub's Music item accepts. Which engine takes which field differs (the
#: vocal engines take `lyrics`, `vocal_language`, `instrumental`; the Hub maps
#: YuE2's vocal language into its style prompt. Stable Audio 3 is
#: instrumental only and takes the rest). The gateway forwards whichever of these
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
    # The delivery container, validated at submit against the modality's list *plus* whatever this
    # model's own row publishes (`api.OUTPUT_FORMATS` / `api._published_output_formats`), so a lane
    # that delivers FLAC (`audio/flac` below) is one a caller may ask for. Optional: absent keeps the
    # lane's native file, and a node that predates it drops the field and answers with that native file.
    "output_format",
)


def _music_params(params: dict[str, Any]) -> dict[str, Any]:
    """Use the same canonical music fields for dispatch and its time allowance."""
    supplied = params.get("params")
    result = dict(supplied) if isinstance(supplied, dict) else {}
    result.update({key: params[key] for key in _HUB_MUSIC_PARAMS if params.get(key) is not None})
    for alias, canonical in (("language", "vocal_language"), ("duration", "duration_seconds")):
        value = params.get(alias, result.get(alias))
        if result.get(canonical) is None and value is not None:
            result[canonical] = value
        result.pop(alias, None)
    return result


def _resolve_base(request: BackendRequest) -> str:
    from ..hub import hub_base_url

    base = (request.values.get("base_url") or request.base_url or hub_base_url()).strip()
    if not base:
        raise BackendError(
            "Studio Hub is not running (no hub-ready.json found)",
            # A Hub that is not there is a site that is down: nothing was sent, so the worker may
            # wait for it or try the next location -- not file an unknown outcome (audit 2026-10-01).
            code="LOCATION_UNREACHABLE",
            retryable=True,
            upstream_accepted=False,
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


#: A narration's own run allowance (owner decision 2026-10-05, Breeze night-atlas failures):
#: `ceil(chars / 8.6) x RTF x 3 + 300` s, clamped to [the lane's stated deadline, 7,200 s]. 8.6 is
#: Breeze's slowest measured narration rate (characters per audio second); RTF is generation seconds
#: per audio second for the Mac running it: its own measured median for this model, else its Mac
#: class's, else 5.22 (the slowest class measured, M1 16 GB). x3 covers section re-renders (measured
#: up to 3.47 renders per delivered section) and +300 s a cold model and Whisper load.
TTS_CHARS_PER_AUDIO_S = 8.6
TTS_FALLBACK_RTF = 5.22
TTS_DEADLINE_MARGIN = 3.0
TTS_DEADLINE_OVERHEAD_S = 300.0
TTS_DEADLINE_CEILING_S = 7200.0


def _tts_rtf(provider_id: str, model_id: str, machine: str | None) -> float:
    """This Mac's measured RTF for this voice model, else its Mac class's, else `TTS_FALLBACK_RTF`.

    `machine` is the Hub's own name for the Mac running the item (`local` for the controller), the
    same value a finished job records as `hub_machine`. `None` (not dispatched yet) is the fallback.
    """
    if not machine:
        return TTS_FALLBACK_RTF
    try:
        import statistics

        from .. import fleet
        from ..admin import mac_class
        from ..runtime import store

        samples = store.tts_speed_samples(model_id)
        own = [rtf for p, m, rtf in samples if (p, m) == (provider_id, machine)]
        if own:
            return statistics.median(own)
        classes = {
            (str(row.get("provider_id") or ""), str(mac.get("machine") or "")):
                mac_class(mac.get("chip"), mac.get("memory_gb"))
            for row in fleet.snapshot(allow_stale=True).get("locations") or []
            for mac in row.get("machines") or []
        }
        mine = classes.get((provider_id, machine))
        same = [rtf for p, m, rtf in samples
                if mine and "unknown" not in mine and "?" not in mine and classes.get((p, m)) == mine]
        if same:
            return statistics.median(same)
    except Exception:  # noqa: BLE001 - no measurement read is "unmeasured", never a failed job
        log.debug("could not read measured TTS speed", exc_info=True)
    return TTS_FALLBACK_RTF


def _tts_length_deadline(request: BackendRequest, machine: str | None = None) -> float:
    """A narration's own run allowance on `machine` (see `TTS_CHARS_PER_AUDIO_S`); `0` for other lanes.

    ponytail: floor only -- the lane's stated `deadline_seconds` still wins when it is larger.
    """
    if request.modality != "tts":
        return 0.0
    text = request.params.get("text") if isinstance(request.params, dict) else None
    audio_s = -(-len(str(text or "")) // TTS_CHARS_PER_AUDIO_S)
    rtf = _tts_rtf(request.provider_id, request.model_id, machine)
    return min(TTS_DEADLINE_CEILING_S,
               audio_s * rtf * TTS_DEADLINE_MARGIN + TTS_DEADLINE_OVERHEAD_S)


#: **A YuE2 song's run allowance** (owner 2026-10-12): the slowest measured Mac's seconds of compute per
#: second of song x the requested length, plus its model load, x 2 (100 % overhead) — one number for
#: every Mac, from the slowest one. Measured table: `_orchestra/audit-20261012/lyrics-flow/SPEED-fleet.md`.
#: The music lane's flat 900 s cut 3-4 min songs (7 of 18 at 15:44). Hub 0.6.170 states the same number
#: on the batch item (`deadline_s`) and its bridge stops there; this keeps an older Hub's songs alive too.
YUE2_MODEL_ID = "vanch007/mlx-Yue2-3B"
#: Measured 2026-10-12 on 3.5 min songs: M1 16 GB 7.09 s per song-second (the slowest; load included),
#: M2 16 GB 5.92, M4 16 GB 4.77, M4 24 GB 4.90. Hub 0.6.170's `yue2.SLOWEST_S_PER_SONG_S` is the same.
YUE2_SLOWEST_S_PER_SONG_S = 7.1
YUE2_SLOWEST_LOAD_S = 120.0
YUE2_DEADLINE_MARGIN = 2.0


def _yue2_length_deadline(request: BackendRequest) -> float:
    """A YuE2 song's own allowance from its requested length; `0` for anything else."""
    if request.modality != "music" or str(request.model_id or "") != YUE2_MODEL_ID:
        return 0.0
    try:
        song = max(0.0, float(_music_params(request.params or {}).get("duration_seconds") or 0))
    except (TypeError, ValueError):
        song = 0.0
    return YUE2_DEADLINE_MARGIN * (YUE2_SLOWEST_S_PER_SONG_S * song + YUE2_SLOWEST_LOAD_S)


def lane_deadline_seconds(request: BackendRequest | None, machine: str | None = None) -> float:
    """How long to poll this job, as the **lane that enforces it** states it.

    Hub 0.6.61 puts `deadline_seconds` inside each model's `lane_contract` — its
    own peer-proxy timeout, "one number, owned by the party that enforces it".
    Reading it is what stops this gateway from cutting a lane short at its own flat
    `JOB_DEADLINE_S` after the Hub has raised its ceiling, which would fail a job
    that was about to succeed.

    A narration's own allowance (`_tts_length_deadline`) on `machine` -- the Mac the Hub says runs
    it, once it says -- raises a voice job above the lane's number.

    Falls back to `JOB_DEADLINE_S` whenever nothing states one: an older Hub, a
    lane with no contract, or a job whose model is not in the cache. The read is
    from the cached discovery row, so it costs no network time on the poll path.

    **The fallback stays a flat 900, and it is not the number to "fix" when the Hub's
    lanes diverge.** On 2026-09-26 Studio Hub made the deadline **per lane** — chat
    1,800 s and voice 1,800 s; image, transcription and music 900 s; video 7,200 s — and the
    previous single constant, `PEER_PROXY_TIMEOUT_S = 900.0`, no longer exists on their
    side. A flat number therefore matches no Hub's table any more, which is exactly why
    it must not be raised to the largest one: **900 is the correct answer for the only
    condition that reaches it.** A Hub that states no deadline is a Hub from before
    that change, whose one proxy timeout *was* 900 for every lane; a lane we cannot read
    a contract for is a lane we know nothing about, and cutting it early fails loudly
    while letting it run to the longest allowance fails by holding a machine for two
    hours. Per-modality numbers here would be a **second copy of a Hub table** that
    goes stale in silence — the thing their own commit message refuses ("a worker
    restating it would be a second copy of a Hub number").
    """
    if request is None:
        return JOB_DEADLINE_S
    # **The chat lane is bounded by the Hub's ceiling on every path**, including the two that
    # state nothing at all. Since 2026-10-19 the Hub computes each Chat job's deadline from the
    # job — its own prompt, its own expected answer, that Mac's measured rate — and its contract
    # can only publish the lane's floor (1,800 s). A 100-prompt scene pack is ~2,500 s of
    # generation on the slowest measured Mac before load and margin, so any shorter bound here
    # cuts a job the Hub is still legitimately running and records a failure for an answer that
    # exists. A chat request whose model is not in the cache is the same case: the lane is known
    # from the request itself, even when the contract is not.
    chat_floor = (CHAT_DEADLINE_CEILING_S
                  if str(getattr(request, "modality", "") or "") == "chat" else 0.0)
    try:
        from ..providers import registry  # local import: avoids a cycle

        for model in registry.models.models_for(request.provider_id, include_disabled=True):
            if str(model.get("model_id") or "") != str(request.model_id or ""):
                continue
            raw = model.get("raw") or {}
            contract = raw.get("lane_contract")
            base = JOB_DEADLINE_S
            if isinstance(contract, dict):
                stated = contract.get("deadline_seconds")
                # A stated deadline of zero or less is not a deadline; keep ours rather
                # than failing every poll instantly on a nonsensical number.
                if isinstance(stated, (int, float)) and not isinstance(stated, bool) and stated > 0:
                    base = float(stated)
            return max(base, chat_floor, _tts_length_deadline(request, machine),
                       _yue2_length_deadline(request))
    except Exception:  # noqa: BLE001 - an unreadable contract is "unstated"
        return max(JOB_DEADLINE_S, chat_floor)
    return max(JOB_DEADLINE_S, chat_floor)


class HubProgress:
    """The worst progress the Hub reported while this gateway polled one batch.

    **The first consumer of a field Studio Hub added and nothing reads yet** — their
    own words, 2026-09-26: *"it is on the row now… nothing in our native app consumes
    it yet, so you would be first."* It is collected here because the chat and voice
    deadlines are now **1,800 s**, and the Hub's argument for why a thirty-minute
    ceiling is *safe* rather than merely long is that a stall is visible — which is
    only true if something shows it. On this path that something is us: Story Studio
    cannot reach a Hub.

    **Every poll's figure is already in the response we read** — `/api/hub/jobs/{id}`
    carries `stalled`, `no_progress_s` and `stalled_after_s` — so this costs no extra
    request. It is a **record**, not a live signal: a caller watching an unfinished job
    still sees `running` until it settles. That half needs a write back to our own job
    row mid-flight and is deliberately not built; see GW-13.

    **The two stall vocabularies are close enough to be confused, and are not the same
    field.** These are the *progress-based* ones (`broker.py`: `stalled_after_s =
    max(15 min, 5 × average)`, `stalled = no_progress_s >= stalled_after_s`). The
    *silence-based* `stalled_seconds` (120 s, `stall.py`) lives on `/api/hub/stats` →
    `fleet_activity`, a surface we do not poll for a job.
    """

    __slots__ = ("stalled", "worst_no_progress_s", "stalled_after_s", "samples", "machine",
                 "run_s")

    def __init__(self) -> None:
        self.stalled = False
        self.worst_no_progress_s: float | None = None
        self.stalled_after_s: int | None = None
        self.samples = 0
        self.machine: str | None = None  # the Mac the Hub said runs the item
        self.run_s = 0.0  # seconds the item has run (what the lane budget counts)

    def note(self, summary: dict[str, Any]) -> None:
        """Record one poll's figure, if it stated one."""
        seconds = summary.get("no_progress_s")
        # **`None` is "not applicable", never zero.** The Hub sets it only while a
        # batch is active — a queued or finished job has no progress to be missing —
        # and folding that in as `0.0` would make every completed job look like it had
        # the best possible progress and drag the worst-case figure down.
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return
        self.samples += 1
        self.worst_no_progress_s = max(self.worst_no_progress_s or 0.0, float(seconds))
        after = summary.get("stalled_after_s")
        if isinstance(after, int) and not isinstance(after, bool):
            self.stalled_after_s = after
        if summary.get("stalled") is True:
            self.stalled = True

    def as_payload(self) -> dict[str, Any] | None:
        """What to publish, or `None` when the Hub never stated a figure at all.

        **"The Hub said nothing" and "the Hub said it never stalled" are different
        answers and must not share a value.** An older Hub, or a lane that reports no
        progress, gets `None` — not a reassuring `stalled: false` this gateway invented
        out of silence.
        """
        if not self.samples:
            return None
        return {
            "stalled": self.stalled,
            "worst_no_progress_s": self.worst_no_progress_s,
            "stalled_after_s": self.stalled_after_s,
            "samples": self.samples,
        }

    def evidence(self, batch_id: str) -> dict[str, Any]:
        """What a failed item leaves on the job record: which Mac, which batch, how long it ran."""
        return {"hub_batch_id": batch_id, "hub_machine": self.machine,
                "run_seconds": round(self.run_s, 1), "hub_progress": self.as_payload()}

    def why(self) -> str:
        """One clause for an error message, or `""` when it adds nothing."""
        if not self.stalled:
            return ""
        against = (f" against a {self.stalled_after_s}s threshold"
                   if self.stalled_after_s is not None else "")
        return (f"; the Hub reported it stalled, with no progress for "
                f"{self.worst_no_progress_s:.0f}s{against}")


def _poll_batch(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    batch_id: str,
    item_index: int,
    jobs_path: str = "/api/hub/jobs",
    deadline_seconds: float | None = None,
    progress: HubProgress | None = None,
    on_wait: Callable[[dict[str, Any] | None], None] | None = None,
    dispatched_at: float | None = None,
    stranded: Callable[[], bool] | None = None,
    deadline_for_machine: Callable[[str], float] | None = None,
    resumed_after_restart: bool = False,
    on_deadline: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Poll a Hub batch to a terminal state.

    `deadline_for_machine` re-reads the limit once the Hub names the Mac running the item (a
    narration's allowance depends on that Mac's measured speed); without it the limit is fixed.

    `jobs_path` differs by modality: image and voice share `/api/hub/jobs`,
    while transcription batches live under `/api/hub/transcription/jobs` and
    are *not* visible from the other route -- polling the wrong one returns 404
    rather than a useful error.

    `deadline_seconds` is the lane's own ceiling when it published one (see
    `lane_deadline_seconds`), and this gateway's flat fallback otherwise. **It counts only the
    time the Hub item is running** (owner decision, 2026-09-30): the lane's number is the Hub's
    per-attempt *generation* ceiling, and spending it on time spent queued behind a busy fleet
    turned a job that was merely waiting for a Mac into a forced unknown-outcome hold. While the
    item is `queued` the clock is paused and `on_wait` is told why (the Hub's own note, and when
    it will try again), so the job can say "waiting for a Mac" instead of looking merely slow.
    `queue_wait_ceiling_s()` is the only bound on that wait, and it cancels rather than guesses.

    **The run clock is the job's, not this call's.** A poll that begins mid-render -- a Gateway
    restart resuming a batch -- must not start the budget from zero, or the limit would apply per
    Gateway process and a job resumed several times would run far past it. So while the item is
    `running` the clock is taken from the Hub's own `elapsed_s` (computed from its `run_started`,
    so no clock skew between machines). A Hub that does not report it falls back to
    `dispatched_at`, the epoch time this Gateway recorded when it dispatched the job (counted
    once, from the first running answer); `None` for a fresh job, which has nothing to catch up on.

    `progress` is filled in as we go and read by the caller afterwards. It is passed
    in rather than returned because the interesting case is the one that **raises**:
    a job that times out after being stalled for most of its window is exactly what
    `HubProgress.why()` exists to explain, and a return value never reaches the caller
    on that path.
    """
    limit = float(deadline_seconds) if deadline_seconds else JOB_DEADLINE_S
    # Read once, like `limit`: the ceiling is the owner's setting (`queue_wait_ceiling_s`) and a
    # poll loop is not the place to re-stat a settings file once a second.
    ceiling = queue_wait_ceiling_s()
    started = tick = time.monotonic()
    ran = 0.0  # seconds this item has spent running -- the only time the lane budget counts
    queued_for = 0.0  # seconds it has spent queued -- the only time the queue ceiling counts
    running = False
    waiting_key: tuple[Any, ...] | None = None
    waiting_since = 0.0
    seen_machine = ""  # the Mac the Hub last said runs the item
    caught_up = False  # `ran` has been seeded from the job's own start (`dispatched_at` fallback)
    withdraw_after = 0.0  # queued seconds before the next try at withdrawing a stranded item
    last: dict[str, Any] = {}
    #: When the current run of consecutive poll failures began, or `None` while the
    #: Hub is answering. Reset by any successful poll, so a Hub that drops one
    #: request an hour is never mistaken for one that has gone away.
    dropped_since: float | None = None
    outage_logged = False
    while True:
        now = time.monotonic()
        if running:
            ran += now - tick
        else:
            queued_for += now - tick
        tick = now
        if progress is not None:
            progress.run_s = ran
        # The ceiling is for a batch that is *still queued*: measured from `started` it also
        # withdrew (DELETE) an item that had waited 2 h 50 and then begun rendering, at the
        # three-hour mark of wall time.
        out_of_time = ran >= limit or (not running and queued_for >= ceiling)
        if out_of_time:
            break
        # **A Mac that dies mid-poll is a named hold, not a raw `ConnectError`.** This call had no
        # guard at all (audit M7): an `httpx.HTTPError` escaped to the worker's generic handler,
        # which recorded a durable `unknown_outcome` hold whose detail was the exception's own
        # repr — no batch, no item, no location — leaving the operator nothing to reconcile
        # against. The outcome is still **unknown**, and deliberately so: the Hub accepted the
        # batch, and a lost connection is not evidence that the worker stopped. What changes is
        # that the hold names the batch, the item and the machine, so it can be settled when that
        # machine answers again.
        # A dropped socket, a 5xx (the Hub restarting, a proxy hiccup) and a 2xx that is not JSON
        # are one thing: the Hub did not give an answer *this time*. A 4xx is an answer.
        fault = ""
        try:
            response = client.get(f"{base}{jobs_path}/{batch_id}", headers=headers)
            if response.status_code >= 500:
                fault = f"HTTP {response.status_code}"
            elif response.status_code < 400:
                summary = response.json() or {}
        except httpx.HTTPError as exc:
            fault = str(exc).strip() or type(exc).__name__
        except ValueError as exc:
            fault = f"unreadable answer: {exc}"
        if fault:
            now = time.monotonic()
            if dropped_since is None:
                dropped_since = now
            reason = fault
            # **Keep asking.** The batch is already accepted and the Hub keeps it,
            # so re-reading it is a GET on something that exists -- idempotent, and
            # the only question that settles the outcome. Giving up here is what
            # turned a finished render into a failure (see `POLL_DROP_GRACE_S`).
            _note_poll(base, now, failed=True)
            if now - dropped_since < POLL_DROP_GRACE_S:
                time.sleep(POLL_INTERVAL_S)
                continue
            if _fleet_outage(base, now):
                if not outage_logged:
                    outage_logged = True
                    log.warning("batch %s at %s: at least %s remote sites went silent together; still polling "
                                "rather than moving the job", batch_id, base, FLEET_OUTAGE_SITES)
                time.sleep(POLL_INTERVAL_S)
                continue
            raise BackendError(
                f"lost contact with {base} while polling batch {batch_id} "
                f"(item {item_index}) for {int(now - dropped_since)}s: {reason}",
                code="PROVIDER_SUBMISSION_UNCERTAIN",
                upstream_accepted=True,
                recovery={
                    "location": base,
                    "batch_id": batch_id,
                    "item_index": item_index,
                    "jobs_path": jobs_path,
                    "reason": reason,
                    # The location itself went silent (not a Hub-reported outcome): the worker
                    # moves a free Studio Hub job to another location on this, see `_dispatch`.
                    "lost_contact": True,
                },
            )
        # The Hub answered, so whatever happened before is over.
        _note_poll(base, now, failed=False)
        dropped_since = None
        if response.status_code >= 400:
            raise BackendError(
                f"hub status HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            )
        last = summary
        if progress is not None:
            progress.note(summary)

        items = summary.get("items") or []
        item = items[item_index] if item_index < len(items) else None
        state = str((item or {}).get("state") or summary.get("state") or "").lower()
        running = state != "queued"
        # The Mac running it, as the Hub names it ("studio@machine"; a bare studio is the controller).
        studio = str((item or {}).get("studio") or "")
        machine = str((item or {}).get("machine") or studio.partition("@")[2]
                      or ("local" if studio else ""))
        if running and machine and machine != seen_machine:
            seen_machine = machine
            if progress is not None:
                progress.machine = machine
            if deadline_for_machine is not None:
                limit = float(deadline_for_machine(machine) or limit)
        # Hub 0.6.159+: the Mac running an Image item states that job's own limit (its steps x that
        # Mac's speed), the number its worker stops at. It only ever raises the lane's number.
        stated = (item or {}).get("deadline_s")
        if (running and isinstance(stated, (int, float)) and not isinstance(stated, bool)
                and math.isfinite(stated) and stated > limit):
            limit = float(stated)
        if state == "running":
            reported = (item or {}).get("elapsed_s")
            if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
                ran = max(ran, float(reported))
                caught_up = True
            elif not caught_up and dispatched_at:
                ran = max(ran, time.time() - float(dispatched_at))
                caught_up = True
        if on_wait is not None:
            if state == "queued":
                reason = str((item or {}).get("error") or summary.get("governor_note") or
                             "waiting for a free Mac")
                retry_at = (item or {}).get("retry_at") or summary.get("next_retry_at")
                key = (reason, retry_at)
                if key != waiting_key:
                    if waiting_key is None:
                        waiting_since = time.time()
                    waiting_key = key
                    on_wait({"reason": reason, "next_try_at": retry_at, "since": waiting_since})
            elif waiting_key is not None:
                waiting_key = None
                on_wait(None)

        # **Queued where no Mac will take it: hand it back.** The Hub says so itself
        # (`NO_AVAILABLE_MACHINE` on the queued item), or - for an older Hub - the item has sat queued
        # `STRANDED_QUEUE_S` while `stranded()` (the fleet read) shows no unpaused Mac here able to run
        # it. A queued item has not run, so it is withdrawn, and only a confirmed withdrawal moves it.
        if state == "queued" and queued_for >= withdraw_after and (
                str((item or {}).get("error_code") or "") == NO_AVAILABLE_MACHINE
                or (stranded is not None and queued_for >= STRANDED_QUEUE_S and stranded())):
            if _withdrawn(base, headers, batch_id, jobs_path):
                raise BackendError(
                    f"{NO_AVAILABLE_MACHINE}: every Mac at this location that can run this model is "
                    "paused; it was withdrawn from the queue before anything ran",
                    code=NO_AVAILABLE_MACHINE,
                    retryable=True,
                    upstream_accepted=False,
                    upstream_settled=True,
                )
            withdraw_after = queued_for + 60.0  # not confirmed: keep waiting, ask again in a minute
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
            hub_code = str((item or {}).get("error_code") or "")
            if hub_code == "MACHINE_BUSY":
                # A controller older than Hub 0.6.111 fails a busy Mac as a terminal item. Nothing
                # ran, and busy is a moment, so it is the same site property the submit path names.
                raise BackendError(
                    f"the location cannot serve this model right now ({hub_code})"
                    + (f": {message}" if message else ""),
                    code="LOCATION_NOT_READY",
                    retryable=True,
                    upstream_accepted=False,
                    upstream_settled=True,
                )
            if hub_code in ("INSUFFICIENT_MEMORY", "WORKSTATION_YIELDED", NO_AVAILABLE_MACHINE):
                # INSUFFICIENT_MEMORY: the Mac refused this job for memory three times and the Hub
                # handed it back (Hub 0.6.127+; owner decision 2026-10-01). WORKSTATION_YIELDED: a
                # shop's workstation Mac stopped it because macOS reported critical memory pressure
                # for 20 s (rare: the only thing that interrupts a job there; someone using the Mac
                # lets the job finish) and its controller had no other free Mac (owner request
                # 2026-10-03). Nothing ran to completion and nothing is pending, so either is a
                # *known* failure of that Mac: the worker re-routes it to another location, excluding
                # this one (`worker._next_machine`; a yield does not count against the retry
                # ceiling), and a job that is pinned - a voice's owning Hub - fails with this same
                # sentence instead. Its own code, so the attempt log and the terminal reason say what
                # happened; deliberately not a LOCATION_* code, which would make the worker wait on
                # the same Mac rather than move or fail.
                said = (message or "").removeprefix(f"{hub_code}: ") or {
                    "INSUFFICIENT_MEMORY": "the Mac does not have enough free memory",
                    NO_AVAILABLE_MACHINE: "every Mac at this location that can run this model is paused",
                }.get(hub_code, "the Mac ran short of memory and stopped it; nothing was kept")
                raise BackendError(
                    f"{hub_code}: {said}",
                    code=hub_code,
                    retryable=True,
                    upstream_accepted=False,
                    upstream_settled=True,
                )
            if hub_code:  # MODEL_PAUSED, MACHINE_DISABLED, ...: terminal, but say which
                message = f"{hub_code}: {message or 'no detail'}"
            candidate = (_review_candidate_locator(client, base, headers, batch_id, item_index)
                         if hub_code.startswith("TTS_SECTION_") and jobs_path == "/api/hub/jobs"
                         else None)
            # **The Hub has finished this item, it produced nothing, and nothing is pending.**
            # The batch id recorded at submit does not contradict that: a batch is a container the
            # Hub creates *before* it validates the item, so a refused or failed item leaves an id
            # behind with no work running. Saying so explicitly is what stops the worker from
            # turning an ordinary item failure — a validation refusal, a model error, an engine out
            # of memory — into a durable `unknown_outcome` hold that asks the operator to reconcile
            # work that never ran. Measured live 2026-09-26: a 422 geometry refusal came back as
            # "Upstream outcome is unknown; reconcile the provider request before retrying; the
            # gateway will not resubmit it automatically". The artifact-fetch path below does *not*
            # set this, so a failure after a successful item keeps its hold.
            failure = BackendError(
                f"hub item {state}: {message or 'no detail'}",
                code="IMAGE_FAILED" if item is None else "PROVIDER_FAILED",
                upstream_settled=True,
            )
            failure.review_candidate = candidate
            raise failure
        time.sleep(POLL_INTERVAL_S)

    if ran < limit:
        # Still queued past the ceiling. A queued item has provably not run, so it may
        # be cancelled -- and only if the Hub *says* it cancelled it from the queue is it safe to
        # let the worker try another location. A running item is never cancelled on a guess.
        if _withdrawn(base, headers, batch_id, jobs_path):
            raise BackendError(
                f"the Hub kept this queued for {int(ceiling)}s with no machine free; "
                "it was withdrawn from the queue before anything ran",
                code="LOCATION_NOT_READY",
                retryable=True,
                upstream_accepted=False,
                upstream_settled=True,
            )
    # **The run budget is spent. The Gateway gives up -- and tells the Hub to stop.** The lane's
    # `deadline_seconds` is a number the Hub publishes but does not enforce for batch items, so
    # walking away used to leave the render running on the Mac with nobody collecting it (and the
    # Mac reserved). Now the give-up is a real cancel: ask the Hub to stop, take the worker's own
    # answer, and end the job with a typed failure that is not a hold. Only a worker that does not
    # answer stays an unknown outcome.
    why = progress.why() if progress is not None else ""
    if ran < limit:
        # Still queued past the ceiling and the Hub did not confirm withdrawing it: unknown.
        raise BackendError(
            f"the Hub kept this queued for {int(ceiling)}s and did not confirm withdrawing it",
            code="PROVIDER_SUBMISSION_UNCERTAIN",
            retryable=True,
        )
    if on_deadline is not None and resumed_after_restart and state == "running":
        try:
            on_deadline(f"Gateway restarted during this attempt; the Hub still reported batch {batch_id} "
                        f"running when its {int(limit)}s running limit was reached.")
        except Exception:  # noqa: BLE001 - still stop work if the record cannot be written
            log.exception("Could not record restart deadline for batch %s", batch_id)
    kind = "transcription" if "transcription" in jobs_path else "image"
    settled = _stop_hub_work(client, base, headers, batch_id, item_index, jobs_path, kind)
    if settled is not None:
        state = str((settled.get("item") or {}).get("state") or "").lower()
        if state in _TERMINAL_OK:
            return settled  # it finished while we were stopping it: the result is real, keep it
        if state in _TERMINAL_BAD:
            raise BackendError(
                f"the job ran past its {int(limit)}s limit on the Mac"
                + (f" {seen_machine}" if seen_machine else "") + f" and was stopped there{why}",
                code="DEADLINE_EXCEEDED",
                upstream_settled=True,
            )
    raise BackendError(
        f"hub job did not finish within {int(limit)}s of running"
        + (", and the Mac did not confirm it stopped" if settled is None else "")
        + why,
        code="PROVIDER_SUBMISSION_UNCERTAIN",
        retryable=True,
    )


# ── Rejected-section review candidates ────────────────────────────────────────────────────────
# A narration section the Voice quality gate refused on every attempt is kept on its worker, inactive,
# so the owner can listen to it (Hub `review-candidate` routes). It is never a result, an artifact or
# anything delivered or imported; these reads only ever GET.
REVIEW_MAX_PCM_BYTES = 64 * 1024 * 1024
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class ReviewCandidateUnavailable(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _review_sha256(value: Any) -> str:
    """The Voice worker's canonical receipt digest, recomputed here."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()


def _checked_review(answer: Any, batch_id: str, item_index: int) -> dict[str, Any]:
    """The Hub's review-candidate answer, re-proved here: its receipt, or ValueError."""
    receipt = answer.get("receipt") if isinstance(answer, dict) else None
    audio = receipt.get("audio") if isinstance(receipt, dict) else None
    if not isinstance(audio, dict):
        raise ValueError("review candidate")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (answer.get("active") is not False or answer.get("disposition") != "review_only"
            or answer.get("deliverable") is not False or answer.get("imported") is not False
            or answer.get("batch_id") != batch_id or answer.get("item_index") != item_index
            or receipt.get("active") is not False or receipt.get("disposition") != "review_only"
            or receipt.get("receipt_sha256") != _review_sha256(body)
            or audio.get("media_type") != "audio/wav" or type(audio.get("bytes")) is not int
            or not 0 < audio["bytes"] <= REVIEW_MAX_PCM_BYTES
            or not isinstance(audio.get("sha256"), str) or not _SHA256_HEX.fullmatch(audio["sha256"])):
        raise ValueError("review candidate")
    return receipt


def _review_candidate_locator(client: httpx.Client, base: str, headers: dict[str, str],
                              batch_id: str, item_index: int) -> dict[str, Any] | None:
    """The Hub's verified candidate for a refused narration item, as a credential-free locator."""
    try:
        response = client.get(f"{base}/api/hub/jobs/{batch_id}/items/{item_index}/review-candidate",
                              headers=headers)
        if response.status_code != 200:
            return None
        receipt = _checked_review(response.json(), batch_id, item_index)
    except (httpx.HTTPError, ValueError, TypeError):
        return None
    return {"batch_id": batch_id, "item_index": item_index,
            "receipt_sha256": receipt["receipt_sha256"],
            "pcm_bytes": receipt["audio"]["bytes"], "pcm_sha256": receipt["audio"]["sha256"]}


def _review_hub(values: dict[str, Any], base_url: str, local: bool) -> tuple[str, dict[str, str]]:
    from ..hub import hub_headers

    token = str((values or {}).get("hub_token") or "")
    if not local and not token:
        raise ReviewCandidateUnavailable(503, "this location has no current Studio Hub credential")
    base = _hold_base(base_url, local)
    if not base:
        raise ReviewCandidateUnavailable(503, "this location has no Studio Hub address")
    return base, hub_headers(token, local=local)


def review_candidate_receipt(values: dict[str, Any], base_url: str, locator: dict[str, Any],
                             *, local: bool) -> dict[str, Any]:
    """Read a bound candidate's receipt from its Hub now, and prove it is the one bound."""
    base, headers = _review_hub(values, base_url, local)
    route = f"{base}/api/hub/jobs/{locator['batch_id']}/items/{locator['item_index']}/review-candidate"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=6.0)) as client:
            response = client.get(route, headers=headers)
    except httpx.HTTPError as exc:
        raise ReviewCandidateUnavailable(502, "Studio Hub did not answer") from exc
    if response.status_code == 404:
        raise ReviewCandidateUnavailable(404, "the review candidate is no longer available")
    try:
        if response.status_code != 200:
            raise ValueError("status")
        receipt = _checked_review(response.json(), locator["batch_id"], locator["item_index"])
        if (receipt["receipt_sha256"], receipt["audio"]["bytes"], receipt["audio"]["sha256"]) != (
                locator["receipt_sha256"], locator["pcm_bytes"], locator["pcm_sha256"]):
            raise ValueError("binding")
    except (ValueError, TypeError) as exc:
        raise ReviewCandidateUnavailable(502, "the review candidate did not match its record") from exc
    return receipt


def review_candidate_audio(values: dict[str, Any], base_url: str, locator: dict[str, Any],
                           *, local: bool) -> bytes:
    """The bound candidate's WAV, read to at most one byte past its receipt and fully verified.

    Nothing is returned until the count and SHA-256 match exactly, so a caller never holds a
    partial or different body.
    """
    review_candidate_receipt(values, base_url, locator, local=local)
    base, headers = _review_hub(values, base_url, local)
    expected, digest = int(locator["pcm_bytes"]), str(locator["pcm_sha256"])
    route = (f"{base}/api/hub/jobs/{locator['batch_id']}/items/{locator['item_index']}"
             "/review-candidate/audio")
    body = bytearray()
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=6.0)) as client, \
                client.stream("GET", route, headers=headers) as response:
            if response.status_code != 200:
                raise ReviewCandidateUnavailable(
                    404 if response.status_code == 404 else 502,
                    "the review candidate's audio is not available")
            claimed = {name: response.headers.get_list(name)
                       for name in ("content-type", "content-length", "x-content-sha256")}
            if claimed != {"content-type": ["audio/wav"], "content-length": [str(expected)],
                           "x-content-sha256": [digest]}:
                raise ReviewCandidateUnavailable(502, "the review candidate's audio did not match its record")
            for chunk in response.iter_bytes():
                body += chunk[:expected + 1 - len(body)]
                if len(body) > expected:
                    break
    except httpx.HTTPError as exc:
        raise ReviewCandidateUnavailable(502, "the review candidate's audio was cut short") from exc
    if len(body) != expected or hashlib.sha256(body).hexdigest() != digest:
        raise ReviewCandidateUnavailable(502, "the review candidate's audio did not match its record")
    return bytes(body)


#: How long the Gateway waits, after asking the Hub to stop a job it gave up on, for the worker's own
#: answer. Music stops at once and narration within seconds; an image render ends with the render.
CANCEL_SETTLE_S = 90.0


def _stop_hub_work(client: httpx.Client, base: str, headers: dict[str, str], batch_id: str,
                   item_index: int, jobs_path: str, kind: str) -> dict[str, Any] | None:
    """DELETE the batch, then read the item until the worker has answered or the grace runs out.

    Returns `{"summary", "item"}` once the item is terminal (`cancelled`, `error` or `done` if it
    beat the cancel), or `None` when the Hub refused the cancel, stayed unreachable, or the worker
    had not answered within `CANCEL_SETTLE_S` -- the cases where the outcome is truly unknown.
    """
    cancel = _cancel_batch(base, headers, batch_id, kind)
    from .. import startup_control

    startup_control.audit("deadline_stop", controller=base, batch=batch_id,
                          requested_by="gateway", switch="deadline",
                          reason="The Gateway's running-time limit was reached.", result=cancel)
    if not cancel.get("ok"):
        return None
    deadline = time.monotonic() + CANCEL_SETTLE_S
    while True:
        try:
            response = client.get(f"{base}{jobs_path}/{batch_id}", headers=headers)
            if response.status_code < 400:
                summary = response.json() or {}
                items = summary.get("items") or []
                item = items[item_index] if item_index < len(items) else None
                state = str((item or {}).get("state") or "").lower()
                if state in _TERMINAL_OK or state in _TERMINAL_BAD:
                    startup_control.audit("deadline_stop_outcome", controller=base, batch=batch_id,
                                          requested_by="gateway", outcome=state)
                    return {"summary": summary, "item": item or {}}
                if state in _TERMINAL_UNCERTAIN:
                    return None
        except (httpx.HTTPError, ValueError):
            pass
        if time.monotonic() >= deadline:
            startup_control.audit("deadline_stop_outcome", controller=base, batch=batch_id,
                                  requested_by="gateway", outcome="not confirmed")
            return None
        time.sleep(POLL_INTERVAL_S)


#: Fetching a finished artifact: the file is still on the worker, so a transient error is worth
#: asking again rather than filing a hold for a job that has already succeeded.
ARTIFACT_TRIES = 3
ARTIFACT_RETRY_S = 2.0


def _fetch_artifact(client: httpx.Client, url: str, headers: dict[str, str],
                    timeout: httpx.Timeout) -> httpx.Response:
    for attempt in range(1, ARTIFACT_TRIES + 1):
        try:
            response = client.get(url, headers=headers, timeout=timeout)
            if response.status_code < 500 or attempt == ARTIFACT_TRIES:
                return response
        except httpx.HTTPError as exc:
            if attempt == ARTIFACT_TRIES:
                raise BackendError(
                    f"hub artifact fetch failed after {attempt} tries: {exc}",
                    code="PROVIDER_FAILED", upstream_accepted=True, retryable=True,
                ) from exc
        time.sleep(ARTIFACT_RETRY_S)
    raise AssertionError("unreachable")  # pragma: no cover


def _verify_digest(artifact: httpx.Response, stated: object = None) -> None:
    """Refuse bytes that do not match the digest the Hub states -- before the receipt says they do.

    The ack tells the Hub its copy is safe to clean up, so it must not go out for a file that
    arrived different. Nothing stated means nothing to compare, and that is not a failure.
    """
    expected = (str(artifact.headers.get("x-content-sha256") or "").strip()
                or str(stated or "").strip()).lower()
    if expected and hashlib.sha256(artifact.content).hexdigest() != expected:
        raise BackendError(
            "the artifact did not match the sha256 the Hub stated for it; not acknowledged",
            code="PROVIDER_FAILED", upstream_accepted=True,
        )


def _hold_base(base_url: str, local: bool) -> str:
    """Where to ask about a held batch, or `""` when nobody can be asked.

    This Mac's own Hub has no stored address ("discovered at runtime"), so it is looked up. A
    *remote* location with no address asks nobody: falling back to this Mac's Hub would answer 404
    for a batch it never saw, which reads as "never started" and would re-run work that may be
    rendering elsewhere.
    """
    from ..hub import hub_base_url

    return (str(base_url or "").strip() or (hub_base_url() if local else "")).rstrip("/")


def adopt_batch(request: BackendRequest, batch_id: str, *, local: bool = False) -> BackendResult:
    """Collect the result of a held batch the Hub reports **done** (owner decision, 2026-09-30).

    A hold is a job whose outcome the gateway lost track of; when the Hub's own record says the
    item finished, the rendered artifact is still there, so it is fetched, sha256-verified and
    returned exactly as a live job's would be -- through the same `_collect_*` functions, with the
    same delivery receipt for the worker to send after the bytes are saved. Raises `BackendError`
    if the Hub no longer says `done` or the bytes cannot be verified; the caller leaves the hold.
    """
    from ..hub import hub_headers

    base = _hold_base(request.base_url, local)
    if not base:
        raise BackendError("no address to collect the held batch from", code="PROVIDER_FAILED")
    headers = hub_headers(str(request.values.get("hub_token") or ""), local=local)
    transcription = request.modality == "transcription"
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        progress = HubProgress()
        outcome = _poll_batch(
            client, base, headers, batch_id, 0,
            jobs_path=_jobs_path(request.modality), deadline_seconds=30.0, progress=progress,
        )
        collect = _collect_transcription if transcription else _collect_batch
        return collect(client, base, headers, request, batch_id, outcome, progress)


def batch_snapshot(values: dict[str, Any], base_url: str, batch_id: str,
                   *, kind: str = "image", local: bool = False) -> dict[str, Any] | None:
    """Ask a Hub about one batch **once**, and say honestly which of three answers it gave.

    Returns:
      * `{"known": True, "state": ..., "item": ...}` — the Hub answered.
      * `{"known": False}` — the Hub answered `404`: it has never heard of this batch, so the work
        it was supposed to be running **never started**.
      * `None` — the Hub could not be reached or refused, so the question is still open.

    **Why this exists at all.** A hold is a job whose upstream outcome is unknown, and the gateway
    treated it as permanently unknown: it waited for a person, even when the Hub could simply be
    asked. The owner's answer (Q8) was *"close when the Hub's batch record says finished or never
    started"* — and both of those are answers the Hub gives in one request. The distinction
    between `known: False` and `None` is the whole safety of it: "the Hub says there is no such
    batch" is proof the work never ran, while "the Hub did not answer" is not, and only the first
    of those may ever lead to a re-run.
    """
    kind = str(kind or "image")
    jobs_path = _jobs_path(kind)
    from ..hub import hub_headers

    headers = hub_headers(str((values or {}).get("hub_token") or ""), local=local)
    base = _hold_base(base_url, local)
    if not base:
        return None
    url = f"{base}{jobs_path}/{batch_id}"
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=6.0)) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code == 404:
        return {"known": False}
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    items = body.get("items") or []
    item = items[0] if items and isinstance(items[0], dict) else None
    # The Hub publishes no batch-level `state` on /api/hub/jobs: the item carries it, and a
    # transcription batch's summary calls its own rollup `status`.
    state = (item or {}).get("state") or body.get("status") or ""
    return {"known": True, "state": str(state).lower(), "item": item, "summary": body}


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
#: The value is `("asset", asset_id, staged_at)`. Keyed by voice alone — which it was — a
#: reference staged on one location was handed to the next location that asked for the same
#: voice, and that location had never seen the asset. Invisible with a single
#: location, which is why it survived; with several it fails the moment a second
#: controller reuses a cached voice.
#: The third element is when the placement stops being reused (a deadline, not a stamp).
_REFERENCE_CACHE: dict[tuple[str, str, str], tuple[str, str, float]] = {}
#: How long one staged clip is reused: 3 days (owner, 2026-10-11; was 6 h).
_REFERENCE_TTL_S = 3 * 86400.0
#: Room left between the last reuse and the Hub's own expiry, for a job queued behind a busy Hub
#: (the adapter's queue ceiling is 3 h).
_REUSE_HEADROOM_S = 12 * 3600.0
#: How long the Hub is asked to keep a staged clip: the reuse window plus the headroom. **The Hub
#: may keep it for less** (Hub `execution_assets.MAX_TTL_SECONDS`, 48 h as of 0.6.166), so reuse
#: also ends `_REUSE_HEADROOM_S` before the `expires_at` the Hub answers (`_reuse_until`).
_STAGED_TTL_S = _REFERENCE_TTL_S + _REUSE_HEADROOM_S


def _reuse_until(staged_at: float, expires_at: object) -> float:
    """When a placement stops being reused: the reuse window, cut short by the Hub's own expiry."""
    until = staged_at + _REFERENCE_TTL_S
    if isinstance(expires_at, (int, float)):
        until = min(until, float(expires_at) - _REUSE_HEADROOM_S)
    return until


def _remember_reference(key: tuple[str, str, str], kind: str, value: str, until: float) -> None:
    """Store a placement and drop the ones that have expired.

    The key carries the clip's digest, so every re-import of a voice adds a key
    whose predecessors are never asked for again. Without this they would sit in
    the dict for the process's life.

    `ponytail:` a full sweep per write, which is a handful of entries per voice per
    location. Above a few thousand voices this wants a heap or an ordered dict.
    """
    now = time.time()
    for stale in [k for k, v in _REFERENCE_CACHE.items() if v[2] <= now]:
        _REFERENCE_CACHE.pop(stale, None)
    _REFERENCE_CACHE[key] = (kind, value, until)


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


def _voice_placement(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    reference: dict[str, Any],
) -> str:
    """The `voice_reference_asset_id` this location should clone from: the clip, staged there.

    **The clip travels with the job; a library id does not.** This used to send the
    controller's own `voice_library_id` whenever that controller's Voice Studio held a clip
    with the same sha256. A library id names an entry on *one* Voice Studio, and the Hub's
    broker sends an id it does not manage as a shared voice to any of its workers
    (`broker._shared_voice_allows_studio` returns "allowed" for an unknown id), including
    a node that has never heard of it. A staged reference is carried by the broker to
    whichever Mac runs the job (`/api/generate/txt2speech/reference`), checked against the
    same sha256 at every hop, so one placement serves every Mac of the location.

    `ponytail:` one upload of the clip per (voice, location, recording) per
    `_REFERENCE_TTL_S`; a voice library big enough for that to matter would want the Hub
    to sync gateway voices instead.
    """
    path = Path(str(reference.get("path") or ""))
    if not path.exists():
        raise BackendError(
            f"voice reference audio is missing for '{voice_id}'",
            code="VOICE_PROVIDER_RESULT_INVALID",
            upstream_accepted=False,
        )
    # Read before the cache, not after it: which recording this is *is* part of the
    # question the cache answers.
    # The words staged with the clip are part of the placement too: the Hub's asset identity
    # includes them, and keyed on the bytes alone a corrected transcript (restored, or segments
    # that arrived after the first staging) went unused until the reuse window ran out.
    words = json.dumps([reference.get("transcript") or "", reference.get("segments") or [],
                        reference.get("language") or ""], sort_keys=True)
    key = _reference_cache_key(
        voice_id, base, _clip_digest(path) + ":" + hashlib.sha256(words.encode()).hexdigest()[:16]
    )
    cached = _REFERENCE_CACHE.get(key)
    if cached and time.time() < cached[2]:
        return cached[1]
    staged_at = time.time()
    asset_id, expires_at = _stage_voice_reference(client, base, headers, voice_id, path, reference)
    _remember_reference(key, "asset", asset_id, _reuse_until(staged_at, expires_at))
    return asset_id


def segments_text(segments: Any) -> str:
    """The words of timestamped segments, in order — minus Whisper's looping tails.

    A segment whose words are mostly one word repeated ("with better stats stats stats …",
    seen on Todd's clip) is a transcription hallucination over silence, not speech, and a
    clone conditioned on it reads words the clip never says.
    """
    texts = []
    for segment in segments if isinstance(segments, list) else []:
        text = str((segment or {}).get("text") or "").strip() if isinstance(segment, dict) else ""
        words = text.lower().split()
        if len(words) > 8 and len(set(words)) * 4 < len(words):
            continue
        if text:
            texts.append(text)
    return " ".join(texts)


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
    # Off is the owner's choice for a language the transcriber cannot read: audio only, so
    # VoxCPM2 clones from the clip alone and a model that needs words refuses on its own.
    included = raw.get("include_reference_transcript") is not False
    segments = (raw.get("reference_segments") or []) if included else []
    return {
        "path": path,
        # A voice imported from a loose clip has no saved transcript, only the timestamped
        # segments its annotation produced. Breeze TTS 2 refuses a reference without its words
        # (the Hub derives them from segments only when it cuts a window, and Breeze publishes
        # no window), so the words come from the segments rather than the voice failing.
        "transcript": (str(raw.get("reference_transcript") or "").strip()
                       or segments_text(segments)) if included else "",
        "segments": segments,
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
            upstream_accepted=False,  # the clip is staged; no batch exists yet
        )


def _stage_voice_reference(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    voice_id: str,
    path: Path,
    reference: dict[str, Any],
) -> tuple[str, object]:
    """Upload a voice's reference clip; return the Hub's asset id and the `expires_at` it states.

    Qwen3 *Base* models are cloning models: without a reference they fail with
    "Voice cloning needs a reference voice or private reference upload". So the
    clip is staged first and passed as `voice_reference_asset_id`.

    Everything the route accepts is sent, and the answer is checked. The clip's
    language, its declared checksum and the durable source it came from are all
    part of what the Hub stores and fingerprints: sending only the audio left the
    staged copy a poorer description of the voice than the gateway already had.
    """
    from .. import voice_audio  # an audio utility, not the voice catalogue

    # **Lossless FLAC when it is provably lossless** (`voice_audio.lossless_flac`: decoded back
    # and compared sample for sample), else the file as it is. ~40% of the bytes for the same
    # samples: Christopher's 105 s clip stages as 3.67 MB instead of 9.30 MB. The Hub and its
    # Voice Studio accept `.flac` and decode it with libsndfile to the same samples as the WAV.
    flac = voice_audio.lossless_flac(path)
    audio = flac if flac is not None else path.read_bytes()
    name = path.with_suffix(".flac").name if flac is not None else path.name
    # One read feeds both the upload and the declared digest: hashing the file a
    # second time would let the two describe different bytes.
    digest = hashlib.sha256(audio).hexdigest()
    files = {"audio": (name, audio, "application/octet-stream")}
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
        # A label for the Hub's job rows ("which voice spoke this"); a Hub older than 0.6.197
        # ignores the field. Not part of the staged asset's identity.
        "display_name": str(reference.get("name") or "")[:200],
        # Outlives the placement cache by `_REUSE_HEADROOM_S`: an asset reused just before
        # the cache lets go still has to be readable when a busy Hub finally dispatches
        # the job (the adapter's queue ceiling is 3 h).
        "ttl_seconds": str(int(_STAGED_TTL_S)),
    }
    try:
        response = client.post(
            f"{base}/api/hub/voice-references", headers=headers, files=files, data=data
        )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise BackendError(
            f"could not reach the Hub to stage the voice reference ({type(exc).__name__})",
            code="LOCATION_UNREACHABLE",
            retryable=True,
            upstream_accepted=False,
        ) from exc
    except httpx.HTTPError as exc:
        # Staging a clip is not a job: no batch exists yet, so whatever went wrong here cannot
        # have started work, and the job must not be filed as an unknown outcome.
        raise BackendError(
            f"voice reference upload failed: {exc}",
            code="SITE_CAPACITY_UNAVAILABLE",
            retryable=True,
            upstream_accepted=False,
        ) from exc

    if response.status_code >= 400:
        raise BackendError(
            f"hub voice-reference HTTP {response.status_code}: {response.text[:200]}",
            code="VOICE_PROVIDER_RESULT_INVALID",
            status=response.status_code,
            retryable=response.status_code >= 500,
            upstream_accepted=False,
        )

    payload = response.json() or {}
    asset_id = str(payload.get("asset_id") or "")
    if not asset_id:
        raise BackendError(
            "hub returned no voice reference asset_id", code="VOICE_PROVIDER_RESULT_INVALID",
            upstream_accepted=False,
        )
    _verify_staged_reference(
        payload,
        digest=digest,
        size=len(audio),
        transcript=bool(transcript.strip() or segments),
    )

    # Caching happens in `_voice_placement`, which is the only caller; it needs the expiry too.
    return asset_id, payload.get("expires_at")


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
            "'params' must be an object of Hub parameters.", code="INVALID_PARAMS",
            upstream_accepted=False,
        )
    item: dict[str, Any] = {"params": dict(supplied or {})}
    if request.modality in {"image", "music", "video"}:
        # All three address the worker by `prompt`; Music then takes its own engine
        # parameters and no image geometry, and Video takes its geometry plus, for the
        # image-to-video path, a reference image the broker turns into multipart.
        item["prompt"] = str(request.params.get("prompt") or "")
        if request.modality == "image":
            keys = ("seed", "width", "height", "aspect_ratio", "resolution", "output_format")
        elif request.modality == "video":
            keys = _HUB_VIDEO_PARAMS
        else:
            keys = _HUB_MUSIC_PARAMS
        for key in keys:
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]
        if request.modality == "music":
            item["params"] = _music_params(request.params)
        if request.modality == "video":
            # `duration_seconds` is the lane's published name and the name Story sends; the
            # broker forwards `duration`, so both travel and the worker's validator resolves
            # them (and refuses if a caller ever sends a disagreeing pair).
            if request.params.get("duration_seconds") is not None:
                item["params"]["duration"] = request.params["duration_seconds"]
            reference = _video_reference(request)
            if reference is not None:
                # **Inside `params`, not beside it.** The Hub builds an item from exactly three
                # keys — `prompt`, `seed` and `params` (`broker._submit_batch_locked`) — and then
                # merges `shared_params` with `item["params"]` before popping `reference_images`
                # off the *body*. An item-level `reference_images` is dropped without a word, the
                # broker takes its JSON door instead of its multipart one, and the worker receives
                # a prompt-only job: measured 2026-09-26, the first chain job generated
                # text-to-video from a request that had uploaded a source image.
                item["params"]["reference_images"] = reference
        elif request.modality == "image":
            # The same rule, the same reason, for "Reference to Image": the broker builds an item
            # from `prompt`, `seed` and `params` alone and pops `reference_images` off the *body*,
            # so a reference beside `params` is dropped without a word and the worker renders the
            # prompt with no characters in it. One loss here is a wrong picture, not an error.
            reference = _image_reference(request)
            if reference is not None:
                item["params"]["reference_images"] = reference
    else:
        item["text"] = str(request.params.get("text") or "")
        for key in ("speed", "voice_id", "language", "temperature", "seed", "output_format",
                    # Delivery / emotion (owner, 2026-10-29): the Hub reads it as the engine's
                    # own instruction on `/api/generate/txt2speech` and on
                    # `/api/generate/txt2speech/reference` — the same field, so both the plain
                    # and the cloned path carry it from this one list. Whether a model takes it
                    # is decided by its own published capability, and refused before this runs.
                    "delivery_instruction",
                    # Join pauses (Hub-validated, 0-3 s); a node that predates them drops them.
                    "paragraph_pause_sec", "sentence_pause_sec"):
            if request.params.get(key) is not None:
                item["params"][key] = request.params[key]
        # **A control is a parameter, not a nested object.** Story sends the controls the catalogue
        # published as `controls: {name: value}`; the Hub merges `item.params` into the worker's
        # request body, whose model reads `temperature` (and its siblings) at the top level and
        # drops an unknown `controls` key without a word -- so a value the caller chose reached no
        # engine at all (audit 2026-10-01). An explicit top-level parameter still wins.
        controls = request.params.get("controls")
        if isinstance(controls, dict):
            for name, value in controls.items():
                if value is not None and isinstance(name, str) and name not in item["params"]:
                    item["params"][name] = value

    body = {
        "modality": modality,
        "model": request.model_id,
        "items": [item],
        "clientRequestId": str(request.params.get("_client_request_id") or request.params.get("_job_id") or ""),
        "label": "gateway-kh",
    }
    if request.modality == "image" and request.params.get("_pool_id"):
        # Image-pool work runs only on a Mac assigned this model (`image_assignment`); the Hub
        # enforces it. A job that names its model keeps the Hub's ordinary placement.
        body["routing"] = "assigned"

    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        if request.modality == "tts":
            voice_id = str(request.params.get("voice_id") or "")
            reference = _reference_for(request)
            if reference is not None:
                body["voice_reference_asset_id"] = _voice_placement(
                    client, base, headers, voice_id, reference
                )
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
                f"hub submit failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True,
                recovery=_submit_unconfirmed(base, exc),
            ) from exc

        if response.status_code >= 400:
            code = _hub_refusal_code(response)
            if _is_location_refusal(code, response):
                # A site property, not a job failure: the controller cannot serve
                # this model right now, and it accepted nothing. `upstream_accepted`
                # is therefore False, so the worker may safely try the next
                # location that offers the same model instead of calling the job
                # failed (or, worse, holding it as an unknown outcome).
                raise _refusal_error(code, response)
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

        progress = HubProgress()
        try:
            outcome = _poll_batch(
                client, base, headers, batch_id, 0,
                deadline_seconds=lane_deadline_seconds(request),
                progress=progress,
                on_wait=request.on_wait,
                stranded=_stranded(request),
                deadline_for_machine=lambda machine: lane_deadline_seconds(request, machine),
            )
        except BackendError as exc:
            exc.evidence = progress.evidence(batch_id)
            raise
        return _collect_batch(client, base, headers, request, batch_id, outcome, progress)


#: The `output_format` vocabulary for a delivered media type (the names the callers use).
_FORMAT_BY_MEDIA = {
    "image/png": "png", "image/jpeg": "jpeg",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/flac": "flac", "audio/x-flac": "flac",
}


def _audio_seconds(terminal: dict[str, Any]) -> float | None:
    """The Hub's measured audio length in seconds, from `audio_duration_s` or `audio_duration_ms`."""
    for key, scale in (("audio_duration_s", 1.0), ("audio_duration_ms", 1000.0)):
        value = terminal.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value) / scale
    return None


def _collect_batch(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    request: BackendRequest,
    batch_id: str,
    outcome: dict[str, Any],
    progress: HubProgress,
) -> BackendResult:
    """Fetch, verify and describe the artifact of an item the Hub has finished.

    The one place a finished image/voice/music/video item becomes a result -- both a job that
    polled its batch to `done` and a hold **adopted** later (see `adopt_batch`) come through here,
    so an adopted result is exactly a normal one and there is no second implementation to drift.
    """
    terminal = (outcome.get("item") or {}).get("terminal_result") or {}
    item = outcome.get("item") or {}
    # **The anti-silent-drop proof, asked before the wrong picture is downloaded.** A reference-guided
    # image must come back as one: the Hub states how many references the worker actually attached,
    # and a job that sent three and applied two finished on a picture of the wrong people with an
    # envelope identical to a success. Unstated is also refused here — a Hub that advertises `edit`
    # and cannot say what it applied has not shown that the references were used. This is the last
    # of the three independent refusals that exist so no single regression can turn a reference
    # render into a text-to-image one (Story's preflight, the gateway's asset resolution, and this).
    sent_references = request.params.get("_reference_digests")
    sent_count = len(sent_references) if isinstance(sent_references, list) else 0
    applied_count = _reference_count(item, terminal)
    if sent_count:
        if applied_count is None:
            raise BackendError(
                f"the Hub finished this reference-guided image without stating how many of the "
                f"{sent_count} reference images it applied; the result cannot be shown to be the "
                "one that was asked for",
                code="PROVIDER_RESULT_INVALID", upstream_accepted=True, upstream_settled=True,
            )
        if applied_count != sent_count:
            raise BackendError(
                f"{sent_count} reference images were sent and the Hub applied {applied_count}; "
                "that is a different render from the one asked for, not a successful image",
                code="PROVIDER_RESULT_INVALID", upstream_accepted=True, upstream_settled=True,
            )

    artifact = _fetch_artifact(
        client, _artifact_url(base, batch_id, 0, request.modality), headers,
        httpx.Timeout(600.0, connect=10.0),
    )
    if artifact.status_code >= 400:
        raise BackendError(
            f"hub artifact HTTP {artifact.status_code}",
            code="PROVIDER_FAILED",
            status=artifact.status_code,
            retryable=artifact.status_code >= 500,
        )

    _verify_digest(artifact, terminal.get("sha256"))
    media_type = artifact.headers.get("content-type", "").split(";")[0].strip()
    if not media_type:
        # Music comes back as WAV like voice, Image as PNG, and a local video as MP4 — the
        # artifact route always states its type, so this only covers a Hub that answered
        # without one.
        media_type = {"image": "image/png", "video": "video/mp4"}.get(
            request.modality, "audio/wav")

    payload: dict[str, Any] = {
        "hub_batch_id": batch_id,
        "hub_machine": terminal.get("machine_id"),
        "hub_worker": terminal.get("worker_id"),
        "resolved_seed": terminal.get("resolved_seed"),
        "sha256": terminal.get("sha256"),
        "bytes": terminal.get("bytes"),
        # How many reference images the Hub says the worker applied — `None` on every job that sent
        # none, and on a Hub that does not state it. Kept on the job record so a customer asking
        # "was this rendered from my characters" has the answer beside the artifact.
        "reference_count": applied_count,
        # **The Hub's own runtime for this item, and the reason image and voice jobs had
        # no timing at all.** `terminal_result` has carried `runtime_s` the whole time —
        # the sibling `machine_id`, `bytes`, `sha256` and `resolved_seed` above are read
        # from the same dict — and this gateway simply never copied it. Chat has always
        # recorded its figure (from `elapsed_seconds`, below), so the generation record
        # measured a rate for chat and nothing for the lanes that do the real work:
        # image, voice, music and video. Same key and same meaning as chat's, because a
        # reader asking "how long did this take" should not have to know which lane
        # answered. Absent stays absent: `None` is "the Hub stated no runtime", which is
        # a different fact from zero and must not render as one.
        "generation_seconds": terminal.get("runtime_s"),
        # The decoded length of the audio the Hub measured (voice and music), kept so the daily
        # summary can total minutes for m4a/flac too. `None` when the Hub stated none.
        "audio_duration_s": _audio_seconds(terminal),
        # **How the job went, not just that it finished** — `None` when the Hub
        # reported no progress figure, which is a different answer from "it never
        # stalled". See `HubProgress`.
        "hub_progress": progress.as_payload(),
        # The Hub's per-section TTS quality check (0.6.137+), kept on the batch item itself rather
        # than in `terminal_result`. `None` when the Hub stated none (older Hubs, other lanes).
        "section_quality": (outcome.get("item") or {}).get("section_quality"),
        # **What the caller asked for and what the Hub delivered**, so a Jobs view can tell a node that
        # honoured `output_format` from one that predates it and returned the native file. Never a
        # failure either way: the artifact is stored and served as the Hub described it.
        "output_format_requested": request.params.get("output_format"),
        "output_format_applied": _FORMAT_BY_MEDIA.get(media_type),
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

    audio_path = staged_path(request.params.get("_upload_path"))
    if audio_path is None or not audio_path.exists():
        raise BackendError("transcription needs an uploaded audio file", code="PROVIDER_FAILED",
                           upstream_accepted=False)
    audio = audio_path.read_bytes()

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
    # The Hub dedupes on (project, episode, model, item_id, filename) while a batch is active, so a
    # constant id made two same-named uploads share one batch and one SRT. The job id is unique.
    data: dict[str, str] = {
        "model": request.model_id,
        "item_ids": str(request.params.get("_job_id") or "0"),
    }
    if request.params.get("language"):
        data["language"] = str(request.params["language"])
    if request.params.get("word_timestamps") is not None:
        data["word_timestamps"] = "true" if request.params["word_timestamps"] else "false"

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
                recovery=_submit_unconfirmed(base, exc),
            ) from exc

        if response.status_code >= 400:
            code = _hub_refusal_code(response)
            if _is_location_refusal(code, response):
                raise _refusal_error(code, response)
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
        if submitted.get("duplicate"):
            log.warning("hub transcription batch %s was already active for this upload "
                        "(duplicate: true)", batch_id)
        if request.checkpoint is not None:
            request.checkpoint(batch_id)

        progress = HubProgress()
        outcome = _poll_batch(
            client, base, headers, batch_id, 0,
            jobs_path="/api/hub/transcription/jobs",
            deadline_seconds=lane_deadline_seconds(request),
            progress=progress,
            on_wait=request.on_wait,
            stranded=_stranded(request),
        )
        return _collect_transcription(client, base, headers, request, batch_id, outcome, progress)



def _collect_transcription(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    request: BackendRequest,
    batch_id: str,
    outcome: dict[str, Any],
    progress: HubProgress,
) -> BackendResult:
    """The transcription twin of `_collect_batch`: one path for a live job and an adopted hold."""
    artifact = _fetch_artifact(
        client, _artifact_url(base, batch_id, 0, "transcription") + "?format=srt", headers,
        httpx.Timeout(300.0, connect=10.0),
    )
    if artifact.status_code >= 400:
        raise BackendError(
            f"hub transcript artifact HTTP {artifact.status_code}",
            code="PROVIDER_FAILED",
            status=artifact.status_code,
        )
    _verify_digest(artifact)

    item = outcome.get("item") or {}
    studio = str(item.get("studio") or "")
    payload: dict[str, Any] = {
        "hub_batch_id": batch_id,
        # Sibling of the image lanes' `runtime_s`/`machine_id`, read from the item the Hub polled:
        # its real elapsed seconds, and the machine after the `@` in `studio` ("name@machine").
        "generation_seconds": item.get("duration_seconds"),
        "hub_machine": studio.split("@", 1)[1] if "@" in studio else None,
        # Same record as the image and voice lanes: `None` when the Hub reported no
        # progress figure, never a reassuring `false` we invented from silence.
        "hub_progress": progress.as_payload(),
    }

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


def _chat_body(request: BackendRequest, notes: dict[str, Any] | None = None) -> dict[str, Any]:
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
    # The caller's own answer length, under every spelling it may use
    # (`base.caller_max_output_tokens`) — this lane read all three by hand while the other
    # two read a subset, which is how the same request came to be answered differently
    # depending on which lane served it.
    #
    # **Never more than this lane will accept** (`base.bounded_max_output_tokens`): the Hub's
    # contract states a `max_tokens.maximum` and refuses a larger value by name after measuring
    # the prompt, so a number above the published allowance is lowered here and reported on the
    # job rather than sent to earn that refusal.
    max_tokens, lowered = bounded_max_output_tokens(
        request, caller_max_output_tokens(request.params))
    if max_tokens:
        body["max_tokens"] = max_tokens
    if lowered is not None and notes is not None:
        notes["max_tokens_lowered_to"] = lowered["sent"]
        notes["max_tokens_asked"] = lowered["asked"]
        notes["max_tokens_lowered_reason"] = lowered["why"]
    response_format = request.params.get("response_format")
    # **Exactly the two the lane publishes.** The Hub's contract states
    # `structured_output.formats: ["json_object", "json_schema"]` and it refuses
    # anything else with 422 — measured 2026-09-25: **fourteen** jobs settled
    # `failed` on `response_format.type: must be json_object or json_schema;
    # received str 'text'`, and this allowlist carried `"text"`.
    #
    # `text` is OpenAI's spelling for "unstructured, the default", and dropping it
    # is equivalent on this lane **as it stands today**: the chat worker resolves
    # `body.get("response_format", defaults.get("response_format"))`, `response_format`
    # carries no `default` on the lane's parameter surface, and nothing has stored
    # one — so omitted means no format is requested, which is what `text` means.
    # That is this adapter's own rule: send only the parameters the lane honours,
    # because "sending one would be a promise made to the caller that the answer
    # does not keep".
    #
    # **The caveat, recorded rather than discovered later.** `response_format` *is*
    # on that parameter surface, so an operator can store one (Models, per model per
    # machine). If one is ever stored, omitting stops meaning "plain text" — the
    # caller's explicit `text` would silently become that Mac's default — and the
    # honest behaviour then is to **refuse** it, because the lane's validation
    # accepts only `json_object`/`json_schema` and has no way to ask for plain text
    # against a stored default. The check to add is "does this row publish a
    # `response_format` default"; it is not needed while none is set, and this note
    # is what makes it cheap when one is.
    if isinstance(response_format, dict) and response_format.get("type") in {
        "json_object", "json_schema",
    }:
        body["response_format"] = response_format
    return body


def chat_queue_wait_seconds(provider_id: str, model_id: str = "") -> float:
    """A new Hub's bounded *local* queue contract; old/cloud sites get no allowance."""
    from .. import fleet
    if not provider_id.startswith(fleet.PROVIDER_PREFIX) or fleet.is_paused_provider(provider_id):
        return 0.0
    try:
        row = next((row for row in fleet.snapshot(allow_stale=True).get("locations") or []
                    if row.get("provider_id") == provider_id and row.get("reachable") is True), {})
        queue = ((row.get("capacity") or {}).get("by_operation") or {}).get("chat.completion") or {}
        slots, wait = queue.get("pending_request_slots"), queue.get("pending_queue_wait_seconds")
        models = queue.get("pending_request_models")
        if (isinstance(slots, bool) or slots != 1 or isinstance(wait, bool)
                or not isinstance(wait, (int, float)) or not 0 < wait <= queue_wait_ceiling_s()
                or not isinstance(models, list) or (model_id and model_id not in models)):
            return 0.0
        return float(wait)
    except (TypeError, ValueError, KeyError):
        return 0.0


def _queued_chat_post(request: BackendRequest, base: str, headers: dict, body: dict,
                      queue_wait: float, timeout: httpx.Timeout, *,
                      id_salt: str = "") -> httpx.Response:
    """Keep the completion intact while publishing only the request's queue phase.

    `id_salt` re-keys the request id for a **re-send**. The Hub's queue registers an id the
    moment it accepts a queued completion (`chat_priority.register`) and marks it `done` when
    the door refuses it, so a second POST under the same id is answered `409
    CHAT_REQUEST_DUPLICATE` — the re-send would be turned away as a replay of work that never
    started. One salt per attempt, so each send is its own id.
    """
    request_id = hashlib.sha256(
        ((id_salt or "") + str(request.params.get("_client_request_id")
                               or request.params.get("_job_id") or time.time_ns())
         ).encode()).hexdigest()[:32]
    headers = {**headers, "X-StudioHub-Request-ID": request_id,
               "X-StudioHub-Queue-Wait-Seconds": str(queue_wait)}
    def report_wait(value):
        if request.on_wait:
            try:
                request.on_wait(value)
            except Exception:
                log.exception("could not publish Chat queue phase")
    report_wait({"reason": "waiting for a free Mac", "since": time.time(), "next_try_at": None})
    if request.checkpoint:
        request.checkpoint(f"chat:{request_id}")
    done = threading.Event()
    outcome: list = []

    def post():
        try:
            outcome.append(httpx.post(f"{base}/studio/chat/v1/chat/completions",
                                      headers=headers, json=body, timeout=timeout))
        except BaseException as exc:
            outcome.append(exc)
        finally:
            done.set()

    # The request counter is thread-local. The job's own thread must record the
    # handoff so an immediate socket failure never reads as safe to retry.
    note_request_started()
    threading.Thread(target=post, name="gateway-chat-request", daemon=True).start()
    try:
        while not done.wait(POLL_INTERVAL_S):
            try:
                status = httpx.get(f"{base}/studio/chat/requests/{request_id}",
                                   headers=_headers(request), timeout=httpx.Timeout(2, connect=1))
                if status.status_code == 200 and status.json().get("state") != "queued" and request.on_wait:
                    report_wait(None)
            except (httpx.HTTPError, ValueError, TypeError):
                pass  # A status read never overrides the completion or causes another POST.
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]
    finally:
        report_wait(None)


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
    # The caller's own expectation travels with the request, because the Hub cannot tell a
    # 100-prompt scene pack from a one-line question by reading the body — and the Hub is the
    # party that sizes the job's deadline from it (`base.expected_output_header`).
    headers = {**_headers(request), "Content-Type": "application/json",
               **expected_output_header(request.params)}
    # The lane's own ceiling applies to the call itself, as it does to a poll: the
    # Hub holds this request open while the model runs.
    queue_wait = chat_queue_wait_seconds(request.provider_id, request.model_id)
    timeout = httpx.Timeout(lane_deadline_seconds(request) + queue_wait + (10 if queue_wait else 0), connect=10.0)
    # Built once, so the request actually sent and the note reported on the job can never
    # describe two different allowances (`base.bounded_max_output_tokens`).
    lowered: dict[str, Any] = {}
    body = _chat_body(request, lowered)
    def post(payload: dict[str, Any], attempt: int = 0) -> httpx.Response:
        # One salt per attempt: the Hub queues an id at the door and marks it used even when it
        # then refuses, so a re-send under the old id would be turned away as a duplicate.
        return (_queued_chat_post(request, base, headers, payload, queue_wait, timeout,
                                  id_salt=f":{attempt}")
                if queue_wait else httpx.post(f"{base}/studio/chat/v1/chat/completions",
                                             headers=headers, json=payload, timeout=timeout))

    try:
        response = post(body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Nothing accepted the connection: an unreachable location, not a job
        # failure, so the worker may try the next site that offers the model.
        raise BackendError(
            f"could not reach the Hub at {base} ({type(exc).__name__})",
            code="LOCATION_UNREACHABLE",
            retryable=True,
            upstream_accepted=False,
            upstream_settled=bool(queue_wait),
        ) from exc
    except httpx.HTTPError as exc:
        # A read timeout *after* the request left is ambiguous: the model may have
        # run and been recorded as a job on that node with nobody to collect it.
        raise BackendError(
            f"hub chat failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True,
            # A dropped connection mid-chat is the location going silent, and a chat re-run is
            # free. A read timeout is not: the model may just be slow, and the hold stays.
            recovery=None if isinstance(exc, httpx.TimeoutException)
            else {"location": base, "reason": str(exc), "lost_contact": True},
        ) from exc

    if response.status_code >= 400:
        code = _hub_refusal_code(response)
        if code in {"CHAT_REQUEST_CANCELLED", "CHAT_QUEUE_EXPIRED", "CHAT_REQUEST_DUPLICATE"}:
            raise BackendError(_refusal_sentence(code, response), code=code,
                               status=response.status_code, retryable=False,
                               upstream_accepted=code == "CHAT_REQUEST_DUPLICATE",
                               upstream_settled=code != "CHAT_REQUEST_DUPLICATE")
        if code in _LOCATION_REFUSAL_CODES:
            error = _refusal_error(code, response)
            error.upstream_settled = bool(queue_wait)
            raise error
        if code == "MACHINE_DISABLED":
            # The peer chat route runs on the controller's own Mac, so a paused controller answers
            # 409 MACHINE_DISABLED even when its group has other Macs. For this free job that is a
            # hand-back, not an operator verdict on the job: moved to another group, nothing
            # charged, not counted against the retry ceiling (owner rule 2026-10-04).
            raise BackendError(_refusal_sentence(code, response), code=NO_AVAILABLE_MACHINE,
                               status=response.status_code, retryable=True, upstream_accepted=False,
                               upstream_settled=bool(queue_wait))
        named_a_figure = _HUB_NAMED_FIGURE_REFUSAL in _hub_detail(response)
        if response.status_code == 422 and named_a_figure:
            # **The answer does not fit beside the prompt — but the prompt itself does.** The Hub
            # measures the prompt and refuses a *named* figure that does not fit beside it, having
            # produced nothing (`check_prompt_fits` runs before the first token). So the ask can be
            # resized from the Hub's own counts and sent once more; the prompt-only refusal is a
            # different sentence and is never re-sent, and if the room left is too thin to answer
            # in this is a refusal rather than a smaller ask.
            counts = _room_left_after_prompt(response)
            if counts is None:
                # The Hub named no window to resize against, so no figure may be invented: the
                # field is dropped and the Hub picks the room left for itself.
                resent: dict[str, Any] | None = {k: v for k, v in body.items()
                                                 if k != "max_tokens"}
                counts = {}
            else:
                resent = _room_left_resend(body, counts)
            if resent is None:
                raise BackendError(
                    f"the prompt leaves too little room for an answer: the Hub measured "
                    f"{counts['prompt']} prompt tokens in a {counts['window']}-token window, "
                    f"which is under {_MIN_ANSWER_TOKENS} answer tokens after its "
                    f"{_ROOM_LEFT_MARGIN_TOKENS}-token margin. Shorten the request.",
                    code="PROVIDER_FAILED", status=422, retryable=False,
                    upstream_accepted=False, upstream_settled=bool(queue_wait))
            try:
                response = post(resent, attempt=1)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise BackendError(
                    f"could not reach the Hub at {base} ({type(exc).__name__})",
                    code="LOCATION_UNREACHABLE", retryable=True,
                    upstream_accepted=False,
                    upstream_settled=bool(queue_wait)) from exc
            except httpx.HTTPError as exc:
                raise BackendError(
                    f"hub chat failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE", retryable=True,
                    recovery=None if isinstance(exc, httpx.TimeoutException)
                    else {"location": base, "reason": str(exc), "lost_contact": True}) from exc
            # **What was sent instead, said on the job** (`payload.update(lowered)` below).
            # `max_tokens_asked` stays the caller's own figure when the lane already lowered it;
            # only the room-left resize overwrites the reason, because it is a second and later
            # decision about the same field.
            asked = lowered.get("max_tokens_asked")
            if asked is None:
                asked = body.get("max_tokens", resent.get("max_tokens"))
            lowered["max_tokens_asked"] = asked
            if "max_tokens" in resent:
                lowered["max_tokens_lowered_to"] = resent["max_tokens"]
                lowered["max_tokens_lowered_reason"] = (
                    "above the room left after the prompt")
            else:
                lowered.pop("max_tokens_lowered_to", None)
                lowered["max_tokens_lowered_reason"] = (
                    "the Hub's sentence named no figure, so max_tokens was omitted and the "
                    "Hub used the room left")
            if counts:
                lowered["prompt_tokens_measured"] = counts["prompt"]
        # A successful re-send leaves `response` at 200: fall out of the refusal block and take
        # the answer below, rather than quoting a refusal that no longer happened.
        if response.status_code >= 400:
            raise BackendError(
                # A 422 here is the Hub's own validation — a prompt past the model's
                # stated context, or a non-boolean `thinking` — and its sentence names
                # the field and the measured count, so it is quoted, not replaced.
                f"hub chat HTTP {response.status_code}: {response.text[:200]}",
                code="PROVIDER_FAILED",
                status=response.status_code,
                retryable=response.status_code >= 500,
            # A refusal *by name* (STUDIO_NOT_RUNNING, OUTPUT_STORAGE_BLOCKED ...) happened at
            # the door, before any work, whatever its status; only a bare 5xx is ambiguous.
                upstream_accepted=None if response.status_code >= 500 and not code else False,
                upstream_settled=bool(queue_wait) and (response.status_code < 500 or bool(code)),
            )

    payload = answer_json(response, request.provider_id)
    # **What was actually sent, when it is not what the caller asked for.** Additive, and only on
    # the requests that needed it — a lowered allowance is part of the answer's provenance, not a
    # failure (see `base.bounded_max_output_tokens`).
    if lowered:
        payload.update(lowered)
    choices = payload.get("choices") or []
    if not choices:
        raise unusable_answer(request.provider_id, "it has no choices")
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    text = text if isinstance(text, str) else ""
    if not text.strip():
        # The Hub has its own code for this (`CHAT_EMPTY_ANSWER`), so an empty
        # answer is a failure here too rather than a successful blank job. The
        # Hub answered, so it is a *settled* failure: `upstream_settled` keeps it
        # out of the unknown-outcome holds, which have no attempt to aim at.
        raise unusable_answer(request.provider_id, "it is empty")
    # **A cut-off answer is a failure on this lane, exactly as it is on the cloud lanes**
    # (`openai_compat`, `anthropic`). Measured live 2026-11-02: job 04a617c2 asked for 64 output
    # tokens on gemma-4 and settled `succeeded` with `finish_reason: length` — a truncated answer
    # recorded as a clean success, and only the finish reason said so. Story already refuses one
    # at its own edge (`genstudio-client.ts:3018`), so settling it here as success let the
    # truncation reach the caller as finished work.
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        error = BackendError(
            f"{request.provider_id}: answer stopped early (length)",
            code="INCOMPLETE_OUTPUT", retryable=True,
            upstream_accepted=True, upstream_settled=True,
        )
        error.evidence = {
            "finish_reason": finish_reason,
            # The partial text travels with the verdict: a caller that can salvage a cut answer
            # should not have to re-run a job whose text was already produced and paid for.
            "partial_response": {
                "text": text,
                "finish_reason": finish_reason,
                "usage": payload.get("usage"),
                "model": payload.get("model") or request.model_id,
                **lowered,
            },
        }
        raise error
    require_json_answer(request, text, finish_reason)
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
            # The Mac that answered (`local`, or the node a controller relayed it to), as every
            # other Hub lane records it. Absent from a Hub that does not state it.
            "hub_machine": response.headers.get("X-StudioHub-Machine"),
            # **What was actually sent, when it is not what the caller asked for.** A lowered
            # answer allowance is part of the answer's provenance, not a failure (see
            # `base.bounded_max_output_tokens`), and the job's result is where a caller can see it.
            **lowered,
        },
    )


#: One batch of short texts takes well under a second; this only bounds a Mac that stopped answering.
EMBEDDINGS_TIMEOUT_S = 60.0


def _generate_embeddings(request: BackendRequest) -> BackendResult:
    """One embeddings batch on a Hub location's Embeddings Studio (Hub 0.6.165+).

    `POST {base}/studio/embeddings/v1/embeddings`, the peer route: the Hub admits it like any other
    work (paused Mac, Lite Mode, model readiness) but holds no machine lease, so it runs beside a
    narration instead of behind it. Synchronous, so the answer is the result: a vector per input.
    """
    base = _resolve_base(request)
    headers = {**_headers(request), "Content-Type": "application/json"}
    texts = request.params.get("input")
    try:
        response = httpx.post(f"{base}/studio/embeddings/v1/embeddings", headers=headers,
                              json={"model": request.model_id, "input": texts},
                              timeout=httpx.Timeout(EMBEDDINGS_TIMEOUT_S, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise BackendError(f"could not reach the Hub at {base} ({type(exc).__name__})",
                           code="LOCATION_UNREACHABLE", retryable=True,
                           upstream_accepted=False) from exc
    except httpx.HTTPError as exc:
        raise BackendError(f"hub embeddings failed: {exc}", code="SITE_CAPACITY_UNAVAILABLE",
                           retryable=True) from exc
    if response.status_code >= 400:
        code = _hub_refusal_code(response)
        if response.status_code == 422:
            raise BackendError(f"the Hub refused the input: {response.text[:300]}",
                               code="INVALID_REQUEST", status=422, retryable=False,
                               upstream_accepted=False)
        # Any other refusal, or a 404 from a Hub older than the lane: try another location.
        raise BackendError(_refusal_sentence(code, response) if code else
                           f"hub embeddings HTTP {response.status_code}: {response.text[:200]}",
                           code="LOCATION_NOT_READY", status=response.status_code,
                           retryable=True, upstream_accepted=False)
    try:
        payload = response.json()
        vectors = [row["embedding"] for row in sorted(payload["data"], key=lambda row: row["index"])]
    except (ValueError, KeyError, TypeError) as exc:
        raise BackendError("the Hub returned an unreadable embeddings answer",
                           code="PROVIDER_FAILED", retryable=True) from exc
    if len(vectors) != len(texts) or not all(isinstance(vector, list) and vector for vector in vectors):
        raise BackendError(f"the Hub returned {len(vectors)} vectors for {len(texts)} inputs",
                           code="PROVIDER_FAILED", retryable=True)
    return BackendResult(payload={"vectors": vectors, "usage": payload.get("usage") or {}})


#: An analysis takes seconds; this only bounds a Mac that stopped answering.
VISION_TIMEOUT_S = 90.0


def _generate_vision(request: BackendRequest) -> BackendResult:
    """One image analysis on one Mac of a Hub location (Hub 0.6.166+, engine `vision-mlx`).

    `params`: `machine` (the Hub's name for the Mac, `local` = the controller's own) and `body` (the
    Hub request). The controller's own Mac answers on the peer route `/studio/vision/v1/vision/analyze`,
    a joined Mac through its controller's relay `/api/hub/fleet/nodes/{machine}/vision/analyze`.
    Synchronous and admitted like embeddings (no lease). A 422 is the caller's; anything else is this
    Mac's, so the caller may try another.
    """
    base = _resolve_base(request)
    machine = str(request.params.get("machine") or "local")
    path = ("/studio/vision/v1/vision/analyze" if machine == "local"
            else f"/api/hub/fleet/nodes/{quote(machine, safe='')}/vision/analyze")
    body = json.dumps(request.params["body"]).encode()
    try:
        response = httpx.post(base + path, content=body,
                              headers={**_headers(request), "Content-Type": "application/json"},
                              timeout=httpx.Timeout(VISION_TIMEOUT_S, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise BackendError(f"could not be reached ({type(exc).__name__})", code="LOCATION_UNREACHABLE",
                           retryable=True, upstream_accepted=False) from exc
    except httpx.HTTPError as exc:
        raise BackendError(f"did not answer in time ({type(exc).__name__})",
                           code="SITE_CAPACITY_UNAVAILABLE", retryable=True) from exc
    if response.status_code == 422:
        raise BackendError("the Hub refused the request: "
                           + (_hub_refusal_reason(response) or response.text[:300]),
                           code="INVALID_REQUEST", status=422, retryable=False, upstream_accepted=False)
    if response.status_code in (404, 405) and not _hub_refusal_code(response):
        raise BackendError("its Studio Hub has no vision route (needs 0.6.166 or newer) or does not "
                           f"know the model (HTTP {response.status_code})", code="LOCATION_NOT_READY",
                           status=response.status_code, retryable=True, upstream_accepted=False)
    if response.status_code >= 400:
        code = _hub_refusal_code(response)
        raise BackendError(_refusal_sentence(code, response) if code else
                           f"refused it (HTTP {response.status_code}): {response.text[:200]}",
                           code="LOCATION_NOT_READY", status=response.status_code,
                           retryable=True, upstream_accepted=False)
    try:
        answer = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise BackendError("the Hub returned an unreadable analysis", code="PROVIDER_FAILED", retryable=True)
    return BackendResult(payload={"answer": answer, "request_bytes": len(body),
                                  "response_bytes": response.num_bytes_downloaded or len(response.content)})


class StudioHubBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.modality == "chat":
            return _generate_chat(request)
        if request.modality == "embeddings":
            return _generate_embeddings(request)
        if request.modality == "vision":
            return _generate_vision(request)
        if request.modality in {"image", "tts", "music", "video"}:
            return _generate_image_or_voice(request)
        if request.modality == "transcription":
            return _generate_transcription(request)
        raise BackendError(
            f"Studio Hub does not serve '{request.modality}'",
            code="MODEL_MODALITY_MISMATCH",
            upstream_accepted=False,
        )

    def resume(self, request: BackendRequest) -> BackendResult:
        """Pick up a batch this gateway submitted before it restarted: the same poll and collect.

        The Hub keeps the batch and its result across a Gateway restart, so a job it is still
        rendering is followed to the end exactly as `generate` would have, by `upstream_job_id`.
        A Hub that answers 404 has no record of the batch: that stays an unknown outcome (the hold
        sweep settles it from there), never a guess either way.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if request.modality not in {"image", "tts", "music", "video", "transcription"} or not batch_id:
            raise BackendError(
                f"a Studio Hub {request.modality} job cannot be resumed by id",
                code="UNKNOWN_OUTCOME", upstream_accepted=True,
            )
        base = _resolve_base(request)
        headers = _headers(request)
        jobs_path = _jobs_path(request.modality)
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            progress = HubProgress()
            try:
                outcome = _poll_batch(
                    client, base, headers, batch_id, request.upstream_item_index,
                    jobs_path=jobs_path, deadline_seconds=lane_deadline_seconds(request),
                    progress=progress, on_wait=request.on_wait,
                    dispatched_at=request.dispatch_started_at, stranded=_stranded(request),
                    deadline_for_machine=lambda machine: lane_deadline_seconds(request, machine),
                    resumed_after_restart=request.resumed_after_restart, on_deadline=request.on_deadline,
                )
            except BackendError as exc:
                if exc.status != 404:
                    exc.evidence = progress.evidence(batch_id)
                    raise
                raise BackendError(
                    f"after a gateway restart the Hub at {base} has no record of batch {batch_id}",
                    code="UNKNOWN_OUTCOME", upstream_accepted=True,
                    recovery={"batch_id": batch_id, "item_index": request.upstream_item_index,
                              "location": base, "jobs_path": jobs_path,
                              "recorded_by": "gateway_restart"},
                ) from exc
            collect = _collect_transcription if request.modality == "transcription" else _collect_batch
            return collect(client, base, headers, request, batch_id, outcome, progress)

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

    def reconcile(self, request: BackendRequest) -> dict[str, Any]:
        """Ask the Hub to settle an uncertain item from its original worker (2026-10-05).

        The Hub's reconcile route only reads the original job - it never submits another - and
        adopts a finished one or records the worker's own failure; either way the Mac is released.
        The controller relays it to the node that ran it, with the fleet credential this gateway
        already holds. Operator-initiated, like `discard`; the hold sweep (`worker.reconcile_holds`)
        then settles the job from the Hub's answer. The Hub has no reconcile route for transcription.
        """
        batch_id = str(request.upstream_job_id or "").strip()
        if not batch_id:
            return {"ok": False, "status": None,
                    "reason": "nothing was dispatched to the Hub for this job"}
        if request.modality == "transcription":
            return {"ok": False, "status": None,
                    "reason": "the Hub has no reconcile route for transcription batches"}
        route = f"/api/hub/jobs/{batch_id}/items/{request.upstream_item_index}/reconcile"
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
        if request.modality == "chat" and batch_id.startswith("chat:"):
            request_id = batch_id.removeprefix("chat:")
            if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
                return {"ok": False, "reason": "invalid Chat request id"}
            route = f"/studio/chat/requests/{request_id}"
            return _recorded_call("DELETE", f"{_resolve_base(request)}{route}", _headers(request), route)
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
