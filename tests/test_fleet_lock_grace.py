#!/usr/bin/env python3
"""A slow read does not withdraw a whole site's models, and recovery is immediate.

**Measured live on terranash-1600, 2026-10-30.** The site answered, then missed one 6 s
overview read while it was busy, and every model it offers flipped `locked: true` — then
back — between consecutive catalogue reads seconds apart:

    14:28:15  1600 reachable=False (slow)   model locked=False
    14:28:24  1600 reachable=False (slow)   model locked=True
    14:28:28  1600 reachable=True           model locked=True   <- the 20 s catalogue TTL
    14:28:46  1600 reachable=True           model locked=False

Two facts are pinned here:

1. **A site that answered within the grace keeps offering its models** (`fleet.
   LOCK_ANSWER_GRACE_S`); one that has not answered past it is locked exactly as before.
   The grace is the last *measured* answer (`answered_at`) — never an invented one — and
   only the catalogue's offer/lock decision reads it. Routing and admission still read the
   live `reachable`, so no work is sent to a site that did not answer.
2. **Which locations are answering is part of the catalogue's own signature**, so the
   lock/unlock follows the fleet answer within a read rather than waiting out the 20 s TTL.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-fleet-lock-grace-")

from gateway import catalog, fleet  # noqa: E402
from gateway.providers.definitions import ProviderDefinition  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB  # noqa: E402

SITE = "2c7a2403564b"


def _site(**overrides) -> dict:
    row = {
        "id": SITE,
        "provider_id": f"studio-hub-{SITE}",
        "label": "Terranash-1600",
        "reachable": True,
        "slow": False,
        "answered_at": time.time(),
        "role": "controller",
        "drained": False,
        "capacity": {"accepting_customer_jobs": True},
        "paused_at": None,
    }
    row.update(overrides)
    return row


def _answering(*rows: dict) -> set[str]:
    with patch.object(fleet, "snapshot", lambda **kw: {"locations": list(rows)}):
        return fleet.usable_location_ids()


class TheCatalogueDoesNotFlipOnOneMissedReadTest(unittest.TestCase):
    def test_a_reachable_site_is_answering(self) -> None:
        self.assertEqual({SITE}, _answering(_site()))

    def test_a_slow_read_inside_the_grace_keeps_offering_its_models(self) -> None:
        row = _site(reachable=False, slow=True, answered_at=time.time() - 10)
        self.assertEqual({SITE}, _answering(row),
                         "a busy site that missed one 6 s read is not offline")

    def test_a_site_silent_past_the_grace_is_locked_as_before(self) -> None:
        row = _site(reachable=False, slow=True,
                    answered_at=time.time() - fleet.LOCK_ANSWER_GRACE_S - 5)
        self.assertEqual(set(), _answering(row))

    def test_a_site_that_never_answered_is_locked(self) -> None:
        row = _site(reachable=False, slow=True, answered_at=None)
        self.assertEqual(set(), _answering(row))

    def test_a_refused_site_is_not_rescued_by_an_older_answer(self) -> None:
        # Only a timeout (`slow`) is a blip. A refusal or a dropped connection is its own fault
        # and has its own sentence; it does not inherit a grace it never had.
        row = _site(reachable=False, slow=False, answered_at=time.time() - 1,
                    error_kind="refused")
        self.assertEqual(set(), _answering(row))

    def test_a_site_that_cannot_take_customer_work_stays_out_whatever_its_answer(self) -> None:
        self.assertEqual(set(), _answering(_site(role="agent")))
        self.assertEqual(set(), _answering(_site(drained=True)))
        self.assertEqual(set(), _answering(_site(capacity={"accepting_customer_jobs": False})))
        self.assertEqual(set(), _answering(_site(paused_at=time.time())))


def _definition() -> ProviderDefinition:
    return ProviderDefinition(
        id=f"studio-hub-{SITE}", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=("chat",),
        discovery="static",
    )


class TheRowFollowsTheSameAnswerTest(unittest.TestCase):
    def _row(self, usable: set[str]) -> dict:
        return catalog.catalog._story_row(
            "chat", _definition(),
            {"model_id": "mlx-community/gemma-4-26B-A4B-it-qat-4bit",
             "display_name": "gemma", "raw": {}},
            usable=usable,
        )

    def test_a_slow_site_inside_the_grace_publishes_an_available_row(self) -> None:
        row = self._row({SITE})
        self.assertIs(row["locked"], False)
        self.assertEqual(row["availability"], "available")
        self.assertIs(row["available"], True)
        self.assertIsNone(row["lock_reason"])

    def test_a_site_silent_past_the_grace_publishes_the_fleet_reason(self) -> None:
        row = self._row(set())
        self.assertIs(row["locked"], True)
        self.assertEqual(row["availability"], "unavailable")
        self.assertIn("No Mac able to run this model is available right now", row["lock_reason"])


class TheSignatureSeesTheFleetAnswerTest(unittest.TestCase):
    def test_a_changed_answering_set_changes_the_catalogue_fingerprint(self) -> None:
        with patch.object(fleet, "usable_location_ids", lambda: {SITE}):
            up = catalog.Catalog()._current_signature()
        with patch.object(fleet, "usable_location_ids", lambda: set()):
            down = catalog.Catalog()._current_signature()
        self.assertNotEqual(up, down, "the catalogue must rebuild as soon as the fleet answer moves")

    def test_the_fingerprint_carries_the_answering_set_itself(self) -> None:
        with patch.object(fleet, "usable_location_ids", lambda: {SITE}):
            self.assertEqual((SITE,), catalog.Catalog()._current_signature()[1])


if __name__ == "__main__":
    unittest.main()
