"""Bounded video controls: no provider calls, browser, native app or paid generation."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.test_extension_workers import _Base, TEXT, IMAGE
from gateway import api, extension_workers as ew, video_capabilities
from gateway.adapters import BackendError
from gateway.adapters.chrome_extension import ChromeExtensionBackend, _payload
from gateway.catalog import Catalog, _video_row
from gateway.providers.definitions import PROVIDERS_BY_ID


class VideoControlsTests(_Base):
    def req(self, record=TEXT, **params):
        request, _ = self.request(record)
        request.params.clear()
        request.params.update(prompt="a scene", **params)
        return request

    def test_non_integer_durations_are_refused_before_offering(self):
        for duration in (4.5, True, False, None, "4.5", "4s", [], {}, float("inf")):
            with self.subTest(duration=duration), self.assertRaises(BackendError) as caught:
                ChromeExtensionBackend().generate(self.req(duration_seconds=duration))
            self.assertEqual(caught.exception.code, "INVALID_REQUEST")
            self.assertIs(caught.exception.upstream_accepted, False)
            self.assertEqual(ew.broker.depth("video"), 0)

    def test_scene_length_is_not_silently_shortened(self):
        for duration in (0, -1, 5, 6, 10):
            with self.subTest(duration=duration), self.assertRaises(BackendError) as caught:
                _payload(self.req(duration_seconds=duration), [])
            self.assertIn("no scene is truncated", str(caught.exception))

    def test_defaults_and_whole_string_duration_preserve_the_bounded_path(self):
        for params in ({}, {"duration_seconds": float(TEXT.duration_default)},
                       {"duration_seconds": str(TEXT.duration_default)},
                       {"duration": TEXT.duration_default}, {"durationSec": TEXT.duration_default}):
            payload = _payload(self.req(**params), [])
            self.assertEqual(payload["durationSec"], TEXT.duration_default)
            self.assertEqual(payload["aspectRatio"], "16:9")
            self.assertEqual(payload["outputCount"], 1)
            self.assertNotIn("resolution", payload)

    def test_contradictory_and_malformed_modes_refuse(self):
        for field in ("mode", "operation", "videoMode"):
            for value in ("image", "reference-to-video", "unknown", None, [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(BackendError):
                    _payload(self.req(**{field: value}), [])
        for record, mode, refs in ((TEXT, "text-to-video", []),
                                    (IMAGE, "reference-to-video", [{"mediaType": "image/png"}])):
            payload = _payload(self.req(record, operation=mode), refs)
            self.assertEqual(payload["videoMode"], "text" if record is TEXT else "image")
        with self.assertRaises(BackendError):
            _payload(self.req(mode="text", operation="image"), [])

    def test_conflicting_aliases_are_never_dropped(self):
        for params in ({"duration_seconds": 4, "duration": 8},
                       {"duration_seconds": 4, "durationSec": 4.5},
                       {"aspect_ratio": "16:9", "aspectRatio": "9:16"},
                       {"output_count": 1, "outputCount": 2}, {"output_count": 1, "n": True}):
            with self.subTest(params=params), self.assertRaises(BackendError):
                _payload(self.req(**params), [])

    def test_uncaptured_resolutions_and_output_counts_refuse(self):
        for params in ({"resolution": "360p"}, {"resolution": "720p"}, {"resolution": ""},
                       {"aspectRatio": "9:16"}, {"aspect_ratio": None},
                       {"output_count": 2}, {"outputCount": 0}, {"n": -1},
                       {"output_count": 1.5}, {"outputCount": True}, {"n": None}):
            with self.subTest(params=params), self.assertRaises(BackendError) as caught:
                ChromeExtensionBackend().generate(self.req(**params))
            self.assertIs(caught.exception.upstream_accepted, False)
            self.assertEqual(ew.broker.depth("video"), 0)
        for name in ("output_count", "outputCount", "n"):
            self.assertEqual(_payload(self.req(**{name: 1}), [])["outputCount"], 1)

    def test_capabilities_publish_known_bounds_and_explicit_unknowns(self):
        definition = PROVIDERS_BY_ID["story-studio-video"]
        row = _video_row(TEXT.model_id, definition, {"model_id": TEXT.model_id})
        self.assertEqual(row["duration_seconds"], {
            "min": TEXT.duration_min, "max": TEXT.duration_max, "step": 1,
            "default": TEXT.duration_default,
        })
        self.assertEqual(row["output_counts"], [1])
        self.assertEqual(row["support_status"], "bounded")
        self.assertEqual(row["aspect_ratios"], ["16:9"])
        self.assertEqual(row["resolutions"], [])
        self.assertEqual(row["resolution_status"], "update_required")
        self.assertEqual(row["pricing_status"], "unknown")
        self.assertEqual(row["variants"], [])

    def test_newly_listed_model_remains_visible_without_invented_mapping(self):
        definition = PROVIDERS_BY_ID["story-studio-video"]
        row = _video_row("newly-listed-video", definition, {
            "model_id": "newly-listed-video", "raw": {"category": "text-to-video"}})
        self.assertEqual(row["support_status"], "update_required")
        self.assertIsNone(row["duration_seconds"])
        self.assertEqual(row["resolutions"], [])
        self.assertEqual(row["output_counts"], [])
        self.assertEqual(row["variants"], [])
        request = self.req()
        request = replace(request, model_id="newly-listed-video")
        with self.assertRaises(BackendError) as caught:
            _payload(request, [])
        self.assertIn("update required", str(caught.exception))

    def test_story_publication_keeps_new_models_listed_but_locked(self):
        definition = PROVIDERS_BY_ID["story-studio-video"]
        for model_id, status in ((TEXT.model_id, "bounded"), ("newly-listed-video", "update_required")):
            row = Catalog()._story_row("video", definition, {
                "model_id": model_id, "display_name": model_id, "raw": {},
            })
            self.assertTrue(row["id"].endswith(model_id))
            self.assertEqual(row["support_status"], status)
            self.assertEqual(row["locked"], status == "update_required")
            self.assertEqual(row["available"], status == "bounded")
            if status == "update_required":
                self.assertIn("Update required", row["activation_blockers"][0])
            self.assertEqual(row["pricing_status"], "unknown")

    def test_validated_controls_reach_authenticated_claim_unchanged(self):
        request = self.req(operation="text-to-video", duration_seconds=TEXT.duration_default,
                           aspect_ratio="16:9", output_count=1)
        payload = _payload(request, [])
        job = ew.broker.offer("video", payload)
        response = self.claim()
        self.assertEqual(response.status_code, 200)
        claimed = response.json()["job"]
        self.assertEqual(claimed, payload)
        self.assertEqual(claimed["jobId"], job.job_id)
        self.assertEqual(claimed["outputCount"], 1)
        self.assertEqual(claimed["durationSec"], TEXT.duration_default)

    def test_a_verified_resolution_record_is_preserved_not_dropped(self):
        # Fixture only: the shipped catalogue publishes no resolution override.
        fixture = replace(TEXT, resolutions=("fixture-tier",))
        with patch.dict(video_capabilities._BY_MODEL,
                        {(TEXT.provider_id, TEXT.model_id): fixture}):
            payload = _payload(self.req(resolution="fixture-tier"), [])
        ew.broker.offer("video", payload)
        self.assertEqual(self.claim().json()["job"]["resolution"], "fixture-tier")


class VideoReplayIdentityTests(unittest.TestCase):
    def test_each_supported_control_spelling_is_part_of_purchase_identity(self):
        fields = {"duration_seconds": 4, "duration": 4, "durationSec": 4,
                  "resolution": "360p", "aspect_ratio": "16:9", "aspectRatio": "16:9",
                  "mode": "text", "operation": "text-to-video", "videoMode": "text",
                  "output_count": 1, "outputCount": 1, "n": 1}
        for field, value in fields.items():
            stored = {"prompt": "a scene", field: value}
            for saved in (stored, json.dumps(stored)):
                api._refuse_changed_video_request({"params": saved}, dict(stored))
                for changed in ({"prompt": "a scene"}, {**stored, field: "changed"}):
                    with self.subTest(field=field), self.assertRaises(api.GatewayError):
                        api._refuse_changed_video_request({"params": saved}, changed)

    def test_replay_route_refuses_control_changes_before_staging_or_submitting(self):
        from fastapi import Response
        original = {"model_id": TEXT.model_id, "prompt": "a scene", "outputCount": 1}
        with patch.object(api.store, "find_by_idempotency_key", return_value={
            "modality": "video", "params": original,
        }), patch.object(api, "_stage_video_reference") as stage, patch.object(api, "_submit") as submit:
            for count in (2, True, 1.0):
                with self.subTest(count=count), self.assertRaises(api.GatewayError):
                    api.create_video({**original, "outputCount": count}, Response(),
                                     idempotency_key="video-control-fixture", caller="fixture")
            stage.assert_not_called()
            submit.assert_not_called()

    def test_explicit_null_control_is_not_an_omitted_default(self):
        for field in ("duration", "durationSec", "aspectRatio", "videoMode", "output_count", "outputCount", "n"):
            for before, after in (({}, {field: None}), ({field: None}, {})):
                with self.subTest(field=field), self.assertRaises(api.GatewayError):
                    api._refuse_changed_video_request({"params": before}, after)

    def test_boolean_or_float_is_not_an_identical_integer_replay(self):
        for field in ("duration_seconds", "output_count", "outputCount", "n"):
            for value in (True, 1.0):
                with self.subTest(field=field, value=value), self.assertRaises(api.GatewayError):
                    api._refuse_changed_video_request({"params": {field: 1}}, {field: value})


if __name__ == "__main__":
    unittest.main()
