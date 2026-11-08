"""Reusable fixtures and builders for Remote Developer Gate contract tests.

Exports coherent test fixtures for gateRequest, capabilities, policy, status, and
tar archive helpers for source and result validation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tarfile

from gateway.gate_contract import encoded, validate


def now_utc_str() -> str:
    """Current UTC timestamp formatted as RFC 3339 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_runner(**overrides) -> dict:
    runner = {
        "id": "studio.app-gate-runner.v1",
        "revision": "runner-rev-1",
        "sha256": "1" * 64,
    }
    runner.update(overrides)
    return runner


def make_tool_pin(**overrides) -> dict:
    pin = {
        "id": "swift",
        "version": "16.0.0",
        "manifest_sha256": "2" * 64,
    }
    pin.update(overrides)
    return pin


def make_tool_manifest(**overrides) -> dict:
    tm = {
        "format": "studio.tool-manifest.v1",
        "id": "manifest-pilot-1",
        "revision": "tm-rev-1",
        "sha256": "3" * 64,
        "provenance": "hub-packaged-official-hash-pinned",
        "tools": [make_tool_pin()],
    }
    tm.update(overrides)
    return tm


def make_source(**overrides) -> dict:
    src = {
        "commit": "d9c950547cb119c6aa57c54f9e2014a678593692",
        "media_type": "application/x-tar",
        "sha256": "103a4d5d882726c035f12e5d0b21587d6d8f2da74c51f4583c25e5c41cf6a8b2",
        "size_bytes": 1024,
        "expanded_bytes": 2048,
        "file_count": 2,
    }
    src.update(overrides)
    return src


def make_limits(**overrides) -> dict:
    lim = {
        "runtime_seconds": 300,
        "workspace_bytes": 10 * 1024 * 1024 * 1024,       # 10 GiB
        "result_spool_bytes": 1024 * 1024 * 1024,          # 1 GiB
        "shared_cache_bytes": 512 * 1024 * 1024,          # 512 MiB
        "minimum_free_bytes": 20 * 1024 * 1024 * 1024,     # 20 GiB
        "toolchain_install_bytes": 1024 * 1024 * 1024,     # 1 GiB
        "max_process_tree_rss_bytes": 4 * 1024 * 1024 * 1024,  # 4 GiB
        "max_system_swap_growth_bytes": 1024 * 1024 * 1024,     # 1 GiB
        "start_min_free_memory_bytes": 1024 * 1024 * 1024,      # 1 GiB
    }
    lim.update(overrides)
    return lim


def make_bounds(**overrides) -> dict:
    b = {
        "json_bytes": 1024 * 1024,
        "source_bytes": 50 * 1024 * 1024,
        "source_expanded_bytes": 200 * 1024 * 1024,
        "source_files": 10000,
        "result_bytes": 50 * 1024 * 1024,
        "log_bytes": 10 * 1024 * 1024,
        "workspace_bytes": 20 * 1024 * 1024 * 1024,
        "result_spool_bytes": 5 * 1024 * 1024 * 1024,
        "shared_cache_bytes": 5 * 1024 * 1024 * 1024,
        "toolchain_install_bytes": 5 * 1024 * 1024 * 1024,
        "max_process_tree_rss_bytes": 8 * 1024 * 1024 * 1024,
        "max_system_swap_growth_bytes": 4 * 1024 * 1024 * 1024,
        "runtime_seconds": 600,
        "tool_prepare_seconds": 600,
        "inventory_max_age_seconds": 300,
        "result_files": 1000,
        "workspace_enforcement": "monitored_stop",
        "sample_interval_seconds": 1.0,
    }
    b.update(overrides)
    return b


def make_gate(**overrides) -> dict:
    gate = {
        "id": "gate-pilot-1",
        "revision": "gate-rev-1",
        "sha256": "4" * 64,
        "source": make_source(),
        "command_fixture_sha256": "d0b8658d783c7680a96ad0c12526d3ab2ce2ae12c87edb733be20f25cbb3d9df",
        "dependency_manifest_sha256": "5" * 64,
        "tool_manifest": make_tool_manifest(),
        "required_toolchains": [make_tool_pin()],
        "limits": make_limits(),
        "budget_provenance": "requested",
        "check_ids": ["lint", "unit-tests"],
    }
    gate.update(overrides)
    return gate


def make_policy(controller: str = "c1", node: str = "n1", **overrides) -> dict:
    pol = {
        "format": "studio.developer-gate.policy.v1",
        "revision": "policy-rev-1",
        "minimum_internal_physical_bytes": 500000000000,
        "allowed_targets": [{"controller_id": controller, "node_id": node}],
        "excluded_targets": [],
    }
    pol.update(overrides)
    return pol


def make_internal_physical_drive(timestamp: str | None = None, **overrides) -> dict:
    ts = timestamp or now_utc_str()
    drive = {
        "device_id": "disk0",
        "internal": True,
        "physical": True,
        "size_bytes": 1000000000000,  # 1 TB decimal
        "source": "diskutil-info-plist",
        "workspace_backing_device_id": "disk0",
        "backing_source": "diskutil-info-plist",
        "observed_at": ts,
        "reason": None,
    }
    drive.update(overrides)
    return drive


def make_space(timestamp: str | None = None, **overrides) -> dict:
    ts = timestamp or now_utc_str()
    sp = {
        "observed_at": ts,
        "workspace_bytes": 0,
        "result_spool_bytes": 0,
        "shared_cache_bytes": 0,
        "workspace_limit_bytes": 20 * 1024 * 1024 * 1024,
        "result_spool_limit_bytes": 5 * 1024 * 1024 * 1024,
        "shared_cache_limit_bytes": 5 * 1024 * 1024 * 1024,
        "cleanup_state": "none",
        "reason": None,
    }
    sp.update(overrides)
    return sp


def make_inventory(controller: str = "c1", node: str = "n1", timestamp: str | None = None, **overrides) -> dict:
    ts = timestamp or now_utc_str()
    inv = {
        "format": "studio.developer-gate.inventory.v1",
        "node_id": node,
        "controller_id": controller,
        "observed_at": ts,
        "internal_physical_drive": make_internal_physical_drive(timestamp=ts),
        "free_bytes": 200 * 1024 * 1024 * 1024,  # 200 GiB
        "toolchains": [
            {
                "id": "swift",
                "available": True,
                "installed_bytes": 500 * 1024 * 1024,
                "observed_at": ts,
                "provenance": "owned-toolchain-readback",
                "pin": make_tool_pin(),
                "reason": None,
            }
        ],
        "developer_gate_space": make_space(timestamp=ts),
    }
    inv.update(overrides)
    return inv


def make_capabilities(controller: str = "c1", node: str = "n1", timestamp: str | None = None, **overrides) -> dict:
    ts = timestamp or now_utc_str()
    pol = make_policy(controller=controller, node=node)
    cap = {
        "format": "studio.developer-gate.capabilities.v1",
        "contract": "studio.developer-gate.v1",
        "node_id": node,
        "controller_id": controller,
        "server_time": ts,
        "supported": True,
        "reason": None,
        "runner": make_runner(),
        "stop_proof": {
            "revision": "stop-rev-1",
            "proof_sha256": "6" * 64,
            "stop_latency_seconds": 0.5,
        },
        "setting": {
            "enabled": True,
            "verified": True,
            "revision": "setting-rev-1",
        },
        "admission_policy": pol,
        "bounds": make_bounds(),
        "concurrency": 1,
        "gates": [make_gate()],
        "tool_manifests": [make_tool_manifest()],
        "inventory": make_inventory(controller=controller, node=node, timestamp=ts),
    }
    cap.update(overrides)
    return cap


def make_gate_request(gate: dict | None = None, **overrides) -> dict:
    g = gate or make_gate()
    req = {
        "format": "studio.developer-gate.request.v1",
        "run_id": "run-001",
        "test_id": "test-001",
        "runner": make_runner(),
        "node_setting_revision": "setting-rev-1",
        "admission_policy_revision": "policy-rev-1",
        "minimum_internal_physical_bytes": 500000000000,
        "requested_limits": copy.deepcopy(g["limits"]),
        "gate": copy.deepcopy(g),
        "source": copy.deepcopy(g["source"]),
        "tool_manifest": copy.deepcopy(g["tool_manifest"]),
    }
    req.update(overrides)
    return req


def make_acceptance(request: dict, controller: str = "c1", node: str = "n1", **overrides) -> dict:
    req_sha = hashlib.sha256(encoded(request)).hexdigest()
    acc = {
        "attempt_id": "attempt-001",
        "idempotency_key": "idemp-001",
        "request_sha256": req_sha,
        "node_id": node,
        "controller_id": controller,
    }
    acc.update(overrides)
    return acc


def make_check(check_id: str = "lint", outcome: str = "passed", exit_code: int = 0, **overrides) -> dict:
    log_text = f"check {check_id} outcome={outcome} exit={exit_code}\n".encode("utf-8")
    chk = {
        "id": check_id,
        "outcome": outcome,
        "exit_code": exit_code,
        "duration_seconds": 1.0,
        "log_sha256": hashlib.sha256(log_text).hexdigest(),
        "log_bytes": len(log_text),
        "reason": None,
    }
    chk.update(overrides)
    return chk


def make_disk_sample(timestamp: str | None = None, **overrides) -> dict:
    ts = timestamp or now_utc_str()
    sample = {
        "free_bytes": 200 * 1024 * 1024 * 1024,
        "workspace_bytes": 0,
        "result_spool_bytes": 0,
        "shared_cache_bytes": 0,
        "observed_at": ts,
    }
    sample.update(overrides)
    return sample


def make_result_manifest(
    request: dict,
    acceptance: dict,
    timestamp: str | None = None,
    outcome: str = "passed",
    **overrides,
) -> dict:
    ts = timestamp or now_utc_str()
    gate = request["gate"]
    checks = []
    for cid in gate.get("check_ids", []):
        chk = make_check(
            check_id=cid,
            outcome=outcome if outcome == "passed" else "failed",
            exit_code=0 if outcome == "passed" else 1,
        )
        checks.append(chk)

    manifest = {
        "format": "studio.developer-gate.result.v1",
        "acceptance": acceptance,
        "runner": copy.deepcopy(request["runner"]),
        "gate": copy.deepcopy(gate),
        "source": copy.deepcopy(request["source"]),
        "tool_manifest": copy.deepcopy(request["tool_manifest"]),
        "actual_toolchains": [
            {
                "id": tpin["id"],
                "available": True,
                "installed_bytes": 500 * 1024 * 1024,
                "observed_at": ts,
                "provenance": "owned-toolchain-readback",
                "pin": copy.deepcopy(tpin),
                "reason": None,
            }
            for tpin in gate.get("required_toolchains", [])
        ],
        "outcome": outcome,
        "exit_code": 0 if outcome == "passed" else 1,
        "termination_signal": None,
        "reason": None if outcome == "passed" else {"code": "CHECK_FAILED", "detail": "One or more checks failed."},
        "started_at": ts,
        "finished_at": ts,
        "before": make_disk_sample(timestamp=ts) if outcome == "passed" else None,
        "peak_workspace_bytes": 1024 * 1024 if outcome == "passed" else 0,
        "process_release": {
            "confirmed": True,
            "observed_at": ts,
            "owned_processes_remaining": 0,
            "proof": "owned-process-identities-exited",
        },
        "internal_physical_drive": make_internal_physical_drive(timestamp=ts),
        "checks": checks,
    }
    manifest.update(overrides)
    return manifest


def make_result_descriptor(tar_bytes: bytes, **overrides) -> dict:
    desc = {
        "media_type": "application/x-tar",
        "sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "size_bytes": len(tar_bytes),
    }
    desc.update(overrides)
    return desc


def make_status(
    request: dict,
    acceptance: dict,
    capabilities: dict,
    state: str = "passed",
    result_descriptor: dict | None = None,
    **overrides,
) -> dict:
    ts = now_utc_str()
    ctrl = capabilities["controller_id"]
    node = capabilities["node_id"]

    is_terminal = state in ("passed", "failed", "cancelled", "interrupted", "not_run")
    st = {
        "format": "studio.developer-gate.status.v1",
        "acceptance": acceptance,
        "state": state,
        "reason": None if state == "passed" else {"code": "TEST_FAILED", "detail": "Gate failed."},
        "accepted_at": ts,
        "updated_at": ts,
        "resource_release_confirmed": is_terminal,
        "result": result_descriptor if state == "passed" else None,
        "result_acknowledged": False,
        "workspace_removed": False,
        "inventory": make_inventory(controller=ctrl, node=node, timestamp=ts),
        "acknowledged_result": None,
        "acknowledged_at": None,
        "cleanup_after": None,
    }
    st.update(overrides)
    return st


def make_source_tar_bytes(files: dict[str, bytes] | None = None) -> tuple[bytes, dict]:
    """Create a real uncompressed tar archive and matching source descriptor."""
    if files is None:
        files = {
            "src/main.swift": b"print(\"hello gate\")\n",
            "Package.swift": b"// swift-tools-version: 5.9\n",
        }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        for name, data in sorted(files.items()):
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(data))
    tar_bytes = buf.getvalue()
    source_desc = {
        "commit": "d9c950547cb119c6aa57c54f9e2014a678593692",
        "media_type": "application/x-tar",
        "sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "size_bytes": len(tar_bytes),
        "expanded_bytes": sum(len(d) for d in files.values()),
        "file_count": len(files),
    }
    return tar_bytes, source_desc


def make_result_tar_bytes(
    request: dict,
    capabilities: dict,
    acceptance: dict,
    outcome: str = "passed",
    custom_logs: dict[str, bytes] | None = None,
) -> tuple[bytes, dict]:
    """Create a real uncompressed result tar containing manifest.json and logs/*.log."""
    ts = now_utc_str()
    checks_declared = request["gate"].get("check_ids", [])
    logs = custom_logs or {}

    checks = []
    for cid in checks_declared:
        log_bytes = logs.get(cid, f"check {cid} passed log output\n".encode("utf-8"))
        chk = {
            "id": cid,
            "outcome": outcome if outcome == "passed" else "failed",
            "exit_code": 0 if outcome == "passed" else 1,
            "duration_seconds": 1.2,
            "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
            "log_bytes": len(log_bytes),
            "reason": None if outcome == "passed" else {"code": "CHECK_FAILED", "detail": "failed"},
        }
        checks.append(chk)
        logs[cid] = log_bytes

    manifest = make_result_manifest(
        request=request,
        acceptance=acceptance,
        timestamp=ts,
        outcome=outcome,
        checks=checks,
    )
    manifest_bytes = json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        ti = tarfile.TarInfo(name="manifest.json")
        ti.size = len(manifest_bytes)
        ti.type = tarfile.REGTYPE
        tar.addfile(ti, io.BytesIO(manifest_bytes))

        for cid in checks_declared:
            ldata = logs[cid]
            ti = tarfile.TarInfo(name=f"logs/{cid}.log")
            ti.size = len(ldata)
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(ldata))

    return buf.getvalue(), manifest


def result_tar(request: dict, cap: dict, acceptance: dict) -> bytes:
    """Convenience helper returning valid result tar bytes directly."""
    tar_bytes, _ = make_result_tar_bytes(request, cap, acceptance, outcome="passed")
    return tar_bytes


def fixture(controller: str = "c1", node: str = "n1", now: datetime | None = None) -> tuple[dict, dict, dict]:
    """Return coherent, valid (request, cap, policy) tuple matching controller and node."""
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ") if now else now_utc_str()
    pol = make_policy(controller=controller, node=node)
    tar_bytes, source_desc = make_source_tar_bytes()
    gate = make_gate(source=source_desc)
    cap = make_capabilities(
        controller=controller,
        node=node,
        timestamp=ts,
        admission_policy=copy.deepcopy(pol),
        gates=[copy.deepcopy(gate)],
    )
    req = make_gate_request(gate=copy.deepcopy(gate))
    return req, cap, pol
