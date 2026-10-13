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
# The gateway modules open their data directory when imported (and `fleet` reads the owner's pause file), so
# a render must never see the real one: point it at a throwaway directory unless the caller chose one.
if not os.environ.get("GATEWAY_APP_SUPPORT"):
    import tempfile
    os.environ["GATEWAY_APP_SUPPORT"] = tempfile.mkdtemp(prefix="gateway-fixture-")
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


def model(repo, label, studio, mid, *, available=True, cache="cached", fit="fits", need=6.0, size=3.4, package=None):
    return {"model": repo, "model_id": repo, "package_id": package or repo.split("/")[-1].lower(),
            "label": label, "family": label.split()[0], "studio": studio,
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
FISH = "majentik/fishaudio-s2-pro-MLX-4bit"


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


def shops() -> dict:
    """GATEWAY_RENDER_FIXTURE=workstation: the "Shops" location, each Mac in one Lite Mode state.

    Shop 1 takes work, Shop 2 is in use (standing by), Shop 3 just stepped aside, Shop 4 is in its quiet hours,
    Shop 5 has the mode off, and Shop 6 runs a Hub too old to report it (no `workstation` block).
    """
    def ws(state, *, enabled=True, reason=None, jobs=0, yields=0):
        return {"enabled": enabled, "idle_only": True, "idle_minutes": 3, "step_aside": True, "resume_after_minutes": 2,
                "start_free_gb": None, "quiet_hours": ["22:00-07:00"], "hide_menu_bar_icon": False, "state": state,
                "reason": reason, "yields_today": yields, "jobs_today": jobs}
    blocks = {"shop-ctl": ws("working", jobs=5), "shop-1": ws("working", jobs=3, yields=1),
              "shop-2": ws("standing_by", reason="someone is using this Mac", jobs=2),
              "shop-3": ws("yielded", reason="macOS reported critical memory pressure", jobs=4, yields=1),
              "shop-4": ws("quiet_hours", reason="quiet hours until 07:00", jobs=7),
              "shop-5": ws("off", enabled=False), "shop-6": None}
    names = {"shop-ctl": "Shops front desk", **{f"shop-{i}": f"Shop {i}" for i in range(1, 7)}}
    machines, activities, nodes = [], [], []
    for i, (mid, block) in enumerate(blocks.items()):
        row = machine(mid, names[mid], "Apple M4", 16.0, 2.1 if mid == "shop-2" else 6.5 + i, 120.0 + 9 * i,
                      disk_total=256.0, modalities={"voice": 10 + i})
        if block is not None:
            row["workstation"] = block
        machines.append(row)
        activities.append(activity(mid, "working" if mid == "shop-1" else "ready", 20 + i, 0, 45.0))
        if mid != "shop-ctl":
            nodes.append({**node(mid, "reachable", address=f"http://100.64.7.{i}:47873", seen=4,
                                 version="0.6.120" if mid == "shop-6" else "0.6.126"), "name": names[mid]})
    return location("ctrl-shops", "Shops", "http://100.64.7.1:47873", machines=machines, activities=activities,
                    nodes=nodes, workers=[worker(mid, "voice", busy=mid == "shop-1", models=[KOKORO]) for mid in blocks],
                    models=[model(KOKORO, "Kokoro 82M", "voice", mid, need=3.0, size=0.4) for mid in blocks])


LATEST = "0.6.132"
FIVE = ("voice", "transcription", "chat", "music", "image")


def health(flavour: str) -> tuple[list, dict]:
    """GATEWAY_RENDER_FIXTURE=health-idle|health-mixed|health-problem: the live 2026-10-02 fleet shape.

    Every node still carries the `video` worker its controller recorded at join (offline, never seen), its
    Hub calls it `needs_attention` with a `partial_read` limitation, and three of them failed a job last
    night - exactly what made a healthy idle fleet read "5 of 6 groups need attention". `health-idle` must
    render all green; `health-mixed` adds Macs updating and one behind; `health-problem` adds real faults.
    """
    def start(version=LATEST, services=True):
        return {"open_at_login": True, "start_services_on_launch": services, "app_version": version,
                "app_reporting": True, "stale": None,
                "services_running": {"up": 5, "total": 5, "studios": {s: "up" for s in FIVE}}}

    def idle(mid, ago, *, error=False, state="needs_attention"):
        return {"machine": mid, "state": state, "current_job": None, "stalled_seconds": None,
                "latest": {"state": "error" if error else "done", "finished_at": NOW - ago, "observed_at": NOW - ago,
                           "error_code": "HTTP 409: local is reserved for other work on this Mac" if error else None},
                "completed": 6, "failed": int(error), "median_runtime_s": None,
                "limitation": "Direct activity partially unavailable", "limitation_code": "partial_read",
                "limitation_reason": "Some reporters missing", "limitation_since": None,
                "limitation_affects": ["progress", "failed", "median_runtime"]}

    def wk(mid, studio, online=True):
        row = worker(mid, studio, online=online)
        row["modality"] = studio
        if studio == "video":
            row["last_seen_at"] = None
        return row

    def site(lid, label, n, *, versions=None, offline=(), crashed=(), held=LATEST, startup_off=(), red=(),
             no_hub=(), stops=None):
        """`no_hub`: offline Macs that answer but run no Studio Hub. `stops`: mid -> the `last_stop` its Mac recorded."""
        stops = stops or {}
        versions = versions or {}
        mids = [f"terranash-{label[-4:-2]}{i:02d}-m2-8-256" for i in range(1, n + 1)]
        ctl = f"terranash-{label[-4:]}-m4-16-256"
        machines = [machine("local", ctl, "Apple M4", 16.0, 9.8, 120.0, disk_total=245.0, start=start(versions.get("local", held)))]
        machines[0]["label"] = ctl
        activities = [idle("local", 15 * HOUR, state="long_idle")]
        nodes, workers = [], [wk("local", s) for s in FIVE]
        for i, mid in enumerate(mids):
            off = mid in offline
            version = versions.get(mid, LATEST)
            row = machine(mid, mid, "Apple M2" if i % 2 else "Apple M1", 8.0 if i % 3 else 16.0, 3.9 + i % 3,
                          9.0 if mid in red else 150.0 + 7 * i, disk_total=245.0, reachable=not off,
                          health=("no_hub" if mid in no_hub else "unreachable") if off else "up",
                          start=start(version, services=mid not in startup_off))
            if mid in stops:
                row["last_stop"] = stops[mid]
            machines.append(row)
            activities.append(idle(mid, (16 + i) * HOUR, error=i in (1, 2)) if not off else
                              {**idle(mid, 30 * HOUR, state="offline"), "limitation_code": "waiting_for_worker",
                               "limitation": "Activity evidence pending"})
            nodes.append({**node(mid, "unreachable" if off else "reachable", address=f"http://100.64.{lid[-1]}.{i}:47873",
                                 version=version, seen=6 * HOUR if off else 4,
                                 detail=(f"could not reach the node ({'no_hub' if mid in no_hub else 'unreachable'})"
                                         if off else None),
                                 studios=(*FIVE, "video")), "role": "agent",
                          **({"reason": "no_hub" if mid in no_hub else "unreachable"} if off else {})})
            workers += [wk(mid, s, online=not off and (mid, s) not in crashed) for s in FIVE]
            workers.append(wk(mid, "video", online=False))
        release = {"version": held, "published_at": NOW - (300 if flavour == "health-mixed" else 6 * HOUR),
                   "publisher_id": "gatewaykh", "publisher_key_id": "cd" * 32}
        return location(lid, label, f"http://100.64.{lid[-1]}.1:47873", version=versions.get("local", held),
                        machines=machines, activities=activities, nodes=nodes, workers=workers, release=release,
                        hub_version_relation="newer")

    local = location("local", "This Mac", "http://127.0.0.1:47873", local=True, version=LATEST,
                     machines=[machine("local", "This Mac", "Apple M2 Max", 32.0, 18.4, 312.0, start=start())],
                     activities=[idle("local", 10 * HOUR, state="long_idle")],
                     workers=[wk("local", s) for s in FIVE],
                     release={"version": LATEST, "published_at": NOW - 6 * HOUR, "publisher_id": "gatewaykh",
                              "publisher_key_id": "cd" * 32})
    for row in local["activity"]["machines"]:
        row.update(limitation=None, limitation_code=None, limitation_reason=None, limitation_affects=None)
    push_at = NOW - (300 if flavour == "health-mixed" else 6 * HOUR)
    push = {"updated_at": push_at, "fingerprint": "cd" * 32,
            "release": {"version": LATEST, "build": "e6a8b80", "filename": f"StudioHub-{LATEST}-e6a8b80.dmg",
                        "bytes": 1_372_776_123, "sha256": "ef" * 32, "signature": "sig"},
            "locations": {}}
    # 1401 never answers (its old stop belongs to an earlier outage); 1402 answers with no Hub, and its Mac
    # recorded a force-quit after the controller last saw it; 1403 has no Hub and an older crash; 1404 is up.
    why_offline = dict(
        offline={"terranash-1401-m2-8-256", "terranash-1402-m2-8-256", "terranash-1403-m2-8-256"},
        no_hub={"terranash-1402-m2-8-256", "terranash-1403-m2-8-256"},
        stops={"terranash-1401-m2-8-256": {"reason": "quit", "at": NOW - 3 * DAY, "label": "Quit", "relaunched": True},
               "terranash-1402-m2-8-256": {"reason": "force_quit", "at": NOW - 5 * HOUR + 60, "label": "Force-quit",
                                           "relaunched": False},
               "terranash-1403-m2-8-256": {"reason": "crashed", "at": NOW - 40 * HOUR, "label": "Crashed (restarted)",
                                           "relaunched": True},
               "terranash-1404-m2-8-256": {"reason": "updated", "at": NOW - 2 * HOUR, "label": "Restarted for an update",
                                           "relaunched": True}})
    if flavour == "health-offline":
        # GATEWAY_RENDER_FIXTURE=health-offline: why a Mac is offline (`last_stop`), a short page that fits one screen.
        rows = [local, site("c1400", "terranash-1400", 5, **why_offline)]
    elif flavour == "health-mixed":
        # Published five minutes ago to 1100 and 1200 (their Macs are still installing it); 1300 was not
        # in that publish, so it and its Macs are behind and the line says to publish to that group.
        rows = [local,
                site("c1100", "terranash-1100", 10, versions={"terranash-1103-m2-8-256": "0.6.131",
                                                          "terranash-1107-m2-8-256": "0.6.131"}),
                site("c1200", "terranash-1200", 8, versions={"terranash-1205-m2-8-256": "0.6.126"}),
                site("c1300", "terranash-1300", 9, held="0.6.131",
                     versions={"local": "0.6.131", "terranash-1302-m2-8-256": "0.6.126",
                               **{f"terranash-13{i:02d}-m2-8-256": "0.6.131" for i in (1, 3, 4, 5)}})]
    elif flavour == "health-catchup":
        # Automatic catch-up (fix/release-catchup): 1600 joined after the publish and was sent it by the
        # Gateway, 1500 is being sent it now, 1400 trusts another key, 1300's push failed and backs off.
        trusted = {"reported": True, "publisher_id": "gatewaykh", "fingerprint": "cd" * 32}
        rows = [local, site("c1100", "terranash-1100", 4), site("c1600", "terranash-1600", 3),
                site("c1500", "terranash-1500", 3, held="0.6.131", versions={"local": "0.6.131"}),
                site("c1400", "terranash-1400", 3, held="0.6.131", versions={"local": "0.6.131"}),
                site("c1300", "terranash-1300", 3, held="0.6.131", versions={"local": "0.6.131"}),
                site("c1200", "terranash-1200", 4)]
        for row in rows[1:]:
            row["trusted_publisher"] = trusted
        rows[4]["trusted_publisher"] = {**trusted, "fingerprint": "9f" * 32}
        auto = {"auto": True, "status": None, "code": None, "detail": None, "release": None}
        push["locations"] = {
            "c1100": {"ok": True, "status": 200, "release": rows[1]["release"]},
            "c1200": {"ok": True, "status": 200, "release": rows[6]["release"]},
            "c1300": {**auto, "ok": False, "at": NOW - 40 * MIN, "retry_at": NOW + 20 * MIN,
                      "code": "LOCATION_UNREACHABLE",
                      "detail": "could not reach the controller at http://100.64.3.1:47873 (ConnectTimeout)"},
            "c1400": {**auto, "ok": False, "skipped": True, "at": NOW - 2 * MIN, "code": "CATCHUP_SKIPPED",
                      "detail": "it doesn't trust this Gateway's App Update Key (it trusts 9f9f9f9f9f9f…); copy this Gateway's "
                                "App Update Key public key from Gateway → Settings → App Update Key → Technical details "
                                "into Hub → Connections → App updates, then compare fingerprints (this Gateway: "
                                + "cd" * 32 + ")"},
            "c1500": {**auto, "ok": None, "pending": True, "at": NOW - 30},
            "c1600": {**auto, "ok": True, "status": 200, "at": NOW - 25 * MIN, "release": rows[2]["release"]},
        }
    elif flavour == "health-problem":
        rows = [local,
                site("c1100", "terranash-1100", 10, crashed={("terranash-1104-m2-8-256", "voice")},
                     startup_off={"terranash-1109-m2-8-256"}),
                site("c1200", "terranash-1200", 8, versions={"terranash-1205-m2-8-256": "0.6.126"},
                     red={"terranash-1203-m2-8-256"}),
                site("c1400", "terranash-1400", 8, **why_offline)]
        rows[1]["running_jobs"] = [batch("b-9001", "voice", QWEN, "Chapter 4 narration", total=12, done=5, running=1,
                                         queued=6, started=50 * MIN, stalled=True)]
    elif flavour.startswith("health-rollout"):
        # A paced rollout of LATEST (feat/paced-rollout): this Mac and 1100 are done (two 1100 Macs still
        # updating themselves), 1200 is installing, the rest wait. `-failed`: 1200 never came back.
        old = "0.6.131"
        rows = [local,
                site("c1100", "terranash-1100", 10, versions={"terranash-1103-m2-8-256": old,
                                                          "terranash-1107-m2-8-256": old,
                                                          **({"local": old} if flavour == "health-rollout-errors" else {})}),
                site("c1200", "terranash-1200", 8, held=old,
                     versions={"local": old, **{f"terranash-12{i:02d}-m2-8-256": old for i in range(1, 9)}})]
        for lid, label, n in (("c1300", "terranash-1300", 9), ("c1400", "terranash-1400", 8),
                              ("c1500", "terranash-1500", 3), ("c1600", "terranash-1600", 4)):
            rows.append(site(lid, label, n, held=old, versions={"local": old, **{
                f"terranash-{label[-4:-2]}{i:02d}-m2-8-256": old for i in range(1, n + 1)}}))
        rows[2]["reachable"] = flavour.endswith(("failed", "errors"))
        if flavour == "health-rollout-errors":
            # GATEWAY_RENDER_FIXTURE=health-rollout-errors: Hub 0.6.152 `startup.last_update_error`. The rollout is at 1100,
            # whose controller (still on the old release) failed downloading 20 min ago - its step shows it - and one of
            # its nodes failed installing 2 h ago. A node's failure from two days ago and a Mac already on the newest
            # release (1105) say nothing.
            def failed(mid: str, **error) -> None:
                row = next(m for m in rows[1]["machines"] if m["machine"] == mid)
                row["startup"]["last_update_error"] = {"build": "e6a8b80", **error}
            failed("local", phase="downloading", at=NOW - 20 * MIN,
                   reason="Install the update failed. HTTP 404 for the release image (not_ready for 15 min).")
            failed("terranash-1107-m2-8-256", phase="installing", at=NOW - 2 * HOUR,
                   reason="The app that landed reports build 91ac2f0, not e6a8b80.")
            failed("terranash-1103-m2-8-256", phase="verifying", at=NOW - 2 * DAY, reason="The signature did not verify.")
            failed("terranash-1105-m2-8-256", phase="staging", at=NOW - 3 * HOUR, reason="Not enough disk space to stage it.")
    else:
        rows = [local, site("c1100", "terranash-1100", 10), site("c1200", "terranash-1200", 8),
                site("c1300", "terranash-1300", 9), site("c1400", "terranash-1400", 8),
                site("c1500", "terranash-1500", 3)]
    return rows, push


def rollout(snapshot: dict, flavour: str) -> dict | None:
    """The rollout block `admin.fleet` adds, from the real `release_rollout.status` over a written state."""
    from gateway import release_rollout

    failed = flavour.endswith("failed")
    order = ["local", "c1100", "c1200", "c1300", "c1400", "c1500", "c1600"]
    labels = {"local": "This Mac", **{cid: f"terranash-{cid[1:]}" for cid in order[1:]}}
    states = ({"local": "updated", "c1100": "sent"} if flavour == "health-rollout-errors" else
              {"local": "updated", "c1100": "updated", "c1200": "failed" if failed else "installing"})
    release_rollout._write({
        "mode": "paced", "state": "failed" if failed else "running", "version": LATEST, "build": "e6a8b80",
        "order": order, "started_at": NOW - 40 * MIN, "finished_at": NOW if failed else None,
        "reason": "terranash-1200 did not come back on 0.6.132 within 30 min, so the rollout stopped there"
                  if failed else None,
        "steps": {cid: {"label": labels[cid], "state": states.get(cid, "waiting"),
                        **({"detail": "did not come back on 0.6.132 within 30 min"} if states.get(cid) == "failed"
                           else {})} for cid in order}})
    return release_rollout.status(snapshot)


def assemble(rows: list, push: dict) -> dict:
    """The snapshot exactly as `fleet._refresh` assembles it, plus the release block `admin.fleet` adds."""
    fleet.stamp_latest(rows, push)
    machines = fleet.machines_flat(rows)
    fleet.stamp_controller_versions(rows, machines)
    return {
        "locations": rows, "machines": machines, "running": fleet.running_flat(rows),
        "summary": fleet._summarise(rows), "issues": fleet._issues(rows), "generated_at": NOW,
        "fleet_warnings": {"hidden": ["macos_updates"], "hideable": []},
        "release": {"identity": {"algorithm": "ed25519", "public_key_base64": "A" * 44, "public_key_hex": "ab" * 32,
                                 "fingerprint": "cd" * 32, "publisher_id": "gatewaykh"},
                    "last_push": push},
    }


def networks() -> None:
    """The fixture's networks: Blue Room and This Mac on A, Studio 2 on B, blue-3 moved to C by hand; Shops on C
    with Shop 2 moved to A and Shop 1 set to C by hand (the owner's real Shops group, 2026-10-05)."""
    from gateway import model_share

    model_share._write_json(model_share.networks_path(), {  # noqa: SLF001
        "locations": {**model_share.DEFAULT_NETWORKS, "ctrl-blue": "A", "ctrl-studio2": "B", "local": "A", "ctrl-shops": "C"},
        "machines": {"ctrl-blue/blue-3": "C", "4bb919aee2e4/imac-3": "E", "ctrl-shops/shop-2": "A",
                     "ctrl-shops/shop-1": "C"}})


def installs() -> list:
    """"Install on every Mac that can run it", mid-spread on an owner-shaped fleet: the real planner (`model_share`)
    driven by fake controllers, so every sentence on the progress card is the gateway's own."""
    from gateway import model_share, startup_control

    report = os.environ.get("GATEWAY_RENDER_REPORT")
    if report:
        if report not in {"completed", "active", "failure"}:
            raise ValueError("GATEWAY_RENDER_REPORT must be completed, active, or failure")
        macs = []
        for index in range(60):
            state = "done"
            if report == "active" and index < 3:
                state = "copying"
            elif report == "active" and index < 5:
                state = "waiting"
            elif report == "failure" and index < 2:
                state = "failed"
            elif report == "completed" and index >= 57:
                state = "skipped"
            macs.append({"location_id": "fixture-room", "machine": f"fixture-mac-{index}",
                         "label": f"Mac {index + 1:02d}", "network": "ABC"[index // 20],
                         "state": state, "from_key": "fixture-source", "from_label": "Network source Mac",
                         "total_bytes": 3_500_000_000, "bytes_done": 1_470_000_000,
                         "detail": "The model copy could not be verified. Retry from the network source."
                                   if state == "failed" else "Offline — not installed." if state == "skipped" else None,
                         "note": "The final machine's retained installation note." if index == 59 else None})
        label = "Fish Audio S2 Pro 5B MLX 4-bit"
        if report == "active":
            label += " — extended publisher label for minimum-width installation-report proof"
        return [model_share.view({"id": "fixture-report", "package_id": "fish-audio-s2-pro-4bit",
                                  "label": label, "state": "running" if report == "active" else "finished",
                                  "created_at": NOW - HOUR, "finished_at": None if report == "active" else NOW,
                                  "macs": macs})]

    package, runs = "fish-audio-s2-pro-4bit", {}
    groups = {"de7250e146a7": ["1101", "1102", "1103"], "9701220bf30f": ["1201", "1202"],
              "a311a0a9f4f7": ["1301", "1302"], "eda08bae6325": ["1401"], "b8ccda585b97": ["1501"],
              "4bb919aee2e4": ["imac-2", "imac-3"]}
    machines, models = [], []
    for loc, mids in groups.items():
        rows = []
        for mid in mids:
            has, lite = mid == "1101", mid == "imac-2"
            machines.append({"location_id": loc, "machine": mid, "label": mid, "reachable": True,
                             "workstation": {"enabled": True} if lite else None,
                             "hub_version": "0.6.174",
                             "installable_models": [] if has or lite else [{"package_id": package,
                                                                            "label": "Fish S2 Pro 4-bit"}]})
            rows.append({"machine": mid, "package_id": package, "label": "Fish S2 Pro 4-bit",
                         "cache_state": "cached" if has else "absent", "memory_eligible": not lite})
        models.append({"id": loc, "version": "0.6.174", "local_models": rows})
    fleet_read = {"machines": machines, "locations": models}

    def share(loc, mid, pkg):
        return {"ok": True, "share": {"url_base": f"http://{mid}/files", "token": "t", "files": []}}

    def install(loc, mid, pkg, *, source=None, peer_only=False):
        assert not peer_only or source is not None, "strict fixture copies require a source"
        runs[mid] = {"id": mid, "state": "running", "total_bytes": 3_500_000_000, "bytes_done": 0,
                     "source_used": "lan" if source else "internet", "peer_only": peer_only}
        return {"ok": True, "run": dict(runs[mid])}

    def status(loc, mid, run):
        return {"ok": True, "run": dict(runs[run])}

    def progress(**done) -> None:
        for mid, fraction in done.items():
            runs[mid].update(state="done" if fraction >= 1 else "running",
                             bytes_done=int(3_500_000_000 * min(fraction, 1)))
        model_share.tick(fleet_read)

    startup_control.share_model, startup_control.install_model = share, install
    startup_control.model_install_status = status
    model_share._ensure_thread = lambda: None  # noqa: SLF001 - the fixture drives the passes itself
    model_share.start(package, fleet_read)
    model_share.tick(fleet_read)
    progress(**{"1102": 1, "1103": 0.42, "1301": 1, "1501": 0.37, "imac-3": 1})
    progress(**{"1201": 0.63, "1202": 0.1, "1302": 0.8, "1401": 0.05})
    return model_share.status()


def memory() -> list:
    """GATEWAY_RENDER_FIXTURE=memory: two 8 GB groups. "Breeze Room" generates on all three Macs at 1.3-1.55 GB
    free (amber meters, but normal: no Attention). "Stuck Room" has one Mac idle at 1.2 GB free for 10 minutes (cannot take work:
    Attention), beside a healthy idle one."""
    def room(lid, label, specs):
        machines, activities, nodes = [], [], []
        for i, (mid, avail, busy) in enumerate(specs):
            job = {"batch_id": f"b-{mid}", "model": QWEN, "label": "Breeze chapter", "studio": "image",
                   "started_at": NOW - 6 * MIN} if busy else None
            machines.append(machine(mid, f"{label} {i + 1}", "Apple M1", 8.0, avail, 140.0, disk_total=256.0,
                                    modalities={"image": 12}, current=job))
            activities.append(activity(mid, "working" if busy else "ready", 12, 0, 80.0))
            nodes.append(node(mid, "reachable", address=f"http://100.64.9.{i + 2}:47873", seen=4, studios=("image",)))
        return location(lid, label, f"http://100.64.9.1:47873", machines=machines, activities=activities, nodes=nodes,
                        workers=[worker(m["machine"], "image", busy=bool(m.get("current"))) for m in machines])
    breeze = room("ctrl-breeze", "Breeze Room", [("br-1", 1.3, True), ("br-2", 1.5, True), ("br-3", 1.55, True)])
    stuck = room("ctrl-stuck", "Stuck Room", [("st-1", 1.2, False), ("st-2", 5.1, False)])
    # The fixture cannot wait five minutes: st-1 has read idle-and-low for ten already.
    fleet._MEMORY_LOW_SINCE[("ctrl-stuck", "st-1")] = NOW - 10 * MIN  # noqa: SLF001
    return [breeze, stuck]


def build() -> dict:
    flavour = os.environ.get("GATEWAY_RENDER_FIXTURE", "")
    networks()
    if flavour.startswith("health"):
        snapshot = assemble(*health(flavour))
        if flavour.startswith("health-rollout"):
            snapshot["release"]["rollout"] = rollout(snapshot, flavour)
        return snapshot
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
                model(FLUX, "FLUX.2 Klein 4B", "image", "blue-1", need=10.0, size=8.0),
                # "Install a model…": Fish fits the 16 GB Macs and is on none of them; the 8 GB Mac is never offered it.
                *[model(FISH, "Fish S2 Pro 4-bit", "voice", mid, cache="absent", available=False, need=16.0, size=3.5,
                        package="fish-audio-s2-pro-4bit", fit="fits" if mid != "blue-2" else "too_big")
                  for mid in ("blue-ctl", "blue-1", "blue-2")],
                model(QWEN, "Qwen3 TTS 0.6B", "voice", "blue-ctl", cache="absent", available=False)],
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
    if flavour in ("paused", "paused-node"):
        # GATEWAY_RENDER_FIXTURE=paused: this Mac paused by the owner (grey, nothing wrong) beside a group the
        # gateway paused after an outage (red, with Resume). `paused-node`: one node of a group paused by hand.
        # Few cards, so the whole page fits one render.
        local.update(paused_at=NOW - 2 * HOUR, paused_by="owner", pause_reason=fleet.OWNER_PAUSE_REASON)
        garage.update(paused_at=NOW - 3600, paused_by="outage",
                      pause_reason="unreachable since 2026-09-29 — paused after 3 days. Routing skips it; nothing "
                                   "was removed. Bring the Mac back, or resume it from the Fleet page.")
        next(n for n in studio2["nodes"] if n["id"] == "s2-1")["paused"] = True
        rows = [local, garage] if flavour == "paused" else [studio2]
    if flavour in ("workstation", "lite-pending"):
        rows = [shops()]
    if flavour == "memory":
        rows = memory()
    if flavour == "lite-pending":
        # GATEWAY_RENDER_FIXTURE=lite-pending: Shop 3 went offline after a Lite Mode change was saved for it.
        shop = rows[0]
        shop["machines"] = [dict(m, reachable=False, health="unreachable", workstation=dict(m["workstation"], stale=True))
                            if m["machine"] == "shop-3" else m
                            for m in shop["machines"]]
        shop["activity"]["machines"] = [dict(a, state="offline") if a["machine"] == "shop-3" else a
                                        for a in shop["activity"]["machines"]]
        shop["nodes"] = [dict(n, state="unreachable", last_seen_at=NOW - 2 * HOUR) if n["id"] == "shop-3" else n
                         for n in shop["nodes"]]
    if flavour.startswith("many"):
        rows = many(rows)
    render_add = None
    if flavour == "add-location":
        # GATEWAY_RENDER_FIXTURE=add-location: Studio 2 too slow to answer this time, keeping what it last
        # reported; the overlay answers the Add with the row the gateway's own `pending_location` builds.
        studio2.update(reachable=False, slow=True, role="controller", version="0.6.124", answered_at=NOW - 4 * MIN,
                       error="Slow to answer: didn't answer within 6 s — last report "
                             + time.strftime("%H:%M", time.localtime(NOW - 4 * MIN)) + ".")
        shops_row = {"id": "4bb919aee2e4", "label": "Shops", "url": "http://100.120.246.9:47873"}
        render_add = {"controller": shops_row, "location": fleet.pending_location(shops_row),
                      "credential": {"scope": "fleet", "detail": "This is the Fleet token — the right one."},
                      "discovered": 7, "discovery_error": None, "enable_seed": None}
    fleet.stamp_latest(rows, {"fingerprint": "cd" * 32, "updated_at": NOW - 3 * HOUR,
                              "release": {"version": "0.6.126"}})
    machines = fleet.machines_flat(rows)
    fleet.stamp_controller_versions(rows, machines)
    for row in machines:
        if flavour == "lite-pending" and row["machine"] == "shop-3":
            row["lite_mode_pending"] = {"settings": {"enabled": True, "idle_only": True, "idle_minutes": 10,
                                                    "step_aside": True, "resume_after_minutes": 5,
                                                    "start_free_gb": 3.5, "quiet_hours": ["21:00-08:00"],
                                                    "hide_menu_bar_icon": True},
                                        "at": NOW - HOUR, "offline_since": row.get("node_last_seen_at")}
    from gateway import model_share

    stamped = {"locations": rows, "machines": machines}
    model_share.stamp_networks(stamped)
    return {
        **({"fleet_warnings": {"hidden": ["macos_updates"], "hideable": []}} if flavour == "many-hidden" else {}),
        **({"render_add": render_add} if render_add else {}),
        "model_installs": installs(),
        "locations": stamped["locations"],
        "machines": stamped["machines"],
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
