"""Browser-extension workers: image and audio kinds — payload mapping, refusals, token-kind scoping.

Mirrors `test_extension_workers.py`. No network, no real Relay. Tests were written but not run
(occupied quiet slot; the lead runs them in the granted window).
"""
import base64
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests._isolation import sandbox_env
sandbox_env("gateway-extension-image-audio-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, extension_workers as ew
from gateway.adapters import BackendError, BackendRequest
from gateway.adapters.chrome_extension import (
    ChromeExtensionBackend, _audio_selectable_keys, _detect_image_signature, _image_payload,
)
from gateway.config import ARTIFACT_DIR

ON = {ew.SWITCH: "1", "GATEWAY_EXTENSION_WORKER_LEASE_SECONDS": "2",
      "GATEWAY_EXTENSION_WORKER_CLAIM_WAIT_SECONDS": "2"}

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 20
WEBP_HEADER = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
WAV_HEADER = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 20


class _Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ON)
        env.start()
        self.addCleanup(env.stop)
        ew.reset_for_tests()
        self.addCleanup(ew.reset_for_tests)
        self.client = TestClient(api.app, client=("100.64.0.9", 40000))

    def auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def claim(self, token, worker, kind):
        return self.client.post("/api/workers/claim", headers=self.auth(token),
                                json={"workerId": worker, "kind": kind})


class ImageKindRegistrationTests(unittest.TestCase):
    def test_image_kind_is_registered(self):
        self.assertIn("image", ew.KINDS)

    def test_image_provider_is_disabled_by_default(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        definition = PROVIDERS_BY_ID["story-studio-image"]
        self.assertFalse(definition.default_enabled)
        self.assertEqual(definition.kind, "chrome_extension")
        self.assertEqual(definition.modalities, ("image",))
        self.assertGreater(len(definition.static_models), 0)


class AudioKindRegistrationTests(unittest.TestCase):
    def test_audio_kind_is_registered(self):
        self.assertIn("audio", ew.KINDS)

    def test_audio_provider_is_disabled_by_default(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        definition = PROVIDERS_BY_ID["story-studio-audio"]
        self.assertFalse(definition.default_enabled)
        self.assertEqual(definition.kind, "chrome_extension")
        self.assertEqual(definition.modalities, ("tts",))
        self.assertGreater(len(definition.static_models), 0)

    def test_real_catalogue_model_ids_are_all_published(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        data = json.loads((Path(__file__).resolve().parents[1] / "gateway" /
                           "extension_audio_models.json").read_text())
        expected = {m["key"] for provider in data["providers"].values()
                    for m in provider["models"]}
        ids = {m["id"] for m in PROVIDERS_BY_ID["story-studio-audio"].static_models}
        self.assertEqual(ids, expected, "public model IDs must not be mistaken for display masking")


class CatalogueShapeTests(unittest.TestCase):
    """The zero-price shape and truthful `chargeable` apply to chrome_extension rows too."""

    def _row(self, modality, provider_id, model_id):
        from gateway.catalog import catalog
        from gateway.providers.definitions import PROVIDERS_BY_ID
        definition = PROVIDERS_BY_ID[provider_id]
        return catalog._story_row(modality, definition,
                                  {"model_id": model_id, "display_name": model_id, "raw": {}})

    def test_image_row_has_zero_price_shape_and_is_chargeable(self):
        row = self._row("image", "story-studio-image", "NARWHAL")
        # Zero-price shape: every price field present at zero (Story drops rows missing them).
        self.assertEqual(row.get("credits_per_image"), 0)
        self.assertEqual(row.get("minimum_credits"), 0)
        self.assertIn("price_version", row)
        # Flow images spend credits; an unpublished price is never read as free.
        self.assertIs(row.get("chargeable"), True)

    def test_audio_row_has_zero_price_shape_and_is_chargeable(self):
        row = self._row("tts", "story-studio-audio", "s2.1-pro")
        # TTS priced shape carries per-1000-character and standard credits at zero.
        self.assertEqual(row.get("credits_per_1000_characters"), 0)
        self.assertEqual(row.get("standard_credits"), 0)
        self.assertEqual(row.get("minimum_credits"), 0)
        self.assertIn("price_version", row)
        # Web-session quota is unknown, so it is not read as free.
        self.assertIs(row.get("chargeable"), True)

    def test_no_placeholder_id_reaches_a_catalogue_row(self):
        from gateway.providers.definitions import PROVIDERS_BY_ID
        for model in PROVIDERS_BY_ID["story-studio-audio"].static_models:
            self.assertNotIn("{{", model["id"])
            self.assertNotIn("}}", model["id"])


class TokenKindScopingTests(_Base):
    def test_image_token_cannot_claim_video_jobs(self):
        image_token = ew.mint("img-worker", "image")
        video_token = ew.mint("vid-worker", "video")
        ew.broker.offer("video", {"prompt": "x"})
        self.assertEqual(self.claim(image_token, "img-worker", "image").status_code, 204)
        self.assertEqual(self.claim(video_token, "vid-worker", "video").status_code, 200)

    def test_audio_token_cannot_claim_image_jobs(self):
        audio_token = ew.mint("aud-worker", "audio")
        image_token = ew.mint("img-worker", "image")
        ew.broker.offer("image", {"prompt": "x"})
        self.assertEqual(self.claim(audio_token, "aud-worker", "audio").status_code, 204)
        self.assertEqual(self.claim(image_token, "img-worker", "image").status_code, 200)

    def test_mint_refuses_unknown_kind(self):
        with self.assertRaises(ValueError):
            ew.mint("x", "unknown")


class _PayloadChecks(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(ew, "enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)


class ImagePayloadRefusalTests(_PayloadChecks):
    def _request(self, model_id="NARWHAL", **params):
        return BackendRequest(
            provider_id="story-studio-image", model_id=model_id, modality="image",
            params={"prompt": "a cat", "aspect_ratio": "16:9", **params},
            values={}, base_url="", checkpoint=None,
        )

    def test_unknown_model_refuses_before_offering(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(model_id="UNKNOWN_KEY"))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIs(caught.exception.upstream_accepted, False)

    def test_empty_prompt_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(prompt=""))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_bad_aspect_ratio_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(aspect_ratio="9:16"))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_too_many_references_refuses(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(5):
            p = ARTIFACT_DIR / f"ref-{i}.png"
            p.write_bytes(PNG_HEADER)
            paths.append(str(p))
        req = self._request(_reference_paths=paths, _reference_types=["image/png"] * 5)
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("at most 4", str(caught.exception))

    def test_unsupported_provider_is_refused_not_overwritten(self):
        """Relay's image contract allows only 'flow'; another provider is a refusal, not a swap."""
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(provider="chatgpt"))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("only 'flow'", str(caught.exception))

    def test_invalid_flow_project_is_refused(self):
        req = BackendRequest(
            provider_id="story-studio-image", model_id="NARWHAL", modality="image",
            params={"prompt": "a cat", "aspect_ratio": "16:9"}, values={},
            base_url="", checkpoint=None,
        )
        req.values = {"flow_project_id": "has a space"}
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("not a valid project id", str(caught.exception))

    def test_valid_flow_project_passes(self):
        req = BackendRequest(
            provider_id="story-studio-image", model_id="NARWHAL", modality="image",
            params={"prompt": "a cat", "aspect_ratio": "16:9"}, values={},
            base_url="", checkpoint=None,
        )
        req.values = {"flow_project_id": "proj_abc-123"}
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "WORKER_OFFLINE")   # passed validation

    def test_valid_png_reference_passes_validation(self):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / "good-ref.png"
        path.write_bytes(PNG_HEADER)
        req = self._request(_reference_paths=[str(path)], _reference_types=["image/png"])
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "WORKER_OFFLINE")


class ImageReferenceSignatureTests(unittest.TestCase):
    """WebP needs RIFF+WEBP; bad base64 refuses; mediaType must agree with the bytes."""

    def _req(self):
        return BackendRequest(
            provider_id="story-studio-image", model_id="NARWHAL", modality="image",
            params={"prompt": "a cat", "aspect_ratio": "16:9"}, values={},
            base_url="", checkpoint=None,
        )

    def test_riff_without_webp_marker_is_refused(self):
        self.assertIsNone(_detect_image_signature(WAV_HEADER))
        self.assertEqual(_detect_image_signature(WEBP_HEADER), "webp")

    def test_bad_base64_is_refused_not_raised(self):
        refs = [{"mediaType": "image/png", "dataBase64": "!!!not-base64!!!"}]
        with self.assertRaises(BackendError) as caught:
            _image_payload(self._req(), refs)
        self.assertIn("does not decode", str(caught.exception))

    def test_media_type_disagreement_is_refused(self):
        refs = [{"mediaType": "image/jpeg",
                 "dataBase64": base64.b64encode(PNG_HEADER).decode("ascii")}]
        with self.assertRaises(BackendError) as caught:
            _image_payload(self._req(), refs)
        self.assertIn("disagrees", str(caught.exception))

    def test_matching_media_type_passes(self):
        refs = [{"mediaType": "image/png",
                 "dataBase64": base64.b64encode(PNG_HEADER).decode("ascii")}]
        self.assertEqual(_image_payload(self._req(), refs)["kind"], "image")


class AudioPayloadTests(_PayloadChecks):
    def _request(self, model="s2.1-pro", provider="fish", voice="v1", text="Hello world",
                 **params):
        return BackendRequest(
            provider_id="story-studio-audio", model_id=model, modality="tts",
            params={"provider": provider, "voice_id": voice, "text": text, **params},
            values={}, base_url="", checkpoint=None,
        )

    def test_unknown_provider_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(provider="unknown_tts"))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("not known", str(caught.exception))

    def test_unknown_model_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(model="nonexistent-model"))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("no capability record", str(caught.exception))

    def test_empty_voice_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(voice=""))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("needs a voice", str(caught.exception))

    def test_empty_text_refuses(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(text=""))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("needs text", str(caught.exception))

    def test_valid_request_fails_closed(self):
        """The catalogue's tts modality reaches audio validation, never offers without inventory."""
        with patch.object(ew, "enabled", return_value=True), \
             patch.object(ew.broker, "offer") as offer, \
             self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request())
        self.assertEqual(caught.exception.code, "WORKER_INVENTORY_UNAVAILABLE")
        self.assertIs(caught.exception.upstream_accepted, False)
        offer.assert_not_called()

    def test_caller_supplied_inventory_cannot_authorize(self):
        """Regression: request params are caller input; a supplied inventory must NOT authorize."""
        inv = {"fish": {"models": ["s2.1-pro"], "voices": ["v1"]}}
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(_worker_inventory=inv))
        self.assertEqual(caught.exception.code, "WORKER_INVENTORY_UNAVAILABLE")


class AudioLimitsAndSettingsTests(_PayloadChecks):
    def _request(self, model="eleven_v3", provider="elevenlabs_web", text="Hi", **params):
        return BackendRequest(
            provider_id="story-studio-audio", model_id=model, modality="tts",
            params={"provider": provider, "voice_id": "v1", "text": text, **params},
            values={}, base_url="", checkpoint=None,
        )

    def test_per_model_max_chars_wins_over_provider(self):
        """ElevenLabs models state 4500; the provider says 5000. The model wins."""
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(text="x" * 4501))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("4500", str(caught.exception))

    def test_speed_false_model_refuses_speed(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(speed=1.0))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("does not accept speed", str(caught.exception))

    def test_non_finite_speed_is_refused(self):
        req = BackendRequest(
            provider_id="story-studio-audio", model_id="s2.1-pro", modality="tts",
            params={"provider": "fish", "voice_id": "v1", "text": "Hi",
                    "speed": float("inf")},
            values={}, base_url="", checkpoint=None,
        )
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("not finite", str(caught.exception))

    def test_speed_out_of_range_is_refused(self):
        req = BackendRequest(
            provider_id="story-studio-audio", model_id="s2.1-pro", modality="tts",
            params={"provider": "fish", "voice_id": "v1", "text": "Hi",
                    "speed": 3.0},
            values={}, base_url="", checkpoint=None,
        )
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("speed must be between", str(caught.exception))

    def test_unsupported_auto_tag_is_refused(self):
        """ElevenLabs does not support autoTag (fish does)."""
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(autoTag=True))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("does not support autoTag", str(caught.exception))

    def test_unknown_provider_setting_is_refused(self):
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(providerSettings={"bogus": 1}))
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("does not support providerSettings", str(caught.exception))

    def test_supported_provider_setting_passes_validation(self):
        """A supported setting passes the local checks, then the unconditional fail-closed applies."""
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(self._request(providerSettings={"stability": 0.4}))
        self.assertEqual(caught.exception.code, "WORKER_INVENTORY_UNAVAILABLE")


class PlaceholderKeyTests(_PayloadChecks):
    def test_placeholder_key_is_refused_with_named_reason(self):
        req = BackendRequest(
            provider_id="story-studio-audio", model_id="{{fixture-placeholder}}", modality="tts",
            params={"provider": "elevenlabs_web", "voice_id": "v1", "text": "Hi"},
            values={}, base_url="", checkpoint=None,
        )
        with self.assertRaises(BackendError) as caught:
            ChromeExtensionBackend().generate(req)
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertIn("template placeholder", str(caught.exception))
        self.assertIn("exact public model id", str(caught.exception))

    def test_placeholder_keys_are_excluded_from_selectable_set(self):
        records = {"fixture": {"models": [{"key": "fixture-valid"},
                                          {"key": "{{fixture-placeholder}}"}]}}
        with patch.dict("gateway.adapters.chrome_extension._AUDIO_PROVIDERS", records):
            self.assertEqual(_audio_selectable_keys("fixture"), {"fixture-valid"})


class ImageResultMediaTypeTests(_Base):
    def test_image_result_requires_image_media_type(self):
        token = ew.mint("img-w", "image")
        self.claim(token, "img-w", "image")
        job = ew.broker.offer("image", {"prompt": "x"})
        self.claim(token, "img-w", "image")
        answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(token),
                                  data={"mediaType": "video/mp4"}, files={"file": ("c", b"x")})
        self.assertEqual(answer.status_code, 422)

    def test_image_result_accepts_image_media_type(self):
        token = ew.mint("img-w", "image")
        self.claim(token, "img-w", "image")
        job = ew.broker.offer("image", {"prompt": "x"})
        self.claim(token, "img-w", "image")
        answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(token),
                                  data={"mediaType": "image/png"}, files={"file": ("c", PNG_HEADER)})
        self.assertEqual(answer.status_code, 200)


class AudioResultMediaTypeTests(_Base):
    def test_audio_result_requires_audio_media_type(self):
        token = ew.mint("aud-w", "audio")
        self.claim(token, "aud-w", "audio")
        job = ew.broker.offer("audio", {"text": "x"})
        self.claim(token, "aud-w", "audio")
        answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(token),
                                  data={"mediaType": "image/png"}, files={"file": ("c", b"x")})
        self.assertEqual(answer.status_code, 422)

    def test_audio_result_accepts_audio_media_type(self):
        token = ew.mint("aud-w", "audio")
        self.claim(token, "aud-w", "audio")
        job = ew.broker.offer("audio", {"text": "x"})
        self.claim(token, "aud-w", "audio")
        answer = self.client.post(f"/api/workers/jobs/{job.job_id}/result", headers=self.auth(token),
                                  data={"mediaType": "audio/mpeg"}, files={"file": ("c", b"A")})
        self.assertEqual(answer.status_code, 200)


class CopyScriptTests(unittest.TestCase):
    """The capability JSON is reproduced by `scripts/copy_extension_models.py` from Relay's source."""

    def test_copied_files_carry_a_64_hex_source_sha256(self):
        for name in ("image", "audio"):
            data = json.loads((Path("gateway") / f"extension_{name}_models.json").read_text())
            self.assertRegex(data["source_sha256"], r"^[0-9a-f]{64}$")

    def test_script_reproduces_files_when_relay_sources_are_present(self):
        import sys
        sys.path.insert(0, "scripts")
        try:
            import copy_extension_models as script
        finally:
            sys.path.pop(0)
        image_work = Path("/Volumes/KH-STUDIO-ATLAS/KH-Worktrees/"
                          "story-studio-image-generator/relay-gateway-20261109")
        audio_work = Path("/Volumes/KH-STUDIO-ATLAS/KH-Worktrees/"
                          "story-studio-audio-generator/relay-gateway-20261109")
        if not (image_work.exists() and audio_work.exists()):
            self.skipTest("Relay worktrees not present on this machine")
        for name, fn, work in (("image", script._image_json, image_work),
                               ("audio", script._audio_json, audio_work)):
            on_disk = json.loads((Path("gateway") / f"extension_{name}_models.json").read_text())
            self.assertEqual(on_disk, fn(work), name)


if __name__ == "__main__":
    unittest.main()