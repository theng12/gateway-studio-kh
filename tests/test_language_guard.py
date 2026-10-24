"""A voice job in a language its model does not publish is refused before it is queued.

Owner, 2026-10-24: Khmer channels moved onto Breeze TTS (English/Chinese only) babbled and failed
their section-quality check four times each, wasting the fleet's time.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-language-guard-")

from gateway import api, language_guard  # noqa: E402
from gateway.api import GatewayError  # noqa: E402

KHMER = "មុនពេលមានស្រែចម្ការ មនុស្សរស់នៅដោយបរបាញ់ និងប្រមូលអាហារពីធម្មជាតិ។"
ENGLISH = "Clement walked back to the hearth table to finish the last few drops of his tea."
BREEZE = {"model_id": "mlx-community/Breeze-TTS-2-mlx-4bit", "display_name": "Breeze TTS 2 4-bit", "raw": {"languages": ["en", "zh"]}}
VOXCPM = {"model_id": "mlx-community/VoxCPM2-4bit", "display_name": "VoxCPM2 4-bit", "raw": {"languages": ["en", "km", "th", "zh"]}}
UNSTATED = {"model_id": "fish", "display_name": "Fish S2", "raw": {"languages": []}}


class ScriptTests(unittest.TestCase):
    def test_scripts_are_recognised(self) -> None:
        self.assertEqual(language_guard.dominant_script(KHMER)[0], "Khmer")
        self.assertEqual(language_guard.dominant_script("สวัสดีครับ")[0], "Thai")
        self.assertIsNone(language_guard.dominant_script(ENGLISH), "Latin is never judged by script")
        # A Khmer line with an English name in it is still Khmer.
        self.assertEqual(language_guard.dominant_script(KHMER + " Komsan Studio")[0], "Khmer")
        # An English script quoting one Khmer word is not Khmer.
        self.assertIsNone(language_guard.dominant_script(ENGLISH * 3 + " ស្រែ"))


class SubmitGuardTests(unittest.TestCase):
    def guard(self, model: dict, **params) -> None:
        with patch.object(api.catalog, "rows", return_value=[
            {"id": "studio-hub::mlx-community/VoxCPM2-4bit", "display_name": "VoxCPM2 4-bit", "supported_languages": ["en", "km"]},
            {"id": "studio-hub::mlx-community/Breeze-TTS-2-mlx-4bit", "display_name": "Breeze TTS 2 4-bit", "supported_languages": ["en", "zh"]},
        ]):
            api._refuse_an_unspoken_language("studio-hub::" + model["model_id"], model, params)

    def test_khmer_on_breeze_is_refused_and_says_where_to_go(self) -> None:
        with self.assertRaises(GatewayError) as caught:
            self.guard(BREEZE, text=KHMER)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "LANGUAGE_UNSUPPORTED")
        message = str(caught.exception.detail)
        self.assertIn("Khmer", message)
        self.assertIn("en, zh", message)
        self.assertIn("VoxCPM2 4-bit", message, "it names a model that can")
        self.assertNotIn("Breeze TTS 2 4-bit.", message.split("list it:")[-1])

    def test_a_stated_language_the_model_lacks_is_refused(self) -> None:
        with self.assertRaises(GatewayError):
            self.guard(BREEZE, text=ENGLISH, language="km-KH")

    def test_what_the_model_can_speak_passes(self) -> None:
        self.guard(VOXCPM, text=KHMER)
        self.guard(VOXCPM, text=KHMER, language="km")
        self.guard(BREEZE, text=ENGLISH)
        self.guard(BREEZE, text=ENGLISH, language="en-US")

    def test_a_model_that_states_no_languages_is_never_refused(self) -> None:
        self.guard(UNSTATED, text=KHMER)

    def test_the_quote_warns_before_anything_is_submitted(self) -> None:
        definition = type("D", (), {"id": "studio-hub-local"})()
        with patch.object(api.catalog, "resolve", return_value=(definition, BREEZE)), \
             patch.object(api.catalog, "rows", return_value=[]), \
             patch.object(api, "definition_chargeable", return_value=False), \
             patch.object(api, "tts_output_container", return_value="m4a"), \
             patch("gateway.catalog._tts_characters", return_value=(4000, "hub")):
            refused = api.tts_quote({"model_id": "studio-hub::x", "text": KHMER})
            fine = api.tts_quote({"model_id": "studio-hub::x", "text": ENGLISH})
        self.assertFalse(refused["generation_enabled"])
        self.assertIn("Khmer", refused["notice"])
        self.assertTrue(fine["generation_enabled"])
        self.assertIsNone(fine["notice"])


if __name__ == "__main__":
    unittest.main()
