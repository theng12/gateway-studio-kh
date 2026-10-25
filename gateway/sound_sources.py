"""Sound-source adapters — Openverse first, others later.

Each sound source is a small adapter that knows how to:

  1. search for commercial-safe audio clips,
  2. normalise the licence into one of `cc0`, `by`, or refuse anything else,
  3. download the chosen clip with the licence re-checked on the wire,
  4. read its live quota from the upstream response headers.

Openverse is the first adapter because it is free, commercial-safe, and
publishes the CC0 / CC BY split the owner wants (`cc0` first, `by` only
when nothing CC0 fits). Freesound direct, Pixabay, and others follow the
same interface — licence check is the gate every one of them has to pass.

**Licence normalisation is strict on purpose.** Monetised channels cannot
carry NC (non-commercial) or ND (no-derivatives) clips. A clip whose
licence field is empty, malformed, or anything outside `cc0` / `by` is
*refused*, not silently downgraded — "unknown" and "none" must produce
different values, or a future adapter that lies about the licence would
slip through.

**ponytail:** one adapter (Openverse), a fixed `kind` vocabulary
(`openverse`), no plugin loading. Upgrade path: add `FreesoundSource`,
`PixabaySource` here, register them in `SOURCES`, and the rest of the
service picks them up.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

from . import config
from .providers.credentials import CredentialStore

log = logging.getLogger("gateway.sound_sources")

# ---------------------------------------------------------------------------
# Licence normalisation
# ---------------------------------------------------------------------------

#: Licences we will carry. Anything else — NC, ND, All Rights Reserved,
#: unknown spelling, empty string — is refused.
ALLOWED_LICENCES: frozenset[str] = frozenset({"cc0", "by"})

#: Ranking weight for CC0-first ordering: lower sorts first.
_LICENCE_RANK: dict[str, int] = {"cc0": 0, "by": 1}


def normalise_licence(raw: Any) -> str | None:
    """Return `cc0`, `by`, or `None` (refused).

    Accepts the Openverse spelling (`cc0`, `by`, `by-nc`, `by-nc-sa`, `by-nd`,
    `by-sa`, `pdm`) and a few case variants. NC and ND variants return
    `None`. So does `sa` on its own — the owner's rule is "no-credit
    licences first, credit only when the licence requires it"; ShareAlike
    adds a derivative-work obligation the gateway is not set up to track.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if not v:
        return None
    if v in {"cc0", "publicdomain", "pdm", "public domain", "zero"}:
        return "cc0"
    if v == "by" or v.startswith("by-") and "nc" not in v and "nd" not in v and "sa" not in v:
        return "by"
    # by-nc, by-nd, by-nc-sa, by-nc-nd, by-sa, all-rights-reserved, …
    return None


def _licence_rank(licence: str | None) -> int:
    """Sort key: CC0 before BY, unknown last."""
    if licence is None:
        return 9
    return _LICENCE_RANK.get(licence, 5)


# ---------------------------------------------------------------------------
# Search result shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SoundSearchResult:
    source: str          # adapter id, e.g. "openverse"
    source_id: str       # upstream id, opaque to the library
    title: str
    author: str | None
    source_url: str      # the upstream page humans can visit
    licence: str         # "cc0" | "by" — already normalised
    licence_version: str | None
    attribution: str | None
    duration_ms: int | None
    filesize: int | None
    filetype: str | None
    # URL to fetch the bytes. Adapter owns the shape.
    download_url: str
    # Anything adapter-specific the download step needs (auth headers, etc.)
    download_extras: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[int, str]:
        return (_licence_rank(self.licence), (self.title or "").casefold())


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

@dataclass
class QuotaState:
    """The last-known quota numbers for a source, read from its response headers.

    Openverse ships `X-RateLimit-Limit-<scope>` and `X-RateLimit-Available-<scope>`.
    We do not synthesise a reset timestamp — the upstream does not publish one,
    so the field stays `None` rather than a made-up number.
    """
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_seen_at: float | None = None
    exhausted: bool = False
    exhausted_scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scopes": self.scopes,
            "last_seen_at": self.last_seen_at,
            "exhausted": self.exhausted,
            "exhausted_scope": self.exhausted_scope,
        }

    def ingest(self, headers: httpx.Headers, *, exhausted: bool) -> None:
        """Pull every `x-ratelimit-*` header into `scopes`."""
        seen: dict[str, dict[str, Any]] = {}
        limits: dict[str, str] = {}
        available: dict[str, str] = {}
        for key, value in headers.items():
            low = key.lower()
            if not low.startswith("x-ratelimit-"):
                continue
            rest = low[len("x-ratelimit-"):]
            if rest.startswith("limit-"):
                limits[rest[len("limit-"):]] = value
            elif rest.startswith("available-"):
                available[rest[len("available-"):]] = value
        for scope, limit in limits.items():
            seen[scope] = {"limit": limit, "available": available.get(scope)}
        # A scope that has an available count but no paired limit
        # (unusual, but the header contract allows it) still counts.
        for scope, avail in available.items():
            seen.setdefault(scope, {"limit": None, "available": avail})
        if seen:
            self.scopes = seen
            self.last_seen_at = time.time()
        self.exhausted = exhausted
        self.exhausted_scope = next(iter(seen), None) if exhausted else None


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------

class SoundSource:
    """Base class. A subclass implements `search` and `download_bytes`."""

    id: str = ""
    display_name: str = ""

    def __init__(self, credential_store: CredentialStore) -> None:
        self.credentials = credential_store
        self.quota = QuotaState()

    # -- subclass contract ---------------------------------------------------
    def is_configured(self) -> bool:
        """True when the adapter can make at least anonymous calls."""
        raise NotImplementedError

    def registered(self) -> bool:
        """True when the owner supplied credentials that raise the quota."""
        return False

    def search(self, *, query: str, min_sec: float | None, max_sec: float | None,
               category: str | None, page_size: int) -> list[SoundSearchResult]:
        raise NotImplementedError

    def download_bytes(self, result: SoundSearchResult, *, timeout: float) -> bytes:
        """Fetch the bytes. Re-checks the licence on the detail response."""
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "configured": self.is_configured(),
            "registered": self.registered(),
            "quota": self.quota.to_dict(),
        }


# ---------------------------------------------------------------------------
# Openverse
# ---------------------------------------------------------------------------

_OPENVERSE_BASE = os.environ.get("OPENVERSE_BASE_URL", "https://api.openverse.org/v1")
#: Openverse categories that fit Story cues. Audiobooks/news/podcasts are
#: spoken-word, not sound design, and are withheld.
_OPENVERSE_CATEGORY_MAP: dict[str, str] = {
    "sound_effect": "sound_effect",
    "music": "music",
}
#: Length bucket labels the Openverse index accepts. The index stores these
#: as categorical terms — not numeric ranges — so we translate `min_sec` /
#: `max_sec` into the closest bucket rather than sending a range the index
#: would ignore.
_OPENVERSE_LENGTH_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 30.0, "shortest"),
    (30.0, 120.0, "short"),
    (120.0, 300.0, "medium"),
    (300.0, float("inf"), "long"),
)
_USER_AGENT = "GatewayKH/0.2 (sound library integration)"


def _bucket_for(min_sec: float | None, max_sec: float | None) -> str | None:
    """Map a duration range to the closest Openverse `length` bucket.

    If the range spans multiple buckets we pick the one whose midpoint sits
    inside it; if it sits outside every bucket we return `None` and let the
    upstream search ignore the filter (better results than a forced mismatch).
    """
    if min_sec is None and max_sec is None:
        return None
    lo = min_sec or 0.0
    hi = max_sec or (lo + 300.0)
    mid = (lo + hi) / 2.0
    for blo, bhi, name in _OPENVERSE_LENGTH_BUCKETS:
        if blo <= mid < bhi:
            return name
    return None


class OpenverseSource(SoundSource):
    id = "openverse"
    display_name = "Openverse"

    def __init__(self, credential_store: CredentialStore) -> None:
        super().__init__(credential_store)
        # In-memory search cache. Same query → no quota cost. A TTL keeps a
        # stale upstream listing from outliving the clip it points at.
        self._cache: dict[str, tuple[float, list[SoundSearchResult]]] = {}
        self._cache_lock = threading.Lock()
        self._client: httpx.Client | None = None

    #: Cache entry lifetime. Long enough that a second search in the same
    #: Story session is free; short enough that a removed upstream clip
    #: surfaces within a minute.
    _CACHE_TTL_S = 60.0

    # -- credentials -----------------------------------------------------
    def _token(self) -> str | None:
        """Client-credentials access token, if the owner registered one.
        Stored directly in the credential store under 'openverse-sound' to avoid
        registering it as a model provider definition.
        """
        return self.credentials.stored("openverse-sound").get("access_token") or None

    def registered(self) -> bool:
        return bool(self._token())

    def is_configured(self) -> bool:
        # Openverse is usable anonymously — the free tier is the default.
        return True

    # -- HTTP ------------------------------------------------------------
    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(connect=8.0, read=30.0, write=8.0, pool=8.0),
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        token = self._token()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    # -- search ----------------------------------------------------------
    def search(self, *, query: str, min_sec: float | None, max_sec: float | None,
               category: str | None, page_size: int) -> list[SoundSearchResult]:
        cache_key = self._cache_key(query, min_sec, max_sec, category, page_size)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                ts, rows = cached
                if time.time() - ts < self._CACHE_TTL_S:
                    return rows

        params: dict[str, Any] = {
            "q": query,
            "license_type": "commercial",
            "page_size": max(1, min(page_size, 20)),
        }
        bucket = _bucket_for(min_sec, max_sec)
        if bucket is not None:
            params["length"] = bucket
        mapped = _OPENVERSE_CATEGORY_MAP.get((category or "").lower())
        if mapped:
            params["category"] = mapped

        client = self._get_client()
        url = f"{_OPENVERSE_BASE}/audio/?{urlencode(params)}"
        try:
            response = client.get(url, headers=self._auth_headers())
        except httpx.HTTPError as exc:
            log.warning("Openverse search network error: %s", exc)
            return []

        exhausted = response.status_code in (429, 403)
        self.quota.ingest(response.headers, exhausted=exhausted)

        if response.status_code == 429:
            # Quota hit — surface empty results; the caller falls back to
            # the library. Raising would force every caller to catch.
            return []
        if response.status_code != 200:
            log.warning("Openverse search %s: %s", response.status_code, response.text[:200])
            return []

        try:
            payload = response.json()
        except ValueError:
            return []

        results: list[SoundSearchResult] = []
        for raw in payload.get("results", []) or []:
            parsed = self._parse_result(raw)
            if parsed is not None:
                results.append(parsed)
        results.sort(key=lambda r: r.sort_key())

        with self._cache_lock:
            self._cache[cache_key] = (time.time(), results)
            self._evict_locked()
        return results

    def _cache_key(self, query: str, min_sec: float | None, max_sec: float | None,
                   category: str | None, page_size: int) -> str:
        return json.dumps({
            "q": query, "min": min_sec, "max": max_sec,
            "cat": category, "ps": page_size,
        }, sort_keys=True)

    def _evict_locked(self) -> None:
        # Ponytail: cap the cache by count, not by sweeping timers. 200
        # entries is more than any Story session generates; upgrade path
        # is an LRU if that stops being true.
        if len(self._cache) <= 200:
            return
        cutoff = time.time() - self._CACHE_TTL_S
        stale = [k for k, (ts, _) in self._cache.items() if ts < cutoff]
        for k in stale:
            del self._cache[k]
        if len(self._cache) <= 200:
            return
        # Still over: drop the oldest quarter.
        items = sorted(self._cache.items(), key=lambda kv: kv[1][0])
        for k, _ in items[: len(items) // 4]:
            del self._cache[k]

    def _parse_result(self, raw: dict[str, Any]) -> SoundSearchResult | None:
        licence = normalise_licence(raw.get("license"))
        if licence is None:
            # NC / ND / unknown — not commercial-safe, skip silently.
            return None
        source_id = raw.get("id")
        if not isinstance(source_id, str) or not source_id:
            return None
        download_url = raw.get("url")
        if not isinstance(download_url, str):
            return None
        # Allow http for localhost in tests only
        _allow_insecure = os.environ.get("OPENVERSE_ALLOW_INSECURE_URLS", "") == "1"
        if not download_url.startswith("https://"):
            if not (_allow_insecure and (download_url.startswith("http://127.0.0.1") or download_url.startswith("http://localhost"))):
                return None
        duration = raw.get("duration")
        duration_ms = int(duration) if isinstance(duration, (int, float)) else None
        filesize = raw.get("filesize")
        if not isinstance(filesize, int):
            filesize = None
        filetype = raw.get("filetype")
        if not isinstance(filetype, str):
            filetype = None
        attribution = raw.get("attribution")
        if not isinstance(attribution, str):
            attribution = None
        return SoundSearchResult(
            source=self.id,
            source_id=source_id,
            title=str(raw.get("title") or "") or source_id,
            author=(raw.get("creator") if isinstance(raw.get("creator"), str) else None),
            source_url=str(raw.get("foreign_landing_url") or ""),
            licence=licence,
            licence_version=(raw.get("license_version")
                             if isinstance(raw.get("license_version"), str) else None),
            attribution=attribution,
            duration_ms=duration_ms,
            filesize=filesize,
            filetype=filetype,
            download_url=download_url,
        )

    # -- download --------------------------------------------------------
    def detail_for(self, source_id: str) -> dict[str, Any] | None:
        """Re-fetch the detail to re-check the licence before the bytes land."""
        client = self._get_client()
        try:
            response = client.get(
                f"{_OPENVERSE_BASE}/audio/{source_id}/",
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            log.warning("Openverse detail network error %s: %s", source_id, exc)
            return None
        self.quota.ingest(response.headers, exhausted=(response.status_code == 429))
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def download_bytes(self, result: SoundSearchResult, *, timeout: float = 45.0) -> bytes:
        # The detail fetch is the licence re-check: the listing can be
        # minutes old (the cache holds it for longer), and an upstream
        # takedown between the search and the download must not land as a
        # library entry with a licence it no longer carries.
        detail = self.detail_for(result.source_id)
        if detail is None:
            raise SoundSourceError("The clip is no longer available upstream.", status=502)
        live_licence = normalise_licence(detail.get("license"))
        if live_licence is None:
            raise SoundSourceError("The clip's licence is no longer commercial-safe.", status=409,
                                   code="LICENCE_CHANGED")
        download_url = detail.get("url")
        if not isinstance(download_url, str):
            raise SoundSourceError("The clip has no downloadable audio URL.", status=502)
        _allow_insecure = os.environ.get("OPENVERSE_ALLOW_INSECURE_URLS", "") == "1"
        if not download_url.startswith("https://"):
            if not (_allow_insecure and (download_url.startswith("http://127.0.0.1") or download_url.startswith("http://localhost"))):
                raise SoundSourceError("The clip has no downloadable audio URL.", status=502)

        client = self._get_client()
        try:
            with client.stream("GET", download_url, timeout=timeout) as resp:
                if resp.status_code != 200:
                    raise SoundSourceError(
                        f"Upstream audio fetch returned {resp.status_code}.", status=502)
                content_type = resp.headers.get("content-type", "").lower()
                if "html" in content_type or "text/plain" in content_type:
                    raise SoundSourceError("The downloaded file is not a valid audio file.", status=415)
                chunks: list[bytes] = []
                total = 0
                ceiling = 50 * 1024 * 1024  # 50 MB — Story's beds are seconds, not hours
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > ceiling:
                        raise SoundSourceError("The audio file is larger than 50 MB.", status=413)
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise SoundSourceError(f"Upstream audio fetch failed: {exc}", status=502) from exc
        return b"".join(chunks)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SoundSourceError(Exception):
    """A refusal the route layer turns into a GatewayError."""
    def __init__(self, message: str, *, status: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_SOURCES: dict[str, type[SoundSource]] = {"openverse": OpenverseSource}
_INSTANCES: dict[str, SoundSource] = {}
_INSTANCES_LOCK = threading.Lock()


def source_for(source_id: str, credential_store: CredentialStore) -> SoundSource | None:
    """Return the live adapter for `source_id`, constructing it once."""
    cls = _SOURCES.get(source_id)
    if cls is None:
        return None
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(source_id)
        if instance is None:
            instance = cls(credential_store)
            _INSTANCES[source_id] = instance
        return instance


def all_sources(credential_store: CredentialStore) -> list[SoundSource]:
    out: list[SoundSource] = []
    for name in _SOURCES:
        src = source_for(name, credential_store)
        if src is not None:
            out.append(src)
    return out


def list_source_ids() -> list[str]:
    return sorted(_SOURCES.keys())


# ---------------------------------------------------------------------------
# Shared media probe — ffprobe if available, else None.
#
# The bundled app ships an ffprobe next to the gateway; a checkout finds
# one on $PATH. If neither is present, the library records `format=None`
# and the test suite stubs the probe — a missing probe is not a reason
# to refuse a known-good download.
# ---------------------------------------------------------------------------

def _find_ffprobe() -> str | None:
    bundled = Path(config.ROOT) / "tools" / "ffprobe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("ffprobe")


def probe_audio(path: Path) -> dict[str, Any] | None:
    """Return `{format, duration_seconds, sample_rate, channels, bit_rate}` or None."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = payload.get("format") or {}
    duration_raw = fmt.get("duration")
    duration_s: float | None
    try:
        duration_s = float(duration_raw) if duration_raw not in (None, "") else None
    except (TypeError, ValueError):
        duration_s = None
    sample_rate: int | None = None
    channels: int | None = None
    if streams:
        sr = streams[0].get("sample_rate")
        try:
            sample_rate = int(sr) if sr not in (None, "") else None
        except (TypeError, ValueError):
            sample_rate = None
        ch = streams[0].get("channels")
        try:
            channels = int(ch) if ch not in (None, "") else None
        except (TypeError, ValueError):
            channels = None
    return {
        "format": fmt.get("format_name") or fmt.get("format_long_name"),
        "duration_seconds": duration_s,
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate")).isdigit() else None,
    }


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()