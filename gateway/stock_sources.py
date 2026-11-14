"""Stock footage connectors — search a provider's own catalogue, fetch into the library.

The same small shape `sound_sources` uses: one **declaration** per provider (what it
is, whether it is free, whether it needs a key, its licence/cost line) plus one
**adapter** (how to talk to it). A new source is a declaration and a class, never a
change to the library or the routes.

**Two questions, never one.** `configured` is "it has what it needs to be called";
`enabled` is "its own switch is on". A keyed provider is off until **both** a key
exists and its switch is on, and turning one on without its key is refused with the
key named rather than silently doing nothing. The switch lives in
``DATA_DIR/stock-sources.json``.

Free, keyless sources are on by default. Only `wikimedia` can work with no
configuration at all; `ai-stock` is free because it generates on the owner's own
Macs through the Gateway's t2v lane — it has no catalogue to search, it has a
`POST /v1/footage/generate` door, and the clip it produces lands in the footage
library with `ai_generated: true` and its provenance recorded.

**Licence is the gate.** Every adapter maps its provider's licence to a word this
gateway carries (`cc0`, `by`, or the provider's own free-use word) and refuses a
clip it cannot vouch for — an NC/ND/unknown clip never reaches search results.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import config
from .providers.credentials import CredentialStore, mask

log = logging.getLogger("gateway.stock_sources")

#: The most one fetch may pull. Stock clips are minutes, not features; anything
#: larger is refused rather than buffered into memory.
MAX_FETCH_BYTES = 512 * 1024 * 1024
_USER_AGENT = "GatewayKH/0.2 (footage library integration)"


class StockSourceError(Exception):
    """A refusal the route layer turns into a GatewayError."""

    def __init__(self, message: str, *, status: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StockItem:
    """One result, already carrying a licence this gateway will store."""

    source: str
    source_id: str
    title: str
    licence: str
    licence_note: str | None = None
    description: str | None = None
    author: str | None = None
    attribution: str | None = None
    source_url: str = ""
    download_url: str = ""
    filetype: str | None = None
    duration_seconds: float | None = None
    filesize: int | None = None
    tags: tuple[str, ...] = ()
    ai_generated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "source_id": self.source_id, "title": self.title,
            "description": self.description, "author": self.author,
            "licence": self.licence, "licence_note": self.licence_note,
            "attribution": self.attribution, "source_url": self.source_url,
            "filetype": self.filetype, "duration_seconds": self.duration_seconds,
            "filesize": self.filesize, "tags": list(self.tags),
            "ai_generated": self.ai_generated,
        }


@dataclass(frozen=True)
class StockSourceDefinition:
    """What a source is, what it costs, and where its key comes from."""

    id: str
    label: str
    kind: str
    #: Can a clip be taken without paying the provider?
    free: bool
    #: Does it require a credential before it can be called at all?
    needs_key: bool
    #: The licence/cost sentence shown beside the source **before** a fetch.
    cost: str
    description: str = ""
    docs_url: str = ""
    register_url: str = ""
    key_field: str = "api_key"
    key_env: str = ""
    key_label: str = "API key"
    default_enabled: bool = False
    default_format: str = "mp4"
    #: The store key, where it differs from the id.
    credential_id: str = ""

    @property
    def store_id(self) -> str:
        return self.credential_id or self.id


# ---------------------------------------------------------------------------
# Adapter interface
# ---------------------------------------------------------------------------
class StockSource:
    """Base class. A subclass implements `search`, `detail_for` and `fetch`."""

    kind = ""

    def __init__(self, definition: StockSourceDefinition, credential_store: CredentialStore) -> None:
        self.definition = definition
        self.id = definition.id
        self.label = definition.label
        self.credentials = credential_store
        self._client: httpx.Client | None = None

    # -- credentials -----------------------------------------------------
    def key(self) -> str:
        """The credential: stored value first, then `.env` — the same chain as a provider's."""
        stored = self.credentials.stored(self.definition.store_id).get(self.definition.key_field)
        if stored:
            return str(stored)
        return str(config.get(self.definition.key_env, "") or "")

    def is_configured(self) -> bool:
        return (not self.definition.needs_key) or bool(self.key())

    def is_enabled(self) -> bool:
        """Its own switch, **and** the key it needs. A key alone does not turn it on."""
        if not self.is_configured():
            return False
        return _switch(self.definition)

    def is_available(self) -> bool:
        return self.is_configured() and self.is_enabled()

    # -- HTTP ------------------------------------------------------------
    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    # -- subclass contract ------------------------------------------------
    def search(self, query: str, limit: int = 10) -> list[StockItem]:
        raise NotImplementedError

    def detail_for(self, source_id: str) -> StockItem | None:
        raise NotImplementedError

    def fetch(self, item: StockItem, *, timeout: float = 120.0) -> bytes:
        """Bytes, re-checked against the same ceiling on the wire."""
        if not item.download_url:
            raise StockSourceError("The clip has no download URL.", status=502)
        try:
            with self._get_client().stream("GET", item.download_url, timeout=timeout) as response:
                if response.status_code != 200:
                    raise StockSourceError(
                        f"Upstream download returned {response.status_code}.", status=502)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    total += len(chunk)
                    if total > MAX_FETCH_BYTES:
                        raise StockSourceError("The clip is larger than 512 MB.", status=413)
                    chunks.append(chunk)
        except StockSourceError:
            raise
        except httpx.HTTPError as exc:
            raise StockSourceError(f"Upstream download failed: {exc}", status=502) from exc
        return b"".join(chunks)

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "free": self.definition.free,
            "configured": self.is_configured(),
            "enabled": self.is_enabled(),
            "available": self.is_available(),
            "needs_key": self.definition.needs_key,
            "key_field": self.definition.key_field,
            "key_label": self.definition.key_label,
            "key_masked": mask(self.key()) if self.definition.needs_key else None,
            "cost": self.definition.cost,
            "description": self.definition.description,
            "docs_url": self.definition.docs_url,
            "register_url": self.definition.register_url,
        }


# ---------------------------------------------------------------------------
# Switch state (persisted, per source)
# ---------------------------------------------------------------------------
def _state_path() -> Path:
    return config.DATA_DIR / "stock-sources.json"


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _switch(definition: StockSourceDefinition) -> bool:
    enabled = _read_state().get("enabled")
    if isinstance(enabled, dict) and definition.id in enabled:
        return bool(enabled[definition.id])
    return definition.default_enabled


def _write_switch(source_id: str, enabled: bool) -> None:
    from .config import write_secret
    state = _read_state()
    switches = dict(state.get("enabled") or {})
    switches[source_id] = bool(enabled)
    write_secret(_state_path(), json.dumps({"enabled": switches}, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str | None:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = " ".join(text.split())
    return text or None


def _seconds(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number >= 0 else None


def _extension(mime: Any, url: Any = "") -> str | None:
    word = str(mime or "").lower()
    for mime_type, ext in (("video/mp4", "mp4"), ("video/webm", "webm"),
                           ("video/quicktime", "mov"), ("video/x-matroska", "mkv"),
                           ("video/x-msvideo", "avi")):
        if mime_type in word:
            return ext
    suffix = str(url or "").split("?")[0].rsplit(".", 1)[-1].lower()
    return suffix if suffix in ("mp4", "webm", "mov", "mkv", "m4v", "avi") else None


# ---------------------------------------------------------------------------
# Wikimedia Commons — free, keyless, on by default
# ---------------------------------------------------------------------------
_WIKIMEDIA_BASE = os.environ.get("WIKIMEDIA_COMMONS_API",
                                 "https://commons.wikimedia.org/w/api.php")


def _commons_licence(raw: Any) -> str | None:
    """Commons' licence name -> `cc0`/`by`, or None when a monetised video may not carry it."""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if "cc0" in text or "public domain" in text:
        return "cc0"
    if text.startswith("cc by") or text.startswith("cc-by"):
        if "nc" not in text and "nd" not in text and "sa" not in text:
            return "by"
    return None


class WikimediaSource(StockSource):
    kind = "wikimedia"

    def _search_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._get_client().get(_WIKIMEDIA_BASE, params=params)
        except httpx.HTTPError as exc:
            raise StockSourceError(f"Wikimedia Commons could not be reached: {exc}",
                                   status=502, code="SOURCE_UNREACHABLE") from exc
        if response.status_code != 200:
            raise StockSourceError(
                f"Wikimedia Commons answered {response.status_code}.", status=502)
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _item(page: dict[str, Any]) -> StockItem | None:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        licence = _commons_licence((meta.get("LicenseShortName") or {}).get("value"))
        download_url = str(info.get("url") or "")
        title = str(page.get("title") or "").removeprefix("File:")
        if licence is None or not download_url or not title:
            return None
        short = _text((meta.get("LicenseShortName") or {}).get("value"))
        return StockItem(
            source="wikimedia",
            source_id=str(page.get("title") or title),
            title=title,
            licence=licence,
            licence_note=f"Wikimedia Commons: {short}" if short else "Wikimedia Commons",
            description=_text((meta.get("ImageDescription") or {}).get("value")),
            author=_text((meta.get("Artist") or {}).get("value")),
            attribution=_text((meta.get("Credit") or {}).get("value")),
            source_url=f"https://commons.wikimedia.org/wiki/{page.get('title') or title}",
            download_url=download_url,
            filetype=_extension(info.get("mime"), download_url),
            duration_seconds=_seconds(info.get("duration")
                                      or (meta.get("Duration") or {}).get("value")),
            filesize=info.get("size") if isinstance(info.get("size"), int) else None,
        )

    def search(self, query: str, limit: int = 10) -> list[StockItem]:
        terms = " ".join(str(query or "").split())
        payload = self._search_payload({
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"filetype:video {terms}".strip(), "gsrnamespace": 6,
            "gsrlimit": max(1, min(limit, 50)),
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        })
        pages = ((payload.get("query") or {}).get("pages") or {})
        items = [self._item(page) for page in pages.values() if isinstance(page, dict)]
        return [item for item in items if item is not None]

    def detail_for(self, source_id: str) -> StockItem | None:
        payload = self._search_payload({
            "action": "query", "format": "json", "titles": source_id,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        })
        pages = ((payload.get("query") or {}).get("pages") or {})
        for page in pages.values():
            item = self._item(page) if isinstance(page, dict) else None
            if item is not None:
                return item
        return None


# ---------------------------------------------------------------------------
# Pexels — free catalogue, needs a key
# ---------------------------------------------------------------------------
_PEXELS_BASE = os.environ.get("PEXELS_API_BASE", "https://api.pexels.com/videos")


class PexelsSource(StockSource):
    kind = "pexels"

    def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": self.key()} if self.key() else {}
        try:
            response = self._get_client().get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise StockSourceError(f"Pexels could not be reached: {exc}",
                                   status=502, code="SOURCE_UNREACHABLE") from exc
        if response.status_code in (401, 403):
            raise StockSourceError("Pexels refused the stored API key.", status=401,
                                   code="KEY_REFUSED")
        if response.status_code != 200:
            raise StockSourceError(f"Pexels answered {response.status_code}.", status=502)
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _item(video: dict[str, Any]) -> StockItem | None:
        files = [f for f in video.get("video_files") or []
                 if isinstance(f, dict) and f.get("link")]
        if not files:
            return None
        # The smallest file that is still a real picture: the first mp4 the API lists.
        chosen = next((f for f in files if "mp4" in str(f.get("file_type") or "")), files[0])
        return StockItem(
            source="pexels",
            source_id=str(video.get("id")),
            title=str(video.get("url") or video.get("id")),
            licence="pexels",
            licence_note="Pexels License — free to use, no attribution required.",
            author=(video.get("user") or {}).get("name"),
            source_url=str(video.get("url") or ""),
            download_url=str(chosen.get("link") or ""),
            filetype=_extension(chosen.get("file_type"), chosen.get("link")),
            duration_seconds=_seconds(video.get("duration")),
            tags=tuple(str(t) for t in video.get("tags") or [] if str(t).strip()),
        )

    def search(self, query: str, limit: int = 10) -> list[StockItem]:
        payload = self._request(_PEXELS_BASE, {"query": query,
                                               "per_page": max(1, min(limit, 80))})
        items = [self._item(video) for video in payload.get("videos") or []]
        return [item for item in items if item is not None]

    def detail_for(self, source_id: str) -> StockItem | None:
        payload = self._request(f"{_PEXELS_BASE}/videos/{source_id}", {})
        if not payload.get("id"):
            return None
        return self._item(payload)


# ---------------------------------------------------------------------------
# Pixabay — free catalogue, needs a key
# ---------------------------------------------------------------------------
_PIXABAY_BASE = os.environ.get("PIXABAY_API_BASE", "https://pixabay.com/api/videos/")


class PixabaySource(StockSource):
    kind = "pixabay"

    def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._get_client().get(_PIXABAY_BASE, params={**params, "key": self.key()})
        except httpx.HTTPError as exc:
            raise StockSourceError(f"Pixabay could not be reached: {exc}",
                                   status=502, code="SOURCE_UNREACHABLE") from exc
        if response.status_code in (401, 403):
            raise StockSourceError("Pixabay refused the stored API key.", status=401,
                                   code="KEY_REFUSED")
        if response.status_code != 200:
            raise StockSourceError(f"Pixabay answered {response.status_code}.", status=502)
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _item(hit: dict[str, Any]) -> StockItem | None:
        videos = hit.get("videos") or {}
        chosen = videos.get("large") or videos.get("medium") or videos.get("small")
        if not isinstance(chosen, dict) or not chosen.get("url"):
            return None
        return StockItem(
            source="pixabay",
            source_id=str(hit.get("id")),
            title=str(hit.get("tags") or hit.get("pageURL") or hit.get("id")),
            licence="pixabay",
            licence_note="Pixabay Content License — free to use.",
            author=hit.get("user"),
            source_url=str(hit.get("pageURL") or ""),
            download_url=str(chosen.get("url") or ""),
            filetype=_extension(None, chosen.get("url")),
            duration_seconds=_seconds(hit.get("duration")),
            filesize=chosen.get("size") if isinstance(chosen.get("size"), int) else None,
            tags=tuple(tag.strip() for tag in str(hit.get("tags") or "").split(",")
                       if tag.strip()),
        )

    def search(self, query: str, limit: int = 10) -> list[StockItem]:
        payload = self._request({"q": query, "per_page": max(3, min(limit, 100))})
        items = [self._item(hit) for hit in payload.get("hits") or []]
        return [item for item in items if item is not None]

    def detail_for(self, source_id: str) -> StockItem | None:
        payload = self._request({"id": source_id})
        hits = payload.get("hits") or []
        return self._item(hits[0]) if hits else None


# ---------------------------------------------------------------------------
# AI stock — generated on the owner's own Macs, no provider catalogue
# ---------------------------------------------------------------------------
class AiStockSource(StockSource):
    kind = "ai_stock"

    def search(self, query: str, limit: int = 10) -> list[StockItem]:
        return []  # nothing to search: a clip is generated, then stored

    def detail_for(self, source_id: str) -> StockItem | None:
        return None

    def fetch(self, item: StockItem, *, timeout: float = 120.0) -> bytes:
        raise StockSourceError(
            "AI stock is generated, not fetched. POST /v1/footage/generate with a model on your Macs.",
            status=400, code="GENERATE_NOT_FETCH")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
STOCK_SOURCE_DEFINITIONS: tuple[StockSourceDefinition, ...] = (
    StockSourceDefinition(
        id="wikimedia",
        label="Wikimedia Commons",
        kind="wikimedia",
        free=True,
        needs_key=False,
        cost="Free — Wikimedia Commons, CC0 or CC BY only. No key needed.",
        description="The free Commons video catalogue. Every result is checked for a "
                    "licence a monetised video may carry; NC, ND and ShareAlike are refused.",
        docs_url="https://commons.wikimedia.org/wiki/Commons:Video",
        default_enabled=True,
    ),
    StockSourceDefinition(
        id="pexels",
        label="Pexels",
        kind="pexels",
        free=True,
        needs_key=True,
        key_field="api_key",
        key_env="PEXELS_API_KEY",
        key_label="Pexels API key",
        cost="Free — Pexels License, no attribution required. Needs a Pexels API key.",
        description="Pexels' own free video catalogue.",
        docs_url="https://www.pexels.com/license/",
        register_url="https://www.pexels.com/api/",
        default_enabled=False,
    ),
    StockSourceDefinition(
        id="pixabay",
        label="Pixabay",
        kind="pixabay",
        free=True,
        needs_key=True,
        key_field="api_key",
        key_env="PIXABAY_API_KEY",
        key_label="Pixabay API key",
        cost="Free — Pixabay Content License. Needs a Pixabay API key.",
        description="Pixabay's own free video catalogue.",
        docs_url="https://pixabay.com/service/license-summary/",
        register_url="https://pixabay.com/api/docs/",
        default_enabled=False,
    ),
    StockSourceDefinition(
        id="ai-stock",
        label="Free AI stock (your Macs)",
        kind="ai_stock",
        free=True,
        needs_key=False,
        cost="Free — generated on your Macs; no provider charge.",
        description="Generates a clip on the local fleet through the Gateway's text-to-video "
                    "lane and stores it in the footage library with its provenance.",
        default_enabled=True,
    ),
)

STOCK_SOURCES_BY_ID: dict[str, StockSourceDefinition] = {
    definition.id: definition for definition in STOCK_SOURCE_DEFINITIONS
}

_ADAPTERS: dict[str, type[StockSource]] = {
    WikimediaSource.kind: WikimediaSource,
    PexelsSource.kind: PexelsSource,
    PixabaySource.kind: PixabaySource,
    AiStockSource.kind: AiStockSource,
}

for _definition in STOCK_SOURCE_DEFINITIONS:
    if _definition.kind not in _ADAPTERS:
        raise RuntimeError(
            f"stock source '{_definition.id}' declares kind '{_definition.kind}', "
            f"which has no adapter")
if {cls.kind for cls in _ADAPTERS.values()} != {d.kind for d in STOCK_SOURCE_DEFINITIONS}:
    raise RuntimeError("an adapter is registered for a kind no stock source declares")

_INSTANCES: dict[str, StockSource] = {}
_INSTANCES_LOCK = threading.Lock()


def definitions() -> tuple[StockSourceDefinition, ...]:
    return STOCK_SOURCE_DEFINITIONS


def definition(source_id: str) -> StockSourceDefinition | None:
    return STOCK_SOURCES_BY_ID.get(source_id)


def source_for(source_id: str, credential_store: CredentialStore) -> StockSource | None:
    declared = STOCK_SOURCES_BY_ID.get(source_id)
    if declared is None:
        return None
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(source_id)
        if instance is None:
            instance = _ADAPTERS[declared.kind](declared, credential_store)
            _INSTANCES[source_id] = instance
        return instance


def all_sources(credential_store: CredentialStore) -> list[StockSource]:
    return [source for declared in STOCK_SOURCE_DEFINITIONS
            if (source := source_for(declared.id, credential_store)) is not None]


def describe_all(credential_store: CredentialStore) -> list[dict[str, Any]]:
    return [source.status() for source in all_sources(credential_store)]


def set_enabled(source_id: str, enabled: bool,
                credential_store: CredentialStore) -> dict[str, Any]:
    """Flip one source's switch. A keyed source without its key is refused, naming the key."""
    source = source_for(source_id, credential_store)
    if source is None:
        raise ValueError(f"Unknown stock source: {source_id}")
    if enabled and source.definition.needs_key and not source.key():
        where = f" ({source.definition.key_env})" if source.definition.key_env else ""
        raise StockSourceError(
            f"{source.label} needs its {source.definition.key_label}{where} before it can be "
            f"switched on.", status=400, code="KEY_REQUIRED")
    _write_switch(source_id, enabled)
    return source.status()


# ---------------------------------------------------------------------------
# Free AI stock: generate on the fleet, then import the artifact
# ---------------------------------------------------------------------------
#: How long a generated clip is collected for. Bounded on purpose: a job that never
#: settles must not keep a daemon thread alive for ever.
MAX_WATCH_SECONDS = 3600.0


def assert_free_model(model_id: str) -> dict[str, Any]:
    """Resolve a video model and refuse a chargeable one by its own name.

    Paid generation stays off: a model whose catalogue row is not free/local is
    refused here, before a job is queued, rather than after it has cost money.
    """
    from .catalog import catalog
    from .pricing import definition_chargeable

    definition, model = catalog.resolve("video", model_id)
    if definition is None or model is None:
        raise StockSourceError(f'Video model "{model_id}" is not available.',
                               status=404, code="MODEL_NOT_FOUND")
    raw_id = str(model.get("model_id") or model_id)
    if definition_chargeable(definition, "video", raw_id, model.get("raw")):
        name = model.get("display_name") or raw_id
        raise StockSourceError(
            f'"{name}" is a paid model; free AI stock generates on your Macs only.',
            status=400, code="PAID_MODEL_REFUSED")
    return model


def finish_job(job_id: str, model_id: str, *, job: dict[str, Any] | None = None) -> str:
    """One look at a generated job. Copies its artifact into the library when settled.

    Returns `"pending"`, `"failed"` or `"imported"`. `job` lets a caller (and the
    tests) pass a settled job directly instead of reading the store.
    """
    from .store import TERMINAL_STATES, artifacts_of

    if job is None:
        from .runtime import store
        job = store.get(job_id)
    if not job or job.get("state") not in TERMINAL_STATES:
        return "pending"
    if job["state"] != "succeeded":
        return "failed"
    rows = artifacts_of(job)
    if not rows:
        return "failed"
    try:
        data = Path(rows[0]["path"]).read_bytes()
    except OSError as exc:
        log.warning("footage: generated artifact for %s is unreadable: %s", job_id, exc)
        return "failed"
    from . import footage_library
    footage_library.add_generated(data, model_id=model_id,
                                  title=f"Generated clip — {model_id}")
    return "imported"


def start_generation(job_id: str, model_id: str, *, poll_seconds: float = 5.0) -> None:
    """Watch a queued generation in the background. Never blocks the request."""
    thread = threading.Thread(target=_watch, args=(job_id, model_id),
                              kwargs={"poll_seconds": poll_seconds},
                              daemon=True, name=f"footage-generate-{job_id}")
    thread.start()


def _watch(job_id: str, model_id: str, *, poll_seconds: float = 5.0) -> None:
    deadline = time.time() + MAX_WATCH_SECONDS
    while time.time() < deadline:
        state = finish_job(job_id, model_id)
        if state != "pending":
            if state == "failed":
                log.warning("footage: generated job %s did not produce a clip", job_id)
            return
        time.sleep(poll_seconds)
    log.warning("footage: generated job %s was not collected within %ss",
                job_id, int(MAX_WATCH_SECONDS))
