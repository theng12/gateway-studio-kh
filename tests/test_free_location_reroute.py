#!/usr/bin/env python3
"""A free Studio Hub location that goes silent mid-job is moved, not held.

The unknown-outcome hold exists so a *paid* provider is never billed twice. A Studio Hub location is
the owner's own Mac: re-running costs nothing, while a hold for a dead Mac strands the render
(owner decision 2026-10-03). So a location that stays silent past the adapter's grace
(`POLL_DROP_GRACE_S`, marked `recovery.lost_contact`) sends the job to the next location offering the
model and cancels the abandoned attempt best effort - and everything that is *not* that stays what it
was: paid lanes hold, a pinned voice holds, a Hub-reported `uncertain` item holds, a gateway restart
resumes the batch it was following.

Mocked Hub transport and fake adapters only: no live Hub, no generation, no installed app.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402

from gateway import concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import base as adapter_base  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendResult  # noqa: E402
from gateway.store import JobStore  # noqa: E402

A, B = "studio-hub-a", "studio-hub-b"
#: A real PNG header: the Gateway refuses an artifact that is not the file its type says.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
BASE = {A: "https://hub-a.test", B: "https://hub-b.test"}


class _Fleet:
    """Two Hubs. `a_dies` makes A accept the batch and then go silent, as a Mac that lost power."""

    def __init__(self, *, a_dies: bool = True, a_states: list[str] | None = None,
                 a_blips: int = 0, a_404: bool = False, a_submit: list[str] | None = None) -> None:
        self.a_dies, self.a_states, self.a_blips, self.a_404 = a_dies, list(a_states or ["done"]), a_blips, a_404
        #: How A answers its next submits: "timeout" (never accepted), "lost" (accepted, answer lost),
        #: "503" (refused at the door), "timeout-forever"; then normally.
        self.a_submit = list(a_submit or [])
        self.batches: dict[str, str] = {}  # clientRequestId -> batch id, as the Hub's replay table
        self.created: dict[str, int] = {"a": 0, "b": 0}
        self.calls: list[tuple[str, str, str]] = []  # (host, method, path)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = "a" if "hub-a" in request.url.host else "b"
        method, path = request.method, request.url.path
        self.calls.append((host, method, path))
        batch = f"batch-{host}"
        if method == "POST" and path == "/api/hub/jobs":
            mode = (self.a_submit.pop(0) if len(self.a_submit) > 1 or (self.a_submit and self.a_submit[0] != "timeout-forever")
                    else self.a_submit[0] if self.a_submit else "ok") if host == "a" else "ok"
            if mode == "timeout":
                raise httpx.ReadTimeout("timed out")
            if mode == "503":
                return httpx.Response(503, json={"detail": "model readiness could not be verified on this Mac"})
            if mode == "timeout-forever":
                raise httpx.ReadTimeout("timed out")
            key = json.loads(request.content)["clientRequestId"]
            replayed = f"{host}:{key}" in self.batches
            if not replayed:
                self.batches[f"{host}:{key}"] = batch
                self.created[host] += 1
            if mode == "lost":
                raise httpx.ReadTimeout("timed out")  # accepted, but the answer never came
            return httpx.Response(200, json={"batch_id": batch, "replayed": replayed})
        if method == "POST" and path.endswith("/ack"):
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and path.endswith("/artifact"):
            return httpx.Response(200, content=PNG + f"bytes-from-{host}".encode(),
                                  headers={"content-type": "image/png"})
        if host == "a" and (self.a_dies or self.a_404):
            if self.a_404 and method == "GET":
                return httpx.Response(404, json={"detail": "no such batch"})
            raise httpx.ConnectError("no route to host")  # the Mac is gone (cancel included)
        if method == "GET" and path == f"/api/hub/jobs/{batch}":
            if host == "a" and self.a_blips > 0:
                self.a_blips -= 1
                raise httpx.ConnectError("blip")
            states = self.a_states if host == "a" else ["done"]
            state = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json={"items": [{"index": 0, "state": state,
                                                        "terminal_result": {"machine_id": host}}]})
        if method == "DELETE":
            return httpx.Response(200, json={"ok": True, "queued_cancelled": 0,
                                             "running_signalled": 1, "running_pending": 0})
        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})

    def submits(self, host: str) -> list[str]:
        return [p for p in self.saw(host, "POST") if p == "/api/hub/jobs"]

    def saw(self, host: str, method: str) -> list[str]:
        return [path for h, m, path in self.calls if (h, m) == (host, method)]


class _Registry:
    def __init__(self, kind_of: dict[str, str] | None = None) -> None:
        self.kind_of = kind_of or {A: "studio_hub", B: "studio_hub"}
        self.credentials = SimpleNamespace(is_enabled=lambda _p: True, resolved=lambda _p: {})
        self.models = SimpleNamespace(enabled_models=lambda _m: [
            (SimpleNamespace(id=pid), {"model_id": "model/test"}) for pid in self.kind_of])

    def definition(self, provider_id: str) -> Any:
        return SimpleNamespace(id=provider_id, kind=self.kind_of[provider_id],
                               base_url=BASE.get(provider_id, ""), display_name=provider_id)


class _Case(unittest.TestCase):
    modality = "image"

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(_SANDBOX.name) / f"{self.id().split('.')[-1]}.sqlite3")
        self.addCleanup(self.store.close)
        self.routed: list[set[str]] = []
        self.next_site: list[str] = [B]

    def route(self, model_id, modality, *, exclude_locations=None, **_k):
        self.routed.append(set(exclude_locations or ()))
        return (SimpleNamespace(id=self.next_site.pop(0)), "") if self.next_site else (None, "")

    def job(self, key: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        row, _ = self.store.submit(modality=self.modality, model_id="model/test",
                                   params=params or {"prompt": "x"}, idempotency_key=key,
                                   provider_id=A)
        return row

    def run_job(self, fleet: _Fleet, row: dict[str, Any], registry: Any = None) -> dict[str, Any]:
        real = httpx.Client

        def factory(*a: Any, **k: Any) -> httpx.Client:
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fleet.handler), **k)

        with patch.object(worker_module, "registry", registry or _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "POLL_DROP_GRACE_S", 0.05):
            worker_module.execute_job(row, self.store)
            self.wait_for(lambda: not any(t.name.startswith("gateway-cancel") and t.is_alive()
                                          for t in __import__("threading").enumerate()))
        return self.store.get(row["id"])

    @staticmethod
    def wait_for(predicate, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            time.sleep(0.01)


class SilentFreeLocationIsMovedTests(_Case):
    def test_a_location_that_goes_silent_mid_poll_is_rerouted_and_the_original_cancelled(self) -> None:
        fleet = _Fleet()
        row = self.job("silent-1")
        job = self.run_job(fleet, row)
        self.assertEqual(job["state"], "succeeded", job.get("error_detail"))
        self.assertEqual(Path(job["asset_path"]).read_bytes(), PNG + b"bytes-from-b")
        self.assertEqual(job["provider_id"], B)
        self.assertFalse(job["recovery_required"], "a free location's silence is not a hold")
        self.assertEqual(fleet.submits("b"), ["/api/hub/jobs"], "re-dispatched once, at the other")
        self.assertEqual(fleet.saw("a", "DELETE"), ["/api/hub/jobs/batch-a"],
                         "best-effort cancel at the dead location")
        self.assertEqual(self.routed, [{"a"}], "the silent location is excluded from the re-route")
        self.assertEqual(job["upstream_job_id"], "batch-b", "the job now follows the new batch only")
        self.assertIsNone(job.get("cancel_remote"), "the abandoned attempt's cancel is not the job's")

    def test_a_paid_provider_that_goes_silent_is_still_an_unknown_outcome_hold(self) -> None:
        calls: list[str] = []

        class _Cloud:
            def generate(self, request):
                calls.append(request.provider_id)
                raise BackendError("lost contact with the provider", code="PROVIDER_SUBMISSION_UNCERTAIN",
                                   upstream_accepted=True,
                                   recovery={"batch_id": "task-1", "lost_contact": True})

        row = self.job("paid-1")
        with patch.object(worker_module, "adapter_for", lambda _d: _Cloud()):
            job = self.run_job(_Fleet(), row, _Registry({A: "openai_compat", B: "openai_compat"}))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "PROVIDER_SUBMISSION_UNCERTAIN"))
        self.assertTrue(job["recovery_required"])
        self.assertEqual(calls, [A], "never resubmitted to another provider")
        self.assertEqual(self.routed, [], "a paid lane is never offered a re-route")

    def test_a_pinned_voice_stays_held_it_is_not_moved(self) -> None:
        self.modality = "tts"
        row = self.job("pinned-1", {"text": "x", "voice_id": "studio-hub-a::clone"})
        with patch.object(worker_module.fleet, "pinned_location", lambda _p: A):
            job = self.run_job(_Fleet(), row)
        self.assertTrue(job["recovery_required"])
        self.assertEqual(self.routed, [])

    def test_with_nowhere_else_to_go_it_stays_a_hold_and_nothing_is_cancelled(self) -> None:
        self.next_site = []
        fleet = _Fleet()
        job = self.run_job(fleet, self.job("alone-1"))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "PROVIDER_SUBMISSION_UNCERTAIN"))
        self.assertTrue(job["recovery_required"])
        self.assertEqual(fleet.saw("a", "DELETE"), [], "the original may still finish: not cancelled")

    def test_a_hub_reported_uncertain_item_stays_a_hold(self) -> None:
        fleet = _Fleet(a_dies=False, a_states=["uncertain"])
        job = self.run_job(fleet, self.job("uncertain-1"))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertTrue(job["recovery_required"])
        self.assertEqual(self.routed, [], "the location answered: that is the Hub's call, not a death")

    def test_a_blip_shorter_than_the_grace_is_ridden_out_not_moved(self) -> None:
        fleet = _Fleet(a_dies=False, a_blips=3, a_states=["running", "done"])
        job = self.run_job(fleet, self.job("blip-1"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A))
        self.assertEqual(self.routed, [])


class SubmitThatDoesNotAnswerTests(_Case):
    """Owner, 2026-10-03: Dreamlands chapters ended `failed / SITE_CAPACITY_UNAVAILABLE` ("hub submit
    failed: timed out"). For a free Studio Hub job that is a wait, and never a second run."""

    def run_until_settled(self, fleet: _Fleet, row: dict[str, Any], *, tries: int = 12) -> list[dict[str, Any]]:
        seen = [self.run_job(fleet, row)]
        while seen[-1]["state"] == "accepted" and len(seen) < tries:
            seen.append(self.run_job(fleet, seen[-1]))  # the queue's backoff, not waited for here
        return seen

    def test_a_submit_timeout_waits_in_the_queue_and_succeeds_on_the_retry(self) -> None:
        fleet = _Fleet(a_dies=False, a_submit=["timeout"])
        seen = self.run_until_settled(fleet, self.job("timeout-1"))
        self.assertEqual(seen[0]["state"], "accepted", "queued and waiting, not failed")
        self.assertIsNone(seen[0]["error_code"])
        self.assertIn("timed out", seen[0]["defer_reason"])
        self.assertEqual((seen[-1]["state"], seen[-1]["provider_id"]), ("succeeded", A))
        self.assertFalse(seen[-1]["recovery_required"])
        self.assertEqual(self.routed, [], "replayed at the same location, never offered elsewhere")

    def test_a_503_at_the_door_waits_when_there_is_nowhere_else(self) -> None:
        self.next_site = []
        fleet = _Fleet(a_dies=False, a_submit=["503"])
        seen = self.run_until_settled(fleet, self.job("busy-1"))
        self.assertEqual(seen[0]["state"], "accepted", "queued, not failed")
        self.assertEqual((seen[-1]["state"], seen[-1]["provider_id"]), ("succeeded", A))
        self.assertEqual(fleet.created["a"], 1)

    def test_a_503_at_the_door_moves_to_another_location_when_one_offers_the_model(self) -> None:
        fleet = _Fleet(a_dies=False, a_submit=["503"])
        job = self.run_job(fleet, self.job("busy-2"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertFalse(job["recovery_required"])

    def test_a_submit_that_was_accepted_but_whose_answer_was_lost_is_not_run_twice(self) -> None:
        fleet = _Fleet(a_dies=False, a_submit=["lost"])
        seen = self.run_until_settled(fleet, self.job("lost-1"))
        self.assertEqual(seen[0]["state"], "accepted")
        self.assertEqual((seen[-1]["state"], seen[-1]["provider_id"]), ("succeeded", A))
        self.assertEqual(fleet.created, {"a": 1, "b": 0}, "the replay found the batch the Hub already made")
        self.assertEqual(len(fleet.submits("a")), 2)
        self.assertEqual(Path(seen[-1]["asset_path"]).read_bytes(), PNG + b"bytes-from-a")

    def test_a_location_that_never_answers_its_submit_is_moved_once_the_wait_is_spent(self) -> None:
        fleet = _Fleet(a_dies=False, a_submit=["timeout-forever"])
        # Built from the producer's own shape, so a key added to `queue_wait_budget` cannot
        # make this stub a stricter or looser thing than the function it stands in for.
        budget = {**concurrency.queue_wait_budget(), "max_deferrals": 1, "defer_cap_seconds": 0}
        with patch.object(worker_module.concurrency, "queue_wait_budget", lambda: budget):
            seen = self.run_until_settled(fleet, self.job("forever-1"))
        self.assertEqual((seen[-1]["state"], seen[-1]["provider_id"]), ("succeeded", B), seen[-1].get("error_detail"))
        self.assertEqual(len(fleet.submits("a")), 2, "the first try, then the one replay the budget allows")

    def test_with_nowhere_to_move_it_the_exhausted_wait_is_the_hold_it_always_was(self) -> None:
        self.next_site = []
        # Built from the producer's own shape, so a key added to `queue_wait_budget` cannot
        # make this stub a stricter or looser thing than the function it stands in for.
        budget = {**concurrency.queue_wait_budget(), "max_deferrals": 1, "defer_cap_seconds": 0}
        with patch.object(worker_module.concurrency, "queue_wait_budget", lambda: budget):
            seen = self.run_until_settled(_Fleet(a_dies=False, a_submit=["timeout-forever"]), self.job("forever-2"))
        self.assertEqual((seen[-1]["state"], seen[-1]["error_code"]), ("failed", "SITE_CAPACITY_UNAVAILABLE"))
        self.assertTrue(seen[-1]["recovery_required"])

    def test_a_paid_provider_that_timed_out_is_still_a_hold_and_never_replayed(self) -> None:
        calls: list[str] = []

        class _Cloud:
            def generate(self, request):
                calls.append(request.provider_id)
                adapter_base.note_request_started()  # the request left, and nothing came back
                raise BackendError("hub submit failed: timed out", code="SITE_CAPACITY_UNAVAILABLE",
                                   retryable=True,
                                   recovery={"lost_contact": True, "replayable": True})

        with patch.object(worker_module, "adapter_for", lambda _d: _Cloud()):
            job = self.run_job(_Fleet(), self.job("paid-timeout"),
                               _Registry({A: "openai_compat", B: "openai_compat"}))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "SITE_CAPACITY_UNAVAILABLE"))
        self.assertTrue(job["recovery_required"])
        self.assertEqual((calls, self.routed), ([A], []))


class LateResultTests(_Case):
    def test_a_late_result_from_the_abandoned_attempt_never_changes_the_settled_job(self) -> None:
        job = self.run_job(_Fleet(), self.job("late-1"))
        self.assertEqual(job["state"], "succeeded")
        before = (Path(job["asset_path"]).read_bytes(), job["result"] if "result" in job else None,
                  job["artifacts"] if "artifacts" in job else None)
        delivered: list[bool] = []
        late = BackendResult(content=PNG + b"bytes-from-a", media_type="image/png",
                             payload={"hub_batch_id": "batch-a"},
                             deliver=lambda: delivered.append(True) or {"ok": True})
        worker_module.settle_result(job["id"], late, self.store)
        self.store.succeed(job["id"], asset_path="/elsewhere.png", media_type="image/png",
                           artifacts=[{"asset_id": "0", "path": "/elsewhere.png",
                                       "media_type": "image/png", "bytes": 1}],
                           result={"text": None, "payload": {"hub_batch_id": "batch-a"}})
        after = self.store.get(job["id"])
        self.assertEqual(after["state"], "succeeded")
        self.assertEqual(after["asset_path"], job["asset_path"])
        self.assertEqual(Path(after["asset_path"]).read_bytes(), before[0])
        self.assertEqual(after.get("result"), before[1])
        self.assertEqual(after.get("artifacts"), before[2])
        self.assertEqual(delivered, [], "the late result is not acknowledged either")


class ChatTests(_Case):
    modality = "chat"

    def _post(self, dies: bool):
        posts: list[str] = []

        def post(url: str, **_k: Any) -> httpx.Response:
            adapter_base.note_request_started()
            posts.append(url)
            if "hub-a" in url:
                raise (httpx.ReadTimeout("slow") if dies == "timeout" else httpx.ReadError("reset"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"},
                                                          "finish_reason": "stop"}]},
                                  request=httpx.Request("POST", url))

        return posts, post

    def _run(self, kind: Any) -> tuple[dict[str, Any], list[str]]:
        posts, post = self._post(kind)
        row = self.job("chat-1", {"messages": [{"role": "user", "content": "hello"}]})
        with patch.object(hub_module.httpx, "post", post):
            return self.run_job(_Fleet(), row), posts

    def test_a_chat_whose_location_drops_the_connection_is_rerun_at_another(self) -> None:
        job, posts = self._run("reset")
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertEqual(len(posts), 2)

    def test_a_chat_read_timeout_stays_a_hold_the_model_may_just_be_slow(self) -> None:
        job, posts = self._run("timeout")
        self.assertTrue(job["recovery_required"])
        self.assertEqual(len(posts), 1)


class RestartIsNotADeathTests(_Case):
    """0.2.103's restart-resume: a Hub that answers is followed, never re-dispatched."""

    def _restart(self, fleet: _Fleet, name: str) -> dict[str, Any]:
        row = self.job(name)
        self.assertTrue(self.store.mark_running(row["id"]))
        self.assertTrue(self.store.begin_dispatch(row["id"]))
        self.store.record_upstream_acceptance(row["id"], "batch-a")
        self.store.close()
        reopened = JobStore(self.store.path)
        self.addCleanup(reopened.close)
        self.store = reopened
        real = httpx.Client

        def factory(*a: Any, **k: Any) -> httpx.Client:
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fleet.handler), **k)

        worker = worker_module.Worker(reopened)
        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "POLL_DROP_GRACE_S", 0.05):
            worker._resume_interrupted()
            self.assertTrue(worker.wait_idle(timeout=10))
        return reopened.get(row["id"])

    def test_a_resumed_batch_whose_hub_answers_keeps_following_it(self) -> None:
        fleet = _Fleet(a_dies=False, a_blips=2, a_states=["running", "running", "done"])
        job = self._restart(fleet, "resume-live")
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A), job.get("error_detail"))
        self.assertEqual(Path(job["asset_path"]).read_bytes(), PNG + b"bytes-from-a")
        self.assertEqual(fleet.submits("a") + fleet.submits("b"), [], "never resubmitted")
        self.assertEqual(self.routed, [])

    def test_a_resume_404_is_still_a_hold_not_evidence_the_location_died(self) -> None:
        fleet = _Fleet(a_dies=False, a_404=True)
        job = self._restart(fleet, "resume-404")
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertEqual(job["recovery_context"]["recorded_by"], "gateway_restart")
        self.assertEqual(self.routed, [])


if __name__ == "__main__":
    unittest.main()
