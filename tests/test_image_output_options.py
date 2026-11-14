#!/usr/bin/env python3
"""Image ratio/output-option contract — every declared ratio has an option, one default.

The live bug (2026-11-14): `studio-hub::standard-image-fleet` declared
`aspect_ratios` [16:9, 1:1, 9:16] with `default_aspect_ratio` 16:9, while its only
published `output_option` was 1K 1:1 — a scene render that selected the fleet
silently produced square images. These tests pin the two invariants the fix
creates, for one model and for a pool alias:

  * every ratio in `aspect_ratios` has at least one `output_options` row;
  * `default_aspect_ratio` is one of them, and exactly one option is `is_default`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-image-options-")

from gateway import catalog  # noqa: E402
from gateway.providers.definitions import ProviderDefinition  # noqa: E402


def _definition() -> ProviderDefinition:
    return ProviderDefinition(
        id="studio-hub-local", display_name="Studio Hub", category="image",
        kind="studio_hub", base_url="http://127.0.0.1:8787", modalities=("image",),
        discovery="static", data_use="none", free_tier=True,
    )


def _raw(ratios: list[str], sizes: list[tuple[str, int, int]], default: str = "16:9") -> dict:
    return {
        "image_sizes": [
            {"id": ratio, "label": ratio, "width": width, "height": height}
            for ratio, width, height in sizes
        ],
        "lane_contract": {
            "schema": "studiohub.lane-contract.v1",
            "modality": "image",
            "parameters": [
                {"name": "prompt", "type": "string", "required": True},
                {"name": "aspect_ratio", "type": "string", "required": False,
                 "enum": ratios, "default": default},
                {"name": "resolution", "type": "string", "required": False, "default": "1K"},
            ],
        },
    }


def assert_ratio_contract(test: unittest.TestCase, row: dict) -> None:
    """The invariant every image row must hold, pool alias or model."""
    ratios = row.get("aspect_ratios") or []
    options = row.get("output_options") or []
    test.assertTrue(ratios, "a row with geometry must publish its ratios")
    for ratio in ratios:
        test.assertTrue(
            any(option.get("aspect_ratio") == ratio for option in options),
            f"{row.get('id')} declares {ratio} with no output option",
        )
    defaults = [option for option in options if option.get("is_default")]
    test.assertEqual(len(defaults), 1,
                     f"{row.get('id')} must publish exactly one default option")
    test.assertIn(row.get("default_aspect_ratio"), ratios)
    test.assertEqual(defaults[0].get("aspect_ratio"), row.get("default_aspect_ratio"))


class LocalImageShapeTests(unittest.TestCase):
    def test_a_declared_ratio_without_pixels_is_not_published(self) -> None:
        raw = _raw(["16:9", "1:1", "9:16"], [("1:1", 1024, 1024)])
        shape = catalog._local_image_shape(raw)
        self.assertIsNotNone(shape)
        self.assertEqual(shape["aspect_ratios"], ["1:1"])
        self.assertEqual(shape["default_aspect_ratio"], "1:1")

    def test_a_fully_sized_lane_keeps_its_order_and_default(self) -> None:
        raw = _raw(
            ["16:9", "1:1", "9:16"],
            [("16:9", 1280, 720), ("1:1", 1024, 1024), ("9:16", 720, 1280)],
        )
        shape = catalog._local_image_shape(raw)
        self.assertEqual(shape["aspect_ratios"], ["16:9", "1:1", "9:16"])
        self.assertEqual(shape["default_aspect_ratio"], "16:9")

    def test_image_row_has_one_option_per_ratio_and_one_default(self) -> None:
        member = {
            "model_id": "z-image-turbo", "display_name": "Z Image Turbo",
            "raw": _raw(["16:9", "1:1"], [("16:9", 1280, 720), ("1:1", 1024, 1024)]),
        }
        row = catalog._image_row("z-image-turbo", _definition(), member)
        assert_ratio_contract(self, row)


class PoolGeometryTests(unittest.TestCase):
    def _member(self, row_id: str, ratios: list[str], sizes: list[tuple[str, int, int]],
                default: str, option_default: bool = False) -> dict:
        options = [
            {"resolution": "1K", "aspect_ratio": ratio, "width": width, "height": height,
             "is_default": option_default and ratio == default}
            for ratio, width, height in sizes if ratio in ratios
        ]
        return {"id": row_id, "aspect_ratios": ratios,
                "default_aspect_ratio": default, "output_options": options}

    def test_the_live_bug_publishes_only_ratios_it_can_render(self) -> None:
        # The live alias: three ratios declared, one square option, no default marked.
        member = self._member("studio-hub::a", ["16:9", "1:1", "9:16"],
                              [("1:1", 1024, 1024)], "16:9")
        ratios, default, options = catalog._pool_geometry([member])
        self.assertEqual(ratios, ["1:1"])
        self.assertEqual(default, "1:1")
        self.assertEqual([option["is_default"] for option in options], [True])

    def test_standard_image_fleet_publishes_16_9_as_the_default(self) -> None:
        first = self._member("studio-hub::a", ["16:9", "1:1"],
                             [("16:9", 1280, 720), ("1:1", 1024, 1024)], "16:9")
        second = self._member("studio-hub::b", ["16:9", "1:1"],
                              [("16:9", 1024, 576), ("1:1", 1024, 1024)], "16:9")
        ratios, default, options = catalog._pool_geometry([first, second])
        self.assertEqual(ratios, ["16:9", "1:1"])
        self.assertEqual(default, "16:9")
        self.assertIn(("16:9", 1280, 720),
                      {(option["aspect_ratio"], option["width"], option["height"])
                       for option in options})
        self.assertIn(("16:9", 1024, 576),
                      {(option["aspect_ratio"], option["width"], option["height"])
                       for option in options})
        self.assertEqual(sum(1 for option in options if option["is_default"]), 1)
        self.assertTrue(next(option for option in options if option["is_default"])
                        ["aspect_ratio"] == "16:9")

    def test_a_member_with_no_options_does_not_invent_geometry(self) -> None:
        member = self._member("studio-hub::a", ["16:9"], [], "16:9")
        ratios, default, options = catalog._pool_geometry([member])
        self.assertEqual((ratios, default, options), ([], None, []))

    def test_pool_row_holds_the_contract(self) -> None:
        first = self._member("studio-hub::a", ["16:9", "1:1"],
                             [("16:9", 1280, 720), ("1:1", 1024, 1024)], "16:9")
        ratios, default, options = catalog._pool_geometry([first])
        row = {"id": "studio-hub::standard-image-fleet", "aspect_ratios": ratios,
               "default_aspect_ratio": default, "output_options": options}
        assert_ratio_contract(self, row)


if __name__ == "__main__":
    unittest.main()
