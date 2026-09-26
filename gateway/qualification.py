"""What a model has actually been measured doing, from real work.

**Nothing here is stated; everything is measured.** The catalogue publishes what a
producer *says* — a ceiling, a context window, a source. This module publishes what
this gateway has *seen*: how large a prompt and a completion really ran, on which
machine, at what rate. The two must never be confused, so every row here carries
`source: "gateway"` and is reported *beside* the stated figures rather than folded
into them. A measured number of ours wearing a producer's label is the one thing
both projects refuse.

**Why it exists.** Asked 2026-09-26: *"we need to keep these for that when I want to
qualify a new LLM, we can apply the same parameter check too."* Before this there was
no record at all — the numbers were read once by hand and lost. Now a model is
qualified by running it and reading the envelope, and the same procedure answers for
the next one.

**It is also the only honest way to size a ceiling against a deadline.** At 17.3
tokens/second a 900-second window carries about 15,500 tokens; on a Mac half that
fast it carries 7,750. Which number applies is a measurement, not an opinion, and a
proposal to raise a cap without it is a guess.

**Nothing new is recorded for this.** Every figure is read from the result payload
already stored on each job: `usage` carries `prompt_tokens`/`completion_tokens`, and
`generation_seconds` is the worker's own measured generation time — after the model
is resident, so it excludes a cold turn's load and is the rate a scheduler wants.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable

#: How many recent jobs one read will look at. ponytail: a flat window rather than a
#: time range, so a busy lane is described by its latest work and an idle one still
#: has something to show. Ceiling — a model last run a month ago falls out of the
#: window and reads as unqualified rather than as stale. Upgrade path — a stored
#: rollup per (model, machine), maintained as jobs settle.
DEFAULT_WINDOW = 500

#: ponytail: **one envelope per (provider, model), whatever the work was.** A prose
#: chapter and a scene-plan JSON body go through the same lane and land in the same
#: row, so `max_completion_tokens` is the largest of *either* and the median rate mixes
#: them. That is a real limit and it is stated rather than hidden, because JSON is
#: denser in tokens per word than the prose the ratios were measured on, so a reader
#: comparing that maximum to a ceiling should know which work produced it.
#: Ceiling — no split by output shape. Upgrade path — every job already stores its
#: `params`, so `json_extract(params, '$.response_format.type')` is a read change and
#: not a schema change; split the row when a lane has run enough of both to be worth
#: telling apart.


def _token(value: Any) -> int | None:
    """A stated token count, or `None`.

    **`bool` is an `int` in Python**, so `True` read as a number would enter the record
    as a one-token completion — a fabricated sample, and the kind this module exists to
    refuse. `False` is worse: it is a zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _rows_from_samples(samples: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per settled job that stated a usage, from `store.usage_samples`."""
    rows: list[dict[str, Any]] = []
    for sample in samples:
        prompt = _token(sample.get("prompt_tokens"))
        completion = _token(sample.get("completion_tokens"))
        if prompt is None and completion is None:
            # A lane that states no usage is not measured by this. Counting it as a
            # zero would drag every envelope toward "this lane does nothing".
            continue
        seconds = sample.get("generation_seconds")
        rate = None
        if (completion and isinstance(seconds, (int, float))
                and not isinstance(seconds, bool) and seconds > 0):
            rate = completion / float(seconds)
        machine = sample.get("hub_machine")
        rows.append({
            "provider_id": str(sample.get("provider_id") or ""),
            "model_id": str(sample.get("model_id") or ""),
            "modality": str(sample.get("modality") or ""),
            "prompt": prompt,
            "completion": completion,
            "rate": rate,
            "machine": machine.strip() if isinstance(machine, str) and machine.strip() else None,
            "at": sample.get("updated_at") or sample.get("created_at") or 0.0,
        })
    return rows


def envelope(
    store: Any, *, provider_id: str | None = None, model_id: str | None = None,
    limit: int = DEFAULT_WINDOW,
) -> list[dict[str, Any]]:
    """The measured envelope per (provider, model), newest work first.

    Reads what is already stored — no schema, no extra call, no cost. A model that
    has never run is **absent** rather than present with zeros: "no measurement" and
    "measured at nothing" are different answers, and only one of them is a reason to
    go and run something.
    """
    rows = _rows_from_samples(store.usage_samples(limit=limit))
    if provider_id:
        rows = [r for r in rows if r["provider_id"] == provider_id]
    if model_id:
        rows = [r for r in rows if r["model_id"] == model_id]

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["provider_id"], row["model_id"]), []).append(row)

    out = [
        {"provider_id": key[0], "model_id": key[1],
         "modalities": sorted({r["modality"] for r in group if r["modality"]}),
         **_summarise(group)}
        for key, group in grouped.items()
    ]
    out.sort(key=lambda row: (row["provider_id"], row["model_id"]))
    return out


def published_index(
    store: Any, *, limit: int = DEFAULT_WINDOW,
) -> dict[tuple[str, str], dict[str, Any]]:
    """The same record keyed by the id a caller sees, for the catalogue to join on.

    **A location's jobs are keyed by the location, but its models are published by the
    maker.** A job routed to `studio-hub-9701220bf30f` is catalogue row
    `studio-hub::~<model>`, and the same model at six sites is **one** catalogue entry
    with six sites' work behind it — so the envelope Story Studio reads is the fleet's,
    not the first site's. Keying on the raw provider id left every local row reading as
    unmeasured, which is the one lane the question is about.

    Every row has the same keys whether one site contributed or six: a shape that
    changes with the number of contributors is a shape a reader has to guess at.
    """
    from .fleet import publish_id

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in envelope(store, limit=limit):
        groups.setdefault((publish_id(row["provider_id"]), row["model_id"]), []).append(row)

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rows in groups.items():
        index[key] = {
            "provider_id": key[0],
            "model_id": key[1],
            "sites": sorted({row["provider_id"] for row in rows}),
            "modalities": sorted({m for row in rows for m in row["modalities"]}),
            "samples": sum(row["samples"] for row in rows),
            "with_usage": sum(row["with_usage"] for row in rows),
            # **Across the fleet, not first-row-wins.** A model offered by six
            # locations has six machines' work behind it; one Mac's slowest run is not
            # the fleet's envelope and one Mac's fastest is not either.
            "max_prompt_tokens": _max_of(rows, "max_prompt_tokens"),
            "max_completion_tokens": _max_of(rows, "max_completion_tokens"),
            "median_completion_tokens": _median_of(rows, "median_completion_tokens"),
            "median_tokens_per_second": _median_of(rows, "median_tokens_per_second"),
            # **The slowest site, published beside the median, because the caller does
            # not choose the Mac.** The gateway picks the location at dispatch, so a
            # ceiling derived from the median is a ceiling that one machine in the
            # fleet cannot meet — measured on the owner's own fleet 2026-09-26: M1
            # 12 tok/s, M2 14, M4 18. A median of 15.4 describes the fleet and hides
            # the 12; anything sized against the median is sized against a machine the
            # job may not land on.
            "slowest_site_tokens_per_second": _min_of(rows, "median_tokens_per_second"),
            "machines": {name: info for row in rows for name, info in row["machines"].items()},
            "first_seen": min(row["first_seen"] for row in rows),
            "last_seen": max(row["last_seen"] for row in rows),
            "source": "gateway",
        }
    return index


def _max_of(rows: list[dict[str, Any]], field: str) -> Any:
    values = [row[field] for row in rows if row.get(field) is not None]
    return max(values) if values else None


def _min_of(rows: list[dict[str, Any]], field: str) -> Any:
    values = [row[field] for row in rows if row.get(field) is not None]
    return min(values) if values else None


def _median_of(rows: list[dict[str, Any]], field: str) -> Any:
    values = [row[field] for row in rows if row.get(field) is not None]
    if not values:
        return None
    value = statistics.median(values)
    return round(value, 1) if field.endswith("per_second") else int(value)



def _summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One (provider, model) from the jobs that ran it."""
    prompts = [r["prompt"] for r in rows if r["prompt"] is not None]
    completions = [r["completion"] for r in rows if r["completion"] is not None]
    rates = [r["rate"] for r in rows if r["rate"] is not None]
    machines: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["machine"]:
            machines.setdefault(row["machine"], []).append(row)
    return {
        "samples": len(rows),
        "with_usage": len(completions),
        # **The largest that actually ran**, which is the only figure that answers
        # "can this lane do it" — a stated maximum answers "may I ask".
        "max_prompt_tokens": max(prompts) if prompts else None,
        "max_completion_tokens": max(completions) if completions else None,
        "median_completion_tokens": (
            int(statistics.median(completions)) if completions else None
        ),
        # Median rather than mean: one cold or contended run must not move the rate
        # a ceiling is sized against.
        "median_tokens_per_second": round(statistics.median(rates), 1) if rates else None,
        "machines": {
            name: {
                "samples": len(group),
                "max_completion_tokens": max(
                    (g["completion"] for g in group if g["completion"] is not None),
                    default=None,
                ),
                "median_tokens_per_second": (
                    round(statistics.median([g["rate"] for g in group
                                             if g["rate"] is not None]), 1)
                    if any(g["rate"] is not None for g in group) else None
                ),
            }
            for name, group in sorted(machines.items())
        },
        "first_seen": min(r["at"] for r in rows),
        "last_seen": max(r["at"] for r in rows),
        # **Never omitted, and never inferred from the numbers above.** Every figure
        # in this row is this gateway's own measurement; the producer stated none of
        # it.
        "source": "gateway",
    }


def qualification_for(
    store: Any, provider_id: str, model_id: str, *, limit: int = DEFAULT_WINDOW,
) -> dict[str, Any] | None:
    """One model's envelope, or `None` when it has never been measured."""
    found = envelope(store, provider_id=provider_id, model_id=model_id, limit=limit)
    return found[0] if found else None
