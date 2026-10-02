#!/usr/bin/env python3
"""Honest outcomes (2026-10-03): a failure before any request is sent is a plain failure.

Gateway 0.2.97/0.2.98's pool raised `AttributeError` while resolving members — before anything was
sent anywhere — and every such job was recorded "Upstream outcome is unknown; reconcile …" because
`dispatch_started_at` is stamped before the adapter runs. Pinned here:

* the worker asks "was a request actually sent?" (`requests_started`, counted at `httpx.Client.send`);
* a pool whose members failed without leaving anything outstanding raises a *known* failure;
* `recovery_required` means an unknown outcome only, never a plain retryable failure;
* the one-off repair relabels exactly the pool bug's rows, once;
* `idempotent_replay` on a POST that hands back an existing job;
* `chargeable` on chat rows is the Models page's Free/Paid, and a pool's is its members'.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-honest-")
base = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(base), "GATEWAY_DATA": str(base / "data"),
    "GATEWAY_ARTIFACTS": str(base / "artifacts"), "GATEWAY_DB": str(base / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(base / "credentials.json"), "GATEWAY_MODEL_CACHE": str(base / "models.json"),
    "GATEWAY_ENV_FILE": str(base / ".env"), "GATEWAY_FLEET": str(base / "fleet.json"),
    "GATEWAY_VOICES": str(base / "voices.json"), "GATEWAY_VOICES_OUT": str(base / "voices.json"),
    "GATEWAY_POOLS": str(base / "pools.json"),
    "STUDIOHUB_READY_FILE": str(base / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "fixture-key",
})

import httpx  # noqa: E402

from gateway import worker as worker_module  # noqa: E402
from gateway.adapters.base import (  # noqa: E402
    BackendError, BackendRequest, note_request_started, requests_started,
)
from gateway.adapters.pool import PoolBackend  # noqa: E402
from gateway.pricing import chargeable  # noqa: E402
from gateway.store import RECOVERY_UNKNOWN, JobStore  # noqa: E402

BUG = "AttributeError: module 'gateway.catalog' has no attribute 'resolve'"


class _Registry:
    credentials = SimpleNamespace(is_enabled=lambda _p: True, resolved=lambda _p: {})
    models = SimpleNamespace(enabled_models=lambda _m: [
        (SimpleNamespace(id="mock-provider", base_url="http://mock", display_name="Mock"),
         {"model_id": "mock-model"})])

    @staticmethod
    def definition(_provider_id):
        return SimpleNamespace(id="mock-provider", base_url="http://mock", display_name="Mock")


class _Raises:
    """An adapter that raises `exc`, after (optionally) sending a request."""

    def __init__(self, exc: Exception, *, sends: bool) -> None:
        self.exc, self.sends = exc, sends

    def generate(self, request):
        if self.sends:
            note_request_started()
        raise self.exc


class SendCounterTest(unittest.TestCase):
    def test_every_httpx_send_is_counted_on_this_thread(self) -> None:
        before = requests_started()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        with httpx.Client(transport=transport) as client:
            client.get("http://example.test/a")
            client.post("http://example.test/b", json={})
        self.assertEqual(requests_started() - before, 2)


class WorkerTest(unittest.TestCase):
    def run_job(self, adapter, name: str) -> dict:
        store = JobStore(base / f"{name}.sqlite3")
        self.addCleanup(store.close)
        row, _ = store.submit(modality="chat", model_id="mock-model", params={},
                              idempotency_key=name, provider_id="mock-provider")
        self.assertTrue(store.mark_running(row["id"]))
        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module, "adapter_for", return_value=adapter):
            worker_module.execute_job(row, store)
        job = store.get(row["id"])
        self.assertIsNotNone(job["dispatch_started_at"], "dispatch had begun in every case")
        return job

    def test_a_gateway_bug_before_any_request_is_a_plain_retryable_failure(self) -> None:
        job = self.run_job(_Raises(AttributeError(BUG[len("AttributeError: "):]), sends=False), "bug")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "PROVIDER_FAILED")
        self.assertTrue(job["retryable"])
        self.assertFalse(job["recovery_required"])
        self.assertNotEqual(job["recovery_state"], RECOVERY_UNKNOWN)
        self.assertNotIn("Upstream outcome is unknown", job["error_detail"])
        self.assertIn("before any request was sent", job["error_detail"])

    def test_a_gateway_bug_after_a_request_went_out_is_still_unknown(self) -> None:
        job = self.run_job(_Raises(RuntimeError("parse failed"), sends=True), "bug-after-send")
        self.assertEqual(job["recovery_state"], RECOVERY_UNKNOWN)
        self.assertTrue(job["recovery_required"])

    def test_an_adapter_refusal_before_sending_is_not_a_hold(self) -> None:
        refusal = BackendError("needs a prompt", code="INVALID_PARAMS")  # no status, nothing stated
        job = self.run_job(_Raises(refusal, sends=False), "refusal")
        self.assertEqual(job["state"], "failed")
        self.assertFalse(job["recovery_required"])
        self.assertNotIn("Upstream outcome is unknown", job["error_detail"])

    def test_a_dropped_connection_after_sending_is_still_a_hold(self) -> None:
        dropped = BackendError("connection dropped", code="PROVIDER_UNAVAILABLE", retryable=True)
        job = self.run_job(_Raises(dropped, sends=True), "dropped")
        self.assertEqual(job["recovery_state"], RECOVERY_UNKNOWN)
        self.assertTrue(job["recovery_required"])


class PoolTest(unittest.TestCase):
    def backend(self, adapters: dict, members: list[str]) -> PoolBackend:
        def resolve(_modality, member):
            if member not in adapters:
                return None, None
            return SimpleNamespace(id=member, kind="openai_compatible", base_url="", display_name=member), \
                {"model_id": "x"}
        backend = PoolBackend()
        backend.attach(resolve, lambda definition: adapters[definition.id])
        backend._credentials_for = lambda _p: {}
        self.addCleanup(patch.stopall)
        patch("gateway.pools.members", return_value=members).start()
        patch("gateway.pool_cooldowns.until", return_value=None).start()
        patch("gateway.pool_cooldowns.note_failure").start()
        return backend

    def request(self) -> BackendRequest:
        return BackendRequest(provider_id="pool", model_id="free", modality="chat",
                              params={}, values={}, base_url="")

    def test_a_member_that_failed_before_sending_does_not_make_the_pool_unknown(self) -> None:
        backend = self.backend({
            "a::x": _Raises(BackendError("quota", code="PROVIDER_QUOTA_EXHAUSTED", status=429), sends=True),
            "b::x": _Raises(KeyError("bug"), sends=False),
        }, ["a::x", "b::x"])
        with self.assertRaises(BackendError) as caught:
            backend.generate(self.request())
        self.assertIs(caught.exception.upstream_accepted, False)

    def test_a_member_that_sent_and_went_silent_keeps_the_pool_ambiguous(self) -> None:
        backend = self.backend({
            "a::x": _Raises(BackendError("timeout", code="PROVIDER_UNAVAILABLE"), sends=True),
            "b::x": _Raises(KeyError("bug"), sends=False),
        }, ["a::x", "b::x"])
        with self.assertRaises(BackendError) as caught:
            backend.generate(self.request())
        self.assertIsNone(caught.exception.upstream_accepted)

    def test_the_pool_bug_itself_reaches_the_worker_as_never_sent(self) -> None:
        backend = PoolBackend()
        backend.attach(lambda *_: (_ for _ in ()).throw(AttributeError(BUG)), None)
        with patch("gateway.pools.members", return_value=["a::x"]):
            before = requests_started()
            with self.assertRaises(AttributeError):
                backend.generate(self.request())
        self.assertEqual(requests_started(), before)


class RepairTest(unittest.TestCase):
    def seed(self, path: Path) -> dict[str, str]:
        store = JobStore(path)
        ids = {}
        for name, provider, detail, upstream in (
            ("bug", "pool", f"{BUG} Upstream outcome is unknown; reconcile …", None),
            ("other-hold", "pool", "RuntimeError: x Upstream outcome is unknown; …", None),
            ("hub-hold", "studio-hub-a", f"{BUG} …", None),
            ("sent", "pool", f"{BUG} …", "up-1"),
        ):
            row, _ = store.submit(modality="chat", model_id="m", params={}, provider_id=provider,
                                  idempotency_key=f"{path.name}-{name}")
            store.fail(row["id"], code="PROVIDER_FAILED", detail=detail,
                       recovery_state=RECOVERY_UNKNOWN, recovery_detail=detail)
            if upstream:
                store._conn.execute("UPDATE jobs SET upstream_job_id = ? WHERE id = ?", (upstream, row["id"]))
                store._conn.commit()
            ids[name] = row["id"]
        store.close()
        return ids

    def test_exactly_the_pool_bug_rows_are_relabelled_once(self) -> None:
        path = base / "repair.sqlite3"
        ids = self.seed(path)
        store = JobStore(path)
        self.addCleanup(store.close)
        fixed = store.get(ids["bug"])
        self.assertEqual((fixed["state"], fixed["retryable"], fixed["recovery_state"]),
                         ("failed", True, "retryable"))
        self.assertFalse(fixed["recovery_required"])
        self.assertIsNone(fixed["recovery_detail"])
        self.assertTrue(fixed["error_detail"].startswith(BUG))
        self.assertNotIn("Upstream outcome is unknown", fixed["error_detail"])
        for untouched in ("other-hold", "hub-hold", "sent"):
            self.assertEqual(store.get(ids[untouched])["recovery_state"], RECOVERY_UNKNOWN, untouched)
        self.assertEqual(store._relabel_never_sent_pool_failures(), [], "idempotent")


class ReplayTest(unittest.TestCase):
    def test_a_repeated_key_is_marked_as_a_replay(self) -> None:
        from fastapi.testclient import TestClient
        from gateway import api

        definition = SimpleNamespace(id="mock-provider")
        client = TestClient(api.app, client=("127.0.0.1", 54321))
        self.addCleanup(client.close)
        headers = {"Authorization": "Bearer fixture-key", "Idempotency-Key": "replay-1"}
        with patch.object(api.catalog, "resolve", return_value=(definition, {"model_id": "m"})):
            first = client.post("/v1/chat/responses", json={"model_id": "mock-provider::m"}, headers=headers)
            again = client.post("/v1/chat/responses", json={"model_id": "mock-provider::m"}, headers=headers)
        self.assertEqual((first.status_code, again.status_code), (202, 200))
        self.assertIs(first.json()["idempotent_replay"], False)
        self.assertIs(again.json()["idempotent_replay"], True)
        self.assertEqual(first.json()["id"], again.json()["id"])


class ChargeableTest(unittest.TestCase):
    def test_free_and_paid_follow_the_models_page(self) -> None:
        unpublished = {"price_source": "unpublished", "price_usd": None}
        self.assertFalse(chargeable(True, "gemini-flash", unpublished), "a free-tier provider")
        self.assertFalse(chargeable(False, "meta/llama:free", unpublished), "a :free route")
        self.assertFalse(chargeable(False, "m", {"price_source": "local", "price_usd": {}}), "your Macs")
        self.assertFalse(chargeable(False, "m", {"price_source": "provider",
                                                 "price_usd": {"input_per_1m_tokens": 0,
                                                               "output_per_1m_tokens": 0}}))
        self.assertTrue(chargeable(False, "gpt-x", unpublished), "unpublished is never free")
        self.assertTrue(chargeable(False, "m", {"price_source": "provider",
                                                "price_usd": {"input_per_1m_tokens": 0,
                                                              "output_per_1m_tokens": 2.5}}))

    def test_chat_rows_and_pools_publish_it(self) -> None:
        from gateway import catalog as catalog_module
        from gateway.providers.definitions import ProviderDefinition

        def provider(pid: str, kind: str = "openai_compatible", free: bool = False) -> ProviderDefinition:
            return ProviderDefinition(id=pid, display_name=pid, category="language", kind=kind,
                                      base_url="http://x", modalities=("chat",), discovery="static",
                                      free_tier=free)

        model = lambda mid: {"model_id": mid, "modality": "chat", "display_name": mid, "raw": {}}  # noqa: E731
        enabled = [(provider("paid-main"), model("big")), (provider("free-tier", free=True), model("small")),
                   (provider("pool", kind="pool"), model("mixed")),
                   (provider("pool", kind="pool"), model("all-free"))]
        members = {"mixed": ["free-tier::small", "paid-main::big"], "all-free": ["free-tier::small", "gone::x"]}
        built = catalog_module.Catalog()
        with patch.object(catalog_module.registry.models, "enabled_models",
                          side_effect=lambda m: enabled if m == "chat" else []), \
                patch.object(catalog_module.pools, "members", side_effect=lambda name: members[name]):
            built.rebuild(force=True)
            rows = {row["id"]: row for row in built._rows["chat"]}
        self.assertTrue(rows["paid-main::big"]["chargeable"])
        self.assertFalse(rows["free-tier::small"]["chargeable"])
        self.assertTrue(rows["pool::mixed"]["chargeable"], "one paid member makes a pool chargeable")
        self.assertFalse(rows["pool::all-free"]["chargeable"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
