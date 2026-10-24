#!/usr/bin/env python3
"""Focused contract for descriptor-relative private render asset metadata."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import render_assets  # noqa: E402
from gateway.render_assets import (  # noqa: E402
    AssetCapacityError,
    AssetConflictError,
    AssetNotFoundError,
    AssetRecoveryRequiredError,
    AssetValidationError,
    RenderAssetStore,
)

KiB = 1024
MiB = 1024 * KiB
MAX_I63 = (1 << 63) - 1
OWNER = "a" * 64
SHA = "b" * 64
UPLOAD = "c" * 32
ASSET = "d" * 32
OPERATION = "e" * 32
CANDIDATE = "f" * 32
ARCHIVE = "1" * 32


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def owner_token(scope: str) -> str:
    return digest(scope.encode("utf-8"))


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(kind: str) -> dict:
    init = {
        "schema": 1,
        "kind": "init",
        "owner_token": OWNER,
        "sha256": SHA,
        "size_bytes": 9,
        "upload_id": UPLOAD,
        "asset_handle": ASSET,
    }
    if kind == "format":
        return {"schema": 1, "kind": "format", "format": "render-asset-fd-v1", "store_id": UPLOAD}
    if kind == "init":
        return init
    if kind == "upload_head":
        return {
            **init,
            "kind": "upload_head",
            "generation": 0,
            "committed_offset": 0,
            "state": "incomplete",
            "pending": None,
            "recovery_reason": None,
        }
    if kind == "range":
        return {
            "schema": 1,
            "kind": "range",
            "owner_token": OWNER,
            "upload_id": UPLOAD,
            "operation_id": OPERATION,
            "candidate_id": CANDIDATE,
            "offset": 0,
            "length": 9,
            "sha256": SHA,
        }
    if kind == "asset":
        return {
            "schema": 1,
            "kind": "asset",
            "owner_token": OWNER,
            "upload_id": UPLOAD,
            "asset_handle": ASSET,
            "sha256": SHA,
            "size_bytes": 9,
            "operation_id": OPERATION,
        }
    if kind == "protection":
        return {
            "schema": 1,
            "kind": "protection",
            "owner_token": OWNER,
            "upload_id": UPLOAD,
            "asset_handle": ASSET,
            "protection_kind": "reference",
            "reference": "attempt-1",
            "generation": 0,
            "state": "active",
        }
    if kind == "record_change":
        return {
            "schema": 1,
            "kind": "record_change",
            "operation_id": OPERATION,
            "record_kind": "upload_head",
            "target_name": "head.json",
            "parent_device": 1,
            "parent_inode": 2,
            "old_generation": None,
            "old_sha256": None,
            "old_device": None,
            "old_inode": None,
            "new_generation": 0,
            "new_sha256": SHA,
            "candidate_id": CANDIDATE,
            "candidate_device": 3,
            "candidate_inode": 4,
            "archive_id": ARCHIVE,
            "archive_device": 5,
            "archive_inode": 6,
        }
    raise AssertionError(kind)


class FreeSpace:
    def __init__(self, value: int) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        return self.value


class BoundedStream(io.BytesIO):
    def __init__(self, data: bytes, largest_read: int) -> None:
        super().__init__(data)
        self.largest_read = largest_read
        self.requests: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requests.append(size)
        if size < 0 or size > self.largest_read:
            raise AssertionError(f"unbounded source read: {size}")
        return super().read(size)


class SimulatedCrash(BaseException):
    pass


class CrashingIterator:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        yield self.data
        raise SimulatedCrash("unknown interruption")


class OnePieceWithoutDrain:
    def __init__(self, piece) -> None:
        self.piece = piece
        self.calls = 0

    def read(self, size: int = -1):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("overflow source was drained")
        return self.piece


class OneItemWithoutDrain:
    def __init__(self, piece) -> None:
        self.piece = piece
        self.calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("overflow iterator was drained")
        return self.piece


class PausingStream(io.BytesIO):
    def __init__(self, data: bytes, entered, release) -> None:
        super().__init__(data)
        self.entered = entered
        self.release = release
        self.paused = False

    def read(self, size: int = -1) -> bytes:
        if not self.paused:
            self.paused = True
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("paused stream was not released")
        return super().read(size)


class TrackedReader:
    def __init__(self, reader, *, fail_close: bool = False) -> None:
        self.reader = reader
        self.closed = False
        self.fail_close = fail_close

    def read(self, size: int = -1):
        return self.reader.read(size)

    def seek(self, *args):
        return self.reader.seek(*args)

    def close(self) -> None:
        self.closed = True
        self.reader.close()
        if self.fail_close:
            raise OSError("injected reader close failure")


class FaultingReader:
    def __init__(self, reader, fail_on: str) -> None:
        self.reader = reader
        self.fail_on = fail_on
        self.failed = False
        self.closed = False

    def read(self, size: int = -1):
        if self.fail_on == "read" and not self.failed:
            self.failed = True
            raise OSError("injected wrapped-reader read failure")
        return self.reader.read(size)

    def seek(self, *args):
        if self.fail_on == "seek" and not self.failed:
            self.failed = True
            raise OSError("injected wrapped-reader seek failure")
        return self.reader.seek(*args)

    def close(self) -> None:
        self.closed = True
        self.reader.close()


def process_append(root: str, upload_id: str, data: bytes, started, done, output) -> None:
    started.set()
    store = None
    try:
        store = RenderAssetStore(
            root,
            max_chunk_bytes=64 * KiB,
            max_asset_bytes=16 * MiB,
            reserve_bytes=2 * MiB,
            measure_free_bytes=lambda: 512 * MiB,
        )
        result = store.append_chunk("owner", upload_id, 0, io.BytesIO(data), len(data))
        output.put(("ok", result))
    except BaseException as exc:
        output.put(("error", type(exc).__name__, str(exc)))
    finally:
        if store is not None:
            store.close()
        done.set()


class RenderAssetStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._trash_bucket = ROOT / "_trash" / f"render-assets-fd-tests-{uuid4().hex}"
        self._trash_bucket.mkdir(parents=True, exist_ok=False)
        self.sandbox = Path(tempfile.mkdtemp(prefix="active-", dir=self._trash_bucket))
        self.root = self.sandbox / "store"
        self.free = FreeSpace(512 * MiB)
        self._stores: list[RenderAssetStore] = []
        self.store = self.open_store()

    def tearDown(self) -> None:
        errors = []
        for store in reversed(self._stores):
            try:
                store.close()
            except BaseException as exc:
                errors.append(exc)
        if self.sandbox.exists() or self.sandbox.is_symlink():
            self.sandbox.rename(self._trash_bucket / "evidence")
        if errors:
            raise errors[0]

    def open_store(self, root: Path | None = None, **overrides) -> RenderAssetStore:
        options = {
            "max_chunk_bytes": 64 * KiB,
            "max_asset_bytes": 16 * MiB,
            "reserve_bytes": 2 * MiB,
            "measure_free_bytes": self.free,
        }
        options.update(overrides)
        store = RenderAssetStore(root or self.root, **options)
        self._stores.append(store)
        return store

    def restart(self) -> None:
        self.store.close()
        self.store = self.open_store()

    def upload_with(self, store: RenderAssetStore, owner: str, data: bytes, *, step: int = 31 * KiB) -> dict:
        begun = store.begin_upload(owner, digest(data), len(data))
        offset = begun["committed_offset"]
        while offset < len(data):
            part = data[offset : offset + step]
            offset = store.append_chunk(owner, begun["upload_id"], offset, io.BytesIO(part), len(part))
        return store.finish_upload(owner, begun["upload_id"])

    def upload(self, owner: str, data: bytes, *, step: int = 31 * KiB) -> dict:
        return self.upload_with(self.store, owner, data, step=step)

    def init_record(self, owner: str, sha256: str) -> dict:
        path = self.root / "index" / f"{owner_token(owner)}-{sha256}.json"
        return read_json(path)

    def upload_dir(self, upload_id: str) -> Path:
        return self.root / "uploads" / upload_id

    def chunk_blobs(self, upload_id: str) -> list[Path]:
        return list((self.upload_dir(upload_id) / "chunks").glob("*.bin"))

    def assembly_blobs(self, upload_id: str) -> list[Path]:
        return list((self.upload_dir(upload_id) / "assembly").glob("*.bin"))

    def published_blob(self, asset_handle: str) -> Path:
        return self.root / "published" / f"{asset_handle}.blob"

    def test_closed_record_codec_is_canonical_and_round_trips_every_kind(self) -> None:
        for kind in ("format", "init", "upload_head", "range", "asset", "protection", "record_change"):
            with self.subTest(kind=kind):
                record = fixture(kind)
                encoded = render_assets._encode_record(record)
                self.assertEqual(encoded, canonical(record))
                self.assertLessEqual(len(encoded), 8192)
                self.assertEqual(render_assets._decode_record(encoded, kind), record)

    def test_private_blob_status_is_read_only_and_async_bridge_keeps_exact_frontier(self) -> None:
        owner = "private-render-owner"
        data = b"bounded-private-render-stream" * 2048
        sha256 = digest(data)
        index = self.root / "index" / f"{owner_token(owner)}-{sha256}.json"

        self.assertEqual(self.store.blob_status(owner, sha256, len(data)), {
            "state": "missing", "offset": 0,
        })
        self.assertFalse(index.exists())
        begun = self.store.begin_upload(owner, sha256, len(data))

        async def pieces():
            for offset in range(0, len(data), 8192):
                yield data[offset:offset + 8192]

        offset = asyncio.run(self.store.append_async_chunk(
            owner, begun["upload_id"], 0, pieces(), len(data),
        ))
        self.assertEqual(offset, len(data))
        self.assertEqual(self.store.blob_status(owner, sha256, len(data))["offset"], len(data))
        published = self.store.finish_upload(owner, begun["upload_id"])
        self.assertEqual(self.store.blob_status(owner, sha256, len(data)), {
            "state": "complete", "offset": len(data),
            "upload_id": begun["upload_id"], "asset_handle": published["asset_handle"],
        })
        self.assertEqual(self.store.blob_status("another-owner", sha256, len(data)), {
            "state": "missing", "offset": 0,
        })

    def test_record_parser_rejects_ceiling_utf8_duplicates_unknowns_and_noncanonical_types(self) -> None:
        valid = fixture("format")
        invalid = (
            b"{" + b" " * 8192,
            b'{"schema":1,"kind":"format","format":"render-asset-fd-v1","store_id":"' + b"a" * 32 + b'","schema":1}\n',
            canonical({**valid, "extra": 1}),
            canonical({**valid, "schema": True}),
            canonical({**valid, "schema": 1.0}),
            b'{"format":"render-asset-fd-v1","kind":"format","schema":NaN,"store_id":"' + b"a" * 32 + b'"}\n',
            b"\xff\n",
            b'{"format":"render-asset-fd-v1","kind":"format","schema":1,"store_id":"\\ud800"}\n',
        )
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(AssetRecoveryRequiredError):
                    render_assets._decode_record(raw, "format")

    def test_record_parser_rejects_bounds_pending_shapes_and_inconsistent_change_fields(self) -> None:
        head = fixture("upload_head")
        pending_range = {
            "kind": "range",
            "operation_id": OPERATION,
            "candidate_id": CANDIDATE,
            "offset": 0,
            "length": 9,
            "phase": "prepared",
            "sha256": None,
        }
        accepted = {**head, "pending": pending_range}
        self.assertEqual(render_assets._decode_record(canonical(accepted), "upload_head"), accepted)
        bad_records = (
            {**fixture("range"), "offset": -1},
            {**fixture("range"), "length": 0},
            {**fixture("range"), "offset": MAX_I63, "length": 1},
            {**fixture("upload_head"), "generation": True},
            {**accepted, "pending": {**pending_range, "phase": "verified", "sha256": None}},
            {**fixture("record_change"), "old_generation": None, "old_sha256": SHA},
            {**fixture("record_change"), "new_generation": 1},
        )
        for record in bad_records:
            with self.subTest(kind=record["kind"]):
                with self.assertRaises(AssetRecoveryRequiredError):
                    render_assets._decode_record(canonical(record), record["kind"])

    def test_fresh_layout_and_exact_init_head_schema(self) -> None:
        expected = {"format.json", "store.lock", "index", "uploads", "assets", "published", "_trash"}
        self.assertEqual({path.name for path in self.root.iterdir()}, expected)
        format_record = read_json(self.root / "format.json")
        self.assertEqual(format_record["format"], "render-asset-fd-v1")
        data = b"schema"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        init = self.init_record("owner", digest(data))
        head = read_json(self.upload_dir(begun["upload_id"]) / "head.json")
        self.assertEqual(set(init), {"schema", "kind", "owner_token", "sha256", "size_bytes", "upload_id", "asset_handle"})
        self.assertEqual(set(head), set(init) | {"generation", "committed_offset", "state", "pending", "recovery_reason"})
        self.assertEqual((init["owner_token"], init["upload_id"]), (owner_token("owner"), begun["upload_id"]))
        self.assertNotIn("owner", init.values())

    METADATA_NAMES = (".DS_Store", "._x", ".localized", "Icon\r", ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems")

    def litter(self, *directories: Path) -> None:
        for directory in directories:
            for name in self.METADATA_NAMES:
                (directory / name).write_bytes(b"finder")

    def litter_every_store_directory(self, upload_id: str | None = None) -> list[Path]:
        directories = [self.root] + [self.root / name for name in ("index", "uploads", "assets", "published", "_trash")]
        if upload_id is not None:
            upload = self.upload_dir(upload_id)
            directories += [upload] + [upload / name for name in ("ranges", "chunks", "assembly", "protections", "changes")]
        self.litter(*directories)
        return directories

    def test_os_metadata_names_are_one_shared_set(self) -> None:
        from gateway.os_metadata import is_os_metadata

        for name in self.METADATA_NAMES + ("._", "._anything.json"):
            self.assertTrue(is_os_metadata(name), name)
        for name in ("format.json", "unknown.txt", "DS_Store", ".DS_Store.json", "x._y", "Icon", ".hidden"):
            self.assertFalse(is_os_metadata(name), name)

    def test_finder_metadata_never_blocks_open_scans_or_sweeps_and_is_never_touched(self) -> None:
        data = b"s" * (70 * KiB)
        asset, upload_id = self._published_upload("owner", data)
        self.store.close()
        directories = self.litter_every_store_directory(upload_id)
        self.store = self.open_store()  # the root scan that crash-looped the Gateway on 2026-10-24
        self.assertEqual(self.store.describe("owner", asset["asset_handle"])["sha256"], digest(data))
        self.assertEqual(self.store.begin_upload("owner", digest(data), len(data))["upload_id"], upload_id)
        self.assertEqual(self.store.active_protection("owner"), [])
        swept = self.store.sweep_staging(grace_seconds=0)
        # Only the real chunk and assembly copies were moved and counted, never the Finder files.
        self.assertEqual((swept["uploads"], swept["files"], swept["bytes"]), (1, 4, 2 * len(data)))
        self.assertTrue(all((directory / name).exists() for directory in directories for name in self.METADATA_NAMES))
        retired = self.store.sweep_published({}, grace_seconds=0)
        self.assertEqual(retired["assets"], 1)
        # The Finder files in the directories that stay behind are still exactly where they were.
        for directory in (self.root, self.root / "index", self.root / "uploads", self.root / "assets", self.root / "published"):
            for name in self.METADATA_NAMES:
                self.assertEqual((directory / name).read_bytes(), b"finder", (directory, name))

    def test_finder_metadata_in_an_empty_root_still_initializes_a_fresh_store(self) -> None:
        root = self.sandbox / "fresh-store"
        root.mkdir()
        self.litter(root)
        store = self.open_store(root)
        self.assertEqual(read_json(root / "format.json")["format"], "render-asset-fd-v1")
        data = b"fresh"
        begun = store.begin_upload("owner", digest(data), len(data))
        self.assertEqual(begun["committed_offset"], 0)
        self.assertTrue((root / ".DS_Store").exists())

    def test_a_genuinely_unknown_entry_is_still_refused_even_beside_finder_metadata(self) -> None:
        data = b"s" * (70 * KiB)
        asset, upload_id = self._published_upload("owner", data)
        self.litter_every_store_directory(upload_id)
        upload = self.upload_dir(upload_id)
        begin = lambda: self.store.begin_upload("owner", digest(data), len(data))
        cases = (
            ("upload directory", upload / "unknown-file", begin, AssetRecoveryRequiredError, "render asset directory has an unexpected entry"),
            ("change evidence", upload / "changes" / "unknown-change", begin, AssetRecoveryRequiredError, "upload has unknown change evidence"),
            ("protection evidence", upload / "protections" / "unknown-protection", lambda: self.store.active_protection("owner"), AssetRecoveryRequiredError, "upload has unknown protection evidence"),
            ("upload directory name", self.root / "uploads" / "unknown-upload", lambda: self.store.active_protection("owner"), AssetRecoveryRequiredError, "unexpected upload directory"),
            ("asset evidence", self.root / "assets" / "unknown-asset", lambda: self.store.sweep_published({}, grace_seconds=0, protected={asset["asset_handle"]}), AssetRecoveryRequiredError, "store has unknown asset evidence"),
        )
        for index, (label, stray, action, error, message) in enumerate(cases):
            with self.subTest(label):
                stray.write_bytes(b"not metadata")
                with self.assertRaisesRegex(error, message):
                    action()
                self.assertEqual(stray.read_bytes(), b"not metadata")
                stray.rename(self._trash_bucket / f"stray-{index}")
        # A root with a real unknown entry is still "another format", Finder files or not.
        self.store.close()
        (self.root / "unknown.txt").write_bytes(b"not metadata")
        with self.assertRaisesRegex(AssetValidationError, "existing render asset root has another format"):
            self.open_store()
        (self.root / "unknown.txt").rename(self._trash_bucket / "stray-root")
        self.store = self.open_store()
        self.assertEqual(self.store.describe("owner", asset["asset_handle"])["sha256"], digest(data))

    def test_constructor_and_identity_fields_reject_bool_zero_and_untrusted_syntax(self) -> None:
        for overrides in (
            {"max_chunk_bytes": 0}, {"max_chunk_bytes": True}, {"max_asset_bytes": 0},
            {"reserve_bytes": -1}, {"reserve_bytes": False}, {"measure_free_bytes": 1},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(AssetValidationError):
                self.open_store(self.sandbox / f"bad-{uuid4().hex}", **overrides)
        for owner, sha256, size in (("", "0" * 64, 1), (None, "0" * 64, 1), ("o", "A" * 64, 1), ("o", "0" * 63, 1), ("o", "0" * 64, 0), ("o", "0" * 64, True)):
            with self.subTest(owner=owner, sha256=sha256, size=size), self.assertRaises(AssetValidationError):
                self.store.begin_upload(owner, sha256, size)

    def test_reference_bounds_control_whitespace_and_utf8_are_enforced(self) -> None:
        asset = self.upload("owner", b"reference bounds")
        invalid = ("", " leading", "trailing ", "line\nbreak", "x" * 513, "é" * 1025)
        for reference in invalid:
            with self.subTest(length=len(reference)), self.assertRaises(AssetValidationError):
                self.store.acquire_reference("owner", asset["asset_handle"], reference)
        valid = "😀" * 512
        self.assertEqual((len(valid), len(valid.encode("utf-8"))), (512, 2048))
        self.store.acquire_reference("owner", asset["asset_handle"], valid)
        self.assertEqual(self.store.active_protection("owner")[0]["reference"], valid)

    def test_begin_is_stable_scoped_and_changed_total_conflicts(self) -> None:
        data = b"same tuple"
        first = self.store.begin_upload("owner-a", digest(data), len(data))
        self.assertEqual(self.store.begin_upload("owner-a", digest(data), len(data)), first)
        with self.assertRaises(AssetConflictError):
            self.store.begin_upload("owner-a", digest(data), len(data) + 1)
        other = self.store.begin_upload("owner-b", digest(data), len(data))
        self.assertNotEqual(first["upload_id"], other["upload_id"])
        self.upload_with(self.store, "owner-a", data)
        with self.assertRaises(AssetConflictError):
            self.store.begin_upload("owner-a", digest(data), len(data) + 1)

    def test_multichunk_resume_exact_replay_and_conflicts(self) -> None:
        data = b"alpha" * 9000 + b"omega" * 3000
        begun = self.store.begin_upload("owner", digest(data), len(data))
        first = data[:40_000]
        self.assertEqual(self.store.append_chunk("owner", begun["upload_id"], 0, BoundedStream(first, 64 * KiB), len(first)), len(first))
        self.assertEqual(self.store.append_chunk("owner", begun["upload_id"], 0, iter((first[:99], first[99:])), len(first)), len(first))
        with self.assertRaises(AssetConflictError):
            self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(b"X" * len(first)), len(first))
        with self.assertRaises(AssetConflictError):
            self.store.append_chunk("owner", begun["upload_id"], len(first) + 1, io.BytesIO(b"x"), 1)
        tail = data[len(first):]
        self.store.append_chunk("owner", begun["upload_id"], len(first), io.BytesIO(tail), len(tail))
        self.assertEqual(self.store.finish_upload("owner", begun["upload_id"])["sha256"], digest(data))

    def _published_upload(self, owner: str, data: bytes) -> tuple[dict, str]:
        begun = self.store.begin_upload(owner, digest(data), len(data))
        offset = 0
        while offset < len(data):
            part = data[offset : offset + 31 * KiB]
            offset = self.store.append_chunk(owner, begun["upload_id"], offset, io.BytesIO(part), len(part))
        asset = self.store.finish_upload(owner, begun["upload_id"])
        return asset, begun["upload_id"]

    def test_sweep_moves_dead_chunk_and_assembly_copies_and_keeps_identity(self) -> None:
        data = b"s" * (70 * KiB)
        asset, upload_id = self._published_upload("owner", data)
        self.assertTrue(self.chunk_blobs(upload_id))
        self.assertTrue(self.assembly_blobs(upload_id))
        # The one-hour floor protects a just-published asset by default.
        self.assertEqual(self.store.sweep_staging()["uploads"], 0)
        self.assertTrue(self.chunk_blobs(upload_id))
        result = self.store.sweep_staging(grace_seconds=0)
        self.assertGreaterEqual(result["uploads"], 1)
        self.assertGreater(result["files"], 0)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(self.chunk_blobs(upload_id), [])
        self.assertEqual(self.assembly_blobs(upload_id), [])
        # The published blob, its descriptor and its index record are untouched.
        self.assertTrue(self.published_blob(asset["asset_handle"]).exists())
        self.assertEqual(self.store.describe("owner", asset["asset_handle"])["sha256"], digest(data))
        self.assertEqual(self.init_record("owner", digest(data))["upload_id"], upload_id)
        # A second sweep has nothing left to move and stays idempotent.
        self.assertEqual(self.store.sweep_staging(grace_seconds=0)["uploads"], 0)

    def test_sweep_skips_unpublished_graced_and_live_protected_uploads(self) -> None:
        # An unpublished (still incomplete) upload has no durable published copy yet.
        unfinished = b"u" * (40 * KiB)
        begun = self.store.begin_upload("owner", digest(unfinished), len(unfinished))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(unfinished[:31 * KiB]), 31 * KiB)
        self.assertEqual(self.store.sweep_staging(grace_seconds=0)["uploads"], 0)
        self.assertTrue(self.chunk_blobs(begun["upload_id"]))
        # A published upload with a live reference is skipped even past the grace floor.
        data = b"t" * (40 * KiB)
        asset, protected_id = self._published_upload("owner", data)
        self.store.acquire_reference("owner", asset["asset_handle"], "render-job:live")
        self.assertEqual(self.store.sweep_staging(grace_seconds=0)["uploads"], 0)
        self.assertTrue(self.chunk_blobs(protected_id))
        # A live reader also protects it.
        self.store.release_reference("owner", asset["asset_handle"], "render-job:live")
        with self.store.open_reader("owner", asset["asset_handle"]):
            self.assertEqual(self.store.sweep_staging(grace_seconds=0)["uploads"], 0)
            self.assertTrue(self.chunk_blobs(protected_id))
        # With no live protection the same upload is finally swept.
        self.assertGreaterEqual(self.store.sweep_staging(grace_seconds=0)["uploads"], 1)
        self.assertEqual(self.chunk_blobs(protected_id), [])

    def test_sweep_moves_a_complete_never_published_upload_only_past_its_grace(self) -> None:
        data = b"a" * (40 * KiB)
        begun = self.store.begin_upload("owner", digest(data), len(data))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        # Complete bytes but still inside the abandoned grace: in flight, untouched.
        self.assertEqual(
            self.store.sweep_staging(grace_seconds=0, abandoned_grace_seconds=10**9)["uploads"], 0
        )
        self.assertTrue(self.chunk_blobs(begun["upload_id"]))
        # Inventory exactly what the store wrote before the move: one range record plus
        # its publication candidate hardlink, and one chunk payload.
        upload = self.upload_dir(begun["upload_id"])
        before_ranges = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (upload / "ranges").iterdir()}
        before_chunks = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (upload / "chunks").iterdir()}
        self.assertEqual(len(before_ranges), 2)
        self.assertIn("0000000000000000.json", before_ranges)
        self.assertEqual(sum(name.startswith("candidate-") for name in before_ranges), 1)
        self.assertEqual(len(before_chunks), 1)
        self.assertEqual(list(before_chunks.values()), [digest(data)])
        # Sealed, unchecked, past the grace: the whole upload moves to _trash.
        result = self.store.sweep_staging(grace_seconds=0, abandoned_grace_seconds=0)
        self.assertEqual(result["uploads"], 1)
        self.assertGreater(result["files"], 0)
        self.assertFalse(self.upload_dir(begun["upload_id"]).exists())
        moved = list((self.root / "_trash").glob(f"{begun['upload_id']}-abandoned-*"))
        self.assertEqual(len(moved), 1)
        # The bucket holds the upload directory itself, contents intact.
        self.assertEqual(sorted(path.name for path in moved[0].iterdir()), [begun["upload_id"]])
        held = moved[0] / begun["upload_id"]
        head = read_json(held / "head.json")
        self.assertEqual(head["upload_id"], begun["upload_id"])
        self.assertEqual(head["state"], "incomplete")
        self.assertEqual(head["sha256"], digest(data))
        self.assertEqual(head["size_bytes"], len(data))
        self.assertEqual(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (held / "ranges").iterdir()},
            before_ranges,
        )
        self.assertEqual(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (held / "chunks").iterdir()},
            before_chunks,
        )
        # The publication candidate is still the same inode as its range record.
        self.assertEqual(len({path.stat().st_ino for path in (held / "ranges").iterdir()}), 1)
        # An upload missing bytes is never guessed dead from age alone.
        partial = self.store.begin_upload("owner", digest(b"pp"), 10)
        self.store.append_chunk("owner", partial["upload_id"], 0, io.BytesIO(b"pp"), 2)
        self.assertEqual(
            self.store.sweep_staging(grace_seconds=0, abandoned_grace_seconds=0)["uploads"], 0
        )
        self.assertTrue(self.chunk_blobs(partial["upload_id"]))

    def test_sweep_published_retires_only_terminal_grace_past_unprotected_assets(self) -> None:
        data = b"p" * (44 * KiB)
        asset, upload_id = self._published_upload("owner", data)
        handle = asset["asset_handle"]
        index_path = self.root / "index" / f"{owner_token('owner')}-{digest(data)}.json"
        asset_record = self.root / "assets" / f"{handle}.json"
        for path in (index_path, asset_record, self.published_blob(handle)):
            self.assertTrue(path.exists(), path)
        # A referenced handle with no terminal reference -> untouched.
        self.assertEqual(self.store.sweep_published({}, grace_seconds=0, protected={handle})["assets"], 0)
        self.assertTrue(self.published_blob(handle).exists())
        # A live reference protects it even when terminal and past the grace floor.
        self.store.acquire_reference("owner", handle, "render-job:live")
        self.assertEqual(self.store.sweep_published({handle: 0.0}, grace_seconds=0)["assets"], 0)
        self.assertTrue(self.published_blob(handle).exists())
        self.store.release_reference("owner", handle, "render-job:live")
        # A live reader protects it too.
        with self.store.open_reader("owner", handle):
            self.assertEqual(self.store.sweep_published({handle: 0.0}, grace_seconds=0)["assets"], 0)
            self.assertTrue(self.published_blob(handle).exists())
        # The default grace floor protects a just-settled asset.
        self.assertEqual(self.store.sweep_published({handle: time.time()})["assets"], 0)
        self.assertTrue(self.published_blob(handle).exists())
        # Terminal, grace-past, unprotected: index, asset record, blob and upload dir all
        # move into the store's own _trash by rename.
        result = self.store.sweep_published({handle: 0.0}, grace_seconds=0)
        self.assertEqual(result["assets"], 1)
        self.assertGreater(result["files"], 0)
        self.assertGreater(result["bytes"], 0)
        self.assertFalse(self.published_blob(handle).exists())
        self.assertFalse(asset_record.exists())
        self.assertFalse(index_path.exists())
        self.assertFalse(self.upload_dir(upload_id).exists())
        moved = list((self.root / "_trash").glob(f"retired-{handle}-*/published/{handle}.blob"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].read_bytes(), data)
        with self.assertRaises(AssetNotFoundError):
            self.store.describe("owner", handle)
        # Idempotent: the handle is gone, so a second pass moves nothing.
        self.assertEqual(self.store.sweep_published({handle: 0.0}, grace_seconds=0)["assets"], 0)
        # An orphan (published, no durable row) is retired only past the grace, and a
        # handle any row still points at is never mistaken for one.
        orphan, _ = self._published_upload("owner", b"o" * (40 * KiB))
        orphan_handle = orphan["asset_handle"]
        self.assertEqual(self.store.sweep_published({}, grace_seconds=0, protected={orphan_handle})["assets"], 0)
        self.assertTrue(self.published_blob(orphan_handle).exists())
        self.assertEqual(self.store.sweep_published({}, grace_seconds=0, protected=set())["assets"], 1)
        self.assertFalse(self.published_blob(orphan_handle).exists())

    def test_sweep_published_refuses_a_handle_recreated_after_the_terminal_row(self) -> None:
        data = b"i" * (44 * KiB)
        asset, upload_id = self._published_upload("owner", data)
        handle = asset["asset_handle"]
        # A stale row names this handle but an upload that is no longer its own.
        stale = {"settled_at": 0.0, "owner_scope": "owner", "upload_id": "0" * 32}
        self.assertEqual(self.store.sweep_published({handle: stale}, grace_seconds=0)["assets"], 0)
        self.assertTrue(self.published_blob(handle).exists())
        # The identity the row recorded retires it.
        recorded = {"settled_at": 0.0, "owner_scope": "owner", "sha256": asset["sha256"],
                    "size_bytes": asset["size_bytes"], "upload_id": upload_id}
        self.assertEqual(self.store.sweep_published({handle: recorded}, grace_seconds=0)["assets"], 1)
        self.assertFalse(self.published_blob(handle).exists())

    def test_typed_multidimensional_and_single_piece_overflow_never_drain(self) -> None:
        typed = memoryview(bytearray(range(8))).cast("I")
        shaped = memoryview(bytearray(range(8))).cast("B", shape=(2, 4))
        begun = self.store.begin_upload("owner", digest(b"xx"), 2)
        for source in (OnePieceWithoutDrain(typed), OneItemWithoutDrain(typed), OnePieceWithoutDrain(shaped), OneItemWithoutDrain(shaped), OnePieceWithoutDrain(b"xxx"), OneItemWithoutDrain(b"xxx")):
            with self.subTest(source=type(source).__name__), self.assertRaises(AssetValidationError):
                self.store.append_chunk("owner", begun["upload_id"], 0, source, 2)
            self.assertEqual(source.calls, 1)
            self.assertEqual(self.store.upload_status("owner", begun["upload_id"])["committed_offset"], 0)
        self.assertTrue(all(path.stat().st_size <= 2 for path in self.chunk_blobs(begun["upload_id"])))
        self.assertEqual(self.store.append_chunk("owner", begun["upload_id"], 0, iter((memoryview(bytearray(b"xx")),)), 2), 2)

    def test_known_short_and_overflow_rejection_survives_same_peer_and_restart(self) -> None:
        for mode in ("short", "overflow"):
            with self.subTest(mode=mode):
                root = self.sandbox / f"known-{mode}"
                first = self.open_store(root)
                peer = self.open_store(root)
                data = b"ok"
                begun = first.begin_upload("owner", digest(data), len(data))
                stream = io.BytesIO(b"x" if mode == "short" else b"xxx")
                with self.assertRaises(AssetValidationError):
                    first.append_chunk("owner", begun["upload_id"], 0, stream, len(data))
                for store in (first, peer):
                    status = store.upload_status("owner", begun["upload_id"])
                    self.assertEqual((status["state"], status["committed_offset"]), ("incomplete", 0))
                first.close()
                peer.close()
                restarted = self.open_store(root)
                self.assertEqual(restarted.upload_status("owner", begun["upload_id"])["state"], "incomplete")
                restarted.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))

    def test_unknown_interrupted_range_holds_same_peer_and_restart(self) -> None:
        root = self.sandbox / "unknown-range"
        first = self.open_store(root)
        peer = self.open_store(root)
        data = b"unknown"
        begun = first.begin_upload("owner", digest(data), len(data))
        with self.assertRaises(SimulatedCrash):
            first.append_chunk("owner", begun["upload_id"], 0, CrashingIterator(data), len(data))
        for store in (first, peer):
            with self.assertRaises(AssetRecoveryRequiredError):
                store.upload_status("owner", begun["upload_id"])
        first.close()
        peer.close()
        restarted = self.open_store(root)
        with self.assertRaises(AssetRecoveryRequiredError):
            restarted.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))

    def test_range_record_and_payload_are_immutable_on_replay(self) -> None:
        data = b"immutable range"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        range_path = self.upload_dir(begun["upload_id"]) / "ranges" / "0000000000000000.json"
        before = (range_path.stat().st_ino, range_path.read_bytes(), [(p.stat().st_ino, p.read_bytes()) for p in self.chunk_blobs(begun["upload_id"])])
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        after = (range_path.stat().st_ino, range_path.read_bytes(), [(p.stat().st_ino, p.read_bytes()) for p in self.chunk_blobs(begun["upload_id"])])
        self.assertEqual(after, before)

    def test_unexpected_range_and_payload_winners_are_never_overwritten(self) -> None:
        data = b"winner"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        upload_dir = self.upload_dir(begun["upload_id"])
        unexpected_range = upload_dir / "ranges" / "0000000000000000.json"
        unexpected_range.write_bytes(b"unexpected range")
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        self.assertEqual(unexpected_range.read_bytes(), b"unexpected range")

        root = self.sandbox / "payload-winner"
        store = self.open_store(root)
        begun = store.begin_upload("owner", digest(data), len(data))
        chunks = root / "uploads" / begun["upload_id"] / "chunks"
        chunks_identity = (chunks.stat().st_dev, chunks.stat().st_ino)
        real_open = render_assets.os.open
        winner = b"unexpected payload"
        injected = False

        def inject(name, flags, mode=0o777, *, dir_fd=None):
            nonlocal injected
            if not injected and dir_fd is not None and str(name).endswith(".bin"):
                info = os.fstat(dir_fd)
                if (info.st_dev, info.st_ino) == chunks_identity and flags & os.O_EXCL:
                    injected = True
                    fd = real_open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
                    os.write(fd, winner)
                    os.close(fd)
            return real_open(name, flags, mode, dir_fd=dir_fd)

        with patch.object(render_assets.os, "open", side_effect=inject):
            with self.assertRaises((AssetRecoveryRequiredError, FileExistsError)):
                store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        self.assertTrue(injected)
        self.assertIn(winner, [path.read_bytes() for path in chunks.glob("*.bin")])

    def test_full_hash_mismatch_durably_requires_recovery_without_repeat_assembly(self) -> None:
        expected = b"expected"
        actual = b"tampered"
        begun = self.store.begin_upload("owner", digest(expected), len(expected))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(actual), len(actual))
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.finish_upload("owner", begun["upload_id"])
        assemblies = {p.name for p in self.assembly_blobs(begun["upload_id"])}
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.finish_upload("owner", begun["upload_id"])
        self.assertEqual({p.name for p in self.assembly_blobs(begun["upload_id"])}, assemblies)
        self.restart()
        self.assertEqual(self.store.upload_status("owner", begun["upload_id"])["state"], "recovery_required")

    def test_finish_is_idempotent_and_unexpected_lookup_or_output_never_wins(self) -> None:
        data = b"publication"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        init = self.init_record("owner", digest(data))
        lookup = self.root / "assets" / f"{init['asset_handle']}.json"
        lookup.write_bytes(b"unexpected lookup")
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.finish_upload("owner", begun["upload_id"])
        self.assertEqual(lookup.read_bytes(), b"unexpected lookup")

        root = self.sandbox / "output-winner"
        store = self.open_store(root)
        begun = store.begin_upload("owner", digest(data), len(data))
        store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        init_path = root / "index" / f"{owner_token('owner')}-{digest(data)}.json"
        handle = read_json(init_path)["asset_handle"]
        output = root / "published" / f"{handle}.blob"
        output.write_bytes(b"unexpected output")
        with self.assertRaises(AssetRecoveryRequiredError):
            store.finish_upload("owner", begun["upload_id"])
        self.assertEqual(output.read_bytes(), b"unexpected output")

    def test_published_replay_verifies_actual_hash_and_size(self) -> None:
        data = b"published integrity" * 100
        asset = self.upload("owner", data)
        self.assertEqual(self.store.describe("owner", asset["asset_handle"]), asset)
        self.assertEqual(self.store.finish_upload("owner", self.store.begin_upload("owner", digest(data), len(data))["upload_id"]), asset)
        output = self.published_blob(asset["asset_handle"])
        with output.open("r+b") as handle:
            handle.seek(0)
            handle.write(b"X")
            handle.flush()
            os.fsync(handle.fileno())
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.describe("owner", asset["asset_handle"])
        self.restart()
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.describe("owner", asset["asset_handle"])

    def test_wrong_scope_and_unknown_handles_are_indistinguishable(self) -> None:
        asset = self.upload("owner-a", b"private")
        unknown = "0" * 32
        errors = []
        for handle in (asset["asset_handle"], unknown):
            with self.assertRaises(AssetNotFoundError) as caught:
                self.store.describe("owner-b", handle)
            errors.append((type(caught.exception), str(caught.exception)))
        self.assertEqual(errors[0], errors[1])
        self.assertEqual(self.store.active_protection("owner-b"), [])

    def test_two_stores_and_processes_serialize_active_ranges(self) -> None:
        root = self.sandbox / "serialized"
        first = self.open_store(root)
        second = self.open_store(root)
        data = b"serialized range"
        begun = first.begin_upload("owner", digest(data), len(data))
        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        results = []

        def append_first():
            results.append(first.append_chunk("owner", begun["upload_id"], 0, PausingStream(data, entered, release), len(data)))

        def append_second():
            try:
                results.append(second.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data)))
            finally:
                second_done.set()

        one = threading.Thread(target=append_first)
        two = threading.Thread(target=append_second)
        one.start()
        self.assertTrue(entered.wait(2))
        two.start()
        self.assertFalse(second_done.wait(0.15))
        release.set()
        one.join(3)
        two.join(3)
        self.assertEqual(results, [len(data), len(data)])

        root2 = self.sandbox / "process-serialized"
        parent_store = self.open_store(root2)
        begun = parent_store.begin_upload("owner", digest(data), len(data))
        entered = threading.Event()
        release = threading.Event()
        parent = threading.Thread(target=lambda: parent_store.append_chunk("owner", begun["upload_id"], 0, PausingStream(data, entered, release), len(data)))
        parent.start()
        self.assertTrue(entered.wait(2))
        context = multiprocessing.get_context("spawn")
        started, done, output = context.Event(), context.Event(), context.Queue()
        child = context.Process(target=process_append, args=(str(root2), begun["upload_id"], data, started, done, output))
        child.start()
        self.assertTrue(started.wait(3))
        self.assertFalse(done.wait(0.2))
        release.set()
        parent.join(5)
        child.join(5)
        self.assertEqual((child.exitcode, output.get(timeout=1)), (0, ("ok", len(data))))

    def test_init_split_cuts_recover_one_winning_identity(self) -> None:
        for target in ("index", "head"):
            with self.subTest(target=target):
                root = self.sandbox / f"init-cut-{target}"
                store = self.open_store(root)
                data = b"init split"
                expected_index = f"{owner_token('owner')}-{digest(data)}.json"
                real_link = render_assets.os.link
                stopped = False

                def stop_link(src, dst, *args, **kwargs):
                    nonlocal stopped
                    name = Path(os.fspath(dst)).name
                    if not stopped and ((target == "index" and name == expected_index) or (target == "head" and name == "head.json")):
                        stopped = True
                        raise SimulatedCrash(f"stop before {target} link")
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=stop_link):
                    with self.assertRaises(SimulatedCrash):
                        store.begin_upload("owner", digest(data), len(data))
                store.close()
                restarted = self.open_store(root)
                first = restarted.begin_upload("owner", digest(data), len(data))
                self.assertEqual(restarted.begin_upload("owner", digest(data), len(data)), first)
                self.assertTrue(stopped)

    def test_index_and_head_post_link_fsync_cuts_recover_exact_state(self) -> None:
        for directory_name in ("index", "uploads"):
            with self.subTest(directory=directory_name):
                root = self.sandbox / f"fsync-cut-{directory_name}"
                store = self.open_store(root)
                watched = root / directory_name
                watched_identity = (watched.stat().st_dev, watched.stat().st_ino)
                real_fsync = render_assets.os.fsync
                stopped = False

                def stop_directory(descriptor: int):
                    nonlocal stopped
                    info = os.fstat(descriptor)
                    if not stopped and stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == watched_identity:
                        stopped = True
                        raise SimulatedCrash("post-link directory fsync")
                    return real_fsync(descriptor)

                data = b"fsync init"
                with patch.object(render_assets.os, "fsync", side_effect=stop_directory):
                    with self.assertRaises(SimulatedCrash):
                        store.begin_upload("owner", digest(data), len(data))
                store.close()
                restarted = self.open_store(root)
                descriptor = restarted.begin_upload("owner", digest(data), len(data))
                self.assertEqual(descriptor["sha256"], digest(data))
                self.assertTrue(stopped)

    def test_verified_range_cut_before_head_advance_completes_once_for_same_peer_and_restart(self) -> None:
        root = self.sandbox / "verified-range-cut"
        first = self.open_store(root)
        peer = self.open_store(root)
        data = b"verified range cut"
        begun = first.begin_upload("owner", digest(data), len(data))
        real_link = render_assets.os.link
        stopped = False

        def stop_final_head(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == "head.json":
                source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=kwargs.get("src_dir_fd"))
                try:
                    candidate = json.loads(os.read(source_fd, 8193).decode("utf-8"))
                finally:
                    os.close(source_fd)
                if candidate.get("committed_offset") == len(data) and candidate.get("pending") is None:
                    stopped = True
                    raise SimulatedCrash("range durable before head advance")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_final_head):
            with self.assertRaises(SimulatedCrash):
                first.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        for store in (peer, first):
            self.assertEqual(store.upload_status("owner", begun["upload_id"])["committed_offset"], len(data))
        first.close()
        peer.close()
        restarted = self.open_store(root)
        self.assertEqual(restarted.upload_status("owner", begun["upload_id"])["committed_offset"], len(data))
        self.assertEqual(len(list((root / "uploads" / begun["upload_id"] / "ranges").glob("0000000000000000.json"))), 1)
        self.assertTrue(stopped)

    def test_publication_output_lookup_and_final_head_cuts_keep_one_winning_handle(self) -> None:
        for cut in ("output", "lookup", "head"):
            with self.subTest(cut=cut):
                root = self.sandbox / f"publication-cut-{cut}"
                store = self.open_store(root)
                data = f"publication-{cut}".encode()
                begun = store.begin_upload("owner", digest(data), len(data))
                store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                winning_handle = read_json(root / "index" / f"{owner_token('owner')}-{digest(data)}.json")["asset_handle"]
                assets_identity = (root / "assets").stat()
                real_link = render_assets.os.link
                stopped = False

                def stop_cut(src, dst, *args, **kwargs):
                    nonlocal stopped
                    destination = Path(os.fspath(dst)).name
                    matches = cut == "output" and destination == f"{winning_handle}.blob"
                    if cut == "lookup" and destination == f"{winning_handle}.json":
                        parent = os.fstat(kwargs.get("dst_dir_fd"))
                        matches = (parent.st_dev, parent.st_ino) == (assets_identity.st_dev, assets_identity.st_ino)
                    if cut == "head" and destination == "head.json":
                        source_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=kwargs.get("src_dir_fd"))
                        try:
                            candidate = json.loads(os.read(source_fd, 8193).decode("utf-8"))
                        finally:
                            os.close(source_fd)
                        matches = candidate.get("state") == "published"
                    if not stopped and matches:
                        stopped = True
                        raise SimulatedCrash(f"publication {cut} cut")
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=stop_cut):
                    with self.assertRaises(SimulatedCrash):
                        store.finish_upload("owner", begun["upload_id"])
                store.close()
                restarted = self.open_store(root)
                asset = restarted.finish_upload("owner", begun["upload_id"])
                self.assertEqual(asset["asset_handle"], winning_handle)
                self.assertEqual(restarted.finish_upload("owner", begun["upload_id"]), asset)
                self.assertTrue(stopped)

    def test_mismatched_publication_candidate_is_preserved_and_refused(self) -> None:
        data = b"candidate linkage"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        assembly = self.upload_dir(begun["upload_id"]) / "assembly"
        assembly_identity = (assembly.stat().st_dev, assembly.stat().st_ino)
        real_open = render_assets.os.open
        stopped = False

        def stop_expected_candidate(name, flags, mode=0o777, *, dir_fd=None):
            nonlocal stopped
            if not stopped and dir_fd is not None and str(name).endswith(".bin") and flags & os.O_EXCL:
                parent = os.fstat(dir_fd)
                if (parent.st_dev, parent.st_ino) == assembly_identity:
                    stopped = True
                    raise SimulatedCrash("publication pending before candidate bytes")
            return real_open(name, flags, mode, dir_fd=dir_fd)

        with patch.object(render_assets.os, "open", side_effect=stop_expected_candidate):
            with self.assertRaises(SimulatedCrash):
                self.store.finish_upload("owner", begun["upload_id"])
        mismatched = assembly / f"{'0' * 32}.bin"
        mismatched.write_bytes(data)
        self.store.close()
        restarted = self.open_store()
        with self.assertRaises(AssetRecoveryRequiredError):
            restarted.finish_upload("owner", begun["upload_id"])
        self.assertEqual(mismatched.read_bytes(), data)

    def test_corrupt_or_missing_immutable_range_record_fails_closed_with_payload_retained(self) -> None:
        for mode in ("corrupt", "missing"):
            with self.subTest(mode=mode):
                root = self.sandbox / f"range-record-{mode}"
                store = self.open_store(root)
                data = b"range integrity"
                begun = store.begin_upload("owner", digest(data), len(data))
                store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                payload = next((root / "uploads" / begun["upload_id"] / "chunks").glob("*.bin"))
                record = root / "uploads" / begun["upload_id"] / "ranges" / "0000000000000000.json"
                if mode == "corrupt":
                    record.write_bytes(b"{corrupt}\n")
                else:
                    record.rename(record.with_name("missing-record-evidence.json"))
                store.close()
                try:
                    restarted = self.open_store(root)
                except AssetRecoveryRequiredError:
                    pass
                else:
                    with self.assertRaises(AssetRecoveryRequiredError):
                        restarted.upload_status("owner", begun["upload_id"])
                self.assertEqual(payload.read_bytes(), data)

    def test_protection_key_uses_protection_kind_and_exact_canonical_tuple(self) -> None:
        reference_key = render_assets._protection_key(OWNER, ASSET, "reference", "same")
        reader_key = render_assets._protection_key(OWNER, ASSET, "reader", "same")
        self.assertEqual(reference_key, digest(canonical([OWNER, ASSET, "reference", "same"])))
        self.assertEqual(reader_key, digest(canonical([OWNER, ASSET, "reader", "same"])))
        self.assertNotEqual(reference_key, reader_key)

    def test_format_and_init_leaf_corruption_never_mints_replacement_identity(self) -> None:
        data = b"identity corruption"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        init_path = self.root / "index" / f"{owner_token('owner')}-{digest(data)}.json"
        original = init_path.read_bytes()
        init_path.write_bytes(b"{corrupt}\n")
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.begin_upload("owner", digest(data), len(data))
        self.assertEqual(init_path.read_bytes(), b"{corrupt}\n")
        self.assertEqual(len(list((self.root / "uploads").iterdir())), 1)
        evidence = init_path.with_name(init_path.name + ".original-evidence")
        evidence.write_bytes(original)
        self.assertEqual(read_json(evidence)["upload_id"], begun["upload_id"])

    def test_record_change_creation_and_replacement_never_overwrite_winners(self) -> None:
        cases = ("head-create", "head-replace", "protection-create")
        for case in cases:
            with self.subTest(case=case):
                root = self.sandbox / case
                store = self.open_store(root)
                data = b"record winner"
                begun = None
                target_name = "head.json"
                if case != "head-create":
                    begun = store.begin_upload("owner", digest(data), len(data))
                if case == "protection-create":
                    asset = self.upload_with(store, "owner", data)
                    key_source = [owner_token("owner"), asset["asset_handle"], "reference", "attempt"]
                    target_name = f"{digest(canonical(key_source))}.json"
                real_link = render_assets.os.link
                winner = b"unexpected live record"
                injected = False

                def inject_winner(src, dst, *args, **kwargs):
                    nonlocal injected
                    if not injected and Path(os.fspath(dst)).name == target_name:
                        injected = True
                        directory_fd = kwargs.get("dst_dir_fd")
                        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                        os.write(fd, winner)
                        os.fsync(fd)
                        os.close(fd)
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=inject_winner):
                    with self.assertRaises((AssetRecoveryRequiredError, FileExistsError)):
                        if case == "head-create":
                            store.begin_upload("owner", digest(data), len(data))
                        elif case == "head-replace":
                            store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                        else:
                            store.acquire_reference("owner", asset["asset_handle"], "attempt")
                self.assertTrue(injected)
                self.assertIn(winner, [path.read_bytes() for path in root.rglob(target_name)])

    def test_record_change_history_keeps_creation_and_replacement_cuts(self) -> None:
        data = b"history"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        creation_archives = [path for path in (self.root / "_trash" / "metadata-history").iterdir() if path.is_dir()]
        self.assertTrue(any((path / "candidate.json").is_file() and (path / "marker.json").is_file() and not (path / "prior.json").exists() for path in creation_archives))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        archives = [path for path in (self.root / "_trash" / "metadata-history").iterdir() if path.is_dir()]
        replacements = [path for path in archives if (path / "prior.json").is_file()]
        self.assertTrue(replacements)
        for archive in replacements:
            self.assertEqual({p.name for p in archive.iterdir()}, {"prior.json", "candidate.json", "marker.json"})

    def test_every_successful_cut_fsyncs_record_payload_and_parent_directories(self) -> None:
        seen: set[tuple[int, int]] = set()
        real_fsync = render_assets.os.fsync

        def remember(descriptor: int):
            info = os.fstat(descriptor)
            seen.add((info.st_dev, info.st_ino))
            return real_fsync(descriptor)

        data = b"fsync coverage"
        with patch.object(render_assets.os, "fsync", side_effect=remember):
            begun = self.store.begin_upload("owner", digest(data), len(data))
            self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
            asset = self.store.finish_upload("owner", begun["upload_id"])
            self.store.acquire_reference("owner", asset["asset_handle"], "attempt-fsync")
        upload = self.upload_dir(begun["upload_id"])
        expected_directories = [self.root / "index", self.root / "uploads", upload, upload / "ranges", upload / "chunks", upload / "assembly", upload / "changes", upload / "protections", self.root / "assets", self.root / "published", self.root / "_trash" / "metadata-history"]
        for directory in expected_directories:
            info = directory.stat()
            self.assertIn((info.st_dev, info.st_ino), seen, directory)
        for path in (self.root / "index").glob("*.json"):
            info = path.stat()
            self.assertIn((info.st_dev, info.st_ino), seen, path)
        self.assertIn((self.published_blob(asset["asset_handle"]).stat().st_dev, self.published_blob(asset["asset_handle"]).stat().st_ino), seen)

    def test_actual_parent_root_subdirectory_and_lock_swaps_are_refused(self) -> None:
        root = self.sandbox / "swap-root"
        first = self.open_store(root)
        peer = self.open_store(root)
        data = b"swap"
        begun = first.begin_upload("owner", digest(data), len(data))
        lock = root / "store.lock"
        saved_lock = root / "store-lock-evidence"
        lock.rename(saved_lock)
        lock.write_bytes(b"replacement lock")
        for store in (first, peer):
            with self.assertRaises(AssetRecoveryRequiredError):
                store.upload_status("owner", begun["upload_id"])
        self.assertEqual(lock.read_bytes(), b"replacement lock")

        foreign = self.sandbox / "foreign"
        foreign.mkdir()
        parent = self.sandbox / "owned-parent"
        owned_root = parent / "store"
        store = self.open_store(owned_root)
        begun = store.begin_upload("owner", digest(data), len(data))
        saved_parent = self.sandbox / "owned-parent-evidence"
        parent.rename(saved_parent)
        parent.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(AssetRecoveryRequiredError):
            store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        self.assertEqual(list(foreign.iterdir()), [])

    def test_initial_root_resolution_never_traverses_symlink_or_replaced_parent(self) -> None:
        foreign = self.sandbox / "foreign-init"
        foreign.mkdir()
        link_parent = self.sandbox / "link-parent"
        link_parent.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(AssetValidationError):
            self.open_store(link_parent / "store")
        self.assertEqual(list(foreign.iterdir()), [])

        parent = self.sandbox / "replace-parent"
        root = parent / "store"
        root.mkdir(parents=True)
        saved = self.sandbox / "replace-parent-evidence"
        foreign2 = self.sandbox / "foreign-init-2"
        foreign2.mkdir()
        real_reject = RenderAssetStore._reject_symlink_chain
        swapped = False

        def swap_after_verified_parent(path: Path) -> None:
            nonlocal swapped
            real_reject(path)
            if not swapped and Path(path) == parent:
                swapped = True
                parent.rename(saved)
                parent.symlink_to(foreign2, target_is_directory=True)

        with patch.object(RenderAssetStore, "_reject_symlink_chain", new=staticmethod(swap_after_verified_parent)):
            with self.assertRaises((AssetValidationError, AssetRecoveryRequiredError)):
                self.open_store(root)
        self.assertTrue(swapped)
        self.assertEqual(list(foreign2.iterdir()), [])

    def test_initial_root_boots_under_a_platform_symlink_prefix(self) -> None:
        # macOS ships /var and /tmp as root-owned links into /private. An
        # isolated boot under $TMPDIR has one in its ancestry; it must not be
        # mistaken for a symlink attack, while a user-owned link still fails.
        if not Path("/tmp").is_symlink():
            self.skipTest("this platform has no symlinked temp prefix")
        sandbox = Path(tempfile.mkdtemp(prefix="platform-", dir="/tmp"))
        try:
            store = self.open_store(sandbox / "store")
            self.assertEqual(store.root.name, "store")
            self.assertTrue(store.root.is_dir())
            self.assertEqual(store.root.parent, Path("/tmp").resolve() / sandbox.name)
        finally:
            sandbox.rename(self._trash_bucket / f"platform-prefix-{uuid4().hex}")

    def test_opened_head_and_published_leaf_swaps_are_detected(self) -> None:
        data = b"opened identity"
        asset = self.upload("owner", data)
        begun = self.store.begin_upload("owner", digest(data), len(data))
        targets = (("head.json", lambda: self.store.upload_status("owner", begun["upload_id"])), (f"{asset['asset_handle']}.blob", lambda: self.store.describe("owner", asset["asset_handle"])))
        for target, action in targets:
            with self.subTest(target=target):
                real_open = render_assets.os.open
                swapped = False

                def swap_after_open(name, flags, mode=0o777, *, dir_fd=None):
                    nonlocal swapped
                    descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
                    if not swapped and Path(os.fspath(name)).name == target and not flags & os.O_CREAT:
                        swapped = True
                        directory = Path(f"/dev/fd/{dir_fd}").resolve() if dir_fd is not None else Path(name).parent
                        visible = directory / target
                        saved = visible.with_name(visible.name + ".opened-evidence")
                        visible.rename(saved)
                        visible.write_bytes(b"foreign replacement")
                    return descriptor

                with patch.object(render_assets.os, "open", side_effect=swap_after_open):
                    with self.assertRaises(AssetRecoveryRequiredError):
                        action()
                self.assertTrue(swapped)

    def test_constructor_refuses_old_or_unknown_format_before_modification(self) -> None:
        for name in ("state.sqlite3", "unknown.bin"):
            with self.subTest(name=name):
                root = self.sandbox / f"foreign-format-{name.replace('.', '-') }"
                root.mkdir()
                evidence = root / name
                evidence.write_bytes(b"existing evidence")
                with self.assertRaises(AssetValidationError):
                    self.open_store(root)
                self.assertEqual(evidence.read_bytes(), b"existing evidence")
                self.assertFalse((root / "format.json").exists())

    def test_constructor_unwinds_every_acquired_descriptor_on_failure(self) -> None:
        for fail_at in range(2, 10):
            with self.subTest(fail_at=fail_at):
                root = self.sandbox / f"constructor-unwind-{fail_at}"
                opened: list[int] = []
                real_open = render_assets.os.open
                calls = 0

                def fail_open(name, flags, mode=0o777, *, dir_fd=None):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError("injected constructor open failure")
                    descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
                    opened.append(descriptor)
                    return descriptor

                with patch.object(render_assets.os, "open", side_effect=fail_open):
                    try:
                        store = self.open_store(root)
                    except (OSError, AssetValidationError, AssetRecoveryRequiredError):
                        pass
                    else:
                        store.close()
                for descriptor in opened:
                    with self.assertRaises(OSError) as caught:
                        fcntl.fcntl(descriptor, fcntl.F_GETFD)
                    self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_payload_open_fstat_and_fdopen_failures_close_the_descriptor(self) -> None:
        for failing_name in ("fstat", "fdopen"):
            with self.subTest(failing_name=failing_name):
                asset = self.upload("owner", f"payload open unwind {failing_name}".encode())
                opened = []
                real_open = render_assets.os.open
                real_fstat = render_assets.os.fstat
                real_fdopen = render_assets.os.fdopen
                failed = False

                def track_open(name, flags, mode=0o777, *, dir_fd=None):
                    descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
                    if Path(os.fspath(name)).name == f"{asset['asset_handle']}.blob":
                        opened.append(descriptor)
                    return descriptor

                def fail_fstat(descriptor):
                    nonlocal failed
                    if failing_name == "fstat" and descriptor in opened and not failed:
                        failed = True
                        raise OSError("injected fstat failure")
                    return real_fstat(descriptor)

                def fail_fdopen(descriptor, *args, **kwargs):
                    nonlocal failed
                    if failing_name == "fdopen" and descriptor in opened and not failed:
                        failed = True
                        raise OSError("injected fdopen failure")
                    return real_fdopen(descriptor, *args, **kwargs)

                with patch.object(render_assets.os, "open", side_effect=track_open), patch.object(render_assets.os, "fstat", side_effect=fail_fstat), patch.object(render_assets.os, "fdopen", side_effect=fail_fdopen):
                    with self.assertRaises(AssetRecoveryRequiredError):
                        self.store.describe("owner", asset["asset_handle"])
                self.assertTrue(opened)
                self.assertTrue(failed)
                for descriptor in opened:
                    with self.assertRaises(OSError):
                        fcntl.fcntl(descriptor, fcntl.F_GETFD)
                status = self.store.upload_status("owner", self.init_record("owner", asset["sha256"])["upload_id"])
                self.assertEqual((status["state"], status["recovery_required"]), ("recovery_required", True))

    def test_reference_key_generation_release_and_reacquire_are_durable(self) -> None:
        asset = self.upload("owner", b"protection")
        reference = "accepted-attempt"
        key_source = [owner_token("owner"), asset["asset_handle"], "reference", reference]
        key = digest(canonical(key_source))
        self.store.acquire_reference("owner", asset["asset_handle"], reference)
        record_path = next((self.root / "uploads").glob(f"*/protections/{key}.json"))
        active = read_json(record_path)
        self.assertEqual((active["protection_kind"], active["reference"], active["generation"], active["state"]), ("reference", reference, 0, "active"))
        self.store.release_reference("owner", asset["asset_handle"], reference)
        self.assertEqual(self.store.active_protection("owner"), [])
        released = read_json(record_path)
        self.assertEqual((released["generation"], released["state"]), (1, "released"))
        self.store.acquire_reference("owner", asset["asset_handle"], reference)
        reacquired = read_json(record_path)
        self.assertEqual((reacquired["generation"], reacquired["state"]), (2, "active"))

    def test_durable_explicit_release_intent_completes_but_absent_intent_stays_active(self) -> None:
        asset = self.upload("owner", b"release authority")
        reference = "attempt-release"
        self.store.acquire_reference("owner", asset["asset_handle"], reference)
        key = digest(canonical([owner_token("owner"), asset["asset_handle"], "reference", reference]))
        real_link = render_assets.os.link
        stopped = False

        def stop_release_link(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == f"{key}.json":
                stopped = True
                raise SimulatedCrash("release intent durable before publication")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_release_link):
            with self.assertRaises(SimulatedCrash):
                self.store.release_reference("owner", asset["asset_handle"], reference)
        self.restart()
        self.assertEqual(self.store.active_protection("owner"), [])
        self.assertTrue(stopped)

        asset2 = self.upload("owner", b"no release authority")
        self.store.acquire_reference("owner", asset2["asset_handle"], "still-active")
        self.restart()
        self.assertEqual([row["reference"] for row in self.store.active_protection("owner")], ["still-active"])

    def test_reader_release_intent_follows_verified_close_and_close_failure_stays_protected(self) -> None:
        asset = self.upload("owner", b"reader close authority")
        context = self.store.open_reader("owner", asset["asset_handle"])
        reader = context.__enter__()
        failing = TrackedReader(reader._file, fail_close=True)
        reader._file = failing
        with self.assertRaises(OSError):
            context.__exit__(None, None, None)
        self.assertTrue(failing.closed)
        protections = self.store.active_protection("owner")
        self.assertEqual([row["kind"] for row in protections], ["reader"])
        self.restart()
        self.assertEqual([row["kind"] for row in self.store.active_protection("owner")], ["reader"])

    def test_reader_registration_is_atomic_with_shutdown_and_store_close_is_idempotent(self) -> None:
        asset = self.upload("owner", b"atomic shutdown")
        context = self.store.open_reader("owner", asset["asset_handle"])
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        outcomes = []
        original = self.store._remember_reader

        def pause(reader):
            entered.set()
            if not release.wait(5):
                raise AssertionError("registration not released")
            original(reader)

        def use():
            try:
                reader = context.__enter__()
                closed.wait(5)
                with self.assertRaises(ValueError):
                    reader.read(1)
                context.__exit__(None, None, None)
                outcomes.append("closed")
            except BaseException as exc:
                outcomes.append((type(exc).__name__, str(exc)))

        with patch.object(self.store, "_remember_reader", side_effect=pause):
            thread = threading.Thread(target=use)
            thread.start()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=lambda: (self.store.close(), closed.set()))
            closer.start()
            self.assertFalse(closed.wait(0.15))
            release.set()
            thread.join(3)
            closer.join(3)
        self.assertEqual(outcomes, ["closed"])
        self.store.close()

    def test_streamed_reader_and_many_ranges_remain_bounded(self) -> None:
        size = 8 * MiB
        block = b"0123456789abcdef" * 4 * KiB
        begun = self.store.begin_upload("owner", digest(block * (size // len(block))), size)
        offset = 0
        while offset < size:
            stream = BoundedStream(block, 64 * KiB)
            offset = self.store.append_chunk("owner", begun["upload_id"], offset, stream, len(block))
            self.assertNotIn(-1, stream.requests)
        asset = self.store.finish_upload("owner", begun["upload_id"])
        tracemalloc.start()
        try:
            actual = hashlib.sha256()
            with self.store.open_reader("owner", asset["asset_handle"]) as reader:
                while piece := reader.read(64 * KiB):
                    actual.update(piece)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual.hexdigest(), asset["sha256"])
        self.assertLess(peak, 2 * MiB)

    def test_missing_authoritative_head_never_reinitializes_accepted_evidence(self) -> None:
        for stage in ("range", "published", "protection"):
            with self.subTest(stage=stage):
                root = self.sandbox / f"missing-head-{stage}"
                first = self.open_store(root)
                peer = self.open_store(root)
                data = f"missing-head-{stage}".encode()
                begun = first.begin_upload("owner", digest(data), len(data))
                first.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                if stage in {"published", "protection"}:
                    asset = first.finish_upload("owner", begun["upload_id"])
                if stage == "protection":
                    first.acquire_reference("owner", asset["asset_handle"], "retained-reference")
                head = root / "uploads" / begun["upload_id"] / "head.json"
                accepted = read_json(head)
                self.assertGreater(accepted["generation"], 0)
                evidence = self.sandbox / f"{stage}-accepted-head-evidence.json"
                head.rename(evidence)
                for store in (first, peer):
                    with self.assertRaises(AssetRecoveryRequiredError):
                        store.upload_status("owner", begun["upload_id"])
                    self.assertFalse(head.exists())
                first.close()
                peer.close()
                restarted = self.open_store(root)
                with self.assertRaises(AssetRecoveryRequiredError):
                    restarted.upload_status("owner", begun["upload_id"])
                with self.assertRaises(AssetRecoveryRequiredError):
                    restarted.begin_upload("owner", digest(data), len(data))
                self.assertFalse(head.exists())
                self.assertEqual(read_json(evidence), accepted)

    def test_constructor_keeps_verified_parent_authority_across_ancestor_swap(self) -> None:
        trusted = self.sandbox / "verified-ancestor"
        parent = trusted / "parent"
        parent.mkdir(parents=True)
        foreign = self.sandbox / "foreign-ancestor"
        foreign_parent = foreign / "parent"
        foreign_parent.mkdir(parents=True)
        sentinel = foreign_parent / "sentinel"
        sentinel.write_bytes(b"foreign evidence")
        saved = self.sandbox / "verified-ancestor-evidence"
        real_reject = RenderAssetStore._reject_symlink_chain
        swapped = False

        def swap_after_verified_walk(path: Path) -> None:
            nonlocal swapped
            real_reject(path)
            if not swapped and Path(path) == parent:
                swapped = True
                trusted.rename(saved)
                trusted.symlink_to(foreign, target_is_directory=True)

        with patch.object(RenderAssetStore, "_reject_symlink_chain", new=staticmethod(swap_after_verified_walk)):
            with self.assertRaises((AssetValidationError, AssetRecoveryRequiredError)):
                self.open_store(parent / "store")
        self.assertTrue(swapped)
        self.assertEqual(sentinel.read_bytes(), b"foreign evidence")
        self.assertFalse((foreign_parent / "store").exists())

    def test_record_change_refuses_replaced_record_parent(self) -> None:
        asset = self.upload("owner", b"record parent binding")
        reference = "parent-binding"
        key = digest(canonical([owner_token("owner"), asset["asset_handle"], "reference", reference]))
        upload_id = self.init_record("owner", asset["sha256"])["upload_id"]
        upload = self.upload_dir(upload_id)
        protections = upload / "protections"
        real_link = render_assets.os.link
        stopped = False

        def stop_protection_link(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == f"{key}.json":
                stopped = True
                raise SimulatedCrash("protection marker durable before live link")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_protection_link):
            with self.assertRaises(SimulatedCrash):
                self.store.acquire_reference("owner", asset["asset_handle"], reference)
        marker = next((upload / "changes").glob("change-*.json"))
        recorded_parent = read_json(marker)
        old_protections = upload / "protections-parent-evidence"
        protections.rename(old_protections)
        protections.mkdir()
        with self.assertRaises(AssetRecoveryRequiredError):
            self.store.active_protection("owner")
        self.assertTrue(stopped)
        self.assertEqual((recorded_parent["parent_device"], recorded_parent["parent_inode"]), (old_protections.stat().st_dev, old_protections.stat().st_ino))
        self.assertEqual(list(protections.iterdir()), [])
        self.assertTrue(marker.exists())

    def test_record_change_binds_marker_name_and_candidate_generation_before_link(self) -> None:
        for mode in ("marker-name", "candidate-generation"):
            with self.subTest(mode=mode):
                root = self.sandbox / f"change-binding-{mode}"
                store = self.open_store(root)
                data = mode.encode()
                begun = store.begin_upload("owner", digest(data), len(data))
                real_link = render_assets.os.link
                stopped = False

                def stop_head_link(src, dst, *args, **kwargs):
                    nonlocal stopped
                    if not stopped and Path(os.fspath(dst)).name == "head.json":
                        stopped = True
                        raise SimulatedCrash("head change prepared before link")
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=stop_head_link):
                    with self.assertRaises(SimulatedCrash):
                        store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                changes = root / "uploads" / begun["upload_id"] / "changes"
                marker = next(changes.glob("change-*.json"))
                marker_record = read_json(marker)
                candidate = changes / f"candidate-{marker_record['candidate_id']}.json"
                if mode == "marker-name":
                    marker.rename(changes / f"change-{'0' * 32}.json")
                else:
                    candidate_record = read_json(candidate)
                    candidate_record["generation"] += 1
                    candidate.write_bytes(canonical(candidate_record))
                    marker_record["new_sha256"] = digest(candidate.read_bytes())
                    marker.write_bytes(canonical(marker_record))
                with self.assertRaises(AssetRecoveryRequiredError):
                    store.upload_status("owner", begun["upload_id"])
                self.assertFalse((root / "uploads" / begun["upload_id"] / "head.json").exists())
                self.assertTrue(stopped)

    def test_protection_change_candidate_binds_to_containing_upload_head(self) -> None:
        for field in ("owner_token", "asset_handle", "upload_id"):
            with self.subTest(field=field):
                root = self.sandbox / f"protection-candidate-{field}"
                store = self.open_store(root)
                first_data = f"candidate-first-{field}".encode()
                second_data = f"candidate-second-{field}".encode()
                first = self.upload_with(store, "owner", first_data)
                second = self.upload_with(store, "owner", second_data)
                first_upload = read_json(root / "index" / f"{owner_token('owner')}-{digest(first_data)}.json")["upload_id"]
                second_upload = read_json(root / "index" / f"{owner_token('owner')}-{digest(second_data)}.json")["upload_id"]
                reference = f"candidate-{field}"
                original_key = digest(canonical([owner_token("owner"), first["asset_handle"], "reference", reference]))
                real_link = render_assets.os.link
                stopped = False

                def stop_protection_link(src, dst, *args, **kwargs):
                    nonlocal stopped
                    if not stopped and Path(os.fspath(dst)).name == f"{original_key}.json":
                        stopped = True
                        raise SimulatedCrash("protection candidate durable before publication")
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=stop_protection_link):
                    with self.assertRaises(SimulatedCrash):
                        store.acquire_reference("owner", first["asset_handle"], reference)
                changes = root / "uploads" / first_upload / "changes"
                marker_path = next(changes.glob("change-*.json"))
                marker = read_json(marker_path)
                candidate_path = changes / f"candidate-{marker['candidate_id']}.json"
                candidate = read_json(candidate_path)
                replacements = {
                    "owner_token": owner_token("other-owner"),
                    "asset_handle": second["asset_handle"],
                    "upload_id": second_upload,
                }
                candidate[field] = replacements[field]
                candidate_path.write_bytes(canonical(candidate))
                mutated_key = digest(canonical([candidate["owner_token"], candidate["asset_handle"], candidate["protection_kind"], candidate["reference"]]))
                marker["target_name"] = f"{mutated_key}.json"
                marker["new_sha256"] = digest(candidate_path.read_bytes())
                marker_path.write_bytes(canonical(marker))

                with self.assertRaises(AssetRecoveryRequiredError):
                    store.active_protection("owner")
                self.assertFalse((root / "uploads" / first_upload / "protections" / f"{mutated_key}.json").exists())
                self.assertTrue(candidate_path.exists())
                self.assertTrue(marker_path.exists())
                self.assertTrue(stopped)

    def test_protection_snapshot_binds_every_valid_row_to_containing_upload_head(self) -> None:
        for field in ("owner_token", "asset_handle", "upload_id"):
            with self.subTest(field=field):
                root = self.sandbox / f"protection-snapshot-{field}"
                store = self.open_store(root)
                first_data = f"snapshot-first-{field}".encode()
                second_data = f"snapshot-second-{field}".encode()
                first = self.upload_with(store, "owner", first_data)
                second = self.upload_with(store, "owner", second_data)
                first_upload = read_json(root / "index" / f"{owner_token('owner')}-{digest(first_data)}.json")["upload_id"]
                second_upload = read_json(root / "index" / f"{owner_token('owner')}-{digest(second_data)}.json")["upload_id"]
                reference = f"snapshot-{field}"
                store.acquire_reference("owner", first["asset_handle"], reference)
                protections = root / "uploads" / first_upload / "protections"
                active = next(protections.glob("*.json"))
                retained = self.sandbox / f"retained-protection-snapshot-{field}.json"
                active.rename(retained)
                record = read_json(retained)
                replacements = {
                    "owner_token": owner_token("other-owner"),
                    "asset_handle": second["asset_handle"],
                    "upload_id": second_upload,
                }
                record[field] = replacements[field]
                mutated_key = digest(canonical([record["owner_token"], record["asset_handle"], record["protection_kind"], record["reference"]]))
                mutated = protections / f"{mutated_key}.json"
                mutated.write_bytes(canonical(record))

                with self.assertRaises(AssetRecoveryRequiredError):
                    store.active_protection("owner")
                self.assertTrue(retained.exists())
                self.assertEqual(mutated.read_bytes(), canonical(record))

    def test_cross_record_init_head_pending_and_asset_relationships_fail_closed(self) -> None:
        for field, replacement in (("owner_token", "f" * 64), ("sha256", "e" * 64), ("size_bytes", 99), ("asset_handle", "a" * 32)):
            with self.subTest(record="head", field=field):
                root = self.sandbox / f"head-mismatch-{field}"
                store = self.open_store(root)
                data = b"head relationship"
                begun = store.begin_upload("owner", digest(data), len(data))
                head_path = root / "uploads" / begun["upload_id"] / "head.json"
                head = read_json(head_path)
                head[field] = replacement
                head_path.write_bytes(canonical(head))
                with self.assertRaises(AssetRecoveryRequiredError):
                    store.upload_status("owner", begun["upload_id"])

        root = self.sandbox / "pending-frontier"
        store = self.open_store(root)
        data = b"frontier"
        begun = store.begin_upload("owner", digest(data), len(data))
        head_path = root / "uploads" / begun["upload_id"] / "head.json"
        head = read_json(head_path)
        candidate_id, operation_id = uuid4().hex, uuid4().hex
        payload = b"x"
        (root / "uploads" / begun["upload_id"] / "chunks" / f"{candidate_id}.bin").write_bytes(payload)
        head["pending"] = {"kind": "range", "operation_id": operation_id, "candidate_id": candidate_id, "offset": 1, "length": 1, "phase": "verified", "sha256": digest(payload)}
        head_path.write_bytes(canonical(head))
        with self.assertRaises(AssetRecoveryRequiredError):
            store.upload_status("owner", begun["upload_id"])
        self.assertFalse((root / "uploads" / begun["upload_id"] / "ranges" / "0000000000000001.json").exists())

        root = self.sandbox / "asset-tuple"
        store = self.open_store(root)
        asset = self.upload_with(store, "owner", b"asset relationship")
        lookup = root / "assets" / f"{asset['asset_handle']}.json"
        changed = read_json(lookup)
        changed["size_bytes"] += 1
        lookup.write_bytes(canonical(changed))
        with self.assertRaises(AssetRecoveryRequiredError):
            store.acquire_reference("owner", asset["asset_handle"], "must-not-bind")
        upload_id = changed["upload_id"]
        self.assertEqual(list((root / "uploads" / upload_id / "protections").glob("*.json")), [])

    def test_immutable_publication_verifies_the_actual_linked_leaf(self) -> None:
        for case in ("init", "range", "asset"):
            with self.subTest(case=case):
                root = self.sandbox / f"immutable-linked-{case}"
                store = self.open_store(root)
                data = f"immutable-linked-{case}".encode()
                target = None
                if case != "init":
                    begun = store.begin_upload("owner", digest(data), len(data))
                if case == "asset":
                    store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                    init = read_json(root / "index" / f"{owner_token('owner')}-{digest(data)}.json")
                    target = f"{init['asset_handle']}.json"
                    target_parent = root / "assets"
                elif case == "range":
                    target = "0000000000000000.json"
                    target_parent = root / "uploads" / begun["upload_id"] / "ranges"
                else:
                    target = f"{owner_token('owner')}-{digest(data)}.json"
                    target_parent = root / "index"
                parent_identity = (target_parent.stat().st_dev, target_parent.stat().st_ino)
                real_link = render_assets.os.link
                swapped = False
                corrupt = b'{"corrupt":true}\n'

                def swap_candidate_before_link(src, dst, *args, **kwargs):
                    nonlocal swapped
                    destination_parent = os.fstat(kwargs["dst_dir_fd"])
                    if not swapped and Path(os.fspath(dst)).name == target and (destination_parent.st_dev, destination_parent.st_ino) == parent_identity:
                        swapped = True
                        evidence_name = os.fspath(src) + ".linked-source-evidence"
                        os.rename(src, evidence_name, src_dir_fd=kwargs["src_dir_fd"], dst_dir_fd=kwargs["src_dir_fd"])
                        replacement = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=kwargs["src_dir_fd"])
                        try:
                            os.write(replacement, corrupt)
                            os.fsync(replacement)
                        finally:
                            os.close(replacement)
                    return real_link(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "link", side_effect=swap_candidate_before_link):
                    with self.assertRaises(AssetRecoveryRequiredError):
                        if case == "init":
                            store.begin_upload("owner", digest(data), len(data))
                        elif case == "range":
                            store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                        else:
                            store.finish_upload("owner", begun["upload_id"])
                self.assertTrue(swapped)
                self.assertEqual((target_parent / target).read_bytes(), corrupt)
                self.assertTrue(any(path.name.endswith(".linked-source-evidence") for path in target_parent.iterdir()))

    def test_published_integrity_refusal_is_durable_for_finish_and_reader(self) -> None:
        for caller in ("finish", "reader"):
            for mode in ("missing", "hash", "size", "symlink", "nonregular"):
                with self.subTest(caller=caller, mode=mode):
                    root = self.sandbox / f"published-integrity-{caller}-{mode}"
                    first = self.open_store(root)
                    peer = self.open_store(root)
                    data = f"published-{caller}-{mode}".encode()
                    begun = first.begin_upload("owner", digest(data), len(data))
                    first.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                    asset = first.finish_upload("owner", begun["upload_id"])
                    published = root / "published" / f"{asset['asset_handle']}.blob"
                    evidence = published.with_name(published.name + ".integrity-evidence")
                    published.rename(evidence)
                    if mode == "hash":
                        published.write_bytes(b"x" * len(data))
                    elif mode == "size":
                        published.write_bytes(data + b"x")
                    elif mode == "symlink":
                        published.symlink_to(evidence.name)
                    elif mode == "nonregular":
                        published.mkdir()
                    with self.assertRaises(AssetRecoveryRequiredError):
                        if caller == "finish":
                            first.finish_upload("owner", begun["upload_id"])
                        else:
                            first.open_reader("owner", asset["asset_handle"]).__enter__()
                    head_path = root / "uploads" / begun["upload_id"] / "head.json"
                    self.assertEqual(read_json(head_path)["state"], "recovery_required")
                    for store in (first, peer):
                        self.assertTrue(store.upload_status("owner", begun["upload_id"])["recovery_required"])
                    first.close()
                    peer.close()
                    restarted = self.open_store(root)
                    self.assertTrue(restarted.upload_status("owner", begun["upload_id"])["recovery_required"])
                    self.assertEqual(evidence.read_bytes(), data)

    def test_verified_assembly_and_missing_lookup_persist_known_recovery(self) -> None:
        root = self.sandbox / "verified-assembly-integrity"
        store = self.open_store(root)
        data = b"verified assembly integrity"
        begun = store.begin_upload("owner", digest(data), len(data))
        store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        handle = read_json(root / "index" / f"{owner_token('owner')}-{digest(data)}.json")["asset_handle"]
        real_link = render_assets.os.link
        stopped = False

        def stop_output(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == f"{handle}.blob":
                stopped = True
                raise SimulatedCrash("verified assembly before output")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_output):
            with self.assertRaises(SimulatedCrash):
                store.finish_upload("owner", begun["upload_id"])
        assembly_dir = root / "uploads" / begun["upload_id"] / "assembly"
        assembly = next(assembly_dir.glob("*.bin"))
        evidence = assembly.with_name(assembly.name + ".integrity-evidence")
        assembly.rename(evidence)
        before = {path.name for path in assembly_dir.iterdir()}
        with self.assertRaises(AssetRecoveryRequiredError):
            store.finish_upload("owner", begun["upload_id"])
        self.assertEqual(read_json(root / "uploads" / begun["upload_id"] / "head.json")["state"], "recovery_required")
        self.assertEqual({path.name for path in assembly_dir.iterdir()}, before)

        root = self.sandbox / "missing-asset-lookup"
        store = self.open_store(root)
        data = b"missing lookup recovery"
        begun = store.begin_upload("owner", digest(data), len(data))
        store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        asset = store.finish_upload("owner", begun["upload_id"])
        lookup = root / "assets" / f"{asset['asset_handle']}.json"
        lookup.rename(lookup.with_name(lookup.name + ".missing-evidence"))
        with self.assertRaises(AssetRecoveryRequiredError):
            store.finish_upload("owner", begun["upload_id"])
        self.assertEqual(read_json(root / "uploads" / begun["upload_id"] / "head.json")["state"], "recovery_required")

    def test_all_acquired_upload_and_parent_walk_descriptors_unwind(self) -> None:
        data = b"upload descriptor unwind"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        upload = self.upload_dir(begun["upload_id"])
        (upload / "unexpected-entry").write_bytes(b"retained")
        opened: list[int] = []
        real_open = render_assets.os.open

        def track_upload(name, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
            if Path(os.fspath(name)).name == begun["upload_id"] and flags & os.O_DIRECTORY:
                opened.append(descriptor)
            return descriptor

        with patch.object(render_assets.os, "open", side_effect=track_upload):
            with self.assertRaises(AssetRecoveryRequiredError):
                self.store.begin_upload("owner", digest(data), len(data))
        self.assertTrue(opened)
        for descriptor in opened:
            with self.assertRaises(OSError):
                fcntl.fcntl(descriptor, fcntl.F_GETFD)

        for failure in ("fstat", "stat"):
            with self.subTest(parent_walk=failure):
                base = self.sandbox / f"parent-unwind-{failure}"
                base.mkdir()
                parent = base / "parent"
                tracked: list[int] = []
                real_open = render_assets.os.open
                real_fstat = render_assets.os.fstat
                real_stat = render_assets.os.stat
                base_info = base.stat()
                base_identity = (base_info.st_dev, base_info.st_ino)
                failed = False

                def is_base(descriptor: int | None) -> bool:
                    if descriptor is None:
                        return False
                    info = real_fstat(descriptor)
                    return (info.st_dev, info.st_ino) == base_identity

                def track_parent(name, flags, mode=0o777, *, dir_fd=None):
                    descriptor = real_open(name, flags, mode, dir_fd=dir_fd)
                    if Path(os.fspath(name)).name == "parent" and is_base(dir_fd):
                        tracked.append(descriptor)
                    return descriptor

                def fail_parent_fstat(descriptor):
                    nonlocal failed
                    if failure == "fstat" and descriptor in tracked and not failed:
                        failed = True
                        raise OSError("injected parent fstat failure")
                    return real_fstat(descriptor)

                def fail_parent_stat(name, *args, **kwargs):
                    nonlocal failed
                    dir_fd = kwargs.get("dir_fd")
                    if failure == "stat" and Path(os.fspath(name)).name == "parent" and is_base(dir_fd) and not failed:
                        failed = True
                        raise OSError("injected parent stat failure")
                    return real_stat(name, *args, **kwargs)

                with patch.object(render_assets.os, "open", side_effect=track_parent), patch.object(render_assets.os, "fstat", side_effect=fail_parent_fstat), patch.object(render_assets.os, "stat", side_effect=fail_parent_stat):
                    with self.assertRaises(AssetValidationError):
                        self.open_store(parent / "store")
                self.assertTrue(failed)
                self.assertTrue(tracked)
                for descriptor in tracked:
                    with self.assertRaises(OSError):
                        fcntl.fcntl(descriptor, fcntl.F_GETFD)

    def test_wrapped_blob_and_reader_entry_failures_close_streams(self) -> None:
        for failure in ("read", "seek"):
            with self.subTest(blob=failure):
                root = self.sandbox / f"wrapped-blob-{failure}"
                store = self.open_store(root)
                asset = self.upload_with(store, "owner", failure.encode())
                wrapped: list[FaultingReader] = []
                real_fdopen = render_assets.os.fdopen

                def faulting_fdopen(descriptor, *args, **kwargs):
                    reader = FaultingReader(real_fdopen(descriptor, *args, **kwargs), failure)
                    wrapped.append(reader)
                    return reader

                with patch.object(render_assets.os, "fdopen", side_effect=faulting_fdopen):
                    with self.assertRaises((OSError, AssetRecoveryRequiredError)):
                        store.describe("owner", asset["asset_handle"])
                self.assertEqual(len(wrapped), 1)
                self.assertTrue(wrapped[0].failed)
                self.assertTrue(wrapped[0].closed)

        root = self.sandbox / "reader-entry-upload-open"
        store = self.open_store(root)
        asset = self.upload_with(store, "owner", b"reader entry unwind")
        wrapped: list[TrackedReader] = []
        real_fdopen = render_assets.os.fdopen
        real_open_owned = store._open_owned_upload
        calls = 0

        def tracked_fdopen(descriptor, *args, **kwargs):
            reader = TrackedReader(real_fdopen(descriptor, *args, **kwargs))
            wrapped.append(reader)
            return reader

        def fail_second_upload_open(owner, upload_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AssetRecoveryRequiredError("injected reader upload reopen failure")
            return real_open_owned(owner, upload_id)

        with patch.object(render_assets.os, "fdopen", side_effect=tracked_fdopen), patch.object(store, "_open_owned_upload", side_effect=fail_second_upload_open):
            with self.assertRaises(AssetRecoveryRequiredError):
                store.open_reader("owner", asset["asset_handle"]).__enter__()
        self.assertEqual((calls, len(wrapped)), (2, 1))
        self.assertTrue(wrapped[0].closed)

    def test_recovery_metadata_allocation_obeys_reserve_without_consuming_intent(self) -> None:
        root = self.sandbox / "recovery-admission"
        first = self.open_store(root)
        peer = self.open_store(root)
        data = b"verified recovery admission"
        begun = first.begin_upload("owner", digest(data), len(data))
        real_link = render_assets.os.link
        stopped = False

        def stop_range_publication(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == "0000000000000000.json":
                stopped = True
                raise SimulatedCrash("verified range before immutable range publication")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_range_publication):
            with self.assertRaises(SimulatedCrash):
                first.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        upload = root / "uploads" / begun["upload_id"]
        head_before = (upload / "head.json").read_bytes()
        history_before = {(path.relative_to(root), path.stat().st_ino, path.stat().st_size) for path in root.rglob("*")}
        self.free.value = 2 * MiB
        for store in (first, peer):
            with self.assertRaises(AssetCapacityError):
                store.upload_status("owner", begun["upload_id"])
            self.assertEqual((upload / "head.json").read_bytes(), head_before)
            self.assertEqual({(path.relative_to(root), path.stat().st_ino, path.stat().st_size) for path in root.rglob("*")}, history_before)
        first.close()
        peer.close()
        restarted = self.open_store(root)
        with self.assertRaises(AssetCapacityError):
            restarted.upload_status("owner", begun["upload_id"])
        self.assertEqual((upload / "head.json").read_bytes(), head_before)
        self.assertTrue(stopped)

    def test_verified_publication_reserves_before_creating_a_missing_output_link(self) -> None:
        root = self.sandbox / "publication-link-admission"
        first = self.open_store(root)
        peer = self.open_store(root)
        data = b"verified publication output admission"
        begun = first.begin_upload("owner", digest(data), len(data))
        first.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        asset_handle = read_json(root / "index" / f"{owner_token('owner')}-{digest(data)}.json")["asset_handle"]
        output = root / "published" / f"{asset_handle}.blob"
        real_link = render_assets.os.link
        stopped = False

        def stop_output_link(src, dst, *args, **kwargs):
            nonlocal stopped
            if not stopped and Path(os.fspath(dst)).name == output.name:
                stopped = True
                raise SimulatedCrash("verified publication before output link")
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "link", side_effect=stop_output_link):
            with self.assertRaises(SimulatedCrash):
                first.finish_upload("owner", begun["upload_id"])
        upload = root / "uploads" / begun["upload_id"]
        head_before = (upload / "head.json").read_bytes()
        history_before = {(path.relative_to(root), path.stat().st_ino, path.stat().st_size) for path in root.rglob("*")}
        self.assertFalse(output.exists())
        self.free.value = 2 * MiB
        for store in (first, peer):
            with self.assertRaises(AssetCapacityError):
                store.upload_status("owner", begun["upload_id"])
            self.assertFalse(output.exists())
            self.assertEqual((upload / "head.json").read_bytes(), head_before)
            self.assertEqual({(path.relative_to(root), path.stat().st_ino, path.stat().st_size) for path in root.rglob("*")}, history_before)
        first.close()
        peer.close()
        restarted = self.open_store(root)
        with self.assertRaises(AssetCapacityError):
            restarted.finish_upload("owner", begun["upload_id"])
        self.assertFalse(output.exists())
        self.assertEqual((upload / "head.json").read_bytes(), head_before)
        self.assertEqual({(path.relative_to(root), path.stat().st_ino, path.stat().st_size) for path in root.rglob("*")}, history_before)

        self.free.value = 512 * MiB
        asset = restarted.finish_upload("owner", begun["upload_id"])
        output_identity = (output.stat().st_dev, output.stat().st_ino)
        published_head = (upload / "head.json").read_bytes()
        self.free.value = 2 * MiB
        self.assertEqual(restarted.finish_upload("owner", begun["upload_id"]), asset)
        self.assertEqual((output.stat().st_dev, output.stat().st_ino), output_identity)
        self.assertEqual((upload / "head.json").read_bytes(), published_head)
        self.assertTrue(stopped)

    def test_record_change_relocation_cuts_hold_or_complete_without_losing_evidence(self) -> None:
        for cut in ("prior", "candidate", "marker"):
            with self.subTest(cut=cut):
                root = self.sandbox / f"relocation-cut-{cut}"
                store = self.open_store(root)
                data = cut.encode()
                begun = store.begin_upload("owner", digest(data), len(data))
                real_rename = render_assets.os.rename
                stopped = False

                def stop_relocation(src, dst, *args, **kwargs):
                    nonlocal stopped
                    if not stopped and Path(os.fspath(dst)).name == f"{cut}.json":
                        stopped = True
                        raise SimulatedCrash(f"record relocation {cut} cut")
                    return real_rename(src, dst, *args, **kwargs)

                with patch.object(render_assets.os, "rename", side_effect=stop_relocation):
                    with self.assertRaises(SimulatedCrash):
                        store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
                store.close()
                restarted = self.open_store(root)
                if cut == "prior":
                    with self.assertRaises(AssetRecoveryRequiredError):
                        restarted.upload_status("owner", begun["upload_id"])
                    self.assertTrue(list((root / "uploads" / begun["upload_id"] / "changes").iterdir()))
                else:
                    with self.assertRaises(AssetRecoveryRequiredError):
                        restarted.upload_status("owner", begun["upload_id"])
                    self.assertEqual(list((root / "uploads" / begun["upload_id"] / "changes").iterdir()), [])
                self.assertTrue(any(path.is_dir() for path in (root / "_trash" / "metadata-history").iterdir()))
                self.assertTrue(stopped)

    def test_record_change_relocation_fsync_order_precedes_publication_and_next_move(self) -> None:
        events = []
        real_rename = render_assets.os.rename
        real_fsync = render_assets.os.fsync
        real_link = render_assets.os.link
        data = b"relocation ordering"
        begun = self.store.begin_upload("owner", digest(data), len(data))
        upload = self.upload_dir(begun["upload_id"])
        identities = {name: (path.stat().st_dev, path.stat().st_ino) for name, path in (("upload", upload), ("changes", upload / "changes"))}

        def record_rename(src, dst, *args, **kwargs):
            result = real_rename(src, dst, *args, **kwargs)
            events.append(("rename", Path(os.fspath(dst)).name, (os.fstat(kwargs["dst_dir_fd"]).st_dev, os.fstat(kwargs["dst_dir_fd"]).st_ino)))
            return result

        def record_fsync(descriptor):
            info = os.fstat(descriptor)
            events.append(("fsync", (info.st_dev, info.st_ino)))
            return real_fsync(descriptor)

        def record_link(src, dst, *args, **kwargs):
            events.append(("link", Path(os.fspath(dst)).name, (os.fstat(kwargs["dst_dir_fd"]).st_dev, os.fstat(kwargs["dst_dir_fd"]).st_ino)))
            return real_link(src, dst, *args, **kwargs)

        with patch.object(render_assets.os, "rename", side_effect=record_rename), patch.object(render_assets.os, "fsync", side_effect=record_fsync), patch.object(render_assets.os, "link", side_effect=record_link):
            self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        prior = next(index for index, event in enumerate(events) if event[:2] == ("rename", "prior.json"))
        publication = next(index for index, event in enumerate(events[prior + 1 :], prior + 1) if event[0] == "link" and event[1] == "head.json")
        archive_identity = events[prior][2]
        between = events[prior + 1 : publication]
        self.assertIn(("fsync", identities["upload"]), between)
        self.assertIn(("fsync", archive_identity), between)
        candidate = next(index for index, event in enumerate(events[publication + 1 :], publication + 1) if event[:2] == ("rename", "candidate.json"))
        marker = next(index for index, event in enumerate(events[candidate + 1 :], candidate + 1) if event[:2] == ("rename", "marker.json"))
        self.assertIn(("fsync", identities["changes"]), events[candidate + 1 : marker])
        self.assertIn(("fsync", archive_identity), events[candidate + 1 : marker])
        self.assertIn(("fsync", identities["changes"]), events[marker + 1 :])
        self.assertIn(("fsync", archive_identity), events[marker + 1 :])

    def test_unknown_change_and_protection_evidence_is_never_silently_omitted(self) -> None:
        for kind in ("change", "protection"):
            with self.subTest(kind=kind):
                root = self.sandbox / f"unknown-{kind}-evidence"
                store = self.open_store(root)
                data = kind.encode()
                asset = self.upload_with(store, "owner", data)
                upload_id = read_json(root / "index" / f"{owner_token('owner')}-{digest(data)}.json")["upload_id"]
                upload = root / "uploads" / upload_id
                if kind == "change":
                    unknown = upload / "changes" / "unknown-change-evidence.json"
                    unknown.write_bytes(b"retained unknown change")
                else:
                    store.acquire_reference("owner", asset["asset_handle"], "must-remain-protected")
                    active = next((upload / "protections").glob("*.json"))
                    unknown = active.with_name("unknown-protection-evidence")
                    active.rename(unknown)
                with self.assertRaises(AssetRecoveryRequiredError):
                    if kind == "change":
                        store.upload_status("owner", upload_id)
                    else:
                        store.active_protection("owner")
                self.assertTrue(unknown.exists())

    def test_disk_admission_accounts_metadata_without_cleanup(self) -> None:
        data = b"metadata admission" * 100
        self.free.value = 2 * MiB + len(data) * 2
        with self.assertRaises(AssetCapacityError):
            self.store.begin_upload("owner", digest(data), len(data))
        self.free.value = 512 * MiB
        begun = self.store.begin_upload("owner", digest(data), len(data))
        self.store.append_chunk("owner", begun["upload_id"], 0, io.BytesIO(data), len(data))
        self.free.value = 2 * MiB + len(data)
        with (
            patch.object(render_assets.os, "unlink", side_effect=AssertionError("cleanup forbidden")),
            patch.object(render_assets.os, "remove", side_effect=AssertionError("cleanup forbidden")),
            patch.object(render_assets.os, "rmdir", side_effect=AssertionError("cleanup forbidden")),
        ):
            with self.assertRaises(AssetCapacityError):
                self.store.finish_upload("owner", begun["upload_id"])
        self.assertEqual(self.store.upload_status("owner", begun["upload_id"])["committed_offset"], len(data))


if __name__ == "__main__":
    unittest.main()
