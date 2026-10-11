#!/usr/bin/env python3
"""`POST /v1/vision/analyze` (0.2.167): free local image analysis on the fleet's vision engine.

  * only fleet models serve, so the route can never spend; bad input is a 422 before any work;
  * the model is the named one, else per task the switched-on row publishing it (default first);
  * the Mac is the one that made the image, then one at the caller's site, then any, and only a Mac
    with every needed model ready; a refusing Mac is skipped (at most 3 tried), a Hub 422 is not;
  * the controller's own Mac is called on the peer route, a joined Mac through the controller's relay;
  * `/v1/vision/models` publishes free `studio-hub::<id>` rows of kind `vision` with their tasks.

Run with::

    .venv/bin/python tests/test_vision_route.py
"""
from __future__ import annotations

import atexit
import base64
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

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-vision-"))
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
from gateway.api import GatewayError  # noqa: E402
from gateway.constants import zero_pricing  # noqa: E402
from gateway.providers import discovery, registry  # noqa: E402
from gateway.providers.definitions import DISCOVERY_STUDIO_HUB, KIND_STUDIO_HUB, ProviderDefinition  # noqa: E402

DEPTH = "depth-anything/Depth-Anything-V2-Small-hf"
FLORENCE = "florence-community/Florence-2-base-ft"
LARGE = "florence-community/Florence-2-large-ft"
TASKS = {DEPTH: ["depth"], FLORENCE: ["subject", "points", "segments"], LARGE: ["subject", "points", "segments"]}
IMAGE = {"b64": base64.b64encode(b"\xff\xd8\xff fake jpeg").decode(), "mime": "image/jpeg"}


class FakeHub:
    """One location's controller: answers the vision peer route and the node relay as told."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.calls: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.calls.append({"path": self.path, "body": payload})
                answer = body if body is not None else {
                    "models": {task: (DEPTH if task == "depth" else FLORENCE) for task in payload["tasks"]},
                    "revisions": {model: "abc123" for model in payload["model"]},
                    "image": {"w": 768, "h": 432}, "timings_ms": {"total": 900}}
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
        modalities=("chat", "image", "tts", "music", "transcription", "video", "embeddings", "vision"),
        discovery=DISCOVERY_STUDIO_HUB)


def _location(location: str, url: str, macs: dict[str, list[str]], *, addresses: dict | None = None,
              paused: tuple = (), cache_state: str = "cached") -> dict:
    """A fleet-snapshot location row: `macs` = machine -> the vision models installed there."""
    return {
        "id": location, "url": url, "reachable": True, "role": "controller", "workers": [],
        "machines": [{"machine": machine, "reachable": True} for machine in macs],
        "nodes": [{"id": machine, "address": (addresses or {}).get(machine, f"http://100.0.0.{i}:47873"),
                   "paused": machine in paused} for i, machine in enumerate(macs) if machine != "local"],
        "local_models": [{"model": model, "model_id": model, "machine": machine, "modality": "vision",
                          "memory_eligible": True, "runtime_ready": True, "cache_state": cache_state}
                         for machine, models in macs.items() for model in models],
    }


def _caller(host: str = "100.50.50.50") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host))


class VisionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hubs: dict[str, FakeHub] = {}

    def hub(self, **kwargs) -> FakeHub:
        hub = FakeHub(**kwargs)
        self.addCleanup(hub.close)
        return hub

    def fleet(self, sites: dict[str, tuple[FakeHub | str, dict]], *, models=(DEPTH, FLORENCE),
              defaults=(FLORENCE,), job: dict | None = None) -> None:
        """`sites`: location -> (hub or base url, snapshot row). Every site switches on `models`."""
        definitions = {name: _definition(name, hub.base if isinstance(hub, FakeHub) else hub)
                       for name, (hub, _) in sites.items()}
        enabled = [(definition, {"model_id": model, "modality": "vision",
                                 "raw": {"lane_contract": {"tasks": TASKS[model]}, "default": model in defaults}})
                   for definition in definitions.values() for model in models]
        snapshot = {"locations": [row for _, row in sites.values()]}
        for patcher in (patch.object(registry.models, "enabled_models",
                                     lambda modality: enabled if modality == "vision" else []),
                        patch.object(registry.credentials, "resolved",
                                     lambda provider_id: {"hub_token": "fleet-token"}),
                        patch.object(fleet, "snapshot", lambda *a, **k: snapshot),
                        patch.object(api.store, "get", lambda job_id: job if job and job["id"] == job_id else None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def analyze(self, body: dict, host: str = "100.50.50.50") -> dict:
        return api.vision_analyze(body, _caller(host))

    # -- refused before any work --------------------------------------------------------------

    def test_bad_requests_and_paid_models_are_refused_before_any_work(self) -> None:
        hub = self.hub()
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH, FLORENCE]}))})
        big = {"b64": base64.b64encode(b"\0" * (12 * 1024 * 1024 + 1)).decode()}
        for body, status, words in (
                ({"tasks": ["depth"], "image": IMAGE, "model": "openrouter::openai/gpt-5"}, 404, "not a vision model"),
                ({"tasks": ["depth"], "image": IMAGE, "colour": "red"}, 422, "colour: not supported"),
                ({"tasks": [], "image": IMAGE}, 422, "tasks"),
                ({"tasks": ["depth", "depth"], "image": IMAGE}, 422, "tasks"),
                ({"tasks": ["caption"], "image": IMAGE}, 422, "tasks"),
                ({"tasks": ["depth"]}, 422, "image"),
                ({"tasks": ["depth"], "image": {"b64": "x", "url": "https://a/b.jpg"}}, 422, "image"),
                ({"tasks": ["depth"], "image": {"url": "file:///etc/passwd"}}, 422, "http or https"),
                ({"tasks": ["depth"], "image": {"b64": "not base64!"}}, 422, "base64"),
                ({"tasks": ["depth"], "image": {**IMAGE, "mime": "image/gif"}}, 422, "mime"),
                ({"tasks": ["depth"], "image": big}, 422, "downscaled ~768 px JPEG"),
                ({"tasks": ["subject"], "image": IMAGE, "text": "x" * 501}, 422, "text"),
                ({"tasks": ["segments"], "image": IMAGE, "regions": ["sky"] * 9}, 422, "regions"),
                ({"tasks": ["segments"], "image": IMAGE, "regions": ["x" * 41]}, 422, "regions"),
                ({"tasks": ["depth"], "image": IMAGE, "priority": "urgent"}, 422, "priority"),
                ({"tasks": ["depth"], "image": IMAGE, "model": f"studio-hub::{FLORENCE}"}, 422, "depth"),
                ({"tasks": ["depth"], "image": IMAGE, "model": "studio-hub::nobody/unknown"}, 422, "not a vision model")):
            with self.subTest(body=str(body)[:90]):
                with self.assertRaises(GatewayError) as caught:
                    self.analyze(body)
                self.assertEqual(caught.exception.status, status)
                self.assertIn(words, caught.exception.detail)
        self.assertEqual(hub.calls, [])

    def test_a_named_model_missing_a_task_names_what_each_model_does(self) -> None:
        hub = self.hub()
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH, FLORENCE]}))})
        with self.assertRaises(GatewayError) as caught:
            self.analyze({"tasks": ["subject"], "image": IMAGE, "model": DEPTH})
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "INVALID_REQUEST"))
        self.assertIn(f"studio-hub::{DEPTH} does depth", caught.exception.detail)
        self.assertIn(f"studio-hub::{FLORENCE} does subject, points, segments", caught.exception.detail)

    # -- the answer ---------------------------------------------------------------------------

    def test_the_hubs_answer_comes_back_with_fleet_ids_and_where_it_ran(self) -> None:
        hub = self.hub()
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH, FLORENCE, LARGE]}))},
                   models=(DEPTH, LARGE, FLORENCE))
        answer = self.analyze({"tasks": ["depth", "subject"], "image": IMAGE, "text": "the lighthouse",
                               "regions": ["sky", "water"], "priority": "background"})
        self.assertEqual(answer["models"], {"depth": f"studio-hub::{DEPTH}", "subject": f"studio-hub::{FLORENCE}"})
        self.assertEqual(set(answer["revisions"]), {f"studio-hub::{DEPTH}", f"studio-hub::{FLORENCE}"})
        self.assertEqual(answer["image"], {"w": 768, "h": 432})
        self.assertEqual(answer["served_by"], {"location_id": "a", "machine": "local", "why": "any"})
        self.assertEqual(answer["attempts"], [])
        self.assertGreater(answer["network"]["request_bytes"], 0)
        self.assertGreater(answer["network"]["response_bytes"], 0)
        # The default row is chosen over the non-default one; bare ids go to the Hub as a list.
        self.assertEqual(hub.calls[0]["body"], {"tasks": ["depth", "subject"], "image": IMAGE,
                                                "text": "the lighthouse", "regions": ["sky", "water"],
                                                "model": [DEPTH, FLORENCE]})
        # A named model is used as named, by its fleet id or bare.
        self.analyze({"tasks": ["subject"], "image": IMAGE, "model": [f"studio-hub::{LARGE}"]})
        self.assertEqual(hub.calls[-1]["body"]["model"], [LARGE])

    def test_the_controllers_own_mac_uses_the_peer_route_and_a_node_the_relay(self) -> None:
        hub = self.hub()
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH], "mac-2": [DEPTH]}))},
                   job={"id": "job-1", "provider_id": "studio-hub-a",
                        "result_json": json.dumps({"payload": {"hub_machine": "mac-2"}})})
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE, "source_job_id": "job-1"})
        self.assertEqual(answer["served_by"], {"location_id": "a", "machine": "mac-2", "why": "source-mac"})
        self.assertEqual(hub.calls[-1]["path"], "/api/hub/fleet/nodes/mac-2/vision/analyze")
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH], "mac-2": []}))})
        self.analyze({"tasks": ["depth"], "image": IMAGE})
        self.assertEqual(hub.calls[-1]["path"], "/studio/vision/v1/vision/analyze")

    # -- which Mac ----------------------------------------------------------------------------

    def test_the_source_mac_then_the_callers_site_then_any(self) -> None:
        far, near, source = self.hub(), self.hub(), self.hub()
        sites = {"far": (far, _location("far", far.base, {"local": [DEPTH]})),
                 "near": (near, _location("near", near.base, {"local": [DEPTH], "shop-mac": [DEPTH]},
                                          addresses={"shop-mac": "http://100.50.50.50:47873"})),
                 "src": (source, _location("src", source.base, {"local": [DEPTH], "m7": [DEPTH]}))}
        job = {"id": "job-9", "provider_id": "studio-hub-src",
               "result_json": json.dumps({"payload": {"hub_machine": "m7"}})}
        self.fleet(sites, job=job)
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE, "source_job_id": "job-9"})
        self.assertEqual(answer["served_by"], {"location_id": "src", "machine": "m7", "why": "source-mac"})
        # No source job: the caller's address is one of `near`'s Macs, so `near` serves.
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE})
        self.assertEqual((answer["served_by"]["location_id"], answer["served_by"]["why"]), ("near", "caller-site"))
        # `site` overrides the guess; a loopback caller matches the location at 127.0.0.1.
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE, "site": "far"})
        self.assertEqual(answer["served_by"], {"location_id": "far", "machine": "local", "why": "caller-site"})
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE}, host="::1")
        self.assertEqual(answer["served_by"]["why"], "caller-site")
        # The source Mac without the model is passed over for the next tier.
        sites["src"] = (source, _location("src", source.base, {"local": [DEPTH], "m7": []}))
        self.fleet(sites, job=job)
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE, "source_job_id": "job-9"})
        self.assertEqual(answer["served_by"]["why"], "caller-site")

    def test_only_a_mac_with_every_model_ready_is_asked(self) -> None:
        hub = self.hub()
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH], "m2": [DEPTH, FLORENCE],
                                                         "m3": [DEPTH, FLORENCE]}, paused=("m3",)))})
        answer = self.analyze({"tasks": ["depth", "points"], "image": IMAGE})
        self.assertEqual(answer["served_by"]["machine"], "m2")
        self.fleet({"a": (hub, _location("a", hub.base, {"local": [DEPTH]}, cache_state="not_installed"))})
        with self.assertRaises(GatewayError) as caught:
            self.analyze({"tasks": ["depth"], "image": IMAGE})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "NO_VISION_ENGINE"))
        self.assertEqual(caught.exception.headers, {"Retry-After": "30"})
        self.assertIn("installed and ready", caught.exception.detail)
        self.assertEqual(len(hub.calls), 1)

    def test_a_refusing_mac_is_skipped_and_at_most_three_are_tried(self) -> None:
        busy = self.hub(status=503, body={"code": "MODEL_NOT_READY", "detail": "still loading"})
        good = self.hub()
        self.fleet({"a": (busy, _location("a", busy.base, {"local": [DEPTH]})),
                    "b": (good, _location("b", good.base, {"local": [DEPTH]}))}, )
        answer = self.analyze({"tasks": ["depth"], "image": IMAGE, "site": "a"})
        self.assertEqual(answer["served_by"]["location_id"], "b")
        self.assertEqual(len(answer["attempts"]), 1)
        self.assertIn("MODEL_NOT_READY", answer["attempts"][0])
        old = self.hub(status=404, body={"detail": "Not Found"})
        self.fleet({"a": (old, _location("a", old.base, {"local": [DEPTH], "n1": [DEPTH], "n2": [DEPTH]})),
                    "b": (busy, _location("b", busy.base, {"local": [DEPTH]})),
                    "c": ("http://127.0.0.1:9", _location("c", "http://127.0.0.1:9", {"local": [DEPTH]}))})
        busy.calls.clear()
        with self.assertRaises(GatewayError) as caught:
            self.analyze({"tasks": ["depth"], "image": IMAGE})
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "NO_VISION_ENGINE"))
        self.assertEqual(caught.exception.headers, {"Retry-After": "30"})
        self.assertEqual(len(old.calls) + len(busy.calls), 3 - caught.exception.detail.count("could not be reached"))
        self.assertEqual(caught.exception.detail.count(" · "), 2)

    def test_a_hub_422_is_the_callers_and_is_not_retried(self) -> None:
        bad, good = self.hub(status=422, body={"detail": "image could not be decoded"}), self.hub()
        self.fleet({"a": (bad, _location("a", bad.base, {"local": [DEPTH]})),
                    "b": (good, _location("b", good.base, {"local": [DEPTH]}))})
        with self.assertRaises(GatewayError) as caught:
            self.analyze({"tasks": ["depth"], "image": IMAGE, "site": "a"})
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "INVALID_REQUEST"))
        self.assertIn("could not be decoded", caught.exception.detail)
        self.assertEqual((len(bad.calls), len(good.calls)), (1, 0))

    def test_nothing_installed_anywhere_says_install_it(self) -> None:
        self.fleet({}, models=())
        for body in ({"tasks": ["depth"], "image": IMAGE}, {"tasks": ["depth"], "image": IMAGE, "model": DEPTH}):
            with self.assertRaises(GatewayError) as caught:
                self.analyze(body)
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "NO_VISION_ENGINE"))
            self.assertIn("0.6.166", caught.exception.detail)

    def test_the_routes_are_mounted_with_the_api_key(self) -> None:
        routes = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", None) or ()))): r
                  for r in api.app.routes}
        self.assertIn(api.require_auth, [d.call for d in routes[("/v1/vision/analyze", ("POST",))].dependant.dependencies])
        self.assertIn(("/v1/vision/models", ("GET",)), routes)


class CatalogTests(unittest.TestCase):
    def test_vision_is_a_free_studio_hub_lane(self) -> None:
        self.assertTrue(serves(_definition("local", "http://127.0.0.1:1"), "vision"))
        self.assertIn("vision", catalog_module.MODALITIES)
        self.assertEqual(discovery._package_stated_modality({"modality": "vision"}), "vision")
        self.assertEqual(zero_pricing("vision")["credits_per_request"], 0)

    def test_the_row_is_a_free_fleet_row_of_kind_vision_with_its_tasks(self) -> None:
        definition = _definition("local", "http://127.0.0.1:1")
        model = {"model_id": FLORENCE, "display_name": "Florence 2 base ft", "modality": "vision",
                 "raw": {"default": True, "lane_contract": {
                     "schema": "studiohub.lane-contract.v1", "modality": "vision", "operation": "image.analysis",
                     "tasks": TASKS[FLORENCE], "artifact": {"media_type": "application/json"}}}}
        with patch.object(fleet, "usable_location_ids", lambda: {"local"}):
            row = catalog_module.catalog._story_row("vision", definition, model, usable={"local"})
        self.assertEqual(row["id"], f"studio-hub::{FLORENCE}")
        self.assertEqual((row["kind"], row["capabilities"], row["operation"], row["tasks"], row["default"]),
                         ("vision", ["vision"], "image.analysis", ["subject", "points", "segments"], True))
        self.assertIs(row["chargeable"], False)
        self.assertEqual(row["price_source"], "local")
        unstated = catalog_module._vision_row(DEPTH, definition, {"model_id": DEPTH, "raw": {}})
        self.assertEqual((unstated["tasks"], unstated["default"]), (None, False))


if __name__ == "__main__":
    unittest.main()
