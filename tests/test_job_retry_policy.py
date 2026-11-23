#!/usr/bin/env python3
"""A7 — three attempts, different machines, never a duplicate.

The owner's decision E, in his words: *"i want a 'job' that's failed to retry on a different
machine, be it in the same cluster or a different site. just make sure no duplicates but also
make sure the job is retried 3 times. if all 3 fails. that it's declared failed properly to
story studio."*

The defect this replaces (audit H5/H6) was two-sided. The exclusion set was built from
`provider_id` (`studio-hub-9701220bf30f`) and compared against `location_id`
(`9701220bf30f`), so it never matched and a busy machine deferred the job **at the same
machine** while an idle one offered the model — 39 live `MACHINE_BUSY` failures. And a machine
that failed *after* the batch started arrived as a settled `PROVIDER_FAILED` with no re-route
at all.

Every test here is about the boundary the policy must not cross: **a retry happens only when
the previous attempt is known to have failed.** An upstream that accepted the work and went
silent is an unknown outcome, is never re-run automatically, and stays a reconciliation hold.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Without this the first `import gateway` binds the process to the owner's real Application
# Support, and every file imported after it inherits that (10 contract tests then failed on
# a database that already held their idempotency keys).
from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

from gateway import concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402


class _Store:
    """A store with the attempt columns, in memory."""

    def __init__(self, job: dict[str, Any]) -> None:
        self.job = dict(job)
        self.log: list[dict[str, Any]] = []
        self.tried: list[str] = []
        self.routes: list[str] = []
        self.failed: list[dict[str, Any]] = []
        self.holds: list[dict[str, Any]] = []
        self.deferred: list[dict[str, Any]] = []
        self.succeeded: list[dict[str, Any]] = []

    def get(self, job_id: str) -> dict[str, Any]:
        return dict(self.job, attempt_log=self.log, attempted_locations=self.tried)

    def record_attempt(self, job_id: str, *, location_id: str, code: str, detail: str):
        self.log.append({"location": location_id, "code": code, "detail": detail,
                         "at": 1.0 + len(self.log)})
        if location_id and location_id not in self.tried:
            self.tried.append(location_id)
        return list(self.log)

    def attempts_of(self, job_id: str):
        return list(self.log)

    def record_route(self, job_id: str, provider_id: str) -> None:
        self.routes.append(provider_id)

    def fail(self, job_id: str, **kwargs: Any) -> None:
        self.failed.append(kwargs)

    def hold(self, job_id: str, **kwargs: Any) -> None:
        self.holds.append(kwargs)

    def defer(self, job_id: str, *, delay_s: float, reason: str, new_round: bool = False) -> bool:
        self.deferred.append({"delay_s": delay_s, "reason": reason, "new_round": new_round})
        return True


class _Site:
    def __init__(self, site_id: str) -> None:
        self.id = f"studio-hub-{site_id}"
        self.label = site_id


class _PoolStore(_Store):
    def record_route(self, job_id: str, provider_id: str, *, model_id: str | None = None) -> bool:
        self.routes.append(provider_id)
        return True

    def defer(self, job_id: str, *, delay_s: float, reason: str, new_round: bool = False,
              unsent: bool = False) -> bool:
        self.deferred.append({"delay_s": delay_s, "reason": reason, "unsent": unsent})
        return True


class PoolRerouteTest(unittest.TestCase):
    """An image-pool re-route reads the job's room hook; it used to read it before assigning it."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.addCleanup(worker_module._ROOM_HOOKS.clear)

    def _next(self, store, room=None):
        if room is not None:
            worker_module._ROOM_HOOKS[store.job["id"]] = room
        seen: dict[str, Any] = {}

        def route_pool(members, modality, *, exclude_locations=None, load=None, **kwargs):
            seen["load"] = load
            return _Site("b"), members[0], ""

        with patch.object(worker_module, "_pool_members_for", lambda job, modality: ["pool-model"]), \
                patch.object(worker_module.fleet, "route_pool", route_pool):
            answer = worker_module._next_machine(store, store.job["id"], "pool-model", "image",
                                                 "studio-hub-a", code="PROVIDER_FAILED", detail="x")
        return answer, seen

    def test_pool_reroute_without_a_room_hook_moves_the_job(self) -> None:
        store = _PoolStore({"id": "pool-1"})
        answer, seen = self._next(store)
        self.assertEqual(answer, "studio-hub-b")
        self.assertIsNone(seen["load"])
        self.assertEqual(store.routes, ["studio-hub-b"])

    def test_pool_reroute_with_every_lane_full_queues_in_gateway(self) -> None:
        store = _PoolStore({"id": "pool-2"})
        room = lambda pid, claim=False: (0, 0, 0)
        answer, seen = self._next(store, room)
        self.assertEqual(answer, worker_module.QUEUED_IN_GATEWAY)
        self.assertIs(seen["load"], room)
        self.assertTrue(store.deferred and store.deferred[0]["unsent"])


class PoolAttemptsTest(unittest.TestCase):
    """A pool job gets ONE attempt (owner, 2026-11-18: "fix and update the pool fallback from 3
    retry to one as this reduce waste").

    The pool's chain already tried every member inside one attempt, so replaying it walks every
    provider in it again — the eight `POOL_EXHAUSTED` chat jobs on 1301 today would each have paid
    `story-studio-llm` three times. Non-pool models keep the fleet's three machines.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_the_pool_ceiling_is_one_and_a_model_keeps_three(self) -> None:
        pool_job = {"provider_id": "pool", "model_id": "story-studio-llm"}
        self.assertEqual(1, concurrency.attempts_for(pool_job)["max_attempts"])
        self.assertTrue(concurrency.attempts_for(pool_job).get("pool"))
        self.assertEqual(3, concurrency.attempts_for(
            {"provider_id": "openai-direct", "model_id": "gpt-6-luna"})["max_attempts"])
        self.assertNotIn("pool", concurrency.attempts_for(None))

    def test_the_number_is_a_setting_and_the_row_states_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"retry": {"max_attempts": 3, "pool_max_attempts": 2}}),
                                encoding="utf-8")
            with patch.object(concurrency, "SETTINGS_PATH", settings):
                concurrency.reset_for_tests()
                self.assertEqual(2, concurrency.attempts_for(
                    {"provider_id": "pool", "model_id": "p"})["max_attempts"])
                self.assertEqual(3, concurrency.attempts_for(
                    {"provider_id": "openai-direct", "model_id": "m"})["max_attempts"])
        # Unsaved back to the default: the wording a pool row prints names the number enforced.
        concurrency.reset_for_tests()
        policy = concurrency.attempts_for({"provider_id": "pool", "model_id": "p"})
        self.assertIn("A pool job gets 1 attempt", concurrency.retry_policy_wording(policy))

    def test_one_walk_ends_the_job_even_though_another_machine_is_free(self) -> None:
        store = _Store({"id": "pool-job", "provider_id": "pool", "model_id": "story-studio-llm",
                        "modality": "chat"})
        sites = [_Site("b")]
        with patch.object(worker_module.fleet, "route",
                          lambda *a, **k: (sites.pop(0), "") if sites else (None, "")):
            answer = worker_module._next_machine(
                store, "pool-job", "story-studio-llm", "chat", "pool",
                code="POOL_EXHAUSTED", detail="no member answered")
        self.assertIsNone(answer, "a pool job must not walk a second attempt")
        self.assertEqual(1, len(store.log), "exactly one attempt was spent")
        self.assertEqual([], store.routes, "nothing was re-routed")


class ThreeMachinesTest(unittest.TestCase):
    """The policy table, driven through the real decision function."""

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def _next(self, store, sites, *, code="PROVIDER_FAILED", detail="engine error",
              provider="studio-hub-a"):
        with patch.object(worker_module.fleet, "route",
                          lambda *a, **k: (sites.pop(0), "note") if sites else (None, "")):
            return worker_module._next_machine(
                store, store.job["id"], "z-image-turbo-6b-mlx-4bit", "image",
                provider, code=code, detail=detail)

    def test_attempt_one_on_a_then_two_on_b_then_three_on_c(self) -> None:
        store = _Store({"id": "job-1"})
        self.assertEqual(self._next(store, [_Site("b")], code="MACHINE_BUSY"), "studio-hub-b")
        self.assertEqual(store.log[0]["location"], "studio-hub-a")
        self.assertEqual(self._next(store, [_Site("c")], code="PROVIDER_FAILED",
                                    provider="studio-hub-b"), "studio-hub-c")
        # The third attempt is the last one: the ceiling is three, and the caller must now
        # fail the job properly rather than look for a fourth machine.
        self.assertIsNone(self._next(store, [_Site("d")], provider="studio-hub-c"))
        self.assertEqual([entry["location"] for entry in store.log],
                         ["studio-hub-a", "studio-hub-b", "studio-hub-c"])

    def test_a_machine_already_tried_is_never_offered_again(self) -> None:
        store = _Store({"id": "job-2"})
        self._next(store, [_Site("b")])
        offered: list[set[str]] = []

        def route(model_id, modality, *, exclude_locations=None, **kwargs):
            offered.append(set(exclude_locations or ()))
            return None, ""

        with patch.object(worker_module.fleet, "route", route):
            worker_module._next_machine(store, "job-2", "m", "image", "studio-hub-b",
                                        code="MACHINE_BUSY", detail="busy")
        self.assertEqual(offered, [{"a", "b"}],
                         "both the current machine and every earlier one must be excluded — in "
                         "the bare controller-id spelling the router actually compares")

    def test_no_other_machine_means_wait_rather_than_a_wasted_attempt(self) -> None:
        # Every site offering the model is busy with its own work. The job defers at the machine
        # it is already on; it does not burn one of its three attempts on a site that cannot
        # take it either.
        store = _Store({"id": "job-3"})
        self.assertIsNone(self._next(store, []))
        self.assertEqual(store.log[0]["location"], "studio-hub-a",
                         "the failure is still recorded — it happened")
        self.assertEqual(len(store.log), 1)

    def test_the_ceiling_is_configurable_and_read_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider-concurrency.local.json"
            path.write_text('{"retry": {"max_attempts": 2}}', encoding="utf-8")
            with patch.object(concurrency, "SETTINGS_PATH", path):
                concurrency.reset_for_tests()
                self.assertEqual(concurrency.retry_policy()["max_attempts"], 2)
                store = _Store({"id": "job-4"})
                self.assertEqual(self._next(store, [_Site("b")]), "studio-hub-b")
                self.assertIsNone(self._next(store, [_Site("c")], provider="studio-hub-b"),
                                  "two attempts means the second failure is terminal")
        self.assertEqual(concurrency.retry_policy()["max_attempts"], 3,
                         "the shipped default is the three the owner named")

    def test_the_exclusion_is_in_the_spelling_the_router_compares(self) -> None:
        """**The regression the §9 rehearsal caught, pinned here so it cannot come back.**

        The attempt log stores the *provider* id (`studio-hub-<controller>`) because that names a
        machine to an operator. `fleet.route` compares `exclude_locations` against the bare
        `<controller>` id. Passing one where the other is expected makes the exclusion **inert** —
        which is the exact mismatch A7 exists to close (audit H5), and it came back in this code:
        with three locations enabled, every job stopped at `attempts_used: 2` while publishing
        `attempts_allowed: 3`, because the lottery re-picked a tried site, the `chosen.id in tried`
        guard rejected it, and the worker read that as "nothing left to try".

        A fake route that records what it was asked to exclude is the only way to see this: the
        numbers look right from outside until you check the spelling.
        """
        store = _Store({"id": "job-spelling"})
        asked: list[set[str]] = []

        def route(model_id, modality, *, exclude_locations=None, **kwargs):
            asked.append(set(exclude_locations or ()))
            return None, ""

        with patch.object(worker_module.fleet, "route", route):
            worker_module._next_machine(store, "job-spelling", "m", "image",
                                        "studio-hub-abc", code="MACHINE_BUSY", detail="busy")
            worker_module._next_machine(store, "job-spelling", "m", "image",
                                        "studio-hub-def", code="MACHINE_BUSY", detail="busy")
        self.assertEqual(asked[0], {"abc"},
                         "the router compares bare controller ids, not provider ids")
        self.assertEqual(asked[1], {"abc", "def"})
        self.assertEqual([entry["location"] for entry in store.log],
                         ["studio-hub-abc", "studio-hub-def"],
                         "while the log keeps the readable provider id an operator sees")

    def test_an_already_tried_site_is_never_returned_by_the_router(self) -> None:
        # The other half: if the router hands back a site that is excluded, that is a router bug
        # rather than a reason to declare the job finished — but the worker must not *re-try* it.
        store = _Store({"id": "job-repeat"})
        with patch.object(worker_module.fleet, "route",
                          lambda *a, **k: (_Site("a"), "note")):
            self.assertIsNone(
                worker_module._next_machine(store, "job-repeat", "m", "image", "studio-hub-a",
                                            code="MACHINE_BUSY", detail="busy"),
                "a router that ignores the exclusion must not produce a repeat attempt")

    def test_the_terminal_reason_names_every_machine_and_why_it_failed(self) -> None:
        store = _Store({"id": "job-5"})
        self._next(store, [_Site("b")], code="MACHINE_BUSY", detail="no free machine")
        self._next(store, [_Site("c")], code="PROVIDER_FAILED",
                   detail="ffmpeg not found on PATH", provider="studio-hub-b")
        self._next(store, [], code="PROVIDER_FAILED", detail="[METAL] failed",
                   provider="studio-hub-c")
        sentence = worker_module._tried_everything_detail(store.log, limit=3)
        for expected in ("studio-hub-a", "studio-hub-b", "studio-hub-c",
                         "MACHINE_BUSY", "ffmpeg not found on PATH", "[METAL] failed"):
            self.assertIn(expected, sentence)
        self.assertIn("end of the retry policy", sentence)


class UnknownOutcomeIsNeverRetriedTest(unittest.TestCase):
    """**The discipline the retry policy must sit on top of, not weaken.**

    A job whose upstream accepted the work and then went silent might be rendering and billing
    right now. Re-running it on another machine is the one thing that could produce two
    artifacts for one request, so the policy must not touch it.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_a_dispatched_job_that_says_nothing_about_acceptance_holds(self) -> None:
        store = _Store({"id": "job-6", "dispatch_started_at": 1.0, "upstream_job_id": "abc"})
        exc = BackendError("studio_hub batch went silent", code="PROVIDER_FAILED",
                           upstream_accepted=None, upstream_settled=False)
        with patch.object(worker_module, "execute_job") as run:
            run.side_effect = exc
            with patch.object(worker_module.fleet, "route") as route:
                worker_module.Worker(store, interval_s=10.0)._run(dict(store.job))
        self.assertFalse(route.called, "an unknown outcome must not look for another machine")
        self.assertEqual(store.log, [])

    def test_the_unknown_outcome_check_is_the_retry_gate(self) -> None:
        # Stated as one assertion a reader can hold onto: `_next_machine` is only reached when
        # the adapter proved nothing is still running.
        exc = BackendError("boom", code="PROVIDER_FAILED", upstream_accepted=None,
                           upstream_settled=False)
        current = {"dispatch_started_at": 1.0}
        definite_refusal = False
        unknown = bool(current.get("upstream_job_id")) or exc.upstream_accepted is True
        if not definite_refusal:
            unknown = unknown or (current.get("dispatch_started_at") is not None
                                  and exc.upstream_accepted is None)
        self.assertTrue(unknown, "this is the condition the caller tests before re-routing")


class EndToEndThroughThreeSitesTest(unittest.TestCase):
    """The job's own acceptance: two sites fail (one busy, one engine error), the third runs it.

    Driven through the real `execute_job` and the real retry decision, with only the fleet's
    routing and the adapters replaced — so the attempt record, the re-route and the terminal
    failure are the production code paths.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    class _Outcome:
        """One fake site: either raises, or returns an artifact."""

        def __init__(self, behaviour: str) -> None:
            self.behaviour = behaviour

        def generate(self, request):
            from gateway.adapters import BackendResult

            if self.behaviour == "busy":
                raise BackendError("no free machine", code="MACHINE_BUSY", status=409,
                                   upstream_accepted=False, upstream_settled=True)
            if self.behaviour == "memory":
                raise BackendError("INSUFFICIENT_MEMORY: Mac mini 1101 needs about 3.2 GB free and has "
                                   "about 3.0 GB free (it refused 3 times).",
                                   code="INSUFFICIENT_MEMORY", retryable=True,
                                   upstream_accepted=False, upstream_settled=True)
            if self.behaviour == "storage_blocked":
                # The Hub's door, word for word from Production 2026-11-16 (studio-1300).
                raise BackendError("the location cannot serve this model right now (OUTPUT_STORAGE_BLOCKED): "
                                   "managed output storage has insufficient free space; empty the Trash "
                                   "Folder to free space", code="LOCATION_NOT_READY", status=507,
                                   retryable=True, upstream_accepted=False)
            if self.behaviour == "refused_poll":
                # Production 2026-11-16, studio-1200: accepted, then refused mid-poll.
                raise BackendError("lost contact with the location while polling batch ab0a235660 (item 0) "
                                   "for 60s: [Errno 61] Connection refused",
                                   code="PROVIDER_SUBMISSION_UNCERTAIN", retryable=False,
                                   upstream_accepted=True,
                                   recovery={"batch_id": "ab0a235660", "item_index": 0,
                                             "location": "http://100.114.4.52:47873", "lost_contact": True})
            if self.behaviour == "refused_submit":
                raise BackendError("could not reach the Hub at http://100.114.4.52:47873 (ConnectError)",
                                   code="LOCATION_UNREACHABLE", retryable=True, upstream_accepted=False)
            if self.behaviour == "engine":
                raise BackendError("ffmpeg not found on PATH", code="PROVIDER_FAILED",
                                   upstream_accepted=False, upstream_settled=True)
            if self.behaviour == "request_shape":
                # The Hub's own words, Production 2026-11-19: a job sent `width/height: 512`.
                raise BackendError("hub submit HTTP 422: Choose a qualified 1K aspect ratio",
                                   code="PROVIDER_FAILED", status=422, retryable=False,
                                   upstream_accepted=False)
            return BackendResult(content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01", media_type="image/png")

    def _run_single_site(self, behaviour: str, *, params=None, extra_sites=False):
        return self._run([behaviour], sites=["a", "b"] if extra_sites else ["a"], params=params)

    def _run(self, behaviours: list[str], *, sites=None, params=None, before=()):
        store = _Store({
            "id": "job-e2e", "modality": "image", "model_id": "z-image-turbo-6b-mlx-4bit",
            "provider_id": "studio-hub-a", "params": json.dumps(params or {}), "state": "running",
            "attempt_count": 1,
        })
        for location, code in before:   # an attempt log the job already carries
            store.record_attempt("job-e2e", location_id=location, code=code, detail="earlier")
        store.succeed = lambda job_id, **kwargs: store.succeeded.append(
            {"job_id": job_id, **kwargs})
        store.succeeded = []
        order = list(behaviours)
        sites = [_Site(name) for name in (sites or ["a", "b", "c"])]
        dispatch: list[str] = []

        def adapter_for(definition):
            dispatch.append(definition.id)
            return self._Outcome(order.pop(0))

        def route(model_id, modality, *, exclude_locations=None, **kwargs):
            # **The fake router honours the exclusion, in the spelling the real one compares.**
            # This is the part of the harness that hid the id-spelling regression: a fake that
            # ignored `exclude_locations` (or compared the wrong spelling) would hand back the
            # site that just failed while the real router would have moved on, so the test would
            # pass against code whose exclusion was inert.
            excluded = set(exclude_locations or ())
            for site in sites:
                if (worker_module.fleet.controller_id_from(site.id) or site.id) not in excluded:
                    return site, "note"
            return None, ""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(worker_module, "adapter_for", adapter_for), \
                    patch.object(worker_module.fleet, "route", route), \
                    patch.object(worker_module.fleet, "_location_takes_customer_jobs",
                                 lambda location_id: True), \
                    patch.object(worker_module.registry, "definition",
                                 lambda provider_id: type("D", (), {
                                     "id": provider_id, "base_url": "", "kind": "studio_hub",
                                     "display_name": provider_id})()), \
                    patch.object(worker_module.registry.credentials, "is_enabled",
                                 lambda provider_id: True), \
                    patch.object(worker_module.registry.models, "enabled_models",
                                 lambda modality: [(type("D", (), {"id": "studio-hub-a", "kind": "studio_hub"})(),
                                                    {"model_id": "z-image-turbo-6b-mlx-4bit",
                                                     "provider_id": "studio-hub-a"})]), \
                    patch.object(worker_module.registry.credentials, "resolved",
                                 lambda provider_id: {}), \
                    patch.object(worker_module, "ARTIFACT_DIR", tmp), \
                    patch.object(worker_module, "store", store, create=True):
                worker_module.execute_job(dict(store.job), store)
        return store, dispatch

    def test_two_failures_then_the_third_machine_succeeds(self) -> None:
        store, dispatch = self._run(["busy", "engine", "ok"])
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-b", "studio-hub-c"])
        self.assertEqual(store.failed, [], "the third machine ran it")
        self.assertEqual([entry["location"] for entry in store.log],
                         ["studio-hub-a", "studio-hub-b"])
        self.assertEqual(store.routes, ["studio-hub-b", "studio-hub-c"])

    def test_a_422_request_refusal_is_terminal_at_the_first_site(self) -> None:
        # Seen live 2026-11-19: a job sent `width/height: 512` was refused by three locations in
        # turn, each saying "Choose a qualified 1K aspect ratio", before failing. The request was
        # wrong, not the machine, so the first refusal is the whole answer.
        store, dispatch = self._run(["request_shape", "ok"])
        self.assertEqual(dispatch, ["studio-hub-a"],
                         "a request-shape refusal must not be spent on another location")
        self.assertEqual(store.routes, [])
        self.assertEqual([f["code"] for f in store.failed], ["PROVIDER_FAILED"])
        self.assertEqual(store.holds, [], "nothing was sent, so there is no outcome to reconcile")

    def test_a_memory_handback_moves_the_job_to_another_machine_excluding_this_one(self) -> None:
        store, dispatch = self._run(["memory", "ok"])
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-b"],
                         "the job goes to another location, never back to the one that handed it back")
        self.assertEqual(store.failed, [], "a hand-back is not a final failure")
        self.assertEqual(store.log[0]["code"], "INSUFFICIENT_MEMORY")
        self.assertEqual(store.deferred, [], "and it does not wait on the Mac that refused it")

    def test_a_memory_handback_with_nowhere_else_waits_for_a_free_mac_instead_of_failing(self) -> None:
        store, dispatch = self._run_single_site("memory")
        self.assertEqual(dispatch, ["studio-hub-a"])
        self.assertEqual(store.failed, [], "a hand-back is not 'run it again'")
        self.assertEqual(len(store.deferred), 1)
        self.assertTrue(store.deferred[0]["new_round"])
        self.assertIn("waiting for a free Mac", store.deferred[0]["reason"])
        self.assertIn("needs about 3.2 GB free", store.deferred[0]["reason"])
        self.assertEqual(store.log[0]["code"], "INSUFFICIENT_MEMORY", "still on the attempt record")

    def test_a_memory_handback_wait_ends_with_the_ordinary_failure_once_the_queue_budget_is_spent(self) -> None:
        store = _Store({"id": "job-e2e", "modality": "image", "model_id": "m", "provider_id": "studio-hub-a",
                        "params": "{}", "state": "running", "attempt_count": 10_000})
        error = BackendError("INSUFFICIENT_MEMORY: no room", code="INSUFFICIENT_MEMORY", retryable=True,
                             upstream_accepted=False, upstream_settled=True)

        class _Memory:
            def generate(self, request):
                raise error

        definition = lambda pid: type("D", (), {"id": pid, "base_url": "", "kind": "studio_hub",
                                                "display_name": pid})()
        with patch.object(worker_module, "adapter_for", lambda _d: _Memory()), \
                patch.object(worker_module.fleet, "route", lambda *a, **k: (None, "")), \
                patch.object(worker_module.fleet, "_location_takes_customer_jobs", lambda _l: True), \
                patch.object(worker_module.registry, "definition", definition), \
                patch.object(worker_module.registry.credentials, "is_enabled", lambda _p: True), \
                patch.object(worker_module.registry.models, "enabled_models",
                             lambda _m: [(type("D", (), {"id": "studio-hub-a", "kind": "studio_hub"})(),
                                          {"model_id": "m", "provider_id": "studio-hub-a"})]), \
                patch.object(worker_module.registry.credentials, "resolved", lambda _p: {}):
            worker_module.execute_job(dict(store.job), store)
        self.assertEqual(store.deferred, [])
        self.assertEqual([f["code"] for f in store.failed], ["INSUFFICIENT_MEMORY"])

    def test_a_pinned_job_is_not_re_routed_and_fails_at_once(self) -> None:
        store, dispatch = self._run_single_site(
            "memory", params={"voice_id": "studio-hub-a::raw-voice"}, extra_sites=True)
        self.assertEqual(dispatch, ["studio-hub-a"], "a pinned job never moves")
        self.assertEqual(store.deferred, [])
        self.assertEqual([f["code"] for f in store.failed], ["INSUFFICIENT_MEMORY"])

    def test_a_job_held_for_a_storage_blocked_site_moves_on_instead_of_waiting(self) -> None:
        """Production 2026-11-16, job 7dd410fb's shape: two counted attempts elsewhere, then fifteen
        refusals from studio-1300's full disk while 23 Macs were eligible. Under 0.2.260 the
        refusals spent the third attempt, so the job could only wait on the same full Mac."""
        before = [("studio-hub-b", "PROVIDER_FAILED"), ("studio-hub-c", "PROVIDER_SUBMISSION_UNCERTAIN")]
        before += [("studio-hub-a", "LOCATION_NOT_READY")] * 15
        self.addCleanup(worker_module.fleet._STORAGE_COOLDOWN.clear)
        store, dispatch = self._run(["storage_blocked", "ok"], sites=["a", "b", "c", "d"], before=before)
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-d"],
                         "the refusal moves the job to an untried site; it does not wait on the full one")
        self.assertEqual((store.failed, store.deferred), ([], []))
        self.assertTrue(worker_module.fleet.storage_cooled(
            worker_module.fleet.controller_id_from("studio-hub-a") or "studio-hub-a",
            "z-image-turbo-6b-mlx-4bit"), "and the full site rests, so the next asset is not sent there")

    def test_a_poll_refused_mid_job_moves_the_job_and_rests_the_site(self) -> None:
        """Job 8eb32470's shape (Production 2026-11-16): refusals that accepted nothing, one lost
        contact at 1200, then 1200 refused the poll again. Under 0.2.260 the refusals had spent the
        budget, so the job became a hold at the dead site; now it moves, and 1200 takes no new work."""
        before = [("studio-hub-x", "OUTPUT_STORAGE_FULL"), ("studio-hub-a", "PROVIDER_SUBMISSION_UNCERTAIN"),
                  ("studio-hub-y", "INSUFFICIENT_MEMORY"), ("studio-hub-z", "LOCATION_NOT_READY")]
        self.addCleanup(worker_module.fleet._REFUSED_AT.clear)
        with patch.object(worker_module, "_hub_batch_answer", lambda *a, **k: None), \
                patch.object(worker_module, "_superseded_stops_allowed", lambda: False):
            store, dispatch = self._run(["refused_poll", "ok"], sites=["a", "b"], before=before)
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-b"], "it moves to a live site")
        self.assertEqual((store.holds, store.failed), ([], []), "not a hold at a site that refuses")
        self.assertTrue(worker_module.fleet.is_refusing_provider("studio-hub-a"),
                        "and the refusing site is out of routing until it answers")

    def test_a_submit_that_could_not_connect_costs_no_attempt(self) -> None:
        """Nothing left this Mac, so it is not an unknown outcome and not one of the three tries."""
        before = [("studio-hub-x", "PROVIDER_FAILED"), ("studio-hub-y", "PROVIDER_FAILED")]
        self.addCleanup(worker_module.fleet._REFUSED_AT.clear)
        store, dispatch = self._run(["refused_submit", "ok"], sites=["a", "b"], before=before)
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-b"],
                         "two real failures plus a refused connection still leave the third try")
        self.assertEqual((store.holds, store.failed), ([], []))
        self.assertTrue(worker_module.fleet.is_refusing_provider("studio-hub-a"))

    def test_three_failures_produce_one_proper_failure_naming_all_three(self) -> None:
        store, dispatch = self._run(["busy", "engine", "engine"])
        self.assertEqual(dispatch, ["studio-hub-a", "studio-hub-b", "studio-hub-c"])
        self.assertEqual(len(store.failed), 1, "one terminal failure, not three")
        reason = store.failed[0]["detail"]
        for expected in ("studio-hub-a", "studio-hub-b", "studio-hub-c",
                         "MACHINE_BUSY", "ffmpeg not found on PATH"):
            self.assertIn(expected, reason)
        self.assertIn("end of the retry policy", reason)
        self.assertEqual(store.holds, [], "a known failure is never a reconciliation hold")


class UnsentRefusalsDoNotSpendTheBudgetTest(unittest.TestCase):
    """A machine that refused the work did not fail it.

    Measured 2026-11-16: one voice job spent all three attempts on a Mac whose Trash Folder was
    full, a Mac with 1.5 GB free against a 3.2 GB need, and a third that then went silent — so
    the ceiling, not the work, ended it. A refusal that accepted nothing (`upstream_accepted is
    False`) is a fact about that machine and must not spend an attempt. The exclusion set still
    grows with every one, so a job cannot bounce between the same machines.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def _next(self, store, sites, *, code="INSUFFICIENT_MEMORY", provider="studio-hub-c"):
        with patch.object(worker_module.fleet, "route",
                          lambda *a, **k: (sites.pop(0), "note") if sites else (None, "")):
            return worker_module._next_machine(store, store.job["id"], "voice-model", "tts",
                                               provider, code=code, detail="refused")

    def test_three_unfit_machines_leave_the_three_for_a_fourth(self) -> None:
        store = _Store({"id": "job-unfit"})
        for site, code in (("a", "INSUFFICIENT_MEMORY"), ("b", "OUTPUT_STORAGE_FULL")):
            store.record_attempt("job-unfit", location_id=f"studio-hub-{site}", code=code, detail="x")
        self.assertEqual(self._next(store, [_Site("d")], provider="studio-hub-c"), "studio-hub-d",
                         "want of memory, of disk or an install is not a failed attempt")
        self.assertEqual([entry["location"] for entry in store.log],
                         ["studio-hub-a", "studio-hub-b", "studio-hub-c"],
                         "every refusal is still recorded — it happened")

    def test_three_real_failures_still_end_at_the_ceiling(self) -> None:
        store = _Store({"id": "job-real"})
        for site in ("a", "b", "c"):
            store.record_attempt("job-real", location_id=f"studio-hub-{site}",
                                 code="PROVIDER_FAILED", detail="engine crashed")
        self.assertIsNone(self._next(store, [_Site("d")], code="PROVIDER_FAILED"))


class EveryMachineUnfitStillEndsTest(unittest.TestCase):
    """The risk in not counting unfit refusals: the job must still end, never cycle.

    With nothing counting, the waiting ladder (`_defer_if_transient`) is what ends the job — the
    same ladder a busy fleet takes — and it must say why. The ladder's budget is shortened here
    (the shipped one is 240 waits) so the proof stays fast; the rule is unchanged.
    """

    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)

    def test_the_job_ends_on_the_waiting_ladder_with_a_named_reason(self) -> None:
        store = _Store({"id": "job-unfit-all", "modality": "tts", "model_id": "voice-model",
                        "provider_id": "studio-hub-a", "params": "{}", "state": "running",
                        "attempt_count": 0})

        def defer(job_id, *, delay_s, reason, new_round=False, unsent=False):
            store.deferred.append({"reason": reason, "new_round": new_round})
            # The dispatcher's own count grows with every wait, exactly as `store.defer` does.
            store.job["attempt_count"] = int(store.job.get("attempt_count") or 0) + 1
            return True

        store.defer = defer
        sites = [_Site(name) for name in ("a", "b", "c")]
        error = BackendError(
            "INSUFFICIENT_MEMORY: Mac mini 1101 needs about 3.2 GB free and has about 1.5 GB free "
            "(it refused 3 times).", code="INSUFFICIENT_MEMORY", retryable=True,
            upstream_accepted=False, upstream_settled=True)

        class _Unfit:
            def generate(self, request):
                raise error

        def route(model_id, modality, *, exclude_locations=None, **kwargs):
            excluded = set(exclude_locations or ())
            for site in sites:
                if (worker_module.fleet.controller_id_from(site.id) or site.id) not in excluded:
                    return site, "note"
            return None, ""

        definition = lambda pid: type("D", (), {"id": pid, "base_url": "", "kind": "studio_hub",
                                                "display_name": pid})()
        budget = dict(concurrency.queue_wait_budget(), max_deferrals=2)
        passes = 0
        with patch.object(worker_module, "adapter_for", lambda _d: _Unfit()), \
                patch.object(worker_module.fleet, "route", route), \
                patch.object(worker_module.fleet, "_location_takes_customer_jobs", lambda _l: True), \
                patch.object(concurrency, "queue_wait_budget", lambda: budget), \
                patch.object(worker_module.registry, "definition", definition), \
                patch.object(worker_module.registry.credentials, "is_enabled", lambda _p: True), \
                patch.object(worker_module.registry.models, "enabled_models",
                             lambda _m: [(type("D", (), {"id": "studio-hub-a", "kind": "studio_hub"})(),
                                          {"model_id": "voice-model",
                                           "provider_id": "studio-hub-a"})]), \
                patch.object(worker_module.registry.credentials, "resolved", lambda _p: {}):
            while not store.failed and passes < 12:
                worker_module.execute_job(dict(store.job), store)
                passes += 1
        self.assertEqual(len(store.failed), 1, "it must end — waiting is bounded, a cycle is not")
        self.assertLess(passes, 12)
        self.assertEqual({entry["location"] for entry in store.log},
                         {"studio-hub-a", "studio-hub-b", "studio-hub-c"},
                         "each unfit machine was tried once, not in a loop")
        self.assertGreaterEqual(len(store.deferred), 2, "the end is the ladder's, and it was used")
        reason = store.failed[0]["detail"]
        self.assertIn("none could take it", reason)
        self.assertIn("INSUFFICIENT_MEMORY", reason)
        self.assertIn("needs about 3.2 GB free", reason)
        self.assertIn("Nothing was charged", reason)
        for machine in ("studio-hub-a", "studio-hub-b", "studio-hub-c"):
            self.assertIn(machine, reason, "the ending names every machine that refused it")


if __name__ == "__main__":
    unittest.main()
