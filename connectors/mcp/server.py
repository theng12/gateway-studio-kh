#!/usr/bin/env python3
"""GatewayKH agent connector — an MCP server that makes assets through this Gateway.

Any MCP client (Claude Code, Claude Desktop, another agent runtime) talks to this over
stdio; this talks to the Gateway over HTTP with **one guest API key** the owner issued.
The key is a guest, not an admin: free and local models by default, its own jobs and
files only, "Requested by <name> (guest)" on everything it starts.

**Standard library only, on purpose.** The connector ships inside the Gateway repository
and runs on whatever `python3` the agent's Mac has; an MCP SDK here would be a second
dependency to keep current for a protocol this small (JSON-RPC over newline-delimited
stdio). The Gateway's own API is the only thing it knows.

Configuration (environment, or the same names in the client's `env` block):

  GATEWAY_URL          e.g. http://100.x.y.z:8787 — the Gateway over Tailscale
  GATEWAY_GUEST_KEY    the key the owner showed once          (or:)
  GATEWAY_GUEST_KEY_SERVICE / GATEWAY_GUEST_KEY_ACCOUNT
                       read it from the macOS Keychain instead
  GATEWAY_OUTPUT_DIR   the only folder `download_result` may write into
                       (default ~/GatewayAssets)

**The key is never written to a file, never put in a URL, never printed.** It travels in
the `Authorization` header, and every message this server sends to the agent passes
through `_redact()` first.
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_NAME = "gateway-kh-agent-connector"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_GATEWAY = "http://127.0.0.1:8787"
CATALOGUE_TTL_S = 60.0
DEFAULT_WAIT_S = 60.0
MAX_WAIT_S = 600.0
TERMINAL_STATES = ("succeeded", "failed", "cancelled")

_KEY_CACHE: dict[str, str] = {}


class ConnectorError(Exception):
    """A refusal the agent should read as a sentence, with the Gateway's own words in it."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def gateway_url() -> str:
    value = (os.environ.get("GATEWAY_URL") or "").strip() or DEFAULT_GATEWAY
    return value.rstrip("/")


def guest_key() -> str:
    """The key, from the environment or the Keychain — read once, held in memory only."""
    if "value" in _KEY_CACHE:
        return _KEY_CACHE["value"]
    value = (os.environ.get("GATEWAY_GUEST_KEY") or "").strip()
    if not value:
        service = (os.environ.get("GATEWAY_GUEST_KEY_SERVICE")
                   or "gateway-kh-agent-connector").strip()
        account = (os.environ.get("GATEWAY_GUEST_KEY_ACCOUNT") or "guest").strip()
        try:
            answer = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if answer.returncode == 0:
                value = answer.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
    _KEY_CACHE["value"] = value
    return value


def output_root() -> Path:
    root = Path(os.environ.get("GATEWAY_OUTPUT_DIR")
                or (Path.home() / "GatewayAssets")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _redact(text: object) -> str:
    """Every character that leaves this process passes here; the key is never one of them."""
    value = str(text)
    key = _KEY_CACHE.get("value") or (os.environ.get("GATEWAY_GUEST_KEY") or "").strip()
    if key:
        value = value.replace(key, "<guest key>")
    return value


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _require_key() -> None:
    if not guest_key():
        raise ConnectorError(
            "No guest key is configured. Ask the Gateway's owner for one (Guests page → "
            "Create key), then set GATEWAY_GUEST_KEY, or store it in the Keychain with: "
            "security add-generic-password -s gateway-kh-agent-connector -a guest -w",
            code="NO_KEY",
        )


def _open(request: urllib.request.Request, timeout: float) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise ConnectorError(
            f"The Gateway at {gateway_url()} did not answer ({exc.reason}). Check that it is "
            "running and that this Mac can reach it over Tailscale.",
            code="GATEWAY_UNREACHABLE",
        ) from None
    except TimeoutError:
        raise ConnectorError(
            f"The Gateway at {gateway_url()} did not answer in time. The job may still be "
            "running — call get_job with the job id.",
            code="GATEWAY_TIMEOUT",
        ) from None


def _failure(status: int, body: bytes) -> ConnectorError:
    """The Gateway's own sentence, verbatim, with its code beside it."""
    detail, code = None, None
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
        if isinstance(parsed, dict):
            detail = parsed.get("detail") or parsed.get("error")
            code = parsed.get("code")
    except (ValueError, UnicodeDecodeError):
        detail = body.decode("utf-8", "replace").strip()[:500] or None
    if not detail:
        detail = f"The Gateway answered {status}."
    return ConnectorError(str(detail), code=str(code) if code else None, status=status)


def api(method: str, path: str, *, body: dict | None = None, raw: bytes | None = None,
        content_type: str = "application/json", accept: str = "application/json",
        timeout: float = 120.0) -> tuple[int, bytes, dict]:
    """One authenticated call. The key never appears in `path` or in any error we raise."""
    _require_key()
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    request = urllib.request.Request(gateway_url() + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + guest_key())
    request.add_header("Accept", accept)
    request.add_header("User-Agent", f"{SERVER_NAME}/{SERVER_VERSION}")
    if data is not None:
        request.add_header("Content-Type", content_type)
    status, payload, headers = _open(request, timeout)
    if status >= 400:
        raise _failure(status, payload)
    return status, payload, headers


def _json_object(payload: bytes) -> dict:
    try:
        parsed = json.loads(payload.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ConnectorError("The Gateway answered with something that is not JSON.",
                             code="GATEWAY_BAD_ANSWER") from None
    if not isinstance(parsed, dict):
        raise ConnectorError("The Gateway answered with something that is not a JSON object.",
                             code="GATEWAY_BAD_ANSWER")
    return parsed


def api_json(method: str, path: str, *, body: dict | None = None, timeout: float = 120.0) -> dict:
    _status, payload, _headers = api(method, path, body=body, timeout=timeout)
    return _json_object(payload)


def multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes,
              content_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8"))
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# The live catalogue — no model list is written down here (AGENTS §1)
# ---------------------------------------------------------------------------
_CATALOGUE: dict[str, object] = {"at": 0.0, "data": None, "error": None}


def catalogue(refresh: bool = False) -> dict | None:
    """The models this key may use, from the Gateway; `None` when it could not be read.

    **This never raises**, because `tools/list` calls it to describe the tools and a client
    whose Gateway is asleep must still be able to start the session. The refusal is kept in
    `_CATALOGUE["error"]`, and `list_models` hands it back verbatim when there is one.
    """
    now = time.time()
    if not refresh and _CATALOGUE["data"] is not None and now - float(_CATALOGUE["at"]) < CATALOGUE_TTL_S:
        return _CATALOGUE["data"]  # type: ignore[return-value]
    try:
        data = api_json("GET", "/admin/api/create/models")
    except ConnectorError as exc:
        _CATALOGUE["error"] = exc
        return None
    _CATALOGUE["data"] = data
    _CATALOGUE["at"] = now
    _CATALOGUE["error"] = None
    return data


def models_for(modality: str) -> list[dict]:
    data = catalogue() or {}
    by_modality = data.get("by_modality") if isinstance(data, dict) else None
    rows = by_modality.get(modality) if isinstance(by_modality, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _join(value) -> str:
    if isinstance(value, list):
        parts = []
        for item in value[:12]:
            if isinstance(item, dict):
                parts.append("/".join(str(item.get(key)) for key in ("width", "height")
                                      if item.get(key) is not None) or
                             (f'{item.get("min")}–{item.get("max")}s' if item.get("max") is not None
                              else json.dumps(item, separators=(",", ":"))))
            else:
                parts.append(str(item))
        return ", ".join(part for part in parts if part)
    return ""


def describe_model(row: dict) -> str:
    """One model in one line: its name, whether it costs, and what it publishes."""
    bits = [str(row.get("display_name") or row.get("id"))]
    if row.get("chargeable") is True:
        bits.append("paid")
    elif row.get("chargeable") is False:
        bits.append("free/local")
    for key, label in (("modes", "modes"), ("aspect_ratios", "aspect"), ("resolutions", "sizes"),
                       ("durations", "durations")):
        text = _join(row.get(key))
        if text:
            bits.append(f"{label}: {text}")
    for key, label in (("max_prompt_characters", "prompt chars"),
                       ("maximum_input_characters", "input chars"),
                       ("max_output_tokens", "max output tokens")):
        if row.get(key) is not None:
            bits.append(f"{label} {row[key]}")
    controls = row.get("controls")
    if isinstance(controls, list) and controls:
        names = []
        for control in controls[:12]:
            if isinstance(control, dict):
                names.append(str(control.get("name") or control.get("id") or "?"))
            else:
                names.append(str(control))
        bits.append("controls: " + ", ".join(names))
    return f'{row.get("id")} — ' + "; ".join(bits)


def model_list(modality: str, limit: int = 30) -> str:
    rows = models_for(modality)
    if not rows:
        return (f"No {modality} models are offered to this key right now. Call list_models "
                "for the live catalogue.")
    lines = [describe_model(row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… and {len(rows) - limit} more (call list_models).")
    return "\n".join(lines)


def model_id_schema(modality: str, description: str) -> dict:
    """`model_id`, described from the live catalogue: an enum when it can be read.

    The enum is convenience, never the rule — the Gateway refuses an unknown id itself, with
    its own sentence, and that sentence is what the agent is shown.
    """
    rows = models_for(modality)
    ids = [str(row.get("id")) for row in rows if row.get("id")]
    schema: dict = {"type": "string", "description": description + "\n" + model_list(modality)}
    if ids:
        schema["enum"] = ids
    return schema


def job_summary(job: dict, modality: str | None = None) -> dict:
    """What an agent needs to know now: id, honest state, and where its output is.

    **A field the Gateway did not publish is left out rather than filled with `null`** — the
    job envelope carries no modality, so a submit says which one it was and a later poll
    simply does not claim to know.
    """
    state = str(job.get("state") or "unknown")
    summary = {
        "job_id": job.get("id"),
        "state": state,
        "requested_by": (job.get("requested_by") or {}).get("label"),
        "done": state in TERMINAL_STATES,
        "outputs": [],
    }
    for key, value in (("modality", modality or job.get("modality")),
                       ("model_id", job.get("model_id"))):
        if value:
            summary[key] = value
    if job.get("failure_reason"):
        summary["failure_reason"] = job.get("failure_reason")
    if job.get("failure_code"):
        summary["failure_code"] = job.get("failure_code")
    if job.get("output_text") is not None:
        summary["output_text"] = job.get("output_text")
    for row in job.get("outputs") or []:
        if isinstance(row, dict):
            summary["outputs"].append({
                "asset_id": row.get("asset_id"),
                "media_type": row.get("media_type"),
                "bytes": row.get("bytes"),
                "present": row.get("present"),
            })
    if state not in TERMINAL_STATES:
        summary["next"] = ("wait_for_job {job_id} to wait a bounded time, or get_job to look "
                           "again. The job keeps running on the Gateway either way.")
    return summary


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _require_str(arguments: dict, name: str, what: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f'{what} is required: pass "{name}".', code="INVALID_REQUEST")
    return value.strip()


def _options(arguments: dict) -> dict:
    value = arguments.get("options") or {}
    if not isinstance(value, dict):
        raise ConnectorError("options must be a JSON object.", code="INVALID_REQUEST")
    return dict(value)


def _submit(modality: str, arguments: dict, params: dict) -> dict:
    model_id = _require_str(arguments, "model_id", "A model id")
    body = {"modality": modality, "model_id": model_id, "params": params,
            "idempotency_key": str(arguments.get("idempotency_key") or uuid.uuid4().hex)}
    answer = api_json("POST", "/admin/api/create", body=body)
    summary = job_summary(answer, modality)
    summary["created"] = answer.get("created")
    return summary


def tool_list_models(arguments: dict) -> dict:
    data = catalogue(refresh=bool(arguments.get("refresh")))
    if data is None:
        problem = _CATALOGUE.get("error")
        if isinstance(problem, ConnectorError):
            # The Gateway's own sentence — "no key", "that key is not valid", "did not answer".
            raise problem
        raise ConnectorError(
            "The Gateway's catalogue could not be read. Check the Gateway is running and this "
            "key is still valid (the owner can see and revoke keys on the Guests page).",
            code="CATALOGUE_UNREADABLE")
    wanted = arguments.get("modality")
    by_modality = data.get("by_modality") or {}
    modalities = [wanted] if wanted else list(by_modality)
    answer = {"paid_models": bool(data.get("paid_models")), "models": {}}
    for modality in modalities:
        rows = [row for row in (by_modality.get(modality) or []) if isinstance(row, dict)]
        answer["models"][modality] = rows
    if wanted and wanted not in by_modality:
        raise ConnectorError(
            f"No such modality: {wanted}. This key is offered "
            f"{', '.join(sorted(by_modality)) or 'no modalities'}.",
            code="INVALID_REQUEST")
    return answer


def tool_list_voices(arguments: dict) -> dict:
    query = {}
    for name in ("model_id", "collection", "search"):
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            query[name] = value.strip()
    path = "/admin/api/voices" + ("?" + urllib.parse.urlencode(query) if query else "")
    return api_json("GET", path)


def tool_generate_image(arguments: dict) -> dict:
    params = _options(arguments)
    params["prompt"] = _require_str(arguments, "prompt", "A prompt")
    return _submit("image", arguments, params)


def tool_generate_speech(arguments: dict) -> dict:
    params = _options(arguments)
    params["text"] = _require_str(arguments, "text", "The text to speak")
    if isinstance(arguments.get("voice_id"), str) and arguments["voice_id"].strip():
        params["voice_id"] = arguments["voice_id"].strip()
    return _submit("tts", arguments, params)


def tool_generate_music(arguments: dict) -> dict:
    params = _options(arguments)
    params["prompt"] = _require_str(arguments, "prompt", "A prompt")
    return _submit("music", arguments, params)


def tool_chat(arguments: dict) -> dict:
    params = _options(arguments)
    messages = arguments.get("messages")
    if isinstance(messages, list) and messages:
        params["input"] = messages
    elif isinstance(arguments.get("prompt"), str) and arguments["prompt"].strip():
        params["input"] = [{"role": "user", "content": arguments["prompt"].strip()}]
    else:
        raise ConnectorError('Pass "messages" (a list of {role, content}) or "prompt".',
                             code="INVALID_REQUEST")
    return _submit("chat", arguments, params)


def _upload(path_value: str, what: str) -> tuple[Path, bytes, str]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise ConnectorError(f"{what} {path} is not a file this Mac can read.",
                             code="FILE_NOT_READABLE")
    content = path.read_bytes()
    if not content:
        raise ConnectorError(f"{what} {path} is empty.", code="EMPTY_FILE")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, content, media_type


def tool_transcribe(arguments: dict) -> dict:
    path, content, media_type = _upload(_require_str(arguments, "file_path", "An audio file"),
                                        "The audio file")
    fields = {
        "model_id": _require_str(arguments, "model_id", "A model id"),
        "idempotency_key": str(arguments.get("idempotency_key") or uuid.uuid4().hex),
    }
    if isinstance(arguments.get("language"), str) and arguments["language"].strip():
        fields["language"] = arguments["language"].strip()
    if isinstance(arguments.get("options"), dict) and arguments["options"]:
        raise ConnectorError(
            "Transcription takes model_id, file_path and language only; the Gateway has no "
            "other field on this route.", code="INVALID_REQUEST")
    body, content_type = multipart(fields, "file", path.name, content, media_type)
    _status, payload, _headers = api("POST", "/admin/api/create/transcription", raw=body,
                                     content_type=content_type, timeout=300.0)
    return job_summary(_json_object(payload))


def tool_generate_video(arguments: dict) -> dict:
    """Image-to-video only: the scene image is a required field, never an optional one."""
    path, content, media_type = _upload(_require_str(arguments, "image_path", "A scene image"),
                                        "The scene image")
    fields = {
        "model_id": _require_str(arguments, "model_id", "A model id"),
        "prompt": str(arguments.get("prompt") or ""),
        "idempotency_key": str(arguments.get("idempotency_key") or uuid.uuid4().hex),
    }
    options = _options(arguments)
    if options:
        fields["options"] = json.dumps(options)
    body, content_type = multipart(fields, "file", path.name, content, media_type)
    _status, payload, _headers = api("POST", "/admin/api/create/video", raw=body,
                                     content_type=content_type, timeout=300.0)
    return job_summary(_json_object(payload))


def _status(job_id: str) -> dict:
    return api_json("GET", "/admin/api/create/status/"
                    + urllib.parse.quote(job_id, safe=""))


def tool_get_job(arguments: dict) -> dict:
    return job_summary(_status(_require_str(arguments, "job_id", "A job id")))


def tool_wait_for_job(arguments: dict) -> dict:
    """Wait a bounded time and tell the truth: finished, or still running, never "done"."""
    job_id = _require_str(arguments, "job_id", "A job id")
    try:
        timeout = float(arguments.get("timeout_seconds") or DEFAULT_WAIT_S)
    except (TypeError, ValueError):
        raise ConnectorError("timeout_seconds must be a number.", code="INVALID_REQUEST")
    timeout = max(1.0, min(timeout, MAX_WAIT_S))
    deadline = time.monotonic() + timeout
    job = _status(job_id)
    while str(job.get("state")) not in TERMINAL_STATES and time.monotonic() < deadline:
        pause = float(job.get("poll_after_ms") or 3000) / 1000.0
        time.sleep(max(0.2, min(pause, 5.0, deadline - time.monotonic())))
        job = _status(job_id)
    summary = job_summary(job)
    summary["waited_seconds"] = round(timeout - max(0.0, deadline - time.monotonic()), 1)
    if not summary["done"]:
        summary["next"] = (f"Still running after {summary['waited_seconds']} s. This is not a "
                           f"failure: call wait_for_job again or get_job to keep watching.")
    return summary


def _safe_target(folder: str, filename: str) -> Path:
    """Resolve inside GATEWAY_OUTPUT_DIR and nowhere else — `..`, absolute paths included."""
    root = output_root()
    target_dir = root if not folder else (root / folder).resolve()
    if target_dir != root and root not in target_dir.parents:
        raise ConnectorError(
            f'{folder!r} is outside the allowed output folder {root}. The connector writes '
            "only inside GATEWAY_OUTPUT_DIR.", code="OUTPUT_OUTSIDE_ROOT")
    name = Path(filename).name
    if not name or name in (".", ".."):
        raise ConnectorError("A filename is required.", code="INVALID_REQUEST")
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / name


def tool_download_result(arguments: dict) -> dict:
    job_id = _require_str(arguments, "job_id", "A job id")
    job = _status(job_id)
    outputs = [row for row in (job.get("outputs") or []) if isinstance(row, dict)]
    if str(job.get("state")) != "succeeded":
        raise ConnectorError(
            f"That job is {job.get('state')}, not finished; there is nothing to download yet. "
            "Use wait_for_job first.", code="JOB_NOT_COMPLETE")
    if not outputs:
        raise ConnectorError("That job reported success but has no output on disk. The Gateway "
                             "kept no artifact for it; this is a Gateway-side fact, not a retry "
                             "hint.", code="NO_OUTPUT")
    asset_id = str(arguments.get("asset_id") or outputs[0].get("asset_id") or "0")
    chosen = next((row for row in outputs if str(row.get("asset_id")) == asset_id), None)
    if chosen is None:
        raise ConnectorError(
            f"That job has no output {asset_id}; it has {len(outputs)} "
            f"({', '.join(str(row.get('asset_id')) for row in outputs)}).",
            code="ARTIFACT_NOT_FOUND")
    path = ("/admin/api/artifacts/" + urllib.parse.quote(job_id, safe="")
            + "/outputs/" + urllib.parse.quote(asset_id, safe="") + "?download=1")
    _status_code, content, headers = api("GET", path, accept="*/*", timeout=600.0)
    media_type = str(chosen.get("media_type") or headers.get("Content-Type")
                     or "application/octet-stream").split(";")[0]
    filename = arguments.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        extension = mimetypes.guess_extension(media_type) or ""
        filename = f"{job.get('modality') or 'asset'}-{job_id[:8]}-{asset_id}{extension}"
    target = _safe_target(str(arguments.get("folder") or ""), filename.strip())
    if target.exists():
        stem, suffix = target.stem, target.suffix
        index = 2
        while target.exists():
            target = target.with_name(f"{stem}-{index}{suffix}")
            index += 1
    target.write_bytes(content)
    return {"saved_to": str(target), "bytes": len(content), "media_type": media_type,
            "job_id": job_id, "asset_id": asset_id}


TOOLS = {
    "list_models": {
        "handler": tool_list_models,
        "description": (
            "The models this key may use, per modality, exactly as the Gateway publishes "
            "them — including each model's limits and options (prompt characters, aspect "
            "ratios, resolutions, durations, controls). Paid models appear only if the owner "
            "allowed them for this guest."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "modality": {"type": "string",
                             "description": "Optional: chat, image, video, tts, music, "
                                            "transcription."},
                "refresh": {"type": "boolean",
                            "description": "Read the catalogue again instead of the cache."},
            },
        },
    },
    "list_voices": {
        "handler": tool_list_voices,
        "description": "Voices usable for speech, from the Gateway's own voice library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "Only voices this model takes."},
                "search": {"type": "string", "description": "Match a name or description."},
                "collection": {"type": "string", "description": "A voice collection."},
            },
        },
    },
    "generate_image": {
        "handler": tool_generate_image,
        "description": "Start an image generation. Returns a job id and its honest state; "
                       "the work continues on the Gateway. Models from the live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "An image model id."},
                "prompt": {"type": "string", "description": "What to draw."},
                "options": {"type": "object",
                            "description": "Published options: aspect_ratio, resolution, "
                                           "output_format, … (see list_models)."},
                "idempotency_key": {"type": "string",
                                    "description": "Reusing it returns the same job, not a "
                                                   "second one."},
            },
            "required": ["model_id", "prompt"],
        },
    },
    "generate_speech": {
        "handler": tool_generate_speech,
        "description": "Start a text-to-speech job. Returns a job id and its state. Models "
                       "from the live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "A TTS model id."},
                "text": {"type": "string", "description": "What to say."},
                "voice_id": {"type": "string",
                             "description": "A voice id from list_voices."},
                "options": {"type": "object",
                            "description": "Published options: speed, language, format."},
                "idempotency_key": {"type": "string"},
            },
            "required": ["model_id", "text"],
        },
    },
    "generate_video": {
        "handler": tool_generate_video,
        "description": "Start an **image-to-video** job from a scene image on this Mac. "
                       "Text-to-video is retired, so image_path is required. Models from the "
                       "live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "A video model id."},
                "image_path": {"type": "string",
                               "description": "Path to the first-frame image (png/jpeg/webp)."},
                "prompt": {"type": "string", "description": "How the scene should move."},
                "options": {"type": "object",
                            "description": "Published options: duration_seconds, "
                                           "resolution, aspect_ratio, audio."},
                "idempotency_key": {"type": "string"},
            },
            "required": ["model_id", "image_path"],
        },
    },
    "transcribe": {
        "handler": tool_transcribe,
        "description": "Upload audio from this Mac and start a transcription. Models from the "
                       "live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "A transcription model id."},
                "file_path": {"type": "string", "description": "Path to the audio file."},
                "language": {"type": "string", "description": "Spoken language, if known."},
                "idempotency_key": {"type": "string"},
            },
            "required": ["model_id", "file_path"],
        },
    },
    "generate_music": {
        "handler": tool_generate_music,
        "description": "Start a music generation. Models from the live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "A music model id."},
                "prompt": {"type": "string", "description": "What to compose."},
                "options": {"type": "object",
                            "description": "Published options: duration_seconds, style, …"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["model_id", "prompt"],
        },
    },
    "chat": {
        "handler": tool_chat,
        "description": "Ask a chat model. Returns a job id and its state; poll get_job until "
                       "it is done, then read output_text. Models from the live catalogue:",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "A chat model id."},
                "messages": {"type": "array",
                             "items": {"type": "object"},
                             "description": "[{role, content}] — the usual shape."},
                "prompt": {"type": "string", "description": "Or one user message."},
                "options": {"type": "object",
                            "description": "Published options: temperature, max_tokens, …"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["model_id"],
        },
    },
    "get_job": {
        "handler": tool_get_job,
        "description": "The honest state of a job this key started: queued, running, succeeded, "
                       "failed or cancelled, with its outputs when finished.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    "wait_for_job": {
        "handler": tool_wait_for_job,
        "description": "Poll a job for up to timeout_seconds and report what is true: the "
                       "finished job, or 'still running'. It never pretends a job is done.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "timeout_seconds": {"type": "number",
                                    "description": f"Default {int(DEFAULT_WAIT_S)}, at most "
                                                   f"{int(MAX_WAIT_S)}."},
            },
            "required": ["job_id"],
        },
    },
    "download_result": {
        "handler": tool_download_result,
        "description": "Save a finished job's output into GATEWAY_OUTPUT_DIR (default "
                       "~/GatewayAssets), in the folder you name. Refuses anything outside it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "asset_id": {"type": "string",
                             "description": "Which output; the first when omitted."},
                "folder": {"type": "string",
                           "description": "A subfolder of the allowed output folder."},
                "filename": {"type": "string", "description": "A name, not a path."},
            },
            "required": ["job_id"],
        },
    },
}


# ---------------------------------------------------------------------------
# MCP over stdio: JSON-RPC 2.0, one object per line
# ---------------------------------------------------------------------------
def _send(stream, payload: dict) -> None:
    stream.write(_redact_json(payload) + "\n")
    stream.flush()


def _redact_json(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return _redact(text)


def _tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": _redact(text)}], "isError": is_error}


def _call_tool(name: str, arguments: dict) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return _tool_result(f"No such tool: {name}. Available: {', '.join(TOOLS)}.", True)
    try:
        answer = tool["handler"](arguments if isinstance(arguments, dict) else {})
        return _tool_result(json.dumps(answer, ensure_ascii=False, indent=2))
    except ConnectorError as exc:
        code = f" ({exc.code})" if exc.code else ""
        return _tool_result(f"The Gateway refused{code}: {exc}", True)
    except (KeyError, TypeError, ValueError) as exc:
        return _tool_result(f"The Gateway's answer was not the shape this connector expects: "
                            f"{exc!r}", True)


#: The model family each tool picks from. `model_id` is filled from the **live catalogue** on
#: every `tools/list`, so no model list is written down in this file (AGENTS §1).
TOOL_MODALITY = {
    "generate_image": "image",
    "generate_speech": "tts",
    "generate_video": "video",
    "transcribe": "transcription",
    "generate_music": "music",
    "chat": "chat",
}


def _tools_listing() -> dict:
    tools = []
    for name, tool in TOOLS.items():
        schema = tool["inputSchema"]
        properties = {}
        for key, value in (schema.get("properties") or {}).items():
            if key == "model_id" and name in TOOL_MODALITY:
                properties[key] = model_id_schema(TOOL_MODALITY[name],
                                                  str(value.get("description") or ""))
            else:
                properties[key] = dict(value) if isinstance(value, dict) else value
        tools.append({"name": name, "description": tool["description"],
                      "inputSchema": {**schema, "properties": properties}})
    return {"tools": tools}


def handle(message: dict) -> dict | None:
    method = str(message.get("method") or "")
    message_id = message.get("id")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": message_id, "result": {
            "protocolVersion": requested if isinstance(requested, str) and requested
            else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "GatewayKH's guest connector. You can make images, speech, music, videos "
                "(image-to-video), transcriptions and chat replies through the owner's "
                "Gateway. Every job is asynchronous: a generate_* tool returns a job id at "
                "once — use wait_for_job or get_job, then download_result to save it. You "
                "may use free and local models only unless the owner allowed paid models "
                "for this guest."),
        }}
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": _tools_listing()}
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        return {"jsonrpc": "2.0", "id": message_id,
                "result": _call_tool(name, arguments)}
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "exit":
        raise SystemExit(0)
    if message_id is None:
        return None
    return {"jsonrpc": "2.0", "id": message_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _send(stdout, {"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "Parse error"}})
            continue
        if not isinstance(message, dict):
            _send(stdout, {"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32600, "message": "Invalid Request"}})
            continue
        try:
            answer = handle(message)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken tool must not kill the session
            message_id = message.get("id")
            answer = ({"jsonrpc": "2.0", "id": message_id,
                       "error": {"code": -32603,
                                 "message": _redact(f"Internal error: {exc!r}")}}
                      if message_id is not None else None)
        if answer is not None:
            _send(stdout, answer)


def main() -> int:
    # **Nothing about the key is ever printed.** Even an import-time mistake that raises
    # would carry no secret: the key is not read until a tool is called.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
