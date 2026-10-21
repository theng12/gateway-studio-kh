"""Private archive integrity, never purchase/ownership authority."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
temporary = tempfile.TemporaryDirectory(prefix="gateway-pack-check-")
os.environ.update(GATEWAY_APP_SUPPORT=temporary.name, GATEWAY_NO_MIGRATE="1",
                  STUDIOHUB_READY_FILE=str(Path(temporary.name) / "no-hub.json"),
                  GATEWAY_HOST="127.0.0.1", GATEWAY_API_KEY="pack-fixture-key")
from gateway import content_packs


def archive(extra=None, mutate=None):
    document = b'{"schema_version":1,"kind":"topic-pack"}'
    manifest = {
        "pack_format": 1, "pack_id": "test.fixture.pack", "publisher_id": "test.fixture",
        "version": "1.0.0", "min_app_version": "1.0.0",
        "delivery_profile": "genstudio-store-receipt-v1", "items": [
            {"id": "test.fixture.topic", "kind": "topic-pack", "schema_version": 1, "path": "content/topic.json"}],
        "files": [{"path": "content/topic.json", "bytes": len(document),
                   "sha256": hashlib.sha256(document).hexdigest(), "mime": "application/json"}],
    }
    if mutate:
        mutate(manifest)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", json.dumps(manifest))
        output.writestr("content/topic.json", document)
        if extra:
            output.writestr(*extra)
    return data.getvalue()


class ContentLibrary(unittest.TestCase):
    def setUp(self):
        from _isolation import isolate_content
        isolate_content(self, Path(temporary.name) / "data")

    def test_integrity_is_not_install_or_ownership(self):
        row = content_packs.inspect_archive(archive())
        self.assertEqual(row["integrity"], "verified")
        self.assertEqual(row["ownership"], "unverified")
        self.assertFalse(row["installable"])
        self.assertEqual(row["item_kinds"], ["topic-pack"])

    def test_store_profile_trust_describes_portable_possession(self):
        row = content_packs.inspect_archive(archive())
        self.assertEqual(row["delivery_profile"], "genstudio-store-receipt-v1")
        self.assertEqual(row["integrity"], "verified")
        self.assertEqual(row["ownership"], "unverified")
        self.assertFalse(row["installable"])
        self.assertEqual(
            row["trust_requirement"],
            "Structural integrity and declared file hashes checked; publisher signature "
            "absent. A compatible pack you possess installs in Story Studio without an account.",
        )

    def test_bad_hash_and_incompatible_format_refused(self):
        for mutate in (lambda m: m["files"][0].update(sha256="0" * 64),
                       lambda m: m.update(pack_format=2),
                       lambda m: m.update(version="../escape"),
                       lambda m: m["items"][0].update(kind={}),
                       lambda m: m["items"][0].update(path=[])):
            with self.assertRaises(content_packs.PackError):
                content_packs.inspect_archive(archive(mutate=mutate))

    def test_unsafe_and_undeclared_members_refused(self):
        link = zipfile.ZipInfo("assets/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for member in ("../escape", "/absolute", "content\\evil", "CONTENT/topic.json", "extra.exe", link):
            with self.assertRaises(content_packs.PackError):
                content_packs.inspect_archive(archive((member, b"bad")))

    def test_duplicate_json_and_duplicate_zip_refused(self):
        with self.assertRaises(content_packs.PackError):
            content_packs.inspect_archive(archive(("manifest.json", b'{"a":1,"a":2}')))
        with self.assertRaises(content_packs.PackError):
            content_packs._json(b'{"a":1,"a":2}')

    def test_malformed_utf8_zip_filename_is_typed_invalid(self):
        data = bytearray(archive())
        index = data.index(b"PK\x01\x02")
        data[index + 9] |= 8
        data[index + 46] = 255
        with self.assertRaises(content_packs.PackError):
            content_packs.inspect_archive(bytes(data))

    def test_concurrent_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            data = archive()
            barrier = threading.Barrier(2)
            rename = Path.rename
            def together(path, target):
                if path.name.startswith(".import-"):
                    barrier.wait(timeout=5)
                return rename(path, target)
            with patch.object(Path, "rename", together), ThreadPoolExecutor(2) as executor:
                pending = [executor.submit(content_packs.import_archive, data, root=root) for _ in range(2)]
                rows = [future.result() for future in pending]
            self.assertEqual(rows[0], rows[1])
            self.assertEqual(len(content_packs.inventory(root=root)), 1)

    def test_inventory_rechecks_preserved_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            row = content_packs.import_archive(archive(), root=root)
            stored = root / row["archive_sha256"] / "archive.sspack"
            stored.write_bytes(b"corrupt")
            self.assertNotEqual(content_packs.inventory(root=root)[0]["integrity"], "verified")
            stored.unlink()
            self.assertNotEqual(content_packs.inventory(root=root)[0]["integrity"], "verified")

    def test_inventory_hashes_an_unchanged_archive_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            row = content_packs.import_archive(archive(), root=root)
            digest = row["archive_sha256"]
            content_packs._VERIFIED.clear()
            with patch.object(content_packs, "read_archive", wraps=content_packs.read_archive) as read:
                for _ in range(3):
                    self.assertEqual(content_packs.inventory(root=root)[0]["integrity"], "verified")
                self.assertEqual(read.call_count, 1, "unchanged archive was re-read and re-hashed")
                stored = root / digest / "archive.sspack"
                stored.write_bytes(stored.read_bytes()[:-1] + b"X")  # same size, new mtime
                self.assertNotEqual(content_packs.inventory(root=root)[0]["integrity"], "verified")

    def test_import_is_private_idempotent_and_download_checks_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            data = archive()
            first = content_packs.import_archive(data, root=root)
            second = content_packs.import_archive(data, root=root)
            self.assertEqual(first["archive_sha256"], second["archive_sha256"])
            self.assertEqual(len(content_packs.inventory(root=root)), 1)
            digest = first["archive_sha256"]
            self.assertEqual(content_packs.read_archive(digest, root=root), data)
            stored = root / digest / "archive.sspack"
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            stored.write_bytes(b"corrupt")
            with self.assertRaises(content_packs.PackError):
                content_packs.read_archive(digest, root=root)
            with self.assertRaises(content_packs.PackError):
                content_packs.read_archive("../escape", root=root)

    def test_http_import_download_and_unauthorized_refusal(self):
        from fastapi.testclient import TestClient
        from gateway.api import app
        client = TestClient(app, client=("127.0.0.1", 10000))
        remote = TestClient(app, client=("192.0.2.1", 10000))
        try:
            data = archive()
            refused = remote.post("/admin/api/content-library/import", content=data)
            self.assertEqual(refused.status_code, 401)
            invalid = client.post("/admin/api/content-library/import", content=b"not a zip")
            self.assertEqual(invalid.status_code, 422)
            imported = client.post("/admin/api/content-library/import", content=data)
            self.assertEqual(imported.status_code, 200)
            row = imported.json()
            self.assertFalse(row["installable"])
            response = client.get(f"/admin/api/content-library/{row['archive_sha256']}/archive")
            self.assertEqual(response.content, data)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(len(client.get("/admin/api/content-library").json()["archives"]), 1)
        finally:
            client.close()
            remote.close()


if __name__ == "__main__":
    unittest.main()
