"""`chrome_extension`: a generation run by a browser-extension worker (wire v1, `extension_workers`).

The adapter offers the job, waits for a worker to claim it, and waits for the worker's answer.
**Paid credits decide every failure:**

- nobody claimed it → withdrawn, never sent: an ordinary retryable refusal;
- claimed → `upstream_job_id` is checkpointed first, so from then on any answer that is not a
  definite one (`ambiguous` fail, a silent lease, a restart) is a durable hold, never replayed;
- a definite `ambiguous: false` fail is settled: the worker says nothing was spent.
"""
from __future__ import annotations

import base64
import time

from .. import extension_workers as ew, video_capabilities
from .base import BackendError, BackendRequest, BackendResult, staged_bytes, STAGED_INPUT_GONE, staged_path

_POLL_SECONDS = 1.0


def _uncertain(message: str) -> BackendError:
    return BackendError(message, code="PROVIDER_SUBMISSION_UNCERTAIN", upstream_accepted=True)


def _references(request: BackendRequest) -> list[dict[str, str]]:
    """Every staged picture, in order, as the worker's `referenceImages`. A missing one refuses."""
    out: list[dict[str, str]] = []
    single = staged_bytes(request, "_reference_path", "_reference_type")
    if single is not None:
        out.append({"mediaType": single[1], "dataBase64": base64.b64encode(single[0]).decode("ascii")})
    paths = request.params.get("_reference_paths")
    types = request.params.get("_reference_types")
    types = types if isinstance(types, list) else []
    for index, path in enumerate(paths if isinstance(paths, list) else []):
        staged = staged_path(path)
        if staged is None or not staged.exists():
            raise BackendError(STAGED_INPUT_GONE, code="INVALID_PARAMS", upstream_accepted=False)
        media_type = str(types[index]) if index < len(types) and types[index] else "image/png"
        out.append({"mediaType": media_type,
                    "dataBase64": base64.b64encode(staged.read_bytes()).decode("ascii")})
    return out


def _refuse(message: str) -> BackendError:
    return BackendError(message, code="INVALID_REQUEST", status=422, upstream_accepted=False)


def _payload(request: BackendRequest, references: list[dict[str, str]]) -> dict:
    """The §4 job, from the model's `VideoCapability` record. What the record does not publish is
    refused here, before any worker sees it (the extension would otherwise improvise and spend)."""
    params = request.params
    record = video_capabilities.for_model(request.provider_id, request.model_id)
    if record is None:
        raise _refuse(f"{request.model_id} has no capability record; it is not selectable")
    text = "text-to-video" in record.modes
    duration = params.get("duration_seconds", params.get("duration"))
    if duration is None:
        duration = record.duration_default
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise _refuse(f"duration {duration!r} is not a whole number of seconds") from None
    if duration not in record.durations():
        raise _refuse(f"{request.model_id} makes {', '.join(map(str, record.durations()))} s clips; "
                      f"{duration} s was asked for")
    aspect = params.get("aspect_ratio") or record.aspect_ratios[0]
    if aspect not in record.aspect_ratios:
        raise _refuse(f"{request.model_id} publishes {', '.join(record.aspect_ratios)}; {aspect} was asked for")
    if text and references:
        raise _refuse(f"{request.model_id} is text-to-video and takes no picture")
    if not text and not references:
        raise _refuse(f"{request.model_id} needs its reference picture")
    if len(references) > record.max_reference_images:
        raise _refuse(f"{request.model_id} takes at most {record.max_reference_images} reference "
                      f"image(s); {len(references)} were named")
    return {
        "kind": request.modality,
        "videoMode": "text" if text else "image",
        "prompt": str(params.get("prompt") or ""),
        "modelKey": request.model_id,
        "modelLabel": request.display_name or request.model_id,
        "durationSec": duration,
        "aspectRatio": aspect,
        "flowProjectId": request.values.get("flow_project_id") or None,
        "referenceImages": references,
    }


class ChromeExtensionBackend:
    def generate(self, request: BackendRequest) -> BackendResult:
        if request.upstream_job_id:
            # A restart lost the hand-off of a job a worker had claimed: it may have spent credits.
            raise _uncertain("a browser worker had this job when the Gateway restarted; "
                             "its outcome is unknown, so it is held and not sent again")
        if not ew.enabled():
            raise BackendError("browser-extension workers are switched off on this Gateway",
                               code="PROVIDER_DISABLED", upstream_accepted=False)
        kind = request.modality
        if kind not in ew.KINDS:
            raise BackendError(f"browser workers do not serve {kind}", code="INVALID_REQUEST",
                               upstream_accepted=False)
        payload = _payload(request, _references(request))
        if not ew.broker.online(kind):
            raise BackendError("no browser worker is online for this model", code="WORKER_OFFLINE",
                               retryable=True, upstream_accepted=False)
        job = ew.broker.offer(kind, payload)
        result = self._follow(request, job)
        # Forgotten only once its answer is in hand; a held job stays so a late answer can find it.
        ew.broker.forget(job)
        return result

    def _follow(self, request: BackendRequest, job: ew.WorkerJob) -> BackendResult:
        offered = time.time()
        checkpointed = False
        while True:
            job.changed.wait(_POLL_SECONDS)
            job.changed.clear()
            if job.state == "pending":
                if request.cancelled is not None and request.cancelled() and ew.broker.withdraw(job):
                    raise BackendError("cancelled before a worker took it", code="CANCELLED",
                                       upstream_accepted=False)
                if time.time() - offered > ew.claim_wait_seconds() and ew.broker.withdraw(job):
                    raise BackendError("no browser worker took this job; nothing was sent",
                                       code="WORKER_OFFLINE", retryable=True, upstream_accepted=False)
                continue
            if not checkpointed:
                # Before anything else: from here the worker may spend credits on it.
                if request.checkpoint is not None:
                    request.checkpoint(ew.upstream_id(job.owner or "", job.job_id))
                checkpointed = True
            if job.state == "succeeded":
                return BackendResult(content=job.content, media_type=job.media_type,
                                     payload={"worker_id": job.owner})
            if job.state == "failed":
                taken = ew.broker.take_failure(job)
                if taken == "held":   # from here a later result is adopted into the row
                    raise _uncertain(f"the browser worker could not tell whether it was charged: "
                                     f"{job.error}")
                if taken == "failed":
                    ew.broker.forget(job)
                    raise BackendError(job.error, code="PROVIDER_FAILED", upstream_accepted=True,
                                       upstream_settled=True)
                continue   # a result replaced the ambiguous failure: read it next turn
            # `hold` is the one transition that can lose a race with the worker's answer: only a
            # hold that actually happened raises; otherwise the answer landed, so read it next turn.
            if time.time() - job.last_heard > ew.lease_seconds() and ew.broker.hold(job):
                raise _uncertain(f"the browser worker went silent for over {int(ew.lease_seconds())} s "
                                 "with this job; it is held, never sent again")
