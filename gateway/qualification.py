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

import json
import statistics
from typing import Any, Iterable

#: How many recent jobs one read will look at. ponytail: a flat window rather than a
#: time range, so a busy lane is described by its latest work and an idle one still
#: has something to show. Ceiling — a model last run a month ago falls out of the
#: window and reads as unqualified rather than as stale. Upgrade path — a stored
#: rollup per (model, machine), maintained as jobs settle.
DEFAULT_WINDOW = 500

#: Which key on a payload names the machine that did the work. The image, voice and
#: music lanes set `hub_machine`; **the chat lane does not**, so a chat model's
#: envelope is per location rather than per Mac until Studio Hub names one.
MACHINE_KEYS = ("hub_machine", "machine")


def _tokens(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """`(prompt, completion)` as the worker stated them, or `None` each."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    def one(key: str) -> int | None:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)
    return one("prompt_tokens"), one("completion_tokens")


def _machine(payload: dict[str, Any]) -> str | None:
    for key in MACHINE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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


def _rows_from_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in jobs:
        raw = job.get("result_json")
        if not raw:
            continue
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            continue
        payload = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(payload, dict):
            continue
        prompt, completion = _tokens(payload)
        if prompt is None and completion is None:
            # A lane that states no usage is not measured by this. Counting it as a
            # zero would drag every envelope toward "this lane does nothing".
            continue
        seconds = payload.get("generation_seconds")
        rate = None
        if (completion and isinstance(seconds, (int, float))
                and not isinstance(seconds, bool) and seconds > 0):
            rate = completion / float(seconds)
        rows.append({
            "provider_id": str(job.get("provider_id") or ""),
            "model_id": str(job.get("model_id") or ""),
            "modality": str(job.get("modality") or ""),
            "prompt": prompt,
            "completion": completion,
            "rate": rate,
            "machine": _machine(payload),
            "at": job.get("updated_at") or job.get("created_at") or 0.0,
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
    jobs = store.list_jobs(limit=limit)
    rows = _rows_from_jobs(jobs)
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


def qualification_for(
    store: Any, provider_id: str, model_id: str, *, limit: int = DEFAULT_WINDOW,
) -> dict[str, Any] | None:
    """One model's envelope, or `None` when it has never been measured."""
    found = envelope(store, provider_id=provider_id, model_id=model_id, limit=limit)
    return found[0] if found else None
