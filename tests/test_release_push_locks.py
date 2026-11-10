"""Per-controller push locks: disjoint publishes overlap, same-controller serialises.

Proves the locking contract of `release_publisher.publish` and `catch_up` without
network or real controllers: `_publish` is replaced with a slow fake that records
which controller is being pushed and when, so concurrency assertions are deterministic.
The state-write tests exercise the real `_update_state` path (no `_publish` mock)
so lost-update regressions are caught.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env()

from gateway import fleet, notifier, release_publisher as rp, release_rollout  # noqa: E402


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
        events: list[tuple[str, str, float]] = []
        lock = threading.Lock()

        def fake_publish(**kwargs):
            cids = kwargs.get("controller_ids") or []
            cid = cids[0] if cids else "unknown"
            with lock:
                events.append(("start", cid, time.monotonic()))
            time.sleep(0.15)
            with lock:
                events.append(("end", cid, time.monotonic()))
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
        # Both started before either ended -> concurrent.
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
             patch.object(release_rollout, "active", return_value=False), \
             patch.object(release_rollout, "finished", return_value=True):

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

            # While its own lock is held, pending must survive untouched and NOT be interrupted.
            state = rp.last_push()
            entry = state.get("locations", {}).get("alpha", {})
            self.assertIs(entry.get("pending"), True,
                          "pending must stay while the controller lock is held")
            self.assertNotEqual(entry.get("code"), "CATCHUP_INTERRUPTED")

            barrier.set()
            t.join(timeout=5)

    def test_pending_interrupted_while_another_controller_is_pushing(self) -> None:
        """Per-controller rule: A's pending entry is interrupted even while B holds its lock."""
        rp._write_state({
            "locations": {"alpha": {"pending": True, "ok": None}},
        })
        bravo = rp._lock_for("bravo")
        bravo.acquire()
        try:
            state = rp.last_push()
            entry = state.get("locations", {}).get("alpha", {})
            self.assertEqual(entry.get("code"), "CATCHUP_INTERRUPTED",
                             "A's pending must not be skipped because B is pushing")
        finally:
            bravo.release()

    def test_pending_marked_interrupted_when_no_lock(self) -> None:
        rp._write_state({
            "locations": {"alpha": {"pending": True, "ok": None}},
        })
        # No lock held -> pending is interrupted.
        state = rp.last_push()
        entry = state.get("locations", {}).get("alpha", {})
        self.assertFalse(entry.get("pending"))
        self.assertEqual(entry.get("code"), "CATCHUP_INTERRUPTED")


class CatchUpLockRelease(Base):
    """A failure between acquiring the controller's lock and starting the push releases it."""

    def test_state_write_failure_releases_controller_lock(self) -> None:
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
             "release": None, "hub_latest": {"version": "0.0.2"}},
        ]

        def boom(_fn):
            raise RuntimeError("state write failed")

        with patch.object(rp, "_update_state", side_effect=boom), \
             patch.object(rp, "_catchup_artifact", return_value=(self.artifact, None)), \
             patch.object(release_rollout, "active", return_value=False), \
             patch.object(release_rollout, "finished", return_value=True):
            with self.assertRaises(RuntimeError):
                rp.catch_up(rows, wait=True)

        self.assertFalse(rp._lock_for("alpha").locked(),
                         "the controller lock leaked when the push never started")


class StateFileSerialisation(Base):
    """Two concurrent manual-path state updates for disjoint controllers lose no entries."""

    def test_concurrent_state_writes_no_lost_update(self) -> None:
        """Exercise the real _update_state path: two threads write disjoint controller
        outcomes for the same release simultaneously. Both must survive."""
        # Seed a release so the manual path merges rather than replaces.
        rp._write_state({
            "release": {"version": "0.0.1", "build": "b", "sha256": "ab" * 32},
            "artifact_path": str(self.artifact),
            "fingerprint": "ab" * 32,
            "force_full": False,
            "locations": {},
        })

        released = {"version": "0.0.1", "build": "b", "sha256": "ab" * 32}
        barrier = threading.Barrier(2)

        def write_location(cid: str) -> None:
            barrier.wait(timeout=5)
            outcome = {"ok": True, "mode": "full", "label": cid}
            def _apply(state: dict) -> None:
                held = state.get("release") if isinstance(state.get("release"), dict) else {}
                if (held.get("version"), held.get("build"), held.get("sha256")) != (
                        released.get("version"), released.get("build"), released.get("sha256")):
                    state.clear()
                    state["locations"] = {}
                state.setdefault("locations", {})[cid] = outcome
            rp._update_state(_apply)

        t1 = threading.Thread(target=write_location, args=("alpha",))
        t2 = threading.Thread(target=write_location, args=("bravo",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        final = rp._read_state()
        locs = final.get("locations", {})
        self.assertIn("alpha", locs, "alpha's entry was lost in concurrent write")
        self.assertIn("bravo", locs, "bravo's entry was lost in concurrent write")
        self.assertTrue(locs["alpha"].get("ok"))
        self.assertTrue(locs["bravo"].get("ok"))


class AllAtOnceRegression(Base):
    """publish(controller_ids=None) still passes None to _publish so record_all_at_once fires."""

    def test_none_controller_ids_reaches_publish_unchanged(self) -> None:
        received: list = []

        def fake_publish(**kwargs):
            received.append(kwargs.get("controller_ids"))
            return {"locations": {}, "release": {"version": "0.0.1"}}

        mock_rollout = MagicMock()
        with patch.object(rp, "_publish", side_effect=fake_publish), \
             patch.dict("sys.modules", {"gateway.release_rollout": mock_rollout}):
            rp.publish(
                artifact_path=str(self.artifact), version="0.0.1", build="b",
                controller_ids=None,
            )

        self.assertEqual(received, [None],
                         "controller_ids=None must reach _publish unchanged")


class _Transport(Base):
    """The real `_publish` and catch-up, with only the network stubbed. `push` waits on a gate
    for the controller named in `self.hold`, so a test can finish a newer release meanwhile."""

    SHA = "ab" * 32

    def setUp(self) -> None:
        super().setUp()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.hold: str | None = None
        self.answer = {"ok": True, "status": 200, "code": None, "detail": None}
        self.sent: list[str] = []

        @contextlib.contextmanager
        def mounted(_path):
            yield self.root

        def no_manifest(_root):
            raise rp.ReleaseError("no manifest", 400, "RELEASE_NO_MANIFEST")

        def push(url, token, descriptor, path):
            if self.hold and url.startswith(f"http://{self.hold}."):
                self.entered.set()
                self.gate.wait(5)
            return {**self.answer, "release": {"version": descriptor["version"]}}

        for started in (
            patch.object(rp, "_probe", lambda url, token: (None, False)),
            patch.object(rp, "_mounted", mounted),
            patch.object(rp, "load_manifest", no_manifest),
            patch.object(rp, "push", push),
            patch.object(rp, "_catchup_backoff", {}),
            patch.object(notifier, "notify", lambda kind, text: self.sent.append(text)),
            patch.object(notifier, "release_message", lambda state: "release message"),
            patch.object(rp, "_catchup_artifact", lambda state, version: (self.artifact, None)),
            patch.object(release_rollout, "active", return_value=False),
            patch.object(release_rollout, "finished", return_value=True),
        ):
            started.start()
            self.addCleanup(started.stop)

    def publish(self, version: str, cid: str, **kw) -> dict:
        return rp.publish(artifact_path=str(self.artifact), version=version, build="b",
                          controller_ids=[cid], **kw)

    def in_background(self, fn, *args, **kw) -> tuple[threading.Thread, list]:
        out: list = []
        t = threading.Thread(target=lambda: out.append(fn(*args, **kw)))
        t.start()
        self.assertTrue(self.entered.wait(5), "the held transfer never started")
        return t, out

    def finish(self, t: threading.Thread) -> None:
        self.gate.set()
        t.join(5)
        self.assertFalse(t.is_alive())

    def seed(self, version: str) -> None:
        rp._write_state({"release": {"version": version, "build": "b", "sha256": self.SHA},
                         "artifact_path": str(self.artifact), "fingerprint": self.SHA,
                         "force_full": False, "locations": {}})

    def rows(self, version: str) -> list[dict]:
        trust = {"reported": True, "fingerprint": self.SHA}
        return [{"id": "alpha", "label": "Alpha", "url": "http://alpha.invalid", "local": False,
                 "reachable": True, "trusted_publisher": trust, "release": None,
                 "hub_latest": {"version": version}}]


class StaleOutcomeStorage(Base):
    def test_failed_retention_reports_the_error_without_touching_the_newer_report(self) -> None:
        state = {"release": {"version": "0.0.2"}, "epoch": 2,
                 "locations": {"bravo": {"ok": True}}}
        rp._write_state(state)
        before = rp._state_path().read_bytes()
        outcome = {"kind": "manual", "release": {"version": "0.0.1"},
                   "locations": {"alpha": {"ok": True}}}
        with patch.object(rp.os, "open", side_effect=OSError("fixture storage unavailable")), \
             patch("builtins.print") as logged:
            result = rp._retain_stale(state, outcome)
        self.assertTrue(result["stale"])
        self.assertEqual(result["locations"], outcome["locations"])
        self.assertIn("fixture storage unavailable", result["retention_error"])
        self.assertEqual(rp._state_path().read_bytes(), before)
        self.assertEqual(state, rp._read_state())
        self.assertEqual(rp.stale_outcomes(), [])
        logged.assert_called_once_with(f"[release] ERROR: {result['retention_error']}", flush=True)


class StaleOwnerPublishFence(_Transport):
    """A publish of release X that finishes after a later-started publish of Y never touches Y's
    report; its own outcome is kept apart and returned to its caller."""

    def check_kept_apart(self, out: list, kind: str, ok: bool) -> None:
        report = rp.last_push()
        self.assertEqual(report["release"]["version"], "0.0.2", "the newer release lost the report")
        self.assertEqual(set(report["locations"]), {"bravo"}, "the stale outcome joined the newer report")
        (kept,) = rp.stale_outcomes()
        self.assertEqual((kept["kind"], kept["release"]["version"], kept["stale"]), (kind, "0.0.1", True))
        self.assertEqual(kept["superseded_by"]["release"]["version"], "0.0.2")
        self.assertIs(kept["locations"]["alpha"]["ok"], ok)
        (result,) = out
        self.assertTrue(result["stale"])
        self.assertEqual(result["locations"]["alpha"]["ok"], ok,
                         "the caller must read its own outcome, not the newer report's")

    def test_stale_success_keeps_the_newer_report(self) -> None:
        self.hold = "alpha"
        t, out = self.in_background(self.publish, "0.0.1", "alpha")
        self.publish("0.0.2", "bravo")
        self.finish(t)
        self.check_kept_apart(out, "manual", True)
        self.assertEqual(len(self.sent), 1, "only the expected release tells the owner")

    def test_stale_rollout_failure_keeps_the_newer_report(self) -> None:
        self.hold = "alpha"
        self.answer = {"ok": False, "status": 503, "code": "HUB_BUSY", "detail": "busy"}
        t, out = self.in_background(self.publish, "0.0.1", "alpha", rollout=True)
        self.answer = {"ok": True, "status": 200, "code": None, "detail": None}
        self.publish("0.0.2", "bravo")
        self.answer = {"ok": False, "status": 503, "code": "HUB_BUSY", "detail": "busy"}
        self.finish(t)
        self.check_kept_apart(out, "rollout", False)
        # release_rollout._push reads exactly this: the step's own failure.
        self.assertEqual(out[0]["locations"]["alpha"]["code"], "HUB_BUSY")

    def test_same_release_overlap_merges_and_keeps_the_later_epoch(self) -> None:
        self.hold = "alpha"
        t, _ = self.in_background(self.publish, "0.0.1", "alpha")
        self.publish("0.0.1", "bravo")
        later = rp._read_state()["epoch"]
        self.finish(t)
        report = rp.last_push()
        self.assertEqual(set(report["locations"]), {"alpha", "bravo"})
        self.assertEqual(report["epoch"], later, "the earlier-started push rewound the epoch")
        self.assertEqual(rp.stale_outcomes(), [])

    def test_epoch_counts_up_across_a_new_report_and_legacy_state(self) -> None:
        self.seed("0.0.1")  # written before epochs existed: epoch 0
        self.publish("0.0.2", "alpha")
        first = rp._read_state()
        self.publish("0.0.3", "bravo")
        second = rp._read_state()
        self.assertEqual(first["epoch"], first["epoch_seq"])
        self.assertGreater(second["epoch"], first["epoch"], "clearing the report reset the counter")
        self.assertEqual(set(second["locations"]), {"bravo"})


class StaleCatchUpFence(_Transport):
    """A catch-up binds every write (pending, outcome, failure, retry) to the release it read."""

    def test_stale_catch_up_success_keeps_the_newer_report(self) -> None:
        self.seed("0.0.1")
        self.hold = "alpha"
        t, out = self.in_background(rp.catch_up, self.rows("0.0.1"), wait=True)
        self.publish("0.0.2", "bravo")
        self.finish(t)
        self.assertEqual(out, ["alpha"])
        report = rp.last_push()
        self.assertEqual(report["release"]["version"], "0.0.2")
        self.assertNotIn("alpha", report["locations"], "the old pending or outcome reached the newer report")
        (kept,) = rp.stale_outcomes()
        self.assertEqual((kept["kind"], kept["release"]["version"]), ("catch-up", "0.0.1"))
        self.assertTrue(kept["locations"]["alpha"]["ok"])
        self.assertFalse(rp._lock_for("alpha").locked())

    def test_stale_catch_up_failure_and_retry_stay_out_of_the_newer_report(self) -> None:
        self.seed("0.0.1")
        entered, gate = threading.Event(), threading.Event()

        def failing(**kwargs):
            entered.set()
            gate.wait(5)
            raise rp.ReleaseError("the file is gone", 400, "RELEASE_ARTIFACT_MISSING")

        with patch.object(rp, "_publish", failing):
            t = threading.Thread(target=rp.catch_up, args=(self.rows("0.0.1"),), kwargs={"wait": True})
            t.start()
            self.assertTrue(entered.wait(5))
            # A newer release reported meanwhile (to other controllers), with an entry for alpha.
            rp._update_state(lambda s: (s.clear(), s.update(
                release={"version": "0.0.2", "build": "b", "sha256": self.SHA}, epoch=9,
                locations={"alpha": {"ok": True, "label": "Alpha"}})))
            gate.set()
            t.join(5)
        report = rp.last_push()
        self.assertEqual(report["locations"]["alpha"], {"ok": True, "label": "Alpha"},
                         "the old failure or its retry_at overwrote the newer outcome")
        (kept,) = rp.stale_outcomes()
        self.assertEqual(kept["locations"]["alpha"]["code"], "RELEASE_ARTIFACT_MISSING")
        self.assertEqual(rp._catchup_backoff["alpha"][0], 1, "the controller still backs off")
        self.assertFalse(rp._lock_for("alpha").locked())

    def test_retry_time_is_written_only_into_the_release_that_was_sent(self) -> None:
        rp._write_state({"release": {"version": "0.0.2", "build": "b", "sha256": self.SHA},
                         "locations": {"alpha": {"ok": False}}})
        rp._catchup_settle("alpha", "Alpha", "0.0.1", {"ok": False, "code": "X"}, ("0.0.1", "b", self.SHA))
        self.assertNotIn("retry_at", rp._read_state()["locations"]["alpha"])
        rp._catchup_settle("alpha", "Alpha", "0.0.2", {"ok": False, "code": "X"}, ("0.0.2", "b", self.SHA))
        self.assertIn("retry_at", rp._read_state()["locations"]["alpha"])

    def test_release_replaced_before_pending_sends_nothing(self) -> None:
        self.seed("0.0.1")
        calls: list = []

        def replaced_meanwhile(state, version):
            # Runs after catch_up read the state, before its pending write.
            rp._update_state(lambda s: s.update(release={"version": "0.0.2", "build": "b", "sha256": self.SHA}))
            return self.artifact, None

        with patch.object(rp, "_catchup_artifact", replaced_meanwhile), \
             patch.object(rp, "_publish", lambda **kw: calls.append(kw)):
            self.assertIsNone(rp.catch_up(self.rows("0.0.1"), wait=True))
        self.assertEqual(calls, [], "a superseded catch-up must not transfer")
        self.assertNotIn("alpha", rp.last_push().get("locations") or {})
        self.assertFalse(rp._lock_for("alpha").locked())


if __name__ == "__main__":
    unittest.main()