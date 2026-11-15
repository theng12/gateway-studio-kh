#!/usr/bin/env python3
"""The old Story Studio update/monitoring feature is gone (owner, 2026-11-15).

The owner, looking at the installed 0.2.256 Fleet: *"the gateway studio kh still shows the old
story studio update feature. it should be removed cleanly."* Removed: the Fleet **Stories** tab,
its pane and renderer, the `/admin/api/story/instances` route, the tab's search/filter/view copy.

**Kept, deliberately, because it is not that feature:** `/v1/apps/story/checkin` and
`gateway/story_instances.py` still receive a Story Mac's check-in, because the Content Packs
"remove from every Story app" report is built from the same data. Gateway signs and serves no
app release (owner rule 2026-11-13); that part was already gone.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-story-update-removed-")

from gateway import admin, api  # noqa: E402
from gateway import story_instances  # noqa: E402

PAGE = (ROOT / "gateway" / "admin_ui.html").read_text(encoding="utf-8")


class StoryUpdateRemovedTests(unittest.TestCase):
    def test_the_admin_monitoring_route_is_gone(self) -> None:
        paths = [getattr(route, "path", "") for route in api.app.router.routes]
        self.assertNotIn("/admin/api/story/instances", paths)
        self.assertFalse(hasattr(admin, "story_instances"))

    def test_no_tab_pane_renderer_or_copy_remains(self) -> None:
        for marker in (
            'fleet-pane-story', 'fleet-story-wrap', 'fleet-story-status', 'fleet-story-note',
            'loadFleetStory', 'renderFleetStory', 'fleetStoryRows',
            "['story', 'Story Studio']", "story: 'list'",
        ):
            self.assertNotIn(marker, PAGE, f"{marker} is a leftover of the removed feature")

    def test_the_fleet_tabs_are_the_monitoring_ones(self) -> None:
        for label in ("Groups", "Locations", "Machines", "Models", "Jobs"):
            self.assertIn(f"'{label}'", PAGE)
        self.assertNotIn("Story Studio", PAGE.split("FLEET_TABS")[1].split("]")[0])

    def test_the_checkin_that_content_packs_use_is_kept(self) -> None:
        paths = [getattr(route, "path", "") for route in api.app.router.routes]
        self.assertIn("/v1/apps/story/checkin", paths,
                      "the check-in also carries the Content Pack removal report")
        self.assertTrue(hasattr(story_instances, "pack_removal_report"))


if __name__ == "__main__":
    unittest.main()
