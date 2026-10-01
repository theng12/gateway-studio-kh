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
import copy
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

from mock_upstream import (  # noqa: E402
    HUB_CHAT_DEADLINE_S,
    HUB_CHAT_CONTEXT_TOKENS,
    HUB_CHAT_MAX_OUTPUT_TOKENS,
    HUB_VIDEO_DEADLINE_S,
    HUB_VOICE_DEADLINE_S,
    Handler,
    Server,
)

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
            modalities=("image", "tts", "music", "transcription", "video"),
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

    def test_a_lane_the_gateway_now_serves_is_discovered_and_publishable(self) -> None:
        """Video is a served lane since 2026-09-26: the row that used to be withheld
        (discovered, then refused by `serves`) is now the row that runs.

        The negative half of this shape is pinned by
        `UnplaceablePackageTests.test_an_engine_nothing_places_is_never_published` and by
        `VideoPublicationTests.test_a_video_model_the_hub_says_does_not_fit_is_withheld`,
        so "discovered but withheld" still has a live example — it is just no longer the
        video lane.
        """
        definition, result = self._discover()
        by_id = {m.model_id: m for m in result.models}
        video = by_id.get("mock-hub/video-model")
        self.assertIsNotNone(video, sorted(by_id))
        self.assertEqual(video.modality, "video")
        self.assertTrue(serves(definition, "video"))

    def test_the_lane_contract_is_carried_verbatim(self) -> None:
        _definition, result = self._discover()
        clone = next(m for m in result.models if m.model_id == "mock-hub/clone-model")
        contract = clone.raw["lane_contract"]
        self.assertEqual(contract["schema"], "studiohub.lane-contract.v1")
        self.assertEqual(contract["operation"], "voice.tts")
        # The Hub adds the deadline it enforces; the studio's half stays verbatim.
        self.assertEqual(contract["deadline_seconds"], HUB_VOICE_DEADLINE_S)
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
        # **Read from the fixture, never restated.** These two tests asserted the bare
        # literals `262144` and `4096`, and Studio Hub's own suite lost eight chat
        # tests to exactly that when they moved `FALLBACK_MAX_OUTPUT_TOKENS`
        # `4096 → 16384` on 2026-09-26. Ours went red the same way the moment this mock
        # was pointed at the new value — the code was right and the test was quoting.
        # The check that survives the constant moving is *"what the Hub stated reached
        # the row"*, which is the mechanism; a number copied into an assertion tests
        # only that nobody edited the fixture.
        row = self._chat_row("mock-hub/chat-model")
        self.assertEqual(row["context_window_tokens"], HUB_CHAT_CONTEXT_TOKENS)
        self.assertEqual(row["context_window_tokens_source"], "provider")
        self.assertIn("max_position_embeddings", row["context_window_tokens_source_detail"])

    def test_a_worker_default_travels_labelled_as_one(self) -> None:
        # The Hub's fallback is published — and says in its own words that it is a
        # fallback, so nobody reads it as the checkpoint's fact.
        row = self._chat_row("mock-hub/chat-model")
        self.assertEqual(row["max_output_tokens"], HUB_CHAT_MAX_OUTPUT_TOKENS)
        self.assertEqual(row["max_output_tokens_source"], "provider")
        self.assertIn("worker default", row["max_output_tokens_source_detail"])

    def test_a_model_that_states_less_than_the_fallback_keeps_its_own_number(self) -> None:
        # Studio Hub: *"the model's own stated ceiling still binds, and the fallback
        # does not override it... a fallback can never raise a lane above the window its
        # own model publishes."* Read as a mechanism rather than a value: whatever the
        # Hub states is what we publish, so a raise on their side can never inflate a
        # model that states less. This is the assertion that stays true at 16,384, at
        # 32,768, and at any number they pick next.
        from gateway.catalog import _chat_row

        _definition, result = self._discover()
        model = next(m for m in result.models if m.model_id == "mock-hub/chat-model")
        smaller = 2048
        raw = {**model.raw,
               "model_limits": {**model.raw["model_limits"],
                                "max_output_tokens": {"value": smaller,
                                                      "source": "model/config.json max_tokens"}}}
        row = _chat_row("mock-hub/chat-model", _definition,
                        {"model_id": "mock-hub/chat-model", "raw": raw})
        self.assertEqual(row["max_output_tokens"], smaller)
        self.assertLess(row["max_output_tokens"], HUB_CHAT_MAX_OUTPUT_TOKENS)

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

    def test_the_whole_contract_is_relayed_not_a_whitelist_of_one(self) -> None:
        # **The regression.** This published `{"structured_output": ...}` and nothing
        # else, so every other key the producer states was dropped — `deadline_seconds`
        # among them. The number sat in the row's `raw`, the adapter read it to size
        # its own poll, and the caller could not see it: Story Studio found the
        # 900-second window by hitting it.
        #
        # Withholding a stated fact is as wrong as inventing one, and it costs the
        # caller the same way.
        row = self._chat_row("mock-hub/chat-model")
        contract = row.get("lane_contract") or {}
        self.assertIn("deadline_seconds", contract,
                      "the lane's own timeout must reach the caller")
        # **The fixture's own number, not this gateway's.** The mock states 600 and
        # the gateway's fallback is 900, so asserting 600 is what proves the value is
        # relayed rather than restated.
        self.assertEqual(contract["deadline_seconds"], HUB_CHAT_DEADLINE_S)
        # **Every key the fixture states, and the set exactly.** Asserting a hand-
        # written list is how the last version of this test over-claimed; the
        # invariant is that nothing the producer sends is dropped, so the comparison
        # is against the fixture's own key set.
        self.assertEqual(
            set(contract),
            {"schema", "modality", "operation", "parameters", "artifact", "states",
             "deadline_seconds", "structured_output"},
            "the published contract must relay the producer's, key for key",
        )

    def test_a_lane_that_declares_response_format_supports_structured_output(self) -> None:
        # Read from the parameter's **declared presence**, not from the prose in
        # its note: the Hub asked us not to match on names inside strings where a
        # fact could be published.
        row = self._chat_row("mock-hub/chat-model")
        self.assertIs(row.get("supports_structured_output"), True)
        # The producer now says it machine-readably (`structured_output.formats`), so the mode is
        # read from it (2026-10-02 decision): a lane taking `json_schema` is the strong mode.
        self.assertEqual(row.get("structured_output_mode"), "request_json_schema")

    def test_a_lane_with_no_contract_publishes_no_structured_output(self) -> None:
        # `mock-hub/preset-model` carries no lane contract at all, so the field is
        # null — "unstated" — rather than an invented capability. (`video-model` was
        # this fixture's example until the video lane gained a real contract.)
        row = self._chat_row("mock-hub/preset-model")
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

    def test_a_deadline_is_read_per_model_and_never_reused(self) -> None:
        """The deadline is now **per lane**, and that is a new shape.

        Studio Hub, 2026-09-26: *"`deadline_seconds` now varies per lane. Your
        `lane_deadline_seconds` adapter already reads it per model, which is right;
        anything on your side that caches one number, or assumes every lane reports the
        same, is now wrong."* Their table is chat 1,800 s and image / voice /
        transcription / music 900 s, and this mock now carries both numbers.

        **The hazard is not reading the wrong field — it is reading it once and reusing
        it**, which passes every single-lane test there is. So: two models, two
        numbers, asked in one process, plus a repeat of the first to catch a cache.
        """
        from unittest.mock import patch

        from gateway.adapters.base import BackendRequest
        from gateway.adapters.studio_hub import lane_deadline_seconds
        from gateway.providers import registry

        rows = [
            {"model_id": "mock-hub/chat-model",
             "raw": {"lane_contract": {"deadline_seconds": HUB_CHAT_DEADLINE_S}}},
            {"model_id": "mock-hub/clone-model",
             "raw": {"lane_contract": {"deadline_seconds": HUB_VOICE_DEADLINE_S}}},
        ]

        def request(model_id: str, modality: str):
            return BackendRequest(
                provider_id="studio-hub-mock", model_id=model_id, modality=modality,
                params={}, values={}, base_url="http://127.0.0.1:1",
            )

        with patch.object(registry.models, "models_for", return_value=rows):
            chat = lane_deadline_seconds(request("mock-hub/chat-model", "chat"))
            voice = lane_deadline_seconds(request("mock-hub/clone-model", "voice"))
            chat_again = lane_deadline_seconds(request("mock-hub/chat-model", "chat"))

        self.assertEqual(chat, HUB_CHAT_DEADLINE_S)
        self.assertEqual(voice, HUB_VOICE_DEADLINE_S)
        self.assertNotEqual(chat, voice,
                            "one number for every lane is the defect this guards")
        self.assertEqual(chat_again, chat, "the first answer must not be cached")


class VideoPublicationTests(_MockHub):
    """A local video lane publishes what its contract states — and only then.

    The plan's tier requirement lives here: a video model whose Hub verdict is
    `memory_eligible: false` must not be offered, because the alternative is a node-side
    load failure on a machine that cannot hold the weights. The gate is the Hub's own
    per-machine verdict, relayed — the gateway does not compute a tier from a model name.
    """

    def _definition(self) -> ProviderDefinition:
        return ProviderDefinition(
            id="studio-hub-mock", display_name="Mock Hub", category="multi",
            kind=KIND_STUDIO_HUB, base_url="http://127.0.0.1:1",
            modalities=("image", "tts", "music", "transcription", "video"),
            discovery=DISCOVERY_STUDIO_HUB,
        )

    def _row(self) -> dict:
        from gateway.catalog import _video_row

        _definition, result = self._discover()
        video = next(m for m in result.models if m.model_id == "mock-hub/video-model")
        return _video_row("mock-hub/video-model", self._definition(), {"raw": video.raw})

    def _contract(self) -> dict:
        """The mock Hub's own lane contract for its video model, as the Hub stated it."""
        _definition, result = self._discover()
        video = next(m for m in result.models if m.model_id == "mock-hub/video-model")
        return video.raw["lane_contract"]

    def test_the_local_row_publishes_the_lane_s_own_modes_and_variants(self) -> None:
        row = self._row()
        self.assertEqual(row["modes"], ["image-to-video", "text-to-video"])
        self.assertEqual(row["modes_source"], "provider")
        self.assertEqual(row["aspect_ratios"], ["7:4", "4:7", "1:1", "11:7"])
        # **The variants are the lane's published CANVASES, not the cross product of its two
        # parameter enums.** The enums above two ratios with two classes, and only four of those
        # eight pairs exist — `7:4` at `448p` is not a canvas — so a row built from the enums
        # offered Story Studio work the lane refuses. Each variant carries the ratio of its own
        # pair, and a canvas that accepts only one of the two durations publishes only that one.
        self.assertEqual({(v["duration_seconds"], v["resolution"], v.get("aspect_ratio"))
                          for v in row["variants"]},
                         {(2, "256p", "7:4"), (4, "256p", "7:4"),
                          (2, "256p", "4:7"), (4, "256p", "4:7"),
                          (2, "448p", "1:1"), (4, "448p", "1:1"),
                          (2, "448p", "11:7")})
        self.assertIs(row["generates_audio"], True)
        # **The lane's switch is relayed, and only the lane's switches are.** `audio` is the one
        # boolean parameter this contract publishes; `mode`, `aspect_ratio`, `resolution`,
        # `duration_seconds` and `seed` are the same vocabulary the row already states better as
        # `variants`, and `seed` in particular is a number a caller sets rather than a switch.
        # The default and the note travel verbatim: this gateway does not know what a given lane's
        # switch costs, and the sentence that says so is the lane's to write.
        self.assertEqual(row["controls"], [{
            "name": "audio", "type": "boolean", "default": True,
            "note": ("the model generates picture and sound together, so this does not "
                     "change generation time; unticking it skips the audio decode and writes a "
                     "silent MP4. The track can be added back in an editor."),
        }])
        # The Hub's own deadline travels on the contract, and a video lane's is not 900.
        _definition, result = self._discover()
        video = next(m for m in result.models if m.model_id == "mock-hub/video-model")
        self.assertEqual(video.raw["lane_contract"]["deadline_seconds"], HUB_VIDEO_DEADLINE_S)

    def test_the_lane_s_switch_is_relayed_and_a_lane_without_one_is_not(self) -> None:
        """`controls` carries the lane's own boolean parameters, and nothing else.

        Story Studio reads a published control as permission to render one, so three statements
        have to stay distinct: a switch (relayed here), a lane that publishes no switch (no key),
        and a lane whose entry this gateway cannot read (dropped, not rendered). The forwarding
        half — that a caller's choice actually reaches the Hub's item body — is asserted on the
        wire in `test_studio_hub_contract.VideoLaneTests`, because the tuple is what changed and
        the forwarded body is what matters.
        """
        from gateway.catalog import _video_row

        row = self._row()
        self.assertEqual(row["controls"], [{
            "name": "audio", "type": "boolean", "default": True,
            "note": ("the model generates picture and sound together, so this does not "
                     "change generation time; unticking it skips the audio decode and writes a "
                     "silent MP4. The track can be added back in an editor."),
        }])
        # A lane that publishes no switch carries no `controls` key at all — absent is not the
        # same statement as an empty list, and a caller must be able to tell them apart. Built
        # from the same contract the row above came from, with only the switch removed, so the
        # comparison is about the switch rather than about a fixture that differs in ten ways.
        without = copy.deepcopy(self._contract())
        without["parameters"] = [entry for entry in without["parameters"]
                                 if entry.get("name") != "audio"]
        quiet = _video_row("mock-hub/video-model", self._definition(),
                           {"raw": {"lane_contract": without}})
        self.assertNotIn("controls", quiet)
        # An entry this gateway cannot read is dropped rather than rendered: `quality` travels
        # with a `null` default rather than the string the lane got wrong (a caller shows the
        # lane's default or nothing, never a coerced one), and the nameless one is gone — a
        # switch with no name is one no caller could ever send a value for.
        malformed = copy.deepcopy(without)
        malformed["parameters"] = [
            *malformed["parameters"],
            {"name": "quality", "type": "boolean", "default": "yes"},
            {"type": "boolean", "default": True},
        ]
        patched = _video_row("mock-hub/video-model", self._definition(),
                             {"raw": {"lane_contract": malformed}})
        self.assertEqual(patched["controls"], [
            {"name": "quality", "type": "boolean", "default": None, "note": None}])

    def test_the_row_s_own_modes_and_variants_are_unchanged_by_the_switch(self) -> None:
        """The switch must not become a variant, and a variant must not gain one.

        A boolean parameter is not a priced axis: turning it off does not change the duration,
        resolution or price a caller is quoted, and a gateway that multiplied the variants by it
        would double every row's option list with entries that differ in nothing it prices. Two
        modes times seven canvas/duration pairs is the whole list, before and after.
        """
        row = self._row()
        self.assertEqual(len(row["variants"]), 14)
        self.assertTrue(all("audio" not in variant for variant in row["variants"]))
        # The Hub's own deadline travels on the contract, and a video lane's is not 900.
        _definition, result = self._discover()
        video = next(m for m in result.models if m.model_id == "mock-hub/video-model")
        self.assertEqual(video.raw["lane_contract"]["deadline_seconds"], HUB_VIDEO_DEADLINE_S)

    def test_a_video_model_the_hub_says_does_not_fit_is_withheld(self) -> None:
        from gateway.providers.models import ModelInventory

        definition = self._definition()
        inventory = ModelInventory.__new__(ModelInventory)
        inventory.credentials = None  # not reached before the memory gate
        model = {"model_id": "mock-hub/video-model", "modality": "video",
                 "raw": {"memory_eligible": False,
                         "lane_contract": {"modes": ["image-to-video"]}}}
        reason = ModelInventory.withhold_reason(inventory, definition, model)
        self.assertIn("memory", reason or "", reason)
        self.assertFalse(ModelInventory.can_publish(inventory, definition, model))

    def test_a_video_model_with_no_contract_is_withheld(self) -> None:
        from gateway.providers.models import ModelInventory

        definition = self._definition()
        inventory = ModelInventory.__new__(ModelInventory)
        inventory.credentials = None
        model = {"model_id": "mock-hub/video-model", "modality": "video",
                 "raw": {"memory_eligible": True, "lane_contract": None}}
        reason = ModelInventory.withhold_reason(inventory, definition, model)
        self.assertIn("lane contract", reason or "", reason)
        self.assertFalse(ModelInventory.can_publish(inventory, definition, model))


if __name__ == "__main__":
    unittest.main(verbosity=2)
