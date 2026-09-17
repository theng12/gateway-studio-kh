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

MODELS = [
    {"id": "mock-chat-1", "name": "Mock Chat One", "context_length": 32000},
    {"id": "mock-chat-2", "name": "Mock Chat Two", "context_length": 128000},
    {"id": "mock-image-1", "name": "Mock Image One"},
    {"id": "mock-tts-1", "name": "Mock TTS One"},
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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        port = self.server.server_address[1]

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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        body = self._body()
        model = str(body.get("model") or "")
        port = self.server.server_address[1]

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
