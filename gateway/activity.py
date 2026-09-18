"""Recent Story-surface activity — "is Story Studio actually talking to me?"

The question came up for real. Story Studio was pointed at this gateway, but its
cached catalogue did not match what this gateway serves, and there was no way to
tell from outside whether the connection had ever succeeded: Story Studio keeps
its credential encrypted in its own app data, and this gateway logs at warning
level.

This is a diagnostic, not analytics. It keeps a bounded ring of the most recent
Story-surface calls — time, method, path, status, and **whether** a bearer token
was presented (never which one) — and nothing else. No bodies, no headers, no
identifiers, no persistence: it is lost on restart, which is the right default
for something nobody asked to keep.

It lives in its own module rather than inside `api.py` so the admin routes can
read it without importing the API module — `api.py` imports `admin` to mount its
router, so the reverse import is a cycle that only works when the API happens to
be imported first.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

#: How many calls are remembered. Small on purpose: enough to answer "did it
#: call, and did it authenticate", not enough to reconstruct a session.
_LIMIT = 12

_lock = threading.Lock()
_recent: deque[dict[str, Any]] = deque(maxlen=_LIMIT)


def record(method: str, path: str, status: int, authenticated: bool) -> None:
    """Note one call on the Story surface."""
    with _lock:
        _recent.append({
            "at": time.time(),
            "method": method,
            "path": path,
            "status": status,
            "authenticated": authenticated,
        })


def story_activity() -> dict[str, Any]:
    """Recent Story-surface calls, newest last."""
    with _lock:
        rows = list(_recent)
    return {
        "count": len(rows),
        "last_call_at": rows[-1]["at"] if rows else None,
        "recent": rows,
    }


def is_story_path(path: str) -> bool:
    """Whether a request belongs on this list.

    `/health` is included because it is the cheapest proof that Story Studio (or
    anything else) can reach the gateway at all; `/admin` deliberately is not,
    since the admin console and this app poll constantly and would drown out the
    one thing worth seeing.
    """
    return path.startswith("/v1/") or path == "/health"
