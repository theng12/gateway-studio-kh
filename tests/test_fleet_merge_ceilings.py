#!/usr/bin/env python3
"""C4 — a merged row states the ceiling that is safe on **every** machine offering it.

Two audit findings, both about a number that was true of one site and published for all of them:

* **M10** — `_merge_capabilities` returned immediately for anything that was not TTS, so a
  merged row kept the **first location's** token window, output ceiling, prompt ceiling and
  upload cap. A Hub-side limit change on the second site never reached Story Studio, and the
  row could advertise a context window one of the machines would refuse.
* **M11** — `available_workers` was the literal `1` on every transcription lane, invented, on a
  model the fleet reported on sixteen machines. A published number nobody measured.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import catalog  # noqa: E402


class AMergedRowTakesTheSmallestCeilingTest(unittest.TestCase):
    def _merge(self, first: dict, second: dict, modality: str = "chat") -> dict:
        row = dict(first)
        catalog._merge_capabilities(row, dict(second), modality)
        return row

    def test_the_smaller_token_window_wins_on_a_chat_row(self) -> None:
        merged = self._merge(
            {"context_window_tokens": 262144, "context_window_tokens_source": "provider"},
            {"context_window_tokens": 32768, "context_window_tokens_source": "provider"},
        )
        self.assertEqual(merged["context_window_tokens"], 32768)
        self.assertEqual(merged["context_window_tokens_source"], "provider")

    def test_the_larger_window_never_wins(self) -> None:
        merged = self._merge(
            {"context_window_tokens": 8192},
            {"context_window_tokens": 262144},
        )
        self.assertEqual(merged["context_window_tokens"], 8192,
                         "the number must be safe on every site, not the best one")

    def test_a_value_its_source_and_its_detail_move_together(self) -> None:
        merged = self._merge(
            {"max_output_tokens": 128000, "max_output_tokens_source": "curated",
             "max_output_tokens_source_detail": "the owner's file"},
            {"max_output_tokens": 4096, "max_output_tokens_source": "provider",
             "max_output_tokens_source_detail": "the model's config.json"},
        )
        self.assertEqual(merged["max_output_tokens"], 4096)
        self.assertEqual(merged["max_output_tokens_source"], "provider")
        self.assertEqual(merged["max_output_tokens_source_detail"],
                         "the model's config.json",
                         "a value and its provenance must come from one place")

    def test_a_site_that_states_nothing_does_not_erase_a_stated_ceiling(self) -> None:
        merged = self._merge({"max_prompt_characters": 4000}, {"max_prompt_characters": None})
        self.assertEqual(merged["max_prompt_characters"], 4000)

    def test_a_stated_zero_is_a_ceiling_like_any_other(self) -> None:
        # `_first_stated` exists because an `or` chain throws a stated 0 away. The merge has the
        # same obligation: 0 is falsy, and treating it as absent would publish the other site's
        # larger number.
        merged = self._merge({"max_prompt_characters": 4000}, {"max_prompt_characters": 0})
        self.assertEqual(merged["max_prompt_characters"], 0)

    def test_the_upload_cap_still_merges_on_a_transcription_row(self) -> None:
        merged = self._merge(
            {"maximum_input_characters": 25000, "maximum_input_characters_source": "gateway"},
            {"maximum_input_characters": 15000, "maximum_input_characters_source": "gateway"},
            modality="transcription",
        )
        self.assertEqual(merged["maximum_input_characters"], 15000)

    def test_a_tts_row_keeps_its_own_rules(self) -> None:
        merged = self._merge(
            {"supports_speed": True, "minimum_speed": 0.5, "maximum_speed": 2.0},
            {"supports_speed": False},
            modality="tts",
        )
        self.assertFalse(merged["supports_speed"])
        merged = self._merge({"maximum_input_characters": 25000},
                             {"maximum_input_characters": 15000}, modality="tts")
        self.assertEqual(merged["maximum_input_characters"], 15000)


class OneNarrationMayUseEveryMacOfferingTheModelTest(unittest.TestCase):
    """Story fans out one narration's sections up to `provider_submit_concurrency`; absent, it
    narrates one section at a time (measured 2026-10-03: every chapter waited for the last)."""

    def test_sites_add_their_tts_slots(self) -> None:
        row = {"provider_submit_concurrency": 4}
        catalog._merge_capabilities(row, {"provider_submit_concurrency": 9}, "tts")
        self.assertEqual(row["provider_submit_concurrency"], 13)

    def test_a_site_with_none_neither_adds_nor_erases(self) -> None:
        row = {"provider_submit_concurrency": 4}
        catalog._merge_capabilities(row, {"provider_submit_concurrency": None}, "tts")
        self.assertEqual(row["provider_submit_concurrency"], 4)
        row = {"provider_submit_concurrency": None}
        catalog._merge_capabilities(row, {"provider_submit_concurrency": None}, "tts")
        self.assertIsNone(row["provider_submit_concurrency"], "zero is unknown, never a limit of 0")

    def test_the_figure_is_the_live_capacity_admission_enforces(self) -> None:
        from unittest.mock import patch
        from gateway import concurrency
        live = {"locations": [
            {"provider_id": "studio-hub-a", "by_modality": {"tts": 7}},
            {"provider_id": "studio-hub-b", "reachable": False, "by_modality": {"tts": 0}},
        ]}
        with patch.object(concurrency, "live_capacity", return_value=live):
            self.assertEqual(catalog._slots_by_location("tts"),
                             {"studio-hub-a": 7, "studio-hub-b": 0})
        with patch.object(concurrency, "live_capacity", side_effect=RuntimeError("fleet down")):
            self.assertEqual(catalog._slots_by_location("tts"), {})

    def test_image_rows_state_their_slots_the_same_way(self) -> None:
        from unittest.mock import patch
        from gateway import concurrency
        live = {"locations": [
            {"provider_id": "studio-hub-a", "by_modality": {"tts": 7, "image": 12}},
            {"provider_id": "studio-hub-b", "by_modality": {"tts": 1, "image": 0}},
        ]}
        with patch.object(concurrency, "live_capacity", return_value=live):
            self.assertEqual(catalog._slots_by_location("image"),
                             {"studio-hub-a": 12, "studio-hub-b": 0})
        row = {"provider_submit_concurrency": 12}
        catalog._merge_capabilities(row, {"provider_submit_concurrency": 5}, "image")
        self.assertEqual(row["provider_submit_concurrency"], 17)


class NoInventedWorkerCountIsPublishedTest(unittest.TestCase):
    def test_a_transcription_row_does_not_claim_a_worker_count(self) -> None:
        # Built through the real row builder, against a stub definition, so the assertion is
        # about what is published rather than about a helper's return value.
        definition = type("D", (), {
            "id": "studio-hub-test", "kind": "studio_hub", "display_name": "Test",
            "base_url": "", "declared_modalities": ("transcription",)})()
        row = catalog._transcription_row("whisper-large-v3-turbo", definition, {})
        self.assertNotIn("available_workers", row,
                         "the fleet reports the real number; the gateway must not invent one")

    def test_the_literal_is_gone_from_the_source(self) -> None:
        source = (ROOT / "gateway" / "catalog.py").read_text(encoding="utf-8")
        self.assertNotIn('"available_workers": 1', source)


class ReferenceCapabilityMergesAsAUnionTest(unittest.TestCase):
    """Reference capability across locations: the **union**, with the limits of the sites that have it.

    Owner, 2026-10-26, overruling the plan's intersection-of-everyone: one location whose Macs are
    all below the edit floor must not hide the feature from the whole fleet, and routing already
    keeps a non-capable location from receiving reference work
    (`fleet.routing_candidates(require_capability="edit")`). The numbers published must still hold on
    every location that can take the job, so they are the least permissive of those.
    """

    CAPABLE = {"supports_reference_images": True, "min_reference_images": 1,
               "max_reference_images": 2, "reference_image_max_dimension": 1024,
               "operations": ["text-to-image", "image-to-image"]}
    INCAPABLE = {"supports_reference_images": False, "min_reference_images": 0,
                 "max_reference_images": 0, "reference_image_max_dimension": None,
                 "operations": ["text-to-image"]}
    UNSTATED = {"supports_reference_images": None, "min_reference_images": None,
                "max_reference_images": None, "reference_image_max_dimension": None,
                "operations": ["text-to-image"]}

    def merge(self, *rows: dict) -> dict:
        merged = dict(rows[0])
        for row in rows[1:]:
            catalog._merge_capabilities(merged, dict(row), "image")
        return merged

    def test_one_capable_location_publishes_the_feature_with_its_own_limits(self) -> None:
        merged = self.merge(self.INCAPABLE, self.CAPABLE)
        self.assertIs(merged["supports_reference_images"], True,
                      "a site that cannot do it must not hide it from the fleet")
        self.assertEqual((merged["min_reference_images"], merged["max_reference_images"],
                          merged["reference_image_max_dimension"]), (1, 2, 1024))
        self.assertEqual(merged["operations"], ["text-to-image", "image-to-image"])

    def test_the_order_of_locations_does_not_decide_the_answer(self) -> None:
        self.assertEqual(self.merge(self.INCAPABLE, self.CAPABLE)["max_reference_images"], 2)
        self.assertEqual(self.merge(self.CAPABLE, self.INCAPABLE)["max_reference_images"], 2,
                         "first-location-wins is the defect this merge replaced")
        self.assertIs(self.merge(self.CAPABLE, self.INCAPABLE)["supports_reference_images"], True)

    def test_two_capable_locations_publish_the_stricter_limits(self) -> None:
        second = {"supports_reference_images": True, "min_reference_images": 2,
                  "max_reference_images": 3, "reference_image_max_dimension": 768}
        merged = self.merge(self.CAPABLE, second)
        self.assertIs(merged["supports_reference_images"], True)
        self.assertEqual(merged["min_reference_images"], 2, "the highest minimum any site needs")
        self.assertEqual(merged["max_reference_images"], 2, "the smallest maximum any site takes")
        self.assertEqual(merged["reference_image_max_dimension"], 768,
                         "the smallest ceiling, so every routed location honours it")

    def test_a_third_location_is_still_measured_against_the_advertisers_only(self) -> None:
        second = {"supports_reference_images": True, "min_reference_images": 1,
                  "max_reference_images": 4, "reference_image_max_dimension": 2048}
        merged = self.merge(self.CAPABLE, second, self.INCAPABLE, self.UNSTATED)
        self.assertIs(merged["supports_reference_images"], True)
        self.assertEqual((merged["min_reference_images"], merged["max_reference_images"],
                          merged["reference_image_max_dimension"]), (1, 2, 1024),
                         "a non-advertising location's 0 is not a ceiling")

    def test_capable_plus_unstated_keeps_the_stated_limits(self) -> None:
        merged = self.merge(self.CAPABLE, self.UNSTATED)
        self.assertIs(merged["supports_reference_images"], True)
        self.assertEqual(merged["max_reference_images"], 2)

    def test_nowhere_capable_publishes_a_definite_no(self) -> None:
        merged = self.merge(self.INCAPABLE, self.INCAPABLE)
        self.assertIs(merged["supports_reference_images"], False)
        self.assertEqual((merged["min_reference_images"], merged["max_reference_images"],
                          merged["reference_image_max_dimension"]), (0, 0, None))

    def test_nobody_stating_anything_publishes_unstated(self) -> None:
        merged = self.merge(self.UNSTATED, self.UNSTATED)
        self.assertIsNone(merged["supports_reference_images"])
        self.assertIsNone(merged["max_reference_images"],
                          "unstated is not a definite no, and not a number either")

    def test_a_definite_no_beside_an_unstated_site_is_a_no(self) -> None:
        merged = self.merge(self.UNSTATED, self.INCAPABLE)
        self.assertIs(merged["supports_reference_images"], False)

    def test_the_capability_is_not_in_the_min_wins_ceiling_table(self) -> None:
        source = (ROOT / "gateway" / "catalog.py").read_text(encoding="utf-8")
        self.assertNotIn('("max_reference_images", "max_reference_images_source"', source,
                         "a min-wins ceiling would publish 0 whenever any site is incapable")


class APoolAliasPublishesWhatEveryMemberSupportsTest(unittest.TestCase):
    """A pool alias is a menu of models: what it states must hold for the member that answers.

    The location merge above takes the union because a reference job is routed to a location that
    has the capability; a pool routes across *models*, and the caller does not choose which, so one
    member without references takes the whole alias down to none.
    """

    def alias(self, *members: dict) -> dict:
        from types import SimpleNamespace
        from unittest.mock import patch

        from gateway import fleet as fleet_module

        rows = {f"m{index}": dict(row) for index, row in enumerate(members)}
        definitions = [SimpleNamespace(id=f"studio-hub-{model_id}", kind="studio_hub")
                       for model_id in rows]
        models = [{"model_id": model_id, "modality": "image", "raw": {}} for model_id in rows]
        with (
            # **No rebuild on read.** `_fresh` rebuilds when the catalogue's fingerprint changed,
            # and this test patches the registry out from under it — so the read would rebuild
            # with the real (empty) registry and drop the rows just built. The fingerprint is no
            # longer a bare tuple of models (0.2.216 added the answering fleet to it), so it is
            # `_fresh` itself that is stubbed here: the intent is "draw these rows", unchanged.
            patch.object(catalog.Catalog, "_fresh", lambda _self: None),
            patch.object(catalog.Catalog, "_story_row",
                         lambda _self, modality, definition, model, **_: dict(rows[model["model_id"]])),
            patch.object(catalog.registry.models, "enabled_models",
                         lambda modality: list(zip(definitions, models))
                         if modality == "image" else []),
            patch.object(catalog.registry.models, "cached", lambda provider_id: {}),
            patch.object(fleet_module, "usable_location_ids", lambda: set()),
            patch.object(catalog.pools, "image_pools",
                         lambda: [{"name": "pool", "display_name": "Pool"}]),
            patch.object(catalog.image_assignment, "pool_models", lambda pool: list(rows)),
        ):
            built = catalog.Catalog()
            # Read **inside** the patches: `_fresh` is stubbed above, and a read after the
            # `with` would compare the real fingerprint with the stubbed build's.
            return next(row for row in built.rows("image") if row["id"] == "studio-hub::pool")

    def _member(self, index: int, **changes) -> dict:
        row = {"id": f"studio-hub::m{index}", "display_name": f"m{index}", "legacy_ids": [],
               "aspect_ratios": ["1:1"],
               "default_aspect_ratio": "1:1", "output_options": [], "supports_seed": True,
               "operations": ["text-to-image"], "max_prompt_characters": 4000,
               "max_prompt_characters_source": "provider", "locked": False, "chargeable": False,
               **ReferenceCapabilityMergesAsAUnionTest.CAPABLE}
        row.update(changes)
        return row

    def test_the_alias_takes_the_stricter_reference_limits_of_its_members(self) -> None:
        alias = self.alias(self._member(0),
                           self._member(1, max_reference_images=3,
                                        reference_image_max_dimension=1536))
        self.assertIs(alias["supports_reference_images"], True)
        self.assertEqual((alias["min_reference_images"], alias["max_reference_images"],
                          alias["reference_image_max_dimension"]), (1, 2, 1024))

    def test_a_member_without_references_takes_the_alias_down_to_none(self) -> None:
        alias = self.alias(self._member(0),
                           self._member(1, **ReferenceCapabilityMergesAsAUnionTest.INCAPABLE))
        self.assertIs(alias["supports_reference_images"], False)
        self.assertIsNone(alias["reference_image_max_dimension"],
                          "no member may be offered a reference ceiling the alias cannot honour")


if __name__ == "__main__":
    unittest.main()
