#!/usr/bin/env python3
"""Music request fields, alias normalization, deadline allowances, and lyrics preflight tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sets the isolated test profile (GATEWAY_APP_SUPPORT, GATEWAY_DATA, STUDIOHUB_READY_FILE, etc.)
from test_studio_hub_contract import (  # noqa: E402
    HUB,
    _Hub,
    _HubCase,
    BackendRequest,
    hub_module,
)
from test_seam_20261001 import (  # noqa: E402
    HUB_DEF,
    STABLE_AUDIO,
    YUE2,
)

from gateway import api, catalog  # noqa: E402
from gateway.adapters.studio_hub import (  # noqa: E402
    YUE2_MODEL_ID,
    _yue2_length_deadline,
    lane_deadline_seconds,
)
from gateway.api import GatewayError  # noqa: E402


class MusicHubWireAndAliasTests(_HubCase):
    """Verify alias normalization and canonical field precedence on the Hub wire."""

    def test_yue2_language_is_forwarded_canonically_for_hub_style_prompt(self) -> None:
        hub = _Hub()
        req = self.request(
            modality="music", model_id=YUE2_MODEL_ID,
            params={"prompt": "chill beat", "lyrics": "[Verse]\nHello", "vocal_language": "en", "language": "km"},
        )
        self.generate(hub, req)
        wire_params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(wire_params["lyrics"], "[Verse]\nHello")
        self.assertEqual(wire_params.get("vocal_language"), "en")
        self.assertNotIn("language", wire_params)

    def test_yue2_nested_language_is_forwarded_canonically(self) -> None:
        hub = _Hub()
        req = self.request(
            modality="music", model_id=YUE2_MODEL_ID,
            params={"prompt": "chill beat", "params": {"vocal_language": "en", "language": "km", "lyrics": "[Verse]\nHello"}},
        )
        self.generate(hub, req)
        wire_params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(wire_params["lyrics"], "[Verse]\nHello")
        self.assertEqual(wire_params.get("vocal_language"), "en")
        self.assertNotIn("language", wire_params)

    def test_music_duration_alias_maps_to_duration_seconds_on_hub_wire(self) -> None:
        hub = _Hub()
        req = self.request(
            modality="music",
            params={"prompt": "epic rock", "duration": 120},
        )
        self.generate(hub, req)
        wire_params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(wire_params.get("duration_seconds"), 120)
        self.assertNotIn("duration", wire_params)

    def test_music_canonical_duration_seconds_wins_over_duration_alias(self) -> None:
        hub = _Hub()
        req = self.request(
            modality="music",
            params={"prompt": "epic rock", "duration_seconds": 90, "duration": 120},
        )
        self.generate(hub, req)
        wire_params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(wire_params.get("duration_seconds"), 90)
        self.assertNotIn("duration", wire_params)

    def test_music_nested_params_aliases_mapped_on_hub_wire(self) -> None:
        hub = _Hub()
        req = self.request(
            modality="music",
            model_id=YUE2_MODEL_ID,
            params={"prompt": "jazz song", "params": {"duration": 120, "language": "km", "vocal_language": "en"}},
        )
        self.generate(hub, req)
        wire_params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(wire_params.get("duration_seconds"), 120)
        self.assertEqual(wire_params.get("vocal_language"), "en")
        self.assertNotIn("duration", wire_params)
        self.assertNotIn("language", wire_params)


class MusicLanguageCapabilitiesTests(unittest.TestCase):
    def test_language_options_follow_the_hub_parameter_enum(self) -> None:
        contract = {"parameters": [{"name": "vocal_language", "type": "string",
                                     "enum": ["fr", "en", "de", 7], "default": "en"}]}
        row = catalog._music_row("m", HUB_DEF, {"raw": {"lane_contract": contract}})
        self.assertEqual(row["controls"], [{"name": "vocal_language", "type": "string",
                                            "enum": ["fr", "en", "de"], "default": "en"}])

    def test_unstated_or_empty_options_publish_no_language_control(self) -> None:
        for contract in (YUE2, STABLE_AUDIO, {"parameters": [{"name": "vocal_language", "enum": []}]}):
            with self.subTest(contract=contract):
                row = catalog._music_row("m", HUB_DEF, {"raw": {"lane_contract": contract}})
                self.assertNotIn("controls", row)


class MusicDeadlineAllowanceTests(unittest.TestCase):
    """Verify deadline calculations for YuE2 including alias resolution and 900s floor."""

    def test_yue2_deadline_with_duration_seconds(self) -> None:
        req = BackendRequest(
            provider_id="studio-hub-test",
            model_id=YUE2_MODEL_ID,
            modality="music",
            params={"prompt": "test", "duration_seconds": 120},
            values={"hub_token": "token"},
            base_url=HUB,
        )
        self.assertEqual(_yue2_length_deadline(req), 1944.0)

    def test_yue2_deadline_with_duration_alias(self) -> None:
        req = BackendRequest(
            provider_id="studio-hub-test",
            model_id=YUE2_MODEL_ID,
            modality="music",
            params={"prompt": "test", "duration": 120},
            values={"hub_token": "token"},
            base_url=HUB,
        )
        self.assertEqual(_yue2_length_deadline(req), 1944.0)

    def test_yue2_deadline_with_nested_duration(self) -> None:
        req = BackendRequest(
            provider_id="studio-hub-test",
            model_id=YUE2_MODEL_ID,
            modality="music",
            params={"prompt": "test", "params": {"duration": 120}},
            values={"hub_token": "token"},
            base_url=HUB,
        )
        self.assertEqual(_yue2_length_deadline(req), 1944.0)

    def test_yue2_deadline_canonical_wins_over_alias(self) -> None:
        req = BackendRequest(
            provider_id="studio-hub-test",
            model_id=YUE2_MODEL_ID,
            modality="music",
            params={"prompt": "test", "duration_seconds": 90, "duration": 120},
            values={"hub_token": "token"},
            base_url=HUB,
        )
        # 2.0 * (7.1 * 90 + 120.0) = 1518.0
        self.assertEqual(_yue2_length_deadline(req), 1518.0)

    def test_yue2_default_duration_has_900s_floor_in_lane_deadline(self) -> None:
        # Default song with omitted duration evaluates helper to 240.0s
        req = BackendRequest(
            provider_id="studio-hub-test",
            model_id=YUE2_MODEL_ID,
            modality="music",
            params={"prompt": "test"},
            values={"hub_token": "token"},
            base_url=HUB,
        )
        self.assertEqual(_yue2_length_deadline(req), 240.0)
        # But lane_deadline_seconds has a 900.0s floor (JOB_DEADLINE_S), so the 240s
        # helper does NOT cause a premature timeout for 36s tracks
        mock_model = {"model_id": YUE2_MODEL_ID, "raw": {"lane_contract": {"deadline_seconds": 900}}}
        from gateway.providers import registry
        with patch.object(registry, "models", create=True) as mock_registry_models:
            mock_registry_models.models_for.return_value = [mock_model]
            deadline = lane_deadline_seconds(req)
            self.assertEqual(deadline, 900.0)


class MusicApiSubmitLyricsPreflightTests(unittest.TestCase):
    """Verify api._submit lyrics validation through the real API path."""

    @staticmethod
    def _resolve(model_id: str, contract: dict):
        return patch.object(
            api.catalog,
            "resolve",
            return_value=(HUB_DEF, {"model_id": model_id, "raw": {"lane_contract": contract}}),
        )

    def test_music_missing_lyrics_refused_before_store_submit(self) -> None:
        """Missing lyrics on a requires_lyrics model is refused 422 before store.submit."""
        with self._resolve("vanch007/mlx-Yue2-3B", YUE2):
            with patch.object(api.store, "submit", MagicMock(return_value=({"id": "job-test"}, True))) as mock_submit:
                with self.assertRaises(GatewayError) as cm:
                    api._submit("music", "vanch007/mlx-Yue2-3B", {"prompt": "test"}, None)
                self.assertEqual(cm.exception.status, 422)
                self.assertEqual(cm.exception.code, "LYRICS_REQUIRED")
                self.assertFalse(mock_submit.called)

    def test_music_empty_whitespace_lyrics_refused_before_store_submit(self) -> None:
        with self._resolve("vanch007/mlx-Yue2-3B", YUE2):
            with patch.object(api.store, "submit", MagicMock(return_value=({"id": "job-test"}, True))) as mock_submit:
                with self.assertRaises(GatewayError) as cm:
                    api._submit("music", "vanch007/mlx-Yue2-3B", {"prompt": "test", "lyrics": "   \n\t  "}, None)
                self.assertEqual(cm.exception.status, 422)
                self.assertEqual(cm.exception.code, "LYRICS_REQUIRED")
                self.assertFalse(mock_submit.called)

    def test_music_valid_lyrics_accepted_and_calls_store_submit(self) -> None:
        with self._resolve("vanch007/mlx-Yue2-3B", YUE2):
            with patch.object(api.store, "submit", MagicMock(return_value=({"id": "job-test"}, True))) as mock_submit:
                res, is_new = api._submit(
                    "music", "vanch007/mlx-Yue2-3B", {"prompt": "test", "lyrics": "[Verse]\nHello world"}, None
                )
                self.assertTrue(mock_submit.called)
                self.assertEqual(res, {"id": "job-test"})
                self.assertTrue(is_new)

    def test_music_valid_nested_lyrics_accepted_and_calls_store_submit(self) -> None:
        with self._resolve("vanch007/mlx-Yue2-3B", YUE2):
            with patch.object(api.store, "submit", MagicMock(return_value=({"id": "job-test"}, True))) as mock_submit:
                res, is_new = api._submit(
                    "music",
                    "vanch007/mlx-Yue2-3B",
                    {"prompt": "test", "params": {"lyrics": "[Verse]\nNested lyrics"}},
                    None,
                )
                self.assertTrue(mock_submit.called)
                self.assertEqual(res, {"id": "job-test"})

    def test_music_model_without_requires_lyrics_allows_omitted_lyrics(self) -> None:
        with self._resolve("stable-audio", STABLE_AUDIO):
            with patch.object(api.store, "submit", MagicMock(return_value=({"id": "job-test"}, True))) as mock_submit:
                res, is_new = api._submit("music", "stable-audio", {"prompt": "instrumental beat"}, None)
                self.assertTrue(mock_submit.called)
                self.assertEqual(res, {"id": "job-test"})


if __name__ == "__main__":
    unittest.main()
