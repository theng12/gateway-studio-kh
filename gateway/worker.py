"""Background worker — executes queued jobs off the request thread.

Why this exists rather than generating inline: a local FLUX image on a busy Mac
can take minutes, and Studio Hub jobs are themselves asynchronous. If the
gateway generated inside the HTTP handler, every Story Studio poll would block
for the duration and its own timeouts would fire. So the request path only ever
*accepts* and *reads*; a single worker thread does the waiting.

One worker thread, deliberately. The gateway serves one owner, and a second
thread would add concurrency bugs (double-claiming, artifact races) for no
benefit. If that ever changes, the claim in `JobStore.mark_running` is the
place to add real locking.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .adapters import BackendError, BackendRequest, adapter_for
from .config import ARTIFACT_DIR
from .providers import registry
from .store import RECOVERY_UNKNOWN, JobStore
from . import fleet

log = logging.getLogger("gateway.worker")

_EXT_BY_MEDIA = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "video/mp4": "mp4",
    "application/x-subrip": "srt",
    "text/plain": "txt",
}


def _ext_for(media_type: str | None) -> str:
    return _EXT_BY_MEDIA.get((media_type or "").split(";")[0].strip(), "bin")


def _unknown_detail(detail: str) -> str:
    suffix = (
        " Upstream outcome is unknown; reconcile the provider request before retrying; "
        "the gateway will not resubmit it automatically."
    )
    return f"{detail[: max(0, 500 - len(suffix))]}{suffix}"


def _fail(
    store: JobStore,
    job_id: str,
    *,
    code: str,
    detail: str,
    retryable: bool = False,
    recovery_state: str | None = None,
    recovery_detail: str | None = None,
    recovery_context: dict[str, Any] | None = None,
) -> None:
    """Settle a real store, while keeping the small legacy test double usable."""
    try:
        store.fail(
            job_id,
            code=code,
            detail=detail,
            retryable=retryable,
            recovery_state=recovery_state,
            recovery_detail=recovery_detail,
            recovery_context=recovery_context,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        store.fail(job_id, code=code, detail=detail)


def execute_job(job: dict[str, Any], store: JobStore) -> None:
    """Run one job to a terminal state. Never raises."""
    job_id = job["id"]
    modality = job["modality"]
    provider_id = job.get("provider_id") or ""
    model_id = job.get("model_id") or ""

    current = store.get(job_id) if hasattr(store, "get") else job
    if current is None:
        return
    state = current.get("state")
    if state == "accepted":
        marker = getattr(store, "mark_running", None)
        if marker is not None and marker(job_id) is False:
            return
    elif state is not None and state != "running":
        return

    definition = registry.definition(provider_id)
    if definition is None:
        _fail(
            store,
            job_id,
            code="PROVIDER_NOT_FOUND",
            detail=f"unknown provider '{provider_id}'",
        )
        return

    # Re-check enablement at execution time. A provider can be switched off
    # between submit and run, and the catalogue and the generation path must
    # never disagree about what is available.
    if not registry.credentials.is_enabled(provider_id):
        _fail(
            store,
            job_id,
            code="PROVIDER_DISABLED",
            detail=f"provider '{provider_id}' is disabled",
        )
        return

    try:
        params = json.loads(job.get("params") or "{}")
    except json.JSONDecodeError:
        _fail(store, job_id, code="PROVIDER_FAILED", detail="unreadable job params")
        return

    # **The whole rule, not just the provider.** Provider enablement was re-checked
    # here and nothing else, so three states could change between submit and run and
    # the job would still be dispatched:
    #   * the *model* switched off — the toggle is a control, and it did nothing here;
    #   * a Hub refresh flipping `catalogued` or `memory_eligible`, which is exactly
    #     the "the model cannot serve" state `can_publish` exists to withhold;
    #   * a modality the adapter does not serve.
    # The catalogue's rule is one function — `enabled_models` — so this asks it
    # instead of re-implementing its gates. (The provider check above stays because
    # `PROVIDER_DISABLED` names the cause more precisely than "not offerable".)
    if not any(
        d.id == provider_id and m["model_id"] == model_id
        for d, m in registry.models.enabled_models(modality)
    ):
        _fail(
            store,
            job_id,
            code="MODEL_NOT_OFFERABLE",
            detail=(
                f"'{model_id}' is no longer offered by '{provider_id}' for {modality} "
                "— disabled, unconfigured, or withheld since this job was queued"
            ),
        )
        return

    # The final check sits immediately before the adapter call. A cancellation
    # that wins here is confirmed locally; once this succeeds, the upstream
    # outcome is no longer ours to assume.
    begin_dispatch = getattr(store, "begin_dispatch", None)
    if begin_dispatch is not None and not begin_dispatch(job_id):
        confirm_cancel = getattr(store, "confirm_cancel", None)
        if confirm_cancel is not None:
            confirm_cancel(job_id)
        return

    params["_job_id"] = job_id
    record_acceptance = getattr(store, "record_upstream_acceptance", None)
    # A voice is pinned to the controller that holds its reference clip, so a
    # refusal there is final. Any other job may be tried at the next location that
    # offers the same model: `LOCATION_NOT_READY` is a property of the site, not of
    # the job, and the adapter proved the Hub accepted nothing before it refused.
    pinned = fleet.pinned_location(params)
    tried: set[str] = set()
    result = None
    while True:
        definition = registry.definition(provider_id)
        if definition is None or not registry.credentials.is_enabled(provider_id):
            _fail(
                store,
                job_id,
                code="PROVIDER_DISABLED" if definition is not None else "PROVIDER_NOT_FOUND",
                detail=f"provider '{provider_id}' is disabled or unknown",
            )
            return
        request = BackendRequest(
            provider_id=provider_id,
            model_id=model_id,
            modality=modality,
            params=params,
            values=registry.credentials.resolved(provider_id),
            base_url=definition.base_url,
            display_name=definition.display_name,
            checkpoint=(
                (lambda upstream_job_id: record_acceptance(job_id, upstream_job_id))
                if record_acceptance is not None
                else None
            ),
        )
        adapter = adapter_for(definition)
        try:
            result = adapter.generate(request)
            break
        except BackendError as exc:
            if exc.code == "LOCATION_NOT_READY" and pinned is None:
                tried.add(provider_id)
                chosen, note = fleet.route(model_id, modality, exclude_locations=tried)
                if chosen is not None and chosen.id not in tried:
                    log.warning(
                        "job %s: %s cannot serve '%s' (%s); rerouting to %s",
                        job_id, provider_id, model_id, exc, chosen.id,
                    )
                    provider_id = chosen.id
                    record_route = getattr(store, "record_route", None)
                    if record_route is not None:
                        record_route(job_id, provider_id)
                    continue
            log.warning("job %s failed via %s: %s", job_id, provider_id, exc)
            current = store.get(job_id) if hasattr(store, "get") else job
            current = current or {}
            # Once dispatch began, an adapter that cannot prove pre-acceptance must
            # be treated as ambiguous even when it calls the failure retryable.
            unknown = bool(current.get("upstream_job_id")) or exc.upstream_accepted is True
            unknown = unknown or (
                current.get("dispatch_started_at") is not None
                and exc.upstream_accepted is None
            )
            if exc.code == "PROVIDER_SUBMISSION_UNCERTAIN":
                unknown = True
            detail = _unknown_detail(str(exc)) if unknown else str(exc)[:500]
            # A durable recovery hold keeps the upstream's own identifiers, so the
            # operator has the batch, item, worker and the routes that resolve it
            # rather than a failure they have to reconstruct.
            recovery_detail = detail
            if unknown and exc.recovery:
                recovery_detail = f"{detail} {json.dumps(exc.recovery, sort_keys=True)}"
            _fail(
                store,
                job_id,
                code=exc.code,
                detail=detail,
                retryable=exc.retryable,
                recovery_state=RECOVERY_UNKNOWN if unknown else None,
                recovery_detail=recovery_detail if unknown else None,
                recovery_context=exc.recovery if unknown else None,
            )
            return
        except Exception as exc:  # noqa: BLE001 - a backend bug must not kill the worker
            log.exception("job %s raised unexpectedly", job_id)
            getter = getattr(store, "get", None)
            current = getter(job_id) if getter is not None else job
            current = current or {}
            detail = _unknown_detail(f"{type(exc).__name__}: {exc}")
            _fail(
                store,
                job_id,
                code="PROVIDER_FAILED",
                detail=detail,
                recovery_state=RECOVERY_UNKNOWN if current.get("dispatch_started_at") else None,
                recovery_detail=detail if current.get("dispatch_started_at") else None,
            )
            return

    # **Every artifact the provider returned is written.** A job holds a list now,
    # because a provider can answer several variations for one request (Suno sends
    # one entry per track, an image call with `n` sends one per image) and keeping
    # only the first made the rest unreachable. Artifact 0 keeps the original
    # `{job_id}.{ext}` name, so every single-artifact route and every file already
    # on disk is unchanged; the rest add an index.
    stored: list[dict[str, Any]] = []
    for index, artifact in enumerate(result.artifacts):
        stem = job_id if index == 0 else f"{job_id}-{index}"
        path = Path(ARTIFACT_DIR) / f"{stem}.{_ext_for(artifact.media_type)}"
        path.write_bytes(artifact.content)
        stored.append(
            {
                "asset_id": str(index),
                "path": str(path),
                "media_type": artifact.media_type,
                "bytes": len(artifact.content),
            }
        )
        record_artifacts = getattr(store, "record_artifacts", None)
        if record_artifacts is not None:
            record_artifacts(job_id, [stored[-1]])

    # **The delivery receipt goes out only now.** The Hub protects its output
    # receipt and index on a 2xx from its ack route, and that route is the caller
    # asserting it has verified delivery — downloading an artifact never reaches
    # it. So it is sent once every artifact is on disk, never from inside the
    # download. A refusal is recorded, not raised: the artifact is saved and
    # serveable, and discarding real work over a bookkeeping step would be worse.
    if result.deliver is not None:
        receipt = result.deliver()
        if not receipt.get("ok"):
            log.warning(
                "job %s: upstream did not acknowledge delivery: %s", job_id, receipt
            )

    store.succeed(
        job_id,
        asset_path=stored[0]["path"] if stored else None,
        media_type=stored[0]["media_type"] if stored else None,
        artifacts=stored,
        result={
            "text": result.text,
            "payload": result.payload,
        },
    )


class Worker:
    """Polls the store for due jobs and executes them."""

    def __init__(self, store: JobStore, interval_s: float = 0.4) -> None:
        self.store = store
        self.interval_s = interval_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.processed = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="gateway-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                due = self.store.due()
            except Exception:  # noqa: BLE001
                log.exception("worker could not read due jobs")
                due = []

            for job in due:
                if self._stop.is_set():
                    return
                # Claim before executing so a second pass cannot re-enter.
                if not self.store.mark_running(job["id"]):
                    continue
                job["state"] = "running"
                try:
                    execute_job(job, self.store)
                    self.processed += 1
                except Exception:  # noqa: BLE001
                    log.exception("worker failed to execute %s", job.get("id"))
                    getter = getattr(self.store, "get", None)
                    current = getter(job["id"]) if getter is not None else job
                    current = current or {}
                    unknown = bool(
                        current.get("dispatch_started_at") or current.get("upstream_job_id")
                    )
                    detail = (
                        _unknown_detail("internal worker error")
                        if unknown
                        else "internal worker error"
                    )
                    _fail(
                        self.store,
                        job["id"],
                        code="PROVIDER_FAILED",
                        detail=detail,
                        recovery_state=RECOVERY_UNKNOWN if unknown else None,
                        recovery_detail=detail if unknown else None,
                    )
                time.sleep(0)  # yield between jobs

            self._stop.wait(self.interval_s)
