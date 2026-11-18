"""Backend adapters — where provider-specific HTTP actually happens.

Every adapter implements one method, `generate`, and returns a `BackendResult`.
That is deliberately narrower than the `GenerationProvider` port in
`gateway.ports`: that port models a *remote asynchronous task* (create, poll,
download), which is exactly right for fal and Kie, but an awkward fit for an
OpenAI-compatible chat call that returns its answer in one response.

So the layering is:

    api.py  ->  adapter.generate()  ->  BackendResult
                     |
                     +-- one-shot HTTP (chat, TTS, transcription)
                     +-- async task + poll (fal, Kie, Studio Hub)

Adapters never touch the job store, the catalogue or auth. They receive a
provider definition, resolved credentials, a model id and params, and either
return a result or raise `BackendError`.
"""

from __future__ import annotations

import base64
import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

# ---------------------------------------------------------------------------
# "Did anything leave the building?" — counted where every request goes through.
#
# A failure raised *before* an adapter's first request is a plain failure: nothing reached a
# provider, so there is nothing to reconcile and a retry cannot duplicate work. Without this the
# worker could only ask "had dispatch begun?", and `dispatch_started_at` is stamped before the
# adapter is even called — so the 0.2.97 pool bug (`AttributeError` while resolving members, no
# request ever made) parked twenty jobs as "Upstream outcome is unknown" and Story locked them.
#
# Every adapter speaks HTTP through `httpx.Client.send` (`httpx.post`/`get` build a Client and go
# through it too), so one counter there sees every send without an adapter having to remember to
# report it — a forgotten mark would read as "nothing sent", the unsafe direction. Counted per
# thread because each job runs on its own worker thread; adapters do not spawn threads.
# ponytail: counts request *attempts* (a refused connect counts as sent). Conservative by design.
# ---------------------------------------------------------------------------
_sends = threading.local()


def requests_started() -> int:
    """How many HTTP requests this thread has started. Compare before/after a call."""
    return getattr(_sends, "count", 0)


def note_request_started() -> None:
    """Count one send. Called by the `httpx.Client.send` wrapper (and by test doubles that fake one)."""
    _sends.count = getattr(_sends, "count", 0) + 1


if not getattr(httpx.Client.send, "_gateway_counts_sends", False):
    _original_send = httpx.Client.send

    def _counted_send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        note_request_started()
        return _original_send(self, request, **kwargs)

    _counted_send._gateway_counts_sends = True  # type: ignore[attr-defined]
    httpx.Client.send = _counted_send  # type: ignore[method-assign]


@dataclass(slots=True, frozen=True)
class Artifact:
    """One binary result: its bytes and the media type those bytes came back as."""

    content: bytes
    media_type: str | None = None


@dataclass(slots=True)
class BackendResult:
    """What a backend produced.

    Exactly one of the artifacts or `text` (a language response) is normally set.
    `payload` carries anything else worth surfacing on the job envelope — a
    finish reason, a usage count, a resolved seed.

    **A provider can answer more than one artifact for one request**, and `content`
    is only the *first*: Suno answers two or more track variations per call, and an
    OpenAI-shaped image call with `n: 2` answers two images. Everything past the
    first rides in `additional_artifacts`. `content`/`media_type` stay the singular
    spelling because every single-artifact lane and every existing caller reads
    them; `artifacts` is the one place that answers "what did this job produce?",
    so a reader never has to know which spelling an adapter happened to fill.
    """

    content: bytes | None = None
    media_type: str | None = None
    text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    additional_artifacts: list[Artifact] = field(default_factory=list)
    #: Sent by the caller **after** it has durably kept the artifacts, never from
    #: inside the download. A delivery receipt is the caller asserting that
    #: delivery was *verified* — the Hub protects its output index only on a 2xx
    #: there — so acknowledging while the bytes exist only in memory would release
    #: work that was never actually kept. Returns what the upstream answered, for
    #: the job record.
    deliver: Callable[[], dict[str, Any]] | None = None

    @property
    def artifacts(self) -> list[Artifact]:
        """Every artifact this result carries, first one first.

        The first artifact is by definition `content`/`media_type`, so the two
        spellings cannot drift: this is the only reader, and adapters that return
        one artifact need not know `additional_artifacts` exists.
        """
        ordered: list[Artifact] = []
        if self.content is not None:
            ordered.append(Artifact(self.content, self.media_type))
        ordered.extend(self.additional_artifacts)
        return ordered


class BackendError(Exception):
    """A generation failed in a way worth reporting to the caller.

    `retryable` distinguishes "the provider was briefly unhappy" from "this
    request is wrong". `code` becomes the job's `failure_code`, which Story
    Studio reads to decide whether to advance its model lineup -- so it matters
    that a transient provider fault is not reported as a permanent one.

    Note the deliberate absence of any 402 mapping. Story Studio holds a credit
    lock keyed on the API key that never clears without a published balance, so
    a provider running out of funds must surface as a settled failure, never as
    insufficient credits.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        status: int | None = None,
        retryable: bool = False,
        upstream_accepted: bool | None = None,
        recovery: dict[str, Any] | None = None,
        upstream_settled: bool = False,
        retry_after: float | None = None,
        quota_scope: str | None = None,
        refusal_scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        # **Whose verdict a refusal is** (0.2.273). A `POLICY_REFUSAL` from a provider's own
        # content filter is that model's answer about *its* output; a gateway-side refusal is
        # about the request and every member would give the same one. A pool must walk past the
        # first and stop on the second, so the adapter that knows says which: `"response"` here,
        # `None` (the request) everywhere else.
        self.refusal_scope = refusal_scope
        # What the provider said about when a quota/rate limit lifts: seconds from now
        # (`Retry-After`, Google's `RetryInfo`), and whether the limit that was hit is a
        # per-minute or a per-day one. Both `None` when it said nothing; a pool reads them
        # to remember an exhausted member (`pool_cooldowns.py`).
        self.retry_after = retry_after
        self.quota_scope = quota_scope
        # `None` means the adapter did not establish whether the remote accepted
        # work; the worker must not infer that a retry is safe from `retryable`.
        self.upstream_accepted = upstream_accepted
        # **The adapter followed its own upstream identifier to a terminal state**: the work is
        # finished, it produced no artifact, and nothing is pending. A *recorded* upstream id
        # cannot say that — an id only proves a container was created — which is why a
        # deferred-endpoint adapter that creates one before validating anything (Studio Hub
        # creates a batch, then checks the item) left every ordinary failure looking ambiguous.
        # Set it only where the adapter has polled that identifier to a terminal answer.
        self.upstream_settled = upstream_settled
        # The upstream's own identifiers for work whose outcome is unknown — the
        # batch, the item, the worker it went to, and the routes that reconcile or
        # discard it. Carried so a recovery hold is actionable rather than a
        # failure an operator has to guess about.
        self.recovery = recovery
        # What the lane stated about the failed attempt (a Studio Hub item's machine, batch and
        # run seconds), kept on the failed job's record. Set by the adapter that knows it.
        self.evidence: dict[str, Any] | None = None
        # A Studio Hub narration section the quality gate refused, kept there for listening:
        # `{"batch_id", "item_index", "receipt_sha256", "pcm_bytes", "pcm_sha256"}`, already
        # verified against the Hub. Never media, a credential or an address; never a result.
        self.review_candidate: dict[str, Any] | None = None


def settled_failure(message: str, *, code: str = "PROVIDER_FAILED") -> BackendError:
    """A poll of the accepted identity explicitly said its work has ended."""
    return BackendError(message, code=code, upstream_accepted=True, upstream_settled=True)


def unusable_answer(provider_id: str, problem: str, *, code: str = "PROVIDER_FAILED") -> BackendError:
    """The provider **answered**, and what it answered cannot be used: a settled failure.

    The bytes are on the wire, so nothing is outstanding upstream — the provider finished and
    said nothing is running. Without `upstream_settled` the worker classifies this as an
    unknown-outcome hold ("reconcile the provider request before retrying"), a hold with no
    attempt to aim at and no way to settle: measured on a 200 whose body was an HTML error page,
    which parked a chat job as `unknown_outcome` with `retryable: False`. `retryable` so the
    caller's Retry gets a fresh answer instead of looping on the same one.
    """
    return BackendError(
        f"{provider_id}: the provider's answer could not be used — {problem}"[:500],
        code=code, retryable=True, upstream_accepted=True, upstream_settled=True,
    )


def require_json_answer(request: "BackendRequest", text: str, finish_reason: Any = None) -> Any:
    """A chat answer the caller asked to be JSON must parse; returns the parsed value (else `None`).

    `response_format` `json_object` / `json_schema` is a request for JSON, so text that does not
    parse is an unusable answer like an empty one — a settled, retryable failure, which makes a
    pool move on to its next member instead of returning it as a success. Measured 2026-10-20:
    free `qoder/qfmodel` answers cut off mid-string (28k–90k characters) under
    `finish_reason: "stop"`, and Story rejected every one while a working member was never tried.
    The cut-off is told from other breakage and kept, with the provider's claimed finish reason,
    as the attempt's evidence. Nothing is repaired here. Costs a dict lookup for any other request.
    """
    response_format = request.params.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") not in {"json_object", "json_schema"}:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        cut_off = "Unterminated" in str(exc) or getattr(exc, "pos", 0) >= len(text.rstrip())
        where = (f"cut off at {len(text)} characters" if cut_off
                 else f"{getattr(exc, 'msg', exc)} at character {getattr(exc, 'pos', '?')} of {len(text)}")
        error = unusable_answer(request.provider_id, f"the model returned text that is not valid JSON ({where})")
        error.evidence = {"finish_reason": finish_reason, "cut_off": cut_off, "answer_characters": len(text)}
        raise error from None


def answer_json(response: Any, provider_id: str) -> dict[str, Any]:
    """The provider's own 200 body as a mapping. A body we cannot read is a settled failure.

    `response.json()` raises `ValueError` on anything that is not JSON (a CDN's HTML error page
    under a 200, a body cut short in flight), and an escaping `ValueError` reaches the worker as
    "an adapter raised unexpectedly" *after* a request was sent — the exact shape it holds as an
    unknown outcome. Reading it here keeps the classification honest.
    """
    try:
        payload = response.json()
    except ValueError:
        raise unusable_answer(
            provider_id, f"its body is not JSON ({response.text[:120]!r})"
        ) from None
    return payload if isinstance(payload, dict) else {}


def quota_hints(response: Any) -> dict[str, Any]:
    """`retry_after` / `quota_scope` kwargs for a `BackendError`, read from a 429-ish response.

    `Retry-After` is seconds or an HTTP date. Google's body carries `RetryInfo.retryDelay`
    ("34s") and `QuotaFailure.violations[].quotaId` ("...PerDay...", "...PerMinute...").
    Anything unreadable is simply absent: a hint is never worth failing the error over.
    """
    import json
    import re
    from email.utils import parsedate_to_datetime
    from datetime import datetime, timezone

    out: dict[str, Any] = {}
    try:
        header = response.headers.get("retry-after")
    except Exception:  # noqa: BLE001
        header = None
    if header:
        try:
            out["retry_after"] = max(0.0, float(header))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                out["retry_after"] = max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, list) and body:  # Google's OpenAI-compatible route wraps the error in a list
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    for detail in (error.get("details") if isinstance(error, dict) else None) or []:
        if not isinstance(detail, dict):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and "retry_after" not in out:
            found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", delay)
            if found:
                out["retry_after"] = float(found.group(1))
        for violation in detail.get("violations") or []:
            quota_id = str((violation or {}).get("quotaId") or "")
            if re.search(r"PerDay|Daily", quota_id, re.I):
                out["quota_scope"] = "day"
            elif re.search(r"PerMinute", quota_id, re.I) and "quota_scope" not in out:
                out["quota_scope"] = "minute"
    return out


@dataclass(slots=True)
class BackendRequest:
    """Everything an adapter needs, resolved by the caller."""

    provider_id: str
    model_id: str
    modality: str
    params: dict[str, Any]
    values: dict[str, str]        # resolved credentials
    base_url: str
    display_name: str = ""
    # Async adapters can call this immediately after receiving a durable remote
    # id. Existing adapters that do not checkpoint remain conservatively manual.
    checkpoint: Callable[[str], None] | None = None
    #: The durable id the upstream gave this work, carried on a cancellation so an
    #: adapter can ask the *owner of the work* to stop it. Empty means nothing was
    #: dispatched, so there is nothing upstream to cancel — which is a different
    #: state from "the upstream refused", and must not read the same.
    upstream_job_id: str = ""
    #: Which item of that upstream batch this job is. The gateway submits
    #: single-item batches, so this is 0 today — but the Hub's per-item routes
    #: (ack, artifact, discard) all name an index, and hardcoding it in each of
    #: them is how one of them ends up addressing the wrong item.
    upstream_item_index: int = 0
    #: Told when a dispatched job is waiting for the upstream to start it (`{"reason", "next_try_at",
    #: "since"}`) and again with `None` when it stops. Absent means nobody is listening.
    on_wait: Callable[[dict[str, Any] | None], None] | None = None
    #: Epoch time the gateway dispatched this job, set only on a resume after a restart: the
    #: fallback for how long it has already run when the upstream does not say.
    dispatch_started_at: float | None = None
    resumed_after_restart: bool = False
    on_deadline: Callable[[str], None] | None = None
    #: The output allowance this gateway **publishes** for the model
    #: (`catalog.published_output_cap`), so an adapter can send the number the caller was told
    #: instead of its own private default or the provider's unstated fallback. A value in
    #: `params` always wins: this is only the answer to "the caller asked for nothing".
    max_output_tokens: int | None = None
    #: **The longest this one call may take**, when the caller knows a tighter bound than the
    #: lane's own ceiling. Only the pool sets it: a silent cloud member must not hold the whole
    #: pool for the Hub's four-hour chat ceiling (`concurrency.pool_member_budget`). `None` means
    #: the lane's own timeout applies, unchanged.
    deadline_seconds: float | None = None
    #: Whether the owner has asked to cancel the durable job this request runs for. Checked before
    #: each request an adapter sends, which is where a cancel can be honoured for free: a single
    #: blocked POST cannot be interrupted from here, and is bounded by `deadline_seconds` instead.
    cancelled: Callable[[], bool] | None = None


#: The names a caller may use for "how long may the answer be". `max_output_tokens` is this
#: gateway's own published control, `max_completion_tokens` is OpenAI's current name for the
#: field and the one the Hub's lane contract publishes, and `max_tokens` is the legacy name
#: OpenAI's own API still accepts. Ordered so the gateway's own control is read first when a
#: caller somehow sends two.
_MAX_OUTPUT_SPELLINGS = ("max_output_tokens", "max_completion_tokens", "max_tokens")


#: How an adapter says "the picture this job was given is not here any more". One sentence, used by
#: every lane that reads a staged upload, so the caller is told what to do rather than which
#: adapter noticed.
STAGED_INPUT_GONE = (
    "the source image for this video job is no longer held; upload it again and retry"
)


def staged_path(value: Any) -> Path | None:
    """A staged input's path, **refused unless it lives in the Gateway's artifact store**.

    The adapter is the last door, and it is the only one that reads bytes. `_reference_path`,
    `_reference_paths`, `_end_reference_path` and `_upload_path` name files the *Gateway*
    staged — but a params dict is client JSON on more than one route, and a value naming
    `~/.ssh/id_rsa` was enough to have an adapter read it and send it to a provider (review
    2026-10-28). The admin create routes now refuse every ``_``-prefixed key from a client;
    this confines the read itself, so no future route, replay or stored job can turn the
    adapters into a file reader. `None` when the request names no file at all.
    """
    text = str(value or "").strip()
    if not text:
        return None
    from ..config import ARTIFACT_DIR

    path = Path(text)
    try:
        resolved = path.resolve()
        root = Path(str(ARTIFACT_DIR)).resolve()
    except OSError as exc:  # a path that cannot even be resolved is not a staged input
        raise BackendError(
            f"the staged input for this job could not be read: {exc}",
            code="INVALID_PARAMS", upstream_accepted=False,
        ) from exc
    if not resolved.is_relative_to(root):
        raise BackendError(
            "the staged input for this job is outside the Gateway's artifact store; "
            "it is refused rather than sent to a provider",
            code="INVALID_PARAMS", upstream_accepted=False,
        )
    return path


def staged_bytes(request: "BackendRequest", path_key: str, type_key: str) -> tuple[bytes, str] | None:
    """One staged upload this job carries, or `None` when the request names none.

    `api._stage_video_reference` copies the caller's upload beside the job at submit, because the
    upload itself is held for 15 minutes and an adapter reads at *dispatch* — a job that queued
    past the window, or outlived a restart, still has its picture.

    **A staged file that has gone is a named refusal**, never a request that quietly loses its
    image: a video generated without the scene image is a video of something else, and nothing
    downstream can tell it apart from the one that was asked for.
    """
    staged = staged_path(request.params.get(path_key))
    if staged is None:
        return None
    if not staged.exists():
        raise BackendError(STAGED_INPUT_GONE, code="INVALID_PARAMS", upstream_accepted=False)
    media_type = str(request.params.get(type_key) or "image/png")
    return staged.read_bytes(), media_type


def staged_data_uri(request: "BackendRequest", path_key: str, type_key: str) -> str | None:
    """`staged_bytes` in the `data:` URI form every provider here accepts for an inline image."""
    staged = staged_bytes(request, path_key, type_key)
    if staged is None:
        return None
    content, media_type = staged
    return f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"


def staged_data_uris(request: "BackendRequest", paths_key: str, types_key: str) -> list[str]:
    """Every staged reference image, in the order the caller named them.

    A list that is partly gone is a refusal, not a shorter list: sending one reference fewer is a
    render of different characters under the same request.
    """
    paths = request.params.get(paths_key)
    if not isinstance(paths, list) or not paths:
        return []
    types = request.params.get(types_key)
    types = types if isinstance(types, list) else []
    uris: list[str] = []
    for index, path in enumerate(paths):
        staged = staged_path(path)
        if staged is None or not staged.exists():
            raise BackendError(STAGED_INPUT_GONE, code="INVALID_PARAMS", upstream_accepted=False)
        media_type = str(types[index]) if index < len(types) and types[index] else "image/png"
        uris.append(f"data:{media_type};base64,"
                    f"{base64.b64encode(staged.read_bytes()).decode('ascii')}")
    return uris


def bounded_max_output_tokens(request: "BackendRequest", stated: Any) -> tuple[Any, dict | None]:
    """The answer length to **send** when the caller named one, never above what the model accepts.

    `catalog.published_output_cap` is the allowance this gateway publishes — and since
    2026-10-30 it is capped at the model's context window, because an allowance above
    the window is not a limit the model can honour. A caller's number above that
    published figure is lowered to it and reported on the job
    (`max_tokens_lowered_to`), so the caller learns what was sent instead of waiting
    for the provider to refuse the request after it has measured the prompt.

    **Nothing is lowered without a published number to lower it to.** `None` cap,
    no caller value, and a malformed value all travel exactly as before — the last
    one is the provider's to refuse, under every spelling it may use.

    Lowering stays on the lane whose own contract states a maximum and whose worker
    would otherwise use a fallback of its own (Studio Hub's chat lane: its contract
    publishes `max_tokens.maximum` beside a `default` of 128,000 that its own note
    says "is not a number the model states"). Cloud lanes keep the caller's number:
    their published limit is the provider's own statement, and their refusal names
    it in their words (`anthropic._max_tokens_too_large`).

    Room is measured against the published allowance, not against
    `context − prompt`: the Gateway never holds an input token count before a request
    is sent, and inventing one from a characters-per-token ratio is the kind of
    guess the house rules forbid.
    """
    if not stated:
        return stated, None
    if isinstance(stated, bool) or not isinstance(stated, (int, float)):
        return stated, None
    cap = getattr(request, "max_output_tokens", None)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return stated, None
    if int(stated) <= cap:
        return stated, None
    return cap, {"asked": stated, "sent": cap,
                 "why": "above the output allowance this model publishes"}


def caller_max_output_tokens(params: Any) -> Any:
    """The answer length the **caller** asked for, under every spelling — one chooser.

    Three lanes build a chat body from the caller's `params` and each read its own subset of
    the names above, so the same request was answered differently depending on which lane
    served it: the OpenAI-compatible lane read two of the three and the Anthropic lane two,
    while the Studio Hub lane read all three. A caller that used `max_completion_tokens` —
    the spelling OpenAI's current API and the Hub's own lane contract use — had its allowance
    silently dropped, and on Anthropic that meant the adapter's private 4,096 was sent in its
    place. Read here, once, so a lane cannot drift again.

    **Falsy is absent**, deliberately, which is the opposite of the rule for a *published*
    limit (`catalog._stated` keeps a stated `0` because a provider saying zero is a claim).
    A caller's `0` is not a claim about the model: it is a number no provider accepts, and
    sending it would turn a harmless typo into a `400`. Absent and zero both mean "the caller
    asked for nothing", which is the question this answers.

    The value travels **verbatim**, exactly as each lane forwarded it before: a malformed one
    is the provider's to refuse, and coercing it here would turn one wrong number into a
    different wrong number that the caller never asked for.
    """
    if not isinstance(params, dict):
        return None
    for spelling in _MAX_OUTPUT_SPELLINGS:
        value = params.get(spelling)
        if value:
            return value
    return None


#: The longest a Chat job may be held, in seconds — **the Hub's own ceiling**, restated here
#: because this gateway must outwait it. Studio Hub computes each Chat job's deadline from the
#: job itself (`components/hub/backend/chat_deadline.py`, `CEILING_S`) and never gives it more
#: than four hours. A chat call bounded by anything shorter than that would cut a job the Hub was
#: still legitimately running — which is exactly what the flat 300 s read timeout here did to a
#: 100-prompt scene pack, and what the Hub's per-lane 1,800 s would still have done.
CHAT_DEADLINE_CEILING_S = 4 * 3600.0


def caller_expected_output_tokens(params: Any) -> int | None:
    """The answer length the caller **expects**, which is what the Hub sizes a deadline from.

    Not the same question as `caller_max_output_tokens`: a ceiling says what may be sent, an
    expectation says what is coming. Story Studio already computes the second — a scene-prompt
    pack's own size x its per-scene tokens, an idea batch's token budget — and sends it as
    `expected_output_tokens`, because the Hub cannot tell a 100-prompt pack from a one-line
    question by reading the request, and sizing every job from `max_tokens` would make every job
    three hours long.

    A value that is not a positive whole number is **not forwarded at all** rather than passed on:
    the Hub's own reader refuses it anyway, and sending junk would only add a second, weaker copy
    of that refusal.
    """
    if not isinstance(params, dict):
        return None
    value = params.get("expected_output_tokens")
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return int(value) if value > 0 and float(value) == int(value) else None


#: The header Studio Hub reads a caller's own expectation from — the Hub's own spelling
#: (`components/hub/backend/chat_deadline.py`, `HINT_HEADER`). A **header**, not a body field,
#: because the Hub forwards the body verbatim to the chat worker and the worker refuses a field
#: it does not know; the headers are rebuilt at every hop, so a header cannot leak onward.
EXPECTED_OUTPUT_HEADER = "X-StudioHub-Expected-Output-Tokens"


def expected_output_header(params: Any) -> dict[str, str]:
    """`{header: count}` for the Hub, or `{}` when the caller stated no expectation."""
    hint = caller_expected_output_tokens(params)
    return {EXPECTED_OUTPUT_HEADER: str(hint)} if hint is not None else {}


@runtime_checkable
class Backend(Protocol):
    def generate(self, request: BackendRequest) -> BackendResult: ...


class CancellableBackend(Protocol):
    """A backend whose remote work can be asked to stop.

    Deliberately separate from `Backend`: most adapters issue a one-shot request
    whose only in-flight state is a socket, and claiming they can cancel remote
    work would be false. An adapter that *can* is asked through `getattr`, so
    "this lane cannot cancel upstream" is an answer the caller records rather
    than a method that silently does nothing.
    """

    def cancel(self, request: BackendRequest) -> dict[str, Any]: ...
