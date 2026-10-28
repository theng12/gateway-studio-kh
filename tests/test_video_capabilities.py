#!/usr/bin/env python3
"""Video lanes: the model's own capability record, and the picture that actually reaches it.

Three defects this pins, all of them live before it:

  * every cloud video model published the same invented row — 5 s and 10 s at `"720p"` in
    `["16:9","9:16","1:1"]` — which Wan 3.0 (2–30 s, 480p/720p/1080p, six ratios) and Seedance 2.5
    (4–30 s, seven ratios) both refuse;
  * Story Studio's uploaded scene image reached **no** cloud adapter: `api._stage_video_reference`
    produced `_reference_path`, and only the Studio Hub adapter read it. fal, Kie, MiniMax and xAI
    each read a field nothing produced, so an image-to-video request animated nothing;
  * a missing image silently became a text-to-video request whose clip looked like a success.

No provider is contacted: every HTTP call is a `MockTransport`, and nothing here spends money.

Run with::

    .venv/bin/python tests/test_video_capabilities.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-video-capabilities-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-video-capabilities-")  # before the first gateway import
os.environ["GATEWAY_DATA"] = str(_TEMP_ROOT)

import httpx  # noqa: E402

from gateway import video_capabilities  # noqa: E402
from gateway.adapters import kie as kie_module  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.adapters.fal import _video_input as fal_video_input  # noqa: E402
from gateway.adapters.kie import _input_for as kie_input, _reference_url as kie_reference_url  # noqa: E402
from gateway.adapters.minimax import _video_body as minimax_video_body  # noqa: E402
from gateway.adapters.xai import _video_body as xai_video_body  # noqa: E402
from gateway.catalog import _video_row  # noqa: E402
from gateway.providers.definitions import PROVIDERS_BY_ID  # noqa: E402

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


def _staged(name: str = "scene.png", content: bytes = _PNG, media_type: str = "image/png") -> Path:
    # **Inside the artifact store**, because that is the only place the Gateway stages a
    # reference and the adapters refuse anything else (review 2026-10-28). A fixture that
    # writes beside the profile was testing a shape the gateway never produces.
    artifacts = Path(os.environ["GATEWAY_ARTIFACTS"])
    artifacts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="staged-", dir=artifacts))
    path = directory / name
    path.write_bytes(content)
    path.touch()
    return path


def _request(provider_id: str, model_id: str, **params) -> BackendRequest:
    return BackendRequest(
        provider_id=provider_id, model_id=model_id, modality="video", params=params,
        values={"api_key": "test-key"}, base_url="https://example.invalid", display_name="test",
    )


class CapabilityRecordTests(unittest.TestCase):
    """Every option a model publishes comes from its record, and nothing else invents one."""

    def test_each_first_model_publishes_its_own_ladder(self) -> None:
        wan = _video_row(
            "alibaba/wan-3.0/image-to-video", PROVIDERS_BY_ID["fal-ai"],
            {"model_id": "alibaba/wan-3.0/image-to-video",
             "raw": {"category": "image-to-video"}},
        )
        seedance = _video_row(
            "bytedance/seedance-2.5/image-to-video", PROVIDERS_BY_ID["fal-ai"],
            {"model_id": "bytedance/seedance-2.5/image-to-video",
             "raw": {"category": "image-to-video"}},
        )
        self.assertEqual(wan["duration_seconds"],
                         {"min": 2, "max": 30, "step": 1, "default": 5})
        self.assertEqual(seedance["duration_seconds"],
                         {"min": 4, "max": 30, "step": 1, "auto": True})
        self.assertEqual(wan["resolutions"], ["480p", "720p", "1080p"])
        self.assertEqual(seedance["resolutions"], ["480p", "720p", "1080p"])
        self.assertEqual(wan["aspect_ratios"],
                         ["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16"])
        self.assertEqual(seedance["aspect_ratios"], ["auto"])
        # 29 seconds x 3 resolutions for one mode — the enumerable form of the same range.
        self.assertEqual(len(wan["variants"]), 29 * 3)
        self.assertEqual(len(seedance["variants"]), 27 * 3)
        self.assertEqual({variant["resolution"] for variant in wan["variants"]},
                         {"480p", "720p", "1080p"})

    def test_a_record_that_lost_a_value_fails_here_not_at_a_provider(self) -> None:
        record = video_capabilities.for_model("fal-ai", "bytedance/seedance-2.5/reference-to-video")
        self.assertIsNotNone(record)
        self.assertEqual(record.reference_field, "image_urls")
        self.assertEqual(record.max_reference_images, 30)
        self.assertEqual(record.duration_style, "string")
        self.assertEqual(record.source, "provider")
        self.assertEqual(record.source_date, "2026-10-26")

    def test_an_undescribed_cloud_model_publishes_its_mode_and_no_variants(self) -> None:
        row = _video_row(
            "unknown/whatever-video", PROVIDERS_BY_ID["fal-ai"],
            {"model_id": "unknown/whatever-video", "raw": {"category": "text-to-video"}},
        )
        self.assertEqual(row["modes"], ["text-to-video"])
        self.assertEqual(row["modes_source"], "provider")
        self.assertEqual(row["variants"], [])
        self.assertEqual(row["aspect_ratios"], [])

    def test_reference_to_video_publishes_its_limit(self) -> None:
        row = _video_row(
            "alibaba/wan-3.0/reference-to-video", PROVIDERS_BY_ID["fal-ai"],
            {"model_id": "alibaba/wan-3.0/reference-to-video",
             "raw": {"category": "reference-to-video"}},
        )
        self.assertEqual(row["modes"], ["reference-to-video"])
        self.assertTrue(row["supports_reference_images"])
        self.assertEqual(row["max_reference_images"], 10)
        self.assertFalse(row["supports_start_frame"])
        self.assertEqual(row["capability_source"], "provider")


class FalRequestMappingTests(unittest.TestCase):
    """The field a model declares, or a refusal — never a field it does not read."""

    def test_wan_puts_the_picture_in_start_image_url(self) -> None:
        path = _staged()
        payload = fal_video_input(_request(
            "fal-ai", "alibaba/wan-3.0/image-to-video", prompt="x",
            _reference_path=str(path), _reference_type="image/png",
            duration_seconds=7, resolution="1080p", aspect_ratio="9:16",
        ))
        self.assertEqual(payload["start_image_url"],
                         "data:image/png;base64," + base64.b64encode(_PNG).decode())
        self.assertNotIn("image_url", payload)
        self.assertEqual(payload["duration"], 7)
        self.assertEqual(payload["resolution"], "1080p")
        self.assertEqual(payload["aspect_ratio"], "9:16")

    def test_seedance_puts_the_picture_in_image_url_and_sends_duration_as_a_string(self) -> None:
        payload = fal_video_input(_request(
            "fal-ai", "bytedance/seedance-2.5/image-to-video", prompt="x",
            _reference_path=str(_staged()), _reference_type="image/png",
            duration_seconds=12,
        ))
        self.assertIn("image_url", payload)
        self.assertNotIn("start_image_url", payload)
        self.assertEqual(payload["duration"], "12")
        self.assertIsInstance(payload["duration"], str)

    def test_a_final_frame_goes_where_the_record_says(self) -> None:
        payload = fal_video_input(_request(
            "fal-ai", "alibaba/wan-3.0/image-to-video", prompt="x",
            _reference_path=str(_staged()), _reference_type="image/png",
            _end_reference_path=str(_staged("end.png")), _end_reference_type="image/png",
        ))
        self.assertIn("end_image_url", payload)

    def test_reference_images_keep_their_order(self) -> None:
        first, second = _staged("a.png"), _staged("b.png", _PNG + b"\x00")
        payload = fal_video_input(_request(
            "fal-ai", "bytedance/seedance-2.5/reference-to-video", prompt="x",
            _reference_paths=[str(first), str(second)],
            _reference_types=["image/png", "image/png"],
        ))
        self.assertEqual(len(payload["image_urls"]), 2)
        self.assertIn(base64.b64encode(_PNG).decode(), payload["image_urls"][0])
        self.assertNotEqual(payload["image_urls"][0], payload["image_urls"][1])

    def test_an_unpublished_canvas_is_refused_naming_the_published_set(self) -> None:
        for params, fragment in (
            ({"duration_seconds": 31}, "2-30s"),
            ({"duration_seconds": 1}, "2-30s"),
            ({"resolution": "1440p"}, "480p, 720p, 1080p"),
            ({"aspect_ratio": "5:2"}, "16:9"),
        ):
            with self.assertRaises(BackendError) as caught:
                fal_video_input(_request(
                    "fal-ai", "alibaba/wan-3.0/image-to-video", prompt="x", **params))
            self.assertIn(fragment, str(caught.exception))
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_text_to_video_sends_no_image_field_and_needs_none(self) -> None:
        payload = fal_video_input(_request(
            "fal-ai", "bytedance/seedance-2.5/text-to-video", prompt="a boat",
            duration_seconds=8, aspect_ratio="21:9",
        ))
        self.assertEqual(payload["prompt"], "a boat")
        self.assertEqual(payload["duration"], "8")
        for field in ("image_url", "start_image_url", "image_urls"):
            self.assertNotIn(field, payload)

    def test_an_endpoint_that_publishes_auto_accepts_it_and_one_that_does_not_refuses(self) -> None:
        seeded = fal_video_input(_request(
            "fal-ai", "bytedance/seedance-2.5/text-to-video", prompt="x",
            duration_seconds="auto",
        ))
        self.assertEqual(seeded["duration"], "auto")
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "alibaba/wan-3.0/text-to-video", prompt="x", duration_seconds="auto"))
        self.assertIn("whole number of seconds", str(caught.exception))

    def test_a_picture_for_a_text_only_endpoint_is_refused_not_dropped(self) -> None:
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "alibaba/wan-3.0/text-to-video", prompt="x",
                _reference_path=str(_staged()), _reference_type="image/png",
            ))
        self.assertIn("takes no", str(caught.exception))
        self.assertEqual(caught.exception.upstream_accepted, False)

    def test_image_to_video_without_its_picture_is_refused_by_name(self) -> None:
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "alibaba/wan-3.0/image-to-video", prompt="x", duration_seconds=5,
            ))
        self.assertIn("carries none", str(caught.exception))
        self.assertIn("/v1/videos/source-images", str(caught.exception))
        self.assertEqual(caught.exception.upstream_accepted, False)

    def test_a_mode_the_model_does_not_publish_is_refused(self) -> None:
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "bytedance/seedance-2.5/text-to-video", prompt="x",
                mode="image-to-video",
            ))
        self.assertIn("image-to-video was asked for", str(caught.exception))

    def test_reference_to_video_needs_a_reference(self) -> None:
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "bytedance/seedance-2.5/reference-to-video", prompt="x",
                mode="reference-to-video",
            ))
        self.assertIn("at least one reference image", str(caught.exception))

    def test_a_staged_file_that_has_gone_is_named_not_silently_dropped(self) -> None:
        # Gone, not "outside": the file was staged inside the artifact store and then removed,
        # which is the case this names. A path outside the store is a different refusal now.
        missing = _staged("gone.png")
        missing.unlink()
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "alibaba/wan-3.0/image-to-video", prompt="x",
                _reference_path=str(missing), _reference_type="image/png",
            ))
        self.assertIn("no longer held", str(caught.exception))
        self.assertEqual(caught.exception.upstream_accepted, False)

    def test_too_many_reference_images_is_refused_with_the_limit(self) -> None:
        with self.assertRaises(BackendError) as caught:
            fal_video_input(_request(
                "fal-ai", "alibaba/wan-3.0/reference-to-video", prompt="x",
                _reference_paths=[str(_staged(f"{i}.png")) for i in range(11)],
                _reference_types=["image/png"] * 11,
            ))
        self.assertIn("at most 10", str(caught.exception))


class OtherAdapterSourceImageTests(unittest.TestCase):
    """The same picture, in each provider's own shape — and never in a field it ignores."""

    def test_xai_takes_the_picture_as_an_image_object(self) -> None:
        body = xai_video_body(_request(
            "xai-direct", "grok-imagine-video-1.5", prompt="x",
            _reference_path=str(_staged()), _reference_type="image/png",
        ))
        self.assertTrue(body["image"]["url"].startswith("data:image/png;base64,"))

    def test_minimax_v2_takes_a_content_array_with_a_first_frame_role(self) -> None:
        body = minimax_video_body(_request(
            "minimax-direct", "MiniMax-H3", prompt="a boat",
            _reference_path=str(_staged()), _reference_type="image/png",
            duration=6, resolution="768P",
        ))
        self.assertNotIn("first_frame_image", body)
        self.assertEqual(body["content"][0], {"type": "text", "text": "a boat"})
        self.assertEqual(body["content"][1]["type"], "image_url")
        self.assertEqual(body["content"][1]["role"], "first_frame")
        self.assertTrue(body["content"][1]["image_url"]["url"].startswith("data:image/png"))

    def test_kie_uploads_the_bytes_and_uses_the_url_it_returns(self) -> None:
        """Kie's inputs take URLs, not base64, so the staged picture is handed to Kie first."""
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append({"url": str(request.url), **json.loads(request.content)})
            return httpx.Response(200, json={
                "code": 200,
                "data": {"downloadUrl": "https://kie.example/scene.png"},
            })

        client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            with patch.object(kie_module.httpx, "post", side_effect=client.post):
                url = kie_reference_url(_request(
                    "kie-ai-images", "kling/v3-turbo-image-to-video", prompt="x",
                    _reference_path=str(_staged()), _reference_type="image/png",
                ))
        finally:
            client.close()
        self.assertEqual(url, "https://kie.example/scene.png")
        self.assertEqual(seen[0]["url"],
                         "https://kieai.redpandaai.co/api/file-base64-upload")
        self.assertTrue(seen[0]["base64Data"].startswith("data:image/png;base64,"))

    def test_kie_video_inputs_use_kie_names(self) -> None:
        payload = kie_input(_request(
            "kie-ai-images", "kling/v3-turbo-image-to-video", prompt="x",
            duration_seconds=5, resolution="1080p",
        ))
        self.assertEqual(payload["duration"], 5)
        self.assertNotIn("duration_seconds", payload)
        self.assertNotIn("reference_asset_url", payload)


class VideoStagingTests(unittest.TestCase):
    """`POST /v1/videos/generations` keeps every named input, and refuses one it cannot."""

    def setUp(self) -> None:
        from gateway import api, video_assets
        self.api, self.assets = api, video_assets

    def _asset(self) -> str:
        return self.assets.store(_PNG, "scene.png", "image/png")

    def test_first_and_last_frame_and_references_are_all_staged(self) -> None:
        first, last, reference = self._asset(), self._asset(), self._asset()
        staged = self.api._stage_video_reference({
            "model_id": "fal-ai::alibaba/wan-3.0/image-to-video",
            "reference_asset_id": first,
            "end_image_asset_id": last,
            "reference_asset_ids": [reference],
        })
        for key in ("_reference_path", "_end_reference_path"):
            self.assertTrue(Path(staged[key]).exists())
            self.assertTrue(staged[key.replace("_path", "_type")])
        self.assertEqual(len(staged["_reference_paths"]), 1)
        self.assertTrue(Path(staged["_reference_paths"][0]).exists())

    def test_an_expired_asset_is_a_named_refusal_not_a_prompt_only_job(self) -> None:
        from gateway.api import GatewayError
        with self.assertRaises(GatewayError) as caught:
            self.api._stage_video_reference({
                "model_id": "fal-ai::alibaba/wan-3.0/image-to-video",
                "reference_asset_id": "not-a-real-asset",
            })
        self.assertIn("no longer held", str(caught.exception))
        self.assertEqual(caught.exception.code, "REFERENCE_ASSET_EXPIRED")

    def test_a_text_to_video_request_stages_nothing(self) -> None:
        staged = self.api._stage_video_reference({
            "model_id": "fal-ai::bytedance/seedance-2.5/text-to-video", "prompt": "x",
        })
        self.assertEqual(staged, {"model_id": "fal-ai::bytedance/seedance-2.5/text-to-video",
                                  "prompt": "x"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
