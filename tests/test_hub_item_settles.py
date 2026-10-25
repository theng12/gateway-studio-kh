"""What the Hub adapter tells the worker about a batch it has finished polling.

The gateway treats a job that carries an upstream id as *possibly still running* — rightly, because
the id is the only evidence it has. But a Studio Hub **batch is a container the controller creates
before it validates the item**, so a refused or failed item leaves an id behind with no work
running and no artifact. Reading the id alone turned every ordinary item failure into a durable
`unknown_outcome` hold: *"reconcile the provider request before retrying; the gateway will not
resubmit it automatically."*

Measured live on 2026-09-26, twice: a `422 Choose a qualified 1K aspect ratio` (a caller's geometry
mistake) and a `409 local is reserved for other work on this Mac` (a busy machine). Both were work
that never ran, both landed in the operator's recovery queue.

The distinction these tests hold:

  * an item polled to a **terminal bad** state is settled — nothing is pending, so it must not hold;
  * an item the Hub calls **uncertain** is not — the work may have completed, so it must;
  * a batch that never reaches a terminal state is not — the deadline proves nothing about the
    upstream, so it must.

`upstream_settled` is the adapter's statement that it has followed its own identifier to a terminal
answer. It is deliberately separate from `upstream_accepted`, which means "did the remote take the
work at all" and is what the submit-time refusals already use.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters.studio_hub import _poll_batch  # noqa: E402


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _Client:
    """Stands in for httpx.Client: one batch document, however many polls it takes."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, url: str, headers: dict | None = None) -> _Response:
        self.calls += 1
        return _Response(self.payload)


def _batch(item: dict) -> dict:
    return {"state": item.get("state"), "items": [item]}


class TheItemIsSettledTest(unittest.TestCase):
    def test_a_failed_item_is_settled_instead_of_held(self) -> None:
        client = _Client(
            _batch(
                {
                    "state": "failed",
                    "error": {"message": "HTTP 422: Choose a qualified 1K aspect ratio"},
                }
            )
        )
        with self.assertRaises(BackendError) as caught:
            _poll_batch(client, "http://hub", {}, "batch-1", 0, deadline_seconds=5)
        failure = caught.exception
        self.assertTrue(
            failure.upstream_settled,
            "a terminal item failure must tell the worker nothing is pending, or the job becomes "
            "a recovery hold for work that never ran",
        )
        self.assertEqual(failure.code, "PROVIDER_FAILED")
        self.assertFalse(failure.retryable)
        # The Hub's own sentence is still what the operator reads.
        self.assertIn("1K aspect ratio", str(failure))

    def test_an_error_state_with_no_item_is_settled_too(self) -> None:
        client = _Client({"state": "error", "items": [], "error": {"message": "no item was created"}})
        with self.assertRaises(BackendError) as caught:
            _poll_batch(client, "http://hub", {}, "batch-2", 0, deadline_seconds=5)
        self.assertTrue(caught.exception.upstream_settled)
        self.assertEqual(caught.exception.code, "IMAGE_FAILED")

    def test_an_uncertain_item_keeps_its_hold(self) -> None:
        client = _Client(_batch({"state": "uncertain"}))
        with self.assertRaises(BackendError) as caught:
            _poll_batch(client, "http://hub", {}, "batch-3", 0, deadline_seconds=5)
        failure = caught.exception
        self.assertFalse(
            failure.upstream_settled,
            "the Hub settled this item without knowing the worker's outcome - the work may have "
            "completed, so it must stay a recovery hold",
        )
        self.assertEqual(failure.code, "UNKNOWN_OUTCOME")
        self.assertIs(failure.upstream_accepted, True)

    def test_a_batch_that_never_settles_keeps_its_hold(self) -> None:
        client = _Client(_batch({"state": "running"}))
        with self.assertRaises(BackendError) as caught:
            _poll_batch(client, "http://hub", {}, "batch-4", 0, deadline_seconds=0.01)
        failure = caught.exception
        self.assertFalse(
            failure.upstream_settled,
            "a deadline is not knowledge about the upstream: the batch may still be running",
        )
        self.assertEqual(failure.code, "PROVIDER_SUBMISSION_UNCERTAIN")


class TheDroppedPollIsNotAnUnknownOutcomeTest(unittest.TestCase):
    """A blip on one poll must not discard a job the Hub is still running.

    Measured on the owner's fleet 2026-09-29: batch `0dcdc6331e` was reported
    failed at 23:10, and the Hub's own record shows that item finished `done`
    **with an artifact** 4m45s later. One `httpx.HTTPError` out of as many as 900
    polls (1 s apart against a 900 s deadline) was enough to throw the render away.
    """

    def _flaky(self, failures: int, payload: dict):
        import httpx

        class _Flaky:
            def __init__(self) -> None:
                self.left = failures
                self.calls = 0

            def get(self, url: str, headers: dict | None = None):
                self.calls += 1
                if self.left > 0:
                    self.left -= 1
                    raise httpx.ConnectError("Server disconnected without sending a response")
                return _Response(payload)

        return _Flaky()

    def test_a_few_dropped_polls_still_return_the_finished_item(self) -> None:
        from unittest.mock import patch

        from gateway.adapters import studio_hub

        client = self._flaky(3, _batch({"state": "done", "artifact_path": "/tmp/a.png"}))
        with patch.object(studio_hub, "POLL_INTERVAL_S", 0.01):
            result = _poll_batch(client, "http://hub", {}, "batch-ok", 0, deadline_seconds=30)
        self.assertEqual("done", result["item"]["state"])
        self.assertEqual(4, client.calls, "it must keep asking after the drops, not give up")

    def test_a_hub_that_stays_gone_still_becomes_a_named_hold(self) -> None:
        from unittest.mock import patch

        from gateway.adapters import studio_hub

        client = self._flaky(10_000, _batch({"state": "done"}))
        with patch.object(studio_hub, "POLL_INTERVAL_S", 0.01), \
                patch.object(studio_hub, "POLL_DROP_GRACE_S", 0.05):
            with self.assertRaises(BackendError) as caught:
                _poll_batch(client, "http://hub", {}, "batch-gone", 0, deadline_seconds=30)
        failure = caught.exception
        self.assertEqual("PROVIDER_SUBMISSION_UNCERTAIN", failure.code)
        self.assertTrue(failure.upstream_accepted)
        # The hold still names the batch, so it can be settled when the Mac answers.
        self.assertEqual("batch-gone", failure.recovery["batch_id"])
        # And it says how long it waited, so "gave up immediately" is distinguishable.
        self.assertIn("for ", str(failure))

    def test_a_successful_poll_clears_the_drop_run(self) -> None:
        # A Hub that drops one request now and another an hour later is not a Hub
        # that has gone away. The run has to reset on every answer.
        from unittest.mock import patch

        from gateway.adapters import studio_hub

        class _Alternating:
            def __init__(self) -> None:
                self.n = 0

            def get(self, url: str, headers: dict | None = None):
                import httpx

                self.n += 1
                if self.n == 1 or self.n == 3:
                    raise httpx.ConnectError("blip")
                if self.n >= 5:
                    return _Response(_batch({"state": "done"}))
                return _Response(_batch({"state": "running"}))

        with patch.object(studio_hub, "POLL_INTERVAL_S", 0.01):
            result = _poll_batch(_Alternating(), "http://hub", {}, "batch-blips", 0,
                                 deadline_seconds=30)
        self.assertEqual("done", result["item"]["state"])


if __name__ == "__main__":
    unittest.main()
