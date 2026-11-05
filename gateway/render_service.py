"""Private, non-chargeable final-render coordination over registered Hub relays."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx

from . import fleet, render_optin
from .render_assets import (
    AssetConflictError,
    AssetNotFoundError,
    AssetRecoveryRequiredError,
    AssetValidationError,
    RenderAssetStore,
)
from .render_contract import (
    LATE_LABEL_CONTRACT, LATE_LABEL_MOTIONS, SUBTITLE_CONTRACT,
    validate_render_encode, validate_render_packet_syntax,
)
from .render_png import validate_cue_png
from .render_encode import execution_authority
from .render_hash_v1 import decode_render_json
from .store import JobStore, RenderJournalCancelled, RenderJournalConflict


_MAX_JSON = 1024 * 1024
_CHUNK = 8 * 1024 * 1024
_STREAM = 64 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

log = logging.getLogger(__name__)

#: How often the redundant staging copies of published assets are swept. An hour matches
#: retention.PRUNE_INTERVAL_S and costs one bounded directory walk.
_SWEEP_INTERVAL_S = 3600.0
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_REQUEST_KEYS = frozenset(("format", "story_uuid", "intent_id", "attempt_no", "packet", "assets"))
#: Story Studio's optional channel/episode labels, which merely ride along to the Hub's Render tab.
#: **Display only**: no routing, dispatch, hashing, idempotency or acceptance decision reads them,
#: and the Gateway's own idempotency identity stays `inbound_story_body` bytes, as before.
_DISPLAY_KEYS = frozenset(("channel", "episode"))
_DISPLAY_MAX_CHARACTERS = 256
_REMOTE_STATES = frozenset((
    "accepted", "launch_pending", "rendering", "cancel_requested", "uncertain",
    "stopped", "cancelled", "failed", "output_ready",
))
_STAGING_STATES = frozenset((
    "staging", "hub_prepared", "send_may_start", "send_unknown", "accepted",
    "output_ready", "acknowledged",
))
_CANCEL_STATES = frozenset(("requested", "cancelled"))
#: The Hub's own name for the machine that ran a render. Every Studio Hub builds its render
#: executor with its broker's self-label (`machine="local"` in components/hub/standalone.py), and
#: a controller relays the executing node's answer body verbatim, so the machine that answers a
#: poll for `.../fleet/nodes/<node>/final-renders` **is** the node the Gateway addressed. `None`
#: is the Hub's answer before it claims the job; the node's id is accepted for a Hub that labels
#: itself by its fleet node. Any other name is a different machine and stays refused.
_HUB_SELF_LABEL = "local"
#: Consecutive status answers this Gateway cannot accept before the render is reported `failed`
#: to Story rather than polled for ever. At the 0.4 s dispatch clock this is about 12 s: long
#: enough to outlast a transient Hub 5xx, short enough that Story's `remote_wait` cannot hang on
#: an answer that will never become acceptable.
_HUB_STATUS_REJECTION_LIMIT = 30
#: How much of an unacceptable answer is quoted in the log: the Hub's words, never the whole body.
_ANSWER_EXCERPT_CHARACTERS = 240
_HUB_JOB_KEYS = frozenset((
    "id", "owner_scope", "dispatch_key", "accepted_request_sha256", "purpose", "state",
    "machine", "dispatch_generation", "attempt_id", "cancel_requested_at",
    "profile_digest", "tools_digest", "compiler_digest", "qualification_digest",
    "stopped_evidence_digest", "output_receipt", "leases", "replayed",
))
_HUB_LEASE_KEYS = frozenset(("compute", "reference", "reader", "transfer", "ack"))
#: A Hub qualification candidate, closed (`fleet._render_candidate_shape`); vector_id is the
#: intent. `encode` is the one optional key: the Hub publishes its compiler-derived fact on
#: qualification rows too, and a machine without an installed authority omits it or says null.
_QUALIFICATION_KEYS = frozenset((
    "vector_id", "hardware_class", "profile_digest", "tools_digest", "compiler_digest",
    "qualification_digest", "selected_tier", "exact_encoder", "font_support", "bounds",
    "profile_execution_bounds", "production_eligible", "qualification_only",
    "available_capacity",
))
_GATEWAY_RECEIPT_KEYS = frozenset((
    "format", "owner_scope", "gateway_instance_id", "story_instance_id",
    "gateway_job_id", "gateway_attempt_key", "story_uuid", "intent_id", "attempt_no",
    "inbound_story_body_sha256", "packet_digest", "outbound_hub_body_sha256",
    "hub_dispatch_key", "dispatch_fence", "hub_owner_scope", "hub_job_id",
    "hub_attempt_id", "hub_receipt_digest", "selected_machine", "profile_digest",
    "tools_digest", "compiler_digest", "qualification_digest", "selected_tier",
    "generated_command_digest", "feature_losses", "output_sha256", "output_size_bytes",
))
_HUB_RECEIPT_KEYS = frozenset((
    "owner_scope", "hub_job_id", "attempt_id", "accepted_request_sha256",
    "receipt_digest", "output_sha256", "output_size_bytes", "profile_digest",
    "tools_digest", "compiler_digest", "qualification_digest",
    "generated_command_digest", "selected_tier", "feature_losses",
))


def _hub_owner_scope(row: dict[str, Any]) -> str:
    # Pin the authenticated namespace at acceptance; a rotated credential cannot change it.
    kind = row["destination_credential_kind"]
    return f"{row['destination_site']}:{row['destination_hub_controller']}:{kind}"


def _stored_encode_binding(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("destination_encode_probe")
    if raw is None:
        return None
    try:
        probe = decode_render_json(bytes(raw))
        authority = execution_authority(
            probe, probe["requested_encode"], physical_node_id=row["destination_hub_controller"],
            capability=decode_render_json(bytes(row["capability_snapshot"])),
            chip=decode_render_json(bytes(row["selected_machine"]))["chip"],
        )
        if authority is None:
            raise ValueError("stored render has no encode authority")
        return authority["encode_authority"]
    except (ValueError, TypeError, KeyError):
        raise RenderServiceError("HUB_RESPONSE_UNKNOWN", 503) from None


class RenderServiceError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


class PrivateRenderUnavailable(RenderServiceError):
    def __init__(self) -> None:
        super().__init__("PRIVATE_RENDER_UNAVAILABLE", 503)


@dataclass(frozen=True)
class AuthenticatedPrivateRender:
    """Who a render request is for: this Gateway's installation plus the Story header."""
    owner_scope: str
    gateway_instance_id: str
    story_instance_id: str


@dataclass(frozen=True)
class RenderDestination:
    site_id: str
    roster_controller_id: str
    hub_controller_id: str
    node_id: str
    capability_snapshot_sha256: str = "0" * 64
    qualification_vector_id: str | None = None
    capability_snapshot: dict[str, Any] | None = None
    selected_machine: dict[str, Any] | None = None
    target_hub_controller_id: str | None = None
    #: The owner accepted this Mac unqualified (`render_optin`): its snapshot is a production-shaped Hub
    #: row carrying the Hub's own `accepted_unqualified` evidence. Anything else must carry none.
    accepted_unqualified: bool = False
    hub_credential_kind: str = "fleet"
    encode_probe: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.hub_credential_kind not in {"hub", "fleet"}:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        for value in (self.site_id, self.roster_controller_id,
                      self.hub_controller_id, self.node_id):
            _text(value, "RENDER_DESTINATION_UNAVAILABLE", pattern=_KEY)
        if _HEX64.fullmatch(self.capability_snapshot_sha256) is None:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if self.qualification_vector_id is not None:
            _text(self.qualification_vector_id, "RENDER_DESTINATION_UNAVAILABLE", pattern=_KEY)
        if self.target_hub_controller_id is not None:
            _text(self.target_hub_controller_id, "RENDER_DESTINATION_UNAVAILABLE", pattern=_KEY)


def destination_for_candidate(row: dict[str, Any]) -> RenderDestination:
    kind = "fleet"
    if row["node_id"] == fleet.LOCAL_ID:
        if row["roster_controller_id"] == fleet.LOCAL_ID:
            kind = "hub"
        else:
            scope = fleet.credential_scope_for(row["roster_controller_id"]).get("scope")
            kind = {"operator": "hub", "fleet": "fleet"}.get(scope)
            if kind is None:
                raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
    capability = row["capability"]
    return RenderDestination(
        site_id=row["site_id"], roster_controller_id=row["roster_controller_id"],
        hub_controller_id=row["hub_controller_id"], node_id=row["node_id"],
        target_hub_controller_id=row["target_hub_controller_id"],
        capability_snapshot_sha256=hashlib.sha256(_canonical(capability)).hexdigest(),
        qualification_vector_id=row.get("qualification_vector_id"),
        capability_snapshot=capability, selected_machine=row["selected_machine"],
        accepted_unqualified=row.get("authority") == render_optin.ACCEPTED,
        hub_credential_kind=kind, encode_probe=row.get("encode_probe"),
    )


def probed_render_candidates(rows: list[dict], requested: dict, hub_client: Any, *,
                             available: bool = False,
                             requested_contract: str | None = None) -> list[dict]:
    """Resolve read-only exact permission on an already owner-gated registered route."""
    validate_render_encode(requested)
    if requested_contract not in (None, LATE_LABEL_CONTRACT, SUBTITLE_CONTRACT):
        raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
    probe_kwargs = {} if requested_contract is None else {"requested_contract": requested_contract}
    result = []
    for row in rows:
        try:
            if requested_contract == SUBTITLE_CONTRACT and not fleet.render_subtitle_support(
                    row["capability"]):
                continue
            answer = hub_client.encode_probe(destination_for_candidate(row), requested,
                                             **probe_kwargs)
            authority = execution_authority(
                answer, requested, physical_node_id=row["target_hub_controller_id"],
                capability=row["capability"], chip=row["selected_machine"]["chip"],
            )
            if authority is None:
                continue
            unqualified = authority["authority_kind"] == "accepted_unqualified"
            if unqualified:
                original = authority["grant"]["v1"]
                marker = row["capability"].get(render_optin.OPT_IN_KEY) or {}
                if (row.get("authority") != render_optin.ACCEPTED
                        or f"{row['roster_controller_id']}/{row['node_id']}" not in render_optin.enabled_keys()
                        or any(marker.get(key) != original[key] for key in (
                            "provenance", "accepted_by", "accepted_at"))):
                    continue
            memory = row["available_memory_source"]["conservative_bytes"]
            if available and (type(memory) is not int
                    or memory < authority["bounds"]["min_available_memory_bytes"]):
                continue
            capability = {**row["capability"], **{key: authority[key] for key in (
                "qualification_digest", "selected_tier", "exact_encoder", "font_support",
                "bounds", "profile_execution_bounds")},
                "encode": {"format": fleet.RENDER_ENCODE_FORMAT, "entries": [requested]}}
            capability.pop("qualification_only", None)
            capability.pop("vector_id", None)
            capability.update(production_enabled=True, production_eligible=True)
            if requested_contract == SUBTITLE_CONTRACT and not fleet.render_subtitle_support(capability):
                continue
            if not unqualified:
                capability.pop(render_optin.OPT_IN_KEY, None)
            candidate = {**row, "capability": capability, "qualification_vector_id": None,
                         "encode_probe": answer}
            if not unqualified:
                candidate.pop("authority", None)
                candidate.pop("network", None)
            result.append(candidate)
        except (RenderServiceError, ValueError, TypeError, KeyError, httpx.HTTPError) as exc:
            if requested_contract is not None:
                raise fleet.ScopedRenderCapabilitiesError(
                    "scoped encode authority could not be verified") from exc
            continue
    return fleet._render_candidates_order(result)


@dataclass
class OutputLease:
    reader: Any
    offset: int
    length: int
    total: int
    sha256: str
    status: int

    def __enter__(self) -> "OutputLease":
        self.reader.__enter__()
        self.reader.seek(self.offset)
        return self

    def chunks(self):
        left = self.length
        while left:
            piece = self.reader.read(min(_STREAM, left))
            if not piece:
                raise RenderServiceError("OUTPUT_INCOMPLETE", 503)
            left -= len(piece)
            yield piece

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.reader.__exit__(exc_type, exc, traceback)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RenderServiceError("RENDER_REQUEST_INVALID") from exc


def _text(value: Any, code: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 256:
        raise RenderServiceError(code)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise RenderServiceError(code)
    return value


def _display(value: Any) -> dict[str, str]:
    """The optional display-only labels: which channel and episode a render belongs to.

    Story Studio sends them so the Hub's Render tab can name a render instead of printing its job
    id; nothing in this service decides anything with them. Both keys are optional, a label is
    bounded and free of NUL, and non-ASCII is admitted because a channel or episode name is the
    customer's own words — the same rule the Hub applies (`render_jobs._display`), so this never
    forwards a label the Hub would refuse. Nothing beyond that rule is enforced: a display label
    must never be the reason a render is refused.
    """
    if type(value) is not dict or not value or not set(value) <= _DISPLAY_KEYS:
        raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
    for label in value.values():
        if (type(label) is not str or not label or len(label) > _DISPLAY_MAX_CHARACTERS
                or "\x00" in label):
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
    return value


def _answer_excerpt(answer: Any) -> str:
    """What the destination answered, bounded — the Hub's own words, never a credential.

    A refusal keeps the HTTP status the Hub answered with, plus whatever body the caller asked the
    client to keep (`status(..., error_body=True)`); a decoded answer is quoted from its body.
    """
    if answer is None:
        return "no answer body (the relay's reply could not be read)"
    if type(answer) is dict and answer.get("accepted") is False:
        said = "the Hub answered HTTP " + str(answer.get("http_status"))
        detail = answer.get("detail")
        if type(detail) is str and detail.strip():
            return said + ": " + detail.strip()[:_ANSWER_EXCERPT_CHARACTERS]
        return said
    try:
        body = json.dumps(answer, sort_keys=True, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        body = repr(answer)
    if len(body) > _ANSWER_EXCERPT_CHARACTERS:
        body = body[:_ANSWER_EXCERPT_CHARACTERS] + "..."
    return "body " + body


def _owned(principal: AuthenticatedPrivateRender, row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None or row.get("owner_scope") != principal.owner_scope:
        raise RenderServiceError("RENDER_NOT_FOUND", 404)
    if row.get("story_instance_id") != principal.story_instance_id:
        raise RenderServiceError("RENDER_NOT_FOUND", 404)
    return row


class RegisteredHubRenderClient:
    """Fixed relay client; destinations and credentials always come from the roster."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _origin(self, destination: RenderDestination) -> tuple[str, dict[str, str]]:
        row = next(
            (item for item in fleet.roster()
             if str(item.get("id") or "") == destination.roster_controller_id),
            None,
        )
        if destination.roster_controller_id == fleet.LOCAL_ID:
            base, token = fleet.hub_base_url() or "", fleet.hub_token() or ""
        elif row is not None:
            token = fleet._token_for(destination.roster_controller_id)
            base = str(row.get("url") or "").rstrip("/")
        else:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if not base or not token:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if destination.node_id == fleet.LOCAL_ID:
            scope = ("operator" if destination.roster_controller_id == fleet.LOCAL_ID
                     else fleet.credential_scope(base, token).get("scope"))
            if {"operator": "hub", "fleet": "fleet"}.get(scope) != destination.hub_credential_kind:
                raise RenderServiceError("RENDER_DESTINATION_CREDENTIAL_SCOPE_CHANGED", 503)
        return base, {"X-Hub-Token": token}

    def _path(self, destination: RenderDestination, suffix: str) -> str:
        if destination.node_id == fleet.LOCAL_ID:
            return "/api/hub/final-renders" + suffix
        return "/api/hub/fleet/nodes/" + quote(destination.node_id, safe="") \
            + "/final-renders" + suffix

    @staticmethod
    def _bounded(response: httpx.Response, limit: int, code: str) -> bytes:
        body = bytearray()
        for piece in response.iter_bytes():
            if len(body) + len(piece) > limit:
                raise RenderServiceError(code, 503)
            body.extend(piece)
        return bytes(body)

    @staticmethod
    def _excerpt(response: httpx.Response) -> str:
        """The first `_ANSWER_EXCERPT_CHARACTERS` of a refusal body; never an error of its own."""
        body = bytearray()
        for piece in response.iter_bytes():
            body.extend(piece)
            if len(body) >= _ANSWER_EXCERPT_CHARACTERS:
                break
        return bytes(body[:_ANSWER_EXCERPT_CHARACTERS]).decode("utf-8", "replace")

    def json(self, destination: RenderDestination, method: str, suffix: str, *,
             body: bytes | None = None, headers: dict[str, str] | None = None,
             error_body: bool = False) -> dict[str, Any]:
        base, auth = self._origin(destination)
        request_headers = dict(auth)
        request_headers.update(headers or {})
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            with client.stream(method, base + self._path(destination, suffix),
                               content=body, headers=request_headers) as response:
                if response.status_code >= 400:
                    answer: dict[str, Any] = {
                        "accepted": False, "http_status": response.status_code,
                    }
                    if error_body:
                        # The Hub's own words about why it refused, bounded. Its shape is opt-in
                        # because callers elsewhere compare `{"accepted": False, "http_status"}`
                        # exactly; a render that cannot be polled is the case that needs a reason.
                        answer["detail"] = self._excerpt(response)
                    return answer
                raw = self._bounded(response, _MAX_JSON, "HUB_RESPONSE_UNKNOWN")
        try:
            decoded = decode_render_json(raw)
        except ValueError:
            raise RenderServiceError("HUB_RESPONSE_UNKNOWN", 503) from None
        if type(decoded) is not dict:
            raise RenderServiceError("HUB_RESPONSE_UNKNOWN", 503)
        return decoded

    def encode_probe(self, destination: RenderDestination, requested: dict[str, Any], *,
                     start: bool = False,
                     requested_contract: str | None = None) -> dict[str, Any]:
        validate_render_encode(requested)
        if type(start) is not bool:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
        if requested_contract not in (None, LATE_LABEL_CONTRACT, SUBTITLE_CONTRACT):
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
        contract_query = ({} if requested_contract is None
                          else {"requested_contract": requested_contract})
        if start:
            suffix = "/encode-probes"
            if contract_query:
                suffix += "?" + urlencode(contract_query)
            body = _canonical({"format": "studio.render.encode-probe-request.v1",
                               "requested_encode": requested})
            return self.json(destination, "POST", suffix, body=body,
                             headers={"Content-Type": "application/json",
                                      "Content-Length": str(len(body))})
        query = {"requested_encode": _canonical(requested).decode("ascii")}
        query.update(contract_query)
        return self.json(destination, "GET", "/encode-probes?" + urlencode(query))

    def blob_status(self, destination: RenderDestination, sha256: str,
                    size_bytes: int) -> dict[str, Any]:
        return self.json(
            destination, "GET", "/blobs/" + quote(sha256, safe="")
            + "/status?size_bytes=" + str(size_bytes),
        )

    def put_blob(self, destination: RenderDestination, sha256: str, offset: int,
                 total: int, data: bytes, range_sha256: str) -> dict[str, Any]:
        headers = {
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{total}",
            "X-Content-SHA256": range_sha256,
        }
        return self.json(destination, "PUT", "/blobs/" + quote(sha256, safe=""),
                         body=data, headers=headers)

    def submit(self, destination: RenderDestination, key: str, body: bytes) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                   "Idempotency-Key": key}
        if destination.qualification_vector_id:
            headers["X-Studio-Render-Qualification-Vector"] = (
                destination.qualification_vector_id
            )
        return self.json(destination, "POST", "", body=body, headers=headers)

    def by_key(self, destination: RenderDestination, key: str) -> dict[str, Any]:
        return self.json(destination, "GET", "/by-idempotency-key/" + quote(key, safe=""))

    def status(self, destination: RenderDestination, hub_job_id: str, *,
               error_body: bool = False) -> dict[str, Any]:
        return self.json(destination, "GET", "/" + quote(hub_job_id, safe=""),
                         error_body=error_body)

    def cancel_by_key(self, destination: RenderDestination, key: str, body: bytes) -> dict[str, Any]:
        return self.json(destination, "POST", "/by-idempotency-key/"
                         + quote(key, safe="") + "/cancel",
                         body=body, headers={"Content-Type": "application/json",
                                             "Content-Length": str(len(body))})

    def cancel(self, destination: RenderDestination, hub_job_id: str, body: bytes) -> dict[str, Any]:
        return self.json(destination, "POST", "/" + quote(hub_job_id, safe="") + "/cancel",
                         body=body,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(body))})

    def output_range(self, destination: RenderDestination, hub_job_id: str,
                     offset: int, length: int,
                     if_match: str) -> tuple[bytes, dict[str, str]]:
        base, headers = self._origin(destination)
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        headers["If-Match"] = if_match
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            with client.stream(
                "GET",
                base + self._path(destination, "/" + quote(hub_job_id, safe="") + "/output"),
                headers=headers,
            ) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if (response.status_code != 206
                        or response_headers.get("content-length") != str(length)):
                    raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
                data = self._bounded(response, length, "HUB_OUTPUT_UNKNOWN")
        if len(data) != length:
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        return data, response_headers

    def ack(self, destination: RenderDestination, hub_job_id: str, key: str,
            body: bytes) -> dict[str, Any]:
        return self.json(destination, "POST", "/" + quote(hub_job_id, safe="") + "/ack",
                         body=body,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(body)),
                                  "Idempotency-Key": key})


class FinalRenderService:
    def __init__(
        self,
        store: JobStore,
        assets: RenderAssetStore,
        *,
        destination_selector: Callable[[dict[str, Any]], RenderDestination],
        hub_client: Any,
        key_configured: Callable[[], bool],
        profile_descriptor: bytes | None = None,
        profile_facts: dict[str, Any] | None = None,
        late_label_profile_descriptor: bytes | None = None,
        late_label_profile_facts: dict[str, Any] | None = None,
        subtitle_profile_descriptor: bytes | None = None,
        subtitle_profile_facts: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.assets = assets
        self.destination_selector = destination_selector
        self.hub = hub_client
        self.profile_descriptor = profile_descriptor
        self.profile_facts = profile_facts or {}
        self.key_configured = key_configured
        self.profile = decode_render_json(profile_descriptor) if profile_descriptor is not None else None
        self.profile_digest = (
            hashlib.sha256(profile_descriptor).hexdigest() if profile_descriptor is not None else None
        )
        self.late_label_profile_descriptor = late_label_profile_descriptor
        self.late_label_profile_facts = late_label_profile_facts or {}
        self.late_label_profile = (decode_render_json(late_label_profile_descriptor)
                                   if late_label_profile_descriptor is not None else None)
        self.late_label_profile_digest = (hashlib.sha256(late_label_profile_descriptor).hexdigest()
                                         if late_label_profile_descriptor is not None else None)
        self.subtitle_profile_descriptor = subtitle_profile_descriptor
        self.subtitle_profile_facts = subtitle_profile_facts or {}
        self.subtitle_profile = (decode_render_json(subtitle_profile_descriptor)
                                 if subtitle_profile_descriptor is not None else None)
        self.subtitle_profile_digest = (hashlib.sha256(subtitle_profile_descriptor).hexdigest()
                                       if subtitle_profile_descriptor is not None else None)
        self._lock = threading.Lock()
        self._active_ids: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._stopping = False
        self._dispatch_cursor: tuple[float, str] | None = None
        self._last_sweep = 0.0
        self._sweep_thread: threading.Thread | None = None
        #: `render id -> consecutive answers this Gateway could not accept`. In memory on purpose:
        #: a restart re-asks the destination, and a process whose answers stayed unacceptable
        #: reaches the bound again in seconds.
        self._status_rejections: dict[str, int] = {}

    def _fresh_send_allowed(self, row: dict[str, Any]) -> bool:
        """New Hub I/O needs the API key still configured and this very installation."""
        try:
            installation = self.store.render_installation()
            if row.get("destination_encode_probe") is not None:
                probe = decode_render_json(bytes(row["destination_encode_probe"]))
                if (probe["execution_authority"]["authority_kind"] == "accepted_unqualified"
                        and f"{row['destination_controller']}/{row['destination_node']}"
                        not in render_optin.enabled_keys()):
                    return False
            return (
                self.key_configured() is True
                and installation["owner_scope"] == row["owner_scope"]
                and installation["gateway_instance_id"] == row["gateway_instance_id"]
            )
        except Exception:
            return False

    def capabilities(self, principal: AuthenticatedPrivateRender, *,
                     requested_encode: dict[str, Any] | None = None,
                     requested_contract: str | None = None) -> dict[str, Any]:
        descriptor, profile_digest, _profile, facts = self._profile_for_contract(requested_contract)
        if requested_encode is not None:
            validate_render_encode(requested_encode)
        # Legacy call shapes stay byte-identical: the kwarg exists only when scoped.
        scoped = {} if requested_contract is None else {"requested_contract": requested_contract}
        try:
            candidates = (fleet.render_candidates(
                profile_digest, qualification=True, available=False,
                resolve_encode_authority=True, **scoped) if requested_encode is not None else
                fleet.render_candidates(profile_digest, qualification=True, available=False,
                                        **scoped))
            idle = (fleet.render_candidates(profile_digest, qualification=True,
                                             resolve_encode_authority=True, **scoped)
                    if requested_encode is not None else
                    fleet.render_candidates(profile_digest, qualification=True, **scoped))
            if requested_encode is not None:
                candidates = probed_render_candidates(candidates, requested_encode, self.hub,
                                                      **scoped)
                if requested_contract == SUBTITLE_CONTRACT:
                    candidates = [row for row in candidates
                                  if fleet.render_subtitle_support(row["capability"])]
                supported = {(row["site_id"], row["roster_controller_id"], row["node_id"]): row
                             for row in candidates}
                idle = [row for row in idle
                        if (row["site_id"], row["roster_controller_id"], row["node_id"]) in supported
                        and (requested_contract != SUBTITLE_CONTRACT
                             or fleet.render_subtitle_support(row["capability"]))
                        and type(row["available_memory_source"]["conservative_bytes"]) is int
                        and row["available_memory_source"]["conservative_bytes"] >= supported[
                            (row["site_id"], row["roster_controller_id"], row["node_id"])
                        ]["capability"]["bounds"]["min_available_memory_bytes"]]
                encode = {"format": fleet.RENDER_ENCODE_FORMAT,
                          "entries": [requested_encode] if candidates else []}
            else:
                if requested_contract == SUBTITLE_CONTRACT:
                    candidates = [row for row in candidates
                                  if fleet.render_subtitle_support(row["capability"])]
                    supported = {(row["site_id"], row["roster_controller_id"], row["node_id"])
                                 for row in candidates}
                    idle = [row for row in idle
                            if (row["site_id"], row["roster_controller_id"], row["node_id"]) in supported
                            and fleet.render_subtitle_support(row["capability"])]
                encode = fleet.render_encode_intersection(candidates)
        except Exception:
            if requested_contract is not None:
                raise PrivateRenderUnavailable() from None
            candidates, idle, encode = [], [], None
        answer: dict[str, Any] = {
            "format": "studio.render.gateway-capabilities.v1",
            "private": True, "chargeable": False,
            "owner_scope": principal.owner_scope,
            "gateway_instance_id": principal.gateway_instance_id,
            "story_instance_id": principal.story_instance_id,
            "eligible": len(candidates),
            "idle": len(idle),
            # Keep the legacy unscoped intersection; support does not depend on free slots.
            "encode": encode,
            "server_time": int(time.time()),
        }
        if requested_encode is not None:
            answer["requested_encode"] = requested_encode
        answer.update({
            "profile_descriptor_utf8": descriptor.decode("utf-8"),
            "profile_digest": profile_digest,
            "tools": facts.get("tools"),
            "compiler": facts.get("compiler"),
        })
        if requested_contract == LATE_LABEL_CONTRACT:
            answer["late_labels"] = {"format": LATE_LABEL_CONTRACT,
                                     "motions": list(LATE_LABEL_MOTIONS)}
        elif requested_contract == SUBTITLE_CONTRACT and candidates:
            answer["subtitles"] = {"format": SUBTITLE_CONTRACT}
            if all(fleet.render_subtitle_support(row["capability"], late_labels=True)
                   for row in candidates):
                answer["late_labels"] = {"format": LATE_LABEL_CONTRACT,
                                         "motions": list(LATE_LABEL_MOTIONS)}
        return answer

    def _profile_for_contract(self, contract: str | None) -> tuple:
        if contract is None:
            self._require_profile()
            return self.profile_descriptor, self.profile_digest, self.profile, self.profile_facts
        if contract == SUBTITLE_CONTRACT:
            if (self.subtitle_profile_descriptor is None
                    or type(self.subtitle_profile) is not dict):
                raise PrivateRenderUnavailable()
            return (self.subtitle_profile_descriptor, self.subtitle_profile_digest,
                    self.subtitle_profile, self.subtitle_profile_facts)
        if contract != LATE_LABEL_CONTRACT:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
        if (self.late_label_profile_descriptor is None
                or type(self.late_label_profile) is not dict):
            raise PrivateRenderUnavailable()
        return (self.late_label_profile_descriptor, self.late_label_profile_digest,
                self.late_label_profile, self.late_label_profile_facts)

    def _packet_profile(self, packet: dict[str, Any]) -> tuple:
        if type(packet) is not dict or type(packet.get("recipe")) is not dict:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
        digest = packet["recipe"].get("profile_digest")
        if digest == self.subtitle_profile_digest and digest is not None:
            return SUBTITLE_CONTRACT, self._profile_for_contract(SUBTITLE_CONTRACT)
        if digest == self.late_label_profile_digest and digest is not None:
            return LATE_LABEL_CONTRACT, self._profile_for_contract(LATE_LABEL_CONTRACT)
        if digest == self.profile_digest and digest is not None:
            return None, self._profile_for_contract(None)
        raise RenderServiceError("RENDER_PROFILE_MISMATCH", 409)

    def _require_profile(self) -> None:
        if self.profile_descriptor is None or type(self.profile) is not dict:
            raise PrivateRenderUnavailable()

    def _require_packet_bounds(self, packet: dict[str, Any], profile: dict | None = None) -> None:
        bounds = ((self.profile if profile is None else profile) or {}).get("execution_bounds")
        recipe = packet.get("recipe") or {}
        manifest = packet.get("manifest") or []
        canvas = recipe.get("canvas") or {}
        duration = recipe.get("target_duration_sec")
        width, height = canvas.get("width"), canvas.get("height")
        canvas_allowed = False
        if type(bounds) is dict:
            if "bounds_version" in bounds:
                limits = bounds.get("canvas_range") or {}
                canvas_allowed = (
                    type(bounds["bounds_version"]) is int and bounds["bounds_version"] == 2
                    and type(width) is int and type(height) is int
                    and width > 0 and height > 0 and width % 2 == height % 2 == 0
                    and max(width, height) <= limits.get("max_edge", 0)
                    and width * height <= limits.get("max_pixels", 0)
                )
            else:
                canvas_allowed = {"width": width, "height": height} in bounds.get("canvas_pairs", [])
        if (type(bounds) is not dict
                or not canvas_allowed
                or canvas.get("working_fps") not in bounds.get("working_fps", [])
                or type(duration) not in (int, float)
                or duration > bounds.get("max_duration_sec", 0)
                or len(recipe.get("operations") or []) > bounds.get("max_operations", 0)
                or len(manifest) > bounds.get("max_assets", 0)
                or any(item["size_bytes"] > bounds.get("max_asset_bytes", 0)
                       for item in manifest)
                or sum(item["size_bytes"] for item in manifest)
                    > bounds.get("max_total_input_bytes", 0)
                or canvas.get("width", 0) * canvas.get("height", 0)
                    * math.ceil(canvas.get("working_fps", 0) * duration)
                    > bounds.get("max_pixel_frames", 0)):
            raise RenderServiceError("RENDER_PROFILE_MISMATCH", 409)

    def _require_cue_assets(self, principal: AuthenticatedPrivateRender,
                            packet: dict, handles: dict) -> None:
        canvas = packet["recipe"]["canvas"]
        dimensions: dict[str, tuple[int, int]] = {}
        for entry in packet["manifest"]:
            if entry["role"] != "cue_label":
                continue
            handle = handles[entry["logical_id"]]
            if handle not in dimensions:
                try:
                    with self.assets.open_reader(principal.owner_scope, handle) as reader:
                        dimensions[handle] = validate_cue_png(
                            reader, max_width=canvas["width"], max_height=canvas["height"])
                except (ValueError, OSError, AssetRecoveryRequiredError):
                    raise RenderServiceError("RENDER_CUE_ASSET_INVALID", 422) from None
        for op in packet["recipe"]["operations"]:
            if op["kind"] != "late_label_stage":
                continue
            label_refs = ([op["label"]["asset"]] if op["label"] is not None else
                          [frame["asset"] for frame in op["reveal"]["frames"]])
            sizes = [dimensions[handles[ref["logical_id"]]] for ref in label_refs]
            if (any(size != sizes[0] for size in sizes)
                    or op["placement"]["x_px"] + sizes[0][0] > canvas["width"]
                    or op["placement"]["y_px"] + sizes[0][1] > canvas["height"]):
                raise RenderServiceError("RENDER_CUE_ASSET_INVALID", 422)
            line_refs = ([op["line"]["asset"]] if op["line"] is not None else
                         [frame["asset"] for frame in op["line_reveal"]["frames"]]
                         if op["line_reveal"] is not None else [])
            if any(dimensions[handles[ref["logical_id"]]] != (canvas["width"], canvas["height"])
                   for ref in line_refs):
                raise RenderServiceError("RENDER_CUE_ASSET_INVALID", 422)

    def _require_subtitle_assets(self, principal: AuthenticatedPrivateRender,
                                 packet: dict, handles: dict) -> None:
        from .render_subtitles import validate_subtitle_ass, validate_subtitle_font
        entries = {item["logical_id"]: item for item in packet["manifest"]}
        recipe = packet["recipe"]
        try:
            for operation in recipe["operations"]:
                if operation["kind"] != "subtitle_stage":
                    continue
                families = []
                for font in operation["fonts"]:
                    logical_id = font["asset"]["logical_id"]
                    entry = entries[logical_id]
                    with self.assets.open_reader(principal.owner_scope, handles[logical_id]) as reader:
                        validate_subtitle_font(reader, max_bytes=entry["size_bytes"],
                                               expected_sha256=entry["sha256"], family=font["family"])
                    families.append(font["family"])
                logical_id = operation["ass"]["logical_id"]
                entry = entries[logical_id]
                with self.assets.open_reader(principal.owner_scope, handles[logical_id]) as reader:
                    validate_subtitle_ass(reader, max_bytes=entry["size_bytes"],
                                          expected_sha256=entry["sha256"], canvas=recipe["canvas"],
                                          duration=recipe["target_duration_sec"], families=families)
        except (ValueError, OSError, AssetRecoveryRequiredError):
            raise RenderServiceError("RENDER_SUBTITLE_ASSET_INVALID", 422) from None

    def blob_status(self, principal: AuthenticatedPrivateRender, sha256: str,
                    size_bytes: int) -> dict[str, Any]:
        try:
            status = self.assets.blob_status(principal.owner_scope, sha256, size_bytes)
            return self._blob_public(sha256, size_bytes, status)
        except AssetNotFoundError:
            return self._blob_public(sha256, size_bytes, {"state": "missing", "offset": 0})
        except (AssetValidationError, AssetConflictError, AssetRecoveryRequiredError) as exc:
            raise RenderServiceError("BLOB_INVALID", 409) from exc

    async def put_blob(self, principal: AuthenticatedPrivateRender, sha256: str,
                       size_bytes: int, offset: int, length: int, body: Any) -> dict[str, Any]:
        self._require_profile()
        if (type(size_bytes) is not int or type(offset) is not int or type(length) is not int
                or size_bytes <= 0 or offset < 0 or length <= 0 or length > _CHUNK
                or offset > size_bytes or length > size_bytes - offset):
            raise RenderServiceError("BLOB_RANGE_INVALID", 422)
        try:
            status = self.assets.blob_status(principal.owner_scope, sha256, size_bytes)
            if status["state"] == "missing":
                upload = self.assets.begin_upload(principal.owner_scope, sha256, size_bytes)
                upload_id, expected = upload["upload_id"], 0
            else:
                upload_id, expected = status["upload_id"], status["offset"]
            if expected != offset:
                raise RenderServiceError("BLOB_OFFSET_CONFLICT", 409)
            committed = await self.assets.append_async_chunk(
                principal.owner_scope, upload_id, offset, body, length
            )
            if committed == size_bytes:
                complete = self.assets.finish_upload(principal.owner_scope, upload_id)
                return self._blob_public(sha256, size_bytes, {
                    "state": "complete", "offset": committed,
                    "upload_id": upload_id, "asset_handle": complete["asset_handle"],
                })
            return self._blob_public(sha256, size_bytes, {
                "state": "uploading", "offset": committed, "upload_id": upload_id,
                "asset_handle": None,
            })
        except RenderServiceError:
            raise
        except (AssetValidationError, AssetConflictError, AssetRecoveryRequiredError) as exc:
            raise RenderServiceError("BLOB_CONFLICT", 409) from exc

    def accept(self, principal: AuthenticatedPrivateRender, gateway_attempt_key: str,
               raw_body: bytes) -> dict[str, Any]:
        key = _text(gateway_attempt_key, "IDEMPOTENCY_KEY_INVALID", pattern=_KEY)
        if type(raw_body) is not bytes or not raw_body or len(raw_body) > _MAX_JSON:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422)
        digest = hashlib.sha256(raw_body).hexdigest()
        existing = self.store.get_render_staging_by_attempt_key(
            owner_scope=principal.owner_scope, gateway_attempt_key=key
        )
        if existing is not None:
            if (existing["inbound_story_body_sha256"] != digest
                    or bytes(existing["inbound_story_body"]) != raw_body
                    or existing["story_instance_id"] != principal.story_instance_id):
                raise RenderServiceError("IDEMPOTENCY_KEY_CONFLICT", 409)
            existing["idempotent_replay"] = True
            return self._accepted(existing)
        self._require_profile()
        try:
            request = decode_render_json(raw_body)
            if (type(request) is not dict
                    or frozenset(request) - {"display"} != _REQUEST_KEYS):
                raise ValueError
            if request["format"] != "studio.render.gateway-request.v1":
                raise ValueError
            if "display" in request:
                _display(request["display"])
            story_uuid = _text(request["story_uuid"], "RENDER_REQUEST_INVALID", pattern=_UUID)
            intent_id = _text(request["intent_id"], "RENDER_REQUEST_INVALID", pattern=_KEY)
            if type(request["attempt_no"]) is not int or not 1 <= request["attempt_no"] <= 2**31 - 1:
                raise ValueError
            packet_raw = _canonical(request["packet"])
            contract, (_, profile_digest, profile, profile_facts) = self._packet_profile(request["packet"])
            packet = (validate_render_packet_syntax(packet_raw, contract=contract)
                      if contract is not None else validate_render_packet_syntax(packet_raw))
            self._require_packet_bounds(packet, profile)
            handles = request["assets"]
            if type(handles) is not dict or set(handles) != {
                    item["logical_id"] for item in packet["manifest"]}:
                raise ValueError
        except RenderServiceError:
            raise
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RenderServiceError("RENDER_REQUEST_INVALID", 422) from exc
        staged = []
        try:
            for manifest in packet["manifest"]:
                logical_id = manifest["logical_id"]
                asset = self.assets.describe(principal.owner_scope, handles[logical_id])
                if (asset["sha256"], asset["size_bytes"]) != (
                        manifest["sha256"], manifest["size_bytes"]):
                    raise RenderServiceError("RENDER_ASSET_CONFLICT", 409)
                staged.append({"logical_id": logical_id,
                               "gateway_asset_handle": handles[logical_id],
                               "sha256": asset["sha256"], "size_bytes": asset["size_bytes"]})
        except AssetNotFoundError as exc:
            raise RenderServiceError("RENDER_ASSET_NOT_FOUND", 404) from exc
        if contract is not None:
            self._require_cue_assets(principal, packet, handles)
        if contract == SUBTITLE_CONTRACT:
            self._require_subtitle_assets(principal, packet, handles)
        destination = self.destination_selector(packet)
        if not isinstance(destination, RenderDestination):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if type(destination.capability_snapshot) is not dict:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if contract == SUBTITLE_CONTRACT and not fleet.render_subtitle_support(
                destination.capability_snapshot,
                late_labels=any(op["kind"] == "late_label_stage"
                                for op in packet["recipe"]["operations"])):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        # The compiler-derived encode fact is optional (an older Hub omits it) and `None`
        # is its "nothing to describe"; anything else must be the exact closed document.
        if ("encode" in destination.capability_snapshot
                and not fleet.render_encode_document(
                    destination.capability_snapshot["encode"])):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        qualification_only = destination.capability_snapshot.get("qualification_only") is True
        qualification_capacity = destination.capability_snapshot.get("available_capacity")
        if (qualification_only and (
                    set(destination.capability_snapshot)
                        not in (_QUALIFICATION_KEYS, _QUALIFICATION_KEYS | {"encode"})
                    or destination.qualification_vector_id is None
                    or destination.capability_snapshot.get("vector_id")
                        != destination.qualification_vector_id
                    or type(qualification_capacity) is not dict
                    or set(qualification_capacity) != {"slots", "slots_total"}
                    or type(qualification_capacity.get("slots")) is not int
                    or type(qualification_capacity.get("slots_total")) is not int
                    or qualification_capacity["slots"] != 1
                    or qualification_capacity["slots_total"] != 1
                    or destination.capability_snapshot.get("production_eligible") is not False
                    or destination.capability_snapshot.get("profile_digest")
                        != profile_digest
                    or destination.capability_snapshot.get("tools_digest")
                        != (profile_facts.get("tools") or {}).get("digest")
                    or destination.capability_snapshot.get("compiler_digest")
                        != (profile_facts.get("compiler") or {}).get("digest")
                )
                or not qualification_only and (
                    destination.qualification_vector_id is not None
                    or destination.capability_snapshot.get("production_eligible") is not True
                    or destination.capability_snapshot.get("production_enabled") is not True
                    # The Hub's own unqualified evidence and the Gateway's choice must agree exactly: a
                    # row that carries it is only ever routed on the owner's say-so, and a destination
                    # that claims it must carry it.
                    or render_optin.unqualified_claim(destination.capability_snapshot)
                    != ("accepted" if destination.accepted_unqualified else "none")
                )
                or destination.capability_snapshot.get("profile_execution_bounds")
                    != profile.get("execution_bounds")
                or destination.capability_snapshot.get("profile_digest") != profile_digest
                or destination.capability_snapshot.get("tools_digest")
                    != (profile_facts.get("tools") or {}).get("digest")
                or destination.capability_snapshot.get("compiler_digest")
                    != (profile_facts.get("compiler") or {}).get("digest")
                or type((destination.capability_snapshot.get("bounds") or {}).get(
                    "output_limit_bytes"
                )) is not int
                or destination.capability_snapshot["bounds"]["output_limit_bytes"] <= 0):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if (type(destination.selected_machine) is not dict
                or set(destination.selected_machine) != {
                    "id", "label", "chip", "memory_gb", "hardware_class"
                }
                or destination.selected_machine.get("id") != destination.node_id
                or any(type(destination.selected_machine.get(key)) is not str
                       or not destination.selected_machine[key]
                       or destination.selected_machine[key] != destination.selected_machine[key].strip()
                       or len(destination.selected_machine[key]) > 256
                       for key in ("id", "label", "chip", "hardware_class"))
                or type(destination.selected_machine.get("memory_gb")) not in (int, float)
                or isinstance(destination.selected_machine.get("memory_gb"), bool)
                or not math.isfinite(destination.selected_machine["memory_gb"])
                or destination.selected_machine["memory_gb"] <= 0):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        known_encode = destination.capability_snapshot.get("encode")
        if (known_encode is not None or not qualification_only) and not fleet.render_encode_supports(
                known_encode, packet["recipe"]["encode"]):
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        if destination.encode_probe is not None:
            try:
                authority = execution_authority(
                    destination.encode_probe, packet["recipe"]["encode"],
                    physical_node_id=destination.target_hub_controller_id or destination.node_id,
                    capability=destination.capability_snapshot,
                    chip=destination.selected_machine["chip"],
                )
                if (authority is None or known_encode is None
                        or any(destination.capability_snapshot.get(key) != authority[key] for key in (
                            "qualification_digest", "selected_tier", "exact_encoder", "bounds",
                            "font_support", "profile_execution_bounds"))
                        or destination.accepted_unqualified != (
                            authority["authority_kind"] == "accepted_unqualified")
                        or destination.accepted_unqualified and
                            f"{destination.roster_controller_id}/{destination.node_id}"
                            not in render_optin.enabled_keys()):
                    raise ValueError("encode permission differs from selected destination")
                if destination.accepted_unqualified:
                    marker = destination.capability_snapshot[render_optin.OPT_IN_KEY]
                    if any(marker[key] != authority["grant"]["v1"][key] for key in (
                            "provenance", "accepted_by", "accepted_at")):
                        raise ValueError("encode grant differs from original owner authority")
            except (ValueError, TypeError, KeyError):
                raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503) from None
        try:
            capability_snapshot = _canonical(destination.capability_snapshot)
            selected_machine = _canonical(destination.selected_machine)
        except RenderServiceError:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503) from None
        if hashlib.sha256(capability_snapshot).hexdigest() != destination.capability_snapshot_sha256:
            raise RenderServiceError("RENDER_DESTINATION_UNAVAILABLE", 503)
        try:
            created = self.store.create_render_staging(
                render_id=str(uuid.uuid4()),
                gateway_instance_id=principal.gateway_instance_id,
                story_instance_id=principal.story_instance_id, owner_scope=principal.owner_scope,
                story_uuid=story_uuid, intent_id=intent_id, attempt_no=request["attempt_no"],
                gateway_attempt_key=key, inbound_story_body=raw_body,
                inbound_story_body_sha256=digest, packet_sha256=packet["packet_digest"],
                destination_site=destination.site_id,
                destination_controller=destination.roster_controller_id,
                destination_hub_controller=(
                    destination.target_hub_controller_id or destination.node_id
                ),
                destination_node=destination.node_id,
                destination_credential_kind=destination.hub_credential_kind,
                destination_encode_probe=(_canonical(destination.encode_probe)
                                          if destination.encode_probe is not None else None),
                selected_machine=selected_machine,
                capability_snapshot=capability_snapshot,
                capability_snapshot_sha256=destination.capability_snapshot_sha256,
                assets=staged,
            )
            return self._accepted(created)
        except RenderJournalCancelled as exc:
            raise RenderServiceError("RENDER_CANCELLED_BEFORE_ACCEPTANCE", 409) from exc
        except RenderJournalConflict as exc:
            raise RenderServiceError("IDEMPOTENCY_KEY_CONFLICT", 409) from exc

    def by_key(self, principal: AuthenticatedPrivateRender, key: str) -> dict[str, Any] | None:
        key = _text(key, "IDEMPOTENCY_KEY_INVALID", pattern=_KEY)
        row = self.store.get_render_staging_by_attempt_key(
            owner_scope=principal.owner_scope, gateway_attempt_key=key
        )
        if row is not None and row.get("story_instance_id") != principal.story_instance_id:
            return None
        return self._public(row) if row else None

    def status(self, principal: AuthenticatedPrivateRender, gateway_job_id: str) -> dict[str, Any]:
        return self._public(_owned(principal, self.store.get_render_staging(gateway_job_id)))

    def request_cancel_by_key(
        self, principal: AuthenticatedPrivateRender, key: str,
        expected_request_sha256: str,
    ) -> dict[str, Any]:
        key = _text(key, "IDEMPOTENCY_KEY_INVALID", pattern=_KEY)
        if (type(expected_request_sha256) is not str
                or _HEX64.fullmatch(expected_request_sha256) is None):
            raise RenderServiceError("CANCEL_REQUEST_IDENTITY_REQUIRED", 422)
        row = self.store.get_render_staging_by_attempt_key(
            owner_scope=principal.owner_scope, gateway_attempt_key=key
        )
        if row is not None:
            row = _owned(principal, row)
            if expected_request_sha256 != row["inbound_story_body_sha256"]:
                raise RenderServiceError("CANCEL_REQUEST_CONFLICT", 409)
        try:
            result = self.store.request_render_cancel_by_key(
                owner_scope=principal.owner_scope, dispatch_key=key,
                expected_request_sha256=expected_request_sha256,
            )
        except RenderJournalConflict as exc:
            raise RenderServiceError("CANCEL_REQUEST_CONFLICT", 409) from exc
        return self._cancel_public(
            result, gateway_attempt_key=key,
            expected_request_sha256=expected_request_sha256,
        )

    def request_cancel(self, principal: AuthenticatedPrivateRender,
                       gateway_job_id: str) -> dict[str, Any]:
        row = _owned(principal, self.store.get_render_staging(gateway_job_id))
        return self.request_cancel_by_key(
            principal, row["gateway_attempt_key"], row["inbound_story_body_sha256"]
        )

    def open_output(self, principal: AuthenticatedPrivateRender, gateway_job_id: str,
                    range_header: str | None, if_match: str | None) -> OutputLease:
        row = _owned(principal, self.store.get_render_staging(gateway_job_id))
        delivery = self.store.get_render_delivery(row["id"])
        if not delivery or not delivery.get("output_asset_handle"):
            raise RenderServiceError("OUTPUT_NOT_READY", 409)
        descriptor = self.assets.describe(principal.owner_scope, delivery["output_asset_handle"])
        total = descriptor["size_bytes"]
        if if_match != '"' + descriptor["sha256"] + '"':
            raise RenderServiceError("OUTPUT_PRECONDITION_FAILED", 412)
        if range_header is None:
            raise RenderServiceError("OUTPUT_RANGE_REQUIRED", 416)
        if len(range_header) > 128:
            raise RenderServiceError("OUTPUT_RANGE_INVALID", 416)
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
        if match is None:
            raise RenderServiceError("OUTPUT_RANGE_INVALID", 416)
        offset, end = int(match.group(1)), int(match.group(2))
        if offset >= total or end < offset or end >= total or end - offset + 1 > _CHUNK:
            raise RenderServiceError("OUTPUT_RANGE_INVALID", 416)
        reader = self.assets.open_reader(principal.owner_scope, delivery["output_asset_handle"])
        return OutputLease(reader, offset, end - offset + 1, total,
                           descriptor["sha256"], 206)

    def acknowledge_output(self, principal: AuthenticatedPrivateRender, gateway_job_id: str,
                           expected_receipt_digest: str) -> dict[str, Any]:
        if (type(expected_receipt_digest) is not str
                or _HEX64.fullmatch(expected_receipt_digest) is None):
            raise RenderServiceError("OUTPUT_RECEIPT_INVALID", 422)
        row = _owned(principal, self.store.get_render_staging(gateway_job_id))
        delivery = self.store.get_render_delivery(row["id"])
        if (not delivery or delivery.get("gateway_receipt_digest") != expected_receipt_digest
                or not delivery.get("output_asset_handle")
                or delivery.get("ack_outcome") != "accepted"):
            raise RenderServiceError("OUTPUT_RECEIPT_CONFLICT", 409)
        self.store.acknowledge_render_story(
            row["id"], receipt_digest=expected_receipt_digest
        )
        self.assets.release_reference(principal.owner_scope, delivery["output_asset_handle"],
                                      "render-job:" + row["id"])
        return {"format": "studio.render.gateway-ack.v1",
                "id": row["id"], "acknowledged": True,
                "receipt_digest": expected_receipt_digest}

    def _destination(self, row: dict[str, Any]) -> RenderDestination:
        capability = decode_render_json(bytes(row["capability_snapshot"]))
        return RenderDestination(
            row["destination_site"], row["destination_controller"],
            row["destination_hub_controller"], row["destination_node"],
            row["capability_snapshot_sha256"],
            (capability.get("vector_id")
             if capability.get("qualification_only") is True else None),
            capability,
            decode_render_json(bytes(row["selected_machine"])),
            row["destination_hub_controller"],
            render_optin.authority_of(capability) == render_optin.ACCEPTED,
            row["destination_credential_kind"],
            (decode_render_json(bytes(row["destination_encode_probe"]))
             if row.get("destination_encode_probe") is not None else None),
        )

    def _hub_job(self, row: dict[str, Any], answer: Any) -> dict[str, Any]:
        try:
            capability = decode_render_json(bytes(row["capability_snapshot"]))
        except ValueError:
            raise RenderServiceError("HUB_RESPONSE_UNKNOWN", 503) from None
        expected_owner = _hub_owner_scope(row)
        expected_purpose = (
            "qualification" if capability.get("qualification_only") is True else "production"
        )
        delivery = self.store.get_render_delivery(row["id"]) or {}
        leases = answer.get("leases") if type(answer) is dict else None
        binding = _stored_encode_binding(row)
        expected_keys = _HUB_JOB_KEYS | ({"encode_authority"} if binding is not None else set())
        if (type(answer) is not dict or frozenset(answer) != expected_keys
                or binding is not None and answer.get("encode_authority") != binding
                or type(answer.get("id")) is not str or _UUID.fullmatch(answer["id"]) is None
                or row.get("hub_job_id") is not None
                    and answer.get("id") != row["hub_job_id"]
                or answer.get("owner_scope") != expected_owner
                or answer.get("dispatch_key") != row.get("dispatch_key")
                or answer.get("accepted_request_sha256")
                    != row.get("outbound_hub_body_sha256")
                or answer.get("purpose") != expected_purpose
                or answer.get("state") not in _REMOTE_STATES
                or answer.get("machine") not in (
                    None, row["destination_node"], _HUB_SELF_LABEL
                )
                or answer.get("dispatch_generation") is not None
                    and (type(answer["dispatch_generation"]) is not int
                         or answer["dispatch_generation"] <= 0)
                or answer.get("attempt_id") is not None
                    and (type(answer["attempt_id"]) is not str
                         or re.fullmatch(r"[0-9a-f]{32}", answer["attempt_id"]) is None)
                or delivery.get("remote_attempt_id") is not None
                    and answer.get("attempt_id") != delivery["remote_attempt_id"]
                or delivery.get("stopped_evidence_digest") is not None
                    and answer.get("stopped_evidence_digest")
                        != delivery["stopped_evidence_digest"]
                or answer.get("profile_digest") != capability.get("profile_digest")
                or answer.get("tools_digest") != capability.get("tools_digest")
                or answer.get("compiler_digest") != capability.get("compiler_digest")
                or answer.get("qualification_digest")
                    != capability.get("qualification_digest")
                or answer.get("cancel_requested_at") is not None
                    and (type(answer["cancel_requested_at"]) not in (int, float)
                         or isinstance(answer["cancel_requested_at"], bool)
                         or not math.isfinite(answer["cancel_requested_at"])
                         or answer["cancel_requested_at"] < 0)
                or answer.get("stopped_evidence_digest") is not None
                    and (type(answer["stopped_evidence_digest"]) is not str
                         or _HEX64.fullmatch(answer["stopped_evidence_digest"]) is None)
                or type(leases) is not dict or frozenset(leases) != _HUB_LEASE_KEYS
                or type(leases.get("compute")) is not bool
                or any(type(leases.get(key)) is not int or leases[key] < 0
                       for key in ("reference", "reader", "transfer", "ack"))
                or type(answer.get("replayed")) is not bool
                or answer.get("output_receipt") is not None
                    and type(answer["output_receipt"]) is not dict
                or answer.get("state") == "cancelled" and (
                    answer.get("attempt_id") is None
                    or leases.get("compute") is not False
                    or answer.get("stopped_evidence_digest") is None
                )):
            raise RenderServiceError("HUB_RESPONSE_UNKNOWN", 503)
        return answer

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        delivery = self.store.get_render_delivery(row["id"]) or {}
        if (row.get("state") not in _STAGING_STATES
                or row.get("cancel_state") not in _CANCEL_STATES | {None}
                or delivery.get("remote_state") not in _REMOTE_STATES | {None}):
            raise PrivateRenderUnavailable()
        result = {key: value for key, value in {
            "format": "studio.render.gateway-job.v1",
            "id": row["id"], "state": row["state"],
            "gateway_attempt_key": row["gateway_attempt_key"],
            "story_uuid": row["story_uuid"], "intent_id": row["intent_id"],
            "attempt_no": row["attempt_no"], "cancel_state": row.get("cancel_state"),
            "remote_state": delivery.get("remote_state"),
            "output_ready": bool(delivery.get("output_asset_handle")),
            "receipt_digest": delivery.get("gateway_receipt_digest"),
            "idempotent_replay": bool(row.get("idempotent_replay")),
        }.items() if value is not None}
        output_fields = (
            delivery.get("output_asset_handle"), delivery.get("gateway_receipt"),
            delivery.get("gateway_receipt_digest"),
        )
        if any(value is not None for value in output_fields):
            if not all(value is not None for value in output_fields):
                raise PrivateRenderUnavailable()
            try:
                raw_receipt, receipt = self._validated_gateway_receipt(row, delivery)
                result["output"] = {
                    "sha256": receipt["output_sha256"],
                    "size_bytes": receipt["output_size_bytes"],
                    "receipt_digest": delivery["gateway_receipt_digest"],
                    "receipt_utf8": raw_receipt.decode("utf-8"),
                }
            except (TypeError, ValueError, KeyError, UnicodeError, RenderServiceError):
                raise PrivateRenderUnavailable() from None
        stopped = delivery.get("stopped_evidence_digest")
        if stopped is not None:
            if (row.get("cancel_state") != "cancelled"
                    or row.get("hub_job_id") is None
                    or delivery.get("remote_attempt_id") is None
                    or type(stopped) is not str or _HEX64.fullmatch(stopped) is None):
                raise PrivateRenderUnavailable()
            result["terminal_proof"] = {
                "format": "studio.render.gateway-terminal-proof.v1",
                "kind": "launched_stopped", "gateway_job_id": row["id"],
                "gateway_attempt_key": row["gateway_attempt_key"],
                "hub_job_id": row["hub_job_id"],
                "hub_attempt_id": delivery["remote_attempt_id"],
                "stopped_evidence_digest": stopped,
            }
        elif row.get("terminal_fence_receipt") is not None:
            fence = row["terminal_fence_receipt"]
            first = row.get("terminal_first_cancel_at")
            if (row.get("cancel_state") != "cancelled" or row.get("hub_job_id") is not None
                    or type(fence) is not str or re.fullmatch(r"[0-9a-f]{32}", fence) is None
                    or type(first) not in (int, float) or isinstance(first, bool)
                    or not math.isfinite(first) or first < 0):
                raise PrivateRenderUnavailable()
            result["terminal_proof"] = {
                "format": "studio.render.gateway-terminal-proof.v1",
                "kind": "never_launched",
                "gateway_attempt_key": row["gateway_attempt_key"],
                "inbound_story_body_sha256": row["inbound_story_body_sha256"],
                "fence_receipt": fence, "first_cancel_at": first,
            }
        return result

    def _validated_gateway_receipt(
        self, row: dict[str, Any], delivery: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        raw = bytes(delivery["gateway_receipt"])
        receipt = decode_render_json(raw)
        capability = decode_render_json(bytes(row["capability_snapshot"]))
        machine = decode_render_json(bytes(row["selected_machine"]))
        remote = json.loads(delivery["remote_descriptor_json"])
        expected = {
            "format": "studio.render.gateway-output-receipt.v1",
            "owner_scope": row["owner_scope"],
            "gateway_instance_id": row["gateway_instance_id"],
            "story_instance_id": row["story_instance_id"],
            "gateway_job_id": row["id"],
            "gateway_attempt_key": row["gateway_attempt_key"],
            "story_uuid": row["story_uuid"], "intent_id": row["intent_id"],
            "attempt_no": row["attempt_no"],
            "inbound_story_body_sha256": row["inbound_story_body_sha256"],
            "packet_digest": row["packet_sha256"],
            "outbound_hub_body_sha256": row["outbound_hub_body_sha256"],
            "hub_dispatch_key": row["dispatch_key"],
            "dispatch_fence": row["dispatch_fence"],
            "hub_owner_scope": _hub_owner_scope(row),
            "hub_job_id": row["hub_job_id"],
            "hub_attempt_id": delivery["remote_attempt_id"],
            "hub_receipt_digest": delivery["receipt_digest"],
            "selected_machine": machine,
            "profile_digest": capability.get("profile_digest"),
            "tools_digest": capability.get("tools_digest"),
            "compiler_digest": capability.get("compiler_digest"),
            "qualification_digest": capability.get("qualification_digest"),
            "selected_tier": capability.get("selected_tier"),
            "generated_command_digest": remote.get("generated_command_digest"),
            "feature_losses": remote.get("feature_losses"),
            "output_sha256": remote.get("output_sha256"),
            "output_size_bytes": remote.get("output_size_bytes"),
        }
        binding = _stored_encode_binding(row)
        hub_receipt_keys = _HUB_RECEIPT_KEYS | ({"encode_authority"} if binding is not None else set())
        digest_fields = (
            "inbound_story_body_sha256", "packet_digest", "outbound_hub_body_sha256",
            "hub_receipt_digest", "profile_digest", "tools_digest", "compiler_digest",
            "qualification_digest", "generated_command_digest", "output_sha256",
        )
        if (type(receipt) is not dict or frozenset(receipt) != _GATEWAY_RECEIPT_KEYS
                or receipt != expected or _canonical(receipt) != raw
                or hashlib.sha256(raw).hexdigest() != delivery["gateway_receipt_digest"]
                or any(type(receipt[key]) is not str or _HEX64.fullmatch(receipt[key]) is None
                       for key in digest_fields)
                or type(receipt["output_size_bytes"]) is not int
                or receipt["output_size_bytes"] <= 0
                or type(receipt["feature_losses"]) is not list
                or len(receipt["feature_losses"]) > 64
                or any(type(item) is not str or not item or len(item) > 256
                       for item in receipt["feature_losses"])
                or type(machine) is not dict
                or set(machine) != {"id", "label", "chip", "memory_gb", "hardware_class"}
                or machine.get("id") != row["destination_node"]
                or any(type(machine.get(key)) is not str or not machine[key]
                       or machine[key] != machine[key].strip() or len(machine[key]) > 256
                       for key in ("id", "label", "chip", "hardware_class"))
                or type(machine.get("memory_gb")) not in (int, float)
                or isinstance(machine.get("memory_gb"), bool)
                or not math.isfinite(machine["memory_gb"])
                or machine["memory_gb"] <= 0
                or type(remote) is not dict or frozenset(remote) != hub_receipt_keys
                or binding is not None and (remote.get("encode_authority") != binding
                                            or remote.get("feature_losses") != [])
                or remote.get("owner_scope") != receipt["hub_owner_scope"]
                or remote.get("hub_job_id") != receipt["hub_job_id"]
                or remote.get("attempt_id") != receipt["hub_attempt_id"]
                or remote.get("accepted_request_sha256")
                    != receipt["outbound_hub_body_sha256"]
                or remote.get("receipt_digest") != receipt["hub_receipt_digest"]
                or any(remote.get(key) != receipt[key] for key in (
                    "output_sha256", "output_size_bytes", "profile_digest",
                    "tools_digest", "compiler_digest", "qualification_digest",
                    "generated_command_digest", "selected_tier", "feature_losses",
                ))
                or hashlib.sha256(_canonical({
                    key: value for key, value in remote.items() if key != "receipt_digest"
                })).hexdigest() != remote.get("receipt_digest")):
            raise ValueError
        return raw, receipt

    @staticmethod
    def _blob_public(sha256: str, size_bytes: int, status: dict[str, Any]) -> dict[str, Any]:
        # The asset store says "incomplete"; Story's wire contract is missing | uploading | complete.
        state = "uploading" if status.get("state") in {"open", "incomplete"} else status.get("state")
        result = {
            "format": "studio.render.gateway-blob.v1", "sha256": sha256,
            "size_bytes": size_bytes, "state": state, "offset": status.get("offset", 0),
        }
        if status.get("upload_id") is not None:
            result["upload_id"] = status["upload_id"]
        if status.get("asset_handle") is not None:
            result["asset_handle"] = status["asset_handle"]
        return result

    @staticmethod
    def _cancel_public(
        result: dict[str, Any], *, gateway_attempt_key: str,
        expected_request_sha256: str,
    ) -> dict[str, Any]:
        if result.get("state") not in {"cancel_requested", "cancelled_before_acceptance"}:
            raise PrivateRenderUnavailable()
        answer = {
            "format": "studio.render.gateway-cancel.v1",
            "state": result["state"], "gateway_attempt_key": gateway_attempt_key,
            "expected_request_sha256": expected_request_sha256,
        }
        for key in ("gateway_job_id", "fence_receipt", "first_cancel_at"):
            if result.get(key) is not None:
                answer[key] = result[key]
        if result["state"] == "cancelled_before_acceptance":
            fence = result.get("fence_receipt")
            first = result.get("first_cancel_at")
            if (type(fence) is not str or re.fullmatch(r"[0-9a-f]{32}", fence) is None
                    or type(first) not in (int, float) or isinstance(first, bool)
                    or not math.isfinite(first) or first < 0):
                raise PrivateRenderUnavailable()
            answer["terminal_proof"] = {
                "format": "studio.render.gateway-terminal-proof.v1",
                "kind": "never_launched", "gateway_attempt_key": gateway_attempt_key,
                "inbound_story_body_sha256": expected_request_sha256,
                "fence_receipt": fence, "first_cancel_at": first,
            }
        return answer

    def _accepted(self, row: dict[str, Any]) -> dict[str, Any]:
        return self._public(row) | {
            "destination_node": row["destination_node"],
            "inbound_story_body_sha256": row["inbound_story_body_sha256"],
        }

    def _bind_cancel_observation(
        self, row: dict[str, Any], answer: dict[str, Any],
    ) -> None:
        if row.get("hub_job_id") is None:
            self.store.record_render_hub_acceptance(row["id"], answer["id"])
        delivery = self.store.get_render_delivery(row["id"]) or {"revision": 0}
        if answer.get("attempt_id") is not None and delivery.get("remote_attempt_id") is None:
            self.store.update_render_delivery(
                row["id"], expected_revision=delivery["revision"],
                remote_state=answer["state"], remote_attempt_id=answer["attempt_id"],
            )

    def _status_answer_refused(
        self, row: dict[str, Any], delivery: dict[str, Any], answer: Any,
        error: RenderServiceError,
    ) -> None:
        """Log an answer the Gateway cannot accept, and end the render after a bounded run of them.

        Raising out of the monitor thread is what made this expensive: the dispatch clock restarts
        the step on the next pass, so one unacceptable answer became 1,992 tracebacks while the row
        stayed `accepted` for ever and Story waited. The Hub's own words go to the log every time;
        a run that never becomes acceptable settles `failed`, which is the delivery state Story
        already reads as a remote refusal and falls back locally from.
        """
        count = self._status_rejections.get(row["id"], 0) + 1
        self._status_rejections[row["id"]] = count
        log.warning(
            "render %s: cannot accept the destination's answer for Hub job %s (%s; %s); "
            "answer %d of %d before the render is reported failed",
            row["id"], row.get("hub_job_id"), error.code, _answer_excerpt(answer), count,
            _HUB_STATUS_REJECTION_LIMIT,
        )
        if count < _HUB_STATUS_REJECTION_LIMIT:
            return
        if delivery.get("output_asset_handle") is not None:
            # An imported output is not un-settled by a later answer: nothing here is allowed to
            # replace the receipt that was already proved.
            return
        try:
            self.store.update_render_delivery(
                row["id"], expected_revision=delivery["revision"], remote_state="failed",
            )
        except RenderJournalConflict:
            return
        self._status_rejections.pop(row["id"], None)
        log.warning(
            "render %s: the destination never answered acceptably, so it is reported failed; "
            "Story falls back to a local render", row["id"],
        )

    def _one_step(self, row: dict[str, Any]) -> None:
        destination = self._destination(row)
        if row.get("cancel_state") == "requested":
            settled = self.store.get_render_delivery(row["id"]) or {}
            if settled.get("remote_state") in ("failed", "output_ready"):
                # The render already settled terminal, so a repeated cancel has nothing to
                # stop. Clear it directly: sending again cannot change a terminal Hub state,
                # and the pending-cancel markers must not cycle through the send path.
                self.store.clear_render_cancel_terminal(
                    row["id"], remote_state=settled["remote_state"]
                )
                return
            if row.get("cancel_outcome") == "accepted":
                if not row.get("hub_job_id"):
                    answer = self.hub.by_key(destination, row["dispatch_key"])
                    try:
                        answer = self._hub_job(row, answer)
                    except RenderServiceError:
                        return
                    self.store.record_render_hub_acceptance(row["id"], answer["id"])
                    if answer["state"] in ("failed", "output_ready"):
                        self._bind_cancel_observation(row, answer)
                        self.store.clear_render_cancel_terminal(
                            row["id"], remote_state=answer["state"],
                            remote_descriptor=(answer.get("output_receipt")
                                               if answer["state"] == "output_ready" else None),
                        )
                    return
                answer = self._hub_job(
                    row, self.hub.status(destination, row["hub_job_id"])
                )
                if answer["state"] == "cancelled":
                    self._bind_cancel_observation(row, answer)
                    self.store.settle_render_cancel(
                        row["id"], outcome="stopped", hub_job_id=answer["id"],
                        remote_attempt_id=answer["attempt_id"],
                        stopped_evidence_digest=answer["stopped_evidence_digest"],
                    )
                elif answer["state"] in ("failed", "output_ready"):
                    # The Hub already finished this render; a cancel cannot stop it. Drop the
                    # pending cancel so the row leaves render_due instead of polling forever.
                    self._bind_cancel_observation(row, answer)
                    self.store.clear_render_cancel_terminal(
                        row["id"], remote_state=answer["state"],
                        remote_descriptor=(answer.get("output_receipt")
                                           if answer["state"] == "output_ready" else None),
                    )
                return
            started = self.store.mark_render_cancel_send_started(row["id"])
            body = bytes(started["cancel_body"])
            # The store prepares a durable cancel identity before any dispatch exists
            # (store.request_render_cancel_by_key): the Hub dispatch key once a dispatch body
            # is prepared, else the raw gateway attempt key. Send by it and bind the fence to
            # the digest the store embedded in the cancel body, so a cancel that arrives while
            # the render is still staging (dispatch_key null) still reaches the Hub, which
            # answers cancelled_before_acceptance and settles the row as never_launched. The
            # post-dispatch paths are unchanged: cancel_key equals dispatch_key once prepared.
            cancel_key = started.get("cancel_key")
            expected_request_sha256 = json.loads(body)["expected_request_sha256"]
            if row.get("hub_job_id"):
                answer = self.hub.cancel(destination, row["hub_job_id"], body)
            elif cancel_key:
                answer = self.hub.cancel_by_key(destination, cancel_key, body)
            else:
                return
            fence = (
                type(answer) is dict
                and set(answer) == {
                    "owner_scope", "dispatch_key", "expected_request_sha256",
                    "fence_id", "cancelled_at", "state",
                }
                and answer.get("owner_scope") == _hub_owner_scope(row)
                and answer.get("dispatch_key") == cancel_key
                and answer.get("expected_request_sha256") == expected_request_sha256
                and answer.get("state") == "cancelled_before_acceptance"
                and type(answer.get("fence_id")) is str
                and re.fullmatch(r"[0-9a-f]{32}", answer["fence_id"]) is not None
                and type(answer.get("cancelled_at")) in (int, float)
                and math.isfinite(answer["cancelled_at"])
                and answer["cancelled_at"] >= 0
            )
            if fence:
                self.store.settle_render_cancel(
                    row["id"], outcome="stopped",
                    fence_receipt=answer["fence_id"],
                    first_cancel_at=answer["cancelled_at"],
                )
                return
            try:
                answer = self._hub_job(row, answer)
            except RenderServiceError:
                return
            self._bind_cancel_observation(row, answer)
            if answer["state"] == "cancelled":
                self.store.settle_render_cancel(
                    row["id"], outcome="stopped", hub_job_id=answer["id"],
                    remote_attempt_id=answer["attempt_id"],
                    stopped_evidence_digest=answer["stopped_evidence_digest"],
                )
            elif answer["state"] in ("failed", "output_ready"):
                # The render reached a terminal Hub state a cancel cannot change; settle
                # honestly and drop the pending cancel so it is not re-sent forever.
                self.store.clear_render_cancel_terminal(
                    row["id"], remote_state=answer["state"],
                    remote_descriptor=(answer.get("output_receipt")
                                       if answer["state"] == "output_ready" else None),
                )
            else:
                self.store.settle_render_cancel(row["id"], outcome="accepted")
            return
        if row["state"] == "staging":
            pending = next((asset for asset in row["assets"]
                            if asset["confirmed_hub_offset"] < asset["size_bytes"]), None)
            if pending is not None:
                existing_pending = pending["pending_offset"] is not None
                if not existing_pending and not self._fresh_send_allowed(row):
                    return
                try:
                    status = self.hub.blob_status(
                        destination, pending["sha256"], pending["size_bytes"]
                    )
                except Exception:
                    return
                descriptor_keys = {
                    "upload_id", "sha256", "size_bytes", "committed_offset", "state",
                    "asset_handle",
                }
                published = (
                    type(status) is dict and set(status) == descriptor_keys
                    and status.get("sha256") == pending["sha256"]
                    and status.get("size_bytes") == pending["size_bytes"]
                    and status.get("committed_offset") == pending["size_bytes"]
                    and status.get("state") == "published"
                    and type(status.get("upload_id")) is str
                    and re.fullmatch(r"[0-9a-f]{32}", status["upload_id"]) is not None
                    and type(status.get("asset_handle")) is str
                    and re.fullmatch(r"[0-9a-f]{32}", status["asset_handle"]) is not None
                )
                if published:
                    self.store.adopt_render_asset_publication(
                        row["id"], pending["logical_id"], sha256=pending["sha256"],
                        size_bytes=pending["size_bytes"],
                        hub_asset_handle=status["asset_handle"],
                    )
                    return
                missing = status == {"accepted": False, "http_status": 404}
                incomplete = (
                    type(status) is dict and set(status) == descriptor_keys
                    and status.get("sha256") == pending["sha256"]
                    and status.get("size_bytes") == pending["size_bytes"]
                    and type(status.get("committed_offset")) is int
                    and 0 <= status["committed_offset"] < pending["size_bytes"]
                    and status.get("state") == "incomplete"
                    and type(status.get("upload_id")) is str
                    and re.fullmatch(r"[0-9a-f]{32}", status["upload_id"]) is not None
                    and status.get("asset_handle") is None
                )
                if not missing and not incomplete:
                    return
                remote_offset = status["committed_offset"] if incomplete else 0
                offset = pending["confirmed_hub_offset"]
                if existing_pending:
                    end = pending["pending_offset"] + pending["pending_length"]
                    if (pending["pending_offset"] != offset
                            or remote_offset not in (offset, end)):
                        return
                    if remote_offset == end:
                        self.store.confirm_render_asset_range(
                            row["id"], pending["logical_id"], offset=offset,
                            length=pending["pending_length"],
                            sha256=pending["pending_sha256"], hub_asset_handle=None,
                        )
                        return
                    length = pending["pending_length"]
                else:
                    if remote_offset != offset:
                        return
                    length = min(_CHUNK, pending["size_bytes"] - offset)
                with self.assets.open_reader(row["owner_scope"], pending["gateway_asset_handle"]) as reader:
                    reader.seek(offset)
                    data = reader.read(length)
                if len(data) != length:
                    raise RenderServiceError("RENDER_ASSET_READ_FAILED", 503)
                range_sha = hashlib.sha256(data).hexdigest()
                if existing_pending:
                    if range_sha != pending["pending_sha256"]:
                        raise RenderServiceError("RENDER_ASSET_READ_FAILED", 503)
                else:
                    self.store.record_render_asset_pending(
                        row["id"], pending["logical_id"], offset=offset, length=length,
                        sha256=range_sha,
                    )
                answer = self.hub.put_blob(destination, pending["sha256"], offset,
                                           pending["size_bytes"], data, range_sha)
                complete = offset + length == pending["size_bytes"]
                if complete:
                    valid = (
                        type(answer) is dict
                        and set(answer) == {"asset_handle", "sha256", "size_bytes"}
                        and answer.get("sha256") == pending["sha256"]
                        and answer.get("size_bytes") == pending["size_bytes"]
                        and type(answer.get("asset_handle")) is str
                        and re.fullmatch(r"[0-9a-f]{32}", answer["asset_handle"]) is not None
                    )
                    handle = answer.get("asset_handle") if valid else None
                else:
                    valid = (
                        type(answer) is dict
                        and set(answer) == {
                            "upload_id", "sha256", "size_bytes", "committed_offset",
                            "state", "asset_handle",
                        }
                        and answer.get("sha256") == pending["sha256"]
                        and answer.get("size_bytes") == pending["size_bytes"]
                        and answer.get("committed_offset") == offset + length
                        and answer.get("state") == "incomplete"
                        and answer.get("asset_handle") is None
                    )
                    handle = None
                if not valid:
                    return
                self.store.confirm_render_asset_range(
                    row["id"], pending["logical_id"], offset=offset, length=length,
                    sha256=range_sha, hub_asset_handle=handle,
                )
                return
            request = decode_render_json(bytes(row["inbound_story_body"]))
            handles = {asset["logical_id"]: asset["hub_asset_handle"] for asset in row["assets"]}
            dispatch_key = "gateway:" + row["gateway_attempt_key"]
            fence = str(uuid.uuid4())
            binding = _stored_encode_binding(row)
            body = _canonical({
                "format": ("studio.render.hub-request.v2" if binding is not None
                           else "studio.render.hub-request.v1"),
                **({"encode_authority": binding} if binding is not None else {}),
                "dispatch": {
                    "story_uuid": row["story_uuid"], "intent_id": row["intent_id"],
                    "attempt_no": row["attempt_no"],
                    "gateway_attempt_key": row["gateway_attempt_key"],
                    "destination_site": row["destination_site"],
                    "destination_controller": row["destination_hub_controller"],
                    "destination_node": row["destination_node"],
                    "dispatch_key": dispatch_key, "dispatch_fence": fence,
                },
                "packet": request["packet"], "assets": handles,
                # Carried through only when Story sent it: the Hub's Render tab names the
                # channel and episode with it and nothing else reads it. A Story Studio that
                # sends no display produces exactly the body it produced before.
                **({"display": request["display"]} if "display" in request else {}),
            })
            self.store.prepare_render_dispatch(
                row["id"], outbound_hub_body=body,
                outbound_hub_body_sha256=hashlib.sha256(body).hexdigest(),
                dispatch_key=dispatch_key, dispatch_fence=fence,
            )
            return
        if row["state"] == "hub_prepared":
            if not self._fresh_send_allowed(row):
                return
            self.store.mark_render_dispatch_started(row["id"])
            try:
                answer = self.hub.submit(destination, row["dispatch_key"],
                                         bytes(row["outbound_hub_body"]))
            except Exception:
                self.store.mark_render_dispatch_unknown(row["id"])
                return
            # A returned answer is a definitive Hub response; only a raised exception is an
            # unknown send. A definitive no-job answer (the Hub created no job for this key)
            # is replayed once with the identical body — the Hub accepts idempotently on
            # (dispatch_key, accepted_request_sha256), so a replay cannot create a second job.
            if answer == {"accepted": False, "http_status": 404}:
                try:
                    answer = self.hub.submit(destination, row["dispatch_key"],
                                             bytes(row["outbound_hub_body"]))
                except Exception:
                    self.store.mark_render_dispatch_unknown(row["id"])
                    return
            try:
                answer = self._hub_job(row, answer)
                self.store.record_render_hub_acceptance(row["id"], answer["id"])
            except RenderServiceError:
                self.store.mark_render_dispatch_unknown(row["id"])
            return
        if row["state"] == "send_may_start":
            self.store.mark_render_dispatch_unknown(row["id"])
            return
        if row["state"] == "send_unknown":
            try:
                answer = self.hub.by_key(destination, row["dispatch_key"])
            except Exception:
                return
            if answer == {"accepted": False, "http_status": 404}:
                # The Hub has no job under this dispatch key, so the earlier submit never
                # landed. Replay the identical submit; the Hub dedupes on
                # (dispatch_key, accepted_request_sha256), so this cannot duplicate a job.
                try:
                    answer = self.hub.submit(destination, row["dispatch_key"],
                                             bytes(row["outbound_hub_body"]))
                except Exception:
                    return
            try:
                answer = self._hub_job(row, answer)
            except RenderServiceError:
                return
            self.store.record_render_hub_acceptance(row["id"], answer["id"])
            return
        if row["state"] == "accepted":
            delivery = self.store.get_render_delivery(row["id"]) or {"revision": 0}
            answer: Any = None
            try:
                if delivery.get("remote_state") == "output_ready" \
                        and delivery.get("output_asset_handle") is None:
                    self._import_output(row, delivery, destination)
                    return
                answer = self.hub.status(destination, row["hub_job_id"], error_body=True)
                answer = self._hub_job(row, answer)
            except RenderServiceError as exc:
                self._status_answer_refused(row, delivery, answer, exc)
                return
            except Exception as exc:  # noqa: BLE001 - retried on the next pass, never fatal
                # No acceptable answer arrived (the Mac is unreachable, the relay timed out, or a
                # local step failed): an unknown outcome is retried and is never counted as a
                # refusal. This used to raise out of the monitor thread and log a traceback on
                # every dispatch pass.
                log.warning("render %s: the status step failed (%s); retrying",
                            row["id"], type(exc).__name__)
                return
            self._status_rejections.pop(row["id"], None)
            remote = answer["state"]
            self.store.update_render_delivery(
                row["id"], expected_revision=delivery["revision"], remote_state=remote,
                remote_attempt_id=answer.get("attempt_id"),
                remote_descriptor=(answer["output_receipt"]
                                   if remote == "output_ready" else None),
            )
            return
        if row["state"] == "output_ready":
            delivery = self.store.get_render_delivery(row["id"]) or {}
            if delivery.get("ack_outcome") is not None:
                return
            prepared = self.store.prepare_render_ack(
                row["id"], receipt_digest=delivery["receipt_digest"],
                attempt_id=delivery["remote_attempt_id"],
                ack_key="ack:" + row["dispatch_key"],
            )
            answer = self.hub.ack(
                destination, row["hub_job_id"], prepared["ack_key"], bytes(prepared["ack_body"])
            )
            if (answer == {"ok": True, "job_id": row["hub_job_id"],
                           "receipt_digest": delivery["receipt_digest"]}):
                self.store.settle_render_ack(row["id"], ack_outcome="accepted")

    def _import_output(self, row: dict[str, Any], delivery: dict[str, Any],
                       destination: RenderDestination) -> None:
        try:
            receipt = json.loads(delivery["remote_descriptor_json"])
            capability = decode_render_json(bytes(row["capability_snapshot"]))
        except (TypeError, ValueError):
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503) from None
        required = {
            "owner_scope", "hub_job_id", "attempt_id", "accepted_request_sha256",
            "receipt_digest", "output_sha256", "output_size_bytes", "profile_digest",
            "tools_digest", "compiler_digest", "qualification_digest",
            "generated_command_digest", "selected_tier", "feature_losses",
        }
        if type(capability) is not dict:
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        binding = _stored_encode_binding(row)
        if binding is not None:
            required.add("encode_authority")
        if (type(receipt) is not dict or set(receipt) != required
                or binding is not None and (receipt.get("encode_authority") != binding
                                            or receipt.get("feature_losses") != [])
                or receipt["owner_scope"] != _hub_owner_scope(row)
                or receipt["hub_job_id"] != row["hub_job_id"]
                or receipt["accepted_request_sha256"] != row["outbound_hub_body_sha256"]
                or receipt["attempt_id"] != delivery.get("remote_attempt_id")
                or any(type(receipt[key]) is not str or _HEX64.fullmatch(receipt[key]) is None
                       for key in ("receipt_digest", "output_sha256", "profile_digest",
                                   "tools_digest", "compiler_digest", "qualification_digest",
                                   "generated_command_digest", "accepted_request_sha256"))
                or type(receipt["output_size_bytes"]) is not int
                or receipt["output_size_bytes"] <= 0
                or type(receipt["feature_losses"]) is not list
                or len(receipt["feature_losses"]) > 64
                or any(type(item) is not str or not item or len(item) > 256
                       for item in receipt["feature_losses"])
                or receipt["profile_digest"] != capability.get("profile_digest")
                or receipt["tools_digest"] != capability.get("tools_digest")
                or receipt["compiler_digest"] != capability.get("compiler_digest")):
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        if (receipt["qualification_digest"] != capability.get("qualification_digest")
                or receipt["selected_tier"] != capability.get("selected_tier")):
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        hub_receipt_body = _canonical({
            key: value for key, value in receipt.items() if key != "receipt_digest"
        })
        if hashlib.sha256(hub_receipt_body).hexdigest() != receipt["receipt_digest"]:
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        profile_limit = (capability.get("profile_execution_bounds") or {}).get(
            "output_limit_bytes", 0
        )
        process_limit = (capability.get("bounds") or {}).get("output_limit_bytes", 0)
        if (type(profile_limit) is not int or type(process_limit) is not int
                or profile_limit <= 0 or process_limit <= 0):
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        output_limit = min(profile_limit, process_limit)
        if receipt["output_size_bytes"] > output_limit:
            raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
        status = self.assets.blob_status(
            row["owner_scope"], receipt["output_sha256"], receipt["output_size_bytes"]
        )
        if status["state"] == "missing":
            status = self.assets.begin_upload(
                row["owner_scope"], receipt["output_sha256"], receipt["output_size_bytes"]
            )
        upload_id = status["upload_id"]
        self.store.bind_render_output_upload(
            row["id"], expected_revision=delivery["revision"], output_upload_id=upload_id
        )
        offset = status.get("offset", 0)
        if offset < receipt["output_size_bytes"]:
            length = min(_CHUNK, receipt["output_size_bytes"] - offset)
            data, headers = self.hub.output_range(
                destination, row["hub_job_id"], offset, length,
                '"' + receipt["output_sha256"] + '"',
            )
            expected_range = f"bytes {offset}-{offset + length - 1}/{receipt['output_size_bytes']}"
            if (headers.get("content-range") != expected_range
                    or headers.get("etag") != '"' + receipt["output_sha256"] + '"'
                    or headers.get("x-content-sha256") != receipt["output_sha256"]
                    or headers.get("content-type") != "video/mp4"
                    or len(data) != length):
                raise RenderServiceError("HUB_OUTPUT_UNKNOWN", 503)
            self.assets.append_chunk(
                row["owner_scope"], upload_id, offset, io.BytesIO(data), length
            )
            return
        asset = self.assets.finish_upload(row["owner_scope"], upload_id)
        selected_machine = decode_render_json(bytes(row["selected_machine"]))
        gateway_receipt = _canonical({
            "format": "studio.render.gateway-output-receipt.v1",
            "owner_scope": row["owner_scope"],
            "gateway_instance_id": row["gateway_instance_id"],
            "story_instance_id": row["story_instance_id"],
            "gateway_job_id": row["id"],
            "gateway_attempt_key": row["gateway_attempt_key"],
            "story_uuid": row["story_uuid"], "intent_id": row["intent_id"],
            "attempt_no": row["attempt_no"],
            "inbound_story_body_sha256": row["inbound_story_body_sha256"],
            "packet_digest": row["packet_sha256"],
            "outbound_hub_body_sha256": row["outbound_hub_body_sha256"],
            "hub_dispatch_key": row["dispatch_key"],
            "dispatch_fence": row["dispatch_fence"],
            "hub_owner_scope": receipt["owner_scope"],
            "hub_job_id": receipt["hub_job_id"],
            "hub_attempt_id": receipt["attempt_id"],
            "hub_receipt_digest": receipt["receipt_digest"],
            "selected_machine": selected_machine,
            "profile_digest": receipt["profile_digest"],
            "tools_digest": receipt["tools_digest"],
            "compiler_digest": receipt["compiler_digest"],
            "qualification_digest": receipt["qualification_digest"],
            "selected_tier": receipt["selected_tier"],
            "generated_command_digest": receipt["generated_command_digest"],
            "feature_losses": receipt["feature_losses"],
            "output_sha256": receipt["output_sha256"],
            "output_size_bytes": receipt["output_size_bytes"],
        })
        gateway_receipt_digest = hashlib.sha256(gateway_receipt).hexdigest()
        self.assets.acquire_reference(
            row["owner_scope"], asset["asset_handle"], "render-job:" + row["id"]
        )
        try:
            self.store.publish_render_winner(
                row["id"], expected_revision=delivery["revision"],
                winner_fence=row["dispatch_fence"], output_upload_id=upload_id,
                output_asset_handle=asset["asset_handle"],
                receipt_digest=receipt["receipt_digest"],
                gateway_receipt=gateway_receipt,
                gateway_receipt_digest=gateway_receipt_digest,
            )
        except Exception:
            self.assets.release_reference(
                row["owner_scope"], asset["asset_handle"], "render-job:" + row["id"]
            )
            raise

    def _maybe_sweep_assets(self) -> None:
        """Start at most one background asset sweep per interval.

        The sweep walks upload directories under the store's own lock, so it runs off the
        dispatch clock: a slow disk must never delay selecting the next render.
        """
        now = time.time()
        with self._lock:
            if (self._stopping or self._sweep_thread is not None
                    or now - self._last_sweep < _SWEEP_INTERVAL_S):
                return
            self._last_sweep = now
            thread = threading.Thread(
                target=self._sweep_assets, name="gateway-render-sweep", daemon=True
            )
            self._sweep_thread = thread
            thread.start()

    def _sweep_assets(self) -> None:
        try:
            self.assets.sweep_staging()
            # An ack releases `render-job:<id>` itself; if the process died between the
            # ack commit and that release, the protection would block reclaim forever.
            # Releasing is idempotent and runs before the protection check below.
            for row in self.store.acknowledged_render_outputs():
                try:
                    self.assets.release_reference(
                        row["owner_scope"], row["output_asset_handle"],
                        "render-job:" + row["id"],
                    )
                except (AssetNotFoundError, AssetRecoveryRequiredError, OSError):
                    continue
            terminal = self.store.terminal_render_asset_handles()
            referenced = self.store.referenced_render_asset_handles()
            self.assets.sweep_published(terminal, protected=referenced)
        except Exception:  # noqa: BLE001 - retention must never break rendering
            log.exception("render asset sweep failed")
        finally:
            with self._lock:
                self._sweep_thread = None

    def dispatch_pass(self) -> bool:
        if self._stopping:
            return False
        self._maybe_sweep_assets()
        with self._lock:
            if len(self._threads) >= 2:
                return False
            rows = self.store.render_due(limit=64, after=self._dispatch_cursor)
            row = next((item for item in rows if item["id"] not in self._active_ids), None)
            if row is None and self._dispatch_cursor is not None:
                rows = self.store.render_due(limit=64)
                row = next((item for item in rows if item["id"] not in self._active_ids), None)
            if row is None:
                return False
            self._dispatch_cursor = (row["created_at"], row["id"])
            self._active_ids.add(row["id"])

            def run() -> None:
                try:
                    self._one_step(row)
                finally:
                    with self._lock:
                        self._active_ids.discard(row["id"])
                        self._threads.discard(threading.current_thread())

            thread = threading.Thread(target=run, name="gateway-render-" + row["id"][:8], daemon=True)
            self._threads.add(thread)
            thread.start()
            return True

    def recover(self) -> None:
        self.store.render_due(limit=1)

    def unsettled(self) -> list[str]:
        with self._lock:
            return sorted(self._active_ids)

    def active(self) -> int:
        with self._lock:
            return len(self._active_ids)

    def stop(self) -> None:
        self._stopping = True
        with self._lock:
            threads = list(self._threads)
            sweep = self._sweep_thread
        for thread in threads:
            thread.join(timeout=5.0)
        if sweep is not None:
            sweep.join(timeout=5.0)
