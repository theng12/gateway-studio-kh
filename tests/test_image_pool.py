#!/usr/bin/env python3
"""Image (two-tier) pool tests — static only; not executed here."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ImagePoolCRUDTests(unittest.TestCase):
    def test_chat_pools_unaffected_by_image_modality(self):
        tmp = Path(tempfile.mkdtemp(prefix="gw-ip-"))
        os.environ.update({
            "GATEWAY_DATA": str(tmp / "data"),
            "GATEWAY_POOLS": str(tmp / "data" / "pools.json"),
            "GATEWAY_APP_SUPPORT": str(tmp / "app"),
            "GATEWAY_NO_MIGRATE": "1",
            "STUDIOHUB_READY_FILE": str(tmp / "nohub.json"),
        })
        from gateway import pools
        pools._cache["key"] = None
        # chat pool
        pools.create("chatp", ["a::one", "b::two"])
        # image pool
        pools.create("imgp", [], modality="image", tier_models={"16": "x::turbo", "8": "z::zimg"},
                     display_name="Standard image (fleet)")
        self.assertIn(pools.get("chatp").get("modality"), (None, "chat"))
        self.assertEqual("image", pools.get("imgp")["modality"])
        self.assertEqual({"16": "x::turbo", "8": "z::zimg"}, pools.tier_models_of(pools.get("imgp")))
        self.assertEqual(["x::turbo", "z::zimg"], pools.get("imgp")["members"])
        # chat pool has no tier_models
        self.assertFalse(pools.tier_models_of(pools.get("chatp")))
        shutil.rmtree(tmp, ignore_errors=True)


class TierCheckTests(unittest.TestCase):
    def test_machine_tier_from_memory(self):
        from gateway import fleet
        self.assertEqual(16, fleet.machine_tier(17.18))
        self.assertEqual(8, fleet.machine_tier(8))
        self.assertIsNone(fleet.machine_tier(None))
        self.assertIsNone(fleet.machine_tier(0))

    def test_pool_member_model_strips_only_the_fleet_prefix(self):
        from gateway import fleet
        self.assertEqual("org/turbo", fleet.pool_member_model("studio-hub::org/turbo"))
        self.assertEqual("org/turbo", fleet.pool_member_model("org/turbo"))
        self.assertEqual("other::x", fleet.pool_member_model("other::x"))


if __name__ == "__main__":
    unittest.main()