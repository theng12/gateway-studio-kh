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

**Where a provider's number comes from, best first — and the answer says which.**
This is the same triad the catalogue already uses for token limits, and for the
same reason: *"I could not work out the answer"* and *"there is no limit"* must
never look the same.

  1. `hub` — a Studio Hub location states its own free slots on
     `capacity.by_operation`. It is the only source that is a live measurement
     rather than a belief, and it is what lets a five-Mac site take five jobs.
  2. `curated` — the owner's file, for a cloud provider that publishes nothing
     machine-readable. Most do: OpenAI and OpenRouter send `x-ratelimit-*`
     headers describing request *rate*, not concurrency, and ElevenLabs publishes
     a concurrency figure in prose on a pricing page. None of that is a number
     this gateway can read, so a person puts it here.
  3. `gateway` — a labelled default of 1. Deliberately the safe direction, and
     deliberately visible: a lane running at the default is under-used, and the
     source field is how a reader knows to go and curate it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR

#: The owner's curated per-provider concurrency. Same `*.local.json` convention as
#: `model-limits.local.json`: user-owned, never shipped, survives a reinstall.
CURATED_PATH = Path(
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
#: rather than a new figure to justify. `constants.admission()` derives what it
#: publishes from here, so the two cannot drift.
DEFAULT_GLOBAL = 4

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
    if value <= 0:
        return None
    return int(value)


def global_limit() -> int:
    """How many jobs this gateway runs at once.

    `GATEWAY_JOB_CONCURRENCY` overrides. An unreadable or non-positive value falls
    back to the default rather than to zero — a typo in an environment variable
    must not stop the gateway taking work at all.
    """
    return _positive_int(os.environ.get("GATEWAY_JOB_CONCURRENCY")) or DEFAULT_GLOBAL


_lane_cache: dict[str, Any] = {"raw": None, "limits": {}}


def _lane_limits() -> dict[str, int]:
    """Per-modality caps from `GATEWAY_LANE_CONCURRENCY`, e.g. `image=2,chat=3`.

    Deliberately empty by default: every lane may use the whole pool, so the
    shipped behaviour is FIFO across everything and nothing is reserved for a lane
    that has no work. A site that wants a slot always free for chat sets
    `GATEWAY_LANE_CONCURRENCY=image=1`.
    """
    raw = os.environ.get("GATEWAY_LANE_CONCURRENCY") or ""
    if raw == _lane_cache["raw"]:
        return dict(_lane_cache["limits"])
    limits: dict[str, int] = {}
    for part in raw.split(","):
        name, _, value = part.partition("=")
        name = name.strip().lower()
        limit = _positive_int(value.strip())
        if name and limit is not None:
            limits[name] = limit
    _lane_cache["raw"] = raw
    _lane_cache["limits"] = limits
    return dict(limits)


def lane_budget(modality: str) -> Budget:
    """How much of the pool one modality may hold."""
    name = str(modality or "").strip().lower()
    stated = _lane_limits().get(name)
    if stated is not None:
        return Budget(
            limit=stated,
            source=SOURCE_CURATED,
            detail=f"GATEWAY_LANE_CONCURRENCY says {name}={stated}",
        )
    return Budget(
        limit=global_limit(),
        source=SOURCE_GATEWAY,
        detail="no lane cap set, so this lane may use the whole pool",
    )


_curated_cache: dict[str, Any] = {"stamp": None, "entries": {}}


def _curated() -> dict[str, dict[str, Any]]:
    """The owner's file, re-read when it changes.

    Cached on `(mtime, size)` like `.env` is, because this sits on the admission
    path: a fix to a rate limit should take effect without a restart, and a
    stat per admitted job is cheap.
    """
    path = CURATED_PATH if CURATED_PATH.exists() else LEGACY_PATH
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    if stamp == _curated_cache["stamp"]:
        return dict(_curated_cache["entries"])

    entries: dict[str, dict[str, Any]] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        parsed = None
    raw = parsed.get("providers") if isinstance(parsed, dict) else None
    if isinstance(raw, dict):
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
    _curated_cache["stamp"] = stamp
    _curated_cache["entries"] = entries
    return dict(entries)


def reset_for_tests() -> None:
    _lane_cache["raw"] = None
    _lane_cache["limits"] = {}
    _curated_cache["stamp"] = None
    _curated_cache["entries"] = {}


def cloud_budget(provider_id: str) -> Budget:
    """A cloud provider's concurrency: curated if the owner said, else the default.

    The `detail` on the default names the file to edit, because a lane running at
    1 is under-used and the reader should not have to go looking for why.
    """
    entry = _curated().get(str(provider_id or ""))
    if entry is not None:
        note = entry.get("note") or "the owner's file"
        return Budget(limit=entry["limit"], source=SOURCE_CURATED, detail=note)
    return Budget(
        limit=DEFAULT_PROVIDER,
        source=SOURCE_GATEWAY,
        detail=(
            "no stated or curated concurrency, so one at a time; set it in "
            f"{CURATED_PATH.name} under providers.{provider_id}.limit"
        ),
    )


def hub_budget(slots: Any, *, operation: str = "") -> Budget:
    """A Studio Hub location's concurrency, from what the site itself published.

    `slots` is `available_worker_slots` from that site's `capacity.by_operation`
    — the site's own count of free workers for this operation, which is the only
    live measurement in this module. A site that states nothing gets the default:
    absence is not a claim that it can take several.
    """
    limit = _positive_int(slots)
    if limit is None:
        return Budget(
            limit=DEFAULT_PROVIDER,
            source=SOURCE_GATEWAY,
            detail=(
                "the site did not state free slots"
                + (f" for {operation}" if operation else "")
                + ", so one at a time"
            ),
        )
    return Budget(
        limit=limit,
        source=SOURCE_HUB,
        detail=(
            f"the site reports {limit} available worker slot"
            f"{'' if limit == 1 else 's'}" + (f" for {operation}" if operation else "")
        ),
    )
