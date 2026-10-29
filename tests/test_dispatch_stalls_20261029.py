#!/usr/bin/env python3
"""Two production dispatch stalls, diagnosed on the live Gateway on 2026-10-29.

**1. The background gate was per site, not per shape.** `Worker._background_room` returned 0 for a
whole site when *any* normal-priority waiting shape had an idle Mac there. Seven normal Breeze-TTS
jobs were bound to a site that is often unreachable and does not publish Breeze; every image site
also lists Breeze, so all six sites refused **132 background image jobs for 3.5 hours** while their
image Macs sat idle. And a job bound to a location that does not publish its model kept replaying
"hub submit failed: timed out" (6–29 attempts) because nothing asked that question before admitting
it again.

**2. A pool member wait had no deadline and no reason.** `adapters.pool._acquire_member` waited for
ever while `magpie`'s cloud budget is the default 1, and one member call could run for the Hub's
four-hour chat ceiling. The job then sat `running` with no upstream id and nothing published.

**3. Generic liveness.** A `running` job with no `upstream_job_id` and no progress used to be
indistinguishable from a healthy one. It now says what it is waiting for, and past the lane's own
call ceiling it fails retryably instead of staying silent.

The tests start from the rows a live Gateway actually holds (accepted normal TTS rows plus accepted
background image rows) and assert that a dispatch pass settles them **without any cancel, delete or
state rewrite**.
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env(prefix="gateway-dispatch-stalls-")  # never bind to the owner's real data

import httpx  # noqa: E402

from gateway import concurrency, fleet  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import openai_compat  # noqa: E402
from gateway.adapters.base import (  # noqa: E402
    CHAT_DEADLINE_CEILING_S, BackendError, BackendRequest, BackendResult,
)
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402
from gateway.worker import Worker  # noqa: E402

IMAGE_MODEL = "flux-klein"
TTS_MODEL = "breeze-tts"
OTHER_MODEL = "qwen-image"

#: Which lane each synthetic model belongs to. Only the tests' own vocabulary.
_LANE = {IMAGE_MODEL: "image", OTHER_MODEL: "image", TTS_MODEL: "tts"}
_OPS = {"image": "image.text_to_image", "tts": "voice.tts"}


def _pid(location_id: str) -> str:
    return f"{fleet.PROVIDER_PREFIX}{location_id}"


def _site(location_id: str, *, macs: int, supply: dict[str, dict[str, int]] | None = None,
          reachable: bool = True, models: list[str] | None = None,
          publish_supply: bool = True) -> dict:
    """One location: `supply` is its `model_supply` (or `{}` for a Hub that states none)."""
    supply = supply if supply is not None else {IMAGE_MODEL: {"eligible": macs, "available": macs}}
    row = {
        "id": location_id, "provider_id": _pid(location_id), "label": location_id,
        "reachable": reachable, "machines": [], "machine_summary": {"reachable": macs},
        "running_jobs": [],
        "capacity": {"queue_depth": 0, "by_operation": {
            op: {"eligible_physical_machine_slots_total": macs,
                 "available_physical_machine_slots": macs} for op in _OPS.values()}},
    }
    if publish_supply:
        row["model_supply"] = supply
    row["_models"] = models if models is not None else list(supply)
    return row


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = Path(tmp.name) / "provider-concurrency.local.json"
        settings.write_text('{"global": 50}', encoding="utf-8")
        self.sites: list[dict] = []

        def enabled(modality):
            return [(types.SimpleNamespace(id=row["provider_id"]),
                     {"model_id": model, "modality": modality, "raw": {}})
                    for row in self.sites for model in row.get("_models") or []
                    if _LANE.get(model) == modality]

        for patcher in (patch.object(concurrency, "SETTINGS_PATH", settings),
                        patch.object(fleet, "snapshot", lambda *a, **k: {"locations": self.sites}),
                        patch.object(registry.models, "enabled_models", enabled)):
            patcher.start()
            self.addCleanup(patcher.stop)
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self._n = 0

    def job(self, model: str = IMAGE_MODEL, *, site: str = "img1", background: bool = False,
            modality: str | None = None) -> str:
        self._n += 1
        row, _ = self.store.submit(
            modality=modality or _LANE.get(model, "image"), model_id=model, params={},
            idempotency_key=f"k{self._n}", provider_id=_pid(site),
            priority="background" if background else None)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET next_action_at = 0, created_at = ? WHERE id = ?",
                (time.time() - 60 + self._n, row["id"]))
            self.store._conn.commit()
        return row["id"]


# ── 1a. A location that does not publish its model ───────────────────────
class PublishesModelTest(unittest.TestCase):
    """The three answers, which are never two (§6: unstated must not read as "none")."""

    def test_published_eligible_macs_is_a_yes(self) -> None:
        row = {"model_supply": {IMAGE_MODEL: {"eligible": 2, "available": 0}}}
        self.assertTrue(fleet.publishes_model(row, IMAGE_MODEL))

    def test_a_published_reduction_that_leaves_the_model_out_is_a_no(self) -> None:
        row = {"model_supply": {OTHER_MODEL: {"eligible": 2, "available": 2}}}
        self.assertFalse(fleet.publishes_model(row, IMAGE_MODEL))

    def test_a_model_with_no_eligible_mac_is_a_no(self) -> None:
        row = {"model_supply": {IMAGE_MODEL: {"eligible": 0, "available": 0}}}
        self.assertFalse(fleet.publishes_model(row, IMAGE_MODEL))

    def test_a_hub_that_states_no_supply_is_unknown_not_no(self) -> None:
        self.assertIsNone(fleet.publishes_model({}, IMAGE_MODEL))
        self.assertIsNone(fleet.publishes_model({"model_supply": {}}, IMAGE_MODEL))

    def test_a_mac_that_lists_the_model_rescues_one_the_reduction_left_out(self) -> None:
        # Companion models are not Hub rows, so they can be absent from `model_supply` and still run.
        row = {"model_supply": {OTHER_MODEL: {"eligible": 1, "available": 1}},
               "workers": [{"models": ["companion"]}]}
        self.assertTrue(fleet.publishes_model(row, "companion"))
        unfit = {"model_supply": {OTHER_MODEL: {"eligible": 1, "available": 1}},
                 "local_models": [{"model_id": "companion", "memory_eligible": False}]}
        self.assertFalse(fleet.publishes_model(unfit, "companion"))


class PublicationAdmissionTest(_Base):
    def test_a_site_that_does_not_publish_the_model_gets_no_room_for_it(self) -> None:
        self.sites = [_site("terranash", macs=1, supply={OTHER_MODEL: {"eligible": 1, "available": 1}},
                            models=[OTHER_MODEL])]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job = self.store.get(self.job(IMAGE_MODEL, site="terranash"))
        self.assertEqual(0, worker._job_room(job, _pid("terranash")))
        self.assertFalse(worker._admit(job))

    def test_an_unreachable_site_gets_no_room(self) -> None:
        self.sites = [_site("img1", macs=2, reachable=False)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job = self.store.get(self.job(IMAGE_MODEL))
        self.assertEqual(0, worker._job_room(job, _pid("img1")))

    def test_a_site_that_publishes_the_model_keeps_its_room(self) -> None:
        self.sites = [_site("img1", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job = self.store.get(self.job(IMAGE_MODEL))
        self.assertGreater(worker._job_room(job, _pid("img1")), 0)
        self.assertTrue(worker._admit(job))

    def test_a_pool_job_is_judged_by_the_pool_s_own_member_list(self) -> None:
        """A pool job's model is the member it is bound to, while the pool picks the next site
        through *any* member — so the publication question must not be asked about that one
        member, or a site that answers through another would be refused."""
        self.sites = [_site("img1", macs=2, supply={OTHER_MODEL: {"eligible": 2, "available": 2}},
                            models=[OTHER_MODEL])]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        self._n += 1
        row, _ = self.store.submit(
            modality="image", model_id=IMAGE_MODEL, params={"_pool_id": "thumbs"},
            idempotency_key=f"pool{self._n}", provider_id=_pid("img1"))
        self.assertGreater(worker._job_room(self.store.get(row["id"]), _pid("img1")), 0)

    def test_a_hub_that_states_no_supply_keeps_its_job(self) -> None:
        # Unknown is not "none": an older Hub must not have every job rebound off it.
        self.sites = [_site("img1", macs=2, publish_supply=False, models=[IMAGE_MODEL])]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job = self.store.get(self.job(IMAGE_MODEL))
        self.assertGreater(worker._job_room(job, _pid("img1")), 0)

    def test_a_job_bound_to_a_site_that_does_not_publish_its_model_is_rebound(self) -> None:
        self.sites = [
            _site("terranash", macs=1, supply={OTHER_MODEL: {"eligible": 1, "available": 1}},
                  models=[OTHER_MODEL]),
            _site("img1", macs=2),
        ]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL, site="terranash")
        self.assertTrue(worker._rebind(self.store.get(job_id)))
        self.assertEqual(_pid("img1"), self.store.get(job_id)["provider_id"])

    def test_a_site_that_publishes_the_model_and_has_room_keeps_its_job(self) -> None:
        self.sites = [_site("img1", macs=2), _site("img2", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL, site="img1")
        self.assertFalse(worker._rebind(self.store.get(job_id)))
        self.assertEqual(_pid("img1"), self.store.get(job_id)["provider_id"])


# ── 1b. The live rows, settled by one dispatch pass ──────────────────────
class StrandedRowsTest(_Base):
    def test_the_live_rows_admit_and_rebind_with_no_cancel_delete_or_rewrite(self) -> None:
        """The rows a live Gateway holds: stranded normal TTS at a site that cannot run them, and
        accepted background image rows elsewhere. One pass must settle both, untouched."""
        self.sites = [
            # Terranash: often unreachable, and its published supply leaves Breeze-TTS out.
            _site("terranash", macs=1, supply={OTHER_MODEL: {"eligible": 1, "available": 1}},
                  models=[OTHER_MODEL]),
            _site("img1", macs=6, supply={IMAGE_MODEL: {"eligible": 6, "available": 6}},
                  models=[IMAGE_MODEL]),
            _site("tts1", macs=3, supply={TTS_MODEL: {"eligible": 3, "available": 3}},
                  models=[TTS_MODEL]),
        ]
        stranded = [self.job(TTS_MODEL, site="terranash") for _ in range(3)]
        background = [self.job(IMAGE_MODEL, site="img1", background=True) for _ in range(6)]
        rows_before = {job_id: self.store.get(job_id) for job_id in stranded + background}

        started: list[tuple[str, str]] = []

        def execute(job, store):
            started.append((job["id"], str(job.get("provider_id") or "")))
            store.succeed(job["id"], result={})

        with patch.object(worker_module, "execute_job", execute):
            worker = Worker(self.store, interval_s=0.01, pool_size=0)
            worker.start()
            try:
                deadline = time.time() + 20
                while time.time() < deadline:
                    if all((self.store.get(job_id) or {}).get("state") == "succeeded"
                           for job_id in stranded + background):
                        break
                    time.sleep(0.02)
            finally:
                worker.wait_idle(10)
                worker.stop()

        rows = {job_id: self.store.get(job_id) for job_id in stranded + background}
        self.assertTrue(all(row["state"] == "succeeded" for row in rows.values()),
                        {job_id: row["state"] for job_id, row in rows.items()})
        # The stranded narration moved to the site that publishes Breeze — and nothing else moved.
        for job_id in stranded:
            self.assertEqual(_pid("tts1"), rows[job_id]["provider_id"])
        for job_id in background:
            self.assertEqual(_pid("img1"), rows[job_id]["provider_id"])
        # No cancel, no delete, no state rewrite: the same rows, ids and request keys.
        for job_id, before in rows_before.items():
            self.assertIsNone(rows[job_id]["cancel_requested_at"])
            self.assertIsNone(rows[job_id]["cancelled_at"] if "cancelled_at" in rows[job_id] else None)
            self.assertEqual(before["idempotency_key"], rows[job_id]["idempotency_key"])
            self.assertEqual(before["created_at"], rows[job_id]["created_at"])
        self.assertEqual(set(job_id for job_id, _ in started), set(stranded + background))
        self.assertNotIn(_pid("terranash"), {where for _, where in started})


# ── 2. A pool member wait is bounded and says why ────────────────────────
class PoolMemberWaitTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        from gateway.adapters import pool as pool_adapter

        self.pool = pool_adapter
        self.budget = 0.25  # seconds; each test states what it needs
        for patcher in (
            patch.object(pool_adapter, "WAIT_STEP_S", 0.01),
            patch.object(pool_adapter, "_member_budget", lambda: {
                "member_wait_seconds": self.budget, "chat_member_deadline_seconds": 600.0,
                "source": concurrency.SOURCE_CURATED, "path": "test"}),
            patch.dict(pool_adapter._member_calls, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _definition(self):
        from gateway.providers.definitions import ProviderDefinition

        return ProviderDefinition(id="magpie", display_name="Magpie", category="language",
                                  kind="openai_compatible", base_url="http://example.test",
                                  modalities=("chat",), discovery="static")

    def _request(self, on_wait=None, name: str = "mix") -> BackendRequest:
        return BackendRequest(provider_id="pool", model_id=name, modality="chat",
                              params={"messages": []}, values={}, base_url="",
                              on_wait=on_wait)

    def test_a_member_with_no_free_slot_gives_up_retryably_with_a_published_reason(self) -> None:
        self.pool._member_calls["magpie"] = 1  # the cloud budget is 1 by default
        waits: list[object] = []
        began = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            self.pool._acquire_member(self._definition(), self._request(waits.append))
        self.assertEqual("POOL_MEMBER_BUSY", caught.exception.code)
        self.assertTrue(caught.exception.retryable)
        self.assertIs(False, caught.exception.upstream_accepted, "nothing was sent")
        self.assertEqual({"magpie": 1}, self.pool._member_calls, "no slot was taken")
        self.assertLess(time.monotonic() - began, 5.0)
        self.assertIn("waiting for a Magpie slot", waits[0]["reason"])
        self.assertIsNone(waits[-1], "the job is told the wait is over")

    def test_the_published_wait_since_is_wall_clock(self) -> None:
        """`waiting.since` is rendered as a date beside the job (`api._iso`), so it must be one.

        A monotonic reading is seconds since an arbitrary boot: the page would print a time in
        1970 or drop the field. The worker's own waiting reason uses wall clock for the same
        reason (review 2026-10-29f, finding 3).
        """
        self.pool._member_calls["magpie"] = 1
        waits: list[object] = []
        began = time.time()
        with self.assertRaises(BackendError):
            self.pool._acquire_member(self._definition(), self._request(waits.append))
        since = waits[0]["since"]
        self.assertIsInstance(since, float)
        self.assertLess(abs(since - began), 60.0,
                        f"a monotonic reading is not a date: {since!r}")

    def test_the_wait_takes_the_slot_as_soon_as_it_frees(self) -> None:
        self.budget = 30.0  # a busy machine may delay the timer; this must not become the test
        self.pool._member_calls["magpie"] = 1
        import threading

        threading.Timer(0.05, self.pool._release_member, args=("magpie",)).start()
        waits: list[object] = []
        self.pool._acquire_member(self._definition(), self._request(waits.append))
        self.assertEqual({"magpie": 1}, self.pool._member_calls)
        self.assertIsNone(waits[-1])

    def test_a_busy_member_does_not_hold_the_pool(self) -> None:
        from gateway.adapters.base import BackendResult
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition

        answers = {
            "magpie::a": BackendResult(text="from magpie"),
            "other::b": BackendResult(text="from other"),
        }

        def resolve(_modality, member):
            provider = member.split("::")[0]
            return ProviderDefinition(
                id=provider, display_name=provider, category="language",
                kind="openai_compatible", base_url="http://example.test",
                modalities=("chat",), discovery="static"), {"model_id": member.split("::")[1]}

        class _Member:
            def __init__(self, result):
                self.result = result
                self.calls: list[str] = []

            def generate(self, request):
                self.calls.append(request.model_id)
                return self.result

        adapters = {name: _Member(result) for name, result in answers.items()}
        self.pool_calls = self.pool._member_calls
        backend = PoolBackend()
        backend.attach(resolve, lambda definition: adapters[f"{definition.id}::a"]
                       if f"{definition.id}::a" in adapters
                       else adapters[f"{definition.id}::b"])
        from gateway import pools

        with patch.object(pools, "POOLS_PATH", Path(_SANDBOX.name) / "pools.json"):
            pools._cache["key"] = None
            pools.create("mix", ["magpie::a", "other::b"])
            self.pool_calls["magpie"] = 1  # the first member has no free slot
            request = self._request()
            result = backend.generate(request)
        self.assertEqual("from other", result.text)
        self.assertEqual([], adapters["magpie::a"].calls)
        outcomes = [a.get("code") for a in result.payload["pool"]["attempts"]]
        self.assertIn("POOL_MEMBER_BUSY", outcomes)

    def test_a_member_request_carries_its_deadline_and_cancel_hook(self) -> None:
        from gateway.adapters.base import BackendResult
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition
        from gateway import pools

        seen: list[BackendRequest] = []

        class _Member:
            def generate(self, request):
                seen.append(request)
                return BackendResult(text="ok")

        backend = PoolBackend()
        backend.attach(
            lambda _m, member: (ProviderDefinition(
                id=member.split("::")[0], display_name="Free", category="language",
                kind="openai_compatible", base_url="http://example.test",
                modalities=("chat",), discovery="static"), {"model_id": "x"}),
            lambda _definition: _Member())
        with patch.object(pools, "POOLS_PATH", Path(_SANDBOX.name) / "pools-two.json"):
            pools._cache["key"] = None
            pools.create("mix2", ["free::x"])
            backend.generate(self._request(name="mix2"))
        self.assertEqual(600.0, seen[0].deadline_seconds)
        self.assertIsNotNone(seen[0].cancelled)
        self.assertFalse(seen[0].cancelled())

    def test_a_hub_member_keeps_the_hubs_own_ceiling(self) -> None:
        from gateway.adapters.base import BackendResult
        from gateway.adapters.pool import PoolBackend
        from gateway.providers.definitions import ProviderDefinition
        from gateway import pools

        seen: list[BackendRequest] = []

        class _Member:
            def generate(self, request):
                seen.append(request)
                return BackendResult(text="ok")

        backend = PoolBackend()
        backend.attach(
            lambda _m, member: (ProviderDefinition(
                id=member.split("::")[0], display_name="Mac", category="local",
                kind="studio_hub", base_url="http://example.test",
                modalities=("chat",), discovery="static"), {"model_id": "x"}),
            lambda _definition: _Member())
        with patch.object(pools, "POOLS_PATH", Path(_SANDBOX.name) / "pools-hub.json"):
            pools._cache["key"] = None
            pools.create("mix3", ["studio-hub-local::x"])
            backend.generate(self._request(name="mix3"))
        self.assertIsNone(seen[0].deadline_seconds, "a Hub job may legitimately run for hours")


class ChatDeadlineTest(unittest.TestCase):
    def test_a_member_deadline_tightens_the_lane_ceiling_and_never_extends_it(self) -> None:
        request = BackendRequest(provider_id="p", model_id="m", modality="chat", params={},
                                 values={}, base_url="")
        self.assertIs(openai_compat.CHAT_TIMEOUT, openai_compat.chat_timeout(request))
        request.deadline_seconds = 600
        self.assertEqual(600.0, openai_compat.chat_timeout(request).read)
        request.deadline_seconds = CHAT_DEADLINE_CEILING_S * 10
        self.assertEqual(CHAT_DEADLINE_CEILING_S, openai_compat.chat_timeout(request).read)

    def test_a_cancel_stops_the_retry_send_the_adapter_would_otherwise_make(self) -> None:
        calls: list[dict] = []
        cancelled = {"now": False}

        def fake_post(url, *, headers, json, timeout):
            calls.append(dict(json))
            cancelled["now"] = True  # the owner cancels while the first answer is in flight
            return httpx.Response(400, json={"error": {"message": (
                "Unsupported value: 'temperature' does not support 0.7 with this model. "
                "Only the default (1) value is supported.")}})

        request = BackendRequest(
            provider_id="openai-direct", model_id="gpt-6-luna", modality="chat",
            params={"input": [{"role": "user", "content": "hi"}], "temperature": 0.7},
            values={"base_url": "http://mock", "api_key": "k"}, base_url="http://mock",
            cancelled=lambda: cancelled["now"])
        with patch.object(openai_compat.httpx, "post", fake_post):
            with self.assertRaises(BackendError) as caught:
                openai_compat._chat(request)
        self.assertEqual("CANCELLED", caught.exception.code)
        self.assertIs(False, caught.exception.upstream_accepted)
        self.assertEqual(1, len(calls), "the second request was never sent")


# ── 3. Generic liveness ──────────────────────────────────────────────────
class RerouteLivenessTest(_Base):
    """Review round 2 (`probe_reroute_sweep.py`): a re-route clears the upstream id, not the start.

    `store.record_route` drops the old site's `upstream_job_id` and nothing else stamps the new
    attempt, while `begin_dispatch` only stamps `dispatch_started_at` when it is NULL and the
    queue-wait statistic reads that column. So a job 30 min old that moves to a second site was
    failed `PROVIDER_TIMEOUT` — retryable — while the second site's submit was still in flight.
    """

    #: A real 1×1 PNG: `artifact_problem` reads the header, so the second answer is usable.
    PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00"
           b"\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n"
           b"-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

    def _patches(self, store, seen, second_started, release):
        import types

        first, second = _pid("imgA"), _pid("imgB")
        png = self.PNG

        def definition(provider_id):
            if provider_id in (first, second):
                return types.SimpleNamespace(id=provider_id, kind="openai", display_name=provider_id,
                                             base_url=f"http://{provider_id}.invalid")
            return None

        def enabled(modality):
            return [(definition(pid), {"model_id": IMAGE_MODEL, "modality": modality, "raw": {}})
                    for pid in (first, second)]

        class Adapter:
            def __init__(self, provider_id: str) -> None:
                self.provider_id = provider_id

            def generate(self, request):
                job_id = str((request.params or {}).get("_job_id") or "")
                seen.append((self.provider_id, job_id))
                if self.provider_id == first:
                    # Accepted, ran, and settled as a failure after 30 min — the live shape.
                    if request.checkpoint is not None:
                        request.checkpoint("batch-A")
                    with store._lock:
                        store._conn.execute(
                            "UPDATE jobs SET dispatch_started_at = ? WHERE id = ?",
                            (time.time() - 1800.0, job_id))
                        store._conn.commit()
                    raise BackendError("the Hub reported the item failed after 30 min",
                                       code="PROVIDER_FAILED", retryable=True,
                                       upstream_accepted=True, upstream_settled=True)
                second_started.set()          # the new site's submit is in flight
                release.wait(30)
                return BackendResult(media_type="image/png", content=png)

        return (
            patch.object(registry.models, "enabled_models", enabled),
            patch.object(registry, "definition", definition),
            patch.object(registry.credentials, "is_enabled", lambda pid: True),
            patch.object(registry.credentials, "resolved", lambda pid: {}),
            patch.object(worker_module, "adapter_for", lambda d: Adapter(str(d.id))),
        )

    def test_a_re_routed_job_is_not_failed_while_its_new_site_submits(self) -> None:
        import threading

        self.sites = [_site("imgA", macs=4), _site("imgB", macs=4)]
        seen: list = []
        second_started, release = threading.Event(), threading.Event()
        row, _ = self.store.submit(modality="image", model_id=IMAGE_MODEL, params={},
                                   idempotency_key="reroute-1", provider_id=_pid("imgA"))
        worker = Worker(self.store, interval_s=0.05, pool_size=0)
        patchers = self._patches(self.store, seen, second_started, release)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(target=worker._run, args=(self.store.get(row["id"]),))
        thread.start()
        try:
            self.assertTrue(second_started.wait(10), f"the job never re-routed: {seen}")
            before = self.store.get(row["id"])
            self.assertEqual(_pid("imgB"), before["provider_id"])
            self.assertIsNone(before["upstream_job_id"], "a re-route drops the old site's id")
            self.assertGreater(time.time() - float(before["dispatch_started_at"]), 720,
                               "the first attempt's stamp is old, as after a long run")

            out = worker._check_running_liveness()
            self.assertEqual({"checked": 1, "said_something": 0, "failed": 0}, out)
            self.assertEqual("running", self.store.get(row["id"])["state"],
                             "the live attempt must not be failed as silent")
        finally:
            release.set()
            thread.join(timeout=30)
        final = self.store.get(row["id"])
        self.assertEqual("succeeded", final["state"])
        self.assertIsNone(final["error_code"])
        self.assertEqual([pid for pid, _ in seen], [_pid("imgA"), _pid("imgB")],
                         "one attempt per site, no duplicate send")

    def test_a_re_routed_attempt_that_really_goes_silent_still_fails(self) -> None:
        # The other half: the sweep must still end a re-routed attempt that goes quiet past the
        # lane's ceiling, so the stamp is a floor for it to read, not a blanket exemption.
        self.sites = [_site("imgA", macs=2), _site("imgB", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        self.assertTrue(self.store.mark_running(job_id))
        self.assertTrue(self.store.begin_dispatch(job_id))
        self.store.record_route(job_id, _pid("imgB"))
        worker._running[job_id] = self.store.get(job_id)
        with self.store._lock:
            stale = time.time() - 1800.0
            self.store._conn.execute(
                "UPDATE jobs SET dispatch_started_at = ?, attempt_started_at = ? WHERE id = ?",
                (stale, stale, job_id))
            self.store._conn.commit()
        out = worker._check_running_liveness()
        self.assertEqual(1, out["failed"])
        row = self.store.get(job_id)
        self.assertEqual("failed", row["state"])
        self.assertEqual("PROVIDER_TIMEOUT", row["error_code"])
        self.assertTrue(row["retryable"])

    def test_a_new_send_stamps_its_own_attempt_start(self) -> None:
        # The stamp is written by the send itself, so the sweep never has to guess.
        self.sites = [_site("img1", macs=2)]
        job_id = self.job(IMAGE_MODEL)
        before = time.time()
        self.assertTrue(self.store.mark_running(job_id))
        self.assertTrue(self.store.begin_dispatch(job_id))
        row = self.store.get(job_id)
        self.assertIsInstance(row["attempt_started_at"], float)
        self.assertGreaterEqual(float(row["attempt_started_at"]), before - 1.0)
        self.assertEqual(float(row["attempt_started_at"]), float(row["dispatch_started_at"]))


class RunningJobLivenessTest(_Base):
    def _running(self, worker: Worker, job_id: str) -> dict:
        self.assertTrue(self.store.mark_running(job_id))
        self.assertTrue(self.store.begin_dispatch(job_id))
        job = self.store.get(job_id)
        worker._running[job_id] = job
        return job

    def test_a_running_job_with_no_id_says_what_it_is_waiting_for(self) -> None:
        self.sites = [_site("img1", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        self._running(worker, job_id)
        started = float(self.store.get(job_id)["dispatch_started_at"])

        out = worker._check_running_liveness(now=started + 200)
        self.assertEqual({"checked": 1, "said_something": 1, "failed": 0}, out)
        waiting = self.store.get(job_id)["waiting"]
        self.assertIn("no job id yet", waiting["reason"])
        self.assertIn("img1", waiting["reason"])
        self.assertEqual({"checked": 1, "said_something": 1, "failed": 0}, worker.liveness()["running_sweep"])

        # Once it has said something, a second sweep does not say it again.
        out = worker._check_running_liveness(now=started + 260)
        self.assertEqual(0, out["said_something"])

    def test_a_job_with_an_upstream_id_is_left_alone(self) -> None:
        self.sites = [_site("img1", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        self._running(worker, job_id)
        self.assertTrue(self.store.record_upstream_acceptance(job_id, "batch-1"))
        started = float(self.store.get(job_id)["dispatch_started_at"])
        out = worker._check_running_liveness(now=started + 3 * 3600)
        self.assertEqual({"checked": 0, "said_something": 0, "failed": 0}, out)
        self.assertEqual("running", self.store.get(job_id)["state"])

    def test_a_job_silent_past_its_lane_deadline_fails_retryably(self) -> None:
        self.sites = [_site("img1", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        self._running(worker, job_id)
        started = float(self.store.get(job_id)["dispatch_started_at"])

        out = worker._check_running_liveness(now=started + worker_module.lane_deadline_s("image") + 1)
        self.assertEqual(1, out["failed"])
        row = self.store.get(job_id)
        self.assertEqual("failed", row["state"])
        self.assertEqual("PROVIDER_TIMEOUT", row["error_code"])
        self.assertTrue(row["retryable"])
        self.assertFalse(row["recovery_required"], "a retryable failure is not an unknown-outcome hold")
        self.assertIn("never reported a job id", row["error_detail"])

    def test_a_job_the_restart_re_sent_is_not_failed_by_the_sweep(self) -> None:
        """The review's `probe_resend_race.py`: the restart re-sends without refreshing the stamp.

        `_resume_interrupted` stamps `restart_resent_at` and leaves `dispatch_started_at` as it was
        before the shutdown, so measuring silence from the old stamp failed a job the restart had
        just re-sent — while its own send was in flight. A *retryable* failure there makes Story
        submit again, and a re-sent Hub job can be charged twice.
        """
        self.sites = [_site("img1", macs=4, supply={IMAGE_MODEL: {"eligible": 4, "available": 4}},
                            models=[IMAGE_MODEL])]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        ids = []
        for index, age in enumerate((2 * 3600, 20 * 60)):   # both past the 720 s media deadline
            job_id = self.job(IMAGE_MODEL)
            with self.store._lock:
                self.store._conn.execute(
                    "UPDATE jobs SET state = 'running', dispatch_started_at = ?, "
                    "upstream_job_id = NULL WHERE id = ?", (time.time() - age, job_id))
                self.store._conn.commit()
            self.store.note_restart_resend(job_id)          # what the restart replay does
            worker._running[job_id] = self.store.get(job_id)
            ids.append(job_id)

        out = worker._check_running_liveness()
        self.assertEqual({"checked": 2, "said_something": 0, "failed": 0}, out,
                         "a re-sent job is not silent: its own send began just now")
        for job_id in ids:
            row = self.store.get(job_id)
            self.assertEqual("running", row["state"])
            self.assertIsNone(row["error_code"])

    def test_a_job_the_restart_re_sent_that_then_goes_silent_still_fails(self) -> None:
        # The other half: the later stamp is used, not blindly ignored, so the sweep still ends a
        # re-send that really does go quiet past the lane's ceiling.
        self.sites = [_site("img1", macs=2)]
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET state = 'running', dispatch_started_at = ?, "
                "restart_resent_at = ?, upstream_job_id = NULL WHERE id = ?",
                (time.time() - 4 * 3600, time.time() - 4 * 3600, job_id))
            self.store._conn.commit()
        worker._running[job_id] = self.store.get(job_id)
        out = worker._check_running_liveness()
        self.assertEqual(1, out["failed"])
        self.assertEqual("PROVIDER_TIMEOUT", self.store.get(job_id)["error_code"])

    def test_the_deadline_is_the_lanes_own_ceiling(self) -> None:
        self.assertEqual(CHAT_DEADLINE_CEILING_S + worker_module._LANE_DEADLINE_GRACE_S,
                         worker_module.lane_deadline_s("chat"))
        self.assertEqual(openai_compat.MEDIA_TIMEOUT.read + worker_module._LANE_DEADLINE_GRACE_S,
                         worker_module.lane_deadline_s("image"))


# ── 4. What Story reads back for a job that failed on submit timeouts ────
class TtsStatusAfterSubmitTimeoutsTest(_Base):
    """Story still showed seven TTS tasks as `accepted` about 230 min after the Gateway failed them.

    This pins the Gateway's half: once the row is failed, every route Story reads a TTS job by
    publishes that verdict, with its code and its sentence. If this is green, the remaining gap is
    on the Story side and no Gateway change can close it.
    """

    def test_a_job_failed_after_repeated_submit_timeouts_reads_failed(self) -> None:
        from gateway import api

        self.sites = [_site("terranash", macs=1, supply={OTHER_MODEL: {"eligible": 1, "available": 1}},
                            models=[OTHER_MODEL])]
        job_id = self.job(TTS_MODEL, site="terranash")
        # Exactly the worker's own settlement after the Hub never answered a submit.
        worker_module._fail(
            self.store, job_id, code="SITE_CAPACITY_UNAVAILABLE", retryable=True,
            detail="hub submit failed: timed out")
        row = self.store.get(job_id)
        self.assertEqual("failed", row["state"])

        with patch.object(api, "store", self.store):
            body = api.get_tts_job(job_id)
        self.assertEqual("failed", body["state"])
        self.assertEqual("SITE_CAPACITY_UNAVAILABLE", body["failure_code"])
        self.assertEqual("hub submit failed: timed out", body["failure_reason"])
        self.assertFalse(body["recovery_required"], "a plain retryable failure is not a hold")
        self.assertFalse(body["cancellable"])

        with patch.object(api, "store", self.store):
            by_key = api.tts_by_idempotency_key(row["idempotency_key"])
        self.assertEqual("failed", by_key["state"])
        self.assertEqual(body["failure_code"], by_key["failure_code"])


class PoolMemberBusyDefersTest(_Base):
    def test_a_busy_member_puts_the_job_back_in_the_queue_with_its_reason(self) -> None:
        """`POOL_MEMBER_BUSY` is a fact about this minute, and nothing was sent, so the job waits
        in the Gateway's own queue with the reason published — never a failure."""
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job(IMAGE_MODEL)
        current = self.store.get(job_id)
        error = BackendError("Pool 'mix': Magpie had no free slot after 15 min.",
                             code="POOL_MEMBER_BUSY", retryable=True,
                             upstream_accepted=False, upstream_settled=True)
        self.assertTrue(worker_module._defer_if_transient(
            self.store, job_id, error, current, unknown=False))
        row = self.store.get(job_id)
        self.assertEqual("accepted", row["state"])
        self.assertIn("no free slot", row["defer_reason"])
        self.assertIsNone(row["recovery_state"], "a wait is not a hold")


# ── 5. A claim begins a new attempt (0.2.209) ────────────────────────────
class ClaimWindowTest(_Base):
    """`mark_running` cleared `dispatch_started_at` and nothing else.

    A claim and `begin_dispatch` are two steps, so between them the row still carried the
    **previous** attempt's `attempt_started_at` / `restart_resent_at`. `_silent_since` reads the
    latest of the three stamps, so a job claimed after a long first attempt measured silence from
    that old attempt: in a slow claim→send window the liveness sweep failed it `PROVIDER_TIMEOUT` —
    retryable — before its own send, and a retryable failure on work that may be live upstream is
    exactly what makes Story submit it again.
    """

    def _age_previous_attempt(self, job_id: str, seconds: float = 3 * 3600) -> float:
        old = time.time() - seconds
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE jobs SET attempt_started_at = ?, restart_resent_at = ?, "
                "dispatch_started_at = ? WHERE id = ?", (old, old, old, job_id))
            self.store._conn.commit()
        return old

    def test_a_claim_clears_the_previous_attempts_stamps(self) -> None:
        job_id = self.job()
        old = self._age_previous_attempt(job_id)
        self.assertLessEqual(worker_module._silent_since(self.store.get(job_id)), old)

        self.assertTrue(self.store.mark_running(job_id))
        row = self.store.get(job_id)
        self.assertIsNone(row["attempt_started_at"], "a claim starts a new attempt")
        self.assertIsNone(row["restart_resent_at"], "a claim starts a new attempt")
        self.assertIsNone(row["dispatch_started_at"], "the queue-wait statistic still reads NULL")
        self.assertGreater(worker_module._silent_since(row), old + 3600,
                           "the claim itself is the floor until a send stamps its own attempt")

    def test_the_sweep_does_not_fail_the_job_between_its_claim_and_its_send(self) -> None:
        worker = Worker(self.store, interval_s=10.0, pool_size=0)
        job_id = self.job()
        self._age_previous_attempt(job_id)
        self.assertTrue(self.store.mark_running(job_id))
        worker._running[job_id] = self.store.get(job_id)
        out = worker._check_running_liveness()
        self.assertEqual({"checked": 1, "said_something": 0, "failed": 0}, out)
        row = self.store.get(job_id)
        self.assertEqual("running", row["state"])
        self.assertIsNone(row["error_code"])

    def test_the_send_after_the_claim_still_stamps_its_own_attempt(self) -> None:
        # The protection is the claim's floor, not a missing stamp: opening the window still
        # writes both columns, so a send that really goes silent is still ended by the sweep.
        job_id = self.job()
        self.assertTrue(self.store.mark_running(job_id))
        self.assertTrue(self.store.begin_dispatch(job_id))
        row = self.store.get(job_id)
        self.assertIsNotNone(row["dispatch_started_at"])
        self.assertIsNotNone(row["attempt_started_at"])


def seconds_of(old: float) -> float:
    """A comparison margin that cannot pass by luck: the old stamp is hours behind the claim."""
    return 3600.0


if __name__ == "__main__":
    unittest.main()
