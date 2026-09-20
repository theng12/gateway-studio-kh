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
import time
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


def hub_headers(token: str, *, local: bool = False) -> dict[str, str]:
    """Auth for one Hub, using the credential that belongs to **that** Hub.

    `token` is the credential stored for the provider being called. This Mac's
    own Hub has none stored — it is found through `hub-ready.json`, and its
    operator token *is* its credential by definition — so `local=True` reads it.
    `token` has no default on purpose: every call site has to say which
    credential it means, rather than inheriting one.

    **Nothing else may fall back to it.** This was `token or hub_token()`,
    unconditionally, so a controller whose credential was absent — added with no
    token, or its token since forgotten — had **this Mac's operator token** put
    in `X-Hub-Token` and sent to the owner-supplied remote address.
    `hub_token()` is full operator scope on this Mac's Hub: it can change that
    Hub's settings and identity, reach its storage and shut it down.
    `fleet.credential_scope` exists to stop the owner pasting an operator token
    into a location; falling back to one was the same mistake made by the code
    instead of by hand. An absent credential must stay absent, and the request
    fails as an ordinary 401.

    The failure this prevents is silent in both directions — a remote Hub that
    accepts the token would just work, and the owner would never learn that his
    own Hub's operator token was being broadcast to a site he added months ago.
    """
    resolved = (token or (hub_token() if local else "")).strip()
    return {"X-Hub-Token": resolved} if resolved else {}


def local_hub_headers() -> dict[str, str]:
    """Auth for **this Mac's** Hub, whose credential is the operator token.

    Named rather than defaulted, so that "whose credential is this?" is answered
    at the call site. Used by the paths that are inherently local: voice-clip
    annotation and anything else that reads `hub_base_url()` directly.
    """
    return hub_headers("", local=True)


#: How long a probe answer stands. The status route is polled by the console, and
#: a two-second timeout on every poll would make the page slow *exactly* when the
#: Hub is down — which is when someone is looking at it.
_PROBE_TTL_S = 5.0
_PROBE_CACHE: dict = {"at": 0.0, "base": "", "result": None}


def hub_probe(*, base: str | None = None, timeout: float = 2.0, force: bool = False) -> dict:
    """Does the Hub actually **answer**, right now?

    Three answers, never two, which is the whole reason this exists:
      * no address — nothing published a `hub-ready.json`;
      * an address that does not answer — a stale ready-file after a crash, a Hub
        still starting, or a port now held by something else;
      * an address that answers.

    `hub_status()` used to publish `running: bool(base)`, i.e. **file existence**.
    So the Overview page and the console said "Studio Hub is running" while the
    menu bar's own `/api/health` probe and `fleet._probe` both reported it
    unreachable. Two readers of one fact disagreeing, and the one on the page that
    exists to say what is available was the weaker one.

    Deliberately hits `/api/health`, the same route the menu bar uses, so both
    readers are answering the same question. It needs no token (measured: 200 bare).

    Cached for `_PROBE_TTL_S` and the age is published, so nobody has to guess how
    old the answer is.

    `base` is asked for explicitly by a caller that already resolved one — the
    annotation pass, which then uses that same base to do the work. Resolving it
    again here would mean the gate and the work could disagree about which Hub they
    are talking about, and it made the gate un-influencable from outside.
    """
    import httpx

    if base is None:
        base = hub_base_url()
    now = time.time()
    cached = _PROBE_CACHE.get("result")
    if (
        not force
        and cached is not None
        and _PROBE_CACHE.get("base") == base
        and (now - float(_PROBE_CACHE.get("at") or 0.0)) < _PROBE_TTL_S
    ):
        return dict(cached, age_ms=int((now - float(_PROBE_CACHE["at"])) * 1000))

    result: dict = {
        "running": False,
        "advertised": bool(base),
        "base_url": base,
        "status": None,
        "latency_ms": None,
        "detail": "",
    }
    if not base:
        result["detail"] = "no hub-ready.json — nothing says a Hub is running"
    else:
        started = time.time()
        try:
            response = httpx.get(
                f"{base.rstrip('/')}/api/health",
                timeout=httpx.Timeout(timeout, connect=min(timeout, 1.5)),
            )
            result["status"] = response.status_code
            result["latency_ms"] = int((time.time() - started) * 1000)
            if response.status_code == 200:
                result["running"] = True
                result["detail"] = f"answered /api/health in {result['latency_ms']}ms"
            else:
                result["detail"] = (
                    f"hub-ready.json advertises {base}, but /api/health answered "
                    f"HTTP {response.status_code}"
                )
        except Exception as exc:  # noqa: BLE001 - a failure is the result, not a crash
            result["latency_ms"] = int((time.time() - started) * 1000)
            result["detail"] = (
                f"hub-ready.json advertises {base}, but it did not answer "
                f"({type(exc).__name__}) — the file is stale or the Hub is starting"
            )

    _PROBE_CACHE["at"] = time.time()
    _PROBE_CACHE["base"] = base
    _PROBE_CACHE["result"] = dict(result)
    result["age_ms"] = int((time.time() - _PROBE_CACHE["at"]) * 1000)
    return result


def hub_status() -> dict:
    """Small status projection for the admin UI.

    `running` now means *it answered*, not *a file exists*. `advertised` keeps the
    old meaning available for anything that needs to distinguish "no Hub" from
    "a Hub that is not answering".
    """
    ready_path = find_ready_file()
    probe = hub_probe()
    return {
        "running": probe["running"],
        # What used to be called `running`: a URL exists on disk.
        "advertised": probe["advertised"],
        "base_url": probe["base_url"],
        "ready_file": str(ready_path) if ready_path else None,
        "token_present": bool(hub_token()),
        "probe": probe["detail"],
        "probe_status": probe["status"],
        "probe_age_ms": probe.get("age_ms"),
    }
