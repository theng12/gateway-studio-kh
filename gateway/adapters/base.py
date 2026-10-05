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

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
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
