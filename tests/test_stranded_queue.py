#!/usr/bin/env python3
"""Free work never waits at a group whose every Mac able to run it is paused (0.2.117).

Measured 2026-10-04: group 1500 had all five Macs paused one by one at its controller; the Gateway still
sent it 21 free TTS jobs, the Hub queued them for ~7,000 s, and 8 GB Macs elsewhere idled. Three fixes:
such a group is not a routing candidate; the Hub's `NO_AVAILABLE_MACHINE` (503 on create, or a handed-back
item) moves the job like any hand-back; and a job queued 10 minutes where the fleet read shows no unpaused
Mac able to run it is withdrawn and moved the same way. A pinned job fails at once instead.

Mocked Hub transport and patched fleet reads only: no live Hub, no generation, no installed app.
"""
from __future__ import annotations

import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from test_free_location_reroute import A, B, BASE, _Case, _Fleet  # sets the sandbox env

from gateway import concurrency, fleet  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendRequest  # noqa: E402
from gateway.providers import registry  # noqa: E402

MODEL = "model/test"
REFUSAL = {"detail": {"code": "NO_AVAILABLE_MACHINE",
                      "detail": "every Mac here that can run this model is paused"}}


def _group(location_id: str, *, local_drained: bool, node_paused: bool, node_fits: bool = True,
           models: list[str] | None = None) -> dict:
    """A controller (its own Mac `local`) and one joined node, both serving MODEL."""
    served = [MODEL] if models is None else models
    return {
        "id": location_id, "label": location_id, "reachable": True, "capacity": {}, "model_supply": {},
        "machines": [], "machine_summary": {"reachable": 2}, "running_jobs": [],
        "nodes": [{"id": "node-1", "paused": node_paused}],
        "workers": [{"machine": "local", "drained": local_drained, "models": served},
                    {"machine": "node-1", "drained": node_paused, "models": served}],
        "local_models": [{"model": MODEL, "machine": "node-1", "memory_eligible": node_fits}],
    }


def _candidates(rows: list[dict]) -> list[str]:
    definitions = [SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{row['id']}") for row in rows]
    with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": rows}), \
            patch.object(registry.models, "enabled_models",
                         lambda _m: [(d, {"model_id": MODEL}) for d in definitions]):
        return [c["location_id"] for c in fleet.routing_candidates(MODEL, "tts")]


class DontRouteThereTests(unittest.TestCase):
    def test_a_group_with_every_able_mac_paused_is_not_a_candidate(self) -> None:
        rows = [_group("g1500", local_drained=True, node_paused=True),
                _group("g1100", local_drained=False, node_paused=False)]
        self.assertEqual(_candidates(rows), ["g1100"])

    def test_one_unpaused_mac_that_can_run_it_keeps_the_group(self) -> None:
        self.assertEqual(_candidates([_group("g", local_drained=True, node_paused=False)]), ["g"])

    def test_an_unpaused_mac_too_small_for_the_model_does_not_count(self) -> None:
        self.assertEqual(_candidates([_group("g", local_drained=True, node_paused=False, node_fits=False)]), [])

    def test_a_hub_that_names_no_macs_for_the_model_is_unknown_not_paused(self) -> None:
        self.assertEqual(_candidates([_group("g", local_drained=True, node_paused=True, models=[])]), ["g"])


class _HubFleet(_Fleet):
    """A refuses at the door (`refuse`), or queues the item (`queued`, `queued_code`, `handed_back`)."""

    def __init__(self, mode: str, times: int = 10**6) -> None:
        super().__init__(a_dies=False)
        self.mode, self.times = mode, times

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "hub-a" in request.url.host and self.times > 0:
            method, path = request.method, request.url.path
            if self.mode == "refuse" and method == "POST" and path == "/api/hub/jobs":
                self.times -= 1
                self.calls.append(("a", method, path))
                return httpx.Response(503, json=REFUSAL)
            if method == "GET" and path == "/api/hub/jobs/batch-a" and self.mode != "refuse":
                self.calls.append(("a", method, path))
                item = {"index": 0, "state": "queued", "error": "waiting for a free Mac"}
                if self.mode == "queued_code":
                    item["error_code"] = "NO_AVAILABLE_MACHINE"
                if self.mode == "handed_back":
                    item.update(state="error", error_code="NO_AVAILABLE_MACHINE")
                return httpx.Response(200, json={"items": [item]})
            if method == "DELETE":
                self.times -= 1
                self.calls.append(("a", method, path))
                return httpx.Response(200, json={"ok": True, "queued_cancelled": 1,
                                                 "running_signalled": 0, "running_pending": 0})
        return super().handler(request)


def _generate(fake: _HubFleet, *, stranded: bool | None = False) -> BackendError:
    """Run one generate. `stranded=None` leaves the real `fleet.location_has_no_free_mac` in place."""
    real = httpx.Client

    def factory(*a, **k):
        k.pop("transport", None)
        return real(transport=httpx.MockTransport(fake.handler), **k)

    request = BackendRequest(provider_id=A, model_id=MODEL, modality="image",
                             params={"prompt": "x", "_job_id": "job-1"}, values={"hub_token": "t"},
                             base_url=BASE[A])
    patches = [patch.object(hub_module.httpx, "Client", side_effect=factory),
               patch.object(hub_module, "POLL_INTERVAL_S", 0),
               patch.object(hub_module, "STRANDED_QUEUE_S", 0)]
    if stranded is not None:
        patches.append(patch.object(fleet, "location_has_no_free_mac", lambda _p, _m: stranded))
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        try:
            hub_module.StudioHubBackend().generate(request)
        except BackendError as exc:
            return exc
    raise AssertionError("expected a BackendError")


class HubSaysNoMachineTests(unittest.TestCase):
    def assert_hand_back(self, error: BackendError) -> None:
        self.assertEqual(error.code, "NO_AVAILABLE_MACHINE")
        self.assertIs(error.upstream_accepted, False, "nothing ran: free to send elsewhere")
        self.assertTrue(error.retryable)

    def test_a_503_on_create_is_a_hand_back(self) -> None:
        error = _generate(_HubFleet("refuse"))
        self.assert_hand_back(error)
        self.assertEqual(error.status, 503)

    def test_a_queued_item_carrying_the_code_is_withdrawn_and_handed_back(self) -> None:
        fake = _HubFleet("queued_code")
        self.assert_hand_back(_generate(fake))
        self.assertEqual(fake.saw("a", "DELETE"), ["/api/hub/jobs/batch-a"])

    def test_an_item_the_hub_ended_with_the_code_is_handed_back(self) -> None:
        error = _generate(_HubFleet("handed_back"))
        self.assert_hand_back(error)
        self.assertTrue(error.upstream_settled)


class SafetyNetTests(unittest.TestCase):
    def test_queued_where_no_unpaused_mac_can_run_it_is_withdrawn(self) -> None:
        fake = _HubFleet("queued")
        error = _generate(fake, stranded=True)
        self.assertEqual(error.code, "NO_AVAILABLE_MACHINE")
        self.assertTrue(error.upstream_settled)
        self.assertEqual(fake.saw("a", "DELETE"), ["/api/hub/jobs/batch-a"])

    def test_queued_behind_a_busy_but_unpaused_mac_keeps_waiting(self) -> None:
        fake = _Fleet(a_dies=False, a_states=["queued", "queued", "done"])
        real = httpx.Client

        def factory(*a, **k):
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fake.handler), **k)

        request = BackendRequest(provider_id=A, model_id=MODEL, modality="image",
                                 params={"prompt": "x", "_job_id": "job-2"}, values={"hub_token": "t"},
                                 base_url=BASE[A])
        with patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module, "STRANDED_QUEUE_S", 0), \
                patch.object(fleet, "location_has_no_free_mac", lambda _p, _m: False):
            hub_module.StudioHubBackend().generate(request)
        self.assertEqual(fake.saw("a", "DELETE"), [], "never withdrawn while a Mac there can run it")

    def test_the_fleet_read_names_the_location_by_provider_id(self) -> None:
        rows = [_group("a", local_drained=True, node_paused=True), _group("b", local_drained=False, node_paused=False)]
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": rows}):
            self.assertTrue(fleet.location_has_no_free_mac(A, MODEL))
            self.assertFalse(fleet.location_has_no_free_mac(B, MODEL))
            self.assertFalse(fleet.location_has_no_free_mac("studio-hub-unknown", MODEL))


class MacsGoneOfflineTests(unittest.TestCase):
    """A controller that still answers while every Mac it has has dropped (site 1400, 2026-10-31).

    Its studios stopped at 21:35 with the location itself still read as reachable and eleven of its
    machines as offline; seven free jobs accepted there waited 55 minutes for a restart by hand.
    The stranded-queue door already moved a job whose Macs were all *paused* — this pins the same
    door for the state the outage actually left behind, and the states it must not misread.
    """

    @staticmethod
    def _site(*, online: list[bool] | None) -> dict:
        row = _group("a", local_drained=False, node_paused=False)
        if online is not None:
            for worker, up in zip(row["workers"], online):
                worker["online"] = up
        return row

    @staticmethod
    def _read(rows: list[dict]) -> bool:
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": rows}):
            return fleet.location_has_no_free_mac(A, MODEL)

    def test_every_able_mac_offline_reads_as_no_free_mac(self) -> None:
        self.assertTrue(self._read([self._site(online=[False, False])]))

    def test_one_mac_still_online_keeps_the_site_alive(self) -> None:
        self.assertFalse(self._read([self._site(online=[True, False])]),
                         "a site with a working Mac must not read as dead")

    def test_a_worker_that_states_no_online_is_unknown_not_offline(self) -> None:
        self.assertFalse(self._read([self._site(online=None)]),
                         "'not read' is never 'dead' (an older Hub)")

    def test_a_site_the_hub_names_no_macs_for_is_unknown_not_offline(self) -> None:
        rows = [_group("a", local_drained=False, node_paused=False, models=[])]
        self.assertFalse(self._read(rows))

    def test_offline_is_not_how_paused_reads_and_routing_skips_the_site(self) -> None:
        row = self._site(online=[False, False])
        self.assertFalse(fleet.no_free_mac(row, MODEL),
                         "an offline Mac is not a paused one; catalog's 'you paused' must not claim it")
        # 0.2.257: an all-offline site is no longer offered as a routing candidate — it was
        # the 1204 seven-days-gone case that produced a four-hour pending job. The stranded
        # door below still withdraws anything already bound to it.
        self.assertEqual(_candidates([row, _group("b", local_drained=False, node_paused=False)]),
                         ["b"], "an offline site is not a candidate; the door still withdraws")

    def test_a_queued_item_there_is_withdrawn_at_once_not_left_to_the_queue_ceiling(self) -> None:
        # The ceiling is the bound the 2026-10-31 jobs actually waited on (55 minutes of a 3-hour
        # one). Shortened only so this test ends either way; without the fix the item is withdrawn
        # by that ceiling instead of by the door, and the code says which one happened.
        fake = _HubFleet("queued")
        with patch.object(fleet, "snapshot",
                          lambda allow_stale=False: {"locations": [self._site(online=[False, False])]}), \
                patch.object(hub_module, "queue_wait_ceiling_s", lambda: 0.2):
            error = _generate(fake, stranded=None)
        self.assertEqual(error.code, "NO_AVAILABLE_MACHINE")
        self.assertTrue(error.upstream_settled, "withdrawn before anything ran")
        self.assertEqual(fake.saw("a", "DELETE"), ["/api/hub/jobs/batch-a"])


class MoveItTests(_Case):
    def test_a_refusal_moves_the_job_at_once_without_spending_the_retry_ceiling(self) -> None:
        one = dict(concurrency.retry_policy(), max_attempts=1)
        with patch.object(worker_module.concurrency, "retry_policy", lambda: one):
            job = self.run_job(_HubFleet("refuse"), self.job("nam-1"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertFalse(job["recovery_required"])
        self.assertEqual(self.routed, [{"a"}], "the paused group is excluded")
        self.assertEqual([a["code"] for a in self.store.attempts_of(job["id"])], ["NO_AVAILABLE_MACHINE"])

    def test_a_job_stranded_on_an_old_hub_is_withdrawn_and_moved(self) -> None:
        fake = _HubFleet("queued")
        with patch.object(hub_module, "STRANDED_QUEUE_S", 0), \
                patch.object(fleet, "location_has_no_free_mac", lambda p, _m: p == A):
            job = self.run_job(fake, self.job("nam-2"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertEqual(fake.saw("a", "DELETE"), ["/api/hub/jobs/batch-a"])
        self.assertFalse(job["recovery_required"])

    def test_with_nowhere_else_it_waits_and_returns_on_a_fresh_request_id(self) -> None:
        self.next_site = []
        fake = _HubFleet("refuse", times=1)
        job = self.run_job(fake, self.job("nam-3"))
        self.assertEqual(job["state"], "accepted", "waiting for a free Mac, not failed")
        self.assertIn("waiting for a free Mac", job["defer_reason"])
        job = self.run_job(fake, self.store.get(job["id"]))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A), job.get("error_detail"))
        self.assertEqual([k for k in fake.batches if k.startswith("a:")], [f"a:{job['id']}.1"])

    def test_a_pinned_job_fails_at_once_with_the_plain_sentence(self) -> None:
        self.modality = "tts"
        row = self.job("nam-pinned", {"text": "x", "voice_id": "studio-hub-a::clone"})
        with patch.object(worker_module.fleet, "pinned_location", lambda _p: A):
            job = self.run_job(_HubFleet("refuse"), row)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "NO_AVAILABLE_MACHINE"))
        self.assertIn("Nothing was charged; run it again", job["error_detail"])
        self.assertFalse(job["recovery_required"])
        self.assertEqual(self.routed, [])
        self.assertIsNone(job["defer_reason"])


if __name__ == "__main__":
    unittest.main()
