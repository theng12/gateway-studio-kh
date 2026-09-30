"""Delta release push: manifest first, only the needed blobs, legacy fallback, concurrency.

A Python controller stand-in speaks the wire contract of Hub 0.6.115
(`_orchestra/audit-20260930/CONTRACT-hub-release-delta.md`) in three flavours: `delta`
(has the capability), `legacy` (no `delta` key, wants `artifact_base64`) and `noroutes`
(advertises `delta` but answers 404 to the manifest push). The DMG is replaced by a
small fake app tree; `rp._mounted` is what tests inject a directory into.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import fleet, release_publisher as rp  # noqa: E402

SCHEMA = "studiohub.file-manifest.v1"


def build_app(root: Path, files: dict[str, bytes]) -> Path:
    """A fake `Studio Hub.app` with a real `file-manifest/`, built the contract's way."""
    app = root / "Studio Hub.app"
    groups: dict[str, list[tuple[str, bytes]]] = {}
    for rel, data in files.items():
        target = app / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        prefix = "Contents/Resources/runtimes/"
        name = "runtime-" + rel[len(prefix):].split("/")[0] if rel.startswith(prefix) else "core"
        groups.setdefault(name, []).append((rel, data))
    directory = app / "Contents/Resources/file-manifest"
    directory.mkdir(parents=True, exist_ok=True)
    index_groups = {}
    for name, rows in groups.items():
        text = "".join(
            f"f\t0\t{len(data)}\t{hashlib.sha256(data).hexdigest()}\t{rel}\n"
            for rel, data in sorted(rows, key=lambda row: row[0].encode())
        ).encode()
        (directory / f"{name}.tsv.gz").write_bytes(gzip.compress(text, mtime=0))
        index_groups[name] = {"sha256": hashlib.sha256(text).hexdigest(), "files": len(rows),
                              "bytes": sum(len(d) for _, d in rows), "prefix": ""}
    committed = "".join(f"{n}\0{index_groups[n]['sha256']}\n" for n in sorted(index_groups))
    (directory / "index.json").write_text(json.dumps({
        "schema": SCHEMA, "files_sha256": hashlib.sha256(committed.encode()).hexdigest(),
        "files_count": len(files), "files_bytes": sum(len(d) for d in files.values()),
        "groups": index_groups,
    }))
    return root


class Controller:
    """One controller stand-in. `held` mirrors what `GET /api/hub/release` returns."""

    def __init__(self, mode: str = "delta", preheld: set[str] | None = None,
                 barrier: threading.Barrier | None = None, break_post: bool = False,
                 drop: str | None = None, drop_first_puts: int = 0) -> None:
        self.mode, self.preheld, self.barrier, self.break_post = mode, set(preheld or ()), barrier, break_post
        # `drop`: hang up on the POST without answering, "before_store" or "after_store".
        self.drop, self.drop_puts = drop, drop_first_puts
        self.held: dict | None = None
        self.posts: list[dict] = []
        self.puts: list[str] = []
        self.blobs: dict[str, bytes] = {}
        self.manifest: dict | None = None
        me = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _need(self):
                have = me.preheld | set(me.blobs)
                return sorted({d for d in me.needed_all() if d not in have})

            def do_GET(self):
                if self.path == "/api/hub/release":
                    body = {"release": me.held, "trusted_publisher": None}
                    if me.mode != "legacy":
                        body["delta"] = {"schema": SCHEMA, "manifest": me.manifest is not None}
                    self._send(200, body)
                elif self.path == "/api/hub/release/need" and me.manifest:
                    self._send(200, {"need": self._need()})
                else:
                    self._send(404, {"detail": "no such route"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.posts.append(body)
                if me.barrier is not None:
                    me.barrier.wait(timeout=8)  # only passes if every controller is in flight at once
                if me.break_post:
                    return self._send(500, {"detail": "boom"})
                if me.drop == "before_store":
                    return  # closes the connection with no response, nothing stored
                if "file_manifest" in body:
                    if me.mode == "noroutes":
                        return self._send(404, {"detail": "no such route"})
                    me.manifest = body["file_manifest"]
                    me.held = {**body["descriptor"], "controller_key_id": "c" * 64}
                    if me.drop == "after_store":
                        return
                    return self._send(200, {"descriptor": me.held, "need": self._need()})
                if "artifact_base64" not in body:
                    return self._send(400, {"detail": "artifact_base64 is required"})
                me.held = dict(body["descriptor"])
                if me.drop == "after_store":
                    return
                self._send(200, me.held)

            def do_PUT(self):
                digest = self.path.rsplit("/", 1)[1]
                assert "chunked" not in self.headers.get("Transfer-Encoding", "")
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if me.drop_puts > 0:
                    me.drop_puts -= 1
                    return  # a lost reply on an idempotent request
                if hashlib.sha256(data).hexdigest() != digest:
                    return self._send(400, {"detail": "sha256 mismatch"})
                me.puts.append(digest)
                me.blobs[digest] = data
                self._send(200, {"ok": True, "stored": digest, "need": len(self._need())})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def needed_all(self) -> set[str]:
        lines = []
        for raw in (self.manifest or {}).get("groups", {}).values():
            lines += gzip.decompress(base64.b64decode(raw)).decode().splitlines()
        return {line.split("\t")[3] for line in lines if line.startswith("f")}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


FILES = {
    "Contents/MacOS/hub": b"main binary " * 100,
    "Contents/Resources/a.txt": b"alpha",
    "Contents/Resources/same-as-a.txt": b"alpha",  # same bytes, second path: one blob
    "Contents/Resources/runtimes/voice/model.bin": b"weights " * 500,
    "Contents/Resources/runtimes/voice/lib.py": b"print('x')\n",
}
sha = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731


class ReleaseDelta(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-delta-")
        self.root = Path(self.tmp.name)
        self.tree = build_app(self.root / "dmg", FILES)
        self.dmg = self.root / "StudioHub-0.6.116-abc.dmg"
        self.dmg.write_bytes(b"pretend disk image " * 1000)
        self.controllers: list[Controller] = []
        self.mounts = {"entered": 0, "exited": 0}
        tree, mounts = self.tree, self.mounts

        @contextmanager
        def fake_mount(_dmg):
            mounts["entered"] += 1
            try:
                yield tree
            finally:
                mounts["exited"] += 1

        self.patches = [
            patch.object(rp, "key_path", lambda: self.root / "release-publisher.pem"),
            patch.object(rp, "_state_path", lambda: self.root / "release-state.json"),
            patch.object(rp, "_mounted", fake_mount),
        ]
        for started in self.patches:
            started.start()
        rp.forget_identity()

    def tearDown(self) -> None:
        rp.forget_identity()
        for started in self.patches:
            started.stop()
        for controller in self.controllers:
            controller.close()
        self.tmp.cleanup()

    def controller(self, **kwargs) -> Controller:
        made = Controller(**kwargs)
        self.controllers.append(made)
        return made

    def publish(self, *targets: Controller) -> dict:
        rows = [{"id": f"c{i}", "label": f"C{i}", "url": t.base} for i, t in enumerate(targets)]
        with patch.object(fleet, "_read", return_value=rows), \
             patch.object(fleet, "_token_for", return_value="tok"):
            return rp.publish(artifact_path=self.dmg, version="0.6.116", build="abc")

    # -- the delta path ------------------------------------------------------

    def test_only_the_needed_blobs_are_sent_and_the_release_is_read_back(self) -> None:
        # The controller already runs most of this build: it holds the binary and a.txt.
        c = self.controller(preheld={sha(FILES["Contents/MacOS/hub"]), sha(b"alpha")})
        state = self.publish(c)
        outcome = state["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["mode"], "delta")
        want = {sha(FILES["Contents/Resources/runtimes/voice/model.bin"]),
                sha(FILES["Contents/Resources/runtimes/voice/lib.py"])}
        self.assertEqual(set(c.puts), want)
        self.assertEqual(len(c.puts), 2)
        self.assertEqual(outcome["blobs_sent"], 2)
        self.assertEqual(c.blobs[sha(FILES["Contents/Resources/runtimes/voice/lib.py"])],
                         FILES["Contents/Resources/runtimes/voice/lib.py"])
        # No image went over the wire, only descriptor + manifest.
        self.assertNotIn("artifact_base64", c.posts[0])
        # Same bytes at two paths are one blob when nothing is held.
        c2 = self.controller()
        self.assertTrue(self.publish(c2)["locations"]["c0"]["ok"])
        self.assertEqual(len(c2.puts), len(set(c2.puts)))
        self.assertEqual(len(c2.puts), 4)  # 5 files, 4 distinct blobs
        self.assertEqual(self.mounts, {"entered": 2, "exited": 2})

    def test_the_descriptor_carries_the_files_fields_inside_the_signature(self) -> None:
        c = self.controller()
        state = self.publish(c)
        descriptor = c.posts[0]["descriptor"]
        index = c.posts[0]["file_manifest"]["index"]
        self.assertEqual(descriptor["files_sha256"], index["files_sha256"])
        self.assertEqual(descriptor["files_count"], 5)
        self.assertEqual(descriptor["files_bytes"], sum(len(v) for v in FILES.values()))
        # sha256/bytes keep meaning the disk image, not the app tree.
        self.assertEqual(descriptor["sha256"], sha(self.dmg.read_bytes()))
        self.assertEqual(descriptor["bytes"], self.dmg.stat().st_size)
        self.assertEqual(state["release"]["files_sha256"], index["files_sha256"])
        # The signature covers the files fields: verify it in Node against our public key.
        key = bytes.fromhex("302a300506032b6570032100") + bytes.fromhex(rp.identity()["public_key_hex"])
        signed = rp.canonical_bytes({k: v for k, v in descriptor.items() if k != "signature"})
        verify = ("const c=require('crypto');const [k,m,s]=process.argv.slice(1);"
                  "process.stdout.write(String(c.verify(null,Buffer.from(m,'base64'),"
                  "{key:Buffer.from(k,'base64'),format:'der',type:'spki'},Buffer.from(s,'base64'))))")
        answer = subprocess.run(
            ["node", "-e", verify, base64.b64encode(key).decode(),
             base64.b64encode(signed).decode(), descriptor["signature"]],
            capture_output=True, text=True,
        ).stdout
        self.assertEqual(answer, "true")

    def test_an_incomplete_push_is_not_reported_as_ok(self) -> None:
        # A controller that answers every PUT 200 but still lists a blob as needed.
        c = self.controller()
        with patch.object(Controller, "needed_all", lambda self: {"f" * 64} | set(self.blobs)):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "RELEASE_NEED_UNKNOWN")

    def test_blobs_still_needed_after_the_last_put_fail_the_push(self) -> None:
        c = self.controller()
        # the controller "loses" what it stored: need never empties
        original_put = rp.httpx.Client.put

        def put_then_forget(client, url, **kw):
            response = original_put(client, url, **kw)
            c.blobs.pop(url.rsplit("/", 1)[1], None)
            return response

        with patch.object(rp.httpx.Client, "put", put_then_forget):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "RELEASE_INCOMPLETE")

    # -- a transport error is not a verdict ----------------------------------------

    def test_a_dropped_reply_after_the_release_was_stored_is_reported_ok(self) -> None:
        # The live pilot: the release was stored, the reply never arrived.
        everything = {sha(data) for data in FILES.values()}
        c = self.controller(preheld=everything, drop="after_store")
        with patch.object(rp, "_RECOVER_SECONDS", 3.0), patch.object(rp, "_RECOVER_INTERVAL", 0.1):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["release"]["version"], "0.6.116")
        self.assertIn("reply was lost", outcome["note"])
        self.assertEqual((outcome["mode"], outcome["blobs_sent"], outcome["bytes_sent"]), ("delta", 0, 0))
        self.assertIn("recover", outcome["timings"])

    def test_a_dropped_reply_before_anything_was_stored_is_the_real_failure(self) -> None:
        c = self.controller(drop="before_store")
        c.held = {"version": "0.6.115", "signature": "the-older-release"}  # holds something, not this
        with patch.object(rp, "_RECOVER_SECONDS", 0.5), patch.object(rp, "_RECOVER_INTERVAL", 0.1):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "RELEASE_PUSH_FAILED")
        self.assertIn("holds 0.6.115, not this release", outcome["detail"])
        self.assertEqual((outcome["mode"], outcome["blobs_sent"], outcome["bytes_sent"]), ("delta", 0, 0))
        self.assertEqual(c.held["version"], "0.6.115")

    def test_a_stored_release_that_still_lacks_blobs_is_not_reported_ok(self) -> None:
        c = self.controller(drop="after_store")  # holds the descriptor, none of the bytes
        with patch.object(rp, "_RECOVER_SECONDS", 3.0), patch.object(rp, "_RECOVER_INTERVAL", 0.1):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["code"], "RELEASE_INCOMPLETE")

    def test_a_lost_reply_on_a_blob_put_is_repeated(self) -> None:
        c = self.controller(drop_first_puts=2)
        with patch.object(rp.time, "sleep", lambda _s: None):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["blobs_sent"], 4)

    def test_a_full_push_whose_reply_was_lost_is_reported_ok(self) -> None:
        c = self.controller(mode="legacy", drop="after_store")
        with patch.object(rp, "_RECOVER_SECONDS", 3.0), patch.object(rp, "_RECOVER_INTERVAL", 0.1):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["mode"], "full")

    def test_every_outcome_says_mode_and_what_was_sent_and_delta_says_how_long(self) -> None:
        state = self.publish(self.controller(), self.controller(break_post=True), self.controller(mode="legacy"))
        for cid, outcome in state["locations"].items():
            self.assertTrue({"mode", "blobs_sent", "bytes_sent"} <= set(outcome), (cid, outcome))
        good, refused = state["locations"]["c0"], state["locations"]["c1"]
        self.assertEqual((refused["mode"], refused["blobs_sent"], refused["bytes_sent"]), ("delta", 0, 0))
        self.assertEqual(good["bytes_sent"], sum(len(d) for d in set(FILES.values())))
        self.assertTrue({"manifest_post", "blob_puts", "need_check", "readback"} <= set(good["timings"]))
        self.assertTrue({"hash_image", "probe", "push"} <= set(state["timings"]))

    # -- fallback ------------------------------------------------------------

    def test_a_controller_without_the_capability_gets_the_full_image(self) -> None:
        c = self.controller(mode="legacy")
        with patch.object(rp, "_mounted", side_effect=AssertionError("nothing to mount for a legacy fleet")):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["mode"], "full")
        body = c.posts[0]
        self.assertNotIn("file_manifest", body)
        self.assertEqual(base64.b64decode(body["artifact_base64"]), self.dmg.read_bytes())
        # A 0.6.114 controller sees exactly the descriptor it always did.
        self.assertFalse({"files_sha256", "files_count", "files_bytes"} & set(body["descriptor"]))
        self.assertEqual(c.puts, [])

    def test_missing_manifest_routes_fall_back_to_the_full_image(self) -> None:
        c = self.controller(mode="noroutes")
        outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["mode"], "full")
        self.assertEqual(len(c.posts), 2)  # the manifest attempt, then the full push
        self.assertEqual(base64.b64decode(c.posts[1]["artifact_base64"]), self.dmg.read_bytes())
        self.assertNotIn("files_sha256", c.posts[1]["descriptor"])

    def test_an_image_with_no_manifest_falls_back_and_says_why(self) -> None:
        (self.tree / "Studio Hub.app/Contents/Resources/file-manifest/index.json").unlink()
        c = self.controller()
        outcome = self.publish(c)["locations"]["c0"]
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["mode"], "full")
        self.assertIn("no file manifest", outcome["note"])
        self.assertEqual(self.mounts["exited"], 1)

    def test_a_tampered_manifest_is_refused_before_any_push(self) -> None:
        core = self.tree / "Studio Hub.app/Contents/Resources/file-manifest/core.tsv.gz"
        core.write_bytes(gzip.compress(b"f\t0\t1\t" + b"0" * 64 + b"\tx\n", mtime=0))
        with self.assertRaises(rp.ReleaseError) as caught:
            rp.load_manifest(self.tree)
        self.assertEqual(caught.exception.code, "RELEASE_MANIFEST_INVALID")

    # -- staged rollout --------------------------------------------------------
    def test_controller_ids_offers_the_release_to_only_the_named_controllers(self) -> None:
        pilot, other = self.controller(), self.controller()
        rows = [{"id": f"c{i}", "label": f"c{i}", "url": c.base} for i, c in enumerate((pilot, other))]
        with patch.object(fleet, "_read", return_value=rows), \
             patch.object(fleet, "_token_for", return_value="tok"):
            state = rp.publish(artifact_path=self.dmg, version="0.6.116", build="abc", controller_ids=["c0"])
        self.assertEqual(set(state["locations"]), {"c0"})
        self.assertTrue(state["locations"]["c0"]["ok"])
        self.assertEqual((other.posts, other.puts, other.held), ([], [], None), "a controller not named must not be offered it")

    def test_an_unknown_controller_id_is_refused_before_anything_is_sent(self) -> None:
        pilot = self.controller()
        with patch.object(fleet, "_read", return_value=[{"id": "c0", "label": "c0", "url": pilot.base}]), \
             patch.object(fleet, "_token_for", return_value="tok"):
            with self.assertRaises(rp.ReleaseError) as caught:
                rp.publish(artifact_path=self.dmg, version="0.6.116", build="abc", controller_ids=["nope"])
        self.assertEqual(caught.exception.code, "RELEASE_UNKNOWN_CONTROLLER")
        self.assertEqual((pilot.posts, pilot.puts), ([], []))

    # -- concurrency ---------------------------------------------------------

    def test_controllers_are_pushed_at_the_same_time(self) -> None:
        # Each controller's POST blocks until all three are in it; a serial push times out.
        barrier = threading.Barrier(3)
        targets = [self.controller(barrier=barrier) for _ in range(3)]
        state = self.publish(*targets)
        self.assertTrue(all(o["ok"] for o in state["locations"].values()), state["locations"])
        for c in targets:
            self.assertEqual(len(c.puts), 4)

    def test_one_failing_controller_does_not_stop_the_others(self) -> None:
        bad, good, legacy = self.controller(break_post=True), self.controller(), self.controller(mode="legacy")
        state = self.publish(bad, good, legacy)["locations"]
        self.assertFalse(state["c0"]["ok"])
        self.assertEqual(state["c0"]["status"], 500)
        self.assertTrue(state["c1"]["ok"] and state["c2"]["ok"], state)
        self.assertEqual({state["c1"]["mode"], state["c2"]["mode"]}, {"delta", "full"})
        self.assertEqual(self.mounts, {"entered": 1, "exited": 1})  # mounted once for the fleet

    def test_an_unreachable_controller_is_its_own_outcome(self) -> None:
        good = self.controller()
        dead = self.controller()
        dead.close()
        state = self.publish(dead, good)["locations"]
        self.assertEqual(state["c0"]["code"], "LOCATION_UNREACHABLE")
        self.assertTrue(state["c1"]["ok"])

    # -- the mount is always undone -------------------------------------------

    def test_the_image_is_detached_when_the_push_blows_up(self) -> None:
        c = self.controller()
        with patch.object(rp, "push_delta", side_effect=RuntimeError("disk full")):
            outcome = self.publish(c)["locations"]["c0"]
        self.assertFalse(outcome["ok"])
        self.assertIn("disk full", outcome["detail"])
        self.assertEqual(self.mounts, {"entered": 1, "exited": 1})

    def test_hdiutil_attach_is_read_only_and_detach_always_runs(self) -> None:
        for patch_p in self.patches:
            patch_p.stop()
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        try:
            with patch.object(rp.subprocess, "run", fake_run):
                with self.assertRaises(KeyError):
                    with rp._mounted(self.dmg):
                        raise KeyError("boom")
                self.assertEqual(calls[0][:5], ["hdiutil", "attach", "-readonly", "-nobrowse", "-noverify"])
                self.assertEqual(calls[-1][:2], ["hdiutil", "detach"])
                calls.clear()
                # attach itself failing must still try to detach
                failing = lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 1, "", "no"))[1]  # noqa: E731
                with patch.object(rp.subprocess, "run", failing):
                    with self.assertRaises(rp.ReleaseError):
                        with rp._mounted(self.dmg):
                            pass
                self.assertEqual(calls[-1][:2], ["hdiutil", "detach"])
        finally:
            for patch_p in self.patches:
                patch_p.start()


if __name__ == "__main__":
    unittest.main()
