"""Synthetic subtitle descriptor bundle, loader and runtime wiring checks.

The real Hub-frozen bytes and digests are injected later; every fixture here is
explicitly synthetic and must never be treated as production authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN_ROOT = ROOT / "_trash" / "tests" / "gateway-render-subtitle-profiles"
RUN_ROOT.mkdir(parents=True, exist_ok=True)
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
from gateway.render_contract import SUBTITLE_CONTRACT  # noqa: E402


_DIGEST = "a" * 64
_BOUNDS = {
    "bounds_version": 2,
    "canvas_range": {"max_edge": 3840, "max_pixels": 8294400},
    "working_fps": [24, 30],
    "max_duration_sec": 60,
    "max_operations": 32,
    "max_assets": 8,
    "max_asset_bytes": 5000,
    "max_total_input_bytes": 20000,
    "max_pixel_frames": 1000000,
    "scratch_reserve_bytes": 300,
    "output_limit_bytes": 5000,
    "process_tree_rss_limit_bytes": 1000,
    "cumulative_wall_seconds": 60,
    "max_subtitle_fonts": 8,
    "max_subtitle_font_bytes": 33554432,
    "max_subtitle_ass_bytes": 4194304,
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _bundle_values() -> dict[str, dict]:
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
        "implementation": "hub-final-render-subtitle-v1",
        "python_runtime_sha256": _DIGEST, "render_hash_sha256": _DIGEST,
        "render_contract_sha256": _DIGEST, "final_render_compiler_sha256": _DIGEST,
    }
    profile = {
        "format": "studio.render.profile.v4",
        "implementation": "story-classic-subtitle-v1",
        "recipe_namespace": "studio.render.final.v1",
        "compiler_artifact_sha256": hashlib.sha256(_canonical(compiler)).hexdigest(),
        "tools_artifact_sha256": hashlib.sha256(_canonical(tools)).hexdigest(),
        "combination_set": "retained-classic-subtitle-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
        "execution_bounds": copy.deepcopy(_BOUNDS),
        "hardware_qualification_record_sha256": None,
        "fidelity_record_sha256": None, "production_eligible": False,
    }
    return {"profile": profile, "tools": tools, "compiler": compiler}


def _write_bundle(root: Path, *, change=None) -> dict[str, bytes]:
    values = _bundle_values()
    if change is not None:
        change(values)
    payloads = {name + ".json": _canonical(value) for name, value in values.items()}
    directory = root / "final-render-subtitle"
    directory.mkdir(parents=True)
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
    return payloads


class SubtitleDescriptorLoaderTests(unittest.TestCase):
    def load(self, root: Path) -> tuple[bytes | None, dict]:
        with mock.patch.object(runtime, "BUNDLED_DATA_DIR", root):
            return runtime._bundled_render_descriptors(SUBTITLE_CONTRACT)

    def test_exact_synthetic_triple_loads_with_its_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = _write_bundle(root)
            profile, facts = self.load(root)
        self.assertEqual(profile, payloads["profile.json"])
        self.assertEqual(facts["tools"]["descriptor_utf8"],
                         payloads["tools.json"].decode("utf-8"))
        self.assertEqual(facts["compiler"]["digest"],
                         hashlib.sha256(payloads["compiler.json"]).hexdigest())

    def test_subtitle_bundle_is_invisible_to_other_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_bundle(root)
            with mock.patch.object(runtime, "BUNDLED_DATA_DIR", root):
                self.assertEqual(runtime._bundled_render_descriptors(), (None, {}))
                self.assertEqual(
                    runtime._bundled_render_descriptors("studio.render.late-label.v3"),
                    (None, {}),
                )

    def test_absent_bundle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(self.load(Path(temporary)), (None, {}))

    def test_wrong_literal_digest_shape_or_eligibility_fails_closed(self) -> None:
        mutations = (
            ("profile format", lambda v: v["profile"].update(format="studio.render.profile.v3")),
            ("profile implementation", lambda v: v["profile"].update(
                implementation="story-classic-subtitle-v0")),
            ("profile combination set", lambda v: v["profile"].update(
                combination_set="retained-classic-late-label-candidate-v3")),
            ("production eligible", lambda v: v["profile"].update(production_eligible=True)),
            ("compiler implementation", lambda v: v["compiler"].update(
                implementation="hub-final-render-late-label-v3")),
            ("extra tools key", lambda v: v["tools"].update(unexpected=True)),
            ("wrong tools digest", lambda v: v["profile"].update(tools_artifact_sha256="b" * 64)),
            ("wrong compiler digest", lambda v: v["profile"].update(
                compiler_artifact_sha256="b" * 64)),
        )
        for label, change in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_bundle(root, change=change)
                self.assertEqual(self.load(root), (None, {}))

    def test_extra_descriptor_breaks_the_exact_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_bundle(root)
            (root / "final-render-subtitle" / "extra.json").write_bytes(b"{}")
            self.assertEqual(self.load(root), (None, {}))

    def test_missing_or_invalid_subtitle_bound_keys_fail_closed(self) -> None:
        for key in ("max_subtitle_fonts", "max_subtitle_font_bytes", "max_subtitle_ass_bytes"):
            with self.subTest(missing=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_bundle(root, change=lambda v: v["profile"]["execution_bounds"].pop(key))
                self.assertEqual(self.load(root), (None, {}))
        for key, value in (("max_subtitle_fonts", 0), ("max_subtitle_font_bytes", True),
                           ("max_subtitle_ass_bytes", "4194304")):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_bundle(root, change=lambda v: v["profile"]["execution_bounds"].update(
                    {key: value}))
                self.assertEqual(self.load(root), (None, {}))

    def test_legacy_subtitle_bounds_shape_is_rejected(self) -> None:
        def legacy(values: dict) -> None:
            bounds = values["profile"]["execution_bounds"]
            del bounds["bounds_version"], bounds["canvas_range"]
            bounds["canvas_pairs"] = [{"width": 1920, "height": 1080}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_bundle(root, change=legacy)
            self.assertEqual(self.load(root), (None, {}))

    def test_base_profile_does_not_accept_subtitle_bound_keys(self) -> None:
        from tests import test_task6_packaging as packaging
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payloads = dict(packaging._authority_files())
            profile = json.loads(payloads["profile.json"])
            profile["execution_bounds"].update({
                "max_subtitle_fonts": 8,
                "max_subtitle_font_bytes": 33554432,
                "max_subtitle_ass_bytes": 4194304,
            })
            payloads["profile.json"] = packaging._canonical(profile)
            directory = root / "final-render"
            directory.mkdir()
            for name, payload in payloads.items():
                (directory / name).write_bytes(payload)
            with mock.patch.object(runtime, "BUNDLED_DATA_DIR", root):
                self.assertEqual(runtime._bundled_render_descriptors(), (None, {}))

    def test_runtime_wires_the_loaded_profile_and_asset_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_bundle(root)
            script = """
import hashlib, json, sys
from pathlib import Path
from gateway import config
root = Path(sys.argv[1])
config.BUNDLED_DATA_DIR = root
from gateway import runtime
bundle = root / "final-render-subtitle"
profile = (bundle / "profile.json").read_bytes()
service = runtime.render_service
assert service.subtitle_profile_descriptor == profile
assert service.subtitle_profile_facts["tools"]["digest"] == hashlib.sha256(
    (bundle / "tools.json").read_bytes()).hexdigest()
assert service.subtitle_profile_facts["compiler"]["digest"] == hashlib.sha256(
    (bundle / "compiler.json").read_bytes()).hexdigest()
bounds = json.loads(profile)["execution_bounds"]
assert runtime.render_assets.max_asset_bytes == bounds["max_asset_bytes"]
assert runtime.render_assets.reserve_bytes == bounds["scratch_reserve_bytes"]
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(root)],
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class SubtitleBundleBuildTests(unittest.TestCase):
    def build(self, *, change=None):
        from tests import test_task6_packaging as packaging
        fixture = packaging._BuildFixture()
        fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
        payloads = _write_bundle(fixture.root / "data", change=change)
        result = fixture.run("--render-authority", "descriptor-only")
        return fixture, payloads, result

    def test_descriptor_build_stages_the_exact_subtitle_bytes(self) -> None:
        fixture, payloads, result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = (fixture.root / "menubar/build/gateway-kh.app/Contents/Resources"
                  / "gateway/data/final-render-subtitle")
        self.assertEqual(sorted(path.name for path in staged.iterdir()), sorted(payloads))
        for name, payload in payloads.items():
            self.assertEqual((staged / name).read_bytes(), payload)

    def test_wrong_subtitle_literal_fails_the_build(self) -> None:
        fixture, _payloads, result = self.build(
            change=lambda v: v["profile"].update(format="studio.render.profile.v3")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile fixed fields", result.stderr)

    def test_subtitle_bounds_missing_invalid_or_legacy_fail_the_build(self) -> None:
        def missing(values: dict) -> None:
            values["profile"]["execution_bounds"].pop("max_subtitle_fonts")

        def invalid(values: dict) -> None:
            values["profile"]["execution_bounds"]["max_subtitle_font_bytes"] = 0

        def legacy(values: dict) -> None:
            bounds = values["profile"]["execution_bounds"]
            del bounds["bounds_version"], bounds["canvas_range"]
            bounds["canvas_pairs"] = [{"width": 1920, "height": 1080}]

        for label, change, expected in (
                ("missing", missing, "profile.execution_bounds has missing or unknown fields"),
                ("invalid", invalid, "max_subtitle_font_bytes"),
                ("legacy", legacy, "profile execution bounds version")):
            with self.subTest(label=label):
                _fixture, _payloads, result = self.build(change=change)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    unittest.main()
