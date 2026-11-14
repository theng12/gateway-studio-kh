"""Keep Hub's per-model list geometry from discovery to Story publication."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from tests.test_image_geometry import HUB_DEFINITION, catalog
from gateway.providers import discovery


class ModelGeometryDiscoveryTests(unittest.TestCase):
    def row(self, model_id, sizes, *, package_sizes=None, declared=True):
        package = {"model_id": model_id, "package_id": model_id, "modality": "image"}
        if package_sizes is not None:
            package["image_sizes"] = package_sizes
        facts = {"capabilities": ["txt2img"], "lane_contract": {"parameters": [
            {"name": "aspect_ratio", "enum": ["16:9", "1:1", "9:16"], "default": "16:9"},
            {"name": "resolution", "enum": ["1K"], "default": "1K"},
        ]}}
        if declared:
            facts["image_sizes"] = sizes
        with patch.object(discovery.httpx, "get", return_value=httpx.Response(200, json={"packages": [package]})), \
                patch.object(discovery, "_hub_catalog", return_value={model_id: facts}), \
                patch.object(discovery, "_hub_controls", return_value={}):
            result = discovery._studio_hub(HUB_DEFINITION, {"base_url": "http://mock.invalid"})
        self.assertIsNone(result.error)
        raw = result.models[0].raw
        return raw, catalog._image_row(model_id, HUB_DEFINITION, {"model_id": model_id, "raw": raw})

    @staticmethod
    def sizes(width, height):
        return [{"id": "16:9", "width": width, "height": height},
                {"id": "1:1", "width": 1024, "height": 1024},
                {"id": "9:16", "width": height, "height": width}]

    def test_per_model_hub_lists_survive_discovery_and_publish_executed_pixels(self):
        # Released Hub222's Qwen mflux and separate mlx-serve presets, not Gateway rules.
        cases = [("mflux-community/qwen-image-2-1-turbo-mflux-q4", 1024, 576),
                 ("mlx-community/Qwen-Image-2.1-mflux-q4", 1024, 576),
                 ("ddalcu/Qwen-Image-2.1-MLX-Serve-4bit", 1280, 720)]
        for model, width, height in cases:
            with self.subTest(model=model):
                sizes = self.sizes(width, height)
                raw, row = self.row(model, sizes)
                self.assertEqual(raw.get("image_sizes"), sizes)
                options = {o["aspect_ratio"]: (o["width"], o["height"]) for o in row["output_options"]}
                self.assertEqual(options["16:9"], (width, height))
                self.assertEqual(options["9:16"], (height, width))
                self.assertEqual({o["resolution"] for o in row["output_options"]}, {"1K"})

    def test_current_catalogue_list_overrides_stale_package_geometry(self):
        sizes = self.sizes(1024, 576)
        for old in ({"16:9": [1280, 720]}, self.sizes(1280, 720)):
            with self.subTest(package_sizes=old):
                raw, row = self.row("model", sizes, package_sizes=old)
                self.assertEqual(raw["image_sizes"], sizes)
                landscape = next(o for o in row["output_options"] if o["aspect_ratio"] == "16:9")
                self.assertEqual((landscape["width"], landscape["height"]), (1024, 576))

    def test_explicit_empty_or_invalid_geometry_never_invents_generic_pixels(self):
        # **No stated pixels means no published choice** (owner rule, 0.2.257): the ratio list
        # and the option list are one offer, so a lane with no usable geometry publishes neither
        # rather than a ratio the picker cannot render.
        for sizes in ([], None, {}, [{"id": "16:9", "width": False, "height": 576}]):
            with self.subTest(sizes=sizes):
                _, row = self.row("model", sizes)
                self.assertEqual(row["output_options"], [])
                self.assertEqual(row["aspect_ratios"], [])
                self.assertIsNone(row["default_aspect_ratio"])

    def test_absent_size_declaration_preserves_legacy_hub_compatibility(self):
        _, row = self.row("legacy-model", None, declared=False)
        landscape = next(o for o in row["output_options"] if o["aspect_ratio"] == "16:9")
        self.assertEqual((landscape["width"], landscape["height"]), (1280, 720))


if __name__ == "__main__":
    unittest.main()
