"""Studio Hub client — locating and authenticating against the local Hub.

The Hub binds an **ephemeral port** (`--port` defaults to 0) and publishes the
resolved URL to a `hub-ready.json` file. So the base URL cannot be configured
statically; it has to be discovered at runtime, every time the Hub restarts.

Two credential scopes exist:
  * the **Hub token** (`<data dir>/.hub_token`) — full operator scope;
  * a **fleet token** (`.fleet_token` / `.kh_studio_token`) — a narrow allowlist
    that cannot read model install state.

This client uses the Hub token. It also deliberately does not call
`/api/hub/capabilities`: in standalone packaging that endpoint derives its model
rows from a GenStudio audit candidate that returns `None` unconditionally, so it
advertises zero models. `/api/hub/model-packages` is the authoritative source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import DATA_DIR, get

# Candidate locations for the Hub's readiness file. The Hub is a native macOS
# app, so its data directory follows the Application Support convention; the
# repo checks cover running it from a checkout.
#
# The real directory is `StudioHub Standalone` -- note the space before
# "Standalone" but none inside "StudioHub". Guessing at plausible spellings
# (`Studio Hub Standalone`, `StudioHubStandalone`) does not match, and a miss
# here presents as a permanent "Studio Hub not running" rather than an error.
_READY_CANDIDATES: tuple[str, ...] = (
    "~/Library/Application Support/StudioHub Standalone/hub-ready.json",
    "~/Library/Application Support/StudioHub/hub-ready.json",
    "~/Library/Application Support/Studio Hub/hub-ready.json",
    "~/Library/Application Support/studiohub/hub-ready.json",
    "~/Library/Application Support/StudioHubStandalone/hub-ready.json",
    "~/Library/Application Support/Studio Hub Standalone/hub-ready.json",
    "~/Projects/studiohub-standalone/.build/hub-ready.json",
    "~/Projects/studiohub-standalone/hub-ready.json",
    "~/.studiohub/hub-ready.json",
)

_TOKEN_CANDIDATES: tuple[str, ...] = (
    "~/Library/Application Support/StudioHub Standalone/.hub_token",
    "~/Library/Application Support/StudioHub/.hub_token",
    "~/Library/Application Support/Studio Hub/.hub_token",
    "~/Library/Application Support/studiohub/.hub_token",
    "~/Library/Application Support/StudioHubStandalone/.hub_token",
    "~/.studiohub/.hub_token",
)


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def find_ready_file() -> Path | None:
    """Locate `hub-ready.json`, honouring an explicit override first."""
    override = get("STUDIOHUB_READY_FILE", "").strip()
    if override:
        candidate = _expand(override)
        return candidate if candidate.exists() else None

    for raw in _READY_CANDIDATES:
        candidate = _expand(raw)
        if candidate.exists():
            return candidate
    return None


def read_ready() -> dict:
    path = find_ready_file()
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def hub_base_url() -> str:
    """Resolve the Hub's live base URL, or '' when it is not running.

    `hub-ready.json` may publish the URL under several keys depending on build;
    the port is also sometimes published separately, so both are handled.
    """
    override = get("STUDIOHUB_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    ready = read_ready()
    for key in ("url", "base_url", "baseUrl", "hub_url", "endpoint"):
        value = ready.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value.rstrip("/")

    host = ready.get("host") or "127.0.0.1"
    port = ready.get("port")
    if isinstance(port, int) and port > 0:
        if str(host) in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    return ""


def hub_token() -> str:
    """Resolve the Hub token.

    Order matters. The Hub publishes its token *inside* `hub-ready.json` on this
    build, so that is authoritative -- and because the port is ephemeral, the
    token in that file is necessarily paired with the live URL. A `.hub_token`
    file is still honoured as a fallback for builds that use one.
    """
    override = get("STUDIOHUB_HUB_TOKEN", "").strip()
    if override:
        return override

    ready = read_ready()
    published = ready.get("token")
    if isinstance(published, str) and published.strip():
        return published.strip()

    for candidate in (DATA_DIR / ".hub_token", *(_expand(p) for p in _TOKEN_CANDIDATES)):
        try:
            if candidate.exists():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return ""


def hub_headers(token: str = "") -> dict[str, str]:
    resolved = (token or hub_token()).strip()
    return {"X-Hub-Token": resolved} if resolved else {}


def hub_status() -> dict:
    """Small status projection for the admin UI."""
    ready_path = find_ready_file()
    base = hub_base_url()
    return {
        "running": bool(base),
        "base_url": base,
        "ready_file": str(ready_path) if ready_path else None,
        "token_present": bool(hub_token()),
    }
