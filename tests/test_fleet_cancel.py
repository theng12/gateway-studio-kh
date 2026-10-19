"""The Fleet cancel route: a request aimed at the right controller, or a refusal.

Cancellation is the one Fleet action that reaches upstream, so what it is aimed at
is the whole risk: the wrong controller, the wrong token, or the local Hub by
default. These drive `fleet_cancel_job` with a recording adapter, so the assertion
is about **what the adapter was handed**, not about a helper's return value.
"""
from pathlib import Path
import json
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

# **Before the first `gateway` import.** Importing the package runs `gateway/runtime.py`, which
# opens the job store at `GATEWAY_DB`; with nothing set that is the owner's live database, and
# `JobStore.__init__` used to hold every job his serving gateway had in flight (measured
# 2026-10-19, from this file among others). The store now refuses to sweep while another process
# owns the queue, but a test must not open his database at all.
_SANDBOX = sandbox_env("gateway-fleet-cancel-")

from gateway import admin, fleet  # noqa: E402


class RecordingAdapter:
    def __init__(self):
        self.requests = []

    def cancel(self, request):
        self.requests.append(request)
        return {"ok": True, "queued_cancelled": 2, "running_signalled": 1,
                "running_pending": 0}


class NoCancelAdapter:
    pass


def _location():
    return {
        "id": "ctrl-1", "label": "Blue Room", "provider_id": "studio-hub-ctrl-1",
        "url": "http://10.9.9.9:47873", "local": False,
    }


class FleetCancel(unittest.TestCase):
    def _call(self, adapter, *, controller="ctrl-1", batch="batch-9",
              modality="image", locations=None):
        definition = types.SimpleNamespace(id="studio-hub-ctrl-1", display_name="Blue Room")
        snapshot = {"locations": locations if locations is not None else [_location()]}
        with patch.object(fleet, "snapshot", return_value=snapshot), \
             patch.object(admin.registry, "definition", return_value=definition), \
             patch.object(admin.registry.credentials, "resolved",
                          return_value={"hub_token": "fleet-tok"}), \
             patch.object(admin, "adapter_for", return_value=adapter):
            return admin.fleet_cancel_job(controller, batch, {"modality": modality})

    def test_the_cancel_names_the_controller_its_token_and_the_batch(self):
        adapter = RecordingAdapter()
        response = self._call(adapter)
        self.assertEqual(len(adapter.requests), 1)
        request = adapter.requests[0]
        self.assertEqual(request.upstream_job_id, "batch-9")
        self.assertEqual(request.modality, "image")
        self.assertEqual(request.provider_id, "studio-hub-ctrl-1")
        # The address the page carried, never this Mac's Hub by default.
        self.assertEqual(request.base_url, "http://10.9.9.9:47873")
        # The stored credential, not an empty one: `_headers` reads it.
        self.assertEqual(request.values.get("hub_token"), "fleet-tok")

    def test_the_hubs_own_answer_is_passed_through_unflattened(self):
        # The Hub reports a request, not a result: some items were queued, one
        # worker was signalled, none is still pending. A boolean would lose which.
        response = self._call(RecordingAdapter())
        self.assertEqual(response["result"]["queued_cancelled"], 2)
        self.assertEqual(response["result"]["running_signalled"], 1)
        self.assertEqual(response["result"]["running_pending"], 0)
        self.assertEqual(response["batch_id"], "batch-9")
        self.assertEqual(response["location"], "Blue Room")

    def test_a_modality_no_lane_serves_is_refused_before_any_request(self):
        adapter = RecordingAdapter()
        response = self._call(adapter, modality="telepathy")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(adapter.requests, [], "a refused cancel must reach nothing")

    def test_an_unknown_location_is_a_404(self):
        response = self._call(RecordingAdapter(), controller="ctrl-missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("No such location", json.loads(bytes(response.body))["detail"])

    def test_a_lane_that_cannot_cancel_says_so_instead_of_pretending(self):
        response = self._call(NoCancelAdapter())
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot cancel", json.loads(bytes(response.body))["detail"])

    def test_the_transcription_route_is_kept_separate(self):
        # Transcription batches are invisible to `/api/hub/jobs`; the modality is
        # what picks the Hub route, so it has to survive the call unchanged.
        adapter = RecordingAdapter()
        self._call(adapter, modality="transcription")
        self.assertEqual(adapter.requests[0].modality, "transcription")


class RecordingRetryAdapter:
    def __init__(self):
        self.requests = []

    def retry(self, request):
        self.requests.append(request)
        return {"ok": True, "status": 200, "answer": {"ok": True, "batch_id": "batch-9",
                                                      "item_index": request.upstream_item_index,
                                                      "state": "queued"}}


class NoRetryAdapter:
    pass


class FleetRetry(unittest.TestCase):
    @staticmethod
    def _request(adapter, *, controller="ctrl-1", batch="batch-9", index=2, modality="image"):
        definition = types.SimpleNamespace(id="studio-hub-ctrl-1", display_name="Blue Room")
        snapshot = {"locations": [_location()]}
        with patch.object(fleet, "snapshot", return_value=snapshot), \
             patch.object(admin.registry, "definition", return_value=definition), \
             patch.object(admin.registry.credentials, "resolved",
                          return_value={"hub_token": "fleet-tok"}), \
             patch.object(admin, "adapter_for", return_value=adapter):
            return admin.fleet_retry_job_item(controller, batch, index, {"modality": modality})

    def test_the_retry_names_the_batch_and_the_failed_item(self):
        adapter = RecordingRetryAdapter()
        response = self._request(adapter)
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0].upstream_job_id, "batch-9")
        # The item index is the whole point: a batch can hold several failures, and
        # the Hub refuses a retry that names the wrong one.
        self.assertEqual(adapter.requests[0].upstream_item_index, 2)
        self.assertEqual(adapter.requests[0].base_url, "http://10.9.9.9:47873")
        self.assertEqual(response["item_index"], 2)
        self.assertEqual(response["result"]["answer"]["state"], "queued")

    def test_a_modality_no_lane_serves_is_refused_before_any_request(self):
        adapter = RecordingRetryAdapter()
        response = self._request(adapter, modality="telepathy")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(adapter.requests, [], "a refused retry must reach nothing")

    def test_a_lane_that_cannot_retry_says_so(self):
        response = self._request(NoRetryAdapter())
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot retry", json.loads(bytes(response.body))["detail"])


if __name__ == "__main__":
    unittest.main()
