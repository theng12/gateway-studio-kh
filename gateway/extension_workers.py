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
import math
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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
    claimed_at: float | None = None
    last_heard: float = 0.0
    phase: str = ""
    message: str = ""
    content: bytes | None = None
    media_type: str | None = None
    error: str = ""
    ambiguous: bool = False
    changed: threading.Event = field(default_factory=threading.Event)


_SOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def valid_credits(credits: Any) -> dict | None:
    """`{value, source, observedAt}` when the worker stated all three well, else `None` (unknown).

    A finite, non-negative number; a short dotted source name (`flow.nzlxg`); an ISO-8601 time with
    a zone. Anything else — NaN/Infinity (which JSON-encode to nothing a browser can parse), a bool,
    a missing source or time — is not a statement, so it changes nothing.
    """
    if not isinstance(credits, dict):
        return None
    value, source, observed = credits.get("value"), credits.get("source"), credits.get("observedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0:
        return None
    if not isinstance(source, str) or not _SOURCE.match(source):
        return None
    if not isinstance(observed, str) or len(observed) > 40:
        return None
    try:
        stamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return {"value": value, "source": source, "observedAt": observed}


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
                credits = valid_credits(telemetry.get("credits"))
                # **Unknown is `None`, never 0**: only a well-formed statement replaces the last one.
                if credits is not None:
                    entry["credits"] = credits
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

    def hold(self, job: WorkerJob) -> bool:
        """Claimed → held, **only if no answer landed first**. `False`: re-read the job's state."""
        with self._lock:
            if job.state != "claimed":
                return False
            job.state = "held"
            return True

    def rehold(self, job: WorkerJob) -> None:
        """An ambiguous failure the adapter has turned into a hold: late answers go to the row."""
        with self._lock:
            if job.state == "failed":
                job.state = "held"

    def touch(self, worker: dict, job_id: str, phase: Any, message: Any) -> bool:
        """Progress from the job's owner renews its lease; `False` for anyone else's job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner != worker["worker_id"]:
                return False
            if job.state == "claimed":
                job.last_heard = time.time()
                job.phase = str(phase or "")[:80]
                job.message = str(message or "")[:300]
                job.changed.set()
            return True

    def answer(self, worker: dict, job_id: str, *, state: str, content: bytes | None = None,
               media_type: str | None = None, error: str = "", ambiguous: bool = False) -> str:
        """Give a claimed job its one answer. **The first answer wins**, under the lock.

        `live`: the adapter is waiting and takes it. `taken`: an answer already landed, this one is
        ignored. `late`: the Gateway is no longer waiting here (held, or forgotten by a restart);
        the caller settles the durable row. `missing`: another worker's job.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job.owner != worker["worker_id"]:
                return "missing"
            if job is None or job.state == "held":
                return "late"
            if job.state != "claimed":
                return "taken"
            job.content, job.media_type = content, media_type
            job.error, job.ambiguous, job.state = error, ambiguous, state
            job.changed.set()
            return "live"

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
                    job.state, job.owner = "claimed", worker["worker_id"]
                    job.last_heard = job.claimed_at = time.time()
                    job.changed.set()
                    return job
        return None

    def owned(self, worker: dict, job_id: str) -> WorkerJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job if job is not None and job.owner == worker["worker_id"] else None

    def jobs_of(self, worker_id: str) -> tuple[dict | None, list[str]]:
        """`(current_job, held_job_ids)` for the status page; what the Gateway still holds in memory."""
        with self._lock:
            mine = [j for j in self._jobs.values() if j.owner == worker_id]
            current = next(({"jobId": j.job_id, "phase": j.phase or None, "message": j.message or None,
                             "claimed_at": j.claimed_at, "last_heard_at": j.last_heard}
                            for j in mine if j.state == "claimed"), None)
            return current, sorted(j.job_id for j in mine if j.state == "held")

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
    if not broker.touch(worker, job_id, body.get("phase"), body.get("message")):
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    return {"ok": True}


def _held_row(worker: dict, job_id: str) -> dict | None:
    """The durable row whose **current** attempt is this worker's job, or `None`.

    Exact `upstream_job_id` only: an id that merely appears in an older attempt's log is not the
    job's answer any more, so a newer winner is never overwritten from here.
    """
    from . import runtime

    wanted = upstream_id(worker["worker_id"], job_id)
    rows = runtime.store.jobs_for_upstream_batches([wanted])
    return next((row for row in rows if row.get("upstream_job_id") == wanted), None)


@router.post("/api/workers/jobs/{job_id}/fail")
async def workers_fail(job_id: str, request: Request,
                       authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    body = await _json(request)
    broker.seen(worker, body.get("telemetry"))
    # Only `false` exactly is a definite failure: anything else may have spent credits.
    ambiguous = body.get("ambiguous") is not False
    error = str(body.get("error") or "the worker reported a failure")[:500]
    outcome = broker.answer(worker, job_id, state="failed", error=error, ambiguous=ambiguous)
    if outcome == "missing":
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    if outcome != "late":
        return {"ok": True}
    row = _held_row(worker, job_id)
    if row is None:
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    if ambiguous:
        return {"ok": True, "held": True}   # still unknown: the hold stays, never replayed
    # The answer the hold was waiting for: nothing was charged. `JobStore.fail` settles a hold
    # and leaves any other verdict (a success, a cancel, a closed hold) as it was.
    from . import runtime

    runtime.store.fail(row["id"], code="PROVIDER_FAILED", detail=error, retryable=False)
    held = broker.get(job_id)
    if held is not None:
        broker.forget(held)
    return {"ok": True, "settled": True}


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
    outcome = broker.answer(worker, job_id, state="succeeded", content=content, media_type=media_type)
    if outcome == "missing":
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    if outcome != "late":
        return {"ok": True}
    # A late answer for a job the Gateway is holding: adopt it into the held row (first result
    # wins, `worker.settle_result`), so finished paid work is never thrown away.
    from . import runtime, worker as worker_module
    from .adapters.base import BackendError, BackendResult

    row = _held_row(worker, job_id)
    if row is None:
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    try:
        worker_module.settle_result(row["id"], BackendResult(content=content, media_type=media_type),
                                    runtime.store)
    except BackendError as exc:   # the same artifact check a live result meets
        return _refusal(422, "RESULT_INVALID", str(exc))
    held = broker.get(job_id)
    if held is not None:
        broker.forget(held)
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
        for row in sorted(_rows().values(), key=lambda r: r.get("created_at") or 0):
            entry = known.setdefault(row.get("worker_id"), {"worker_id": row.get("worker_id"),
                                                            "kind": row.get("kind"),
                                                            "has_live_token": False})
            entry["has_live_token"] = entry["has_live_token"] or not row.get("revoked_at")
            # The newest token's dates: when this worker's key was made, and revoked (null = live).
            entry["created_at"], entry["revoked_at"] = row.get("created_at"), row.get("revoked_at")
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
            entry.setdefault("created_at", None)
            entry.setdefault("revoked_at", None)
            entry["current_job"], entry["held_jobs"] = broker.jobs_of(str(entry["worker_id"]))
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
