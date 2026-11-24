"""The first probe after a quiet spell gets the cap, not the default (0.2.307).

Measured 2026-11-24: locations that answer in 60 ms while awake took 9–14 s on the first probe
after a quiet morning, so the fan-out reported them "unreachable — timeout" and the headline
collapsed to whichever sites happened to be awake. Nothing was wrong with the sites; the bound was
tuned only by the *last successful* probe, which is no help to a Mac that was asleep.

    python -B tests/test_fleet_probe_deadline.py -v
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-probe-deadline-")

from gateway import fleet  # noqa: E402

SITE = "9701220bf30f"


class ProbeDeadlineTest(unittest.TestCase):
    def setUp(self) -> None:
        fleet._probe_seen.clear()
        fleet._probe_durations.clear()

    def test_a_site_never_probed_gets_the_cap(self) -> None:
        self.assertEqual(fleet._probe_timeout(SITE), fleet.PROBE_TIMEOUT_MAX_S)

    def test_a_site_answering_now_keeps_the_default(self) -> None:
        fleet._note_probe(SITE, 60)
        self.assertEqual(fleet._probe_timeout(SITE), fleet.PROBE_TIMEOUT_S)

    def test_a_slow_site_gets_three_times_what_it_took(self) -> None:
        fleet._note_probe(SITE, 5000)
        self.assertEqual(fleet._probe_timeout(SITE), 15.0)

    def test_three_times_a_very_slow_probe_stops_at_the_cap(self) -> None:
        fleet._note_probe(SITE, 9000)
        self.assertEqual(fleet._probe_timeout(SITE), fleet.PROBE_TIMEOUT_MAX_S)

    def test_the_first_probe_after_a_quiet_spell_gets_the_cap(self) -> None:
        fleet._note_probe(SITE, 60)
        later = time.time() + fleet.QUIET_SPELL_S + 1
        with patch.object(fleet.time, "time", lambda: later):
            self.assertEqual(fleet._probe_timeout(SITE), fleet.PROBE_TIMEOUT_MAX_S)

    def test_a_failed_probe_still_counts_as_contact(self) -> None:
        # A refusal is an answer: the site is awake, so the next bound goes back to the default
        # rather than staying at the cap for ever.
        fleet._note_probe(SITE, None)
        self.assertEqual(fleet._probe_timeout(SITE), fleet.PROBE_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
