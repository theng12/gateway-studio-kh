"""LAN model sharing (owner decision 2026-10-05, `INTERFACES-0146.md`): networks, the per-network spread, the relay.

What these pin:

* networks: the owner's names are written once when the file is missing, a Mac's own network beats its
  location's, an empty override goes back to the location's, and a shop iMac with none set is on its group's;
* the spread: a Mac copies only from a Mac on its own named network, never across; a network with no copy
  downloads from the internet once at a time; every source lends to at most two; finished Macs lend in turn;
* skips: Lite Mode for a model it does not run, This Mac while paused for generation, offline Macs;
* an older source Hub (no share route) leaves that network waiting, with the HUB_TOO_OLD sentence;
* the plan lives in a file: a Gateway restart keeps following the same downloads instead of starting new ones.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
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
    machines, models, versions = [], {}, {}
    for loc, mid, has, *extra in macs:
        extra = extra[0] if extra else {}
        fits = extra.get("fits", True)
        machines.append({"location_id": loc, "machine": mid, "label": mid, "reachable": extra.get("reachable", True),
                         "current": extra.get("current"), "workstation": extra.get("workstation"),
                         "node_paused": extra.get("paused"), "hub_version": extra.get("hub_version", "0.6.174"),
                         "installable_models": [] if has or not fits else [{"package_id": FISH, "label": "Fish S2"}]})
        models.setdefault(loc, []).append({"machine": mid, "package_id": FISH, "label": "Fish S2",
                                           "cache_state": "cached" if has else "absent", "memory_eligible": fits})
        versions[loc] = extra.get("controller_version", versions.get(loc, "0.6.174"))
    return {"machines": machines, "locations": [
        {"id": loc, "version": versions[loc], "local_models": rows} for loc, rows in models.items()]}


class FakeHubs:
    """Every controller at once: shares, installs and the runs they start."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.runs: dict[str, dict] = {}
        self.shares: list[str] = []
        self.installs: list[tuple[str, str | None]] = []   # (target, source url_base or None)
        self.peer_only: list[bool] = []
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

    def install_model(self, loc, mid, package, *, source=None, peer_only=False):
        run = f"run-{len(self.runs) + 1}"
        self.runs[run] = {"id": run, "state": "running", "total_bytes": 100, "bytes_done": 0, "target": f"{loc}/{mid}",
                          "source_used": "lan" if source else "internet"}
        self.installs.append((f"{loc}/{mid}", (source or {}).get("url_base")))
        self.peer_only.append(peer_only)
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

    def test_c_space_c_and_space_c_are_one_network_and_pair(self) -> None:
        model_share._write_json(model_share.networks_path(), {"locations": {A1: "c "}, "machines": {}})  # noqa: SLF001
        model_share.set_network(A2, "C")
        model_share.set_network(B1, " c")
        self.assertEqual(model_share.networks()["locations"], {A1: "C", A2: "C", B1: "C"}, "an old 'c ' reads as C")
        fleet_read = snapshot((A1, "1101", True), (A2, "1201", False), (B1, "1301", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual(dict(self.hubs.installs), {f"{A2}/1201": "http://1101/share", f"{B1}/1301": "http://1101/share"})

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

    def test_lan_starts_are_strict_and_a_known_unavailable_source_blocks_an_internet_seed(self) -> None:
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        plan = model_share.read_plans()[0]
        plan["resting"] = {f"{A1}/1101": time.time() + 60}
        model_share._save([plan])
        model_share.tick(fleet_read)
        self.assertEqual(self.hubs.installs, [], "a resting verified source still owns the network copy")
        self.assertIn("verified copy", self.entries()[f"{A1}/1102"]["detail"])

        plan = model_share.read_plans()[0]
        plan["resting"] = {}
        model_share._save([plan])
        model_share.tick(fleet_read)
        self.assertEqual(self.hubs.peer_only, [True])

    def test_an_unreachable_verified_source_waits_instead_of_starting_another_internet_copy(self) -> None:
        fleet_read = snapshot((A1, "1101", True, {"reachable": False}), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual(self.hubs.installs, [])
        self.assertIn("verified copy", self.entries()[f"{A1}/1102"]["detail"])

    def test_a_durable_verified_source_missing_from_the_fleet_rows_still_owns_its_network_copy(self) -> None:
        model_share.start(FISH, snapshot((A1, "1101", True), (A1, "1102", False)))
        without_source_row = snapshot((A1, "1102", False))
        model_share.tick(without_source_row)
        self.assertEqual(self.hubs.installs, [])
        self.assertIn("verified copy", self.entries()[f"{A1}/1102"]["detail"])

    def test_old_or_unknown_target_waits_for_the_strict_lan_protocol(self) -> None:
        for version in ("0.6.173", None):
            with self.subTest(version=version):
                model_share._save([])
                self.hubs.installs.clear()
                self.hubs.peer_only.clear()
                fleet_read = snapshot((A1, "1101", True),
                                      (A1, "1102", False, {"hub_version": version}))
                model_share.start(FISH, fleet_read)
                model_share.tick(fleet_read)
                self.assertEqual(self.hubs.installs, [])
                detail = self.entries()[f"{A1}/1102"]["detail"]
                self.assertIn("Studio Hub 0.6.174", detail)
                self.assertIn("unknown" if version is None else version, detail)

    def test_an_old_controller_waits_instead_of_dropping_the_strict_protocol_bit(self) -> None:
        fleet_read = snapshot((A1, "1101", True),
                              (A1, "1102", False, {"controller_version": "0.6.173"}))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual(self.hubs.installs, [])
        detail = self.entries()[f"{A1}/1102"]["detail"]
        self.assertIn("controller", detail)
        self.assertIn("0.6.173", detail)

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

    def test_a_hub_memory_refusal_is_a_skip_not_a_failure(self) -> None:
        """2026-10-08: Qwen Low-RAM's plan queued 8 GB Macs; each Hub refused ("needs a Mac with at least
        16 GB of memory ... Nothing was downloaded") and the plan called that Failed. It is a skip, and the
        network's next Mac starts its download in the same pass."""
        refused = {f"{A1}/1101"}
        real = self.hubs.install_model

        def install(loc, mid, package, *, source=None):
            if f"{loc}/{mid}" in refused:
                raise startup_control.StartupError(409, {"detail": (
                    f"Not on this Mac: {package} needs a Mac with at least 16 GB of memory and this one has "
                    "8 GB, so it could never run here. Nothing was downloaded.")})
            return real(loc, mid, package, source=source)

        fleet_read = snapshot((A1, "1101", False), (A1, "1102", False))
        with patch.object(startup_control, "install_model", install):
            model_share.start(FISH, fleet_read)
            model_share.tick(fleet_read)
        entries = self.entries()
        self.assertEqual(model_share.text(entries[f"{A1}/1101"]), "Skipped: Not enough memory for this model.")
        self.assertEqual(entries[f"{A1}/1102"]["state"], "downloading")
        self.hubs.finish_all()
        model_share.tick(fleet_read)
        self.assertEqual(model_share.read_plans()[0]["state"], "finished")
        self.assertFalse(any(e["state"] == "failed" for e in self.entries().values()))

    def test_a_mac_below_the_packages_tier_is_not_offered_it(self) -> None:
        """The Image worker's `memory_eligible` is host-level, so the package tier decides (fleet read)."""
        location = {"local_models": [{"machine": "m", "package_id": "qwen-image-2-1-mflux-q4", "label": "Qwen",
                                      "memory_eligible": True, "min_unified_memory_gb": 16.0,
                                      "cache_state": "not_installed"}]}
        offered = lambda gb: [m["package_id"] for m in fleet.installable_models(location, "m", gb)]  # noqa: E731
        self.assertEqual(offered(8.59), [], "an 8 GB Mac")
        self.assertEqual(offered(17.18), ["qwen-image-2-1-mflux-q4"], "a 16 GB Mac (16 GiB)")
        self.assertEqual(offered(None), ["qwen-image-2-1-mflux-q4"], "unknown memory: the Hub decides")

    def test_this_mac_never_lends_its_127_address(self) -> None:
        model_share.set_network(fleet.LOCAL_ID, "A")
        fleet_read = snapshot((fleet.LOCAL_ID, "local", True), (A1, "1101", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        self.assertEqual((self.hubs.shares, self.hubs.installs), ([], [(f"{A1}/1101", None)]))

    def test_an_old_source_hub_keeps_the_verified_copy_reserved_for_lan(self) -> None:
        self.hubs.too_old.add(f"{A1}/1101")
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)
        entry = self.entries()[f"{A1}/1102"]
        self.assertEqual((entry["state"], self.hubs.installs), ("waiting", []))
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


    def test_a_copy_that_fell_back_says_so_and_holds_the_internet_slot(self) -> None:
        macs = [(SHOPS, "1500", True), (SHOPS, "1501", False), (SHOPS, "1502", False), (SHOPS, "1503", False)]
        model_share.start(FISH, snapshot(*macs))
        model_share.tick(snapshot(*macs))
        run = next(r for r in self.hubs.runs.values() if r["target"] == f"{SHOPS}/1501")
        run.update(source_used="internet", lan_error="URLError: <urlopen error [Errno 61] Connection refused>")
        macs[0] = (SHOPS, "1500", True, {"reachable": False})       # no LAN source left on network C
        model_share.tick(snapshot(*macs))
        entries = self.entries()
        self.assertEqual(entries[f"{SHOPS}/1501"]["state"], "downloading")
        self.assertEqual(model_share.text(entries[f"{SHOPS}/1501"]),
                         "Downloading 0% from the internet (the copy from 1500 (LAN) did not work: "
                         "URLError: <urlopen error [Errno 61] Connection refused>)")
        self.assertEqual(entries[f"{SHOPS}/1503"]["detail"], "Waiting for the internet download on network C to finish.")
        self.assertEqual(len(self.hubs.installs), 2, "the hidden fallback is network C's one internet download")


class NetworksLockTest(Case):
    def test_networks_answers_while_a_pass_is_blocked_on_a_relay(self) -> None:
        FakeHubs(self)
        entered, release = threading.Event(), threading.Event()
        fleet_read = snapshot((A1, "1101", True), (A1, "1102", False))
        model_share.start(FISH, fleet_read)
        model_share.tick(fleet_read)

        def stuck(*args):                    # an unreachable controller: the relay waits up to 40 s
            entered.set()
            release.wait(10)
            return {"ok": True, "run": {"state": "running"}}
        with patch.object(startup_control, "model_install_status", stuck):
            worker = threading.Thread(target=model_share.tick, args=(fleet_read,))
            worker.start()
            self.assertTrue(entered.wait(5))
            began = time.monotonic()
            self.assertEqual(model_share.networks()["locations"][A1], "A")
            self.assertLess(time.monotonic() - began, 1.0, "the Fleet page does not wait behind the pass")
            release.set()
            worker.join(5)


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
                            httpx.Response(200, json={"ok": True, "run": {"id": "run-1"}}),
                            source=source, peer_only=True)
        self.assertEqual(json.loads(seen[0].content), {"source": source, "peer_only": True})
        self.assertNotIn("very-secret", startup_control.audit_path().read_text())
        _, seen = self.call(startup_control.install_model, httpx.Response(200, json={"ok": True, "run": {"id": "r"}}))
        self.assertEqual(json.loads(seen[0].content), {}, "no source: the internet, exactly as before")
        error, seen = self.call(startup_control.install_model, httpx.Response(200, json={}),
                                source=None, peer_only=True)
        self.assertEqual((error.status, seen), (400, []))


class AFinishedInstallIsSwitchedOnTest(Case):
    """0.2.150: plan 80e0f8dd921a put Qwen Low-RAM on 1100-1500, but those locations had no decision for it,
    so a remote location's default (off) kept every job on This Mac (`eligible_macs: 1`)."""

    def setUp(self) -> None:
        super().setUp()
        self.hubs = FakeHubs(self)
        from gateway.providers import models as models_module, registry
        from gateway.providers.credentials import CredentialStore
        from gateway.providers.definitions import DISCOVERY_STUDIO_HUB, KIND_STUDIO_HUB, ProviderDefinition

        definitions = {fleet.provider_id_for(loc): ProviderDefinition(
            id=fleet.provider_id_for(loc), display_name=loc, category="multi", kind=KIND_STUDIO_HUB,
            base_url="http://x.invalid", modalities=("tts",), discovery=DISCOVERY_STUDIO_HUB)
            for loc in (A1, A2, B1)}
        self.inventory = models_module.ModelInventory(
            CredentialStore(path=self.data / "credentials.json"), path=self.data / "models-cache.json",
            definition_lookup=definitions.get, definitions_lookup=lambda: list(definitions.values()))
        patcher = patch.object(registry, "models", self.inventory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def decisions(self, loc: str) -> dict:
        return json.loads((self.data / "models-cache.json").read_text())["enabled"].get(fleet.provider_id_for(loc), {})

    def test_on_where_undecided_an_off_stands_and_nothing_else_changes(self) -> None:
        fleet_read = snapshot((A1, "1101", True), (A2, "1201", False), (B1, "1301", False))
        for loc in fleet_read["locations"]:
            for row in loc["local_models"]:
                row["model"] = "org/fish-4bit"
            loc["local_models"].append({"machine": "x", "package_id": "pocket-tts-4bit",
                                        "model": "mlx-community/pocket-tts-4bit"})
        self.inventory.set_model_enabled(fleet.provider_id_for(B1), "org/fish-4bit", False)   # the owner's off
        self.inventory.set_model_enabled(fleet.provider_id_for(A2), "org/other", False)
        model_share.start(FISH, fleet_read)
        for _ in range(4):
            model_share.tick(fleet_read)
            self.hubs.finish_all()
        self.assertTrue(all(e["state"] in ("done", "have") for e in self.entries().values()))
        self.assertEqual(self.decisions(A2), {"org/other": False, "org/fish-4bit": True})
        self.assertEqual(self.decisions(B1), {"org/fish-4bit": False}, "an explicit off is never overridden")
        self.assertEqual(self.decisions(A1), {}, "a Mac that already had it installed nothing here")


if __name__ == "__main__":
    unittest.main()
