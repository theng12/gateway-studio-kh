"""Remote final rendering: which Macs the owner lets render a final video without formal qualification.

**Owner decision (2026-10-23).** "I want to be able to start doing remote rendering asap. It should be
enablement in Gateway, and maybe we start with the ones on the same network." Studio Hub 0.6.195 offers
no render capacity until a Mac holds a qualification record, so until now no Mac could be asked. A Hub
that has been told to *accept unqualified* work publishes a production-shaped render row with one extra
key, `accepted_unqualified` (`{provenance: "owner-opt-in-unqualified", accepted_by, accepted_at}`);
this module is the other half - the owner's per-Mac choice, kept here, and the picture of the fleet the
admin page draws.

**Two keys, both required, and the default is off.** A Mac renders for Story only when

1. the owner switched it on here (`render_optin.json`, absent = off for every Mac), **and**
2. its own Studio Hub publishes the `accepted_unqualified` evidence on its render row.

A Gateway switch with no Hub confirmation (an older Hub, a relay still queued) leaves the Mac
ineligible and never an error. A Hub that accepts with no Gateway switch is **excluded**, not treated as
the qualified Mac its production-shaped row would otherwise pass for: the owner's switch is the control.
With nothing switched on, no Mac is added to what 0.2.195 would select.

**No new door.** The setting travels on the controller relay every other Fleet action uses
(`startup_control.set_node_render_authority`); there is no SSH, no new port and no shell. Everything
else here is read-only against the fleet snapshot the Gateway already holds.

**Same network** is judged from addresses the fleet already publishes: the Mac's own interface list when
its Hub sends one (`machine.network`: address + prefix length), else the `node.address` its controller
dials, against the networks this Mac's own interfaces are on. A private LAN address inside one of those
networks is `same_network`; a private address elsewhere is `other_lan`; 100.64.0.0/10 is Tailscale; a
hostname, a public address or no address is `unknown` - never guessed. `network_source` says which.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("gateway.render_optin")

#: The key a Hub adds to its `video.final_render` worker row once the owner told it to accept work without a
#: measured qualification, and the one provenance string that key may carry.
OPT_IN_KEY = "accepted_unqualified"
PROVENANCE = "owner-opt-in-unqualified"
#: The label a routed render carries (`authority`) when it went to such a Mac.
ACCEPTED = "accepted_unqualified"
FORMAT = "gateway.render-optin.v1"

#: Network classes, in the order the page lists them (same network first).
SAME_NETWORK, OTHER_LAN, TAILSCALE, UNKNOWN = "same_network", "other_lan", "tailscale", "unknown"
_NETWORK_ORDER = {SAME_NETWORK: 0, OTHER_LAN: 1, TAILSCALE: 2, UNKNOWN: 3}
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_lock = threading.Lock()


# --- the owner's switches ------------------------------------------------------------------------------

def optin_path():
    return config.DATA_DIR / "render_optin.json"


def _key(controller_id: str, machine_id: str) -> str:
    return f"{controller_id}/{machine_id}"


def _read() -> dict[str, dict[str, Any]]:
    """The switches that are on. Unreadable or malformed is *nothing on*: off is the safe answer."""
    try:
        value = json.loads(optin_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    machines = value.get("machines") if isinstance(value, dict) else None
    if not isinstance(machines, dict):
        return {}
    return {key: row for key, row in machines.items()
            if isinstance(key, str) and isinstance(row, dict) and row.get("enabled") is True}


def enabled_keys() -> frozenset[str]:
    """Every `controller/machine` the owner has switched on."""
    return frozenset(_read())


def is_enabled(controller_id: str, machine_id: str) -> bool:
    return _key(controller_id, machine_id) in _read()


def set_enabled(controller_id: str, machine_id: str, enabled: bool, *, by: str = "gateway-admin") -> None:
    """Switch one Mac on or off. Off removes the entry: absence is the default, not a second state."""
    key = _key(controller_id, machine_id)
    with _lock:
        machines = _read()
        if enabled:
            machines[key] = {"enabled": True, "at": round(time.time(), 3), "by": by}
        else:
            machines.pop(key, None)
        path = optin_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(path.name + ".tmp")
        with os.fdopen(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                       encoding="utf-8") as handle:
            json.dump({"format": FORMAT, "machines": machines}, handle, sort_keys=True)
        os.replace(scratch, path)


# --- what a Hub says about itself -----------------------------------------------------------------------

def unqualified_claim(worker: object) -> str:
    """What this render row says about unqualified acceptance: `none`, `accepted` or `invalid`.

    The Hub sends the evidence key only while its owner opt-in is the authority, so absence (or an
    explicit null) is "no claim" - a 0.6.195 row, or a Hub with the setting off. A key that is there but
    is not the Hub's evidence is `invalid`: the row cannot be told apart from a qualified one, so it is
    not selected at all.
    """
    if not isinstance(worker, dict) or worker.get(OPT_IN_KEY) is None:
        return "none"
    evidence = worker[OPT_IN_KEY]
    return "accepted" if isinstance(evidence, dict) and evidence.get("provenance") == PROVENANCE \
        else "invalid"


def accepts_unqualified(worker: object) -> bool:
    """True when this worker row carries the Hub's own evidence that it accepts unqualified work."""
    return unqualified_claim(worker) == "accepted"


def authority_of(capability: object) -> str:
    """`production`, `accepted_unqualified` or `qualification` - what a stored capability snapshot was."""
    if isinstance(capability, dict):
        if capability.get("qualification_only") is True:
            return "qualification"
        if accepts_unqualified(capability):
            return ACCEPTED
    return "production"


# --- same local network ---------------------------------------------------------------------------------

_INET = re.compile(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]{1,8})\b", re.MULTILINE)


def parse_ifconfig(text: str) -> list[ipaddress.IPv4Network]:
    """The IPv4 networks an `ifconfig -a` listing puts this Mac on.

    Loopback, link-local and Tailscale addresses are not a LAN and are left out, so a Tailscale tunnel
    can never make every Tailscale Mac look like a neighbour.
    """
    networks: list[ipaddress.IPv4Network] = []
    for address, mask in _INET.findall(text or ""):
        try:
            host = ipaddress.IPv4Address(address)
            network = ipaddress.ip_network(
                f"{address}/{ipaddress.IPv4Address(int(mask, 16))}", strict=False)
        except ValueError:
            continue
        if host.is_loopback or host.is_link_local or host in _TAILSCALE_V4 or network.prefixlen < 8:
            continue
        if network not in networks:
            networks.append(network)
    return networks


_NETS_TTL = 30.0
_nets: dict[str, Any] = {"at": 0.0, "value": []}


def local_networks() -> list[ipaddress.IPv4Network]:
    """This Mac's LAN networks, read from its own interfaces (a local read, cached for 30 s).

    An interface listing that cannot be read is an empty list, which classifies everything as not on
    this network - never a guess that something is.
    """
    now = time.monotonic()
    with _lock:
        if now - _nets["at"] < _NETS_TTL:
            return list(_nets["value"])
    try:
        output = subprocess.run(["/sbin/ifconfig", "-a"], capture_output=True, text=True,
                                timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        output = ""
    value = parse_ifconfig(output)
    with _lock:
        _nets.update(at=now, value=value)
    return list(value)


def _address_of(address: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(address, str) or not address.strip():
        return None
    text = address.strip()
    try:
        host = urlsplit(text if "://" in text else "//" + text).hostname
        return ipaddress.ip_address(host) if host else None
    except ValueError:
        return None   # a hostname (mDNS names included) says nothing without resolving it


def classify(address: object, networks: list | None = None) -> str:
    """`same_network`, `other_lan`, `tailscale` or `unknown` for one published node address."""
    ip = _address_of(address)
    if ip is None:
        return UNKNOWN
    if ip in _TAILSCALE_V4 or ip in _TAILSCALE_V6:
        return TAILSCALE
    if ip.is_loopback or not (ip.is_private or ip.is_link_local):
        return UNKNOWN
    return SAME_NETWORK if any(ip in network for network in (networks or [])
                               if network.version == ip.version) else OTHER_LAN


def network_rank(network_class: str) -> int:
    return _NETWORK_ORDER.get(network_class, _NETWORK_ORDER[UNKNOWN])


def _lan_interfaces(published: object) -> list[ipaddress.IPv4Interface] | None:
    """A Hub's own interface list (`[{interface, address, prefix_length}]`) as LAN interfaces.

    `None` when the Hub sent none (an older Hub); an empty list when it sent some and none is a LAN.
    Loopback, link-local and Tailscale interfaces are not a shared network and are left out.
    """
    if not isinstance(published, list):
        return None
    out: list[ipaddress.IPv4Interface] = []
    for entry in published:
        try:
            interface = ipaddress.IPv4Interface(f"{entry['address']}/{int(entry['prefix_length'])}")
        except (KeyError, TypeError, ValueError):
            continue
        ip = interface.ip
        if ip.is_loopback or ip.is_link_local or ip in _TAILSCALE_V4 or interface.network.prefixlen < 8:
            continue
        out.append(interface)
    return out


def classify_machine(machine: object, address: object, networks: list | None = None) -> tuple[str, str]:
    """`(class, source)` for one Mac: its own interface list when it sent one, else its dialled address.

    Sharing a subnet with any of this Mac's networks is `same_network` whichever way the controller
    reaches it; otherwise the dialled address decides (Tailscale stays Tailscale), and a Mac that
    published LAN interfaces but no usable address is still `other_lan`, not unknown.
    """
    by_address = classify(address, networks)
    interfaces = _lan_interfaces(machine.get("network") if isinstance(machine, dict) else None)
    if interfaces is None:
        return by_address, "address"
    if any(interface.network.overlaps(network) for interface in interfaces
           for network in (networks or []) if network.version == 4):
        return SAME_NETWORK, "interfaces"
    if by_address == UNKNOWN and interfaces:
        return OTHER_LAN, "interfaces"
    return by_address, "address"


# --- the page's picture of the fleet --------------------------------------------------------------------

def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value == value and abs(value) != float("inf") else None


def workers(view: dict[str, Any], *, networks: list | None = None) -> list[dict[str, Any]]:
    """Every Mac in the fleet snapshot that publishes `video.final_render`, same network first.

    A controller's own Mac is left out: the Gateway never sends a render to it (`fleet.render_candidates`).
    `hub_accepting` is the Hub's own word; `opted_in` is the owner's switch here; a Mac is only used
    when both are true. `suggested` marks the awake same-network Macs not yet switched on.
    """
    switched_on = enabled_keys()
    nets = local_networks() if networks is None else networks
    rows: list[dict[str, Any]] = []
    for location in (view.get("locations") if isinstance(view, dict) else None) or []:
        if not isinstance(location, dict) or location.get("local") is not False:
            continue
        controller = str(location.get("id") or "")
        machines = {str(item.get("physical_machine_id") or item.get("machine") or item.get("id") or ""): item
                    for item in location.get("machines") or [] if isinstance(item, dict)}
        nodes = {str(item.get("id") or ""): item for item in location.get("nodes") or []
                 if isinstance(item, dict)}
        for worker in location.get("workers") or []:
            if (not isinstance(worker, dict)
                    or "video.final_render" not in (worker.get("supported_operations") or [])):
                continue
            machine_id = str(worker.get("physical_machine_id") or worker.get("machine") or "")
            if not controller or not machine_id or machine_id == str(location.get("controller_id") or ""):
                continue
            machine, node = machines.get(machine_id, {}), nodes.get(machine_id, {})
            address = node.get("address") if isinstance(node.get("address"), str) else None
            klass, source = classify_machine(machine, address, nets)
            opted_in = _key(controller, machine_id) in switched_on
            claim = unqualified_claim(worker)
            accepting = claim == "accepted"
            production = (claim == "none" and worker.get("production_enabled") is True
                          and worker.get("production_eligible") is True)
            evidence = worker[OPT_IN_KEY] if accepting else {}
            busy = (worker.get("busy") is True or machine.get("current_job") is not None
                    or node.get("current_job") is not None)
            if worker.get("online") is not True or machine.get("reachable") is False:
                idle = "offline"
            elif busy:
                idle = "busy"
            elif worker.get("ready") is True:
                idle = "idle"
            else:
                idle = "not_ready"
            rows.append({
                "controller_id": controller,
                "location": location.get("label"),
                "machine_id": machine_id,
                "machine": machine.get("label") or machine_id,
                "chip": machine.get("chip"),
                "memory_gb": _num(machine.get("memory_gb")),
                "available_gb": _num(machine.get("available_gb")),
                "hub_version": node.get("version") if isinstance(node.get("version"), str) else None,
                "idle_state": idle,
                "network": klass,
                "network_source": source,
                "address": address,
                "authority": ACCEPTED if accepting else "production" if production else "none",
                "opted_in": opted_in,
                "hub_accepting": accepting,
                "accepted_by": evidence.get("accepted_by") if isinstance(evidence.get("accepted_by"), str)
                else None,
                "accepted_at": evidence.get("accepted_at") if isinstance(evidence.get("accepted_at"), int)
                else None,
                "suggested": (klass == SAME_NETWORK and not opted_in and not production
                              and claim != "invalid" and idle != "offline"),
                "note": _note(opted_in, accepting, production, claim == "invalid"),
            })
    rows.sort(key=lambda row: (network_rank(row["network"]), str(row["machine"]).lower(),
                               row["controller_id"], row["machine_id"]))
    return rows


def _note(opted_in: bool, accepting: bool, production: bool, invalid: bool) -> str | None:
    if invalid:
        return "This Mac's Studio Hub sent a remote-rendering statement the Gateway cannot read, so it is not used."
    if production:
        return "Already qualified: this Mac renders without the switch."
    if opted_in and not accepting:
        return ("Switched on here, but this Mac's Studio Hub has not confirmed it yet. It stays unused "
                "until it does; an older Studio Hub never will.")
    if accepting and not opted_in:
        return "This Mac's Studio Hub accepts unqualified work, but it is switched off here, so none is sent."
    return None


def recent_jobs(store: Any, limit: int = 20) -> list[dict[str, Any]]:
    """The newest routed renders with the authority they were routed on, so an unqualified one reads as such."""
    try:
        rows = store.recent_render_routes(limit)
    except Exception:   # the page must still draw its Macs when the journal cannot be read
        log.warning("render routes could not be read", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            capability = json.loads(bytes(row["capability_snapshot"]).decode("utf-8"))
            machine = json.loads(bytes(row["selected_machine"]).decode("utf-8"))
        except (ValueError, UnicodeError, TypeError):
            capability, machine = None, {}
        authority = authority_of(capability) if isinstance(capability, dict) else None
        out.append({
            "id": row["id"], "state": row["state"], "created_at": row["created_at"],
            "controller_id": row["destination_controller"], "machine_id": row["destination_node"],
            "machine": (machine.get("label") if isinstance(machine, dict) else None)
            or row["destination_node"],
            "authority": authority, "unqualified": authority == ACCEPTED,
        })
    return out


def listing(store: Any = None) -> dict[str, Any]:
    """The Remote final rendering page: its Macs (same network first) and the newest routed jobs."""
    from . import fleet   # late: fleet imports this module

    view = fleet.snapshot(allow_stale=True)
    return {
        "workers": workers(view),
        "recent_jobs": recent_jobs(store) if store is not None else [],
        "untested": True,
    }


def set_optin(controller_id: str, machine_id: str, enabled: object, *, client: Any = None,
              by: str = "gateway-admin") -> dict[str, Any]:
    """Switch one Mac on or off, and tell its Studio Hub through the controller relay.

    **On** needs the relay to succeed first: a Mac the Hub did not take is not switched on, so the page
    never shows a switch that did nothing. (A Mac the controller can only reach at its next check-in
    answers `queued`; that counts as taken, and the Mac stays unused until its Hub confirms.)
    **Off** is written here first - the Gateway stops sending at once - and the Hub is told afterwards;
    if it cannot be reached the answer says so, and nothing is sent to it either way. Raises
    `startup_control.StartupError` for a bad request or a refused relay.
    """
    from . import fleet, startup_control   # late: both import this module's neighbours

    if not isinstance(enabled, bool):
        raise startup_control.StartupError(400, {
            "ok": False, "code": "VALUE_REQUIRED", "detail": "enabled must be true or false"})
    if enabled:
        listed = {(row["controller_id"], row["machine_id"])
                  for row in workers(fleet.snapshot(allow_stale=True), networks=[])}
        if (controller_id, machine_id) not in listed:
            raise startup_control.StartupError(404, {
                "ok": False, "code": "NOT_A_RENDER_MAC",
                "detail": "That Mac is not publishing final rendering, so it cannot be switched on."})
        answered = startup_control.set_node_render_authority(controller_id, machine_id, True, client=client)
        set_enabled(controller_id, machine_id, True, by=by)
        startup_control.audit("render_optin_changed", controller=controller_id, machine=machine_id,
                              enabled=True, queued=answered.get("queued") is True)
        return {"ok": True, "opted_in": True, "relayed": True, "machine": machine_id,
                "queued": answered.get("queued") is True,
                "render_authority": answered.get("render_authority")}
    set_enabled(controller_id, machine_id, False, by=by)
    startup_control.audit("render_optin_changed", controller=controller_id, machine=machine_id, enabled=False)
    try:
        answered = startup_control.set_node_render_authority(controller_id, machine_id, False, client=client)
    except startup_control.StartupError as error:
        return {"ok": True, "opted_in": False, "relayed": False, "machine": machine_id,
                "code": error.body.get("code"),
                "detail": "Switched off here: no render will be sent to this Mac. Its Studio Hub could not "
                          f"be told ({error.body.get('detail') or error.body.get('code') or 'no answer'})."}
    return {"ok": True, "opted_in": False, "relayed": True, "machine": machine_id,
            "queued": answered.get("queued") is True,
            "render_authority": answered.get("render_authority")}
