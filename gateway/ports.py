"""The internal provider abstraction — lifted from GenStudio KH.

GenStudio's `app/ports/generation_provider.py` is about 110 lines: three
methods, three dataclasses and four exceptions. Every image and music adapter
there implements it exactly and imports *nothing* but `app.ports.*` — no DB, no
business modules. That is precisely why those adapters are reusable verbatim,
and why this gateway copies the shape instead of inventing a new one.

The reason it fits is that Studio Hub's HTTP API already has the same shape:

    POST /api/hub/jobs  ->  GET /api/hub/jobs/{batch}  ->  GET .../artifact

So a local runner and a remote API both reduce to "a thing with create_task,
get_task and download_artifact". That equivalence *is* the architecture.

One deliberate deviation from the original: `GenerationProvider` here is
`runtime_checkable`. GenStudio's is not, and its video adapters consequently
had to be dispatched with concrete `isinstance` checks that its own type
annotations pretended were unnecessary. Making the port checkable costs nothing
and removes that trap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable


class ProviderTaskState(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderTaskHandle:
    task_id: str


@dataclass(frozen=True, slots=True)
class ProviderTask:
    task_id: str
    state: ProviderTaskState
    result_urls: tuple[str, ...] = ()
    # Retained from the original for shape-compatibility with lifted adapters.
    # This gateway charges nothing, so it is always None or 0.
    credits_consumed: int | float | None = None
    fail_code: str | None = None
    fail_message: str | None = None
    # A refinement of FAILED, never a state of its own.
    content_refused: bool = False


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    source_url: str
    content_type: str
    content: bytes


@runtime_checkable
class GenerationProvider(Protocol):
    def create_task(
        self,
        *,
        model: str,
        input: Mapping[str, object],
        callback_url: str | None = None,
    ) -> ProviderTaskHandle: ...

    def get_task(self, task_id: str) -> ProviderTask: ...

    def download_artifact(self, url: str) -> ProviderArtifact: ...


class ProviderError(RuntimeError):
    """Base provider failure. `retryable` drives whether the job is re-driven."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderUnavailableError(ProviderError):
    retryable = True


class ProviderProtocolError(ProviderError):
    pass


class ProviderContentRefusedError(ProviderProtocolError):
    pass
