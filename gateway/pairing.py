"""Automatic pairing for apps on this Mac.

`~/.kh/gateway-endpoint.json` (mode 0600, rewritten on every launch) carries the URL, a machine id
and a **pairing token**. That token can do exactly one thing: mint a worker token for a worker id
over loopback (`POST /admin/api/extension-workers/pair`). It is not the Gateway API key — it cannot
read providers, change settings, publish, or act as admin.

The switch **Allow local apps to pair automatically** (on by default) writes the file and mints the
token. Turning it off removes the file and revokes the token. The plaintext token lives only in that
0600 file; this module keeps its sha256 digest beside the switch, the way worker and guest tokens
are kept.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

from . import admin_session
from .config import APP_SUPPORT, bind_host, bind_port, write_secret

SCHEMA = 1
#: Distinct from the worker prefix (`ekw_`) and from any gateway key.
TOKEN_PREFIX = "gwp_"
#: A bind that still answers on 127.0.0.1 — the pairing endpoint is loopback-only.
_LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}

_lock = threading.Lock()


def endpoint_path() -> Path:
    """The file apps on this Mac read. Tests point `GATEWAY_ENDPOINT_FILE` at a temp path."""
    return Path(os.environ.get("GATEWAY_ENDPOINT_FILE")
                or (Path.home() / ".kh" / "gateway-endpoint.json"))


def state_path() -> Path:
    return APP_SUPPORT / "pairing.json"


def _state() -> dict:
    return admin_session._read(state_path())


def _save(state: dict) -> None:
    admin_session._write(state_path(), state)


def enabled() -> bool:
    """On by default: the file appears on the first launch after this feature ships."""
    return _state().get("enabled", True) is not False


def _url() -> str | None:
    """The URL a local app should call, or None when this gateway does not listen on loopback."""
    if bind_host() not in _LOOPBACK_BINDS:
        return None
    return f"http://127.0.0.1:{bind_port()}"


def _token(state: dict) -> dict | None:
    token = state.get("token")
    return token if isinstance(token, dict) and token.get("digest") else None


def _endpoint_token(state: dict) -> str | None:
    """The plaintext token already in the file, when its digest is the one this gateway recorded."""
    try:
        row = json.loads(endpoint_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = row.get("pairingToken") if isinstance(row, dict) else None
    recorded = _token(state)
    if not isinstance(token, str) or not token or not recorded:
        return None
    return token if secrets.compare_digest(admin_session._digest(token), recorded["digest"]) else None


def _write_endpoint(state: dict, token: str) -> None:
    url = _url()
    if url is None:
        raise RuntimeError("pairing needs a gateway that listens on loopback")
    write_secret(endpoint_path(), json.dumps({
        "url": url,
        "machineId": state["machineId"],
        "pairingToken": token,
        "schema": SCHEMA,
    }) + "\n")


def _remove_endpoint() -> None:
    try:
        endpoint_path().unlink()
    except FileNotFoundError:
        pass


def verify(authorization: str | None) -> bool:
    """Is this the live pairing token? Reads the state on every call, so a revoke is immediate."""
    recorded = _token(_state()) if enabled() else None
    if recorded is None:
        return False
    bearer = authorization or ""
    if not bearer.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(admin_session._digest(bearer[7:].strip()), recorded["digest"])


def _apply(state: dict) -> None:
    """Write the file and keep the token while on; remove both while off.

    Reads the switch from `state` itself: `set_enabled` changes it in memory before this runs,
    and re-reading the file here would act on the previous value.
    """
    if state.get("enabled", True) is False:
        state.pop("token", None)
        _remove_endpoint()
        return
    if _url() is None:
        # No loopback listener: a file would name an address that cannot answer.
        state.pop("token", None)
        _remove_endpoint()
        return
    token = _endpoint_token(state)
    if token is None:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        state["token"] = {"digest": admin_session._digest(token), "createdAt": time.time()}
    _write_endpoint(state, token)


def sync_on_startup() -> dict:
    """Called once per gateway start: rewrite the endpoint file for this launch."""
    with _lock:
        state = _state()
        if not isinstance(state.get("machineId"), str) or not state["machineId"]:
            state["machineId"] = "gw-" + uuid.uuid4().hex[:12]
        _apply(state)
        _save(state)
    return status()


def set_enabled(value: bool) -> dict:
    with _lock:
        state = _state()
        state["enabled"] = bool(value)
        if not isinstance(state.get("machineId"), str) or not state["machineId"]:
            state["machineId"] = "gw-" + uuid.uuid4().hex[:12]
        _apply(state)
        _save(state)
    return status()


def status() -> dict:
    """What the Settings page may show — never the token itself."""
    state = _state()
    return {
        "enabled": enabled(),
        "endpoint_file": str(endpoint_path()),
        "url": _url(),
        "machine_id": state.get("machineId"),
        "token_live": bool(_token(state)) and enabled() and _url() is not None,
    }
