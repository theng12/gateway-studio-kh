#!/usr/bin/env python3
"""The lane that renders owns the geometry, and a dead fleet is not an account problem.

TWO AUDIT FINDINGS, ONE FILE (work item **A6**, findings **H3** and **H4**).

**H3 — Story Studio was published image geometry the Macs do not render.** Every image
model, cloud and local alike, was described by `definitions.IMAGE_SIZE_BY_RATIO`: 16:9 →
1536x1024, 4:3 → 1536x1024. That is the *cloud* table (what an OpenAI-compatible endpoint
is sent), and it was also put on Studio Hub rows, whose worker renders **1280x720** and
**1152x864** — four of the five published sizes wrong by 23–33% linearly — while the lane's
own contract accepts **thirteen** ratios, including `16:9 small` = **1024x576**, the
video-valid frame the Image studio added on purpose (720 is not a multiple of 32). Story
composed at 1536x1024 and received a 1280x720 file, and Story's picker could not offer
eight of the ratios the lane takes.

**H4 — a model whose every Mac is down published `available: true`.** `_story_row` wrote a
hardcoded `True` beside a `locked`/`lock_reason`/`availability` triple that said otherwise,
so Story Studio's `isUsableLiveGenStudioModel` (which requires `model.available`) offered a
model no location could run. Its locked-row sentence then mapped the unusable row onto an
account restriction, because the reason published for the outage was not the fleet's own.

The contract below is **verbatim from the running Hub on this Mac** — `/api/hub/catalog`
on 2026-09-28, Studio Hub **0.6.91** build `92d7642`, whose installed bundle at
`/Applications/Studio Hub.app/Contents/Resources/components/image/app/backend/generation.py`
carries the same `SIZE_PRESETS` (checked, not assumed: a checkout can lead the bundle).
The pixel presets are cross-checked against that Hub's own `image_sizes` for both image
models. Run with::

    .venv/bin/python tests/test_image_geometry.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-image-geometry-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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
        "GATEWAY_API_KEY": "",
    }
)

from gateway import catalog  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    HUB_IMAGE_SIZE_BY_RATIO,
    IMAGE_SIZE_BY_RATIO,
    ProviderDefinition,
)


#: Studio Hub 0.6.91's image lane, as `/api/hub/catalog` published it on 2026-09-28 —
#: trimmed only of `runtime_seconds` and `cancel.semantics` prose, which no reader here
#: touches. The `aspect_ratio` enum is the load-bearing part: thirteen entries, in the
#: lane's own (sorted) order, `16:9 small` among them.
HUB_IMAGE_CONTRACT: dict = {
    "schema": "studiohub.lane-contract.v1",
    "modality": "image",
    "operation": "image.text_to_image",
    "parameters": [
        {"name": "prompt", "type": "string", "required": True, "max_characters": 4000},
        {"name": "aspect_ratio", "type": "string", "required": False,
         "enum": ["16:9", "16:9 small", "1:1", "1:2", "21:9", "2:1", "2:3", "3:2",
                  "3:4", "4:3", "4:5", "5:4", "9:16"],
         "default": "16:9",
         "note": "width and height follow from the preset; a mismatched pair is refused"},
        {"name": "resolution", "type": "string", "required": False,
         "enum": ["1K"], "default": "1K"},
        {"name": "steps", "type": "integer", "required": False,
         "minimum": 2, "maximum": 100, "default": 4},
        {"name": "guidance", "type": "number", "required": False,
         "minimum": 0.0, "maximum": 30.0, "default": 1.0},
        {"name": "seed", "type": "integer", "required": False,
         "minimum": -1, "maximum": 4294967295, "default": -1,
         "note": "-1 draws a random seed"},
    ],
    "artifact": {"modality": "image", "media_type": "image/png", "extension": "png",
                 "per_item": 1},
    "states": {"accepted": ["queued"], "active": ["running", "cancel_requested"],
               "terminal": ["done", "error", "cancelled"], "uncertain": "uncertain",
               "uncertain_is_terminal": False, "discard_supported": True},
    "cancel": {"supported": True, "request": "DELETE /api/hub/jobs/{batch_id}"},
}

HUB_DEFINITION = ProviderDefinition(
    id="studio-hub-local", display_name="Studio Hub", category="local",
    kind="studio_hub", base_url="", modalities=("image",), discovery="studio_hub",
)
CLOUD_DEFINITION = ProviderDefinition(
    id="kie-ai-images", display_name="Kie", category="image", kind="kie",
    base_url="https://api.kie.ai", modalities=("image",), discovery="kie",
)


def image_row(definition: ProviderDefinition, raw: dict) -> dict:
    return catalog._image_row(
        "AITRADER/FLUX2-klein-4B-mlx-4bit", definition,
        {"model_id": "AITRADER/FLUX2-klein-4B-mlx-4bit", "raw": raw},
    )


class HubImageGeometryTests(unittest.TestCase):
    """H3: a `studio-hub` row is described by the lane's own ratios and presets."""

    def setUp(self) -> None:
        self.row = image_row(HUB_DEFINITION, {"lane_contract": HUB_IMAGE_CONTRACT,
                                             "capabilities": ["txt2img"]})

    def test_the_lane_publishes_every_ratio_it_states(self) -> None:
        """Thirteen, not the five the cloud table lists — the lane's order, verbatim."""
        self.assertEqual(
            self.row["aspect_ratios"],
            ["16:9", "16:9 small", "1:1", "1:2", "21:9", "2:1", "2:3", "3:2",
             "3:4", "4:3", "4:5", "5:4", "9:16"],
        )
        self.assertEqual(len(self.row["aspect_ratios"]), 13)

    def test_one_output_option_per_ratio_with_the_hubs_own_pixels(self) -> None:
        sizes = {option["aspect_ratio"]: (option["width"], option["height"])
                 for option in self.row["output_options"]}
        self.assertEqual(len(sizes), 13)
        # The four the audit found wrong by 23–33% linearly, plus the frame the video
        # lane needs. The cloud table's answers are in the comments, not on the wire.
        self.assertEqual(sizes["16:9"], (1280, 720))       # was 1536x1024
        self.assertEqual(sizes["9:16"], (720, 1280))       # was 1024x1536
        self.assertEqual(sizes["4:3"], (1152, 864))        # was 1536x1024
        self.assertEqual(sizes["3:4"], (864, 1152))        # was 1024x1536
        self.assertEqual(sizes["16:9 small"], (1024, 576))

    def test_the_video_valid_frame_is_offered(self) -> None:
        """`16:9 small` is a *size* key, and it is the reason Story can compose for video."""
        self.assertIn("16:9 small", self.row["aspect_ratios"])
        option = next(o for o in self.row["output_options"]
                      if o["aspect_ratio"] == "16:9 small")
        self.assertEqual((option["width"], option["height"]), (1024, 576))
        self.assertIs(option["is_default"], False)

    def test_the_lanes_own_default_is_the_default_option(self) -> None:
        self.assertEqual(self.row["default_aspect_ratio"], "16:9")
        self.assertEqual(self.row["default_resolution"], "1K")
        flagged = [option["aspect_ratio"] for option in self.row["output_options"]
                   if option["is_default"]]
        self.assertEqual(flagged, ["16:9"])

    def test_the_option_shape_story_reads_is_intact(self) -> None:
        """Story filters on `resolution`+`aspect_ratio` and renders `width.toLocaleString()`.

        So every published option must carry all four, and none may be null: a row with a
        null width is a crash in the picker, not an honest gap.
        """
        for option in self.row["output_options"]:
            with self.subTest(ratio=option["aspect_ratio"]):
                self.assertEqual(option["resolution"], "1K")
                for key in ("width", "height"):
                    self.assertIsInstance(option[key], int, option)
                    self.assertGreater(option[key], 0)
                for key in ("credits_per_image", "base_credits_per_image",
                            "reference_generation_credits"):
                    self.assertEqual(option[key], 0)
                self.assertEqual(option["price_version"], "flat")
                self.assertEqual(option["reference_generation_price_version"], "flat")

    def test_a_hub_that_states_nothing_keeps_the_existing_table(self) -> None:
        """Absence is not "no ratios" — a contract-less row falls back, as it always did."""
        for raw in ({}, {"lane_contract": None}, {"lane_contract": "nonsense"},
                    {"lane_contract": {"parameters": [{"name": "aspect_ratio",
                                                       "enum": []}]}},
                    {"lane_contract": {"parameters": [{"name": "prompt"}]}}):
            with self.subTest(raw=raw):
                row = image_row(HUB_DEFINITION, raw)
                self.assertEqual(row["aspect_ratios"], catalog._IMAGE_RATIOS)
                self.assertEqual(
                    [(o["aspect_ratio"], o["width"], o["height"])
                     for o in row["output_options"]],
                    [(ratio, *IMAGE_SIZE_BY_RATIO[ratio]) for ratio in catalog._IMAGE_RATIOS],
                )

    def test_the_hubs_stated_size_list_is_the_whole_answer(self) -> None:
        """When the Hub states `image_sizes`, our copy of its table is not consulted.

        **A ratio with no stated pixels is not offered** (owner rule, 0.2.257): the picker
        renders the matching option's width, so every published ratio must have one. The
        stated default `7:5` has no size here, so the first sized ratio becomes the default
        and our table's 1024x1024 is never substituted for a missing geometry.
        """
        contract = {"parameters": [
            {"name": "aspect_ratio", "enum": ["16:9", "7:5", "1:1"], "default": "7:5"},
            {"name": "resolution", "enum": ["2K"], "default": "2K"},
        ]}
        raw = {"lane_contract": contract,
               "image_sizes": [{"id": "16:9", "width": 640, "height": 360},
                               {"id": "7:5", "width": None, "height": None}]}
        row = image_row(HUB_DEFINITION, raw)
        self.assertEqual(row["aspect_ratios"], ["16:9"])
        self.assertEqual(row["default_aspect_ratio"], "16:9")
        self.assertEqual(row["default_resolution"], "2K")
        self.assertEqual([(o["aspect_ratio"], o["width"], o["height"])
                          for o in row["output_options"]], [("16:9", 640, 360)])
        # `1:1` is a ratio the lane states and the size list omits entirely: withheld, and
        # the table's 1024x1024 is not silently substituted for it.
        self.assertNotIn("1:1", row["aspect_ratios"])
        self.assertNotIn("1:1", [o["aspect_ratio"] for o in row["output_options"]])

    def test_the_hub_preset_table_is_the_documented_hub_geometry(self) -> None:
        """One table, and it is the Hub's — 0.6.91's thirteen `SIZE_PRESETS` entries."""
        self.assertEqual(
            HUB_IMAGE_SIZE_BY_RATIO,
            {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024),
             "16:9 small": (1024, 576), "4:3": (1152, 864), "3:4": (864, 1152),
             "3:2": (1200, 800), "2:3": (800, 1200), "5:4": (1120, 896),
             "4:5": (896, 1120), "21:9": (1680, 720), "2:1": (1408, 704),
             "1:2": (704, 1408)},
        )
        self.assertNotEqual(HUB_IMAGE_SIZE_BY_RATIO["16:9"], IMAGE_SIZE_BY_RATIO["16:9"])


class CloudImageGeometryIsUntouchedTests(unittest.TestCase):
    """(c) The cloud table stays exactly as it is for every non-`studio-hub` kind."""

    def test_a_cloud_row_keeps_the_cloud_table(self) -> None:
        row = image_row(CLOUD_DEFINITION, {})
        self.assertEqual(row["aspect_ratios"], ["16:9", "9:16", "1:1", "4:3", "3:4"])
        self.assertEqual(row["default_aspect_ratio"], "16:9")
        self.assertEqual(
            [(o["aspect_ratio"], o["width"], o["height"]) for o in row["output_options"]],
            [(ratio, *IMAGE_SIZE_BY_RATIO[ratio]) for ratio in catalog._IMAGE_RATIOS],
        )

    def test_a_lane_contract_on_a_cloud_row_is_not_read(self) -> None:
        """The contract path is `studio_hub` only: another kind's contract is not its sizes."""
        row = image_row(CLOUD_DEFINITION, {"lane_contract": HUB_IMAGE_CONTRACT})
        self.assertEqual(row["aspect_ratios"], catalog._IMAGE_RATIOS)
        self.assertEqual(row["output_options"][0]["width"], 1536)


class AvailabilityTests(unittest.TestCase):
    """H4: `available` is `not locked`, and the reason is the fleet's own."""

    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
                "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
                "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
                "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
                "GATEWAY_API_KEY": "",
            },
        )
        self._env.start()
        from gateway import fleet
        from gateway.providers import registry

        self.fleet = fleet
        self.registry = registry

    def tearDown(self) -> None:
        self._env.stop()

    def _rebuild_with(self, proposals, answering: set[str]):
        definitions = [
            SimpleNamespace(
                id=provider_id, kind="studio_hub", display_name="Studio Hub",
                description="Your own Apple Silicon runners.",
            )
            for provider_id, _model in proposals
        ]

        def enabled(modality: str):
            if modality != "image":
                return []
            return [(definition, dict(model))
                    for definition, (_pid, model) in zip(definitions, proposals)]

        with patch.object(self.registry.models, "enabled_models", enabled), \
                patch.object(self.registry.models, "cached", lambda provider_id: {}), \
                patch.object(self.fleet, "usable_location_ids", lambda: set(answering)), \
                patch.object(self.registry, "definitions", lambda: definitions):
            rebuilt = catalog.Catalog()
            # Read while still patched: `rows()` re-checks the registry signature, and outside the
            # patches that is the real registry's, which differs once any earlier test enabled a provider.
            rows = [row for row in rebuilt.rows("image") if row["id"].endswith("::hub/model")]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def _proposals(self):
        raw = {"lane_contract": HUB_IMAGE_CONTRACT}
        return [
            ("studio-hub-local", {"model_id": "hub/model", "modality": "image",
                                  "display_name": "Hub Model", "raw": raw}),
            ("studio-hub-site-a", {"model_id": "hub/model", "modality": "image",
                                   "display_name": "Hub Model", "raw": raw}),
        ]

    def test_one_writer_for_every_spelling_of_availability(self) -> None:
        row: dict = {}
        catalog._publish_lock(row, True, "No Mac offering this model is reachable.")
        self.assertIs(row["locked"], True)
        self.assertIs(row["available"], False)
        self.assertEqual(row["availability"], "unavailable")
        self.assertEqual(row["lock_reason"], "No Mac offering this model is reachable.")
        catalog._publish_lock(row, False, None)
        self.assertIs(row["locked"], False)
        self.assertIs(row["available"], True)
        self.assertEqual(row["availability"], "available")
        self.assertIsNone(row["lock_reason"])

    def test_every_mac_down_is_unavailable_with_the_fleets_own_reason(self) -> None:
        row = self._rebuild_with(self._proposals(), answering=set())
        self.assertIs(row["locked"], True)
        self.assertIs(row["available"], False, row)
        self.assertEqual(row["availability"], "unavailable")
        reason = row["lock_reason"] or ""
        # A sentence naming the outage, in the fleet's words...
        self.assertIn("No Mac able to run this model is available right now", reason)
        self.assertIn("offline", reason)
        # ...and never an account/billing code, which is the one reason Story Studio's
        # locked-row sentence turns into an account restriction.
        self.assertNotIn("paid", reason.lower())
        self.assertNotIn("account", reason.lower())
        self.assertNotIn("credit", reason.lower())
        # The same sentence reaches the field Story Studio's health route joins.
        self.assertIn(reason, row["activation_blockers"])

    def test_one_answering_mac_offers_it_in_all_three_vocabularies(self) -> None:
        row = self._rebuild_with(self._proposals(), answering={"site-a"})
        self.assertIs(row["locked"], False)
        self.assertIs(row["available"], True, row)
        self.assertEqual(row["availability"], "available")
        self.assertIsNone(row["lock_reason"])
        self.assertEqual(row["activation_blockers"], [])

    def test_the_merged_row_still_publishes_the_lanes_geometry(self) -> None:
        """One model, two locations, one row — and it is described by the lane contract."""
        row = self._rebuild_with(self._proposals(), answering={"site-a"})
        self.assertEqual(row["aspect_ratios"][:3], ["16:9", "16:9 small", "1:1"])
        self.assertEqual(row["output_options"][0]["width"], 1280)
        self.assertEqual(row["output_options"][0]["height"], 720)

    def test_a_cloud_row_is_available_and_unlocked(self) -> None:
        definition = SimpleNamespace(
            id="kie-ai-images", kind="kie", display_name="Kie", description="",
            modalities=("image",), base_url="https://api.kie.ai",
        )
        built = catalog.Catalog.__new__(catalog.Catalog)
        row = built._story_row(
            "image", definition,
            {"model_id": "some-image", "modality": "image", "display_name": "Some",
             "raw": {}},
            usable=set(),
        )
        self.assertIs(row["available"], True)
        self.assertIs(row["locked"], False)
        self.assertEqual(row["availability"], "available")
        self.assertIsNone(row["lock_reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
