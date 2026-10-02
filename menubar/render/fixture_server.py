#!/usr/bin/env python3
"""A disposable Gateway for the render harness. Never touches the owner's Gateway, Hub, keys or data.

Starts, all on 127.0.0.1 with random ports and a temp profile:
  * a tiny OpenAI-shaped upstream that lists a few fictional providers' models,
  * a real gateway (uvicorn gateway.api:app) pointed at it, seeded with providers, voices and jobs,
  * an overlay on the port the app is told to use: it answers /admin/api/fleet from `fleet_fixture.py`
    (the live fleet needs real Hubs) and proxies everything else to the real gateway.

    python menubar/render/fixture_server.py --port 18787 --support /tmp/dir     (run from the repository root)

Prints `READY <support>` once serving; SIGTERM stops everything.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PROVIDERS = {
    "atlas": ("Atlas AI", ["chat", "image", "tts", "transcription"], [
        "atlas-chat-large", "atlas-chat-mini", "atlas-reason-1", "atlas-image-2", "atlas-tts-hd", "atlas-whisper-1"]),
    "meridian": ("Meridian Router", ["chat"], [f"{v}/{m}" for v, ms in {
        "anthropic-like": ["sonnet-4", "haiku-4", "opus-4"], "google-like": ["pro-2.5", "flash-2.5", "flash-lite"],
        "meta-like": ["llama-4-maverick", "llama-4-scout", "llama-3.3-70b-instruct"],
        "mistral-like": ["large-2", "small-3.2", "codestral"], "qwen-like": ["qwen3-235b", "qwen3-32b", "qwq-32b"],
        "deepseek-like": ["v3.2", "r1-0528"], "xai-like": ["grok-4", "grok-4-fast"],
    }.items() for m in ms]),
    "voxline": ("Voxline Audio", ["tts", "music"], [
        "voxline-tts-v3", "voxline-speech-turbo", "voxline-voice-clone-tts", "voxline-music-1", "voxline-music-long"]),
    "prism": ("Prism Media", ["image", "video"], [
        "prism-flux-pro", "prism-flux-dev", "prism-sdxl-lightning", "prism-video-1", "prism-hailuo-02", "prism-kling-2"]),
}
JOBS = [  # (modality, model, provider, state, minutes ago, caller, error)
    ("tts", "voxline-tts-v3", "voxline", "succeeded", 4, "story-studio", None),
    ("image", "prism-flux-pro", "prism", "succeeded", 9, "story-studio", None),
    ("chat", "atlas-chat-large", "atlas", "succeeded", 12, "story-studio", None),
    ("tts", "voice-clone-qwen3", "studio-hub-local", "succeeded", 17, "story-studio", None),
    ("image", "FLUX.2-klein-4B", "studio-hub-ctrl-studio2", "succeeded", 26, "story-studio", None),
    ("video", "prism-hailuo-02", "prism", "failed", 33, "story-studio",
     ("PROVIDER_TIMEOUT", "The provider did not finish within 600 s. The job was cancelled upstream.")),
    ("transcription", "atlas-whisper-1", "atlas", "succeeded", 48, "gateway-app", None),
    ("music", "voxline-music-1", "voxline", "succeeded", 70, "gateway-app", None),
    ("chat", "meridian/anthropic-like/sonnet-4", "meridian", "succeeded", 95, "story-studio", None),
    ("image", "prism-flux-dev", "prism", "failed", 130, "story-studio",
     ("INSUFFICIENT_MEMORY", "Studio 2 B needed 10.0 GB and has 3.1 GB free. Routed elsewhere was not possible.")),
    ("tts", "atlas-tts-hd", "atlas", "succeeded", 180, "story-studio", None),
    ("tts", "voice-clone-qwen3", "studio-hub-ctrl-blue", "cancelled", 260, "story-studio", None),
    ("image", "FLUX.2-klein-4B", "studio-hub-local", "succeeded", 400, "gateway-app", None),
    ("chat", "atlas-reason-1", "atlas", "succeeded", 700, "story-studio", None),
]
VOICES = [("Marlowe", "en", "qwen3-tts"), ("Ines", "es", "qwen3-tts"), ("Okafor", "en", "kokoro"),
          ("Hana", "ja", "qwen3-tts"), ("Dmitri", "ru", "kokoro"), ("Brightwater (narrator)", "en", "qwen3-tts")]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        name = self.path.split("/")[1]
        label, _, models = PROVIDERS.get(name, ("", [], []))
        body = json.dumps({"object": "list", "data": [{"id": m, "object": "model"} for m in models]}).encode()
        self.send_response(200 if models else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def seed(support: Path, upstream_port: int) -> dict:
    data = support / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "providers.local.json").write_text(json.dumps([{
        "id": pid, "display_name": label, "kind": "openai_compatible", "category": "language" if mods == ["chat"] else "multi",
        "base_url": f"http://127.0.0.1:{upstream_port}/{pid}/v1", "modalities": mods,
    } for pid, (label, mods, _) in PROVIDERS.items()]))
    voices = []
    for index, (name, language, engine) in enumerate(VOICES):
        reference = data / f"voice-{index}.wav"
        with wave.open(str(reference), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * (16000 * (4 + index)))
        voices.append({"id": f"voice-{index:012d}", "display_name": name, "language": language,
                       "engine_id": engine, "engine_name": engine.title(), "tts_model_id": "Qwen3-TTS-12Hz-0.6B-Base-4bit",
                       "reference_path": str(reference), "reference_transcript": "The quick brown fox.",
                       "audio_sha256": f"{index:064d}", "permission_acknowledged": index % 2 == 0, "license": ""})
    (data / "voices.json").write_text(json.dumps({"voices": voices}))
    (support / ".env").write_text("GATEWAY_API_KEY=render-fixture-key\n")
    (support / ".env").chmod(0o600)
    # Backups go to temp stand-ins for iCloud, ~/Developer/_backups and a KH-STUDIO drive.
    (support / "CloudDocs").mkdir(exist_ok=True)
    (support / "Volumes/KH-STUDIO-NOVA/_trash").mkdir(parents=True, exist_ok=True)
    hub_voice = support / "hub/voice/voices/0616aec33b41"
    hub_voice.mkdir(parents=True, exist_ok=True)
    (hub_voice / "transcript.txt").write_text("The quick brown fox.")
    (hub_voice / "reference.wav").write_bytes((data / "voice-0.wav").read_bytes())
    return {"GATEWAY_APP_SUPPORT": str(support), "GATEWAY_DATA": str(data), "GATEWAY_DB": str(data / "gateway.sqlite3"),
            "GATEWAY_ARTIFACTS": str(data / "artifacts"), "GATEWAY_CREDENTIALS": str(data / "credentials.json"),
            "GATEWAY_MODEL_CACHE": str(data / "models_cache.json"), "GATEWAY_ENV_FILE": str(support / ".env"),
            "GATEWAY_FLEET": str(data / "fleet.json"), "GATEWAY_VOICES": str(data / "voices.json"),
            "GATEWAY_VOICES_OUT": str(data / "voices.json"), "GATEWAY_NO_MIGRATE": "1",
            "STUDIOHUB_READY_FILE": str(support / "no-hub.json"), "GATEWAY_API_KEY": "render-fixture-key",
            "GATEWAY_BUILD": "render-fixture",
            "GATEWAY_BACKUP_ICLOUD": str(support / "CloudDocs/KH Backups"),
            "GATEWAY_BACKUP_LOCAL": str(support / "_backups"), "GATEWAY_BACKUP_VOLUMES": str(support / "Volumes"),
            "GATEWAY_BACKUP_TRASH": str(support / "trash"), "GATEWAY_BACKUP_HUB_DIR": str(support / "hub")}


def seed_jobs(env: dict) -> None:
    os.environ.update(env)
    from gateway.store import JobStore  # imported late: it reads the environment at import
    store = JobStore(env["GATEWAY_DB"])
    now = time.time()
    for modality, model, provider, state, minutes, caller, error in JOBS:
        row, _ = store.submit(modality=modality, model_id=model, provider_id=provider, caller=caller,
                              idempotency_key=None,
                              params={"prompt": "A lighthouse at dusk" if modality == "image" else
                                      "Chapter fourteen begins in the rain." if modality == "tts" else "Hello"})
        if state == "succeeded":
            store.succeed(row["id"], media_type={"tts": "audio/wav", "image": "image/png", "music": "audio/mpeg",
                                                 "transcription": "text/vtt"}.get(modality, "text/plain"),
                          result={"usage": {"input_tokens": 420, "output_tokens": 180}} if modality == "chat" else None)
        elif state == "failed":
            store.fail(row["id"], code=error[0], detail=error[1])
        else:
            store.cancel(row["id"])
        created = now - minutes * 60
        with sqlite3.connect(env["GATEWAY_DB"]) as conn:
            conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE id=?", (created, created + 40, row["id"]))
    store.close()


class Overlay(BaseHTTPRequestHandler):
    gateway_port = 0
    fleet_body = b"{}"

    def log_message(self, *args):
        pass

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway_port, timeout=30)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "connection"}}
        connection.request(self.command, self.path, body=body, headers=headers)
        reply = connection.getresponse()
        payload = reply.read()
        if self.command == "GET" and self.path.split("?")[0] == "/admin/api/status":
            try:  # the fixture fleet has a running Hub; the isolated gateway has none
                status = json.loads(payload)
                status["hub"] = {"running": True, "advertised": True, "base_url": "http://127.0.0.1:47873"}
                payload = json.dumps(status).encode()
            except ValueError:
                pass
        self.send_response(reply.status)
        for key, value in reply.getheaders():
            if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.split("?")[0] == "/admin/api/fleet":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.fleet_body)))
            self.end_headers()
            self.wfile.write(self.fleet_body)
            return
        self._proxy()

    do_POST = do_PUT = do_PATCH = do_DELETE = _proxy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--support", type=Path, required=True)
    args = parser.parse_args()
    args.support.mkdir(parents=True, exist_ok=True)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    env = seed(args.support, upstream.server_address[1])
    seed_jobs(env)
    gateway_port = free_port()
    env.update({"GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(gateway_port)})
    child_env = dict(os.environ, **env)
    log = (args.support / "gateway.log").open("w")
    gateway = subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1",
                                "--port", str(gateway_port), "--no-proxy-headers"],
                               cwd=ROOT, env=child_env, stdout=log, stderr=log)

    def stop(*_):
        gateway.terminate()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for _ in range(150):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=2)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                break
        except OSError:
            time.sleep(0.2)
    else:
        print((args.support / "gateway.log").read_text())
        raise SystemExit("gateway did not start")

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=30)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        reply = conn.getresponse()
        return json.loads(reply.read() or b"{}")

    for pid, (_, _, models) in PROVIDERS.items():
        call("POST", f"/admin/api/providers/{pid}/credentials", {"values": {"api_key": "mock-only"}})
        call("POST", f"/admin/api/providers/{pid}/refresh")
        found = [row["model_id"] for row in call("GET", f"/admin/api/providers/{pid}/models").get("models", [])]
        keep = [m for index, m in enumerate(found) if index % 4 != 3]  # leave some off
        call("POST", f"/admin/api/providers/{pid}/models/enabled", {"model_ids": keep, "enabled": True})

    call("POST", "/admin/api/backup/run")  # so Settings → Backups shows a real last backup

    fleet_json = subprocess.run([sys.executable, str(HERE / "fleet_fixture.py")], cwd=ROOT, capture_output=True,
                                check=True).stdout
    Overlay.gateway_port, Overlay.fleet_body = gateway_port, fleet_json
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Overlay)
    print(f"READY {args.support}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
