#!/usr/bin/env python3
"""The queue says how much capacity a lane offers, and never invents a zero (0.2.295).

Production's ask, in one line: 74 image jobs waiting while 8 Macs sit idle is invisible today, and
it should not be. The figure has to be **the one routing acts on** — computed from the same
functions, not from a second, parallel calculation — or the page and the router disagree the first
time somebody finds a gap between them.

Two rules this file pins, both about not lying:

1. `offered` counts the Macs `fleet._eligible_macs` says may run this lane's models **now**, over the
   same model set `routing_candidates` reads (`registry.models.enabled_models`). A Mac the router
   would skip — memory-ineligible here — is not counted, and the two numbers move together.
2. A site that publishes no per-machine detail is **not reported**, never `offered 0`. A zero that
   means "unknown" is the same defect as a stale failure row.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-queue-offer-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "offer-fixture-key",
})

from gateway import admin, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402
from gateway.runtime import store, worker  # noqa: E402

MEMBER = "z-image-turbo-6b-mlx-4bit"
MODEL_ID = f"studio-hub::{MEMBER}"
MODALITY = "image"
SITE = "loc1"


def _site(*, machines: int, eligible: int, reports_detail: bool = True,
          reachable: bool = True) -> dict:
    """One Hub row: `machines` Macs, the first `eligible` of them memory-eligible for the member."""
    row = {
        "id": SITE,
        "provider_id": f"studio-hub-{SITE}",
        "label": "Site One",
        "reachable": reachable,
        "slow": False,
        "answered_at": 1.0,
        "role": "controller",
        "drained": False,
        "capacity": {"accepting_customer_jobs": True, "queue_depth": 3,
                     "eligible_physical_machine_slots_total": machines,
                     "available_physical_machine_slots": eligible},
        "paused_at": None,
        "machines": [{"machine": f"mac-{i}", "reachable": True} for i in range(machines)],
        "machine_summary": {"reachable": machines, "total": machines},
        "running_jobs": [],
        "model_supply": {MEMBER: {"eligible": machines, "available": eligible}},
    }
    if not reachable:
        row["machines"] = []
        row["machine_summary"] = {"reachable": 0, "total": 0}
        row["model_supply"] = {}
        return row
    if reports_detail:
        row["local_models"] = [
            {"model_id": MEMBER, "machine": f"mac-{i}", "modality": MODALITY,
             "memory_eligible": i < eligible, "runtime_ready": True}
            for i in range(machines)
        ]
    return row


def _definition(site: str = SITE) -> ProviderDefinition:
    return ProviderDefinition(
        id=f"studio-hub-{site}", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=(MODALITY,),
        discovery="static",
    )


def _enabled(requested: str):
    if requested != MODALITY:
        return []
    row = {"model_id": MEMBER, "display_name": "z-image", "modality": MODALITY,
           "enabled": True, "raw": {}}
    return [(_definition(SITE), row), (_definition("loc2"), row), (_definition("loc3"), row)]


class OfferTable(unittest.TestCase):
    def setUp(self) -> None:
        worker._lane_in_flight.clear()
        worker._in_flight.clear()
        # A lane is published when something is waiting or running in it; these tests are about the
        # figure, so the lane is given one running job rather than a queued one.
        worker._lane_in_flight[MODALITY] = 1
        self.addCleanup(worker._lane_in_flight.clear)

    def _queue(self, rows: list[dict]) -> dict:
        with patch.object(fleet, "snapshot", lambda **_kw: {"locations": rows}), \
                patch.object(registry.models, "enabled_models", _enabled):
            return admin.queue_view(limit=10)

    def _lane(self, body: dict) -> dict:
        lane = next((row for row in body["lanes"] if row["modality"] == MODALITY), None)
        self.assertIsNotNone(lane, f"the image lane was not published: {body['lanes']}")
        return lane

    def test_offered_is_the_macs_routing_would_use(self) -> None:
        """Three Macs, one memory-ineligible: the router skips it and so does the offer."""
        worker._lane_in_flight[MODALITY] = 1
        body = self._queue([_site(machines=3, eligible=2)])
        lane = self._lane(body)
        with patch.object(fleet, "snapshot", lambda **_kw: {"locations": [_site(machines=3, eligible=2)]}), \
                patch.object(registry.models, "enabled_models", _enabled):
            candidates = fleet.routing_candidates(MEMBER, MODALITY, respect_model_supply=False)
        self.assertEqual(lane["offered"], 2,
                         "the offer counted a Mac the router would never send work to")
        self.assertEqual(lane["offered"], len(fleet._eligible_macs(_site(machines=3, eligible=2),
                                                                   MEMBER, MODALITY)),
                         "the offer is a second calculation, not the router's own rule")
        self.assertTrue(candidates, "the router found no candidate for a Mac the offer counted")
        self.assertEqual(lane["used"], 1)
        self.assertEqual(lane["idle"], 1, "idle capacity is the number nobody can see today")

    def test_a_hub_without_per_machine_detail_is_not_reported_not_zero(self) -> None:
        body = self._queue([_site(machines=3, eligible=0, reports_detail=False)])
        lane = self._lane(body)
        self.assertIsNone(lane["offered"],
                          "a site that publishes no per-machine detail was read as offered 0")
        self.assertIsNone(lane["idle"], "idle was derived from a figure that does not exist")
        self.assertIn("not reported", str(lane.get("offer_detail") or "").lower(),
                      f"the lane did not say the figure is missing: {lane}")

    def test_the_figure_says_which_sites_it_could_not_count(self) -> None:
        unreported = _site(machines=2, eligible=0, reports_detail=False)
        unreported["id"] = "loc2"
        unreported["provider_id"] = f"studio-hub-loc2"
        unreported["label"] = "Site Two"
        body = self._queue([_site(machines=3, eligible=2), unreported])
        lane = self._lane(body)
        self.assertEqual(lane["offered"], 2)
        detail = str(lane.get("offer_detail") or "")
        self.assertIn("Site Two", detail,
                      f"a partial figure did not name the site it could not count: {detail}")


    def test_an_offline_site_is_not_called_an_old_hub(self) -> None:
        """Two different unknowns, two sentences: found live on 1301, 0.2.295."""
        offline = _site(machines=0, eligible=0, reports_detail=False, reachable=False)
        offline["id"] = "loc3"
        offline["provider_id"] = "studio-hub-loc3"
        offline["label"] = "Site Offline"
        body = self._queue([_site(machines=3, eligible=2), offline])
        lane = self._lane(body)
        self.assertEqual(lane["offered"], 2)
        detail = str(lane.get("offer_detail") or "")
        self.assertIn("Site Offline", detail)
        self.assertIn("not answering", detail,
                      f"an offline site was described as a Hub that cannot report: {detail}")

    def test_a_lane_whose_only_site_is_offline_says_so(self) -> None:
        body = self._queue([_site(machines=0, eligible=0, reports_detail=False, reachable=False)])
        lane = self._lane(body)
        self.assertIsNone(lane["offered"])
        self.assertIn("answering", str(lane.get("offer_detail") or "").lower())


    def test_the_old_capacity_number_is_not_beside_it(self) -> None:
        """One capacity figure: the page renders the offer, and the JSON names what produced it."""
        body = self._queue([_site(machines=3, eligible=2)])
        lane = self._lane(body)
        self.assertIn("offer_source", lane, "the payload does not say what produced the figure")
        self.assertIn("_eligible_macs", str(lane["offer_source"]))


if __name__ == "__main__":
    unittest.main()
