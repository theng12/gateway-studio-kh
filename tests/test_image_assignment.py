#!/usr/bin/env python3
"""Per-Mac image-pool model (owner, 2026-10-24): Auto by RAM, or the owner's pin per Mac.

The Gateway decides each Mac's model and sends it to the Mac's controller; the Hub stores it, refuses
pool work for other models on that Mac and reports it back. Routing trusts only what the Hub reports.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TURBO, ZIMG, Q4 = "mflux-community/qwen-image-2-1-turbo-mflux-q4", "filipstrand/Z-Image-Turbo-mflux-4bit", "mlx-community/Qwen-Image-2.1-mflux-q4"


def site(machines, local_models):
    return {"id": "c1", "label": "Site 1", "machines": machines, "local_models": local_models}


def mac(machine, gb, reported=None, supports=True):
    row = {"machine": machine, "label": machine, "memory_gb": gb}
    if supports:
        row["assigned_image_model"] = reported
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gw-ia-"))
        self.env = mock.patch.dict(os.environ, {"GATEWAY_DATA": str(self.tmp / "data")})
        self.env.start()
        from gateway import config, image_assignment
        self.config_dir = mock.patch.object(config, "DATA_DIR", self.tmp / "data")
        self.config_dir.start()
        self.ia = image_assignment
        self.tiers = mock.patch.object(image_assignment, "_tier_map", lambda: {"16": TURBO, "8": ZIMG})
        self.tiers.start()

    def tearDown(self):
        self.tiers.stop()
        self.config_dir.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshot(self, rows):
        from gateway import fleet
        return mock.patch.object(fleet, "snapshot", lambda allow_stale=False, force=False: {"locations": rows})


class DesiredModel(Base):
    def test_auto_follows_ram_and_a_pin_overrides_it_for_that_mac_only(self):
        self.assertEqual(self.ia.desired("c1", mac("a", 17.18)), (TURBO, "auto"))
        self.assertEqual(self.ia.desired("c1", mac("b", 8)), (ZIMG, "auto"))
        self.ia.set_override("c1", "a", f"studio-hub::{ZIMG}")
        self.assertEqual(self.ia.desired("c1", mac("a", 17.18)), (ZIMG, "pinned"), "the prefix is stripped")
        self.assertEqual(self.ia.desired("c1", mac("c", 16)), (TURBO, "auto"), "another 16 GB Mac keeps Auto")
        self.ia.set_override("c1", "a", None)
        self.assertEqual(self.ia.desired("c1", mac("a", 17.18)), (TURBO, "auto"), "null returns it to Auto")

    def test_no_memory_or_no_tier_model_means_no_model_with_a_reason(self):
        model, why = self.ia.desired("c1", mac("a", None))
        self.assertIsNone(model)
        self.assertIn("memory", why)
        model, why = self.ia.desired("c1", mac("a", 32))
        self.assertIsNone(model)
        self.assertIn("32 GB", why)

    def test_pool_models_are_tier_models_plus_pins(self):
        pool = {"modality": "image", "tier_models": {"16": f"studio-hub::{TURBO}", "8": ZIMG}}
        self.assertEqual(self.ia.pool_models(pool), [TURBO, ZIMG])
        self.ia.set_override("c1", "a", Q4)
        self.assertEqual(self.ia.pool_models(pool), [TURBO, ZIMG, Q4])

    def test_an_unreadable_pin_file_is_refused_not_treated_as_empty(self):
        path = self.tmp / "data" / "image-model-overrides.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        with self.assertRaises(RuntimeError):
            self.ia.overrides()
        with self.assertRaises(RuntimeError):
            self.ia.set_override("c1", "a", ZIMG)
        self.assertEqual(path.read_text(), "{ not json", "left as it was")


class ViewAndSync(Base):
    def rows(self):
        return [site([mac("a", 17.18, reported=TURBO), mac("b", 8, reported=None), mac("old", 8, supports=False)],
                     [{"machine": "a", "model_id": TURBO}, {"machine": "b", "model_id": ZIMG}])]

    def test_view_reports_auto_installed_and_sync_state(self):
        with self.snapshot(self.rows()):
            view = {m["machine"]: m for m in self.ia.view()}
        self.assertTrue(view["a"]["in_sync"] and view["a"]["installed"])
        self.assertFalse(view["b"]["in_sync"])
        self.assertFalse(view["old"]["hub_supports"], "a Hub that predates the setting says so")

    def test_sync_does_not_clear_a_mac_whose_memory_was_not_read(self):
        row = {"controller_id": "c", "machine": "m", "model": None, "reason": "its memory size is not reported",
               "tier": None, "in_sync": False, "hub_supports": True}
        with mock.patch.object(self.ia, "view", return_value=[row]), \
                mock.patch("gateway.startup_control.set_node_image_model") as send:
            self.assertEqual(self.ia.sync(), [])
        send.assert_not_called()

    def test_sync_sends_only_the_macs_whose_hub_reports_something_else(self):
        sent = []
        from gateway import startup_control
        with self.snapshot(self.rows()), mock.patch.object(
                startup_control, "set_node_image_model", lambda c, m, model: sent.append((c, m, model))):
            self.ia.sync()
        self.assertEqual(sent, [("c1", "b", ZIMG)], "in-sync and too-old Macs are not sent anything")

    def test_one_unreachable_mac_does_not_stop_the_others(self):
        rows = [site([mac("a", 16, reported=None), mac("b", 8, reported=None)], [])]
        sent = []

        def flaky(c, m, model):
            if m == "a":
                raise RuntimeError("offline")
            sent.append(m)
        from gateway import startup_control
        with self.snapshot(rows), mock.patch.object(startup_control, "set_node_image_model", flaky):
            self.ia.sync()
        self.assertEqual(sent, ["b"])


class Routing(Base):
    def candidates(self, rows, members):
        from gateway import fleet

        def cands(model, modality, only_location=None, exclude_locations=None,
                  require_capability=None, **_kw):
            return [{"location_id": "c1", "provider_id": "studio-hub-c1", "label": "Site 1", "queue": 0, "slots": 1}]
        eligible = {TURBO: {"a": {}}, ZIMG: {"b": {}, "a": {}}}
        with self.snapshot(rows), mock.patch.object(fleet, "routing_candidates", cands), \
                mock.patch.object(fleet, "_eligible_macs", lambda row, model, modality: eligible.get(model, {})):
            return fleet.pool_candidates(members, "image")

    def test_a_site_qualifies_for_a_member_only_through_a_mac_assigned_it(self):
        rows = [site([mac("a", 16, reported=TURBO), mac("b", 8, reported=ZIMG)], [])]
        got = self.candidates(rows, [f"studio-hub::{TURBO}", ZIMG])
        self.assertEqual(sorted(c["_member"] for c in got), sorted([TURBO, ZIMG]))

    def test_a_16gb_mac_never_takes_zimage_unless_assigned_it(self):
        rows = [site([mac("a", 16, reported=TURBO)], [])]
        got = self.candidates(rows, [ZIMG])
        self.assertEqual(got, [], "Mac a can run Z-Image but is assigned turbo, so the site sits out for Z-Image")

    def test_a_member_nobody_is_assigned_sits_out(self):
        rows = [site([mac("a", 16, reported=None), mac("b", 8, reported=None)], [])]
        self.assertEqual(self.candidates(rows, [TURBO, ZIMG]), [])

    def test_an_assigned_mac_that_cannot_run_the_model_sits_out(self):
        rows = [site([mac("c", 16, reported=Q4)], [])]
        self.assertEqual(self.candidates(rows, [Q4]), [], "assigned but not eligible (not installed) is not capacity")


class HubRequest(unittest.TestCase):
    def test_only_pool_image_work_is_sent_assigned_routing(self):
        source = (ROOT / "gateway" / "adapters" / "studio_hub.py").read_text()
        block = source[source.index('"label": "gateway-kh",'):][:600]
        self.assertIn('request.modality == "image" and request.params.get("_pool_id")', block)
        self.assertIn('body["routing"] = "assigned"', block)


if __name__ == "__main__":
    unittest.main()
