"""Isolated private Content Pack authority and delivery checks."""
from __future__ import annotations

import os
from pathlib import Path
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory(prefix="gateway-content-delivery-")
SUPPORT = Path(TEMP.name)
STORY_SOURCE = Path("/build/Developer/story-studio-kh")
STORY_FIXTURE = SUPPORT / "story-fixture"
STORY_REGISTRY = STORY_FIXTURE / "content-packs" / "authoring-registry.json"
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
    "GATEWAY_API_KEY": "content-delivery-fixture-key",
    "GATEWAY_STORY_SOURCE": str(STORY_SOURCE),
    "GATEWAY_STORY_REGISTRY": str(STORY_REGISTRY),
})

from fastapi.testclient import TestClient
from gateway.api import app
from gateway.activity import story_activity
from gateway import content_delivery, content_packs
from _isolation import isolate_content


# A 1x1 PNG: the smallest thing the lettering schema's preview asset can be.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
#: A minimal WebP-shaped payload. The route states the media type; the bytes only
#: have to be distinguishable from another cover.
_WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"cover-fixture-bytes"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _record(slug: str, version: str = "1.0.0", *, dependencies=None) -> dict:
    return {
        "pack_id": f"com.storystudio.pack.topic.{slug}",
        "item_id": f"com.storystudio.topic.{slug}",
        "version": version,
        "publisher_id": "com.storystudio",
        "min_app_version": "{{MIN_APP_VERSION}}",
        "min_app_version_status": "release-bound",
        "kind": "topic-pack",
        "source_path": f"content-packs/reserved/{slug}-{version}",
        "status": "reserved",
        "dependencies": dependencies or [],
        "stocking_proposal": {
            "family": {"id": f"com.storystudio.family.topic.{slug}", "status": "proposed"},
            "sku": {"id": f"com.storystudio.sku.topic.{slug}", "status": "proposed"},
        },
    }


def _topic_document(record: dict, title: str) -> dict:
    return {
        "kind": "topic-pack",
        "schema_version": 1,
        "id": record["item_id"],
        "name": title,
        "description": f"{title} description.",
        "compatible_formula_ids": ["com.storystudio.formula.fixture"],
        "subjects": ["Ledger"],
        "episode_objects": ["Record"],
        "time_place_scopes": ["Modern"],
        "mechanisms": ["Choice"],
        "evidence_lenses": ["Documents"],
        "exclusions": [],
        "max_siblings_per_subject": 4,
        "prompt_context": "Use evidence.",
    }


class ContentDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        isolate_content(self, SUPPORT / "data", "content-delivery-fixture-key")
        for path in (
            SUPPORT / "data" / "content-delivery",
            SUPPORT / "data" / "content-library",
            SUPPORT / "build-inputs",
            SUPPORT / "build-outputs",
            STORY_FIXTURE,
        ):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        STORY_REGISTRY.parent.mkdir(parents=True)
        STORY_REGISTRY.write_text(json.dumps({
            "revision": 1,
            "batches": [],
            "transitions": [],
            "records": [],
        }))
        self.client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 41001))

    def tearDown(self) -> None:
        self.client.close()

    def _setup(self) -> dict:
        response = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _install_registry_records(self, records: list[dict]) -> str:
        request = {
            "expected_revision": 1,
            "coordinator": {"task": "gateway-test", "branch": "codex/content-pack-delivery"},
            "proposal_ref": "isolated-test-fixture",
            "records": records,
        }
        receipt = "authoring:" + hashlib.sha256(_canonical(request).encode()).hexdigest()
        STORY_REGISTRY.write_text(json.dumps({
            "revision": 2,
            "batches": [{"revision": 2, "receipt_ref": receipt, "request": request}],
            "transitions": [],
            "records": [{"receipt_ref": receipt, **record} for record in records],
        }))
        return receipt

    def _multi_item_record(self, slug: str, items: list[str]) -> dict:
        return {
            "pack_id": f"com.storystudio.pack.lettering.{slug}",
            "item_ids": items,
            "version": "1.0.0",
            "publisher_id": "com.storystudio",
            "min_app_version": "1.4.216",
            "min_app_version_status": "fixed",
            "kind": "lettering-style",
            "source_path": f"content-packs/reserved/lettering-{slug}-1.0.0",
            "status": "reserved",
            "dependencies": [],
            "stocking_proposal": {
                "family": {"id": f"com.storystudio.family.lettering.{slug}", "status": "proposed"},
                "sku": {"id": f"com.storystudio.sku.lettering.{slug}", "status": "proposed"},
            },
        }

    def _build_multi_archive(self, record: dict, items: list[tuple[str, str]]) -> bytes:
        """A pack holding several items, the shape the big lettering packs have."""
        key = hashlib.sha256(f"{record['pack_id']}:{len(items)}".encode()).hexdigest()[:12]
        source = SUPPORT / "build-inputs" / f"multi-{key}"
        output = SUPPORT / "build-outputs" / f"multi-{key}.sspack"
        source.mkdir(parents=True, exist_ok=True)
        (source / "content").mkdir()
        (source / "pack.json").write_text(json.dumps({
            "pack_format": 1, "pack_id": record["pack_id"], "publisher_id": record["publisher_id"],
            "version": record["version"], "min_app_version": record["min_app_version"],
            "lifecycle": "active", "changelog": "Two styles in one pack.",
            "entitlement": {"sku": record["stocking_proposal"]["sku"]["id"],
                            "family": record["stocking_proposal"]["family"]["id"]},
            "dependencies": record["dependencies"],
        }))
        # The lettering schema requires at least one preview asset, and the archive
        # layer checks the declared file is really there.
        (source / "assets").mkdir()
        for slug, name in items:
            asset_name = f"{slug}.png"
            (source / "assets" / asset_name).write_bytes(_TINY_PNG)
            (source / "content" / f"{slug}.json").write_text(json.dumps({
                "kind": "lettering-style", "schema_version": 1,
                "id": f"com.storystudio.lettering.{slug}", "axis": "font",
                "name": name, "description": f"{name} description.",
                "phrase": f"{name} phrase.",
                "preview_assets": [f"assets/{asset_name}"],
            }))
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run([
            shutil.which("node") or "node",
            str(STORY_SOURCE / "scripts/content-packs/build-pack.mjs"), str(source),
            "--output", str(output), "--delivery-profile", "genstudio-store-receipt-v1",
        ], cwd=STORY_SOURCE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout.decode() + completed.stderr.decode())
        return output.read_bytes()

    def _build_archive(self, record: dict, title: str) -> bytes:
        key = hashlib.sha256(f"{record['pack_id']}:{record['version']}:{title}".encode()).hexdigest()[:12]
        source = SUPPORT / "build-inputs" / key
        output = SUPPORT / "build-outputs" / f"{key}.sspack"
        source.mkdir(parents=True, exist_ok=True)
        (source / "content").mkdir()
        (source / "pack.json").write_text(json.dumps({
            "pack_format": 1,
            "pack_id": record["pack_id"],
            "publisher_id": record["publisher_id"],
            "version": record["version"],
            "min_app_version": "{{MIN_APP_VERSION}}",
            "lifecycle": "active",
            "entitlement": {
                "sku": record["stocking_proposal"]["sku"]["id"],
                "family": record["stocking_proposal"]["family"]["id"],
            },
            "dependencies": record["dependencies"],
        }))
        (source / "content" / "topic-pack.json").write_text(json.dumps(_topic_document(record, title)))
        output.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run([
            shutil.which("node") or "node",
            str(STORY_SOURCE / "scripts/content-packs/build-pack.mjs"),
            str(source),
            "--output", str(output),
            "--min-app-version", "1.4.213",
            "--delivery-profile", "genstudio-store-receipt-v1",
        ], cwd=STORY_SOURCE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout.decode() + completed.stderr.decode())
        return output.read_bytes()

    def _preserve(self, data: bytes) -> str:
        response = self.client.post("/admin/api/content-library/import", content=data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["archive_sha256"]

    def _publish(self, data: bytes):
        # One pack in, one product available: importing is the only publisher now,
        # and every identity fact comes from the archive's own manifest.
        return self.client.post("/admin/api/content-library/import-and-publish", content=data)

    def _story_headers(self, key: str = "content-delivery-fixture-key") -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def _verify_signature(self, public_key_base64: str, payload: dict, signature: str) -> None:
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
            input=json.dumps({"public_key_base64": public_key_base64, "payload": payload, "signature": signature}).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr.decode())

    def test_unconfigured_shelf_read_does_not_create_authority(self) -> None:
        response = self.client.get("/admin/api/content-shelf")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])
        self.assertEqual(set(response.json()), {
            "configured", "available", "detail", "authority", "supported_kinds", "releases",
        })
        self.assertFalse((SUPPORT / "data" / "content-delivery").exists())
        for path in ("/v1/content/quotes", "/v1/content/purchases"):
            unavailable = self.client.post(path, headers=self._story_headers(), json={})
            self.assertEqual(unavailable.status_code, 503)
            self.assertEqual(unavailable.json()["code"], "CONTENT_STORE_NOT_CONNECTED")

    def test_status_reports_missing_or_timed_out_node_as_safe_unavailability(self) -> None:
        self._setup()
        for failure in (
            content_delivery.node_tool.NodeToolError("missing executable /private/path", 503),
            content_delivery.node_tool.NodeToolError("helper timed out with secret args", 503),
        ):
            with self.subTest(str(failure)), patch.object(
                    content_delivery.node_tool, "run_process", side_effect=failure):
                response = self.client.get("/admin/api/content-shelf")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertFalse(body["configured"])
                self.assertFalse(body["available"])
                self.assertNotIn("/private/path", body["detail"])
                self.assertNotIn("secret", body["detail"])

    def test_explicit_setup_is_private_idempotent_and_never_regenerates_damage(self) -> None:
        refused = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": False})
        self.assertEqual(refused.status_code, 422)
        response = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": True})
        self.assertEqual(response.status_code, 200)
        first = response.json()
        self.assertEqual(set(first), {
            "configured", "available", "detail", "authority", "supported_kinds", "releases",
        })
        self.assertTrue(first["configured"])
        self.assertTrue(first["available"])
        self.assertEqual(first["supported_kinds"], [
            "series-formula", "topic-pack", "visual-style", "thumbnail-recipe",
            # Accepted by Story 1.4.216; withheld from older clients in `products()`.
            "lettering-style", "metadata-formula",
        ])
        self.assertEqual(set(first["authority"]), {
            "account_id", "key_id", "public_key_fingerprint", "created_at", "baseline_revision",
        })
        shipped = content_delivery._shipped_baseline()
        self.assertIsNotNone(shipped, "the shipped historical identity list must be present")
        self.assertEqual(first["authority"]["baseline_revision"], shipped["registry_revision"])
        serialized = json.dumps(first).lower()
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("public_key_base64", serialized)
        self.assertNotIn("-----begin", serialized)

        root = SUPPORT / "data" / "content-delivery"
        private_key = root / "signing-key.pem"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(private_key.stat().st_mode), 0o600)
        replay = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": True})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["authority"], first["authority"])

        private_key.unlink()
        damaged = self.client.get("/admin/api/content-shelf").json()
        self.assertFalse(damaged["configured"])
        retry = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": True})
        self.assertNotEqual(retry.status_code, 200)
        self.assertFalse(private_key.exists())

    def test_complete_identity_loss_never_regenerates_initialized_authority(self) -> None:
        first = self._setup()["authority"]
        root = SUPPORT / "data" / "content-delivery"
        (root / "authority.json").unlink()
        (root / "signing-key.pem").unlink()
        damaged = self.client.get("/admin/api/content-shelf").json()
        self.assertFalse(damaged["configured"])
        retry = self.client.post("/admin/api/content-shelf/setup", json={"acknowledge": True})
        self.assertNotEqual(retry.status_code, 200)
        self.assertFalse((root / "authority.json").exists())
        self.assertFalse((root / "signing-key.pem").exists())
        self.assertNotEqual(damaged.get("authority"), first)

    def test_private_safe_reads_refuse_fifos_and_symlinks_without_blocking(self) -> None:
        self._setup()
        root = SUPPORT / "data" / "content-delivery"
        state_path = root / "authority.json"
        state_bytes = state_path.read_bytes()
        state_path.unlink()
        os.mkfifo(state_path, 0o600)
        started = time.monotonic()
        damaged = self.client.get("/admin/api/content-shelf").json()
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertFalse(damaged["configured"])

        state_path.unlink()
        state_path.write_bytes(state_bytes)
        state_path.chmod(0o600)
        marker = root / "initialized"
        marker_bytes = marker.read_bytes()
        marker.unlink()
        target = SUPPORT / "marker-target"
        target.write_bytes(marker_bytes)
        target.chmod(0o600)
        marker.symlink_to(target)
        started = time.monotonic()
        self.assertFalse(self.client.get("/admin/api/content-shelf").json()["configured"])
        self.assertLess(time.monotonic() - started, 5.0)

    def test_node_safe_reads_refuse_fifo_key_and_archive_without_waiting_for_timeout(self) -> None:
        self._setup()
        key = SUPPORT / "data" / "content-delivery" / "signing-key.pem"
        key.unlink()
        os.mkfifo(key, 0o600)
        started = time.monotonic()
        self.assertFalse(self.client.get("/admin/api/content-shelf").json()["configured"])
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_content_request_reads_the_authority_once(self) -> None:
        """The dependency hands the handler the state it already read.

        `_configured_state()` re-reads the authority files *and* spawns Node to
        re-inspect the signing identity (~100 ms). Reading it in the dependency and
        again in the handler measured ~204 ms per configured request, so the
        dependency returns the state and the handler passes it down.
        """
        self._setup()
        record = _record("private-ledger")
        self._install_registry_records([record])
        archive = self._build_archive(record, "Private Ledger")
        self._publish(archive)
        original = content_delivery._configured_state
        reads: list[int] = []

        def counting():
            reads.append(1)
            return original()

        with patch.object(content_delivery, "_configured_state", side_effect=counting):
            response = self.client.get("/v1/content/products", params={"story_version": "1.4.216"},
                                       headers=self._story_headers())
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(len(reads), 1, f"one request read the content authority {len(reads)} times")

    def test_availability_verifies_once_and_rechecks_a_changed_archive(self) -> None:
        """Availability keeps the SHA-256 guarantee without re-hashing every request.

        It read and hashed every archive in the library on each catalogue request
        (~155 ms for 140 packs). The verified answer is cached against the archive
        file's own stat instead, so a changed file is re-verified rather than
        trusted.
        """
        self._setup()
        record = _record("private-ledger")
        self._install_registry_records([record])
        archive = self._build_archive(record, "Private Ledger")
        self._publish(archive)
        state = content_delivery._configured_state()
        release = next(row for row in state["releases"] if row["pack_id"] == record["pack_id"])

        original = content_packs.read_archive
        hashed: list[int] = []

        def counting(digest, **kwargs):
            hashed.append(1)
            return original(digest, **kwargs)

        content_delivery._availability.clear()  # publishing already warmed this entry
        with patch.object(content_packs, "read_archive", side_effect=counting):
            for _ in range(3):
                self.assertTrue(content_delivery._available(release))
        self.assertEqual(len(hashed), 1, f"re-hashed an unchanged archive {len(hashed)} times")

        # Same size, different bytes: the cache must not answer from the stamp alone.
        path = content_packs.LIBRARY / release["archive_sha256"] / "archive.sspack"
        untouched = path.read_bytes()
        corrupted = bytearray(untouched)
        corrupted[0] ^= 0xFF
        path.write_bytes(bytes(corrupted))
        self.assertFalse(content_delivery._available(release), "a corrupted archive must read as unavailable")
        path.write_bytes(untouched)

    def test_a_client_too_old_for_a_product_does_not_receive_it(self) -> None:
        """A pack the client is too old to parse is withheld, not listed.

        A kind an older Story does not know throws inside its catalogue parser and
        takes the whole Content Store down with it, so publishing a new kind is only
        safe if old clients never receive the row. A malformed `story_version` must
        not silently empty the catalogue, so that case keeps seeing everything.
        """
        self._setup()
        record = _record("private-ledger")
        self._install_registry_records([record])
        self._publish(self._build_archive(record, "Private Ledger"))

        state_path = content_delivery._state_path()
        original = json.loads(state_path.read_text())

        def rewrite(minimum: str) -> None:
            state = json.loads(state_path.read_text())
            for row in state["releases"]:
                row["min_story_version"] = minimum
            replacement = state_path.with_suffix(".tmp")
            replacement.write_text(json.dumps(state))
            os.chmod(replacement, 0o600)  # the authority state must stay private
            os.replace(replacement, state_path)

        def listed(story_version: str) -> bool:
            response = self.client.get("/v1/content/products", params={"story_version": story_version},
                                       headers=self._story_headers())
            self.assertEqual(response.status_code, 200, response.text[:200])
            return any("private-ledger" in product["product_id"] for product in response.json()["products"])

        try:
            rewrite("99.0.0")
            self.assertFalse(listed("1.4.216"), "a product the client cannot install must not be sent")
            self.assertTrue(listed("not-a-version"), "a malformed version must not empty the catalogue")
        finally:
            replacement = state_path.with_suffix(".tmp")
            replacement.write_text(json.dumps(original))
            os.chmod(replacement, 0o600)
            os.replace(replacement, state_path)

    def test_a_cover_beside_the_archive_is_published_and_served(self) -> None:
        """A cover is product-level data: a file beside the archive, no new release.

        The catalogue publishes the Gateway-relative path Story's parser accepts, and
        Story's cover proxy reaches it with the normal Gateway key. The bytes are
        served with their stated media type and a long cache; nothing here touches
        the release, its version or its archive hash.
        """
        self._setup()
        record = _record("cover-art")
        self._install_registry_records([record])
        self._publish(self._build_archive(record, "Covered Pack"))
        state = content_delivery._configured_state()
        release = next(row for row in state["releases"] if row["pack_id"] == record["pack_id"])
        archive_bytes = release["archive_sha256"]
        (content_packs.LIBRARY / archive_bytes / "cover.webp").write_bytes(_WEBP_BYTES)

        headers = self._story_headers()
        catalog = self.client.get("/v1/content/products", params={"story_version": "1.4.216"},
                                  headers=headers).json()
        product = next(p for p in catalog["products"] if p["product_id"] == release["product_id"])
        self.assertEqual(product["cover"], {
            "url": f"/admin/api/content-library/covers/{record['pack_id']}/1.0.0",
            "alt": "Covered Pack",
        })
        self.assertIsNone(product["hero"])
        # The product still describes the same release: a cover adds no version.
        self.assertEqual(product["current_release"]["release_id"], release["release_id"])
        self.assertEqual(product["current_release"]["archive_sha256"], archive_bytes)

        served = self.client.get(
            f"/admin/api/content-library/covers/{record['pack_id']}/1.0.0", headers=headers)
        self.assertEqual(served.status_code, 200, served.text[:200])
        self.assertEqual(served.headers["content-type"], "image/webp")
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        self.assertIn("max-age=86400", served.headers["cache-control"])
        self.assertEqual(served.content, _WEBP_BYTES)

    def test_a_product_without_a_cover_is_null_and_the_route_is_404(self) -> None:
        self._setup()
        record = _record("no-cover")
        self._install_registry_records([record])
        self._publish(self._build_archive(record, "Plain Pack"))
        headers = self._story_headers()
        catalog = self.client.get("/v1/content/products", params={"story_version": "1.4.216"},
                                  headers=headers).json()
        product = next(p for p in catalog["products"] if "no-cover" in p["product_id"])
        self.assertIsNone(product["cover"], "no cover file means null, never a stand-in image")
        response = self.client.get(
            f"/admin/api/content-library/covers/{record['pack_id']}/1.0.0", headers=headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CONTENT_COVER_NOT_FOUND")

    def test_an_unknown_or_malformed_cover_identity_is_refused(self) -> None:
        self._setup()
        record = _record("cover-ids")
        self._install_registry_records([record])
        self._publish(self._build_archive(record, "Identity Pack"))
        headers = self._story_headers()

        # A well-formed identity the authority does not hold.
        missing = self.client.get(
            "/admin/api/content-library/covers/com.storystudio.pack.topic.nobody/1.0.0",
            headers=headers)
        self.assertEqual(missing.status_code, 404)

        # Path-shaped ids never reach the stored release lookup.
        for url in (
            "/admin/api/content-library/covers/_bad/1.0.0",
            "/admin/api/content-library/covers/pack/bad!",
        ):
            response = self.client.get(url, headers=headers)
            self.assertEqual(response.status_code, 400, url)
            self.assertEqual(response.json()["code"], "CONTENT_COVER_INVALID", url)

        # Traversal is refused either by this route's shape check or by the
        # framework's own path normalisation; neither ever reaches a file.
        for url in (
            "/admin/api/content-library/covers/..%2F..%2Fetc/1.0.0",
            "/admin/api/content-library/covers/pack/..%2F1.0.0",
            "/admin/api/content-library/covers/%2e%2e/1.0.0",
            "/admin/api/content-library/covers/pack/1.0.0%00",
        ):
            response = self.client.get(url, headers=headers)
            self.assertIn(response.status_code, (400, 404), url)
            # A refusal is never a cover.
            self.assertNotEqual(response.content, _WEBP_BYTES, url)

    def test_the_cover_route_needs_storys_gateway_key_beyond_loopback(self) -> None:
        """Story sends the gateway key, not an admin session; a stranger gets 401."""
        self._setup()
        record = _record("cover-auth")
        self._install_registry_records([record])
        self._publish(self._build_archive(record, "Keyed Pack"))
        release = next(row for row in content_delivery._configured_state()["releases"]
                       if row["pack_id"] == record["pack_id"])
        (content_packs.LIBRARY / release["archive_sha256"] / "cover.webp").write_bytes(_WEBP_BYTES)
        url = f"/admin/api/content-library/covers/{record['pack_id']}/1.0.0"

        remote = TestClient(app, base_url="http://100.64.0.9:8787",
                            client=("100.64.0.9", 50123))
        try:
            self.assertEqual(remote.get(url).status_code, 401)
            self.assertEqual(remote.get(url, headers={"Authorization": "Bearer wrong"}).status_code, 403)
            self.assertEqual(remote.get(url, headers=self._story_headers()).status_code, 200)
        finally:
            remote.close()

    def test_a_multi_item_pack_publishes_and_keeps_every_item(self) -> None:
        """A pack may hold several items, and none of them may be borrowed.

        The lettering and metadata packs hold six styles each, so the shelf's
        "exactly one item" rule was lifted: every item must still be receipted and
        the whole list is stored.

        A second pack claiming one of those items is refused by the shelf's own
        overlap check — with the registry cross-check gone, that check is the guard.
        """
        self._setup()
        items = ["com.storystudio.lettering.alpha", "com.storystudio.lettering.beta"]
        record = self._multi_item_record("duo", items)
        self._install_registry_records([record])
        archive = self._build_multi_archive(record, [("alpha", "Alpha"), ("beta", "Beta")])

        response = self._publish(archive)
        self.assertEqual(response.status_code, 200, response.text[:300])
        stored = next(row for row in content_delivery._configured_state()["releases"]
                      if row["pack_id"] == record["pack_id"])
        self.assertEqual(stored["item_ids"], items)

        # Re-publishing the same archive replays rather than duplicating the release.
        again = self._publish(archive)
        self.assertEqual(again.status_code, 200, again.text[:200])
        self.assertTrue(again.json()["replayed"])

        # A second pack that claims an already-published item is refused.
        thief = self._multi_item_record("thief", ["com.storystudio.lettering.alpha"])
        stolen = self._build_multi_archive(thief, [("alpha", "Alpha")])
        refused = self._publish(stolen)
        self.assertEqual(refused.status_code, 409, refused.text[:200])
        self.assertIn("identities must remain unique", refused.json()["detail"].lower())

    def test_real_story_validation_restore_grant_and_exact_download(self) -> None:
        setup = self._setup()
        record = _record("private-ledger")
        self._install_registry_records([record])
        archive = self._build_archive(record, "Private Ledger")

        response = self._publish(archive)
        self.assertEqual(response.status_code, 200, response.text)
        publication = response.json()
        self.assertEqual(set(publication), {"archive", "release", "replayed"})
        self.assertFalse(publication["replayed"])
        release = publication["release"]
        self.assertEqual(set(release), {
            "release_id", "pack_id", "version", "archive_sha256", "bytes", "title", "kind",
            "current", "available", "published_at",
        })
        self.assertEqual((release["pack_id"], release["version"], release["title"], release["kind"]),
                         (record["pack_id"], "1.0.0", "Private Ledger", "topic-pack"))
        self.assertTrue(release["current"])
        self.assertTrue(release["available"])
        replay = self._publish(archive)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["release"], release)

        shelf = self.client.get("/admin/api/content-shelf").json()
        self.assertEqual(shelf["releases"], [release])
        headers = self._story_headers()
        account = self.client.get("/v1/content/account", headers=headers)
        products = self.client.get("/v1/content/products", params={"story_version": "1.4.213"}, headers=headers)
        shelves = self.client.get("/v1/content/shelves", params={"story_version": "1.4.213"}, headers=headers)
        public_key = self.client.get("/v1/content/public-key", headers=headers)
        receipts = self.client.get("/v1/content/receipts", headers=headers)
        history = self.client.get(
            f"/v1/content/entitlements/{record['stocking_proposal']['family']['id']}/releases", headers=headers)
        for result in (account, products, shelves, public_key, receipts, history):
            self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(account.json()["owned"][0]["purchase_id"], None)
        product = products.json()["products"][0]
        self.assertTrue(product["owned"])
        self.assertEqual(product["credit_price"], 0)
        self.assertTrue(product["free"])
        self.assertIsNone(product["cover"])
        self.assertIsNone(product["hero"])
        self.assertEqual(shelves.json()["shelves"][0]["product_ids"], [product["product_id"]])
        keys = public_key.json()
        self.assertEqual(keys["algorithm"], "ed25519")
        self.assertEqual(len(keys["keys"]), 1)
        restored = receipts.json()
        self.assertEqual(restored["purchase_receipts"], [])
        self.assertIsNone(restored["entitlements"][0]["purchase_id"])
        self.assertEqual(restored["restore_receipt"]["receipt"]["releases"][0]["release_id"], release["release_id"])
        self._verify_signature(
            keys["public_key_base64"],
            restored["restore_receipt"]["receipt"],
            restored["restore_receipt"]["receipt_signature"],
        )
        self.assertEqual(history.json()["current_release_id"], release["release_id"])

        grant_response = self.client.post(
            "/v1/content/downloads",
            headers={**headers, "Host": "attacker.invalid"},
            json={"release_id": release["release_id"]},
        )
        self.assertEqual(grant_response.status_code, 200, grant_response.text)
        grant = grant_response.json()
        family_grant = self.client.post("/v1/content/downloads", headers=headers, json={
            "entitlement_family": record["stocking_proposal"]["family"]["id"],
            "version": record["version"],
        })
        self.assertEqual(family_grant.status_code, 200, family_grant.text)
        self.assertEqual(family_grant.json()["release_id"], release["release_id"])
        for invalid_body in (
            {"release_id": release["release_id"], "version": record["version"]},
            {"entitlement_family": record["stocking_proposal"]["family"]["id"],
             "version": record["version"], "extra": True},
        ):
            self.assertEqual(self.client.post("/v1/content/downloads", headers=headers, json=invalid_body).status_code, 422)
        self.assertEqual(grant["ttl_seconds"], 900)
        self.assertEqual(urlsplit(grant["download_url"]).netloc, "127.0.0.1:8000")
        self.assertNotIn("attacker.invalid", grant["download_url"])
        self._verify_signature(keys["public_key_base64"], grant["grant"], grant["grant_signature"])
        parsed_grant_url = urlsplit(grant["download_url"])
        token = parse_qs(parsed_grant_url.query)["grant"][0]
        encoded_payload = token.split(".", 1)[0]
        token_payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4)))
        self.assertEqual(token_payload["account_id"], setup["authority"]["account_id"])
        self.assertEqual(token_payload["key_id"], setup["authority"]["key_id"])
        download = self.client.get(grant["download_url"])
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.content, archive)
        self.assertTrue(all("?" not in row["path"] for row in story_activity()["recent"]))

        fixed = datetime.fromtimestamp(int(time.time()), timezone.utc)
        expired_payload = {
            **token_payload,
            "grant_id": "gateway.private.grant.exact-expiry-bound",
            "exp": int(fixed.timestamp()),
        }
        expired_token = content_delivery._grant_token(expired_payload, "content-delivery-fixture-key")
        expired_url = urlunsplit(parsed_grant_url._replace(query=urlencode({"grant": expired_token})))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed if tz else fixed.replace(tzinfo=None)

        with patch("gateway.content_delivery.datetime", FrozenDateTime):
            self.assertEqual(self.client.get(expired_url).status_code, 403)

        stale_payload = {**token_payload, "account_id": "gateway.private.account.stale"}
        stale_token = content_delivery._grant_token(stale_payload, "content-delivery-fixture-key")
        stale_url = urlunsplit(parsed_grant_url._replace(query=urlencode({"grant": stale_token})))
        self.assertEqual(self.client.get(stale_url).status_code, 403)
        tampered_token = token[:-1] + ("A" if token[-1] != "A" else "B")
        tampered_url = urlunsplit(parsed_grant_url._replace(query=urlencode({"grant": tampered_token})))
        self.assertEqual(self.client.get(tampered_url).status_code, 403)

        os.environ["GATEWAY_API_KEY"] = "rotated-content-delivery-key"
        try:
            self.assertEqual(self.client.get("/v1/content/account", headers=headers).status_code, 403)
            self.assertEqual(self.client.get(
                "/v1/content/account", headers=self._story_headers("rotated-content-delivery-key")).status_code, 200)
            self.assertEqual(self.client.get(grant["download_url"]).status_code, 403)
            os.environ["GATEWAY_API_KEY"] = ""
            self.assertEqual(self.client.get("/v1/content/account", headers={"Authorization": "Bearer placeholder"}).status_code, 503)
            self.assertEqual(self.client.get(grant["download_url"]).status_code, 403)
        finally:
            os.environ["GATEWAY_API_KEY"] = "content-delivery-fixture-key"

        stored = SUPPORT / "data" / "content-library" / release["archive_sha256"] / "archive.sspack"
        stored.write_bytes(b"changed")
        unavailable = self.client.get("/admin/api/content-shelf").json()["releases"][0]
        self.assertFalse(unavailable["available"])
        self.assertFalse(unavailable["current"])
        damaged_download = self.client.get(grant["download_url"])
        self.assertEqual(damaged_download.status_code, 410)
        self.assertEqual(damaged_download.json()["code"], "CONTENT_ARCHIVE_UNAVAILABLE")

    def test_dependency_closure_rejects_conflicting_versions_and_logical_pack_cycles(self) -> None:
        releases = [
            {"pack_id": "com.storystudio.pack.topic.base", "version": "1.0.0", "dependencies": []},
            {"pack_id": "com.storystudio.pack.topic.base", "version": "2.0.0", "dependencies": []},
            {"pack_id": "com.storystudio.pack.topic.left", "version": "1.0.0", "dependencies": [
                {"pack_id": "com.storystudio.pack.topic.base", "version": "1.0.0"},
            ]},
            {"pack_id": "com.storystudio.pack.topic.right", "version": "1.0.0", "dependencies": [
                {"pack_id": "com.storystudio.pack.topic.base", "version": "2.0.0"},
            ]},
        ]
        with self.assertRaisesRegex(content_delivery.DeliveryError, "conflicting versions"):
            content_delivery._validate_dependency_closure(releases, [
                {"pack_id": "com.storystudio.pack.topic.left", "version": "1.0.0"},
                {"pack_id": "com.storystudio.pack.topic.right", "version": "1.0.0"},
            ])

        cyclic = [
            {"pack_id": "com.storystudio.pack.topic.alpha", "version": "1.0.0", "dependencies": [
                {"pack_id": "com.storystudio.pack.topic.beta", "version": "1.0.0"},
            ]},
            {"pack_id": "com.storystudio.pack.topic.alpha", "version": "2.0.0", "dependencies": []},
            {"pack_id": "com.storystudio.pack.topic.beta", "version": "1.0.0", "dependencies": [
                {"pack_id": "com.storystudio.pack.topic.alpha", "version": "2.0.0"},
            ]},
        ]
        with self.assertRaisesRegex(content_delivery.DeliveryError, "logical pack cycle"):
            content_delivery._validate_dependency_closure(cyclic, [
                {"pack_id": "com.storystudio.pack.topic.alpha", "version": "1.0.0"},
            ])

    def test_historical_archive_is_never_eligible(self) -> None:
        shipped = content_delivery._shipped_baseline()
        self.assertIsNotNone(shipped, "the shipped historical identity list must be present")
        historical = _record("historical-archive")
        historical["pack_id"] = shipped["pack_ids"][0]
        self._setup()
        refused = self._publish(self._build_archive(historical, "Historical Archive"))
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["code"], "CONTENT_IDENTITY_HISTORICAL")
        self.assertEqual(self.client.get("/admin/api/content-shelf").json()["releases"], [])

    def test_version_conflict_numeric_current_and_failed_write_do_not_corrupt_shelf(self) -> None:
        self._setup()
        older = _record("numeric-order", "1.9.0")
        newer = _record("numeric-order", "1.10.0")
        failed = _record("failed-write")
        self._install_registry_records([older, newer, failed])

        first = self._publish(self._build_archive(older, "Numeric Older"))
        self.assertEqual(first.status_code, 200, first.text)
        conflict = self._publish(self._build_archive(older, "Different Exact Version Bytes"))
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["code"], "CONTENT_RELEASE_CONFLICT")

        latest = self._publish(self._build_archive(newer, "Numeric Newer"))
        self.assertEqual(latest.status_code, 200, latest.text)
        rows = self.client.get("/admin/api/content-shelf").json()["releases"]
        self.assertEqual([row["version"] for row in rows if row["current"]], ["1.10.0"])

        candidate = self._build_archive(failed, "Failed Durable Write")
        with patch.object(content_delivery, "_atomic_state", side_effect=content_delivery.DeliveryError(
                "Private Content Pack authority state could not be stored.", 503, "CONTENT_DELIVERY_UNAVAILABLE")):
            refused = self.client.post("/admin/api/content-library/import-and-publish", content=candidate)
        self.assertEqual(refused.status_code, 503)
        self.assertNotIn(failed["pack_id"], [
            row["pack_id"] for row in self.client.get("/admin/api/content-shelf").json()["releases"]
        ])

    def test_concurrent_identical_publication_is_one_release_plus_one_replay(self) -> None:
        self._setup()
        record = _record("concurrent")
        self._install_registry_records([record])
        digest = self._preserve(self._build_archive(record, "Concurrent Publication"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(content_delivery.publish_archive, [digest, digest]))
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(len(self.client.get("/admin/api/content-shelf").json()["releases"]), 1)

    def test_public_response_bound_accepts_exactly_one_mib_and_refuses_one_more_byte(self) -> None:
        empty = {"value": ""}
        overhead = len(json.dumps(empty, ensure_ascii=False, separators=(",", ":")).encode())
        exact = {"value": "x" * (content_delivery._PUBLIC_MAX - overhead)}
        self.assertEqual(
            len(json.dumps(exact, ensure_ascii=False, separators=(",", ":")).encode()),
            content_delivery._PUBLIC_MAX,
        )
        self.assertIs(content_delivery._bounded(exact), exact)
        with self.assertRaisesRegex(content_delivery.DeliveryError, "safe limit"):
            content_delivery._bounded({"value": exact["value"] + "x"})


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEMP.cleanup()
