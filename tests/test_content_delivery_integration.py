"""Real Story -> isolated Gateway private-shelf delivery integration.

This intentionally starts a disposable Uvicorn process and invokes Story's
actual tsx-loaded consumer.  It never addresses a live Gateway or Story profile.
"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx


ROOT = Path(__file__).resolve().parents[1]
STORY = Path(os.environ.get("GATEWAY_STORY_SOURCE", "/build/Developer/story-studio-kh"))
NODE = os.environ.get("GATEWAY_AUTHORING_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
API_KEY = "content-delivery-integration-fixture"


def unused_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    try:
        return listener.getsockname()[1]
    finally:
        listener.close()


def safe_diagnostic(output: str) -> str:
    lines = output.splitlines()[-16:]
    safe = []
    for line in lines:
        line = re.sub(r"https?://[^\s]+", "[URL withheld]", line)
        line = re.sub(r"\b[A-Za-z0-9_-]{96,}\b", "[opaque value withheld]", line)
        line = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[redacted]", line)
        line = re.sub(r"(?i)([?&](?:grant|token|signature)=)[^&\s]+", r"\1[redacted]", line)
        line = re.sub(r"(?i)(public[_ -]?key(?:_base64)?[=:]\s*)[^\s,]+", r"\1[redacted]", line)
        safe.append(line[:1024])
    return "\n".join(safe)[-2400:]


class ContentDeliveryIntegration(unittest.TestCase):
    def test_real_story_private_shelf_round_trip(self):
        self.assertTrue(STORY.is_dir(), f"Story source is unavailable: {STORY}")
        with tempfile.TemporaryDirectory(prefix="gateway-content-delivery-") as temporary:
            root = Path(temporary)
            fixture = root / "story-authoring"
            env = dict(os.environ)
            env.update({
                "GATEWAY_APP_SUPPORT": str(root / "support"),
                "GATEWAY_DATA": str(root / "data"),
                "GATEWAY_ENV_FILE": str(root / ".env"),
                "GATEWAY_ARTIFACTS": str(root / "artifacts"),
                "GATEWAY_DB": str(root / "gateway.sqlite3"),
                "GATEWAY_CREDENTIALS": str(root / "credentials"),
                "GATEWAY_MODEL_CACHE": str(root / "model-cache"),
                "GATEWAY_FLEET": str(root / "fleet"),
                "GATEWAY_VOICES": str(root / "voices"),
                "GATEWAY_VOICES_OUT": str(root / "voices-out"),
                "STUDIOHUB_READY_FILE": str(root / "no-hub.json"),
                "GATEWAY_NO_MIGRATE": "1",
                "GATEWAY_API_KEY": API_KEY,
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_STORY_SOURCE": str(fixture),
                "GATEWAY_STORY_REGISTRY": str(fixture / "content-packs" / "authoring-registry.json"),
                "GATEWAY_AUTHORING_NODE": NODE,
                "STORY_STUDIO_DATA_DIR": str(root / "story-data"),
            })
            prepared = subprocess.run(
                [NODE, "tests/prepare_authoring_fixture.mjs", str(fixture), str(STORY), "--empty-registry"],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=45, check=True,
            )
            self.assertIsNone(json.loads(prepared.stdout)["key"])
            port = unused_port()
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1", "--port", str(port),
                 "--no-proxy-headers", "--no-access-log", "--log-level", "warning"],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                with httpx.Client(base_url=base_url, trust_env=False, timeout=2) as client:
                    for _ in range(100):
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            time.sleep(0.05)
                    else:
                        process.terminate()
                        _, stderr = process.communicate(timeout=5)
                        self.fail(f"isolated Gateway did not start: {safe_diagnostic(stderr)}")
                story = subprocess.run(
                    [NODE, "--import", "tsx", str(ROOT / "tests/verify_story_content_delivery.mjs"),
                     "round-trip", base_url, API_KEY, str(fixture), str(STORY), str(root / "data")],
                    cwd=STORY, env=env, text=True, capture_output=True, timeout=180,
                )
                self.assertEqual(story.returncode, 0, f"Story integration process failed:\n{safe_diagnostic(story.stderr)}")
                evidence = json.loads(story.stdout)
                self.assertEqual(evidence["installed_version"], "1.0.1")
                self.assertEqual(evidence["retained_versions"], ["1.0.0", "1.0.1"])
                self.assertEqual(evidence["published_kinds"], [
                    "series-formula", "thumbnail-recipe", "topic-pack", "visual-style",
                ])
                self.assertTrue(evidence["tampered_archive_refused"])
                self.assertTrue(evidence["old_account_anchor_retained"])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
                if process.stderr is not None:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
