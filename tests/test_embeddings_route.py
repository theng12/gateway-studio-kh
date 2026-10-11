#!/usr/bin/env python3
"""`POST /v1/embeddings` (0.2.166): free local embeddings on the fleet's Embeddings Studio.

  * the adapter posts the Hub's peer route with the location's credential and returns the vectors
    in input order; a refusal is a location problem (try the next), a 422 is the caller's;
  * the route answers the OpenAI shape, skips a location that refuses or cannot be reached, and
    answers 503 `NO_EMBEDDINGS_ENGINE` when no Mac can run the batch;
  * only fleet models serve here, so this route can never spend;
  * the catalogue publishes the model as a free `studio-hub::<id>` row of kind `embeddings`.

Run with::

    .venv/bin/python tests/test_embeddings_route.py
"""
from __future__ import annotations

import atexit
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-embeddings-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
    "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
    "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
    "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
})

from gateway import api, catalog as catalog_module, fleet  # noqa: E402
from gateway.adapters import serves  # noqa: E402
from gateway.adapters.base import BackendError, BackendRequest  # noqa: E402
from gateway.adapters.studio_hub import StudioHubBackend  # noqa: E402
from gateway.api import GatewayError  # noqa: E402
from gateway.constants import zero_pricing  # noqa: E402
from gateway.providers import discovery, registry  # noqa: E402
from gateway.providers.definitions import DISCOVERY_STUDIO_HUB, KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402

MODEL = "mlx-community/bge-small-en-v1.5-bf16"


class FakeHub:
    """A loopback Hub peer surface: answers `/studio/embeddings/v1/embeddings` as told."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.calls: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.calls.append({"path": self.path, "token": self.headers.get("x-hub-token"),
                                    "authorization": self.headers.get("authorization"),
                                    "body": payload})
                answer = body if body is not None else {
                    "object": "list", "model": MODEL, "usage": {"prompt_tokens": 7, "total_tokens": 7},
                    # Out of order on purpose: the index decides, not the position.
                    "data": [{"object": "embedding", "index": i, "embedding": [float(i), 1.0]}
                             for i in reversed(range(len(payload["input"])))]}
                encoded = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _definition(location: str, base: str) -> ProviderDefinition:
    return ProviderDefinition(
        id=f"{fleet.PROVIDER_PREFIX}{location}", display_name=f"Site {location}", category="multi",
        kind=KIND_STUDIO_HUB, base_url=base,
        modalities=("chat", "image", "tts", "music", "transcription", "video", "embeddings"),
        discovery=DISCOVERY_STUDIO_HUB)


def _request(base: str, texts) -> BackendRequest:
    return BackendRequest(provider_id="studio-hub-a", model_id=MODEL, modality="embeddings",
                          params={"input": texts}, values={"hub_token": "fleet-token"},
                          base_url=base, display_name="Site a")


class AdapterTests(unittest.TestCase):
    def hub(self, **kwargs) -> FakeHub:
        hub = FakeHub(**kwargs)
        self.addCleanup(hub.close)
        return hub

    def test_the_batch_goes_to_the_peer_route_and_comes_back_in_input_order(self) -> None:
        hub = self.hub()
        result = StudioHubBackend().generate(_request(hub.base, ["a", "b", "c"]))
        self.assertEqual(result.payload["vectors"], [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]])
        self.assertEqual(result.payload["usage"]["total_tokens"], 7)
        call = hub.calls[0]
        self.assertEqual(call["path"], "/studio/embeddings/v1/embeddings")
        self.assertEqual(call["body"], {"model": MODEL, "input": ["a", "b", "c"]})
        self.assertIn("fleet-token", (call["token"] or "") + (call["authorization"] or ""))

    def test_a_refusal_is_the_locations_and_a_422_is_the_callers(self) -> None:
        busy = self.hub(status=409, body={"code": "MACHINE_DISABLED", "detail": "paused"})
        with self.assertRaises(BackendError) as caught:
            StudioHubBackend().generate(_request(busy.base, ["a"]))
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "LOCATION_NOT_READY")
        bad = self.hub(status=422, body={"detail": "input[0] is 600 tokens"})
        with self.assertRaises(BackendError) as caught:
            StudioHubBackend().generate(_request(bad.base, ["a"]))
        self.assertFalse(caught.exception.retryable)
        self.assertEqual((caught.exception.code, caught.exception.status), ("INVALID_REQUEST", 422))

    def test_an_answer_with_the_wrong_count_is_not_a_result(self) -> None:
        short = self.hub(body={"data": [{"index": 0, "embedding": [1.0]}]})
        with self.assertRaises(BackendError) as caught:
            StudioHubBackend().generate(_request(short.base, ["a", "b"]))
        self.assertEqual(caught.exception.code, "PROVIDER_FAILED")

    def test_an_unreachable_hub_is_retryable(self) -> None:
        with self.assertRaises(BackendError) as caught:
            StudioHubBackend().generate(_request("http://127.0.0.1:9", ["a"]))
        self.assertEqual(caught.exception.code, "LOCATION_UNREACHABLE")
        self.assertTrue(caught.exception.retryable)


class RouteTests(unittest.TestCase):
    """`api.embeddings` with two fake sites behind the fleet's own routing."""

    def setUp(self) -> None:
        self.hubs: dict[str, FakeHub] = {}

    def sites(self, **hubs: FakeHub | str) -> None:
        bases = {name: hub.base if isinstance(hub, FakeHub) else hub for name, hub in hubs.items()}
        definitions = {name: _definition(name, base) for name, base in bases.items()}
        order = list(definitions)

        def route(model_id, modality, *, routing_key="", exclude_locations=None, **_):
            self.assertEqual((model_id, modality), (MODEL, "embeddings"))
            left = [name for name in order if name not in (exclude_locations or set())]
            return (definitions[left[0]], "chosen") if left else (None, "no location can run it")

        enabled = [(definition, {"model_id": MODEL, "modality": "embeddings"})
                   for definition in definitions.values()]
        for patcher in (patch.object(fleet, "route", route),
                        patch.object(registry.models, "enabled_models",
                                     lambda modality: enabled if modality == "embeddings" else []),
                        patch.object(registry.credentials, "resolved",
                                     lambda provider_id: {"hub_token": "fleet-token"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def hub(self, **kwargs) -> FakeHub:
        hub = FakeHub(**kwargs)
        self.addCleanup(hub.close)
        return hub

    def test_the_openai_shape_comes_back(self) -> None:
        self.sites(a=self.hub())
        answer = api.embeddings({"model": f"studio-hub::{MODEL}", "input": ["x", "y"]})
        self.assertEqual(answer["object"], "list")
        self.assertEqual(answer["model"], f"studio-hub::{MODEL}")
        self.assertEqual([row["index"] for row in answer["data"]], [0, 1])
        self.assertEqual(answer["data"][1]["embedding"], [1.0, 1.0])
        self.assertEqual(answer["usage"], {"prompt_tokens": 7, "total_tokens": 7})
        # A bare id names the same fleet model, and one string is one input.
        self.assertEqual(len(api.embeddings({"model": MODEL, "input": "x"})["data"]), 1)

    def test_a_refusing_or_unreachable_site_is_skipped_for_the_next(self) -> None:
        busy = self.hub(status=409, body={"code": "MODEL_NOT_READY", "detail": "not installed"})
        good = self.hub()
        self.sites(a="http://127.0.0.1:9", b=busy, c=good)
        answer = api.embeddings({"model": MODEL, "input": ["x"]})
        self.assertEqual(len(answer["data"]), 1)
        self.assertEqual((len(busy.calls), len(good.calls)), (1, 1))

    def test_no_mac_able_to_run_it_is_a_503_that_says_why(self) -> None:
        busy = self.hub(status=409, body={"code": "MACHINE_DISABLED", "detail": "paused"})
        self.sites(a=busy)
        with self.assertRaises(GatewayError) as caught:
            api.embeddings({"model": MODEL, "input": ["x"]})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "NO_EMBEDDINGS_ENGINE"))
        self.assertIn("MACHINE_DISABLED", caught.exception.detail)
        self.sites()
        with self.assertRaises(GatewayError) as caught:
            api.embeddings({"model": MODEL, "input": ["x"]})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "NO_EMBEDDINGS_ENGINE"))
        self.assertIn("install it", caught.exception.detail)

    def test_bad_requests_and_paid_models_are_refused_before_any_work(self) -> None:
        hub = self.hub()
        self.sites(a=hub)
        for body, status in (({"model": MODEL, "input": []}, 422),
                             ({"model": MODEL, "input": ["ok", " "]}, 422),
                             ({"model": MODEL, "input": ["x"] * 2049}, 422),
                             ({"model": MODEL, "input": ["x"], "dimensions": 8}, 422),
                             ({"model": MODEL, "input": ["x"], "encoding_format": "base64"}, 422),
                             ({"model": "openrouter::openai/text-embedding-3-small", "input": ["x"]}, 404)):
            with self.subTest(body=str(body)[:70]):
                with self.assertRaises(GatewayError) as caught:
                    api.embeddings(body)
                self.assertEqual(caught.exception.status, status)
        self.assertEqual(hub.calls, [])

    def test_the_route_is_mounted_with_the_api_key(self) -> None:
        routes = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", None) or ()))): r
                  for r in api.app.routes}
        route = routes[("/v1/embeddings", ("POST",))]
        self.assertIn(api.require_auth, [d.call for d in route.dependant.dependencies])
        self.assertIn(("/v1/embeddings/models", ("GET",)), routes)


class CatalogTests(unittest.TestCase):
    def test_embeddings_is_a_studio_hub_lane_and_is_free(self) -> None:
        definition = _definition("local", "http://127.0.0.1:1")
        self.assertTrue(serves(definition, "embeddings"))
        self.assertIn("embeddings", catalog_module.MODALITIES)
        self.assertEqual(discovery._package_stated_modality({"modality": "embeddings"}), "embeddings")
        self.assertEqual(zero_pricing("embeddings")["input_credits_per_million_tokens"], 0)

    def test_the_row_is_a_free_fleet_row_of_kind_embeddings(self) -> None:
        definition = _definition("local", "http://127.0.0.1:1")
        model = {"model_id": MODEL, "display_name": "BGE small en v1.5 MLX bf16", "modality": "embeddings",
                 "raw": {"lane_contract": {
                     "operation": "text.embedding",
                     "parameters": [{"name": "input", "max_items": 2048, "max_tokens_per_item": 511}],
                     "artifact": {"dimensions": 384, "normalized": True}}}}
        with patch.object(fleet, "usable_location_ids", lambda: {"local"}):
            row = catalog_module.catalog._story_row("embeddings", definition, model, usable={"local"})
        self.assertEqual(row["id"], f"studio-hub::{MODEL}")
        self.assertEqual((row["kind"], row["capabilities"], row["operation"]),
                         ("embeddings", ["embeddings"], "text.embedding"))
        self.assertEqual((row["dimensions"], row["normalized"], row["max_inputs"], row["max_input_tokens"]),
                         (384, True, 2048, 511))
        self.assertIs(row["chargeable"], False)
        self.assertEqual(row["price_source"], "local")
        self.assertFalse(row["locked"])


if __name__ == "__main__":
    unittest.main()
