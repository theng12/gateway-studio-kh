#!/usr/bin/env python3
"""A healthy, idle fleet reads healthy; real problems still read as problems, by name; every Mac's
Studio Hub version is rated against the newest release this Gateway published.

Live 2026-10-02 (Gateway 0.2.97, Hub 0.6.132 on every online Mac, nothing generating) the Groups page
said "5 of 6 groups need attention" and every idle node "Needs Attention". Each of those was traced
to one of three artefacts, all reproduced here from the live shapes:

* a `video` worker every node still listed on its controller (frozen at join; Video was retired), so
  "10 workers are offline" and, through the Hub's "any studio down" rule, `needs_attention`;
* "Direct activity partially unavailable (partial_read)" — the same phantom reporter, as a warning;
* "Hub v0.6.132 (newer)" — a comparison with the release this Gateway *build* targets, not with the
  newest one it published.

Run with::

    .venv/bin/python -m unittest tests.test_fleet_health
"""
from __future__ import annotations

import copy
import time
import unittest

from tests._isolation import sandbox_env  # noqa: E402

# Importing gateway.* here freezes DATA_DIR; without this it binds the owner's real
# data folder at ~/Library/Application Support/gateway-kh/data.
_SANDBOX = sandbox_env()

from gateway import fleet

NOW = time.time()
LATEST = "0.6.132"
FINGERPRINT = "ab" * 32
PUSH = {"fingerprint": FINGERPRINT, "updated_at": NOW - 6 * 3600,
        "release": {"version": LATEST, "build": "e6a8b80"}, "locations": {}}
STUDIOS = ("voice", "transcription", "chat", "music", "image")


def startup(*studios: str, services: bool = True, version: str = LATEST) -> dict:
    return {"open_at_login": True, "start_services_on_launch": services, "app_version": version,
            "app_reporting": True, "stale": None,
            "services_running": {"up": len(studios), "total": len(studios),
                                 "studios": {name: "up" for name in studios}}}


def worker(mid: str, studio: str, *, online: bool = True, seen: bool = True) -> dict:
    return {"id": f"{mid}:{studio}", "studio": studio, "modality": studio, "machine": mid,
            "online": online, "ready": online, "busy": False, "drained": False, "maintenance": False,
            "quarantined": False, "slots": 1 if online else 0, "slots_total": 1,
            "last_seen_at": "2026-10-02T14:59:37Z" if seen else None, "models": [],
            "supported_operations": []}


def idle_entry(mid: str, *, state: str = "needs_attention", latest_state: str = "done",
               ago: float = 17 * 3600, limitation: bool = True) -> dict:
    """The Hub ≤ 0.6.132's activity row for an idle node with a phantom Video studio."""
    return {
        "machine": mid, "state": state, "current_job": None, "stalled_seconds": None,
        "latest": {"state": latest_state, "finished_at": NOW - ago, "observed_at": NOW - ago,
                   "error_code": "HTTP 409: local is reserved" if latest_state == "error" else None},
        "completed": 6, "failed": 1 if latest_state == "error" else 0, "median_runtime_s": None,
        "limitation": "Direct activity partially unavailable" if limitation else None,
        "limitation_code": "partial_read" if limitation else None,
        "limitation_reason": "Some reporters missing" if limitation else None,
        "limitation_since": None,
        "limitation_affects": ["progress", "failed", "median_runtime"] if limitation else None,
    }


def node(mid: str, *, version: str = LATEST, state: str = "reachable", detail: str | None = None) -> dict:
    return {"id": mid, "name": mid, "state": state, "role": "agent", "version": version,
            "address": f"http://100.64.0.{len(mid)}:47873", "detail": detail,
            "last_seen_at": NOW - 5, "studios": [{"id": s, "modality": s} for s in (*STUDIOS, "video")]}


def machine(mid: str, *, reachable: bool = True, free: float = 160.0, start: dict | None = None) -> dict:
    disk_state = "red" if free < 14 else "green"
    return {"machine": mid, "label": mid, "reachable": reachable, "chip": "Apple M2", "memory_gb": 8.59,
            "available_gb": 4.0 if reachable else None, "disk_free_gb": free, "disk_total_gb": 245.0,
            "health": "up" if reachable else "unreachable",
            "disk": {"state": disk_state, "free_gb": free, "total_gb": 245.0, "warnings": []},
            "startup": start if start is not None else startup(*STUDIOS)}


def group(nodes: int = 3, *, held: str = LATEST) -> dict:
    """One controller and its idle nodes, exactly as the live 1100 group read on 2026-10-02."""
    mids = [f"node-{i}" for i in range(1, nodes + 1)]
    return {
        "id": "ctrl-1100", "label": "terranash-1100", "url": "http://100.64.0.1:47873", "local": False,
        "reachable": True, "role": "controller", "version": LATEST, "hub_version_relation": "newer",
        "error": None, "drained": False, "ready": True,
        "release": {"version": held, "published_at": NOW - 6 * 3600, "publisher_id": "gatewaykh",
                    "publisher_key_id": FINGERPRINT},
        "machines": [machine("local")] + [machine(mid) for mid in mids],
        "activity": {"pulse": {}, "observed_at": NOW,
                     "machines": [idle_entry("local", state="long_idle", limitation=False)]
                     + [idle_entry(mid) for mid in mids]},
        "nodes": [node(mid) for mid in mids],
        # Five real studios on every Mac, plus the Video worker no Mac runs any more.
        "workers": [worker("local", s) for s in STUDIOS]
        + [worker(mid, s) for mid in mids for s in STUDIOS]
        + [worker(mid, "video", online=False, seen=False) for mid in mids],
        "local_models": [], "model_health": [], "running_jobs": [], "probe_machines_hidden": 0,
    }


def read(*rows: dict, push: dict | None = PUSH) -> tuple[dict, list[dict], dict]:
    rows = [copy.deepcopy(row) for row in rows]
    fleet.stamp_latest(list(rows), push)
    flat = {m["machine"]: m for m in fleet.machines_flat(list(rows))}
    return flat, fleet._issues(list(rows)), fleet._summarise(list(rows))


def loud(issues: list[dict]) -> list[str]:
    """What turns a group amber: every error and warning, by code."""
    return sorted(i["code"] for i in issues if i["severity"] in ("error", "warning"))


class AHealthyIdleFleetIsGreen(unittest.TestCase):
    def test_no_warning_no_needs_attention_every_mac_on_latest(self) -> None:
        flat, issues, summary = read(group())
        self.assertEqual(loud(issues), [], issues)
        self.assertEqual(summary["issues"], 0)
        self.assertEqual({m["state"] for m in flat.values()}, {"long_idle"})
        self.assertEqual({m["version_status"] for m in flat.values()}, {"latest"})
        self.assertEqual((summary["on_latest"], summary["version_known"], summary["hub_latest"]), (4, 4, LATEST))

    def test_the_phantom_video_worker_and_its_partial_read_are_not_reported(self) -> None:
        flat, issues, _ = read(group())
        self.assertNotIn("worker_offline", [i["code"] for i in issues])
        self.assertNotIn("machine_limited", [i["code"] for i in issues])
        self.assertIsNone(flat["node-1"]["limitation_code"])
        self.assertEqual(flat["node-1"]["not_installed_studios"], ["Video"])

    def test_last_nights_failed_job_does_not_hold_an_idle_mac_amber(self) -> None:
        row = group(1)
        row["activity"]["machines"][1] = idle_entry("node-1", latest_state="error", ago=16 * 3600)
        flat, issues, _ = read(row)
        self.assertEqual(flat["node-1"]["state"], "long_idle")
        self.assertEqual(loud(issues), [])

    def test_no_drift_note_against_the_build_target(self) -> None:
        _, issues, _ = read(group())
        self.assertNotIn("hub_version_drift", [i["code"] for i in issues])

    def test_a_partial_read_left_after_the_phantom_is_gone_is_information_only(self) -> None:
        row = group(1)
        row["workers"] = [w for w in row["workers"] if w["studio"] != "video"]
        flat, issues, _ = read(row)
        limited = [i for i in issues if i["code"] == "machine_limited"]
        self.assertEqual([i["severity"] for i in limited], ["info"])
        self.assertEqual(flat["node-1"]["state"], "needs_attention",
                         "no visible cause and no known artefact: the Hub's verdict stands")
        self.assertEqual(flat["node-1"]["attention"], "Flagged by its Hub")


class RealProblemsStillShowByName(unittest.TestCase):
    def test_a_worker_that_should_run_and_crashed(self) -> None:
        row = group(1)
        voice = next(w for w in row["workers"] if w["id"] == "node-1:voice")
        voice.update(online=False, ready=False)
        flat, issues, _ = read(row)
        self.assertEqual(flat["node-1"]["state"], "needs_attention")
        self.assertEqual(flat["node-1"]["attention"], "Voice studio not running")
        offline = [i for i in issues if i["code"] == "worker_offline"]
        self.assertEqual([i["subject"] for i in offline], ["node-1:voice"], "the real one, not the phantom")
        self.assertEqual(offline[0]["severity"], "warning")
        self.assertIn("restart", offline[0]["action"])

    def test_a_job_that_failed_minutes_ago(self) -> None:
        row = group(1)
        row["activity"]["machines"][1] = idle_entry("node-1", latest_state="error", ago=120)
        flat, _, _ = read(row)
        self.assertEqual(flat["node-1"]["state"], "needs_attention")
        self.assertTrue(flat["node-1"]["attention"].startswith("Last job failed"))

    def test_a_quality_rejection_is_not_a_mac_problem_but_other_errors_are(self) -> None:
        for code, state in (("TTS_SECTION_TOO_LONG", "ready"), ("TTS_SECTION_SILENT", "ready"),
                            ("DEADLINE_EXCEEDED", "needs_attention")):
            row = group(1)
            row["activity"]["machines"][1] = idle_entry("node-1", latest_state="error", ago=120)
            row["activity"]["machines"][1]["latest"]["error_code"] = code
            flat, _, _ = read(row)
            self.assertEqual(flat["node-1"]["state"], state, code)

    def test_an_offline_node_is_one_warning_and_its_version_is_unknown(self) -> None:
        row = group(2)
        row["machines"][2] = machine("node-2", reachable=False)
        row["activity"]["machines"][2] = {**idle_entry("node-2", state="offline"),
                                          "limitation_code": "waiting_for_worker",
                                          "limitation": "Activity evidence pending"}
        row["nodes"][1].update(state="unreachable", detail="could not reach the node (unreachable)")
        for w in row["workers"]:
            if w["machine"] == "node-2":
                w["online"] = False
        row["local_models"] = [{"model": "whisper", "machine": "node-2", "catalog_error": "ConnectTimeout"},
                               {"model": "qwen", "machine": "node-2", "catalog_error": "worker_unreachable"},
                               # httpx's message, not its class name (live 1200, 2026-10-03).
                               {"model": "moonshine", "machine": "node-2",
                                "catalog_error": "All connection attempts failed"},
                               {"model": "parakeet", "machine": "node-2",
                                "catalog_error": "[Errno 61] Connection refused"}]
        flat, issues, summary = read(row)
        self.assertEqual(loud(issues), ["machine_offline"], issues)
        self.assertEqual(flat["node-2"]["version_status"], "unknown")
        self.assertEqual((summary["on_latest"], summary["hub_unknown"]), (2, 1))

    def test_a_refused_credential_disk_red_and_startup_off(self) -> None:
        row = group(3)
        row["nodes"][0].update(state="unauthorized", detail="re-join it with the current Fleet Token")
        row["machines"][2] = machine("node-2", free=9.0)
        row["machines"][3] = machine("node-3", start=startup(*STUDIOS, services=False))
        _, issues, _ = read(row)
        self.assertEqual(loud(issues), ["disk_red", "node_credential_rejected", "services_not_auto_start"])

    def test_a_stalled_job(self) -> None:
        row = group(1)
        row["running_jobs"] = [{"id": "b-1", "stalled": True}]
        _, issues, _ = read(row)
        self.assertIn("running_stalled", loud(issues))


class EveryAttentionReasonCarriesItsCode(unittest.TestCase):
    """`attention` is the sentence; `attention_code` is what says what it is.

    Without the code, every one of these was drawn as "Its Hub reports a problem this Gateway cannot
    see from outside" — including a studio this Gateway watched go down and a job it watched fail.
    A healthy row carries neither.
    """

    def row(self, row: dict, mid: str = "node-1") -> dict:
        flat, _, _ = read(row)
        return flat[mid]

    def test_a_studio_that_should_run_and_is_not(self) -> None:
        row = group(1)
        next(w for w in row["workers"] if w["id"] == "node-1:voice").update(online=False, ready=False)
        got = self.row(row)
        self.assertEqual(got["state"], "needs_attention")
        self.assertEqual(got["attention"], "Voice studio not running")
        self.assertEqual(got["attention_code"], "studio_down")

    def test_a_studio_that_is_up_but_not_ready(self) -> None:
        row = group(1)
        next(w for w in row["workers"] if w["id"] == "node-1:voice").update(ready=False)
        got = self.row(row)
        self.assertEqual((got["attention"], got["attention_code"]),
                         ("Voice studio not ready", "studio_not_ready"))

    def test_a_job_that_stopped_making_progress(self) -> None:
        row = group(1)
        entry = idle_entry("node-1")
        entry["stalled_seconds"] = 900
        row["activity"]["machines"][1] = entry
        got = self.row(row)
        self.assertEqual((got["attention"], got["attention_code"]),
                         ("No progress for 15 min", "job_stalled"))

    def test_a_health_word_the_hub_stated(self) -> None:
        row = group(1)
        row["machines"][1]["health"] = "degraded"
        got = self.row(row)
        self.assertEqual((got["attention"], got["attention_code"]),
                         ("Health: degraded", "health_word"))

    def test_a_job_that_failed_in_the_last_quarter_hour(self) -> None:
        row = group(1)
        row["activity"]["machines"][1] = idle_entry("node-1", latest_state="error", ago=120)
        got = self.row(row)
        self.assertEqual(got["attention_code"], "recent_job_failed")
        self.assertTrue(got["attention"].startswith("Last job failed"))

    def test_a_request_too_long_for_the_model_does_not_blame_the_mac(self) -> None:
        row = group(1)
        row["activity"]["machines"][1] = idle_entry("node-1", latest_state="error", ago=120)
        row["activity"]["machines"][1]["latest"]["error_code"] = "context_exceeded"
        got = self.row(row)
        self.assertIsNone(got["attention_code"])
        self.assertIsNone(got["attention"])

    def test_a_verdict_only_the_hub_can_give(self) -> None:
        row = group(1)
        row["workers"] = [w for w in row["workers"] if w["studio"] != "video"]
        got = self.row(row)
        self.assertEqual((got["attention"], got["attention_code"]),
                         ("Flagged by its Hub", "hub_flagged"))

    def test_a_mac_reading_healthy_has_neither(self) -> None:
        got = self.row(group())
        self.assertEqual((got["attention"], got["attention_code"]), (None, None))


class VersionBadges(unittest.TestCase):
    def test_behind_and_not_updating_is_a_warning_naming_the_mac_and_its_version(self) -> None:
        row = group(2)
        row["nodes"][1]["version"] = "0.6.126"
        row["machines"][2]["startup"] = startup(*STUDIOS, version="0.6.126")
        flat, issues, summary = read(row)
        self.assertEqual(flat["node-2"]["hub_version"], "0.6.126")
        self.assertEqual(flat["node-2"]["version_status"], "behind",
                         "its controller has held 0.6.132 for six hours: it is not getting there")
        behind = [i for i in issues if i["code"] == "hub_behind"]
        self.assertEqual(len(behind), 1)
        self.assertIn("node-2 (0.6.126)", behind[0]["message"])
        self.assertIn("Update this Mac", behind[0]["action"])
        self.assertEqual((summary["on_latest"], summary["version_known"], summary["hub_behind"]), (2, 3, 1))

    def test_updating_inside_the_install_window_is_not_a_warning(self) -> None:
        row = group(1)
        row["nodes"][0]["version"] = "0.6.131"
        fresh = {**PUSH, "updated_at": NOW - 300, "release": {"version": "0.6.133"}}
        row["release"].update(version="0.6.133", published_at=NOW - 300)
        flat, issues, summary = read(row, push=fresh)
        self.assertEqual(flat["node-1"]["version_status"], "updating")
        self.assertEqual(flat["local"]["version_status"], "updating", "the controller holds it, not installed yet")
        self.assertEqual(loud(issues), [])
        self.assertEqual(summary["hub_updating"], 2)

    def test_a_busy_mac_past_the_window_is_still_updating(self) -> None:
        self.assertEqual(fleet.version_status("0.6.131", {"version": LATEST, "published_at": NOW - 7200},
                                              controller_holds_latest=True, busy=True, now=NOW), "updating")
        self.assertEqual(fleet.version_status("0.6.131", {"version": LATEST, "published_at": NOW - 7200},
                                              controller_holds_latest=True, busy=False, now=NOW), "behind")

    def test_a_controller_that_lacks_the_release_is_behind_and_says_publish(self) -> None:
        row = group(1, held="0.6.131")
        row["version"] = "0.6.131"
        row["machines"][0]["startup"] = startup(*STUDIOS, version="0.6.131")
        flat, issues, _ = read(row)
        self.assertEqual(flat["local"]["version_status"], "behind")
        behind = next(i for i in issues if i["code"] == "hub_behind")
        self.assertIn("publish the release to this group", behind["message"])

    def test_latest_is_the_newest_published_release_from_either_source(self) -> None:
        pilot = group(1, held="0.6.133")
        pilot["release"]["published_at"] = NOW - 60
        other = group(1)
        self.assertEqual(fleet.latest_hub_release([pilot, other], PUSH)["version"], "0.6.133",
                         "a pilot publish to one controller is still the newest release")
        foreign = group(1, held="0.9.0")
        foreign["release"]["publisher_key_id"] = "cd" * 32
        self.assertEqual(fleet.latest_hub_release([foreign], PUSH)["version"], LATEST,
                         "a release another publisher signed is not ours to call latest")
        self.assertIsNone(fleet.latest_hub_release([group(1, held="")], {}))

    def test_nothing_published_gives_no_verdict(self) -> None:
        row = group(1)
        row["release"] = None
        flat, issues, summary = read(row, push={})
        self.assertIsNone(flat["node-1"]["version_status"])
        self.assertIsNone(summary["hub_latest"])
        self.assertNotIn("hub_behind", [i["code"] for i in issues])


if __name__ == "__main__":
    unittest.main()
