"""Tests for retiring, un-retiring, and removing Content Packs."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEMP = tempfile.TemporaryDirectory(prefix="gateway-retire-remove-")
SUPPORT = Path(TEMP.name)

os.environ.update({
    "GATEWAY_APP_SUPPORT": str(SUPPORT),
    "GATEWAY_DATA": str(SUPPORT / "data"),
    "GATEWAY_ARTIFACTS": str(SUPPORT / "artifacts"),
    "GATEWAY_DB": str(SUPPORT / "jobs.sqlite3"),
    "GATEWAY_CREDENTIALS": str(SUPPORT / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(SUPPORT / "models.json"),
    "GATEWAY_ENV_FILE": str(SUPPORT / ".env"),
    "GATEWAY_FLEET": str(SUPPORT / "fleet.json"),
    "GATEWAY_VOICES": str(SUPPORT / "voices.json"),
    "GATEWAY_VOICES_OUT": str(SUPPORT / "voices.json"),
    "STUDIOHUB_READY_FILE": str(SUPPORT / "no-hub.json"),
    "GATEWAY_NO_MIGRATE": "1",
    "GATEWAY_HOST": "127.0.0.1",
    "GATEWAY_API_KEY": "fixture-api-key",
})

from fastapi.testclient import TestClient
from gateway.api import app
from gateway import content_delivery, content_packs
from _isolation import isolate_content


def make_archive(pack_id: str, version: str = "1.0.0", title: str = "Fixture Pack", body: bytes | None = None) -> bytes:
    doc = json.dumps({
        "schema_version": 1,
        "kind": "topic-pack",
        "id": f"{pack_id}.item",
        "name": title,
        "description": f"{title} description.",
    }).encode() if body is None else body

    slug = pack_id.split(".")[-1]
    manifest = {
        "pack_format": 1,
        "pack_id": pack_id,
        "publisher_id": "com.storystudio",
        "version": version,
        "min_app_version": "1.4.213",
        "delivery_profile": "genstudio-store-receipt-v1",
        "entitlement": {
            "sku": f"com.storystudio.sku.{slug}",
            "family": f"com.storystudio.family.{slug}",
        },
        "items": [
            {"id": f"{pack_id}.item", "kind": "topic-pack", "schema_version": 1, "path": "content/topic.json"}
        ],
        "files": [
            {"path": "content/topic.json", "bytes": len(doc), "sha256": hashlib.sha256(doc).hexdigest()}
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("content/topic.json", doc)
    return buf.getvalue()


class ContentRetireRemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = SUPPORT / "data"
        isolate_content(self, self.data_dir, "fixture-api-key")
        for p in (self.data_dir / "content-delivery", self.data_dir / "content-library",
                  self.data_dir / "content-trash", self.data_dir / "_trash"):
            if p.exists():
                import shutil
                shutil.rmtree(p)
        self.client = TestClient(app, base_url="http://127.0.0.1:8787", client=("127.0.0.1", 41001))
        content_delivery.setup()

    def tearDown(self) -> None:
        self.client.close()

    def test_retire_and_unretire(self) -> None:
        pack_id = "com.storystudio.pack.topic.retire1"
        archive_bytes = make_archive(pack_id, "1.0.0", "Retire One")
        content_delivery.import_and_publish(archive_bytes)

        # Retiring the only/newest version requires confirm_latest
        with self.assertRaises(content_delivery.DeliveryError) as cm:
            content_delivery.retire_pack(pack_id)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.code, "CONFIRM_LATEST_REQUIRED")
        self.assertIn("newest version", str(cm.exception))

        # With confirm_latest=True, it succeeds
        res = content_delivery.retire_pack(pack_id, confirm_latest=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["retired"])

        # Check status shows retired
        st = content_delivery.status()
        rel = next(r for r in st["releases"] if r["pack_id"] == pack_id)
        self.assertTrue(rel["retired"])
        self.assertIsNotNone(rel["retired_at"])
        self.assertFalse(rel["current"])

        # Un-retire makes it active again
        res_un = content_delivery.unretire_pack(pack_id)
        self.assertTrue(res_un["ok"])
        self.assertFalse(res_un["retired"])

        st2 = content_delivery.status()
        rel2 = next(r for r in st2["releases"] if r["pack_id"] == pack_id)
        self.assertFalse(rel2.get("retired", False))
        self.assertTrue(rel2["current"])

    def test_catalogue_hiding(self) -> None:
        pack_id = "com.storystudio.pack.topic.hiding"
        archive_bytes = make_archive(pack_id, "1.0.0", "Hiding Pack")
        content_delivery.import_and_publish(archive_bytes)

        # Visible before retirement
        prods = content_delivery.products("1.4.213")["products"]
        self.assertTrue(any(p["title"] == "Hiding Pack" for p in prods))
        shelf = content_delivery.shelves("1.4.213")["shelves"]
        self.assertTrue(len(shelf) > 0 and len(shelf[0]["product_ids"]) > 0)

        # Retire
        content_delivery.retire_pack(pack_id, confirm_latest=True)

        # Hidden after retirement
        prods_retired = content_delivery.products("1.4.213")["products"]
        self.assertFalse(any(p["title"] == "Hiding Pack" for p in prods_retired))
        shelf_retired = content_delivery.shelves("1.4.213")["shelves"]
        self.assertEqual(shelf_retired, [])

        # Un-retire: reappears
        content_delivery.unretire_pack(pack_id)
        prods_restored = content_delivery.products("1.4.213")["products"]
        self.assertTrue(any(p["title"] == "Hiding Pack" for p in prods_restored))

    def test_install_refusal(self) -> None:
        pack_id = "com.storystudio.pack.topic.installrefusal"
        archive_bytes = make_archive(pack_id, "1.0.0", "Install Refusal")
        pub = content_delivery.import_and_publish(archive_bytes)
        release_id = pub["release"]["release_id"]
        family = pub["archive"]["entitlement"]["family"]

        content_delivery.retire_pack(pack_id, confirm_latest=True)

        # Direct issue_download refusal
        with self.assertRaises(content_delivery.DeliveryError) as cm1:
            content_delivery.issue_download(release_id, "http://127.0.0.1:8787", "fixture-api-key")
        self.assertEqual(cm1.exception.status, 410)
        self.assertEqual(cm1.exception.code, "CONTENT_PRODUCT_RETIRED")

        # Family resolution refusal
        with self.assertRaises(content_delivery.DeliveryError) as cm2:
            content_delivery.resolve_release_id(family, "1.0.0")
        self.assertEqual(cm2.exception.status, 410)
        self.assertEqual(cm2.exception.code, "CONTENT_PRODUCT_RETIRED")

        # HTTP API refusal
        resp = self.client.post("/v1/content/downloads",
                                headers={"Authorization": "Bearer fixture-api-key"},
                                json={"release_id": release_id})
        self.assertEqual(resp.status_code, 410)
        self.assertIn("CONTENT_PRODUCT_RETIRED", resp.json().get("code", ""))

    def test_existing_install_still_served(self) -> None:
        pack_id = "com.storystudio.pack.topic.existingserved"
        archive_bytes = make_archive(pack_id, "1.0.0", "Existing Served")
        pub = content_delivery.import_and_publish(archive_bytes)
        release_id = pub["release"]["release_id"]

        # Issue grant BEFORE retirement
        grant = content_delivery.issue_download(release_id, "http://127.0.0.1:8787", "fixture-api-key")
        token = grant["download_url"].split("grant=")[1]

        # Now retire the pack
        content_delivery.retire_pack(pack_id, confirm_latest=True)

        # Existing grant token can still download the archive bytes
        downloaded = content_delivery.download_archive(release_id, token)
        self.assertEqual(downloaded, archive_bytes)

        # HTTP download route also succeeds with existing grant
        resp = self.client.get(f"/v1/content/archives/{release_id}", params={"grant": token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, archive_bytes)

        # Receipts route still serves the restore receipt for the family (with retired: True)
        receipts = content_delivery.receipts()
        entitlement = next(e for e in receipts["entitlements"] if e["title"] == "Existing Served")
        self.assertTrue(entitlement["retired"])
        self.assertFalse(entitlement["download_available"])

        # Restore receipt release is intact
        restore_rel = next(r for r in receipts["restore_receipt"]["receipt"]["releases"] if r["release_id"] == release_id)
        self.assertEqual(restore_rel["release_id"], release_id)

    def test_remove_refused_for_non_retired_pack(self) -> None:
        pack_id = "com.storystudio.pack.topic.notretired"
        archive_bytes = make_archive(pack_id, "1.0.0", "Not Retired")
        pub = content_delivery.import_and_publish(archive_bytes)
        digest = pub["release"]["archive_sha256"]

        # Attempt to remove an active (non-retired) pack
        with self.assertRaises(content_delivery.DeliveryError) as cm:
            content_delivery.remove_pack(pack_id, confirm=True)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.code, "CONTENT_PACK_NOT_RETIRED")

        # Archive file remains in library
        archive_file = self.data_dir / "content-library" / digest / "archive.sspack"
        self.assertTrue(archive_file.is_file())

    def test_remove_moves_to_trash_with_manifest_and_drops_records(self) -> None:
        pack_id = "com.storystudio.pack.topic.toberemoved"
        archive_bytes = make_archive(pack_id, "1.0.0", "To Be Removed")
        pub = content_delivery.import_and_publish(archive_bytes)
        digest = pub["release"]["archive_sha256"]

        # Retire first
        content_delivery.retire_pack(pack_id, confirm_latest=True)

        # Remove requires confirm=True
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.remove_pack(pack_id, confirm=False)

        # Remove with confirmation
        res = content_delivery.remove_pack(pack_id, confirm=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["removed"])

        # Archive file moved out of content-library
        active_path = self.data_dir / "content-library" / digest / "archive.sspack"
        self.assertFalse(active_path.exists())

        # Archive file is in content-trash
        trash_dir = self.data_dir / "content-trash" / digest
        trash_archive = trash_dir / "archive.sspack"
        self.assertTrue(trash_archive.is_file())
        self.assertEqual(trash_archive.read_bytes(), archive_bytes)

        # Sidecars like metadata.json also moved
        trash_meta = trash_dir / "metadata.json"
        self.assertTrue(trash_meta.is_file())

        # Manifest entry written in trash folder
        manifest_path = trash_dir / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        entry = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(entry["what"], f"{pack_id}@1.0.0")
        self.assertEqual(entry["sha256"], digest)
        self.assertIn("content-library", entry["from_where"])
        self.assertIn("when", entry)

        # Released record dropped from state["releases"]
        st = content_delivery.status()
        self.assertFalse(any(r["pack_id"] == pack_id for r in st["releases"]))

        # Dropped pack recorded in state["removed"]
        removed = content_delivery._configured_state().get("removed", [])
        self.assertTrue(any(r["pack_id"] == pack_id and r["archive_sha256"] == digest for r in removed))

    def test_multi_version_pack_retire_previous_vs_newest(self) -> None:
        pack_id = "com.storystudio.pack.topic.multiversion"
        doc1 = json.dumps({"schema_version": 1, "kind": "topic-pack", "id": f"{pack_id}.item", "name": "MV 1.0.0", "description": "d1"}).encode()
        doc2 = json.dumps({"schema_version": 1, "kind": "topic-pack", "id": f"{pack_id}.item", "name": "MV 1.0.1", "description": "d2"}).encode()

        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "MV 1.0.0", doc1))
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.1", "MV 1.0.1", doc2))

        # Catalogue initially offers 1.0.1
        prod = next(p for p in content_delivery.products("1.4.213")["products"] if "multiversion" in p["product_id"])
        self.assertEqual(prod["current_release"]["version"], "1.0.1")

        # Retiring older version 1.0.0 does NOT require confirm_latest
        res1 = content_delivery.retire_pack(pack_id, version="1.0.0")
        self.assertTrue(res1["ok"])

        # Catalogue still offers 1.0.1
        prod = next(p for p in content_delivery.products("1.4.213")["products"] if "multiversion" in p["product_id"])
        self.assertEqual(prod["current_release"]["version"], "1.0.1")

        # Retiring newest version 1.0.1 requires confirm_latest
        with self.assertRaises(content_delivery.DeliveryError):
            content_delivery.retire_pack(pack_id, version="1.0.1")

        content_delivery.retire_pack(pack_id, version="1.0.1", confirm_latest=True)

        # Both retired -> catalogue hides the pack
        self.assertFalse(any("multiversion" in p["product_id"] for p in content_delivery.products("1.4.213")["products"]))

        # Un-retire 1.0.0 -> catalogue falls back to offering 1.0.0
        content_delivery.unretire_pack(pack_id, version="1.0.0")
        prod = next(p for p in content_delivery.products("1.4.213")["products"] if "multiversion" in p["product_id"])
        self.assertEqual(prod["current_release"]["version"], "1.0.0")

    def test_admin_api_retire_unretire_remove_routes(self) -> None:
        pack_id = "com.storystudio.pack.topic.adminroutes"
        archive_bytes = make_archive(pack_id, "1.0.0", "Admin Routes Pack")
        content_delivery.import_and_publish(archive_bytes)

        # POST /api/content-shelf/retire without confirm_latest -> 409
        r = self.client.post("/admin/api/content-shelf/retire", json={"pack_id": pack_id})
        self.assertEqual(r.status_code, 409)

        # POST with confirm_latest -> 200
        r2 = self.client.post("/admin/api/content-shelf/retire", json={"pack_id": pack_id, "confirm_latest": True})
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["retired"])

        # POST /api/content-shelf/unretire -> 200
        r3 = self.client.post("/admin/api/content-shelf/unretire", json={"pack_id": pack_id})
        self.assertEqual(r3.status_code, 200)
        self.assertFalse(r3.json()["retired"])

        # Remove while active -> 409
        r4 = self.client.post("/admin/api/content-shelf/remove", json={"pack_id": pack_id, "confirm": True})
        self.assertEqual(r4.status_code, 409)

        # Retire then remove -> 200
        self.client.post("/admin/api/content-shelf/retire", json={"pack_id": pack_id, "confirm_latest": True})
        r5 = self.client.post("/admin/api/content-shelf/remove", json={"pack_id": pack_id, "confirm": True})
        self.assertEqual(r5.status_code, 200)
        self.assertTrue(r5.json()["removed"])

    def test_record_removed_migration_helper(self) -> None:
        pack_id = "com.storystudio.pack.topic.migrationfixture"
        archive_bytes = make_archive(pack_id, "1.0.0", "Migration Fixture")
        pub = content_delivery.import_and_publish(archive_bytes)
        digest = pub["release"]["archive_sha256"]

        # Call record_removed
        dummy_backup_path = "/tmp/fake-backup-location/archive.sspack"
        res = content_delivery.record_removed(
            pack_id, "1.0.0", digest,
            trash_path=dummy_backup_path,
            title="Migration Fixture"
        )
        self.assertTrue(res["ok"])

        # Release is removed from active releases
        st = content_delivery.status()
        self.assertFalse(any(r["archive_sha256"] == digest for r in st["releases"]))

        # Recorded in state["removed"]
        state = content_delivery._configured_state()
        entry = next(e for e in state["removed"] if e["archive_sha256"] == digest)
        self.assertEqual(entry["trash_path"], dummy_backup_path)
        self.assertEqual(entry["what"], f"{pack_id}@1.0.0")

    # ── "Also remove from every Story app" (owner, 2026-10-29) ─────────────────────────────
    # Retire still keeps the file and hides it; this is a second, per-pack, deliberate ask that
    # each Story app remove its own copy. Off unless the owner says so.

    def test_retiring_alone_stores_no_removal_ask(self) -> None:
        pack_id = "com.storystudio.pack.topic.retireonly"
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Retire Only"))

        res = content_delivery.retire_pack(pack_id, confirm_latest=True)
        self.assertFalse(res["remove_from_installs"])
        self.assertIsNone(res["remove_from_installs_at"])
        self.assertIn("keep the copy", res["story_apps"])

        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertTrue(row["retired"])
        self.assertIs(row["remove_from_installs"], False)
        self.assertNotIn("remove_from_installs_at", row)

    def test_retiring_with_the_option_stores_the_ask_and_its_unix_stamp(self) -> None:
        pack_id = "com.storystudio.pack.topic.retireandremove"
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Retire And Remove"))

        before = int(time.time())
        res = content_delivery.retire_pack(pack_id, confirm_latest=True, remove_from_installs=True)
        after = int(time.time())
        self.assertTrue(res["remove_from_installs"])
        self.assertIsInstance(res["remove_from_installs_at"], int, "the wire field is unix seconds")
        self.assertTrue(before <= res["remove_from_installs_at"] <= after)
        self.assertIn("told to remove", res["story_apps"])

        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertIs(row["remove_from_installs"], True)
        self.assertEqual(row["remove_from_installs_at"], res["remove_from_installs_at"])

        # A SECOND plain retire must not quietly withdraw an ask the owner already made.
        content_delivery.retire_pack(pack_id, confirm_latest=True)
        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertIs(row["remove_from_installs"], True)

    def test_the_ask_can_be_made_and_withdrawn_on_an_already_retired_pack(self) -> None:
        pack_id = "com.storystudio.pack.topic.laterask"
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Later Ask"))
        content_delivery.retire_pack(pack_id, confirm_latest=True)

        asked = content_delivery.set_remove_from_installs(pack_id, enabled=True)
        self.assertTrue(asked["remove_from_installs"])
        self.assertIsInstance(asked["remove_from_installs_at"], int)
        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertIs(row["remove_from_installs"], True)

        cleared = content_delivery.set_remove_from_installs(pack_id, enabled=False)
        self.assertFalse(cleared["remove_from_installs"])
        self.assertIsNone(cleared["remove_from_installs_at"])
        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertIs(row["remove_from_installs"], False)
        self.assertNotIn("remove_from_installs_at", row)
        # The pack is still retired: withdrawing the ask is not un-retiring.
        self.assertTrue(row["retired"])

    def test_a_live_pack_cannot_be_marked_for_removal(self) -> None:
        pack_id = "com.storystudio.pack.topic.liveask"
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Live Ask"))
        with self.assertRaises(content_delivery.DeliveryError) as cm:
            content_delivery.set_remove_from_installs(pack_id, enabled=True)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "CONTENT_PACK_NOT_RETIRED"))

    def test_unretiring_withdraws_the_ask_too(self) -> None:
        pack_id = "com.storystudio.pack.topic.unretireask"
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Unretire Ask"))
        content_delivery.retire_pack(pack_id, confirm_latest=True, remove_from_installs=True)

        res = content_delivery.unretire_pack(pack_id)
        self.assertFalse(res["remove_from_installs"])
        row = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)
        self.assertFalse(row.get("retired", False))
        self.assertFalse(row.get("remove_from_installs", False))
        self.assertNotIn("remove_from_installs_at", row)

    def test_the_catalogue_read_carries_retired_packs_and_the_ask(self) -> None:
        """A retired pack stays out of the catalogue but its id and the ask reach Story."""
        pack_id = "com.storystudio.pack.topic.catalogueask"
        pub = content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Catalogue Ask"))
        release_id = pub["release"]["release_id"]
        content_delivery.retire_pack(pack_id, confirm_latest=True, remove_from_installs=True)
        asked_at = next(r for r in content_delivery.status()["releases"] if r["pack_id"] == pack_id)["remove_from_installs_at"]

        catalogue = content_delivery.products("1.4.213")
        self.assertFalse(any(p["title"] == "Catalogue Ask" for p in catalogue["products"]),
                         "a retired pack must not be offered as something to install")
        entry = next(p for p in catalogue["retired_packs"] if p["pack_id"] == pack_id)
        self.assertEqual(set(entry), {"pack_id", "release_id", "retired_at",
                                      "remove_from_installs", "remove_from_installs_at"})
        self.assertEqual(entry["release_id"], release_id)
        self.assertIsInstance(entry["retired_at"], str)
        self.assertIs(entry["remove_from_installs"], True)
        self.assertEqual(entry["remove_from_installs_at"], asked_at)

        # Over HTTP, on the call Story actually makes.
        response = self.client.get("/v1/content/products", params={"story_version": "1.4.213"},
                                   headers={"Authorization": "Bearer fixture-api-key"})
        self.assertEqual(response.status_code, 200, response.text[:200])
        body = response.json()
        self.assertEqual(next(p for p in body["retired_packs"] if p["pack_id"] == pack_id)["remove_from_installs"], True)

        # Un-retiring removes it from the retired list as well as restoring the product.
        content_delivery.unretire_pack(pack_id)
        restored = content_delivery.products("1.4.213")
        self.assertFalse(any(p["pack_id"] == pack_id for p in restored["retired_packs"]))
        self.assertTrue(any(p["title"] == "Catalogue Ask" for p in restored["products"]))

    def test_the_admin_routes_take_the_option_and_validate_it(self) -> None:
        pack_id = "com.storystudio.pack.topic.adminask"
        pub = content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Admin Ask"))
        release_id = pub["release"]["release_id"]

        # Retire over the route, with the option.
        r = self.client.post("/admin/api/content-shelf/retire",
                             json={"release_id": release_id, "confirm_latest": True,
                                   "remove_from_installs": True})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertTrue(r.json()["remove_from_installs"])
        shelf = self.client.get("/admin/api/content-shelf").json()
        row = next(x for x in shelf["releases"] if x["release_id"] == release_id)
        self.assertIs(row["remove_from_installs"], True)

        # A string is not a boolean: a durable ask on customers' Macs is never coerced.
        bad = self.client.post("/admin/api/content-shelf/retire",
                               json={"release_id": release_id, "remove_from_installs": "false"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(bad.json()["code"], "CONTENT_REQUEST_INVALID")

        # Withdraw and re-make the ask later through the separate action.
        off = self.client.post("/admin/api/content-shelf/remove-from-installs",
                               json={"release_id": release_id, "enabled": False})
        self.assertEqual(off.status_code, 200, off.text[:200])
        self.assertFalse(off.json()["remove_from_installs"])
        on = self.client.post("/admin/api/content-shelf/remove-from-installs",
                              json={"release_id": release_id, "enabled": True})
        self.assertEqual(on.status_code, 200, on.text[:200])
        self.assertTrue(on.json()["remove_from_installs"])
        self.assertIsInstance(on.json()["remove_from_installs_at"], int)

        # `enabled` must be a real boolean, and the pack must be retired.
        self.assertEqual(self.client.post("/admin/api/content-shelf/remove-from-installs",
                                          json={"release_id": release_id, "enabled": "yes"}).status_code, 422)
        live = "com.storystudio.pack.topic.adminlive"
        live_pub = content_delivery.import_and_publish(make_archive(live, "1.0.0", "Admin Live"))
        refused = self.client.post("/admin/api/content-shelf/remove-from-installs",
                                   json={"release_id": live_pub["release"]["release_id"], "enabled": True})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "CONTENT_PACK_NOT_RETIRED")

    def test_the_shelf_shows_what_each_story_mac_reported(self) -> None:
        """`removed on 1 of 2` is drawn from the Macs' own words; an unreported Mac is named."""
        pack_id = "com.storystudio.pack.topic.reportcard"
        pub = content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Report Card"))
        content_delivery.retire_pack(pack_id, confirm_latest=True, remove_from_installs=True)
        asked_at = next(r for r in content_delivery.status()["releases"]
                        if r["pack_id"] == pack_id)["remove_from_installs_at"]

        instances = {
            "mac-a": {"instance_id": "mac-a", "machine": "mac-a", "last_seen": asked_at + 60,
                      "pack_removals": [{"pack_id": pack_id, "outcome": "removed", "channels": []}],
                      "pack_removals_at": asked_at + 60},
            "mac-b": {"instance_id": "mac-b", "machine": "mac-b", "last_seen": asked_at + 60,
                      "pack_removals": [{"pack_id": pack_id, "outcome": "kept_in_use",
                                         "channels": ["Night Falls"]}],
                      "pack_removals_at": asked_at + 60},
        }
        path = self.data_dir / "story-instances.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(instances), encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)

        shelf = self.client.get("/admin/api/content-shelf").json()
        row = next(x for x in shelf["releases"] if x["release_id"] == pub["release"]["release_id"])
        report = row["remove_report"]
        self.assertEqual((report["removed_count"], report["total_instances"], report["reported_count"]),
                         (1, 2, 2))
        self.assertEqual(report["kept_in_use"][0]["machine"], "mac-b")
        self.assertEqual(report["kept_in_use"][0]["channels"], ["Night Falls"])

        # A Mac that last reported BEFORE the owner asked answers nothing: "not reported yet".
        instances["mac-b"]["pack_removals_at"] = asked_at - 60
        path.write_text(json.dumps(instances), encoding="utf-8")
        shelf = self.client.get("/admin/api/content-shelf").json()
        row = next(x for x in shelf["releases"] if x["release_id"] == pub["release"]["release_id"])
        self.assertEqual(row["remove_report"]["removed_count"], 1)
        self.assertEqual(row["remove_report"]["reported_count"], 1)
        self.assertEqual(row["remove_report"]["not_reported"], ["mac-b"])

        # No ask → no report is drawn at all.
        content_delivery.set_remove_from_installs(pack_id, enabled=False)
        shelf = self.client.get("/admin/api/content-shelf").json()
        row = next(x for x in shelf["releases"] if x["release_id"] == pub["release"]["release_id"])
        self.assertNotIn("remove_report", row)

    # ---- A retired family and the contract Story parses -----------------------
    #
    # Story's `parseContentStoreReceipts` refuses the WHOLE payload when one entitlement's
    # `download_available` disagrees with its `current_release.archive_available`
    # (`electron/server/lib/genstudio-content-store.ts`). A retired family whose archive is still
    # on disk used to say false beside true, so every install and Restore failed on every shipped
    # Story. These tests encode Story's rules, not just the one live example.

    def _story_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer fixture-api-key"}

    def _assert_story_entitlement_rule(self, receipts: dict) -> None:
        """Story's receipts parser, in Python: every entitlement row must agree with its release."""
        rows = receipts["entitlements"]
        self.assertTrue(rows, "the fixture owns at least one pack")
        for index, entitlement in enumerate(rows):
            release = entitlement.get("current_release")
            self.assertEqual(
                bool(entitlement["download_available"]),
                bool(release and release["archive_available"]),
                f"entitlements[{index}] download_available must match current release "
                "archive availability (Story rejects the whole payload otherwise)")

    def _assert_history_matches_entitlement(self, entitlement: dict, history: dict) -> None:
        """Story's `ownedReleaseHistory`: every identity field and the current release must agree."""
        for field in ("entitlement_family", "entitlement_id", "product_id", "title", "kind",
                      "publisher_id", "granted_at", "retired"):
            self.assertEqual(history[field], entitlement[field],
                             f"release history {field} must match the verified entitlement")
        expected = (entitlement.get("current_release") or {}).get("release_id")
        self.assertEqual(history["current_release_id"], expected,
                         "the receipt's current_release must be the history's current_release_id")
        current = [row for row in history["releases"] if row["current"]]
        if expected is None:
            self.assertEqual(current, [], "no current release is named, so no row may claim to be it")
        else:
            self.assertEqual([row["release_id"] for row in current], [expected],
                             "exactly the named release is current")

    def _verify_signature(self, public_key_base64: str, payload: dict, signature: str) -> None:
        """The same ed25519 check `test_content_delivery` runs, so a signed payload stays signed."""
        program = """
import { createPublicKey, verify } from 'node:crypto';
const chunks=[]; for await (const chunk of process.stdin) chunks.push(chunk);
const row=JSON.parse(Buffer.concat(chunks));
const canonical=value => Array.isArray(value) ? `[${value.map(canonical).join(',')}]`
  : value && typeof value === 'object' ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  : JSON.stringify(value);
const key=createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),Buffer.from(row.public_key_base64,'base64')]),format:'der',type:'spki'});
process.exit(verify(null,Buffer.from(canonical(row.payload),'utf8'),key,Buffer.from(row.signature,'base64')) ? 0 : 1);
"""
        checked = subprocess.run(
            [shutil.which("node") or "node", "--input-type=module", "-e", program],
            input=json.dumps({"public_key_base64": public_key_base64, "payload": payload,
                              "signature": signature}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr.decode())

    def test_a_retired_family_with_its_archive_on_disk_is_consistent_for_story(self) -> None:
        pack_id = "com.storystudio.pack.style.oil-painting-classical-v2"
        archive = make_archive(pack_id, "1.0.0", "Oil Painting Classical")
        pub = content_delivery.import_and_publish(archive)
        family = pub["archive"]["entitlement"]["family"]
        release_id = pub["release"]["release_id"]
        digest = pub["release"]["archive_sha256"]
        content_delivery.retire_pack(pack_id, confirm_latest=True)
        # The archive is still on disk: that is the state that used to break the parser.
        self.assertTrue((self.data_dir / "content-library" / digest / "archive.sspack").is_file())

        headers = self._story_headers()
        receipts = self.client.get("/v1/content/receipts", headers=headers).json()
        self._assert_story_entitlement_rule(receipts)
        entitlement = next(e for e in receipts["entitlements"] if e["entitlement_family"] == family)
        self.assertTrue(entitlement["retired"])
        self.assertFalse(entitlement["download_available"])
        self.assertIsNotNone(entitlement["current_release"])
        self.assertFalse(entitlement["current_release"]["archive_available"],
                         "a refused download is not an available archive")

        # Story draws its overview's `downloadAvailable` from this row.
        account = self.client.get("/v1/content/account", headers=headers).json()
        owned = next(o for o in account["owned"] if o["entitlement_family"] == family)
        self.assertFalse(owned["current_release"]["archive_available"])

        history = self.client.get(
            f"/v1/content/entitlements/{family}/releases", headers=headers).json()
        self._assert_history_matches_entitlement(entitlement, history)
        self.assertTrue(history["retired"])
        self.assertFalse(any(row["archive_available"] for row in history["releases"]),
                         "every version of a retired family is refused by `issue_download`")

        # The catalogue still hides a retired family, and still tells Story to remove a copy.
        products = self.client.get("/v1/content/products",
                                   params={"story_version": "1.4.213"}, headers=headers).json()
        self.assertFalse(any(p["entitlement_family"] == family for p in products["products"]),
                         "products must not offer a retired family")
        self.assertIn(pack_id, [row["pack_id"] for row in products["retired_packs"]])
        shelves = self.client.get("/v1/content/shelves",
                                  params={"story_version": "1.4.213"}, headers=headers).json()
        offered = {product_id for shelf in shelves["shelves"] for product_id in shelf["product_ids"]}
        self.assertNotIn(entitlement["product_id"], offered)

        # The refusal itself is unchanged (6729f29).
        refused = self.client.post("/v1/content/downloads", headers=headers,
                                   json={"release_id": release_id})
        self.assertEqual(refused.status_code, 410)
        self.assertIn("CONTENT_PRODUCT_RETIRED", refused.json().get("code", ""))

        # Nothing inside the signed restore receipt changed, and it still verifies.
        keys = self.client.get("/v1/content/public-key", headers=headers).json()
        signed = receipts["restore_receipt"]
        self._verify_signature(keys["public_key_base64"], signed["receipt"],
                               signed["receipt_signature"])
        self.assertIn(release_id, [row["release_id"] for row in signed["receipt"]["releases"]])

    def test_a_live_family_still_reports_a_downloadable_release(self) -> None:
        pack_id = "com.storystudio.pack.topic.livereceipt"
        pub = content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Live Receipt"))
        family = pub["archive"]["entitlement"]["family"]
        headers = self._story_headers()

        receipts = self.client.get("/v1/content/receipts", headers=headers).json()
        self._assert_story_entitlement_rule(receipts)
        entitlement = next(e for e in receipts["entitlements"] if e["entitlement_family"] == family)
        self.assertFalse(entitlement["retired"])
        self.assertTrue(entitlement["download_available"])
        self.assertTrue(entitlement["current_release"]["archive_available"])

        history = self.client.get(
            f"/v1/content/entitlements/{family}/releases", headers=headers).json()
        self._assert_history_matches_entitlement(entitlement, history)
        self.assertFalse(history["retired"])
        self.assertTrue(all(row["archive_available"] for row in history["releases"]))

        # A live family is still offered, and still downloadable.
        products = self.client.get("/v1/content/products",
                                   params={"story_version": "1.4.213"}, headers=headers).json()
        self.assertTrue(any(p["entitlement_family"] == family for p in products["products"]))
        shelves = self.client.get("/v1/content/shelves",
                                  params={"story_version": "1.4.213"}, headers=headers).json()
        offered = {product_id for shelf in shelves["shelves"] for product_id in shelf["product_ids"]}
        self.assertIn(entitlement["product_id"], offered)
        grant = self.client.post("/v1/content/downloads", headers=headers,
                                 json={"release_id": entitlement["current_release"]["release_id"]})
        self.assertEqual(grant.status_code, 200, grant.text)

    def test_a_mixed_family_history_describes_the_receipts_ownership(self) -> None:
        """A retired older version beside a live newer one: both parsers must agree on the family."""
        pack_id = "com.storystudio.pack.topic.mixedreceipt"
        doc1 = json.dumps({"schema_version": 1, "kind": "topic-pack", "id": f"{pack_id}.item",
                           "name": "Mixed 1.0.0", "description": "d1"}).encode()
        doc2 = json.dumps({"schema_version": 1, "kind": "topic-pack", "id": f"{pack_id}.item",
                           "name": "Mixed 1.0.1", "description": "d2"}).encode()
        content_delivery.import_and_publish(make_archive(pack_id, "1.0.0", "Mixed 1.0.0", doc1))
        pub = content_delivery.import_and_publish(
            make_archive(pack_id, "1.0.1", "Mixed 1.0.1", doc2))
        family = pub["archive"]["entitlement"]["family"]
        content_delivery.retire_pack(pack_id, version="1.0.0")

        headers = self._story_headers()
        receipts = self.client.get("/v1/content/receipts", headers=headers).json()
        self._assert_story_entitlement_rule(receipts)
        entitlement = next(e for e in receipts["entitlements"] if e["entitlement_family"] == family)
        self.assertFalse(entitlement["retired"], "the live version owns the family")
        self.assertEqual(entitlement["current_release"]["version"], "1.0.1")

        history = self.client.get(
            f"/v1/content/entitlements/{family}/releases", headers=headers).json()
        self._assert_history_matches_entitlement(entitlement, history)
        by_version = {row["version"]: row for row in history["releases"]}
        self.assertFalse(by_version["1.0.0"]["archive_available"], "the retired version is refused")
        self.assertTrue(by_version["1.0.1"]["archive_available"])


if __name__ == "__main__":
    unittest.main()
