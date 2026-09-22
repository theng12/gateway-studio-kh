"""Run a disposable Gateway + mock provider for browser/native UI inspection.

Prints its isolated support path and URL; Ctrl-C stops both subprocesses.
No provider credentials, restored data or live Hub are used.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Optional built GatewayKH executable to inspect")
    args = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="gateway-ui-fixture-") as temporary:
        support = Path(temporary)
        data = support / "data"
        data.mkdir()
        gateway_port, mock_port = free_port(), free_port()
        (data / "providers.local.json").write_text(json.dumps([{
            "id": "ui-fixture", "display_name": "Local test provider",
            "kind": "openai_compatible", "category": "language",
            "base_url": f"http://127.0.0.1:{mock_port}/v1",
            "modalities": ["chat", "image", "tts", "transcription"],
        }]))
        (data / "voices.json").write_text('{"voices": []}')
        (support / ".env").write_text(f"GATEWAY_API_KEY=ui-fixture-key\nGATEWAY_HOST=127.0.0.1\nGATEWAY_PORT={gateway_port}\n")
        (support / ".env").chmod(0o600)
        (support / "menubar.json").write_text(json.dumps({
            "gatewayDir": str(ROOT), "port": gateway_port, "autoStartGateway": True,
            "hasLaunchedBefore": True, "keepGatewayRunning": False,
        }))
        env = dict(os.environ)
        env.update({
            "GATEWAY_APP_SUPPORT": temporary, "GATEWAY_DATA": str(data),
            "GATEWAY_DB": str(data / "gateway.sqlite3"), "GATEWAY_ARTIFACTS": str(data / "artifacts"),
            "GATEWAY_CREDENTIALS": str(data / "credentials.json"),
            "GATEWAY_MODEL_CACHE": str(data / "models_cache.json"),
            "GATEWAY_ENV_FILE": str(support / ".env"), "GATEWAY_FLEET": str(data / "fleet.json"),
            "GATEWAY_VOICES": str(data / "voices.json"), "GATEWAY_VOICES_OUT": str(data / "voices.json"),
            "GATEWAY_NO_MIGRATE": "1", "STUDIOHUB_READY_FILE": str(support / "no-hub.json"),
            "GATEWAY_API_KEY": "ui-fixture-key", "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(gateway_port), "GATEWAY_BUILD": "isolated-ui-fixture",
            "GATEWAY_NATIVE_DEFAULTS_SUITE": "kh.gateway.ui-fixture." + support.name,
        })
        log = (support / "fixture.log").open("w")
        try:
            for command in ([sys.executable, "tests/mock_upstream.py", "--port", str(mock_port)],
                            [sys.executable, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1", "--port", str(gateway_port), "--no-proxy-headers"]):
                processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log))
            with httpx.Client(base_url=f"http://127.0.0.1:{gateway_port}", trust_env=False, timeout=10) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").is_success:
                            break
                    except httpx.TransportError:
                        time.sleep(.1)
                else:
                    raise RuntimeError((support / "fixture.log").read_text())
                response = client.post("/admin/api/providers/ui-fixture/credentials", json={"values": {"api_key": "mock-only"}})
                response.raise_for_status()
                response = client.post("/admin/api/providers/ui-fixture/refresh")
                response.raise_for_status()
                models = client.get("/admin/api/providers/ui-fixture/models").json()
                response = client.post("/admin/api/providers/ui-fixture/models/enabled", json={
                    "model_ids": [row["model_id"] for row in models.get("models", [])], "enabled": True,
                })
                response.raise_for_status()
            if args.app:
                processes.append(subprocess.Popen([str(args.app.resolve())], env=env, stdout=log, stderr=log))
            print(json.dumps({"support": temporary, "url": f"http://127.0.0.1:{gateway_port}/admin", "mock_port": mock_port}), flush=True)
            while all(process.poll() is None for process in processes):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            log.close()
            if args.app:
                subprocess.run(["/usr/bin/defaults", "delete", env["GATEWAY_NATIVE_DEFAULTS_SUITE"]],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
