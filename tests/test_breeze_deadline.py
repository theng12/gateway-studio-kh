"""Breeze night-atlas failures (2026-10-05): a per-Mac narration deadline, one free move after it, and
the failed item's Mac kept on the record.

* D - a narration's run allowance is `ceil(chars / 8.6) x RTF x 3 + 300` s, clamped to [the lane's
  stated deadline, 7,200 s]; RTF is the running Mac's measured median for the model, else its Mac
  class's, else 5.22. It is re-read once the Hub names the Mac.
* E - a free Studio Hub job that ran past it moves to another location once, without spending the
  retry ceiling; a second deadline fails; paid lanes never move.
* H - a failed Hub item's machine, batch and run seconds are stored and published by the job API.

Mocked Hub transport and fake adapters only: no live Hub, no generation.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from test_free_location_reroute import A, B, _Case, _Fleet, _Registry, _SANDBOX  # sets the sandbox env  # noqa: E501

from gateway import api, concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendRequest  # noqa: E402
from gateway.store import JobStore  # noqa: E402


def _tts(chars: int) -> BackendRequest:
    return BackendRequest(provider_id=A, model_id="breeze", modality="tts",
                          params={"text": "a" * chars}, values={}, base_url="http://hub-a.test")


class PerMacDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(Path(_SANDBOX.name) / f"{self.id().split('.')[-1]}.sqlite3")
        self.addCleanup(self.store.close)
        self.locations: list[dict] = []
        for patcher in (patch("gateway.runtime.store", self.store),
                        patch("gateway.fleet.snapshot", lambda *a, **k: {"locations": self.locations})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def finished(self, provider: str, machine: str, seconds: float, audio_s: float) -> None:
        row, _ = self.store.submit(modality="tts", model_id="breeze", params={}, provider_id=provider,
                                   idempotency_key=f"{provider}-{machine}-{seconds}-{audio_s}")
        self.store.succeed(row["id"], result={"payload": {
            "hub_machine": machine, "generation_seconds": seconds, "audio_duration_s": audio_s}})

    def deadline(self, chars: int, stated: float = 2700, machine: str | None = None) -> float:
        from gateway.providers import registry

        raw = {"lane_contract": {"deadline_seconds": stated}}
        with patch.object(registry.models, "models_for", return_value=[{"model_id": "breeze", "raw": raw}]):
            return hub_module.lane_deadline_seconds(_tts(chars), machine)

    def test_an_unnamed_mac_is_given_the_slowest_class_measured(self) -> None:
        # 1,500 characters: ceil(1500 / 8.6) = 175 audio s x 5.22 x 3 + 300.
        self.assertAlmostEqual(self.deadline(1500), 175 * 5.22 * 3 + 300)
        self.assertGreater(self.deadline(1500), 1747, "covers the measured M1 16 GB tail")

    def test_it_is_clamped_to_the_lane_number_and_to_two_hours(self) -> None:
        self.assertEqual(self.deadline(100), 2700, "a short part keeps the lane's number")
        self.assertEqual(self.deadline(40_000), 7200)
        self.assertEqual(self.deadline(100, stated=1800), 1800, "an older Hub's lane number is the floor")
        self.assertEqual(hub_module._tts_length_deadline(
            BackendRequest(provider_id=A, model_id="m", modality="image", params={"text": "a" * 9000},
                           values={}, base_url="")), 0.0)

    def test_the_mac_running_it_sets_the_speed(self) -> None:
        self.finished(A, "m4-01", 315, 100)
        self.finished(A, "m4-01", 300, 100)
        self.finished(A, "m4-01", 330, 100)
        self.finished(B, "m4-01", 999, 100)  # same name at another location: not this Mac
        self.assertAlmostEqual(hub_module._tts_rtf(A, "breeze", "m4-01"), 3.15)
        self.assertAlmostEqual(self.deadline(3000, machine="m4-01"), 349 * 3.15 * 3 + 300)

    def test_an_unmeasured_mac_takes_its_class_then_the_fallback(self) -> None:
        self.locations = [{"provider_id": A, "machines": [
            {"machine": "m1-01", "chip": "Apple M1", "memory_gb": 16},
            {"machine": "m1-02", "chip": "Apple M1", "memory_gb": 17.18},
            {"machine": "m4-01", "chip": "Apple M4", "memory_gb": 16}]}]
        self.finished(A, "m1-01", 500, 100)
        self.finished(A, "m4-01", 300, 100)
        self.assertAlmostEqual(hub_module._tts_rtf(A, "breeze", "m1-02"), 5.0, msg="M1 16 GB class")
        self.assertEqual(hub_module._tts_rtf(A, "breeze", "unseen"), hub_module.TTS_FALLBACK_RTF)
        self.assertEqual(hub_module._tts_rtf(A, "other-model", "m1-01"), hub_module.TTS_FALLBACK_RTF)


class Yue2DeadlineTests(unittest.TestCase):
    """0.2.174 (owner 2026-10-12): a YuE2 song's allowance = 2 x (slowest s per song-second x length + load)."""

    def deadline(self, params: dict, model: str = "vanch007/mlx-Yue2-3B", stated: float = 900) -> float:
        from gateway.providers import registry

        raw = {"lane_contract": {"deadline_seconds": stated}}
        request = BackendRequest(provider_id=A, model_id=model, modality="music", params=params,
                                 values={}, base_url="http://hub-a.test")
        with patch.object(registry.models, "models_for", return_value=[{"model_id": model, "raw": raw}]):
            return hub_module.lane_deadline_seconds(request)

    def test_a_song_gets_its_own_allowance_and_other_music_keeps_the_lane_number(self) -> None:
        want = 2 * (hub_module.YUE2_SLOWEST_S_PER_SONG_S * 210 + hub_module.YUE2_SLOWEST_LOAD_S)
        self.assertEqual(self.deadline({"duration_seconds": 210}), want)
        self.assertGreater(want, 900)
        self.assertEqual(self.deadline({"duration_seconds": 210}, model="stabilityai/stable-audio-3-optimized"), 900)
        self.assertEqual(self.deadline({}, stated=900), max(900, 2 * hub_module.YUE2_SLOWEST_LOAD_S))


class PollRereadsTheLimitTests(unittest.TestCase):
    def poll(self, items: list[dict], deadline_for_machine):
        answers = list(items)

        def handler(request: httpx.Request) -> httpx.Response:
            item = answers.pop(0) if len(answers) > 1 else answers[0]
            return httpx.Response(200, json={"items": [item]})

        def cancel(*_a):  # the Hub confirms the stop; the worker answers `cancelled`
            answers[:] = [{"state": "cancelled", "studio": "voice@m1-01"}]
            return {"ok": True}

        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        progress = hub_module.HubProgress()
        with patch.object(hub_module, "POLL_INTERVAL_S", 0.005), \
                patch.object(hub_module, "CANCEL_SETTLE_S", 0.3), \
                patch.object(hub_module, "_cancel_batch", cancel):
            try:
                return hub_module._poll_batch(client, "http://hub-a.test", {}, "batch-a", 0,
                                              deadline_seconds=60, progress=progress,
                                              deadline_for_machine=deadline_for_machine), progress
            except BackendError as exc:
                exc.progress = progress
                raise

    def test_the_limit_is_read_again_once_the_hub_names_the_mac(self) -> None:
        asked: list[str] = []
        with self.assertRaises(BackendError) as caught:
            self.poll([{"state": "queued"}, {"state": "running", "studio": "voice@m1-01"}],
                      lambda machine: asked.append(machine) or 0.05)
        self.assertEqual(asked, ["m1-01"], "once, with the Hub's own name for the Mac")
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertIn("on the Mac m1-01", str(caught.exception))
        self.assertEqual(caught.exception.progress.evidence("batch-a")["hub_machine"], "m1-01")

    def test_a_faster_mac_answer_does_not_cut_a_finished_job(self) -> None:
        outcome, progress = self.poll([{"state": "running", "machine": "local"}, {"state": "done"}],
                                      lambda machine: 5000)
        self.assertEqual(outcome["item"]["state"], "done")
        self.assertEqual(progress.machine, "local")

    # Hub 0.6.159 (owner 2026-10-08): an Image item states its own limit (steps x that Mac's speed).
    def test_an_items_own_deadline_raises_the_lanes_number(self) -> None:
        outcome, _ = self.poll([{"state": "running", "machine": "m1-01", "elapsed_s": 100,
                                 "deadline_s": 3000}, {"state": "done"}], None)
        self.assertEqual(outcome["item"]["state"], "done", "100 s is past the lane's 60 s, not 3,000")

    def test_an_items_own_deadline_is_enforced(self) -> None:
        with self.assertRaises(BackendError) as caught:
            self.poll([{"state": "running", "machine": "m1-01", "elapsed_s": 3001,
                        "deadline_s": 3000}], None)
        self.assertEqual(caught.exception.code, "DEADLINE_EXCEEDED")
        self.assertIn("3000s", str(caught.exception))

    def test_a_smaller_or_unusable_item_deadline_never_lowers_the_lanes_number(self) -> None:
        for stated in (10, 0, -5, True, "9999", float("inf")):
            with self.subTest(stated=stated):
                outcome, _ = self.poll([{"state": "running", "machine": "m1-01", "elapsed_s": 30,
                                         "deadline_s": stated}, {"state": "done"}], None)
                self.assertEqual(outcome["item"]["state"], "done")


class _DeadlineAt:
    """A Studio Hub adapter whose Macs at `slow` run past the deadline; elsewhere it finishes."""

    def __init__(self, slow: set[str]) -> None:
        self.slow, self.calls = slow, []

    def generate(self, request):
        self.calls.append(request.provider_id)
        if request.provider_id in self.slow:
            exc = BackendError("the job ran past its 3040s limit on the Mac m1-01 and was stopped there",
                               code="DEADLINE_EXCEEDED", upstream_settled=True)
            exc.evidence = {"hub_batch_id": "batch-a", "hub_machine": "m1-01", "run_seconds": 3040.2}
            raise exc
        from gateway.adapters.base import BackendResult
        return BackendResult(content=b"RIFF\x24\x00\x00\x00WAVEfmt ", media_type="audio/wav",
                             payload={"hub_machine": "m4-01", "hub_batch_id": "batch-b"})


class OneFreeMoveAfterADeadlineTests(_Case):
    modality = "tts"

    def run_with(self, adapter, row, registry=None):
        with patch.object(worker_module, "adapter_for", lambda _d: adapter):
            return self.run_job(_Fleet(a_dies=False), row, registry)

    def test_a_free_job_moves_once_without_spending_the_retry_ceiling(self) -> None:
        one = dict(concurrency.retry_policy(), max_attempts=1)
        adapter = _DeadlineAt({A})
        with patch.object(worker_module.concurrency, "retry_policy", lambda: one):
            job = self.run_with(adapter, self.job("deadline-1", {"text": "x"}))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertEqual(adapter.calls, [A, B])
        self.assertEqual(self.routed, [{"a"}], "the slow location is excluded")
        self.assertEqual([a["code"] for a in self.store.attempts_of(job["id"])], ["DEADLINE_EXCEEDED"])

    def test_a_second_deadline_fails_and_is_not_moved_again(self) -> None:
        self.next_site = [B, A]
        adapter = _DeadlineAt({A, B})
        job = self.run_with(adapter, self.job("deadline-2", {"text": "x"}))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "DEADLINE_EXCEEDED"))
        self.assertEqual(adapter.calls, [A, B], "one move only")
        self.assertFalse(job["recovery_required"], "a stopped job is a known failure, not a hold")

    def test_a_paid_lane_is_never_moved_on_a_deadline(self) -> None:
        adapter = _DeadlineAt({A, B})
        job = self.run_with(adapter, self.job("deadline-paid", {"text": "x"}),
                            _Registry({A: "openai_compat", B: "openai_compat"}))
        self.assertEqual((job["state"], adapter.calls, self.routed), ("failed", [A], []))

    def test_the_failed_items_mac_and_time_are_kept_and_published(self) -> None:
        self.next_site = []
        job = self.run_with(_DeadlineAt({A}), self.job("deadline-3", {"text": "x"}))
        self.assertEqual(job["state"], "failed")
        payload = json.loads(job["result_json"])["payload"]
        self.assertEqual(payload, {"hub_batch_id": "batch-a", "hub_machine": "m1-01", "run_seconds": 3040.2})
        body = api._job_envelope(job)
        self.assertEqual((body["hub_machine"], body["hub_batch_id"], body["run_seconds"]),
                         ("m1-01", "batch-a", 3040.2))
        self.assertEqual(body["failure_code"], "DEADLINE_EXCEEDED")
        machines = {row["key"]: row for row in self.store.generation_analytics()["by_machine"]}
        self.assertEqual(machines[f"m1-01 @ {A}"]["failed"], 1, "the failure is placed on its Mac")


class AdapterKeepsTheFailedItemsMacTests(unittest.TestCase):
    def test_a_hub_item_error_carries_its_mac_and_batch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"batch_id": "batch-a"})
            return httpx.Response(200, json={"items": [{"state": "error", "machine": "m1-01",
                                                        "error": "engine fault"}]})

        real = httpx.Client

        def factory(*a, **k):
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(handler), **k)

        request = BackendRequest(provider_id=A, model_id="breeze", modality="tts",
                                 params={"text": "hello", "_job_id": "job-1"}, values={"hub_token": "t"},
                                 base_url="https://hub-a.test")
        with patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                self.assertRaises(BackendError) as caught:
            hub_module.StudioHubBackend().generate(request)
        self.assertEqual(caught.exception.evidence["hub_batch_id"], "batch-a")
        self.assertEqual(caught.exception.evidence["hub_machine"], "m1-01")
        self.assertIsInstance(caught.exception.evidence["run_seconds"], float)


if __name__ == "__main__":
    unittest.main()
