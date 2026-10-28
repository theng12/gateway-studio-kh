#!/usr/bin/env python3
"""A TTS delivery/emotion instruction: published only where the Hub says so, and never dropped.

Owner, 2026-10-29. Studio Hub adds a per-request `delivery_instruction` (text, at most 500
characters) to `/api/generate/txt2speech` and `/api/generate/txt2speech/reference`; Breeze TTS 2
reads it as the engine's own `instruct` beside a reference clone. The Gateway's three jobs:

* **publish the control only for models whose Hub publishes the capability** — read from the
  model's own record, never a model name (§1);
* **forward the Hub's per-model guidance unchanged** inside that control, and show nothing when
  the Hub published none;
* **refuse** a non-empty instruction on a model that does not take one, with a plain 422 —
  silently dropping it would deliver the plain reading under a sentence claiming otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-delivery-")
BASE = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(BASE), "GATEWAY_DATA": str(BASE / "data"),
    "GATEWAY_ARTIFACTS": str(BASE / "artifacts"), "GATEWAY_DB": str(BASE / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(BASE / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(BASE / "models.json"),
    "GATEWAY_ENV_FILE": str(BASE / ".env"), "GATEWAY_FLEET": str(BASE / "fleet.json"),
    "GATEWAY_VOICES": str(BASE / "voices.json"), "GATEWAY_VOICES_OUT": str(BASE / "voices.json"),
    "STUDIOHUB_READY_FILE": str(BASE / "no-hub.json"), "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1", "GATEWAY_API_KEY": "delivery-key",
})

import httpx
from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, catalog  # noqa: E402
from gateway.providers import definitions as defs  # noqa: E402
from gateway.runtime import store  # noqa: E402

HUB_DEFINITION = SimpleNamespace(id="studio-hub-test", display_name="Studio Hub",
                                 kind=defs.KIND_STUDIO_HUB)

#: **The Hub's own guidance shape** (`components/hub/backend/capabilities.py`
#: `_delivery_instruction`), unchanged in every field — this is what the Gateway must forward.
BREEZE_GUIDANCE = {
    "kind": "free_text",
    "engine_form": "instruct",
    "max_characters": 500,
    "examples": ["Speak slowly and sadly, voice close to breaking.",
                 "Cheerful and brisk, like good news."],
    "language_rule": "Write the instruction in English, whatever the script's language.",
    "inline_events": [],
    "directed_cfg_scale": 4.0,
    "directed_cfg_parameter": "breeze_cfg_scale",
    "source_url": "https://huggingface.co/mlx-community/Breeze-TTS-2-mlx-4bit",
    "note": "Voice Direction, from the model card.",
}
VOXCPM_GUIDANCE = {
    "kind": "free_text",
    "engine_form": "text_prefix",
    "max_characters": 500,
    "examples": ["(slightly faster, cheerful tone)"],
    "language_rule": None,
    "inline_events": [{"syntax": "[laugh]", "event": "a short laugh", "where": "text"}],
    "directed_cfg_scale": None,
    "directed_cfg_parameter": None,
    "source_url": "https://huggingface.co/mlx-community/VoxCPM2-4bit",
    "note": "Controllable Cloning: a (style) prefix in front of the text.",
}


def hub_model(capabilities=("tts", "voice-cloning", "delivery-instruction"),
              guidance=BREEZE_GUIDANCE) -> dict:
    """One Studio Hub model's raw record: catalogue capabilities + the snapshot's controls."""
    raw = {"capabilities": list(capabilities)}
    if guidance is not None:
        raw["hub_controls"] = {
            "capabilities": ["voice-cloning", "delivery-instruction"],
            "delivery_instruction": dict(guidance),
        }
    return raw


def tts_row(raw: dict, model_id: str = "breeze-tts-2") -> dict:
    return catalog._tts_row(
        model_id, HUB_DEFINITION,
        {"model_id": model_id, "display_name": "Breeze TTS 2", "raw": raw},
    )


class TheControl(unittest.TestCase):
    def test_it_is_published_only_where_the_hub_says_so(self) -> None:
        with_capability = tts_row(hub_model())
        without = tts_row({"capabilities": ["tts", "voice-cloning"]})
        self.assertIn("delivery_instruction",
                      [c["name"] for c in with_capability.get("controls") or []])
        self.assertNotIn("delivery_instruction",
                         [c["name"] for c in (without.get("controls") or [])])

    def test_either_published_place_is_enough(self) -> None:
        # The catalogue token, the snapshot token, or both: the Hub may publish it in either,
        # and the Gateway derives nothing from a model id or a family name.
        for raw in ({"capabilities": ["tts", "delivery-instruction"]},
                    {"capabilities": ["tts"], "hub_controls": {
                        "capabilities": ["delivery-instruction"]}},
                    hub_model()):
            with self.subTest(raw=sorted(raw)):
                row = tts_row(raw)
                self.assertIn("delivery_instruction",
                              [c["name"] for c in row.get("controls") or []])

    def test_breeze_publishes_the_hubs_guidance_unchanged(self) -> None:
        row = tts_row(hub_model())
        control = next(c for c in row["controls"] if c["name"] == "delivery_instruction")
        self.assertEqual(control["guidance"], BREEZE_GUIDANCE, "byte for byte, not reshaped")
        self.assertEqual(control["guidance"]["engine_form"], "instruct")
        self.assertEqual(control["guidance"]["directed_cfg_scale"], 4.0)
        self.assertEqual(control["max_length"], 500)

    def test_voxcpm2_publishes_its_own_form(self) -> None:
        # The other engine form: a `(style)` prefix, no directed cfg, and inline vocal events.
        row = tts_row(hub_model(guidance=VOXCPM_GUIDANCE), model_id="voxcpm2-4bit")
        control = next(c for c in row["controls"] if c["name"] == "delivery_instruction")
        self.assertEqual(control["guidance"], VOXCPM_GUIDANCE)
        self.assertEqual(control["guidance"]["engine_form"], "text_prefix")
        self.assertIsNone(control["guidance"]["directed_cfg_scale"])
        self.assertEqual(control["guidance"]["inline_events"][0]["syntax"], "[laugh]")

    def test_no_published_guidance_means_no_guidance_field(self) -> None:
        row = tts_row({"capabilities": ["tts", "delivery-instruction"]})
        control = next(c for c in row["controls"] if c["name"] == "delivery_instruction")
        self.assertNotIn("guidance", control, "nothing is invented for a Hub that said nothing")

    def test_a_model_with_no_token_publishes_no_control(self) -> None:
        # Qwen3-TTS Base clones and samples and takes no direction; its row must say nothing.
        row = tts_row({"capabilities": ["tts", "voice-cloning"]}, model_id="qwen3-base")
        self.assertNotIn("delivery_instruction",
                         [c["name"] for c in row.get("controls") or []])


class DiscoveryCarriesTheControls(unittest.TestCase):
    """The guidance is read from `/api/hub/capabilities` live, at discovery (§1)."""

    SNAPSHOT = {"models": [
        {"model_id": "mlx-community/Breeze-TTS-2-mlx-4bit",
         "repo": "mlx-community/Breeze-TTS-2-mlx-4bit", "hub_modality": "voice",
         "controls": {"capabilities": ["voice-cloning", "delivery-instruction"],
                      "delivery_instruction": BREEZE_GUIDANCE}},
    ]}

    def test_the_snapshot_is_indexed_by_model_id_and_repo(self) -> None:
        from gateway.providers import discovery

        with patch.object(discovery.httpx, "get",
                          return_value=httpx.Response(200, json=self.SNAPSHOT)):
            index = discovery._hub_controls("http://hub.test", "tok", local=False)
        self.assertIsNotNone(index)
        for key in ("mlx-community/Breeze-TTS-2-mlx-4bit",):
            self.assertEqual(index[key]["delivery_instruction"], BREEZE_GUIDANCE)

    def test_an_unanswered_snapshot_is_unread_not_empty(self) -> None:
        from gateway.providers import discovery

        with patch.object(discovery.httpx, "get", side_effect=httpx.ConnectError("no")):
            self.assertIsNone(discovery._hub_controls("http://hub.test", "tok", local=False))
        with patch.object(discovery.httpx, "get", return_value=httpx.Response(404)):
            self.assertIsNone(discovery._hub_controls("http://hub.test", "tok", local=False))

    def test_a_discovered_model_carries_its_controls(self) -> None:
        from gateway.providers import discovery

        packages = {"packages": [{
            "package_id": "breeze", "model_id": "mlx-community/Breeze-TTS-2-mlx-4bit",
            "engine": "voice-mlx", "files_state": "files_verified",
            "dispatch_state": "eligible", "type": "model"}]}
        catalogue = {"models": [{
            "repo": "mlx-community/Breeze-TTS-2-mlx-4bit", "hub_modality": "voice",
            "capabilities": ["tts", "voice-cloning", "delivery-instruction"]}]}

        def router(url, **_kwargs):
            if str(url).endswith("/api/hub/model-packages"):
                return httpx.Response(200, json=packages)
            if str(url).endswith("/api/hub/catalog"):
                return httpx.Response(200, json=catalogue)
            if str(url).endswith("/api/hub/capabilities"):
                return httpx.Response(200, json=self.SNAPSHOT)
            return httpx.Response(404)

        definition = SimpleNamespace(id="studio-hub-test", display_name="Studio Hub",
                                     kind=defs.KIND_STUDIO_HUB, base_url="http://hub.test")
        with patch.object(discovery.httpx, "get", side_effect=router):
            result = discovery._studio_hub(definition, {"hub_token": "tok"})
        model = next(m for m in result.models
                     if m.model_id == "mlx-community/Breeze-TTS-2-mlx-4bit")
        self.assertEqual(model.raw["hub_controls"]["delivery_instruction"], BREEZE_GUIDANCE,
                         "the guidance must ride the model's own record")
        self.assertTrue(catalog.takes_delivery_instruction(model.raw))


class TheRefusal(unittest.TestCase):
    def model(self, capabilities: list) -> dict:
        return {"model_id": "breeze-tts-2", "display_name": "Breeze TTS 2",
                "raw": {"capabilities": capabilities}}

    def test_a_model_without_it_refuses_a_non_empty_instruction(self) -> None:
        with self.assertRaises(api.GatewayError) as caught:
            api._refuse_a_delivery_instruction(
                "studio-hub::plain", self.model(["tts"]),
                {"delivery_instruction": "warm"})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "DELIVERY_INSTRUCTION_UNSUPPORTED")
        self.assertIn("does not take", caught.exception.detail)

    def test_a_model_with_it_accepts_one(self) -> None:
        api._refuse_a_delivery_instruction(
            "studio-hub::breeze", self.model(["tts", "delivery-instruction"]),
            {"delivery_instruction": "warm"})

    def test_an_absent_or_blank_instruction_is_never_refused(self) -> None:
        for params in ({}, {"delivery_instruction": ""}, {"delivery_instruction": "   "},
                       {"delivery_instruction": None}):
            with self.subTest(params=params):
                api._refuse_a_delivery_instruction("x", self.model(["tts"]), params)

    def test_more_than_five_hundred_characters_is_refused_with_the_number(self) -> None:
        with self.assertRaises(api.GatewayError) as caught:
            api._refuse_a_delivery_instruction(
                "x", self.model(["tts", "delivery-instruction"]),
                {"delivery_instruction": "a" * 501})
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "DELIVERY_INSTRUCTION_TOO_LONG")
        self.assertIn("500", caught.exception.detail)

    def test_a_non_string_is_refused_rather_than_stringified(self) -> None:
        with self.assertRaises(api.GatewayError) as caught:
            api._refuse_a_delivery_instruction(
                "x", self.model(["tts", "delivery-instruction"]),
                {"delivery_instruction": {"instruction": "warm"}})
        self.assertEqual(caught.exception.code, "DELIVERY_INSTRUCTION_INVALID")


class ThroughTheDoor(unittest.TestCase):
    """The refusal and the stored param, through `/v1/audio/jobs` — Story's own route."""

    def setUp(self) -> None:
        self.client = TestClient(api.app, client=("127.0.0.1", 1))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + os.environ["GATEWAY_API_KEY"],
                        "Idempotency-Key": self.id().split(".")[-1]}

    def tearDown(self) -> None:
        with store._lock:
            store._conn.execute("DELETE FROM jobs WHERE idempotency_key LIKE ?",
                                (f"%{self.headers['Idempotency-Key']}%",))
            store._conn.commit()

    def test_it_reaches_the_stored_job_on_a_model_that_takes_it(self) -> None:
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (HUB_DEFINITION, {
                                    "model_id": "breeze-tts-2", "display_name": "Breeze TTS 2",
                                    "raw": {"capabilities": ["tts", "delivery-instruction"]}}))
        resolved.start()
        self.addCleanup(resolved.stop)
        response = self.client.post("/v1/audio/jobs", headers=self.headers, json={
            "model_id": "studio-hub::breeze-tts-2", "text": "hello", "voice_id": "v",
            "delivery_instruction": "warm and unhurried"})
        self.assertEqual(response.status_code, 202, response.text)
        job = store.find_by_idempotency_key(response.json()["id"]) or store.get(response.json()["id"])
        self.assertEqual(json.loads(job["params"]).get("delivery_instruction"), "warm and unhurried")

    def test_a_model_without_it_refuses_and_queues_nothing(self) -> None:
        resolved = patch.object(api.catalog, "resolve",
                                lambda *a, **k: (HUB_DEFINITION, {
                                    "model_id": "plain", "display_name": "Plain TTS",
                                    "raw": {"capabilities": ["tts"]}}))
        resolved.start()
        self.addCleanup(resolved.stop)
        response = self.client.post("/v1/audio/jobs", headers=self.headers, json={
            "model_id": "studio-hub::plain", "text": "hello", "voice_id": "v",
            "delivery_instruction": "warm"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "DELIVERY_INSTRUCTION_UNSUPPORTED")
        self.assertIsNone(store.find_by_idempotency_key(self.headers["Idempotency-Key"]))


if __name__ == "__main__":
    unittest.main()
