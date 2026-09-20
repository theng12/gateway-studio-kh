#!/usr/bin/env python3
"""Adapter-level proof that accepted async work records its upstream identity."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import httpx


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-adapter-checkpoints-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway.adapters import bfl as bfl_module  # noqa: E402
from gateway.adapters import fal as fal_module  # noqa: E402
from gateway.adapters import kie as kie_module  # noqa: E402
from gateway.adapters import minimax as minimax_module  # noqa: E402
from gateway.adapters import studio_hub as studio_hub_module  # noqa: E402
from gateway.adapters import xai as xai_module  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402


@dataclass
class _Scenario:
    run: Callable[[Callable[[str], None] | None], None]
    events: list[str]
    submits: list[str]


class AdapterCheckpointTests(unittest.TestCase):
    @staticmethod
    def _request(
        *,
        provider_id: str,
        model_id: str,
        modality: str,
        params: dict | None = None,
        checkpoint: Callable[[str], None] | None = None,
        values: dict[str, str] | None = None,
        base_url: str = "https://upstream.test",
    ) -> BackendRequest:
        return BackendRequest(
            provider_id=provider_id,
            model_id=model_id,
            modality=modality,
            params=params or {},
            values=values or {"api_key": "test-key"},
            base_url=base_url,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _client_runner(module, handler, operation) -> None:
        real_client = httpx.Client
        client = real_client(transport=httpx.MockTransport(handler))
        try:
            with patch.object(module.httpx, "Client", return_value=client):
                operation()
        finally:
            client.close()

    def _fal_scenario(self, modality: str) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                return httpx.Response(
                    200,
                    json={
                        "request_id": "fal-request-1",
                        "status_url": "https://queue.fal.run/status?token=secret",
                        "response_url": "https://queue.fal.run/result?token=secret",
                    },
                )
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        params = {"prompt": "test"}
        if modality == "tts":
            params = {"text": "test"}
        if modality == "video":
            params["duration_seconds"] = 5

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="fal-test",
                model_id="model/test",
                modality=modality,
                params=params,
                checkpoint=checkpoint,
            )
            self._client_runner(
                module=fal_module,
                handler=handler,
                operation=lambda: fal_module.FalBackend().generate(request),
            )

        return _Scenario(run=run, events=events, submits=submits)

    def _bfl_scenario(self, *, include_id: bool = True) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                payload = {
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=bfl-1&token=secret"
                }
                if include_id:
                    payload["id"] = "bfl-1"
                return httpx.Response(200, json=payload)
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="bfl-test",
                model_id="flux-test",
                modality="image",
                params={"prompt": "test"},
                checkpoint=checkpoint,
            )
            self._client_runner(module=bfl_module, handler=handler, operation=lambda: bfl_module.generate(request))

        return _Scenario(run=run, events=events, submits=submits)

    def _kie_scenario(self, modality: str) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                return httpx.Response(200, json={"code": 200, "data": {"taskId": "kie-task-1"}})
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        params = {"prompt": "test"}
        if modality == "music":
            params = {"lyrics": "test"}

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="kie-test",
                model_id="V6" if modality == "music" else "model/test",
                modality=modality,
                params=params,
                checkpoint=checkpoint,
            )
            self._client_runner(
                module=kie_module,
                handler=handler,
                operation=lambda: kie_module.KieBackend().generate(request),
            )

        return _Scenario(run=run, events=events, submits=submits)

    def _minimax_scenario(self) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                return httpx.Response(
                    200,
                    json={"task_id": "minimax-task-1", "base_resp": {"status_code": 0}},
                )
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="minimax-test",
                model_id="video-test",
                modality="video",
                params={"prompt": "test"},
                checkpoint=checkpoint,
            )
            real_client = httpx.Client
            client = real_client(transport=httpx.MockTransport(handler))
            try:
                with patch.object(minimax_module.httpx, "post", side_effect=client.post), patch.object(
                    minimax_module.httpx, "get", side_effect=client.get
                ), patch.object(minimax_module.time, "sleep", return_value=None):
                    minimax_module._video(request)
            finally:
                client.close()

        return _Scenario(run=run, events=events, submits=submits)

    def _xai_scenario(self) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                return httpx.Response(200, json={"request_id": "xai-request-1"})
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="xai-test",
                model_id="video-test",
                modality="video",
                params={"prompt": "test"},
                checkpoint=checkpoint,
            )
            self._client_runner(
                module=xai_module,
                handler=handler,
                operation=lambda: xai_module._generate_video(request),
            )

        return _Scenario(run=run, events=events, submits=submits)

    def _studio_hub_scenario(self, modality: str) -> _Scenario:
        events: list[str] = []
        submits: list[str] = []
        upload = _TEMP_ROOT / f"{modality}.wav"
        upload.write_bytes(b"test audio")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                events.append("submit")
                submits.append(request.url.path)
                return httpx.Response(200, json={"batch_id": "hub-batch-1"})
            events.append("poll")
            return httpx.Response(500, json={"error": "poll failed"})

        params = {"prompt": "test"} if modality == "image" else {"text": "test"}
        if modality == "transcription":
            params = {
                "_upload_path": str(upload),
                "_upload_name": "test.wav",
                "_upload_type": "audio/wav",
            }

        def run(checkpoint: Callable[[str], None] | None) -> None:
            request = self._request(
                provider_id="studio-hub-test",
                model_id="model/test",
                modality=modality,
                params=params,
                checkpoint=checkpoint,
                values={"base_url": "https://hub.test", "hub_token": "test-token"},
                base_url="https://hub.test",
            )
            self._client_runner(
                module=studio_hub_module,
                handler=handler,
                operation=lambda: studio_hub_module.StudioHubBackend().generate(request),
            )

        return _Scenario(run=run, events=events, submits=submits)

    def _assert_checkpointed_failure(self, factory, expected_id: str) -> None:
        scenario = factory()
        checkpointed: list[str] = []

        with self.assertRaises(BackendError) as raised:
            scenario.run(lambda upstream_id: (scenario.events.append("checkpoint"), checkpointed.append(upstream_id)))
        scenario.events.append("failure")

        self.assertEqual(raised.exception.code, "PROVIDER_FAILED")
        self.assertEqual(checkpointed, [expected_id])
        self.assertEqual(scenario.events, ["submit", "checkpoint", "poll", "failure"])
        self.assertEqual(len(scenario.submits), 1)
        self.assertNotIn("http", checkpointed[0])
        self.assertNotIn("?", checkpointed[0])

    def _assert_without_checkpoint_unchanged(self, factory) -> None:
        scenario = factory()
        with self.assertRaises(BackendError) as raised:
            scenario.run(None)
        scenario.events.append("failure")

        self.assertEqual(raised.exception.code, "PROVIDER_FAILED")
        self.assertEqual(scenario.events, ["submit", "poll", "failure"])
        self.assertEqual(len(scenario.submits), 1)

    def test_async_adapters_checkpoint_safe_ids_before_poll_failure(self) -> None:
        cases = [
            ("fal-image", lambda: self._fal_scenario("image"), "fal-request-1"),
            ("fal-video", lambda: self._fal_scenario("video"), "fal-request-1"),
            ("fal-tts", lambda: self._fal_scenario("tts"), "fal-request-1"),
            ("bfl-image", lambda: self._bfl_scenario(), "bfl-1"),
            ("kie-image", lambda: self._kie_scenario("image"), "kie-task-1"),
            ("kie-video", lambda: self._kie_scenario("video"), "kie-task-1"),
            ("kie-music", lambda: self._kie_scenario("music"), "kie-task-1"),
            ("minimax-video", self._minimax_scenario, "minimax-task-1"),
            ("xai-video", self._xai_scenario, "xai-request-1"),
            ("hub-image", lambda: self._studio_hub_scenario("image"), "hub-batch-1"),
            ("hub-tts", lambda: self._studio_hub_scenario("tts"), "hub-batch-1"),
            (
                "hub-transcription",
                lambda: self._studio_hub_scenario("transcription"),
                "hub-batch-1",
            ),
        ]
        for name, factory, expected_id in cases:
            with self.subTest(name=name):
                self._assert_checkpointed_failure(factory, expected_id)

    def test_async_adapters_without_checkpoint_keep_existing_failure_behavior(self) -> None:
        factories = [
            lambda: self._fal_scenario("image"),
            self._bfl_scenario,
            lambda: self._kie_scenario("music"),
            self._minimax_scenario,
            self._xai_scenario,
            lambda: self._studio_hub_scenario("tts"),
        ]
        for factory in factories:
            with self.subTest(factory=factory):
                self._assert_without_checkpoint_unchanged(factory)

    def test_bfl_without_safe_id_never_checkpoints_polling_url(self) -> None:
        scenario = self._bfl_scenario(include_id=False)
        checkpointed: list[str] = []

        with self.assertRaises(BackendError):
            scenario.run(checkpointed.append)
        scenario.events.append("failure")

        self.assertEqual(checkpointed, [])
        self.assertEqual(scenario.events, ["submit", "poll", "failure"])
        self.assertEqual(len(scenario.submits), 1)


if __name__ == "__main__":
    unittest.main()
