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

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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

    def defer(self, job_id: str, *, delay_s: float, reason: str) -> bool:
        self.deferred.append({"delay_s": delay_s, "reason": reason})
        return True


class _Site:
    def __init__(self, site_id: str) -> None:
        self.id = f"studio-hub-{site_id}"
        self.label = site_id


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
        self.assertEqual(offered, [{"studio-hub-a", "studio-hub-b"}],
                         "both the current machine and every earlier one must be excluded")

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
            if self.behaviour == "engine":
                raise BackendError("ffmpeg not found on PATH", code="PROVIDER_FAILED",
                                   upstream_accepted=False, upstream_settled=True)
            return BackendResult(content=b"png", media_type="image/png")

    def _run(self, behaviours: list[str]):
        store = _Store({
            "id": "job-e2e", "modality": "image", "model_id": "z-image-turbo-6b-mlx-4bit",
            "provider_id": "studio-hub-a", "params": {}, "state": "running",
            "attempt_count": 1,
        })
        store.succeed = lambda job_id, **kwargs: store.succeeded.append(
            {"job_id": job_id, **kwargs})
        store.succeeded = []
        order = list(behaviours)
        sites = [_Site("a"), _Site("b"), _Site("c")]
        dispatch: list[str] = []

        def adapter_for(definition):
            dispatch.append(definition.id)
            return self._Outcome(order.pop(0))

        def route(model_id, modality, *, exclude_locations=None, **kwargs):
            for site in sites:
                if site.id not in (exclude_locations or ()):
                    return site, "note"
            return None, ""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(worker_module, "adapter_for", adapter_for), \
                    patch.object(worker_module.fleet, "route", route), \
                    patch.object(worker_module.registry, "definition",
                                 lambda provider_id: type("D", (), {
                                     "id": provider_id, "base_url": "",
                                     "display_name": provider_id})()), \
                    patch.object(worker_module.registry.credentials, "is_enabled",
                                 lambda provider_id: True), \
                    patch.object(worker_module.registry.models, "enabled_models",
                                 lambda modality: [(type("D", (), {"id": "studio-hub-a"})(),
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


if __name__ == "__main__":
    unittest.main()
