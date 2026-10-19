"""A Gateway restart never strands a job (0.2.158).

Every install used to catch jobs mid-send: the app SIGKILLed the backend 2 s after TERM while a
Hub submit or a cloud call was still waiting for its id, and the next start held it
`UNKNOWN_OUTCOME` for the owner to resolve (0.2.153: 2, 0.2.156: 10, 0.2.157: 1). Now a stop takes
no new work, waits (bounded) for every in-flight send to get an id the next start can follow,
and leaves queued jobs queued.

    .venv/bin/python tests/test_lossless_restart.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-lossless-restart-")
os.environ["GATEWAY_HOST"] = "127.0.0.1"

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.runtime import worker as live_worker  # noqa: E402
from gateway.store import JobStore  # noqa: E402

_ROOT = Path(_SANDBOX.name)


class _Registry:
    """Every provider is a Studio Hub location, whose lane can be followed by id after a restart."""

    @staticmethod
    def definitions():
        return []

    @staticmethod
    def definition(provider_id: str):
        return SimpleNamespace(id=provider_id, kind="studio_hub", base_url="http://hub.invalid",
                               display_name="Hub")


class _Sends:
    """A fake `execute_job`: dispatch, a submit that takes `send_s`, the Hub's batch id, then a render."""

    def __init__(self, send_s: float) -> None:
        self.send_s = send_s
        self.sending = threading.Event()
        self.rendering = threading.Event()  # set by cleanup: ends the render and any unanswered send
        self.executed: list[str] = []

    def __call__(self, job, store, resume=False):
        self.executed.append(job["id"])
        store.begin_dispatch(job["id"])
        self.sending.set()
        if self.rendering.wait(self.send_s):  # the HTTP submit, answer not back yet
            return
        store.record_upstream_acceptance(job["id"], f"batch-{job['id'][:8]}")
        self.rendering.wait(30)  # rendering on the Hub; a restart resumes it by id


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.path = _ROOT / f"{self._testMethodName}.sqlite3"
        self.store = JobStore(self.path)
        self.addCleanup(self.store.close)
        for item in (patch.object(worker_module, "registry", _Registry()),
                     patch("gateway.fleet.snapshot", lambda **_: {"locations": []})):
            item.start()
            self.addCleanup(item.stop)

    def submit(self, key: str) -> str:
        row, _ = self.store.submit(modality="image", model_id="m", params={}, idempotency_key=key,
                                   provider_id="studio-hub-test")
        return row["id"]

    def start(self, sends: _Sends) -> worker_module.Worker:
        worker = worker_module.Worker(self.store, interval_s=0.02, pool_size=4)
        executing = patch.object(worker_module, "execute_job", sends)
        executing.start()
        self.addCleanup(executing.stop)
        self.addCleanup(sends.rendering.set)
        self.addCleanup(worker.stop)
        worker.start()
        return worker

    def restart(self) -> JobStore:
        """The next start's recovery sweep, on the same database.

        The store the old process held is closed first, because that is what a restart *is*: the
        process that owned the queue is gone, so the next starter is the one that sweeps
        (`JobStore._claim_queue`). Leaving it open modelled two gateways on one database — the bug
        this branch fixes — and the sweep now refuses, correctly, to touch a queue another live
        process owns.
        """
        self.store.close()
        reopened = JobStore(self.path)
        self.addCleanup(reopened.close)
        return reopened


class AnInFlightSendCompletesAndIsNotHeld(_Case):
    def test_a_send_in_progress_at_shutdown_gets_its_id_and_the_restart_resumes_it(self) -> None:
        job_id = self.submit("a")
        sends = _Sends(send_s=1.0)
        worker = self.start(sends)
        self.assertTrue(sends.sending.wait(3))
        started = time.monotonic()
        self.assertEqual(worker.drain(timeout=5), [], "the drain waits for the send")
        self.assertGreater(time.monotonic() - started, 0.5)
        worker.stop()
        after = self.restart()
        job = after.get(job_id)
        self.assertEqual(job["state"], "running", "followed by its batch id, not held")
        self.assertFalse(job["recovery_required"])
        self.assertEqual(after.interrupted, [job_id])

    def test_without_the_drain_the_same_fleet_send_is_left_for_a_re_send(self) -> None:
        """The control: a restart mid-send has no batch id. A fleet send is no longer held (0.2.160):
        the next start re-sends it to the same site with the same key (`test_restart_resend.py`)."""
        job_id = self.submit("b")
        sends = _Sends(send_s=1.0)
        self.start(sends)
        self.assertTrue(sends.sending.wait(3))
        after = self.restart()
        job = after.get(job_id)
        self.assertEqual((job["state"], job["error_code"], job["upstream_job_id"]), ("running", None, None))
        self.assertEqual(after.interrupted, [job_id])


class QueuedJobsStayQueued(_Case):
    def test_a_drain_takes_no_new_work_and_touches_nothing_queued(self) -> None:
        first = self.submit("first")
        sends = _Sends(send_s=0.6)
        worker = self.start(sends)
        self.assertTrue(sends.sending.wait(3))
        done: list[list[str]] = []
        drain = threading.Thread(target=lambda: done.append(worker.drain(timeout=5, pause_s=60)))
        drain.start()
        time.sleep(0.1)
        queued = [self.submit("q1"), self.submit("q2")]  # arrive while draining
        before = {job_id: self.store.get(job_id) for job_id in queued}
        drain.join(6)
        self.assertEqual(done, [[]])
        time.sleep(0.3)  # many dispatch passes
        for job_id in queued:
            self.assertEqual(self.store.get(job_id), before[job_id], "queued and untouched")
        self.assertEqual(sends.executed, [first])
        sends.rendering.set()  # the first job finishes, freeing the site's slot
        worker.resume_dispatch()
        deadline = time.time() + 3
        while len(sends.executed) < 3 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sorted(sends.executed[1:]), sorted(queued), "dispatch resumes afterwards")

    def test_a_job_claimed_but_not_sent_goes_back_to_the_queue(self) -> None:
        job_id = self.submit("claimed")
        sends = _Sends(send_s=0)
        worker = worker_module.Worker(self.store)
        self.assertTrue(self.store.mark_running(job_id))
        worker._paused_until = float("inf")
        with patch.object(worker_module, "execute_job", sends):
            worker._run(self.store.get(job_id))
        job = self.store.get(job_id)
        self.assertEqual((job["state"], job["attempt_count"]), ("accepted", 0))
        self.assertIsNone(job["dispatch_started_at"])
        self.assertEqual(sends.executed, [], "nothing was sent")


class TheDrainIsBounded(_Case):
    def test_a_send_that_never_answers_ends_the_drain_at_its_deadline(self) -> None:
        job_id = self.submit("silent")
        sends = _Sends(send_s=30)
        worker = self.start(sends)
        self.assertTrue(sends.sending.wait(3))
        started = time.monotonic()
        self.assertEqual(worker.drain(timeout=0.5), [job_id])
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_app_waits_longer_than_the_drain(self) -> None:
        swift = (ROOT / "menubar/main.swift").read_text()
        self.assertIn("wait(GatewayController.backendDrainWait)", swift)
        wait = float(swift.split("static let backendDrainWait: Double = ")[1].split("\n")[0])
        self.assertGreaterEqual(wait, api._SHUTDOWN_DRAIN_S + 3)
        self.assertLessEqual(api._SHUTDOWN_DRAIN_S, 30, "a drain is bounded")


class TermStopsDispatchAtOnce(unittest.TestCase):
    def test_term_pauses_dispatch_before_uvicorn_shuts_down(self) -> None:
        import signal
        seen: list[int] = []
        original = signal.signal(signal.SIGTERM, lambda sig, _frame: seen.append(sig))  # uvicorn's
        self.addCleanup(signal.signal, signal.SIGTERM, original)
        self.addCleanup(live_worker.resume_dispatch)
        api._pause_dispatch_on_term()
        self.assertFalse(live_worker.dispatch_paused())
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        self.assertTrue(live_worker.dispatch_paused(), "no new send starts once TERM arrives")
        self.assertEqual(seen, [signal.SIGTERM], "uvicorn's own handler still runs")


class TheDrainRoute(unittest.TestCase):
    def test_drain_reports_safe_pauses_dispatch_and_resumes(self) -> None:
        self.addCleanup(live_worker.resume_dispatch)
        client = TestClient(api.app, client=("127.0.0.1", 50123))
        self.addCleanup(client.close)
        answer = client.post("/admin/api/maintenance/drain", params={"timeout_s": 1, "pause_s": 30})
        self.assertEqual(answer.status_code, 200, answer.text)
        body = answer.json()
        self.assertEqual((body["safe"], body["unsettled"]), (True, []))
        self.assertTrue(live_worker.dispatch_paused())
        self.assertTrue(client.get("/health").json()["worker"]["dispatch_paused"])
        self.assertEqual(client.delete("/admin/api/maintenance/drain").json(), {"dispatch_paused": False})

    def test_the_pause_expires_by_itself(self) -> None:
        self.addCleanup(live_worker.resume_dispatch)
        client = TestClient(api.app, client=("127.0.0.1", 50124))
        self.addCleanup(client.close)
        client.post("/admin/api/maintenance/drain", params={"timeout_s": 0, "pause_s": 1})
        self.assertTrue(live_worker.dispatch_paused())
        time.sleep(1.1)
        self.assertFalse(live_worker.dispatch_paused(), "an aborted install never leaves the queue paused")

    def test_the_route_is_admin_only(self) -> None:
        client = TestClient(api.app, client=("203.0.113.9", 50125))
        self.addCleanup(client.close)
        self.assertIn(client.post("/admin/api/maintenance/drain").status_code, (401, 403, 503))


if __name__ == "__main__":
    unittest.main()
