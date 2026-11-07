"""Push a signed Studio Hub app release to a contract-enforcing mock controller.

The mock (`tests/mock_controller_release.mjs`) verifies the gateway's Ed25519
signature, the artifact length and hash, and the trusted fingerprint exactly as a
real controller does. So a pass here is "the wire contract is matched byte for
byte" — not "the request was sent". The gateway signs in Node and the mock
verifies in Node, while the descriptor is built in Python, so the encodings the
addendum pins (sorted keys, no whitespace, UTF-8, base64 with padding, hex
fingerprint) are exercised across two languages.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import fleet, release_publisher as rp  # noqa: E402
from tests.test_release_delta import build_app  # noqa: E402


class MockController:
    """A controller stand-in, started as its own process."""

    def __init__(self, state: Path, fleet_token: str = "fleet-tok",
                 hub_token: str = "hub-token", delta: bool = False) -> None:
        self.process = subprocess.Popen(
            [
                "node", str(ROOT / "tests/mock_controller_release.mjs"),
                "--state", str(state), "--port", "0",
                "--fleet-token", fleet_token, "--hub-token", hub_token,
            ] + (["--delta"] if delta else []),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"mock controller did not start: {self.process.stderr.read()[:400]}")
        info = json.loads(line)
        self.port = int(info["port"])
        self.fingerprint = info["fingerprint"]
        self.base = f"http://127.0.0.1:{self.port}"
        self.fleet_token = fleet_token
        self.hub_token = hub_token

    def trust(self, public_key_hex: str) -> None:
        response = httpx.post(
            f"{self.base}/api/hub/release/trust",
            headers={"X-Hub-Token": self.hub_token},
            json={"public_key": public_key_hex}, timeout=10,
        )
        response.raise_for_status()

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


class ReleasePublish(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-release-")
        self.root = Path(self.tmp.name)
        self.controller = MockController(self.root / "controller")
        # The publisher's key and push state belong to a temp dir for the test, so
        # nothing touches the owner's real data directory.
        self.patches = [
            patch.object(rp, "key_path", lambda: self.root / "release-publisher.pem"),
            patch.object(rp, "_state_path", lambda: self.root / "release-state.json"),
            patch.object(rp, "_mounted", lambda _dmg: contextlib.nullcontext(self.root / "legacy-image")),
        ]
        for started in self.patches:
            started.start()
        rp.forget_identity()
        self.artifact = b"StudioHub app release bytes\n" * 64
        self.dmg = self.root / "StudioHub-0.6.50-abc1234.dmg"
        self.dmg.write_bytes(self.artifact)

    def tearDown(self) -> None:
        rp.forget_identity()
        for started in self.patches:
            started.stop()
        self.controller.close()
        self.tmp.cleanup()

    def _publish(self, **kwargs) -> dict:
        location = [{"id": "ctrl-1", "label": "Blue Room", "url": self.controller.base}]
        with patch.object(fleet, "_read", return_value=location), \
             patch.object(fleet, "_token_for", return_value=self.controller.fleet_token):
            return rp.publish(
                artifact_path=self.dmg, version="0.6.50", build="abc1234", **kwargs
            )

    def _delta_controller(self) -> "MockController":
        """A controller that advertises the delta capability (Hub 0.6.115+)."""
        self.controller.close()
        self.controller = MockController(self.root / "controller-delta", delta=True)
        self.addCleanup(self.controller.close)
        return self.controller

    def _publish_against_mount(self, mount: Path, **kwargs) -> dict:
        # The DMG is a fake file here, so the mounted app tree is injected; the mock controller
        # still verifies the signature, the artifact length and the artifact hash for real.
        with patch.object(rp, "_mounted", lambda _dmg: contextlib.nullcontext(mount)):
            return self._publish(**kwargs)

    def test_force_full_sends_the_whole_image_to_a_delta_capable_controller(self) -> None:
        """The 0.6.201 refusal: it advertises delta, but a delta is refused, so the owner forces full.

        Measured 2026-10-27: Hub 0.6.201 refuses the 0.6.201→0.6.203 stage (713,165,943 B is
        50.44% of the 1,413,754,368 B image, over `MAX_DELTA_FRACTION` 0.5) and the old app then
        asks for a disk image a files-only release never had. With `force_full`, the controller
        must receive `artifact_base64`, store the plain descriptor and serve the image.
        """
        self._delta_controller()
        identity = rp.identity()
        self.controller.trust(identity["public_key_hex"])
        self.app_root = build_app(self.root / "mounted", {"Contents/Info.plist": b"<plist/>"})
        state = self._publish_against_mount(self.app_root, force_full=True)

        outcome = state["locations"]["ctrl-1"]
        self.assertTrue(outcome["ok"], outcome["detail"])
        self.assertEqual(outcome["mode"], "full", outcome)
        self.assertEqual(outcome["blobs_sent"], 0)

        held = rp.read_release(self.controller.base, self.controller.fleet_token)
        self.assertIsNotNone(held)
        # The plain descriptor: a delta descriptor would carry `files_sha256`.
        self.assertNotIn("files_sha256", held)
        self.assertEqual(held["signature"], outcome["release"]["signature"])
        # A full push stored the bytes, so the controller serves them.
        served = httpx.get(f"{self.controller.base}/api/hub/release/artifact",
                           headers={"X-Hub-Token": self.controller.fleet_token}, timeout=20)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, self.artifact)

    def test_a_normal_publish_to_the_same_controller_sends_only_blobs(self) -> None:
        self._delta_controller()
        identity = rp.identity()
        self.controller.trust(identity["public_key_hex"])
        self.app_root = build_app(self.root / "mounted", {"Contents/Info.plist": b"<plist/>"})
        state = self._publish_against_mount(self.app_root)

        outcome = state["locations"]["ctrl-1"]
        self.assertTrue(outcome["ok"], outcome["detail"])
        self.assertEqual(outcome["mode"], "delta", outcome)
        self.assertGreater(outcome["blobs_sent"], 0)
        held = rp.read_release(self.controller.base, self.controller.fleet_token)
        self.assertIsNotNone(held)
        self.assertIn("files_sha256", held)

    def test_a_release_is_pushed_and_read_back(self) -> None:
        identity = rp.identity()
        self.controller.trust(identity["public_key_hex"])
        state = self._publish()

        outcome = state["locations"]["ctrl-1"]
        self.assertTrue(outcome["ok"], outcome["detail"])
        # **The report is the controller's own record**, read back after storing —
        # not the push's status code.
        held = rp.read_release(self.controller.base, self.controller.fleet_token)
        self.assertIsNotNone(held)
        self.assertEqual(held["version"], "0.6.50")
        self.assertEqual(held["build"], "abc1234")
        self.assertEqual(held["sha256"], hashlib.sha256(self.artifact).hexdigest())
        self.assertEqual(held["publisher_key_id"], identity["fingerprint"])
        self.assertEqual(held["filename"], self.dmg.name)
        # The controller countersigned, binding this gateway's signature.
        self.assertEqual(held["controller_key_id"], self.controller.fingerprint)
        self.assertTrue(held["controller_signature"])
        self.assertEqual(held["signature"], outcome["release"]["signature"])

    def test_the_signed_fields_are_never_rewritten(self) -> None:
        self.controller.trust(rp.identity()["public_key_hex"])
        descriptor = rp.descriptor_for_bytes(
            self.artifact, version="0.6.50", build="abc1234", filename=self.dmg.name
        )
        held = rp.read_release(
            *self._after_push(descriptor)
        )
        for field in ("schema", "schema_version", "version", "build", "filename",
                      "bytes", "sha256", "published_at", "publisher_id",
                      "publisher_key_id", "signature"):
            self.assertEqual(held[field], descriptor[field], field)

    def _after_push(self, descriptor: dict):
        rp.push(self.controller.base, self.controller.fleet_token, descriptor, self.artifact)
        return self.controller.base, self.controller.fleet_token

    def test_a_corrupted_artifact_is_refused(self) -> None:
        self.controller.trust(rp.identity()["public_key_hex"])
        descriptor = rp.descriptor_for_bytes(
            self.artifact, version="0.6.50", build="abc1234", filename=self.dmg.name
        )
        corrupted = bytearray(self.artifact)
        corrupted[0] ^= 0x01  # one byte, with the descriptor still describing the original
        outcome = rp.push(
            self.controller.base, self.controller.fleet_token, descriptor, bytes(corrupted)
        )
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["status"], 400)
        self.assertIn("sha256", outcome["detail"].lower())

    def test_a_length_mismatch_is_refused(self) -> None:
        self.controller.trust(rp.identity()["public_key_hex"])
        descriptor = rp.descriptor_for_bytes(
            self.artifact, version="0.6.50", build="abc1234", filename=self.dmg.name
        )
        outcome = rp.push(
            self.controller.base, self.controller.fleet_token, descriptor, self.artifact + b"x"
        )
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["status"], 400)
        self.assertIn("length", outcome["detail"].lower())

    def test_an_untrusted_controller_names_the_app_update_key_public_key(self) -> None:
        # The 409 is the one action the owner has to take on that Mac, so the
        # gateway names the raw public-key path and comparison fingerprint.
        identity = rp.identity()
        outcome = self._publish()["locations"]["ctrl-1"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["status"], 409)
        self.assertIn("App Update Key is installed", outcome["detail"])
        self.assertIn("App Update Key public key", outcome["detail"])
        self.assertIn("App Update Key into Hub → Connections", outcome["detail"])
        self.assertIn("then compare fingerprints", outcome["detail"])
        self.assertIn(identity["fingerprint"], outcome["detail"])
        self.assertNotIn("install this gateway's publisher fingerprint", outcome["detail"].lower())

    def test_a_connect_timeout_is_an_unreachable_controller(self) -> None:
        # A connection that was never established delivered nothing, so it is a
        # site that is down — not a generic push failure. (A *read* timeout after
        # the request left keeps the generic path, because it may have arrived.)
        class _Client:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def post(self, *args, **kwargs): raise httpx.ConnectTimeout("connect timed out")

        descriptor = rp.descriptor_for_bytes(
            b"x", version="0.6.50", build="abc1234", filename="f.dmg"
        )
        with patch.object(rp.httpx, "Client", lambda **kwargs: _Client()):
            outcome = rp.push("http://10.255.255.1:47873", "fleet-tok", descriptor, b"x")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "LOCATION_UNREACHABLE")
        self.assertIsNone(outcome["status"])
        self.assertIn("could not reach the controller", outcome["detail"])

    def _trust_and_hold(self, version: str, build: str) -> None:
        """Put a release on the mock controller, so it holds something to compare."""
        self.controller.trust(rp.identity()["public_key_hex"])
        descriptor = rp.descriptor_for_bytes(
            self.artifact, version=version, build=build, filename=self.dmg.name
        )
        outcome = rp.push(
            self.controller.base, self.controller.fleet_token, descriptor, self.artifact
        )
        self.assertTrue(outcome["ok"], outcome["detail"])

    def test_publishing_an_older_version_is_refused(self) -> None:
        # The Hub's own updater has no ordering — it takes whatever build differs —
        # so a gateway pushing 0.6.50 over a controller holding 0.6.51 would walk
        # that Mac backwards. Nothing on the controller side can stop it; this is
        # the only guard.
        self._trust_and_hold("0.6.51", "newer")
        outcome = self._publish()["locations"]["ctrl-1"]
        self.assertFalse(outcome["ok"], outcome)
        self.assertEqual(outcome["code"], "RELEASE_DOWNGRADE")
        self.assertIs(outcome["downgrade"], True, "the menu-bar app colours a refusal by this flag")
        self.assertIn("0.6.51", outcome["detail"])
        self.assertIn("0.6.50", outcome["detail"])
        # Refused *before* the push: the controller still holds the newer release.
        held = rp.read_release(self.controller.base, self.controller.fleet_token)
        self.assertEqual(held["version"], "0.6.51")

    def test_allow_downgrade_publishes_anyway(self) -> None:
        self._trust_and_hold("0.6.51", "newer")
        location = [{"id": "ctrl-1", "label": "Blue Room", "url": self.controller.base}]
        with patch.object(fleet, "_read", return_value=location), \
             patch.object(fleet, "_token_for", return_value=self.controller.fleet_token):
            state = rp.publish(
                artifact_path=self.dmg, version="0.6.50", build="abc1234",
                allow_downgrade=True,
            )
        outcome = state["locations"]["ctrl-1"]
        self.assertTrue(outcome["ok"], outcome["detail"])
        held = rp.read_release(self.controller.base, self.controller.fleet_token)
        self.assertEqual(held["version"], "0.6.50")

    def test_a_newer_version_is_not_blocked(self) -> None:
        self._trust_and_hold("0.6.49", "older")
        outcome = self._publish()["locations"]["ctrl-1"]
        self.assertTrue(outcome["ok"], outcome["detail"])

    def test_version_ordering_is_not_guessed(self) -> None:
        self.assertTrue(rp.is_downgrade("0.6.50", "0.6.51"))
        self.assertFalse(rp.is_downgrade("0.6.51", "0.6.50"))
        self.assertFalse(rp.is_downgrade("0.6.50", "0.6.50"))
        self.assertTrue(rp.is_downgrade("0.6.9", "0.6.10"))  # 10 > 9, not string order
        # A version with no dotted-number order is never blocked on a guess.
        for odd in ("", "abc1234", "2026-09-23", "0.6.50-beta"):
            self.assertFalse(rp.is_downgrade("0.6.50", odd), odd)
        self.assertEqual(rp.version_key("1.2.3"), (1, 2, 3))
        self.assertIsNone(rp.version_key("1.2.3-rc1"))

    def test_fleet_reports_the_trusted_publisher(self) -> None:
        fingerprint = "ab" * 32
        self.assertEqual(
            fleet._trusted_publisher(
                {"trusted_publisher": {"publisher_id": "gatewaykh", "fingerprint": fingerprint}}
            ),
            {"reported": True, "publisher_id": "gatewaykh", "fingerprint": fingerprint},
        )
        # Explicit null is "trusts nobody"; a missing key is "did not say". The
        # page must not draw the same sentence for both.
        self.assertEqual(
            fleet._trusted_publisher({"trusted_publisher": None}),
            {"reported": True, "publisher_id": None, "fingerprint": None},
        )
        self.assertEqual(
            fleet._trusted_publisher({}),
            {"reported": False, "publisher_id": None, "fingerprint": None},
        )

    def test_absence_is_null_never_a_stub(self) -> None:
        self.assertIsNone(
            rp.read_release(self.controller.base, self.controller.fleet_token)
        )

    def test_canonical_bytes_are_sorted_and_whitespace_free(self) -> None:
        descriptor = {"b": 2, "a": "x", "nested": {"z": 1, "a": 2}}
        self.assertEqual(
            rp.canonical_bytes(descriptor),
            b'{"a":"x","b":2,"nested":{"a":2,"z":1}}',
        )

    def test_the_private_key_never_leaves_an_api_answer(self) -> None:
        identity = rp.identity()
        self.assertEqual(sorted(set(identity) - {"publisher_id"}), sorted({
            "algorithm", "public_key_base64", "public_key_hex", "fingerprint",
        }))
        encoded = json.dumps(identity)
        self.assertNotIn("PRIVATE KEY", encoded)
        # The key file exists, and the helper wrote it 0600.
        mode = (self.root / "release-publisher.pem").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # The public key really is the raw 32 bytes the fingerprint names.
        raw = bytes.fromhex(identity["public_key_hex"])
        self.assertEqual(len(raw), 32)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), identity["fingerprint"])
        self.assertEqual(base64.b64decode(identity["public_key_base64"]), raw)

    def test_full_push_timeout_and_recovery_bounds(self) -> None:
        # A full push of ~1.9 GB base64 over WAN/Tailscale takes 30+ minutes; timeout must allow it.
        self.assertGreaterEqual(rp._PUSH_TIMEOUT.read, 3600.0)
        self.assertGreaterEqual(rp._RECOVER_SECONDS, 300.0)


if __name__ == "__main__":
    unittest.main()
