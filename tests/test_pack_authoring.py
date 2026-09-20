"""Private Topic Pack draft persistence and Story bridge boundary checks."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_IMPORT_TEMP = tempfile.TemporaryDirectory(prefix="gateway-authoring-import-")
_IMPORT_SUPPORT = Path(_IMPORT_TEMP.name)
ROOT = Path(__file__).resolve().parents[1]
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(_IMPORT_SUPPORT),
    "GATEWAY_DATA": str(_IMPORT_SUPPORT / "data"),
    "GATEWAY_ENV_FILE": str(_IMPORT_SUPPORT / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(_IMPORT_SUPPORT / "no-hub.json"),
})
sys.path.insert(0, str(ROOT))

from gateway import pack_authoring


KEY = "com.fixture.topic-pack@1.0.0"
RESERVATION = {
    "key": KEY,
    "item_id": "com.fixture.topic-pack.item",
    "pack_id": "com.fixture.topic-pack",
    "version": "1.0.0",
    "receipt_ref": "receipt-fixture-1",
}


class StoryProcess:
    """A subprocess boundary double; draft files remain real filesystem state."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.archive = b"fixture candidate bytes"
        self.bad_digest = False
        self.build_error = False
        self.omit_identity = False

    def run(self, argv, input_bytes, env, *, timeout, output_limit):
        self.calls.append({
            "argv": list(argv), "input": input_bytes, "env": env,
            "timeout": timeout, "output_limit": output_limit,
        })
        request = json.loads(input_bytes)
        action = request["action"]
        if action == "catalogue":
            result = {
                "registry_revision": "registry-fixture-1",
                "reservations": [RESERVATION],
                "templates": [{"key": "fixture-template", "name": "Fixture"}],
            }
        elif action == "reservation":
            result = RESERVATION
        elif action == "template":
            result = {"document": {"kind": "topic-pack", "schema_version": 1}}
        elif action == "validate":
            result = {
                "document": request["document"],
                "manifest": {
                    "pack_id": RESERVATION["pack_id"],
                    "version": RESERVATION["version"],
                },
            }
        elif action == "build":
            if self.build_error:
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps({"error": "Reservation receipt is withdrawn."}).encode(),
                    stderr=b"private tool detail",
                )
            output = Path(argv[-1])
            if output.exists():
                return SimpleNamespace(
                    returncode=1,
                    stdout=json.dumps({"error": "build output path already exists"}).encode(),
                    stderr=b"private tool detail",
                )
            output.write_bytes(self.archive)
            result = {
                "sha256": "0" * 64 if self.bad_digest else hashlib.sha256(self.archive).hexdigest(),
                "bytes": len(self.archive),
            }
            if not self.omit_identity:
                result.update(pack_id=RESERVATION["pack_id"], version=RESERVATION["version"])
        else:
            return SimpleNamespace(
                returncode=1,
                stdout=json.dumps({"error": "Unsupported fixture action."}).encode(),
                stderr=b"private tool detail",
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode(), stderr=b"")


class PackAuthoring(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gateway-authoring-")
        self.data = Path(self.temporary.name) / "data"
        self.data.mkdir()
        self.data_patch = patch.object(pack_authoring, "DATA_DIR", self.data)
        self.data_patch.start()
        self.story = StoryProcess()

        def setting(key: str, default: str = "") -> str:
            return {
                "GATEWAY_AUTHORING_NODE": "/fixture/node",
                "GATEWAY_STORY_SOURCE": "/fixture/story",
            }.get(key, default)

        self.settings_patch = patch.object(pack_authoring.config, "get", side_effect=setting)
        self.settings_patch.start()
        self.real_run_process = pack_authoring._run_process
        self.subprocess_patch = patch.object(
            pack_authoring, "_run_process", side_effect=self.story.run
        )
        self.subprocess_patch.start()

    def tearDown(self) -> None:
        self.subprocess_patch.stop()
        self.settings_patch.stop()
        self.data_patch.stop()
        self.temporary.cleanup()

    def root(self) -> Path:
        return self.data / "content-authoring"

    def candidate(self, draft: dict) -> Path:
        return self.root() / draft["id"] / "candidate.sspack"

    def candidate_metadata(self, draft: dict) -> Path:
        return self.root() / draft["id"] / "candidate.json"

    def test_missing_tools_reports_prerequisite_but_saved_drafts_stay_readable(self):
        draft = pack_authoring.create_draft(KEY, "{unfinished")
        with patch.object(pack_authoring, "_run_process", side_effect=FileNotFoundError()):
            catalogue = pack_authoring.catalogue()
            self.assertFalse(catalogue["available"])
            self.assertIn("unavailable", catalogue["detail"].lower())
            self.assertEqual(pack_authoring.list_drafts(), [draft])
            self.assertEqual(pack_authoring.read_draft(draft["id"]), draft)
            with self.assertRaises(pack_authoring.AuthoringError) as caught:
                pack_authoring.template("fixture-template")
        self.assertEqual(caught.exception.status, 503)

    def test_create_is_idempotent_and_persists_raw_invalid_text_with_private_modes(self):
        raw = '{"kind":"topic-pack",'
        first = pack_authoring.create_draft(KEY, raw)
        second = pack_authoring.create_draft(KEY, '{"different":true}')
        self.assertEqual(first, second)
        self.assertEqual(first["document_text"], raw)
        self.assertEqual(
            set(first),
            {"id", "key", "receipt_ref", "pack_id", "version", "item_id", "document_text", "revision", "sealed"},
        )
        self.assertEqual(first["revision"], 1)
        self.assertFalse(first["sealed"])
        self.assertEqual(stat.S_IMODE(self.root().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root() / first["id"]).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root() / first["id"] / "draft.json").stat().st_mode), 0o600)
        self.assertEqual(pack_authoring.list_drafts(), [first])

    def test_save_uses_revision_cas_and_serializes_concurrent_edits(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        saved = pack_authoring.save_draft(draft["id"], '{"winner":1}', 1)
        self.assertEqual(saved["revision"], 2)
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            pack_authoring.save_draft(draft["id"], '{"stale":true}', 1)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(pack_authoring.read_draft(draft["id"]), saved)

        barrier = threading.Barrier(2)

        def edit(text: str):
            barrier.wait(timeout=5)
            try:
                return ("ok", pack_authoring.save_draft(draft["id"], text, 2))
            except pack_authoring.AuthoringError as exc:
                return ("error", exc.status)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, ('{"a":1}', '{"b":2}')))
        self.assertEqual(sorted(result[0] for result in results), ["error", "ok"])
        current = pack_authoring.read_draft(draft["id"])
        self.assertEqual(current["revision"], 3)
        self.assertIn(current["document_text"], ('{"a":1}', '{"b":2}'))

    def test_traversal_and_symlink_paths_are_rejected_without_following_them(self):
        with self.assertRaises(pack_authoring.AuthoringError) as traversal:
            pack_authoring.read_draft("../escape")
        self.assertEqual(traversal.exception.status, 422)

        root = self.root()
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(pack_authoring.AuthoringError) as root_link:
            pack_authoring.list_drafts()
        self.assertEqual(root_link.exception.status, 422)
        root.unlink()

        root.mkdir(mode=0o700)
        (root / ("a" * 64)).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(pack_authoring.AuthoringError) as draft_link:
            pack_authoring.read_draft("a" * 64)
        self.assertEqual(draft_link.exception.status, 422)

    def test_preview_strictly_parses_but_does_not_discard_invalid_saved_text(self):
        draft = pack_authoring.create_draft(KEY, '{"a":1,"a":2}')
        before = pack_authoring.read_draft(draft["id"])
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            pack_authoring.preview_draft(draft["id"], 1)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(pack_authoring.read_draft(draft["id"]), before)

    def test_preview_and_build_send_the_saved_reservation_identity(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        expected = {name: draft[name] for name in ("key", "item_id", "pack_id", "version", "receipt_ref")}
        pack_authoring.preview_draft(draft["id"], 1)
        preview_request = json.loads(self.story.calls[-1]["input"])
        self.assertEqual(preview_request["expected_reservation"], expected)
        pack_authoring.export_draft(draft["id"], 1)
        build_request = json.loads(self.story.calls[-1]["input"])
        self.assertEqual(build_request["expected_reservation"], expected)

    def test_failed_build_preserves_draft_and_does_not_leave_candidate(self):
        draft = pack_authoring.create_draft(KEY, '{"kind":"topic-pack"}')
        self.story.bad_digest = True
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(caught.exception.status, 422)
        self.assertFalse(self.candidate(draft).exists())
        self.assertEqual(pack_authoring.read_draft(draft["id"]), draft)

        self.story.bad_digest = False
        exported, metadata = pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(exported, self.story.archive)
        self.assertEqual(metadata["sha256"], hashlib.sha256(self.story.archive).hexdigest())

    def test_build_result_requires_exact_pack_identity_fields(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        self.story.omit_identity = True
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(caught.exception.status, 422)
        self.assertFalse(self.candidate(draft).exists())

    def test_authority_failure_preserves_draft(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        self.story.build_error = True
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(caught.exception.status, 422)
        self.assertFalse(self.candidate(draft).exists())
        self.assertEqual(pack_authoring.read_draft(draft["id"]), draft)

    def test_export_freezes_exact_bytes_across_retry_restart_and_sha_mismatch(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        expected, metadata = pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(pack_authoring.read_draft(draft["id"])["sealed"], True)
        calls = len(self.story.calls)
        with patch.object(pack_authoring, "_run_process", side_effect=AssertionError("retry rebuilt")):
            self.assertEqual(pack_authoring.export_draft(draft["id"], 1), (expected, metadata))
        self.assertEqual(len(self.story.calls), calls)

        original_config_data = pack_authoring.config.DATA_DIR
        pack_authoring.config.DATA_DIR = self.data
        try:
            importlib.reload(pack_authoring)
            with patch.object(pack_authoring, "_run_process", side_effect=AssertionError("restart rebuilt")):
                self.assertEqual(pack_authoring.read_draft(draft["id"])["sealed"], True)
                self.assertEqual(pack_authoring.export_draft(draft["id"], 1), (expected, metadata))
        finally:
            pack_authoring.config.DATA_DIR = original_config_data

        self.candidate(draft).write_bytes(b"tampered")
        with self.assertRaises(pack_authoring.AuthoringError) as mismatch:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(mismatch.exception.status, 410)
        with self.assertRaises(pack_authoring.AuthoringError) as sealed:
            pack_authoring.save_draft(draft["id"], "{}", 1)
        self.assertEqual(sealed.exception.status, 409)

    def test_candidate_without_metadata_freezes_but_never_authorizes_bytes(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        candidate = self.candidate(draft)
        candidate.write_bytes(b"first arbitrary bytes")
        self.assertTrue(pack_authoring.read_draft(draft["id"])["sealed"])
        with self.assertRaises(pack_authoring.AuthoringError) as missing:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(missing.exception.status, 410)
        self.assertIn("metadata", missing.exception.message.lower())
        candidate.write_bytes(b"different arbitrary bytes")
        with self.assertRaises(pack_authoring.AuthoringError) as changed:
            pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(changed.exception.status, 410)
        self.assertFalse(self.candidate_metadata(draft).exists())

    def test_installed_candidate_is_rechecked_against_metadata_before_sealing(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        install = pack_authoring._install_candidate

        def replace_after_install(temporary: Path, candidate: Path) -> None:
            install(temporary, candidate)
            candidate.write_bytes(b"different bytes after install")

        with patch.object(pack_authoring, "_install_candidate", side_effect=replace_after_install):
            with self.assertRaises(pack_authoring.AuthoringError) as caught:
                pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(caught.exception.status, 410)
        self.assertTrue(pack_authoring.read_draft(draft["id"])["sealed"])

    def test_exact_candidate_recovery_persists_the_raw_draft_seal(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        expected = pack_authoring.export_draft(draft["id"], 1)
        draft_path = self.root() / draft["id"] / "draft.json"
        raw = json.loads(draft_path.read_text())
        raw["sealed"] = False
        draft_path.write_text(json.dumps(raw))

        with patch.object(pack_authoring, "_run_process", side_effect=AssertionError("recovery rebuilt")):
            self.assertEqual(pack_authoring.export_draft(draft["id"], 1), expected)
        self.assertTrue(json.loads(draft_path.read_text())["sealed"])

    def test_metadata_without_candidate_freezes_and_fails_closed(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        self.candidate_metadata(draft).write_text(json.dumps({
            "sha256": hashlib.sha256(b"lost bytes").hexdigest(),
            "bytes": len(b"lost bytes"),
            "pack_id": draft["pack_id"],
            "version": draft["version"],
        }))
        self.assertTrue(pack_authoring.read_draft(draft["id"])["sealed"])
        with patch.object(pack_authoring, "_run_process", side_effect=AssertionError("must not rebuild")):
            with self.assertRaises(pack_authoring.AuthoringError) as missing:
                pack_authoring.export_draft(draft["id"], 1)
        self.assertEqual(missing.exception.status, 410)
        self.assertIn("bytes", missing.exception.message.lower())

    def test_persisted_reads_do_not_use_unbounded_path_read_bytes(self):
        draft = pack_authoring.create_draft(KEY, "{}")
        expected = pack_authoring.export_draft(draft["id"], 1)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            self.assertTrue(pack_authoring.read_draft(draft["id"])["sealed"])
            self.assertEqual(pack_authoring.export_draft(draft["id"], 1), expected)

    def test_fifo_inputs_are_rejected_without_blocking(self):
        child = r'''import os
import sys
from pathlib import Path

action, raw_path, raw_support, root = sys.argv[1:]
path = Path(raw_path)
support = Path(raw_support)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(support),
    "GATEWAY_DATA": str(support / "data"),
    "GATEWAY_ENV_FILE": str(support / ".env"),
    "GATEWAY_NO_MIGRATE": "1",
    "STUDIOHUB_READY_FILE": str(support / "no-hub.json"),
})
sys.path.insert(0, root)
from gateway import pack_authoring

try:
    if action == "read":
        pack_authoring._read_bounded_file(
            path, 8, missing="missing", unreadable="unreadable",
            unsafe="unsafe", too_large="too large", status=503,
        )
    else:
        pack_authoring._sync_file(path)
except pack_authoring.AuthoringError as exc:
    if exc.status != 503:
        raise
else:
    raise SystemExit("FIFO was accepted")
'''
        for action in ("read", "sync"):
            with self.subTest(action=action):
                fifo = Path(self.temporary.name) / f"{action}.fifo"
                support = Path(self.temporary.name) / f"{action}-support"
                support.mkdir()
                os.mkfifo(fifo)
                try:
                    completed = subprocess.run(
                        [sys.executable, "-c", child, action, str(fifo), str(support), str(ROOT)],
                        cwd=ROOT, capture_output=True, text=True, timeout=1.5,
                    )
                except subprocess.TimeoutExpired:
                    self.fail(f"{action} helper blocked on a FIFO")
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_directory_sync_failures_are_not_ignored(self):
        with patch.object(pack_authoring.os, "open", side_effect=OSError("fixture sync failure")):
            with self.assertRaises(pack_authoring.AuthoringError) as caught:
                pack_authoring._sync_directory(self.data)
        self.assertEqual(caught.exception.status, 503)

    def test_wrapper_uses_fixed_argv_bounded_timeout_and_sanitized_environment(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "must-not-cross",
                "AWS_SECRET_ACCESS_KEY": "must-not-cross",
                "GATEWAY_API_KEY": "must-not-cross",
                "PATH": "/fixture/path",
            },
        ):
            result = pack_authoring.catalogue()
        self.assertTrue(result["available"])
        call = self.story.calls[-1]
        self.assertEqual(call["argv"], [
            "/fixture/node",
            str(Path(pack_authoring.__file__).with_name("story_authoring.mjs")),
            "/fixture/story",
        ])
        self.assertGreater(call["timeout"], 0)
        self.assertGreater(call["output_limit"], 0)
        self.assertEqual(call["env"]["PATH"], "/fixture/path")
        self.assertNotIn("OPENAI_API_KEY", call["env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", call["env"])
        self.assertNotIn("GATEWAY_API_KEY", call["env"])

    def test_document_bytes_are_bounded_and_nonobjects_are_not_validated(self):
        with self.assertRaises(pack_authoring.AuthoringError) as too_large:
            pack_authoring.create_draft(KEY, "x" * (4 * 1024**2 + 1))
        self.assertEqual(too_large.exception.status, 413)
        draft = pack_authoring.create_draft(KEY, "[]")
        with self.assertRaises(pack_authoring.AuthoringError) as non_object:
            pack_authoring.preview_draft(draft["id"], 1)
        self.assertEqual(non_object.exception.status, 422)

    def test_record_bound_preserves_valid_document_text_after_json_escaping(self):
        raw = "\0" * 750_000
        draft = pack_authoring.create_draft(KEY, raw)
        self.assertEqual(pack_authoring.read_draft(draft["id"])["document_text"], raw)

    def test_streaming_subprocess_capture_stops_at_output_limit(self):
        with self.assertRaises(pack_authoring.AuthoringError) as caught:
            self.real_run_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
                b"",
                {},
                timeout=5,
                output_limit=1024,
            )
        self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
