"""A job reaches a terminal state when its upstream stops progressing.

The owner's rule, 2026-11-18: *"a job must reach a terminal state when its upstream stops
progressing."* Measured live on 1301: eleven jobs sat at 137 minutes as `running`,
`retryable: false`, `recovery_required: false`, while everything around them reported fine.

Two ways a `running` row with an upstream batch could sit there for ever, both pinned here:

  * **The Hub says it stalled.** It computes `stalled_after_s` per batch and publishes `stalled`
    and `no_progress_s` on every poll; nothing on this side acted on it, so a batch that stopped
    producing rode to the lane's deadline in silence. The poll now ends at the first stalled
    answer, names the seconds and the threshold, and says what a retry would do.
  * **Nothing is polling it.** The poller died, or a restart could not resume it, and the liveness
    sweep skipped every row with an upstream id. The hold sweep now ends such a row once it is past
    the longest batch lane — after asking the Hub once, so the sentence is its answer.

What must **not** change: a row a poller owns is never touched; a batch that finished is collected,
not failed; and the batch is recorded for the cleanup before the verdict, so "run it again" is only
offered once the old one is stopped.

Run with::

    .venv/bin/python -m unittest tests.test_wedged_runs
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-wedged-runs-")

from gateway import worker  # noqa: E402
from gateway.adapters.base import BackendError  # noqa: E402
from gateway.adapters.studio_hub import HubProgress, _poll_batch  # noqa: E402
from gateway.runtime import store  # noqa: E402

T0 = 1_760_000_000.0
PROVIDER = "studio-hub-site"


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _StalledClient:
    """A Hub that keeps answering `running` and says the batch has stalled."""

    def __init__(self, no_progress: float = 1200.0, after: int = 900) -> None:
        self.no_progress = no_progress
        self.after = after
        self.calls = 0

    def get(self, _url: str, headers=None) -> _Response:  # noqa: ANN001, ARG002
        self.calls += 1
        return _Response({
            "items": [{"state": "running", "elapsed_s": 1300.0}],
            "no_progress_s": self.no_progress,
            "stalled_after_s": self.after,
            "stalled": True,
        })


class TheHubStallEndsThePoll(unittest.TestCase):
    def test_a_stalled_batch_ends_with_the_seconds_and_the_threshold(self):
        progress = HubProgress()
        with self.assertRaises(BackendError) as caught:
            _poll_batch(_StalledClient(), "http://site.test", {}, "batch-1", 0,
                        deadline_seconds=7200.0, progress=progress)
        error = caught.exception
        self.assertEqual(error.code, "UPSTREAM_STALLED")
        # The sentence offers a retry after the cleanup stops the batch, so the flag must say
        # the same thing; only the automatic move to another Mac is withheld.
        self.assertTrue(error.retryable)
        self.assertIn("UPSTREAM_STALLED", worker._NO_REROUTE_CODES)
        self.assertTrue(error.upstream_settled)
        self.assertIn("no progress for 1200 s", str(error))
        self.assertIn("900 s threshold", str(error))
        self.assertIn("running the job again after that is safe", str(error))
        self.assertEqual(error.recovery["batch_id"], "batch-1")
        self.assertEqual(error.recovery["item_index"], 0)

    def test_a_healthy_poll_still_reaches_its_terminal_state(self):
        class _Finishes:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, _url: str, headers=None):  # noqa: ANN001, ARG002
                self.calls += 1
                if self.calls == 1:
                    return _Response({"items": [{"state": "running"}], "no_progress_s": 4.0,
                                      "stalled_after_s": 900, "stalled": False})
                return _Response({"items": [{"state": "done"}]})

        outcome = _poll_batch(_Finishes(), "http://site.test", {}, "batch-2", 0,
                              deadline_seconds=7200.0, progress=HubProgress())
        self.assertEqual(outcome["item"]["state"], "done")


class AQueuedItemIsNeverCalledStalled(unittest.TestCase):
    """Measured 2026-11-19: 127 jobs ended as "the Hub reported this job stalled: no progress for
    901 s" while every one was still **queued** and had never reached a Mac. The Hub's clock
    counts from the batch being made, not from it starting, so a site that never places work
    looks exactly like a render that hung. What the operator needs is the fact underneath."""

    class _Queued:
        def get(self, _url: str, headers=None):  # noqa: ANN001, ARG002
            return _Response({"items": [{"state": "queued"}], "no_progress_s": 901.0,
                              "stalled_after_s": 900, "stalled": True})

    _CANCELLED = {"ok": True, "status": 200, "route": "/api/hub/jobs/batch-q1",
                  "answer": {"ok": True, "queued_cancelled": 1,
                             "running_signalled": 0, "running_pending": 0}}

    def test_it_is_withdrawn_and_the_job_handed_on(self):
        from gateway.adapters import studio_hub
        with patch.object(studio_hub, "_cancel_batch", return_value=self._CANCELLED) as cancel:
            with self.assertRaises(BackendError) as caught:
                _poll_batch(self._Queued(), "http://site.test", {}, "batch-q1", 0,
                            deadline_seconds=7200.0, progress=HubProgress())
        error = caught.exception
        cancel.assert_called_once()
        self.assertEqual(error.code, "NO_AVAILABLE_MACHINE")
        self.assertIn("never put this job on a Mac", str(error))
        self.assertIn("901 s", str(error))
        self.assertTrue(error.retryable)
        self.assertFalse(error.upstream_accepted, "nothing ran, so the job may move")
        self.assertTrue(error.upstream_settled, "the Hub confirmed the withdrawal")

    def test_an_unconfirmed_withdrawal_never_becomes_a_stall_claim(self):
        from gateway.adapters import studio_hub

        class _KeepWaiting(Exception):
            pass

        refused = {"ok": False, "status": None, "route": "/api/hub/jobs/batch-q1",
                   "error": "ConnectTimeout: timed out"}
        with patch.object(studio_hub, "_cancel_batch", return_value=refused), \
                patch.object(studio_hub.time, "sleep", side_effect=_KeepWaiting):
            with self.assertRaises(_KeepWaiting):
                _poll_batch(self._Queued(), "http://site.test", {}, "batch-q1", 0,
                            deadline_seconds=7200.0, progress=HubProgress())

    def test_the_lane_ceiling_travels_with_the_evidence(self):
        # The worker's follow guard needs the limit the poll was given, so that follows cannot
        # multiply the window.
        progress = HubProgress()
        with self.assertRaises(BackendError):
            _poll_batch(_StalledClient(), "http://site.test", {}, "batch-3", 0,
                        deadline_seconds=1800.0, progress=progress)
        self.assertEqual(progress.evidence("batch-3")["lane_ceiling_s"], 1800.0)


class TheBoundIsGroundedInMeasuredDuration(unittest.TestCase):
    """Per machine and per model, from the Hub's own medians -- never one fleet-wide number."""

    SNAP = {
        "machines": [
            {"machine": "slow-m1", "median_runtime_s": 240.0},
            {"machine": "fast-m4", "average_seconds": 60.0},
        ],
        "locations": [{
            "local_models": [{"model": "m", "machine": "slow-m1"},
                             {"model": "m", "machine": "fast-m4"}],
        }],
    }

    def test_a_machine_uses_its_own_measurement(self):
        from gateway import fleet
        with patch.object(fleet, "snapshot", return_value=self.SNAP):
            bound, source = fleet.measured_duration_bound("m", "slow-m1")
            fast, fast_source = fleet.measured_duration_bound("m", "fast-m4")
        self.assertEqual(bound, 900.0, "240 s x 3 is below the floor, so the floor holds")
        self.assertEqual(source, "measured 240 s on this Mac")
        self.assertEqual(fast, 900.0)
        self.assertEqual(fast_source, "measured 60 s on this Mac")

    def test_no_machine_named_uses_the_fleets_usual_figure(self):
        from gateway import fleet
        with patch.object(fleet, "snapshot", return_value=self.SNAP):
            bound, source = fleet.measured_duration_bound("m", None)
        self.assertEqual(source, "the fleet's usual 240 s for this model")
        self.assertEqual(bound, 900.0)

    def test_nothing_measured_means_no_number_is_invented(self):
        from gateway import fleet
        with patch.object(fleet, "snapshot", return_value=self.SNAP):
            bound, source = fleet.measured_duration_bound("never-run", None)
        self.assertEqual((bound, source), (None, ""))

    def test_the_cap_is_the_lanes_ceiling(self):
        from gateway import fleet
        with patch.object(fleet, "snapshot", return_value={
                "machines": [{"machine": "m1", "median_runtime_s": 3600.0}],
                "locations": [{"local_models": [{"model": "m", "machine": "m1"}]}]}):
            bound, _ = fleet.measured_duration_bound("m", "m1", cap_s=7200.0)
        self.assertEqual(bound, 7200.0)


class TheQueueWaitNamesItsBound(unittest.TestCase):
    """A job queued at the Hub says how long it is held to and where that number came from."""

    def test_a_queue_timeout_names_the_figure_and_its_source(self):
        from gateway.adapters import studio_hub

        class _Queued:
            def get(self, _url, headers=None):  # noqa: ANN001, ARG002
                return _Response({"items": [{"state": "queued"}]})

        with patch.object(studio_hub, "queue_wait_ceiling_s", return_value=300.0):
            with self.assertRaises(BackendError) as caught:
                _poll_batch(_Queued(), "http://site.test", {}, "batch-q", 0,
                            deadline_seconds=7200.0, progress=HubProgress(),
                            wait_bound=(1.0, "the fleet's usual 200 s for this model"))
        message = str(caught.exception)
        self.assertIn("held to", message)
        self.assertIn("the fleet's usual 200 s for this model", message)


class TheOrphanRowEnds(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store
        self.store._conn.execute("DELETE FROM jobs")
        self.store._conn.commit()
        self.job_id = "wedged-1"
        self._insert(self.job_id, updated_at=T0 - 8000)

    def _insert(self, job_id: str, *, updated_at: float, state: str = "running",
                started: float | None = None) -> None:
        self.store._conn.execute(
            "INSERT INTO jobs (id, modality, model_id, provider_id, state, params, created_at,"
            " updated_at, upstream_job_id, dispatch_started_at) VALUES (?, 'image', 'm', ?, ?, '{}',"
            " ?, ?, 'batch-9', ?)",
            (job_id, PROVIDER, state, started or (T0 - 9000), updated_at,
             started or (T0 - 9000)),
        )
        self.store._conn.commit()

    def _row(self, job_id: str = None) -> dict:
        from gateway.store import JobStore  # noqa: F401  (the store's own row shape)
        return dict(self.store._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id or self.job_id,)).fetchone())

    def test_a_row_nothing_polls_past_the_ceiling_ends_with_a_sentence(self):
        with patch.object(worker, "_hub_batch_answer", return_value={"known": True, "state": "running"}):
            out = worker.reconcile_wedged_runs(self.store, set(), now=T0)
        self.assertEqual(out["ended"], 1)
        row = self._row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error_code"], worker.UPSTREAM_UNPOLLED)
        self.assertEqual(row["retryable"], 1, "the sentence offers a retry, so the flag agrees")
        self.assertIn("No poll was recorded for this job for 150 min, longer than the 122 min it is held to", row["error_detail"])
        self.assertIn("still reports batch batch-9 as running", row["error_detail"])
        self.assertIn("running the job again after that is safe", row["error_detail"])
        self.assertEqual(row["upstream_job_id"], "batch-9", "the evidence is kept")
        recorded = json.loads(row["superseded_batches"] or "[]")
        self.assertEqual([item["batch_id"] for item in recorded], ["batch-9"],
                         "the cleanup is told which batch to stop first")

    def test_a_row_a_poller_owns_is_left_alone(self):
        with patch.object(worker, "_hub_batch_answer", return_value={"known": True, "state": "running"}):
            out = worker.reconcile_wedged_runs(self.store, {self.job_id}, now=T0)
        self.assertEqual(out["ended"], 0)
        self.assertEqual(self._row()["state"], "running")

    def test_a_batch_the_hub_never_heard_of_is_retryable(self):
        with patch.object(worker, "_hub_batch_answer", return_value={"known": False}):
            worker.reconcile_wedged_runs(self.store, set(), now=T0)
        row = self._row()
        self.assertEqual(row["error_code"], worker.UPSTREAM_UNPOLLED)
        self.assertEqual(row["retryable"], 1, "nothing upstream: a retry duplicates nothing")
        self.assertIn("has no record of that batch at all", row["error_detail"])
        self.assertIn("held to", row["error_detail"])

    def test_a_batch_that_finished_is_collected_not_failed(self):
        with patch.object(worker, "_hub_batch_answer", return_value={"known": True, "state": "done"}), \
                patch.object(worker, "_adopt_hub_batch", return_value=(True, True, None)):
            out = worker.reconcile_wedged_runs(self.store, set(), now=T0)
        self.assertEqual(out["adopted"], 1)
        self.assertEqual(out["ended"], 0)
        self.assertEqual(self._row()["state"], "running", "the adoption path owns the row now")

    def test_a_hub_that_cannot_be_asked_is_not_read_as_finished(self):
        with patch.object(worker, "_hub_batch_answer", return_value=None):
            worker.reconcile_wedged_runs(self.store, set(), now=T0)
        row = self._row()
        self.assertEqual(row["retryable"], 0, "could not ask is not it is finished")
        self.assertIn("could not be asked", row["error_detail"])
        self.assertIn("A retry is not offered yet", row["error_detail"])
        self.assertNotIn("running the job again after that is safe", row["error_detail"])

    def test_the_printed_figure_is_the_figure_enforced(self):
        with patch.object(worker.fleet, "measured_duration_bound",
                          return_value=(7200.0, "the fleet's usual 2400 s for this model")):
            inside = "inside-bound"
            self.store._conn.execute("DELETE FROM jobs")
            self._insert(inside, updated_at=T0 - 7000, started=T0 - 7000)   # 116 min: inside 120
            self.store._conn.commit()
            with patch.object(worker, "_hub_batch_answer",
                              return_value={"known": True, "state": "running"}):
                self.assertEqual(worker.reconcile_wedged_runs(self.store, set(), now=T0)["ended"], 0)
                self.assertEqual(self._row(inside)["state"], "running")
                self.store._conn.execute("DELETE FROM jobs")
                self._insert("over-bound", updated_at=T0 - 7300, started=T0 - 7300)  # 121 min: outside 120
                self.store._conn.commit()
                self.assertEqual(worker.reconcile_wedged_runs(self.store, set(), now=T0)["ended"], 1)
        row = self._row("over-bound")
        self.assertIn("the 120 min it is held to", row["error_detail"])
        self.assertIn("the fleet's usual 2400 s for this model", row["error_detail"])

    def test_a_row_inside_the_ceiling_is_not_looked_at(self):
        self.store._conn.execute("DELETE FROM jobs")
        self._insert("fresh", updated_at=T0 - 60)
        self.store._conn.commit()
        with patch.object(worker, "_hub_batch_answer", return_value={"known": True, "state": "running"}):
            out = worker.reconcile_wedged_runs(self.store, set(), now=T0)
        self.assertEqual(out["checked"], 0)
        self.assertEqual(self._row("fresh")["state"], "running")


if __name__ == "__main__":
    unittest.main()
