"""Wire validation for Remote Developer Gates (contract studio.developer-gate.v1).

The frozen design bytes live in ``data/developer-gate-v1.schema.json`` (sha256
a97d32a008f62f17293ee0666001ba79207c0367b4424ce6888731f40e66ad9a) and normative
wire rules in ``docs/developer-gate-wire-v1.txt`` (sha256
67122d464f591daf34dcaa58664c7e82d8e7b7ef425373ff28aacd5375cf43dd); this module
refuses to run if those schema bytes change.

``decode`` provides the strict byte path (UTF-8, duplicate keys, nonfinite/unsafe numbers,
ceiling before parse) into closed named-schema validation.

``validate_request``, ``verify_source``, ``verify_result``, and ``validate_status``
enforce the normative wire contract: live physical device bounds, packaged gate/manifest
matching, free space reservations, uncompressed tar member safety, strict result manifest
exit/outcome semantics, and ACK tombstone consistency.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tarfile
from typing import NoReturn

from .developer_contract import DeveloperTestError, _check, _SUPPORTED
from .render_hash_v1 import _constant, _integer, _pairs

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "developer-gate-v1.schema.json"
_SCHEMA_SHA256 = "a97d32a008f62f17293ee0666001ba79207c0367b4424ce6888731f40e66ad9a"

# ponytail: 32 MiB is a reader ceiling for developer gate JSON envelopes; raise with wire revision if payloads grow.
_MAX_BYTES = 32 * 1024 * 1024

_SCHEMA = None


class GateError(DeveloperTestError):
    """A wire refusal: HTTP status, stable machine code, human detail."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status = int(status)
        self.code = str(code)
        self.detail = str(detail)

    def to_dict(self) -> dict:
        return {
            "format": "studio.developer-gate.error.v1",
            "reason": {"code": self.code, "detail": self.detail},
            "acceptance": None,
        }


def _number(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def _timestamp(text: str) -> datetime:
    """RFC 3339 UTC 'Z' instant; anything else is not a contract timestamp."""
    if type(text) is not str or "T" not in text[:11] or not text.endswith("Z"):
        raise ValueError("not a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("not a UTC Z timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("not a UTC Z timestamp")
    return parsed


def _schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        candidates = [_SCHEMA_PATH]
        raw = None
        loaded_path = None
        for candidate in candidates:
            if candidate.is_file():
                try:
                    raw = candidate.read_bytes()
                    loaded_path = candidate
                    break
                except OSError:
                    continue
        if raw is None:
            raise GateError(500, "SCHEMA_UNAVAILABLE", "The developer gate schema file is unavailable.")
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != _SCHEMA_SHA256:
            raise GateError(500, "SCHEMA_INTEGRITY",
                            f"The developer gate schema bytes at {loaded_path} do not match the pinned SHA256.")
        try:
            loaded = json.loads(raw.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeError) as exc:
            raise GateError(500, "SCHEMA_INTEGRITY", str(exc)) from exc
        _SCHEMA = loaded
    return _SCHEMA


def encoded(value: object) -> bytes:
    """Canonical wire JSON: ensure_ascii=True, sort_keys=True, compact separators, allow_nan=False."""
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode(raw: bytes, kind: str, ceiling: int = _MAX_BYTES) -> dict | list:
    """Strict UTF-8 JSON bytes -> closed named-schema dict/list, or GateError."""
    # ponytail: ceiling bounds memory before decode; upgrade if larger envelopes needed.
    if type(raw) is not bytes:
        raise GateError(400, "INVALID_JSON", "Wire input must be bytes.")
    if type(ceiling) is not int or ceiling <= 0:
        raise GateError(500, "INVALID_CEILING", "The caller byte ceiling must be a positive integer.")
    effective_ceiling = min(ceiling, _MAX_BYTES)
    if len(raw) > effective_ceiling:
        raise GateError(413, "PAYLOAD_TOO_LARGE", f"The body exceeds the {effective_ceiling} byte ceiling.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise GateError(400, "INVALID_JSON", "A UTF-8 BOM is forbidden.")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GateError(400, "INVALID_JSON", f"The body is not strict UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_number,
            parse_constant=_constant,
        )
    except ValueError as exc:
        reason = str(exc).removeprefix("Invalid render hash value: ")
        raise GateError(400, "INVALID_JSON", reason or "The body is not valid JSON.") from exc
    except RecursionError as exc:
        raise GateError(400, "INVALID_JSON", "The body nests too deeply.") from exc
    return validate(value, kind)


def validate(value: object, kind: str) -> object:
    """Validate against the frozen named schema definition."""
    schema = _schema()
    defs = schema.get("$defs")
    if type(kind) is not str or type(defs) is not dict or kind not in defs:
        raise GateError(500, "UNKNOWN_KIND", f"No frozen schema defines {kind!r}.")
    try:
        _check(value, {"$ref": "#/$defs/" + kind}, kind, schema)
    except DeveloperTestError as exc:
        raise GateError(exc.status, exc.code, exc.detail) from exc
    if kind == "capabilities":
        _capability_semantics(value)
    return value


def _capability_semantics(caps: dict) -> None:
    inventory = caps["inventory"]
    if any(inventory[field] != caps[field] for field in ("controller_id", "node_id")):
        raise GateError(409, "IDENTITY_MISMATCH", "Capability inventory belongs to another controller or node.")
    bounds = caps["bounds"]
    if caps["supported"]:
        proof = caps.get("stop_proof")
        if proof is None:
            raise GateError(502, "INVALID_HUB_RESPONSE", "supported capabilities carry no stop proof.")
        if proof.get("stop_latency_seconds", 0) <= 0:
            raise GateError(502, "INVALID_HUB_RESPONSE", "The stop proof asserts no positive stop latency.")
        if not caps.get("gates"):
            raise GateError(502, "INVALID_HUB_RESPONSE", "supported capabilities advertise no gates.")
        if not caps.get("tool_manifests"):
            raise GateError(502, "INVALID_HUB_RESPONSE", "supported capabilities advertise no tool manifests.")
        if caps.get("concurrency") != 1:
            raise GateError(502, "INVALID_HUB_RESPONSE", "capabilities concurrency must be 1.")
    else:
        if caps.get("reason") is None:
            raise GateError(502, "INVALID_HUB_RESPONSE", "Unsupported capabilities carry no reason.")
    seen_gates = set()
    for g in caps.get("gates", []):
        gid = g.get("id")
        if gid in seen_gates:
            raise GateError(502, "INVALID_HUB_RESPONSE", f"Gate {gid!r} is declared twice.")
        seen_gates.add(gid)
    seen_manifests = set()
    for tm in caps.get("tool_manifests", []):
        tmid = tm.get("id")
        if tmid in seen_manifests:
            raise GateError(502, "INVALID_HUB_RESPONSE", f"Tool manifest {tmid!r} is declared twice.")
        seen_manifests.add(tmid)


def validate_request(
    request: dict | bytes,
    capabilities: dict | bytes,
    policy: dict | bytes,
    controller_id: str,
    node_id: str,
    now: datetime | None = None,
) -> dict:
    """Validate a GateRequest against node capabilities and owner admission policy.

    controller_id and node_id identify the target in Gateway roster terms (e.g. 'local').
    Parent verifies cap.controller_id against Fleet location and cap.node_id against target.
    """
    if isinstance(request, (bytes, bytearray)):
        req = decode(bytes(request), "gateRequest")
    else:
        req = validate(request, "gateRequest")

    if isinstance(capabilities, (bytes, bytearray)):
        cap = decode(bytes(capabilities), "capabilities")
    else:
        cap = validate(capabilities, "capabilities")

    if isinstance(policy, (bytes, bytearray)):
        pol = decode(bytes(policy), "policy")
    else:
        pol = validate(policy, "policy")

    if not cap.get("supported"):
        reason = cap.get("reason") or {}
        detail = reason.get("detail", "Node capabilities are unsupported.")
        raise GateError(503, "UNSUPPORTED_CAPABILITIES", detail)

    # 1. Policy allow / exclude checks using Gateway target identity
    for excluded in pol.get("excluded_targets", []):
        if excluded.get("controller_id") == controller_id and excluded.get("node_id") == node_id:
            raise GateError(403, "TARGET_EXCLUDED", f"Target {controller_id}/{node_id} is in policy excluded_targets.")

    allowed = pol.get("allowed_targets", [])
    if not any(t.get("controller_id") == controller_id and t.get("node_id") == node_id for t in allowed):
        raise GateError(403, "TARGET_NOT_ALLOWED", f"Target {controller_id}/{node_id} is not in policy allowed_targets.")

    # 2. Policy revision and minimum internal physical bytes match
    if req["admission_policy_revision"] != pol["revision"]:
        raise GateError(409, "POLICY_REVISION_MISMATCH",
                        f"Request policy revision {req['admission_policy_revision']!r} != policy {pol['revision']!r}.")
    if req["minimum_internal_physical_bytes"] != pol["minimum_internal_physical_bytes"]:
        raise GateError(409, "POLICY_MINIMUM_BYTES_MISMATCH",
                        f"Request minimum_internal_physical_bytes {req['minimum_internal_physical_bytes']} != policy {pol['minimum_internal_physical_bytes']}.")
    cap_policy = cap.get("admission_policy", {})
    if cap_policy.get("revision") != pol["revision"]:
        raise GateError(409, "CAPABILITY_POLICY_MISMATCH",
                        f"Capability policy revision {cap_policy.get('revision')!r} != current policy {pol['revision']!r}.")
    if cap_policy.get("minimum_internal_physical_bytes") != pol["minimum_internal_physical_bytes"]:
        raise GateError(409, "CAPABILITY_POLICY_MISMATCH",
                        "Capability minimum_internal_physical_bytes does not match current policy.")

    # 3. Node setting revision & enabled verification
    cap_setting = cap.get("setting", {})
    if req["node_setting_revision"] != cap_setting.get("revision"):
        raise GateError(409, "SETTING_REVISION_MISMATCH",
                        f"Request node_setting_revision {req['node_setting_revision']!r} != node setting {cap_setting.get('revision')!r}.")
    if not cap_setting.get("enabled") or not cap_setting.get("verified"):
        raise GateError(409, "SETTING_NOT_ENABLED", "Node developer setting is not enabled and verified.")

    # 4. Runner match
    if req["runner"] != cap.get("runner"):
        raise GateError(409, "RUNNER_MISMATCH", "Request runner does not match node runner exactly.")

    # 5. Packaged Gate & Tool Manifest exact match
    req_gate = req["gate"]
    matching_gate = None
    for g in cap.get("gates", []):
        if g.get("id") == req_gate.get("id"):
            matching_gate = g
            break
    if matching_gate is None:
        raise GateError(404, "GATE_NOT_FOUND", f"Gate {req_gate.get('id')!r} is not advertised by node.")
    if req_gate != matching_gate:
        raise GateError(409, "GATE_MISMATCH", "Request gate does not match advertised gate definition exactly.")
    if req["source"] != req_gate["source"]:
        raise GateError(409, "SOURCE_MISMATCH", "Request source does not match gate source exactly.")
    if req["tool_manifest"] != req_gate["tool_manifest"]:
        raise GateError(409, "TOOL_MANIFEST_MISMATCH", "Request tool_manifest does not match gate tool_manifest.")

    matching_manifest = None
    for tm in cap.get("tool_manifests", []):
        if tm.get("id") == req["tool_manifest"].get("id"):
            matching_manifest = tm
            break
    if matching_manifest is None:
        raise GateError(404, "TOOL_MANIFEST_NOT_FOUND", f"Tool manifest {req['tool_manifest'].get('id')!r} is not advertised.")
    if req["tool_manifest"] != matching_manifest:
        raise GateError(409, "TOOL_MANIFEST_MISMATCH", "Request tool_manifest does not match advertised manifest.")

    tool_pins = req["tool_manifest"].get("tools", [])
    if tool_pins != req_gate.get("required_toolchains", []):
        raise GateError(409, "REQUIRED_TOOLS_MISMATCH",
                        "Tool manifest tools do not match gate required_toolchains exactly.")

    # 6. Unique IDs
    check_ids = req_gate.get("check_ids", [])
    if len(check_ids) != len(set(check_ids)):
        raise GateError(409, "DUPLICATE_CHECK_ID", "Gate check_ids contains duplicate check identifiers.")
    tool_ids = [t["id"] for t in tool_pins]
    if len(tool_ids) != len(set(tool_ids)):
        raise GateError(409, "DUPLICATE_TOOL_ID", "Tool manifest contains duplicate tool identifiers.")

    # 7. Timestamps and Freshness
    bounds = cap.get("bounds", {})
    max_age = bounds.get("inventory_max_age_seconds", 300)
    check_time = now if now is not None else datetime.now(timezone.utc)

    inv = cap.get("inventory", {})
    drive = inv.get("internal_physical_drive", {})
    space = inv.get("developer_gate_space", {})

    for ts_name, ts_str in (
        ("server_time", cap.get("server_time")),
        ("inventory.observed_at", inv.get("observed_at")),
        ("drive.observed_at", drive.get("observed_at")),
        ("space.observed_at", space.get("observed_at")),
    ):
        if not ts_str:
            raise GateError(409, "MISSING_TIMESTAMP", f"Capability missing {ts_name}.")
        try:
            parsed_ts = _timestamp(ts_str)
        except ValueError as exc:
            raise GateError(400, "INVALID_TIMESTAMP", f"{ts_name} is not a valid UTC Z timestamp: {exc}") from exc
        age = (check_time - parsed_ts).total_seconds()
        if age > max_age:
            raise GateError(409, "STALE_INVENTORY",
                            f"Measurement {ts_name} age {age:.1f}s exceeds maximum age {max_age}s.")
        if age < -300:
            raise GateError(409, "INVALID_TIMESTAMP", f"Measurement {ts_name} is in the future.")

    # 8. Physical drive verification
    if not drive.get("internal"):
        raise GateError(409, "NOT_INTERNAL_DRIVE", "Drive must be internal.")
    if not drive.get("physical"):
        raise GateError(409, "NOT_PHYSICAL_DRIVE", "Drive must be physical.")
    if drive.get("source") != "diskutil-info-plist":
        raise GateError(409, "UNVERIFIED_DRIVE_SOURCE", "Drive source must be 'diskutil-info-plist'.")
    if drive.get("backing_source") not in ("diskutil-info-plist", "diskutil-apfs-list-plist"):
        raise GateError(409, "UNVERIFIED_BACKING_SOURCE", "Backing source must be a verified diskutil device mapping.")
    dev_id = drive.get("device_id")
    backing_id = drive.get("workspace_backing_device_id")
    if not dev_id or dev_id != backing_id:
        raise GateError(409, "DRIVE_MAPPING_MISMATCH",
                        f"Physical device_id {dev_id!r} does not equal workspace_backing_device_id {backing_id!r}.")

    min_drive_bytes = pol.get("minimum_internal_physical_bytes", 0)
    drive_size = drive.get("size_bytes")
    if drive_size is None or type(drive_size) not in (int, float):
        raise GateError(409, "UNKNOWN_MEASUREMENT", "Physical drive size_bytes is unknown or null.")
    if min_drive_bytes > 0 and drive_size < min_drive_bytes:
        raise GateError(409, "INSUFFICIENT_PHYSICAL_DRIVE",
                        f"Physical drive size {drive_size} bytes is below policy minimum {min_drive_bytes} bytes.")

    # 9. Finite request limits against gate manifest limits AND capability bounds
    req_limits = req.get("requested_limits", {})
    gate_limits = req_gate.get("limits", {})
    comparisons = (
        ("runtime_seconds", True),
        ("workspace_bytes", True),
        ("result_spool_bytes", True),
        ("shared_cache_bytes", True),
        ("toolchain_install_bytes", True),
        ("max_process_tree_rss_bytes", True),
        ("max_system_swap_growth_bytes", True),
    )
    for key, in_bounds in comparisons:
        val = req_limits.get(key)
        if val is None or not math.isfinite(val):
            raise GateError(400, "INVALID_LIMIT", f"Requested limit {key} must be a finite number.")
        gate_max = gate_limits.get(key)
        if gate_max is not None and val > gate_max:
            raise GateError(409, "LIMIT_EXCEEDS_GATE",
                            f"Requested {key} ({val}) exceeds gate manifest limit ({gate_max}).")
        if in_bounds:
            cap_max = bounds.get(key)
            if cap_max is not None and val > cap_max:
                raise GateError(409, "LIMIT_EXCEEDS_BOUNDS",
                                f"Requested {key} ({val}) exceeds capability bound ({cap_max}).")

    for key in ("minimum_free_bytes", "start_min_free_memory_bytes"):
        if req_limits[key] < gate_limits[key]:
            raise GateError(409, "LIMIT_BELOW_GATE", f"Requested {key} cannot lower the packaged safety minimum.")

    source = req.get("source", {})
    if source.get("size_bytes", 0) > bounds.get("source_bytes", 0):
        raise GateError(413, "SOURCE_TOO_LARGE", "Source size exceeds capability source_bytes bound.")
    if source.get("expanded_bytes", 0) > bounds.get("source_expanded_bytes", 0):
        raise GateError(413, "SOURCE_EXPANDED_TOO_LARGE", "Source expanded_bytes exceeds bound.")
    if source.get("file_count", 0) > bounds.get("source_files", 0):
        raise GateError(413, "SOURCE_FILES_TOO_MANY", "Source file_count exceeds bound.")

    # 10. Free space reservations
    free_bytes = inv.get("free_bytes")
    if free_bytes is None or type(free_bytes) not in (int, float) or not math.isfinite(free_bytes):
        raise GateError(409, "UNKNOWN_MEASUREMENT", "Inventory free_bytes is unknown or null.")

    actual_free_need = (
        source.get("size_bytes", 0)
        + source.get("expanded_bytes", 0)
        + req_limits.get("workspace_bytes", 0)
        + req_limits.get("result_spool_bytes", 0)
        + req_limits.get("shared_cache_bytes", 0)
        + req_limits.get("toolchain_install_bytes", 0)
        + req_limits.get("minimum_free_bytes", 0)
    )
    if free_bytes < actual_free_need:
        raise GateError(409, "INSUFFICIENT_FREE_SPACE",
                        f"Available free space {free_bytes} bytes is less than required {actual_free_need} bytes.")

    spool_used = space.get("result_spool_bytes")
    spool_limit = space.get("result_spool_limit_bytes")
    if spool_used is None or spool_limit is None:
        raise GateError(409, "UNKNOWN_MEASUREMENT", "Result spool measurement is unknown or null.")
    if spool_used + req_limits.get("result_spool_bytes", 0) > spool_limit:
        raise GateError(409, "RESULT_SPOOL_LIMIT_EXCEEDED",
                        f"Result spool reservation {spool_used + req_limits.get('result_spool_bytes', 0)} exceeds limit {spool_limit}.")

    ws_used = space.get("workspace_bytes")
    ws_limit = space.get("workspace_limit_bytes")
    if ws_used is None or ws_limit is None:
        raise GateError(409, "UNKNOWN_MEASUREMENT", "Workspace measurement is unknown or null.")
    if ws_used + req_limits.get("workspace_bytes", 0) > ws_limit:
        raise GateError(409, "WORKSPACE_LIMIT_EXCEEDED",
                        f"Workspace reservation {ws_used + req_limits.get('workspace_bytes', 0)} exceeds limit {ws_limit}.")

    cache_used = space.get("shared_cache_bytes")
    cache_limit = space.get("shared_cache_limit_bytes", 0)
    if cache_limit > 0 and (cache_used or 0) + req_limits.get("shared_cache_bytes", 0) > cache_limit:
        raise GateError(409, "SHARED_CACHE_LIMIT_EXCEEDED", "Shared cache reservation exceeds limit.")

    return req


def _archive_name(member: tarfile.TarInfo) -> str:
    name = member.name
    canonical = name[:-1] if member.isdir() and name.endswith('/') else name
    if (not canonical or canonical == '.' or canonical.startswith('/') or '\\' in canonical
            or '\0' in canonical or '..' in canonical.split('/')
            or os.path.normpath(canonical) != canonical):
        raise GateError(400, 'UNSAFE_TAR_MEMBER', 'Archive member has an unsafe or aliased path.')
    return canonical


def verify_source(path: Path | str, request: dict, capabilities: dict) -> None:
    """Validate source tar archive without extracting.

    Validates regular file, hash, size, plain uncompressed tar, safe unique member names
    (directories and regular files only, no symlinks/sparse/device/escape), and prewrite bounds.
    """
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise GateError(400, "INVALID_SOURCE", f"Source at {p} must be an existing regular file, not a symlink or directory.")

    bounds = capabilities.get("bounds", {})
    source_desc = request.get("source", {})
    expected_size = source_desc.get("size_bytes")
    stat = p.stat()
    if stat.st_size != expected_size:
        raise GateError(400, "SOURCE_SIZE_MISMATCH",
                        f"Source archive size {stat.st_size} bytes does not match descriptor {expected_size} bytes.")
    if stat.st_size > bounds.get("source_bytes", _MAX_BYTES):
        raise GateError(413, "SOURCE_TOO_LARGE", f"Source archive size {stat.st_size} bytes exceeds bound.")

    # Compute raw sha256
    hasher = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    actual_sha = hasher.hexdigest()
    if actual_sha != source_desc.get("sha256"):
        raise GateError(400, "SOURCE_HASH_MISMATCH",
                        f"Source sha256 {actual_sha} does not match descriptor {source_desc.get('sha256')}.")

    # Inspect plain uncompressed tar without extraction
    try:
        tar = tarfile.open(p, mode="r:")
    except (tarfile.ReadError, OSError) as exc:
        raise GateError(400, "INVALID_TAR", f"Source must be a valid plain uncompressed tar archive: {exc}") from exc

    with tar:
        regular_count = 0
        total_count = 0
        expanded_bytes = 0
        seen_names = set()
        max_files = bounds.get("source_files", 65536)
        max_expanded = bounds.get("source_expanded_bytes", _MAX_BYTES)

        for member in tar.getmembers():
            total_count += 1
            if total_count > max_files:
                raise GateError(413, "SOURCE_FILES_EXCEEDED", f"Source entries exceed bound {max_files}.")

            name = member.name
            if name.startswith("/") or name.startswith("\\"):
                raise GateError(400, "UNSAFE_TAR_MEMBER", f"Source member {name!r} has absolute path.")
            if "\0" in name:
                raise GateError(400, "UNSAFE_TAR_MEMBER", "Source member name contains NUL byte.")
            parts = Path(name).parts
            if ".." in parts:
                raise GateError(400, "UNSAFE_TAR_MEMBER", f"Source member {name!r} contains path traversal '..'.")

            norm_name = _archive_name(member)
            if norm_name in seen_names:
                raise GateError(400, "DUPLICATE_TAR_MEMBER", f"Source archive contains duplicate member: {name!r}.")
            seen_names.add(norm_name)

            if member.issparse():
                raise GateError(400, "UNSAFE_TAR_MEMBER", f"Source member {name!r} is a sparse file.")

            if member.isdir():
                pass
            elif member.isfile():
                regular_count += 1
                expanded_bytes += member.size
                if expanded_bytes > max_expanded:
                    raise GateError(413, "SOURCE_EXPANDED_BYTES_EXCEEDED",
                                    f"Source expanded bytes exceed bound {max_expanded}.")
            else:
                raise GateError(400, "UNSAFE_TAR_MEMBER",
                                f"Source member {name!r} has disallowed type (must be regular file or directory).")

        expected_count = source_desc.get("file_count")
        if regular_count != expected_count:
            raise GateError(400, "SOURCE_FILE_COUNT_MISMATCH",
                            f"Source regular file count {regular_count} does not match descriptor {expected_count}.")
        expected_expanded = source_desc.get("expanded_bytes")
        if expanded_bytes != expected_expanded:
            raise GateError(400, "SOURCE_EXPANDED_BYTES_MISMATCH",
                            f"Source expanded bytes {expanded_bytes} does not match descriptor {expected_expanded}.")


def verify_result(path: Path | str, request: dict, capabilities: dict, acceptance: dict) -> dict:
    """Validate immutable result archive and return verified resultManifest dict.

    Validates uncompressed tar containing exactly manifest.json and logs/<check-id>.log for each check,
    regular-only safe unique bounded files, log hashes/sizes/UTF-8, strict manifest schema,
    pinned identities, process release, and exit/outcome semantics.
    """
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise GateError(400, "INVALID_RESULT", f"Result at {p} must be an existing regular file, not a symlink.")

    bounds = capabilities.get("bounds", {})
    stat = p.stat()
    if stat.st_size > bounds.get("result_bytes", _MAX_BYTES):
        raise GateError(413, "RESULT_TOO_LARGE", f"Result archive size {stat.st_size} exceeds result_bytes bound.")

    try:
        tar = tarfile.open(p, mode="r:")
    except (tarfile.ReadError, OSError) as exc:
        raise GateError(400, "INVALID_TAR", f"Result must be a valid plain uncompressed tar archive: {exc}") from exc

    with tar:
        members = tar.getmembers()
        max_files = bounds.get("result_files", 4096)
        if len(members) > max_files:
            raise GateError(413, "RESULT_FILES_EXCEEDED", f"Result entries {len(members)} exceed bound {max_files}.")

        member_map = {}
        for m in members:
            name = m.name
            if m.isdir():
                raise GateError(400, "FORBIDDEN_DIR_ENTRY", f"Directory entry {name!r} is forbidden in result archive.")
            if not m.isfile() or m.issparse() or m.issym() or m.islnk():
                raise GateError(400, "UNSAFE_TAR_MEMBER", f"Result member {name!r} must be a regular non-sparse file.")
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts or "\0" in name:
                raise GateError(400, "UNSAFE_TAR_MEMBER", f"Result member {name!r} has unsafe path.")
            norm_name = _archive_name(m)
            if norm_name in member_map:
                raise GateError(400, "DUPLICATE_TAR_MEMBER", f"Result archive contains duplicate member: {name!r}.")
            member_map[norm_name] = m

        if "manifest.json" not in member_map:
            raise GateError(400, "MISSING_MANIFEST", "Result archive is missing manifest.json.")

        manifest_member = member_map["manifest.json"]
        max_json = bounds.get("json_bytes", 1048576)
        if manifest_member.size > max_json:
            raise GateError(413, "MANIFEST_TOO_LARGE", f"manifest.json size {manifest_member.size} exceeds json_bytes bound.")

        mf = tar.extractfile(manifest_member)
        if mf is None:
            raise GateError(400, "INVALID_MANIFEST", "Could not read manifest.json from result archive.")
        manifest_bytes = mf.read()

        manifest = decode(manifest_bytes, "resultManifest", ceiling=max_json)

        # 1. Pinned acceptance check
        if manifest.get("acceptance") != acceptance:
            raise GateError(409, "ACCEPTANCE_MISMATCH", "Result manifest acceptance does not match accepted attempt.")
        req_sha = hashlib.sha256(encoded(request)).hexdigest()
        if acceptance.get("request_sha256") != req_sha:
            raise GateError(409, "REQUEST_HASH_MISMATCH", "Acceptance request_sha256 does not match canonical request hash.")

        # 2. Pinned identities from request
        if manifest.get("runner") != request.get("runner"):
            raise GateError(409, "RUNNER_MISMATCH", "Result manifest runner does not match request runner.")
        if manifest.get("gate") != request.get("gate"):
            raise GateError(409, "GATE_MISMATCH", "Result manifest gate does not match request gate.")
        if manifest.get("source") != request.get("source"):
            raise GateError(409, "SOURCE_MISMATCH", "Result manifest source does not match request source.")
        if manifest.get("tool_manifest") != request.get("tool_manifest"):
            raise GateError(409, "TOOL_MANIFEST_MISMATCH", "Result manifest tool_manifest does not match request.")

        # 3. Archive member exactness: manifest.json and logs/<check-id>.log ONLY
        checks = manifest.get("checks", [])
        check_ids = [c["id"] for c in checks]
        if len(check_ids) != len(set(check_ids)):
            raise GateError(400, "DUPLICATE_CHECK_ID", "Result manifest checks contain duplicate check IDs.")

        expected_members = {"manifest.json"} | {f"logs/{cid}.log" for cid in check_ids}
        actual_members = set(member_map.keys())
        if actual_members != expected_members:
            unexpected = sorted(actual_members - expected_members)
            missing = sorted(expected_members - actual_members)
            raise GateError(400, "INVALID_RESULT_MEMBERS",
                            f"Result members mismatch: unexpected={unexpected}, missing={missing}.")

        # 4. Verify each check log file
        max_log_bytes = bounds.get("log_bytes", _MAX_BYTES)
        for c in checks:
            cid = c["id"]
            log_key = f"logs/{cid}.log"
            log_member = member_map[log_key]
            if log_member.size != c.get("log_bytes"):
                raise GateError(400, "LOG_SIZE_MISMATCH",
                                f"Log {log_key} size {log_member.size} != check descriptor {c.get('log_bytes')}.")
            if log_member.size > max_log_bytes:
                raise GateError(413, "LOG_TOO_LARGE", f"Log {log_key} size exceeds log_bytes bound {max_log_bytes}.")
            lf = tar.extractfile(log_member)
            if lf is None:
                raise GateError(400, "INVALID_LOG", f"Could not read log file {log_key}.")
            log_data = lf.read()
            log_sha = hashlib.sha256(log_data).hexdigest()
            if log_sha != c.get("log_sha256"):
                raise GateError(400, "LOG_HASH_MISMATCH",
                                f"Log {log_key} sha256 {log_sha} != check descriptor {c.get('log_sha256')}.")
            try:
                log_data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GateError(400, "INVALID_LOG_ENCODING", f"Log {log_key} is not strict UTF-8: {exc}") from exc

        # 5. Process release confirmation
        proc_release = manifest.get("process_release", {})
        if not proc_release.get("confirmed") or proc_release.get("owned_processes_remaining") != 0:
            raise GateError(409, "PROCESS_RELEASE_UNCONFIRMED", "Process release must be confirmed with 0 remaining processes.")

        # 6. Exit / Outcome semantics
        outcome = manifest.get("outcome")
        if outcome == "passed":
            if manifest.get("exit_code") != 0:
                raise GateError(409, "PASSED_OUTCOME_NONZERO_EXIT", "Passed manifest must have exit_code 0.")
            if manifest.get("termination_signal") is not None:
                raise GateError(409, "PASSED_OUTCOME_TERMINATION_SIGNAL", "Passed manifest must have null termination_signal.")
            if manifest.get("reason") is not None:
                raise GateError(409, "PASSED_OUTCOME_HAS_REASON", "Passed manifest must not carry a failure reason.")
            if manifest.get("before") is None:
                raise GateError(409, "MISSING_BASELINE", "Passed manifest must carry precleanup before baseline diskSample.")
            if manifest.get("peak_workspace_bytes") is None or manifest.get("peak_workspace_bytes", -1) < 0:
                raise GateError(409, "MISSING_PEAK_WORKSPACE", "Passed manifest must carry non-negative peak_workspace_bytes.")

            req_check_ids = request.get("gate", {}).get("check_ids", [])
            if sorted(check_ids) != sorted(req_check_ids):
                raise GateError(409, "MISSING_REQUIRED_CHECKS",
                                f"Passed manifest checks {sorted(check_ids)} != gate required checks {sorted(req_check_ids)}.")
            for c in checks:
                if c.get("outcome") != "passed":
                    raise GateError(409, "CHECK_OUTCOME_MISMATCH", f"Check {c['id']} outcome is {c.get('outcome')!r}, not 'passed'.")
                if c.get("exit_code") != 0:
                    raise GateError(409, "CHECK_EXIT_MISMATCH", f"Check {c['id']} exit_code is {c.get('exit_code')!r}, not 0.")
                if c.get("duration_seconds") is None or c.get("duration_seconds", -1) < 0:
                    raise GateError(409, "CHECK_DURATION_INVALID", f"Check {c['id']} must have non-negative duration_seconds.")
                if c.get("reason") is not None:
                    raise GateError(409, "CHECK_REASON_PRESENT", f"Passed check {c['id']} must not carry a failure reason.")

            # Actual toolchains cover all gate required toolchains
            actual_tools = {tf["id"]: tf for tf in manifest.get("actual_toolchains", [])}
            for req_tool in request.get("gate", {}).get("required_toolchains", []):
                tid = req_tool["id"]
                tf = actual_tools.get(tid)
                if tf is None or not tf.get("available") or tf.get("installed_bytes") is None:
                    raise GateError(409, "TOOLCHAIN_UNAVAILABLE", f"Required toolchain {tid!r} is not proven available.")
                if tf.get("pin") != req_tool:
                    raise GateError(409, "TOOLCHAIN_PIN_MISMATCH", f"Toolchain {tid!r} pin does not match required pin.")
        else:
            if manifest.get("exit_code") == 0:
                raise GateError(409, "NON_PASSED_ZERO_EXIT", f"Outcome {outcome!r} cannot have exit_code 0.")
            if outcome in ("interrupted", "cancelled", "not_run") and manifest.get("reason") is None:
                raise GateError(409, "MISSING_FAILURE_REASON", f"Outcome {outcome!r} must carry an explanatory reason.")

    return manifest


def validate_status(
    status: dict | bytes,
    request: dict | None = None,
    capabilities: dict | None = None,
    acceptance: dict | None = None,
) -> dict:
    """Validate status document, state, resource release, and ACK tombstone consistency."""
    if isinstance(status, (bytes, bytearray)):
        st = decode(bytes(status), "status")
    else:
        st = validate(status, "status")

    if acceptance is not None:
        if st.get("acceptance") != acceptance:
            raise GateError(409, "ACCEPTANCE_MISMATCH", "Status acceptance does not match accepted attempt.")

    if request is not None:
        req_sha = hashlib.sha256(encoded(request)).hexdigest()
        if st.get("acceptance", {}).get("request_sha256") != req_sha:
            raise GateError(409, "REQUEST_HASH_MISMATCH", "Status request_sha256 does not match canonical request hash.")

    if capabilities is not None:
        cap_ctrl = capabilities.get("controller_id")
        cap_node = capabilities.get("node_id")
        st_acc = st.get("acceptance", {})
        if st_acc.get("controller_id") != cap_ctrl or st_acc.get("node_id") != cap_node:
            raise GateError(409, "IDENTITY_MISMATCH", "Status acceptance node/controller does not match capabilities.")
        st_inv = st.get("inventory", {})
        if st_inv.get("controller_id") != cap_ctrl or st_inv.get("node_id") != cap_node:
            raise GateError(409, "IDENTITY_MISMATCH", "Status inventory node/controller does not match capabilities.")

    state = st.get("state")
    terminal_states = {"passed", "failed", "cancelled", "interrupted", "not_run"}
    if state in terminal_states:
        if not st.get("resource_release_confirmed"):
            raise GateError(409, "RELEASE_UNCONFIRMED", f"Terminal state {state!r} requires resource_release_confirmed=True.")
        if st.get("result") is None:
            raise GateError(409, "MISSING_RESULT_DESCRIPTOR", f"Terminal status in state {state!r} must carry a result descriptor.")
        if state == "passed":
            if st.get("reason") is not None:
                raise GateError(409, "PASSED_HAS_REASON", "Passed status must not carry a failure reason.")
        else:
            if st.get("reason") is None:
                raise GateError(409, "TERMINAL_MISSING_REASON", f"Non-passed terminal status in state {state!r} must carry a reason.")
    else:
        if st.get("resource_release_confirmed"):
            raise GateError(409, "INVALID_RELEASE_STATE", f"Non-terminal state {state!r} cannot assert resource_release_confirmed=True.")

    # ACK durable tombstone consistency (persisted before workspace removal)
    if not st.get("result_acknowledged"):
        if st.get("acknowledged_result") is not None:
            raise GateError(409, "PREACK_HAS_RESULT", "Pre-ACK status must have acknowledged_result=null.")
        if st.get("acknowledged_at") is not None:
            raise GateError(409, "PREACK_HAS_TIMESTAMP", "Pre-ACK status must have acknowledged_at=null.")
    else:
        if st.get("acknowledged_result") is None:
            raise GateError(409, "ACK_MISSING_RESULT", "Post-ACK status must retain acknowledged_result descriptor.")
        if st.get("result") is not None and st.get("acknowledged_result") != st.get("result"):
            raise GateError(409, "ACK_RESULT_MISMATCH", "acknowledged_result must match original result descriptor.")
        if st.get("acknowledged_at") is None:
            raise GateError(409, "ACK_MISSING_TIMESTAMP", "Post-ACK status must carry acknowledged_at timestamp.")

    # Workspace removal consistency
    if st.get("workspace_removed"):
        cleanup = st.get("cleanup_after")
        if cleanup is None:
            raise GateError(409, "MISSING_CLEANUP_AFTER", "Status with workspace_removed=True must carry cleanup_after diskSample.")
        if cleanup.get("workspace_bytes") != 0:
            raise GateError(409, "NONZERO_REMOVED_WORKSPACE", "cleanup_after.workspace_bytes must be 0 when workspace_removed=True.")
    else:
        if st.get("cleanup_after") is not None:
            raise GateError(409, "PREMATURE_CLEANUP_AFTER", "cleanup_after must be null while workspace is not removed.")

    return st
