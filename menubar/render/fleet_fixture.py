"""A realistic Fleet snapshot, built by the gateway's own assembly code from hand-made location rows.

Offline and synthetic: no Hub, no token, no network. The rows have the shape `fleet._probe` returns, and
`machines_flat` / `running_flat` / `_summarise` / `_issues` are the real functions, so the JSON has exactly
the keys and derived fields `/admin/api/fleet` serves. Used only by the render harness.

    python menubar/render/fleet_fixture.py > fleet.json     (run from the repository root)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gateway import fleet  # noqa: E402

NOW = time.time()
MIN, HOUR, DAY = 60, 3600, 86400
GB = 1.0


def disk(free: float, total: float = 460.0) -> dict:
    """What `feat/disk-governor` adds per host: a `disk` object with a tier. Only the fields the design uses."""
    percent = free / total
    state = "stop" if free < 5 else "red" if percent < 0.06 else "amber" if percent < 0.12 else "green"
    return {"state": state, "free_gb": free, "total_gb": total, "free_percent": round(100 * percent, 1),
            "trash_gb": 6.0 if state != "green" else 1.2, "swap_used_gb": 0.0,
            "warnings": [] if state == "green" else [f"Only {free:g} GB free."]}


def startup(*, login=True, services=True, stale=False):
    """What a Mac says it starts when it starts (`machines[].startup`)."""
    return {"open_at_login": login, "start_services_on_launch": services, "app_version": "0.6.126",
            "app_reporting": True, "stale": stale}


def machine(mid, label, chip, mem, avail, disk_free, *, reachable=True, health="up", modalities=None,
            disk_total=460.0, start=None, current=None):
    row = {"machine": mid, "label": label, "reachable": reachable, "chip": chip, "memory_gb": mem,
           "available_gb": avail, "disk_free_gb": disk_free, "disk_total_gb": disk_total, "health": health,
           "modalities": modalities or {}, "disk": disk(disk_free, disk_total),
           "startup": start if start is not None else startup()}
    if current:
        row["current"] = current
    return row


def activity(mid, state, completed, failed, median, limitation=None):
    return {"machine": mid, "state": state, "completed": completed, "failed": failed,
            "median_runtime_s": median, "limitation": limitation, "limitation_code": None,
            "limitation_reason": None, "limitation_since": None, "limitation_affects": None}


def node(mid, state, *, address, version="0.6.126", seen=0, detail=None, studios=("voice",)):
    return {"id": mid, "name": mid, "state": state, "role": "agent", "address": address,
            "joined_at": NOW - 30 * DAY, "last_seen_at": NOW - seen, "credential_issued_at": NOW - 30 * DAY,
            "has_credential": True, "version": version, "detail": detail,
            "studios": [{"id": s, "modality": s} for s in studios]}


def worker(mid, studio, *, online=True, busy=False, ready=True, slots=1, models=()):
    return {"id": f"{mid}:{studio}", "studio": studio, "modality": {"subtitle": "transcription"}.get(studio, studio),
            "machine": mid, "online": online, "ready": ready and online, "busy": busy, "drained": False,
            "maintenance": False, "quarantined": False, "slots": 0 if busy else slots, "slots_total": slots,
            "last_seen_at": NOW - 4, "models": list(models), "supported_operations": []}


def model(repo, label, studio, mid, *, available=True, cache="cached", fit="fits", need=6.0, size=3.4):
    return {"model": repo, "model_id": repo, "label": label, "family": label.split()[0], "studio": studio,
            "modality": {"subtitle": "transcription"}.get(studio, studio), "machine": mid, "kind": "mlx",
            "size_gb": size, "min_unified_memory_gb": need, "required_free_memory_gb": need,
            "memory_eligible": fit != "too_big", "runtime_ready": cache == "cached", "available": available,
            "loaded": False, "fit": {"state": fit, "label": fit.replace("_", " "), "hint": None,
                                     "actual_gb": 16.0, "required_gb": need},
            "cache_state": cache, "catalog_error": None, "catalog_stale": False, "active_download": False,
            "gated": False, "dispatch_state": "eligible" if available else None}


def batch(bid, modality, model_id, label, *, total, done, running, queued, started, stalled=False, note=None):
    return {"id": bid, "modality": modality, "model": model_id, "model_id": model_id, "label": label,
            "total": total, "queued": queued, "running": running, "retrying": 0, "done": done, "error": 0,
            "stalled": stalled, "stalled_after_s": 900 if stalled else None,
            "no_progress_s": 1500 if stalled else 20, "next_retry_at": None, "uncertain": 0,
            "cancel_requested": 0, "cancelled": False, "cancelled_items": 0, "routing": "pool",
            "governor_note": note, "avg_s": 41.0, "processing_elapsed_s": 300, "created_at": NOW - started,
            "last_activity_at": NOW - 12, "errors": None}


def location(lid, label, url, *, local=False, reachable=True, role="controller", version="0.6.126",
             machines=(), activities=(), nodes=(), workers=(), models=(), running=(), error=None, **extra):
    node_rows = list(nodes)
    summary = {"total": len(node_rows), "reachable": sum(n["state"] == "reachable" for n in node_rows),
               "unreachable": sum(n["state"] == "unreachable" for n in node_rows),
               "unauthorized": sum(n["state"] == "unauthorized" for n in node_rows), "error": 0, "removed": 0}
    row = {
        "id": lid, "label": label, "url": url, "local": local, "provider_id": fleet.provider_id_for(lid),
        "reachable": reachable, "error": error, "role": role if reachable else None,
        "version": version if reachable else None,
        "hub_version_relation": ("same" if version == "0.6.126" else "older") if reachable else None,
        "machines": list(machines), "machine_summary": {"total": len(machines),
                                                         "reachable": sum(bool(m["reachable"]) for m in machines)},
        "nodes": node_rows, "node_summary": summary if node_rows else None,
        "running_jobs": list(running),
        "activity": {"pulse": {}, "machines": list(activities), "observed_at": NOW - 5},
        "by_machine": {}, "matrix": [], "jobs_total": 240, "site_id": lid, "controller_id": lid, "ready": reachable,
        "drained": False, "capacity": {}, "latency_ms": 38 if reachable else None, "model_supply": {},
        "local_models": list(models), "model_health": [], "release": None, "trusted_publisher": None,
        "workers": list(workers), "hub_workers": [], "probe_machines_hidden": 0,
        "models_discovered": len({m["model"] for m in models}), "models_enabled": len({m["model"] for m in models}),
        "models_publishable": len({m["model"] for m in models if m["available"]}),
        "members_from": None, "members_stale_seconds": None,
        "unreachable_since": None, "paused_at": None, "pause_reason": None,
    }
    row.update(extra)
    return row


KOKORO = "mlx-community/Kokoro-82M-bf16"
QWEN = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
FLUX = "black-forest-labs/FLUX.2-klein-4B"
WHISPER = "mlx-community/whisper-large-v3-turbo"
STABLE = "stabilityai/stable-audio-3-optimized"


UPDATES = ("macOS is set to download updates automatically (8-15 GB each). Turn it off in System Settings > "
           "General > Software Update > Automatic Updates > Download new updates.")


def many(rows: list) -> list:
    """GATEWAY_RENDER_FIXTURE=many: 45 Macs in all, most of them saying the same Hub warning, as on the live fleet.

    A big controller group ("Terranash") of 37 Macs: its own Mac comes through labelled "This Mac" (what a Hub
    calls itself), 17 download macOS updates automatically, a few more carry the Time Machine and low-disk
    warnings, eight are off.
    """
    names = [f"terranash-{1100 + i}-m4-16-256" for i in range(37)]
    machines, activities, nodes = [], [], []
    for i, name in enumerate(names):
        free = 29.8 if i == 4 else 12.0 if i == 9 else 4.0 if i == 11 else 180.0 + i
        row = machine(name, "This Mac" if i == 0 else name, "Apple M4", 16.0, 7.0 + i % 5, free,
                      reachable=not (28 <= i < 36), modalities={"voice": 10 + i},
                      start=startup(services=i % 6 != 0))
        warnings = [w for w in row["disk"]["warnings"] if "free" not in w] if free > 40 else []
        if i < 17:
            warnings += [UPDATES, UPDATES] if i == 3 else [UPDATES]
        if i in (20, 21):
            warnings.append("Time Machine is backing up to this disk, so free space reads lower than it is.")
        if i == 4:
            row["disk"]["top"] = [{"name": "other", "gb": 373.0}]
        row["disk"]["warnings"] = warnings
        machines.append(row)
        activities.append(activity(name, "offline" if 28 <= i < 36 else "working" if i % 7 == 0 else "ready",
                                   40 + i, i % 4, 45.0))
        if i:
            nodes.append(node(name, "unreachable" if 28 <= i < 36 else "reachable",
                              address=f"http://100.64.1.{i}:47873", seen=3 * HOUR if 28 <= i < 36 else 5,
                              detail="could not reach the node: All connection attempts failed" if 28 <= i < 36 else None))
    big = location("ctrl-terranash", "Terranash", "http://100.64.1.1:47873", machines=machines, activities=activities,
                   nodes=nodes, workers=[worker(n, "voice", online=not (28 <= i < 36)) for i, n in enumerate(names)])
    return [rows[0], rows[1], rows[2], big, rows[3]]


def build() -> dict:
    local = location(
        "local", "This Mac", "http://127.0.0.1:47873", local=True, role="controller",
        machines=[machine("local", "This Mac", "Apple M2 Max", 32.0, 18.4, 61.0, disk_total=460.0,
                          modalities={"voice": 120, "image": 31, "music": 12})],
        activities=[activity("local", "working", 163, 4, 52.0)],
        workers=[worker("local", "voice", busy=True, models=[KOKORO, QWEN]), worker("local", "image", models=[FLUX]),
                 worker("local", "subtitle", models=[WHISPER]), worker("local", "music", models=[STABLE])],
        models=[model(KOKORO, "Kokoro 82M", "voice", "local", need=3.0, size=0.4),
                model(QWEN, "Qwen3 TTS 0.6B", "voice", "local", need=6.0),
                model(FLUX, "FLUX.2 Klein 4B", "image", "local", need=10.0, size=8.0),
                model(WHISPER, "Whisper large v3 turbo", "subtitle", "local", need=6.0, size=1.6),
                model(STABLE, "Stable Audio 3", "music", "local", need=12.0, size=6.1)],
        running=[batch("b-7341", "voice", QWEN, "Chapter 14 narration", total=18, done=11, running=1, queued=6,
                       started=22 * MIN)],
    )
    blue = location(
        "ctrl-blue", "Blue Room", "http://100.64.0.11:47873",
        machines=[machine("blue-ctl", "Blue Room mini", "Apple M4", 16.0, 7.1, 188.0, modalities={"voice": 40}),
                  machine("blue-1", "Blue Room 1", "Apple M4", 16.0, 9.8, 142.0, modalities={"voice": 61, "image": 8},
                          current={"batch_id": "b-7355", "model": QWEN, "label": "Podcast ep 31", "studio": "voice",
                                   "started_at": NOW - 9 * MIN}),
                  machine("blue-2", "Blue Room 2", "Apple M2", 8.0, 2.2, 21.0, health="up",
                          modalities={"voice": 30}, disk_total=256.0, start=startup(services=False)),
                  machine("blue-3", "Blue Room 3", "Apple M2", 8.0, 0.0, 0.0, reachable=False, health="down",
                          modalities={}, disk_total=256.0, start=startup(services=False, stale=True))],
        activities=[activity("blue-ctl", "ready", 40, 0, 38.0), activity("blue-1", "working", 69, 1, 44.0),
                    activity("blue-2", "ready", 30, 3, 61.0, limitation="Direct activity partially unavailable"),
                    activity("blue-3", "offline", 0, 0, None)],
        nodes=[node("blue-1", "reachable", address="http://100.64.0.12:47873", seen=3, studios=("voice", "image")),
               node("blue-2", "reachable", address="http://100.64.0.13:47873", seen=9),
               node("blue-3", "unreachable", address="http://100.64.0.14:47873", seen=2 * DAY + 3 * HOUR,
                    detail="could not reach the node: All connection attempts failed")],
        workers=[worker("blue-ctl", "voice", models=[KOKORO]), worker("blue-1", "voice", busy=True, models=[KOKORO, QWEN]),
                 worker("blue-1", "image", models=[FLUX]), worker("blue-2", "voice", models=[KOKORO]),
                 worker("blue-3", "voice", online=False, models=[KOKORO])],
        models=[model(KOKORO, "Kokoro 82M", "voice", "blue-ctl", need=3.0, size=0.4),
                model(KOKORO, "Kokoro 82M", "voice", "blue-1", need=3.0, size=0.4),
                model(QWEN, "Qwen3 TTS 0.6B", "voice", "blue-1"),
                model(QWEN, "Qwen3 TTS 0.6B", "voice", "blue-2", fit="tight", available=False),
                model(FLUX, "FLUX.2 Klein 4B", "image", "blue-1", need=10.0, size=8.0)],
        running=[batch("b-7355", "voice", QWEN, "Podcast ep 31", total=9, done=3, running=1, queued=5, started=9 * MIN),
                 batch("b-7350", "voice", KOKORO, "Ad reads", total=40, done=22, running=2, queued=16,
                       started=41 * MIN, stalled=True, note="waiting on a free slot")],
    )
    studio2 = location(
        "ctrl-studio2", "Studio 2", "http://100.64.0.21:47873", version="0.6.124",
        machines=[machine("s2-ctl", "Studio 2 mini", "Apple M4 Pro", 24.0, 14.5, 302.0, modalities={"image": 90, "voice": 12},
                          disk_total=460.0),
                  machine("s2-1", "Studio 2 A", "Apple M4", 16.0, 11.0, 244.0, modalities={"image": 55}),
                  machine("s2-2", "Studio 2 B", "Apple M4", 16.0, 3.1, 14.0, health="up",
                          modalities={"image": 41}, disk_total=256.0, start=startup(services=False))],
        activities=[activity("s2-ctl", "long_idle", 102, 2, 70.0), activity("s2-1", "ready", 55, 0, 49.0),
                    activity("s2-2", "working", 41, 5, 58.0)],
        nodes=[node("s2-1", "reachable", address="http://100.64.0.22:47873", version="0.6.124", seen=6, studios=("image",)),
               node("s2-2", "unauthorized", address="http://100.64.0.23:47873", version="0.6.120", seen=1200,
                    detail="the node answered but rejected this controller's credential (HTTP 401) - re-join it "
                           "with the current Fleet Token", studios=("image",))],
        workers=[worker("s2-ctl", "image", models=[FLUX]), worker("s2-1", "image", models=[FLUX]),
                 worker("s2-2", "image", busy=True, models=[FLUX])],
        models=[model(FLUX, "FLUX.2 Klein 4B", "image", "s2-ctl", need=10.0, size=8.0),
                model(FLUX, "FLUX.2 Klein 4B", "image", "s2-1", need=10.0, size=8.0),
                model(FLUX, "FLUX.2 Klein 4B", "image", "s2-2", need=10.0, size=8.0, fit="tight")],
        running=[batch("b-7362", "image", FLUX, "Cover set 4", total=24, done=9, running=1, queued=14, started=15 * MIN)],
    )
    garage = location(
        "ctrl-garage", "Garage", "http://100.64.0.31:47873", reachable=False,
        error="No answer within 5s. The address is reachable but nothing replied — check the machine is awake and "
              "that Tailscale is up at both ends.",
        nodes=[node("g-1", "unreachable", address="http://100.64.0.32:47873", seen=4 * DAY),
               node("g-2", "unreachable", address="http://100.64.0.33:47873", seen=4 * DAY)],
        unreachable_since=NOW - 4 * DAY, members_from="last_known", members_stale_seconds=4 * DAY,
    )
    rows = [local, blue, studio2, garage]
    flavour = os.environ.get("GATEWAY_RENDER_FIXTURE", "")
    if flavour.startswith("many"):
        rows = many(rows)
    return {
        **({"fleet_warnings": {"hidden": ["macos_updates"], "hideable": []}} if flavour == "many-hidden" else {}),
        "locations": rows,
        "machines": fleet.machines_flat(rows),
        "running": fleet.running_flat(rows),
        "summary": fleet._summarise(rows),
        "issues": fleet._issues(rows),
        "generated_at": NOW,
        "release": {
            "identity": {"algorithm": "ed25519", "public_key_base64": "A" * 44, "public_key_hex": "ab" * 32,
                         "fingerprint": "cd" * 32, "publisher_id": "gatewaykh"},
            "last_push": {"updated_at": NOW - 3 * HOUR, "fingerprint": "cd" * 32,
                          "release": {"version": "0.6.126", "build": "a1b2c3d", "filename": "StudioHub-0.6.126.dmg",
                                      "bytes": 4_400_000_000, "sha256": "ef" * 32, "signature": "sig"},
                          "locations": {"ctrl-blue": {"ok": True, "status": 200},
                                        "ctrl-studio2": {"ok": False, "status": 409, "code": "PUBLISHER_NOT_TRUSTED",
                                                         "detail": "no trusted gateway key is installed."}}},
        },
    }


if __name__ == "__main__":
    json.dump(build(), sys.stdout, default=str)
