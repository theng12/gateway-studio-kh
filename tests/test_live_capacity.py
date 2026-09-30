#!/usr/bin/env python3
"""C3 — capacity is derived, never configured (the owner's decision D).

> *"i want story to be able to run what the fleet can actually do. setting a number isn't correct.
> because i may have 50 machines now, but i'll have 30 if i sell 20 of them. and i may have 100
> machines later if i buy another 50. so it must be a number that gateway has and offer to story
> directly. and it must by dynamic of reality."*

The defect (audit H7): the number published to Story Studio as its concurrency was the **settings
pool** — live `60` — while the fleet could do about **4**. Two numbers, and the one Story acted on
was the one nobody measured.

The rule these tests hold: the published number is the **sum, over reachable locations, of the
budget admission itself enforces** — one source, so the figure Story is given and the figure that
bounds dispatch cannot disagree.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import concurrency  # noqa: E402
from gateway import constants  # noqa: E402


def _site(site_id: str, *, machines: int, reachable: bool = True) -> dict:
    operations = {}
    for operation in ("chat.completion", "image.text_to_image", "video.generation",
                      "voice.tts", "audio.transcription", "music.generation"):
        operations[operation] = {
            "available_physical_machine_slots": machines,
            "eligible_physical_machine_slots_total": machines,
        }
    return {
        "provider_id": f"studio-hub-{site_id}",
        "reachable": reachable,
        "capacity": {"available_physical_machine_slots": machines,
                     "eligible_physical_machine_slots_total": machines,
                     "by_operation": operations},
    }


class ThePublishedNumberIsTheFleetsTest(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def _admission(self, rows: list[dict]) -> dict:
        with patch("gateway.fleet.snapshot", lambda **_: {"locations": rows}):
            return constants.admission()

    def test_the_number_is_the_sum_over_reachable_locations(self) -> None:
        # Three sites of two Macs each, six modalities: 3 x 2 x 6 = 36.
        rows = [_site("a", machines=2), _site("b", machines=2), _site("c", machines=2)]
        published = self._admission(rows)
        self.assertEqual(published["paid_concurrency_limit"], 36)
        self.assertEqual(published["concurrency_by_modality"]["image"], 6)
        self.assertIn("live fleet", published["concurrency_source"])

    def test_selling_two_macs_lowers_the_number_with_no_settings_edit(self) -> None:
        # **The owner's own test.** Nothing on disk changes between the two readings.
        before = self._admission([_site("a", machines=2), _site("b", machines=2)])
        after = self._admission([_site("a", machines=1), _site("b", machines=1)])
        self.assertGreater(before["paid_concurrency_limit"], after["paid_concurrency_limit"])
        self.assertEqual(before["paid_concurrency_limit"], 24)
        self.assertEqual(after["paid_concurrency_limit"], 12)

    def test_buying_three_macs_raises_it_with_no_settings_edit(self) -> None:
        before = self._admission([_site("a", machines=1)])
        after = self._admission([_site("a", machines=4)])
        self.assertEqual(before["paid_concurrency_limit"], 6)
        self.assertEqual(after["paid_concurrency_limit"], 24)

    def test_an_unreachable_cluster_contributes_nothing(self) -> None:
        # A house whose power is out is not capacity, and it must not be advertised as such.
        live = self._admission([_site("a", machines=3), _site("b", machines=3)])
        dark = self._admission([_site("a", machines=3), _site("b", machines=3, reachable=False)])
        self.assertLess(dark["paid_concurrency_limit"], live["paid_concurrency_limit"])
        self.assertEqual(dark["paid_concurrency_limit"], 18)

    def test_the_pool_is_published_beside_it_and_is_not_the_answer(self) -> None:
        # Whatever the pool is set to -- here a value guaranteed to differ from the fleet's 6 --
        # the fleet is what is published as the limit, and the pool is named beside it.
        pool = concurrency.Budget(limit=6 + 7, source=concurrency.SOURCE_CURATED, detail="test")
        with patch.object(concurrency, "global_budget", lambda: pool):
            published = self._admission([_site("a", machines=1)])
        self.assertEqual(published["paid_concurrency_limit"], 6,
                         "the fleet is the answer, not the pool")
        self.assertEqual(published["concurrency_pool"], pool.limit,
                         "and the pool is named, so the two cannot be confused")

    def test_the_named_pool_is_the_one_dispatch_uses(self) -> None:
        # No number: with the owner's live settings or a clean default, what is published as the
        # pool is exactly what `global_budget()` -- the ceiling admission enforces -- says.
        published = self._admission([_site("a", machines=1)])
        self.assertEqual(published["concurrency_pool"], concurrency.global_budget().limit)

    def test_a_cloud_lane_is_not_counted_as_fleet_capacity(self) -> None:
        # D is about Macs you can sell. A vendor lane's limit is a contract, not a machine.
        rows = [_site("a", machines=1),
                {"provider_id": "openai-direct", "reachable": True, "capacity": {}}]
        published = self._admission(rows)
        self.assertEqual(published["paid_concurrency_limit"], 6)


if __name__ == "__main__":
    unittest.main()
