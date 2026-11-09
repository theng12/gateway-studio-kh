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


#: The audit events that are **an actual stop of a Hub batch**, and nothing else. A refusal is not
#: listed: the batch is still running, so there is nothing yet to see afterwards.
STOP_EVENTS = ("superseded_stop", "orphan_stop", "deadline_stop")

#: The switch each event came from when the row does not say. Rows written before 0.2.212 carry no
#: `switch` field, and each event has always had exactly one writer, so this is a fact about the
#: code, not a guess about the row.
_SWITCH_BY_EVENT = {"superseded_stop": "superseded_stop", "orphan_stop": "auto_stop"}

#: The Fleet page's list: the last seven days, at most 200 stops. Bounded because the audit keeps
#: 500 lines, so this reads a file that is already small.
STOP_DAYS = 7.0
STOP_LIMIT = 200
_HUB_COUNTS = ("queued_cancelled", "running_signalled", "running_pending")


def recent_stops(days: float = STOP_DAYS, limit: int = STOP_LIMIT, *,
                 now: float | None = None) -> dict[str, Any]:
    """Every Hub batch this Gateway stopped without a person, newest first, for the Fleet page.

    An automatic stop nobody can see afterwards is worse than none, so this is the read behind
    "Recent automatic stops". It lists `superseded_stop` and `orphan_stop` rows — the copy this
    Gateway replaced after a site went silent, the orphan the owner's own switch covered — and the
    **manual** orphan Stop, which the same route writes with `requested_by: "admin"` and
    `switch: "manual"`, because a stop a person pressed belongs in the same list and must not read
    as automatic.

    **An unreadable log is `available: false` with `stops: None`, never an empty list.** "Nothing
    was stopped" and "I could not read the log" must not be the same answer, and an empty list is
    a claim this read cannot make when the file cannot be opened.
    """
    days = min(max(float(days or 0.0), 0.25), 30.0)
    limit = max(1, min(int(limit or 0), _AUDIT_KEEP))
    moment = time.time() if now is None else now
    try:
        lines = audit_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # No audit has ever been written: nothing has been stopped, and that is a real answer.
        return {"available": True, "stops": [], "count": 0, "days": days, "limit": limit,
                "note": "No stop has been recorded yet."}
    except OSError as exc:
        return {"available": False, "stops": None, "count": None, "days": days, "limit": limit,
                "note": f"not available: the audit log could not be read ({type(exc).__name__})."}
    cutoff = moment - days * 86400.0
    stops: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or str(row.get("event") or "") not in STOP_EVENTS:
            continue
        at = row.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or float(at) < cutoff:
            continue
        stops.append(_stop_row(row))
        if len(stops) >= limit:
            break
    return {"available": True, "stops": stops, "count": len(stops), "days": days,
            "limit": limit, "note": None}


def _stop_row(row: dict[str, Any]) -> dict[str, Any]:
    """One audit line as the page draws it. A field the row does not carry stays `None`."""
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    # Every Studio Hub adapter records the Hub's own body under `answer` (`_recorded_call`), and
    # these rows are all Studio Hub stops, so that is the one place the counts live.
    answer = result.get("answer") if isinstance(result.get("answer"), dict) else {}
    site = str(row.get("controller") or "").strip()
    event = str(row.get("event") or "")
    return {
        "at": float(row.get("at") or 0.0),
        "site": site or None,
        "site_label": _site_label(site) or None,
        "batch": _stop_text(row.get("batch")),
        "job": _stop_text(row.get("job")),
        "modality": _stop_text(row.get("modality")),
        "reason": _stop_text(row.get("reason")),
        "switch": _stop_text(row.get("switch")) or _SWITCH_BY_EVENT.get(event),
        "requested_by": _stop_text(row.get("requested_by")),
        "ok": result.get("ok") if isinstance(result.get("ok"), bool) else None,
        "error": _stop_text(result.get("error")) or _stop_text(answer.get("detail")),
        "hub": {key: _stop_count(answer.get(key)) for key in _HUB_COUNTS},
    }


def _site_label(controller: str) -> str:
    """The owner's name for the location the row names; its id when the roster no longer has it.

    The audit records the id whatever it was called: the superseded cleanup writes the provider id
    (`studio-hub-<controller>`) and the orphan sweep the controller id. A row for a location since
    removed keeps its id, which is still the truth it was written with — never an invented name.
    """
    if not controller:
        return ""
    label = fleet.label_for_provider(controller)
    if label:
        return label
    if controller == fleet.LOCAL_ID:
        return fleet.local_label()
    for entry in fleet.roster():
        if str(entry.get("id") or "") == controller:
            return str(entry.get("label") or "").strip() or controller
    return controller


def _stop_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stop_count(value: object) -> int | None:
    """A count the Hub sent, or `None`: a missing figure never becomes a zero nobody measured."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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


def set_node_image_model(controller_id: str, machine_id: str, model: str | None, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """Tell a Mac's controller which image-pool model that Mac serves (`None` clears it), owner 2026-10-24.

    Sent by `image_assignment.sync`; the controller keeps it in its machine flags, refuses pool work
    for other models on that Mac, and reports it on the Mac's row.
    """
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED", "detail": "model must be a model id or null"})
    return _node_post(
        controller_id, machine_id, "assigned-image-model", {"model": model}, event="image_model",
        too_old="This controller's Studio Hub cannot take a per-Mac image model yet. Update Studio Hub first "
                "(Fleet > Release).",
        refused="The controller refused this gateway's credential for setting a Mac's image model.",
        audit_fields={"model": model}, client=client,
    )


#: What a workstation-mode change may carry; the Hub checks each value's range (`BAD_SETTING`).
WORKSTATION_SETTINGS = ("enabled", "idle_only", "idle_minutes", "step_aside", "resume_after_minutes",
                        "start_free_gb", "quiet_hours", "hide_menu_bar_icon")

# The wire vocabulary is Studio Hub's registered local function vocabulary. Gateway's public `tts`
# lane maps to `voice` before placement; the per-Mac policy itself always uses these Hub tokens.
# `render` is final video assembly: it has no worker and no registry row, but since owner
# 2026-10-28 it is an ordinary entry in the same saved order beside the rest.
WORK_PRIORITY_FUNCTIONS = frozenset({"voice", "transcription", "chat", "image", "music", "video",
                                     "render"})


def get_node_work_policy(controller_id: str, machine_id: str, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """Read one Mac's enabled functions and ranked work preference through its controller."""
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    answered = _node_post(
        controller_id, machine_id, "work-policy", None, method="GET", event="work_priority",
        too_old=("This controller's Studio Hub cannot read this Mac's work priorities yet. "
                 "Update Studio Hub first (Fleet > Release); the saved order is read-only until then."),
        refused="The controller refused this gateway's credential for reading work priorities.",
        audit_fields={}, client=client,
    )
    policy = answered.get("work_policy")
    return policy if isinstance(policy, dict) else answered


def set_node_work_policy(controller_id: str, machine_id: str, priority_order: object, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """Replace one Mac's ranked functions; an empty list makes every enabled function equal."""
    valid = (isinstance(priority_order, list)
             and all(isinstance(item, str) and item in WORK_PRIORITY_FUNCTIONS for item in priority_order)
             and len(priority_order) == len(set(priority_order)))
    if not valid:
        raise StartupError(400, {
            "ok": False, "code": "BAD_PRIORITY_ORDER",
            "detail": "priority_order must be a list of unique local function ids.",
        })
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    answered = _node_post(
        controller_id, machine_id, "work-policy", {"priority_order": priority_order},
        event="work_priority",
        too_old=("This controller's Studio Hub cannot change this Mac's work priorities yet. "
                 "Update Studio Hub first (Fleet > Release); the saved order was not changed."),
        refused="The controller refused this gateway's credential for changing work priorities.",
        audit_fields={"priority_order": priority_order}, client=client,
    )
    policy = answered.get("work_policy")
    return policy if isinstance(policy, dict) and answered.get("queued") is not True else answered


MEMORY_ADMISSION_TOO_OLD = ("This controller's Studio Hub cannot read or change a Mac's memory limits yet. "
                            "It needs Studio Hub 0.6.226: update it first (Fleet > Release).")


def get_node_memory_admission(controller_id: str, machine_id: str, model: str, mode: str | None = None, *,
                              client: httpx.Client | None = None) -> dict[str, Any]:
    """Read one model's effective RAM admission on one Mac, with the revision a change must name."""
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    params = {"model": model, **({"mode": mode} if mode else {})}
    return _node_post(
        controller_id, machine_id, "memory/admission", None, method="GET", event="memory_admission",
        too_old=MEMORY_ADMISSION_TOO_OLD,
        refused="The controller refused this gateway's credential for reading memory limits.",
        audit_fields={}, client=client, send={"params": params},
    )


def set_node_memory_admission(controller_id: str, machine_id: str, body: object, *,
                              client: httpx.Client | None = None) -> dict[str, Any]:
    """Change or reset one model's RAM limits on one Mac, once, exactly as sent.

    The Hub validates every field and owns the revision check: a 409 REVISION_CONFLICT (with the
    current revision) and a 202 `outcome: pending` relay are passed through, never retried here.
    """
    if not isinstance(body, dict):
        raise StartupError(400, {"ok": False, "code": "BAD_VALUE", "detail": "send a JSON object"})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "memory/admission", body, method="PUT", event="memory_admission",
        too_old=MEMORY_ADMISSION_TOO_OLD,
        refused="The controller refused this gateway's credential for changing memory limits.",
        audit_fields={"model": body.get("model"), "mode": body.get("mode"),
                      "expected_revision": body.get("expected_revision")}, client=client,
    )


def set_node_preferred_work(controller_id: str, machine_id: str, preferred_work: object, *,
                            client: httpx.Client | None = None) -> dict[str, Any]:
    """Set the preference through the controller; the Mac validates its own choices."""
    if not isinstance(preferred_work, dict):
        raise StartupError(400, {"ok": False, "code": "BAD_PREFERRED_WORK",
                                 "detail": "preferred_work must be an object."})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    current = get_node_work_policy(controller_id, machine_id, client=client)
    if current.get("queued") is True or not isinstance(current.get("preferred_choices"), list):
        raise StartupError(503, {"ok": False, "code": "PREFERRED_WORK_UNKNOWN",
                                 "detail": "This Mac did not confirm its preferred work. Check that it is reachable and its Studio Hub is up to date."})
    answered = _node_post(
        controller_id, machine_id, "work-policy", {"preferred_work": preferred_work},
        event="preferred_work",
        too_old="This controller's Studio Hub cannot change preferred work yet. Update Studio Hub first (Fleet > Release).",
        refused="The controller refused this gateway's credential for changing preferred work.",
        audit_fields={"preferred_work": preferred_work}, client=client,
    )
    policy = answered.get("work_policy")
    return policy if isinstance(policy, dict) else answered


def set_node_function_enabled(controller_id: str, machine_id: str, function_enabled: object, *,
                              client: httpx.Client | None = None) -> dict[str, Any]:
    """Switch one or more functions on or off in one Mac's work order (owner, 2026-10-28)."""
    allowed = WORK_PRIORITY_FUNCTIONS | {"vision", "embeddings"}
    valid = (isinstance(function_enabled, dict) and bool(function_enabled)
             and all(isinstance(kind, str) and kind in allowed and type(on) is bool
                     for kind, on in function_enabled.items()))
    if not valid:
        raise StartupError(400, {
            "ok": False, "code": "BAD_FUNCTION_ENABLED",
            "detail": "function_enabled must map rankable function ids to true or false.",
        })
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    answered = _node_post(
        controller_id, machine_id, "work-policy", {"function_enabled": dict(function_enabled)},
        event="work_priority",
        too_old=("This controller's Studio Hub cannot switch individual functions in this Mac's "
                 "work order yet. Update Studio Hub first (Fleet > Release)."),
        refused="The controller refused this gateway's credential for changing work priorities.",
        audit_fields={"function_enabled": dict(function_enabled)}, client=client,
    )
    policy = answered.get("work_policy")
    return policy if isinstance(policy, dict) else answered


def set_node_workstation(controller_id: str, machine_id: str, settings: object, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """Change one Mac's Lite Mode (internally "workstation"; owner request 2026-10-03), through its controller.

    Lite Mode lets a shop's own Mac take fleet work only when nobody is using it, take no new work
    while someone does (its current job finishes), start a job only with enough free memory (`start_free_gb`,
    `null` = each model's own floor) and rest in quiet hours. The body is any subset
    of `WORKSTATION_SETTINGS`; the Hub validates the values and answers `BAD_SETTING` with a sentence,
    which reaches the caller unchanged, as do `NODE_UNKNOWN` and `NODE_REFUSED` (that Mac's own Hub is too
    old). A controller without the route answers `HUB_TOO_OLD`. A Mac out of reach (`NODE_UNREACHABLE`, or its
    controller not answering) keeps the change as pending and the answer is `{"ok": true, "pending": true}`;
    the fleet refresher sends it when the Mac is back (`apply_pending_lite_mode`).
    """
    if not isinstance(settings, dict) or not settings:
        raise StartupError(400, {"ok": False, "code": "BAD_SETTING",
                                 "detail": "Send at least one Lite Mode setting to change."})
    unknown = sorted(set(settings) - set(WORKSTATION_SETTINGS))
    if unknown:
        raise StartupError(400, {"ok": False, "code": "BAD_SETTING",
                                 "detail": f"Not a Lite Mode setting: {', '.join(map(str, unknown))}."})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    key = f"{controller_id}/{machine_id}"
    # A change still waiting for this Mac goes with this one (this one's fields win), so the refresher
    # never sends an older value after a newer one landed.
    body = {**(pending_lite_mode().get(key, {}).get("settings") or {}), **settings}
    try:
        answered = _send_workstation(controller_id, machine_id, body, event="workstation", client=client)
    except StartupError as error:
        if error.body.get("code") not in _REACH_CODES:
            raise
        # Owner request 2026-10-04: an offline Mac keeps the change and gets it when it is back.
        with _pending_lock:
            waiting = pending_lite_mode()
            old = waiting.get(key) or {}
            now = round(time.time(), 3)
            waiting[key] = {"controller": controller_id, "machine": machine_id,
                            "settings": {**(old.get("settings") or {}), **settings},
                            "since": old.get("since") or now, "at": now}
            _write_pending(waiting)
        audit("workstation_pending_saved", controller=controller_id, machine=machine_id,
              settings=waiting[key]["settings"], reach=error.body.get("code"))
        fleet.invalidate()
        return {"ok": True, "pending": True, "machine": machine_id, "settings": waiting[key]["settings"],
                "detail": PENDING_SENTENCE}
    _drop_pending(key)
    return answered


def _send_workstation(controller_id: str, machine_id: str, body: dict[str, Any], *, event: str,
                      client: httpx.Client | None) -> dict[str, Any]:
    return _node_post(
        controller_id, machine_id, "workstation", body, event=event,
        too_old="This controller's Studio Hub has no Lite Mode yet. It needs a newer Studio Hub: "
                "update it first (Fleet > Release).",
        refused="The controller refused this gateway's credential for changing Lite Mode.",
        audit_fields={"settings": body}, client=client,
    )


#: The setting this relay carries, as Studio Hub names it (`render_authority.accept_unqualified`).
RENDER_AUTHORITY_TOO_OLD = ("This controller's Studio Hub cannot accept remote final rendering yet. "
                            "Update it first (Fleet > Release); no render is sent to it until then.")


def set_node_render_authority(controller_id: str, machine_id: str, accept_unqualified: object, *,
                              client: httpx.Client | None = None) -> dict[str, Any]:
    """Tell one Mac's Studio Hub to accept (or stop accepting) final renders without a qualification record.

    Owner decision 2026-10-23, "Remote final rendering". The same controller relay as every other action
    here: `POST {controller}/api/hub/fleet/nodes/{machine}/render-authority`, body
    `{"accept_unqualified": bool, "requested_by": "gateway-admin"}`, the controller relays it to the Mac
    and the Mac's own Hub validates and persists it. The Gateway never writes a file on the Mac.
    A controller without the route answers `HUB_TOO_OLD`; a Mac whose own Hub is older answers
    `RENDER_AUTHORITY_UNSUPPORTED` (409); a Mac the controller can only reach at its next check-in answers
    `202 queued`, which is success. None is retried: the Gateway keeps no pending copy, because a switch that is
    on here and not confirmed there simply leaves the Mac unused (`render_optin.accepts_unqualified`).
    """
    if not isinstance(accept_unqualified, bool):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED",
                                 "detail": "accept_unqualified must be true or false"})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "render-authority",
        {"accept_unqualified": accept_unqualified, "requested_by": "gateway-admin"},
        event="render_authority", too_old=RENDER_AUTHORITY_TOO_OLD,
        refused="The controller refused this gateway's credential for changing remote rendering.",
        audit_fields={"accept_unqualified": accept_unqualified}, client=client,
    )


#: The setting this relay carries, as Studio Hub names it (`render_while_paused`).
RENDER_WHILE_PAUSED_TOO_OLD = (
    "This controller's Studio Hub cannot exempt a paused Mac's final rendering yet. "
    "Update it first (Fleet > Release); the Mac stays out of rendering until then.")


def set_node_render_while_paused(controller_id: str, machine_id: str, render_while_paused: object, *,
                                 client: httpx.Client | None = None) -> dict[str, Any]:
    """Tell one Mac's Studio Hub whether its own pause exempts final rendering (owner, 2026-10-28).

    The same controller relay as the remote-rendering switch: the body is closed, the Mac records
    who asked, and nothing here writes a file on the Mac. Off by default, so a Mac whose owner has
    not switched it on behaves exactly as it did.
    """
    if not isinstance(render_while_paused, bool):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED",
                                 "detail": "render_while_paused must be true or false"})
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "render-while-paused",
        {"render_while_paused": render_while_paused, "requested_by": "gateway-admin"},
        event="render_while_paused", too_old=RENDER_WHILE_PAUSED_TOO_OLD,
        refused="The controller refused this gateway's credential for changing remote rendering.",
        audit_fields={"render_while_paused": render_while_paused}, client=client,
    )


# --- Lite Mode changes waiting for an offline Mac (owner request 2026-10-04) ---------------------------
#
# One entry per Mac (`controller/machine`) in `lite_mode_pending.json`: `settings` (merged field by field),
# `since` (first saved) and `at` (last change); or, once its Hub refused the values, `refused` (the Hub's
# sentence) until the next change for that Mac. Studio Hub publishes no time for a change made on the Mac
# itself, so a waiting change always applies (the owner's latest Gateway action).

#: The relay could not reach the Mac (its controller did not answer, or the controller could not reach it).
_REACH_CODES = frozenset({"CONTROLLER_UNREACHABLE", "NODE_UNREACHABLE"})
PENDING_SENTENCE = "This Mac is offline. The change is saved and will be applied when it comes back."
_pending_lock = threading.Lock()


def pending_path() -> Path:
    return config.DATA_DIR / "lite_mode_pending.json"


def pending_lite_mode() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, dict)} if isinstance(value, dict) else {}


def _write_pending(value: dict[str, dict[str, Any]]) -> None:
    path = pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
    os.replace(scratch, path)


def _drop_pending(key: str, *, unless_changed_since: float | None = None) -> None:
    with _pending_lock:
        waiting = pending_lite_mode()
        entry = waiting.get(key)
        if entry is None or (unless_changed_since is not None and entry.get("at") != unless_changed_since):
            return
        del waiting[key]
        _write_pending(waiting)


def apply_pending_lite_mode(machines: list[dict[str, Any]], *, client: httpx.Client | None = None) -> None:
    """Send each waiting change to its Mac once the Fleet sees it reachable (called by the fleet refresher).

    Sent: cleared and audited. Still out of reach: kept. Refused (`BAD_SETTING`, or any other refusal that
    a retry would only repeat): cleared, and the Hub's sentence kept as a Fleet issue for that Mac.
    """
    waiting = {k: v for k, v in pending_lite_mode().items() if v.get("settings")}
    if not waiting:
        return
    for row in machines:
        key = f"{row.get('location_id')}/{row.get('machine')}"
        entry = waiting.get(key)
        if not entry or not row.get("location_reachable") or row.get("reachable") is not True \
                or str(row.get("state") or "").lower() == "offline":
            continue
        controller, machine = str(row["location_id"]), str(row["machine"])
        # ponytail: one Mac at a time on the refresher thread; a thread per Mac if many wait at once.
        try:
            _send_workstation(controller, machine, entry["settings"], event="workstation_pending", client=client)
        except StartupError as error:
            if error.body.get("code") in _REACH_CODES:
                continue
            detail = str(error.body.get("detail") or error.body.get("code") or "refused")
            with _pending_lock:
                now_waiting = pending_lite_mode()
                if (now_waiting.get(key) or {}).get("at") == entry.get("at"):
                    now_waiting[key] = {"controller": controller, "machine": machine, "refused": detail,
                                        "refused_code": error.body.get("code"), "at": round(time.time(), 3)}
                    _write_pending(now_waiting)
            audit("workstation_pending_refused", controller=controller, machine=machine,
                  settings=entry["settings"], code=error.body.get("code"), detail=detail)
            fleet.invalidate()
            continue
        _drop_pending(key, unless_changed_since=entry.get("at"))
        audit("workstation_pending_applied", controller=controller, machine=machine, settings=entry["settings"])


def stamp_lite_mode_pending(machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put each waiting change on its Mac's row (`lite_mode_pending`) and return the refusals as Fleet issues."""
    waiting = pending_lite_mode()
    issues: list[dict[str, Any]] = []
    for row in machines:
        entry = waiting.get(f"{row.get('location_id')}/{row.get('machine')}")
        row["lite_mode_pending"] = None
        if not entry:
            continue
        label = row.get("label") or row.get("machine")
        if entry.get("settings"):
            row["lite_mode_pending"] = {"settings": entry["settings"], "at": entry.get("at"),
                                        "offline_since": row.get("node_last_seen_at") or entry.get("since")}
        elif entry.get("refused"):
            issues.append(fleet._issue(
                "warning", "lite_mode_refused", row.get("location_id"), row.get("location"), str(label),
                f"{label} came back, but its Studio Hub refused the Lite Mode change that was waiting for it: "
                f"{entry['refused']}", "Fleet → Machines → … → Lite Mode…"))
    return issues


def _node_post(controller_id: str, machine_id: str, route: str, body: dict[str, Any] | None, *, event: str,
               too_old: str, refused: str, audit_fields: dict[str, Any],
               client: httpx.Client | None, method: str = "POST", send: dict[str, Any] | None = None,
               timeout: httpx.Timeout = _TIMEOUT) -> dict[str, Any]:
    """POST one node action to the controller that owns the Mac, with the credential every fleet call uses.

    ``method="GET"`` is a status read through the same path: not audited, and the Fleet is not re-read.
    ``send`` replaces the JSON ``body`` with other httpx request arguments (a multipart upload).
    """
    base, token = _target(controller_id)
    action = method != "GET"
    if action:
        audit(f"{event}_requested", controller=controller_id, machine=machine_id, **audit_fields)
    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        path = (f"/api/hub/{route}"
                if machine_id == fleet.LOCAL_ID and route in {
                    "render-authority", "render-while-paused"}
                else f"/api/hub/fleet/nodes/{machine_id}/{route}")
        response = client.request(method, base + path,
                                  headers={"X-Hub-Token": token} if token else {},
                                  **(send if send is not None else {"json": body}))
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
    # A Hub whose Fleet allowlist does not name the route refuses it before routing (403, "the fleet
    # credential may send work ..."): the token was accepted, the Hub is simply older than the route.
    older = (response.status_code == 403
             and "the fleet credential may send work" in str(answered.get("detail") or ""))
    if response.status_code == 404 and answered.get("detail") == "unknown model" and "code" not in answered:
        # The Hub knows the route but not the model (memory admission): that is not an old Hub.
        answered = {"ok": False, "code": "UNKNOWN_MODEL", "detail": "this Mac does not know that model"}
    elif (response.status_code in (404, 405) or older) and "code" not in answered:
        answered = {"ok": False, "code": "HUB_TOO_OLD", "detail": too_old}
    elif response.status_code in (401, 403) and "code" not in answered:
        answered = {"ok": False, "code": "CONTROLLER_REFUSED", "detail": refused}
    answered.setdefault("ok", response.is_success)
    answered["machine"] = machine_id
    # **`queued` is the controller having taken the request, not having applied it yet.** Hub
    # 0.6.193 holds a request for a Mac it cannot dial — a Shops Mac, reached only through its own
    # check-in — and answers `202` with `queued: true` and the command's id. That is a success for
    # the caller: the controller owns delivery from that moment, and keeping a copy here would send
    # the same change twice. It is named in the answer and in the audit line so a reader can tell
    # "the Mac has it" from "the controller has it".
    if answered.get("queued") is True:
        answered.setdefault("detail", "held for the Mac: its controller cannot dial it and will "
                                      "deliver it on the Mac's next check-in")
    if action:
        audit(f"{event}_result", controller=controller_id, machine=machine_id, status=response.status_code,
              code=answered.get("code"), ok=answered.get("ok"), queued=answered.get("queued"))
    if not response.is_success:
        raise StartupError(response.status_code if response.status_code < 500 else 502, answered)
    if action:
        fleet.invalidate()   # the next Fleet read shows the new name, not a cached old one
    return answered


MODEL_INSTALL_TOO_OLD = ("This controller's Studio Hub cannot install models from the Gateway yet. It needs "
                         "Studio Hub 0.6.143: update it first (Fleet > Release).")


MODEL_SHARE_TOO_OLD = ("This controller's Studio Hub cannot share models between Macs yet. It needs Studio Hub "
                       "0.6.146: update it before using this Mac as a LAN source.")


def install_model(controller_id: str, machine_id: str, package_id: str, *, source: dict[str, Any] | None = None,
                  peer_only: bool = False, client: httpx.Client | None = None) -> dict[str, Any]:
    """Start a model download on one Mac (owner decision 2026-10-04), through its controller.

    The Mac's Hub refuses an unknown package, a shelved model, a full disk and a model it has too
    little memory to run, each in its own sentence, which is passed through. Answers
    `{"ok": true, "node", "run": {"id", "state", "total_bytes", ...}}`; follow it with
    `model_install_status`. `source` is a LAN share from `share_model`; `peer_only=True` requires Studio Hub
    0.6.174 and forbids internet fallback. The share's token is never audited.
    """
    if not _MACHINE_ID.fullmatch(machine_id or "") or not _MACHINE_ID.fullmatch(package_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine or model id is not valid"})
    if not isinstance(peer_only, bool):
        raise StartupError(400, {"ok": False, "code": "VALUE_REQUIRED",
                                 "detail": "peer_only must be true or false"})
    if peer_only and source is None:
        raise StartupError(400, {"ok": False, "code": "SOURCE_REQUIRED",
                                 "detail": "A LAN-only model copy requires a source share."})
    payload: dict[str, Any] = {"source": source} if source else {}
    if peer_only:
        payload["peer_only"] = True
    return _node_post(
        controller_id, machine_id, f"model-packages/{package_id}/download", payload,
        event="model_install", too_old=MODEL_INSTALL_TOO_OLD,
        refused="The controller refused this gateway's credential for installing models.",
        audit_fields={"package": package_id, "source": "lan" if source else "internet"}, client=client,
    )


MODEL_PRESETS_TOO_OLD = ("This controller's Studio Hub cannot show or set a model preset yet. It needs "
                          "Studio Hub 0.6.205: update it first (Fleet > Release).")

MODEL_REMOVE_TOO_OLD = ("This controller's Studio Hub cannot remove a model from a Mac yet. It needs Studio Hub "
                        "0.6.205: update it first (Fleet > Release).")


def model_presets(controller_id: str, machine_id: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """One Mac's install screen as its Hub answers it: memory, disk, four presets, every model.

    A read through the same node path as everything else (`GET .../fleet/nodes/{id}/model-presets`).
    The answer is the Hub's own projection - a preset's count is the Hub's verified receipts and a
    model's floor is its manifest's - so the Gateway shows what the Mac computed rather than a
    second opinion.
    """
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(
        controller_id, machine_id, "model-presets", None, event="model_presets",
        too_old=MODEL_PRESETS_TOO_OLD,
        refused="The controller refused this gateway's credential for reading model presets.",
        audit_fields={}, client=client, method="GET",
    )


def set_model_preset(controller_id: str, machine_id: str, preset_id: str, *, requested_by: str,
                     client: httpx.Client | None = None) -> dict[str, Any]:
    """Set one Mac's preset (owner decision 2026-10-28): one of the four the Shop's Hub ships.

    `requested_by` is required and recorded on that Mac, exactly as the unqualified-render grant is:
    a preset decides what a Mac installs and what the one-time cleanup moves, so "who asked" is a
    fact worth keeping. The Hub refuses an unknown id by name rather than choosing one.
    """
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    who = str(requested_by or "").strip()
    if not who:
        raise StartupError(400, {"ok": False, "code": "APPROVER_REQUIRED",
                                 "detail": "Name who set this preset."})
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise StartupError(400, {"ok": False, "code": "BAD_PRESET", "detail": "A preset id is required."})
    return _node_post(
        controller_id, machine_id, "model-preset", {"preset_id": preset_id.strip(), "requested_by": who},
        event="model_preset", too_old=MODEL_PRESETS_TOO_OLD,
        refused="The controller refused this gateway's credential for setting a model preset.",
        audit_fields={"preset": preset_id.strip(), "requested_by": who}, client=client,
    )


def remove_model(controller_id: str, machine_id: str, package_id: str, *,
                 client: httpx.Client | None = None) -> dict[str, Any]:
    """Remove one model from one Mac: the Hub moves it into that library's trash, never deletes.

    Refused on that Mac in exactly the cases its own Models screen refuses (a running job, a paused
    model, a package another one needs), and the Mac's sentence passes through unchanged.
    """
    if not _MACHINE_ID.fullmatch(machine_id or "") or not _MACHINE_ID.fullmatch(package_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine or model id is not valid"})
    return _node_post(
        controller_id, machine_id, f"model-packages/{package_id}", None, event="model_remove",
        too_old=MODEL_REMOVE_TOO_OLD,
        refused="The controller refused this gateway's credential for removing models.",
        audit_fields={"package": package_id}, client=client, method="DELETE",
    )


def share_model(controller_id: str, machine_id: str, package_id: str, *,
                client: httpx.Client | None = None) -> dict[str, Any]:
    """Ask a Mac that holds a model to lend it to another Mac on its network (Studio Hub 0.6.146).

    Answers `{"ok": true, "share": {"url_base", "token", "expires_at", "files", "revision"}}`: a read-only,
    time-limited share of that one package, handed to the target's `install_model(source=...)`. The Mac
    refuses a package it does not hold verified, and a third transfer (429).
    """
    if not _MACHINE_ID.fullmatch(machine_id or "") or not _MACHINE_ID.fullmatch(package_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine or model id is not valid"})
    return _node_post(
        controller_id, machine_id, f"model-packages/{package_id}/share", {}, event="model_share",
        too_old=MODEL_SHARE_TOO_OLD,
        refused="The controller refused this gateway's credential for sharing models.",
        audit_fields={"package": package_id}, client=client,
    )


def model_install_status(controller_id: str, machine_id: str, run_id: str, *,
                         client: httpx.Client | None = None) -> dict[str, Any]:
    """How a download started by `install_model` is going: `{"ok", "node", "run": {"state", "bytes_done", ...}}`."""
    if not _MACHINE_ID.fullmatch(machine_id or "") or not _MACHINE_ID.fullmatch(run_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine or download id is not valid"})
    return _node_post(
        controller_id, machine_id, f"model-downloads/{run_id}", None, event="model_install",
        too_old=MODEL_INSTALL_TOO_OLD,
        refused="The controller refused this gateway's credential for installing models.",
        audit_fields={}, client=client, method="GET",
    )


NODE_TRANSCRIBE_TOO_OLD = ("This controller's Studio Hub cannot run a test transcription on one Mac yet. It needs "
                           "Studio Hub 0.6.147: update it first (Fleet > Release).")

#: The Hub relay waits 960 s for the Mac's answer (its own 900 s Subtitle deadline plus the hop); this waits a little longer.
_TRANSCRIBE_TIMEOUT = httpx.Timeout(1000.0, connect=6.0)


def transcribe_on_machine(controller_id: str, machine_id: str, audio: bytes, filename: str, *, model: str,
                          language: str = "", word_timestamps: bool = True,
                          client: httpx.Client | None = None) -> dict[str, Any]:
    """Run one test transcription on one chosen Mac, for model qualification (Studio Hub 0.6.147).

    The Mac's Subtitle worker answers synchronously and in full: `{"text", "language", "duration", "model",
    "segments": [{"start", "end", "text", "words": [{"word", "start", "end"}]}], "srt", "vtt",
    "elapsed_seconds", "job_id", "package_id", "model_revision", "node", "machine"}` (`words` only with
    `word_timestamps`). No job record and no catalog change: the model only has to be installed on that Mac.
    """
    if not _MACHINE_ID.fullmatch(machine_id or "") or not model or len(model) > 200:
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine or model id is not valid"})
    return _node_post(
        controller_id, machine_id, "transcribe", None, event="node_transcribe", too_old=NODE_TRANSCRIBE_TOO_OLD,
        refused="The controller refused this gateway's credential for test transcriptions.",
        audit_fields={"model": model, "language": language or None, "bytes": len(audio)}, client=client,
        timeout=_TRANSCRIBE_TIMEOUT,
        send={"files": {"file": (filename, audio)},
              "data": {"model": model, "language": language, "word_timestamps": str(bool(word_timestamps)).lower()}},
    )


# A Mac's Trash Folder (owner decision 4g, 2026-10-16): Studio Hub never hard-deletes what it retires, so the
# owner lists, moves to macOS Trash, or permanently deletes it from here instead of visiting each Mac.
TRASH_HUB = "0.6.182"
TRASH_TOO_OLD = f"Needs Studio Hub {TRASH_HUB} to manage its Trash Folder from the Gateway."
#: A large Trash Folder takes a while to measure and delete; the Hub's relay waits 900 s.
_TRASH_TIMEOUT = httpx.Timeout(960.0, connect=6.0)
#: A Trash Folder reading older than this is labelled `stale` and left out of a claimed fresh total.
TRASH_STALE_S = 300.0


def _trash_freshness(answer: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """`status`, `measured_at` and `age_seconds` for one Mac's Trash reading.

    The Hub stamps `measured_at` when it walks the folder, so this is passed through; a Hub that states
    none gets the Gateway's receipt time. Only `fresh` may be counted in a fleet total: an unreachable Mac
    or one whose Hub is too old states no size, and an old reading is not a measurement now.
    """
    now = time.time() if now is None else float(now)
    stated = answer.get("measured_at")
    measured = float(stated) if isinstance(stated, (int, float)) and not isinstance(stated, bool) else now
    age = max(0.0, now - measured)
    return {"status": "fresh" if age <= TRASH_STALE_S else "stale", "measured_at": measured,
            "age_seconds": round(age, 3)}


def _trash(controller_id: str, machine_id: str, route: str, body: dict[str, Any] | None, *, event: str,
           audit_fields: dict[str, Any], method: str = "POST", client: httpx.Client | None = None) -> dict[str, Any]:
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(controller_id, machine_id, route, body, event=event, too_old=TRASH_TOO_OLD,
                      refused="The controller refused this gateway's credential for Trash Folder actions.",
                      audit_fields=audit_fields, client=client, method=method, timeout=_TRASH_TIMEOUT)


def trash_list(controller_id: str, machine_id: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """One Mac's Trash Folder: `items` (`id`, `kind`, `bytes`, `age_days`), `count`, `bytes`, `by_kind`,
    `by_age`, `disk`, plus `status` (`fresh` / `stale` / `unreachable` / `unknown`) and `measured_at`.
    A Mac whose Studio Hub is older answers `supported: false` with `needs_hub`; one out of reach answers
    `status: unreachable`, so a partial fleet result still lists every Mac and no size is invented."""
    try:
        answer = _trash(controller_id, machine_id, "trash", None, event="trash_list", audit_fields={},
                        method="GET", client=client)
    except StartupError as error:
        code = str(error.body.get("code") or "")
        if code == "HUB_TOO_OLD":
            return {"ok": True, "supported": False, "needs_hub": TRASH_HUB, "detail": TRASH_TOO_OLD,
                    "machine": machine_id, "status": "unknown", "measured_at": None}
        if code in _REACH_CODES:
            return {"ok": True, "supported": False, "machine": machine_id, "status": "unreachable",
                    "measured_at": None,
                    "detail": str(error.body.get("detail") or "This Mac did not answer.")}
        raise
    return {**answer, "supported": True, **_trash_freshness(answer)}


def _trash_items(items: object) -> object:
    if items == "all" or (isinstance(items, list) and items and all(isinstance(i, str) for i in items)):
        return items
    raise StartupError(400, {"ok": False, "code": "BAD_ITEMS", "detail": 'items must be "all" or a list of item ids'})


def trash_move(controller_id: str, machine_id: str, items: object, *,
               client: httpx.Client | None = None) -> dict[str, Any]:
    """Move Trash Folder items (`"all"` or ids) to that Mac's macOS Trash. Answers `moved` and `failed`."""
    items = _trash_items(items)
    return _trash(controller_id, machine_id, "trash/move-to-macos-trash", {"items": items}, event="trash_move",
                  audit_fields={"items": items if items == "all" else len(items)}, client=client)


def trash_delete(controller_id: str, machine_id: str, items: object, confirm: object, expected_count: object,
                 expected_bytes: object, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """Permanently delete Trash Folder items. The Mac re-measures and refuses a count or bytes that differ from
    what the owner confirmed (409 `TRASH_CHANGED`, nothing deleted). Answers `deleted`, `freed_bytes`, `failed`."""
    items = _trash_items(items)
    if confirm is not True or any(type(v) is not int or v < 0 for v in (expected_count, expected_bytes)):
        raise StartupError(400, {"ok": False, "code": "CONFIRM_REQUIRED",
                                 "detail": "Permanent delete needs confirm: true, expected_count and expected_bytes."})
    return _trash(controller_id, machine_id, "trash/delete",
                  {"items": items, "confirm": True, "expected_count": expected_count, "expected_bytes": expected_bytes},
                  event="trash_delete", client=client,
                  audit_fields={"items": items if items == "all" else len(items), "count": expected_count,
                                "bytes": expected_bytes})


# A Mac's macOS Trash preference (owner request 2026-10-19, Studio Hub 0.6.187). Every retirement a Hub
# makes ends in macOS Trash, so a Mac whose Finder never empties that Trash fills its disk - and across
# ~53 Macs nobody could tell which ones were set. The Hub reads it into `startup.macos_trash_auto_empty`
# on every machine row. These turn it **on**, never off: the Hub has no route that turns it off.
MACOS_TRASH_HUB = "0.6.187"
MACOS_TRASH_TOO_OLD = f"Needs Studio Hub {MACOS_TRASH_HUB} to turn on macOS's Trash setting from the Gateway."
#: `defaults write` plus the read-back the Hub verifies; nothing here waits on a user.
_MACOS_TRASH_TIMEOUT = httpx.Timeout(90.0, connect=6.0)


def enable_macos_trash_auto_empty(controller_id: str, machine_id: str, *,
                                  client: httpx.Client | None = None) -> dict[str, Any]:
    """Turn on one Mac's Finder "Remove items from the Trash after 30 days", through its controller.

    One way only: the Hub has no route that turns it off, so nothing here can. The answer carries the
    value the Mac **read back**, not the write's exit code - a write that did not stick is a refusal
    (`NOT_APPLIED`). A Hub older than 0.6.187 answers `HUB_TOO_OLD`; a Mac that is not answering
    answers `NODE_UNREACHABLE`, which no remote action can fix.
    """
    if not _MACHINE_ID.fullmatch(machine_id or ""):
        raise StartupError(400, {"ok": False, "code": "BAD_MACHINE", "detail": "machine id is not valid"})
    return _node_post(controller_id, machine_id, "macos-trash-auto-empty/enable", {},
                      event="macos_trash_enable", too_old=MACOS_TRASH_TOO_OLD,
                      refused="The controller refused this gateway's credential for this Mac.",
                      audit_fields={}, client=client, timeout=_MACOS_TRASH_TIMEOUT)


def _enable_where_off(fact_key: str, ask, *, event: str, dry_run: bool,
                      machines: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Ask every reachable Mac whose Hub said `fact_key` is off, and name each one in the answer.

    Only Macs whose Hub said `false` are touched (never `unknown`: an older Hub cannot take the request,
    and guessing would turn a settings change into a pile of refusals). A Mac that is currently
    unreachable is listed, not tried - its controller cannot pass anything on, so it never blocks the
    rest. The result names every Mac and what happened, so the one button can be audited afterwards.
    """
    rows = machines if machines is not None else (fleet.snapshot(force=True).get("machines") or [])
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for machine in rows:
        fact = machine.get("startup")
        if not fact or fact.get(fact_key) is not False:
            continue
        reachable = machine.get("reachable") is True and not fact.get("stale")
        entry = {"location_id": machine.get("location_id"), "machine": machine.get("machine"),
                 "label": machine.get("label") or machine.get("machine")}
        if not reachable:
            skipped.append({**entry, "reason": _unreachable_reason(machine)})
        else:
            targets.append(entry)
    audit(event, dry_run=dry_run, would_ask=len(targets), skipped=len(skipped))
    if dry_run:
        return {"dry_run": True, "asked": 0, "would_ask": targets, "skipped": skipped,
                "applied": [], "failed": []}

    def one(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        try:
            return entry, ask(str(entry["location_id"]), str(entry["machine"])), None
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
    audit(f"{event}_done", asked=len(targets), applied=len(applied), failed=len(failed))
    return {"dry_run": False, "asked": len(targets), "applied": applied, "failed": failed,
            "skipped": skipped}


def enable_autostart_where_off(*, dry_run: bool = False,
                               machines: list[dict[str, Any]] | None = None,
                               ask=request) -> dict[str, Any]:
    """Turn "Start services on startup" on for every Mac that reported it off.

    Only Macs whose Hub said `false` are touched (never `unknown`: an older Hub cannot take the
    request, and guessing would turn a settings change into a pile of refusals). A Mac that is
    currently unreachable is listed, not tried - its controller cannot pass anything on. The result
    names every Mac and what happened, so the one button can be audited afterwards.
    """
    return _enable_where_off("start_services_on_launch",
                             lambda controller, machine: ask(controller, machine,
                                                             "set_start_services_on_launch", True),
                             event="bulk_enable_autostart", dry_run=dry_run, machines=machines)


def enable_macos_trash_auto_empty_where_off(*, dry_run: bool = False,
                                            machines: list[dict[str, Any]] | None = None,
                                            ask=enable_macos_trash_auto_empty) -> dict[str, Any]:
    """Turn macOS's Trash setting on for every Mac whose Hub said it is off.

    Same rules as `enable_autostart_where_off`, and the same answer: a Mac that is offline or stale is
    listed with why and skipped, never blocking the ones that are answering. Nothing turns it off.
    """
    return _enable_where_off("macos_trash_auto_empty",
                             lambda controller, machine: ask(controller, machine),
                             event="bulk_enable_macos_trash", dry_run=dry_run, machines=machines)


def _unreachable_reason(machine: dict[str, Any]) -> str:
    reason = fleet._node_reach_reason(machine)
    if reason == "no_hub":
        return ("its Studio Hub is not running (services are off); start the services on that Mac once, "
                "then it keeps them on")
    return "that Mac is not answering (asleep, off or off the network); this applies when it is back"
