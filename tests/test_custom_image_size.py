#!/usr/bin/env python3
"""A custom size is refused, never silently replaced (0.2.292).

**The defect.** `definitions.image_size_for` honoured a caller's width/height *in orientation only*
and then substituted the preset for that orientation. A request for 1344x768 on a 16:9 lane was
answered with the lane's 16:9 preset (1536x1024 on the cloud table) and reported success — the
picture is not the size that was asked for, and nothing in the record said so. That is the same
class as the empty description and the 45,284 polls: a system answering a question it was not asked
and reporting success.

What must be true instead:

1. Geometry that **matches** the preset is sent as it is — no refusal for agreeing with us.
2. Geometry that does not match raises, and the adapter turns it into `INVALID_SIZE`, which is not
   retryable and not re-routed: every machine would refuse the identical numbers.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-custom-image-size-")

from gateway import worker  # noqa: E402
from gateway.adapters.base import BackendError  # noqa: E402
from gateway.adapters.openai_compat import _size_for  # noqa: E402
from gateway.providers.definitions import IMAGE_SIZE_BY_RATIO, image_size_for  # noqa: E402

WIDE = IMAGE_SIZE_BY_RATIO["16:9"]


class ACustomSizeIsRefusedTest(unittest.TestCase):
    def test_the_preset_is_used_when_no_geometry_is_given(self) -> None:
        self.assertEqual(image_size_for("16:9"), f"{WIDE[0]}x{WIDE[1]}")

    def test_geometry_that_matches_the_preset_is_sent(self) -> None:
        self.assertEqual(image_size_for("16:9", WIDE[0], WIDE[1]), f"{WIDE[0]}x{WIDE[1]}")

    def test_geometry_that_does_not_match_is_refused_and_says_both(self) -> None:
        with self.assertRaises(ValueError) as caught:
            image_size_for("16:9", 1344, 768)
        message = str(caught.exception)
        self.assertIn("1344x768", message)
        self.assertIn(f"{WIDE[0]}x{WIDE[1]}", message)
        self.assertNotIn("1344x768 replaced", message)

    def test_the_refusal_names_what_it_can_render(self) -> None:
        """A caller told only "unsupported" will guess; the alternatives are the useful half."""
        with self.assertRaises(ValueError) as caught:
            image_size_for("16:9", 1344, 768)
        message = str(caught.exception)
        for size in {f"{w}x{h}" for w, h in IMAGE_SIZE_BY_RATIO.values()}:
            self.assertIn(size, message, f"the refusal does not name {size}")

    def test_the_adapter_reports_it_as_invalid_size(self) -> None:
        with self.assertRaises(BackendError) as caught:
            _size_for({"aspect_ratio": "16:9", "width": 1344, "height": 768})
        self.assertEqual(caught.exception.code, "INVALID_SIZE")
        self.assertFalse(caught.exception.retryable,
                         "the same numbers fail on every machine; a retry is not the answer")

    def test_a_request_the_model_can_render_still_goes(self) -> None:
        self.assertEqual(_size_for({"aspect_ratio": "16:9", "width": WIDE[0], "height": WIDE[1]}),
                         f"{WIDE[0]}x{WIDE[1]}")

    def test_it_is_not_re_routed_to_another_machine(self) -> None:
        self.assertIn("INVALID_SIZE", worker._NO_REROUTE_CODES)


if __name__ == "__main__":
    unittest.main()
