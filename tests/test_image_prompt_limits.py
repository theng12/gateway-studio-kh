from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

# Reuse the existing image suite's isolated profile before importing Gateway readers.
from tests.test_image_geometry import CLOUD_DEFINITION, HUB_DEFINITION, catalog, image_row
from gateway.providers import discovery


class ImagePromptLimitTests(unittest.TestCase):
    def discover(self, facts: dict) -> dict:
        package = {"model_id": "image-model", "package_id": "image-model", "modality": "image"}
        with patch.object(discovery.httpx, "get", return_value=httpx.Response(
            200, json={"packages": [package]},
        )), patch.object(discovery, "_hub_catalog", return_value={"image-model": facts}), \
                patch.object(discovery, "_hub_controls", return_value={}):
            result = discovery._studio_hub(HUB_DEFINITION, {"base_url": "http://mock.invalid"})
        self.assertIsNone(result.error)
        self.assertEqual(len(result.models), 1)
        return result.models[0].raw

    def test_each_hub_model_limit_survives_discovery_and_publication(self) -> None:
        for model, value in (("FLUX 2 Klein", 1900), ("Qwen 2.1", 8000), ("Z-Image", 2000)):
            with self.subTest(model=model), patch.object(catalog.limits, "gap", return_value=12345):
                raw = self.discover({"lane_contract": {"parameters": [
                    {"name": "prompt", "type": "string", "max_characters": value},
                ]}})
                row = image_row(HUB_DEFINITION, raw)
                self.assertEqual(row["max_prompt_characters"], value)
                self.assertEqual(row["max_prompt_characters_source"], "provider")

    def test_explicit_top_level_value_including_zero_takes_precedence(self) -> None:
        for value in (0, 1900, 8000, 2000):
            with self.subTest(value=value), patch.object(catalog.limits, "gap", return_value=12345):
                raw = self.discover({"max_prompt_characters": value, "lane_contract": {
                    "parameters": [{"name": "prompt", "max_characters": 999}],
                }})
                self.assertEqual(raw["max_prompt_characters"], value)
                self.assertEqual(image_row(HUB_DEFINITION, raw)["max_prompt_characters"], value)

    def test_explicit_unknown_or_invalid_declaration_never_becomes_curated(self) -> None:
        for value in (None, False, -1, "8000", 1.5):
            for raw in ({"max_prompt_characters": value, "lane_contract": {
                "parameters": [{"name": "prompt", "max_characters": 8000}],
            }}, {"lane_contract": {"parameters": [
                {"name": "prompt", "max_characters": value},
            ]}}):
                with self.subTest(value=value, raw=raw), patch.object(
                    catalog.limits, "gap", return_value=12345,
                ):
                    row = image_row(HUB_DEFINITION, self.discover(raw))
                    self.assertIsNone(row["max_prompt_characters"])
                    self.assertEqual(row["max_prompt_characters_source"], "provider")

    def test_absent_limit_is_unknown_without_a_curated_value(self) -> None:
        with patch.object(catalog.limits, "gap", return_value=None):
            for definition in (HUB_DEFINITION, CLOUD_DEFINITION):
                for raw in ({}, {"lane_contract": None}, {"lane_contract": "invalid"}):
                    with self.subTest(provider=definition.id, raw=raw):
                        self.assertIsNone(image_row(definition, raw)["max_prompt_characters"])

    def test_curated_limit_only_fills_an_absent_provider_value(self) -> None:
        with patch.object(catalog.limits, "gap", return_value=2500):
            row = image_row(HUB_DEFINITION, {})
        self.assertEqual(row["max_prompt_characters"], 2500)
        self.assertEqual(row["max_prompt_characters_source"], "curated")

    def test_same_model_across_locations_keeps_the_safe_active_minimum(self) -> None:
        first = image_row(HUB_DEFINITION, {"max_prompt_characters": 8000})
        lower = image_row(HUB_DEFINITION, {"max_prompt_characters": 1900})
        catalog._merge_capabilities(first, lower, "image", site_paused=True)
        self.assertEqual(first["max_prompt_characters"], 8000)
        catalog._merge_capabilities(first, lower, "image")
        self.assertEqual(first["max_prompt_characters"], 1900)
        self.assertEqual(first["max_prompt_characters_source"], "provider")


if __name__ == "__main__":
    unittest.main()
