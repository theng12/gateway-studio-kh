from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Temp dir, not the repo's `_trash` (a symlink): RenderAssetStore refuses a user-owned symlink in
# the data path as unsafe, so a profile under it cannot boot.
RUN_ROOT = Path(tempfile.mkdtemp(prefix="gateway-render-descriptors-"))
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(RUN_ROOT),
    "GATEWAY_DATA": str(RUN_ROOT / "data"),
    "GATEWAY_ARTIFACTS": str(RUN_ROOT / "artifacts"),
    "GATEWAY_DB": str(RUN_ROOT / "gateway.sqlite3"),
    "GATEWAY_CREDENTIALS": str(RUN_ROOT / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(RUN_ROOT / "models.json"),
    "GATEWAY_ENV_FILE": str(RUN_ROOT / ".env"),
    "GATEWAY_FLEET": str(RUN_ROOT / "fleet.json"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(RUN_ROOT / "no-hub.json"),
})

from gateway import runtime  # noqa: E402


_DIGEST = "a" * 64
_BOUNDS = {
    "bounds_version": 2,
    "canvas_range": {"max_edge": 3840, "max_pixels": 8294400},
    "working_fps": [24, 30],
    "max_duration_sec": 60,
    "max_operations": 32,
    "max_assets": 32,
    "max_asset_bytes": 1000,
    "max_total_input_bytes": 2000,
    "max_pixel_frames": 100000,
    "scratch_reserve_bytes": 100,
    "output_limit_bytes": 1000,
    "process_tree_rss_limit_bytes": 1000,
    "cumulative_wall_seconds": 60,
}


def _descriptor_set(root: Path, *, late_label: bool) -> None:
    directory = root / ("final-render-late-label" if late_label else "final-render")
    directory.mkdir()
    tools = {
        "format": "studio.render.tools.v1", "platform": "macos-arm64",
        "ffmpeg_sha256": _DIGEST, "ffmpeg_size_bytes": 1,
        "ffprobe_sha256": _DIGEST, "ffprobe_size_bytes": 1,
        "architecture_evidence_sha256": _DIGEST,
        "minimum_os_evidence_sha256": _DIGEST,
        "linked_libraries_evidence_sha256": _DIGEST,
        "build_configuration_evidence_sha256": _DIGEST,
        "capability_inventory_evidence_sha256": _DIGEST,
    }
    compiler = {
        "format": "studio.render.compiler.v1",
        "implementation": "hub-final-render-late-label-v3" if late_label else "hub-final-render-v1",
        "python_runtime_sha256": _DIGEST, "render_hash_sha256": _DIGEST,
        "render_contract_sha256": _DIGEST, "final_render_compiler_sha256": _DIGEST,
    }
    tool_bytes = json.dumps(tools, separators=(",", ":")).encode()
    compiler_bytes = json.dumps(compiler, separators=(",", ":")).encode()
    profile = {
        "format": "studio.render.profile.v3" if late_label else "studio.render.profile.v1",
        "implementation": "story-classic-late-label-v3" if late_label else "story-classic-no-font-v1",
        "recipe_namespace": "studio.render.final.v1",
        "compiler_artifact_sha256": hashlib.sha256(compiler_bytes).hexdigest(),
        "tools_artifact_sha256": hashlib.sha256(tool_bytes).hexdigest(),
        "combination_set": "retained-classic-late-label-candidate-v3" if late_label else "retained-classic-no-font-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
        "execution_bounds": _BOUNDS,
        "hardware_qualification_record_sha256": None,
        "fidelity_record_sha256": None, "production_eligible": False,
    }
    for name, value in (("tools.json", tools), ("compiler.json", compiler), ("profile.json", profile)):
        (directory / name).write_text(json.dumps(value, separators=(",", ":")))


class BundledRenderDescriptorTests(unittest.TestCase):
    def test_optional_v3_triple_is_loaded_only_for_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _descriptor_set(root, late_label=True)
            with patch.object(runtime, "BUNDLED_DATA_DIR", root):
                self.assertEqual(runtime._bundled_render_descriptors()[0], None)
                profile, facts = runtime._bundled_render_descriptors("studio.render.late-label.v3")
            self.assertIsNotNone(profile)
            self.assertEqual(json.loads(profile)["production_eligible"], False)
            self.assertEqual(facts["compiler"]["digest"], hashlib.sha256(
                (root / "final-render-late-label" / "compiler.json").read_bytes()
            ).hexdigest())

    def test_v3_rejects_wrong_compiler_and_non_false_production_eligibility(self) -> None:
        for field, value in (("implementation", "hub-final-render-v1"),
                             ("production_eligible", True)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _descriptor_set(root, late_label=True)
                path = root / "final-render-late-label" / ("compiler.json" if field == "implementation" else "profile.json")
                descriptor = json.loads(path.read_text())
                descriptor[field] = value
                path.write_text(json.dumps(descriptor, separators=(",", ":")))
                with patch.object(runtime, "BUNDLED_DATA_DIR", root):
                    self.assertEqual(
                        runtime._bundled_render_descriptors("studio.render.late-label.v3"),
                        (None, {}),
                    )

    def test_legacy_default_still_loads_without_optional_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _descriptor_set(root, late_label=False)
            with patch.object(runtime, "BUNDLED_DATA_DIR", root):
                profile, facts = runtime._bundled_render_descriptors()
                late_profile, late_facts = runtime._bundled_render_descriptors(
                    "studio.render.late-label.v3"
                )
            self.assertIsNotNone(profile)
            self.assertTrue(facts["compiler"])
            self.assertIsNone(late_profile)
            self.assertEqual(late_facts, {})


if __name__ == "__main__":
    unittest.main()
