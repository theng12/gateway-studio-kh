"""Accept the Launcher's private token for maintenance, never issue or rotate it."""
from __future__ import annotations

import hmac
import ipaddress
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_S = 20.0
MAX_TIMEOUT_S = 30.0
DEFAULT_PAUSE_S = 120.0
MAX_PAUSE_S = 900.0
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def is_token_form(value: str) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _own_token_file() -> Path:
    """Next to the app's other state, honouring `GATEWAY_APP_SUPPORT` the way config.py does."""
    support = os.environ.get("GATEWAY_APP_SUPPORT")
    root = Path(support) if support else Path.home() / "Library/Application Support/gateway-kh"
    return root / "hold-token"


def _legacy_token_file() -> Path:
    return Path.home() / "Library/KHUpdater/state/hold-token"


def token_path() -> Path:
    """The file this app reads: its own, unless a test names another."""
    return Path(os.environ.get("KHU_HOLD_TOKEN_FILE") or _own_token_file())


def _read_guarded(path: Path) -> str | None:
    """Read through the descriptor: 0600 regular file, ours, 64 hex — including during rotation."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or not 64 <= info.st_size <= 66):
            return None
        raw = os.read(fd, 67)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return token if len(raw) <= 66 and is_token_form(token) else None


def migrate_token_file() -> str | None:
    """Copy the estate hold token into this app's own file, once, and say so.

    Own file first: after the copy nothing reads the legacy path, so a half-migration cannot keep
    working by accident. Returns the one log line when a copy is made or refused, else None.
    """
    own = token_path()
    if os.environ.get("KHU_HOLD_TOKEN_FILE") or own.exists():
        return None
    legacy = _legacy_token_file()
    if not legacy.exists():
        return None
    token = _read_guarded(legacy)
    if token is None:
        return (f"hold token NOT migrated: {legacy} is not a 0600 regular file of 64 hex;"
                f" updates stay blocked until {own} exists")
    try:
        own.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(own, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return f"hold token NOT migrated: cannot write {own}: {exc}"
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return f"hold token migrated: copied {legacy} to {own}; the legacy path is no longer read"


def token_problem() -> str | None:
    """The sentence a person needs when an update cannot hold, or None when the token is usable."""
    path = token_path()
    if not path.exists():
        return f"update blocked: this app's hold token is missing at {path}"
    if _read_guarded(path) is None:
        return f"update blocked: this app's hold token at {path} is not a 0600 file of 64 hex"
    return None


def _token_file() -> Path:
    return token_path()


def _read_token_file() -> str | None:
    if (line := migrate_token_file()) is not None:
        print(line, flush=True)
    return _read_guarded(_token_file())


def matches_current(token: str) -> bool:
    """Is this the live token? A stale token is indistinguishable from any other bearer."""
    expected = _read_token_file()
    return expected is not None and hmac.compare_digest(token, expected)


def verify(authorization: str | None, hub_token: str | None, request: Any) -> bool:
    """One or two matching headers, a direct local peer, and no ambient credential paths."""
    headers = request.headers
    if ("origin" in headers or "cookie" in headers
            or any(k.lower() == "forwarded" or k.lower().startswith("x-forwarded-")
                   for k in headers)):
        return False
    if set(getattr(request, "query_params", {})) - {"timeout_s", "pause_s"}:
        return False
    if hasattr(headers, "getlist") and any(
        len(headers.getlist(name)) > 1 for name in ("authorization", "x-hub-token")
    ):
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_loopback:
            return False
    except (AttributeError, ValueError, TypeError):
        return False

    bearer = None
    if authorization is not None:
        if not authorization.lower().startswith("bearer "):
            return False
        bearer = authorization[7:].strip()
        if not is_token_form(bearer):
            return False
    supplied = hub_token.strip() if isinstance(hub_token, str) else None
    if hub_token is not None and not is_token_form(supplied):
        return False
    if bearer is None and supplied is None:
        return False
    if bearer is not None and supplied is not None and not hmac.compare_digest(bearer, supplied):
        return False
    expected = _read_token_file()
    return expected is not None and hmac.compare_digest(bearer or supplied, expected)


def clamp_drain_params(timeout_s: float, pause_s: float) -> tuple[float, float]:
    def finite(value: float, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    timeout = finite(timeout_s, DEFAULT_TIMEOUT_S)
    timeout = DEFAULT_TIMEOUT_S if timeout < 0 else min(timeout, MAX_TIMEOUT_S)
    pause = min(max(finite(pause_s, DEFAULT_PAUSE_S), 1.0), MAX_PAUSE_S)
    return timeout, pause
