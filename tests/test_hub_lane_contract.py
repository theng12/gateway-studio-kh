#!/usr/bin/env python3
"""What the Hub states about a lane is what the gateway reads.

Studio Hub 0.6.61 answered this gateway's lane-contract request (its commit
`f20fd1a`, spec `docs/lane-execution-contract.md`). Three of those answers are
load-bearing here:

  * `modality` on every `/api/hub/model-packages` row, from the Hub's own engine
    table — so placing a model no longer depends on a second, best-effort
    catalogue read;
  * `lane_contract` per model (schema `studiohub.lane-contract.v1`) — operation,
    accepted parameters, artifact, terminal states including which means
    *uncertain* and whether the lane can discard, cancel, and the deadline the
    **Hub** enforces;
  * `model_limits` on chat rows, each value with the sentence saying where it came
    from — because the gateway had been publishing the owner's curated flat
    `1,000,000` as though it were a model's own window.

Each check below names what the gateway does with the field, not just that the
field parses. Run with::

    .venv/bin/python tests/test_hub_lane_contract.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-hub-lane-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from mock_upstream import Handler, Server  # noqa: E402

from gateway.adapters import serves  # noqa: E402
from gateway.providers import discovery  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    DISCOVERY_STUDIO_HUB,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)


class _MockHub(unittest.TestCase):
    """One in-process mock Hub, so the wire shape is what is under test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _discover(self):
        definition = ProviderDefinition(
            id="studio-hub-mock", display_name="Mock Hub", category="multi",
            kind=KIND_STUDIO_HUB, base_url=self.base,
            modalities=("image", "tts", "music", "transcription"),
            discovery=DISCOVERY_STUDIO_HUB,
        )
        return definition, discovery.discover(definition, {"base_url": self.base})


class PackageRowTests(_MockHub):
    def test_the_row_s_own_modality_places_the_model(self) -> None:
        # The Hub's stated lane on the package row is read first: it is present
        # whenever the engine is registered and never depends on a second call.
        _definition, result = self._discover()
        self.assertIsNone(result.error, result.error)
        by_id = {m.model_id: m for m in result.models}
        clone = by_id["mock-hub/clone-model"]
        self.assertEqual(clone.modality, "tts")
        self.assertEqual(clone.raw["modality_source"], "package")

    def test_a_lane_the_gateway_serves_is_discovered_and_publishable(self) -> None:
        # Chat became a served lane in 0.6.63 (a Fleet credential may now run a
        # completion through the Hub), so it is discovered **and** offerable — the
        # row that used to be withheld with a reason is now the row that runs.
        definition, result = self._discover()
        by_id = {m.model_id: m for m in result.models}
        chat = by_id.get("mock-hub/chat-model")
        self.assertIsNotNone(chat, sorted(by_id))
        self.assertEqual(chat.modality, "chat")
        self.assertTrue(serves(definition, "chat"))

    def test_a_lane_the_gateway_cannot_serve_is_discovered_and_withheld(self) -> None:
        # Video is shelved by the owner on the Hub side and this adapter has no
        # lane for it, so it arrives as a row — and `serves` is what withholds it,
        # not a silent drop in discovery.
        definition, result = self._discover()
        by_id = {m.model_id: m for m in result.models}
        video = by_id.get("mock-hub/video-model")
        self.assertIsNotNone(video, sorted(by_id))
        self.assertEqual(video.modality, "video")
        self.assertFalse(serves(definition, "video"))

    def test_the_lane_contract_is_carried_verbatim(self) -> None:
        _definition, result = self._discover()
        clone = next(m for m in result.models if m.model_id == "mock-hub/clone-model")
        contract = clone.raw["lane_contract"]
        self.assertEqual(contract["schema"], "studiohub.lane-contract.v1")
        self.assertEqual(contract["operation"], "voice.tts")
        # The Hub adds the deadline it enforces; the studio's half stays verbatim.
        self.assertEqual(contract["deadline_seconds"], 900.0)
        self.assertTrue(contract["states"]["discard_supported"])

    def test_a_package_nothing_can_place_is_reported_with_the_hub_s_own_reason(self) -> None:
        # "I could not work out what this is" must not read as "there is nothing
        # here" — and the Hub's sentence names the allowlist a person must edit,
        # so it is the one that is reported.
        _definition, result = self._discover()
        skipped = {row["engine"]: row for row in result.skipped}
        self.assertIn("mystery-mlx", skipped, result.skipped)
        self.assertEqual(skipped["mystery-mlx"]["count"], 1)
        self.assertIn("_ENGINE_ROUTE", skipped["mystery-mlx"]["reason"])
        self.assertEqual(skipped["mystery-mlx"]["model_ids"], ["mock-hub/unplaceable-model"])

    def test_a_package_with_no_modality_key_still_falls_back(self) -> None:
        # `video-model` predates 0.6.61 and carries no `modality`, so the
        # catalogue's own statement places it — the fallback is what keeps an
        # older Hub working.
        _definition, result = self._discover()
        video = next(m for m in result.models if m.model_id == "mock-hub/video-model")
        self.assertEqual(video.modality, "video")
        self.assertEqual(video.raw["modality_source"], "stated")


class ChatLimitTests(_MockHub):
    """The Hub's own numbers, with the sentence saying which number it is."""

    def _chat_row(self, model_id: str) -> dict:
        from gateway.catalog import _chat_row

        definition = ProviderDefinition(
            id="studio-hub-mock", display_name="Mock Hub", category="multi",
            kind=KIND_STUDIO_HUB, base_url=self.base, modalities=("chat",),
            discovery=DISCOVERY_STUDIO_HUB,
        )
        _definition, result = self._discover()
        model = next(m for m in result.models if m.model_id == model_id)
        return _chat_row(model_id, definition, {"model_id": model_id, "raw": model.raw})

    def test_the_hub_s_window_is_published_as_the_model_s_own(self) -> None:
        row = self._chat_row("mock-hub/chat-model")
        self.assertEqual(row["context_window_tokens"], 262144)
        self.assertEqual(row["context_window_tokens_source"], "provider")
        self.assertIn("max_position_embeddings", row["context_window_tokens_source_detail"])

    def test_a_worker_default_travels_labelled_as_one(self) -> None:
        # The Hub's fallback is published — and says in its own words that it is a
        # fallback, so nobody reads it as the checkpoint's fact.
        row = self._chat_row("mock-hub/chat-model")
        self.assertEqual(row["max_output_tokens"], 4096)
        self.assertEqual(row["max_output_tokens_source"], "provider")
        self.assertIn("worker default", row["max_output_tokens_source_detail"])

    def test_the_producers_json_guarantee_is_published_at_its_own_path(self) -> None:
        # `lane_contract.structured_output`, verbatim, because the Hub named that
        # path and the fact is the producer's: which formats are accepted, how the
        # guarantee is kept, and what it costs. Note what it is **not** — a
        # `two_pass` flag. Two-pass is a caller's technique; the row states the
        # facts that decide it.
        row = self._chat_row("mock-hub/chat-model")
        structured = (row.get("lane_contract") or {}).get("structured_output") or {}
        self.assertEqual(structured.get("formats"), ["json_object", "json_schema"])
        self.assertEqual(structured.get("enforcement"), "prompt_validate_repair")
        self.assertIs(structured.get("grammar_constrained"), False)
        self.assertIsNone(structured.get("two_pass"), "a technique is not a model fact")

    def test_a_lane_that_declares_response_format_supports_structured_output(self) -> None:
        # Read from the parameter's **declared presence**, not from the prose in
        # its note: the Hub asked us not to match on names inside strings where a
        # fact could be published.
        row = self._chat_row("mock-hub/chat-model")
        self.assertIs(row.get("supports_structured_output"), True)
        # The mode is unstated until the producer says it machine-readably, so it
        # is null rather than this gateway picking one of the two.
        self.assertIsNone(row.get("structured_output_mode"))

    def test_a_lane_with_no_contract_publishes_no_structured_output(self) -> None:
        # `mock-hub/video-model` carries no lane contract at all, so the field is
        # null — "unstated" — rather than an invented capability.
        row = self._chat_row("mock-hub/video-model")
        self.assertIsNone(row.get("lane_contract"))
        self.assertIsNone(row.get("supports_structured_output"))

    def test_nothing_stated_still_reads_as_curated_or_gateway(self) -> None:
        # A model with no `model_limits` at all keeps the existing behaviour: the
        # curated gap-fill, labelled as curated. The Hub's arrival did not change
        # what an unstated limit means.
        row = self._chat_row("mock-hub/video-model")
        self.assertIn(row["context_window_tokens_source"], {"curated", "gateway"})
        self.assertNotEqual(row["context_window_tokens_source"], "provider")


class LaneDeadlineTests(unittest.TestCase):
    """The ceiling belongs to the party that enforces it.

    Hub 0.6.61 adds `deadline_seconds` to every contract — its own peer-proxy
    timeout. Reading it is what stops this gateway from cutting a lane short at its
    own flat fallback after the Hub has raised its ceiling, which would fail a job
    that was about to succeed.
    """

    def _request(self):
        from gateway.adapters.base import BackendRequest

        return BackendRequest(
            provider_id="studio-hub-mock", model_id="mock-hub/chat-model", modality="chat",
            params={}, values={}, base_url="http://127.0.0.1:1",
        )

    def test_a_stated_deadline_is_used(self) -> None:
        from unittest.mock import patch

        from gateway.adapters.studio_hub import lane_deadline_seconds
        from gateway.providers import registry

        stated = {"model_id": "mock-hub/chat-model",
                  "raw": {"lane_contract": {"deadline_seconds": 600.0}}}
        with patch.object(registry.models, "models_for", return_value=[stated]):
            self.assertEqual(lane_deadline_seconds(self._request()), 600.0)

    def test_an_unstated_or_nonsensical_deadline_falls_back(self) -> None:
        from unittest.mock import patch

        from gateway.adapters.studio_hub import JOB_DEADLINE_S, lane_deadline_seconds
        from gateway.providers import registry

        for raw in ({}, {"lane_contract": None}, {"lane_contract": {}},
                    {"lane_contract": {"deadline_seconds": None}},
                    {"lane_contract": {"deadline_seconds": 0}},
                    {"lane_contract": {"deadline_seconds": "soon"}}):
            with self.subTest(raw=raw), patch.object(
                registry.models, "models_for",
                return_value=[{"model_id": "mock-hub/chat-model", "raw": raw}],
            ):
                self.assertEqual(lane_deadline_seconds(self._request()), JOB_DEADLINE_S)
        # And a job with no request at all is the same answer, never a crash.
        self.assertEqual(lane_deadline_seconds(None), JOB_DEADLINE_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
