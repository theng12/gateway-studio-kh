#!/usr/bin/env python3
"""Focused Task 6 checks for portable defaults and packaging identity."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "menubar" / "build.sh"
PY_VERSION = "3.11.16"
PY_TAG = "20260901"
PY_ARCH = "aarch64"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


class _BuildFixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="gateway-task6-build-")
        self.root = Path(self._tmp.name)
        self.menubar = self.root / "menubar"
        self.menubar.mkdir()
        (self.root / "data").mkdir()
        (self.root / "gateway").mkdir()
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.asset = self.root / "python-asset.tar.gz"
        self.pip_count = self.root / "pip-count"
        self.codesign_log = self.root / "codesign.log"

        shutil.copy2(BUILD_SCRIPT, self.menubar / "build.sh")
        shutil.copy2(ROOT / "menubar" / "make_icon.py", self.menubar / "make_icon.py")
        (self.menubar / "main.swift").write_text("import Cocoa\n", encoding="utf-8")
        (self.menubar / "whats-new.md").write_text("fixture\n", encoding="utf-8")
        (self.root / "gateway" / "__init__.py").write_text("", encoding="utf-8")
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
if [ "${1:-}" = "--verify" ]; then
  printf 'verified\\n' >> "$TASK6_CODESIGN_LOG"
fi
""",
        )

    def __enter__(self) -> "_BuildFixture":
        return self

    def __exit__(self, *_: object) -> None:
        self._tmp.cleanup()

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "TASK6_ASSET_SOURCE": str(self.asset),
            "TASK6_PIP_COUNT": str(self.pip_count),
            "TASK6_CODESIGN_LOG": str(self.codesign_log),
            "GATEWAY_PYTHON_URL": "https://fixture.invalid/python.tar.gz",
        }

    def run(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.environment()
        env.update(overrides)
        return subprocess.run(
            ["sh", str(self.menubar / "build.sh"), "--no-install"],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


class Task6Packaging(unittest.TestCase):
    def test_shipped_catalogue_is_an_empty_default(self) -> None:
        payload = json.loads((ROOT / "data" / "voices.json").read_text(encoding="utf-8"))
        self.assertEqual(payload.get("voices"), [])

    def test_fresh_profile_has_no_ghost_voice_and_import_resolves_its_clip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gateway-task6-profile-") as name:
            base = Path(name)
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
            result = fixture.run()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            app = fixture.root / "menubar" / "build" / "gateway-kh.app"
            shipped = json.loads(
                (app / "Contents" / "Resources" / "gateway" / "data" / "voices.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(shipped.get("voices"), [])
            self.assertTrue((app / "Contents" / "Resources" / "gateway" / "requirements.lock").is_file())
            self.assertIn("Built:", result.stdout)
            self.assertRegex(result.stdout, r"archive sha256:[0-9a-f]{64}")
            self.assertIn("verified", fixture.codesign_log.read_text(encoding="utf-8"))

    def test_icon_failure_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run(TASK6_FAIL_ICON="1")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("Built:", output)
            self.assertNotIn("Installed:", output)

    def test_signing_failure_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run(TASK6_FAIL_CODESIGN="1")
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertNotIn("Built:", output)
            self.assertNotIn("Installed:", output)

    def test_archive_checksum_mismatch_cannot_claim_a_built_bundle(self) -> None:
        with _BuildFixture() as fixture:
            result = fixture.run(GATEWAY_PYTHON_SHA256="0" * 64)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("downloaded Python archive checksum", output)
            self.assertNotIn("Built:", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
