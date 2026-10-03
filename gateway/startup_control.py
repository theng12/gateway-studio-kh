"""See and change, from the Gateway, what each Studio Hub Mac starts when it starts.

**Why this exists (owner, 2026-10-02).** "I set up ~30 Macs and didn't tick 'start services on
startup'." A Mac whose services are off is silent to its controller after every reboot, and nobody can
tell from here which ones. Studio Hub 0.6.127 reports the two settings (`startup` on every machine row)
and accepts changes through the fleet path this gateway already uses.

**No new door.** A request goes to the *controller* that owns the Mac, with the Fleet token the gateway
holds for it (`X-Hub-Token`, the credential every other fleet read and job action uses) - for this Mac's
own Hub, its own Hub token. The controller relays it to the node with the same credential and the node's
own allowlist decides. This module holds no credential of its own and opens no route that is not behind
`require_admin`.

**What it cannot do** is reach a Mac whose Studio Hub is not running: nothing there is listening. That is
the case the setting exists to prevent, so a Mac that is `no_hub` is reported as such with the one thing
to do (start the services on it once) rather than as a failure of this call. A controller on a Hub older
than 0.6.127 has no such route; the answer says so (`HUB_TOO_OLD`) instead of a bare 404.

Every request, bulk run and outcome is appended to `startup_audit.jsonl` (owner-only, bounded).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from . import config, fleet

log = logging.getLogger("gateway.startup")

#: What a caller may ask a Mac to do. The Hub validates again; refusing here keeps a typo off the wire.
ACTIONS = ("set_start_services_on_launch", "set_open_at_login", "start_services", "restart_services")
_BOOL_ACTIONS = ("set_start_services_on_launch", "set_open_at_login")
_MACHINE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
#: A restart can wait for the app to collect it (about ten seconds), so the budget is generous.
_TIMEOUT = httpx.Timeout(40.0, connect=6.0)
_BULK_WORKERS = 8
_AUDIT_KEEP = 500
_audit_lock = threading.Lock()


class StartupError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("detail") or body.get("code") or status))
        self.status, self.body = status, body


def audit_path() -> Path:
    return config.DATA_DIR / "startup_audit.jsonl"


def audit(event: str, **fields: Any) -> None:
    """One JSON line per event. Never raises: an audit that cannot be written is logged, not a refusal."""
    row = {"at": round(time.time(), 3), "event": event, **fields}
    log.info("startup %s %s", event, json.dumps(fields, sort_keys=True, default=str))
    with _audit_lock:
        try:
            path = audit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            lines.append(json.dumps(row, sort_keys=True, default=str))
            scratch = path.with_name(path.name + ".tmp")
            descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines[-_AUDIT_KEEP:]) + "\n")
            os.replace(scratch, path)
        except OSError as exc:
            log.warning("startup audit could not be written: %s", exc)


def recent_audit(limit: int = 50) -> list[dict[str, Any]]:
    try:
        lines = audit_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, min(limit, _AUDIT_KEEP)):]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return list(reversed(rows))


def validate(action: object, value: object, force: object) -> tuple[str, bool | None, bool]:
    """The request as the Hub will see it, or a `StartupError(400)` naming what is wrong."""
    if action not in ACTIONS:
        raise StartupError(400, {"ok": False, "code": "UNKNOWN_ACTION",
                                 "detail": f"action must be one of {', '.join(ACTIONS)}"})
    if action in _BOOL_ACTIONS and not isinstance(value, bool):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED",
                                 "detail": f"{action} needs value: true or false"})
    if not isinstance(force, bool):
        raise StartupError(400, {"ok": False, "code": "BAD_FORCE", "detail": "force must be true or false"})
    return str(action), value if action in _BOOL_ACTIONS else None, force


def _target(controller_id: str) -> tuple[str, str]:
    """The controller's base address and the credential this gateway presents to it."""
    if controller_id == fleet.LOCAL_ID:
        from .hub import hub_base_url, hub_token
        base, token = hub_base_url() or "", hub_token() or ""
    else:
        row = next((row for row in fleet.roster() if str(row.get("id")) == controller_id), None)
        base = str((row or {}).get("url") or "")
        token = fleet._token_for(controller_id)
    if not base:
        raise StartupError(404, {"ok": False, "code": "UNKNOWN_LOCATION", "detail": "No such location."})
    return base.rstrip("/"), token


def request(controller_id: str, machine_id: str, action: object, value: object = None,
            force: object = False, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Ask one Mac to change a setting or restart its services, through its controller.

    Returns the Hub's own answer (`state`: `applied`, `queued`, `handed_to_app`, `already_running`, or
    `failed`) with the machine named. Raises `StartupError` with the status and body to return when the
    request is refused - invalid, unauthorised, work in progress, or the Mac cannot be reached.
    """
    action, value, force = validate(action, value, force)
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    base, token = _target(controller_id)
    headers = {"X-Hub-Token": token} if token else {}
    body: dict[str, Any] = {"action": action, "force": force, "requested_by": "gateway-admin"}
    if value is not None:
        body["value"] = value
    audit("requested", controller=controller_id, machine=machine_id, action=action, value=value, force=force)
    owned = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = client.post(f"{base}/api/hub/fleet/nodes/{machine_id}/startup", headers=headers, json=body)
    except httpx.HTTPError as exc:
        error = StartupError(502, {"ok": False, "code": "CONTROLLER_UNREACHABLE",
                                   "detail": "The controller did not answer: "
                                             f"{type(exc).__name__}. Check it is awake and on the network."})
        audit("result", controller=controller_id, machine=machine_id, action=action, status=502,
              code="CONTROLLER_UNREACHABLE")
        raise error from exc
    finally:
        if owned:
            client.close()
    try:
        answered = response.json()
    except ValueError:
        answered = {}
    if not isinstance(answered, dict):
        answered = {}
    if response.status_code in (404, 405) and "code" not in answered:
        answered = {"ok": False, "code": "HUB_TOO_OLD",
                    "detail": "This controller runs a Studio Hub older than 0.6.127, which cannot change "
                              "startup settings remotely. Update it first (Fleet > Release)."}
    elif response.status_code in (401, 403) and "code" not in answered:
        answered = {"ok": False, "code": "CONTROLLER_REFUSED",
                    "detail": "The controller refused this gateway's credential for startup changes."}
    answered.setdefault("ok", response.is_success)
    answered["machine"] = machine_id
    audit("result", controller=controller_id, machine=machine_id, action=action,
          status=response.status_code, state=answered.get("state"), code=answered.get("code"),
          ok=answered.get("ok"))
    if not response.is_success:
        raise StartupError(response.status_code if response.status_code < 500 else 502, answered)
    return answered


def rename(controller_id: str, machine_id: str, name: object, *,
           client: httpx.Client | None = None) -> dict[str, Any]:
    """Rename one Mac (owner request 2026-10-02), through its controller like every action here.

    The controller renames the Mac **on the Mac** first and then its roster row, so the Mac's own
    Connections page, its controller and this Fleet page all show the new name, and the Mac's next
    report does not undo it. A Mac that is off, or on a Hub without fleet renaming, keeps its name and
    the answer says why. Only the name changes: the machine id, routing and membership never read it.
    """
    clean = str(name or "").strip()
    if not 1 <= len(clean) <= 120 or not clean.isprintable():
        raise StartupError(400, {"ok": False, "code": "BAD_NAME",
                                 "detail": "A name is 1 to 120 characters, with no line breaks."})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "name", {"name": clean}, event="rename",
        too_old="This controller's Studio Hub cannot rename its Macs yet. Update it first "
                "(Fleet > Release), or rename the Mac on the Mac itself (Connections).",
        refused="The controller refused this gateway's credential for renaming.",
        audit_fields={"name": clean}, client=client,
    )


def set_node_pause(controller_id: str, machine_id: str, paused: object, *,
                   client: httpx.Client | None = None) -> dict[str, Any]:
    """Pause or resume one Mac by hand (owner request 2026-10-03), through its controller.

    The controller marks the Mac paused in its roster (kept across restarts) and its broker sends it
    no new work; jobs already running there finish, and the Mac rejoins when resumed. Only the
    controller's own Hub can do this: the gateway never routes to a single node. A controller on a
    Hub without the route answers `HUB_TOO_OLD`.
    """
    if not isinstance(paused, bool):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED", "detail": "paused must be true or false"})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "pause", {"paused": paused}, event="pause",
        too_old="This controller's Studio Hub cannot pause its Macs yet. It needs Studio Hub 0.6.135: "
                "update it first (Fleet > Release).",
        refused="The controller refused this gateway's credential for pausing a Mac.",
        audit_fields={"paused": paused}, client=client,
    )


def _node_post(controller_id: str, machine_id: str, route: str, body: dict[str, Any], *, event: str,
               too_old: str, refused: str, audit_fields: dict[str, Any],
               client: httpx.Client | None) -> dict[str, Any]:
    """POST one node action to the controller that owns the Mac, with the credential every fleet call uses."""
    base, token = _target(controller_id)
    audit(f"{event}_requested", controller=controller_id, machine=machine_id, **audit_fields)
    owned = client is None
    client = client or httpx.Client(timeout=_TIMEOUT)
    try:
        response = client.post(f"{base}/api/hub/fleet/nodes/{machine_id}/{route}",
                               headers={"X-Hub-Token": token} if token else {}, json=body)
    except httpx.HTTPError as exc:
        raise StartupError(502, {"ok": False, "code": "CONTROLLER_UNREACHABLE",
                                 "detail": "The controller did not answer: "
                                           f"{type(exc).__name__}. Check it is awake and on the network."}) from exc
    finally:
        if owned:
            client.close()
    try:
        answered = response.json()
    except ValueError:
        answered = {}
    if not isinstance(answered, dict):
        answered = {}
    if response.status_code in (404, 405) and "code" not in answered:
        answered = {"ok": False, "code": "HUB_TOO_OLD", "detail": too_old}
    elif response.status_code in (401, 403) and "code" not in answered:
        answered = {"ok": False, "code": "CONTROLLER_REFUSED", "detail": refused}
    answered.setdefault("ok", response.is_success)
    answered["machine"] = machine_id
    audit(f"{event}_result", controller=controller_id, machine=machine_id, status=response.status_code,
          code=answered.get("code"), ok=answered.get("ok"))
    if not response.is_success:
        raise StartupError(response.status_code if response.status_code < 500 else 502, answered)
    fleet.invalidate()   # the next Fleet read shows the new name, not a cached old one
    return answered


def enable_autostart_where_off(*, dry_run: bool = False,
                               machines: list[dict[str, Any]] | None = None,
                               ask=request) -> dict[str, Any]:
    """Turn "Start services on startup" on for every Mac that reported it off.

    Only Macs whose Hub said `false` are touched (never `unknown`: an older Hub cannot take the
    request, and guessing would turn a settings change into a pile of refusals). A Mac that is
    currently unreachable is listed, not tried - its controller cannot pass anything on. The result
    names every Mac and what happened, so the one button can be audited afterwards.
    """
    rows = machines if machines is not None else (fleet.snapshot(force=True).get("machines") or [])
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for machine in rows:
        fact = machine.get("startup")
        if not fact or fact.get("start_services_on_launch") is not False:
            continue
        reachable = machine.get("reachable") is True and not fact.get("stale")
        entry = {"location_id": machine.get("location_id"), "machine": machine.get("machine"),
                 "label": machine.get("label") or machine.get("machine")}
        if not reachable:
            skipped.append({**entry, "reason": _unreachable_reason(machine)})
        else:
            targets.append(entry)
    audit("bulk_enable_autostart", dry_run=dry_run, would_ask=len(targets), skipped=len(skipped))
    if dry_run:
        return {"dry_run": True, "asked": 0, "would_ask": targets, "skipped": skipped,
                "applied": [], "failed": []}

    def one(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        try:
            answer = ask(str(entry["location_id"]), str(entry["machine"]),
                         "set_start_services_on_launch", True)
            return entry, answer, None
        except StartupError as error:
            return entry, None, error.body

    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=_BULK_WORKERS) as pool:
        for entry, answer, error in pool.map(one, targets):
            if answer is not None and answer.get("state") in ("applied", "queued") and answer.get("ok", True):
                applied.append({**entry, "state": answer.get("state")})
            else:
                body = error or answer or {}
                failed.append({**entry, "code": body.get("code"), "detail": body.get("detail")})
    audit("bulk_enable_autostart_done", asked=len(targets), applied=len(applied), failed=len(failed))
    return {"dry_run": False, "asked": len(targets), "applied": applied, "failed": failed,
            "skipped": skipped}


def _unreachable_reason(machine: dict[str, Any]) -> str:
    reason = fleet._node_reach_reason(machine)
    if reason == "no_hub":
        return ("its Studio Hub is not running (services are off); start the services on that Mac once, "
                "then it keeps them on")
    return "that Mac is not answering (asleep, off or off the network); this applies when it is back"
