"""Run a Node tool safely, with a pinned environment, a timeout and an output cap.

The private Content Pack signer is a Node script (`content_signing.mjs`), and this
is the only process runner the gateway needs for it: a missing or misbehaving Node
becomes an error rather than a hang or a memory spike. It was lifted out of the
retired pack-authoring module — nothing here reads, writes or authors a pack.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from . import config


_HOMEBREW_NODE = Path("/opt/homebrew/bin/node")
_SAFE_ENV = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE")


class NodeToolError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def node_executable() -> str:
    # The env var keeps its historical name so an existing `.env` is not broken;
    # it now only ever points at the Node used to run the signer.
    configured = config.get("GATEWAY_AUTHORING_NODE", "").strip()
    if configured:
        return configured
    found = shutil.which("node")
    if found:
        return found
    if _HOMEBREW_NODE.is_file():
        return str(_HOMEBREW_NODE)
    return "node"


def tool_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _SAFE_ENV if key in os.environ}
    environment.setdefault("PATH", os.defpath)
    return environment


def run_process(argv: list[str], input_bytes: bytes, environment: dict[str, str], *,
                timeout: float, output_limit: int) -> Any:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            close_fds=True,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise NodeToolError("The signing tool is unavailable.", 503) from exc

    stdout = bytearray()
    stderr = bytearray()
    total = 0
    guard = threading.Lock()
    too_large = threading.Event()

    def collect(pipe, target: bytearray) -> None:
        nonlocal total
        try:
            while True:
                chunk = pipe.read(64 * 1024)
                if not chunk:
                    return
                with guard:
                    if total + len(chunk) > output_limit:
                        too_large.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                        return
                    total += len(chunk)
                    target.extend(chunk)
        except (OSError, ValueError):
            return

    def send() -> None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    readers = [
        threading.Thread(target=collect, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=collect, args=(process.stderr, stderr), daemon=True),
    ]
    writer = threading.Thread(target=send, daemon=True)
    for thread in (*readers, writer):
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if too_large.is_set():
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise NodeToolError("The signing tool returned too much output.", 503)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait()
                raise NodeToolError("The signing tool timed out.", 503)
            try:
                process.wait(timeout=min(remaining, 0.05))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        for thread in (*readers, writer):
            thread.join(timeout=1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass
    if too_large.is_set():
        raise NodeToolError("The signing tool returned too much output.", 503)
    return type("Completed", (), {
        "returncode": process.returncode,
        "stdout": bytes(stdout),
        "stderr": bytes(stderr),
    })()
