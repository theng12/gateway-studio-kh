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
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
import time
from typing import Any, Iterator, NamedTuple

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
#: A full push is one JSON body with the image in base64, generated in chunks here
#: (the controller still buffers it); a delta push's POST can take ~20 s of hashing on
#: the controller's side, so the read timeout is generous.
_PUSH_TIMEOUT = httpx.Timeout(600.0, connect=15.0)
#: Everything after the manifest POST answers from memory or a small write; a step that
#: hangs is a dead controller, and 600 s of waiting on it is what a 550 s failed pilot
#: looked like. The idempotent steps get a short read timeout plus `_retry`.
_STEP_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
#: A transport error mid-push does not say whether the controller stored the release
#: (the reply can be lost after the store, or the Hub can restart to install it). Poll its
#: own record this long before calling the push failed.
_RECOVER_SECONDS = 60.0
_RECOVER_INTERVAL = 5.0
#: Transport errors worth repeating on an idempotent request (a stale keep-alive
#: connection is "Server disconnected without sending a response"). Not read timeouts.
_TRANSIENT = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectError)
MANIFEST_SCHEMA = "studiohub.file-manifest.v1"
#: Parallel blob PUTs to ONE controller (the contract says "a handful" is fine).
_BLOB_WORKERS = 4
_CHUNK = 3 * 1024 * 1024  # multiple of 3, so every base64 chunk but the last has no padding


class ReleaseError(ValueError):
    """A refusal the console can show as-is."""

    def __init__(self, message: str, status: int = 400, code: str = "RELEASE_ERROR") -> None:
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Ordering two versions — the downgrade guard
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")


def version_key(version: str) -> tuple[int, ...] | None:
    """Order a release version, or `None` when it is not dotted numbers.

    The Hub's own updater has no ordering at all: it offers whatever build
    differs from what it holds, so a gateway publishing `0.6.50` over a
    controller holding `0.6.51` would walk that Mac *backwards*. This is the
    gateway's guard against that. Only a clean `1.2.3`-style version gets an
    order; anything else (a date, a suffix, an empty field) has none here and is
    not blocked, because refusing on a guess is worse than not refusing.
    """
    text = (version or "").strip()
    if not _VERSION_RE.match(text):
        return None
    return tuple(int(part) for part in text.split("."))


def is_downgrade(publishing: str, held: str) -> bool:
    """True only when both versions order and the controller's is the newer one."""
    published, present = version_key(publishing), version_key(held)
    if published is None or present is None:
        return False
    return present > published


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


def descriptor_for(
    *, sha256: str, size: int, version: str, build: str, filename: str,
    files: dict[str, Any] | None = None, published_at: int | None = None,
) -> dict[str, Any]:
    """Build and sign a release descriptor for a disk image known by its digest.

    `files` is `{files_sha256, files_count, files_bytes}` from the DMG's own
    `file-manifest/index.json`; the three go **inside the signed bytes** (all or
    none), and `sha256`/`bytes` keep meaning the disk image.
    """
    if not size:
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
        "bytes": size,
        "sha256": sha256,
        "published_at": published_at or int(time.time()),
        "publisher_id": PUBLISHER_ID,
        "publisher_key_id": identity()["fingerprint"],
    }
    if files:
        descriptor.update({key: files[key] for key in ("files_sha256", "files_count", "files_bytes")})
    descriptor["signature"] = sign_descriptor(descriptor)
    return descriptor


def descriptor_for_bytes(
    artifact: bytes, *, version: str, build: str, filename: str
) -> dict[str, Any]:
    """Build and sign a release descriptor for an artifact's bytes."""
    return descriptor_for(
        sha256=hashlib.sha256(artifact).hexdigest(), size=len(artifact),
        version=version, build=build, filename=filename,
    )


def _hash_file(path: Path) -> tuple[str, int]:
    """sha256 and size of a file, streamed — the disk image never sits in memory."""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# The file manifest inside the disk image (delta push)
# ---------------------------------------------------------------------------

@contextmanager
def _mounted(dmg: Path) -> Iterator[Path]:
    """The disk image mounted read-only at a private mount point; always detached."""
    mountpoint = tempfile.mkdtemp(prefix="gateway-dmg-")
    try:
        attach = subprocess.run(
            ["hdiutil", "attach", "-readonly", "-nobrowse", "-noverify", "-noautoopen",
             "-mountpoint", mountpoint, str(dmg)],
            capture_output=True, text=True, timeout=300,
        )
        if attach.returncode != 0:
            raise ReleaseError(
                f"Could not mount the disk image: {(attach.stderr or '').strip()[:300]}",
                500, "RELEASE_MOUNT_FAILED",
            )
        yield Path(mountpoint)
    finally:
        # Detach even when attach failed half way; a mount that never happened just errors.
        subprocess.run(["hdiutil", "detach", "-force", mountpoint], capture_output=True, timeout=120)
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass


class Manifest(NamedTuple):
    index: dict[str, Any]
    groups: dict[str, str]  # group -> base64 of its .tsv.gz, exactly as the Hub wants it
    blobs: dict[str, Path]  # sha256 of file bytes -> a file that has them
    files: dict[str, Any]   # the three descriptor fields


def load_manifest(root: Path) -> Manifest:
    """Read and self-check `<app>/Contents/Resources/file-manifest/` under a mounted image."""
    app = next(iter(sorted(root.glob("*.app"))), None)
    directory = app / "Contents/Resources/file-manifest" if app else None
    if directory is None or not (directory / "index.json").is_file():
        raise ReleaseError("The disk image carries no file manifest.", 400, "RELEASE_NO_MANIFEST")
    try:
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        assert index["schema"] == MANIFEST_SCHEMA
        described = index["groups"]
        count, total = int(index["files_count"]), int(index["files_bytes"])
        assert count > 0 and total > 0 and described
        groups: dict[str, str] = {}
        blobs: dict[str, Path] = {}
        for name, info in described.items():
            raw = (directory / f"{name}.tsv.gz").read_bytes()
            text = gzip.decompress(raw)
            assert hashlib.sha256(text).hexdigest() == info["sha256"]
            groups[name] = base64.b64encode(raw).decode("ascii")
            for line in text.decode("utf-8").splitlines():
                kind, _x, _size, digest, rel = line.split("\t", 4)
                if kind == "f":
                    blobs.setdefault(digest, app / rel)
        committed = "".join(f"{n}\0{described[n]['sha256']}\n" for n in sorted(described))
        assert hashlib.sha256(committed.encode()).hexdigest() == index["files_sha256"]
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
        raise ReleaseError(
            f"The disk image's file manifest is unusable ({type(exc).__name__}).",
            400, "RELEASE_MANIFEST_INVALID",
        ) from exc
    files = {"files_sha256": index["files_sha256"], "files_count": count, "files_bytes": total}
    return Manifest(index, groups, blobs, files)


# ---------------------------------------------------------------------------
# Push and read back
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict[str, str]:
    return {"X-Hub-Token": token} if token else {}


def _probe(base: str, token: str) -> tuple[dict[str, Any] | None, bool]:
    """What a controller holds, and whether it can take a delta push.

    The release is the controller's own answer: `{"release": null}` is published as
    `None`, never as a stub that could read as a release. A `delta` key with the
    manifest schema is the capability marker (0.6.115+); no key means the full push.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=6.0)) as client:
            response = client.get(f"{base.rstrip('/')}/api/hub/release", headers=_headers(token))
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None, False
    if not isinstance(body, dict):
        return None, False
    release = body.get("release")
    delta = body.get("delta")
    return (
        release if isinstance(release, dict) else None,
        isinstance(delta, dict) and delta.get("schema") == MANIFEST_SCHEMA,
    )


def read_release(base: str, token: str) -> dict[str, Any] | None:
    """What a controller actually holds, from its own answer."""
    return _probe(base, token)[0]


def _failed(exc: httpx.HTTPError, base: str) -> dict[str, Any]:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        # A connection that was never established delivered nothing, so this is a
        # site that is down rather than an unknown push. A read timeout after the
        # request left is different and keeps the generic path below.
        return {
            "ok": False, "status": None, "code": "LOCATION_UNREACHABLE",
            "detail": f"could not reach the controller at {base} ({type(exc).__name__})",
            "release": None,
        }
    return {
        "ok": False, "status": None, "code": "RELEASE_PUSH_FAILED",
        "detail": f"the push failed: {exc}", "release": None,
    }


def _retry(call: Any, attempts: int = 3) -> Any:
    """Repeat an idempotent request on a transient transport error."""
    for n in range(attempts):
        try:
            return call()
        except _TRANSIENT:
            if n == attempts - 1:
                raise
            time.sleep(0.5 * (n + 1))


def _recover(
    base: str, token: str, descriptor: dict[str, Any], failed: dict[str, Any], *, delta: bool
) -> dict[str, Any]:
    """A transport error is not a verdict: ask the controller what it holds.

    Polls its own record for `_RECOVER_SECONDS`. If it carries this descriptor's
    signature (so version and build too) the push succeeded and only the reply was lost;
    for a delta, `GET /need` must then also be empty. Otherwise the original failure is
    reported, with what the controller actually holds.
    """
    deadline = time.monotonic() + _RECOVER_SECONDS
    while True:
        release, _ = _probe(base, token)
        if release is not None and release.get("signature") == descriptor["signature"]:
            outcome = {**failed, "ok": True, "code": None, "detail": None, "release": release,
                       "note": f"the reply was lost ({failed['detail']}), but the controller's own record shows this release"}
            if delta:
                try:
                    with httpx.Client(timeout=_STEP_TIMEOUT) as client:
                        answer = _retry(lambda: client.get(f"{base.rstrip('/')}/api/hub/release/need", headers=_headers(token)))
                    need = answer.json().get("need") if answer.status_code == 200 else None
                except (httpx.HTTPError, ValueError, AttributeError):
                    need = None
                if isinstance(need, list) and need:
                    outcome.update(ok=False, code="RELEASE_INCOMPLETE", release=None, detail=(
                        f"the controller stored the release but still lacks {len(need)} blob(s); publish again to send them"))
                elif not isinstance(need, list):
                    outcome["note"] += "; completeness (GET /need) could not be read"
            return outcome
        if time.monotonic() >= deadline:
            held = str((release or {}).get("version") or "").strip()
            failed = {**failed, "detail": f"{failed['detail']} (the controller was re-checked for {int(_RECOVER_SECONDS)} s and "
                      + (f"holds {held}, not this release)" if held else "does not hold this release)")}
            return failed
        time.sleep(_RECOVER_INTERVAL)


def _refusal(response: httpx.Response) -> dict[str, Any]:
    """The controller's own refusal sentence, verbatim.

    A `409` means no trusted gateway key is installed on that controller yet, which
    is the one action the owner has to take there — so the fingerprint to install
    is appended in front of the controller's own sentence.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    reason = payload.get("detail") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        reason = f"the controller refused the release (HTTP {response.status_code})"
    if response.status_code == 409:
        # The controller cannot know the fingerprint — it is the gateway's.
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


def _full_body(descriptor: dict[str, Any], artifact: bytes | Path) -> tuple[Iterator[bytes], int]:
    """The legacy JSON push body, generated in chunks instead of held in memory.

    Byte for byte `{"descriptor": …, "artifact_base64": "…"}`; the length is computed up
    front so it goes out with a Content-Length (no chunked encoding for the controller
    to trip over). The controller still buffers it on its side.
    """
    head = b'{"descriptor":' + json.dumps(descriptor).encode() + b',"artifact_base64":"'
    tail = b'"}'
    size = len(artifact) if isinstance(artifact, bytes) else artifact.stat().st_size
    length = len(head) + 4 * ((size + 2) // 3) + len(tail)

    def chunks() -> Iterator[bytes]:
        yield head
        if isinstance(artifact, bytes):
            for start in range(0, size, _CHUNK):
                yield base64.b64encode(artifact[start:start + _CHUNK])
        else:
            with open(artifact, "rb") as handle:
                while block := handle.read(_CHUNK):
                    yield base64.b64encode(block)
        yield tail

    return chunks(), length


def push(
    base: str, token: str, descriptor: dict[str, Any], artifact: bytes | Path
) -> dict[str, Any]:
    """Offer a whole release (descriptor + disk image) to one controller.

    Streams the body, so the DMG is never in memory here; used for controllers without
    the delta capability. Its refusal is the controller's own words.
    """
    body, length = _full_body(descriptor, artifact)
    headers = {**_headers(token), "Content-Type": "application/json", "Content-Length": str(length)}
    try:
        with httpx.Client(timeout=_PUSH_TIMEOUT) as client:
            response = client.post(f"{base.rstrip('/')}/api/hub/release", headers=headers, content=body)
    except httpx.HTTPError as exc:
        failed = _failed(exc, base)
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return failed  # nothing was delivered
        return _recover(base, token, descriptor, failed, delta=False)
    if response.status_code != 200:
        return _refusal(response)
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


class _Refused(Exception):
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome


def push_delta(
    base: str, token: str, descriptor: dict[str, Any], manifest: Manifest
) -> dict[str, Any] | None:
    """Manifest first, then only the blobs the controller says it lacks, then read back.

    Returns `None` when the controller has no such routes (404/405): the caller falls
    back to the full push. Every other answer is final. The outcome's `release` is the
    controller's own record read back after the last blob, and `ok` needs it to carry
    this descriptor's signature and `need` to be empty.
    """
    root = f"{base.rstrip('/')}/api/hub/release"
    headers = _headers(token)
    stop = threading.Event()
    lock = threading.Lock()
    stats: dict[str, Any] = {"mode": "delta", "blobs_sent": 0, "bytes_sent": 0}
    timings: dict[str, float] = {}
    step = "manifest"

    def finish(outcome: dict[str, Any]) -> dict[str, Any]:
        """Every path out reports how much was sent, and how long each step took."""
        return {**outcome, **stats, "timings": dict(timings)}

    def put(client: httpx.Client, digest: str) -> int:
        path = manifest.blobs.get(digest)
        if path is None:
            raise _Refused({
                "ok": False, "status": 200, "code": "RELEASE_NEED_UNKNOWN",
                "detail": f"the controller asked for blob {digest[:12]}, which is not in the manifest",
                "release": None,
            })
        if stop.is_set():
            return 0
        size = path.stat().st_size

        def send() -> httpx.Response:
            with open(path, "rb") as handle:  # a retry re-reads from the start
                return client.put(
                    f"{root}/blob/{digest}", headers={**headers, "Content-Length": str(size)},
                    content=iter(lambda: handle.read(1 << 20), b""),
                )

        response = _retry(send)
        if response.status_code != 200:
            stop.set()
            raise _Refused(_refusal(response))
        with lock:
            stats["blobs_sent"] += 1
            stats["bytes_sent"] += size
        return size

    started = time.monotonic()
    try:
        with httpx.Client(timeout=_PUSH_TIMEOUT) as client:
            posted = client.post(root, headers=headers, json={
                "descriptor": descriptor,
                "file_manifest": {"index": manifest.index, "groups": manifest.groups},
            })
            timings["manifest_post"] = round(time.monotonic() - started, 2)
            if posted.status_code in (404, 405):
                return None
            if posted.status_code != 200:
                return finish(_refusal(posted))
            try:
                need = posted.json().get("need")
            except (ValueError, AttributeError):
                need = None
            if not isinstance(need, list):
                return finish({
                    "ok": False, "status": 200, "code": "RELEASE_NOT_STORED",
                    "detail": "the controller answered 200 but did not say which blobs it needs",
                    "release": None,
                })
            step, mark = "blobs", time.monotonic()
            wanted = list(dict.fromkeys(str(digest) for digest in need))
            stats["blobs_needed"] = len(wanted)
            with httpx.Client(timeout=_STEP_TIMEOUT) as steps, ThreadPoolExecutor(max_workers=_BLOB_WORKERS) as pool:
                list(pool.map(lambda digest: put(steps, digest), wanted))
                timings["blob_puts"] = round(time.monotonic() - mark, 2)
                step, mark = "need", time.monotonic()
                remaining = _retry(lambda: steps.get(f"{root}/need", headers=headers))
                timings["need_check"] = round(time.monotonic() - mark, 2)
                step, mark = "readback", time.monotonic()
                held = _retry(lambda: steps.get(root, headers=headers))
                timings["readback"] = round(time.monotonic() - mark, 2)
    except _Refused as refused:
        return finish(refused.outcome)
    except httpx.HTTPError as exc:
        failed = _failed(exc, base)
        timings[f"{step}_failed_after"] = round(time.monotonic() - started, 2)
        if step == "manifest" and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return finish(failed)  # nothing was delivered: unreachable, not an unknown push
        mark = time.monotonic()
        recovered = _recover(base, token, descriptor, failed, delta=True)
        timings["recover"] = round(time.monotonic() - mark, 2)
        return finish(recovered)
    try:
        still = remaining.json().get("need") if remaining.status_code == 200 else None
        release = held.json().get("release") if held.status_code == 200 else None
    except (ValueError, AttributeError):
        still, release = None, None
    outcome: dict[str, Any] = {"ok": False, "status": 200, "code": None, "detail": None, "release": None}
    if still != []:
        outcome.update(
            code="RELEASE_INCOMPLETE",
            detail=f"the controller still lacks {len(still) if isinstance(still, list) else 'an unknown number of'} blob(s) after the push",
        )
    elif not isinstance(release, dict) or release.get("signature") != descriptor["signature"]:
        outcome.update(
            code="RELEASE_NOT_STORED",
            detail="the controller's own record does not show this release after the push",
        )
    else:
        outcome.update(ok=True, release=release)
    return finish(outcome)


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
    *,
    artifact_path: str | Path,
    version: str,
    build: str,
    filename: str | None = None,
    allow_downgrade: bool = False,
    controller_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Sign one artifact and offer it to every configured controller (or only `controller_ids`).

    Returns the descriptor, its fingerprint, and a per-controller outcome in which
    `release` is what the controller **read back** after storing it.

    Controllers are probed first. A controller that reports the `delta` capability gets
    the DMG's file manifest and then only the blobs it lacks (the DMG is mounted
    read-only once, streamed from, and always detached); any other controller — or one
    without the routes — gets the whole image, streamed. Controllers are pushed
    concurrently and each keeps its own outcome: one failing does not stop the rest.

    A controller holding a **newer** version than this artifact is refused
    (`RELEASE_DOWNGRADE`) unless `allow_downgrade` is set: the Hub's updater has no
    ordering and would take the older build, so the gateway is the only place that can
    stop it.
    """
    path = Path(artifact_path).expanduser()
    if not path.is_file():
        raise ReleaseError(f"No artifact at {path}.", 400, "RELEASE_ARTIFACT_MISSING")
    clock = time.monotonic()
    timings: dict[str, float] = {}

    def lap(name: str) -> None:
        nonlocal clock
        timings[name] = round(time.monotonic() - clock, 2)
        clock = time.monotonic()

    sha256, size = _hash_file(path)
    lap("hash_image")
    fields = {"sha256": sha256, "size": size, "version": version, "build": build,
              "filename": filename or path.name, "published_at": int(time.time())}
    descriptor = descriptor_for(**fields)
    fingerprint = identity()["fingerprint"]
    lap("sign")
    target = descriptor["version"]

    rows = [
        (str(row.get("id") or ""), row.get("label"), str(row.get("url") or ""))
        for row in fleet._read()  # noqa: SLF001 - the roster, and only controllers take a release
    ]
    if controller_ids is not None:
        # A staged rollout (a pilot controller first): only the named controllers are offered it.
        unknown = sorted(set(controller_ids) - {cid for cid, _, _ in rows})
        if unknown or not controller_ids:
            raise ReleaseError(f"Unknown controller id(s): {', '.join(unknown) or '(none given)'}.",
                               400, "RELEASE_UNKNOWN_CONTROLLER")
        rows = [row for row in rows if row[0] in set(controller_ids)]
    tokens = {cid: fleet._token_for(cid) for cid, _, _ in rows}  # noqa: SLF001
    results: dict[str, Any] = {}
    with ExitStack() as stack:
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=max(1, len(rows))))
        probes = list(pool.map(lambda row: _probe(row[2], tokens[row[0]]), rows))
        lap("probe")

        manifest: Manifest | None = None
        skipped: str | None = None
        if any(capable for _, capable in probes):
            try:
                manifest = load_manifest(stack.enter_context(_mounted(path)))
            except ReleaseError as exc:
                skipped = f"delta push skipped ({exc.message}); the whole image was sent"
        delta_descriptor = descriptor_for(**fields, files=manifest.files) if manifest else None
        lap("mount_manifest_sign")

        def one(row: tuple[str, Any, str], probe: tuple[dict[str, Any] | None, bool]) -> dict[str, Any]:
            cid, label, url = row
            held, capable = probe
            planned = "delta" if capable and manifest and delta_descriptor else "full"
            held_version = str((held or {}).get("version") or "").strip()
            try:
                if held is not None and not allow_downgrade and is_downgrade(target, held_version):
                    outcome = {
                        "ok": False, "status": None, "code": "RELEASE_DOWNGRADE",
                        "detail": (
                            f"this controller holds {held_version}, which is newer than {target}. "
                            "Publishing would take it backwards; publish a newer build, or choose "
                            "to publish anyway."
                        ),
                        "release": None,
                    }
                else:
                    outcome = (
                        push_delta(url, tokens[cid], delta_descriptor, manifest)
                        if planned == "delta" else None
                    )
                    if outcome is None:
                        outcome = push(url, tokens[cid], descriptor, path)
                        outcome["mode"] = "full"
                        outcome.setdefault("bytes_sent", size if outcome["ok"] else 0)
                        if capable and skipped:
                            outcome["note"] = skipped
            except Exception as exc:  # noqa: BLE001 - one controller must not stop the others
                outcome = {
                    "ok": False, "status": None, "code": "RELEASE_PUSH_FAILED",
                    "detail": f"the push failed: {type(exc).__name__}: {exc}", "release": None,
                }
            # Every path says what it tried and how much left this Mac, failures included.
            outcome.setdefault("mode", planned)
            outcome.setdefault("blobs_sent", 0)
            outcome.setdefault("bytes_sent", 0)
            outcome["label"] = label or cid
            steps = ", ".join(f"{k} {v}s" for k, v in outcome.get("timings", {}).items())
            print(f"[release] {outcome['label']}: {outcome['mode']} {'ok' if outcome['ok'] else outcome['code']}, "
                  f"{outcome['blobs_sent']} blob(s) {outcome['bytes_sent'] // 1024} KiB ({steps})", flush=True)
            return outcome

        for row, outcome in zip(rows, pool.map(one, rows, probes)):
            results[row[0]] = outcome
        lap("push")

    shown = delta_descriptor or descriptor
    state = {
        "updated_at": int(time.time()),
        "fingerprint": fingerprint,
        "release": {
            key: shown[key]
            for key in ("version", "build", "filename", "bytes", "sha256", "signature",
                        "files_sha256", "files_count", "files_bytes")
            if key in shown
        },
        "locations": results,
        "timings": timings,
    }
    _write_state(state)
    return state
