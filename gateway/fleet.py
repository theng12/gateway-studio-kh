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
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

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


def _write(rows: list[dict], *, local_name: str | None = None) -> None:
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
    fd = os.open(str(FLEET_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
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
    return candidate.rstrip("/")


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
    rows = _read()
    existing = None
    # Best-effort: a probe failure is a normal state (the controller is switched
    # off, or the token was not pasted), never an error for the add itself.
    stable_id = ""
    if clean_token:
        try:
            probed = _probe(url, clean_token)
        except Exception:  # noqa: BLE001 - the address is still a usable answer
            probed = {}
        stable_id = str(probed.get("site_id") or probed.get("controller_id") or "")
    if stable_id:
        existing = next(
            (row for row in rows if str(row.get("site_id") or "") == stable_id), None
        )
    if existing is None:
        existing = next((row for row in rows if row.get("url") == url), None)
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
    return _masked(existing)


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


def _is_probe_machine(machine: dict) -> bool:
    """Whether a machine the Hub lists is one of its own connectivity probes.

    Named `probe-<something>` and labelled "probe" by the Hub. Matched on the id
    rather than the label: a label is the owner's to change, and this must not
    start hiding a real machine because someone called it "probe".
    """
    return str(machine.get("machine") or "").startswith("probe-")


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
        # The site's own admission numbers, which are what routing needs. Kept
        # because it is already fetched: dropping data this module has paid for is
        # how the routing layer ended up with nothing to route on.
        "capacity": {},
        "latency_ms": None,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            overview = _fetch(client, base, token, "/api/hub/fleet/overview")
            try:
                # The node roster is the *second* thing to go wrong on a Hub that
                # is up: measured live, a Hub mid-rebuild answered
                # `fleet/overview`, `capabilities`, `stats` and `settings` with 200
                # while `fleet/nodes` alone returned 500. Requiring it made that
                # location read as entirely offline — "HTTP 500 from the
                # controller" — which is wrong and, worse, hides six working
                # machines behind one broken route.
                nodes = _fetch(client, base, token, "/api/hub/fleet/nodes")
            except httpx.HTTPError:
                nodes = {}
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

    location.update({
        "reachable": True,
        # Prefer the Hub's own identity; fall back to the older sources only when
        # it is unavailable, so a Hub that does not serve capabilities still works.
        "role": controller.get("role") or nodes.get("role"),
        "version": controller.get("studiohub_version") or _version_from(overview),
        "site_id": capabilities.get("site_id") or controller.get("site_id"),
        "controller_id": controller.get("controller_id"),
        "ready": controller.get("ready"),
        "machines": real_machines,
        "machine_summary": summary,
        "probe_machines_hidden": hidden,
        "nodes": node_rows,
        "node_summary": nodes.get("summary") or {},
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
            # Read from the controller id, which is what the provider id is derived
            # from — not from the label, which the owner may rename at any time.
            "provider_id": provider_id_for(str(row.get("id") or "")),
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

    return {
        "locations": len(rows),
        "locations_up": sum(1 for row in rows if row["reachable"]),
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
    now = time.time()
    with _cache_lock:
        cached = _cache["value"]
        if not force and cached and (allow_stale or (now - _cache["at"]) < _CACHE_TTL):
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


# --------------------------------------------------------------------------
# Routing: which location runs a job
#
# The global scheduler belongs here and the site-local one belongs to Studio Hub.
# The Hub says so itself: `/api/hub/capabilities` publishes
# `authority: {global: "genstudio", site_local_scheduler: "sqlite",
# global_job_claiming: false}`. So the gateway chooses a *site* and never a
# machine -- which machine has the memory for the model is machine-specific and
# is the controller's decision, not this module's.


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def usable_location_ids() -> set[str]:
    """Locations answering right now, by id.

    Read once per catalogue rebuild rather than per row: this is the difference
    between one fleet read and one per model.
    """
    try:
        return {
            str(row.get("id"))
            for row in snapshot(allow_stale=True)["locations"]
            if row.get("reachable")
        }
    except Exception:  # noqa: BLE001 - a routing hint must never break the catalogue
        return set()


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
    model_id: str, modality: str, *, only_location: str | None = None
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
        row = rows.get(location_id) or {}
        if not row.get("reachable"):
            continue

        capacity = row.get("capacity") or {}
        slots = _as_int(capacity.get("eligible_physical_machine_slots_total"))
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
            "queue": max(queue, 0),
        })
    return candidates


def route(
    model_id: str,
    modality: str,
    *,
    routing_key: str = "",
    only_location: str | None = None,
) -> tuple[ProviderDefinition | None, str]:
    """Choose the location that should run this model. Never raises.

    Returns the location's provider definition and a human note naming the choice,
    or `(None, reason)` when nothing can take it.
    """
    candidates = routing_candidates(model_id, modality, only_location=only_location)
    if not candidates:
        if only_location is not None:
            return None, f"the location that owns this voice ({only_location}) cannot run {model_id}"
        return None, f"no location can run {model_id} right now"

    def rank(candidate: dict) -> tuple:
        # Pressure first -- queue per eligible machine -- then the weighted
        # lottery, then the id so the result is deterministic.
        return (
            candidate["queue"] / candidate["slots"],
            _weighted_tiebreak(
                routing_key or model_id, candidate["location_id"], candidate["slots"]
            ),
            candidate["location_id"],
        )

    chosen = min(candidates, key=rank)
    note = (
        f"{chosen['label']}: {chosen['queue']} queued / {chosen['slots']} eligible"
        f" of {len(candidates)} location{'s' if len(candidates) != 1 else ''}"
    )
    return chosen["definition"], note


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
