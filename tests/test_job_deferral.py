"""A busy fleet is not a broken job.

Measured 2026-09-25: ninety-three of a hundred and thirty-five chat jobs failed
inside half an hour, most of them `MACHINE_BUSY` against the single location that
had the model. The gateway had nowhere else to route and settled each one as a
failure — which is a lie about the work rather than a report about the fleet. The
old GenStudio held work in its own queue; this had to be built.

The rule these tests hold, and the reason each condition exists:

  * **defer** a transient site condition — "not now" is not "not ever";
  * **never** defer an unknown outcome, because a request that may have reached a
    worker is a recovery hold and an automatic retry could duplicate it;
  * **never** defer forever — a job that is never reported is worse than one
    reported honestly;
  * a deferred job is **not terminal**, and it comes back on its own.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway.adapters import BackendError  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import (  # noqa: E402
    _MAX_DEFERRALS,
    _TRANSIENT_SITE_CODES,
    _defer_delay,
    _defer_if_transient,
)


class _Refusal(BackendError):
    """A BackendError the test can spell out."""


def _error(code: str, *, accepted: bool | None) -> BackendError:
    return BackendError(f"{code} happened", code=code, retryable=True,
                        upstream_accepted=accepted)


class TheQueueHoldsItTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.job, _ = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="k",
            provider_id="studio-hub-local",
        )

    def _current(self, attempts: int = 1, **extra) -> dict:
        return {"attempt_count": attempts, **extra}

    def test_a_transient_refusal_is_waited_out_rather_than_failed(self) -> None:
        deferred = _defer_if_transient(
            self.store, self.job["id"], _error("LOCATION_NOT_READY", accepted=False),
            self._current(1), unknown=False,
        )
        self.assertTrue(deferred)
        row = self.store.get(self.job["id"])
        self.assertEqual(row["state"], "accepted", "it must go back in the queue")
        self.assertNotIn(row["state"], {"failed", "succeeded", "cancelled"})
        self.assertGreater(row["next_action_at"], time.time(), "it must not spin")
        self.assertIn("LOCATION_NOT_READY", row["defer_reason"])

    def test_an_unreachable_site_is_the_same_kind_of_fact(self) -> None:
        self.assertTrue(_defer_if_transient(
            self.store, self.job["id"], _error("LOCATION_UNREACHABLE", accepted=False),
            self._current(1), unknown=False,
        ))

    def test_an_unknown_outcome_is_never_deferred(self) -> None:
        # The rule that matters most. A request that may have reached a worker is a
        # durable recovery hold; retrying it automatically could generate twice.
        for code in sorted(_TRANSIENT_SITE_CODES):
            for accepted, unknown in ((None, True), (True, False), (False, True)):
                self.assertFalse(
                    _defer_if_transient(
                        self.store, self.job["id"], _error(code, accepted=accepted),
                        self._current(1), unknown=unknown,
                    ),
                    f"{code} accepted={accepted} unknown={unknown} was deferred",
                )
        self.assertEqual(self.store.get(self.job["id"])["state"], "accepted",
                         "and nothing was written")

    def test_a_plain_failure_is_not_deferred(self) -> None:
        # A 400 from a provider is settled work, not a busy machine.
        self.assertFalse(_defer_if_transient(
            self.store, self.job["id"], _error("PROVIDER_FAILED", accepted=False),
            self._current(1), unknown=False,
        ))

    def test_it_gives_up_after_the_bound_and_says_so(self) -> None:
        # A job that is never reported is worse than one reported honestly.
        self.assertFalse(_defer_if_transient(
            self.store, self.job["id"], _error("LOCATION_NOT_READY", accepted=False),
            self._current(_MAX_DEFERRALS + 1), unknown=False,
        ))
        self.assertTrue(_defer_if_transient(
            self.store, self.job["id"], _error("LOCATION_NOT_READY", accepted=False),
            self._current(_MAX_DEFERRALS), unknown=False,
        ), "the last permitted wait must still happen")

    def test_the_wait_grows_and_then_stops_growing(self) -> None:
        """The cap is now a setting, and it is **five minutes, not one**.

        The literal `60.0` here was the shipped `DEFAULT_DEFER_CAP_S`, and the shipped defaults
        were also the effective ceiling — the live settings file did not override them — so a job
        waiting on a busy site was **failed after about four minutes** (audit M4). The owner's
        decision C replaces that with *"hold, don't fail, for a temporarily absent cluster"*, and
        two-hour video jobs are the reason: 240 deferrals capped at five minutes is about twenty
        hours, while the retry policy (A7) is what declares a job failed when real machines have
        tried it. The assertion is therefore read from the setting rather than restated, because a
        second copy of this number is exactly what went stale.
        """
        from gateway import concurrency

        cap = float(concurrency.queue_wait_budget()["defer_cap_seconds"])
        self.assertGreaterEqual(cap, 300.0, "a long render must survive its own queue")
        delays = [_defer_delay(n) for n in range(1, _MAX_DEFERRALS + 3)]
        self.assertEqual(delays, sorted(delays), "the backoff must not go backwards")
        self.assertEqual(delays[0], 2.0)
        self.assertLessEqual(max(delays), cap, "a busy fleet must not be hammered")
        self.assertEqual(delays[-1], delays[-2], "and it must flatten, not explode")
        # An explicit, long render: the cap must still hold it.
        self.assertLessEqual(
            max(_defer_delay(n) for n in range(1, 40)), cap,
            "a two-hour job waits, and does not spin")


class TheDeferredJobComesBackTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def test_it_is_due_again_once_the_delay_passes(self) -> None:
        job, _ = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="k",
            provider_id="studio-hub-local",
        )
        self.store.defer(job["id"], delay_s=30.0, reason="busy")
        self.assertEqual([j["id"] for j in self.store.due()], [],
                         "a deferred job must not be due while it waits")
        self.assertEqual([j["id"] for j in self.store.due(time.time() + 31)], [job["id"]],
                         "and it must come back on its own, with no retry loop to run")

    def test_dispatch_clears_the_waiting_reason(self) -> None:
        # `accepted` plus a reason means waiting; a running job is not waiting, and
        # a stale sentence beside a running job is how a page starts lying.
        job, _ = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="k",
            provider_id="studio-hub-local",
        )
        self.store.defer(job["id"], delay_s=0.0, reason="busy")
        self.assertTrue(self.store.mark_running(job["id"]))
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["defer_reason"])

    def test_a_terminal_job_is_not_reopened(self) -> None:
        job, _ = self.store.submit(
            modality="chat", model_id="m", params={}, idempotency_key="k",
            provider_id="studio-hub-local",
        )
        self.store.fail(job["id"], code="PROVIDER_FAILED", detail="no")
        self.assertFalse(self.store.defer(job["id"], delay_s=1.0, reason="busy"),
                         "a settled job must never be put back in the queue")


if __name__ == "__main__":
    unittest.main(verbosity=2)
