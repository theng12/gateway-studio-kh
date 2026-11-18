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
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx

from . import fleet_history
from . import render_optin
from .config import DATA_DIR
from .hub import hub_base_url, hub_token
from .render_contract import LATE_LABEL_CONTRACT, LATE_LABEL_MOTIONS, SUBTITLE_CONTRACT
from .render_hash_v1 import decode_render_json

log = logging.getLogger("gateway.fleet")

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


def label_for_provider(provider_id: str) -> str | None:
    """The owner's own name for a location (`studio-hub-<id>`), or `None` when it has none.

    Published text names a Mac by this label, never by its controller id: the id means nothing to the
    owner (and Story's rule is that it never shows one).
    """
    controller_id = controller_id_from(str(provider_id or ""))
    if controller_id is None:
        return None
    if controller_id == LOCAL_ID:
        return local_label()
    for row in _read():
        if str(row.get("id") or "") == controller_id:
            label = str(row.get("label") or "").strip()
            return label or None
    return None


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

    **A clone whose clip this gateway holds pins nothing** (`_clip_travels_with_job`).
    """
    if not isinstance(params, dict):
        return None
    from . import image_target
    target = image_target.read(params)
    if target is not None:
        # A measurement pin never disappears when a site becomes unavailable.
        return target["location_id"]
    for key in _VOICE_PIN_KEYS:
        value = str(params.get(key) or "")
        location_id = location_for_voice(value)
        if location_id:
            if key == "voice_id" and _clip_travels_with_job(value):
                continue
            return location_id if _location_takes_customer_jobs(location_id) else None
    # 0.2.172: a request may name its site outright (`"site": "<controller id>"` or `"studio-hub-<id>"`),
    # e.g. a music job measured on one location. It stays in the job's params (so a late re-route keeps
    # it) and is never forwarded to a Hub (`studio_hub._HUB_MUSIC_PARAMS` is an allowlist).
    site = str(params.get("site") or "").strip()
    if site:
        location_id = controller_id_from(site) or site
        return location_id if _location_takes_customer_jobs(location_id) else None
    return None


def _clip_travels_with_job(voice_id: str) -> bool:
    """Whether this gateway holds the voice's reference clip, so the clip can go with the job.

    The gateway's own voices are published as `studio-hub-local::voice-…`, and the pin read that
    namespace as "the clip lives on this Mac's Hub". It does not: it lives in the gateway's voice
    library, and the adapter stages it at whichever location runs the job
    (`studio_hub._stage_voice_reference`, `POST /api/hub/voice-references` — "a clone reference
    travels with the job that uses it"). The Hub refuses bytes that do not match the declared
    sha256, the adapter checks the digest the Hub kept, and the worker checks it again, so the
    voice is the same recording everywhere. The pin protected none of that and held every chapter
    of a narration on one Mac while the fleet sat idle (measured 2026-10-03: 36 of 37 TTS jobs on
    `studio-hub-local`, ~14 min each, one at a time).

    A voice whose clip lives only on a Hub (a controller's shared voice) is not in this library and
    keeps its pin, as does a library entry whose file is missing (the adapter would refuse it).
    """
    from .voices import resolve  # lazy: `voices` imports this module

    voice = resolve(voice_id)
    path = str(((voice.raw or {}) if voice is not None else {}).get("reference_path") or "")
    return bool(path) and Path(path).is_file()


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
    ("/api/hub/fleet/nodes/local/work-policy", {}),
)


def _fetch_optional(client: "httpx.Client", base: str, token: str) -> tuple[dict, set[str]]:
    """Every `_OPTIONAL_PROBES` path, in parallel, each defaulting on failure.

    Sequentially these were the reason a busy Hub timed the page out. Every one
    carries the full per-request timeout, and none needs another's answer, so the
    cost should be the *slowest* rather than the sum. Measured across the owner's
    six locations: one snapshot took 22.7 s, against the app's 15 s request
    timeout -- so the page appeared only when the fleet was idle, which is exactly
    when nobody is looking at it.

    **The second half of the answer is which reads failed**, and only one caller
    needs it: the orphan check. `{"batches": []}` from a route that never answered
    and from a controller with nothing running are the same dict, and a page that
    read the first as "no batches" would report a clean fleet on a broken read.
    """
    out: dict = {}
    failed: set[str] = set()
    # The owner's "Reset counters" baseline rides on the stats read; a Hub that predates the parameter
    # ignores it and says so through `fleet_activity.window.since_s` (see `counted_since`).
    since = counters_since()
    query = {"/api/hub/stats": f"?activity_since={since}"} if since else {}
    with ThreadPoolExecutor(max_workers=len(_OPTIONAL_PROBES)) as pool:
        pending = {
            pool.submit(_fetch, client, base, token, path + query.get(path, "")): (path, empty)
            for path, empty in _OPTIONAL_PROBES
        }
        # `.result()` in submission order collects rather than serialises: every
        # future is already running by the time the first is awaited.
        for future, (path, empty) in pending.items():
            try:
                out[path] = future.result()
            except Exception:  # noqa: BLE001 - "best effort" is the contract
                out[path] = empty
                failed.add(path)
    return out, failed


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


_KEEP = object()


def _write(rows: list[dict], *, local_name: str | None = None, local_paused_at: object = _KEEP,
           local_paused_by: object = _KEEP) -> None:
    with _ROSTER_LOCK:
        _write_locked(rows, local_name=local_name, local_paused_at=local_paused_at,
                      local_paused_by=local_paused_by)


def _write_locked(rows: list[dict], *, local_name: str | None = None, local_paused_at: object = _KEEP,
                  local_paused_by: object = _KEEP) -> None:
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
    # The owner's hand-pause of this Mac rides along the same way: kept unless a caller states it.
    held = _read_payload().get("local_paused_at") if local_paused_at is _KEEP else local_paused_at
    if held:
        payload["local_paused_at"] = held
    # Who made that pause, beside it, so the row can say so and a resume can clear both.
    by = _read_payload().get("local_paused_by") if local_paused_by is _KEEP else local_paused_by
    if by:
        payload["local_paused_by"] = by
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
    # location switches on the models it has installed (`seed_enabled_installed`), but re-adding a location
    # that already carries the owner's choices — a new address, a re-pasted
    # token — must never overwrite them.
    row["created"] = created
    return row


#: Models the owner has ruled out, never switched on by the Gateway on its own (a new location, a finished
#: install plan); the owner can still tick one by hand. Matched as substrings of the model id and package id.
#: ponytail: a hand list. The Hub publishes no shelved/parked/disqualified flag on its model rows, so this names
#: Phonon-2 (parked), Pocket-TTS (disqualified) and the Hub's own `components/shelved_models.py`; read a Hub
#: flag instead once it publishes one.
NEVER_AUTO_ENABLED = ("phonon-2", "pocket-tts", "whisper-base", "whisper-small", "voxcpm2-bf16",
                      "fish-audio-s2-pro-bf16", "chatterbox", "s3tokenizerv2")


def ruled_out(*names: object) -> bool:
    """True when any of these names (model id, package id) is a model the Gateway never switches on itself."""
    return any(token in str(name or "").lower() for name in names for token in NEVER_AUTO_ENABLED)


def seed_enabled_installed(provider_id: str, inventory: Any = None) -> dict:
    """Switch on, at a location, every model its own Hub reports installed and ready.

    The owner's rule (2026-10-09): *"a new group starts with every model it actually has installed switched
    on; you switch off what you don't want in the Gateway. Nothing copied from This Mac."* Installed and ready
    is the Hub's own `files_verified` + `eligible`, a catalogued model (not a dependency) the catalogue could
    publish (`can_publish`: not retired). `NEVER_AUTO_ENABLED` stays off. A model that already has a decision
    there keeps it; nothing else is written.

    *owner, 2026-10-31: "models installed should be on"* — so this is no longer only the add's
    seeding. `ModelInventory.refresh` runs it after **every** read of a location's inventory, which is
    the same rule applied to the same facts, just not only once. `inventory` is the store to read and
    write: always the one that just refreshed, never a second handle on the same file.

    **`enabled` counts what is on now, not what this call switched on.** The rule runs on every read,
    so a second run must not report "0 switched on" beside a page that had just switched five on.
    """
    from .providers import registry

    # **Only a location's own models.** A cloud provider's catalogue is switched on by
    # hand from the Models page, never by this rule, so a caller that arrives here
    # with a vendor's id is refused rather than quietly enabling a vendor's rows.
    if not is_location_provider(provider_id):
        return {"provider_id": provider_id, "models_discovered": 0, "enabled": 0, "switched_on": 0,
                "ruled_out": 0, "sentence": "Only a location's own installed models are switched on like this."}
    inventory = inventory if inventory is not None else registry.models
    definition = registry.definition(provider_id)
    if definition is None:
        return {"provider_id": provider_id, "models_discovered": 0, "enabled": 0, "switched_on": 0,
                "ruled_out": 0, "error": f"unknown provider '{provider_id}'",
                "sentence": "This location is not on the roster any more, so nothing was switched on."}
    rows = inventory.models_for(provider_id, include_disabled=True)
    ready = [m for m in rows if (m.get("raw") or {}).get("files_state") == "files_verified"
             and (m.get("raw") or {}).get("dispatch_state") == "eligible"
             and (m.get("raw") or {}).get("catalogued") is not False
             and inventory.can_publish(definition, m)]
    kept_off = [m["model_id"] for m in ready if ruled_out(m["model_id"], (m.get("raw") or {}).get("package_id"))]
    wanted = [m["model_id"] for m in ready if m["model_id"] not in set(kept_off)]
    switched_on = inventory.enable_undecided(provider_id, wanted)
    # **What is on now.** Re-read, because the readiness filter above is the rule and the
    # bucket is the owner's: a model the owner switched off is on neither list.
    on = {m["model_id"] for m in inventory.models_for(provider_id, include_disabled=True) if m["enabled"]}
    enabled = [mid for mid in wanted if mid in on]
    discovered = len({m["model_id"] for m in rows})
    if not discovered:
        sentence = "No models were discovered here, so nothing was switched on."
    else:
        sentence = (f"{len(enabled)} installed {'model' if len(enabled) == 1 else 'models'} switched on here, "
                    f"of {discovered} discovered")
        if kept_off:
            sentence += f"; {len(set(kept_off))} ruled out by the owner left off"
        sentence += ". Switch off what you don't want under Models."
    return {"provider_id": provider_id, "models_discovered": discovered, "enabled": len(enabled),
            "switched_on": len(switched_on), "ruled_out": len(set(kept_off)), "sentence": sentence}


def seed_installed_after_refresh(provider_id: str, result: Any, inventory: Any = None) -> dict:
    """Switch on what a location has installed, after a read of its inventory that answered.

    The rule above, run again — never a second rule. **A failed or partial read writes nothing**: a
    Hub that did not answer is not a Hub saying "nothing is installed", and a catalogue that did not
    answer is not an inventory that says what is ready. Either is this pass's ignorance; the next
    read that does answer seeds. The result is a report, `{}` when nothing was written.
    """
    if result is None or getattr(result, "error", None) or getattr(result, "catalogue_missing", False):
        return {}
    try:
        return seed_enabled_installed(provider_id, inventory)
    except Exception:  # noqa: BLE001 - a seeding failure is never a discovery failure
        log.warning("installed-model seeding failed for %s", provider_id, exc_info=True)
        return {"provider_id": provider_id, "error": "seeding failed"}


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


def remove_controller(controller_id: str, *, store: Any = None) -> bool:
    """Remove a controller and purge every Gateway record for it and its nodes."""
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
    _purge_controller_state(controller_id, store=store)
    _scrub_cached_snapshot(controller_id)
    invalidate()
    return True


def remove_node(controller_id: str, machine_id: str, *, store: Any = None) -> dict[str, Any]:
    """Purge every Gateway record for one node (`fleet_history`, overrides, opt-ins, jobs, cache)."""
    purged = _purge_node_state(controller_id, machine_id, store=store)
    _scrub_cached_snapshot(controller_id, machine_id)
    invalidate()
    return purged


def _active_store(explicit: Any) -> Any:
    if explicit is not None:
        return explicit
    import sys
    mod = sys.modules.get("gateway.runtime")
    return getattr(mod, "store", None) if mod is not None else None


def _purge_controller_state(controller_id: str, *, store: Any = None) -> dict[str, Any]:
    from . import image_assignment, limits, model_share, render_optin, startup_control

    fleet_history.purge_location(controller_id)
    _purge_lite_mode_known(controller_id)
    startup_control.purge_pending(controller_id)
    render_optin.purge(controller_id)
    image_assignment.purge(controller_id)
    model_share.purge(controller_id)
    limits.purge_provider(provider_id_for(controller_id))
    db = _active_store(store)
    db_counts = db.purge_provider_records(provider_id_for(controller_id), controller_id=controller_id) if db is not None else {}
    return {"controller_id": controller_id, "db": db_counts}


def _purge_node_state(controller_id: str, machine_id: str, *, store: Any = None) -> dict[str, Any]:
    from . import image_assignment, model_share, render_optin, startup_control

    history_count = fleet_history.purge_machine(controller_id, machine_id)
    lite_known_count = _purge_lite_mode_known(controller_id, machine_id)
    lite_pending_count = startup_control.purge_pending(controller_id, machine_id)
    optin_count = render_optin.purge(controller_id, machine_id)
    override_count = image_assignment.purge(controller_id, machine_id)
    share_counts = model_share.purge(controller_id, machine_id)
    db = _active_store(store)
    db_counts = db.purge_node_records(controller_id, machine_id) if db is not None else {}
    return {
        "history": history_count,
        "lite_mode_known": lite_known_count,
        "lite_mode_pending": lite_pending_count,
        "render_optin": optin_count,
        "image_overrides": override_count,
        "model_share": share_counts,
        "db": db_counts,
    }


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


def _artifact_count(batch: dict) -> int | None:
    """How many artifacts a batch says it made, or `None` when the Hub did not say.

    `None` is the point: a batch row that names no artifacts is not a batch that made zero, and
    the Fleet page prints "not reported" for it. A list the Hub sends is counted as it is; a
    number the Hub states is passed through only when it is a real non-negative integer.
    """
    stated = batch.get("artifact_count")
    if isinstance(stated, int) and not isinstance(stated, bool) and stated >= 0:
        return stated
    artifacts = batch.get("artifacts")
    if isinstance(artifacts, list):
        return len(artifacts)
    items = batch.get("items")
    if isinstance(items, list):
        # Transcription batches publish `artifact_url` per done item (Studio Hub's own shape).
        return sum(1 for item in items if isinstance(item, dict) and item.get("artifact_url"))
    return None


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
            # **What it has produced, when the Hub says.** The list route publishes no per-batch
            # output today; these read through if a newer Hub adds them, and arrive as `None`
            # otherwise so a reader can say "not reported" instead of printing a zero. The
            # artifact count is derived from a list when the Hub sends the list itself.
            "artifact_count": _artifact_count(batch),
            "output_path": batch.get("output_path"),
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
        # **Which kind of failure it was**, in the vocabulary `fleet_history` uses for its cause
        # hints: "refused", "timeout", "http", "server_disconnected" or "error". The `error`
        # sentence is for a person and its wording has changed twice; a hint must not parse it.
        "error_kind": None,
        "role": None,
        "version": None,
        # How that version compares to the one this build targets; `None` until a
        # Hub answers, and `None` again if it refuses to state a version.
        "hub_version_relation": None,
        "machines": [],
        "machine_summary": None,
        "nodes": [],
        "node_summary": None,
        "local_machine_paused": None,
        "running_jobs": [],
        # **Did the in-flight read actually answer?** `running_jobs: []` cannot say whether a
        # controller has nothing running or its jobs route failed — the orphan check must not
        # report the second as a clean fleet (see `orphans.annotate`). `None` until measured.
        "jobs_read": None,
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
        # Exits in reverse: the pool drains before the client it shares closes.
        with httpx.Client(timeout=timeout) as client, ThreadPoolExecutor(max_workers=1) as pool:
            # **The enrichment reads, all at once -- and beside the overview,
            # not after it.** Nothing below needs anything else below, so fetching
            # them in sequence -- which is what this did -- made each pay the full
            # per-request timeout one after the other. On a Hub that is up but busy
            # rendering, that is ~22 s for one snapshot against the app's 15 s
            # timeout, so the Fleet page loaded only when the fleet was idle. The
            # overview, too, was a serial round trip in front of them (up to 2 s
            # measured on a loaded controller over Tailscale).
            pending = pool.submit(_fetch_optional, client, base, token)
            overview = _fetch(client, base, token, "/api/hub/fleet/overview")
            optional, failed_reads = pending.result()
            nodes = optional["/api/hub/fleet/nodes"]
            jobs = optional["/api/hub/jobs"]
            capabilities = optional["/api/hub/capabilities"]
            stats = optional["/api/hub/stats"]
            models_payload = optional["/api/hub/models"]
            workers_payload = optional["/api/hub/workers"]
            packages_payload = optional["/api/hub/model-packages"]
            health_payload = optional["/api/hub/model-health"]
            policy_payload = optional["/api/hub/fleet/nodes/local/work-policy"]
            policy = policy_payload.get("work_policy") if isinstance(policy_payload, dict) else None
            enabled = policy.get("machine_enabled") if isinstance(policy, dict) else None
            # The controller is absent from its joined-node roster; its own work policy owns this flag.
            location["local_machine_paused"] = not enabled if type(enabled) is bool else None

            # Why each enrichment is best-effort rather than required, kept
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
            # * `fleet/nodes/local/work-policy` -- the controller's own machine
            #   enablement; it has no joined-node row. Absent or invalid stays unknown.
            #
            # Each degrades to its own empty value in `_OPTIONAL_PROBES`; one
            # broken route must never make a whole location read as offline.
    except httpx.HTTPStatusError as exc:
        location["error_kind"] = "http"
        location["error"] = (
            "Rejected the Hub token (HTTP %d)." % exc.response.status_code
            if exc.response.status_code in (401, 403)
            else "HTTP %d from the controller." % exc.response.status_code
        )
        return location
    except httpx.ConnectError:
        location["error_kind"] = "refused"
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
        location["error_kind"] = "timeout"
        location["error"] = (
            f"No answer within {int(_TIMEOUT)}s. The address is reachable but nothing replied — "
            "check the machine is awake and that Tailscale is up at both ends."
        )
        # Slow, not gone: `_refresh` keeps what it last reported instead of blanking the row.
        location["slow"] = True
        return location
    except httpx.HTTPError as exc:
        # A dropped connection (`RemoteProtocolError: Server disconnected`) is its own fault —
        # something *was* answering — and is worth telling apart from a refusal.
        location["error_kind"] = (
            "server_disconnected" if "disconnect" in str(exc).lower() else "error")
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
    local_models = _local_models(models_payload, dispatch, _package_ids(packages_payload))

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
        # Whether the jobs read above answered. A failed optional read degrades to `{"batches":
        # []}`, which is indistinguishable from an idle controller — and the orphan check has to
        # tell them apart, so it reports "not checked" rather than "none".
        "jobs_read": "/api/hub/jobs" not in failed_reads,
        "activity": {
            "pulse": activity.get("pulse") or {},
            "machines": real_activity,
            "observed_at": activity.get("observed_at"),
            # From when this Hub counted done/failed (`{since_s, now}`); how the page tells a
            # reset Hub from one too old to honour the baseline.
            "window": activity.get("window"),
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


def _package_ids(payload: object) -> dict[str, str]:
    """Model id -> the package id its Hub installs it by (`/api/hub/model-packages`, models only).

    A Studio's model row names its package only when the files came from the package library, so a
    model that is not on a Mac yet - the one an install is for - has none; the catalog always does.
    """
    rows = (payload.get("models") or payload.get("packages") or []) if isinstance(payload, dict) else []
    return {str(row["model_id"]): str(row["package_id"]) for row in rows if isinstance(row, dict)
            and row.get("model_id") and row.get("package_id") and row.get("type", "model") == "model"}


def installable_models(location: dict, machine: str, memory_gb: object = None) -> list[dict]:
    """What "Install a model…" offers for one Mac (owner decision 2026-10-04): the models its own catalog
    says it has the memory for (`memory_eligible`), that are not on it and not downloading, by package id.

    `memory_gb` is the Mac's total; a row whose package tier (`min_unified_memory_gb`) is above it is not
    offered even when the worker calls the host eligible (Image workers publish a host-level
    `memory_eligible`, so Hub 0.6.158 offered 16 GB Qwen Low-RAM to 8 GB Macs, whose Hub then refused).
    """
    def fits(row: dict) -> bool:
        tier, total = _num(row.get("min_unified_memory_gb")), _num(memory_gb)
        return tier is None or total is None or total >= tier

    found: dict[str, dict] = {}
    for row in location.get("local_models") or []:
        if (row.get("machine") == machine and row.get("memory_eligible") is True and row.get("package_id")
                and row.get("cache_state") != "cached" and not row.get("active_download") and fits(row)):
            found.setdefault(row["package_id"], {"package_id": row["package_id"], "model": row.get("model"),
                                                 "label": row.get("label") or row.get("model"),
                                                 "size_gb": row.get("size_gb")})
    return sorted(found.values(), key=lambda item: str(item["label"]).lower())


def _local_models(payload: object, dispatch: dict[str, object],
                  packages: dict[str, str] | None = None) -> list[dict]:
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
        package_id = str(cache.get("package_id") or row.get("package_id")
                         or (packages or {}).get(str(row.get("model_id") or row.get("repo") or "")) or "").strip()
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
            # The package list is the controller's own disk, so its verdict is the controller's
            # Mac's (`local`) only: stamped on a joined node's row it called a model that node has
            # installed "ineligible" whenever the controller lacked it, and the reverse.
            "dispatch_state": (dispatch.get(package_id)
                               if package_id and row.get("hub_machine") in (None, "local") else None),
            # What the Hub installs this model by (Fleet → Machines → … → Install a model…).
            "package_id": package_id or None,
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
    result = {
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
    if "video.final_render" in result["supported_operations"]:
        for key in (
            "operation", "physical_machine_id", "hardware_class", "profile_digest",
            "tools_digest", "compiler_digest", "qualification_digest", "selected_tier",
            "exact_encoder", "font_support", "bounds", "profile_execution_bounds",
            "production_eligible", "production_enabled", "qualification_candidates",
            "encode",
        ):
            result[key] = row.get(key)
        # The Hub's own evidence that it accepts unqualified work (owner opt-in). Carried only when the
        # Hub sent it, so a 0.6.195 row (and the capability snapshot made from it) is exactly what it was.
        if render_optin.OPT_IN_KEY in row:
            result[render_optin.OPT_IN_KEY] = row[render_optin.OPT_IN_KEY]
        # The newer optional key (owner, 2026-10-28), copied only when the Hub sent it so a
        # 0.6.195 row and the snapshot made from it stay byte-identical to before. The measured
        # speed is NOT copied here: it travels on the machine row, because this row's key set is
        # the mixed-fleet contract an older controller checks.
        if render_optin.PAUSED_RENDER_KEY in row:
            result[render_optin.PAUSED_RENDER_KEY] = row[render_optin.PAUSED_RENDER_KEY]
        result["available_capacity"] = dict(capacity)
    return result


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


def _unread_location(location_id: str, label: str, url: str, *, local: bool = False) -> dict:
    """A location row with nothing read from it yet: every field present, every value empty."""
    return {
        "id": location_id, "label": label, "url": url, "local": local,
        "provider_id": provider_id_for(location_id),
        "reachable": None, "error": None, "error_kind": None,
        "role": None, "version": None, "hub_version_relation": None,
        "machines": [], "machine_summary": None,
        "nodes": [], "node_summary": None, "running_jobs": [], "jobs_read": None,
        "activity": {"pulse": {}, "machines": [], "observed_at": None},
        "by_machine": {}, "matrix": [], "jobs_total": None, "latency_ms": None,
        "site_id": None, "controller_id": None, "ready": None,
        "capacity": {}, "local_models": [], "workers": [], "hub_workers": [],
        "model_health": [],
        "release": None,
        "probe_machines_hidden": 0,
    }


def pending_location(row: dict) -> dict:
    """A configured location the fleet has not read yet — "Just added — reading its Macs…".

    The add answers with this, and a read served before the next fan-out carries it, so a location
    that exists in the roster is never absent from the page while its first read is in flight.
    """
    out = _unread_location(str(row.get("id") or ""), str(row.get("label") or row.get("url") or ""),
                           str(row.get("url") or ""))
    out["pending"] = True
    return out


def with_roster(value: dict) -> dict:
    """A snapshot made to agree with the roster on disk now: who is in it, and what each is called.

    Served while the snapshot is older than the last roster change (`invalidate`), so the page shows a
    just-added location (pending), drops a removed one and shows a rename — without waiting for a
    fan-out that takes as long as the slowest controller. Never mutates the cached dict.
    """
    roster = {str(row.get("id") or ""): row for row in _read()}
    rows: list[dict] = []
    seen: set[str] = set()
    for row in value.get("locations") or []:
        rid = str(row.get("id") or "")
        if row.get("local"):
            rows.append(row)
            continue
        if rid not in roster:
            continue
        seen.add(rid)
        configured = roster[rid]
        label = configured.get("label") or configured.get("url")
        if row.get("label") != label or row.get("url") != configured.get("url"):
            row = {**row, "label": label, "url": configured.get("url")}
        rows.append(row)
    rows += [pending_location(row) for rid, row in roster.items() if rid not in seen]
    kept_ids = {str(r.get("id") or "") for r in rows}
    if len(kept_ids) == len({str(r.get("id") or "") for r in (value.get("locations") or [])}):
        return {**value, "locations": rows}
    machines = [m for m in value.get("machines") or [] if str(m.get("location_id") or "") in kept_ids]
    last_known = [m for m in value.get("last_known_machines") or [] if str(m.get("location_id") or "") in kept_ids]
    running = [r for r in value.get("running") or [] if str(r.get("location_id") or "") in kept_ids]
    issues = [i for i in value.get("issues") or [] if str(i.get("location_id") or "") in kept_ids]
    summary = _summarise(rows)
    summary["issues"] = len(issues)
    return {
        **value,
        "locations": rows,
        "machines": machines,
        "last_known_machines": last_known,
        "running": running,
        "issues": issues,
        "summary": summary,
    }


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
        # Stamped before the probe: one that began before a refusal must not clear it.
        local_started = time.time()
        local = _probe(local_base, local_token)
        if local.get("reachable"):
            _remember_live_read(LOCAL_ID, local_started)
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
        _stamp_pause(local, LOCAL_ID)
    else:
        # Published here too, and deliberately: the provider identity does not
        # depend on the Hub answering. A page that can only name the provider of
        # a location that is up cannot offer to refresh the one that is down.
        local = _unread_location(LOCAL_ID, local_label(), "", local=True)
        local.update(reachable=False, error_kind="local_hub_down",
                     error=local_error or "Studio Hub is not running on this Mac.")
        _stamp_pause(local, LOCAL_ID)
    _qualify_location_labels(local)
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
    probes_started = time.time()
    with ThreadPoolExecutor(max_workers=min(_MAX_PROBES, len(configured))) as pool:
        probed = list(pool.map(
            lambda row: _probe(str(row["url"]), _token_for(str(row.get("id") or ""))),
            configured,
        ))

    for row, result in zip(configured, probed):
        controller_id = str(row.get("id") or "")
        reachable_now = bool(result.get("reachable"))
        if reachable_now:
            _remember_live_read(controller_id, probes_started)
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
        _stamp_pause(result, controller_id)
        _qualify_location_labels(result)
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
#: Keep ordinary reads from launching another sweep between the 15-second refresher passes.
#: ponytail: 30 seconds permits a fallback refresh if the refresher falls behind; tune from measured sweep time.
_CACHE_TTL = 30.0
#: `gen` counts roster changes (`invalidate`): a fan-out that read the roster before the latest change
#: must not cache its answer. `last` is the value `invalidate` dropped, served (made to agree with the
#: roster, `with_roster`) to a plain read until the next fan-out lands, so that read never waits.
_cache: dict = {"at": 0.0, "value": None, "gen": 0, "last": None}
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


def _drop_from_snapshot(snap: dict, controller_id: str, machine_id: str | None = None) -> dict:
    """Return a copy of `snap` with `controller_id` (or just `machine_id` inside it) removed."""
    if machine_id is None:
        rows = [r for r in (snap.get("locations") or []) if str(r.get("id") or "") != controller_id]
        match = lambda item: str(item.get("location_id") or "") == controller_id  # noqa: E731
        issue_match = lambda item: str(item.get("location_id") or "") == controller_id  # noqa: E731
    else:
        rows = []
        for row in snap.get("locations") or []:
            if str(row.get("id") or "") != controller_id:
                rows.append(row)
                continue
            act = row.get("activity") if isinstance(row.get("activity"), dict) else {}
            rows.append({
                **row,
                "machines": [m for m in (row.get("machines") or []) if str(m.get("machine") or "") != machine_id],
                "nodes": [n for n in (row.get("nodes") or []) if str(n.get("id") or "") != machine_id],
                "local_models": [m for m in (row.get("local_models") or []) if str(m.get("machine") or "") != machine_id],
                "workers": [w for w in (row.get("workers") or []) if str(w.get("machine") or "") != machine_id],
                "model_health": [h for h in (row.get("model_health") or []) if str(h.get("machine") or "") != machine_id],
                "activity": {
                    **act,
                    "machines": [e for e in (act.get("machines") or []) if str(e.get("machine") or "") != machine_id],
                },
            })
        match = lambda item: (str(item.get("location_id") or "") == controller_id  # noqa: E731
                              and str(item.get("machine") or "") == machine_id)
        issue_match = lambda item: (str(item.get("location_id") or "") == controller_id  # noqa: E731
                                    and str(item.get("subject") or "") == machine_id)
    machines = [m for m in (snap.get("machines") or []) if not match(m)]
    last_known = [m for m in (snap.get("last_known_machines") or []) if not match(m)]
    running = [r for r in (snap.get("running") or []) if not match(r)]
    issues = [i for i in (snap.get("issues") or []) if not issue_match(i)]
    summary = _summarise(rows)
    summary["issues"] = len(issues)
    return {
        **snap,
        "locations": rows,
        "machines": machines,
        "last_known_machines": last_known,
        "running": running,
        "issues": issues,
        "summary": summary,
    }


def _scrub_cached_snapshot(controller_id: str, machine_id: str | None = None) -> None:
    with _cache_lock:
        for slot in ("value", "last"):
            if isinstance(_cache.get(slot), dict):
                _cache[slot] = _drop_from_snapshot(_cache[slot], controller_id, machine_id)


def invalidate() -> None:
    """Drop the cached snapshot.

    Called whenever the roster changes. Without it, adding a location leaves the
    next read serving the previous snapshot for up to the refresh interval, so
    the page you just changed would not show your change — the most confusing
    possible moment to be stale.
    """
    with _cache_lock:
        _cache["last"] = _cache["value"] or _cache["last"]
        _cache["at"] = 0.0
        _cache["value"] = None
        _cache["gen"] += 1


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

    off_lite = sum(1 for row in flat if row.get("lite_off"))
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

    # **One Mac joined to two locations is two rows in the table, one Mac in the fleet.**
    # `machines` counts rows (the table's own number, deliberately); these say how many Macs
    # those rows are, so a headline cannot imply 47 Macs when it means 43.
    # A row with no id is counted as its own Mac, never as a second row of another one.
    distinct_machines = len({str(row.get("machine") or f"row-{i}") for i, row in enumerate(flat)})

    startups = [row.get("startup") for row in flat]
    statuses = [row.get("version_status") for row in flat]
    latest = next((row.get("hub_latest") for row in rows if isinstance(row.get("hub_latest"), dict)), None)
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
        "machines_distinct": distinct_machines,
        "machines_shared_across_locations": max(0, machines_total - distinct_machines),
        "machines_online": online,
        # A Lite Mode Mac switched off is not "offline" (a fault) but its own, expected count.
        "machines_offline": offline - off_lite,
        "machines_off_lite": off_lite,
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
        # **Which Macs are on the newest Studio Hub this Gateway published.** `hub_latest` is that
        # version (`None`: nothing published here, so no verdict); `on_latest` of `version_known`
        # (Macs that answered with a version), plus how many are updating, behind, or unknown.
        "hub_latest": (latest or {}).get("version"),
        "on_latest": statuses.count("latest"),
        "version_known": sum(1 for status in statuses if status in {"latest", "updating", "behind"}),
        "hub_updating": statuses.count("updating"),
        "hub_behind": statuses.count("behind"),
        "hub_unknown": statuses.count("unknown"),
        "jobs_completed": completed,
        "jobs_failed": failed,
        # The owner's "Reset counters" baseline (epoch seconds), `None` when never reset.
        "counters_since": counters_since(),
        "running_jobs": sum(len(row["running_jobs"]) for row in rows),
        # The Fleet page's Models/Machines tabs.
        "local_models_total": local_models_total,
        "models_available": models_available,
        "workers_total": workers_total,
        "workers_ready": workers_ready,
        # **How the fleet is wired, in one line** ("3 Macs on slow Ethernet"). Counted from the same
        # machine rows the table draws, so the headline cannot disagree with it; a Mac whose Hub
        # does not publish its link is counted as `unmeasured`, never as fine.
        "connections": _connection_summary(flat),
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


#: Hub disk warnings that are not about free space, by what they are. The owner keeps macOS
#: automatic updates on deliberately, so each *kind* can be hidden; a warning about **disk space**
#: is never hideable (`HIDEABLE_WARNING_KINDS` is the whole list, and the setter refuses the rest).
WARNING_KIND_TITLES = {
    "macos_updates": "macOS automatic updates",
    "time_machine": "Time Machine",
    "disk_space": "Disk space",
    "other": "Other",
}
HIDEABLE_WARNING_KINDS = ("macos_updates", "time_machine")
FLEET_WARNINGS_PATH = Path(str(DATA_DIR)) / "fleet_warnings.json"
_warnings_lock = threading.Lock()
_warnings_cache: dict = {"stamp": None, "hidden": []}


def warning_kind(text: object) -> str:
    """What a Hub's disk warning is about: `macos_updates`, `time_machine`, `disk_space` or `other`."""
    lowered = str(text or "").lower()
    if "time machine" in lowered:
        return "time_machine"
    if "update" in lowered and ("automatic" in lowered or "download" in lowered):
        return "macos_updates"
    if "free" in lowered or "disk" in lowered or "space" in lowered:
        return "disk_space"
    return "other"


def hidden_warning_kinds() -> list[str]:
    """The warning kinds the owner hid, read fresh (cheap: re-read only when the file changes)."""
    try:
        info = FLEET_WARNINGS_PATH.stat()
        stamp = (info.st_mtime_ns, info.st_size)
    except OSError:
        return []
    with _warnings_lock:
        if _warnings_cache["stamp"] == stamp:
            return list(_warnings_cache["hidden"])
        try:
            raw = json.loads(FLEET_WARNINGS_PATH.read_text(encoding="utf-8")).get("hidden")
        except (OSError, ValueError, AttributeError):
            raw = []
        hidden = [k for k in HIDEABLE_WARNING_KINDS if k in (raw if isinstance(raw, list) else [])]
        _warnings_cache.update(stamp=stamp, hidden=hidden)
        return list(hidden)


def set_hidden_warning_kinds(kinds: object) -> list[str]:
    """Replace the hidden list. Refuses anything that is not hideable, disk space above all."""
    if not isinstance(kinds, list):
        raise ValueError("`hidden` must be a list of warning kinds.")
    refused = [str(k) for k in kinds if k not in HIDEABLE_WARNING_KINDS]
    if refused:
        raise ValueError(
            f"Cannot hide {', '.join(refused)}: only {', '.join(HIDEABLE_WARNING_KINDS)} can be hidden. "
            "Disk-space warnings are always shown.")
    hidden = [k for k in HIDEABLE_WARNING_KINDS if k in kinds]
    with _warnings_lock:
        FLEET_WARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        scratch = FLEET_WARNINGS_PATH.with_name(FLEET_WARNINGS_PATH.name + ".tmp")
        scratch.write_text(json.dumps({"hidden": hidden}, indent=2), encoding="utf-8")
        os.replace(scratch, FLEET_WARNINGS_PATH)
        _warnings_cache["stamp"] = None
    return hidden


# The Machines tab's "Reset counters": a baseline, never a deletion. Every Hub keeps its whole job
# history; this only says from when its done/failed/median are counted (`/api/hub/stats?activity_since`).
FLEET_COUNTERS_PATH = Path(str(DATA_DIR)) / "fleet_counters.json"


def counters_since() -> float | None:
    """The baseline the owner set with "Reset counters", in epoch seconds; `None` when never reset."""
    try:
        value = json.loads(FLEET_COUNTERS_PATH.read_text(encoding="utf-8")).get("since")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def reset_counters(now: float | None = None) -> float:
    """Count every Mac's jobs from now on. Returns the new baseline; the next read fetches afresh."""
    since = round(float(time.time() if now is None else now), 3)
    FLEET_COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = FLEET_COUNTERS_PATH.with_name(FLEET_COUNTERS_PATH.name + ".tmp")
    scratch.write_text(json.dumps({"since": since}), encoding="utf-8")
    os.replace(scratch, FLEET_COUNTERS_PATH)
    invalidate()
    return since


def counted_since(row: dict, baseline: float | None) -> float | None:
    """The baseline, when this location's Hub honoured it; `None` when it counts its whole history
    (no reset, or a Hub older than `activity_since`), so the page never claims a reset that did not happen."""
    window = ((row.get("activity") or {}).get("window") or {}).get("since_s")
    if baseline is None or not isinstance(window, (int, float)):
        return None
    return baseline if window >= baseline - 1 else None


def warnings_setting() -> dict:
    return {
        "hidden": hidden_warning_kinds(),
        "hideable": [{"kind": k, "title": WARNING_KIND_TITLES[k]} for k in HIDEABLE_WARNING_KINDS],
    }


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
    clean: list[str] = []
    for warning in warnings if isinstance(warnings, list) else []:
        text = warning.get("message") or warning.get("text") if isinstance(warning, dict) else warning
        # A Hub that repeats a sentence (it re-adds the same warning each sweep) states it once.
        if text and str(text).strip() and str(text).strip() not in clean:
            clean.append(str(text).strip())
    swap = _num(raw.get("swap_used_gb"))
    trash = _num(raw.get("trash_gb"))
    return {
        "warning_items": [{"kind": warning_kind(text), "text": text} for text in clean],
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


def machine_connection(machine: dict) -> dict | None:
    """One Mac's network link, normalised from the block its Hub publishes. `None` when it said nothing.

    The owner, 2026-10-28: a Mac linked at 100 Mb/s (or 10) through a failing cable answers every
    other field in this file — it is `reachable`, `ready`, and takes work — so nothing could show
    that its link, not the Mac, was the reason its jobs crawled. Mac 1301 was measured at
    `100baseTX` and had been `10baseT` before that on the same old cable.

    **Only the Hub's own reading is published.** The interface is the one its kernel says carries
    the default route, the kind is the hardware port its OS names for that device and the speed is
    the media that port states it negotiated (`components/hub/backend/network_link.py`). The
    Gateway reads those; it never infers a speed from an address, a name or a model, and a Mac whose
    Hub is older than the block (0.6.206) reports `None` here — unknown, which is not "fine".

    `slow` is the one verdict it adds, because it is the reason the field exists: an Ethernet link
    under 1 Gb/s, or at half duplex, is a cable or a port to look at. Wi-Fi is not called slow — a
    Mac on Wi-Fi is a different fact and the page says so instead.
    """
    raw = machine.get("connection")
    if not isinstance(raw, dict) or not raw:
        return None
    status = str(raw.get("status") or "").strip().lower()
    kind = str(raw.get("kind") or "").strip().lower() or None
    reason = str(raw.get("reason") or "").strip() or None
    # A block that claims a measurement but names no interface and no kind is not one: the two
    # facts that make it a reading are absent, so it is treated as unreadable rather than drawn
    # as a link of unknown type.
    if status == "measured" and not (raw.get("interface") or kind):
        status = "unmeasured"
    speed = _num(raw.get("speed_mbps"))
    duplex = str(raw.get("duplex") or "").strip().lower() or None
    wifi = raw.get("wifi") if isinstance(raw.get("wifi"), dict) else {}
    wifi_up = wifi.get("up") if isinstance(wifi.get("up"), bool) else None
    wifi_rate = _num(wifi.get("rate_mbps"))
    # A reading the Hub could not make is unmeasured **with its reason**, never a link of 0 Mb/s.
    if status != "measured":
        return {"status": "unmeasured", "reason": reason or "Its Hub could not read the link.",
                "interface": None, "kind": None, "speed_mbps": None, "duplex": None,
                "media": None, "link_up": None, "wifi_up": None, "wifi_rate_mbps": None,
                "slow": False, "text": "—"}
    slow = kind == "ethernet" and (
        (speed is not None and speed < 1000) or duplex == "half")
    return {
        "status": "measured",
        "reason": reason,
        "interface": str(raw.get("interface") or "").strip() or None,
        "kind": kind,
        "speed_mbps": None if speed is None else int(speed),
        "duplex": duplex,
        "media": str(raw.get("media") or "").strip() or None,
        "link_up": raw.get("link_up") if isinstance(raw.get("link_up"), bool) else None,
        # Whether the Mac's Wi-Fi is *also* up beside its primary link, which is worth knowing:
        # an Ethernet Mac that has silently fallen back to Wi-Fi looks identical otherwise.
        "wifi_up": wifi_up,
        "wifi_rate_mbps": None if wifi_rate is None else int(wifi_rate),
        "slow": slow,
        "text": _connection_text(kind, speed, duplex, wifi_rate, reason),
    }


def _connection_text(kind: str | None, speed: float | None, duplex: str | None,
                     wifi_rate: float | None, reason: str | None) -> str:
    """The one sentence the Fleet page draws, built once so every reader says the same thing."""
    if kind == "ethernet":
        if speed is None:
            # The port is the primary link but states no negotiated media: a cable out, or a port
            # forcing a media nothing answers. Never drawn as a speed of 0.
            return "Ethernet · speed not reported"
        return f"Ethernet {int(speed)} Mb/s" + (f" {duplex}" if duplex else "")
    if kind == "wifi":
        return "Wi-Fi" + (f" {int(wifi_rate)} Mb/s" if wifi_rate else "")
    if kind == "other":
        return "Other connection" + (f" {int(speed)} Mb/s" if speed else "")
    return reason or "—"


def _connection_summary(machines: list[dict]) -> dict:
    """The fleet-wide line: how many Macs are on a slow Ethernet link, on Wi-Fi, and unknown.

    Counted from the machine rows the page draws, so the headline and the table can never
    disagree about how many Macs there are — the mistake `_summarise` records for online/offline.
    A Mac whose Hub does not publish the block is **not** counted as fine: it is `unmeasured`, and
    the page says how many of those there are rather than leaving them out of the total.
    """
    slow: list[str] = []
    wifi: list[str] = []
    ethernet = 0
    unmeasured = 0
    for machine in machines:
        connection = machine.get("connection")
        if not isinstance(connection, dict):
            unmeasured += 1
            continue
        if connection.get("status") != "measured":
            unmeasured += 1
        elif connection.get("kind") == "wifi":
            wifi.append(str(machine.get("label") or machine.get("machine") or ""))
        elif connection.get("kind") == "ethernet":
            ethernet += 1
            if connection.get("slow"):
                slow.append(str(machine.get("label") or machine.get("machine") or ""))
    return {"slow_ethernet": len(slow), "slow_machines": slow, "wifi": len(wifi),
            "wifi_machines": wifi, "ethernet": ethernet, "unmeasured": unmeasured,
            "total": len(machines)}


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
                 "last_start_at", "app_reporting", "stale", "observed_at", "reported_at", "last_update_error",
                 "macos_trash_auto_empty")

#: Where a Studio Hub update stopped (Hub 0.6.152 `startup.last_update_error.phase`).
_UPDATE_ERROR_PHASES = frozenset({"verifying", "staging", "downloading", "installing"})


def _update_error_fact(raw: object) -> dict | None:
    """A Mac's last failed Studio Hub update `{phase, reason, build, at}`, or `None` - another machine's word.

    Only those keys pass: `phase` one of the words the Hub defines (an unfamiliar one is `None`, never
    guessed), `reason` one line of at most 300 characters, `build` at most 40, `at` a real epoch number.
    Nothing usable (no reason and no known phase) means no failure is claimed.
    """
    if not isinstance(raw, dict):
        return None

    def text(key: str, limit: int) -> str | None:
        value = raw.get(key)
        return (" ".join(value.split())[:limit] or None) if isinstance(value, str) else None

    at = raw.get("at")
    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and abs(at) != float("inf") and at == at else None
    phase = raw.get("phase")
    phase = phase if isinstance(phase, str) and phase in _UPDATE_ERROR_PHASES else None
    reason = text("reason", 300)
    if phase is None and reason is None:
        return None
    return {"phase": phase, "reason": reason, "build": text("build", 40), "at": at}


def _macos_trash_fact(machine: dict) -> dict | None:
    """What this Mac's macOS Trash holds (Studio Hub 0.6.243), its own column beside the Hub folder.

    Every retirement a Hub makes ends in Finder's Trash, so a Mac whose Trash is never emptied fills
    its disk even after its Studio Hub Trash Folder is empty. The Hub states the count and bytes per
    Mac; `None` from a Hub that does not — unknown, never 0. Read from the Hub's object, and from a
    flat spelling a first Hub cut may use, so one naming mismatch does not hide the figure.
    """
    raw = machine.get("macos_trash")
    raw = raw if isinstance(raw, dict) else {}

    def count_or_bytes(value: object) -> int | None:
        # A boolean is not a size, and neither is a negative: unstated, never 0.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value) if value >= 0 else None

    # The whole macOS Trash (`count`/`bytes`) is what Hub 0.6.243's empty-trash removes; the
    # Hub-moved-items spelling of a first cut is still read so one naming mismatch hides nothing.
    count = count_or_bytes(raw.get("count", raw.get("hub_items_count", machine.get("macos_trash_count"))))
    size = count_or_bytes(raw.get("bytes", raw.get("hub_items_bytes", machine.get("macos_trash_bytes"))))
    # Hub 0.6.243 states why when it cannot read the Trash at all (`FULL_DISK_ACCESS_NEEDED`,
    # `TRASH_UNREADABLE`): that is a reason, not a size, and never 0.
    code = raw.get("code")
    code = code.strip() if isinstance(code, str) and code.strip() else None
    if count is None and size is None and code is None:
        return None
    return {"count": count, "bytes": size, "code": code}


def _hub_trash_fact(machine: dict) -> dict | None:
    """What this Mac's Studio Hub Trash Folder holds, as the Hub states it (Studio Hub 0.6.243).

    `hub_trash: {count, bytes}` on the machine row; `None` from a Hub that does not state it —
    unknown, never 0. A boolean or a negative is not a size.
    """
    raw = machine.get("hub_trash")
    if not isinstance(raw, dict):
        return None

    def size(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        return int(value)

    count, total = size(raw.get("count")), size(raw.get("bytes"))
    if count is None and total is None:
        return None
    return {"count": count, "bytes": total}


def _startup_fact(machine: dict, node: dict) -> dict | None:
    """What starts when this Mac starts, as the Hub reported it - or `None` when it did not.

    `None` is *unknown*, never "off": a Hub older than 0.6.127 says nothing about it, and so does
    a Mac nobody has heard from since. A report the controller is repeating from before the Mac
    went quiet carries `stale: true`. The two settings are published as the booleans the Hub sent;
    anything else becomes `None`.

    `macos_trash_auto_empty` (Hub 0.6.187) rides in the same object: macOS's own "Remove items from
    the Trash after 30 days", which every Hub retirement ends up in. `None` there is a Hub too old
    to say, never "off".
    """
    raw = machine.get("startup") if isinstance(machine.get("startup"), dict) else node.get("startup")
    if not isinstance(raw, dict):
        return None
    out: dict = {key: raw.get(key) for key in _STARTUP_KEYS}
    for key in ("open_at_login", "open_at_login_saved", "start_services_on_launch", "app_reporting", "stale",
                "macos_trash_auto_empty"):
        if not isinstance(out[key], bool):
            out[key] = None
    running = raw.get("services_running")
    out["services_running"] = (
        {"up": running.get("up"), "total": running.get("total"), "studios": running.get("studios") or {}}
        if isinstance(running, dict) else None)
    out["last_update_error"] = _update_error_fact(raw.get("last_update_error"))
    return out


#: Why a Mac's Studio Hub last stopped, as the Mac itself recorded it (`last_stop` on a machine or node row).
_LAST_STOP_REASONS = frozenset({"quit", "crashed", "updated", "force_quit", "low_memory", "mac_restarted"})


def _last_stop_fact(machine: dict, node: dict) -> dict | None:
    """The last time this Mac's Studio Hub stopped and why, as that Mac reported it - or `None`.

    It is another machine's word, so only the known keys pass and each is checked: `at` must be a
    real epoch number (otherwise nothing is published), `reason` one of the words the Hub defines
    (an unfamiliar one becomes `None`, never a guess), `label` a short single line, the booleans real
    booleans. A fact with neither a known reason nor a label says nothing and is dropped.
    """
    raw = machine.get("last_stop") if isinstance(machine.get("last_stop"), dict) else node.get("last_stop")
    if not isinstance(raw, dict):
        return None
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or at != at or abs(at) == float("inf"):
        return None
    reason = raw.get("reason") if raw.get("reason") in _LAST_STOP_REASONS else None
    label = " ".join(str(raw["label"]).split())[:80] if isinstance(raw.get("label"), str) else ""
    if reason is None and not label:
        return None
    return {"reason": reason, "at": float(at), "label": label or None,
            "relaunched": raw["relaunched"] if isinstance(raw.get("relaunched"), bool) else None,
            "stale": raw["stale"] if isinstance(raw.get("stale"), bool) else None}


#: Workstation mode (Hub, owner request 2026-10-03): a shop's own Mac lends the fleet its idle time.
_WORKSTATION_STATES = frozenset({"off", "working", "standing_by", "yielded", "quiet_hours"})
#: The states in which the Mac is the shop's for now: grey and temporary, never offline, attention or an alarm.
WORKSTATION_RESTING = frozenset({"standing_by", "yielded", "quiet_hours"})


def _workstation_fact(machine: dict, node: dict) -> dict | None:
    """This Mac's workstation-mode settings and state, as its Hub reported them - or `None` (Hub too old).

    Another machine's word, like `last_stop`: only the known keys pass, each checked for its type (a wrong
    one becomes `None`), and an unfamiliar state is unknown, never a guess.
    """
    raw = machine.get("workstation") if isinstance(machine.get("workstation"), dict) else node.get("workstation")
    if not isinstance(raw, dict):
        return None

    def flag(key: str) -> bool | None:
        return raw[key] if isinstance(raw.get(key), bool) else None

    def number(key: str) -> float | None:
        value = raw.get(key)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")
        return value if ok else None

    def count(key: str) -> int | None:
        value = number(key)
        return None if value is None else int(value)

    hours, reason = raw.get("quiet_hours"), raw.get("reason")
    return {
        "enabled": flag("enabled"), "idle_only": flag("idle_only"), "idle_minutes": count("idle_minutes"),
        "step_aside": flag("step_aside"), "resume_after_minutes": count("resume_after_minutes"),
        "start_free_gb": number("start_free_gb"),
        "quiet_hours": [h.strip()[:16] for h in hours if isinstance(h, str)][:6] if isinstance(hours, list) else None,
        "hide_menu_bar_icon": flag("hide_menu_bar_icon"),
        "state": raw.get("state") if raw.get("state") in _WORKSTATION_STATES else None,
        "reason": " ".join(reason.split())[:120] or None if isinstance(reason, str) else None,
        "yields_today": count("yields_today"), "jobs_today": count("jobs_today"),
        "stale": flag("stale"),
    }


def _machine_priority_order(machine: dict, node: dict) -> list[str]:
    """The ordered function preference this Mac stated, with node and legacy fallbacks."""
    if "priority_order" in machine:
        return _priority_order(machine)
    if "priority_order" in node:
        return _priority_order(node)
    if "chat_priority" in machine:
        return _priority_order(machine)
    return _priority_order(node)


def _function_enabled(machine: dict) -> dict[str, bool]:
    """One Mac's saved on/off switches (`function_enabled`), cut to real booleans on known functions.

    **Absent = on, and only a real boolean `false` is off** (owner, 2026-10-28: "each generation
    should actually have a cross to switch them on or off. but defaults to on."). A malformed or
    non-boolean value is dropped rather than guessed, so a damaged row keeps taking work.
    """
    value = machine.get("function_enabled")
    if not isinstance(value, dict):
        return {}
    allowed = _PRIORITY_FUNCTIONS | {"vision", "embeddings"}
    return {kind: on for kind, on in value.items()
            if isinstance(kind, str) and kind in allowed and type(on) is bool}


def _machine_function_enabled_map(machine: dict, node: dict) -> dict[str, bool]:
    """The switch map this Mac stated, preferring the overview machine row over the node row."""
    if "function_enabled" in machine:
        return _function_enabled(machine)
    return _function_enabled(node)


def _machine_function_enabled(machine: dict, node: dict, kind: str) -> bool:
    """Whether this Mac takes `kind` at all (owner, 2026-10-28). Absent = ON; only `False` is off."""
    return _machine_function_enabled_map(machine, node).get(kind) is not False


def workstation_resting(row: dict) -> bool:
    """A workstation Mac that is the shop's for now (standing by, stepped aside, quiet hours), freshly reported.

    Such a Mac is temporarily busy, not offline, not "needs attention" and not a Telegram alarm. A block the
    controller repeats for a Mac it can no longer reach (`stale`) says nothing about now.
    """
    block = row.get("workstation")
    return isinstance(block, dict) and block.get("state") in WORKSTATION_RESTING and block.get("stale") is not True


# --- A Lite Mode Mac that is switched off is expected (owner, 2026-10-08) -----------------------------------
#
# Shop Macs are on by day, off at night and off for holidays. Off, a Mac reports nothing, so its Lite Mode is
# what the Gateway last heard (`lite_mode_known.json`, one bool per `controller/machine`) unless a change is
# waiting for it (`lite_mode_pending.json`), which wins: it is the owner's latest word. Such a Mac reads `lite_off`:
# no warning, not offline in any count, never a Telegram alarm. It still gets no work (it is not reachable).
LITE_MODE_KNOWN_PATH = Path(str(DATA_DIR)) / "lite_mode_known.json"
#: Off longer than this, a Lite Mode Mac gets one quiet info line (never a warning).
# ponytail: one fixed 14 days (a holiday is 5); a per-Mac number if a shop ever closes for longer.
LITE_OFF_NOTE_S = 14 * 86400


def _lite_mode_known() -> dict[str, bool]:
    try:
        value = json.loads(LITE_MODE_KNOWN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in value.items() if isinstance(v, bool)} if isinstance(value, dict) else {}


def remember_lite_mode(machines: list[dict]) -> None:
    """Keep each Mac's last reported Lite Mode on/off, so it is still known while the Mac is switched off."""
    known = _lite_mode_known()
    seen = {f"{m.get('location_id')}/{m.get('machine')}": m["workstation"]["enabled"] for m in machines
            if isinstance(m.get("workstation"), dict) and isinstance(m["workstation"].get("enabled"), bool)}
    if all(known.get(key) is value for key, value in seen.items()):
        return
    known.update(seen)
    LITE_MODE_KNOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = LITE_MODE_KNOWN_PATH.with_name(LITE_MODE_KNOWN_PATH.name + ".tmp")
    scratch.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
    os.replace(scratch, LITE_MODE_KNOWN_PATH)


def _purge_lite_mode_known(controller_id: str, machine_id: str | None = None) -> int:
    known = _lite_mode_known()
    if not known:
        return 0
    prefix = f"{controller_id}/"
    exact = f"{controller_id}/{machine_id}" if machine_id else None
    gone = [k for k in known if (k == exact if exact is not None else k.startswith(prefix))]
    if not gone:
        return 0
    for k in gone:
        known.pop(k, None)
    LITE_MODE_KNOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    scratch = LITE_MODE_KNOWN_PATH.with_name(LITE_MODE_KNOWN_PATH.name + ".tmp")
    scratch.write_text(json.dumps(known, sort_keys=True), encoding="utf-8")
    os.replace(scratch, LITE_MODE_KNOWN_PATH)
    return len(gone)


def _lite_mode_on(key: str, workstation: dict | None, pending: dict, known: dict[str, bool]) -> bool:
    """Lite Mode as best known: a change waiting for this Mac, else its report, else its last report."""
    waiting = (pending.get(key) or {}).get("settings") or {}
    for value in (waiting.get("enabled"), (workstation or {}).get("enabled"), known.get(key)):
        if isinstance(value, bool):
            return value
    return False


def _current_job(machine: dict, entry: dict) -> dict | None:
    """The job a node is running right now, **when the Hub says so** (Fleet Groups "Doing now").

    The Hub reports a machine as `working` and the batches without saying which machine runs which, so
    the Gateway cannot work this out. Hubs that state it carry `current` on the machine row or on its
    activity entry as `{batch_id, model, label, started_at, studio}`; this republishes exactly those
    keys and nothing the node chose to add. `None` is *unknown* - the page then shows the busy studios
    only - and never "idle".
    """
    # Hub 0.6.1xx names it `current_job` (`{job_id, modality, model, label, started_at}`) on the activity
    # entry and the node row; `current` is the older spelling. Either is read.
    raw = next((value for value in (machine.get("current"), machine.get("current_job"),
                                    entry.get("current"), entry.get("current_job"))
                if isinstance(value, dict)), None)
    if not isinstance(raw, dict):
        return None
    out = {
        "batch_id": raw.get("batch_id") or raw.get("id") or raw.get("job_id"),
        "model": raw.get("model"),
        "label": raw.get("label"),
        "studio": raw.get("studio") or raw.get("modality"),
        "started_at": raw.get("started_at") if isinstance(raw.get("started_at"), (int, float)) else None,
    }
    return out if any(value not in (None, "") for value in out.values()) else None


#: What a Hub calls its own Mac when nobody named it. A remote controller reports itself under this
#: placeholder too, so on its own it says nothing about *which* Mac a row is.
_GENERIC_MACHINE_LABELS = frozenset({"this mac", "this-mac", "this computer", "local", "localhost", "mac"})


#: A Mac counts as "low on memory" as a *fault* only when it cannot work (2026-10-05 owner rule: 1.6-1.8 GB free
#: while generating on an 8 GB Mac is normal, and a warning that is always on is never read).
MEMORY_IDLE_FAULT_S = 300.0
#: (location id, machine) -> when it first read idle-and-low in this unbroken stretch. In memory only: a Gateway
#: restart restarts the five minutes, which errs towards silence.
_MEMORY_LOW_SINCE: dict[tuple[str, str], float] = {}


def memory_fault(row: dict, now: float) -> str | None:
    """Why this Mac's memory is a fault, or `None` (the meter may still be amber; that is information).

    A fault is one of: macOS memory pressure critical (when the Hub says so), or idle (no job,
    not `working`) with free memory under the start floor for 5 minutes -- it cannot take work.
    Low free memory while a job runs is never one. Cumulative swap usage remains disk information;
    without a rate it does not say that the Mac is under memory pressure now.
    """
    key = (str(row.get("location_id") or ""), str(row.get("machine") or ""))
    free, total = _num(row.get("available_gb")), _num(row.get("memory_gb"))
    if row.get("reachable") is False or free is None or not total:
        _MEMORY_LOW_SINCE.pop(key, None)
        return None
    busy = bool(row.get("current")) or str(row.get("state") or "").lower() == "working"
    floor = _num((row.get("workstation") or {}).get("start_free_gb")) or total * 0.20
    since = None
    if busy or free >= floor:
        _MEMORY_LOW_SINCE.pop(key, None)
    else:
        since = _MEMORY_LOW_SINCE.setdefault(key, now)
    if str(row.get("memory_pressure") or "").lower() == "critical":
        return "macOS memory pressure is critical"
    if since is not None and now - since >= MEMORY_IDLE_FAULT_S:
        return f"idle with only {free:.1f} GB free for {int((now - since) // 60)} min: it cannot take work"
    return None


def _is_generic_label(text: object) -> bool:
    clean = str(text or "").strip().lower()
    return not clean or clean in _GENERIC_MACHINE_LABELS


def machine_label(row: dict, machine: dict, node: dict | None = None) -> str:
    """The name a machine is shown under: roster name, then the Hub's own label, then its hostname.

    **"This Mac" is reserved for the Gateway's own Mac.** Every controller's own machine comes
    through labelled "This Mac" (the Hub's default), so a fleet of 45 Macs read as 20 rows of
    "This Mac"; a placeholder is skipped in favour of the roster name, the machine id (the
    hostname) and, for a controller whose own id is just `local`, the controller's roster label.
    Only the Gateway's own location -- and only its own row there, the one with no roster entry --
    may be "This Mac", under whatever the owner renamed it to.
    """
    mid = str(machine.get("machine") or "").strip()
    hub = str(machine.get("hub_label", machine.get("label")) or "").strip()
    roster = str((node or {}).get("name") or "").strip()
    if row.get("local") and not node:
        return hub if not _is_generic_label(hub) else (str(row.get("label") or "").strip() or local_label())
    for candidate in (roster, hub, mid):
        if not _is_generic_label(candidate):
            return candidate
    return str(row.get("label") or "").strip() or mid or "Mac"


def _qualify_location_labels(row: dict) -> None:
    """Give raw location rows the same display names as the flat machine list."""
    nodes = {str(node.get("id")): node for node in row.get("nodes") or []
             if isinstance(node, dict) and node.get("id")}
    row["machines"] = [
        {**machine, "hub_label": machine.get("hub_label", machine.get("label")),
         "label": machine_label(row, machine, nodes.get(str(machine.get("machine") or "")))}
        for machine in row.get("machines") or []
    ]


#: How long a Mac may stay behind after its controller received the newest release before it reads
#: "Behind" rather than "Updating…". Measured 2026-10-01: idle Macs installed a published release
#: within 5-15 minutes; a busy one waits for its work to finish, so a working Mac stays "Updating…".
UPDATE_GRACE_S = 45 * 60

#: How recent a failed job must be to make a Mac "need attention" (the Hub's own `JUST_FINISHED_S`).
ATTENTION_ERROR_S = 15 * 60
_LONG_IDLE_S = 2 * 3600


def _version_key(version: object) -> tuple[int, ...] | None:
    text = str(version or "").strip()
    return tuple(int(part) for part in text.split(".")) if re.fullmatch(r"\d+(?:\.\d+)*", text) else None


def latest_hub_release(rows: list[dict], push: dict | None = None) -> dict | None:
    """Always `None`: this Gateway no longer publishes Studio Hub app releases.

    Owner rule 2026-11-13 — only Studio Launcher KH installs updates, for every app — removed the
    release publisher and its signed descriptors, so "the newest Hub this Gateway published" no
    longer exists to compare a Mac against. `version_status` answers `None` and the Fleet shows each
    Mac's own reported version without a latest/behind claim. The Launcher feed is the only source
    of update truth.
    """
    return None

def version_status(version: object, latest: dict | None, *, reachable: object = True,
                   controller_holds_latest: bool = False, busy: bool = False,
                   since: float | None = None, now: float | None = None) -> str | None:
    """`latest` / `updating` / `behind` / `unknown` for one Mac, or `None` when nothing was published.

    * **latest** — at (or past) the newest release this Gateway published.
    * **updating** — behind, but its controller already holds the newest release and the Mac is
      still inside the install window, or is busy (a Mac finishes its work before it installs).
    * **behind** — behind and not getting there by itself: its controller lacks the release, or the
      window passed (an app not in /Applications, a Mac that never went idle long enough).
    * **unknown** — the Mac is not answering, or stated no version.

    `since` is when *its* controller got the release (the signed `published_at` of the copy it holds:
    each publish signs anew, so a group published to after the pilot has a later one); the newest
    release's own time stands in when the controller did not say.
    """
    if not latest or _version_key(latest.get("version")) is None:
        return None
    mine = _version_key(version)
    if reachable is False or mine is None:
        return "unknown"
    if mine >= _version_key(latest["version"]):  # type: ignore[operator]
        return "latest"
    if controller_holds_latest:
        since = _num(since) if since is not None else _num(latest.get("published_at"))
        moment = time.time() if now is None else now
        if busy or since is None or moment - since < UPDATE_GRACE_S:
            return "updating"
    return "behind"


def stamp_latest(rows: list[dict], push: dict | None = None) -> dict | None:
    """Put the newest published release on every location row, so `machines_flat` can rate each Mac."""
    latest = latest_hub_release(rows, push)
    for row in rows:
        row["hub_latest"] = latest
    return latest


def stamp_controller_versions(rows: list[dict], machines: list[dict]) -> None:
    """Each location's own Mac's `version_status`, for the Locations tab and the group header."""
    for row in rows:
        own = next((m for m in machines if m.get("location_id") == row.get("id")
                    and m.get("node_state") is None and m.get("node_role") is None), None)
        row["version_status"] = (own or {}).get("version_status")


def _running_studios(startup: dict | None) -> set[str] | None:
    """The studios a Mac says it runs itself (`startup.services_running.studios`), or `None` if unsaid."""
    if not isinstance(startup, dict) or startup.get("stale"):
        return None
    running = startup.get("services_running")
    studios = running.get("studios") if isinstance(running, dict) else None
    if not isinstance(studios, dict) or not studios:
        return None
    return {str(name).strip().lower() for name in studios}


def _not_installed(worker: dict, running: set[str] | None) -> bool:
    """A worker the controller lists for a Mac that does not run that studio at all.

    Hub ≤ 0.6.132 froze a node's studios at join time, so every Mac that joined while Video shipped
    kept a `video` worker on its controller after Video was retired: offline for ever, never seen,
    on a Mac whose own report lists five studios and no Video. That is not a fault anyone can fix
    on the Mac, and it is not shown as one (Hub 0.6.133 drops it from the roster itself).
    """
    if running is None or worker.get("online") is not False:
        return False
    names = {str(worker.get(key) or "").strip().lower() for key in ("modality", "studio")}
    return not (names & running)


def _studio_word(worker: dict) -> str:
    name = str(worker.get("modality") or worker.get("studio") or "a").strip()
    return {"transcription": "Subtitle", "tts": "Voice"}.get(name, name.capitalize())


def _attention(entry: dict, workers: list[dict], phantom: list[dict], health: object,
               now: float) -> tuple[str, str | None, str | None]:
    """Re-read a Hub `needs_attention` against what can be seen: `(state, why, code)`.

    The Hub says "needs attention" for a reachable Mac with any studio down, and (≤ 0.6.132) for any
    Mac whose newest job failed, however long ago. Both made a healthy idle fleet read amber: the
    "down" studio was a Video worker that no longer exists, and the failure was last night's. So
    the verdict stands only with a cause someone can act on — a studio that should run and is not,
    a failure in the last 15 minutes (not a TTS_SECTION_* quality rejection, which the Hub re-makes,
    nor a request too long for the model),
    a stalled job, a health word — and says which. When the only
    cause is one of those two artefacts, the state is worked out again the way the Hub would.

    **The code travels with the sentence.** Five of these six reasons are this Gateway's own read of
    the Mac (a studio down, a job stalled, a job that failed), and quoting them as "Its Hub reports a
    problem this Gateway cannot see from outside" told the owner to go and look somewhere else. Only
    the last one is genuinely the Hub's word alone. Each reason names its code so `machine_causes`
    can state a plain sentence of its own, keeping `why` as the evidence.
    """
    latest = entry.get("latest") if isinstance(entry.get("latest"), dict) else {}
    finished = _num(latest.get("finished_at")) or _num(latest.get("observed_at"))
    age = None if finished is None else max(0.0, now - finished)
    down = [w for w in workers if w.get("online") is False and w not in phantom]
    if down:
        names = ", ".join(sorted({_studio_word(w) for w in down}))
        return "needs_attention", f"{names} studio not running", "studio_down"
    stuck = [w for w in workers if w.get("online") and w.get("ready") is False and not w.get("busy")
             and not w.get("drained") and not w.get("maintenance")]
    if stuck:
        return ("needs_attention",
                ", ".join(sorted({_studio_word(w) for w in stuck})) + " studio not ready",
                "studio_not_ready")
    stalled = _num(entry.get("stalled_seconds"))
    if stalled:
        return "needs_attention", f"No progress for {int(stalled // 60)} min", "job_stalled"
    if str(health or "up").strip().lower() not in {"up", ""}:
        return "needs_attention", f"Health: {health}", "health_word"
    reason = str(latest.get("error_code") or "").strip()
    # A quality rejection (TTS_SECTION_*) means the Hub's check caught one bad piece and re-made it.
    failed_now = (latest.get("state") == "error" and age is not None and age < ATTENTION_ERROR_S
                  and not reason.startswith("TTS_SECTION_")
                  # A request too long for the model is the caller's, and says nothing about this Mac.
                  and reason != "context_exceeded")
    if failed_now:
        return ("needs_attention", "Last job failed" + (f": {reason[:80]}" if reason else ""),
                "recent_job_failed")
    old_failure = latest.get("state") == "error"
    if not phantom and not old_failure:
        # The Hub saw something this Gateway cannot: keep its verdict, and say only that.
        return "needs_attention", "Flagged by its Hub", "hub_flagged"
    if isinstance(entry.get("current_job"), dict):
        return "working", None, None
    if age is None:
        return "ready", None, None
    if latest.get("state") == "done" and age < ATTENTION_ERROR_S:
        return "just_finished", None, None
    return ("long_idle" if age >= _LONG_IDLE_S else "ready"), None, None


def project_developer_gate_inventory(raw: Any, *, controller_id: str | None,
                                     node_id: str) -> dict[str, Any] | None:
    """Preserve only closed facts bound to this controller and physical node."""
    from .gate_contract import decode, encoded, GateError
    from .gate_transport import JSON_CEILING
    if (not isinstance(raw, dict) or not controller_id
            or raw.get("controller_id") != controller_id or raw.get("node_id") != node_id):
        return None
    try:
        return decode(encoded(raw), "inventory", ceiling=JSON_CEILING)
    except (GateError, ValueError, TypeError, RecursionError):
        return None


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
    which is the owner's to change. The controller's own Mac has no node row;
    only its pause flag comes from its own authenticated work policy. Other node
    fields stay `None`, and missing policy evidence stays unknown.
    """
    # One settings read for the whole fan-out, not one per machine row.
    from . import concurrency as _conc
    _attention_max_age = float(_conc.queue_wait_budget().get(
        "attention_max_age_seconds", _conc.DEFAULT_ATTENTION_MAX_AGE_S))
    out: list[dict] = []
    baseline = counters_since()
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
            paused = row.get("local_machine_paused") if mid == "local" else node.get("paused")
            return {
                "node_state": node.get("state"),
                "node_detail": node.get("detail"),
                # **Why** it is unreachable, when the Hub states it as a field. See
                # `_node_reach_reason` for the fallback that covers Hubs predating it.
                "node_reason": node.get("reason"),
                "node_role": node.get("role"),
                "node_last_seen_at": node.get("last_seen_at"),
                "node_address": node.get("address"),
                # The Hub's own flag for a Mac that has not answered **once** since it joined
                # (`fleet_nodes.probe`; its own app draws it as "never connected since it joined").
                # A bool or nothing, like every other field from another machine.
                "node_never_connected": (node.get("never_connected")
                                         if isinstance(node.get("never_connected"), bool) else None),
                # **How the controller knows the Mac is there.** `"probe"` is a dial it made;
                # `"check-in"` is the Mac's own authenticated call home, which is the only channel
                # a Shops Mac has — sharing a Tailscale node works one way, so its controller can
                # never dial it. `reachable` alone cannot tell those apart, and an operator looking
                # at a Mac that never answers a dial needs to know the controller is hearing from
                # it at all. `None` from a Hub older than 0.6.193, never read as `check-in`.
                "node_via": node.get("via"),
                # The owner paused this Mac by hand on its controller (Hub 0.6.135); `None` when the
                # Hub does not say, never read as "not paused".
                "node_paused": paused if type(paused) is bool else None,
            }

        # Each Mac's model readiness, from the Hub's own `model-health` rows (absent on an older Hub).
        model_states: dict[str, list[str]] = {}
        for health in row.get("model_health") or []:
            if isinstance(health, dict) and health.get("machine"):
                model_states.setdefault(str(health["machine"]), []).append(str(health.get("state") or ""))
        workers_by_machine: dict[str, list[dict]] = {}
        for worker in row.get("workers") or []:
            if isinstance(worker, dict):
                workers_by_machine.setdefault(str(worker.get("machine") or ""), []).append(worker)
        latest = row.get("hub_latest") if isinstance(row.get("hub_latest"), dict) else None
        counted = counted_since(row, baseline)
        held = row.get("release") if isinstance(row.get("release"), dict) else {}
        controller_holds_latest = bool(latest) and _version_key(held.get("version")) is not None \
            and _version_key(held.get("version")) >= _version_key(latest.get("version"))  # type: ignore[operator]
        now = time.time()

        def health_fields(mid: str, machine: dict, entry: dict, startup: dict | None,
                          workstation: dict | None) -> dict:
            """State (re-read where the Hub's verdict has no visible cause), version and its badge."""
            node = nodes.get(mid) or {}
            workers = workers_by_machine.get(mid, [])
            phantom = [w for w in workers if _not_installed(w, _running_studios(startup))]
            state, why, code = entry.get("state"), None, None
            if str(state or "").lower() == "needs_attention":
                state, why, code = _attention(entry, workers, phantom, machine.get("health"), now)
            # **A Mac with no model it can run is not "Ready".** Its studios are up, so the Hub calls
            # it ready, but routing never picks it: 1108 sat "Ready · 0 done · 0 failed" for days with
            # no model files at all (every model `download_required`) while its siblings worked.
            states = model_states.get(mid)
            if states and "ready" not in states and str(state or "").lower() in {
                    "ready", "long_idle", "just_finished"}:
                missing = states.count("download_required")
                state, why, code = "needs_attention", (
                    f"No model is ready on this Mac ({missing} of {len(states)} need downloading), "
                    "so it is never given a job" if missing
                    else "No model is ready on this Mac, so it is never given a job"), "no_model_ready"
            # A workstation Mac the shop is using (or in its quiet hours) is busy for now, not offline and not
            # needing attention: its state is the workstation's word. A job still finishing there stays `working`.
            if workstation_resting({"workstation": workstation}) and str(state or "").lower() != "working":
                state, why, code = workstation["state"], None, None
            # A node's own Hub version, as its controller last read it; the controller's own Mac is
            # the controller's version.
            version = node.get("version") or (startup or {}).get("app_version") \
                or (row.get("version") if mid == "local" else None)
            limitation = _limitation_fields(entry)
            # "Some reporters missing" on a Mac whose only silent reporter is a studio it does not run
            # is that same artefact again, not a third finding.
            if phantom and limitation.get("limitation_code") == "partial_read" and not any(
                    w.get("online") is False and w not in phantom for w in workers):
                limitation = {key: None for key in limitation}
            reachable = machine.get("reachable") if machine else None
            # **When this verdict was measured.** A current row is measured at this fan-out's own
            # time; a carried row keeps its `as_of` so the page can say "not checked in since HH:MM"
            # rather than drawing a stale fault as live.
            attention_measured_at = now
            attention_source = "current"
            # Hub's relay may answer with source="unreachable" for a node it cannot reach (HTTP 502,
            # code LOCATION_UNREACHABLE): that is an un-measured row, not a fault — treat it like
            # a cached/carried row so the page draws "Not checked in since" instead of attention.
            node_source = node.get("source")
            if isinstance(node_source, str) and node_source.lower() == "unreachable":
                attention_source = "last_known"
            # Age the verdict: Hub pushes every 30 min, so a measurement older than the configured
            # ceiling is stale even if the row itself arrived fresh. Effective time prefers the Hub's
            # own measured_at, falls back to checked_in_at, then to this read's now (no regression
            # for Hubs predating these fields).
            _hub_measured = node.get("measured_at")
            _effective = (float(_hub_measured) if isinstance(_hub_measured, (int, float)) and not isinstance(_hub_measured, bool) and _hub_measured > 0
                          else (node.get("last_seen_at") if isinstance(node.get("last_seen_at"), (int, float)) and not isinstance(node.get("last_seen_at"), bool) and node.get("last_seen_at") and node.get("last_seen_at") > 0
                                else now))
            if attention_source == "current" and now - _effective > _attention_max_age:
                attention_source = "last_known"
                attention_measured_at = _effective
            elif attention_source == "last_known" and attention_measured_at == now:
                attention_measured_at = _effective
            return {
                "state": state,
                "attention": why,
                # The code that says *what* `attention` is, beside it on the row so the Likely-cause
                # cell can state a sentence of its own instead of quoting the Hub for a studio this
                # Gateway watched go down. `None` with `attention` set means an older row that never
                # carried one: the reader falls back to the Hub's own wording.
                "attention_code": code,
                "attention_measured_at": attention_measured_at,
                "attention_source": attention_source,
                "checked_in_at": node.get("last_seen_at"),
                "hub_version": version,
                "version_status": version_status(
                    version, latest,
                    reachable=False if str(state or "").lower() == "offline" else reachable,
                    controller_holds_latest=controller_holds_latest,
                    busy=str(state or "").lower() == "working",
                    since=_num(held.get("published_at")), now=now),
                "not_installed_studios": sorted({_studio_word(w) for w in phantom}) or None,
                **limitation,
            }

        seen: set[str] = set()
        for machine in row["machines"]:
            mid = str(machine.get("machine") or "")
            seen.add(mid)
            entry = activity.get(mid) or {}
            node = nodes.get(mid) or {}
            startup = _startup_fact(machine, nodes.get(mid) or {})
            priority_order = _machine_priority_order(machine, node)
            out.append({
                "location_id": row["id"],
                "location": row["label"],
                "location_local": row["local"],
                "location_reachable": row["reachable"],
                "machine": mid,
                "label": machine_label(row, machine, nodes.get(mid)),
                "reachable": machine.get("reachable"),
                "completed": entry.get("completed"),
                "failed": entry.get("failed"),
                "counted_since": counted,
                "average_seconds": machine.get("average_seconds"),
                "median_runtime_s": entry.get("median_runtime_s"),
                "chip": machine.get("chip"),
                "memory_gb": machine.get("memory_gb"),
                # What is free *now*, which is the number the Hub's own admission
                # uses and the one a reader needs: `memory_gb` is the machine's
                # total, so "Apple M4 · 17.18 GB" beside "118.11 GB free" read as
                # though the memory were free too.
                "available_gb": machine.get("available_gb"),
                "memory_pressure": machine.get("memory_pressure"),
                "disk_free_gb": machine.get("disk_free_gb"),
                "disk_total_gb": machine.get("disk_total_gb"),
                "disk": machine_disk(machine),
                # The items the Hub moved into macOS Trash (see `_macos_trash_fact`): its own count
                # and bytes, or None when it does not report them.
                "macos_trash": _macos_trash_fact(machine),
                # The Mac's Studio Hub Trash Folder, as the Hub states it (0.6.243, `hub_trash`),
                # so the Trash page is one fleet read rather than a request per Mac.
                "hub_trash": _hub_trash_fact(machine),
                "health": machine.get("health"),
                # How this Mac is actually connected (see `machine_connection`): the interface, its
                # kind and, for Ethernet, the media the OS says it negotiated. `None` from a Hub
                # older than 0.6.206 — unknown, never read as a healthy gigabit link.
                "connection": machine_connection(machine),
                "modalities": machine.get("modalities") or {},
                **node_fields(mid),
                "startup": startup,
                "last_stop": _last_stop_fact(machine, nodes.get(mid) or {}),
                "workstation": (workstation := _workstation_fact(machine, nodes.get(mid) or {})),
                "priority_order": priority_order,
                "chat_priority": bool(priority_order and priority_order[0] == "chat"),
                "function_enabled": _machine_function_enabled_map(machine, node),
                "current": _current_job(machine, entry),
                "installable_models": installable_models(row, mid, machine.get("memory_gb")),
                "developer_gate_inventory": project_developer_gate_inventory(
                    machine.get("developer_gate_inventory") or node.get("developer_gate_inventory"),
                    controller_id=row.get("controller_id"), node_id=mid,
                ),
                **health_fields(mid, machine, entry, startup, workstation),
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
                "label": machine_label(row, {"machine": mid}, nodes.get(mid)),
                "reachable": None,
                "completed": entry.get("completed"),
                "failed": entry.get("failed"),
                "counted_since": counted,
                "average_seconds": None,
                "median_runtime_s": entry.get("median_runtime_s"),
                "chip": None, "memory_gb": None, "available_gb": None,
                "disk_free_gb": None, "disk_total_gb": None, "disk": None, "macos_trash": None,
                "health": None,
                # A machine known only from the activity list carries no host snapshot, so its
                # link is unknown rather than borrowed from another Mac.
                "connection": None,
                "modalities": {},
                **node_fields(mid),
                "startup": _startup_fact({}, nodes.get(mid) or {}),
                "last_stop": _last_stop_fact({}, nodes.get(mid) or {}),
                "workstation": (workstation := _workstation_fact({}, nodes.get(mid) or {})),
                "priority_order": (priority_order := _machine_priority_order({}, nodes.get(mid) or {})),
                "chat_priority": bool(priority_order and priority_order[0] == "chat"),
                "function_enabled": _machine_function_enabled_map({}, nodes.get(mid) or {}),
                "current": _current_job({}, entry),
                "developer_gate_inventory": project_developer_gate_inventory(
                    nodes.get(mid, {}).get("developer_gate_inventory"),
                    controller_id=row.get("controller_id"), node_id=mid,
                ),
                **health_fields(mid, {}, entry, _startup_fact({}, nodes.get(mid) or {}), workstation),
            })
    now = time.time()
    from .startup_control import pending_lite_mode  # late: it imports this module

    pending, known = pending_lite_mode(), _lite_mode_known()
    for machine_row in out:
        # `no_hub` / `unreachable` / `None`: the one reading of why the controller could not reach it.
        machine_row["reach_reason"] = _node_reach_reason(machine_row) or None
        machine_row["memory_fault"] = memory_fault(machine_row, now)
        # Switched off, and in Lite Mode: expected, not a fault (see `LITE_MODE_KNOWN_PATH`).
        machine_row["lite_off"] = (
            str(machine_row.get("state") or "").lower() == "offline"
            and str(machine_row.get("node_state") or "").lower() != "unauthorized"
            and _lite_mode_on(f"{machine_row.get('location_id')}/{machine_row.get('machine')}",
                              machine_row.get("workstation"), pending, known))
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


def _never_connected_issue(machine: dict, machine_id: str) -> dict:
    """One Mac that has not answered its controller **once** since it joined.

    **Not the same fault as "switched off", and the two read identically.** A Mac that *was*
    working and is now off has a `last_seen_at` past its join; one that never has may be holding an
    address its controller cannot route to, or be on a network the controller cannot dial — and it
    can never correct itself, because the roster address is written at join and only at join. Both
    states are `offline`, so without this the owner is told to go and switch on a Mac that is on:
    measured 2026-10-20, the Shops office iMac sat at "offline · never connected" for 51 hours
    while the Hub's own app on the controller was already saying "never connected since it joined".

    The address travels in the message because it is the one fact that can be checked from either
    end, and it is what a wrong or foreign network address looks like.
    """
    address = str(machine.get("node_address") or "").strip()
    seen_at = _num(machine.get("node_last_seen_at"))
    name = machine.get("label") or machine_id
    # A never-connected row's `last_seen_at` is the join time itself (`fleet_nodes.join` seeds it),
    # so this reads as how long the Mac has been on the roster without ever answering.
    when = f"{int((time.time() - seen_at) // 86400)} days ago" if seen_at else "at join"
    return _issue(
        "warning", "machine_never_connected", machine.get("location_id"),
        machine.get("location"), machine_id,
        f"{name} has never answered since it joined {when}, at "
        f"{address or 'the address its controller kept'}.",
        "Check that Mac is switched on; if it is, check its controller can reach it",
    )


#: Catalogue errors that only say a machine could not be reached: the Hub's own word, and httpx's.
_REACH_ERRORS = frozenset({"worker_unreachable", "ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError",
                           "WriteTimeout", "PoolTimeout", "TimeoutException", "NetworkError",
                           "RemoteProtocolError"})


#: ...and the same faults as the Hub actually stores them: `str(exc)`, httpx's or the OS's message,
#: not the class name (live 2026-10-03: seven "All connection attempts failed" rows on 1200's offline node).
_REACH_PHRASES = ("all connection attempts failed", "connection refused", "connection reset",
                  "no route to host", "host is down", "network is unreachable", "timed out",
                  "server disconnected", "nodename nor servname")


def _is_reach_error(text: object) -> bool:
    text = str(text or "").strip()
    if text.removeprefix("httpx.") in _REACH_ERRORS:
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _REACH_PHRASES)


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

    # **Behind the newest release, and not getting there by itself.** "Latest" is the newest Studio
    # Hub this Gateway has published (`latest_hub_release`), not the release this build was compiled
    # against: that comparison called every controller "newer" the day after a release, which is
    # noise, and it never once said the one thing worth knowing — which Macs are not on the latest.
    # A Mac whose controller already holds the release and is inside the install window is
    # "updating", which is the normal state for a few minutes after a publish and not a warning.
    flat = machines_flat([row])
    behind = [m for m in flat if m.get("version_status") == "behind"]
    if behind:
        latest_version = str((row.get("hub_latest") or {}).get("version") or "the latest")
        named = ", ".join(
            f"{m.get('label') or m.get('machine')} ({m.get('hub_version') or '?'})"
            for m in sorted(behind, key=lambda m: str(m.get("label") or m.get("machine")))[:4]
        ) + (f" and {len(behind) - 4} more" if len(behind) > 4 else "")
        held = (row.get("release") or {}).get("version") if isinstance(row.get("release"), dict) else None
        if _version_key(held) is not None and _version_key(held) >= _version_key(latest_version):  # type: ignore[operator]
            why = ("Its controller has the update, but these Macs have not installed it: open Studio Hub "
                   "on that Mac, or run \u201cUpdate this Mac\u201d.")
        else:
            why = "Their controller does not hold it yet: publish the release to this group."
        count = f"{len(behind)} Mac" + ("" if len(behind) == 1 else "s")
        out.append(_issue(
            "warning", "hub_behind", location_id, location, count,
            f"{named}: behind Studio Hub {latest_version}. {why}",
            "Open Studio Hub on that Mac, or run Update this Mac",
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
    lite_off: set[str] = set()
    startup_off: list[str] = []
    disk_low: dict[str, list[str]] = {"stop": [], "red": []}
    for machine in flat:
        machine_id = str(machine.get("machine") or "")
        # A Mac that is not answering has no disk worth reporting (its last free-space reading is
        # stale or a zero), so only a reachable Mac in red/stop counts.
        disk = machine.get("disk") or {}
        if machine.get("reachable") and disk.get("state") in disk_low and disk.get("free_gb") is not None:
            disk_low[disk["state"]].append(f"{machine.get('label') or machine_id} {disk['free_gb']:g} GB free")
        fact = machine.get("startup")
        if fact and fact.get("start_services_on_launch") is False:
            startup_off.append(str(machine.get("label") or machine_id))
        state = str(machine.get("state") or "").strip().lower()
        health = str(machine.get("health") or "").strip()
        unhealthy = bool(health) and health.lower() != "up"
        # **A Mac the controller has never once reached is not the Mac that is off for the night.**
        # Both read `offline`, and for a Lite Mode Mac the "expected off" fold below would otherwise
        # hide it for ever: measured 2026-10-20, the Shops office iMac sat at "offline · never
        # connected" for 51 hours while the Hub's own app on its controller already said "never
        # connected since it joined". One fault, one row: this replaces `machine_offline` (and the
        # quiet `lite_mode_long_off` line) rather than joining them, and the machine still folds its
        # workers and models in as any offline Mac does.
        never_connected = machine.get("node_never_connected") is True
        if never_connected and (machine.get("lite_off") or state == "offline") \
                and machine_id not in rejected_nodes:
            offline_machines.add(machine_id)
            if machine.get("lite_off"):
                lite_off.add(machine_id)
            out.append(_never_connected_issue(machine, machine_id))
        elif machine.get("lite_off"):
            # A Lite Mode Mac switched off is expected: no warning. Its studios and models are folded in as for
            # any offline Mac; only a long absence earns one quiet line.
            offline_machines.add(machine_id)
            lite_off.add(machine_id)
            seen_at = _num(machine.get("node_last_seen_at"))
            if seen_at is not None and time.time() - seen_at > LITE_OFF_NOTE_S:
                name = machine.get("label") or machine_id
                out.append(_issue(
                    "info", "lite_mode_long_off", machine.get("location_id"), machine.get("location"), machine_id,
                    f"{name} is in Lite Mode and has been switched off for "
                    f"{int((time.time() - seen_at) // 86400)} days.",
                    "Nothing to do while the shop is closed; otherwise check that Mac is switched on",
                ))
        elif state == "offline" and machine_id not in rejected_nodes:
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
        # An offline Mac reports nothing, so "evidence pending" on it is the offline row again.
        if limitation and state != "offline" and machine_id not in rejected_nodes:
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
            # **Information, not a warning.** It says which activity *fields* the Hub could not
            # read (a progress figure, a median), not that a Mac is unwell: a studio that is
            # actually down is its own `worker_offline` row. As a warning it turned every group
            # with an idle Mac amber.
            out.append(_issue(
                "info", "machine_limited", location_id, location, machines_text,
                detail, "Fleet → Machines",
            ))
    for state, severity in (("stop", "error"), ("red", "warning")):
        names = disk_low[state]
        if names:
            shown = "; ".join(sorted(names)[:4]) + (" and more" if len(names) > 4 else "")
            consequence = ("The Hub refuses new work on it." if state == "stop"
                           else "Free space is low; new work may be refused.")
            out.append(_issue(
                severity, f"disk_{state}", location_id, location,
                f"{len(names)} machine" + ("" if len(names) == 1 else "s"),
                f"Disk {state.upper()}: {shown}. {consequence}",
                "Fleet → Machines (Disk column)",
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
    running = {str(m.get("machine") or ""): _running_studios(m.get("startup")) for m in flat}
    for worker in row.get("workers") or []:
        if not isinstance(worker, dict):
            continue
        worker_key = (worker.get("machine"), worker.get("id"))
        if worker_key in seen_workers:
            continue
        seen_workers.add(worker_key)
        worker_id = str(worker.get("id") or worker.get("machine") or "worker")
        machine_id = str(worker.get("machine") or worker_id)
        # A studio the Mac does not run at all (see `_not_installed`) is not a worker that is down, and a switched-off
        # Lite Mode Mac's studios are expected to be down.
        if _not_installed(worker, running.get(machine_id)) or machine_id in lite_off:
            continue
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
                    f"Worker {worker_id} is offline: the Mac answers, but its "
                    f"{_studio_word(worker)} studio is not running.",
                    "Open Studio Hub on that Mac and restart its services",
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
        # Counted for completeness, as information: each machine's own offline row is the warning.
        out.append(_issue(
            "info", "worker_offline", location_id, location, workers_text,
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
        # `ConnectTimeout` / `ConnectError` / `ReadTimeout` are the same "could not reach it" in
        # httpx's words (live: seven Whisper rows on two switched-off 1400 nodes).
        if (str(model.get("machine") or "") in offline_machines
                and _is_reach_error(model.get("catalog_error"))):
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
    out: list[dict] = []
    seen: set[tuple] = set()
    for issue in (issue for row in rows for issue in _issues_for(row)):
        # The same sentence about the same subject at the same location is one warning.
        key = (issue.get("code"), issue.get("location_id"), issue.get("subject"), issue.get("message"))
        if key not in seen:
            seen.add(key)
            out.append(issue)
    out.sort(key=_issue_order)
    return out


def _issue_order(issue: dict) -> tuple:
    return (_ISSUE_SEVERITY.get(issue["severity"], 3), str(issue.get("location") or ""),
            str(issue.get("subject") or ""), str(issue.get("code") or ""))


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
        last = _cache["last"]
    if not force and cached is not None:
        if allow_stale or age < _CACHE_TTL:
            return cached
        _revalidate_in_background()
        return cached
    # **Just after a roster change, a plain read still does not wait.** `invalidate` dropped the
    # cache, and a cold read here used to wait for a whole fan-out -- as long as the slowest
    # controller, ~19 s on the owner's fleet against the app's 15 s timeout -- so the page that
    # had just added a location timed out and showed nothing. It gets the previous snapshot made
    # to agree with the roster (the new location pending) while the fan-out runs behind it.
    if not force and not allow_stale and last is not None:
        _revalidate_in_background()
        return with_roster(last)

    # **`allow_stale` never waits for the fan-out, and that is the whole of its contract.**
    # It says "must not block" in as many words, and it did: with nothing cached it fell through
    # to the serialised branch below and waited on `_refresh_lock` for as long as the slowest
    # controller — 435 s in the 2026-10-19 incident — and `Worker._provider_budget`,
    # `_model_supply`, `_background_room`, `concurrency.live_capacity` and therefore `/health`
    # all read the fleet this way. So a fan-out that had gone slow parked admission and the
    # health probe behind it. A caller that must not block gets what is there — the last
    # snapshot made to agree with the roster, or an empty one on a cold cache, which every
    # reader already handles (a site it cannot see is a site with no published capacity) — and
    # the fan-out it needs is started behind it.
    if not force and allow_stale:
        _revalidate_in_background()
        return with_roster(last) if last is not None else _empty_snapshot()

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


def _empty_snapshot() -> dict:
    """`snapshot`'s shape with nothing in it, for a reader that may not wait (see `snapshot`).

    Built from the same three functions the fan-out uses, so a key added there cannot go missing
    here — a reader of an empty fleet must read "no published capacity", never a `KeyError`.
    """
    rows: list[dict] = []
    return {
        "locations": rows,
        "machines": machines_flat(rows),
        # Carried rows are a Fleet-page view of a slow site (see `_carry_last_known_machines`),
        # never capacity; an empty fleet has none.
        "last_known_machines": [],
        "running": running_flat(rows),
        "summary": _summarise(rows),
        "issues": [],
        "generated_at": 0.0,
    }


def _refresh() -> dict:
    """One fan-out over every location, cached on the way out."""
    with _cache_lock:
        gen = _cache["gen"]
        previous = _cache["value"] or _cache["last"]
    rows = locations()
    _keep_last_known(rows, previous)
    # The newest release this Gateway published rides on every row, so each Mac can be rated
    # against it (`machines_flat` → `version_status`).
    stamp_latest(rows)
    machines = machines_flat(rows)
    # **The reachability history is recorded here, and only here** — one fan-out, one observation, so
    # there is no second poller to drift or to hammer the fleet with (see `fleet_history`). It is fed
    # the flat machine rows too, so the reason it stores is the one `_node_reach_reason` worked out
    # rather than a second reading of the same sentence. It writes to disk only on a real change.
    fleet_history.observe(rows, machines)
    stamp_controller_versions(rows, machines)
    from . import startup_control  # late: it imports this module

    lite_issues = startup_control.stamp_lite_mode_pending(machines)
    remember_lite_mode(machines)
    summary = _summarise(rows)
    summary["issues"] += len(lite_issues)
    value = {
        "locations": rows,
        "machines": machines,
        # **A slow site's Macs, kept apart from `machines` on purpose.** Routing, admission,
        # capacity and the reachability history read `machines`, and a row that was not measured
        # in this fan-out must never look like capacity. Only `admin.fleet_snapshot` adds these
        # to a Fleet-page copy (see `_carry_last_known_machines`).
        "last_known_machines": _carry_last_known_machines(rows, previous),
        "running": running_flat(rows),
        "summary": summary,
        "issues": sorted(_issues(rows) + lite_issues, key=_issue_order),
        "generated_at": time.time(),
    }
    # **The one automatic action over a fan-out, and it is off unless the owner turned it on.**
    # Piggybacks on this refresh rather than polling anything new; `orphans` filters to this
    # Gateway's own labelled batches, refuses `orphan_unknown`, and applies the same guard the
    # Stop button shows. Imported late: it reads the jobs store, which reads this module.
    try:
        from . import orphans

        orphans.maybe_auto_stop(value)
    except Exception:  # noqa: BLE001 - a fan-out is never failed by a convenience switch
        log.warning("orphan auto-stop pass failed", exc_info=True)
    # **The one place the fleet's live capacity is computed for `/health`.** The probe reads the
    # summary this publishes rather than walking every location on every poll (see
    # `concurrency._capacity_cache`); a fan-out that has just run is exactly when the number
    # changed, and the poller runs on its own timer. Rows are passed in, never re-read: this is
    # already inside the refresh lock.
    try:
        from . import concurrency  # late: it imports this module

        concurrency.remember_capacity(concurrency.live_capacity(rows=rows))
    except Exception:  # noqa: BLE001 - a figure is never a reason to fail a fan-out
        pass
    with _cache_lock:
        # **Only if the roster has not changed since this fan-out read it.** The poller's sweep takes
        # as long as the slowest controller; one that started before an add finished after it and
        # cached the old roster as fresh, so the page read without the new location -- "it takes a
        # while for it to show" -- until the next sweep. Its answer still goes to its own caller.
        if _cache["gen"] == gen:
            _cache["at"] = time.time()
            _cache["value"] = value
            _cache["last"] = None
    return value


#: What a slow location keeps from its last answer: who it is and what it runs, never its live state.
_LAST_KNOWN_KEYS = ("role", "version", "hub_version_relation", "release", "site_id", "controller_id")

#: How old a carried machine row may be and still be shown on the Fleet page. One hour: the owner
#: walks between sites, and a Mac that answered an hour ago is still the likely truth; beyond that
#: the location row's own "last report HH:MM" is the honest answer and the rows are not invented.
LAST_KNOWN_MAX_AGE_S = 3600.0


def _carry_last_known_machines(rows: list[dict], previous: dict | None) -> list[dict]:
    """A slow site's machine rows from its last answer, **kept out of `value["machines"]`**.

    Only the Fleet page shows these, on the copies `admin.fleet_snapshot` makes. Routing,
    admission, capacity, orphan detection and the reachability history read `machines`, and a row
    that was not measured in this fan-out is not capacity — so it is carried separately and every
    row is marked (`last_known`, `as_of`, `state_text`) and its old live state kept under
    `last_known_state`. `reachable` is `None`: unknown now, never online and never offline.

    **Only `slow`** (it timed out but has answered before). A refused, rejected or dropped
    connection is not carried: the location row already says why, and `_last_known_members` names
    the members it last knew. Nothing is carried for a site never seen up, nor for rows older than
    `LAST_KNOWN_MAX_AGE_S` — the row keeps saying when it last reported instead.
    """
    if not previous:
        return []
    # Both lists, so a site that has been slow for several fan-outs keeps being carried: the
    # previous read's carried rows are still the last thing it said.
    before: dict[str, list[dict]] = {}
    for machine in [*(previous.get("machines") or []), *(previous.get("last_known_machines") or [])]:
        if not isinstance(machine, dict):
            continue
        location_id = str(machine.get("location_id") or "")
        if location_id:
            before.setdefault(location_id, []).append(machine)
    now = time.time()
    carried: list[dict] = []
    for row in rows:
        if row.get("reachable") or not row.get("slow"):
            continue
        as_of = row.get("answered_at")
        if not isinstance(as_of, (int, float)) or isinstance(as_of, bool) or not as_of:
            continue
        age = now - float(as_of)
        if age < 0 or age > LAST_KNOWN_MAX_AGE_S:
            continue
        for machine in before.get(str(row.get("id")), []):
            carried.append({
                **machine,
                "last_known": True,
                "as_of": float(as_of),
                # The old state stays available, but never as the live word: the row says when it
                # was last reported, and `state` has a value no live badge can be mistaken for.
                "last_known_state": machine.get("state"),
                "state": "last_known",
                "state_text": time.strftime("Last report %H:%M", time.localtime(as_of)),
                "reachable": None,
                "location_reachable": row.get("reachable"),
                # A carried row's attention verdict is stale by definition: mark it so the page
                # draws "not checked in since HH:MM" instead of a fault sentence.
                "attention_source": "last_known",
                "attention_measured_at": float(as_of),
            })
    return carried


def _keep_last_known(rows: list[dict], previous: dict | None) -> None:
    """A location that was too slow to answer keeps what it last reported, with a note saying so.

    A timeout is not an absence: blanking the row's role and version made a busy controller read as
    an empty one. Live state (reachable, machines, jobs) is not carried — those must be measured.
    """
    now = time.time()
    before = {str(row.get("id")): row for row in (previous or {}).get("locations") or []}
    for row in rows:
        if row.get("reachable"):
            row["answered_at"] = now
            continue
        old = before.get(str(row.get("id")))
        if not old:
            continue
        if old.get("answered_at"):
            row["answered_at"] = old["answered_at"]
        if not row.get("slow"):
            continue
        for key in _LAST_KNOWN_KEYS:
            if row.get(key) is None and old.get(key) is not None:
                row[key] = old[key]
        if row.get("answered_at"):
            row["error"] = (
                f"Slow to answer: didn't answer within {int(_TIMEOUT)} s — "
                f"last report {time.strftime('%H:%M', time.localtime(row['answered_at']))}."
            )


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
# and all), so the weight is per model rather than per site. Speed is not a routing
# input: measured per model × Mac class (Analytics), speed-aware site choice gained
# nothing in simulation (−1%…+8%), so measured speed feeds ETAs and planning only.


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


#: What the Fleet page says of a location the owner paused by hand (`paused_by: "owner"`).
OWNER_PAUSE_REASON = (
    "Paused by you. It gets no new work; jobs already running there finish. Resume it when you want it back."
)


def other_pause_reason(actor: str) -> str:
    """The same sentence for a pause made by an agent or any other caller — **never "you"**.

    The reason is stored once and read back on the owner's own Fleet page, so a pause an agent made
    must not address him as its author. Two pauses read "Paused by you" live on 1301 on 2026-11-17
    when no person had touched the page; that is the class this wording must not repeat.
    """
    return (f"Paused by {actor}. It gets no new work; jobs already running there finish. "
            f"Resume it from the Fleet page.")


def owner_paused_at(controller_id: str) -> float | None:
    """When the owner paused this location by hand, or `None`.

    Read from `fleet.json` on every call, never from the cached snapshot: a pause has to bite on the
    very next routing decision. It is its own field, apart from the automatic outage pause
    (`paused_at`), which `note_reachability` clears the moment a Mac answers -- an owner's pause must
    never be undone by a Mac coming back.
    """
    if controller_id == LOCAL_ID:
        value = _read_payload().get("local_paused_at")
    else:
        value = next((r.get("owner_paused_at") for r in _read() if str(r.get("id")) == controller_id), None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value else None


def owner_paused_by(controller_id: str) -> str | None:
    """Who made the hand-pause: `"owner"` from the app, or the caller the route named.

    `None` when the location is not hand-paused, and also for a pause written before this field
    existed — the caller treats `None` as `"owner"` so an upgrade does not rewrite history.
    """
    if controller_id == LOCAL_ID:
        value = _read_payload().get("local_paused_by")
    else:
        value = next((r.get("owner_paused_by") for r in _read() if str(r.get("id")) == controller_id), None)
    return str(value) if isinstance(value, str) and value.strip() else None


def _stamp_pause(result: dict, controller_id: str) -> None:
    """Say on a published location row who paused it: the page's owner, a named caller, or `outage`."""
    at = owner_paused_at(controller_id)
    if at:
        actor = owner_paused_by(controller_id) or "owner"
        result.update(paused_at=at, paused_by=actor,
                      pause_reason=OWNER_PAUSE_REASON if actor == "owner" else other_pause_reason(actor))
    elif result.get("paused_at"):
        result["paused_by"] = "outage"


def set_owner_pause(controller_id: str, paused: bool, *, actor: str = "owner") -> dict | None:
    """Pause or resume a location by hand (`local` is this Mac); `None` when there is no such location.

    Persisted in the roster, so it survives a restart. Jobs already dispatched there are not touched.

    `actor` is **who** did it: `"owner"` only for a pause made in the app, otherwise the authenticated
    caller the route named (`an API key`, `a local API client (no credential)`, or a declared name).
    It is stored beside the stamp and read back by `_stamp_pause`; without it every hand-pause
    published as the owner's, which is exactly what the live Fleet page showed on 2026-11-17.
    """
    stamp = time.time() if paused else None
    actor = (actor or "owner") if paused else None
    with _ROSTER_LOCK:
        if controller_id == LOCAL_ID:
            _write(_read(), local_paused_at=stamp, local_paused_by=actor)
            row: dict = {"id": LOCAL_ID, "label": local_label(), "local": True}
        else:
            rows = _read()
            found = next((r for r in rows if str(r.get("id")) == controller_id), None)
            if found is None:
                return None
            if paused:
                found["owner_paused_at"] = stamp
                found["owner_paused_by"] = actor
            else:
                found.pop("owner_paused_at", None)
                found.pop("owner_paused_by", None)
            _write(rows)
            row = _masked(found)
    invalidate()
    row.update(paused_at=stamp, paused_by=actor)
    return row


def pause_message_for_voice(params: dict) -> str | None:
    """A plain sentence when a voice this job needs exists only on a Mac the owner paused, else `None`.

    A clone the gateway holds travels with the job (`_clip_travels_with_job`) and never lands here. A
    voice whose clip lives only on one Hub cannot run anywhere else, so work for it fails with the
    reason instead of silently going somewhere that lacks the voice.
    """
    if not isinstance(params, dict):
        return None
    for key in _VOICE_PIN_KEYS:
        value = str(params.get(key) or "")
        location_id = location_for_voice(value)
        if not location_id or (key == "voice_id" and _clip_travels_with_job(value)):
            continue
        if owner_paused_at(location_id):
            name = label_for_provider(provider_id_for(location_id)) or location_id
            return (f"This voice only exists on {name}, which you paused. "
                    f"Share it to the fleet or resume {name}.")
    return None


def is_paused_provider(provider_id: str) -> bool:
    """True for a location provider the owner paused by hand."""
    location_id = controller_id_from(provider_id)
    return location_id is not None and owner_paused_at(location_id) is not None


def resume_location(controller_id: str) -> dict | None:
    """Clear a pause (the owner's and the outage one) and restart the clock, without touching anything else."""
    if controller_id == LOCAL_ID:
        return set_owner_pause(LOCAL_ID, False)
    with _ROSTER_LOCK:
        rows = _read()
        row = next((r for r in rows if str(r.get("id")) == str(controller_id)), None)
        if row is None:
            return None
        row.pop("paused_at", None)
        row.pop("pause_reason", None)
        row.pop("unreachable_since", None)
        row.pop("owner_paused_at", None)
        row.pop("owner_paused_by", None)
        _write(rows)
    invalidate()
    return _masked(row)


def _last_machine_seen(row: dict) -> float | None:
    """The newest heartbeat among a location's last-known Macs, if any stated one."""
    seen = [_num(m.get("node_last_seen_at")) for m in row.get("machines") or []
            if isinstance(m, dict)]
    return max((value for value in seen if value), default=None)


def _no_work_reason(row: dict) -> tuple[str, str]:
    """The vocabulary reason a row that `_takes_customer_jobs` refused should carry."""
    if row.get("paused_at") or owner_paused_at(str(row.get("id") or "")):
        return "paused", "the location is paused"
    if (str(row.get("role") or "").strip().lower() == "agent"
            or str(row.get("handed_over_to") or "").strip()):
        return "location_mismatch", "an agent-mode or handed-over location does not take customer jobs"
    if _location_disk_stopped(row):
        return "draining", "every Mac here is out of disk"
    return "draining", "the location has stopped taking customer jobs"


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
    if row.get("paused_at") or owner_paused_at(str(row.get("id") or "")):
        return False
    # **A location whose every Mac is out of disk (`stop`) is unavailable** -- it would only
    # answer 507 -- so routing re-routes to another location, as it does for a drained one.
    if _location_disk_stopped(row):
        return False
    return row.get("drained") is not True


def _able_workers(row: dict, model_id: str) -> list[dict]:
    """The workers here that could run `model_id`: those listing it, less the Hub's memory refusals.

    Shared by `no_free_mac` and `_all_able_offline` so the two can never disagree about which Macs
    a question is even about. No such worker at all is "unknown" (an older Hub), never "none".
    """
    unfit = {str(m.get("machine")) for m in row.get("local_models") or []
             if isinstance(m, dict) and model_id in (m.get("model_id"), m.get("model"))
             and m.get("memory_eligible") is False}
    return [w for w in row.get("workers") or []
            if isinstance(w, dict) and model_id in (w.get("models") or [])
            and str(w.get("machine")) not in unfit]


def no_free_mac(row: dict, model_id: str) -> bool:
    """True when a location names the Macs that can run `model_id` and **every one is paused**.

    Measured 2026-10-04: group 1500 had all five Macs paused one by one at its controller, yet it
    stayed a candidate (its location was not paused), took 21 free TTS jobs and queued them for
    ~7,000 s while 8 GB Macs elsewhere idled. A Mac's pause reaches the fleet read twice: the node's
    `paused` (joined Macs) and `drained` on each of its workers (the controller's own Mac too, which
    has no node row). The Macs that could run the model are the workers that list it, less those the
    Hub says cannot fit it (`memory_eligible: False`). No such worker at all is "unknown" (an older
    Hub), never "all paused", so it changes nothing.

    Paused is the whole of this question; a Mac that has gone **offline** is `_all_able_offline`,
    asked beside it by `location_has_no_free_mac`, so that catalog's "you paused" never labels one.
    """
    paused = {str(n.get("id")) for n in row.get("nodes") or []
              if isinstance(n, dict) and n.get("paused") is True}
    able = _able_workers(row, model_id)
    return bool(able) and all(w.get("drained") is True or str(w.get("machine")) in paused
                              for w in able)


#: How old a Mac's last heartbeat may be before routing treats it as offline.
#:
#: **A missed heartbeat is an offline Mac** (owner report, 2026-11-14: 1204's only qwen-image Mac
#: had been gone seven days and routing still offered the location). The Hub's `online: false` is
#: one statement of the same fact; this is the other, for a Hub that keeps a stale worker row.
#: The window is generous on purpose: a busy Mac answers its overview less often than an idle one.
ONLINE_WINDOW_S = 900.0


def _machine_offline(row: dict, worker: dict, now: float) -> bool:
    """Whether one able Mac is offline by the Hub's word **or** by a heartbeat past the window.

    Three answers again: a stated `online: false`, a machine row the Hub marks unreachable or whose
    node has not been seen inside `ONLINE_WINDOW_S`, or a worker whose own `last_seen_at` is that
    old. Nothing stated stays "unknown", never "offline".
    """
    if worker.get("online") is False:
        return True
    seen = _num(worker.get("last_seen_at"))
    if seen is not None and now - seen > ONLINE_WINDOW_S:
        return True
    machines = {str(m.get("id")): m for m in row.get("machines") or [] if isinstance(m, dict)}
    machine = machines.get(str(worker.get("machine")))
    if isinstance(machine, dict):
        if machine.get("reachable") is False:
            return True
        node_seen = _num(machine.get("node_last_seen_at"))
        if node_seen is not None and now - node_seen > ONLINE_WINDOW_S:
            return True
    return False


def _all_able_offline(row: dict, model_id: str, now: float | None = None) -> bool:
    """Every Mac here that could run `model_id` has gone offline, so none of them can take work.

    **A controller whose Macs have all dropped keeps answering.** On 2026-10-31 its studios stopped
    at 21:35 while the Fleet still read the location itself as reachable and eleven machines at it
    as offline; seven free jobs accepted there waited 55 minutes until the studios were restarted by
    hand, because `no_free_mac` counts only `paused`/`drained` and an offline Mac is neither, so the
    stranded-queue door in `_poll_batch` never opened. The Hub still lists such workers with
    `online: false` (measured live 2026-11-02: 56 of them at Shops, every one on a machine the
    activity read calls `offline`).

    **A stale heartbeat counts the same way** (`_machine_offline`): 1204's only qwen-image worker
    was last seen seven days ago while its row still listed the model, and routing offered it.

    Three answers, never two: `False` while any able worker is online — a site with one working Mac
    is not dead; `False` when a worker states no `online` at all (an older Hub), because "not read"
    is never "dead"; and `False` when the location names no able Mac, which stays unknown.
    """
    able = _able_workers(row, model_id)
    now = time.time() if now is None else now
    if not able:
        # **An older Hub publishes no worker list, but its machine rows still carry heartbeats.**
        # 1204's only qwen-image Mac was last seen seven days ago with no worker row naming the
        # model; every machine at the location stale is the same fact as every able Mac offline.
        machines = [m for m in row.get("machines") or [] if isinstance(m, dict)]
        if not machines:
            return False
        return all(machine.get("reachable") is False
                   or (_num(machine.get("node_last_seen_at")) is not None
                       and now - _num(machine.get("node_last_seen_at")) > ONLINE_WINDOW_S)
                   for machine in machines)
    return all(_machine_offline(row, worker, now) for worker in able)


def location_has_no_free_mac(provider_id: str, model_id: str) -> bool:
    """`no_free_mac` for a provider id, or the same site with every able Mac offline.

    From the warm snapshot. Unknown (no row, no read, no Mac named) is False. This is the predicate
    `_stranded` polls while a free job sits queued at a location, so it is where "that site cannot
    take this" has to cover both ways a site stops being able to.
    """
    location_id = controller_id_from(provider_id)
    try:
        rows = snapshot(allow_stale=True).get("locations") or []
    except Exception:  # noqa: BLE001 - a routing hint must never break a job
        return False
    return any(str(row.get("id")) == location_id
               and (no_free_mac(row, model_id) or _all_able_offline(row, model_id))
               for row in rows)


def publishes_model(row: dict, model_id: str) -> bool | None:
    """Whether this location states that it has the model at all — **not** whether it is free now.

    The question a job bound to a location must answer before it is sent there again. Measured on
    the live Gateway, 2026-10-29: seven normal Breeze-TTS jobs stayed bound to a site that does not
    publish the model in its `model_supply`, and replayed "hub submit failed: timed out" for 6–29
    attempts, because nothing asked this. The location-level "does the catalogue offer it" question
    is a different one (`execute_job` answers that with `MODEL_NOT_OFFERABLE`); this is the Hub's
    own per-model reduction, the same evidence `_model_supply` budgets from.

    Three answers, never two:

      * `True` — the Hub names eligible Macs for it, or a Mac at the site lists it;
      * `False` — the Hub publishes a per-model supply that leaves it out with no Mac listing it,
        or gives it no eligible Mac: the site cannot run it and a submit would be refused;
      * `None` — the location publishes no per-model evidence at all (an older Hub). Unstated is
        unknown, and unknown never becomes "cannot" (§6 of the house rules).

    A Mac that lists the model rescues a model missing from the reduction, because `model_supply`
    is a reduction of the Hub's own rows and a companion model the Hub does not reduce as a row is
    still runnable there.
    """
    supply = row.get("model_supply")
    if not isinstance(supply, dict) or not supply:
        return None
    entry = supply.get(str(model_id))
    if isinstance(entry, dict):
        return _as_int(entry.get("eligible")) > 0
    named = [m for m in row.get("local_models") or []
             if isinstance(m, dict) and model_id in (m.get("model_id"), m.get("model"))]
    if any(m.get("memory_eligible") is not False for m in named):
        return True
    if any(model_id in (w.get("models") or []) for w in row.get("workers") or []
           if isinstance(w, dict)):
        return True
    return False


def site_wait_reason(provider_id: str, model_id: str) -> str:
    """Why a job bound to this location is not running, in the fleet's own words.

    Written onto the job as its deferral reason (`worker._note_waiting`), so a queued
    job publishes a `waiting` sentence instead of reading `accepted` with nothing to
    say — which is how a research call sat for twenty minutes with no reason on it
    (measured 2026-10-30). Every branch is a fact this gateway already holds: no row,
    a site that is not answering (with its own error), a site that cannot run this
    model, or one that is merely full.
    """
    label = label_for_provider(provider_id) or "its site"
    try:
        row = next((r for r in snapshot(allow_stale=True).get("locations") or []
                    if str(r.get("provider_id") or "") == provider_id), None)
    except Exception:  # noqa: BLE001 - a sentence shown to a person must never fail a pass
        row = None
    if not isinstance(row, dict):
        return f"waiting for a Mac that can run {model_id}"
    if row.get("reachable") is False:
        error = str(row.get("error") or "").strip() or "it is not answering"
        return f"waiting for {label} to answer — {error}"
    if publishes_model(row, model_id) is False:
        return f"waiting for a Mac at {label} that can run {model_id}"
    return f"waiting for a free Mac at {label}"


def site_cannot_take(provider_id: str, model_id: str) -> bool:
    """True when this location could not take this model even if one of its Macs were free.

    Not answering, or stating it has no Mac for the model. Those are not a queue to stand in: only
    a fresh answer from the site or a different route changes them, so a job bound here must be
    moved or ended quickly instead of waiting out the placement ceiling (2026-11-17: two image jobs
    spent 45 minutes at terranash-1500, which answered nothing). **Unknown is not a refusal** — no
    row, or supply it does not state, leaves this `False` and the job keeps its normal line.
    """
    try:
        row = next((r for r in snapshot(allow_stale=True).get("locations") or []
                    if str(r.get("provider_id") or "") == provider_id), None)
    except Exception:  # noqa: BLE001 - a routing hint must never break a job
        return False
    if not isinstance(row, dict):
        return False
    if row.get("reachable") is False:
        return True
    return publishes_model(row, model_id) is False


def location_publishes_model(provider_id: str, model_id: str) -> bool | None:
    """`publishes_model` for a provider id, from the warm snapshot. No row at all is unknown."""
    location_id = controller_id_from(provider_id)
    try:
        rows = snapshot(allow_stale=True).get("locations") or []
    except Exception:  # noqa: BLE001 - a routing hint must never break a job
        return None
    for row in rows:
        if str(row.get("id")) == location_id or str(row.get("provider_id") or "") == provider_id:
            return publishes_model(row, model_id)
    return None


#: How long a location that has answered may stay silent before its models are withdrawn.
#:
#: A controller that misses one 6 s overview read while it is busy answering a render is not
#: offline — measured live on terranash-1600, 2026-10-30: its models flipped `locked: true` and
#: back on consecutive catalogue reads seconds apart, because `usable_location_ids` read a single
#: probe. The grace is the last *measured* answer, with its age (`answered_at`, which
#: `_keep_last_known` keeps across a timeout), not an invented one; a site silent past it is
#: locked exactly as before.
#:
#: Only the catalogue's offer/lock decision reads this. Routing and admission still read the live
#: `reachable`, so nothing is dispatched to a site that did not answer — a submit against a model
#: held open by the grace is refused with `why_no_route`'s sentence. That is the honest trade: a
#: picker that flickers teaches the owner nothing; a clear refusal at submit does.
#:
#: ponytail: one flat grace for every location. Ceiling — a site that died seconds ago reads
#: offered for up to the grace. Upgrade path: read it from the Fleet settings file when the owner
#: wants to tune it per site.
LOCK_ANSWER_GRACE_S = 45.0


def usable_location_ids() -> set[str]:
    """Locations that can actually take work right now, by id.

    Read once per catalogue rebuild rather than per row: this is the difference
    between one fleet read and one per model. An agent or a drained Hub is left
    out, so a model only they offer reads as unavailable rather than being
    published to Story Studio and then refused on use.

    **A slow read is not a dead site.** A location that answered within
    `LOCK_ANSWER_GRACE_S` and missed this one probe keeps offering its models;
    beyond the grace it is out, as before. Without this a busy controller's whole
    catalogue flipped locked and back between consecutive reads (see the constant).
    """
    try:
        now = time.time()
        answering: set[str] = set()
        for row in snapshot(allow_stale=True)["locations"]:
            if not _takes_customer_jobs(row):
                continue
            if row.get("reachable"):
                answering.add(str(row.get("id")))
                continue
            answered = row.get("answered_at")
            if (row.get("slow") and isinstance(answered, (int, float))
                    and not isinstance(answered, bool)
                    and 0 <= now - answered <= LOCK_ANSWER_GRACE_S):
                answering.add(str(row.get("id")))
        return answering
    except Exception:  # noqa: BLE001 - a routing hint must never break the catalogue
        return set()


_RENDER_DIGEST_FIELDS = (
    "profile_digest", "tools_digest", "compiler_digest", "qualification_digest",
)
_RENDER_BOUND_FIELDS = (
    "max_rss_bytes", "max_wall_seconds", "min_available_memory_bytes", "output_limit_bytes",
)

#: The Hub's compiler-derived encode fact (`components/hub/backend/render_encode.py`).
#: The Gateway never compiles or invents a combination: it validates the closed document
#: at the fleet boundary and republishes only what every eligible machine advertises.
RENDER_ENCODE_FORMAT = "studio.render.encode-authority.v1"
_RENDER_ENCODE_KEYS = frozenset(("format", "entries"))
_RENDER_ENCODE_ENTRY_KEYS = frozenset((
    "container", "requested_codec", "requested_effort", "crf",
    "quality_cpu_preset", "bitrate_kbps",
))
_RENDER_ENCODE_ENTRY_ORDER = (
    "container", "requested_codec", "requested_effort", "crf",
    "quality_cpu_preset", "bitrate_kbps",
)
_RENDER_ENCODE_VALUES = {
    "container": frozenset(("mp4", "mov", "webm")),
    "requested_codec": frozenset(("h264", "h265")),
    "requested_effort": frozenset(("ultrafast", "medium", "slow", "hardware")),
    "quality_cpu_preset": frozenset(("medium", "slow", "ultrafast")),
}
#: The Hub's uncapped grid is 3 x 2 x 4 x 5 = 120 entries; a larger document is not its answer.
_RENDER_ENCODE_MAX_ENTRIES = 120


def render_encode_document(value: object) -> bool:
    """The closed encode fact is absent-able, `None`, or an exact compiler-derived document."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != _RENDER_ENCODE_KEYS:
        return False
    if value.get("format") != RENDER_ENCODE_FORMAT:
        return False
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > _RENDER_ENCODE_MAX_ENTRIES:
        return False
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _RENDER_ENCODE_ENTRY_KEYS:
            return False
        if any(not isinstance(entry.get(name), str)
               or entry[name] not in allowed
               for name, allowed in _RENDER_ENCODE_VALUES.items()):
            return False
        # Contract bounds, not today's grid: a non-negative safe integer CRF and a
        # positive safe integer cap are what the Hub may publish; the exact five quality
        # pairs are the compiler's business, not this validator's.
        crf = entry.get("crf")
        if type(crf) is not int or not 0 <= crf <= 2 ** 53 - 1:
            return False
        bitrate = entry.get("bitrate_kbps")
        if bitrate is not None and (
                type(bitrate) is not int or not 1 <= bitrate <= 2 ** 53 - 1):
            return False
    return True


def render_encode_intersection(candidates: object) -> dict | None:
    """The encode combinations every eligible candidate advertises, or `None` when unknown.

    Each machine's document is derived by the Hub from its own pinned compiler. The
    Gateway only intersects the entry sets; a candidate without a document (an older
    Hub), an explicit `None`, or a malformed document makes the answer unknown rather
    than guessed. An empty intersection is still an answer: nothing can be rendered
    remotely with an encode every machine agrees on, so a caller falls back locally.
    """
    if not isinstance(candidates, list) or not candidates:
        return None
    shared: dict[tuple, dict] | None = None
    for candidate in candidates:
        capability = candidate.get("capability") if isinstance(candidate, dict) else None
        document = capability.get("encode") if isinstance(capability, dict) else None
        if not render_encode_document(document) or document is None:
            return None
        current: dict[tuple, dict] = {}
        for entry in document["entries"]:
            current.setdefault(tuple(entry[name] for name in _RENDER_ENCODE_ENTRY_ORDER), entry)
        if shared is None:
            shared = current
        else:
            shared = {key: entry for key, entry in shared.items() if key in current}
    return {"format": RENDER_ENCODE_FORMAT, "entries": list((shared or {}).values())}


def render_encode_supports(document: object, requested: object) -> bool:
    """Only an explicitly advertised exact encode can select this destination."""
    return (document is not None and render_encode_document(document)
            and render_encode_document({"format": RENDER_ENCODE_FORMAT, "entries": [requested]})
            and requested in document["entries"])


#: The Hub's closed envelope for one node's scoped late-label capability row: exactly the
#: five keys both leads ACKed, with `reason` nonempty if and only if `worker` is null.
_LATE_LABEL_CAPABILITIES_FORMAT = "studio.render.hub-late-label-capabilities.v1"
#: The jointly frozen subtitle envelope: same five keys, its own format literal.
_SUBTITLE_CAPABILITIES_FORMAT = "studio.render.hub-subtitle-capabilities.v1"
_SCOPED_CAPABILITIES_FORMATS = {
    LATE_LABEL_CONTRACT: _LATE_LABEL_CAPABILITIES_FORMAT,
    SUBTITLE_CONTRACT: _SUBTITLE_CAPABILITIES_FORMAT,
}
#: A scoped read is a moment, not a fact — the same staleness bound as the fleet snapshot.
_SCOPED_CAPABILITIES_MAX_AGE_S = _MEMBERS_STALE_AFTER_S
_SCOPED_CAPABILITIES_TIMEOUT = httpx.Timeout(15.0, connect=6.0)
_SCOPED_CAPABILITIES_MAX_JSON = 256 * 1024


class ScopedRenderCapabilitiesError(Exception):
    """A scoped capability read could not be trusted; never a measured zero."""


def render_subtitle_support(value: object, *, late_labels: bool = False) -> bool:
    """Both features must belong to this exact compiler/worker, never a fleet union."""
    if (type(value) is not dict or value.get("font_support") is not True
            or value.get("subtitles") != {"format": SUBTITLE_CONTRACT}):
        return False
    late = value.get("late_labels")
    expected = {"format": LATE_LABEL_CONTRACT, "motions": list(LATE_LABEL_MOTIONS)}
    return ("late_labels" not in value or late == expected) and (not late_labels or late == expected)


def _decode_scoped_capabilities(payload: object, *, requested_contract: str,
                                now: float) -> dict | None:
    """Validate the closed envelope. `None` is the Hub's measured absence for this node."""
    expected_format = _SCOPED_CAPABILITIES_FORMATS.get(requested_contract)
    if expected_format is None:
        raise ScopedRenderCapabilitiesError("scoped capability authority is not frozen")
    if (not isinstance(payload, dict)
            or set(payload) != {"format", "requested_contract", "server_time", "worker", "reason"}
            or payload["format"] != expected_format
            or payload["requested_contract"] != requested_contract):
        raise ScopedRenderCapabilitiesError("invalid scoped capability envelope")
    server_time = payload["server_time"]
    if type(server_time) is not int or not 0 < server_time <= 2**53 - 1:
        raise ScopedRenderCapabilitiesError("invalid scoped capability clock")
    if abs(now - server_time) > _SCOPED_CAPABILITIES_MAX_AGE_S:
        raise ScopedRenderCapabilitiesError("stale scoped capability clock")
    worker, reason = payload["worker"], payload["reason"]
    if worker is None:
        if not isinstance(reason, str) or not reason or len(reason) > 1024:
            raise ScopedRenderCapabilitiesError("scoped capability absence has no reason")
        return None
    if reason is not None or not isinstance(worker, dict):
        raise ScopedRenderCapabilitiesError("invalid scoped capability worker")
    if requested_contract == SUBTITLE_CONTRACT and not render_subtitle_support(worker):
        raise ScopedRenderCapabilitiesError("scoped subtitle worker lacks exact support")
    return worker


def _scoped_capability_payload(controller_id: str, machine_id: str,
                               requested_contract: str) -> object:
    """One node's envelope over the registered controller route; no new credential or address."""
    if controller_id == LOCAL_ID:
        base, token = hub_base_url() or "", hub_token() or ""
    else:
        row = next((item for item in roster() if str(item.get("id") or "") == controller_id), None)
        base = str((row or {}).get("url") or "")
        token = _token_for(controller_id)
    if not base or not token:
        raise ScopedRenderCapabilitiesError("no registered route for the scoped capability read")
    path = ("/api/hub/final-renders/capabilities"
            if machine_id == LOCAL_ID else
            f"/api/hub/fleet/nodes/{quote(machine_id, safe='')}/final-renders/capabilities")
    url = base.rstrip("/") + path + "?" + urlencode({"requested_contract": requested_contract})
    body = bytearray()
    try:
        with httpx.stream("GET", url, headers={"X-Hub-Token": token},
                          timeout=_SCOPED_CAPABILITIES_TIMEOUT, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ScopedRenderCapabilitiesError("scoped capability read was refused")
            for piece in response.iter_bytes():
                if len(body) + len(piece) > _SCOPED_CAPABILITIES_MAX_JSON:
                    raise ScopedRenderCapabilitiesError("scoped capability response is too large")
                body.extend(piece)
    except httpx.HTTPError as exc:
        raise ScopedRenderCapabilitiesError("scoped capability read did not answer") from exc
    try:
        return decode_render_json(bytes(body))
    except ValueError:
        raise ScopedRenderCapabilitiesError("scoped capability read was not valid JSON") from None


def _fetch_scoped_worker(controller_id: str, machine_id: str,
                         requested_contract: str) -> dict | None:
    payload = _scoped_capability_payload(controller_id, machine_id, requested_contract)
    return _decode_scoped_capabilities(payload, requested_contract=requested_contract,
                                       now=time.time())


def _render_candidate_shape(value: object, *, qualification: bool) -> dict | None:
    if not isinstance(value, dict):
        return None
    required = {
        "vector_id", "hardware_class", *_RENDER_DIGEST_FIELDS, "selected_tier", "exact_encoder",
        "font_support", "bounds", "profile_execution_bounds", "production_eligible",
        "qualification_only", "available_capacity",
    } if qualification else {
        "hardware_class", *_RENDER_DIGEST_FIELDS, "selected_tier", "exact_encoder",
        "font_support", "bounds", "profile_execution_bounds", "production_eligible",
        "production_enabled", "available_capacity",
    }
    if ((qualification and set(value) not in (required, required | {"encode"}))
            or (not qualification and any(key not in value for key in required))):
        return None
    if "encode" in value and not render_encode_document(value["encode"]):
        return None
    if any(not isinstance(value.get(key), str) or len(value[key]) != 64
           or any(char not in "0123456789abcdef" for char in value[key])
           for key in _RENDER_DIGEST_FIELDS):
        return None
    if not all(isinstance(value.get(key), str) and value[key] for key in
               ("hardware_class", "selected_tier", "exact_encoder")):
        return None
    if type(value.get("font_support")) is not bool:
        return None
    bounds = value.get("bounds")
    if not isinstance(bounds, dict) or set(bounds) != set(_RENDER_BOUND_FIELDS):
        return None
    if any(type(bounds[key]) is not int or bounds[key] <= 0 for key in _RENDER_BOUND_FIELDS):
        return None
    if not isinstance(value.get("profile_execution_bounds"), dict):
        return None
    capacity = value.get("available_capacity")
    if (not isinstance(capacity, dict) or set(capacity) != {"slots", "slots_total"}
            or any(type(capacity[key]) is not int or capacity[key] < 0 for key in capacity)):
        return None
    if qualification:
        if (value.get("production_eligible") is not False
                or value.get("qualification_only") is not True
                or not isinstance(value.get("vector_id"), str) or not value["vector_id"]
                or capacity["slots_total"] != 1
                or capacity["slots"] not in (0, 1)):
            return None
    elif (type(value.get("production_eligible")) is not bool
          or type(value.get("production_enabled")) is not bool):
        return None
    return value


def _measured_render_speed(machine: dict) -> dict[str, Any] | None:
    """This Mac's own measured final-render speed, or `None` when its Hub stated none.

    The shape is what the Hub's `fleet_overview` machine row publishes from its
    `render_history.speed_by_machine`: `{"speed": output_seconds_per_wall_second, "renders": N}`.
    Read with a first-non-`None` lookup and a positive check, never an `or` chain: a stated `0`
    would be a claim ("measured as infinitely slow"), and absence is the honest different fact.
    """
    value = machine.get("render_speed")
    if not isinstance(value, dict):
        return None
    speed = value.get("speed")
    renders = value.get("renders")
    if (not isinstance(speed, (int, float)) or isinstance(speed, bool)
            or not math.isfinite(float(speed)) or speed <= 0
            or not isinstance(renders, int) or isinstance(renders, bool) or renders < 1):
        return None
    return {"output_seconds_per_wall_second": float(speed), "renders": renders}


def _render_candidate_order(item: dict) -> tuple:
    """**Fastest first, and every step of the reasoning is a published fact** (owner, 2026-10-28).

    When several opted-in render Macs are eligible the render goes to the fastest one; only a busy
    or ineligible one sends it down the list, which `candidates[0]` in `runtime._render_destination`
    already does by taking the head.

    * **Measured first.** A Mac whose own Hub has timed a render publishes
      `measured_render_speed` (output seconds per wall second, from `render_history`), and a
      higher ratio sorts ahead. That is evidence from this fleet, not a claim about silicon.
    * **Unmeasured Macs follow measured ones**, which is the honest order rather than a guess:
      there is nothing to compare, and putting an unmeasured M4 ahead of a measured M1 would be
      inventing a number the Mac never stated. Among the unmeasured, hardware is the stated
      fallback — chip generation, then memory — and the Fleet page says so.
    * **No machine list anywhere.** Nothing here names a Mac.
    * Qualified Macs and owner-accepted ones keep their existing tiers, and the Mac's own saved
      `video` rank still breaks a tie, exactly as before.
    """
    hardware = str(item.get("hardware_class") or "").upper()
    chip = 0 if "M4" in hardware else 1 if "M2" in hardware else 2 if "M1" in hardware else 3
    measured = item.get("measured_render_speed")
    ratio = measured.get("output_seconds_per_wall_second") if isinstance(measured, dict) else None
    measured_ratio = (float(ratio) if isinstance(ratio, (int, float))
                      and not isinstance(ratio, bool) and ratio > 0 else None)
    # Measured Macs first, best first; then the unmeasured by hardware. One tuple key, so the
    # reason the page names and the value the sort uses cannot come from two different readings.
    by_speed = (0, -measured_ratio) if measured_ratio is not None else (1, 0.0)
    machine = item.get("selected_machine") if isinstance(item.get("selected_machine"), dict) else {}
    memory = -(machine.get("memory_gb") or 0.0)
    # Qualified (and one-use qualification) Macs keep their order; an unqualified Mac comes after
    # them, the ones on this Mac's own network ahead of the rest.
    tier = (1 + render_optin.network_rank(item["network"]) if item.get("authority") else 0)
    return (tier, by_speed, chip, memory, item.get("priority_rank"),
            item["site_id"], item["node_id"])


def _render_candidates_order(candidates: list[dict]) -> list[dict]:
    """`render_candidates`' rows in send order. Named so the page and a test read the same function."""
    return sorted(candidates, key=_render_candidate_order)


def render_candidates(required_profile_digest: str | None, *,
                      qualification: bool = False, available: bool = True,
                      resolve_encode_authority: bool = False,
                      requested_contract: str | None = None) -> list[dict]:
    """Qualified physical final-render candidates, independent of model catalogue rows.

    With `qualification`, a worker may offer its first qualification candidate. `available=False`
    describes enabled compatible workers without treating occupied slots or current memory as
    missing support. `resolve_encode_authority` yields provisional rows: the caller must resolve
    current tuple permission and memory bounds before advertising or dispatching them. That keeps
    a measured tuple independent of a different legacy tier's unqualified owner grant.

    With `requested_contract`, each operationally-eligible node's capability row comes from the
    scoped Hub envelope for that exact contract instead of the snapshot's legacy row — while
    every operational gate below still reads the fleet rows, and a transport, envelope or profile
    failure raises rather than reading as measured zero support.
    """
    if requested_contract not in (None, LATE_LABEL_CONTRACT, SUBTITLE_CONTRACT):
        return []
    if (not isinstance(required_profile_digest, str)
            or len(required_profile_digest) != 64
            or any(char not in "0123456789abcdef" for char in required_profile_digest)):
        return []
    view = snapshot(allow_stale=True)
    generated_at = view.get("generated_at") if isinstance(view, dict) else None
    now = time.time()
    if (type(generated_at) not in (int, float) or isinstance(generated_at, bool)
            or not math.isfinite(generated_at) or generated_at > now
            or now - generated_at > _MEMBERS_STALE_AFTER_S):
        return []
    rows = view.get("locations") or []
    configured = {str(row.get("id") or "") for row in roster()}
    opted_in = render_optin.enabled_keys()
    networks: list | None = None
    candidates: list[dict] = []
    for location in rows:
        if (not isinstance(location, dict)
                or str(location.get("id") or "") not in configured | {LOCAL_ID}
                or location.get("reachable") is not True or location.get("ready") is not True
                or location.get("drained") is not False
                or not location.get("site_id") or not location.get("controller_id")
                or "paused_at" not in location or "members_stale_seconds" not in location
                or location.get("members_from") is not None
                or location.get("members_stale_seconds") is not None):
            continue
        # **A paused location still offers the render Macs whose own pause says render is
        # exempt** (owner, 2026-10-28). That is the whole of the exemption: the pause, for final
        # render only, on a Mac switched on in this section and at its own Hub. Every other
        # condition above and below still applies — an unreachable, drained, stale or unavailable
        # location, and every other function, are refused exactly as before.
        location_paused = location.get("paused_at") is not None
        machines = {str(item.get("physical_machine_id") or item.get("machine")
                        or item.get("id") or ""): item
                    for item in (location.get("machines") or []) if isinstance(item, dict)}
        nodes = {str(item.get("id") or ""): item for item in (location.get("nodes") or [])
                 if isinstance(item, dict)}
        for worker in location.get("workers") or []:
            if (not isinstance(worker, dict)
                    or "video.final_render" not in (worker.get("supported_operations") or [])
                    or (available and (worker.get("online") is not True
                                       or worker.get("ready") is not True
                                       or worker.get("busy") is not False))
                    or worker.get("drained") is not False
                    or worker.get("maintenance") is not False
                    or worker.get("quarantined") is not False):
                continue
            raw: object = worker
            qualifying = False
            machine_id = str(worker.get("physical_machine_id") or worker.get("machine") or "")
            exempt = worker.get(render_optin.PAUSED_RENDER_KEY) is True
            if location_paused and not exempt:
                continue
            machine = machines.get(machine_id, {})
            node = nodes.get(machine_id, {})
            controller_local = machine_id == LOCAL_ID
            available_gb = machine.get("available_gb")
            disk_free_gb = machine.get("disk_free_gb")
            if (not machine_id or machine_id == str(location.get("controller_id") or "")
                    or (available and (machine.get("reachable") is not True
                        or type(available_gb) not in (int, float)
                        or isinstance(available_gb, bool) or not math.isfinite(available_gb)
                        or available_gb <= 0
                        or ("memory_pressure" in machine
                            and machine.get("memory_pressure") != "normal")
                        or type(disk_free_gb) not in (int, float)
                        or isinstance(disk_free_gb, bool) or not math.isfinite(disk_free_gb)
                        or disk_free_gb <= 0 or machine.get("current_job") is not None
                        or "current_job" not in machine
                        or (not controller_local and node.get("state") != "reachable")))
                    or not isinstance(node, dict)
                    # **The work order's own on/off switch** (owner, 2026-10-28): a Mac that
                    # switched final render off takes no render at all, pause-exemption or not.
                    or not _machine_function_enabled(machine, node, "render")
                    # `paused` is the controller's own machine pause; the exemption is that same
                    # pause, for render only, so it is the one flag it may overrule.
                    or (not controller_local and (
                        (node.get("paused") is not False and not exempt)
                        or (available and ("current_job" not in node
                                           or node.get("current_job") is not None))
                        or node.get("on_previous_token") is not False))):
                continue
            if requested_contract is not None:
                # The snapshot's own row is the legacy profile's; only the scoped envelope can
                # authorise this contract, and it must name the digest this caller selected.
                # Every operational gate above still came from the fleet rows, never from here.
                scoped = _fetch_scoped_worker(location["id"], machine_id, requested_contract)
                if scoped is None:
                    continue
                production_shaped = (scoped.get("production_enabled") is True
                                     and scoped.get("production_eligible") is True)
                claim = render_optin.unqualified_claim(scoped)
                unqualified = claim == "accepted" and production_shaped
                if claim == "invalid" or (unqualified and not resolve_encode_authority
                        and f"{location['id']}/{machine_id}" not in opted_in):
                    continue
                if not production_shaped:
                    # Protocol support alone grants no execution admission.
                    continue
                capability = _render_candidate_shape(scoped, qualification=False)
                if capability is None:
                    raise ScopedRenderCapabilitiesError("scoped capability row is malformed")
                if capability["profile_digest"] != required_profile_digest:
                    raise ScopedRenderCapabilitiesError(
                        "scoped capability row pins another profile")
            else:
                production_shaped = (worker.get("production_enabled") is True
                                     and worker.get("production_eligible") is True)
                claim = render_optin.unqualified_claim(worker)
                # A Hub that accepts unqualified work publishes a production-shaped row plus its evidence.
                # That row is only ever used on the owner's say-so here; a claim that is not the Hub's
                # evidence cannot be told from a qualified row, so it is not used at all.
                unqualified = claim == "accepted" and production_shaped
                if claim == "invalid" or (unqualified and not resolve_encode_authority
                        and f"{location['id']}/{machine_id}" not in opted_in):
                    continue
                if not production_shaped:
                    match = next((item for item in (worker.get("qualification_candidates") or [])
                                  if isinstance(item, dict)), None) if qualification else None
                    if match is None:
                        continue
                    raw, qualifying = match, True
                capability = _render_candidate_shape(raw, qualification=qualifying)
                if capability is None or capability["profile_digest"] != required_profile_digest:
                    continue
            available_memory = (max(0, int((available_gb - 0.005) * 1_000_000_000))
                                if type(available_gb) in (int, float)
                                and math.isfinite(available_gb) else None)
            # Exact-encode callers apply that tuple's measured bounds after the read-only probe.
            if (available and not resolve_encode_authority
                    and available_memory < capability["bounds"]["min_available_memory_bytes"]):
                continue
            slots = capability["available_capacity"]["slots"]
            label = machine.get("label")
            chip = machine.get("chip")
            memory_gb = machine.get("memory_gb")
            if ((available and slots <= 0) or capability["available_capacity"]["slots_total"] <= 0
                    or type(label) is not str or not label
                    or type(chip) is not str or not chip
                    or type(memory_gb) not in (int, float)
                    or not math.isfinite(memory_gb) or memory_gb <= 0):
                continue
            if unqualified and networks is None:
                networks = render_optin.local_networks()
            candidates.append({
                "site_id": str(location["site_id"]),
                "roster_controller_id": str(location["id"]),
                "hub_controller_id": str(location["controller_id"]),
                "target_hub_controller_id": (str(location["controller_id"])
                                             if controller_local else machine_id),
                "node_id": machine_id,
                "hardware_class": capability["hardware_class"],
                "profile_digest": capability["profile_digest"],
                "qualification_vector_id": capability.get("vector_id"),
                "available_slots": slots,
                # **What this Mac's own render history measured, or None** (owner, 2026-10-28).
                # Output seconds per wall second, from the Hub's `render_history`, on the machine
                # row rather than the render worker row (that row's key set is the mixed-fleet
                # contract). `None` means "not measured yet", never zero, so the ordering can tell
                # evidence from absence and the page can say which it is showing.
                "measured_render_speed": _measured_render_speed(machine),
                "render_while_paused": capability.get("render_while_paused") is True,
                "available_memory_source": {
                    "field": "available_gb", "unit_bytes": 1_000_000_000,
                    "decimal_places": 2, "conservative_bytes": available_memory,
                },
                "priority_rank": (
                    _machine_priority_order(machine, nodes.get(machine_id) or {}).index("video")
                    if "video" in _machine_priority_order(
                        machine, nodes.get(machine_id) or {}
                    ) else _UNLISTED_PRIORITY
                ),
                "selected_machine": {
                    "id": machine_id, "label": label, "chip": chip,
                    "memory_gb": memory_gb,
                    "hardware_class": capability["hardware_class"],
                },
                "capability": capability,
                # Only an owner-accepted unqualified Mac carries these; every other row is as it was.
                **({"authority": render_optin.ACCEPTED,
                    "network": render_optin.classify_machine(machine, node.get("address"), networks)[0]}
                   if unqualified else {}),
            })
    # The one ordering function, module level so the page and the dispatcher cannot drift.
    return _render_candidates_order(candidates)


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

# Studio Hub names the local Voice function `voice`; the public Gateway lane is `tts`.
_PRIORITY_FUNCTION_BY_MODALITY = {**{name: name for name in HUB_OPERATION_BY_MODALITY}, "tts": "voice"}
# Final render is a rankable function with no operation of its own (owner, 2026-10-28): it is
# settled by `render_candidates`, not by catalogue routing, but it is named in the same saved
# order a Mac publishes, so reading that order must accept it.
_PRIORITY_FUNCTIONS = frozenset(_PRIORITY_FUNCTION_BY_MODALITY.values()) | {"render"}
_NEUTRAL_PRIORITY = float(len(_PRIORITY_FUNCTIONS))
_UNLISTED_PRIORITY = _NEUTRAL_PRIORITY + 1.0


def _priority_order(machine: dict) -> list[str]:
    """One Mac's ordered function preference, with the legacy Chat flag as a read fallback.

    An explicit array is authoritative. A malformed array is no preference rather than a partial
    guess, and therefore never falls through to the legacy boolean. Missing arrays on Hub 0.6.156
    retain its `chat_priority` meaning until every Mac has upgraded.
    """
    if "priority_order" not in machine:
        return ["chat"] if machine.get("chat_priority") is True else []
    value = machine.get("priority_order")
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or item not in _PRIORITY_FUNCTIONS for item in value)):
        return []
    if len(value) != len(set(value)):
        return []
    return list(value)


def _routing_preference(row: dict, model_id: str, modality: str, slots: int) -> tuple[float, int]:
    """Best function rank among this model's eligible Macs, plus how many share it.

    Listed functions sort ahead of a Mac with no preference; an explicitly unlisted function sorts
    behind it. Ordinal positions use the fixed six-function vocabulary, so second means second in
    both partial and full lists. This is only a routing tiebreak: queue pressure and a free slot still
    win first, which is the idle-lending rule.
    """
    function = _PRIORITY_FUNCTION_BY_MODALITY.get(modality, modality)
    machines = {str(machine.get("machine") or ""): machine for machine in row.get("machines") or []
                if isinstance(machine, dict) and machine.get("machine")}
    nodes = {str(node.get("id") or ""): node for node in row.get("nodes") or []
             if isinstance(node, dict) and node.get("id")}
    eligible = _eligible_macs(row, model_id, modality)
    if not eligible:
        return _NEUTRAL_PRIORITY, max(slots, 1)

    # A busy Mac cannot donate its preference to a different idle Mac. When the Hub names at least
    # one available model-specific slot, score only those Macs; if every fitting Mac is busy, their
    # ranks still break ties between equally loaded locations without claiming capacity exists.
    available = {machine: model for machine, model in eligible.items() if model.get("available") is True}
    if available:
        eligible = available

    scores: list[float] = []
    for machine in eligible:
        order = _machine_priority_order(machines.get(machine) or {}, nodes.get(machine) or {})
        if not order:
            scores.append(_NEUTRAL_PRIORITY)
        elif function in order:
            scores.append(float(order.index(function)))
        else:
            scores.append(_UNLISTED_PRIORITY)
    best = min(scores)
    return best, sum(score == best for score in scores)


def _eligible_macs(row: dict, model_id: str, modality: str) -> dict[str, dict]:
    """`machine -> local model row` for the Macs at this site that may run `model_id` now.

    Ready, memory-eligible, not paused, not resting after a memory refusal, serving it, reachable,
    enabled and not a resting workstation: the rule `_routing_preference` scores by.
    """
    function = _PRIORITY_FUNCTION_BY_MODALITY.get(modality, modality)
    machines = {str(machine.get("machine") or ""): machine for machine in row.get("machines") or []
                if isinstance(machine, dict) and machine.get("machine")}
    nodes = {str(node.get("id") or ""): node for node in row.get("nodes") or []
             if isinstance(node, dict) and node.get("id")}
    paused = {machine for machine, node in nodes.items() if node.get("paused") is True}
    workers = [worker for worker in row.get("workers") or [] if isinstance(worker, dict)
               and model_id in (worker.get("models") or [])]
    working = {str(worker.get("machine") or "") for worker in workers
               if worker.get("online") is not False and worker.get("drained") is not True
               and not _truthy(worker.get("maintenance")) and not _truthy(worker.get("quarantined"))}
    cooled = memory_cooled_machine_ids(row, model_id)
    eligible: dict[str, dict] = {}
    for model in row.get("local_models") or []:
        if not isinstance(model, dict):
            continue
        current = str(model.get("model_id") or model.get("model") or "").strip()
        machine = str(model.get("machine") or "")
        stated_modality = str(model.get("modality") or modality)
        stated_function = _PRIORITY_FUNCTION_BY_MODALITY.get(stated_modality, stated_modality)
        if (current == model_id and machine and model.get("memory_eligible") is True
                and model.get("runtime_ready") is not False and model.get("gated") is not True
                and stated_function == function
                and machine not in paused and machine not in cooled
                and (not workers or machine in working)):
            fact = machines.get(machine) or {}
            node = nodes.get(machine) or {}
            workstation = _workstation_fact(fact, node)
            if (fact.get("reachable") is not False and fact.get("enabled") is not False
                    and _machine_function_enabled(fact, node, function)
                    and not (workstation and workstation_resting({"workstation": workstation}))):
                eligible[machine] = model
    return eligible


def _function_off_at_site(row: dict, model_id: str, modality: str) -> bool:
    """True when the Macs at `row` that serve `(model_id, modality)` have switched that kind off.

    **Owner, 2026-10-28: a kind that is off is not taken by that Mac at all.** When every Mac at a
    site that can run `model_id` for `modality` has switched that function off (or is paused/drained
    beside one that switched it off), the site itself cannot take that kind of job and must not be
    offered as a candidate — while every other function on the same site stays unaffected.
    """
    function = _PRIORITY_FUNCTION_BY_MODALITY.get(modality, modality)
    machines = {str(m.get("machine") or ""): m for m in row.get("machines") or []
                if isinstance(m, dict) and m.get("machine")}
    nodes = {str(n.get("id") or ""): n for n in row.get("nodes") or []
             if isinstance(n, dict) and n.get("id")}
    paused = {mid for mid, n in nodes.items() if n.get("paused") is True}
    serving = [
        str(m.get("machine") or "") for m in row.get("local_models") or []
        if isinstance(m, dict) and model_id in (m.get("model_id"), m.get("model"))
        and m.get("memory_eligible") is not False
        and _PRIORITY_FUNCTION_BY_MODALITY.get(str(m.get("modality") or modality),
                                               str(m.get("modality") or modality)) == function
        and m.get("machine")
    ]
    if not serving:
        unfit = {str(m.get("machine")) for m in row.get("local_models") or []
                 if isinstance(m, dict) and model_id in (m.get("model_id"), m.get("model"))
                 and m.get("memory_eligible") is False}
        serving = [str(w.get("machine")) for w in row.get("workers") or []
                   if isinstance(w, dict) and model_id in (w.get("models") or [])
                   and w.get("machine") and str(w.get("machine")) not in unfit
                   and w.get("drained") is not True]
    if not serving:
        serving = list(machines or nodes)
    if not serving:
        return False
    switched_off = {mid for mid in serving
                    if not _machine_function_enabled(machines.get(mid) or {},
                                                     nodes.get(mid) or {}, function)}
    return bool(switched_off) and all(mid in switched_off or mid in paused for mid in serving)


#: Modalities whose result is a file the Mac has to write. A Mac out of disk cannot produce one,
#: whatever model it has: measured 2026-11-16, a voice job was sent to a Mac whose managed output
#: storage was full and was refused `VOICE_REFERENCE_REJECTED`. Video render already refuses a
#: `disk` red/stop machine this way (`_render_candidates`); this asks the same question of the
#: other output modalities, at dispatch.
_STORAGE_BACKED_MODALITIES = frozenset({"tts", "music", "video"})
#: `machine_disk` states that mean the Mac cannot take output (black is the Hub's own stop).
_DISK_STOPPED = frozenset({"red", "stop", "black"})


def _no_disk_for_output(row: dict, model_id: str, modality: str) -> bool:
    """Whether every Mac here that could run `model_id` reports a disk that cannot take output.

    Only a positive reading of **every** able Mac says so: a site that states no per-machine rows,
    or one Mac whose disk is unstated, is not refused on a guess.
    """
    if modality not in _STORAGE_BACKED_MODALITIES:
        return False

    def stopped(machine: dict) -> str:
        disk = machine_disk(machine) or {}
        # Only the Hub's own state is acted on; a state this gateway derived is for showing.
        return str(disk.get("state") or "") if disk.get("state_source") == "hub" else ""

    machines = [m for m in row.get("machines") or [] if isinstance(m, dict)]
    # **The controller's own disk gates its whole site.** A work POST is refused
    # `OUTPUT_STORAGE_BLOCKED` by the Mac that receives it, and the controller receives every one:
    # measured 2026-11-16, terranash-1300 (disk "stop", 4.1 GB free) refused nine voice jobs
    # fifteen times each while its own nodes were green.
    if any(str(m.get("machine") or "") == LOCAL_ID and stopped(m) in _DISK_STOPPED for m in machines):
        return True
    able = set(_eligible_macs(row, model_id, modality))
    if not able:
        return False
    states = {stopped(m) for m in machines if str(m.get("machine") or "") in able}
    return bool(states) and states <= _DISK_STOPPED


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
            # **Summed, not last-wins.** The Hub groups supply by (model, operation, runtime
            # revision, contract hash), so Macs on different revisions of one model are separate
            # rows; keeping the last let one subset of a site's Macs speak for all of them.
            # ponytail: a Mac in two rows of one model would count twice; union the rows'
            # `machines` if that ever shows up.
            entry = index.setdefault(model_id, {"eligible": 0, "available": 0})
            entry["eligible"] += _as_int(row.get("eligible_physical_slots_total"))
            entry["available"] += _as_int(row.get("available_physical_slots"))
    if not index and fallback:
        index = dict(fallback)
    return index


#: **A Mac that just refused a model for memory is not offered it again for a while** (F1, fleet
#: audit 2026-10-07). On 10-04 the fleet logged 993 `INSUFFICIENT_MEMORY` hand-backs on 110 jobs
#: (~9 each, ~160 s each); 898 came from 8 GB Macs the Hub's supply still counted as eligible.
#: `(location id, model id) -> {Mac: (until, GB it said it needs)}`. In memory only: a restart
#: forgets it, which costs at most one more refusal per Mac.
MEMORY_COOLDOWN_S = 15 * 60.0
#: How far above its stated need a Mac's reported free memory must be to end its cool-down early.
#: ponytail: a flat margin. Idle 8 GB Macs read ~3.2 GB free in the fleet read (live, 2026-10-07)
#: while their own check refused at 2.4 for a 3.2 GB need, so `free >= need` would end the rest
#: at once for exactly the Macs it is for; raise it if refusals recur right after a clear.
MEMORY_CLEAR_MARGIN_GB = 1.0
_MEMORY_COOLDOWN: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
_MEMORY_REFUSAL = re.compile(r"INSUFFICIENT_MEMORY: (.+?) needs about ([\d.]+) GB free")


def note_memory_refusal(provider_id: str, model_id: str, detail: str, now: float | None = None) -> None:
    """Start the cool-down for the Mac a Hub memory hand-back names (Hub 0.6.127+ sentence)."""
    match = _MEMORY_REFUSAL.search(detail or "")
    location = controller_id_from(provider_id)
    if match and location:
        until = (time.time() if now is None else now) + MEMORY_COOLDOWN_S
        _MEMORY_COOLDOWN.setdefault((location, model_id), {})[match[1]] = (until, float(match[2]))


def memory_cooled(location_id: str, model_id: str, machines: object = None, now: float | None = None,
                  *, reachable_only: bool = False) -> int:
    """How many Macs at this location are cooling down for this model.

    A Mac leaves early once the fleet read shows it with clearly more free memory than it said it
    needed (`MEMORY_CLEAR_MARGIN_GB`)."""
    macs = _MEMORY_COOLDOWN.get((location_id, model_id))
    if not macs:
        return 0
    now = time.time() if now is None else now
    free: dict[str, float] = {}
    listed = [m for m in machines if isinstance(m, dict)] if isinstance(machines, list) else []
    for machine in listed:
        gb = _num(machine.get("available_gb"))
        if gb is not None:
            free.update({str(name): gb for name in (machine.get("label"), machine.get("machine")) if name})
    for name, (until, need) in list(macs.items()):
        if until <= now or free.get(name, -1.0) >= need + MEMORY_CLEAR_MARGIN_GB:
            macs.pop(name, None)
    if reachable_only and listed:
        reachable = {str(name) for machine in listed if machine.get("reachable") is not False
                     for name in (machine.get("label"), machine.get("machine")) if name}
        return sum(name in reachable for name in macs)
    return len(macs)


def memory_cooled_machine_ids(row: dict, model_id: str) -> set[str]:
    """Machine ids currently resting after a model-specific memory refusal."""
    location_id = str(row.get("id") or "")
    machines = [machine for machine in row.get("machines") or [] if isinstance(machine, dict)]
    memory_cooled(location_id, model_id, machines)
    cooled = set((_MEMORY_COOLDOWN.get((location_id, model_id)) or {}).keys())
    return {
        str(machine.get("machine"))
        for machine in machines
        if machine.get("machine") and cooled.intersection(
            str(name) for name in (machine.get("machine"), machine.get("label")) if name
        )
    }


#: **A Mac that ran out of output space refuses every job until its Trash Folder is emptied.**
#: Measured 2026-11-16: `VOICE_REFERENCE_REJECTED: managed output storage has insufficient free
#: space; empty the Trash Folder` — the first of three attempts a voice job spent before the
#: ceiling ended it. The Hub's sentence names no Mac, so the rest is per location and model, the
#: same shape and effect as F1's memory rest (a spent location is offered only when nothing else
#: can take the model). In memory only: a restart costs at most one more refusal.
STORAGE_COOLDOWN_S = 15 * 60.0
_STORAGE_COOLDOWN: dict[tuple[str, str], float] = {}


def note_storage_refusal(provider_id: str, model_id: str, now: float | None = None) -> None:
    """Rest a location that just refused work for lack of output space."""
    location = controller_id_from(provider_id)
    if location:
        _STORAGE_COOLDOWN[(location, model_id)] = (
            (time.time() if now is None else now) + STORAGE_COOLDOWN_S)


def storage_cooled(location_id: str, model_id: str, now: float | None = None) -> bool:
    """Whether this location refused work for lack of output space inside its rest window."""
    until = _STORAGE_COOLDOWN.get((location_id, model_id))
    if until is None:
        return False
    if until <= (time.time() if now is None else now):
        _STORAGE_COOLDOWN.pop((location_id, model_id), None)
        return False
    return True


#: **A location installing an update is not offered work until it is back** (Kind I, 2026-10-19).
#: The Hub answers `MACHINE_BUSY: this Mac is finishing its current work to install an update`
#: when it has deliberately stopped taking work to install. Measured 2026-10-19: 885 such lines
#: across the two live gateway logs (856 in `.log.1`, 29 in `.log`), and job
#: `97bdce60-90f4-4f47-b789-073744e87ea7` was sent to the same installing Mac
#: **124 times** (`attempt_count` 126) before it failed with "tried 3 machines and every one
#: failed" naming one machine 124 times. `location id -> until`; in memory only, so a restart
#: forgets it and costs at most one more refusal, the same trade `_MEMORY_COOLDOWN` makes.
DRAIN_COOLDOWN_S = 15 * 60.0
_DRAIN_COOLDOWN: dict[str, float] = {}
#: The Hub's own words for an install. **The sentence, not the code**: `MACHINE_BUSY` is also
#: "local is reserved for other work on this Mac" (27 measured) and "every Mac with this Chat
#: model is busy" — a Mac that is merely busy is a moment that frees in seconds, and resting it
#: for the whole window would delay work the fleet is about to run. Only the install is a stop.
#: ponytail: a flat window; end the rest from the location's own `version_status` when the
#: install finishes, if a 15-minute wait for a two-minute install ever shows up in the queue.
_DRAIN_REFUSAL = re.compile(r"install(?:ing)? an update", re.IGNORECASE)


def note_drain_refusal(provider_id: str, detail: str, now: float | None = None) -> None:
    """Rest a location that said it is installing an update, so selection skips it until it is back."""
    location = controller_id_from(provider_id)
    if location and _DRAIN_REFUSAL.search(detail or ""):
        _DRAIN_COOLDOWN[location] = (time.time() if now is None else now) + DRAIN_COOLDOWN_S


#: **A location that refused the connection takes no new work until it answers again**
#: (2026-11-16: terranash-1200 refused connections at 21:10, yet 20 voice sections were still sent
#: there and polled until they became holds). The fleet read is cached for `_CACHE_TTL` and a flapping
#: Hub can pass one probe and refuse the next, so the worker's own refusal is the faster fact.
#: `location id -> when it refused`; cleared by the next live read of that location
#: (`_remember_live_read`), never by a clock. In memory only, like the drain rest.
_REFUSED_AT: dict[str, float] = {}
_LIVE_READ_AT: dict[str, float] = {}


def note_connection_refused(provider_id: str, now: float | None = None) -> None:
    """The worker could not connect to this location: keep it out of routing until it answers."""
    location = controller_id_from(provider_id)
    if location:
        _REFUSED_AT[location] = time.time() if now is None else now


def _remember_live_read(location_id: str, now: float | None = None) -> None:
    _LIVE_READ_AT[str(location_id)] = time.time() if now is None else now


def is_refusing_provider(provider_id: str) -> bool:
    """True while a location's last refused connection is newer than its last live read."""
    location = controller_id_from(provider_id)
    refused = _REFUSED_AT.get(location or "")
    if refused is None:
        return False
    if _LIVE_READ_AT.get(location or "", 0.0) > refused:
        _REFUSED_AT.pop(location or "", None)
        return False
    return True


def is_draining_provider(provider_id: str, now: float | None = None) -> bool:
    """True while a location is installing an update and must not be offered work."""
    location = controller_id_from(provider_id)
    if location is None:
        return False
    until = _DRAIN_COOLDOWN.get(location)
    if until is None:
        return False
    if until <= (time.time() if now is None else now):
        _DRAIN_COOLDOWN.pop(location, None)
        return False
    return True


def reachable_machines(row: dict) -> int | None:
    """Macs at a location that are not known to be offline; `None` when the Hub listed none (F3)."""
    machines = [m for m in row.get("machines") or [] if isinstance(m, dict)]
    return sum(1 for m in machines if m.get("reachable") is not False) if machines else None


def usable_slots(row: dict, model_id: str, eligible: int) -> int:
    """A model's eligible Macs at one location as a budget may count them (F1 + F3).

    Never more than the Macs that are reachable now (a Shops site counted 3 slots with 2 iMacs
    offline, and 26 % of its images waited 30 s+), less the Macs cooling down after a memory refusal.
    """
    reach = reachable_machines(row)
    if reach is not None:
        eligible = min(eligible, reach)
    return max(eligible - memory_cooled(str(row.get("id") or ""), model_id, row.get("machines"),
                                       reachable_only=True), 0)


#: When each (site, model) last stated it had a Mac for that model. One Mac missing a heartbeat
#: reads `eligible 0` for a single fleet read, so a 0 inside the grace is a blip and not a fact;
#: a 0 past it (or with nothing to protect) is the site's own answer. Keyed per model because
#: Gemma 4 being gone at 1600 says nothing about Qwen at 1600.
_ELIGIBLE_AT: dict[tuple[str, str], float] = {}


def states_no_mac(row: dict, model_id: str, now: float | None = None) -> bool:
    """Whether a site states it has **no** Mac that can run `model_id`.

    `True` only for an explicit statement — `model_supply[model_id].eligible == 0`. Unstated
    supply is unknown and never reads as "none": an older Hub that publishes no per-model row
    keeps every fallback it had, and a value that is not a number is not a reading.

    **One bad read is not an outage, and this is the same grace `usable_location_ids` gives a
    slow site answer** (`LOCK_ANSWER_GRACE_S`). A site that stated `eligible > 0` inside the
    grace still offers the model; past it, a 0 is the site's own answer. A site that has never
    stated `eligible > 0` since this process started reads as none at once — there is no earlier
    reading to protect, and the real case (1600 answering with nine Macs up and none for Gemma 4)
    must not sit for 45 s before the door refuses it.

    The same answer for the row and for routing, because both ask this one function.
    """
    supply = (row.get("model_supply") or {}).get(model_id)
    if not isinstance(supply, dict):
        return False
    eligible = supply.get("eligible")
    if isinstance(eligible, bool) or not isinstance(eligible, (int, float, str)):
        return False
    try:
        eligible = int(eligible)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    key = (str(row.get("id") or ""), str(model_id))
    if eligible > 0:
        _ELIGIBLE_AT[key] = now
        return False
    if eligible != 0:
        return False
    stated_at = _ELIGIBLE_AT.get(key)
    if stated_at is None:
        return True
    if now - stated_at > LOCK_ANSWER_GRACE_S:
        _ELIGIBLE_AT.pop(key, None)
        return True
    return False


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


#: The capability token a location's worker advertises when it can render from reference images.
#: The Hub publishes it in its own catalogue (`capabilities: ["txt2img", "edit"]`) and only on a Mac
#: whose worker has the edit lane, the weights and the memory — so the gateway reads it rather than
#: deciding either of those itself.
EDIT_CAPABILITY = "edit"


def advertises(model: dict, capability: str) -> bool:
    """Whether one location's discovered model row advertises a capability token.

    Read from the Hub's own statement, never derived from an engine name or a model id. A row that
    states no capabilities states nothing, which is not the same as stating that it cannot — but for
    routing the two answers coincide: work may only go where the token is actually advertised.
    """
    raw = model.get("raw") or {}
    wanted = str(capability or "").strip().lower()
    if wanted in {"txt2img", EDIT_CAPABILITY}:
        from .providers.definitions import hub_image_mode
        mode = hub_image_mode(raw, wanted)
        if mode is not None:
            return mode["available"]
        workers = raw.get("image_workers") or [raw]
        # An older, undescribed text lane keeps its existing route; explicit modes fail closed.
        return wanted == "txt2img" and all(
            not worker.get("capabilities") and "image_modes" not in worker for worker in workers
            if isinstance(worker, dict)
        )
    capabilities = raw.get("capabilities")
    return isinstance(capabilities, list) and bool(wanted) and any(
        str(item).strip().lower() == wanted for item in capabilities
    )


def required_capability(modality: str, params: Any) -> str | None:
    """The capability a job's **own parameters** demand of the location that runs it.

    Image jobs demand `txt2img` or `edit` from the Hub's published modes. `create_image` stages
    references under `_reference_digests`, so that key (even empty) retains reference intent.
    Other modalities use any location offering the model.

    Derived from the request rather than passed by each caller, because there are two routing
    moments (the submit and the worker's re-route after a refusal) and a rule that only one of them
    remembers is how a reconsidered job ends up somewhere that cannot run it.
    """
    if modality != "image" or not isinstance(params, dict):
        return None
    return EDIT_CAPABILITY if "_reference_digests" in params else "txt2img"


def routing_candidates(
    model_id: str, modality: str, *, only_location: str | None = None,
    exclude_locations: set[str] | None = None,
    require_capability: str | None = None, image_params: dict | None = None,
    respect_model_supply: bool = True,
    rejections: list[dict] | None = None,
) -> list[dict]:
    """Every location that can run this model right now, with its pressure.

    A candidate must be a model the catalogue would **offer**, be answering, and
    -- when the request names a voice -- be the location that owns that voice.

    **Every skip can say why.** A caller that passes `rejections` gets one row per
    location the walk passed over -- `{location_id, provider_id, label, machine, reason,
    detail, last_seen_at?}` -- in the fixed vocabulary the dispatch contract publishes
    (`offline`, `paused`, `draining`, `no_slot`, `spent`, `model_not_published`,
    `model_not_installed`, `location_mismatch`, `memory_ineligible`, `unknown`). A skipped
    candidate used to leave no trace, so the live 2026-11-14 job showed a candidate that was
    offered and never selected with nothing saying why.

    "Enabled" is not that question. This used to test `is_enabled` plus a row
    match, while `enabled_models` -- the set the catalogue is built from -- also
    applies `is_configured` and `can_publish`. The two answers drifted, and the
    failure was worse than an extra candidate: `route` chose a location on the
    weaker test and `catalog.resolve` then rejected that pair on the stronger
    one, so a request answered **`MODEL_NOT_FOUND`** — with a perfectly good copy
    of the same model enabled on another site, never tried. The candidate set is
    therefore read from `enabled_models`, not re-derived; asking "can this be
    offered?" in two places is how the two answers came apart.

    `require_capability` narrows that set further to the locations whose worker advertises a token
    (`edit` for a reference-guided image). It is a requirement, so a location without it is not a
    candidate — "may this site do this at all" — and its absence leaves the set exactly as it was.

    `respect_model_supply=False` is for an image **pool**, which asks a different question: a pool
    is a menu of models and a Mac assigned one of them, so the pool's own `assigned_macs` check is
    the authority. Answering with a site's per-model statement here would drop a member a Mac is
    assigned and can run. Everything else keeps the default.
    """
    from .providers import registry

    # Stale rather than blocking, for the same reason the catalogue reads it that
    # way: a controller that is switched off costs the full timeout to fail, and
    # that cost lands on the request that submits the work. A route picked from a
    # view up to one refresher interval old can be wrong about a site that died
    # seconds ago, and the job then settles as `failed` -- which is the outcome
    # this endpoint already prefers to making the caller wait.
    locations = snapshot(allow_stale=True).get("locations") or []
    rows = {str(row.get("id")): row for row in locations}
    candidates: list[dict] = []
    seconds: Callable[[str, str], float] | None = None
    now = time.time()

    def reject(definition: Any, location_id: str, row: dict, reason: str, detail: str,
               machine: str | None = None, last_seen_at: float | None = None) -> None:
        if rejections is None:
            return
        rejections.append({
            "location_id": location_id,
            "provider_id": getattr(definition, "id", "") or provider_id_for(location_id),
            "label": row.get("label") or location_id,
            "machine": machine,
            "reason": reason,
            "detail": detail,
            **({"last_seen_at": last_seen_at} if last_seen_at is not None else {}),
        })

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
        if require_capability and not advertises(model, require_capability):
            # **Capability, not memory, is the gateway's question.** The Hub already decided per
            # Mac whether the weights are there and the RAM is enough, and a Mac that cannot do the
            # work does not advertise it — so a location whose row lacks the token is skipped rather
            # than offered work its own broker will refuse. One site that cannot therefore does not
            # take the feature down for the others; the catalogue publishes the union and this line
            # is what makes that honest.
            reject(definition, location_id, row, "location_mismatch",
                   f"this location does not offer {require_capability} for the model")
            continue
        if not row:
            # **Not read yet is not offline** (1301, 2026-11-16: ten queued voice sections failed
            # `DISPATCH_NO_ELIGIBLE_MACHINE` "offline (not read yet)" right after a restart, 1100
            # included, which was up). Skipped for routing, but stated as unknown.
            reject(definition, location_id, row, "unknown", "not read yet")
            continue
        if not row.get("reachable"):
            reject(definition, location_id, row, "offline",
                   "not answering" + (f": {row.get('error')}" if row.get("error") else ""),
                   last_seen_at=_last_machine_seen(row))
            continue
        # A site that cannot accept customer work is not a candidate: an agent-mode
        # Hub answers 409 to the submit, and a drained one has been told to stop.
        if not _takes_customer_jobs(row):
            reject(definition, location_id, row, *_no_work_reason(row))
            continue
        # Every Mac here that could run this model is paused: as good as unreachable for it.
        if no_free_mac(row, model_id):
            reject(definition, location_id, row, "paused",
                   "every Mac here that can run this model is paused")
            continue
        # **A Mac whose heartbeat has stopped is offline for routing** (`_machine_offline`).
        if _all_able_offline(row, model_id, now):
            reject(definition, location_id, row, "offline",
                   "every Mac here that can run this model is offline",
                   last_seen_at=_last_machine_seen(row))
            continue
        # **Every Mac here that could run this model switched this kind off** (owner, 2026-10-28):
        # never dispatch that kind to this site.
        if _function_off_at_site(row, model_id, modality):
            reject(definition, location_id, row, "model_not_installed",
                   f"every Mac here that can run this model has {modality} switched off")
            continue
        # **A location installing an update is not a candidate until it is back** (Kind I). It
        # said so itself, deliberately, and re-offering it is what turned one job into 126
        # attempts against a Mac that could not answer any of them.
        if is_draining_provider(definition.id):
            reject(definition, location_id, row, "draining",
                   "the location is installing an update")
            continue
        if is_refusing_provider(definition.id):
            reject(definition, location_id, row, "offline",
                   "it refused the connection; no new work until it answers again",
                   last_seen_at=_last_machine_seen(row))
            continue

        # **A site that states it has no Mac for this model is not a candidate.** Its controller
        # may answer perfectly — 1600 answers with nine Macs up and still has none for Gemma 4 —
        # so "the site is reachable" is not the question. Falling through to `by_operation`, the
        # site total and the reachable machine count would override the Hub's own statement and
        # hand the job to a site that has no Mac to run it on; it then sits until the 3-hour
        # ceiling. Skipping it here is what makes `route` return nothing when no site can take
        # the model, and `catalog.resolve` return no route, so the submit is refused at the door.
        # Unstated supply is unknown, never "none" (see `states_no_mac`), so an older Hub keeps
        # every fallback below.
        if respect_model_supply and states_no_mac(row, model_id):
            reject(definition, location_id, row, "model_not_installed",
                   "the location states it has no Mac that can run this model")
            continue

        from . import image_limits
        limits = image_limits.assess(model.get("raw") or {}, model_id, image_params) if modality == "image" and image_params is not None else None
        if limits is not None and not limits["allowed"]:
            reject(definition, location_id, row, "memory_ineligible",
                   str(limits.get("reason") or "no Mac here fits the model for this request"))
            continue

        capacity = row.get("capacity") or {}
        # The exact model first: how many machines at this site can run **this**
        # model. The old site-wide total counted each 8 GB image Mac in the weight
        # for a 24 GB model it can never run, so a small-Mac site out-weighed a
        # big-Mac one and the heavy jobs serialised on its single fitting Mac.
        supply = (row.get("model_supply") or {}).get(model_id) or {}
        slots = _as_int(supply.get("eligible"))
        available = _as_optional_int(supply.get("available"))
        # Every Mac here that fits it is offline or just refused it for memory (F1/F3): offered
        # only if no other location can take it, never dropped (a submit with no route is refused).
        usable = usable_slots(row, model_id, slots) if slots > 0 else 0
        # A location that just refused for lack of output space, or whose every able Mac reports a
        # disk that cannot take it, is offered only when nothing else can run the model — the same
        # last-resort rule the memory rest uses, because a submit with no route is refused.
        spent = ((slots > 0 and usable <= 0)
                 or storage_cooled(location_id, model_id)
                 or _no_disk_for_output(row, model_id, modality))
        if slots > 0:
            slots = max(usable, 1)
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
        reach = reachable_machines(row)
        if reach is not None:
            slots = min(slots, reach)  # offline Macs are not capacity (F3)
        if limits is not None:
            slots = min(slots, limits["eligible"]) if slots > 0 else limits["eligible"]
            if available is not None:
                available = min(available, slots)
        queue = _as_int(capacity.get("queue_depth"))
        if not capacity:
            queue = len(row.get("running_jobs") or [])
        preference, preference_slots = _routing_preference(row, model_id, modality, slots)
        speed = {}
        if speed_ranked(model_id):
            if seconds is None:
                seconds = _mac_seconds(rows, model_id, locations)
            speed = _site_speed(row, model_id, modality, seconds, available)

        candidates.append({
            **({"image_limit_machines": limits["machines"]} if limits is not None else {}),
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
            "spent": spent,
            "preference": preference,
            "preference_slots": preference_slots,
            **speed,
        })
    return [c for c in candidates if not c["spent"]] or candidates


#: Rejection reasons that mean "this location is busy now" rather than "it can never run this".
#: A job whose only rejections are these is a normal queue wait, never `DISPATCH_NO_ELIGIBLE_MACHINE`.
BUSY_REASONS = frozenset({"no_slot", "spent"})

#: The fixed rejection vocabulary of the dispatch contract (Story consumes it; the brief is
#: `_orchestra/gateway-lead/next-gate-batch-20261113.md`, item 16).
REJECTION_REASONS = ("offline", "paused", "draining", "no_slot", "spent", "model_not_published",
                     "model_not_installed", "location_mismatch", "memory_ineligible", "unknown")


def dispatch_eligibility(model_id: str, modality: str, *,
                         require_capability: str | None = None,
                         image_params: dict | None = None) -> dict:
    """Whether any reachable location can run this model **now**, and a reason for every one that cannot.

    `routing_candidates` answers the candidate half; this adds the locations the walk never even
    reached because they do not publish the model, and the fleet row's own reasons. The shape is
    the one the dispatch contract publishes: `candidates` (with `provider_id`, `location_id`,
    `slots`, …), `rejections` (`{location_id, provider_id, label, machine, reason, detail,
    last_seen_at?}`), `available`, `busy_only` and `checked_at`.

    **Busy is not broken.** `busy_only` is true when every rejection is `no_slot`/`spent` and a
    candidate exists somewhere; a caller must keep waiting in that case, not fail the job.
    """
    rejections: list[dict] = []
    candidates = routing_candidates(
        model_id, modality, require_capability=require_capability,
        image_params=image_params, rejections=rejections,
    )
    covered = {str(c.get("location_id")) for c in candidates}
    covered |= {str(r.get("location_id")) for r in rejections}
    try:
        rows = snapshot(allow_stale=True).get("locations") or []
    except Exception:  # noqa: BLE001 - an unread fleet is unknown, never "no eligible machine"
        rows = []
    for row in rows:
        location_id = str(row.get("id") or "")
        if not location_id or location_id in covered:
            continue
        if row.get("reachable") is False:
            rejections.append({
                "location_id": location_id, "provider_id": provider_id_for(location_id),
                "label": row.get("label") or location_id, "machine": None,
                "reason": "offline",
                "detail": str(row.get("error") or "not answering"),
                **_seen(row),
            })
            continue
        if not _takes_customer_jobs(row):
            reason, detail = _no_work_reason(row)
            rejections.append({
                "location_id": location_id, "provider_id": provider_id_for(location_id),
                "label": row.get("label") or location_id, "machine": None,
                "reason": reason, "detail": detail,
            })
            continue
        # Reachable and taking work, but absent from the model's enabled inventory: the Hub does
        # not publish the model at all. That is `model_not_published`, not "not installed".
        rejections.append({
            "location_id": location_id, "provider_id": provider_id_for(location_id),
            "label": row.get("label") or location_id, "machine": None,
            "reason": "model_not_published",
            "detail": "this location does not publish the model",
        })
    busy_only = bool(rejections) and all(r.get("reason") in BUSY_REASONS for r in rejections)
    return {
        "model_id": model_id,
        "modality": modality,
        "candidates": candidates,
        "rejections": rejections,
        "available": bool(candidates),
        "busy_only": busy_only,
        "checked_at": time.time(),
    }


def _seen(row: dict) -> dict:
    """`last_seen_at` for a rejection row, only when the fleet actually stated one."""
    seen = _last_machine_seen(row)
    return {"last_seen_at": seen} if seen is not None else {}


def dispatch_reason_summary(eligibility: dict) -> str:
    """One sentence for a job's failure: the reasons, with the age of an offline location."""
    rejections = eligibility.get("rejections") or []
    if not rejections:
        return "no location publishes this model"
    parts: list[str] = []
    for item in rejections[:6]:
        label = item.get("label") or item.get("location_id") or "a location"
        reason = item.get("reason") or "unknown"
        if reason == "offline" and item.get("last_seen_at"):
            age = max(0.0, time.time() - float(item["last_seen_at"]))
            days = int(age // 86400)
            hours = int((age % 86400) // 3600)
            when = f"{days}d" if days else f"{hours}h" if hours else f"{int(age // 60)}m"
            parts.append(f"{label}: offline, last seen {when}")
        else:
            parts.append(f"{label}: {reason}" + (f" ({item['detail']})" if item.get("detail") else ""))
    more = f" (+{len(rejections) - 6} more)" if len(rejections) > 6 else ""
    return "; ".join(parts) + more


#: **Prefer fast Macs for Qwen** (owner decision 2026-10-09). 20-step medians on 10-08: This Mac
#: (M2 Max) 228 s, M4 600-610 s, M2 16 GB 812 s, M1 16 GB 949 s. A slow image model is routed by
#: when a new job would *finish*, not by queue pressure: a fast Mac first, a slow one only when
#: waiting for a fast one would finish later. Every other model keeps the pressure rank.
def speed_ranked(model_id: str) -> bool:
    return "qwen-image" in str(model_id).lower()


#: The fallback when no Mac of a class has history for the model: those 10-08 medians by chip family.
_CHIP_SECONDS = {"M2 Max": 228.0, "M4": 605.0, "M2": 812.0, "M1": 949.0}
#: A Mac with neither history nor a known chip is assumed as slow as the slowest measured.
_UNMEASURED_S = 949.0
#: Half a job: how long, on average, the job a busy Mac is running still has to go.
_RUNNING_REMAINDER = 0.5
_SPEED_TTL_S = 60.0
_speed_cache: dict[str, tuple[float, list]] = {}


def _speed_samples(model_id: str) -> list:
    """`(provider_id, machine, seconds)` of this model's recent successes, read at most once a minute."""
    now = time.monotonic()
    cached = _speed_cache.get(model_id)
    if cached is not None and now - cached[0] < _SPEED_TTL_S:
        return cached[1]
    try:
        from .runtime import store
        samples = store.speed_samples(model_id)
    except Exception:  # noqa: BLE001 - no history is "unmeasured", never a failed route
        samples = []
    _speed_cache[model_id] = (now, samples)
    return samples


#: Unified-memory sizes Apple ships; a Mac reports its total in GB or GiB (16 GiB = 17.18 GB).
_MEMORY_CLASSES = (8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 512)

#: RAM tiers a pool member may be assigned to. A Mac's tier is the shipped memory
#: size it reports (see `mac_class`), rounded down to one of these. Tier boundaries
#: live here and nowhere else; routing and the Pools page both read this.
RAM_TIERS = (8, 16, 24, 32, 48, 64)


def machine_tier(memory_gb: object) -> int | None:
    """The RAM tier (in GB) a Mac belongs to, from its published `memory_gb`.

    Returns None when the Hub did not state a usable memory size. Never inferred
    from names or labels.
    """
    try:
        gb = float(memory_gb)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (gb > 0):
        return None
    candidates = [t for t in RAM_TIERS if t <= gb + 0.5]
    return max(candidates) if candidates else None


def mac_class(chip: object, memory_gb: object) -> str:
    """"M4 · 16 GB": the chip family without "Apple", and the shipped memory size."""
    family = str(chip or "").replace("Apple", "").strip() or "unknown chip"
    try:
        gb = float(memory_gb)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"{family} · ? GB"
    size = max((c for c in _MEMORY_CLASSES if c <= gb + 0.5), default=round(gb))
    return f"{family} · {size} GB"


_seconds_memo: dict[str, tuple[object, list, Callable[[str, str], float]]] = {}


def _mac_seconds(rows: dict, model_id: str, locations: object = None) -> Callable[[str, str], float]:
    """`(provider_id, machine) -> expected seconds`: its own median, else its class's, else its chip's.

    Memoised per model while the snapshot (`locations`) and the sample read stay the same objects:
    a queue of waiting Qwen jobs re-ranks every dispatch pass.
    """
    import statistics

    samples = _speed_samples(model_id)
    memo = _seconds_memo.get(model_id)
    if memo is not None and locations is not None and memo[0] is locations and memo[1] is samples:
        return memo[2]
    classes = {(str(row.get("provider_id") or ""), str(mac.get("machine") or "")):
               mac_class(mac.get("chip"), mac.get("memory_gb"))
               for row in rows.values() for mac in row.get("machines") or [] if isinstance(mac, dict)}
    own: dict[tuple[str, str], list[float]] = {}
    by_class: dict[str, list[float]] = {}
    for provider_id, machine, seconds in samples:
        own.setdefault((provider_id, machine), []).append(seconds)
        known = classes.get((provider_id, machine), "")
        if known and "unknown" not in known and "?" not in known:
            by_class.setdefault(known, []).append(seconds)
    own_median = {key: statistics.median(values) for key, values in own.items()}
    class_median = {key: statistics.median(values) for key, values in by_class.items()}

    def seconds(provider_id: str, machine: str) -> float:
        if (provider_id, machine) in own_median:
            return own_median[(provider_id, machine)]
        known = classes.get((provider_id, machine), "")
        if known in class_median:
            return class_median[known]
        return _CHIP_SECONDS.get(known.split(" · ")[0], _UNMEASURED_S)
    if locations is not None:
        _seconds_memo[model_id] = (locations, samples, seconds)
    return seconds


def _site_speed(row: dict, model_id: str, modality: str, seconds: Callable[[str, str], float],
                available: int | None) -> dict:
    """`fast_s` (the fastest fitting Mac here) and `idle_s` (the idle ones' times, fastest first).

    The Hub, not the Gateway, picks the Mac inside a site, so this is the best the site can do.
    """
    provider_id = str(row.get("provider_id") or "")
    eligible = _eligible_macs(row, model_id, modality)
    fitting = [m for m, model in eligible.items() if model.get("available") is True] or list(eligible)
    busy = {str(w.get("machine") or "") for w in row.get("workers") or []
            if isinstance(w, dict) and w.get("busy") is True}
    times = sorted(seconds(provider_id, m) for m in fitting)
    idle = sorted(seconds(provider_id, m) for m in fitting if m not in busy)
    if available is not None:
        idle = idle[:max(available, 0)]
    return {"fast_s": times[0] if times else _UNMEASURED_S, "idle_s": idle}


def expected_finish_s(candidate: dict, ahead: int = 0) -> float:
    """Seconds until a new job sent here would finish, `ahead` more of ours waiting before it.

    An idle fitting Mac: its own time. None (all busy, or our lane here full): the fastest Mac's
    time after the backlog per fitting Mac and the running job's remainder.
    ponytail: the site's whole queue stands in for this model's; split it if mixed queues mislead.
    """
    idle = [] if candidate.get("room", 1) <= 0 else list(candidate.get("idle_s") or [])
    if ahead < len(idle):
        return idle[ahead]
    backlog = max(int(candidate["queue"]) + ahead - len(idle), 0)
    return candidate["fast_s"] * (1 + _RUNNING_REMAINDER + backlog / candidate["slots"])


def rank_candidate(candidate: dict, routing_key: str, ahead: int = 0) -> tuple:
    """The order `route` picks by, lowest first -- and what a late rebind re-ranks with.

    A free fitting slot first, then queue pressure per **fitting** machine, the best preference
    among fitting Macs, the weighted lottery, and the id. Thus a large busy site cannot beat a
    smaller idle site merely because its queue-to-slot ratio is lower: idle capacity is lent before
    preference is considered. `slots` is the model's own eligible machine count, so a site's small
    Macs no longer dilute a heavy model's share. The lottery is weighted by the fitting Macs at the
    site's best rank for this function; old Hubs use every fitting slot and keep today's behavior.
    """
    full = candidate.get("room", 1) <= 0  # only set by `apply_load`: a full lane ranks last
    pressure = candidate["queue"] / candidate["slots"]
    no_free = (candidate["available"] or 0) <= 0
    if "fast_s" in candidate:
        # A speed-ranked model: the expected finish decides, waiting for a full lane included.
        full, no_free, pressure = False, False, expected_finish_s(candidate, ahead)
    preference = float(candidate.get("preference", _NEUTRAL_PRIORITY))
    lottery = _weighted_tiebreak(
        routing_key, candidate["location_id"],
        int(candidate.get("preference_slots") or candidate["slots"]),
    )
    return (full, no_free, pressure, preference, lottery, candidate["location_id"])


def apply_load(candidates: list[dict], load: Callable[[str], tuple[int, int, int]]) -> list[dict]:
    """Candidates as this Gateway's own books see them now, for a re-route or late rebind.

    The snapshot is up to one refresh old, so every move in a burst read the same free slots and
    queue: overnight 10-08 This Mac (TTS limit 1) held 39 re-routed TTS jobs. `load(provider_id)`
    is the worker's `(room, ours, just_sent)` for this job's lane at that site: `room` is the lane
    budget left (0 ranks it last, see `rank_candidate`), `ours` our jobs running there (a floor
    under the snapshot's queue), `just_sent` jobs we sent there since about the snapshot (taken off
    its free count). Priorities, preferences and the lottery are unchanged.
    """
    out = []
    for candidate in candidates:
        room, ours, just_sent = load(str(candidate["provider_id"]))
        available = candidate.get("available")
        out.append(dict(candidate, room=room, queue=max(int(candidate["queue"]), ours),
                        available=None if available is None else available - just_sent,
                        **({"idle_s": candidate["idle_s"][just_sent:]} if "idle_s" in candidate else {})))
    return out


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
    load: Callable[[str], tuple[int, int, int]] | None = None,
    require_capability: str | None = None, image_params: dict | None = None,
) -> tuple[ProviderDefinition | None, str]:
    """Choose the location that should run this model. Never raises.

    Returns the location's provider definition and a human note naming the choice,
    or `(None, reason)` when nothing can take it. `exclude_locations` is how the
    worker asks for the **next** site after one refused the model at submit. `load` (a re-route)
    ranks by the worker's live books (`apply_load`); a full lane is still returned, last, so the
    caller can tell "every lane is full" from "nowhere". `require_capability` drops every location
    whose worker does not advertise the token — see `routing_candidates`.
    """
    candidates = routing_candidates(
        model_id, modality, only_location=only_location, exclude_locations=exclude_locations,
        require_capability=require_capability, image_params=image_params,
    )
    if load is not None and candidates:
        candidates = apply_load(candidates, load)
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


def pool_member_model(member: str) -> str:
    """The Hub's own model id behind an image-pool member (`studio-hub::X` or `X` -> `X`)."""
    member = str(member or "").strip()
    return member.split("::", 1)[1] if member.startswith(f"{FLEET_PROVIDER_ID}::") else member


def assigned_macs(row: dict, member: str, modality: str = "image") -> set[str]:
    """The Macs at this site assigned this pool member AND able to run it now.

    The assignment is the Hub's `assigned_image_model` per Mac, set from the Gateway (Auto by RAM, or
    the owner's pick: `image_assignment`). The Hub enforces it too, for work sent `routing: assigned`.
    """
    model = pool_member_model(member)
    assigned = {str(m.get("machine") or "") for m in row.get("machines") or []
                if isinstance(m, dict) and m.get("machine") and m.get("assigned_image_model") == model}
    return assigned & set(_eligible_macs(row, model, modality))


def pool_candidates(
    members: list[str], modality: str, *, only_location: str | None = None,
    exclude_locations: set[str] | None = None, require_capability: str | None = None, image_params: dict | None = None,
) -> list[dict]:
    """Every (location, member) that can take an image-pool job now, each tagged `_member`.

    A location qualifies for a member only when one of its Macs is assigned that member and can run
    it; a member nobody is assigned to sits out, so no job is sent where the Hub would refuse it.
    `require_capability` asks the same question of each member's row, so a reference job routed
    through a pool skips a site whose worker cannot run it either.
    """
    rows = {str(r.get("id")): r for r in snapshot(allow_stale=True).get("locations") or []}
    found: list[dict] = []
    for member in dict.fromkeys(pool_member_model(m) for m in members or [] if str(m).strip()):
        # `respect_model_supply=False`: a pool is exempt from the per-model statement. Its own
        # `assigned_macs` below is the authority — a Mac assigned this member and able to run it
        # is what qualifies a site — and a site's `eligible 0` for one member is not a refusal of
        # the pool, whose other members it may still run.
        for candidate in routing_candidates(member, modality, only_location=only_location,
                                            exclude_locations=exclude_locations,
                                            require_capability=require_capability, image_params=image_params,
                                            respect_model_supply=False):
            assigned = assigned_macs(rows.get(str(candidate.get("location_id"))) or {}, member, modality)
            if "image_limit_machines" in candidate:
                assigned &= set(candidate["image_limit_machines"])
            if assigned:
                slots = min(candidate["slots"], len(assigned)) if "image_limit_machines" in candidate else candidate["slots"]
                found.append({**candidate, "slots": slots, "_member": member})
    return found


def route_pool(
    members: list[str],
    modality: str,
    *,
    routing_key: str = "",
    only_location: str | None = None,
    exclude_locations: set[str] | None = None,
    load: Callable[[str], tuple[int, int, int]] | None = None,
    require_capability: str | None = None, image_params: dict | None = None,
) -> tuple[Any, str | None, str | None]:
    """Route an image-pool job to the best location with a free Mac assigned one of its members.

    Returns `(definition, member model id, note)`, or `(None, None, reason)`.
    """
    from .providers import registry

    candidates = pool_candidates(members, modality, only_location=only_location,
                                 exclude_locations=exclude_locations,
                                 require_capability=require_capability, image_params=image_params)
    if load is not None and candidates:
        candidates = apply_load(candidates, load)
    if not candidates:
        if only_location is not None:
            return None, None, f"the location that owns this voice ({only_location}) has no Mac assigned a pool model"
        if exclude_locations:
            return None, None, "every location with a Mac assigned a pool model refused it"
        return None, None, "no Mac is assigned a pool model it can run right now"
    chosen = min(candidates, key=lambda c: rank_candidate(c, routing_key or "pool"))
    for definition, model in registry.models.enabled_models(modality):
        if definition.id == chosen["provider_id"] and model["model_id"] == chosen["_member"]:
            note = (f"{chosen['label']}: {chosen['queue']} queued / {chosen['slots']} eligible"
                    f" of {len(candidates)} candidate{'s' if len(candidates) != 1 else ''}")
            return definition, chosen["_member"], note
    return None, None, "resolved location has no enabled member"


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


#: provider id -> the model report (`_model_report`) its last successful discovery saw. In memory
#: on purpose: empty when the gateway starts, so every location is re-discovered once per start --
#: after an upgrade the cached rows were built by the previous build's discovery code. Measured
#: 2026-10-04: 0.2.119's site-memory fix stayed invisible at 1100/1300/1400 for 10+ minutes after
#: install, until each location was refreshed by hand.
_model_reports: dict[str, str] = {}

#: The per-Mac facts discovery's verdicts are built from (installed, fits, runtime, available, the
#: controller's package state). Not `loaded`/`fit`/`active_download`, which change by the minute.
_REPORT_KEYS = ("model", "machine", "cache_state", "memory_eligible", "runtime_ready", "available",
                "dispatch_state")


def _model_report(row: dict) -> str:
    """A location's per-Mac model facts as one comparable string, from the fleet read."""
    return json.dumps(sorted(
        [str(m.get(key)) for key in _REPORT_KEYS]
        for m in row.get("local_models") or [] if isinstance(m, dict)
    ))


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
        # Also when what the Macs report changed (a model installed on a node, a Mac joined or
        # left, a memory verdict flipped) and once per gateway start: none of those changes the
        # Hub's version, and the catalogue's per-site verdicts are built from exactly them.
        report = _model_report(row)
        try:
            recorded = str(registry.models.cached(provider_id).get("hub_version") or "").strip()
            if recorded == version and _model_reports.get(provider_id) == report:
                continue
            result = registry.refresh(provider_id, hub_version=version)
        except Exception:  # noqa: BLE001 - one bad location is not a failed pass
            continue
        if result.error is None:
            refreshed[provider_id] = version
            _model_reports[provider_id] = report
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
                # A controller added after a release, or offline during it, is sent it here.
                # Each Mac's image-pool model (Auto by RAM, or the owner's pin) is sent where its Hub
                # reports a different one: a new Mac, one that was offline, or a mapping just changed.
                try:
                    from . import image_assignment

                    image_assignment.sync()
                except Exception:  # noqa: BLE001 - one unreachable Mac must not kill the poller
                    pass
                # A Lite Mode change saved while its Mac was offline is sent once the Mac is back.
                try:
                    from . import startup_control

                    startup_control.apply_pending_lite_mode(value.get("machines") or [])
                except Exception:  # noqa: BLE001 - a waiting change must not kill the poller
                    pass
            _refresher_stop.wait(interval)

    _refresher = threading.Thread(target=loop, name="fleet-refresh", daemon=True)
    _refresher.start()
