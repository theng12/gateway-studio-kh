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
            self._json(200, {"nodes": [], "summary": {"nodes": 0}})
            return
        if path == "/api/hub/capabilities":
            self._json(200, {"controller": {"role": "controller", "ready": True},
                             "site_id": "mock-site"})
            return
        if path == "/api/hub/stats":
            self._json(200, {"fleet_activity": {}})
            return
        if path == "/api/hub/jobs":
            self._json(200, {"batches": []})
            return

        # ---- what a location's models *are*, for the local voice lane.
        #
        # `model-packages` says what is installed; `catalog` says what each model
        # is — whether it clones at all (a preset-speaker tier does not) and what
        # reference contract it declares. Both are served here so the voice
        # matching, the per-model reference report and the no-invented-numbers
        # rule can be asserted without a real Hub.
        #
        # Three deliberate shapes:
        #   mock-hub/clone-model        catalogued, audited contract (3/8/15s)
        #   mock-hub/preset-model       catalogued, `declared: false` — no reference
        #   mock-hub/uncatalogued-model installed but absent from the catalogue
        #                               → *unverified* for its voices, and not a
        #                                 model at all as far as publication goes
        #   mock-hub/toobig-model       catalogued, but the Hub says it does not fit
        #                               this machine (`memory_eligible: false`)
        if path == "/api/hub/model-packages":
            self._json(200, {"models": [
                {"package_id": "clone-model", "model_id": "mock-hub/clone-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "a" * 40},
                {"package_id": "preset-model", "model_id": "mock-hub/preset-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "b" * 40},
                {"package_id": "uncatalogued-model", "model_id": "mock-hub/uncatalogued-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "c" * 40},
                {"package_id": "toobig-model", "model_id": "mock-hub/toobig-model",
                 "engine": "voice-mlx", "files_state": "files_verified",
                 "dispatch_state": "eligible", "revision": "d" * 40},
            ]})
            return
        if path == "/api/hub/catalog":
            self._json(200, {"models": [
                {"repo": "mock-hub/clone-model", "family": "mock-clone",
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
                 "capabilities": ["tts", "expressive"],
                 "min_unified_memory_gb": 8, "memory_eligible": True,
                 "reference_audio": {"declared": False, "supported": False,
                                     "profile_audited": False,
                                     "profile_source": "unverified"}},
                {"repo": "mock-hub/toobig-model", "family": "mock-toobig",
                 "capabilities": ["tts", "voice-cloning", "multilingual"],
                 "min_unified_memory_gb": 64, "memory_eligible": False,
                 "reference_audio": {"declared": True, "supported": True,
                                     "profile_audited": False,
                                     "profile_source": "unverified"}},
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
            self._json(200, {"ok": True})
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

        if path.endswith("/images/generations"):
            self._json(
                200,
                {"created": 0, "data": [{"b64_json": base64.b64encode(ONE_PIXEL_PNG).decode()}]},
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
