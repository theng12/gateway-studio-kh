"""Paced Studio Hub rollout: this Mac first, then one controller at a time (`gateway/release_rollout.py`).

`tick` is driven by hand with fleet rows, so time and the controllers' versions are the test's. The pushes
are counted through a fake `release_publisher.publish`, except in `RealPush`, which sends a real (fake-DMG)
release to controller stand-ins from `test_release_delta`.
"""
from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-rollout-")

from gateway import admin, fleet, notifier, release_publisher as rp, release_rollout as ro  # noqa: E402
from test_release_delta import FILES, Controller, build_app  # noqa: E402

V = "0.6.136"
ROSTER = [{"id": "c13", "label": "1300", "url": "http://c13.invalid"},
          {"id": "c11", "label": "1100", "url": "http://c11.invalid"},
          {"id": "c12", "label": "1200", "url": "http://c12.invalid"}]


def loc(cid: str, version: str | None, reachable: bool = True) -> dict:
    return {"id": cid, "label": cid, "local": cid == "local", "reachable": reachable, "version": version}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(_SANDBOX.name) / self.id().rsplit(".", 1)[-1]
        self.root.mkdir(parents=True, exist_ok=True)
        self.dmg = self.root / f"StudioHub-{V}-abc1234.dmg"
        self.dmg.write_bytes(b"pretend disk image " * 1000)
        self.sent: list[str] = []
        self.pushes: list[str] = []
        self.answer: dict[str, dict] = {}
        for started in (
            patch.object(rp, "_state_path", lambda: self.root / "release-state.json"),
            patch.object(rp, "key_path", lambda: self.root / "release-publisher.pem"),
            patch.object(rp, "_catchup_backoff", {}),
            patch.object(rp, "_artifact_verdict", None),
            patch.object(fleet, "_read", lambda: list(ROSTER)),
            patch.object(fleet, "hub_base_url", lambda: "http://127.0.0.1:47873"),
            patch.object(ro, "_ensure_thread", lambda: None),  # tests tick by hand
            patch.object(notifier, "notify", lambda event, text, **_: self.sent.append(text) or True),
        ):
            started.start()
            self.addCleanup(started.stop)
        rp.forget_identity()
        self.addCleanup(rp.forget_identity)

    def fake_publish(self, **kwargs) -> dict:
        (cid,) = kwargs["controller_ids"]
        self.assertTrue(kwargs["rollout"])
        self.pushes.append(cid)
        return {"locations": {cid: self.answer.get(cid, {"ok": True})}}

    def start(self) -> dict:
        return ro.start(artifact_path=self.dmg, version=V, build="abc1234")

    def steps(self) -> dict[str, str]:
        return {cid: step["state"] for cid, step in ro.read()["steps"].items()}


class Paced(Base):
    def setUp(self) -> None:
        super().setUp()
        started = patch.object(rp, "publish", self.fake_publish)
        started.start()
        self.addCleanup(started.stop)
        self.versions = {"local": "0.6.135", "c11": "0.6.135", "c12": "0.6.135", "c13": "0.6.135"}
        self.down: set[str] = set()

    def rows(self) -> list[dict]:
        return [loc(cid, v, cid not in self.down) for cid, v in self.versions.items()]

    def test_order_is_this_mac_then_controllers_by_label(self) -> None:
        self.assertEqual(self.start()["order"], ["local", "c11", "c12", "c13"])
        with patch.object(fleet, "hub_base_url", lambda: None):
            self.assertEqual([cid for cid, _ in ro.plan()], ["c11", "c12", "c13"], "no Hub here: not a location")

    def test_each_controller_is_waited_for_before_the_next(self) -> None:
        self.start()
        ro.tick(self.rows(), now=0)
        self.assertEqual(self.pushes, ["local"])
        ro.tick(self.rows(), now=60)
        self.assertEqual(self.pushes, ["local"], "the next one waits until this Mac reports the version")
        self.versions["local"] = V
        ro.tick(self.rows(), now=120)
        self.assertEqual(self.pushes, ["local", "c11"])
        self.down.add("c11")  # relaunching into the new app
        ro.tick(self.rows(), now=180)
        self.assertEqual(self.steps()["c11"], "installing")
        self.assertIn("Updating 1100 (2 of 4)", ro.summary(ro.read()))
        self.assertIn("1200–1300", ro.summary(ro.read()).replace("1200, 1300", "1200–1300"))
        self.down.clear()
        self.versions["c11"] = V
        ro.tick(self.rows(), now=240)
        self.assertEqual(self.pushes, ["local", "c11", "c12"])
        self.assertEqual(ro.summary(ro.read()), "Updating 1200 (3 of 4) · This Mac, 1100 done · 1300 waiting")
        self.versions.update(c12=V, c13=V)
        state = ro.tick(self.rows(), now=300)
        self.assertEqual(state["state"], "finished")
        self.assertEqual(self.pushes, ["local", "c11", "c12"], "1300 already ran it: nothing to send")
        self.assertEqual(len(self.sent), 1)
        self.assertIn(f"Studio Hub {V} rolled out", self.sent[0])
        self.assertTrue(ro.finished(V))

    def test_a_controller_that_does_not_come_back_stops_the_rollout(self) -> None:
        self.start()
        self.versions["local"] = V
        ro.tick(self.rows(), now=0)
        self.assertEqual(self.pushes, ["c11"])
        ro.tick(self.rows(), now=ro.STEP_TIMEOUT - 1)
        self.assertEqual(ro.read()["state"], "running")
        state = ro.tick(self.rows(), now=ro.STEP_TIMEOUT + 1)
        self.assertEqual((state["state"], self.steps()["c11"]), ("failed", "failed"))
        self.assertIn("1100 did not come back", state["reason"])
        self.assertIn("rollout stopped", self.sent[-1])
        ro.tick(self.rows(), now=ro.STEP_TIMEOUT + 600)
        self.assertEqual(self.pushes, ["c11"], "nothing more is sent after a stop")
        # Resume sends 1100 the release again and carries on.
        ro.resume()
        ro.tick(self.rows(), now=ro.STEP_TIMEOUT + 700)
        self.assertEqual(self.pushes, ["c11", "c11"])

    def test_a_group_still_updating_its_macs_keeps_the_step_waiting(self) -> None:
        self.start()
        self.versions["local"] = V
        mac = {"location_id": "c11", "hub_version": "0.6.135"}
        ro.tick(self.rows(), now=0, machines=[mac])
        step = ro.tick(self.rows(), now=ro.STEP_TIMEOUT - 60, machines=[mac])["steps"]["c11"]
        self.assertEqual(step["state"], "sent")
        mac["hub_version"] = V  # a Mac of the group got its turn and updated
        ro.tick(self.rows(), now=ro.STEP_TIMEOUT + 60, machines=[mac])
        self.assertEqual(ro.read()["state"], "running", "progress restarted the clock")
        ro.tick(self.rows(), now=2 * ro.STEP_TIMEOUT - 60, machines=[mac])
        self.assertEqual(ro.read()["state"], "running")
        state = ro.tick(self.rows(), now=2 * ro.STEP_TIMEOUT + 61, machines=[mac])
        self.assertEqual(state["state"], "failed", "no further progress for STEP_TIMEOUT")
        self.assertIn("no progress", state["reason"])
        # The backstop: steady progress cannot keep a step waiting forever.
        ro.resume()
        ro.tick(self.rows(), now=10_000, machines=[])
        for n in range(1, 9):  # one more Mac updated every 25 min
            state = ro.tick(self.rows(), now=10_000 + n * 25 * 60,
                            machines=[{"location_id": "c11", "hub_version": V}] * n)
            if state["state"] == "failed":
                break
        self.assertEqual(state["state"], "failed")
        self.assertIn(f"after {ro.STEP_CEILING // 60} min", state["reason"])

    def test_an_unreachable_controller_is_retried_then_skipped(self) -> None:
        self.start()
        self.versions["local"] = V
        self.down.add("c11")
        ro.tick(self.rows(), now=0)
        self.assertEqual(self.pushes, [])
        self.assertIn("trying again in 5 min", ro.read()["steps"]["c11"]["detail"])
        ro.tick(self.rows(), now=60)
        self.assertEqual(self.pushes, [], "inside the retry wait")
        self.answer["c11"] = {"ok": False, "code": "LOCATION_UNREACHABLE", "detail": "no route"}
        self.down.clear()
        ro.tick(self.rows(), now=ro.RETRY_EVERY + 1)
        self.assertEqual(self.pushes, ["c11"])
        self.assertEqual(self.steps()["c11"], "waiting")
        ro.tick(self.rows(), now=ro.SKIP_AFTER + 1)
        self.assertEqual(self.steps()["c11"], "skipped")
        self.assertEqual(self.pushes, ["c11", "c11", "c12"], "an hour later it is skipped and 1200 is next")
        self.versions.update(c12=V, c13=V)
        state = ro.tick(self.rows(), now=ro.SKIP_AFTER + 60)
        self.assertEqual(state["state"], "finished")
        self.assertIn("1100 skipped", self.sent[-1])

    def test_a_downgrade_refusal_skips_at_once(self) -> None:
        self.start()
        self.versions["local"] = V
        self.answer["c11"] = {"ok": False, "downgrade": True, "detail": "this controller holds 0.6.140"}
        ro.tick(self.rows(), now=0)
        self.assertEqual(self.steps()["c11"], "skipped")
        self.assertEqual(self.pushes, ["c11", "c12"])

    def test_a_restart_carries_on_and_resends_a_push_it_cut_short(self) -> None:
        self.start()
        self.versions["local"] = V
        ro.tick(self.rows(), now=0)
        state = ro.read()
        state["steps"]["c12"]["state"] = "sending"  # the process died in the middle of the next push
        state["steps"]["c11"]["state"] = "updated"
        ro._write(state)
        resumed: list[bool] = []
        with patch.object(ro, "_ensure_thread", lambda: resumed.append(True)):
            ro.resume_on_startup()
        self.assertEqual(resumed, [True])
        ro.tick(self.rows(), now=30)
        self.assertEqual(self.pushes, ["c11", "c12"])
        self.assertEqual(self.steps()["c12"], "sent")
        # Nothing to resume once it is over.
        ro.stop()
        resumed.clear()
        with patch.object(ro, "_ensure_thread", lambda: resumed.append(True)):
            ro.resume_on_startup()
        self.assertEqual(resumed, [])

    def test_the_owner_can_stop_and_resume(self) -> None:
        self.start()
        with self.assertRaises(rp.ReleaseError):
            self.start()
        ro.stop()
        self.assertEqual(ro.read()["state"], "stopped")
        self.assertIn("stopped by the owner", self.sent[-1])
        ro.tick(self.rows(), now=0)
        self.assertEqual(self.pushes, [])
        ro.resume()
        ro.tick(self.rows(), now=1)
        self.assertEqual(self.pushes, ["local"])

    def test_status_counts_each_groups_macs_on_the_new_version(self) -> None:
        self.start()
        machines = [{"location_id": "c11", "hub_version": V}, {"location_id": "c11", "hub_version": "0.6.135"},
                    {"location_id": "c11", "hub_version": V}]
        status = ro.status({"machines": machines})
        self.assertEqual((status["steps"]["c11"]["nodes_updated"], status["steps"]["c11"]["nodes_total"]), (2, 3))
        self.assertIsNone(status["steps"]["c12"]["nodes_total"])
        self.assertTrue(status["summary"].startswith("Waiting for This Mac (1 of 4)"))

    def test_status_shows_why_a_controllers_update_to_this_build_failed(self) -> None:
        self.start()  # build abc1234
        def own(cid: str, **error) -> dict:
            return {"location_id": cid, "hub_version": "0.6.130", "node_state": None, "node_role": None,
                    "startup": {"last_update_error": {"phase": "downloading", "reason": "HTTP 404", "at": 1.0, **error}}}
        node = {"location_id": "c12", "hub_version": "0.6.130", "node_state": "reachable", "node_role": "agent",
                "startup": {"last_update_error": {"phase": "installing", "reason": "a node, not the controller",
                                                  "build": "abc1234", "at": 1.0}}}
        steps = ro.status({"machines": [own("c11", build="abc1234"), own("c12", build="0000000"), node,
                                        own("local", build=None)]})["steps"]
        self.assertEqual(steps["c11"]["update_error"]["reason"], "HTTP 404")
        self.assertIsNone(steps["c12"]["update_error"], "another build's failure is not this step's, nor a node's")
        self.assertIsNone(steps["local"]["update_error"], "no build, no claim")
        self.assertIsNone(steps["c13"]["update_error"])

    def test_an_updated_step_has_nothing_to_explain(self) -> None:
        self.start()
        state = ro.read()
        state["steps"]["c11"]["state"] = "updated"
        ro._write(state)
        error = {"phase": "installing", "reason": "old news", "build": "abc1234", "at": 1.0}
        status = ro.status({"machines": [{"location_id": "c11", "node_state": None, "node_role": None,
                                          "startup": {"last_update_error": error}}]})
        self.assertIsNone(status["steps"]["c11"]["update_error"])


class CatchUpWaits(Base):
    """The release catch-up never races a rollout, and handles its stragglers once it is over."""

    def setUp(self) -> None:
        super().setUp()
        self.catchups: list[list[str]] = []

        def fake(**kwargs) -> dict:
            if kwargs.get("rollout"):
                return self.fake_publish(**kwargs)
            self.catchups.append(kwargs["controller_ids"])
            return {"locations": {kwargs["controller_ids"][0]: {"ok": True}}}

        for started in (patch.object(rp, "publish", fake), patch.object(rp, "_publish", fake)):
            started.start()
            self.addCleanup(started.stop)
        rp._write_state({"updated_at": 1, "fingerprint": "ab" * 32, "artifact_path": str(self.dmg),
                         "release": {"version": V, "build": "abc1234", "bytes": self.dmg.stat().st_size,
                                     "sha256": rp._hash_file(self.dmg)[0]},
                         "locations": {}})

    def rows(self, c11: str | None, c11_reachable: bool = True) -> list[dict]:
        trust = {"reported": True, "publisher_id": "gatewaykh", "fingerprint": "ab" * 32}
        rows = []
        for cid, held, up in (("c11", c11, c11_reachable), ("c12", V, True), ("c13", V, True)):
            rows.append({**loc(cid, held, up), "trusted_publisher": trust,
                         "release": {"version": held, "publisher_key_id": "ab" * 32} if held else None})
        fleet.stamp_latest(rows, rp.last_push())
        return rows

    def test_catch_up_waits_for_the_rollout_then_sends_the_skipped_one(self) -> None:
        self.start()
        self.assertIsNone(rp.catch_up(self.rows("0.6.135"), wait=True), "a rollout is running")
        state = ro.read()
        for cid in ("local", "c12", "c13"):
            state["steps"][cid]["state"] = "updated"
        state["steps"]["c11"]["state"] = "skipped"
        ro._write(state)
        ro.tick(self.rows("0.6.135", c11_reachable=False), now=0)
        self.assertEqual(ro.read()["state"], "finished")
        self.assertEqual(rp.catch_up(self.rows("0.6.135"), wait=True), "c11")
        self.assertEqual(self.catchups, [["c11"]])

    def test_a_stopped_rollout_or_a_pilot_is_not_spread(self) -> None:
        self.assertIsNone(rp.catch_up(self.rows("0.6.135"), wait=True), "no rollout finished this release")
        self.start()
        ro.stop()
        self.assertIsNone(rp.catch_up(self.rows("0.6.135"), wait=True), "the rollout was stopped")
        self.assertEqual(self.catchups, [])

    def test_an_all_at_once_publish_counts_as_finished(self) -> None:
        ro.record_all_at_once(V, "abc1234")
        self.assertEqual(rp.catch_up(self.rows(None), wait=True), "c11")


class Endpoint(Base):
    def test_publish_is_paced_by_default_and_all_at_once_on_request(self) -> None:
        body = {"artifact_path": str(self.dmg), "version": V, "build": "abc1234"}
        answer = admin.release_publish(dict(body))
        self.assertEqual(answer["rollout"]["state"], "running")
        self.assertEqual(admin.release_publish(dict(body)).status_code, 409, "one rollout at a time")
        calls: list[dict] = []
        with patch.object(rp, "publish", lambda **kw: calls.append(kw) or {"locations": {}}):
            admin.release_publish({**body, "mode": "all_at_once"})
        self.assertEqual(calls[0]["controller_ids"], None)
        self.assertEqual(ro.read()["state"], "stopped", "an emergency publish replaces the rollout")
        self.assertEqual(admin.release_publish({**body, "mode": "later"}).status_code, 422)


class RealPush(Base):
    """Real pushes: this Mac's Hub with its own token, then each controller with its Fleet token."""

    def setUp(self) -> None:
        super().setUp()
        tree = build_app(self.root / "dmg", FILES)

        @contextmanager
        def fake_mount(_dmg):
            yield tree

        self.hubs = {cid: Controller() for cid in ("local", "c11", "c12", "c13")}
        for hub in self.hubs.values():
            self.addCleanup(hub.close)
        roster = [{**row, "url": self.hubs[row["id"]].base} for row in ROSTER]
        self.tokens: dict[str, set[str]] = {}
        probe = rp._probe
        base_of = {hub.base: cid for cid, hub in self.hubs.items()}

        def recording_probe(base: str, token: str):
            self.tokens.setdefault(base_of[base], set()).add(token)
            return probe(base, token)

        for started in (patch.object(rp, "_mounted", fake_mount),
                        patch.object(rp, "_probe", recording_probe),
                        patch.object(fleet, "_read", lambda: roster),
                        patch.object(fleet, "hub_base_url", lambda: self.hubs["local"].base),
                        patch.object(fleet, "hub_token", lambda: "local-token"),
                        patch.object(fleet, "_token_for", lambda cid: f"fleet-{cid}")):
            started.start()
            self.addCleanup(started.stop)

    def test_one_location_at_a_time_into_one_report(self) -> None:
        self.start()
        rows = {cid: loc(cid, "0.6.135") for cid in self.hubs}
        for n, cid in enumerate(("local", "c11", "c12", "c13")):
            ro.tick(list(rows.values()), now=n * 60)
            self.assertEqual(self.hubs[cid].held["version"], V, cid)
            later = [c for c in self.hubs if c not in ("local", "c11", "c12", "c13")[:n + 1]]
            self.assertTrue(all(self.hubs[c].held is None for c in later), "the next one waits")
            rows[cid]["version"] = V
        state = ro.tick(list(rows.values()), now=300)
        self.assertEqual(state["state"], "finished")
        push = rp.last_push()
        self.assertEqual(set(push["locations"]), {"local", "c11", "c12", "c13"}, "one report for the release")
        self.assertEqual(push["artifact_path"], str(self.dmg.resolve()))
        self.assertEqual(len(self.sent), 1, "one Telegram message, when the rollout ends")
        self.assertEqual(self.tokens, {"local": {"local-token"}, "c11": {"fleet-c11"},
                                       "c12": {"fleet-c12"}, "c13": {"fleet-c13"}},
                         "this Mac's own token goes to this Mac's Hub and nowhere else")


if __name__ == "__main__":
    unittest.main()
