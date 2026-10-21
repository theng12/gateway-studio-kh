#!/usr/bin/env python3
"""Focused offline contract for the private final-render coordinator.

The retained fixtures contain only deterministic fake identities and tiny byte strings.  This
suite never opens a socket, runs a renderer, reads configured credentials, or calls a provider.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN_ROOT = ROOT / "_trash" / "tests" / f"gateway-render-service-{uuid.uuid4().hex}"
RUN_ROOT.mkdir(parents=True, mode=0o700)
os.environ.update({
    "GATEWAY_APP_SUPPORT": str(RUN_ROOT),
    "GATEWAY_DATA": str(RUN_ROOT / "data"),
    "GATEWAY_ARTIFACTS": str(RUN_ROOT / "artifacts"),
    "GATEWAY_DB": str(RUN_ROOT / "gateway.sqlite3"),
    "GATEWAY_CREDENTIALS": str(RUN_ROOT / "credentials.json"),
    "GATEWAY_MODEL_CACHE": str(RUN_ROOT / "models.json"),
    "GATEWAY_ENV_FILE": str(RUN_ROOT / ".env"),
    "GATEWAY_FLEET": str(RUN_ROOT / "fleet.json"),
    "GATEWAY_POOLS": str(RUN_ROOT / "pools.json"),
    "GATEWAY_VOICES": str(RUN_ROOT / "voices.json"),
    "GATEWAY_VOICES_OUT": str(RUN_ROOT / "voices.json"),
    "GATEWAY_VOICE_LIBRARY": str(RUN_ROOT / "voice-library"),
    "GATEWAY_BACKUP_HUB_DIR": str(RUN_ROOT / "backup-hub"),
    "STUDIOHUB_READY_FILE": str(RUN_ROOT / "no-hub.json"),
    "GATEWAY_NO_MIGRATE": "1",
})

from gateway import fleet  # noqa: E402
from gateway import runtime  # noqa: E402
from gateway.render_assets import RenderAssetStore  # noqa: E402
from gateway.render_hash_v1 import render_hash  # noqa: E402
from gateway.render_service import (  # noqa: E402
    AuthenticatedPrivateRender,
    FinalRenderService,
    RegisteredHubRenderClient,
    RenderDestination,
    RenderServiceError,
)
from gateway.store import JobStore, RenderJournalConflict  # noqa: E402


OWNER = "11111111-1111-4111-8111-111111111111"
GATEWAY = "22222222-2222-4222-8222-222222222222"
STORY_INSTANCE = "33333333-3333-4333-8333-333333333333"
REINSTALLED_GATEWAY = "44444444-4444-4444-8444-444444444444"
INTENT_ID = "66666666-6666-4666-8666-0000000000aa"
STORY_UUID = "55555555-5555-4555-8555-555555555555"


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


PROFILE_RAW = canonical({
    "format": "studio.render.profile.v1",
    "implementation": "story-classic-no-font-v1",
    "recipe_namespace": "studio.render.final.v1",
    "compiler_artifact_sha256": "c" * 64,
    "tools_artifact_sha256": "d" * 64,
    "combination_set": "retained-classic-no-font-candidate-v1",
    "tier_policy": "story-final-chain-v1",
    "output_policy": "story-final-output-v1",
    "execution_bounds": {
        "canvas_pairs": [{"width": 1920, "height": 1080}],
        "working_fps": [24],
        "max_duration_sec": 60,
        "max_operations": 32,
        "max_assets": 32,
        "max_asset_bytes": 32 * 1024 * 1024,
        "max_total_input_bytes": 64 * 1024 * 1024,
        "max_pixel_frames": 1920 * 1080 * 24 * 60,
        "scratch_reserve_bytes": 1,
        "output_limit_bytes": 32 * 1024 * 1024,
        "process_tree_rss_limit_bytes": 1,
        "cumulative_wall_seconds": 60,
    },
    "hardware_qualification_record_sha256": None,
    "fidelity_record_sha256": None,
    "production_eligible": True,
})
CAPABILITY_SNAPSHOT = {
    "profile_digest": hashlib.sha256(PROFILE_RAW).hexdigest(),
    "tools_digest": "d" * 64,
    "compiler_digest": "c" * 64,
    "qualification_digest": "4" * 64,
    "selected_tier": "software-h264",
    "production_eligible": True,
    "production_enabled": True,
    "bounds": {"output_limit_bytes": 32 * 1024 * 1024},
    "profile_execution_bounds": json.loads(PROFILE_RAW)["execution_bounds"],
}


def principal(*, owner_scope: str = OWNER) -> AuthenticatedPrivateRender:
    return AuthenticatedPrivateRender(
        owner_scope=owner_scope,
        gateway_instance_id=GATEWAY,
        story_instance_id=STORY_INSTANCE,
    )


def install(store: JobStore, *, owner_scope: str = OWNER,
            gateway_instance_id: str = GATEWAY) -> None:
    """Pin the store's once-minted installation row to the fixture identity."""
    with store._render_write():
        store._conn.execute(
            "UPDATE render_installation SET owner_scope = ?, gateway_instance_id = ?",
            (owner_scope, gateway_instance_id),
        )


def packet(entries: list[dict[str, object]], *, profile_digest: str | None = None) -> dict[str, object]:
    profile_digest = profile_digest or hashlib.sha256(PROFILE_RAW).hexdigest()
    recipe = {
        "namespace": "studio.render.final.v1",
        "profile_digest": profile_digest,
        "base_video": {"type": "asset_ref", "logical_id": "base"},
        "premixed_audio": {"type": "asset_ref", "logical_id": "audio"},
        "canvas": {"width": 1920, "height": 1080, "working_fps": 24},
        "target_duration_sec": 3,
        "effects_gate": {"start_sec": None, "end_sec": None},
        "operations": [],
        "encode": {
            "container": "mp4",
            "requested_codec": "h264",
            "requested_effort": "medium",
            "crf": 23,
            "quality_cpu_preset": "medium",
            "bitrate_kbps": None,
        },
    }
    recipe_digest = render_hash("recipe", recipe)
    manifest_digest = render_hash("manifest", entries)
    return {
        "recipe": recipe,
        "manifest": entries,
        "recipe_digest": recipe_digest,
        "manifest_digest": manifest_digest,
        "packet_digest": render_hash(
            "packet", {"recipe_digest": recipe_digest, "manifest_digest": manifest_digest}
        ),
    }


IMMUTABLE_CANDIDATE_KEYS = {
    "vector_id", "hardware_class", "profile_digest", "tools_digest", "compiler_digest",
    "qualification_digest", "selected_tier", "exact_encoder", "font_support",
    "bounds", "profile_execution_bounds", "production_eligible", "qualification_only",
}


def candidate_authority() -> dict[str, object]:
    tools_raw = canonical({
        "format": "studio.render.tools.v1", "platform": "macos-arm64",
        "ffmpeg_sha256": "1" * 64, "ffmpeg_size_bytes": 10,
        "ffprobe_sha256": "2" * 64, "ffprobe_size_bytes": 11,
        "architecture_evidence_sha256": "3" * 64,
        "minimum_os_evidence_sha256": "4" * 64,
        "linked_libraries_evidence_sha256": "5" * 64,
        "build_configuration_evidence_sha256": "6" * 64,
        "capability_inventory_evidence_sha256": "7" * 64,
    })
    compiler_raw = canonical({
        "format": "studio.render.compiler.v1", "implementation": "hub-final-render-v1",
        "python_runtime_sha256": "8" * 64, "render_hash_sha256": "9" * 64,
        "render_contract_sha256": "a" * 64,
        "final_render_compiler_sha256": "b" * 64,
    })
    execution_bounds = {
        "canvas_pairs": [{"width": 1920, "height": 1080}], "working_fps": [24],
        "max_duration_sec": 60, "max_operations": 32, "max_assets": 32,
        "max_asset_bytes": 32 * 1024 * 1024,
        "max_total_input_bytes": 64 * 1024 * 1024,
        "max_pixel_frames": 1920 * 1080 * 24 * 60,
        "scratch_reserve_bytes": 1, "output_limit_bytes": 32 * 1024 * 1024,
        "process_tree_rss_limit_bytes": 1, "cumulative_wall_seconds": 60,
    }
    profile_raw = canonical({
        "format": "studio.render.profile.v1",
        "implementation": "story-classic-no-font-v1",
        "recipe_namespace": "studio.render.final.v1",
        "compiler_artifact_sha256": hashlib.sha256(compiler_raw).hexdigest(),
        "tools_artifact_sha256": hashlib.sha256(tools_raw).hexdigest(),
        "combination_set": "retained-classic-no-font-candidate-v1",
        "tier_policy": "story-final-chain-v1", "output_policy": "story-final-output-v1",
        "execution_bounds": execution_bounds,
        "hardware_qualification_record_sha256": None,
        "fidelity_record_sha256": None, "production_eligible": False,
    })
    projection = {
        "vector_id": INTENT_ID, "hardware_class": "apple-m4-16gb",
        "profile_digest": hashlib.sha256(profile_raw).hexdigest(),
        "tools_digest": hashlib.sha256(tools_raw).hexdigest(),
        "compiler_digest": hashlib.sha256(compiler_raw).hexdigest(),
        "qualification_digest": "0" * 64, "selected_tier": "hardware-h264",
        "exact_encoder": "h264_videotoolbox", "font_support": False,
        "bounds": {
            "max_rss_bytes": execution_bounds["process_tree_rss_limit_bytes"],
            "max_wall_seconds": execution_bounds["cumulative_wall_seconds"],
            "min_available_memory_bytes": 2_000_000_000,
            "output_limit_bytes": execution_bounds["output_limit_bytes"],
        },
        "profile_execution_bounds": execution_bounds,
        "production_eligible": False, "qualification_only": True,
    }
    return {
        "profile_raw": profile_raw, "tools_raw": tools_raw, "compiler_raw": compiler_raw,
        "projection": projection,
    }


def qualification_capability() -> dict[str, object]:
    return {
        "vector_id": INTENT_ID,
        "hardware_class": "apple-m4-16gb",
        "profile_digest": hashlib.sha256(PROFILE_RAW).hexdigest(),
        "tools_digest": "d" * 64, "compiler_digest": "c" * 64,
        "qualification_digest": "4" * 64, "selected_tier": "software-h264",
        "exact_encoder": "libx264", "font_support": False,
        "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 60,
                   "min_available_memory_bytes": 1,
                   "output_limit_bytes": 32 * 1024 * 1024},
        "profile_execution_bounds": json.loads(PROFILE_RAW)["execution_bounds"],
        "production_eligible": False, "qualification_only": True,
        "available_capacity": {"slots": 1, "slots_total": 1},
    }


class BundledAuthorityTests(unittest.TestCase):
    _serial = 0

    def setUp(self) -> None:
        type(self)._serial += 1
        self.root = RUN_ROOT / f"bundled-authority-{type(self)._serial:03d}"
        self.root.mkdir(mode=0o700)

    def write_authority(self, *, stray_vector: bool = False) -> tuple[Path, dict[str, object]]:
        authority = candidate_authority()
        bundled = self.root / "data/final-render"
        bundled.mkdir(parents=True)
        for name in ("profile", "tools", "compiler"):
            (bundled / f"{name}.json").write_bytes(authority[f"{name}_raw"])
        if stray_vector:
            (bundled / "qualification-vector.json").write_bytes(b"{}")
        return bundled, authority

    def load(self):
        with patch.object(runtime, "BUNDLED_DATA_DIR", self.root / "data"):
            return runtime._bundled_render_descriptors()

    def test_absent_bundle_is_tool_only_and_complete_three_is_descriptor_only(self) -> None:
        self.assertEqual(self.load(), (None, {}))
        _, authority = self.write_authority()

        loaded = self.load()
        self.assertEqual(len(loaded), 2)
        profile, facts = loaded

        self.assertEqual(profile, authority["profile_raw"])
        self.assertEqual(facts, {
            "tools": {"descriptor_utf8": authority["tools_raw"].decode(),
                      "digest": hashlib.sha256(authority["tools_raw"]).hexdigest()},
            "compiler": {"descriptor_utf8": authority["compiler_raw"].decode(),
                         "digest": hashlib.sha256(authority["compiler_raw"]).hexdigest()},
        })

    def test_bundled_qualification_vector_is_no_longer_authority(self) -> None:
        # Qualification is a Hub operator intent now; a sealed vector file is a foreign entry.
        self.write_authority(stray_vector=True)
        self.assertEqual(self.load(), (None, {}))

    def test_partial_or_cross_digest_mismatched_bundle_is_fully_unavailable(self) -> None:
        authority = candidate_authority()
        for index, names in enumerate((("profile",), ("profile", "tools"))):
            with self.subTest(names=names):
                self.root = RUN_ROOT / f"bundled-authority-partial-{index:02d}"
                bundled = self.root / "data/final-render"
                bundled.mkdir(parents=True)
                for name in names:
                    (bundled / f"{name}.json").write_bytes(authority[f"{name}_raw"])
                self.assertEqual(self.load(), (None, {}))

        self.root = RUN_ROOT / "bundled-authority-cross-digest"
        bundled, authority = self.write_authority()
        tools = json.loads(authority["tools_raw"])
        tools["platform"] = "macos-x86_64"
        (bundled / "tools.json").write_bytes(canonical(tools))
        self.assertEqual(self.load(), (None, {}))

    def test_destination_prefers_production_then_any_qualification_candidate(self) -> None:
        authority = candidate_authority()
        projection = authority["projection"]
        digest = projection["profile_digest"]
        render_packet = packet([], profile_digest=digest)

        def row(capability: dict[str, object], vector_id: str | None) -> dict[str, object]:
            return {
                "site_id": "site-a", "roster_controller_id": "roster-a",
                "hub_controller_id": "hub-a", "node_id": "node-m4-a",
                "target_hub_controller_id": "node-m4-a",
                "qualification_vector_id": vector_id, "capability": capability,
                "selected_machine": {
                    "id": "node-m4-a", "label": "M4 fixture", "chip": "Apple M4",
                    "memory_gb": 16, "hardware_class": projection["hardware_class"],
                },
            }

        production = row(CAPABILITY_SNAPSHOT, None)
        qualification = row(
            {**projection, "available_capacity": {"slots": 1, "slots_total": 1}}, INTENT_ID
        )
        with patch.object(runtime.fleet_module, "render_candidates",
                          side_effect=[[production]]) as selected:
            destination = runtime._render_destination(render_packet)
        self.assertEqual(selected.call_args_list, [call(digest)])
        self.assertIsNone(destination.qualification_vector_id)

        with patch.object(runtime.fleet_module, "render_candidates",
                          side_effect=[[], [qualification]]) as selected:
            destination = runtime._render_destination(render_packet)
        self.assertEqual(selected.call_args_list, [
            call(digest), call(digest, qualification=True),
        ])
        self.assertEqual(destination.qualification_vector_id, INTENT_ID)

        with patch.object(runtime.fleet_module, "render_candidates", side_effect=[[], []]):
            with self.assertRaises(RenderServiceError) as caught:
                runtime._render_destination(render_packet)
        self.assertEqual(caught.exception.code, "RENDER_DESTINATION_UNAVAILABLE")


class FakeHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.answers: dict[str, list[object]] = {}
        self.before: dict[str, object] = {}

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.calls.append((name, (args, kwargs)))
            callback = self.before.get(name)
            if callback:
                callback(*args, **kwargs)
            answers = self.answers.get(name, [])
            if name == "blob_status" and not answers:
                return {"accepted": False, "http_status": 404}
            if not answers:
                raise AssertionError(f"unexpected Hub I/O: {name}")
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return call


class RegisteredHubWireTests(unittest.TestCase):
    def test_every_hub_call_uses_the_fixed_relay_route_and_exact_wire_metadata(self) -> None:
        calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

        class Response:
            def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
                self.status_code = status
                self.body = body
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.body

        job_id = "66666666-6666-4666-8666-666666666666"
        digest = "d" * 64
        hub_job = {
            "id": job_id, "owner_scope": "site-a:node-m4-a:fleet",
            "dispatch_key": "gateway:key", "accepted_request_sha256": "a" * 64,
            "purpose": "production", "state": "accepted", "machine": None,
            "dispatch_generation": None, "attempt_id": None,
            "cancel_requested_at": None, "profile_digest": "b" * 64,
            "tools_digest": "c" * 64, "compiler_digest": "d" * 64,
            "qualification_digest": "e" * 64, "stopped_evidence_digest": None,
            "output_receipt": None,
            "leases": {"compute": False, "reference": 0, "reader": 0,
                       "transfer": 0, "ack": 0},
            "replayed": False,
        }
        blob_status = {
            "upload_id": "1" * 32, "sha256": digest, "size_bytes": 9,
            "committed_offset": 3, "state": "incomplete", "asset_handle": None,
        }
        put_result = {"asset_handle": "2" * 32, "sha256": digest, "size_bytes": 9}
        fence = {
            "owner_scope": "site-a:node-m4-a:fleet", "dispatch_key": "gateway:key",
            "expected_request_sha256": "a" * 64, "fence_id": "3" * 32,
            "cancelled_at": 1_700_000_000.0, "state": "cancelled_before_acceptance",
        }
        ack = {"ok": True, "job_id": job_id, "receipt_digest": "4" * 64}
        wire_results = [blob_status, put_result, hub_job, hub_job, hub_job, fence, hub_job, ack]
        responses = [Response(200, canonical(value)) for value in wire_results] + [Response(
            206, b"x", {
                "Content-Length": "1", "Content-Range": "bytes 0-0/1",
                "ETag": '"' + "e" * 64 + '"', "X-Content-SHA256": "e" * 64,
                "Content-Type": "video/mp4",
            },
        )]

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def stream(self, method, url, *, content=None, headers=None):
                calls.append((method, url, content, dict(headers or {})))
                return responses.pop(0)

        destination = RenderDestination(
            "site-a", "roster-controller-a", "parent-controller-a", "node-m4-a"
        )
        client = RegisteredHubRenderClient()
        with patch.object(fleet, "roster", return_value=[{
                    "id": "roster-controller-a", "url": "https://relay.invalid/",
                }]), patch.object(fleet, "_token_for", return_value="fake-hub-token"), \
                patch("gateway.render_service.httpx.Client", return_value=Client()):
            actual_results = [
                client.blob_status(destination, digest, 9),
                client.put_blob(destination, digest, 3, 9, b"abc", "f" * 64),
                client.submit(destination, "gateway:key", b"submit"),
                client.by_key(destination, "gateway:key"),
                client.status(destination, job_id),
                client.cancel_by_key(destination, "gateway:key", b"key-cancel"),
                client.cancel(destination, job_id, b"id-cancel"),
                client.ack(destination, job_id, "gateway:ack", b"ack"),
            ]
            payload, headers = client.output_range(
                destination, job_id, 0, 1, '"' + "e" * 64 + '"'
            )

        prefix = "https://relay.invalid/api/hub/fleet/nodes/node-m4-a/final-renders"
        auth = {"X-Hub-Token": "fake-hub-token"}
        self.assertEqual(calls, [
            ("GET", prefix + "/blobs/" + digest + "/status?size_bytes=9", None, auth),
            ("PUT", prefix + "/blobs/" + digest, b"abc", auth | {
                "Content-Length": "3", "Content-Range": "bytes 3-5/9",
                "X-Content-SHA256": "f" * 64,
            }),
            ("POST", prefix, b"submit", auth | {
                "Content-Type": "application/json", "Content-Length": "6",
                "Idempotency-Key": "gateway:key",
            }),
            ("GET", prefix + "/by-idempotency-key/gateway%3Akey", None, auth),
            ("GET", prefix + "/" + job_id, None, auth),
            ("POST", prefix + "/by-idempotency-key/gateway%3Akey/cancel", b"key-cancel",
             auth | {"Content-Type": "application/json", "Content-Length": "10"}),
            ("POST", prefix + "/" + job_id + "/cancel", b"id-cancel",
             auth | {"Content-Type": "application/json", "Content-Length": "9"}),
            ("POST", prefix + "/" + job_id + "/ack", b"ack", auth | {
                "Content-Type": "application/json", "Content-Length": "3",
                "Idempotency-Key": "gateway:ack",
            }),
            ("GET", prefix + "/" + job_id + "/output", None, auth | {
                "Range": "bytes=0-0", "If-Match": '"' + "e" * 64 + '"',
            }),
        ])
        self.assertEqual((payload, headers["content-range"]), (b"x", "bytes 0-0/1"))
        self.assertEqual(actual_results, wire_results)
        self.assertEqual(responses, [])


class RenderServiceCase(unittest.TestCase):
    _serial = 0

    def setUp(self) -> None:
        type(self)._serial += 1
        self.case_root = RUN_ROOT / f"{type(self)._serial:03d}-{self.id().rsplit('.', 1)[-1]}"
        self.case_root.mkdir(mode=0o700)
        self.store = JobStore(self.case_root / "gateway.sqlite3")
        install(self.store)
        self.assets = RenderAssetStore(
            self.case_root / "assets",
            max_chunk_bytes=8 * 1024 * 1024,
            max_asset_bytes=32 * 1024 * 1024,
            reserve_bytes=0,
            measure_free_bytes=lambda: 1 << 40,
        )
        self.hub = FakeHub()
        self.destination = RenderDestination(
            site_id="site-a",
            roster_controller_id="roster-controller-a",
            hub_controller_id="hub-controller-a",
            node_id="node-m4-a",
            capability_snapshot_sha256=hashlib.sha256(
                canonical(CAPABILITY_SNAPSHOT)
            ).hexdigest(),
            capability_snapshot=CAPABILITY_SNAPSHOT,
            selected_machine={
                "id": "node-m4-a", "label": "M4 fixture", "chip": "Apple M4",
                "memory_gb": 16, "hardware_class": "apple-m4-16gb",
            },
        )
        self.service = FinalRenderService(
            self.store,
            self.assets,
            destination_selector=lambda _packet: self.destination,
            hub_client=self.hub,
            key_configured=lambda: True,
            profile_descriptor=PROFILE_RAW,
            profile_facts={"tools": {"digest": "d" * 64},
                           "compiler": {"digest": "c" * 64}},
        )
        self.addCleanup(self.service.stop)
        self.addCleanup(self.assets.close)
        self.addCleanup(self.store.close)

    def publish(
        self, name: str, payload: bytes, suffix: str, *, owner_scope: str = OWNER
    ) -> tuple[dict[str, object], str]:
        digest = hashlib.sha256(payload).hexdigest()
        upload = self.assets.begin_upload(owner_scope, digest, len(payload))
        if upload["state"] != "published":
            self.assets.append_chunk(
                owner_scope, upload["upload_id"], 0, io.BytesIO(payload), len(payload)
            )
        asset = self.assets.finish_upload(owner_scope, upload["upload_id"])
        return (
            {
                "logical_id": name,
                "safe_name": f"{name}.{suffix}",
                "role": "premixed_audio" if name == "audio" else "base_video",
                "sha256": digest,
                "size_bytes": len(payload),
            },
            asset["asset_handle"],
        )

    def request(self, *, owner_scope: str = OWNER, **changes: object) -> bytes:
        audio, audio_handle = self.publish(
            "audio", b"fake-audio-bytes", "wav", owner_scope=owner_scope
        )
        base, base_handle = self.publish(
            "base", b"fake-video-bytes", "mp4", owner_scope=owner_scope
        )
        value: dict[str, object] = {
            "format": "studio.render.gateway-request.v1",
            "story_uuid": STORY_UUID,
            "intent_id": "episode-final",
            "attempt_no": 1,
            "packet": packet([audio, base]),
            "assets": {"audio": audio_handle, "base": base_handle},
        }
        value.update(changes)
        return canonical(value)

    def shared_digest_request(self, **changes: object) -> bytes:
        payload = b"same-fake-input-bytes"
        audio, handle = self.publish("audio", payload, "wav")
        base = {
            **audio,
            "logical_id": "base",
            "safe_name": "base.mp4",
            "role": "base_video",
        }
        value: dict[str, object] = {
            "format": "studio.render.gateway-request.v1",
            "story_uuid": STORY_UUID,
            "intent_id": "shared-digest-final",
            "attempt_no": 1,
            "packet": packet([audio, base]),
            "assets": {"audio": handle, "base": handle},
        }
        value.update(changes)
        return canonical(value)

    def wait_for_render_steps(self, expected_active: int = 0) -> None:
        deadline = time.monotonic() + 2
        while self.service.active() != expected_active and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(self.service.active(), expected_active)

    def assert_code(self, code: str, call, *args, **kwargs) -> RenderServiceError:
        with self.assertRaises(RenderServiceError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class AcceptanceTests(RenderServiceCase):
    def test_capabilities_count_production_and_qualification_candidates(self) -> None:
        rows = [{"available_slots": 1}, {"available_slots": 0}, {"available_slots": 1}]
        with patch.object(fleet, "render_candidates", return_value=rows) as candidates:
            public = self.service.capabilities(principal())
        candidates.assert_called_once_with(
            hashlib.sha256(PROFILE_RAW).hexdigest(), qualification=True
        )
        self.assertEqual((public["eligible"], public["idle"]), (3, 2))
        self.assertNotIn("qualification_provenance", public)
        self.assertEqual(
            (public["owner_scope"], public["gateway_instance_id"], public["story_instance_id"]),
            (OWNER, GATEWAY, STORY_INSTANCE),
        )

        with patch.object(fleet, "render_candidates", side_effect=RuntimeError("fake")):
            public = self.service.capabilities(principal())
        self.assertEqual((public["eligible"], public["idle"]), (0, 0))

    def test_never_eligible_bundled_profile_still_reports_runtime_capacity(self) -> None:
        # `production_eligible` stays false in profile.json forever; Hub runtime state decides.
        authority = candidate_authority()
        bundled = self.case_root / "bundled/final-render"
        bundled.mkdir(parents=True)
        for name in ("profile", "tools", "compiler"):
            (bundled / f"{name}.json").write_bytes(authority[f"{name}_raw"])
        with patch.object(runtime, "BUNDLED_DATA_DIR", bundled.parent):
            profile, facts = runtime._bundled_render_descriptors()
        self.assertIs(json.loads(profile)["production_eligible"], False)
        service = FinalRenderService(
            self.store, self.assets, destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: True,
            profile_descriptor=profile, profile_facts=facts,
        )
        self.addCleanup(service.stop)
        with patch.object(fleet, "render_candidates",
                          return_value=[{"available_slots": 1}]) as candidates:
            public = service.capabilities(principal())
        candidates.assert_called_once_with(
            hashlib.sha256(profile).hexdigest(), qualification=True
        )
        self.assertEqual((public["eligible"], public["idle"]), (1, 1))

    def test_capabilities_publish_only_the_encode_every_eligible_machine_advertises(self) -> None:
        shared = {"container": "mp4", "requested_codec": "h264",
                  "requested_effort": "medium", "crf": 23,
                  "quality_cpu_preset": "medium", "bitrate_kbps": None}
        extra = {**shared, "requested_effort": "slow"}

        def row(*entries: dict) -> dict:
            return {"available_slots": 1, "capability": {"encode": {
                "format": fleet.RENDER_ENCODE_FORMAT, "entries": list(entries),
            }}}

        with patch.object(fleet, "render_candidates",
                          return_value=[row(shared, extra), row(shared)]):
            public = self.service.capabilities(principal())
        self.assertEqual(public["encode"], {
            "format": fleet.RENDER_ENCODE_FORMAT, "entries": [shared],
        })

        # Every machine agreeing on nothing is still an answer; unknown is `null`.
        with patch.object(fleet, "render_candidates", return_value=[row(shared), row(extra)]):
            self.assertEqual(self.service.capabilities(principal())["encode"], {
                "format": fleet.RENDER_ENCODE_FORMAT, "entries": [],
            })
        for rows in (
            [row(shared), {"available_slots": 1}],
            [row(shared), {"available_slots": 1, "capability": {"encode": None}}],
            [row(shared), {"available_slots": 1, "capability": {"encode": {
                "format": fleet.RENDER_ENCODE_FORMAT,
                "entries": [{**shared, "requested_effort": "turbo"}],
            }}}],
        ):
            with self.subTest(rows=rows), \
                    patch.object(fleet, "render_candidates", return_value=rows):
                self.assertIsNone(self.service.capabilities(principal())["encode"])

    def test_qualification_accept_checks_intent_slot_shape_and_gateway_descriptors(self) -> None:
        raw = self.request()
        capability = qualification_capability()
        self.assertEqual(set(capability), IMMUTABLE_CANDIDATE_KEYS | {"available_capacity"})

        def select(value: dict[str, object], vector_id: str | None = INTENT_ID) -> None:
            self.destination = RenderDestination(
                site_id="site-a", roster_controller_id="roster-controller-a",
                hub_controller_id="hub-controller-a", node_id="node-m4-a",
                capability_snapshot_sha256=hashlib.sha256(canonical(value)).hexdigest(),
                qualification_vector_id=vector_id, capability_snapshot=value,
                selected_machine={
                    "id": "node-m4-a", "label": "M4 fixture", "chip": "Apple M4",
                    "memory_gb": 16, "hardware_class": "apple-m4-16gb",
                },
            )

        select(capability)
        accepted = self.service.accept(principal(), "qualification-exact", raw)
        self.assertEqual(accepted["state"], "staging")

        # The compiler-derived encode fact is the one optional qualification key: the Hub
        # publishes it per machine, `None` is "nothing to describe", and a malformed one
        # is still refused before any row, permit or asset reference exists. Each accepted
        # staging row is a distinct attempt: the store's identity is
        # (owner_scope, story_uuid, intent_id, attempt_no), so a new logical attempt needs
        # a new attempt_no, exactly as Story supplies.
        encode = {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [{
            "container": "mp4", "requested_codec": "h264",
            "requested_effort": "medium", "crf": 23,
            "quality_cpu_preset": "medium", "bitrate_kbps": None}]}
        select({**capability, "encode": encode})
        self.assertEqual(
            self.service.accept(
                principal(), "qualification-encode", self.request(attempt_no=2))["state"],
            "staging")
        select({**capability, "encode": None})
        self.assertEqual(
            self.service.accept(
                principal(), "qualification-encode-null",
                self.request(attempt_no=3))["state"],
            "staging")
        for index, bad in enumerate((
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": "no"},
            {"format": fleet.RENDER_ENCODE_FORMAT,
             "entries": [{**encode["entries"][0], "requested_effort": "turbo"}]},
        )):
            select({**capability, "encode": bad})
            self.assert_code(
                "RENDER_DESTINATION_UNAVAILABLE", self.service.accept, principal(),
                f"qualification-encode-bad-{index:02d}", self.request(attempt_no=4 + index),
            )

        # Gateway checks what it can know itself: the intent the Hub published, the purpose
        # flags and its own profile/tools/compiler digests. The Hub verifies the rest.
        bounds = capability["bounds"]
        for index, (key, value) in enumerate((
            ("vector_id", "66666666-6666-4666-8666-0000000000bb"),
            ("profile_digest", "f" * 64), ("tools_digest", "f" * 64),
            ("compiler_digest", "f" * 64),
            ("production_eligible", True), ("qualification_only", False),
            ("profile_execution_bounds", {**capability["profile_execution_bounds"],
                                          "unexpected": 1}),
            ("bounds", {**bounds, "output_limit_bytes": 0}),
        )):
            select({**capability, key: value})
            self.assert_code(
                "RENDER_DESTINATION_UNAVAILABLE", self.service.accept, principal(),
                f"qualification-immutable-{index:02d}", raw,
            )
        select(capability, vector_id=None)
        self.assert_code(
            "RENDER_DESTINATION_UNAVAILABLE", self.service.accept, principal(),
            "qualification-no-intent", raw,
        )

        capacity_cases = (
            {"slots": 0, "slots_total": 1},
            {"slots": 1, "slots_total": 2},
            {"slots": 2, "slots_total": 1},
            {"slots": 1, "slots_total": 1, "extra": 0},
            {"slots": 1},
        )
        for index, capacity in enumerate(capacity_cases):
            changed = copy.deepcopy(capability)
            changed["available_capacity"] = capacity
            select(changed)
            self.assert_code(
                "RENDER_DESTINATION_UNAVAILABLE", self.service.accept, principal(),
                f"qualification-capacity-{index:02d}", raw,
            )

        for index, changed in enumerate((
            {**capability, "extra": None},
            {**capability, "packet_digest": "1" * 64},
            {key: value for key, value in capability.items() if key != "exact_encoder"},
        )):
            select(changed)
            self.assert_code(
                "RENDER_DESTINATION_UNAVAILABLE", self.service.accept, principal(),
                f"qualification-shape-{index:02d}", raw,
            )

    def test_accept_persists_exact_bytes_identity_assets_and_destination_before_any_hub_call(self) -> None:
        raw = self.request()
        created = self.service.accept(principal(), "gateway-attempt-001", raw)

        self.assertEqual(created["state"], "staging")
        self.assertEqual(created["gateway_attempt_key"], "gateway-attempt-001")
        self.assertEqual(created["story_uuid"], STORY_UUID)
        self.assertEqual(created["destination_node"], "node-m4-a")
        self.assertEqual(created["inbound_story_body_sha256"], hashlib.sha256(raw).hexdigest())
        persisted = self.store.get_render_staging(created["id"])
        self.assertEqual(persisted["inbound_story_body"], raw)
        self.assertEqual(tuple(row["logical_id"] for row in persisted["assets"]), ("audio", "base"))
        self.assertEqual(self.hub.calls, [])

    def test_cancel_tombstone_before_acceptance_is_exact_replay_and_permanently_fences_launch(self) -> None:
        raw = self.request()
        body_digest = hashlib.sha256(raw).hexdigest()
        first = self.store.request_render_cancel_by_key(
            owner_scope=OWNER,
            dispatch_key="gateway-attempt-cancelled",
            expected_request_sha256=body_digest,
        )
        again = self.store.request_render_cancel_by_key(
            owner_scope=OWNER,
            dispatch_key="gateway-attempt-cancelled",
            expected_request_sha256=body_digest,
        )
        self.assertEqual(first, again)
        self.assertEqual(first["state"], "cancelled_before_acceptance")
        self.assertEqual(first["expected_request_sha256"], body_digest)

        self.assert_code(
            "RENDER_CANCELLED_BEFORE_ACCEPTANCE",
            self.service.accept,
            principal(),
            "gateway-attempt-cancelled",
            raw,
        )
        self.assertIsNone(self.store.get_render_staging_by_attempt_key(
            owner_scope=OWNER, gateway_attempt_key="gateway-attempt-cancelled"
        ))
        self.assertEqual(self.hub.calls, [])

    def test_cancel_tombstone_changed_hash_conflicts_and_other_scope_isolated(self) -> None:
        raw = self.request()
        digest = hashlib.sha256(raw).hexdigest()
        self.store.request_render_cancel_by_key(
            owner_scope=OWNER,
            dispatch_key="gateway-attempt-cancel-hash",
            expected_request_sha256=digest,
        )
        with self.assertRaises(RenderJournalConflict):
            self.store.request_render_cancel_by_key(
                owner_scope=OWNER,
                dispatch_key="gateway-attempt-cancel-hash",
                expected_request_sha256="f" * 64,
            )

        other_scope = "88888888-8888-4888-8888-888888888888"
        other = self.store.request_render_cancel_by_key(
            owner_scope=other_scope,
            dispatch_key="gateway-attempt-cancel-hash",
            expected_request_sha256="f" * 64,
        )
        self.assertEqual(other["owner_scope"], other_scope)

    def test_cancel_of_accepted_staging_row_binds_that_original_job(self) -> None:
        raw = self.request()
        accepted = self.service.accept(principal(), "gateway-attempt-cancel-existing", raw)
        inbound_digest = hashlib.sha256(raw).hexdigest()
        receipt = self.service.request_cancel_by_key(
            principal(), "gateway-attempt-cancel-existing", inbound_digest,
        )
        self.assertEqual(receipt["gateway_job_id"], accepted["id"])
        self.assertEqual(receipt["state"], "cancel_requested")
        self.assertEqual(receipt["expected_request_sha256"], inbound_digest)
        stored = self.store.get_render_staging(accepted["id"])
        self.assertEqual(stored["cancel_state"], "requested")
        self.assertEqual(json.loads(stored["cancel_body"]), {
            "expected_request_sha256": inbound_digest,
        })
        self.assert_code(
            "CANCEL_REQUEST_CONFLICT", self.service.request_cancel_by_key,
            principal(), "gateway-attempt-cancel-existing", "f" * 64,
        )
        self.assertEqual(self.hub.calls, [])

    def test_exact_replay_is_accepted_first_but_changed_bytes_or_scope_conflict(self) -> None:
        raw = self.request()
        first = self.service.accept(principal(), "gateway-attempt-002", raw)
        again = self.service.accept(principal(), "gateway-attempt-002", raw)
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent_replay"])

        changed_bytes = raw + b" "
        self.assert_code(
            "IDEMPOTENCY_KEY_CONFLICT",
            self.service.accept,
            principal(),
            "gateway-attempt-002",
            changed_bytes,
        )
        other_scope = "66666666-6666-4666-8666-666666666666"
        other_principal = principal(owner_scope=other_scope)
        self.assertIsNone(self.service.by_key(other_principal, "gateway-attempt-002"))
        with self.assertRaises(RenderServiceError) as foreign_assets:
            self.service.accept(other_principal, "gateway-attempt-foreign-assets", raw)
        self.assertEqual(foreign_assets.exception.status, 404)
        self.assertNotIn(next(iter(json.loads(raw)["assets"].values())), str(foreign_assets.exception))

        other_raw = self.request(owner_scope=other_scope)
        other = self.service.accept(other_principal, "gateway-attempt-002", other_raw)
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(self.service.by_key(other_principal, "gateway-attempt-002")["id"], other["id"])
        self.assertEqual(self.hub.calls, [])

class DispatchLifecycleTests(RenderServiceCase):
    def _row(self, job_id: str) -> dict[str, object]:
        return self.store.get_render_staging(job_id)

    def _stage_inputs_and_prepare(self, job_id: str) -> dict[str, object]:
        for index in range(2):
            current = self._row(job_id)
            asset = current["assets"][index]
            self.hub.answers.setdefault("put_blob", []).append({
                "asset_handle": ("c" if index == 0 else "d") * 32,
                "sha256": asset["sha256"],
                "size_bytes": asset["size_bytes"],
            })
            self.service._one_step(current)
        self.service._one_step(self._row(job_id))
        return self._row(job_id)

    def _hub_job(self, row, *, job_id="66666666-6666-4666-8666-666666666666",
                 state="accepted", attempt_id=None, output_receipt=None,
                 purpose="production", owner_scope=None, compute=None,
                 stopped_evidence_digest=None):
        return {
            "id": job_id,
            "owner_scope": owner_scope or "site-a:node-m4-a:fleet",
            "dispatch_key": row["dispatch_key"],
            "accepted_request_sha256": row["outbound_hub_body_sha256"],
            "purpose": purpose, "state": state,
            "machine": "node-m4-a" if attempt_id else None,
            "dispatch_generation": 1 if attempt_id else None,
            "attempt_id": attempt_id, "cancel_requested_at": None,
            "profile_digest": hashlib.sha256(PROFILE_RAW).hexdigest(),
            "tools_digest": "d" * 64, "compiler_digest": "c" * 64,
            "qualification_digest": "4" * 64,
            "stopped_evidence_digest": stopped_evidence_digest,
            "output_receipt": output_receipt,
            "leases": {"compute": (state not in {"failed", "cancelled", "output_ready"}
                                   if compute is None else compute),
                       "reference": 1, "reader": 0, "transfer": 0, "ack": 0},
            "replayed": False,
        }

    @staticmethod
    def _receipt(row, *, hub_job_id, attempt_id, output=b"fake-winning-output",
                 owner_scope="site-a:node-m4-a:fleet", **changes):
        value = {
            "owner_scope": owner_scope,
            "hub_job_id": hub_job_id, "attempt_id": attempt_id,
            "accepted_request_sha256": row["outbound_hub_body_sha256"],
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "output_size_bytes": len(output),
            "profile_digest": hashlib.sha256(PROFILE_RAW).hexdigest(),
            "tools_digest": "d" * 64, "compiler_digest": "c" * 64,
            "qualification_digest": "4" * 64,
            "generated_command_digest": "5" * 64,
            "selected_tier": "software-h264", "feature_losses": [],
        }
        value.update(changes)
        value["receipt_digest"] = hashlib.sha256(canonical(value)).hexdigest()
        return value

    def test_each_input_range_and_exact_dispatch_are_durable_before_the_first_send(self) -> None:
        accepted = self.service.accept(principal(), "gateway-stage-before-send", self.request())

        def verify_pending(_destination, _sha, offset, _total, data, range_sha):
            current = self._row(accepted["id"])
            pending = next(item for item in current["assets"]
                           if item["pending_offset"] is not None)
            self.assertEqual((pending["pending_offset"], pending["pending_length"]),
                             (offset, len(data)))
            self.assertEqual(pending["pending_sha256"], range_sha)

        self.hub.before["put_blob"] = verify_pending
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.assertEqual(prepared["state"], "hub_prepared")
        self.assertEqual(
            hashlib.sha256(bytes(prepared["outbound_hub_body"])).hexdigest(),
            prepared["outbound_hub_body_sha256"],
        )
        journal = self.store.get_render_journal(accepted["id"])
        self.assertEqual(journal["state"], "prepared")
        self.assertFalse(any(name == "submit" for name, _ in self.hub.calls))

    def test_relay_parent_and_target_node_service_identities_stay_distinct(self) -> None:
        self.destination = RenderDestination(
            site_id="site-a",
            roster_controller_id="roster-controller-a",
            hub_controller_id="parent-controller-a",
            target_hub_controller_id="node-controller-a",
            node_id="node-controller-a",
            capability_snapshot_sha256=hashlib.sha256(
                canonical(CAPABILITY_SNAPSHOT)
            ).hexdigest(),
            capability_snapshot=CAPABILITY_SNAPSHOT,
            selected_machine={
                "id": "node-controller-a", "label": "M4 fixture", "chip": "Apple M4",
                "memory_gb": 16, "hardware_class": "apple-m4-16gb",
            },
        )
        accepted = self.service.accept(principal(), "gateway-distinct-hop-identities", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        outbound = json.loads(bytes(prepared["outbound_hub_body"]))
        self.assertEqual(outbound["dispatch"]["destination_controller"], "node-controller-a")
        valid = self._hub_job(
            prepared, owner_scope="site-a:node-controller-a:fleet",
        )
        self.assertEqual(self.service._hub_job(prepared, valid)["owner_scope"],
                         "site-a:node-controller-a:fleet")
        self.assert_code(
            "HUB_RESPONSE_UNKNOWN", self.service._hub_job, prepared,
            self._hub_job(prepared, owner_scope="site-a:parent-controller-a:fleet"),
        )

    def test_target_node_scope_fences_output_receipt_lineage(self) -> None:
        self.destination = RenderDestination(
            site_id="site-a", roster_controller_id="roster-controller-a",
            hub_controller_id="parent-controller-a",
            target_hub_controller_id="node-controller-a", node_id="node-controller-a",
            capability_snapshot_sha256=hashlib.sha256(
                canonical(CAPABILITY_SNAPSHOT)
            ).hexdigest(), capability_snapshot=CAPABILITY_SNAPSHOT,
            selected_machine={
                "id": "node-controller-a", "label": "M4 fixture", "chip": "Apple M4",
                "memory_gb": 16, "hardware_class": "apple-m4-16gb",
            },
        )
        accepted = self.service.accept(principal(), "gateway-target-output", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        attempt_id = "abcdefabcdefabcdefabcdefabcdefab"
        self.hub.answers["submit"] = [self._hub_job(
            prepared, job_id=hub_id, owner_scope="site-a:node-controller-a:fleet",
        )]
        self.service._one_step(prepared)
        row = self._row(accepted["id"])
        output = b"target-node-output"
        receipt = self._receipt(
            row, hub_job_id=hub_id, attempt_id=attempt_id, output=output,
            owner_scope="site-a:node-controller-a:fleet",
        )
        delivery = self.store.update_render_delivery(
            row["id"], expected_revision=0, remote_state="output_ready",
            remote_attempt_id=attempt_id, remote_descriptor=receipt,
        )
        output_sha = hashlib.sha256(output).hexdigest()
        self.hub.answers["output_range"] = [(
            output, {"content-range": f"bytes 0-{len(output) - 1}/{len(output)}",
                     "etag": f'"{output_sha}"', "x-content-sha256": output_sha,
                     "content-type": "video/mp4"},
        )]
        self.service._import_output(row, delivery, self.destination)
        self.service._import_output(
            row, self.store.get_render_delivery(row["id"]), self.destination,
        )
        public_receipt = json.loads(
            self.service.status(principal(), accepted["id"])["output"]["receipt_utf8"]
        )
        self.assertEqual(public_receipt["hub_owner_scope"],
                         "site-a:node-controller-a:fleet")

        wrong = self._receipt(
            row, hub_job_id=hub_id, attempt_id=attempt_id, output=output,
            owner_scope="site-a:parent-controller-a:fleet",
        )
        with self.assertRaises(RenderServiceError) as caught:
            self.service._import_output(
                row, {**delivery, "remote_descriptor_json": json.dumps(wrong)},
                self.destination,
            )
        self.assertEqual(caught.exception.code, "HUB_OUTPUT_UNKNOWN")

    def test_published_hub_blob_reconciliation_survives_lost_final_reply(self) -> None:
        accepted = self.service.accept(principal(), "gateway-lost-final-blob", self.request())
        row = self._row(accepted["id"])
        asset = row["assets"][0]
        self.hub.answers["blob_status"] = [
            {"accepted": False, "http_status": 404},
            {
                "upload_id": "7" * 32, "asset_handle": "8" * 32,
                "sha256": asset["sha256"], "size_bytes": asset["size_bytes"],
                "committed_offset": asset["size_bytes"], "state": "published",
            },
        ]
        self.hub.answers["put_blob"] = [TimeoutError("fake lost final upload response")]
        with self.assertRaises(TimeoutError):
            self.service._one_step(row)
        self.service._one_step(self._row(accepted["id"]))
        recovered = self._row(accepted["id"])["assets"][0]
        self.assertEqual((recovered["confirmed_hub_offset"], recovered["hub_asset_handle"]),
                         (asset["size_bytes"], "8" * 32))
        self.assertEqual(len([name for name, _ in self.hub.calls if name == "put_blob"]), 1)

    def test_published_digest_is_reused_by_later_job_and_two_logical_assets(self) -> None:
        raw = self.shared_digest_request()
        digest = json.loads(raw)["packet"]["manifest"][0]["sha256"]
        size = json.loads(raw)["packet"]["manifest"][0]["size_bytes"]
        published = {
            "upload_id": "7" * 32, "asset_handle": "8" * 32,
            "sha256": digest, "size_bytes": size, "committed_offset": size,
            "state": "published",
        }
        first = self.service.accept(principal(), "gateway-shared-digest-one", raw)
        self.hub.answers["blob_status"] = [published, published]
        self.service._one_step(self._row(first["id"]))
        self.service._one_step(self._row(first["id"]))
        self.service._one_step(self._row(first["id"]))
        self.assertEqual(self._row(first["id"])["state"], "hub_prepared")
        self.assertFalse(any(name == "put_blob" for name, _ in self.hub.calls))

        later_raw = self.shared_digest_request(intent_id="shared-digest-later", attempt_no=2)
        later = self.service.accept(principal(), "gateway-shared-digest-two", later_raw)
        self.hub.answers["blob_status"] = [published, published]
        self.service._one_step(self._row(later["id"]))
        self.service._one_step(self._row(later["id"]))
        self.assertTrue(all(
            item["hub_asset_handle"] == "8" * 32
            for item in self._row(later["id"])["assets"]
        ))

    def test_literal_six_key_incomplete_status_resumes_at_its_exact_frontier(self) -> None:
        accepted = self.service.accept(
            principal(), "gateway-six-key-incomplete", self.request()
        )
        row = self._row(accepted["id"])
        asset = row["assets"][0]
        self.hub.answers["blob_status"] = [{
            "upload_id": "7" * 32, "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"], "committed_offset": 0,
            "state": "incomplete", "asset_handle": None,
        }]
        self.hub.answers["put_blob"] = [{
            "asset_handle": "8" * 32, "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
        }]
        self.service._one_step(row)
        resumed = self._row(accepted["id"])["assets"][0]
        self.assertEqual(
            (resumed["confirmed_hub_offset"], resumed["hub_asset_handle"]),
            (asset["size_bytes"], "8" * 32),
        )

    def test_changed_hub_blob_status_never_advances_the_pending_range(self) -> None:
        def mutate(value, **changes):
            return {**value, **changes}

        for index, kind in enumerate(("sha", "size", "state", "fields", "offset")):
            with self.subTest(kind=kind):
                accepted = self.service.accept(
                    principal(), f"gateway-changed-blob-status-{index}",
                    self.request(intent_id=f"changed-blob-status-{index}"),
                )
                row = self._row(accepted["id"])
                asset = row["assets"][0]
                status = {
                    "upload_id": "7" * 32, "asset_handle": "8" * 32,
                    "sha256": asset["sha256"], "size_bytes": asset["size_bytes"],
                    "committed_offset": asset["size_bytes"], "state": "published",
                }
                bad = {
                    "sha": mutate(status, sha256="9" * 64),
                    "size": mutate(status, size_bytes=asset["size_bytes"] + 1),
                    "state": mutate(status, state="recovery_required", asset_handle=None),
                    "fields": mutate(status, published=True),
                    "offset": mutate(status, committed_offset=asset["size_bytes"] - 1),
                }[kind]
                puts = len([name for name, _ in self.hub.calls if name == "put_blob"])
                self.hub.answers["blob_status"] = [bad]
                self.service._one_step(row)
                held = self._row(accepted["id"])["assets"][0]
                self.assertEqual(
                    (held["confirmed_hub_offset"], held["hub_asset_handle"]), (0, None)
                )
                self.assertEqual(
                    len([name for name, _ in self.hub.calls if name == "put_blob"]), puts
                )

    def test_ambiguous_submit_reconciles_only_the_original_key_and_node(self) -> None:
        accepted = self.service.accept(principal(), "gateway-send-unknown", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost response")]
        self.service._one_step(prepared)
        unknown = self._row(accepted["id"])
        self.assertEqual(unknown["state"], "send_unknown")

        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["by_key"] = [self._hub_job(unknown, job_id=hub_id)]
        self.service._one_step(unknown)
        attached = self._row(accepted["id"])
        self.assertEqual((attached["state"], attached["hub_job_id"]),
                         ("accepted", hub_id))
        submits = [call for call in self.hub.calls if call[0] == "submit"]
        lookups = [call for call in self.hub.calls if call[0] == "by_key"]
        self.assertEqual((len(submits), len(lookups)), (1, 1))
        self.assertEqual(submits[0][1][0][0], lookups[0][1][0][0])
        self.assertEqual(submits[0][1][0][1], lookups[0][1][0][1])

    def _advance_to_accepted(self, key: str, *, hub_id: str, attempt_id: str) -> dict[str, object]:
        accepted = self.service.accept(principal(), key, self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [
            self._hub_job(prepared, job_id=hub_id, state="accepted", attempt_id=attempt_id)
        ]
        self.service._one_step(prepared)
        row = self._row(accepted["id"])
        self.assertEqual((row["state"], row["hub_job_id"]), ("accepted", hub_id))
        return row

    def test_send_unknown_definitive_404_replays_the_identical_submit(self) -> None:
        accepted = self.service.accept(principal(), "gateway-replay-404", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost response")]
        self.service._one_step(prepared)
        unknown = self._row(accepted["id"])
        self.assertEqual(unknown["state"], "send_unknown")

        hub_id = "77777777-7777-4777-8777-777777777777"
        self.hub.answers["by_key"] = [{"accepted": False, "http_status": 404}]
        self.hub.answers["submit"] = [self._hub_job(unknown, job_id=hub_id)]
        self.service._one_step(unknown)
        attached = self._row(accepted["id"])
        self.assertEqual((attached["state"], attached["hub_job_id"]), ("accepted", hub_id))
        submits = [call for call in self.hub.calls if call[0] == "submit"]
        self.assertEqual(len(submits), 2)
        first, replay = submits[0][1][0], submits[1][1][0]
        self.assertEqual(first[0], replay[0])  # same destination
        self.assertEqual(first[1], replay[1])  # same dispatch key
        self.assertEqual(first[2], replay[2])  # same exact body
        self.assertEqual(replay[1], unknown["dispatch_key"])

    def test_send_unknown_non_404_stays_unknown_without_resubmitting(self) -> None:
        accepted = self.service.accept(principal(), "gateway-stay-unknown", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost response")]
        self.service._one_step(prepared)
        unknown = self._row(accepted["id"])
        self.assertEqual(unknown["state"], "send_unknown")
        self.hub.answers["by_key"] = [{"accepted": False, "http_status": 503}]
        self.service._one_step(unknown)
        self.assertEqual(self._row(accepted["id"])["state"], "send_unknown")
        self.assertEqual(len([call for call in self.hub.calls if call[0] == "submit"]), 1)

    def test_hub_prepared_definitive_404_is_replayed_before_becoming_unknown(self) -> None:
        accepted = self.service.accept(principal(), "gateway-prepared-404", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "88888888-8888-4888-8888-888888888888"
        self.hub.answers["submit"] = [
            {"accepted": False, "http_status": 404},
            self._hub_job(prepared, job_id=hub_id),
        ]
        self.service._one_step(prepared)
        row = self._row(accepted["id"])
        self.assertEqual((row["state"], row["hub_job_id"]), ("accepted", hub_id))
        submits = [call for call in self.hub.calls if call[0] == "submit"]
        self.assertEqual(len(submits), 2)
        self.assertEqual(submits[0][1][0][2], submits[1][1][0][2])

    def test_terminal_remote_failure_is_recorded_and_no_longer_polled(self) -> None:
        hub_id = "99999999-9999-4999-8999-999999999999"
        attempt = "a1" * 16
        row = self._advance_to_accepted("gateway-terminal-failed", hub_id=hub_id, attempt_id=attempt)
        self.hub.answers["status"] = [
            self._hub_job(row, job_id=hub_id, state="failed", attempt_id=attempt)
        ]
        self.service._one_step(row)
        self.assertEqual(self.store.get_render_delivery(row["id"])["remote_state"], "failed")
        self.assertEqual(self._row(row["id"])["state"], "accepted")
        self.assertNotIn(row["id"], [item["id"] for item in self.store.render_due(limit=64)])
        public = self.service.status(principal(), row["id"])
        self.assertEqual(public["remote_state"], "failed")
        self.assertNotIn("terminal_proof", public)

    def test_cancel_on_already_failed_render_clears_pending_cancel(self) -> None:
        hub_id = "abababab-abab-4bab-8bab-abababababab"
        attempt = "b2" * 16
        row = self._advance_to_accepted("gateway-cancel-failed", hub_id=hub_id, attempt_id=attempt)
        self.service.request_cancel(principal(), row["id"])
        pending = self._row(row["id"])
        self.assertEqual(pending["cancel_state"], "requested")
        self.hub.answers["cancel"] = [
            self._hub_job(pending, job_id=hub_id, state="failed", attempt_id=attempt)
        ]
        self.service._one_step(pending)
        cleared = self._row(row["id"])
        self.assertIsNone(cleared["cancel_state"])
        self.assertEqual(self.store.get_render_delivery(row["id"])["remote_state"], "failed")
        self.assertNotIn(row["id"], [item["id"] for item in self.store.render_due(limit=64)])
        public = self.service.status(principal(), row["id"])
        self.assertEqual(public["remote_state"], "failed")
        self.assertNotIn("terminal_proof", public)
        self.assertEqual(len([call for call in self.hub.calls if call[0] == "cancel"]), 1)

    def test_cancel_on_output_ready_stops_resending_and_keeps_output_deliverable(self) -> None:
        hub_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
        attempt = "c3" * 16
        row = self._advance_to_accepted("gateway-cancel-output", hub_id=hub_id, attempt_id=attempt)
        self.service.request_cancel(principal(), row["id"])
        pending = self._row(row["id"])
        self.assertEqual(pending["cancel_state"], "requested")
        receipt = self._receipt(pending, hub_job_id=hub_id, attempt_id=attempt)
        self.hub.answers["cancel"] = [
            self._hub_job(pending, job_id=hub_id, state="output_ready",
                          attempt_id=attempt, output_receipt=receipt)
        ]
        self.service._one_step(pending)
        cleared = self._row(row["id"])
        self.assertIsNone(cleared["cancel_state"])
        self.assertEqual(self.store.get_render_delivery(row["id"])["remote_state"], "output_ready")
        # The render is still due so the normal output import/ack path can deliver it.
        self.assertIn(row["id"], [item["id"] for item in self.store.render_due(limit=64)])
        self.assertEqual(len([call for call in self.hub.calls if call[0] == "cancel"]), 1)

    def test_dispatch_rotation_reaches_more_than_eight_rows_despite_unchanged_oldest(self) -> None:
        jobs = [
            self.service.accept(
                principal(), f"gateway-fair-{index}",
                self.request(intent_id=f"episode-fair-{index}"),
            )
            for index in range(10)
        ]
        seen: list[str] = []

        def immediate(row):
            seen.append(row["id"])

        with patch.object(self.service, "_one_step", side_effect=immediate):
            for _ in jobs:
                self.assertTrue(self.service.dispatch_pass())
                self.wait_for_render_steps()
        self.assertEqual(seen, [job["id"] for job in jobs])

    def test_network_step_ceiling_remains_two_while_fair_dispatch_is_busy(self) -> None:
        for index in range(3):
            self.service.accept(
                principal(), f"gateway-step-ceiling-{index}",
                self.request(intent_id=f"episode-step-ceiling-{index}"),
            )
        release = threading.Event()
        entered = threading.Barrier(3)

        def blocked(_row):
            entered.wait(timeout=2)
            release.wait(timeout=2)

        with patch.object(self.service, "_one_step", side_effect=blocked):
            self.assertTrue(self.service.dispatch_pass())
            self.assertTrue(self.service.dispatch_pass())
            entered.wait(timeout=2)
            self.wait_for_render_steps(expected_active=2)
            self.assertFalse(self.service.dispatch_pass())
            release.set()
            self.wait_for_render_steps()

    def test_hub_acked_story_unacked_output_does_not_starve_later_network_work(self) -> None:
        older = self.service.accept(principal(), "gateway-hub-acked", self.request())
        later = self.service.accept(
            principal(), "gateway-after-hub-acked",
            self.request(intent_id="episode-after-hub-acked"),
        )
        with self.store._render_write():
            self.store._conn.execute(
                "UPDATE render_staging SET state='output_ready' WHERE id=?", (older["id"],)
            )
            self.store._conn.execute(
                "UPDATE render_delivery SET ack_outcome='accepted' WHERE render_id=?",
                (older["id"],),
            )
        due = self.store.render_due(limit=64)
        self.assertNotIn(older["id"], [row["id"] for row in due])
        self.assertIn(later["id"], [row["id"] for row in due])

    def guarded(self, key_configured) -> FinalRenderService:
        service = FinalRenderService(
            self.store, self.assets,
            destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, profile_descriptor=PROFILE_RAW,
            profile_facts=self.service.profile_facts, key_configured=key_configured,
        )
        self.addCleanup(service.stop)
        return service

    @contextlib.contextmanager
    def reinstalled(self, **identity: str):
        install(self.store, **(identity or {"gateway_instance_id": REINSTALLED_GATEWAY}))
        try:
            yield
        finally:
            install(self.store)

    def test_key_and_installation_are_rechecked_before_first_blob_or_submit_send(self) -> None:
        held_states = (
            (lambda: False, {}),
            (lambda: (_ for _ in ()).throw(RuntimeError("fake key read failure")), {}),
            (lambda: True, {"gateway_instance_id": REINSTALLED_GATEWAY}),
            (lambda: True, {"owner_scope": REINSTALLED_GATEWAY}),
        )
        for index, (key_configured, identity) in enumerate(held_states):
            with self.subTest(index=index):
                accepted = self.service.accept(
                    principal(), f"gateway-credential-hold-{index}",
                    self.request(intent_id=f"credential-hold-{index}"),
                )
                guarded = self.guarded(key_configured)
                with self.reinstalled(**identity) if identity else contextlib.nullcontext():
                    guarded._one_step(self._row(accepted["id"]))
                self.assertEqual(self._row(accepted["id"])["state"], "staging")
                self.assertEqual(self.hub.calls, [])

        accepted = self.service.accept(
            principal(), "gateway-revoke-before-post",
            self.request(intent_id="revoke-before-post"),
        )
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        calls_before = list(self.hub.calls)
        self.guarded(lambda: False)._one_step(prepared)
        self.assertEqual(self._row(accepted["id"])["state"], "hub_prepared")
        self.assertEqual(self.hub.calls, calls_before)

    def test_unconfigured_key_still_allows_original_key_reconciliation(self) -> None:
        accepted = self.service.accept(
            principal(), "gateway-revoked-reconcile",
            self.request(intent_id="revoked-reconcile"),
        )
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost response")]
        self.service._one_step(prepared)
        unknown = self._row(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["by_key"] = [self._hub_job(unknown, job_id=hub_id)]
        self.guarded(lambda: False)._one_step(unknown)
        self.assertEqual(self._row(accepted["id"])["hub_job_id"], hub_id)

    def test_reinstalled_identity_blocks_fresh_sends_but_preserves_history(self) -> None:
        replaced = self.guarded(lambda: True)
        fresh = self.service.accept(
            principal(), "gateway-replaced-record-blob",
            self.request(intent_id="replaced-record-blob"),
        )
        calls_before = list(self.hub.calls)
        with self.reinstalled():
            replaced._one_step(self._row(fresh["id"]))
        self.assertEqual(self._row(fresh["id"])["state"], "staging")
        self.assertEqual(self.hub.calls, calls_before)

        exact = self.service.accept(
            principal(), "gateway-exact-record-blob",
            self.request(intent_id="exact-record-blob"),
        )
        exact_row = self._row(exact["id"])
        exact_asset = exact_row["assets"][0]
        self.hub.answers["blob_status"] = [{
            "upload_id": "7" * 32, "asset_handle": "8" * 32,
            "sha256": exact_asset["sha256"], "size_bytes": exact_asset["size_bytes"],
            "committed_offset": exact_asset["size_bytes"], "state": "published",
        }]
        matching = self.guarded(lambda: True)
        matching._one_step(exact_row)
        self.assertEqual(
            self._row(exact["id"])["assets"][0]["hub_asset_handle"], "8" * 32
        )

        before_post = self.service.accept(
            principal(), "gateway-replaced-record-post",
            self.request(intent_id="replaced-record-post"),
        )
        prepared = self._stage_inputs_and_prepare(before_post["id"])
        calls_before = list(self.hub.calls)
        with self.reinstalled():
            replaced._one_step(prepared)
        self.assertEqual(self._row(before_post["id"])["state"], "hub_prepared")
        self.assertEqual(self.hub.calls, calls_before)

        historical = self.service.accept(
            principal(), "gateway-replaced-record-history",
            self.request(intent_id="replaced-record-history"),
        )
        historical_prepared = self._stage_inputs_and_prepare(historical["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost response")]
        self.service._one_step(historical_prepared)
        unknown = self._row(historical["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["by_key"] = [self._hub_job(unknown, job_id=hub_id)]
        with self.reinstalled():
            replaced._one_step(unknown)
            attached = self._row(historical["id"])
            self.assertEqual((attached["state"], attached["hub_job_id"]), ("accepted", hub_id))
            replaced.request_cancel(principal(), historical["id"])
            self.hub.answers["cancel"] = [self._hub_job(
                self._row(historical["id"]), job_id=hub_id, state="cancel_requested",
            )]
            replaced._one_step(self._row(historical["id"]))
        self.assertEqual(self._row(historical["id"])["cancel_outcome"], "accepted")

    def test_server_owned_qualification_destination_requires_qualification_hub_purpose(self) -> None:
        raw = self.request()
        capability = qualification_capability()
        self.destination = RenderDestination(
            site_id="site-a", roster_controller_id="roster-controller-a",
            hub_controller_id="hub-controller-a", node_id="node-m4-a",
            capability_snapshot_sha256=hashlib.sha256(canonical(capability)).hexdigest(),
            qualification_vector_id=INTENT_ID, capability_snapshot=capability,
            selected_machine={
                "id": "node-m4-a", "label": "M4 fixture", "chip": "Apple M4",
                "memory_gb": 16, "hardware_class": "apple-m4-16gb",
            },
        )
        accepted = self.service.accept(principal(), "gateway-qualification-purpose", raw)
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        qualified = self._hub_job(prepared, purpose="qualification")
        self.assertEqual(self.service._hub_job(prepared, qualified)["purpose"], "qualification")
        self.assert_code(
            "HUB_RESPONSE_UNKNOWN", self.service._hub_job, prepared,
            self._hub_job(prepared, purpose="production"),
        )

    def test_cancel_body_and_original_key_are_persisted_and_ambiguous_send_replays_only_it(self) -> None:
        accepted = self.service.accept(principal(), "gateway-cancel-race", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
        self.service._one_step(prepared)
        self.service.request_cancel(principal(), accepted["id"])

        def verify_cancel(_destination, _hub_job_id, body):
            current = self._row(accepted["id"])
            self.assertEqual(current["cancel_outcome"], "send_unknown")
            self.assertEqual(bytes(current["cancel_body"]), body)
            self.assertEqual(current["cancel_key"], current["dispatch_key"])

        self.hub.before["cancel"] = verify_cancel
        self.hub.answers["cancel"] = [TimeoutError("fake ambiguous cancel")]
        with self.assertRaises(TimeoutError):
            self.service._one_step(self._row(accepted["id"]))
        restarted = FinalRenderService(
            self.store, self.assets, destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: True,
            profile_descriptor=PROFILE_RAW,
        )
        self.hub.answers["cancel"] = [self._hub_job(
            self._row(accepted["id"]), job_id=hub_id, state="cancel_requested",
            attempt_id="abcdefabcdefabcdefabcdefabcdefab",
        )]
        restarted._one_step(self._row(accepted["id"]))
        self.assertEqual(len([call for call in self.hub.calls if call[0] == "cancel"]), 2)
        self.hub.answers["status"] = [self._hub_job(
            self._row(accepted["id"]), job_id=hub_id, state="cancelled",
            attempt_id="abcdefabcdefabcdefabcdefabcdefab",
            compute=False, stopped_evidence_digest="6" * 64,
        )]
        restarted._one_step(self._row(accepted["id"]))
        self.assertEqual(self._row(accepted["id"])["cancel_state"], "cancelled")

    def test_cancelled_by_key_binds_first_hub_identity_before_terminal_settlement(self) -> None:
        accepted = self.service.accept(
            principal(), "gateway-cancel-first-identity", self.request()
        )
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        self.hub.answers["submit"] = [TimeoutError("fake lost submit response")]
        self.service._one_step(prepared)
        self.service.request_cancel(principal(), accepted["id"])

        hub_id = "66666666-6666-4666-8666-666666666666"
        attempt_id = "abcdefabcdefabcdefabcdefabcdefab"
        stop_digest = "6" * 64
        unknown = self._row(accepted["id"])
        terminal = self._hub_job(
            unknown, job_id=hub_id, state="cancelled", attempt_id=attempt_id,
            compute=False, stopped_evidence_digest=stop_digest,
        )
        self.hub.answers["cancel_by_key"] = [terminal]
        self.service._one_step(unknown)

        reopened_store = JobStore(self.case_root / "gateway.sqlite3")
        self.addCleanup(reopened_store.close)
        restarted = FinalRenderService(
            reopened_store, self.assets,
            destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: True,
            profile_descriptor=PROFILE_RAW, profile_facts=self.service.profile_facts,
        )
        self.addCleanup(restarted.stop)
        public = restarted.status(principal(), accepted["id"])
        self.assertEqual(public["terminal_proof"], {
            "format": "studio.render.gateway-terminal-proof.v1",
            "kind": "launched_stopped", "gateway_job_id": accepted["id"],
            "gateway_attempt_key": "gateway-cancel-first-identity",
            "hub_job_id": hub_id, "hub_attempt_id": attempt_id,
            "stopped_evidence_digest": stop_digest,
        })
        row = reopened_store.get_render_staging(accepted["id"])
        self.assertEqual(restarted._hub_job(row, terminal), terminal)
        invalid = (
            self._hub_job(
                unknown, job_id="77777777-7777-4777-8777-777777777777",
                state="cancelled", attempt_id=attempt_id, compute=False,
                stopped_evidence_digest=stop_digest,
            ),
            self._hub_job(
                unknown, job_id=hub_id, state="cancelled",
                attempt_id="0123456789abcdef0123456789abcdef", compute=False,
                stopped_evidence_digest=stop_digest,
            ),
            self._hub_job(
                unknown, job_id=hub_id, state="cancelled", attempt_id=attempt_id,
                compute=False, stopped_evidence_digest="7" * 64,
            ),
        )
        for answer in invalid:
            with self.subTest(answer=answer):
                self.assert_code("HUB_RESPONSE_UNKNOWN", restarted._hub_job, row, answer)

    def test_cancel_poll_binds_first_attempt_before_terminal_settlement(self) -> None:
        accepted = self.service.accept(
            principal(), "gateway-cancel-first-attempt", self.request()
        )
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
        self.service._one_step(prepared)
        self.service.request_cancel(principal(), accepted["id"])
        self.hub.answers["cancel"] = [self._hub_job(
            self._row(accepted["id"]), job_id=hub_id, state="cancel_requested",
        )]
        self.service._one_step(self._row(accepted["id"]))

        reopened_store = JobStore(self.case_root / "gateway.sqlite3")
        self.addCleanup(reopened_store.close)
        restarted = FinalRenderService(
            reopened_store, self.assets,
            destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: True,
            profile_descriptor=PROFILE_RAW, profile_facts=self.service.profile_facts,
        )
        self.addCleanup(restarted.stop)
        attempt_id = "abcdefabcdefabcdefabcdefabcdefab"
        stop_digest = "6" * 64
        polling = reopened_store.get_render_staging(accepted["id"])
        terminal = self._hub_job(
            polling, job_id=hub_id, state="cancelled", attempt_id=attempt_id,
            compute=False, stopped_evidence_digest=stop_digest,
        )
        self.hub.answers["status"] = [terminal]
        restarted._one_step(polling)
        public = restarted.status(principal(), accepted["id"])
        self.assertEqual(public["terminal_proof"], {
            "format": "studio.render.gateway-terminal-proof.v1",
            "kind": "launched_stopped", "gateway_job_id": accepted["id"],
            "gateway_attempt_key": "gateway-cancel-first-attempt",
            "hub_job_id": hub_id, "hub_attempt_id": attempt_id,
            "stopped_evidence_digest": stop_digest,
        })
        self.assertEqual(
            restarted._hub_job(reopened_store.get_render_staging(accepted["id"]), terminal),
            terminal,
        )

    def test_known_hub_cancel_accepts_only_story_body_hash_and_fences_with_hub_body_hash(self) -> None:
        raw = self.request()
        accepted = self.service.accept(principal(), "gateway-cancel-per-hop", raw)
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        inbound_digest = hashlib.sha256(raw).hexdigest()
        self.assertNotEqual(inbound_digest, prepared["outbound_hub_body_sha256"])

        public = self.service.request_cancel_by_key(
            principal(), "gateway-cancel-per-hop", inbound_digest,
        )
        stored = self._row(accepted["id"])
        self.assertEqual(public["expected_request_sha256"], inbound_digest)
        self.assertEqual(json.loads(stored["cancel_body"]), {
            "expected_request_sha256": prepared["outbound_hub_body_sha256"],
        })
        self.assert_code(
            "CANCEL_REQUEST_CONFLICT", self.service.request_cancel_by_key,
            principal(), "gateway-cancel-per-hop", "e" * 64,
        )

    @staticmethod
    def _cancel_fence(*, dispatch_key, digest, fence_id="3" * 32,
                      cancelled_at=1_700_000_000.0):
        return {
            "owner_scope": "site-a:node-m4-a:fleet", "dispatch_key": dispatch_key,
            "expected_request_sha256": digest, "fence_id": fence_id,
            "cancelled_at": cancelled_at, "state": "cancelled_before_acceptance",
        }

    def test_staging_cancel_settles_never_launched_through_the_prepared_cancel_key(self) -> None:
        # A cancel that arrives before the dispatch body is prepared (dispatch_key null) must
        # still reach the Hub and settle. The store prepares cancel_key = the raw gateway
        # attempt key before any dispatch exists; the service sends by it and binds the fence
        # to the digest embedded in the cancel body (not the hash of the cancel body itself).
        raw = self.request()
        accepted = self.service.accept(principal(), "gateway-predispatch-cancel", raw)
        inbound_digest = hashlib.sha256(raw).hexdigest()
        requested = self.service.request_cancel_by_key(
            principal(), "gateway-predispatch-cancel", inbound_digest,
        )
        self.assertEqual(requested["state"], "cancel_requested")
        row = self._row(accepted["id"])
        self.assertEqual(row["state"], "staging")
        self.assertIsNone(row["dispatch_key"])
        self.assertIsNone(row["hub_job_id"])
        self.assertEqual(row["cancel_key"], "gateway-predispatch-cancel")

        self.hub.answers["cancel_by_key"] = [self._cancel_fence(
            dispatch_key="gateway-predispatch-cancel", digest=inbound_digest,
        )]
        self.service._one_step(row)

        cancel_calls = [entry for entry in self.hub.calls if entry[0] == "cancel_by_key"]
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0][1][0][1], "gateway-predispatch-cancel")
        self.assertEqual(json.loads(cancel_calls[0][1][0][2]), {
            "expected_request_sha256": inbound_digest,
        })
        settled = self._row(accepted["id"])
        self.assertEqual(settled["cancel_state"], "cancelled")
        self.assertEqual(settled["cancel_outcome"], "stopped")
        self.assertEqual(settled["terminal_fence_receipt"], "3" * 32)
        self.assertEqual(self.service.status(principal(), accepted["id"])["terminal_proof"], {
            "format": "studio.render.gateway-terminal-proof.v1",
            "kind": "never_launched",
            "gateway_attempt_key": "gateway-predispatch-cancel",
            "inbound_story_body_sha256": inbound_digest,
            "fence_receipt": "3" * 32,
            "first_cancel_at": 1_700_000_000.0,
        })

    def test_staging_cancel_fence_identity_mismatch_never_settles(self) -> None:
        raw = self.request()
        accepted = self.service.accept(principal(), "gateway-predispatch-mismatch", raw)
        inbound_digest = hashlib.sha256(raw).hexdigest()
        self.service.request_cancel_by_key(
            principal(), "gateway-predispatch-mismatch", inbound_digest,
        )
        wrong_digest = self._cancel_fence(
            dispatch_key="gateway-predispatch-mismatch", digest="f" * 64,
        )
        wrong_key = self._cancel_fence(
            dispatch_key="gateway:gateway-predispatch-mismatch", digest=inbound_digest,
        )
        for answer in (wrong_digest, wrong_key):
            with self.subTest(answer=answer):
                self.hub.answers["cancel_by_key"] = [answer]
                self.service._one_step(self._row(accepted["id"]))
                held = self._row(accepted["id"])
                self.assertEqual(held["cancel_state"], "requested")
                self.assertEqual(held["cancel_outcome"], "send_unknown")
                self.assertIsNone(held["terminal_fence_receipt"])
                self.assertNotIn(
                    "terminal_proof", self.service.status(principal(), accepted["id"]),
                )

    def test_staging_cancel_lost_response_holds_and_replays_the_same_cancel_key(self) -> None:
        raw = self.request()
        accepted = self.service.accept(principal(), "gateway-predispatch-lost", raw)
        inbound_digest = hashlib.sha256(raw).hexdigest()
        self.service.request_cancel_by_key(
            principal(), "gateway-predispatch-lost", inbound_digest,
        )
        self.hub.answers["cancel_by_key"] = [TimeoutError("fake lost cancel response")]
        with self.assertRaises(TimeoutError):
            self.service._one_step(self._row(accepted["id"]))
        held = self._row(accepted["id"])
        self.assertEqual((held["cancel_state"], held["cancel_outcome"]),
                         ("requested", "send_unknown"))
        self.assertIsNone(held["terminal_fence_receipt"])

        restarted = FinalRenderService(
            self.store, self.assets, destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: True,
            profile_descriptor=PROFILE_RAW, profile_facts=self.service.profile_facts,
        )
        self.addCleanup(restarted.stop)
        self.hub.answers["cancel_by_key"] = [self._cancel_fence(
            dispatch_key="gateway-predispatch-lost", digest=inbound_digest,
        )]
        restarted._one_step(self._row(accepted["id"]))
        self.assertEqual(
            [entry[1][0][1] for entry in self.hub.calls if entry[0] == "cancel_by_key"],
            ["gateway-predispatch-lost", "gateway-predispatch-lost"],
        )
        self.assertEqual(self._row(accepted["id"])["cancel_state"], "cancelled")
        self.assertEqual(
            self.service.status(principal(), accepted["id"])["terminal_proof"]["kind"],
            "never_launched",
        )

    def test_post_acceptance_answers_bind_hub_id_attempt_and_typed_stop_evidence(self) -> None:
        accepted = self.service.accept(principal(), "gateway-stop-proof", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        attempt_id = "abcdefabcdefabcdefabcdefabcdefab"
        self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
        self.service._one_step(prepared)
        row = self._row(accepted["id"])
        self.hub.answers["status"] = [self._hub_job(
            row, job_id=hub_id, state="rendering", attempt_id=attempt_id,
        )]
        self.service._one_step(row)
        row = self._row(accepted["id"])

        invalid = (
            self._hub_job(row, job_id="77777777-7777-4777-8777-777777777777",
                          state="cancelled", attempt_id=attempt_id,
                          compute=False, stopped_evidence_digest="6" * 64),
            self._hub_job(row, job_id=hub_id, state="cancelled",
                          attempt_id="0123456789abcdef0123456789abcdef",
                          compute=False, stopped_evidence_digest="6" * 64),
            self._hub_job(row, job_id=hub_id, state="cancelled", attempt_id=attempt_id,
                          compute=True, stopped_evidence_digest="6" * 64),
            self._hub_job(row, job_id=hub_id, state="cancelled", attempt_id=attempt_id,
                          compute=False, stopped_evidence_digest=None),
        )
        for answer in invalid:
            with self.subTest(answer=answer):
                self.assert_code("HUB_RESPONSE_UNKNOWN", self.service._hub_job, row, answer)

        self.service.request_cancel(principal(), accepted["id"])
        stop_digest = "6" * 64
        self.hub.answers["cancel"] = [self._hub_job(
            self._row(accepted["id"]), job_id=hub_id, state="cancelled",
            attempt_id=attempt_id, compute=False, stopped_evidence_digest=stop_digest,
        )]
        self.service._one_step(self._row(accepted["id"]))
        public = self.service.status(principal(), accepted["id"])
        self.assertEqual(public["terminal_proof"], {
            "format": "studio.render.gateway-terminal-proof.v1",
            "kind": "launched_stopped",
            "gateway_job_id": accepted["id"],
            "gateway_attempt_key": "gateway-stop-proof",
            "hub_job_id": hub_id,
            "hub_attempt_id": attempt_id,
            "stopped_evidence_digest": stop_digest,
        })
        self.assert_code(
            "HUB_RESPONSE_UNKNOWN", self.service._hub_job,
            self._row(accepted["id"]),
            self._hub_job(
                self._row(accepted["id"]), job_id=hub_id, state="cancelled",
                attempt_id=attempt_id, compute=False, stopped_evidence_digest="7" * 64,
            ),
        )

    def test_before_acceptance_cancel_remains_a_distinct_never_launched_proof(self) -> None:
        raw = self.request(intent_id="never-launched-proof")
        digest = hashlib.sha256(raw).hexdigest()
        public = self.service.request_cancel_by_key(
            principal(), "gateway-never-launched-proof", digest,
        )
        self.assertEqual(public["state"], "cancelled_before_acceptance")
        self.assertEqual(public["terminal_proof"], {
            "format": "studio.render.gateway-terminal-proof.v1",
            "kind": "never_launched",
            "gateway_attempt_key": "gateway-never-launched-proof",
            "inbound_story_body_sha256": digest,
            "fence_receipt": public["fence_receipt"],
            "first_cancel_at": public["first_cancel_at"],
        })

    def test_public_key_matching_another_jobs_hub_key_creates_its_own_tombstone(self) -> None:
        raw = self.request()
        accepted = self.service.accept(principal(), "collision-source", raw)
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        colliding_public_key = prepared["dispatch_key"]
        self.assertEqual(colliding_public_key, "gateway:collision-source")

        tombstoned_raw = self.request(intent_id="collision-tombstone")
        digest = hashlib.sha256(tombstoned_raw).hexdigest()
        tombstone = self.service.request_cancel_by_key(
            principal(), colliding_public_key, digest,
        )
        self.assertEqual(tombstone["state"], "cancelled_before_acceptance")
        self.assertEqual(tombstone["expected_request_sha256"], digest)
        self.assertIsNone(self._row(accepted["id"])["cancel_state"])
        self.assert_code(
            "RENDER_CANCELLED_BEFORE_ACCEPTANCE", self.service.accept,
            principal(), colliding_public_key, tombstoned_raw,
        )

    def test_unconfigured_key_can_still_read_and_cancel_existing_lineage(self) -> None:
        accepted = self.service.accept(principal(), "gateway-revoked-lineage", self.request())
        held = FinalRenderService(
            self.store, self.assets, destination_selector=lambda _packet: self.destination,
            hub_client=self.hub, key_configured=lambda: False, profile_descriptor=PROFILE_RAW,
        )
        self.addCleanup(held.stop)
        self.assertEqual(held.status(principal(), accepted["id"])["id"], accepted["id"])
        self.assertEqual(
            held.request_cancel(principal(), accepted["id"])["gateway_job_id"],
            accepted["id"],
        )

    def test_verified_range_import_publishes_winner_before_hub_ack_and_serves_bounded_range(self) -> None:
        accepted = self.service.accept(principal(), "gateway-output", self.request())
        prepared = self._stage_inputs_and_prepare(accepted["id"])
        hub_id = "66666666-6666-4666-8666-666666666666"
        self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
        self.service._one_step(prepared)
        row = self._row(accepted["id"])
        output = b"fake-winning-output"
        output_sha = hashlib.sha256(output).hexdigest()
        attempt_id = "abcdefabcdefabcdefabcdefabcdefab"
        receipt = self._receipt(row, hub_job_id=hub_id, attempt_id=attempt_id, output=output)
        self.hub.answers["status"] = [self._hub_job(
            row, job_id=hub_id, state="output_ready", attempt_id=attempt_id,
            output_receipt=receipt,
        )]
        self.service._one_step(row)
        row = self._row(accepted["id"])
        self.hub.answers["output_range"] = [(
            output,
            {"content-range": f"bytes 0-{len(output) - 1}/{len(output)}",
             "etag": '"' + output_sha + '"',
             "x-content-sha256": hashlib.sha256(output).hexdigest(),
             "content-type": "video/mp4"},
        )]
        self.hub.before["output_range"] = (
            lambda _destination, _job_id, _offset, _length, if_match:
            self.assertEqual(if_match, '"' + output_sha + '"')
        )
        self.service._one_step(row)
        self.assertFalse(any(name == "ack" for name, _ in self.hub.calls))
        self.service._one_step(self._row(accepted["id"]))
        ready = self._row(accepted["id"])
        self.assertEqual(ready["state"], "output_ready")
        self.assertFalse(any(name == "ack" for name, _ in self.hub.calls))

        public = self.service.status(principal(), accepted["id"])
        gateway_receipt = public["output"]["receipt_utf8"].encode("utf-8")
        self.assertEqual(hashlib.sha256(gateway_receipt).hexdigest(),
                         public["output"]["receipt_digest"])
        receipt_value = json.loads(gateway_receipt)
        self.assertEqual(receipt_value["gateway_job_id"], accepted["id"])
        self.assertEqual(receipt_value["hub_job_id"], hub_id)
        self.assertNotEqual(receipt_value["gateway_job_id"], receipt_value["hub_job_id"])
        self.assertEqual(receipt_value["selected_machine"], self.destination.selected_machine)
        self.assertEqual(receipt_value["feature_losses"], [])

        self.hub.answers["ack"] = [{"ok": True, "job_id": hub_id,
                                      "receipt_digest": receipt["receipt_digest"]}]
        self.service._one_step(ready)
        self.assertEqual(len([call for call in self.hub.calls if call[0] == "ack"]), 1)
        lease = self.service.open_output(
            principal(), accepted["id"], "bytes=1-3", '"' + output_sha + '"'
        )
        with lease:
            self.assertEqual(b"".join(lease.chunks()), output[1:4])
        self.assert_code("OUTPUT_RANGE_REQUIRED", self.service.open_output,
                         principal(), accepted["id"], None, '"' + output_sha + '"')
        acknowledged = self.service.acknowledge_output(
            principal(), accepted["id"], public["output"]["receipt_digest"]
        )
        self.assertEqual(acknowledged, {
            "format": "studio.render.gateway-ack.v1", "id": accepted["id"],
            "acknowledged": True,
            "receipt_digest": public["output"]["receipt_digest"],
        })
        self.assertEqual(
            self.store.get_render_delivery(accepted["id"])["story_ack_digest"],
            public["output"]["receipt_digest"],
        )
        changed = {**receipt_value, "intent_id": "different-intent"}
        changed_raw = canonical(changed)
        with self.store._render_write():
            self.store._conn.execute(
                "UPDATE render_delivery SET gateway_receipt=?,gateway_receipt_digest=? "
                "WHERE render_id=?",
                (changed_raw, hashlib.sha256(changed_raw).hexdigest(), accepted["id"]),
            )
        self.assert_code(
            "PRIVATE_RENDER_UNAVAILABLE", self.service.status, principal(), accepted["id"]
        )

    def test_output_precondition_is_exact_and_rejected_before_reader_grant(self) -> None:
        accepted = self.service.accept(principal(), "gateway-output-precondition", self.request())
        output_manifest, output_handle = self.publish("output", b"x", "mp4")
        output_sha = output_manifest["sha256"]
        with self.store._render_write():
            self.store._conn.execute(
                "UPDATE render_staging SET state='output_ready' WHERE id=?", (accepted["id"],)
            )
            self.store._conn.execute(
                "UPDATE render_delivery SET output_asset_handle=?,remote_descriptor_json=? "
                "WHERE render_id=?",
                (output_handle, json.dumps({"output_sha256": output_sha}), accepted["id"]),
            )
        invalid = (None, output_sha, 'W/"' + output_sha + '"', '"' + "8" * 64 + '"', '"bad"')
        with patch.object(self.assets, "open_reader") as opened:
            for etag in invalid:
                with self.subTest(etag=etag):
                    error = self.assert_code(
                        "OUTPUT_PRECONDITION_FAILED", self.service.open_output,
                        principal(), accepted["id"], "bytes=0-0", etag,
                    )
                    self.assertEqual(error.status, 412)
            opened.assert_not_called()

    def test_hub_output_range_requires_if_match_and_exact_metadata(self) -> None:
        output = b"output"
        output_sha = hashlib.sha256(output).hexdigest()
        cases = (
            ("weak-etag", output, {"content-range": "bytes 0-5/6", "etag": f'W/"{output_sha}"',
                           "x-content-sha256": output_sha, "content-type": "video/mp4"}),
            ("wrong-range", output, {"content-range": "bytes 1-6/6", "etag": f'"{output_sha}"',
                             "x-content-sha256": output_sha, "content-type": "video/mp4"}),
            ("wrong-media", output, {"content-range": "bytes 0-5/6", "etag": f'"{output_sha}"',
                             "x-content-sha256": output_sha, "content-type": "text/plain"}),
            ("missing-media", output, {"content-range": "bytes 0-5/6", "etag": f'"{output_sha}"',
                               "x-content-sha256": output_sha}),
            ("wrong-length", b"short", {"content-range": "bytes 0-5/6",
                                         "etag": f'"{output_sha}"',
                                         "x-content-sha256": output_sha,
                                         "content-type": "video/mp4"}),
        )
        for index, (label, payload, headers) in enumerate(cases):
            with self.subTest(label=label):
                accepted = self.service.accept(
                    principal(), f"gateway-output-metadata-{index}",
                    self.request(intent_id=f"output-metadata-{index}"),
                )
                prepared = self._stage_inputs_and_prepare(accepted["id"])
                hub_id = f"66666666-6666-4666-8666-{index:012x}"
                attempt_id = f"{index + 1:032x}"
                self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
                self.service._one_step(prepared)
                row = self._row(accepted["id"])
                receipt = self._receipt(
                    row, hub_job_id=hub_id, attempt_id=attempt_id, output=output,
                )
                delivery = self.store.update_render_delivery(
                    row["id"], expected_revision=0, remote_state="output_ready",
                    remote_attempt_id=attempt_id, remote_descriptor=receipt,
                )
                self.hub.before["output_range"] = (
                    lambda _destination, _job_id, _offset, _length, if_match:
                    self.assertEqual(if_match, f'"{output_sha}"')
                )
                self.hub.answers["output_range"] = [(payload, headers)]
                self.assert_code(
                    "HUB_OUTPUT_UNKNOWN", self.service._import_output,
                    row, delivery, self.destination,
                )

    def test_output_receipt_refuses_wrong_lineage_tier_hash_features_and_changed_receipt(self) -> None:
        cases = (
            ("lineage", {"accepted_request_sha256": "9" * 64}),
            ("tier", {"selected_tier": "wrong-tier"}),
            ("hash", {"output_sha256": "8" * 64}),
            ("feature-loss", {"feature_losses": [""]}),
            ("receipt", {"receipt_digest": "7" * 64}),
        )
        for index, (label, mutation) in enumerate(cases):
            with self.subTest(label=label):
                accepted = self.service.accept(
                    principal(), f"gateway-output-negative-{index}",
                    self.request(intent_id=f"output-negative-{index}"),
                )
                prepared = self._stage_inputs_and_prepare(accepted["id"])
                hub_id = f"66666666-6666-4666-8666-{index:012x}"
                self.hub.answers["submit"] = [self._hub_job(prepared, job_id=hub_id)]
                self.service._one_step(prepared)
                row = self._row(accepted["id"])
                attempt_id = f"{index + 1:032x}"
                receipt = self._receipt(row, hub_job_id=hub_id, attempt_id=attempt_id,
                                        output=b"output", **{
                                            key: value for key, value in mutation.items()
                                            if key != "receipt_digest"
                                        })
                if "receipt_digest" in mutation:
                    receipt["receipt_digest"] = mutation["receipt_digest"]
                delivery = self.store.update_render_delivery(
                    row["id"], expected_revision=0, remote_state="output_ready",
                    remote_attempt_id=attempt_id, remote_descriptor=receipt,
                )
                if label == "hash":
                    self.hub.answers["output_range"] = [(
                        b"output",
                        {"content-range": "bytes 0-5/6",
                         "etag": '"' + hashlib.sha256(b"output").hexdigest() + '"',
                         "x-content-sha256": hashlib.sha256(b"output").hexdigest(),
                         "content-type": "video/mp4"},
                    )]
                self.assert_code(
                    "HUB_OUTPUT_UNKNOWN", self.service._import_output, row, delivery,
                    self.destination,
                )


class PhysicalRoutingTests(unittest.TestCase):
    @staticmethod
    def production_location() -> dict[str, object]:
        capability = {
            "hardware_class": "Apple M4", "profile_digest": hashlib.sha256(PROFILE_RAW).hexdigest(),
            "tools_digest": "4" * 64, "compiler_digest": "5" * 64,
            "qualification_digest": "6" * 64, "selected_tier": "software-h264",
            "exact_encoder": "libx264", "font_support": False,
            "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                       "min_available_memory_bytes": 7_900_000_000,
                       "output_limit_bytes": 1},
            "profile_execution_bounds": {"max_duration_sec": 1},
            "production_eligible": True, "production_enabled": True,
            "available_capacity": {"slots": 1, "slots_total": 1},
        }
        return {
            "id": "roster-controller-a", "local": False, "reachable": True,
            "ready": True, "drained": False, "paused_at": None,
            "site_id": "site-a", "controller_id": "parent-controller-a",
            "members_from": None, "members_stale_seconds": None,
            "machines": [{
                "machine": "node-controller-a", "reachable": True,
                "label": "M4 fixture", "chip": "Apple M4", "memory_gb": 16.0,
                "available_gb": 8.0, "disk_free_gb": 100.0, "current_job": None,
            }],
            "nodes": [{
                "id": "node-controller-a", "state": "reachable", "paused": False,
                "current_job": None, "on_previous_token": False,
                "workstation": None, "priority_order": ["video"],
            }],
            "workers": [{
                "machine": "node-controller-a", "physical_machine_id": "node-controller-a",
                "online": True, "ready": True, "busy": False, "drained": False,
                "maintenance": False, "quarantined": False,
                "supported_operations": ["video.final_render"], **capability,
            }],
        }

    def test_qualification_shape_is_exact_and_capacity_has_current_one_slot_semantics(self) -> None:
        projection = candidate_authority()["projection"]
        capability = {**projection, "available_capacity": {"slots": 1, "slots_total": 1}}
        self.assertEqual(
            fleet._render_candidate_shape(capability, qualification=True), capability
        )
        zero = copy.deepcopy(capability)
        zero["available_capacity"] = {"slots": 0, "slots_total": 1}
        self.assertEqual(fleet._render_candidate_shape(zero, qualification=True), zero)
        for changed in (
            {**capability, "extra": None},
            {key: value for key, value in capability.items() if key != "exact_encoder"},
            {**capability, "available_capacity": {"slots": 1, "slots_total": 2}},
            {**capability, "available_capacity": {"slots": 2, "slots_total": 1}},
        ):
            with self.subTest(keys=set(changed)):
                self.assertIsNone(fleet._render_candidate_shape(changed, qualification=True))

    def test_render_encode_fact_is_closed_optional_and_intersected(self) -> None:
        projection = candidate_authority()["projection"]
        capability = {**projection, "available_capacity": {"slots": 1, "slots_total": 1}}
        entry = {"container": "mp4", "requested_codec": "h264",
                 "requested_effort": "medium", "crf": 23,
                 "quality_cpu_preset": "medium", "bitrate_kbps": None}
        document = {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [entry]}
        with_encode = {**capability, "encode": document}
        self.assertEqual(
            fleet._render_candidate_shape(with_encode, qualification=True), with_encode)
        self.assertEqual(
            fleet._render_candidate_shape({**capability, "encode": None}, qualification=True),
            {**capability, "encode": None})
        # A zero CRF is inside the contract even though today's compiler grid never emits it.
        zero_crf = {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [{**entry, "crf": 0}]}
        self.assertEqual(
            fleet._render_candidate_shape(
                {**capability, "encode": zero_crf}, qualification=True),
            {**capability, "encode": zero_crf})

        production = {key: value for key, value in capability.items()
                      if key not in {"vector_id", "qualification_only"}}
        production.update(production_eligible=True, production_enabled=True)
        self.assertEqual(
            fleet._render_candidate_shape(
                {**production, "encode": document}, qualification=False),
            {**production, "encode": document})

        malformed = (
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": "no"},
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [{**entry, "extra": 1}]},
            {"format": fleet.RENDER_ENCODE_FORMAT,
             "entries": [{**entry, "requested_effort": "turbo"}]},
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [{**entry, "container": {}}]},
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [{**entry, "crf": -1}]},
            {"format": fleet.RENDER_ENCODE_FORMAT,
             "entries": [{**entry, "crf": 2 ** 53}]},
            {"format": fleet.RENDER_ENCODE_FORMAT,
             "entries": [{**entry, "bitrate_kbps": 0}]},
            {"format": "studio.render.encode-authority.v2", "entries": [entry]},
            {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [entry], "extra": True},
            ["not", "a", "document"],
        )
        for bad in malformed:
            with self.subTest(encode=bad):
                self.assertIsNone(fleet._render_candidate_shape(
                    {**production, "encode": bad}, qualification=False))

        superset = {**entry, "requested_effort": "slow"}
        self.assertEqual(fleet.render_encode_intersection([
            {"capability": {"encode": document}},
            {"capability": {"encode": {
                "format": fleet.RENDER_ENCODE_FORMAT, "entries": [entry, superset]}}},
        ]), {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [entry]})
        self.assertEqual(fleet.render_encode_intersection([
            {"capability": {"encode": document}},
            {"capability": {"encode": {
                "format": fleet.RENDER_ENCODE_FORMAT, "entries": [superset]}}},
        ]), {"format": fleet.RENDER_ENCODE_FORMAT, "entries": []})
        for unknown in ([], [{"available_slots": 1}],
                        [{"capability": {"encode": None}}],
                        [{"capability": {"encode": {
                            "format": "studio.render.encode-authority.v2", "entries": []}}}]):
            with self.subTest(candidates=unknown):
                self.assertIsNone(fleet.render_encode_intersection(unknown))

    def test_literal_fresh_overview_node_worker_shapes_select_only_complete_joined_leaf(self) -> None:
        now = 1_700_000_000.0
        location = self.production_location()
        with patch.object(fleet.time, "time", return_value=now), \
                patch.object(fleet, "snapshot", return_value={
                    "generated_at": now, "locations": [location],
                }), patch.object(fleet, "roster", return_value=[{
                    "id": "roster-controller-a",
                }]):
            selected = fleet.render_candidates(hashlib.sha256(PROFILE_RAW).hexdigest())
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["hub_controller_id"], "parent-controller-a")
        self.assertEqual(selected[0]["target_hub_controller_id"], "node-controller-a")
        self.assertEqual(selected[0]["available_memory_source"], {
            "field": "available_gb", "unit_bytes": 1_000_000_000,
            "decimal_places": 2, "conservative_bytes": 7_995_000_000,
        })

        mutations = (
            lambda row: row["machines"][0].update(available_gb=None),
            lambda row: row["machines"][0].update(available_gb=7.90),
            lambda row: row["machines"][0].update(disk_free_gb=None),
            lambda row: row["machines"][0].update(current_job={"job_id": "busy"}),
            lambda row: row["nodes"][0].pop("state"),
            lambda row: row["nodes"][0].pop("paused"),
            lambda row: row["nodes"][0].pop("current_job"),
            lambda row: row["nodes"][0].update(paused=True),
            lambda row: row["nodes"][0].update(state="unreachable"),
            lambda row: row["nodes"][0].update(current_job={"job_id": "busy"}),
            lambda row: row["workers"][0].pop("online"),
            lambda row: row["workers"][0].update(ready=None),
            lambda row: row["workers"][0].update(busy=None),
            lambda row: row["workers"][0].update(online=False),
            lambda row: row["workers"][0].update(ready=False),
            lambda row: row["workers"][0].update(busy=True),
            lambda row: row["workers"][0].pop("drained"),
            lambda row: row["workers"][0].pop("maintenance"),
            lambda row: row["workers"][0].pop("quarantined"),
            lambda row: row["workers"][0].update(drained=True),
            lambda row: row["workers"][0].update(maintenance=True),
            lambda row: row["workers"][0].update(quarantined=True),
            lambda row: row.update(nodes=[]),
        )
        for index, mutate in enumerate(mutations):
            candidate = copy.deepcopy(location)
            mutate(candidate)
            with self.subTest(index=index), \
                    patch.object(fleet.time, "time", return_value=now), \
                    patch.object(fleet, "snapshot", return_value={
                        "generated_at": now, "locations": [candidate],
                    }), patch.object(fleet, "roster", return_value=[{
                        "id": "roster-controller-a",
                    }]):
                self.assertEqual(
                    fleet.render_candidates(hashlib.sha256(PROFILE_RAW).hexdigest()), []
                )

        for pressure in (None, "critical", "warning", 0):
            candidate = copy.deepcopy(location)
            candidate["machines"][0]["memory_pressure"] = pressure
            with self.subTest(memory_pressure=pressure), \
                    patch.object(fleet.time, "time", return_value=now), \
                    patch.object(fleet, "snapshot", return_value={
                        "generated_at": now, "locations": [candidate],
                    }), patch.object(fleet, "roster", return_value=[{
                        "id": "roster-controller-a",
                    }]):
                self.assertEqual(
                    fleet.render_candidates(hashlib.sha256(PROFILE_RAW).hexdigest()), []
                )

    def test_stale_snapshot_and_controller_only_machine_never_report_capacity(self) -> None:
        now = 1_700_000_000.0
        location = self.production_location()
        controller_only = copy.deepcopy(location)
        controller_only["machines"][0]["machine"] = "parent-controller-a"
        controller_only["workers"][0]["machine"] = "parent-controller-a"
        controller_only["workers"][0]["physical_machine_id"] = "parent-controller-a"
        controller_only["nodes"] = []
        for row, generated_at in ((location, now - 3600), (controller_only, now)):
            with patch.object(fleet.time, "time", return_value=now), \
                    patch.object(fleet, "snapshot", return_value={
                        "generated_at": generated_at, "locations": [row],
                    }), patch.object(fleet, "roster", return_value=[{
                        "id": "roster-controller-a",
                    }]):
                self.assertEqual(
                    fleet.render_candidates(hashlib.sha256(PROFILE_RAW).hexdigest()), []
                )

    def test_empty_model_catalogue_does_not_hide_exact_m4_m2_m1_qualification_capacity(self) -> None:
        profile_digest = hashlib.sha256(PROFILE_RAW).hexdigest()

        def location(index: int, hardware: str) -> dict[str, object]:
            machine = f"node-{hardware.lower()}"
            candidate = {
                "vector_id": INTENT_ID, "hardware_class": hardware, "profile_digest": profile_digest,
                "tools_digest": "4" * 64, "compiler_digest": "5" * 64,
                "qualification_digest": "6" * 64, "selected_tier": "software-h264",
                "exact_encoder": "libx264", "font_support": False,
                "bounds": {"max_rss_bytes": 1, "max_wall_seconds": 1,
                           "min_available_memory_bytes": 1, "output_limit_bytes": 1},
                "profile_execution_bounds": {"max_duration_sec": 1},
                "production_eligible": False, "qualification_only": True,
                "available_capacity": {"slots": 1, "slots_total": 1},
            }
            return {
                "id": f"controller-{index}", "local": False, "reachable": True,
                "ready": True, "drained": False, "site_id": f"site-{index}",
                "controller_id": f"hub-{index}", "members_from": None,
                "members_stale_seconds": None, "paused_at": None,
                "local_models": [],
                "machines": [{"machine": machine, "reachable": True,
                              "label": hardware + " fixture", "chip": "Apple " + hardware,
                              "memory_gb": 16, "available_gb": 8.0,
                              "disk_free_gb": 100.0, "current_job": None}],
                "nodes": [{"id": machine, "state": "reachable", "paused": False,
                           "current_job": None, "on_previous_token": False,
                           "priority_order": ["video"], "workstation": None}],
                "workers": [{"machine": machine, "physical_machine_id": machine,
                             "online": True, "ready": True, "busy": False,
                             "drained": False, "maintenance": False, "quarantined": False,
                             "supported_operations": ["video.final_render"],
                             "production_enabled": False, "production_eligible": False,
                             "qualification_candidates": [candidate]}],
            }

        rows = [location(1, "Apple M1"), location(2, "Apple M4"), location(3, "Apple M2")]
        now = 1_700_000_000.0
        with patch.object(fleet.time, "time", return_value=now), \
                patch.object(fleet, "snapshot", return_value={
                    "generated_at": now, "locations": rows,
                }), \
                patch.object(fleet, "roster", return_value=[{"id": row["id"]} for row in rows]):
            production_only = fleet.render_candidates(profile_digest)
            selected = fleet.render_candidates(profile_digest, qualification=True)
        self.assertEqual(production_only, [])
        self.assertEqual([row["hardware_class"] for row in selected],
                         ["Apple M4", "Apple M2", "Apple M1"])
        self.assertEqual({row["qualification_vector_id"] for row in selected}, {INTENT_ID})


async def _pieces(payload: bytes, width: int = 17):
    for offset in range(0, len(payload), width):
        yield payload[offset:offset + width]


class BlobTests(RenderServiceCase):
    def test_status_is_read_only_and_streaming_put_resumes_without_cross_scope_disclosure(self) -> None:
        payload = b"0123456789abcdef" * (65536 * 6 // 16)
        digest = hashlib.sha256(payload).hexdigest()
        missing = self.service.blob_status(principal(), digest, len(payload))
        self.assertEqual(missing, {
            "format": "studio.render.gateway-blob.v1", "sha256": digest,
            "size_bytes": len(payload), "state": "missing", "offset": 0,
        })
        self.assertEqual(tuple((self.case_root / "assets" / "uploads").iterdir()), ())

        first = payload[:65536]
        partial = asyncio.run(
            self.service.put_blob(principal(), digest, len(payload), 0, len(first), _pieces(first))
        )
        self.assertEqual(partial["offset"], len(first))
        finished = asyncio.run(
            self.service.put_blob(
                principal(), digest, len(payload), len(first), len(payload) - len(first),
                _pieces(payload[len(first):]),
            )
        )
        self.assertEqual((finished["state"], finished["offset"]), ("complete", len(payload)))

        other_scope = "77777777-7777-4777-8777-777777777777"
        self.assertEqual(
            self.service.blob_status(principal(owner_scope=other_scope), digest, len(payload)),
            {"format": "studio.render.gateway-blob.v1", "sha256": digest,
             "size_bytes": len(payload), "state": "missing", "offset": 0},
        )

    def test_put_refuses_declared_range_before_consuming_body(self) -> None:
        consumed = False

        async def body():
            nonlocal consumed
            consumed = True
            yield b"never-consumed"

        with self.assertRaises(RenderServiceError) as caught:
            asyncio.run(
                self.service.put_blob(principal(), "c" * 64, 10, 11, 1, body())
            )
        self.assertEqual(caught.exception.code, "BLOB_RANGE_INVALID")
        self.assertFalse(consumed)


if __name__ == "__main__":
    unittest.main()
