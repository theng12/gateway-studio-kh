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
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import DATA_DIR
from .hub import hub_base_url, hub_token

#: The location roster: id, label, address. **No secrets.** The Hub service
#: token for a location lives in the provider credential store under the
#: provider id this module derives, so there is exactly one secret store in the
#: gateway rather than two that can drift.
FLEET_PATH = Path(os.environ.get("GATEWAY_FLEET") or (DATA_DIR / "fleet.json"))

#: A configured location is also a *provider*, so its models are discovered and
#: its work is dispatched through exactly the same path as the local Hub — the
#: adapter already takes a per-provider base_url and token, so nothing else is
#: needed to send generation to another site.
PROVIDER_PREFIX = "studio-hub-"


def provider_id_for(controller_id: str) -> str:
    return f"{PROVIDER_PREFIX}{controller_id}"


def controller_id_from(provider_id: str) -> str | None:
    return provider_id[len(PROVIDER_PREFIX):] if provider_id.startswith(PROVIDER_PREFIX) else None

#: Per-request budget. A controller on the far end of a VPN is slower than
#: loopback, and a location that takes longer than this is reported unreachable
#: with the timeout as its reason rather than silently omitted.
_TIMEOUT = 6.0


def _token_for(controller_id: str) -> str:
    """The controller's Hub service token, from the one credential store.

    Imported lazily: `providers.registry` reads this module to build its
    definitions, so a module-level import here would be a cycle.
    """
    try:
        from .providers import registry
        return str(
            registry.credentials.resolved(provider_id_for(controller_id)).get("hub_token") or ""
        )
    except Exception:  # noqa: BLE001 - a missing store means "no token", not a crash
        return ""


def _set_token(controller_id: str, token: str) -> None:
    from .providers import registry
    registry.credentials.set_values(provider_id_for(controller_id), {"hub_token": token})


def _clear_token(controller_id: str) -> None:
    try:
        from .providers import registry
        registry.credentials.clear(provider_id_for(controller_id))
    except Exception:  # noqa: BLE001
        pass


#: Bumped on every write. The provider registry derives its location-provider
#: definitions from this roster and caches them; keying that cache on the file's
#: modification time is the classic fragile choice, because a filesystem with
#: one-second resolution can hand back the *same* timestamp for two writes in
#: the same second and the second change would be ignored. A counter owned by
#: the only writer cannot do that. The mtime is still part of the key, so an
#: edit made by something else is picked up too.
_revision = 0


def revision() -> int:
    return _revision


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
    global _revision
    _revision += 1
    FLEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The roster carries no secrets, but it is written 0600 anyway: which sites
    # this gateway talks to is nobody else's business on a shared Mac.
    fd = os.open(str(FLEET_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {"controllers": [{k: v for k, v in row.items() if k != "token"} for row in rows]},
            handle, indent=2,
        )
    try:
        FLEET_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def roster() -> list[dict]:
    """Configured locations, as definitions need them: no secrets involved."""
    return _read()


def controllers() -> list[dict]:
    """Configured remote controllers, with tokens masked for display."""
    return [_masked(row) for row in _read()]


def _masked(row: dict) -> dict:
    token = _token_for(str(row.get("id") or ""))
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
        controller_id = str(existing.get("id"))
        # A pre-existing roster may still carry the token; move it out rather
        # than leaving a second copy behind.
        legacy = str(existing.pop("token", "") or "")
    else:
        controller_id = uuid.uuid4().hex[:12]
        existing = {
            "id": controller_id,
            "label": (label or url).strip(),
            "url": url,
            "added_at": time.time(),
        }
        rows.append(existing)
        legacy = ""
    secret = (token or "").strip() or legacy or _token_for(controller_id)
    # The roster is written **first**: the provider definition that carries this
    # location's `hub_token` credential field is derived from the roster, and the
    # credential store refuses a field for a provider it has never heard of. So
    # the location has to exist before its secret can.
    _write(rows)
    if secret:
        _set_token(controller_id, secret)
    invalidate()
    return _masked(existing)


def remove_controller(controller_id: str) -> bool:
    rows = _read()
    keep = [row for row in rows if row.get("id") != controller_id]
    if len(keep) == len(rows):
        return False
    _write(keep)
    # The location is gone, so its token must not linger in the store.
    _clear_token(controller_id)
    invalidate()
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
        "activity": {"pulse": {}, "machines": [], "observed_at": None},
        "by_machine": {},
        "matrix": [],
        "jobs_total": None,
        "site_id": None,
        "controller_id": None,
        "ready": None,
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
            try:
                # The authoritative identity: `capabilities.controller` carries
                # role, Hub version and readiness in one object, plus the stable
                # `site_id`.
                #
                # This replaced a derivation assembled from two other endpoints —
                # `fleet/nodes.role` and a machine's `version` — and the Hub
                # session found that `fleet/nodes.role` was returning a constant
                # "controller" for a standalone Mac. Reading a field that was
                # silently wrong is exactly how a Fleet page shows the wrong role.
                capabilities = _fetch(client, base, token, "/api/hub/capabilities")
            except httpx.HTTPError:
                capabilities = {}
            try:
                # The Hub's own fleet analytics. This is where per-machine state
                # (working / ready / idle / offline) and per-machine job counts
                # come from; `fleet/overview` alone cannot answer "how many
                # machines are online, and which one did the work".
                stats = _fetch(client, base, token, "/api/hub/stats")
            except httpx.HTTPError:
                stats = {}
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
    activity = stats.get("fleet_activity") or {}
    controller = capabilities.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    location.update({
        "reachable": True,
        # Prefer the Hub's own identity; fall back to the older sources only when
        # it is unavailable, so a Hub that does not serve capabilities still works.
        "role": controller.get("role") or nodes.get("role"),
        "version": controller.get("studiohub_version") or _version_from(overview),
        "site_id": capabilities.get("site_id") or controller.get("site_id"),
        "controller_id": controller.get("controller_id"),
        "ready": controller.get("ready"),
        "machines": overview.get("machines") or [],
        "machine_summary": overview.get("summary") or {},
        "nodes": node_rows,
        "node_summary": nodes.get("summary") or {},
        "running_jobs": _running_jobs(jobs.get("batches") or []),
        "activity": {
            "pulse": activity.get("pulse") or {},
            "machines": activity.get("machines") or [],
            "observed_at": activity.get("observed_at"),
        },
        "by_machine": stats.get("by_machine") or {},
        "matrix": stats.get("matrix") or [],
        "jobs_total": stats.get("total"),
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
            "nodes": [], "node_summary": None, "running_jobs": [],
            "activity": {"pulse": {}, "machines": [], "observed_at": None},
            "by_machine": {}, "matrix": [], "jobs_total": None, "latency_ms": None,
            "site_id": None, "controller_id": None, "ready": None,
        }
    rows.append(local)

    configured = _read()
    if not configured:
        return rows

    # Controllers are probed **concurrently**. Sequentially, the owner's "at
    # least 3-4 controllers" is already several seconds of waiting, and the
    # hundreds this has to survive would be minutes — every one of them spent
    # waiting on the slowest site rather than on the work. The pool is bounded so
    # a large fleet cannot open an unbounded number of sockets.
    with ThreadPoolExecutor(max_workers=min(_MAX_PROBES, len(configured))) as pool:
        probed = list(pool.map(
            lambda row: _probe(str(row["url"]), _token_for(str(row.get("id") or ""))),
            configured,
        ))

    for row, result in zip(configured, probed):
        result.update({
            "id": row.get("id"),
            "label": row.get("label") or row["url"],
            "url": row["url"],
            "local": False,
        })
        rows.append(result)
    return rows


#: How many controllers are contacted at once. Bounded so a fleet of hundreds
#: cannot open hundreds of sockets, while still finishing far faster than
#: one-at-a-time.
#:
#: ponytail: a fixed pool size, not an adaptive one. Ceiling — with far more
#: than ~24 slow controllers a full refresh can exceed the 15s interval, so the
#: poller falls behind rather than degrading gracefully. Upgrade path — size the
#: pool from the measured round-trip, or refresh only the sites whose data has
#: aged out.
_MAX_PROBES = 24

#: How long a snapshot is reused. The page polls, and several clients (the
#: window, the console, a test) read the same data; re-probing every controller
#: on each read would multiply the load on every site by the number of readers.
_CACHE_TTL = 5.0
_cache: dict = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()


def invalidate() -> None:
    """Drop the cached snapshot.

    Called whenever the roster changes. Without it, adding a location leaves the
    next read serving the previous snapshot for up to the refresh interval, so
    the page you just changed would not show your change — the most confusing
    possible moment to be stale.
    """
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def _summarise(rows: list[dict]) -> dict:
    # Machine counts come from the Hub's own pulse, which is the only place that
    # distinguishes "ready" from "long idle" from "offline". `fleet/overview`
    # only knows reachable/unreachable, so it is the fallback, not the source.
    pulse: dict[str, int] = {}
    for row in rows:
        for state, count in (row["activity"]["pulse"] or {}).items():
            pulse[state] = pulse.get(state, 0) + int(count or 0)

    machines = sum(len(row["machines"]) for row in rows)
    machines_up = sum(
        1 for row in rows for machine in row["machines"] if machine.get("reachable")
    )
    if pulse:
        # Every state except `offline` means the Hub can reach the machine.
        online = sum(count for state, count in pulse.items() if state != "offline")
        offline = int(pulse.get("offline") or 0)
        machines_total = online + offline
    else:
        online, offline, machines_total = machines_up, machines - machines_up, machines

    completed = failed = 0
    for row in rows:
        for machine in row["activity"]["machines"]:
            completed += int(machine.get("completed") or 0)
            failed += int(machine.get("failed") or 0)

    return {
        "locations": len(rows),
        "locations_up": sum(1 for row in rows if row["reachable"]),
        "machines": machines_total,
        "machines_online": online,
        "machines_offline": offline,
        "pulse": pulse,
        "jobs_completed": completed,
        "jobs_failed": failed,
        "running_jobs": sum(len(row["running_jobs"]) for row in rows),
    }


def machines_flat(rows: list[dict]) -> list[dict]:
    """Every machine in the fleet as one row, with its location attached.

    A table is what scales: hundreds of machines as a flat list can be
    virtualised by the view, while hundreds of nested cards cannot. The join is
    done here, once, so the view never has to walk the tree.
    """
    out: list[dict] = []
    for row in rows:
        activity = {entry.get("machine"): entry
                    for entry in row["activity"]["machines"] if entry.get("machine")}
        seen: set[str] = set()
        for machine in row["machines"]:
            mid = str(machine.get("machine") or "")
            seen.add(mid)
            entry = activity.get(mid) or {}
            out.append({
                "location_id": row["id"],
                "location": row["label"],
                "location_local": row["local"],
                "location_reachable": row["reachable"],
                "machine": mid,
                "label": machine.get("label") or mid,
                "state": entry.get("state"),
                "reachable": machine.get("reachable"),
                "completed": entry.get("completed"),
                "failed": entry.get("failed"),
                "average_seconds": machine.get("average_seconds"),
                "median_runtime_s": entry.get("median_runtime_s"),
                "chip": machine.get("chip"),
                "memory_gb": machine.get("memory_gb"),
                "disk_free_gb": machine.get("disk_free_gb"),
                "health": machine.get("health"),
                "modalities": machine.get("modalities") or {},
                "limitation": entry.get("limitation"),
            })
        # A machine the activity report knows about but the overview omitted is
        # still a machine.
        for mid, entry in activity.items():
            if mid in seen:
                continue
            out.append({
                "location_id": row["id"],
                "location": row["label"],
                "location_local": row["local"],
                "location_reachable": row["reachable"],
                "machine": mid,
                "label": mid,
                "state": entry.get("state"),
                "reachable": None,
                "completed": entry.get("completed"),
                "failed": entry.get("failed"),
                "average_seconds": None,
                "median_runtime_s": entry.get("median_runtime_s"),
                "chip": None, "memory_gb": None, "disk_free_gb": None, "health": None,
                "modalities": {},
                "limitation": entry.get("limitation"),
            })
    return out


def running_flat(rows: list[dict]) -> list[dict]:
    """Work in flight, as one row per batch, with its location attached."""
    out: list[dict] = []
    for row in rows:
        for job in row["running_jobs"]:
            out.append({
                "location": row["label"],
                "location_local": row["local"],
                "modality": job.get("modality"),
                "model": job.get("model"),
                "label": job.get("label"),
                "total": job.get("total"),
                "running": job.get("running"),
                "queued": job.get("queued"),
                "retrying": job.get("retrying"),
                "done": job.get("done"),
                "error": job.get("error"),
                "stalled": job.get("stalled"),
                "started_at": job.get("started_at"),
            })
    return out


def snapshot(force: bool = False) -> dict:
    """Everything the Fleet page shows, in one read.

    Served from a short-lived cache so that polling, the console and a test all
    reading at once does not multiply the load on every controller.
    """
    now = time.time()
    with _cache_lock:
        cached = _cache["value"]
        if not force and cached and (now - _cache["at"]) < _CACHE_TTL:
            return cached

    rows = locations()
    value = {
        "locations": rows,
        "machines": machines_flat(rows),
        "running": running_flat(rows),
        "summary": _summarise(rows),
        "generated_at": time.time(),
    }
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = value
    return value


_refresher: threading.Thread | None = None


def start_refresher(interval: float = 15.0) -> None:
    """Keep the snapshot warm in the background.

    This is what makes the page survive a large fleet. A controller that is
    switched off takes the full timeout to answer, so reading on demand would
    let one unreachable site slow down the reader — and with hundreds of them,
    no timeout short enough to be tolerable is long enough for a VPN. Refreshing
    on a timer means the request path only ever reads a value that is already
    there, and a slow site costs the *poller* time, not the person looking at it.

    Started from the app's lifespan, so a short-lived process (a test, a CLI
    call) never pays for it.
    """
    global _refresher
    if _refresher is not None and _refresher.is_alive():
        return

    def loop() -> None:
        while True:
            try:
                snapshot(force=True)
            except Exception:  # noqa: BLE001 - the poller must outlive any one site
                pass
            time.sleep(interval)

    _refresher = threading.Thread(target=loop, name="fleet-refresh", daemon=True)
    _refresher.start()
