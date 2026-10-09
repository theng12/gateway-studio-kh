"""A fleet send still unanswered at a restart is re-sent, not held (0.2.160, owner 2026-10-09).

The drain (0.2.158) waits 15 s for every send to get a batch id; one that never answered was held
`UNKNOWN_OUTCOME` for the owner. The Hub answers a repeated `clientRequestId` with the batch it
already made (Hub `broker._submit_batch_locked`, durable in its ledger), so the next start sends it
again to the **same site with the same key**: it attaches to that batch, or runs once if the Hub never
got it. Anything short of that is held exactly as before, and never moved to another site. Cloud
lanes keep today's hold.

    .venv/bin/python tests/test_restart_resend.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-restart-resend-")
os.environ["GATEWAY_HOST"] = "127.0.0.1"

import httpx  # noqa: E402

from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.store import JobStore  # noqa: E402

_ROOT = Path(_SANDBOX.name)
HUB = "https://site-a.test"
SITE = "studio-hub-a"
ARTIFACT = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01png-bytes"


class _Registry:
    credentials = SimpleNamespace(is_enabled=lambda _pid: True, resolved=lambda _pid: {})

    def __init__(self, kind: str = "studio_hub") -> None:
        self.kind = kind

    def definition(self, provider_id: str):
        return SimpleNamespace(id=provider_id, kind=self.kind, base_url=HUB, display_name="Site A")


class _Hub:
    """The Hub's submit door as broker.py has it: a repeated clientRequestId gets the batch it made."""

    def __init__(self, known: dict[str, str] | None = None, fault: object = None) -> None:
        self.known = dict(known or {})
        self.fault = fault
        self.runs = 0  # batches this Hub newly created, i.e. work started
        self.posts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/hub/jobs":
            body = json.loads(request.content)
            self.posts.append((request.url.host, body))
            if self.fault == "connect":
                raise httpx.ConnectError("refused")
            if self.fault == "timeout":
                raise httpx.ReadTimeout("timed out")
            if isinstance(self.fault, tuple):
                return httpx.Response(self.fault[0], json={"detail": self.fault[1]})
            key = body["clientRequestId"]
            if key in self.known:
                return httpx.Response(200, json={"batch_id": self.known[key], "replayed": True})
            self.runs += 1
            self.known[key] = f"batch-{self.runs}"
            return httpx.Response(200, json={"batch_id": self.known[key], "replayed": False})
        if request.method == "GET" and path.endswith("/artifact"):
            return httpx.Response(200, content=ARTIFACT, headers={
                "content-type": "image/png", "X-Content-SHA256": hashlib.sha256(ARTIFACT).hexdigest()})
        if request.method == "GET" and path.startswith("/api/hub/jobs/"):
            batch = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"batch_id": batch, "items": [
                {"index": 0, "state": "done", "terminal_result": {"machine_id": "mac-1"}}]})
        if request.method == "POST" and path.endswith("/ack"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"no route {request.method} {path}"})


class _Case(unittest.TestCase):
    def interrupted(self, name: str, *, provider_id: str = SITE, modality: str = "image",
                    cancel: bool = False) -> tuple[JobStore, str]:
        """A job whose send was unanswered when the Gateway stopped: dispatched, no batch id."""
        store = JobStore(_ROOT / f"{name}.sqlite3")
        row, _ = store.submit(modality=modality, model_id="model/test", params={"prompt": "x"},
                              idempotency_key=name, provider_id=provider_id)
        self.assertTrue(store.mark_running(row["id"]))
        self.assertTrue(store.begin_dispatch(row["id"]))
        if cancel:
            store.cancel(row["id"])
        store.close()
        reopened = JobStore(store.path)  # the restart
        self.addCleanup(reopened.close)
        return reopened, row["id"]

    def start(self, store: JobStore, hub: _Hub, registry: _Registry | None = None) -> list:
        moves: list = []
        real = httpx.Client

        def client(*args, **kwargs):
            kwargs.pop("transport", None)
            return real(transport=httpx.MockTransport(hub.handler), **kwargs)

        worker = worker_module.Worker(store)
        with patch.object(worker_module, "registry", registry or _Registry()), \
                patch.object(worker_module, "_next_machine", lambda *a, **k: moves.append(a)), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0), \
                patch.object(hub_module.httpx, "Client", side_effect=client):
            worker._resume_interrupted()
            self.assertTrue(worker.wait_idle(timeout=10))
        return moves

    def assert_held_at_the_same_site(self, store: JobStore, job_id: str, hub: _Hub, moves: list) -> None:
        job = store.get(job_id)
        self.assertEqual((job["state"], job["recovery_state"]), ("failed", "unknown_outcome"), "held")
        self.assertTrue(job["recovery_required"])
        self.assertEqual(job["provider_id"], SITE)
        self.assertEqual(moves, [], "never moved to another site")
        self.assertEqual({host for host, _ in hub.posts} - {"site-a.test"}, set())


class AFleetSendUnansweredAtShutdownIsReSent(_Case):
    def test_it_is_re_sent_with_the_same_key_and_attaches_to_the_existing_batch(self) -> None:
        store, job_id = self.interrupted("attach")
        self.assertEqual(store.get(job_id)["state"], "running", "not held at open")
        hub = _Hub(known={job_id: "batch-first"})  # the first send did reach the Hub
        moves = self.start(store, hub)
        job = store.get(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["upstream_job_id"], "batch-first", "attached to the existing batch")
        self.assertEqual(hub.runs, 0, "no second run")
        self.assertEqual([(h, b["clientRequestId"]) for h, b in hub.posts], [("site-a.test", job_id)])
        self.assertIsNotNone(job["restart_resent_at"], "recorded on the job")
        self.assertEqual(moves, [])

    def test_one_the_hub_never_got_runs_once(self) -> None:
        store, job_id = self.interrupted("fresh", modality="tts")
        hub = _Hub()
        self.start(store, hub)
        job = store.get(job_id)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(hub.runs, 1)
        self.assertEqual(len(hub.posts), 1)
        self.assertEqual(hub.posts[0][1]["clientRequestId"], job_id)

    def test_a_re_send_that_does_not_attach_is_held_never_moved(self) -> None:
        faults = {
            "connect": "connect",  # would normally go to the next site
            "timeout": "timeout",  # would normally be replayed, then moved
            "busy": (409, {"code": "MACHINE_BUSY"}),  # a location refusal: normally the next site
            "mismatch": (400, "clientRequestId was already used for a different batch"),
        }
        for name, fault in faults.items():
            with self.subTest(name):
                store, job_id = self.interrupted(f"fault-{name}")
                hub = _Hub(fault=fault)
                moves = self.start(store, hub)
                self.assert_held_at_the_same_site(store, job_id, hub, moves)
                self.assertEqual(len(hub.posts), 1, "sent once, to the same site")


class EverythingElseKeepsTheHold(_Case):
    def test_a_cloud_lane_is_held_as_before(self) -> None:
        store, job_id = self.interrupted("cloud", provider_id="fal")
        self.assertEqual(store.interrupted, [])
        job = store.get(job_id)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "UNKNOWN_OUTCOME"))
        self.assertIsNone(job["restart_resent_at"])

    def test_a_site_whose_adapter_is_not_the_hub_is_held_by_the_worker(self) -> None:
        store, job_id = self.interrupted("not-hub")
        hub = _Hub()
        moves = self.start(store, hub, _Registry(kind="openai_compat"))
        self.assert_held_at_the_same_site(store, job_id, hub, moves)
        self.assertEqual(hub.posts, [])

    def test_transcription_chat_and_a_cancelled_job_are_held(self) -> None:
        for name, kwargs in {"srt": {"modality": "transcription"}, "chat": {"modality": "chat"},
                             "cancel": {"cancel": True}}.items():
            with self.subTest(name):
                store, job_id = self.interrupted(f"held-{name}", **kwargs)
                self.assertEqual(store.interrupted, [])
                self.assertEqual(store.get(job_id)["error_code"], "UNKNOWN_OUTCOME")


if __name__ == "__main__":
    unittest.main()
