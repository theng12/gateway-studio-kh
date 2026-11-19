#!/usr/bin/env python3
"""A site that answers but has no Mac for a model is refused at the door, not queued.

**The real case (measured live 2026-11-02).** Gemma 4
(`studio-hub::mlx-community/gemma-4-26B-A4B-it-qat-4bit`) was `eligible 0` at every site that
takes customer work: 1600 answered with one Mac (1607) gone, 1100 / 1200 / 1500 / Shops answered
with Macs up and none for it, and Mac Studio — the only other site with an eligible Mac — was
paused by the owner. The catalogue still published the row unlocked, and a submit was **accepted**
and sat until the 3-hour ceiling with nothing able to run it.

Two things were wrong, and both are pinned here:

1. **"The controller answers" is not "a Mac can run this."** `_story_row` locked only when the
   site was not in `usable`, so a site that answers with nine Macs up and `eligible 0` for one
   model published `locked: false`.
2. **`spent: true` did not refuse anything.** `routing_candidates` ends
   `return [c for c in candidates if not c["spent"]] or candidates`, so a spent site was still the
   last resort and the job was accepted.

**What is true here.** Unstated supply is unknown, never "none" (an older Hub keeps every
fallback). A Mac that is merely busy — `eligible 1, available 0` — keeps queueing with its
`waiting` sentence. One read cannot flip a model off: the 45 s `LOCK_ANSWER_GRACE_S` that protects
a slow site answer protects a missed `eligible > 0` the same way, per (site, model). Image pools
are exempt: a pool's authority is its own `assigned_macs`, not a per-model statement.

**And a job already bound to a site is bounded on the same evidence** (0.2.289): a site that
publishes this model and has nothing free **and no work on it in flight** is not a queue that
moves, so its wait drops from the 45-minute ceiling to `_UNUSABLE_SITE_WAIT_S` (5 min). It stays a
routing candidate, deliberately — see `test_a_busy_mac_stays_a_candidate`; the two are different
questions asked at different moments.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-admission-no-eligible-")

from gateway import api, catalog, config, fleet  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.providers.definitions import KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402

SITE_1600 = "2c7a2403564b"
SITE_1100 = "1100abc"
SITE_STUDIO = "bf33509789c5"  # Mac Studio: paused by the owner
MODEL_ID = "mlx-community/gemma-4-26B-A4B-it-qat-4bit"
POOL_MEMBER = "mlx-community/heavy-image-model"
MODALITY = "chat"


def _site(
    site_id: str,
    *,
    eligible: int | None = 0,
    available: int = 0,
    reachable_macs: int = 10,
    accepting: bool = True,
    paused: bool = False,
    role: str = "controller",
) -> dict:
    """One site row, shaped the way the Hub publishes it.

    `eligible=None` publishes no per-model supply row at all — an older Hub — which must stay
    unknown rather than reading as "none".
    """
    row: dict = {
        "id": site_id,
        "provider_id": f"studio-hub-{site_id}",
        "label": f"Site {site_id}",
        "reachable": True,
        "slow": False,
        "answered_at": time.time(),
        "role": role,
        "drained": False,
        "capacity": {"accepting_customer_jobs": accepting, "queue_depth": 0},
        "paused_at": time.time() if paused else None,
        "machines": [{"machine": f"mac-{i}", "reachable": True} for i in range(reachable_macs)],
        "machine_summary": {"reachable": reachable_macs, "total": reachable_macs},
        "running_jobs": [],
        "model_supply": {} if eligible is None else {MODEL_ID: {"eligible": eligible,
                                                               "available": available}},
    }
    return row


def _definition(site_id: str, modality: str = MODALITY) -> ProviderDefinition:
    return ProviderDefinition(
        id=f"studio-hub-{site_id}", display_name="Studio Hub", category="multi",
        kind=KIND_STUDIO_HUB, base_url="http://mock", modalities=(modality,),
        discovery="static",
    )


def _model_row(model_id: str = MODEL_ID, modality: str = MODALITY) -> dict:
    return {"model_id": model_id, "display_name": "Gemma 4", "modality": modality,
            "enabled": True, "raw": {}}


def _enabled(*sites: str, model_id: str = MODEL_ID, modality: str = MODALITY):
    """`registry.models.enabled_models` stubbed to offer `model_id` at each named site."""

    def _fn(requested: str):
        if requested != modality:
            return []
        return [(_definition(s, modality), _model_row(model_id, modality)) for s in sites]

    return _fn


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        # The grace tracker is module state; a test that left a positive reading behind would
        # rescue the next test's zero, and a test that left a zero would poison the next.
        fleet._ELIGIBLE_AT.clear()

    def patches(self, rows: list[dict], *, sites: list[str], model_id: str = MODEL_ID,
                modality: str = MODALITY):
        """The two reads every path here takes: the fleet snapshot and the enabled set."""
        return (
            patch.object(fleet, "snapshot", lambda **_kw: {"locations": list(rows)}),
            patch.object(registry.models, "enabled_models", _enabled(*sites, model_id=model_id,
                                                                     modality=modality)),
        )


class TheSiteStatesNoMacTest(_Case):
    """`states_no_mac` is one answer, asked by the row and by routing alike."""

    def test_a_site_with_eligible_zero_and_macs_up_states_no_mac(self) -> None:
        # The real case: 1600 answers, nine Macs reachable, none of them for this model.
        self.assertTrue(fleet.states_no_mac(_site(SITE_1600, eligible=0, reachable_macs=9),
                                            MODEL_ID, now=1_000.0))

    def test_unstated_supply_never_reads_as_none(self) -> None:
        """An older Hub publishes no per-model row: unknown, not failed."""
        self.assertFalse(fleet.states_no_mac(_site(SITE_1600, eligible=None), MODEL_ID, now=1_000.0))

    def test_a_number_that_is_not_a_number_is_not_a_reading(self) -> None:
        row = _site(SITE_1600, eligible=None)
        row["model_supply"] = {MODEL_ID: {"eligible": "not-a-count"}}
        self.assertFalse(fleet.states_no_mac(row, MODEL_ID, now=1_000.0))

    def test_a_busy_mac_is_not_none(self) -> None:
        """`eligible 1, available 0` is a Mac that is busy, not a Mac that is absent."""
        self.assertFalse(fleet.states_no_mac(_site(SITE_1600, eligible=1, available=0),
                                             MODEL_ID, now=1_000.0))

    def test_one_zero_read_inside_the_grace_is_a_blip(self) -> None:
        # A Mac missing one heartbeat reads eligible 0 for a single read.
        healthy = _site(SITE_1600, eligible=2, available=2)
        self.assertFalse(fleet.states_no_mac(healthy, MODEL_ID, now=1_000.0))
        blip = _site(SITE_1600, eligible=0)
        self.assertFalse(fleet.states_no_mac(blip, MODEL_ID, now=1_000.0 + 10),
                         "one missed read inside the grace must not flip the model off")

    def test_a_zero_past_the_grace_is_the_sites_answer(self) -> None:
        fleet.states_no_mac(_site(SITE_1600, eligible=2), MODEL_ID, now=1_000.0)
        later = _site(SITE_1600, eligible=0)
        self.assertTrue(fleet.states_no_mac(later, MODEL_ID,
                                            now=1_000.0 + fleet.LOCK_ANSWER_GRACE_S + 1))

    def test_a_site_that_never_stated_one_reads_as_none_at_once(self) -> None:
        """No earlier reading to protect, so the real case is not delayed 45 s."""
        self.assertTrue(fleet.states_no_mac(_site(SITE_1600, eligible=0), MODEL_ID, now=1_000.0))

    def test_the_grace_is_held_per_site_and_model(self) -> None:
        fleet.states_no_mac(_site(SITE_1600, eligible=2), MODEL_ID, now=1_000.0)
        # Another site, and another model at this site, both read as none at once.
        self.assertTrue(fleet.states_no_mac(_site(SITE_1100, eligible=0), MODEL_ID, now=1_001.0))
        other = _site(SITE_1600, eligible=None)
        other["model_supply"] = {"some/other-model": {"eligible": 0}}
        self.assertTrue(fleet.states_no_mac(other, "some/other-model", now=1_001.0))


class TheRoutingSkipsItTest(_Case):
    """A site that states no Mac is not a candidate, so `route` finds nothing."""

    def test_a_site_stating_no_mac_is_not_a_candidate(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9),
                _site(SITE_1100, eligible=0, reachable_macs=5)]
        snap, enabled = self.patches(rows, sites=[SITE_1600, SITE_1100])
        with snap, enabled:
            self.assertEqual(fleet.routing_candidates(MODEL_ID, MODALITY), [],
                             "a site that states no Mac for this model is not a candidate")

    def test_route_returns_nothing_when_no_site_can_take_it(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9)]
        snap, enabled = self.patches(rows, sites=[SITE_1600])
        with snap, enabled:
            chosen, note = fleet.route(MODEL_ID, MODALITY)
        self.assertIsNone(chosen, note)

    def test_the_fallback_chain_does_not_resurrect_it(self) -> None:
        """`by_operation`, the site total and the machine count must not override the statement."""
        row = _site(SITE_1600, eligible=0, reachable_macs=9)
        row["capacity"] = {"accepting_customer_jobs": True, "queue_depth": 0,
                           "eligible_physical_machine_slots_total": 12,
                           "by_operation": {"chat.response": {
                               "eligible_physical_machine_slots_total": 7}}}
        snap, enabled = self.patches([row], sites=[SITE_1600])
        with snap, enabled:
            self.assertEqual(fleet.routing_candidates(MODEL_ID, MODALITY), [])

    def test_unstated_supply_keeps_its_fallbacks(self) -> None:
        """An older Hub that states nothing is still weighted, never read as "none"."""
        row = _site(SITE_1600, eligible=None, reachable_macs=4)
        row["capacity"] = {"accepting_customer_jobs": True, "queue_depth": 0,
                           "eligible_physical_machine_slots_total": 4}
        snap, enabled = self.patches([row], sites=[SITE_1600])
        with snap, enabled:
            candidate, = fleet.routing_candidates(MODEL_ID, MODALITY)
        self.assertFalse(candidate["spent"])
        self.assertEqual(candidate["slots"], 4)

    def test_a_busy_mac_stays_a_candidate(self) -> None:
        snap, enabled = self.patches([_site(SITE_1600, eligible=1, available=0)],
                                     sites=[SITE_1600])
        with snap, enabled:
            candidate, = fleet.routing_candidates(MODEL_ID, MODALITY)
        self.assertFalse(candidate["spent"], "a busy Mac keeps queueing")

    def test_one_live_site_takes_the_work(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9),
                _site(SITE_1100, eligible=1, available=1, reachable_macs=1)]
        snap, enabled = self.patches(rows, sites=[SITE_1600, SITE_1100])
        with snap, enabled:
            candidates = fleet.routing_candidates(MODEL_ID, MODALITY)
        self.assertEqual([c["location_id"] for c in candidates], [SITE_1100])

    def test_a_paused_site_is_not_a_route_even_with_an_eligible_mac(self) -> None:
        # Mac Studio had the model and was paused by the owner: it takes no customer work.
        snap, enabled = self.patches([_site(SITE_STUDIO, eligible=1, paused=True)],
                                     sites=[SITE_STUDIO])
        with snap, enabled:
            self.assertEqual(fleet.routing_candidates(MODEL_ID, MODALITY), [])


class ThePublishedRowLocksTest(_Case):
    """The merged row locks only when **every** answering site states no Mac."""

    def _rows(self, rows: list[dict], sites: list[str]) -> list[dict]:
        snap, enabled = self.patches(rows, sites=sites)
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            return catalog.catalog.rows(MODALITY)

    def _row(self, rows: list[dict], sites: list[str]) -> dict:
        found = [r for r in self._rows(rows, sites) if r.get("id") == f"studio-hub::{MODEL_ID}"]
        self.assertTrue(found, f"the merged row must exist: {[r.get('id') for r in self._rows(rows, sites)]}")
        return found[0]

    def test_every_site_stating_no_mac_locks_the_row_with_macs_still_up(self) -> None:
        row = self._row([_site(SITE_1600, eligible=0, reachable_macs=9),
                         _site(SITE_1100, eligible=0, reachable_macs=5)],
                        [SITE_1600, SITE_1100])
        self.assertIs(row["locked"], True, row)
        self.assertEqual(row["availability"], "unavailable")
        self.assertIs(row["available"], False)

    def test_one_live_site_keeps_the_row_unlocked(self) -> None:
        row = self._row([_site(SITE_1600, eligible=0, reachable_macs=9),
                         _site(SITE_1100, eligible=1, available=1)],
                        [SITE_1600, SITE_1100])
        self.assertIs(row["locked"], False, "one site that can run it is enough")

    def test_unstated_supply_does_not_lock(self) -> None:
        row = self._row([_site(SITE_1600, eligible=None)], [SITE_1600])
        self.assertIs(row["locked"], False, "unstated supply is unknown, not none")

    def test_the_sentence_does_not_claim_the_sites_are_offline(self) -> None:
        """The site is up; saying so would send the owner looking at the wrong thing."""
        row = self._row([_site(SITE_1600, eligible=0, reachable_macs=9)], [SITE_1600])
        self.assertIn("No Mac able to run this model is available right now",
                      row["lock_reason"] or "")
        self.assertNotIn("offline or not taking customer work", row["lock_reason"] or "")

    def test_a_site_that_never_answered_keeps_the_same_sentence(self) -> None:
        """One sentence for both conditions, so the picker and the error cannot disagree."""
        row = self._row([_site(SITE_1600, eligible=1, accepting=False)], [SITE_1600])
        self.assertIs(row["locked"], True, row)
        self.assertEqual(row["lock_reason"], catalog._NO_MAC_REASON)


class ABoundJobOnASiteWithNothingFreeTest(_Case):
    """The third shape (0.2.289): publishes the model, nothing free, nothing in flight on it.

    Measured 2026-11-19: a site that had answered 148 times that day kept `available` at 0 for a
    whole 45-minute wait while taking other work, and the job ended saying only "waiting for a
    free Mac" -- 45 minutes for an answer the site gave in its first reply.
    """

    def _row(self, *, available: int, running: tuple[str, ...] = (), supply=None) -> dict:
        row = _site(SITE_1600, eligible=3, available=available, reachable_macs=3)
        if supply is not None:
            row["model_supply"] = {MODEL_ID: supply}
        row["running_jobs"] = [{"id": f"batch-{i}", "model_id": m}
                               for i, m in enumerate(running)]
        return row

    def _cannot_take(self, row: dict) -> bool:
        with patch.object(fleet, "snapshot", lambda **_kw: {"locations": [row]}):
            return fleet.site_cannot_take(f"studio-hub-{SITE_1600}", MODEL_ID)

    def test_nothing_free_and_nothing_in_flight_cannot_take(self) -> None:
        """The failing path: the site answers, publishes the model, and has nothing to run it on."""
        self.assertTrue(self._cannot_take(self._row(available=0)))

    def test_one_free_mac_restores_the_normal_ceiling(self) -> None:
        self.assertFalse(self._cannot_take(self._row(available=1)))

    def test_no_available_key_is_unknown_not_a_refusal(self) -> None:
        self.assertFalse(self._cannot_take(self._row(available=0, supply={"eligible": 3})))

    def test_work_on_this_model_at_the_site_is_a_queue_that_moves(self) -> None:
        self.assertFalse(self._cannot_take(self._row(available=0, running=(MODEL_ID,))))

    def test_work_on_another_model_does_not_keep_the_job_waiting(self) -> None:
        self.assertTrue(self._cannot_take(self._row(available=0, running=("some/other-model",))))


class AStaleFleetReadIsNotAFleetTest(_Case):
    """`allow_stale` returns the cached snapshot however old it is -- right for a page, wrong here.

    Nothing else ages a row out: `_machine_offline` reads "not stated" as unknown, and a stalled
    refresher leaves every row saying `reachable: True`. Past `_ROUTING_VIEW_MAX_AGE_S` the row is
    `unknown`, which is what "we have not heard" has always meant -- never a refusal.
    """

    def _candidates(self, *, age: float, rejections: list[dict] | None = None) -> list[dict]:
        row = _site(SITE_1600, eligible=1, available=1, reachable_macs=1)
        view = {"locations": [row], "generated_at": time.time() - age}
        snap = patch.object(fleet, "snapshot", lambda **_kw: view)
        enabled = patch.object(registry.models, "enabled_models", _enabled(SITE_1600))
        with snap, enabled:
            return fleet.routing_candidates(MODEL_ID, MODALITY, rejections=rejections)

    def test_a_view_past_the_bound_offers_nothing(self) -> None:
        # Ten minutes: no plausible bound is longer, and a slow fan-out is not this slow.
        self.assertEqual(self._candidates(age=600.0), [])

    def test_a_fresh_view_still_offers_the_site(self) -> None:
        candidate, = self._candidates(age=1.0)
        self.assertEqual(candidate["location_id"], SITE_1600)

    def test_the_skip_says_unknown_and_says_how_old(self) -> None:
        rejections: list[dict] = []
        self._candidates(age=600.0, rejections=rejections)
        self.assertEqual([r["reason"] for r in rejections], ["unknown"])
        self.assertIn("600 s old", rejections[0]["detail"])


class PoolsStayExemptTest(_Case):
    """A pool's authority is its own `assigned_macs`, not a per-model statement."""

    def test_a_pool_still_finds_a_site_that_states_no_mac(self) -> None:
        row = _site(SITE_1600, eligible=0, reachable_macs=1)
        row["machines"] = [{"machine": "mac-0", "reachable": True,
                            "assigned_image_model": POOL_MEMBER}]
        row["local_models"] = [{"model_id": POOL_MEMBER, "machine": "mac-0",
                                "memory_eligible": True, "modality": "image"}]
        snap, enabled = self.patches([row], sites=[SITE_1600], model_id=POOL_MEMBER,
                                     modality="image")
        with snap, enabled:
            found = fleet.pool_candidates([f"studio-hub::{POOL_MEMBER}"], "image")
        self.assertEqual([c["_member"] for c in found], [POOL_MEMBER],
                         "a pool member a Mac is assigned and can run is still a candidate")

    def test_the_gate_can_be_turned_off_by_the_caller(self) -> None:
        snap, enabled = self.patches([_site(SITE_1600, eligible=0)], sites=[SITE_1600])
        with snap, enabled:
            gated = fleet.routing_candidates(MODEL_ID, MODALITY)
            open_ = fleet.routing_candidates(MODEL_ID, MODALITY, respect_model_supply=False)
        self.assertEqual(gated, [])
        self.assertEqual(len(open_), 1)


class TheDoorRefusesTest(_Case):
    """Through the real `POST /v1/chat/responses`, not just the helpers."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        self.client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(self.client.close)
        self._n = 0

    def headers(self) -> dict:
        # Keyless development accepts any token on loopback; otherwise the configured one.
        return {"Authorization": f"Bearer {config.gateway_api_key() or 'admission-door-test'}"}

    def post(self, key: str):
        self._n += 1
        return self.client.post(
            "/v1/chat/responses",
            headers={**self.headers(), "Idempotency-Key": key},
            json={"model_id": f"studio-hub::{MODEL_ID}", "prompt": "hi"},
        )

    def test_a_submit_is_refused_503_with_the_rows_sentence(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9),
                _site(SITE_1100, eligible=0, reachable_macs=5),
                _site(SITE_STUDIO, eligible=1, paused=True)]
        sites = [SITE_1600, SITE_1100, SITE_STUDIO]
        snap, enabled = self.patches(rows, sites=sites)
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            row, = [r for r in catalog.catalog.rows(MODALITY) if r.get("id") == f"studio-hub::{MODEL_ID}"]
            self.assertIs(row["locked"], True, "the row the door must agree with")
            answer = self.post("admission-locked-0001")
        self.assertEqual(answer.status_code, 503, answer.text)
        body = answer.json()
        self.assertEqual(body["code"], "MODEL_LOCKED", body)
        self.assertEqual(body["detail"], row["lock_reason"], "the same sentence, not a second story")

    def test_no_job_row_is_created_for_the_refusal(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9)]
        snap, enabled = self.patches(rows, sites=[SITE_1600])
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            answer = self.post("admission-locked-0002")
        self.assertEqual(answer.status_code, 503, answer.text)
        self.assertIsNone(api.store.find_by_idempotency_key("admission-locked-0002"),
                          "a refusal must not leave a job behind to sit on the ceiling")

    def test_a_busy_mac_is_still_accepted_and_queues(self) -> None:
        rows = [_site(SITE_1600, eligible=1, available=0, reachable_macs=1)]
        snap, enabled = self.patches(rows, sites=[SITE_1600])
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            answer = self.post("admission-busy-0001")
        self.assertIn(answer.status_code, (200, 202), answer.text)
        job = api.store.find_by_idempotency_key("admission-busy-0001")
        self.assertIsNotNone(job, "a busy Mac is a queue, not a lock")
        self.assertEqual(job["state"], "accepted")
        self.assertIsNone(job.get("waiting"),
                          "`waiting` is a payload fact, null until a Mac is actually waiting on it")

    def test_one_live_site_still_takes_the_job(self) -> None:
        rows = [_site(SITE_1600, eligible=0, reachable_macs=9),
                _site(SITE_1100, eligible=1, available=1, reachable_macs=1)]
        snap, enabled = self.patches(rows, sites=[SITE_1600, SITE_1100])
        with snap, enabled:
            catalog.catalog.rebuild(force=True)
            answer = self.post("admission-live-0001")
        self.assertIn(answer.status_code, (200, 202), answer.text)
        job = api.store.find_by_idempotency_key("admission-live-0001")
        self.assertIsNotNone(job)
        self.assertEqual(job["provider_id"], f"studio-hub-{SITE_1100}",
                         "the work goes to the one site that can run it")


if __name__ == "__main__":
    unittest.main()
