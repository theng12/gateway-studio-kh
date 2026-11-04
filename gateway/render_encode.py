"""Validate exact Hub probe evidence separately from permission to execute it."""
from __future__ import annotations

import hashlib
import json
import re

from .render_contract import validate_render_encode
from .render_hash_v1 import decode_render_json


def requested_encode_query(values):
    """Parse one optional query value using the packet's closed encode contract."""
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > 4096:
        raise ValueError("invalid requested_encode query")
    return validate_render_encode(decode_render_json(values[0].encode("utf-8")))

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_STATE_KEYS = frozenset(("format", "requested_encode", "state", "probe_key", "reason",
                         "evidence", "authority_digest", "execution_authority"))
_EVIDENCE_KEYS = frozenset(("format", "requested_encode", "node_identity", "hardware_identity",
    "profile_digest", "tools_digest", "compiler_digest", "selected_tier", "exact_encoder",
    "probe_vector_digest", "probe_result_digest", "purpose"))
_EXECUTION_KEYS = frozenset(("format", "requested_encode", "encode_authority", "authority_kind",
    "qualification_digest", "profile_digest", "tools_digest", "compiler_digest", "hardware_class",
    "selected_tier", "exact_encoder", "font_support", "bounds", "profile_execution_bounds", "grant"))
_BINDING_KEYS = frozenset(("format", "authority_digest", "selected_tier", "exact_encoder"))
_DESCRIPTOR_KEYS = ("profile_digest", "tools_digest", "compiler_digest")
_BOUND_KEYS = frozenset(("max_rss_bytes", "max_wall_seconds", "min_available_memory_bytes",
                         "output_limit_bytes"))


def _exact(value, keys):
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid encode authority shape")
    return value


def _digest(value):
    if type(value) is not str or _HEX.fullmatch(value) is None:
        raise ValueError("invalid encode authority digest")
    return value


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 256:
        raise ValueError("invalid encode authority text")
    return value


def evidence_digest(evidence):
    """Evidence uses UTF-8 canonical JSON, independently of packet identity bytes."""
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def execution_authority(answer, requested, *, physical_node_id, capability, chip):
    """Return current exact execution permission, or None for valid non-executable states.

    The authenticated Hub recomputes grant validity on GET. The caller must also retain
    Gateway owner switches, readiness/resource checks and the immutable registered route.
    """
    validate_render_encode(requested)
    _exact(answer, _STATE_KEYS)
    if (answer["format"] != "studio.render.encode-probe-state.v1"
            or validate_render_encode(answer["requested_encode"]) != requested
            or type(answer["state"]) is not str
            or answer["state"] not in {"supported", "unsupported", "pending", "unmeasured"}):
        raise ValueError("invalid encode probe state")
    if answer["probe_key"] is not None:
        _digest(answer["probe_key"])
    if answer["reason"] is not None:
        _text(answer["reason"])
    if answer["state"] != "supported":
        if any(answer[key] is not None for key in ("evidence", "authority_digest", "execution_authority")):
            raise ValueError("unsupported probe claims authority")
        return None
    evidence = _exact(answer["evidence"], _EVIDENCE_KEYS)
    if (evidence["format"] != "studio.render." + "encode-probe-evidence.v1"
            or evidence["purpose"] != "encode_support_only"
            or validate_render_encode(evidence["requested_encode"]) != requested):
        raise ValueError("invalid exact encode evidence")
    if _exact(evidence["node_identity"], {"physical_node_id"})["physical_node_id"] != physical_node_id:
        raise ValueError("encode evidence names another node")
    hardware = _exact(evidence["hardware_identity"], {"hardware_class", "chip", "memory_bytes", "backend"})
    if (hardware["hardware_class"] != capability["hardware_class"] or hardware["chip"] != chip
            or type(hardware["memory_bytes"]) is not int
            or not 0 < hardware["memory_bytes"] <= 2**53 - 1
            or hardware["backend"] != evidence["exact_encoder"]):
        raise ValueError("encode evidence hardware differs")
    for key in (*_DESCRIPTOR_KEYS, "probe_vector_digest", "probe_result_digest"):
        _digest(evidence[key])
    if any(evidence[key] != capability[key] for key in _DESCRIPTOR_KEYS):
        raise ValueError("encode evidence descriptors differ")
    _text(evidence["exact_encoder"])
    if evidence["selected_tier"] is not None:
        _text(evidence["selected_tier"])
    if evidence_digest(evidence) != _digest(answer["authority_digest"]):
        raise ValueError("encode evidence digest differs")
    execution = answer["execution_authority"]
    if execution is None:
        return None
    _exact(execution, _EXECUTION_KEYS)
    binding = _exact(execution["encode_authority"], _BINDING_KEYS)
    expected_binding = {"format": "studio.render.encode-binding.v1",
                        "authority_digest": answer["authority_digest"],
                        "selected_tier": evidence["selected_tier"], "exact_encoder": evidence["exact_encoder"]}
    if (execution["format"] != "studio.render.encode-execution-authority.v1"
            or validate_render_encode(execution["requested_encode"]) != requested
            or binding != expected_binding
            or any(execution[key] != evidence[key] for key in (*_DESCRIPTOR_KEYS, "selected_tier", "exact_encoder"))
            or execution["hardware_class"] != hardware["hardware_class"]
            or type(execution["font_support"]) is not bool
            or execution["profile_execution_bounds"] != capability["profile_execution_bounds"]):
        raise ValueError("execution permission differs from evidence")
    _digest(execution["qualification_digest"])
    if execution["qualification_digest"] == answer["authority_digest"]:
        raise ValueError("probe evidence is not qualification")
    bounds = _exact(execution["bounds"], _BOUND_KEYS)
    if any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in bounds.values()):
        raise ValueError("invalid execution bounds")
    if execution["authority_kind"] == "measured":
        if execution["grant"] is not None:
            raise ValueError("measured authority carries an owner grant")
    elif execution["authority_kind"] == "accepted_unqualified":
        grant = _exact(execution["grant"], {"format", "digest", "v1", "migration", "node"})
        if grant["format"] != "studio.render.accept-unqualified-probed-v2.v1":
            raise ValueError("invalid execution grant provenance")
        original = _exact(grant["v1"], {"sha256", "provenance", "accepted_by", "accepted_at"})
        migration = _exact(grant["migration"], {"authorization_id", "authorized_at"})
        node = _exact(grant["node"], {"physical_node_id"})
        if (original["provenance"] != "owner-opt-in-unqualified"
                or node["physical_node_id"] != physical_node_id):
            raise ValueError("execution grant names another authority")
        _digest(original["sha256"])
        for value in (original["accepted_by"], migration["authorization_id"]):
            _text(value)
        for value in (original["accepted_at"], migration["authorized_at"]):
            if type(value) is not int or not 0 < value <= 2**53 - 1:
                raise ValueError("invalid execution grant timestamp")
        # The full grant's entries stay on Hub; preserve its digest, never reconstruct it.
        _digest(grant["digest"])
    else:
        raise ValueError("unknown execution authority kind")
    # Phase 1's compiler/transport executes MP4/H.264 only; HEVC probes are capability-only.
    if (requested["container"] != "mp4" or requested["requested_codec"] != "h264"
            or evidence["selected_tier"] is None
            or evidence["exact_encoder"] not in {"libx264", "h264_videotoolbox"}):
        raise ValueError("probe does not authorize a Phase 1 render")
    return execution
