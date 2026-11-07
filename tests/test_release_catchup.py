"""Automatic release catch-up: a controller that missed the newest publish is sent it.

`release_publisher.catch_up` runs after each fleet pass with that pass's location rows. The first
test drives the real publish path against the delta controller stand-in from `test_release_delta`;
the rest replace `_publish` so they can count pushes and fail on demand.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import fleet, notifier, release_publisher as rp, release_rollout  # noqa: E402
from test_release_delta import FILES, Controller, build_app  # noqa: E402

FP = "ab" * 32


def row(cid: str, held: str | None, *, reachable: bool = True, trusted: object = FP,
        signer: str = FP) -> dict:
    """One location row as `fleet._refresh` publishes it, reduced to what catch-up reads."""
    trust = ({"reported": True, "publisher_id": "gatewaykh", "fingerprint": trusted}
             if trusted is not None else {"reported": False, "publisher_id": None, "fingerprint": None})
    return {"id": cid, "label": cid.upper(), "url": f"http://{cid}.invalid", "local": False,
            "reachable": reachable, "trusted_publisher": trust,
            "release": ({"version": held, "publisher_key_id": signer} if held else None)}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="gateway-catchup-")
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(rp, "key_path", lambda: self.root / "release-publisher.pem"),
            patch.object(rp, "_state_path", lambda: self.root / "release-state.json"),
            patch.object(rp, "_catchup_backoff", {}),
            patch.object(rp, "_artifact_verdict", None),
            patch.object(notifier, "notify", self._notify),
        ]
        for started in self.patches:
            started.start()
        self.sent: list[str] = []
        rp.forget_identity()

    def tearDown(self) -> None:
        rp.forget_identity()
        for started in self.patches:
            started.stop()
        self.tmp.cleanup()

    def _notify(self, event: str, text: str, **_kw) -> bool:
        self.assertEqual(event, "release")
        self.sent.append(text)
        return True

    def stamp(self, rows: list[dict]) -> list[dict]:
        fleet.stamp_latest(rows, rp.last_push())
        return rows


class RealPush(Base):
    """The whole path: publish to two controllers, add a third, the fleet pass sends it."""

    def setUp(self) -> None:
        super().setUp()
        tree = build_app(self.root / "dmg", FILES)

        @contextmanager
        def fake_mount(_dmg):
            yield tree

        started = patch.object(rp, "_mounted", fake_mount)
        started.start()
        self.patches.append(started)
        self.dmg = self.root / "StudioHub-0.6.132-e6a8b80.dmg"
        self.dmg.write_bytes(b"pretend disk image " * 1000)
        self.controllers = [Controller() for _ in range(3)]

    def tearDown(self) -> None:
        for controller in self.controllers:
            controller.close()
        super().tearDown()

    def test_a_controller_added_after_the_publish_gets_it(self) -> None:
        roster = [{"id": f"c{i}", "label": f"C{i}", "url": c.base} for i, c in enumerate(self.controllers)]
        with patch.object(fleet, "_read", return_value=roster[:2]), \
             patch.object(fleet, "_token_for", return_value="tok"):
            first = rp.publish(artifact_path=self.dmg, version="0.6.132", build="e6a8b80")
        self.assertEqual(first["artifact_path"], str(self.dmg.resolve()))
        fingerprint = first["fingerprint"]
        self.sent.clear()  # the publish's own rollout message

        def rows() -> list[dict]:
            out = []
            for item, c in zip(roster, self.controllers):
                r = row(item["id"], None, trusted=fingerprint)
                r.update(url=item["url"], release=c.held)
                out.append(r)
            return self.stamp(out)

        with patch.object(fleet, "_read", return_value=roster), \
             patch.object(fleet, "_token_for", return_value="tok"):
            self.assertEqual(rp.catch_up(rows(), wait=True), "c2")
            self.assertIsNone(rp.catch_up(rows(), wait=True), "nothing is behind any more")

        self.assertEqual(self.controllers[2].held["version"], "0.6.132")
        state = rp.last_push()
        self.assertTrue(state["locations"]["c2"]["ok"], state["locations"]["c2"])
        self.assertTrue(state["locations"]["c2"]["auto"])
        self.assertEqual(set(state["locations"]), {"c0", "c1", "c2"}, "the publish's own report is kept")
        self.assertEqual(state["updated_at"], first["updated_at"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("sent automatically to C2", self.sent[0])


class Decisions(Base):
    def setUp(self) -> None:
        super().setUp()
        self.dmg = self.root / "hub.dmg"
        self.dmg.write_bytes(b"x" * 4096)
        self.write_state()
        release_rollout.record_all_at_once("0.6.132", "e6a8b80")  # the release finished its rollout
        self.pushes: list[dict] = []
        self.result = {"ok": True, "status": 200, "code": None, "detail": None,
                       "release": {"version": "0.6.132"}}
        started = patch.object(rp, "_publish", self._fake_publish)
        started.start()
        self.patches.append(started)

    def write_state(self, **override) -> None:
        state = {"updated_at": 1000, "fingerprint": FP, "artifact_path": str(self.dmg),
                 "release": {"version": "0.6.132", "build": "e6a8b80", "filename": "hub.dmg",
                             "bytes": self.dmg.stat().st_size,
                             "sha256": hashlib.sha256(self.dmg.read_bytes()).hexdigest()},
                 "locations": {"a": {"ok": True}, "b": {"ok": True}}}
        state.update(override)
        rp._write_state(state)

    def _fake_publish(self, **kwargs) -> dict:
        self.pushes.append(kwargs)
        self.assertTrue(kwargs["auto"])
        (cid,) = kwargs["controller_ids"]
        state = rp._read_state()
        state["locations"][cid] = {**self.result, "auto": True, "at": 2000}
        rp._write_state(state)
        return state

    def fleet_rows(self, newcomer: dict) -> list[dict]:
        return self.stamp([row("a", "0.6.132"), row("b", "0.6.132"), newcomer])

    def outcome(self, cid: str = "n") -> dict:
        return rp.last_push()["locations"][cid]

    # -- who qualifies --------------------------------------------------------

    def test_a_controller_holding_nothing_is_sent_the_recorded_file(self) -> None:
        self.assertEqual(rp.catch_up(self.fleet_rows(row("n", None)), wait=True), "n")
        (push,) = self.pushes
        self.assertEqual((push["artifact_path"], push["version"], push["build"], push["controller_ids"]),
                         (self.dmg, "0.6.132", "e6a8b80", ["n"]))
        self.assertTrue(self.outcome()["ok"])

    def test_manifest_refusal_keeps_its_code_and_detail(self) -> None:
        error = rp.ReleaseError("The file manifest is invalid.", 400, "RELEASE_MANIFEST_INVALID")
        with patch.object(rp, "_publish", side_effect=error):
            self.assertEqual(rp.catch_up(self.fleet_rows(row("n", None)), wait=True), "n")
        self.assertFalse(self.outcome()["ok"])
        self.assertEqual(self.outcome()["code"], "RELEASE_MANIFEST_INVALID")
        self.assertEqual(self.outcome()["detail"], error.message)

    def test_offline_then_online(self) -> None:
        self.assertIsNone(rp.catch_up(self.fleet_rows(row("n", "0.6.131", reachable=False)), wait=True))
        self.assertEqual(self.pushes, [])
        self.assertEqual(rp.catch_up(self.fleet_rows(row("n", "0.6.131")), wait=True), "n")
        self.assertEqual(len(self.pushes), 1)

    def test_an_untrusting_controller_is_told_why_and_never_pushed(self) -> None:
        for trust in ("cd" * 32, "", None):
            self.assertIsNone(rp.catch_up(self.fleet_rows(row("n", "0.6.131", trusted=trust)), wait=True))
        self.assertEqual(self.pushes, [])
        self.assertTrue(self.outcome()["skipped"])
        self.assertIn("did not say which App Update Key", self.outcome()["detail"])
        rp.catch_up(self.fleet_rows(row("n", "0.6.131", trusted="cd" * 32)), wait=True)
        detail = self.outcome()["detail"]
        self.assertIn("doesn't trust this Gateway's App Update Key", detail)
        self.assertIn("App Update Key public key", detail)
        self.assertIn("App Update Key into Hub → Connections", detail)
        self.assertIn("then compare fingerprints", detail)
        self.assertIn(FP, detail)

    def test_never_downgrades_or_touches_a_newer_or_unknown_release(self) -> None:
        for held in ("0.6.133", "0.7.0", "nightly"):
            newer = row("n", held, signer="ee" * 32)  # someone else's build: it does not move "latest"
            self.assertIsNone(rp.catch_up(self.fleet_rows(newer), wait=True), held)
        self.assertIsNone(rp.catch_up(self.fleet_rows(row("n", "0.6.132")), wait=True))
        self.assertEqual(self.pushes, [])

    def test_a_pilot_release_is_not_spread(self) -> None:
        # Pushed by hand to a pilot controller: no rollout finished this release, so nothing is spread.
        (self.root / "release-rollout.json").unlink()
        rows = self.stamp([row("a", "0.6.132"), row("b", "0.6.131"), row("n", None)])
        self.assertIsNone(rp.catch_up(rows, wait=True))
        self.assertEqual(self.pushes, [])

    def test_the_local_mac_is_not_a_controller(self) -> None:
        local = {**row("local", None), "local": True}
        self.assertIsNone(rp.catch_up(self.stamp([row("a", "0.6.132"), local]), wait=True))
        self.assertEqual(self.pushes, [])

    # -- the file -------------------------------------------------------------

    def test_a_missing_or_changed_artifact_is_a_reason_not_a_push(self) -> None:
        self.write_state(artifact_path=None)  # published before catch-up recorded the file
        rp.catch_up(self.fleet_rows(row("n", None)), wait=True)
        self.assertIn("publish it once more", self.outcome()["detail"])
        self.write_state()
        self.dmg.write_bytes(b"y" * 4096)  # same size, different bytes
        rp.catch_up(self.fleet_rows(row("n", None)), wait=True)
        self.assertIn("has changed since", self.outcome()["detail"])
        self.dmg.rename(self.root / "moved.dmg")
        rp.catch_up(self.fleet_rows(row("n", None)), wait=True)
        self.assertIn("is gone", self.outcome()["detail"])
        self.assertEqual(self.pushes, [])

    def test_a_newest_release_this_gateway_holds_no_file_for(self) -> None:
        rows = self.stamp([row("a", "0.6.133"), row("b", "0.6.133"), row("n", "0.6.131")])
        self.assertIsNone(rp.catch_up(rows, wait=True), "0.6.133 did not finish a rollout from here")
        release_rollout.record_all_at_once("0.6.133", "f00")
        self.assertIsNone(rp.catch_up(rows, wait=True))
        self.assertIn("not the one this Gateway last published (0.6.132)", self.outcome()["detail"])
        self.assertEqual(self.pushes, [])

    # -- pacing ---------------------------------------------------------------

    def test_failures_back_off_and_a_streak_is_reported_once(self) -> None:
        self.result = {"ok": False, "status": None, "code": "LOCATION_UNREACHABLE", "detail": "down",
                       "release": None}
        clock = [10_000.0]
        with patch.object(rp.time, "time", lambda: clock[0]):
            rows = self.fleet_rows(row("n", None))
            self.assertEqual(rp.catch_up(rows, wait=True), "n")
            self.assertEqual(self.outcome()["retry_at"], 10_000 + 15 * 60)
            self.assertIsNone(rp.catch_up(rows, wait=True), "inside the back-off")
            clock[0] += 15 * 60
            self.assertEqual(rp.catch_up(rows, wait=True), "n")
            clock[0] += 15 * 60
            self.assertIsNone(rp.catch_up(rows, wait=True), "the second wait is longer")
            clock[0] += 15 * 60
            self.assertEqual(rp.catch_up(rows, wait=True), "n")
            self.assertEqual(len(self.pushes), 3)
            self.assertEqual(len(self.sent), 1)
            self.assertIn("keeps failing (3 tries): down", self.sent[0])
            self.result = {**self.result, "ok": True}
            clock[0] += 6 * 3600
            self.assertEqual(rp.catch_up(rows, wait=True), "n")
            self.assertIn("sent automatically", self.sent[-1])
            self.assertIsNone(rp.catch_up(rows, wait=True), "a pass that read it before the push landed")

    def test_one_push_in_flight_and_the_page_says_catching_up(self) -> None:
        release = threading.Event()
        original = self._fake_publish

        def slow(**kwargs) -> dict:
            release.wait(5)
            return original(**kwargs)

        rows = self.stamp([row("a", "0.6.132"), row("b", "0.6.132"), row("c", "0.6.132"),
                           row("m", None), row("n", None)])
        with patch.object(rp, "_publish", slow):
            self.assertEqual(rp.catch_up(rows), "m")
            self.assertTrue(self.outcome("m")["pending"])
            self.assertIsNone(rp.catch_up(rows), "a second push while the first is in flight")
            release.set()
            for _ in range(100):
                if not rp._push_lock.locked():
                    break
                threading.Event().wait(0.05)
        self.assertEqual([p["controller_ids"] for p in self.pushes], [["m"]])
        self.assertTrue(self.outcome("m")["ok"])
        self.assertEqual(rp.catch_up(rows, wait=True), "n")

    def test_a_manual_publish_in_flight_holds_catch_up_off(self) -> None:
        with rp._push_lock:
            self.assertIsNone(rp.catch_up(self.fleet_rows(row("n", None)), wait=True))
        self.assertEqual(self.pushes, [])

    def test_a_push_the_gateway_stopped_in_the_middle_of_is_not_shown_as_running(self) -> None:
        state = rp._read_state()
        state["locations"]["n"] = {"pending": True, "auto": True, "ok": None}
        rp._write_state(state)
        self.assertEqual(self.outcome()["code"], "CATCHUP_INTERRUPTED")
        self.assertNotIn("pending", self.outcome())
        self.assertEqual(json.loads((self.root / "release-state.json").read_text())["locations"]["n"]["pending"],
                         True, "only the reading changes; the next pass rewrites the file")


if __name__ == "__main__":
    unittest.main()
