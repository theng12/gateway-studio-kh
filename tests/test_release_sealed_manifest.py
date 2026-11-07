"""Actual Gateway acceptance of Hub's frozen v2 fixture, never real fleet traffic."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests import test_release_delta as delta
from gateway import fleet, release_publisher as rp

FIXTURE = Path(__file__).parent / "fixtures/sealed_manifest/sealed_manifest_fixture.json"
FIXTURE_SHA256 = "5422c8eb5a660dfd7f28ef8787a37d86cf83e5694c7de14c40e8d75f4f8e6156"


class SealedManifest(unittest.TestCase):
    def setUp(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FIXTURE_SHA256)
        self.fixture = json.loads(raw)
        self.valid = self.fixture["valid_sealed_release"]
        self.harness = delta.ReleaseDelta("test_only_the_needed_blobs_are_sent_and_the_release_is_read_back")
        self.harness.setUp()
        self.addCleanup(self.harness.tearDown)
        self.root = self.harness.root / "sealed"

        @contextmanager
        def mounted(_):
            yield self.root

        self.mount = patch.object(rp, "_mounted", mounted)
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def materialize(self, sidecar=False):
        app = self.root / "Studio Hub.app"
        for rel, row in self.valid["app_files"].items():
            data = base64.b64decode(row["content_b64"], validate=True)
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            path = app / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if row["executable"] else 0o644)
        directory = self.root / "file-manifest" if sidecar else app / "Contents/Resources/file-manifest"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.json").write_text(json.dumps(self.valid["manifest_index"]))
        for name, encoded in self.valid["manifest_tsv_gz_b64"].items():
            (directory / f"{name}.tsv.gz").write_bytes(base64.b64decode(encoded, validate=True))
        return app, directory

    def publish(self, controller, **kwargs):
        with patch.object(fleet, "_read", return_value=[{"id": "c0", "label": "Fixture", "url": controller.base}]), \
             patch.object(fleet, "_token_for", return_value="fixture-token"):
            return rp.publish(artifact_path=self.harness.dmg, version="0.6.225", build="fixture", **kwargs)

    def refused_before_push(self, controller, **kwargs):
        with self.assertRaises(rp.ReleaseError) as caught:
            self.publish(controller, **kwargs)
        self.assertEqual(caught.exception.code, "RELEASE_MANIFEST_INVALID")
        self.assertEqual((controller.posts, controller.puts), ([], []))

    def test_ac1_to_ac4_running_identity_controls_transport_not_held_future(self):
        self.materialize(sidecar=True)
        for version in ("0.6.223", "0.6.224", None, "unknown", "0.6.225.0", ["0.6.224", "0.6.225"],
                        "0.6.225-rc.1", " 0.6.225", "00.6.225", "0.6.225", "0.6.226"):
            with self.subTest(version=version):
                controller = self.harness.controller(running_version=version)
                controller.held = {"version": "0.6.225", "signature": "held-future"}
                outcome = self.publish(controller)["locations"]["c0"]
                self.assertTrue(outcome["ok"], outcome)
                eligible = version in ("0.6.225", "0.6.226")
                self.assertEqual(outcome["mode"], "delta" if eligible else "full")
                self.assertGreater(controller.version_reads, 0)
                self.assertEqual("file_manifest" in controller.posts[0], eligible)
                if not eligible:
                    self.assertEqual(controller.puts, [])
                    self.assertIn("artifact_base64", controller.posts[0])

    def test_version_endpoint_failure_preserves_held_release_but_disables_delta(self):
        import httpx
        client = httpx.Client
        held = {"version": "0.6.225"}
        for response in ({}, [], {"version": 225}, {"version": "0.6.225\n"}):
            with self.subTest(response=response):
                def handler(request):
                    body = {"release": held, "delta": self.fixture["actual_224_wire"]["delta"]} if request.url.path.endswith("/release") else response
                    return httpx.Response(200, json=body)
                transport = httpx.MockTransport(handler)
                with patch.object(rp.httpx, "Client", side_effect=lambda **kwargs: client(transport=transport, **kwargs)):
                    self.assertEqual(rp._probe("http://fixture.invalid", "fixture-token"), (held, False))
        for raw in (b'{"version":"0.6.224","version":"0.6.225"}', b'{'):
            def malformed(request):
                if request.url.path.endswith("/release"):
                    return httpx.Response(200, json={"release": held, "delta": {"schema": rp.MANIFEST_SCHEMA}})
                return httpx.Response(200, content=raw)
            with patch.object(rp.httpx, "Client", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(malformed), **kwargs)):
                self.assertEqual(rp._probe("http://fixture.invalid", "fixture-token"), (held, False))
        def unavailable(request):
            if request.url.path.endswith("/release"):
                return httpx.Response(200, json={"release": held, "delta": {"schema": rp.MANIFEST_SCHEMA}})
            raise httpx.ReadTimeout("fixture timeout")
        with patch.object(rp.httpx, "Client", side_effect=lambda **kwargs: client(transport=httpx.MockTransport(unavailable), **kwargs)):
            self.assertEqual(rp._probe("http://fixture.invalid", "fixture-token"), (held, False))

    def test_ac5_valid_sidecar_precedes_valid_legacy(self):
        self.materialize()
        _, directory = self.materialize(sidecar=True)
        raw = gzip.compress(gzip.decompress((directory / "core.tsv.gz").read_bytes()), mtime=1)
        (directory / "core.tsv.gz").write_bytes(raw)
        manifest = rp.load_manifest(self.root)
        self.assertEqual(manifest.groups["core"], base64.b64encode(raw).decode())
        self.assertEqual(manifest.files["files_sha256"], self.valid["manifest_index"]["files_sha256"])

    def test_ac6_invalid_preferred_never_falls_back_even_for_forced_full(self):
        self.materialize()
        _, preferred = self.materialize(sidecar=True)
        for data in ("{", "{}", json.dumps(self.valid["manifest_index"] | {"groups": [1]})):
            (preferred / "index.json").write_text(data)
            for version, forced in (("0.6.224", False), ("0.6.225", False), ("0.6.225", True)):
                with self.subTest(data=data, version=version, forced=forced):
                    self.refused_before_push(self.harness.controller(running_version=version), force_full=forced)
        (preferred / "index.json").rename(self.harness.root / "withdrawn-index.json")
        self.refused_before_push(self.harness.controller())

    def test_ac7_native_and_signature_tamper_refused_before_any_transfer(self):
        for sidecar in (False, True):
            app, _ = self.materialize(sidecar=sidecar)
            for key in ("corrupted_bytes_case", "corrupted_signature_case"):
                with self.subTest(sidecar=sidecar, key=key):
                    row = self.fixture[key]
                    path = app / row["target_file"]
                    original = path.read_bytes()
                    path.write_bytes(base64.b64decode(row["corrupted_b64"], validate=True))
                    self.refused_before_push(self.harness.controller())
                    path.write_bytes(original)

    def test_ac8_corrupt_group_never_falls_back_to_whole_image(self):
        _, directory = self.materialize(sidecar=True)
        (directory / "core.tsv.gz").write_bytes(gzip.compress(b"corrupt group\n", mtime=0))
        self.refused_before_push(self.harness.controller())

    def test_ac9_legacy_requires_complete_byte_closure(self):
        app, _ = self.materialize()
        self.assertEqual(rp.load_manifest(self.root).files["files_count"], 4)
        for rel in self.valid["app_files"]:
            with self.subTest(rel=rel):
                path = app / rel
                original = path.read_bytes()
                path.write_bytes(original + b"tamper")
                self.refused_before_push(self.harness.controller())
                path.write_bytes(original)
        (app / "Contents/Resources/unlisted.txt").write_bytes(b"not in the manifest")
        self.refused_before_push(self.harness.controller())

    def test_optimized_python_refuses_instead_of_skipping_validation(self):
        app, _ = self.materialize(sidecar=True)
        (app / "Contents/Resources/version.txt").write_bytes(b"tampered")
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            "from tests._isolation import sandbox_env\n"
            "_sandbox = sandbox_env()\n"
            "from pathlib import Path\n"
            "from gateway import release_publisher as rp\n"
            "try:\n"
            f" rp.load_manifest(Path({str(self.root)!r}))\n"
            "except rp.ReleaseError as exc:\n"
            " print(exc.code)\n"
            " sys.exit(0 if exc.code == 'RELEASE_MANIFEST_INVALID' else 2)\n"
            "sys.exit(1)\n"
        )
        result = subprocess.run([sys.executable, "-I", "-B", "-O", "-c", source],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "RELEASE_MANIFEST_INVALID")

    def test_expanded_group_limit_refuses_before_push_and_states_ceiling(self):
        _, directory = self.materialize(sidecar=True)
        (directory / "core.tsv.gz").write_bytes(gzip.compress(b"x" * 4096, mtime=0))
        controller = self.harness.controller()
        with patch.object(rp, "_MANIFEST_GROUP_MAX_BYTES", 32):
            with self.assertRaises(rp.ReleaseError) as caught:
                self.publish(controller)
        self.assertEqual(caught.exception.code, "RELEASE_MANIFEST_INVALID")
        self.assertIn("32-byte limit", caught.exception.message)
        self.assertEqual((controller.posts, controller.puts), ([], []))

    def test_manifest_resource_errors_are_named_refusals(self):
        self.materialize(sidecar=True)
        for error in (MemoryError(), RecursionError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(rp.json, "loads", side_effect=error):
                    with self.assertRaises(rp.ReleaseError) as caught:
                        rp.load_manifest(self.root)
                self.assertEqual(caught.exception.code, "RELEASE_MANIFEST_INVALID")

    def test_executable_and_symlink_substitution_are_refused(self):
        app, _ = self.materialize(sidecar=True)
        native = app / self.fixture["corrupted_bytes_case"]["target_file"]
        native.chmod(0o644)
        self.refused_before_push(self.harness.controller())
        native.rename(self.harness.root / "saved-native")
        native.symlink_to(self.harness.root / "saved-native")
        self.refused_before_push(self.harness.controller())


if __name__ == "__main__":
    unittest.main()
