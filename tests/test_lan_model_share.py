"""LAN model sharing (owner decision 2026-10-05, `INTERFACES-0146.md`): networks, the per-network spread, the relay.

What these pin:

* networks: the owner's names are written once when the file is missing, a Mac's own network beats its
  location's, an empty override goes back to the location's, and a shop iMac with none set is on its group's;
* the spread: a Mac copies only from a Mac on its own named network, never across; a network with no copy
  downloads from the internet once at a time; every source lends to at most two; finished Macs lend in turn;
* skips: Lite Mode for a model it does not run, This Mac while paused for generation, offline Macs;
* an older Hub (no share route) means that Mac downloads from the internet, with the HUB_TOO_OLD sentence;
* the plan lives in a file: a Gateway restart keeps following the same downloads instead of starting new ones.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-lan-share-")

import httpx  # noqa: E402

from gateway import admin, config, fleet, model_share, startup_control  # noqa: E402

FISH = "fish-audio-s2-pro-4bit"
A1, A2, B1 = "de7250e146a7", "9701220bf30f", "a311a0a9f4f7"   # groups 1100, 1200 (network A), 1300 (B)
SHOPS = "4bb919aee2e4"


def snapshot(*macs: tuple) -> dict:
    """`(location, machine, has_it, **extra)` rows, shaped like the Fleet read."""
    machines, models = [], {}
    for loc, mid, has, *extra in macs:
        extra = extra[0] if extra else {}
        fits = extra.get("fits", True)
        machines.append({"location_id": loc, "machine": mid, "label": mid, "reachable": extra.get("reachable", True),
                         "current": extra.get("current"), "workstation": extra.get("workstation"),
                         "node_paused": extra.get("paused"),
                         "installable_models": [] if has or not fits else [{"package_id": FISH, "label": "Fish S2"}]})
        models.setdefault(loc, []).append({"machine": mid, "package_id": FISH, "label": "Fish S2",
                                           "cache_state": "cached" if has else "absent", "memory_eligible": fits})
    return {"machines": machines, "locations": [{"id": loc, "local_models": rows} for loc, rows in models.items()]}


class FakeHubs:
    """Every controller at once: shares, installs and the runs they start."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.runs: dict[str, dict] = {}
        self.shares: list[str] = []
        self.installs: list[tuple[str, str | None]] = []   # (target, source url_base or None)
        self.too_old: set[str] = set()
        for name in ("share_model", "install_model", "model_install_status"):
            patcher = patch.object(startup_control, name, getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)

    def share_model(self, loc, mid, package):
        if f"{loc}/{mid}" in self.too_old:
            raise startup_control.StartupError(404, {"ok": False, "code": "HUB_TOO_OLD",
                                                     "detail": startup_control.MODEL_SHARE_TOO_OLD})
        self.shares.append(f"{loc}/{mid}")
        return {"ok": True, "share": {"url_base": f"http://{mid}/share", "token": "secret-token", "files": [],
                                      "revision": "r1"}}

    def install_model(self, loc, mid, package, *, source=None):
        run = f"run-{len(self.runs) + 1}"
        self.runs[run] = {"id": run, "state": "running", "total_bytes": 100, "bytes_done": 0, "target": f"{loc}/{mid}",
                          "source_used": "lan" if source else "internet"}
        self.installs.append((f"{loc}/{mid}", (source or {}).get("url_base")))
        return {"ok": True, "run": dict(self.runs[run])}

    def model_install_status(self, loc, mid, run):
        return {"ok": True, "run": dict(self.runs[run])}

    def finish_all(self) -> None:
        for run in self.runs.values():
            if run["state"] == "running":
                run.update(state="done", bytes_done=100)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gateway-lan-share-case-")
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for patcher in (patch.object(config, "DATA_DIR", self.data),
                        patch.object(model_share, "_ensure_thread", lambda: None),
                        patch.object(fleet, "owner_paused_at", lambda cid: None),
                        patch.object(fleet, "roster", lambda: [{"id": cid} for cid in model_share.DEFAULT_NETWORKS])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def entries(self) -> dict[str, dict]:
        return {e["key"]: e for e in model_share.read_plans()[0]["macs"]}


class NetworkTest(Case):
    def test_the_owners_networks_are_written_once_when_the_file_is_missing(self) -> None:
        self.assertFalse(model_share.networks_path().exists())
        table = model_share.networks()
        self.assertEqual(table["locations"][A1], "A")
        self.assertEqual(table["locations"][B1], "B")
        self.assertEqual(table["locations"][SHOPS], "C")
        self.assertEqual(table["machines"], {}, "no shop iMac is given a network of its own")
        model_share.set_network(A1, "")
        self.assertNotIn(A1, model_share.networks()["locations"], "a cleared name stays cleared, never re-seeded")

    def test_a_macs_own_network_beats_its_locations_and_empty_goes_back(self) -> None:
        self.assertEqual(model_share.network_of(SHOPS, "imac-3"), "C", "a shop iMac defaults to its group's network")
        model_share.set_network(SHOPS, " B ", "imac-3")
        self.assertEqual(model_share.network_of(SHOPS, "imac-3"), "B")
        self.assertEqual(model_share.network_of(SHOPS, "imac-4"), "C")
        model_share.set_network(SHOPS, None, "imac-3")
        self.assertEqual(model_share.network_of(SHOPS, "imac-3"), "C")
        with self.assertRaises(model_share.ShareError):
            model_share.set_network(SHOPS, "x" * 40)
        with self.assertRaises(model_share.ShareError):
            model_share.set_network("nowhere", "A")

    def test_rows_carry_the_network_in_effect_and_the_override(self) -> None:
        model_share.set_network(SHOPS, "E", "imac-1")
        out = {"locations": [{"id": SHOPS}], "machines": [{"location_id": SHOPS, "machine": "imac-1"},
                                                          {"location_id": SHOPS, "machine": "imac-2"}]}
        model_share.stamp_networks(out)
        self.assertEqual(out["locations"][0]["network"], "C")
        self.assertEqual([(m["network"], m["network_override"]) for m in out["machines"]], [("E", "E"), ("C", None)])

    def test_the_admin_routes_set_and_refuse(self) -> None:
        self.assertEqual(admin.fleet_location_network(B1, {"network": "D"})["networks"]["locations"][B1], "D")
        answer = admin.fleet_machine_network(B1, "node-1", {"network": "A"})
        self.assertEqual(answer["networks"]["machines"][f"{B1}/node-1"], "A")
        refused = admin.fleet_location_network("nowhere", {"network": "A"})
        self.assertEqual((refused.status_code, json.loads(refused.body)["code"]), (404, "NOT_FOUND"))


class SpreadTest(Case):
    def setUp(self) -> None:
        super().setUp()
        self.hubs = FakeHubs(self)

    def test_a_mac_copies_from_its_own_network_never_another(self) -> None:
        fleet_read = snapshot((A1, "1101", True), (A2, "1201", False), (B1, "1301", False), (SHOPS, "1501", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        installs = dict(self.hubs.installs)
        self.assertEqual(installs[f"{A2}/1201"], "http://1101/share", "1201 copies from 1101 on network A")
        self.assertIsNone(installs[f"{B1}/1301"], "network B has no copy: the internet, not 1101 on A")
        self.assertIsNone(installs[f"{SHOPS}/1501"], "network C has no copy either")
        self.assertEqual(self.hubs.shares, [f"{A1}/1101"])
        text = model_share.text(self.entries()[f"{A2}/1201"])
        self.assertEqual(text, "Copying 0% from 1101 (LAN)")
        self.hubs.finish_all()
        model_share.tick(fleet_read)
        lines = {e["key"]: model_share.text(e) for e in self.entries().values()}
        self.assertEqual(lines[f"{A2}/1201"], "Installed from 1101 (LAN)")
        self.assertEqual(lines[f"{B1}/1301"], "Installed from the internet")
        self.assertEqual(model_share.read_plans()[0]["state"], "finished")

    def test_one_internet_download_per_network_then_a_tree_of_at_most_two_per_source(self) -> None:
        targets = [f"11{n:02d}" for n in range(1, 9)]
        fleet_read = snapshot(*[(A1, mid, False) for mid in targets])
        model_share.start(FISH, fleet_read)
        peak = 0
        for _ in range(10):
            model_share.tick(fleet_read)
            copying: dict[str, int] = {}
            for entry in self.entries().values():
                if entry["state"] == "copying":
                    copying[entry["from_key"]] = copying.get(entry["from_key"], 0) + 1
            peak = max([peak, *copying.values()])
            self.assertLessEqual(sum(e["state"] == "downloading" for e in self.entries().values()), 1)
            self.hubs.finish_all()
        self.assertEqual(sum(source is None for _, source in self.hubs.installs), 1, "network A pulled it once")
        self.assertEqual(peak, 2, "each source lent to two at once, never more")
        self.assertTrue(all(e["state"] == "done" for e in self.entries().values()))
        lenders = {e.get("from_key") for e in self.entries().values()} - {None}
        self.assertGreater(len(lenders), 1, "finished Macs lent in turn (a tree, not a star)")

    def test_a_mac_waits_for_a_busy_source_rather_than_the_internet(self) -> None:
        fleet_read = snapshot((A1, "1101", True), *[(A1, f"110{n}", False) for n in range(2, 6)])
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        states = sorted(e["state"] for e in self.entries().values())
        self.assertEqual(states, ["copying", "copying", "have", "waiting", "waiting"])
        waiting = [e for e in self.entries().values() if e["state"] == "waiting"]
        self.assertEqual(waiting[0]["detail"], "Waiting for a Mac on network A to copy from.")
        self.assertTrue(all(source for _, source in self.hubs.installs), "nobody went to the internet")

    def test_skips_lite_mode_this_mac_paused_and_offline(self) -> None:
        fleet_read = snapshot((A1, "1101", False),
                              (SHOPS, "imac-1", False, {"fits": False, "workstation": {"enabled": True}}),
                              (A1, "1102", False, {"reachable": False}),
                              (fleet.LOCAL_ID, "local", False))
        with patch.object(fleet, "owner_paused_at", lambda cid: 1.0 if cid == fleet.LOCAL_ID else None):
            model_share.start(FISH, fleet_read)
        entries = self.entries()
        self.assertEqual(entries[f"{SHOPS}/imac-1"]["detail"], "Lite Mode: this model is not one Lite Mode runs.")
        self.assertEqual(entries[f"{A1}/1102"]["detail"], "Offline.")
        self.assertEqual(entries["local/local"]["detail"], "This Mac is paused for generation.")
        self.assertEqual(entries[f"{A1}/1101"]["state"], "waiting")
        with self.assertRaises(model_share.ShareError) as refused:
            model_share.start(FISH, fleet_read)
        self.assertEqual(refused.exception.body["code"], "ALREADY_RUNNING")

    def test_this_mac_never_lends_its_127_address(self) -> None:
        model_share.set_network(fleet.LOCAL_ID, "A")
        fleet_read = snapshot((fleet.LOCAL_ID, "local", True), (A1, "1101", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual((self.hubs.shares, self.hubs.installs), ([], [(f"{A1}/1101", None)]))

    def test_an_older_hub_means_the_internet_for_that_mac_with_the_sentence(self) -> None:
        self.hubs.too_old.add(f"{A1}/1101")
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        entry = self.entries()[f"{A1}/1102"]
        self.assertEqual((entry["state"], self.hubs.installs), ("downloading", [(f"{A1}/1102", None)]))
        self.assertIn("needs Studio Hub 0.6.146", entry["note"])

    def test_a_gateway_restart_keeps_following_the_same_downloads(self) -> None:
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual(len(self.hubs.installs), 1)
        # "Restart": nothing survives but the file; the next start resumes the worker and the same run.
        with patch.object(model_share, "_ensure_thread") as resumed:
            model_share.resume_on_startup()
        resumed.assert_called_once()
        self.hubs.finish_all()
        model_share.tick(fleet_read)
        self.assertEqual(len(self.hubs.installs), 1, "no second download was started")
        self.assertEqual(self.entries()[f"{A1}/1102"]["state"], "done")
        self.assertNotIn("secret-token", model_share.plans_path().read_text(), "a share token is never stored")

    def test_the_view_groups_by_network(self) -> None:
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False), (B1, "1301", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        plan = model_share.status()[0]
        self.assertEqual([(n["title"], n["done"], n["total"]) for n in plan["networks"]],
                         [("Network A", 1, 2), ("Network B", 0, 1)])
        self.assertEqual(plan["networks"][0]["macs"][1]["text"], "Copying 0% from 1101 (LAN)")
        self.assertEqual(model_share.stop(plan["id"])["state"], "stopped")


class RelayTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="gateway-lan-share-relay-")
        self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(config, "DATA_DIR", Path(tmp.name)),
                        patch.object(startup_control, "_target", lambda c: ("http://100.64.0.2:47873", "fleet-token")),
                        patch.object(fleet, "invalidate")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, fn, response, **kwargs):
        seen: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or response))
        try:
            return fn("ctl1", "node-1", FISH, client=client, **kwargs), seen
        except startup_control.StartupError as error:
            return error, seen

    def test_share_goes_to_the_sources_controller_and_an_old_hub_says_so(self) -> None:
        share = {"url_base": "http://100.64.0.9:47873/api/hub/model-share/t", "token": "t", "files": []}
        answer, seen = self.call(startup_control.share_model, httpx.Response(200, json={"ok": True, "share": share}))
        self.assertEqual(seen[0].url.path, f"/api/hub/fleet/nodes/node-1/model-packages/{FISH}/share")
        self.assertEqual(answer["share"]["token"], "t")
        error, _ = self.call(startup_control.share_model, httpx.Response(404, json={"detail": "Not Found"}))
        self.assertEqual(error.body["code"], "HUB_TOO_OLD")
        self.assertIn("0.6.146", error.body["detail"])

    def test_the_download_carries_the_source_and_the_token_is_not_audited(self) -> None:
        source = {"url_base": "http://100.64.0.9:47873/api/hub/model-share/x", "token": "very-secret", "files": [],
                  "revision": "r1"}
        _, seen = self.call(startup_control.install_model,
                            httpx.Response(200, json={"ok": True, "run": {"id": "run-1"}}), source=source)
        self.assertEqual(json.loads(seen[0].content), {"source": source})
        self.assertNotIn("very-secret", startup_control.audit_path().read_text())
        _, seen = self.call(startup_control.install_model, httpx.Response(200, json={"ok": True, "run": {"id": "r"}}))
        self.assertEqual(json.loads(seen[0].content), {}, "no source: the internet, exactly as before")


if __name__ == "__main__":
    unittest.main()
