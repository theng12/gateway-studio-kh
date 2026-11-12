#!/usr/bin/env python3
"""The MCP agent connector, driven end to end against a fixture Gateway on a spare port.

The connector is `connectors/mcp/server.py`: one stdio process any MCP client can launch.
These tests start it as a subprocess and speak the protocol to it exactly as a client
does, against a real HTTP server that answers like the Gateway's guest Create API — no
live Gateway, no network beyond 127.0.0.1 on an ephemeral port, and never port 8787.

What is proven here:

1. **Every tool, end to end.** list_models, list_voices, generate_image, generate_speech,
   generate_video (a real multipart upload), transcribe (a real file), generate_music,
   chat, get_job, wait_for_job, download_result.
2. **Async and honest.** A generate tool returns at once with a job id and a state; the
   waiting helper reports "still running" rather than pretending it finished, and returns
   the finished job when it does.
3. **The Gateway's own refusal.** An unknown model is refused with the Gateway's sentence,
   not one the connector invented.
4. **A dead key is a clear error**, and the key never appears in a tool result or on the
   server's stdout/stderr — the connector is a client, and a client that leaks its token
   is worse than one that fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "connectors" / "mcp" / "server.py"
GUEST_KEY = "gwk_fixture-key-not-a-real-secret"
REVOKED_KEY = "gwk_revoked-fixture-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
AUDIO = b"ID3" + b"\x00" * 32


def envelope(job_id: str, state: str, modality: str, model_id: str,
             outputs: list[dict] | None = None) -> dict:
    return {
        "id": job_id, "state": state, "modality": modality, "model_id": model_id,
        "requested_by": {"role": "guest", "guest_id": "guest-1", "label": "Maria (guest)"},
        "failure_code": None, "failure_reason": None,
        "poll_after_ms": 0 if state != "queued" else 50,
        "outputs": outputs or [],
        **({"output_text": "hello from the fixture"} if modality == "chat" and state == "succeeded"
           else {}),
    }


class FixtureGateway:
    """The guest Create API in miniature: catalogue, jobs, artifacts, and one dead key."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.objections: list[str] = []
        self.artifacts: dict[tuple[str, str], bytes] = {}
        self.uploads: list[dict] = []
        self._lock = threading.Lock()
        self._next = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # keep the test's own output quiet
                pass

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _answer(self, status: int, payload: dict | bytes, media="application/json") -> None:
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                header = self.headers.get("Authorization") or ""
                if header == "Bearer " + GUEST_KEY:
                    return True
                self._answer(401, {"detail": "That guest API key is not valid — it may have "
                                            "been revoked, or its guest disabled or removed. "
                                            "Ask the Gateway's owner for a new one.",
                                   "code": "GUEST_KEY_INVALID"})
                return False

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                if not self._auth():
                    return
                parsed = urlsplit(self.path)
                fixture.requests.append(("GET", parsed.path))
                if parsed.path == "/admin/api/create/models":
                    return self._answer(200, {
                        "by_modality": {
                            "image": [{"id": "fixture::image-1", "display_name": "Fixture Image",
                                       "chargeable": False, "aspect_ratios": ["16:9", "9:16"],
                                       "resolutions": ["1024x1024"],
                                       "max_prompt_characters": 4000}],
                            "tts": [{"id": "fixture::tts-1", "display_name": "Fixture Voice",
                                     "chargeable": False, "maximum_input_characters": 15000}],
                            "video": [{"id": "fixture::video-1", "display_name": "Fixture Video",
                                       "chargeable": False, "modes": ["image-to-video"],
                                       "aspect_ratios": ["16:9"], "durations": ["5s", "10s"]}],
                            "music": [{"id": "fixture::music-1", "display_name": "Fixture Music",
                                       "chargeable": False}],
                            "chat": [{"id": "fixture::chat-1", "display_name": "Fixture Chat",
                                      "chargeable": False}],
                            "transcription": [{"id": "fixture::asr-1",
                                               "display_name": "Fixture ASR",
                                               "chargeable": False}],
                        },
                        "counts": {"image": 1, "tts": 1, "video": 1, "music": 1, "chat": 1,
                                   "transcription": 1},
                        "paid_models": False,
                    })
                if parsed.path == "/admin/api/voices":
                    return self._answer(200, {"voices": [
                        {"id": "fixture-voice", "display_name": "Fixture Voice", "language": "en"}]})
                if parsed.path.startswith("/admin/api/create/status/"):
                    job = fixture.jobs.get(parsed.path.rsplit("/", 1)[-1])
                    if job is None:
                        return self._answer(404, {"detail": "no job",
                                                  "code": "JOB_NOT_FOUND"})
                    return self._answer(200, fixture.tick(job))
                if parsed.path.startswith("/admin/api/artifacts/"):
                    parts = parsed.path.strip("/").split("/")
                    asset = parts[-1]
                    job_id = parts[-3]
                    content = fixture.artifacts.get((job_id, asset))
                    if content is None:
                        return self._answer(404, {"detail": "no job"})
                    return self._answer(200, content, media="image/png")
                return self._answer(404, {"detail": "not here"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth():
                    return
                body = self._body()
                parsed = urlsplit(self.path)
                fixture.requests.append(("POST", parsed.path))
                if parsed.path == "/admin/api/create":
                    answer = fixture.submit(json.loads(body or b"{}"))
                    if "detail" in answer:
                        return self._answer(404, answer)
                    return self._answer(202, answer)
                if parsed.path in ("/admin/api/create/transcription", "/admin/api/create/video"):
                    fields, files = fixture.multipart(body, self.headers.get("Content-Type") or "")
                    fixture.uploads.append({"path": parsed.path, "fields": fields,
                                            "files": {k: v[:8] for k, v in files.items()}})
                    model_id = fields.get("model_id", "")
                    if model_id not in fixture.known_ids():
                        return self._answer(404, {"detail": f'Model "{model_id}" is not '
                                                            "available.", "code": "MODEL_NOT_FOUND"})
                    modality = "video" if parsed.path.endswith("/video") else "transcription"
                    if modality == "video" and "file" not in files:
                        return self._answer(422, {"detail": "A video generation needs the scene "
                                                            "image."})
                    return self._answer(202, fixture.queue(modality, model_id))
                return self._answer(404, {"detail": "not here"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FixtureGateway":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    # -- the Gateway's rules, in miniature --------------------------------
    def known_ids(self) -> set[str]:
        return {"fixture::image-1", "fixture::tts-1", "fixture::video-1", "fixture::music-1",
                "fixture::chat-1", "fixture::asr-1"}

    def queue(self, modality: str, model_id: str) -> dict:
        with self._lock:
            self._next += 1
            job_id = f"job-{self._next}"
            job = {"id": job_id, "modality": modality, "model_id": model_id, "polls": 0,
                   "state": "queued", "outputs": [{"asset_id": "0", "media_type": "image/png",
                                                   "bytes": len(PNG), "present": True}]}
            self.jobs[job_id] = job
            self.artifacts[(job_id, "0")] = PNG
            return envelope(job_id, "queued", modality, model_id)

    def submit(self, payload: dict) -> dict:
        model_id = str(payload.get("model_id") or "")
        if model_id not in self.known_ids():
            return {"detail": f'Model "{model_id}" is not available.', "code": "MODEL_NOT_FOUND"}
        modality = str(payload.get("modality") or "")
        if modality not in ("chat", "image", "music", "tts"):
            return {"detail": f"modality must be one of chat, image, music, tts; got {modality!r}"}
        return self.queue(modality, model_id)

    def tick(self, job: dict) -> dict:
        """Every other poll advances the job, so a waiting helper sees a real transition."""
        job["polls"] += 1
        if job["polls"] >= 2 and job["state"] != "succeeded":
            job["state"] = "succeeded"
        return envelope(job["id"], job["state"], job["modality"], job["model_id"],
                        job["outputs"] if job["state"] == "succeeded" else [])

    def multipart(self, body: bytes, content_type: str) -> tuple[dict, dict]:
        """Just enough multipart parsing to read a form and its one file."""
        boundary = content_type.split("boundary=", 1)[-1].strip().encode()
        fields, files = {}, {}
        for part in body.split(b"--" + boundary):
            if b"Content-Disposition" not in part:
                continue
            head, _sep, content = part.partition(b"\r\n\r\n")
            content = content.rstrip(b"\r\n")
            disposition = head.decode("utf-8", "replace")
            name = disposition.split('name="', 1)[-1].split('"', 1)[0]
            if 'filename="' in disposition:
                files[name] = content
            else:
                fields[name] = content.decode("utf-8", "replace")
        return fields, files


class Mcp:
    """One connector process, spoken to over stdio, the way an MCP client does."""

    def __init__(self, *, url: str, key: str | None, output_dir: Path) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in ("GATEWAY_GUEST_KEY", "GATEWAY_URL", "GATEWAY_OUTPUT_DIR")}
        env.update({"GATEWAY_URL": url, "GATEWAY_OUTPUT_DIR": str(output_dir),
                    "PYTHONUNBUFFERED": "1"})
        if key is not None:
            env["GATEWAY_GUEST_KEY"] = key
        self.process = subprocess.Popen(
            [sys.executable, "-I", str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self.transcript = ""
        self.stderr = ""
        self.closed = False

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = f"req-{time.monotonic_ns()}"
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        if notify:
            return None
        line = self.process.stdout.readline()
        self.transcript += line
        if not line:
            self.stderr += self.process.stderr.read()
            raise AssertionError(f"the connector died: {self.stderr}")
        return json.loads(line)

    def call(self, tool: str, arguments: dict | None = None) -> tuple[dict, bool]:
        answer = self.send("tools/call", {"name": tool, "arguments": arguments or {}})
        result = answer["result"]
        text = "".join(part.get("text", "") for part in result.get("content") or [])
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = {"text": text}
        return parsed, bool(result.get("isError"))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.stderr += self.process.stderr.read()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FixtureGateway().start()
        self.addCleanup(self.gateway.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-connector-")
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name) / "assets"
        self.output_dir.mkdir()
        self.connector = Mcp(url=self.gateway.url, key=GUEST_KEY, output_dir=self.output_dir)
        self.addCleanup(self.connector.close)
        handshake = self.connector.send("initialize", {"protocolVersion": "2025-06-18",
                                                       "capabilities": {},
                                                       "clientInfo": {"name": "test",
                                                                      "version": "0"}})
        self.assertEqual(handshake["result"]["serverInfo"]["name"],
                         "gateway-kh-agent-connector")
        self.connector.send("notifications/initialized", notify=True)


class TheProtocol(_Base):
    def test_every_tool_is_listed_with_a_schema_from_the_live_catalogue(self) -> None:
        listing = self.connector.send("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in listing}
        self.assertEqual(names, {"list_models", "list_voices", "generate_image",
                                 "generate_speech", "generate_video", "transcribe",
                                 "generate_music", "chat", "get_job", "wait_for_job",
                                 "download_result"})
        image = next(tool for tool in listing if tool["name"] == "generate_image")
        model_id = image["inputSchema"]["properties"]["model_id"]
        # The enum is the fixture's live catalogue, not a list written in the connector.
        self.assertEqual(model_id["enum"], ["fixture::image-1"])
        self.assertIn("fixture::image-1", model_id["description"])
        self.assertIn("16:9", model_id["description"])
        video = next(tool for tool in listing if tool["name"] == "generate_video")
        self.assertIn("image_path", video["inputSchema"]["required"])
        self.assertIn("image-to-video", video["description"])

    def test_an_unknown_method_is_a_json_rpc_error(self) -> None:
        answer = self.connector.send("tools/nonsense")
        self.assertEqual(answer["error"]["code"], -32601)


class EveryTool(_Base):
    def test_list_models_and_voices(self) -> None:
        models, error = self.connector.call("list_models")
        self.assertFalse(error)
        self.assertEqual(models["models"]["image"][0]["max_prompt_characters"], 4000)
        self.assertFalse(models["paid_models"])
        voices, error = self.connector.call("list_voices", {"search": "fixture"})
        self.assertFalse(error)
        self.assertEqual(voices["voices"][0]["id"], "fixture-voice")

    def _started(self, tool: str, arguments: dict, modality: str, model_id: str) -> dict:
        summary, error = self.connector.call(tool, arguments)
        self.assertFalse(error, summary)
        self.assertEqual(summary["state"], "queued")
        self.assertEqual(summary["modality"], modality)
        self.assertEqual(summary["model_id"], model_id)
        self.assertEqual(summary["requested_by"], "Maria (guest)")
        self.assertTrue(summary["job_id"])
        return summary

    def test_image_speech_music_and_chat(self) -> None:
        self._started("generate_image", {"model_id": "fixture::image-1", "prompt": "a lighthouse",
                                         "options": {"aspect_ratio": "16:9"}},
                      "image", "fixture::image-1")
        self._started("generate_speech", {"model_id": "fixture::tts-1", "text": "hello",
                                          "voice_id": "fixture-voice"}, "tts", "fixture::tts-1")
        self._started("generate_music", {"model_id": "fixture::music-1", "prompt": "calm"},
                      "music", "fixture::music-1")
        chat_asked = self._started("chat", {"model_id": "fixture::chat-1", "prompt": "hi"},
                                   "chat", "fixture::chat-1")
        done, error = self.connector.call("wait_for_job", {"job_id": chat_asked["job_id"],
                                                           "timeout_seconds": 10})
        self.assertFalse(error)
        self.assertTrue(done["done"])
        self.assertEqual(done["output_text"], "hello from the fixture")

    def test_video_uploads_the_scene_image(self) -> None:
        image = Path(self.temp.name) / "scene.png"
        image.write_bytes(PNG)
        summary = self._started("generate_video",
                                {"model_id": "fixture::video-1", "image_path": str(image),
                                 "prompt": "she walks", "options": {"duration_seconds": 5}},
                                "video", "fixture::video-1")
        self.assertTrue(summary["job_id"])
        upload = self.gateway.uploads[-1]
        self.assertEqual(upload["path"], "/admin/api/create/video")
        self.assertEqual(upload["fields"]["prompt"], "she walks")
        self.assertEqual(json.loads(upload["fields"]["options"]), {"duration_seconds": 5})
        self.assertEqual(upload["files"]["file"][:8], PNG[:8])

    def test_transcription_uploads_the_file(self) -> None:
        audio = Path(self.temp.name) / "clip.mp3"
        audio.write_bytes(AUDIO)
        summary = self._started("transcribe", {"model_id": "fixture::asr-1",
                                               "file_path": str(audio), "language": "km"},
                                "transcription", "fixture::asr-1")
        self.assertTrue(summary["job_id"])
        upload = self.gateway.uploads[-1]
        self.assertEqual(upload["path"], "/admin/api/create/transcription")
        self.assertEqual(upload["fields"]["language"], "km")
        self.assertEqual(upload["files"]["file"][:3], AUDIO[:3])

    def test_a_missing_local_file_is_named_before_any_call(self) -> None:
        summary, error = self.connector.call("generate_video",
                                             {"model_id": "fixture::video-1",
                                              "image_path": "/no/such/photo.png"})
        self.assertTrue(error)
        self.assertIn("is not a file this Mac can read", summary["text"])

    def test_an_unknown_model_is_refused_in_the_gateways_own_words(self) -> None:
        summary, error = self.connector.call("generate_image",
                                             {"model_id": "nobody::no-such-model",
                                              "prompt": "x"})
        self.assertTrue(error)
        self.assertIn('Model "nobody::no-such-model" is not available.', summary["text"])

    def test_waiting_is_bounded_and_never_pretends(self) -> None:
        image = self._started("generate_image", {"model_id": "fixture::image-1", "prompt": "x"},
                              "image", "fixture::image-1")
        # The fixture finishes on its second poll; a one-second wait may or may not see it, so
        # the honest property is: either done, or "still running" with the job id — never a
        # claim of completion without an output.
        summary, error = self.connector.call("wait_for_job",
                                             {"job_id": image["job_id"], "timeout_seconds": 1})
        self.assertFalse(error)
        if summary["done"]:
            self.assertEqual(summary["state"], "succeeded")
            self.assertEqual(summary["outputs"][0]["asset_id"], "0")
        else:
            self.assertIn("Still running", summary["next"])
        final = self.connector.call("get_job", {"job_id": image["job_id"]})[0]
        self.assertIn(final["state"], ("queued", "running", "succeeded"))

    def test_download_result_saves_inside_the_allowed_folder(self) -> None:
        job = self._started("generate_image", {"model_id": "fixture::image-1", "prompt": "x"},
                            "image", "fixture::image-1")
        self.connector.call("wait_for_job", {"job_id": job["job_id"], "timeout_seconds": 5})
        saved, error = self.connector.call("download_result",
                                           {"job_id": job["job_id"], "folder": "today",
                                            "filename": "scene.png"})
        self.assertFalse(error, saved)
        target = Path(saved["saved_to"])
        self.assertEqual(target.parent.resolve(), (self.output_dir / "today").resolve())
        self.assertEqual(target.read_bytes(), PNG)
        again, error = self.connector.call("download_result",
                                           {"job_id": job["job_id"], "folder": "today",
                                            "filename": "scene.png"})
        self.assertFalse(error)
        self.assertNotEqual(again["saved_to"], saved["saved_to"], "an existing file is never "
                                                                  "overwritten silently")
        self.assertTrue(Path(again["saved_to"]).is_file())

    def test_download_refuses_a_folder_outside_the_allowed_one(self) -> None:
        job = self._started("generate_image", {"model_id": "fixture::image-1", "prompt": "x"},
                            "image", "fixture::image-1")
        self.connector.call("wait_for_job", {"job_id": job["job_id"], "timeout_seconds": 5})
        for folder in ("../outside", "/tmp", "a/../../b"):
            summary, error = self.connector.call("download_result",
                                                 {"job_id": job["job_id"], "folder": folder})
            self.assertTrue(error, f"{folder} was accepted")
            self.assertIn("outside the allowed output folder", summary["text"])

    def test_download_before_done_says_so(self) -> None:
        job = self._started("generate_image", {"model_id": "fixture::image-1", "prompt": "x"},
                            "image", "fixture::image-1")
        # A fresh fixture job has not polled to success yet.
        summary, error = self.connector.call("download_result", {"job_id": job["job_id"]})
        self.assertTrue(error)
        self.assertIn("not finished", summary["text"])


class TheKey(_Base):
    def test_a_revoked_key_is_a_clear_refusal(self) -> None:
        dead = Mcp(url=self.gateway.url, key=REVOKED_KEY, output_dir=self.output_dir)
        self.addCleanup(dead.close)
        dead.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "0"}})
        summary, error = dead.call("list_models")
        self.assertTrue(error)
        self.assertIn("is not valid", summary["text"])
        self.assertIn("GUEST_KEY_INVALID", summary["text"])
        dead.close()

    def test_no_key_says_how_to_get_one(self) -> None:
        missing = Mcp(url=self.gateway.url, key=None, output_dir=self.output_dir)
        self.addCleanup(missing.close)
        missing.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})
        # tools/list still answers: a client that cannot reach the Gateway must not hang.
        listing = missing.send("tools/list")["result"]["tools"]
        self.assertTrue(listing)
        catalogued, catalog_error = missing.call("list_models")
        self.assertTrue(catalog_error)
        self.assertIn("No guest key is configured", catalogued["text"])
        summary, error = missing.call("generate_image",
                                      {"model_id": "fixture::image-1", "prompt": "x"})
        self.assertTrue(error)
        self.assertIn("No guest key is configured", summary["text"])
        self.assertIn("GATEWAY_GUEST_KEY", summary["text"])
        missing.close()

    def test_the_key_never_appears_in_a_result_or_on_the_wire(self) -> None:
        self.connector.call("list_models")
        self.connector.call("generate_image", {"model_id": "nobody::no-such-model", "prompt": "x"})
        self.connector.call("get_job", {"job_id": "job-1"})
        self.connector.close()
        self.assertNotIn(GUEST_KEY, self.connector.transcript)
        self.assertNotIn(GUEST_KEY, self.connector.stderr)
        self.assertNotIn("Bearer", self.connector.transcript)


if __name__ == "__main__":
    unittest.main()
