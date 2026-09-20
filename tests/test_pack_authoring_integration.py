"""Real HTTP -> private persistence -> Story registry/schema/builder, no paid calls.

Requires the optional local Story checkout and its locked Node dependencies.
Uses an isolated registry and profile; never writes a live product reservation.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-author-integration-")
SUPPORT = Path(TEMP.name)
STORY = Path(os.environ.get("GATEWAY_STORY_SOURCE", Path.home() / "Developer/story-studio-kh"))
NODE = os.environ.get("GATEWAY_AUTHORING_NODE") or shutil.which("node") or "/opt/homebrew/bin/node"
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(SUPPORT), "GATEWAY_DATA": str(SUPPORT / "data"),
    "GATEWAY_ENV_FILE": str(SUPPORT / ".env"), "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(SUPPORT / "no-hub.json"),
    "GATEWAY_API_KEY": "author-integration-fixture", "GATEWAY_HOST": "127.0.0.1",
    "GATEWAY_AUTHORING_NODE": NODE,
})
from fastapi.testclient import TestClient
from gateway.api import app


class AuthoringIntegration(unittest.TestCase):
    def test_real_candidate_roundtrip_and_sealed_retry(self):
        setup = subprocess.run([NODE, "tests/prepare_authoring_fixture.mjs", str(SUPPORT / "story"), str(STORY)],
                               cwd=ROOT, text=True, capture_output=True, timeout=30, check=True)
        fixture = json.loads(setup.stdout)
        os.environ["GATEWAY_STORY_SOURCE"] = fixture["source"]
        root = "/admin/api/pack-authoring"
        registry_path = SUPPORT / "story/content-packs/authoring-registry.json"
        registry_before = registry_path.read_bytes()
        client = TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)
        self.addCleanup(client.close)
        catalogue = client.get(root)
        self.assertEqual(catalogue.status_code, 200, catalogue.text)
        self.assertTrue(catalogue.json()["available"], catalogue.text)
        self.assertEqual(catalogue.json()["reservations"][0]["key"], fixture["key"])
        raw = json.dumps(fixture["document"], ensure_ascii=False, indent=2)
        created = client.post(root + "/drafts", json={"key": fixture["key"], "document_text": raw})
        self.assertEqual(created.status_code, 200, created.text)
        draft = created.json()
        item = root + "/drafts/" + draft["id"]
        self.assertEqual(client.post(root + "/drafts", json={"key": fixture["key"], "document_text": "{}"}).json(), draft)
        saved = client.put(item, json={"document_text": "{unfinished", "revision": draft["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        revision = saved.json()["revision"]
        self.assertEqual(client.get(item).json()["document_text"], "{unfinished")
        self.assertEqual(client.post(item + "/preview", json={"revision": revision}).status_code, 422)
        self.assertEqual(client.put(item, json={"document_text": raw, "revision": draft["revision"]}).status_code, 409)
        saved = client.put(item, json={"document_text": raw, "revision": revision})
        self.assertEqual(saved.status_code, 200, saved.text)
        revision = saved.json()["revision"]
        preview = client.post(item + "/preview", json={"revision": revision})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["document"], fixture["document"])
        exported = client.post(item + "/export", json={"revision": revision})
        self.assertEqual(exported.status_code, 200, exported.text if exported.status_code != 200 else "")
        self.assertIn("attachment", exported.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["pack_id"], draft["pack_id"])
            self.assertEqual(manifest["delivery_profile"], "genstudio-store-receipt-v1")
            self.assertNotIn("signature.ed25519", archive.namelist())
            self.assertEqual(json.loads(archive.read(manifest["items"][0]["path"])), fixture["document"])
        sealed = client.get(item).json()
        self.assertTrue(sealed["sealed"])
        self.assertEqual(client.put(item, json={"document_text": "{}", "revision": sealed["revision"]}).status_code, 409)
        retried = client.post(item + "/export", json={"revision": sealed["revision"]})
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.content, exported.content)
        candidate = SUPPORT / "candidate.sspack"
        candidate.write_bytes(exported.content)
        verified = subprocess.run([NODE, "--input-type=module", "-e",
            "const {validateContentPack}=await import(process.argv[1]); console.log(JSON.stringify(await validateContentPack({archivePath:process.argv[2],deliveryProfile:'genstudio-store-receipt-v1'})));",
            (STORY / "scripts/content-packs/lib.mjs").as_uri(), str(candidate)],
            text=True, capture_output=True, timeout=30, check=True)
        self.assertEqual(json.loads(verified.stdout)["sha256"], hashlib.sha256(exported.content).hexdigest())
        self.assertEqual(registry_path.read_bytes(), registry_before)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEMP.cleanup()
