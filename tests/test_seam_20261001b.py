"""Owner decisions of 2026-10-01 on the seams: the Gateway decides, the Hub is told, Story follows.

* A running narration or music job is cancellable; images, Whisper and chat are not.
* When the Gateway gives up on a job (run budget spent) it sends a real cancel to the Hub and ends
  the job with a typed failure -- no orphaned render, no hold -- unless the Hub cannot say.
* A job waiting in the queue past the ceiling is ended with `QUEUE_TIMEOUT`.
* Published text names a Mac by the owner's label, never `studio-hub-<id>`.
* A retried transcription quote with the same Idempotency-Key is the same quote.
* Hub chat rows publish their structured-output mode; music rows publish their length bounds.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_studio_hub_contract import (  # noqa: E402  (sets the isolated test profile)
    BATCH, HUB, _Hub, _HubCase, _TEMP_ROOT, hub_module, worker_module,
)

import httpx  # noqa: E402

from gateway import api, catalog, concurrency  # noqa: E402
from gateway.adapters.base import BackendError  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB  # noqa: E402
from gateway.store import JobStore  # noqa: E402

HUB_DEF = SimpleNamespace(id="studio-hub-local", kind=KIND_STUDIO_HUB, base_url="", display_name="Hub")


class CancellableWhileRunningTests(unittest.TestCase):
    def fields(self, modality: str, **over) -> dict:
        row = {"state": "running", "modality": modality, "dispatch_started_at": 1.0,
               "upstream_job_id": BATCH, "cancel_requested": 0, "waiting": None}
        row.update(over)
        return api._contract_fields(row)

    def test_a_running_narration_or_music_job_can_be_cancelled(self) -> None:
        for modality in ("tts", "music"):
            self.assertIs(self.fields(modality)["cancellable"], True, modality)

    def test_work_that_cannot_be_stopped_says_so(self) -> None:
        for modality in ("image", "transcription", "chat", "video"):
            self.assertIs(self.fields(modality)["cancellable"], False, modality)

    def test_not_while_a_cancel_is_already_in_flight_or_before_the_hub_has_it(self) -> None:
        self.assertIs(self.fields("tts", cancel_requested=1)["cancellable"], False)
        self.assertIs(self.fields("tts", upstream_job_id=None, dispatch_started_at=1.0)["cancellable"], False)


class GivingUpStopsTheHubTests(_HubCase):
    """`_poll_batch` past its run budget: DELETE, then the worker's own answer."""

    def poll(self, hub: _Hub, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(hub.handler))
        self.addCleanup(client.close)
        with patch.object(hub_module, "POLL_INTERVAL_S", 0.005), patch.object(
            hub_module, "CANCEL_SETTLE_S", 0.3
        ), self.hub_client(hub):
            return hub_module._poll_batch(client, HUB, {}, BATCH, 0, deadline_seconds=0.03, **kwargs)

    def hub_that(self, after_delete: dict | None) -> _Hub:
        hub = _Hub(states=["running"], item={"state": "running"})
        real = hub.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE" and after_delete is not None:
                hub.item = after_delete
            return real(request)

        hub.handler = handler
        return hub

    def test_the_hub_confirms_the_stop_and_the_job_ends_typed_not_held(self) -> None:
        hub = self.hub_that({"state": "cancelled"})
        with self.assertRaises(BackendError) as caught:
            self.poll(hub)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"], "the give-up must reach the Hub")
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertTrue(caught.exception.upstream_settled, "known outcome: no unknown-outcome hold")
        self.assertIn("was stopped there", str(caught.exception))

    def test_a_job_that_finished_while_being_stopped_is_kept(self) -> None:
        hub = self.hub_that({"state": "done", "terminal_result": {"machine_id": "local"}})
        outcome = self.poll(hub)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertEqual(outcome["item"]["state"], "done")

    def test_a_worker_that_never_answers_stays_an_unknown_outcome(self) -> None:
        hub = self.hub_that(None)  # DELETE is accepted but the item never leaves `running`
        with self.assertRaises(BackendError) as caught:
            self.poll(hub)
        self.assertEqual(hub.deletes, [f"/api/hub/jobs/{BATCH}"])
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")
        self.assertIn("did not confirm it stopped", str(caught.exception))

    def test_a_hub_that_refuses_the_cancel_is_unknown_and_not_retried_blindly(self) -> None:
        hub = _Hub(states=["running"], item={"state": "running"}, cancel_status=500)
        with self.assertRaises(BackendError) as caught:
            self.poll(hub)
        self.assertEqual(caught.exception.code, "PROVIDER_SUBMISSION_UNCERTAIN")

    def test_a_deadline_is_not_retried_on_another_mac(self) -> None:
        # Except once for free Studio Hub work (2026-10-05): see test_breeze_deadline.py.
        self.assertIn("DEADLINE_EXCEEDED", worker_module._NO_REROUTE_CODES)


class QueueCeilingTests(unittest.TestCase):
    def test_a_job_that_waited_out_the_ceiling_fails_queue_timeout(self) -> None:
        store = JobStore(_TEMP_ROOT / "queue-ceiling.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="tts", model_id="m", params={}, provider_id="studio-hub-local",
                              idempotency_key="queue-ceiling-1")
        with store._lock:
            store._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                                (time.time() - 3 * 3600 - 60, row["id"]))
            store._conn.commit()
        worker = worker_module.Worker(store)
        self.assertTrue(worker._expired_in_queue(store.get(row["id"])))
        failed = store.get(row["id"])
        self.assertEqual((failed["state"], failed["error_code"]), ("failed", "QUEUE_TIMEOUT"))
        self.assertIn("No Mac was free for 3 hours", failed["error_detail"])
        self.assertNotEqual(failed["recovery_state"], "unknown_outcome", "nothing was dispatched, so nothing to reconcile")

    def test_a_fresh_job_and_a_cloud_job_are_left_alone(self) -> None:
        store = JobStore(_TEMP_ROOT / "queue-ceiling-2.sqlite3")
        self.addCleanup(store.close)
        fresh, _ = store.submit(modality="tts", model_id="m", params={}, provider_id="studio-hub-local",
                                idempotency_key="queue-ceiling-2")
        cloud, _ = store.submit(modality="tts", model_id="m", params={}, provider_id="elevenlabs-direct",
                                idempotency_key="queue-ceiling-3")
        with store._lock:
            store._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                                (time.time() - 9 * 3600, cloud["id"]))
            store._conn.commit()
        worker = worker_module.Worker(store)
        self.assertFalse(worker._expired_in_queue(store.get(fresh["id"])))
        self.assertFalse(worker._expired_in_queue(store.get(cloud["id"])))

    def test_the_ceiling_is_the_owners_setting_not_a_private_adapter_constant(self) -> None:
        """The one number that ends a queued job, read from the one place the owner edits it.

        **Real case:** 146 jobs failed `QUEUE_TIMEOUT` in the seven days to 2026-10-19
        (`d854c394-1d64-49a1-b296-a7bdfcba23a7`, `954add3e-cd30-4d5a-995f-7baae8355009`,
        `d13e1523-234c-433e-82c2-1d51917db49b`, `effd967a-9e5f-4760-b169-8800c8a19108`,
        `8cfed7ed-2734-47db-93cf-3c7d2742d46a`, `883c4a06-5e80-47e0-b06f-fccd76e5e33e`) against
        `studio_hub.QUEUE_WAIT_CEILING_S` — a module constant in the adapter — while the editable
        budget beside it (`concurrency.queue_wait_budget()`, read by the worker for deferrals and
        by the API for `Retry-After`) was `queue.wait_ceiling_seconds`. The owner could edit his
        and be overruled in silence.

        Both readers are asserted against the *same* setting, because a fix in one of them is how
        the two numbers came apart in the first place.
        """
        settings = _TEMP_ROOT / "queue-ceiling-settings.json"
        settings.write_text(json.dumps({"queue": {"wait_ceiling_seconds": 900}}), encoding="utf-8")
        store = JobStore(_TEMP_ROOT / "queue-ceiling-4.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="tts", model_id="m", params={}, provider_id="studio-hub-local",
                              idempotency_key="queue-ceiling-4")
        with store._lock:
            store._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                                (time.time() - 1800, row["id"]))
            store._conn.commit()
        with patch.object(concurrency, "SETTINGS_PATH", settings):
            self.assertEqual(concurrency.queue_wait_budget()["wait_ceiling_seconds"], 900.0)
            self.assertEqual(hub_module.queue_wait_ceiling_s(), 900.0,
                             "the adapter must read the owner's ceiling, not keep its own copy")
            worker = worker_module.Worker(store)
            self.assertTrue(worker._expired_in_queue(store.get(row["id"])),
                            "half an hour is past the owner's 15-minute ceiling")
            failed = store.get(row["id"])
            self.assertEqual((failed["state"], failed["error_code"]), ("failed", "QUEUE_TIMEOUT"))
        self.assertNotIn("3 hours", failed["error_detail"],
                         "the sentence must describe the ceiling that actually applied")

    def test_a_ceiling_the_owner_raised_is_honoured(self) -> None:
        """The same number, the other way: raising it must stop the Gateway ending the job early."""
        settings = _TEMP_ROOT / "queue-ceiling-settings-raised.json"
        settings.write_text(json.dumps({"queue": {"wait_ceiling_seconds": 86400}}), encoding="utf-8")
        store = JobStore(_TEMP_ROOT / "queue-ceiling-5.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="tts", model_id="m", params={}, provider_id="studio-hub-local",
                              idempotency_key="queue-ceiling-5")
        with store._lock:
            store._conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                                (time.time() - 3 * 3600 - 60, row["id"]))
            store._conn.commit()
        with patch.object(concurrency, "SETTINGS_PATH", settings):
            self.assertFalse(worker_module.Worker(store)._expired_in_queue(store.get(row["id"])))
        self.assertEqual(store.get(row["id"])["state"], "accepted")

    def test_a_nonsense_ceiling_falls_back_to_the_shipped_default(self) -> None:
        settings = _TEMP_ROOT / "queue-ceiling-settings-bad.json"
        settings.write_text(json.dumps({"queue": {"wait_ceiling_seconds": -1}}), encoding="utf-8")
        with patch.object(concurrency, "SETTINGS_PATH", settings):
            self.assertEqual(concurrency.queue_wait_budget()["wait_ceiling_seconds"],
                             concurrency.DEFAULT_QUEUE_WAIT_CEILING_S)


class MachineLabelTests(unittest.TestCase):
    def test_a_controller_id_becomes_the_owners_label(self) -> None:
        with patch.object(api.fleet, "label_for_provider",
                          side_effect=lambda pid: "Mac mini 1500" if pid.endswith("9701220bf30f") else None):
            text = api.scrub("tried 2 machines - studio-hub-9701220bf30f (PROVIDER_FAILED); "
                             "studio-hub-de7250e146a7 (LOCATION_NOT_READY)")
        self.assertIn("Mac mini 1500 (PROVIDER_FAILED)", text)
        self.assertIn("a Mac (LOCATION_NOT_READY)", text, "an unnamed controller is 'a Mac', never its id")
        self.assertNotIn("studio-hub-", text)

    def test_the_label_lookup_reads_the_roster(self) -> None:
        with patch.object(api.fleet, "_read", return_value=[{"id": "abc123", "label": "Studio 1200", "url": "http://x"}]):
            self.assertEqual(api.fleet.label_for_provider("studio-hub-abc123"), "Studio 1200")
            self.assertIsNone(api.fleet.label_for_provider("studio-hub-nope"))
            self.assertIsNone(api.fleet.label_for_provider("fish-audio"))


class QuoteIdempotencyTests(unittest.TestCase):
    def test_a_retried_quote_is_the_same_quote_and_holds_one_copy(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": "story-stt-retry-0001"}
        audio = b"\x00" * 4096
        with patch.object(api.catalog, "resolve", return_value=(HUB_DEF, {})):
            first = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                files={"file": ("a.mp3", audio, "audio/mpeg")}, data={"model": "hub"})
            held = len(api._UPLOADS)
            again = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                files={"file": ("a.mp3", audio, "audio/mpeg")}, data={"model": "hub"})
            other = client.post("/v1/audio/transcriptions/quotes",
                                headers={**headers, "Idempotency-Key": "story-stt-retry-0002"},
                                files={"file": ("a.mp3", audio, "audio/mpeg")}, data={"model": "hub"})
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(len(api._UPLOADS), held + 1, "the retry added no second copy; the other key did")
        self.assertNotEqual(first.json()["id"], other.json()["id"])

    def test_a_key_whose_audio_is_gone_makes_a_new_quote(self) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": "story-stt-gone-0001"}
        with patch.object(api.catalog, "resolve", return_value=(HUB_DEF, {})):
            first = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                files={"file": ("a.mp3", b"abc", "audio/mpeg")}, data={"model": "hub"})
            api._UPLOADS.pop(first.json()["id"], None)  # a restart
            second = client.post("/v1/audio/transcriptions/quotes", headers=headers,
                                 files={"file": ("a.mp3", b"abc", "audio/mpeg")}, data={"model": "hub"})
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.status_code, 200)


class CatalogFactsTests(unittest.TestCase):
    def test_a_hub_chat_row_publishes_the_mode_its_lane_states(self) -> None:
        provider = SimpleNamespace(id="studio-hub-local", kind=KIND_STUDIO_HUB, base_url="", display_name="Hub")
        for formats, expected in ((["json_object", "json_schema"], "request_json_schema"),
                                  (["json_object"], "json_object"), ([], None)):
            raw = {"lane_contract": {"parameters": [{"name": "response_format"}],
                                     "structured_output": {"formats": formats}}}
            row = catalog._chat_row("m", provider, {"raw": raw})
            self.assertEqual(row["structured_output_mode"], expected, formats)

    def test_music_rows_publish_the_lanes_length_bounds(self) -> None:
        contract = {"parameters": [{"name": "prompt"}, {"name": "duration_seconds", "minimum": 1,
                                                          "maximum": 380, "default": 30}]}
        row = catalog._music_row("m", HUB_DEF, {"raw": {"lane_contract": contract}})
        self.assertEqual((row["min_duration_seconds"], row["max_duration_seconds"],
                          row["default_duration_seconds"]), (1, 380, 30))
        bare = catalog._music_row("m", HUB_DEF, {"raw": {}})
        self.assertNotIn("max_duration_seconds", bare)

    def test_the_declared_hub_version_matches_the_fleet(self) -> None:
        self.assertEqual(hub_module.SUPPORTED_STUDIO_HUB_VERSION, "0.6.126")


if __name__ == "__main__":
    unittest.main()
