"""The operator key (`kho_`) — one estate credential, scoped and revocable.

Step 1 of the approved design (`_orchestra/gateway-lead/agent-friendly-20261120/PROPOSAL-operator-key.md`,
§6): **additive**. A key works *beside* the guards already in force — no route loses a guard it has
today, and nothing here is trusted until a route asks for it.

The store is one 0600 file per Mac, in the shape the update hold already uses:
`~/Library/KHUpdater/state/operator-keys.json`, written atomically. Gateway, Story and Hub read the
same file, so one key covering three apps is one key and one revoke.

**Hashed at rest** — the plaintext is returned once, at issue, and never stored. `last_used_at` is
approximate on purpose: three apps read one file and a write per request would be a hot file with
three writers for a field nobody needs to the second, so it is flushed at most once a minute.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

PREFIX = "kho_"

#: The whole vocabulary. `empty-trash` is the wide destructive door: it is issued to nothing by
#: default, because it reaches every Mac's macOS Trash and is the least reversible action we have.
SCOPES: tuple[str, ...] = ("read", "create", "ops", "release", "owner", "empty-trash")

_lock = threading.Lock()
#: id -> the instant it was last used, flushed to disk at most once a minute (see the module doc).
_pending_touch: dict[str, float] = {}
_last_flush = 0.0
_FLUSH_EVERY_S = 60.0


def path() -> Path:
    """The one file every app reads. Overridable for tests and for a non-default install."""
    override = os.environ.get("GATEWAY_OPERATOR_KEYS")
    if override:
        return Path(override)
    return Path.home() / "Library" / "KHUpdater" / "state" / "operator-keys.json"


def _read() -> dict[str, Any]:
    target = path()
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return {"keys": []}
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        return {"keys": []}
    return data


def _write(data: dict[str, Any]) -> None:
    """Atomic, 0600 — the shape `hold-token` is written in, so the estate has one credential model."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(target.parent), prefix=".operator-keys.")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as stream:
            json.dump(data, stream, indent=1, sort_keys=True)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public(record: dict[str, Any]) -> dict[str, Any]:
    """A record as a page may show it: never the hash, never the token."""
    return {key: value for key, value in record.items() if key not in ("sha256",)}


def list_keys() -> list[dict[str, Any]]:
    return [public(record) for record in _read().get("keys") or [] if isinstance(record, dict)]


def mint(name: str, scopes: list[str] | tuple[str, ...], *, issued_by: str = "") -> tuple[str, dict[str, Any]]:
    """Issue one key. Returns `(token, record)`; the token is never recoverable afterwards.

    An unknown scope is refused rather than dropped: a key that silently carries less than it was
    asked for is a key that fails in the field instead of at issue.
    """
    wanted = [str(scope).strip() for scope in scopes or [] if str(scope).strip()]
    unknown = sorted(set(wanted) - set(SCOPES))
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(unknown)}; the scopes are {', '.join(SCOPES)}")
    if not wanted:
        raise ValueError("a key with no scope can do nothing; name at least one")
    token = PREFIX + secrets.token_urlsafe(32)
    record = {
        "id": secrets.token_hex(6),
        "name": str(name or "").strip() or "operator key",
        "sha256": _digest(token),
        "scopes": sorted(set(wanted)),
        "issued_at": time.time(),
        "issued_by": issued_by or "gateway",
        "last_used_at": None,
        "revoked_at": None,
    }
    with _lock:
        data = _read()
        data.setdefault("keys", []).append(record)
        _write(data)
    return token, public(record)


def revoke(key_id: str) -> bool:
    """Revoke one key by id. A key is never deleted — a record that a credential existed stays."""
    with _lock:
        data = _read()
        changed = False
        for record in data.get("keys") or []:
            if isinstance(record, dict) and record.get("id") == key_id and not record.get("revoked_at"):
                record["revoked_at"] = time.time()
                changed = True
        if changed:
            _write(data)
        return changed


def _find(token: str) -> dict[str, Any] | None:
    if not token or not token.startswith(PREFIX):
        return None
    wanted = _digest(token)
    for record in _read().get("keys") or []:
        if not isinstance(record, dict) or record.get("revoked_at"):
            continue
        stored = str(record.get("sha256") or "")
        if len(stored) == len(wanted) and hmac.compare_digest(stored, wanted):
            return record
    return None


def matches(authorization: str | None) -> dict[str, Any] | None:
    """The key record behind an `Authorization: Bearer kho_…` header, or `None`.

    Only a token with this module's own prefix is looked up, so the gateway key Story carries never
    reads this file.
    """
    bearer = str(authorization or "")
    token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else ""
    return _find(token)


def looks_like_key(authorization: str | None) -> bool:
    """Is this request *trying* to present an operator key, whatever its spelling?

    Matched case-insensitively on purpose: `KHO_…` and `kho_…` are the same claim, and a claim that
    is not honoured must be **refused**, never quietly downgraded to whatever the route trusts next.
    On a loopback-bound install that fall-through would accept a revoked key.
    """
    bearer = str(authorization or "")
    token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else ""
    return token.lower().startswith(PREFIX)


def touch(record: dict[str, Any]) -> None:
    """Remember that a key was used; the write is batched (see the module docstring)."""
    global _last_flush
    key_id = str(record.get("id") or "")
    if not key_id:
        return
    with _lock:
        _pending_touch[key_id] = time.time()
        if time.time() - _last_flush < _FLUSH_EVERY_S:
            return
        _last_flush = time.time()
        data = _read()
        for entry in data.get("keys") or []:
            if isinstance(entry, dict) and entry.get("id") in _pending_touch:
                entry["last_used_at"] = _pending_touch[entry["id"]]
        _pending_touch.clear()
        _write(data)


def allows(record: dict[str, Any], scope: str) -> bool:
    """Does this key carry `scope`? `owner` implies the lower read/ops scopes, never `empty-trash`."""
    carried = set(record.get("scopes") or [])
    if scope in carried:
        return True
    return scope in ("read", "create", "ops") and "owner" in carried
