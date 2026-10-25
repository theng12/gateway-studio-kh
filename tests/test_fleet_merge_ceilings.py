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


if __name__ == "__main__":
    unittest.main()
