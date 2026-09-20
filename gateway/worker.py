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
from .store import JobStore

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


def execute_job(job: dict[str, Any], store: JobStore) -> None:
    """Run one job to a terminal state. Never raises."""
    job_id = job["id"]
    modality = job["modality"]
    provider_id = job.get("provider_id") or ""
    model_id = job.get("model_id") or ""

    definition = registry.definition(provider_id)
    if definition is None:
        store.fail(job_id, code="PROVIDER_NOT_FOUND", detail=f"unknown provider '{provider_id}'")
        return

    # Re-check enablement at execution time. A provider can be switched off
    # between submit and run, and the catalogue and the generation path must
    # never disagree about what is available.
    if not registry.credentials.is_enabled(provider_id):
        store.fail(
            job_id,
            code="PROVIDER_DISABLED",
            detail=f"provider '{provider_id}' is disabled",
        )
        return

    try:
        params = json.loads(job.get("params") or "{}")
    except json.JSONDecodeError:
        store.fail(job_id, code="PROVIDER_FAILED", detail="unreadable job params")
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
        store.fail(
            job_id,
            code="MODEL_NOT_OFFERABLE",
            detail=(
                f"'{model_id}' is no longer offered by '{provider_id}' for {modality} "
                "— disabled, unconfigured, or withheld since this job was queued"
            ),
        )
        return

    params["_job_id"] = job_id
    request = BackendRequest(
        provider_id=provider_id,
        model_id=model_id,
        modality=modality,
        params=params,
        values=registry.credentials.resolved(provider_id),
        base_url=definition.base_url,
        display_name=definition.display_name,
    )

    adapter = adapter_for(definition)
    try:
        result = adapter.generate(request)
    except BackendError as exc:
        log.warning("job %s failed via %s: %s", job_id, provider_id, exc)
        store.fail(job_id, code=exc.code, detail=str(exc)[:500])
        return
    except Exception as exc:  # noqa: BLE001 - a backend bug must not kill the worker
        log.exception("job %s raised unexpectedly", job_id)
        store.fail(
            job_id,
            code="PROVIDER_FAILED",
            detail=f"{type(exc).__name__}: {exc}"[:500],
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
                    self.store.fail(
                        job["id"], code="PROVIDER_FAILED", detail="internal worker error"
                    )
                time.sleep(0)  # yield between jobs

            self._stop.wait(self.interval_s)
