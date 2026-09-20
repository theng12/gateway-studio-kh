"""Voice registry — one catalogue across providers that disagree about voices.

The providers do not share a concept, and pretending otherwise produces a UI
that lies. Concretely:

    Studio Hub    voices are local reference clips (a file + a transcript)
    ElevenLabs    voices are account entries, drawn from a large public library
    Fish Audio    voices are *models*: your own trained clones, plus a public
                  library, and both are addressed by the same `_id`
    everything else   has no voice concept at all — the model is the voice

So a voice here is a record with a `source` that says where it came from, and
each provider kind has its own discovery. What the registry unifies is the part
that genuinely is common: a stable id, a display name, a preview, and whether
the owner has switched it on.

**Discovery and availability are separate, exactly as for models.** Everything a
provider reports is discovered; only enabled voices reach Story Studio. The
ElevenLabs public library alone holds tens of thousands of voices — publishing
all of them would be worse than useless.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import (
    CUSTOM_PROVIDERS_PATH,  # noqa: F401  (re-exported for callers)
    DATA_DIR,
    VOICES_PATH,
    WRITABLE_VOICES_PATH,
    write_secret,
)
from .providers import registry
from .providers.definitions import (
    KIND_ELEVENLABS,
    KIND_FAL,
    KIND_FISH,
    KIND_STUDIO_HUB,
    ProviderDefinition,
)

STATE_PATH = DATA_DIR / "voices_state.json"
DISCOVERY_TTL_S = 900.0
TIMEOUT = httpx.Timeout(30.0, connect=8.0)
PREVIEW_TIMEOUT = httpx.Timeout(30.0, connect=8.0)

ID_SEPARATOR = "::"

#: Where a voice came from. Drives both display and what can be done with it.
SOURCE_LOCAL = "local"        # this Mac's own reference clips
SOURCE_ACCOUNT = "account"    # the owner's own entry at the provider
SOURCE_LIBRARY = "library"    # the provider's public library, not yet owned


def make_id(provider_id: str, provider_voice_id: str) -> str:
    return f"{provider_id}{ID_SEPARATOR}{provider_voice_id}"


def split_id(voice_id: str) -> tuple[str, str]:
    provider_id, _, remainder = voice_id.partition(ID_SEPARATOR)
    return provider_id, remainder


@dataclass(slots=True)
class Voice:
    id: str
    provider_id: str
    provider_voice_id: str
    display_name: str
    source: str
    language: str = ""
    description: str = ""
    gender: str = ""
    accent: str = ""
    age: str = ""
    preview_url: str = ""
    owned: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "provider_voice_id": self.provider_voice_id,
            "display_name": self.display_name,
            "source": self.source,
            "language": self.language,
            "description": self.description,
            "gender": self.gender,
            "accent": self.accent,
            "age": self.age,
            "has_preview": bool(self.preview_url),
            "owned": self.owned,
            # **Not `tts_model_id`.** That name is a *serving* claim — Story
            # Studio drops any voice whose `tts_model_id` is not the model being
            # asked about — and a local voice serves every model that declares
            # cloning. What this stamp actually records is the tier the clip was
            # imported and annotated against, which decides nothing at render
            # time, so it is published under a name that says so.
            "minted_against_model": (self.raw or {}).get("tts_model_id"),
            # The clip's own length, when it was measured at import. Reported
            # rather than inferred: a voice whose duration was never recorded
            # says `None` instead of a plausible guess.
            "duration_seconds": (self.raw or {}).get("duration_seconds"),
        }


# ---------------------------------------------------------------------------
# Discovery, per provider kind
# ---------------------------------------------------------------------------
def _elevenlabs_account(definition: ProviderDefinition, values: dict[str, str]) -> list[Voice]:
    """The workspace's own voices.

    `/v2/voices` is the current endpoint; `/v1/voices` is kept as a fallback so
    an older account is not left with an empty list.
    """
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    headers = {"xi-api-key": values.get("api_key", "")}
    voices: list[Voice] = []

    for path in ("/v2/voices?page_size=100", "/v1/voices"):
        try:
            response = httpx.get(f"{base}{path}", headers=headers, timeout=TIMEOUT)
        except httpx.HTTPError:
            continue
        if response.status_code >= 400:
            continue
        payload = response.json() or {}
        items = payload.get("voices")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("voice_id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not voice_id or not name:
                continue
            labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
            description = str(item.get("description") or "").strip()
            if not description:
                description = ", ".join(str(v) for v in labels.values() if v)
            voices.append(
                Voice(
                    id=make_id(definition.id, voice_id),
                    provider_id=definition.id,
                    provider_voice_id=voice_id,
                    display_name=name,
                    source=SOURCE_ACCOUNT,
                    language=str(labels.get("language") or "")[:24],
                    description=description[:300],
                    gender=str(labels.get("gender") or ""),
                    accent=str(labels.get("accent") or ""),
                    age=str(labels.get("age") or ""),
                    preview_url=str(item.get("preview_url") or ""),
                    owned=True,
                    raw={"category": item.get("category")},
                )
            )
        if voices:
            break
    return voices


def _fal_tts_voices(definition: ProviderDefinition, values: dict[str, str]) -> list[Voice]:
    """The voices fal's ElevenLabs TTS endpoints accept.

    **Curated, because fal publishes none.** Its model page and OpenAPI schema for
    `fal-ai/elevenlabs/tts/*` carry no voice-list endpoint and no `enum` on the
    `voice` field — only twenty `examples` (Aria, Roger, Sarah … Bill) and a
    default of `Rachel`. Without them a TTS job has no voice to name and Story
    Studio's picker is empty, so the lane would look connected and produce nothing.

    The names are passed through verbatim as `provider_voice_id`, which is what
    the endpoint expects; fal validates the string server-side against the
    ElevenLabs voice set, so a name outside this list is a provider error rather
    than a gateway one.

    ponytail: a hand-copied list of fal's own examples. Ceiling — a voice fal
    accepts but does not `example` cannot be picked here. Upgrade path — read the
    list from fal if it ever publishes one, or let the owner add names by hand.
    """
    return [
        Voice(
            id=make_id(definition.id, name),
            provider_id=definition.id,
            provider_voice_id=name,
            display_name=name,
            source=SOURCE_ACCOUNT,
            language="",
            description="ElevenLabs voice, served through fal",
            owned=True,
            raw={"engine_name": "ElevenLabs (via fal)", "catalog_status": "published"},
        )
        for name in FAL_TTS_VOICE_NAMES
    ]


#: The twenty voices fal lists as `examples` for `fal-ai/elevenlabs/tts/*`, plus
#: none invented. `Rachel` is fal's own default, so it is first.
FAL_TTS_VOICE_NAMES: tuple[str, ...] = (
    "Rachel", "Aria", "Roger", "Sarah", "Laura", "Charlie", "George", "Callum",
    "River", "Liam", "Charlotte", "Alice", "Matilda", "Will", "Jessica", "Eric",
    "Chris", "Brian", "Daniel", "Lily", "Bill",
)


def _fish_models(
    definition: ProviderDefinition, values: dict[str, str], *, mine: bool
) -> list[Voice]:
    """Fish addresses both your clones and the public library through `/model`.

    `self=true` narrows it to models this account owns; without it the endpoint
    is the public library.
    """
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {values.get('api_key', '')}"}
    voices: list[Voice] = []
    page = 1
    while page <= 20:
        params: dict[str, Any] = {"page_size": 100, "page_number": page}
        if mine:
            params |= {"self": "true", "sort_by": "created_at"}
        try:
            response = httpx.get(
                f"{base}/model", headers=headers, params=params, timeout=TIMEOUT
            )
        except httpx.HTTPError:
            break
        if response.status_code >= 400:
            break
        payload = response.json() or {}
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            break
        for item in items:
            voice = _fish_voice(definition, item, mine=mine)
            if voice is not None:
                voices.append(voice)
        total = payload.get("total")
        if len(items) < 100 or (isinstance(total, int) and len(voices) >= total):
            break
        page += 1
    return voices


def _fish_voice(definition: ProviderDefinition, item: Any, *, mine: bool) -> Voice | None:
    if not isinstance(item, dict):
        return None
    # The id is `_id`; `id` appears in some responses and is worth honouring.
    voice_id = str(item.get("_id") or item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    if not voice_id or not title:
        return None
    # Fish lists non-speech and untrained entries too; those are not voices.
    if item.get("type") not in (None, "tts"):
        return None
    if item.get("state") not in (None, "trained"):
        return None

    samples = item.get("samples") if isinstance(item.get("samples"), list) else []
    preview = ""
    for sample in samples:
        if isinstance(sample, dict) and sample.get("audio"):
            preview = str(sample["audio"])
            break

    languages = item.get("languages")
    language = ""
    if isinstance(languages, list) and languages:
        language = str(languages[0])[:24]

    return Voice(
        id=make_id(definition.id, voice_id),
        provider_id=definition.id,
        provider_voice_id=voice_id,
        display_name=title,
        source=SOURCE_ACCOUNT if mine else SOURCE_LIBRARY,
        language=language,
        description=str(item.get("description") or "")[:300],
        preview_url=preview,
        owned=mine,
        raw={"task_count": item.get("task_count"), "like_count": item.get("like_count")},
    )


def _local_voices() -> list[Voice]:
    """This Mac's own reference clips, from the voice catalogue.

    The writable copy is preferred **at read time**. `VOICES_PATH` is resolved once
    when the process starts, so on a fresh install it points at the shipped default
    and keeps pointing there — a voice added afterwards would not appear until a
    restart, which reads exactly like the import having failed.
    """
    path = WRITABLE_VOICES_PATH if WRITABLE_VOICES_PATH.exists() else VOICES_PATH
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("voices") if isinstance(payload, dict) else payload
    voices: list[Voice] = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        raw_id = str(entry["id"])
        voices.append(
            Voice(
                # Local voices keep a plain id: they have no provider and are
                # already addressed directly by the catalogue.
                id=make_id("studio-hub-local", raw_id),
                provider_id="studio-hub-local",
                provider_voice_id=raw_id,
                display_name=str(entry.get("display_name") or raw_id),
                source=SOURCE_LOCAL,
                language=str(entry.get("language") or ""),
                description=str(entry.get("description") or ""),
                gender=str(entry.get("gender") or ""),
                preview_url="",
                owned=True,
                raw={
                    "engine_name": entry.get("engine_name"),
                    "tts_model_id": entry.get("tts_model_id"),
                    # How long the recording is, as measured when it was imported.
                    # Needed to say whether a model's window will cut it, and it
                    # is the *clip* length rather than the speech span the
                    # segments describe.
                    "duration_seconds": entry.get("duration_seconds"),
                    # Carried through so the Studio Hub adapter can stage the
                    # clone reference without re-reading the catalogue file.
                    "reference_path": entry.get("reference_path"),
                    "reference_transcript": entry.get("reference_transcript"),
                    "reference_segments": entry.get("reference_segments"),
                    # Whether the owner has granted permission to clone and
                    # distribute this voice. False until he says so, per voice.
                    "permission_acknowledged": bool(entry.get("permission_acknowledged")),
                    "license": entry.get("license"),
                },
            )
        )
    return voices


def _studio_hub_voices(definition: ProviderDefinition, _values: dict[str, str]) -> list[Voice]:
    """The gateway's own voice catalogue — published **once**, not once per location.

    Every Studio Hub location used to run `_local_voices()`, which reads *this Mac's*
    catalogue and ignores the definition entirely. Two things were wrong with that:

      * with N locations every voice appeared N times — three junk locations turned
        ten voices into thirty identical rows, and the owner saw it;
      * a remote controller's own library was never read at all, so a location with
        voices the gateway had never heard of contributed nothing while claiming to
        contribute this Mac's.

    The handoff with the Studio Hub session settles which behaviour is wanted:
    *"Gateway owns the voice list, each node owns its library."* The list is the
    gateway's, so it is published by the gateway alone. A location's own library is
    reached by staging a reference to it, not by listing it here.
    """
    from .fleet import LOCAL_ID, provider_id_for

    if definition.id != provider_id_for(LOCAL_ID):
        return []
    return _local_voices()


_DISCOVERY = {
    KIND_ELEVENLABS: _elevenlabs_account,
    KIND_FAL: _fal_tts_voices,
    KIND_STUDIO_HUB: _studio_hub_voices,
}


def discover(definition: ProviderDefinition, values: dict[str, str]) -> list[Voice]:
    """Account-side voices for one provider. Never raises."""
    if definition.kind == KIND_FISH:
        return _fish_models(definition, values, mine=True)
    handler = _DISCOVERY.get(definition.kind)
    if handler is None:
        return []
    try:
        return handler(definition, values)
    except Exception:  # noqa: BLE001 - a provider must never break the registry
        return []


def search_library(
    definition: ProviderDefinition, values: dict[str, str], query: str, page: int = 0
) -> list[Voice]:
    """Live search of a provider's *public* library.

    Deliberately not cached: ElevenLabs' library alone holds tens of thousands
    of voices, and the owner wants a handful. Search on demand, import what is
    wanted, and let it become an account voice.
    """
    query = query.strip()
    try:
        if definition.kind == KIND_ELEVENLABS:
            return _elevenlabs_library(definition, values, query, page)
        if definition.kind == KIND_FISH:
            return _fish_models(definition, values, mine=False)
    except Exception:  # noqa: BLE001
        return []
    return []


def _elevenlabs_library(
    definition: ProviderDefinition, values: dict[str, str], query: str, page: int
) -> list[Voice]:
    base = (values.get("base_url") or definition.base_url).rstrip("/")
    headers = {"xi-api-key": values.get("api_key", "")}
    params: dict[str, Any] = {"page": max(page, 0), "page_size": 30}
    if query:
        params["search"] = query

    response = httpx.get(
        f"{base}/v1/shared-voices", headers=headers, params=params, timeout=TIMEOUT
    )
    if response.status_code >= 400:
        return []
    payload = response.json() or {}
    items = payload.get("voices")
    if not isinstance(items, list):
        return []

    voices: list[Voice] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        voice_id = str(item.get("voice_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not voice_id or not name:
            continue
        owner = str(item.get("owner_id") or item.get("public_owner_id") or "").strip()
        voices.append(
            Voice(
                id=make_id(definition.id, voice_id),
                provider_id=definition.id,
                provider_voice_id=voice_id,
                display_name=name,
                source=SOURCE_LIBRARY,
                language=str(item.get("language") or "")[:24],
                description=str(item.get("description") or "")[:300],
                gender=str(item.get("gender") or ""),
                accent=str(item.get("accent") or ""),
                age=str(item.get("age") or ""),
                preview_url=str(item.get("preview_url") or ""),
                # Library voices are not usable until imported into the account.
                owned=False,
                raw={"owner_id": owner, "use_case": item.get("use_case")},
            )
        )
    return voices


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class VoiceRegistry:
    def __init__(self, path: Any = None) -> None:
        self.path = path or STATE_PATH
        self._data: dict[str, Any] = {"enabled": {}, "discovered": {}, "custom": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(parsed, dict):
            for key in ("enabled", "discovered", "custom"):
                if isinstance(parsed.get(key), dict):
                    self._data[key] = parsed[key]

    def _save(self) -> None:
        write_secret(self.path, json.dumps(self._data, indent=2) + "\n")

    # -- discovery --------------------------------------------------------
    def refresh(self, provider_id: str, *, force: bool = True) -> dict[str, Any]:
        definition = registry.definition(provider_id)
        if definition is None:
            raise KeyError(provider_id)

        cached = self._data["discovered"].get(provider_id) or {}
        fetched_at = cached.get("fetched_at") or 0
        if not force and (time.time() - fetched_at) < DISCOVERY_TTL_S:
            return {"provider_id": provider_id, "count": len(cached.get("voices") or []),
                    "error": cached.get("error"), "cached": True}

        values = registry.credentials.resolved(provider_id)
        voices = discover(definition, values)
        error = None
        if not voices and not registry.credentials.is_configured(provider_id):
            error = "no API key configured"

        self._data["discovered"][provider_id] = {
            "fetched_at": time.time(),
            "error": error,
            "voices": [v.as_dict() | {"raw": v.raw} for v in voices],
        }
        self._save()
        return {"provider_id": provider_id, "count": len(voices), "error": error, "cached": False}

    def refresh_all(self) -> dict[str, Any]:
        outcomes: dict[str, Any] = {}
        for definition in registry.definitions():
            if definition.kind not in _DISCOVERY and definition.kind != KIND_FISH:
                continue
            if not registry.credentials.is_configured(definition.id):
                continue
            outcomes[definition.id] = self.refresh(definition.id)
        return outcomes

    # -- reads ------------------------------------------------------------
    def _from_cached(self, provider_id: str) -> list[Voice]:
        cached = self._data["discovered"].get(provider_id) or {}
        out: list[Voice] = []
        for item in cached.get("voices") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out.append(
                Voice(
                    id=str(item["id"]),
                    provider_id=str(item.get("provider_id") or provider_id),
                    provider_voice_id=str(item.get("provider_voice_id") or ""),
                    display_name=str(item.get("display_name") or ""),
                    source=str(item.get("source") or SOURCE_ACCOUNT),
                    language=str(item.get("language") or ""),
                    description=str(item.get("description") or ""),
                    gender=str(item.get("gender") or ""),
                    accent=str(item.get("accent") or ""),
                    age=str(item.get("age") or ""),
                    preview_url=str(item.get("preview_url") or ""),
                    owned=bool(item.get("owned", True)),
                    raw=item.get("raw") or {},
                )
            )
        return out

    def _custom(self, provider_id: str) -> list[Voice]:
        out: list[Voice] = []
        for item in self._data["custom"].get(provider_id) or []:
            if isinstance(item, dict) and item.get("id"):
                out.append(
                    Voice(
                        id=str(item["id"]),
                        provider_id=provider_id,
                        provider_voice_id=str(item.get("provider_voice_id") or ""),
                        display_name=str(item.get("display_name") or ""),
                        source=SOURCE_ACCOUNT,
                        language=str(item.get("language") or ""),
                        description=str(item.get("description") or ""),
                        owned=True,
                        raw=item.get("raw") or {},
                    )
                )
        return out

    def is_enabled(self, voice: Voice) -> bool:
        explicit = (self._data["enabled"].get(voice.provider_id) or {}).get(voice.provider_voice_id)
        if explicit is not None:
            return bool(explicit)
        # Local voices are the owner's own and are on by default, matching how
        # Studio Hub models behave. Everything else is opt-in: a provider
        # library can hold tens of thousands of voices.
        return voice.source == SOURCE_LOCAL

    def voices(
        self, *, provider_id: str | None = None, include_disabled: bool = True
    ) -> list[Voice]:
        out: list[Voice] = []
        for definition in registry.definitions():
            if provider_id and definition.id != provider_id:
                continue
            if definition.kind == KIND_STUDIO_HUB:
                # Through the same function `discover()` uses, not `_local_voices()`
                # directly. Calling it here was the actual site of the multiplication
                # bug: this path ignored the definition, so every added location
                # appended the whole local catalogue again — ten voices became twenty,
                # then thirty — and it bypassed `discover()`, so a fix made there
                # changed nothing the owner could see.
                out.extend(_studio_hub_voices(definition, {}))
                continue
            if not registry.credentials.is_configured(definition.id):
                continue
            out.extend(self._from_cached(definition.id))
            out.extend(self._custom(definition.id))
        if not include_disabled:
            out = [v for v in out if self.is_enabled(v)]
        return sorted(out, key=lambda v: (v.provider_id, v.display_name.lower()))

    def enabled_voices(self) -> list[Voice]:
        return self.voices(include_disabled=False)

    def find(self, voice_id: str) -> Voice | None:
        for voice in self.voices():
            if voice.id == voice_id:
                return voice
        return None

    def summary(self) -> dict[str, Any]:
        all_voices = self.voices()
        enabled = [v for v in all_voices if self.is_enabled(v)]
        by_source: dict[str, int] = {}
        for voice in enabled:
            by_source[voice.source] = by_source.get(voice.source, 0) + 1
        return {
            "discovered": len(all_voices),
            "enabled": len(enabled),
            "by_source": by_source,
        }

    # -- writes -----------------------------------------------------------
    def set_enabled(self, voice_id: str, enabled: bool) -> bool:
        provider_id, provider_voice_id = split_id(voice_id)
        if not provider_voice_id:
            return False
        bucket = self._data["enabled"].setdefault(provider_id, {})
        bucket[provider_voice_id] = bool(enabled)
        self._save()
        return True

    def add_custom(
        self,
        provider_id: str,
        provider_voice_id: str,
        display_name: str,
        *,
        language: str = "",
        description: str = "",
        raw: dict[str, Any] | None = None,
    ) -> Voice:
        """Register a voice the provider's list does not return.

        Used when a library voice has just been imported into the account, so it
        is usable immediately rather than after the next discovery.
        """
        entry = {
            "id": make_id(provider_id, provider_voice_id),
            "provider_voice_id": provider_voice_id,
            "display_name": display_name or provider_voice_id,
            "language": language,
            "description": description,
            "raw": raw or {},
        }
        bucket = self._data["custom"].setdefault(provider_id, [])
        bucket = [item for item in bucket if item.get("provider_voice_id") != provider_voice_id]
        bucket.append(entry)
        self._data["custom"][provider_id] = bucket
        self._save()
        self.set_enabled(entry["id"], True)
        return Voice(
            id=entry["id"],
            provider_id=provider_id,
            provider_voice_id=provider_voice_id,
            display_name=entry["display_name"],
            source=SOURCE_ACCOUNT,
            language=language,
            description=description,
            owned=True,
            raw=raw or {},
        )

    def remove_custom(self, voice_id: str) -> bool:
        provider_id, provider_voice_id = split_id(voice_id)
        bucket = self._data["custom"].get(provider_id) or []
        before = len(bucket)
        self._data["custom"][provider_id] = [
            item for item in bucket if item.get("provider_voice_id") != provider_voice_id
        ]
        (self._data["enabled"].get(provider_id) or {}).pop(provider_voice_id, None)
        self._save()
        return len(self._data["custom"][provider_id]) != before

    # -- previews ---------------------------------------------------------
    def preview(self, voice_id: str) -> tuple[bytes, str]:
        """Fetch preview audio, proxied through the gateway.

        Proxying rather than handing the app a provider URL: preview URLs are
        sometimes credentialed, and the app should not have to know how each
        provider authenticates.
        """
        voice = self.find(voice_id)
        if voice is None:
            raise KeyError(voice_id)

        # Local voices preview from their own reference clip.
        if voice.source == SOURCE_LOCAL:
            path = (voice.raw or {}).get("reference_path")
            if path:
                from pathlib import Path

                candidate = Path(str(path))
                if candidate.exists():
                    media = "audio/wav" if candidate.suffix == ".wav" else "audio/mpeg"
                    return candidate.read_bytes(), media

        if voice.preview_url:
            response = httpx.get(voice.preview_url, timeout=PREVIEW_TIMEOUT, follow_redirects=True)
            if response.status_code < 400 and response.content:
                media = response.headers.get("content-type", "").split(";")[0].strip()
                return response.content, media or "audio/mpeg"

        # Nothing stored: synthesise a short sample through the provider.
        return self._synth_preview(voice)

    def _synth_preview(self, voice: Voice) -> tuple[bytes, str]:
        definition = registry.definition(voice.provider_id)
        if definition is None:
            raise KeyError(voice.id)

        from .adapters import BackendError, BackendRequest, adapter_for

        sample = "This is how I sound. Select me and I will read your script."
        request = BackendRequest(
            provider_id=definition.id,
            model_id=self._preview_model(definition),
            modality="tts",
            params={"text": sample, "reference_id": voice.provider_voice_id,
                    "voice_id": voice.provider_voice_id},
            values=registry.credentials.resolved(definition.id),
            base_url=definition.base_url,
            display_name=definition.display_name,
        )
        try:
            result = adapter_for(definition).generate(request)
        except BackendError as exc:
            raise KeyError(str(exc)) from exc
        return (result.content or b""), (result.media_type or "audio/wav")

    @staticmethod
    def _preview_model(definition: ProviderDefinition) -> str:
        """The model id to synthesise a preview with.

        Provider voices are not tied to one model, so the definition's first
        catalogued TTS model is used.
        """
        for model in definition.static_models:
            if model.get("modality") == "tts":
                return str(model["id"])
        return ""


REGISTRY: VoiceRegistry | None = None


def voice_registry() -> VoiceRegistry:
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = VoiceRegistry()
    return REGISTRY


# ---------------------------------------------------------------------------
# The Story Studio projection
# ---------------------------------------------------------------------------
def resolve(voice_id: str) -> Voice | None:
    """Find a voice by the id Story Studio sends back.

    Story Studio echoes whatever `id` it was given, so the id is the compound
    `provider::voice` form: unambiguous, and it tells the adapters exactly which
    provider and which upstream id to use without either having to guess.
    """
    if not voice_id:
        return None
    return voice_registry().find(voice_id)


def story_row(voice: Voice) -> dict[str, Any]:
    """One voice in the shape Story Studio's parser expects.

    The price fields ride along for the same reason the catalogue rows carry
    them: the parser drops entries whose price fields are missing. This docstring
    said that while the row published only `credits_per_1000_characters`, so the
    other three of the model catalogue's zero-constants were absent — and the same
    omission was in a **second** voice encoder in `catalog.py` that nothing
    called. That duplicate is gone now; this is the one encoder, and it carries
    the full balanced set. `chargeable` especially: the TTS quote route already
    publishes it explicitly because the parser tests `=== true`, and an absent
    field renders a claim the gateway never made.
    """
    definition = registry.definition(voice.provider_id)
    engine = str((voice.raw or {}).get("engine_name") or "")
    if not engine:
        engine = definition.display_name if definition else voice.provider_id

    return {
        # The id Story Studio sends back as `voice_id`.
        "id": voice.id,
        "display_name": voice.display_name,
        # Unknown language publishes the sentinel for "many, unenumerated"
        # rather than "en". These are ElevenLabs, Fish and local cloning voices —
        # none of them English-only — and the GenStudio this replaces published
        # `multilingual` for the same voices, so `"en"` was both a false claim
        # and a regression against the known-good contract.
        "language": voice.language or "multilingual",
        "description": voice.description or "",
        "provider": definition.display_name if definition else voice.provider_id,
        "engine_name": engine,
        "engine_id": voice.provider_id,
        "model_family": voice.source,
        "model_revision_id": None,
        "voice_tier": "custom" if voice.source != SOURCE_LIBRARY else "library",
        "voice_revision": 1,
        "custom_voice_id": voice.provider_voice_id if voice.owned else None,
        "public_voice_id": None if voice.owned else voice.provider_voice_id,
        "credits_per_1000_characters": 0,
        "minimum_credits": 0,
        "price_version": "flat",
        "chargeable": True,
        # ElevenLabs returns mp3 and Fish can return either; both are accepted
        # by Story Studio, which transcodes locally.
        "output_format": "mp3" if voice.provider_id.startswith("elevenlabs") else "wav",
        # MUST be exactly 'published': Story Studio treats any other value as a
        # generation gate and silently hides the voice.
        "catalog_status": "published",
        "quote_enabled": True,
        "generation_enabled": True,
        "preview_available": bool(voice.preview_url) or voice.source == SOURCE_LOCAL,
        "preview_url": None,
        "preview_text": None,
        "preview_notice": None,
        "is_saved": False,
        # **Never name a model here unless it is the one serving the request.**
        #
        # Story Studio reads `/v1/voices?model_id=X` as authoritative for scope,
        # and keeps one defensive check on top: "a row explicitly naming a
        # different model can never be offered for this one". A local voice used
        # to publish the tier it was *minted against* — every one of the owner's
        # said `Qwen3-TTS-12Hz-0.6B-Base-4bit` while the catalogue id is
        # `studio-hub::mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit`. Not equal,
        # so Story Studio dropped all ten and the narration picker was empty for
        # every local model — the gateway returning them was only half the fix.
        #
        # `null` is the honest answer to "which model serves this voice": a local
        # voice serves every model that declares cloning, and Story Studio's own
        # comment says a row that omits the field inherits the scoped endpoint's
        # answer. The mint stamp is kept in `raw` for diagnostics, under no
        # published name.
        "tts_model_id": None,
        "tts_model_name": "",
    }


def accepted_voice_providers(provider_id: str, model_id: str) -> set[str]:
    """Which providers' voices may be used with this model.

    Normally one: a voice belongs to the provider that serves the model. The
    exception is a **proxy that speaks another provider's ids unchanged** — fal's
    ElevenLabs routes, which the owner has driven with ElevenLabs voice ids many
    times. Without it the picker offered only the twenty names fal happens to
    `example`, so a voice saved from his own account was invisible for that lane
    while working perfectly through it. Keyed on the provider's *kind* and the
    word `elevenlabs` in the endpoint, never on a hardcoded provider id.
    """
    accepted = {provider_id}
    owner = registry.definition(provider_id)
    if owner is not None and owner.kind == KIND_FAL and "elevenlabs" in model_id.lower():
        accepted |= {d.id for d in registry.definitions() if d.kind == KIND_ELEVENLABS}
    return accepted


def _model_record(provider_id: str, model_id: str) -> dict[str, Any] | None:
    """A model's own record, looking behind a published maker id.

    Story Studio asks for `studio-hub::<model>` — the **maker** — while the model
    belongs to one *location* (`studio-hub-<controller>`), and that is where its
    catalogue facts were stored. Unless the published id is mapped back first,
    every lookup misses, and a miss is not neutral: it reads as *unverified*,
    which offers the voice (safe) but silently loses the contract — including the
    `declared: false` that must withhold a preset-voice model from a voice picker.
    """
    from .fleet import FLEET_PROVIDER_ID, publish_id

    candidates = [provider_id]
    if provider_id == FLEET_PROVIDER_ID:
        candidates = [d.id for d in registry.definitions()
                      if publish_id(d.id) == FLEET_PROVIDER_ID] or [provider_id]
    for candidate in candidates:
        for model in registry.models.models_for(candidate, include_disabled=True):
            if str(model.get("model_id")) == model_id:
                return model
    return None


def reference_contract(provider_id: str, model_id: str) -> dict[str, Any]:
    """The Hub's own statement about a model's reference clip, or `{}`.

    Read, never computed. Verified against Hub 0.6.18: `/api/hub/catalog`
    publishes per model a `reference_audio` block carrying `declared`,
    `supported`, `profile_audited` and — when the profile has been audited —
    `minimum`/`target`/`recommended`/`maximum_duration_seconds`, whether a
    transcript is required, and whether timestamped segments are needed for
    model-specific selection.

    `{}` means **unstated**, which is not the same as "no reference": a model
    absent from the catalogue (VoxCPM2 8-bit is installed but not catalogued)
    is *unverified*, and inventing a window for it — from its upstream docs or
    anywhere else — would be the plausible-default mistake this codebase keeps
    auditing out.
    """
    model = _model_record(provider_id, model_id)
    if model is None:
        return {}
    block = (model.get("raw") or {}).get("reference_audio")
    return dict(block) if isinstance(block, dict) else {}


def reference_support(provider_id: str, model_id: str) -> bool | None:
    """Does this model take a reference clip? `True`, `False`, or `None` if unstated.

    Three answers, because collapsing them is the bug. A model that *says* it
    cannot clone must not be offered voices — picking one would fail on use,
    which is the `:batch` trap. A model that says *nothing* must not be treated
    as one that cannot, because that is how ten working voices disappear from a
    picker with no error anywhere to explain it.
    """
    contract = reference_contract(provider_id, model_id)
    if contract:
        if contract.get("declared") is False or contract.get("supported") is False:
            return False
        if contract.get("declared") is True or contract.get("supported") is True:
            return True
    model = _model_record(provider_id, model_id)
    if model is None:
        return None
    capabilities = (model.get("raw") or {}).get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        return "voice-cloning" in capabilities
    return None


def voices_for_model(compound_model_id: str) -> list[dict[str, Any]]:
    """Voices usable with a namespaced catalogue model id.

    Two different rules, because the providers genuinely differ:

      * **which local models a local voice may be used with is the model's own
        statement**, not the tier the voice was minted against. Every voice this
        Mac owns was stamped `Qwen3-TTS-12Hz-0.6B-Base-4bit` when it was
        imported, and matching on that string alone hid all ten of them from the
        other seven clone-capable local rows — an empty picker, which reads as
        "no voices" rather than "the namespaces disagree". The clip is a
        recording; the window it needs belongs to the model, and the Hub cuts it;
      * a **provider** voice works with every TTS model that provider offers, so
        the match is on the provider alone.

    **The provider id is matched in both namespaces.** The catalogue publishes
    every Studio Hub row under the maker (`studio-hub::<model>`) while a local
    voice is minted under the concrete provider (`studio-hub-local`), so an
    exact string comparison silently matched nothing: the narration voice picker
    for the local lane returned an empty list, and empty reads as "no voices"
    rather than "the namespaces disagree". Both spellings are accepted because
    both are really sent — a saved Story Studio preset still holds the pre-merge
    id, which is exactly why every row also publishes `legacy_ids`.

    **fal's ElevenLabs routes take ElevenLabs voices, so those are offered too.**
    fal proxies ElevenLabs and accepts a voice id unchanged — the owner confirms
    he has used them that way many times — but the picker only offered the twenty
    names fal happens to `example`, because a voice is matched on its *provider*.
    So a voice he had saved from his own ElevenLabs account was invisible for the
    fal lane while working perfectly through it. A fal endpoint with `elevenlabs`
    in its id now also accepts every voice from an `elevenlabs` provider.
    """
    from .fleet import publish_id

    provider_id, _, raw_model = compound_model_id.partition(ID_SEPARATOR)
    if not provider_id:
        return [story_row(v) for v in voice_registry().enabled_voices()]

    accepted = accepted_voice_providers(provider_id, raw_model)
    takes_reference = reference_support(provider_id, raw_model)

    rows: list[dict[str, Any]] = []
    for voice in voice_registry().enabled_voices():
        if not ({voice.provider_id, publish_id(voice.provider_id)} & accepted):
            continue
        if voice.source == SOURCE_LOCAL and takes_reference is False:
            # The model states it does not clone (preset speakers, voice design,
            # a fixed voicepack). A reference offered here is a control that
            # fails on use, so it is withheld rather than advertised.
            continue
        rows.append(story_row(voice))
    return rows
