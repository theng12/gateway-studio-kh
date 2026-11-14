#!/usr/bin/env python3
"""`/v1/videos/models` — the video catalogue with its preflight evidence.

What is pinned here:

* every row carries kinds / durations / resolutions / aspect ratios **with their
  own source**, price evidence with a source and an observation date, an
  `available` verdict and the dispatch contract's rejection vocabulary;
* `available` is the fleet's own answer — a model whose only location is offline
  is unavailable with "offline" and the last-seen time, not merely `locked`;
* a free local model reads `free: true` with amount `0`; a price nobody stated is
  `null` + "not stated", never `0`/`free`;
* a duration nobody stated is `null` + "not stated", never `0`;
* Story's preflight can compute `unit price × max duration` for an available
  model from exactly these fields, and has no number to show for an unpriced one.

No network: the fleet snapshot and the enabled-model walk are patched.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-video-models-"))
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

from gateway import fleet, video_models  # noqa: E402
from gateway.providers import registry  # noqa: E402

MODEL = "mlx-community/video-model"
KINDS = {"t2v", "i2v"}
SOURCE_WORDS = {"declared", "observed", "default"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _catalogue_row(**overrides) -> dict:
    """One row shaped exactly as `catalog._story_row` builds a local video row."""
    row = {
        "id": f"studio-hub::{MODEL}",
        "display_name": "Video Model",
        "provider_name": "Studio Hub",
        "modality": "video",
        "modes": ["text-to-video", "image-to-video"],
        "modes_source": "provider",
        "aspect_ratios": ["16:9", "9:16"],
        "resolutions": ["480p", "720p"],
        "duration_seconds": {"min": 2, "max": 10, "step": 2},
        "variants": [],
        "price_source": "local",
        "price_usd": {"per_video": 0},
        "price_as_of": None,
        "chargeable": False,
        "locked": False,
        "available": True,
    }
    row.update(overrides)
    return row


def _paid_cloud_row(**overrides) -> dict:
    row = {
        "id": "fal-ai::alibaba/wan-3.0/text-to-video",
        "display_name": "Wan 3.0",
        "provider_name": "fal.ai",
        "modality": "video",
        "modes": ["text-to-video"],
        "modes_source": "provider",
        "aspect_ratios": ["16:9", "9:16"],
        "resolutions": ["480p", "720p", "1080p"],
        "duration_seconds": {"min": 2, "max": 30, "step": 1},
        "variants": [],
        "price_source": "provider",
        "price_as_of": "2026-10-26T00:00:00+00:00",
        "price_basis": "per_second",
        "price_usd": {"per_second": 0.04},
        "chargeable": True,
        "locked": False,
        "available": True,
    }
    row.update(overrides)
    return row


def _unstated_row(**overrides) -> dict:
    """A model nobody has described: no caps, no price, no mode."""
    row = _paid_cloud_row(
        id="fal-ai::mystery-video", display_name="Mystery Video",
        modes=[], modes_source=None, aspect_ratios=[], resolutions=[],
        duration_seconds=None, variants=[],
        price_source="unpublished", price_usd=None, price_as_of=None,
        chargeable=True,
    )
    row.update(overrides)
    return row


def _location(location_id: str, **overrides) -> dict:
    row = {
        "id": location_id,
        "label": location_id.title(),
        "reachable": True,
        "capacity": {},
        "model_supply": {MODEL: {"eligible": 2, "available": 2}},
        "machines": [],
        "machine_summary": {"reachable": 2},
        "running_jobs": [],
    }
    row.update(overrides)
    return row


def _definition(location_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{location_id}")


class _Fleet:
    """A patched fleet: these locations, and every one of them publishing MODEL."""

    def __init__(self, locations: list[dict]) -> None:
        self.locations = locations
        self._patches = [
            patch.object(fleet, "snapshot",
                         lambda allow_stale=False: {"locations": self.locations}),
            patch.object(
                registry.models, "enabled_models",
                lambda modality: [(_definition(row["id"]),
                                   {"model_id": MODEL, "modality": modality,
                                    "enabled": True, "raw": {}})
                                  for row in self.locations]),
        ]

    def __enter__(self) -> "_Fleet":
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for item in reversed(self._patches):
            item.stop()


def _populated() -> dict:
    with _Fleet([_location("a", model_supply={MODEL: {"eligible": 2, "available": 1}})]):
        return video_models.row(_catalogue_row())


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class ContractTests(unittest.TestCase):
    def test_every_video_row_carries_its_evidence_and_sources(self) -> None:
        with _Fleet([_location("a")]):
            rows = [video_models.row(item) for item in
                    (_catalogue_row(), _paid_cloud_row(), _unstated_row())]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row=row["id"]):
                self.assertTrue(row["id"])
                self.assertTrue(row["provider"], "a row names its real provider")
                self.assertTrue(row["display_name"])

                if row["kinds"] is not None:
                    self.assertTrue(set(row["kinds"]) <= KINDS, row["kinds"])
                    self.assertIn(row["kinds_source"], SOURCE_WORDS)
                else:
                    self.assertIn("kinds", row["not_stated"])

                if row["durations"] is not None:
                    self.assertLessEqual(row["durations"]["min"], row["durations"]["max"])
                    self.assertGreater(row["durations"]["step"], 0)
                    self.assertIn(row["durations_source"], SOURCE_WORDS)
                else:
                    self.assertIn("durations", row["not_stated"])

                for field in ("aspect_ratios", "resolutions"):
                    if row[field]:
                        self.assertIn(row[f"{field}_source"], SOURCE_WORDS)
                    else:
                        self.assertIn(field, row["not_stated"])

                price = row["price_evidence"]
                for key in ("unit", "amount", "currency", "free", "source", "observed_at"):
                    self.assertIn(key, price)

                self.assertIsInstance(row["available"], bool)
                if not row["available"]:
                    self.assertIn(row["reason"], fleet.REJECTION_REASONS)
                self.assertIn("serving", row["machines"])
                self.assertIn("publishers", row["machines"])
                self.assertTrue(
                    row["machines"]["label"] == "not on the fleet"
                    or row["machines"]["label"].endswith(
                        f" of {row['machines']['publishers']}"),
                    row["machines"])

    def test_the_live_catalogue_envelope_keeps_its_shape(self) -> None:
        envelope = video_models.envelope()
        self.assertIn("models", envelope)
        self.assertIn("freshness", envelope)

    def test_rejection_rows_speak_only_the_dispatch_vocabulary(self) -> None:
        eligibility = {
            "available": False,
            "candidates": [],
            "rejections": [
                {"location_id": "a", "provider_id": "studio-hub-a", "label": "A",
                 "machine": None, "reason": "offline", "detail": "not answering",
                 "last_seen_at": 1.0},
                {"location_id": "b", "provider_id": "studio-hub-b", "label": "B",
                 "machine": None, "reason": "not-a-real-reason", "detail": "?"},
            ],
        }
        with patch.object(fleet, "dispatch_eligibility", lambda *a, **k: eligibility):
            row = video_models.row(_catalogue_row())
        self.assertFalse(row["available"])
        self.assertEqual(row["reason"], "offline")
        self.assertEqual(row["reason_detail"], "not answering")
        self.assertEqual(row["rejections"][0]["last_seen_at"], 1.0)
        for rejection in row["rejections"]:
            self.assertIn(rejection["reason"], fleet.REJECTION_REASONS)


# ---------------------------------------------------------------------------
# Eligibility is the fleet's own answer
# ---------------------------------------------------------------------------
class EligibilityTests(unittest.TestCase):
    def test_available_matches_a_fresh_routing_candidates_read(self) -> None:
        locations = [_location("a"), _location("b")]
        with _Fleet(locations):
            fresh = fleet.routing_candidates(MODEL, "video")
            row = video_models.row(_catalogue_row())
        self.assertEqual(row["available"], bool(fresh))
        self.assertEqual(row["machines"]["serving"],
                         len({c["location_id"] for c in fresh}))
        self.assertEqual(row["machines"]["publishers"], 2)
        self.assertEqual(row["machines"]["label"], "2 of 2")

    def test_a_model_whose_only_location_is_offline_is_unavailable_with_the_reason(self) -> None:
        offline = _location("a", reachable=False, error="Nothing is listening.",
                            machine_summary={"reachable": 0})
        with _Fleet([offline]):
            row = video_models.row(_catalogue_row())
        self.assertFalse(row["available"])
        self.assertEqual(row["reason"], "offline")
        self.assertIn("not answering", row["reason_detail"])
        self.assertEqual(row["machines"]["serving"], 0)
        self.assertEqual(row["machines"]["publishers"], 1)

    def test_a_reachable_location_that_does_not_publish_the_model_is_not_a_publisher(self) -> None:
        with _Fleet([_location("a")]):
            with patch.object(registry.models, "enabled_models", lambda modality: []):
                row = video_models.row(_catalogue_row())
        self.assertFalse(row["available"])
        self.assertEqual(row["machines"], {"serving": 0, "publishers": 0, "label": "0 of 0"})

    def test_a_cloud_model_uses_its_own_catalogue_lock_not_a_fleet_verdict(self) -> None:
        locked = video_models.row(_paid_cloud_row(locked=True, available=False,
                                                   lock_reason="Every Mac is busy"))
        self.assertFalse(locked["available"])
        self.assertEqual(locked["reason"], "unknown")
        self.assertEqual(locked["reason_detail"], "Every Mac is busy")
        open_row = video_models.row(_paid_cloud_row())
        self.assertTrue(open_row["available"])
        self.assertIsNone(open_row["reason"])


# ---------------------------------------------------------------------------
# Honest money and honest time
# ---------------------------------------------------------------------------
class PriceTests(unittest.TestCase):
    def test_a_free_local_model_reads_free_with_amount_zero(self) -> None:
        row = _populated()
        price = row["price_evidence"]
        self.assertIs(price["free"], True)
        self.assertEqual(price["amount"], 0)
        self.assertEqual(price["source"], "local")
        self.assertEqual(price["label"], "Free (your Macs)")

    def test_a_provider_price_is_carried_with_its_unit_and_day(self) -> None:
        price = video_models.row(_paid_cloud_row())["price_evidence"]
        self.assertEqual(price["unit"], "per_second")
        self.assertEqual(price["amount"], 0.04)
        self.assertEqual(price["currency"], "USD")
        self.assertIs(price["free"], False)
        self.assertEqual(price["source"], "provider")
        self.assertEqual(price["observed_at"], "2026-10-26T00:00:00+00:00")

    def test_an_unknown_price_is_null_and_not_stated_never_zero(self) -> None:
        price = video_models.row(_unstated_row())["price_evidence"]
        self.assertIsNone(price["amount"])
        self.assertIsNone(price["unit"])
        self.assertIsNone(price["free"])
        self.assertEqual(price["note"], "not stated")

    def test_an_unknown_duration_is_null_and_not_stated_never_zero(self) -> None:
        row = video_models.row(_unstated_row())
        self.assertIsNone(row["durations"])
        self.assertIsNone(row["durations_source"])
        self.assertIn("durations", row["not_stated"])
        self.assertNotEqual(row["durations"], 0)

    def test_durations_read_every_whole_second_the_record_states(self) -> None:
        record = _catalogue_row(duration_seconds={"min": 2, "max": 10, "step": 2})
        row = video_models.row(record)
        self.assertEqual(row["durations"], {"min": 2, "max": 10, "step": 2})
        self.assertEqual(row["durations_source"], "declared")


# ---------------------------------------------------------------------------
# Story's preflight, computed from the served fields only
# ---------------------------------------------------------------------------
def preflight_max_cost(row: dict, kind: str = "t2v") -> float | None:
    """What Story does before a segment runs: price × the longest allowed clip.

    Returns `None` — "not priced" — when there is no number to multiply, so an
    unpriced model never shows `0`.
    """
    if not row["available"] or kind not in (row["kinds"] or []):
        return None
    price, durations = row["price_evidence"], row["durations"]
    amount = price.get("amount")
    if not isinstance(amount, (int, float)) or not durations:
        return None
    if price["unit"] == "per_second":
        return amount * durations["max"]
    return amount


class PreflightTests(unittest.TestCase):
    def test_max_cost_is_unit_price_times_max_duration_for_an_available_model(self) -> None:
        row = video_models.row(_paid_cloud_row())
        self.assertTrue(row["available"])
        self.assertEqual(row["kinds"], ["t2v"])
        self.assertAlmostEqual(preflight_max_cost(row, "t2v"), 0.04 * 30)
        # The served evidence is what the fixture multiplied — same numbers, same source.
        self.assertEqual(row["price_evidence"]["amount"], 0.04)
        self.assertEqual(row["durations"]["max"], 30)

    def test_a_local_free_model_costs_zero_per_clip(self) -> None:
        row = _populated()
        self.assertEqual(preflight_max_cost(row, "t2v"), 0)

    def test_an_unpriced_model_shows_no_number(self) -> None:
        row = video_models.row(_unstated_row())
        self.assertIsNone(preflight_max_cost(row, "t2v"))

    def test_an_unavailable_model_shows_no_number(self) -> None:
        with _Fleet([_location("a", reachable=False)]):
            row = video_models.row(_catalogue_row())
        self.assertFalse(row["available"])
        self.assertIsNone(preflight_max_cost(row, "t2v"))


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
class RouteTests(unittest.TestCase):
    def test_one_handler_answers_the_path_and_it_replaces_the_generic_one(self) -> None:
        from gateway import api

        matching = [route for route in api.app.routes
                    if getattr(route, "path", None) == "/v1/videos/models"]
        self.assertEqual(len(matching), 1, "one path, one handler")
        self.assertEqual(matching[0].name, "video_models_route")
        self.assertTrue(getattr(matching[0].endpoint, "__name__", ""))


if __name__ == "__main__":
    unittest.main()
