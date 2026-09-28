"""Adding a location must not replace a different one.

Studio Hub answers `site_id: "local-site"` for any Hub that was never assigned a
site id (`components/hub/backend/control_plane.py::defaults`). The gateway used to
identify a controller by that value alone, so the second controller added matched
the first row and overwrote it — "Add a location" appeared to replace an existing
site. The per-machine `controller_id` is the fallback identity.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import fleet  # noqa: E402


class StableIdentityTests(unittest.TestCase):
    def test_a_real_site_id_is_the_identity(self) -> None:
        self.assertEqual(
            fleet._stable_identity({"site_id": "site-a", "controller_id": "x-hub"}), "site-a"
        )

    def test_the_shared_default_site_id_is_not_an_identity(self) -> None:
        self.assertEqual(
            fleet._stable_identity({"site_id": "local-site", "controller_id": "alpha-hub"}),
            "alpha-hub",
        )

    def test_controller_id_is_the_fallback(self) -> None:
        self.assertEqual(fleet._stable_identity({"controller_id": "x-hub"}), "x-hub")
        self.assertEqual(fleet._stable_identity({}), "")


class AddControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows: list[dict] = []
        self.tokens: dict[str, str] = {}
        self.responses = {
            "http://10.0.0.1:47873": {
                "reachable": True, "site_id": "local-site", "controller_id": "alpha-hub",
            },
            "http://10.0.0.2:47873": {
                "reachable": True, "site_id": "local-site", "controller_id": "beta-hub",
            },
        }
        self.patches = patch.multiple(
            fleet,
            _probe=lambda url, token: self.responses.get(url, {}),
            _read=lambda: self.rows,
            _write=lambda rows: None,
            _set_token=lambda cid, tok: self.tokens.__setitem__(cid, tok),
            _token_for=lambda cid: self.tokens.get(cid, ""),
            invalidate=lambda: None,
        )
        self.patches.start()

    def tearDown(self) -> None:
        self.patches.stop()

    def test_two_hubs_with_the_default_site_id_stay_two_locations(self) -> None:
        # Before the fix the second add matched the first row's `local-site` and
        # replaced it, so the roster never grew past one.
        fleet.add_controller("A", "http://10.0.0.1:47873", "tok")
        fleet.add_controller("B", "http://10.0.0.2:47873", "tok")
        self.assertEqual(len(self.rows), 2, self.rows)
        self.assertEqual({row["site_id"] for row in self.rows}, {"alpha-hub", "beta-hub"})

    def test_re_adding_the_same_controller_updates_it(self) -> None:
        fleet.add_controller("A", "http://10.0.0.1:47873", "tok")
        fleet.add_controller("A renamed", "http://10.0.0.1:47873", "tok2")
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.rows[0]["label"], "A renamed")


if __name__ == "__main__":
    unittest.main()
