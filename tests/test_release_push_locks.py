"""Per-controller push locks: disjoint publishes overlap, same-controller serialises.

Proves the locking contract of `release_publisher.publish` and `catch_up` without
network or real controllers: `_publish` is replaced with a slow fake that records
which controller is being pushed and when, so concurrency assertions are deterministic.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

from gateway import fleet, release_publisher as rp  # noqa: E402


class _FakeFleet:
    """Minimal fleet stub: three controllers, no LOCAL_ID."""

    LOCAL_ID = "__local__"

    @staticmethod
    def _read():
        return [
            {"id": "alpha", "label": "Alpha", "url": "http://alpha.invalid"},
            {"id": "bravo", "label": "Bravo", "url": "http://bravo.invalid"},
            {"id": "charlie", "label": "Charlie", "url": "http://charlie.invalid"},
        ]

    @staticmethod
    def local_label():
        return "Local"

    @staticmethod
    def hub_base_url():
        return "http://local.invalid"

    @staticmethod
    def hub_token():
        return "hub-tok"

    @staticmethod
    def _token_for(cid):
        return f"tok-{cid}"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-push-locks-")
        self.root = Path(self.tmp.name)
        self.artifact = self.root / "fake.dmg"
        self.artifact.write_bytes(b"fake image " * 100)
        self.patches = [
            patch.object(rp, "_state_path", lambda: self.root / "release-state.json"),
            patch.object(rp, "key_path", lambda: self.root / "release-publisher.pem"),
            patch.object(rp, "resolve_artifact", lambda p: self.artifact),
            patch.object(rp, "_hash_file", lambda p: ("ab" * 32, self.artifact.stat().st_size)),
            patch.object(rp, "identity", lambda: {"fingerprint": "ab" * 32}),
            patch.object(rp, "descriptor_for", lambda **kw: {
                "version": kw.get("version", "0.0.1"), "build": kw.get("build", "b"),
                "filename": kw.get("filename", "f.dmg"), "sha256": "ab" * 32,
                "signature": "sig", "bytes": kw.get("size", 100),
                "files_sha256": None, "files_count": None, "files_bytes": None,
            }),
            patch.object(fleet, "_read", _FakeFleet._read),
            patch.object(fleet, "LOCAL_ID", _FakeFleet.LOCAL_ID),
            patch.object(fleet, "local_label", _FakeFleet.local_label),
            patch.object(fleet, "hub_base_url", _FakeFleet.hub_base_url),
            patch.object(fleet, "hub_token", _FakeFleet.hub_token),
            patch.object(fleet, "_token_for", _FakeFleet._token_for),
        ]
        for p in self.patches:
            p.start()
        # Reset per-controller locks between tests.
        rp._controller_locks.clear()
        rp.forget_identity()

    def tearDown(self) -> None:
        rp.forget_identity()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class DisjointOverlap(Base):
    """Two publishes naming disjoint controllers run at the same time."""

    def test_disjoint_controllers_transfer_concurrently(self) -> None:
        active: dict[str, float] = {}
        lock = threading.Lock()
        events: list[tuple[str, str, float]] = []

        def fake_publish(**kwargs):
            cids = kwargs.get("controller_ids") or []
            cid = cids[0] if cids else "unknown"
            with lock:
                active[cid] = time.monotonic()
                events.append(("start", cid, time.monotonic()))
            time.sleep(0.15)
            with lock:
                events.append(("end", cid, time.monotonic()))
                del active[cid]
            return {"locations": {cid: {"ok": True}}, "release": {"version": "0.0.1"}}

        with patch.object(rp, "_publish", side_effect=fake_publish):
            t1 = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["alpha"],
            })
            t2 = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["bravo"],
            })
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        starts = [e for e in events if e[0] == "start"]
        ends = [e for e in events if e[0] == "end"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(ends), 2)
        # Both started before either ended → concurrent.
        first_end = min(e[2] for e in ends)
        last_start = max(e[2] for e in starts)
        self.assertLess(last_start, first_end,
                        "disjoint publishes must overlap; they ran serially")


class SameControllerSerialisation(Base):
    """Two publishes naming the same controller run one after the other."""

    def test_same_controller_is_serialised(self) -> None:
        active_count = 0
        max_active = 0
        lock = threading.Lock()

        def fake_publish(**kwargs):
            nonlocal active_count, max_active
            with lock:
                active_count += 1
                max_active = max(max_active, active_count)
            time.sleep(0.1)
            with lock:
                active_count -= 1
            cids = kwargs.get("controller_ids") or []
            cid = cids[0] if cids else "x"
            return {"locations": {cid: {"ok": True}}, "release": {"version": "0.0.1"}}

        with patch.object(rp, "_publish", side_effect=fake_publish):
            threads = [
                threading.Thread(target=rp.publish, kwargs={
                    "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                    "controller_ids": ["alpha"],
                })
                for _ in range(3)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(max_active, 1,
                         "same-controller publishes must serialise; saw concurrent runs")


class CatchUpSkip(Base):
    """Catch-up skips a controller whose lock is held by another publish."""

    def test_catchup_skips_busy_controller(self) -> None:
        barrier = threading.Event()

        def fake_publish(**kwargs):
            # Hold the lock until the test signals release.
            barrier.wait(timeout=5)
            cids = kwargs.get("controller_ids") or []
            cid = cids[0] if cids else "x"
            return {"locations": {cid: {"ok": True}}, "release": {"version": "0.0.1"}}

        # Seed state so catch_up has a release to send.
        rp._write_state({
            "release": {"version": "0.0.2", "build": "b", "sha256": "ab" * 32},
            "artifact_path": str(self.artifact),
            "fingerprint": "ab" * 32,
            "force_full": False,
            "locations": {},
        })

        rows = [
            {"id": "alpha", "label": "Alpha", "url": "http://a.invalid", "local": False,
             "reachable": True, "trusted_publisher": {"reported": True, "fingerprint": "ab" * 32},
             "release": None,
             "hub_latest": {"version": "0.0.2"}},
        ]

        with patch.object(rp, "_publish", side_effect=fake_publish), \
             patch.object(rp, "_catchup_artifact", return_value=(self.artifact, None)), \
             patch("gateway.release_publisher.release_rollout") as rr:
            rr.active.return_value = False
            rr.finished.return_value = True

            # Start a manual publish that holds alpha's lock.
            t = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["alpha"],
            })
            t.start()
            time.sleep(0.05)  # let it acquire

            # Catch-up should skip alpha (lock held) and return None.
            result = rp.catch_up(rows, wait=True)
            self.assertIsNone(result, "catch-up must skip a busy controller, not wait")

            barrier.set()
            t.join(timeout=5)


class LastPushPendingRule(Base):
    """last_push marks pending entries as interrupted only when no lock is held."""

    def test_pending_not_interrupted_while_lock_held(self) -> None:
        rp._write_state({
            "locations": {"alpha": {"pending": True, "ok": None}},
        })
        barrier = threading.Event()

        def fake_publish(**kwargs):
            barrier.wait(timeout=5)
            return {"locations": {"alpha": {"ok": True}}, "release": {"version": "0.0.1"}}

        with patch.object(rp, "_publish", side_effect=fake_publish):
            t = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["alpha"],
            })
            t.start()
            time.sleep(0.05)

            # While the lock is held, pending must NOT be marked interrupted.
            state = rp.last_push()
            entry = state.get("locations", {}).get("alpha", {})
            self.assertTrue(entry.get("pending"),
                            "pending must stay while the controller lock is held")
            self.assertNotEqual(entry.get("code"), "CATCHUP_INTERRUPTED")

            barrier.set()
            t.join(timeout=5)

    def test_pending_marked_interrupted_when_no_lock(self) -> None:
        rp._write_state({
            "locations": {"alpha": {"pending": True, "ok": None}},
        })
        # No lock held → pending is interrupted.
        state = rp.last_push()
        entry = state.get("locations", {}).get("alpha", {})
        self.assertFalse(entry.get("pending"))
        self.assertEqual(entry.get("code"), "CATCHUP_INTERRUPTED")


class StateFileSerialisation(Base):
    """Two publishes finishing together do not lose each other's state writes."""

    def test_concurrent_state_writes_no_lost_update(self) -> None:
        writes: list[dict] = []
        original_write = rp._write_state

        def recording_write(state):
            writes.append(dict(state))
            original_write(state)

        def fake_publish(**kwargs):
            cids = kwargs.get("controller_ids") or []
            cid = cids[0] if cids else "x"
            time.sleep(0.05)
            return {"locations": {cid: {"ok": True}}, "release": {"version": "0.0.1"}}

        with patch.object(rp, "_publish", side_effect=fake_publish), \
             patch.object(rp, "_write_state", side_effect=recording_write):
            t1 = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["alpha"],
            })
            t2 = threading.Thread(target=rp.publish, kwargs={
                "artifact_path": str(self.artifact), "version": "0.0.1", "build": "b",
                "controller_ids": ["bravo"],
            })
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        # Both writes landed.
        self.assertGreaterEqual(len(writes), 2)
        final = rp._read_state()
        locs = final.get("locations", {})
        self.assertIn("alpha", locs)
        self.assertIn("bravo", locs)


if __name__ == "__main__":
    unittest.main()