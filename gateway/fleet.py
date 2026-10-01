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

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config
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


#: The single namespace every location's models are published under.
#:
#: A location is a provider *internally* — that is what gives it discovery, a
#: credential and an enable toggle — but locations are not separate products.
#: Story Studio sees one ``studio-hub::<model>`` row and the gateway chooses which
#: location runs it.
#:
#: Publishing one row per location instead showed the same model as many times as
#: there were sites, all with the same `display_name`, and turned "which model?"
#: into "which building?". It also broke a saved preset whenever that site went
#: offline. Namespace by **maker**, never by site — the same reason
#: `gateway/catalog.py` namespaces at all, applied consistently.
FLEET_PROVIDER_ID = "studio-hub"

#: What Story Studio is told the maker of a location's models is. The old
#: GenStudio never exposed a site or a machine to the customer, and a migration in
#: it is literally named `0338_kokoro_names_the_maker_not_the_machines`.
FLEET_DISPLAY_NAME = "Studio Hub"

#: Separator used by every namespaced id in the gateway (`provider::thing`).
ID_SEPARATOR = "::"


def is_location_provider(provider_id: str) -> bool:
    """True for the local Hub and for every configured controller."""
    return controller_id_from(provider_id) is not None


def publish_id(provider_id: str) -> str:
    """The id Story Studio sees for a provider's models.

    Locations collapse into one namespace; everything else keeps its own provider
    id, because two cloud vendors genuinely offering the same model *are*
    different products.
    """
    return FLEET_PROVIDER_ID if is_location_provider(provider_id) else provider_id


def location_for_voice(voice_id: str) -> str | None:
    """Which location owns a voice, from the namespace its id already carries.

    A voice id is `studio-hub-<controller>::<raw>`, so the location that holds the
    library is already written into it. Voice work has to go to that location and
    no other: the reference clip exists on one Hub, not on the fleet.
    """
    provider_id, _, _ = (voice_id or "").partition(ID_SEPARATOR)
    return controller_id_from(provider_id)


#: Request params that can name the location a voice belongs to.
_VOICE_PIN_KEYS = ("voice_id", "voice_reference_source_id", "voice_library_id")


def pinned_location(params: dict) -> str | None:
    """The location a request already names, if it names one **that can take it**.

    A voice id carries its owner (`studio-hub-<controller>::<raw>`), so voice work
    belongs at the location holding the reference clip — but only when that location
    accepts customer work. A voice owned by an **agent-mode Hub** (this Mac, when it
    is a node of a controller) pins work to a site that answers `409`, which is not a
    route at all: measured live, a TTS job carrying a local voice resolved to a
    404 because the only candidate was the agent that had just been excluded.

    When the owner cannot take work the pin is dropped and the job routes to a
    controller that can; the adapter stages the reference clip there, which is the
    same cross-location path a voice owned elsewhere already uses. An owner the
    snapshot cannot classify keeps the pin, because misrouting a voice whose clip
    lives on one Hub is worse than a refusal that names the reason.
    """
    if not isinstance(params, dict):
        return None
    for key in _VOICE_PIN_KEYS:
        location_id = location_for_voice(str(params.get(key) or ""))
        if location_id:
            return location_id if _location_takes_customer_jobs(location_id) else None
    return None


def _location_takes_customer_jobs(location_id: str) -> bool:
    """Whether a named location accepts customer work, from the cached snapshot.

    The submit path already pays for one fleet read, so this must not add another.
    A location the snapshot does not know, or a snapshot that cannot be read, keeps
    the old behaviour (the pin holds) rather than silently sending voice work to a
    site that may not hold the clip.
    """
    try:
        rows = snapshot(allow_stale=True).get("locations") or []
    except Exception:  # noqa: BLE001 - a routing hint must never break a submit
        return True
    for row in rows:
        if str(row.get("id")) == location_id:
            return _takes_customer_jobs(row)
    return True

#: Per-request budget. A controller on the far end of a VPN is slower than
#: loopback, and a location that takes longer than this is reported unreachable
#: with the timeout as its reason rather than silently omitted.
_TIMEOUT = 6.0

#: The best-effort reads, each with what an absent answer degrades to. **Fetched
#: concurrently** -- see `_fetch_optional` for why that is not a micro-optimisation.
_OPTIONAL_PROBES: tuple[tuple[str, object], ...] = (
    ("/api/hub/fleet/nodes", {}),
    ("/api/hub/jobs", {"batches": []}),
    ("/api/hub/capabilities", {}),
    ("/api/hub/stats", {}),
    ("/api/hub/models", {}),
    ("/api/hub/workers", {}),
    ("/api/hub/model-packages", {}),
    ("/api/hub/model-health", {}),
    ("/api/hub/release", {}),
)


def _fetch_optional(client: "httpx.Client", base: str, token: str) -> dict:
    """Every `_OPTIONAL_PROBES` path, in parallel, each defaulting on failure.

    Sequentially these were the reason a busy Hub timed the page out. Every one
    carries the full per-request timeout, and none needs another's answer, so the
    cost should be the *slowest* rather than the sum. Measured across the owner's
    six locations: one snapshot took 22.7 s, against the app's 15 s request
    timeout -- so the page appeared only when the fleet was idle, which is exactly
    when nobody is looking at it.
    """
    out: dict = {}
    with ThreadPoolExecutor(max_workers=len(_OPTIONAL_PROBES)) as pool:
        pending = {
            pool.submit(_fetch, client, base, token, path): (path, empty)
            for path, empty in _OPTIONAL_PROBES
        }
        # `.result()` in submission order collects rather than serialises: every
        # future is already running by the time the first is awaited.
        for future, (path, empty) in pending.items():
            try:
                out[path] = future.result()
            except Exception:  # noqa: BLE001 - "best effort" is the contract
                out[path] = empty
    return out


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


def _set_token(controller_id: str, token: str, *, admin: bool = False) -> None:
    from .providers import registry
    registry.credentials.set_values(
        provider_id_for(controller_id), {"admin_token" if admin else "hub_token": token}
    )


def admin_token_for(controller_id: str) -> str:
    """A controller's Hub token, for the routes the fleet credential may not use.

    Separate from `_token_for` on purpose. Shared-voice administration is refused to
    a fleet credential by design — that separation is what stops a *sold node's*
    credential from administering the controller that owns it — so the gateway holds
    two values per controller and uses this one only for those routes. It is never
    used to dispatch work or read fleet state.
    """
    if controller_id == LOCAL_ID:
        try:
            # This Mac's own Hub token, which is the operator token by definition
            # because it is this machine's own Hub.
            return hub_token() or ""
        except Exception:  # noqa: BLE001
            return ""
    try:
        from .providers import registry
        return str(
            registry.credentials.resolved(provider_id_for(controller_id)).get("admin_token") or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def set_admin_token(controller_id: str, token: str) -> dict | None:
    """Store a controller's Hub token for shared-voice administration."""
    clean = (token or "").strip()
    if not clean:
        raise ValueError("A token is required.")
    if controller_id == LOCAL_ID:
        raise ValueError("This Mac administers its own Hub directly.")
    row = next((r for r in _read() if r.get("id") == controller_id), None)
    if row is None:
        return None
    _set_token(controller_id, clean, admin=True)
    invalidate()
    return _masked(row)


def _clear_token(controller_id: str) -> None:
    """Forget the credential entirely — the location it belonged to is gone."""
    try:
        from .providers import registry
        registry.credentials.forget(provider_id_for(controller_id))
    except Exception:  # noqa: BLE001
        pass


def _forget_models(controller_id: str) -> int:
    """Forget the cached models and the enable/disable decisions too.

    A credential is not the only thing a removed location leaves behind. Its
    discovered models and the owner's per-model choices survived in
    `models_cache.json`, so re-adding a location — or any id collision — would
    silently inherit decisions made about a machine that is no longer on the
    fleet. `fleet.json` currently holds no controllers while the cache still
    lists six `studio-hub-*` providers, which is exactly this leak.
    """
    try:
        from .providers import registry
        return registry.models.forget_provider(provider_id_for(controller_id))
    except Exception:  # noqa: BLE001
        return 0


#: The one route that tells a controller's two credentials apart.
#:
#: `/api/hub/settings` is operator-only: 200 for the Hub's own service token, 403
#: for a fleet credential. Every route this gateway actually calls answers 200 to
#: both, which is exactly why pasting the wrong one is silent.
_SCOPE_PROBE_PATH = "/api/hub/settings"


def credential_scope(base: str, token: str, *, timeout: float = _TIMEOUT) -> dict:
    """Classify a controller credential: `operator`, `fleet`, or `unknown`.

    Pasted credentials fail silently by design of the API: both kinds reach
    everything the gateway does, so an over-privileged one works perfectly until
    the day it matters. The operator token can change a controller's settings and
    identity, reach its storage, and shut it down — nothing the gateway ever needs.
    Saying which one was pasted turns that from a hope into a check.
    """
    if not base or not token:
        return {"scope": "unknown", "detail": "No address or credential to check."}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                base.rstrip("/") + _SCOPE_PROBE_PATH, headers={"X-Hub-Token": token}
            )
    except httpx.HTTPError as exc:
        return {"scope": "unknown", "detail": f"Could not check it: {type(exc).__name__}."}
    if response.status_code == 200:
        return {
            "scope": "operator",
            "detail": (
                "This is the controller's Hub token — the Mac's own key, not the work "
                "credential. It is sufficient but not least privilege: it can change that "
                "controller's settings and identity, reach its storage, and shut it down. "
                "Use the Fleet token instead."
            ),
        }
    if response.status_code == 403:
        # Authorised, but not for operator routes. That is what a fleet credential
        # looks like, and it is the answer we want.
        return {
            "scope": "fleet",
            "detail": "A valid Fleet token — the work badge, and the credential this gateway runs work with.",
        }
    if response.status_code == 401:
        # Measured against Hub 0.5.1: a wrong token and no token both answer 401,
        # where a valid fleet credential answers 403. Both are refusals, so they
        # must not be reported as the credential we are hoping for.
        return {
            "scope": "rejected",
            "detail": "The controller rejected this credential — it is not a valid token for it.",
        }
    return {"scope": "unknown", "detail": f"Unexpected HTTP {response.status_code}."}


def credential_scope_for(controller_id: str) -> dict:
    """Scope of a location's stored credential, local Mac included."""
    if controller_id == LOCAL_ID:
        try:
            return credential_scope(hub_base_url() or "", hub_token() or "")
        except Exception:  # noqa: BLE001 - a missing Hub is a state, not a crash
            return {"scope": "unknown", "detail": "Studio Hub is not running on this Mac."}
    row = next((r for r in _read() if r.get("id") == controller_id), None)
    if row is None:
        return {"scope": "unknown", "detail": "No such location."}
    return credential_scope(str(row.get("url") or ""), _token_for(controller_id))


def set_token(controller_id: str, token: str) -> dict | None:
    """Replace a location's credential; `None` when there is no such location.

    Separate from `add_controller`, which is keyed on the address. A credential
    changes for reasons that have nothing to do with where the machine is, and
    requiring the address again would mean re-typing something that did not change
    in order to fix something that did.
    """
    clean = (token or "").strip()
    if not clean:
        raise ValueError("A credential is required.")
    if controller_id == LOCAL_ID:
        raise ValueError("This Mac's credential comes from its own Hub, not from here.")
    row = next((r for r in _read() if r.get("id") == controller_id), None)
    if row is None:
        return None
    _set_token(controller_id, clean)
    invalidate()
    return _masked(row)


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


def _read_payload() -> dict:
    if not FLEET_PATH.exists():
        return {}
    try:
        payload = json.loads(FLEET_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


#: The reserved id for this Mac. It is listed in the Fleet page beside every
#: controller and can be renamed like one, but it is not a roster entry: there is
#: no address to configure and nothing to remove.
LOCAL_ID = "local"

#: What this Mac is called until the owner renames it.
LOCAL_LABEL_DEFAULT = "This Mac"


def local_label() -> str:
    return str(_read_payload().get("local_label") or "").strip() or LOCAL_LABEL_DEFAULT


def _read() -> list[dict]:
    rows = _read_payload().get("controllers")
    return [row for row in (rows or []) if isinstance(row, dict) and row.get("url")]


#: **Every read-modify-write of `fleet.json` holds this.** The roster is edited by the owner (rename,
#: address, add, remove, resume) and by the snapshot loop (reachability), on different threads, and
#: the snapshot used to write back the list it had read *before* a multi-second probe -- so an edit
#: that landed during the probe was reverted a few seconds later. Measured live 2026-09-30: a rename
#: answered 200, showed in the file, and was gone 3 s later. Re-entrant, because a caller that holds
#: it calls `_write`, which takes it again.
_ROSTER_LOCK = threading.RLock()


def _write(rows: list[dict], *, local_name: str | None = None) -> None:
    with _ROSTER_LOCK:
        _write_locked(rows, local_name=local_name)


def _write_locked(rows: list[dict], *, local_name: str | None = None) -> None:
    global _revision
    _revision += 1
    FLEET_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "controllers": [{k: v for k, v in row.items() if k != "token"} for row in rows],
    }
    # This Mac's name lives in the same file, because "what do I call my
    # locations" is one concern whether the location is this Mac or a controller
    # across town. Passing `local_name` sets it; omitting it preserves whatever is
    # already there, so adding or removing a controller cannot reset the local
    # name as a side effect.
    name = (local_name if local_name is not None else local_label()).strip()
    if name and name != LOCAL_LABEL_DEFAULT:
        payload["local_label"] = name
    # The roster carries no secrets, but it is written 0600 anyway: which sites
    # this gateway talks to is nobody else's business on a shared Mac.
    # Written beside the roster and renamed over it, so a reader on another thread sees the old file
    # or the new one and never a half-written one (which reads as an empty roster).
    scratch = FLEET_PATH.with_name(FLEET_PATH.name + ".tmp")
    fd = os.open(str(scratch), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(scratch, FLEET_PATH)
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
        # The controller's own stable identity, when it has been probed. Published
        # so two rows that are really one controller can be told apart by
        # something other than the address they happen to answer on.
        "site_id": row.get("site_id"),
        "token_set": bool(token),
        "token_hint": (token[:4] + "…" + token[-4:]) if len(token) > 12 else ("set" if token else ""),
        "added_at": row.get("added_at"),
    }


def _validate_address(url: str) -> str:
    """Return a usable address, or raise with what is wrong with it.

    Anything was accepted before this: `http://7` and `http://not a url` both became
    real locations, complete with credentials and a place in every probe. A junk
    location is not harmless — it is polled on every fleet read, it publishes voice
    rows, and it has to be found and deleted by hand.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("An address is required.")
    if not candidate.startswith(("http://", "https://")):
        candidate = "http://" + candidate
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("An address must start with http:// or https://.")
    if not parsed.netloc:
        raise ValueError("That address has no host in it.")
    if any(character.isspace() for character in parsed.netloc):
        raise ValueError("An address cannot contain spaces.")
    if ":" in parsed.netloc:  # a port was given; it has to be a port
        port = parsed.netloc.rsplit(":", 1)[1]
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError(f"'{port}' is not a valid port.")
    if not parsed.hostname:
        raise ValueError("That address has no host in it.")
    _require_private_host(parsed.hostname, parsed.port)
    return candidate.rstrip("/")


#: Address ranges a controller may answer on. **A Fleet token goes to whatever is typed here**,
#: and the token is a real allowlist — it can submit work to a controller and read its results.
def _is_private_address(address: Any) -> bool:
    import ipaddress

    return bool(
        address.is_loopback or address.is_private or address.is_link_local
        or address in ipaddress.ip_network("100.64.0.0/10")  # CGNAT, which is Tailscale
    )


def _require_private_host(host: str, port: int | None) -> None:
    """Refuse to send a credential to a public host (audit H13).

    The Hub has refused a non-private *node* address for exactly this reason since it gained a
    fleet (`fleet_nodes.validate_address`); the Gateway did not, so a mistyped or pasted public
    hostname received a location's Fleet token in `X-Hub-Token` on the very next probe. A
    controller is on the owner's own network — a Tailscale address, a LAN address, or this Mac —
    and nothing else is a location.

    **A hostname is allowed if, and only if, every address it resolves to is private.** Tailscale's
    MagicDNS names (`mac.tailnet.ts.net`) are the reason: the owner's live roster uses Tailscale
    *literals* (`100.76.125.18`), but a name is a reasonable thing to type and refusing all names
    would break a working setup. Resolution happens once, at add/re-point time — never on the probe
    path — and a name that resolves to nothing is refused rather than accepted on faith.
    """
    import ipaddress
    import socket

    text = str(host or "").strip().strip("[]")
    try:
        literal = ipaddress.ip_address(text)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_private_address(literal):
            raise ValueError(
                f"{text} is a public address. A controller answers on your own network — a "
                f"Tailscale address, a LAN address, or this Mac — because a Fleet token is sent "
                f"to it.")
        return
    try:
        resolved = socket.getaddrinfo(text, port or 0, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(
            f"'{text}' could not be resolved ({type(exc).__name__}), so it is not a controller "
            f"this gateway can reach.") from exc
    addresses = {info[4][0] for info in resolved}
    if not addresses:
        raise ValueError(f"'{text}' resolved to no addresses.")
    public = sorted(a for a in addresses if not _is_private_address(ipaddress.ip_address(a)))
    if public:
        raise ValueError(
            f"'{text}' resolves to {', '.join(public)}, which is public. A controller answers on "
            f"your own network — a Tailscale address, a LAN address, or this Mac — because a "
            f"Fleet token is sent to it.")


#: Studio Hub's default `site_id` for a Hub that was never assigned one
#: (`components/hub/backend/control_plane.py::defaults`). **Every** such Hub answers
#: this same string, so it is not an identity: matching a new controller on it
#: collapsed every freshly added location onto the first row, and "Add a location"
#: replaced an existing site instead of appending. The Hub's `controller_id` is
#: per machine (`<hostname>-hub`), so it is used whenever the site id is the default.
_DEFAULT_SITE_ID = "local-site"


def _stable_identity(probed: dict) -> str:
    """The controller's own id, preferring a real site over the Hub's shared default.

    A controller that has been assigned a `site_id` keeps it — that is what
    survives a new address. A Hub that still answers the default has no such
    identity, so its per-machine `controller_id` stands in; without that, two
    different controllers read as one and the second add overwrites the first.
    """
    site_id = str(probed.get("site_id") or "").strip()
    controller_id = str(probed.get("controller_id") or "").strip()
    if site_id and site_id != _DEFAULT_SITE_ID:
        return site_id
    return controller_id or site_id


def add_controller(label: str, url: str, token: str) -> dict:
    """Remember a location controller. Re-adding the same *controller* updates it.

    **A location is identified by the controller, not by where it answers.** This
    matched on the address alone, so a controller that moved (a new DHCP lease, a
    different Tailscale name, a rebuilt machine) minted a *second* location and
    orphaned the first one's credential and every per-model enable decision with
    it. The Hub states a stable `site_id` on `/api/hub/capabilities`, which
    `_probe` already reads, so the probe is asked first and the address is only the
    fallback — for an unreachable controller, or a first add with no token yet.
    """
    url = _validate_address(url)
    clean_token = (token or "").strip()
    # Best-effort: a probe failure is a normal state (the controller is switched
    # off, or the token was not pasted), never an error for the add itself.
    stable_id = ""
    if clean_token:
        try:
            probed = _probe(url, clean_token)
        except Exception:  # noqa: BLE001 - the address is still a usable answer
            probed = {}
        stable_id = _stable_identity(probed)
    with _ROSTER_LOCK:
        return _add_controller_locked(url, label, clean_token, stable_id)


def _add_controller_locked(url: str, label: str, clean_token: str, stable_id: str) -> dict:
    # Read **after** the probe above: the roster this edits must be the one on disk now.
    rows = _read()
    existing = None
    if stable_id:
        existing = next(
            (row for row in rows if str(row.get("site_id") or "") == stable_id), None
        )
    if existing is None:
        existing = next((row for row in rows if row.get("url") == url), None)
    created = existing is None
    if existing:
        existing["label"] = (label or existing.get("label") or url).strip()
        existing["url"] = url
        if stable_id:
            existing["site_id"] = stable_id
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
        if stable_id:
            existing["site_id"] = stable_id
        rows.append(existing)
        legacy = ""
    secret = clean_token or legacy or _token_for(controller_id)
    # The roster is written **first**: the provider definition that carries this
    # location's `hub_token` credential field is derived from the roster, and the
    # credential store refuses a field for a provider it has never heard of. So
    # the location has to exist before its secret can.
    _write(rows)
    if secret:
        _set_token(controller_id, secret)
    invalidate()
    row = _masked(existing)
    # **Whether this location is new**, published on the add result only (the
    # roster list stays shaped as it was). The enable seeding reads it: a fresh
    # location inherits this gateway's own decisions, but re-adding a location
    # that already carries the owner's choices — a new address, a re-pasted
    # token — must never overwrite them.
    row["created"] = created
    return row


def seed_enabled_from_local(provider_id: str) -> dict:
    """Seed a new location's enable map from this gateway's own Hub decisions.

    Adding a location left every model it discovered switched off
    (`auto_enable_discovered=False` in `providers/registry.py`, which is right
    for a cloud vendor's 300-row catalogue). A **location** is not that: its
    models are the same packages this Mac's own Hub already runs, so the owner
    ticked a model once, believed the fleet offered it, and every other Mac went
    on not serving it — the audit's "each remote controller has 5 of 19
    enabled".

    So the decision already recorded for a model here is the decision a newly
    added location inherits, **in both directions**:

      * the local Hub has the model **enabled** — enable it for the new one;
      * the local Hub has it **disabled** — record that decision too, so the two
        agree rather than one of them silently defaulting;
      * the local Hub has **never seen it** — nothing is written. An absent
        decision is not a decision, and the new location keeps its own default,
        which for every remote provider is off.

    The counts returned are the decisions actually applied, because the add
    result's sentence is built from them and a sentence that is not computed
    from what was written is a claim about the fleet nobody measured.
    """
    from .providers import registry

    source_provider_id = provider_id_for(LOCAL_ID)
    incoming = {
        str(model.get("model_id") or ""): bool(model.get("enabled"))
        for model in registry.models.models_for(source_provider_id, include_disabled=True)
    }
    inherited_on: list[str] = []
    inherited_off: list[str] = []
    unknown: list[str] = []
    for model in registry.models.models_for(provider_id, include_disabled=True):
        model_id = str(model.get("model_id") or "")
        if not model_id:
            continue
        if model_id not in incoming:
            unknown.append(model_id)
        elif incoming[model_id]:
            inherited_on.append(model_id)
        else:
            inherited_off.append(model_id)

    # One write per direction, so "enabled" and "disabled" are told apart in the
    # store exactly as they were decided here — not all written as one value and
    # re-derived later.
    if inherited_on:
        registry.models.set_many(provider_id, inherited_on, True)
    if inherited_off:
        registry.models.set_many(provider_id, inherited_off, False)

    discovered = len(inherited_on) + len(inherited_off) + len(unknown)
    if not discovered:
        # Discovery succeeded and found nothing. "0 of 0 models enabled" is a
        # fraction nobody can act on, and it reads as a decision when there was
        # nothing to decide.
        sentence = "No models were discovered here, so there was nothing to inherit."
    else:
        sentence = (
            f"{len(inherited_on)} of {discovered} models enabled here, from this Mac's Hub's "
            f"own decisions; {len(inherited_off)} are off there too"
        )
        if unknown:
            sentence += (
                f", and {len(unknown)} the local Hub has never seen "
                f"{'stays' if len(unknown) == 1 else 'stay'} off"
            )
        sentence += "."
    return {
        "provider_id": provider_id,
        "source_provider_id": source_provider_id,
        "models_discovered": discovered,
        "enabled": len(inherited_on),
        "disabled": len(inherited_off),
        "no_decision": len(unknown),
        "sentence": sentence,
    }


def reconcile_cached_locations() -> list[str]:
    """Drop cached models belonging to locations that are no longer on the roster.

    Removal already forgets a location's models as it goes, but the cache
    accumulated before that existed: `fleet.json` holds no controllers while
    `models_cache.json` still lists six `studio-hub-*` providers, one of which
    also carried a switched-on transcription model. Inert today — the catalogue is
    built by walking the live definitions, so an orphan publishes nothing — but it
    is a standing invitation for a stale decision to be inherited.

    **Conservative by construction**, because the failure mode of a wrong sweep is
    losing live locations' models: it only ever considers ids under this module's
    own `studio-hub-` prefix, and it compares against `registry.definitions()`,
    which is built from the roster file on disk. If the live set cannot be read it
    does nothing at all rather than treating "I could not read it" as "there is
    nothing".
    """
    try:
        from .providers import registry
        live = {d.id for d in registry.definitions()}
    except Exception:  # noqa: BLE001
        return []
    if not live:
        # An empty live set means the lookup failed, not that every provider was
        # deleted. Never act on it.
        return []
    try:
        cached = registry.models.cached_provider_ids()
    except Exception:  # noqa: BLE001
        return []
    orphans = [
        provider_id
        for provider_id in cached
        if provider_id.startswith(PROVIDER_PREFIX) and provider_id not in live
    ]
    for provider_id in orphans:
        try:
            registry.models.forget_provider(provider_id)
        except Exception:  # noqa: BLE001
            pass
    return orphans


def remove_controller(controller_id: str) -> bool:
    with _ROSTER_LOCK:
        rows = _read()
        keep = [row for row in rows if row.get("id") != controller_id]
        if len(keep) == len(rows):
            return False
        _write(keep)
    # The location is gone, so its token must not linger in the store.
    _clear_token(controller_id)
    # Nor its models, nor which of them the owner had switched on.
    _forget_models(controller_id)
    invalidate()
    return True


def replace_address(controller_id: str, url: str, token: str) -> dict | None:
    """Point an existing location at a new address, **keeping its identity and its decisions**.

    A3 step 4. When a controller is replaced by promoting one of its own nodes, the new head has a
    different address *and* a different controller id — so adding it creates a **second** location
    and orphans the first one's credential and every per-model enable decision with it. A
    rehearsal measured exactly that: after the handover the replacement row's `model_id` was null
    and `locations_enabled` was null, because it was a new row that had inherited nothing.

    This is deliberately not `add_controller` with a hint. Adding and re-pointing answer different
    questions, and the difference is the whole point: **adding** discovers what a controller offers,
    **re-pointing** says "the Mac that was here is now there". The row id is kept on purpose — it
    is the provider id Story's saved presets and the operator's enable decisions are keyed on.
    """
    url = _validate_address(url)
    clean_token = (token or "").strip()
    with _ROSTER_LOCK:
        rows = _read()
        row = next((r for r in rows if str(r.get("id")) == str(controller_id)), None)
        if row is None:
            return None
        previous_url = str(row.get("url") or "")
        row["url"] = url
        # **The site id is cleared, not guessed.** It named the *old* controller's identity, and
        # the Mac now answering is a different one; keeping it would make two Macs share an
        # identity — the defect `_stable_identity` exists to prevent. The next probe fills in the
        # new one.
        row.pop("site_id", None)
        row["repointed_at"] = time.time()
        row["repointed_from"] = previous_url or None
        _write(rows)
    if clean_token:
        _set_token(str(controller_id), clean_token)
    invalidate()
    return _masked(row)


def rename_controller(controller_id: str, label: str) -> dict | None:
    """Give a location a new name; `None` when there is no such location.

    A name is a **display name, never an identity**. Renaming touches neither the
    controller id, its address, its token, nor whether it is enabled, so it is
    safe at any moment -- including after that location's models have been
    discovered and enabled. Nothing published, stored or scheduled is keyed on
    the label.
    """
    clean = (label or "").strip()
    if not clean:
        raise ValueError("A name is required.")
    with _ROSTER_LOCK:
        if controller_id == LOCAL_ID:
            _write(_read(), local_name=clean)
            invalidate()
            return {"id": LOCAL_ID, "label": clean, "url": "", "local": True}
        rows = _read()
        row = next((r for r in rows if r.get("id") == controller_id), None)
        if row is None:
            return None
        row["label"] = clean
        _write(rows)
    invalidate()
    return _masked(row)


# --------------------------------------------------------------------------
# Reading a location


def _fetch(client: httpx.Client, base: str, token: str, path: str) -> dict:
    """Fetch one controller route and insist on a JSON *object*.

    Both halves matter for `_probe`'s "never raises" promise. A hub that answers
    200 with a non-JSON body — an HTML error page from a proxy, a truncated
    response — raised out of `json()`; a body that parses as a list or a bare
    `null` got through and then blew up on the first `.get()`. `_probe` catches
    only httpx errors, so both reached the routing path, which put a 500 in front
    of a generation request. Every caller here expects an object, so anything
    else is a failed read, reported as one.
    """
    headers = {"X-Hub-Token": token} if token else {}
    response = client.get(base.rstrip("/") + path, headers=headers)
    response.raise_for_status()
    try:
        parsed = response.json()
    except ValueError as exc:
        raise httpx.HTTPError(f"{path} answered 200 with a body that is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise httpx.HTTPError(
            f"{path} answered with a JSON {type(parsed).__name__}, expected an object"
        )
    return parsed


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
            # The canonical id, new in 0.6.48: the same string a model row, a
            # supply row and a health row carry.
            "model_id": batch.get("model_id"),
            # Per-item failure text, new in 0.6.48 — the batch's `error` is a count,
            # so this is the only place the list route says *what* failed.
            "errors": batch.get("errors"),
            "label": batch.get("label"),
            "total": batch.get("total"),
            "queued": batch.get("queued"),
            "running": batch.get("running"),
            "retrying": batch.get("retrying"),
            "done": batch.get("done"),
            # **The Hub's own `error` is a count, not a sentence.** Measured live:
            # `"error": 0` on a finished batch alongside `"queued"/"running"`. Read
            # as text it decoded to nothing, so the page's "Needs attention" card
            # was blank on exactly the batches it exists for. The reason a batch is
            # held is `governor_note`; the per-item failure text lives at
            # `/api/hub/jobs/{batch_id}`, a read this page does not make.
            "error": batch.get("error"),
            "stalled": batch.get("stalled"),
            "stalled_after_s": batch.get("stalled_after_s"),
            "no_progress_s": batch.get("no_progress_s"),
            "next_retry_at": batch.get("next_retry_at"),
            "uncertain": batch.get("uncertain"),
            "cancel_requested": batch.get("cancel_requested"),
            "cancelled": batch.get("cancelled"),
            "cancelled_items": batch.get("cancelled_items"),
            "routing": batch.get("routing"),
            "governor_note": batch.get("governor_note"),
            "avg_s": batch.get("avg_s"),
            "processing_elapsed_s": batch.get("processing_elapsed_s"),
            "started_at": batch.get("created_at"),
            "last_activity_at": batch.get("last_activity_at"),
        })
    live.sort(key=lambda row: row.get("started_at") or 0)
    return live


def _is_probe_machine(machine: dict) -> bool:
    """Whether a machine the Hub lists is one of its own connectivity probes.

    Named `probe-<something>` and labelled "probe" by the Hub. Matched on the id
    rather than the label: a label is the owner's to change, and this must not
    start hiding a real machine because someone called it "probe".
    """
    return str(machine.get("machine") or "").startswith("probe-")


def _hub_version_relation(version: object) -> str | None:
    """How a controller's Hub version compares to the one this build targets.

    Imported inside the function on purpose: `adapters` loads
    `providers.definitions`, which loads *this* module for the fleet provider ids,
    so a module-level import here is a circular import — the same reason every
    other cross-module read in this file is local.
    """
    from .adapters.studio_hub import hub_version_relation

    return hub_version_relation(version)


#: The last roster a controller published, per location: `(at, nodes, node_summary)`.
#:
#: **A cluster that is down still has members.** Check 10 of the fleet rehearsal: take one
#: controller's Hub down and the Gateway correctly routes elsewhere and keeps the model — but its
#: Fleet page showed *nothing* about that site's Macs, because the roster only ever came from the
#: live probe. "This house is offline" and "this house has no Macs" then read the same, which is
#: exactly the distinction the owner needs when a site loses power. In-process on purpose: it is a
#: display of what we last knew, not a durable record, and it disappears with the process rather
#: than becoming a second source of truth about a machine.
_last_members: dict[str, tuple[float, list[dict], dict]] = {}
_MEMBERS_STALE_AFTER_S = 120.0


def _remember_members(controller_id: str, nodes: Any, summary: Any) -> None:
    """Keep the last non-empty roster a controller published."""
    if not isinstance(nodes, list) or not nodes:
        return
    _last_members[str(controller_id)] = (
        time.time(), [dict(row) for row in nodes if isinstance(row, dict)],
        dict(summary) if isinstance(summary, dict) else {},
    )


def _last_known_members(controller_id: str) -> dict:
    """What this Gateway last knew about a location's members, and when.

    Published **only** when the location is not answering, and clearly labelled: `members_from` is
    the timestamp of the last successful read and `members_stale_seconds` its age, so a stale count
    is never mistaken for a live one. A live read replaces it and the keys disappear.
    """
    remembered = _last_members.get(str(controller_id))
    if not remembered:
        return {}
    at, nodes, summary = remembered
    return {
        "nodes": nodes,
        "node_summary": summary,
        "members_from": at,
        "members_stale_seconds": max(0.0, time.time() - at),
    }


def _probe(base: str, token: str, *, timeout: float = _TIMEOUT) -> dict:
    """Read one controller. Never raises: a failure *is* the result."""
    started = time.time()
    location: dict = {
        "reachable": False,
        "error": None,
        "role": None,
        "version": None,
        # How that version compares to the one this build targets; `None` until a
        # Hub answers, and `None` again if it refuses to state a version.
        "hub_version_relation": None,
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
        # The site's own admission numbers, which are what routing needs. Kept
        # because it is already fetched: dropping data this module has paid for is
        # how the routing layer ended up with nothing to route on.
        "capacity": {},
        "latency_ms": None,
        # Additive data for the Fleet page's Models and Machines tabs. Empty on an
        # unreachable controller rather than absent, because "no data" and "no
        # models" are different facts and the page has to tell them apart.
        "local_models": [],
        "workers": [],
        # `/api/hub/workers` read raw. The rich roster the page shows is
        # capabilities' `workers[]` (see `_worker_roster`); this endpoint's rows
        # carry no studio or physical-machine identity, so it is kept whole
        # rather than trimmed into a second, weaker worker shape.
        "hub_workers": [],
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            overview = _fetch(client, base, token, "/api/hub/fleet/overview")

            # **The nine enrichment reads, all at once.** Nothing below needs
            # anything else below, so fetching them in sequence -- which is what
            # this did -- made each pay the full per-request timeout one after the
            # other. On a Hub that is up but busy rendering, that is ~22 s for one
            # snapshot against the app's 15 s timeout, so the Fleet page loaded
            # only when the fleet was idle.
            optional = _fetch_optional(client, base, token)
            nodes = optional["/api/hub/fleet/nodes"]
            jobs = optional["/api/hub/jobs"]
            capabilities = optional["/api/hub/capabilities"]
            stats = optional["/api/hub/stats"]
            models_payload = optional["/api/hub/models"]
            workers_payload = optional["/api/hub/workers"]
            packages_payload = optional["/api/hub/model-packages"]
            health_payload = optional["/api/hub/model-health"]
            release_payload = optional["/api/hub/release"]

            # Why each of the nine is best-effort rather than required, kept
            # beside the read it explains:
            #
            # * `fleet/nodes` -- the *second* thing to go wrong on a Hub that is
            #   up: measured live, a Hub mid-rebuild answered `fleet/overview`,
            #   `capabilities`, `stats` and `settings` with 200 while
            #   `fleet/nodes` alone returned 500. Requiring it made that location
            #   read as entirely offline, hiding six working machines behind one
            #   broken route.
            # * `jobs` -- the least important part of "is this location alive"; a
            #   controller that reports machines but not jobs is still up.
            # * `capabilities` -- the authoritative identity: `controller` carries
            #   role, Hub version and readiness, plus the stable `site_id`. This
            #   replaced a derivation from `fleet/nodes.role`, which was returning
            #   a constant "controller" for a standalone Mac.
            # * `stats` -- the Hub's own fleet analytics, and the only place
            #   per-machine state (working / ready / idle / offline) comes from.
            # * `models`, `workers`, `model-packages` -- the per-model/per-worker
            #   supply the Models tab reads, and the `dispatch_state` readiness
            #   gate the gateway's model withholding reads.
            # * `model-health` -- the Hub's per-(machine, model) verdict. New in
            #   0.6.48; a 403 from an older Hub is one more empty list.
            # * `release` -- **what this controller actually holds**, read back
            #   rather than the push it was sent. Absence is `null`: a Hub with no
            #   release route answered nothing, which is not an empty release.
            #
            # Each degrades to its own empty value in `_OPTIONAL_PROBES`; one
            # broken route must never make a whole location read as offline.
    except httpx.HTTPStatusError as exc:
        location["error"] = (
            "Rejected the Hub token (HTTP %d)." % exc.response.status_code
            if exc.response.status_code in (401, 403)
            else "HTTP %d from the controller." % exc.response.status_code
        )
        return location
    except httpx.ConnectError:
        # Nothing accepted the connection. On a fleet this is the loopback-bind
        # signature far more often than a dead machine: Studio Hub defaults to
        # `listenHost: 127.0.0.1`, so a controller that is running perfectly well
        # refuses every connection from here. Naming that is the difference
        # between a one-minute settings change and a drive to a site to discover
        # the machine was never broken. This exact mistake is in
        # `docs/fleet-deployment-notes.md`.
        location["error"] = (
            "Nothing is listening on that address. If Studio Hub is running there, it is "
            "answering that Mac only — open Studio Hub, stop its services, tick its Tailscale "
            "network under \"Listening on\", and start them again. Confirm the port is 47873."
        )
        return location
    except httpx.TimeoutException:
        # Routed, but silent: a different problem from a refusal, with a different fix.
        location["error"] = (
            f"No answer within {int(_TIMEOUT)}s. The address is reachable but nothing replied — "
            "check the machine is awake and that Tailscale is up at both ends."
        )
        return location
    except httpx.HTTPError as exc:
        location["error"] = f"{type(exc).__name__}: {exc}" or "No response from the controller."
        return location

    node_rows = nodes.get("nodes") or []
    activity = stats.get("fleet_activity") or {}
    controller = capabilities.get("controller")
    controller = controller if isinstance(controller, dict) else {}
    # Studio Hub lists its own connectivity probes as machines. They are not
    # hardware: this Mac has one machine and the Hub reported three — `local` plus
    # `probe-fleet` and `probe-hub`, both labelled "probe" and permanently
    # unreachable. Counting them inflates the offline figure the owner asked this
    # page for, and at six sites it would read as ~12 broken machines when nothing
    # is wrong. They are dropped here, from one place, so the counts, the machine
    # table and the routing capacity all agree; the number hidden is reported so
    # nothing disappears silently.
    all_machines = [m for m in (overview.get("machines") or []) if isinstance(m, dict)]
    real_machines = [m for m in all_machines if not _is_probe_machine(m)]
    activity_machines = [m for m in (activity.get("machines") or []) if isinstance(m, dict)]
    real_activity = [m for m in activity_machines if not _is_probe_machine(m)]
    hidden = len(all_machines) - len(real_machines)

    summary = dict(overview.get("summary") or {})
    if hidden:
        # Recomputed from the machines that remain rather than adjusted from the
        # Hub's totals: subtracting the probe count would assume what state they
        # were in, and `machine_summary.reachable` is what the router divides by.
        reachable = sum(1 for m in real_machines if m.get("reachable"))
        summary.update({
            "total": len(real_machines),
            "reachable": reachable,
            "unreachable": len(real_machines) - reachable,
            "probe_entries_hidden": hidden,
        })

    dispatch = _dispatch_index(packages_payload)
    local_models = _local_models(models_payload, dispatch)

    location.update({
        "reachable": True,
        # Prefer the Hub's own identity; fall back to the older sources only when
        # it is unavailable, so a Hub that does not serve capabilities still works.
        "role": controller.get("role") or nodes.get("role"),
        "version": controller.get("studiohub_version") or _version_from(overview),
        # **How that version compares to the one this gateway targets.** Read from
        # the same string, in the same `update`, so the number and its verdict can
        # never describe different releases. `None` when the Hub stated none.
        "hub_version_relation": _hub_version_relation(
            controller.get("studiohub_version") or _version_from(overview)
        ),
        "site_id": capabilities.get("site_id") or controller.get("site_id"),
        "controller_id": controller.get("controller_id"),
        "ready": controller.get("ready"),
        "drained": controller.get("drained"),
        "machines": real_machines,
        "machine_summary": summary,
        "probe_machines_hidden": hidden,
        "nodes": node_rows,
        "node_summary": nodes.get("summary") or {},
        # **A live roster is remembered, so a later outage can still say who is there.**
        # Check 10: a cluster that loses power must read as *down with N members*, not as empty.
        # The keys published beside a live read (`members_from`, `members_stale_seconds`) appear
        # only when the location is not answering, so a stale count is never mistaken for a live
        # one and a live read carries no staleness at all.
        "members_from": None,
        "members_stale_seconds": None,
        "running_jobs": _running_jobs(jobs.get("batches") or []),
        "activity": {
            "pulse": activity.get("pulse") or {},
            "machines": real_activity,
            "observed_at": activity.get("observed_at"),
        },
        "by_machine": stats.get("by_machine") or {},
        "matrix": stats.get("matrix") or [],
        "jobs_total": stats.get("total"),
        "capacity": capabilities.get("capacity") or {},
        # Per-model physical capacity, which the router reads so a model is
        # weighted by the machines that can run **it**, not by every machine at the
        # site (see `routing_candidates`). The Hub's own reduction when it publishes
        # one; otherwise the same counts reduced from its per-machine model rows.
        "model_supply": _model_supply_index(
            capabilities.get("model_supply") or [],
            _supply_from_local_models(local_models),
        ),
        # The Models tab: one trimmed row per (model, machine), with the installed
        # package's `dispatch_state` joined in.
        "local_models": local_models,
        # The Hub's own per-(machine, model) verdict, with its reason. The Models
        # tab shows this instead of assembling a story from four other fields.
        "model_health": _model_health_rows(health_payload),
        # The app release this controller holds, read from the controller itself.
        # `None` is "it holds none" or "it has no release route" — the page draws
        # both the same way rather than inventing a version.
        "release": (
            release_payload.get("release")
            if isinstance(release_payload.get("release"), dict)
            else None
        ),
        # The publisher fingerprint this controller has installed, when its Hub
        # reports it (0.6.51+). `reported` is separate from the fingerprint so
        # "it says it trusts nobody" and "it did not tell us" stay distinct.
        "trusted_publisher": _trusted_publisher(release_payload),
        # The Machines tab: capabilities' worker roster, falling back to the
        # node list's studios.
        "workers": _worker_roster(capabilities, node_rows),
        "hub_workers": (
            workers_payload.get("workers")
            if isinstance(workers_payload.get("workers"), list)
            else []
        ),
        "latency_ms": int((time.time() - started) * 1000),
    })
    return location


def _trusted_publisher(payload: Any) -> dict[str, Any]:
    """A controller's installed release trust, from its own `/api/hub/release` answer.

    `reported` keeps the two absences apart: Hub 0.6.51 answers
    `trusted_publisher: null` when it trusts nobody (the state that makes a push
    answer 409), while a controller with no such field answered nothing at all.
    Both used to look identical once flattened to `None`.
    """
    if not isinstance(payload, dict) or "trusted_publisher" not in payload:
        return {"reported": False, "publisher_id": None, "fingerprint": None}
    value = payload.get("trusted_publisher")
    if not isinstance(value, dict):
        return {"reported": True, "publisher_id": None, "fingerprint": None}
    publisher_id = value.get("publisher_id")
    fingerprint = value.get("fingerprint")
    return {
        "reported": True,
        "publisher_id": publisher_id if isinstance(publisher_id, str) else None,
        "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
    }


def _version_from(overview: dict) -> str | None:
    for machine in overview.get("machines") or []:
        if machine.get("version"):
            return str(machine["version"])
    return None


def _dispatch_index(payload: object) -> dict[str, object]:
    """`package_id` -> `dispatch_state`, from the Hub's installed packages.

    `/api/hub/model-packages` is where readiness lives; `/api/hub/models` names
    the package a model row belongs to inside its `cache` block. One read per
    location, joined here so the page never walks the package list per model.
    """
    index: dict[str, object] = {}
    if not isinstance(payload, dict):
        return index
    rows = payload.get("models") or payload.get("packages") or payload.get("items") or []
    if not isinstance(rows, list):
        return index
    for row in rows:
        if not isinstance(row, dict):
            continue
        package_id = str(row.get("package_id") or "").strip()
        if package_id:
            index[package_id] = row.get("dispatch_state")
    return index


def _local_models(payload: object, dispatch: dict[str, object]) -> list[dict]:
    """One trimmed row per (model, machine) the controller publishes.

    A controller answers `/api/hub/models` with **one row per machine per model**
    (measured live: 55 models x 4 machines = 220 rows), so this is a supply list,
    not a model list. Every key is published as `None` when the Hub did not state
    it, never as a plausible default.
    """
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cache = row.get("cache") if isinstance(row.get("cache"), dict) else {}
        package_id = str(cache.get("package_id") or row.get("package_id") or "").strip()
        fit = row.get("fit")
        out.append({
            "model": row.get("repo"),
            # The Hub's canonical join key, added in 0.6.48: a model row, a
            # `model_supply` row, a model-health row and a batch all carry this
            # same string. `model` (the repo) is kept because it is what this
            # gateway publishes and dispatches, and on 0.6.48 the two agree.
            "model_id": row.get("model_id"),
            "label": row.get("label"),
            "family": row.get("family"),
            "studio": row.get("hub_studio"),
            "modality": row.get("hub_modality"),
            "machine": row.get("hub_machine"),
            "kind": row.get("kind"),
            "size_gb": row.get("size_gb"),
            "min_unified_memory_gb": row.get("min_unified_memory_gb"),
            "required_free_memory_gb": row.get("required_free_memory_gb"),
            "memory_eligible": row.get("memory_eligible"),
            "runtime_ready": row.get("runtime_ready"),
            "available": row.get("available"),
            "loaded": row.get("loaded"),
            "fit": fit if isinstance(fit, dict) else None,
            "cache_state": cache.get("state"),
            "catalog_error": row.get("hub_catalog_error"),
            "catalog_stale": row.get("hub_catalog_stale"),
            "active_download": bool(row.get("active_download")),
            "gated": row.get("gated"),
            "dispatch_state": dispatch.get(package_id) if package_id else None,
        })
    return out


#: A worker names its studio (`voice`, `image`, `subtitle`, `music`) but the
#: capabilities roster carries **no** `modality` field at all — measured live, so
#: reading one published `null` for every worker and the Machines tab lost the one
#: thing it groups by. `subtitle` is the studio whose modality is `transcription`,
#: which is the same pairing `_HUB_OPERATION_BY_MODALITY` uses.
_STUDIO_MODALITY = {
    "voice": "voice",
    "image": "image",
    "subtitle": "transcription",
    # The capabilities roster names the Subtitle service's studio `transcription`,
    # while the node roster names it `subtitle`. Both are the same studio, and both
    # spellings are mapped so neither roster loses its modality.
    "transcription": "transcription",
    "music": "music",
    # The Video worker registers its studio as `video`, the same word its modality uses.
    "video": "video",
}


def _studio_modality(studio: object, stated: object = None) -> object:
    """The modality a worker serves: what it states, else its studio's mapping.

    `None` when neither is known — never a guessed modality, because the Machines
    tab groups by this and a wrong group is a wrong answer about which Macs can do
    the work.
    """
    if stated:
        return stated
    return _STUDIO_MODALITY.get(str(studio or "").strip().lower())


def _model_health_rows(payload: object) -> list[dict]:
    """Per-(machine, model) health with the Hub's own reason, one row each.

    Published by Hub 0.6.48 as `{"models": [{model_id, machine, state, reason,
    hint, observed_at}]}` with a closed `state` vocabulary. Read verbatim: a
    missing value stays `None`, because "the Hub did not say" and "nothing is
    wrong" must not render the same — the same rule every other field follows.
    """
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "model_id": row.get("model_id"),
            "machine": row.get("machine"),
            "state": row.get("state"),
            "reason": row.get("reason"),
            "hint": row.get("hint"),
            "observed_at": row.get("observed_at"),
        })
    return out


def _worker_models(row: dict) -> list[str]:
    """The model ids a worker serves, from its own catalogue evidence.

    Hub 0.6.48 publishes this (it was empty through 0.6.47, which is *why*
    `model_supply` was empty — the supply view is built from it). Each entry is an
    object naming `internal_model_id`; a bare string is accepted too, so a Hub that
    trims the shape keeps working.
    """
    models = row.get("models")
    if not isinstance(models, list):
        return []
    out: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            model_id = str(entry.get("internal_model_id") or entry.get("model_id") or "").strip()
        elif isinstance(entry, str):
            model_id = entry.strip()
        else:
            continue
        if model_id:
            out.append(model_id)
    return out


def _worker_operations(row: dict) -> list[str]:
    operations = row.get("supported_operations")
    if not isinstance(operations, list):
        return []
    return [str(operation) for operation in operations if str(operation).strip()]


def _trim_worker(row: object) -> dict | None:
    """One capabilities worker, trimmed to the keys the Fleet page draws."""
    if not isinstance(row, dict):
        return None
    capacity = row.get("available_capacity")
    capacity = capacity if isinstance(capacity, dict) else {}
    return {
        "id": row.get("service_id"),
        "studio": row.get("studio_type"),
        "modality": _studio_modality(row.get("studio_type"), row.get("modality")),
        "machine": row.get("physical_machine_id"),
        "online": row.get("online"),
        "ready": row.get("ready"),
        "busy": row.get("busy"),
        "drained": row.get("drained"),
        "maintenance": row.get("maintenance"),
        "quarantined": row.get("machine_quarantined"),
        "slots": capacity.get("slots"),
        "slots_total": capacity.get("slots_total"),
        "last_seen_at": row.get("last_seen_at"),
        # What this worker actually serves, from the worker's own catalogue — the
        # answer to "which workers have this model", not an approximation by studio.
        "models": _worker_models(row),
        "supported_operations": _worker_operations(row),
    }


def _workers_from_nodes(nodes: object) -> list[dict]:
    """The fallback roster for a Hub whose capabilities carry no `workers[]`.

    A node names its studios, so one worker per (node, studio) is the same
    granularity the capabilities roster uses. Only `online` is answerable from a
    node: `ready`, `busy`, quarantine and slot counts belong to capabilities and
    stay `None` rather than being invented.
    """
    out: list[dict] = []
    if not isinstance(nodes, list):
        return out
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("machine") or node.get("name") or "")
        online = node.get("state") == "reachable"
        studios = node.get("studios")
        if not isinstance(studios, list):
            continue
        for studio in studios:
            if not isinstance(studio, dict):
                continue
            out.append({
                "id": f"{node_id}:{studio.get('id')}",
                "studio": studio.get("id"),
                "modality": _studio_modality(studio.get("id"), studio.get("modality")),
                "machine": node_id,
                "online": online,
                "ready": None,
                "busy": None,
                "drained": None,
                "maintenance": None,
                "quarantined": None,
                "slots": None,
                "slots_total": None,
                "last_seen_at": None,
                # A node names studios, not a catalogue, so there is nothing to
                # invent here.
                "models": [],
                "supported_operations": [],
            })
    return out


def _worker_roster(capabilities: object, nodes: object) -> list[dict]:
    """The Hub's workers, preferring the rich capabilities roster.

    `/api/hub/capabilities` states each worker's studio, physical machine,
    readiness and quarantine. `fleet/nodes` only gives a node's studios, so it is
    the fallback for a Hub that does not publish capabilities — and a
    capabilities roster that really is empty is kept as the answer rather than
    silently replaced by the weaker one.
    """
    if isinstance(capabilities, dict) and isinstance(capabilities.get("workers"), list):
        return [
            worker
            for worker in (_trim_worker(row) for row in capabilities["workers"])
            if worker is not None
        ]
    return _workers_from_nodes(nodes)


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
        local.update({
            "id": LOCAL_ID, "label": local_label(), "url": local_base, "local": True,
            # **The provider id this location is managed under.** A dashboard row
            # carries both identities and they are not the same string: the
            # location is `local`, and the provider whose models it offers is
            # `studio-hub-local`. Anything that wants to act on a location's models
            # — "Refresh" on the Fleet page, say — needs this, and deriving it from
            # the label or by prefixing in the client is how a label becomes an
            # identity.
            "provider_id": provider_id_for(LOCAL_ID),
        })
    else:
        local = {
            "id": LOCAL_ID, "label": local_label(), "url": "", "local": True,
            # Published here too, and deliberately: the provider identity does not
            # depend on the Hub answering. A page that can only name the provider of
            # a location that is up cannot offer to refresh the one that is down.
            "provider_id": provider_id_for(LOCAL_ID),
            "reachable": False,
            "error": local_error or "Studio Hub is not running on this Mac.",
            "role": None, "version": None, "hub_version_relation": None,
            "machines": [], "machine_summary": None,
            "nodes": [], "node_summary": None, "running_jobs": [],
            "activity": {"pulse": {}, "machines": [], "observed_at": None},
            "by_machine": {}, "matrix": [], "jobs_total": None, "latency_ms": None,
            "site_id": None, "controller_id": None, "ready": None,
            "capacity": {}, "local_models": [], "workers": [], "hub_workers": [],
            "model_health": [],
            "release": None,
            "probe_machines_hidden": 0,
        }
    rows.append(local)

    configured = _read()
    if not configured:
        _attach_admission(rows)
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
        controller_id = str(row.get("id") or "")
        reachable_now = bool(result.get("reachable"))
        if reachable_now:
            # Remember what a live controller said about its members, so a later outage can still
            # name them (check 10 — a powered-down house must not look like an empty one).
            _remember_members(controller_id, result.get("nodes"), result.get("node_summary"))
        else:
            last_known = _last_known_members(controller_id)
            if last_known:
                result.update(last_known)
        # **The pause clock** (Q7): stamped on a change of reachability, so a probe loop does not
        # rewrite the roster every few seconds, and persisted so a gateway restart does not reset
        # a week-long outage to zero.
        # `last_reachable_at` is stamped on every reachable probe, so it goes in the comparison in
        # five-minute steps (it is only ever read as "has this been seen up at all"): otherwise
        # the roster was rewritten every probe, ~every 10-30 s, for a timestamp nobody reads.
        def marks(item: dict) -> tuple:
            return (bool(item.get("paused_at")), bool(item.get("unreachable_since")),
                    int((item.get("last_reachable_at") or 0) // 300))

        before = marks(row)
        note_reachability(row, reachable_now)
        if before != marks(row):
            _persist_reachability(row)
        result["unreachable_since"] = row.get("unreachable_since")
        result["paused_at"] = row.get("paused_at")
        result["pause_reason"] = row.get("pause_reason")
        result.update({
            "id": row.get("id"),
            "label": row.get("label") or row["url"],
            "url": row["url"],
            "local": False,
            # Read from the controller id, which is what the provider id is derived
            # from — not from the label, which the owner may rename at any time.
            "provider_id": provider_id_for(str(row.get("id") or "")),
        })
        rows.append(result)
    _attach_admission(rows)
    return rows


#: What the snapshot may write back to a roster row: its reachability, and nothing else.
_REACHABILITY_KEYS = ("paused_at", "pause_reason", "unreachable_since", "last_reachable_at")


def _persist_reachability(row: dict) -> None:
    """Merge one probed row's reachability into the roster **as it is on disk now**.

    The snapshot holds a list it read before probing, and probing takes seconds; writing that list
    back reverted any rename, address change or removal made in the meantime. So it re-reads under
    the lock and copies over only the reachability fields of the row it probed. A row that was
    removed while the probe ran stays removed.
    """
    with _ROSTER_LOCK:
        fresh = _read()
        target = next((r for r in fresh if str(r.get("id")) == str(row.get("id"))), None)
        if target is None:
            return
        for key in _REACHABILITY_KEYS:
            if key in row:
                target[key] = row[key]
            else:
                target.pop(key, None)
        _write(fresh)


def _attach_admission(rows: list[dict]) -> None:
    """Give every location the number admission will actually use, per lane.

    **A1's live receipt.** The Hub's own `available_worker_slots` and the budget this
    gateway computes from it are two different numbers — the site's eligible total (queue
    ahead, 0.2.82), then `min` with the operator's cap — and a reader who can only see the first cannot
    tell a site running at half its Macs from one that is genuinely full. Publishing both,
    side by side, is what makes the arithmetic checkable from outside the process.

    The in-flight count comes from the live worker when there is one. A snapshot taken
    with no worker (a unit test, a script) publishes the same budget with `in_flight: 0`
    and says so, rather than implying the site is idle.
    """
    try:
        from . import concurrency
    except Exception:  # noqa: BLE001 - a budget is not worth failing a fleet read over
        return
    live_worker = None
    try:
        from .runtime import worker as live_worker  # noqa: PLC0415 - cycle, by design
    except Exception:  # noqa: BLE001
        live_worker = None
    for row in rows:
        provider_id = str(row.get("provider_id") or "")
        reachable = row.get("reachable") is not False
        lanes: dict[str, dict] = {}
        for modality, operation in HUB_OPERATION_BY_MODALITY.items():
            reading = concurrency.hub_slot_reading(row, operation)
            free = reading.get("free")
            if free is None and not reading.get("unstated"):
                free = reading.get("ceiling")
            in_flight = 0
            if live_worker is not None:
                try:
                    in_flight = live_worker._site_lane_in_flight(provider_id, modality)
                except Exception:  # noqa: BLE001
                    in_flight = 0
            budget = concurrency.hub_budget(
                free,
                operation=operation,
                stated=concurrency.stated_provider_limit(provider_id),
                ceiling=reading.get("ceiling"),
                reachable=reachable,
            )
            entry = budget.as_dict()
            entry.update({
                "operation": operation,
                "hub_free": reading.get("free"),
                "hub_total": reading.get("ceiling"),
                "hub_unit": reading.get("unit"),
                "in_flight": in_flight,
            })
            lanes[modality] = entry
        row["admission"] = {"lanes": lanes}


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

#: **One fan-out at a time, process-wide.** A stampede of readers arriving on a
#: stale cache used to start a fan-out each, so N readers meant N sweeps of every
#: controller — and the slowest site's timeout, paid N times over. The poller and
#: any revalidation now share this lock.
#: **Re-entrant, and that is load-bearing.** A refresh reaches
#: `_attach_admission`, which imports `runtime` to find the live worker -- a lazy
#: import that exists to break a cycle. That import constructs the `Catalog`,
#: whose `rebuild()` asks `usable_location_ids()` and therefore calls back into
#: `snapshot()`. A plain `Lock` deadlocked the entire read on the first refresh
#: after boot: the outer call held it and the inner one waited for a lock its own
#: thread already owned. Measured with `faulthandler`, stuck in `snapshot` ->
#: `usable_location_ids` -> `catalog.rebuild` -> `_attach_admission` -> ... ->
#: `snapshot`. An `RLock` lets the same thread through -- which is what happened
#: before this lock existed -- while still serialising *other* threads, and that
#: stampede is the thing being prevented.
_refresh_lock = threading.RLock()

#: Set while a background revalidation is in flight, so a stale read starts one
#: rather than one per reader.
_revalidating = threading.Event()


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
    # Machine counts come from the Hub's own per-machine states, which are the only
    # place that distinguishes "ready" from "long idle" from "offline".
    # `fleet/overview` only knows reachable/unreachable, so it is the fallback.
    #
    # **Both headline numbers come from the table the page draws.** They used to be
    # counted separately: `online` was "every pulse state except `offline`" — which
    # counted `unknown` (a *missing* state, coerced) as reachable and therefore
    # overstated the fleet — while `machines` came from the activity list, which is
    # not the union of overview + activity that the table shows. So a machine with
    # no state read as online, and the two numbers could disagree about how many
    # machines there are at all.
    #
    # Now: an unknown state is counted as **neither** online nor offline, and is
    # published as its own number rather than being dropped — a page that hides
    # rows without saying so is the same failure as one that invents them.
    flat = machines_flat(rows)
    states = [str(row.get("state") or "unknown").lower() for row in flat]

    # The published `pulse` is derived from the same filtered list, never taken
    # straight from the Hub's aggregate: that aggregate carries no machine
    # identity, so a probe counted in it could not be taken back out — which is
    # exactly how two phantom machines reached the headline once.
    derived_pulse: dict[str, int] = {}
    for state in states:
        derived_pulse[state] = derived_pulse.get(state, 0) + 1
    hub_pulse: dict[str, int] = {}
    for row in rows:
        for state, count in (row["activity"]["pulse"] or {}).items():
            hub_pulse[state] = hub_pulse.get(state, 0) + int(count or 0)
    pulse = derived_pulse or hub_pulse

    if states:
        online = sum(1 for state in states if state not in {"offline", "unknown"})
        offline = sum(1 for state in states if state == "offline")
        machines_unknown = sum(1 for state in states if state == "unknown")
        machines_total = len(states)
    else:
        # No per-machine report at all: nothing is claimed to be online.
        online, offline = 0, 0
        machines_total = machines_unknown = sum(len(row["machines"]) for row in rows)

    completed = failed = 0
    for row in rows:
        for machine in row["activity"]["machines"]:
            completed += int(machine.get("completed") or 0)
            failed += int(machine.get("failed") or 0)

    # The Models tab's headline numbers. **Both count a distinct model once per
    # location**, because the page draws them as one fraction — "N/M local models
    # available" — and a fraction whose numerator counts rows while its
    # denominator counts models is nonsense: measured live on 2026-09-24 it read
    # **175/102**, since the Hub repeats a model per machine (55 models x 4
    # machines) while `local_models_total` collapses to the distinct set. A model
    # counts here as available when **any** machine at that location can run it
    # now, which is the same claim the Models tab's per-model row makes.
    local_models_total = 0
    models_available = 0
    workers_total = 0
    workers_ready = 0
    for row in rows:
        local_models = [m for m in (row.get("local_models") or []) if isinstance(m, dict)]
        by_model: dict[object, bool] = {}
        for model in local_models:
            name = model.get("model")
            if name is None:
                continue
            by_model[name] = bool(by_model.get(name)) or model.get("available") is True
        local_models_total += len(by_model)
        models_available += sum(1 for available in by_model.values() if available)
        workers = row.get("workers") or []
        workers_total += len(workers)
        # Ready means what it says: answering, willing, and not under maintenance
        # or quarantine. A `None` readiness is not readiness.
        workers_ready += sum(
            1 for worker in workers
            if isinstance(worker, dict)
            and worker.get("online")
            and worker.get("ready")
            and not worker.get("maintenance")
            and not worker.get("quarantined")
        )

    startups = [row.get("startup") for row in flat]
    return {
        "locations": len(rows),
        "locations_up": sum(1 for row in rows if row["reachable"]),
        # **Which Macs will serve after a restart.** `off` is a Mac that said "Start services on
        # startup" is off (including one repeating its last word from before it went quiet),
        # `unknown` one that said nothing (an older Hub, or never heard from).
        "startup_off": sum(1 for fact in startups if fact and fact["start_services_on_launch"] is False),
        "startup_unknown": sum(1 for fact in startups
                               if not fact or fact["start_services_on_launch"] is None),
        "machines": machines_total,
        "machines_online": online,
        "machines_offline": offline,
        # Neither online nor offline: the Hub reported no state for it (or the
        # overview omitted it). Published so the headline can account for every
        # machine in the table instead of quietly leaving a gap.
        "machines_unknown": machines_unknown,
        "pulse": pulse,
        # Said out loud rather than silently dropped: a page that hides rows
        # without saying so is the same failure as one that invents them.
        "probe_machines_hidden": sum(
            int(row.get("probe_machines_hidden") or 0) for row in rows
        ),
        "jobs_completed": completed,
        "jobs_failed": failed,
        "running_jobs": sum(len(row["running_jobs"]) for row in rows),
        # The Fleet page's Models/Machines tabs.
        "local_models_total": local_models_total,
        "models_available": models_available,
        "workers_total": workers_total,
        "workers_ready": workers_ready,
        # Error + warning only: info issues are statements of fact (a downloading
        # model, a hidden probe) and must not make the fleet look wrong.
        "issues": sum(
            1 for row in rows for issue in _issues_for(row)
            if issue["severity"] in ("error", "warning")
        ),
    }


def _limitation_fields(entry: dict) -> dict:
    """The Hub's own reason for a machine's activity limitation, read verbatim.

    `limitation` is the sentence; 0.6.49 adds a closed `limitation_code`, a short
    `limitation_reason`, when the standing condition began (`limitation_since`), and
    which fields are consequently missing (`limitation_affects`). All are published
    as-is, because a null code under a null sentence is the Hub saying the machine
    reports **fully** — and a null median beneath that is "not measured yet", which
    is a different fact from "could not be measured".
    """
    return {
        "limitation": entry.get("limitation"),
        "limitation_code": entry.get("limitation_code"),
        "limitation_reason": entry.get("limitation_reason"),
        "limitation_since": entry.get("limitation_since"),
        "limitation_affects": entry.get("limitation_affects"),
    }


_DISK_STATES = ("green", "amber", "red", "stop")


def _num(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _disk_top(raw: object) -> list[dict]:
    """The Hub's biggest consumers as `[{name, gb}]`, at most five, whatever shape it sent."""
    pairs: list[tuple[str, float | None]] = []
    if isinstance(raw, dict):
        pairs = [(str(k), _num(v)) for k, v in raw.items()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("label") or item.get("path")
                gb = _num(item.get("gb", item.get("size_gb")))
                if gb is None and _num(item.get("bytes")) is not None:
                    gb = _num(item.get("bytes")) / 1024**3  # type: ignore[operator]
                if name:
                    pairs.append((str(name), gb))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((str(item[0]), _num(item[1])))
    return [{"name": n, "gb": None if g is None else round(g, 2)} for n, g in pairs[:5]]


def machine_disk(machine: dict) -> dict | None:
    """One Mac's disk, normalised from whatever the Hub sent. `None` when it said nothing.

    Reads the Hub's `disk` object defensively (state, free_gb, total_gb, trash_gb, top
    consumers, swap_used_gb, last_sweep, warnings); a Hub that predates it sends only
    `disk_free_gb` / `disk_total_gb`. `state` is `green|amber|red|stop` (`black` is the Hub's
    older word for `stop`). When the Hub states none but the sizes are known it is **derived**
    from the same thresholds the gateway uses for its own disk, and marked `state_source:
    "derived"` -- shown, but never acted on by routing.
    """
    raw = machine.get("disk")
    raw = raw if isinstance(raw, dict) else {}
    free = _num(raw.get("free_gb"))
    if free is None:
        free = _num(machine.get("disk_free_gb"))
    total = _num(raw.get("total_gb"))
    if total is None:
        total = _num(machine.get("disk_total_gb"))
    state = str(raw.get("state") or "").strip().lower()
    state = "stop" if state in {"black", "stopped"} else state
    source = "hub" if state in _DISK_STATES else None
    if source is None:
        state = ""
        if free is not None and total:
            from . import diskguard

            gb = diskguard.GB
            state = diskguard.state_of(int(total * gb), int(free * gb))
            if free * gb < diskguard.REFUSE_FREE_BYTES:
                state = "stop"
            source = "derived"
    if free is None and not raw:
        return None
    warnings = raw.get("warnings")
    if isinstance(warnings, (str, dict)):
        warnings = [warnings]
    clean = []
    for warning in warnings if isinstance(warnings, list) else []:
        text = warning.get("message") or warning.get("text") if isinstance(warning, dict) else warning
        if text and str(text).strip():
            clean.append(str(text).strip())
    swap = _num(raw.get("swap_used_gb"))
    trash = _num(raw.get("trash_gb"))
    return {
        "state": state or None,
        "state_source": source,
        "free_gb": None if free is None else round(free, 2),
        "total_gb": None if total is None else round(total, 2),
        "trash_gb": None if trash is None else round(trash, 2),
        "swap_used_gb": None if swap is None else round(swap, 2),
        "top": _disk_top(raw.get("top", raw.get("top_consumers"))),
        "last_sweep": raw.get("last_sweep", raw.get("last_sweep_at")),
        "warnings": clean,
    }


def _location_disk_stopped(row: dict) -> bool:
    """Every Mac at this location is **stated** `stop` by its Hub: nothing can be sent there.

    A derived state never counts, and a location with one Mac still answering is not stopped:
    the controller places the job on that Mac.
    """
    machines = [m for m in (row.get("machines") or []) if isinstance(m, dict)]
    if not machines:
        return False
    for machine in machines:
        disk = machine_disk(machine)
        if not disk or disk["state"] != "stop" or disk["state_source"] != "hub":
            return False
    return True


def disk_summary(machines: list[dict]) -> dict:
    """The compact fleet disk view: every Mac that reported disk, least free space first."""
    from . import diskguard

    macs = []
    for machine in machines:
        disk = machine.get("disk") if "state_source" in (machine.get("disk") or {}) else machine_disk(machine)
        if not disk:
            continue
        macs.append({
            "location": machine.get("location"),
            "location_id": machine.get("location_id"),
            "machine": machine.get("machine"),
            "label": machine.get("label") or machine.get("machine"),
            "reachable": machine.get("reachable"),
            **disk,
        })
    macs.sort(key=lambda m: (m["free_gb"] is None, m["free_gb"] if m["free_gb"] is not None else 0.0))
    low = [m for m in macs if m["state"] in ("amber", "red", "stop")]
    return {
        "generated_at": time.time(),
        "gateway": diskguard.status(),
        "macs": macs,
        "low": [m["label"] for m in low],
        "worst": max((m["state"] for m in macs if m["state"]),
                     key=_DISK_STATES.index, default=None),
    }


#: The only keys of a Mac's `startup` report this gateway republishes. The Hub is somebody else's
#: process, so what reaches the console is a fixed shape, never whatever the node chose to send.
_STARTUP_KEYS = ("open_at_login", "open_at_login_saved", "start_services_on_launch", "app_version",
                 "last_start_at", "app_reporting", "stale", "observed_at", "reported_at")


def _startup_fact(machine: dict, node: dict) -> dict | None:
    """What starts when this Mac starts, as the Hub reported it - or `None` when it did not.

    `None` is *unknown*, never "off": a Hub older than 0.6.127 says nothing about it, and so does
    a Mac nobody has heard from since. A report the controller is repeating from before the Mac
    went quiet carries `stale: true`. The two settings are published as the booleans the Hub sent;
    anything else becomes `None`.
    """
    raw = machine.get("startup") if isinstance(machine.get("startup"), dict) else node.get("startup")
    if not isinstance(raw, dict):
        return None
    out: dict = {key: raw.get(key) for key in _STARTUP_KEYS}
    for key in ("open_at_login", "open_at_login_saved", "start_services_on_launch", "app_reporting", "stale"):
        if not isinstance(out[key], bool):
            out[key] = None
    running = raw.get("services_running")
    out["services_running"] = (
        {"up": running.get("up"), "total": running.get("total"), "studios": running.get("studios") or {}}
        if isinstance(running, dict) else None)
    return out


def machines_flat(rows: list[dict]) -> list[dict]:
    """Every machine in the fleet as one row, with its location attached.

    A table is what scales: hundreds of machines as a flat list can be
    virtualised by the view, while hundreds of nested cards cannot. The join is
    done here, once, so the view never has to walk the tree.

    **A node's own sentence travels with its machine.** `/api/hub/fleet/nodes`
    states, per node, why the controller cannot reach it ("could not reach the
    node: All connection attempts failed") and whether it is `no_hub` or
    `unreachable`; the machine table drew neither, so eight dead nodes read as
    eight identical "offline" rows with nothing to act on. The join is on the
    node **id**, which is the machine id the overview uses — never the label,
    which is the owner's to change. A machine with no node row is the
    controller's own Mac, and the node fields stay `None` rather than borrowing
    the machine's own facts.
    """
    out: list[dict] = []
    for row in rows:
        activity = {entry.get("machine"): entry
                    for entry in row["activity"]["machines"] if entry.get("machine")}
        nodes = {
            str(node.get("id")): node
            for node in (row.get("nodes") or [])
            if isinstance(node, dict) and node.get("id")
        }

        def node_fields(mid: str) -> dict:
            node = nodes.get(mid) or {}
            return {
                "node_state": node.get("state"),
                "node_detail": node.get("detail"),
                # **Why** it is unreachable, when the Hub states it as a field. See
                # `_node_reach_reason` for the fallback that covers Hubs predating it.
                "node_reason": node.get("reason"),
                "node_role": node.get("role"),
                "node_last_seen_at": node.get("last_seen_at"),
                "node_address": node.get("address"),
            }

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
                # What is free *now*, which is the number the Hub's own admission
                # uses and the one a reader needs: `memory_gb` is the machine's
                # total, so "Apple M4 · 17.18 GB" beside "118.11 GB free" read as
                # though the memory were free too.
                "available_gb": machine.get("available_gb"),
                "disk_free_gb": machine.get("disk_free_gb"),
                "disk_total_gb": machine.get("disk_total_gb"),
                "disk": machine_disk(machine),
                "health": machine.get("health"),
                "modalities": machine.get("modalities") or {},
                **_limitation_fields(entry),
                **node_fields(mid),
                "startup": _startup_fact(machine, nodes.get(mid) or {}),
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
                "chip": None, "memory_gb": None, "available_gb": None,
                "disk_free_gb": None, "disk_total_gb": None, "disk": None, "health": None,
                "modalities": {},
                **_limitation_fields(entry),
                **node_fields(mid),
                "startup": _startup_fact({}, nodes.get(mid) or {}),
            })
    return out


def running_flat(rows: list[dict]) -> list[dict]:
    """Work in flight, as one row per batch, with its location attached."""
    out: list[dict] = []
    for row in rows:
        for job in row["running_jobs"]:
            out.append({
                # **The location's id and provider id travel with the row.** A
                # cancel has to name the controller whose Hub holds the batch, and
                # a label is not an identity — the page may rename it at any time.
                "location_id": row.get("id"),
                "provider_id": row.get("provider_id"),
                "location": row["label"],
                "location_local": row["local"],
                # The Hub's batch id: what a cancel addresses, and the only handle
                # on the upstream work this gateway did not itself create.
                "batch_id": job.get("id"),
                "modality": job.get("modality"),
                "model": job.get("model"),
                "model_id": job.get("model_id"),
                # The per-item failures: `[{item_index, code, message}]`. The text
                # the "Needs attention" card draws, and what a Retry addresses.
                "errors": job.get("errors"),
                "label": job.get("label"),
                "total": job.get("total"),
                "running": job.get("running"),
                "queued": job.get("queued"),
                "retrying": job.get("retrying"),
                "done": job.get("done"),
                "error": job.get("error"),
                "stalled": job.get("stalled"),
                "stalled_after_s": job.get("stalled_after_s"),
                "no_progress_s": job.get("no_progress_s"),
                "next_retry_at": job.get("next_retry_at"),
                "uncertain": job.get("uncertain"),
                "cancel_requested": job.get("cancel_requested"),
                "cancelled": job.get("cancelled"),
                "cancelled_items": job.get("cancelled_items"),
                "routing": job.get("routing"),
                "governor_note": job.get("governor_note"),
                "avg_s": job.get("avg_s"),
                "started_at": job.get("started_at"),
            })
    return out


#: Errors first, then warnings, then information.
_ISSUE_SEVERITY = {"error": 0, "warning": 1, "info": 2}


def _node_reach_reason(machine: dict) -> str:
    """Why a node did not answer, refusing to guess when the Hub does not say.

    **The field first.** Hub 0.6.113 publishes `reason` on an unreachable node, and that is
    the answer: `no_hub` for a refused TCP connection (the Mac is up, nothing listening on
    :47873) and `unreachable` for dropped packets (asleep, off, or firewalled).

    **Then the sentence's token, because it is the probe's own classification.** Controllers
    that predate the field state it in `node_detail` — `could not reach the node (no_hub)`.

    **Then the machine's `health`, which has carried the same two words since before the
    field existed** — the live fleet of 2026-09-24 had one controller saying `no_hub` and
    another `unreachable` in exactly that slot. Only those two tokens are accepted: any other
    health word is a different fault and must not be read as a reachability reason.

    ⚠ **The sentence outranks `health`, and the live fleet is why.** `terranash-1209` reports
    `health: unreachable` while its probe detail says `(no_hub)` — the host answered. Taking
    `health` first called that Mac switched-off when something had just replied to it. A
    `no_hub` is positive evidence the Mac is up; `unreachable` is the absence of evidence, so
    where the two disagree the positive one is the better answer.

    The sentence parse is a second copy of a vocabulary the Hub owns, which is exactly the
    kind of thing that drifts; it is confined to this one function so that deleting it, once
    every controller publishes the field, is a two-line change with no caller to update.

    **Neutral when none of them says.** An empty answer is not "unreachable": it is "nobody
    told us", and those must not render as the same advice.
    """
    stated = str(machine.get("node_reason") or "").strip().lower()
    if stated:
        return stated
    sentence = str(machine.get("node_detail") or "")
    match = re.search(r"\((no_hub|unreachable)\)\s*$", sentence.strip())
    if match:
        return match.group(1)
    health = str(machine.get("health") or "").strip().lower()
    if health in {"no_hub", "unreachable"}:
        return health
    return ""


def _offline_action(reason: str) -> str:
    """What to go and check, which is the opposite question for the two reasons.

    A machine whose Hub is not running needs Studio Hub started on it. A machine that never
    answered needs its power and its network checked — and the previous advice, *"Studio Hub
    on that Mac"*, sent the owner to the second of those looking at the first. Eight
    powered-off Macs read as a Hub fault because of it.
    """
    if reason == "no_hub":
        return "Start Studio Hub on that Mac"
    if reason == "unreachable":
        return "Check that Mac is switched on and awake"
    # Nothing stated it: say only what is certainly true.
    return "Check that Mac"


def _issue(
    severity: str, code: str, location_id: object, location: object,
    subject: str, message: str, action: str,
) -> dict:
    return {
        "severity": severity,
        "code": code,
        "location_id": location_id,
        "location": location,
        "subject": subject,
        "message": message,
        "action": action,
    }


def _issues_for(row: dict) -> list[dict]:
    """Everything visibly wrong with one location, in one list.

    A condition a Hub repeats on thousands of rows is aggregated: one machine
    issue per machine, one worker issue per worker, one catalogue-error issue per
    (location, model) however many machines repeat that model. **A fault that is
    only a consequence of another fault is not repeated at all** — a machine's
    health word and its node sentence belong to its offline row, and its workers
    and its unreachable models are counted there rather than listed again.
    """
    out: list[dict] = []
    location_id = row.get("id")
    location = row.get("label") or row.get("id") or "location"
    subject = str(location_id if location_id is not None else location)
    # A location-level issue is *about* the whole row, so its subject is the
    # address the page would otherwise make you go and find — a bare controller id
    # (`de7250e146a7`) next to the label is the same fact twice, and not the useful
    # one. Falls back to the id only when the Hub never gave an address.
    address = str(row.get("url") or "").strip() or subject

    if not row.get("reachable"):
        out.append(_issue(
            "error", "location_unreachable", location_id, location, address,
            # The location's own sentence: it already says what to do about it.
            row.get("error") or "This location is not answering.",
            "Fleet → Locations",
        ))

    if str(row.get("role") or "").lower() == "controller" and (
        row.get("ready") is False or _truthy(row.get("drained"))
    ):
        out.append(_issue(
            "warning", "controller_drained", location_id, location, address,
            "The controller reports itself drained and will not accept new work.",
            "Studio Hub on that Mac",
        ))

    # **A Hub on another version keeps working, and is named.** Studio Hub
    # auto-updates itself, so a fleet can move a version ahead of this gateway
    # without anyone touching it. Refusing would turn that into an outage; saying
    # nothing would hide the one fact that explains a lane this build has never
    # heard of. `info`, deliberately: it is a statement of fact, not a fault, and
    # `summary.issues` counts only errors and warnings.
    relation = str(row.get("hub_version_relation") or "")
    if row.get("reachable") and relation in {"older", "newer"}:
        from .adapters.studio_hub import SUPPORTED_STUDIO_HUB_VERSION

        version = str(row.get("version") or "an unstated version")
        if relation == "newer":
            detail = (
                f"This controller runs Studio Hub {version}; gateway build targets "
                f"{SUPPORTED_STUDIO_HUB_VERSION}. It keeps working — anything the newer "
                "Hub serves in a lane this gateway does not know is listed as "
                "unsupported rather than offered."
            )
        else:
            detail = (
                f"This controller runs Studio Hub {version}; gateway build targets "
                f"{SUPPORTED_STUDIO_HUB_VERSION}. It keeps working, but a route or field "
                "added after that release is not read here."
            )
        out.append(_issue(
            "info", "hub_version_drift", location_id, location, version, detail,
            "Fleet → Locations",
        ))

    # **A Hub condition repeated per machine is one issue, not one per machine.**
    # "Direct activity partially unavailable" is the Hub saying it could not read
    # that machine's activity — the same sentence on every machine at a site — and
    # `state: unknown` is the same kind of measurement gap. Emitted per machine
    # they buried the list under ten identical rows; aggregated they are one line
    # that says how many machines are affected, with the Hub's own wording kept
    # verbatim. A machine-specific fault (offline, unhealthy) still gets its own
    # row, because it names a machine someone can go and look at.
    #
    # **One fault, one row.** The Hub reports a dead machine three ways — its
    # activity state (`offline`), its health (`unreachable` / `no_hub`), and each
    # of its studios as an offline worker — and every one of them was a separate
    # row, so eight dead nodes produced 48 warnings and buried the two facts that
    # needed someone. The health word and the node's own sentence now ride on the
    # offline row (they are *why* it is offline, not further faults), and workers
    # on an already-offline machine are counted once instead of four times.
    #
    # **A node that answered and refused this controller is not an offline
    # machine.** The Hub gives it its own state — `unauthorized` — precisely so a
    # credential problem is not read as a network one, and it writes a sentence
    # naming the fix. Its machine still reads `offline` in the activity report,
    # because no work can be dispatched; reporting both would be the same fault
    # twice, with the actionable half second. So the unauthorized node keeps its
    # own row and the offline row for that machine is not also emitted.
    rejected_nodes = {
        str(node.get("id") or node.get("name") or ""): node
        for node in (row.get("nodes") or [])
        if isinstance(node, dict)
        and str(node.get("state") or "").strip().lower() == "unauthorized"
    }
    limitations: dict[tuple[str, str, str], dict] = {}
    unknown_state = 0
    offline_machines: set[str] = set()
    startup_off: list[str] = []
    for machine in machines_flat([row]):
        machine_id = str(machine.get("machine") or "")
        fact = machine.get("startup")
        if fact and fact.get("start_services_on_launch") is False:
            startup_off.append(str(machine.get("label") or machine_id))
        state = str(machine.get("state") or "").strip().lower()
        health = str(machine.get("health") or "").strip()
        unhealthy = bool(health) and health.lower() != "up"
        if state == "offline" and machine_id not in rejected_nodes:
            offline_machines.add(machine_id)
            detail = f"Machine {machine_id} is offline."
            if unhealthy:
                detail += f" The Hub reports its health as \u2018{health}\u2019."
            # The controller's own sentence, quoted: it distinguishes a machine
            # that never answered from one whose Hub is not running, and it is the
            # only thing that tells the owner what to go and check.
            node_detail = str(machine.get("node_detail") or "").strip()
            if node_detail:
                detail += f" {node_detail}"
            # **The action follows the reason, because the two answers are opposite.**
            # `no_hub` is a refused TCP connection — the Mac is up and its Studio Hub is
            # not running — so pointing at Studio Hub is right. `unreachable` is dropped
            # packets: the Mac is asleep, off, or firewalled, and telling the owner to fix
            # Studio Hub on a switched-off machine sends him to the wrong place. This
            # page used to say "Studio Hub on that Mac" for both, which is what made eight
            # powered-off Macs read as a Hub fault.
            out.append(_issue(
                "warning", "machine_offline", machine.get("location_id"),
                machine.get("location"), machine_id, detail,
                _offline_action(_node_reach_reason(machine)),
            ))
        elif not state or state == "unknown":
            unknown_state += 1
        # A health word on a machine that is *up* is its own fault and keeps its
        # own row; on an offline machine it is the same fault in a second
        # vocabulary, already carried above.
        if unhealthy and machine_id not in offline_machines \
                and machine_id not in rejected_nodes:
            out.append(_issue(
                "warning", "machine_not_healthy", machine.get("location_id"),
                machine.get("location"), machine_id,
                f"Machine {machine_id} reports health '{health}'.",
                "Studio Hub on that Mac",
            ))
        limitation = str(machine.get("limitation") or "").strip()
        if limitation:
            key = (
                limitation,
                str(machine.get("limitation_code") or ""),
                str(machine.get("limitation_reason") or ""),
            )
            bucket = limitations.setdefault(key, {"count": 0, "affects": set()})
            bucket["count"] += 1
            affects = machine.get("limitation_affects")
            if isinstance(affects, list):
                bucket["affects"].update(str(field) for field in affects)

    if rejected_nodes:
        for node_id, node in sorted(rejected_nodes.items()):
            name = str(node.get("name") or node_id)
            # **The Hub's own sentence, quoted.** It names the fix — re-join the
            # Mac with the current Fleet Token — and paraphrasing it would drop the
            # one actionable fact. A Hub that states no detail gets a sentence that
            # says only that, rather than invented advice.
            detail = str(node.get("detail") or "").strip() or (
                f"The controller marks node {name} unauthorized and stated no detail."
            )
            out.append(_issue(
                "error", "node_credential_rejected", location_id, location,
                node_id or name, detail, "Studio Hub on that Mac",
            ))

    if limitations:
        for (text, code, reason), bucket in sorted(limitations.items()):
            count = bucket["count"]
            machines_text = f"{count} machine" + ("" if count == 1 else "s")
            # The Hub's sentence stays verbatim and the 0.6.49 code and phrase are
            # added beside it, never instead of it: "Direct activity partially
            # unavailable" is what the owner sees on that Mac's own Hub, and the
            # code is what makes it groupable and reportable.
            detail = f"{machines_text}: {text}"
            if reason and reason.lower() not in text.lower():
                detail += f" — {reason}"
            if code:
                detail += f" ({code})"
            if bucket["affects"]:
                detail += "; missing " + ", ".join(sorted(bucket["affects"]))
            out.append(_issue(
                "warning", "machine_limited", location_id, location, machines_text,
                detail, "Fleet → Machines",
            ))
    if startup_off:
        # One line per location, not per Mac: the fix is the same button on every one of them.
        shown = ", ".join(sorted(startup_off)[:4]) + (" and more" if len(startup_off) > 4 else "")
        out.append(_issue(
            "warning", "services_not_auto_start", location_id, location,
            f"{len(startup_off)} machine" + ("" if len(startup_off) == 1 else "s"),
            f"{shown}: \"Start services on startup\" is off, so after a restart the Mac opens "
            "Studio Hub but serves nothing until someone starts the services by hand.",
            "Fleet → Machines → Enable auto-start",
        ))
    if unknown_state:
        machines_text = f"{unknown_state} machine" + ("" if unknown_state == 1 else "s")
        out.append(_issue(
            "info", "machine_unknown_state", location_id, location, machines_text,
            f"{machines_text} {'reports' if unknown_state == 1 else 'report'} no state. "
            "The Hub counts them as neither online nor offline, so they are left out of both.",
            "Fleet → Machines",
        ))

    # One issue per worker, keyed on (machine, worker) so a Hub that repeats a
    # worker cannot multiply the page.
    #
    # A worker whose machine is already reported offline is counted, not repeated:
    # the machine row says the machine is down, and "voice is offline, image is
    # offline, subtitle is offline, music is offline" is that one sentence four
    # times. A worker down on a machine that is **up** is a real, separate fault
    # and keeps its own row.
    seen_workers: set[tuple] = set()
    offline_workers: dict[str, int] = {}
    for worker in row.get("workers") or []:
        if not isinstance(worker, dict):
            continue
        worker_key = (worker.get("machine"), worker.get("id"))
        if worker_key in seen_workers:
            continue
        seen_workers.add(worker_key)
        worker_id = str(worker.get("id") or worker.get("machine") or "worker")
        machine_id = str(worker.get("machine") or worker_id)
        if _truthy(worker.get("quarantined")):
            out.append(_issue(
                "error", "machine_quarantined", location_id, location, machine_id,
                f"Machine {machine_id} is quarantined.", "Fleet → Machines",
            ))
        if worker.get("online") is False:
            if machine_id in offline_machines:
                offline_workers[machine_id] = offline_workers.get(machine_id, 0) + 1
            else:
                out.append(_issue(
                    "warning", "worker_offline", location_id, location, worker_id,
                    f"Worker {worker_id} is offline.", "Studio Hub on that Mac",
                ))
        if _truthy(worker.get("maintenance")):
            out.append(_issue(
                "warning", "worker_maintenance", location_id, location, worker_id,
                f"Worker {worker_id} is in maintenance.", "Studio Hub on that Mac",
            ))

    if offline_workers:
        workers_text = f"{sum(offline_workers.values())} worker" + (
            "" if sum(offline_workers.values()) == 1 else "s")
        machines_text = f"{len(offline_workers)} offline machine" + (
            "" if len(offline_workers) == 1 else "s")
        named = ", ".join(sorted(offline_workers))
        out.append(_issue(
            "warning", "worker_offline", location_id, location, workers_text,
            f"{workers_text} on {machines_text}: {named}. Each machine's own row "
            "says why it is offline.",
            "Fleet → Machines",
        ))

    # A model is repeated once per machine. Conditions are OR-ed across its
    # machines first, then reported once per model — never once per row, and never
    # lost because the machine that stated it came second.
    #
    # **A model that could not be checked because the machine serving it is off is
    # not a second fault.** `worker_unreachable` on an offline machine is the
    # offline machine again — live, 98 model names produced 98 warnings beside the
    # eight machine rows that explain all of them — so those rows are skipped. Any
    # other catalog error is kept, including on an offline machine: a `401` from a
    # node that answered is a different problem from a node that did not.
    model_conditions: dict[str, dict] = {}
    for model in row.get("local_models") or []:
        if not isinstance(model, dict):
            continue
        if (str(model.get("machine") or "") in offline_machines
                and str(model.get("catalog_error") or "").strip() == "worker_unreachable"):
            continue
        name = str(model.get("model") or "")
        bucket = model_conditions.setdefault(
            name, {"catalog_error": None, "active_download": False}
        )
        if model.get("catalog_error") and not bucket["catalog_error"]:
            bucket["catalog_error"] = model["catalog_error"]
        if model.get("active_download"):
            bucket["active_download"] = True
    for name, bucket in model_conditions.items():
        if bucket["catalog_error"]:
            out.append(_issue(
                "warning", "model_catalog_error", location_id, location, name,
                f"Model {name} failed its catalogue check: {bucket['catalog_error']}",
                "Fleet → Models",
            ))
        if bucket["active_download"]:
            out.append(_issue(
                "info", "model_downloading", location_id, location, name,
                f"Model {name} is downloading.", "Fleet → Models",
            ))

    # **The Hub's own verdict, where it is a fault.** `model_health` (0.6.48) is one
    # row per (machine, model). `ready` and `download_required` are normal states —
    # a model that is not downloaded is not broken — so only the states that need
    # someone are reported, aggregated per state so a small machine that cannot fit
    # twenty models is one line rather than twenty.
    fault_states = ("quarantined", "unsupported_os", "unsupported_chip", "runtime_missing")
    health_counts: dict[str, int] = {}
    for entry in row.get("model_health") or []:
        if not isinstance(entry, dict):
            continue
        state = str(entry.get("state") or "").strip().lower()
        if state in fault_states:
            health_counts[state] = health_counts.get(state, 0) + 1
    for state, count in sorted(health_counts.items()):
        models_text = f"{count} model" + ("" if count == 1 else "s")
        out.append(_issue(
            "warning", "model_health", location_id, location, models_text,
            f"{models_text} report {state.replace('_', ' ')} on at least one machine.",
            "Fleet → Models",
        ))

    for job in row.get("running_jobs") or []:
        if not isinstance(job, dict) or not job.get("stalled"):
            continue
        job_id = str(job.get("id") or job.get("model") or "job")
        out.append(_issue(
            "error", "running_stalled", location_id, location, job_id,
            f"Job {job_id} is stalled.", "Fleet → Jobs",
        ))

    hidden = int(row.get("probe_machines_hidden") or 0)
    if hidden > 0:
        out.append(_issue(
            "info", "probe_machines_hidden", location_id, location, str(hidden),
            f"{hidden} Hub connectivity probe(s) are hidden from the machine list.",
            "Fleet → Machines",
        ))
    return out


def _issues(rows: list[dict]) -> list[dict]:
    """Every location's issues, ordered error, warning, info.

    Stable inside a severity by location and subject, so a page that refreshes
    does not reshuffle rows whose facts did not change.
    """
    out = [issue for row in rows for issue in _issues_for(row)]
    out.sort(key=lambda issue: (
        _ISSUE_SEVERITY.get(issue["severity"], 3),
        str(issue.get("location") or ""),
        str(issue.get("subject") or ""),
        str(issue.get("code") or ""),
    ))
    return out


def snapshot(force: bool = False, *, allow_stale: bool = False) -> dict:
    """Everything the Fleet page shows, in one read.

    Served from a short-lived cache so that polling, the console and a test all
    reading at once does not multiply the load on every controller.

    `allow_stale` is for a caller that must not block: the catalogue needs to know
    which locations are answering, but a controller on the far side of a VPN takes
    the full per-request timeout to fail, and Story Studio polls the catalogue. A
    reachability answer up to one refresher interval old is worth far more there
    than a catalogue read that stalls for seconds.
    """
    # **Stale-while-revalidate, because the alternative is a page that times out.**
    #
    # The refresher already owns freshness -- that is what `start_refresher` says
    # it is for: *"the request path only ever reads a value that is already
    # there"*. It did not hold, and the arithmetic is why: `_CACHE_TTL` was 5 s
    # while the poller runs every 15 s, so the cache was stale two thirds of the
    # time and the request path did the fan-out itself. Measured on the owner's
    # fleet: **22.7 s** cold, against the app's **15 s** timeout. The Fleet page
    # could only load when nothing was running — which is exactly when nobody
    # needs it (`"this isn't showing. but when there's no job running. it did
    # show."`).
    #
    # A caller that did not say `force` gets the cached value and a background
    # refresh, so the read is O(1) whatever the fleet is doing. Only a cold cache
    # (nothing to serve at all) waits, and the refresher's first pass happens at
    # startup rather than after one interval, so that window is the boot only.
    now = time.time()
    with _cache_lock:
        cached = _cache["value"]
        age = now - _cache["at"]
    if not force and cached is not None:
        if allow_stale or age < _CACHE_TTL:
            return cached
        _revalidate_in_background()
        return cached

    # `force`, or nothing cached yet. **Serialised, and deduplicated even for
    # `force`**: `at > now` means a refresh finished *after this caller asked*,
    # so its answer is newer than the question and starting a second sweep would
    # only make the fleet do the same work twice. Eight concurrent Refresh
    # presses are one fan-out, not eight — measured at eight before this check
    # covered the forced path.
    with _refresh_lock:
        with _cache_lock:
            cached = _cache["value"]
            at = _cache["at"]
        if cached is not None and at > now:
            return cached
        return _refresh()


def _refresh() -> dict:
    """One fan-out over every location, cached on the way out."""
    rows = locations()
    value = {
        "locations": rows,
        "machines": machines_flat(rows),
        "running": running_flat(rows),
        "summary": _summarise(rows),
        "issues": _issues(rows),
        "generated_at": time.time(),
    }
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = value
    return value


def _revalidate_in_background() -> None:
    """Refresh a stale cache without making a reader wait for it.

    Started by a read, not by a timer, so a fleet nobody is looking at costs
    nothing -- and at most one runs, so attention on the page cannot turn into a
    stampede on the controllers.
    """
    if _revalidating.is_set():
        return
    _revalidating.set()

    def run() -> None:
        try:
            with _refresh_lock:
                _refresh()
        except Exception:  # noqa: BLE001 - a reader must never be failed by this
            pass
        finally:
            _revalidating.clear()

    threading.Thread(target=run, name="fleet-revalidate", daemon=True).start()


# --------------------------------------------------------------------------
# Routing: which location runs a job
#
# The global scheduler belongs here and the site-local one belongs to Studio Hub.
# The Hub says so itself: `/api/hub/capabilities` publishes
# `authority: {global: "genstudio", site_local_scheduler: "sqlite",
# global_job_claiming: false}`. So the gateway chooses a *site* and never a
# machine -- which machine has the memory for the model is machine-specific and
# is the controller's decision, not this module's.
#
# Choosing the *site* still has to respect the model: the Hub's `model_supply`
# says how many machines at each controller can actually run **it** (memory floor
# and all), so the weight is per model rather than per site. Setting
# `GATEWAY_PREFER_FASTEST_SITE=1` trades that capacity share for latency and ranks
# the fastest site first.


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


#: How long a controller may be unreachable before routing pauses it (Q7, the owner's answer:
#: *"Pause after 7 days, never auto-remove; visible per cluster"*).
#:
#: **Paused is not removed.** The row, its credential and every per-model enable decision stay
#: exactly as they are; routing simply stops offering it, and the Fleet page says why. A house
#: whose power is out for a week is a house you will switch back on, and re-adding a controller
#: costs a token paste and loses nothing else — which is precisely why an automatic *removal*
#: would be the wrong answer. Editable in the same owner file as the pool size:
#:
#:     {"locations": {"pause_unreachable_after_days": 7}}
PAUSE_AFTER_DAYS_DEFAULT = 7


def pause_after_days() -> int:
    """The owner's threshold, from the settings file, or the seven days he chose."""
    try:
        from . import concurrency

        entry = concurrency._read_file().get("locations")
        stated = concurrency._positive_int(
            (entry or {}).get("pause_unreachable_after_days") if isinstance(entry, dict) else None
        )
    except Exception:  # noqa: BLE001 - an unreadable setting is the default, not an outage
        stated = None
    return stated if stated is not None else PAUSE_AFTER_DAYS_DEFAULT


def note_reachability(row: dict, reachable: bool) -> None:
    """Remember when a location was last seen up, and pause it past the threshold.

    **This is the one thing about a location that must survive a restart**, because "unreachable
    for seven days" is a question about the calendar, not about this process: a gateway that
    restarted yesterday would otherwise reset every controller's clock and never pause anything.
    Written to `fleet.json` on a *change* of reachability only, so a probe loop does not rewrite
    the roster every few seconds.
    """
    now = time.time()
    if reachable:
        row["last_reachable_at"] = now
        row.pop("unreachable_since", None)
        row.pop("paused_at", None)
        return
    if not row.get("last_reachable_at"):
        # Never seen up: the clock starts at the first failed probe rather than being absent, or
        # a location added while its Mac is off could never pause.
        row["last_reachable_at"] = None
    row.setdefault("unreachable_since", now)
    limit = pause_after_days() * 86400
    if now - float(row.get("unreachable_since") or now) >= limit and not row.get("paused_at"):
        row["paused_at"] = now
        row["pause_reason"] = (
            f"unreachable since {time.strftime('%Y-%m-%d', time.localtime(row['unreachable_since']))}"
            f" — paused after {pause_after_days()} days. Routing skips it; nothing was removed. "
            f"Bring the Mac back, or resume it from the Fleet page."
        )


def resume_location(controller_id: str) -> dict | None:
    """Clear a pause and restart the clock, deliberately, without touching anything else."""
    with _ROSTER_LOCK:
        rows = _read()
        row = next((r for r in rows if str(r.get("id")) == str(controller_id)), None)
        if row is None:
            return None
        row.pop("paused_at", None)
        row.pop("pause_reason", None)
        row.pop("unreachable_since", None)
        _write(rows)
    invalidate()
    return _masked(row)


def _takes_customer_jobs(row: dict) -> bool:
    """Whether a location may be sent customer work at all.

    **An agent does not take customer jobs.** It runs workers for a controller;
    the Hub refuses a submit with `409 "This Hub is in agent mode; submit customer
    jobs to a controller."` — measured live on 2026-09-23, after the gateway routed
    both an image and a TTS job to this Mac's agent-mode Hub and both failed at the
    door while the two healthy controllers sat idle.

    `drained` is the Hub's own "stop sending me work" flag, set deliberately.
    A standalone Hub and a controller both take work; only these two states do not.
    `ready: false` is deliberately **not** a disqualifier — it is a controller
    starting up, and treating a transient as "cannot take work" would lock the
    catalogue during every restart.

    **A controller that has handed its group over is retired** (A3 step 4). It keeps its
    history, stops accepting dispatch, and the operator then removes it from the Gateway and
    wipes or sells it. The Hub publishes that decision itself as
    `capacity.accepting_customer_jobs: false`, so routing skips it in the window between the
    handover and the operator's cleanup — otherwise a retired Mac would keep receiving work that
    the new controller's cluster is meant to run.
    """
    if str(row.get("role") or "").strip().lower() == "agent":
        return False
    if str(row.get("handed_over_to") or "").strip():
        return False
    capacity = row.get("capacity") if isinstance(row.get("capacity"), dict) else {}
    if capacity.get("accepting_customer_jobs") is False:
        return False
    # **A paused location is skipped by routing** (Q7). The row is still there, its models are
    # still published, and every per-model decision survives — it simply stops being offered.
    if row.get("paused_at"):
        return False
    # **A location whose every Mac is out of disk (`stop`) is unavailable** -- it would only
    # answer 507 -- so routing re-routes to another location, as it does for a drained one.
    if _location_disk_stopped(row):
        return False
    return row.get("drained") is not True


def usable_location_ids() -> set[str]:
    """Locations that can actually take work right now, by id.

    Read once per catalogue rebuild rather than per row: this is the difference
    between one fleet read and one per model. An agent or a drained Hub is left
    out, so a model only they offer reads as unavailable rather than being
    published to Story Studio and then refused on use.
    """
    try:
        return {
            str(row.get("id"))
            for row in snapshot(allow_stale=True)["locations"]
            if row.get("reachable") and _takes_customer_jobs(row)
        }
    except Exception:  # noqa: BLE001 - a routing hint must never break the catalogue
        return set()


#: The Hub's operation names for this gateway's modalities, read from the Hub's own
#: `OPERATION_BY_MODALITY` table (`components/hub/backend/capabilities.py`). Used
#: only as the middle fallback, when a location publishes capacity per operation but
#: no per-model supply row.
#:
#: **One table, and now one copy.** It existed twice — here, and in `worker.py` for
#: admission's lookup — and the two had already drifted: this copy was missing `chat`,
#: so routing fell through to the site-wide total for a chat job while admission read
#: `chat.completion`. Admission and routing now compare the same field of the same
#: operation, which is the whole point of A1, so a second copy is a defect waiting to
#: reappear. `worker` imports this name.
HUB_OPERATION_BY_MODALITY = {
    "chat": "chat.completion",
    "image": "image.text_to_image",
    "music": "music.generation",
    "tts": "voice.tts",
    "transcription": "audio.transcription",
    "video": "video.generation",
}

#: The old private name, kept so a reader following a stale note still lands here.
_HUB_OPERATION_BY_MODALITY = HUB_OPERATION_BY_MODALITY


def _as_optional_int(value: object) -> int | None:
    """`None` stays `None`: "the Hub did not say" is not "zero"."""
    return None if value is None else _as_int(value)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _supply_from_local_models(local_models: object) -> dict[str, dict[str, int]]:
    """Per-model machine counts, reduced from the Hub's own per-machine rows.

    Hub 0.6.47 publishes `capabilities.model_supply` **empty** — measured against
    one agent and two controllers — while `/api/hub/models` states, per machine and
    model, whether that machine is `memory_eligible` and `available`. Counting
    those stated booleans is the same reduction `model_supply` describes, from the
    Hub's own evidence rather than a guess, so the router can weight by the
    machines that can run a **specific** model instead of falling back to the
    site-wide total.

    The model id is the Hub's `repo`, which is the id this gateway publishes and
    dispatches for a location (30 of 35 live published ids are Hub repos; the rest
    are companion models the Hub does not list as rows).

    A machine that states neither boolean is **not** counted: `None` is "unknown",
    which must not read as "cannot" — the same rule every other field follows.
    """
    supply: dict[str, dict[str, int]] = {}
    if not isinstance(local_models, list):
        return supply
    for row in local_models:
        if not isinstance(row, dict):
            continue
        # The Hub's canonical `model_id` is what `model_supply.internal_model_id`
        # and a batch both carry; `model` (the repo) is the fallback for a Hub that
        # predates the field. On Hub 0.6.48 the two are the same string here.
        model_id = str(row.get("model_id") or row.get("model") or "").strip()
        if not model_id:
            continue
        entry = supply.setdefault(model_id, {"eligible": 0, "available": 0})
        if row.get("memory_eligible") is True:
            entry["eligible"] += 1
        if row.get("available") is True:
            entry["available"] += 1
    return supply


def _model_supply_index(
    rows: object, fallback: dict[str, dict[str, int]] | None = None
) -> dict[str, dict[str, int]]:
    """The Hub's per-model physical capacity, keyed by the model id it names.

    `model_supply` is the Hub's own reduction of every worker's evidence, and it
    already answers the question the router used to approximate: how many
    *machines* can run **this** model. `eligible` counts the machines that clear
    the model's memory floor; `available` counts the ones ready with a free slot
    now. Both are read, never derived — and a machine that cannot fit the model is
    in neither, which is the whole point.

    **A Hub that publishes no `model_supply` at all is filled from its own
    per-machine model rows** (`fallback`), because that is the same evidence the
    Hub would have reduced. Hub 0.6.48 publishes the rows, so from there the
    published set is authoritative and the fallback is not merged in: a model the
    Hub deliberately left out is "unknown", and re-adding it from our own reduction
    would overrule a decision the Hub is better placed to make.
    """
    index: dict[str, dict[str, int]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("internal_model_id") or "").strip()
            if not model_id:
                continue
            index[model_id] = {
                "eligible": _as_int(row.get("eligible_physical_slots_total")),
                "available": _as_int(row.get("available_physical_slots")),
            }
    if not index and fallback:
        index = dict(fallback)
    return index


#: Apple chip generation out of the marketing brand string ("Apple M4 Pro").
_CHIP_GENERATION = re.compile(r"\bM([1-9])\b")


def _site_speed_score(machines: object) -> float:
    """The fastest Mac at a site, by the Hub's own chip+RAM proxy.

    Mirrors Studio Hub's `_studio_speed_score` (`generation * 100 + memory_gb`),
    which is itself a proxy for image throughput because nothing in the fleet
    measures seconds-per-image. Used only behind `GATEWAY_PREFER_FASTEST_SITE`;
    it ranks, it never admits or excludes.
    """
    best = 0.0
    if not isinstance(machines, list):
        return best
    for machine in machines:
        if not isinstance(machine, dict):
            continue
        match = _CHIP_GENERATION.search(str(machine.get("chip") or "").upper())
        generation = int(match.group(1)) if match else 0
        try:
            memory_gb = max(0.0, float(machine.get("memory_gb") or 0.0))
        except (TypeError, ValueError):
            memory_gb = 0.0
        best = max(best, generation * 100 + memory_gb)
    return best


def _weighted_tiebreak(routing_key: str, location_id: str, compatible_slots: int) -> float:
    """A weighted lottery draw: lower wins, and a bigger site wins more often.

    Ported from the old GenStudio's `MultiSiteAssignment._weighted_tiebreak`,
    including its reasoning. Capacity belongs in the *weight*, never as a rank
    term: sorting by raw size made a small site unreachable, while ignoring size
    gave every site an equal share and overloaded the small ones. Multiplying
    gives every healthy site a non-zero share and lets a ten-slot site take about
    ten times the work of a one-slot site.
    """
    digest = hashlib.sha256(f"{routing_key}:{location_id}".encode()).digest()
    unit = (int.from_bytes(digest, "big") + 1) / (2**256 + 1)
    return -math.log(unit) / max(compatible_slots, 1)


def routing_candidates(
    model_id: str, modality: str, *, only_location: str | None = None,
    exclude_locations: set[str] | None = None,
) -> list[dict]:
    """Every location that can run this model right now, with its pressure.

    A candidate must be a model the catalogue would **offer**, be answering, and
    -- when the request names a voice -- be the location that owns that voice.

    "Enabled" is not that question. This used to test `is_enabled` plus a row
    match, while `enabled_models` -- the set the catalogue is built from -- also
    applies `is_configured` and `can_publish`. The two answers drifted, and the
    failure was worse than an extra candidate: `route` chose a location on the
    weaker test and `catalog.resolve` then rejected that pair on the stronger
    one, so a request answered **`MODEL_NOT_FOUND`** — with a perfectly good copy
    of the same model enabled on another site, never tried. The candidate set is
    therefore read from `enabled_models`, not re-derived; asking "can this be
    offered?" in two places is how the two answers came apart.
    """
    from .providers import registry

    # Stale rather than blocking, for the same reason the catalogue reads it that
    # way: a controller that is switched off costs the full timeout to fail, and
    # that cost lands on the request that submits the work. A route picked from a
    # view up to one refresher interval old can be wrong about a site that died
    # seconds ago, and the job then settles as `failed` -- which is the outcome
    # this endpoint already prefers to making the caller wait.
    rows = {
        str(row.get("id")): row
        for row in (snapshot(allow_stale=True).get("locations") or [])
    }
    candidates: list[dict] = []
    for definition, model in registry.models.enabled_models(modality):
        location_id = controller_id_from(definition.id)
        if location_id is None:
            continue
        if model["model_id"] != model_id:
            continue
        if only_location is not None and location_id != only_location:
            continue
        if exclude_locations and location_id in exclude_locations:
            # Tried already and refused this model; the worker is looking for the
            # next site, not the same one again.
            continue
        row = rows.get(location_id) or {}
        if not row.get("reachable"):
            continue
        # A site that cannot accept customer work is not a candidate: an agent-mode
        # Hub answers 409 to the submit, and a drained one has been told to stop.
        if not _takes_customer_jobs(row):
            continue

        capacity = row.get("capacity") or {}
        # The exact model first: how many machines at this site can run **this**
        # model. The old site-wide total counted each 8 GB image Mac in the weight
        # for a 24 GB model it can never run, so a small-Mac site out-weighed a
        # big-Mac one and the heavy jobs serialised on its single fitting Mac.
        supply = (row.get("model_supply") or {}).get(model_id) or {}
        slots = _as_int(supply.get("eligible"))
        available = _as_optional_int(supply.get("available"))
        if slots <= 0:
            # The Hub's capacity per operation, for a location that publishes no
            # per-model supply row.
            operation = _HUB_OPERATION_BY_MODALITY.get(str(modality or ""))
            by_operation = (capacity.get("by_operation") or {}).get(operation or "") or {}
            slots = _as_int(by_operation.get("eligible_physical_machine_slots_total"))
            if available is None:
                available = _as_optional_int(by_operation.get("available_physical_machine_slots"))
        if slots <= 0:
            # Then the site-wide total, as before, so an older Hub is not penalised.
            slots = _as_int(capacity.get("eligible_physical_machine_slots_total"))
            if available is None:
                available = _as_optional_int(capacity.get("available_physical_machine_slots"))
        if slots <= 0:
            # A Hub that does not report capacity still reports machines. Fall back
            # to the reachable machine count so a site is never weighted zero.
            slots = _as_int((row.get("machine_summary") or {}).get("reachable"))
        queue = _as_int(capacity.get("queue_depth"))
        if not capacity:
            queue = len(row.get("running_jobs") or [])

        candidates.append({
            "provider_id": definition.id,
            "definition": definition,
            "location_id": location_id,
            "label": row.get("label") or location_id,
            "slots": max(slots, 1),
            # 0 is a real reading ("every fitting machine is busy"); None is "the
            # Hub did not say". The rank below treats them the same, but keeping
            # them distinct lets a reader tell them apart.
            "available": available,
            "queue": max(queue, 0),
            "speed": _site_speed_score(row.get("machines")),
        })
    return candidates


def rank_candidate(candidate: dict, routing_key: str) -> tuple:
    """The order `route` picks by, lowest first -- and what a late rebind re-ranks with.

    Pressure first -- queue per **fitting** machine -- then free fitting capacity, then the
    weighted lottery, then the id so the result is deterministic. `slots` is the model's
    own eligible machine count, so a site's small Macs no longer dilute a heavy model's
    share. Numbers decide; the controller still picks which machine runs it.
    """
    pressure = candidate["queue"] / candidate["slots"]
    free = -(candidate["available"] or 0)
    lottery = _weighted_tiebreak(routing_key, candidate["location_id"], candidate["slots"])
    if _truthy(config.get("GATEWAY_PREFER_FASTEST_SITE")) and (candidate["available"] or 0) > 0:
        # `GATEWAY_PREFER_FASTEST_SITE=1` is a **nudge, not a takeover**: it
        # only breaks a tie between sites whose numbers already match and that
        # each have a fitting Mac free. Fleet utilisation by capacity stays the
        # rule, so work a smaller Mac can do is not pulled onto a big one -- the
        # controller's own scheduler still reserves large Macs for heavy jobs.
        return (pressure, free, -candidate["speed"], lottery, candidate["location_id"])
    return (pressure, free, lottery, candidate["location_id"])


def pool_size(model_id: str, modality: str) -> int:
    """How many machines, fleet-wide, can run this model: the scarcity a queue is ordered by.

    The sum of each reachable location's published eligible slots for the model (RAM-aware
    as soon as the Hub's own figure is). Busy machines count -- this is capacity, not
    availability. `0` when nothing can run it right now.
    """
    return sum(c["slots"] for c in routing_candidates(model_id, modality))


def route(
    model_id: str,
    modality: str,
    *,
    routing_key: str = "",
    only_location: str | None = None,
    exclude_locations: set[str] | None = None,
) -> tuple[ProviderDefinition | None, str]:
    """Choose the location that should run this model. Never raises.

    Returns the location's provider definition and a human note naming the choice,
    or `(None, reason)` when nothing can take it. `exclude_locations` is how the
    worker asks for the **next** site after one refused the model at submit.
    """
    candidates = routing_candidates(
        model_id, modality, only_location=only_location, exclude_locations=exclude_locations
    )
    if not candidates:
        if only_location is not None:
            return None, f"the location that owns this voice ({only_location}) cannot run {model_id}"
        if exclude_locations:
            return None, f"every location that offers {model_id} refused it"
        return None, f"no location can run {model_id} right now"

    chosen = min(candidates, key=lambda c: rank_candidate(c, routing_key or model_id))
    note = (
        f"{chosen['label']}: {chosen['queue']} queued / {chosen['slots']} eligible"
        f" of {len(candidates)} location{'s' if len(candidates) != 1 else ''}"
    )
    return chosen["definition"], note


def hub_version_for(provider_id: str) -> str:
    """The Hub build behind a provider id, from the warm snapshot, or `""`.

    For a caller that is about to discover a location and wants to record which
    build answered. `allow_stale` because this must never be the reason a page
    waits: a version one refresher interval old is exactly as good for this, and
    the alternative is probing every controller again just to label a list.
    """
    if not provider_id:
        return ""
    try:
        for row in snapshot(allow_stale=True).get("locations") or []:
            if str(row.get("provider_id") or "") == provider_id:
                return str(row.get("version") or "").strip()
    except Exception:  # noqa: BLE001 - an unreadable snapshot means "unstated"
        return ""
    return ""


def refresh_models_when_hub_changes(rows: list[dict]) -> dict[str, str]:
    """Re-discover a location's models when the Hub behind it changes build.

    A Hub updates itself, and its model list is this gateway's memory of what it
    serves. Nothing re-read that memory on its own: the refresher below already
    probes every location and already knows the version, and it did nothing with
    a change — so a Hub that gained a lane kept being described from a cached
    list until somebody pressed Refresh. Measured 2026-09-25 on a controller that
    had updated: the new contract stayed invisible.

    **The baseline is the discovery cache, not memory.** `hub_version` is stored
    beside the rows it describes, so it survives a gateway restart — the common
    case, because an update and the restart that follows it are usually the same
    event. A location with no record yet is refreshed once and then recorded, so
    the feature seeds itself rather than staying quietly inert.

    Returns `{provider_id: version}` for what it refreshed, so a caller can log
    it and a test can assert it. Never raises: one location that cannot be
    discovered must not stop the others, and nothing here may kill the poller.
    """
    try:
        from .providers import registry
    except Exception:  # noqa: BLE001 - no registry means there is nothing to refresh
        return {}

    refreshed: dict[str, str] = {}
    for row in rows or []:
        provider_id = str(row.get("provider_id") or "").strip()
        version = str(row.get("version") or "").strip()
        # An unstated version is not a change. A Hub that is down, or one too old
        # to publish `capabilities.controller.studiohub_version`, must not make
        # this re-discover on every pass — that would be a discovery loop, not a
        # trigger.
        if not provider_id or not version:
            continue
        try:
            recorded = str(registry.models.cached(provider_id).get("hub_version") or "").strip()
            if recorded == version:
                continue
            result = registry.refresh(provider_id, hub_version=version)
        except Exception:  # noqa: BLE001 - one bad location is not a failed pass
            continue
        if result.error is None:
            refreshed[provider_id] = version
    return refreshed


_refresher: threading.Thread | None = None


_refresher_stop = threading.Event()


def stop_refresher() -> None:
    """Ask the background refresher to end (the app's lifespan calls this on shutdown).

    A test that enters the app's lifespan used to leave the poller running for the rest of
    the process, where it raced every later `snapshot(force=True)` (a refresh that finished
    after the caller asked is returned instead of the caller's own).
    """
    _refresher_stop.set()


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
    _refresher_stop.clear()  # before the liveness check: a stop that has not landed yet is undone
    if _refresher is not None and _refresher.is_alive():
        return

    def loop() -> None:
        while not _refresher_stop.is_set():
            value = None
            try:
                value = snapshot(force=True)
            except Exception:  # noqa: BLE001 - the poller must outlive any one site
                pass
            if value is not None:
                # **After** the snapshot is cached, never before. Discovery talks
                # to the Hub and can take seconds; the page must never wait on it.
                # This thread is the only caller, so a slow discovery delays the
                # next pass rather than piling up behind it.
                try:
                    refresh_models_when_hub_changes(value.get("locations") or [])
                except Exception:  # noqa: BLE001 - discovery must not kill the poller
                    pass
            _refresher_stop.wait(interval)

    _refresher = threading.Thread(target=loop, name="fleet-refresh", daemon=True)
    _refresher.start()
