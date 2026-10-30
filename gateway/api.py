"""gateway-kh HTTP surface — the contract Story Studio KH already speaks.

Story Studio needs **zero code changes**: point its `genstudio_url` at this
service and its model pickers fill up. So this module mirrors GenStudio KH's
client contract, including the parts that look like vestigial commerce.

Four contract rules are easy to violate and expensive to debug:

  * **Never return 402.** Story Studio holds a credit lock keyed on the API key.
    If it ever sees 402 and a balance is never published, that hold never
    clears -- the key is dead permanently. A provider running out of funds is a
    settled `failed` job here, never insufficient credits.
  * **Artifact URLs must be same-origin with the configured base**, or the
    client rejects them. We return relative paths for this reason.
  * **Every settled job carries a balanced `0/0/0` settlement triple.** This is
    not display data: Story Studio advances its LLM lineup to the next candidate
    only when it can prove the failed attempt cost nothing.
  * **`freshness` must be true.** `stale: true` disables every model for new
    work, so it is computed from the last catalogue rebuild, not from process
    start.

Generation runs on a worker thread (see `worker.py`); this module only accepts
work and reports on it, so a long local job never blocks a Story Studio poll.
"""

from __future__ import annotations


import asyncio
import hmac
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .build import build_id
from .adapters import BackendError, BackendRequest, adapter_for
from .adapters.studio_hub import SUPPORTED_STUDIO_HUB_VERSION
from .catalog import (
    MODALITIES,
    TTS_PAUSE_PARAMETERS,
    catalog,
    encode_model_id,
    published_output_cap,
)
from .config import ARTIFACT_DIR, gateway_api_key, is_loopback, request_is_loopback
from .constants import CREDIT_BALANCE, ZERO_SETTLEMENT, admission
from .pricing import definition_chargeable, job_cost
from .providers import registry
from . import fleet
from . import retention
from .notifier import notifier
from .activity import is_story_path, record
from .runtime import render_service, store, worker
from .render_service import (
    _UUID, AuthenticatedPrivateRender, PrivateRenderUnavailable, RenderServiceError,
)
from .render_hash_v1 import decode_render_json
from .store import (BACKGROUND, HOLD_CLOSED, NORMAL, PRIORITIES, TERMINAL_STATES, artifacts_of,
                   requested_by_of)
from .worker import cancel_upstream
from .voices import story_row, tts_output_container, voice_registry, voices_for_model


# Uploaded audio for transcription quotes, held between the quote and its
# confirm. In memory on purpose: it is a single-process gateway and the payload
# is a transient request body, not state worth persisting.
_UPLOADS: dict[str, tuple[Any, ...]] = {}
#: Idempotency-Key -> the quote it created, so a retried quote POST is answered, not repeated.
_QUOTE_BY_KEY: dict[str, str] = {}
_QUOTE_KEY_AT: dict[str, float] = {}
#: A staged audio upload, held between the quote and the confirm.
#:
#: `content` is `None` for a quote that carried a model but no audio — the
#: price-only call. It is stored rather than left out so that confirm can tell
#: "you never sent audio with this quote" (400, and the caller can fix it) from
#: "this quote has expired" (410, and Story Studio must re-quote). Both used to
#: be 410, which sent the caller to re-quote a request that would fail again.
#: The model is stored with it because the quote's whole purpose is to name the
#: model that will run; confirm used to ignore it and run the first enabled
#: transcription row instead — a different model, possibly a different provider,
#: than the one quoted and confirmed.
_UPLOAD_TTL_S = 3600.0

#: Refuse an audio upload larger than this, before it is held or sent anywhere.
#:
#: Every cloud transcription API on the gateway caps a single upload — OpenAI's
#: whisper endpoint at 25 MB — and a caller that crosses it should be told what
#: the limit is, not handed a provider error after the gateway has buffered the
#: bytes, staged them on disk and queued a job that cannot succeed. 25 MB is also
#: the shared floor rather than OpenAI's quirk: Groq accepts the same ceiling, so
#: one number is honest for both lanes.
#:
#: ponytail: one flat ceiling for every transcription provider, because none of
#: them *publishes* its limit over its API. Ceiling — a provider with a larger cap
#: is under-used. Upgrade path — read it per model once a provider states it, the
#: way TTS limits are read from `maximum_text_length_per_request`.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: What a **Studio Hub** transcription takes: Story Studio's own ceiling, 100 MiB (the Hub itself
#: accepts 2 GiB). The 25 MB above is a cloud API's cap and was applied to every model, so a
#: narration past about 35 minutes at Story's 96 kbps MP3 could never be subtitled by the local
#: Whisper that handles it easily — a 413 Story does not retry (audit 2026-10-01).
_MAX_UPLOAD_BYTES_HUB = 100 * 1024 * 1024

#: The largest image `/v1/vision/analyze` accepts, **and the number it publishes**. Moved up here
#: (2026-10-29) so the body ceilings below can cite it instead of repeating it.
VISION_MAX_IMAGE_BYTES = 12 * 1024 * 1024


#: The largest multipart body either upload door will even *receive*, before the model — which
#: lives inside the body — is known: the Hub's 100 MiB ceiling plus room for the envelope. The
#: exact per-model limit is still applied afterwards, and is the number the caller is told when
#: the body fits under this.
_MAX_UPLOAD_BODY = _MAX_UPLOAD_BYTES_HUB + (2 * 1024 * 1024)

from .content_packs import MAX_ARCHIVE as _PACK_MAX_ARCHIVE  # noqa: E402 - a constant, no cycle

#: A Content Pack archive is capped at `content_packs.MAX_ARCHIVE` by the route that reads it;
#: the door has to allow the archive *and* whatever the request carries beside it.
_MAX_PACK_BODY = _PACK_MAX_ARCHIVE + (2 * 1024 * 1024)

#: What a multipart envelope costs around one file: boundaries, part headers, the other fields.
#: **A declared `Content-Length` is the whole body, never the file**, so a route that compares
#: it with a file limit refuses a file of exactly the published size (review 2026-10-29e,
#: finding 4). 64 KiB is far more than any envelope here and far less than a real file.
UPLOAD_ENVELOPE_BYTES = 64 * 1024

#: **A chat turn with pictures, and the number is this gateway's own.** No chat provider here
#: publishes a per-image limit or an images-per-message count, so this is not a model's figure:
#: it is the largest image this gateway tells a caller it may send (the 12 MiB vision limit it
#: publishes) for a turn carrying two of them, in base64 (×4/3), plus room for the envelope.
#: Stated as ours rather than derived, because deriving it would mean inventing a catalogue
#: field nothing publishes (§1's "never invent a number the customer will mistake for the
#: model's own" — this one is labelled as the gateway's by being constant and named here).
_CHAT_IMAGES_PER_TURN = 2
_CHAT_BODY_CEILING = VISION_MAX_IMAGE_BYTES * _CHAT_IMAGES_PER_TURN * 4 // 3 + (2 * 1024 * 1024)

#: A vision image arrives base64 inside JSON: 12 MiB decoded is 16 MiB encoded, plus the envelope.
_VISION_BODY_CEILING = VISION_MAX_IMAGE_BYTES * 4 // 3 + (2 * 1024 * 1024)


class _UploadTooLarge(Exception):
    """The body crossed its ceiling while being read. Answered as 413, never a 500."""


#: **Every named door says what it takes.** A route that accepts a file, or reads
#: `request.stream()`/`request.body()` itself, must be here; everything else gets the default.
#:
#: The list is checked, not trusted: `tests/test_body_ceiling.py` walks the app's own route table
#: and fails when a file route or a raw-stream reader is not named (review 2026-10-29c, finding 1
#: — a 4.26 MiB `.sspack` was refused by a 2 MiB default nobody had thought about).
#: `(pattern, ceiling, what, kind)`. **`kind` decides whether the body is budgeted**: an
#: `upload` holds bytes for as long as it runs, so it reserves. A `body` is parsed into memory
#: and dropped (a chat turn with a picture, a vision image) — bounded, but not competing for
#: the upload pool, or a batch of image chats would meet the same cap as 8 parallel uploads.
_UPLOAD_CEILINGS: tuple[tuple["re.Pattern[str]", int, str, str], ...] = (
    (re.compile(r"^/v1/images/source-images$"), _MAX_UPLOAD_BYTES, "a source image", "upload"),
    (re.compile(r"^/v1/videos/source-images$"), _MAX_UPLOAD_BYTES, "a source image", "upload"),
    (re.compile(r"^/admin/api/create/transcription$"), _MAX_UPLOAD_BODY, "a transcription upload",
     "upload"),
    (re.compile(r"^/admin/api/create/video$"), _MAX_UPLOAD_BYTES, "a source image", "upload"),
    (re.compile(r"^/v1/audio/transcriptions/quotes$"), _MAX_UPLOAD_BODY, "a transcription upload",
     "upload"),
    (re.compile(r"^/admin/api/fleet/controllers/[^/]+/machines/[^/]+/transcribe$"),
     _MAX_UPLOAD_BODY, "a transcription upload", "upload"),
    # A Content Pack is an archive up to 50 MiB, streamed straight to disk by its route.
    (re.compile(r"^/admin/api/content-library/import$"), _MAX_PACK_BODY, "a Content Pack", "upload"),
    (re.compile(r"^/admin/api/content-library/import-and-publish$"), _MAX_PACK_BODY,
     "a Content Pack", "upload"),
    # Pictures in a chat turn arrive as base64 data URIs inside the JSON.
    (re.compile(r"^/v1/chat/completions$"), _CHAT_BODY_CEILING, "a chat turn", "body"),
    (re.compile(r"^/v1/chat/responses$"), _CHAT_BODY_CEILING, "a chat turn", "body"),
    (re.compile(r"^/v1/vision/analyze$"), _VISION_BODY_CEILING, "a vision image", "body"),
)

#: Any other request with a body: JSON and small forms. Four MiB, because the largest
#: self-bounded JSON reader on the gateway (`_json_body` in admin.py) accepts 4 MiB and a door
#: below it would refuse a body that route was written to take.
_BODY_CEILING_DEFAULT = 4 * 1024 * 1024

#: The upload doors a **guest API key** may open. Both are the Create surface's own; every
#: other upload door refuses the key at the route, so `_upload_credentials_ok` must not read
#: a body for a request that is certain to be refused.
_GUEST_UPLOAD_PATHS = frozenset({"/admin/api/create/transcription", "/admin/api/create/video"})

#: A private render blob is a whole video, streamed by its own route against a size the caller
#: declares and `render_service` checks. Capping it here would refuse real renders.
_BODY_CEILING_EXEMPT = (re.compile(r"^/v1/final-renders/blobs/[^/]+$"),)

#: What this process will hold in spool files at once, and how many uploads may be held.
#: **One upload of the largest ceiling always fits** (finding 4): a budget below a legitimate
#: 102 MiB transcription would answer 503 to the very upload the door exists for.
_LARGEST_CEILING = max(ceiling for _pattern, ceiling, _what, _kind in _UPLOAD_CEILINGS)
_SPOOL_BUDGET_BYTES = _LARGEST_CEILING + (16 * 1024 * 1024)
_SPOOL_MAX_IN_FLIGHT = 8
#: One host may not take the whole budget — except that one maximum upload must still fit, so
#: the share can never be below the largest ceiling (finding 5, resolved against finding 4).
_SPOOL_PER_HOST_BYTES = max(_SPOOL_BUDGET_BYTES // 4, _LARGEST_CEILING)
#: A sender that opens an upload and then dribbles (or stops) must not hold a slot for ever.
#: **Both paths are watched** (review 2026-10-29d, finding 1): no message for this long, *or*
#: fewer than `_SPOOL_MIN_PROGRESS_BYTES` inside one window, is a stall. 64 KiB per 30 s is
#: about 2 KB/s — far below any real upload on a LAN or over Tailscale, and far above the one
#: byte a minute a stuck or abandoned client sends.
_SPOOL_IDLE_TIMEOUT_S = 30.0
_SPOOL_MIN_PROGRESS_BYTES = 64 * 1024
#: Bodies below this never take a slot or a reservation: ordinary JSON is a few kilobytes and
#: making every admin POST compete for the upload pool would turn a burst of small calls into
#: 503s. An upload is what this pool is for.
_SPOOL_BUDGET_MIN_BYTES = 1 * 1024 * 1024
_spool_state = {"bytes": 0, "in_flight": 0, "hosts": {}}
_SPOOL_LOCK = threading.Lock()


def body_ceiling_for(path: str) -> tuple[int, str] | None:
    """`(ceiling, 'route' | 'default')` for this path, or `None` when it is exempt.

    The second element is what the walking test reads: a route that accepts a file, or reads its
    own stream, must be named in `_UPLOAD_CEILINGS`, so "covered by the default" is not good
    enough for one.
    """
    for pattern, ceiling, _what, _kind in _UPLOAD_CEILINGS:
        if pattern.match(path):
            return ceiling, "route"
    for pattern in _BODY_CEILING_EXEMPT:
        if pattern.match(path):
            return None
    return _BODY_CEILING_DEFAULT, "default"


def upload_door(path: str) -> tuple[int, str, bool]:
    """`(ceiling, what, is_upload)` for a named door, or the default for anything else."""
    for pattern, ceiling, what, kind in _UPLOAD_CEILINGS:
        if pattern.match(path):
            return ceiling, what, kind == "upload"
    return _BODY_CEILING_DEFAULT, "this request", False


def _spool_take(host: str, count: int) -> str | None:
    """Reserve `count` bytes for `host`; `None` when it fits, or **the limit that refused it**.

    The two byte limits are different facts — this host's share and the whole budget — and a
    reader sent to the wrong one looks at a number that was fine (review 2026-10-29f, finding 3).
    So the answer names which one tripped (`"host"` or `"bytes"`) instead of a bare `False`.

    **The slot count is not checked here.** `_spool_slot` already incremented it, so testing
    `in_flight >= _SPOOL_MAX_IN_FLIGHT` again refused the *eighth* upload on every run —
    the slot that had just been granted was the one that failed the test (review 2026-10-29d,
    finding 2). Story sends reference images with `Promise.all` and does not retry, so a
    spurious 503 fails a scene.
    """
    with _SPOOL_LOCK:
        held = _spool_state["hosts"].get(host, 0)
        if _spool_state["bytes"] + count > _SPOOL_BUDGET_BYTES:
            return "bytes"
        if held + count > _SPOOL_PER_HOST_BYTES:
            return "host"
        _spool_state["bytes"] += count
        _spool_state["hosts"][host] = held + count
        return None


def _spool_slot(host: str) -> bool:
    with _SPOOL_LOCK:
        if _spool_state["in_flight"] >= _SPOOL_MAX_IN_FLIGHT:
            return False
        _spool_state["in_flight"] += 1
        return True


def _spool_release(host: str, count: int, *, slot: bool = True) -> None:
    with _SPOOL_LOCK:
        if count:
            _spool_state["bytes"] = max(0, _spool_state["bytes"] - count)
            left = max(0, _spool_state["hosts"].get(host, 0) - count)
            if left:
                _spool_state["hosts"][host] = left
            else:
                _spool_state["hosts"].pop(host, None)
        if slot:
            _spool_state["in_flight"] = max(0, _spool_state["in_flight"] - 1)


#: Headers whose presence means "this came through a proxy", so the peer address is not the
#: caller. The same list `config.request_is_loopback` refuses.
_PROXY_HEADERS = (b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto")


def _cross_origin(scope: Any) -> bool:
    """Was this request sent by a page that is not this Gateway's own?

    A browser always sends `Origin` on a cross-origin POST. No header at all means a
    non-browser client (Story Studio, a script), which is not this question. A header is
    compared against the request's own `Host`, the same way `admin_session.same_origin` does.
    """
    from urllib.parse import urlsplit

    headers = {key.lower(): value for key, value in (scope.get("headers") or [])}
    origin = (headers.get(b"origin") or b"").decode("latin-1").strip()
    if not origin:
        return False
    host = (headers.get(b"host") or b"").decode("latin-1").strip()
    if not host:
        return True
    return urlsplit(origin).netloc.lower() != host.lower()


def _upload_credentials_ok(scope: Any) -> bool:
    """A bearer key or a valid session cookie, read from the headers alone.

    **Checked before a byte of the body is read** (review 2026-10-29c, finding 3). FastAPI parses
    a multipart body *before* it solves dependencies, so without this an unauthenticated caller
    could make the Gateway hold 32 × 102 MB of uploads and only then be told 401.
    """
    import ipaddress

    from . import admin_session
    from .config import gateway_api_key

    headers = {key.lower(): value for key, value in (scope.get("headers") or [])}
    # **`require_admin`'s rule, exactly** (review 2026-10-29d, finding 3). It trusts a
    # loopback *peer* only while the gateway is itself **bound** to loopback: on a fleet bind
    # (0.0.0.0) a request from 127.0.0.1 is an ordinary unauthenticated caller, and trusting
    # it here had the body read before the route answered 401.
    from .config import is_loopback

    if is_loopback() and not any(name in headers for name in _PROXY_HEADERS):
        peer = (scope.get("client") or ("", 0))[0]
        try:
            address = ipaddress.ip_address(peer)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            if address.is_loopback:
                # **A cross-site form is still refused.** A browser sends `Origin`; an upload
                # from a page that is not this Gateway's own is not the owner's window, even
                # when it is running on this Mac.
                if not _cross_origin(scope):
                    return True
        except ValueError:
            pass
    authorization = (headers.get(b"authorization") or b"").decode("latin-1").strip()
    if authorization.lower().startswith("bearer "):
        expected = gateway_api_key()
        token = authorization[7:].strip()
        if expected and hmac.compare_digest(token.encode(), expected.encode()):
            return True
        # A guest's API key is a credential for the Create door, which is an upload door.
        # **Only those two doors**: elsewhere the route refuses the key anyway, and the body
        # must not be spooled (or the upload budget spent) for a refusal that was certain.
        if admin_session.guest_by_key(token) is not None:
            if (scope.get("path") or "") in _GUEST_UPLOAD_PATHS:
                return True
    cookie_header = (headers.get(b"cookie") or b"").decode("latin-1")
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == admin_session.COOKIE and admin_session.session(value) is not None:
            return True
    return False


class _Stalled(Exception):
    """The body stopped arriving, or arrived too slowly. Answered as 408."""


class _BodyWatch:
    """Idle and progress watchdog for one request body.

    Wraps `receive`: silence longer than `_SPOOL_IDLE_TIMEOUT_S`, or a window in which fewer
    than `_SPOOL_MIN_PROGRESS_BYTES` arrived, raises `_Stalled`. **Both upload paths use it** —
    the chunked one catches the raise directly, the declared one (where the raise happens
    inside FastAPI's parser) marks a flag and lets the middleware answer instead.
    """

    def __init__(self, receive: Any, on_stall: Any) -> None:
        self._receive = receive
        self._on_stall = on_stall
        self.total = 0
        self._window_start = time.monotonic()
        self._window_bytes = 0

    async def __call__(self) -> dict[str, Any]:
        try:
            message = await asyncio.wait_for(self._receive(), _SPOOL_IDLE_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._on_stall()
            raise _Stalled() from None
        if message.get("type") == "http.request":
            self.total += len(message.get("body") or b"")
            now = time.monotonic()
            if now - self._window_start >= _SPOOL_IDLE_TIMEOUT_S:
                if self.total - self._window_bytes < _SPOOL_MIN_PROGRESS_BYTES:
                    self._on_stall()
                    raise _Stalled()
                self._window_start, self._window_bytes = now, self.total
        return message


class BoundedUploadBody:
    """**Bound every request that carries a body, before anything reads it.**

    An ASGI middleware, not a dependency, because **FastAPI parses the form before it solves
    dependencies** — a `Depends` guard runs after Starlette has already read (and spooled) the
    whole multipart body, which is the defect. Wrapping `receive` is the only place genuinely
    before the parser.

    * A **named door** checks its credentials first (bearer key or session cookie) and answers
      401 from the headers, so an unauthenticated caller never gets a body read.
    * A declared `Content-Length` over the ceiling is refused **on the header alone**; under it,
      the declared size is **reserved against the same budget** as a chunked body, so a burst of
      large uploads cannot all be in flight at once.
    * A chunked body is counted as it arrives, spooled only up to its ceiling, and cut there.
    * The budget is shared: bytes, concurrent uploads and a per-host share are reserved before
      anything is read, and a sender that goes quiet for `_SPOOL_IDLE_TIMEOUT_S` loses its slot.
      **It bounds how much is being received at once, not how much a route has parsed**: both are
      freed once the body is in, so a wave of uploads whose routes are still answering does not
      refuse the next wave (owner's decision, 2026-10-29).
    """

    #: Only these can carry a body; a GET is never spooled.
    METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _refusal(limit: int, size: int | None, what: str) -> JSONResponse:
        said = (f"{size // (1024 * 1024)} MB" if size
                else f"larger than {limit // (1024 * 1024)} MB")
        return JSONResponse(
            status_code=413,
            content={
                "detail": (f"That request body is {said}. This door takes at most "
                           f"{limit // (1024 * 1024)} MB for {what}."),
                "code": "UPLOAD_TOO_LARGE",
            },
        )

    @staticmethod
    def _cross_site() -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"detail": "This upload came from another site. Send it from this "
                               "Gateway's own page or with the gateway API key.",
                     "code": "CROSS_SITE_UPLOAD"},
        )

    @staticmethod
    def _unauthenticated() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"detail": "This upload door needs the gateway API key or a signed-in "
                               "session; send it before the body.",
                     "code": "UNAUTHENTICATED_UPLOAD"},
        )

    @staticmethod
    def _busy(limit: str) -> JSONResponse:
        """503 naming **the limit that actually tripped**: the slots, this host's share, or the
        whole budget.

        One sentence that named all three sent an operator to the wrong number — the slots read
        8 of 8 while the host share that refused them was "a quarter of the budget", and the
        budget itself was half empty (review 2026-10-29f, finding 3). The figure that refused is
        the figure quoted.
        """
        megabyte = 1024 * 1024
        if limit == "slots":
            detail = (f"{_SPOOL_MAX_IN_FLIGHT} uploads are already being received, and that is "
                      f"the most this Gateway holds at once.")
        elif limit == "host":
            detail = (f"This caller already holds {_SPOOL_PER_HOST_BYTES // megabyte} MB of "
                      f"uploads in flight, one host's share of the "
                      f"{_SPOOL_BUDGET_BYTES // megabyte} MB budget.")
        else:
            detail = (f"Uploads in flight already hold the whole "
                      f"{_SPOOL_BUDGET_BYTES // megabyte} MB budget.")
        return JSONResponse(
            status_code=503,
            content={"detail": f"{detail} Try again in a moment.",
                     "code": "UPLOAD_BUDGET_BUSY",
                     "limit": limit},
            headers={"Retry-After": "1"},
        )

    @staticmethod
    def _stalled() -> JSONResponse:
        return JSONResponse(
            status_code=408,
            content={"detail": (f"Nothing arrived for {int(_SPOOL_IDLE_TIMEOUT_S)} s while the "
                                f"upload was being received, so it was dropped."),
                     "code": "UPLOAD_STALLED"},
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in self.METHODS:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        resolved = body_ceiling_for(path)
        if resolved is None:
            await self.app(scope, receive, send)
            return
        limit, source = resolved
        limit, what, is_upload = upload_door(path)
        host = ((scope.get("client") or ("", 0))[0]) or "?"
        headers = {key.lower(): value for key, value in (scope.get("headers") or [])}
        declared = (headers.get(b"content-length") or b"").decode("latin-1").strip()
        # **Ceiling first, from the header alone**: a caller is told its body is too big without
        # a byte being read, and the answer is the same whether or not it authenticated.
        if declared.isdigit() and int(declared) > limit:
            await self._refusal(limit, int(declared), what)(scope, receive, send)
            return
        if source == "route" and _cross_origin(scope):
            # Checked even with credentials: a page on another origin must not upload with the
            # operator's cookie, and the answer must come before the body.
            await self._cross_site()(scope, receive, send)
            return
        if source == "route" and not _upload_credentials_ok(scope):
            # **Then credentials, still before the body** (finding 3): FastAPI would otherwise
            # parse the whole multipart before it solved `require_auth`, and an unauthenticated
            # host could make this process hold 32 × 102 MB it was going to refuse.
            await self._unauthenticated()(scope, receive, send)
            return
        if declared.isdigit():
            await self._declared(scope, receive, send, int(declared), host, is_upload)
            return
        await self._bounded_stream(scope, receive, send, limit, what, host, is_upload)

    async def _declared(self, scope: Any, receive: Any, send: Any, size: int, host: str,
                        budgeted: bool) -> None:
        """A body whose length is known: reserve it, then let the route read it.

        **The budget bounds how much is being received at once, not how much a route has parsed.**
        The slot and the byte reservation are both released the moment the last byte is in, so a
        wave of uploads whose routes are still answering does not make the next wave meet 503 —
        Story does not retry an upload and a 503 fails a scene (owner's decision, 2026-10-29:
        production traffic beats the memory concern, which only an authenticated caller could
        exploit).

        The reservation still does its real job: a burst of large bodies *arriving together* cannot
        exceed the budget, because each is reserved before a byte is read.
        """
        if not budgeted or size < _SPOOL_BUDGET_MIN_BYTES:
            # Parsed into memory and dropped, or too small to be an upload: bounded by the
            # ceiling above, but not competing for the upload pool.
            await self.app(scope, receive, send)
            return
        if not _spool_slot(host):
            await self._busy("slots")(scope, receive, send)
            return
        refused = _spool_take(host, size)
        if refused is not None:
            _spool_release(host, 0)  # the slot, not the bytes: nothing was reserved
            await self._busy(refused)(scope, receive, send)
            return
        stalled = {"hit": False}
        released = {"done": False}

        def release() -> None:
            """The slot **and** the bytes, when the body has been received.

            Idempotent, so the `finally` still covers a body that was never read.
            """
            if released["done"]:
                return
            released["done"] = True
            _spool_release(host, size)

        def note_stall() -> None:
            stalled["hit"] = True

        received = {"n": 0}

        async def counting() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "http.request":
                received["n"] += len(message.get("body") or b"")
                if received["n"] >= size or not message.get("more_body"):
                    release()
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            # FastAPI turns an exception from its parser into a 400; when the watchdog fired,
            # that answer is replaced by the 408 below, so nothing reaches the client twice.
            if stalled["hit"]:
                return
            await send(message)

        try:
            await self.app(scope, _BodyWatch(counting, note_stall), guarded_send)
        except _Stalled:
            pass
        finally:
            release()
        if stalled["hit"]:
            await self._stalled()(scope, receive, send)

    async def _bounded_stream(self, scope: Any, receive: Any, send: Any, limit: int,
                              what: str, host: str, budgeted: bool) -> None:
        """No declared length (a chunked upload): count as it arrives, spool what is kept.

        Slot and bytes are both freed when the body is complete — the budget bounds simultaneous
        reception, not what a route is still parsing (see `_declared`).
        """
        received = 0
        reserved = 0
        slot = False
        try:
            with tempfile.SpooledTemporaryFile(max_size=1 << 20) as spool:
                watched = _BodyWatch(receive, lambda: None)
                while True:
                    try:
                        message = await watched()
                    except _Stalled:
                        await self._stalled()(scope, receive, send)
                        return
                    if message.get("type") != "http.request":
                        # The client went away mid-upload; let the app see the disconnect.
                        await self.app(scope, _empty_receive(message), send)
                        return
                    body = message.get("body") or b""
                    received += len(body)
                    if received > limit:
                        await self._refusal(limit, None, what)(scope, receive, send)
                        return
                    if body:
                        if not slot:
                            # **The slot is taken when the body becomes an upload**, not when a
                            # request opens: a chunked JSON call must not compete with one.
                            if budgeted and received >= _SPOOL_BUDGET_MIN_BYTES:
                                if not _spool_slot(host):
                                    await self._busy("slots")(scope, receive, send)
                                    return
                                refused = _spool_take(host, received)
                                if refused is not None:
                                    # The slot was taken and the bytes were not: give the
                                    # slot back, or a refused upload holds one for ever.
                                    _spool_release(host, 0)
                                    await self._busy(refused)(scope, receive, send)
                                    return
                                slot = True
                                reserved = received
                        else:
                            refused = _spool_take(host, len(body))
                            if refused is not None:
                                await self._busy(refused)(scope, receive, send)
                                return
                            reserved += len(body)
                    spool.write(body)
                    if not message.get("more_body"):
                        break
                # **Freed here, before the route runs**: the body is complete, and the route
                # reading its copy from the spool is not an upload any more.
                _spool_release(host, reserved, slot=slot)
                reserved = 0
                slot = False
                spool.seek(0)

                async def replay() -> dict[str, Any]:
                    chunk = spool.read(64 * 1024)
                    if chunk:
                        return {"type": "http.request", "body": chunk, "more_body": True}
                    return {"type": "http.request", "body": b"", "more_body": False}

                await self.app(scope, replay, send)
        finally:
            _spool_release(host, reserved, slot=slot)


def _empty_receive(message: dict[str, Any]):
    """A receive that hands the app the one non-request message it already got."""
    async def replay() -> dict[str, Any]:
        return message
    return replay


def upload_limit_bytes(model_id: str | None) -> int:
    """The largest audio upload the model named will take: the Hub's, or a cloud API's 25 MB."""
    from .providers.definitions import KIND_STUDIO_HUB

    try:
        definition, _ = catalog.resolve("transcription", str(model_id or ""))
    except Exception:  # noqa: BLE001 - an unreadable model id gets the conservative ceiling
        definition = None
    if definition is not None and definition.kind == KIND_STUDIO_HUB:
        return _MAX_UPLOAD_BYTES_HUB
    return _MAX_UPLOAD_BYTES


#: How long a TERM waits for in-flight sends to get an id a restart can follow (or to settle).
#: ponytail: a fixed 15 s; a fleet send still unanswered after it is re-sent at the next start
#: (same site, same key: `worker.restart_can_resend`), a cloud one held as before. The app waits
#: this plus a margin before SIGKILL (`menubar/main.swift` `terminateBackends`): keep them in step.
_SHUTDOWN_DRAIN_S = 15.0


def _pause_dispatch_on_term() -> None:
    """TERM stops dispatch at once, not when the shutdown below finally runs.

    uvicorn installs its TERM handler before startup and then, on TERM, waits for open requests
    before running the lifespan shutdown; with Story's connections open that wait can be long, and
    the dispatcher kept sending new work all through it. Chained, so uvicorn still shuts down."""
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(sig: int, frame: Any) -> None:
        worker.pause_dispatch()
        if callable(previous):
            previous(sig, frame)

    try:
        signal.signal(signal.SIGTERM, on_term)
    except ValueError:  # not the main thread (a TestClient's portal): there is no TERM to catch
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    _pause_dispatch_on_term()
    worker.start()
    # Keeps the fleet snapshot warm so no request waits on an unreachable site.
    fleet.start_refresher()
    # Catalogue reads are memory copies from here on; the rebuild runs behind them.
    catalog.start_refresher()
    # A Studio Hub rollout the last run was in the middle of carries on (`release_rollout`).
    from . import release_rollout

    release_rollout.resume_on_startup()
    # An "Install on every Mac" the last run was in the middle of carries on (`model_share`).
    from . import model_share

    model_share.resume_on_startup()
    # The gateway only *receives* artifacts; Story Studio keeps the copy that
    # matters. Without this the directory grew without bound.
    retention.start_pruner()
    # Telegram messages about the fleet. Does nothing until the owner sets a bot token and chat id.
    notifier.start()
    # Nightly backup of keys, roster and voices; it only acts when the owner turned it on.
    from . import backup

    backup.start_scheduler()
    # Cached models belonging to a location that is no longer on the roster. The
    # cache outlived the locations: six `studio-hub-*` providers were still in it
    # with no controllers configured, one carrying a switched-on model. Inert,
    # but a stale enable/disable decision must never be inherited by a location
    # that did not make it.
    # A reference clip that is a QuickTime container named `.mp3` cannot be previewed by the
    # app; this gives it a proper WAV once (idempotent, logged), keeping the original beside it.
    try:
        from . import voice_import

        voice_import.migrate_references()
    except Exception:  # noqa: BLE001 - a repair must never stop the gateway from starting
        pass
    try:
        fleet.reconcile_cached_locations()
    except Exception:  # noqa: BLE001
        # A tidy-up must never stop the gateway from starting.
        pass
    try:
        yield
    finally:
        # **A bounded drain, so a restart never strands a job.** Take no new work, then wait for
        # every in-flight send to get an id the next start can follow (or to settle). A job still
        # rendering under such an id is not waited for: the next start resumes polling it
        # (`Worker._resume_interrupted`). Queued jobs are untouched.
        left = worker.drain(_SHUTDOWN_DRAIN_S)
        if left:
            logging.getLogger("gateway.api").warning(
                "shutdown: %d send(s) still unanswered after %.0fs: %s",
                len(left), _SHUTDOWN_DRAIN_S, ", ".join(left))
        worker.stop()
        fleet.stop_refresher()
        notifier.stop()


app = FastAPI(title="gateway-kh", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Record what the Story surface is asked for, so "is Story Studio connected?"
# is answerable from the gateway rather than by archaeology in another app's
# encrypted state. The ring itself lives in `activity.py` — see that module for
# why it is not here.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _record_story_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    path = request.url.path
    if is_story_path(path):
        record(
            request.method,
            path,
            response.status_code,
            bool(request.headers.get("authorization")),
        )
    return response


# ---------------------------------------------------------------------------
# How much of this gateway one client may hold at once. A blast-radius cap, not
# a rate limit: it does not care how many requests arrive, only how many are in
# flight, so a client that is *answered* is never refused and only a client that
# is piling up work it cannot get an answer for is.
#
# **Why it exists.** On 2026-10-19 the gateway stopped answering anything at all:
# every thread was parked on a lock a slow fleet fan-out held, and the clients
# that had piled up requests kept piling them up, so the pile was the load that
# kept the deadlock fed. A cap turns "one client can hold the whole process" into
# "one client can hold 32 of it", and a `429` with `Retry-After` tells it so
# rather than leaving it to time out.
#
# **`/health` is exempt and must stay exempt.** It is the app's readiness probe
# and the one route that has to answer while everything else is stuck; a cap that
# could refuse it would turn a busy gateway into an app that says "not running".
#
# ponytail: 32 is a flat number, not a measurement of any client. It is comfortably above the
# fan-out a client derives from `burst_concurrency_ceiling` on a gateway left at the default pool
# (`DEFAULT_GLOBAL` is 4), and **below** one whose owner has raised the pool past 32 — such a
# caller would meet `CLIENT_TOO_BUSY` on work the gateway had already promised it may send. The
# upgrade path is to raise this, or to key it on the pool (`concurrency.global_limit()`), not to
# make it adaptive; it is one constant so that stays a one-line decision.
_CLIENT_MAX_IN_FLIGHT = 32

#: `client host -> requests in flight`. Guarded by `_CLIENT_LOCK`; the whole point
#: is that this is the *only* state the cap needs, so the middleware never waits on
#: anything a request handler owns.
_CLIENT_IN_FLIGHT: dict[str, int] = {}
_CLIENT_LOCK = threading.Lock()


@app.middleware("http")
async def _cap_one_client(request: Request, call_next):  # type: ignore[no-untyped-def]
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    client = request.client.host if request.client else ""
    with _CLIENT_LOCK:
        in_flight = _CLIENT_IN_FLIGHT.get(client, 0)
        if in_flight >= _CLIENT_MAX_IN_FLIGHT:
            over = True
        else:
            _CLIENT_IN_FLIGHT[client] = in_flight + 1
            over = False
    if over:
        # The same `429` + `Retry-After` shape every other refusal here uses, so a caller
        # already handling `QUEUE_FULL` needs no new branch.
        return JSONResponse(
            status_code=429,
            content={
                "detail": (f"this client already has {in_flight} requests in flight, which is "
                           f"this gateway's per-client limit ({_CLIENT_MAX_IN_FLIGHT}). It is "
                           f"still answering the ones it has; retry shortly."),
                "code": "CLIENT_TOO_BUSY",
            },
            headers={"Retry-After": "1"},
        )
    try:
        return await call_next(request)
    finally:
        with _CLIENT_LOCK:
            left = _CLIENT_IN_FLIGHT.get(client, 1) - 1
            if left > 0:
                _CLIENT_IN_FLIGHT[client] = left
            else:
                _CLIENT_IN_FLIGHT.pop(client, None)


# ---------------------------------------------------------------------------
# Errors — Story Studio reads exactly two fields: `detail` and optional `code`.
# ---------------------------------------------------------------------------
class GatewayError(Exception):
    def __init__(self, status: int, detail: str, code: str | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.detail = detail
        self.code = code
        # **`Retry-After` is part of an honest refusal.** A `429` that does not say when to come
        # back is a wall; the fleet's own deferral cap is the number this gateway would use, so it
        # is the number the caller is given.
        self.headers = headers or {}


@app.exception_handler(GatewayError)
async def _gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail}
    if exc.code:
        body["code"] = exc.code
    return JSONResponse(status_code=exc.status, content=body, headers=exc.headers or None)


@app.exception_handler(RenderServiceError)
async def _render_service_error_handler(_: Request, exc: RenderServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code}})


# ---------------------------------------------------------------------------
# Auth. Scopes are deliberately ignored: this is a single-owner gateway, not a
# multi-tenant API, and Story Studio only uses scopes for user-facing hints.
# ---------------------------------------------------------------------------
def require_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayError(401, "Missing bearer token.", "UNAUTHORIZED")
    token = authorization[7:].strip()
    if not token:
        raise GatewayError(401, "Empty bearer token.", "UNAUTHORIZED")
    # **A guest key is never a `/v1` credential.** `/v1` is the owner's API and holds the
    # fleet, account, content and final-render surfaces; a guest reaches the Create API only.
    # Refused here, before loopback or the configured key is consulted, so the answer is the
    # same sentence the Create door uses and not an accident of which check ran first.
    # **Resolved, not merely spelled**: a configured gateway key that happened to start with
    # the guest prefix is still the owner's key.
    from . import admin_session

    if token.startswith(admin_session.GUEST_KEY_PREFIX):
        if admin_session.guest_by_key(token) is not None:
            raise GatewayError(403, "A guest API key may only use the Create API.",
                               "GUEST_NOT_ALLOWED")
    # Read per request, not from a module-level snapshot. It *was* snapshotted at
    # import, so editing `GATEWAY_API_KEY` in `.env` changed nothing and — the part
    # that matters — **after rotating the key the old one still worked**. A
    # rotation that appears to have happened and has not is worse than one that
    # clearly failed.
    configured = gateway_api_key()
    if not configured and not (is_loopback() and request_is_loopback(request)):
        raise GatewayError(
            503, "Set GATEWAY_API_KEY before using the gateway beyond loopback.",
            "API_KEY_NOT_CONFIGURED",
        )
    if configured and token != configured:
        raise GatewayError(403, "Invalid API key.", "FORBIDDEN")


def require_private_render(
    authorization: str | None = Header(default=None),
    x_story_instance: str | None = Header(default=None),
) -> AuthenticatedPrivateRender:
    """The Gateway API key, checked like `require_content_auth` (no loopback exemption, constant
    time), plus the caller's Story instance. Owner-approved 2026-10-16: one key for renders too."""
    configured = gateway_api_key()
    if not configured:
        raise PrivateRenderUnavailable()
    if not authorization or not authorization.lower().startswith("bearer "):
        raise RenderServiceError("UNAUTHORIZED", 401)
    token = authorization[7:].strip()
    if not token or x_story_instance is None or _UUID.fullmatch(x_story_instance) is None:
        raise RenderServiceError("UNAUTHORIZED", 401)
    if not hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
        raise RenderServiceError("FORBIDDEN", 403)
    installation = store.render_installation()
    return AuthenticatedPrivateRender(
        owner_scope=installation["owner_scope"],
        gateway_instance_id=installation["gateway_instance_id"],
        story_instance_id=x_story_instance,
    )


async def require_auth_async(request: Request, authorization: str | None = Header(default=None)) -> None:
    """`require_auth` on the event loop, for routes that must not wait for a worker thread."""
    require_auth(request, authorization)


#: A header a caller may set to name itself. **A hint, never a credential** —
#: there is one API key and every caller shares it, so this answers "which of my
#: tools sent this?" and never "is it allowed?". A caller that omits it is not
#: refused, and a caller that lies is not caught; it is not that kind of field.
CALLER_HEADER = "X-Caller"


def caller_hint(request: Request) -> str | None:
    """Who asked, as far as the request can say. Never refuses, never guesses.

    Recorded on the job so the question an operator actually asks at 2am — *"was
    that Story Studio, or a script I left running?"* — is answerable from the row
    instead of from memory. Asked on 2026-09-25 and unanswerable: forty-one chat
    jobs failed in one minute and the records could not say who sent them.

    The declared header wins, because a caller that names itself means it. The
    `User-Agent` is the fallback that needs no cooperation from the caller. `None`
    when the request says neither, so **"the caller said nothing" stays different
    from "the caller called itself something"** — the rule every other field here
    follows.
    """
    declared = (request.headers.get(CALLER_HEADER) or "").strip()
    if declared:
        return declared[:200]
    agent = (request.headers.get("user-agent") or "").strip()
    return agent[:200] or None


def require_content_auth(authorization: str | None = Header(default=None)) -> str:
    """Content authority never inherits the loopback placeholder-key exception."""
    configured = gateway_api_key()
    if not configured:
        raise GatewayError(503, "Content Store is not connected.", "CONTENT_STORE_NOT_CONNECTED")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise GatewayError(401, "Missing bearer token.", "UNAUTHORIZED")
    token = authorization[7:].strip()
    if not token:
        raise GatewayError(401, "Empty bearer token.", "UNAUTHORIZED")
    if token != configured:
        raise GatewayError(403, "Invalid API key.", "FORBIDDEN")
    return configured


def require_content_authority(api_key: str = Depends(require_content_auth)) -> dict[str, Any]:
    """Answer an unconfigured Store before any request-shape validation, and hand
    the handler the authority state it just read.

    FastAPI raises a missing required query parameter or a malformed body as 422,
    so a route that parses first reports an unconfigured Store as a bad request.
    Dependencies run before that validation, and `AGENTS.md` requires unconfigured
    delivery to stay `CONTENT_STORE_NOT_CONNECTED`; this keeps the two apart without
    weakening validation once a shelf exists.

    The state is returned rather than merely checked: reading it costs a Node
    signing-identity inspection (~100 ms), and the handler re-read it — measured at
    ~204 ms per content request.
    """
    from . import content_delivery
    try:
        return content_delivery.ensure_configured()
    except content_delivery.DeliveryError as exc:
        raise GatewayError(exc.status, str(exc), exc.code) from exc


def _require_idempotency_key(value: str | None) -> str:
    if not value:
        raise GatewayError(
            422, "Idempotency-Key header is required.", "IDEMPOTENCY_KEY_REQUIRED"
        )
    if not 8 <= len(value) <= 128:
        raise GatewayError(
            422, "Idempotency-Key must be 8..128 characters.", "IDEMPOTENCY_KEY_INVALID"
        )
    return value


def _named_attempt(job: dict[str, Any]) -> tuple[str, int] | None:
    """The batch and item a hold names, or `None` when it names nothing.

    **Only `recovery_context` counts.** The Hub writes it when it settles an item
    `uncertain`, and `store._recover_interrupted` writes it when the gateway
    restarts mid-flight. `upstream_job_id` alone is deliberately not enough: it
    names a batch without saying which of its items is being abandoned, and the
    operator's decision is "abandon *this* attempt". The batch and item are read
    from either spelling of the context so a context recorded by the Hub and one
    reconstructed at restart are the same fact to this function.
    """
    context = job.get("recovery_context")
    if not isinstance(context, dict) or not context:
        return None
    # **Every spelling the writers actually use.** `studio_hub.py` records
    # `batch_id` / `item_index`; a restart reconstructs `hub_batch_id` /
    # `hub_item_index`. Reading only the `hub_*`/`upstream_*` pair meant the one
    # route that clears a hold refused a hold whose attempt was named perfectly
    # well, with `RECOVERY_CONTEXT_MISSING` — measured on a real uncertain
    # submission whose context read `{"batch_id": "9a9e74a3a5", "item_index": 0}`
    # while this function looked for keys nothing had written. `worker.py` reads
    # `batch_id` too, so the omission was here alone.
    batch = str(
        context.get("hub_batch_id")
        or context.get("upstream_job_id")
        or context.get("batch_id")
        or ""
    )
    if not batch:
        return None
    try:
        index = int(
            context.get("hub_item_index")
            or context.get("upstream_item_index")
            or context.get("item_index")
            or 0
        )
    except (TypeError, ValueError):
        index = 0
    return batch, index


def _lane_contract(job: dict[str, Any]) -> dict[str, Any]:
    """The job's lane contract, as the model's own studio published it.

    Read from the cached discovery row rather than re-fetched: the contract is a
    property of the model build, not of this moment, and an operator action must
    not depend on a network read. `{}` when the lane published none — an older
    Hub, or a lane with no studio up — which every caller treats as "unstated".
    """
    provider_id = str(job.get("provider_id") or "")
    model_id = str(job.get("model_id") or "")
    if not provider_id or not model_id:
        return {}
    try:
        for model in registry.models.models_for(provider_id, include_disabled=True):
            if str(model.get("model_id") or "") != model_id:
                continue
            raw = model.get("raw") or {}
            contract = raw.get("lane_contract")
            return contract if isinstance(contract, dict) else {}
    except Exception:  # noqa: BLE001 - an unreadable contract is "unstated", not a crash
        return {}
    return {}


def _lane_discard_state(job: dict[str, Any]) -> bool | None:
    """Whether the job's lane says it can discard: `True`, `False`, or unstated.

    Hub 0.6.61 states it per lane in `lane_contract.states.discard_supported` —
    chat says `false`, because a synchronous completion has no queued attempt to
    abandon. Without reading it the operator is offered a discard the Hub will
    refuse, which is a button that fails on press.
    """
    states = _lane_contract(job).get("states")
    if not isinstance(states, dict) or "discard_supported" not in states:
        return None
    value = states.get("discard_supported")
    return value if isinstance(value, bool) else None


def recovery_action(job: dict[str, Any]) -> str | None:
    """What an operator can do about this hold, decided by the lane that owns it.

    Published on the job so the Jobs window stops inferring the action from
    `recovery_context`: only the gateway knows whether a lane has a discard route
    at all, and a button that fails on press is worse than no button.

    * `"discard_upstream"` — the lane can discard **and** the hold names the batch
      to abandon, so the Hub's own answer is the outcome.
    * `"close_hold"` — there is nothing upstream to tell: no lane other than
      Studio Hub has a discard route, Studio Hub says so per lane when its
      contract states `discard_supported: false`, and a Hub hold that names no
      batch has nothing to aim at.
    * `None` — not a hold at all.
    """
    if not job.get("recovery_required"):
        return None
    definition = registry.definition(str(job.get("provider_id") or ""))
    if definition is None:
        # The lane is gone, so no upstream can be reached; the local record can
        # still be closed by the person looking at it.
        return "close_hold"
    if getattr(adapter_for(definition), "discard", None) is None:
        return "close_hold"
    if _lane_discard_state(job) is False:
        # The lane's own statement outranks the adapter's capability: the adapter
        # has a discard call, and this lane's contract says it does not apply.
        return "close_hold"
    # A Hub hold that names no attempt (a submit that timed out before the Hub answered, from
    # before such a submit waited in the queue) used to get no action at all, and the hold sweep
    # cannot reach it either: three such rows sat from 2026-10-03 with no way to clear them.
    return "discard_upstream" if _named_attempt(job) else "close_hold"


def _discard_recovery_hold(job_id: str) -> dict[str, Any]:
    """Resolve a recovery hold, on the operator's word.

    A Hub item that settled `uncertain` keeps the node's machine reserved for
    reconciliation, and only a person can decide that waiting is over. So this is
    an explicit action with no timer anywhere near it: the Hub runs its own
    thirty-minute silence sweep as the walk-away backstop, and a gateway that
    abandoned real work automatically would be making a different and much worse
    decision.

    It refuses anything that is not a hold, because discarding live work would
    abandon a worker that may still be running — the same rule the Hub enforces
    with a 400. A refusal from the Hub is recorded and surfaced; the hold stays.

    **A lane with no discard route is closed locally, and says so.** Only Studio
    Hub can be asked to abandon an attempt; every cloud lane has no such route, so
    a cloud hold had no action at all — measured live, a job interrupted by a
    gateway restart sat on "manual recovery required" with no button in the Jobs
    window and no endpoint that would clear it. Nothing is sent upstream in that
    case (there is nobody to send it to) and the recorded sentence says exactly
    that, rather than quoting a discard that never happened.
    """
    job = store.get(job_id)
    if job is None:
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    if not job.get("recovery_state"):
        raise GatewayError(
            409,
            "This job is not waiting on a recovery hold, so there is nothing to discard.",
            "NO_RECOVERY_HOLD",
        )
    provider_id = str(job.get("provider_id") or "")
    definition = registry.definition(provider_id)
    discard = getattr(adapter_for(definition), "discard", None) if definition else None
    lane_declined = discard is not None and _lane_discard_state(job) is False
    if lane_declined:
        # The lane's own contract says it has no discard route (Hub 0.6.61,
        # `states.discard_supported: false` — a synchronous chat completion has no
        # queued attempt). Calling anyway would get a refusal and leave the hold,
        # so it takes the same path as a lane with no discard call at all.
        discard = None
    unnamed = discard is not None and not _named_attempt(job)
    if unnamed:
        discard = None
    if discard is None:
        # **Nothing upstream can be told, so the record is closed here and the
        # sentence says so.** The lane either has no discard route, says the one it
        # has does not apply here, or is no longer configured; in every case the
        # gateway would be quoting a discard that never happened if it claimed one.
        if unnamed:
            lane = "This hold does not name the upstream attempt it was waiting on"
        elif lane_declined:
            lane = "This lane's own contract states it has no discard route"
        elif definition is not None:
            lane = f"The {definition.kind} lane has no discard route"
        else:
            lane = f"Provider '{provider_id}' is no longer configured"
        detail = (
            f"An operator closed this unknown-outcome hold. {lane}, so the provider was not "
            "told and cannot be: the attempt stays abandoned. A result it produces anyway is "
            "still this job's result and is adopted if it arrives — the gateway has stopped "
            "waiting, not stopped collecting."
        )
        closed = store.close_recovery_hold(job_id, code=HOLD_CLOSED, detail=detail)
        return {
            "ok": True,
            "closed": "local",
            "discard": None,
            "job": _job_envelope(closed or store.get(job_id), native=True),
        }
    batch, index = _named_attempt(job)
    request = BackendRequest(
        provider_id=provider_id,
        model_id=str(job.get("model_id") or ""),
        modality=str(job.get("modality") or ""),
        # **The hold's own context travels with the action.** A pool has no upstream id of its
        # own — the attempt belongs to a *member* — so the member it stopped on is recorded in
        # this context (`pool.py` writes `pool_member` and the member's `provider_id` there) and
        # `PoolBackend.discard` is the only reader. Without it the pool lane had no discard route
        # at all and every pool hold was closed locally.
        params=dict(job.get("recovery_context") or {}),
        values=registry.credentials.resolved(provider_id),
        base_url=definition.base_url,
        display_name=definition.display_name,
        upstream_job_id=batch,
        upstream_item_index=index,
    )
    try:
        answer = discard(request)
    except Exception as exc:  # noqa: BLE001 - an action must report, not 500
        answer = {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(answer, dict):
        answer = {
            "ok": False,
            "status": None,
            "error": f"unreadable discard answer: {answer!r}",
        }
    # Recorded either way, so a refused discard is visible rather than silent.
    store.record_discard_remote(job_id, answer)
    if not answer.get("ok"):
        reason = answer.get("reason") or answer.get("error") or f"HTTP {answer.get('status')}"
        raise GatewayError(
            409, f"The upstream did not discard the attempt: {reason}", "DISCARD_REFUSED"
        )
    detail = (
        "An operator discarded this uncertain attempt: it was abandoned rather than "
        "reconciled, so a result the original worker may still produce is ignored."
    )
    closed = store.close_recovery_hold(
        job_id, code="DISCARDED_UNCERTAIN", detail=detail
    )
    return {
        "ok": True,
        "closed": "upstream",
        "discard": answer,
        "job": _job_envelope(closed or store.get(job_id), native=True),
    }


def _reconcile_recovery_hold(job_id: str) -> dict[str, Any]:
    """Ask the upstream to settle a hold from its original worker, on the operator's word.

    The alternative to discarding: a node that finished the work while its controller restarted
    still holds the result, and discarding throws it away (2026-10-05, five Z-Image renders). The
    Hub reads only the original job, adopts it if it finished or records the worker's own failure,
    and releases the Mac either way; nothing is resubmitted. The hold stays as it is here - the
    hold sweep (`worker.reconcile_holds`, each minute) adopts a `done` item or closes a failed one
    from the Hub's own record - so this route only asks, and reports what the Hub said.
    """
    job = store.get(job_id)
    if job is None:
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    if not job.get("recovery_state"):
        raise GatewayError(
            409, "This job is not waiting on a recovery hold, so there is nothing to reconcile.",
            "NO_RECOVERY_HOLD",
        )
    provider_id = str(job.get("provider_id") or "")
    definition = registry.definition(provider_id)
    reconcile = getattr(adapter_for(definition), "reconcile", None) if definition else None
    if reconcile is None:
        raise GatewayError(409, "This lane has no reconcile route; discard or close the hold instead.",
                           "NO_RECONCILE_ROUTE")
    batch, index = _named_attempt(job) or ("", 0)
    if not batch:
        raise GatewayError(
            409, "This hold does not name the upstream attempt it is waiting on.",
            "RECOVERY_CONTEXT_MISSING",
        )
    request = BackendRequest(
        provider_id=provider_id, model_id=str(job.get("model_id") or ""),
        modality=str(job.get("modality") or ""),
        # The hold's own context, for the same reason as the discard route: it is what names a
        # pool's member (`PoolBackend.reconcile`).
        params=dict(job.get("recovery_context") or {}),
        values=registry.credentials.resolved(provider_id), base_url=definition.base_url,
        display_name=definition.display_name, upstream_job_id=batch, upstream_item_index=index,
    )
    try:
        answer = reconcile(request)
    except Exception as exc:  # noqa: BLE001 - an action must report, not 500
        answer = {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(answer, dict):
        answer = {"ok": False, "status": None, "error": f"unreadable reconcile answer: {answer!r}"}
    hub = answer.get("answer") if isinstance(answer.get("answer"), dict) else {}
    if not answer.get("ok"):
        reason = (hub.get("detail") or answer.get("reason") or answer.get("error")
                  or f"HTTP {answer.get('status')}")
        raise GatewayError(409, f"The upstream did not reconcile the attempt: {reason}",
                           "RECONCILE_REFUSED")
    item = hub.get("item") if isinstance(hub.get("item"), dict) else {}
    return {
        "ok": True,
        "reconciled": bool(hub.get("reconciled")),
        "item_state": item.get("state"),
        "reconcile": answer,
        "job": _job_envelope(store.get(job_id), native=True),
    }


def _resolve_all_recovery_holds(before: float | None = None) -> dict[str, Any]:
    """"Resolve All Needing Recovery": the per-job buttons, pressed once for every hold.

    For each `failed` job still needing recovery (last changed at or before `before`, so work
    that went on hold after the owner confirmed is left alone): ask the Hub first, exactly as
    Recover Result does, and leave a finished result for the hold sweep to collect; otherwise do
    exactly what Discard Attempt / Close does. A refusal is reported per job and that hold stays.
    """
    from .worker import _DONE_UPSTREAM_STATES

    cutoff = time.time() if before is None else before
    out: dict[str, Any] = {"recovered": 0, "discarded": 0, "left": []}
    # ponytail: one pass over at most 1,000 holds; press again if there were ever more.
    for job in store.holds(limit=1000):
        if not job.get("recovery_required") or job.get("state") != "failed":
            continue
        if float(job.get("updated_at") or 0) > cutoff:
            continue
        job_id = str(job["id"])
        try:
            if recovery_action(job) == "discard_upstream":
                try:
                    answer = _reconcile_recovery_hold(job_id)
                except GatewayError:
                    answer = {}
                if answer.get("reconciled") and str(answer.get("item_state") or "") in _DONE_UPSTREAM_STATES:
                    out["recovered"] += 1
                    continue
            _discard_recovery_hold(job_id)
            out["discarded"] += 1
        except GatewayError as exc:
            out["left"].append({"id": job_id, "code": exc.code, "detail": exc.detail})
    return {"ok": True, **out}


def _load_job(job_id: str, modality: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job is None or job["modality"] != modality:
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return job


def _result_of(job: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(job.get("result_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _lifecycle_fields(job: dict[str, Any]) -> dict[str, Any]:
    """Shared recovery facts for Story and the owner's job library."""
    return {
        **{key: bool(job.get(key)) for key in ("cancel_requested", "retryable", "recovery_required")},
        **{key: job.get(key) for key in (
            "cancel_state", "recovery_state", "recovery_detail", "upstream_job_id", "late_result_state",
            "cancel_remote",
        )},
    }


# ---------------------------------------------------------------------------
# The job envelope's contract additions (2026-09-30): dispatch state, cancellation, waiting.
# ---------------------------------------------------------------------------
_HUB_ROUTE = re.compile(r"(?:https?://[^\s/'\"]+|[\w.-]+:\d+)?/api/hub[^\s'\"),]*")
_ADDRESS = re.compile(
    r"https?://[^\s/'\"]+"
    r"|\b(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|[\w-]+(?:\.[\w-]+)+):\d{2,5}\b"
)


_CONTROLLER_ID = re.compile(r"\bstudio-hub-[A-Za-z0-9][A-Za-z0-9_-]*")


def _label_controller(match: "re.Match[str]") -> str:
    """A location's own label where the owner has named it, else 'a Mac' -- never the id."""
    try:
        return fleet.label_for_provider(match.group(0)) or "a Mac"
    except Exception:  # noqa: BLE001 - a label must never fail a publish
        return "a Mac"


def scrub(value: Any) -> Any:
    """Remove a Hub's `host:port` and `/api/hub/...` routes from anything about to be published.

    Story Studio's invariant is that it never learns a second address. It never *calls* one, but
    the address still crossed inside `failure_reason`, `recovery_detail` and the cancel record. One
    helper, applied to every published text field, so the next field cannot forget it.
    """
    if isinstance(value, str):
        return _CONTROLLER_ID.sub(
            _label_controller, _ADDRESS.sub("the location", _HUB_ROUTE.sub("the Hub", value)))
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def _scrub_failure_evidence(value: Any) -> Any:
    """Scrub diagnostics while keeping generated partial responses verbatim."""
    if isinstance(value, dict):
        return {
            key: item if key == "partial_response" else _scrub_failure_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_failure_evidence(item) for item in value]
    return scrub(value)


def _iso(moment: Any) -> str | None:
    if not isinstance(moment, (int, float)):
        return None
    return datetime.fromtimestamp(float(moment), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: How soon a caller should ask again, by lane. A 30-second video does not change once a second,
#: and a fixed 1000 pinned Story to ~1 GET/s per job for the whole render.
_POLL_AFTER_MS = {"chat": 1000, "tts": 1500, "image": 3000, "video": 5000, "music": 5000,
                  "transcription": 5000}


#: Lanes whose running work a cancel really stops (Hub worker confirmed: voice ~5 s, music at once).
_STOPPABLE_WHEN_RUNNING = frozenset({"tts", "music"})


def _contract_fields(job: dict[str, Any]) -> dict[str, Any]:
    """`dispatch_state`, `cancellable`, `cancellation_requested`, `cancelled`, `waiting`.

    `pre_dispatch` is "nothing has been sent upstream yet", which is also exactly when a cancel is
    honoured now (`store.cancel` settles it locally). `waiting` is only ever published while the job
    is running here but its Hub item is still queued, and is `null` the moment a Mac starts.
    """
    state = job["state"]
    pre = state == "accepted" or (
        job.get("dispatch_started_at") is None and not job.get("upstream_job_id"))
    waiting = job.get("waiting") if state == "running" else None
    if isinstance(waiting, dict):
        waiting = {"reason": scrub(str(waiting.get("reason") or "waiting for a free Mac")),
                   "next_try_at": _iso(waiting.get("next_try_at")),
                   "since": _iso(waiting.get("since"))}
    else:
        waiting = None
    # **A job waiting for a Mac is cancellable too.** The Hub answers a DELETE for an item that is
    # still `queued` by cancelling it on the spot (nothing ran), and the poll then sees
    # `cancelled` and settles the job. Story already offers Cancel for "dispatched + waiting +
    # cancellable" (`job-control.ts`), but this field was only ever true before dispatch, so a job
    # that could sit in a queue for hours (QUEUE_WAIT_CEILING_S) had no way out (audit 2026-10-01).
    # **A running narration or music job is cancellable too** (owner decision 2026-10-01). The cancel
    # goes down to the Hub (`DELETE /api/hub/jobs/{id}`), the Hub tells the worker, the voice worker
    # stops within seconds and the music worker at once, both report `cancelled`, and the Mac is
    # released the moment they do. Images and Whisper cannot be interrupted mid-render and chat has no
    # batch to cancel, so they stay `false` and Story does not offer it.
    stoppable = (state == "running" and job.get("modality") in _STOPPABLE_WHEN_RUNNING
                 and bool(job.get("upstream_job_id")))
    return {
        "dispatch_state": "pre_dispatch" if pre else "dispatched",
        "cancellable": (state not in TERMINAL_STATES and not job.get("cancel_requested")
                        and (pre or waiting is not None or stoppable)),
        "cancellation_requested": bool(job.get("cancel_requested")),
        "cancelled": state == "cancelled",
        "waiting": waiting,
    }


def _cancel_upstream(job: dict[str, Any]) -> dict[str, Any] | None:
    """Ask whoever owns dispatched work to stop it (see `worker.cancel_upstream`)."""
    return cancel_upstream(job, store)


def _served_model_fields(job: dict[str, Any]) -> dict[str, Any]:
    try:
        params = json.loads(job.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict) or not params.get("_pool_id"):
        return {}
    return {
        "served_model_id": job.get("model_id"),
        "served_model_name": job.get("model_id"),
    }


def _job_envelope(job: dict[str, Any], *, native: bool = False) -> dict[str, Any]:
    """The job as Story Studio reads it.

    `native=True` is the menu bar app's own reading: it does not scrub, because the owner's
    operator views want the address. Both readings publish the exact state: a cancelled job is
    `state: "cancelled"` (what the database says), with `cancelled: true` kept beside it.
    """
    modality = job["modality"]
    result = _result_of(job)
    payload = result.get("payload") or {}
    # A cancelled job is published as `state: "cancelled"` plus `cancelled: true` (audit 2026-10-02:
    # it used to say `failed`, which disagreed with the record and with the menu bar app). Story
    # Studio reads both spellings as the same terminal fact (`genstudio-client.ts` maps a
    # `state: "cancelled"` body to failed + cancelled and settles on `cancelled: true`), so a
    # caller keyed on either one still stops polling. `failure_code: CANCELLED` stays for them.
    is_cancelled = job["state"] == "cancelled"

    body: dict[str, Any] = {
        "id": job["id"],
        "state": job["state"],
        "model_id": catalog_encode(job),
        **_served_model_fields(job),
        "priority": job.get("priority") or NORMAL,
        "failure_code": None,
        "failure_class": None,
        "failure_reason": None,
        "routing_status_code": None,
        "queue_position": None,
        "queue_eta_seconds": None,
        "queue_reason": None,
        "retry_after_seconds": None,
        "retry_at": None,
        "poll_after_ms": _POLL_AFTER_MS.get(modality, 3000) if job["state"] not in TERMINAL_STATES
        else 1000,
        **_lifecycle_fields(job),
        **_contract_fields(job),
        # **Who asked**, or `null` for a job recorded before the Gateway knew. The page prints
        # "not recorded" for null; it never falls back to "owner", which would be a claim.
        "requested_by": requested_by_of(job),
        **ZERO_SETTLEMENT,
    }

    if job["state"] == "succeeded":
        if modality == "chat":
            body["output_text"] = result.get("text") or ""
            # **A deliberate exception to "publish what is known".** A provider that
            # states no finish reason at all is published as a clean stop, because
            # this field is a hard gate on the consumer's side: it refuses anything
            # that is not a clean stop as a truncated response, so publishing
            # `null` — the honest value — would turn a *working* generation into a
            # failure. We only reach here with `state == "succeeded"` and text in
            # hand, which is what a completed generation looks like, so `stop` is
            # the almost-certainly-true reading rather than an invented one.
            #
            # A reason the provider *does* state is never rewritten: `length`,
            # `content_filter` and anything unrecognised pass through so the
            # consumer refuses them. Marked rather than silent because it is the
            # one place here that chooses a useful value over a strictly-known one.
            body["finish_reason"] = payload.get("finish_reason") or "stop"
            # **Two facts about the numbers, kept apart.** `generation_seconds` is
            # the worker's *own* generation time when the lane states one (Hub
            # 0.6.63 measures it after the model is resident, so it excludes a cold
            # turn's load) — a different fact from this gateway's round trip, and
            # the Hub asks that a rate be derived from this rather than from ours.
            # `verified_token_usage` is `false` for the same reason the catalogue
            # says so: the counts were passed through, not re-measured. Both are
            # `null` when the lane stated neither, because "unstated" and "zero"
            # must not render alike.
            if payload.get("generation_seconds") is not None:
                body["generation_seconds"] = payload.get("generation_seconds")
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage:
                body["verified_token_usage"] = False
        elif modality == "image":
            url = f"/v1/images/{job['id']}/file"
            body["image_url"] = url
            body["output_url"] = url
        elif modality == "tts":
            url = f"/v1/jobs/{job['id']}/audio"
            body["audio_url"] = url
            body["output_url"] = url
        elif modality == "transcription":
            url = f"/v1/transcriptions/{job['id']}/srt"
            body["srt_url"] = url
            body["output_url"] = url
        elif modality == "video":
            url = f"/v1/videos/{job['id']}/file"
            body["video_url"] = url
            body["output_url"] = url
        elif modality == "music":
            # Story Studio reads music results as an array of tracks, each with
            # its own asset id, so a single-artifact shape would not be read. The
            # array is every artifact the job kept — Suno answers several
            # variations per call, and a fixed one-entry array is what made the
            # second track unreachable.
            outputs = [
                {
                    "asset_id": artifact["asset_id"],
                    "url": f"/v1/music/jobs/{job['id']}/outputs/{artifact['asset_id']}",
                }
                for artifact in artifacts_of(job)
            ]
            body["outputs"] = outputs
            if outputs:
                body["output_url"] = outputs[0]["url"]
        # **Every artifact a job kept is published, not just the first.** A provider
        # can answer several variations for one request — Suno returns one entry per
        # track, an image call with `n` returns one image each — and the singular
        # `*_url` fields above can only ever name one of them, so the rest were
        # written to disk and then unreachable by anyone. The singular fields stay
        # exactly as they were (no existing reader changes) and the full list rides
        # alongside them. Music builds its own list above, under the route prefix it
        # has always published, so it is left alone.
        if "outputs" not in body:
            outputs = [
                {
                    "asset_id": artifact["asset_id"],
                    "url": f"/v1/jobs/{job['id']}/outputs/{artifact['asset_id']}",
                }
                for artifact in artifacts_of(job)
            ]
            if outputs:
                body["outputs"] = outputs
        # A variation that would not download is *stated* rather than silent: a
        # caller handed a shorter list than the provider produced can tell that from
        # the response instead of having to guess why.
        if payload.get("variations_failed"):
            body["variations_failed"] = payload["variations_failed"]
        if payload.get("usage"):
            body["usage"] = payload["usage"]
        # How many voice sections the Mac re-made after its quality check, per section.
        if payload.get("section_quality"):
            body["section_quality"] = payload["section_quality"]
        # What the provider says it *charged* for this job — stated, never computed from tokens.
        body.update(job_cost(payload))

    if job["state"] == "failed":
        body["failure_code"] = job.get("error_code") or "PROVIDER_FAILED"
        body["failure_class"] = {
            "POLICY_REFUSAL": "content_refused",
            "INCOMPLETE_OUTPUT": "incomplete_output",
        }.get(body["failure_code"], "provider_error")
        body["failure_reason"] = job.get("error_detail") or "generation failed"
        for key in ("partial_response", "pool_attempts"):
            if payload.get(key) is not None:
                body[key] = payload[key]
    elif is_cancelled:
        body["failure_code"] = "CANCELLED"
        body["failure_class"] = "cancelled"
        body["failure_reason"] = "The job was cancelled."

    # **Which Mac ran it, and for how long** - for a failed Studio Hub item too (Breeze night-atlas
    # 2026-10-05: the failures could only be placed on a Mac from the log). Absent when unstated.
    for key in ("hub_machine", "hub_batch_id", "run_seconds"):
        if payload.get(key) is not None:
            body[key] = payload[key]

    _publish_attempt_evidence(body, job)
    _publish_queue_evidence(body, job)

    if not native:
        for key in ("failure_reason", "recovery_detail", "cancel_remote", "attempts",
                    "pool_attempts", "queue_reason"):
            if body.get(key) is not None:
                body[key] = (_scrub_failure_evidence(body[key]) if key == "pool_attempts"
                             else scrub(body[key]))
    return body


def _publish_attempt_evidence(body: dict[str, Any], job: dict[str, Any]) -> None:
    """Say which machines this job has been tried on, and what the policy allows (A7).

    The owner's decision E put the policy in two places by name: **the document, and "the same
    policy stated on the Gateway's Jobs view so an operator can see attempt *n* of 3"**. An
    operator watching a job move between machines had nothing to read: `attempt_count` counts
    every dispatch including pure queue deferrals, so it is not the number the ceiling is
    measured against, and the machines were not published at all.

    Additive, and only published when there is something to say — a job still on its first
    machine carries no `attempts` key rather than an empty list, so "never retried" and "we did
    not look" stay distinguishable.
    """
    attempts_of = getattr(store, "attempts_of", None)
    attempts: list[dict[str, Any]] = []
    if attempts_of is not None:
        try:
            attempts = attempts_of(job["id"])
        except Exception:  # noqa: BLE001 - an attempt figure must never fail a job response
            attempts = []
    if not attempts:
        return
    from . import concurrency

    limit = int(concurrency.retry_policy()["max_attempts"])
    body["attempts"] = [
        {
            "number": index + 1,
            "machine": entry.get("location"),
            "failure_code": entry.get("code"),
            "failure_reason": entry.get("detail"),
            "at": entry.get("at"),
        }
        for index, entry in enumerate(attempts)
    ]
    body["attempts_used"] = len(attempts)
    body["attempts_allowed"] = limit
    body["attempt_policy"] = concurrency.retry_policy_wording()


def _publish_queue_evidence(body: dict[str, Any], job: dict[str, Any]) -> None:
    """Say when a job is waiting in the gateway's own queue, in the words Story already reads.

    `store.defer()` puts a job back with a `next_action_at` and a reason when every location that
    offers the model is merely busy, and this envelope published `null` for all three queue fields —
    so a queued job and a slow one looked identical to the caller, and Story's Jobs page (which has
    read these fields all along, and renders *"Waiting in the GenStudio provider queue… position N…
    about 2 minutes… pacing new requests"*) could never show the queue the owner asked for.

    **Waiting is `accepted`, with or without a deferral.** `store.submit` opens every job at
    `next_action_at = now + 0.8`, so a job that has never been refused is still queued behind
    everything in front of it; publishing only when a `defer_reason` was set made "waiting for a
    slot" and "already running" the same payload. A job that is *running* publishes nothing here,
    so absent still means "not in this queue" rather than position zero.

    The vocabulary is Story's own: `queue_reason` is exactly `provider_pace | null` in its contract,
    so this fills that field rather than inventing a second one — and only when the gateway really
    did put the job back because a lane refused it. A fresh submission is queued, not paced, and
    saying otherwise would be a claim about the provider nobody measured.

    `queue_eta_seconds` is **when this gateway will try again**, which is the only figure it can
    honestly give: it is not a prediction of when the provider will finish, and it is not published
    as one.

    `queue_size` is the number bounding the position — the whole waiting set of that modality — so
    "position 3" can be read as "3 of 7" rather than as "third of an unknown many". It is additive:
    Story reads `queue_position`, `queue_eta_seconds` and `queue_reason` and ignores the rest.
    """
    if str(job.get("state") or "") != "accepted":
        return
    position: int | None = None
    size: int | None = None
    # `queue_rank` is the reader of the queue rule; a store that predates it still
    # answers the single number, so a waiting job is never published without one.
    rank_of = getattr(store, "queue_rank", None)
    if rank_of is not None:
        try:
            rank = rank_of(job["id"])
        except Exception:  # noqa: BLE001 - a queue figure must never fail a job response
            rank = None
        if isinstance(rank, dict):
            state_position = rank.get("position")
            state_size = rank.get("size")
            position = state_position if isinstance(state_position, int) else None
            size = state_size if isinstance(state_size, int) else None
    if position is None:
        position_of = getattr(store, "queue_position", None)
        if position_of is not None:
            try:
                fallback = position_of(job["id"])
            except Exception:  # noqa: BLE001 - a queue figure must never fail a job response
                fallback = None
            position = fallback if isinstance(fallback, int) else None
    if position is not None and position >= 1:
        body["queue_position"] = position
        if size is not None and size >= position:
            body["queue_size"] = size
    if job.get("defer_reason"):
        body["queue_reason"] = "provider_pace"
    next_action = job.get("next_action_at")
    if isinstance(next_action, (int, float)):
        body["queue_eta_seconds"] = max(0, int(round(next_action - time.time())))


def catalog_encode(job: dict[str, Any]) -> str:
    """Rebuild the namespaced id Story Studio sent, so its bookkeeping matches.

    Which location actually ran the job is deliberately not part of this. The job
    stores the concrete location it was routed to, but the id Story Studio is told
    back has to be the id it sent, or a saved preset stops matching its own jobs.
    `publish_id` collapses a location back to the maker's namespace, which is the
    namespace the catalogue published.
    """
    from . import fleet
    from .catalog import encode_model_id

    provider_id = job.get("provider_id") or ""
    model_id = job.get("model_id") or ""
    try:
        params = json.loads(job.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    pool_id = params.get("_pool_id") if isinstance(params, dict) else None
    if pool_id:
        return encode_model_id("studio-hub", str(pool_id))
    if not provider_id:
        return model_id
    return encode_model_id(fleet.publish_id(str(provider_id)), model_id)


def _pinned_location(params: dict[str, Any]) -> str | None:
    """The location a request already names, if it names one.

    A voice id carries its owner (`studio-hub-<controller>::<raw>`), so a voice whose
    clip exists on one Hub only must go to that location. A clone from the gateway's
    own library travels with the job (its clip is staged wherever it runs) and a
    cloud voice names no location, so neither pins anything.
    """
    from . import fleet

    paused = fleet.pause_message_for_voice(params)
    if paused:
        raise GatewayError(409, paused, "VOICE_ON_PAUSED_LOCATION")
    return fleet.pinned_location(params)


def _refuse_when_the_queue_is_full(background: bool = False) -> None:
    """Refuse a new job when the gateway's own queue is at its published ceiling.

    `max_queued_jobs` is published in the admission record Story Studio reads, and Story holds
    itself to it — but nothing here did, so the queue this process owns was unbounded while the
    caller believed it was bounded (audit M8, and H7's family of published-but-unread numbers).

    **`429` with `Retry-After`, never a silent accept and never `402`.** A refusal the caller can
    act on is better than a job that sits in a queue nobody promised to drain; the alternative
    considered and rejected was deleting the field and leaving the queue unbounded, which would
    have made the published number a lie in the other direction.
    """
    limit = int(admission()["max_queued_jobs"])
    waiting_of = getattr(store, "waiting_jobs", None)
    if waiting_of is None:
        return
    try:
        # **Background work never fills the queue for production (0.2.165).** A normal job counts
        # only normal jobs waiting; a background job counts everything, so a 500-song pool is held
        # to the ceiling while a narration job behind it is still accepted.
        waiting = len(waiting_of(limit=limit + 1) if background
                      else waiting_of(limit=limit + 1, normal_only=True))
    except Exception:  # noqa: BLE001 - a queue figure must never fail a submission
        return
    if waiting <= limit:
        return
    from . import concurrency

    retry_after = int(concurrency.queue_wait_budget()["defer_cap_seconds"])
    raise GatewayError(
        429,
        (f"this gateway is already holding {waiting} waiting jobs, its published ceiling "
         f"(max_queued_jobs) is {limit}, and it will not accept more work than it can honestly "
         f"queue. Retry after {retry_after} seconds."),
        "QUEUE_FULL",
        headers={"Retry-After": str(retry_after)},
    )


def _refuse_when_the_disk_is_low(extra_bytes: int = 0) -> None:
    """`503 DISK_LOW` when the Mac is under 5 GB free, before anything is written.

    A red volume is swept first (`diskguard.require_room`), so the refusal is for a disk the
    gateway could not clear itself. A sentence a person can act on, not a failed write.
    """
    from . import diskguard

    try:
        diskguard.require_room(extra_bytes)
    except diskguard.DiskLow as low:
        raise GatewayError(503, low.sentence, "DISK_LOW", headers={"Retry-After": "300"}) from low


#: The optional `output_format` request, per modality: the container a Studio Hub lane should
#: deliver instead of its native WAV / PNG. Absent means native, which is what every caller before
#: this field got. One fixed preset per name (AAC / JPEG) lives in the Hub; there is no quality knob.
#:
#: **A floor, not the whole answer.** A value outside this list is still admitted when the named
#: model's own catalogue row publishes it (`_published_output_formats`) — a lane is the authority
#: on what it accepts, so the door must not refuse a format the catalogue advertises.
#: This list is what the rest falls back to: a request naming no model, a model whose lane states
#: no `output_format` enum, and the cloud providers. Music carries `flac` because a Studio Hub
#: music lane really delivers it — YuE2's native file *is* FLAC (the Hub music lane's
#: `_native_format`) and `adapters/studio_hub._FORMAT_BY_MEDIA` already maps `audio/flac` to
#: `.flac`, so the door was refusing a container it could already describe and receive.
OUTPUT_FORMATS = {
    "image": ("png", "jpeg"),
    "music": ("wav", "flac", "m4a"),
    "tts": ("wav", "m4a"),
}


def _validate_tts_pauses(payload: dict[str, Any]) -> None:
    """Refuse a join pause that is not a number of seconds >= 0 before anything is queued.

    The upper bound is the Hub lane's own (published on the row as `pause_parameters`); it refuses
    a value past it by name. `null` is the same as absent.
    """
    for name in TTS_PAUSE_PARAMETERS:
        value = payload.get(name)
        if value is None:
            payload.pop(name, None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
            raise GatewayError(
                400, f"{name} must be a number of seconds, 0 or more; got {value!r}.", "INVALID_PARAMS")


def _published_output_formats(modality: str, payload: dict[str, Any]) -> tuple[str, ...]:
    """The delivery formats the named model's own catalogue row publishes, or `()`.

    **One source of truth: the row the catalogue publishes.** A Studio Hub lane states its delivery
    formats in its own `lane_contract`; `catalog._output_formats` relays them onto the row, and this
    reads that same declaration back, so a row can never advertise a format the door refuses. A
    YuE2 music lane declares `["flac", "m4a"]` and its native file *is* FLAC, while this door
    answered 400 for `flac` — the catalogue offered a container the door would not take.

    `catalog.resolve` returns the *inventory* model, not the published row (the same distinction
    `tts_quote` records), so the row's own `_output_formats` is what is read. Nothing is invented:
    a model whose lane states no enum, a model this gateway does not offer, and a request naming no
    model all answer `()`, and the flat list stands.
    """
    compound = str(payload.get("model_id") or payload.get("model") or "")
    if not compound:
        return ()
    definition, model = catalog.resolve(modality, compound)
    if definition is None or model is None:
        return ()
    # `catalog` here is the catalogue *instance*; the row builder is a module function.
    from .catalog import _output_formats

    published = _output_formats(definition, model.get("raw") or {}).get("output_formats")
    return tuple(published or ())


def _validate_output_format(modality: str, payload: dict[str, Any]) -> None:
    """Refuse an `output_format` the modality does not define, before anything is queued or charged.

    Normalised in place (` JPEG ` -> `jpeg`) so the stored job and the Hub see one spelling. `null`
    is the same as absent. What a modality defines is its flat list *plus* whatever the named
    model's own row publishes (`_published_output_formats`) — the lane owns its own vocabulary.
    """
    value = payload.get("output_format")
    if value is None:
        payload.pop("output_format", None)
        return
    cleaned = value.strip().lower() if isinstance(value, str) else None
    allowed = OUTPUT_FORMATS[modality]
    if cleaned not in allowed:
        # **Only a value the flat list refuses needs the lane asked.** A caller sending `m4a` or
        # `png` — which is every caller before this field existed — never pays for a resolution.
        allowed = tuple(dict.fromkeys([*allowed, *_published_output_formats(modality, payload)]))
    if cleaned not in allowed:
        raise GatewayError(
            400,
            f"output_format must be one of {', '.join(allowed)} for a {modality} request; got {value!r}. "
            "Omit it to receive the lane's native file.",
            "INVALID_PARAMS",
        )
    payload["output_format"] = cleaned


def _replay_marked(envelope: dict[str, Any], created: bool) -> dict[str, Any]:
    """`idempotent_replay: true` when this POST handed back a job an earlier request already made.

    Story used to infer it from the HTTP status (200 rather than 202); the body now says it.
    """
    envelope["idempotent_replay"] = not created
    return envelope


def idempotency_scope(requested_by: dict[str, Any] | None) -> str:
    """The prefix an idempotency key is stored under, so one caller's key is their own.

    **A key identifies a request, and a request belongs to somebody.** One flat keyspace let
    a guest that guessed (or read) a key be handed the owner's job — its envelope, its model
    and, once finished, its output text — and let the same guess sit on a key that an owner
    or Story Studio submission would later land on. Keys are now stored per requester:

      * `guest:<guest id>` for a guest — the only untrusted caller;
      * *nothing* for the owner and the gateway key, which is what every row written before
        this change holds, so a retry of an in-flight key still finds its job across the
        upgrade.

    A guest can therefore neither read nor block anyone else's key, and their own retries
    still work.
    """
    data = requested_by or {}
    if data.get("role") == "guest":
        return f"guest:{data.get('guest_id') or ''}"
    return ""


def scoped_idempotency_key(scope: str, key: str) -> str:
    return f"{scope}:{key}" if scope and key else key


def _replay_is_theirs(job: dict[str, Any], requested_by: dict[str, Any]) -> bool:
    """Is this stored job the one *this* caller may be handed back?

    A guest's answer must never be somebody else's job, so a row that names a different
    guest (or no guest at all) is not a replay for them. The owner and the key are trusted
    with every job, which is what they already have.
    """
    data = requested_by_of(job) or {}
    if requested_by.get("role") == "guest":
        return data.get("role") == "guest" and data.get("guest_id") == requested_by.get("guest_id")
    return True


def _submit(
    modality: str,
    compound_id: str,
    params: dict[str, Any],
    idempotency_key: str,
    caller: str | None = None,
    requested_by: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Resolve a catalogue id and queue a job.

    Resolution is against *enabled* models, so a disabled toggle is honoured on
    the generation path and the catalogue and dispatch can never disagree.

    **The queue has a ceiling, and it is enforced here** (the owner's decision on
    `max_queued_jobs`: *"enforce it with a clean 429 + Retry-After, and raise the default to 256"*).
    The number was published to Story Studio, which holds itself to it, and read by nothing on this
    side — so the queue was unbounded in the one process that owns it (audit M8). One guard here,
    on the single path every generation route funnels through, rather than a check per route that
    the next route would forget.

    **`job_group` is taken out of the params here (D6)**, so no adapter ever forwards it to a Hub;
    it is stored on the job for the dispatcher's fair turns and shown on the Jobs page. A value
    that is not a short id (`[A-Za-z0-9._:/-]`, at most 120) is ignored — it is advisory, and a
    tag is never a reason to refuse work.
    """
    job_group = params.pop("job_group", None) if isinstance(params, dict) else None
    job_group = job_group if isinstance(job_group, str) and _JOB_GROUP.fullmatch(job_group) else None
    priority = _pop_priority(params)
    requester = requested_by or {
        # Every `/v1` job is the gateway key's: there is one key and Story Studio holds it.
        # A client that names itself (`X-Caller`) is quoted; otherwise it is Story Studio.
        "role": "key",
        "label": (caller or "").strip() or "Story Studio",
    }
    # **The key belongs to the requester** (see `idempotency_scope`), and a replay is only ever
    # the caller's own job. Both are needed: the scope keeps a guest out of everyone else's
    # keyspace, and the check keeps a row that somehow carries a foreign name from being
    # handed over anyway.
    scope = idempotency_scope(requester)
    if idempotency_key:
        idempotency_key = scoped_idempotency_key(scope, idempotency_key)
    # **A replay is not a new submission, so it is answered before anything can refuse it.** A
    # caller whose 202 was lost re-POSTs the same key; the job already exists, and a full queue,
    # a model that went offline since, or an unresolvable id must not turn "here is the job you
    # made" into a 429/503/404 (and a 429 puts Story on an account-wide hold). The same
    # cross-modality guard as below applies.
    if idempotency_key:
        already = store.find_by_idempotency_key(idempotency_key)
        if already is not None and not _replay_is_theirs(already, requester):
            # Unreachable by construction — a guest's scope is theirs alone — but if it ever
            # happens, the answer is a job of their own, never somebody else's row.
            idempotency_key = f"{idempotency_key}:{uuid.uuid4().hex[:8]}"
            already = None
        if already is not None:
            if already["modality"] != modality:
                raise GatewayError(
                    409,
                    f"That Idempotency-Key was already used for a {already['modality']} request. "
                    f"A key identifies one request; use a new one for this {modality} request.",
                    "IDEMPOTENCY_KEY_REUSED",
                )
            return already, False
    _refuse_when_the_queue_is_full(priority == BACKGROUND)
    _refuse_when_the_disk_is_low()
    from . import pools as _pools

    pool_name = _pools.image_pool_name(compound_id) if modality == "image" else None
    if pool_name is not None:
        params = dict(params or {})
        params["_pool_id"] = pool_name

    # **A job's own parameters decide what the location must be able to do.** Reference-guided
    # images may only run where the worker advertises `edit`; every other modality and every
    # prompt-only image job states no requirement, so an old or non-reference location still
    # takes it. Read once, so the route and the refusal sentence can never disagree.
    capability = fleet.required_capability(modality, params)
    definition, model = catalog.resolve(
        modality, compound_id, only_location=_pinned_location(params),
        job_key=uuid.uuid4().hex, require_capability=capability,
    )
    if definition is None or model is None:
        # **An offered model with no live site is not a missing model.** The fleet
        # id resolved to nothing because every location that offers it is offline
        # or cannot take customer work — a temporary, site-shaped condition, and
        # 503 says so. The 404 below is kept for the ids that really are not in the
        # catalogue, so the two can never read the same.
        why = catalog.why_no_route(
            modality, compound_id, only_location=_pinned_location(params),
            require_capability=capability,
        )
        if why is not None:
            raise GatewayError(503, why, "SITE_CAPACITY_UNAVAILABLE")
        raise GatewayError(
            404,
            f'Model "{compound_id}" is not available on this gateway.',
            "MODEL_NOT_FOUND",
        )
    if modality == "tts":
        _refuse_an_unspoken_language(compound_id, model, params)
        _refuse_a_delivery_instruction(compound_id, model, params)
    if capability is not None:
        _refuse_references_this_lane_cannot_take(definition, model, params)
    # An idempotency key identifies one request, and a request has a modality.
    # The replay lookup matched on the key alone, so posting to
    # `/v1/images/generations` with a key already used on `/v1/chat/responses`
    # returned the *chat* job's envelope with HTTP 200 — the image was never
    # made, and the caller was told to poll a chat job. The read routes already
    # treat a cross-modality key as a mismatch (404); the write path has to
    # refuse it rather than answer with another modality's work.
    if idempotency_key:
        existing = store.find_by_idempotency_key(idempotency_key)
        if existing is not None and existing["modality"] != modality:
            raise GatewayError(
                409,
                f"That Idempotency-Key was already used for a {existing['modality']} request. "
                f"A key identifies one request; use a new one for this {modality} request.",
                "IDEMPOTENCY_KEY_REUSED",
            )
    return store.submit(
        modality=modality,
        model_id=model["model_id"],
        provider_id=definition.id,
        params=params,
        idempotency_key=idempotency_key,
        caller=caller,
        requested_by=requester,
        job_group=job_group,
        priority=priority,
    )


def _refuse_an_unspoken_language(compound_id: str, model: dict[str, Any], params: Any) -> None:
    """A voice job in a language its model does not publish is refused before it is queued.

    Owner, 2026-10-24: Khmer scripts sent to Breeze (English/Chinese only) babbled and failed
    their quality check four times each — fleet time spent on audio that could never pass.
    The model's own `languages` decide; a model that lists none is never refused here.
    """
    from . import language_guard
    from .catalog import _language_codes

    params = params if isinstance(params, dict) else {}
    text = str(params.get("text") or params.get("input") or "")
    raw = model.get("raw") or {}
    published = _language_codes(raw.get("languages"))
    if not published:
        return
    needed = language_guard.needed_codes(text, params.get("language"))
    speakers = [
        str(row.get("display_name") or row.get("id"))
        for row in catalog.rows("tts")
        if needed and needed & {language_guard.base_code(c) for c in row.get("supported_languages") or []}
    ][:6]
    reason = language_guard.refusal(text, params.get("language"), published,
                                    str(model.get("display_name") or compound_id), speakers)
    if reason:
        raise GatewayError(422, reason, "LANGUAGE_UNSUPPORTED")


def _refuse_a_delivery_instruction(compound_id: str, model: dict[str, Any], params: Any) -> None:
    """A delivery/emotion instruction is refused by a model that does not publish it.

    **Silently dropping it is the defect this prevents**: the caller would hear the plain
    reading and believe the instruction had been applied. Support is the model's own Hub
    record (`capabilities`), read live — never a model name, never a list held here.
    """
    from .catalog import DELIVERY_INSTRUCTION_LIMIT, takes_delivery_instruction

    params = params if isinstance(params, dict) else {}
    value = params.get("delivery_instruction")
    if value is None or value == "":
        return
    name = str(model.get("display_name") or compound_id)
    if not isinstance(value, str):
        raise GatewayError(422, "delivery_instruction must be text.", "DELIVERY_INSTRUCTION_INVALID")
    text = value.strip()
    if not text:
        return
    if len(text) > DELIVERY_INSTRUCTION_LIMIT:
        raise GatewayError(
            422,
            f"delivery_instruction is {len(text)} characters; this Gateway sends at most "
            f"{DELIVERY_INSTRUCTION_LIMIT} to the engine.",
            "DELIVERY_INSTRUCTION_TOO_LONG",
        )
    if takes_delivery_instruction(model.get("raw")):
        return
    raise GatewayError(
        422,
        f"{name} does not take a delivery/emotion instruction. Leave it out, or choose a model "
        f"whose catalogue row publishes the 'delivery_instruction' control.",
        "DELIVERY_INSTRUCTION_UNSUPPORTED",
    )


def _pop_priority(params: Any) -> str:
    """Take `priority` out of the request (never forwarded upstream) and check it.

    `normal` (the default; absent or null is normal, so an old client is unchanged) or `background`:
    bulk work that runs only on idle Macs no normal job is waiting for, and never pre-empts.
    Anything else is refused before anything is queued: a typo must not silently become normal work.
    """
    value = params.pop("priority", None) if isinstance(params, dict) else None
    if value is None:
        return NORMAL
    if isinstance(value, str) and value.strip().lower() in PRIORITIES:
        return value.strip().lower()
    raise GatewayError(
        400, f"priority must be one of {', '.join(PRIORITIES)}; got {value!r}. Omit it for normal work.",
        "INVALID_PARAMS")


# ---------------------------------------------------------------------------
# Private final-render transport. Its bearer has no loopback or ordinary-key path.
# ---------------------------------------------------------------------------
async def _bounded_render_json(request: Request) -> bytes:
    body = bytearray()
    async for piece in request.stream():
        if len(body) + len(piece) > 1024 * 1024:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 413)
        body.extend(piece)
    if not body:
        raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
    return bytes(body)


@app.get("/v1/final-renders/capabilities")
def final_render_capabilities(
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    return render_service.capabilities(principal)


@app.get("/v1/final-renders/blobs/{sha256}/status")
def final_render_blob_status(
    sha256: str, size_bytes: int = Query(..., ge=1),
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    return render_service.blob_status(principal, sha256, size_bytes)


@app.put("/v1/final-renders/blobs/{sha256}")
async def final_render_put_blob(
    sha256: str, request: Request,
    size_bytes: int = Query(..., ge=1), offset: int = Query(..., ge=0),
    content_length: int | None = Header(default=None, alias="Content-Length"),
    content_range: str | None = Header(default=None, alias="Content-Range"),
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    if content_length is None:
        raise RenderServiceError("BLOB_RANGE_INVALID", 411)
    if content_range != f"bytes {offset}-{offset + content_length - 1}/{size_bytes}":
        raise RenderServiceError("BLOB_RANGE_INVALID", 422)
    return await render_service.put_blob(
        principal, sha256, size_bytes, offset, content_length, request.stream()
    )


def _note_final_render_refusal(step: str, key: str, exc: BaseException) -> None:
    """One log line per refused or failed final-render call: Story files any of them as an unknown
    outcome, so without this line the cause is invisible (WI0005/7, 2026-10-25). No body, no key."""
    cause = exc.__cause__
    detail = f" cause={type(cause).__name__}: {str(cause)[:200]}" if cause is not None else ""
    print(f"[final-render] {step} refused: {type(exc).__name__} code={getattr(exc, 'code', None)} "
          f"status={getattr(exc, 'status', 500)} key={key[:8]}{detail}", flush=True)


@app.post("/v1/final-renders", status_code=202)
async def create_final_render(
    request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    if not idempotency_key:
        raise RenderServiceError("IDEMPOTENCY_KEY_REQUIRED", 422)
    try:
        return render_service.accept(principal, idempotency_key, await _bounded_render_json(request))
    except Exception as exc:
        _note_final_render_refusal("create", idempotency_key, exc)
        raise


@app.get("/v1/final-renders/by-idempotency-key/{key}")
def final_render_by_key(
    key: str, principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    try:
        row = render_service.by_key(principal, key)
    except Exception as exc:
        _note_final_render_refusal("lookup", key, exc)
        raise
    if row is None:
        raise RenderServiceError("RENDER_NOT_FOUND", 404)
    return row


@app.post("/v1/final-renders/by-idempotency-key/{key}/cancel")
async def cancel_final_render_by_key(
    key: str, request: Request,
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    try:
        body = decode_render_json(await _bounded_render_json(request))
    except ValueError:
        raise RenderServiceError("CANCEL_REQUEST_INVALID", 422) from None
    if (type(body) is not dict or set(body) != {"expected_request_sha256"}
            or type(body["expected_request_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", body["expected_request_sha256"]) is None):
        raise RenderServiceError("CANCEL_REQUEST_INVALID", 422)
    return render_service.request_cancel_by_key(
        principal, key, body["expected_request_sha256"]
    )


@app.get("/v1/final-renders/{gateway_job_id}")
def final_render_status(
    gateway_job_id: str,
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    return render_service.status(principal, gateway_job_id)


@app.post("/v1/final-renders/{gateway_job_id}/cancel")
def cancel_final_render(
    gateway_job_id: str,
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    return render_service.request_cancel(principal, gateway_job_id)


@app.get("/v1/final-renders/{gateway_job_id}/output")
def final_render_output(
    gateway_job_id: str, range_header: str | None = Header(default=None, alias="Range"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> StreamingResponse:
    lease = render_service.open_output(
        principal, gateway_job_id, range_header, if_match
    )

    def chunks():
        with lease:
            yield from lease.chunks()

    headers = {
        "Accept-Ranges": "bytes", "ETag": '"' + lease.sha256 + '"',
        "Content-Length": str(lease.length),
    }
    if lease.status == 206:
        headers["Content-Range"] = (
            f"bytes {lease.offset}-{lease.offset + lease.length - 1}/{lease.total}"
        )
    return StreamingResponse(chunks(), status_code=lease.status, media_type="video/mp4",
                             headers=headers)


@app.post("/v1/final-renders/{gateway_job_id}/ack")
async def acknowledge_final_render(
    gateway_job_id: str, request: Request,
    principal: AuthenticatedPrivateRender = Depends(require_private_render),
) -> dict[str, Any]:
    try:
        body = decode_render_json(await _bounded_render_json(request))
    except ValueError:
        raise RenderServiceError("OUTPUT_RECEIPT_INVALID", 422) from None
    if type(body) is not dict or set(body) != {"expected_receipt_digest"}:
        raise RenderServiceError("OUTPUT_RECEIPT_INVALID", 422)
    return render_service.acknowledge_output(
        principal, gateway_job_id, body["expected_receipt_digest"]
    )


#: A job group as Story sends it: `<project-slug>/<episode-id | project>` (INTERFACES-0144, D6).
_JOB_GROUP = re.compile(r"[A-Za-z0-9._:/-]{1,120}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
#: **`/health` never waits behind other requests** (0.2.157). Sync routes share anyio's one
#: 40-thread pool; when store-bound requests (Story's polls, a slow Jobs list) filled it, `/health`
#: queued for seconds although its own work takes ~1 ms, the app's 2.5 s probe timed out, and the
#: window said "The gateway is not running". Two threads of its own keep it answering.
_HEALTH_THREADS = anyio.CapacityLimiter(2)


@app.get("/health")
async def health_route() -> dict[str, Any]:
    return await anyio.to_thread.run_sync(health, limiter=_HEALTH_THREADS)


def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "gateway-kh",
        # The app adopts a gateway it did not start, so reporting the pid is what
        # lets it still stop that process on request rather than only pretending
        # to — and reporting the **build** is what lets it notice that the process
        # it adopted is running code from before the last rebuild. Without this the
        # only symptom is a feature quietly not working.
        "pid": os.getpid(),
        "build": build_id(),
        "providers": registry.provider_counts(),
        "models": registry.models.summary(),
        "jobs_processed": worker.processed,
        # **Whether dispatch is actually moving** (C1/M6). `/health` said `ok` while the
        # dispatcher thread was dead, because nothing published the loop's own state: every job
        # after that raise sat in `accepted` for ever and the only signal was an absence. A stale
        # `last_pass_at` is the number that says so.
        "worker": worker.liveness(),
        # **The capacity Story is offered, on the endpoint an operator checks first.**
        # `paid_concurrency_limit` lives on `/v1/account/overview`, which is Story's surface; this
        # is the same number on the health read, beside the pool it is *not*, so "the fleet can do
        # 24 while the pool says 60" is visible without knowing which endpoint to ask (D, H7).
        "concurrency": {
            **admission(),
            "in_flight": worker.active(),
        },
        # **The Studio Hub release this build targets.** Studio Hub moves on its
        # own schedule and auto-updates itself, so the pairing is declared rather
        # than assumed; the number is here so a rollout can assert it and the kit
        # can state it. It is not a gate — see `adapters.studio_hub`.
        "studio_hub": {"supported_version": SUPPORTED_STUDIO_HUB_VERSION},
    }


@app.get("/api/version")
def version() -> dict[str, Any]:
    return {"version": __version__, "service": "gateway-kh"}


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
@app.get("/v1/account/overview", dependencies=[Depends(require_auth)])
def account_overview() -> dict[str, Any]:
    # Story Studio's `hasVerifiedCapacity` needs integer `paid_active_jobs` and `paid_queued_jobs`;
    # without them it reads this whole record as unverified and never shows or paces to the queue.
    live = {
        "paid_active_jobs": worker.active(),
        "paid_queued_jobs": len(store.waiting_jobs(limit=int(admission()["max_queued_jobs"]) + 1)),
    }
    return {
        **admission(),
        **live,
        "credit_balance": CREDIT_BALANCE,
        "credits_used_30d": 0,
        "currency": "credits",
        "tier": "owner",
        "lifetime_paid_cents": 0,
        "service_tier": "owner",
        "admission": {**admission(), **live},
        "capacity_verified": True,
        "limit_source": "gateway",
    }


@app.get("/v1/account/jobs", dependencies=[Depends(require_auth)])
def account_jobs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)
) -> dict[str, Any]:
    # Bounded, because an unbounded `int` query parameter is not a small thing:
    # FastAPI parses arbitrary-precision Python ints, and `page_size` went
    # straight into a SQL `LIMIT ?`, so `?page_size=99999999999999999999999`
    # raised `OverflowError: Python int too large to convert to SQLite INTEGER`
    # — a 500, from a URL. `page_size=-1` was worse in a quieter way: that is
    # SQLite's "no limit", so a caller could ask for every job ever recorded.
    rows = store.recent(page_size, offset=(page - 1) * page_size)
    return {
        "items": [
            {
                "id": r["id"],
                "operation": r["modality"],
                "state": r["state"],
                "model_id": catalog_encode(r),
                **ZERO_SETTLEMENT,
            }
            for r in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": store.job_count(),
    }


@app.get("/v1/account/credit-activity", dependencies=[Depends(require_auth)])
def credit_activity(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)
) -> dict[str, Any]:
    # Bounded for the same reason as `/v1/account/jobs`: it does not reach SQL
    # *today*, so an absurd value cannot crash it — but leaving one paginated
    # route bounded and its sibling unbounded is how the next one gets written
    # without the guard and then wired to the store.
    return {"items": [], "page": page, "page_size": page_size, "total": 0}


# ---------------------------------------------------------------------------
# Story Studio releases (`story_releases`): the signed release, its file, and each Story Mac's
# check-in. Same bearer key as every other call Story makes, so no second credential exists.
# ---------------------------------------------------------------------------
def _story_offer(call: Any) -> Any:
    from . import release_publisher, story_releases
    try:
        return call()
    except story_releases.StoryReleaseError as exc:
        raise GatewayError(exc.status, exc.message, exc.code) from exc
    except release_publisher.ReleaseError as exc:
        raise GatewayError(exc.status, exc.message, exc.code) from exc


@app.get("/v1/apps/story/release", dependencies=[Depends(require_auth)])
def story_release() -> Any:
    from . import story_releases
    return _story_offer(story_releases.offer)


@app.get("/v1/apps/story/release/file", dependencies=[Depends(require_auth)])
def story_release_file(
    range_header: str | None = Header(default=None, alias="Range"),
    if_range: str | None = Header(default=None, alias="If-Range"),
) -> Any:
    """Serve the offered release file.

    HTTP range (RFC 9110 §14): a caller may send `Range: bytes=N-` to resume a partial
    transfer of the ~240 MB artifact. `bytes=N-M` (a closed range) is honoured too, with
    its inclusive end clamped to the last byte. The offer is immutable while published,
    but a NEW publish replaces it, so an `If-Range` whose value is not THIS descriptor's
    sha256 is answered with the full 200 body (the spec's behaviour for a changed
    validator), never a mixed 206. Deliberately supported is the single-range form only:
    a suffix range (`bytes=-N`), a reversed or unsatisfiable-inner range, multiple ranges,
    any non-`bytes` unit, and any malformed spec are all IGNORED with the full 200 — a
    consistent explicit fallback, never a partial guess. A start at or past the end of the
    file answers 416 with `Content-Range: bytes */<total>`. Every accepted bound is parsed
    strictly ASCII-digit and length-capped so a huge or Unicode-digit header can neither
    raise nor allocate; a caller without Range gets exactly the old full-body response.
    """
    from fastapi.responses import StreamingResponse
    from . import story_releases
    opened = story_releases.open_artifact()
    if opened is None:
        raise GatewayError(404, "No Story Studio release is published.", "STORY_RELEASE_NONE")
    handle, descriptor = opened
    total = descriptor["bytes"]

    def ascii_digits(text: str) -> bool:
        # `str.isdigit()` accepts Unicode digits (e.g. "٥", "５") that int() would parse
        # into surprising values, and a header can carry thousands of digits whose int()
        # conversion itself costs time; accept only plain ASCII and bound the length.
        return 0 < len(text) <= 20 and all("0" <= c <= "9" for c in text)

    start, end_inclusive = 0, total - 1
    ranged = False
    if range_header is not None:
        units, _, spec = range_header.partition("=")
        first, dash, last = spec.strip().partition("-")
        if (units.strip().lower() == "bytes" and dash
                and ascii_digits(first) and (last == "" or ascii_digits(last))):
            # Exactly one range, no list: a second comma means we ignore the header.
            if "," not in spec:
                first_n, last_n = int(first), int(last) if last else None
                if last_n is not None and last_n < first_n:
                    pass  # reversed bounds: ignored, full 200 below
                else:
                    # A stale If-Range means the partial belongs to a REPLACED artifact, so the
                    # contract answer is the full 200 — even past the new file's end. Only a
                    # range that survives its validator may 416.
                    stale = if_range is not None and if_range.strip() != descriptor["sha256"]
                    if not stale:
                        if first_n >= total:
                            handle.close()
                            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
                        start = first_n
                        end_inclusive = total - 1 if last_n is None else min(last_n, total - 1)
                        ranged = True

    length = end_inclusive - start + 1

    def chunks() -> Any:
        if start:
            handle.seek(start)
        remaining = length
        with handle:
            while remaining > 0 and (block := handle.read(min(1 << 20, remaining))):
                remaining -= len(block)
                yield block

    headers = {"Accept-Ranges": "bytes",
               "Content-Disposition": f'attachment; filename="{descriptor["filename"]}"',
               "Content-Length": str(length)}
    if ranged:
        headers["Content-Range"] = f"bytes {start}-{end_inclusive}/{total}"
        return StreamingResponse(chunks(), status_code=206, media_type="application/octet-stream",
                                 headers=headers)
    return StreamingResponse(chunks(), media_type="application/octet-stream", headers=headers)


@app.post("/v1/apps/story/checkin", dependencies=[Depends(require_auth)])
def story_checkin(payload: dict[str, Any]) -> Any:
    from . import story_releases
    return _story_offer(lambda: story_releases.checkin(payload))


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
#: **The catalogue has threads of its own (0.2.162),** like `/health`. Live on 2026-10-09 every one of
#: anyio's 40 shared threads was walking the artifact store, and `/v1/models` timed out at 180 s
#: behind them. Its auth check is async for the same reason (`require_auth_async`).
_CATALOG_THREADS = anyio.CapacityLimiter(4)


@app.get("/v1/models", dependencies=[Depends(require_auth_async)])
async def models_registry_route() -> dict[str, Any]:
    return await anyio.to_thread.run_sync(models_registry, limiter=_CATALOG_THREADS)


def models_registry() -> dict[str, Any]:
    """Every model row, across every modality, in one answer.

    **This said "consumed by nothing in Story Studio's frontend; served for completeness", and
    that was wrong** (C7, the audit's LOW list): Story does consume it, and the sentence was the
    kind of note that stops the next reader from checking whether a change here reaches the app.
    `freshness` beside it is the other half — `stale: true` disables every model on Story's side,
    so the two fields travel together and neither is decorative.
    """
    return {"models": catalog.all_rows(), "freshness": catalog.freshness()}


def _catalog_handler(modality: str):
    async def handler() -> dict[str, Any]:
        return await anyio.to_thread.run_sync(catalog.envelope, modality, limiter=_CATALOG_THREADS)

    return handler


for _modality, _path in (
    ("chat", "/v1/chat/models"),
    ("image", "/v1/image/models"),
    ("video", "/v1/videos/models"),
    ("tts", "/v1/tts/models"),
    ("music", "/v1/music/models"),
    ("transcription", "/v1/transcription/models"),
    ("embeddings", "/v1/embeddings/models"),
    ("vision", "/v1/vision/models"),
):
    app.add_api_route(
        _path,
        _catalog_handler(_modality),
        methods=["GET"],
        dependencies=[Depends(require_auth_async)],
        name=f"catalog_{_modality}",
    )


@app.get("/v1/fleet/capacity", dependencies=[Depends(require_auth)])
def fleet_capacity(response: Response, modality: str | None = None,
                   model: str | None = None) -> dict[str, Any]:
    """How much of each fleet model the Macs can run right now, to size a submit window (0.2.165).

    Per model: `eligible_macs` (reachable Macs that can run it), `idle_macs` (the Hubs' own count of
    those free now; background work runs only there), and this Gateway's `running`/`queued` jobs of
    it, split `normal`/`background`. `modality` (music, image, tts, ...) and `model` (the catalogue
    id or the bare model id) narrow it. Read from the fleet snapshot the dispatcher already holds
    and one grouped count, so it is cheap; cache it for a few seconds.
    """
    from . import concurrency, fleet

    if modality is not None and modality not in fleet.HUB_OPERATION_BY_MODALITY:
        raise GatewayError(400, f"modality must be one of {', '.join(fleet.HUB_OPERATION_BY_MODALITY)}; "
                                f"got {modality!r}.", "INVALID_PARAMS")
    wanted = model.split("::", 1)[-1] if model else None
    counts: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
    for row in store.priority_counts():
        entry = counts.setdefault((row["modality"], row["model_id"]), {
            "running": {NORMAL: 0, BACKGROUND: 0}, "queued": {NORMAL: 0, BACKGROUND: 0}})
        entry["running"][row["priority"]] += row["running"]
        entry["queued"][row["priority"]] += row["waiting"]
    models: dict[tuple[str, str], dict[str, Any]] = {}
    for lane in [modality] if modality else list(fleet.HUB_OPERATION_BY_MODALITY):
        for definition, item in registry.models.enabled_models(lane):
            model_id = str(item.get("model_id") or "")
            if (not definition.id.startswith(fleet.PROVIDER_PREFIX) or (lane, model_id) in models
                    or (wanted is not None and model_id != wanted)):
                continue
            candidates = fleet.routing_candidates(model_id, lane)
            count = counts.get((lane, model_id), {"running": {NORMAL: 0, BACKGROUND: 0},
                                                  "queued": {NORMAL: 0, BACKGROUND: 0}})
            idle = sum(int(c.get("available") or 0) for c in candidates)
            # A fleet-wide cap (YuE2) is what this model can really start now, so a caller sizing its
            # window from `idle_macs` keeps only a short line waiting instead of one per idle Mac.
            cap = concurrency.model_fleet_cap(model_id)
            if cap is not None:
                idle = max(0, min(idle, cap - sum(count["running"].values())))
            models[(lane, model_id)] = {
                "id": encode_model_id(fleet.publish_id(definition.id), model_id),
                "model_id": model_id,
                "modality": lane,
                "locations": len(candidates),
                "eligible_macs": sum(0 if c.get("spent") else int(c["slots"]) for c in candidates),
                "idle_macs": idle,
                **({"fleet_cap": cap} if cap is not None else {}),
                **count,
            }
    response.headers["Cache-Control"] = "private, max-age=5"
    return {
        "generated_at": time.time(),
        "fleet_generated_at": fleet.snapshot(allow_stale=True).get("generated_at"),
        "max_queued_jobs": int(admission()["max_queued_jobs"]),
        "models": list(models.values()),
    }


@app.get("/v1/voices", dependencies=[Depends(require_auth)])
def voices(
    model_id: str | None = None,
    collection: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Voices usable with a TTS model.

    Only *enabled* voices are returned — discovery and availability are separate
    here exactly as they are for models, because a provider's public library can
    hold tens of thousands of voices.

    Which voices match a model depends on the provider, and the two rules are
    genuinely different: a local cloning voice belongs to one specific model,
    while a provider's voices work with every TTS model that provider offers.
    `voices_for_model` encodes both.
    """
    rows = (
        voices_for_model(model_id)
        if model_id
        else [story_row(v) for v in voice_registry().enabled_voices()]
    )
    if search:
        term = search.lower()
        rows = [
            r
            for r in rows
            if term in r["display_name"].lower() or term in (r["description"] or "").lower()
        ]
    return {"voices": rows, "models": rows}


@app.post("/v1/voices/{voice_id}/save", dependencies=[Depends(require_auth)])
def save_voice(voice_id: str) -> dict[str, Any]:
    return {"id": voice_id, "is_saved": True}


@app.delete("/v1/voices/{voice_id}/save", dependencies=[Depends(require_auth)])
def unsave_voice(voice_id: str) -> dict[str, Any]:
    return {"id": voice_id, "is_saved": False}


@app.get("/v1/voices/{voice_id:path}/preview", dependencies=[Depends(require_auth)])
def voice_preview(voice_id: str) -> Response:
    """A voice's stored sample, proxied so the caller never learns a provider URL or credential.

    Published as `preview_url` on the voice row, and only for voices that have one.
    """
    try:
        content, media_type = voice_registry().preview(voice_id)
    except KeyError as exc:
        raise GatewayError(404, f"No preview is available for this voice: {exc}", "PREVIEW_NOT_FOUND")
    if not content:
        raise GatewayError(404, "The provider returned no preview audio.", "PREVIEW_NOT_FOUND")
    return Response(content=content, media_type=media_type)


# The provider libraries Story Studio's picker can browse, under the names it hard-codes.
_VOICE_LIBRARIES = {"elevenlabs": "elevenlabs-direct", "fish": "fish-audio"}
#: What the last page of each library listed, by `(library, public_voice_id)`: the add and preview
#: calls carry only the id, and ElevenLabs needs the voice's `owner_id` (and its sample URL) that
#: only the listing knows. ponytail: process memory, so a restart between browse and add answers
#: 409 "browse again"; persist it if that ever bites.
_LIBRARY_SEEN: dict[tuple[str, str], Any] = {}


def _library_provider(library: str) -> str:
    provider_id = _VOICE_LIBRARIES.get(library)
    if provider_id is None:
        raise GatewayError(404, f'There is no voice library named "{library}".', "LIBRARY_NOT_FOUND")
    return provider_id


def _admin_result(answer: Any) -> Any:
    """The admin voice-library code answers an error as a response object; raise it as ours."""
    if isinstance(answer, JSONResponse):
        try:
            detail = json.loads(answer.body).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise GatewayError(answer.status_code, str(detail or "the voice library could not answer"),
                           "VOICE_LIBRARY_ERROR")
    return answer


@app.get("/v1/voice-library/{library}", dependencies=[Depends(require_auth)])
def voice_library_page(library: str, search: str = "", page: int = Query(0, ge=0)) -> dict[str, Any]:
    """Browse a provider's public voice library, in the shape Story Studio's picker reads.

    Reads the same live search the admin library uses (`voices.search_library`).
    """
    from .voices import search_library

    provider_id = _library_provider(library)
    definition = registry.definition(provider_id)
    if definition is None or not registry.credentials.is_configured(provider_id):
        raise GatewayError(409, f"No API key is configured for {library}.", "LIBRARY_NOT_CONFIGURED")
    found = search_library(definition, registry.credentials.resolved(provider_id), search, page)
    if library == "fish" and search.strip():  # Fish answers its whole library; filter here
        needle = search.strip().lower()
        found = [v for v in found if needle in v.display_name.lower()
                 or needle in v.description.lower()]
    known = {v.provider_voice_id: v.id for v in voice_registry().voices(provider_id=provider_id)}
    voices = []
    for voice in found:
        _LIBRARY_SEEN[(library, voice.provider_voice_id)] = voice
        voices.append({
            "public_voice_id": voice.provider_voice_id,
            "name": voice.display_name,
            "description": voice.description,
            "language": voice.language,
            "accent": voice.accent or None,
            "gender": voice.gender or None,
            "age": voice.age or None,
            "preview_available": bool(voice.preview_url),
            "preview_url": (f"/v1/voice-library/{library}/{voice.provider_voice_id}/preview"
                            if voice.preview_url else None),
            "genstudio_voice_id": known.get(voice.provider_voice_id),
            "is_saved": voice.provider_voice_id in known,
            "credits_per_1000_characters": 0,
        })
    # ElevenLabs pages are 30 voices; Fish answers its whole library at once.
    return {"page": page, "has_more": library == "elevenlabs" and len(voices) >= 30,
            "voices": voices}


@app.post("/v1/voice-library/{library}/add", dependencies=[Depends(require_auth)])
def voice_library_add(library: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Bring a browsed library voice into this gateway's voices (`admin.import_voice`)."""
    from . import admin

    provider_id = _library_provider(library)
    voice_id = str(payload.get("public_voice_id") or "").strip()
    if not voice_id:
        raise GatewayError(422, "public_voice_id is required.", "INVALID_REQUEST")
    seen = _LIBRARY_SEEN.get((library, voice_id))
    if seen is None:
        raise GatewayError(409, "Browse the library again, then add the voice.", "LIBRARY_STALE")
    answer = _admin_result(admin.import_voice({
        "provider_id": provider_id, "provider_voice_id": voice_id,
        "display_name": seen.display_name, "owner_id": (seen.raw or {}).get("owner_id") or "",
    }))
    voice = answer["voice"]
    return {"voice_id": voice["id"], "created": True, "saved": True,
            "credits_per_1000_characters": 0}


@app.get("/v1/voice-library/{library}/{voice_id}/preview", dependencies=[Depends(require_auth)])
def voice_library_preview(library: str, voice_id: str) -> Response:
    _library_provider(library)
    seen = _LIBRARY_SEEN.get((library, voice_id))
    url = seen.preview_url if seen is not None else ""
    if not url:
        raise GatewayError(404, "No preview is available for this voice.", "PREVIEW_NOT_FOUND")
    try:
        answer = httpx.get(url, timeout=20.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise GatewayError(502, f"The provider's sample could not be fetched: {exc}", "PREVIEW_UNAVAILABLE")
    if answer.status_code >= 400 or not answer.content:
        raise GatewayError(502, "The provider returned no preview audio.", "PREVIEW_UNAVAILABLE")
    return Response(content=answer.content,
                    media_type=answer.headers.get("content-type", "audio/mpeg").split(";")[0])


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/v1/chat/responses", dependencies=[Depends(require_auth)])
def create_chat_response(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("chat", model_id, payload, key, caller=caller)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/chat/responses/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _replay_marked(_job_envelope(job), created)


@app.get("/v1/chat/responses/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def chat_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "chat":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/chat/responses/{job_id}", dependencies=[Depends(require_auth)])
def get_chat_response(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "chat"))


@app.post("/v1/chat/responses/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_chat_response(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "chat")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


@app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
def chat_completions(payload: dict[str, Any]) -> Any:
    """Synchronous rollback route.

    Story Studio only selects this when
    `STORY_STUDIO_GENSTUDIO_CHAT_TRANSPORT=completions`. It blocks for the whole
    generation, which is exactly why the durable route is the default -- but it
    is implemented so switching transports does not dead-end.
    """
    payload.pop("job_group", None)  # no job here to order, and a group is never forwarded upstream
    _pop_priority(payload)  # likewise: checked, never forwarded
    model_id = str(payload.get("model") or payload.get("model_id") or "")
    definition, model = catalog.resolve("chat", model_id)
    if definition is None or model is None:
        raise GatewayError(404, f'Model "{model_id}" is not available.', "MODEL_NOT_FOUND")

    request = BackendRequest(
        provider_id=definition.id,
        model_id=model["model_id"],
        modality="chat",
        params=payload,
        values=registry.credentials.resolved(definition.id),
        base_url=definition.base_url,
        display_name=definition.display_name,
        # The published allowance, never the adapter's private default: a caller that sends no
        # `max_tokens` on this route is told the model's number by the catalogue above.
        max_output_tokens=published_output_cap(definition, model["model_id"], model),
    )
    try:
        result = adapter_for(definition).generate(request)
    except BackendError as exc:
        if exc.code == "INCOMPLETE_OUTPUT":
            evidence = dict(exc.evidence or {})
            body: dict[str, Any] = {
                "detail": str(exc),
                "code": exc.code,
                "failure_code": exc.code,
                "failure_class": "incomplete_output",
            }
            for key in ("partial_response", "pool_attempts"):
                if evidence.get(key) is not None:
                    body[key] = evidence[key]
            return JSONResponse(status_code=502, content=_scrub_failure_evidence(body))
        # Never 402: a drained provider account must not lock Story Studio's key.
        raise GatewayError(502, str(exc), exc.code) from exc
    except Exception as exc:  # noqa: BLE001
        # Catch *everything*, not just the errors we predicted.
        #
        # This is the only synchronous route — the durable one runs in a worker
        # that already settles any exception as a failed job — so an adapter bug
        # is the one place where "the provider sent something odd" leaves the
        # gateway as a 500, and a 500 from the rollback transport is a wall Story
        # Studio cannot get past. A provider answering 200 with a non-JSON body,
        # or a JSON list instead of an object, is enough: `response.json()` and
        # the `.get` right after it are both unguarded upstream.
        raise GatewayError(
            502, f"Provider '{definition.display_name}' could not be read: {exc}", "BACKEND_ERROR"
        ) from exc

    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model["model_id"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text or ""},
                # Same deliberate exception as `_job_envelope`: an absent reason
                # becomes a clean stop because the field is a hard gate and we have
                # text in hand. A stated reason is never rewritten.
                "finish_reason": (result.payload or {}).get("finish_reason") or "stop",
            }
        ],
        "usage": (result.payload or {}).get("usage"),
    }


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
#: OpenAI's own per-request ceiling; the Hub's lane states the same number.
EMBEDDINGS_MAX_INPUTS = 2048
#: How many locations one request may try before it gives up.
EMBEDDINGS_MAX_LOCATIONS = 3
_EMBEDDINGS_FIELDS = frozenset({"model", "input", "encoding_format", "user"})


@app.post("/v1/embeddings", dependencies=[Depends(require_auth)])
def embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-compatible embeddings on the fleet's Embeddings Studio: free, local, synchronous.

    `{model, input: [...]}` -> `{object: "list", data: [{embedding, index}], model, usage}`. Only
    fleet models (`studio-hub::<id>`; a bare id means the same) serve here, so this can never spend.
    Interactive work: no job, no queue. The fleet's own routing picks the location; a location that
    refuses or cannot be reached is skipped for the next one, up to `EMBEDDINGS_MAX_LOCATIONS`.
    """
    unsupported = sorted(set(payload) - _EMBEDDINGS_FIELDS)
    if unsupported:
        raise GatewayError(422, f"{', '.join(unsupported)}: not supported; this route accepts "
                                f"{', '.join(sorted(_EMBEDDINGS_FIELDS))}.", "INVALID_REQUEST")
    if payload.get("encoding_format", "float") != "float":
        raise GatewayError(422, "encoding_format: only float is served.", "INVALID_REQUEST")
    texts = payload.get("input")
    texts = [texts] if isinstance(texts, str) else texts
    if (not isinstance(texts, list) or not texts
            or not all(isinstance(text, str) and text.strip() for text in texts)):
        raise GatewayError(422, "input must be a non-empty string or a non-empty list of "
                                "non-empty strings.", "INVALID_REQUEST")
    if len(texts) > EMBEDDINGS_MAX_INPUTS:
        raise GatewayError(422, f"input has {len(texts)} items; at most {EMBEDDINGS_MAX_INPUTS} "
                                "per request.", "INVALID_REQUEST")
    from .catalog import decode_model_id

    requested = str(payload.get("model") or "")
    provider_id, model_id = decode_model_id(requested)
    if provider_id not in ("", fleet.FLEET_PROVIDER_ID) or not model_id:
        raise GatewayError(404, f'Model "{requested}" is not an embeddings model on this gateway; '
                                f"use a {fleet.FLEET_PROVIDER_ID}::<id> row from "
                                "/v1/embeddings/models.", "MODEL_NOT_FOUND")
    offered = {definition.id: (definition, model)
               for definition, model in registry.models.enabled_models("embeddings")
               if model["model_id"] == model_id}
    tried: set[str] = set()
    refusals: list[str] = []
    while len(tried) < EMBEDDINGS_MAX_LOCATIONS:
        chosen, note = fleet.route(model_id, "embeddings", routing_key=uuid.uuid4().hex,
                                   exclude_locations=tried)
        if chosen is None or chosen.id not in offered:
            if not refusals:
                refusals.append(note)
            break
        definition, model = offered[chosen.id]
        tried.add(fleet.controller_id_from(definition.id) or definition.id)
        request = BackendRequest(
            provider_id=definition.id, model_id=model_id, modality="embeddings",
            params={"input": texts}, values=registry.credentials.resolved(definition.id),
            base_url=definition.base_url, display_name=definition.display_name)
        try:
            result = adapter_for(definition).generate(request)
        except BackendError as exc:
            if not exc.retryable:
                raise GatewayError(exc.status or 502, str(exc), exc.code) from exc
            refusals.append(f"{definition.display_name}: {exc}")
            continue
        usage = result.payload.get("usage") or {}
        return {"object": "list", "model": encode_model_id(fleet.FLEET_PROVIDER_ID, model_id),
                "data": [{"object": "embedding", "index": index, "embedding": vector}
                         for index, vector in enumerate(result.payload["vectors"])],
                "usage": {"prompt_tokens": usage.get("prompt_tokens"),
                          "total_tokens": usage.get("total_tokens")}}
    if not offered and not tried:
        raise GatewayError(503, f'No Mac offers the embeddings model "{model_id}" right now: install '
                                "it on a Studio Hub (0.6.165 or newer) and switch it on.",
                           "NO_EMBEDDINGS_ENGINE")
    raise GatewayError(503, "No Mac could run this embeddings batch right now. "
                            + " · ".join(refusals), "NO_EMBEDDINGS_ENGINE",
                       headers={"Retry-After": "30"})


# ---------------------------------------------------------------------------
# Vision (image analysis)
# ---------------------------------------------------------------------------
VISION_TASKS = ("depth", "subject", "points", "segments")
#: How many Macs one request may try before it gives up.
VISION_MAX_MACS = 3
_VISION_FIELDS = frozenset({"tasks", "image", "model", "text", "regions", "source_job_id", "site", "priority"})
_VISION_MIMES = ("image/jpeg", "image/png", "image/webp")
_VISION_WHY = ("source-mac", "caller-site", "any")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _vision_invalid(detail: str) -> GatewayError:
    return GatewayError(422, detail, "INVALID_REQUEST")


def _no_vision_engine(detail: str) -> GatewayError:
    return GatewayError(503, detail, "NO_VISION_ENGINE", headers={"Retry-After": "30"})


def _vision_image(image: Any) -> dict[str, Any]:
    """The image as the Hub takes it: `{b64, mime?}` (at most 12 MB decoded) or `{url}` (http/https)."""
    from urllib.parse import urlsplit
    import base64
    import binascii

    if isinstance(image, dict) and set(image) == {"url"}:
        url = image["url"]
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
            raise _vision_invalid("image.url must be an http or https address.")
        return {"url": url}
    if not isinstance(image, dict) or "b64" not in image or set(image) - {"b64", "mime"}:
        raise _vision_invalid('image must be {"b64": "...", "mime": "image/jpeg"} or {"url": "https://..."}.')
    if image.get("mime", "image/jpeg") not in _VISION_MIMES:
        raise _vision_invalid(f"image.mime must be one of {', '.join(_VISION_MIMES)}.")
    try:
        size = len(base64.b64decode(image["b64"], validate=True))
    except (binascii.Error, TypeError, ValueError):
        size = 0
    if not size:
        raise _vision_invalid("image.b64 is not a base64-encoded image.")
    if size > VISION_MAX_IMAGE_BYTES:
        raise _vision_invalid(f"The image is {size / 1048576:.1f} MB; at most 12 MB is accepted. "
                              "Send a downscaled ~768 px JPEG.")
    return dict(image)


def _vision_models(tasks: list[str], named: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """The bare model ids to send, and per model the locations (id -> definition) that switched it on."""
    from .catalog import decode_model_id, vision_tasks

    rows: dict[str, dict[str, Any]] = {}
    for definition, model in registry.models.enabled_models("vision"):
        entry = rows.setdefault(model["model_id"], {"tasks": None, "default": False, "sites": {}})
        entry["tasks"] = entry["tasks"] or vision_tasks(model)
        entry["default"] = entry["default"] or (model.get("raw") or {}).get("default") is True
        entry["sites"][fleet.controller_id_from(definition.id) or definition.id] = definition
    menu = "; ".join(f"{encode_model_id(fleet.FLEET_PROVIDER_ID, model_id)} does "
                     + ", ".join(rows[model_id]["tasks"] or ["(not stated)"]) for model_id in sorted(rows))
    install = ("install one on a Studio Hub (0.6.166 or newer) and switch it on in the Gateway.")
    if named is None:
        chosen = []
        for task in tasks:
            able = sorted((model_id for model_id, entry in rows.items() if task in (entry["tasks"] or ())),
                          key=lambda model_id: (not rows[model_id]["default"], model_id))
            if not able:
                raise _no_vision_engine(f'No Mac offers a vision model that does "{task}" right now: {install}')
            chosen.append(able[0])
    else:
        names = [named] if isinstance(named, str) else named
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise _vision_invalid("model must be a model id or a list of model ids.")
        chosen = []
        for name in names:
            provider_id, model_id = decode_model_id(name)
            if provider_id not in ("", fleet.FLEET_PROVIDER_ID) or not model_id:
                raise GatewayError(404, f'Model "{name}" is not a vision model on this gateway; use a '
                                        f"{fleet.FLEET_PROVIDER_ID}::<id> row from /v1/vision/models.",
                                   "MODEL_NOT_FOUND")
            chosen.append(model_id)
        if not rows:
            raise _no_vision_engine(f"No Mac offers a vision model right now: {install}")
        unknown = [model_id for model_id in chosen if model_id not in rows]
        if unknown:
            raise _vision_invalid(f"{', '.join(unknown)}: not a vision model switched on here. Available: {menu}.")
        for task in tasks:
            if not any(rows[model_id]["tasks"] is None or task in rows[model_id]["tasks"] for model_id in chosen):
                raise _vision_invalid(f'None of the named models does "{task}". Available: {menu}.')
    needed = list(dict.fromkeys(chosen))
    return needed, {model_id: rows[model_id]["sites"] for model_id in needed}


def _vision_macs(needed: list[str], sites: dict[str, dict[str, Any]],
                 source_job_id: Any, site: Any, client_host: str) -> list[tuple[str, str, str]]:
    """`(location_id, machine, why)` for every Mac with every needed model ready, nearest first.

    Owner rule (keep Tailscale traffic low): the Mac that generated the image (`source-mac`), then the
    caller's own site (`caller-site`: `site`, else the location whose controller or joined Mac has the
    caller's address), then any other Mac (`any`). Ready = what fleet routing calls eligible (installed
    and ready, fits in memory, not paused, not a resting Lite Mode Mac, reachable) and files `cached`.
    """
    import random

    source_location = source_machine = None
    job = store.get(source_job_id) if isinstance(source_job_id, str) and source_job_id else None
    if job:
        source_location = fleet.controller_id_from(str(job.get("provider_id") or ""))
        try:
            result = json.loads(job.get("result_json") or "{}")
        except ValueError:
            result = {}
        source_machine = ((result or {}).get("payload") or {}).get("hub_machine")

    def host(url: Any) -> str:
        from urllib.parse import urlsplit

        name = urlsplit(str(url or "")).hostname or ""
        return "127.0.0.1" if name in _LOOPBACK_HOSTS else name

    caller = "127.0.0.1" if client_host in _LOOPBACK_HOSTS else client_host
    locations = fleet.snapshot(allow_stale=True).get("locations") or []
    if not (isinstance(site, str) and site):
        site = next((str(row.get("id")) for row in locations if caller and caller in
                     {host(row.get("url"))} | {host(node.get("address")) for node in row.get("nodes") or []
                                               if isinstance(node, dict)}), None)
    macs: list[tuple[str, str, str]] = []
    for row in locations:
        location = str(row.get("id") or "")
        if (not all(location in sites[model_id] for model_id in needed) or not row.get("reachable")
                or not fleet._takes_customer_jobs(row)):
            continue
        ready = None
        for model_id in needed:
            here = {machine for machine, model in fleet._eligible_macs(row, model_id, "vision").items()
                    if model.get("cache_state") == "cached"}
            ready = here if ready is None else ready & here
        for machine in sorted(ready or ()):
            why = ("source-mac" if (location, machine) == (source_location, source_machine)
                   else "caller-site" if location == site else "any")
            macs.append((location, machine, why))
    # ponytail: random spread within a tier; rank by each Mac's load if one ever queues up.
    return sorted(macs, key=lambda mac: (_VISION_WHY.index(mac[2]), random.random()))


@app.post("/v1/vision/analyze", dependencies=[Depends(require_auth)])
def vision_analyze(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Image analysis (depth, subject, points, segments) on the fleet's vision engine: free, local, synchronous.

    Only fleet models serve here, so this can never spend. The Mac is picked nearest the image first
    (`_vision_macs`); one that refuses or cannot be reached is skipped for the next, up to
    `VISION_MAX_MACS`. The answer is the Hub's own, plus `served_by`, `attempts` and `network`.
    """
    unsupported = sorted(set(payload) - _VISION_FIELDS)
    if unsupported:
        raise _vision_invalid(f"{', '.join(unsupported)}: not supported; this route accepts "
                              f"{', '.join(sorted(_VISION_FIELDS))}.")
    tasks = payload.get("tasks")
    if (not isinstance(tasks, list) or not tasks or len(set(map(str, tasks))) != len(tasks)
            or not all(task in VISION_TASKS for task in tasks)):
        raise _vision_invalid(f"tasks must be a non-empty list of different tasks from {', '.join(VISION_TASKS)}.")
    body: dict[str, Any] = {"tasks": tasks, "image": _vision_image(payload.get("image"))}
    text = payload.get("text")
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise _vision_invalid("text must be a non-empty phrase of at most 500 characters.")
        body["text"] = text
    regions = payload.get("regions")
    if regions is not None:
        if (not isinstance(regions, list) or not 1 <= len(regions) <= 8
                or not all(isinstance(r, str) and r.strip() and len(r) <= 40 for r in regions)):
            raise _vision_invalid("regions must be a list of 1 to 8 names (each at most 40 characters).")
        body["regions"] = regions
    if payload.get("priority", "normal") not in PRIORITIES:
        raise _vision_invalid('priority must be "normal" or "background".')
    for field in ("source_job_id", "site"):
        if payload.get(field) is not None and not isinstance(payload.get(field), str):
            raise _vision_invalid(f"{field} must be a string.")
    needed, sites = _vision_models(tasks, payload.get("model"))
    body["model"] = needed
    client_host = request.client.host if getattr(request, "client", None) else ""
    macs = _vision_macs(needed, sites, payload.get("source_job_id"), payload.get("site"), client_host)
    attempts: list[str] = []
    for location, machine, why in macs[:VISION_MAX_MACS]:
        definition = sites[needed[0]][location]
        call = BackendRequest(
            provider_id=definition.id, model_id=needed[0], modality="vision",
            params={"machine": machine, "body": body}, values=registry.credentials.resolved(definition.id),
            base_url=definition.base_url, display_name=definition.display_name)
        try:
            result = adapter_for(definition).generate(call)
        except BackendError as exc:
            if not exc.retryable:
                raise GatewayError(exc.status or 502, str(exc), exc.code) from exc
            attempts.append(f"{definition.display_name} ({machine}) {exc}")
            continue
        answer = dict(result.payload["answer"])
        fleet_id = lambda model_id: encode_model_id(fleet.FLEET_PROVIDER_ID, str(model_id))  # noqa: E731
        if isinstance(answer.get("models"), dict):
            answer["models"] = {task: fleet_id(model_id) for task, model_id in answer["models"].items()}
        if isinstance(answer.get("revisions"), dict):
            answer["revisions"] = {fleet_id(model_id): sha for model_id, sha in answer["revisions"].items()}
        return answer | {
            "served_by": {"location_id": location, "machine": machine, "why": why},
            "attempts": attempts,
            "network": {"request_bytes": result.payload["request_bytes"],
                        "response_bytes": result.payload["response_bytes"]},
        }
    if not attempts:
        raise _no_vision_engine(f"No Mac has {', '.join(needed)} installed and ready right now (awake, not "
                                "paused, enough free memory). Install it on a Studio Hub (0.6.166 or newer).")
    raise _no_vision_engine("No Mac could analyse this image right now. " + " · ".join(attempts))


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
@app.post("/v1/images/admission/permits", dependencies=[Depends(require_auth)])
def acquire_permit(payload: dict[str, Any]) -> dict[str, Any]:
    """Reserve a slot against the fleet's **live** capacity, or say there is none.

    It used to hand back an id and `expires_at: None` — a permit that reserved nothing, for ever,
    against a number that was a setting rather than a measurement (audit H7 and the LOW finding
    *"the permit route always grants"*). A caller that asks whether it may start work deserves the
    same answer the dispatcher will give it: the capacity the fleet has right now, and a refusal
    when there is none left.

    **Refused with 429, never 402** — the contract trap this repo has paid for: a 402 permanently
    locks the caller's API key. `Retry-After` is the honest hint, and it is the deferral the queue
    would apply anyway.
    """
    import uuid

    from . import concurrency

    capacity = concurrency.live_capacity(worker=worker)
    in_flight = worker.active()
    # **Which number bounds this request.** The fleet's live capacity for the modality asked
    # about, when the fleet has any — and the **pool** otherwise. That fallback is load-bearing
    # and was found by this repo's own e2e suite: a gateway with no Studio Hub location at all
    # (a cloud-only setup, or every Mac switched off) reports a fleet total of `0`, and refusing
    # every permit on that figure would have made the guard itself an outage. `0` from the fleet
    # means "no Mac is reachable", not "no capacity exists" — a cloud lane is still there, and a
    # request for a Hub model is refused properly at submit with `SITE_CAPACITY_UNAVAILABLE`.
    modality = _permit_modality(payload)
    fleet_limit = capacity["by_modality"].get(modality) if modality else None
    if fleet_limit:
        limit, source = int(fleet_limit), capacity["source"]
    else:
        limit = int(concurrency.global_limit())
        source = concurrency.global_budget().detail
    if limit > 0 and in_flight >= limit:
        retry_after = int(concurrency.queue_wait_budget()["defer_cap_seconds"])
        raise GatewayError(
            429,
            (f"no free capacity right now: {in_flight} of {limit} slots are in use "
             f"({source}). Retry after {retry_after} seconds."),
            "FLEET_AT_CAPACITY",
            headers={"Retry-After": str(retry_after)},
        )
    # **A permit that expires**, because a reservation nobody releases is the defect this route
    # already had. The TTL is the queue's own cap: long enough to submit, short enough that a
    # caller who walks away does not hold a slot for ever.
    ttl = int(concurrency.queue_wait_budget()["defer_cap_seconds"])
    return {
        "id": str(uuid.uuid4()),
        "model_id": payload.get("model_id"),
        "operation": payload.get("operation"),
        "expires_at": time.time() + ttl,
        "expires_in_seconds": ttl,
        "capacity": {
            "total": limit,
            "in_flight": in_flight,
            "available": max(0, limit - in_flight),
            "by_modality": capacity["by_modality"],
            "source": source,
            "fleet_total": capacity["total"],
        },
    }


#: Which modality an admission permit is about, when the caller names one.
#:
#: `operation` is Story Studio's own vocabulary (`text-to-image`), and `modality` is this
#: gateway's; both spellings are accepted because both are on the wire. An operation this gateway
#: does not recognise answers `None`, which the guard reads as "ask the pool" rather than guessing
#: a lane.
_PERMIT_OPERATIONS = {
    "text-to-image": "image",
    "image": "image",
    "image-to-image": "image",
    "text-to-video": "video",
    "image-to-video": "video",
    "video": "video",
    "text-to-speech": "tts",
    "tts": "tts",
    "transcription": "transcription",
    "music": "music",
    "chat": "chat",
}


def _permit_modality(payload: dict[str, Any]) -> str | None:
    for key in ("modality", "operation"):
        value = str(payload.get(key) or "").strip().lower()
        if value in _PERMIT_OPERATIONS:
            return _PERMIT_OPERATIONS[value]
    return None


@app.post("/v1/images/generations", dependencies=[Depends(require_auth)])
def create_image(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    _validate_output_format("image", payload)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    reference_ids = _image_reference_ids(payload)
    # **A replay is answered before the uploads are looked at.** The job already exists and has its
    # own staged copies, so a caller whose 202 was lost gets its job back even after the 15-minute
    # upload window closed — and if the references it now names are a different set, that is a
    # different render and a 409 rather than the earlier job's image.
    already = store.find_by_idempotency_key(key) if reference_ids is not None else None
    if already is not None and already["modality"] == "image":
        _refuse_changed_references(already, reference_ids)
        return _replay_marked(_job_envelope(already), False)
    params = _stage_image_references(payload)
    job, created = _submit("image", model_id, params, key, caller=caller)
    if not created:
        # Two concurrent submissions can both pass the check above; the store settles which one
        # made the job, and its reference set is still the one this key now means.
        _refuse_changed_references(job, reference_ids or [])
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/images/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _replay_marked(_job_envelope(job), created)


@app.get("/v1/images/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def image_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "image":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/images/{job_id}", dependencies=[Depends(require_auth)])
def get_image_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "image"))


def _artifact_response(
    job: dict[str, Any], default_media: str, asset_id: str | None = None
) -> Response:
    """Serve one artifact from a job.

    `asset_id` is the index a multi-artifact route names. It used to be accepted
    by `/v1/music/jobs/{id}/outputs/{asset_id}` and then ignored, so every index
    answered the first track — a control that did nothing, pointing at exactly the
    variation a caller could not otherwise reach. Naming an index that does not
    exist is refused rather than silently answered with track 0.
    """
    if job["state"] != "succeeded":
        raise GatewayError(409, "Job is not complete.", "JOB_NOT_COMPLETE")

    rows = artifacts_of(job)
    if not rows:
        raise GatewayError(409, "Job is not complete.", "JOB_NOT_COMPLETE")

    if asset_id is None:
        chosen = rows[0]
    else:
        chosen = next((r for r in rows if str(r.get("asset_id")) == str(asset_id)), None)
        if chosen is None:
            raise GatewayError(
                404,
                f"This job has no artifact '{asset_id}'; it has {len(rows)}.",
                "ARTIFACT_NOT_FOUND",
            )

    try:
        return stream_file(Path(chosen["path"]), chosen.get("media_type") or default_media)
    except FileNotFoundError:
        raise GatewayError(410, "Artifact is gone.", "ARTIFACT_GONE")


def stream_file(path: Path, media_type: str, headers: dict[str, str] | None = None) -> StreamingResponse:
    """Serve a file from an already-open handle, 1 MiB at a time.

    A finished chapter is 64 MB of WAV and `read_bytes()` held every concurrent fetch in memory
    whole. Opening *here* keeps the typed 410 when the file is already gone (`FileNotFoundError`
    reaches the caller) and a file removed after the open still finishes streaming (POSIX).
    """
    handle = path.open("rb")
    size = os.fstat(handle.fileno()).st_size

    def chunks():
        with handle:
            while block := handle.read(1 << 20):
                yield block

    return StreamingResponse(chunks(), media_type=media_type,
                             headers={**(headers or {}), "Content-Length": str(size)})


@app.get("/v1/images/{job_id}/file", dependencies=[Depends(require_auth)])
def get_image_file(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "image"), "image/png")


@app.post("/v1/images/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_image_job(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "image")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


@app.post("/v1/images/source-images", dependencies=[Depends(require_auth)])
async def upload_image_source_image(
    file: UploadFile | None = File(default=None),
    upload: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Hold one reference image a reference-guided generation will name.

    Story Studio's "Reference to Image" flow uploads here first and then submits with the returned
    ids as ``reference_asset_ids`` — the same two-step shape as image-to-video, and this is the one
    address Story knows, so the bytes are held between the calls (``gateway/image_assets.py``).

    **The answer is Story's field names, not the video route's.** It reads ``id``, ``media_type``,
    ``byte_size``, ``width``, ``height`` and ``expires_at``; the video route answers ``bytes`` and
    ``expires_in_seconds``, and an image client handed those throws.

    **The body is read as an image here, not at the worker.** A body that is not a readable image of
    the type it claims would fail mid-render on a Mac that has already taken the job; the upload door
    is the cheapest place to refuse it, and it is the only place that can name the caller's file.
    """
    from . import image_assets

    chosen = upload or file
    if chosen is None:
        raise GatewayError(
            400, "Upload a reference image (multipart field 'upload').", "INVALID_PARAMS"
        )
    content = await chosen.read()
    _refuse_when_the_disk_is_low(len(content))
    if not content:
        raise GatewayError(400, "The reference image is empty.", "INVALID_PARAMS")
    if len(content) > image_assets.MAX_SOURCE_IMAGE_BYTES:
        raise GatewayError(
            413,
            f"That image is {len(content) / (1024 * 1024):.1f} MB; the limit is "
            f"{image_assets.MAX_SOURCE_IMAGE_BYTES // (1024 * 1024)} MB per image.",
            "PAYLOAD_TOO_LARGE",
        )
    media_type = (chosen.content_type or "").split(";")[0].strip().lower()
    if media_type not in image_assets.ACCEPTED_MEDIA_TYPES:
        raise GatewayError(
            400,
            f"Unsupported reference image type {media_type or 'unknown'!r}; "
            f"use one of {', '.join(image_assets.ACCEPTED_MEDIA_TYPES)}.",
            "INVALID_PARAMS",
        )
    size = image_assets.dimensions(content, media_type)
    if size is None:
        raise GatewayError(
            400,
            f"That file is not a readable {media_type} image; its header does not state a size. "
            "Upload an intact PNG, JPEG or WebP.",
            "INVALID_PARAMS",
        )
    held = image_assets.store(content, chosen.filename or "reference.png", media_type)
    return {"id": held["id"], "media_type": held["media_type"],
            "byte_size": held["byte_size"], "width": size[0], "height": size[1],
            "expires_at": held["expires_at"]}


def _image_reference_ids(payload: dict[str, Any]) -> list[str] | None:
    """The uploaded reference assets a generation names, in order.

    `None` means the request names none at all — an ordinary text-to-image job. **An empty list is
    not the same thing**: the caller asked for a reference render with nothing in it, and a model
    that needs at least one must refuse that rather than quietly render from the prompt alone.

    Read here rather than at dispatch because the id is the whole request: it decides the route (a
    reference job may only run where `edit` is advertised) and the replay (a key replayed with
    different references is a different render). A list the caller got wrong is refused before
    anything is staged, so it cannot become "some of your references were used".
    """
    raw = payload.get("reference_asset_ids") if isinstance(payload, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise GatewayError(
            400,
            "reference_asset_ids must be a list of the ids POST /v1/images/source-images returned.",
            "INVALID_PARAMS",
        )
    return [str(item).strip() for item in raw]


def _stage_image_references(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep every reference image with the job, the way a transcription's audio is staged.

    The upload lives 15 minutes in process memory, but the adapter reads the bytes at *dispatch*: a
    job that waited longer in the queue, or outlived a restart, would have lost them. Written beside
    the other staged inputs at submit, so the job carries its own copies — all of them, in order —
    and the retention sweep knows to keep them while the job is queued or held (``_reference_paths``
    is one of ``store._INPUT_PARAM_KEYS``).

    A reference that is gone is a refusal, never a shorter list: sending one image fewer would be a
    render of different characters under the same request, which is the failure this feature exists
    to make impossible.
    """
    from . import image_assets

    asset_ids = _image_reference_ids(payload)
    if asset_ids is None:
        return payload
    directory = ARTIFACT_DIR / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    types: list[str] = []
    digests: list[str] = []
    expired: list[str] = []
    for asset_id in asset_ids:
        asset = image_assets.get(asset_id)
        if asset is None:
            expired.append(asset_id)
            continue
        staged = directory / f"image-ref-{asset['digest']}"
        staged.write_bytes(asset["content"])
        paths.append(str(staged))
        types.append(asset["media_type"])
        digests.append(asset["digest"])
    if expired:
        raise GatewayError(
            400,
            f"Reference image {', '.join(expired)} is no longer held — the upload window is "
            f"{image_assets.TTL_S // 60} minutes. Upload it again and retry.",
            "REFERENCE_ASSET_EXPIRED",
        )
    return {**payload, "_reference_paths": paths, "_reference_types": types,
            "_reference_digests": digests}


def _job_reference_digests(job: dict[str, Any]) -> list[str]:
    """The reference digests the job was made with, from its stored params."""
    params = job.get("params")
    if not isinstance(params, dict):
        try:
            params = json.loads(params or "{}")
        except (TypeError, ValueError):
            return []
    digests = params.get("_reference_digests") if isinstance(params, dict) else None
    return [str(item) for item in digests] if isinstance(digests, list) else []


def _refuse_changed_references(job: dict[str, Any], digests: list[str]) -> None:
    """Refuse a replay whose reference set is not the one that job was made with.

    A key identifies one request. Handing back the earlier job for a request carrying different
    characters would answer with an image of the wrong people — silently, because the envelope looks
    exactly like a success.
    """
    stored = _job_reference_digests(job)
    if stored == list(digests):
        return
    raise GatewayError(
        409,
        f"That Idempotency-Key was already used for an image request naming "
        f"{len(stored)} reference image{'s' if len(stored) != 1 else ''}, and this request names "
        f"{len(digests)} whose content differs. A key identifies one request — use a new one for "
        "this render.",
        "IDEMPOTENCY_KEY_REUSED",
    )


def _refuse_references_this_lane_cannot_take(
    definition: Any, model: dict[str, Any], params: dict[str, Any]
) -> None:
    """Refuse references a model cannot take, and a count its lane does not.

    Two checks, both read from the model's own statement:

      * **The lane must take references at all.** Routing keeps a reference job off a location that
        does not advertise `edit`, but a legacy site-scoped id (`studio-hub-<site>::…`) and a cloud
        id resolve *without* routing — the caller named the lane. Without this line that path would
        hand `_reference_paths` to an adapter that never reads them and answer with a text-to-image
        render that looks like success.
      * **The count must be inside the window the lane states.** The figures are the ones the
        catalogue publishes as `min_reference_images` / `max_reference_images`, so what is enforced
        and what Story was shown are one value, and the message names them.

    A lane that states no limits is not refused here: the worker is then the only authority, and a
    gateway number would be one Story was never told about.
    """
    from .providers.definitions import (
        KIND_STUDIO_HUB, image_reference_limits, image_reference_state,
    )

    raw = (model or {}).get("raw") or {}
    states = image_reference_state(str(getattr(definition, "kind", "")),
                                   str((model or {}).get("model_id") or ""), raw)
    if states is not True:
        raise GatewayError(
            400,
            "This model does not take reference images. Pick a model whose catalogue row "
            "publishes supports_reference_images: true, or send the request without "
            "reference_asset_ids.",
            "INVALID_PARAMS",
        )
    if getattr(definition, "kind", None) != KIND_STUDIO_HUB:
        # The model takes references; this gateway cannot carry them to *this* lane. The Studio
        # Hub adapter is the only one that reads the staged reference bytes today, so a cloud
        # endpoint that wants a reference URL of its own would be handed nothing — the silent
        # text-to-image render this whole guard exists to prevent. Whose limit it is, said plainly.
        raise GatewayError(
            400,
            "This model takes reference images, but this gateway cannot send them to that "
            "provider's lane yet. Pick the Studio Hub model, or send the request without "
            "reference_asset_ids.",
            "INVALID_PARAMS",
        )
    limits = image_reference_limits(raw)
    count = len(params.get("_reference_digests") or [])
    maximum, minimum = limits["max"], limits["min"]
    if isinstance(maximum, int) and count > maximum:
        raise GatewayError(
            400,
            f"This model takes at most {maximum} reference image{'s' if maximum != 1 else ''}; "
            f"this request names {count}.",
            "INVALID_PARAMS",
        )
    if isinstance(minimum, int) and count < minimum:
        raise GatewayError(
            400,
            f"This model needs at least {minimum} reference image{'s' if minimum != 1 else ''}; "
            f"this request names {count}.",
            "INVALID_PARAMS",
        )


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------
@app.post("/v1/videos/source-images", dependencies=[Depends(require_auth)])
async def upload_video_source_image(
    file: UploadFile | None = File(default=None),
    upload: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Hold the exact scene image an image-to-video generation will animate.

    Story Studio's video flow uploads here first and then submits with the returned id as
    ``reference_asset_id`` — that two-step shape is Story's, and this is the one address it
    knows, so the bytes are held between the calls (``gateway/video_assets.py``).

    **The field is accepted under both names it arrives as.** Story's client sends ``upload``;
    the transcription door next to this one takes ``file``. Accepting one name only would have
    made the difference between a working I2V lane and a 422 that names a field nobody set.
    """
    from . import video_assets

    chosen = upload or file
    if chosen is None:
        raise GatewayError(
            422, "Upload a source image (multipart field 'upload').", "INVALID_REQUEST"
        )
    content = await chosen.read()
    _refuse_when_the_disk_is_low(len(content))
    if not content:
        raise GatewayError(422, "The source image is empty.", "INVALID_REQUEST")
    if len(content) > video_assets.MAX_SOURCE_IMAGE_BYTES:
        raise GatewayError(
            413,
            f"That image is {len(content) / (1024 * 1024):.1f} MB; the limit is "
            f"{video_assets.MAX_SOURCE_IMAGE_BYTES // (1024 * 1024)} MB per request.",
            "UPLOAD_TOO_LARGE",
        )
    media_type = (chosen.content_type or "").split(";")[0].strip().lower()
    if media_type not in video_assets.ACCEPTED_MEDIA_TYPES:
        raise GatewayError(
            422,
            f"Unsupported source image type {media_type or 'unknown'!r}; "
            f"use one of {', '.join(video_assets.ACCEPTED_MEDIA_TYPES)}.",
            "INVALID_REQUEST",
        )
    asset_id = video_assets.store(content, chosen.filename or "scene.png", media_type)
    return {"id": asset_id, "bytes": len(content), "media_type": media_type,
            "expires_in_seconds": video_assets.TTL_S}


def _stage_video_reference(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep every source image with the job, the way a transcription's audio is staged.

    The upload lives 15 minutes in process memory, but the adapter reads it at *dispatch*: a video
    job that waited longer in the queue, or outlived a restart, failed with "upload it again".
    Written beside the other staged inputs at submit, so the job carries its own bytes (and the
    retention sweep knows not to delete them while it is queued or held).

    **Every input the caller named is copied, and none may be missing.** A reference that has aged
    out is a refusal, never a shorter list and never a request that quietly loses its picture: a
    video generated without the scene image is a video of something else, and Story Studio cannot
    tell it apart from the one it asked for.

    Three shapes, because three request shapes exist: `reference_asset_id` (a first frame),
    `end_image_asset_id` (a final frame, for the models whose endpoint declares one) and
    `reference_asset_ids` (reference-to-video). The keys are the ones `store._INPUT_PARAM_KEYS`
    protects and the adapters read.
    """
    from . import video_assets

    directory = ARTIFACT_DIR / "uploads"
    staged = dict(payload)

    def copy(asset_id: Any, prefix: str) -> tuple[str, str]:
        value = str(asset_id or "").strip()
        asset = video_assets.get(value)
        if asset is None:
            raise GatewayError(
                400,
                f"Source image {value} is no longer held — the upload window is "
                f"{video_assets.TTL_S // 60} minutes. Upload it again and retry.",
                "REFERENCE_ASSET_EXPIRED",
            )
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{prefix}-{value}"
        target.write_bytes(asset["content"])
        return str(target), str(asset["media_type"])

    if str(payload.get("reference_asset_id") or "").strip():
        staged["_reference_path"], staged["_reference_type"] = copy(
            payload["reference_asset_id"], "video-ref"
        )
    if str(payload.get("end_image_asset_id") or "").strip():
        staged["_end_reference_path"], staged["_end_reference_type"] = copy(
            payload["end_image_asset_id"], "video-end-ref"
        )
    references = payload.get("reference_asset_ids")
    if references is not None:
        if not isinstance(references, list) or not all(
            isinstance(item, str) and item.strip() for item in references
        ):
            raise GatewayError(
                400,
                "reference_asset_ids must be a list of the ids POST /v1/videos/source-images "
                "returned.",
                "INVALID_PARAMS",
            )
        pairs = [copy(asset_id, "video-ref") for asset_id in references]
        staged["_reference_paths"] = [path for path, _ in pairs]
        staged["_reference_types"] = [media_type for _, media_type in pairs]
    return staged


#: Every field that changes what is bought. An idempotency key identifies one request, so a replay
#: carrying a different scene image, duration or canvas is a different video — and answering it
#: with the earlier job's clip is indistinguishable from a success.
_VIDEO_REQUEST_FIELDS: tuple[str, ...] = (
    "model_id", "model", "mode", "operation", "prompt", "duration_seconds", "resolution",
    "aspect_ratio", "audio", "reference_asset_id", "end_image_asset_id", "reference_asset_ids",
)


def _refuse_changed_video_request(job: dict[str, Any], payload: dict[str, Any]) -> None:
    """Refuse a replay whose request is not the one that job was made with."""
    stored = job.get("params")
    if not isinstance(stored, dict):
        try:
            stored = json.loads(stored or "{}")
        except (TypeError, ValueError):
            return
    if not isinstance(stored, dict):
        return
    for field in _VIDEO_REQUEST_FIELDS:
        if stored.get(field) != payload.get(field):
            raise GatewayError(
                409,
                f"That Idempotency-Key was already used for a different video request ({field} "
                "changed). A key identifies one request; use a new one for a different video.",
                "IDEMPOTENCY_KEY_REUSED",
            )


@app.post("/v1/videos/generations", dependencies=[Depends(require_auth)])
def create_video(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    # **A replay is answered before the uploads are looked at.** The job already exists and carries
    # its own staged copies, so a caller whose 202 was lost gets its job back even after the
    # 15-minute upload window closed — unless it is now naming a different request.
    already = store.find_by_idempotency_key(key)
    if already is not None and already["modality"] == "video":
        _refuse_changed_video_request(already, payload)
        return _replay_marked(_job_envelope(already), False)
    job, created = _submit("video", model_id, _stage_video_reference(payload), key, caller=caller)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/videos/{job['id']}"
        response.headers["Retry-After"] = "2"
    return _replay_marked(_job_envelope(job), created)


@app.get("/v1/videos/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def video_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "video":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/videos/{job_id}", dependencies=[Depends(require_auth)])
def get_video_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "video"))


@app.get("/v1/videos/{job_id}/file", dependencies=[Depends(require_auth)])
def get_video_file(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "video"), "video/mp4")


@app.post("/v1/videos/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_video_job(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "video")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
def _voice_tier(voice_id: Any) -> str:
    """The tier of the voice the quote names, in the catalogue's own words.

    A quote used to answer a flat `"standard"` for every voice, while the voice
    catalogue publishes `custom` for the owner's own stock and `library` for a
    provider's public library — so the same voice was described two ways
    depending on which endpoint answered. An unknown or absent voice keeps
    `standard`, which is what the caller is actually asking about in that case.
    """
    from . import voices as voices_module

    if not voice_id:
        return "standard"
    found = voices_module.voice_registry().find(str(voice_id))
    if found is None:
        return "standard"
    # **The same rule the voice row uses**: ownership, not "some source that is
    # not the library". They were two spellings of one question, so a voice could
    # be `custom` in the catalogue and `library` in its own quote.
    return "custom" if found.owned else "library"


@app.post("/v1/tts/quotes", dependencies=[Depends(require_auth)])
def tts_quote(payload: dict[str, Any]) -> dict[str, Any]:
    model_id = str(payload.get("model_id") or "")
    definition, model = catalog.resolve("tts", model_id)
    if definition is None or model is None:
        raise GatewayError(404, f'TTS model "{model_id}" is not available.', "MODEL_NOT_FOUND")
    text = str(payload.get("text") or "")
    # The limit comes from the same function that builds the catalogue row, so the
    # two cannot disagree. This used to be a flat 40,000 — the FIGURE the
    # catalogue had already stopped publishing: it over-promised the local lane by
    # 60% and Fish by eight times, and it meant Story Studio validated a narration
    # against a limit that contradicted the model row it came from.
    #
    # It cannot be read off the catalogue row here: `catalog.resolve` returns the
    # *inventory* model (raw provider fields), not the published row.
    from .catalog import _TTS_CHARACTERS_DEFAULT, _tts_characters

    limit, limit_source = _tts_characters(str(model.get("model_id") or ""), model, definition)
    if not isinstance(limit, int) or limit <= 0:
        limit, limit_source = _TTS_CHARACTERS_DEFAULT, "gateway"
    # The quote says in advance what submitting would refuse: a language this model cannot speak.
    try:
        _refuse_an_unspoken_language(model_id, model, payload)
        language_notice = None
    except GatewayError as refused:
        language_notice = refused.detail
    return {
        "model_id": model_id,
        "model_name": model["display_name"],
        "voice_id": payload.get("voice_id"),
        "voice_tier": _voice_tier(payload.get("voice_id")),
        "billable_characters": len(text),
        "speed": payload.get("speed", 1.0),
        # ponytail: a flat 14 characters/second, **ours**, and labelled as ours by
        # the companion field below. Ceiling — speech rate varies several-fold
        # across languages and voices, so this is rough for anything that is not
        # English prose. Upgrade path — measure it per model and voice from a
        # produced artifact, the way a reference clip's contract is measured.
        "estimated_duration_ms": int(max(len(text) / 14.0, 0.5) * 1000),
        "estimated_duration_ms_source": "gateway",
        "maximum_input_characters": limit,
        # Same value, same decision, and the same label as the model row carries:
        # a quote that disagreed with the row it came from is what put the old flat
        # 40,000 on this route after the catalogue had stopped publishing it.
        "maximum_input_characters_source": limit_source,
        "controls": payload.get("controls", {}),
        "credits": 0,
        "maximum_credits": 0,
        "credits_per_1000_characters": 0,
        "price_version": "flat",
        # **The container this lane actually returns**, from the one function the
        # voice row uses. This was a flat `"wav"` for every model, so an
        # ElevenLabs quote promised WAV while the adapter downloaded
        # `mp3_44100_128`. `None` where the kind has no TTS lane.
        "output_format": tts_output_container(definition.id),
        # Published EXPLICITLY: the parser does `payload?.chargeable === true`,
        # so an absent field renders a claim the gateway never made. Truthful, by the
        # catalogue row's own rule: `false` on the owner's Macs or a free lane.
        "chargeable": definition_chargeable(
            definition, "tts", str(model.get("model_id") or ""), model.get("raw")),
        "generation_enabled": language_notice is None,
        "notice": language_notice,
    }


@app.post("/v1/audio/jobs", dependencies=[Depends(require_auth)])
def create_tts_job(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    _validate_output_format("tts", payload)
    _validate_tts_pauses(payload)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("tts", model_id, payload, key, caller=caller)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/jobs/{job['id']}"
        response.headers["Retry-After"] = "1"
    return _replay_marked(_job_envelope(job), created)


@app.get("/v1/jobs/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def tts_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "tts":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_tts_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "tts"))


@app.get("/v1/jobs/{job_id}/audio", dependencies=[Depends(require_auth)])
def get_tts_audio(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "tts"), "audio/wav")


#: The media type to fall back on per modality when a job recorded none. The stored
#: type is what the provider actually answered, so it wins; this only covers a job
#: whose artifact arrived with no `content-type` at all.
_MEDIA_BY_MODALITY = {
    "image": "image/png",
    "video": "video/mp4",
    "music": "audio/mpeg",
    "tts": "audio/wav",
    "transcription": "application/x-subrip",
}


@app.get("/v1/jobs/{job_id}/outputs/{asset_id}", dependencies=[Depends(require_auth)])
def get_job_output(job_id: str, asset_id: str) -> Response:
    """Serve one artifact of a job, by the asset id the job envelope published.

    Modality-agnostic on purpose: the route that reaches the *second* result of a
    multi-artifact job is the same shape whichever lane produced it, so a caller
    that asked for two images, two video takes or two music variations fetches them
    the same way. Music additionally keeps its own `/v1/music/jobs/…/outputs/…`
    route, which it has always published.
    """
    job = store.get(job_id)
    if job is None:
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _artifact_response(
        job,
        _MEDIA_BY_MODALITY.get(str(job.get("modality")), "application/octet-stream"),
        asset_id=asset_id,
    )


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_tts_job(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "tts")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------
@app.post("/v1/music/generations", dependencies=[Depends(require_auth)])
def create_music(
    payload: dict[str, Any],
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    _validate_output_format("music", payload)
    model_id = str(payload.get("model_id") or payload.get("model") or "")
    job, created = _submit("music", model_id, payload, key, caller=caller)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/music/jobs/{job['id']}"
        response.headers["Retry-After"] = "2"
    return _replay_marked(_job_envelope(job), created)


@app.get("/v1/music/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def music_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "music":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/music/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_music_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "music"))


@app.get("/v1/music/jobs/{job_id}/outputs/{asset_id}", dependencies=[Depends(require_auth)])
def get_music_output(job_id: str, asset_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "music"), "audio/mpeg", asset_id=asset_id)


@app.post("/v1/music/jobs/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_music_job(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "music")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


# ---------------------------------------------------------------------------
# Transcription — quote (multipart upload) -> confirm -> poll.
# ---------------------------------------------------------------------------
def _prune_uploads(now: float) -> None:
    for key in [k for k, v in _UPLOADS.items() if now - v[3] > _UPLOAD_TTL_S]:
        _UPLOADS.pop(key, None)
    for key in [k for k, quote_id in _QUOTE_BY_KEY.items() if quote_id not in _UPLOADS]:
        # Bound only while its quote is held (a quote still being written has no upload yet).
        if now - _QUOTE_KEY_AT.get(key, now) > 60:
            _QUOTE_BY_KEY.pop(key, None)
            _QUOTE_KEY_AT.pop(key, None)


def _quote_answer(quote_id: str, upload: tuple[Any, ...]) -> dict[str, Any]:
    """The quote envelope for a held upload (same shape the first answer had)."""
    return {
        "id": quote_id,
        "quoted_credit_cost": 0,
        "price_version": "flat",
        "expires_at": datetime.fromtimestamp(upload[3] + _UPLOAD_TTL_S, timezone.utc).isoformat(),
        "model": upload[4],
    }


@app.post("/v1/audio/transcriptions/quotes", dependencies=[Depends(require_auth)])
async def transcription_quote(
    request: Request,
    file: UploadFile | None = File(default=None),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
    word_timestamps: str | None = Form(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Accept the audio upload and quote it.

    Story Studio uploads the media here (multipart) and only later confirms, so
    the bytes are held between the two calls.

    A JSON body is also accepted, and it is a **price-only** quote: it names a
    model and holds no audio, so the confirm for it will refuse for lack of audio
    rather than pretending the quote expired. It used to be advertised as "for
    callers that upload separately", which was never true — there is no route
    that uploads separately, so every confirm of a JSON quote answered 410 and
    the caller's only option was to re-quote the same way and fail again.
    """
    import uuid

    # **A retried quote with the same key is the same quote.** Story sends the key on the upload and
    # retries a timed-out POST up to four times; the gateway ignored it, so every retry held another
    # full copy of the audio (up to 100 MiB each) for an hour and returned a different quote id. A key
    # already bound to a quote whose audio is still held answers with that quote, untouched.
    if idempotency_key:
        held = _QUOTE_BY_KEY.get(idempotency_key)
        if held is not None:
            upload = _UPLOADS.get(held)
            if upload is not None and time.time() - upload[3] <= _UPLOAD_TTL_S:
                return _quote_answer(held, upload)
            _QUOTE_BY_KEY.pop(idempotency_key, None)
    quote_id = str(uuid.uuid4())
    if idempotency_key:
        _QUOTE_BY_KEY[idempotency_key] = quote_id
        _QUOTE_KEY_AT[idempotency_key] = time.time()
    body_model = model
    if file is None and request.headers.get("content-type", "").startswith("application/json"):
        # A body that cannot be read is the caller's problem, and it has to be
        # answered as one. `await request.json()` raises on an empty body or
        # malformed JSON, and unhandled that became a **500** — for a request that
        # is merely incomplete, and on the endpoint Story Studio uses to send audio.
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any unreadable body is a bad request
            raise GatewayError(
                400,
                "Expected a JSON body with a model, or an audio file to upload.",
                "INVALID_REQUEST",
            )
        if not isinstance(payload, dict):
            raise GatewayError(400, "Expected a JSON object.", "INVALID_REQUEST")
        body_model = str(body_model or payload.get("model") or "")
        _prune_uploads(now := time.time())
        _UPLOADS[quote_id] = (None, "", "", now, body_model)
        return {
            "id": quote_id,
            "quoted_credit_cost": 0,
            "price_version": "flat",
            "expires_at": datetime.fromtimestamp(now + _UPLOAD_TTL_S, timezone.utc).isoformat(),
            "model": body_model,
        }

    if file is not None:
        content = await file.read()
        # Checked here, before anything is held or staged. `UploadFile` spools to
        # disk rather than buffering in memory, so an oversized upload does not
        # exhaust the process — but it would still be written, staged and queued
        # against a provider that is going to refuse it, and the caller would get
        # a provider-shaped failure instead of the one sentence that explains it.
        _refuse_when_the_disk_is_low(len(content))
        limit = upload_limit_bytes(body_model)
        if len(content) > limit:
            raise GatewayError(
                413,
                f"That audio is {len(content) / (1024 * 1024):.1f} MB; the limit is "
                f"{limit // (1024 * 1024)} MB per request.",
                "UPLOAD_TOO_LARGE",
            )
        _prune_uploads(now := time.time())
        _UPLOADS[quote_id] = (
            content,
            file.filename or "audio.mp3",
            file.content_type or "application/octet-stream",
            now,
            str(body_model or ""),
            # What Story validated against the model's published languages and sent with the
            # audio: carried to the confirm, or it is told one language and runs another.
            {key: value for key, value in (
                ("language", (language or "").strip() or None),
                ("word_timestamps", None if word_timestamps is None
                 else word_timestamps.strip().lower() in {"1", "true", "yes", "on"}),
            ) if value is not None},
        )

    return {
        "id": quote_id,
        "quoted_credit_cost": 0,
        "price_version": "flat",
        "expires_at": datetime.fromtimestamp(time.time() + _UPLOAD_TTL_S, timezone.utc).isoformat(),
        "model": body_model,
    }


@app.post(
    "/v1/audio/transcriptions/quotes/{quote_id}/confirm",
    dependencies=[Depends(require_auth)],
)
async def confirm_transcription(
    quote_id: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    caller: str | None = Depends(caller_hint),
    model: str | None = Form(default=None),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    # Read the requested identity before reattaching: replay must not make an
    # explicit model change look accepted. Omitted model still reattaches after
    # expiry/restart using only its durable idempotency key.
    body = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - unreadable JSON supplies no override
            body = None
        if model is None and isinstance(body, dict) and body.get("model"):
            model = str(body["model"])
    existing = store.find_by_idempotency_key(key)
    if existing is not None:
        if existing["modality"] != "transcription":
            raise GatewayError(409, "Idempotency key belongs to another modality.", "IDEMPOTENCY_KEY_CONFLICT")
        # The same Hub model may have been submitted using its location ID or
        # its published fleet ID. Accept only those two spellings of this job,
        # not a different location or model.
        identities = {catalog_encode(existing), encode_model_id(
            existing.get("provider_id") or "", existing["model_id"])}
        if model and model not in identities:
            raise GatewayError(409, "Confirmation model differs from the confirmed model. Request a new quote to change models.", "TRANSCRIPTION_QUOTE_MODEL_MISMATCH")
        return _job_envelope(existing)
    _prune_uploads(time.time())
    upload = _UPLOADS.get(quote_id)
    # An expired quote is a typed terminal condition for Story Studio (410),
    # which forces it to re-quote rather than retry a job it cannot satisfy.
    if upload is None:
        raise GatewayError(
            410, "Transcription quote expired or was lost on restart. Upload the audio again to obtain a new quote.", "TRANSCRIPTION_QUOTE_EXPIRED"
        )

    content, name, content_type, _, _quoted_model, *rest = upload
    if content is None:
        raise GatewayError(
            400,
            "That quote carries a price but no audio. Upload the audio with the "
            "quote — POST /v1/audio/transcriptions/quotes with the file — then confirm.",
            "TRANSCRIPTION_AUDIO_MISSING",
        )

    # Resolve the model from the quote's own model when the confirm omits it, so
    # the model that runs is the model that was quoted.
    model_id = _quote_model(quote_id, model)
    if not model_id:
        raise GatewayError(
            404, "No transcription model is available.", "MODEL_NOT_FOUND"
        )
    definition, _ = catalog.resolve("transcription", model_id)
    if definition is None:
        raise GatewayError(
            404, f'Model "{model_id}" is not available.', "MODEL_NOT_FOUND"
        )

    # Job params are persisted as JSON, so raw audio cannot travel inside them.
    # Stage the upload on disk and pass a path; the adapter reads it back. This
    # also survives a gateway restart mid-queue, which an in-memory handoff
    # would not.
    upload_dir = ARTIFACT_DIR / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f"{quote_id}{Path(name).suffix or '.bin'}"
    staged.write_bytes(content)

    payload = {
        "_upload_path": str(staged),
        "_upload_name": name,
        "_upload_type": content_type,
        **(rest[0] if rest else {}),
    }
    if isinstance(body, dict) and body.get("job_group"):
        payload["job_group"] = body["job_group"]  # the confirm creates the job, so the group rides here
    if isinstance(body, dict) and body.get("priority") is not None:
        payload["priority"] = body["priority"]
    job, created = _submit("transcription", model_id, payload, key, caller=caller)
    if created:
        response.status_code = 202
        response.headers["Location"] = f"/v1/transcriptions/{job['id']}"
    # Keep the quote until its TTL for compatibility with existing clients.
    # A retry with the confirmed key reattaches from SQLite above, even after
    # this in-memory upload expires or a restart discards it.
    return _replay_marked(_job_envelope(job), created)


def _quote_model(quote_id: str, asked: str | None) -> str:
    """The model this quote will actually run.

    The quote's own model wins, because that is the model the caller was quoted
    and the one it will confirm. Only a quote that named none falls back, and the
    fallback is the first *enabled* transcription row — not merely the first row,
    which could be one the owner has switched off.
    """
    stored = _UPLOADS.get(quote_id)
    if stored and stored[4]:
        if asked and asked != stored[4]:
            raise GatewayError(409, "Confirmation model differs from the quoted model. Request a new quote to change models.", "TRANSCRIPTION_QUOTE_MODEL_MISMATCH")
        return stored[4]
    if asked:
        return asked
    rows = catalog.rows("transcription")
    return str(rows[0]["id"]) if rows else ""


@app.get("/v1/transcriptions/by-idempotency-key/{key}", dependencies=[Depends(require_auth)])
def transcription_by_idempotency_key(key: str) -> dict[str, Any]:
    job = store.find_by_idempotency_key(key)
    if job is None or job["modality"] != "transcription":
        raise GatewayError(404, "No such job.", "JOB_NOT_FOUND")
    return _job_envelope(job)


@app.get("/v1/transcriptions/{job_id}", dependencies=[Depends(require_auth)])
def get_transcription_job(job_id: str) -> dict[str, Any]:
    return _job_envelope(_load_job(job_id, "transcription"))


@app.get("/v1/transcriptions/{job_id}/srt", dependencies=[Depends(require_auth)])
def get_transcription_srt(job_id: str) -> Response:
    return _artifact_response(_load_job(job_id, "transcription"), "application/x-subrip")


@app.post("/v1/transcriptions/{job_id}/cancel", dependencies=[Depends(require_auth)])
def cancel_transcription_job(job_id: str) -> dict[str, Any]:
    job = _load_job(job_id, "transcription")
    store.cancel(job_id)
    _cancel_upstream(job)
    return _job_envelope(store.get(job_id))


# ---------------------------------------------------------------------------
# Content Store — private owner authority over explicitly qualified releases.
# ---------------------------------------------------------------------------
def _content_result(call, *args):  # type: ignore[no-untyped-def]
    from . import content_delivery
    try:
        return call(*args)
    except content_delivery.DeliveryError as exc:
        raise GatewayError(exc.status, str(exc), exc.code) from exc


def _content_origin(request: Request) -> str:
    """Use the accepted ASGI endpoint, never attacker-controlled Host input."""
    scheme = request.scope.get("scheme")
    server = request.scope.get("server")
    if scheme not in ("http", "https") or not isinstance(server, (tuple, list)) or len(server) != 2:
        raise GatewayError(503, "Trusted Gateway download origin is unavailable.", "CONTENT_DELIVERY_UNAVAILABLE")
    host, port = server
    if (not isinstance(host, str) or not host or any(character in host for character in "/?#@\r\n")
            or type(port) is not int or not 1 <= port <= 65535):
        raise GatewayError(503, "Trusted Gateway download origin is unavailable.", "CONTENT_DELIVERY_UNAVAILABLE")
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    return f"{scheme}://{rendered}{'' if default else f':{port}'}"


async def _content_body(
    request: Request,
    fields: dict[str, type] | tuple[dict[str, type], ...],
) -> dict[str, Any]:
    from .content_packs import PackError, _json
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise GatewayError(415, "Content requests require Content-Type: application/json.", "CONTENT_REQUEST_INVALID")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 64 * 1024:
            raise GatewayError(413, "Content request exceeds 64 KiB.", "CONTENT_REQUEST_INVALID")
        data.extend(chunk)
    try:
        body = _json(bytes(data))
    except PackError as exc:
        raise GatewayError(422, "Send one unambiguous JSON object.", "CONTENT_REQUEST_INVALID") from exc
    alternatives = fields if isinstance(fields, tuple) else (fields,)
    if (not isinstance(body, dict) or not any(
            set(body) == set(option)
            and all(type(body[key]) is kind for key, kind in option.items())
            for option in alternatives)):
        raise GatewayError(422, "Content request fields are invalid.", "CONTENT_REQUEST_INVALID")
    return body


@app.get("/v1/content/account")
def content_account(state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.account, state)


@app.get("/v1/content/products")
def content_products(story_version: str = Query(...),
                     state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.products, story_version, state)


@app.get("/v1/content/shelves")
def content_shelves(story_version: str = Query(...),
                    state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.shelves, story_version, state)


@app.get("/v1/content/public-key")
def content_public_key(state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.public_key, state)


@app.get("/v1/content/receipts")
def content_receipts(state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.receipts, state)


@app.get("/v1/content/entitlements/{family}/releases")
def content_release_history(family: str,
                            state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    return _content_result(content_delivery.release_history, family, state)


@app.post("/v1/content/downloads")
async def content_download(request: Request,
                           api_key: str = Depends(require_content_auth),
                           state: dict[str, Any] = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    body = await _content_body(request, (
        {"release_id": str},
        {"entitlement_family": str, "version": str},
    ))
    release_id = (body["release_id"] if "release_id" in body else
                  _content_result(content_delivery.resolve_release_id,
                                  body["entitlement_family"], body["version"], state))
    return _content_result(content_delivery.issue_download, release_id, _content_origin(request), api_key, state)


@app.get("/v1/content/archives/{release_id}")
def content_archive(release_id: str, grant: str = Query(...)) -> Response:
    from . import content_delivery
    data = _content_result(content_delivery.download_archive, release_id, grant)
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": 'attachment; filename="content-pack.sspack"',
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/v1/content/quotes")
@app.post("/v1/content/purchases")
def content_commerce_unavailable(_: str = Depends(require_content_authority)) -> dict[str, Any]:
    from . import content_delivery
    _content_result(content_delivery.ensure_configured)
    raise GatewayError(503, "Private Content Packs are owner entitlements and cannot be purchased.",
                       "CONTENT_COMMERCE_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Sound Library (Openverse)
# ---------------------------------------------------------------------------
from .sound_library import list_clips, search_library, download_into_library, quota_status
from .providers.registry import registry as _registry

@app.get("/v1/sounds/search", dependencies=[Depends(require_auth)])
def sounds_search(
    q: str = "",
    kind: str | None = None,
    min_sec: float | None = None,
    max_sec: float | None = None,
) -> dict[str, Any]:
    """Search upstream sources for commercial-safe audio clips."""
    from .sound_sources import all_sources
    sources = all_sources(_registry.credentials)
    results: list[dict[str, Any]] = []
    exhausted = False
    for src in sources:
        if not src.is_configured():
            continue
        try:
            hits = src.search(query=q, min_sec=min_sec, max_sec=max_sec, category=None, page_size=10)
            for h in hits:
                results.append({
                    "source": h.source,
                    "source_id": h.source_id,
                    "title": h.title,
                    "author": h.author,
                    "licence": h.licence,
                    "duration_ms": h.duration_ms,
                    "attribution": h.attribution,
                })
        except Exception as exc:
            log.warning("Sound search failed for %s: %s", src.id, exc)
        if src.quota.exhausted:
            exhausted = True
    # Deduplicate by source_id + source
    seen = set()
    unique = []
    for r in results:
        key = (r["source"], r["source_id"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    # Sort CC0 first
    unique.sort(key=lambda x: (0 if x["licence"] == "cc0" else 1, x["title"].casefold()))

    library_only = False
    if exhausted and not unique:
        # Fall back to local library when upstream quota is exhausted
        from .sound_library import search_library
        lib_hits = search_library(q, kind=kind)
        for clip in lib_hits:
            unique.append({
                "source": "library",
                "source_id": clip.get("sha256", ""),
                "title": clip.get("title", ""),
                "author": clip.get("author"),
                "licence": clip.get("licence"),
                "duration_ms": int(clip.get("duration_seconds", 0) * 1000) if clip.get("duration_seconds") else None,
                "attribution": clip.get("attribution"),
            })
        library_only = True

    return {"results": unique, "quota_exhausted": exhausted, "library_only": library_only}

@app.post("/v1/sounds/library", dependencies=[Depends(require_auth)])
def sounds_add_to_library(payload: dict[str, Any]) -> dict[str, Any]:
    """Download a clip into the shared library."""
    source = payload.get("source")
    source_id = payload.get("source_id")
    if not source or not source_id:
        raise GatewayError(400, "source and source_id required.")
    try:
        meta = download_into_library(str(source), str(source_id), _registry.credentials)
        return meta
    except Exception as exc:
        status = getattr(exc, "status", 500)
        code = getattr(exc, "code", None)
        raise GatewayError(status, str(exc), code)

@app.get("/v1/sounds/library", dependencies=[Depends(require_auth)])
def sounds_list_library() -> dict[str, Any]:
    """List clips already in the shared library."""
    return {"clips": list_clips()}

@app.get("/v1/sounds/quota", dependencies=[Depends(require_auth)])
def sounds_quota() -> dict[str, Any]:
    """Report live quota status for all sound sources."""
    return quota_status(_registry.credentials)


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------
from .admin import AdminError  # noqa: E402 - mounted after app setup
from .admin import router as admin_router  # noqa: E402

app.include_router(admin_router)


#: What a **guest** session may reach under `/admin`, and nothing more.
#:
#: **Deny by default, twice.** The dependencies are the rule — every route that has not
#: asked for `admin.require_guest_or_admin` takes `admin.require_admin`, which refuses a
#: guest before it looks at anything else. This list is the backstop underneath it: a page,
#: a path or a route mounted later without either one is still refused to a guest, because
#: reaching it has to be written here first. A test walks every registered admin route and
#: asserts the refusal, so the two cannot drift apart quietly.
_GUEST_ALLOWED = re.compile(
    r"^/admin/?$"                                             # the page itself (it hides what it may not use)
    r"|^/admin/(login|logout)$"
    r"|^/admin/api/create/models$"
    r"|^/admin/api/create$"
    r"|^/admin/api/create/transcription$"
    r"|^/admin/api/create/video$"
    r"|^/admin/api/create/status/[^/]+(/cancel)?$"
    r"|^/admin/api/voices$"
    r"|^/admin/api/jobs$"
    r"|^/admin/api/artifacts/keep$"
    r"|^/admin/api/artifacts/[^/]+/outputs/[^/]+$"
)


@app.middleware("http")
async def guest_scope(request: Request, call_next):
    """A guest is confined to Create, whatever route is asked for.

    Only a *guest* is filtered here. The owner, the app's window and the gateway key pass
    through untouched — their access is decided by the routes themselves.
    """
    path = request.url.path
    if not path.startswith("/admin"):
        return await call_next(request)
    from .admin import guest_identity

    # The browser's cookie and a guest's API key are one identity (review 2026-10-30): the
    # key is the same guest, so it is confined by the same list rather than by a second one.
    identity = guest_identity(request)
    if identity is not None:
        if not _GUEST_ALLOWED.match(path):
            return JSONResponse(
                status_code=403,
                content={"detail": "A guest account may only use the Create page.",
                         "code": "GUEST_NOT_ALLOWED"},
            )
    return await call_next(request)


# Added last, so it is the outermost of the app's own middleware: the body is counted before
# anything else in this process reads it.
app.add_middleware(BoundedUploadBody)



@app.exception_handler(AdminError)
async def _admin_error_handler(_: Request, exc: AdminError) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail}
    if getattr(exc, "code", None):
        body["code"] = exc.code
    return JSONResponse(status_code=exc.status, content=body)
