#!/usr/bin/env python3
"""A Mac that only ever receives packs: initialise, import, and it is available.

The tests here never invoke Node, Story Studio's source, or `build-pack.mjs`. That
is the point: this is the environment a customer has, and the whole path — authority
setup, the historical-identity guard, importing an emailed `.sspack` — has to work
with nothing but the app.
"""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_TEMP_ROOT = Path(tempfile.mkdtemp(prefix="gateway-content-import-"))
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(
    {
        "GATEWAY_APP_SUPPORT": str(_TEMP_ROOT),
        "GATEWAY_DATA": str(_TEMP_ROOT / "data"),
        "GATEWAY_ARTIFACTS": str(_TEMP_ROOT / "data" / "artifacts"),
        "GATEWAY_DB": str(_TEMP_ROOT / "data" / "jobs.sqlite3"),
        "GATEWAY_CREDENTIALS": str(_TEMP_ROOT / "data" / "credentials.json"),
        "GATEWAY_MODEL_CACHE": str(_TEMP_ROOT / "data" / "models_cache.json"),
        "GATEWAY_ENV_FILE": str(_TEMP_ROOT / ".env"),
        "GATEWAY_NO_MIGRATE": "1",
        # A customer's Mac has no Story Studio checkout. Point the source at a path
        # that does not exist, which is what "no source" means to the shelf.
        "GATEWAY_STORY_SOURCE": str(_TEMP_ROOT / "no-story-here"),
        "GATEWAY_API_KEY": "fixture-key",
        "STUDIOHUB_READY_FILE": str(_TEMP_ROOT / "no-hub-ready.json"),
    }
)

from gateway import content_delivery  # noqa: E402


def _archive(
    pack_id: str,
    *,
    title: str = "Imported Style",
    kind: str = "visual-style",
    item_id: str | None = None,
    tags: list[str] | None = None,
    version: str = "1.0.0",
    min_app_version: str = "1.4.216",
) -> bytes:
    """A minimal valid format-1 archive, built without Story Studio."""
    document = {
        "kind": kind,
        "schema_version": 1,
        "id": item_id or pack_id.replace(".pack.", "."),
        "name": title,
        "description": f"{title} description.",
        "tags": tags or [],
    }
    content = json.dumps(document).encode()
    manifest = {
        "pack_format": 1,
        "pack_id": pack_id,
        "publisher_id": "com.storystudio",
        "version": version,
        "min_app_version": min_app_version,
        "lifecycle": "active",
        "delivery_profile": "genstudio-store-receipt-v1",
        "files": [
            {
                "path": "content/style.json",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "items": [
            {"id": document["id"], "kind": kind, "path": "content/style.json", "schema_version": 1}
        ],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("manifest.json", json.dumps(manifest).encode()),
            ("content/style.json", content),
        ):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, payload)
    return buffer.getvalue()


class ContentImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # One shelf for the class: setup is deliberately idempotent, and a second
        # call is asserted to be a replay rather than a second identity.
        content_delivery.setup()

    def test_a_mac_without_story_initializes_the_same_guard(self) -> None:
        status = content_delivery.status()
        self.assertTrue(status["configured"], status)
        authority = status["authority"]
        shipped = content_delivery._shipped_baseline()
        self.assertIsNotNone(shipped)
        self.assertEqual(authority["baseline_revision"], shipped["registry_revision"])
        # The guard still refuses every historical Store identity, from the list the
        # build ships rather than from a registry this Mac does not have.
        self.assertGreater(len(content_delivery._read_state()["authority"]["baseline_pack_ids"]), 0)

    def test_the_shipped_list_carries_no_key_material(self) -> None:
        """It ships to customers, so it must state identities and nothing else."""
        text = content_delivery._HISTORICAL.read_text()
        payload = json.loads(text)
        self.assertEqual(
            sorted(payload),
            ["baseline_revision", "family_ids", "note", "pack_ids", "schema_version"],
        )
        upper = text.upper()
        for forbidden in ("PRIVATE KEY", "BEGIN ", "PUBLIC KEY", "ACCOUNT_ID", "SIGNING"):
            self.assertNotIn(forbidden, upper)
        self.assertTrue(all(value.startswith("com.storystudio.") for value in payload["pack_ids"]))

    def test_a_received_pack_becomes_available(self) -> None:
        pack_id = "com.storystudio.pack.style.received-set-v1"
        result = content_delivery.import_and_publish(_archive(pack_id, title="Received Set"))
        release = result["release"]
        # What Story is told.
        self.assertEqual(release["pack_id"], pack_id)
        self.assertEqual(release["title"], "Received Set")
        self.assertTrue(release["available"])
        self.assertTrue(release["archive_sha256"])
        # What the shelf stored: the identity is derived from the pack id by the rule
        # every published pack follows, so an imported pack keeps the identity it was
        # published under instead of acquiring a new one per install.
        stored = next(
            row for row in content_delivery._read_state()["releases"] if row["pack_id"] == pack_id
        )
        self.assertEqual(stored["product_id"], "com.storystudio.sku.style.received-set-v1")
        self.assertEqual(stored["entitlement_family"], "com.storystudio.family.style.received-set-v1")
        self.assertEqual(stored["min_story_version"], "1.4.216")
        self.assertEqual(stored["receipt_ref"], f"import:{stored['archive_sha256']}")
        listed = {row["pack_id"] for row in content_delivery.status()["releases"]}
        self.assertIn(pack_id, listed)

    def test_a_received_pack_is_served_to_story(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        pack_id = "com.storystudio.pack.style.served-set-v1"
        content_delivery.import_and_publish(_archive(pack_id, title="Served Set"))
        with TestClient(api.app, client=("127.0.0.1", 54321)) as client:
            response = client.get(
                "/v1/content/products",
                params={"story_version": "1.4.216"},
                headers={"Authorization": "Bearer fixture-key"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        products = response.json()["products"]
        served = [row for row in products if row["title"] == "Served Set"]
        self.assertEqual(len(served), 1, [row["title"] for row in products][:5])
        self.assertEqual(served[0]["kind"], "visual_style")

    def test_a_historical_store_identity_is_still_refused(self) -> None:
        shipped = content_delivery._shipped_baseline()
        historical = sorted(shipped["pack_ids"])[0]
        with self.assertRaises(content_delivery.DeliveryError) as raised:
            content_delivery.import_and_publish(_archive(historical, title="Not Ours"))
        self.assertEqual(raised.exception.code, "CONTENT_IDENTITY_HISTORICAL")

    def test_storys_tag_limit_is_applied_on_this_path_too(self) -> None:
        # Thirteen tags emptied Story's whole catalogue once, so the cap is not
        # optional just because no parser runs here.
        pack_id = "com.storystudio.pack.style.tag-heavy-v1"
        content_delivery.import_and_publish(
            _archive(pack_id, tags=[f"tag-{index}" for index in range(20)])
        )
        stored = next(
            row for row in content_delivery._read_state()["releases"] if row["pack_id"] == pack_id
        )
        self.assertEqual(len(stored["tags"]), 12)
        self.assertEqual(stored["tags"][0], "tag-0")

    def test_importing_a_folder_full_of_identical_packs_replays(self) -> None:
        pack_id = "com.storystudio.pack.style.replayed-v1"
        data = _archive(pack_id)
        first = content_delivery.import_and_publish(data)
        second = content_delivery.import_and_publish(data)
        self.assertEqual(first["release"]["release_id"], second["release"]["release_id"])
        self.assertTrue(second.get("replayed"))

    def test_setup_fails_closed_when_the_shipped_list_is_missing(self) -> None:
        """No shipped list is an authority that knows nothing, so setup must refuse."""
        with patch.object(content_delivery, "_HISTORICAL", _TEMP_ROOT / "absent.json"):
            self.assertIsNone(content_delivery._shipped_baseline())
            with self.assertRaises(content_delivery.DeliveryError) as raised:
                content_delivery.setup()
        self.assertEqual(raised.exception.code, "CONTENT_DELIVERY_UNAVAILABLE")

    def test_the_one_action_route_imports_and_serves(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        payload = _archive("com.storystudio.pack.style.routed-set-v1", title="Routed Set")
        with TestClient(api.app, client=("127.0.0.1", 54321)) as client:
            response = client.post(
                "/admin/api/content-library/import-and-publish", content=payload
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["release"]["title"], "Routed Set")
        self.assertEqual(body["archive"]["archive_sha256"], body["release"]["archive_sha256"])

    def test_the_route_refuses_a_corrupt_archive(self) -> None:
        from fastapi.testclient import TestClient

        from gateway import api

        with TestClient(api.app, client=("127.0.0.1", 54321)) as client:
            response = client.post(
                "/admin/api/content-library/import-and-publish", content=b"not a zip"
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "CONTENT_ARCHIVE_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
