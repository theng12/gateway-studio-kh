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

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class BackendResult:
    """What a backend produced.

    Exactly one of `content` (a binary artifact) or `text` (a language
    response) is normally set. `payload` carries anything else worth surfacing
    on the job envelope — a finish reason, a usage count, a resolved seed.
    """

    content: bytes | None = None
    media_type: str | None = None
    text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


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


@runtime_checkable
class Backend(Protocol):
    def generate(self, request: BackendRequest) -> BackendResult: ...
