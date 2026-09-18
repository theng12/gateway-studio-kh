"""Fleet — the gateway's side of the location-controller topology.

Studio Hub's plan is explicit about the shape this has to fit:

    "GenStudio connects once to each location's controller using its address/IP
     and Hub token. Each controller manages its joined nodes; nodes do not
     contact GenStudio directly."

The gateway *is* the GenStudio endpoint Story Studio talks to, so the list of
locations belongs here: Story Studio must never learn about a second address.

Two things follow, and both are deliberate:

  * **The local Mac is always a location.** It is discovered the same way the
    rest of the gateway discovers the Hub (``hub-ready.json``), so a single-Mac
    install needs no configuration and there is no second code path to drift.
  * **A location is a controller, not a machine.** Nodes belong to their
    controller and are reported by it. Asking a node directly would be the
    routing layer the plan rules out.

Nothing here invents a value it did not read. A controller that is down reports
``reachable: false`` and an error; its machine list is empty rather than zeroed,
because "no data" and "no machines" are different facts and the UI has to be able
to tell them apart.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from pathlib import Path

import httpx

from .config import DATA_DIR
from .hub import hub_base_url, hub_token

#: Where the owner's location controllers are remembered. Tokens live here, so
#: the file is created 0600 like the provider credential store.
FLEET_PATH = Path(os.environ.get("GATEWAY_FLEET") or (DATA_DIR / "fleet.json"))

#: Per-request budget. A controller on the far end of a VPN is slower than
#: loopback, and a location that takes longer than this is reported unreachable
#: with the timeout as its reason rather than silently omitted.
_TIMEOUT = 6.0


def _read() -> list[dict]:
    if not FLEET_PATH.exists():
        return []
    try:
        payload = json.loads(FLEET_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("controllers") if isinstance(payload, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict) and row.get("url")]


def _write(rows: list[dict]) -> None:
    FLEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Create 0600 up front rather than chmod-ing afterwards: there must be no
    # window in which a token is world-readable.
    fd = os.open(str(FLEET_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"controllers": rows}, handle, indent=2)
    try:
        FLEET_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def controllers() -> list[dict]:
    """Configured remote controllers, with tokens masked for display."""
    return [_masked(row) for row in _read()]


def _masked(row: dict) -> dict:
    token = str(row.get("token") or "")
    return {
        "id": row.get("id"),
        "label": row.get("label") or row.get("url"),
        "url": row.get("url"),
        "token_set": bool(token),
        "token_hint": (token[:4] + "…" + token[-4:]) if len(token) > 12 else ("set" if token else ""),
        "added_at": row.get("added_at"),
    }


def add_controller(label: str, url: str, token: str) -> dict:
    """Remember a location controller. Re-adding the same address updates it."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("An address is required.")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    rows = _read()
    existing = next((row for row in rows if row.get("url") == url), None)
    if existing:
        existing["label"] = (label or existing.get("label") or url).strip()
        existing["token"] = (token or "").strip() or existing.get("token") or ""
    else:
        rows.append({
            "id": uuid.uuid4().hex[:12],
            "label": (label or url).strip(),
            "url": url,
            "token": (token or "").strip(),
            "added_at": time.time(),
        })
    _write(rows)
    return _masked(existing or rows[-1])


def remove_controller(controller_id: str) -> bool:
    rows = _read()
    keep = [row for row in rows if row.get("id") != controller_id]
    if len(keep) == len(rows):
        return False
    _write(keep)
    return True


# --------------------------------------------------------------------------
# Reading a location


def _fetch(client: httpx.Client, base: str, token: str, path: str):
    headers = {"X-Hub-Token": token} if token else {}
    response = client.get(base.rstrip("/") + path, headers=headers)
    response.raise_for_status()
    return response.json()


def _running_jobs(batches: list[dict]) -> list[dict]:
    """In-flight work only.

    A batch is running while it still has items in queued/running/retrying and no
    finish time. Everything else is history and belongs in Jobs, not here.
    """
    live = []
    for batch in batches:
        if batch.get("finished_at"):
            continue
        active = sum(int(batch.get(key) or 0) for key in ("queued", "running", "retrying"))
        if active <= 0:
            continue
        live.append({
            "id": batch.get("id"),
            "modality": batch.get("modality"),
            "model": batch.get("model"),
            "label": batch.get("label"),
            "total": batch.get("total"),
            "queued": batch.get("queued"),
            "running": batch.get("running"),
            "retrying": batch.get("retrying"),
            "done": batch.get("done"),
            "error": batch.get("error"),
            "stalled": batch.get("stalled"),
            "governor_note": batch.get("governor_note"),
            "started_at": batch.get("created_at"),
            "last_activity_at": batch.get("last_activity_at"),
        })
    live.sort(key=lambda row: row.get("started_at") or 0)
    return live


def _probe(base: str, token: str, *, timeout: float = _TIMEOUT) -> dict:
    """Read one controller. Never raises: a failure *is* the result."""
    started = time.time()
    location: dict = {
        "reachable": False,
        "error": None,
        "role": None,
        "version": None,
        "machines": [],
        "machine_summary": None,
        "nodes": [],
        "node_summary": None,
        "running_jobs": [],
        "latency_ms": None,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            overview = _fetch(client, base, token, "/api/hub/fleet/overview")
            nodes = _fetch(client, base, token, "/api/hub/fleet/nodes")
            try:
                jobs = _fetch(client, base, token, "/api/hub/jobs")
            except httpx.HTTPError:
                # Jobs are the least important part of "is this location alive";
                # a controller that reports machines but not jobs is still up.
                jobs = {"batches": []}
    except httpx.HTTPStatusError as exc:
        location["error"] = (
            "Rejected the Hub token (HTTP %d)." % exc.response.status_code
            if exc.response.status_code in (401, 403)
            else "HTTP %d from the controller." % exc.response.status_code
        )
        return location
    except httpx.HTTPError as exc:
        location["error"] = f"{type(exc).__name__}: {exc}" or "No response from the controller."
        return location

    node_rows = nodes.get("nodes") or []
    location.update({
        "reachable": True,
        "role": nodes.get("role"),
        "version": _version_from(overview),
        "machines": overview.get("machines") or [],
        "machine_summary": overview.get("summary") or {},
        "nodes": node_rows,
        "node_summary": nodes.get("summary") or {},
        "running_jobs": _running_jobs(jobs.get("batches") or []),
        "latency_ms": int((time.time() - started) * 1000),
    })
    return location


def _version_from(overview: dict) -> str | None:
    for machine in overview.get("machines") or []:
        if machine.get("version"):
            return str(machine["version"])
    return None


def locations() -> list[dict]:
    """The local Mac first, then every configured controller."""
    rows: list[dict] = []

    local_base, local_token, local_error = "", "", None
    try:
        local_base = hub_base_url() or ""
        local_token = hub_token() or ""
    except Exception as exc:  # noqa: BLE001 - a missing Hub is a state, not a crash
        local_error = str(exc)

    if local_base:
        local = _probe(local_base, local_token)
        local.update({"id": "local", "label": "This Mac", "url": local_base, "local": True})
    else:
        local = {
            "id": "local", "label": "This Mac", "url": "", "local": True,
            "reachable": False,
            "error": local_error or "Studio Hub is not running on this Mac.",
            "role": None, "version": None, "machines": [], "machine_summary": None,
            "nodes": [], "node_summary": None, "running_jobs": [], "latency_ms": None,
        }
    rows.append(local)

    for row in _read():
        probed = _probe(str(row["url"]), str(row.get("token") or ""))
        probed.update({
            "id": row.get("id"),
            "label": row.get("label") or row["url"],
            "url": row["url"],
            "local": False,
        })
        rows.append(probed)
    return rows


def snapshot() -> dict:
    """Everything the Fleet page shows, in one read."""
    rows = locations()
    machines_total = sum(len(row["machines"]) for row in rows)
    machines_up = sum(
        1 for row in rows for machine in row["machines"] if machine.get("reachable")
    )
    return {
        "locations": rows,
        "summary": {
            "locations": len(rows),
            "locations_up": sum(1 for row in rows if row["reachable"]),
            "machines": machines_total,
            "machines_up": machines_up,
            "running_jobs": sum(len(row["running_jobs"]) for row in rows),
        },
        "generated_at": time.time(),
    }
