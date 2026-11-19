#!/usr/bin/env python3
"""Why every other location was refused is written down, not thrown away (0.2.290).

**The real case (measured live 2026-11-19, job `7281f6a2`).** A job waited out its placement
ceiling and ended with "waiting for a free Mac at <the site it was tried on>" and nothing else.
From the store: `attempted_locations` = one site, `attempt_count` 0. Every skip in
`routing_candidates` computes a reason (`fleet.py`, "Every skip can say why"), but `_next_machine`
called `fleet.route` without `rejections`, so `reject()` returned immediately and the walk left no
trace. The job was undiagnosable by design, not by accident.

Two things are pinned here:

1. `fleet.route(..., rejections=…)` fills the list for every location it passes over.
2. A placement-ceiling failure names those refusals in the job's `error_detail`, and the newest
   attempt entry carries them as `rejections` — so the next incident is read, not guessed.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-rejections-recorded-")

from gateway import concurrency, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

MODEL_ID = "mlx-community/qwen-image-2-1-turbo-mflux-q4"
MODALITY = "image"
TRIED = "9701220bf30f"      # the site the job was stuck on
OTHER = "eda08bae6325"      # the site that should say why it was passed over


def _site(site_id: str, *, eligible: int | None) -> dict:
    """One site row shaped the way the Hub publishes it; `eligible` 0 = it states no Mac for it."""
    return {
        "id": site_id,
        "provider_id": f"studio-hub-{site_id}",
        "label": f"Site {site_id}",
        "reachable": True,
        "slow": False,
        "answered_at": time.time(),
        "role": "controller",
        "drained": False,
        "capacity": {"accepting_customer_jobs": True, "queue_depth": 0},
        "paused_at": None,
        "machines": [{"machine": f"mac-{i}", "reachable": True} for i in range(3)],
        "machine_summary": {"reachable": 3, "total": 3},
        "running_jobs": [],
        "model_supply": ({MODEL_ID: {"eligible": eligible, "available": eligible}}
                         if eligible is not None else {}),
    }


def _definition(site_id: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=f"studio-hub-{site_id}", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=(MODALITY,),
        discovery="static",
    )


def _enabled(requested: str):
    if requested != MODALITY:
        return []
    row = {"model_id": MODEL_ID, "display_name": "qwen", "modality": MODALITY,
           "enabled": True, "raw": {}}
    return [(_definition(TRIED), row), (_definition(OTHER), row)]


class RejectionsAreRecordedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="rejections-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.job, _ = self.store.submit(
            modality=MODALITY, model_id=MODEL_ID,
            provider_id=f"studio-hub-{TRIED}", params={"prompt": "hi"},
            idempotency_key="rejections-1",
        )
        self.worker = Worker(self.store)

    def _patches(self):
        rows = [_site(TRIED, eligible=1), _site(OTHER, eligible=0)]
        return (
            patch.object(fleet, "snapshot", lambda **_kw: {"locations": rows}),
            patch.object(registry.models, "enabled_models", _enabled),
        )

    def test_fleet_route_fills_the_list_when_it_picks_nothing(self) -> None:
        rejections: list[dict] = []
        snap, enabled = self._patches()
        with snap, enabled:
            chosen, _note = fleet.route(MODEL_ID, MODALITY,
                                        exclude_locations={TRIED}, rejections=rejections)
        self.assertIsNone(chosen)
        self.assertTrue(rejections, "the walk passed over a location and said nothing")
        self.assertIn(OTHER, {str(row.get("location_id")) for row in rejections})
        self.assertTrue(all(row.get("reason") for row in rejections),
                        f"a rejection with no reason is not a reason: {rejections}")

    def test_a_placement_failure_names_the_other_locations(self) -> None:
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        snap, enabled = self._patches()
        with snap, enabled:
            self.worker._note_waiting(self.store.get(self.job["id"]))
        row = self.store.get(self.job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        detail = row.get("error_detail") or ""
        self.assertIn("Every other location was passed over", detail)
        self.assertIn(OTHER, detail, f"the refusal did not name the other site: {detail}")

    def test_the_newest_attempt_entry_carries_them(self) -> None:
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        snap, enabled = self._patches()
        with snap, enabled:
            self.worker._note_waiting(self.store.get(self.job["id"]))
        log = self.store.attempts_of(self.job["id"])
        self.assertTrue(log, "the failure recorded no attempt")
        self.assertTrue(log[-1].get("rejections"),
                        f"the newest attempt entry has no rejections: {log[-1]}")
        # The reasons are evidence, never an attempt: nothing is added to the tried set.
        self.assertEqual(self.store.get(self.job["id"])["attempt_count"], 0)


if __name__ == "__main__":
    unittest.main()
