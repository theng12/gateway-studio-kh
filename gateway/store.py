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
    next_action_at   REAL
);
CREATE INDEX IF NOT EXISTS jobs_state_next ON jobs(state, next_action_at);
"""

# Columns added after the first release. Applied idempotently so an existing
# Phase 0 database keeps working instead of needing to be deleted.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("provider_id", "TEXT"),
    ("result_json", "TEXT"),
    ("artifacts_json", "TEXT"),
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


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._ensure_columns()
        self._conn.commit()

    def _ensure_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")

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
                    return dict(existing), False

            job_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO jobs (id, idempotency_key, modality, model_id, provider_id,
                                  state, params, created_at, updated_at, next_action_at)
                VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?)
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
        self._update(
            job_id,
            state="succeeded",
            asset_path=asset_path,
            media_type=media_type,
            artifacts_json=json.dumps(artifacts) if artifacts is not None else None,
            result_json=json.dumps(result) if result is not None else None,
            next_action_at=None,
        )

    def fail(self, job_id: str, *, code: str, detail: str) -> None:
        self._update(
            job_id,
            state="failed",
            error_code=code,
            error_detail=detail,
            next_action_at=None,
        )

    def mark_running(self, job_id: str) -> bool:
        """Claim only work still accepted, not a cancelled row in an old snapshot."""
        with self._lock:
            result = self._conn.execute(
                """UPDATE jobs SET state = 'running', next_action_at = NULL,
                                   updated_at = ?
                   WHERE id = ? AND state = 'accepted'""",
                (time.time(), job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    def cancel(self, job_id: str) -> dict[str, Any]:
        row = self.get(job_id)
        if row["state"] not in TERMINAL_STATES:
            self._update(job_id, state="cancelled", next_action_at=None)
            row = self.get(job_id)
        return row

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE id = ?",  # noqa: S608 - fixed keys
                (*fields.values(), job_id),
            )
            self._conn.commit()

    # -- read -------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

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
        return [dict(r) for r in rows]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

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
        return [dict(r) for r in rows]

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
