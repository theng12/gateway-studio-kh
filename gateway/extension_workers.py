"""Browser-extension workers: the `/api/workers/*` wire (v1) and the hand-off to the adapter.

A Chrome extension (Relay Studio KH, "gateway" mode) drives a signed provider web session, so it
is a **worker, not a server**: it polls this Gateway for a job, runs it, and uploads the result.
Wire v1 is `chrome-extensions/_handoffs/2026-11-09-gateway-extension-provider-handoff.md` §4,
kept exactly; every addition is an optional field.

**One authoritative record per thing, no new store:**

- the job is the ordinary `JobStore` row. The adapter checkpoints `ext:<jobId>` as its
  `upstream_job_id` the moment a worker claims it, so the worker's own unknown-outcome rule
  (a checkpointed id + no settled answer = a durable hold, never replayed) applies unchanged;
- worker tokens live in `APP_SUPPORT/extension-workers.json` through `admin_session`'s own
  `0600` reader/writer, **sha256 only** (`admin_session._digest`), the way sessions are kept;
- the hand-off between the adapter thread and the worker's requests is in memory (`_Broker`).
  ponytail: the broker forgets on restart. A claimed job then has its checkpoint and becomes a
  hold (paid credits may be spent; never re-sent), and an unclaimed one was never sent and simply
  runs again. Upgrade to rows in `JobStore` only if a worker job must survive a restart unheld.

**Off by default** (`GATEWAY_EXTENSION_WORKERS`, read live): with it off every route here answers
404, so a live Gateway exposes nothing new until the owner turns it on.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import admin_session
from .config import APP_SUPPORT, get as config_get

SWITCH = "GATEWAY_EXTENSION_WORKERS"
#: The kinds a worker may serve. Video first (owner/Relay brief, 2026-11-09).
KINDS = frozenset({"video"})
#: The largest finished file a worker may upload (Flow clips run to 256 MB, handoff §8).
MAX_RESULT_BYTES = 256 * 1024 * 1024
TOKEN_PREFIX = "ekw_"
_ON = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return config_get(SWITCH, "").strip().lower() in _ON


def _seconds(key: str, default: float) -> float:
    try:
        value = float(config_get(key, "") or default)
        return value if value > 0 else default
    except ValueError:
        return default


def lease_seconds() -> float:
    """A claimed job silent this long is a hold: the credits may be spent, so it is never re-queued."""
    return _seconds("GATEWAY_EXTENSION_WORKER_LEASE_SECONDS", 600)


def online_seconds() -> float:
    """A worker is online while it called within this window (it claims every 30 s)."""
    return _seconds("GATEWAY_EXTENSION_WORKER_ONLINE_SECONDS", 90)


def claim_wait_seconds() -> float:
    """How long a job may wait to be claimed before it is withdrawn, unsent, and refused."""
    return _seconds("GATEWAY_EXTENSION_WORKER_CLAIM_WAIT_SECONDS", 180)


# ---------------------------------------------------------------------------
# Tokens: one per worker, bound to its id and kind, stored hashed, revocable
# ---------------------------------------------------------------------------
def _tokens_path():
    return APP_SUPPORT / "extension-workers.json"


def _rows() -> dict[str, dict]:
    raw = admin_session._read(_tokens_path()).get("workers")
    return raw if isinstance(raw, dict) else {}


def mint(worker_id: str, kind: str) -> str:
    """A new token for this worker, **returned once** and stored only as its digest.

    Minting again for the same worker revokes its earlier token, so one worker has one key.
    """
    worker_id = str(worker_id or "").strip()
    if not worker_id or len(worker_id) > 120 or kind not in KINDS:
        raise ValueError("a worker needs an id (at most 120 characters) and a known kind")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    with admin_session._lock:
        rows = _rows()
        for row in rows.values():
            if row.get("worker_id") == worker_id and not row.get("revoked_at"):
                row["revoked_at"] = now
        rows[admin_session._digest(token)] = {"worker_id": worker_id, "kind": kind,
                                              "created_at": now, "revoked_at": None}
        admin_session._write(_tokens_path(), {"workers": rows})
    return token


def revoke(worker_id: str) -> int:
    """Revoke every live token of this worker; the records stay, so the history does."""
    count = 0
    with admin_session._lock:
        rows = _rows()
        for row in rows.values():
            if row.get("worker_id") == worker_id and not row.get("revoked_at"):
                row["revoked_at"] = time.time()
                count += 1
        if count:
            admin_session._write(_tokens_path(), {"workers": rows})
    return count


def worker_for(token: str) -> dict | None:
    """The live worker this token belongs to, or `None`. Read per call, so a revoke is immediate."""
    token = str(token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = _rows().get(admin_session._digest(token))
    return dict(row) if row and not row.get("revoked_at") else None


def bearer_worker(authorization: str | bytes | None) -> dict | None:
    if isinstance(authorization, bytes):
        authorization = authorization.decode("latin-1")
    text = (authorization or "").strip()
    return worker_for(text[7:]) if text.lower().startswith("bearer ") else None


# ---------------------------------------------------------------------------
# The hand-off between the adapter and the worker's requests
# ---------------------------------------------------------------------------
@dataclass
class WorkerJob:
    job_id: str
    kind: str
    payload: dict[str, Any]
    state: str = "pending"            # pending | claimed | succeeded | failed | held | withdrawn
    owner: str | None = None
    created_at: float = field(default_factory=time.time)
    last_heard: float = 0.0
    phase: str = ""
    message: str = ""
    content: bytes | None = None
    media_type: str | None = None
    error: str = ""
    ambiguous: bool = False
    changed: threading.Event = field(default_factory=threading.Event)


class _Broker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, WorkerJob] = {}
        #: worker id -> what it last told us (seen time, kind, telemetry). In memory: telemetry.
        self.workers: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._jobs.clear()
            self.workers.clear()

    def seen(self, worker: dict, telemetry: Any = None) -> None:
        with self._lock:
            entry = self.workers.setdefault(worker["worker_id"], {"credits": None})
            entry.update(kind=worker["kind"], seen_at=time.time())
            if isinstance(telemetry, dict):
                credits = telemetry.get("credits")
                # **Unknown is `None`, never 0**: only a stated number with its source is kept.
                if isinstance(credits, dict) and isinstance(credits.get("value"), (int, float)) \
                        and not isinstance(credits.get("value"), bool):
                    entry["credits"] = {"value": credits["value"], "source": credits.get("source"),
                                        "observedAt": credits.get("observedAt")}
                version = telemetry.get("extensionVersion")
                if isinstance(version, str):
                    entry["extension_version"] = version[:40]

    def online(self, kind: str) -> bool:
        cutoff = time.time() - online_seconds()
        with self._lock:
            return any(w.get("kind") == kind and w.get("seen_at", 0) >= cutoff
                       for w in self.workers.values())

    def offer(self, kind: str, payload: dict[str, Any]) -> WorkerJob:
        job = WorkerJob(job_id=f"job_{uuid.uuid4().hex}", kind=kind, payload=payload)
        job.payload["jobId"] = job.job_id
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> WorkerJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def withdraw(self, job: WorkerJob) -> bool:
        """Take back a job nobody claimed. `False` when a worker claimed it in the meantime."""
        with self._lock:
            if job.state != "pending":
                return False
            job.state = "withdrawn"
            self._jobs.pop(job.job_id, None)
            return True

    def hold(self, job: WorkerJob) -> None:
        """The adapter gave up waiting on a claimed job; a late answer is still accepted."""
        with self._lock:
            if job.state == "claimed":
                job.state = "held"

    def forget(self, job: WorkerJob) -> None:
        with self._lock:
            self._jobs.pop(job.job_id, None)

    def claim(self, worker: dict) -> WorkerJob | None:
        """**One job per worker**: an unfinished one of its own comes back again, never a second."""
        with self._lock:
            for job in self._jobs.values():
                if job.owner == worker["worker_id"] and job.state == "claimed":
                    job.last_heard = time.time()
                    return job
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at):
                if job.state == "pending" and job.kind == worker["kind"]:
                    job.state, job.owner, job.last_heard = "claimed", worker["worker_id"], time.time()
                    job.changed.set()
                    return job
        return None

    def owned(self, worker: dict, job_id: str) -> WorkerJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job if job is not None and job.owner == worker["worker_id"] else None

    def depth(self, kind: str) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.kind == kind and j.state == "pending")


broker = _Broker()


def reset_for_tests() -> None:
    broker.reset()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter()
_NOT_FOUND = JSONResponse({"detail": "Not Found"}, status_code=404)


def _refusal(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "detail": detail}, status_code=status)


def _worker(authorization: str | None) -> dict | JSONResponse:
    if not enabled():
        return _NOT_FOUND
    worker = bearer_worker(authorization)
    if worker is None:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token is not recognised")
    return worker


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/api/workers/health")
def workers_health(authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    broker.seen(worker)
    return {"ok": True, "workerId": worker["worker_id"], "kind": worker["kind"],
            "maxResultBytes": MAX_RESULT_BYTES, "leaseSeconds": lease_seconds()}


@router.post("/api/workers/claim")
async def workers_claim(request: Request, authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    body = await _json(request)
    if str(body.get("workerId") or "") != worker["worker_id"] or body.get("kind") != worker["kind"]:
        return _refusal(403, "WORKER_MISMATCH", "this token belongs to another worker or kind")
    broker.seen(worker, body.get("telemetry"))
    job = broker.claim(worker)
    if job is None:
        return Response(status_code=204)
    return {"job": job.payload}


@router.post("/api/workers/jobs/{job_id}/progress")
async def workers_progress(job_id: str, request: Request,
                           authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    body = await _json(request)
    broker.seen(worker, body.get("telemetry"))
    job = broker.owned(worker, job_id)
    if job is None:
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    job.last_heard = time.time()
    job.phase = str(body.get("phase") or "")[:80]
    job.message = str(body.get("message") or "")[:300]
    job.changed.set()
    return {"ok": True}


@router.post("/api/workers/jobs/{job_id}/fail")
async def workers_fail(job_id: str, request: Request,
                       authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    body = await _json(request)
    broker.seen(worker)
    job = broker.owned(worker, job_id)
    if job is None:
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    if job.state == "claimed":
        # Only `false` exactly is a definite failure: anything else may have spent credits.
        job.ambiguous = body.get("ambiguous") is not False
        job.error = str(body.get("error") or "the worker reported a failure")[:500]
        job.state = "failed"
        job.changed.set()
    return {"ok": True}


@router.post("/api/workers/jobs/{job_id}/result")
async def workers_result(job_id: str, mediaType: str = Form(default=""),
                         creditsAfter: str = Form(default=""),
                         file: UploadFile | None = File(default=None),
                         authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    # Optional, the telemetry `credits` shape ({value, source, observedAt}) as a JSON form field.
    try:
        credits_after = json.loads(creditsAfter) if creditsAfter else None
    except ValueError:
        credits_after = None
    broker.seen(worker, {"credits": credits_after})
    media_type = mediaType.split(";")[0].strip().lower()
    if file is None or not media_type.startswith(f"{worker['kind']}/"):
        return _refusal(422, "RESULT_INVALID",
                        f"a result needs `file` and a stated {worker['kind']}/* `mediaType`")
    content = await file.read(MAX_RESULT_BYTES + 1)
    if len(content) > MAX_RESULT_BYTES:
        return _refusal(413, "RESULT_TOO_LARGE", f"a result may be at most {MAX_RESULT_BYTES} bytes")
    if not content:
        return _refusal(422, "RESULT_INVALID", "the uploaded file is empty")
    job = broker.owned(worker, job_id)
    if job is not None and job.state == "claimed":
        job.content, job.media_type, job.state = content, media_type, "succeeded"
        job.changed.set()
        return {"ok": True}
    # A late answer for a job the Gateway is holding: adopt it into the held row (first result
    # wins, `worker.settle_result`), so finished paid work is never thrown away.
    from . import runtime, worker as worker_module
    from .adapters.base import BackendError, BackendResult

    rows = runtime.store.jobs_for_upstream_batches([upstream_id(worker["worker_id"], job_id)])
    if not rows:
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    try:
        worker_module.settle_result(rows[0]["id"], BackendResult(content=content,
                                                                 media_type=media_type), runtime.store)
    except BackendError as exc:   # the same artifact check a live result meets
        return _refusal(422, "RESULT_INVALID", str(exc))
    if job is not None:
        broker.forget(job)
    return {"ok": True, "adopted": True}


def upstream_id(worker_id: str, job_id: str) -> str:
    """The checkpoint a claimed job carries: who has it and under which id. No secret in it."""
    return f"ext:{worker_id}:{job_id}"


# --- the owner's controls -----------------------------------------------------
def _admin_routes() -> None:
    from .admin import require_admin, require_local_app

    @router.get("/admin/api/extension-workers", dependencies=[Depends(require_admin)])
    def extension_workers_status():
        if not enabled():
            return _NOT_FOUND
        cutoff = time.time() - online_seconds()
        known: dict[str, dict[str, Any]] = {}
        for row in _rows().values():
            entry = known.setdefault(row.get("worker_id"), {"worker_id": row.get("worker_id"),
                                                            "kind": row.get("kind"),
                                                            "has_live_token": False})
            entry["has_live_token"] = entry["has_live_token"] or not row.get("revoked_at")
        for worker_id, seen in list(broker.workers.items()):
            entry = known.setdefault(worker_id, {"worker_id": worker_id, "kind": seen.get("kind"),
                                                 "has_live_token": False})
            entry.update(last_seen_at=seen.get("seen_at"), credits=seen.get("credits"),
                         extension_version=seen.get("extension_version"))
        for entry in known.values():
            entry.setdefault("last_seen_at", None)
            entry.setdefault("credits", None)   # not reported yet: unknown, never 0
            entry["online"] = (entry["last_seen_at"] or 0) >= cutoff
            entry["queue_depth"] = broker.depth(entry.get("kind") or "")
        return {"enabled": True, "lease_seconds": lease_seconds(),
                "online_seconds": online_seconds(), "max_result_bytes": MAX_RESULT_BYTES,
                "workers": sorted(known.values(), key=lambda e: str(e["worker_id"]))}

    @router.post("/admin/api/extension-workers", dependencies=[Depends(require_local_app)])
    async def extension_workers_mint(request: Request):
        if not enabled():
            return _NOT_FOUND
        body = await _json(request)
        try:
            token = mint(str(body.get("worker_id") or ""), str(body.get("kind") or ""))
        except ValueError as exc:
            return _refusal(400, "WORKER_INVALID", str(exc))
        # Shown once, to the app on this Mac only; never stored, logged or shown again.
        return {"ok": True, "worker_id": body.get("worker_id"), "token": token}

    @router.delete("/admin/api/extension-workers/{worker_id}",
                   dependencies=[Depends(require_local_app)])
    def extension_workers_revoke(worker_id: str):
        if not enabled():
            return _NOT_FOUND
        return {"ok": True, "revoked": revoke(worker_id)}



def install(app) -> None:
    """Mount the wire and the owner's controls. Called by `api` (admin is imported late: no cycle)."""
    _admin_routes()
    app.include_router(router)
