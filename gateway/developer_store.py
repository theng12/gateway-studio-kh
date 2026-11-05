"""Durable Developer Mode attempts and measured approvals; never a production setting store."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .config import DATA_DIR, secret_file_mode


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class DeveloperStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "developer-tests.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        secret_file_mode(self.path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, campaign TEXT NOT NULL, data TEXT NOT NULL,
                    request BLOB NOT NULL, receipt BLOB, log BLOB NOT NULL DEFAULT X'');
                CREATE TABLE IF NOT EXISTS observations(
                    id INTEGER PRIMARY KEY, attempt TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS setting_actions(
                    id TEXT PRIMARY KEY, body BLOB NOT NULL, result BLOB);
                CREATE TABLE IF NOT EXISTS proposals(hash TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_previous(id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS measured(scope TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('revision', ?)", (uuid4().hex,))
        secret_file_mode(self.path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def revision(db) -> str:
        return db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]

    def create(self, campaign: str, body: bytes, attempts: list[dict]) -> bool:
        from .developer_contract import DeveloperTestError
        with self.connection() as db:
            existing = db.execute("SELECT body FROM campaigns WHERE id=?", (campaign,)).fetchone()
            if existing:
                if existing[0] != body:
                    raise DeveloperTestError(409, "RUN_CONFLICT", "This run ID already names different inputs.")
                return False
            db.execute("INSERT INTO campaigns VALUES(?,?)", (campaign, body))
            for row in attempts:
                request = row["request_bytes"]
                data = {k: v for k, v in row.items() if k != "request_bytes"}
                db.execute("INSERT INTO attempts(id,campaign,data,request) VALUES(?,?,?,?)",
                           (row["id"], campaign, encoded(data).decode(), request))
        return True

    def get(self, attempt: str) -> dict:
        from .developer_contract import DeveloperTestError
        with self.connection() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        if row is None:
            raise DeveloperTestError(404, "UNKNOWN_TEST", "No such Developer Mode attempt.")
        return {**json.loads(row["data"]), "request_bytes": bytes(row["request"]),
                "receipt_bytes": None if row["receipt"] is None else bytes(row["receipt"]),
                "log_bytes": bytes(row["log"])}

    def rows(self, campaign: str | None = None) -> list[dict]:
        with self.connection() as db:
            if campaign is None:
                ids = db.execute("SELECT id FROM attempts ORDER BY rowid DESC").fetchall()
            else:
                ids = db.execute("SELECT id FROM attempts WHERE campaign=? ORDER BY rowid", (campaign,)).fetchall()
        return [self.get(row[0]) for row in ids]

    def update(self, attempt: str, *, receipt: bytes | None = None,
               log: bytes | None = None, **fields) -> dict:
        from .developer_contract import DeveloperTestError
        with self.connection() as db:
            row = db.execute("SELECT data,receipt,log FROM attempts WHERE id=?", (attempt,)).fetchone()
            data = json.loads(row["data"])
            if row["receipt"] is not None and receipt is not None and row["receipt"] != receipt:
                raise DeveloperTestError(409, "IMMUTABLE_RECEIPT", "The terminal receipt changed.")
            data.update(fields)
            db.execute("UPDATE attempts SET data=?,receipt=?,log=? WHERE id=?",
                       (encoded(data).decode(), receipt if receipt is not None else row["receipt"],
                        log if log is not None else row["log"], attempt))
            db.execute("INSERT INTO observations(attempt,at,data) VALUES(?,?,?)",
                       (attempt, time.time(), encoded(fields).decode()))
        return self.get(attempt)

    def claim_send(self, attempt: str) -> bool:
        with self.connection() as db:
            data = json.loads(db.execute("SELECT data FROM attempts WHERE id=?", (attempt,)).fetchone()[0])
            if data["submitted"]:
                return False
            data.update(submitted=True, state="recovery_hold", connectivity="unknown",
                        reason="Submission started; acceptance must be reconciled at the original destination.")
            db.execute("UPDATE attempts SET data=? WHERE id=?", (encoded(data).decode(), attempt))
        return True

    def setting_action(self, action: str, body: bytes) -> tuple[bool, bytes | None]:
        from .developer_contract import DeveloperTestError
        with self.connection() as db:
            row = db.execute("SELECT body,result FROM setting_actions WHERE id=?", (action,)).fetchone()
            if row:
                if row[0] != body:
                    raise DeveloperTestError(409, "SETTING_CONFLICT", "This switch request ID already names different bytes.")
                return False, row[1]
            db.execute("INSERT INTO setting_actions(id,body) VALUES(?,?)", (action, body))
        return True, None

    def setting_result(self, action: str, result: bytes) -> None:
        from .developer_contract import DeveloperTestError
        with self.connection() as db:
            prior = db.execute("SELECT result FROM setting_actions WHERE id=?", (action,)).fetchone()
            if prior is None:
                raise DeveloperTestError(404, "UNKNOWN_SETTING_ACTION", "No such switch action.")
            if prior[0] is not None and prior[0] != result:
                raise DeveloperTestError(409, "SETTING_CONFLICT", "The retained switch result changed.")
            db.execute("UPDATE setting_actions SET result=? WHERE id=?", (result, action))

    def evidence(self) -> dict:
        with self.connection() as db:
            return {"store_revision": self.revision(db),
                    "measurements": [json.loads(row[0]) for row in db.execute("SELECT data FROM measured")],
                    "approvals": [json.loads(row[0]) for row in db.execute("SELECT body FROM approvals ORDER BY rowid")]}
