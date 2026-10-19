#!/usr/bin/env python3
"""Story Studio releases: publish (version read from the bundle), serve, check-in, "Update now".

The signature is verified in Node, the language Story verifies it in, over the same canonical
bytes, so a pass means a Story Mac would accept it — not only that something was signed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-story-releases-")

from fastapi.testclient import TestClient  # noqa: E402

from gateway import api, config, release_publisher, retention, story_releases  # noqa: E402
from gateway.store import JobStore  # noqa: E402

KEY = "story-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}

_VERIFY = r"""
const { createPublicKey, verify } = require('node:crypto')
const { descriptor, key } = JSON.parse(require('node:fs').readFileSync(0, 'utf8'))
const body = Object.fromEntries(Object.keys(descriptor).filter(k => k !== 'signature').sort().map(k => [k, descriptor[k]]))
const spki = Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), Buffer.from(key, 'base64')])
const ok = verify(null, Buffer.from(JSON.stringify(body)), createPublicKey({ key: spki, format: 'der', type: 'spki' }), Buffer.from(descriptor.signature, 'base64'))
process.stdout.write(ok ? 'ok' : 'bad')
"""


def node_verifies(descriptor: dict, key_b64: str) -> bool:
    out = subprocess.run(["node", "-e", _VERIFY], input=json.dumps({"descriptor": descriptor, "key": key_b64}),
                         capture_output=True, text=True, timeout=30)
    return out.stdout == "ok"


def story_zip(path: Path, version: str, build: str = "") -> Path:
    info = plistlib.dumps({"CFBundleShortVersionString": version, "CFBundleVersion": build or version})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Story Studio KH.app/Contents/Info.plist", info)
        archive.writestr("Story Studio KH.app/Contents/MacOS/Story Studio KH", b"#!/bin/sh\n" + version.encode())
    return path


class StoryReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="story-rel-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        data = self.root / "data"
        data.mkdir()
        for target, name, value in ((config, "DATA_DIR", data), (retention, "TRASH_DIR", data / "_trash")):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        recycled = self.root / "macos-trash"
        recycled.mkdir()
        def recycle(path):
            target = recycled / path.name
            count = 0
            while target.exists():
                count += 1
                target = recycled / f"{path.name}.{count}"
            path.rename(target)
            return True
        p = patch.object(retention, "_recycle", side_effect=recycle)
        p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"GATEWAY_API_KEY": KEY})
        p.start()
        self.addCleanup(p.stop)
        release_publisher.forget_identity()
        self.addCleanup(release_publisher.forget_identity)
        self.client = TestClient(api.app, headers=AUTH)

    def publish(self, version: str, name: str | None = None) -> dict:
        artifact = story_zip(self.root / (name or f"Story Studio KH {version}.zip"), version, f"b{version}")
        response = self.client.post("/admin/api/story/release/publish", json={"artifact_path": str(artifact)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["descriptor"]

    def test_publish_reads_the_version_from_the_bundle_and_signs_what_story_verifies(self) -> None:
        descriptor = self.publish("1.4.258")
        self.assertEqual((descriptor["version"], descriptor["build"], descriptor["app"]), ("1.4.258", "b1.4.258", "story"))
        offer = self.client.get("/v1/apps/story/release", headers=AUTH).json()
        key = offer["publisher"]["public_key_base64"]
        self.assertEqual(descriptor["publisher_key_id"], hashlib.sha256(base64.b64decode(key)).hexdigest())
        self.assertTrue(node_verifies(offer["release"], key))
        self.assertFalse(node_verifies({**offer["release"], "version": "9.9.9"}, key))
        body = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        self.assertEqual((hashlib.sha256(body).hexdigest(), len(body)), (descriptor["sha256"], descriptor["bytes"]))

    def test_a_dmg_is_read_through_a_read_only_mount(self) -> None:
        folder = self.root / "dmg-src"
        app = folder / "Story Studio KH.app" / "Contents"
        app.mkdir(parents=True)
        (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.4.260",
                                                         "CFBundleVersion": "1.4.260"}))
        dmg = self.root / "Story Studio KH 1.4.260-arm64.dmg"
        subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(folder), "-format", "UDZO", str(dmg)],
                       check=True, timeout=120)
        self.assertEqual(story_releases.read_versions(dmg), ("1.4.260", "1.4.260"))

    def test_the_release_needs_the_gateway_key(self) -> None:
        self.publish("1.4.258")
        self.assertEqual(TestClient(api.app).get("/v1/apps/story/release").status_code, 401)
        self.assertEqual(self.client.get("/v1/apps/story/release/file",
                                         headers={"Authorization": "Bearer wrong"}).status_code, 403)

    def test_nothing_published_is_an_empty_offer_and_a_404_file(self) -> None:
        self.assertEqual(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"], None)
        self.assertEqual(self.client.get("/v1/apps/story/release/file", headers=AUTH).status_code, 404)

    def test_a_bundle_without_a_version_is_refused(self) -> None:
        bad = story_zip(self.root / "bad.zip", "not-a-version")
        response = self.client.post("/admin/api/story/release/publish", json={"artifact_path": str(bad)})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(story_releases.current())

    def test_a_replaced_artifact_goes_to_the_trash_folder(self) -> None:
        self.publish("1.4.258")
        self.publish("1.4.259")
        trashed = list((self.root / "data" / "_trash").rglob("Story Studio KH 1.4.258.zip"))
        self.assertEqual(len(trashed), 1)
        self.assertEqual(story_releases.current()["version"], "1.4.259")

    def test_a_range_request_resumes_and_the_whole_file_still_hashes(self) -> None:
        # The Story updater restarts a ~240 MB transfer after every mid-stream stall; this
        # route is where a resume has to work. First byte offset chosen mid-file.
        descriptor = self.publish("1.4.317")
        full = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        self.assertEqual((len(full), hashlib.sha256(full).hexdigest()),
                         (descriptor["bytes"], descriptor["sha256"]))
        mid = len(full) // 3
        resumed = self.client.get("/v1/apps/story/release/file",
                                  headers={**AUTH, "Range": f"bytes={mid}-"})
        self.assertEqual(resumed.status_code, 206)
        self.assertEqual(resumed.headers["content-range"], f"bytes {mid}-{len(full) - 1}/{len(full)}")
        self.assertEqual(resumed.headers["accept-ranges"], "bytes")
        self.assertEqual(resumed.headers["content-length"], str(len(full) - mid))
        self.assertEqual(resumed.content, full[mid:])
        # A range that STARTS AT ZERO is still a range: 206, not a plain 200 (the numeric
        # start is 0, so the decision must not key on "start").
        zero_open = self.client.get("/v1/apps/story/release/file",
                                    headers={**AUTH, "Range": "bytes=0-"})
        self.assertEqual(zero_open.status_code, 206)
        self.assertEqual(zero_open.headers["content-range"], f"bytes 0-{len(full) - 1}/{len(full)}")
        self.assertEqual(zero_open.content, full)
        zero_closed = self.client.get("/v1/apps/story/release/file",
                                      headers={**AUTH, "Range": "bytes=0-5"})
        self.assertEqual(zero_closed.status_code, 206)
        self.assertEqual(zero_closed.headers["content-range"], f"bytes 0-5/{len(full)}")
        self.assertEqual(zero_closed.headers["content-length"], "6")
        self.assertEqual(zero_closed.content, full[:6])

    def test_if_range_with_a_stale_validator_serves_the_whole_file(self) -> None:
        # A resumed partial that belongs to a REPLACED artifact must never be glued to the
        # new one: a mismatched If-Range is answered with the full 200, per RFC 9110 §13.1.6.
        self.publish("1.4.317")
        full = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        stale = hashlib.sha256(b"some-older-artifact").hexdigest()
        response = self.client.get("/v1/apps/story/release/file",
                                   headers={**AUTH, "Range": "bytes=10-", "If-Range": stale})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-range", {k.lower() for k in response.headers})
        self.assertEqual(response.content, full)
        # The matching validator resumes.
        ok = self.client.get("/v1/apps/story/release/file",
                             headers={**AUTH, "Range": "bytes=10-",
                                      "If-Range": story_releases.current()["sha256"]})
        self.assertEqual(ok.status_code, 206)
        self.assertEqual(ok.content, full[10:])
        # The validator is evaluated BEFORE the 416: a STALE partial whose offset is past the
        # new file's end is still the replaced artifact's business, so it gets the full 200 —
        # never a 416. The same out-of-bounds range with the MATCHING validator may 416.
        past_end = f"bytes={len(full)}-"
        stale_past = self.client.get("/v1/apps/story/release/file",
                                     headers={**AUTH, "Range": past_end, "If-Range": stale})
        self.assertEqual(stale_past.status_code, 200)
        self.assertEqual(stale_past.content, full)
        matching_past = self.client.get("/v1/apps/story/release/file",
                                        headers={**AUTH, "Range": past_end,
                                                 "If-Range": story_releases.current()["sha256"]})
        self.assertEqual(matching_past.status_code, 416)
        self.assertEqual(matching_past.headers["content-range"], f"bytes */{len(full)}")

    def test_an_out_of_range_start_is_416_and_other_units_are_ignored(self) -> None:
        self.publish("1.4.317")
        total = story_releases.current()["bytes"]
        too_far = self.client.get("/v1/apps/story/release/file", headers={**AUTH, "Range": f"bytes={total}-"})
        self.assertEqual(too_far.status_code, 416)
        self.assertEqual(too_far.headers["content-range"], f"bytes */{total}")
        # A malformed/other-unit Range is not honoured: plain 200 full body, never a 500.
        weird = self.client.get("/v1/apps/story/release/file", headers={**AUTH, "Range": "items=0-5"})
        self.assertEqual(weird.status_code, 200)
        suffix = self.client.get("/v1/apps/story/release/file", headers={**AUTH, "Range": "bytes=-100"})
        self.assertEqual(suffix.status_code, 200)
        # A closed range `bytes=N-M` is honoured with its INCLUSIVE end clamped to the file.
        mid, end = total // 4, total // 2
        closed = self.client.get("/v1/apps/story/release/file",
                                 headers={**AUTH, "Range": f"bytes={mid}-{end}"})
        self.assertEqual(closed.status_code, 206)
        self.assertEqual(closed.headers["content-range"], f"bytes {mid}-{end}/{total}")
        self.assertEqual(closed.headers["content-length"], str(end - mid + 1))
        full = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        self.assertEqual(closed.content, full[mid:end + 1])
        # An end beyond EOF clamps to the last byte, still 206.
        over = self.client.get("/v1/apps/story/release/file",
                               headers={**AUTH, "Range": f"bytes={mid}-{len(full) * 2}"})
        self.assertEqual(over.status_code, 206)
        self.assertEqual(over.headers["content-range"], f"bytes {mid}-{len(full) - 1}/{total}")
        self.assertEqual(over.content, full[mid:])
        # Reversed bounds are ignored with the full 200, never a 500 or an empty 206.
        reversed_range = self.client.get("/v1/apps/story/release/file",
                                         headers={**AUTH, "Range": "bytes=20-10"})
        self.assertEqual(reversed_range.status_code, 200)
        self.assertEqual(reversed_range.content, full)
        # Multiple ranges are ignored (single-range support only), full 200.
        multi = self.client.get("/v1/apps/story/release/file",
                                headers={**AUTH, "Range": "bytes=0-1,5-6"})
        self.assertEqual(multi.status_code, 200)
        self.assertEqual(multi.content, full)

    def test_a_malformed_or_oversized_digit_header_is_200_not_500(self) -> None:
        # F1: `int("5"*5000)` is valid Python but a needless 5,000-digit conversion, and a
        # value that is not plain ASCII digits (e.g. "+5", "0x10") must be ignored, not
        # raise or produce a partial answer. Raw non-ASCII bytes in a header cannot reach
        # this handler through httpx's str-encoding transport (they raise client-side in
        # httpx 0.28 before the server is called), so the Unicode shapes are untestable
        # here; the server's `ascii_digits` guard still covers them for real traffic.
        # NOTE the boundary: " 5" is a VALID range after HTTP whitespace stripping
        # (`bytes=5-` with padded spaces), so it belongs to the 206 case below, not here.
        self.publish("1.4.317")
        full = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        for bad in ("5" * 5000, "0x10", "+5", ""):
            for header in (f"bytes={bad}-", f"bytes={bad}-{bad}"):
                response = self.client.get("/v1/apps/story/release/file",
                                           headers={**AUTH, "Range": header})
                self.assertEqual(response.status_code, 200, header[:40])
                self.assertEqual(response.content, full, header[:40])
        # A start that is valid ASCII but exceeds the 20-digit cap is ignored too.
        huge = self.client.get("/v1/apps/story/release/file",
                               headers={**AUTH, "Range": "bytes=123456789012345678901234567890-"})
        self.assertEqual(huge.status_code, 200)
        self.assertEqual(huge.content, full)
        # Whitespace-padded ASCII digits are stripped by the server and honoured: 206.
        padded = self.client.get("/v1/apps/story/release/file", headers={**AUTH, "Range": "bytes= 5-"})
        self.assertEqual(padded.status_code, 206)
        self.assertEqual(padded.headers["content-range"], f"bytes 5-{len(full) - 1}/{len(full)}")
        self.assertEqual(padded.headers["content-length"], str(len(full) - 5))
        self.assertEqual(padded.content, full[5:])

    def test_a_replaced_artifact_invalidates_a_saved_resume(self) -> None:
        # The discriminating end-to-end shape: partial of 317-v1 + If-Range(v1 sha) while
        # 317-v2 is published → the client gets the full new file, so its size+sha256 check
        # can only pass against the new artifact, never a mixed one.
        first = self.publish("1.4.317")
        full_first = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        self.publish("1.4.318")
        partial_headers = {**AUTH, "Range": "bytes=5-", "If-Range": first["sha256"]}
        response = self.client.get("/v1/apps/story/release/file", headers=partial_headers)
        full_second = self.client.get("/v1/apps/story/release/file", headers=AUTH).content
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, full_second)
        self.assertNotEqual(full_first, full_second)

    def test_retention_recycles_superseded_release_but_preserves_the_active_offer(self) -> None:
        self.publish("1.4.258")
        active = self.publish("1.4.259")
        active_path = story_releases._artifact(active)
        store = JobStore(self.root / "jobs.sqlite3")
        self.addCleanup(store.close)
        result = retention.prune(now=time.time() + 8 * 86400, store=store)
        self.assertEqual(result["recycled_files"], 1)
        self.assertTrue(active_path.is_file())
        self.assertEqual(story_releases.current()["version"], "1.4.259")
        self.assertEqual(len(list((self.root / "macos-trash").iterdir())), 1)

    def test_checkin_update_now_and_status(self) -> None:
        self.publish("1.4.258")
        report = {"instance_id": "Mac-mini", "version": "1.4.257", "channels": 12,
                  "queue": {"running": 2, "queued": 5, "paused": 0}, "update": {"phase": "idle"}}
        first = self.client.post("/v1/apps/story/checkin", json=report, headers=AUTH).json()
        self.assertEqual((first["release"]["version"], first["update_now"]), ("1.4.258", False))
        rows = self.client.get("/admin/api/story/instances").json()["instances"]
        self.assertEqual((rows[0]["status"], rows[0]["channels"], rows[0]["queue"]["queued"]),
                         ("update_available", 12, 5))

        self.assertEqual(self.client.post("/admin/api/story/instances/Mac-mini/update").status_code, 200)
        self.assertTrue(self.client.post("/v1/apps/story/checkin", json=report, headers=AUTH).json()["update_now"])
        done = self.client.post("/v1/apps/story/checkin", json={**report, "version": "1.4.258"}, headers=AUTH).json()
        self.assertFalse(done["update_now"])
        self.assertEqual(self.client.get("/admin/api/story/instances").json()["instances"][0]["status"], "up_to_date")

    def test_a_failed_install_clears_update_now(self) -> None:
        self.publish("1.4.258")
        report = {"instance_id": "Mac-mini", "version": "1.4.257"}
        self.client.post("/v1/apps/story/checkin", json=report, headers=AUTH)
        self.client.post("/admin/api/story/instances/Mac-mini/update")
        failed = {**report, "update": {"phase": "rolled_back", "version": "1.4.258", "error": "health check"}}
        self.assertFalse(self.client.post("/v1/apps/story/checkin", json=failed, headers=AUTH).json()["update_now"])
        self.assertEqual(self.client.get("/admin/api/story/instances").json()["instances"][0]["status"], "failed")

    def test_unknown_mac_and_bad_name_are_refused(self) -> None:
        self.assertEqual(self.client.post("/admin/api/story/instances/nobody/update").status_code, 404)
        bad = self.client.post("/v1/apps/story/checkin", json={"instance_id": "../x"}, headers=AUTH)
        self.assertEqual(bad.status_code, 422)

    def checkin(self, instance: str = "Mac-mini", **update) -> None:
        response = self.client.post(
            "/v1/apps/story/checkin",
            json={"instance_id": instance, "version": "1.4.257", "update": update}, headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)

    def record(self, instance: str = "Mac-mini") -> dict:
        return story_releases._read_json(story_releases._instances_file())[instance]

    def test_a_reported_byte_count_is_recorded_and_times_the_progress(self) -> None:
        """`update_progress_at` is what the watchdog reads to tell a slow download from a stopped one."""
        self.publish("1.4.258")
        self.checkin(phase="downloading", version="1.4.258", downloaded_bytes=1000, total_bytes=9000)
        first = self.record()
        self.assertEqual((first["update"]["downloaded_bytes"], first["update"]["total_bytes"]), (1000, 9000))
        started = first["update_progress_at"]

        time.sleep(0.01)
        self.checkin(phase="downloading", version="1.4.258", downloaded_bytes=1000, total_bytes=9000)
        self.assertEqual(self.record()["update_progress_at"], started)  # nothing moved

        self.checkin(phase="downloading", version="1.4.258", downloaded_bytes=2000, total_bytes=9000)
        self.assertGreater(self.record()["update_progress_at"], started)

    def test_an_older_story_that_states_no_bytes_is_still_timed(self) -> None:
        """A Mac that reports nothing to compare is *unmeasurable*, never silently "stalled"."""
        self.publish("1.4.258")
        self.checkin(phase="failed", version="1.4.258", error="The download was refused (403).")
        row = self.record()
        self.assertIsNone(row["update"]["downloaded_bytes"])
        self.assertIsInstance(row["update_progress_at"], float)
        self.assertEqual(row["update"]["error"], "The download was refused (403).")

    def test_an_unfamiliar_phase_is_none_not_a_guess(self) -> None:
        self.publish("1.4.258")
        self.checkin(phase="frobnicating")
        self.assertIsNone(self.record()["update"]["phase"])

    def test_the_instances_route_carries_the_progress_stamp(self) -> None:
        self.publish("1.4.258")
        self.checkin(phase="downloading", version="1.4.258", downloaded_bytes=10)
        row = self.client.get("/admin/api/story/instances").json()["instances"][0]
        self.assertIsInstance(row["update_progress_at"], float)
        self.assertEqual(row["update"]["downloaded_bytes"], 10)


    def test_the_queue_view_counts_by_caller_and_group(self) -> None:
        from gateway.runtime import store
        store.submit(modality="image", model_id="m", params={}, idempotency_key=None,
                              caller="story-studio@Mac-mini", job_group="ch/ep9")
        view = self.client.get("/admin/api/queue").json()
        self.assertGreaterEqual(view["by_caller"]["story-studio@Mac-mini"]["waiting"], 1)
        self.assertGreaterEqual(view["by_group"]["ch/ep9"]["waiting"], 1)

    def test_offline_after_missing_two_30_minute_checks(self) -> None:
        now = time.time()
        for minutes, status in ((74, "up_to_date"), (76, "offline")):
            row = story_releases.status_row({"instance_id": "m", "last_seen": now - minutes * 60}, None, now)
            self.assertEqual(row["status"], status, minutes)
        self.assertEqual(row["next_check_by"], now - 76 * 60 + 30 * 60)

    def test_a_descriptor_whose_file_is_missing_or_the_wrong_size_is_not_offered(self) -> None:
        descriptor = self.publish("1.4.258")
        path = story_releases._artifact(descriptor)
        original = path.read_bytes()
        path.write_bytes(original[:-1])  # cut short
        self.assertIsNone(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"])
        self.assertEqual(self.client.get("/v1/apps/story/release/file", headers=AUTH).status_code, 404)
        path.write_bytes(original)
        self.assertEqual(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"], descriptor)
        path.rename(path.with_name("moved"))  # gone
        self.assertIsNone(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"])

    def _poll(self, seen: list, client: TestClient, known: set) -> None:
        """One poll as a Story Mac does it: whatever file is served must be a whole published artifact
        (its sha256 is one we published), and when it is the one just offered, it matches the signed size."""
        offer = client.get("/v1/apps/story/release", headers=AUTH).json()["release"]
        if offer is None:
            seen.append(None)
            return
        body = client.get("/v1/apps/story/release/file", headers=AUTH)
        if body.status_code == 404:
            return  # replaced between the two calls: Story just asks again
        digest = hashlib.sha256(body.content).hexdigest()
        self.assertIn(digest, known)
        if digest == offer["sha256"]:
            self.assertEqual(len(body.content), offer["bytes"])
            seen.append(offer["version"])

    def test_nothing_is_offered_while_a_publish_is_still_copying(self) -> None:
        known = {story_releases.current()["sha256"] for _ in [self.publish("1.4.258")]}
        big = story_zip(self.root / "Story Studio KH 1.4.259.zip", "1.4.259")
        started, proceed = threading.Event(), threading.Event()
        real = story_releases.shutil.copyfile

        def slow_copy(src, dst, **kw):  # half the bytes, then hold the publish open
            data = Path(src).read_bytes()
            with open(dst, "wb") as out:
                out.write(data[: len(data) // 2])
                out.flush()
                started.set()
                proceed.wait(10)
                out.write(data[len(data) // 2:])
            return dst

        errors: list = []

        def run() -> None:
            try:
                with patch.object(story_releases.shutil, "copyfile", slow_copy):
                    story_releases.publish(big)
            except Exception as exc:  # surfaced below
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(10))
        seen: list = []
        for _ in range(5):  # mid-copy: still the old, complete release, byte for byte
            self._poll(seen, self.client, known)
        self.assertEqual(set(seen), {"1.4.258"})
        self.assertTrue(list(story_releases.release_dir().glob(".incoming-*")))  # the copy is under a hidden name
        proceed.set()
        thread.join(15)
        self.assertEqual(errors, [])
        self.assertEqual(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"]["version"], "1.4.259")
        self.assertEqual(list(story_releases.release_dir().glob(".incoming-*")), [])

    def test_requests_during_a_republish_never_see_a_half_published_release(self) -> None:
        known = {story_releases.current()["sha256"] for _ in [self.publish("1.4.258")]}
        stop, seen, errors = threading.Event(), [], []

        def poll() -> None:
            client = TestClient(api.app, headers=AUTH)
            try:
                while not stop.is_set():
                    self._poll(seen, client, known)
            except Exception as exc:
                errors.append(exc)

        pollers = [threading.Thread(target=poll) for _ in range(3)]
        for t in pollers:
            t.start()
        try:
            for n in range(259, 265):  # the same file name every time: replaced in place by name
                zipped = story_zip(self.root / "Story Studio KH.zip", f"1.4.{n}")
                known.add(hashlib.sha256(zipped.read_bytes()).hexdigest())
                story_releases.publish(zipped)
        finally:
            stop.set()
            for t in pollers:
                t.join(15)
        self.assertEqual(errors, [])
        self.assertTrue(seen)
        self.assertEqual(self.client.get("/v1/apps/story/release", headers=AUTH).json()["release"]["version"], "1.4.264")
        folders = [p for p in story_releases.release_dir().iterdir() if p.is_dir()]
        self.assertEqual(len(folders), 1)  # every replaced artifact went to the Trash Folder
        self.assertGreaterEqual(len(list((self.root / "data" / "_trash").rglob("Story Studio KH.zip"))), 1)

    def test_a_failed_publish_leaves_the_offer_alone(self) -> None:
        self.publish("1.4.258")
        with patch.object(release_publisher, "sign_descriptor", side_effect=release_publisher.ReleaseError("no key", 503, "X")):
            with self.assertRaises(release_publisher.ReleaseError):
                story_releases.publish(story_zip(self.root / "next.zip", "1.4.259"))
        self.assertEqual(story_releases.current()["version"], "1.4.258")
        self.assertEqual(list(story_releases.release_dir().glob(".incoming-*")), [])


class CallerCountTests(unittest.TestCase):
    def test_waiting_and_running_by_caller_and_group(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = JobStore(Path(tmp.name) / "jobs.sqlite3")
        self.addCleanup(store.close)
        for caller, group in (("story-studio@mini", "ch/ep1"), ("story-studio@mini", "ch/ep1"),
                              ("story-studio@studio", "x/project"), (None, None)):
            store.submit(modality="image", model_id="m", params={}, idempotency_key=None,
                         caller=caller, job_group=group)
        counts = store.active_counts_by_caller()
        self.assertEqual(counts["story-studio@mini"], {"waiting": 2, "running": 0})
        self.assertEqual(counts[""], {"waiting": 1, "running": 0})
        self.assertEqual(store.active_counts_by_caller("job_group")["x/project"]["waiting"], 1)
        with self.assertRaises(ValueError):
            store.active_counts_by_caller("state; drop")


if __name__ == "__main__":
    unittest.main()
