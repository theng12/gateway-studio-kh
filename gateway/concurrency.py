"""How many jobs may be in flight at once — globally, per lane, and per provider.

**Why this exists.** The gateway ran exactly one job at a time. That was correct
while it served one owner on one Mac (`store.py` says so plainly: leases, permits
and admission capacity were dropped because "multiple replicas and a fleet of
sites" did not apply). With a fleet of Macs and a wall of cloud keys, it is the
thing that makes 19 machines idle: one slow image render held the only slot, and
every chat, TTS and subtitle job queued behind it.

**Three ceilings, checked before a job is admitted.** They are different
questions and all three have to pass:

  * **global** — how many jobs this gateway will run at once, full stop.
  * **lane** — how much of that one modality may take. A lane cap is a fairness
    knob, not a correctness one: without it, four long renders can hold every
    slot while a chat job that arrived first waits. It defaults to the whole
    pool, because a cap that is too tight wastes the fleet and FIFO already
    stops true starvation.
  * **provider** — the only ceiling the *provider* has an opinion about, and the
    reason this is not simply "run N at once". Over-scheduling is what makes a
    cloud lane answer 429, and a 429 mid-flight is a job that has to be held
    rather than retried blindly.

**One file, and it is the owner's.** `provider-concurrency.local.json` under the
data directory holds all three, the same `*.local.json` convention as
`model-limits.local.json`: user-owned, never shipped, survives a reinstall. It is
hand-editable *and* writable from the Providers page in the console, because a
number only the owner can supply (a cloud tier, a bigger fleet) should not need a
terminal:

    {
      "global": 8,
      "lanes": {"image": 2},
      "providers": {"openai-direct": {"limit": 4, "note": "tier 4"}}
    }

**Where each number comes from, best first — and the answer says which.** This is
the same triad the catalogue already uses for token limits, and for the same
reason: *"I could not work out the answer"* and *"there is no limit"* must never
look the same.

  1. `hub` — a Studio Hub location states its own free slots on
     `capacity.by_operation`. It is the only source that is a live measurement
     rather than a belief, and it is what lets a five-Mac site take five jobs.
  2. `curated` — the owner's file above.
  3. `gateway` — a labelled default of 1 for a provider, naming the file to edit.
     Deliberately the safe direction, and deliberately visible: a lane running at
     the default is under-used, and the source field is how a reader knows.

**Two sources must never disagree in silence.** The file is the explicit,
UI-editable setting, so it wins; `GATEWAY_JOB_CONCURRENCY` and
`GATEWAY_LANE_CONCURRENCY` remain as scripted-setup fallbacks, and every answer's
`detail` names which of the two answered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR, write_secret

#: The owner's concurrency settings — all three ceilings in one file. Same
#: `*.local.json` convention as `model-limits.local.json`.
SETTINGS_PATH = Path(
    os.environ.get("GATEWAY_PROVIDER_CONCURRENCY")
    or (DATA_DIR / "provider-concurrency.local.json")
)
LEGACY_PATH = Path(__file__).resolve().parent.parent / "data" / "provider-concurrency.local.json"

#: Which of the three sources answered.
SOURCE_HUB = "hub"
SOURCE_CURATED = "curated"
SOURCE_GATEWAY = "gateway"

#: The pool's size when nothing says otherwise. Four because it is what Story Studio
#: had already been told (`paid_concurrency_limit: 4`) while the gateway ran one job
#: at a time — so this is the number that finally makes the published figure true
#: rather than a new figure to justify.
DEFAULT_GLOBAL = 4

#: `ponytail:` a flat eight deferrals with a backoff capped at a minute - about four minutes in
#: total, and nine dispatch attempts. **Now editable** (`queue_wait_budget`); these are only the
#: defaults a fresh profile starts from. The ceiling they encode is unchanged: a fleet busy for
#: longer than the configured window still fails work it would eventually have run.
DEFAULT_MAX_DEFERRALS = 8
DEFAULT_DEFER_CAP_S = 60.0

#: What a cloud provider gets when neither its own answer nor the owner's file says
#: anything. One, because over-scheduling is what produces 429s, and a 429 on a
#: submitted job is a durable unknown-outcome hold rather than a clean retry.
DEFAULT_PROVIDER = 1


@dataclass(frozen=True)
class Budget:
    """One ceiling, and where the number came from."""

    limit: int
    source: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"limit": self.limit, "source": self.source, "detail": self.detail}


def _positive_int(value: Any) -> int | None:
    """A stated limit, or `None`. Booleans are not numbers here.

    **Strings are accepted**, because every environment variable and every JSON
    number the owner types is one. Rejecting them was a real bug caught by the
    tests: `GATEWAY_JOB_CONCURRENCY=7` and `GATEWAY_LANE_CONCURRENCY=image=1` were
    both parsed as "nothing stated" and silently fell back to the default, which
    looks exactly like the setting working until you count what is running.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    # A fraction is not a number of jobs. Truncating `0.5` to `0` would write a
    # limit that means "nobody said" — the one outcome this module exists to keep
    # distinguishable from a real answer.
    if float(value) != int(value):
        return None
    if value <= 0:
        return None
    return int(value)


# -- the settings file ------------------------------------------------------
_settings_cache: dict[str, Any] = {"stamp": None, "value": {}}


def _path() -> Path:
    return SETTINGS_PATH if SETTINGS_PATH.exists() else LEGACY_PATH


def _read_file() -> dict[str, Any]:
    """The settings file, re-read when it changes.

    Cached on `(path, mtime, size)` like `.env` is: this sits on the admission
    path, and a fix to a rate limit should take effect without a restart while a
    stat per admitted job stays cheap.
    """
    path = _path()
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if stamp == _settings_cache["stamp"]:
        return dict(_settings_cache["value"])

    value: dict[str, Any] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        value = parsed
    _settings_cache["stamp"] = stamp
    _settings_cache["value"] = value
    return dict(value)


def reset_for_tests() -> None:
    _settings_cache["stamp"] = None
    _settings_cache["value"] = {}


def _settings_lanes() -> dict[str, int]:
    raw = _read_file().get("lanes")
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for name, value in raw.items():
        limit = _positive_int(value)
        if limit is not None:
            limits[str(name).strip().lower()] = limit
    return limits


def _settings_providers() -> dict[str, dict[str, Any]]:
    raw = _read_file().get("providers")
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for provider_id, value in raw.items():
        if not isinstance(value, dict):
            continue
        limit = _positive_int(value.get("limit"))
        if limit is None:
            continue
        entries[str(provider_id)] = {
            "limit": limit,
            "note": str(value.get("note") or "").strip()[:200],
        }
    return entries


def _env_lanes() -> dict[str, int]:
    """`GATEWAY_LANE_CONCURRENCY=image=2,chat=3`, the scripted-setup fallback."""
    raw = os.environ.get("GATEWAY_LANE_CONCURRENCY") or ""
    limits: dict[str, int] = {}
    for part in raw.split(","):
        name, _, value = part.partition("=")
        name = name.strip().lower()
        limit = _positive_int(value.strip())
        if name and limit is not None:
            limits[name] = limit
    return limits


# -- the three ceilings -----------------------------------------------------
def global_budget() -> Budget:
    """How many jobs this gateway runs at once, and which source said so."""
    stated = _positive_int(_read_file().get("global"))
    if stated is not None:
        return Budget(limit=stated, source=SOURCE_CURATED,
                      detail=f"{SETTINGS_PATH.name} sets global to {stated}")
    env = _positive_int(os.environ.get("GATEWAY_JOB_CONCURRENCY"))
    if env is not None:
        return Budget(limit=env, source=SOURCE_CURATED,
                      detail=f"GATEWAY_JOB_CONCURRENCY is set to {env}")
    return Budget(limit=DEFAULT_GLOBAL, source=SOURCE_GATEWAY,
                  detail=f"nothing set it, so {DEFAULT_GLOBAL}; edit it on Providers")


def global_limit() -> int:
    """The pool size. Kept as a function of its own because callers are many."""
    return global_budget().limit


def queue_wait_budget() -> dict[str, Any]:
    """How long a job waits for a busy fleet, and where each number came from.

    **These were two constants with a `ponytail:` note naming this exact upgrade path** — *"make
    both numbers settings beside the pool size, which is where an owner would look"* — and the note
    was right about the ceiling: eight deferrals with a backoff capped at a minute is about four
    minutes and nine dispatch attempts, so a fleet busy for longer than that fails work it would
    eventually have run. On a fleet of ~50 Macs that is the difference between "it queued" and "it
    reported an error", which is the owner's own requirement.

    Read live from the same file as the pool size (`global`), so a change lands without a restart:

        {"global": 60, "queue": {"max_deferrals": 24, "defer_cap_seconds": 120}}
    """
    wording = _read_file().get("queue")
    entry = wording if isinstance(wording, dict) else {}
    attempts = _positive_int(entry.get("max_deferrals"))
    cap = entry.get("defer_cap_seconds")
    cap_seconds = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else None
    return {
        "max_deferrals": attempts if attempts is not None else DEFAULT_MAX_DEFERRALS,
        "defer_cap_seconds": cap_seconds if cap_seconds is not None else DEFAULT_DEFER_CAP_S,
        "source": SOURCE_CURATED if (attempts is not None or cap_seconds is not None) else SOURCE_GATEWAY,
        "path": str(SETTINGS_PATH),
    }


def lane_budget(modality: str) -> Budget:
    """How much of the pool one modality may hold."""
    name = str(modality or "").strip().lower()
    stated = _settings_lanes().get(name)
    if stated is not None:
        return Budget(limit=stated, source=SOURCE_CURATED,
                      detail=f"{SETTINGS_PATH.name} caps {name} at {stated}")
    env = _env_lanes().get(name)
    if env is not None:
        return Budget(limit=env, source=SOURCE_CURATED,
                      detail=f"GATEWAY_LANE_CONCURRENCY caps {name} at {env}")
    return Budget(limit=global_limit(), source=SOURCE_GATEWAY,
                  detail="no lane cap set, so this lane may use the whole pool")


def cloud_budget(provider_id: str) -> Budget:
    """A cloud provider's concurrency: curated if the owner said, else the default.

    The `detail` on the default names where to set it, because a lane running at 1
    is under-used and the reader should not have to go looking for why.
    """
    entry = _settings_providers().get(str(provider_id or ""))
    if entry is not None:
        note = entry.get("note") or f"{SETTINGS_PATH.name}"
        return Budget(limit=entry["limit"], source=SOURCE_CURATED, detail=note)
    return Budget(
        limit=DEFAULT_PROVIDER,
        source=SOURCE_GATEWAY,
        detail=f"no limit set, so one at a time; set it on the Providers page ({provider_id})",
    )


def hub_budget(slots: Any, *, operation: str = "", stated: Any = None) -> Budget:
    """A Studio Hub location's concurrency, from the site's own slots and your cap.

    `slots` is `available_worker_slots` from that site's `capacity.by_operation` — the
    site's own count of free workers for this operation, and the only live measurement in
    this module. `stated` is the operator's number for this location, when they set one.

    **The smaller of the two wins, and they stay distinguishable.** The site's count is what
    it can physically take; the operator's number is policy, and an operator must be able to
    throttle a busy site below what it offers. The converse must not hold: a number typed
    into a form is not licence to send a site more concurrent work than it says it has free
    workers — that is how a queue becomes a crash, and it is why this is a `min` rather than
    a substitution.

    **Before this, the operator's number was not read at all on this path.** A Studio Hub row
    in the console was editable and ignored, so a number typed for a location described
    nothing; the page only ever showed the site's own count. Reading it as a cap is what makes
    that control mean what it looks like it means.
    """
    site = _positive_int(slots)
    cap = _positive_int(stated)
    if site is None and cap is None:
        return Budget(
            limit=DEFAULT_PROVIDER,
            source=SOURCE_GATEWAY,
            detail=(
                "the site did not state free slots"
                + (f" for {operation}" if operation else "")
                + ", so one at a time"
            ),
        )
    if site is None:
        # The operator's number is all there is. Say that, rather than dressing it as the
        # site's answer — a value and its source come from one place or they disagree.
        return Budget(
            limit=cap,
            source=SOURCE_CURATED,
            detail=(
                f"you set {cap}; the site states no free slots"
                + (f" for {operation}" if operation else "")
            ),
        )
    if cap is None:
        return Budget(
            limit=site,
            source=SOURCE_HUB,
            detail=(
                f"the site reports {site} available worker slot"
                f"{'' if site == 1 else 's'}" + (f" for {operation}" if operation else "")
            ),
        )
    limit = min(site, cap)
    tighter = "your limit is tighter" if cap <= site else "the site's count is tighter"
    return Budget(
        limit=limit,
        source=SOURCE_CURATED if cap <= site else SOURCE_HUB,
        detail=f"the site reports {site} free and you set {cap}, so {limit} runs — {tighter}",
    )


def stated_provider_limit(provider_id: str) -> int | None:
    """The operator's own number for this provider, or `None` when they have set none."""
    entry = _settings_providers().get(str(provider_id or ""))
    return entry["limit"] if entry else None


# -- the settings surface ---------------------------------------------------
def _hub_operation_slots(provider_id: str) -> dict[str, int] | None:
    """The free worker slots a Studio Hub location published, per operation.

    A Hub's concurrency is **its own live number**, not a setting: `Worker._provider_budget`
    reads `capacity.by_operation[<operation>].available_worker_slots` and never consults the
    settings file, and there is one budget per operation (`image.text_to_image`,
    `voice.tts`, …) rather than one per provider. That is why the console needs this: it
    drew every provider from `cloud_budget`, so a location running four image jobs at once
    was shown as "no limit set, so one at a time; set it on the Providers page" — a number
    that did not describe the system, and advice that does nothing, because setting a limit
    there is not read on this path.

    Imported lazily and swallowed on failure on purpose: `fleet` imports this module, so the
    import has to happen inside the call, and an unreadable fleet must not stop the console
    from drawing the rest of the page.
    """
    try:
        from . import fleet

        for row in fleet.snapshot(allow_stale=True).get("locations") or []:
            if str(row.get("provider_id") or "") != provider_id:
                continue
            by_operation = (row.get("capacity") or {}).get("by_operation") or {}
            slots: dict[str, int] = {}
            for operation, entry in by_operation.items():
                if not isinstance(entry, dict):
                    continue
                stated = _positive_int(entry.get("available_worker_slots"))
                if stated is not None:
                    slots[str(operation)] = stated
            return slots or None
    except Exception:  # noqa: BLE001 - the console must draw even with no fleet
        return None
    return None


def _hub_provider_budget(provider_id: str) -> Budget | None:
    """What a Studio Hub location will actually run, in the console's words.

    The page draws ONE row per provider while the Hub publishes one budget per operation,
    so this reports the largest and names the spread rather than implying the number is
    uniform — a site whose image lane takes four and whose voice lane takes one is not
    described by either number alone. Returns `None` for anything that is not a Hub
    location, so cloud rows keep the settings-file answer they are governed by.

    **The operator's number is a cap here too**, so the page shows what will run rather than
    what the site offered: a location reporting eight free slots with a limit of three drawn
    as eight would be the same defect this row was fixed for, one layer along.
    """
    slots = _hub_operation_slots(provider_id)
    if not slots:
        return None
    site_slots = dict(slots)
    cap = stated_provider_limit(provider_id)
    if cap is not None:
        slots = {op: min(n, cap) for op, n in slots.items()}
    widest = max(slots, key=lambda op: slots[op])
    limit = slots[widest]
    capped = ""
    if cap is not None:
        # Two different situations, two different sentences. The first version said "raise it to
        # use the rest" in both, which is advice that does nothing when the site's own count is
        # already the smaller of the two — the same defect as the sentence this row was fixed for.
        if cap < site_slots[widest]:
            capped = (f" You set {cap} for this location, tighter than the site's own count, so "
                      f"{limit} runs. Raise it to use the site's full {site_slots[widest]}.")
        else:
            capped = (f" You set {cap} for this location, and the site's own count is already "
                      f"smaller, so that binds. A number here throttles a location; it never "
                      f"over-schedules one.")
    if len(set(slots.values())) == 1:
        detail = (f"the site reports {limit} available worker slot"
                  f"{'' if limit == 1 else 's'}, the same for every lane it serves."
                  + capped)
    else:
        spread = ", ".join(f"{op} {n}" for op, n in sorted(slots.items()))
        detail = (f"the site reports its own free slots per lane and this gateway follows "
                  f"them, so this is the widest — {spread}." + capped)
    return Budget(limit=limit, source=SOURCE_HUB, detail=detail)


def settings_view(provider_ids: list[str] | None = None,
                  provider_scopes: dict[str, str] | None = None,
                  lane_names: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """Everything the console needs to show and edit the three ceilings.

    `provider_ids` is the providers the caller knows about, so the page can list a
    row for one that has no limit yet — a provider missing from the file is the
    case that most needs a control, and a page that only drew what was already set
    would hide exactly the ones still running at the default.

    `provider_scopes` is `{id: "cloud" | "local"}` from `registry.describe_all()`, passed in
    rather than derived here because the derivation already exists in one place —
    `credentials.py` reads it off the provider's **kind**, `studio_hub` being the only local
    one. A second copy of that rule is free to disagree with the first.

    The scope is on every row because **the two kinds are governed by different rules and the
    page draws them as separate tabs**: a cloud limit is a number the owner chooses, and a
    location's is a cap on what the site reports for itself. One list mixing eighteen vendors
    with six controllers, under one column heading, was the thing the owner asked to have
    taken apart.
    """
    scopes = provider_scopes or {}
    known = {str(pid) for pid in (provider_ids or []) if pid}
    known.update(_settings_providers())
    providers = []
    for provider_id in sorted(known):
        # A Studio Hub location is governed by its own published slots, so reporting the
        # settings-file answer for it would be a number that does not describe the system.
        budget = _hub_provider_budget(provider_id) or cloud_budget(provider_id)
        providers.append({
            "provider_id": provider_id,
            # `None` rather than a guess when the caller did not say: an unknown scope must not
            # silently land in the cloud tab, where its limit would be described by the wrong rule.
            "scope": scopes.get(provider_id),
            **budget.as_dict(),
            # What is written down, as opposed to what is in force — the page needs
            # both to show an unset row as unset rather than as "1".
            "stated": _settings_providers().get(provider_id, {}).get("limit"),
        })
    # **A lane the gateway serves must be drawable even with nothing set on it.** The caller
    # passes the modality vocabulary (`catalog.MODALITIES`) rather than this module keeping a
    # second copy of it: a lane absent from the page is the one most likely to be running at the
    # pool default, which is exactly the row the owner needs to see. Settings keys the caller did
    # not name are still listed, so a cap set by hand survives a vocabulary change.
    names = sorted({str(name).strip().lower() for name in lane_names if str(name).strip()}
                   | set(_settings_lanes()))
    return {
        "path": str(SETTINGS_PATH),
        "global": {**global_budget().as_dict(),
                   "stated": _positive_int(_read_file().get("global"))},
        "lanes": {
            **lane_budget("").as_dict(),
            "values": _settings_lanes(),
            "rows": [{
                "lane": name,
                **lane_budget(name).as_dict(),
                "stated": _settings_lanes().get(name),
            } for name in names],
        },
        "providers": providers,
    }


class SettingsRefused(Exception):
    """A settings write the caller can act on."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Write the ceilings the caller named, leaving everything else alone.

    **A key that is absent is not a key to clear.** The same rule the retention
    settings follow: a page that sends only what it changed must not erase the
    rest, and a provider removed from the file silently returns to the default
    rather than being deleted from a fleet.

    `limit: null` (or 0) *is* how a value is cleared — stated explicitly, never
    inferred from silence.
    """
    if not isinstance(payload, dict):
        raise SettingsRefused("expected a settings object")

    current = dict(_read_file())

    if "global" in payload:
        value = payload["global"]
        if value in (None, "", 0):
            current.pop("global", None)
        else:
            limit = _positive_int(value)
            if limit is None:
                raise SettingsRefused(
                    f"the pool size must be a whole number of jobs, not {value!r}"
                )
            current["global"] = limit

    if "lanes" in payload:
        lanes = payload["lanes"]
        if lanes in (None, {}):
            current.pop("lanes", None)
        elif not isinstance(lanes, dict):
            raise SettingsRefused("lanes must be an object of modality -> limit")
        else:
            merged = dict(current.get("lanes") or {})
            for name, value in lanes.items():
                key = str(name).strip().lower()
                if value in (None, "", 0):
                    merged.pop(key, None)
                    continue
                limit = _positive_int(value)
                if limit is None:
                    raise SettingsRefused(
                        f"the {key} lane limit must be a whole number, not {value!r}"
                    )
                merged[key] = limit
            if merged:
                current["lanes"] = merged
            else:
                current.pop("lanes", None)

    if "providers" in payload:
        providers = payload["providers"]
        if providers in (None, {}):
            current.pop("providers", None)
        elif not isinstance(providers, dict):
            raise SettingsRefused("providers must be an object of provider_id -> limit")
        else:
            merged = dict(current.get("providers") or {})
            for provider_id, value in providers.items():
                key = str(provider_id).strip()
                if value in (None, "", 0):
                    merged.pop(key, None)
                    continue
                if isinstance(value, dict):
                    limit = _positive_int(value.get("limit"))
                    note = str(value.get("note") or "").strip()[:200]
                else:
                    limit = _positive_int(value)
                    note = ""
                if not key or limit is None:
                    raise SettingsRefused(
                        f"the limit for {key or '(unnamed)'} must be a whole number, "
                        f"not {value!r}"
                    )
                entry: dict[str, Any] = {"limit": limit}
                if note:
                    entry["note"] = note
                merged[key] = entry
            if merged:
                current["providers"] = merged
            else:
                current.pop("providers", None)

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_secret(SETTINGS_PATH, json.dumps(current, indent=2) + "\n")
    reset_for_tests()
    return settings_view()
