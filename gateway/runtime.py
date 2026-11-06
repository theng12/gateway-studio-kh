"""Shared runtime singletons.

`store`, `worker` and the catalogue are process-wide by design: one gateway
serves one owner, so a single SQLite connection and one job store are correct.
Keeping them here (rather than constructing them in `api.py`) lets the admin
router and the API share them without an import cycle.

**`worker` is still one object, but it is no longer one thread.** It dispatches to
bounded concurrency — see `gateway/concurrency.py` for the three ceilings, and
`worker.Worker` for why a `ThreadPoolExecutor` was deliberately not used. The
connection stays shared: `sqlite3.threadsafety` is 3 (serialized) on this
interpreter, and every read-modify-write already holds the store's lock.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time

from .catalog import catalog
from . import fleet as fleet_module
from .config import BUNDLED_DATA_DIR, DATA_DIR, DB_PATH, gateway_api_key
from .os_metadata import is_os_metadata
from .providers import registry
from .render_assets import RenderAssetStore
from .render_contract import LATE_LABEL_CONTRACT, SUBTITLE_CONTRACT
from .render_hash_v1 import decode_render_json
from .render_service import (FinalRenderService, RegisteredHubRenderClient, RenderDestination,
                             RenderServiceError, destination_for_candidate, probed_render_candidates)
from .store import JobStore
from .worker import Worker

#: When this process came up. The dashboard reports it as uptime, which is the
#: cheapest honest signal that the service is actually being supervised.
STARTED_AT = time.time()

store = JobStore(DB_PATH)
worker = Worker(store)


def _digest(value: object) -> bool:
    return (type(value) is str and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _positive_integer(value: object) -> bool:
    return type(value) is int and 1 <= value <= 9_007_199_254_740_991


def _valid_bounds(value: object) -> bool:
    keys = {
        "canvas_pairs", "working_fps", "max_duration_sec", "max_operations",
        "max_assets", "max_asset_bytes", "max_total_input_bytes", "max_pixel_frames",
        "scratch_reserve_bytes", "output_limit_bytes", "process_tree_rss_limit_bytes",
        "cumulative_wall_seconds",
    }
    if type(value) is not dict:
        return False
    if "bounds_version" in value:
        if (set(value) != keys - {"canvas_pairs"} | {"bounds_version", "canvas_range"}
                or type(value["bounds_version"]) is not int or value["bounds_version"] != 2):
            return False
        canvas = value["canvas_range"]
        if (type(canvas) is not dict or set(canvas) != {"max_edge", "max_pixels"}
                or not all(_positive_integer(item) for item in canvas.values())
                or canvas["max_edge"] % 2 or canvas["max_edge"] < 2
                or canvas["max_pixels"] < 4):
            return False
    else:
        if set(value) != keys:
            return False
        pairs = value["canvas_pairs"]
        if (type(pairs) is not list or not pairs
                or not all(type(pair) is dict and set(pair) == {"width", "height"}
                           and _positive_integer(pair["width"])
                           and _positive_integer(pair["height"]) for pair in pairs)):
            return False
    fps = value["working_fps"]
    duration = value["max_duration_sec"]
    integers = keys - {"canvas_pairs", "working_fps", "max_duration_sec"}
    return (
        type(fps) is list and bool(fps) and all(_positive_integer(item) for item in fps)
        and type(duration) in (int, float) and math.isfinite(duration) and duration > 0
        and all(_positive_integer(value[key]) for key in integers)
    )


_AUTHORITY_DESCRIPTOR_NAMES = frozenset(("profile.json", "tools.json", "compiler.json"))
#: contract -> (bundle directory, exact profile literals, exact compiler implementation,
#: must be production-ineligible). Missing optional bundles simply publish no authority.
_RENDER_DESCRIPTOR_BUNDLES = {
    "studio.render.profile.v1": (
        "final-render",
        {
            "format": "studio.render.profile.v1",
            "implementation": "story-classic-no-font-v1",
            "recipe_namespace": "studio.render.final.v1",
            "combination_set": "retained-classic-no-font-candidate-v1",
            "tier_policy": "story-final-chain-v1",
            "output_policy": "story-final-output-v1",
        },
        None,
        False,
    ),
    LATE_LABEL_CONTRACT: (
        "final-render-late-label",
        {
            "format": "studio.render.profile.v3",
            "implementation": "story-classic-late-label-v3",
            "recipe_namespace": "studio.render.final.v1",
            "combination_set": "retained-classic-late-label-candidate-v3",
            "tier_policy": "story-final-chain-v1",
            "output_policy": "story-final-output-v1",
        },
        "hub-final-render-late-label-v3",
        True,
    ),
    SUBTITLE_CONTRACT: (
        "final-render-subtitle",
        {
            "format": "studio.render.profile.v4",
            "implementation": "story-classic-subtitle-v1",
            "recipe_namespace": "studio.render.final.v1",
            "combination_set": "retained-classic-subtitle-candidate-v1",
            "tier_policy": "story-final-chain-v1",
            "output_policy": "story-final-output-v1",
        },
        "hub-final-render-subtitle-v1",
        True,
    ),
}

def _bundled_render_descriptors(
    requested_contract: str | None = None,
) -> tuple[bytes | None, dict]:
    if requested_contract is None:
        requested_contract = "studio.render.profile.v1"
    bundle = _RENDER_DESCRIPTOR_BUNDLES.get(requested_contract)
    if bundle is None:
        return None, {}
    directory, profile_literals, compiler_implementation, must_be_ineligible = bundle
    root = BUNDLED_DATA_DIR / directory
    values: dict[str, bytes] = {}
    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def read(path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not 2 <= before.st_size <= 1024 * 1024:
                raise OSError("invalid bundled render descriptor")
            raw = bytearray()
            while len(raw) <= 1024 * 1024:
                piece = os.read(descriptor, min(4096, 1024 * 1024 + 1 - len(raw)))
                if not piece:
                    break
                raw.extend(piece)
            after = os.fstat(descriptor)
            visible = os.stat(path, follow_symlinks=False)
            if len(raw) != before.st_size or identity(before) != identity(after) \
                    or identity(after) != identity(visible):
                raise OSError("unstable bundled render descriptor")
            return bytes(raw)
        finally:
            os.close(descriptor)
    try:
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            return None, {}
        if frozenset(name for name in os.listdir(root) if not is_os_metadata(name)) != _AUTHORITY_DESCRIPTOR_NAMES:
            return None, {}
        for name in ("profile", "tools", "compiler"):
            path = root / (name + ".json")
            descriptor = read(path)
            if type(decode_render_json(descriptor)) is not dict:
                return None, {}
            values[name] = descriptor
        profile = decode_render_json(values["profile"])
        tools = decode_render_json(values["tools"])
        compiler = decode_render_json(values["compiler"])
        if (set(profile) != {
                "format", "implementation", "recipe_namespace", "compiler_artifact_sha256",
                "tools_artifact_sha256", "combination_set", "tier_policy", "output_policy",
                "execution_bounds", "hardware_qualification_record_sha256",
                "fidelity_record_sha256", "production_eligible"}
                or any(profile.get(key) != value for key, value in profile_literals.items())
                or type(profile.get("production_eligible")) is not bool
                or must_be_ineligible and profile["production_eligible"] is not False
                or not _valid_bounds(profile.get("execution_bounds"))
                or any(value is not None and not _digest(value) for value in (
                    profile.get("hardware_qualification_record_sha256"),
                    profile.get("fidelity_record_sha256"),
                ))
                or set(tools) != {
                    "format", "platform", "ffmpeg_sha256", "ffmpeg_size_bytes",
                    "ffprobe_sha256", "ffprobe_size_bytes", "architecture_evidence_sha256",
                    "minimum_os_evidence_sha256", "linked_libraries_evidence_sha256",
                    "build_configuration_evidence_sha256",
                    "capability_inventory_evidence_sha256"}
                or tools.get("format") != "studio.render.tools.v1"
                or type(tools.get("platform")) is not str or not tools["platform"]
                or any(not _digest(tools.get(key)) for key in (
                    "ffmpeg_sha256", "ffprobe_sha256", "architecture_evidence_sha256",
                    "minimum_os_evidence_sha256", "linked_libraries_evidence_sha256",
                    "build_configuration_evidence_sha256",
                    "capability_inventory_evidence_sha256",
                ))
                or not _positive_integer(tools.get("ffmpeg_size_bytes"))
                or not _positive_integer(tools.get("ffprobe_size_bytes"))
                or set(compiler) != {
                    "format", "implementation", "python_runtime_sha256",
                    "render_hash_sha256", "render_contract_sha256",
                    "final_render_compiler_sha256"}
                or compiler.get("format") != "studio.render.compiler.v1"):
            return None, {}
        if (compiler_implementation is not None
                and compiler.get("implementation") != compiler_implementation
                or compiler_implementation is None and (
                    type(compiler.get("implementation")) is not str
                    or not compiler["implementation"])):
            return None, {}
        if (any(not _digest(compiler.get(key)) for key in (
                    "python_runtime_sha256", "render_hash_sha256",
                    "render_contract_sha256", "final_render_compiler_sha256",
                ))):
            return None, {}
        if (profile.get("tools_artifact_sha256") != hashlib.sha256(values["tools"]).hexdigest()
                or profile.get("compiler_artifact_sha256")
                != hashlib.sha256(values["compiler"]).hexdigest()):
            return None, {}
        facts = {
            "tools": {"descriptor_utf8": values["tools"].decode("utf-8"),
                      "digest": hashlib.sha256(values["tools"]).hexdigest()},
            "compiler": {"descriptor_utf8": values["compiler"].decode("utf-8"),
                         "digest": hashlib.sha256(values["compiler"]).hexdigest()},
        }
        return values["profile"], facts
    except (OSError, UnicodeError, ValueError, TypeError):
        return None, {}


_render_profile, _render_profile_facts = _bundled_render_descriptors()
_late_label_profile, _late_label_profile_facts = _bundled_render_descriptors(
    "studio.render.late-label.v3"
)
_subtitle_profile, _subtitle_profile_facts = _bundled_render_descriptors(SUBTITLE_CONTRACT)
_render_profile_value = decode_render_json(_render_profile) if _render_profile else {}
_late_label_profile_value = decode_render_json(_late_label_profile) if _late_label_profile else {}
_subtitle_profile_value = decode_render_json(_subtitle_profile) if _subtitle_profile else {}
_render_bounds = [
    profile.get("execution_bounds") or {}
    for profile in (_render_profile_value, _late_label_profile_value, _subtitle_profile_value)
]
render_assets = RenderAssetStore(
    DATA_DIR / "render-assets",
    max_chunk_bytes=8 * 1024 * 1024,
    max_asset_bytes=max((int(bounds.get("max_asset_bytes") or 1) for bounds in _render_bounds)),
    reserve_bytes=max((int(bounds.get("scratch_reserve_bytes") or 0) for bounds in _render_bounds)),
    measure_free_bytes=lambda: os.statvfs(DATA_DIR).f_bavail * os.statvfs(DATA_DIR).f_frsize,
)


def _render_destination(packet: dict) -> RenderDestination:
    """Production capacity first, then Macs the owner accepted unqualified (same network first - the
    candidate order already says so); only without either does a qualification candidate take it."""
    digest = packet["recipe"]["profile_digest"]
    # The packet's own selected profile picks the contract; legacy packets keep the exact
    # unscoped call shape they have always had.
    contract = (SUBTITLE_CONTRACT
                if render_service.subtitle_profile_digest is not None
                and digest == render_service.subtitle_profile_digest else
                LATE_LABEL_CONTRACT if render_service.late_label_profile_digest is not None
                and digest == render_service.late_label_profile_digest else None)
    scoped = {} if contract is None else {"requested_contract": contract}
    def compatible(rows: list[dict]) -> list[dict]:
        if contract == SUBTITLE_CONTRACT:
            combined = any(op["kind"] == "late_label_stage"
                           for op in packet["recipe"]["operations"])
            rows = [row for row in rows if fleet_module.render_subtitle_support(
                row["capability"], late_labels=combined)]
        return probed_render_candidates(rows, packet["recipe"]["encode"],
                                        render_service.hub, available=True, **scoped)

    try:
        candidates = (compatible(fleet_module.render_candidates(
                          digest, resolve_encode_authority=True, **scoped))
                      or compatible(fleet_module.render_candidates(
                          digest, qualification=True, resolve_encode_authority=True, **scoped)))
    except fleet_module.ScopedRenderCapabilitiesError as exc:
        # A scoped read that could not be trusted is a loud refusal, never an empty route.
        raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503) from exc
    for row in candidates:
        try:
            return destination_for_candidate(row)
        except RenderServiceError:
            continue
    raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)


render_service = FinalRenderService(
    store, render_assets, destination_selector=_render_destination,
    hub_client=RegisteredHubRenderClient(), key_configured=lambda: bool(gateway_api_key()),
    profile_descriptor=_render_profile, profile_facts=_render_profile_facts,
    late_label_profile_descriptor=_late_label_profile,
    late_label_profile_facts=_late_label_profile_facts,
    subtitle_profile_descriptor=_subtitle_profile,
    subtitle_profile_facts=_subtitle_profile_facts,
)
worker.attach_render_service(render_service)

# The catalogue publishes a `measured` block per row, read from settled jobs, so it
# needs the store — and it cannot import it without a cycle (this module imports the
# catalogue). Attached explicitly, after both exist.
catalog.attach_store(store)

__all__ = ["STARTED_AT", "catalog", "registry", "render_assets", "render_service", "store", "worker"]
