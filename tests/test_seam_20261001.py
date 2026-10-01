"""Seams found tracing Story Studio -> Gateway -> Hub end to end (audit 2026-10-01).

* The Music catalogue lists YuE2 first and YuE2 refuses a request without lyrics; the row now
  says so (`requires_lyrics`), which is what lets Story skip it for an instrumental top-up.
* A TTS `controls` object never reached an engine: the adapter forwarded it nested, the worker's
  request model reads `temperature` at the top level, and the catalogue published a temperature
  knob on every Hub voice although no lane contract lists one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_studio_hub_contract import (  # noqa: E402  (sets the isolated test profile)
    BATCH, HUB, _Hub, _HubCase, _Registry, _TEMP_ROOT, hub_module, worker_module,
)

from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402

from gateway import api, catalog  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, KIND_OPENAI_COMPATIBLE  # noqa: E402

HUB_DEF = SimpleNamespace(id="studio-hub-local", kind=KIND_STUDIO_HUB, base_url="", display_name="Hub")
CLOUD_DEF = SimpleNamespace(id="elevenlabs-direct", kind=KIND_OPENAI_COMPATIBLE,
                            base_url="https://example.test", display_name="ElevenLabs")


def _contract(*params: dict) -> dict:
    return {"schema": "studiohub.lane-contract.v1", "parameters": list(params)}


YUE2 = _contract(
    {"name": "prompt", "type": "string", "required": True},
    {"name": "lyrics", "type": "string", "required": True,
     "note": "this engine sings; there is no instrumental switch"},
)
STABLE_AUDIO = _contract(
    {"name": "prompt", "type": "string", "required": True},
    {"name": "duration_seconds", "type": "number", "required": False},
)


class MusicLyricsFactsTests(unittest.TestCase):
    def row(self, contract: dict | None) -> dict:
        raw = {"lane_contract": contract} if contract is not None else {}
        return catalog._music_row("m", HUB_DEF, {"raw": raw})

    def test_a_lane_that_requires_lyrics_says_so_and_has_no_instrumental(self) -> None:
        row = self.row(YUE2)
        self.assertIs(row["requires_lyrics"], True)
        self.assertIs(row["supports_vocals"], True)
        self.assertIs(row["supports_instrumental"], False)

    def test_a_lane_without_lyrics_states_nothing_about_them(self) -> None:
        row = self.row(STABLE_AUDIO)
        self.assertNotIn("requires_lyrics", row)
        self.assertNotIn("supports_vocals", row)
        self.assertIsNone(row["supports_instrumental"])  # unstated, not a claim

    def test_a_row_with_no_contract_is_unchanged(self) -> None:
        row = self.row(None)
        self.assertNotIn("requires_lyrics", row)
        self.assertIsNone(row["supports_instrumental"])


class TtsControlsTests(unittest.TestCase):
    def controls(self, definition, contract: dict | None) -> list:
        raw = {"lane_contract": contract} if contract is not None else {}
        return catalog._tts_row("m", definition, {"raw": raw})["controls"]

    def test_a_hub_voice_that_lists_no_temperature_publishes_no_control(self) -> None:
        self.assertEqual(self.controls(HUB_DEF, _contract({"name": "text", "type": "string"})), [])
        self.assertEqual(self.controls(HUB_DEF, None), [])

    def test_a_hub_voice_that_lists_temperature_publishes_its_own_range(self) -> None:
        controls = self.controls(HUB_DEF, _contract(
            {"name": "temperature", "type": "number", "minimum": 0.2, "maximum": 1.5, "default": 0.9}))
        self.assertEqual([c["name"] for c in controls], ["temperature"])
        self.assertEqual((controls[0]["minimum"], controls[0]["maximum"], controls[0]["default"]),
                         (0.2, 1.5, 0.9))

    def test_a_cloud_voice_keeps_the_default_control(self) -> None:
        self.assertEqual([c["name"] for c in self.controls(CLOUD_DEF, None)], ["temperature"])


class ControlsReachTheEngineTests(_HubCase):
    def test_a_story_control_is_forwarded_as_a_parameter_not_a_nested_object(self) -> None:
        hub = _Hub()
        self.generate(hub, self.request(
            modality="tts", params={"text": "hello", "controls": {"temperature": 0.4}}))
        params = hub.payloads[0]["items"][0]["params"]
        self.assertEqual(params.get("temperature"), 0.4)
        self.assertNotIn("controls", params)

    def test_an_explicit_top_level_parameter_wins_over_a_control(self) -> None:
        hub = _Hub()
        self.generate(hub, self.request(
            modality="tts", params={"text": "hi", "temperature": 0.9, "controls": {"temperature": 0.1}}))
        self.assertEqual(hub.payloads[0]["items"][0]["params"]["temperature"], 0.9)


class WaitingForAMacCanBeCancelledTests(_HubCase):
    """A job queued at the Hub is cancellable: the Hub cancels a queued item on the spot."""

    @staticmethod
    def job(**over) -> dict:
        row = {"state": "running", "dispatch_started_at": 1.0, "upstream_job_id": BATCH,
               "cancel_requested": 0,
               "waiting": {"reason": "waiting for a free Mac", "next_try_at": None, "since": 1.0}}
        row.update(over)
        return row

    def test_the_envelope_says_a_waiting_job_can_be_cancelled(self) -> None:
        fields = api._contract_fields(self.job())
        self.assertIs(fields["cancellable"], True)
        self.assertEqual(fields["dispatch_state"], "dispatched")
        self.assertEqual(fields["waiting"]["reason"], "waiting for a free Mac")

    def test_a_rendering_job_is_not_cancellable_now(self) -> None:
        self.assertIs(api._contract_fields(self.job(waiting=None))["cancellable"], False)

    def test_a_cancel_already_requested_is_not_offered_again(self) -> None:
        self.assertIs(api._contract_fields(self.job(cancel_requested=1))["cancellable"], False)

    def test_cancelling_a_queued_item_settles_the_job_cancelled(self) -> None:
        hub = _Hub(states=["queued", "queued", "cancelled"],
                   queued_item={"error": "waiting for a free machine", "retry_at": 9e9})
        store = JobStore(_TEMP_ROOT / "waiting-cancel.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="image", model_id="model/test", params={"prompt": "x"},
                              idempotency_key="waiting-cancel", provider_id="studio-hub-test")
        self.assertTrue(store.mark_running(row["id"]))
        real, polls, fired = hub.handler, [], []

        def handler(request: httpx.Request) -> httpx.Response:
            reply = real(request)
            if request.method == "GET" and request.url.path == f"/api/hub/jobs/{BATCH}":
                polls.append(1)
            # The second poll: the first one has been read, so the job is already "waiting".
            if len(polls) == 2 and not fired:
                fired.append(1)
                seen = store.get(row["id"])
                fields = api._contract_fields(seen)
                self.assertIs(fields["cancellable"], True, "the job is waiting, so it can be cancelled")
                store.cancel(row["id"])
            return reply

        hub.handler = handler
        with patch.object(worker_module, "registry", _Registry()), patch.object(
            worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
        ), patch.object(hub_module, "POLL_INTERVAL_S", 0), self.hub_client(hub):
            worker_module.execute_job(row, store)
        self.assertEqual(store.get(row["id"])["state"], "cancelled")


class PreflightFailuresAreNotHoldsTests(_HubCase):
    """A failure before anything reached the Hub is a plain failure, never "reconcile first".

    `execute_job` marks dispatch started before it calls the adapter, and files a hold whenever
    `upstream_accepted` is `None` after that -- so every adapter refusal that raised before the Hub
    was contacted (aged-out video source image, no Hub running, no upload, a failed voice-reference
    staging) read "Upstream outcome is unknown; reconcile the provider request before retrying" and
    parked the job for an operator, with no batch anywhere to reconcile.
    """

    def run_job(self, modality: str, params: dict, key: str, *, hub: _Hub | None = None,
                registry=None, patch_hub: bool = True):
        hub = hub or _Hub()
        store = JobStore(_TEMP_ROOT / f"{key}.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality=modality, model_id="model/test", params=params,
                              idempotency_key=key, provider_id="studio-hub-test")
        with patch.object(worker_module, "registry", registry or _Registry()), patch.object(
            worker_module, "adapter_for", return_value=hub_module.StudioHubBackend()
        ), patch.object(hub_module, "POLL_INTERVAL_S", 0), self.hub_client(hub):
            worker_module.execute_job(row, store)
        return store.get(row["id"]), hub

    def assertPlain(self, job: dict) -> None:
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["recovery_state"], job["error_detail"])
        self.assertNotIn("Upstream outcome is unknown", job["error_detail"] or "")

    def test_an_aged_out_video_source_image(self) -> None:
        job, hub = self.run_job("video", {"prompt": "x", "reference_asset_id": "0" * 8 + "-0000-0000-0000-" + "0" * 12},
                                "pf-video")
        self.assertPlain(job)
        self.assertEqual(job["error_code"], "INVALID_PARAMS")
        self.assertEqual(hub.calls, [], "nothing may reach the Hub")

    def test_a_transcription_with_no_upload(self) -> None:
        job, hub = self.run_job("transcription", {"_upload_path": "/nonexistent/audio.wav"}, "pf-stt")
        self.assertPlain(job)
        self.assertEqual(hub.calls, [])

    def test_a_hub_that_is_not_running_is_an_unreachable_location(self) -> None:
        class NoBase(_Registry):
            @staticmethod
            def definition(_provider_id):
                return SimpleNamespace(id="studio-hub-test", base_url="", display_name="Hub")

        # No `hub-ready.json` and no override, whatever another test module left in the environment.
        with patch("gateway.hub.hub_base_url", return_value=""):
            job, _ = self.run_job("image", {"prompt": "x"}, "pf-nohub", registry=NoBase())
        # Unreachable is a transient site property: it is deferred (and re-routed when another
        # location offers the model), so the job is waiting, not parked as a hold.
        self.assertIsNone(job["recovery_state"])
        self.assertNotIn("Upstream outcome is unknown", job["error_detail"] or "")
        self.assertIn(job["state"], {"accepted", "failed"})
        if job["state"] == "failed":
            self.assertEqual(job["error_code"], "LOCATION_UNREACHABLE")


class UploadCeilingTests(unittest.TestCase):
    """Story sends up to 100 MiB for subtitles; only a cloud transcription API caps it at 25 MB."""

    def limit(self, resolved) -> int:
        with patch.object(api.catalog, "resolve", return_value=resolved):
            return api.upload_limit_bytes("studio-hub::mlx-community/whisper-large-v3-turbo")

    def test_a_hub_model_takes_what_story_sends(self) -> None:
        self.assertEqual(self.limit((HUB_DEF, {})), 100 * 1024 * 1024)

    def test_a_cloud_model_keeps_the_25_mb_ceiling(self) -> None:
        self.assertEqual(self.limit((CLOUD_DEF, {})), 25 * 1024 * 1024)

    def test_an_unknown_model_gets_the_conservative_ceiling(self) -> None:
        self.assertEqual(self.limit((None, None)), 25 * 1024 * 1024)
        with patch.object(api.catalog, "resolve", side_effect=RuntimeError("boom")):
            self.assertEqual(api.upload_limit_bytes("x"), 25 * 1024 * 1024)
        self.assertEqual(api.upload_limit_bytes(None), 25 * 1024 * 1024)

    def test_the_quote_route_refuses_past_the_models_own_ceiling(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        big = b"\x00" * (26 * 1024 * 1024)
        headers = {"Authorization": "Bearer fixture-key"}
        with patch.object(api.catalog, "resolve", return_value=(CLOUD_DEF, {})):
            refused = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                  files={"file": ("big.mp3", big, "audio/mpeg")}, data={"model": "cloud"})
        self.assertEqual(refused.status_code, 413, refused.text)
        with patch.object(api.catalog, "resolve", return_value=(HUB_DEF, {})):
            accepted = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                   files={"file": ("big.mp3", big, "audio/mpeg")}, data={"model": "hub"})
        self.assertEqual(accepted.status_code, 200, accepted.text)


class ReplayIsAnsweredBeforeRefusalTests(unittest.TestCase):
    """Re-POSTing a key whose job exists returns that job, even if a new one would be refused."""

    def setUp(self) -> None:
        # A private store: the shared `api.store` is closed by other suites in a full discover.
        self.store = JobStore(_TEMP_ROOT / "replay.sqlite3")
        self.addCleanup(self.store.close)
        patcher = patch.object(api, "store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_replay_survives_a_full_queue_and_a_model_that_went_away(self) -> None:
        key = "replay-before-refusal-0001"
        row, created = self.store.submit(modality="tts", model_id="m", params={}, provider_id="p",
                                         idempotency_key=key)
        self.assertTrue(created)
        refuse = api.GatewayError(429, "full", "QUEUE_FULL")
        with patch.object(api, "_refuse_when_the_queue_is_full", side_effect=refuse), patch.object(
            api.catalog, "resolve", return_value=(None, None)
        ), patch.object(api.catalog, "why_no_route", return_value=None):
            again, created_again = api._submit("tts", "studio-hub::gone", {}, key)
        self.assertFalse(created_again)
        self.assertEqual(again["id"], row["id"])

    def test_a_new_key_is_still_refused_when_the_queue_is_full(self) -> None:
        refuse = api.GatewayError(429, "full", "QUEUE_FULL")
        with patch.object(api, "_refuse_when_the_queue_is_full", side_effect=refuse):
            with self.assertRaises(api.GatewayError) as caught:
                api._submit("tts", "studio-hub::m", {}, "a-brand-new-key-0002")
        self.assertEqual(caught.exception.code, "QUEUE_FULL")

    def test_a_key_from_another_modality_is_still_a_conflict(self) -> None:
        key = "replay-other-modality-0003"
        self.store.submit(modality="image", model_id="m", params={}, provider_id="p", idempotency_key=key)
        with self.assertRaises(api.GatewayError) as caught:
            api._submit("tts", "studio-hub::m", {}, key)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_REUSED")


class MenuBarCancelReachesTheHubTests(_HubCase):
    """`POST /admin/api/create/status/{id}/cancel` -- the menu-bar Jobs window and the Create page --
    only stamped the cancel locally; a dispatched job kept rendering on the Hub."""

    def test_a_dispatched_job_cancelled_from_the_app_gets_a_hub_delete(self) -> None:
        from fastapi.testclient import TestClient
        from gateway import worker as gateway_worker

        from gateway import admin

        hub = _Hub()
        store = JobStore(_TEMP_ROOT / "admin-cancel.sqlite3")
        self.addCleanup(store.close)
        for module in (api, admin):
            patcher = patch.object(module, "store", store)
            patcher.start()
            self.addCleanup(patcher.stop)
        row, _ = store.submit(modality="image", model_id="model/test", params={}, provider_id="studio-hub-test",
                              idempotency_key="admin-cancel-0001")
        store.mark_running(row["id"])
        store.begin_dispatch(row["id"])
        store.record_upstream_acceptance(row["id"], BATCH)
        definition = SimpleNamespace(id="studio-hub-test", kind="studio_hub", base_url=HUB, display_name="Hub")
        registry = SimpleNamespace(
            definition=lambda _p: definition,
            credentials=SimpleNamespace(resolved=lambda _p: {"hub_token": "t"}),
        )
        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        with patch.object(gateway_worker, "registry", registry), patch.object(
            gateway_worker, "adapter_for", return_value=hub_module.StudioHubBackend()
        ), self.hub_client(hub):
            answer = client.post(f"/admin/api/create/status/{row['id']}/cancel")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertTrue(answer.json()["cancel_requested"])
        self.assertTrue((store.get(row["id"]).get("cancel_remote") or {}).get("ok"))


class RefusalCarriesTheHubsOwnSentenceTests(_HubCase):
    """The code says what kind of refusal; the Hub's `detail` says why. The owner needs the second."""

    def refusal(self, modality: str, body: dict, **params) -> str:
        hub = _Hub(submit_refusal=body)
        with self.assertRaises(hub_module.BackendError) as caught:
            self.generate(hub, self.request(modality=modality, params={"prompt": "x", "text": "x", **params}))
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        return str(caught.exception)

    def test_a_batch_refusal_keeps_the_reason(self) -> None:
        text = self.refusal("image", {"detail": {"code": "OUTPUT_STORAGE_BLOCKED",
                                                  "detail": "managed output storage has insufficient free space"}})
        self.assertIn("OUTPUT_STORAGE_BLOCKED", text)
        self.assertIn("insufficient free space", text)

    def test_a_refusal_with_no_reason_reads_as_before(self) -> None:
        text = self.refusal("tts", {"detail": {"code": "MODEL_NOT_READY"}})
        self.assertEqual(text, "the location cannot serve this model right now (MODEL_NOT_READY)")

    def test_a_peer_shaped_refusal_keeps_the_reason_too(self) -> None:
        self.assertIn("reserved", self.refusal("tts", {"code": "MACHINE_BUSY", "detail": "local is reserved for other work"}))


if __name__ == "__main__":
    unittest.main()
