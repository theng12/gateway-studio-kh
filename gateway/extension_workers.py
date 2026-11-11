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

from . import admin_session, extension_accounts
from .config import APP_SUPPORT, get as config_get

SWITCH = "GATEWAY_EXTENSION_WORKERS"
#: The kinds a worker may serve. Video first (owner/Relay brief, 2026-11-09); image and audio added
#: alongside (same wire, same credit rules, per-kind token scoping).
KINDS = frozenset({"video", "image", "audio"})
#: The largest finished file a worker may upload (Flow clips run to 256 MB, handoff §8).
MAX_RESULT_BYTES = 256 * 1024 * 1024
TOKEN_PREFIX = "ekw_"
_ON = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return config_get(SWITCH, "").strip().lower() in _ON


def _seconds(key: str, default: float) -> float:
    try:
        value = float(config_get(key, "") or default)
        return value if math.isfinite(value) and value > 0 else default
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


def audio_inventory_max_bytes() -> int:
    """Bound the entire audio claim body, including its authenticated discovery snapshot."""
    try:
        value = int(config_get("GATEWAY_EXTENSION_WORKER_INVENTORY_MAX_BYTES", "") or 1048576)
        return value if 0 < value <= 2**53 - 1 else 1048576
    except ValueError:
        return 1048576


def audio_inventory(value: Any, now: float) -> dict:
    """Schema 1 proves current membership only; it states no compatible pairs or provider limits."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("audioInventory must use schema 1")
    providers = value.get("providers")
    if not isinstance(providers, dict) or set(providers) - {"fish", "elevenlabs_web"}:
        raise ValueError("audioInventory needs known providers")
    clean = {}
    for provider, data in providers.items():
        if not isinstance(data, dict) or data.get("source") != "live" or data.get("complete") is not True:
            raise ValueError("audio inventory must be a complete live observation")
        fetched = data.get("fetchedAt")
        if type(fetched) is not int or not 0 < fetched <= 2**53 - 1:
            raise ValueError("inventory fetchedAt must be a positive safe integer in milliseconds")
        if fetched > now * 1000 or now * 1000 - fetched >= online_seconds() * 1000:
            raise ValueError(f"inventory must be within the {online_seconds():g} second age limit, without a future timestamp")
        ids = {}
        for field_name in ("models", "voices"):
            rows = data.get(field_name)
            if not isinstance(rows, list) or not rows or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip()
                for row in rows
            ):
                raise ValueError(f"inventory {field_name} must contain exact nonempty IDs")
            ids[field_name] = frozenset(row["id"] for row in rows)
        clean[provider] = {**ids, "fetched_at": fetched / 1000}
    return clean


# ---------------------------------------------------------------------------
# Tokens: one per worker, bound to its id and kind, stored hashed, revocable
# ---------------------------------------------------------------------------
def _tokens_path():
    return APP_SUPPORT / "extension-workers.json"


def _rows() -> dict[str, dict]:
    raw = admin_session._read(_tokens_path()).get("workers")
    return raw if isinstance(raw, dict) else {}


def mint(worker_id: str, kind: str, *, worker_ids: list[str] | None = None) -> str:
    """A new token for this worker, **returned once** and stored only as its digest.

    Minting again for the same worker revokes its earlier token, so one worker has one key.
    """
    worker_id = str(worker_id or "").strip()
    if not worker_id or len(worker_id) > 120 or kind not in KINDS | {"manager"}:
        raise ValueError("a worker needs an id (at most 120 characters) and a known kind")
    if kind == "manager" and (not isinstance(worker_ids, list) or not worker_ids
            or len(worker_ids) > 128 or any(not isinstance(w, str) or not w or len(w) > 120 for w in worker_ids)):
        raise ValueError("a manager needs its explicit worker_ids scope (at most 128)")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = time.time()
    with admin_session._lock:
        rows = _rows()
        if any(row.get("worker_id") == worker_id and ((row.get("kind") == "manager") != (kind == "manager"))
               for row in rows.values()):
            raise ValueError("manager and execution-worker identities must be distinct")
        if kind == "manager":
            paired = {row["worker_id"] for row in rows.values()
                      if row.get("kind") in KINDS and not row.get("revoked_at")}
            if not set(worker_ids).issubset(paired):
                raise ValueError("manager scope must name already paired workers")
        for row in rows.values():
            if row.get("worker_id") == worker_id and not row.get("revoked_at"):
                row["revoked_at"] = now
        rows[admin_session._digest(token)] = {"worker_id": worker_id, "kind": kind,
                                              "created_at": now, "revoked_at": None,
                                              "dispatch_enabled": False,
                                              "credential_generation": uuid.uuid4().hex,
                                              **({"worker_ids": sorted(set(worker_ids))} if kind == "manager" else {})}
        admin_session._write(_tokens_path(), {"workers": rows})
        broker.clear_inventory(worker_id)
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
        broker.clear_inventory(worker_id)
    return count


def worker_for(token: str) -> dict | None:
    """The live worker this token belongs to, or `None`. Read per call, so a revoke is immediate."""
    token = str(token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    token_id = admin_session._digest(token)
    row = _rows().get(token_id)
    return {**row, "_token_id": token_id} if row and not row.get("revoked_at") else None


def _current_worker(worker: dict, rows: dict) -> bool:
    row = rows.get(worker.get("_token_id"))
    return bool(row and not row.get("revoked_at") and row.get("worker_id") == worker.get("worker_id")
                and row.get("kind") == worker.get("kind"))


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
    owner_token_id: str | None = None
    gateway_job_id: str = ""
    account_binding: dict | None = None
    reservation: dict | None = None
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

    def _entry(self, worker: dict) -> dict:
        entry = self.workers.get(worker["worker_id"])
        if entry is None or entry.get("_token_id") != worker["_token_id"]:
            entry = {"worker_id": worker["worker_id"], "kind": worker["kind"],
                     "_token_id": worker["_token_id"], "credits": None}
            self.workers[worker["worker_id"]] = entry
        return entry

    def seen(self, worker: dict, telemetry: Any = None) -> None:
        with admin_session._lock, self._lock:
            if not _current_worker(worker, _rows()):
                return
            entry = self._entry(worker)
            entry["seen_at"] = time.time()
            if isinstance(telemetry, dict):
                credits = valid_credits(telemetry.get("credits"))
                # **Unknown is `None`, never 0**: only a well-formed statement replaces the last one.
                if credits is not None:
                    entry["credits"] = credits
                version = telemetry.get("extensionVersion")
                if isinstance(version, str):
                    entry["extension_version"] = version[:40]

    def clear_inventory(self, worker_id: str, token_id: str | None = None) -> None:
        with self._lock:
            entry = self.workers.get(worker_id)
            if entry is not None and (token_id is None or entry.get("_token_id") == token_id):
                entry.pop("audio_inventory", None)

    def online(self, kind: str) -> bool:
        cutoff = time.time() - online_seconds()
        with admin_session._lock, self._lock:
            rows = _rows()
            return any(w.get("kind") == kind and w.get("seen_at", 0) >= cutoff
                       and _current_worker(w, rows) for w in self.workers.values())

    @staticmethod
    def _audio_member(entry: dict, payload: dict, now: float) -> bool:
        data = entry.get("audio_inventory", {}).get(payload.get("provider"), {})
        observed = data.get("fetched_at", 0)
        return (0 <= now - observed < online_seconds()
                and payload.get("model") in data.get("models", ())
                and payload.get("voice") in data.get("voices", ()))

    def audio_member(self, payload: dict) -> bool:
        """Discovery preflight only, never proof that the model/voice pair can be generated."""
        with admin_session._lock, self._lock:
            rows, now = _rows(), time.time()
            return any(_current_worker(entry, rows)
                       and entry.get("kind") == "audio"
                       and now - entry.get("seen_at", 0) < online_seconds()
                       and self._audio_member(entry, payload, now)
                       for entry in self.workers.values())

    def inventory_status(self, worker_id: str) -> dict:
        with admin_session._lock, self._lock:
            entry = self.workers.get(worker_id, {})
            if not _current_worker(entry, _rows()):
                return {}
            now = time.time()
            return {provider: {"observed_at": data["fetched_at"],
                               "fresh": 0 <= now - data["fetched_at"] < online_seconds(),
                               "model_count": len(data["models"]), "voice_count": len(data["voices"])}
                    for provider, data in entry.get("audio_inventory", {}).items()}

    def offer(self, kind: str, payload: dict[str, Any], *, gateway_job_id: str = "") -> WorkerJob:
        job = WorkerJob(job_id=f"job_{uuid.uuid4().hex}", kind=kind, payload=payload,
                        gateway_job_id=gateway_job_id)
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
            _settle_account(job.owner, job.owner_token_id, job.job_id, "held")
            job.state = "held"
            return True

    def take_failure(self, job: WorkerJob) -> str | None:
        """The adapter takes a failure, atomically: `held` (ambiguous; later answers go to the
        checkpointed row), `failed` (definite), or `None` when another answer replaced it first."""
        with self._lock:
            if job.state != "failed":
                return None
            if job.ambiguous:
                _settle_account(job.owner, job.owner_token_id, job.job_id, "held")
                job.state = "held"
                return "held"
            return "failed"

    def touch(self, worker: dict, job_id: str, phase: Any, message: Any) -> bool:
        """Progress from the job's owner renews its lease; `False` for anyone else's job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner != worker["worker_id"] or job.kind != worker["kind"] or job.owner_token_id != worker["_token_id"]:
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

        `live`: the adapter is waiting and takes it. `taken`: a definite answer already landed,
        this one is ignored. An **ambiguous** failure is not definite: until the adapter has taken
        it (`take_failure`), a result or a definite failure that follows replaces it, so finished
        paid work is never dropped behind "could not tell". `late`: the Gateway is no longer waiting here (held, or forgotten by a restart);
        the caller settles the durable row. `missing`: another worker's job.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and (job.owner != worker["worker_id"] or job.kind != worker["kind"] or job.owner_token_id != worker["_token_id"]):
                return "missing"
            if job is None or job.state == "held":
                return "late"
            if job.state != "claimed" and not (job.state == "failed" and job.ambiguous):
                return "taken"
            _settle_account(worker["worker_id"], worker["_token_id"], job_id,
                            "held" if ambiguous else state)
            job.content, job.media_type = content, media_type
            job.error, job.ambiguous, job.state = error, ambiguous, state
            job.changed.set()
            return "live"

    def forget(self, job: WorkerJob) -> None:
        with self._lock:
            self._jobs.pop(job.job_id, None)

    def report(self, worker: dict, body: dict) -> None:
        telemetry = body.get("telemetry")
        state = telemetry.get("state") if isinstance(telemetry, dict) else None
        normalized = extension_accounts.worker_state(state, now=time.time()) if state is not None else None
        if "account" in body:
            observation = extension_accounts.account(body["account"], now=time.time())
            if normalized is not None and any(observation.get(key) != value for key, value in normalized.items()):
                raise ValueError("account and telemetry.state must report the same worker state")
            self.observe_account(worker, body["account"])
        self.seen(worker, telemetry)
        if normalized is not None:
            with admin_session._lock, self._lock:
                if not _current_worker(worker, _rows()):
                    raise PermissionError("worker token revoked")
                entry = self._entry(worker)
                previous = entry.get("state")
                if previous is None or normalized["observed_at"] > previous["observed_at"]:
                    entry["state"], entry["state_received_at"] = normalized, time.time()

    def observe_account(self, worker: dict, value: Any) -> None:
        from . import runtime

        now = time.time()
        observation = extension_accounts.account(value, now=now)
        with admin_session._lock, self._lock:
            if not _current_worker(worker, _rows()):
                raise PermissionError("this worker token was revoked before observation")
            runtime.store.extension_account_observe(worker["worker_id"], worker["_token_id"],
                                                    observation, now=now)
            entry = self._entry(worker)
            previous_account, previous_state = entry.get("account"), entry.get("state")
            if previous_account is None or observation["observed_at"] > previous_account["observed_at"]:
                entry["account"] = observation
            if previous_state is None or observation["observed_at"] > previous_state["observed_at"]:
                entry["state"], entry["state_received_at"] = observation, now

    def claim(self, worker: dict, inventory: dict | None = None, quote: dict | None = None) -> WorkerJob | None:
        """A paid handoff is durable and account-reserved before the worker can execute it."""
        from . import runtime

        with admin_session._lock, self._lock:
            rows = _rows()
            if not _current_worker(worker, rows):
                raise PermissionError("this worker token was revoked before assignment")
            entry = self._entry(worker)
            entry.pop("quote_required", None)
            if worker["kind"] == "audio":
                entry["audio_inventory"] = inventory or {}
            for job in self._jobs.values():
                if job.owner == worker["worker_id"] and job.state == "claimed":
                    if job.kind != worker["kind"]:
                        return None
                    # Polling never re-authorizes execution, even on the same token.
                    raise ValueError("WORKER_BUSY: this worker already has an active job; report progress or its result")
            if rows[worker["_token_id"]].get("dispatch_enabled") is not True:
                return None
            account = entry.get("account")
            if not account:
                return None
            now = time.time()
            state = entry.get("state", account)
            revision = rows[worker["_token_id"]].get("control_revision")
            if revision is not None and state.get("control_revision") != revision:
                return None
            if (state["connection"] != "connected" or state["sign_in"] != "signed_in"
                    or state["readiness"] != "ready" or not state["enabled"] or state["paused"]
                    or state["activity"] not in ("idle", "waiting")
                    or not 0 <= now - state["observed_at"] < online_seconds()
                    or state["next_eligible_at"] is None or state["next_eligible_at"] > now):
                return None
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at):
                if job.state != "pending" or job.kind != worker["kind"]:
                    continue
                if job.kind == "audio" and not self._audio_member(entry, job.payload, now):
                    continue
                provider = job.payload.get("provider", "flow" if job.kind in ("video", "image") else None)
                if account["provider"] != provider or not job.gateway_job_id:
                    continue
                digest = extension_accounts.payload_sha256(job.payload)
                if quote is None:
                    entry["quote_required"] = {"job": job.payload, "payloadSha256": digest,
                                               "executionAuthorized": False}
                    return None
                if (quote["payload_sha256"] != digest or quote["provider"] != account["provider"]
                        or quote["account_id"] != account["account_id"]):
                    continue
                reservation = runtime.store.extension_account_reserve(
                    worker["worker_id"], worker["_token_id"], job_id=job.gateway_job_id,
                    worker_job_id=job.job_id, payload_sha256=digest, quote=quote,
                    now=now, max_age=online_seconds())
                if reservation is None:
                    return None
                job.account_binding = {"provider": account["provider"], "accountId": account["account_id"]}
                job.reservation = {"payloadSha256": digest, "value": quote["value"], "unit": quote["unit"]}
                job.state, job.owner = "claimed", worker["worker_id"]
                job.owner_token_id = worker["_token_id"]
                job.last_heard = job.claimed_at = now
                job.changed.set()
                return job
        return None

    def owned(self, worker: dict, job_id: str) -> WorkerJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job if (job is not None and job.owner == worker["worker_id"]
                           and job.kind == worker["kind"] and job.owner_token_id == worker["_token_id"]) else None

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
    if worker is None or worker.get("kind") not in KINDS:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token is not recognised")
    return worker


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _settle_account(worker_id: str | None, token_id: str | None, job_id: str, outcome: str) -> None:
    from . import runtime

    if worker_id and token_id:
        runtime.store.extension_account_settle(worker_id, token_id, job_id,
                                               outcome=outcome, now=time.time())


def worker_control(worker: dict) -> dict:
    row = _rows().get(worker["_token_id"], {})
    return {"credentialGeneration": row.get("credential_generation"),
            "dispatchEnabled": row.get("dispatch_enabled") is True,
            "closeRequested": row.get("close_requested") is True,
            "revision": row.get("control_revision"), "requestedAt": row.get("control_requested_at"),
            "pacing": row.get("pacing", {"minSeconds": 5, "maxSeconds": 15})}


async def _account_body(request: Request) -> dict:
    limit, raw = audio_inventory_max_bytes(), bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise OverflowError(f"the complete worker claim/telemetry exceeds the {limit} byte limit")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("invalid worker JSON") from None
    if not isinstance(body, dict):
        raise ValueError("worker body must be an object")
    return body


def _manager(authorization: str | None) -> dict | JSONResponse:
    if not enabled():
        return _NOT_FOUND
    identity = bearer_worker(authorization)
    if identity is None or identity.get("kind") != "manager":
        return _refusal(401, "MANAGER_TOKEN_REFUSED", "a paired scoped manager token is required")
    return identity


def manager_view(worker_ids: list[str]) -> dict:
    from . import runtime

    now, scope = time.time(), set(worker_ids)
    accounts = []
    for observed in runtime.store.extension_account_status(now=now, max_age=online_seconds()):
        if not scope.intersection(observed["workers"]):
            continue
        accounts.append({**{key: value for key, value in observed.items() if key != "bindings"},
                         "workers": sorted(scope.intersection(observed["workers"]))})
    workers = []
    with admin_session._lock, broker._lock:
        rows = _rows()
        for worker_id in sorted(scope):
            row = next((r for r in reversed(list(rows.values())) if r["worker_id"] == worker_id
                        and r["kind"] in KINDS and not r.get("revoked_at")), None)
            entry = broker.workers.get(worker_id, {})
            current_entry = row is not None and _current_worker(entry, rows)
            state = entry.get("state") if current_entry else None
            received = entry.get("state_received_at") if current_entry else None
            fresh = bool(state and received is not None
                         and 0 <= now - state["observed_at"] < online_seconds()
                         and 0 <= now - received < online_seconds())
            control = {"credentialGeneration": row.get("credential_generation") if row else None,
                       "dispatchEnabled": bool(row and row.get("dispatch_enabled") is True),
                       "closeRequested": bool(row and row.get("close_requested") is True),
                       "revision": row.get("control_revision") if row else None,
                       "requestedAt": row.get("control_requested_at") if row else None,
                       "pacing": row.get("pacing", {"minSeconds": 5, "maxSeconds": 15}) if row else None}
            mine = [j for j in broker._jobs.values() if j.owner == worker_id]
            active = next(({"jobId": j.job_id, "phase": j.phase or None,
                            "claimed_at": j.claimed_at, "last_heard_at": j.last_heard}
                           for j in mine if j.state == "claimed"), None)
            held = sorted(j.job_id for j in mine if j.state == "held" or (j.state == "failed" and j.ambiguous))
            durable_busy = runtime.store.extension_account_worker_busy(worker_id)
            observed_account = entry.get("account") if current_entry else None
            workers.append({"worker_id": worker_id, "kind": row.get("kind") if row else None,
                            "has_live_token": row is not None, "control": control,
                            "state": state, "receivedAt": received, "fresh": fresh,
                            "current_job": active, "held_jobs": held, "durableBusy": durable_busy,
                            "safeToClose": bool(fresh and control["credentialGeneration"]
                                and not control["dispatchEnabled"]
                                and control["revision"] and state["paused"] and state.get("paused_observed") is True
                                and state["activity"] in ("idle", "cooling", "resting", "waiting")
                                and state["control_revision"] == control["revision"]
                                and active is None and not held and not durable_busy),
                            "accountBinding": {"provider": observed_account["provider"],
                                               "accountId": observed_account["account_id"]} if observed_account else None})
    return {"schema": 1, "receivedAt": now, "maxAgeSeconds": online_seconds(),
            "workers": workers, "accounts": accounts}


@router.get("/api/workers/manager")
def workers_manager(authorization: str | None = Header(default=None)):
    manager = _manager(authorization)
    if isinstance(manager, JSONResponse):
        return manager
    return manager_view(manager["worker_ids"])


@router.post("/api/workers/manager/control")
async def workers_manager_control(request: Request, authorization: str | None = Header(default=None)):
    manager = _manager(authorization)
    if isinstance(manager, JSONResponse):
        return manager
    try:
        body = await _account_body(request)
        worker_ids = body.get("worker_ids")
        if (not isinstance(worker_ids, list) or not worker_ids or len(worker_ids) > 128
                or any(not isinstance(w, str) or w not in manager["worker_ids"] for w in worker_ids)):
            return _refusal(403, "MANAGER_SCOPE_REFUSED", "control only the exact paired worker scope")
        dispatch, close = body.get("dispatchEnabled"), body.get("closeRequested", False)
        pacing_only = "pacing" in body and "dispatchEnabled" not in body and "closeRequested" not in body
        if not pacing_only and (type(dispatch) is not bool or type(close) is not bool or (close and dispatch)):
            raise ValueError("control needs dispatchEnabled boolean or pacing only; close requires dispatch disabled")
        selected_pacing = extension_accounts.pacing(body["pacing"]) if "pacing" in body else None
        with admin_session._lock:
            rows = _rows()
            if not _current_worker(manager, rows):
                return _refusal(401, "MANAGER_TOKEN_REFUSED", "this manager token was revoked")
            revision, requested = uuid.uuid4().hex, time.time()
            for row in rows.values():
                if row["worker_id"] in worker_ids and row["kind"] in KINDS and not row.get("revoked_at"):
                    if not pacing_only:
                        row.update(dispatch_enabled=dispatch, close_requested=close)
                    row.update(control_revision=revision, control_requested_at=requested)
                    if selected_pacing is not None:
                        row["pacing"] = selected_pacing
            admin_session._write(_tokens_path(), {"workers": rows})
    except ValueError as exc:
        return _refusal(422, "MANAGER_CONTROL_INVALID", str(exc))
    except OverflowError as exc:
        return _refusal(413, "MANAGER_CONTROL_INVALID", str(exc))
    return manager_view(manager["worker_ids"])


@router.post("/api/workers/telemetry")
async def workers_telemetry(request: Request, authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    try:
        body = await _account_body(request)
        if body.get("workerId") != worker["worker_id"] or body.get("kind") != worker["kind"]:
            return _refusal(403, "WORKER_MISMATCH", "this token belongs to another worker or kind")
        broker.report(worker, body)
    except ValueError as exc:
        return _refusal(422, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except OverflowError as exc:
        return _refusal(413, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except PermissionError:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token was revoked")
    return {"ok": True, "receivedAt": time.time(), "control": worker_control(worker)}


@router.get("/api/workers/health")
def workers_health(authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    broker.seen(worker)
    age = online_seconds()
    return {"ok": True, "workerId": worker["worker_id"], "kind": worker["kind"],
            "maxResultBytes": MAX_RESULT_BYTES, "leaseSeconds": lease_seconds(),
            "onlineSeconds": age, "audioInventoryMaxAgeSeconds": age,
            "audioInventoryMaxBytes": audio_inventory_max_bytes(),
            "accountSchema": 1, "accountMaxAgeSeconds": age,
            "claimMaxBytes": audio_inventory_max_bytes(), "control": worker_control(worker)}


@router.post("/api/workers/claim")
async def workers_claim(request: Request, authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    inventory = None
    if worker["kind"] == "audio":
        broker.clear_inventory(worker["worker_id"], worker["_token_id"])
    try:
        body = await _account_body(request)
        if worker["kind"] == "audio":
            inventory = audio_inventory(body.get("audioInventory", {"schema": 1, "providers": {}}), time.time())
        elif "audioInventory" in body:
            raise ValueError("audioInventory requires an audio token")
    except ValueError as exc:
        return _refusal(422, "WORKER_INVENTORY_INVALID", str(exc))
    except OverflowError as exc:
        return _refusal(413, "WORKER_INVENTORY_INVALID", str(exc))
    if str(body.get("workerId") or "") != worker["worker_id"] or body.get("kind") != worker["kind"]:
        return _refusal(403, "WORKER_MISMATCH", "this token belongs to another worker or kind")
    try:
        broker.report(worker, body)
        selected_quote = extension_accounts.quote(body.get("quote"), now=time.time())
        if selected_quote is not None and "account" not in body:
            raise ValueError("an executable claim needs its actual account observation in the same request")
        job = broker.claim(worker, inventory, selected_quote)
    except PermissionError:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token was revoked before assignment")
    except ValueError as exc:
        return _refusal(409, "ACCOUNT_CLAIM_REFUSED", str(exc))
    if job is None:
        preview = broker.workers.get(worker["worker_id"], {}).get("quote_required")
        if preview:
            return {"job": None, "quoteRequired": preview, "control": worker_control(worker)}
        return Response(status_code=204)
    return {"job": job.payload, "accountBinding": job.account_binding,
            "reservation": job.reservation, "control": worker_control(worker)}


@router.post("/api/workers/jobs/{job_id}/progress")
async def workers_progress(job_id: str, request: Request,
                           authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    try:
        body = await _account_body(request)
        broker.report(worker, body)
    except ValueError as exc:
        return _refusal(422, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except OverflowError as exc:
        return _refusal(413, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except PermissionError:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token was revoked")
    if not broker.touch(worker, job_id, body.get("phase"), body.get("message")):
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    return {"ok": True}


def _held_row(worker: dict, job_id: str) -> dict | None:
    """The durable row whose **current** attempt is this worker's job, or `None`.

    Exact `upstream_job_id` only: an id that merely appears in an older attempt's log is not the
    job's answer any more, so a newer winner is never overwritten from here.
    """
    from . import runtime

    if (runtime.store.extension_account_has_reservation(worker["worker_id"], job_id)
            and runtime.store.extension_account_reservation(worker["worker_id"], worker["_token_id"], job_id) is None):
        return None
    wanted = upstream_id(worker["worker_id"], job_id)
    rows = runtime.store.jobs_for_upstream_batches([wanted])
    modality = "tts" if worker["kind"] == "audio" else worker["kind"]
    return next((row for row in rows if row.get("upstream_job_id") == wanted
                 and row.get("modality") == modality), None)


@router.post("/api/workers/jobs/{job_id}/fail")
async def workers_fail(job_id: str, request: Request,
                       authorization: str | None = Header(default=None)):
    worker = _worker(authorization)
    if isinstance(worker, JSONResponse):
        return worker
    try:
        body = await _account_body(request)
        broker.report(worker, body)
    except ValueError as exc:
        return _refusal(422, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except OverflowError as exc:
        return _refusal(413, "ACCOUNT_OBSERVATION_INVALID", str(exc))
    except PermissionError:
        return _refusal(401, "WORKER_TOKEN_REFUSED", "this worker token was revoked")
    # Only `false` exactly is a definite failure: anything else may have spent credits.
    ambiguous = body.get("ambiguous") is not False
    error = str(body.get("error") or "the worker reported a failure")[:500]
    outcome = broker.answer(worker, job_id, state="failed", error=error, ambiguous=ambiguous)
    if outcome == "missing":
        return _refusal(404, "JOB_NOT_FOUND", "no job of this worker has that id")
    if outcome == "taken":
        return {"ok": True, "ignored": True}   # a definite answer already settled this job
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
    _settle_account(worker["worker_id"], worker["_token_id"], job_id, "failed")
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
    if outcome == "taken":
        return {"ok": True, "ignored": True}   # a definite answer already settled this job
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
    _settle_account(worker["worker_id"], worker["_token_id"], job_id, "succeeded")
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
            if row.get("kind") == "manager":
                continue
            entry = known.setdefault(row.get("worker_id"), {"worker_id": row.get("worker_id"),
                                                            "kind": row.get("kind"),
                                                            "has_live_token": False})
            entry["has_live_token"] = entry["has_live_token"] or not row.get("revoked_at")
            # The newest token's dates: when this worker's key was made, and revoked (null = live).
            entry["created_at"], entry["revoked_at"] = row.get("created_at"), row.get("revoked_at")
            entry["kind"] = row.get("kind")
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
            if entry["kind"] == "audio":
                entry["audio_inventory"] = broker.inventory_status(str(entry["worker_id"]))
                entry["audio_admission_ready"] = False
                entry["audio_admission_reason"] = "Discovery does not establish compatible model/voice pairs or required provider limits"
        view = manager_view(list(known))
        controls = {entry["worker_id"]: entry for entry in view["workers"]}
        return {"enabled": True, "lease_seconds": lease_seconds(),
                "online_seconds": online_seconds(), "max_result_bytes": MAX_RESULT_BYTES,
                "accounts": view["accounts"],
                "workers": [{**entry, **controls.get(entry["worker_id"], {})}
                            for entry in sorted(known.values(), key=lambda e: str(e["worker_id"]))]}

    @router.post("/admin/api/extension-workers/manager", dependencies=[Depends(require_local_app)])
    async def extension_manager_mint(request: Request):
        if not enabled():
            return _NOT_FOUND
        body = await _json(request)
        try:
            manager_id = str(body.get("manager_id") or "")
            token = mint(manager_id, "manager", worker_ids=body.get("worker_ids"))
        except ValueError as exc:
            return _refusal(422, "MANAGER_INVALID", str(exc))
        return {"ok": True, "manager_id": manager_id, "token": token}

    @router.put("/admin/api/extension-workers/accounts/enabled", dependencies=[Depends(require_local_app)])
    async def extension_account_enabled(request: Request):
        if not enabled():
            return _NOT_FOUND
        from . import runtime

        body = await _json(request)
        if type(body.get("enabled")) is not bool:
            return _refusal(422, "ACCOUNT_CONTROL_INVALID", "enabled must be an explicit boolean")
        try:
            changed = runtime.store.extension_account_set_enabled(body.get("provider"), body.get("account_id"), body["enabled"])
        except ValueError as exc:
            return _refusal(422, "ACCOUNT_CONTROL_INVALID", str(exc))
        if not changed:
            return _refusal(404, "ACCOUNT_NOT_FOUND", "observe the actual account before enabling it")
        return {"ok": True, "enabled": body["enabled"]}

    @router.get("/admin/api/extension-workers/accounts/history", dependencies=[Depends(require_admin)])
    def extension_account_history(provider: str, account_id: str):
        if not enabled():
            return _NOT_FOUND
        from . import runtime

        return {"history": runtime.store.extension_account_history(provider, account_id),
                "jobs": runtime.store.extension_account_reservations(provider=provider, account_id=account_id)}

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
