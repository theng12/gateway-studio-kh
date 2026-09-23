"""Publish a signed Studio Hub app release to every configured controller.

GatewayKH is the source of truth for the release: it signs a descriptor with its
own Ed25519 **publisher** key, pushes the descriptor plus the artifact to each
controller with that controller's Fleet token, and then **reads back** what the
controller actually holds. "The request was accepted" is not the answer to "what
does it hold", so the report comes from `GET /api/hub/release`, never from the
push's status code.

The wire contract is
`studio-hub-kh/.planning/2026-09-10-standalone-app-feasibility/remote-update-design.md`
("Wire contract (chosen, to build against)"), matched byte for byte:

* **Canonical bytes** are the descriptor JSON with sorted keys and no whitespace,
  UTF-8. All fields are ASCII, so Python's `sort_keys`/`separators` and Node's
  `JSON.stringify` over sorted keys agree.
* The gateway signs the canonical bytes with the `"signature"` field **removed**,
  as standard base64 (with padding) of the 64 raw Ed25519 bytes.
* `publisher_key_id` is the lowercase hex sha256 of the raw 32-byte public key —
  the fingerprint the owner installs on a controller with
  `POST /api/hub/release/trust`.

The key pair lives in the gateway's data directory and is never returned by an
API. Signing runs in the same Node helper the Content Pack signer uses
(`content_signing.mjs`), which already canonicalises exactly this way — one
implementation of "canonical", not two that can drift.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import httpx

from . import config, fleet, node_tool


SCHEMA = "studiohub.app-release.v1"
SCHEMA_VERSION = 1
PUBLISHER_ID = "gatewaykh"

_SIGNER = Path(__file__).with_name("content_signing.mjs")
#: A descriptor is a few hundred bytes; the signer's output limit is for that, not
#: for the artifact — the artifact never goes anywhere near the signer.
_OUTPUT_LIMIT = 64 * 1024
_SIGN_TIMEOUT = 30.0
#: A pushed artifact is base64 in one JSON body, so the upload is larger than the
#: file. `ponytail:` this holds the artifact and its base64 in memory on both
#: sides (~1.5x the file); the design chose "keep it simple" and this is the point
#: where a memory-tight side would want multipart.
_PUSH_TIMEOUT = httpx.Timeout(600.0, connect=15.0)


class ReleaseError(ValueError):
    """A refusal the console can show as-is."""

    def __init__(self, message: str, status: int = 400, code: str = "RELEASE_ERROR") -> None:
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# The publisher key
# ---------------------------------------------------------------------------

def key_path() -> Path:
    return Path(config.DATA_DIR) / "release-publisher.pem"


def _state_path() -> Path:
    return Path(config.DATA_DIR) / "release-state.json"


def _node_request(request: dict[str, Any]) -> dict[str, Any]:
    """One request to the Node signing helper. Never raises its errors as ours."""
    encoded = json.dumps(
        request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    try:
        completed = node_tool.run_process(
            [node_tool.node_executable(), str(_SIGNER)],
            encoded,
            node_tool.tool_environment(),
            timeout=_SIGN_TIMEOUT,
            output_limit=_OUTPUT_LIMIT,
        )
    except (node_tool.NodeToolError, OSError) as exc:
        raise ReleaseError(
            "The release signer is unavailable (Node could not run).",
            503, "RELEASE_SIGNER_UNAVAILABLE",
        ) from exc
    try:
        result = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise ReleaseError(
            "The release signer returned invalid output.", 503, "RELEASE_SIGNER_UNAVAILABLE"
        ) from exc
    if not isinstance(result, dict) or completed.returncode != 0 or "error" in result:
        detail = result.get("error") if isinstance(result, dict) else None
        if not isinstance(detail, str) or not detail or len(detail) > 400:
            detail = "The release signer is unavailable."
        raise ReleaseError(detail, 503, "RELEASE_SIGNER_UNAVAILABLE")
    return result


def identity() -> dict[str, Any]:
    """This gateway's publisher public key and fingerprint, generating on first use.

    Only the public half is returned; the private key stays in the data directory
    with the helper's 0600 and never enters a response body. The answer is cached
    for the process: the key is created once and never replaced, and the Fleet page
    asks for this on every poll — which would otherwise be a Node process per poll.
    """
    global _identity_cache
    if _identity_cache is not None:
        return _identity_cache
    path = key_path()
    if not path.exists():
        _node_request({"action": "generate", "private_key_path": str(path)})
    inspected = _node_request({"action": "inspect", "private_key_path": str(path)})
    raw = base64.b64decode(str(inspected.get("public_key_base64") or ""), validate=True)
    fingerprint = str(inspected.get("public_key_fingerprint") or "").lower()
    if len(raw) != 32 or len(fingerprint) != 64:
        raise ReleaseError(
            "The publisher key is not a usable Ed25519 identity.", 503, "RELEASE_KEY_INVALID"
        )
    _identity_cache = {
        "algorithm": "ed25519",
        "public_key_base64": base64.b64encode(raw).decode("ascii"),
        "public_key_hex": raw.hex(),
        "fingerprint": fingerprint,
        "publisher_id": PUBLISHER_ID,
    }
    return _identity_cache


def forget_identity() -> None:
    """Drop the cached identity. For a test that points at a fresh data directory."""
    global _identity_cache
    _identity_cache = None


_identity_cache: dict[str, Any] | None = None


def sign_descriptor(descriptor: dict[str, Any]) -> str:
    """The base64 signature over the canonical bytes **without** `signature`."""
    payload = {key: value for key, value in descriptor.items() if key != "signature"}
    signed = _node_request({
        "action": "sign",
        "payload": payload,
        "private_key_path": str(key_path()),
    })
    signature = signed.get("signature")
    if not isinstance(signature, str) or not signature:
        raise ReleaseError("The release signer produced no signature.", 503, "RELEASE_KEY_INVALID")
    return signature


def canonical_bytes(descriptor: dict[str, Any]) -> bytes:
    """The bytes that get signed: sorted keys, no whitespace, UTF-8.

    Exposed so a test can assert the exact encoding rather than trusting that two
    languages agree about it.
    """
    return json.dumps(
        descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def descriptor_for_bytes(
    artifact: bytes, *, version: str, build: str, filename: str
) -> dict[str, Any]:
    """Build and sign a release descriptor for an artifact's bytes."""
    if not artifact:
        raise ReleaseError("The artifact is empty.", 400, "RELEASE_ARTIFACT_EMPTY")
    if not version.strip() or not build.strip() or not filename.strip():
        raise ReleaseError(
            "A release needs a version, a build and a filename.", 400, "RELEASE_FIELDS_REQUIRED"
        )
    descriptor: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "version": version.strip(),
        "build": build.strip(),
        "filename": filename.strip(),
        "bytes": len(artifact),
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "published_at": int(time.time()),
        "publisher_id": PUBLISHER_ID,
        "publisher_key_id": identity()["fingerprint"],
    }
    descriptor["signature"] = sign_descriptor(descriptor)
    return descriptor


# ---------------------------------------------------------------------------
# Push and read back
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {"X-Hub-Token": token} if token else {}


def read_release(base: str, token: str) -> dict[str, Any] | None:
    """What a controller actually holds, from its own answer.

    `{"release": null}` is the controller saying it holds nothing — published as
    `None`, never as a stub that could read as a release.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=6.0)) as client:
            response = client.get(f"{base.rstrip('/')}/api/hub/release", headers=_headers(token))
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    release = body.get("release") if isinstance(body, dict) else None
    return release if isinstance(release, dict) else None


def push(base: str, token: str, descriptor: dict[str, Any], artifact: bytes) -> dict[str, Any]:
    """Offer a release to one controller. Its refusal is the controller's own words.

    A `409` means no trusted gateway key is installed on that controller yet, which
    is the one action the owner has to take there — so the fingerprint to install
    is appended, and the controller's own sentence is kept verbatim in front of it.
    """
    body = {
        "descriptor": descriptor,
        "artifact_base64": base64.b64encode(artifact).decode("ascii"),
    }
    endpoint = f"{base.rstrip('/')}/api/hub/release"
    try:
        with httpx.Client(timeout=_PUSH_TIMEOUT) as client:
            response = client.post(endpoint, headers=_headers(token), json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # A connection that was never established delivered nothing, so this is a
        # site that is down rather than an unknown push. A read timeout after the
        # request left is different and keeps the generic path below.
        return {
            "ok": False, "status": None, "code": "LOCATION_UNREACHABLE",
            "detail": f"could not reach the controller at {base} ({type(exc).__name__})",
            "release": None,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False, "status": None, "code": "RELEASE_PUSH_FAILED",
            "detail": f"the push failed: {exc}", "release": None,
        }
    detail: str | None = None
    release: dict[str, Any] | None = None
    if response.status_code == 200:
        try:
            answered = response.json()
        except ValueError:
            answered = {}
        release: dict[str, Any] | None = None
        if isinstance(answered, dict):
            candidate = answered.get("release")
            if isinstance(candidate, dict):
                release = candidate
            elif answered.get("schema") == SCHEMA:
                # The contract's 200 is **the stored descriptor itself**; the
                # `{"release": …}` envelope is the shape the GET uses. Accept
                # either, so a controller that follows the literal wording is not
                # read as "it stored nothing".
                release = answered
        if release is None:
            # A 200 without the stored descriptor is not a success we can report:
            # the only proof is the controller's own record.
            return {
                "ok": False, "status": 200, "code": "RELEASE_NOT_STORED",
                "detail": "the controller answered 200 but returned no stored release",
                "release": None,
            }
        return {"ok": True, "status": 200, "code": None, "detail": None, "release": release}

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    reason = payload.get("detail") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        reason = f"the controller refused the release (HTTP {response.status_code})"
    if response.status_code == 409:
        # No trusted key: name the fingerprint the owner must install. The
        # controller cannot know it — it is the gateway's, and this is the side
        # that holds it.
        try:
            fingerprint = identity()["fingerprint"]
            reason = f"{reason.strip()} Install this gateway's publisher fingerprint on that Mac: {fingerprint}"
        except ReleaseError:
            pass
    return {
        "ok": False,
        "status": response.status_code,
        "code": code if isinstance(code, str) and code else "RELEASE_REFUSED",
        "detail": reason,
        "release": None,
    }


# ---------------------------------------------------------------------------
# The last push, remembered for the page
# ---------------------------------------------------------------------------

def _read_state() -> dict[str, Any]:
    try:
        parsed = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def last_push() -> dict[str, Any]:
    """The last publish this gateway made, for the Fleet page."""
    return _read_state()


def publish(
    *, artifact_path: str | Path, version: str, build: str, filename: str | None = None
) -> dict[str, Any]:
    """Sign one artifact and offer it to every configured controller.

    Returns the descriptor, its fingerprint, and a per-controller outcome in which
    `release` is what the controller **read back** after storing it.
    """
    path = Path(artifact_path).expanduser()
    if not path.is_file():
        raise ReleaseError(f"No artifact at {path}.", 400, "RELEASE_ARTIFACT_MISSING")
    artifact = path.read_bytes()
    descriptor = descriptor_for_bytes(
        artifact, version=version, build=build, filename=filename or path.name
    )
    fingerprint = identity()["fingerprint"]

    controllers = fleet._read()  # noqa: SLF001 - the roster, and only controllers take a release
    results: dict[str, Any] = {}
    for row in controllers:
        controller_id = str(row.get("id") or "")
        outcome = push(str(row.get("url") or ""), fleet._token_for(controller_id),  # noqa: SLF001
                       descriptor, artifact)
        outcome["label"] = row.get("label") or controller_id
        results[controller_id] = outcome

    state = {
        "updated_at": int(time.time()),
        "fingerprint": fingerprint,
        "release": {
            "version": descriptor["version"],
            "build": descriptor["build"],
            "filename": descriptor["filename"],
            "bytes": descriptor["bytes"],
            "sha256": descriptor["sha256"],
            "signature": descriptor["signature"],
        },
        "locations": results,
    }
    _write_state(state)
    return state
