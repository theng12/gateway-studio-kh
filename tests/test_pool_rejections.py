#!/usr/bin/env python3
"""A pool job that finds nowhere to go says which sites it passed over, and why (0.2.294).

`_next_machine` fills `rejections` for a direct-model re-route (0.2.290) and said in its own
docstring that a **pool** re-route does not: `pool_candidates` has its own per-member walk and took
no `rejections` list, so `route_pool` returned one sentence naming no site. A pool job that waited
out its placement ceiling therefore ended exactly the way the 2026-11-19 stall did — "waiting for a
free Mac at <one site>", with the other sites' reasons computed and discarded.

A pool has a second refusal of its own that no other walk makes: a site can be perfectly able to run
the member and still have **no Mac assigned it**, and that is the check `pool_candidates` makes after
`routing_candidates` has already accepted the site. That reason is written here too, because it is
the one an operator is most likely to meet.

Pinned: the sentence the operator reads, not merely that a field was set.
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

_SANDBOX = sandbox_env("gateway-pool-rejections-")

from gateway import concurrency, fleet, image_assignment, pools  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.worker import Worker  # noqa: E402

MEMBER = "z-image-turbo-6b-mlx-4bit"
MODEL_ID = f"studio-hub::{MEMBER}"
MODALITY = "image"
TRIED = "9701220bf30f"
OTHER = "eda08bae6325"


def _site(site_id: str) -> dict:
    """A site that can run the member — reachable, not paused, answering — with no Mac assigned it."""
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
        "machines": [{"machine": f"mac-{i}", "reachable": True,
                      "assigned_image_model": "some-other-model"} for i in range(3)],
        "machine_summary": {"reachable": 3, "total": 3},
        "running_jobs": [],
        "model_supply": {},
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
    row = {"model_id": MEMBER, "display_name": "z-image", "modality": MODALITY,
           "enabled": True, "raw": {}}
    return [(_definition(TRIED), row), (_definition(OTHER), row)]


class PoolRejectionsAreRecordedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(tempfile.mkdtemp(prefix="pool-rejections-")) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.job, _ = self.store.submit(
            modality=MODALITY, model_id=MODEL_ID,
            provider_id=f"studio-hub-{TRIED}", params={"_pool_id": "standard-image-fleet"},
            idempotency_key="pool-rejections-1",
        )
        self.worker = Worker(self.store)

    def _patches(self):
        rows = [_site(TRIED), _site(OTHER)]
        return (
            patch.object(fleet, "snapshot", lambda **_kw: {"locations": rows}),
            patch.object(registry.models, "enabled_models", _enabled),
            patch.object(pools, "get", lambda _pool_id: {"id": "standard-image-fleet"}),
            patch.object(image_assignment, "pool_models", lambda _pool: [MEMBER]),
        )

    def test_a_pool_failure_names_the_sites_it_passed_over(self) -> None:
        ceiling = float(concurrency.queue_wait_budget()["placement_wait_ceiling_seconds"])
        self.worker._placement_wait_started[self.job["id"]] = time.time() - ceiling - 1
        for patch_ in self._patches():
            patch_.start()
            self.addCleanup(patch_.stop)
        self.worker._note_waiting(self.store.get(self.job["id"]))
        row = self.store.get(self.job["id"])
        self.assertEqual(row["state"], "failed", "the ceiling did not end the job")
        self.assertEqual(row["error_code"], "PLACEMENT_TIMEOUT")
        detail = row.get("error_detail") or ""
        self.assertIn("Every other location was passed over", detail,
                      f"a pool failure named no site: {detail}")
        self.assertIn(OTHER, detail, f"the refusal did not name the other site: {detail}")
        self.assertIn(MEMBER, detail,
                      f"the sentence did not say which pool model had no Mac assigned: {detail}")

    def test_a_pool_out_of_attempts_still_says_why(self) -> None:
        rejections: list[dict] = []
        for patch_ in self._patches():
            patch_.start()
            self.addCleanup(patch_.stop)
        # A pool job's budget is one: one recorded attempt spends it.
        self.store.record_attempt(self.job["id"], location_id=f"studio-hub-{TRIED}",
                                  code="UPSTREAM_ERROR", detail="the site refused it")
        chosen = worker_module._next_machine(
            self.store, self.job["id"], MODEL_ID, MODALITY, f"studio-hub-{TRIED}",
            code="UPSTREAM_ERROR", detail="again", rejections=rejections,
        )
        self.assertIsNone(chosen, "a spent pool job was routed again")
        self.assertTrue(rejections, "the spent job's walk passed over every site and said nothing")
        self.assertIn(OTHER, {str(row.get("location_id")) for row in rejections},
                      f"the other site left no row: {rejections}")
        self.assertTrue(all(row.get("detail") for row in rejections),
                        f"a pool rejection with no detail is not a reason: {rejections}")


if __name__ == "__main__":
    unittest.main()
