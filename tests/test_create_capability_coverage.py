#!/usr/bin/env python3
"""Capability cross-check for the Create pages (0.2.257).

The owner's gate (2026-11-13): every capability a model declares has a control, a capability
the catalogue stops publishing loses its control, and an exception is written down beside it.

Two directions, so drift fails either way:

  * **server → page**: every control-bearing key these row builders publish is read by
    `gateway/admin_ui.html` (named reader in `READERS`), or is a recorded exception;
  * **page → server**: every capability the page draws is still published by the row
    builders — a control reading a field the catalogue no longer sends is a dead control.

No app, no gateway, no network: the row builders are driven with fixtures, and the page is
read as source.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation  # noqa: E402

_TMP = _isolation.sandbox_env("gateway-capability-coverage-")

from gateway import catalog  # noqa: E402
from gateway.catalog import catalog as live_catalog  # noqa: E402
from gateway.providers.definitions import ProviderDefinition  # noqa: E402

PAGE = (ROOT / "gateway" / "admin_ui.html").read_text(encoding="utf-8")

#: capability key → the page function that draws it. A reader must exist in the page source.
READERS = {
    "aspect_ratios": "syncCreateImageOptions",
    "default_aspect_ratio": "syncCreateImageOptions",
    "output_options": "createOutputOptions",
    "supports_instrumental": "updateCreateGate",
    "requires_lyrics": "createRequiredProblem",
    "supports_vocals": "updateCreateGate",
    "variants": "syncCreateVideoOptions",
    "upload_limit_bytes": "showUploadLimit",
    "controls": "applyCreateModelControls",
}

#: Capabilities with no control in the console **on purpose**, and why. An exception is
#: never silence: it is a decision written down where the check can read it.
EXCEPTIONS = {
    "supports_reference_images": (
        "image references are staged through /v1/images/source-images by Story; the console "
        "Create page has no upload field yet (M2 follow-up, not public)"),
    "max_reference_images": "same as supports_reference_images",
    "min_reference_images": "same as supports_reference_images",
    "supported_languages": (
        "music language is offered through the model's declared `vocal_language` control; "
        "`supported_languages` is the same statement in the catalogue's own vocabulary"),
}


def _definition(kind: str = "studio_hub") -> ProviderDefinition:
    return ProviderDefinition(
        id="studio-hub-local", display_name="Studio Hub", category="media", kind=kind,
        base_url="http://127.0.0.1:8787", modalities=("image", "music", "video"),
        discovery="static", data_use="none", free_tier=True,
    )


def _fixture_rows() -> dict[str, dict]:
    image = catalog._image_row("flux", _definition(), {
        "model_id": "flux", "display_name": "Flux",
        "raw": {
            "image_sizes": [{"id": "16:9", "width": 1280, "height": 720}],
            "lane_contract": {"parameters": [
                {"name": "aspect_ratio", "enum": ["16:9"], "default": "16:9"},
            ]},
        },
    })
    music = catalog._music_row("song", _definition("studio_hub"), {
        "model_id": "song", "display_name": "Song",
        "raw": {"lane_contract": {"parameters": [
            {"name": "lyrics", "required": True},
            {"name": "vocal_language", "enum": ["en", "km"]},
            {"name": "duration_seconds", "minimum": 8, "maximum": 360, "default": 36},
        ]}},
    })
    video = catalog._video_row("clip", _definition("studio_hub"), {
        "model_id": "clip", "display_name": "Clip",
        "raw": {"video_modes": ["text-to-video"], "aspect_ratios": ["16:9"],
                "durations": [5], "resolutions": ["720p"]},
    })
    return {"image": image, "music": music, "video": video}


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = _fixture_rows()

    def test_every_control_bearing_key_has_a_reader_or_a_recorded_exception(self) -> None:
        checked = 0
        for modality, row in self.rows.items():
            for key in row:
                if key in READERS:
                    reader = READERS[key]
                    self.assertIn(f"function {reader}", PAGE,
                                  f"{modality} row publishes `{key}` but the page has no {reader}()")
                    checked += 1
                elif key in EXCEPTIONS:
                    self.assertTrue(EXCEPTIONS[key], f"`{key}` has an empty exception reason")
        self.assertGreaterEqual(checked, 4, "the fixture must exercise real controls")

    def test_the_page_reads_no_capability_the_rows_do_not_publish(self) -> None:
        published = {key for row in self.rows.values() for key in row}
        for key, reader in READERS.items():
            if key in ("upload_limit_bytes",):
                continue  # transcription's row, not one of these three builders
            self.assertIn(f"function {reader}", PAGE)
            self.assertIn(key, published,
                          f"the page draws `{key}` but no row builder here publishes it — "
                          "a control reading a field the catalogue stopped sending is dead")

    def test_a_lyrics_required_lane_hides_instrumental(self) -> None:
        music = self.rows["music"]
        self.assertTrue(music.get("requires_lyrics"))
        self.assertFalse(music.get("supports_instrumental"),
                         "YuE2's own contract says it is lyrics-only; publishing an "
                         "instrumental switch would offer a control the lane refuses")

    def test_the_live_catalogue_still_agrees_when_it_has_rows(self) -> None:
        """Walk the live catalogue when this profile has one; say so when it does not."""
        live = {modality: live_catalog.rows(modality)
                for modality in ("image", "music", "video")}
        if not any(live.values()):
            self.skipTest("no live catalogue rows in this isolated profile — not checked")
        for modality, rows in live.items():
            for row in rows:
                for key in ("aspect_ratios", "output_options"):
                    if key in row and key in READERS:
                        self.assertIn(f"function {READERS[key]}", PAGE)
                # The one invariant every image row must hold, pool aliases included.
                if modality == "image" and row.get("aspect_ratios"):
                    for ratio in row["aspect_ratios"]:
                        self.assertTrue(
                            any(option.get("aspect_ratio") == ratio
                                for option in (row.get("output_options") or [])),
                            f"{row.get('id')} declares {ratio} with no output option")


if __name__ == "__main__":
    unittest.main()
