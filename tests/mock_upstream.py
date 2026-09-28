#!/usr/bin/env python3
"""Mock OpenAI-compatible upstream, for verifying gateway-kh without spending money.

Serves just enough of the OpenAI surface for the gateway's
`openai_compatible` adapter: model discovery, chat, image, speech.

It also implements `mock-fail`, which always returns 429, so the error-mapping
path can be tested -- specifically that a drained provider account becomes a
settled failure and never a 402 (a 402 would permanently lock Story Studio's
API key).

Usage: python tests/mock_upstream.py --port 8899
"""

from __future__ import annotations

import argparse
import base64
import email
import json
import io
import socket
import socketserver
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A genuine 1x1 PNG. Story Studio signature-checks image bytes at 1..4, so the
# mock must return a real PNG for the end-to-end check to mean anything.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: What the gateway pushes to a controller when a voice is shared. Recorded here
#: so the isolated suite can assert the fan-out really happened, that it used the
#: **Hub** token rather than the fleet one, and that the wire shape is the one the
#: Hub's shared-voice route wants. Reset by `POST /__test__/reset`.
SHARED_VOICES: list[dict] = []
SHARED_TOKENS: list[str] = []

#: Every `X-Hub-Token` this mock was sent, per Hub-shaped path.
#:
#: The operator-token leak is a **wire** fact. A controller may legitimately hold
#: no credential, and the bug was that the gateway then put *this Mac's own
#: operator token* in `X-Hub-Token` and sent it to the remote address the owner
#: had typed. Asserting the helper's return value only proves the helper is
#: right; asserting here proves nothing on the wire carried it, however the header
#: was built. Reset by `POST /__test__/reset`.
HUB_TOKENS: list[dict] = []

#: Every call this mock received on the Hub's **chat peer route**, path plus the
#: body it carried.
#:
#: The lane's contract is a *wire* fact: which route the gateway posts to (the
#: Hub's peer surface, not a worker's own and not the Hub-token-only spelling),
#: whether it asks for a stream it cannot consume, and whether it sends a
#: parameter the Hub would silently drop. Asserting the adapter's return value
#: would prove none of that. Reset by `POST /__test__/reset`.
CHAT_CALLS: list[dict] = []

#: Knobs for the chat lane's failure shapes, so a test can ask for one without a
#: second server: a status to answer with, a `code` to refuse under (the Hub's own
#: vocabulary, e.g. `MODEL_NOT_PACKAGED`), a sentence for the body, and whether to
#: answer 200 with an empty choice. Reset by `POST /__test__/reset`.
CHAT_STATUS: int = 200
CHAT_REFUSAL_CODE: str | None = None
CHAT_DETAIL: str = ""
CHAT_EMPTY: bool = False

#: Every body this mock was sent on Kie's `jobs/createTask`, in order.
#:
#: Music and images share that route, so the *body* is the only thing that says
#: which lane a request is on: the top-level `model` is the api-model
#: (`ai-music-api/generate` for music) and the tier rides in `input.model`. A
#: recording rather than a callback because the assertion is about the wire.
#: Reset by `POST /__test__/reset`.
KIE_TASKS: list[dict] = []

#: A minimal MPEG audio frame: a valid 0xFFFB header followed by silence. Enough
#: for the artifact route to serve as `audio/mpeg`; the lane check does not sniff
#: audio the way the image and video checks sniff their magic bytes.
MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 2048

#: A second track, byte-for-byte different from `MP3`. Suno answers one entry per
#: variation, so the multi-artifact checks need two results that are genuinely
#: distinct — identical bytes would let "both were kept" pass on a bug that stored
#: the first one twice.
MP3_ALT = b"\xff\xfb\x90\x00" + b"\x11" * 2048


def one_pixel_png(rgb: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """A valid 1x1 PNG of one colour.

    Built rather than pasted so a multi-image response can hand back several
    images that really do differ. Returns a real PNG (correct CRC per chunk), so a
    test asserting the bytes are an image is testing an image.
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes((0, *rgb))))
        + chunk(b"IEND", b"")
    )

MODELS = [
    # The mock states the fields the catalogue's "publish what is known, and
    # nothing else" checks actually inspect, in the spellings a real provider
    # uses: `context_length` and `top_provider.max_completion_tokens` are
    # OpenRouter's, and `architecture.input_modalities` is how it says a model
    # takes images. With every field absent — which is how this list started —
    # those checks passed without reading a single value: a model that states
    # nothing is trivially consistent with itself, and the hardcoding they exist
    # to catch (`supports_vision: false`, one shared `max_output_tokens`) would
    # have reintroduced `input_modalities: ["text"]`, which *agrees*.
    {
        "id": "mock-chat-1", "name": "Mock Chat One", "context_length": 32000,
        "top_provider": {"max_completion_tokens": 8192},
        "architecture": {"input_modalities": ["text"]},
    },
    {
        "id": "mock-chat-2", "name": "Mock Chat Two", "context_length": 128000,
        "top_provider": {"max_completion_tokens": 16384},
        "architecture": {"input_modalities": ["text", "image"]},
    },
    # States nothing at all: the half of the rule that says an unstated value
    # arrives as null rather than as a plausible default.
    {"id": "mock-chat-bare", "name": "Mock Chat Bare"},
    # Also states nothing, but the owner's curated file has a value for it, so
    # this is the row that proves a gap gets filled rather than merely that the
    # gap-filling function works in isolation.
    {"id": "mock-chat-plain", "name": "Mock Chat Plain"},
    {"id": "mock-image-1", "name": "Mock Image One"},
    # States its own character limit, which must beat the 25,000 default.
    {"id": "mock-tts-1", "name": "Mock TTS One",
     "maximum_text_length_per_request": 5000},
    # States nothing, so the owner's curated file is the only source. The two
    # together are what prove the precedence order rather than just asserting it.
    {"id": "mock-tts-plain", "name": "Mock TTS Plain"},
    {"id": "mock-whisper-1", "name": "Mock Whisper One"},
    {"id": "mock-fail", "name": "Mock Failing Model"},
]


# --------------------------------------------------------------------------
# The Fleet surface: two machines, two modalities, one row per (model, machine).
#
# `/api/hub/models` is what a controller answers with one row per machine per
# model, so the Fleet page's Models tab can show per-worker supply. Three models
# between them state the shapes the gateway has to survive:
#
#   mock-hub/vision-one   available on mac-a and not mac-b, with a `fit` verdict
#   mock-hub/voice-one    `hub_catalog_error` on **both** machines, so the issue
#                         aggregation can be shown to report it once
#   mock-hub/voice-two    downloading on mac-b
#
# `mac-a`/`mac-b` are also the two physical machines the capabilities workers
# and `fleet/nodes` roster name.
FLEET_MODEL_ROWS = [
    {
        "repo": "mock-hub/vision-one", "label": "Vision One", "family": "mock-vision",
        "hub_studio": "image", "hub_modality": "image", "hub_machine": "mac-a",
        "kind": "mlx", "size_gb": 4.2, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 6, "memory_eligible": True, "runtime_ready": True,
        "available": True, "loaded": False,
        "fit": {"state": "fits", "label": "Fits", "hint": "",
                "actual_gb": 4.2, "required_gb": 6},
        "cache": {"state": "ready", "package_id": "vision-one-pkg"},
        "hub_catalog_stale": False, "hub_catalog_error": None,
        "active_download": None, "gated": False,
    },
    {
        "repo": "mock-hub/vision-one", "label": "Vision One", "family": "mock-vision",
        "hub_studio": "image", "hub_modality": "image", "hub_machine": "mac-b",
        "kind": "mlx", "size_gb": 4.2, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 6, "memory_eligible": True, "runtime_ready": True,
        "available": False, "loaded": False,
        "fit": None,
        "cache": {"state": "ready", "package_id": "vision-one-pkg"},
        "hub_catalog_stale": False, "hub_catalog_error": None,
        "active_download": None, "gated": False,
    },
    {
        "repo": "mock-hub/voice-one", "label": "Voice One", "family": "mock-voice",
        "hub_studio": "voice", "hub_modality": "voice", "hub_machine": "mac-a",
        "kind": "mlx", "size_gb": 2.0, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 4, "memory_eligible": True, "runtime_ready": True,
        "available": True, "loaded": True,
        "fit": None,
        "cache": {"state": "ready", "package_id": "voice-one-pkg"},
        "hub_catalog_stale": False, "hub_catalog_error": "catalogue read failed",
        "active_download": None, "gated": False,
    },
    {
        "repo": "mock-hub/voice-one", "label": "Voice One", "family": "mock-voice",
        "hub_studio": "voice", "hub_modality": "voice", "hub_machine": "mac-b",
        "kind": "mlx", "size_gb": 2.0, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 4, "memory_eligible": True, "runtime_ready": True,
        "available": False, "loaded": False,
        "fit": None,
        "cache": {"state": "ready", "package_id": "voice-one-pkg"},
        "hub_catalog_stale": False, "hub_catalog_error": "catalogue read failed",
        "active_download": None, "gated": False,
    },
    {
        "repo": "mock-hub/voice-two", "label": "Voice Two", "family": "mock-voice",
        "hub_studio": "voice", "hub_modality": "voice", "hub_machine": "mac-a",
        "kind": "mlx", "size_gb": 1.5, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 3, "memory_eligible": True, "runtime_ready": False,
        "available": False, "loaded": False, "fit": None,
        # No `package_id`: this row's dispatch state is genuinely unknown, so the
        # page must say `None` rather than guess one.
        "cache": {"state": "not_installed"},
        "hub_catalog_stale": False, "hub_catalog_error": None,
        "active_download": None, "gated": True,
    },
    {
        "repo": "mock-hub/voice-two", "label": "Voice Two", "family": "mock-voice",
        "hub_studio": "voice", "hub_modality": "voice", "hub_machine": "mac-b",
        "kind": "mlx", "size_gb": 1.5, "min_unified_memory_gb": 8,
        "required_free_memory_gb": 3, "memory_eligible": True, "runtime_ready": False,
        "available": False, "loaded": False, "fit": None,
        "cache": {"state": "downloading"},
        "hub_catalog_stale": False, "hub_catalog_error": None,
        "active_download": {"percent": 40}, "gated": False,
    },
]

#: `GET /api/hub/capabilities`'s rich worker roster. Four workers across two
#: machines and two modalities, deliberately covering each state the issues list
#: derives: one offline, one in maintenance, one quarantined, one healthy.
FLEET_WORKERS = [
    {"service_id": "svc-image-a", "studio_type": "image", "modality": "image",
     "physical_machine_id": "mac-a", "online": True, "ready": True, "busy": False,
     "physical_machine_busy": False, "drained": False, "maintenance": False,
     "machine_quarantined": False, "last_seen_at": 1.0,
     "available_capacity": {"slots": 1, "slots_total": 2,
                            "shared_by_physical_machine": False}},
    {"service_id": "svc-image-b", "studio_type": "image", "modality": "image",
     "physical_machine_id": "mac-b", "online": False, "ready": False, "busy": False,
     "drained": False, "maintenance": False, "machine_quarantined": False,
     "last_seen_at": 0.5,
     "available_capacity": {"slots": 0, "slots_total": 2}},
    {"service_id": "svc-voice-a", "studio_type": "voice", "modality": "voice",
     "physical_machine_id": "mac-a", "online": True, "ready": True, "busy": False,
     "drained": False, "maintenance": True, "machine_quarantined": False,
     "last_seen_at": 1.0,
     "available_capacity": {"slots": 1, "slots_total": 1}},
    {"service_id": "svc-voice-b", "studio_type": "voice", "modality": "voice",
     "physical_machine_id": "mac-b", "online": True, "ready": False, "busy": False,
     "drained": False, "maintenance": False, "machine_quarantined": True,
     "last_seen_at": 0.75,
     "available_capacity": {"slots": 0, "slots_total": 1}},
]

#: The fallback roster: a node names its studios, and `state` is the only
#: reachability statement a node makes.
#:
#: **And the shape the real Hub publishes**, which is the part that matters for the
#: Fleet page: `fleet_nodes.probe` (0.6.91) gives every row its join `role`, its
#: `address`, `joined_at`, `last_seen_at`, `credential_issued_at` and
#: `has_credential`, plus its own `detail` sentence when it cannot be reached and
#: the `host` facts it answered with. Three states are deliberately present, because
#: two of them are the ones the page had no way to tell apart: a Mac that is off
#: (`unreachable`) and a Mac that **answered and refused this controller**
#: (`unauthorized`, with the Hub's own sentence naming the fix). `mac-c` is the
#: credential case.
FLEET_NODES = [
    {"id": "mac-a", "name": "Mac A", "state": "reachable", "role": "agent",
     "address": "http://10.0.0.11:47873", "joined_at": 1.0, "last_seen_at": 2.0,
     "credential_issued_at": 1.0, "has_credential": True, "detail": None,
     "version": "0.6.91", "host": {"chip": "Apple M4", "memory_gb": 16.0},
     "studios": [{"id": "image", "modality": "image"},
                  {"id": "voice", "modality": "voice"}]},
    {"id": "mac-b", "name": "Mac B", "state": "unreachable", "role": "agent",
     "address": "http://10.0.0.12:47873", "joined_at": 1.0, "last_seen_at": 0.5,
     "credential_issued_at": 1.0, "has_credential": True,
     "detail": "could not reach the node (unreachable)", "version": None, "host": {},
     "studios": [{"id": "image", "modality": "image"}]},
    {"id": "mac-c", "name": "Mac C", "state": "unauthorized", "role": "agent",
     "address": "http://10.0.0.13:47873", "joined_at": 1.0, "last_seen_at": 0.75,
     "credential_issued_at": 1.0, "has_credential": True,
     "detail": "the node answered but rejected this controller's credential (HTTP 401)"
               " - re-join it with the current Fleet Token",
     "version": None, "host": {},
     "studios": [{"id": "image", "modality": "image"}]},
]


def fleet_node_summary(nodes: list[dict]) -> dict[str, int]:
    """The Hub's own summary arithmetic, computed rather than written down.

    `fleet_nodes.probe` counts each state, and `removed` from its eviction record; a
    literal here would agree with `FLEET_NODES` only until the next node is added,
    which is how a mock starts asserting last week's shape.
    """
    return {
        "total": len(nodes),
        "reachable": sum(node.get("state") == "reachable" for node in nodes),
        "unreachable": sum(node.get("state") == "unreachable" for node in nodes),
        "unauthorized": sum(node.get("state") == "unauthorized" for node in nodes),
        "error": sum(node.get("state") == "error" for node in nodes),
        "removed": 0,
    }

#: **The real Hub's numbers, named, because the real Hub moves them.**
#:
#: On 2026-09-26 Studio Hub raised `FALLBACK_MAX_OUTPUT_TOKENS` `4096 → 16384` and
#: made the deadline **per lane** (`PEER_PROXY_TIMEOUT_S` was one flat 900 s):
#: chat 1,800 s; image, voice, transcription and music 900 s; video 7,200 s. This mock
#: now models the **new** Hub, deliberately before their release is cut, so anything of
#: ours that breaks on the change breaks here first.
#:
#: **Named rather than written inline, and that is the whole point.** Studio Hub's own
#: suite had **eight chat tests go red** on the change: several asserted the bare
#: literal `4096`, and several fixtures had been hand-tuned to sit *just above* the old
#: fallback — one carrying the comment *"8192, not 4096: the request below asks for the
#: worker's default output budget"*. At 16,384 those fixtures **silently inverted**:
#: the fixture became the binding constraint instead of the fallback. A number written
#: as a literal, or sized relative to somebody else's constant, is a number that turns
#: into a lie without anything going red. Change these in one place; let the tests read
#: them.
HUB_CHAT_MAX_OUTPUT_TOKENS = 16_384
HUB_CHAT_CONTEXT_TOKENS = 262_144
HUB_CHAT_DEADLINE_S = 1800.0
HUB_VOICE_DEADLINE_S = 900.0
#: The Hub's own per-modality floor, and the fixture reads it rather than restating 7,200: a
#: video lane's deadline is a Hub number, and a test that hardcoded it would pass the day the
#: Hub changed it.
HUB_VIDEO_DEADLINE_S = 7200.0

#: `dispatch_state`, joined by the `package_id` a model's `cache` names. The
#: engine is deliberately one `_HUB_ENGINE_MODALITY` does not know, so the voice
#: discovery path keeps seeing exactly the five packages `test_e2e` asserts while
#: these rows are available for the Fleet page's join.
FLEET_PACKAGES = [
    {"package_id": "vision-one-pkg", "state": "ready", "files_state": "files_verified",
     "runtime_state": "ready", "os_state": "ready", "chip_state": "ready",
     "dispatch_state": "eligible", "engine": "fleet-mock",
     "minimum_macos": "14.0", "verification": "ok"},
    {"package_id": "voice-one-pkg", "state": "ready", "files_state": "files_verified",
     "runtime_state": "ready", "os_state": "ready", "chip_state": "ready",
     "dispatch_state": "ineligible", "engine": "fleet-mock",
     "minimum_macos": "14.0", "verification": "ok"},
]


def _wav(seconds: float = 0.25, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 on purpose. With HTTP/1.1 keep-alive, a client that abandons a
    # connection (a timed-out discovery request, say) can leave a handler
    # thread mid-stream and later requests on that socket hang. For a test
    # double, closing per request is strictly more reliable.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # keep test output readable
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status: int, content: bytes, media_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def _raw(self) -> bytes:
        """The request body, read **once**.

        `_body()` and `_form()` both need it, and reading `rfile` twice is a
        deadlock rather than an error: the first read drains the socket and the
        second blocks forever waiting for bytes that will never come. That is
        exactly how the shared-voice fan-out test first hung — `do_POST` parsed
        JSON (getting `{}` from the multipart body) and then `_form()` waited for
        the same bytes again.
        """
        if not hasattr(self, "_raw_cache"):
            length = int(self.headers.get("Content-Length") or 0)
            self._raw_cache = self.rfile.read(length) if length else b""
        return self._raw_cache

    def _body(self) -> dict:
        raw = self._raw()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        port = self.server.server_address[1]
        self._record_hub_token(path)

        # ---- the shared-voice fan-out, as a controller's Hub serves it.
        #
        # This exists so the *whole* sharing path can be exercised without writing
        # anything to the owner's real Hub: the permission gate, the two-token
        # distinction, the controllers-only push, and the wire shape. A stray voice
        # on his live Hub is a side effect a test has no business causing.
        if path == "/__test__/shared-voices":
            self._json(200, {"voices": SHARED_VOICES, "tokens": SHARED_TOKENS})
            return
        if path == "/__test__/hub-tokens":
            self._json(200, {"tokens": HUB_TOKENS})
            return
        # ---- Kie's unified task route, as a music task answers it.
        #
        # `createTask` is the *same* route Kie's images and videos use; the music
        # lane differs only in the api-model name and the `resultJson` shape. This
        # mock exists so the whole lane can run without spending the owner's
        # credits — the real one was measured once by hand (12 credits) and that
        # measurement is what these recorded bodies are asserted against.
        if path == "/__test__/kie-tasks":
            self._json(200, {"tasks": KIE_TASKS})
            return
        if path == "/api/v1/jobs/recordInfo":
            # A finished task, in the shape the live API returned: `state` and
            # `status`, with `resultJson` a JSON **string** holding a `data` array
            # of tracks (not the `resultUrls` keys the image/video lanes use).
            track = f"http://127.0.0.1:{port}/kie-track.mp3"
            track_two = f"http://127.0.0.1:{port}/kie-track-2.mp3"
            self._json(200, {"code": 200, "msg": "success", "data": {
                "taskId": (self.path.split("taskId=")[-1] if "taskId=" in self.path else "mock"),
                "model": "ai-music-api/generate",
                "state": "success",
                "status": "SUCCESS",
                "creditsConsumed": 12.0,
                "resultJson": json.dumps({"code": 200, "data": [
                    {"audio_url": track, "stream_audio_url": track,
                     "image_url": f"http://127.0.0.1:{port}/kie-cover.jpeg",
                     "id": "mock-track", "duration": 164.8,
                     "model_name": "chirp-hawk", "prompt": "", "title": ""},
                    # A second variation, which is what Suno really answers. The
                    # gateway used to download `data[0]` only.
                    {"audio_url": track_two, "stream_audio_url": track_two,
                     "image_url": f"http://127.0.0.1:{port}/kie-cover.jpeg",
                     "id": "mock-track-2", "duration": 171.2,
                     "model_name": "chirp-hawk", "prompt": "", "title": ""},
                ]}),
            }})
            return
        if path == "/kie-track.mp3":
            self._bytes(200, MP3, "audio/mpeg")
            return
        if path == "/kie-track-2.mp3":
            self._bytes(200, MP3_ALT, "audio/mpeg")
            return
        if path == "/api/hub/shared-voices":
            self._json(200, {"voices": SHARED_VOICES})
            return
        # The routes `fleet._probe` reads, so a location pointed at this mock can
        # be *reachable* rather than erroring — the fan-out skips an unreachable
        # location before it ever looks at its token, which would make the token
        # assertion vacuous.
        if path == "/api/hub/fleet/overview":
            self._json(200, {"machines": [], "summary": {"machines": 0}})
            return
        if path == "/api/hub/fleet/nodes":
            self._json(200, {"nodes": FLEET_NODES,
                             "summary": fleet_node_summary(FLEET_NODES)})
            return
        if path == "/api/hub/capabilities":
            # The rich roster `fleet._worker_roster` prefers. `/api/hub/workers`
            # below is read too, but its rows carry no studio or physical-machine
            # identity, so it is not the roster the page is shown.
            self._json(200, {
                "controller": {"role": "controller", "ready": True},
                "site_id": "mock-site",
                "workers": FLEET_WORKERS,
            })
            return
        if path == "/api/hub/stats":
            self._json(200, {"fleet_activity": {}})
            return
        if path == "/api/hub/jobs":
            self._json(200, {"batches": []})
            return
        if path == "/api/hub/models":
            self._json(200, {
                "models": FLEET_MODEL_ROWS, "per_studio": {},
                "total": len(FLEET_MODEL_ROWS),
            })
            return
        if path == "/api/hub/workers":
            self._json(200, {"workers": [
                {"id": worker["service_id"], "title": worker["service_id"],
                 "modality": worker["modality"], "machine": worker["physical_machine_id"],
                 "runtime_identity": {}}
                for worker in FLEET_WORKERS
            ]})
            return

        # ---- what a location's models *are*, for the local voice lane.
        #
        # `model-packages` says what is installed; `catalog` says what each model
        # is — whether it clones at all (a preset-speaker tier does not) and what
        # reference contract it declares. Both are served here so the voice
        # matching, the per-model reference report and the no-invented-numbers
        # rule can be asserted without a real Hub.
        #
        # Five deliberate shapes:
        #   mock-hub/clone-model        catalogued, audited contract (3/8/15s)
        #   mock-hub/preset-model       catalogued, `declared: false` — no reference
        #   mock-hub/uncatalogued-model installed but absent from the catalogue
        #                               → *unverified* for its voices, and not a
        #                                 model at all as far as publication goes
        #   mock-hub/toobig-model       catalogued, but the Hub says it does not fit
        #                               this machine (`memory_eligible: false`)
        #   mock-hub/capability-only-model
        #                               catalogued, **no `reference_audio` block**
        #                               and no `voice-cloning` in `capabilities` —
        #                               the only statement available is the
        #                               capability list, so it does not clone. The
        #                               catalogue's published flag and the voice
        #                               picker must give the same answer; they used
        #                               to be two implementations of one rule.
        if path == "/api/hub/model-packages":
            self._json(200, {"models": [
                {"package_id": "clone-model", "model_id": "mock-hub/clone-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "a" * 40,
                 # Hub 0.6.61 puts the lane on the row itself, from its own engine
                 # table, "so a scheduler does not have to make a second,
                 # best-effort catalog read and join on model id" — and so does the
                 # contract, verbatim except for the Hub's own `deadline_seconds`.
                 "modality": "voice",
                 "lane_contract": {
                     "schema": "studiohub.lane-contract.v1",
                     "modality": "voice",
                     "operation": "voice.tts",
                     "parameters": [
                         {"name": "text", "type": "string", "required": True},
                         {"name": "speed", "type": "number", "required": False,
                          "minimum": 0.7, "maximum": 1.2, "default": 1.0},
                     ],
                     "artifact": {"modality": "voice", "media_type": "audio/wav",
                                  "extension": "wav", "per_item": 1},
                     "states": {"accepted": ["queued"], "active": ["running"],
                                "terminal": ["done", "error", "cancelled"],
                                "uncertain": "uncertain", "uncertain_is_terminal": False,
                                "discard_supported": True},
                     "cancel": {"supported": True,
                                "request": "DELETE /api/hub/jobs/{batch_id}"},
                     "deadline_seconds": HUB_VOICE_DEADLINE_S,
                 }},
                {"package_id": "preset-model", "model_id": "mock-hub/preset-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "b" * 40,
                 "modality": "voice", "lane_contract": None},
                {"package_id": "uncatalogued-model", "model_id": "mock-hub/uncatalogued-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "c" * 40,
                 "modality": "voice", "lane_contract": None},
                {"package_id": "toobig-model", "model_id": "mock-hub/toobig-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "d" * 40,
                 "modality": "voice", "lane_contract": None},
                {"package_id": "capability-only-model", "model_id": "mock-hub/capability-only-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "e" * 40,
                 "modality": "voice", "lane_contract": None},
                # A **chat** package: the lane exists, so the row states it and
                # carries a contract whose `discard_supported` is false, exactly as
                # the Hub's own chat lane does (a synchronous completion has no
                # queued attempt to abandon).
                {"package_id": "chat-model", "model_id": "mock-hub/chat-model",
                 "engine": "chat-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "1" * 40,
                 "modality": "chat",
                 "lane_contract": {
                     "schema": "studiohub.lane-contract.v1",
                     "modality": "chat",
                     "operation": "chat.completion",
                     "parameters": [
                         {"name": "messages", "type": "array", "required": True},
                         {"name": "max_tokens", "type": "integer", "required": False},
                         {"name": "response_format", "type": "object", "required": False},
                     ],
                     "artifact": {"modality": "chat", "media_type": "application/json",
                                  "per_item": 1},
                     "states": {"accepted": ["queued"], "active": ["running"],
                                "terminal": ["done", "error", "cancelled"],
                                "uncertain": "uncertain", "uncertain_is_terminal": False,
                                "discard_supported": False},
                     "deadline_seconds": HUB_CHAT_DEADLINE_S,
                     # The producer's statement about the JSON guarantee, at the
                     # path the Hub session named. Note what it is *not*: there is
                     # no `two_pass` here, because whether to ask twice is the
                     # caller's technique and a model cannot know it.
                     "structured_output": {
                         "formats": ["json_object", "json_schema"],
                         "enforcement": "prompt_validate_repair",
                         "grammar_constrained": False,
                         "repair_attempts": 1,
                         "note": "no grammar-constrained decoding; JSON is asked for and validated",
                     },
                 }},
                # **A video lane, on the Hub's own terms.** `video-model` runs on a
                # registered video engine and carries the worker's own contract, so the
                # gateway publishes it with the modes, canvases and durations the *lane*
                # stated rather than a flat 5s/10s@720p guess.
                #
                # No `modality` key, deliberately: this row keeps the pre-0.6.61 shape so
                # the catalogue's own `hub_modality` remains the thing that places it, and
                # the package-stated path is pinned separately by `clone-model`.
                {"package_id": "video-model", "model_id": "mock-hub/video-model",
                 "engine": "video-ltx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "f" * 40,
                 "lane_contract": {
                     "schema": "studiohub.lane-contract.v1",
                     "modality": "video",
                     "operation": "video.generation",
                     "modes": ["image-to-video", "text-to-video"],
                     "parameters": [
                         {"name": "prompt", "type": "string", "required": True,
                          "max_characters": 4000},
                         {"name": "mode", "type": "string", "required": False,
                          "enum": ["image-to-video", "text-to-video"],
                          "default": "image-to-video"},
                         {"name": "aspect_ratio", "type": "string", "required": False,
                          "enum": ["7:4", "4:7", "1:1", "11:7"], "default": "11:7"},
                         {"name": "resolution", "type": "string", "required": False,
                          "enum": ["256p", "448p"], "default": "448p"},
                         {"name": "duration_seconds", "type": "integer", "required": False,
                          "enum": [2, 4], "default": 4},
                         {"name": "seed", "type": "integer", "required": False,
                          "minimum": -1, "maximum": 4294967295, "default": -1},
                         # The switch Hub 0.6.83 added to this lane, verbatim — including the note,
                         # copied from the running Hub's own `/api/hub/catalog` on 2026-09-27 rather
                         # than paraphrased. **The note is the point of relaying it**: the lane, not
                         # this gateway, knows that unticking it skips a decode rather than a
                         # denoise step, and a caller that shows the switch without that sentence
                         # implies a saving the lane does not make. `seed`/`mode` above are the
                         # control case: neither may become a switch.
                         {"name": "audio", "type": "boolean", "required": False, "default": True,
                          "ignored": False,
                          "note": ("the model generates picture and sound together, so this does "
                                   "not change generation time; unticking it skips the audio "
                                   "decode and writes a silent MP4. The track can be added back "
                                   "in an editor.")},
                     ],
                     # The pairs, exactly as the real lane publishes them: two classes, one
                     # ratio each way for 256p and two for 448p. `7:4` at `448p` is NOT a canvas,
                     # and a gateway that publishes the cross product of the two enums above
                     # would offer it to Story Studio.
                     "canvases": [
                         {"resolution": "256p", "aspect_ratio": "7:4", "width": 448, "height": 256,
                          "durations": [2, 4]},
                         {"resolution": "256p", "aspect_ratio": "4:7", "width": 256, "height": 448,
                          "durations": [2, 4]},
                         {"resolution": "448p", "aspect_ratio": "1:1", "width": 448, "height": 448,
                          "durations": [2, 4]},
                         {"resolution": "448p", "aspect_ratio": "11:7", "width": 704, "height": 448,
                          "durations": [2]},
                     ],
                     "artifact": {"modality": "video", "media_type": "video/mp4",
                                  "extension": "mp4", "per_item": 1},
                     "states": {"accepted": ["queued"], "active": ["running",
                                                                  "cancel_requested"],
                                "terminal": ["done", "error", "cancelled"],
                                "uncertain": "uncertain", "uncertain_is_terminal": False,
                                "discard_supported": True},
                     "cancel": {"supported": True,
                                "request": "DELETE /api/hub/jobs/{batch_id}"},
                     "deadline_seconds": HUB_VIDEO_DEADLINE_S,
                 }},
                # **A video model this Mac cannot run.** The Hub's own verdict is
                # `memory_eligible: false` — the same field a real 16 GB controller publishes for
                # the 22B model — so the gateway withholds it and the picker never offers work the
                # location would refuse. This is the tier, asserted at the public surface rather
                # than only in a unit test.
                {"package_id": "video-toobig-model", "model_id": "mock-hub/video-toobig-model",
                 "engine": "video-ltx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "9" * 40,
                 "modality": "video",
                 "lane_contract": {
                     "schema": "studiohub.lane-contract.v1",
                     "modality": "video",
                     "operation": "video.generation",
                     "modes": ["image-to-video"],
                     "parameters": [
                         {"name": "prompt", "type": "string", "required": True},
                         {"name": "resolution", "type": "string", "required": False,
                          "enum": ["448p"], "default": "448p"},
                         {"name": "duration_seconds", "type": "integer", "required": False,
                          "enum": [4], "default": 4},
                     ],
                     "artifact": {"modality": "video", "media_type": "video/mp4",
                                  "extension": "mp4", "per_item": 1},
                     "states": {"accepted": ["queued"], "active": ["running"],
                                "terminal": ["done", "error", "cancelled"],
                                "uncertain": "uncertain", "uncertain_is_terminal": False,
                                "discard_supported": True},
                     "deadline_seconds": HUB_VIDEO_DEADLINE_S,
                 }},
                # `unplaceable-model` is the one case nothing can place: an unknown
                # engine **and** no statement anywhere. It must be *reported*, not
                # silently dropped — "I could not work out what this is" and "there
                # is nothing here" are different answers. The Hub names the engine
                # allowlist in `reason`, and that sentence is what the gateway
                # reports.
                {"package_id": "unplaceable-model", "model_id": "mock-hub/unplaceable-model",
                 "engine": "mystery-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "0" * 40,
                 "modality": None, "lane_contract": None, "manifest_error": True,
                 "reason": ("the manifest engine 'mystery-mlx' has no worker or modality; add "
                            "it to _ENGINE_ROUTE in components/hub/standalone.py and to the "
                            "engine allowlist in components/model_packages.py")},
            ] + FLEET_PACKAGES})
            return
        if path == "/api/hub/catalog":
            self._json(200, {"models": [
                {"repo": "mock-hub/clone-model", "family": "mock-clone",
                 # The Hub **states** what each model is; that statement is what
                 # places a row. Measured live on 0.6.60: every catalogue row
                 # carries `hub_modality` (`voice` 197, `transcription` 65,
                 # `music` 10, `image` 5).
                 "hub_modality": "voice",
                 "capabilities": ["tts", "voice-cloning", "multilingual"],
                 "min_unified_memory_gb": 8, "memory_eligible": True,
                 "reference_audio": {
                     "declared": True, "supported": True, "profile_audited": True,
                     "profile_source": "audit",
                     "minimum_duration_seconds": 3, "target_duration_seconds": 8,
                     "recommended_duration_seconds": {"minimum": 8, "maximum": 12},
                     "maximum_duration_seconds": 15,
                     "transcript": "required",
                     "timestamped_transcript_segments":
                         "required_when_model_specific_selection_is_needed",
                 }},
                {"repo": "mock-hub/preset-model", "family": "mock-preset",
                 "hub_modality": "voice",
                 "capabilities": ["tts", "expressive"],
                 "min_unified_memory_gb": 8, "memory_eligible": True,
                 "reference_audio": {"declared": False, "supported": False,
                                     "profile_audited": False,
                                     "profile_source": "unverified"}},
                {"repo": "mock-hub/toobig-model", "family": "mock-toobig",
                 "hub_modality": "voice",
                 "capabilities": ["tts", "voice-cloning", "multilingual"],
                 "min_unified_memory_gb": 64, "memory_eligible": False,
                 "reference_audio": {"declared": True, "supported": True,
                                     "profile_audited": False,
                                     "profile_source": "unverified"}},
                # No `reference_audio` key at all. `capabilities` is the only
                # statement, and it does not name cloning.
                {"repo": "mock-hub/capability-only-model", "family": "mock-caps",
                 "hub_modality": "voice",
                 "capabilities": ["tts", "multilingual"],
                 "min_unified_memory_gb": 8, "memory_eligible": True},
                # The Hub's **stated** modality, which is what places this row (its
                # package row predates 0.6.61 and states none). The capabilities are the
                # broker's own tokens — `img2video` is the one its reference-image gate
                # reads — and `generates_audio` is tri-state: `true` here because the
                # LTX pack's own runs carry an audio track.
                {"repo": "mock-hub/video-model", "family": "mock-video",
                 "hub_modality": "video",
                 "capabilities": ["img2video", "txt2video"],
                 "generates_audio": True,
                 "min_unified_memory_gb": 24, "memory_eligible": True},
                # The same lane on a Mac below its tier: the Hub says so, and the gateway must
                # withhold the row rather than offering work the location would refuse.
                {"repo": "mock-hub/video-toobig-model", "family": "mock-video",
                 "hub_modality": "video",
                 "capabilities": ["img2video"],
                 "min_unified_memory_gb": 24, "memory_eligible": False},
                # A chat model: the Hub states the lane, its own limits — with the
                # sentence saying where each number came from — and its runtime
                # stays unmeasured.
                {"repo": "mock-hub/chat-model", "family": "qwen3_5",
                 "hub_modality": "chat",
                 "capabilities": ["chat"],
                 "min_unified_memory_gb": 8, "memory_eligible": True,
                 "model_limits": {
                     "context_window_tokens": {
                         "value": HUB_CHAT_CONTEXT_TOKENS,
                         "source": "model/config.json text_config.max_position_embeddings",
                     },
                     "max_output_tokens": {
                         "value": HUB_CHAT_MAX_OUTPUT_TOKENS,
                         "source": ("worker default: the checkpoint ships no "
                                    "generation_config.json"),
                     },
                 }},
                # `mock-hub/unplaceable-model` is deliberately absent: no
                # statement anywhere, on an engine nothing knows.
            ]})
            return

        # Black Forest Labs polling: Ready with a delivery URL.
        if path.startswith("/bfl-poll/"):
            self._json(200, {
                "id": path.rsplit("/", 1)[-1],
                "status": "Ready",
                "result": {"sample": f"http://127.0.0.1:{port}/bfl-sample.png"},
            })
            return

        if path == "/bfl-sample.png":
            self._bytes(200, ONE_PIXEL_PNG, "image/png")
            return

        # MiniMax video result.
        if path.startswith("/v2/query/video_generation/"):
            task_id = path.rsplit("/", 1)[-1]
            self._json(200, {
                "task": {
                    "id": task_id,
                    "status": "succeeded",
                    "content": {"url": f"http://127.0.0.1:{port}/minimax-video.mp4"},
                }
            })
            return

        if path in {"/minimax-video.mp4", "/minimax-music.mp3"}:
            media = "video/mp4" if path.endswith(".mp4") else "audio/mpeg"
            self._bytes(200, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, media)
            return

        if path.endswith("/models"):
            # Reject a missing key so the gateway's auth path is exercised.
            if not self.headers.get("Authorization"):
                self._json(401, {"error": {"message": "missing key"}})
                return
            self._json(200, {"object": "list", "data": MODELS})
            return
        self._json(404, {"error": {"message": "not found"}})

    def _record_hub_token(self, path: str) -> None:
        """Remember the credential this request carried, if it was aimed at a Hub."""
        if path.startswith("/api/hub"):
            HUB_TOKENS.append({
                "path": path,
                "token": str(self.headers.get("X-Hub-Token") or ""),
            })

    def _form(self) -> dict:
        """Request fields, for a multipart upload or a JSON body.

        The shared-voice push is **multipart** — the clip travels as a file part
        beside the text fields — so `_body()` sees no JSON and the recording would
        have been a silent `{}`, making every assertion about what was pushed
        vacuous. File parts are recorded by size, which is enough to prove the clip
        was actually uploaded.
        """
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype:
            return self._body()
        raw = self._raw()
        message = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        )
        out: dict = {}
        for part in message.get_payload() or []:
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            if part.get_filename():
                out[name] = f"<file {len(payload)} bytes>"
                out["__file_bytes__"] = len(payload)
            else:
                out[name] = payload.decode("utf-8", "replace")
        return out

    def do_POST(self) -> None:  # noqa: N802
        # A handler instance serves one request per connection here (HTTP/1.0,
        # close per request), but reset the cache anyway so a keep-alive reuse
        # could never hand a later request the previous body.
        self.__dict__.pop("_raw_cache", None)
        path = self.path.split("?")[0].rstrip("/")
        self._record_hub_token(path)
        body = self._body()
        model = str(body.get("model") or "")
        port = self.server.server_address[1]

        # ---- shared voices: record what the gateway pushed, and with which token.
        if path == "/__test__/reset":
            SHARED_VOICES.clear()
            SHARED_TOKENS.clear()
            HUB_TOKENS.clear()
            KIE_TASKS.clear()
            CHAT_CALLS.clear()
            global CHAT_STATUS, CHAT_REFUSAL_CODE, CHAT_DETAIL, CHAT_EMPTY
            CHAT_STATUS, CHAT_REFUSAL_CODE, CHAT_DETAIL, CHAT_EMPTY = 200, None, "", False
            self._json(200, {"ok": True})
            return
        # ---- Kie's unified task route. Images, videos and music all land here;
        # the recording is what lets the suite assert which lane a body was for.
        if path == "/api/v1/jobs/createTask":
            KIE_TASKS.append(body)
            self._json(200, {"code": 200, "msg": "success",
                             "data": {"taskId": f"task-{len(KIE_TASKS)}"}})
            return
        if path == "/api/hub/shared-voices":
            SHARED_TOKENS.append(str(self.headers.get("X-Hub-Token") or ""))
            SHARED_VOICES.append(self._form())
            # A controller accepts it and fans out to its own nodes; from the
            # gateway's side the voice is installed, and a node that is off is that
            # controller's `pending` to report, not the gateway's.
            self._json(200, {
                "created": True,
                "voice": {
                    "id": "abcdef123456",
                    "segment_count": 1,
                    "sync": {"installed": 1, "pending": 0},
                    "targets": [{"machine": "mock-node", "status": "pending"}],
                },
            })
            return

        if model == "mock-fail":
            self._json(429, {"error": {"message": "mock quota exhausted"}})
            return

        # ---- Black Forest Labs: POST /v1/{model}, raw input, no `model` field
        if path.startswith("/v1/") and not model and "prompt" in body:
            self._json(200, {
                "id": "bfl-mock-1",
                "polling_url": f"http://127.0.0.1:{port}/bfl-poll/bfl-mock-1",
            })
            return

        # ---- MiniMax
        if path.endswith("/v1/image_generation"):
            self._json(200, {
                "data": {"image_base64": [base64.b64encode(ONE_PIXEL_PNG).decode()]},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            })
            return

        if path.endswith("/v1/music_generation"):
            self._json(200, {
                "data": {"status": 2, "audio": f"http://127.0.0.1:{port}/minimax-music.mp3"},
                "base_resp": {"status_code": 0},
            })
            return

        if path.endswith("/v1/t2a_v2"):
            # MiniMax returns audio as HEX, not base64. The mock must too, or a
            # decoder bug would pass here and only surface in production.
            self._json(200, {
                "data": {"audio": _wav().hex()},
                "base_resp": {"status_code": 0},
            })
            return

        if path.endswith("/v2/video_generation"):
            self._json(200, {"task_id": "mm-video-1", "base_resp": {"status_code": 0}})
            return

        # The Hub's chat **peer** route (0.6.63). This is the lane the gateway
        # dispatches on: a Fleet credential may run a completion here, through the
        # Hub, so admission and the machine lease apply. Its answer carries the
        # worker's **own** generation time — measured after the model is resident,
        # so it is deliberately not the round trip — which the gateway publishes
        # as `generation_seconds` and must not present as its own latency.
        if path == "/studio/chat/v1/chat/completions":
            CHAT_CALLS.append({
                "path": path,
                "token": self.headers.get("X-Hub-Token"),
                "body": body,
            })
            if CHAT_STATUS >= 400:
                payload: dict = {"detail": CHAT_DETAIL or "refused"}
                if CHAT_REFUSAL_CODE:
                    payload["code"] = CHAT_REFUSAL_CODE
                self._json(CHAT_STATUS, payload)
                return
            if CHAT_EMPTY:
                self._json(200, {
                    "id": "chatcmpl-hub-mock", "object": "chat.completion",
                    "model": model or "mock-hub/chat-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": ""}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                    "studio_hub": {"elapsed_seconds": 0.1},
                })
                return
            self._json(200, {
                "id": "chatcmpl-hub-mock",
                "object": "chat.completion",
                "created": 1790300000,
                "model": model or "mock-hub/chat-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant",
                                "content": f"mock hub reply from {model or 'the model'}"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 11, "completion_tokens": 13, "total_tokens": 24},
                "studio_hub": {"elapsed_seconds": 2.5},
            })
            return

        if path.endswith("/chat/completions"):
            self._json(
                200,
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"mock reply from {model}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
                },
            )
            return

        # ElevenLabs' voice-import route. Deliberately answers **200 with a body
        # that is not JSON**: a proxy in front of a provider can do that, and the
        # gateway used to raise `JSONDecodeError` straight out of the handler — a
        # 500 with no sentence, after the voice had already been added upstream.
        # The id that was asked for is the answer, so the import must stand.
        if path.startswith("/v1/voices/add/"):
            self._bytes(200, b"<html><body>ok</body></html>", "text/html")
            return

        if path.endswith("/images/generations"):
            # `n` is honoured, and every entry is a *different* pixel, so a check
            # that several images came back cannot pass on a bug that returned the
            # first one repeatedly.
            body = self._body()
            try:
                count = int(body.get("n") or 1)
            except (TypeError, ValueError):
                count = 1
            count = max(1, min(count, 8))
            self._json(
                200,
                {
                    "created": 0,
                    "data": [
                        {"b64_json": base64.b64encode(
                            one_pixel_png((index * 30 % 256, 40, 90))
                        ).decode()}
                        for index in range(count)
                    ],
                },
            )
            return

        if path.endswith("/audio/speech"):
            self._bytes(200, _wav(), "audio/wav")
            return

        if path.endswith("/audio/transcriptions"):
            # The body is multipart and already drained by _body(); the test
            # double only needs to prove the gateway round-trips an upload.
            self._bytes(
                200,
                b"1\n00:00:00,000 --> 00:00:02,000\nmock transcript\n\n",
                "application/x-subrip",
            )
            return

        self._json(404, {"error": {"message": f"unhandled path {path}"}})


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS stall.

    `HTTPServer.server_bind()` calls `socket.getfqdn()` to fill in
    `server_name`. On a machine whose DNS resolution for 127.0.0.1 is slow or
    blackholed, that call blocks *after* `bind()` but *before* `listen()` -- so
    the socket is held but accepts nothing, and every client sees a connect
    timeout rather than a connection refusal. Skipping the lookup removes the
    whole failure mode; nothing here needs a hostname.
    """

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    server = Server(("127.0.0.1", args.port), Handler)
    print(f"mock upstream on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
