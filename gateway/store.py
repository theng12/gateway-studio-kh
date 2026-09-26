"""Durable job store — SQLite, single process, no broker.

GenStudio drives progress with in-process `asyncio` periodic loops and has no
worker process, broker or cron. Its job tables carry fencing tokens, lease
expiry, admission permits and failover columns *because multiple replicas and a
fleet of sites must be safe*. None of that applies here.

What we keep is the part that actually earns its place:

  * a durable row per job, so a restart does not lose work;
  * `next_action_at`, which is the entire scheduling primitive;
  * a UNIQUE idempotency key, because re-POSTing with the same key is Story
    Studio's reattach path after a network failure -- returning a *new* job
    there would double-generate.

What we drop: leases, fencing, permits, fairness ranking, admission capacity.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
UNKNOWN_OUTCOME_CODE = "UNKNOWN_OUTCOME"
RECOVERY_UNKNOWN = "unknown_outcome"
RECOVERY_RETRYABLE = "retryable"
CANCEL_REQUESTED = "requested"
CANCEL_CONFIRMED = "confirmed"
_INPUT_PARAM_KEYS = ("_upload_path",)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    idempotency_key  TEXT UNIQUE,
    modality         TEXT NOT NULL,
    model_id         TEXT,
    provider_id      TEXT,
    state            TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '{}',
    asset_path       TEXT,
    media_type       TEXT,
    result_json      TEXT,
    error_code       TEXT,
    error_detail     TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    next_action_at   REAL,
    retryable        INTEGER NOT NULL DEFAULT 0,
    recovery_state   TEXT,
    recovery_detail  TEXT,
    cancel_requested_at REAL,
    cancel_state     TEXT,
    dispatch_started_at REAL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    upstream_job_id  TEXT,
    late_result_state TEXT,
    late_error_code  TEXT,
    late_error_detail TEXT,
    caller           TEXT,
    defer_reason     TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state_next ON jobs(state, next_action_at);
"""

# Columns added after the first release. Applied idempotently so an existing
# Phase 0 database keeps working instead of needing to be deleted.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider_id", "TEXT"),
    ("result_json", "TEXT"),
    ("artifacts_json", "TEXT"),
    ("retryable", "INTEGER NOT NULL DEFAULT 0"),
    ("recovery_state", "TEXT"),
    ("recovery_detail", "TEXT"),
    ("cancel_requested_at", "REAL"),
    ("cancel_state", "TEXT"),
    ("dispatch_started_at", "REAL"),
    ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("upstream_job_id", "TEXT"),
    ("late_result_state", "TEXT"),
    ("late_error_code", "TEXT"),
    ("late_error_detail", "TEXT"),
    # What the upstream answered when asked to stop a dispatched job. Kept
    # separate from `cancel_state`, which records *our* lifecycle: "the Hub
    # signalled the worker" and "dispatch never began" are different claims, and
    # collapsing them is how a requested cancel becomes a reported remote stop.
    ("cancel_remote", "TEXT"),
    # The upstream's own identifiers for work whose outcome is unknown — batch,
    # item, worker, machine and the routes that resolve it. Structured rather than
    # only prose, because an operator action has to name the exact batch and item
    # it is resolving, and parsing them back out of a sentence would make the
    # action depend on the wording of a message.
    ("recovery_context", "TEXT"),
    # What the upstream answered when an operator resolved a recovery hold.
    ("discard_remote", "TEXT"),
    # Who asked for the job, as far as the request could say. Not an identity —
    # there is one API key and every caller shares it — but the difference between
    # Story Studio and a shell loop, which is the question an operator actually
    # asks at 2am when a batch failed. Absent means the caller said nothing, and
    # that stays distinguishable from a caller that named itself.
    ("caller", "TEXT"),
    # Why a job is back in the queue instead of running. **A busy fleet is not a
    # broken job**: a site with no free machine right now will have one shortly,
    # and the caller asked for work rather than for a verdict on the fleet's
    # occupancy. Cleared on dispatch, because a running job is not waiting.
    ("defer_reason", "TEXT"),
)


def artifacts_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Every artifact a job kept, first one first. The one reader of the list.

    `asset_path`/`media_type` remain the *first* artifact because every
    single-artifact route serves them, and a job written before
    `artifacts_json` existed only has those. Both spellings are folded here so a
    caller never has to know which one a given job happens to carry — and so
    "this job produced one artifact" and "I could not work out how many" cannot
    produce the same value.
    """
    raw = job.get("artifacts_json")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict) and r.get("path")]
            if rows:
                return rows
    if job.get("asset_path"):
        return [
            {
                "asset_id": "0",
                "path": job["asset_path"],
                "media_type": job.get("media_type"),
            }
        ]
    return []


def input_paths_of(job: dict[str, Any]) -> set[str]:
    """Return staged input paths held by a job, without interpreting other params."""
    raw_params = job.get("params")
    if isinstance(raw_params, dict):
        params = raw_params
    else:
        try:
            params = json.loads(raw_params or "{}")
        except (TypeError, ValueError):
            return set()
    if not isinstance(params, dict):
        return set()
    return {
        str(params[key])
        for key in _INPUT_PARAM_KEYS
        if isinstance(params.get(key), str) and params[key]
    }


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._recover_interrupted()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if "retryable" in result:
            result["retryable"] = bool(result["retryable"])
        result["cancel_requested"] = result.get("cancel_requested_at") is not None
        result["recovery_required"] = bool(result.get("recovery_state"))
        # A JSON column we cannot read is "unknown", never a confirmation: an
        # unreadable cancel must not read as a remote stop, and an unreadable
        # recovery context must not look like a resolvable hold.
        for column in ("cancel_remote", "recovery_context", "discard_remote"):
            raw = result.get(column)
            if raw == "":
                result[column] = None
            elif isinstance(raw, str):
                try:
                    result[column] = json.loads(raw)
                except ValueError:
                    result[column] = None
        return result

    def _recover_interrupted(self) -> int:
        """Close the unsafe restart window without ever replaying in-flight work.

        **The upstream attempt is recorded, not only described.** The sweep used to
        leave `recovery_context` null and name the batch only inside the prose, so
        the operator's discard action had nothing to aim at: `discard-uncertain`
        answered `RECOVERY_CONTEXT_MISSING` forever and the Jobs window offered no
        button, which left a restart-interrupted job unresolvable by anyone short
        of editing the database. The context is written in the same shape the Hub
        adapter uses, with the item index defaulted the way the discard path always
        has — this gateway submits one-item batches, so item 0 is the batch's only
        item — and marked so a reader can tell a reconstructed identity from one
        the Hub itself stated.
        """
        rows = self._conn.execute(
            "SELECT id, upstream_job_id FROM jobs WHERE state = 'running'"
        ).fetchall()
        if not rows:
            return 0
        now = time.time()
        for row in rows:
            upstream = str(row["upstream_job_id"] or "")
            suffix = f" Upstream job id: {upstream}." if upstream else ""
            detail = (
                "The gateway restarted while this job was in flight, so its upstream "
                "outcome is unknown. Reconcile the upstream request before retrying; "
                "the gateway will not dispatch it again automatically."
                + suffix
            )
            context = (
                json.dumps({
                    "upstream_job_id": upstream,
                    "upstream_item_index": 0,
                    "recorded_by": "gateway_restart",
                })
                if upstream
                else None
            )
            self._conn.execute(
                """
                UPDATE jobs
                   SET state = 'failed', error_code = ?, error_detail = ?,
                       retryable = 0, recovery_state = ?, recovery_detail = ?,
                       recovery_context = ?,
                       next_action_at = NULL, updated_at = ?
                 WHERE id = ? AND state = 'running'
                """,
                (
                    UNKNOWN_OUTCOME_CODE,
                    detail,
                    RECOVERY_UNKNOWN,
                    detail,
                    context,
                    now,
                    row["id"],
                ),
            )
        return len(rows)

    def close(self) -> None:
        self._conn.close()

    # -- write ------------------------------------------------------------
    def submit(
        self,
        *,
        modality: str,
        model_id: str,
        params: dict[str, Any],
        idempotency_key: str | None,
        provider_id: str | None = None,
        caller: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a job, or return the existing one for a repeated key.

        Returns `(row, created)`. `created=False` means this was an idempotent
        replay and the caller must NOT start a second generation.
        """
        now = time.time()
        with self._lock:
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_dict(existing), False

            job_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO jobs (id, idempotency_key, modality, model_id, provider_id,
                                  state, params, created_at, updated_at, next_action_at, caller)
                VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    modality,
                    model_id,
                    provider_id,
                    json.dumps(params),
                    now,
                    now,
                    now + 0.8,
                    (caller or "").strip()[:200] or None,
                ),
            )
            self._conn.commit()
            return self.get(job_id), True

    def succeed(
        self,
        job_id: str,
        *,
        asset_path: str | None = None,
        media_type: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            existing = dict(row)
            fields: dict[str, Any] = {}
            merged = self._merged_artifacts(existing, artifacts)
            if artifacts is not None:
                fields["artifacts_json"] = json.dumps(merged)
            if asset_path is not None:
                fields["asset_path"] = asset_path
            elif merged and not existing.get("asset_path"):
                fields["asset_path"] = merged[0].get("path")
            if media_type is not None:
                fields["media_type"] = media_type
            elif merged and not existing.get("media_type"):
                fields["media_type"] = merged[0].get("media_type")
            if result is not None:
                fields["result_json"] = json.dumps(result)

            if existing["state"] in TERMINAL_STATES:
                if existing["state"] != "succeeded":
                    fields["late_result_state"] = "succeeded"
                self._update_locked(job_id, fields)
            else:
                fields.update(
                    state="succeeded",
                    next_action_at=None,
                    retryable=0,
                    recovery_state=None,
                    recovery_detail=None,
                )
                if existing.get("cancel_requested_at") is not None:
                    fields["cancel_state"] = "completed_after_request"
                self._update_locked(job_id, fields)
            self._conn.commit()

    def fail(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
        retryable: bool = False,
        recovery_state: str | None = None,
        recovery_detail: str | None = None,
        recovery_context: dict[str, Any] | None = None,
    ) -> None:
        """Settle once, retaining a late failure without reopening a terminal row."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            if row["state"] in TERMINAL_STATES:
                self._update_locked(
                    job_id,
                    {
                        "late_result_state": "failed",
                        "late_error_code": code,
                        "late_error_detail": detail,
                    },
                )
                self._conn.commit()
                return
            if recovery_state is None and retryable:
                recovery_state = RECOVERY_RETRYABLE
            self._update_locked(
                job_id,
                {
                    "state": "failed",
                    "error_code": code,
                    "error_detail": detail,
                    "retryable": int(bool(retryable)),
                    "recovery_state": recovery_state,
                    "recovery_detail": recovery_detail,
                    "recovery_context": (
                        json.dumps(recovery_context) if recovery_context else None
                    ),
                    "next_action_at": None,
                },
            )
            self._conn.commit()

    def record_discard_remote(self, job_id: str, answer: dict[str, Any]) -> bool:
        """Persist what the upstream answered when an operator resolved a hold."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            self._update_locked(job_id, {"discard_remote": json.dumps(answer)})
            self._conn.commit()
            return True

    def close_recovery_hold(
        self, job_id: str, *, code: str, detail: str
    ) -> dict[str, Any] | None:
        """Mark a hold resolved by decision, so it stops asking to be reconciled.

        The job stays terminal and keeps its failure — what changes is that the
        *unknown* is gone, because an operator abandoned the attempt. Clearing
        `recovery_state` is what takes it off the recovery list; leaving it set
        after a successful discard would keep telling an operator to reconcile
        work that no longer exists to reconcile.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            self._update_locked(
                job_id,
                {
                    "error_code": code,
                    "error_detail": detail,
                    "retryable": 0,
                    "recovery_state": None,
                    "recovery_detail": None,
                    "recovery_context": None,
                },
            )
            self._conn.commit()
            return self._row_dict(
                self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            )

    def mark_running(self, job_id: str) -> bool:
        """Claim only work still accepted, not a cancelled row in an old snapshot."""
        with self._lock:
            result = self._conn.execute(
                """UPDATE jobs SET state = 'running', next_action_at = NULL,
                                   dispatch_started_at = NULL,
                                   attempt_count = attempt_count + 1,
                                   defer_reason = NULL,
                                   updated_at = ?
                   WHERE id = ? AND state = 'accepted'
                     AND cancel_requested_at IS NULL""",
                (time.time(), job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    def defer(self, job_id: str, *, delay_s: float, reason: str) -> bool:
        """Put an accepted-nothing job back in the queue instead of failing it.

        **A busy fleet is not a broken job.** A site with no free machine right now
        will have one shortly, and the caller asked for work — not for a verdict on
        the fleet's occupancy. Measured 2026-09-25: ninety-three of a hundred and
        thirty-five chat jobs failed inside half an hour, most of them
        `MACHINE_BUSY` against the one location that had the model. The gateway had
        nowhere else to route and settled each one as a failure, which is a lie
        about the work rather than a report about the fleet.

        `next_action_at` is the whole mechanism — it is the scheduling primitive
        `due()` already reads — so a deferred job comes back on its own.

        **The caller must only use this when the upstream accepted nothing.** A
        request that may have reached a worker is a durable recovery hold and never
        an automatic retry; this method has no opinion on that, which is why it is
        not called from the unknown-outcome path.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return False
            self._update_locked(
                job_id,
                {
                    "state": "accepted",
                    "next_action_at": time.time() + max(float(delay_s), 0.0),
                    "defer_reason": reason[:300],
                },
            )
            self._conn.commit()
            return True

    def begin_dispatch(self, job_id: str) -> bool:
        """Open the dispatch window only while a running job has no cancel request."""
        with self._lock:
            result = self._conn.execute(
                """
                UPDATE jobs
                   SET dispatch_started_at = ?, updated_at = ?
                 WHERE id = ? AND state = 'running'
                   AND cancel_requested_at IS NULL
                   AND dispatch_started_at IS NULL
                """,
                (time.time(), time.time(), job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    def confirm_cancel(self, job_id: str) -> bool:
        """Confirm a running cancellation only if upstream dispatch never began."""
        with self._lock:
            result = self._conn.execute(
                """
                UPDATE jobs
                   SET state = 'cancelled', cancel_state = ?, next_action_at = NULL,
                       updated_at = ?
                 WHERE id = ? AND state = 'running'
                   AND cancel_requested_at IS NOT NULL
                   AND dispatch_started_at IS NULL
                """,
                (CANCEL_CONFIRMED, time.time(), job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    def record_cancel_remote(self, job_id: str, answer: dict[str, Any]) -> bool:
        """Persist what the upstream answered when asked to stop this job.

        Recorded, never interpreted: the reader decides what it means, and the
        absence of a confirmation stays visible as `ok: false` rather than
        becoming a silent success.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            self._update_locked(job_id, {"cancel_remote": json.dumps(answer)})
            self._conn.commit()
            return True

    def record_upstream_acceptance(self, job_id: str, upstream_job_id: str) -> bool:
        """Persist an adapter's id immediately after the remote accepts work."""
        identifier = str(upstream_job_id or "").strip()
        if not identifier:
            return False
        with self._lock:
            result = self._conn.execute(
                """
                UPDATE jobs SET upstream_job_id = ?, updated_at = ?
                 WHERE id = ? AND state = 'running'
                """,
                (identifier, time.time(), job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    def record_artifacts(
        self, job_id: str, artifacts: list[dict[str, Any]]
    ) -> bool:
        """Checkpoint output metadata before terminal settlement."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            existing = dict(row)
            merged = self._merged_artifacts(existing, artifacts)
            fields: dict[str, Any] = {"artifacts_json": json.dumps(merged)}
            if merged and not existing.get("asset_path"):
                fields["asset_path"] = merged[0].get("path")
            if merged and not existing.get("media_type"):
                fields["media_type"] = merged[0].get("media_type")
            if existing["state"] in TERMINAL_STATES and existing["state"] != "succeeded":
                fields["late_result_state"] = "succeeded"
            self._update_locked(job_id, fields)
            self._conn.commit()
            return True

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None  # type: ignore[return-value]
            if row["state"] == "accepted":
                self._update_locked(
                    job_id,
                    {
                        "state": "cancelled",
                        "cancel_state": CANCEL_CONFIRMED,
                        "next_action_at": None,
                    },
                )
            elif row["state"] == "running":
                self._update_locked(
                    job_id,
                    {
                        "cancel_requested_at": time.time(),
                        "cancel_state": CANCEL_REQUESTED,
                    },
                )
            self._conn.commit()
            return self._row_dict(
                self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            )

    def record_route(self, job_id: str, provider_id: str) -> bool:
        """Point a queued job at a different location after a site refusal.

        The model is unchanged; only the controller that will run it moved. The
        published model id is location-independent (`fleet.publish_id` collapses
        every site to the maker), so nothing the caller sees changes — only the row
        that records which site actually took the work.
        """
        self._update(job_id, provider_id=provider_id)
        return True

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        with self._lock:
            self._update_locked(job_id, fields)
            self._conn.commit()

    def _update_locked(self, job_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        fields = {**fields, "updated_at": time.time()}
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ?",  # noqa: S608 - fixed keys
            (*fields.values(), job_id),
        )

    @staticmethod
    def _merged_artifacts(
        job: dict[str, Any], additions: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        rows = artifacts_of(job)
        if additions is None:
            return rows
        by_asset = {
            str(row.get("asset_id")): index
            for index, row in enumerate(rows)
            if row.get("asset_id") is not None
        }
        for row in additions:
            if not isinstance(row, dict) or not row.get("path"):
                continue
            asset_id = str(row.get("asset_id", len(rows)))
            index = by_asset.get(asset_id)
            if index is None:
                by_asset[asset_id] = len(rows)
                rows.append(dict(row, asset_id=asset_id))
            else:
                rows[index] = {**rows[index], **row, "asset_id": asset_id}
        return rows

    # -- read -------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return self._row_dict(row)

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return self._row_dict(row)

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        rows = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE state NOT IN ('succeeded', 'failed', 'cancelled')
              AND next_action_at IS NOT NULL
              AND next_action_at <= ?
            ORDER BY next_action_at
            """,
            (now,),
        ).fetchall()
        return [self._row_dict(r) for r in rows]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_dict(r) for r in rows]

    def list_jobs(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
        modality: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent jobs, newest first, optionally filtered."""
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if modality:
            clauses.append("modality = ?")
            params.append(modality)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
            (*params, limit),
        ).fetchall()
        return [self._row_dict(r) for r in rows]

    def usage_samples(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """What each settled job measured, for the qualification record.

        **Read in SQL, not in Python, and that is the point.** `result_json` holds the
        *whole* generated text — up to ~16 KB on a 4,096-token completion — so pulling
        `SELECT *` over 500 recent jobs to read four numbers would materialise megabytes
        of prose every time the catalogue is rebuilt, which is every 20 seconds on a
        path Story Studio polls. `json_extract` reads the four fields and leaves the
        prose in the file.

        **`CASE WHEN json_valid(...)` rather than `WHERE json_valid(...)`.** A WHERE
        clause is not a guarantee about evaluation order: SQLite is free to evaluate
        `json_extract` first, and it raises `malformed JSON` on a row the filter would
        have removed. One unreadable row must not take the whole record with it — the
        CASE yields `NULL` for it and every other row is still published.

        **`json_type(...) IN ('integer', 'real')`, not a Python `isinstance` check, and
        the tests are why.** `json_extract` returns `1` for a JSON `true` and `0` for
        `false` — the boolean-ness is gone by the time Python sees it, so the `bool`
        guard that used to reject `{"completion_tokens": true}` in Python stopped
        working the moment the read moved into SQL, and `True` re-entered the record as
        a one-token completion. The type has to be checked where it is still a JSON
        type. A stringified `"7"` is refused by the same clause.

        Only the fields a job actually states come back; a row with no `usage` returns
        `NULL`s and is dropped by the reader rather than counted as zero.
        """
        rows = self._conn.execute(
            """
            SELECT provider_id, model_id, modality, created_at, updated_at,
                   CASE WHEN json_valid(result_json)
                         AND json_type(result_json, '$.payload.usage.prompt_tokens')
                             IN ('integer', 'real')
                        THEN json_extract(result_json, '$.payload.usage.prompt_tokens') END
                       AS prompt_tokens,
                   CASE WHEN json_valid(result_json)
                         AND json_type(result_json, '$.payload.usage.completion_tokens')
                             IN ('integer', 'real')
                        THEN json_extract(result_json, '$.payload.usage.completion_tokens') END
                       AS completion_tokens,
                   CASE WHEN json_valid(result_json)
                         AND json_type(result_json, '$.payload.generation_seconds')
                             IN ('integer', 'real')
                        THEN json_extract(result_json, '$.payload.generation_seconds') END
                       AS generation_seconds,
                   CASE WHEN json_valid(result_json)
                        THEN json_extract(result_json, '$.payload.hub_machine') END
                       AS hub_machine,
                   -- **What kind of work this was**, from the request the caller sent.
                   -- `response_format` is on every job's `params`; `json_object` and
                   -- `json_schema` mean structured, anything else (including absent)
                   -- means prose. Without this the record blends a 1,500-token chapter
                   -- with a 12,000-token scene pack and publishes one median over two
                   -- different things — which it did, and the blend was 1.45 where
                   -- prose alone measures 1.21.
                   CASE WHEN json_valid(params)
                         AND json_type(params, '$.response_format.type') = 'text'
                        THEN json_extract(params, '$.response_format.type') END
                       AS response_format
              FROM jobs
             WHERE result_json IS NOT NULL AND result_json != ''
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def protected_input_paths(self) -> set[str]:
        """Inputs held by queued, running, or explicitly recoverable jobs."""
        rows = self._conn.execute(
            """
            SELECT state, recovery_state, params FROM jobs
             WHERE state NOT IN ('succeeded', 'failed', 'cancelled')
                OR recovery_state IS NOT NULL
            """
        ).fetchall()
        protected: set[str] = set()
        for row in rows:
            protected.update(input_paths_of(dict(row)))
        return protected

    def stats(self) -> dict[str, Any]:
        """Counts for the dashboard: totals by state, plus the newest activity."""
        by_state = {
            row["state"]: row["n"]
            for row in self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        }
        by_modality = {
            row["modality"]: row["n"]
            for row in self._conn.execute(
                "SELECT modality, COUNT(*) AS n FROM jobs GROUP BY modality"
            ).fetchall()
        }
        newest = self._conn.execute(
            "SELECT MAX(created_at) AS t FROM jobs"
        ).fetchone()
        return {
            "by_state": by_state,
            "by_modality": by_modality,
            "total": sum(by_state.values()),
            "last_job_at": newest["t"] if newest else None,
        }
