#!/usr/bin/env python3
"""Remote final rendering: the owner's per-Mac switch, its relay, and who may be sent a render.

Offline: no socket, no renderer, no real Hub. Fleet snapshots are literal fixtures, the controller relay is
an `httpx.MockTransport`, and the switch file lives in a temp data directory.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-render-optin-")

from gateway import admin_session, api, config, fleet, render_optin, startup_control  # noqa: E402

PROFILE = "a" * 64
NOW = 1_700_000_000.0
LAN = [ipaddress.ip_network("192.168.1.0/24")]
#: What a Hub that was told to accept unqualified work adds to its (production-shaped) render row.
ACCEPTING = {"provenance": render_optin.PROVENANCE, "accepted_by": "gateway-admin", "accepted_at": 1_700_000_000}


def worker(machine: str, *, hardware: str = "Apple M4", production: bool = False,
           accepted: object = None, **changes: object) -> dict[str, object]:
    """A `video.final_render` worker row as the controller republishes it. `accepted` is the Hub's evidence
    that its owner opted in; such a row is production-shaped, exactly as Studio Hub publishes it."""
    row: dict[str, object] = {
        "machine": machine, "physical_machine_id": machine,
        "online": True, "ready": True, "busy": False, "drained": False,
        "maintenance": False, "quarantined": False,
        "supported_operations": ["video.final_render"],
        "hardware_class": hardware, "profile_digest": PROFILE,
        "tools_digest": "4" * 64, "compiler_digest": "5" * 64,
        "qualification_digest": "6" * 64, "selected_tier": "software-h264",
        "exact_encoder": "libx264", "font_support": False,
        "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                   "min_available_memory_bytes": 7_900_000_000, "output_limit_bytes": 1},
        "profile_execution_bounds": {"max_duration_sec": 1},
        "production_eligible": production, "production_enabled": production,
        "available_capacity": {"slots": 1, "slots_total": 1},
    }
    if accepted is not None:
        row.update(production_eligible=True, production_enabled=True)
        row[render_optin.OPT_IN_KEY] = accepted
    row.update(changes)
    return row


def location(macs: dict[str, dict[str, object]], *, location_id: str = "roster-a") -> dict[str, object]:
    """One reachable roster location. `macs` maps machine id -> {"address", "worker", "chip", "label"}."""
    return {
        "id": location_id, "label": "Shop", "local": False, "reachable": True,
        "ready": True, "drained": False, "paused_at": None,
        "site_id": "site-a", "controller_id": "hub-a",
        "members_from": None, "members_stale_seconds": None,
        "machines": [{
            "machine": mid, "reachable": True, "label": spec.get("label", mid),
            "chip": spec.get("chip", "Apple M4"), "memory_gb": 16.0, "available_gb": 8.0,
            "disk_free_gb": 100.0, "current_job": None,
            **({"network": spec["network"]} if "network" in spec else {}),
        } for mid, spec in macs.items()],
        "nodes": [{
            "id": mid, "state": "reachable", "paused": False, "current_job": None,
            "on_previous_token": False, "address": spec.get("address"), "version": spec.get("version"),
            "priority_order": ["video"], "workstation": None,
        } for mid, spec in macs.items()],
        "workers": [spec["worker"] for spec in macs.values()],
    }


def view(*locations: dict[str, object]) -> dict[str, object]:
    return {"generated_at": NOW, "locations": list(locations)}


class Fleet:
    """Patches the clock, the snapshot, the roster and this Mac's networks around one block."""

    def __init__(self, *locations: dict[str, object], networks: list = LAN) -> None:
        self.view = view(*locations)
        self.patches = [
            patch.object(fleet.time, "time", return_value=NOW),
            patch.object(fleet, "snapshot", return_value=self.view),
            patch.object(fleet, "roster", return_value=[{"id": row["id"]} for row in locations]),
            patch.object(render_optin, "local_networks", return_value=networks),
        ]

    def __enter__(self) -> "Fleet":
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for item in reversed(self.patches):
            item.stop()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="render-optin-")
        self.addCleanup(self.tmp.cleanup)
        for item in (patch.object(config, "DATA_DIR", Path(self.tmp.name)),):
            item.start()
            self.addCleanup(item.stop)


class DefaultOffTests(Case):
    def test_every_mac_is_off_until_the_owner_says_otherwise(self) -> None:
        self.assertFalse(render_optin.optin_path().exists())
        self.assertEqual(render_optin.enabled_keys(), frozenset())
        mac = {"address": "http://192.168.1.20:47873", "worker": worker("mac-a", accepted=ACCEPTING)}
        with Fleet(location({"mac-a": mac})):
            rows = render_optin.workers(fleet.snapshot())
        self.assertEqual([(row["machine_id"], row["opted_in"]) for row in rows], [("mac-a", False)])
        # The Hub says it accepts, but nothing was switched on here: it is not sent anything.
        self.assertTrue(rows[0]["hub_accepting"])
        self.assertIn("switched off here", rows[0]["note"])

    def test_an_unreadable_or_malformed_switch_file_is_off_not_an_error(self) -> None:
        path = render_optin.optin_path()
        for text in ("", "{", "[]", '{"machines": []}', '{"machines": {"roster-a/mac-a": "yes"}}',
                     '{"machines": {"roster-a/mac-a": {"enabled": "true"}}}'):
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                self.assertFalse(render_optin.is_enabled("roster-a", "mac-a"))

    def test_switching_off_removes_the_entry_and_the_file_is_owner_only(self) -> None:
        render_optin.set_enabled("roster-a", "mac-a", True)
        self.assertTrue(render_optin.is_enabled("roster-a", "mac-a"))
        self.assertEqual(oct(render_optin.optin_path().stat().st_mode & 0o777), "0o600")
        render_optin.set_enabled("roster-a", "mac-a", False)
        self.assertFalse(render_optin.is_enabled("roster-a", "mac-a"))
        self.assertEqual(json.loads(render_optin.optin_path().read_text())["machines"], {})


class ClassificationTests(unittest.TestCase):
    def test_published_addresses_are_classified_from_the_networks_this_mac_is_on(self) -> None:
        nets = [ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("10.0.0.0/8")]
        for address, expected in (
            ("http://192.168.1.20:47873", render_optin.SAME_NETWORK),
            ("192.168.1.20", render_optin.SAME_NETWORK),
            ("https://10.4.5.6", render_optin.SAME_NETWORK),
            ("http://192.168.2.20:47873", render_optin.OTHER_LAN),     # a private LAN, but not ours
            ("http://172.16.0.9:1", render_optin.OTHER_LAN),
            ("http://169.254.3.4:1", render_optin.OTHER_LAN),          # link-local is a LAN, not ours
            ("http://100.101.102.103:47873", render_optin.TAILSCALE),
            ("100.64.0.1", render_optin.TAILSCALE),
            ("100.127.255.254", render_optin.TAILSCALE),
            ("http://[fd7a:115c:a1e0::1]:47873", render_optin.TAILSCALE),
            ("http://100.128.0.1", render_optin.UNKNOWN),              # just outside 100.64.0.0/10: public
            ("http://8.8.8.8:80", render_optin.UNKNOWN),
            ("http://mac-mini.local:47873", render_optin.UNKNOWN),     # a name is never resolved or guessed
            ("http://127.0.0.1:47873", render_optin.UNKNOWN),
            ("", render_optin.UNKNOWN), (None, render_optin.UNKNOWN), (12, render_optin.UNKNOWN),
        ):
            with self.subTest(address=address):
                self.assertEqual(render_optin.classify(address, nets), expected)
        # No known network (the interface listing could not be read): nothing is "ours".
        self.assertEqual(render_optin.classify("http://192.168.1.20", []), render_optin.OTHER_LAN)

    def test_a_macs_own_interface_list_outranks_the_address_it_is_dialled_on(self) -> None:
        nets = [ipaddress.ip_network("192.168.1.0/24")]

        def iface(address: str, prefix: int = 24) -> dict:
            return {"interface": "en0", "address": address, "prefix_length": prefix}

        tail = "http://100.101.1.1:47873"
        for machine, address, expected in (
            ({"network": [iface("192.168.1.77")]}, tail, ("same_network", "interfaces")),
            ({"network": [iface("192.168.1.77", 16)]}, None, ("same_network", "interfaces")),
            ({"network": [iface("192.168.9.5")]}, tail, ("tailscale", "address")),
            ({"network": [iface("192.168.9.5")]}, None, ("other_lan", "interfaces")),
            ({"network": [iface("192.168.9.5")]}, "http://192.168.9.5:1", ("other_lan", "address")),
            # Tailscale, link-local and loopback interfaces are not a shared network.
            ({"network": [iface("100.101.1.1", 32), iface("169.254.1.1", 16), iface("127.0.0.1", 8)]},
             tail, ("tailscale", "address")),
            ({"network": [{"address": "x"}, "junk", {"address": "192.168.1.5", "prefix_length": "nope"}]},
             None, ("unknown", "address")),
            ({"network": []}, None, ("unknown", "address")),
            # An older Hub sends none: the dialled address is all there is, and the source says so.
            ({}, "http://192.168.1.50:1", ("same_network", "address")),
            ({"network": None}, tail, ("tailscale", "address")),
            (None, None, ("unknown", "address")),
        ):
            with self.subTest(machine=machine, address=address):
                self.assertEqual(render_optin.classify_machine(machine, address, nets), expected)

    def test_tailscale_never_counts_as_the_local_network(self) -> None:
        text = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.5 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 fe80::1%en0 prefixlen 64 secured scopeid 0xb
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.101.1.1 --> 100.101.1.1 netmask 0xffffffff
en1: flags=8863<UP> mtu 1500
\tinet 169.254.9.9 netmask 0xffff0000 broadcast 169.254.255.255
bridge0: flags=8863<UP> mtu 1500
\tinet 10.1.2.3 netmask 0xffff0000 broadcast 10.1.255.255
"""
        self.assertEqual(render_optin.parse_ifconfig(text), [
            ipaddress.ip_network("192.168.1.0/24"), ipaddress.ip_network("10.1.0.0/16"),
        ])
        self.assertEqual(render_optin.parse_ifconfig(""), [])
        self.assertEqual(render_optin.parse_ifconfig("garbage inet 1.2.3.4 netmask nope"), [])

    def test_an_unreadable_interface_listing_is_no_networks(self) -> None:
        with patch.object(render_optin.subprocess, "run", side_effect=OSError("no ifconfig")), \
                patch.dict(render_optin._nets, {"at": -1e9, "value": []}):
            self.assertEqual(render_optin.local_networks(), [])


class EligibilityTests(Case):
    def candidates(self, *locations: dict[str, object], qualification: bool = False) -> list[dict]:
        with Fleet(*locations):
            return fleet.render_candidates(PROFILE, qualification=qualification)

    def test_with_nothing_switched_on_selection_is_exactly_the_old_selection(self) -> None:
        macs = {
            "qualified": {"address": "http://192.168.1.10:1", "worker": worker("qualified", production=True)},
            # A 0.6.195 Mac: no marker, no capacity.
            "old": {"address": "http://192.168.1.11:1", "worker": worker("old")},
            # A new Hub that accepts, which the owner has NOT switched on. Its row is production-shaped,
            # so 0.2.195 would have selected it as if qualified; the owner's switch is the control.
            "accepts": {"address": "http://192.168.1.12:1", "worker": worker("accepts", accepted=ACCEPTING)},
        }
        selected = self.candidates(location(macs))
        self.assertEqual([row["node_id"] for row in selected], ["qualified"])
        # A qualified row carries none of the new keys.
        self.assertNotIn("authority", selected[0])
        self.assertNotIn("network", selected[0])
        self.assertEqual(self.candidates(location(macs), qualification=True), selected)

    def test_switched_on_and_confirmed_macs_follow_qualified_ones_same_network_first(self) -> None:
        macs = {
            "tail-m4": {"address": "http://100.101.1.1:1", "worker": worker("tail-m4", accepted=ACCEPTING)},
            "far-m4": {"address": "http://192.168.7.7:1", "worker": worker("far-m4", accepted=ACCEPTING)},
            "near-m1": {"address": "http://192.168.1.40:1",
                        "worker": worker("near-m1", hardware="Apple M1", accepted=ACCEPTING)},
            "near-m4": {"address": "http://192.168.1.41:1", "worker": worker("near-m4", accepted=ACCEPTING)},
            "named": {"address": "http://shop-mini.local:1", "worker": worker("named", accepted=ACCEPTING)},
            "qualified": {"address": "http://100.101.9.9:1", "worker": worker("qualified", production=True)},
        }
        for machine in ("tail-m4", "far-m4", "near-m1", "near-m4", "named"):
            render_optin.set_enabled("roster-a", machine, True)
        selected = self.candidates(location(macs))
        self.assertEqual(
            [(row["node_id"], row.get("network")) for row in selected],
            [("qualified", None),
             # same network first (its own hardware order inside it), then other LAN, Tailscale, unknown
             ("near-m4", "same_network"), ("near-m1", "same_network"),
             ("far-m4", "other_lan"), ("tail-m4", "tailscale"), ("named", "unknown")])
        self.assertTrue(all(row["authority"] == render_optin.ACCEPTED for row in selected[1:]))
        # Production capacity is asked for first, so a qualified Mac still wins.
        self.assertEqual(selected[0]["node_id"], "qualified")
        # The capability snapshot is the Hub's own row, so it keeps the evidence of what it was.
        self.assertEqual(selected[1]["capability"][render_optin.OPT_IN_KEY], ACCEPTING)
        self.assertNotIn(render_optin.OPT_IN_KEY, selected[0]["capability"])

    def test_only_the_switched_on_mac_is_eligible_and_only_for_its_own_controller(self) -> None:
        macs = {
            "a": {"address": "http://192.168.1.1:1", "worker": worker("a", accepted=ACCEPTING)},
            "b": {"address": "http://192.168.1.2:1", "worker": worker("b", accepted=ACCEPTING)},
        }
        render_optin.set_enabled("roster-a", "a", True)
        render_optin.set_enabled("other-controller", "b", True)   # same machine id, another roster entry
        self.assertEqual([row["node_id"] for row in self.candidates(location(macs))], ["a"])

    def test_mixed_version_fleet_older_hubs_stay_ineligible_without_an_error(self) -> None:
        macs = {
            "old-hub": {"address": "http://192.168.1.2:1", "worker": worker("old-hub")},
            "old-hub-with-leftover-candidates": {
                "address": "http://192.168.1.3:1",
                "worker": worker("old-hub-with-leftover-candidates", qualification_candidates=[])},
            "new-hub": {"address": "http://192.168.1.4:1", "worker": worker("new-hub", accepted=ACCEPTING)},
        }
        for machine in macs:
            render_optin.set_enabled("roster-a", machine, True)   # the owner switched every one on
        with Fleet(location(macs)):
            self.assertEqual([row["node_id"] for row in fleet.render_candidates(PROFILE, qualification=True)],
                             ["new-hub"])
            rows = {row["machine_id"]: row for row in render_optin.workers(fleet.snapshot())}
        self.assertTrue(rows["old-hub"]["opted_in"])
        self.assertFalse(rows["old-hub"]["hub_accepting"])
        self.assertIn("has not confirmed", rows["old-hub"]["note"])
        self.assertTrue(rows["new-hub"]["hub_accepting"])
        self.assertIsNone(rows["new-hub"]["note"])

    def test_a_statement_that_is_not_the_hubs_evidence_is_never_used(self) -> None:
        # Present but wrong: the row cannot be told from a qualified one, so it is excluded, switched on or not.
        for marker in ({"provenance": "production"}, {"provenance": None}, {}, {"mode": "accepted_unqualified"},
                       "accepted_unqualified", ["accepted_unqualified"], True, 1, 0, False, ""):
            macs = {"m": {"address": "http://192.168.1.1:1", "worker": worker(
                "m", production=True, **{render_optin.OPT_IN_KEY: marker})}}
            for opted in (False, True):
                render_optin.set_enabled("roster-a", "m", opted)
                with self.subTest(marker=marker, opted=opted):
                    self.assertEqual(self.candidates(location(macs), qualification=True), [])
        # An explicit null is no claim at all: an ordinary qualified row, selected as it always was.
        plain = {"m": {"address": "http://192.168.1.1:1", "worker": worker(
            "m", production=True, **{render_optin.OPT_IN_KEY: None})}}
        self.assertEqual([row.get("authority") for row in self.candidates(location(plain))], [None])

    def test_a_mac_on_this_lan_is_same_network_even_when_dialled_over_tailscale(self) -> None:
        lan = [{"interface": "en0", "address": "192.168.1.77", "prefix_length": 24}]
        macs = {
            "dialled-tailscale": {"address": "http://100.101.1.1:1", "network": lan,
                                  "worker": worker("dialled-tailscale", accepted=ACCEPTING)},
            "elsewhere": {"address": "http://100.101.1.2:1",
                          "network": [{"interface": "en0", "address": "192.168.9.5", "prefix_length": 24}],
                          "worker": worker("elsewhere", accepted=ACCEPTING)},
            "no-interfaces": {"address": "http://100.101.1.3:1",
                              "worker": worker("no-interfaces", accepted=ACCEPTING)},   # an older Hub's machine row
        }
        for machine in macs:
            render_optin.set_enabled("roster-a", machine, True)
        self.assertEqual([(row["node_id"], row["network"]) for row in self.candidates(location(macs))],
                         [("dialled-tailscale", "same_network"), ("elsewhere", "tailscale"),
                          ("no-interfaces", "tailscale")])

    def test_a_switched_on_mac_still_has_to_pass_every_existing_check(self) -> None:
        render_optin.set_enabled("roster-a", "m", True)
        base = {"m": {"address": "http://192.168.1.1:1", "worker": worker("m", accepted=ACCEPTING)}}
        self.assertEqual(len(self.candidates(location(base))), 1)
        for index, mutate in enumerate((
            lambda row: row["workers"][0].update(busy=True),
            lambda row: row["workers"][0].update(online=False),
            lambda row: row["workers"][0].update(profile_digest="b" * 64),
            lambda row: row["workers"][0].update(hardware_class="Apple M3"),
            lambda row: row["workers"][0].update(available_capacity={"slots": 0, "slots_total": 1}),
            lambda row: row["machines"][0].update(available_gb=7.0),
            lambda row: row["machines"][0].update(current_job={"job_id": "x"}),
            lambda row: row["nodes"][0].update(paused=True),
            lambda row: row.update(drained=True),
            lambda row: row.update(machines=[], nodes=[]),
        )):
            changed = copy.deepcopy(location(base))
            mutate(changed)
            with self.subTest(index=index):
                self.assertEqual(self.candidates(changed), [])
        # The Gateway never sends a render back to a controller's own Mac, switched on or not.
        own = {"hub-a": {"address": "http://192.168.1.9:1", "worker": worker("hub-a", accepted=ACCEPTING)}}
        render_optin.set_enabled("roster-a", "hub-a", True)
        self.assertEqual(self.candidates(location(own)), [])

    def test_trimmed_worker_carries_the_hubs_evidence_only_when_the_hub_sent_it(self) -> None:
        raw = {"service_id": "s", "studio_type": "video", "physical_machine_id": "m",
               "supported_operations": ["video.final_render"], "available_capacity": {"slots": 1}}
        self.assertNotIn(render_optin.OPT_IN_KEY, fleet._trim_worker(raw))
        self.assertEqual(
            fleet._trim_worker({**raw, render_optin.OPT_IN_KEY: ACCEPTING})[render_optin.OPT_IN_KEY],
            ACCEPTING)


class ListingTests(Case):
    def test_workers_are_listed_with_machine_idle_state_and_network_same_network_first(self) -> None:
        macs = {
            "zeta-far": {"address": "http://192.168.9.1:1", "label": "Zeta", "chip": "Apple M2",
                         "version": "0.6.195", "worker": worker("zeta-far")},
            "alpha-tail": {"address": "http://100.100.1.1:1", "label": "Alpha",
                           "worker": worker("alpha-tail", accepted=ACCEPTING)},
            "mid-near": {"address": "http://192.168.1.30:1", "label": "Mid", "chip": "Apple M4",
                         "worker": worker("mid-near", busy=True, accepted=ACCEPTING)},
            "bravo-near": {"address": "http://192.168.1.31:1", "label": "Bravo",
                           "worker": worker("bravo-near", production=True)},
            "off": {"address": "http://192.168.1.32:1", "label": "Off", "worker": worker("off", online=False)},
            "carl-near": {"address": "http://192.168.1.34:1", "label": "Carl", "worker": worker("carl-near")},
            "no-render": {"address": "http://192.168.1.33:1", "label": "NoRender",
                          "worker": worker("no-render", supported_operations=["image.generate"])},
        }
        render_optin.set_enabled("roster-a", "mid-near", True)
        local = copy.deepcopy(location({"x": {"address": "http://192.168.1.99:1", "worker": worker("x")}}))
        local["local"] = True
        with Fleet(location(macs), local):
            rows = render_optin.workers(fleet.snapshot())
        self.assertEqual([row["machine_id"] for row in rows],
                         ["bravo-near", "carl-near", "mid-near", "off", "zeta-far", "alpha-tail"])
        by = {row["machine_id"]: row for row in rows}
        self.assertEqual([row["network"] for row in rows],
                         ["same_network", "same_network", "same_network", "same_network", "other_lan",
                          "tailscale"])
        self.assertEqual(by["mid-near"]["idle_state"], "busy")
        self.assertEqual(by["bravo-near"]["idle_state"], "idle")
        self.assertEqual(by["off"]["idle_state"], "offline")
        self.assertEqual((by["mid-near"]["machine"], by["mid-near"]["chip"], by["mid-near"]["memory_gb"]),
                         ("Mid", "Apple M4", 16.0))
        self.assertEqual(by["zeta-far"]["hub_version"], "0.6.195")
        # Suggested = awake, same network, not yet on, not already qualified.
        self.assertEqual({m: by[m]["suggested"] for m in by},
                         {"bravo-near": False, "carl-near": True, "mid-near": False, "off": False,
                          "zeta-far": False, "alpha-tail": False})
        self.assertEqual(by["bravo-near"]["authority"], "production")
        self.assertEqual(by["mid-near"]["authority"], render_optin.ACCEPTED)
        self.assertEqual(by["zeta-far"]["authority"], "none")


class RelayTests(Case):
    def setUp(self) -> None:
        super().setUp()
        target = patch.object(startup_control, "_target", return_value=("http://controller:47873", "fleet-token"))
        target.start()
        self.addCleanup(target.stop)
        self.seen: list[httpx.Request] = []
        self.macs = {"mac": {"address": "http://192.168.1.20:1", "worker": worker("mac")}}

    def client(self, answer: httpx.Response | Exception) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_switching_on_uses_the_existing_controller_relay_then_persists(self) -> None:
        taken = httpx.Response(200, json={"ok": True, "node": "mac",
                                          "render_authority": {"accept_unqualified": True}})
        with Fleet(location(self.macs)):
            answer = render_optin.set_optin("roster-a", "mac", True, client=self.client(taken))
        request = self.seen.pop()
        self.assertEqual((request.method, request.url.path),
                         ("POST", "/api/hub/fleet/nodes/mac/render-authority"))
        self.assertEqual(request.headers["x-hub-token"], "fleet-token")
        self.assertEqual(json.loads(request.content),
                         {"accept_unqualified": True, "requested_by": "gateway-admin"})
        self.assertEqual(self.seen, [])
        self.assertEqual((answer["ok"], answer["opted_in"], answer["relayed"]), (True, True, True))
        self.assertTrue(render_optin.is_enabled("roster-a", "mac"))
        stored = json.loads(render_optin.optin_path().read_text())
        self.assertEqual(stored["machines"]["roster-a/mac"]["by"], "gateway-admin")
        # The choice survives a restart: it is read from the file, not remembered.
        self.assertEqual(render_optin.enabled_keys(), frozenset({"roster-a/mac"}))

    def test_a_mac_only_reachable_at_its_next_check_in_counts_as_taken_but_stays_unused(self) -> None:
        held = httpx.Response(202, json={"ok": True, "queued": True, "command": "c_1", "node": "mac"})
        with Fleet(location(self.macs)):
            answer = render_optin.set_optin("roster-a", "mac", True, client=self.client(held))
            rows = render_optin.workers(fleet.snapshot())
            selected = fleet.render_candidates(PROFILE, qualification=True)
        self.assertEqual((answer["ok"], answer["opted_in"], answer["queued"]), (True, True, True))
        self.assertTrue(render_optin.is_enabled("roster-a", "mac"))
        # The Hub has not confirmed (no evidence on its row yet), so nothing is sent to it.
        self.assertEqual(selected, [])
        self.assertEqual((rows[0]["opted_in"], rows[0]["hub_accepting"]), (True, False))

    def test_switching_on_is_not_kept_when_the_hub_did_not_take_it(self) -> None:
        for answer in (
            httpx.Response(404, json={"detail": "Not Found"}),                      # controller on Hub 0.6.195
            httpx.Response(403, json={"detail": "the fleet credential may send work to nodes"}),
            httpx.Response(409, json={"ok": False, "code": "RENDER_AUTHORITY_UNSUPPORTED", "detail": "update"}),
            httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "asleep"}),
            httpx.ConnectError("down"),
        ):
            with Fleet(location(self.macs)), self.assertRaises(startup_control.StartupError) as caught:
                render_optin.set_optin("roster-a", "mac", True, client=self.client(answer))
            with self.subTest(answer=repr(answer)[:40]):
                self.assertFalse(render_optin.is_enabled("roster-a", "mac"))
                self.assertGreaterEqual(caught.exception.status, 400)
        # A controller that predates the route says so in words, not a bare 404.
        with Fleet(location(self.macs)), self.assertRaises(startup_control.StartupError) as caught:
            render_optin.set_optin("roster-a", "mac", True,
                                   client=self.client(httpx.Response(404, json={"detail": "Not Found"})))
        self.assertEqual(caught.exception.body["code"], "HUB_TOO_OLD")
        self.assertIn("Update it first", caught.exception.body["detail"])

    def test_switching_off_stops_sending_here_even_when_the_hub_cannot_be_told(self) -> None:
        render_optin.set_enabled("roster-a", "mac", True)
        with Fleet(location(self.macs)):
            answer = render_optin.set_optin(
                "roster-a", "mac", False, client=self.client(httpx.Response(502, json={
                    "ok": False, "code": "NODE_UNREACHABLE", "detail": "asleep"})))
        self.assertFalse(render_optin.is_enabled("roster-a", "mac"))
        self.assertEqual((answer["ok"], answer["opted_in"], answer["relayed"], answer["code"]),
                         (True, False, False, "NODE_UNREACHABLE"))
        self.assertIn("no render will be sent", answer["detail"])
        self.assertEqual(json.loads(self.seen.pop().content)["accept_unqualified"], False)

    def test_switching_off_relays_the_off_when_the_hub_answers(self) -> None:
        render_optin.set_enabled("roster-a", "mac", True)
        with Fleet(location(self.macs)):
            answer = render_optin.set_optin("roster-a", "mac", False,
                                            client=self.client(httpx.Response(200, json={"ok": True})))
        self.assertEqual((answer["opted_in"], answer["relayed"]), (False, True))
        self.assertFalse(render_optin.is_enabled("roster-a", "mac"))

    def test_a_request_that_is_wrong_never_reaches_a_controller_or_the_file(self) -> None:
        with Fleet(location(self.macs)):
            for controller, machine, enabled, status, code in (
                ("roster-a", "mac", "yes", 400, "VALUE_REQUIRED"),
                ("roster-a", "mac", None, 400, "VALUE_REQUIRED"),
                ("roster-a", "ghost", True, 404, "NOT_A_RENDER_MAC"),
                ("other", "mac", True, 404, "NOT_A_RENDER_MAC"),
                ("roster-a", "../mac", True, 404, "NOT_A_RENDER_MAC"),
            ):
                with self.subTest(machine=machine, enabled=enabled), \
                        self.assertRaises(startup_control.StartupError) as caught:
                    render_optin.set_optin(controller, machine, enabled,
                                           client=self.client(httpx.Response(200, json={"ok": True})))
                self.assertEqual((caught.exception.status, caught.exception.body["code"]), (status, code))
        with self.assertRaises(startup_control.StartupError) as caught:
            startup_control.set_node_render_authority("roster-a", "bad id", True,
                                                      client=self.client(httpx.Response(200, json={})))
        self.assertEqual(caught.exception.body["code"], "BAD_MACHINE")
        self.assertEqual(self.seen, [])
        self.assertFalse(render_optin.optin_path().exists())


class AdminAuthTests(Case):
    KEY = {"Authorization": "Bearer optin-key"}
    LISTING = "/admin/api/fleet/remote-rendering"
    SWITCH = "/admin/api/fleet/controllers/roster-a/machines/mac/render-optin"

    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(os.environ, {"GATEWAY_HOST": "0.0.0.0", "GATEWAY_API_KEY": "optin-key"})
        env.start()
        self.addCleanup(env.stop)
        self.phone = TestClient(api.app, client=("100.64.0.9", 50000))
        self.mac = TestClient(api.app, client=("127.0.0.1", 50001))

    def test_reading_needs_the_admin_key_and_switching_needs_the_owner(self) -> None:
        with patch.object(render_optin, "listing", return_value={"workers": []}) as listing, \
                patch.object(render_optin, "set_optin", return_value={"ok": True}) as switch:
            read = lambda c, h: c.get(self.LISTING, headers=h)  # noqa: E731
            self.assertEqual(read(self.phone, {}).status_code, 401)
            self.assertEqual(read(self.phone, {"Authorization": "Bearer wrong"}).status_code, 403)
            self.assertEqual(read(self.mac, {}).status_code, 401)        # off loopback binding: key needed
            listing.assert_not_called()
            self.assertEqual(read(self.phone, self.KEY).status_code, 200)
            post = lambda c, h: c.post(self.SWITCH, json={"enabled": True}, headers=h)  # noqa: E731
            # The gateway key is not enough from another machine: Story holds it too.
            for headers in ({}, {"Authorization": "Bearer wrong"}, self.KEY):
                self.assertEqual(post(self.phone, headers).status_code, 403)
            self.assertEqual(post(self.mac, {}).status_code, 403)       # the app on this Mac sends its key
            switch.assert_not_called()
            self.assertEqual(post(self.mac, self.KEY).status_code, 200)
            with patch.object(admin_session, "valid_session", return_value=True), \
                    patch.object(admin_session, "same_origin", return_value=True):
                self.assertEqual(post(self.phone, {}).status_code, 200, "the owner's signed-in browser may")
            with patch.object(admin_session, "valid_session", return_value=True), \
                    patch.object(admin_session, "same_origin", return_value=False):
                self.assertEqual(post(self.phone, {}).status_code, 403, "never cross-site")
            self.assertEqual(switch.call_count, 2)
            switch.assert_called_with("roster-a", "mac", True)

    def test_the_switch_takes_exactly_one_boolean_and_relays_a_refusal_unchanged(self) -> None:
        with patch.object(render_optin, "set_optin") as switch:
            for body in ({}, {"enabled": True, "extra": 1}, {"opt_in": True}):
                self.assertEqual(self.mac.post(self.SWITCH, json=body, headers=self.KEY).status_code, 400)
            switch.assert_not_called()
        refusal = startup_control.StartupError(409, {"ok": False, "code": "NODE_REFUSED", "detail": "old"})
        with patch.object(render_optin, "set_optin", side_effect=refusal):
            response = self.mac.post(self.SWITCH, json={"enabled": True}, headers=self.KEY)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "NODE_REFUSED"))


if __name__ == "__main__":
    unittest.main()
