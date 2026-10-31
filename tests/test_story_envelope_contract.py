"""The job envelope Story Studio reads: the 2026-09-30 contract and the Story<->gateway seam fixes.

CONTRACT-job-envelope: every envelope carries `dispatch_state`, `cancellable`,
`cancellation_requested`, `cancelled` and `waiting`; a cancelled job is published `cancelled` with
`cancelled: true` beside it; a held job whose result arrives is `succeeded`; no `host:port` or `/api/hub`
path ever appears in a published text field. Plus the routes and fields Story built against that
the gateway did not serve (cancel for video and transcription, voice preview, the voice library,
`paid_*_jobs`, transcription language, the video source image, the real version).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-envelope-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "fixture-key",
})

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, video_assets, voices  # noqa: E402
from gateway.store import JobStore  # noqa: E402

READ = {"Authorization": "Bearer fixture-key"}
CONTRACT_KEYS = ("dispatch_state", "cancellable", "cancellation_requested", "cancelled", "waiting")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self._n = 0

    def job(self, modality: str = "image", *, running: bool = False, dispatched: bool = False) -> dict:
        self._n += 1
        row, _ = api.store.submit(modality=modality, model_id="m", params={}, provider_id="p",
                                  idempotency_key=f"env-{modality}-{id(self)}-{self._n}-{time.time()}")
        if running or dispatched:
            self.assertTrue(api.store.mark_running(row["id"]))
        if dispatched:
            self.assertTrue(api.store.begin_dispatch(row["id"]))
        return row

    def get(self, path: str) -> dict:
        response = self.client.get(path, headers=READ)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class EnvelopeContractTests(_Case):
    def test_every_envelope_carries_the_contract_fields(self) -> None:
        body = self.get(f"/v1/images/{self.job()['id']}")
        for key in CONTRACT_KEYS:
            self.assertIn(key, body)
        self.assertEqual(body["dispatch_state"], "pre_dispatch")
        self.assertIs(body["cancellable"], True)
        self.assertIs(body["cancellation_requested"], False)
        self.assertIs(body["cancelled"], False)
        self.assertIsNone(body["waiting"])

    def test_a_dispatched_job_is_not_cancellable_now(self) -> None:
        body = self.get(f"/v1/images/{self.job(dispatched=True)['id']}")
        self.assertEqual(body["dispatch_state"], "dispatched")
        self.assertIs(body["cancellable"], False)

    def test_a_cancel_request_on_dispatched_work_is_reported_as_requested(self) -> None:
        job = self.job(dispatched=True)
        response = self.client.post(f"/v1/images/{job['id']}/cancel", headers=READ)
        body = response.json()
        self.assertIs(body["cancellation_requested"], True)
        self.assertEqual(body["state"], "running", "a request is not proof the work stopped")
        self.assertIs(body["cancelled"], False)

    def test_a_cancelled_job_is_published_cancelled_with_cancelled_true(self) -> None:
        # The envelope says what the record says (`cancelled`); `cancelled: true` and
        # `failure_code: CANCELLED` stay beside it for callers keyed on either (audit 2026-10-02:
        # it used to say `failed`).
        for modality, path in (("video", "/v1/videos/{}/cancel"),
                               ("transcription", "/v1/transcriptions/{}/cancel"),
                               ("image", "/v1/images/{}/cancel")):
            job = self.job(modality)
            response = self.client.post(path.format(job["id"]), headers=READ)
            self.assertEqual(response.status_code, 200, (modality, response.text))
            body = response.json()
            self.assertEqual((body["state"], body["cancelled"]), ("cancelled", True), modality)
            self.assertEqual(body["state"], api.store.get(job["id"])["state"], "envelope == record")
            self.assertEqual(self.client.get(path.format(job["id"]).removesuffix("/cancel"), headers=READ).json()["state"],
                             "cancelled", "a later poll agrees with the cancel answer")
            self.assertEqual(body["failure_code"], "CANCELLED")
            self.assertIs(body["cancellable"], False)
            self.assertEqual(api.store.get(job["id"])["state"], "cancelled", "the record is exact")

    def test_the_native_and_story_readings_publish_the_same_state(self) -> None:
        job = self.job()
        api.store.cancel(job["id"])
        row = api.store.get(job["id"])
        self.assertEqual(api._job_envelope(row, native=True)["state"], "cancelled")
        self.assertEqual(api._job_envelope(row)["state"], "cancelled")

    def test_a_cancel_for_another_modality_is_not_found(self) -> None:
        job = self.job("image")
        self.assertEqual(self.client.post(f"/v1/videos/{job['id']}/cancel", headers=READ).status_code, 404)

    def test_waiting_is_published_while_the_hub_item_is_queued_and_cleared_after(self) -> None:
        job = self.job(dispatched=True)
        api.store.record_waiting(job["id"], {
            "reason": "waiting for a free machine: http://100.103.25.41:47873/api/hub/jobs/x",
            "next_try_at": 1_700_000_030.0, "since": 1_700_000_000.0})
        waiting = self.get(f"/v1/images/{job['id']}")["waiting"]
        self.assertEqual(waiting["next_try_at"], "2023-11-14T22:13:50Z")
        self.assertEqual(waiting["since"], "2023-11-14T22:13:20Z")
        self.assertNotIn("47873", waiting["reason"])
        self.assertNotIn("/api/hub", waiting["reason"])
        api.store.record_waiting(job["id"], None)
        self.assertIsNone(self.get(f"/v1/images/{job['id']}")["waiting"])

    def test_waiting_is_never_published_for_a_job_that_is_not_running(self) -> None:
        job = self.job()
        api.store.record_waiting(job["id"], {"reason": "x", "next_try_at": None, "since": 1.0})
        self.assertIsNone(self.get(f"/v1/images/{job['id']}")["waiting"])

    def test_typed_chat_failures_publish_their_class_and_preserved_evidence(self) -> None:
        for code, failure_class in (
            ("POLICY_REFUSAL", "content_refused"),
            ("INCOMPLETE_OUTPUT", "incomplete_output"),
        ):
            job = self.job("chat", dispatched=True)
            evidence = {
                "partial_response": {"text": "kept words", "finish_reason": "length"},
                "pool_attempts": [{"member": "haiku::x", "outcome": "failed"}],
            }
            api.store.fail(job["id"], code=code, detail="stopped", evidence=evidence)
            body = api._job_envelope(api.store.get(job["id"]))
            self.assertEqual(failure_class, body["failure_class"])
            self.assertEqual(evidence["partial_response"], body["partial_response"])
            self.assertEqual(evidence["pool_attempts"], body["pool_attempts"])


class ScrubTests(_Case):
    def test_no_address_or_hub_route_survives_in_any_published_text(self) -> None:
        for text in ("could not reach the Hub at http://100.103.25.41:47873 (ConnectError)",
                     "lost contact with https://mac.tail1234.ts.net:47873 while polling",
                     "GET 100.103.25.41:47873/api/hub/jobs/abc/items/0",
                     "reconcile at /api/hub/jobs/abc/items/0/reconcile"):
            clean = api.scrub(text)
            self.assertIsNone(re.search(r":\d{4,5}|/api/hub|https?://", clean), (text, clean))

    def test_a_failed_job_and_its_hold_are_scrubbed_but_the_owners_view_is_not(self) -> None:
        job = self.job(dispatched=True)
        api.store.fail(job["id"], code="LOCATION_UNREACHABLE",
                       detail="could not reach the Hub at http://100.103.25.41:47873",
                       recovery_state="unknown_outcome",
                       recovery_detail="reconcile /api/hub/jobs/b/items/0/reconcile")
        body = self.get(f"/v1/images/{job['id']}")
        blob = repr([body["failure_reason"], body["recovery_detail"]])
        self.assertNotRegex(blob, r"47873|/api/hub|100\.103")
        native = api._job_envelope(api.store.get(job["id"]), native=True)
        self.assertIn("47873", native["failure_reason"], "the operator still sees the location")


class LateResultTests(_Case):
    def _hold(self) -> dict:
        job = self.job(dispatched=True)
        api.store.fail(job["id"], code="UNKNOWN_OUTCOME", detail="restarted",
                       recovery_state="unknown_outcome", recovery_detail="restarted")
        return job

    def test_a_result_for_a_held_job_settles_it_succeeded_with_its_url(self) -> None:
        job = self._hold()
        path = base / f"{job['id']}.png"
        path.write_bytes(b"png")
        api.store.succeed(job["id"], asset_path=str(path), media_type="image/png",
                          artifacts=[{"asset_id": "0", "path": str(path),
                                      "media_type": "image/png", "bytes": 3}])
        body = self.get(f"/v1/images/{job['id']}")
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(body["image_url"], f"/v1/images/{job['id']}/file")
        self.assertFalse(body["recovery_required"])
        self.assertIsNone(body["failure_code"])
        self.assertEqual(self.client.get(body["image_url"], headers=READ).content, b"png")

    def test_a_late_result_for_an_ordinary_failure_does_not_reopen_it(self) -> None:
        job = self.job(dispatched=True)
        api.store.fail(job["id"], code="PROVIDER_FAILED", detail="refused")
        api.store.succeed(job["id"], result={"text": "late"})
        self.assertEqual(api.store.get(job["id"])["state"], "failed")

    def test_rows_stranded_before_this_rule_are_settled_when_the_store_opens(self) -> None:
        job = self._hold()
        with api.store._lock:
            api.store._conn.execute(
                "UPDATE jobs SET late_result_state = 'succeeded', asset_path = 'x' WHERE id = ?",
                (job["id"],))
            api.store._conn.commit()
        reopened = JobStore(api.store.path)
        self.addCleanup(reopened.close)
        row = reopened.get(job["id"])
        self.assertEqual(row["state"], "succeeded")
        self.assertFalse(row["recovery_required"])


class PollHintTests(_Case):
    def test_long_lanes_are_not_polled_every_second(self) -> None:
        self.assertEqual(self.get(f"/v1/videos/{self.job('video')['id']}")["poll_after_ms"], 5000)
        self.assertEqual(self.get(f"/v1/images/{self.job('image')['id']}")["poll_after_ms"], 3000)
        self.assertEqual(self.get(f"/v1/chat/responses/{self.job('chat')['id']}")["poll_after_ms"], 1000)


class SeamRouteTests(_Case):
    def test_account_overview_publishes_the_live_job_counts_story_needs(self) -> None:
        before = self.client.get("/v1/account/overview", headers=READ).json()
        self.job("image")
        after = self.client.get("/v1/account/overview", headers=READ).json()
        for key in ("paid_active_jobs", "paid_queued_jobs"):
            self.assertIsInstance(after[key], int)
            self.assertIsInstance(after["admission"][key], int)
        self.assertEqual(after["paid_queued_jobs"], before["paid_queued_jobs"] + 1)

    def test_api_version_is_the_release_not_a_placeholder(self) -> None:
        shipped = (ROOT / "VERSION").read_text().strip()
        self.assertEqual(self.client.get("/api/version").json()["version"], shipped)

    def test_transcription_language_and_word_timestamps_reach_the_job(self) -> None:
        seen: dict = {}

        def submit(modality, model_id, payload, key, caller=None):
            seen.update(payload)
            return {"id": "j", "state": "accepted", "modality": "transcription",
                    "model_id": "m", "provider_id": ""}, True

        quote = self.client.post(
            "/v1/audio/transcriptions/quotes", headers=READ,
            files={"file": ("a.wav", b"audio", "audio/wav")},
            data={"model": "whisper", "language": "km", "word_timestamps": "true"}).json()
        with patch.object(api, "_submit", submit), \
                patch.object(api.catalog, "resolve", return_value=(SimpleNamespace(), {})):
            response = self.client.post(
                f"/v1/audio/transcriptions/quotes/{quote['id']}/confirm",
                headers={**READ, "Idempotency-Key": f"lang-{time.time()}"}, json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual((seen["language"], seen["word_timestamps"]), ("km", True))

    def test_a_video_source_image_is_kept_with_the_job_past_the_upload_window(self) -> None:
        from gateway.adapters import studio_hub

        asset = video_assets.store(b"\x89PNGscene", "s.png", "image/png")
        staged = api._stage_video_reference({"reference_asset_id": asset, "prompt": "x"})
        video_assets._IMAGES.clear()  # the 15 minutes have passed, or the gateway restarted
        request = SimpleNamespace(params=staged)
        entry = studio_hub._video_reference(request)
        self.assertEqual(len(entry), 1)
        self.assertTrue(Path(staged["_reference_path"]).exists())
        row, _ = api.store.submit(modality="video", model_id="m", params=staged,
                                  idempotency_key=f"ref-{time.time()}")
        self.assertIn(staged["_reference_path"], api.store.protected_input_paths(),
                      "the retention sweep must not delete a queued job's source image")

    def test_a_model_whose_lane_publishes_references_is_published_with_them(self) -> None:
        """**Inverted 2026-10-26.** This used to assert the opposite: every reference-capable image
        model was published as taking none, because the gateway served neither half of the feature
        (no upload route, no adapter reading `reference_asset_ids`). Both halves exist now — this is
        the publication half — so a lane whose worker advertises `edit` and states its own limits is
        described by them, and the three answers are a fact the Hub states rather than a gateway
        ceiling."""
        from gateway.catalog import _image_row
        from gateway.providers.definitions import PROVIDERS_BY_ID

        row = _image_row("AITRADER/FLUX2-klein-4B-mlx-4bit", PROVIDERS_BY_ID["studio-hub-local"],
                         {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit",
                          "raw": {"capabilities": ["txt2img", "edit"],
                                  "lane_contract": {"references": {
                                      "min": 1, "max": 2, "max_dimension": 1024}}}})
        self.assertIs(row["supports_reference_images"], True)
        self.assertEqual(row["operations"], ["text-to-image", "image-to-image"])
        self.assertEqual((row["min_reference_images"], row["max_reference_images"],
                          row["reference_image_max_dimension"]), (1, 2, 1024))

    def test_an_old_hub_advertising_the_lane_but_stating_no_limits_publishes_no_numbers(self) -> None:
        """A Hub that advertises `edit` without the `references` block states no figures, and the
        gateway invents none — `None` (unstated), never the removed `1, 4` / `2048` literals."""
        from gateway.catalog import _image_row
        from gateway.providers.definitions import PROVIDERS_BY_ID

        row = _image_row("AITRADER/FLUX2-klein-4B-mlx-4bit", PROVIDERS_BY_ID["studio-hub-local"],
                         {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit",
                          "raw": {"capabilities": ["txt2img", "edit"]}})
        self.assertIs(row["supports_reference_images"], True)
        self.assertIsNone(row["min_reference_images"])
        self.assertIsNone(row["max_reference_images"])
        self.assertIsNone(row["reference_image_max_dimension"])

    def test_a_lane_that_states_text_only_is_still_published_as_taking_none(self) -> None:
        from gateway.catalog import _image_row
        from gateway.providers.definitions import PROVIDERS_BY_ID

        row = _image_row("AITRADER/FLUX2-klein-4B-mlx-4bit", PROVIDERS_BY_ID["studio-hub-local"],
                         {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit",
                          "raw": {"capabilities": ["txt2img"]}})
        self.assertIs(row["supports_reference_images"], False)
        self.assertEqual(row["operations"], ["text-to-image"])
        self.assertEqual((row["min_reference_images"], row["max_reference_images"],
                          row["reference_image_max_dimension"]), (0, 0, None))

    def test_every_uploaded_reference_is_kept_with_the_job(self) -> None:
        """Two references, one job: the second must be protected from the retention sweep too.

        `input_paths_of` used to read a single string per key, so a list of staged inputs protected
        only what a string could hold — nothing — and the sweep was free to delete a queued job's
        characters. `_reference_paths` is one key holding all of them, in order.
        """
        from gateway import image_assets
        import struct

        def _png(width: int) -> bytes:
            return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                    + struct.pack(">II", width, width) + b"\x08\x02\x00\x00\x00" + b"body")

        ids = [image_assets.store(_png(width), f"{width}.png", "image/png")["id"]
               for width in (640, 800)]
        staged = api._stage_image_references({"reference_asset_ids": ids, "prompt": "two of them"})
        self.assertEqual(len(staged["_reference_paths"]), 2)
        self.assertEqual(staged["_reference_digests"], ids)
        self.assertTrue(all(Path(path).exists() for path in staged["_reference_paths"]))
        row, _ = api.store.submit(modality="image", model_id="m", params=staged,
                                  idempotency_key=f"refs-{time.time()}")
        protected = api.store.protected_input_paths()
        for path in staged["_reference_paths"]:
            self.assertIn(path, protected,
                          "the retention sweep must not delete a queued job's reference images")


class VoicePreviewTests(_Case):
    def _voice(self, clip: Path | None) -> voices.Voice:
        return voices.Voice(id="studio-hub-x::v1", provider_id="studio-hub-x", provider_voice_id="v1",
                            display_name="V", source=voices.SOURCE_LOCAL,
                            raw={"reference_path": str(clip)} if clip else {})

    def test_preview_is_offered_only_when_there_is_one_and_it_points_at_a_real_route(self) -> None:
        clip = base / "clip.wav"
        clip.write_bytes(b"RIFF")
        with_clip = voices.story_row(self._voice(clip))
        self.assertIs(with_clip["preview_available"], True)
        self.assertEqual(with_clip["preview_url"], "/v1/voices/studio-hub-x::v1/preview")
        without = voices.story_row(self._voice(base / "missing.wav"))
        self.assertIs(without["preview_available"], False)
        self.assertIsNone(without["preview_url"])

    def test_the_public_preview_route_serves_the_sample_and_404s_without_one(self) -> None:
        good = SimpleNamespace(find=lambda vid: None, preview=lambda vid: (b"audio", "audio/wav"))
        with patch.object(api, "voice_registry", lambda: good):
            response = self.client.get("/v1/voices/studio-hub-x::v1/preview", headers=READ)
        self.assertEqual((response.status_code, response.content), (200, b"audio"))

        def none(vid):
            raise KeyError(vid)

        with patch.object(api, "voice_registry", lambda: SimpleNamespace(find=lambda vid: None, preview=none)):
            self.assertEqual(self.client.get("/v1/voices/nope/preview", headers=READ).status_code, 404)

        # A guest's own voice stays private here until the owner enables it.
        guest = SimpleNamespace(raw={"guest_owner": {"guest_id": "g"}, "guest_status": "ready"})
        private = SimpleNamespace(find=lambda vid: guest, is_enabled=lambda v: False, preview=good.preview)
        with patch.object(api, "voice_registry", lambda: private):
            self.assertEqual(self.client.get("/v1/voices/g::v/preview", headers=READ).status_code, 404)

    def test_the_preview_route_needs_the_key(self) -> None:
        self.assertEqual(self.client.get("/v1/voices/a::b/preview").status_code, 401)


class VoiceLibraryTests(_Case):
    def _library(self):
        voice = voices.Voice(id="elevenlabs-direct::e1", provider_id="elevenlabs-direct",
                             provider_voice_id="e1", display_name="Ada", source=voices.SOURCE_LIBRARY,
                             language="en", preview_url="https://cdn.example/e1.mp3", owned=False,
                             raw={"owner_id": "own9"})
        return [voice]

    def test_browse_add_and_preview_follow_the_shape_story_reads(self) -> None:
        with patch.object(api.registry.credentials, "is_configured", return_value=True), \
                patch("gateway.voices.search_library", lambda *a, **k: self._library()):
            page = self.client.get("/v1/voice-library/elevenlabs?search=ada", headers=READ).json()
        row = page["voices"][0]
        self.assertEqual((row["public_voice_id"], row["name"], row["is_saved"]), ("e1", "Ada", False))
        self.assertEqual(row["preview_url"], "/v1/voice-library/elevenlabs/e1/preview")
        self.assertEqual(page["page"], 0)

        from gateway import admin
        sent = {}

        def imported(body):
            sent.update(body)
            return {"voice": {"id": "elevenlabs-direct::e1"}}

        with patch.object(admin, "import_voice", imported):
            added = self.client.post("/v1/voice-library/elevenlabs/add", headers=READ,
                                     json={"public_voice_id": "e1"}).json()
        self.assertEqual(added["voice_id"], "elevenlabs-direct::e1")
        self.assertEqual((sent["provider_id"], sent["owner_id"]), ("elevenlabs-direct", "own9"))

        class Answer:
            status_code, content, headers = 200, b"mp3", {"content-type": "audio/mpeg"}

        with patch.object(api.httpx, "get", return_value=Answer()) as fetched:
            preview = self.client.get("/v1/voice-library/elevenlabs/e1/preview", headers=READ)
        self.assertEqual((preview.status_code, preview.content), (200, b"mp3"))
        self.assertEqual(fetched.call_args.args[0], "https://cdn.example/e1.mp3")

    def test_an_unknown_library_and_an_unbrowsed_add_are_refused_cleanly(self) -> None:
        self.assertEqual(self.client.get("/v1/voice-library/nope", headers=READ).status_code, 404)
        self.assertEqual(self.client.post("/v1/voice-library/fish/add", headers=READ,
                                          json={"public_voice_id": "never-listed"}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
