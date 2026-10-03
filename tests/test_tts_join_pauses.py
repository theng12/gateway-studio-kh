"""Join pauses for Studio Hub narration (`paragraph_pause_sec` / `sentence_pause_sec`, owner 2026-10-03).

The Hub voice lane takes a caller's own pause at a paragraph break and after a sentence (Sleep: 1.0 s
/ 0.3 s). The gateway forwards both to the Hub, publishes them on a voice row only when the lane
declares them, and refuses a value that is not a number of seconds before anything is queued.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_studio_hub_contract import _Hub, _HubCase  # noqa: E402  (sets the isolated test profile)
from test_seam_20261001 import CLOUD_DEF, HUB_DEF, _contract  # noqa: E402

from gateway import api, catalog  # noqa: E402
from gateway.api import GatewayError  # noqa: E402

PARAGRAPH = {"name": "paragraph_pause_sec", "type": "number", "minimum": 0.0, "maximum": 3.0,
             "default": 0.6, "request_only": True}
SENTENCE = {"name": "sentence_pause_sec", "type": "number", "minimum": 0.0, "maximum": 3.0,
            "default": 0.3, "request_only": True}


class PublishedOnTheVoiceRowTests(unittest.TestCase):
    def row(self, definition, contract):
        raw = {"lane_contract": contract} if contract is not None else {}
        return catalog._tts_row("m", definition, {"raw": raw})

    def test_a_lane_that_declares_them_publishes_its_own_bounds_and_defaults(self) -> None:
        row = self.row(HUB_DEF, _contract(PARAGRAPH, SENTENCE))
        self.assertEqual(row["pause_parameters"], [
            {"name": "paragraph_pause_sec", "minimum": 0.0, "maximum": 3.0, "default": 0.6},
            {"name": "sentence_pause_sec", "minimum": 0.0, "maximum": 3.0, "default": 0.3},
        ])

    def test_an_older_hub_and_a_cloud_voice_publish_nothing(self) -> None:
        self.assertNotIn("pause_parameters", self.row(HUB_DEF, _contract({"name": "text"})))
        self.assertNotIn("pause_parameters", self.row(HUB_DEF, None))
        self.assertNotIn("pause_parameters", self.row(CLOUD_DEF, _contract(PARAGRAPH, SENTENCE)))


class ForwardedToTheHubTests(_HubCase):
    def test_both_pauses_reach_the_hub_item(self) -> None:
        hub = _Hub()
        self.generate(hub, self.request(modality="tts", params={
            "text": "One.\n\nTwo.", "paragraph_pause_sec": 1.0, "sentence_pause_sec": 0.3}))
        params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual((params["paragraph_pause_sec"], params["sentence_pause_sec"]), (1.0, 0.3))

    def test_absent_pauses_send_nothing(self) -> None:
        hub = _Hub()
        self.generate(hub, self.request(modality="tts", params={"text": "hello"}))
        params = hub.payloads[0]["items"][0]["params"]
        self.assertNotIn("paragraph_pause_sec", params)
        self.assertNotIn("sentence_pause_sec", params)


class ValidationTests(unittest.TestCase):
    def test_numbers_pass_and_null_is_absent(self) -> None:
        payload = {"paragraph_pause_sec": 1, "sentence_pause_sec": None}
        api._validate_tts_pauses(payload)
        self.assertEqual(payload, {"paragraph_pause_sec": 1})

    def test_anything_else_is_refused_by_name(self) -> None:
        for bad in (-0.1, "1.0", True, float("nan"), float("inf")):
            with self.assertRaises(GatewayError) as caught:
                api._validate_tts_pauses({"paragraph_pause_sec": bad})
            self.assertIn("paragraph_pause_sec", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
