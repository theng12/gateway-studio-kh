#!/usr/bin/env python3
"""The image-reference capability gate must only allow kinds that actually send staged bytes.

`IMAGE_REFERENCE_CAPABLE_KINDS` is shared by the publication gate (`can_publish` / `withhold_reason`)
so a reference-only model is only offered when its kind's image adapter can put the staged reference
bytes on the wire. `kie` was falsely included in that set, although the Kie image adapter never reads
or forwards `_reference_paths` — while Studio Hub's adapter does. This test pins the gate so Kie's
reference-only rows stay withheld, Kie's plain text-to-image rows keep publishing, and Studio Hub's
reference rows remain available.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-image-ref-cap-gate-")

from gateway.providers.credentials import CredentialStore  # noqa: E402
from gateway.providers.definitions import (  # noqa: E402
    PROVIDERS_BY_ID, image_reference_state,
)
from gateway.providers.models import ModelInventory  # noqa: E402


class ImageReferenceCapabilityGateTests(unittest.TestCase):
    """Kie must not be falsely included in `IMAGE_REFERENCE_CAPABLE_KINDS`."""

    def setUp(self) -> None:
        self.inventory = ModelInventory(
            CredentialStore(path=Path(_SANDBOX.name) / "credentials.json")
        )

    def test_kie_reference_only_row_is_withheld(self) -> None:
        definition = PROVIDERS_BY_ID["kie-ai-images"]
        model = {"model_id": "kie/edit-model", "modality": "image",
                 "raw": {"category": "image-to-image", "capabilities": ["img2img"]}}
        reason = self.inventory.withhold_reason(definition, model)
        self.assertIsNotNone(reason, "reference-only Kie model should be withheld")
        self.assertIn("reference image", reason)

    def test_kie_normal_text_image_stays_published(self) -> None:
        definition = PROVIDERS_BY_ID["kie-ai-images"]
        model = {"model_id": "kie/text-model", "modality": "image",
                 "raw": {"category": "text-to-image", "capabilities": ["txt2img"]}}
        self.assertTrue(self.inventory.can_publish(definition, model))

    def test_hub_reference_support_never_comes_from_a_model_name(self) -> None:
        self.assertIsNone(image_reference_state("studio_hub", "maker/klein-edit-img2img", {}))
        self.assertFalse(image_reference_state("studio_hub", "maker/edit", {"capabilities": ["txt2img"]}))
        self.assertTrue(image_reference_state("studio_hub", "maker/plain", {"capabilities": ["reference-to-image"]}))

    def test_studio_hub_reference_row_remains_published(self) -> None:
        definition = PROVIDERS_BY_ID["studio-hub-local"]
        model = {"model_id": "studio-hub/image-ref-model", "modality": "image",
                 "raw": {"capabilities": ["txt2img", "edit"], "lane_contract": {
                     "references": {"min": 1, "max": 4}}}}
        self.assertTrue(self.inventory.can_publish(definition, model))


if __name__ == "__main__":
    unittest.main(verbosity=2)
