#!/usr/bin/env python3
"""Focused Task 6 checks for portable defaults and packaging identity."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUILD_SCRIPT = ROOT / "menubar" / "build.sh"
# Temp, not the repo's `_trash`: that is a symlink to the shared trash, so paths through it are
# shallower than their string form and the relative-path acceptance cases below mis-resolve.
# realpath the base too: $TMPDIR is `/var/...`, a link to `/private/var`, and `..` past it in a
# relative path lands outside `/`.
ARTIFACT_ROOT = Path(os.path.realpath(tempfile.mkdtemp(prefix="gateway-task6-packaging-")))
# The staged bundle is ~700 MB; without this every run leaves a copy in TMPDIR, failed runs too.
atexit.register(shutil.rmtree, ARTIFACT_ROOT, ignore_errors=True)
RUN_ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=ARTIFACT_ROOT))
PY_VERSION = "3.11.16"
PY_TAG = "20260901"
PY_ARCH = "aarch64"
ACCEPT_SCRIPT = ROOT / "scripts" / "accept_release.py"
RELEASE_DIST_HELPER = Path(
    "/build/Developer/Shared Tools/portfolio/tools/release-dist.py"
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _authority_files() -> dict[str, bytes]:
    tools = {
        "format": "studio.render.tools.v1", "platform": "macos-arm64",
        "ffmpeg_sha256": "1" * 64, "ffmpeg_size_bytes": 10,
        "ffprobe_sha256": "2" * 64, "ffprobe_size_bytes": 11,
        "architecture_evidence_sha256": "3" * 64,
        "minimum_os_evidence_sha256": "4" * 64,
        "linked_libraries_evidence_sha256": "5" * 64,
        "build_configuration_evidence_sha256": "6" * 64,
        "capability_inventory_evidence_sha256": "7" * 64,
    }
    tools_raw = _canonical(tools)
    compiler_raw = _canonical({
        "format": "studio.render.compiler.v1", "implementation": "hub-final-render-v1",
        "python_runtime_sha256": "8" * 64, "render_hash_sha256": "9" * 64,
        "render_contract_sha256": "a" * 64,
        "final_render_compiler_sha256": "b" * 64,
    })
    execution_bounds = {
        "bounds_version": 2,
        "canvas_range": {"max_edge": 3840, "max_pixels": 8294400},
        "working_fps": [24, 25, 30, 60],
        "max_duration_sec": 60, "max_operations": 32, "max_assets": 32,
        "max_asset_bytes": 32 * 1024 * 1024,
        "max_total_input_bytes": 64 * 1024 * 1024,
        "max_pixel_frames": 1920 * 1080 * 24 * 60,
        "scratch_reserve_bytes": 1, "output_limit_bytes": 32 * 1024 * 1024,
        "process_tree_rss_limit_bytes": 1, "cumulative_wall_seconds": 60,
    }
    profile_raw = _canonical({
        "format": "studio.render.profile.v1",
        "implementation": "story-classic-no-font-v1",
        "recipe_namespace": "studio.render.final.v1",
        "compiler_artifact_sha256": hashlib.sha256(compiler_raw).hexdigest(),
        "tools_artifact_sha256": hashlib.sha256(tools_raw).hexdigest(),
        "combination_set": "retained-classic-no-font-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
        "execution_bounds": execution_bounds,
        "hardware_qualification_record_sha256": None,
        "fidelity_record_sha256": None, "production_eligible": False,
    })
    return {
        "profile.json": profile_raw, "tools.json": tools_raw,
        "compiler.json": compiler_raw,
    }


class _BuildFixture:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="build-", dir=RUN_ROOT))
        self.menubar = self.root / "menubar"
        self.menubar.mkdir()
        (self.root / "data").mkdir()
        shutil.copytree(ROOT / "gateway", self.root / "gateway")
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.asset = self.root / "python-asset.tar.gz"
        self.pip_count = self.root / "pip-count"
        self.codesign_log = self.root / "codesign.log"
        self.codesign_calls = self.root / "codesign-calls.log"

        shutil.copy2(BUILD_SCRIPT, self.menubar / "build.sh")
        shutil.copy2(ROOT / "menubar" / "make_icon.py", self.menubar / "make_icon.py")
        (self.menubar / "main.swift").write_text("import Cocoa\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text("fixture\n", encoding="utf-8")
        shutil.copy2(ROOT / "requirements.txt", self.root / "requirements.txt")
        lock = ROOT / "requirements.lock"
        if lock.exists():
            shutil.copy2(lock, self.root / lock.name)
        else:
            (self.root / "requirements.lock").write_text("# fixture lock\n", encoding="utf-8")
        shutil.copy2(ROOT / "data" / "voices.json", self.root / "data" / "voices.json")

        runtime_root = self.root / "asset-root" / "python" / "bin"
        runtime_root.mkdir(parents=True)
        _write_executable(
            runtime_root / "python3",
            """
if [ "${1:-}" = "--version" ]; then
  echo "Python 3.11.16"
  exit 0
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  count=0
  if [ -f "$TASK6_PIP_COUNT" ]; then count=$(cat "$TASK6_PIP_COUNT"); fi
  printf '%s\\n' "$((count + 1))" > "$TASK6_PIP_COUNT"
  exit 0
fi
if [ "${1:-}" = "-c" ]; then
  echo "${TASK6_RUNTIME_MACHINE:-arm64}"
  exit 0
fi
if [ "${1:-}" = "-" ]; then
  if [ "${TASK6_SWAP_AUTHORITY_FOR_VALIDATION:-0}" = "1" ]; then
    source="$2"
    evidence="$TASK6_VALIDATION_SWAP_EVIDENCE"
    mkdir -p "$evidence"
    for name in profile.json tools.json compiler.json; do
      cp "$source/$name" "$evidence/$name"
      cp "$TASK6_VALID_AUTHORITY_SOURCE/$name" "$source/$name"
    done
    set +e
    "$TASK6_VALIDATOR_PYTHON" "$@"
    status=$?
    set -e
    for name in profile.json tools.json compiler.json; do
      cp "$evidence/$name" "$source/$name"
    done
    exit "$status"
  fi
  exec "$TASK6_VALIDATOR_PYTHON" "$@"
fi
if [ "${TASK6_FAIL_ICON:-0}" = "1" ]; then
  echo "fixture icon generation failed" >&2
  exit 77
fi
last=""
for arg in "$@"; do last="$arg"; done
printf 'fixture icns\\n' > "$last"
""",
        )
        with tarfile.open(self.asset, "w:gz") as archive:
            archive.add(self.root / "asset-root" / "python", arcname="python")

        _write_executable(
            self.bin / "curl",
            """
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then shift; output="$1"; fi
  shift
done
cp "$TASK6_ASSET_SOURCE" "$output"
""",
        )
        _write_executable(
            self.bin / "swiftc",
            """
output=""
previous=""
for arg in "$@"; do
  if [ "$previous" = "-o" ]; then output="$arg"; fi
  previous="$arg"
done
mkdir -p "$(dirname "$output")"
printf '#!/bin/sh\\nexit 0\\n' > "$output"
chmod +x "$output"
""",
        )
        _write_executable(
            self.bin / "codesign",
            """
if [ "${TASK6_FAIL_CODESIGN:-0}" = "1" ]; then
  echo "fixture codesign failed" >&2
  exit 77
fi
printf '%s\\n' "$*" >> "$TASK6_CODESIGN_CALLS"
if [ "${1:-}" = "--verify" ]; then
  printf 'verified\\n' >> "$TASK6_CODESIGN_LOG"
fi
""",
        )
        # The real Keychain is never read: the fixture states which identities exist.
        _write_executable(
            self.bin / "security",
            """
printf '%s\\n' "${TASK6_SECURITY_OUTPUT:-}"
""",
        )
        # hdiutil is a native tool; the fixture writes the named image instead.
        _write_executable(
            self.bin / "hdiutil",
            """
for arg in "$@"; do output="$arg"; done
mkdir -p "$(dirname "$output")"
printf 'fixture dmg\\n' > "$output"
""",
        )

    def __enter__(self) -> "_BuildFixture":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "TASK6_ASSET_SOURCE": str(self.asset),
            "TASK6_PIP_COUNT": str(self.pip_count),
            "TASK6_CODESIGN_LOG": str(self.codesign_log),
            "TASK6_CODESIGN_CALLS": str(self.codesign_calls),
            "GATEWAY_SIGN_IDENTITY": "",
            "TASK6_VALIDATOR_PYTHON": sys.executable,
            "GATEWAY_PYTHON_URL": "https://fixture.invalid/python.tar.gz",
        }

    def write_authority(self, names: tuple[str, ...]) -> dict[str, bytes]:
        payloads = _authority_files()
        root = self.root / "data/final-render"
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / name).write_bytes(payloads[name])
        return payloads

    def run(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.environment()
        env.update(overrides)
        return subprocess.run(
            ["sh", str(self.menubar / "build.sh"), "--no-install", *arguments],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


class Task6Packaging(unittest.TestCase):
    def test_packaged_descriptors_match_the_hub_bytes(self) -> None:
        configured = os.environ.get("STUDIOHUB_RENDER_PROFILE")
        peer = (Path(configured) if configured else
                ROOT.parents[1] / "studio-hub-kh" / ROOT.name
                / "packaging/render-authority/PROFILE.json")
        if not peer.is_file():
            self.skipTest("Set STUDIOHUB_RENDER_PROFILE to the Hub's packaged PROFILE.json")
        self.assertEqual((ROOT / "data/final-render/profile.json").read_bytes(), peer.read_bytes())

    def test_packaged_profile_preserves_all_other_resource_fences(self) -> None:
        profile = json.loads((ROOT / "data/final-render/profile.json").read_bytes())
        self.assertIs(profile["production_eligible"], False)
        self.assertEqual(profile["execution_bounds"], {
            "bounds_version": 2,
            "canvas_range": {"max_edge": 3840, "max_pixels": 8294400},
            "working_fps": [24, 25, 30, 60],
            "max_duration_sec": 10800, "max_operations": 8, "max_assets": 4,
            "max_asset_bytes": 8589934592, "max_total_input_bytes": 17179869184,
            "max_pixel_frames": 5374771200000, "scratch_reserve_bytes": 17179869184,
            "output_limit_bytes": 8589934592, "process_tree_rss_limit_bytes": 10737418240,
            "cumulative_wall_seconds": 14400,
        })

    def test_build_and_runtime_reject_malformed_v2_bounds(self) -> None:
        mutations = (
            lambda b: b.update(bounds_version=3),
            lambda b: b.update(bounds_version=True),
            lambda b: b.update(bounds_version=2.0),
            lambda b: b.pop("bounds_version"),
            lambda b: b.update(canvas_pairs=[]),
            lambda b: b["canvas_range"].update(extra=1),
            lambda b: b["canvas_range"].pop("max_edge"),
            lambda b: b.update(canvas_range=[]),
            lambda b: b["canvas_range"].update(max_edge=3841),
            lambda b: b["canvas_range"].update(max_edge=True),
            lambda b: b["canvas_range"].update(max_pixels=0),
            lambda b: b["canvas_range"].update(max_pixels=8294400.0),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), _BuildFixture() as fixture:
                payloads = fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
                profile = json.loads(payloads["profile.json"])
                mutate(profile["execution_bounds"])
                (fixture.root / "data/final-render/profile.json").write_bytes(_canonical(profile))
                result = fixture.run("--render-authority", "descriptor-only")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Built:", result.stdout + result.stderr)

    def test_shipped_catalogue_is_an_empty_default(self) -> None:
        payload = json.loads((ROOT / "data" / "voices.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("voices"), [])

    def test_fresh_profile_has_no_ghost_voice_and_import_resolves_its_clip(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="profile-", dir=RUN_ROOT))
        with self.subTest(retained=base):
            fixture = base / "fixture"
            fixture.mkdir()
            (fixture / "Morgan.wav").write_bytes(b"fixture audio bytes")
            (fixture / "Morgan.txt").write_text("fixture transcript", encoding="utf-8")
            (fixture / "Morgan.json").write_text(
                json.dumps({"name": "Morgan Fixture", "language": "en", "gender": "f"}),
                encoding="utf-8",
            )
            env = dict(os.environ)
            for key in (
                "GATEWAY_VOICES",
                "GATEWAY_VOICES_OUT",
                "GATEWAY_NO_MIGRATE",
                "STUDIOHUB_READY_FILE",
            ):
                env.pop(key, None)
            env.update({
                "PYTHONPATH": str(ROOT),
                "GATEWAY_APP_SUPPORT": str(base / "app-support"),
                "GATEWAY_DATA": str(base / "data"),
                "GATEWAY_ARTIFACTS": str(base / "data" / "artifacts"),
                "GATEWAY_DB": str(base / "data" / "gateway.sqlite3"),
                "GATEWAY_CREDENTIALS": str(base / "data" / "credentials.json"),
                "GATEWAY_MODEL_CACHE": str(base / "data" / "models_cache.json"),
                "GATEWAY_ENV_FILE": str(base / ".env"),
                "GATEWAY_NO_MIGRATE": "1",
                "STUDIOHUB_READY_FILE": str(base / "no-hub-ready.json"),
                "TASK6_FIXTURE": str(fixture),
            })
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import os
from pathlib import Path
from gateway.voice_import import import_folder
from gateway.voices import _local_voices, voice_registry

initial = _local_voices()
enabled = voice_registry().enabled_voices()
if initial or enabled:
    raise SystemExit(f"fresh profile exposed local={len(initial)} enabled={len(enabled)} voices")
result = import_folder(Path(os.environ["TASK6_FIXTURE"]))
if len(result["imported"]) != 1:
    raise SystemExit(f"import result: {result}")
loaded = _local_voices()
if len(loaded) != 1:
    raise SystemExit(f"loaded voices: {len(loaded)}")
if len(voice_registry().enabled_voices()) != 1:
    raise SystemExit("imported voice was not usable")
voice = loaded[0]
clip = Path(str(voice.raw.get("reference_path") or ""))
if not clip.is_file() or clip.read_bytes() != b"fixture audio bytes":
    raise SystemExit(f"unresolved clip: {clip}")
if voice.raw.get("reference_transcript") != "fixture transcript":
    raise SystemExit(f"unresolved transcript: {voice.raw.get('reference_transcript')!r}")
print(f"fresh=0 imported={voice.display_name} clip={clip}")
""",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_development_and_bundle_use_a_locked_dependency_input(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("-c requirements.lock", requirements)
        lock = ROOT / "requirements.lock"
        self.assertTrue(lock.is_file())
        for package in ("fastapi", "uvicorn", "httpx", "python-multipart"):
            self.assertRegex(lock.read_text(encoding="utf-8"), rf"(?m)^{package}==")

    def test_native_launch_does_not_modify_signed_python_resources(self) -> None:
        launcher = (ROOT / "menubar/main.swift").read_text()
        arguments = launcher.split("private func spawn(", 1)[1].split("task.currentDirectoryURL", 1)[0]
        self.assertIn('"-B", "-m", "uvicorn"', arguments)

    def test_runtime_cache_changes_for_lock_and_python_asset(self) -> None:
        with _BuildFixture() as fixture:
            # The default is descriptor-only, so a build that is meant to succeed needs a
            # truthful selected source; the fixture writes the same three descriptors the
            # default now requires.
            fixture.write_authority(tuple(_authority_files()))
            first = fixture.run()
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(fixture.pip_count.read_text().strip(), "1")

            second = fixture.run()
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(fixture.pip_count.read_text().strip(), "1")

            lock = fixture.root / "requirements.lock"
            lock.write_text(lock.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
            third = fixture.run()
            self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
            self.assertEqual(fixture.pip_count.read_text().strip(), "2")

            fourth = fixture.run(GATEWAY_PYTHON_TAG="fixture-asset-2")
            self.assertEqual(fourth.returncode, 0, fourth.stdout + fourth.stderr)
            self.assertEqual(fixture.pip_count.read_text().strip(), "3")
            stamps = [p.name for p in (fixture.root / "menubar" / "vendor" / "runtime").iterdir()]
            self.assertTrue(any("fixture-asset-2" in name and PY_ARCH in name for name in stamps))

    def test_runtime_architecture_is_verified_before_bundling(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run(TASK6_RUNTIME_MACHINE="x86_64")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("bundled runtime architecture", output)
            self.assertNotIn("Built:", output)

    def test_bundle_contains_empty_catalogue_lock_and_verified_signature(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(tuple(_authority_files()))
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            app = fixture.root / "menubar" / "build" / "gateway-kh.app"
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            self.assertEqual(info["CFBundleDisplayName"], "Gateway Studio KH")
            self.assertEqual(info["CFBundleIdentifier"], "kh.gateway.menubar")
            self.assertEqual(info["CFBundleExecutable"], "Gateway Studio KH")
            self.assertEqual(info["CFBundleName"], "Gateway Studio KH")
            version = info["CFBundleShortVersionString"]
            self.assertFalse((fixture.root / "dist" / f"Gateway Studio KH-{version}.dmg").exists())
            candidates = list(
                (fixture.root / "dist" / ".candidates").rglob(f"Gateway Studio KH-{version}-*.dmg")
            )
            self.assertEqual(len(candidates), 1, candidates)
            shipped = json.loads(
                (app / "Contents" / "Resources" / "gateway" / "data" / "voices.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(shipped.get("voices"), [])
            self.assertTrue((app / "Contents" / "Resources" / "gateway" / "requirements.lock").is_file())
            self.assertIn("Built:", result.stdout)
            self.assertRegex(result.stdout, r"archive sha256:[0-9a-f]{64}")
            self.assertIn("verified", fixture.codesign_log.read_text(encoding="utf-8"))
            # The gateway service is a proper helper bundle, so macOS names it by
            # "Gateway Studio KH Helper" instead of a bare interpreter.
            helper = app / "Contents" / "Helpers" / "Gateway Studio KH Helper.app"
            helper_info = plistlib.loads((helper / "Contents/Info.plist").read_bytes())
            self.assertEqual(helper_info["CFBundleName"], "Gateway Studio KH Helper")
            self.assertEqual(helper_info["CFBundleDisplayName"], "Gateway Studio KH Helper")
            self.assertEqual(helper_info["CFBundleIdentifier"], "kh.gateway.helper")
            self.assertEqual(helper_info["CFBundleExecutable"], "Gateway Studio KH Helper")
            self.assertTrue(helper_info["LSUIElement"])
            self.assertTrue((helper / "Contents/MacOS/Gateway Studio KH Helper").is_file())
            self.assertTrue((helper / "Contents/Resources/AppIcon.icns").is_file())

    def test_default_build_stages_descriptor_only_when_authority_source_exists(self) -> None:
        with _BuildFixture() as fixture:
            names = ("profile.json", "tools.json", "compiler.json")
            payloads = fixture.write_authority(names)
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bundled = (fixture.root / "menubar/build/gateway-kh.app/Contents/Resources"
                       / "gateway/data/final-render")
            self.assertTrue(bundled.is_dir())
            self.assertEqual(sorted(path.name for path in bundled.iterdir()), sorted(names))
            for name in names:
                self.assertEqual((bundled / name).read_bytes(), payloads[name])

    def test_tool_only_build_ignores_the_authority_source(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
            result = fixture.run("--render-authority", "tool-only")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bundled = (fixture.root / "menubar/build/gateway-kh.app/Contents/Resources"
                       / "gateway/data/final-render")
            self.assertFalse(bundled.exists())

    def test_descriptor_only_build_stages_exact_three_bytes(self) -> None:
        with _BuildFixture() as fixture:
            names = ("profile.json", "tools.json", "compiler.json")
            payloads = fixture.write_authority(names)
            result = fixture.run("--render-authority", "descriptor-only")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bundled = (fixture.root / "menubar/build/gateway-kh.app/Contents/Resources"
                       / "gateway/data/final-render")
            self.assertTrue(bundled.is_dir())
            self.assertEqual(sorted(path.name for path in bundled.iterdir()), sorted(names))
            for name in names:
                self.assertEqual((bundled / name).read_bytes(), payloads[name])

    def test_sealed_candidate_build_stages_exact_three_descriptors(self) -> None:
        # There is no qualification vector: the Gateway runtime reads only the three
        # descriptors, and its exact-set check must keep seeing exactly those three names.
        with _BuildFixture() as fixture:
            names = tuple(_authority_files())
            payloads = fixture.write_authority(names)
            result = fixture.run("--render-authority", "sealed-candidate")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            bundled = (fixture.root / "menubar/build/gateway-kh.app/Contents/Resources"
                       / "gateway/data/final-render")
            self.assertTrue(bundled.is_dir())
            staged = ("profile.json", "tools.json", "compiler.json")
            self.assertEqual(sorted(path.name for path in bundled.iterdir()), sorted(staged))
            self.assertFalse((bundled / "qualification-vector.json").exists())
            for name in staged:
                self.assertEqual((bundled / name).read_bytes(), payloads[name])

    def test_closed_authority_selector_rejects_unknown_value_without_build_claim(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run("--render-authority", "other")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Built:", result.stdout + result.stderr)

    def test_selected_authority_state_rejects_partial_extra_and_cross_digest_inputs(self) -> None:
        cases = (
            ("descriptor-only", ("profile.json",)),
            ("descriptor-only", ("profile.json", "tools.json")),
            ("sealed-candidate", ("profile.json", "tools.json")),
        )
        for mode, names in cases:
            with self.subTest(mode=mode, names=names), _BuildFixture() as fixture:
                fixture.write_authority(names)
                result = fixture.run("--render-authority", mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Built:", result.stdout + result.stderr)

        for mode in ("descriptor-only", "sealed-candidate"):
            with self.subTest(mode=mode, extra="qualification-vector.json"), _BuildFixture() as fixture:
                fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
                (fixture.root / "data/final-render/qualification-vector.json").write_bytes(b"{}")
                result = fixture.run("--render-authority", mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Built:", result.stdout + result.stderr)

        with _BuildFixture() as fixture:
            payloads = fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
            profile = json.loads(payloads["profile.json"])
            profile["tools_artifact_sha256"] = "f" * 64
            (fixture.root / "data/final-render/profile.json").write_bytes(_canonical(profile))
            result = fixture.run("--render-authority", "sealed-candidate")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Built:", result.stdout + result.stderr)

    def test_descriptor_only_build_rejects_malformed_duplicate_and_open_schema(self) -> None:
        profile_cases = {}
        payloads = _authority_files()
        profile_cases["duplicate-key"] = (
            b'{"format":"studio.render.profile.v1",' + payloads["profile.json"][1:]
        )
        profile_cases["malformed-trailing-json"] = payloads["profile.json"] + b"{"
        opened = json.loads(payloads["profile.json"])
        opened["unexpected"] = None
        profile_cases["open-schema"] = _canonical(opened)

        for label, malformed in profile_cases.items():
            with self.subTest(label=label), _BuildFixture() as fixture:
                fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
                source = fixture.root / "data/final-render/profile.json"
                source.write_bytes(malformed)
                result = fixture.run("--render-authority", "descriptor-only")
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertNotIn("Built:", output)
                self.assertEqual(source.read_bytes(), malformed)

    def test_selected_build_binds_validated_bytes_to_the_copied_originals(self) -> None:
        with _BuildFixture() as fixture:
            payloads = fixture.write_authority(("profile.json", "tools.json", "compiler.json"))
            profile = json.loads(payloads["profile.json"])
            profile["tools_artifact_sha256"] = "f" * 64
            invalid = _canonical(profile)
            source = fixture.root / "data/final-render/profile.json"
            source.write_bytes(invalid)

            valid_root = fixture.root / "validator-swap-valid"
            valid_root.mkdir()
            for name, payload in payloads.items():
                (valid_root / name).write_bytes(payload)
            evidence = fixture.root / "validator-swap-evidence"
            result = fixture.run(
                "--render-authority", "sealed-candidate",
                TASK6_SWAP_AUTHORITY_FOR_VALIDATION="1",
                TASK6_VALID_AUTHORITY_SOURCE=str(valid_root),
                TASK6_VALIDATION_SWAP_EVIDENCE=str(evidence),
            )
            output = result.stdout + result.stderr
            self.assertEqual(source.read_bytes(), invalid)
            self.assertEqual((evidence / "profile.json").read_bytes(), invalid)
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("Built:", output)

    def test_icon_failure_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            # Without the selected source the build stops earlier and this would pass
            # vacuously; the fixture must reach the icon stage the test is about.
            fixture.write_authority(tuple(_authority_files()))
            result = fixture.run(TASK6_FAIL_ICON="1")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("Built:", output)
            self.assertNotIn("Installed:", output)

    def test_signing_failure_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(tuple(_authority_files()))
            result = fixture.run(TASK6_FAIL_CODESIGN="1")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("Built:", output)
            self.assertNotIn("Installed:", output)

    def test_missing_named_signing_identity_fails_closed(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(tuple(_authority_files()))
            result = fixture.run(GATEWAY_SIGN_IDENTITY="KH Missing Fixture Identity")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn('signing identity "KH Missing Fixture Identity" is not in the Keychain', output)
            self.assertNotIn("Built:", output)
            self.assertFalse(fixture.codesign_calls.exists(), "nothing may be signed ad-hoc instead")

    def test_named_identity_signs_nested_code_then_bundle_without_deep(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(tuple(_authority_files()))
            name = "KH Fixture Signing"
            result = fixture.run(
                GATEWAY_SIGN_IDENTITY=name,
                TASK6_SECURITY_OUTPUT=f'  1) {"A" * 40} "{name}" (CSSMERR_TP_NOT_TRUSTED)',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = fixture.codesign_calls.read_text(encoding="utf-8").splitlines()
            signing = [c for c in calls if not c.startswith("--verify")]
            self.assertTrue(signing)
            for call in signing:
                self.assertNotIn("--deep", call)
                self.assertIn(f"--sign {name}", call)
            helper = [c for c in signing if "--identifier kh.gateway.python" in c]
            self.assertEqual(len(helper), 1, calls)
            self.assertTrue(helper[0].endswith("Contents/Resources/python/bin/python3"), helper)
            self.assertTrue(signing[-2].endswith("/Gateway Studio KH Helper.app"), signing[-2])
            self.assertTrue(signing[-1].endswith("/gateway-kh.app"), "the bundle is sealed last")

    def test_archive_checksum_mismatch_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run(GATEWAY_PYTHON_SHA256="0" * 64)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("downloaded Python archive checksum", output)
            self.assertNotIn("Built:", output)


class CandidateStaging(unittest.TestCase):
    def test_same_version_candidate_leaves_canonical_pair_untouched(self) -> None:
        with _BuildFixture() as fixture:
            fixture.write_authority(tuple(_authority_files()))
            (fixture.root / "VERSION").write_text("0.2.241\n", encoding="utf-8")
            dist = fixture.root / "dist"
            dist.mkdir()
            current = dist / "Gateway Studio KH-0.2.241.dmg"
            previous = dist / "Gateway Studio KH-0.2.240.dmg"
            current.write_bytes(b"accepted current")
            previous.write_bytes(b"accepted previous")
            _write_executable(fixture.bin / "date", "printf '202611101200\\n'\n")

            failed = fixture.run(TASK6_FAIL_ICON="1")
            self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
            self.assertEqual(current.read_bytes(), b"accepted current")
            self.assertEqual(previous.read_bytes(), b"accepted previous")

            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(current.read_bytes(), b"accepted current")
            self.assertEqual(previous.read_bytes(), b"accepted previous")
            candidates = list((dist / ".candidates").rglob("Gateway Studio KH-0.2.241-*.dmg"))
            self.assertEqual(len(candidates), 1, candidates)
            self.assertIn(f"Candidate DMG: {candidates[0]}", result.stdout)
            # The staged name must be exactly what acceptance reads back from the
            # installed bundle, or a verified candidate could never be promoted.
            built = fixture.root / "menubar/build/gateway-kh.app/Contents/Info.plist"
            info = plistlib.loads(built.read_bytes())
            self.assertEqual(
                candidates[0].name,
                f"Gateway Studio KH-{info['CFBundleShortVersionString']}-{info['GatewayBuild']}.dmg",
            )
            first = candidates[0]
            first.write_bytes(b"active candidate under verification")
            again = fixture.run()
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual(first.read_bytes(), b"active candidate under verification")
            self.assertEqual(len(list((dist / ".candidates").rglob(first.name))), 2)
            self.assertEqual(current.read_bytes(), b"accepted current")
            self.assertEqual(previous.read_bytes(), b"accepted previous")


class AcceptanceHelper(unittest.TestCase):
    BUILD = "abc1234-202611101200"

    def install(self, root: Path, version: str, build: str = BUILD) -> Path:
        app = root / "gateway-kh.app"
        (app / "Contents").mkdir(parents=True)
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": build,
                    "GatewayBuild": build,
                },
                handle,
            )
        return app

    def candidate(self, root: Path, version: str, build: str = BUILD,
                  data: bytes = b"new accepted bytes") -> Path:
        path = root / f"Gateway Studio KH-{version}-{build}.dmg"
        path.write_bytes(data)
        return path

    def base(self, root: Path, version: str = "0.2.242"):
        project = root / "repository"
        (project / "dist").mkdir(parents=True)
        evidence = root / "acceptance.json"
        evidence.write_text("{}", encoding="utf-8")
        return project, self.install(root, version), evidence

    def adopt(self, project: Path, root: Path, version: str, data: bytes,
              evidence: Path) -> subprocess.CompletedProcess[str]:
        """One trusted adoption through the shared helper, as the release lead does."""
        source = root / f"adopt-{version}.dmg"
        source.write_bytes(data)
        pins = root / f"pins-{version}.json"
        pins.write_text(json.dumps({"schema": 1, "apps": [{
            "app": "Gateway Studio KH",
            "project": str(project),
            "releases": [{
                "version": version,
                "source": str(source),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "evidence": str(evidence),
            }],
        }]}), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(RELEASE_DIST_HELPER), str(pins)],
            text=True, capture_output=True, check=False,
        )

    def run_accept(self, project, candidate, version, installed, evidence, helper=None, *, cwd=None):
        command = [
            sys.executable,
            str(ACCEPT_SCRIPT),
            "--candidate", str(candidate),
            "--version", version,
            "--installed-app", str(installed),
            "--evidence", str(evidence),
            "--project", str(project),
            "--helper", str(helper or RELEASE_DIST_HELPER),
        ]
        return subprocess.run(command, text=True, capture_output=True, check=False, cwd=cwd)

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_documented_relative_candidate_works_with_the_actual_helper(self) -> None:
        with tempfile.TemporaryDirectory(dir=RUN_ROOT) as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root)
            staged = project / "dist" / ".candidates" / "fixture"
            staged.mkdir(parents=True)
            candidate = self.candidate(staged, "0.2.242")
            result = self.run_accept(
                project, candidate.relative_to(project), "0.2.242",
                Path(os.path.relpath(installed, project)),
                Path(os.path.relpath(evidence, project)),
                Path(os.path.relpath(RELEASE_DIST_HELPER, project)), cwd=project,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            name = "Gateway Studio KH-0.2.242.dmg"
            self.assertEqual((project / "dist" / name).read_bytes(), b"new accepted bytes")
            ledger = json.loads((project / "dist" / ".release-dist.json").read_text())
            recorded = Path(ledger["apps"]["Gateway Studio KH"]["files"][name]["evidence"])
            self.assertTrue(recorded.is_absolute())
            self.assertTrue(recorded.samefile(evidence))

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_relative_symlinks_remain_visible_to_the_actual_helper(self) -> None:
        for linked in ("candidate", "project"):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory(dir=RUN_ROOT) as temporary:
                root = Path(temporary)
                project, installed, evidence = self.base(root)
                candidate = self.candidate(root, "0.2.242")
                project_arg = Path(".")
                if linked == "candidate":
                    alias = project / "dist" / ".candidates" / candidate.name
                    alias.parent.mkdir()
                    alias.symlink_to(candidate)
                    candidate = alias
                else:
                    alias = root / "repository-link"
                    alias.symlink_to(project, target_is_directory=True)
                    project_arg = Path(os.path.relpath(alias, project))
                result = self.run_accept(
                    project_arg, Path(os.path.relpath(candidate, project)), "0.2.242",
                    Path(os.path.relpath(installed, project)),
                    Path(os.path.relpath(evidence, project)),
                    Path(os.path.relpath(RELEASE_DIST_HELPER, project)), cwd=project,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not be a symlink", result.stderr)
                self.assertFalse((project / "dist" / "Gateway Studio KH-0.2.242.dmg").exists())
                self.assertEqual(candidate.read_bytes(), b"new accepted bytes")

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_accepted_candidate_is_promoted_and_prior_release_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.242")
            dist = project / "dist"
            adopted = self.adopt(project, root, "0.2.241", b"prior accepted bytes", evidence)
            self.assertEqual(adopted.returncode, 0, adopted.stderr)

            result = self.run_accept(
                project, self.candidate(root, "0.2.242"), "0.2.242", installed, evidence
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((dist / "Gateway Studio KH-0.2.242.dmg").read_bytes(),
                             b"new accepted bytes")
            self.assertEqual((dist / "Gateway Studio KH-0.2.241.dmg").read_bytes(),
                             b"prior accepted bytes")
            self.assertTrue((project / ".release-dist.lock").is_file())
            self.assertEqual(
                [p.name for p in project.iterdir() if p.name.startswith("accept-release-")],
                [], "scratch pins must not survive the run",
            )

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_a_symlinked_trash_retires_the_oldest_release_into_it(self) -> None:
        """The repo's `_trash` is a symlink to the shared trash, which the helper refuses
        unless the pin names the real folder; without that the third release cannot be
        retired. This is the live 0.2.240/241/242 case."""
        with tempfile.TemporaryDirectory(dir=RUN_ROOT) as temporary:
            root = Path(os.path.realpath(temporary))
            project, installed, evidence = self.base(root, "0.2.242")
            dist = project / "dist"
            trash = root / "shared-trash"
            (trash / "dist").mkdir(parents=True)
            (project / "_trash").symlink_to(trash, target_is_directory=True)
            releases = []
            for version, data in (("0.2.240", b"oldest"), ("0.2.241", b"prior")):
                source = root / f"adopt-{version}.dmg"
                source.write_bytes(data)
                releases.append({"version": version, "source": str(source),
                                 "sha256": hashlib.sha256(data).hexdigest(),
                                 "bytes": len(data), "evidence": str(evidence)})
            pins = root / "pins-pair.json"
            pins.write_text(json.dumps({"schema": 1, "apps": [{
                "app": "Gateway Studio KH", "project": str(project), "releases": releases,
            }]}), encoding="utf-8")
            adopted = subprocess.run([sys.executable, str(RELEASE_DIST_HELPER), str(pins)],
                                     text=True, capture_output=True, check=False)
            self.assertEqual(adopted.returncode, 0, adopted.stderr)

            result = self.run_accept(project, self.candidate(root, "0.2.242"), "0.2.242",
                                     installed, evidence)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((dist / "Gateway Studio KH-0.2.240.dmg").exists())
            self.assertEqual(
                (trash / "dist" / "Gateway Studio KH-0.2.240.dmg").read_bytes(), b"oldest")
            self.assertTrue((dist / "Gateway Studio KH-0.2.241.dmg").is_file())
            self.assertEqual((dist / "Gateway Studio KH-0.2.242.dmg").read_bytes(),
                             b"new accepted bytes")

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_repeating_the_same_version_is_allowed_only_when_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.242")
            dist = project / "dist"
            self.assertEqual(self.adopt(project, root, "0.2.242", b"managed bytes",
                                        evidence).returncode, 0)

            again = self.run_accept(
                project, self.candidate(root, "0.2.242", data=b"managed bytes"),
                "0.2.242", installed, evidence,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual((dist / "Gateway Studio KH-0.2.242.dmg").read_bytes(),
                             b"managed bytes")

            changed = self.run_accept(
                project, self.candidate(root, "0.2.242", data=b"changed bytes"),
                "0.2.242", installed, evidence,
            )
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn("byte-identical", changed.stderr)
            self.assertEqual((dist / "Gateway Studio KH-0.2.242.dmg").read_bytes(),
                             b"managed bytes")

    @unittest.skipUnless(RELEASE_DIST_HELPER.is_file(), "shared release-dist helper is not present")
    def test_a_version_below_the_managed_pair_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.241")
            dist = project / "dist"
            self.assertEqual(self.adopt(project, root, "0.2.242", b"current rollback",
                                        evidence).returncode, 0)

            result = self.run_accept(
                project, self.candidate(root, "0.2.241", data=b"older build"),
                "0.2.241", installed, evidence,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lower than managed", result.stderr)
            self.assertEqual((dist / "Gateway Studio KH-0.2.242.dmg").read_bytes(),
                             b"current rollback")

    def test_candidate_must_be_the_exact_staged_name_of_the_installed_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.242")
            stray = root / "Gateway Studio KH-0.2.242-otherbuild.dmg"
            stray.write_bytes(b"arbitrary dmg bytes")

            result = self.run_accept(project, stray, "0.2.242", installed, evidence)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exact candidate", result.stderr)
            self.assertFalse((project / "dist" / "Gateway Studio KH-0.2.242.dmg").exists())

    def test_installed_version_must_match_the_requested_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.241")
            result = self.run_accept(
                project, self.candidate(root, "0.2.242"), "0.2.242", installed, evidence
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installed version", result.stderr)

    def test_acceptance_evidence_must_be_a_present_non_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, _ = self.base(root, "0.2.242")
            candidate = self.candidate(root, "0.2.242")
            empty = root / "empty-receipt.json"
            empty.write_text("", encoding="utf-8")
            for bad in (empty, root / "no-such-receipt.json"):
                result = self.run_accept(
                    project, candidate, "0.2.242", installed, bad
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("acceptance evidence is missing or empty", result.stderr)

    def test_malformed_ledger_is_refused_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.242")
            (project / "dist" / ".release-dist.json").write_text(
                '{"schema": 1, "apps": []}', encoding="utf-8"
            )
            result = self.run_accept(
                project, self.candidate(root, "0.2.242"), "0.2.242", installed, evidence
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema 1 record", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_helper_failure_is_reported_and_canonical_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, installed, evidence = self.base(root, "0.2.242")
            helper = root / "failing_helper.py"
            helper.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")

            result = self.run_accept(
                project, self.candidate(root, "0.2.242"), "0.2.242", installed, evidence, helper
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertFalse((project / "dist" / "Gateway Studio KH-0.2.242.dmg").exists())

    def test_refuses_before_the_helper_when_install_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "repository"
            (project / "dist").mkdir(parents=True)
            evidence = root / "acceptance.json"
            evidence.write_text("{}", encoding="utf-8")

            result = self.run_accept(
                project, self.candidate(root, "0.2.242"), "0.2.242",
                root / "missing.app", evidence,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("acceptance must follow install", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
