#!/usr/bin/env python3
"""A job is never `succeeded` with an empty or broken artifact (2026-10-09).

A Hub voice item finished `done` with a 0-byte m4a (Story ref 20b773bc); the Gateway stored it,
acknowledged it and marked the job succeeded, and Story failed EP0038's narration three times
collecting it. Now the bytes are checked before anything is written or acknowledged: an empty or
broken artifact is a settled, known failure of that attempt, so a fleet job is retried on another
Mac by the ordinary A7 rule.

Mocked Hub transport only: no live Hub, no generation, no installed app.
"""
from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

import httpx  # noqa: E402

from gateway import concurrency  # noqa: E402
from gateway import worker as worker_module  # noqa: E402
from gateway.adapters import BackendError  # noqa: E402
from gateway.adapters import studio_hub as hub_module  # noqa: E402
from gateway.adapters.base import BackendResult  # noqa: E402
from gateway.store import JobStore  # noqa: E402

A, B = "studio-hub-a", "studio-hub-b"
BASE = {A: "https://hub-a.test", B: "https://hub-b.test"}
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 4, 4) + b"\x08\x02\x00\x00\x00"
M4A = (b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00isom" + b"\x00\x00\x00\x08moov"
       + b"\x00\x00\x00\x0cmdat" + b"\x00" * 4)


class _Fleet:
    """Two Hubs that finish every item; `artifact` says what each one serves for it."""

    def __init__(self, artifact: dict[str, tuple[bytes, str]]) -> None:
        self.artifact = artifact
        self.calls: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = "a" if "hub-a" in request.url.host else "b"
        method, path = request.method, request.url.path
        self.calls.append((host, method, path))
        if method == "POST" and path == "/api/hub/jobs":
            json.loads(request.content)
            return httpx.Response(200, json={"batch_id": f"batch-{host}", "replayed": False})
        if method == "POST" and path.endswith("/ack"):
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and path.endswith("/artifact"):
            content, media = self.artifact[host]
            return httpx.Response(200, content=content, headers={"content-type": media})
        if method == "GET" and path == f"/api/hub/jobs/batch-{host}":
            return httpx.Response(200, json={"items": [{"index": 0, "state": "done",
                                                        "terminal_result": {"machine_id": host}}]})
        return httpx.Response(404, json={"detail": f"no route for {method} {path}"})

    def saw(self, host: str, method: str, suffix: str = "") -> list[str]:
        return [p for h, m, p in self.calls if (h, m) == (host, method) and p.endswith(suffix)]


class _Registry:
    def __init__(self, kind: str = "studio_hub") -> None:
        self.kind = kind
        self.credentials = SimpleNamespace(is_enabled=lambda _p: True, resolved=lambda _p: {})
        self.models = SimpleNamespace(enabled_models=lambda _m: [
            (SimpleNamespace(id=pid), {"model_id": "model/test"}) for pid in (A, B)])

    def definition(self, provider_id: str) -> Any:
        return SimpleNamespace(id=provider_id, kind=self.kind, base_url=BASE[provider_id],
                               display_name=provider_id)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        concurrency.reset_for_tests()
        self.addCleanup(concurrency.reset_for_tests)
        self.store = JobStore(Path(_SANDBOX.name) / f"{self.id().split('.')[-1]}.sqlite3")
        self.addCleanup(self.store.close)
        self.next_site: list[str] = [B]

    def route(self, model_id, modality, *, exclude_locations=None, **_k):
        return (SimpleNamespace(id=self.next_site.pop(0)), "") if self.next_site else (None, "")

    def run_job(self, fleet: _Fleet, modality: str = "tts") -> dict[str, Any]:
        row, _ = self.store.submit(modality=modality, model_id="model/test", params={"text": "x"},
                                   idempotency_key=self.id(), provider_id=A)
        real = httpx.Client

        def factory(*a: Any, **k: Any) -> httpx.Client:
            k.pop("transport", None)
            return real(transport=httpx.MockTransport(fleet.handler), **k)

        with patch.object(worker_module, "registry", _Registry()), \
                patch.object(worker_module.fleet, "route", self.route), \
                patch.object(hub_module.httpx, "Client", side_effect=factory), \
                patch.object(hub_module, "POLL_INTERVAL_S", 0):
            worker_module.execute_job(row, self.store)
        return self.store.get(row["id"])


class FleetJobTests(_Case):
    def test_an_empty_artifact_is_a_failed_attempt_and_the_job_is_retried_on_another_mac(self) -> None:
        fleet = _Fleet({"a": (b"", "audio/mp4"), "b": (M4A, "audio/mp4")})
        job = self.run_job(fleet)
        self.assertEqual(job["state"], "succeeded", job.get("error_detail"))
        self.assertEqual(job["provider_id"], B)
        self.assertEqual(Path(job["asset_path"]).read_bytes(), M4A)
        self.assertEqual(fleet.saw("a", "POST", "/ack"), [], "the empty file is never acknowledged")
        self.assertEqual(len(fleet.saw("b", "POST", "/ack")), 1)
        attempts = self.store.attempts_of(job["id"])
        self.assertEqual([(a["location"], a["code"]) for a in attempts], [(A, "INVALID_ARTIFACT")])
        self.assertFalse(job["recovery_required"], "a settled bad answer is not an unknown outcome")

    def test_an_m4a_without_its_moov_box_is_refused_too(self) -> None:
        broken = M4A[:20] + b"\x00\x00\x00\x0cmdat" + b"\x00" * 4  # ftyp + mdat, no moov
        job = self.run_job(_Fleet({"a": (broken, "audio/mp4"), "b": (M4A, "audio/mp4")}))
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", B))

    def test_a_valid_artifact_succeeds_at_the_first_mac(self) -> None:
        fleet = _Fleet({"a": (M4A, "audio/mp4"), "b": (M4A, "audio/mp4")})
        job = self.run_job(fleet)
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A))
        self.assertEqual(Path(job["asset_path"]).read_bytes(), M4A)
        self.assertEqual(len(fleet.saw("a", "POST", "/ack")), 1)
        self.assertEqual(self.store.attempts_of(job["id"]), [])

    def test_a_valid_image_succeeds(self) -> None:
        job = self.run_job(_Fleet({"a": (PNG, "image/png"), "b": (PNG, "image/png")}), "image")
        self.assertEqual((job["state"], job["provider_id"]), ("succeeded", A))

    def test_with_nowhere_else_to_go_the_job_fails_and_nothing_is_kept(self) -> None:
        self.next_site = []
        job = self.run_job(_Fleet({"a": (b"", "audio/mp4"), "b": (b"", "audio/mp4")}))
        self.assertEqual((job["state"], job["error_code"]), ("failed", "INVALID_ARTIFACT"))
        self.assertIn("is empty (0 bytes)", job["error_detail"])
        self.assertFalse(job["recovery_required"])
        self.assertFalse(job.get("artifacts"), "no artifact is recorded for the job")


class AdoptAndProbeTests(_Case):
    def test_settle_result_refuses_an_empty_artifact_and_sends_no_receipt(self) -> None:
        row, _ = self.store.submit(modality="tts", model_id="model/test", params={"text": "x"},
                                   idempotency_key="adopt-1", provider_id=A)
        delivered: list[bool] = []
        empty = BackendResult(content=b"", media_type="audio/mp4",
                              deliver=lambda: delivered.append(True) or {"ok": True})
        with self.assertRaises(BackendError) as caught:
            worker_module.settle_result(row["id"], empty, self.store)
        self.assertEqual(caught.exception.code, "INVALID_ARTIFACT")
        self.assertNotEqual(self.store.get(row["id"])["state"], "succeeded")
        self.assertEqual(delivered, [])

    def test_the_probe(self) -> None:
        problem = worker_module.artifact_problem
        wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28
        for content, media in ((M4A, "audio/mp4"), (M4A, "video/mp4"), (wav, "audio/wav"),
                               (PNG, "image/png"), (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/jpeg"),
                               (b"ID3\x04" + b"\x00" * 20, "audio/mpeg"), (b"1\n00:00", "application/x-subrip"),
                               (b"anything", None),
                               # a good file under the wrong label is kept: refusing it would pay twice
                               (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image/png"), (wav, "audio/mpeg"),
                               (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16, "image/jpeg")):
            self.assertIsNone(problem(content, media), media)
        for content, media, why in (
                (b"", "audio/mp4", "empty"), (None, "image/png", "empty"),
                (b"", "application/x-subrip", "empty"),
                (M4A[:20], "audio/mp4", "moov"),
                (M4A[:-2], "audio/mp4", "cut off"),
                (b"<html>error</html>", "image/png", "not a valid image/png"),
                (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8, "image/png", "not a valid"),
                (b"JUNKJUNKJUNK", "audio/wav", "not a valid audio/wav")):
            self.assertIn(why, problem(content, media) or "", (content[:12] if content else content, media))


if __name__ == "__main__":
    unittest.main()
