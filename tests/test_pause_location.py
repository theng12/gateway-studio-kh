#!/usr/bin/env python3
"""Pause a location or a node by hand (owner request 2026-10-03; `local` is the Mac running the Gateway).

A paused location gets no new work from routing, late binding or retry-elsewhere; jobs already running
there finish; it stays listed and comes back on Resume. Kept across restarts, and the automatic outage
resume never lifts it. A clone voice the gateway holds travels with the job; a voice that exists only on
a paused Mac fails with a plain sentence. A node is paused on its controller (Hub 0.6.135).
"""
from __future__ import annotations

import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from test_late_binding import COMPOUND, MODALITY, MODEL, _Base, _Fleet, _location, _pid  # sets the sandbox env

from gateway import admin, api, fleet, startup_control  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.worker import Worker  # noqa: E402

LOCAL_VOICE = f"{_pid('local')}::voice-abc"


class _Paused(_Base):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = patch.object(fleet, "FLEET_PATH", self.tmp / "fleet.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        fleet._write([{"id": "a", "url": "http://a.example:47873", "label": "A"}])

    def candidates(self, locations: list[dict]) -> set[str]:
        with _Fleet(locations):
            return {c["location_id"] for c in fleet.routing_candidates(MODEL, MODALITY)}


class PauseTest(_Paused):
    def test_it_persists_and_resume_clears_it_for_both_kinds_of_location(self) -> None:
        for location in ("local", "a"):
            self.assertIsNone(fleet.owner_paused_at(location))
            fleet.set_owner_pause(location, True)
            self.assertIsNotNone(fleet.owner_paused_at(location))   # a fresh read of the file, as after a restart
        self.assertIsNone(fleet.set_owner_pause("ghost", True))
        fleet.resume_location("local")
        fleet.resume_location("a")
        self.assertIsNone(fleet.owner_paused_at("local"))
        self.assertIsNone(fleet.owner_paused_at("a"))

    def test_pausing_one_does_not_lose_the_other_or_the_name(self) -> None:
        fleet.rename_controller("local", "Studio")
        fleet.set_owner_pause("local", True)
        fleet.set_owner_pause("a", True)
        fleet.rename_controller("a", "A2")          # any roster write must keep the local pause
        self.assertEqual(fleet.local_label(), "Studio")
        self.assertIsNotNone(fleet.owner_paused_at("local"))
        self.assertIsNotNone(fleet.owner_paused_at("a"))

    def test_the_outage_auto_resume_never_lifts_an_owner_pause(self) -> None:
        fleet.set_owner_pause("a", True)
        row = fleet._read()[0]
        fleet.note_reachability(row, False)
        fleet.note_reachability(row, True)          # the Mac answers again
        fleet._write([row])
        self.assertIsNotNone(fleet.owner_paused_at("a"))

    def test_the_published_row_says_who_paused_it(self) -> None:
        owner, outage, plain = {}, {"paused_at": 5.0}, {}
        fleet.set_owner_pause("a", True)
        fleet._stamp_pause(owner, "a")
        fleet._stamp_pause(outage, "ghost")
        fleet._stamp_pause(plain, "ghost")
        self.assertEqual(owner["paused_by"], "owner")
        self.assertIn("Paused by you", owner["pause_reason"])
        self.assertEqual(outage["paused_by"], "outage")
        self.assertNotIn("paused_by", plain)

    def test_admin_routes(self) -> None:
        self.assertEqual(admin.fleet_pause("local")["controller"]["paused_by"], "owner")
        self.assertIsNotNone(fleet.owner_paused_at("local"))
        self.assertEqual(admin.fleet_pause("ghost").status_code, 404)
        self.assertIsNone(admin.fleet_resume("local")["controller"]["paused_by"])
        self.assertIsNone(fleet.owner_paused_at("local"))


class RoutingTest(_Paused):
    def test_a_paused_location_is_not_a_candidate_and_resume_brings_it_back(self) -> None:
        rows = [_location("local", eligible=1, available=1), _location("a", eligible=2, available=2)]
        self.assertEqual(self.candidates(rows), {"local", "a"})
        fleet.set_owner_pause("local", True)
        self.assertEqual(self.candidates(rows), {"a"})
        with _Fleet(rows):
            self.assertEqual(fleet.route(MODEL, MODALITY)[0].id, _pid("a"))
            # retry-elsewhere: the next site is still never the paused one
            self.assertIsNone(fleet.route(MODEL, MODALITY, exclude_locations={"a"})[0])
        fleet.resume_location("local")
        self.assertEqual(self.candidates(rows), {"local", "a"})

    def test_another_location_can_be_paused_too(self) -> None:
        rows = [_location("local", eligible=1, available=1), _location("a", eligible=2, available=2)]
        fleet.set_owner_pause("a", True)
        self.assertEqual(self.candidates(rows), {"local"})

    def test_submit_never_binds_to_a_paused_location_and_a_legacy_id_is_rerouted(self) -> None:
        fleet.set_owner_pause("local", True)
        with _Fleet([_location("local", eligible=9, available=9), _location("a", eligible=1, available=1)]), \
                patch.object(api, "store", self.store):
            api._submit(MODALITY, COMPOUND, {}, "k1")
            api._submit(MODALITY, f"{_pid('local')}::{MODEL}", {}, "k2")
            self.assertEqual(self.bound(), {_pid("a"): 2})

    def test_only_a_paused_location_offers_it_gives_a_plain_503(self) -> None:
        fleet.set_owner_pause("local", True)
        with _Fleet([_location("local", eligible=1, available=1)]), patch.object(api, "store", self.store):
            with self.assertRaises(api.GatewayError) as caught:
                api._submit(MODALITY, COMPOUND, {}, "k")
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("1 you paused", caught.exception.detail)

    def test_a_queued_job_bound_to_a_paused_location_moves_to_the_fleet(self) -> None:
        self.store.submit(modality=MODALITY, model_id=MODEL, params={}, idempotency_key="q",
                          provider_id=_pid("local"))
        ran: list[str] = []
        done = threading.Event()

        def execute(row, store):
            ran.append(row["provider_id"])
            store.succeed(row["id"], result={})
            done.set()

        fleet.set_owner_pause("local", True)
        with _Fleet([_location("local", eligible=1, available=1), _location("a", eligible=1, available=1)]), \
                patch.object(worker_module, "execute_job", execute):
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            worker.start()
            try:
                self.assertTrue(done.wait(5))
            finally:
                worker.wait_idle(5)
                worker.stop()
        self.assertEqual(ran, [_pid("a")])


class VoiceTest(_Paused):
    def _voice(self, clip: Path | None):
        raw = {"reference_path": str(clip)} if clip else {}
        return patch("gateway.voices.resolve", return_value=types.SimpleNamespace(raw=raw))

    def test_a_gateway_held_clone_routes_to_the_fleet_while_this_mac_is_paused(self) -> None:
        clip = self.tmp / "chris.wav"
        clip.write_bytes(b"clip")
        fleet.set_owner_pause("local", True)
        params = {"voice_id": LOCAL_VOICE}
        with _Fleet([_location("local", eligible=1, available=1), _location("a", eligible=1, available=1)]), \
                self._voice(clip), patch.object(api, "store", self.store):
            self.assertIsNone(fleet.pause_message_for_voice(params))
            api._submit(MODALITY, COMPOUND, params, "k")
            self.assertEqual(self.bound(), {_pid("a"): 1})

    def test_a_hub_only_voice_on_a_paused_mac_fails_with_a_sentence_naming_the_pause(self) -> None:
        fleet.set_owner_pause("local", True)
        with _Fleet([_location("local", eligible=1, available=1), _location("a", eligible=1, available=1)]), \
                self._voice(None), patch.object(api, "store", self.store):
            with self.assertRaises(api.GatewayError) as caught:
                api._submit(MODALITY, COMPOUND, {"voice_id": LOCAL_VOICE}, "k")
        self.assertEqual(caught.exception.detail, "This voice only exists on This Mac, which you paused. "
                                                  "Share it to the fleet or resume This Mac.")

    def test_it_runs_where_it_lives_when_nothing_is_paused(self) -> None:
        with self._voice(None):
            self.assertIsNone(fleet.pause_message_for_voice({"voice_id": LOCAL_VOICE}))

    def test_a_job_queued_before_the_pause_is_failed_with_the_sentence(self) -> None:
        job, _ = self.store.submit(modality=MODALITY, model_id=MODEL, idempotency_key="v",
                                   params={"voice_id": LOCAL_VOICE}, provider_id=_pid("local"))
        fleet.set_owner_pause("local", True)
        with self._voice(None):
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            self.assertTrue(worker._stranded_on_paused(self.store.get(job["id"])))
        row = self.store.get(job["id"])
        self.assertEqual(row["state"], "failed")
        self.assertIn("which you paused", json.dumps(row))


class NodePauseTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def pause(self, answer, paused=True):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return answer
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(fleet, "invalidate") as invalidated:
            try:
                return startup_control.set_node_pause("ctl1", "node-1-hub", paused, client=client), seen, invalidated
            except startup_control.StartupError as error:
                return error, seen, invalidated

    def test_it_goes_to_the_controller_on_the_fleet_credential(self) -> None:
        answer, seen, invalidated = self.pause(httpx.Response(200, json={"ok": True, "node": "node-1-hub", "paused": True}))
        self.assertTrue(answer["ok"])
        self.assertEqual(seen[0].url.path, "/api/hub/fleet/nodes/node-1-hub/pause")
        self.assertEqual(seen[0].headers["x-hub-token"], "fleet-token")
        self.assertEqual(json.loads(seen[0].content), {"paused": True})
        invalidated.assert_called_once()

    def test_an_older_hub_gets_a_plain_sentence_naming_the_version(self) -> None:
        error, _, _ = self.pause(httpx.Response(404, json={"detail": "Not Found"}))
        self.assertEqual(error.body["code"], "HUB_TOO_OLD")
        self.assertIn("0.6.135", error.body["detail"])

    def test_the_controllers_refusal_reaches_the_caller_and_junk_is_refused_locally(self) -> None:
        error, _, _ = self.pause(httpx.Response(404, json={"ok": False, "code": "NODE_UNKNOWN", "detail": "no such Mac"}))
        self.assertEqual((error.status, error.body["code"]), (404, "NODE_UNKNOWN"))
        error, seen, _ = self.pause(httpx.Response(200, json={}), paused="yes")
        self.assertEqual((error.status, seen), (400, []))

    def test_the_machine_row_carries_the_hubs_flag(self) -> None:
        node = {"id": "n1", "paused": True}
        row = {"id": "ctl", "label": "C", "local": False, "reachable": True, "nodes": [node],
               "activity": {"machines": []}, "machines": [{"machine": "n1", "reachable": True}], "workers": []}
        machine, = fleet.machines_flat([row])
        self.assertIs(machine["node_paused"], True)


if __name__ == "__main__":
    unittest.main()
