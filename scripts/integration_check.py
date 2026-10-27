#!/usr/bin/env python3
"""Gateway -> Studio Hub -> Story integration check.

Run this **after a release** (owner, 2026-10-26: "a few times then it's no longer
needed once we are stable"). It is not a scheduled job and it never touches the
live Gateway on :8787, the owner's Hub data, the fleet, the mini, or this Mac's
real Studio Hub.

What it does
------------
1. Starts a throwaway Studio Hub and a throwaway Gateway, isolated to temporary
   directories and a free loopback port. The Hub is built from the installed
   ``/Applications/Studio Hub.app`` workers (image + voice only), with a
   copy-on-write clone of just the two model packages the jobs need - an APFS
   clone, so it costs no meaningful disk and no download.
2. Pairs them the only way the real apps pair: the Gateway reads the Hub's
   ``hub-ready.json``; credentials are generated for this run.
3. Drives one tiny image job through ``POST /v1/images/generations``, one short
   TTS clip through ``POST /v1/audio/jobs``, and one final-render request through
   ``POST /v1/final-renders``. Image and TTS must complete; a final render may
   complete or be refused with a stated reason (a single local Hub has no remote
   render node, which is the honest outcome).
4. Prints a PASS/FAIL table and exits non-zero on failure.

The cross-app defects this is shaped to catch
---------------------------------------------
* a Hub/worker machine identity the Gateway will not route to ("local");
* a packaged runtime missing ``python-multipart`` (the image/voice workers'
  multipart routes);
* blob-state spellings that disagree with Story's wire contract;
* a render profile whose ``working_fps`` the Gateway's own bounds refuse.

Isolation and cleanup
---------------------
The lab is ONE reused directory (``$TMPDIR/studiohub-integration-check``).
A lock file prevents two runs at once. On exit - including on failure - every
child process is stopped and the lab directory is moved into
``~/Developer/_trash/integration-check/<timestamp>``. Nothing is ever deleted.
An abandoned directory is moved to the same place at the start of the next run.

Usage
-----
    /path/to/gateway/.venv/bin/python scripts/integration_check.py
    python3 scripts/integration_check.py --app "/Applications/Studio Hub.app"
    ... --gateway-root ~/Developer/gateway-kh/repository
                            # from a worktree: the built checkout holds the generated
                            # data/final-render profile (it is not committed)
    ... --setup-only        # pairing + model discovery only, no model jobs
    ... --skip-render
    ... --keep              # leave the lab directory in place (for debugging)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
import wave
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = Path("/Applications/Studio Hub.app")
DEFAULT_MODELS = Path.home() / "StudioHubModels"
DEFAULT_HUB_CHECKOUT = Path.home() / "Developer" / "studio-hub-kh"
TRASH_DEFAULT = Path.home() / "Developer" / "_trash" / "integration-check"
LAB_NAME = "studiohub-integration-check"

TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}
TERMINAL_RENDER_STATES = {"output_ready", "acknowledged", "failed", "cancelled", "stopped"}
BLOB_STATES = {"missing", "uploading", "complete"}
# The Gateway's own profile bounds allow these; a packet naming another fps is
# what the 2026-10-25 render-profile mismatch looked like on the wire.
ALLOWED_FPS = {24, 25, 30, 60}


# ---------------------------------------------------------------------------
# small HTTP client (stdlib only: the check must not need a second dependency)
# ---------------------------------------------------------------------------
def http(method: str, url: str, *, token: str | None = None, body: bytes | None = None,
         headers: dict[str, str] | None = None, timeout: float = 30.0,
         raw: bool = False):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "*/*" if raw else "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, (payload if raw else _json(payload)), dict(response.headers)
    except urllib.error.HTTPError as error:
        payload = error.read()
        return error.code, (payload if raw else _json(payload)), dict(error.headers)


def _json(payload: bytes):
    try:
        return json.loads(payload or b"null")
    except ValueError:
        return {"raw": payload[:500].decode("utf-8", "replace")}


def _error_code(body) -> str | None:
    """The machine code, from either error envelope shape the Gateway uses."""
    if not isinstance(body, dict):
        return None
    for key in ("code", "failure_code"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    error = body.get("error")
    if isinstance(error, dict):
        value = error.get("code")
        if isinstance(value, str) and value:
            return value
    return None


def _error_text(status: int, body) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message.strip():
                return message.strip()
            if isinstance(error.get("code"), str):
                return error["code"]
        for key in ("detail", "message", "failure_reason", "reason"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        code = _error_code(body)
        if code:
            return code
        return json.dumps(body)[:400]
    return str(body)[:400]


# ---------------------------------------------------------------------------
# result table
# ---------------------------------------------------------------------------
class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, float, str]] = []

    def record(self, step: str, ok: bool, started: float, detail: str) -> None:
        self.rows.append((step, "PASS" if ok else "FAIL", time.monotonic() - started, detail))
        print(f"  {('PASS' if ok else 'FAIL'):4}  {step}: {detail}", flush=True)

    def fail(self, step: str, started: float, detail: str) -> None:
        self.record(step, False, started, detail)

    def passed(self) -> bool:
        return bool(self.rows) and all(row[1] == "PASS" for row in self.rows)

    def table(self) -> str:
        if not self.rows:
            return "(no checks ran)"
        step_width = max(len(row[0]) for row in self.rows)
        lines = [f"{'STEP'.ljust(step_width)}  RESULT  SECONDS  DETAIL"]
        for step, result, seconds, detail in self.rows:
            lines.append(f"{step.ljust(step_width)}  {result:6}  {seconds:7.1f}  {detail}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# process and path helpers
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def directory_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def move_to_trash(source: Path, trash_root: Path) -> Path | None:
    """Move, never delete (the repo's disk rule)."""
    if not source.exists():
        return None
    trash_root.mkdir(parents=True, exist_ok=True)
    target = trash_root / f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    shutil.move(str(source), str(target))
    return target


def clone_packages(source: Path, destination: Path, packages: list[str]) -> None:
    """Copy-on-write clone of just the packages the jobs need.

    The same shape ``qualification/library_guard.py`` uses: APFS ``cp -Rcp`` (a
    clone shares the bytes) and the source library's install receipts, because
    the Hub verifies a package through its receipt, not through the folder name.
    """
    destination.mkdir(parents=True, exist_ok=True)
    state = destination / ".studiohub"
    state.mkdir(mode=0o700, exist_ok=True)
    try:
        source_index = json.loads((source / ".studiohub" / "installed.json").read_text())
    except (OSError, ValueError):
        source_index = {}
    kept = {}
    for package in packages:
        if Path(package).name != package or not (source / package).is_dir():
            raise SystemExit(f"model package {package!r} is not in {source}")
        subprocess.run(["/bin/cp", "-Rcp", str(source / package), str(destination / package)],
                       check=True)
        if package in source_index:
            kept[package] = source_index[package]
    index = state / "installed.json"
    index.write_text(json.dumps(kept, sort_keys=True))
    os.chmod(state, 0o700)
    os.chmod(index, 0o600)


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o600)


def wait_for_ready(name: str, ready: Path, process: subprocess.Popen,
                   log: Path, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited with {process.returncode}:\n{_tail(log)}")
        try:
            return json.loads(ready.read_text())
        except (OSError, ValueError):
            time.sleep(0.2)
    raise RuntimeError(f"{name} was not ready within {timeout:.0f}s:\n{_tail(log)}")


def _tail(path: Path, limit: int = 3000) -> str:
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return "(no log)"


class Stack:
    """Owns every child process started by one run."""

    def __init__(self, lab: Path | None = None) -> None:
        self.processes: list[tuple[str, subprocess.Popen, object]] = []
        self.lab = lab

    def spawn(self, name: str, command: list[str], *, env: dict[str, str], cwd: Path,
              log_dir: Path) -> subprocess.Popen:
        log = open(log_dir / f"{name}.log", "wb")
        process = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((name, process, log))
        if self.lab is not None:
            # For the next run's stale-process sweep, if this run is killed before
            # its own ``finally`` can stop the children.
            pids = [item.pid for _, item, _ in self.processes]
            try:
                (self.lab / "pids.json").write_text(json.dumps(pids))
            except OSError:
                pass
        return process

    def stop_one(self, name: str) -> bool:
        """Stop one worker (all of its group) and forget it.

        Used between the two model jobs: the owner's rule is one model at a
        time, and terminating the worker is the only release that cannot race a
        lazy idle timer.
        """
        for index, (current, process, log) in enumerate(self.processes):
            if current != name:
                continue
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                process.wait(timeout=5)
            try:
                log.close()
            except Exception:  # noqa: BLE001
                pass
            self.processes.pop(index)
            return True
        return False

    def stop(self) -> None:
        for name, process, log in reversed(self.processes):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 10
        for name, process, log in reversed(self.processes):
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
        for name, process, log in self.processes:
            try:
                log.close()
            except Exception:  # noqa: BLE001 - a log handle must not mask the result
                pass
        self.processes.clear()


# ---------------------------------------------------------------------------
# model choice
# ---------------------------------------------------------------------------
#: Package-name fragments that name a support file, not a generation model.
#: `s3tokenizerv2` and `pocket-tts-tokenizer` share the Voice engine but are
#: accessories; the Hub never publishes them as TTS models.
_ACCESSORY = ("tokenizer", "-vae", "preset-voices")


def _manifest_modality(manifest: dict) -> str | None:
    engine = str(manifest.get("engine") or "").strip()
    package = str(manifest.get("package_id") or "").lower()
    model = str(manifest.get("model_id") or "").lower()
    if any(fragment in package or fragment in model for fragment in _ACCESSORY):
        return None
    if "whisper" in package or "whisper" in model:
        return None                       # transcription, not speech
    if engine.startswith("image-"):
        return "image"
    # voice-mlx is the family Breeze, Chatterbox and the Qwen3-TTS ports use.
    if engine == "voice-mlx":
        return "tts"
    return None


def choose_models(app_resources: Path, source_library: Path) -> tuple[dict, dict]:
    """Smallest installed, production-allowed image and TTS package.

    "Smallest" is measured from the installed bytes; the manifest supplies the
    identity the Gateway will publish, never a guessed name.
    """
    manifests = {}
    for path in sorted((app_resources / "model-manifests").glob("*.json")):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        modality = _manifest_modality(value)
        package = str(value.get("package_id") or "")
        if modality and package and value.get("production_allowed") is not False:
            if (source_library / package).is_dir():
                manifests.setdefault(modality, []).append((directory_size(source_library / package),
                                                           value))
    chosen = {}
    for modality in ("image", "tts"):
        candidates = sorted(manifests.get(modality) or [], key=lambda item: item[0])
        if not candidates:
            raise SystemExit(
                f"no installed {modality} model: put one in {source_library} or pass --models")
        _, manifest = candidates[0]
        chosen[modality] = {
            "package_id": manifest["package_id"],
            "model_id": manifest["model_id"],
            "display_name": manifest.get("display_name") or manifest.get("model_id"),
            "size": candidates[0][0],
        }
    return chosen["image"], chosen["tts"]


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------
def run(args) -> int:
    results = Results()
    started_all = time.monotonic()

    app = Path(args.app).expanduser().resolve()
    resources = app / "Contents" / "Resources"
    if not (resources / "components/hub/standalone.py").is_file():
        checkout = Path(args.hub_checkout).expanduser().resolve()
        if (checkout / "components/hub/standalone.py").is_file() and \
                (checkout / "runtimes/voice/bin/python3").is_file():
            resources = checkout
        else:
            raise SystemExit(
                f"no runnable Studio Hub bundle at {app}. Install Studio Hub, or pass --app. "
                f"A checkout without runtimes cannot supply the packaged workers.")
    for required in ("voice_worker.py", "image_worker.py", "runtimes/voice/bin/python3",
                     "runtimes/image/bin/python3", "release-manifest.json", "model-manifests"):
        if not (resources / required).exists():
            raise SystemExit(f"Studio Hub bundle is missing {required}: {resources}")

    source_library = Path(args.models).expanduser().resolve()
    if not source_library.is_dir():
        raise SystemExit(f"model library is unavailable: {source_library}")
    image_model, tts_model = choose_models(resources, source_library)

    gateway_root = (Path(args.gateway_root).expanduser().resolve()
                    if args.gateway_root else REPO_ROOT)
    if not (gateway_root / "gateway/api.py").is_file():
        raise SystemExit(f"no gateway package under --gateway-root {gateway_root}")

    python = Path(args.python).expanduser() if args.python else (
        gateway_root / ".venv" / "bin" / "python"
        if (gateway_root / ".venv/bin/python").exists() else Path(sys.executable))
    probe = subprocess.run([str(python), "-c", "import uvicorn, httpx, fastapi"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise SystemExit(
            f"{python} cannot import the Gateway's dependencies (uvicorn/httpx/fastapi).\n"
            f"Run this script with the gateway checkout's .venv python, or pass --python.\n"
            f"{probe.stderr.strip()[-600:]}")

    lab = Path(args.lab).expanduser() / LAB_NAME
    lock_path = lab.with_name(lab.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another integration check is already running ({lock_path})")

    # An abandoned lab from a previous run is moved aside, never reused. If a
    # previous process is somehow still alive, stop only the PIDs it recorded and
    # only when their command still names this lab directory.
    if lab.exists():
        _stop_stale(lab)
        move_to_trash(lab, Path(args.trash))
    lab.mkdir(parents=True, exist_ok=True)
    os.chmod(lab, 0o700)

    stack = Stack(lab)
    try:
        token = secrets.token_urlsafe(32)
        hub_data = lab / "hub"
        hub_data.mkdir(mode=0o700)
        write_private(hub_data / ".hub_token", token + "\n")

        library = lab / "library"
        started = time.monotonic()
        clone_packages(source_library, library,
                       [image_model["package_id"], tts_model["package_id"]])
        results.record("models: clone library", True, started,
                       f"{image_model['package_id']} + {tts_model['package_id']} "
                       f"(APFS clone, source left untouched)")

        # -- workers ---------------------------------------------------------
        started = time.monotonic()
        base_env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("PYTHON", "HF_", "HUGGINGFACE_", "STUDIOHUB_",
                                           "KH_STUDIO_", "PINOKIO_", "DYLD_"))}
        workers = []
        for name, runtime, script in (
            ("image", "runtimes/image", "image_worker.py"),
            ("voice", "runtimes/voice", "voice_worker.py"),
        ):
            state = lab / f"{name}-state"
            state.mkdir(mode=0o700)
            if name == "image":
                # Force the model's bounded low-RAM configuration, the owner's
                # "smallest qualified model, bounded low-memory mode" request.
                image_data = state / "image"
                image_data.mkdir(mode=0o700)
                write_private(image_data / "loading_mode.json", '{"mode": "low"}\n')
            write_private(state / ".hub_token", token + "\n")
            ready = state / "ready.json"
            command = [str(resources / runtime / "bin/python3"), "-I", "-B",
                       str(resources / script),
                       "--data-dir", str(state), "--models-dir", str(library),
                       "--ready-file", str(ready), "--hub-token-file", str(state / ".hub_token"),
                       "--port", "0", "--offline"]
            process = stack.spawn(name, command, env=base_env, cwd=state, log_dir=lab)
            info = wait_for_ready(name, ready, process, lab / f"{name}.log")
            workers.append({
                "id": name, "title": f"{name.title()} Studio",
                "modality": "image" if name == "image" else "voice",
                "host": "127.0.0.1", "port": urlsplit(info["url"]).port,
                "machine": "local", "local": True,
                "hub_token": token, "studio_token": token,
                "runtime_identity": {"id": f"{name}-runtime", "compatible": True},
                "managed_output_roots": [str(state / "output")],
            })
        write_private(hub_data / "workers.json", json.dumps(workers))
        results.record("workers: packaged image + voice", True, started,
                       f"image {workers[0]['port']}, voice {workers[1]['port']}")

        # -- Hub -------------------------------------------------------------
        started = time.monotonic()
        serve_hub = lab / "serve_hub.py"
        serve_hub.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(resources)!r})\n"
            "from components.hub.standalone import app\n"
            "from voice_worker import run_server\n"
            f"run_server(app, 0, Path({str(hub_data / 'ready.json')!r}), "
            f"'integration-check', None, None, token={token!r}, host='127.0.0.1')\n")
        hub_env = {**base_env,
                   "STUDIOHUB_READY_FILE": str(hub_data / "ready.json"),
                   "STUDIOHUB_DATA_DIR": str(hub_data),
                   "STUDIOHUB_WORKERS_FILE": str(hub_data / "workers.json"),
                   "STUDIOHUB_MODELS_DIR": str(library),
                   "STUDIOHUB_MODEL_MANIFESTS": str(resources / "model-manifests"),
                   "STUDIOHUB_RELEASE_MANIFEST": str(resources / "release-manifest.json"),
                   "STUDIOHUB_PACKAGED": "1", "STUDIOHUB_REQUIRE_AUTH": "1"}
        hub_process = stack.spawn(
            "hub", [str(resources / "runtimes/voice/bin/python3"), "-I", "-B", str(serve_hub)],
            env=hub_env, cwd=hub_data, log_dir=lab)
        hub_info = wait_for_ready("hub", hub_data / "ready.json", hub_process, lab / "hub.log")
        hub_url = hub_info["url"].rstrip("/")
        results.record("hub: throwaway", True, started,
                       f"{hub_url} (data {hub_data}, models {library})")

        # -- Gateway ---------------------------------------------------------
        started = time.monotonic()
        port = free_port()
        if port == 8787:
            raise SystemExit("refusing to use the live Gateway port 8787")
        gateway_data = lab / "gateway"
        (gateway_data / "data").mkdir(parents=True)
        gateway_env = {**base_env,
                       "GATEWAY_APP_SUPPORT": str(gateway_data),
                       "GATEWAY_DATA": str(gateway_data / "data"),
                       "GATEWAY_ARTIFACTS": str(gateway_data / "data/artifacts"),
                       "GATEWAY_DB": str(gateway_data / "data/gateway.sqlite3"),
                       "GATEWAY_CREDENTIALS": str(gateway_data / "data/credentials.json"),
                       "GATEWAY_MODEL_CACHE": str(gateway_data / "data/models_cache.json"),
                       "GATEWAY_ENV_FILE": str(gateway_data / "gateway.env"),
                       "GATEWAY_FLEET": str(gateway_data / "fleet.json"),
                       "GATEWAY_VOICES": str(gateway_data / "voices.json"),
                       "GATEWAY_VOICES_OUT": str(gateway_data / "voices.json"),
                       "GATEWAY_NO_MIGRATE": "1",
                       "STUDIOHUB_READY_FILE": str(hub_data / "ready.json"),
                       # Explicit overrides, so a missing ready file can never fall
                       # back to this Mac's real Hub.
                       "STUDIOHUB_BASE_URL": hub_url,
                       "STUDIOHUB_HUB_TOKEN": token,
                       "GATEWAY_API_KEY": secrets.token_urlsafe(32),
                       "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(port)}
        key = gateway_env["GATEWAY_API_KEY"]
        gateway_process = stack.spawn(
            "gateway", [str(python), "-c",
                        "import uvicorn,sys; uvicorn.run('gateway.api:app',"
                        "host='127.0.0.1',port=int(sys.argv[1]),log_level='warning')",
                        str(port)],
            env=gateway_env, cwd=gateway_root, log_dir=lab)
        gateway_url = f"http://127.0.0.1:{port}"
        _wait_http(gateway_url + "/health", 120)
        results.record("gateway: throwaway", True, started,
                       f"{gateway_url} (data {gateway_data}, api key generated)")

        # -- pairing ---------------------------------------------------------
        started = time.monotonic()
        status, body, _ = http("GET", hub_url + "/api/health", timeout=10)
        hub_machine = ""
        if status != 200:
            results.fail("pairing: hub health", started, _error_text(status, body))
        else:
            # The Hub's monitor polls its workers; a slot count of 0 in the first
            # second is "not measured yet", not "no capacity". Give it one monitor
            # cycle before calling the pairing unhealthy.
            deadline = time.monotonic() + 30
            rows: list[dict] = []
            while time.monotonic() < deadline:
                status, caps, _ = http("GET", hub_url + "/api/hub/capabilities",
                                       token=token, timeout=30)
                rows = [worker for worker in (caps.get("workers") or [])
                        if worker.get("service_id") in ("image", "voice")]
                if status == 200 and len(rows) >= 2 and all(
                        row.get("online") and row.get("ready")
                        and int((row.get("available_capacity") or {}).get("slots") or 0) >= 1
                        for row in rows):
                    break
                time.sleep(1)
            if status != 200 or not rows:
                results.fail("pairing: hub capability", started,
                             _error_text(status, caps) if status != 200
                             else "the Hub published no image/voice workers")
            else:
                hub_machine = str(rows[0].get("physical_machine_id") or "")
                ready = all(row.get("online") and row.get("ready") for row in rows)
                results.record("pairing: hub capability", ready, started,
                               f"{len(rows)} workers, machine {hub_machine!r}, "
                               f"slots {[row.get('available_capacity', {}).get('slots') for row in rows]}")

        # -- packaged runtime dependencies ----------------------------------
        started = time.monotonic()
        missing = []
        for name, runtime in (("image", "runtimes/image"), ("voice", "runtimes/voice")):
            probe = subprocess.run(
                [str(resources / runtime / "bin/python3"), "-I", "-c",
                 # Import it, do not just find it: the recorded defect was the
                 # package absent from the packaged runtime, and a broken install
                 # fails at import too.
                 "try:\n import multipart\nexcept ImportError:\n import python_multipart\n"],
                capture_output=True, text=True)
            if probe.returncode != 0:
                missing.append(name)
        results.record("runtime: python-multipart", not missing, started,
                       "image and voice runtimes import multipart" if not missing
                       else f"missing in {', '.join(missing)} runtime(s)")

        # -- models and fleet capacity --------------------------------------
        started = time.monotonic()
        image_row = tts_row = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status, listing, _ = http("GET", gateway_url + "/v1/models", token=key, timeout=60)
            for row in (listing.get("models") or []):
                row_id = str(row.get("id") or "")
                if not row_id.startswith("studio-hub"):
                    continue
                if row_id.endswith("::" + image_model["model_id"]):
                    image_row = row
                if row_id.endswith("::" + tts_model["model_id"]):
                    tts_row = row
            if image_row and tts_row:
                break
            time.sleep(2)
        if not image_row or not tts_row:
            seen = [row.get("id") for row in (listing.get("models") or [])]
            results.fail("models: gateway catalogue", started,
                         f"missing image={image_model['model_id']} or "
                         f"tts={tts_model['model_id']}; published: {seen}")
            return _finish(results)
        image_id = str(image_row["id"])
        tts_id = str(tts_row["id"])
        unavailable = [label for label, row in (("image", image_row), ("tts", tts_row))
                       if row.get("available") is not True]
        results.record("models: gateway catalogue", not unavailable, started,
                       f"image {image_id}, tts {tts_id}" if not unavailable
                       else f"published but unavailable: {', '.join(unavailable)}")
        started = time.monotonic()
        capacity_ok = True
        capacity_detail = []
        for modality, model in (("image", image_model), ("tts", tts_model)):
            status, capacity, _ = http(
                "GET", f"{gateway_url}/v1/fleet/capacity?modality={modality}&model={model['model_id']}",
                token=key, timeout=30)
            row = next((item for item in (capacity.get("models") or [])
                        if item.get("model_id") == model["model_id"]), None)
            if status != 200 or row is None:
                capacity_ok = False
                capacity_detail.append(f"{modality}: {_error_text(status, capacity)}")
            elif int(row.get("eligible_macs") or 0) < 1 or int(row.get("idle_macs") or 0) < 1:
                # This is the recorded cross-app defect: the Hub names a machine
                # ("local") that the Gateway will not route work to.
                capacity_ok = False
                capacity_detail.append(
                    f"{modality}: Hub machine {hub_machine!r} not routable "
                    f"(locations={row.get('locations')}, eligible={row.get('eligible_macs')}, "
                    f"idle={row.get('idle_macs')})")
            else:
                capacity_detail.append(f"{modality}: idle {row.get('idle_macs')} of "
                                       f"{row.get('eligible_macs')} Mac(s)")
        results.record("pairing: machine routable", capacity_ok, started,
                       "; ".join(capacity_detail))

        if unavailable or not capacity_ok:
            # A model the Gateway will not route to is the defect; starting a job
            # would only repeat the same failure against the queue.
            return _finish(results)

        if args.setup_only:
            results.record("jobs", True, time.monotonic(), "skipped (--setup-only)")
            return _finish(results)

        # -- image job -------------------------------------------------------
        if not args.skip_image:
            started = time.monotonic()
            body = json.dumps({
                "model_id": image_id,
                "prompt": "A small red apple on a plain white table, soft light",
                "aspect_ratio": "1:1", "resolution": "1K", "seed": 7,
            }).encode()
            status, job, _ = http("POST", gateway_url + "/v1/images/generations", token=key,
                                  body=body, headers={
                                      "Content-Type": "application/json",
                                      "Idempotency-Key": f"integration-image-{uuid.uuid4().hex}"},
                                  timeout=60)
            if status not in (200, 202):
                results.fail("image job", started, _error_text(status, job))
            else:
                try:
                    job = _wait_job(gateway_url + f"/v1/images/{job['id']}", key,
                                    args.image_timeout)
                except RuntimeError as exc:
                    results.fail("image job", started, str(exc))
                    job = None
                if job is not None and job.get("state") != "succeeded":
                    results.fail("image job", started, f"{job.get('state')}: "
                                 f"{_error_text(200, job)}")
                elif job is not None:
                    status, payload, _ = http(
                        "GET", gateway_url + f"/v1/images/{job['id']}/file", token=key,
                        timeout=120, raw=True)
                    magic = payload[:12] if isinstance(payload, bytes) else b""
                    good = status == 200 and len(payload) > 0 and (
                        magic.startswith(b"\x89PNG\r\n\x1a\n") or magic.startswith(b"\xff\xd8\xff")
                        or magic[:4] == b"RIFF" and payload[8:12] == b"WEBP")
                    stated_machine = job.get("hub_machine")
                    named = stated_machine in (None, "", hub_machine)
                    results.record("image job", good and named, started,
                                   f"{job.get('state')}, {len(payload) if isinstance(payload, bytes) else 0} bytes, "
                                   f"machine {stated_machine or hub_machine!r}"
                                   if good and named else
                                   f"HTTP {status}, {len(payload) if isinstance(payload, bytes) else 0} bytes, "
                                   f"machine {stated_machine!r} vs Hub {hub_machine!r}")

        # -- TTS job ---------------------------------------------------------
        if not args.skip_image:
            # One model at a time: the Image worker's model must be gone before
            # the Voice worker loads one.
            stopped = stack.stop_one("image")
            results.record("memory: image worker stopped", stopped, time.monotonic(),
                           "model memory freed before the TTS model loads"
                           if stopped else "image worker was already gone")

        if not args.skip_tts:
            started = time.monotonic()
            body = json.dumps({
                "model_id": tts_id,
                "text": "Integration check, one short line.",
                "output_format": "wav",
            }).encode()
            status, job, _ = http("POST", gateway_url + "/v1/audio/jobs", token=key,
                                  body=body, headers={
                                      "Content-Type": "application/json",
                                      "Idempotency-Key": f"integration-tts-{uuid.uuid4().hex}"},
                                  timeout=60)
            if status not in (200, 202):
                results.fail("tts job", started, _error_text(status, job))
            else:
                try:
                    job = _wait_job(gateway_url + f"/v1/jobs/{job['id']}", key, args.tts_timeout)
                except RuntimeError as exc:
                    results.fail("tts job", started, str(exc))
                    job = None
                if job is not None and job.get("state") != "succeeded":
                    results.fail("tts job", started, f"{job.get('state')}: "
                                 f"{_error_text(200, job)}")
                elif job is not None:
                    status, payload, _ = http(
                        "GET", gateway_url + f"/v1/jobs/{job['id']}/audio", token=key,
                        timeout=120, raw=True)
                    good = status == 200 and isinstance(payload, bytes) and len(payload) > 0 and (
                        payload[:4] == b"RIFF" or payload[:4] == b"\x00\x00\x00\x14"
                        or payload[4:8] == b"ftyp")
                    stated_machine = job.get("hub_machine")
                    named = stated_machine in (None, "", hub_machine)
                    results.record("tts job", good and named, started,
                                   f"{job.get('state')}, {len(payload) if isinstance(payload, bytes) else 0} bytes, "
                                   f"machine {stated_machine or hub_machine!r}"
                                   if good and named else
                                   f"HTTP {status}, {len(payload) if isinstance(payload, bytes) else 0} bytes, "
                                   f"machine {stated_machine!r} vs Hub {hub_machine!r}")

        if not args.skip_tts:
            stopped = stack.stop_one("voice")
            results.record("memory: voice worker stopped", stopped, time.monotonic(),
                           "model memory freed before the render check"
                           if stopped else "voice worker was already gone")

        # -- final render ----------------------------------------------------
        if not args.skip_render:
            if not (gateway_root / "data/final-render/profile.json").is_file():
                results.fail("render: profile", time.monotonic(),
                             f"no bundled final-render profile under {gateway_root}/data/"
                             f"final-render; run with --gateway-root pointing at the built "
                             f"checkout (the profile is generated, not committed)")
            else:
                _render_check(results, gateway_url, key, hub_url, token, hub_machine,
                              gateway_root)

        return _finish(results)
    except Exception as exc:  # noqa: BLE001 - the table is the product; never a traceback dump
        results.fail("check aborted", started_all, f"{type(exc).__name__}: {exc}")
        return _finish(results)
    finally:
        stack.stop()
        if not args.keep:
            try:
                moved = move_to_trash(lab, Path(args.trash))
                if moved:
                    print(f"\ntrash: {moved}", flush=True)
            except OSError as exc:
                print(f"\nWARNING: could not move {lab} to trash: {exc}", flush=True)
        lock.close()


def _finish(results: Results) -> int:
    print("\n" + results.table(), flush=True)
    print(f"\n{'PASS' if results.passed() else 'FAIL'}", flush=True)
    return 0 if results.passed() else 1


def _wait_http(url: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body, _ = http("GET", url, timeout=3)
            if status == 200:
                return body
            last = _error_text(status, body)
        except Exception as exc:  # noqa: BLE001 - a starting server refuses connections
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"no answer from {url} within {timeout:.0f}s: {last}")


def _wait_job(url: str, token: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        status, last, _ = http("GET", url, token=token, timeout=20)
        if status == 200 and last.get("state") in TERMINAL_JOB_STATES:
            return last
        time.sleep(2)
    raise RuntimeError(f"job did not finish within {timeout:.0f}s; last state "
                       f"{last.get('state')!r}: {_error_text(200, last)}")


def _wav_clip() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * 12000)          # 0.5 s of silence
    return output.getvalue()


def _render_check(results: Results, gateway_url: str, key: str, hub_url: str,
                  hub_token: str, hub_machine: str, gateway_root: Path) -> None:
    """One render request, including the blob flow and the status poll.

    A single local Hub has no remote render node, so a refused request with a
    stated reason is the honest PASS. An accepted one is followed to a terminal
    state and its machine named; a profile/fps mismatch is always a FAIL.
    """
    sys.path.insert(0, str(gateway_root))
    from gateway.render_hash_v1 import render_hash  # noqa: E402

    started = time.monotonic()
    story = str(uuid.uuid4())
    headers = {"X-Story-Instance": story}
    status, caps, _ = http("GET", gateway_url + "/v1/final-renders/capabilities",
                           token=key, headers=headers, timeout=30)
    digest = str((caps or {}).get("profile_digest") or "")
    if status != 200 or len(digest) != 64 or not (caps or {}).get("profile_descriptor_utf8"):
        results.fail("render: capabilities", started, _error_text(status, caps))
        return
    try:
        profile = json.loads(caps["profile_descriptor_utf8"])
    except ValueError:
        results.fail("render: capabilities", started, "profile_descriptor_utf8 is not JSON")
        return
    bounds = profile.get("execution_bounds") or {}
    fps_values = bounds.get("working_fps") or [24]
    bad_fps = [value for value in fps_values if value not in ALLOWED_FPS]
    if bad_fps:
        results.fail("render: profile fps", started, f"profile publishes unsupported fps {bad_fps}")
        return
    fps = 24 if 24 in fps_values else int(fps_values[0])
    if "canvas_range" in bounds:
        width, height = 1280, 720
        max_edge = int((bounds["canvas_range"] or {}).get("max_edge") or 0)
        max_pixels = int((bounds["canvas_range"] or {}).get("max_pixels") or 0)
        if max_edge and max(width, height) > max_edge:
            width, height = 640, 360
        if max_pixels and width * height > max_pixels:
            width, height = 320, 180
    else:
        pair = (bounds.get("canvas_pairs") or [{"width": 1280, "height": 720}])[0]
        width, height = int(pair["width"]), int(pair["height"])
    results.record("render: profile fps", True, started,
                   f"digest {digest[:12]}..., fps {fps}, canvas {width}x{height}")

    audio = _wav_clip()
    base = (b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 40)
    assets = {}
    blob_ok = True
    blob_detail = []
    blob_started = time.monotonic()
    for name, payload in (("audio", audio), ("base", base)):
        sha = hashlib.sha256(payload).hexdigest()
        status, blob, _ = http(
            "GET", f"{gateway_url}/v1/final-renders/blobs/{sha}/status"
                   f"?size_bytes={len(payload)}", token=key, headers=headers, timeout=30)
        if status != 200 or blob.get("state") not in BLOB_STATES:
            blob_ok = False
            blob_detail.append(f"{name} before: {_error_text(status, blob)}")
            continue
        if blob.get("state") != "missing":
            blob_ok = False
            blob_detail.append(f"{name} before: expected missing, got {blob.get('state')!r}")
        status, blob, _ = http(
            "PUT", f"{gateway_url}/v1/final-renders/blobs/{sha}"
                   f"?size_bytes={len(payload)}&offset=0", token=key, body=payload,
            headers={**headers, "Content-Type": "application/octet-stream",
                     "Content-Length": str(len(payload)),
                     "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"},
            timeout=60)
        if status != 200 or blob.get("state") != "complete" or not blob.get("asset_handle"):
            blob_ok = False
            blob_detail.append(f"{name} put: {_error_text(status, blob)}")
            continue
        assets[name] = blob["asset_handle"]
        status, again, _ = http(
            "GET", f"{gateway_url}/v1/final-renders/blobs/{sha}/status"
                   f"?size_bytes={len(payload)}", token=key, headers=headers, timeout=30)
        if status != 200 or again.get("state") != "complete" or again.get("offset") != len(payload):
            blob_ok = False
            blob_detail.append(f"{name} after: {_error_text(status, again)}")
        else:
            blob_detail.append(f"{name} missing->complete")
    results.record("render: blob states", blob_ok, blob_started,
                   "; ".join(blob_detail))
    if not blob_ok:
        results.fail("render request", blob_started, "blob staging failed")
        return

    recipe = {
        "namespace": "studio.render.final.v1", "profile_digest": digest,
        "base_video": {"type": "asset_ref", "logical_id": "base"},
        "premixed_audio": {"type": "asset_ref", "logical_id": "audio"},
        "canvas": {"width": width, "height": height, "working_fps": fps},
        "target_duration_sec": 1.0,
        "effects_gate": {"start_sec": None, "end_sec": None},
        "operations": [],
        "encode": {"container": "mp4", "requested_codec": "h264",
                   "requested_effort": "medium", "crf": 23,
                   "quality_cpu_preset": "medium", "bitrate_kbps": None},
    }
    manifest = [
        {"logical_id": "audio", "safe_name": "audio.wav", "role": "premixed_audio",
         "sha256": hashlib.sha256(audio).hexdigest(), "size_bytes": len(audio)},
        {"logical_id": "base", "safe_name": "base.mp4", "role": "base_video",
         "sha256": hashlib.sha256(base).hexdigest(), "size_bytes": len(base)},
    ]
    recipe_digest = render_hash("recipe", recipe)
    manifest_digest = render_hash("manifest", manifest)
    request = {
        "format": "studio.render.gateway-request.v1", "story_uuid": story,
        "intent_id": "integration-check", "attempt_no": 1,
        "packet": {"recipe": recipe, "manifest": manifest,
                   "recipe_digest": recipe_digest, "manifest_digest": manifest_digest,
                   "packet_digest": render_hash(
                       "packet", {"recipe_digest": recipe_digest,
                                  "manifest_digest": manifest_digest})},
        "assets": assets,
    }
    started = time.monotonic()
    status, job, _ = http("POST", gateway_url + "/v1/final-renders", token=key,
                          body=json.dumps(request, sort_keys=True,
                                          separators=(",", ":")).encode(),
                          headers={**headers, "Content-Type": "application/json",
                                   "Idempotency-Key": f"integration-render-{uuid.uuid4().hex}"},
                          timeout=60)
    if status == 409 and _error_code(job) == "RENDER_PROFILE_MISMATCH":
        results.fail("render request", started, "RENDER_PROFILE_MISMATCH: profile/fps refused")
        return
    if status == 202:
        node = str(job.get("destination_node") or job.get("machine") or "")
        deadline = time.monotonic() + 120
        final = job
        while time.monotonic() < deadline:
            if (final.get("state") in TERMINAL_RENDER_STATES
                    or final.get("remote_state") in TERMINAL_RENDER_STATES):
                break
            time.sleep(1)
            status, final, _ = http("GET", gateway_url + f"/v1/final-renders/{job['id']}",
                                    token=key, headers=headers, timeout=30)
        state = final.get("remote_state") or final.get("state")
        machine = str(final.get("destination_node") or final.get("machine") or node)
        # The Gateway named the Mac it dispatched to. "local" is a valid name
        # here when the Gateway accepted it; the defect this checks is a refusal
        # to route the name, and that is caught by the capacity row above.
        named = bool(machine)
        if state in ("output_ready", "acknowledged"):
            results.record("render request", named, started,
                           f"{state}, machine {machine!r}")
        elif state in ("failed", "cancelled", "stopped"):
            results.record("render request", True, started,
                           f"{state} with stated reason: {_error_text(200, final)}")
        else:
            results.fail("render request", started,
                         f"no terminal state after 120s (last {state!r}, machine {machine!r})")
        return
    if status == 503 and _error_code(job) == "RENDER_DESTINATION_UNAVAILABLE":
        # The successful, honest answer from a single local Hub: it has no remote
        # Mac that the Gateway is allowed to render on. The refusal must state it.
        hazard = ""
        _, caps_hub, _ = http("GET", hub_url + "/api/hub/capabilities",
                                        token=hub_token, timeout=30)
        render_rows = [row for row in (caps_hub.get("workers") or [])
                       if "video.final_render" in (row.get("supported_operations") or [])]
        if render_rows:
            hazard = (f"; Hub render row machine "
                      f"{render_rows[0].get('physical_machine_id')!r}, "
                      f"ready={render_rows[0].get('ready')}")
        results.record("render request", bool(_error_text(status, job)), started,
                       f"refused with stated reason RENDER_DESTINATION_UNAVAILABLE{hazard}")
        return
    results.fail("render request", started, _error_text(status, job))


def _stop_stale(lab: Path) -> None:
    """Stop processes recorded by an abandoned run, only when they are ours."""
    try:
        pids = json.loads((lab / "pids.json").read_text())
    except (OSError, ValueError):
        return
    for pid in pids:
        try:
            listing = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout
            pgid = os.getpgid(pid)
            if str(lab) in listing and pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", default=str(DEFAULT_APP),
                        help="Studio Hub.app to take the workers from")
    parser.add_argument("--hub-checkout", default=str(DEFAULT_HUB_CHECKOUT),
                        help="studio-hub-kh checkout, used only if the app is absent")
    parser.add_argument("--models", default=str(DEFAULT_MODELS),
                        help="installed model library to clone the two used packages from")
    parser.add_argument("--gateway-root", default="",
                        help="checkout to launch the Gateway from (default: this repo). "
                             "Point this at the main checkout when running from a worktree, "
                             "whose data/final-render profile is generated and not committed")
    parser.add_argument("--python", default="", help="python with the Gateway dependencies")
    parser.add_argument("--lab", default=str(Path(tempfile.gettempdir())),
                        help="parent for the single reusable lab directory")
    parser.add_argument("--trash", default=str(TRASH_DEFAULT),
                        help="where the finished lab directory is moved")
    parser.add_argument("--setup-only", action="store_true",
                        help="pair and discover models; run no generation")
    parser.add_argument("--skip-image", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--image-timeout", type=float, default=900.0)
    parser.add_argument("--tts-timeout", type=float, default=600.0)
    parser.add_argument("--keep", action="store_true",
                        help="leave the lab directory in place instead of moving it to trash")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
