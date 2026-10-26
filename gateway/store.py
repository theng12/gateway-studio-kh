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

import fcntl
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("gateway.store")

#: Appended to a failure raised inside the gateway before any provider request was made.
NEVER_SENT_SUFFIX = (
    " Failed inside the gateway before any request was sent to a provider;"
    " nothing to reconcile, safe to retry."
)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
#: The request field `priority` (0.2.165): `normal` (the default, stored NULL) or `background`.
NORMAL, BACKGROUND = "normal", "background"
PRIORITIES = (NORMAL, BACKGROUND)
UNKNOWN_OUTCOME_CODE = "UNKNOWN_OUTCOME"
RECOVERY_UNKNOWN = "unknown_outcome"
#: The code a hold carries once an operator has closed it **without** the attempt's answer
#: (`api._discard_recovery_hold`). Named here because `succeed()` reads it to decide adoption:
#: this is the one close that says nothing about the attempt, so a real answer that turns up
#: afterwards is still this job's answer.
HOLD_CLOSED = "HOLD_CLOSED"
#: The Studio Hub lanes submitted through `/api/hub/jobs`, which answers a repeated `clientRequestId`
#: with the batch it already made, from a SQLite ledger that survives a Hub restart (Hub `broker.py`
#: `_submit_batch_locked`). Transcription (dedupe only while a batch is active) and chat are not.
HUB_RESENDABLE_MODALITIES = frozenset({"image", "tts", "music", "video"})
RECOVERY_RETRYABLE = "retryable"
CANCEL_REQUESTED = "requested"
CANCEL_CONFIRMED = "confirmed"
#: Request params that hold a staged input file. `_reference_paths` is the image lane's **list** —
#: several reference images ride one job — so `input_paths_of` expands list values as well as
#: strings; a key read as a bare string would protect the first reference and let the retention
#: sweep delete the rest.
_INPUT_PARAM_KEYS = ("_upload_path", "_reference_path", "_reference_paths")


class RenderJournalConflict(ValueError):
    """A render journal identity or state transition conflicts with durable state."""


class RenderJournalNotFound(RenderJournalConflict):
    """A render journal writer was given an unknown Gateway journal id."""


class RenderJournalCancelled(RenderJournalConflict):
    """Cancellation was recorded before the first send marker could win."""

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
-- The notifier asks "what settled in this window", and a window read must not walk every job's
-- (large) result_json to find out.
CREATE INDEX IF NOT EXISTS jobs_updated ON jobs(updated_at);
-- **Every hot read walks an index, never the table** (0.2.157). The store is one connection behind
-- one lock, so a full scan of ~50k rows (~300 MB with result_json) held every other request and
-- job thread for it: the Jobs list took 6-21 s live and starved the threadpool `/health` needs.
-- Newest-first pages (all, and per state), the stats/queue counts, and the dispatcher's open set.
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS jobs_modality_state ON jobs(modality, state);
CREATE INDEX IF NOT EXISTS jobs_open ON jobs(created_at, id)
    WHERE state NOT IN ('succeeded', 'failed', 'cancelled');
-- What the Analytics page needs of a job after `archive_old` removed it: six scalars, never
-- the prompt or the generated text. `generation_analytics` reads this beside `jobs`.
CREATE TABLE IF NOT EXISTS job_summaries (
    id           TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    modality     TEXT,
    model_id     TEXT,
    provider_id  TEXT,
    machine      TEXT,
    state        TEXT NOT NULL,
    seconds      REAL,
    bytes        REAL
);
-- Where a failed narration's refused section is kept for listening (a Studio Hub item's review
-- candidate). Written once with the failure, never updated, and outside `archive_old`: an archived
-- job keeps its candidate reachable from the admin routes.
CREATE TABLE IF NOT EXISTS review_candidate_locators (
    gateway_job_id  TEXT PRIMARY KEY,
    provider_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    item_index      INTEGER NOT NULL,
    receipt_sha256  TEXT NOT NULL,
    pcm_bytes       INTEGER NOT NULL,
    pcm_sha256      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    schema_version  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS render_journal (
    journal_id TEXT PRIMARY KEY,
    story_uuid TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    gateway_attempt_key TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    inbound_story_body_sha256 TEXT NOT NULL,
    outbound_hub_body_sha256 TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    destination_site TEXT NOT NULL,
    destination_controller TEXT NOT NULL,
    destination_node TEXT NOT NULL,
    dispatch_key TEXT NOT NULL,
    dispatch_fence TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'send_may_start', 'send_unknown', 'accepted')),
    send_may_start_at REAL,
    hub_job_id TEXT,
    cancel_requested_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(owner_scope, gateway_attempt_key),
    UNIQUE(owner_scope, dispatch_key),
    UNIQUE(owner_scope, story_uuid, intent_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS render_installation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_scope TEXT NOT NULL,
    gateway_instance_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS render_staging (
    id TEXT PRIMARY KEY,
    gateway_instance_id TEXT NOT NULL,
    story_instance_id TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    story_uuid TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    gateway_attempt_key TEXT NOT NULL,
    inbound_story_body BLOB NOT NULL,
    inbound_story_body_sha256 TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    destination_site TEXT NOT NULL,
    destination_controller TEXT NOT NULL,
    destination_hub_controller TEXT NOT NULL,
    destination_node TEXT NOT NULL,
    selected_machine BLOB NOT NULL,
    capability_snapshot BLOB NOT NULL,
    capability_snapshot_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    outbound_hub_body BLOB,
    outbound_hub_body_sha256 TEXT,
    dispatch_key TEXT,
    dispatch_fence TEXT,
    hub_job_id TEXT,
    cancel_state TEXT,
    cancel_body BLOB,
    cancel_body_sha256 TEXT,
    cancel_key TEXT,
    cancel_outcome TEXT,
    terminal_fence_receipt TEXT,
    terminal_first_cancel_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(owner_scope, gateway_attempt_key),
    UNIQUE(owner_scope, story_uuid, intent_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS render_staging_assets (
    render_id TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    gateway_asset_handle TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    confirmed_hub_offset INTEGER NOT NULL DEFAULT 0,
    hub_asset_handle TEXT,
    pending_offset INTEGER,
    pending_length INTEGER,
    pending_sha256 TEXT,
    PRIMARY KEY(render_id, logical_id),
    FOREIGN KEY(render_id) REFERENCES render_staging(id)
);
CREATE TABLE IF NOT EXISTS render_delivery (
    render_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    remote_state TEXT,
    remote_attempt_id TEXT,
    remote_descriptor_json TEXT,
    output_upload_id TEXT,
    output_asset_handle TEXT,
    winner_fence TEXT,
    receipt_digest TEXT,
    gateway_receipt BLOB,
    gateway_receipt_digest TEXT,
    ack_body BLOB,
    ack_body_sha256 TEXT,
    ack_key TEXT,
    ack_outcome TEXT,
    story_ack_digest TEXT,
    stopped_evidence_digest TEXT,
    updated_at REAL NOT NULL,
    FOREIGN KEY(render_id) REFERENCES render_staging(id)
);
CREATE TABLE IF NOT EXISTS render_cancel_fences (
    owner_scope TEXT NOT NULL,
    dispatch_key TEXT NOT NULL,
    expected_request_sha256 TEXT NOT NULL,
    fence_receipt TEXT NOT NULL,
    first_cancel_at REAL NOT NULL,
    gateway_job_id TEXT,
    PRIMARY KEY(owner_scope, dispatch_key)
);
"""
REVIEW_LOCATOR_FIELDS = ("provider_id", "batch_id", "item_index", "receipt_sha256", "pcm_bytes",
                         "pcm_sha256")
REVIEW_LOCATORS_PER_PROVIDER = 32

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
    # Which project run this job belongs to (`<project-slug>/<episode-id | project>`, D6), so the
    # dispatcher can give projects turns. Advisory: never sent to a Hub, never part of idempotency;
    # absent means the job is its own group, which is the order every job had before.
    ("job_group", "TEXT"),
    # `background` (0.2.165): bulk work that runs only on Macs no normal job is waiting for. NULL is
    # normal, so every job before this column, and every client that never sends the field, is normal.
    ("priority", "TEXT"),
    # While a running job's Hub item is still queued: `{"reason", "next_try_at", "since"}` as JSON.
    # Published as `waiting` on the job envelope; null as soon as a Mac starts rendering.
    ("waiting_json", "TEXT"),
    # Why a job is back in the queue instead of running. **A busy fleet is not a
    # broken job**: a site with no free machine right now will have one shortly,
    # and the caller asked for work rather than for a verdict on the fleet's
    # occupancy. Cleared on dispatch, because a running job is not waiting.
    ("defer_reason", "TEXT"),
    # **Where this job has already been tried and failed** (A7). A JSON list of location
    # ids, so a re-route cannot revisit one — and, more importantly, so the exclusion
    # survives a restart, a deferral and a redelivery. The dispatch loop's own `tried` set
    # was local to one call, which is why a job that deferred and came back could be sent
    # to the same machine twice.
    ("attempted_locations", "TEXT"),
    # When a restart re-sent this fleet job to the same site with the same key (0.2.160): its first
    # send was still unanswered at shutdown, and the Hub answers a repeated key with that batch.
    ("restart_resent_at", "REAL"),
    # Every settled attempt, in order: `[{"location", "code", "detail", "at"}]`. The
    # terminal failure after the last attempt **names the machines and why each failed**,
    # and that sentence has to be built from a record rather than reconstructed from the
    # last error, which knows only about itself.
    ("attempt_log", "TEXT"),
    # When the owner removed this failed job from the Jobs list (0.2.153). A view flag only: the
    # row, its totals, analytics and billing stay. Hidden while `dismissed_at >= updated_at`, so a
    # job that changes afterwards (a late result, a retry) shows again.
    ("dismissed_at", "REAL"),
)

_RENDER_STAGING_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("terminal_fence_receipt", "TEXT"),
    ("terminal_first_cancel_at", "REAL"),
)
_RENDER_DELIVERY_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("stopped_evidence_digest", "TEXT"),
)

#: Settled failures the owner may remove from the list: never a needs-recovery hold, never one
#: already removed, never anything that is not `failed`.
_DISMISSABLE = (
    "state = 'failed' AND COALESCE(recovery_state, '') != 'unknown_outcome'"
    " AND (dismissed_at IS NULL OR dismissed_at < updated_at)"
)

_RENDER_IMMUTABLE_FIELDS = (
    "journal_id",
    "story_uuid",
    "intent_id",
    "attempt_no",
    "gateway_attempt_key",
    "owner_scope",
    "inbound_story_body_sha256",
    "outbound_hub_body_sha256",
    "packet_sha256",
    "destination_site",
    "destination_controller",
    "destination_node",
    "dispatch_key",
    "dispatch_fence",
)
_RENDER_TEXT_FIELDS = (
    "story_uuid",
    "intent_id",
    "gateway_attempt_key",
    "owner_scope",
    "destination_site",
    "destination_controller",
    "destination_node",
    "dispatch_key",
    "dispatch_fence",
)


def _render_text(name: str, value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or not value.isascii() or not all(" " <= char <= "~" for char in value)):
        raise ValueError(f"{name} must be 1-256 printable ASCII characters")
    return value


def _render_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("journal_id must be a canonical lowercase UUID")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("journal_id must be a canonical lowercase UUID") from exc
    if canonical != value:
        raise ValueError("journal_id must be a canonical lowercase UUID")
    return value


def _render_digest(name: str, value: Any) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} must be a lowercase SHA-256 value")
    return value


def _render_attempt(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise ValueError("attempt_no must be an integer from 1 through 2^63-1")
    return value


def _render_now() -> float:
    now = time.time()
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("render journal timestamp must be finite numeric seconds")
    return float(now)


def _render_values(values: dict[str, Any]) -> dict[str, Any]:
    checked = dict(values)
    if "journal_id" in checked:
        checked["journal_id"] = _render_uuid(checked["journal_id"])
    if "attempt_no" in checked:
        checked["attempt_no"] = _render_attempt(checked["attempt_no"])
    for field in _RENDER_TEXT_FIELDS:
        if field in checked:
            checked[field] = _render_text(field, checked[field])
    for field in (
        "inbound_story_body_sha256",
        "outbound_hub_body_sha256",
        "packet_sha256",
    ):
        if field in checked:
            checked[field] = _render_digest(field, checked[field])
    return checked


def _loads_list(value: Any) -> list[Any]:
    """A JSON list column, or `[]`.

    Every reader of `attempted_locations`/`attempt_log` goes through here, so a row written
    by an older build (the column added to an existing database is `NULL`) and a row whose
    JSON was truncated by a crash read the same way: no attempts recorded. That is the
    conservative reading — it can cost one extra attempt, never a duplicate.
    """
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return loaded if isinstance(loaded, list) else []


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
    paths: set[str] = set()
    for key in _INPUT_PARAM_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value:
            paths.add(value)
        elif isinstance(value, list):
            # A job may carry several staged inputs under one key (the image lane's references).
            paths.update(str(item) for item in value if isinstance(item, str) and item)
    return paths


class JobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # One connection serves the API, dispatcher and job threads. Every access is serialized;
        # this is re-entrant because locked writers call read helpers such as `submit() -> get()`.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        #: Jobs found `running` with an upstream id (or a fleet send with none yet) when this store
        #: opened: the worker resumes or re-sends them (or holds them) on start. See `_recover_interrupted`.
        self.interrupted: list[str] = []
        self._hold_sweep_after = ""
        #: Held for as long as this process owns the queue, so a second opener cannot sweep
        #: (`_claim_queue`). Released by `close()`, and by the kernel if this process dies.
        self._owner_fd: int | None = None
        self._conn.executescript(_SCHEMA)
        # The private-render identity: minted once, never regenerated (no-op once the row exists).
        self._conn.execute(
            "INSERT OR IGNORE INTO render_installation VALUES (1, ?, ?, ?)",
            (str(uuid.uuid4()), str(uuid.uuid4()), time.time()),
        )
        self._ensure_columns()
        if self._claim_queue():
            self._recover_interrupted()
        self._settle_late_successes()
        self._relabel_never_sent_pool_failures()
        self._conn.commit()

    def _claim_queue(self) -> bool:
        """Whether *this* process is the one taking the queue over, so the restart sweep may run.

        **The sweep is not a property of opening the database.** It exists to close the one unsafe
        window: a job was in flight when the process that owned it died. Running it whenever anyone
        opens the file made *every second opener* orphan the serving process's live work under a
        sentence about a restart that never happened — measured 2026-10-19 on the owner's gateway:
        a test module imported without redirecting its paths, and a gateway spawn that could not
        bind 8787, each held every `running` job as `failed / UNKNOWN_OUTCOME`, repeatedly, while
        pid 12123 had been up since 08:15 and its job threads were still running and still answered.

        The lock is an `flock` on `<db>.owner`: advisory, per open file description (so a second
        opener in this process is excluded too), and released by the kernel the moment the owner
        exits — which is exactly the evidence "the gateway restarted" needs. A stale lock file
        cannot hold the queue: only the lock matters, never the file's existence.
        """
        try:
            fd = os.open(f"{self.path}.owner", os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:  # noqa: BLE001 - an unopenable lock is never worse than no lock
            log.warning("no owner lock beside %s; sweeping as the only opener", self.path)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            log.info("%s is being served by another process; leaving its jobs alone", self.path)
            return False
        self._owner_fd = fd
        return True

    def _ensure_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
        for table, columns in (
            ("render_staging", _RENDER_STAGING_ADDED_COLUMNS),
            ("render_delivery", _RENDER_DELIVERY_ADDED_COLUMNS),
        ):
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, sql_type in columns:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if "retryable" in result:
            result["retryable"] = bool(result["retryable"])
        result["cancel_requested"] = result.get("cancel_requested_at") is not None
        # **Only an unknown outcome needs reconciling.** `retryable` is also a recovery state (it
        # keeps the job's inputs and its row out of the archive so it can be retried), but Story
        # reads `recovery_required: true` as "held, do not resubmit" (`holdsUnknownOutcome`) — so a
        # plain retryable failure published that way was locked as if its outcome were unknown.
        result["recovery_required"] = result.get("recovery_state") == RECOVERY_UNKNOWN
        try:
            result["waiting"] = json.loads(result["waiting_json"]) if result.get("waiting_json") else None
        except ValueError:
            result["waiting"] = None
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

    def _settle_late_successes(self) -> None:
        """A hold whose result arrived late is a success, not a failure with a footnote.

        `succeed()` now settles such a job itself; this is the same rule applied once to the rows
        written before it did (restarts that raced a finishing job left `failed / UNKNOWN_OUTCOME`
        beside an artifact on disk that nothing published). A hold an operator closed locally
        (`HOLD_CLOSED`) is the same case — nothing was sent upstream, so the answer is still the
        job's — and it is included for the same reason.
        """
        self._conn.execute(
            """
            UPDATE jobs
               SET state = 'succeeded', recovery_state = NULL, recovery_detail = NULL,
                   recovery_context = NULL, error_code = NULL, error_detail = NULL,
                   retryable = 0, next_action_at = NULL
             WHERE state = 'failed' AND (recovery_state = ? OR error_code = ?)
               AND late_result_state = 'succeeded'
               AND (asset_path IS NOT NULL OR result_json IS NOT NULL)
            """,
            (RECOVERY_UNKNOWN, HOLD_CLOSED),
        )

    #: The exact failure gateway 0.2.97/0.2.98's pool adapter raised while resolving its members,
    #: before it had sent anything anywhere.
    _POOL_RESOLVE_BUG = "AttributeError: module 'gateway.catalog' has no attribute 'resolve'"

    def _relabel_never_sent_pool_failures(self) -> list[str]:
        """One-off repair (2026-10-03): the pool bug's jobs were never sent, so they are not holds.

        Those builds recorded every pool job as "Upstream outcome is unknown; reconcile …" because
        `dispatch_started_at` is stamped before the adapter runs, and Story correctly locked them.
        Nothing reached a provider, so they become what the worker now records for that case: a
        plain, retryable failure with nothing to reconcile. Matched on the exact error, the pool
        provider and the absence of any upstream id, so no other hold is touched; idempotent,
        because a relabelled row is no longer an `unknown_outcome`.
        """
        rows = self._conn.execute(
            """
            SELECT id, error_detail FROM jobs
             WHERE state = 'failed' AND recovery_state = ? AND provider_id = 'pool'
               AND COALESCE(upstream_job_id, '') = '' AND error_detail LIKE ?
            """,
            (RECOVERY_UNKNOWN, self._POOL_RESOLVE_BUG + "%"),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                """
                UPDATE jobs
                   SET retryable = 1, recovery_state = ?, recovery_detail = NULL,
                       recovery_context = NULL, error_detail = ?
                 WHERE id = ?
                """,
                (RECOVERY_RETRYABLE,
                 self._POOL_RESOLVE_BUG + " (gateway 0.2.97/0.2.98 pool bug)." + NEVER_SENT_SUFFIX,
                 row["id"]),
            )
        ids = [row["id"] for row in rows]
        if ids:
            log.warning("relabelled %d never-sent pool failures as retryable: %s",
                        len(ids), ", ".join(ids))
        return ids

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
            "SELECT id, modality, provider_id, upstream_job_id, dispatch_started_at, "
            "cancel_requested_at FROM jobs WHERE state = 'running'"
        ).fetchall()
        if not rows:
            return 0
        now = time.time()
        # **A job that never left this process goes back in the queue** (audit M5). The sweep
        # failed *everything* `running`, so a restart turned work that had not been dispatched —
        # `dispatch_started_at IS NULL` means the adapter was never called — into a durable
        # recovery hold the operator had to reconcile by hand. Six of the live holds were exactly
        # that, `upstream_job_id: NULL` and nothing running anywhere.
        #
        # The line is drawn at `dispatch_started_at` rather than at the upstream id on purpose.
        # An id is proof that a container was created; **no** id is not proof that none was, for
        # the one window where the POST succeeded and the answer was lost. Before dispatch began
        # there is no such window, so that case is provably safe to re-queue and the ambiguous
        # one still holds. Never replay what might be running; always resume what provably is not.
        requeued = 0
        for row in rows:
            if row["dispatch_started_at"] is None and not row["upstream_job_id"]:
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET state = 'accepted', error_code = NULL, error_detail = NULL,
                           retryable = 0, recovery_state = NULL, recovery_detail = NULL,
                           recovery_context = NULL,
                           next_action_at = ?, updated_at = ?
                     WHERE id = ? AND state = 'running'
                    """,
                    (now, now, row["id"]),
                )
                requeued += 1
                continue
            # **A job the upstream gave an id is still running there, so it stays `running`** and
            # the worker resumes polling that id on start (`Worker._resume_interrupted`). Failing
            # it here threw away narrations 30 minutes into rendering on every Gateway install,
            # while the Hub kept the batch and its result. Only the worker knows whether the lane
            # can be polled by id; one that cannot gets the hold below via `hold_interrupted`.
            if row["upstream_job_id"]:
                self.interrupted.append(row["id"])
                continue
            # **A fleet send unanswered at shutdown is re-sent, not held** (owner 2026-10-09: "allow
            # re-send"). The Hub de-duplicates `/api/hub/jobs` by client request id in its durable
            # ledger, so the worker re-sends it to the same site with the same key
            # (`worker.restart_can_resend` has the whole rule; anything it refuses is held there).
            if (str(row["provider_id"] or "").startswith("studio-hub-")
                    and row["modality"] in HUB_RESENDABLE_MODALITIES
                    and row["cancel_requested_at"] is None):
                self.interrupted.append(row["id"])
                continue
            self._write_restart_hold(row["id"], "", now)
        return len(rows) - requeued - len(self.interrupted)

    def _write_restart_hold(self, job_id: str, upstream: str, now: float) -> None:
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
            (UNKNOWN_OUTCOME_CODE, detail, RECOVERY_UNKNOWN, detail, context, now, job_id),
        )

    def note_restart_resend(self, job_id: str) -> None:
        self._update(job_id, restart_resent_at=time.time())

    def hold_interrupted(self, job_id: str) -> None:
        """The restart hold, for an interrupted job whose lane cannot be polled by its id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT upstream_job_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is not None:
                self._write_restart_hold(job_id, str(row["upstream_job_id"] or ""), time.time())
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
            if self._owner_fd is not None:
                # Releasing the queue is what lets the *next* starter sweep: a store closed here
                # and reopened is a restart, and its `running` jobs are the ones the sweep is for.
                os.close(self._owner_fd)
                self._owner_fd = None

    # -- render journal ---------------------------------------------------
    def render_installation(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._conn.execute(
                "SELECT owner_scope, gateway_instance_id, created_at FROM render_installation"
            ).fetchone())

    @contextmanager
    def _render_write(self):
        """Serialize this connection and make every render mutation an explicit transaction."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    @staticmethod
    def _render_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _render_matches(row: sqlite3.Row | dict[str, Any], values: dict[str, Any]) -> bool:
        return all(row[field] == value for field, value in values.items())

    def create_render_journal(
        self,
        *,
        journal_id: str,
        story_uuid: str,
        intent_id: str,
        attempt_no: int,
        gateway_attempt_key: str,
        owner_scope: str,
        inbound_story_body_sha256: str,
        outbound_hub_body_sha256: str,
        packet_sha256: str,
        destination_site: str,
        destination_controller: str,
        destination_node: str,
        dispatch_key: str,
        dispatch_fence: str,
    ) -> dict[str, Any]:
        values = _render_values({
            "journal_id": journal_id,
            "story_uuid": story_uuid,
            "intent_id": intent_id,
            "attempt_no": attempt_no,
            "gateway_attempt_key": gateway_attempt_key,
            "owner_scope": owner_scope,
            "inbound_story_body_sha256": inbound_story_body_sha256,
            "outbound_hub_body_sha256": outbound_hub_body_sha256,
            "packet_sha256": packet_sha256,
            "destination_site": destination_site,
            "destination_controller": destination_controller,
            "destination_node": destination_node,
            "dispatch_key": dispatch_key,
            "dispatch_fence": dispatch_fence,
        })
        now = _render_now()
        try:
            with self._render_write():
                self._conn.execute(
                    """
                    INSERT INTO render_journal (
                        journal_id, story_uuid, intent_id, attempt_no, gateway_attempt_key,
                        owner_scope, inbound_story_body_sha256, outbound_hub_body_sha256,
                        packet_sha256, destination_site, destination_controller, destination_node,
                        dispatch_key, dispatch_fence, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (*(values[field] for field in _RENDER_IMMUTABLE_FIELDS), now, now),
                )
                row = self._conn.execute(
                    "SELECT * FROM render_journal WHERE journal_id = ?", (values["journal_id"],)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT * FROM render_journal
                     WHERE journal_id = ?
                        OR (owner_scope = ? AND gateway_attempt_key = ?)
                        OR (owner_scope = ? AND dispatch_key = ?)
                        OR (owner_scope = ? AND story_uuid = ? AND intent_id = ? AND attempt_no = ?)
                    """,
                    (
                        values["journal_id"],
                        values["owner_scope"], values["gateway_attempt_key"],
                        values["owner_scope"], values["dispatch_key"],
                        values["owner_scope"], values["story_uuid"], values["intent_id"],
                        values["attempt_no"],
                    ),
                ).fetchall()
            if len(rows) == 1 and self._render_matches(rows[0], values):
                return self._render_row(rows[0])  # type: ignore[return-value]
            raise RenderJournalConflict("render journal identity conflicts with durable state") from exc
        return self._render_row(row)  # type: ignore[return-value]

    def get_render_journal(self, journal_id: str) -> dict[str, Any] | None:
        journal_id = _render_uuid(journal_id)
        with self._lock:
            return self._render_row(self._conn.execute(
                "SELECT * FROM render_journal WHERE journal_id = ?", (journal_id,)
            ).fetchone())

    def get_render_journal_by_attempt_key(
        self, *, owner_scope: str, gateway_attempt_key: str
    ) -> dict[str, Any] | None:
        owner_scope = _render_text("owner_scope", owner_scope)
        gateway_attempt_key = _render_text("gateway_attempt_key", gateway_attempt_key)
        with self._lock:
            return self._render_row(self._conn.execute(
                """SELECT * FROM render_journal
                    WHERE owner_scope = ? AND gateway_attempt_key = ?""",
                (owner_scope, gateway_attempt_key),
            ).fetchone())

    def replay_render_journal(
        self,
        *,
        gateway_attempt_key: str,
        owner_scope: str,
        story_uuid: str,
        intent_id: str,
        attempt_no: int,
        inbound_story_body_sha256: str,
        outbound_hub_body_sha256: str,
        packet_sha256: str,
        destination_site: str,
        destination_controller: str,
        destination_node: str,
        dispatch_key: str,
        dispatch_fence: str,
    ) -> dict[str, Any] | None:
        values = _render_values({
            "gateway_attempt_key": gateway_attempt_key,
            "owner_scope": owner_scope,
            "story_uuid": story_uuid,
            "intent_id": intent_id,
            "attempt_no": attempt_no,
            "inbound_story_body_sha256": inbound_story_body_sha256,
            "outbound_hub_body_sha256": outbound_hub_body_sha256,
            "packet_sha256": packet_sha256,
            "destination_site": destination_site,
            "destination_controller": destination_controller,
            "destination_node": destination_node,
            "dispatch_key": dispatch_key,
            "dispatch_fence": dispatch_fence,
        })
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM render_journal
                    WHERE owner_scope = ? AND gateway_attempt_key = ?""",
                (values["owner_scope"], values["gateway_attempt_key"]),
            ).fetchone()
        if row is None:
            return None
        if not self._render_matches(row, values):
            raise RenderJournalConflict("render journal replay changed immutable fields")
        return self._render_row(row)

    def mark_render_send_may_start(self, journal_id: str) -> dict[str, Any]:
        journal_id = _render_uuid(journal_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """
                UPDATE render_journal
                   SET state = 'send_may_start', send_may_start_at = ?, updated_at = ?
                 WHERE journal_id = ? AND state = 'prepared' AND cancel_requested_at IS NULL
                """,
                (now, now, journal_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_journal WHERE journal_id = ?", (journal_id,)
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render journal not found")
            if changed != 1:
                if row["state"] == "send_may_start":
                    changed = 0
                elif row["state"] == "prepared" and row["cancel_requested_at"] is not None:
                    raise RenderJournalCancelled("render journal was cancelled before send")
                else:
                    raise RenderJournalConflict("render journal cannot start another send")
            result = self._render_row(row)
        result["newly_recorded"] = changed == 1  # type: ignore[index]
        return result  # type: ignore[return-value]

    def mark_render_send_unknown(self, journal_id: str) -> dict[str, Any]:
        journal_id = _render_uuid(journal_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_journal SET state = 'send_unknown', updated_at = ?
                    WHERE journal_id = ? AND state = 'send_may_start'""",
                (now, journal_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_journal WHERE journal_id = ?", (journal_id,)
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render journal not found")
            if changed != 1 and row["state"] != "send_unknown":
                raise RenderJournalConflict("render journal cannot record an unknown send")
            result = self._render_row(row)
        return result  # type: ignore[return-value]

    def record_render_acceptance(self, journal_id: str, hub_job_id: str) -> dict[str, Any]:
        journal_id = _render_uuid(journal_id)
        hub_job_id = _render_text("hub_job_id", hub_job_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """
                UPDATE render_journal
                   SET state = 'accepted', hub_job_id = ?, updated_at = ?
                 WHERE journal_id = ? AND state IN ('send_may_start', 'send_unknown')
                   AND hub_job_id IS NULL
                """,
                (hub_job_id, now, journal_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_journal WHERE journal_id = ?", (journal_id,)
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render journal not found")
            if changed != 1 and not (row["state"] == "accepted" and row["hub_job_id"] == hub_job_id):
                raise RenderJournalConflict("render journal acceptance conflicts with durable state")
            result = self._render_row(row)
        return result  # type: ignore[return-value]

    def request_render_cancel(self, journal_id: str) -> dict[str, Any]:
        journal_id = _render_uuid(journal_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """
                UPDATE render_journal SET cancel_requested_at = ?, updated_at = ?
                 WHERE journal_id = ? AND cancel_requested_at IS NULL
                   AND state IN ('prepared', 'send_may_start', 'send_unknown', 'accepted')
                """,
                (now, now, journal_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_journal WHERE journal_id = ?", (journal_id,)
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render journal not found")
            if changed != 1 and row["cancel_requested_at"] is None:
                raise RenderJournalConflict("render journal cannot record cancellation")
            result = self._render_row(row)
        return result  # type: ignore[return-value]

    # -- private final-render vertical slice ------------------------------
    def _render_staging_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        assets = self._conn.execute(
            "SELECT * FROM render_staging_assets WHERE render_id = ? ORDER BY logical_id",
            (row["id"],),
        ).fetchall()
        result["assets"] = [dict(asset) for asset in assets]
        result["idempotent_replay"] = False
        return result

    def get_render_staging(self, render_id: str) -> dict[str, Any] | None:
        render_id = _render_uuid(render_id)
        with self._lock:
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id = ?", (render_id,)
            ).fetchone())

    def recent_render_routes(self, limit: int = 20) -> list[dict[str, Any]]:
        """The newest routed final renders, newest first: only what the admin page names (no request bytes)."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("render route limit must be 1..100")
        with self._lock:
            return [dict(row) for row in self._conn.execute(
                """SELECT id, state, created_at, destination_controller, destination_node,
                          selected_machine, capability_snapshot
                   FROM render_staging ORDER BY created_at DESC, id DESC LIMIT ?""", (limit,),
            ).fetchall()]

    def get_render_staging_by_attempt_key(
        self, *, owner_scope: str, gateway_attempt_key: str
    ) -> dict[str, Any] | None:
        owner_scope = _render_text("owner_scope", owner_scope)
        gateway_attempt_key = _render_text("gateway_attempt_key", gateway_attempt_key)
        with self._lock:
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE owner_scope = ? AND gateway_attempt_key = ?",
                (owner_scope, gateway_attempt_key),
            ).fetchone())

    def create_render_staging(
        self,
        *,
        render_id: str,
        gateway_instance_id: str,
        story_instance_id: str,
        owner_scope: str,
        story_uuid: str,
        intent_id: str,
        attempt_no: int,
        gateway_attempt_key: str,
        inbound_story_body: bytes,
        inbound_story_body_sha256: str,
        packet_sha256: str,
        destination_site: str,
        destination_controller: str,
        destination_hub_controller: str,
        destination_node: str,
        selected_machine: bytes,
        capability_snapshot: bytes,
        capability_snapshot_sha256: str,
        assets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        attempt_no = _render_attempt(attempt_no)
        text = {
            name: _render_text(name, value)
            for name, value in {
                "gateway_instance_id": gateway_instance_id,
                "story_instance_id": story_instance_id,
                "owner_scope": owner_scope,
                "story_uuid": story_uuid,
                "intent_id": intent_id,
                "gateway_attempt_key": gateway_attempt_key,
                "destination_site": destination_site,
                "destination_controller": destination_controller,
                "destination_hub_controller": destination_hub_controller,
                "destination_node": destination_node,
            }.items()
        }
        digests = {
            name: _render_digest(name, value)
            for name, value in {
                "inbound_story_body_sha256": inbound_story_body_sha256,
                "packet_sha256": packet_sha256,
                "capability_snapshot_sha256": capability_snapshot_sha256,
            }.items()
        }
        if type(inbound_story_body) is not bytes or not inbound_story_body or len(inbound_story_body) > 1024 * 1024:
            raise ValueError("inbound_story_body must be 1..1048576 bytes")
        if hashlib.sha256(inbound_story_body).hexdigest() != digests["inbound_story_body_sha256"]:
            raise ValueError("inbound_story_body digest mismatch")
        if (type(capability_snapshot) is not bytes or not capability_snapshot
                or len(capability_snapshot) > 1024 * 1024
                or hashlib.sha256(capability_snapshot).hexdigest()
                    != digests["capability_snapshot_sha256"]):
            raise ValueError("capability snapshot is invalid")
        if type(selected_machine) is not bytes or not selected_machine \
                or len(selected_machine) > 4096:
            raise ValueError("selected machine snapshot is invalid")
        if type(assets) is not list or not assets:
            raise ValueError("assets must be a non-empty ordered list")
        checked_assets = []
        previous = ""
        for asset in assets:
            if type(asset) is not dict or set(asset) != {
                "logical_id", "gateway_asset_handle", "sha256", "size_bytes"
            }:
                raise ValueError("render staging asset shape is invalid")
            logical_id = _render_text("logical_id", asset["logical_id"])
            handle = _render_text("gateway_asset_handle", asset["gateway_asset_handle"])
            sha256 = _render_digest("sha256", asset["sha256"])
            size = _render_attempt(asset["size_bytes"])
            if logical_id <= previous or len(handle) != 32 or any(c not in "0123456789abcdef" for c in handle):
                raise ValueError("render staging assets must be ordered with valid handles")
            previous = logical_id
            checked_assets.append((logical_id, handle, sha256, size))
        now = _render_now()
        with self._render_write():
            existing = self._conn.execute(
                "SELECT * FROM render_staging WHERE owner_scope = ? AND gateway_attempt_key = ?",
                (text["owner_scope"], text["gateway_attempt_key"]),
            ).fetchone()
            if existing is not None:
                if (existing["inbound_story_body_sha256"] != digests["inbound_story_body_sha256"]
                        or bytes(existing["inbound_story_body"]) != inbound_story_body
                        or existing["story_instance_id"] != text["story_instance_id"]
                        or existing["gateway_instance_id"] != text["gateway_instance_id"]):
                    raise RenderJournalConflict("render attempt key changed immutable identity")
                result = self._render_staging_row(existing)
                result["idempotent_replay"] = True  # type: ignore[index]
                return result  # type: ignore[return-value]
            fence = self._conn.execute(
                "SELECT * FROM render_cancel_fences WHERE owner_scope = ? AND dispatch_key = ?",
                (text["owner_scope"], text["gateway_attempt_key"]),
            ).fetchone()
            if fence is not None:
                if fence["expected_request_sha256"] != digests["inbound_story_body_sha256"]:
                    raise RenderJournalConflict("cancel fence request identity conflicts")
                raise RenderJournalCancelled("render attempt was cancelled before acceptance")
            self._conn.execute(
                """INSERT INTO render_staging (
                    id, gateway_instance_id, story_instance_id, owner_scope, story_uuid, intent_id, attempt_no,
                    gateway_attempt_key, inbound_story_body, inbound_story_body_sha256,
                    packet_sha256, destination_site, destination_controller,
                    destination_hub_controller, destination_node, selected_machine,
                    capability_snapshot,
                    capability_snapshot_sha256,
                    state, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'staging',?,?)""",
                (
                    render_id, text["gateway_instance_id"], text["story_instance_id"], text["owner_scope"],
                    text["story_uuid"], text["intent_id"], attempt_no,
                    text["gateway_attempt_key"], inbound_story_body,
                    digests["inbound_story_body_sha256"], digests["packet_sha256"],
                    text["destination_site"], text["destination_controller"],
                    text["destination_hub_controller"], text["destination_node"],
                    selected_machine, capability_snapshot,
                    digests["capability_snapshot_sha256"], now, now,
                ),
            )
            self._conn.executemany(
                """INSERT INTO render_staging_assets (
                    render_id, logical_id, gateway_asset_handle, sha256, size_bytes
                ) VALUES (?,?,?,?,?)""",
                [(render_id, *asset) for asset in checked_assets],
            )
            self._conn.execute(
                "INSERT INTO render_delivery (render_id, updated_at) VALUES (?,?)",
                (render_id, now),
            )
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id = ?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def render_due(
        self, limit: int = 8, *, after: tuple[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("render due limit must be 1..64")
        if (after is not None and (
                type(after) is not tuple or len(after) != 2
                or type(after[0]) not in (int, float) or not math.isfinite(after[0])
                or type(after[1]) is not str)):
            raise ValueError("render due cursor is invalid")
        with self._lock:
            cursor_sql = ""
            params: tuple[Any, ...] = ()
            if after is not None:
                cursor_sql = "AND (s.created_at > ? OR (s.created_at = ? AND s.id > ?))"
                params = (after[0], after[0], after[1])
            rows = self._conn.execute(
                f"""SELECT s.* FROM render_staging AS s
                   LEFT JOIN render_delivery AS d ON d.render_id = s.id
                   WHERE s.state IN ('staging','hub_prepared','send_may_start','send_unknown',
                                     'accepted','output_ready')
                     AND COALESCE(s.cancel_state, '') NOT IN ('cancelled','acknowledged')
                     AND NOT (s.state = 'output_ready' AND d.ack_outcome IS NOT NULL)
                     AND NOT (COALESCE(d.remote_state, '') IN ('failed','cancelled')
                              AND COALESCE(s.cancel_state, '') != 'requested')
                     {cursor_sql}
                   ORDER BY s.created_at, s.id LIMIT ?""", (*params, limit),
            ).fetchall()
            return [self._render_staging_row(row) for row in rows]  # type: ignore[misc]

    def terminal_render_asset_handles(self) -> dict[str, dict[str, Any]]:
        """Asset handles whose every durable reference is terminal, with their identity.

        Inputs come from `render_staging_assets`; outputs from
        `render_delivery.output_asset_handle`. Handles are content-deduped across rows, so a
        handle is returned only when EVERY row referencing it is terminal — one live row
        excludes it, and so does disagreement about the handle's identity. Unreferenced
        handles are not returned here; `referenced_render_asset_handles` lets the sweep tell
        an orphan from a handle a live row still points at.

        Each value carries `settled_at` (latest `render_staging.updated_at` /
        `render_delivery.updated_at`, the time the grace runs from) plus the identity the
        store re-checks before moving anything: `owner_scope`, and whichever of `sha256`,
        `size_bytes` (inputs) and `upload_id` (outputs) the row recorded. `None` means the
        reference does not state that field.

        Terminal means Story acked the output (`acknowledged`), the render was cancelled
        (`cancel_state='cancelled'`), or the Hub reported `failed`/`cancelled` for an
        accepted row. An `output_ready` row is NOT terminal: Story has not acked it.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT a.gateway_asset_handle AS handle,
                          s.owner_scope AS owner_scope,
                          a.sha256 AS sha256, a.size_bytes AS size_bytes,
                          NULL AS upload_id,
                          s.state AS state, s.cancel_state AS cancel_state,
                          s.updated_at AS staging_at,
                          d.remote_state AS remote_state,
                          d.updated_at AS delivery_at
                   FROM render_staging_assets a
                   JOIN render_staging s ON s.id = a.render_id
                   LEFT JOIN render_delivery d ON d.render_id = s.id
                   UNION ALL
                   SELECT d.output_asset_handle AS handle,
                          s.owner_scope AS owner_scope,
                          NULL AS sha256, NULL AS size_bytes,
                          d.output_upload_id AS upload_id,
                          s.state AS state, s.cancel_state AS cancel_state,
                          s.updated_at AS staging_at,
                          d.remote_state AS remote_state,
                          d.updated_at AS delivery_at
                   FROM render_delivery d
                   JOIN render_staging s ON s.id = d.render_id
                   WHERE d.output_asset_handle IS NOT NULL"""
            ).fetchall()
        identity: dict[str, dict[str, Any]] = {}
        every_terminal: dict[str, bool] = {}
        conflicted: set[str] = set()
        settled_at: dict[str, float] = {}
        for row in rows:
            handle = row["handle"]
            terminal = (
                row["state"] == "acknowledged"
                or row["cancel_state"] == "cancelled"
                or (row["state"] == "accepted"
                    and row["remote_state"] in ("failed", "cancelled"))
            )
            every_terminal[handle] = every_terminal.get(handle, True) and terminal
            known = identity.setdefault(
                handle, {"owner_scope": None, "sha256": None,
                         "size_bytes": None, "upload_id": None}
            )
            for field in ("owner_scope", "sha256", "size_bytes", "upload_id"):
                value = row[field]
                if value is None:
                    continue
                if known[field] is None:
                    known[field] = value
                elif known[field] != value:
                    conflicted.add(handle)
            stamps = [stamp for stamp in (row["staging_at"], row["delivery_at"])
                      if stamp is not None]
            settled_at[handle] = max(
                settled_at.get(handle, 0.0), *(stamps or [0.0])
            )
        return {
            handle: {**identity[handle], "settled_at": settled_at[handle]}
            for handle, terminal in every_terminal.items()
            if terminal and handle not in conflicted
        }

    def referenced_render_asset_handles(self) -> set[str]:
        """Every handle a durable render row still points at, terminal or not."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT gateway_asset_handle AS handle FROM render_staging_assets
                   UNION
                   SELECT output_asset_handle AS handle FROM render_delivery
                   WHERE output_asset_handle IS NOT NULL"""
            ).fetchall()
        return {row["handle"] for row in rows}

    def acknowledged_render_outputs(self) -> list[dict[str, Any]]:
        """Acked outputs, so the sweep can release a `render-job:<id>` protection a crash left."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.id AS id, s.owner_scope AS owner_scope,
                          d.output_asset_handle AS output_asset_handle
                   FROM render_staging s
                   JOIN render_delivery d ON d.render_id = s.id
                   WHERE s.state = 'acknowledged'
                     AND d.output_asset_handle IS NOT NULL"""
            ).fetchall()
        return [dict(row) for row in rows]

    def record_render_asset_pending(
        self, render_id: str, logical_id: str, *, offset: int, length: int, sha256: str
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        logical_id = _render_text("logical_id", logical_id)
        if type(offset) is not int or not 0 <= offset <= 9_007_199_254_740_991:
            raise ValueError("offset must be a non-negative safe integer")
        length = _render_attempt(length)
        sha256 = _render_digest("sha256", sha256)
        with self._render_write():
            row = self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id = ? AND logical_id = ?",
                (render_id, logical_id),
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging asset not found")
            pending = (row["pending_offset"], row["pending_length"], row["pending_sha256"])
            wanted = (offset, length, sha256)
            if row["pending_offset"] is not None and pending != wanted:
                raise RenderJournalConflict("render asset pending range changed")
            if offset != row["confirmed_hub_offset"] or length > row["size_bytes"] - offset:
                raise RenderJournalConflict("render asset pending range is not contiguous")
            self._conn.execute(
                """UPDATE render_staging_assets SET pending_offset=?, pending_length=?, pending_sha256=?
                   WHERE render_id=? AND logical_id=?""", (*wanted, render_id, logical_id),
            )
            return dict(self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id=? AND logical_id=?",
                (render_id, logical_id),
            ).fetchone())

    def confirm_render_asset_range(
        self, render_id: str, logical_id: str, *, offset: int, length: int,
        sha256: str, hub_asset_handle: str | None = None,
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        logical_id = _render_text("logical_id", logical_id)
        if type(offset) is not int or not 0 <= offset <= 9_007_199_254_740_991:
            raise ValueError("offset must be a non-negative safe integer")
        length = _render_attempt(length)
        sha256 = _render_digest("sha256", sha256)
        if hub_asset_handle is not None:
            hub_asset_handle = _render_text("hub_asset_handle", hub_asset_handle)
            if len(hub_asset_handle) != 32 or any(c not in "0123456789abcdef" for c in hub_asset_handle):
                raise ValueError("hub_asset_handle is invalid")
        with self._render_write():
            row = self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id=? AND logical_id=?",
                (render_id, logical_id),
            ).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging asset not found")
            if (row["pending_offset"], row["pending_length"], row["pending_sha256"]) != (offset, length, sha256):
                raise RenderJournalConflict("render asset confirmation changed pending identity")
            confirmed = offset + length
            if confirmed > row["size_bytes"]:
                raise RenderJournalConflict("render asset confirmation exceeds declared size")
            if confirmed == row["size_bytes"] and hub_asset_handle is None:
                raise RenderJournalConflict("complete Hub asset requires its scoped handle")
            self._conn.execute(
                """UPDATE render_staging_assets
                   SET confirmed_hub_offset=?, hub_asset_handle=COALESCE(?,hub_asset_handle),
                       pending_offset=NULL,pending_length=NULL,pending_sha256=NULL
                   WHERE render_id=? AND logical_id=?""",
                (confirmed, hub_asset_handle, render_id, logical_id),
            )
            return dict(self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id=? AND logical_id=?",
                (render_id, logical_id),
            ).fetchone())

    def adopt_render_asset_publication(
        self, render_id: str, logical_id: str, *, sha256: str,
        size_bytes: int, hub_asset_handle: str,
    ) -> dict[str, Any]:
        """Adopt exact Hub publication evidence without inventing a sent range."""
        render_id = _render_uuid(render_id)
        logical_id = _render_text("logical_id", logical_id)
        sha256 = _render_digest("sha256", sha256)
        size_bytes = _render_attempt(size_bytes)
        hub_asset_handle = _render_text("hub_asset_handle", hub_asset_handle)
        if (len(hub_asset_handle) != 32
                or any(char not in "0123456789abcdef" for char in hub_asset_handle)):
            raise ValueError("hub_asset_handle is invalid")
        with self._render_write():
            row = self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id=? AND logical_id=?",
                (render_id, logical_id),
            ).fetchone()
            if (row is None or row["sha256"] != sha256 or row["size_bytes"] != size_bytes):
                raise RenderJournalConflict("published Hub asset identity changed")
            if (row["hub_asset_handle"] is not None
                    and row["hub_asset_handle"] != hub_asset_handle):
                raise RenderJournalConflict("published Hub asset handle changed")
            self._conn.execute(
                """UPDATE render_staging_assets
                   SET confirmed_hub_offset=?,hub_asset_handle=?,pending_offset=NULL,
                       pending_length=NULL,pending_sha256=NULL
                   WHERE render_id=? AND logical_id=?""",
                (size_bytes, hub_asset_handle, render_id, logical_id),
            )
            return dict(self._conn.execute(
                "SELECT * FROM render_staging_assets WHERE render_id=? AND logical_id=?",
                (render_id, logical_id),
            ).fetchone())

    def prepare_render_dispatch(
        self, render_id: str, *, outbound_hub_body: bytes, outbound_hub_body_sha256: str,
        dispatch_key: str, dispatch_fence: str,
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        outbound_hub_body_sha256 = _render_digest(
            "outbound_hub_body_sha256", outbound_hub_body_sha256
        )
        dispatch_key = _render_text("dispatch_key", dispatch_key)
        dispatch_fence = _render_text("dispatch_fence", dispatch_fence)
        if type(outbound_hub_body) is not bytes or not outbound_hub_body or len(outbound_hub_body) > 1024 * 1024:
            raise ValueError("outbound Hub body must be 1..1048576 bytes")
        if hashlib.sha256(outbound_hub_body).hexdigest() != outbound_hub_body_sha256:
            raise ValueError("outbound Hub body digest mismatch")
        now = _render_now()
        with self._render_write():
            row = self._conn.execute("SELECT * FROM render_staging WHERE id=?", (render_id,)).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging not found")
            if row["cancel_state"] is not None:
                raise RenderJournalCancelled("render staging was cancelled before dispatch")
            existing = row["outbound_hub_body_sha256"] is not None
            if existing:
                if (row["outbound_hub_body_sha256"], bytes(row["outbound_hub_body"]),
                        row["dispatch_key"], row["dispatch_fence"]) != (
                            outbound_hub_body_sha256, outbound_hub_body, dispatch_key, dispatch_fence):
                    raise RenderJournalConflict("render dispatch body changed")
                return self._render_staging_row(row)  # type: ignore[return-value]
            incomplete = self._conn.execute(
                """SELECT 1 FROM render_staging_assets WHERE render_id=?
                   AND (confirmed_hub_offset != size_bytes OR hub_asset_handle IS NULL) LIMIT 1""",
                (render_id,),
            ).fetchone()
            if incomplete is not None:
                raise RenderJournalConflict("render assets are not fully staged")
            self._conn.execute(
                """UPDATE render_staging SET outbound_hub_body=?,outbound_hub_body_sha256=?,
                   dispatch_key=?,dispatch_fence=?,state='hub_prepared',updated_at=? WHERE id=?""",
                (outbound_hub_body, outbound_hub_body_sha256, dispatch_key, dispatch_fence, now, render_id),
            )
            self._conn.execute(
                """INSERT INTO render_journal (
                    journal_id,story_uuid,intent_id,attempt_no,gateway_attempt_key,owner_scope,
                    inbound_story_body_sha256,outbound_hub_body_sha256,packet_sha256,
                    destination_site,destination_controller,destination_node,dispatch_key,
                    dispatch_fence,state,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (render_id,row["story_uuid"],row["intent_id"],row["attempt_no"],
                 row["gateway_attempt_key"],row["owner_scope"],row["inbound_story_body_sha256"],
                 outbound_hub_body_sha256,row["packet_sha256"],row["destination_site"],
                 row["destination_controller"],row["destination_node"],dispatch_key,
                 dispatch_fence,now,now),
            )
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def mark_render_dispatch_started(self, render_id: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        now = _render_now()
        with self._render_write():
            row = self._conn.execute("SELECT * FROM render_staging WHERE id=?", (render_id,)).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging not found")
            if row["cancel_state"] is not None:
                raise RenderJournalCancelled("render staging was cancelled before send")
            if row["state"] == "hub_prepared":
                self._conn.execute(
                    "UPDATE render_staging SET state='send_may_start',updated_at=? WHERE id=?",
                    (now, render_id),
                )
                self._conn.execute(
                    """UPDATE render_journal SET state='send_may_start',send_may_start_at=?,updated_at=?
                       WHERE journal_id=? AND state='prepared'""", (now, now, render_id),
                )
            elif row["state"] != "send_may_start":
                raise RenderJournalConflict("render dispatch cannot start another send")
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def mark_render_dispatch_unknown(self, render_id: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                "UPDATE render_staging SET state='send_unknown',updated_at=? WHERE id=? AND state='send_may_start'",
                (now, render_id),
            ).rowcount
            row = self._conn.execute("SELECT * FROM render_staging WHERE id=?", (render_id,)).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging not found")
            if changed != 1 and row["state"] != "send_unknown":
                raise RenderJournalConflict("render dispatch cannot become unknown")
            self._conn.execute(
                "UPDATE render_journal SET state='send_unknown',updated_at=? WHERE journal_id=? AND state='send_may_start'",
                (now, render_id),
            )
            return self._render_staging_row(row)  # type: ignore[return-value]

    def record_render_hub_acceptance(self, render_id: str, hub_job_id: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        hub_job_id = _render_text("hub_job_id", hub_job_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_staging SET state='accepted',hub_job_id=?,updated_at=?
                   WHERE id=? AND state IN ('send_may_start','send_unknown') AND hub_job_id IS NULL""",
                (hub_job_id, now, render_id),
            ).rowcount
            row = self._conn.execute("SELECT * FROM render_staging WHERE id=?", (render_id,)).fetchone()
            if row is None:
                raise RenderJournalNotFound("render staging not found")
            if changed != 1 and not (row["state"] == "accepted" and row["hub_job_id"] == hub_job_id):
                raise RenderJournalConflict("Hub acceptance conflicts with durable render state")
            self._conn.execute(
                """UPDATE render_journal SET state='accepted',hub_job_id=?,updated_at=?
                   WHERE journal_id=? AND state IN ('send_may_start','send_unknown')""",
                (hub_job_id, now, render_id),
            )
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def request_render_cancel_by_key(
        self, *, owner_scope: str, dispatch_key: str, expected_request_sha256: str
    ) -> dict[str, Any]:
        owner_scope = _render_text("owner_scope", owner_scope)
        dispatch_key = _render_text("dispatch_key", dispatch_key)
        expected_request_sha256 = _render_digest(
            "expected_request_sha256", expected_request_sha256
        )
        now = _render_now()
        with self._render_write():
            job = self._conn.execute(
                """SELECT * FROM render_staging
                   WHERE owner_scope=? AND gateway_attempt_key=?""",
                (owner_scope, dispatch_key),
            ).fetchone()
            if job is not None:
                if job["inbound_story_body_sha256"] != expected_request_sha256:
                    raise RenderJournalConflict("cancel request hash conflicts with render")
                request_digest = (
                    job["outbound_hub_body_sha256"] or job["inbound_story_body_sha256"]
                )
                cancel_key = job["dispatch_key"] or job["gateway_attempt_key"]
                first = job["cancel_state"] is None
                if first:
                    body = json.dumps(
                        {"expected_request_sha256": request_digest},
                        sort_keys=True, separators=(",", ":"),
                    ).encode("ascii")
                    self._conn.execute(
                        """UPDATE render_staging SET cancel_state='requested',cancel_body=?,
                           cancel_body_sha256=?,cancel_key=?,updated_at=? WHERE id=?""",
                        (body, hashlib.sha256(body).hexdigest(), cancel_key, now, job["id"]),
                    )
                return {
                    "state": "cancel_requested", "gateway_job_id": job["id"],
                    "owner_scope": owner_scope, "dispatch_key": cancel_key,
                    "expected_request_sha256": expected_request_sha256,
                }
            existing = self._conn.execute(
                "SELECT * FROM render_cancel_fences WHERE owner_scope=? AND dispatch_key=?",
                (owner_scope, dispatch_key),
            ).fetchone()
            if existing is not None:
                if existing["expected_request_sha256"] != expected_request_sha256:
                    raise RenderJournalConflict("cancel fence request hash conflicts")
                return dict(existing) | {"state": "cancelled_before_acceptance"}
            receipt = uuid.uuid4().hex
            self._conn.execute(
                """INSERT INTO render_cancel_fences (
                    owner_scope,dispatch_key,expected_request_sha256,fence_receipt,first_cancel_at
                ) VALUES (?,?,?,?,?)""",
                (owner_scope, dispatch_key, expected_request_sha256, receipt, now),
            )
            return {
                "state": "cancelled_before_acceptance", "owner_scope": owner_scope,
                "dispatch_key": dispatch_key,
                "expected_request_sha256": expected_request_sha256,
                "fence_receipt": receipt, "first_cancel_at": now,
                "gateway_job_id": None,
            }

    def update_render_delivery(
        self, render_id: str, *, expected_revision: int, remote_state: str,
        remote_attempt_id: str | None = None,
        remote_descriptor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        remote_state = _render_text("remote_state", remote_state)
        if remote_attempt_id is not None:
            remote_attempt_id = _render_text("remote_attempt_id", remote_attempt_id)
        descriptor = None if remote_descriptor is None else json.dumps(
            remote_descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_delivery SET revision=revision+1,remote_state=?,
                   remote_attempt_id=COALESCE(remote_attempt_id,?),remote_descriptor_json=?,
                   updated_at=? WHERE render_id=? AND revision=?
                   AND (remote_attempt_id IS NULL OR remote_attempt_id IS ?)""",
                (remote_state, remote_attempt_id, descriptor, now, render_id,
                 expected_revision, remote_attempt_id),
            ).rowcount
            if changed != 1:
                raise RenderJournalConflict("render delivery revision changed")
            return dict(self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone())

    def mark_render_cancel_send_started(self, render_id: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_staging SET cancel_outcome='send_unknown',updated_at=?
                   WHERE id=? AND cancel_state='requested' AND cancel_body IS NOT NULL
                     AND cancel_outcome IS NULL""",
                (now, render_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone()
            if changed != 1 and not (row is not None and row["cancel_state"] == "requested"
                                     and row["cancel_outcome"] == "send_unknown"):
                raise RenderJournalConflict("render cancel send already has an outcome")
            return self._render_staging_row(row)  # type: ignore[return-value]

    def settle_render_cancel(
        self, render_id: str, *, outcome: str, hub_job_id: str | None = None,
        remote_attempt_id: str | None = None,
        stopped_evidence_digest: str | None = None,
        fence_receipt: str | None = None, first_cancel_at: float | None = None,
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        outcome = _render_text("cancel outcome", outcome)
        if outcome not in ("accepted", "stopped"):
            raise ValueError("render cancel outcome is unsupported")
        launched = any(value is not None for value in (
            hub_job_id, remote_attempt_id, stopped_evidence_digest,
        ))
        never_launched = fence_receipt is not None or first_cancel_at is not None
        if launched:
            hub_job_id = _render_uuid(hub_job_id)
            remote_attempt_id = _render_text("remote_attempt_id", remote_attempt_id)
            stopped_evidence_digest = _render_digest(
                "stopped_evidence_digest", stopped_evidence_digest
            )
        if never_launched:
            fence_receipt = _render_text("fence_receipt", fence_receipt)
            if (len(fence_receipt) != 32
                    or any(char not in "0123456789abcdef" for char in fence_receipt)
                    or type(first_cancel_at) not in (int, float)
                    or isinstance(first_cancel_at, bool)
                    or not math.isfinite(first_cancel_at) or first_cancel_at < 0):
                raise ValueError("never-launched render evidence is invalid")
        if outcome != "stopped" and (launched or never_launched):
            raise ValueError("pending cancellation cannot carry terminal evidence")
        if outcome == "stopped" and launched == never_launched:
            raise ValueError("stopped cancellation requires one proof kind")
        now = _render_now()
        with self._render_write():
            job = self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone()
            delivery = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            if job is None or delivery is None:
                raise RenderJournalNotFound("render staging not found")
            if launched:
                if (job["hub_job_id"] != hub_job_id
                        or delivery["remote_attempt_id"] != remote_attempt_id
                        or delivery["stopped_evidence_digest"] not in (
                            None, stopped_evidence_digest
                        )):
                    raise RenderJournalConflict("render stopped evidence changed")
                self._conn.execute(
                    """UPDATE render_delivery
                       SET remote_state='cancelled',stopped_evidence_digest=?,updated_at=?
                       WHERE render_id=?""",
                    (stopped_evidence_digest, now, render_id),
                )
            elif never_launched:
                if (job["hub_job_id"] is not None
                        or job["terminal_fence_receipt"] not in (None, fence_receipt)
                        or job["terminal_first_cancel_at"] not in (None, first_cancel_at)):
                    raise RenderJournalConflict("render no-launch evidence changed")
                self._conn.execute(
                    """UPDATE render_staging
                       SET terminal_fence_receipt=?,terminal_first_cancel_at=?
                       WHERE id=?""",
                    (fence_receipt, first_cancel_at, render_id),
                )
            changed = self._conn.execute(
                """UPDATE render_staging SET cancel_outcome=?,cancel_state=?,updated_at=?
                   WHERE id=? AND cancel_outcome IN ('send_unknown','accepted')""",
                (outcome, "cancelled" if outcome == "stopped" else "requested", now, render_id),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone()
            if changed != 1 and not (row is not None and row["cancel_outcome"] == outcome):
                raise RenderJournalConflict("render cancel outcome changed")
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def clear_render_cancel_terminal(
        self, render_id: str, *, remote_state: str,
        remote_descriptor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Clear a pending cancel whose render already reached a terminal Hub state.

        The Hub leaves an already `failed` or `output_ready` job unchanged when a cancel
        arrives, so that pending cancel can never become `cancelled` and would be polled
        forever. Record the observed terminal remote state and drop the pending cancel: the
        row stops being due (for `failed`, by the render_due remote-state exclusion) or
        proceeds through the normal output path (for `output_ready`, whose receipt is kept
        so the import step still has its descriptor). No launch-stop evidence exists, so
        this is deliberately not a `stopped` settlement.
        """
        render_id = _render_uuid(render_id)
        remote_state = _render_text("remote_state", remote_state)
        if remote_state not in ("failed", "output_ready"):
            raise ValueError("render terminal cancel state is unsupported")
        descriptor = None if remote_descriptor is None else json.dumps(
            remote_descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        now = _render_now()
        with self._render_write():
            job = self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone()
            if job is None:
                raise RenderJournalNotFound("render staging not found")
            if job["cancel_state"] != "requested":
                raise RenderJournalConflict("render has no pending cancel to clear")
            changed = self._conn.execute(
                """UPDATE render_delivery SET revision=revision+1,remote_state=?,
                   remote_descriptor_json=COALESCE(?,remote_descriptor_json),updated_at=?
                   WHERE render_id=?""",
                (remote_state, descriptor, now, render_id),
            ).rowcount
            if changed != 1:
                raise RenderJournalNotFound("render delivery not found")
            # cancel_outcome returns to NULL so a later explicit cancel is a clean first
            # request; the terminal state is carried by remote_state, not this marker.
            self._conn.execute(
                """UPDATE render_staging SET cancel_state=NULL,cancel_outcome=NULL,updated_at=?
                   WHERE id=? AND cancel_state='requested'""",
                (now, render_id),
            )
            return self._render_staging_row(self._conn.execute(
                "SELECT * FROM render_staging WHERE id=?", (render_id,)
            ).fetchone())  # type: ignore[return-value]

    def publish_render_winner(
        self, render_id: str, *, expected_revision: int, winner_fence: str,
        output_upload_id: str, output_asset_handle: str, receipt_digest: str,
        gateway_receipt: bytes, gateway_receipt_digest: str,
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        winner_fence = _render_text("winner_fence", winner_fence)
        output_upload_id = _render_text("output_upload_id", output_upload_id)
        output_asset_handle = _render_text("output_asset_handle", output_asset_handle)
        receipt_digest = _render_digest("receipt_digest", receipt_digest)
        gateway_receipt_digest = _render_digest(
            "gateway_receipt_digest", gateway_receipt_digest
        )
        if (type(gateway_receipt) is not bytes or not gateway_receipt
                or len(gateway_receipt) > 1024 * 1024
                or hashlib.sha256(gateway_receipt).hexdigest() != gateway_receipt_digest):
            raise ValueError("Gateway receipt is invalid")
        now = _render_now()
        with self._render_write():
            job = self._conn.execute("SELECT * FROM render_staging WHERE id=?", (render_id,)).fetchone()
            if job is None:
                raise RenderJournalNotFound("render staging not found")
            if job["cancel_state"] is not None:
                raise RenderJournalCancelled("cancelled render cannot publish output")
            changed = self._conn.execute(
                """UPDATE render_delivery SET revision=revision+1,winner_fence=?,output_upload_id=?,
                   output_asset_handle=?,receipt_digest=?,gateway_receipt=?,
                   gateway_receipt_digest=?,updated_at=?
                   WHERE render_id=? AND revision=? AND output_asset_handle IS NULL""",
                (winner_fence, output_upload_id, output_asset_handle, receipt_digest,
                 gateway_receipt, gateway_receipt_digest, now, render_id, expected_revision),
            ).rowcount
            delivery = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            if changed != 1:
                if not (delivery is not None
                        and delivery["gateway_receipt"] is not None
                        and delivery["winner_fence"] == winner_fence
                        and delivery["output_asset_handle"] == output_asset_handle
                        and delivery["receipt_digest"] == receipt_digest
                        and bytes(delivery["gateway_receipt"]) == gateway_receipt
                        and delivery["gateway_receipt_digest"] == gateway_receipt_digest):
                    raise RenderJournalConflict("render output winner changed")
            self._conn.execute(
                "UPDATE render_staging SET state='output_ready',updated_at=? WHERE id=?",
                (now, render_id),
            )
            return dict(delivery)

    def bind_render_output_upload(
        self, render_id: str, *, expected_revision: int, output_upload_id: str
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        output_upload_id = _render_text("output_upload_id", output_upload_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_delivery SET output_upload_id=?,updated_at=?
                   WHERE render_id=? AND revision=? AND output_upload_id IS NULL""",
                (output_upload_id, now, render_id, expected_revision),
            ).rowcount
            row = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            if changed != 1 and not (row is not None and row["revision"] == expected_revision
                                     and row["output_upload_id"] == output_upload_id):
                raise RenderJournalConflict("render output upload changed")
            return dict(row)

    def prepare_render_ack(
        self, render_id: str, *, receipt_digest: str, attempt_id: str, ack_key: str
    ) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        receipt_digest = _render_digest("receipt_digest", receipt_digest)
        attempt_id = _render_text("attempt_id", attempt_id)
        ack_key = _render_text("ack_key", ack_key)
        body = json.dumps(
            {"attempt_id": attempt_id, "receipt_digest": receipt_digest},
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        now = _render_now()
        with self._render_write():
            row = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            if row is None or row["output_asset_handle"] is None:
                raise RenderJournalConflict("render output is not durably published")
            if row["remote_attempt_id"] != attempt_id:
                raise RenderJournalConflict("render acknowledgement attempt changed")
            if row["receipt_digest"] != receipt_digest:
                raise RenderJournalConflict("render acknowledgement receipt changed")
            if row["ack_body"] is None:
                self._conn.execute(
                    """UPDATE render_delivery SET ack_body=?,ack_body_sha256=?,ack_key=?,updated_at=?
                       WHERE render_id=?""",
                    (body, hashlib.sha256(body).hexdigest(), ack_key, now, render_id),
                )
            elif (bytes(row["ack_body"]), row["ack_key"]) != (body, ack_key):
                raise RenderJournalConflict("render acknowledgement identity changed")
            return dict(self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone())

    def settle_render_ack(self, render_id: str, *, ack_outcome: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        ack_outcome = _render_text("ack_outcome", ack_outcome)
        now = _render_now()
        with self._render_write():
            changed = self._conn.execute(
                """UPDATE render_delivery SET ack_outcome=?,updated_at=?
                   WHERE render_id=? AND ack_body IS NOT NULL
                     AND (ack_outcome IS NULL OR ack_outcome=?)""",
                (ack_outcome, now, render_id, ack_outcome),
            ).rowcount
            if changed != 1:
                raise RenderJournalConflict("render acknowledgement outcome changed")
            return dict(self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone())

    def acknowledge_render_story(self, render_id: str, *, receipt_digest: str) -> dict[str, Any]:
        render_id = _render_uuid(render_id)
        receipt_digest = _render_digest("receipt_digest", receipt_digest)
        now = _render_now()
        with self._render_write():
            row = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            if row is None or row["gateway_receipt_digest"] != receipt_digest:
                raise RenderJournalConflict("Story acknowledgement receipt changed")
            if row["story_ack_digest"] not in (None, receipt_digest):
                raise RenderJournalConflict("Story acknowledgement identity changed")
            self._conn.execute(
                "UPDATE render_delivery SET story_ack_digest=?,updated_at=? WHERE render_id=?",
                (receipt_digest, now, render_id),
            )
            self._conn.execute(
                "UPDATE render_staging SET state='acknowledged',updated_at=? WHERE id=?",
                (now, render_id),
            )
            return dict(self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone())

    def get_render_delivery(self, render_id: str) -> dict[str, Any] | None:
        render_id = _render_uuid(render_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM render_delivery WHERE render_id=?", (render_id,)
            ).fetchone()
            return dict(row) if row is not None else None

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
        job_group: str | None = None,
        priority: str | None = None,
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
                                  state, params, created_at, updated_at, next_action_at, caller,
                                  job_group, priority)
                VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?)
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
                    job_group or None,
                    priority if priority == BACKGROUND else None,
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
            if existing["state"] == "succeeded":
                return  # first result wins: a late one never rewrites a settled job
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

            # **A result for a job held as an unknown outcome settles the hold** (owner decision,
            # 2026-09-30: finished work is adopted, never left `failed / UNKNOWN_OUTCOME`). Any
            # other terminal job keeps its verdict and records the late result beside it.
            #
            # **Including a hold an operator closed without the attempt's answer** (2026-10-19).
            # `HOLD_CLOSED` is the one close that is a statement about *waiting*, not about the
            # attempt: nothing was sent upstream, so the answer this job was waiting for may still
            # arrive, and throwing it away is the one outcome nobody can undo. Measured: a pool
            # job closed at 13:36 whose own later attempt then answered — recorded in
            # `late_result_state` beside a `failed` verdict, so Story's by-key lookup read
            # `failed` and the answer reached nobody. A hold closed from the *upstream's* own
            # record (`close_hold`) keeps its verdict: that close is a proved answer, and a
            # later success does not outrank it.
            holding = (existing["state"] == "failed"
                       and (existing.get("recovery_state") == RECOVERY_UNKNOWN
                            or existing.get("error_code") == HOLD_CLOSED))
            if existing["state"] in TERMINAL_STATES and not holding:
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
                if holding:
                    fields.update(error_code=None, error_detail=None, recovery_context=None)
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
        evidence: dict[str, Any] | None = None,
        review_candidate: dict[str, Any] | None = None,
    ) -> None:
        """Settle once, retaining a late failure without reopening a terminal row.

        `evidence` is what the lane stated about the failed attempt (a Studio Hub item's machine,
        batch and run seconds), kept as the result payload so the job record and the analytics read
        it at the same `$.payload.hub_machine` path a success uses.

        `review_candidate` binds the refused narration section kept at the Hub, in this same
        commit; a late failure never binds one.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state, recovery_state FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return
            # **A hold is settled by the answer it was waiting for.** An `unknown_outcome` hold
            # says the upstream outcome could not be learned (`_write_restart_hold`); a failure
            # arriving for it *is* that answer, so the job becomes the failure it was instead of
            # staying a hold whose real code is buried in `late_error_code` (measured live
            # 2026-10-19: pool chat jobs held as "the gateway restarted" whose own attempt then
            # answered `LOCATION_NOT_READY` / `PROVIDER_UNAVAILABLE`, and were still held). This is
            # `succeed()`'s rule for a hold applied to the other outcome. Anything already settled
            # — a success, a cancel, a hold the operator closed — keeps its verdict.
            holding = (row["state"] == "failed"
                       and row["recovery_state"] == RECOVERY_UNKNOWN)
            if row["state"] in TERMINAL_STATES and not holding:
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
                    **({"result_json": json.dumps({"payload": evidence})} if evidence else {}),
                },
            )
            if review_candidate:
                try:
                    self._bind_review_candidate_locked(job_id, review_candidate)
                except (sqlite3.Error, KeyError, TypeError) as exc:   # the failure still settles
                    log.warning("job %s: review candidate not bound (%s)", job_id, type(exc).__name__)
            self._conn.commit()

    def _bind_review_candidate_locked(self, job_id: str, locator: dict[str, Any]) -> None:
        """Insert once; an identical replay is a no-op, a conflict or a full provider binds nothing."""
        row = self._conn.execute(
            "SELECT * FROM review_candidate_locators WHERE gateway_job_id = ?", (job_id,)
        ).fetchone()
        if row is not None:
            if any(row[key] != locator.get(key) for key in REVIEW_LOCATOR_FIELDS):
                log.warning("job %s: a different review candidate was refused", job_id)
            return
        used = self._conn.execute(
            "SELECT COUNT(*) FROM review_candidate_locators WHERE provider_id = ?",
            (locator.get("provider_id"),),
        ).fetchone()[0]
        if used >= REVIEW_LOCATORS_PER_PROVIDER:
            log.warning("job %s: review candidate not bound (location holds %s)", job_id, used)
            return
        self._conn.execute(
            "INSERT INTO review_candidate_locators (gateway_job_id, "
            + ", ".join(REVIEW_LOCATOR_FIELDS) + ", created_at, schema_version) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (job_id, *(locator[key] for key in REVIEW_LOCATOR_FIELDS), time.time()),
        )

    def review_locator(self, job_id: str) -> dict[str, Any] | None:
        """A job's bound review candidate, live or archived."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_candidate_locators WHERE gateway_job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

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

    def unclaim(self, job_id: str) -> bool:
        """Undo `mark_running` for a job nothing was sent for (a drain): queued again, no attempt used."""
        with self._lock:
            now = time.time()
            result = self._conn.execute(
                """UPDATE jobs SET state = 'accepted', next_action_at = ?, updated_at = ?,
                                   attempt_count = MAX(attempt_count - 1, 0)
                   WHERE id = ? AND state = 'running'
                     AND dispatch_started_at IS NULL AND COALESCE(upstream_job_id, '') = ''""",
                (now, now, job_id),
            )
            self._conn.commit()
            return result.rowcount == 1

    # -- the attempt record (A7) -------------------------------------------
    def record_attempt(self, job_id: str, *, location_id: str, code: str,
                       detail: str) -> dict[str, Any] | None:
        """Note that this job was tried at `location_id` and came back a **known** failure.

        Called only where the outcome is settled — the caller has already proved the upstream
        accepted nothing that could still be running. An unknown outcome never reaches here,
        which is what keeps "retry on a different machine" from duplicating work.

        Returns the new attempt log, or `None` when the row is gone.
        """
        entry = {"location": str(location_id or ""), "code": str(code or "")[:120],
                 "detail": str(detail or "")[:500], "at": time.time()}
        with self._lock:
            row = self._conn.execute(
                "SELECT attempted_locations, attempt_log FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            tried = _loads_list(row["attempted_locations"])
            if entry["location"] and entry["location"] not in tried:
                tried.append(entry["location"])
            log = _loads_list(row["attempt_log"])
            log.append(entry)
            self._conn.execute(
                "UPDATE jobs SET attempted_locations = ?, attempt_log = ?, updated_at = ? "
                "WHERE id = ?",
                (json.dumps(tried), json.dumps(log), time.time(), job_id),
            )
            self._conn.commit()
            return log

    def holds(self, *, limit: int = 200, sweepable: bool = False,
              provider_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Recovery holds: newest first for display, fair keyset batches for a sweep.

        `sweepable` narrows it to what the Hub can be asked about: unknown outcomes that carry an
        upstream id, optionally restricted to providers that can recover them. Without it,
        `retryable` rows and id-less chat holds fill the sweep's window
        and starve the real ones.

        A hold is a job whose upstream outcome is unknown. They were only ever visible one at a
        time, inside its own job response, so nobody could see how many were accumulating — and
        with a fleet whose houses lose power, they accumulate.
        """
        where = ("recovery_state = 'unknown_outcome' AND COALESCE(upstream_job_id, '') != ''"
                 if sweepable else "recovery_state IS NOT NULL")
        params: list[Any] = []
        if provider_ids is not None:
            if not provider_ids:
                return []
            where += " AND provider_id IN (" + ",".join("?" for _ in provider_ids) + ")"
            params.extend(provider_ids)
        with self._lock:
            if sweepable:
                # Stable keyset rotation: unresolved rows cannot keep monopolising the batch.
                rows = self._conn.execute(
                    f"SELECT * FROM jobs WHERE {where} AND id > ? ORDER BY id LIMIT ?",
                    (*params, self._hold_sweep_after, max(1, int(limit))),
                ).fetchall()
                if not rows:
                    rows = self._conn.execute(
                        f"SELECT * FROM jobs WHERE {where} ORDER BY id LIMIT ?",
                        (*params, max(1, int(limit))),
                    ).fetchall()
                self._hold_sweep_after = str(rows[-1]["id"]) if rows else ""
            else:
                rows = self._conn.execute(
                    f"SELECT * FROM jobs WHERE {where} ORDER BY updated_at DESC LIMIT ?",
                    (*params, max(1, int(limit))),
                ).fetchall()
        return [self._row_dict(row) for row in rows]

    def close_hold(self, job_id: str, *, resolution: str, detail: str) -> bool:
        """Close a hold because the **upstream's own record** settled it.

        `resolution` is the word that goes in the record — `failed` for a batch the Hub reports
        terminal, `requeued` for one the Hub has never heard of — and `detail` is the sentence a
        reader gets. The job keeps its upstream ids: closing a hold is a statement about what is
        known, never permission to forget what was sent.

        Only a caller that has *proved* the answer may call this. A hold closed on a guess is
        worse than a hold left open, because a hold is at least honest.
        """
        now = time.time()
        with self._lock:
            if resolution == "requeued":
                result = self._conn.execute(
                    """
                    UPDATE jobs
                       SET state = 'accepted', recovery_state = NULL, recovery_detail = NULL,
                           recovery_context = NULL, error_code = NULL, error_detail = ?,
                           retryable = 0, next_action_at = ?, updated_at = ?
                     WHERE id = ? AND recovery_state IS NOT NULL
                    """,
                    (detail[:500], now, now, job_id),
                )
            else:
                result = self._conn.execute(
                    """
                    UPDATE jobs
                       SET state = 'failed', recovery_state = NULL, recovery_detail = NULL,
                           -- settled now, so no longer "unknown": the Hub's own record decided it
                       error_code = CASE WHEN COALESCE(error_code, ?) = ? THEN 'PROVIDER_FAILED'
                                         ELSE error_code END,
                           error_detail = ?, retryable = 0, next_action_at = NULL, updated_at = ?
                     WHERE id = ? AND recovery_state IS NOT NULL
                    """,
                    (UNKNOWN_OUTCOME_CODE, UNKNOWN_OUTCOME_CODE, detail[:500], now, job_id),
                )
            self._conn.commit()
            return result.rowcount == 1

    def attempts_of(self, job_id: str) -> list[dict[str, Any]]:
        """Every settled attempt on this job, oldest first."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_log FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _loads_list(row["attempt_log"]) if row is not None else []

    def attempted_locations(self, job_id: str) -> list[str]:
        """The locations this job has already failed at, so a re-route cannot revisit one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempted_locations FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return [str(value) for value in _loads_list(row["attempted_locations"])] \
            if row is not None else []

    def defer(self, job_id: str, *, delay_s: float, reason: str, new_round: bool = False,
              unsent: bool = False) -> bool:
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

        `new_round`: a free Studio Hub job handed back by its Mac (workstation yield, low memory)
        with nowhere else to go. The Hub has finished with that batch, so nothing is pending: the
        dead batch id and the stale dispatch stamp are dropped (a late rebind and the queue ceiling
        both ignore a job that carries them), the locations tried so far are forgotten, and the
        attempt log's entries are marked `waited`, so when a Mac frees up *any* location -
        including the one that handed it back - may take the job and the retry ceiling starts afresh.

        `unsent`: a re-route found every lane full and bound the job to a site it has not sent to
        (`worker._next_machine`). Only the dispatch stamp and batch id go, so the late rebind may
        move it; the tried locations and the attempt log stay.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT state, attempt_log FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return False
            fields: dict[str, Any] = {
                "state": "accepted",
                "next_action_at": time.time() + max(float(delay_s), 0.0),
                "defer_reason": reason[:300],
            }
            if unsent:
                fields.update(upstream_job_id=None, dispatch_started_at=None)
            if new_round:
                fields.update(
                    upstream_job_id=None, dispatch_started_at=None, attempted_locations="[]",
                    attempt_log=json.dumps([{**e, "waited": True} for e in _loads_list(row["attempt_log"])]),
                )
            self._update_locked(job_id, fields)
            self._conn.commit()
            return True

    #: **The one statement of what "ahead in the queue" means**, used by both the
    #: single-job reader and the listing, so the number a caller is told and the
    #: order the console draws cannot disagree. `ahead_count` counts the jobs that
    #: come first: waiting in the same lane and created earlier; ties break on id,
    #: so two jobs created in the same microsecond still have an order rather than
    #: both being told they are next. `queue_size` is the lane's whole waiting set,
    #: which is the number bounding the position (`ahead_count + 1`).
    _QUEUE_RANK_SQL = """
        (SELECT COUNT(*) FROM jobs AS ahead
          WHERE ahead.state = 'accepted' AND ahead.modality = j.modality
            AND ((ahead.priority IS NOT NULL) < (j.priority IS NOT NULL)
                 OR ((ahead.priority IS NOT NULL) = (j.priority IS NOT NULL)
                     AND (ahead.created_at < j.created_at
                          OR (ahead.created_at = j.created_at AND ahead.id < j.id))))) AS ahead_count,
        (SELECT COUNT(*) FROM jobs AS lane
          WHERE lane.state = 'accepted' AND lane.modality = j.modality) AS queue_size
    """

    def queue_rank(self, job_id: str) -> dict[str, int] | None:
        """This job's place among the jobs waiting in its lane, or `None`.

        **Every `accepted` job is waiting, deferred or not.** `defer()` puts a job
        back with `next_action_at` and a reason, and the caller could not tell a
        queued job from a slow one: Story's Jobs page reads `queue_position`,
        `queue_eta_seconds` and `queue_reason` — it renders *"Waiting in the
        GenStudio provider queue… position N… about 2 minutes… pacing new
        requests"* — and the envelope published none of it. Worse, when it did
        publish a position it counted only the **deferred** jobs, so a job that had
        just been put back read "1" while the fresh submissions the dispatcher
        takes first stood in front of it.

        So the population is every `accepted` job of the same modality, and the
        order is first-come-first-served — the order `due()` is being moved to.
        `{"position": n, "size": m}` with `1 <= n <= m`, or `None` for a job that is
        not waiting: a dispatched job is not in this queue at all, and returning 1
        for a running job would read as "you are next" to a caller that is simply
        running.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT j.id, {self._QUEUE_RANK_SQL} FROM jobs AS j "  # noqa: S608 - a constant
                " WHERE j.id = ? AND j.state = 'accepted'",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {"position": int(row["ahead_count"]) + 1, "size": int(row["queue_size"])}

    def queue_position(self, job_id: str) -> int | None:
        """Where this job sits in the gateway's own queue, 1-based, or `None`.

        The single number `queue_rank` states, kept as its own name because it is
        what the job envelope has always published and what a caller reads. See
        `queue_rank` for the rule; this is a reader of it, never a second rule.
        """
        rank = self.queue_rank(job_id)
        return rank["position"] if rank is not None else None

    def waiting_normal_shapes(self) -> set[tuple[str, str]]:
        """`(model_id, modality)` of every normal fleet job still waiting, due or deferred.

        What a background job must not take a Mac from (`Worker._background_room`).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT model_id, modality FROM jobs WHERE state = 'accepted' "
                " AND priority IS NULL AND provider_id LIKE 'studio-hub-%'"
            ).fetchall()
        return {(str(row["model_id"] or ""), str(row["modality"] or "")) for row in rows}

    def priority_counts(self) -> list[dict[str, Any]]:
        """`[{modality, model_id, priority, waiting, running}]` over every unfinished job."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT modality, model_id, COALESCE(priority, 'normal') AS priority, state, COUNT(*) AS n "
                "  FROM jobs WHERE state IN ('accepted', 'running') GROUP BY 1, 2, 3, 4"
            ).fetchall()
        out: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            entry = out.setdefault((row["modality"], row["model_id"], row["priority"]), {
                "modality": row["modality"], "model_id": row["model_id"], "priority": row["priority"],
                "waiting": 0, "running": 0})
            entry["waiting" if row["state"] == "accepted" else "running"] += int(row["n"])
        return list(out.values())

    def waiting_jobs(self, *, limit: int = 200, normal_only: bool = False) -> list[dict[str, Any]]:
        """The queue itself: every job waiting to be dispatched, oldest first.

        The console's queue view reads this rather than `list_jobs(state="accepted")`
        because the **order is the queue** — `created_at`, ties by id, the order
        `queue_rank` ranks in — and a listing sorted newest-first would draw the
        job that is next as the last row. `limit` is bounded by the route.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = 'accepted' "
                + (" AND priority IS NULL" if normal_only else "")
                + " ORDER BY created_at, id LIMIT ?",
                (max(int(limit), 1),),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def voice_use(self, raw_voice_id: str, *, since: float) -> list[dict[str, Any]]:
        """TTS jobs since `since` whose params name this voice, per caller, busiest first.

        Matched on `<raw id>"` inside the stored params, so both the published
        (`provider::voice-…`) and the plain spelling count. ponytail: a LIKE over the
        window's TTS params (index on created_at bounds it); a voice column if it gets hot.
        """
        if not raw_voice_id:
            return []
        needle = f'%{raw_voice_id}"%'
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(caller, '') AS caller, COUNT(*) AS n, MAX(created_at) AS last "
                "FROM jobs WHERE created_at >= ? AND modality = 'tts' AND params LIKE ? "
                "GROUP BY caller ORDER BY n DESC",
                (since, needle),
            ).fetchall()
        return [{"caller": row["caller"], "jobs": int(row["n"]), "last_used_at": row["last"]}
                for row in rows]

    def active_counts_by_caller(self, column: str = "caller") -> dict[str, dict[str, int]]:
        """`{caller: {"waiting", "running"}}` over every unfinished job (or by `job_group`).

        A job with no caller is counted under `""` so the totals still add up.
        """
        if column not in ("caller", "job_group"):
            raise ValueError(column)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT COALESCE({column}, '') AS key, state, COUNT(*) AS n FROM jobs "
                " WHERE state IN ('accepted', 'running') GROUP BY key, state"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            entry = counts.setdefault(row["key"], {"waiting": 0, "running": 0})
            entry["waiting" if row["state"] == "accepted" else "running"] += int(row["n"])
        return counts

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

    def record_waiting(self, job_id: str, waiting: dict[str, Any] | None) -> None:
        """Remember why a running job is waiting for the upstream to start it (or that it is not)."""
        self._update(job_id, waiting_json=json.dumps(waiting) if waiting else None)

    def settle_cancelled(self, job_id: str) -> bool:
        """Settle a cancelled-while-running job whose upstream attempt has ended in failure."""
        with self._lock:
            result = self._conn.execute(
                """
                UPDATE jobs
                   SET state = 'cancelled', cancel_state = ?, next_action_at = NULL,
                       updated_at = ?
                 WHERE id = ? AND state = 'running' AND cancel_requested_at IS NOT NULL
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

    def record_route(self, job_id: str, provider_id: str, model_id: str | None = None) -> bool:
        """Point a queued job at a different location (and, optionally, member model) after a site refusal.

        The model is unchanged unless ``model_id`` is given (image pool re-route across members).
        """
        fields: dict[str, Any] = {"provider_id": provider_id, "upstream_job_id": None}
        if model_id is not None:
            fields["model_id"] = model_id
        self._update(job_id, **fields)
        return True

    def rebind_unsent(self, job_id: str, *, from_provider: str, to_provider: str,
                      model_id: str | None = None) -> bool:
        """Move a still-`accepted` job to another location (and member model), **only if nothing was ever sent**.

        The late bind: the location chosen at submit turned out to be full while another has
        room. One conditional UPDATE, so the guard and the write cannot be separated by a
        dispatch: a job with `dispatch_started_at` or an `upstream_job_id` (or one that has
        left `accepted`, or already moved) matches no row and stays exactly where it is --
        re-sending work that may already be running is duplicate paid work. `True` only when
        the row moved.
        """
        if model_id is None:
            sql = """UPDATE jobs SET provider_id = ?, updated_at = ?
                   WHERE id = ? AND provider_id = ? AND state = 'accepted'
                     AND dispatch_started_at IS NULL
                     AND COALESCE(upstream_job_id, '') = ''"""
            params = (to_provider, time.time(), job_id, from_provider)
        else:
            sql = """UPDATE jobs SET provider_id = ?, model_id = ?, updated_at = ?
                   WHERE id = ? AND provider_id = ? AND state = 'accepted'
                     AND dispatch_started_at IS NULL
                     AND COALESCE(upstream_job_id, '') = ''"""
            params = (to_provider, model_id, time.time(), job_id, from_provider)
        with self._lock:
            result = self._conn.execute(sql, params)
            self._conn.commit()
            return result.rowcount == 1

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
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_dict(row)

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._row_dict(row)

    def due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Everything ready to run, **oldest first** — the queue's own order.

        This ordered by `next_action_at`, which is the *readiness* clock rather than the arrival
        order, and `defer()` pushes that clock forward on every retry — so a job that had waited
        kept losing its place to work that arrived later (audit M3). A queue where waiting costs
        you your position is not a queue, and on a fleet of fifty Macs the jobs that wait most are
        exactly the ones being retried for a machine.

        `created_at` is the arrival order, ties broken by id so the order is total and stable —
        two jobs created in the same millisecond must not swap places between passes, or a
        deferred job could be starved by a coin toss. `next_action_at` still decides *whether* a
        job is ready; it no longer decides who goes first.
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE state NOT IN ('succeeded', 'failed', 'cancelled')
                  AND next_action_at IS NOT NULL
                  AND next_action_at <= ?
                ORDER BY created_at, id
                """,
                (now,),
            ).fetchall()
        return [self._row_dict(r) for r in rows]

    def recent(self, limit: int = 20, *, offset: int = 0) -> list[dict[str, Any]]:
        if offset > 2**63 - 1:  # a validated page can still exceed SQLite's integer range
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_dict(r) for r in rows]

    def job_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def list_jobs(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
        modality: str | None = None,
        since: float | None = None,
        before: float | None = None,
        hide_dismissed: bool = False,
    ) -> list[dict[str, Any]]:
        """Recent jobs, newest first, optionally filtered; `since`/`before` bound `created_at` so a
        reader can page past one answer by passing the last row's `created_at` as `before`."""
        clauses: list[str] = []
        params: list[Any] = []
        if hide_dismissed:
            clauses.append("(dismissed_at IS NULL OR dismissed_at < updated_at)")
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if before is not None:
            clauses.append("created_at < ?")
            params.append(before)
        if state:
            clauses.append("state = ?")
            params.append(state)
        if modality:
            clauses.append("modality = ?")
            params.append(modality)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                (*params, limit),
            ).fetchall()
        return [self._row_dict(r) for r in rows]

    def dismissable_failed_count(self, before: float | None = None) -> int:
        with self._lock:
            return int(self._conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {_DISMISSABLE} AND updated_at <= ?",  # noqa: S608
                (time.time() if before is None else before,),
            ).fetchone()[0])

    def needs_recovery_count(self, before: float) -> int:
        """Failed jobs whose outcome is unknown, last changed at or before `before` (the Jobs bulk count).

        An indexed COUNT; it used to load up to 1,000 whole hold rows to count them in Python.
        """
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = 'failed' AND recovery_state = ? AND updated_at <= ?",
                (RECOVERY_UNKNOWN, before),
            ).fetchone()[0])

    def dismiss_failed(self, before: float | None = None) -> int:
        """Hide settled failures (last changed at or before `before`) from the Jobs list.

        Writes `dismissed_at` only. `updated_at` is deliberately untouched: analytics, durations
        and retention read it, and a list action must not move them.
        """
        now = time.time()
        with self._lock:
            result = self._conn.execute(
                f"UPDATE jobs SET dismissed_at = ? WHERE {_DISMISSABLE} AND updated_at <= ?",  # noqa: S608
                (now, now if before is None else min(before, now)),
            )
            self._conn.commit()
            return result.rowcount

    def settled_between(self, start: float, end: float) -> list[dict[str, Any]]:
        """Jobs that finished (succeeded, failed or cancelled) in `[start, end)`, without their prose.

        Only the scalar columns the notifier needs; `result_json` holds the whole generated text and
        is never read here (bar the audio length of voice/music jobs). The window is on `updated_at`, which is when the job settled.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, modality, state, error_code, error_detail, asset_path, artifacts_json, updated_at, "
                # Only the one number out of result_json, never the generated text.
                "CASE WHEN modality IN ('tts', 'music') AND json_valid(result_json) THEN json_extract(result_json, '$.payload.audio_duration_s') "
                "END AS audio_duration_s "
                "FROM jobs WHERE updated_at >= ? AND updated_at < ? "
                "AND state IN ('succeeded', 'failed', 'cancelled')",
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def attempts_with_code(self, code: str, since: float) -> list[dict[str, Any]]:
        """Every attempt-log entry with this failure `code` made at or after `since`.

        `[{job_id, location, at}]`. Used to notice a Mac handing jobs back for memory repeatedly.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, attempt_log FROM jobs WHERE updated_at >= ? AND attempt_log LIKE ?",
                (since, f"%{code}%"),
            ).fetchall()
        hits: list[dict[str, Any]] = []
        for row in rows:
            for entry in _loads_list(row["attempt_log"]):
                if isinstance(entry, dict) and entry.get("code") == code and float(entry.get("at") or 0) >= since:
                    hits.append({"job_id": row["id"], "location": entry.get("location"), "at": entry.get("at")})
        return hits

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

        **Either spelling of the token counts.** OpenAI-shaped lanes state
        `prompt_tokens`/`completion_tokens`; Anthropic states `input_tokens`/
        `output_tokens`, and reading only the first meant no `anthropic-direct` model
        ever had a qualification row.

        Only the fields a job actually states come back; a row with no `usage` returns
        `NULL`s and is dropped by the reader rather than counted as zero.
        """
        with self._lock:
            rows = self._conn.execute(
                """
            SELECT provider_id, model_id, modality, created_at, updated_at,
                   COALESCE(
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.usage.prompt_tokens')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.usage.prompt_tokens') END,
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.usage.input_tokens')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.usage.input_tokens') END)
                       AS prompt_tokens,
                   COALESCE(
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.usage.completion_tokens')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.usage.completion_tokens') END,
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.usage.output_tokens')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.usage.output_tokens') END)
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

    def daily_by_model(self, since: float) -> list[dict[str, Any]]:
        """Per local day and model since `since`: jobs, outcomes, failure codes and queue wait.

        Queue wait is `created_at` to the dispatch that ran last (`dispatch_started_at`), so it holds
        every deferral and re-route before the work that settled the job. Scalars only, never prose.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT date(created_at, 'unixepoch', 'localtime') AS day, modality, model_id, state,
                          error_code, dispatch_started_at - created_at AS wait
                   FROM jobs WHERE created_at >= ? ORDER BY day, model_id""",
                (since,),
            ).fetchall()
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        waits: dict[tuple[str, str, str], list[float]] = {}
        for row in rows:
            key = (row["day"], row["modality"], row["model_id"])
            entry = groups.setdefault(key, {"day": key[0], "modality": key[1], "model_id": key[2], "jobs": 0,
                                            "succeeded": 0, "failed": 0, "cancelled": 0, "failures": {}})
            entry["jobs"] += 1
            if row["state"] in entry:
                entry[row["state"]] += 1
            if row["state"] == "failed":
                code = row["error_code"] or "UNKNOWN"
                entry["failures"][code] = entry["failures"].get(code, 0) + 1
            if row["wait"] is not None:
                waits.setdefault(key, []).append(max(float(row["wait"]), 0.0))
        for key, entry in groups.items():
            ordered = sorted(waits.get(key, []))
            for name, share in (("queue_wait_p50_s", 0.5), ("queue_wait_p90_s", 0.9)):
                entry[name] = round(ordered[max(math.ceil(share * len(ordered)) - 1, 0)], 1) if ordered else None
        return list(groups.values())

    def generation_analytics(self, *, limit: int = 20000,
                             mac_class: Any = None) -> dict[str, Any]:
        """What this gateway has generated, grouped three ways, from the job record.

        **One pass over scalars, never over prose.** `result_json` carries the whole
        generated text, so the read extracts only the five fields this needs and leaves
        the prose in the file — the same rule `usage_samples` documents. What is
        different here is that a median needs the individual values, not a summary, so
        this cannot be a single `GROUP BY`: the rows are the raw material and the
        grouping happens below. The row count is bounded for that reason.

        **Median beside mean, because generation time is not symmetric.** A lane's first
        item after a model load takes far longer than the ones behind it, so a mean over
        a handful of image jobs describes the cold load rather than the machine's real
        rate. The owner is reading this to decide where to send future work, and the
        median is the number that answers that; the mean is kept beside it so the gap
        between them is visible rather than hidden. Both are `None` — never `0` — when no
        job in the group stated a time, because "unstated" and "instant" must not render
        alike.

        `machine` is the Hub's own answer for the item: `"local"` means the controller
        ran it on itself, and a node id means a joined Mac did. The two are reported
        under `machine` with the provider beside them, so a controller's own work is
        never mistaken for a joined node's.

        Settled jobs only. A queued or running job has no duration yet, and counting one
        would drag every average toward zero.

        **`by_model_class` (D8): measured speed per model × Mac class**, when the caller passes
        `mac_class(provider_id, machine) -> "M4 · 16 GB"` (the job record stores the machine, not
        its chip). For ETAs and capacity planning only — routing does not read speed. Voice rows
        also carry `rtf_median`: generation seconds per second of audio, from `audio_duration_s`.
        """
        # `job_summaries` holds the jobs `archive_old` removed, so dropping a 90-day-old job
        # from the table does not change the totals this page shows.
        with self._lock:
            rows = self._conn.execute(
                """
            SELECT modality, model_id, provider_id, machine, state, seconds, bytes, audio_s FROM (
                SELECT created_at,
                       modality,
                       model_id,
                       provider_id,
                       CASE WHEN json_valid(result_json)
                            THEN json_extract(result_json, '$.payload.hub_machine') END
                           AS machine,
                       state,
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.generation_seconds')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.generation_seconds') END
                           AS seconds,
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.bytes')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.bytes') END
                           AS bytes,
                       CASE WHEN json_valid(result_json)
                             AND json_type(result_json, '$.payload.audio_duration_s')
                                 IN ('integer', 'real')
                            THEN json_extract(result_json, '$.payload.audio_duration_s') END
                           AS audio_s
                  FROM jobs
                 WHERE state IN ('succeeded', 'failed', 'cancelled')
                UNION ALL
                SELECT created_at, modality, model_id, provider_id, machine, state, seconds, bytes,
                       NULL
                  FROM job_summaries
            )
             ORDER BY created_at DESC
             LIMIT ?
                """,
                (limit,),
            ).fetchall()

        def group(key: Any) -> dict[str, Any]:
            return {
                "key": key,
                "jobs": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "bytes": 0,
                "bytes_jobs": 0,
                "_seconds": [],
                "_rtf": [],
            }

        buckets: dict[str, dict[str, Any]] = {}
        dimensions = {
            "by_modality": lambda r: r["modality"] or "unknown",
            "by_model": lambda r: r["model_id"] or "unknown",
            "by_machine": lambda r: (
                f"{r['machine'] or 'unstated'} @ {r['provider_id'] or 'unknown'}"
            ),
        }
        if mac_class is not None:
            dimensions["by_model_class"] = lambda r: (
                f"{r['model_id'] or 'unknown'} @ {mac_class(r['provider_id'], r['machine'])}"
            )
        for name, keyer in dimensions.items():
            buckets[name] = {}
        totals = group("all")

        for row in rows:
            state = row["state"]
            seconds = row["seconds"]
            size = row["bytes"]
            for name, keyer in dimensions.items():
                bucket = buckets[name].setdefault(keyer(row), group(keyer(row)))
                bucket["jobs"] += 1
                bucket[state] = bucket.get(state, 0) + 1
                if isinstance(size, (int, float)) and not isinstance(size, bool):
                    bucket["bytes"] += int(size)
                    bucket["bytes_jobs"] += 1
                if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                    bucket["_seconds"].append(float(seconds))
                    audio = row["audio_s"]
                    if isinstance(audio, (int, float)) and audio > 0 and state == "succeeded":
                        bucket["_rtf"].append(float(seconds) / float(audio))
            totals["jobs"] += 1
            totals[state] = totals.get(state, 0) + 1
            if isinstance(size, (int, float)) and not isinstance(size, bool):
                totals["bytes"] += int(size)
                totals["bytes_jobs"] += 1
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                totals["_seconds"].append(float(seconds))

        def finish(bucket: dict[str, Any]) -> dict[str, Any]:
            rtf = bucket.pop("_rtf")
            bucket["rtf_median"] = round(statistics.median(rtf), 2) if rtf else None
            values = sorted(bucket.pop("_seconds"))
            if values:
                middle = len(values) // 2
                median = (
                    values[middle]
                    if len(values) % 2
                    else (values[middle - 1] + values[middle]) / 2
                )
                bucket["seconds_min"] = round(values[0], 2)
                bucket["seconds_max"] = round(values[-1], 2)
                bucket["seconds_mean"] = round(sum(values) / len(values), 2)
                bucket["seconds_median"] = round(median, 2)
            else:
                # Stated by nobody in this group: absent, not zero.
                bucket["seconds_min"] = None
                bucket["seconds_max"] = None
                bucket["seconds_mean"] = None
                bucket["seconds_median"] = None
            bucket["timed_jobs"] = len(values)
            return bucket

        def ranked(name: str) -> list[dict[str, Any]]:
            finished = [finish(b) for b in buckets[name].values()]
            finished.sort(key=lambda b: (-b["jobs"], str(b["key"])))
            return finished

        return {
            "totals": finish(totals),
            "by_modality": ranked("by_modality"),
            "by_model": ranked("by_model"),
            "by_machine": ranked("by_machine"),
            **({"by_model_class": ranked("by_model_class")} if mac_class is not None else {}),
            "sampled": len(rows),
        }

    def speed_samples(self, model_id: str, *, limit: int = 500) -> list[tuple[str, str, float]]:
        """`(provider_id, hub_machine, generation_seconds)` for this model's latest successes.

        What `fleet` ranks a slow image model by (Qwen, owner 2026-10-09). Scalars in SQL, newest first.
        ponytail: whole-job seconds whatever the steps; divide by steps if callers start mixing them.
        """
        with self._lock:
            rows = self._conn.execute(
                """
            SELECT provider_id,
                   json_extract(result_json, '$.payload.hub_machine') AS machine,
                   json_extract(result_json, '$.payload.generation_seconds') AS seconds
              FROM jobs
             WHERE state = 'succeeded' AND model_id = ? AND json_valid(result_json)
             ORDER BY created_at DESC
             LIMIT ?
                """,
                (model_id, limit),
            ).fetchall()
        return [(str(row["provider_id"] or ""), str(row["machine"] or ""), float(row["seconds"]))
                for row in rows
                if row["machine"] and isinstance(row["seconds"], (int, float)) and row["seconds"] > 0]

    def tts_speed_samples(self, model_id: str, *, limit: int = 500) -> list[tuple[str, str, float]]:
        """`(provider_id, hub_machine, rtf)` for this voice model's latest successful jobs.

        RTF is generation seconds per second of audio, the same figure `generation_analytics`
        publishes as `rtf_median`. Read in SQL, scalars only, newest first.
        """
        with self._lock:
            rows = self._conn.execute(
                """
            SELECT provider_id,
                   json_extract(result_json, '$.payload.hub_machine') AS machine,
                   json_extract(result_json, '$.payload.generation_seconds') AS seconds,
                   json_extract(result_json, '$.payload.audio_duration_s') AS audio_s
              FROM jobs
             WHERE state = 'succeeded' AND modality = 'tts' AND model_id = ?
               AND json_valid(result_json)
             ORDER BY created_at DESC
             LIMIT ?
                """,
                (model_id, limit),
            ).fetchall()
        return [
            (str(row["provider_id"] or ""), str(row["machine"] or ""),
             float(row["seconds"]) / float(row["audio_s"]))
            for row in rows
            if isinstance(row["seconds"], (int, float)) and isinstance(row["audio_s"], (int, float))
            and row["seconds"] > 0 and row["audio_s"] > 0
        ]

    def _retire_archive(self, out_dir: Path, moment: float) -> int:
        """Retire month files older than the artifact retention into the Gateway Trash.

        Every archived row is already summarised in `job_summaries`; the month file is full-record
        history, and the owner asked that generated data ages out by itself. A failed move keeps the
        file in place; nothing here is unlinked.
        """
        try:
            from . import retention

            cutoff = moment - retention.settings()["max_age_days"] * 86400.0
        except Exception:  # noqa: BLE001 - retention must never stop archiving
            return 0
        retired = 0
        try:
            files = sorted(out_dir.glob("jobs-*.jsonl"))
        except OSError:
            return 0
        for path in files:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            try:
                retired += int(retention.retire(path, root=out_dir, namespace="job-archive", now=moment))
            except Exception:  # noqa: BLE001
                continue
        return retired

    def archive_old(
        self,
        *,
        days: float = 90.0,
        now: float | None = None,
        archive_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Move settled jobs older than `days` out of the live table, then VACUUM.

        Each job is first written in full to `job-archive/jobs-YYYY-MM.jsonl` (beside the
        database; one file per month of the job's creation), its analytics scalars are kept in
        `job_summaries`, and only then is the row deleted. A write that fails leaves the row
        where it is. Jobs that are not settled, or that carry a recovery hold, are never
        touched: a hold is a durable record of an unknown outcome.
        """
        moment = time.time() if now is None else now
        cutoff = moment - float(days) * 86400.0
        out_dir = Path(archive_dir) if archive_dir else Path(self.path).parent / "job-archive"
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                 WHERE state IN ('succeeded', 'failed', 'cancelled')
                   AND recovery_state IS NULL
                   AND updated_at < ?
                 ORDER BY created_at
                """,
                (cutoff,),
            ).fetchall()
            if not rows:
                return {"archived": 0, "days": float(days), "file_count": 0, "vacuumed": False,
                        "retired_files": self._retire_archive(out_dir, moment)}
            by_month: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                month = time.strftime("%Y-%m", time.gmtime(row["created_at"]))
                by_month.setdefault(month, []).append(row)
            done_ids: list[str] = []
            files = 0
            for month, group in by_month.items():
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    with open(out_dir / f"jobs-{month}.jsonl", "a", encoding="utf-8") as handle:
                        for row in group:
                            handle.write(json.dumps(dict(row), default=str) + "\n")
                except OSError:
                    continue  # not archived, so not deleted
                files += 1
                done_ids.extend(row["id"] for row in group)
            for start in range(0, len(done_ids), 500):
                chunk = done_ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"""
                    INSERT OR IGNORE INTO job_summaries
                        (id, created_at, modality, model_id, provider_id, machine, state,
                         seconds, bytes)
                    SELECT id, created_at, modality, model_id, provider_id,
                           CASE WHEN json_valid(result_json)
                                THEN json_extract(result_json, '$.payload.hub_machine') END,
                           state,
                           CASE WHEN json_valid(result_json)
                                 AND json_type(result_json, '$.payload.generation_seconds')
                                     IN ('integer', 'real')
                                THEN json_extract(result_json, '$.payload.generation_seconds') END,
                           CASE WHEN json_valid(result_json)
                                 AND json_type(result_json, '$.payload.bytes')
                                     IN ('integer', 'real')
                                THEN json_extract(result_json, '$.payload.bytes') END
                      FROM jobs WHERE id IN ({marks})
                    """,  # noqa: S608 - placeholders only
                    chunk,
                )
                self._conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", chunk)  # noqa: S608
            self._conn.commit()
            vacuumed = False
            pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            free_pages = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
            if done_ids and pages and free_pages * 5 >= pages:  # >= 20 % reclaimable
                self._conn.execute("VACUUM")
                vacuumed = True
        return {"archived": len(done_ids), "days": float(days), "file_count": files,
                "vacuumed": vacuumed, "retired_files": self._retire_archive(out_dir, moment)}

    def protected_input_paths(self) -> set[str]:
        """Inputs held by queued, running, or explicitly recoverable jobs."""
        with self._lock:
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
        with self._lock:
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
            archived = self._conn.execute("SELECT COUNT(*) AS n FROM job_summaries").fetchone()["n"]
            # Background rows only, one indexed search per state (no sort): a few hundred at most.
            background: dict[str, Any] = {"waiting": 0, "running": 0, "waiting_by_modality": {}}
            for state, key in (("accepted", "waiting"), ("running", "running")):
                for row in self._conn.execute(
                        "SELECT modality FROM jobs WHERE state = ? AND priority IS NOT NULL", (state,)):
                    background[key] += 1
                    if key == "waiting":
                        lane = background["waiting_by_modality"]
                        lane[row["modality"]] = lane.get(row["modality"], 0) + 1
        return {
            # Background jobs (0.2.165) are inside `by_state` too; this says how many of them there are.
            "background": background,
            "by_state": by_state,
            "by_modality": by_modality,
            "total": sum(by_state.values()),
            "archived": archived,
            "last_job_at": newest["t"] if newest else None,
        }
