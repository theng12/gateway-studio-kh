"""Browser sign-in for `/admin`: an owner-set password, server-side sessions, per-IP backoff.

The gateway key keeps working as a bearer token, exactly as before. This is a second
door for a phone browser over Tailscale:

* The owner sets the password in the app's own Settings, which talks to the gateway
  from this Mac with the key (`require_local_app` in admin.py). Only a salted scrypt
  hash is stored, never the password.
* A correct password (or the gateway key) at the login page mints a random session
  token. Only its SHA-256 is kept, server-side, and the browser gets it as an
  HttpOnly, SameSite=Strict cookie. Not `Secure`: the Tailscale URL is plain http,
  where a Secure cookie would never be sent back.
* A session ends after 30 days without use, on Sign out, or when the password is
  changed or removed.
* Failed attempts back off per client address: five free, then 30 s doubling to 15 min.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import APP_SUPPORT

COOKIE = "gateway_admin_session"
IDLE_SECONDS = 30 * 86400
MIN_LENGTH = 8
FREE_ATTEMPTS = 5
_LOCK_BASE_S = 30
_LOCK_MAX_S = 15 * 60
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}

_lock = threading.Lock()
_sessions: dict[str, float] | None = None  # sha256(token) -> last used
# ponytail: in-memory per-address counters, reset by a restart; enough behind Tailscale.
_failures: dict[str, tuple[int, float]] = {}  # address -> (failures, locked until)


def _password_path() -> Path:
    return APP_SUPPORT / "admin-password.json"


def _sessions_path() -> Path:
    return APP_SUPPORT / "admin-sessions.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=128 * 1024 * 1024, dklen=32)


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------
def password_set() -> bool:
    return bool(_read(_password_path()).get("hash"))


def set_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    _write(_password_path(), {
        "algorithm": "scrypt", **_SCRYPT, "salt": salt.hex(),
        "hash": _scrypt(password, salt, **_SCRYPT).hex(), "set_at": time.time(),
    })
    end_all_sessions()


def remove_password() -> None:
    try:
        _password_path().unlink()
    except FileNotFoundError:
        pass
    end_all_sessions()


def check_password(password: str) -> bool:
    record = _read(_password_path())
    if not record.get("hash") or not isinstance(password, str) or not password:
        return False
    try:
        got = _scrypt(password, bytes.fromhex(record["salt"]),
                      int(record["n"]), int(record["r"]), int(record["p"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), str(record["hash"]))


# ---------------------------------------------------------------------------
# Failed-attempt backoff
# ---------------------------------------------------------------------------
def retry_after(address: str) -> int:
    with _lock:
        _count, until = _failures.get(address, (0, 0.0))
    return max(0, math.ceil(until - time.time()))


def note_failure(address: str) -> None:
    with _lock:
        count, _until = _failures.get(address, (0, 0.0))
        count += 1
        until = 0.0
        if count >= FREE_ATTEMPTS:
            until = time.time() + min(_LOCK_MAX_S, _LOCK_BASE_S * 2 ** (count - FREE_ATTEMPTS))
        _failures[address] = (count, until)


def note_success(address: str) -> None:
    with _lock:
        _failures.pop(address, None)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _loaded() -> dict[str, float]:
    global _sessions
    if _sessions is None:
        raw = _read(_sessions_path())
        _sessions = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return _sessions


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = _loaded()
        for key in [k for k, seen in sessions.items() if now - seen > IDLE_SECONDS]:
            del sessions[key]
        sessions[_digest(token)] = now
        _write(_sessions_path(), sessions)
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    key = _digest(token)
    now = time.time()
    with _lock:
        sessions = _loaded()
        seen = sessions.get(key)
        if seen is None:
            return False
        if now - seen > IDLE_SECONDS:
            del sessions[key]
            _write(_sessions_path(), sessions)
            return False
        # Written at most hourly: idle expiry is measured in days, not requests.
        if now - seen > 3600:
            sessions[key] = now
            _write(_sessions_path(), sessions)
    return True


def end_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        sessions = _loaded()
        if sessions.pop(_digest(token), None) is not None:
            _write(_sessions_path(), sessions)


def end_all_sessions() -> None:
    with _lock:
        _loaded().clear()
        _write(_sessions_path(), {})


# ---------------------------------------------------------------------------
# CSRF: a cookie-authenticated write must come from this same origin.
# ---------------------------------------------------------------------------
def same_origin(request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    host = request.headers.get("host")
    if not source or not host or source == "null":
        return False
    return urlsplit(source).netloc.lower() == host.lower()


# ---------------------------------------------------------------------------
# Owner console: actions only the owner's own Gateway app window may take.
#
# Loopback is trusted and the gateway key also signs a browser in, so neither tells the owner apart
# from an agent holding the key. A permanent delete (a Mac's Trash Folder) therefore also needs this
# per-launch secret: the app makes it, keeps it in memory, and hands it to the gateway it spawns on a
# stdin pipe (never argv, the environment or a file, which other processes of this user can read).
# Only the app's native client sends it back, as `OWNER_HEADER`. A gateway the app adopted instead of
# spawning never got it, so the action stays refused until the app restarts the gateway.
# ---------------------------------------------------------------------------
OWNER_HEADER = "X-Gateway-Owner-Console"
_owner_secret = ""


def _read_owner_secret() -> None:
    global _owner_secret
    try:
        line = sys.stdin.readline(512).strip()
    except (OSError, ValueError):
        return
    if len(line) >= 32:
        _owner_secret = line


def owner_console(value: str | None) -> bool:
    return bool(_owner_secret) and bool(value) and hmac.compare_digest(str(value), _owner_secret)


if os.environ.get("GATEWAY_OWNER_CONSOLE_STDIN") == "1":
    threading.Thread(target=_read_owner_secret, name="owner-console-secret", daemon=True).start()
