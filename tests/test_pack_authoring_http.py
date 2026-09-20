"""HTTP boundary checks; service doubles do not exercise the Story builder."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-author-http-")
support = Path(TEMP.name)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(support), "GATEWAY_DATA": str(support / "data"),
    "GATEWAY_ENV_FILE": str(support / ".env"), "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(support / "no-hub.json"),
    "GATEWAY_API_KEY": "author-fixture", "GATEWAY_HOST": "127.0.0.1",
})
from fastapi.testclient import TestClient
import gateway
from gateway import api


class AuthoringError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class AuthoringHTTP(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.draft = {"id": "a" * 64, "revision": 1, "sealed": False,
                      "key": "com.fixture.pack@1.0.0", "document_text": "{}"}
        def record(name, result):
            def invoke(*args):
                self.calls.append((name, args))
                return result
            return invoke
        self.service = types.SimpleNamespace(
            AuthoringError=AuthoringError,
            catalogue=record("catalogue", {"available": True, "reservations": [], "templates": []}),
            list_drafts=record("list", [self.draft]),
            template=record("template", {"document": {"kind": "topic-pack"}}),
            create_draft=record("create", self.draft),
            read_draft=record("read", self.draft),
            save_draft=record("save", self.draft),
            preview_draft=record("preview", {"document": {"kind": "topic-pack"}}),
            export_draft=record("export", (b"fixture archive", {"pack_id": "com.fixture.pack", "version": "1.0.0"})),
        )
        self.patcher = patch.dict(sys.modules, {"gateway.pack_authoring": self.service})
        self.patcher.start()
        self.package_patch = patch.object(gateway, "pack_authoring", self.service, create=True)
        self.package_patch.start()
        self.client = TestClient(api.app, client=("127.0.0.1", 12345))

    def tearDown(self):
        self.client.close()
        self.package_patch.stop()
        self.patcher.stop()

    def test_crud_preview_export_preserve_exact_inputs_and_attachment(self):
        root = "/admin/api/pack-authoring"
        response = self.client.get(root)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["available"])
        self.assertEqual(self.client.get(root + "/drafts").json()["drafts"], [self.draft])
        self.assertEqual(self.client.post(root + "/template", json={"key": "template"}).status_code, 200)
        response = self.client.post(root + "/drafts", json={"key": "reserved", "document_text": "{unfinished"})
        self.assertEqual(response.status_code, 200)
        item = root + "/drafts/" + self.draft["id"]
        self.assertEqual(self.client.get(item).status_code, 200)
        self.assertEqual(self.client.put(item, json={"document_text": "{still editing", "revision": 1}).status_code, 200)
        self.assertEqual(self.client.post(item + "/preview", json={"revision": 1}).status_code, 200)
        response = self.client.post(item + "/export", json={"revision": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"fixture archive")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn(".sspack", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn(("create", ("reserved", "{unfinished")), self.calls)
        self.assertIn(("save", (self.draft["id"], "{still editing", 1)), self.calls)

    def test_invalid_oversized_or_ambiguous_body_never_calls_service(self):
        path = "/admin/api/pack-authoring/drafts"
        for raw in ('[]', '{"key":"a","key":"b","document_text":"{}"}',
                    '{"key":"a","document_text":NaN}', '{"key":"a","document_text":"{}","output":"/tmp/file"}'):
            with self.subTest(raw=raw):
                self.assertIn(self.client.post(path, content=raw, headers={"Content-Type":"application/json"}).status_code, (400, 422))
        self.assertEqual(self.client.post(path, content=b"x" * (4 * 1024**2 + 1), headers={"Content-Type":"application/json"}).status_code, 413)
        self.assertEqual(self.calls, [])

    def test_browser_simple_content_types_cannot_mutate_loopback_drafts(self):
        path = "/admin/api/pack-authoring/drafts"
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            response = self.client.post(path, content='{"key":"reserved","document_text":"{}"}',
                                        headers={"Content-Type":content_type,"Origin":"https://untrusted.example"})
            self.assertEqual(response.status_code, 415)
        self.assertEqual(self.calls, [])

    def test_revision_conflict_and_missing_prerequisite_are_not_success(self):
        def conflict(*args):
            raise AuthoringError("Draft changed; reload before saving.", 409)
        self.service.save_draft = conflict
        path = "/admin/api/pack-authoring/drafts/" + self.draft["id"]
        response = self.client.put(path, json={"document_text": "{}", "revision": 1})
        self.assertEqual(response.status_code, 409)
        self.assertIn("reload", response.text)

    def test_remote_requests_require_key_before_reading_or_writing(self):
        remote = TestClient(api.app, client=("203.0.113.8", 12345))
        try:
            for method, path in (("GET", "/admin/api/pack-authoring"),
                                 ("POST", "/admin/api/pack-authoring/drafts")):
                response = remote.request(method, path, json={})
                self.assertIn(response.status_code, (401, 403))
        finally:
            remote.close()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
