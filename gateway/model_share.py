"""Install a model on every Mac that can run it, copying it between Macs on the same network.

Owner decision 2026-10-05 (contract `_orchestra/audit-20261001/INTERFACES-0146.md`): each **network**
downloads a model from the internet once; the Macs on that network then copy it from each other at LAN
speed, spreading like a tree.

* **Networks** (`fleet_networks.json`): a name per location (its Macs' default) and an override per Mac,
  because a shop iMac can sit on any network. Empty is "unknown": such a Mac is never paired with another
  and downloads from the internet by itself. The owner's networks are written once, when the file does not
  exist yet (`DEFAULT_NETWORKS`); after that the file is the owner's.
* **The plan** (`model_installs.json`): one row per Mac, written on every change, so a Gateway restart
  carries on following the downloads it started (`resume_on_startup`). Every pass (`tick`):
  follow what is running; then per network start at most **one internet download at a time**, and for each
  waiting Mac copy from a Mac on its network that has the model (each finished Mac becomes a source; a
  source lends to at most `SHARES_PER_SOURCE` at once, idle Macs first). A Mac with no free source waits
  for one rather than pulling from the internet; only a network with no usable copy at all downloads.
* **Relay**: the share on the source's controller, then a strict LAN download on the target's
  (`startup_control.share_model` / `install_model`). A known verified source that is resting,
  unreachable or busy keeps the network waiting; only a network with no known copy starts an internet seed.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, fleet, startup_control

#: The owner's networks (2026-10-05): A = 1100 + 1200, B = 1300 + 1400, C = 1500 + 1600 + Shops.
DEFAULT_NETWORKS = {
    "de7250e146a7": "A", "9701220bf30f": "A",
    "a311a0a9f4f7": "B", "eda08bae6325": "B",
    "b8ccda585b97": "C", "2c7a2403564b": "C", "4bb919aee2e4": "C",
}
NAME_MAX = 24
SHARES_PER_SOURCE = 2
#: Starting a download that the Mac's controller could not be reached for is tried this many times.
ATTEMPTS = 3
#: Status reads in a row that may fail before a download is given up as lost.
MISSES = 20
#: A source that refused or did not answer is not asked again for this long (an old Hub: an hour).
REST = 120.0
REST_TOO_OLD = 3600.0
TICK = 5.0
KEEP_FINISHED = 5
ACTIVE = ("copying", "downloading")
LAN_ONLY_HUB_VERSION = (0, 6, 174)
LAN_ONLY_HUB_VERSION_TEXT = "0.6.174"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

_lock = threading.RLock()
_thread: threading.Thread | None = None
_wake = threading.Event()


class ShareError(Exception):
    def __init__(self, status: int, detail: str, code: str = "REFUSED") -> None:
        super().__init__(detail)
        self.status, self.body = status, {"ok": False, "code": code, "detail": detail}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(scratch, path)


# --- Networks -------------------------------------------------------------------------------------------

def networks_path() -> Path:
    return config.DATA_DIR / "fleet_networks.json"


def networks() -> dict[str, dict[str, str]]:
    """`{"locations": {location id: name}, "machines": {"location/machine": name}}`.

    The first read ever writes the owner's networks (a one-time migration); a file that exists is never
    re-seeded, so a name the owner cleared stays cleared. Read without `_lock` (every write is an atomic
    `os.replace`): a spread's pass holds that lock through its relay calls, and every Fleet page read lands here.
    """
    path = networks_path()
    if not path.exists():
        with _lock:
            if not path.exists():
                _write_json(path, {"locations": dict(DEFAULT_NETWORKS), "machines": {}})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    value = value if isinstance(value, dict) else {}
    return {kind: {str(k): _norm(v) for k, v in (value.get(kind) or {}).items() if isinstance(v, str) and v.strip()}
            if isinstance(value.get(kind), dict) else {} for kind in ("locations", "machines")}


def _norm(name: str) -> str:
    """One spelling per network: "c ", " c" and "C" are the same network C (a typo never splits one)."""
    return " ".join(name.split()).upper()


def clean_name(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ShareError(400, "A network is a short name like A, or empty to clear it.", "BAD_NETWORK")
    name = _norm(value)
    if len(name) > NAME_MAX:
        raise ShareError(400, f"A network name is at most {NAME_MAX} characters.", "BAD_NETWORK")
    return name


def set_network(location_id: str, name: object, machine: str | None = None) -> dict[str, dict[str, str]]:
    """Name a location's network (its Macs' default), or one Mac's (`machine`); empty clears it."""
    name = clean_name(name)
    if location_id != fleet.LOCAL_ID and not any(str(r.get("id")) == location_id for r in fleet.roster()):
        raise ShareError(404, "No such location.", "NOT_FOUND")
    if machine is not None and not _ID.fullmatch(machine):
        raise ShareError(400, "machine id is not valid", "BAD_MACHINE")
    with _lock:
        value = networks()
        table, key = (value["machines"], f"{location_id}/{machine}") if machine else (value["locations"], location_id)
        if name:
            table[key] = name
        else:
            table.pop(key, None)
        _write_json(networks_path(), value)
    return value


def network_of(location_id: object, machine: object, table: dict[str, dict[str, str]] | None = None) -> str:
    table = table or networks()
    return table["machines"].get(f"{location_id}/{machine}") or table["locations"].get(str(location_id), "")


def stamp_networks(out: dict[str, Any]) -> None:
    """`network` on each location row; `network` (in effect) and `network_override` on each Mac's row (copies)."""
    table = networks()
    out["locations"] = [{**row, "network": table["locations"].get(str(row.get("id"))) or None}
                        for row in out.get("locations") or []]
    out["machines"] = [{**row, "network": network_of(row.get("location_id"), row.get("machine"), table) or None,
                        "network_override": table["machines"].get(f"{row.get('location_id')}/{row.get('machine')}")}
                       for row in out.get("machines") or []]


# --- The plan -------------------------------------------------------------------------------------------

def plans_path() -> Path:
    return config.DATA_DIR / "model_installs.json"


def read_plans() -> list[dict[str, Any]]:
    try:
        value = json.loads(plans_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in value.get("plans") or [] if isinstance(p, dict)] if isinstance(value, dict) else []


def _save(plans: list[dict[str, Any]]) -> None:
    running = [p for p in plans if p.get("state") == "running"]
    _write_json(plans_path(), {"plans": running + [p for p in plans if p.get("state") != "running"][:KEEP_FINISHED]})


def _model_rows(snapshot: dict[str, Any], package_id: str) -> dict[str, dict[str, Any]]:
    """`location/machine` -> that Mac's catalog row for the package."""
    return {f"{loc.get('id')}/{row.get('machine')}": row
            for loc in snapshot.get("locations") or [] for row in loc.get("local_models") or []
            if isinstance(row, dict) and row.get("package_id") == package_id and row.get("machine")}


def _why_not(row: dict[str, Any], model: dict[str, Any] | None) -> str:
    if model is None:
        return "Its Studio Hub does not offer this model."
    if model.get("active_download"):
        return "It is already downloading it."
    if (row.get("workstation") or {}).get("enabled"):
        return "Lite Mode: this model is not one Lite Mode runs."
    return "Not enough memory for this model."


def start(package_id: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan installing `package_id` on every Mac that can run it and start the spread. Answers the plan's view."""
    if not isinstance(package_id, str) or not _ID.fullmatch(package_id):
        raise ShareError(400, "model id is not valid", "BAD_MODEL")
    snapshot = snapshot if snapshot is not None else fleet.snapshot()
    with _lock:
        plans = read_plans()
        if any(p.get("package_id") == package_id and p.get("state") == "running" for p in plans):
            raise ShareError(409, "This model is already being installed on every Mac. Stop that first, "
                                  "or wait for it to finish.", "ALREADY_RUNNING")
        table, models = networks(), _model_rows(snapshot, package_id)
        this_mac_paused = fleet.owner_paused_at(fleet.LOCAL_ID) is not None
        label, macs = None, []
        for row in snapshot.get("machines") or []:
            loc, mid = row.get("location_id"), row.get("machine")
            if not loc or not mid:
                continue
            key = f"{loc}/{mid}"
            model = models.get(key)
            offered = next((m for m in row.get("installable_models") or [] if m.get("package_id") == package_id), None)
            label = label or (offered or {}).get("label") or (model or {}).get("label")
            entry = {"key": key, "location_id": loc, "machine": mid, "label": row.get("label") or mid,
                     "network": network_of(loc, mid, table), "state": "waiting", "attempts": 0}
            if (model or {}).get("cache_state") == "cached":
                entry["state"] = "have"
            elif loc == fleet.LOCAL_ID and this_mac_paused:
                entry.update(state="skipped", detail="This Mac is paused for generation.")
            elif row.get("reachable") is not True:
                entry.update(state="skipped", detail="Offline.")
            elif not offered:
                entry.update(state="skipped", detail=_why_not(row, model))
            macs.append(entry)
        if not any(e["state"] == "waiting" for e in macs):
            raise ShareError(409, "No Mac needs it: every Mac that can run it has it already, or is offline.",
                             "NOTHING_TO_DO")
        macs.sort(key=lambda e: (e["network"] or "~", str(e["label"]).lower()))
        plan = {"id": uuid.uuid4().hex[:12], "package_id": package_id, "label": label or package_id,
                "state": "running", "created_at": round(time.time(), 3), "macs": macs, "resting": {}}
        startup_control.audit("model_spread_started", package=package_id, plan=plan["id"],
                              macs=sum(e["state"] == "waiting" for e in macs))
        _save([plan, *plans])
    _ensure_thread()
    return view(plan)


def stop(plan_id: str) -> dict[str, Any]:
    """No new copies or downloads; the ones already started finish on their Macs."""
    with _lock:
        plans = read_plans()
        plan = next((p for p in plans if p.get("id") == plan_id), None)
        if plan is None:
            raise ShareError(404, "No such install.", "NOT_FOUND")
        if plan.get("state") == "running":
            plan.update(state="stopped", finished_at=round(time.time(), 3))
            for entry in plan["macs"]:
                if entry["state"] == "waiting":
                    entry.update(state="skipped", detail="Stopped before it started.")
            startup_control.audit("model_spread_stopped", plan=plan_id)
            _save(plans)
    return view(plan)


def tick(snapshot: dict[str, Any] | None = None) -> None:
    """One pass over every running plan (the worker thread's loop; tests call it directly)."""
    with _lock:
        plans = read_plans()
        if not any(p.get("state") == "running" for p in plans):
            return
        snapshot = snapshot if snapshot is not None else fleet.snapshot()
        for plan in plans:
            if plan.get("state") == "running":
                _advance(plan, snapshot)
        _save(plans)


def _sentence(error: startup_control.StartupError) -> str:
    return str(error.body.get("detail") or error.body.get("code") or f"HTTP {error.status}")


def _follow(entry: dict[str, Any], resting: dict[str, float], now: float) -> None:
    try:
        answer = startup_control.model_install_status(entry["location_id"], entry["machine"], entry["run_id"])
    except startup_control.StartupError as error:
        entry["misses"] = entry.get("misses", 0) + 1
        if entry["misses"] >= MISSES:
            entry.update(state="failed", detail=f"Lost track of the download: {_sentence(error)}")
        return
    entry["misses"] = 0
    run = answer.get("run") if isinstance(answer.get("run"), dict) else {}
    for key in ("bytes_done", "total_bytes"):
        if isinstance(run.get(key), (int, float)):
            entry[key] = run[key]
    if run.get("source_used") in ("lan", "internet", "mixed"):
        entry["source_used"] = run["source_used"]
    if run.get("lan_error"):
        entry["lan_error"] = str(run["lan_error"])[:200]
    if entry["state"] == "copying" and entry.get("source_used") in ("internet", "mixed"):
        # The copy fell back to the internet (Hub 0.6.148 says so mid-run): it says so now, and holds its
        # network's one internet download instead of a source's LAN slot.
        entry["state"] = "downloading"
    state = run.get("state")
    if state == "done":
        entry.update(state="done", finished_at=round(now, 3), detail=None)
    elif state in ("error", "interrupted", "cancelled"):
        tail = str(run.get("error_tail") or state).strip().splitlines()[-1:] or [state]
        if entry.get("from_key"):
            resting[entry["from_key"]] = now + REST
        entry["attempts"] = entry.get("attempts", 0) + 1
        if state != "cancelled" and entry["attempts"] < ATTEMPTS:
            entry.update(state="waiting", run_id=None, detail=f"Trying again after: {tail[0]}")
        else:
            entry.update(state="failed", detail=tail[0])


#: The Hub's refusal of a model the Mac has too little memory for (`standalone._memory_refusal`).
_MEMORY_REFUSAL = re.compile(r"needs a Mac with at least [\d.]+ GB of memory")


def _start(entry: dict[str, Any], package_id: str, source: dict[str, Any] | None, now: float,
           from_entry: dict[str, Any] | None = None) -> bool:
    try:
        answer = startup_control.install_model(
            entry["location_id"], entry["machine"], package_id, source=source,
            **({"peer_only": True} if source is not None else {}))
    except startup_control.StartupError as error:
        if _MEMORY_REFUSAL.search(_sentence(error)):   # nothing was downloaded: the same as a plan-time skip
            entry.update(state="skipped", detail="Not enough memory for this model.")
            return False
        entry["attempts"] = entry.get("attempts", 0) + 1
        retry = error.body.get("code") in startup_control._REACH_CODES and entry["attempts"] < ATTEMPTS  # noqa: SLF001
        entry.update(state="waiting" if retry else "failed", detail=_sentence(error))
        return False
    run = answer.get("run") if isinstance(answer.get("run"), dict) else {}
    if not run.get("id"):
        entry.update(state="failed", detail="The Mac did not say it started.")
        return False
    entry.update(state="copying" if source else "downloading", run_id=run["id"], started_at=round(now, 3),
                 bytes_done=run.get("bytes_done") or 0, total_bytes=run.get("total_bytes"), misses=0, detail=None,
                 from_key=(from_entry or {}).get("key"), from_label=(from_entry or {}).get("label"),
                 source_used=None, lan_error=None)
    return True


def _switch_on(location_id: str, package_id: str, snapshot: dict[str, Any]) -> None:
    """The owner asked for this install, so he wants it used: switch the package's model on at that location.

    Only where the location has no decision for it yet (an "off" stands), and never a model the owner ruled
    out (`fleet.NEVER_AUTO_ENABLED`). Idempotent, so every pass may call it for every finished Mac."""
    from .providers import registry

    model_ids = [str(row.get("model") or row.get("model_id"))
                 for loc in snapshot.get("locations") or [] if str(loc.get("id")) == str(location_id)
                 for row in loc.get("local_models") or []
                 if isinstance(row, dict) and row.get("package_id") == package_id
                 and (row.get("model") or row.get("model_id"))
                 and not fleet.ruled_out(row.get("model"), row.get("model_id"), package_id)]
    if not model_ids:
        return
    try:
        added = registry.models.enable_undecided(fleet.provider_id_for(str(location_id)), model_ids)
    except KeyError:   # the location left the roster
        return
    if added:
        startup_control.audit("model_spread_enabled", package=package_id, location=location_id, models=added)


def _advance(plan: dict[str, Any], snapshot: dict[str, Any]) -> None:
    now = time.time()
    package_id = plan["package_id"]
    resting: dict[str, float] = {k: v for k, v in (plan.get("resting") or {}).items() if v > now}
    rows = {f"{r.get('location_id')}/{r.get('machine')}": r for r in snapshot.get("machines") or []}
    controller_versions = {str(r.get("id")): r.get("version") for r in snapshot.get("locations") or []}
    macs = plan["macs"]
    for entry in macs:
        entry["network"] = _norm(entry.get("network") or "")   # a plan started before names were normalised
        if entry["state"] in ACTIVE:
            _follow(entry, resting, now)
        if entry["state"] == "done":
            _switch_on(entry["location_id"], package_id, snapshot)

    # Who could lend it: a Mac in the plan that has it now, or any Mac the Fleet read shows holding it verified.
    known = {e["key"]: e for e in macs}
    held = {k for k, m in _model_rows(snapshot, package_id).items() if m.get("cache_state") == "cached"}
    held |= {e["key"] for e in macs if e["state"] in ("done", "have")}

    def usable(key: str) -> bool:
        row = rows.get(key) or {}
        return (row.get("reachable") is True and row.get("node_paused") is not True
                and fleet.owner_paused_at(str(row.get("location_id"))) is None)

    def lender(key: str) -> bool:
        # This Mac's own Hub is reached at 127.0.0.1, so a share it hands out names an address no other Mac can
        # reach (the Hub builds `url_base` from the Host it was called on): it is never a source.
        return usable(key) and not key.startswith(f"{fleet.LOCAL_ID}/")

    def describe(key: str) -> dict[str, Any]:
        row = rows.get(key) or {}
        return known.get(key) or {"key": key, "label": row.get("label") or row.get("machine"),
                                  "location_id": row.get("location_id"), "machine": row.get("machine")}

    table = networks()
    sources: dict[str, list[str]] = {}
    known_sources: dict[str, list[str]] = {}
    for key in held:
        row = rows.get(key) or {}
        identity = row or known.get(key) or {}
        name = network_of(identity.get("location_id"), identity.get("machine"), table) if identity else ""
        if name and not key.startswith(f"{fleet.LOCAL_ID}/"):
            known_sources.setdefault(name, []).append(key)
        if name and lender(key):
            sources.setdefault(name, []).append(key)
    load: dict[str, int] = {}
    internet: set[str] = set()
    for entry in macs:
        if entry["state"] == "copying" and entry.get("from_key"):
            load[entry["from_key"]] = load.get(entry["from_key"], 0) + 1
        if entry["state"] == "downloading":
            internet.add(entry["network"] or f"~{entry['key']}")

    for entry in macs:
        if entry["state"] != "waiting":
            continue
        if not usable(entry["key"]):
            entry["detail"] = "Offline or paused — waits for it to come back."
            continue
        network = entry["network"]
        bucket = network or f"~{entry['key']}"
        notes: list[str] = []
        tried = False
        if known_sources.get(network):
            version = (rows.get(entry["key"]) or {}).get("hub_version")
            version_key = fleet._version_key(version)  # noqa: SLF001 - shared fleet wire-version parser
            if version_key is None or version_key < LAN_ONLY_HUB_VERSION:
                entry["detail"] = (
                    f"Waiting: this Mac needs Studio Hub {LAN_ONLY_HUB_VERSION_TEXT} for LAN-only model "
                    f"copies (reported {str(version or 'unknown')}).")
                continue
            controller_version = controller_versions.get(str(entry["location_id"]))
            controller_key = fleet._version_key(controller_version)  # noqa: SLF001
            if controller_key is None or controller_key < LAN_ONLY_HUB_VERSION:
                entry["detail"] = (
                    f"Waiting: this Mac's controller needs Studio Hub {LAN_ONLY_HUB_VERSION_TEXT} to relay "
                    f"LAN-only model copies (reported {str(controller_version or 'unknown')}).")
                continue
        # Idle Macs first, then the least busy; never one that just refused or failed.
        for key in sorted((k for k in sources.get(network, []) if k not in resting and k != entry["key"]
                           and load.get(k, 0) < SHARES_PER_SOURCE),
                          key=lambda k: (bool((rows.get(k) or {}).get("current")), load.get(k, 0),
                                         str(describe(k)["label"]))):
            lending = describe(key)
            try:
                share = startup_control.share_model(str(lending["location_id"]), str(lending["machine"]),
                                                    package_id).get("share") or {}
                if not share.get("url_base") or not share.get("token"):
                    raise startup_control.StartupError(502, {"code": "BAD_SHARE",
                                                             "detail": "That Mac did not hand out a share."})
            except startup_control.StartupError as error:
                resting[key] = now + (REST_TOO_OLD if error.body.get("code") == "HUB_TOO_OLD" else REST)
                notes.append(f"{lending['label']}: {_sentence(error)}")
                continue
            tried = True
            source = share   # passed unchanged (Hub notes 2026-10-05); never written to the plan file
            if _start(entry, package_id, source, now, lending):
                load[key] = load.get(key, 0) + 1
            break
        if tried:   # started, or its controller did not answer (tried again next pass)
            entry["note"] = "; ".join(notes) or None
            continue
        free_or_busy = [k for k in sources.get(network, []) if k not in resting and k != entry["key"]]
        if free_or_busy:
            entry["detail"] = f"Waiting for a Mac on network {network} to copy from."
        elif bucket in internet:
            entry["detail"] = f"Waiting for the internet download on network {network} to finish." if network \
                else "Waiting to download."
        elif known_sources.get(network):
            entry["detail"] = f"Waiting for a known verified copy on network {network} to become available."
        elif _start(entry, package_id, None, now):
            internet.add(bucket)
        entry["note"] = "; ".join(notes) or entry.get("note")
    plan["resting"] = resting

    # Finished when nothing is running and nothing that could start is left (a Mac that stayed offline is not
    # waited for forever).
    if not any(e["state"] in ACTIVE for e in macs) and not any(
            e["state"] == "waiting" and usable(e["key"]) for e in macs):
        for entry in macs:
            if entry["state"] == "waiting":
                entry.update(state="skipped", detail="Offline — not installed.")
        plan.update(state="finished", finished_at=round(now, 3))
        startup_control.audit("model_spread_finished", plan=plan["id"], package=package_id,
                              done=sum(e["state"] == "done" for e in macs),
                              failed=sum(e["state"] == "failed" for e in macs))


# --- What the page shows --------------------------------------------------------------------------------

def _percent(entry: dict[str, Any]) -> str:
    total, done = entry.get("total_bytes") or 0, entry.get("bytes_done") or 0
    return f" {min(99, int(done * 100 / total))}%" if total else ""


def text(entry: dict[str, Any]) -> str:
    """The Mac's line in the progress view: "Copying 42% from 1104 (LAN)", "Installed from the internet"."""
    state, lan = entry["state"], f"from {entry.get('from_label')} (LAN)"
    why = f": {entry['lan_error']}" if entry.get("lan_error") else ""
    fell_back = f" (the copy {lan} did not work{why})" if entry.get("from_key") else ""
    if state == "have":
        return "Already has it"
    if state == "copying":
        return f"Copying{_percent(entry)} {lan}"
    if state == "downloading":
        return f"Downloading{_percent(entry)} from the internet{fell_back}"
    if state == "done":
        used = entry.get("source_used") or ("lan" if entry.get("from_key") else "internet")
        if not entry.get("from_key") or used == "internet":
            return f"Installed from the internet{fell_back}"
        return f"Installed {lan}" + (" and the internet" if used == "mixed" else "")
    if state == "failed":
        return f"Failed: {entry.get('detail') or 'unknown error'}"
    if state == "skipped":
        return f"Skipped: {entry.get('detail')}"
    return entry.get("detail") or "Waiting"


def view(plan: dict[str, Any]) -> dict[str, Any]:
    """A plan as the page draws it: per network, per Mac, each with its sentence. No share token is ever kept."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in plan.get("macs") or []:
        copied = entry.get("from_key") and entry.get("state") != "downloading"   # a copy that fell back is internet
        groups.setdefault(entry.get("network") or "", []).append({
            "location_id": entry.get("location_id"), "machine": entry.get("machine"), "label": entry.get("label"),
            "state": entry.get("state"), "source": "lan" if copied else (
                "internet" if entry.get("state") in ("downloading", "done") else None),
            "from": entry.get("from_label"), "bytes_done": entry.get("bytes_done"),
            "total_bytes": entry.get("total_bytes"), "text": text(entry), "note": entry.get("note")})

    def counts(rows: list[dict[str, Any]]) -> tuple[int, int]:
        wanted = [r for r in rows if r["state"] != "skipped"]
        return sum(r["state"] in ("done", "have") for r in wanted), len(wanted)

    networks_out = []
    for name in sorted(groups, key=lambda n: (n == "", n)):
        done, total = counts(groups[name])
        networks_out.append({"network": name or None, "title": f"Network {name}" if name else "No network set",
                             "done": done, "total": total, "macs": groups[name]})
    done, total = counts([m for rows in groups.values() for m in rows])
    return {"id": plan.get("id"), "package_id": plan.get("package_id"), "label": plan.get("label"),
            "state": plan.get("state"), "created_at": plan.get("created_at"), "finished_at": plan.get("finished_at"),
            "done": done, "total": total, "networks": networks_out}


def status() -> list[dict[str, Any]]:
    return [view(plan) for plan in read_plans()]


# --- The worker -----------------------------------------------------------------------------------------

def resume_on_startup() -> None:
    """A Gateway restart in the middle of a spread carries on following it."""
    if any(p.get("state") == "running" for p in read_plans()):
        _ensure_thread()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        _wake.set()
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="model-share", daemon=True)
        _thread.start()


def _run() -> None:
    while any(p.get("state") == "running" for p in read_plans()):
        _wake.clear()
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the spread
            print(f"[model-share] pass failed: {type(exc).__name__}: {exc}", flush=True)
        _wake.wait(TICK)
