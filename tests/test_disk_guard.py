#!/usr/bin/env python3
"""The gateway's disk guard (owner decisions 2026-10-01).

Trash size cap, the red-volume sweep, `DISK_LOW` refusals, job-database archiving and the
fleet disk view / routing around a Mac that said `stop`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import sandbox_env  # noqa: E402

_SANDBOX = sandbox_env("gateway-disk-guard-")

from gateway import admin, api, diskguard, fleet, retention, worker  # noqa: E402
from gateway.providers import registry  # noqa: E402
from gateway.store import JobStore  # noqa: E402

GB = 1024**3
MB = 1024**2


class _Store:
    """Just enough store for the pruner: nothing is protected."""

    def protected_input_paths(self) -> set[str]:
        return set()


class _Dirs(unittest.TestCase):
    """Per-test artifact and Trash directories (the module paths are frozen at import)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="disk-guard-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.artifacts = root / "artifacts"
        self.trash = root / "_trash"
        self.recycled = root / "macos-trash"
        self.recycled.mkdir()
        self.artifacts.mkdir()
        for name, value in (("ARTIFACT_DIR", self.artifacts), ("TRASH_DIR", self.trash),
                            ("SETTINGS_PATH", root / "retention.json"),
                            ("KEPT_PATH", root / "kept.json"), ("LEGACY_TRASH_DIR", root / "trash")):
            patcher = patch.object(retention, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        def recycle(path):
            path.rename(self.recycled / path.name)
            return True
        patcher = patch.object(retention, "_recycle", side_effect=recycle)
        patcher.start(); self.addCleanup(patcher.stop)

    def trash_file(self, days_ago: int, name: str, size: int = MB, mtime_offset: float = 0) -> Path:
        day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        path = self.trash / day / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = time.time() - days_ago * 86400 + mtime_offset
        os.utime(path, (stamp, stamp))
        return path

    def artifact(self, name: str, age_s: float, size: int = MB) -> Path:
        path = self.artifacts / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path


class TrashCapTests(_Dirs):
    def test_cap_is_twenty_gb_independent_of_volume_size(self) -> None:
        self.assertEqual(retention.trash_cap_bytes(100 * GB), 20 * GB)
        self.assertEqual(retention.trash_cap_bytes(2000 * GB), 20 * GB)
        self.assertEqual(retention.trash_cap_bytes(0), 20 * GB)

    def test_over_the_cap_the_oldest_files_go_first(self) -> None:
        old = self.trash_file(5, "old.bin")
        mid = self.trash_file(3, "mid.bin")
        new = self.trash_file(1, "new.bin")
        with patch.object(retention, "trash_cap_bytes", return_value=int(2.5 * MB)):
            result = retention._transfer_trash(time.time())
            files, freed = result["recycled_files"], result["recycled_bytes"]
        self.assertEqual(files, 1)
        self.assertGreaterEqual(freed, MB)
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists() and new.exists())

    def test_legacy_same_day_cap_uses_path_order_not_source_mtime(self) -> None:
        a = self.trash_file(1, "a.bin", mtime_offset=-100)
        b = self.trash_file(1, "b.bin", mtime_offset=-50)
        c = self.trash_file(1, "c.bin", mtime_offset=0)
        with patch.object(retention, "trash_cap_bytes", return_value=int(1.5 * MB)):
            retention._transfer_trash(time.time())
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(c.exists())

    def test_prune_applies_the_cap_and_reports_it(self) -> None:
        self.trash_file(4, "old.bin")
        keep = self.trash_file(1, "new.bin")
        with patch.object(retention, "trash_cap_bytes", return_value=int(1.5 * MB)):
            result = retention.prune(store=_Store())
        self.assertEqual(result["trash_capped_files"], 1)
        self.assertTrue(keep.exists())

    def test_the_cap_holds_even_when_input_protection_cannot_be_read(self) -> None:
        self.trash_file(4, "old.bin")
        self.trash_file(1, "new.bin")
        with patch.object(retention, "trash_cap_bytes", return_value=int(1.5 * MB)), \
                patch.object(retention, "_protected_input_paths", return_value=None):
            result = retention.prune(store=_Store())
        self.assertIn("blocked_reason", result)
        self.assertEqual(len(list(self.trash.rglob("*.bin"))), 1)

    def test_a_folder_that_is_not_a_date_is_left_alone(self) -> None:
        stray = self.trash / "notes" / "keep.txt"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x" * MB)
        with patch.object(retention, "trash_cap_bytes", return_value=1):
            retention._transfer_trash(time.time(), cap=1)
        self.assertTrue(stray.exists())

    def test_usage_reports_physical_size_and_the_cap(self) -> None:
        self.trash_file(1, "a.bin", size=2 * MB)
        usage = retention.usage()
        self.assertGreaterEqual(usage["trash_physical_bytes"], 2 * MB)
        self.assertIn("trash_cap_bytes", usage)
        self.assertIn("state", usage["disk"])


class ReclaimTests(_Dirs):
    def test_pressure_preserves_recent_trash_and_retires_expired_artifacts(self) -> None:
        old_trash = self.trash_file(4, "t-old.bin")
        new_trash = self.trash_file(1, "t-new.bin")
        expired = self.artifact("expired.bin", age_s=5 * 86400)
        result = retention.reclaim(int(1.5 * MB), store=_Store())
        self.assertTrue(old_trash.exists() and new_trash.exists())
        self.assertFalse(expired.exists())
        self.assertTrue(next(self.trash.rglob("expired.bin")).exists())
        self.assertFalse(result["enough"])
        self.assertEqual(result["freed_bytes"], 0)
        self.assertEqual(list(self.recycled.iterdir()), [])

    def test_expired_artifacts_are_next_and_only_the_deletable_ones(self) -> None:
        retention.save(max_age_days=3)
        expired = self.artifact("expired.bin", age_s=5 * 86400)
        fresh = self.artifact("fresh.bin", age_s=2 * 86400)          # inside the age limit
        young = self.artifact("young.bin", age_s=60)                  # inside the 1 h grace
        pinned = self.artifact("pinned.bin", age_s=9 * 86400)
        staged = self.artifact("uploads/in.wav", age_s=9 * 86400)    # input of a live job
        retention.keep(pinned)

        class Live(_Store):
            def protected_input_paths(self_inner) -> set[str]:
                return {str(staged)}

        result = retention.reclaim(100 * MB, store=Live())
        self.assertFalse(expired.exists())
        for survivor in (fresh, young, pinned, staged):
            self.assertTrue(survivor.exists(), survivor.name)
        self.assertFalse(result["enough"])
        self.assertEqual(result["artifact_files"], 1)

    def test_unreadable_input_protection_sweeps_only_the_trash(self) -> None:
        expired = self.artifact("expired.bin", age_s=9 * 86400)
        with patch.object(retention, "_protected_input_paths", return_value=None):
            retention.reclaim(100 * MB, store=_Store())
        self.assertTrue(expired.exists())


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.total = 500 * GB
        self.free = 100 * GB
        for patcher in (patch.object(diskguard, "disk_usage", lambda: (self.total, self.free)),
                        patch.dict(diskguard._last_sweep, {"at": 0.0, "result": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_states_follow_the_free_fraction(self) -> None:
        self.assertEqual(diskguard.state_of(500 * GB, 61 * GB), "green")   # 12.2 %
        self.assertEqual(diskguard.state_of(500 * GB, 59 * GB), "amber")   # 11.8 %
        self.assertEqual(diskguard.state_of(500 * GB, 31 * GB), "amber")   # 6.2 %
        self.assertEqual(diskguard.state_of(500 * GB, 29 * GB), "red")     # 5.8 %
        self.assertEqual(diskguard.status()["state"], "green")

    def test_plenty_of_room_never_sweeps(self) -> None:
        with patch.object(retention, "reclaim") as reclaim:
            diskguard.require_room(10 * GB)
        reclaim.assert_not_called()

    def test_under_5_gb_free_refuses_with_a_sentence(self) -> None:
        self.free = 3 * GB
        with patch.object(retention, "reclaim", return_value={"freed_bytes": 0}):
            with self.assertRaises(diskguard.DiskLow) as caught:
                diskguard.require_room()
        self.assertIn("3.0 GB free", caught.exception.sentence)
        self.assertIn("Empty the Trash", caught.exception.sentence)

    def test_a_red_volume_is_swept_first_and_then_admitted(self) -> None:
        self.free = 4 * GB

        def reclaim(need: int) -> dict:
            self.free += 20 * GB
            return {"freed_bytes": 20 * GB, "trash_files": 3, "artifact_files": 0}

        with patch.object(retention, "reclaim", side_effect=reclaim) as swept:
            diskguard.require_room()
        swept.assert_called_once()
        self.assertEqual(diskguard.status()["last_sweep"]["freed_bytes"], 20 * GB)

    def test_the_request_size_counts_against_the_floor(self) -> None:
        self.free = 7 * GB
        with patch.object(retention, "reclaim", return_value={"freed_bytes": 0}):
            diskguard.require_room(1 * GB)
            with self.assertRaises(diskguard.DiskLow):
                diskguard.require_room(3 * GB)

    def test_sweeps_are_rate_limited_but_the_loop_can_force(self) -> None:
        self.free = 20 * GB  # red on 500 GB (4 %), above the refusal floor
        with patch.object(retention, "reclaim", return_value={"freed_bytes": 1}) as reclaim:
            diskguard.sweep_if_red()
            diskguard.sweep_if_red()
            self.assertEqual(reclaim.call_count, 1)
            diskguard.sweep_if_red(force=True)
            self.assertEqual(reclaim.call_count, 2)

    def test_amber_does_not_sweep(self) -> None:
        self.free = 40 * GB  # 8 %
        with patch.object(retention, "reclaim") as reclaim:
            self.assertIsNone(diskguard.sweep_if_red(force=True))
        reclaim.assert_not_called()


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(diskguard, "disk_usage", lambda: (500 * GB, 2 * GB))
        patcher.start()
        self.addCleanup(patcher.stop)
        sweep = patch.object(retention, "reclaim", return_value={"freed_bytes": 0})
        sweep.start()
        self.addCleanup(sweep.stop)

    def test_a_new_job_is_refused_with_disk_low_not_failed_mid_write(self) -> None:
        with self.assertRaises(api.GatewayError) as caught:
            api._submit("image", "any/model", {}, "")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "DISK_LOW")
        self.assertNotEqual(caught.exception.status, 402)
        self.assertIn("almost out of disk space", caught.exception.detail)
        self.assertIn("Retry-After", caught.exception.headers)

    def test_a_finished_artifact_is_not_written_and_the_job_fails_retryable(self) -> None:
        failures: list[dict] = []

        class Recorder:
            def fail(self, job_id, **kwargs):
                failures.append(dict(kwargs, job_id=job_id))

            def succeed(self, *a, **k):  # pragma: no cover - must not be reached
                raise AssertionError("settled a job whose artifact could not be written")

        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        artifact = types.SimpleNamespace(content=png + b"x" * 1000, media_type="image/png")
        result = types.SimpleNamespace(artifacts=[artifact], deliver=None, text="", payload={})
        written: list[str] = []
        with patch.object(Path, "write_bytes", lambda self, data: written.append(str(self))):
            worker.settle_result("job-1", result, Recorder())
        self.assertEqual(written, [])
        self.assertEqual(failures[0]["code"], "DISK_LOW")
        self.assertTrue(failures[0]["retryable"])


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="disk-guard-db-")
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(Path(self._tmp.name) / "jobs.sqlite3")
        self.addCleanup(self.store.close)

    def _job(self, key: str, *, age_days: float, state: str = "succeeded",
             seconds: float = 4.0, size: int = 100, payload_bytes: int = 0) -> str:
        row, _ = self.store.submit(modality="image", model_id="m/x",
                                   params={"prompt": "p" * payload_bytes if payload_bytes
                                           else "p"},
                                   idempotency_key=key, provider_id="studio-hub-a")
        stamp = time.time() - age_days * 86400
        if state == "succeeded":
            self.store.succeed(row["id"], asset_path=None, media_type="image/png", artifacts=[],
                               result={"text": "", "payload": {"generation_seconds": seconds,
                                                               "bytes": size, "hub_machine": "mac-1"}})
        else:
            self.store.fail(row["id"], code="PROVIDER_FAILED", detail="x")
        self.store._conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                                 (stamp, stamp, row["id"]))
        self.store._conn.commit()
        return row["id"]

    def test_old_settled_jobs_move_to_a_monthly_file_and_analytics_does_not_change(self) -> None:
        old_ok = self._job("old-ok", age_days=120)
        old_bad = self._job("old-bad", age_days=100, state="failed")
        recent = self._job("recent", age_days=5, seconds=8.0)
        before = self.store.generation_analytics()
        done = self.store.archive_old(days=90)
        self.assertEqual(done["archived"], 2)
        self.assertIsNone(self.store.get(old_ok))
        self.assertIsNone(self.store.get(old_bad))
        self.assertIsNotNone(self.store.get(recent))
        lines = [json.loads(line)
                 for path in (Path(self.store.path).parent / "job-archive").glob("jobs-*.jsonl")
                 for line in path.read_text().splitlines()]
        self.assertEqual({row["id"] for row in lines}, {old_ok, old_bad})
        after = self.store.generation_analytics()
        self.assertEqual(after["totals"], before["totals"])
        self.assertEqual(after["by_model"], before["by_model"])
        self.assertEqual(self.store.stats()["archived"], 2)

    def test_holds_and_unsettled_jobs_are_never_archived(self) -> None:
        queued, _ = self.store.submit(modality="image", model_id="m/x", params={},
                                      idempotency_key="queued", provider_id="studio-hub-a")
        held = self._job("held", age_days=200, state="failed")
        self.store._conn.execute(
            "UPDATE jobs SET recovery_state='unknown_outcome', updated_at=? WHERE id IN (?, ?)",
            (time.time() - 200 * 86400, held, queued["id"]))
        self.store._conn.commit()
        self.assertEqual(self.store.archive_old(days=90)["archived"], 0)
        self.assertIsNotNone(self.store.get(held))
        self.assertIsNotNone(self.store.get(queued["id"]))

    def test_the_database_is_vacuumed_when_a_fifth_of_it_is_free(self) -> None:
        # The 20 % rule is unchanged. The fixture must leave at least a fifth of the whole
        # file reclaimable: the render tables now contribute baseline pages to every job
        # database, so small prompt rows no longer dominate it. 60 x 100 KB of prompt text
        # puts the deleted rows well past the threshold without touching `archive_old`.
        for n in range(60):
            self._job(f"bulk-{n}", age_days=150, payload_bytes=100_000)
        done = self.store.archive_old(days=90)
        self.assertEqual(done["archived"], 60)
        self.assertTrue(done["vacuumed"])
        self.assertEqual(self.store._conn.execute("PRAGMA freelist_count").fetchone()[0], 0)

    def test_archived_month_files_age_out_into_the_trash_folder(self) -> None:
        old = self._job("old", age_days=120)
        self.assertEqual(self.store.archive_old(days=90)["archived"], 1)
        [archive] = list((Path(self.store.path).parent / "job-archive").glob("jobs-*.jsonl"))
        ancient = time.time() - 10 * 86400
        os.utime(archive, (ancient, ancient))
        done = self.store.archive_old(days=90)          # no new rows; the sweep still runs
        self.assertEqual(done["retired_files"], 1)
        self.assertFalse(archive.exists())
        self.assertEqual(len(list(retention.TRASH_DIR.rglob(archive.name))), 1)

    def test_nothing_is_deleted_when_the_archive_cannot_be_written(self) -> None:
        old = self._job("old", age_days=120)
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("a file where the directory should be")
        done = self.store.archive_old(days=90, archive_dir=blocker / "sub")  # mkdir fails
        self.assertEqual(done["archived"], 0)
        self.assertIsNotNone(self.store.get(old))


def _machine(mid: str, **disk) -> dict:
    out = {"machine": mid, "label": mid, "reachable": True}
    if disk:
        out["disk"] = disk
    return out


class FleetDiskTests(unittest.TestCase):
    def test_reads_the_hubs_disk_object(self) -> None:
        disk = fleet.machine_disk(_machine(
            "m1", state="red", free_gb=14.5, total_gb=228.0, trash_gb=3.2, swap_used_gb=6,
            top=[{"name": "models", "gb": 61.5}, ["outputs", 20], {"name": "logs", "bytes": 2 * GB}],
            last_sweep="2026-10-02T01:00:00Z",
            warnings=["macOS updates download automatically", {"message": "Time Machine is on"}]))
        self.assertEqual(disk["state"], "red")
        self.assertEqual(disk["state_source"], "hub")
        self.assertEqual(disk["free_gb"], 14.5)
        self.assertEqual([t["name"] for t in disk["top"]], ["models", "outputs", "logs"])
        self.assertEqual(disk["top"][2]["gb"], 2.0)
        self.assertEqual(disk["warnings"], ["macOS updates download automatically",
                                            "Time Machine is on"])
        self.assertEqual(disk["swap_used_gb"], 6.0)

    def test_an_older_hub_with_only_disk_free_gb_gets_a_derived_state(self) -> None:
        disk = fleet.machine_disk({"machine": "old", "disk_free_gb": 10.0, "disk_total_gb": 500.0})
        self.assertEqual((disk["state"], disk["state_source"]), ("red", "derived"))
        self.assertEqual(disk["top"], [])
        self.assertEqual(disk["warnings"], [])
        self.assertIsNone(fleet.machine_disk({"machine": "silent"}))
        no_total = fleet.machine_disk({"machine": "x", "disk_free_gb": 40})
        self.assertIsNone(no_total["state"])

    def test_junk_is_tolerated_and_black_means_stop(self) -> None:
        disk = fleet.machine_disk({"machine": "m", "disk": {
            "state": "black", "free_gb": "n/a", "top": "nonsense", "warnings": 7, "swap_used_gb": None}})
        self.assertEqual(disk["state"], "stop")
        self.assertIsNone(disk["free_gb"])
        self.assertEqual(disk["top"], [])
        fleet.machine_disk({"machine": "m", "disk": "string"})
        fleet.machine_disk({"machine": "m", "disk": {"top": [None, 3, {"gb": 1}]}})

    def test_machines_flat_carries_disk_and_the_summary_sorts_by_free_space(self) -> None:
        row = {"id": "c1", "label": "C1", "local": False, "reachable": True,
               "machines": [
                   {**_machine("big", state="green", free_gb=120, total_gb=500)},
                   {**_machine("tight", state="amber", free_gb=30, total_gb=250)},
                   {"machine": "legacy", "label": "legacy", "disk_free_gb": 8, "disk_total_gb": 250}],
               "activity": {"machines": []}, "nodes": []}
        flat = fleet.machines_flat([row])
        self.assertEqual({m["machine"]: m["disk"]["state"] for m in flat},
                         {"big": "green", "tight": "amber", "legacy": "red"})
        summary = fleet.disk_summary(flat)
        self.assertEqual([m["machine"] for m in summary["macs"]], ["legacy", "tight", "big"])
        self.assertEqual(summary["low"], ["legacy", "tight"])
        self.assertEqual(summary["worst"], "red")
        self.assertIn("state", summary["gateway"])

    def test_the_admin_route_reads_the_cached_snapshot(self) -> None:
        flat = [{"machine": "m", "label": "M", **{"disk_free_gb": 3, "disk_total_gb": 250}}]
        with patch.object(fleet, "snapshot", lambda **kw: {"machines": flat}):
            out = admin.fleet_disk()
        self.assertEqual(out["macs"][0]["state"], "stop")


class RefresherLifecycleTests(unittest.TestCase):
    def test_the_lifespans_refresher_ends_when_the_lifespan_does(self) -> None:
        # A TestClient that entered the lifespan used to leave the poller alive for the whole
        # run, where it raced later `snapshot(force=True)` calls (test_fleet_routing flaked).
        with patch.object(fleet, "snapshot", return_value={"locations": []}), \
                patch.object(fleet, "refresh_models_when_hub_changes"):
            fleet.start_refresher(interval=60)
            thread = fleet._refresher
            self.assertTrue(thread.is_alive())
            fleet.stop_refresher()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            fleet._refresher_stop.clear()


def _location(location_id: str, machines: list[dict]) -> dict:
    return {"id": location_id, "label": location_id.title(), "reachable": True, "capacity": {},
            "model_supply": {}, "machines": machines, "machine_summary": {"reachable": len(machines)},
            "running_jobs": []}


class SchedulerTests(unittest.TestCase):
    MODEL, MODALITY = "m/x", "image"

    def _route(self, rows: list[dict], **kwargs):
        defs = [types.SimpleNamespace(id=f"{fleet.PROVIDER_PREFIX}{r['id']}") for r in rows]
        model = {"model_id": self.MODEL, "modality": self.MODALITY, "enabled": True, "raw": {}}
        with patch.object(fleet, "snapshot", lambda allow_stale=False: {"locations": rows}), \
                patch.object(registry.models, "enabled_models", lambda m: [(d, model) for d in defs]):
            return fleet.route(self.MODEL, self.MODALITY, **kwargs)

    def test_a_location_whose_macs_are_all_stop_is_rerouted(self) -> None:
        full = _location("full", [_machine("a", state="stop", free_gb=2, total_gb=250)])
        fine = _location("fine", [_machine("b", state="green", free_gb=100, total_gb=250)])
        chosen, _ = self._route([full, fine])
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}fine")
        chosen, note = self._route([full])
        self.assertIsNone(chosen)

    def test_one_mac_with_room_keeps_the_location_available(self) -> None:
        mixed = _location("mixed", [_machine("a", state="stop", free_gb=2, total_gb=250),
                                    _machine("b", state="amber", free_gb=20, total_gb=250)])
        chosen, _ = self._route([mixed])
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}mixed")

    def test_a_state_the_gateway_derived_never_blocks_routing(self) -> None:
        legacy = _location("legacy", [{"machine": "a", "disk_free_gb": 2, "disk_total_gb": 250}])
        self.assertEqual(fleet.machine_disk(legacy["machines"][0])["state"], "stop")
        chosen, _ = self._route([legacy])
        self.assertEqual(chosen.id, f"{fleet.PROVIDER_PREFIX}legacy")

    def test_amber_and_red_still_route(self) -> None:
        for state in ("amber", "red"):
            row = _location("x", [_machine("a", state=state, free_gb=10, total_gb=250)])
            chosen, _ = self._route([row])
            self.assertIsNotNone(chosen, state)


class FleetDiskIssueTests(unittest.TestCase):
    """Fleet Overview lists a Mac whose disk is red or stopped; an unreachable Mac's last reading does not count."""

    @staticmethod
    def _row(machines: list[dict]) -> dict:
        return {"id": "ctl", "label": "Site", "local": False, "reachable": True, "machines": machines,
                "nodes": [], "activity": {"machines": [{"machine": m["machine"], "state": "ready"} for m in machines],
                                          "pulse": {}},
                "running_jobs": [], "local_models": [], "workers": [], "probe_machines_hidden": 0}

    def test_red_and_stop_macs_get_an_issue_each_green_and_amber_do_not(self) -> None:
        def mac(mid, free, **extra):
            return {"machine": mid, "label": mid, "reachable": True, "disk_free_gb": free,
                    "disk_total_gb": 460.0, "disk": {"state": extra.pop("state"), "free_gb": free, "total_gb": 460.0}}
        row = self._row([mac("a", 3.0, state="stop"), mac("b", 20.0, state="red"),
                         mac("c", 40.0, state="amber"), mac("d", 300.0, state="green")])
        issues = {i["code"]: i for i in fleet._issues_for(row) if i["code"].startswith("disk_")}
        self.assertEqual(set(issues), {"disk_stop", "disk_red"})
        self.assertEqual(issues["disk_stop"]["severity"], "error")
        self.assertIn("a 3 GB free", issues["disk_stop"]["message"])
        self.assertEqual(issues["disk_red"]["severity"], "warning")
        self.assertIn("b 20 GB free", issues["disk_red"]["message"])
        self.assertNotIn("c 40", issues["disk_red"]["message"])

    def test_an_unreachable_mac_is_not_reported_for_its_stale_disk(self) -> None:
        row = self._row([{"machine": "z", "label": "z", "reachable": False, "disk_free_gb": 0.0,
                          "disk_total_gb": 256.0}])
        self.assertEqual([i for i in fleet._issues_for(row) if i["code"].startswith("disk_")], [])


if __name__ == "__main__":
    unittest.main()
