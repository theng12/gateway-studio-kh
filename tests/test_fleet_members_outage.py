#!/usr/bin/env python3
"""Check 10 — a cluster that is down still has members, and a new head keeps its decisions.

Two findings from the fleet rehearsal, both about what the Gateway can say when a controller is
not answering:

* **A powered-down house looked like an empty one.** The roster only ever came from the live
  probe, so an unreachable location published `nodes: []` and `node_summary: null` — the operator
  could not tell "this site is offline with five Macs" from "this site has no Macs".
* **Re-pointing a location at a promoted node made a second location.** `add_controller` keys on
  the controller's identity, and a promoted Mac has a *different* one, so the replacement row
  inherited nothing: measured in the rehearsal as `model_id: null`, `locations_enabled: null`.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-members-outage-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import fleet  # noqa: E402


class ADownClusterStillNamesItsMembersTest(unittest.TestCase):
    def setUp(self) -> None:
        fleet._last_members.clear()
        self.addCleanup(fleet._last_members.clear)

    def test_a_live_roster_is_remembered_and_served_while_the_site_is_down(self) -> None:
        nodes = [{"id": "mac-a", "name": "A", "state": "reachable"},
                 {"id": "mac-b", "name": "B", "state": "unreachable"}]
        summary = {"total": 2, "reachable": 1, "unreachable": 1, "unauthorized": 0,
                   "error": 0, "removed": 0}
        fleet._remember_members("abc123", nodes, summary)
        stale = fleet._last_known_members("abc123")
        self.assertEqual(stale["node_summary"], summary)
        self.assertEqual([row["id"] for row in stale["nodes"]], ["mac-a", "mac-b"])
        self.assertGreaterEqual(stale["members_stale_seconds"], 0.0)
        self.assertAlmostEqual(stale["members_from"], time.time(), delta=5)

    def test_nothing_is_invented_for_a_site_never_seen_up(self) -> None:
        # The distinction that matters most: "we have never read this site" must not publish a
        # zero, because a zero is a claim about the fleet.
        self.assertEqual(fleet._last_known_members("never-seen"), {})

    def test_an_empty_roster_is_not_remembered_over_a_real_one(self) -> None:
        fleet._remember_members("abc123", [{"id": "mac-a"}], {"total": 1})
        fleet._remember_members("abc123", [], {"total": 0})
        self.assertEqual(fleet._last_known_members("abc123")["node_summary"], {"total": 1},
                         "a controller mid-rebuild must not erase what it last said")


class RepointingKeepsTheRowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fleet.json"
        patcher = patch.object(fleet, "FLEET_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `_read`/`_write` also carry `local_label`; give them a document to work with.
        self.path.write_text('{"controllers": [], "local_label": "This Mac"}', encoding="utf-8")
        self.addCleanup(fleet.invalidate)

    def _add(self, controller_id: str, url: str) -> None:
        rows = fleet._read()
        rows.append({"id": controller_id, "label": "Site", "url": url,
                     "added_at": time.time(), "site_id": "old-site"})
        fleet._write(rows)
        fleet.invalidate()

    def test_repointing_keeps_the_id_and_clears_the_old_identity(self) -> None:
        self._add("abc123", "http://100.64.0.1:47873")
        with patch.object(fleet, "_set_token"):
            row = fleet.replace_address("abc123", "http://100.64.0.9:47873", "tok")
        self.assertEqual(row["id"], "abc123", "the provider id is what enable decisions key on")
        self.assertEqual(row["url"], "http://100.64.0.9:47873")
        stored = next(r for r in fleet._read() if r["id"] == "abc123")
        self.assertNotIn("site_id", stored,
                         "the old controller's identity must not follow to a different Mac")
        self.assertEqual(stored["repointed_from"], "http://100.64.0.1:47873")

    def test_repointing_an_unknown_location_is_none_not_a_new_row(self) -> None:
        self.assertIsNone(fleet.replace_address("nope", "http://100.64.0.9:47873", "tok"))
        self.assertEqual(fleet._read(), [])

    def test_a_public_address_is_refused(self) -> None:
        """H13 — a Fleet token goes to whatever is typed, so the address is checked first."""
        self._add("abc123", "http://100.64.0.1:47873")
        with self.assertRaises(ValueError) as caught:
            fleet.replace_address("abc123", "http://93.184.216.34:47873", "tok")
        self.assertIn("public address", str(caught.exception))

    def test_a_name_that_resolves_to_a_public_address_is_refused_too(self) -> None:
        # A hostname is allowed for MagicDNS names, but only when *every* address it resolves to
        # is private — otherwise a name is a way round the literal check.
        self._add("abc123", "http://100.64.0.1:47873")
        with self.assertRaises(ValueError):
            fleet.replace_address("abc123", "http://example.com:80", "tok")

    def test_a_name_that_does_not_resolve_is_refused_rather_than_taken_on_faith(self) -> None:
        self._add("abc123", "http://100.64.0.1:47873")
        with self.assertRaises(ValueError) as caught:
            fleet.replace_address("abc123", "http://no-such-host.invalid:47873", "tok")
        self.assertIn("could not be resolved", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
