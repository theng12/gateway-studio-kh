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
#: An automatic fleet lane: the number is the live fleet's own capacity (D3).
SOURCE_FLEET = "fleet"

#: The lanes the Studio Hub fleet serves, and the Hub operation each is published under.
#: **Their caps follow the fleet by default** (owner, 2026-10-04: *"the fleet will grow — 70,
#: 100, 200, 1000 Macs"*): a fixed 50 was set when the fleet had ~50 Macs and was binding at 56.
#: A number the owner types is an optional ceiling; with none, the lane is what the reachable,
#: unpaused groups publish, re-read every pass, and each group's per-model room is the real bound.
FLEET_LANES: dict[str, str] = {
    "tts": "voice.tts",
    "image": "image.text_to_image",
    "music": "music.generation",
    "transcription": "audio.transcription",
}
#: The per-lane cap the live settings file carried for every fleet lane before automatic lanes.
#: It meant "about the fleet's size" when it was typed, so it is read as "not set" (automatic).
#: ponytail: an owner who wants exactly 50 types 49 or 51; add an explicit flag if that matters.
LEGACY_FLEET_LANE_CAP = 50

#: The pool's size when nothing says otherwise. Four because it is what Story Studio
#: had already been told (`paid_concurrency_limit: 4`) while the gateway ran one job
#: at a time — so this is the number that finally makes the published figure true
#: rather than a new figure to justify.
DEFAULT_GLOBAL = 4

#: **How long a job waits for a busy fleet, and the default that had to change.**
#:
#: Eight deferrals with a backoff capped at a minute is about **four minutes** — fine for one Mac
#: and one owner, and wrong for a fleet running two-hour video jobs. The owner's decision C says
#: it in as many words: *"hold, don't fail, for a temporarily absent cluster: work aimed at a
#: cluster that is down waits rather than failing immediately (this replaces the audit's
#: four-minute deferral budget)"*. Measured live before this: a job waiting on a busy site was
#: **failed** after ~4 minutes, and the settings file did not override it, so the shipped default
#: was the ceiling in practice (audit M4).
#:
#: 240 deferrals with a five-minute cap is about **twenty hours** in the worst case — longer than
#: any render this fleet runs, and still bounded, because a queue that can wait for ever is a
#: queue nobody can reason about. The **retry policy is what makes this safe**: a job that keeps
#: failing on real machines is declared failed after three attempts (A7) rather than waiting here.
#: So this bounds "nothing can take it yet", and A7 bounds "everything tried it and failed".
#:
#: Editable without a restart (`queue_wait_budget`); these are the defaults a fresh profile and
#: the live file both start from.
DEFAULT_MAX_DEFERRALS = 240
DEFAULT_DEFER_CAP_S = 300.0

#: **How long a job may sit queued on a Hub location before the Gateway withdraws it** — the
#: owner's three-hour ceiling, now the *only* copy of it.
#:
#: It lived as a module constant in the Studio Hub adapter (`QUEUE_WAIT_CEILING_S`), while the
#: deferral budget beside it was editable here — so the owner could edit one number and the other
#: silently overruled it. It is the one that ends a job: `worker._expired_in_queue` fails it
#: `QUEUE_TIMEOUT`, and `studio_hub._poll_batch` withdraws the batch from the Hub's queue. 146
#: `QUEUE_TIMEOUT` in the seven days to 2026-10-19 came through that constant.
#:
#: The default keeps the shipped behaviour until the owner edits `queue.wait_ceiling_seconds`:
#:
#:     {"global": 60, "queue": {"wait_ceiling_seconds": 10800}}
DEFAULT_QUEUE_WAIT_CEILING_S = 3 * 3600.0

#: What a cloud provider gets when neither its own answer nor the owner's file says
#: anything. One, because over-scheduling is what produces 429s, and a 429 on a
#: submitted job is a durable unknown-outcome hold rather than a clean retry.
DEFAULT_PROVIDER = 1

#: How many **machines** one job may be tried on before it is declared failed (A7, the owner's
#: decision E: *"make sure the job is retried 3 times. if all 3 fails. that it's declared failed
#: properly to story studio."*). Three is the shipped default he named, not a ceiling anyone
#: derived — and it is editable beside the pool size, because a fleet of one Mac and a fleet of
#: fifty want different answers and neither wants a release.
DEFAULT_MAX_ATTEMPTS = 3


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


#: **Fleet-wide ceilings for one model** — none by default (0.2.171, owner 2026-10-12: every Mac runs
#: one generation at a time and scene images are low priority, so YuE2 songs need no cap to shield them).
#: The owner may set one in the settings file, `"models": {"<model id>": N}`; `0` lifts it again.
DEFAULT_MODEL_FLEET_CAPS: dict[str, int] = {}


def model_fleet_cap(model_id: str) -> int | None:
    """At most this many jobs of `model_id` running on the fleet at once, or `None` (no cap)."""
    raw = _read_file().get("models")
    if isinstance(raw, dict) and model_id in raw:
        return _positive_int(raw[model_id])
    return DEFAULT_MODEL_FLEET_CAPS.get(model_id)


def _optional_int(value: Any) -> int | None:
    """A stated count, **preserving a stated zero**, or `None` when nothing was said.

    `_positive_int` collapses `0` into "nobody said", which is right for a *limit*
    and wrong for a *measurement*: a site reporting `available_physical_machine_slots: 0`
    has answered the question — every Mac it has is busy — and a reader that turns
    that into `None` is guessing. This is the reader for the Hub's own numbers.
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
    if float(value) != int(value):
        return None
    return int(value)


# -- the Hub's own reading --------------------------------------------------
#: Free/ceiling field pairs on a Studio Hub's `capacity`, **physical machines first**.
#:
#: The Hub publishes both a *worker service* count and a *physical machine* count, and
#: they disagree wherever one Mac runs more than one worker: live on this Mac,
#: `audio.transcription` read `available_worker_slots: 2` on **one** Mac. Routing already
#: ranks sites by the physical count (`fleet.routing_candidates`), so admission reading
#: the service count instead let a site be told it had two slots and then answer
#: `MACHINE_BUSY` for the second job. One Mac runs one job at a time; that is the number.
_SLOT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("physical_machine", "available_physical_machine_slots",
     "eligible_physical_machine_slots_total"),
    ("worker_service", "available_worker_slots", "eligible_worker_service_slots_total"),
)


def hub_slot_reading(row: dict, operation: str) -> dict[str, Any]:
    """What one Studio Hub location says about one operation, in one shape.

    Returns `{"unit", "scope", "free", "ceiling"}` — `free`/`ceiling` are `None` when the
    Hub states nothing, and a stated `0` stays `0`. Physical machines win over worker
    services; the operation's own row wins over the site-wide one; a Hub that publishes
    neither falls back to its reachable machine count so an older build is not read as
    "no capacity". `unstated` is true only when the Hub said nothing at all.
    """
    capacity = row.get("capacity") if isinstance(row.get("capacity"), dict) else {}
    raw = (capacity.get("by_operation") or {}).get(operation) if operation else None
    entry = raw if isinstance(raw, dict) else {}
    for unit, free_key, total_key in _SLOT_FIELDS:
        for scope, book in (("operation", entry), ("site", capacity)):
            free = _optional_int(book.get(free_key))
            ceiling = _optional_int(book.get(total_key))
            if free is not None or ceiling is not None:
                return {"unit": unit, "scope": scope, "free": free, "ceiling": ceiling,
                        "unstated": False}
    machines = row.get("machine_summary") if isinstance(row.get("machine_summary"), dict) else {}
    reachable = _optional_int(machines.get("reachable"))
    if reachable is not None:
        return {"unit": "physical_machine", "scope": "machines", "free": None,
                "ceiling": reachable, "unstated": True}
    return {"unit": "physical_machine", "scope": "none", "free": None, "ceiling": None,
            "unstated": True}


# -- the settings file ------------------------------------------------------
_settings_cache: dict[str, Any] = {"stamp": None, "value": {}}

#: The last `live_capacity` summary, and who published it. **Read by `/health`**, which is the
#: app's 2.5 s probe: it must not walk the fleet, take a fleet lock or sum anything, because
#: every second it spends is a second the window can decide the gateway is not running. The
#: fleet refresher computes the summary on its own pass and publishes it here
#: (`remember_capacity`), so the probe reads one dict. A reader that finds nothing computes it —
#: the boot window, before the first fan-out has landed.
_capacity_cache: dict[str, Any] = {"value": None}


def remember_capacity(capacity: dict[str, Any]) -> None:
    """Publish a `live_capacity` summary for `/health` to read without doing the work."""
    _capacity_cache["value"] = capacity


def published_capacity() -> dict[str, Any] | None:
    """The last published summary, or `None` before the first fan-out (see `_capacity_cache`)."""
    return _capacity_cache["value"]


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
    # The published capacity is derived from those settings and the fleet, so it goes with them:
    # a test that resets the pool and reads `constants.admission()` must read its own answer, not
    # the last fan-out's.
    _capacity_cache["value"] = None


def _settings_lanes() -> dict[str, int]:
    raw = _read_file().get("lanes")
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for name, value in raw.items():
        limit = _positive_int(value)
        name = str(name).strip().lower()
        if limit is not None and not (name in FLEET_LANES and limit == LEGACY_FLEET_LANE_CAP):
            limits[name] = limit
    return limits


def fleet_capacity(modality: str) -> int:
    """The live fleet's capacity for one fleet lane: the Macs every reachable, unpaused group
    publishes as eligible for its operation, summed from the snapshot routing reads.

    The same per-group reading admission compares each group's in-flight count with
    (`hub_slot_reading`), so the lane total and the groups' budgets can never disagree.
    """
    operation = FLEET_LANES.get(str(modality or "").strip().lower())
    if not operation:
        return 0
    try:
        from . import fleet

        total = 0
        for row in fleet.snapshot(allow_stale=True).get("locations") or []:
            if row.get("reachable") is not True or fleet.is_paused_provider(str(row.get("provider_id") or "")):
                continue
            total += max(0, hub_slot_reading(row, operation).get("ceiling") or 0)
        return total
    except Exception:  # noqa: BLE001 - an unreadable fleet states no capacity
        return 0


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


def fleet_share() -> dict[str, Any]:
    """The pool's fleet share, stated beside `global`: the fleet lanes' own capacity, not a number.

    `global` bounds cloud and other work; fleet-lane work at a location is bounded per group
    and model and so grows with the fleet. Published so a page can say both.
    """
    macs = {name: fleet_capacity(name) for name in FLEET_LANES}
    return {"fleet_share": macs, "fleet_share_detail": (
        "Fleet lanes are not counted against this number: they follow the fleet automatically ("
        + ", ".join(f"{name} {n}" for name, n in macs.items()) + " Macs now).")}


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

        {"global": 60, "queue": {"max_deferrals": 24, "defer_cap_seconds": 120,
                                 "wait_ceiling_seconds": 10800}}

    `wait_ceiling_seconds` is the third number in that family and the one that *ends* a job: past
    it `worker._expired_in_queue` fails the job `QUEUE_TIMEOUT` and `studio_hub._poll_batch`
    withdraws the batch from the Hub's queue. It has to be read here because the adapter and the
    worker both judge the same wait, and a second copy of the number is how they came to disagree.
    """
    wording = _read_file().get("queue")
    entry = wording if isinstance(wording, dict) else {}
    attempts = _positive_int(entry.get("max_deferrals"))
    cap = entry.get("defer_cap_seconds")
    cap_seconds = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else None
    ceiling = entry.get("wait_ceiling_seconds")
    ceiling_seconds = float(ceiling) if isinstance(ceiling, (int, float)) and not isinstance(ceiling, bool) and ceiling > 0 else None
    return {
        "max_deferrals": attempts if attempts is not None else DEFAULT_MAX_DEFERRALS,
        "defer_cap_seconds": cap_seconds if cap_seconds is not None else DEFAULT_DEFER_CAP_S,
        "wait_ceiling_seconds": (ceiling_seconds if ceiling_seconds is not None
                                 else DEFAULT_QUEUE_WAIT_CEILING_S),
        "source": SOURCE_CURATED if (attempts is not None or cap_seconds is not None
                                     or ceiling_seconds is not None) else SOURCE_GATEWAY,
        "path": str(SETTINGS_PATH),
    }


def retry_policy() -> dict[str, Any]:
    """How many machines one job may be tried on, and where that number came from.

    The owner's decision E, and he asked for it to be **written down as well as enforced**:
    *"i want a 'job' that's failed to retry on a different machine … just make sure no duplicates
    but also make sure the job is retried 3 times. if all 3 fails. that it's declared failed
    properly to story studio."*

    Live from the same file as the pool size, so a change lands without a restart:

        {"global": 60, "retry": {"max_attempts": 3}}

    **`max_attempts` counts machines, not dispatches.** A deferral — a site that is busy and has
    nothing else to offer — is not an attempt: it never reached a machine that could have run the
    work. `attempt_count` on the job row counts every dispatch including those; the attempt *log*
    (see `store.record_attempt`) is what this ceiling is measured against.
    """
    wording = _read_file().get("retry")
    entry = wording if isinstance(wording, dict) else {}
    stated = _positive_int(entry.get("max_attempts"))
    return {
        "max_attempts": stated if stated is not None else DEFAULT_MAX_ATTEMPTS,
        "source": SOURCE_CURATED if stated is not None else SOURCE_GATEWAY,
        "path": str(SETTINGS_PATH),
    }


def retry_policy_wording() -> str:
    """The policy in the owner's own terms, for the console and the docs to quote.

    One string, so the page an operator reads and the document he asked for cannot drift: the
    policy is enforced in `worker._next_machine` and stated here.
    """
    return (
        "A failed job is retried on a **different machine** — another Mac in the same cluster "
        "first, then any other site offering the same model — up to {n} attempts in total. A "
        "retry happens only when the previous attempt is *known* to have failed; a machine that "
        "accepted the work and then went silent is an unknown outcome, is never re-run "
        "automatically, and waits for reconciliation. After the last attempt the job is declared "
        "failed to Story Studio with a reason naming every machine that was tried and why each "
        "one failed. A clone voice's reference clip travels with the job, so it is retried like any "
        "other job; only a voice whose clip lives on one Hub alone keeps that location, and its "
        "failure there is terminal after its own attempts."
    ).format(n=retry_policy()["max_attempts"])


def live_capacity(*, worker: Any = None, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """**What the fleet can actually do right now**, per modality and in total.

    The owner's decision D, in his words: *"i want story to be able to run what the fleet can
    actually do. setting a number isn't correct. because i may have 50 machines now, but i'll
    have 30 if i sell 20 of them … so it must be a number that gateway has and offer to story
    directly. and it must by dynamic of reality."*

    Published was the *settings* pool: live **60**, while the fleet could do about **4**. Two
    numbers, and the one Story acted on was the one nobody measured (H7).

    **One source, and it is the number admission itself uses.** For every reachable location and
    every modality, this is `hub_slot_reading` + `hub_budget` — the same two functions
    `Worker._provider_budget` calls — summed. A second derivation would be free to disagree with
    the ceiling that actually bounds dispatch, which is the defect this replaces, one layer along.

    Recomputed on every call, and the callers are the fleet refresh and every membership change,
    so a Mac that is sold leaves the number the next time Story asks. Nothing here is cached
    beyond the fleet's own 5-second snapshot.

    `worker` is accepted for its callers; since 0.2.82 the figure no longer depends on this
    gateway's own in-flight count (`hub_budget` states the site's eligible total).

    `rows` is the fleet fan-out's own rows, for the one caller that already has them
    (`fleet._refresh`, which publishes the summary with `remember_capacity`). Passing them is
    not an optimisation: reading the snapshot from inside a fan-out would re-enter it.
    """
    per_modality: dict[str, int] = {}
    locations: list[dict[str, Any]] = []
    if rows is None:
        try:
            from . import fleet as fleet_module

            rows = fleet_module.snapshot(allow_stale=True).get("locations") or []
        except Exception:  # noqa: BLE001 - an unreadable fleet is zero capacity, not a crash
            rows = []
    for row in rows:
        provider_id = str(row.get("provider_id") or "")
        reachable = row.get("reachable") is not False
        entry: dict[str, Any] = {"provider_id": provider_id, "reachable": reachable,
                                 "by_modality": {}}
        for modality, operation in _LIVE_CAPACITY_MODALITIES:
            reading = hub_slot_reading(row, operation)
            free = reading.get("free")
            if free is None and not reading.get("unstated"):
                free = reading.get("ceiling")
            budget = hub_budget(
                free, operation=operation,
                stated=stated_provider_limit(provider_id),
                ceiling=reading.get("ceiling"),
                reachable=reachable,
            )
            # A **cloud** provider is not part of this: D is about the Macs, and a vendor lane's
            # limit is a contract with the vendor rather than a machine you can sell. The
            # settings file still governs those, exactly as before.
            if _is_hub_provider(provider_id):
                per_modality[modality] = per_modality.get(modality, 0) + budget.limit
                entry["by_modality"][modality] = budget.limit
            else:
                entry["by_modality"][modality] = None
        locations.append(entry)
    total = sum(per_modality.values())
    pool = global_budget()
    return {
        "total": total,
        "by_modality": per_modality,
        "locations": locations,
        # **The pool is a local pool, not the published answer.** It bounds how many jobs *this*
        # gateway runs at once; the fleet figure is what the fleet can do. `global` is reported
        # beside it so a reader can see the two disagree rather than discovering it by counting.
        "pool": {"limit": pool.limit, "source": pool.source, "detail": pool.detail},
        "source": "the live fleet, summed over reachable locations",
    }


#: Which modalities `live_capacity` sums, and the Hub operation each is served by.
_LIVE_CAPACITY_MODALITIES: tuple[tuple[str, str], ...] = (
    ("chat", "chat.completion"),
    ("image", "image.text_to_image"),
    ("video", "video.generation"),
    ("tts", "voice.tts"),
    ("transcription", "audio.transcription"),
    ("music", "music.generation"),
)


def _is_hub_provider(provider_id: str) -> bool:
    """Whether this provider id is a Studio Hub location rather than a cloud lane."""
    try:
        from .fleet import PROVIDER_PREFIX

        return str(provider_id or "").startswith(PROVIDER_PREFIX)
    except Exception:  # noqa: BLE001 - unknown means "not a location"
        return False


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
    if name in FLEET_LANES:
        macs = fleet_capacity(name)
        return Budget(limit=macs, source=SOURCE_FLEET,
                      detail=f"Automatic — follows the fleet (now {macs} Mac{'' if macs == 1 else 's'})")
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


def pool_budget(member_ids: list[str]) -> Budget:
    """A model pool's concurrency: **the sum of what its members' providers allow**.

    A pool is not a provider with a rate limit of its own; it is a walk over other providers'
    models, and it was bounded as if it were one (`cloud_budget("pool")` → 1), so three
    Dreamlands script expansions on `pool::gemini-free-all` ran strictly one after another
    (~20 min instead of ~7) while the members' providers allowed 5 and 10. The owner's explicit
    `pool` entry in the settings file still wins. Each member provider's own limit is enforced
    again where the call is made (`adapters/pool.py`), so admitting up to the sum cannot push
    any one provider past its number.
    """
    entry = _settings_providers().get("pool")
    if entry is not None:
        return cloud_budget("pool")
    providers = list(dict.fromkeys(
        str(m).partition("::")[0] for m in member_ids if str(m).partition("::")[0] not in ("", "pool")
    ))
    if not providers:
        return cloud_budget("pool")
    budgets = {p: cloud_budget(p) for p in providers}
    stated = all(b.source == SOURCE_CURATED for b in budgets.values())
    return Budget(
        limit=sum(b.limit for b in budgets.values()),
        source=SOURCE_CURATED if stated else SOURCE_GATEWAY,
        detail="the pool's members' providers: "
               + ", ".join(f"{p} {b.limit}" for p, b in budgets.items()),
    )


def hub_budget(slots: Any, *, operation: str = "", stated: Any = None,
               ceiling: Any = None, reachable: bool = True) -> Budget:
    """A Studio Hub location's concurrency, from the site's own total and your cap.

    `slots` is the site's **free** count for this operation — `available_worker_slots` or,
    where the Hub publishes it, `available_physical_machine_slots` (see
    `hub_slot_reading`: the physical count is the one routing already uses). `ceiling` is
    the site's own eligible total for the same operation. `reachable` is the site's
    reachability from the last fleet read.

    **Queue ahead (0.2.82): the number is the site's eligible total, not what is free now.**
    It was `min(free + in_flight, ceiling)` -- which is the total again whenever this
    gateway owns the busy Macs, and *less* than it when the Hub's own queue or another
    client held some. Either way the gateway kept the Hub's queue near-empty and held a
    burst itself, bound to one site, while another site's Macs went idle (measured
    2026-09-30, 20 music jobs). Now each site holds up to its eligible total (an "up to"
    ceiling from the Hub, never a number of ours); the Hub's queue is durable and
    cancellable, and the adapter's 3 h queue ceiling still withdraws work that waits too
    long. The worker compares this with its own in-flight count at that site, and when the
    site is full while another has room it re-binds a job that was never sent.

    A Hub that publishes free slots and **no** total cannot distinguish "three free of
    three" from "three free of thirty", so its free count is used as-is.

    **A site that is not reachable has no budget**, and returns `0`: admission then agrees
    with routing, which already skips it.

    **A stated zero never becomes a permanent wall.** With no total, `free == 0` means the
    site is busy *right now*, and `_positive_int` reads it as "no usable reading" — the
    site falls through to the operator's cap or the default of one.

    **The smaller of the site and the operator wins, and they stay distinguishable.** The
    site's count is what it can take; the operator's number is policy, and an operator must
    be able to throttle a site below what it offers. The converse must not hold: a number
    typed into a form is not licence to send a site more concurrent work than it states —
    which is why this is a `min` rather than a substitution.
    """
    if not reachable:
        return Budget(
            limit=0,
            source=SOURCE_HUB,
            detail=("the site is not reachable, so this gateway claims no admission "
                    "budget for it; routing skips it for the same reason"),
        )
    free = _optional_int(slots)
    free = max(0, free) if free is not None else None
    total = _positive_int(ceiling)
    # **Queue ahead: a site that states its own total is admitted up to that total**, whatever
    # its free count says right now. The Hub's queue is durable and cancellable, so work handed
    # to it waits there for whichever Mac frees first -- instead of waiting here, where a burst
    # bound to one site sat while another site's Macs idled.
    # A Hub that states free slots and no total cannot distinguish "three free of three" from
    # "three free of thirty", so its free count is used as-is -- conservative, not wrong.
    live = total if total is not None else free
    if live is not None and live <= 0:
        # "Every Mac is busy right now" is not "this site is closed for ever".
        live = None
    site = live
    cap = _positive_int(stated)
    clamped = ""
    how = (" That is the site's eligible total, held ahead in the Hub's own queue for "
           "whichever Mac frees first.") if total is not None and live == total else ""
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
                + "." + how + clamped
            ),
        )
    limit = min(site, cap)
    tighter = "your limit is tighter" if cap <= site else "the site's count is tighter"
    return Budget(
        limit=limit,
        source=SOURCE_CURATED if cap <= site else SOURCE_HUB,
        detail=(f"the site reports {site} free and you set {cap}, so {limit} runs — "
                f"{tighter}." + how + clamped),
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
            for operation in by_operation:
                # **The same reader admission uses**, so the console cannot describe a
                # different number from the one that bounds dispatch: physical machines
                # before worker services (M1 — they disagreed 2:1 for transcription on
                # one Mac, and the service count won one job too many).
                reading = hub_slot_reading(row, str(operation))
                stated = reading.get("free")
                if stated is None:
                    stated = reading.get("ceiling")
                stated = _positive_int(stated)
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
                   "stated": _positive_int(_read_file().get("global")),
                   **fleet_share()},
        "lanes": {
            **lane_budget("").as_dict(),
            "values": _settings_lanes(),
            "rows": [{
                "lane": name,
                **lane_budget(name).as_dict(),
                "stated": _settings_lanes().get(name),
                "automatic": lane_budget(name).source == SOURCE_FLEET,
            } for name in names],
        },
        "providers": providers,
        # **The retry policy, on the page an operator already opens.** The owner asked for it in
        # two places by name — a document and the UI — and a number that governs how many
        # machines a job is tried on belongs beside the pool size it competes with. `wording` is
        # the same string the job envelope publishes, so the page and the API cannot disagree.
        "retry": {**retry_policy(), "wording": retry_policy_wording()},
        "queue": queue_wait_budget(),
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

    if "retry" in payload:
        stated = payload["retry"]
        if stated in (None, "", 0):
            current.pop("retry", None)
        else:
            entry = stated if isinstance(stated, dict) else {"max_attempts": stated}
            attempts = _positive_int(entry.get("max_attempts") if isinstance(entry, dict) else None)
            if attempts is None:
                raise SettingsRefused(
                    f"retry.max_attempts must be a whole number of machines, not "
                    f"{entry.get('max_attempts') if isinstance(entry, dict) else stated!r}"
                )
            current["retry"] = {"max_attempts": attempts}

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
