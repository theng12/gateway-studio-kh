#!/usr/bin/env python3
"""Workstation mode: a shop's own Mac lends its idle time to the fleet (owner request 2026-10-03).

The Hub reports each Mac's `workstation` block on its machine/node rows and takes setting changes through
its controller. When such a Mac stops a job (macOS critical memory pressure for 20 s, rare - the only thing
that interrupts a job there) and its controller has no other free Mac, the item comes back `WORKSTATION_YIELDED`: nothing ran to
completion, nothing is pending, and re-running is free. So the Gateway moves the job at once to the next
location offering the model, without spending the job's retry ceiling; a pinned voice fails at once with a plain
sentence; paid lanes are untouched. A Mac standing by, stepped aside or in quiet hours is not offline, not
"needs attention" and not a Telegram alarm.

Mocked Hub transport only: no live Hub, no generation, no installed app.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from test_free_location_reroute import A, B, BASE, _Case, _Fleet, _Registry  # sets the sandbox env

from gateway import admin, concurrency, fleet, notifier, startup_control  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendRequest  # noqa: E402

YIELDED = "WORKSTATION_YIELDED: Shop 2 stepped aside (memory pressure); nothing was kept"
WORKSTATION = {
    "enabled": True, "idle_only": False, "idle_minutes": 3, "step_aside": True, "resume_after_minutes": 2,
    "start_free_gb": None, "quiet_hours": ["22:00-07:00"], "hide_menu_bar_icon": False,
    "state": "standing_by", "reason": "someone is using this Mac", "yields_today": 1, "jobs_today": 3,
}


class _YieldingFleet(_Fleet):
    """A accepts the batch, then its workstation Mac steps aside and the controller hands the item back."""

    def __init__(self, yields: int = 10**6, **kwargs) -> None:
        super().__init__(a_dies=False, **kwargs)
        self.yields_left = yields

    def handler(self, request: httpx.Request) -> httpx.Response:
        if ("hub-a" in request.url.host and request.method == "GET" and request.url.path == "/api/hub/jobs/batch-a"
                and self.yields_left > 0):
            self.yields_left -= 1
            self.calls.append(("a", "GET", request.url.path))
            return httpx.Response(200, json={"items": [{"index": 0, "state": "error",
                                                        "error_code": "WORKSTATION_YIELDED", "error": YIELDED}]})
        return super().handler(request)


class AdapterTests(unittest.TestCase):
    def test_a_yield_is_its_own_known_free_failure_not_a_wait_or_a_hold(self) -> None:
        fake = _YieldingFleet()
        real = httpx.Client

        def factory(*a, **k):
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fake.handler), **k)

        request = BackendRequest(provider_id=A, model_id="model/test", modality="image",
                                 params={"prompt": "x", "_job_id": "job-1"}, values={"hub_token": "t"},
                                 base_url=BASE[A])
        with patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                self.assertRaises(BackendError) as caught:
            hub_module.StudioHubBackend().generate(request)
        error = caught.exception
        self.assertEqual(error.code, "WORKSTATION_YIELDED")
        self.assertEqual(str(error), YIELDED, "the Hub's sentence, not prefixed twice")
        self.assertTrue(error.retryable)
        self.assertIs(error.upstream_accepted, False)
        self.assertTrue(error.upstream_settled, "the Hub finished with it: nothing is pending")


class RerouteTests(_Case):
    def test_a_yield_moves_the_job_at_once_to_the_next_location(self) -> None:
        fake = _YieldingFleet()
        job = self.run_job(fake, self.job("yield-1"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertFalse(job["recovery_required"], "a yield is never an unknown-outcome hold")
        self.assertEqual(self.routed, [{"a"}], "the Mac that stepped aside is excluded")
        self.assertEqual(fake.submits("b"), ["/api/hub/jobs"])
        self.assertIsNone(job.get("defer_reason"), "moved immediately, not deferred")
        self.assertEqual([a["code"] for a in self.store.attempts_of(job["id"])], ["WORKSTATION_YIELDED"])

    def test_a_yield_does_not_spend_the_retry_ceiling(self) -> None:
        one = dict(concurrency.retry_policy(), max_attempts=1)
        with patch.object(worker_module.concurrency, "retry_policy", lambda: one):
            job = self.run_job(_YieldingFleet(), self.job("yield-2"))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))

    def test_with_nowhere_else_it_waits_for_a_free_mac_and_is_never_failed_or_held(self) -> None:
        self.next_site = []
        job = self.run_job(_YieldingFleet(), self.job("yield-3"))
        self.assertEqual(job["state"], "accepted", "queued, not failed with 'run it again'")
        self.assertIsNone(job["error_code"])
        self.assertFalse(job["recovery_required"])
        self.assertIn("waiting for a free Mac", job["defer_reason"])
        self.assertIn("stepped aside", job["defer_reason"])
        self.assertGreater(job["next_action_at"], time.time(), "on the queue-wait backoff ladder")
        # What lets the next pass send it anywhere: no dead batch, no stale dispatch stamp, no tried list.
        self.assertIsNone(job["upstream_job_id"])
        self.assertIsNone(job["dispatch_started_at"])
        self.assertEqual(self.store.attempted_locations(job["id"]), [])
        self.assertEqual([a["code"] for a in self.store.attempts_of(job["id"])], ["WORKSTATION_YIELDED"])
        self.assertTrue(self.store.rebind_unsent(job["id"], from_provider=A, to_provider=B),
                        "another group with room can still take it")

    def test_the_waiting_job_runs_at_the_same_mac_once_it_stops_standing_by_on_a_fresh_request_id(self) -> None:
        self.next_site = []
        fleet = _YieldingFleet(yields=1)
        job = self.run_job(fleet, self.job("yield-4"))
        self.assertEqual(job["state"], "accepted")
        job = self.run_job(fleet, self.store.get(job["id"]))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A), job.get("error_detail"))
        self.assertEqual(fleet.created["a"], 2, "the Hub keeps a request id for good: a repeat would replay the dead batch")
        self.assertEqual(sorted(k.split(":", 1)[1] for k in fleet.batches if k.startswith("a:")),
                         [job["id"], job["id"] + "~1"])
        self.assertIsNone(job["defer_reason"])

    def test_a_job_that_waited_is_not_bound_by_the_locations_it_tried_before(self) -> None:
        self.next_site = []
        fleet = _YieldingFleet(yields=1)
        job = self.run_job(fleet, self.job("yield-5"))
        self.next_site = [B]
        # A yields again on its next turn: B was never tried in the new round, so the job moves at once.
        fleet.yields_left = 1
        job = self.run_job(fleet, self.store.get(job["id"]))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B), job.get("error_detail"))
        self.assertEqual(self.routed[-1], {"a"})

    def test_the_wait_ends_with_the_old_failure_once_the_queue_budget_is_spent(self) -> None:
        self.next_site = []
        row = self.job("yield-6")
        self.store._conn.execute("UPDATE jobs SET attempt_count = 100000 WHERE id = ?", (row["id"],))
        self.store._conn.commit()
        job = self.run_job(_YieldingFleet(), self.store.get(row["id"]))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "WORKSTATION_YIELDED"))
        self.assertIn("Nothing was charged", job["error_detail"])

    def test_a_pinned_voice_fails_at_once_when_its_mac_steps_aside(self) -> None:
        self.modality = "tts"
        row = self.job("yield-pinned", {"text": "x", "voice_id": "studio-hub-a::clone"})
        with patch.object(worker_module.fleet, "pinned_location", lambda _p: A):
            job = self.run_job(_YieldingFleet(), row)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "WORKSTATION_YIELDED"))
        self.assertIn("Nothing was charged; run it again", job["error_detail"])
        self.assertFalse(job["recovery_required"])
        self.assertEqual(self.routed, [], "a pinned voice has nowhere else to go")
        self.assertIsNone(job["defer_reason"])

    def test_a_paid_lane_is_never_offered_a_reroute(self) -> None:
        calls: list[str] = []

        class _Cloud:
            def generate(self, request):
                calls.append(request.provider_id)
                raise BackendError("WORKSTATION_YIELDED: odd", code="WORKSTATION_YIELDED", retryable=True,
                                   upstream_accepted=False, upstream_settled=True)

        with patch.object(worker_module, "adapter_for", lambda _d: _Cloud()):
            job = self.run_job(_Fleet(), self.job("paid-1"), _Registry({A: "openai_compat", B: "openai_compat"}))
        self.assertEqual((job["state"], calls, self.routed), ("failed", [A], []))


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, answer: httpx.Response, settings: object):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return answer
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(fleet, "invalidate"):
            try:
                return startup_control.set_node_workstation("ctl1", "shop-2", settings, client=client), seen
            except startup_control.StartupError as error:
                return error, seen

    def test_it_goes_to_the_controller_on_the_fleet_credential_and_returns_its_answer(self) -> None:
        answer, seen = self.send(httpx.Response(200, json={"ok": True, "node": "shop-2", "workstation": WORKSTATION}),
                                 {"enabled": True, "idle_minutes": 3, "quiet_hours": ["22:00-07:00"]})
        self.assertEqual(answer["workstation"]["state"], "standing_by")
        self.assertEqual(seen[0].url.path, "/api/hub/fleet/nodes/shop-2/workstation")
        self.assertEqual(seen[0].headers["x-hub-token"], "fleet-token")
        self.assertEqual(json.loads(seen[0].content),
                         {"enabled": True, "idle_minutes": 3, "quiet_hours": ["22:00-07:00"]})
        _, seen = self.send(httpx.Response(200, json={"ok": True}), {"start_free_gb": None})
        self.assertEqual(json.loads(seen[0].content), {"start_free_gb": None}, "null = each model's own floor")

    def test_an_older_controller_is_hub_too_old(self) -> None:
        for status in (404, 405):
            error, _ = self.send(httpx.Response(status, json={"detail": "Not Found"}), {"enabled": True})
            self.assertEqual((error.status, error.body["code"]), (status, "HUB_TOO_OLD"))
            self.assertIn("newer Studio Hub", error.body["detail"])

    def test_the_hubs_refusals_pass_through(self) -> None:
        for status, code in ((400, "BAD_SETTING"), (404, "NODE_UNKNOWN"), (502, "NODE_REFUSED"),
                             (502, "NODE_UNREACHABLE")):
            error, _ = self.send(httpx.Response(status, json={"ok": False, "code": code, "detail": "said the Hub"}),
                                 {"idle_minutes": 99})
            self.assertEqual((error.status, error.body["code"], error.body["detail"]), (status, code, "said the Hub"))

    def test_junk_never_reaches_the_wire(self) -> None:
        for junk in ("on", {}, {"colour": "red"}, {"memory_floor_gb": 2.5}):
            error, seen = self.send(httpx.Response(200, json={}), junk)
            self.assertEqual((error.status, error.body["code"], seen), (400, "BAD_SETTING", []))

    def test_the_admin_route_answers_the_refusal_with_its_status(self) -> None:
        refusal = startup_control.StartupError(404, {"ok": False, "code": "HUB_TOO_OLD", "detail": "x"})
        with patch.object(startup_control, "set_node_workstation", side_effect=refusal):
            response = admin.fleet_machine_workstation("ctl1", "shop-2", {"enabled": True})
        self.assertEqual((response.status_code, json.loads(response.body)["code"]), (404, "HUB_TOO_OLD"))
        with patch.object(startup_control, "set_node_workstation", return_value={"ok": True}) as called:
            self.assertEqual(admin.fleet_machine_workstation("ctl1", "shop-2", {"enabled": False}), {"ok": True})
        called.assert_called_once_with("ctl1", "shop-2", {"enabled": False})


def _row(machine: dict, node: dict | None = None, activity: dict | None = None) -> dict:
    return {"id": "ctl", "label": "Shops", "local": False, "reachable": True, "nodes": [node] if node else [],
            "activity": {"machines": [activity] if activity else []}, "machines": [machine], "workers": []}


def _flat(machine: dict, node: dict | None = None, activity: dict | None = None) -> dict:
    return fleet.machines_flat([_row(machine, node, activity)])[0]


class FleetRowTests(unittest.TestCase):
    def test_the_block_travels_from_the_machine_row_else_the_node_row(self) -> None:
        self.assertEqual(_flat({"machine": "m1", "reachable": True, "workstation": WORKSTATION})["workstation"]["state"],
                         "standing_by")
        node = {"id": "m1", "state": "reachable", "workstation": dict(WORKSTATION, state="working")}
        published = _flat({"machine": "m1", "reachable": True}, node)["workstation"]
        self.assertEqual((published["state"], published["jobs_today"], published["quiet_hours"]),
                         ("working", 3, ["22:00-07:00"]))
        custom = _flat({"machine": "m1", "reachable": True, "workstation": dict(WORKSTATION, start_free_gb=4.5)})
        self.assertEqual(custom["workstation"]["start_free_gb"], 4.5)
        self.assertIsNone(_flat({"machine": "m1", "reachable": True})["workstation"], "absent: Hub too old")

    def test_only_known_keys_of_the_right_type_pass(self) -> None:
        raw = dict(WORKSTATION, state="napping", idle_minutes="5", secret="x", stale=True)
        published = _flat({"machine": "m1", "reachable": True, "workstation": raw})["workstation"]
        self.assertNotIn("secret", published)
        self.assertIsNone(published["state"], "an unfamiliar state is unknown, never a guess")
        self.assertIsNone(published["idle_minutes"])
        self.assertIs(published["stale"], True)


class HealthTests(unittest.TestCase):
    ATTENTION = {"machine": "m1", "state": "needs_attention", "stalled_seconds": 300, "latest": {}}

    def test_a_resting_workstation_is_not_needs_attention_or_offline(self) -> None:
        for state in ("standing_by", "yielded", "quiet_hours"):
            for hub_said in ("needs_attention", "offline"):
                location = _row({"machine": "m1", "reachable": True, "workstation": dict(WORKSTATION, state=state)},
                                activity=dict(self.ATTENTION, state=hub_said))
                row = fleet.machines_flat([location])[0]
                self.assertEqual((row["state"], row["attention"]), (state, None))
                codes = {issue["code"] for issue in fleet._issues_for(location)}
                self.assertNotIn("machine_offline", codes)

    def test_a_job_finishing_on_it_still_reads_working_and_a_stale_block_changes_nothing(self) -> None:
        working = _flat({"machine": "m1", "reachable": True, "workstation": WORKSTATION},
                        activity={"machine": "m1", "state": "working", "current_job": {"id": "b"}})
        self.assertEqual(working["state"], "working")
        stale = _flat({"machine": "m1", "reachable": False, "workstation": dict(WORKSTATION, stale=True)},
                      activity={"machine": "m1", "state": "offline"})
        self.assertEqual(stale["state"], "offline", "a Mac that went away is offline, whatever it said last")

    def _notifier(self, store=None) -> notifier.Notifier:
        tmp = Path(tempfile.mkdtemp())
        return notifier.Notifier(transport=lambda *a: notifier.SendResult(True), clock=lambda: 1_760_000_000.0,
                                 sleep=lambda s: None, settings_path=tmp / "telegram.json",
                                 state_path=tmp / "state.json", sync=True, snapshot=lambda: None,
                                 store=store, disk_status=lambda: {"state": "green"})

    def test_telegram_raises_no_offline_alarm_for_a_resting_workstation(self) -> None:
        base = {"location_id": "ctl", "location": "Shops", "location_reachable": True, "label": "Shop 2",
                "node_state": "unreachable", "reachable": False}
        snap = {"locations": [{"id": "ctl", "label": "Shops", "reachable": True, "url": "http://x"}],
                "machines": [dict(base, machine="m1", workstation=dict(WORKSTATION, state="quiet_hours")),
                             dict(base, machine="m2", workstation=dict(WORKSTATION, stale=True)),
                             dict(base, machine="m3", workstation=None)]}
        keys = {c.key for c in self._notifier().offline_conditions(snap)}
        self.assertEqual(keys, {"mac:ctl:m2", "mac:ctl:m3"})

    def test_yields_are_not_a_failure_burst(self) -> None:
        from gateway.store import JobStore

        store = JobStore(Path(tempfile.mkdtemp()) / "jobs.sqlite3")
        self.addCleanup(store.close)
        now = 1_760_000_000.0
        for i in range(8):
            job, _ = store.submit(modality="image", model_id="m", params={}, idempotency_key=f"k{i}")
            with store._lock:
                store._conn.execute("UPDATE jobs SET state='failed', error_code='WORKSTATION_YIELDED', updated_at=? "
                                    "WHERE id=?", (now - 60, job["id"]))
                store._conn.commit()
        n = self._notifier(store)
        n.save_settings({"bot_token": "123456789:AAH-fake_token_for_tests_0123456789abc", "chat_id": "-100123"})
        self.assertFalse([c for c in n.job_conditions(now) if c.key == "jobs:failures"])


if __name__ == "__main__":
    unittest.main()
