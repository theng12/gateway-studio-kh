#!/usr/bin/env python3
"""Q8 — a hold is closed when the **Hub's own record** settles it, and never on a guess.

The owner's answer: *"Yes — auto-close when the Hub's own batch record says finished or never
started."* Both answers are one request away, and the safety of the whole thing is the difference
between them and a third case:

  * **finished** — the batch is terminal, so nothing is running and the hold protects nothing.
  * **never started** — the Hub has never heard of the batch id. The work does not exist, so the
    job goes back in the queue: this is the *same proof* the restart sweep acts on, not an
    inference from silence.
  * **the Hub did not answer** — *left alone*. "I could not ask" is not "it is finished", and a
    hold closed on that would be a re-run of work that may still be rendering.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests._isolation import sandbox_env
_SANDBOX = sandbox_env("gateway-hold-reconciliation-")

from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import studio_hub  # noqa: E402
from gateway.store import JobStore  # noqa: E402


class _Store:
    def __init__(self, holds: list[dict[str, Any]], *, attempts: int = 0) -> None:
        self._holds = holds
        self._attempts = attempts
        self.closed: list[dict[str, Any]] = []

    def holds(self, *, limit: int = 200, sweepable: bool = False, provider_ids=None):
        self.sweepable = sweepable
        return list(self._holds)[:limit]

    def attempts_of(self, job_id: str):
        return [{"location": f"studio-hub-{n}"} for n in range(self._attempts)]

    def close_hold(self, job_id: str, *, resolution: str, detail: str) -> bool:
        self.closed.append({"id": job_id, "resolution": resolution, "detail": detail})
        return True


class _Definition:
    def __init__(self, kind: str = "studio_hub") -> None:
        self.kind = kind
        self.base_url = "http://127.0.0.1:47873"


def _hold(job_id: str = "job-1", **over: Any) -> dict[str, Any]:
    row = {"id": job_id, "provider_id": "studio-hub-abc", "modality": "image",
           "recovery_state": "unknown_outcome",
           "recovery_context": {"batch_id": "batch-1", "item_index": 0}}
    row.update(over)
    return row


class TheHubSettlesTheHoldTest(unittest.TestCase):
    def _run(self, snapshot: Any, *, holds=None, attempts: int = 0, kind: str = "studio_hub"):
        store = _Store(holds if holds is not None else [_hold()], attempts=attempts)
        with patch.object(worker_module.registry, "definition", lambda pid: _Definition(kind)), \
                patch.object(worker_module.registry.credentials, "resolved", lambda pid: {}), \
                patch.object(worker_module.studio_hub, "batch_snapshot",
                             lambda *a, **k: snapshot):
            return store, worker_module.reconcile_holds(store)

    def test_a_finished_batch_closes_the_hold_as_failed(self) -> None:
        store, out = self._run({"known": True, "state": "error",
                                "item": {"error": "the worker ran out of memory"}})
        self.assertEqual(out["closed_finished"], 1)
        self.assertEqual(store.closed[0]["resolution"], "failed")
        self.assertIn("finished with state 'error'", store.closed[0]["detail"])
        self.assertIn("the worker ran out of memory", store.closed[0]["detail"])

    def test_a_done_item_is_adopted_not_closed_as_failed(self) -> None:
        # The Hub rendered it and still holds the artifact (owner decision, 2026-09-30): fetch,
        # verify, save and settle it through the normal done path -- never close it as failed.
        settled: list[str] = []
        with patch.object(worker_module.studio_hub, "adopt_batch", lambda *a, **k: "result"), \
                patch.object(worker_module, "settle_result",
                             lambda job_id, result, store: settled.append((job_id, result))):
            store, out = self._run({"known": True, "state": "done", "item": {"state": "done"}})
        self.assertEqual(settled, [("job-1", "result")])
        self.assertEqual(store.closed, [], "a done item must not be closed as failed")
        self.assertEqual(out["adopted"], 1)

    def test_a_done_item_that_cannot_be_adopted_stays_a_hold(self) -> None:
        def refuse(*a, **k):
            raise RuntimeError("the sha256 did not match")

        with patch.object(worker_module.studio_hub, "adopt_batch", refuse):
            store, out = self._run({"known": True, "state": "done", "item": {"state": "done"}})
        self.assertEqual(store.closed, [])
        self.assertEqual((out["adopted"], out["still_open"]), (0, 1))

    def test_the_sweep_asks_only_for_sweepable_holds(self) -> None:
        store, _ = self._run(None)
        self.assertTrue(store.sweepable)

    def test_a_batch_the_hub_never_heard_of_returns_the_job_to_the_queue(self) -> None:
        store, out = self._run({"known": False}, attempts=0)
        self.assertEqual(out["requeued"], 1)
        self.assertEqual(store.closed[0]["resolution"], "requeued")
        self.assertIn("never heard of batch", store.closed[0]["detail"])
        self.assertIn("proof of no work", store.closed[0]["detail"])

    def test_a_batch_the_hub_never_heard_of_is_failed_once_the_attempts_are_used(self) -> None:
        # The same proof, but there is nothing left to try it on: closed as failed rather than
        # queued again, so it cannot loop.
        store, out = self._run({"known": False}, attempts=3)
        self.assertEqual(out["closed_finished"], 1)
        self.assertEqual(store.closed[0]["resolution"], "failed")
        self.assertIn("used all 3 attempts", store.closed[0]["detail"])

    def test_a_running_batch_is_left_alone(self) -> None:
        store, out = self._run({"known": True, "state": "running", "item": None})
        self.assertEqual(store.closed, [])
        self.assertEqual(out["still_open"], 1)

    def test_a_hub_that_did_not_answer_leaves_the_hold_open(self) -> None:
        # **The line that must never move.** Silence is not absolution: a hold closed here could
        # re-run work that is still rendering, and the owner's rule is no duplicates ever.
        store, out = self._run(None)
        self.assertEqual(store.closed, [], "an unanswered question must not close a hold")
        self.assertEqual(out["still_open"], 1)

    def test_a_cloud_lane_is_not_asked(self) -> None:
        # Only Studio Hub keeps a batch record and publishes a route for it. A cloud hold stays on
        # the operator's discard action, which is the only thing that can settle it.
        store, out = self._run({"known": False}, kind="openai_compat")
        self.assertEqual(store.closed, [])
        self.assertEqual(out["still_open"], 1)

    def test_a_hold_with_no_batch_id_is_left_alone(self) -> None:
        store, out = self._run({"known": False}, holds=[_hold(recovery_context=None)])
        self.assertEqual(store.closed, [])
        self.assertEqual(out["still_open"], 1)

    def test_an_unreadable_table_does_not_raise(self) -> None:
        class Broken:
            def holds(self, **_):
                raise RuntimeError("database is locked")

            def close_hold(self, *a, **k):  # pragma: no cover - never reached
                return True

        self.assertEqual(worker_module.reconcile_holds(Broken())["checked"], 0)


def _snapshot(body: dict[str, Any], *, base: str = "http://hub.test", local: bool = False,
              kind: str = "image", ready: str = ""):
    """`batch_snapshot` against a Hub that answers `body` — the REAL summary shape."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    real = httpx.Client
    with patch.object(studio_hub.httpx, "Client",
                      side_effect=lambda *a, **k: real(transport=httpx.MockTransport(handler))), \
            patch("gateway.hub.hub_base_url", lambda: ready), \
            patch("gateway.hub.hub_token", lambda: "operator-token"):
        return studio_hub.batch_snapshot({}, base, "b1", kind=kind, local=local), seen


class TheRealHubSummaryShapeTest(unittest.TestCase):
    # /api/hub/jobs publishes NO batch-level `state`; the item carries it. A transcription
    # summary rolls up into `status`. (Measured from broker.batch_summary / transcription summary.)
    def test_the_state_comes_from_the_item(self) -> None:
        snap, _ = _snapshot({"id": "b1", "errors": [], "items": [{"index": 0, "state": "error"}]})
        self.assertEqual(snap["state"], "error")

    def test_a_transcription_summary_uses_status(self) -> None:
        snap, _ = _snapshot({"id": "b1", "status": "cancelled", "items": []},
                            kind="transcription")
        self.assertEqual(snap["state"], "cancelled")

    def test_this_macs_own_hub_is_found_at_runtime_and_gets_its_token(self) -> None:
        snap, seen = _snapshot({"items": [{"state": "running"}]}, base="", local=True,
                               ready="http://127.0.0.1:47873")
        self.assertEqual(snap["state"], "running")
        self.assertEqual(str(seen[0].url), "http://127.0.0.1:47873/api/hub/jobs/b1")
        self.assertEqual(seen[0].headers["x-hub-token"], "operator-token")

    def test_a_remote_hold_with_no_address_asks_nobody(self) -> None:
        # Never fall back to *this* Mac's Hub for another location's batch: a 404 there would
        # read as "never started" and re-run work that may be rendering.
        snap, seen = _snapshot({}, base="", local=False, ready="http://127.0.0.1:47873")
        self.assertIsNone(snap)
        self.assertEqual(seen, [])


class TheSweepWindowTest(unittest.TestCase):
    def test_retryable_and_idless_holds_cannot_starve_the_real_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs.sqlite3")
            self.addCleanup(store.close)
            rows = [("real", "unknown_outcome", "batch-1", 1.0),
                    ("chat", "unknown_outcome", "", 5.0),
                    ("retry", "retryable", "batch-2", 6.0)]
            for job_id, recovery, upstream, updated in rows:
                store._conn.execute(
                    "INSERT INTO jobs (id, modality, state, created_at, updated_at, "
                    "recovery_state, upstream_job_id) VALUES (?, 'image', 'failed', 0, ?, ?, ?)",
                    (job_id, updated, recovery, upstream))
            store._conn.commit()
            self.assertEqual([r["id"] for r in store.holds(limit=1, sweepable=True)], ["real"])
            self.assertEqual(len(store.holds()), 3)


class _HubLane:
    id = "studio-hub-abc"
    kind = "studio_hub"
    base_url = "http://127.0.0.1:47873"
    display_name = "Hub Studio KH"


class _CloudLane:
    def __init__(self, provider_id: str) -> None:
        self.id = provider_id
        self.kind = "openai_compatible"
        self.base_url = "https://invalid.test"
        self.display_name = provider_id


class AHoldNothingCanBeAskedAboutGetsACeiling(unittest.TestCase):
    """**An action must end — terminal means terminal** (owner, 2026-11-15).

    A chat call is one request with no durable batch, and a lane with no batch route has nobody to
    ask, so `reconcile_holds` can never settle either: measured live, 2026-11-18, a chat hold
    recorded `failed` at 2026-11-16T20:44Z still answered `state: "running"` more than thirty hours
    later, with `cancellable: false` and no failure code — for ever. A hold no lane can be asked
    about therefore gets its own ceiling, and keeps every trace of itself when that ceiling ends it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(Path(self._tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def _hold(self, *, now: float, held_s: float, modality: str = "chat",
              provider_id: str = _HubLane.id, batch: str = "",
              context: dict[str, Any] | None = None) -> str:
        job, _ = self.store.submit(modality=modality, model_id="m", provider_id=provider_id,
                                   params={}, idempotency_key=None)
        self.store.mark_running(job["id"])
        detail = ("The gateway restarted while this job was in flight, so its upstream outcome is "
                  "unknown.")
        self.store.fail(job["id"], code="UNKNOWN_OUTCOME", detail=detail,
                        recovery_state="unknown_outcome", recovery_detail=detail,
                        recovery_context=context if context is not None
                        else {"request_id": "r-1"})
        held_at = now - held_s
        self.store._conn.execute(
            "UPDATE jobs SET upstream_job_id = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (batch, held_at, held_at, job["id"]))
        self.store._conn.commit()
        return str(job["id"])

    def _run(self, now: float, *, snapshot: Any = None) -> dict[str, int]:
        def definition(provider_id: str):
            return _HubLane() if provider_id == _HubLane.id else _CloudLane(provider_id)

        with patch.object(worker_module.registry, "definitions", lambda: [_HubLane()]), \
                patch.object(worker_module.registry, "definition", definition), \
                patch.object(worker_module.registry.credentials, "resolved", lambda pid: {}), \
                patch.object(worker_module.studio_hub, "batch_snapshot",
                             lambda *a, **k: snapshot):
            return worker_module.reconcile_holds(self.store, now=now)

    def test_a_chat_hold_past_its_ceiling_is_published_terminal_with_a_reason(self) -> None:
        now = time.time()
        ceiling = worker_module.UNASKABLE_HOLD_CEILING_S
        job_id = self._hold(now=now, held_s=ceiling + 24 * 3600)

        out = self._run(now)

        self.assertEqual(out["expired"], 1)
        row = self.store.get(job_id)
        self.assertEqual((row["state"], row["error_code"]), ("failed", "HOLD_EXPIRED"))
        self.assertIsNone(row["recovery_state"], "it stops being a hold, so it stops being live")
        self.assertEqual(row["recovery_context"], {"request_id": "r-1"},
                         "the evidence is kept, unlike a discard")
        self.assertIn("Held as an unknown outcome for", row["error_detail"])
        self.assertIn("no batch record", row["error_detail"])
        self.assertAlmostEqual(row["finished_at"], now - ceiling - 24 * 3600,
                               msg="when it was held is not rewritten to the expiry moment")

        from gateway import api
        story = api._job_envelope(row)
        self.assertEqual(story["state"], "failed", "the caller is told the truth, not 'running'")
        self.assertEqual(story["failure_code"], "HOLD_EXPIRED")
        self.assertIn("nothing could ever settle it", story["failure_reason"])

    def test_a_hold_inside_its_ceiling_still_reads_running(self) -> None:
        now = time.time()
        job_id = self._hold(now=now, held_s=60.0)

        out = self._run(now)

        self.assertEqual(out["expired"], 0)
        row = self.store.get(job_id)
        self.assertEqual(row["recovery_state"], "unknown_outcome")
        from gateway import api
        self.assertEqual(api._job_envelope(row)["state"], "running")

    def test_a_cloud_lane_hold_is_expired_too(self) -> None:
        # A lane with no batch record has nobody to ask, so an upstream id does not save the hold.
        now = time.time()
        job_id = self._hold(now=now, held_s=worker_module.UNASKABLE_HOLD_CEILING_S + 60,
                            modality="image", provider_id="paid", batch="cloud-batch-1")

        out = self._run(now)

        self.assertEqual(out["expired"], 1)
        row = self.store.get(job_id)
        self.assertEqual(row["error_code"], "HOLD_EXPIRED")
        self.assertIn("keeps no batch record", row["error_detail"])

    def test_a_hub_hold_with_no_upstream_id_is_expired(self) -> None:
        # The sweep reads only rows carrying an upstream id, so a hold that names its batch only
        # in the context has no closer either -- nothing may sit there for ever.
        now = time.time()
        job_id = self._hold(now=now, held_s=worker_module.UNASKABLE_HOLD_CEILING_S + 60,
                            modality="image", batch="", context={"batch_id": "batch-ctx"})

        out = self._run(now)

        self.assertEqual(out["expired"], 1)
        self.assertIn("no upstream id", self.store.get(job_id)["error_detail"])

    def test_a_hub_hold_inside_its_ceiling_is_left_to_its_sweep(self) -> None:
        # The Hub answered, and the hold is still inside the ceiling, so the sweep keeps owning it.
        now = time.time()
        job_id = self._hold(now=now, held_s=60.0, modality="image", batch="batch-1")

        out = self._run(now, snapshot={"known": True, "state": "running"})

        self.assertEqual(out["expired"], 0, "inside the ceiling the Hub sweep still owns it")
        self.assertEqual(out["still_open"], 1)
        row = self.store.get(job_id)
        self.assertEqual(row["recovery_state"], "unknown_outcome")
        self.assertEqual(row["error_code"], "UNKNOWN_OUTCOME")

    def test_a_hub_hold_past_its_ceiling_that_the_hub_keeps_reporting_running_expires(self) -> None:
        # The Hub answered and the answer is known and non-terminal, but past the ceiling no answer
        # can still be in flight, so the hold stops being published as running.
        now = time.time()
        job_id = self._hold(now=now, held_s=worker_module.UNASKABLE_HOLD_CEILING_S + 60,
                            modality="image", batch="batch-1")

        out = self._run(now, snapshot={"known": True, "state": "running"})

        self.assertEqual(out["expired"], 1)
        row = self.store.get(job_id)
        self.assertEqual((row["state"], row["error_code"]), ("failed", "HOLD_EXPIRED"))
        self.assertIsNone(row["recovery_state"])
        self.assertEqual(row["recovery_context"], {"request_id": "r-1"},
                         "the evidence is kept, exactly as an unaskable hold keeps it")
        self.assertIn("the Hub still reported 'running'", row["error_detail"])

    def test_a_hub_hold_past_its_ceiling_whose_hub_does_not_answer_is_left_alone(self) -> None:
        # "I could not ask" is not "it is finished": a silent Hub gets no ceiling.
        now = time.time()
        job_id = self._hold(now=now, held_s=worker_module.UNASKABLE_HOLD_CEILING_S + 60,
                            modality="image", batch="batch-1")

        out = self._run(now, snapshot=None)

        self.assertEqual(out["expired"], 0)
        self.assertEqual(out["still_open"], 1)
        row = self.store.get(job_id)
        self.assertEqual(row["recovery_state"], "unknown_outcome")
        self.assertEqual(row["error_code"], "UNKNOWN_OUTCOME")


if __name__ == "__main__":
    unittest.main()
