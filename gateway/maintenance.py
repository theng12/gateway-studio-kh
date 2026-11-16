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


def _token_file() -> Path:
    return Path(os.environ.get("KHU_HOLD_TOKEN_FILE")
                or Path.home() / "Library/KHUpdater/state/hold-token")


def _read_token_file() -> str | None:
    """Use the opened descriptor for both checks and a bounded read, including during rotation."""
    try:
        fd = os.open(_token_file(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
